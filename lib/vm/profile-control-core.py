#!/usr/bin/env python3
"""Fail-closed protocol and immutable-runtime primitives for named VM profiles.

This module deliberately performs no privileged mutation and executes no runtime
code. The installed fixed helper is responsible for authorization, protected
metadata, locks and lifecycle effects; it calls these primitives after reading
the profile's authoritative runtime pin.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import stat

PROTOCOL_VERSION = 1
INSTALL_MANIFEST_SCHEMA_VERSION = 1
PROFILE_RUNTIME_API = 1
CONTROL_HELPER_API = 1
MAX_REQUEST_BYTES = 64 * 1024
MAX_INVENTORY_ENTRIES = 4096
MAX_PATH_BYTES = 240
# Leave room for every contract-valid inventory entry (maximum path, mode and
# digest plus JSON framing), rather than rejecting a schema-valid installation.
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TREE_BYTES = 256 * 1024 * 1024
MANIFEST_FILENAME = "manifest.json"
RUNTIME_STORE = pathlib.Path("/opt/little-mester/v3")

SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
PROFILE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
RESERVED_PROFILE_NAMES = frozenset({
    "system", "profiles", "installations", "prune", "bootstrap", "control",
    "prepare-dependencies",
})
RUNTIME_PREFIXES = ("lib/vm/", "agent/opencode/plugins/")


class Refused(RuntimeError):
    """The request or protected runtime cannot be accepted safely."""


def validate_profile_name(value: object) -> str:
    if (not isinstance(value, str) or not PROFILE_NAME.fullmatch(value)
            or value in RESERVED_PROFILE_NAMES):
        raise Refused("profile name is invalid or reserved")
    return value


def _valid_sha(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise Refused(f"{field} must be a full lowercase commit SHA")
    return value


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refused("JSON object contains a duplicate key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise Refused("JSON contains a non-standard numeric value")


def _bounded_text(value: object, *, field: str, maximum: int = 4096) -> str:
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(char) < 0x20 or ord(char) == 0x7f for char in value)):
        raise Refused(f"{field} is invalid")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise Refused(f"{field} is invalid") from error
    return value


def _absolute_path(value: object, *, field: str) -> str:
    text = _bounded_text(value, field=field)
    if not text.startswith("/"):
        raise Refused(f"{field} must be an absolute path")
    return text


def _exact_keys(request: dict, *, required: set[str], optional: set[str] = frozenset()) -> None:
    keys = set(request)
    if not required <= keys or keys - required - optional:
        raise Refused("request keys do not match the operation schema")


_PROFILE_ONLY_OPERATIONS = frozenset({
    "status", "down", "ssh", "auth", "tui", "verify", "accept", "settings.show",
    "profiles.show", "reset", "remove",
})


def validate_request(request: object) -> dict:
    """Validate a protocol-v1 request's exact operation-specific field set.

    This is only the request-schema layer. It does not authorize a caller or
    implement an operation. In particular, signed control-artifact framing is
    intentionally handled by the separate bootstrap/update adapter.
    """
    if type(request) is not dict:
        raise Refused("request must be a JSON object")
    if type(request.get("protocol")) is not int or request["protocol"] != PROTOCOL_VERSION:
        raise Refused("unsupported request protocol")
    operation = request.get("operation")
    if not isinstance(operation, str):
        raise Refused("request operation is missing or invalid")

    if operation == "install":
        _exact_keys(request, required={"protocol", "operation", "profile", "repository", "commit_sha"})
        validate_profile_name(request["profile"])
        _absolute_path(request["repository"], field="repository")
        _valid_sha(request["commit_sha"], field="commit_sha")
    elif operation == "up":
        _exact_keys(
            request,
            required={"protocol", "operation", "profile"},
            optional={"workspace", "unsafe_recurse", "reassign"},
        )
        validate_profile_name(request["profile"])
        if "workspace" in request:
            _absolute_path(request["workspace"], field="workspace")
        for field in ("unsafe_recurse", "reassign"):
            if field in request and type(request[field]) is not bool:
                raise Refused(f"{field} must be a boolean")
    elif operation in _PROFILE_ONLY_OPERATIONS:
        _exact_keys(request, required={"protocol", "operation", "profile"})
        validate_profile_name(request["profile"])
    elif operation == "profiles.list" or operation == "installations":
        _exact_keys(request, required={"protocol", "operation"})
    elif operation == "settings.set":
        _exact_keys(
            request,
            required={"protocol", "operation", "profile"},
            optional={"model", "theme"},
        )
        validate_profile_name(request["profile"])
        if not ({"model", "theme"} & set(request)):
            raise Refused("settings.set requires model or theme")
        for field in ("model", "theme"):
            if field in request:
                _bounded_text(request[field], field=field, maximum=128)
    elif operation == "prune":
        _exact_keys(request, required={"protocol", "operation", "dry_run"})
        if type(request["dry_run"]) is not bool:
            raise Refused("dry_run must be a boolean")
    else:
        raise Refused("request operation is not supported by this helper core")

    return dict(request)


def parse_request(raw: bytes) -> dict:
    """Decode one bounded stdin request, rejecting duplicate keys and junk."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_REQUEST_BYTES:
        raise Refused("request is empty or exceeds the size bound")
    try:
        text = raw.decode("utf-8", errors="strict")
        request = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except Refused:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise Refused("request is not one valid UTF-8 JSON value") from error
    return validate_request(request)


def read_request(stream) -> dict:
    """Read one request from a binary stream without buffering unbounded stdin."""
    data = bytearray()
    while len(data) <= MAX_REQUEST_BYTES:
        block = stream.read(min(8192, MAX_REQUEST_BYTES + 1 - len(data)))
        if not isinstance(block, bytes):
            raise Refused("request stream did not return bytes")
        if not block:
            break
        data.extend(block)
    return parse_request(bytes(data))


def _runtime_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise Refused("runtime inventory path is invalid or too long")
    try:
        if len(value.encode("utf-8", errors="strict")) > MAX_PATH_BYTES:
            raise Refused("runtime inventory path is invalid or too long")
        value.encode("ascii")
    except UnicodeEncodeError as error:
        raise Refused("runtime inventory path must use ASCII characters") from error
    if any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._/-" for char in value):
        raise Refused("runtime inventory path contains an unsupported character")
    if value == "profile-runtime.json":
        return value
    if not value.startswith(RUNTIME_PREFIXES) or value.startswith("/") or value.endswith("/"):
        raise Refused("runtime inventory path is outside the approved roots")
    if any(component in {"", ".", ".."} for component in value.split("/")):
        raise Refused("runtime inventory path contains an unsafe component")
    return value


def validate_runtime_manifest(
    manifest: object,
    *,
    pinned_sha: str,
    helper_api: int = CONTROL_HELPER_API,
    runtime_api: int = PROFILE_RUNTIME_API,
) -> dict:
    """Validate the exact immutable-install manifest schema and API pin."""
    pinned_sha = _valid_sha(pinned_sha, field="pinned_sha")
    if type(helper_api) is not int or helper_api < 1 or type(runtime_api) is not int or runtime_api < 1:
        raise Refused("helper or runtime API version is invalid")
    if type(manifest) is not dict:
        raise Refused("runtime manifest must be an object")
    expected_keys = {
        "schema_version", "commit_sha", "profile_runtime_api",
        "control_helper_api_min", "control_helper_api_max", "files",
    }
    if set(manifest) != expected_keys:
        raise Refused("runtime manifest keys do not match schema v1")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != INSTALL_MANIFEST_SCHEMA_VERSION:
        raise Refused("runtime manifest schema is unsupported")
    if _valid_sha(manifest["commit_sha"], field="manifest commit_sha") != pinned_sha:
        raise Refused("runtime manifest does not match the profile's pinned SHA")

    api_fields = ("profile_runtime_api", "control_helper_api_min", "control_helper_api_max")
    for field in api_fields:
        if type(manifest[field]) is not int or manifest[field] < 1:
            raise Refused("runtime manifest API version is invalid")
    if manifest["profile_runtime_api"] != runtime_api:
        raise Refused("runtime API is incompatible with this helper")
    api_min = manifest["control_helper_api_min"]
    api_max = manifest["control_helper_api_max"]
    if api_min > api_max or not api_min <= helper_api <= api_max:
        raise Refused("runtime requires an incompatible control-helper API")

    files = manifest["files"]
    if type(files) is not list or not files or len(files) > MAX_INVENTORY_ENTRIES:
        raise Refused("runtime manifest inventory is empty or exceeds its bound")
    paths = []
    entrypoint_seen = False
    descriptor_seen = False
    for entry in files:
        if type(entry) is not dict or set(entry) != {"path", "git_mode", "sha256"}:
            raise Refused("runtime manifest inventory entry is malformed")
        path = _runtime_path(entry["path"])
        if not isinstance(entry["git_mode"], str) or entry["git_mode"] not in {"100644", "100755"}:
            raise Refused("runtime inventory contains an unsafe Git mode")
        digest = entry["sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise Refused("runtime inventory digest is invalid")
        paths.append(path)
        entrypoint_seen |= path == "lib/vm/profile-runtime.py"
        descriptor_seen |= path == "profile-runtime.json"
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise Refused("runtime inventory must be sorted and unique")
    if not entrypoint_seen or not descriptor_seen:
        raise Refused("runtime inventory omits its fixed entrypoint or descriptor")
    return dict(manifest)


def _open_absolute_directory(path: pathlib.Path) -> int:
    if not path.is_absolute():
        raise Refused("runtime store path must be absolute")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current_fd = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except OSError as error:
        os.close(current_fd)
        raise Refused("runtime store path is missing or contains a symlink") from error


def _same_inode(before: os.stat_result, after: os.stat_result) -> bool:
    return (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)


def _same_file_state(before: os.stat_result, after: os.stat_result) -> bool:
    fields = ("st_dev", "st_ino", "st_uid", "st_gid", "st_mode", "st_nlink", "st_size",
              "st_mtime_ns", "st_ctime_ns")
    return all(getattr(before, field) == getattr(after, field) for field in fields)


def _check_directory(info: os.stat_result, *, expected_uid: int, mode: int | None) -> None:
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != expected_uid
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or (mode is not None and stat.S_IMODE(info.st_mode) != mode)):
        raise Refused("runtime directory ownership, type or mode is unsafe")


def _open_child_directory(
    parent_fd: int,
    name: str,
    *,
    expected_uid: int,
    expected_device: int | None = None,
) -> int:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _check_directory(before, expected_uid=expected_uid, mode=0o555)
        if expected_device is not None and before.st_dev != expected_device:
            raise Refused("runtime directory crosses a filesystem boundary")
        child_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        try:
            after = os.fstat(child_fd)
            _check_directory(after, expected_uid=expected_uid, mode=0o555)
            if (not _same_inode(before, after)
                    or (expected_device is not None and after.st_dev != expected_device)):
                raise Refused("runtime directory changed during verification")
            return child_fd
        except BaseException:
            os.close(child_fd)
            raise
    except OSError as error:
        raise Refused("runtime directory is missing or unsafe") from error


def _read_regular_at(
    parent_fd: int,
    name: str,
    *,
    expected_uid: int,
    expected_mode: int,
    maximum: int,
    expected_device: int,
) -> tuple[bytes, os.stat_result]:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != expected_uid
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != expected_mode
                or before.st_size < 0 or before.st_size > maximum
                or before.st_dev != expected_device):
            raise Refused("runtime metadata file ownership, type or mode is unsafe")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    except OSError as error:
        raise Refused("runtime metadata file is missing or unsafe") from error
    try:
        opened = os.fstat(fd)
        if (not _same_inode(before, opened) or not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != expected_uid or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) != expected_mode
                or opened.st_size < 0 or opened.st_size > maximum
                or opened.st_dev != expected_device):
            raise Refused("runtime metadata file changed during verification")
        chunks = bytearray()
        while len(chunks) <= maximum:
            block = os.read(fd, min(65536, maximum + 1 - len(chunks)))
            if not block:
                break
            chunks.extend(block)
        after = os.fstat(fd)
        if (len(chunks) > maximum or not _same_file_state(opened, after)
                or len(chunks) != after.st_size):
            raise Refused("runtime metadata file changed or exceeded its bound")
        return bytes(chunks), after
    finally:
        os.close(fd)


def _hash_runtime_file(
    parent_fd: int,
    name: str,
    *,
    expected_uid: int,
    expected_mode: int,
    expected_sha256: str,
    expected_device: int,
) -> int:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != expected_uid
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != expected_mode
                or before.st_size < 0 or before.st_size > MAX_FILE_BYTES
                or before.st_dev != expected_device):
            raise Refused("runtime file ownership, type or mode is unsafe")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    except OSError as error:
        raise Refused("runtime file is missing or unsafe") from error
    try:
        opened = os.fstat(fd)
        if (not _same_inode(before, opened) or not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != expected_uid or opened.st_nlink != 1
                or stat.S_IMODE(opened.st_mode) != expected_mode
                or opened.st_size < 0 or opened.st_size > MAX_FILE_BYTES
                or opened.st_dev != expected_device):
            raise Refused("runtime file changed during verification")
        digest = hashlib.sha256()
        size = 0
        while size <= MAX_FILE_BYTES:
            block = os.read(fd, min(65536, MAX_FILE_BYTES + 1 - size))
            if not block:
                break
            size += len(block)
            digest.update(block)
        after = os.fstat(fd)
        if (size > MAX_FILE_BYTES or not _same_file_state(opened, after)
                or size != after.st_size):
            raise Refused("runtime file changed or exceeded its bound")
        if digest.hexdigest() != expected_sha256:
            raise Refused("runtime file digest does not match its manifest")
        return size
    finally:
        os.close(fd)


def _parse_manifest_bytes(raw: bytes) -> dict:
    try:
        return json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except Refused:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise Refused("runtime manifest is not valid bounded UTF-8 JSON") from error


def verify_runtime_installation(
    runtime_directory: pathlib.Path,
    *,
    pinned_sha: str,
    helper_api: int = CONTROL_HELPER_API,
    runtime_api: int = PROFILE_RUNTIME_API,
    expected_uid: int = 0,
) -> dict:
    """Verify one complete immutable runtime tree without following links.

    The root-owned common bootstrap establishes the protected `/opt` ancestry;
    this verifier opens that fixed store component-by-component without following
    links and validates the selected SHA directory and every tree node.
    """
    pinned_sha = _valid_sha(pinned_sha, field="pinned_sha")
    if type(expected_uid) is not int or expected_uid < 0:
        raise Refused("expected runtime owner is invalid")
    if not isinstance(runtime_directory, pathlib.Path) or runtime_directory.name != pinned_sha:
        raise Refused("runtime directory does not match the profile's pinned SHA")
    store_path = runtime_directory.parent
    if ".." in store_path.parts or ".." in runtime_directory.parts:
        raise Refused("runtime path contains traversal components")
    store_fd = _open_absolute_directory(store_path)
    target_fd = None
    try:
        store_info = os.fstat(store_fd)
        _check_directory(store_info, expected_uid=expected_uid, mode=None)
        target_fd = _open_child_directory(
            store_fd, pinned_sha, expected_uid=expected_uid, expected_device=store_info.st_dev,
        )
        target_info = os.fstat(target_fd)
        _check_directory(target_info, expected_uid=expected_uid, mode=0o555)
        if target_info.st_dev != store_info.st_dev:
            raise Refused("runtime tree crosses a filesystem boundary")

        raw_manifest, _ = _read_regular_at(
            target_fd, MANIFEST_FILENAME, expected_uid=expected_uid,
            expected_mode=0o444, maximum=MAX_MANIFEST_BYTES, expected_device=target_info.st_dev,
        )
        manifest = validate_runtime_manifest(
            _parse_manifest_bytes(raw_manifest), pinned_sha=pinned_sha,
            helper_api=helper_api, runtime_api=runtime_api,
        )
        inventory = {entry["path"]: entry for entry in manifest["files"]}
        expected_directories = set()
        for path in inventory:
            components = path.split("/")[:-1]
            for index in range(1, len(components) + 1):
                expected_directories.add("/".join(components[:index]))

        seen_files = set()
        seen_directories = set()
        total_size = 0

        def walk(directory_fd: int, prefix: str = "") -> None:
            nonlocal total_size
            directory_before = os.fstat(directory_fd)
            names = os.listdir(directory_fd)
            for name in names:
                path = f"{prefix}/{name}" if prefix else name
                if path == MANIFEST_FILENAME and not prefix:
                    continue
                if path in expected_directories:
                    child_fd = _open_child_directory(
                        directory_fd, name, expected_uid=expected_uid,
                        expected_device=target_info.st_dev,
                    )
                    try:
                        seen_directories.add(path)
                        walk(child_fd, path)
                    finally:
                        os.close(child_fd)
                    continue
                entry = inventory.get(path)
                if entry is None:
                    raise Refused("runtime tree contains an undeclared path")
                expected_mode = 0o444 if entry["git_mode"] == "100644" else 0o555
                size = _hash_runtime_file(
                    directory_fd, name, expected_uid=expected_uid,
                    expected_mode=expected_mode, expected_sha256=entry["sha256"],
                    expected_device=target_info.st_dev,
                )
                total_size += size
                if total_size > MAX_TREE_BYTES:
                    raise Refused("runtime tree exceeds its total size bound")
                seen_files.add(path)
            directory_after = os.fstat(directory_fd)
            _check_directory(directory_after, expected_uid=expected_uid, mode=0o555)
            if (not _same_file_state(directory_before, directory_after)
                    or directory_after.st_dev != target_info.st_dev):
                raise Refused("runtime directory changed during verification")

        walk(target_fd)
        if seen_files != set(inventory) or seen_directories != expected_directories:
            raise Refused("runtime tree does not match its complete manifest inventory")
        return manifest
    except OSError as error:
        raise Refused("runtime tree could not be inspected safely") from error
    finally:
        if target_fd is not None:
            os.close(target_fd)
        os.close(store_fd)


def resolve_pinned_runtime(
    pinned_sha: str,
    *,
    runtime_store: pathlib.Path = RUNTIME_STORE,
    helper_api: int = CONTROL_HELPER_API,
    runtime_api: int = PROFILE_RUNTIME_API,
    expected_uid: int = 0,
) -> pathlib.Path:
    """Return the verified path selected by protected profile metadata.

    Callers must pass the authoritative profile pin, never checkout ``HEAD`` or
    request-supplied runtime paths. This function verifies before returning it.
    """
    pinned_sha = _valid_sha(pinned_sha, field="pinned_sha")
    if not isinstance(runtime_store, pathlib.Path) or not runtime_store.is_absolute():
        raise Refused("runtime store must be a fixed absolute path")
    if ".." in runtime_store.parts:
        raise Refused("runtime store contains traversal components")
    runtime_directory = runtime_store / pinned_sha
    verify_runtime_installation(
        runtime_directory,
        pinned_sha=pinned_sha,
        helper_api=helper_api,
        runtime_api=runtime_api,
        expected_uid=expected_uid,
    )
    return runtime_directory
