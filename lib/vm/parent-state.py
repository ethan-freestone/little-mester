#!/usr/bin/env python3
"""Private persistent-parent state, lifecycle lock, and read-only status."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pathlib
import pwd
import secrets
import stat
import sys
import re
from contextlib import contextmanager


PARENT_ID = "default"
STATE_COMPONENTS = (".local", "state", "little-mester", "parent-v3", PARENT_ID)
SCHEMA = 1
DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


class Refused(RuntimeError):
    pass


def _check_directory(fd: int, uid: int, label: str) -> None:
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise Refused(f"unsafe private parent {label}")


def _open_directory_chain(*, create: bool, home_override: str | os.PathLike | None = None) -> int | None:
    uid = os.getuid()
    home = pathlib.Path(home_override if home_override is not None else pwd.getpwuid(uid).pw_dir).resolve(strict=True)
    fd = os.open(home, DIRECTORY_FLAGS)
    try:
        _check_directory(fd, uid, "home directory")
        for index, component in enumerate(STATE_COMPONENTS):
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                next_fd = os.open(component, DIRECTORY_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    os.close(fd)
                    return None
                raise Refused("parent state directory disappeared during open")
            except OSError as error:
                raise Refused("parent state path contains a symlink or non-directory") from error
            try:
                _check_directory(next_fd, uid, "directory")
                if index >= 1 and stat.S_IMODE(os.fstat(next_fd).st_mode) != 0o700:
                    raise Refused("parent state directory has unsafe permissions")
            except BaseException:
                os.close(next_fd)
                raise
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _read_json(fd: int, name: str) -> dict:
    try:
        file_fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise Refused(f"parent state file {name} is unsafe or unreadable") from error
    try:
        info = os.fstat(file_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise Refused(f"parent state file {name} has unsafe type, owner, mode, or link count")
        with os.fdopen(file_fd, "r", encoding="utf-8", closefd=False) as stream:
            value = json.load(stream, object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise Refused(f"parent state file {name} is malformed or unreadable") from error
    finally:
        os.close(file_fd)
    if not isinstance(value, dict):
        raise Refused(f"parent state file {name} is not an object")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _atomic_json(fd: int, name: str, value: dict) -> None:
    temporary = f".{name}.{secrets.token_hex(16)}.tmp"
    file_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                      0o600, dir_fd=fd)
    try:
        os.fchmod(file_fd, 0o600)
        payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        view = memoryview(payload)
        while view:
            written = os.write(file_fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(file_fd)
        os.close(file_fd)
        file_fd = -1
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


def initialize(capacity_gib: int = 64, *, home: str | os.PathLike | None = None,
               lock_fd: int | None = None) -> dict:
    if type(capacity_gib) is not int or not 1 <= capacity_gib <= 4096:
        raise Refused("virtual disk capacity must be an integer from 1 through 4096 GiB")
    if lock_fd is None:
        with lifecycle_lock(home=home) as acquired_fd:
            return initialize(capacity_gib, home=home, lock_fd=acquired_fd)
    _verify_lock_fd(lock_fd, home=home)
    fd = _open_directory_chain(create=True, home_override=home)
    assert fd is not None
    try:
        try:
            existing = _read_json(fd, "parent.json")
        except FileNotFoundError:
            existing = None
        if existing is not None:
            validate_parent(existing)
            if existing["disk"]["virtual_capacity_gib"] != capacity_gib:
                raise Refused("parent already exists with a different disk capacity")
            return existing
        parent = {
            "schema": SCHEMA,
            "parent_id": PARENT_ID,
            "owner_uid": os.getuid(),
            "disk": {
                "path": "disk.qcow2",
                "format": "qcow2",
                "virtual_capacity_gib": capacity_gib,
                "provisioned": False,
                "device": None,
                "inode": None,
            },
            "workspace": {"backing": "guest-disk"},
        }
        _atomic_json(fd, "parent.json", parent)
        return parent
    finally:
        os.close(fd)


def validate_parent(value: dict) -> None:
    disk = value.get("disk")
    if (set(value) != {"schema", "parent_id", "owner_uid", "disk", "workspace"}
            or type(value.get("schema")) is not int or value.get("schema") != SCHEMA
            or value.get("parent_id") != PARENT_ID
            or type(value.get("owner_uid")) is not int or value["owner_uid"] != os.getuid()
            or not isinstance(disk, dict)
            or set(disk) != {"path", "format", "virtual_capacity_gib", "provisioned", "device", "inode"}
            or disk.get("path") != "disk.qcow2"
            or disk.get("format") != "qcow2"
            or type(disk.get("virtual_capacity_gib")) is not int
            or not 1 <= disk["virtual_capacity_gib"] <= 4096
            or type(disk.get("provisioned")) is not bool
            or (disk["provisioned"] and (type(disk.get("device")) is not int
                                        or type(disk.get("inode")) is not int
                                        or disk["device"] < 0 or disk["inode"] <= 0))
            or (not disk["provisioned"] and (disk.get("device") is not None or disk.get("inode") is not None))
            or not isinstance(value.get("workspace"), dict)
            or set(value["workspace"]) != {"backing"}
            or value["workspace"].get("backing") != "guest-disk"):
        raise Refused("parent state schema, owner, disk identity, or workspace is invalid")


def _verify_lock_fd(lock_fd: int, *, home: str | os.PathLike | None = None) -> None:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as error:
        raise Refused("parent lifecycle transaction does not own the exclusive lock") from error
    info = os.fstat(lock_fd)
    fd = _open_directory_chain(create=False, home_override=home)
    if fd is None:
        raise Refused("parent state disappeared while lifecycle lock was held")
    try:
        path_info = os.stat("lifecycle.lock", dir_fd=fd, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_dev != path_info.st_dev
                or info.st_ino != path_info.st_ino or info.st_uid != os.getuid()
                or not stat.S_ISREG(path_info.st_mode) or path_info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise Refused("lifecycle transaction does not hold the private lock file")
    finally:
        os.close(fd)


@contextmanager
def lifecycle_lock(*, nonblocking: bool = True, home: str | os.PathLike | None = None):
    """Hold the one-writer lock; the returned fd is non-inheritable by exec."""
    fd = _open_directory_chain(create=True, home_override=home)
    assert fd is not None
    try:
        lock_fd = os.open("lifecycle.lock", os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
                          0o600, dir_fd=fd)
        info = os.fstat(lock_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            os.close(lock_fd)
            raise Refused("lifecycle lock has unsafe type, owner, mode, or link count")
        os.set_inheritable(lock_fd, False)
        operation = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        try:
            fcntl.flock(lock_fd, operation)
        except BlockingIOError as error:
            os.close(lock_fd)
            raise Refused("parent lifecycle lock is held") from error
        yield lock_fd
    finally:
        if "lock_fd" in locals():
            try:
                os.close(lock_fd)
            except OSError:
                pass
        os.close(fd)


def _disk_status(fd: int, parent: dict) -> dict:
    disk = parent["disk"]
    try:
        disk_fd = os.open(disk["path"], os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
    except FileNotFoundError:
        if disk["provisioned"]:
            raise Refused("provisioned parent disk is missing")
        return {**disk, "host_allocated_bytes": None, "file_bytes": None}
    except OSError as error:
        raise Refused("parent disk path is unsafe or unreadable") from error
    try:
        info = os.fstat(disk_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise Refused("parent disk has unsafe type, owner, mode, or link count")
        if not disk["provisioned"] or info.st_dev != disk["device"] or info.st_ino != disk["inode"]:
            raise Refused("parent disk identity does not match the recorded parent")
        return {**disk, "host_allocated_bytes": info.st_blocks * 512, "file_bytes": info.st_size}
    finally:
        os.close(disk_fd)


def read_status(*, home: str | os.PathLike | None = None) -> dict:
    fd = _open_directory_chain(create=False, home_override=home)
    if fd is None:
        return {"status": "NOT_CREATED", "parent_id": PARENT_ID}
    try:
        try:
            parent = _read_json(fd, "parent.json")
        except FileNotFoundError:
            names = set(os.listdir(fd))
            if names - {"lifecycle.lock"}:
                return {"status": "AMBIGUOUS", "parent_id": PARENT_ID}
            return {"status": "NOT_CREATED", "parent_id": PARENT_ID}
        except Refused:
            return {"status": "AMBIGUOUS", "parent_id": PARENT_ID}
        try:
            validate_parent(parent)
            disk = _disk_status(fd, parent)
        except Refused:
            return {"status": "AMBIGUOUS", "parent_id": PARENT_ID}
        try:
            run = _read_json(fd, "active-run.json")
        except FileNotFoundError:
            if not parent["disk"]["provisioned"]:
                return {"status": "UNPROVISIONED", "parent_id": PARENT_ID, "disk": disk}
            return {"status": "AMBIGUOUS", "parent_id": PARENT_ID}
        except Refused:
            return {"status": "AMBIGUOUS", "parent_id": PARENT_ID}
        return status_for_run(parent, run, disk)
    finally:
        os.close(fd)


def status_for_run(parent: dict, run: dict, disk: dict | None = None) -> dict:
    validate_parent(parent)
    if (not isinstance(run, dict) or type(run.get("schema")) is not int or run["schema"] != SCHEMA
            or run.get("parent_id") != PARENT_ID
            or not isinstance(run.get("run_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", run["run_id"])
            or set(run) != {"schema", "parent_id", "run_id", "supervisor"}):
        return {"status": "AMBIGUOUS", "parent_id": PARENT_ID}
    identity = run.get("supervisor") if isinstance(run, dict) else None
    if not isinstance(identity, dict):
        return {"status": "AMBIGUOUS", "parent_id": PARENT_ID}
    if (set(identity) != {"schema", "pid", "uid", "host_boot_id", "start_time", "exe", "argv"}
            or type(identity.get("schema")) is not int or identity["schema"] != 1
            or type(identity.get("pid")) is not int or identity["pid"] <= 1
            or type(identity.get("uid")) is not int or identity["uid"] != os.getuid()
            or not isinstance(identity.get("host_boot_id"), str)
            or not isinstance(identity.get("start_time"), str)
            or not identity["start_time"].isdigit()
            or not isinstance(identity.get("exe"), str) or not identity["exe"].startswith("/")
            or not isinstance(identity.get("argv"), list)
            or not identity["argv"] or not all(isinstance(item, str) for item in identity["argv"])
            or len(identity["argv"]) != 7
            or not identity["argv"][0].startswith("/")
            or os.path.realpath(identity["argv"][0]) != identity["exe"]
            or identity["argv"][1] != "-I"
            or not identity["argv"][2].endswith("/lib/vm/parent-supervisor.py")
            or identity["argv"][3:] != ["--parent-id", PARENT_ID, "--run-id", run["run_id"]]
            or not parent["disk"]["provisioned"] or disk is None):
        return {"status": "AMBIGUOUS", "parent_id": PARENT_ID}
    try:
        current_boot_id = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return {"status": "AMBIGUOUS", "parent_id": PARENT_ID}
    try:
        pid = identity["pid"]
        if type(pid) is not int or pid <= 1:
            raise ValueError("invalid PID")
        if (identity.get("schema") != 1 or type(identity.get("uid")) is not int
                or identity["uid"] != os.getuid()
                or identity.get("host_boot_id") != current_boot_id):
            return {"status": "AMBIGUOUS", "parent_id": PARENT_ID}
        proc = pathlib.Path(f"/proc/{pid}")
        executable = os.path.realpath(proc / "exe")
        argv = [os.fsdecode(arg) for arg in (proc / "cmdline").read_bytes().split(b"\0") if arg]
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        uid_fields = next((line.split() for line in (proc / "status").read_text().splitlines()
                           if line.startswith("Uid:")), [])
        if (len(fields) <= 19 or len(uid_fields) < 2
                or str(identity.get("start_time")) != fields[19]
                or identity.get("exe") != executable or identity.get("argv") != argv
                or int(uid_fields[1]) != identity["uid"]):
            return {"status": "AMBIGUOUS", "parent_id": PARENT_ID}
    except FileNotFoundError:
        try:
            proc.stat()
        except FileNotFoundError:
            if disk is None or not parent["disk"]["provisioned"]:
                return {"status": "AMBIGUOUS", "parent_id": PARENT_ID}
            return {"status": "STOPPED", "parent_id": PARENT_ID, "disk": disk}
        except OSError:
            return {"status": "AMBIGUOUS", "parent_id": PARENT_ID}
        return {"status": "AMBIGUOUS", "parent_id": PARENT_ID}
    except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration):
        return {"status": "AMBIGUOUS", "parent_id": PARENT_ID}
    return {"status": "RUNNING", "parent_id": PARENT_ID, "run_id": run["run_id"], "disk": disk}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("status",))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = read_status()
    except Refused as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"Parent: {result['status']}")
        if result["status"] in ("RUNNING", "STOPPED", "UNPROVISIONED"):
            disk = result["disk"]
            print(f"Disk: {disk['path']} format={disk['format']} virtual_capacity_gib={disk['virtual_capacity_gib']}")
            if disk["host_allocated_bytes"] is None:
                print("Host allocation: disk not provisioned")
            else:
                print(f"Host allocation: {disk['host_allocated_bytes']} bytes; file length: {disk['file_bytes']} bytes")
        if result["status"] == "RUNNING":
            print(f"Run: {result['run_id']}")
    return 0 if result["status"] in ("NOT_CREATED", "UNPROVISIONED", "STOPPED", "RUNNING") else 2


if __name__ == "__main__":
    raise SystemExit(main())
