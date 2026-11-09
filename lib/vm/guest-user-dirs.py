#!/usr/bin/env python3
"""Prepare only the fixed guest user's rootless Docker configuration dirs."""

from __future__ import annotations

import argparse
import os
import pwd
import secrets
import stat
import sys


DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
USER_DIRS = (
    (".config",),
    (".config", "systemd"),
    (".config", "systemd", "user"),
    (".config", "systemd", "user", "docker.service.d"),
    (".config", "docker"),
    (".local",),
    (".local", "share"),
    (".local", "share", "opencode"),
)


class Refusal(Exception):
    pass


def _protected_directory(info: os.stat_result, *, label: str) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise Refusal(f"{label} is not a directory")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise Refusal(f"{label} is group/world-writable")


def open_guest_home(uid: int, gid: int) -> int:
    """Open the fixed /home/mester directory without following path links."""
    if not os.path.isabs("/home/mester"):
        raise Refusal("guest home path is not absolute")
    root_fd = os.open("/", DIR_FLAGS)
    home_parent_fd = -1
    try:
        home_parent_fd = os.open("home", DIR_FLAGS, dir_fd=root_fd)
        parent_info = os.fstat(home_parent_fd)
        _protected_directory(parent_info, label="/home")
        if parent_info.st_uid != 0:
            raise Refusal("/home is not root-owned")
        try:
            before = os.stat("mester", dir_fd=home_parent_fd, follow_symlinks=False)
        except OSError as error:
            raise Refusal("/home/mester is missing or cannot be inspected") from error
        _protected_directory(before, label="/home/mester")
        if before.st_uid != uid or before.st_gid != gid:
            raise Refusal(
                f"/home/mester ownership is {before.st_uid}:{before.st_gid}; "
                f"expected {uid}:{gid}; refusing broad or recursive ownership repair"
            )
        try:
            home_fd = os.open("mester", DIR_FLAGS, dir_fd=home_parent_fd)
        except OSError as error:
            raise Refusal("/home/mester is unsafe or contains a symlink") from error
        opened = os.fstat(home_fd)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            os.close(home_fd)
            raise Refusal("/home/mester changed while it was opened")
        os.fchmod(home_fd, 0o700)
        return home_fd
    finally:
        os.close(root_fd)
        if home_parent_fd >= 0:
            os.close(home_parent_fd)


def _open_or_create_owned_dir(parent_fd: int, name: str, uid: int, gid: int) -> int:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            # Another actor won the create race; validate the object below.
            pass
        except OSError as error:
            raise Refusal(f"could not create guest configuration directory {name}") from error
        try:
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise Refusal(f"new guest configuration directory {name} disappeared") from error
    except OSError as error:
        raise Refusal(f"could not inspect guest configuration directory {name}") from error

    _protected_directory(before, label=f"guest configuration component {name}")
    if before.st_uid not in (0, uid) or before.st_gid not in (0, gid):
        raise Refusal(f"guest configuration component {name} has unexpected ownership")
    try:
        child_fd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        raise Refusal(f"guest configuration component {name} is unsafe or a symlink") from error
    try:
        opened = os.fstat(child_fd)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise Refusal(f"guest configuration component {name} changed while opening")
        if (opened.st_uid, opened.st_gid) != (uid, gid):
            # Normalize this one verified directory only; never recurse into it.
            os.fchown(child_fd, uid, gid)
        os.fchmod(child_fd, 0o700)
        return child_fd
    except BaseException:
        os.close(child_fd)
        raise


def prepare_config_dirs(home_fd: int, uid: int, gid: int) -> None:
    home_info = os.fstat(home_fd)
    _protected_directory(home_info, label="/home/mester")
    if (home_info.st_uid, home_info.st_gid) != (uid, gid):
        raise Refusal("/home/mester ownership changed during provisioning")
    os.fchmod(home_fd, 0o700)

    for components in USER_DIRS:
        parent_fd = os.dup(home_fd)
        try:
            for component in components:
                child_fd = _open_or_create_owned_dir(parent_fd, component, uid, gid)
                os.close(parent_fd)
                parent_fd = child_fd
        finally:
            os.close(parent_fd)


def _write_probe(directory_fd: int, uid: int, label: str) -> None:
    for _ in range(8):
        name = f".mester-write-check-{secrets.token_hex(8)}"
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory_fd,
            )
            break
        except FileExistsError:
            continue
    else:
        raise Refusal(f"could not allocate a unique write check in {label}")
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise Refusal(f"guest user write check did not create a private regular file in {label}")
        os.write(descriptor, b"rootless-user-write-check\n")
    finally:
        os.close(descriptor)
        os.unlink(name, dir_fd=directory_fd)


def provision(uid: int) -> None:
    if os.geteuid() != 0:
        raise Refusal("guest configuration directories must be prepared by root")
    try:
        account = pwd.getpwnam("mester")
    except KeyError as error:
        raise Refusal("guest account mester does not exist") from error
    if account.pw_uid != uid or account.pw_dir != "/home/mester":
        raise Refusal("guest mester account UID/home does not match cloud-init input")
    home_fd = open_guest_home(uid, account.pw_gid)
    try:
        prepare_config_dirs(home_fd, uid, account.pw_gid)
    finally:
        os.close(home_fd)


def verify_user_write_at(home_fd: int, uid: int, gid: int) -> None:
    home_info = os.fstat(home_fd)
    if (home_info.st_uid, home_info.st_gid) != (uid, gid):
        raise Refusal("/home/mester is not owned by guest user mester")
    if stat.S_IMODE(home_info.st_mode) != 0o700:
        raise Refusal("/home/mester is not private mode 0700")
    if os.geteuid() != uid:
        raise Refusal("write check must run as guest user mester")
    _write_probe(home_fd, uid, "/home/mester")
    user_dir_fd = os.open(".config", DIR_FLAGS, dir_fd=home_fd)
    try:
        systemd_fd = os.open("systemd", DIR_FLAGS, dir_fd=user_dir_fd)
        try:
            target_fd = os.open("user", DIR_FLAGS, dir_fd=systemd_fd)
            try:
                directory_info = os.fstat(target_fd)
                if (directory_info.st_uid, directory_info.st_gid) != (uid, gid):
                    raise Refusal("systemd/user is not owned by guest user mester")
                if stat.S_IMODE(directory_info.st_mode) != 0o700:
                    raise Refusal("systemd/user is not private mode 0700")
                _write_probe(target_fd, uid, "systemd/user")
            finally:
                os.close(target_fd)
        finally:
            os.close(systemd_fd)
    finally:
        os.close(user_dir_fd)


def verify_user_write() -> None:
    account = pwd.getpwnam("mester")
    if os.geteuid() != account.pw_uid or account.pw_dir != "/home/mester":
        raise Refusal("write check must run as guest user mester")
    home_fd = open_guest_home(account.pw_uid, account.pw_gid)
    try:
        verify_user_write_at(home_fd, account.pw_uid, account.pw_gid)
    finally:
        os.close(home_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("uid", type=int)
    commands.add_parser("verify-write")
    args = parser.parse_args(argv)
    try:
        if args.operation == "prepare":
            provision(args.uid)
        else:
            verify_user_write()
    except (OSError, Refusal) as error:
        print(f"guest rootless configuration refused: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
