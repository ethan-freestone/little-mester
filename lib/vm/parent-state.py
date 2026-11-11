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
import select
import signal
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
                or stat.S_IMODE(info.st_mode) != 0o600 or stat.S_IMODE(path_info.st_mode) != 0o600
                or info.st_nlink != 1 or path_info.st_nlink != 1):
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
    creation_artifacts = {"base-image.part", "base-image.qcow2", "disk.qcow2.part"}
    if creation_artifacts.intersection(os.listdir(fd)):
        raise Refused("incomplete or leftover parent disk creation evidence is present")
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
        return status_for_run(parent, run, disk, home=home)
    finally:
        os.close(fd)


def status_for_run(parent: dict, run: dict, disk: dict | None = None, *, home: str | os.PathLike | None = None) -> dict:
    validate_parent(parent)
    if (not isinstance(run, dict) or type(run.get("schema")) is not int or run["schema"] != SCHEMA
            or run.get("parent_id") != PARENT_ID
            or not isinstance(run.get("run_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", run["run_id"])
            or run.get("phase") not in ("STARTING", "RUNNING", "STOPPING")
            or (run.get("phase") == "RUNNING"
                and set(run) != {"schema", "parent_id", "run_id", "phase", "supervisor", "qemu"})
            or (run.get("phase") != "RUNNING"
                and set(run) != {"schema", "parent_id", "run_id", "phase", "supervisor"})):
        return {"status": "AMBIGUOUS", "parent_id": PARENT_ID}
    identity = run.get("supervisor") if isinstance(run, dict) else None
    if not isinstance(identity, dict):
        return {"status": "AMBIGUOUS", "parent_id": PARENT_ID}
    if (set(identity) != {"schema", "pid", "uid", "host_boot_id", "start_time", "exe", "argv"}
            or type(identity.get("schema")) is not int or identity["schema"] != 1
            or type(identity.get("pid")) is not int or not 1 < identity["pid"] <= 2**31 - 1
            or type(identity.get("uid")) is not int or identity["uid"] != os.getuid()
            or not isinstance(identity.get("host_boot_id"), str)
            or not isinstance(identity.get("start_time"), str)
            or not identity["start_time"].isdigit()
            or not isinstance(identity.get("exe"), str) or not identity["exe"].startswith("/")
            or not isinstance(identity.get("argv"), list)
            or not identity["argv"] or not all(isinstance(item, str) for item in identity["argv"])
            or len(identity["argv"]) not in (11, 13)
            or not identity["argv"][0].startswith("/")
            or os.path.realpath(identity["argv"][0]) != identity["exe"]
            or identity["argv"][1] != "-I"
            or identity["argv"][2] != str(pathlib.Path(__file__).with_name("parent-supervisor.py").resolve())
            or identity["argv"][3:7] != ["--parent-id", PARENT_ID, "--run-id", run["run_id"]]
            or identity["argv"][7] != "--lock-fd" or not identity["argv"][8].isdigit()
            or identity["argv"][9] != "--handoff-fd" or not identity["argv"][10].isdigit()
            or (len(identity["argv"]) == 13 and
                (identity["argv"][11] != "--state-home" or home is None
                 or identity["argv"][12] != str(pathlib.Path(home).resolve())))
            or (len(identity["argv"]) == 11 and home is not None)
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
            if run["phase"] == "STOPPING":
                return {"status": "STOPPED", "parent_id": PARENT_ID, "disk": disk}
            if run["phase"] == "RUNNING":
                qemu_state = _recorded_process_status(
                    run.get("qemu"), expected_basename="qemu-system-x86_64",
                    current_boot_id=current_boot_id)
                if qemu_state == "GONE":
                    return {"status": "STOPPED", "parent_id": PARENT_ID, "disk": disk}
            return {"status": "AMBIGUOUS", "parent_id": PARENT_ID}
        except OSError:
            return {"status": "AMBIGUOUS", "parent_id": PARENT_ID}
        return {"status": "AMBIGUOUS", "parent_id": PARENT_ID}
    except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration):
        return {"status": "AMBIGUOUS", "parent_id": PARENT_ID}
    if run["phase"] == "RUNNING":
        qemu_state = _recorded_process_status(
            run.get("qemu"), expected_basename="qemu-system-x86_64",
            current_boot_id=current_boot_id)
        if qemu_state != "MATCH":
            return {"status": "AMBIGUOUS", "parent_id": PARENT_ID}
    return {"status": run["phase"], "parent_id": PARENT_ID, "run_id": run["run_id"], "disk": disk}


def _recorded_process_status(identity: object, *, expected_basename: str,
                             current_boot_id: str) -> str:
    if (not isinstance(identity, dict)
            or set(identity) != {"schema", "pid", "uid", "host_boot_id", "start_time", "exe", "argv"}
            or type(identity.get("schema")) is not int or identity["schema"] != 1
            or type(identity.get("pid")) is not int or not 1 < identity["pid"] <= 2**31 - 1
            or type(identity.get("uid")) is not int or identity["uid"] != os.getuid()
            or identity.get("host_boot_id") != current_boot_id
            or not isinstance(identity.get("start_time"), str) or not identity["start_time"].isdigit()
            or not isinstance(identity.get("exe"), str)
            or pathlib.Path(identity["exe"]).name != expected_basename
            or not isinstance(identity.get("argv"), list) or not identity["argv"]
            or not all(isinstance(arg, str) for arg in identity["argv"])):
        return "AMBIGUOUS"
    try:
        current = observe_process(identity["pid"])
    except Refused:
        proc = pathlib.Path(f"/proc/{identity['pid']}")
        try:
            proc.stat()
        except FileNotFoundError:
            return "GONE"
        except OSError:
            return "AMBIGUOUS"
        return "AMBIGUOUS"
    if (current["uid"] != identity["uid"] or current["host_boot_id"] != identity["host_boot_id"]
            or current["start_time"] != identity["start_time"] or current["exe"] != identity["exe"]
            or current["argv"] != identity["argv"]):
        return "AMBIGUOUS"
    return "MATCH"


def observe_process(pid: int) -> dict:
    """Capture exact live-process identity for a gated child before release."""
    if type(pid) is not int or not 1 < pid <= 2**31 - 1:
        raise Refused("invalid child PID")
    proc = pathlib.Path(f"/proc/{pid}")
    try:
        executable = os.path.realpath(proc / "exe")
        raw_argv = (proc / "cmdline").read_bytes().split(b"\0")
        if raw_argv and raw_argv[-1] == b"":
            raw_argv.pop()  # Drop only procfs's final terminator; preserve empty arguments.
        argv = [os.fsdecode(arg) for arg in raw_argv]
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        uid_fields = next((line.split() for line in (proc / "status").read_text().splitlines()
                           if line.startswith("Uid:")), [])
        boot_id = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if len(fields) <= 19 or len(uid_fields) < 2:
            raise ValueError("incomplete process identity")
        uid = int(uid_fields[1])
    except (OSError, ValueError, IndexError, StopIteration) as error:
        raise Refused("could not capture complete child identity") from error
    return {"schema": 1, "pid": pid, "uid": uid, "host_boot_id": boot_id,
            "start_time": fields[19], "exe": executable, "argv": argv}


def create_exec_transition(gate_identity: dict, target_exe: str, target_argv: list[str], *,
                           release_token: str, target_path: str, identity_path: str,
                           fd_contracts: list[dict] | None = None) -> dict:
    """Build exact gate and permitted same-PID direct-exec state for atomic commit."""
    if fd_contracts is None:
        fd_contracts = []
    if (not isinstance(gate_identity, dict) or set(gate_identity) !=
            {"schema", "pid", "uid", "host_boot_id", "start_time", "exe", "argv"}
            or type(gate_identity.get("schema")) is not int or gate_identity["schema"] != 1
            or type(gate_identity.get("pid")) is not int or not 1 < gate_identity["pid"] <= 2**31 - 1
            or type(gate_identity.get("uid")) is not int or gate_identity["uid"] != os.getuid()
            or not isinstance(gate_identity.get("host_boot_id"), str)
            or not isinstance(gate_identity.get("start_time"), str)
            or not gate_identity["start_time"].isdigit()
            or not isinstance(gate_identity.get("exe"), str) or not gate_identity["exe"].startswith("/")
            or not isinstance(gate_identity.get("argv"), list)
            or not gate_identity["argv"] or not all(isinstance(arg, str) for arg in gate_identity["argv"])
            or not isinstance(target_exe, str) or not target_exe.startswith("/")
            or not isinstance(target_argv, list) or not target_argv
            or not all(isinstance(arg, str) for arg in target_argv)
            or os.path.realpath(target_argv[0]) != os.path.realpath(target_exe)
            or not isinstance(release_token, str) or not re.fullmatch(r"[0-9a-f]{32}", release_token)
            or not isinstance(target_path, str) or not isinstance(identity_path, str)
            or not re.fullmatch(r"child-[a-z0-9-]+\.target\.json", target_path)
            or not re.fullmatch(r"child-[a-z0-9-]+\.identity\.json", identity_path)
            or not isinstance(fd_contracts, list)):
        raise Refused("invalid gated child transition identity")
    gate_argv = gate_identity["argv"]
    expected_gate = pathlib.Path(__file__).with_name("parent-gate.py").resolve()
    if (len(gate_argv) != 15 or not gate_argv[0].startswith("/")
            or os.path.realpath(gate_argv[0]) != gate_identity["exe"] or gate_argv[1] != "-I"
            or pathlib.Path(gate_argv[2]) != expected_gate
            or gate_argv[3] != "--release-fd" or not gate_argv[4].isdigit()
            or gate_argv[5] != "--token" or gate_argv[6] != release_token
            or gate_argv[7] != "--target" or gate_argv[8] != target_path
            or gate_argv[9] != "--identity" or gate_argv[10] != identity_path
            or gate_argv[11] != "--state-dir-fd" or not gate_argv[12].isdigit()
            or gate_argv[13] != "--disk-fd" or not (gate_argv[14] == "-1" or gate_argv[14].isdigit())):
        raise Refused("spawned child is not the protected parent gate invocation")
    _validate_fd_contracts(fd_contracts)
    disk_arg = gate_argv[14]
    if bool(fd_contracts) != (disk_arg != "-1") or (fd_contracts and int(disk_arg) in (int(gate_argv[4]), int(gate_argv[12]))):
        raise Refused("gate descriptor arguments do not match the FD contract")
    return {"schema": 2, "pid": gate_identity["pid"], "uid": gate_identity["uid"],
            "host_boot_id": gate_identity["host_boot_id"], "start_time": gate_identity["start_time"],
            "release_token": release_token, "target_path": target_path, "identity_path": identity_path,
            "gate": {"exe": gate_identity["exe"], "argv": gate_identity["argv"]},
            "target": {"exe": os.path.realpath(target_exe), "argv": target_argv,
                       "fd_contracts": fd_contracts}}


def _validate_fd_contracts(contracts: list[dict]) -> None:
    if not isinstance(contracts, list) or len(contracts) > 1:
        raise Refused("unsupported inherited descriptor contract")
    for contract in contracts:
        if (not isinstance(contract, dict) or set(contract) !=
                {"fd", "purpose", "device", "inode", "access", "fdset"}
                or contract.get("fd") != 3 or contract.get("purpose") != "parent-disk"
                or type(contract.get("device")) is not int or contract["device"] < 0
                or type(contract.get("inode")) is not int or contract["inode"] <= 0
                or contract.get("access") != "read-write"
                or type(contract.get("fdset")) is not int or contract["fdset"] != 1):
            raise Refused("invalid parent disk descriptor contract")


def persist_exec_transition(dir_fd: int, name: str, identity: dict, *, lock_fd: int) -> None:
    """Durably authorize a validated gate transition before releasing its pipe."""
    _validate_exec_transition(identity)
    _verify_lock_for_dir(dir_fd, lock_fd)
    if exec_transition_status(identity) != "GATE":
        raise Refused("parent gate identity is no longer exact before durable commit")
    if not re.fullmatch(r"child-[a-z0-9-]+\.identity\.json", name):
        raise Refused("invalid child identity filename")
    if name != identity["identity_path"]:
        raise Refused("child identity record path does not match gate authorization")
    _atomic_json(dir_fd, name, identity)


def _verify_lock_for_dir(dir_fd: int, lock_fd: int) -> None:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_info = os.fstat(lock_fd)
        path_info = os.stat("lifecycle.lock", dir_fd=dir_fd, follow_symlinks=False)
    except OSError as error:
        raise Refused("parent lifecycle lock is unavailable") from error
    if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid()
            or stat.S_IMODE(lock_info.st_mode) != 0o600 or not stat.S_ISREG(path_info.st_mode)
            or path_info.st_uid != os.getuid() or stat.S_IMODE(path_info.st_mode) != 0o600
            or lock_info.st_dev != path_info.st_dev or lock_info.st_ino != path_info.st_ino):
        raise Refused("parent lifecycle lock does not match the state directory")


def _validate_exec_transition(identity: dict) -> None:
    if (not isinstance(identity, dict) or set(identity) !=
            {"schema", "pid", "uid", "host_boot_id", "start_time", "release_token",
             "target_path", "identity_path", "gate", "target"}
            or type(identity.get("schema")) is not int or identity["schema"] != 2
            or type(identity.get("pid")) is not int or not 1 < identity["pid"] <= 2**31 - 1
            or type(identity.get("uid")) is not int or identity["uid"] != os.getuid()
            or not isinstance(identity.get("host_boot_id"), str)
            or not isinstance(identity.get("start_time"), str) or not identity["start_time"].isdigit()
            or not isinstance(identity.get("release_token"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", identity["release_token"])
            or not isinstance(identity.get("target_path"), str)
            or not re.fullmatch(r"child-[a-z0-9-]+\.target\.json", identity["target_path"])
            or not isinstance(identity.get("identity_path"), str)
            or not re.fullmatch(r"child-[a-z0-9-]+\.identity\.json", identity["identity_path"])):
        raise Refused("invalid gated transition schema")
    gate, target = identity.get("gate"), identity.get("target")
    if (not isinstance(gate, dict) or set(gate) != {"exe", "argv"}
            or not isinstance(gate.get("exe"), str) or not gate["exe"].startswith("/")
            or not isinstance(gate.get("argv"), list) or not gate["argv"]
            or not all(isinstance(arg, str) for arg in gate["argv"])):
        raise Refused("invalid gated transition gate identity")
    expected_gate = pathlib.Path(__file__).with_name("parent-gate.py").resolve()
    argv = gate["argv"]
    if (len(argv) != 15 or not argv[0].startswith("/")
            or os.path.realpath(argv[0]) != gate["exe"] or argv[1] != "-I"
            or pathlib.Path(argv[2]) != expected_gate or argv[3] != "--release-fd"
            or not argv[4].isdigit() or argv[5] != "--token" or argv[6] != identity["release_token"]
            or argv[7] != "--target" or argv[8] != identity["target_path"]
            or argv[9] != "--identity" or argv[10] != identity["identity_path"]
            or argv[11] != "--state-dir-fd" or not argv[12].isdigit()
            or argv[13] != "--disk-fd" or not (argv[14] == "-1" or argv[14].isdigit())):
        raise Refused("gate identity does not describe the protected invocation")
    if (not isinstance(target, dict) or set(target) != {"exe", "argv", "fd_contracts"}
            or not isinstance(target.get("exe"), str) or not target["exe"].startswith("/")
            or not isinstance(target.get("argv"), list) or not target["argv"]
            or not all(isinstance(arg, str) for arg in target["argv"])
            or not target["argv"][0].startswith("/")
            or os.path.realpath(target["argv"][0]) != target["exe"]):
        raise Refused("invalid gated transition target identity")
    _validate_fd_contracts(target["fd_contracts"])
    disk_arg = argv[14]
    if bool(target["fd_contracts"]) != (disk_arg != "-1") or (target["fd_contracts"] and int(disk_arg) in (int(argv[4]), int(argv[12]))):
        raise Refused("gate descriptor arguments do not match the FD contract")


def exec_transition_status(identity: dict) -> str:
    """Return GATE, TARGET, STOPPED, or AMBIGUOUS without signalling."""
    try:
        _validate_exec_transition(identity)
    except (Refused, TypeError, ValueError):
        return "AMBIGUOUS"
    try:
        current_boot_id = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "AMBIGUOUS"
    if identity["host_boot_id"] != current_boot_id:
        return "AMBIGUOUS"
    pid = identity["pid"]
    proc = pathlib.Path(f"/proc/{pid}")
    try:
        current = observe_process(pid)
    except Refused:
        try:
            proc.stat()
        except FileNotFoundError:
            return "STOPPED"
        except OSError:
            return "AMBIGUOUS"
        return "AMBIGUOUS"
    if (current["uid"] != identity["uid"] or current["host_boot_id"] != identity["host_boot_id"]
            or current["start_time"] != identity["start_time"]):
        return "AMBIGUOUS"
    for phase in ("gate", "target"):
        candidate = identity.get(phase)
        expected_keys = {"exe", "argv"} if phase == "gate" else {"exe", "argv", "fd_contracts"}
        if (isinstance(candidate, dict) and set(candidate) == expected_keys
                and isinstance(candidate.get("exe"), str) and isinstance(candidate.get("argv"), list)
                and all(isinstance(arg, str) for arg in candidate["argv"])
                and current["exe"] == candidate["exe"] and current["argv"] == candidate["argv"]):
            return phase.upper()
    return "AMBIGUOUS"


def stop_exec_transition(identity: dict, *, term_grace_ms: int = 5000, settle_ms: int = 2000) -> str:
    """Identity-bound bounded stop; callers must separately require owner authorization."""
    if (type(term_grace_ms) is not int or type(settle_ms) is not int
            or term_grace_ms <= 0 or settle_ms <= 0 or term_grace_ms + settle_ms > 15000):
        raise Refused("invalid bounded child stop deadline")
    try:
        _validate_exec_transition(identity)
    except (Refused, TypeError, ValueError):
        return "AMBIGUOUS"
    try:
        pidfd = os.pidfd_open(identity["pid"], 0)
    except ProcessLookupError:
        return "STOPPED"
    except (OSError, KeyError, TypeError):
        return "AMBIGUOUS"
    try:
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        if poller.poll(0):
            return "STOPPED"
        phase = exec_transition_status(identity)
        if phase not in ("GATE", "TARGET"):
            return phase
        signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        if poller.poll(term_grace_ms):
            return "STOPPED"
        if exec_transition_status(identity) not in ("GATE", "TARGET"):
            return "AMBIGUOUS"
        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        if poller.poll(settle_ms):
            return "STOPPED"
        return "AMBIGUOUS"
    except (OSError, ProcessLookupError):
        return "AMBIGUOUS"
    finally:
        os.close(pidfd)


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
        if result["status"] in ("RUNNING", "STARTING", "STOPPED", "UNPROVISIONED"):
            disk = result["disk"]
            print(f"Disk: {disk['path']} format={disk['format']} virtual_capacity_gib={disk['virtual_capacity_gib']}")
            if disk["host_allocated_bytes"] is None:
                print("Host allocation: disk not provisioned")
            else:
                print(f"Host allocation: {disk['host_allocated_bytes']} bytes; file length: {disk['file_bytes']} bytes")
        if result["status"] in ("RUNNING", "STARTING"):
            print(f"Run: {result['run_id']}")
    return 0 if result["status"] in ("NOT_CREATED", "UNPROVISIONED", "STOPPED", "STARTING", "RUNNING") else 2


if __name__ == "__main__":
    raise SystemExit(main())
