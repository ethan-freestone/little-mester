#!/usr/bin/env python3
"""Bounded snapshot/rollback primitives for the fixed VM host-code update.

Callers supply only the installer's fixed target and managed-directory maps.
This is deliberately not a general backup or migration facility.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import pathlib
import re
import secrets
import stat


MAX_TARGET_BYTES = 2 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
SHA = re.compile(r"^[0-9a-f]{40}$")


class Refused(RuntimeError):
    pass


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(value: str) -> None:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise Refused("candidate and previous versions must be full lowercase commit SHAs")


def _path_chain(path: pathlib.Path, *, allow_missing_final: bool = True) -> tuple[bool, os.stat_result | None]:
    """Reject every symlink component; return target existence and lstat metadata."""
    absolute = pathlib.Path(os.path.abspath(path))
    current = pathlib.Path(absolute.anchor)
    parts = absolute.parts[1:]
    for index, part in enumerate(parts):
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if index == len(parts) - 1 or allow_missing_final:
                return False, None
            return False, None
        if stat.S_ISLNK(info.st_mode):
            raise Refused("protected update path contains a symlink")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise Refused("protected update path has a non-directory ancestor")
    return True, os.lstat(absolute)


def _read_regular(path: pathlib.Path) -> tuple[bytes, os.stat_result] | None:
    exists, info = _path_chain(path)
    if not exists:
        return None
    assert info is not None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_TARGET_BYTES:
        raise Refused("protected update target is not a bounded single-link regular file")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)):
            raise Refused("protected update target changed while being inspected")
        chunks = bytearray()
        while len(chunks) <= MAX_TARGET_BYTES:
            block = os.read(fd, min(65536, MAX_TARGET_BYTES + 1 - len(chunks)))
            if not block:
                break
            chunks.extend(block)
        if len(chunks) > MAX_TARGET_BYTES:
            raise Refused("protected update target exceeds its size bound")
        return bytes(chunks), opened
    finally:
        os.close(fd)


def _target_entries(target_map: dict[str, pathlib.Path]) -> list[dict]:
    if not isinstance(target_map, dict) or not target_map:
        raise Refused("fixed protected target inventory is empty or invalid")
    if len(set(target_map)) != len(target_map):
        raise Refused("duplicate protected target path")
    entries = []
    for logical, target in sorted(target_map.items()):
        if (not isinstance(logical, str) or not logical.startswith("/")
                or "\0" in logical or not isinstance(target, pathlib.Path)):
            raise Refused("invalid protected target inventory entry")
        observed = _read_regular(target)
        if observed is None:
            entries.append({"path": logical, "state": "absent"})
            continue
        data, info = observed
        entries.append({
            "path": logical,
            "state": "file",
            "uid": info.st_uid,
            "gid": info.st_gid,
            "mode": stat.S_IMODE(info.st_mode),
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "data": base64.b64encode(data).decode("ascii"),
        })
    return entries


def _directory_entries(managed_dirs: dict[str, tuple[pathlib.Path, int, int, int]]) -> list[dict]:
    entries = []
    for logical, spec in sorted(managed_dirs.items()):
        if (not isinstance(logical, str) or not logical.startswith("/")
                or not isinstance(spec, tuple) or len(spec) != 4
                or not isinstance(spec[0], pathlib.Path)):
            raise Refused("invalid managed directory inventory")
        path, expected_mode, expected_uid, expected_gid = spec
        exists, info = _path_chain(path)
        if not exists:
            entries.append({"path": logical, "state": "absent",
                            "candidate_mode": expected_mode, "candidate_uid": expected_uid,
                            "candidate_gid": expected_gid})
            continue
        assert info is not None
        if not stat.S_ISDIR(info.st_mode):
            raise Refused("managed protected directory is not a directory")
        entries.append({"path": logical, "state": "directory", "uid": info.st_uid,
                        "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode),
                        "dev": info.st_dev, "ino": info.st_ino,
                        "candidate_mode": expected_mode, "candidate_uid": expected_uid,
                        "candidate_gid": expected_gid})
    return entries


def publish_snapshot(snapshot_path: pathlib.Path, target_map: dict[str, pathlib.Path], *,
                     candidate_sha: str, previous_sha: str,
                     managed_dirs: dict[str, tuple[pathlib.Path, int, int, int]] | None = None) -> None:
    """Durably publish one complete snapshot before target replacement begins."""
    _sha(candidate_sha)
    _sha(previous_sha)
    managed_dirs = {} if managed_dirs is None else managed_dirs
    if candidate_sha == previous_sha:
        raise Refused("candidate and previous commit must differ")
    if not isinstance(snapshot_path, pathlib.Path):
        raise Refused("invalid rollback snapshot path")
    exists, _ = _path_chain(snapshot_path)
    if exists:
        raise Refused("rollback snapshot already exists; preserving prior evidence")
    parent = snapshot_path.parent
    parent_exists, parent_info = _path_chain(parent)
    if (not parent_exists or parent_info is None or not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != os.geteuid() or stat.S_IMODE(parent_info.st_mode) != 0o700):
        raise Refused("rollback snapshot directory must be private and owned by the updater")

    data = {
        "schema": 1,
        "candidate_sha": candidate_sha,
        "previous_sha": previous_sha,
        "phase": "prepared",
        "targets": _target_entries(target_map),
        "directories": _directory_entries(managed_dirs),
        "stages": [],
    }
    payload = {**data, "sha256": hashlib.sha256(_canonical(data)).hexdigest()}
    encoded = _canonical(payload)
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise Refused("rollback snapshot exceeds its total size bound")

    temporary = parent / f".{snapshot_path.name}.tmp-{secrets.token_hex(8)}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    created = os.fstat(fd)
    try:
        view = memoryview(encoded)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise Refused("rollback snapshot write made no progress")
            view = view[count:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            current = os.lstat(temporary)
            if ((current.st_dev, current.st_ino) == (created.st_dev, created.st_ino)
                    and stat.S_ISREG(current.st_mode) and current.st_nlink == 1):
                os.unlink(temporary)
                parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
        except OSError:
            pass
        raise
    else:
        os.close(fd)
    try:
        # link() publishes only if the final name is absent; it cannot replace a
        # prior recovery snapshot.
        os.link(temporary, snapshot_path, follow_symlinks=False)
        os.unlink(temporary)
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _load_snapshot(snapshot_path: pathlib.Path) -> dict:
    exists, info = _path_chain(snapshot_path)
    if not exists or info is None:
        raise Refused("rollback snapshot is unavailable")
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            or info.st_size > MAX_SNAPSHOT_BYTES):
        raise Refused("rollback snapshot has unsafe type, owner, mode, links, or size")
    content = _read_regular(snapshot_path)
    if content is None:
        raise Refused("rollback snapshot disappeared")
    raw, _ = content
    try:
        snapshot = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise Refused("rollback snapshot is malformed") from error
    if (not isinstance(snapshot, dict)
            or set(snapshot) != {"schema", "candidate_sha", "previous_sha", "phase",
                                "targets", "directories", "stages", "sha256"}
            or snapshot.get("schema") != 1 or type(snapshot.get("schema")) is not int):
        raise Refused("rollback snapshot schema is unsupported")
    checksum = snapshot.pop("sha256")
    if (not isinstance(checksum, str)
            or not secrets.compare_digest(checksum, hashlib.sha256(_canonical(snapshot)).hexdigest())):
        raise Refused("rollback snapshot integrity check failed")
    _sha(snapshot.get("candidate_sha"))
    _sha(snapshot.get("previous_sha"))
    if snapshot.get("phase") not in ("prepared", "verified-update", "verified-rollback"):
        raise Refused("rollback snapshot phase is unsupported")
    if (not isinstance(snapshot.get("targets"), list)
            or not isinstance(snapshot.get("directories"), list)
            or not isinstance(snapshot.get("stages"), list)):
        raise Refused("rollback snapshot inventory is malformed")
    snapshot["sha256"] = checksum
    return snapshot


def set_snapshot_phase(snapshot_path: pathlib.Path, *, candidate_sha: str, phase: str) -> None:
    """Durably record verified completion before the maintenance marker is removed."""
    _sha(candidate_sha)
    if phase not in ("verified-update", "verified-rollback"):
        raise Refused("unsupported code transaction completion phase")
    snapshot = _load_snapshot(snapshot_path)
    if snapshot["candidate_sha"] != candidate_sha:
        raise Refused("completion journal belongs to another candidate")
    if snapshot["stages"]:
        raise Refused("cannot complete a code transaction with staged files outstanding")
    if snapshot["phase"] == "verified-rollback" and phase != "verified-rollback":
        raise Refused("completed rollback phase cannot move backwards")
    snapshot.pop("sha256")
    snapshot["phase"] = phase
    snapshot["sha256"] = hashlib.sha256(_canonical(snapshot)).hexdigest()
    _atomic_write_snapshot(snapshot_path, snapshot)


def _atomic_write_snapshot(snapshot_path: pathlib.Path, snapshot: dict) -> None:
    body = {key: value for key, value in snapshot.items() if key != "sha256"}
    body["sha256"] = hashlib.sha256(_canonical(body)).hexdigest()
    payload = _canonical(body)
    if len(payload) > MAX_SNAPSHOT_BYTES:
        raise Refused("transaction journal exceeds its size bound")
    exists, info = _path_chain(snapshot_path)
    if (not exists or info is None or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise Refused("transaction journal identity is unsafe")
    temporary = snapshot_path.parent / f".{snapshot_path.name}.journal-{secrets.token_hex(8)}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(payload)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise Refused("transaction journal write made no progress")
            view = view[count:]
        os.fchown(fd, os.geteuid(), os.getegid())
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, snapshot_path)
    parent_fd = os.open(snapshot_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def stage_candidate(snapshot_path: pathlib.Path, target_map: dict[str, pathlib.Path], *,
                    logical_target: str, candidate_sha: str, index: int,
                    payload: bytes, mode: int, uid: int = 0, gid: int = 0) -> pathlib.Path:
    """Create a complete unnamed file, journal its inode, then atomically link it."""
    _sha(candidate_sha)
    if (logical_target not in target_map or type(index) is not int or index < 0
            or not isinstance(payload, bytes) or len(payload) > MAX_TARGET_BYTES
            or any(type(value) is not int for value in (mode, uid, gid))):
        raise Refused("candidate staging request is outside fixed installer policy")
    target = target_map[logical_target]
    parent = target.parent
    name = f".{target.name}.lm-update-{candidate_sha}-{index}.tmp"
    staged_path = parent / name
    if os.path.lexists(staged_path):
        raise Refused("candidate staging target already exists; preserving evidence")
    snapshot = _load_snapshot(snapshot_path)
    if snapshot["candidate_sha"] != candidate_sha or snapshot["phase"] != "prepared":
        raise Refused("candidate staging journal identity or phase is invalid")
    if any(item.get("temp") == str(staged_path) for item in snapshot["stages"]):
        raise Refused("candidate staging path is already journaled")
    if not hasattr(os, "O_TMPFILE"):
        raise Refused("filesystem does not support anonymous protected staging")
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fd = os.open(parent, os.O_TMPFILE | os.O_WRONLY | os.O_CLOEXEC, 0o600)
        except OSError as error:
            raise Refused("filesystem cannot create anonymous protected staging") from error
        try:
            view = memoryview(payload)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise Refused("candidate staging write made no progress")
                view = view[count:]
            if os.geteuid() != uid or os.getegid() != gid:
                os.fchown(fd, uid, gid)
            os.fchmod(fd, mode)
            os.fsync(fd)
            info = os.fstat(fd)
            entry = {"target": logical_target, "temp": str(staged_path),
                     "dev": info.st_dev, "ino": info.st_ino,
                     "uid": info.st_uid, "gid": info.st_gid,
                     "mode": stat.S_IMODE(info.st_mode), "size": len(payload),
                     "sha256": hashlib.sha256(payload).hexdigest()}
            snapshot["stages"].append(entry)
            _atomic_write_snapshot(snapshot_path, snapshot)
            libc = ctypes.CDLL(None, use_errno=True)
            linkat = libc.linkat
            linkat.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                               ctypes.c_char_p, ctypes.c_int)
            linkat.restype = ctypes.c_int
            # Some supported filesystems reject AT_EMPTY_PATH for O_TMPFILE.
            # Resolve the still-open anonymous inode through procfs instead;
            # AT_SYMLINK_FOLLOW links the inode, not the procfs symlink.
            proc_fd = f"/proc/self/fd/{fd}".encode()
            if linkat(-100, proc_fd, parent_fd, os.fsencode(name), 0x400) != 0:
                error_number = ctypes.get_errno()
                raise OSError(error_number, os.strerror(error_number))
            linked = os.stat(staged_path, follow_symlinks=False)
            if ((linked.st_dev, linked.st_ino) != (info.st_dev, info.st_ino)
                    or not stat.S_ISREG(linked.st_mode) or linked.st_nlink != 1):
                raise Refused("candidate staging inode changed during publication")
        finally:
            os.close(fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return staged_path


def clear_stage(snapshot_path: pathlib.Path, *, candidate_sha: str, temp_path: pathlib.Path) -> None:
    snapshot = _load_snapshot(snapshot_path)
    if snapshot["candidate_sha"] != candidate_sha:
        raise Refused("staging journal belongs to another candidate")
    matches = [item for item in snapshot["stages"] if item.get("temp") == str(temp_path)]
    if len(matches) > 1:
        raise Refused("staging journal has duplicate target records")
    snapshot["stages"] = [item for item in snapshot["stages"] if item.get("temp") != str(temp_path)]
    _atomic_write_snapshot(snapshot_path, snapshot)


def _remove_journaled_stages(snapshot_path: pathlib.Path, target_map, expected_candidate,
                             candidate_sha: str) -> None:
    snapshot = _load_snapshot(snapshot_path)
    if snapshot["candidate_sha"] != candidate_sha:
        raise Refused("staging journal belongs to another candidate")
    recorded = {entry.get("temp") for entry in snapshot["stages"] if isinstance(entry, dict)}
    for entry in list(snapshot["stages"]):
        if (not isinstance(entry, dict)
                or set(entry) != {"target", "temp", "dev", "ino", "uid", "gid", "mode", "size", "sha256"}
                or entry.get("target") not in target_map
                or any(type(entry.get(key)) is not int for key in
                       ("dev", "ino", "uid", "gid", "mode", "size"))):
            raise Refused("staging journal entry is malformed")
        target = target_map[entry["target"]]
        temp = pathlib.Path(entry["temp"])
        expected_name = f".{target.name}.lm-update-{candidate_sha}-"
        if temp.parent != target.parent or not temp.name.startswith(expected_name) or not temp.name.endswith(".tmp"):
            raise Refused("staging journal path is outside the fixed target policy")
        wanted = expected_candidate[entry["target"]]
        if (entry["size"] != len(wanted[0])
                or entry["sha256"] != hashlib.sha256(wanted[0]).hexdigest()
                or (entry["mode"], entry["uid"], entry["gid"])
                   != (wanted[1], wanted[2], wanted[3])):
            raise Refused("staging journal content differs from the candidate policy")
        exists, info = _path_chain(temp)
        if exists:
            if (info is None or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or (info.st_dev, info.st_ino) != (entry["dev"], entry["ino"])
                    or info.st_uid != entry["uid"] or info.st_gid != entry["gid"]
                    or stat.S_IMODE(info.st_mode) != entry["mode"]):
                raise Refused("journaled staging inode changed; preserving evidence")
            content = _read_regular(temp)
            if content is None or content[0] != wanted[0]:
                raise Refused("journaled staging bytes changed; preserving evidence")
            os.unlink(temp)
            parent_fd = os.open(temp.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        clear_stage(snapshot_path, candidate_sha=candidate_sha, temp_path=temp)
    for _logical, target in target_map.items():
        pattern = re.compile(r"^\." + re.escape(target.name) +
                             r"\.lm-update-" + re.escape(candidate_sha) + r"-[0-9]+\.tmp$")
        for path in target.parent.glob(f".{target.name}.lm-update-{candidate_sha}-*.tmp"):
            if pattern.fullmatch(path.name) and str(path) not in recorded:
                raise Refused("unrecorded candidate staging artifact; preserving evidence")


def recover_candidate_stages(snapshot_path: pathlib.Path, target_map,
                             expected_candidate, *, candidate_sha: str) -> None:
    """Recover only journaled, inode-verified candidate staging before retrying writes."""
    _sha(candidate_sha)
    _remove_journaled_stages(snapshot_path, target_map, expected_candidate, candidate_sha)


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _expected_candidate_map(expected_candidate: dict[str, tuple[bytes, int, int, int]],
                            target_map: dict[str, pathlib.Path]) -> dict[str, tuple[bytes, int, int, int]]:
    if not isinstance(expected_candidate, dict) or set(expected_candidate) != set(target_map):
        raise Refused("candidate target inventory does not match the protected policy")
    result = {}
    for logical, value in expected_candidate.items():
        if (not isinstance(value, tuple) or len(value) != 4 or not isinstance(value[0], bytes)
                or any(type(item) is not int for item in value[1:])
                or len(value[0]) > MAX_TARGET_BYTES):
            raise Refused("candidate target metadata is invalid")
        result[logical] = value
    return result


def _entry_data(entry: dict) -> bytes | None:
    if entry.get("state") == "absent":
        if set(entry) != {"path", "state"}:
            raise Refused("absent snapshot entry has unexpected fields")
        return None
    if entry.get("state") != "file" or set(entry) != {
            "path", "state", "uid", "gid", "mode", "size", "sha256", "data"}:
        raise Refused("snapshot target entry has unexpected schema")
    try:
        data = base64.b64decode(entry["data"], validate=True)
    except (ValueError, TypeError) as error:
        raise Refused("snapshot target bytes are malformed") from error
    if (len(data) != entry["size"] or len(data) > MAX_TARGET_BYTES
            or hashlib.sha256(data).hexdigest() != entry["sha256"]):
        raise Refused("snapshot target content digest is invalid")
    if any(type(entry.get(key)) is not int for key in ("uid", "gid", "mode", "size")):
        raise Refused("snapshot target metadata is invalid")
    return data


def _current_state(path: pathlib.Path) -> tuple[bytes, int, int, int] | None:
    observed = _read_regular(path)
    if observed is None:
        return None
    data, info = observed
    return data, stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid


def _matches_old(entry: dict, current: tuple[bytes, int, int, int] | None) -> bool:
    old_data = _entry_data(entry)
    if old_data is None:
        return current is None
    return current == (old_data, entry["mode"], entry["uid"], entry["gid"])


def _stage_file(path: pathlib.Path, data: bytes, mode: int, uid: int, gid: int,
                candidate_sha: str, index: int) -> pathlib.Path:
    exists, _ = _path_chain(path.parent, allow_missing_final=False)
    if not exists:
        raise Refused("rollback target parent is unavailable")
    temporary = path.parent / f".lm-code-rollback-{candidate_sha}-{index}"
    if os.path.lexists(temporary):
        existing = _read_regular(temporary)
        if existing is None:
            raise Refused("stale rollback staging path is unsafe")
        staged_data, staged_info = existing
        if (staged_data != data or stat.S_IMODE(staged_info.st_mode) != mode
                or staged_info.st_uid != uid or staged_info.st_gid != gid):
            incomplete_owner = staged_info.st_uid == os.geteuid()
            incomplete_group = staged_info.st_gid in (os.getegid(), gid)
            incomplete_mode = (stat.S_IMODE(staged_info.st_mode) == 0o600
                              or stat.S_IMODE(staged_info.st_mode) | mode == mode)
            if (not incomplete_owner or not incomplete_group or not incomplete_mode
                    or len(staged_data) > len(data) or not data.startswith(staged_data)):
                raise Refused("stale rollback staging file differs from the snapshot")
            os.unlink(temporary)
            parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            return _stage_file(path, data, mode, uid, gid, candidate_sha, index)
        return temporary
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise Refused("rollback staging write made no progress")
            view = view[count:]
        if os.geteuid() != uid or os.getegid() != gid:
            os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    else:
        os.close(fd)
    return temporary


def _validate_dir_entry(entry: dict, path: pathlib.Path) -> tuple[bool, os.stat_result | None]:
    exists, info = _path_chain(path)
    state = entry.get("state")
    if state == "absent":
        required = {"path", "state", "candidate_mode", "candidate_uid", "candidate_gid"}
        if set(entry) != required:
            raise Refused("absent managed directory entry has unexpected fields")
        if any(type(entry.get(key)) is not int for key in
               ("candidate_mode", "candidate_uid", "candidate_gid")):
            raise Refused("absent managed directory metadata is invalid")
        if exists:
            assert info is not None
            if (not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != entry["candidate_uid"]
                    or info.st_gid != entry["candidate_gid"]
                    or stat.S_IMODE(info.st_mode) != entry["candidate_mode"]):
                raise Refused("candidate-created directory has unexpected identity")
        return exists, info
    required = {"path", "state", "uid", "gid", "mode", "dev", "ino",
                "candidate_mode", "candidate_uid", "candidate_gid"}
    if state != "directory" or set(entry) != required:
        raise Refused("managed directory snapshot schema is invalid")
    if any(type(entry.get(key)) is not int for key in
           ("uid", "gid", "mode", "dev", "ino", "candidate_mode", "candidate_uid", "candidate_gid")):
        raise Refused("managed directory snapshot metadata is invalid")
    if (not exists or info is None or not stat.S_ISDIR(info.st_mode)
            or info.st_dev != entry["dev"] or info.st_ino != entry["ino"]
            or info.st_uid != entry["uid"] or info.st_gid != entry["gid"]
            or stat.S_IMODE(info.st_mode) != entry["mode"]):
        raise Refused("pre-existing managed directory identity changed")
    return exists, info


def restore_snapshot(snapshot_path: pathlib.Path, target_map: dict[str, pathlib.Path],
                     expected_candidate: dict[str, tuple[bytes, int, int, int]], *,
                     candidate_sha: str,
                     managed_dirs: dict[str, tuple[pathlib.Path, int, int, int]] | None = None,
                     preserve_candidate_paths: frozenset[str] = frozenset()) -> None:
    """Restore the exact snapshot; safe to retry after an interrupted restore."""
    _sha(candidate_sha)
    managed_dirs = {} if managed_dirs is None else managed_dirs
    snapshot = _load_snapshot(snapshot_path)
    if snapshot["candidate_sha"] != candidate_sha:
        raise Refused("rollback snapshot belongs to another candidate")
    candidate = _expected_candidate_map(expected_candidate, target_map)
    old_entries = snapshot["targets"]
    dir_entries = snapshot["directories"]
    if ({entry.get("path") for entry in old_entries} != set(target_map)
            or len(old_entries) != len(target_map)
            or {entry.get("path") for entry in dir_entries} != set(managed_dirs)
            or len(dir_entries) != len(managed_dirs)):
        raise Refused("rollback snapshot inventory does not match fixed installer policy")
    _remove_journaled_stages(snapshot_path, target_map, candidate, candidate_sha)

    old_by_path = {entry["path"]: entry for entry in old_entries}
    current_by_path: dict[str, tuple[bytes, int, int, int] | None] = {}
    for logical, path in sorted(target_map.items()):
        entry = old_by_path[logical]
        current = _current_state(path)
        current_by_path[logical] = current
        old_match = _matches_old(entry, current)
        wanted = candidate[logical]
        candidate_match = current == wanted
        if not old_match and not candidate_match:
            raise Refused("protected target drifted from both snapshot and candidate")
        if logical in preserve_candidate_paths and not (old_match or candidate_match):
            raise Refused("permanent guard target is neither prior nor candidate state")

    dir_by_path = {entry["path"]: entry for entry in dir_entries}
    dir_states = {}
    for logical, spec in sorted(managed_dirs.items()):
        path = spec[0]
        dir_states[logical] = _validate_dir_entry(dir_by_path[logical], path)

    # Stage every previous file before replacing any target. An interrupted
    # restore leaves the published snapshot plus an old/new target mixture.
    staged: list[tuple[pathlib.Path, pathlib.Path, bytes, int, int, int]] = []
    try:
        for index, (logical, path) in enumerate(sorted(target_map.items())):
            entry = old_by_path[logical]
            old_data = _entry_data(entry)
            if logical in preserve_candidate_paths and current_by_path[logical] == candidate[logical]:
                continue
            if old_data is None or _matches_old(entry, current_by_path[logical]):
                continue
            temporary = _stage_file(path, old_data, entry["mode"], entry["uid"],
                                    entry["gid"], candidate_sha, index)
            staged.append((temporary, path, old_data, entry["mode"], entry["uid"], entry["gid"]))
        for temporary, path, _data, _mode, _uid, _gid in staged:
            os.replace(temporary, path)
            parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)

        # Remove candidate-only files only when bytes and metadata exactly match
        # the candidate. If already absent, the operation is an idempotent retry.
        for index, (logical, path) in enumerate(sorted(target_map.items(), reverse=True)):
            entry = old_by_path[logical]
            if entry.get("state") != "absent":
                continue
            if logical in preserve_candidate_paths and current_by_path[logical] == candidate[logical]:
                continue
            current = _current_state(path)
            if current is None:
                continue
            if current != candidate[logical]:
                raise Refused("candidate-only target changed before removal")
            os.unlink(path)
            parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)

        # Remove only managed directories that were absent before the update;
        # rmdir is deliberately non-recursive and therefore preserves surprises.
        for logical, spec in sorted(managed_dirs.items(), reverse=True):
            entry = dir_by_path[logical]
            if entry.get("state") != "absent":
                continue
            path, candidate_mode, candidate_uid, candidate_gid = spec
            if any(item in preserve_candidate_paths and target_map[item].parent == path
                   for item in target_map):
                continue
            exists, info = _path_chain(path)
            if not exists:
                continue
            assert info is not None
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != candidate_uid
                    or info.st_gid != candidate_gid or stat.S_IMODE(info.st_mode) != candidate_mode):
                raise Refused("candidate-created directory changed before removal")
            os.rmdir(path)
            parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)

        # Verify the complete restored file inventory and directory identity.
        for logical, path in target_map.items():
            entry = old_by_path[logical]
            current = _current_state(path)
            if logical in preserve_candidate_paths and current == candidate[logical]:
                continue
            if not _matches_old(entry, current):
                raise Refused("rollback target verification failed")
        for logical, spec in managed_dirs.items():
            _validate_dir_entry(dir_by_path[logical], spec[0])
    finally:
        # Clean only temporary names created by this fixed rollback operation;
        # an unknown file at that name is retained as evidence.
        for temporary, _target, expected_data, expected_mode, expected_uid, expected_gid in staged:
            try:
                if os.path.lexists(temporary):
                    existing = _read_regular(temporary)
                    if (existing is not None and existing[0] == expected_data
                            and stat.S_IMODE(existing[1].st_mode) == expected_mode
                            and existing[1].st_uid == expected_uid
                            and existing[1].st_gid == expected_gid):
                        os.unlink(temporary)
                    else:
                        raise Refused("rollback staging path changed; preserving unexpected evidence")
            except OSError:
                pass
