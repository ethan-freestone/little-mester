#!/usr/bin/env python3
"""Protected, nonblocking maintenance lease substrate for VM control.

This is a small filesystem/lock primitive, not an authorization boundary. The
fixed bootstrap or fixed-system installer may call
``ensure_maintenance_substrate`` only after its own explicit authorization.
Normal control dispatch must use ``maintenance_lease``; it never creates or
repairs the substrate.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import stat
from collections.abc import Iterator


CONTROL_DIRECTORY = "/run/little-mester-vm-control"
MAINTENANCE_LOCK = "maintenance.lock"
OWNER_UID = 0
OWNER_GID = 0
DIRECTORY_MODE = 0o755
LOCK_MODE = 0o600

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_LOCK_FLAGS = os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC


class Refused(RuntimeError):
    """The substrate is missing, unsafe, or changed during validation."""


class Busy(RuntimeError):
    """A conflicting maintenance lease is already held."""


def _components(path: str | os.PathLike[str]) -> list[str]:
    try:
        value = os.fspath(path)
    except TypeError as error:
        raise Refused("maintenance directory path is invalid") from error
    if (not isinstance(value, str) or not value.startswith("/") or value == "/"
            or value.endswith("/") or "\x00" in value or value.startswith("//")):
        raise Refused("maintenance directory path is invalid")
    result = value[1:].split("/")
    if any(component in {"", ".", ".."} for component in result):
        raise Refused("maintenance directory path is not canonical")
    return result


def _identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _check_ancestor(info: os.stat_result, allowed_uids: frozenset[int]) -> None:
    mode = info.st_mode
    if (not stat.S_ISDIR(mode) or info.st_uid not in allowed_uids
            or (mode & (stat.S_IWGRP | stat.S_IWOTH) and not mode & stat.S_ISVTX)):
        raise Refused("maintenance path has unsafe ancestry")


def _open_parent(path: str | os.PathLike[str], *, ancestor_uids: frozenset[int]) -> tuple[int, str]:
    components = _components(path)
    current_fd = -1
    try:
        current_fd = os.open("/", _DIRECTORY_FLAGS)
        os.set_inheritable(current_fd, False)
        root_info = os.fstat(current_fd)
        _check_ancestor(root_info, ancestor_uids)
        for component in components[:-1]:
            child_fd = -1
            try:
                before = os.stat(component, dir_fd=current_fd, follow_symlinks=False)
                _check_ancestor(before, ancestor_uids)
                child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
                os.set_inheritable(child_fd, False)
                opened = os.fstat(child_fd)
                after = os.stat(component, dir_fd=current_fd, follow_symlinks=False)
                _check_ancestor(opened, ancestor_uids)
                if not _identity(before, opened) or not _identity(opened, after):
                    raise Refused("maintenance path ancestry changed during open")
            except Refused:
                if child_fd >= 0:
                    os.close(child_fd)
                raise
            except OSError as error:
                if child_fd >= 0:
                    os.close(child_fd)
                raise Refused("maintenance path ancestry is missing or unsafe") from error
            os.close(current_fd)
            current_fd = child_fd
        parent_fd = current_fd
        current_fd = -1
        return parent_fd, components[-1]
    except BaseException:
        if current_fd >= 0:
            os.close(current_fd)
        raise


def _check_directory(info: os.stat_result, *, owner_uid: int, owner_gid: int) -> None:
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid
            or info.st_gid != owner_gid or stat.S_IMODE(info.st_mode) != DIRECTORY_MODE):
        raise Refused("maintenance directory has unsafe type, owner, group, or mode")


def _open_directory(parent_fd: int, name: str, *, owner_uid: int, owner_gid: int) -> int:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _check_directory(before, owner_uid=owner_uid, owner_gid=owner_gid)
        directory_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        os.set_inheritable(directory_fd, False)
        opened = os.fstat(directory_fd)
        after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _check_directory(opened, owner_uid=owner_uid, owner_gid=owner_gid)
        _check_directory(after, owner_uid=owner_uid, owner_gid=owner_gid)
        if not _identity(before, opened) or not _identity(opened, after):
            raise Refused("maintenance directory changed during open")
        return directory_fd
    except Refused:
        if "directory_fd" in locals():
            os.close(directory_fd)
        raise
    except OSError as error:
        if "directory_fd" in locals():
            os.close(directory_fd)
        raise Refused("maintenance directory is missing or unsafe") from error


def _check_lock(info: os.stat_result, *, owner_uid: int, owner_gid: int) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid
            or info.st_gid != owner_gid or stat.S_IMODE(info.st_mode) != LOCK_MODE
            or info.st_nlink != 1):
        raise Refused("maintenance lock has unsafe type, owner, group, mode, or link count")


def _open_lock(directory_fd: int, *, owner_uid: int, owner_gid: int) -> int:
    try:
        before = os.stat(MAINTENANCE_LOCK, dir_fd=directory_fd, follow_symlinks=False)
        _check_lock(before, owner_uid=owner_uid, owner_gid=owner_gid)
        lock_fd = os.open(MAINTENANCE_LOCK, _LOCK_FLAGS, dir_fd=directory_fd)
        os.set_inheritable(lock_fd, False)
        opened = os.fstat(lock_fd)
        after = os.stat(MAINTENANCE_LOCK, dir_fd=directory_fd, follow_symlinks=False)
        _check_lock(opened, owner_uid=owner_uid, owner_gid=owner_gid)
        _check_lock(after, owner_uid=owner_uid, owner_gid=owner_gid)
        if not _identity(before, opened) or not _identity(opened, after):
            raise Refused("maintenance lock changed during open")
        return lock_fd
    except Refused:
        if "lock_fd" in locals():
            os.close(lock_fd)
        raise
    except OSError as error:
        if "lock_fd" in locals():
            os.close(lock_fd)
        raise Refused("maintenance lock is missing or unsafe") from error


def _validate_bindings(
    path: str | os.PathLike[str],
    parent_fd: int,
    directory_fd: int,
    lock_fd: int | None,
    *,
    owner_uid: int,
    owner_gid: int,
    ancestor_uids: frozenset[int],
) -> None:
    check_parent_fd, name = _open_parent(path, ancestor_uids=ancestor_uids)
    try:
        if not _identity(os.fstat(parent_fd), os.fstat(check_parent_fd)):
            raise Refused("maintenance directory parent changed")
        directory_path = os.stat(name, dir_fd=check_parent_fd, follow_symlinks=False)
        directory_open = os.fstat(directory_fd)
        _check_directory(directory_path, owner_uid=owner_uid, owner_gid=owner_gid)
        _check_directory(directory_open, owner_uid=owner_uid, owner_gid=owner_gid)
        if not _identity(directory_path, directory_open):
            raise Refused("maintenance directory path no longer names its descriptor")
        if lock_fd is not None:
            lock_path = os.stat(MAINTENANCE_LOCK, dir_fd=directory_fd, follow_symlinks=False)
            lock_open = os.fstat(lock_fd)
            _check_lock(lock_path, owner_uid=owner_uid, owner_gid=owner_gid)
            _check_lock(lock_open, owner_uid=owner_uid, owner_gid=owner_gid)
            if not _identity(lock_path, lock_open):
                raise Refused("maintenance lock path no longer names its descriptor")
    except OSError as error:
        raise Refused("maintenance substrate changed during validation") from error
    finally:
        os.close(check_parent_fd)


def _open_or_create_directory(
    parent_fd: int,
    name: str,
    *,
    owner_uid: int,
    owner_gid: int,
) -> int:
    created = False
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        try:
            os.mkdir(name, DIRECTORY_MODE, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        except OSError as error:
            raise Refused("maintenance directory could not be created") from error
    except OSError as error:
        raise Refused("maintenance directory could not be inspected") from error

    if not created:
        return _open_directory(parent_fd, name, owner_uid=owner_uid, owner_gid=owner_gid)

    directory_fd = -1
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode):
            raise Refused("new maintenance directory has an unexpected type")
        directory_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        os.set_inheritable(directory_fd, False)
        opened = os.fstat(directory_fd)
        after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _identity(before, opened) or not _identity(opened, after):
            raise Refused("new maintenance directory changed during creation")
        os.fchown(directory_fd, owner_uid, owner_gid)
        os.fchmod(directory_fd, DIRECTORY_MODE)
        _check_directory(os.fstat(directory_fd), owner_uid=owner_uid, owner_gid=owner_gid)
        os.fsync(parent_fd)
        return directory_fd
    except Refused:
        if directory_fd >= 0:
            os.close(directory_fd)
        raise
    except OSError as error:
        if directory_fd >= 0:
            os.close(directory_fd)
        raise Refused("new maintenance directory could not be secured") from error


def _open_or_create_lock(directory_fd: int, *, owner_uid: int, owner_gid: int) -> int:
    try:
        lock_fd = os.open(
            MAINTENANCE_LOCK,
            _LOCK_FLAGS | os.O_CREAT | os.O_EXCL,
            LOCK_MODE,
            dir_fd=directory_fd,
        )
    except FileExistsError:
        return _open_lock(directory_fd, owner_uid=owner_uid, owner_gid=owner_gid)
    except OSError as error:
        raise Refused("maintenance lock could not be created") from error

    try:
        os.set_inheritable(lock_fd, False)
        opened = os.fstat(lock_fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise Refused("new maintenance lock has an unexpected identity")
        os.fchown(lock_fd, owner_uid, owner_gid)
        os.fchmod(lock_fd, LOCK_MODE)
        opened = os.fstat(lock_fd)
        path_info = os.stat(MAINTENANCE_LOCK, dir_fd=directory_fd, follow_symlinks=False)
        _check_lock(opened, owner_uid=owner_uid, owner_gid=owner_gid)
        _check_lock(path_info, owner_uid=owner_uid, owner_gid=owner_gid)
        if not _identity(opened, path_info):
            raise Refused("new maintenance lock changed during creation")
        os.fsync(lock_fd)
        os.fsync(directory_fd)
        return lock_fd
    except Refused:
        os.close(lock_fd)
        raise
    except OSError as error:
        os.close(lock_fd)
        raise Refused("new maintenance lock could not be secured") from error


def _ensure_at(
    path: str | os.PathLike[str],
    *,
    owner_uid: int,
    owner_gid: int,
    ancestor_uids: frozenset[int],
) -> None:
    parent_fd, name = _open_parent(path, ancestor_uids=ancestor_uids)
    directory_fd = -1
    lock_fd = -1
    try:
        directory_fd = _open_or_create_directory(
            parent_fd, name, owner_uid=owner_uid, owner_gid=owner_gid,
        )
        lock_fd = _open_or_create_lock(directory_fd, owner_uid=owner_uid, owner_gid=owner_gid)
        _validate_bindings(
            path, parent_fd, directory_fd, lock_fd,
            owner_uid=owner_uid, owner_gid=owner_gid, ancestor_uids=ancestor_uids,
        )
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        if directory_fd >= 0:
            os.close(directory_fd)
        os.close(parent_fd)


@contextlib.contextmanager
def _lease_at(
    path: str | os.PathLike[str],
    *,
    exclusive: bool,
    owner_uid: int,
    owner_gid: int,
    ancestor_uids: frozenset[int],
) -> Iterator[int]:
    if type(exclusive) is not bool:
        raise Refused("maintenance lease mode must be boolean")
    parent_fd, name = _open_parent(path, ancestor_uids=ancestor_uids)
    directory_fd = -1
    lock_fd = -1
    locked = False
    try:
        directory_fd = _open_directory(parent_fd, name, owner_uid=owner_uid, owner_gid=owner_gid)
        lock_fd = _open_lock(directory_fd, owner_uid=owner_uid, owner_gid=owner_gid)
        _validate_bindings(
            path, parent_fd, directory_fd, lock_fd,
            owner_uid=owner_uid, owner_gid=owner_gid, ancestor_uids=ancestor_uids,
        )
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(lock_fd, operation | fcntl.LOCK_NB)
            locked = True
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                raise Busy("maintenance lease is busy") from error
            raise Refused("maintenance lease could not be acquired") from error
        _validate_bindings(
            path, parent_fd, directory_fd, lock_fd,
            owner_uid=owner_uid, owner_gid=owner_gid, ancestor_uids=ancestor_uids,
        )
        yield lock_fd
    finally:
        if lock_fd >= 0:
            if locked:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                os.close(lock_fd)
            except OSError:
                pass
        if directory_fd >= 0:
            os.close(directory_fd)
        os.close(parent_fd)


def ensure_maintenance_substrate() -> None:
    """Create or validate the fixed substrate for authorized bootstrap only.

    This function contains no caller authorization. Its future bootstrap or
    fixed-system-installer caller must establish that authorization before
    invoking it. Existing objects are validated, never repaired or replaced.
    """
    _ensure_at(
        CONTROL_DIRECTORY,
        owner_uid=OWNER_UID,
        owner_gid=OWNER_GID,
        ancestor_uids=frozenset({OWNER_UID}),
    )


@contextlib.contextmanager
def maintenance_lease(*, exclusive: bool) -> Iterator[int]:
    """Yield the held fixed maintenance-lock descriptor without waiting.

    Missing or unsafe substrate raises ``Refused``. A conflicting flock raises
    ``Busy``. The lease descriptor is close-on-exec and always unlocked/closed.
    """
    with _lease_at(
        CONTROL_DIRECTORY,
        exclusive=exclusive,
        owner_uid=OWNER_UID,
        owner_gid=OWNER_GID,
        ancestor_uids=frozenset({OWNER_UID}),
    ) as lock_fd:
        yield lock_fd


def _test_ensure_substrate(
    path: str | os.PathLike[str], *, owner_uid: int, owner_gid: int,
) -> None:
    """Private fixture seam; production callers must use the fixed API above."""
    _ensure_at(
        path,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        ancestor_uids=frozenset({OWNER_UID, owner_uid}),
    )


@contextlib.contextmanager
def _test_maintenance_lease(
    path: str | os.PathLike[str],
    *,
    exclusive: bool,
    owner_uid: int,
    owner_gid: int,
) -> Iterator[int]:
    """Private fixture seam; its path/ownership values are never requests."""
    with _lease_at(
        path,
        exclusive=exclusive,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        ancestor_uids=frozenset({OWNER_UID, owner_uid}),
    ) as lock_fd:
        yield lock_fd
