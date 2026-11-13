#!/usr/bin/env python3
"""Private persistent-parent state, lifecycle lock, and read-only status."""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
import pathlib
import pwd
import re
import secrets
import select
import signal
import stat
import subprocess
import sys
import time
from contextlib import contextmanager

PARENT_ID = "default"
STATE_COMPONENTS = (".local", "state", "little-mester", "parent-v3", PARENT_ID)
SCHEMA = 1
PARENT_STOP_TIMEOUT_SECONDS = 90.0
_ARTIFACT_EXPECTATION_UNSET = object()
STALE_RECONCILIATION_PREFIX = "stale-boot-reconciliation-"


def _stale_reconciliation_filename(run_id: str) -> str:
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise Refused("invalid run ID for stale-boot audit lookup")
    return f"{STALE_RECONCILIATION_PREFIX}{run_id}.json"


def _installed_runtime_root(source: str | os.PathLike | None = None,
                            filename: str = "parent-state.py") -> pathlib.Path | None:
    """Derive the v3 prefix from a runtime's own immutable installation path."""
    path = pathlib.Path(__file__ if source is None else source)
    if source is None:
        path = path.resolve()
    if (path.name != filename or path.parent.name != "vm"
            or path.parent.parent.name != "lib" or not path.is_absolute()):
        return None
    install = path.parents[2]
    if not re.fullmatch(r"[0-9a-f]{40}", install.name) or install.parent.name != "v3":
        return None
    return install.parent


PARENT_READY_COMMAND = (
    "cloud-init status --long >/dev/null 2>&1 || exit 41; "
    "test -x /usr/bin/tmux || exit 42; "
    "test -x /usr/local/bin/lm-opencode || exit 43; "
    "/usr/local/bin/opencode --version 2>/dev/null | grep -Fx 'opencode v2.2.5' >/dev/null || exit 44"
)
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


def _atomic_bytes(fd: int, name: str, payload: bytes) -> None:
    if not isinstance(payload, bytes):
        raise TypeError("private atomic payload must be bytes")
    temporary = f".{name}.{secrets.token_hex(16)}.tmp"
    file_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                      0o600, dir_fd=fd)
    try:
        os.fchmod(file_fd, 0o600)
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


def _atomic_json(fd: int, name: str, value: dict) -> None:
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    _atomic_bytes(fd, name, payload)


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
    expected_fields = {"schema", "parent_id", "owner_uid", "disk", "workspace"}
    if "ssh_identity" in value:
        expected_fields.add("ssh_identity")
    ssh_identity = value.get("ssh_identity")
    if "ssh_identity" in value and (not isinstance(ssh_identity, dict)
            or set(ssh_identity) != {"seed_sha256", "control_private_sha256", "control_public_sha256",
                                    "host_private_sha256", "host_public_sha256"}
            or any(not isinstance(ssh_identity.get(name), str)
                   or not re.fullmatch(r"[0-9a-f]{64}", ssh_identity[name])
                   for name in ("seed_sha256", "control_private_sha256", "control_public_sha256",
                                "host_private_sha256", "host_public_sha256"))):
        raise Refused("parent SSH identity metadata is invalid")
    if (set(value) != expected_fields
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
    creation_artifacts = {"base-image.part", "base-image.qcow2", "disk.qcow2.part", "seed.iso.part"}
    if creation_artifacts.intersection(os.listdir(fd)):
        raise Refused("incomplete or leftover parent disk creation evidence is present")
    _validate_seed_identity(fd, parent)
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


def _hash_private_file(fd: int, name: str) -> str:
    try:
        file_fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd)
    except OSError as error:
        raise Refused(f"persistent parent identity file is unavailable: {name}") from error
    try:
        info = os.fstat(file_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise Refused(f"persistent parent identity file is unsafe: {name}")
        digest = hashlib.sha256()
        while chunk := os.read(file_fd, 1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(file_fd)


def _validate_seed_identity(fd: int, parent: dict) -> None:
    identity = parent.get("ssh_identity")
    seed_files = {"id_vm_control", "id_vm_control.pub", "ssh_host_ed25519_key",
                  "ssh_host_ed25519_key.pub", "user-data", "meta-data", "seed.iso.part"}
    try:
        seed_info = os.stat("seed.iso", dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        if identity is not None:
            raise Refused("persistent parent cloud-init seed is missing")
        if seed_files.intersection(os.listdir(fd)):
            raise Refused("incomplete persistent parent SSH seed evidence is present")
        return
    if identity is None:
        raise Refused("persistent parent cloud-init seed has no recorded identity")
    if (not stat.S_ISREG(seed_info.st_mode) or seed_info.st_uid != os.getuid()
            or stat.S_IMODE(seed_info.st_mode) != 0o600 or seed_info.st_nlink != 1):
        raise Refused("persistent parent cloud-init seed is unsafe")
    for filename, key in (("seed.iso", "seed_sha256"), ("id_vm_control", "control_private_sha256"),
                          ("id_vm_control.pub", "control_public_sha256"),
                          ("ssh_host_ed25519_key", "host_private_sha256"),
                          ("ssh_host_ed25519_key.pub", "host_public_sha256")):
        if _hash_private_file(fd, filename) != identity[key]:
            raise Refused(f"persistent parent identity does not match {filename}")
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


def recovery_preview(*, home: str | os.PathLike | None = None) -> dict:
    """Report recovery evidence without creating state, repairing files, or signalling."""
    directory_fd = _open_directory_chain(create=False, home_override=home)
    if directory_fd is None:
        return {"dry_run": True, "action": "none", "status": "NOT_CREATED",
                "parent_id": PARENT_ID}
    lock_fd = -1
    try:
        try:
            lock_fd = os.open("lifecycle.lock", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                              dir_fd=directory_fd)
        except FileNotFoundError:
            status = read_status(home=home)
            return {"dry_run": True, "action": "none", "status": status["status"],
                    "parent_id": PARENT_ID, "reason": "no lifecycle lock file exists"}
        except OSError as error:
            raise Refused("parent lifecycle lock is unsafe or unreadable") from error
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Refused("live supervisor or lifecycle operation owns the parent lock") from error
        except OSError as error:
            raise Refused("could not verify ownership of the parent lifecycle lock") from error
        try:
            _verify_lock_fd(lock_fd, home=home)
        except (OSError, Refused) as error:
            raise Refused("parent lifecycle lock changed or has unsafe identity") from error
        try:
            plan = _validated_recovery_plan(directory_fd, home=home)
        except (Refused, OSError):
            return {"dry_run": True, "action": "none", "status": "AMBIGUOUS",
                    "parent_id": PARENT_ID,
                    "reason": "state is ambiguous; no identity was signalled or changed"}
        if plan is None:
            status = read_status(home=home)
            return {"dry_run": True, "action": "none", "status": status["status"],
                    "parent_id": PARENT_ID,
                    "reason": "no recovery action is required or safely available"}
        if plan["run"]["phase"] == "STOPPING":
            children_stopped = all(value == "STOPPED" for value in plan["child_states"].values())
            if not children_stopped:
                return {"dry_run": True, "action": "recover", "status": "RECOVERABLE",
                        "parent_id": PARENT_ID, "run_id": plan["run"]["run_id"],
                        "children": plan["child_states"], "artifacts": plan["pending_artifacts"],
                        "reason": "exact recorded child identities remain live; execution requires --execute"}
            if plan["pending_artifacts"]:
                return {"dry_run": True, "action": "cleanup", "status": "STOPPED",
                        "parent_id": PARENT_ID, "run_id": plan["run"]["run_id"],
                        "artifacts": plan["pending_artifacts"],
                        "reason": "children are stopped; verified recovery artifacts remain to clean"}
            return {"dry_run": True, "action": "none", "status": "STOPPED",
                    "parent_id": PARENT_ID, "run_id": plan["run"]["run_id"],
                    "reason": "parent is already verified STOPPED; no recovery action is required"}
        return {"dry_run": True, "action": "recover", "status": "RECOVERABLE",
                "parent_id": PARENT_ID, "run_id": plan["run"]["run_id"],
                "children": plan["child_states"],
                "artifacts": plan["pending_artifacts"],
                "reason": "dead supervisor and complete exact child identities verified; execution requires --execute"}
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(directory_fd)


def stale_boot_reconciliation(*, home: str | os.PathLike | None = None,
                              execute: bool = False) -> dict:
    """Preview or explicitly reconcile a validated run from a prior host boot.

    Historical identities are never signalled or rewritten. Execution requires
    the lifecycle lock and a complete privileged holder scan of the exact disk.
    """
    if type(execute) is not bool:
        raise Refused("stale-boot execution flag is invalid")
    directory_fd = _open_directory_chain(create=False, home_override=home)
    if directory_fd is None:
        return {"dry_run": not execute, "action": "none", "status": "NOT_CREATED",
                "parent_id": PARENT_ID}
    lock_fd = disk_fd = -1
    try:
        try:
            lock_fd = os.open("lifecycle.lock", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                              dir_fd=directory_fd)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _verify_lock_fd(lock_fd, home=home)
        except (OSError, Refused) as error:
            raise Refused("stale-boot reconciliation requires exclusive verified lifecycle ownership") from error
        plan = _validated_stale_boot_plan(directory_fd, home=home)
        if plan is None:
            current = read_status(home=home)
            return {"dry_run": not execute, "action": "none", "status": current["status"],
                    "parent_id": PARENT_ID}
        disk_path = plan["disk"]["path"]
        disk_fd = os.open(disk_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                          dir_fd=directory_fd)
        disk_info = os.fstat(disk_fd)
        expected = (plan["disk"]["device"], plan["disk"]["inode"])
        if (not stat.S_ISREG(disk_info.st_mode) or (disk_info.st_dev, disk_info.st_ino) != expected):
            raise Refused("persistent disk identity changed before holder scan")
        _scan_stale_disk_holders(disk_fd, expected,
                                 scratch_dir=pathlib.Path(disk_path).parent)
        # The scan is point-in-time: revalidate both the lock and all state/disk
        # evidence immediately before any audit/state writes.
        _verify_lock_fd(lock_fd, home=home)
        fresh = _validated_stale_boot_plan(directory_fd, home=home)
        if fresh is None or fresh != plan:
            raise Refused("stale-boot evidence changed during holder scan")
        disk_info = os.fstat(disk_fd)
        if (disk_info.st_dev, disk_info.st_ino) != expected:
            raise Refused("persistent disk identity changed during holder scan")
        audit = {"schema": 1, "parent_id": PARENT_ID, "run_id": plan["run"]["run_id"],
                 "operator_uid": os.getuid(), "reconciled_at_unix_ns": time.time_ns(),
                 "current_boot_id": plan["current_boot_id"],
                 "historical_boot_id": plan["historical_boot_id"],
                  "disk": {"device": expected[0], "inode": expected[1]},
                  "artifacts": plan["artifacts"],
                  "holder_scan": {"method": "privileged-procfs", "complete_scans": 1,
                                  "positive_control": True,
                                  "coverage": {"procfs": True, "pid_namespace": "init-shared",
                                               "user_namespace": "init-shared",
                                               "descriptors": True, "file_mappings": True}},
                  "historical_run": plan["run"],
                   "historical_transitions": _archive_stale_transitions(plan["transitions"])}
        audit_name = _stale_reconciliation_filename(plan["run"]["run_id"])
        try:
            existing_audit = _read_json(directory_fd, audit_name)
        except FileNotFoundError:
            existing_audit = None
        if existing_audit is not None:
            audit_tombstone = {key: value for key, value in plan["run"].items() if key != "qemu"}
            audit_tombstone["phase"] = "STOPPING"
            if (not isinstance(existing_audit, dict)
                    or existing_audit.get("historical_run") != plan["run"]
                    or _valid_stale_reconciliation(directory_fd, plan["parent"], audit_tombstone,
                                                   plan["disk"], allow_pending=True) is None):
                raise Refused("existing stale-boot audit conflicts with current state")
            if existing_audit.get("transaction") == "COMPLETE":
                return {"dry_run": not execute, "action": "none", "status": "STOPPED",
                        "parent_id": PARENT_ID, "run_id": plan["run"]["run_id"],
                        "disk_retained": True}
            cleanup_artifacts = existing_audit["artifacts"]
        else:
            cleanup_artifacts = plan["artifacts"]
        if not execute:
            return {"dry_run": True, "action": "reconcile", "status":
                    "RECONCILIATION_PENDING" if existing_audit is not None else "RECONCILABLE",
                    "parent_id": PARENT_ID, "run_id": plan["run"]["run_id"],
                    "disk": {"device": expected[0], "inode": expected[1],
                             "virtual_capacity_gib": plan["parent"]["disk"]["virtual_capacity_gib"]},
                    "holder_scan": {"method": "privileged-procfs", "complete_scans": 1,
                                    "positive_control": True,
                                    "coverage": {"procfs": True, "pid_namespace": "init-shared",
                                                 "user_namespace": "init-shared",
                                                 "descriptors": True, "file_mappings": True},
                                    "unexpected_holders": 0},
                    "reason": "prior-boot records and no current disk holders verified; execution requires --execute"}
        _verify_lock_fd(lock_fd, home=home)
        if existing_audit is None:
            audit["transaction"] = "PENDING"
            _atomic_json(directory_fd, audit_name, audit)
        tombstone = {key: value for key, value in plan["run"].items() if key != "qemu"}
        tombstone["phase"] = "STOPPING"
        _atomic_json(directory_fd, "active-run.json", tombstone)
        for name, kind in (("supervisor.sock", "socket"), ("qmp.sock", "socket"),
                           ("proxy.port", "file")):
            _verify_lock_fd(lock_fd, home=home)
            _recovery_remove_artifact(directory_fd, name, kind,
                                      expected=cleanup_artifacts[name])
        os.fsync(directory_fd)
        _verify_lock_fd(lock_fd, home=home)
        for name in ("supervisor.sock", "qmp.sock", "proxy.port"):
            try:
                os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise Refused(f"reconciliation cleanup did not remove {name}")
        # A STOPPING tombstone is not completion. Publish COMPLETE only after
        # cleanup is verified; interrupted transactions remain ambiguous.
        completed = dict(audit)
        completed["transaction"] = "COMPLETE"
        _atomic_json(directory_fd, audit_name, completed)
        os.fsync(directory_fd)
        status = read_status(home=home)
        if status["status"] != "STOPPED":
            raise Refused("reconciliation committed but final parent status is not STOPPED")
        return {"dry_run": False, "action": "reconciled", "status": "STOPPED",
                "parent_id": PARENT_ID, "run_id": plan["run"]["run_id"],
                "disk_retained": True}
    finally:
        if disk_fd >= 0:
            os.close(disk_fd)
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(directory_fd)


def _validated_stale_boot_plan(directory_fd: int, *, home) -> dict | None:
    """Validate old-boot evidence structurally without observing/signalling its PIDs."""
    parent = _read_json(directory_fd, "parent.json")
    validate_parent(parent)
    disk = _disk_status(directory_fd, parent)
    active_run = _read_json(directory_fd, "active-run.json")
    try:
        current_boot = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError as error:
        raise Refused("current host boot identity is unavailable") from error
    if active_run.get("phase") == "STOPPING":
        try:
            audit = _read_json(directory_fd,
                               _stale_reconciliation_filename(active_run.get("run_id")))
        except FileNotFoundError as error:
            raise Refused("STOPPING state lacks stale-boot reconciliation audit") from error
        historical = audit.get("historical_run") if isinstance(audit, dict) else None
        if (not isinstance(historical, dict) or not _valid_stale_reconciliation(
                directory_fd, parent, active_run, disk, allow_pending=True)):
            raise Refused("incomplete stale-boot reconciliation evidence")
        run = historical
    else:
        run = active_run
    if (not parent["disk"]["provisioned"] or disk is None
            or run.get("schema") != SCHEMA or run.get("parent_id") != PARENT_ID
            or run.get("phase") != "RUNNING"
            or set(run) != {"schema", "parent_id", "run_id", "phase", "supervisor", "qemu"}
            or not isinstance(run.get("run_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", run["run_id"])):
        raise Refused("stale-boot run or persistent disk evidence is malformed")
    supervisor = run["supervisor"]
    qemu = run["qemu"]
    if not _valid_recorded_identity(supervisor) or not _valid_recorded_identity(qemu):
        raise Refused("stale-boot process identity evidence is malformed")
    if (supervisor["host_boot_id"] == current_boot or qemu["host_boot_id"] == current_boot
            or supervisor["host_boot_id"] != qemu["host_boot_id"]):
        raise Refused("run is not a consistent prior-boot run")
    # Preserve the trusted runtime/argv contract by validating a copy through the
    # normal schema validator; only the copied boot binding is adjusted, never disk state.
    stopping = dict(run)
    stopping["phase"] = "STOPPING"
    stopping.pop("qemu")
    if not _valid_supervisor_schema(parent, stopping, supervisor, disk, home):
        raise Refused("historical supervisor trusted schema or runtime path is invalid")
    transitions = {}
    for role in ("qemu", "metrics", "proxy"):
        ident_name, target_name = f"child-{role}.identity.json", f"child-{role}.target.json"
        try:
            transition = _read_json(directory_fd, ident_name)
        except FileNotFoundError:
            if role in ("qemu", "proxy"):
                raise Refused(f"stale-boot record lacks required {role} identity")
            try:
                _read_json(directory_fd, target_name)
            except FileNotFoundError:
                pass
            else:
                raise Refused(f"stale-boot record has an unpaired {role} target")
            continue
        _validate_exec_transition(transition)
        if (transition["identity_path"] != ident_name or transition["target_path"] != target_name
                or transition["host_boot_id"] != supervisor["host_boot_id"]):
            raise Refused(f"stale-boot {role} transition binding is inconsistent")
        target = _read_json(directory_fd, target_name)
        if target != {"executable": transition["target"]["exe"],
                      "argv": transition["target"]["argv"],
                      "fd_contracts": transition["target"]["fd_contracts"]}:
            raise Refused(f"stale-boot {role} target record is inconsistent")
        transitions[role] = transition
    qemu_transition = transitions.get("qemu")
    proxy_transition = transitions.get("proxy")
    proxy_port = str(pathlib.Path(home if home is not None else pwd.getpwuid(os.getuid()).pw_dir)
                      .joinpath(*STATE_COMPONENTS, "proxy.port"))
    proxy_argv = proxy_transition["target"]["argv"] if proxy_transition is not None else []
    if (proxy_transition is None
            or not pathlib.Path(proxy_transition["target"]["exe"]).name.startswith("python3")
            or len(proxy_argv) != 5 or proxy_argv[0] != proxy_transition["target"]["exe"]
            or proxy_argv[1] != "-I" or proxy_argv[3:] != ["--port-file", proxy_port]
            or not _trusted_parent_runtime_script(
                proxy_argv[2] if len(proxy_argv) > 2 else "", "proxy.py")):
        raise Refused("stale-boot proxy identity does not match the trusted runtime invocation")
    if (qemu_transition is None or any(qemu.get(key) != qemu_transition.get(key)
                                      for key in ("pid", "uid", "host_boot_id", "start_time"))
            or qemu.get("exe") != qemu_transition["target"]["exe"]
            or qemu.get("argv") != qemu_transition["target"]["argv"]
            or qemu.get("exe") != "/usr/bin/qemu-system-x86_64"
            or qemu_transition["target"]["fd_contracts"] != [{
                "fd": 3, "purpose": "parent-disk", "device": disk["device"],
                "inode": disk["inode"], "access": "read-write", "fdset": 1}]):
        raise Refused("stale-boot QEMU identity does not match its trusted transition")
    artifacts = {}
    for name, kind in (("supervisor.sock", "socket"), ("qmp.sock", "socket"),
                       ("proxy.port", "file")):
        artifacts[name] = _recovery_artifact_identity(directory_fd, name, kind)
    return {"parent": parent, "run": run, "disk": disk, "transitions": transitions,
            "artifacts": artifacts,
            "current_boot_id": current_boot,
            "historical_boot_id": supervisor["host_boot_id"]}


def _valid_recorded_identity(identity) -> bool:
    return (isinstance(identity, dict)
            and set(identity) == {"schema", "pid", "uid", "host_boot_id", "start_time", "exe", "argv"}
            and type(identity.get("schema")) is int and identity["schema"] == 1
            and type(identity.get("pid")) is int and 1 < identity["pid"] <= 2**31 - 1
            and type(identity.get("uid")) is int and identity["uid"] == os.getuid()
            and isinstance(identity.get("host_boot_id"), str)
            and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                             identity["host_boot_id"]) is not None
            and isinstance(identity.get("start_time"), str) and identity["start_time"].isdigit()
            and isinstance(identity.get("exe"), str) and identity["exe"].startswith("/")
            and isinstance(identity.get("argv"), list) and bool(identity["argv"])
            and all(isinstance(arg, str) for arg in identity["argv"]))


def _scan_stale_disk_holders(disk_fd: int, expected: tuple[int, int], *,
                             scratch_dir: pathlib.Path | None = None) -> None:
    """Run the installed root-only procfs scanner against a scratch control and disk."""
    try:
        install_root = pathlib.Path(__file__).resolve().parents[2]
        script = install_root / "scripts/parent-readonly-safety-gate.py"
        for component in (script, script.parent, *script.parents[1:]):
            info = component.lstat()
            if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise Refused("privileged holder scanner path is not protected")
            if component == script:
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o644:
                    raise Refused("trusted privileged holder scanner is unavailable")
            elif not stat.S_ISDIR(info.st_mode):
                raise Refused("trusted privileged holder scanner parent is unsafe")
            if component == install_root:
                break
        sudo = pathlib.Path("/usr/bin/sudo")
        python = pathlib.Path("/usr/bin/python3")
        for executable in (sudo, python):
            info = executable.stat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                    or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                    or not os.access(executable, os.X_OK)):
                raise Refused(f"trusted {executable} is unavailable")

        def scan(device: int, inode: int, owner_pid: int, owner_fd: int) -> dict:
            command = [str(sudo), "-n", str(python), "-I", "-B", str(script),
                       "--scan-inode", str(device), str(inode), str(owner_pid), str(owner_fd)]
            try:
                result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        timeout=30, check=False,
                                        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
                                             "PYTHONDONTWRITEBYTECODE": "1"})
            except (OSError, subprocess.SubprocessError) as error:
                raise Refused("privileged procfs scan failed or timed out") from error
            if result.returncode or len(result.stdout) > 65536 or result.stderr:
                error = result.stderr.decode("utf-8", "replace") if result.stderr else ""
                category = next((line.removeprefix("REFUSED: ") for line in error.splitlines()
                                 if line.startswith("REFUSED: ")), "scan_incomplete")
                if category not in {
                        "insufficient_privilege_or_invalid_identity", "procfs_unavailable",
                        "procfs_restricted",
                        "pid_namespace_restricted", "user_namespace_restricted",
                        "procfs_visibility_failed", "procfs_incomplete",
                        "procfs_enumeration_failed", "malformed_process_identity",
                        "incomplete_task_enumeration", "incomplete_fd_enumeration",
                        "descriptor_observation_race", "descriptor_changed_during_observation",
                        "incomplete_mapping_observation", "malformed_mapping_identity",
                        "malformed_mapping_observation", "unreadable_live_process",
                        "incomplete_procfs_scan", "task_enumeration_limit_exceeded",
                        "descriptor_enumeration_limit_exceeded", "mapping_output_limit_exceeded",
                        "process_changed_during_scan", "incomplete_live_process_observation",
                        "invalid_caller_fd_exemption", "caller_changed_during_scan",
                        "protected_helper_unavailable",
                        "protected_helper_path_untrusted"}:
                    category = "scan_incomplete"
                raise Refused(f"privileged procfs scan refused: {category}")
            try:
                value = json.loads(result.stdout)
            except (ValueError, UnicodeError) as error:
                raise Refused("privileged procfs scanner returned malformed evidence") from error
            if (not isinstance(value, dict) or value.get("complete") is not True
                    or type(value.get("processes_scanned")) is not int
                    or value["processes_scanned"] < 1
                    or value.get("coverage") != {"procfs": True,
                                                   "pid_namespace": "init-shared",
                                                   "user_namespace": "init-shared",
                                                   "descriptors": True,
                                                   "file_mappings": True}
                    or not isinstance(value.get("holders"), list)):
                raise Refused("privileged procfs scanner did not affirm complete coverage")
            return value

        authenticated = subprocess.run([str(sudo), "-v"], check=False, timeout=30)
        if authenticated.returncode:
            raise Refused("privileged scanner authentication failed")
        import tempfile
        with tempfile.NamedTemporaryFile(prefix="parent-reconcile-positive-",
                                         dir=scratch_dir) as positive:
            positive_info = os.fstat(positive.fileno())
            control = scan(positive_info.st_dev, positive_info.st_ino, 0, -1)
            if not any(isinstance(item, list) and len(item) == 3
                       and item[0] == os.getpid() and item[1] == "fd"
                       for item in control["holders"]):
                raise Refused("privileged procfs positive control was not detected")
        info = os.fstat(disk_fd)
        if (info.st_dev, info.st_ino) != expected or not stat.S_ISREG(info.st_mode):
            raise Refused("persistent disk identity changed before privileged scan")
        result = scan(*expected, os.getpid(), disk_fd)
        if result["holders"]:
            raise Refused("persistent disk has observed descriptor or mapping holders")
    except Exception as error:
        raise Refused(f"privileged disk-holder verification failed: {error}") from error


def _validated_recovery_plan(directory_fd: int, *, home: str | os.PathLike | None) -> dict | None:
    """Validate complete orphan evidence. Never infer an absent child identity."""
    try:
        parent = _read_json(directory_fd, "parent.json")
        validate_parent(parent)
        disk = _disk_status(directory_fd, parent)
        run = _read_json(directory_fd, "active-run.json")
    except FileNotFoundError:
        return None
    if (run.get("schema") != SCHEMA or run.get("parent_id") != PARENT_ID
            or not isinstance(run.get("run_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", run["run_id"])
            or run.get("phase") not in ("STARTING", "RUNNING", "STOPPING")):
        raise Refused("parent recovery run record is malformed")

    reconciled = _valid_stale_reconciliation(directory_fd, parent, run, disk)
    if reconciled:
        pending = []
        for name, kind in (("supervisor.sock", "socket"), ("qmp.sock", "socket"),
                           ("proxy.port", "file")):
            _validate_recovery_artifact(directory_fd, name, kind)
            try:
                os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            pending.append(name)
        if pending:
            raise Refused("reconciled stale run unexpectedly retains cleanup artifacts")
        return {"run": run, "transitions": {}, "child_states": {},
                "pending_artifacts": [], "boot_id": reconciled["current_boot_id"],
                "stale_reconciled": True}

    # Reuse the complete supervisor schema/trusted-argv validation in status_for_run,
    # then independently require that exact supervisor identity to be gone.
    stopping_shape = dict(run)
    stopping_shape["phase"] = "STOPPING"
    stopping_shape.pop("qemu", None)
    validated_status = status_for_run(parent, stopping_shape, disk, home=home)
    if validated_status["status"] not in ("STOPPED", "STOPPING"):
        raise Refused("supervisor identity or run schema is ambiguous")
    supervisor = run.get("supervisor")
    try:
        boot_id = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError as error:
        raise Refused("host boot identity is unavailable for recovery") from error
    supervisor_state = _recorded_process_status(
        supervisor, expected_basename=pathlib.Path(supervisor["exe"]).name,
        current_boot_id=boot_id)
    if supervisor_state != "GONE":
        raise Refused("recovery requires the exact recorded supervisor to be stopped")
    if not parent["disk"]["provisioned"] or disk is None:
        return None

    child_states = {}
    transitions = {}
    roles = ("qemu", "metrics", "proxy")
    required_roles = {"qemu", "proxy"} if run["phase"] in ("STARTING", "RUNNING") else set()
    for role in roles:
        identity_name = f"child-{role}.identity.json"
        target_name = f"child-{role}.target.json"
        try:
            transition = _read_json(directory_fd, identity_name)
        except FileNotFoundError:
            if role in required_roles:
                raise Refused(f"recovery lacks the durable {role} child identity")
            if run["phase"] == "STOPPING":
                try:
                    _read_json(directory_fd, target_name)
                except FileNotFoundError:
                    pass
                else:
                    raise Refused(f"STOPPING recovery has an unpaired {role} target record")
            continue
        _validate_exec_transition(transition)
        if (transition["identity_path"] != identity_name
                or transition["target_path"] != target_name):
            raise Refused(f"recovery {role} transition names an unexpected state file")
        target_record = _read_json(directory_fd, target_name)
        if (target_record != {"executable": transition["target"]["exe"],
                              "argv": transition["target"]["argv"],
                              "fd_contracts": transition["target"]["fd_contracts"]}):
            raise Refused(f"recovery {role} target does not match its durable identity")
        child_state = exec_transition_status(transition)
        if child_state not in ("GATE", "TARGET", "STOPPED"):
            raise Refused(f"recovery {role} process identity is ambiguous")
        transitions[role] = transition
        child_states[role] = child_state

    if (run["phase"] == "STOPPING"
            and any(child_state != "STOPPED" for child_state in child_states.values())
            and (child_states.get("qemu") != "STOPPED"
                 or any(role not in ("metrics", "proxy") and child_state != "STOPPED"
                        for role, child_state in child_states.items()))):
        raise Refused("STOPPING recovery requires verified-stopped QEMU and exact helper identities")

    if run["phase"] in ("STARTING", "RUNNING") and not transitions:
        # No records cannot prove that no process was spawned before a SIGKILL.
        raise Refused("recovery cannot infer child absence from a partial startup record")
    if run["phase"] == "RUNNING":
        qemu = run.get("qemu")
        transition = transitions.get("qemu")
        if (not isinstance(qemu, dict) or transition is None
                or any(qemu.get(key) != transition.get(key) for key in
                       ("pid", "uid", "host_boot_id", "start_time"))
                or qemu.get("exe") != transition["target"]["exe"]
                or qemu.get("argv") != transition["target"]["argv"]
                or _recorded_process_status(qemu, expected_basename="qemu-system-x86_64",
                                            current_boot_id=boot_id) not in ("MATCH", "GONE")):
            raise Refused("RUNNING QEMU identity does not match its durable child transition")
    pending_artifacts = []
    for name, kind in (("supervisor.sock", "socket"), ("qmp.sock", "socket"),
                       ("proxy.port", "file")):
        try:
            os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        _validate_recovery_artifact(directory_fd, name, kind)
        pending_artifacts.append(name)
    return {"run": run, "transitions": transitions, "child_states": child_states,
            "pending_artifacts": pending_artifacts, "boot_id": boot_id}


def recover_parent(*, home: str | os.PathLike | None = None, execute: bool = False) -> dict:
    """Explicitly stop only fully recorded orphan children; default remains preview-only."""
    if type(execute) is not bool:
        raise Refused("recovery execution flag is invalid")
    if not execute:
        return recovery_preview(home=home)
    directory_fd = _open_directory_chain(create=False, home_override=home)
    if directory_fd is None:
        return {"dry_run": False, "action": "none", "status": "NOT_CREATED",
                "parent_id": PARENT_ID}
    lock_fd = -1
    try:
        try:
            lock_fd = os.open("lifecycle.lock", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                              dir_fd=directory_fd)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _verify_lock_fd(lock_fd, home=home)
        except (OSError, Refused) as error:
            raise Refused("recovery could not acquire the verified exclusive lifecycle lock") from error
        plan = _validated_recovery_plan(directory_fd, home=home)
        if plan is None:
            status = read_status(home=home)
            return {"dry_run": False, "action": "none", "status": status["status"],
                    "parent_id": PARENT_ID}
        run = plan["run"]
        transitions = plan["transitions"]
        children_stopped = all(value == "STOPPED" for value in plan["child_states"].values())
        if run["phase"] == "STOPPING" and children_stopped and not plan["pending_artifacts"]:
            return {"dry_run": False, "action": "none", "status": "STOPPED",
                    "parent_id": PARENT_ID, "run_id": run["run_id"], "disk_retained": True}
        for role in ("qemu", "metrics", "proxy"):
            transition = transitions.get(role)
            if transition is None or plan["child_states"][role] == "STOPPED":
                continue
            _verify_lock_fd(lock_fd, home=home)
            if stop_exec_transition(
                    transition, authorize_signal=lambda: _verify_lock_fd(lock_fd, home=home)) != "STOPPED":
                raise Refused(f"recovery could not stop the exact {role} child")

        # Publish STOPPING only after every recorded child is independently stopped.
        _verify_lock_fd(lock_fd, home=home)
        run["phase"] = "STOPPING"
        run.pop("qemu", None)
        _atomic_json(directory_fd, "active-run.json", run)
        for name, kind in (("supervisor.sock", "socket"), ("qmp.sock", "socket"),
                           ("proxy.port", "file")):
            _verify_lock_fd(lock_fd, home=home)
            _recovery_remove_artifact(directory_fd, name, kind)
        os.fsync(directory_fd)
        status = read_status(home=home)
        if status["status"] != "STOPPED":
            raise Refused("recovery cleanup completed but final status is not verified STOPPED")
        return {"dry_run": False, "action": "stopped", "status": "STOPPED",
                "parent_id": PARENT_ID, "run_id": run["run_id"], "disk_retained": True}
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(directory_fd)


def _recovery_remove_artifact(directory_fd: int, name: str, kind: str, *,
                              expected: dict | None | object = _ARTIFACT_EXPECTATION_UNSET) -> None:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    _validate_recovery_artifact(directory_fd, name, kind)
    observed = {"device": info.st_dev, "inode": info.st_ino, "mode": info.st_mode,
                "uid": info.st_uid, "nlink": info.st_nlink}
    if expected is not _ARTIFACT_EXPECTATION_UNSET and (
            expected is None or observed != expected):
        raise Refused(f"recovery artifact identity changed: {name}")
    os.unlink(name, dir_fd=directory_fd)


def _recovery_artifact_identity(directory_fd: int, name: str, kind: str) -> dict | None:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    _validate_recovery_artifact(directory_fd, name, kind)
    return {"device": info.st_dev, "inode": info.st_ino, "mode": info.st_mode,
            "uid": info.st_uid, "nlink": info.st_nlink}


def _valid_stale_reconciliation(directory_fd: int, parent: dict, run: dict,
                                disk: dict | None, *, allow_pending: bool = False) -> dict | None:
    """Validate the durable audit/tombstone projection without relaxing PID checks."""
    try:
        audit = _read_json(directory_fd, _stale_reconciliation_filename(run.get("run_id")))
    except FileNotFoundError:
        return None
    if (not isinstance(audit, dict) or set(audit) != {
            "schema", "parent_id", "run_id", "operator_uid", "reconciled_at_unix_ns",
            "current_boot_id", "historical_boot_id", "disk", "artifacts", "holder_scan", "historical_run",
            "historical_transitions", "transaction"}
            or type(audit.get("schema")) is not int or audit["schema"] != 1
            or audit.get("parent_id") != PARENT_ID
            or type(audit.get("operator_uid")) is not int or audit["operator_uid"] != os.getuid()
            or type(audit.get("reconciled_at_unix_ns")) is not int
            or audit["reconciled_at_unix_ns"] <= 0
            or not isinstance(disk, dict) or not parent["disk"]["provisioned"]):
        raise Refused("stale-boot reconciliation audit is malformed")
    old = audit["historical_run"]
    scans = audit["holder_scan"]
    if (not isinstance(old, dict) or old.get("phase") != "RUNNING"
            or set(old) != {"schema", "parent_id", "run_id", "phase", "supervisor", "qemu"}
            or type(old.get("schema")) is not int or old.get("schema") != SCHEMA
            or old.get("parent_id") != PARENT_ID
            or not isinstance(old.get("run_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", old["run_id"])
            or old.get("run_id") != run.get("run_id")
            or audit.get("run_id") != run.get("run_id")
            or run.get("phase") != "STOPPING"
            or set(run) != {"schema", "parent_id", "run_id", "phase", "supervisor"}
            or any(run.get(key) != old.get(key) for key in
                   ("schema", "parent_id", "run_id", "supervisor"))
            or not _valid_recorded_identity(old.get("supervisor"))
            or not _valid_recorded_identity(old.get("qemu"))
            or audit.get("historical_boot_id") != old["supervisor"]["host_boot_id"]
            or old["qemu"]["host_boot_id"] != audit["historical_boot_id"]
            or not isinstance(audit.get("current_boot_id"), str)
            or not audit["current_boot_id"]
            or audit["current_boot_id"] == audit["historical_boot_id"]
            or not isinstance(audit.get("disk"), dict)
            or audit["disk"] != {"device": disk["device"], "inode": disk["inode"]}
            or not isinstance(scans, dict)
             or scans != {"method": "privileged-procfs", "complete_scans": 1,
                          "positive_control": True,
                          "coverage": {"procfs": True, "pid_namespace": "init-shared",
                                       "user_namespace": "init-shared",
                                       "descriptors": True, "file_mappings": True}}):
        raise Refused("stale-boot reconciliation audit does not match current retained state")
    allowed_transactions = ("PENDING", "COMPLETE") if allow_pending else ("COMPLETE",)
    if audit.get("transaction") not in allowed_transactions:
        raise Refused("stale-boot reconciliation transaction is incomplete")
    artifact_kinds = {"supervisor.sock": "socket", "qmp.sock": "socket", "proxy.port": "file"}
    artifacts = audit["artifacts"]
    if not isinstance(artifacts, dict) or set(artifacts) != set(artifact_kinds):
        raise Refused("stale-boot audit artifact snapshot is malformed")
    for name, kind in artifact_kinds.items():
        expected_artifact = artifacts[name]
        if expected_artifact is not None and (
                not isinstance(expected_artifact, dict)
                or set(expected_artifact) != {"device", "inode", "mode", "uid", "nlink"}
                or any(type(expected_artifact.get(key)) is not int for key in
                       ("device", "inode", "mode", "uid", "nlink"))):
            raise Refused("stale-boot audit artifact identity is malformed")
        current_artifact = _recovery_artifact_identity(directory_fd, name, kind)
        if current_artifact is not None and current_artifact != expected_artifact:
            raise Refused("stale-boot transient artifact was replaced after verification")
        if audit["transaction"] == "COMPLETE" and current_artifact is not None:
            raise Refused("completed stale-boot reconciliation retains a transient artifact")
    transitions = audit["historical_transitions"]
    if (not isinstance(transitions, dict) or not {"qemu", "proxy"}.issubset(transitions)
            or set(transitions) - {"qemu", "proxy", "metrics"}):
        raise Refused("stale-boot audit historical transitions are malformed")
    for role, transition in transitions.items():
        if not isinstance(transition, dict):
            raise Refused("stale-boot audit historical transition is malformed")
        identity_name = f"child-{role}.identity.json"
        target_name = f"child-{role}.target.json"
        actual_transition = _read_json(directory_fd, identity_name)
        _validate_exec_transition(actual_transition)
        if (actual_transition.get("identity_path") != identity_name
                or actual_transition.get("target_path") != target_name
                or actual_transition.get("host_boot_id") != audit["historical_boot_id"]
                or _archive_stale_transition(actual_transition) != transition):
            raise Refused("stale-boot audit historical transition changed")
        target = _read_json(directory_fd, target_name)
        if target != {"executable": actual_transition["target"]["exe"],
                      "argv": actual_transition["target"]["argv"],
                      "fd_contracts": actual_transition["target"]["fd_contracts"]}:
            raise Refused("stale-boot audit historical target changed")
    qemu = old["qemu"]
    if qemu.get("exe") != "/usr/bin/qemu-system-x86_64":
        raise Refused("stale-boot audit contains unexpected QEMU identity")
    return audit


def _archive_stale_transition(transition: dict) -> dict:
    """Preserve transition evidence without duplicating its one-time release token."""
    _validate_exec_transition(transition)
    archived = copy.deepcopy(transition)
    token = archived.pop("release_token")
    argv = archived.get("gate", {}).get("argv")
    if not isinstance(argv, list):
        raise Refused("historical gate argv is malformed")
    occurrences = [index for index, value in enumerate(argv) if value == token]
    if len(occurrences) != 1:
        raise Refused("historical release token binding is ambiguous")
    argv[occurrences[0]] = "<redacted-release-token>"
    archived["release_token_sha256"] = hashlib.sha256(token.encode("ascii")).hexdigest()
    return archived


def _archive_stale_transitions(transitions: dict) -> dict:
    return {role: _archive_stale_transition(transition)
            for role, transition in transitions.items()}


def _validate_recovery_artifact(directory_fd: int, name: str, kind: str) -> None:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (info.st_uid != os.getuid() or info.st_nlink != 1
            or (kind == "socket" and
                (not stat.S_ISSOCK(info.st_mode)
                 or stat.S_IMODE(info.st_mode) & 0o600 != 0o600
                 or stat.S_IMODE(info.st_mode) & 0o077))
            or (kind == "file" and
                (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600))):
        raise Refused(f"recovery preserved unsafe {name} evidence")


def request_supervisor_stop(*, home: str | os.PathLike | None = None,
                            timeout_seconds: float = PARENT_STOP_TIMEOUT_SECONDS,
                            reap_supervisor=None) -> dict:
    """Request cooperative stop through a pidfd for a verified supervisor."""
    if (type(timeout_seconds) not in (int, float)
            or not 0 < timeout_seconds <= PARENT_STOP_TIMEOUT_SECONDS):
        raise Refused("supervisor stop timeout exceeds the shared 90-second completion budget")
    if reap_supervisor is not None and not callable(reap_supervisor):
        raise Refused("supervisor reaper must be a callable process poll")
    if reap_supervisor is not None:
        # The launcher owns this child. Reap an already-exited supervisor
        # before read_status() mistakes its zombie procfs entry for an
        # identity mismatch.
        reap_supervisor()
    status = read_status(home=home)
    if status["status"] == "AMBIGUOUS" and reap_supervisor is not None:
        # The child can exit between poll() and the procfs identity read.
        reap_supervisor()
        status = read_status(home=home)
    if status["status"] == "STOPPED":
        _require_completed_stop(home=home, expected_run_id=status.get("run_id"))
        return {"status": "STOPPED", "parent_id": PARENT_ID,
                "run_id": status.get("run_id"), "disk_retained": True}
    if status["status"] not in ("STARTING", "RUNNING"):
        raise Refused("parent stop requires a verified STARTING or RUNNING supervisor")
    directory_fd = _open_directory_chain(create=False, home_override=home)
    if directory_fd is None:
        raise Refused("parent state disappeared before stop request")
    pidfd = -1
    try:
        run = _read_json(directory_fd, "active-run.json")
        if (run.get("run_id") != status.get("run_id") or run.get("phase") != status.get("status")
                or not isinstance(run.get("supervisor"), dict)
                or run["supervisor"].get("pid") != status.get("supervisor_pid")):
            raise Refused("parent run changed before stop request")
        identity = run["supervisor"]
        try:
            pidfd = os.pidfd_open(identity["pid"], 0)
        except (OSError, ProcessLookupError, AttributeError) as error:
            raise Refused("could not bind a pidfd to the verified supervisor") from error
        try:
            current = observe_process(identity["pid"])
        except Refused as error:
            raise Refused("supervisor identity changed before stop request") from error
        if any(current.get(key) != identity.get(key)
               for key in ("schema", "pid", "uid", "host_boot_id", "start_time", "exe", "argv")):
            raise Refused("supervisor identity changed before stop request")
        if _recorded_process_status(identity, expected_basename=pathlib.Path(identity["exe"]).name,
                                    current_boot_id=identity["host_boot_id"]) != "MATCH":
            raise Refused("supervisor identity failed final pre-signal verification")
        try:
            signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        except (OSError, ProcessLookupError, AttributeError) as error:
            raise Refused("could not send the cooperative stop request through the supervisor pidfd") from error
    finally:
        if pidfd >= 0:
            os.close(pidfd)
        os.close(directory_fd)
    deadline = time.monotonic() + timeout_seconds
    ambiguous_seen = False
    while time.monotonic() < deadline:
        if reap_supervisor is not None:
            reap_supervisor()
        result = read_status(home=home)
        if result["status"] == "AMBIGUOUS" and reap_supervisor is not None:
            # Close the exit race between poll() and the procfs read: if the
            # supervisor exited in that interval, poll again and re-evaluate.
            reap_supervisor()
            result = read_status(home=home)
        if result["status"] == "STOPPED":
            _require_completed_stop(home=home, expected_run_id=status.get("run_id"))
            return {"status": "STOPPED", "parent_id": PARENT_ID,
                    "run_id": status.get("run_id"), "disk_retained": True}
        if result["status"] == "AMBIGUOUS":
            # Procfs can briefly expose a zombie/reap race after the pidfd signal.
            # Continue observation only; never send another signal or infer success.
            ambiguous_seen = True
        time.sleep(0.05)
    if ambiguous_seen:
        raise Refused("supervisor stop left ambiguous identity evidence; no signal escalation performed")
    raise Refused("supervisor did not stop before the bounded wait expired; no signal escalation performed")


def _require_completed_stop(*, home: str | os.PathLike | None,
                            expected_run_id: str | None = None) -> None:
    """Accept stop only after the same run publishes its verified STOPPING tombstone."""
    try:
        with lifecycle_lock(home=home):
            directory_fd = _open_directory_chain(create=False, home_override=home)
            if directory_fd is None:
                raise Refused("parent state disappeared while verifying completed stop")
            try:
                status = read_status(home=home)
                plan = _validated_recovery_plan(directory_fd, home=home)
            finally:
                os.close(directory_fd)
    except (OSError, Refused) as error:
        raise Refused("could not verify completed parent stop under the lifecycle lock") from error
    if (status.get("status") != "STOPPED" or plan is None
            or plan["run"].get("phase") != "STOPPING"
            or not isinstance(plan["run"].get("run_id"), str)
            or expected_run_id is not None and plan["run"].get("run_id") != expected_run_id
            or any(child_state != "STOPPED" for child_state in plan["child_states"].values())
            or plan["pending_artifacts"]):
        raise Refused("supervisor exited without publishing verified STOPPING cleanup; preserve lifecycle evidence")


def parent_ssh_plan(*, home: str | os.PathLike | None = None,
                    attach_opencode: bool = False, allow_starting: bool = False,
                    remote_command: str | None = None, tty: bool = True) -> tuple[list[str], list[int]]:
    """Build pinned SSH argv and retain validated identity/key descriptors for its caller."""
    if (type(attach_opencode) is not bool or type(allow_starting) is not bool
            or type(tty) is not bool
            or (remote_command is not None and
                (not isinstance(remote_command, str) or not remote_command
                 or "\0" in remote_command or "\n" in remote_command or "\r" in remote_command))
            or attach_opencode and (allow_starting or remote_command is not None or not tty)
            or allow_starting and (remote_command != PARENT_READY_COMMAND or tty or attach_opencode)
            or not allow_starting and remote_command not in (None, "exec /usr/local/bin/lm-opencode")):
        raise Refused("parent SSH mode is invalid")
    status = read_status(home=home)
    allowed = ("RUNNING", "STARTING") if allow_starting else ("RUNNING",)
    if status["status"] not in allowed:
        raise Refused("parent SSH requires a verified RUNNING parent")
    directory_fd = _open_directory_chain(create=False, home_override=home)
    if directory_fd is None:
        raise Refused("parent state disappeared before SSH setup")
    retained_fds = []
    try:
        parent = _read_json(directory_fd, "parent.json")
        validate_parent(parent)
        _validate_seed_identity(directory_fd, parent)

        def open_private(name: str, maximum: int = 65536) -> tuple[int, bytes]:
            fd = -1
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory_fd)
            except OSError as error:
                raise Refused(f"persistent SSH file is unavailable: {name}") from error
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                    raise Refused(f"persistent SSH file is unsafe: {name}")
                value = bytearray()
                while len(value) <= maximum:
                    chunk = os.read(fd, min(4096, maximum + 1 - len(value)))
                    if not chunk:
                        break
                    value.extend(chunk)
                if len(value) > maximum:
                    raise Refused(f"persistent SSH file exceeds its size bound: {name}")
                retained_fds.append(fd)
                return fd, bytes(value)
            except BaseException:
                if fd >= 0:
                    os.close(fd)
                raise

        def release(fd: int) -> None:
            os.close(fd)
            retained_fds.remove(fd)

        port_fd, port_bytes = open_private("ssh_port", 8)
        release(port_fd)
        port_text = port_bytes.decode("ascii").strip()
        if not re.fullmatch(r"[0-9]{1,5}", port_text) or not 1024 <= int(port_text) <= 65535:
            raise Refused("persistent parent SSH port is invalid")
        known_hosts_file_fd, known_hosts = open_private("known_hosts")
        key_file_fd, private_key = open_private("id_vm_control")
        host_public_fd, host_public_bytes = open_private("ssh_host_ed25519_key.pub", 4096)
        identity = parent.get("ssh_identity")
        if (not isinstance(identity, dict)
                or hashlib.sha256(private_key).hexdigest() != identity.get("control_private_sha256")
                or hashlib.sha256(host_public_bytes).hexdigest() != identity.get("host_public_sha256")):
            raise Refused("persistent SSH identity changed during descriptor validation")
        public_key = host_public_bytes.decode("ascii").strip().split()
        if (len(public_key) < 2 or public_key[0] != "ssh-ed25519"
                or known_hosts != f"[127.0.0.1]:{port_text} {public_key[0]} {public_key[1]}\n".encode()):
            raise Refused("persistent parent SSH host-key pin does not match its seed identity")
        release(host_public_fd)

        def sealed_copy(name: str, content: bytes) -> int:
            if not hasattr(os, "memfd_create") or not hasattr(fcntl, "F_ADD_SEALS"):
                raise Refused("sealed in-memory SSH identity descriptors are unavailable")
            flags = getattr(os, "MFD_CLOEXEC", 1) | getattr(os, "MFD_ALLOW_SEALING", 2)
            try:
                fd = os.memfd_create(name, flags)
            except OSError as error:
                raise Refused("could not create a sealed in-memory SSH identity file") from error
            try:
                os.fchmod(fd, 0o600)
                view = memoryview(content)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise Refused("could not copy validated SSH identity into sealed memory")
                    view = view[written:]
                seals = (getattr(fcntl, "F_SEAL_SEAL", 1) | getattr(fcntl, "F_SEAL_SHRINK", 2)
                         | getattr(fcntl, "F_SEAL_GROW", 4) | getattr(fcntl, "F_SEAL_WRITE", 8))
                fcntl.fcntl(fd, fcntl.F_ADD_SEALS, seals)
                os.lseek(fd, 0, os.SEEK_SET)
                os.set_inheritable(fd, False)
                retained_fds.append(fd)
                return fd
            except BaseException:
                os.close(fd)
                raise

        key_fd = sealed_copy("little-mester-parent-ssh-key", private_key)
        known_hosts_fd = sealed_copy("little-mester-parent-known-hosts", known_hosts)
        release(known_hosts_file_fd)
        release(key_file_fd)
        # OpenSSH closes its own descriptors above stderr during startup, so its
        # /proc/self/fd paths are unusable. Point at this still-live caller's
        # sealed memfds; callers must keep these descriptors open until ssh exits.
        caller_fd_root = f"/proc/{os.getpid()}/fd"
        argv = ["/usr/bin/ssh", "-F", "/dev/null", "-tt" if tty else "-T", "-a", "-x",
                "-p", port_text, "-i", f"{caller_fd_root}/{key_fd}",
                "-o", f"UserKnownHostsFile={caller_fd_root}/{known_hosts_fd}",
                "-o", "GlobalKnownHostsFile=/dev/null", "-o", "StrictHostKeyChecking=yes",
                "-o", "HostKeyAlgorithms=ssh-ed25519", "-o", "PubkeyAcceptedAlgorithms=ssh-ed25519",
                "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none", "-o", "PasswordAuthentication=no",
                "-o", "PreferredAuthentications=publickey", "-o", "ClearAllForwardings=yes",
                "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "PermitLocalCommand=no",
                "-o", "ProxyCommand=none", "-o", "ProxyJump=none", "-o", "EscapeChar=none",
                "mester@127.0.0.1"]
        if remote_command is not None:
            argv.append(remote_command)
        elif attach_opencode:
            argv.append("exec /usr/local/bin/lm-opencode")
        return argv, retained_fds
    except BaseException:
        for fd in retained_fds:
            os.close(fd)
        raise
    finally:
        os.close(directory_fd)


def _run_interactive_ssh(argv: list[str]) -> int:
    """Run SSH while retaining sealed key memfds and forwarding termination safely."""
    if not isinstance(argv, list) or not argv or argv[0] != "/usr/bin/ssh":
        raise Refused("interactive SSH command is invalid")
    managed_signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    child = None
    pidfd = -1
    prior_handlers = {}
    received_signal: int | None = None
    forwarding_failed = False

    def forward(signum, _frame):
        nonlocal received_signal, forwarding_failed
        if received_signal is None:
            received_signal = signum
        if pidfd >= 0:
            try:
                signal.pidfd_send_signal(pidfd, signum)
            except ProcessLookupError:
                # SSH may have exited concurrently; wait() below determines outcome.
                pass
            except (AttributeError, OSError):
                # Do not wait forever if the kernel refuses pidfd signalling. This
                # is our direct child; the bounded wait loop will terminate/reap it.
                forwarding_failed = True

    try:
        if not hasattr(signal, "pidfd_send_signal") or not hasattr(os, "pidfd_open"):
            raise Refused("identity-safe SSH signal forwarding is unavailable")
        for signum in managed_signals:
            prior_handlers[signum] = signal.signal(signum, forward)
        child = subprocess.Popen(argv)
        try:
            pidfd = os.pidfd_open(child.pid, 0)
        except OSError as error:
            # The child is still unreaped, so its PID cannot have been reused.
            child.kill()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                raise Refused("SSH did not exit after pidfd setup failed") from error
            raise Refused("could not bind an identity-safe signal handle to SSH") from error
        if received_signal is not None:
            forward(received_signal, None)
        while True:
            try:
                return_code = child.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if forwarding_failed:
                    child.kill()
                    try:
                        return_code = child.wait(timeout=3)
                    except subprocess.TimeoutExpired as error:
                        raise Refused("SSH did not exit after failed identity-safe signal forwarding") from error
                    break
        if forwarding_failed:
            raise Refused("identity-safe SSH signal forwarding failed; SSH was terminated")
        return 128 + received_signal if received_signal is not None else return_code
    finally:
        cleanup_failed = False
        if child is not None and child.poll() is None:
            try:
                if pidfd >= 0:
                    signal.pidfd_send_signal(pidfd, signal.SIGTERM)
                else:
                    child.terminate()
            except (AttributeError, OSError, ProcessLookupError):
                try:
                    child.kill()
                except OSError:
                    pass
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    if pidfd >= 0:
                        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                    else:
                        child.kill()
                    child.wait(timeout=3)
                except (AttributeError, OSError, ProcessLookupError, subprocess.TimeoutExpired):
                    cleanup_failed = True
        for signum, handler in prior_handlers.items():
            signal.signal(signum, handler)
        if pidfd >= 0:
            os.close(pidfd)
        if cleanup_failed:
            raise Refused("SSH cleanup remained incomplete after bounded termination attempts")


def _valid_supervisor_options(options: list[str], home: str | os.PathLike | None) -> bool:
    if home is not None:
        expected = ["--state-home", str(pathlib.Path(home).resolve())]
        if options[:2] != expected:
            return False
        options = options[2:]
    elif options and options[0] == "--state-home":
        return False
    if not options:
        return True
    if options:
        if (len(options) != 2 or options[0] != "--ssh-port"
                or not re.fullmatch(r"[0-9]{1,5}", options[1])
                or not 1024 <= int(options[1]) <= 65535):
            return False
    return True


def _trusted_parent_runtime_script(path: str, filename: str) -> bool:
    """Accept current checkout code or a protected SHA-pinned installed runtime."""
    current = pathlib.Path(__file__).with_name(filename).resolve()
    candidate = pathlib.Path(path)
    if candidate == current:
        return True
    trusted_root = _installed_runtime_root(candidate, filename)
    if trusted_root is None:
        return False
    try:
        relative = candidate.relative_to(trusted_root)
    except ValueError:
        return False
    if (not isinstance(path, str) or not candidate.is_absolute()
            or path != os.path.abspath(path) or path != str(candidate)
            or len(relative.parts) != 4
            or not re.fullmatch(r"[0-9a-f]{40}", relative.parts[0])
            or relative.parts[1:] != ("lib", "vm", filename)):
        return False
    current_path = pathlib.Path("/")
    parts = candidate.parts[1:]
    for index, component in enumerate(parts):
        current_path /= component
        try:
            info = os.lstat(current_path)
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return False
        if index == len(parts) - 1:
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o644:
                return False
        elif not stat.S_ISDIR(info.st_mode):
            return False
    return True


def _valid_supervisor_schema(parent: dict, run: dict, identity: object,
                             disk: dict | None, home) -> bool:
    invalid = (not isinstance(identity, dict)
            or set(identity) != {"schema", "pid", "uid", "host_boot_id", "start_time", "exe", "argv"}
            or type(identity.get("schema")) is not int or identity["schema"] != 1
            or type(identity.get("pid")) is not int or not 1 < identity["pid"] <= 2**31 - 1
            or type(identity.get("uid")) is not int or identity["uid"] != os.getuid()
            or not isinstance(identity.get("host_boot_id"), str)
            or not isinstance(identity.get("start_time"), str) or not identity["start_time"].isdigit()
            or not isinstance(identity.get("exe"), str) or not identity["exe"].startswith("/")
            or not isinstance(identity.get("argv"), list) or not identity["argv"]
            or not all(isinstance(item, str) for item in identity["argv"])
            or len(identity["argv"]) not in (11, 13, 15)
            or not identity["argv"][0].startswith("/")
            or os.path.realpath(identity["argv"][0]) != identity["exe"]
            or identity["argv"][1] != "-I"
            or not _trusted_parent_runtime_script(identity["argv"][2], "parent-supervisor.py")
            or identity["argv"][3:7] != ["--parent-id", PARENT_ID, "--run-id", run["run_id"]]
            or identity["argv"][7] != "--lock-fd" or not identity["argv"][8].isdigit()
            or identity["argv"][9] != "--handoff-fd" or not identity["argv"][10].isdigit()
            or not _valid_supervisor_options(identity["argv"][11:], home)
            or not parent["disk"]["provisioned"] or disk is None)
    return not invalid


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
    if not _valid_supervisor_schema(parent, run, identity, disk, home):
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
            if (run["phase"] in ("RUNNING", "STOPPING")
                    and identity.get("host_boot_id") != current_boot_id):
                state_fd = _open_directory_chain(create=False, home_override=home)
                if state_fd is not None:
                    try:
                        stopping = dict(run)
                        stopping["phase"] = "STOPPING"
                        stopping.pop("qemu", None)
                        audit = _valid_stale_reconciliation(
                            state_fd, parent, stopping, disk, allow_pending=True)
                        if audit is not None and audit["transaction"] == "COMPLETE":
                            return {"status": "STOPPED", "parent_id": PARENT_ID,
                                    "run_id": run["run_id"], "disk": disk}
                        if audit is not None and audit["transaction"] == "PENDING":
                            return {"status": "RECONCILIATION_PENDING", "parent_id": PARENT_ID,
                                    "run_id": run["run_id"], "disk": disk}
                    except (OSError, Refused, KeyError, TypeError):
                        pass
                    finally:
                        os.close(state_fd)
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
                return {"status": "STOPPED", "parent_id": PARENT_ID,
                        "run_id": run["run_id"], "disk": disk}
            if run["phase"] == "RUNNING":
                qemu_state = _recorded_process_status(
                    run.get("qemu"), expected_basename="qemu-system-x86_64",
                    current_boot_id=current_boot_id)
                if qemu_state == "GONE":
                    return {"status": "STOPPED", "parent_id": PARENT_ID,
                            "run_id": run["run_id"], "disk": disk}
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
    return {"status": run["phase"], "parent_id": PARENT_ID, "run_id": run["run_id"],
            "supervisor_pid": identity["pid"], "disk": disk}


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
    _validate_gate_invocation(gate_argv, gate_identity["exe"], release_token,
                              target_path, identity_path, fd_contracts,
                              "spawned child is not the protected parent gate invocation")
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


def _validate_gate_invocation(argv: list[str], executable: str, release_token: str,
                              target_path: str, identity_path: str,
                              fd_contracts: list[dict], error_message: str) -> None:
    expected_gate = pathlib.Path(__file__).with_name("parent-gate.py").resolve()
    if (not isinstance(argv, list) or len(argv) != 15
            or not all(isinstance(arg, str) for arg in argv)
            or not isinstance(executable, str) or not executable.startswith("/")
            or not argv[0].startswith("/") or os.path.realpath(argv[0]) != executable
            or argv[1] != "-I"
            or (pathlib.Path(argv[2]) != expected_gate
                and not _trusted_parent_runtime_script(argv[2], "parent-gate.py"))
            or argv[3] != "--release-fd" or not re.fullmatch(r"[0-9]{1,10}", argv[4])
            or int(argv[4]) > 2**31 - 1
            or argv[5] != "--token" or argv[6] != release_token
            or argv[7] != "--target" or argv[8] != target_path
            or argv[9] != "--identity" or argv[10] != identity_path
            or argv[11] != "--state-dir-fd" or not re.fullmatch(r"[0-9]{1,10}", argv[12])
            or int(argv[12]) > 2**31 - 1
            or argv[13] != "--disk-fd" or not (argv[14] == "-1"
                                                   or re.fullmatch(r"[0-9]{1,10}", argv[14])
                                                   and int(argv[14]) <= 2**31 - 1)):
        raise Refused(error_message)
    _validate_fd_contracts(fd_contracts)
    if (int(argv[4]) == int(argv[12])
            or bool(fd_contracts) != (argv[14] != "-1")
            or (fd_contracts and int(argv[14]) in (int(argv[4]), int(argv[12])))):
        raise Refused("gate descriptor arguments do not match the FD contract")


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
    argv = gate["argv"]
    if (not isinstance(target, dict) or set(target) != {"exe", "argv", "fd_contracts"}
            or not isinstance(target.get("exe"), str) or not target["exe"].startswith("/")
            or not isinstance(target.get("argv"), list) or not target["argv"]
            or not all(isinstance(arg, str) for arg in target["argv"])
            or not target["argv"][0].startswith("/")
            or os.path.realpath(target["argv"][0]) != target["exe"]):
        raise Refused("invalid gated transition target identity")
    _validate_gate_invocation(argv, gate["exe"], identity["release_token"],
                              identity["target_path"], identity["identity_path"],
                              target["fd_contracts"],
                              "gate identity does not describe the protected invocation")


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
        current = None
    if current is not None and (current["uid"] != identity["uid"]
                                or current["host_boot_id"] != identity["host_boot_id"]
                                or current["start_time"] != identity["start_time"]):
        return "AMBIGUOUS"
    phase = _exec_transition_phase(identity, current) if current is not None else "AMBIGUOUS"
    if phase != "AMBIGUOUS":
        return phase

    # /proc fields are read separately; during the authorized gate execve they
    # can expose an incomplete or mixed exe/argv view. Confirm exactly once.
    # The second sample must independently match the immutable process identity
    # and one of the two recorded images; persistent or replaced identity stays
    # AMBIGUOUS and is never signalled as a target.
    try:
        confirmed = observe_process(pid)
    except Refused:
        return "AMBIGUOUS"
    if (confirmed["uid"] != identity["uid"]
            or confirmed["host_boot_id"] != identity["host_boot_id"]
            or confirmed["start_time"] != identity["start_time"]):
        return "AMBIGUOUS"
    return _exec_transition_phase(identity, confirmed)


def _exec_transition_phase(identity: dict, current: dict) -> str:
    """Classify a complete procfs sample against the only two authorized images."""
    for phase in ("gate", "target"):
        candidate = identity.get(phase)
        expected_keys = {"exe", "argv"} if phase == "gate" else {"exe", "argv", "fd_contracts"}
        if (isinstance(candidate, dict) and set(candidate) == expected_keys
                and isinstance(candidate.get("exe"), str) and isinstance(candidate.get("argv"), list)
                and all(isinstance(arg, str) for arg in candidate["argv"])
                and current["exe"] == candidate["exe"] and current["argv"] == candidate["argv"]):
            return phase.upper()
    return "AMBIGUOUS"


def stop_exec_transition(identity: dict, *, term_grace_ms: int = 5000, settle_ms: int = 2000,
                         authorize_signal=None) -> str:
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
        if authorize_signal is not None:
            authorize_signal()
        signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        if poller.poll(term_grace_ms):
            return "STOPPED"
        if exec_transition_status(identity) not in ("GATE", "TARGET"):
            return "AMBIGUOUS"
        if authorize_signal is not None:
            authorize_signal()
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
    parser.add_argument("command", choices=("status", "recover", "reconcile", "stop", "ssh", "tui"))
    parser.add_argument("--json", action="store_true")
    recovery_mode = parser.add_mutually_exclusive_group()
    recovery_mode.add_argument("--dry-run", action="store_true", help="preview recovery (the default)")
    recovery_mode.add_argument("--execute", action="store_true",
                               help="explicitly recover fully verified orphan children")
    args = parser.parse_args()
    if args.command not in ("recover", "reconcile") and (args.dry_run or args.execute):
        parser.error("--dry-run/--execute is only valid with recover/reconcile")
    if args.command in ("ssh", "tui") and args.json:
        parser.error("--json is not valid with ssh/tui")
    try:
        if args.command in ("ssh", "tui"):
            argv, descriptors = parent_ssh_plan(attach_opencode=args.command == "tui")
            try:
                # Keep the sealed memfds open in this process while OpenSSH reads
                # their /proc/<caller>/fd paths. OpenSSH closes inherited FDs.
                return _run_interactive_ssh(argv)
            finally:
                for fd in descriptors:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
        if args.command == "recover":
            result = recover_parent(execute=args.execute)
        elif args.command == "reconcile":
            result = stale_boot_reconciliation(execute=args.execute)
        elif args.command == "stop":
            result = request_supervisor_stop()
        else:
            result = read_status()
    except Refused as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        if args.command in ("recover", "reconcile"):
            if result.get("dry_run"):
                print(f"{args.command.title()} preview: {result['status']} (dry-run; action={result['action']})")
            else:
                print(f"{args.command.title()}: {result['status']} (action={result['action']}; disk retained)")
            print(result.get("reason", ""))
            return 0 if result["status"] in (
                "NOT_CREATED", "UNPROVISIONED", "STOPPED", "RECOVERABLE", "RECONCILABLE",
                "RECONCILIATION_PENDING") else 2
        if args.command == "stop":
            print(f"Parent supervisor stopped: run_id={result['run_id']}; persistent disk retained")
            return 0
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
    return 0 if result["status"] in (
        "NOT_CREATED", "UNPROVISIONED", "STOPPED", "STARTING", "RUNNING", "RECOVERABLE",
        "RECONCILABLE") else 2


if __name__ == "__main__":
    raise SystemExit(main())
