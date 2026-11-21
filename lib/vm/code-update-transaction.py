#!/usr/bin/env python3
"""Bounded snapshot/rollback primitives for the fixed VM host-code update.

Callers supply only the installer's fixed target and managed-directory maps.
This is deliberately not a general backup or migration facility.
"""

from __future__ import annotations

import base64
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
        "targets": _target_entries(target_map),
        "directories": _directory_entries(managed_dirs),
    }
    payload = {**data, "sha256": hashlib.sha256(_canonical(data)).hexdigest()}
    encoded = _canonical(payload)
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise Refused("rollback snapshot exceeds its total size bound")

    temporary = parent / f".{snapshot_path.name}.tmp-{secrets.token_hex(8)}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
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
            os.unlink(temporary)
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
            or set(snapshot) != {"schema", "candidate_sha", "previous_sha", "targets", "directories", "sha256"}
            or snapshot.get("schema") != 1 or type(snapshot.get("schema")) is not int):
        raise Refused("rollback snapshot schema is unsupported")
    checksum = snapshot.pop("sha256")
    if (not isinstance(checksum, str)
            or not secrets.compare_digest(checksum, hashlib.sha256(_canonical(snapshot)).hexdigest())):
        raise Refused("rollback snapshot integrity check failed")
    _sha(snapshot.get("candidate_sha"))
    _sha(snapshot.get("previous_sha"))
    if not isinstance(snapshot.get("targets"), list) or not isinstance(snapshot.get("directories"), list):
        raise Refused("rollback snapshot inventory is malformed")
    snapshot["sha256"] = checksum
    return snapshot


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
            raise Refused("stale rollback staging file differs from the snapshot")
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


def _remove_candidate_staging_files(target_map, expected_candidate) -> None:
    """Remove only exact, bounded temp files created by the interrupted updater."""
    for logical, target in sorted(target_map.items()):
        pattern = re.compile(r"^\." + re.escape(target.name) + r"\.[0-9]+\.[0-9a-f]{16}\.tmp$")
        for path in target.parent.glob(f".{target.name}.*.tmp"):
            if not pattern.fullmatch(path.name):
                continue
            current = _current_state(path)
            if current is None:
                continue
            if current != expected_candidate[logical]:
                raise Refused("interrupted candidate staging file has unexpected content or metadata")
            os.unlink(path)
            parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)


def restore_snapshot(snapshot_path: pathlib.Path, target_map: dict[str, pathlib.Path],
                     expected_candidate: dict[str, tuple[bytes, int, int, int]], *,
                     candidate_sha: str,
                     managed_dirs: dict[str, tuple[pathlib.Path, int, int, int]] | None = None) -> None:
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

        _remove_candidate_staging_files(target_map, candidate)

        # Remove candidate-only files only when bytes and metadata exactly match
        # the candidate. If already absent, the operation is an idempotent retry.
        for index, (logical, path) in enumerate(sorted(target_map.items(), reverse=True)):
            entry = old_by_path[logical]
            if entry.get("state") != "absent":
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
            if not _matches_old(old_by_path[logical], _current_state(path)):
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
