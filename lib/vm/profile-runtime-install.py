#!/usr/bin/env python3
"""Read and install one committed profile runtime as bounded immutable data.

This module is intended to be called by the fixed root-owned control helper.
The caller must authorize the owner and hold the maintenance, profile and
per-SHA locks required by the operation. Git runs as the unprivileged owner;
candidate checkout files are never imported or executed.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import importlib.util
import json
import os
import pathlib
import pwd
import re
import secrets
import selectors
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import BinaryIO, Callable


CORE_PATH = pathlib.Path(__file__).with_name("profile-control-core.py")
CORE_SPEC = importlib.util.spec_from_file_location("vm_profile_control_core", CORE_PATH)
if CORE_SPEC is None or CORE_SPEC.loader is None:
    raise RuntimeError("profile control core is unavailable")
core = importlib.util.module_from_spec(CORE_SPEC)
sys.modules.setdefault(CORE_SPEC.name, core)
CORE_SPEC.loader.exec_module(core)

GIT = "/usr/bin/git"
MAX_TREE_OBJECT_BYTES = 16 * 1024 * 1024
MAX_COMMITTED_TREE_BYTES = 64 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 30
GIT_STREAM_MAX_SECONDS = 300
RENAME_NOREPLACE = 1
SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class InstallRefused(RuntimeError):
    """Candidate or immutable runtime installation was not safe to accept."""


@dataclass(frozen=True)
class TreeEntry:
    path: str
    git_mode: str
    object_id: str


@dataclass(frozen=True)
class InstallResult:
    runtime_path: pathlib.Path
    created: bool
    manifest: dict


def _git_environment() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "XDG_CONFIG_HOME": "/nonexistent",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_LITERAL_PATHSPECS": "1",
    }


def _owner_preexec(owner_uid: int, owner_gid: int) -> Callable[[], None] | None:
    if owner_uid <= 0 or owner_gid < 0:
        raise InstallRefused("runtime candidate owner identity is invalid")
    if os.geteuid() == 0:
        try:
            account = pwd.getpwuid(owner_uid)
            groups = os.getgrouplist(account.pw_name, owner_gid)
        except (KeyError, OSError) as error:
            raise InstallRefused("runtime candidate owner account is unavailable") from error
        if account.pw_gid != owner_gid:
            raise InstallRefused("runtime candidate primary group does not match its owner account")

        def drop_privileges() -> None:
            os.setgroups(groups)
            os.setgid(owner_gid)
            os.setuid(owner_uid)

        return drop_privileges
    if os.geteuid() != owner_uid or os.getegid() != owner_gid:
        raise InstallRefused("Git can only inspect the invoking owner’s checkout")
    return None


def _canonical_repository(repository: pathlib.Path, owner_uid: int) -> tuple[pathlib.Path, os.stat_result]:
    if not isinstance(repository, pathlib.Path) or not repository.is_absolute():
        raise InstallRefused("candidate repository must be an absolute canonical directory")
    try:
        resolved = repository.resolve(strict=True)
        info = os.lstat(repository)
    except OSError as error:
        raise InstallRefused("candidate repository is missing or unsafe") from error
    if (resolved != repository or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != owner_uid):
        raise InstallRefused("candidate repository must be a canonical directory owned by the caller")
    current = pathlib.Path(repository.anchor)
    try:
        for component in repository.parts[1:]:
            current /= component
            parent_info = os.lstat(current)
            if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
                raise InstallRefused("candidate repository ancestry contains a symlink or non-directory")
    except OSError as error:
        raise InstallRefused("candidate repository ancestry is unsafe") from error
    return repository, info


def _check_repository_identity(repository: pathlib.Path, before: os.stat_result) -> None:
    try:
        after = os.lstat(repository)
    except OSError as error:
        raise InstallRefused("candidate repository changed during inspection") from error
    identity_fields = ("st_dev", "st_ino", "st_uid", "st_gid", "st_mode", "st_nlink")
    if any(getattr(before, field) != getattr(after, field) for field in identity_fields):
        raise InstallRefused("candidate repository identity changed during inspection")


def _git_argv(repository: pathlib.Path, args: list[str]) -> list[str]:
    return [
        GIT,
        "--no-replace-objects",
        "-c", "core.fsmonitor=false",
        "-c", "core.hooksPath=/dev/null",
        "-c", "core.pager=cat",
        "-c", "credential.helper=",
        "-c", "protocol.file.allow=never",
        "-c", "protocol.ext.allow=never",
        "-C", str(repository),
        *args,
    ]


def _git_capture(
    repository: pathlib.Path,
    args: list[str],
    *,
    owner_uid: int,
    owner_gid: int,
    maximum: int,
) -> bytes:
    try:
        process = subprocess.Popen(
            _git_argv(repository, args), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=_git_environment(), close_fds=True,
            preexec_fn=_owner_preexec(owner_uid, owner_gid),
        )
    except OSError as error:
        raise InstallRefused("fixed Git could not inspect the committed runtime") from error
    assert process.stdout is not None
    descriptor = process.stdout.fileno()
    os.set_blocking(descriptor, False)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    payload = bytearray()
    deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InstallRefused("fixed Git timed out while reading the candidate commit")
            events = selector.select(remaining)
            if not events:
                raise InstallRefused("fixed Git timed out while reading the candidate commit")
            block = os.read(descriptor, min(65536, maximum + 1 - len(payload)))
            if not block:
                break
            payload.extend(block)
            if len(payload) > maximum:
                raise InstallRefused("candidate Git output exceeds its fixed bound")
        try:
            return_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise InstallRefused("fixed Git did not exit after reading the candidate commit") from error
        if return_code != 0:
            raise InstallRefused("fixed Git refused the requested committed object")
        return bytes(payload)
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise
    finally:
        selector.close()
        process.stdout.close()


def _parse_json(raw: bytes, *, description: str) -> object:
    try:
        return json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=core._object_without_duplicate_keys,
            parse_constant=core._reject_json_constant,
        )
    except core.Refused as error:
        raise InstallRefused(str(error)) from error
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise InstallRefused(f"{description} is not valid bounded UTF-8 JSON") from error


def _candidate_head(
    repository: pathlib.Path,
    *,
    commit_sha: str,
    owner_uid: int,
    owner_gid: int,
) -> str:
    try:
        requested = core._valid_sha(commit_sha, field="requested commit_sha")
    except core.Refused as error:
        raise InstallRefused(str(error)) from error
    raw = _git_capture(
        repository, ["rev-parse", "--verify", "HEAD^{commit}"],
        owner_uid=owner_uid, owner_gid=owner_gid, maximum=128,
    )
    try:
        actual = raw.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as error:
        raise InstallRefused("candidate HEAD is not a full commit SHA") from error
    if actual != requested or not SHA_RE.fullmatch(actual):
        raise InstallRefused("candidate repository HEAD does not match the requested full commit SHA")
    return actual


def _object_digest(object_type: str, raw: bytes, object_id: str) -> str:
    algorithm = "sha1" if len(object_id) == 40 else "sha256"
    digest = hashlib.new(algorithm)
    digest.update(f"{object_type} {len(raw)}\0".encode("ascii"))
    digest.update(raw)
    return digest.hexdigest()


def _read_git_object(
    repository: pathlib.Path,
    object_type: str,
    object_id: str,
    *,
    owner_uid: int,
    owner_gid: int,
    maximum: int,
) -> bytes:
    if not SHA_RE.fullmatch(object_id):
        raise InstallRefused("candidate tree contains an invalid Git object ID")
    raw = _git_capture(
        repository, ["cat-file", object_type, object_id],
        owner_uid=owner_uid, owner_gid=owner_gid, maximum=maximum,
    )
    if _object_digest(object_type, raw, object_id) != object_id:
        raise InstallRefused(f"candidate Git {object_type} bytes do not match their object ID")
    return raw


class CommittedGitTree:
    """Resolve requested paths only through hash-verified commit/tree objects."""

    def __init__(self, repository: pathlib.Path, commit_sha: str, *, owner_uid: int, owner_gid: int):
        self.repository = repository
        self.commit_sha = commit_sha
        self.owner_uid = owner_uid
        self.owner_gid = owner_gid
        self.object_id_bytes = len(commit_sha) // 2
        commit = _read_git_object(
            repository, "commit", commit_sha, owner_uid=owner_uid, owner_gid=owner_gid,
            maximum=MAX_TREE_OBJECT_BYTES,
        )
        header, separator, _message = commit.partition(b"\n\n")
        tree_fields = [line[5:] for line in header.split(b"\n") if line.startswith(b"tree ")]
        if (not separator or len(tree_fields) != 1 or len(tree_fields[0]) != len(commit_sha)
                or not re.fullmatch(rb"[0-9a-f]+", tree_fields[0])):
            raise InstallRefused("candidate commit does not contain one valid root tree ID")
        self.root_tree_id = tree_fields[0].decode("ascii")
        self.tree_cache: dict[str, dict[bytes, tuple[bytes, str]]] = {}
        self.tree_bytes = 0

    def _entries(self, tree_id: str) -> dict[bytes, tuple[bytes, str]]:
        cached = self.tree_cache.get(tree_id)
        if cached is not None:
            return cached
        raw = _read_git_object(
            self.repository, "tree", tree_id, owner_uid=self.owner_uid,
            owner_gid=self.owner_gid, maximum=MAX_TREE_OBJECT_BYTES,
        )
        self.tree_bytes += len(raw)
        if self.tree_bytes > MAX_COMMITTED_TREE_BYTES:
            raise InstallRefused("candidate directory-tree metadata exceeds its fixed bound")
        entries = {}
        previous_order_key = None
        offset = 0
        while offset < len(raw):
            mode_end = raw.find(b" ", offset)
            name_end = raw.find(b"\0", mode_end + 1) if mode_end >= 0 else -1
            oid_start = name_end + 1
            oid_end = oid_start + self.object_id_bytes
            if (mode_end <= offset or name_end < mode_end + 2 or oid_end > len(raw)):
                raise InstallRefused("candidate Git tree object is malformed")
            mode = raw[offset:mode_end]
            name = raw[mode_end + 1:name_end]
            object_id_bytes = raw[oid_start:oid_end]
            if name in entries:
                raise InstallRefused("candidate Git tree contains a duplicate entry name")
            if (not name or b"/" in name or name in {b".", b".."}
                    or mode not in {b"40000", b"100644", b"100755", b"120000", b"160000"}
                    or len(object_id_bytes) != self.object_id_bytes):
                raise InstallRefused("candidate Git tree has an unsafe or noncanonical entry")
            order_key = name + (b"/" if mode == b"40000" else b"")
            if previous_order_key is not None and order_key <= previous_order_key:
                raise InstallRefused("candidate Git tree entries are not uniquely sorted")
            previous_order_key = order_key
            entries[name] = (mode, object_id_bytes.hex())
            if len(entries) > 262144:
                raise InstallRefused("candidate Git tree has too many directory entries")
            offset = oid_end
        self.tree_cache[tree_id] = entries
        return entries

    def lookup(self, path: str) -> tuple[str, str]:
        try:
            core._runtime_path(path)
        except core.Refused as error:
            raise InstallRefused(str(error)) from error
        components = path.encode("ascii").split(b"/")
        current_tree = self.root_tree_id
        for index, component in enumerate(components):
            found = self._entries(current_tree).get(component)
            if found is None:
                raise InstallRefused("candidate commit omits a declared runtime path")
            mode, object_id = found
            if index + 1 == len(components):
                return mode.decode("ascii"), object_id
            if mode != b"40000":
                raise InstallRefused("candidate runtime path traverses a non-directory Git entry")
            current_tree = object_id
        raise InstallRefused("candidate runtime path is empty")


def _read_descriptor(
    tree: CommittedGitTree,
    *,
    owner_uid: int,
    owner_gid: int,
) -> tuple[dict, str]:
    mode, object_id = tree.lookup("profile-runtime.json")
    if mode not in {"100644", "100755"}:
        raise InstallRefused("committed profile-runtime.json is not a regular Git blob")
    raw = _read_git_object(
        tree.repository, "blob", object_id,
        owner_uid=owner_uid, owner_gid=owner_gid,
        maximum=core.MAX_MANIFEST_BYTES,
    )
    descriptor = _parse_json(raw, description="committed profile-runtime.json")
    try:
        return core.validate_runtime_descriptor(descriptor), hashlib.sha256(raw).hexdigest()
    except core.Refused as error:
        raise InstallRefused(str(error)) from error


def _tree_entries(
    tree: CommittedGitTree,
    descriptor: dict,
) -> list[TreeEntry]:
    declared = {entry["path"]: entry["git_mode"] for entry in descriptor["files"]}
    result = []
    for path in [*declared, "profile-runtime.json"]:
        mode, object_id = tree.lookup(path)
        expected_mode = declared.get(path)
        if path == "profile-runtime.json":
            if mode not in {"100644", "100755"}:
                raise InstallRefused("candidate runtime descriptor is not a regular Git blob")
        elif expected_mode is None or mode != expected_mode:
            raise InstallRefused("candidate Git mode does not match the committed runtime descriptor")
        if mode not in {"100644", "100755"} or not SHA_RE.fullmatch(object_id):
            raise InstallRefused("candidate runtime entry is not a regular Git blob")
        result.append(TreeEntry(path, mode, object_id))
    result.sort(key=lambda entry: entry.path)
    return result


class _ExactReader:
    def __init__(self, stream: BinaryIO, size: int):
        self.stream = stream
        self.remaining = size

    def read(self, maximum: int) -> bytes:
        if self.remaining <= 0:
            return b""
        block = self.stream.read(min(maximum, self.remaining))
        if not block:
            raise InstallRefused("candidate Git blob ended before its declared size")
        self.remaining -= len(block)
        return block


class _PipeReader:
    """Nonblocking, deadline-bound reader for fixed Git's batch protocol."""

    def __init__(self, fd: int, deadline: float):
        self.fd = fd
        self.deadline = deadline
        self.pending = bytearray()
        self.selector = selectors.DefaultSelector()
        os.set_blocking(fd, False)
        self.selector.register(fd, selectors.EVENT_READ)

    def _read_from_pipe(self, maximum: int) -> bytes:
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise InstallRefused("fixed Git timed out while streaming committed blobs")
            try:
                return os.read(self.fd, maximum)
            except BlockingIOError:
                continue

    def read(self, maximum: int) -> bytes:
        if maximum <= 0:
            return b""
        if self.pending:
            count = min(maximum, len(self.pending))
            result = bytes(self.pending[:count])
            del self.pending[:count]
            return result
        return self._read_from_pipe(maximum)

    def readline(self, maximum: int) -> bytes:
        while True:
            newline = self.pending.find(b"\n")
            if newline >= 0:
                result = bytes(self.pending[:newline + 1])
                del self.pending[:newline + 1]
                return result
            if len(self.pending) >= maximum:
                result = bytes(self.pending[:maximum])
                del self.pending[:maximum]
                return result
            block = self._read_from_pipe(min(4096, maximum - len(self.pending)))
            if not block:
                result = bytes(self.pending)
                self.pending.clear()
                return result
            self.pending.extend(block)

    def has_only_eof(self) -> bool:
        if self.pending:
            return False
        return self.read(1) == b""

    def close(self) -> None:
        self.selector.close()


def _write_pipe_request(fd: int, payload: bytes, *, deadline: float) -> None:
    selector = selectors.DefaultSelector()
    selector.register(fd, selectors.EVENT_WRITE)
    view = memoryview(payload)
    try:
        while view:
            if deadline <= time.monotonic():
                raise InstallRefused("fixed Git timed out while accepting a blob request")
            try:
                count = os.write(fd, view)
            except BlockingIOError:
                if not selector.select(deadline - time.monotonic()):
                    raise InstallRefused("fixed Git timed out while accepting a blob request")
                continue
            if count <= 0:
                raise InstallRefused("fixed Git request pipe closed unexpectedly")
            view = view[count:]
    finally:
        selector.close()


def _read_blobs(
    repository: pathlib.Path,
    entries: list[TreeEntry],
    *,
    owner_uid: int,
    owner_gid: int,
    consume: Callable[[TreeEntry, _ExactReader, int], str],
) -> dict[str, str]:
    try:
        process = subprocess.Popen(
            _git_argv(repository, ["cat-file", "--batch"]),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=_git_environment(), close_fds=True,
            preexec_fn=_owner_preexec(owner_uid, owner_gid),
        )
    except OSError as error:
        raise InstallRefused("fixed Git could not open the committed runtime blobs") from error
    assert process.stdin is not None and process.stdout is not None
    digests = {}
    total_size = 0
    overall_deadline = time.monotonic() + GIT_STREAM_MAX_SECONDS
    reader = _PipeReader(process.stdout.fileno(), overall_deadline)
    stdin_fd = process.stdin.fileno()
    os.set_blocking(stdin_fd, False)
    try:
        for entry in entries:
            deadline = min(overall_deadline, time.monotonic() + GIT_TIMEOUT_SECONDS)
            reader.deadline = deadline
            _write_pipe_request(
                stdin_fd, entry.object_id.encode("ascii") + b"\n", deadline=deadline,
            )
            header = reader.readline(256)
            try:
                returned_oid, object_type, raw_size = header.rstrip(b"\n").split(b" ")
                size = int(raw_size)
            except (ValueError, OverflowError) as error:
                raise InstallRefused("fixed Git returned a malformed blob header") from error
            try:
                returned_oid_text = returned_oid.decode("ascii", errors="strict")
            except UnicodeDecodeError as error:
                raise InstallRefused("fixed Git returned an invalid blob identity") from error
            if (not header.endswith(b"\n") or returned_oid_text != entry.object_id
                    or object_type != b"blob" or size < 0 or size > core.MAX_FILE_BYTES):
                raise InstallRefused("candidate Git object is not a bounded regular blob")
            total_size += size
            if total_size > core.MAX_TREE_BYTES:
                raise InstallRefused("candidate runtime exceeds its total size bound")
            exact_reader = _ExactReader(reader, size)
            digests[entry.path] = consume(entry, exact_reader, size)
            if exact_reader.remaining != 0:
                raise InstallRefused("runtime extractor did not consume the complete Git blob")
            if reader.read(1) != b"\n":
                raise InstallRefused("fixed Git returned malformed blob framing")
            if time.monotonic() > overall_deadline:
                raise InstallRefused("fixed Git timed out while streaming candidate blobs")
        process.stdin.close()
        try:
            return_code = process.wait(timeout=max(0.1, overall_deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise InstallRefused("fixed Git timed out after streaming candidate blobs") from error
        reader.deadline = time.monotonic() + 1
        if return_code != 0 or not reader.has_only_eof():
            raise InstallRefused("fixed Git did not finish the bounded blob stream cleanly")
        return digests
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise
    finally:
        reader.close()
        if not process.stdin.closed:
            process.stdin.close()
        process.stdout.close()


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise InstallRefused("runtime extraction write made no progress")
        view = view[count:]


def _open_or_create_directory(parent_fd: int, name: str, *, owner_uid: int, owner_gid: int) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise InstallRefused("could not create a private runtime staging directory") from error
    except OSError as error:
        raise InstallRefused("runtime staging directory could not be inspected") from error
    if (not stat.S_ISDIR(before.st_mode) or before.st_uid != owner_uid
            or before.st_gid != owner_gid or stat.S_IMODE(before.st_mode) != 0o700):
        raise InstallRefused("runtime staging directory has unexpected ownership or mode")
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        raise InstallRefused("runtime staging directory could not be opened safely") from error
    opened = os.fstat(fd)
    if ((before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or not stat.S_ISDIR(opened.st_mode) or opened.st_uid != owner_uid
            or opened.st_gid != owner_gid or stat.S_IMODE(opened.st_mode) != 0o700):
        os.close(fd)
        raise InstallRefused("runtime staging directory changed during open")
    return fd


def _parent_directory_fd(root_fd: int, relative: str, *, owner_uid: int, owner_gid: int) -> int:
    current_fd = os.dup(root_fd)
    try:
        for component in relative.split("/")[:-1]:
            next_fd = _open_or_create_directory(
                current_fd, component, owner_uid=owner_uid, owner_gid=owner_gid,
            )
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _write_git_blob(
    stage_fd: int,
    entry: TreeEntry,
    reader: _ExactReader,
    size: int,
    *,
    owner_uid: int,
    owner_gid: int,
) -> str:
    parent_fd = _parent_directory_fd(
        stage_fd, entry.path, owner_uid=owner_uid, owner_gid=owner_gid,
    )
    filename = entry.path.rsplit("/", 1)[-1]
    fd = None
    digest = hashlib.sha256()
    git_object_digest = hashlib.new("sha1" if len(entry.object_id) == 40 else "sha256")
    git_object_digest.update(f"blob {size}\0".encode("ascii"))
    try:
        fd = os.open(
            filename,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600, dir_fd=parent_fd,
        )
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != owner_uid or info.st_gid != owner_gid):
            raise InstallRefused("runtime staging file has unexpected identity")
        total = 0
        while total < size:
            block = reader.read(min(65536, size - total))
            total += len(block)
            digest.update(block)
            git_object_digest.update(block)
            _write_all(fd, block)
        if total != size:
            raise InstallRefused("runtime extraction size does not match the Git object")
        if git_object_digest.hexdigest() != entry.object_id:
            raise InstallRefused("candidate blob bytes do not match their committed Git object ID")
        mode = 0o444 if entry.git_mode == "100644" else 0o555
        os.fchown(fd, owner_uid, owner_gid)
        os.fchmod(fd, mode)
        os.fsync(fd)
        return digest.hexdigest()
    except OSError as error:
        raise InstallRefused("runtime blob could not be extracted safely") from error
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def _write_manifest(stage_fd: int, manifest: dict, *, owner_uid: int, owner_gid: int) -> None:
    raw = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    fd = None
    try:
        fd = os.open(
            core.MANIFEST_FILENAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600, dir_fd=stage_fd,
        )
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise InstallRefused("runtime manifest staging inode is unsafe")
        _write_all(fd, raw)
        os.fchown(fd, owner_uid, owner_gid)
        os.fchmod(fd, 0o444)
        os.fsync(fd)
    except OSError as error:
        raise InstallRefused("runtime manifest could not be written safely") from error
    finally:
        if fd is not None:
            os.close(fd)


def _finalize_directories(
    directory_fd: int,
    prefix: str,
    *,
    expected_files: dict[str, int],
    expected_directories: set[str],
    owner_uid: int,
    owner_gid: int,
) -> None:
    names = os.listdir(directory_fd)
    for name in names:
        relative = f"{prefix}/{name}" if prefix else name
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as error:
            raise InstallRefused("runtime staging tree changed during finalization") from error
        if relative in expected_directories:
            child_fd = _open_or_create_directory(
                directory_fd, name, owner_uid=owner_uid, owner_gid=owner_gid,
            )
            try:
                _finalize_directories(
                    child_fd, relative, expected_files=expected_files,
                    expected_directories=expected_directories,
                    owner_uid=owner_uid, owner_gid=owner_gid,
                )
                os.fchown(child_fd, owner_uid, owner_gid)
                os.fchmod(child_fd, 0o555)
                os.fsync(child_fd)
            finally:
                os.close(child_fd)
        elif relative in expected_files:
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid
                    or info.st_gid != owner_gid or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != expected_files[relative]):
                raise InstallRefused("runtime staging tree contains an unexpected file")
        else:
            raise InstallRefused("runtime staging tree contains an undeclared entry")


def _rename_noreplace(src_fd: int, source: str, dst_fd: int, destination: str) -> None:
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as error:
        raise InstallRefused("atomic no-replace runtime publication is unavailable") from error
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(src_fd, os.fsencode(source), dst_fd, os.fsencode(destination), RENAME_NOREPLACE)
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise InstallRefused("a runtime target appeared during publication; preserving both trees")
        raise InstallRefused(f"atomic runtime publication failed ({os.strerror(error_number)})")


def install_committed_runtime(
    repository: pathlib.Path,
    commit_sha: str,
    *,
    owner_uid: int,
    owner_gid: int,
    runtime_store: pathlib.Path = core.RUNTIME_STORE,
) -> InstallResult:
    """Install only descriptor-listed blobs from the owner's exact committed HEAD.

    The caller is responsible for the ordered control/profile/SHA locks and for
    the final profile-mapping transaction. This function takes no request-supplied
    destination path in production; ``runtime_store`` is injectable for fixtures.
    Interrupted staging trees are intentionally retained for inspection.
    """
    if not isinstance(runtime_store, pathlib.Path) or not runtime_store.is_absolute():
        raise InstallRefused("immutable runtime store must be an absolute fixed path")
    repository, repository_identity = _canonical_repository(repository, owner_uid)
    commit_sha = _candidate_head(
        repository, commit_sha=commit_sha, owner_uid=owner_uid, owner_gid=owner_gid,
    )
    tree = CommittedGitTree(
        repository, commit_sha, owner_uid=owner_uid, owner_gid=owner_gid,
    )
    descriptor, descriptor_digest = _read_descriptor(
        tree, owner_uid=owner_uid, owner_gid=owner_gid,
    )
    entries = _tree_entries(tree, descriptor)
    _check_repository_identity(repository, repository_identity)

    try:
        store_fd = core._open_absolute_directory(runtime_store)
    except (OSError, core.Refused) as error:
        raise InstallRefused("immutable runtime store is missing or contains an unsafe path") from error
    expected_uid = 0 if os.geteuid() == 0 else owner_uid
    expected_gid = 0 if os.geteuid() == 0 else owner_gid
    store_info = os.fstat(store_fd)
    if (not stat.S_ISDIR(store_info.st_mode) or store_info.st_uid != expected_uid
            or store_info.st_gid != expected_gid
            or store_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        os.close(store_fd)
        raise InstallRefused("immutable runtime store ownership or mode is unsafe")
    runtime_path = runtime_store / commit_sha
    try:
        existing = os.stat(commit_sha, dir_fd=store_fd, follow_symlinks=False)
    except FileNotFoundError:
        existing = None
    except OSError as error:
        os.close(store_fd)
        raise InstallRefused("existing runtime target could not be inspected") from error
    if existing is not None:
        if not stat.S_ISDIR(existing.st_mode) or stat.S_ISLNK(existing.st_mode):
            os.close(store_fd)
            raise InstallRefused("existing SHA target is unknown; preserving it")
        try:
            manifest = core.verify_runtime_installation(
                runtime_path, pinned_sha=commit_sha, expected_uid=expected_uid,
            )
        except core.Refused as error:
            os.close(store_fd)
            raise InstallRefused("existing SHA target is incomplete or modified; preserving it") from error
        os.close(store_fd)
        return InstallResult(runtime_path, False, manifest)

    stage_fd = None
    # A same-parent rename avoids a crash window where cross-directory rename
    # requires temporarily making the published runtime root owner-writable.
    stage_name = f".staging-{commit_sha}-{secrets.token_hex(8)}"
    expected_files = {
        entry.path: (0o444 if entry.git_mode == "100644" else 0o555)
        for entry in entries
    }
    expected_directories = set()
    for path in expected_files:
        components = path.split("/")[:-1]
        for index in range(1, len(components) + 1):
            expected_directories.add("/".join(components[:index]))
    try:
        try:
            os.mkdir(stage_name, mode=0o700, dir_fd=store_fd)
        except OSError as error:
            raise InstallRefused("could not create a unique private runtime staging tree") from error
        stage_fd = _open_or_create_directory(
            store_fd, stage_name, owner_uid=expected_uid, owner_gid=expected_gid,
        )

        def consume(entry: TreeEntry, reader: _ExactReader, size: int) -> str:
            return _write_git_blob(
                stage_fd, entry, reader, size,
                owner_uid=expected_uid, owner_gid=expected_gid,
            )

        digests = _read_blobs(
            repository, entries, owner_uid=owner_uid, owner_gid=owner_gid, consume=consume,
        )
        if digests.get("profile-runtime.json") != descriptor_digest:
            raise InstallRefused("committed runtime descriptor changed during bounded extraction")
        _check_repository_identity(repository, repository_identity)
        manifest = {
            "schema_version": core.INSTALL_MANIFEST_SCHEMA_VERSION,
            "commit_sha": commit_sha,
            "profile_runtime_api": descriptor["profile_runtime_api"],
            "control_helper_api_min": descriptor["control_helper_api_min"],
            "control_helper_api_max": descriptor["control_helper_api_max"],
            "files": [
                {"path": entry.path, "git_mode": entry.git_mode, "sha256": digests[entry.path]}
                for entry in entries
            ],
        }
        try:
            manifest = core.validate_runtime_manifest(manifest, pinned_sha=commit_sha)
        except core.Refused as error:
            raise InstallRefused(str(error)) from error
        _write_manifest(stage_fd, manifest, owner_uid=expected_uid, owner_gid=expected_gid)
        expected_files[core.MANIFEST_FILENAME] = 0o444
        _finalize_directories(
            stage_fd, "", expected_files=expected_files,
            expected_directories=expected_directories,
            owner_uid=expected_uid, owner_gid=expected_gid,
        )
        os.fchown(stage_fd, expected_uid, expected_gid)
        os.fchmod(stage_fd, 0o555)
        os.fsync(stage_fd)
        os.close(stage_fd)
        stage_fd = None

        staged_path = runtime_store / stage_name
        try:
            core.verify_runtime_installation(
                staged_path, pinned_sha=commit_sha, expected_uid=expected_uid,
                allow_staging=True,
            )
        except core.Refused as error:
            raise InstallRefused("completed staging tree failed immutable-runtime verification") from error
        try:
            os.stat(commit_sha, dir_fd=store_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise InstallRefused("a runtime target appeared during extraction; preserving the stage")
        _rename_noreplace(store_fd, stage_name, store_fd, commit_sha)
        os.fsync(store_fd)
        final_manifest = core.verify_runtime_installation(
            runtime_path, pinned_sha=commit_sha, expected_uid=expected_uid,
        )
        return InstallResult(runtime_path, True, final_manifest)
    except OSError as error:
        raise InstallRefused("runtime installation filesystem operation failed safely") from error
    finally:
        if stage_fd is not None:
            os.close(stage_fd)
        os.close(store_fd)
