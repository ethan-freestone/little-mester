#!/usr/bin/env python3
"""Allocate and inspect the private short-path virtiofs socket for one run."""

from __future__ import annotations

import json
import os
import pathlib
import re
import stat
import sys


RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}$")
SOCKET_DIR_PREFIX = "lmv3-"
SOCKET_NAME = "virtiofs.sock"
AUTH_CONTROL_SOCKET_NAME = "auth-control.sock"
SOCKET_PATH_FILE = "virtiofs.socket-path"
MAX_SUN_PATH_BYTES = 107
MAX_AUTH_CONTROL_PATH_BYTES = 99
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class Refusal(Exception):
    pass


def _check_directory(info, owner: int, *, mode: int | None = None, searchable: bool = True) -> None:
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != owner
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise Refusal(
            f"runtime ancestry is not an owner-controlled protected directory "
            f"(owner {info.st_uid}, expected {owner}, mode {stat.S_IMODE(info.st_mode):04o})"
        )
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise Refusal("private runtime directory mode is not 0700")
    if searchable:
        search_bit = stat.S_IXUSR if owner == os.getuid() else stat.S_IXOTH
        if not info.st_mode & search_bit:
            raise Refusal("runtime ancestry is not traversable by the invoking user")


def _open_runtime_root(runtime_root: pathlib.Path, uid: int, system_uid: int) -> int:
    if not runtime_root.is_absolute() or ".." in runtime_root.parts:
        raise Refusal("runtime root is not a canonical absolute path")
    fd = os.open("/", DIRECTORY_FLAGS)
    current = pathlib.Path("/")
    try:
        parts = runtime_root.parts[1:]
        for index, component in enumerate(parts):
            current /= component
            expected_owner = uid if index == len(parts) - 1 else system_uid
            try:
                before = os.stat(component, dir_fd=fd, follow_symlinks=False)
                _check_directory(before, expected_owner,
                                 mode=0o700 if index == len(parts) - 1 else None)
                child_fd = os.open(component, DIRECTORY_FLAGS, dir_fd=fd)
            except OSError as error:
                raise Refusal("runtime ancestry is missing, inaccessible, or contains a symlink") from error
            try:
                opened = os.fstat(child_fd)
                _check_directory(opened, expected_owner,
                                 mode=0o700 if index == len(parts) - 1 else None)
                if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                    raise Refusal("runtime ancestry changed while opening")
            except BaseException:
                os.close(child_fd)
                raise
            os.close(fd)
            fd = child_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_run_dir(run_dir: pathlib.Path, uid: int) -> int:
    if not run_dir.is_absolute() or ".." in run_dir.parts:
        raise Refusal("run directory is not a canonical absolute path")
    try:
        info = os.lstat(run_dir)
        _check_directory(info, uid, mode=0o700)
        fd = os.open(run_dir, DIRECTORY_FLAGS)
        opened = os.fstat(fd)
        _check_directory(opened, uid, mode=0o700)
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            os.close(fd)
            raise Refusal("run directory changed while opening")
        return fd
    except OSError as error:
        raise Refusal("run directory is missing, inaccessible, or unsafe") from error


def _run_id(run_dir: pathlib.Path) -> str:
    value = run_dir.name
    if not RUN_ID_RE.fullmatch(value):
        raise Refusal("run directory name is not a fresh VM run identity")
    return value


def _expected_socket_path(run_dir: pathlib.Path, runtime_root: pathlib.Path, uid: int) -> pathlib.Path:
    run_id = _run_id(run_dir)
    path = runtime_root / f"{SOCKET_DIR_PREFIX}{run_id}" / SOCKET_NAME
    byte_length = len(os.fsencode(path))
    if byte_length > MAX_SUN_PATH_BYTES:
        raise Refusal(
            f"virtiofs UNIX socket path is {byte_length} bytes; Linux supports at most "
            f"{MAX_SUN_PATH_BYTES}; use a shorter protected runtime root"
        )
    return path


def _expected_auth_control_path(run_dir: pathlib.Path, runtime_root: pathlib.Path, uid: int) -> pathlib.Path:
    run_id = _run_id(run_dir)
    path = runtime_root / f"{SOCKET_DIR_PREFIX}{run_id}" / AUTH_CONTROL_SOCKET_NAME
    byte_length = len(os.fsencode(path))
    if byte_length > MAX_AUTH_CONTROL_PATH_BYTES:
        raise Refusal(
            f"SSH control socket path is {byte_length} bytes; must be shorter than 100"
        )
    return path


def allocate(run_dir: pathlib.Path, runtime_root: pathlib.Path, uid: int, system_uid: int = 0) -> pathlib.Path:
    """Exclusively allocate a per-run directory under the protected UID runtime root."""
    run_fd = _open_run_dir(run_dir, uid)
    try:
        root_fd = _open_runtime_root(runtime_root, uid, system_uid)
    except BaseException:
        os.close(run_fd)
        raise
    created = False
    try:
        run_id = _run_id(run_dir)
        directory_name = f"{SOCKET_DIR_PREFIX}{run_id}"
        socket_path = _expected_socket_path(run_dir, runtime_root, uid)
        for name in (SOCKET_PATH_FILE, "virtiofs.sock", "virtiofs.sock.pid"):
            try:
                os.stat(name, dir_fd=run_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise Refusal("refusing pre-existing virtiofs socket/PID metadata in the run directory")
        try:
            os.mkdir(directory_name, 0o700, dir_fd=root_fd)
            created = True
        except FileExistsError as error:
            raise Refusal("refusing pre-existing short virtiofs socket directory; run identity collision") from error
        socket_dir_fd = os.open(directory_name, DIRECTORY_FLAGS, dir_fd=root_fd)
        try:
            info = os.fstat(socket_dir_fd)
            _check_directory(info, uid, mode=0o700)
            for name in (SOCKET_NAME, SOCKET_NAME + ".pid", AUTH_CONTROL_SOCKET_NAME):
                try:
                    os.stat(name, dir_fd=socket_dir_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                raise Refusal("refusing a pre-existing per-run runtime socket or PID artifact")
        finally:
            os.close(socket_dir_fd)

        descriptor = os.open(
            SOCKET_PATH_FILE,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=run_fd,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(str(socket_path) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(run_fd)
        return socket_path
    except BaseException:
        if created:
            try:
                os.rmdir(directory_name, dir_fd=root_fd)
            except OSError:
                pass
        raise
    finally:
        os.close(root_fd)
        os.close(run_fd)


def read_path(run_dir: pathlib.Path, runtime_root: pathlib.Path, uid: int, system_uid: int = 0) -> pathlib.Path:
    run_fd = _open_run_dir(run_dir, uid)
    try:
        root_fd = _open_runtime_root(runtime_root, uid, system_uid)
    except BaseException:
        os.close(run_fd)
        raise
    try:
        expected = _expected_socket_path(run_dir, runtime_root, uid)
        try:
            info = os.stat(SOCKET_PATH_FILE, dir_fd=run_fd, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 256
                    or info.st_nlink != 1):
                raise Refusal("virtiofs socket-path evidence has unsafe metadata")
            descriptor = os.open(SOCKET_PATH_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=run_fd)
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                recorded = stream.read(257).rstrip("\n")
        except OSError as error:
            raise Refusal("virtiofs socket-path evidence is missing or unsafe") from error
        if recorded != str(expected):
            raise Refusal("virtiofs socket path does not match this run's protected short-path allocation")
        directory_name = expected.parent.name
        try:
            info = os.stat(directory_name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return expected
        _check_directory(info, uid, mode=0o700)
        child_fd = os.open(directory_name, DIRECTORY_FLAGS, dir_fd=root_fd)
        try:
            opened = os.fstat(child_fd)
            _check_directory(opened, uid, mode=0o700)
            if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
                raise Refusal("virtiofs socket directory changed while opening")
        finally:
            os.close(child_fd)
        return expected
    finally:
        os.close(root_fd)
        os.close(run_fd)


def inspect(run_dir: pathlib.Path, runtime_root: pathlib.Path, uid: int, system_uid: int,
            *, startup: bool) -> dict:
    socket_path = read_path(run_dir, runtime_root, uid, system_uid)
    root_fd = _open_runtime_root(runtime_root, uid, system_uid)
    directory_name = socket_path.parent.name
    try:
        try:
            directory_info = os.stat(directory_name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            if startup:
                raise Refusal("allocated short virtiofs socket directory disappeared")
            if _listener_present(str(socket_path)):
                raise Refusal("virtiofs socket still has a live listener")
            return {"socket_path": str(socket_path), "runtime_directory": "expired",
                    "socket_present": False, "pid_present": False}
        _check_directory(directory_info, uid, mode=0o700)
        socket_dir_fd = os.open(directory_name, DIRECTORY_FLAGS, dir_fd=root_fd)
        try:
            entries = {entry.name for entry in os.scandir(socket_dir_fd)}
            if not entries.issubset({SOCKET_NAME, SOCKET_NAME + ".pid"}):
                raise Refusal("short virtiofs directory contains unexpected artifacts")
            socket_info = None
            pid_info = None
            try:
                socket_info = os.stat(SOCKET_NAME, dir_fd=socket_dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            try:
                pid_info = os.stat(SOCKET_NAME + ".pid", dir_fd=socket_dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            if socket_info is not None and (
                not stat.S_ISSOCK(socket_info.st_mode) or socket_info.st_uid != uid
            ):
                raise Refusal("virtiofs socket failed type or owner validation")
            if pid_info is not None:
                if (not stat.S_ISREG(pid_info.st_mode) or pid_info.st_uid != uid
                        or stat.S_IMODE(pid_info.st_mode) != 0o600 or pid_info.st_nlink != 1):
                    raise Refusal("virtiofs PID artifact failed type, owner, or mode validation")
                fd = os.open(SOCKET_NAME + ".pid", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=socket_dir_fd)
                with os.fdopen(fd, "r", encoding="ascii") as stream:
                    pid = stream.read(64).strip()
                if not pid.isdigit() or int(pid) <= 1:
                    raise Refusal("virtiofs PID artifact is malformed")
        finally:
            os.close(socket_dir_fd)
    finally:
        os.close(root_fd)

    if startup and (socket_info is None or pid_info is None):
        raise Refusal("virtiofsd did not create its short socket and private PID artifact")
    if not startup and _listener_present(str(socket_path)):
        raise Refusal("virtiofs socket still has a live listener")
    return {"socket_path": str(socket_path), "runtime_directory": "present",
            "socket_present": socket_info is not None, "pid_present": pid_info is not None}


def _listener_present(path: str) -> bool:
    try:
        with open("/proc/net/unix", encoding="ascii") as stream:
            return any((fields := line.rstrip("\n").split(None, 7)) and len(fields) == 8
                       and fields[7] == path for line in stream.readlines()[1:])
    except OSError as error:
        raise Refusal("could not verify virtiofs socket listener state") from error


def create_log(run_dir: pathlib.Path, name: str) -> None:
    if name != "virtiofsd.log":
        raise Refusal("unsupported private log name")
    run_fd = _open_run_dir(run_dir, os.getuid())
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=run_fd,
        )
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1):
                raise Refusal("pre-created virtiofsd log failed type or ownership validation")
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(run_fd)
    except FileExistsError as error:
        raise Refusal("refusing pre-existing virtiofsd log") from error
    finally:
        os.close(run_fd)


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3) or argv[0] not in {"allocate", "path", "auth-path", "startup", "stopped", "create-log"}:
        print("usage: virtiofs-socket.py {allocate|path|auth-path|startup|stopped|create-log} RUN_DIR [LOG_NAME]", file=sys.stderr)
        return 2
    operation = argv[0]
    raw_run_dir = argv[1]
    run_dir = pathlib.Path(raw_run_dir)
    uid = os.getuid()
    runtime_root = pathlib.Path(f"/run/user/{uid}")
    try:
        if operation == "create-log":
            if len(argv) != 3:
                raise Refusal("create-log requires an exact log name")
            create_log(run_dir, argv[2])
        elif len(argv) != 2:
            raise Refusal("unexpected arguments")
        elif operation == "allocate":
            print(allocate(run_dir, runtime_root, uid))
        elif operation == "path":
            print(read_path(run_dir, runtime_root, uid))
        elif operation == "auth-path":
            # Revalidate the existing exclusive allocation before deriving the
            # second socket path in its private per-run runtime directory.
            read_path(run_dir, runtime_root, uid)
            print(_expected_auth_control_path(run_dir, runtime_root, uid))
        else:
            print(json.dumps(inspect(run_dir, runtime_root, uid, 0, startup=operation == "startup"),
                             sort_keys=True))
    except (OSError, Refusal, ValueError, UnicodeError) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
