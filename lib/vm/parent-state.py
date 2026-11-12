#!/usr/bin/env python3
"""Private persistent-parent state, lifecycle lock, and read-only status."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
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
import time
from contextlib import contextmanager


PARENT_ID = "default"
STATE_COMPONENTS = (".local", "state", "little-mester", "parent-v3", PARENT_ID)
SCHEMA = 1
TRUSTED_INSTALL_ROOT = pathlib.Path("/opt/little-mester/v3")
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
        status = read_status(home=home)
        result = {"dry_run": True, "action": "none", "status": status["status"],
                  "parent_id": PARENT_ID}
        if "run_id" in status:
            result["run_id"] = status["run_id"]
        if status["status"] == "AMBIGUOUS":
            result["reason"] = "state is ambiguous; no identity was signalled or changed"
        else:
            result["reason"] = "no recovery action was requested or performed"
        return result
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(directory_fd)


def request_supervisor_stop(*, home: str | os.PathLike | None = None,
                            timeout_seconds: float = 15.0,
                            reap_supervisor=None) -> dict:
    """Request cooperative stop through a pidfd for a verified supervisor."""
    if (type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 60):
        raise Refused("supervisor stop timeout must be between 0 and 60 seconds")
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
        return {"status": "STOPPED", "parent_id": PARENT_ID, "disk_retained": True}
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


def parent_ssh_plan(*, home: str | os.PathLike | None = None,
                    attach_opencode: bool = False, allow_starting: bool = False,
                    remote_command: str | None = None, tty: bool = True) -> tuple[list[str], list[int]]:
    """Build pinned SSH argv and retain validated identity/key descriptors for exec."""
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
        argv = ["/usr/bin/ssh", "-F", "/dev/null", "-tt" if tty else "-T", "-a", "-x",
                "-p", port_text, "-i", f"/proc/self/fd/{key_fd}",
                "-o", f"UserKnownHostsFile=/proc/self/fd/{known_hosts_fd}",
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
    try:
        relative = candidate.relative_to(TRUSTED_INSTALL_ROOT)
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
            or len(identity["argv"]) not in (11, 13, 15)
            or not identity["argv"][0].startswith("/")
            or os.path.realpath(identity["argv"][0]) != identity["exe"]
            or identity["argv"][1] != "-I"
            or not _trusted_parent_runtime_script(identity["argv"][2], "parent-supervisor.py")
            or identity["argv"][3:7] != ["--parent-id", PARENT_ID, "--run-id", run["run_id"]]
            or identity["argv"][7] != "--lock-fd" or not identity["argv"][8].isdigit()
            or identity["argv"][9] != "--handoff-fd" or not identity["argv"][10].isdigit()
            or not _valid_supervisor_options(identity["argv"][11:], home)
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
    parser.add_argument("command", choices=("status", "recover", "stop", "ssh", "tui"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="preview recovery; recovery is always non-mutating in this version")
    args = parser.parse_args()
    if args.command != "recover" and args.dry_run:
        parser.error("--dry-run is only valid with recover")
    if args.command in ("ssh", "tui") and args.json:
        parser.error("--json is not valid with ssh/tui")
    try:
        if args.command in ("ssh", "tui"):
            argv, descriptors = parent_ssh_plan(attach_opencode=args.command == "tui")
            try:
                for fd in descriptors:
                    os.set_inheritable(fd, True)
                os.execv(argv[0], argv)
            finally:
                for fd in descriptors:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
        if args.command == "recover":
            result = recovery_preview()
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
        if args.command == "recover":
            print(f"Recovery preview: {result['status']} (dry-run; action=none)")
            print(result.get("reason", ""))
            return 0
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
    return 0 if result["status"] in ("NOT_CREATED", "UNPROVISIONED", "STOPPED", "STARTING", "RUNNING") else 2


if __name__ == "__main__":
    raise SystemExit(main())
