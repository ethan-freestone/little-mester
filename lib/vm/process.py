#!/usr/bin/env python3
"""Private VM process identity, signalling and bounded diagnostic streams.

PID files are never authority. Every mutating operation verifies process start
ticks, executable and exact argv, then signals through a pidfd to avoid PID reuse.
"""

import argparse
import base64
import binascii
import fcntl
import importlib.util
import ipaddress
import json
import os
import re
import select
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# `paths.py` is loaded dynamically from the frozen runtime. Keep Python caches
# out of that private tree; its files must remain exactly manifest-validated.
sys.dont_write_bytecode = True

_PATHS_SPEC = importlib.util.spec_from_file_location("vm_process_paths", Path(__file__).with_name("paths.py"))
if _PATHS_SPEC is None or _PATHS_SPEC.loader is None:
    raise RuntimeError("VM paths helper is unavailable")
_PATHS = importlib.util.module_from_spec(_PATHS_SPEC)
_PATHS_SPEC.loader.exec_module(_PATHS)

LOG_SEGMENT_BYTES = 10 * 1024 * 1024
MAX_REGISTRY_BYTES = 1024 * 1024
ROLE_RE = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


class ProcessPolicyError(ValueError):
    """Unsafe paths, malformed identities or unsupported process state."""


def require(condition):
    if not condition:
        raise ProcessPolicyError("VM process policy validation failed")


def _private_directory(path):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts)
    parent = path if path.exists() else path.parent
    try:
        require(parent.resolve(strict=True) == parent and parent.is_dir() and not parent.is_symlink())
        info = parent.stat(follow_symlinks=False)
    except OSError as error:
        raise ProcessPolicyError("VM process directory is unavailable") from error
    require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700)
    return path


def _private_regular(path, *, max_bytes=MAX_REGISTRY_BYTES):
    path = Path(path)
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise ProcessPolicyError("VM process record is unavailable") from error
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
            and info.st_size <= max_bytes)
    return info


def _atomic_create(path, payload):
    path = _private_directory(Path(path).parent) / Path(path).name
    require(not path.exists() and not path.is_symlink())
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        os.unlink(temporary)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_replace(path, payload):
    path = Path(path)
    _private_directory(path.parent)
    _private_regular(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _encode(data):
    return (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def _decode(payload):
    def no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result)
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("ascii"), object_pairs_hook=no_duplicates,
                           parse_constant=lambda _: require(False))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ProcessPolicyError("VM process record is malformed") from error
    return value


def _validate_run_id(run_id):
    require(isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id) is not None)
    return run_id


def _validate_role(role):
    require(isinstance(role, str) and ROLE_RE.fullmatch(role) is not None)
    return role


def capture_identity(pid, *, proc_root=Path("/proc")):
    require(type(pid) is int and 1 <= pid <= 2**31 - 1)
    proc_root = Path(proc_root)
    proc = proc_root / str(pid)
    last_error = None
    for _attempt in range(20):
        try:
            raw_stat = (proc / "stat").read_text(encoding="ascii")
            end = raw_stat.rfind(")")
            require(end >= 0)
            fields = raw_stat[end + 1 :].split()
            require(len(fields) > 19)
            state = fields[0]
            start_ticks = int(fields[19])
            executable = os.readlink(proc / "exe")
            raw_argv = (proc / "cmdline").read_bytes()
            require(raw_argv.endswith(b"\0") and len(raw_argv) > 1)
            argv = [os.fsdecode(argument) for argument in raw_argv[:-1].split(b"\0")]
            require(all(argument and "\0" not in argument for argument in argv))
            after_stat = (proc / "stat").read_text(encoding="ascii")
            after_end = after_stat.rfind(")")
            require(after_end >= 0)
            after_fields = after_stat[after_end + 1 :].split()
            require(len(after_fields) > 19 and int(after_fields[19]) == start_ticks)
            require(after_fields[0] not in {"Z", "X"})
            require(start_ticks > 0 and state not in {"Z", "X"})
            return {
                "pid": pid,
                "start_ticks": start_ticks,
                "executable": executable,
                "argv": argv,
            }
        except (OSError, ValueError, IndexError, ProcessPolicyError) as error:
            last_error = error
            if not proc.exists():
                break
            time.sleep(0.005)
    raise ProcessPolicyError("Could not capture VM process identity") from last_error


def _validate_identity(identity):
    require(isinstance(identity, dict) and identity.keys() == {
        "pid", "start_ticks", "executable", "argv"})
    require(type(identity["pid"]) is int and 1 <= identity["pid"] <= 2**31 - 1)
    require(type(identity["start_ticks"]) is int and identity["start_ticks"] > 0)
    require(isinstance(identity["executable"], str) and identity["executable"].startswith("/"))
    require(isinstance(identity["argv"], list) and bool(identity["argv"]))
    require(all(isinstance(item, str) and item and "\0" not in item for item in identity["argv"]))
    return identity


def identity_state(identity, *, proc_root=Path("/proc")):
    try:
        _validate_identity(identity)
        raw_stat = (Path(proc_root) / str(identity["pid"]) / "stat").read_text(encoding="ascii")
        end = raw_stat.rfind(")")
        require(end >= 0)
        fields = raw_stat[end + 1 :].split()
        require(len(fields) > 19)
        current_state = fields[0]
        current_start = int(fields[19])
        if current_start != identity["start_ticks"]:
            return "unknown"
        if current_state in {"Z", "X"}:
            return "stopped"
        current = capture_identity(identity["pid"], proc_root=proc_root)
    except (ProcessPolicyError, OSError, ValueError):
        try:
            if not (Path(proc_root) / str(identity.get("pid", ""))).exists():
                return "stopped"
        except (AttributeError, OSError):
            pass
        return "unknown"
    return "running" if current == identity else "unknown"


def signal_identity(identity, sig, wait_seconds=0, *, proc_root=Path("/proc")):
    _validate_identity(identity)
    require(isinstance(sig, int) and int(sig) in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP))
    sig = int(sig)
    require(isinstance(wait_seconds, (int, float)) and 0 <= wait_seconds <= 3600)
    if identity_state(identity, proc_root=proc_root) != "running":
        return identity_state(identity, proc_root=proc_root)
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return "unknown"
    try:
        pidfd = os.pidfd_open(identity["pid"], 0)
    except (OSError, ProcessLookupError):
        return identity_state(identity, proc_root=proc_root)
    try:
        if identity_state(identity, proc_root=proc_root) != "running":
            return identity_state(identity, proc_root=proc_root)
        try:
            signal.pidfd_send_signal(pidfd, sig, None, 0)
        except ProcessLookupError:
            return "stopped"
        if wait_seconds:
            poller = select.poll()
            poller.register(pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
            if poller.poll(int(wait_seconds * 1000)):
                return "stopped"
        return identity_state(identity, proc_root=proc_root)
    finally:
        os.close(pidfd)


def _registry_lock(path, *, create=False):
    path = Path(path)
    _private_directory(path.parent)
    lock = path.with_name(f".{path.name}.lock")
    try:
        flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
        if create:
            flags |= os.O_CREAT
        fd = os.open(lock, flags, 0o600)
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd
    except OSError as error:
        raise ProcessPolicyError("Could not lock VM process registry") from error


def _registry_validate(data, run_id):
    _validate_run_id(run_id)
    require(isinstance(data, dict) and data.keys() == {"schema_version", "run_id", "processes"})
    require(type(data["schema_version"]) is int and data["schema_version"] == 1)
    require(data["run_id"] == run_id and isinstance(data["processes"], dict))
    for role, item in data["processes"].items():
        _validate_role(role)
        require(isinstance(item, dict) and item.keys() == {"identity", "exit_code"})
        require(item["exit_code"] is None or type(item["exit_code"]) is int)
        if item["identity"] is None:
            require(item["exit_code"] is not None)
        else:
            _validate_identity(item["identity"])
    return data


def _registry_read_unlocked(path, run_id):
    _private_regular(path)
    payload = Path(path).read_bytes()
    return _registry_validate(_decode(payload), run_id)


def registry_initialize(path, run_id):
    _validate_run_id(run_id)
    fd = _registry_lock(path, create=True)
    try:
        _atomic_create(path, _encode({"schema_version": 1, "run_id": run_id, "processes": {}}))
    finally:
        os.close(fd)


def registry_read(path, run_id):
    fd = _registry_lock(path)
    try:
        return _registry_read_unlocked(path, run_id)
    finally:
        os.close(fd)


def _registry_update(path, run_id, update):
    fd = _registry_lock(path)
    try:
        data = _registry_read_unlocked(path, run_id)
        update(data)
        _atomic_replace(path, _encode(data))
        return data
    finally:
        os.close(fd)


def registry_add(path, run_id, role, identity):
    role = _validate_role(role)
    identity = _validate_identity(identity)

    def add(data):
        require(role not in data["processes"])
        data["processes"][role] = {"identity": identity, "exit_code": None}

    _registry_update(path, run_id, add)


def registry_add_exited(path, run_id, role, exit_code):
    role = _validate_role(role)
    require(type(exit_code) is int and -255 <= exit_code <= 255)

    def add(data):
        require(role not in data["processes"])
        data["processes"][role] = {"identity": None, "exit_code": exit_code}

    _registry_update(path, run_id, add)


def registry_finish(path, run_id, role, exit_code):
    role = _validate_role(role)
    require(type(exit_code) is int and -255 <= exit_code <= 255)

    def finish(data):
        require(role in data["processes"])
        data["processes"][role]["exit_code"] = exit_code

    _registry_update(path, run_id, finish)


def registry_status(path, run_id, role=None, *, proc_root=Path("/proc")):
    _validate_run_id(run_id)
    if role is not None:
        _validate_role(role)
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return {"schema_version": 1, "run_id": run_id, "state": "absent", "processes": {}}
    try:
        data = registry_read(path, run_id)
    except (OSError, ProcessPolicyError):
        return {"schema_version": 1, "run_id": run_id, "state": "unknown", "processes": {}}
    selected = [role] if role is not None else list(data["processes"])
    result = {}
    states = []
    for name in selected:
        _validate_role(name)
        item = data["processes"].get(name)
        if item is None:
            state, exit_code = "stopped", None
        elif item["identity"] is None:
            state, exit_code = "stopped", item["exit_code"]
        else:
            state = identity_state(item["identity"], proc_root=proc_root)
            exit_code = item["exit_code"]
        result[name] = {"state": state, "exit_code": exit_code}
        states.append(state)
    if any(state == "unknown" for state in states):
        overall = "unknown"
    elif any(state == "running" for state in states):
        overall = "running"
    else:
        overall = "stopped"
    return {"schema_version": 1, "run_id": run_id, "state": overall, "processes": result}


def registry_signal(path, run_id, role, sig=signal.SIGTERM, wait_seconds=0):
    state = registry_status(path, run_id, role)
    item = state["processes"].get(role)
    if item is None or item["state"] != "running":
        return item["state"] if item is not None else "stopped"
    data = registry_read(path, run_id)
    identity = data["processes"][role]["identity"]
    if identity is None:
        return "stopped"
    result = signal_identity(identity, sig, wait_seconds)
    if result == "stopped":
        return result
    # Recheck after the wait; never treat a PID-only observation as stopped.
    return identity_state(identity)


def registry_stop(path, run_id, wait_seconds=5):
    require(isinstance(wait_seconds, (int, float)) and 0 <= wait_seconds <= 3600)
    data = registry_read(path, run_id)
    outcomes = {}
    # Only logger/supervisor entries created by this run receive rollback TERM.
    for role in reversed(list(data["processes"])):
        if not role.endswith("_logger"):
            continue
        outcomes[role] = registry_signal(path, run_id, role, signal.SIGTERM, wait_seconds)
    # Re-read after logger shutdown: every recorded child must be verified stopped,
    # not merely assumed gone because its supervisor received TERM.
    try:
        final = registry_read(path, run_id)
    except (OSError, ProcessPolicyError):
        return "unknown"
    outcomes.update({
        role: "stopped" if item["identity"] is None else identity_state(item["identity"])
        for role, item in final["processes"].items()
    })
    if any(state == "unknown" for state in outcomes.values()):
        return "unknown"
    if any(state == "running" for state in outcomes.values()):
        return "running"
    return "stopped"


def wait_for_role(path, run_id, role, timeout):
    _validate_role(role)
    require(isinstance(timeout, (int, float)) and 0 <= timeout <= 3600)
    deadline = time.monotonic() + timeout
    while True:
        try:
            data = registry_read(path, run_id)
        except (OSError, ProcessPolicyError):
            return "unknown"
        if role in data["processes"]:
            identity = data["processes"][role]["identity"]
            return "stopped" if identity is None else identity_state(identity)
        if time.monotonic() >= deadline:
            return "absent"
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))


def release_supervisor_lifecycle_fds():
    """Drop inherited references, not LOCK_UN: the controller still owns its locks."""
    for fd_key, path_key, filename in (
        ("VM_HELD_ALLOCATION_FD", "VM_HELD_ALLOCATION_ROOT", ".allocation.lock"),
        ("VM_HELD_PROFILE_FD", "VM_HELD_PROFILE_PATH", ".profile.lock"),
    ):
        raw = os.environ.get(fd_key)
        if raw is None:
            continue
        require(raw.isdecimal() and int(raw) >= 3 and path_key in os.environ)
        fd = int(raw)
        path = Path(os.environ[path_key]) / filename
        info, expected = os.fstat(fd), path.stat(follow_symlinks=False)
        require(stat.S_ISREG(info.st_mode) and info.st_dev == expected.st_dev and info.st_ino == expected.st_ino)
        os.close(fd)
    for key in list(os.environ):
        if key.startswith("VM_HELD_"):
            del os.environ[key]


def run_logged(registry, run_id, role, log_path, argv, max_bytes=LOG_SEGMENT_BYTES):
    release_supervisor_lifecycle_fds()
    _validate_run_id(run_id)
    role = _validate_role(role)
    require(isinstance(argv, list) and bool(argv)
            and all(isinstance(item, str) and item and "\0" not in item for item in argv))
    logger_role = _validate_role(f"{role}_logger")
    interrupt = {"signal": None, "child": None}

    def forward_signal(signum, _frame):
        interrupt["signal"] = signum
        child = interrupt["child"]
        if child is not None and child.poll() is None:
            try:
                # Popen owns this unreaped child; its PID cannot be reused.
                child.send_signal(signum)
            except OSError:
                pass

    previous_handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)
    }
    log_failed = False
    child = None
    rotator = None
    try:
        signal.signal(signal.SIGTERM, forward_signal)
        signal.signal(signal.SIGINT, forward_signal)
        rotator = _PATHS.BoundedLogWriter(log_path, max_bytes)
        logger_identity = capture_identity(os.getpid())
        registry_add(registry, run_id, logger_role, logger_identity)
        if interrupt["signal"] is not None:
            result = 128 + interrupt["signal"]
            registry_add_exited(registry, run_id, role, result)
            registry_finish(registry, run_id, logger_role, result)
            return result
        try:
            child = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                close_fds=True, start_new_session=True, bufsize=0,
            )
        except OSError as error:
            try:
                rotator.write(f"VM process launch failed (errno={error.errno}).\n".encode("ascii"))
            except OSError:
                pass
            registry_add_exited(registry, run_id, role, 127)
            registry_finish(registry, run_id, logger_role, 127)
            return 127
        interrupt["child"] = child
        if interrupt["signal"] is not None and child.poll() is None:
            child.send_signal(interrupt["signal"])
        try:
            identity = capture_identity(child.pid)
        except ProcessPolicyError:
            initial_exit = child.poll()
            if initial_exit is None:
                raise
            registry_add_exited(registry, run_id, role, initial_exit)
        else:
            registry_add(registry, run_id, role, identity)
        assert child.stdout is not None
        while True:
            chunk = child.stdout.read(65536)
            if not chunk:
                break
            if not log_failed:
                try:
                    rotator.write(chunk)
                except OSError:
                    # Continue draining after ENOSPC or another write failure so
                    # QEMU never blocks on a full diagnostic filesystem.
                    log_failed = True
                    try:
                        rotator.close()
                    except OSError:
                        pass
        raw_status = child.wait()
        registry_finish(registry, run_id, role, raw_status)
        process_exit = raw_status if raw_status >= 0 else 128 + abs(raw_status)
        registry_finish(registry, run_id, logger_role, process_exit)
        return process_exit
    except BaseException:
        if child is not None and child.poll() is None:
            try:
                child.send_signal(signal.SIGTERM)
            except OSError:
                pass
        raise
    finally:
        if rotator is not None:
            try:
                rotator.close()
            except OSError:
                pass
        if child is not None and child.stdout is not None:
            child.stdout.close()
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)


def _decode_proc_ipv4(value):
    try:
        address, port = value.split(":", 1)
        parsed = ipaddress.IPv4Address(bytes.fromhex(address)[::-1])
        return parsed, int(port, 16)
    except (ValueError, TypeError):
        raise ProcessPolicyError("Malformed host TCP table") from None


def listener_owned_by(pid, port, *, proc_root=Path("/proc")):
    require(type(pid) is int and pid > 0 and type(port) is int and 1 <= port <= 65535)
    proc_root = Path(proc_root)
    try:
        rows = (proc_root / "net/tcp").read_text(encoding="ascii").splitlines()
        candidates = []
        for row in rows[1:]:
            fields = row.split()
            require(len(fields) >= 10)
            local, state, inode = fields[1], fields[3], fields[9]
            address, local_port = _decode_proc_ipv4(local)
            if local_port == port and state == "0A":
                candidates.append((address, inode))
        if len(candidates) != 1 or candidates[0][0] != ipaddress.IPv4Address("127.0.0.1"):
            return False
        inode = candidates[0][1]
        fds = (proc_root / str(pid) / "fd").iterdir()
        return any(os.readlink(fd) == f"socket:[{inode}]" for fd in fds)
    except (OSError, ProcessPolicyError):
        return False


def _host_key_line(payload, expected_host):
    try:
        lines = [line.strip() for line in payload.decode("ascii").splitlines() if line.strip()]
    except UnicodeError as error:
        raise ProcessPolicyError("SSH host-key scan was malformed") from error
    require(len(lines) == 1)
    fields = lines[0].split()
    require(len(fields) == 3 and fields[0] == expected_host and fields[1] == "ssh-ed25519")
    try:
        blob = base64.b64decode(fields[2], validate=True)
    except (binascii.Error, ValueError) as error:
        raise ProcessPolicyError("SSH host-key scan was malformed") from error
    require(len(blob) >= 4)
    length = int.from_bytes(blob[:4], "big")
    require(length == len(b"ssh-ed25519") and blob[4 : 4 + length] == b"ssh-ed25519")
    key_length_offset = 4 + length
    require(key_length_offset + 4 <= len(blob))
    key_length = int.from_bytes(blob[key_length_offset : key_length_offset + 4], "big")
    require(key_length == 32 and key_length_offset + 4 + key_length == len(blob))
    return " ".join(fields)


def trust_host_key(path, scan_output, expected_host="127.0.0.1", port=22):
    require(expected_host == "127.0.0.1")
    require(type(port) is int and 1 <= port <= 65535)
    scan_host = expected_host if port == 22 else f"[{expected_host}]:{port}"
    require(isinstance(scan_output, bytes) and len(scan_output) <= 65536)
    require(all(byte in (9, 10, 13) or 32 <= byte <= 126 for byte in scan_output))
    # Scanner commentary is not key data. Filter it only at this boundary;
    # existing known_hosts below still requires exactly one bare valid key.
    records = [line.strip() for line in scan_output.splitlines()
               if line.strip() and not line.strip().startswith(b"#")]
    scanned = _host_key_line(b"\n".join(records), scan_host)
    _, key_type, key_data = scanned.split()
    line = f"{expected_host} {key_type} {key_data}"
    path = Path(path)
    _private_directory(path.parent)
    payload = (line + "\n").encode("ascii")
    if path.exists() or path.is_symlink():
        info = _private_regular(path, max_bytes=4096)
        del info
        existing = _host_key_line(path.read_bytes(), expected_host)
        require(existing == line)
        return "matched"
    try:
        _atomic_create(path, payload)
    except FileExistsError:
        _private_regular(path, max_bytes=4096)
        require(_host_key_line(path.read_bytes(), expected_host) == line)
        return "matched"
    return "created"


def proxy_ready(registry, run_id, port_file, marker_file):
    def read_private(path):
        path = Path(path)
        _private_regular(path, max_bytes=4096)
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            payload = os.read(fd, 4097)
            require(len(payload) <= 4096)
            return payload
        finally:
            os.close(fd)

    raw_port = read_private(port_file)
    require(re.fullmatch(rb"[1-9][0-9]{0,4}\n", raw_port) is not None)
    port = int(raw_port[:-1])
    require(port <= 65535)
    marker = _decode(read_private(marker_file))
    require(isinstance(marker, dict) and marker.keys() == {
        "schema_version", "pid", "start_ticks", "executable", "port"})
    require(type(marker["schema_version"]) is int and marker["schema_version"] == 1)
    require(type(marker["pid"]) is int and marker["pid"] > 0)
    require(isinstance(marker["start_ticks"], str) and marker["start_ticks"].isdigit())
    require(isinstance(marker["executable"], str) and marker["executable"].startswith("/"))
    require(marker["port"] == port)
    state = registry_status(registry, run_id, "proxy")
    require(state["processes"].get("proxy", {}).get("state") == "running")
    data = registry_read(registry, run_id)
    identity = data["processes"]["proxy"]["identity"]
    require(marker["pid"] == identity["pid"]
            and marker["start_ticks"] == str(identity["start_ticks"])
            and marker["executable"] == identity["executable"])
    require(listener_owned_by(identity["pid"], port))
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    except OSError as error:
        raise ProcessPolicyError("Proxy readiness listener is unavailable") from error
    require(identity_state(identity) == "running" and listener_owned_by(identity["pid"], port))
    return port


def write_failure(path, run_id, stage, cause_exit_code, diagnostics):
    _validate_run_id(run_id)
    require(isinstance(stage, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", stage))
    require(cause_exit_code is None or type(cause_exit_code) is int)
    require(isinstance(diagnostics, list) and len(diagnostics) <= 8)
    safe_diagnostics = []
    for item in diagnostics:
        require(isinstance(item, str) and item.startswith("/") and "\n" not in item and "\r" not in item)
        safe_diagnostics.append(item)
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "stage": stage,
        "cause_exit_code": cause_exit_code,
        "diagnostics": safe_diagnostics,
    }
    path = Path(path)
    _atomic_create(path, _encode(report))
    return report


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--registry", required=True, type=Path)
    init.add_argument("--run-id", required=True)
    status = commands.add_parser("status")
    status.add_argument("--registry", required=True, type=Path)
    status.add_argument("--run-id", required=True)
    status.add_argument("--role")
    stop = commands.add_parser("stop")
    stop.add_argument("--registry", required=True, type=Path)
    stop.add_argument("--run-id", required=True)
    stop.add_argument("--wait", type=float, default=5)
    wait_role = commands.add_parser("wait-role")
    wait_role.add_argument("--registry", required=True, type=Path)
    wait_role.add_argument("--run-id", required=True)
    wait_role.add_argument("--role", required=True)
    wait_role.add_argument("--timeout", type=float, required=True)
    signal_cmd = commands.add_parser("signal")
    signal_cmd.add_argument("--registry", required=True, type=Path)
    signal_cmd.add_argument("--run-id", required=True)
    signal_cmd.add_argument("--role", required=True)
    signal_cmd.add_argument("--signal", choices=("TERM", "INT", "HUP"), default="TERM")
    signal_cmd.add_argument("--wait", type=float, default=0)
    launch = commands.add_parser("run-logged")
    launch.add_argument("--registry", required=True, type=Path)
    launch.add_argument("--run-id", required=True)
    launch.add_argument("--role", required=True)
    launch.add_argument("--log", required=True, type=Path)
    launch.add_argument("--max-bytes", type=int, default=LOG_SEGMENT_BYTES)
    launch.add_argument("command_argv", nargs=argparse.REMAINDER)
    listener = commands.add_parser("listener-owned")
    listener.add_argument("--registry", required=True, type=Path)
    listener.add_argument("--run-id", required=True)
    listener.add_argument("--role", required=True)
    listener.add_argument("--port", required=True, type=int)
    proxy_ready_cmd = commands.add_parser("proxy-ready")
    proxy_ready_cmd.add_argument("--registry", required=True, type=Path)
    proxy_ready_cmd.add_argument("--run-id", required=True)
    proxy_ready_cmd.add_argument("--port-file", required=True, type=Path)
    proxy_ready_cmd.add_argument("--marker-file", required=True, type=Path)
    key = commands.add_parser("trust-key")
    key.add_argument("--known-hosts", required=True, type=Path)
    key.add_argument("--host", default="127.0.0.1")
    key.add_argument("--port", type=int, default=22)
    failure = commands.add_parser("failure")
    failure.add_argument("--path", required=True, type=Path)
    failure.add_argument("--run-id", required=True)
    failure.add_argument("--stage", required=True)
    failure.add_argument("--cause-exit-code", type=int)
    failure.add_argument("--diagnostic", action="append", default=[])
    return parser.parse_args()


def main():
    args = _parse_args()
    if args.command == "init":
        registry_initialize(args.registry, args.run_id)
        return 0
    if args.command == "status":
        result = registry_status(args.registry, args.run_id, args.role)
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
        return 0 if result["state"] != "unknown" else 2
    if args.command == "signal":
        sig = {"TERM": signal.SIGTERM, "INT": signal.SIGINT, "HUP": signal.SIGHUP}[args.signal]
        state = registry_signal(args.registry, args.run_id, args.role, sig, args.wait)
        sys.stdout.write(state + "\n")
        return 0 if state in {"stopped", "running"} else 2
    if args.command == "stop":
        state = registry_stop(args.registry, args.run_id, args.wait)
        sys.stdout.write(state + "\n")
        return 0 if state == "stopped" else 2
    if args.command == "wait-role":
        state = wait_for_role(args.registry, args.run_id, args.role, args.timeout)
        sys.stdout.write(state + "\n")
        return 0 if state == "running" else 2
    if args.command == "run-logged":
        argv = args.command_argv
        if argv and argv[0] == "--":
            argv = argv[1:]
        return run_logged(args.registry, args.run_id, args.role, args.log, argv, args.max_bytes)
    if args.command == "listener-owned":
        state = registry_status(args.registry, args.run_id, args.role)
        if state["processes"].get(args.role, {}).get("state") != "running":
            return 2
        data = registry_read(args.registry, args.run_id)
        identity = data["processes"][args.role]["identity"]
        if not listener_owned_by(identity["pid"], args.port):
            return 2
        return 0 if identity_state(identity) == "running" else 2
    if args.command == "proxy-ready":
        port = proxy_ready(args.registry, args.run_id, args.port_file, args.marker_file)
        sys.stdout.write(f"{port}\n")
        return 0
    if args.command == "trust-key":
        scan = sys.stdin.buffer.read(65537)
        try:
            result = trust_host_key(args.known_hosts, scan, args.host, args.port)
        except (ProcessPolicyError, OSError, ValueError, TypeError):
            lines = [line.strip() for line in scan.splitlines() if line.strip()]
            comments = sum(line.startswith(b"#") for line in lines)
            # Counts only: never return key material or filesystem error text.
            print(f"scan-lines={len(lines)},comments={comments},records={len(lines) - comments}")
            return 2
        sys.stdout.write(result + "\n")
        return 0
    if args.command == "failure":
        report = write_failure(args.path, args.run_id, args.stage, args.cause_exit_code, args.diagnostic)
        sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
        return 0
    raise ProcessPolicyError("Unsupported VM process command")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ProcessPolicyError, OSError, ValueError, TypeError):
        print("VM process operation refused; identity or private state did not validate", file=sys.stderr)
        sys.exit(2)
