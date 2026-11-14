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
PROXY_UNIT_CONFIG = (
    b"[Service]\n"
    b"Environment=HTTP_PROXY=http://10.0.2.100:3128\n"
    b"Environment=HTTPS_PROXY=http://10.0.2.100:3128\n"
    b"Environment=NO_PROXY=localhost,127.0.0.1,10.0.2.100\n"
    b"Environment=DOCKERD_ROOTLESS_ROOTLESSKIT_FLAGS=--cidr=10.0.3.0/24\n"
)
DOCKER_DAEMON_CONFIG = (
    b'{"proxies":{"http-proxy":"http://10.0.2.100:3128",'
    b'"https-proxy":"http://10.0.2.100:3128",'
    b'"no-proxy":"localhost,127.0.0.1,10.0.2.100"}}\n'
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


def _open_directory_chain(home_fd: int, components: tuple[str, ...]) -> int:
    parent_fd = os.dup(home_fd)
    try:
        for component in components:
            child_fd = os.open(component, DIR_FLAGS, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child_fd
        result_fd = parent_fd
        parent_fd = -1
        return result_fd
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


def _create_private_config_file(
    directory_fd: int, name: str, contents: bytes, uid: int, gid: int
) -> None:
    directory_info = os.fstat(directory_fd)
    if (directory_info.st_uid, directory_info.st_gid) != (uid, gid):
        raise Refusal(f"configuration directory for {name} is not owned by guest user mester")
    if stat.S_IMODE(directory_info.st_mode) != 0o700:
        raise Refusal(f"configuration directory for {name} is not private mode 0700")
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory_fd,
        )
    except FileExistsError as error:
        raise Refusal(f"refusing to replace existing guest configuration file {name}") from error
    except OSError as error:
        raise Refusal(f"could not safely create guest configuration file {name}") from error
    try:
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, 0o600)
        remaining = memoryview(contents)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write creating guest configuration")
            remaining = remaining[written:]
    except BaseException:
        os.close(descriptor)
        os.unlink(name, dir_fd=directory_fd)
        raise
    else:
        os.close(descriptor)


def prepare_rootless_config(home_fd: int, uid: int, gid: int) -> None:
    prepare_config_dirs(home_fd, uid, gid)
    unit_dir_fd = _open_directory_chain(home_fd, (".config", "systemd", "user", "docker.service.d"))
    try:
        _create_private_config_file(
            unit_dir_fd, "80-mester-proxy.conf", PROXY_UNIT_CONFIG, uid, gid
        )
    finally:
        os.close(unit_dir_fd)
    docker_dir_fd = _open_directory_chain(home_fd, (".config", "docker"))
    try:
        _create_private_config_file(
            docker_dir_fd, "daemon.json", DOCKER_DAEMON_CONFIG, uid, gid
        )
    finally:
        os.close(docker_dir_fd)


def prepare_auth_store_at(home_fd: int, uid: int, gid: int) -> None:
    """Privatize the fixed OpenCode credential store without rewriting data."""
    if os.geteuid() != uid or os.getegid() != gid:
        raise Refusal("credential privacy repair must run as guest user mester")
    home_info = os.fstat(home_fd)
    if (home_info.st_uid, home_info.st_gid) != (uid, gid):
        raise Refusal("/home/mester ownership is unsafe for credential privacy repair")

    parent_fd = os.dup(home_fd)
    try:
        for component in (".local", "share", "opencode"):
            try:
                before = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                before = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as error:
                raise Refusal(f"credential directory {component} cannot be inspected") from error
            if (not stat.S_ISDIR(before.st_mode)
                    or (before.st_uid, before.st_gid) != (uid, gid)):
                    raise Refusal(f"credential directory {component} has unsafe type or ownership")
            try:
                child_fd = os.open(component, DIR_FLAGS, dir_fd=parent_fd)
            except OSError as error:
                raise Refusal(f"credential directory {component} is missing or unsafe") from error
            try:
                opened = os.fstat(child_fd)
                if ((before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                        or (opened.st_uid, opened.st_gid) != (uid, gid)):
                    raise Refusal(f"credential directory {component} changed while opening")
                os.fchmod(child_fd, 0o700)
            except BaseException:
                os.close(child_fd)
                raise
            os.close(parent_fd)
            parent_fd = child_fd

        try:
            tmux_info = os.stat("tmux", dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            try:
                os.mkdir("tmux", 0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            tmux_info = os.stat("tmux", dir_fd=parent_fd, follow_symlinks=False)
        if (not stat.S_ISDIR(tmux_info.st_mode)
                or (tmux_info.st_uid, tmux_info.st_gid) != (uid, gid)):
            raise Refusal("private tmux socket directory has unsafe type or ownership")
        tmux_fd = os.open("tmux", DIR_FLAGS, dir_fd=parent_fd)
        tmux_opened = os.fstat(tmux_fd)
        if ((tmux_info.st_dev, tmux_info.st_ino) != (tmux_opened.st_dev, tmux_opened.st_ino)
                or (tmux_opened.st_uid, tmux_opened.st_gid) != (uid, gid)):
            os.close(tmux_fd)
            raise Refusal("private tmux socket directory changed while opening")
        os.fchmod(tmux_fd, 0o700)
        os.close(tmux_fd)

        # OpenCode 2.2.5 stores provider credentials in its SQLite data DB;
        # auth.json remains a legacy migration input. Tighten the DB and all
        # SQLite sidecars without parsing, truncating, or rewriting bytes.
        for name in ("opencode.db", "opencode.db-wal", "opencode.db-shm",
                     "opencode.db-journal", "auth.json"):
            try:
                credential_fd = os.open(
                    name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                continue
            except OSError as error:
                raise Refusal(f"OpenCode credential data file is unsafe: {name}") from error
            try:
                info = os.fstat(credential_fd)
                if (not stat.S_ISREG(info.st_mode) or (info.st_uid, info.st_gid) != (uid, gid)
                        or info.st_nlink != 1):
                    raise Refusal(f"OpenCode credential data file has unsafe type, ownership, or link count: {name}")
                os.fchmod(credential_fd, 0o600)
            finally:
                os.close(credential_fd)
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
        prepare_rootless_config(home_fd, uid, account.pw_gid)
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


def prepare_auth_store() -> None:
    account = pwd.getpwnam("mester")
    if (os.geteuid(), os.getegid(), account.pw_dir) != (
            account.pw_uid, account.pw_gid, "/home/mester"):
        raise Refusal("credential privacy repair must run as guest user mester")
    home_fd = open_guest_home(account.pw_uid, account.pw_gid)
    try:
        prepare_auth_store_at(home_fd, account.pw_uid, account.pw_gid)
    finally:
        os.close(home_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("uid", type=int)
    commands.add_parser("verify-write")
    commands.add_parser("prepare-auth")
    args = parser.parse_args(argv)
    try:
        if args.operation == "prepare":
            provision(args.uid)
        elif args.operation == "verify-write":
            verify_user_write()
        else:
            prepare_auth_store()
    except (OSError, Refusal) as error:
        print(f"guest rootless configuration refused: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
