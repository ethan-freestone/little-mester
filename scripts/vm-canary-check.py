#!/usr/bin/env python3
"""Bounded, redacting checks for one synthetic canary and exact artifacts."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import stat
import time
import zlib
from pathlib import Path
from typing import Any

CHUNK_BYTES = 1024 * 1024
MAX_ARTIFACTS = 512
MAX_FILES = 100_000
MAX_TOTAL_BYTES = 5 * 1024**3
MAX_ISO_BYTES = 64 * 1024**2
DEFAULT_TIMEOUT_SECONDS = 60
ARTIFACT_ID_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
RUN_ID_RE = re.compile(r"[a-f0-9]{32}")
PROFILE_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_LOG_NAMES = {"processes.json", "known_hosts", "proxy.port", "proxy.ready", "failure.json",
              "proxy.log", "virtiofsd.log"}
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW


class CanaryCheckError(ValueError):
    """Invalid checker input; details intentionally contain no artifact data."""


class _Blocked(Exception):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code


class _Budget:
    def __init__(self, timeout_seconds: int):
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= DEFAULT_TIMEOUT_SECONDS:
            raise CanaryCheckError("checker time limit is invalid")
        self.deadline = time.monotonic() + timeout_seconds
        self.files = 0
        self.bytes = 0

    def check(self) -> None:
        if time.monotonic() > self.deadline:
            raise _Blocked("scan_timeout")

    def add_file(self) -> None:
        self.check()
        self.files += 1
        if self.files > MAX_FILES:
            raise _Blocked("file_count_limit")

    def add_bytes(self, count: int) -> None:
        self.check()
        self.bytes += count
        if self.bytes > MAX_TOTAL_BYTES:
            raise _Blocked("byte_limit")


class _Matcher:
    def __init__(self, canary: bytes):
        if not isinstance(canary, bytes) or not 32 <= len(canary) <= 128:
            raise CanaryCheckError("synthetic canary is invalid")
        encodings = {
            canary,
            canary.hex().encode("ascii"),
            base64.b64encode(canary),
            base64.urlsafe_b64encode(canary),
        }
        self.needles = tuple(sorted(encodings, key=len, reverse=True))
        self.tail = b""
        self.found = False

    @property
    def overlap(self) -> int:
        return max(len(item) for item in self.needles) - 1

    def feed(self, value: bytes) -> None:
        if self.found:
            return
        joined = self.tail + value
        self.found = any(needle in joined for needle in self.needles)
        self.tail = joined[-self.overlap:] if self.overlap else b""


def read_canary(path_value: str | os.PathLike[str]) -> bytes:
    """Read only one owner-owned, private, canonical regular fixture."""
    path = Path(path_value)
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or path.is_symlink()
                or path.resolve(strict=True) != path or not 32 <= info.st_size <= 128):
            raise CanaryCheckError("synthetic canary fixture is unsafe")
        fd = os.open(path, _FILE_FLAGS)
    except CanaryCheckError:
        raise
    except (OSError, RuntimeError) as error:
        raise CanaryCheckError("synthetic canary fixture is unavailable") from error
    try:
        opened = os.fstat(fd)
        if _signature(opened) != _signature(info) or not stat.S_ISREG(opened.st_mode):
            raise CanaryCheckError("synthetic canary fixture changed during validation")
        value = bytearray()
        while len(value) <= 128:
            chunk = os.read(fd, min(CHUNK_BYTES, 129 - len(value)))
            if not chunk:
                break
            value.extend(chunk)
        if (not 32 <= len(value) <= 128 or len(value) != info.st_size
                or _signature(os.fstat(fd)) != _signature(opened)):
            raise CanaryCheckError("synthetic canary fixture changed during validation")
        return bytes(value)
    finally:
        os.close(fd)


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _safe_directory(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o022 or path.is_symlink()
                or path.resolve(strict=True) != path):
            raise _Blocked("unsafe_directory")
        return info
    except _Blocked:
        raise
    except (OSError, RuntimeError) as error:
        raise _Blocked("missing_artifact") from error


def _safe_regular(path: Path, *, max_size: int | None = None) -> tuple[int, os.stat_result]:
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o022 or path.is_symlink()
                or path.resolve(strict=True) != path):
            raise _Blocked("unsafe_file")
        if max_size is not None and info.st_size > max_size:
            raise _Blocked("artifact_size_limit")
        fd = os.open(path, _FILE_FLAGS)
        opened = os.fstat(fd)
        if _signature(opened) != _signature(info) or not stat.S_ISREG(opened.st_mode):
            os.close(fd)
            raise _Blocked("artifact_changed")
        return fd, opened
    except _Blocked:
        raise
    except FileNotFoundError as error:
        raise _Blocked("missing_artifact") from error
    except (OSError, RuntimeError) as error:
        raise _Blocked("unsafe_file") from error


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_manifest(manifest: Any) -> tuple[list[dict[str, Any]], list[str]]:
    if (not isinstance(manifest, dict) or set(manifest) != {"schema_version", "required_ids", "artifacts"}
            or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1
            or not isinstance(manifest.get("required_ids"), list)
            or not isinstance(manifest.get("artifacts"), list)
            or not manifest["required_ids"]
            or len(manifest["required_ids"]) > MAX_ARTIFACTS
            or len(manifest["artifacts"]) > MAX_ARTIFACTS):
        raise CanaryCheckError("artifact manifest is invalid")
    required = manifest["required_ids"]
    if (any(not isinstance(item, str) or ARTIFACT_ID_RE.fullmatch(item) is None for item in required)
            or len(set(required)) != len(required)):
        raise CanaryCheckError("required artifact list is invalid")
    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for artifact in manifest["artifacts"]:
        if (not isinstance(artifact, dict) or set(artifact) - {"id", "path", "kind", "reason_code"}
                or not {"id", "path", "kind"} <= set(artifact)
                or not isinstance(artifact["id"], str)
                or ARTIFACT_ID_RE.fullmatch(artifact["id"]) is None
                or artifact["id"] in seen or not isinstance(artifact["path"], str)
                or not isinstance(artifact["kind"], str)):
            raise CanaryCheckError("artifact manifest entry is invalid")
        seen.add(artifact["id"])
        artifacts.append(artifact)
    return artifacts, required


def _scan_file(fd: int, before: os.stat_result, matcher: _Matcher,
               budget: _Budget, digest: Any) -> None:
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.getuid():
        raise _Blocked("unsafe_file")
    if before.st_size < 0 or before.st_size > MAX_TOTAL_BYTES:
        raise _Blocked("artifact_size_limit")
    budget.add_file()
    read = 0
    while True:
        budget.check()
        chunk = os.read(fd, CHUNK_BYTES)
        if not chunk:
            break
        read += len(chunk)
        budget.add_bytes(len(chunk))
        digest.update(chunk)
        matcher.feed(chunk)
    if read != before.st_size or _signature(before) != _signature(os.fstat(fd)):
        raise _Blocked("artifact_changed")


def _scan_single_file(path: Path, kind: str, matcher: _Matcher,
                      budget: _Budget) -> dict[str, Any]:
    if kind == "iso9660":
        fd, info = _safe_regular(path, max_size=MAX_ISO_BYTES)
    else:
        fd, info = _safe_regular(path)
    try:
        if kind == "iso9660":
            descriptor = os.pread(fd, 7, 16 * 2048)
            if len(descriptor) != 7 or descriptor[0] != 1 or descriptor[1:6] != b"CD001" or descriptor[6] != 1:
                raise _Blocked("unsupported_iso_format")
        digest = hashlib.sha256()
        _scan_file(fd, info, matcher, budget, digest)
        return {"status": "FAIL" if matcher.found else "PASS",
                "reason_code": "canary_present" if matcher.found else None,
                "artifact_sha256": digest.hexdigest(), "file_count": 1}
    finally:
        os.close(fd)


def _scan_tree(path: Path, matcher: _Matcher, budget: _Budget,
               *, logs_only: bool = False) -> dict[str, Any]:
    root_info = _safe_directory(path)
    root_fd = os.open(path, _DIR_FLAGS)
    if _signature(os.fstat(root_fd)) != _signature(root_info):
        os.close(root_fd)
        raise _Blocked("artifact_changed")
    root = path
    tree_digest = hashlib.sha256()
    count = 0
    log_files = 0
    stack: list[tuple[int, Path, os.stat_result]] = [(root_fd, Path("."), root_info)]
    try:
        while stack:
            budget.check()
            directory_fd, relative, directory_info = stack.pop()
            try:
                if _signature(os.fstat(directory_fd)) != _signature(directory_info):
                    raise _Blocked("artifact_changed")
                try:
                    names = sorted(os.listdir(directory_fd))
                except OSError as error:
                    raise _Blocked("unreadable_artifact") from error
                for name in names:
                    budget.check()
                    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
                        raise _Blocked("unsafe_path")
                    relative_name = Path(name) if relative == Path(".") else relative / name
                    try:
                        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    except OSError as error:
                        raise _Blocked("artifact_changed") from error
                    if stat.S_ISDIR(info.st_mode):
                        if logs_only and (relative != Path(".")
                                          or PROFILE_RUN_ID_RE.fullmatch(name) is None):
                            raise _Blocked("unmanifested_log_directory")
                        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
                            raise _Blocked("unsafe_directory")
                        child_fd = os.open(name, _DIR_FLAGS, dir_fd=directory_fd)
                        opened = os.fstat(child_fd)
                        if _signature(opened) != _signature(info):
                            os.close(child_fd)
                            raise _Blocked("artifact_changed")
                        stack.append((child_fd, relative_name, info))
                    elif stat.S_ISREG(info.st_mode):
                        if logs_only and not (
                            name in _LOG_NAMES or name.endswith(".log")
                            or re.fullmatch(r"qemu_[1-3]\.log", name)
                        ):
                            raise _Blocked("unmanifested_log_file")
                        if info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o022:
                            raise _Blocked("unsafe_file")
                        fd = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
                        try:
                            opened = os.fstat(fd)
                            if _signature(opened) != _signature(info):
                                raise _Blocked("artifact_changed")
                            file_digest = hashlib.sha256()
                            _scan_file(fd, opened, matcher, budget, file_digest)
                            tree_digest.update(os.fsencode(relative_name.as_posix()))
                            tree_digest.update(b"\0")
                            tree_digest.update(file_digest.digest())
                            count += 1
                            if name.endswith(".log"):
                                log_files += 1
                        finally:
                            os.close(fd)
                        if count > MAX_FILES:
                            raise _Blocked("file_count_limit")
                        continue
                    elif stat.S_ISLNK(info.st_mode):
                        if logs_only:
                            raise _Blocked("unsafe_symlink")
                        try:
                            target = os.readlink(name, dir_fd=directory_fd)
                            resolved = (root / relative_name.parent / target).resolve(strict=True)
                        except (OSError, RuntimeError) as error:
                            raise _Blocked("unsafe_symlink") from error
                        if (Path(target).is_absolute() or not _inside(resolved, root)
                                or resolved == root or not (resolved.is_file() or resolved.is_dir())):
                            raise _Blocked("unsafe_symlink")
                        encoded_target = os.fsencode(target)
                        budget.add_bytes(len(encoded_target))
                        matcher.feed(encoded_target)
                        tree_digest.update(os.fsencode(relative_name.as_posix()))
                        tree_digest.update(b"\0")
                        tree_digest.update(encoded_target)
                    elif logs_only and stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid():
                        pass
                    else:
                        raise _Blocked("unsupported_file_type")
                    count += 1
                    if count > MAX_FILES:
                        raise _Blocked("file_count_limit")
            finally:
                os.close(directory_fd)
        if logs_only and log_files == 0:
            raise _Blocked("required_logs_missing")
    except BaseException:
        for fd, _relative, _info in stack:
            try:
                os.close(fd)
            except OSError:
                pass
        raise
    return {"status": "FAIL" if matcher.found else "PASS",
            "reason_code": "canary_present" if matcher.found else None,
            "artifact_sha256": tree_digest.hexdigest(), "file_count": count,
            "log_file_count": log_files if logs_only else None}


def _loose_object(path: Path, matcher: _Matcher, budget: _Budget,
                  digest: Any) -> None:
    fd, info = _safe_regular(path)
    decoder = zlib.decompressobj()
    header = bytearray()
    header_done = False
    declared_size: int | None = None
    content_size = 0
    compressed_size = 0

    def consume(output: bytes) -> None:
        nonlocal header_done, declared_size, content_size
        if not output:
            return
        budget.add_bytes(len(output))
        if not header_done:
            marker = output.find(b"\0")
            if marker < 0:
                header.extend(output)
                if len(header) > 128:
                    raise _Blocked("invalid_git_object")
                return
            header.extend(output[:marker])
            match = re.fullmatch(rb"(?:blob|tree|commit|tag) (0|[1-9][0-9]{0,15})", bytes(header))
            if match is None:
                raise _Blocked("invalid_git_object")
            declared_size = int(match.group(1))
            header_done = True
            body = output[marker + 1:]
        else:
            body = output
        content_size += len(body)
        if declared_size is not None and content_size > declared_size:
            raise _Blocked("invalid_git_object")
        matcher.feed(body)

    try:
        if len(path.name) != 38 or re.fullmatch(r"[a-f0-9]{38}", path.name) is None:
            raise _Blocked("unsupported_git_object")
        budget.add_file()
        while True:
            budget.check()
            chunk = os.read(fd, CHUNK_BYTES)
            if not chunk:
                break
            compressed_size += len(chunk)
            budget.add_bytes(len(chunk))
            digest.update(chunk)
            pending = chunk
            while pending:
                before_len = len(pending)
                try:
                    output = decoder.decompress(pending, CHUNK_BYTES)
                except zlib.error as error:
                    raise _Blocked("invalid_git_object") from error
                pending = decoder.unconsumed_tail
                consume(output)
                if pending and len(pending) == before_len and not output:
                    raise _Blocked("invalid_git_object")
        while True:
            budget.check()
            try:
                output = decoder.decompress(b"", CHUNK_BYTES)
            except zlib.error as error:
                raise _Blocked("invalid_git_object") from error
            if not output:
                break
            consume(output)
        if (not decoder.eof or decoder.unused_data or not header_done or declared_size is None
                or content_size != declared_size or compressed_size != info.st_size
                or _signature(os.fstat(fd)) != _signature(info)):
            raise _Blocked("invalid_git_object")
    finally:
        os.close(fd)


def _scan_git_loose(path: Path, matcher: _Matcher, budget: _Budget) -> dict[str, Any]:
    _safe_directory(path)
    objects = path / "objects"
    _safe_directory(objects)
    tree_digest = hashlib.sha256()
    count = 0
    try:
        top_names = set(os.listdir(objects))
        if not top_names <= {"info", "pack"} | {item for item in top_names if re.fullmatch(r"[a-f0-9]{2}", item)}:
            raise _Blocked("unsupported_git_object_store")
        for special in ("info", "pack"):
            special_dir = objects / special
            if not special_dir.exists() and not special_dir.is_symlink():
                continue
            _safe_directory(special_dir)
            if os.listdir(special_dir):
                raise _Blocked("packed_or_alternate_git_objects_unsupported")
        for prefix in sorted(item for item in top_names if re.fullmatch(r"[a-f0-9]{2}", item)):
            directory = objects / prefix
            _safe_directory(directory)
            for suffix in sorted(os.listdir(directory)):
                if re.fullmatch(r"[a-f0-9]{38}", suffix) is None:
                    raise _Blocked("unsupported_git_object")
                object_path = directory / suffix
                digest = hashlib.sha256()
                _loose_object(object_path, matcher, budget, digest)
                tree_digest.update(prefix.encode("ascii") + suffix.encode("ascii") + digest.digest())
                count += 1
                if count > MAX_FILES:
                    raise _Blocked("file_count_limit")
    except _Blocked:
        raise
    except (OSError, RuntimeError) as error:
        raise _Blocked("unreadable_git_objects") from error
    if count == 0:
        raise _Blocked("git_objects_missing")
    return {"status": "FAIL" if matcher.found else "PASS",
            "reason_code": "canary_present" if matcher.found else None,
            "artifact_sha256": tree_digest.hexdigest(), "file_count": count}


def scan_manifest(canary: bytes, manifest: Any,
                  allowed_roots: list[str | os.PathLike[str]], *,
                  timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Scan only explicit canonical artifacts under the supplied private roots."""
    artifacts, required_ids = _validate_manifest(manifest)
    roots: list[Path] = []
    root_reason: str | None = None
    for item in allowed_roots:
        root = Path(item)
        try:
            info = _safe_directory(root)
            if info.st_uid != os.getuid():
                root_reason = "unsafe_directory"
            else:
                roots.append(root)
        except _Blocked as error:
            root_reason = error.reason_code
    if not roots:
        if not allowed_roots:
            raise CanaryCheckError("allowed artifact roots are required")
        roots = [Path(item) for item in allowed_roots]
    else:
        root_reason = None
    budget = _Budget(timeout_seconds)
    matcher = _Matcher(canary)
    by_id = {item["id"]: item for item in artifacts}
    if len(by_id) != len(artifacts):
        raise CanaryCheckError("artifact manifest contains duplicate identifiers")
    results: list[dict[str, Any]] = []
    any_match = False

    for artifact_id in required_ids:
        matcher.found = False
        matcher.tail = b""
        artifact = by_id.get(artifact_id)
        if artifact is None:
            results.append({"id": artifact_id, "status": "BLOCKED", "reason_code": "manifest_incomplete"})
            continue
        result: dict[str, Any] = {"id": artifact_id, "status": "BLOCKED", "reason_code": "unsafe_path"}
        try:
            if root_reason is not None:
                raise _Blocked(root_reason)
            path = Path(artifact["path"])
            if not path.is_absolute() or ".." in path.parts:
                raise _Blocked("unsafe_path")
            try:
                parent = path.parent.resolve(strict=True)
                canonical = path.resolve(strict=True)
            except FileNotFoundError as error:
                raise _Blocked("missing_artifact") from error
            except (OSError, RuntimeError) as error:
                raise _Blocked("unsafe_path") from error
            if path != canonical or parent != path.parent or not any(_inside(path, root) for root in roots):
                raise _Blocked("unsafe_path")
            if path == Path(artifact["path"]).resolve(strict=True) and path.name == "":
                raise _Blocked("unsafe_path")
            kind = artifact["kind"]
            if kind in {"file", "iso9660"}:
                checked = _scan_single_file(path, kind, matcher, budget)
            elif kind == "tree":
                checked = _scan_tree(path, matcher, budget)
            elif kind == "logs":
                checked = _scan_tree(path, matcher, budget, logs_only=True)
            elif kind == "git-loose":
                checked = _scan_git_loose(path, matcher, budget)
            elif kind == "opaque-qcow2":
                fd, info = _safe_regular(path)
                if info.st_size == 0:
                    os.close(fd)
                    raise _Blocked("empty_image")
                digest = hashlib.sha256()
                try:
                    _scan_file(fd, info, matcher, budget, digest)
                finally:
                    os.close(fd)
                checked = {
                    "status": "FAIL" if matcher.found else "BLOCKED",
                    "reason_code": "canary_present" if matcher.found else "qcow2_content_not_inspected",
                    "artifact_sha256": digest.hexdigest(), "file_count": 1,
                }
            elif kind == "unsupported":
                reason = artifact.get("reason_code")
                if not isinstance(reason, str) or re.fullmatch(r"[a-z][a-z0-9_]{1,63}", reason) is None:
                    raise CanaryCheckError("unsupported artifact reason is invalid")
                checked = {"status": "BLOCKED", "reason_code": reason}
            else:
                raise CanaryCheckError("artifact format is unsupported")
            result.update(checked)
        except _Blocked as error:
            result["status"] = "BLOCKED"
            result["reason_code"] = error.reason_code
        except CanaryCheckError:
            raise
        except (OSError, RuntimeError, ValueError, zlib.error):
            result["status"] = "BLOCKED"
            result["reason_code"] = "inspection_failed"
        results.append(result)
        any_match = any_match or result.get("status") == "FAIL"

    statuses = [item["status"] for item in results]
    overall = "FAIL" if any_match else "BLOCKED" if "BLOCKED" in statuses else "PASS"
    return {
        "schema_version": 1,
        "result": overall,
        "checks": results,
        "limits": {"files_scanned": budget.files, "bytes_scanned": budget.bytes,
                   "timeout_seconds": timeout_seconds},
    }
