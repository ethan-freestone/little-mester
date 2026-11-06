#!/usr/bin/env python3
"""Build an immutable, filtered source snapshot for one private VM run."""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import fnmatch
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_MAX_SNAPSHOT_BYTES = 2 * 1024**3
MAX_CONFIGURED_SNAPSHOT_BYTES = 16 * 1024**3
STAGING_RESERVE_BYTES = 128 * 1024**2
MAX_ENTRIES = 100_000
BUNDLE_NAME = "candidate-snapshot"
WORKSPACE_NAME = "workspace"
MANIFEST_NAME = "source-manifest.json"
LOCK_NAME = ".snapshot.lock"
STAGING_PREFIX = ".candidate-snapshot-stage-"
RENAME_NOREPLACE = 1

EXCLUDED_NAMES = {
    ".aws", ".bench-models", ".cache", ".config", ".git", ".gnupg",
    ".kube", ".local", ".mester", ".pytest_cache", ".ruff_cache",
    ".secrets", ".ssh", ".tox", ".venv", ".vscode-server", "__pycache__",
    "auth", "cache", "caches", "credentials", "node_modules", "secrets",
    "weights", "checkpoints", "models", "vendor-cache",
}
EXCLUDED_FILES = {
    ".env", ".npmrc", ".pypirc", ".netrc", "auth.json", "credentials.json",
    "opencode.db", "secrets.env", "token.json", "known_hosts", "config.json",
}
SAFE_ENV_EXAMPLES = {".env.example", ".env.sample", ".env.template"}
EXCLUDED_GLOBS = (
    ".env.*", "*.db", "*.db-*", "*.sqlite", "*.sqlite-*", "*.sqlite3",
    "*.sqlite3-*", "*.key", "*.pem", "*.p12", "*.pfx", "*.secret",
    "*.secrets", "*.token", "*.credential", "*.credentials", "id_rsa*",
    "id_ed25519*", "id_ecdsa*", "id_dsa*", "*.gguf", "*.safetensors",
    "*.ckpt", "*.pth", "*.pt", "*.onnx", "*.model", "*.weights",
    ".*cache*", "*.pyc", "*.pyo", "*.swp", "*.swo", "*.bak~",
)


class SnapshotError(ValueError):
    """Unsafe, changed, or incomplete source snapshot input."""


def require(condition: bool) -> None:
    if not condition:
        raise SnapshotError("source snapshot policy check failed")


def _safe_component(value: str) -> bool:
    return bool(value) and not any(ord(char) < 32 or 127 <= ord(char) <= 159
                                   or 0xD800 <= ord(char) <= 0xDFFF for char in value)


def _is_excluded_name(name: str) -> bool:
    lowered = name.casefold()
    if lowered in SAFE_ENV_EXAMPLES:
        return False
    if lowered in EXCLUDED_NAMES or lowered in EXCLUDED_FILES:
        return True
    return any(fnmatch.fnmatchcase(lowered, pattern) for pattern in EXCLUDED_GLOBS)


def _excluded_relative_path(relative: Path) -> bool:
    return any(_is_excluded_name(part) for part in relative.parts)


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _stat_signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
        info.st_mtime_ns, info.st_ctime_ns,
    )


def _open_directory(root_fd: int, relative: Path) -> int:
    current = os.dup(root_fd)
    try:
        for component in relative.parts:
            following = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=current,
            )
            os.close(current)
            current = following
        return current
    except BaseException:
        os.close(current)
        raise


def _open_regular(root_fd: int, relative: Path) -> tuple[int, os.stat_result]:
    parent_fd = _open_directory(root_fd, relative.parent)
    try:
        fd = os.open(
            relative.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
    finally:
        os.close(parent_fd)
    return fd, os.fstat(fd)


def _read_regular(root_fd: int, relative: Path, before: os.stat_result) -> tuple[str, bytes | None]:
    fd, opened = _open_regular(root_fd, relative)
    try:
        require(stat.S_ISREG(opened.st_mode) and _stat_signature(opened) == _stat_signature(before))
        require(opened.st_nlink == 1 and opened.st_size <= MAX_CONFIGURED_SNAPSHOT_BYTES)
        digest = hashlib.sha256()
        chunks = [] if opened.st_size <= 1024 * 1024 else None
        remaining = opened.st_size
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            require(bool(chunk))
            digest.update(chunk)
            if chunks is not None:
                chunks.append(chunk)
            remaining -= len(chunk)
        require(not os.read(fd, 1))
        after = os.fstat(fd)
        require(_stat_signature(opened) == _stat_signature(after))
        return digest.hexdigest(), b"".join(chunks) if chunks is not None else None
    finally:
        os.close(fd)


def _canonical_head(source: Path) -> str:
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME", "/"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }
    executable = shutil.which("git", path=environment["PATH"])
    require(executable is not None)
    probe = subprocess.run(
        [executable, "-C", str(source), "rev-parse", "--show-toplevel"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, env=environment, check=False,
    )
    require(probe.returncode == 0)
    try:
        git_root = Path(probe.stdout.strip()).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SnapshotError("source snapshot policy check failed") from exc
    require(git_root.is_dir())
    head = subprocess.run(
        [executable, "-C", str(source), "rev-parse", "--verify", "HEAD^{commit}"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, env=environment, check=False,
    )
    require(head.returncode == 0 and len(head.stdout.strip()) in (40, 64)
            and all(char in "0123456789abcdef" for char in head.stdout.strip()))
    return head.stdout.strip()


def _canonical_manifest(root_mode: int, entries: list[dict[str, object]]) -> tuple[str, bytes]:
    payload = json.dumps(
        {"root_mode": root_mode, "entries": entries},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest(), payload


def inventory(source: Path, max_bytes: int) -> tuple[dict[str, object], dict[str, tuple[object, ...]]]:
    """Inventory current content, with a private identity fingerprint for race detection."""
    root_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    entries: list[dict[str, object]] = []
    fingerprints: dict[str, tuple[object, ...]] = {}
    total_bytes = 0
    try:
        root_info = os.fstat(root_fd)
        require(stat.S_ISDIR(root_info.st_mode) and root_info.st_uid == os.getuid())
        root_mode = stat.S_IMODE(root_info.st_mode)
        require(root_mode & ~0o777 == 0 and root_mode & 0o500 == 0o500)
        fingerprints["."] = _stat_signature(root_info)
        pending = [Path(".")]
        while pending:
            relative_directory = pending.pop()
            directory_fd = _open_directory(root_fd, relative_directory)
            try:
                directory_info = os.fstat(directory_fd)
                directory_key = "." if str(relative_directory) == "." else relative_directory.as_posix()
                expected_directory = fingerprints[directory_key]
                require(_stat_signature(directory_info) == expected_directory)
                names = sorted(os.listdir(directory_fd))
                for name in names:
                    require(_safe_component(name))
                    if _is_excluded_name(name):
                        continue
                    relative = Path(name) if str(relative_directory) == "." else relative_directory / name
                    relative_text = relative.as_posix()
                    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    mode = stat.S_IMODE(info.st_mode)
                    require(mode & ~0o777 == 0)
                    fingerprints[relative_text] = _stat_signature(info)
                    if stat.S_ISDIR(info.st_mode):
                        require(mode & 0o500 == 0o500)
                        entries.append({"path": relative_text, "type": "directory", "mode": mode})
                        pending.append(relative)
                    elif stat.S_ISREG(info.st_mode):
                        require(info.st_nlink == 1 and mode & 0o400)
                        total_bytes += info.st_size
                        require(total_bytes <= max_bytes and len(entries) < MAX_ENTRIES)
                        digest, _ = _read_regular(root_fd, relative, info)
                        entries.append({
                            "path": relative_text, "type": "file", "mode": mode,
                            "size": info.st_size, "sha256": digest,
                        })
                    elif stat.S_ISLNK(info.st_mode):
                        raw_target = os.readlink(name, dir_fd=directory_fd)
                        require(_safe_component(raw_target) and not Path(raw_target).is_absolute())
                        candidate = (source / relative.parent / raw_target)
                        try:
                            resolved = candidate.resolve(strict=True)
                        except (OSError, RuntimeError) as exc:
                            raise SnapshotError("source snapshot policy check failed") from exc
                        require(_inside(resolved, source))
                        target_relative = resolved.relative_to(source)
                        require(target_relative.parts and not _excluded_relative_path(target_relative))
                        target_info = resolved.lstat()
                        require(stat.S_ISDIR(target_info.st_mode) or stat.S_ISREG(target_info.st_mode))
                        target = os.path.relpath(target_relative, relative.parent)
                        require(_safe_component(target))
                        entries.append({
                            "path": relative_text, "type": "symlink", "mode": mode,
                            "target": target,
                        })
                        fingerprints[relative_text] = (*_stat_signature(info), raw_target)
                    else:
                        raise SnapshotError("source snapshot policy check failed")
                    require(len(entries) <= MAX_ENTRIES)
                after_directory = os.fstat(directory_fd)
                require(_stat_signature(directory_info) == _stat_signature(after_directory))
            finally:
                os.close(directory_fd)
        entries.sort(key=lambda item: str(item["path"]))
        tree_sha256, _ = _canonical_manifest(root_mode, entries)
        manifest = {
            "schema_version": 1,
            "source_head": _canonical_head(source),
            "root_mode": root_mode,
            "tree_sha256": tree_sha256,
            "total_bytes": total_bytes,
            "entry_count": len(entries),
            "entries": entries,
        }
        return manifest, fingerprints
    finally:
        os.close(root_fd)


def _same_manifest(left: dict[str, object], right: dict[str, object]) -> bool:
    return left == right


def _check_private_directory(path: Path, *, exact_mode: int = 0o700) -> None:
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
            and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == exact_mode
            and path.resolve(strict=True) == path)


def _check_private_file(path: Path, *, maximum: int) -> None:
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_size <= maximum)


def _open_lock(run_root: Path) -> int:
    path = run_root / LOCK_NAME
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    info = os.fstat(fd)
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _rename_noreplace(source: Path, target: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise SnapshotError("source snapshot publication is unsupported")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100, os.fsencode(source), -100, os.fsencode(target), RENAME_NOREPLACE,
    )
    if result != 0:
        error = ctypes.get_errno()
        if error in (errno.EEXIST, errno.ENOTEMPTY):
            raise SnapshotError("source snapshot already exists")
        raise OSError(error, os.strerror(error))


def copy_regular_file(source_path: Path, destination: Path, expected: dict[str, object]) -> None:
    """Copy one stable regular file; staging files remain private until final chmod."""
    relative = Path(str(expected["path"]))
    root = Path(str(expected["_source_root"]))
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        # Reopen through the root with no-follow directory traversal and compare
        # the descriptor identity with the inventory captured before copying.
        fd, opened = _open_regular(root_fd, relative)
        try:
            require(stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1)
            require(_stat_signature(opened) == tuple(expected["_signature"]))
            target_fd = os.open(
                destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
            )
            digest = hashlib.sha256()
            remaining = opened.st_size
            try:
                while remaining:
                    chunk = os.read(fd, min(1024 * 1024, remaining))
                    require(bool(chunk))
                    digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        count = os.write(target_fd, view)
                        require(count > 0)
                        view = view[count:]
                    remaining -= len(chunk)
                require(not os.read(fd, 1))
                require(digest.hexdigest() == expected["sha256"])
                require(_stat_signature(opened) == _stat_signature(os.fstat(fd)))
                os.fsync(target_fd)
            finally:
                os.close(target_fd)
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)


def copy_snapshot(source: Path, workspace: Path, manifest: dict[str, object],
                  fingerprints: dict[str, tuple[object, ...]]) -> None:
    workspace.mkdir(mode=0o700)
    directories = [entry for entry in manifest["entries"] if entry["type"] == "directory"]
    files = [entry for entry in manifest["entries"] if entry["type"] == "file"]
    symlinks = [entry for entry in manifest["entries"] if entry["type"] == "symlink"]
    for entry in sorted(directories, key=lambda item: (str(item["path"]).count("/"), str(item["path"]))):
        (workspace / entry["path"]).mkdir(mode=0o700)
    for original in files:
        entry = dict(original)
        entry["_source_root"] = str(source)
        entry["_signature"] = fingerprints[entry["path"]]
        copy_regular_file(source / entry["path"], workspace / entry["path"], entry)
    for entry in symlinks:
        destination = workspace / entry["path"]
        destination.symlink_to(entry["target"])
    _write_synthetic_git(workspace)
    for entry in sorted(directories, key=lambda item: (-str(item["path"]).count("/"), str(item["path"]))):
        os.chmod(workspace / entry["path"], int(entry["mode"]))
    for entry in files:
        os.chmod(workspace / entry["path"], int(entry["mode"]))
    os.chmod(workspace, int(manifest["root_mode"]))


def _git_environment() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME", "/"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Little Mester Snapshot",
        "GIT_AUTHOR_EMAIL": "snapshot@localhost.invalid",
        "GIT_COMMITTER_NAME": "Little Mester Snapshot",
        "GIT_COMMITTER_EMAIL": "snapshot@localhost.invalid",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000",
    }


def _git_run(workspace: Path, *arguments: str, input_data: bytes | None = None,
             check: bool = True) -> subprocess.CompletedProcess:
    executable = shutil.which("git", path=os.environ.get("PATH", os.defpath))
    require(executable is not None)
    result = subprocess.run(
        [executable, "-C", str(workspace), *arguments], input=input_data,
        stdin=subprocess.DEVNULL if input_data is None else None,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=_git_environment(), check=False,
    )
    if check:
        require(result.returncode == 0)
    return result


def _hash_blob(workspace: Path, path: Path) -> str:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode))
        executable = shutil.which("git", path=os.environ.get("PATH", os.defpath))
        require(executable is not None)
        result = subprocess.run(
            [executable, "-C", str(workspace), "hash-object", "-w", "--stdin"],
            stdin=fd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=_git_environment(), check=False,
        )
        require(result.returncode == 0)
        oid = result.stdout.decode("ascii").strip()
        require(len(oid) in (40, 64) and all(char in "0123456789abcdef" for char in oid))
        return oid
    finally:
        os.close(fd)


def _tree_object(workspace: Path, directory: Path) -> str:
    records: list[tuple[bytes, bool, bytes]] = []
    for child in directory.iterdir():
        if directory == workspace and child.name == ".git":
            continue
        name = os.fsencode(child.name)
        require(b"/" not in name and b"\0" not in name)
        info = child.lstat()
        if stat.S_ISDIR(info.st_mode):
            oid = _tree_object(workspace, child)
            record = b"040000 tree " + oid.encode("ascii") + b"\t" + name + b"\0"
            records.append((name, True, record))
        elif stat.S_ISREG(info.st_mode):
            mode = b"100755" if info.st_mode & 0o111 else b"100644"
            oid = _hash_blob(workspace, child)
            record = mode + b" blob " + oid.encode("ascii") + b"\t" + name + b"\0"
            records.append((name, False, record))
        elif stat.S_ISLNK(info.st_mode):
            target = os.readlink(child)
            require(not os.path.isabs(target))
            payload = os.fsencode(target)
            executable = shutil.which("git", path=os.environ.get("PATH", os.defpath))
            require(executable is not None)
            blob = subprocess.run(
                [executable, "-C", str(workspace), "hash-object", "-w", "--stdin"],
                input=payload, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=_git_environment(), check=False,
            )
            require(blob.returncode == 0)
            oid = blob.stdout.decode("ascii").strip()
            record = b"120000 blob " + oid.encode("ascii") + b"\t" + name + b"\0"
            records.append((name, False, record))
        else:
            raise SnapshotError("source snapshot policy check failed")
    records.sort(key=lambda item: item[0] + (b"/" if item[1] else b""))
    data = b"".join(item[2] for item in records)
    result = _git_run(workspace, "mktree", "-z", input_data=data)
    oid = result.stdout.decode("ascii").strip()
    require(len(oid) in (40, 64) and all(char in "0123456789abcdef" for char in oid))
    return oid


def _write_synthetic_git(workspace: Path) -> tuple[str, str]:
    executable = shutil.which("git", path=os.environ.get("PATH", os.defpath))
    require(executable is not None)
    initialized = subprocess.run(
        [executable, "-C", str(workspace), "init", "--quiet", "--initial-branch=snapshot",
         "--template=/dev/null"], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=_git_environment(), check=False,
    )
    require(initialized.returncode == 0)
    tree = _tree_object(workspace, workspace)
    commit_result = _git_run(workspace, "commit-tree", tree, "-m", "Filtered source snapshot")
    commit = commit_result.stdout.decode("ascii").strip()
    require(len(commit) in (40, 64) and all(char in "0123456789abcdef" for char in commit))
    _git_run(workspace, "update-ref", "refs/heads/snapshot", commit)
    _git_run(workspace, "symbolic-ref", "HEAD", "refs/heads/snapshot")
    _git_run(workspace, "read-tree", commit)
    require(_git_run(workspace, "rev-list", "--count", "HEAD").stdout.strip() == b"1")
    require(_git_run(workspace, "rev-parse", "HEAD^{tree}").stdout.decode().strip() == tree)
    return commit, tree


def _git_directory_digest(workspace: Path) -> str:
    git_dir = workspace / ".git"
    require(git_dir.is_dir() and not git_dir.is_symlink())
    records: list[dict[str, object]] = []
    for directory, dirs, files in os.walk(git_dir, topdown=True, followlinks=False):
        current = Path(directory)
        info = current.lstat()
        require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode))
        for name in sorted(dirs):
            child = current / name
            child_info = child.lstat()
            require(stat.S_ISDIR(child_info.st_mode) and not stat.S_ISLNK(child_info.st_mode))
            records.append({"path": child.relative_to(git_dir).as_posix(), "type": "directory",
                            "mode": stat.S_IMODE(child_info.st_mode)})
        for name in sorted(files):
            child = current / name
            child_info = child.lstat()
            require(stat.S_ISREG(child_info.st_mode) and child_info.st_nlink == 1)
            records.append({"path": child.relative_to(git_dir).as_posix(), "type": "file",
                            "mode": stat.S_IMODE(child_info.st_mode), "size": child_info.st_size,
                            "sha256": _hash_path(child)})
    records.sort(key=lambda item: str(item["path"]))
    raw = json.dumps(records, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _hash_path(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode))
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        require(_stat_signature(info) == _stat_signature(os.fstat(fd)))
    finally:
        os.close(fd)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=".manifest-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        payload = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _read_manifest(bundle: Path) -> dict[str, object]:
    path = bundle / MANIFEST_NAME
    _check_private_file(path, maximum=32 * 1024 * 1024)
    try:
        value = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SnapshotError("source snapshot policy check failed") from exc
    require(isinstance(value, dict) and value.keys() == {
        "schema_version", "source_manifest", "snapshot_commit", "snapshot_tree", "git_sha256",
    })
    require(type(value["schema_version"]) is int and value["schema_version"] == 1)
    return value


def _validate_snapshot(bundle: Path, expected_source: dict[str, object]) -> dict[str, object]:
    _check_private_directory(bundle)
    require({item.name for item in bundle.iterdir()} == {WORKSPACE_NAME, MANIFEST_NAME})
    value = _read_manifest(bundle)
    stored_source = value["source_manifest"]
    require(isinstance(stored_source, dict) and _same_manifest(stored_source, expected_source))
    workspace = bundle / WORKSPACE_NAME
    require(workspace.is_dir() and not workspace.is_symlink())
    actual_source, _ = inventory(workspace.resolve(strict=True), int(expected_source["total_bytes"]))
    # The source snapshot's .git directory is intentionally excluded by inventory;
    # the source HEAD is therefore synthetic and compared separately below.
    actual_source["source_head"] = expected_source["source_head"]
    require(_same_manifest(actual_source, expected_source))
    require(_git_run(workspace, "rev-parse", "--verify", "HEAD").stdout.decode().strip()
            == value["snapshot_commit"])
    require(_git_run(workspace, "rev-list", "--count", "HEAD").stdout.strip() == b"1")
    require(_git_run(workspace, "rev-parse", "HEAD^{tree}").stdout.decode().strip()
            == value["snapshot_tree"])
    require(_git_directory_digest(workspace) == value["git_sha256"])
    return {
        "bundle": str(bundle), "workspace": str(workspace),
        "source_head": expected_source["source_head"],
        "tree_sha256": expected_source["tree_sha256"],
        "total_bytes": expected_source["total_bytes"],
        "entry_count": expected_source["entry_count"], "reused": True,
    }


def verify_bundle(bundle_value: str | os.PathLike[str]) -> dict[str, object]:
    try:
        requested = Path(bundle_value)
        require(requested.is_absolute() and requested.resolve(strict=True) == requested)
        bundle = requested
    except (OSError, RuntimeError) as exc:
        raise SnapshotError("source snapshot policy check failed") from exc
    _check_private_directory(bundle)
    metadata = _read_manifest(bundle)
    expected_source = metadata["source_manifest"]
    require(isinstance(expected_source, dict))
    return _validate_snapshot(bundle, expected_source)


def _validate_limit(max_bytes: int) -> None:
    require(type(max_bytes) is int and 1 <= max_bytes <= MAX_CONFIGURED_SNAPSHOT_BYTES)


def _prepare(source_value: str | os.PathLike[str], run_root_value: str | os.PathLike[str],
             max_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES) -> dict[str, object]:
    _validate_limit(max_bytes)
    try:
        source = Path(source_value).resolve(strict=True)
        run_root = Path(run_root_value).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SnapshotError("source snapshot policy check failed") from exc
    require(source.is_dir() and run_root.is_dir())
    _check_private_directory(run_root)
    require(not _inside(source, run_root) and not _inside(run_root, source))

    source_manifest, source_fingerprints = inventory(source, max_bytes)
    bundle = run_root / BUNDLE_NAME
    lock_fd = _open_lock(run_root)
    try:
        locked_manifest, locked_fingerprints = inventory(source, max_bytes)
        require(_same_manifest(locked_manifest, source_manifest)
                and locked_fingerprints == source_fingerprints)
        require(not bundle.is_symlink())
        if bundle.exists():
            return _validate_snapshot(bundle, source_manifest)
        require(not list(run_root.glob(STAGING_PREFIX + "*")))
        required_free = 2 * int(source_manifest["total_bytes"]) + STAGING_RESERVE_BYTES
        try:
            available = shutil.disk_usage(run_root).free
        except OSError as exc:
            raise SnapshotError("source snapshot policy check failed") from exc
        require(available >= required_free)

        stage = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=run_root))
        os.chmod(stage, 0o700)
        try:
            workspace = stage / WORKSPACE_NAME
            copy_snapshot(source, workspace, source_manifest, source_fingerprints)
            copied_manifest, _ = inventory(workspace.resolve(strict=True), max_bytes)
            copied_manifest["source_head"] = source_manifest["source_head"]
            require(_same_manifest(copied_manifest, source_manifest))

            current_manifest, current_fingerprints = inventory(source, max_bytes)
            require(_same_manifest(current_manifest, source_manifest)
                    and current_fingerprints == source_fingerprints)
            snapshot_commit = _git_run(workspace, "rev-parse", "--verify", "HEAD").stdout.decode().strip()
            snapshot_tree = _git_run(workspace, "rev-parse", "HEAD^{tree}").stdout.decode().strip()
            metadata = {
                "schema_version": 1,
                "source_manifest": source_manifest,
                "snapshot_commit": snapshot_commit,
                "snapshot_tree": snapshot_tree,
                "git_sha256": _git_directory_digest(workspace),
            }
            _atomic_json(stage / MANIFEST_NAME, metadata)
            for directory, _, _ in os.walk(stage, topdown=False):
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            _rename_noreplace(stage, bundle)
            fd = os.open(run_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except BaseException:
            if stage.exists() and not stage.is_symlink():
                shutil.rmtree(stage)
            raise
        result = _validate_snapshot(bundle, source_manifest)
        result["reused"] = False
        return result
    finally:
        os.close(lock_fd)


def prepare(source_value: str | os.PathLike[str], run_root_value: str | os.PathLike[str],
            max_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES) -> dict[str, object]:
    try:
        return _prepare(source_value, run_root_value, max_bytes)
    except SnapshotError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise SnapshotError("source snapshot policy check failed") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--source", required=True)
    prepare_parser.add_argument("--run-root", required=True)
    prepare_parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_SNAPSHOT_BYTES)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--bundle", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.source, args.run_root, args.max_bytes)
        else:
            result = verify_bundle(args.bundle)
    except (SnapshotError, OSError, subprocess.SubprocessError):
        print("REFUSED: source snapshot was unsafe, changed, over budget, or incomplete.", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
