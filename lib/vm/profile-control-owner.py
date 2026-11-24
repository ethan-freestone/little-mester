#!/usr/bin/env python3
"""Read-only sudo-owner enrollment and authorization gate for VM profiles.

This module does not create enrollment state, install sudoers rules, dispatch
requests, or perform profile/runtime operations.  The fixed helper must call
``authorize_owner`` before reading profile metadata or dispatching an installer.
"""

from __future__ import annotations

import dataclasses
import grp
import json
import os
import pwd
import re
import stat
from collections.abc import Callable, Iterable


ENROLLMENT_FILE = "/etc/little-mester-vm/control-owner.json"
CONTROL_GROUP = "lm-vm-profile-control"
SCHEMA_VERSION = 1
MAX_ENROLLMENT_BYTES = 256
ENROLLMENT_DIRECTORY_MODE = 0o750
ENROLLMENT_FILE_MODE = 0o600

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
_CANONICAL_UID = re.compile(r"^[1-9][0-9]*$")


class OwnerRefused(RuntimeError):
    """The invoking sudo owner or protected enrollment state is unsafe."""


@dataclasses.dataclass(frozen=True)
class OwnerIdentity:
    """Resolved installer ownership and the dedicated profile-control group."""

    uid: int
    primary_gid: int
    account_name: str
    control_group_gid: int


def _duplicate_rejecting_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise OwnerRefused("owner enrollment contains a duplicate JSON key")
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> None:
    raise OwnerRefused("owner enrollment contains a non-standard JSON value")


def _identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _stat_state(info: os.stat_result) -> tuple[int, ...]:
    """Fields that must remain stable across descriptor-relative reads."""
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
        info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
    )


def _components(path: str) -> list[str]:
    if (not isinstance(path, str) or not path.startswith("/") or path == "/"
            or path.endswith("/") or "\x00" in path or path.startswith("//")):
        raise OwnerRefused("owner enrollment path is invalid")
    components = path[1:].split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise OwnerRefused("owner enrollment path is not canonical")
    return components


def _check_ancestor(info: os.stat_result, *, allowed_uids: frozenset[int]) -> None:
    mode = info.st_mode
    if not stat.S_ISDIR(mode) or info.st_uid not in allowed_uids:
        raise OwnerRefused("owner enrollment path has unsafe ancestry")
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        # The sticky, root-owned /tmp ancestor is permitted only to support the
        # private fixture seam; the production path contains no such component.
        if not (info.st_uid == 0 and mode & stat.S_ISVTX):
            raise OwnerRefused("owner enrollment path has writable ancestry")


def _check_enrollment_directory(
    info: os.stat_result, *, owner_uid: int, group_gid: int, mode: int,
) -> None:
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid
            or info.st_gid != group_gid or stat.S_IMODE(info.st_mode) != mode):
        raise OwnerRefused("owner enrollment directory has unsafe type, owner, group, or mode")


def _open_parent(
    path: str,
    *,
    owner_uid: int,
    group_gid: int,
    directory_mode: int,
    allowed_ancestor_uids: frozenset[int],
) -> tuple[int, str]:
    components = _components(path)
    current_fd = -1
    try:
        current_fd = os.open("/", _DIRECTORY_FLAGS)
        os.set_inheritable(current_fd, False)
        root_info = os.fstat(current_fd)
        _check_ancestor(root_info, allowed_uids=allowed_ancestor_uids)

        for index, component in enumerate(components[:-1]):
            child_fd = -1
            is_enrollment_directory = index == len(components) - 2
            try:
                before = os.stat(component, dir_fd=current_fd, follow_symlinks=False)
                if is_enrollment_directory:
                    _check_enrollment_directory(
                        before, owner_uid=owner_uid, group_gid=group_gid,
                        mode=directory_mode,
                    )
                else:
                    _check_ancestor(before, allowed_uids=allowed_ancestor_uids)

                child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
                os.set_inheritable(child_fd, False)
                opened = os.fstat(child_fd)
                after = os.stat(component, dir_fd=current_fd, follow_symlinks=False)
                if is_enrollment_directory:
                    for item in (opened, after):
                        _check_enrollment_directory(
                            item, owner_uid=owner_uid, group_gid=group_gid,
                            mode=directory_mode,
                        )
                else:
                    _check_ancestor(opened, allowed_uids=allowed_ancestor_uids)
                    _check_ancestor(after, allowed_uids=allowed_ancestor_uids)
                if not (_identity(before, opened) and _identity(opened, after)
                        and _stat_state(before) == _stat_state(opened)
                        and _stat_state(opened) == _stat_state(after)):
                    raise OwnerRefused("owner enrollment ancestry changed during open")
            except OwnerRefused:
                if child_fd >= 0:
                    os.close(child_fd)
                raise
            except OSError as error:
                if child_fd >= 0:
                    os.close(child_fd)
                raise OwnerRefused("owner enrollment ancestry is missing or unsafe") from error
            os.close(current_fd)
            current_fd = child_fd

        parent_fd = current_fd
        current_fd = -1
        return parent_fd, components[-1]
    except OwnerRefused:
        if current_fd >= 0:
            os.close(current_fd)
        raise
    except OSError as error:
        if current_fd >= 0:
            os.close(current_fd)
        raise OwnerRefused("owner enrollment ancestry is missing or unsafe") from error


def _check_enrollment_file(info: os.stat_result, *, owner_uid: int, owner_gid: int) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid
            or info.st_gid != owner_gid
            or stat.S_IMODE(info.st_mode) != ENROLLMENT_FILE_MODE
            or info.st_nlink != 1 or info.st_size > MAX_ENROLLMENT_BYTES):
        raise OwnerRefused("owner enrollment file has unsafe type, owner, mode, or link count")


def _read_enrollment(
    path: str,
    *,
    owner_uid: int,
    directory_gid: int,
    file_gid: int,
    directory_mode: int,
    allowed_ancestor_uids: frozenset[int],
) -> bytes:
    parent_fd, name = _open_parent(
        path,
        owner_uid=owner_uid,
        group_gid=directory_gid,
        directory_mode=directory_mode,
        allowed_ancestor_uids=allowed_ancestor_uids,
    )
    file_fd = -1
    try:
        parent_before = os.fstat(parent_fd)
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _check_enrollment_file(before, owner_uid=owner_uid, owner_gid=file_gid)
        file_fd = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
        os.set_inheritable(file_fd, False)
        opened = os.fstat(file_fd)
        after_open = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        for item in (opened, after_open):
            _check_enrollment_file(item, owner_uid=owner_uid, owner_gid=file_gid)
        if not (_identity(before, opened) and _identity(opened, after_open)
                and _stat_state(before) == _stat_state(opened)
                and _stat_state(opened) == _stat_state(after_open)):
            raise OwnerRefused("owner enrollment file changed during open")

        chunks: list[bytes] = []
        remaining = MAX_ENROLLMENT_BYTES + 1
        while remaining:
            chunk = os.read(file_fd, min(remaining, 64))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)

        after_read = os.fstat(file_fd)
        path_after_read = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        for item in (after_read, path_after_read):
            _check_enrollment_file(item, owner_uid=owner_uid, owner_gid=file_gid)
        if not (_identity(opened, after_read) and _identity(after_read, path_after_read)
                and _stat_state(opened) == _stat_state(after_read)
                and _stat_state(after_read) == _stat_state(path_after_read)
                and len(raw) == after_read.st_size):
            raise OwnerRefused("owner enrollment file changed during read")

        rechecked_parent_fd, rechecked_name = _open_parent(
            path,
            owner_uid=owner_uid,
            group_gid=directory_gid,
            directory_mode=directory_mode,
            allowed_ancestor_uids=allowed_ancestor_uids,
        )
        try:
            parent_after = os.fstat(parent_fd)
            rechecked_parent = os.fstat(rechecked_parent_fd)
            if (name != rechecked_name or not _identity(parent_before, parent_after)
                    or not _identity(parent_after, rechecked_parent)
                    or _stat_state(parent_before) != _stat_state(parent_after)
                    or _stat_state(parent_after) != _stat_state(rechecked_parent)):
                raise OwnerRefused("owner enrollment ancestry changed during read")
        finally:
            os.close(rechecked_parent_fd)

        if len(raw) > MAX_ENROLLMENT_BYTES:
            raise OwnerRefused("owner enrollment file exceeds the size bound")
        return raw
    except OwnerRefused:
        raise
    except OSError as error:
        raise OwnerRefused("owner enrollment file is missing or unsafe") from error
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(parent_fd)


def _parse_enrollment(raw: bytes) -> int:
    if not raw or len(raw) > MAX_ENROLLMENT_BYTES:
        raise OwnerRefused("owner enrollment file is empty or exceeds the size bound")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_json_constant,
        )
    except OwnerRefused:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise OwnerRefused("owner enrollment is not valid bounded UTF-8 JSON") from error
    if type(value) is not dict or set(value) != {"schema_version", "owner_uid"}:
        raise OwnerRefused("owner enrollment does not match the exact schema")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise OwnerRefused("owner enrollment schema version is unsupported")
    owner_uid = value["owner_uid"]
    if type(owner_uid) is not int or owner_uid <= 0:
        raise OwnerRefused("owner enrollment UID must be a positive integer")
    return owner_uid


def _check_invocation(
    *,
    real_uid: int,
    effective_uid: int,
    sudo_uid: object,
    sudo_user: object,
) -> tuple[int, str]:
    if (type(real_uid) is not int or type(effective_uid) is not int
            or effective_uid != 0 or real_uid <= 0):
        raise OwnerRefused("owner authorization requires a root sudo invocation")
    if not isinstance(sudo_uid, str) or not _CANONICAL_UID.fullmatch(sudo_uid):
        raise OwnerRefused("SUDO_UID is absent or not a canonical positive UID")
    uid = int(sudo_uid)
    if uid != real_uid:
        raise OwnerRefused("SUDO_UID conflicts with the real invoking UID")
    if (not isinstance(sudo_user, str) or not sudo_user
            or sudo_user != sudo_user.strip()
            or any(ord(char) < 0x21 or ord(char) == 0x7f for char in sudo_user)):
        raise OwnerRefused("SUDO_USER is absent or invalid")
    return uid, sudo_user


def _field(account: object, name: str) -> object:
    try:
        return getattr(account, name)
    except (AttributeError, TypeError) as error:
        raise OwnerRefused("account database returned an invalid record") from error


def _resolve_account(
    uid: int,
    account_name: str,
    *,
    account_by_name: Callable[[str], object],
    account_by_uid: Callable[[int], object],
) -> tuple[int, int, str]:
    try:
        by_name = account_by_name(account_name)
        by_uid = account_by_uid(uid)
    except Exception as error:
        raise OwnerRefused("sudo owner does not resolve consistently in the account database") from error

    name_uid = _field(by_name, "pw_uid")
    name_name = _field(by_name, "pw_name")
    primary_gid = _field(by_name, "pw_gid")
    uid_uid = _field(by_uid, "pw_uid")
    uid_name = _field(by_uid, "pw_name")
    if (type(name_uid) is not int or name_uid != uid or type(uid_uid) is not int
            or uid_uid != uid or name_name != account_name or uid_name != account_name
            or type(primary_gid) is not int or primary_gid < 0 or uid == 0):
        raise OwnerRefused("SUDO_UID and SUDO_USER do not identify the same non-root account")
    return uid, primary_gid, account_name


def _resolve_control_group(
    *,
    account_name: str,
    primary_gid: int,
    group_by_name: Callable[[str], object],
    all_groups: Callable[[], Iterable[object]],
) -> int:
    try:
        control_group = group_by_name(CONTROL_GROUP)
        entries = tuple(all_groups())
    except Exception as error:
        raise OwnerRefused("profile-control group lookup failed") from error

    control_gid = _field(control_group, "gr_gid")
    control_name = _field(control_group, "gr_name")
    if type(control_gid) is not int or control_gid <= 0 or control_name != CONTROL_GROUP:
        raise OwnerRefused("profile-control group database entry is invalid")

    matching_entry = False
    supplementary_member = False
    try:
        for entry in entries:
            if _field(entry, "gr_name") == CONTROL_GROUP:
                entry_gid = _field(entry, "gr_gid")
                if type(entry_gid) is not int or entry_gid != control_gid or matching_entry:
                    raise OwnerRefused("profile-control group database is inconsistent")
                members = _field(entry, "gr_mem")
                if not isinstance(members, (list, tuple)):
                    raise OwnerRefused("profile-control group membership data is invalid")
                matching_entry = True
                supplementary_member = account_name in members
    except OwnerRefused:
        raise
    except (TypeError, ValueError) as error:
        raise OwnerRefused("profile-control group membership data is invalid") from error

    if not matching_entry:
        raise OwnerRefused("profile-control group is absent from the group database")
    if primary_gid != control_gid and not supplementary_member:
        raise OwnerRefused("sudo owner is not a profile-control group member")
    return control_gid


def _authorize_owner(
    *,
    control_path: str,
    enrollment_owner_uid: int,
    enrollment_file_gid: int,
    directory_mode: int,
    allowed_ancestor_uids: frozenset[int],
    real_uid: int,
    effective_uid: int,
    sudo_uid: object,
    sudo_user: object,
    account_by_name: Callable[[str], object],
    account_by_uid: Callable[[int], object],
    group_by_name: Callable[[str], object],
    all_groups: Callable[[], Iterable[object]],
) -> OwnerIdentity:
    uid, account_name = _check_invocation(
        real_uid=real_uid,
        effective_uid=effective_uid,
        sudo_uid=sudo_uid,
        sudo_user=sudo_user,
    )
    resolved_uid, primary_gid, resolved_name = _resolve_account(
        uid,
        account_name,
        account_by_name=account_by_name,
        account_by_uid=account_by_uid,
    )
    control_group_gid = _resolve_control_group(
        account_name=resolved_name,
        primary_gid=primary_gid,
        group_by_name=group_by_name,
        all_groups=all_groups,
    )
    enrolled_uid = _parse_enrollment(_read_enrollment(
        control_path,
        owner_uid=enrollment_owner_uid,
        directory_gid=control_group_gid,
        file_gid=enrollment_file_gid,
        directory_mode=directory_mode,
        allowed_ancestor_uids=allowed_ancestor_uids,
    ))
    if enrolled_uid != resolved_uid:
        raise OwnerRefused("sudo owner does not match the enrolled UID")
    return OwnerIdentity(
        uid=resolved_uid,
        primary_gid=primary_gid,
        account_name=resolved_name,
        control_group_gid=control_group_gid,
    )


def authorize_owner() -> OwnerIdentity:
    """Authorize and return the enrolled sudo caller using fixed host inputs.

    No path, UID, account, group, or request value is accepted from the caller.
    The SUDO_* environment values are interpreted only as the conventional
    sudo invocation context; they are not cryptographically authenticated.
    """
    return _authorize_owner(
        control_path=ENROLLMENT_FILE,
        enrollment_owner_uid=0,
        enrollment_file_gid=0,
        directory_mode=ENROLLMENT_DIRECTORY_MODE,
        allowed_ancestor_uids=frozenset({0}),
        real_uid=os.getuid(),
        effective_uid=os.geteuid(),
        sudo_uid=os.environ.get("SUDO_UID"),
        sudo_user=os.environ.get("SUDO_USER"),
        account_by_name=pwd.getpwnam,
        account_by_uid=pwd.getpwuid,
        group_by_name=grp.getgrnam,
        all_groups=grp.getgrall,
    )


def _test_authorize_owner(
    *,
    control_path: str,
    enrollment_owner_uid: int,
    enrollment_file_gid: int,
    directory_mode: int,
    invocation: tuple[int, int, object, object],
    account_by_name: Callable[[str], object],
    account_by_uid: Callable[[int], object],
    group_by_name: Callable[[str], object],
    all_groups: Callable[[], Iterable[object]],
    allowed_ancestor_uids: frozenset[int] | None = None,
) -> OwnerIdentity:
    """Private fixture seam; never used by the production public interface."""
    real_uid, effective_uid, sudo_uid, sudo_user = invocation
    return _authorize_owner(
        control_path=control_path,
        enrollment_owner_uid=enrollment_owner_uid,
        enrollment_file_gid=enrollment_file_gid,
        directory_mode=directory_mode,
        allowed_ancestor_uids=(
            frozenset({0, enrollment_owner_uid})
            if allowed_ancestor_uids is None else allowed_ancestor_uids
        ),
        real_uid=real_uid,
        effective_uid=effective_uid,
        sudo_uid=sudo_uid,
        sudo_user=sudo_user,
        account_by_name=account_by_name,
        account_by_uid=account_by_uid,
        group_by_name=group_by_name,
        all_groups=all_groups,
    )
