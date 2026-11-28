#!/usr/bin/env python3
"""Fixed systemd-owned VM runtime and owner command implementation.

The privileged fixed-service launcher holds a shared update lock, drops to the
unit's fixed identity, and execs the candidate-built service command. QEMU
therefore remains systemd's MainPID while the lock lives for the service
lifetime. No operation accepts a unit, path, QEMU option, or remote command
from its caller.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import grp
import importlib.util
import hashlib
import json
import os
import pathlib
import pwd
import re
import secrets
import select
import selectors
import shutil
import socket
import stat
import struct
import signal
import subprocess
import sys
import time


INSTANCE_POLICY_PATH = pathlib.Path(__file__).with_name("vm-instance.py")
INSTANCE_POLICY_SPEC = importlib.util.spec_from_file_location(
    "system_vm_instance_policy", INSTANCE_POLICY_PATH)
if INSTANCE_POLICY_SPEC is None or INSTANCE_POLICY_SPEC.loader is None:
    raise RuntimeError("fixed VM instance descriptor is unavailable")
INSTANCE_POLICY = importlib.util.module_from_spec(INSTANCE_POLICY_SPEC)
sys.modules[INSTANCE_POLICY_SPEC.name] = INSTANCE_POLICY
INSTANCE_POLICY_SPEC.loader.exec_module(INSTANCE_POLICY)
INSTANCE = INSTANCE_POLICY.Instance.singleton()
STAGE_REPORTING_SPEC = importlib.util.spec_from_file_location(
    "system_vm_stage_reporting", pathlib.Path(__file__).with_name("vm-stage-reporting.py"))
if STAGE_REPORTING_SPEC is None or STAGE_REPORTING_SPEC.loader is None:
    raise RuntimeError("shared VM stage reporter is unavailable")
STAGE_REPORTING = importlib.util.module_from_spec(STAGE_REPORTING_SPEC)
STAGE_REPORTING_SPEC.loader.exec_module(STAGE_REPORTING)
StageTimer = STAGE_REPORTING.StageTimer

SERVICE_USER = INSTANCE.vm_user
PROXY_USER = INSTANCE.proxy_user
CONTROL_GROUP = "lm-vm-control"
KVM_GROUP = "kvm"
PR_SET_NO_NEW_PRIVS = 38
NNP_REQUIRED_ROLES = frozenset({"qemu", "proxy", "metrics"})
STATE = INSTANCE.state_root
CONFIG = INSTANCE.config_root
DISK = INSTANCE.disk
SEED = INSTANCE.seed
CONTROL_KEY = INSTANCE.control_key
HOST_KEY = INSTANCE.host_key
KNOWN_HOSTS = INSTANCE.known_hosts
QMP_RUNTIME = INSTANCE.qmp_runtime
QMP_SOCKET_PATH = INSTANCE.qmp_socket
PROXY_RUNTIME = INSTANCE.proxy_runtime
PROXY_PORT_FILE = INSTANCE.proxy_port_file
SSH_PORT = INSTANCE.ssh_port
PROXY_PORT = INSTANCE.proxy_port
WORKSPACE_SOCKET = INSTANCE.workspace_socket
QMP_STOP_TIMEOUT = 18.0
SSH_READY_TIMEOUT = 900.0
IMAGE_SHA256 = "04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94"
IMAGE_URL = "https://cloud-images.ubuntu.com/releases/noble/release-20261017/ubuntu-24.04-server-cloudimg-amd64.img"
IMAGE_NAME = "base-image.part"
DISK_PART = "disk.qcow2.part"
CODE_UPDATE_LOCK = CONFIG / "code-update.lock"
CODE_UPDATE_MARKER = CONFIG / "code-update.in-progress"
GUEST_COMPAT_MAX_OUTPUT = 8192
CLOUD_INIT_STATUS_COMMAND = "cloud-init status --format=json"
GUEST_PROVISIONED_COMMAND = (
    "test -f /var/lib/mester-v3-provisioned && "
    "test ! -L /var/lib/mester-v3-provisioned")


class Refused(RuntimeError):
    pass


class GuestSSHTimeout(Refused):
    """A bounded captured guest command timed out; callers may retry by policy."""

    def __init__(self, message: str, *, stderr: bytes = b""):
        super().__init__(message)
        self.stderr = stderr


def _require_fixed_file(path: pathlib.Path, *, uid: int, mode: int,
                        regular: bool = True, group: str | None = None) -> os.stat_result:
    try:
        info = os.lstat(path)
    except OSError as error:
        raise Refused(f"fixed VM asset is unavailable: {path.name}") from error
    if (stat.S_ISLNK(info.st_mode) or (regular and not stat.S_ISREG(info.st_mode))
            or info.st_uid != uid or stat.S_IMODE(info.st_mode) != mode
            or info.st_nlink != 1):
        raise Refused(f"fixed VM asset has unsafe type, owner, mode, or link count: {path.name}")
    if group is not None:
        try:
            gid = grp.getgrnam(group).gr_gid
        except KeyError as error:
            raise Refused("fixed VM control group is unavailable") from error
        if info.st_gid != gid or (gid not in os.getgroups() and gid != os.getegid()):
            raise Refused("fixed VM asset is not readable through the exact control group")
    return info


def _verify_kvm_access() -> None:
    try:
        kvm_gid = grp.getgrnam(KVM_GROUP).gr_gid
        info = os.stat("/dev/kvm", follow_symlinks=False)
    except (KeyError, OSError) as error:
        raise Refused("KVM device or exact kvm group is unavailable") from error
    if (not stat.S_ISCHR(info.st_mode) or info.st_gid != kvm_gid
            or stat.S_IMODE(info.st_mode) & 0o060 != 0o060
            or kvm_gid not in os.getgroups()):
        raise Refused("/dev/kvm must be group-owned by kvm and accessible through the service's exact supplementary group")
    if not os.access("/dev/kvm", os.R_OK | os.W_OK, effective_ids=True):
        raise Refused("service identity lacks DAC read/write access to /dev/kvm")


def _set_no_new_privs() -> None:
    """Make the post-drop service exec non-escalating despite systemd's '+' prefix."""
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                      ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    if prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        detail = os.strerror(error) if error else "unknown prctl error"
        raise Refused(f"could not set no_new_privs for locked service: {detail}")


def _locked_service_exec(kind: str, owner: str | None = None) -> None:
    """Take a root-opened shared lock, drop to a fixed unit identity, then exec."""
    if os.geteuid() != 0:
        raise Refused("locked service launcher must be invoked by its fixed systemd unit")
    if kind not in ("qemu", "proxy", "workspace", "metrics"):
        raise Refused("unsupported fixed locked service role")
    if kind == "workspace":
        if not isinstance(owner, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", owner):
            raise Refused("fixed workspace unit owner is invalid")
        service_name = owner
    else:
        if owner is not None:
            raise Refused("unexpected owner for fixed service role")
        service_name = {"qemu": SERVICE_USER, "proxy": PROXY_USER,
                        "metrics": SERVICE_USER}[kind]
    try:
        service = pwd.getpwnam(service_name)
        if service.pw_uid == 0:
            raise Refused("root cannot be a fixed service identity")
        control_gid = grp.getgrnam(CONTROL_GROUP).gr_gid
        kvm_gid = grp.getgrnam(KVM_GROUP).gr_gid if kind == "qemu" else None
    except KeyError as error:
        raise Refused("fixed locked service identity is unavailable") from error
    fd = os.open(CODE_UPDATE_LOCK, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0
                or info.st_gid != control_gid or stat.S_IMODE(info.st_mode) != 0o660):
            raise Refused("fixed code-update lock identity is unsafe")
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Refused("fixed service start is blocked by code update/rollback") from error
        if CODE_UPDATE_MARKER.exists() or CODE_UPDATE_MARKER.is_symlink():
            raise Refused("fixed service start is blocked by incomplete code update/rollback")
        groups = [control_gid] if kind in ("qemu", "workspace") else []
        if kvm_gid is not None:
            groups.append(kvm_gid)
        os.setgroups(sorted(set(groups)))
        os.setgid(service.pw_gid)
        os.setuid(service.pw_uid)
        if kind in NNP_REQUIRED_ROLES:
            _set_no_new_privs()
        elif kind != "workspace":
            raise Refused("locked service role has no explicit no_new_privs policy")
        if kind == "qemu":
            argv = qemu_argv()
        elif kind == "proxy":
            argv = ["/usr/bin/python3", "-I", str(pathlib.Path(__file__).with_name("proxy.py")),
                    "--port-file", str(PROXY_PORT_FILE), "--port", str(PROXY_PORT)]
        elif kind == "workspace":
            argv = ["/usr/bin/python3", "-I", str(pathlib.Path(__file__).with_name("system-vm-workspace.py")),
                    "serve"]
        else:
            argv = ["/usr/bin/python3", "-I", str(pathlib.Path(__file__).with_name("system-vm-metrics.py"))]
        os.set_inheritable(fd, True)
        os.execv(argv[0], argv)
    finally:
        os.close(fd)


def _validate_runtime_directory(path: pathlib.Path = QMP_RUNTIME, *,
                                service_uid: int | None = None) -> None:
    try:
        info = os.lstat(path)
        expected_uid = (pwd.getpwnam(SERVICE_USER).pw_uid
                        if service_uid is None else service_uid)
    except (KeyError, OSError) as error:
        raise Refused("private QMP RuntimeDirectory is unavailable") from error
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != expected_uid or stat.S_IMODE(info.st_mode) != 0o700):
        raise Refused("QMP RuntimeDirectory must be private to the dedicated VM service identity")


def qemu_argv() -> list[str]:
    """Construct the system unit's fixed direct-QEMU argv for validation."""
    _verify_kvm_access()
    uid = os.geteuid()
    _require_fixed_file(DISK, uid=uid, mode=0o600)
    _require_fixed_file(SEED, uid=0, mode=0o640, group=SERVICE_USER)
    _validate_runtime_directory()
    _verify_workspace_share()
    if QMP_SOCKET_PATH.exists() or QMP_SOCKET_PATH.is_symlink():
        raise Refused("preserving pre-existing QMP socket evidence")
    proxy = read_proxy_port()
    module_path = pathlib.Path(__file__).with_name("parent-qemu.py")
    import importlib.util
    spec = importlib.util.spec_from_file_location("system_parent_qemu", module_path)
    if spec is None or spec.loader is None:
        raise Refused("shared QEMU argv builder is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_system_qemu_argv(
        executable="/usr/bin/qemu-system-x86_64", disk_path=str(DISK), seed_iso=str(SEED),
        ssh_port=SSH_PORT, proxy_port=proxy, qmp_socket=str(QMP_SOCKET_PATH),
        vm_name=INSTANCE.name, virtiofs_socket=str(WORKSPACE_SOCKET),
        cpus=INSTANCE.cpus, memory_mib=INSTANCE.memory_mib,
        metrics_port=INSTANCE.metrics_port)


def _verify_workspace_share() -> None:
    workspace_helper = pathlib.Path(__file__).with_name("system-vm-workspace.py")
    spec = importlib.util.spec_from_file_location("system_vm_workspace_policy", workspace_helper)
    if spec is None or spec.loader is None:
        raise Refused("fixed workspace policy helper is unavailable")
    workspace = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = workspace
    spec.loader.exec_module(workspace)
    try:
        workspace.verify_service_socket()
    except (OSError, workspace.Refused) as error:
        raise Refused("fixed host workspace share is not ready") from error


def read_proxy_port() -> int:
    """Use only the fixed loopback listener; proxy identity stays separate."""
    port = PROXY_PORT
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    except OSError as error:
        raise Refused("restricted egress proxy is not accepting connections") from error
    return port


def _recv_qmp_line(client: socket.socket, deadline: float, buffer: bytearray) -> dict:
    while b"\n" not in buffer:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Refused("QMP exchange exceeded its bounded deadline")
        client.settimeout(remaining)
        chunk = client.recv(min(4096, 65536 - len(buffer)))
        if not chunk:
            raise Refused("QMP peer closed before completing its response")
        buffer.extend(chunk)
        if len(buffer) > 65536:
            raise Refused("QMP response exceeded its fixed size bound")
    line, _, remainder = buffer.partition(b"\n")
    buffer[:] = remainder
    try:
        value = json.loads(line)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise Refused("QMP peer returned malformed JSON") from error
    if not isinstance(value, dict):
        raise Refused("QMP peer returned an invalid message")
    return value


def _open_qmp_peer(pid: int, *, qmp_runtime: pathlib.Path,
                   qmp_socket_path: pathlib.Path, service_uid: int,
                   deadline: float, pidfd: int):
    _check_qmp_pidfd(pidfd)
    _validate_runtime_directory(qmp_runtime, service_uid=service_uid)
    if qmp_socket_path != qmp_runtime / "qmp.sock":
        raise Refused("QMP socket is outside its fixed private runtime directory")
    try:
        sock_info = os.lstat(qmp_socket_path)
    except OSError as error:
        raise Refused("private QMP socket is unavailable") from error
    if not stat.S_ISSOCK(sock_info.st_mode) or sock_info.st_uid != service_uid:
        raise Refused("QMP socket has unsafe type or owner")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(min(2.0, max(0.1, deadline - time.monotonic())))
        client.connect(str(qmp_socket_path))
        peer = struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if peer[0] != pid or peer[1] != service_uid:
            raise Refused("QMP peer differs from the exact QEMU PID and service identity")
        _check_qmp_pidfd(pidfd)
        buffer = bytearray()
        if not isinstance(_recv_qmp_line(client, deadline, buffer).get("QMP"), dict):
            raise Refused("QMP peer did not send its protocol greeting")
        client.settimeout(max(0.1, deadline - time.monotonic()))
        client.sendall((json.dumps({"execute": "qmp_capabilities"}, separators=(",", ":"))
                        + "\r\n").encode())
        while True:
            response = _recv_qmp_line(client, deadline, buffer)
            if "event" in response:
                continue
            if "error" in response or not isinstance(response.get("return"), dict):
                raise Refused("QMP rejected its fixed capability negotiation")
            break
        return client, pidfd, buffer
    except (OSError, TimeoutError):
        client.close()
        raise Refused("identity-bound QMP connection could not be completed")
    except BaseException:
        client.close()
        raise


def _check_qmp_pidfd(pidfd: int) -> None:
    poller = select.poll()
    poller.register(pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
    if poller.poll(0):
        raise Refused("QEMU MainPID exited during identity-bound QMP operation")


def qmp_powerdown_and_wait(pid: int, *, timeout_seconds: float = QMP_STOP_TIMEOUT,
                           qmp_runtime: pathlib.Path | None = None,
                           qmp_socket_path: pathlib.Path | None = None,
                           service_uid: int | None = None) -> None:
    """Send identity-bound QMP powerdown and wait for actual QEMU process exit."""
    if type(pid) is not int or pid <= 1 or not 0 < timeout_seconds <= 20:
        raise Refused("invalid fixed QMP stop request")
    qmp_runtime = QMP_RUNTIME if qmp_runtime is None else pathlib.Path(qmp_runtime)
    qmp_socket_path = QMP_SOCKET_PATH if qmp_socket_path is None else pathlib.Path(qmp_socket_path)
    service_uid = os.geteuid() if service_uid is None else service_uid
    try:
        pidfd = os.pidfd_open(pid, 0)
    except OSError as error:
        # systemd can race ExecStop after QEMU has already exited.
        if error.errno == errno.ESRCH:
            return
        raise Refused("could not bind shutdown to the systemd MainPID") from error
    deadline = time.monotonic() + timeout_seconds
    client = None
    try:
        client, pidfd, buffer = _open_qmp_peer(
            pid, qmp_runtime=qmp_runtime, qmp_socket_path=qmp_socket_path,
            service_uid=service_uid, deadline=deadline, pidfd=pidfd)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Refused("QMP exchange exceeded its bounded deadline")
        client.settimeout(remaining)
        client.sendall((json.dumps({"execute": "system_powerdown"}, separators=(",", ":"))
                        + "\r\n").encode())
        while True:
            response = _recv_qmp_line(client, deadline, buffer)
            if "event" in response:
                continue
            if "error" in response or not isinstance(response.get("return"), dict):
                raise Refused("QMP rejected fixed graceful-shutdown command")
            break
        poller = select.poll()
        poller.register(pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        if not poller.poll(remaining_ms):
            raise Refused("QEMU did not exit after bounded QMP system_powerdown; systemd KILL fallback required")
    except (OSError, TimeoutError) as error:
        raise Refused("could not complete identity-bound bounded QMP shutdown") from error
    finally:
        if client is not None:
            client.close()
        os.close(pidfd)


def qmp_query_status(pid: int, *, qmp_runtime: pathlib.Path | None = None,
                     qmp_socket_path: pathlib.Path | None = None,
                     service_uid: int | None = None,
                     timeout_seconds: float = 2.0) -> dict[str, bool | str]:
    """Return only allowlisted QMP status fields from the identity-pinned peer."""
    if type(pid) is not int or pid <= 1 or not 0 < timeout_seconds <= 5:
        raise Refused("invalid bounded QMP observation request")
    qmp_runtime = QMP_RUNTIME if qmp_runtime is None else pathlib.Path(qmp_runtime)
    qmp_socket_path = QMP_SOCKET_PATH if qmp_socket_path is None else pathlib.Path(qmp_socket_path)
    service_uid = os.geteuid() if service_uid is None else service_uid
    deadline = time.monotonic() + timeout_seconds
    try:
        pidfd = os.pidfd_open(pid, 0)
    except OSError as error:
        raise Refused("could not bind QMP observation to the exact QEMU PID") from error
    try:
        client, pidfd, buffer = _open_qmp_peer(
            pid, qmp_runtime=qmp_runtime, qmp_socket_path=qmp_socket_path,
            service_uid=service_uid, deadline=deadline, pidfd=pidfd)
    except BaseException:
        os.close(pidfd)
        raise
    try:
        client.settimeout(max(0.1, deadline - time.monotonic()))
        client.sendall((json.dumps({"execute": "query-status"}, separators=(",", ":"))
                        + "\r\n").encode())
        while True:
            response = _recv_qmp_line(client, deadline, buffer)
            if "event" in response:
                continue
            result = response.get("return")
            if "error" in response or not isinstance(result, dict):
                raise Refused("QMP status observation was rejected")
            status = result.get("status")
            running = result.get("running")
            single_step = result.get("singlestep")
            if (not isinstance(status, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", status)
                    or type(running) is not bool or type(single_step) is not bool):
                raise Refused("QMP status observation has an invalid fixed schema")
            return {"status": status, "running": running, "singlestep": single_step}
    finally:
        client.close()
        os.close(pidfd)


def _proxy_ready() -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if read_proxy_port() == PROXY_PORT:
                return
        except (OSError, Refused, KeyError):
            time.sleep(0.1)
    raise Refused("restricted proxy did not publish the fixed accepting listener")


def guest_ssh_argv(*, ssh_port: int, identity_file: pathlib.Path,
                   known_hosts: pathlib.Path, tty: bool = False) -> list[str]:
    """Build the one strict host-key-pinned SSH command used by every VM instance."""
    if type(ssh_port) is not int or not 1024 <= ssh_port <= 65535:
        raise Refused("guest SSH port is outside its fixed unprivileged range")
    for label, path in (("identity", identity_file), ("known-hosts", known_hosts)):
        text = os.fspath(path)
        if not isinstance(text, str) or not text.startswith("/") or any(
                character in text for character in ("\0", "\n", "\r")):
            raise Refused(f"guest SSH {label} path is not a fixed absolute path")
    return ["/usr/bin/ssh", "-F", "/dev/null", *( ["-tt"] if tty else []),
            "-p", str(ssh_port), "-i", str(identity_file),
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ProxyCommand=none",
            "-o", "ProxyJump=none", "-o", "ConnectTimeout=5", "-o", "ClearAllForwardings=yes",
            "-o", "HostKeyAlgorithms=ssh-ed25519", "-o", "PubkeyAcceptedAlgorithms=ssh-ed25519",
            "-o", "CheckHostIP=no", "-o", "UpdateHostKeys=no", "mester@127.0.0.1"]


def _read_guest_ssh_asset(path: pathlib.Path, *, expected_uid: int | None,
                          mode: int, limit: int, group: str | None = None) -> bytes:
    if expected_uid is None:
        _require_fixed_file(path, uid=0, mode=mode, group=group)
    else:
        try:
            before = os.lstat(path)
        except OSError as error:
            raise Refused("profile guest SSH identity is unavailable") from error
        if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
                or before.st_uid != expected_uid or stat.S_IMODE(before.st_mode) != mode
                or before.st_nlink != 1 or before.st_size > limit):
            raise Refused("profile guest SSH identity has unsafe owner, mode, or size")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != mode
                or (expected_uid is not None and info.st_uid != expected_uid)
                or info.st_size > limit):
            raise Refused("guest SSH credential changed while opening")
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(fd, min(4096, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > limit:
            raise Refused("guest SSH credential exceeds its fixed size bound")
        return bytes(data)
    finally:
        os.close(fd)


def _write_guest_ssh_stage(path: pathlib.Path, payload: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise Refused("temporary guest SSH credential write made no progress")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _run_guest_ssh(argv: list[str], *, tty: bool, timeout: float,
                   capture_limit: int | None, capture_stderr_limit: int):
    if capture_limit is not None and (
            type(capture_limit) is not int or not 0 < capture_limit <= GUEST_COMPAT_MAX_OUTPUT):
        raise Refused("bounded guest inspection output limit is invalid")
    if type(capture_stderr_limit) is not int or not 0 <= capture_stderr_limit <= 8192:
        raise Refused("bounded guest SSH diagnostic output limit is invalid")
    if tty and (capture_limit is not None or capture_stderr_limit):
        raise Refused("interactive guest SSH cannot capture output")
    if capture_limit is None and capture_stderr_limit == 0:
        result = subprocess.run(argv, stdin=None if tty else subprocess.DEVNULL,
                                timeout=timeout, check=False)
        return result.returncode

    process = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture_limit is not None else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture_stderr_limit else subprocess.DEVNULL,
        close_fds=True)
    streams = {}
    outputs: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    selector = selectors.DefaultSelector()
    if process.stdout is not None:
        streams[process.stdout.fileno()] = ("stdout", capture_limit)
    if process.stderr is not None:
        streams[process.stderr.fileno()] = ("stderr", capture_stderr_limit)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            selector.register(stream, selectors.EVENT_READ)
            os.set_blocking(stream.fileno(), False)
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            events = selector.select(min(remaining, 0.1))
            if not events and process.poll() is not None:
                # Drain to EOF; a child can exit with buffered pipe data.
                continue
            for key, _ in events:
                label, limit = streams[key.fd]
                try:
                    chunk = os.read(key.fd, 4096)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target = outputs[label]
                remaining_capacity = (None if limit is None else max(0, limit - len(target)))
                if remaining_capacity is not None:
                    target.extend(chunk[:remaining_capacity])
                if (label == "stdout" and remaining_capacity is not None
                        and len(chunk) > remaining_capacity):
                    process.kill()
                    process.wait()
                    raise Refused("fixed guest compatibility response exceeded its bound")
        if timed_out:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise GuestSSHTimeout("bounded guest SSH request timed out",
                                  stderr=bytes(outputs["stderr"]))
        return_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as error:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise GuestSSHTimeout("bounded guest SSH request timed out",
                              stderr=bytes(outputs["stderr"])) from error
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    if capture_limit is not None and capture_stderr_limit:
        return return_code, bytes(outputs["stdout"]), bytes(outputs["stderr"])
    if capture_limit is not None:
        return return_code, bytes(outputs["stdout"])
    return return_code, bytes(outputs["stderr"])


def _guest_ssh(command: str, *, tty: bool = False, timeout: float = 20,
               capture_limit: int | None = None, capture_stderr_limit: int = 0,
               ssh_port: int = SSH_PORT, identity_file: pathlib.Path = CONTROL_KEY,
               known_hosts: pathlib.Path = KNOWN_HOSTS,
               expected_identity_uid: int | None = None,
               runtime_directory: pathlib.Path | None = None,
               expected_runtime_uid: int | None = None):
    identity_mode = 0o640 if expected_identity_uid is None else 0o600
    identity_group = CONTROL_GROUP if expected_identity_uid is None else None
    known_hosts_mode = 0o644 if expected_identity_uid is None else 0o600
    identity = _read_guest_ssh_asset(identity_file, expected_uid=expected_identity_uid,
                                     mode=identity_mode, limit=16 * 1024,
                                     group=identity_group)
    known = _read_guest_ssh_asset(known_hosts, expected_uid=expected_identity_uid,
                                  mode=known_hosts_mode, limit=4096)
    current_uid = os.getuid()
    service_uid = pwd.getpwnam(SERVICE_USER).pw_uid
    runtime = (runtime_directory if runtime_directory is not None else
               QMP_RUNTIME if current_uid == service_uid else pathlib.Path(f"/run/user/{current_uid}"))
    runtime_owner = (expected_runtime_uid if expected_runtime_uid is not None else current_uid)
    try:
        runtime_info = os.lstat(runtime)
    except OSError as error:
        raise Refused("private SSH identity runtime is unavailable") from error
    if (not stat.S_ISDIR(runtime_info.st_mode) or stat.S_ISLNK(runtime_info.st_mode)
            or runtime_info.st_uid != runtime_owner or stat.S_IMODE(runtime_info.st_mode) != 0o700):
        raise Refused("private SSH identity runtime has unsafe owner or mode")
    key_dir = runtime / f"lm-system-ssh-{secrets.token_hex(12)}"
    key_dir.mkdir(mode=0o700)
    identity_path = key_dir / "identity"
    known_path = key_dir / "known_hosts"
    try:
        _write_guest_ssh_stage(identity_path, identity)
        _write_guest_ssh_stage(known_path, known)
        ssh = guest_ssh_argv(ssh_port=ssh_port, identity_file=identity_path,
                             known_hosts=known_path, tty=tty) + [command]
        return _run_guest_ssh(ssh, tty=tty, timeout=timeout, capture_limit=capture_limit,
                              capture_stderr_limit=capture_stderr_limit)
    finally:
        for path in (identity_path, known_path):
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                continue
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != current_uid
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise Refused("temporary SSH credential changed; preserving the evidence")
            os.unlink(path)
        os.rmdir(key_dir)


def _browser_auth() -> int:
    """Use the maintained bounded browser bridge against this fixed guest."""
    uid = os.getuid()
    runtime = pathlib.Path(f"/run/user/{uid}")
    try:
        runtime_info = os.lstat(runtime)
    except OSError as error:
        raise Refused("private per-user runtime directory is unavailable for browser auth") from error
    if (not stat.S_ISDIR(runtime_info.st_mode) or stat.S_ISLNK(runtime_info.st_mode)
            or runtime_info.st_uid != uid or stat.S_IMODE(runtime_info.st_mode) != 0o700):
        raise Refused("private per-user runtime directory is unsafe for browser auth")
    _require_fixed_file(CONTROL_KEY, uid=0, mode=0o640, group=CONTROL_GROUP)
    _require_fixed_file(KNOWN_HOSTS, uid=0, mode=0o644)
    run_name = f"a-{secrets.token_hex(12)}"
    run_dir = runtime / run_name
    control_dir = runtime / f"lmv3-{run_name}"
    socket_path = control_dir / "auth-control.sock"
    if len(os.fsencode(socket_path)) >= 100:
        raise Refused("private SSH auth control socket path exceeds the fixed OpenSSH bound")
    run_dir.mkdir(mode=0o700)
    control_dir.mkdir(mode=0o700)
    paths = {name: run_dir / name for name in (
        "identity_file", "known_hosts", "ssh_identity", "callback_port_file", "proof_file")}
    interrupted: list[int] = []
    old_handlers: dict[int, object] = {}

    def defer(signum, _frame):
        if signum not in interrupted:
            interrupted.append(signum)

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        old_handlers[signum] = signal.signal(signum, defer)
    result = 1
    try:
        for path, payload in ((paths["identity_file"], CONTROL_KEY.read_bytes()),
                              (paths["known_hosts"], KNOWN_HOSTS.read_bytes())):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise Refused("browser-auth credential staging made no progress")
                    remaining = remaining[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
        helper_path = pathlib.Path(__file__).with_name("browser-auth-session.py")
        spec = __import__("importlib.util", fromlist=["spec_from_file_location"]).spec_from_file_location(
            "system_browser_auth_session", helper_path)
        if spec is None or spec.loader is None:
            raise Refused("protected browser auth session helper is unavailable")
        bridge = __import__("importlib.util", fromlist=["module_from_spec"]).module_from_spec(spec)
        spec.loader.exec_module(bridge)
        if not interrupted:
            result = bridge.main([
                "--ssh", "/usr/bin/ssh", "--ssh-port", str(SSH_PORT),
                "--identity-file", str(paths["identity_file"]),
                "--known-hosts", str(paths["known_hosts"]),
                "--control-socket", str(socket_path),
                "--ssh-identity", str(paths["ssh_identity"]),
                "--callback-port-file", str(paths["callback_port_file"]),
                "--proof-file", str(paths["proof_file"]), "--run-dir", str(run_dir),
                "--guest-auth-dir", "/tmp/mester-v3-auth-system-vm",
                "--timeout-seconds", "600", "--output-limit", "65536", "--persistent-parent",
            ])
    finally:
        for path in paths.values():
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                continue
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise Refused("browser-auth temporary artifact is unsafe; preserving it")
            os.unlink(path)
        for directory in (control_dir, run_dir):
            try:
                os.rmdir(directory)
            except FileNotFoundError:
                pass
            except OSError as error:
                raise Refused("browser-auth cleanup left unexpected private evidence") from error
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
    if interrupted:
        return 128 + interrupted[0]
    return result


def _reject_duplicate_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate status key")
        result[key] = value
    return result


def _cloud_init_status_state(return_code: int, stdout: bytes) -> str:
    """Classify the bounded machine-readable status without exposing its contents."""
    if type(return_code) is not int or return_code not in (0, 1, 2):
        return "INCONCLUSIVE"
    if return_code in (1, 2):
        return "ERROR"
    if not isinstance(stdout, bytes) or not stdout or len(stdout) > GUEST_COMPAT_MAX_OUTPUT:
        return "INCONCLUSIVE"
    try:
        document = json.loads(stdout.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_pairs)
    except (UnicodeError, json.JSONDecodeError, ValueError, TypeError, RecursionError):
        return "INCONCLUSIVE"
    if not isinstance(document, dict):
        return "INCONCLUSIVE"
    status, extended = document.get("status"), document.get("extended_status")
    errors, recoverable_errors = document.get("errors"), document.get("recoverable_errors")
    if (not isinstance(status, str) or not isinstance(extended, str)
            or type(errors) is not list or type(recoverable_errors) is not dict):
        return "INCONCLUSIVE"
    if errors or recoverable_errors:
        return "ERROR"
    if status in ("error", "disabled") or extended.startswith(("error", "degraded")):
        return "ERROR"
    if status in ("not started", "running"):
        return "RUNNING" if extended == status else "INCONCLUSIVE"
    if status == "done":
        return "DONE" if extended == "done" else "INCONCLUSIVE"
    return "INCONCLUSIVE"


def _ssh_failure_label(stderr: bytes, returncode: int, *, timed_out: bool = False) -> str:
    """Reduce bounded, untrusted OpenSSH output to a fixed diagnostic vocabulary."""
    text = stderr.decode("utf-8", "ignore").lower()
    if "host key verification failed" in text or "remote host identification has changed" in text:
        return "SSH_HOST_KEY_REJECTED"
    if "permission denied" in text and "publickey" in text:
        return "SSH_AUTH_REJECTED"
    if timed_out:
        return "SSH_CLIENT_ATTEMPT_TIMED_OUT"
    if "connection refused" in text:
        return "GUEST_SSH_SERVICE_UNAVAILABLE"
    if "connection timed out" in text or "operation timed out" in text:
        return "GUEST_SSH_SERVICE_UNRESPONSIVE"
    if returncode == 255:
        return "SSH_TRANSPORT_OR_HANDSHAKE_FAILED"
    return "GUEST_READINESS_COMMAND_FAILED"


def _open_qemu_readiness_pidfd(pid: int | None, expected_uid: int | None) -> int | None:
    if pid is None:
        return None
    if (type(pid) is not int or pid <= 1
            or (expected_uid is not None
                and (type(expected_uid) is not int or expected_uid <= 0))):
        raise Refused("QEMU readiness process identity is invalid")
    if not hasattr(os, "pidfd_open"):
        raise Refused("pidfd support is required to monitor QEMU readiness")
    try:
        pidfd = os.pidfd_open(pid, 0)
    except OSError as error:
        raise Refused("QEMU_PROCESS_EXITED_BEFORE_READINESS") from error
    try:
        if expected_uid is None:
            _check_qemu_readiness_pidfd(pidfd)
            return pidfd
        executable = os.readlink(f"/proc/{pid}/exe")
        status = pathlib.Path(f"/proc/{pid}/status").read_text(encoding="ascii")
    except OSError as error:
        os.close(pidfd)
        raise Refused("QEMU_PROCESS_EXITED_BEFORE_READINESS") from error
    except BaseException:
        os.close(pidfd)
        raise
    try:
        uid_line = next((line for line in status.splitlines() if line.startswith("Uid:")), "")
        values = uid_line.split()
        if (len(values) != 5 or values[1] != str(expected_uid)
                or os.path.realpath(executable) != "/usr/bin/qemu-system-x86_64"):
            raise Refused("QEMU_IDENTITY_MISMATCH_BEFORE_READINESS")
        _check_qemu_readiness_pidfd(pidfd)
        return pidfd
    except BaseException:
        os.close(pidfd)
        raise


def _check_qemu_readiness_pidfd(pidfd: int | None) -> None:
    if pidfd is None:
        return
    poller = select.poll()
    poller.register(pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
    if poller.poll(0):
        raise Refused("QEMU_PROCESS_EXITED_BEFORE_READINESS")


def _readiness_progress(started: float, phase: str, attempts: int, last: str,
                        last_report: list[float]) -> None:
    now = time.monotonic()
    if now - last_report[0] < 30.0:
        return
    elapsed = max(0, int(now - started))
    print(f"guest_readiness_progress elapsed_seconds={elapsed} phase={phase} "
          f"attempts={attempts} last={last}", flush=True)
    last_report[0] = now


def _guest_ssh_with_options(command: str, guest_ssh_options: dict | None = None, **kwargs):
    options = {} if guest_ssh_options is None else dict(guest_ssh_options)
    options.update(kwargs)
    return _guest_ssh(command, **options)


def _wait_for_guest_provisioning(*, deadline: float | None = None,
                                 guest_ssh_options: dict | None = None,
                                 qemu_check=None, progress=None) -> int:
    """Wait for clean cloud-init completion under the caller's one startup deadline."""
    started = time.monotonic()
    deadline = started + SSH_READY_TIMEOUT if deadline is None else deadline
    attempts = 0
    last_failure = "SSH_FORWARD_NOT_LISTENING"
    while True:
        if qemu_check is not None:
            qemu_check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            result = _guest_ssh_with_options(
                CLOUD_INIT_STATUS_COMMAND, guest_ssh_options,
                timeout=min(15.0, remaining), capture_limit=GUEST_COMPAT_MAX_OUTPUT,
                capture_stderr_limit=8192)
            attempts += 1
        except GuestSSHTimeout as error:
            attempts += 1
            last_failure = _ssh_failure_label(error.stderr, -signal.SIGKILL, timed_out=True)
            if progress is not None:
                progress("cloud-init", attempts, last_failure)
            time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
            continue
        except (OSError, subprocess.SubprocessError) as error:
            attempts += 1
            last_failure = "SSH_TRANSPORT_OR_HANDSHAKE_FAILED"
            if progress is not None:
                progress("cloud-init", attempts, last_failure)
            result = None
        if result is not None:
            if not isinstance(result, tuple) or len(result) not in (2, 3):
                raise Refused("cloud-init status response is inconclusive")
            return_code, stdout = result[:2]
            stderr = result[2] if len(result) == 3 else b""
            if type(return_code) is int and return_code == 255:
                last_failure = _ssh_failure_label(stderr, return_code)
                if last_failure in ("SSH_HOST_KEY_REJECTED", "SSH_AUTH_REJECTED"):
                    raise Refused(last_failure)
                state = "TRANSPORT"
            else:
                state = _cloud_init_status_state(return_code, stdout)
            if state == "ERROR":
                raise Refused("cloud-init reported unsuccessful or degraded completion")
            if state == "INCONCLUSIVE":
                raise Refused("cloud-init status is inconclusive; provisioning is not established")
            if state == "RUNNING":
                last_failure = "CLOUD_INIT_RUNNING"
            if state == "DONE":
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    marker_result = _guest_ssh_with_options(
                        GUEST_PROVISIONED_COMMAND, guest_ssh_options,
                        timeout=min(15.0, remaining), capture_stderr_limit=8192)
                    if type(marker_result) is int:
                        marker_status, marker_stderr = marker_result, b""
                    else:
                        marker_status, marker_stderr = marker_result
                except GuestSSHTimeout as error:
                    marker_status, marker_stderr = 255, error.stderr
                except (OSError, subprocess.SubprocessError):
                    marker_status, marker_stderr = 255, b""
                if marker_status == 0:
                    elapsed = time.monotonic() - started
                    if time.monotonic() < deadline:
                        return max(0, int(elapsed * 1000))
                    break
                if marker_status != 255:
                    raise Refused("guest provisioning completion marker is missing or unsafe")
                last_failure = _ssh_failure_label(marker_stderr, marker_status)
                if last_failure in ("SSH_HOST_KEY_REJECTED", "SSH_AUTH_REJECTED"):
                    raise Refused(last_failure)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if progress is not None:
            progress("cloud-init", attempts, last_failure)
        time.sleep(min(2.0, remaining))
    raise Refused(f"GUEST_READINESS_DEADLINE_EXPIRED phase=cloud-init last={last_failure} "
                  f"attempts={attempts}")


def _ensure_guest_ready(*, qemu_pid: int | None = None,
                        qemu_pidfd: int | None = None,
                        expected_qemu_uid: int | None = None,
                        guest_ssh_options: dict | None = None,
                        workspace_path: str | None = None,
                        verify_workspace_mount: bool = False,
                        timeout_seconds: float | None = None) -> None:
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "system_parent_session_contract", pathlib.Path(__file__).with_name("parent-session-contract.py"))
    if spec is None or spec.loader is None:
        raise Refused("protected parent session contract is unavailable")
    contract = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(contract)
    workspace_path = contract.SYSTEM_WORKSPACE if workspace_path is None else workspace_path
    timeout_seconds = SSH_READY_TIMEOUT if timeout_seconds is None else timeout_seconds
    if (type(timeout_seconds) not in (int, float)
            or not 0 < timeout_seconds <= SSH_READY_TIMEOUT):
        raise Refused("guest readiness deadline is outside its existing fixed bound")
    if qemu_pidfd is not None:
        if qemu_pid is not None or type(qemu_pidfd) is not int or qemu_pidfd < 0:
            raise Refused("guest readiness pidfd input is invalid")
        try:
            if os.readlink(f"/proc/self/fd/{qemu_pidfd}") != "anon_inode:[pidfd]":
                raise Refused("guest readiness descriptor is not a pidfd")
        except OSError as error:
            raise Refused("guest readiness pidfd is unavailable") from error
    started = time.monotonic()
    deadline = started + timeout_seconds
    last_report = [started]
    attempts = 0
    last_failure = "SSH_FORWARD_NOT_LISTENING"
    external_pidfd = qemu_pidfd is not None
    pidfd = (qemu_pidfd if external_pidfd
             else _open_qemu_readiness_pidfd(qemu_pid, expected_qemu_uid))

    def check_qemu() -> None:
        _check_qemu_readiness_pidfd(pidfd)

    def progress(phase: str, count: int, last: str) -> None:
        _readiness_progress(started, phase, count, last, last_report)

    try:
        with StageTimer().timed_stage("guest_readiness_ssh"):
            cloud_init_ready_wait_ms = _wait_for_guest_provisioning(
                deadline=deadline, guest_ssh_options=guest_ssh_options,
                qemu_check=check_qemu, progress=progress)
            check_qemu()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Refused("GUEST_READINESS_DEADLINE_EXPIRED phase=compatibility "
                              "last=SSH_CLIENT_ATTEMPT_TIMED_OUT attempts=0")
            _check_guest_compatibility(cloud_init_ready_wait_ms=cloud_init_ready_wait_ms,
                                       workspace_path=workspace_path,
                                       guest_ssh_options=guest_ssh_options,
                                       timeout=min(20.0, remaining))
            command = contract.readiness_checks(workspace_path=workspace_path)
            while time.monotonic() < deadline:
                check_qemu()
                remaining = deadline - time.monotonic()
                try:
                    result = _guest_ssh_with_options(
                        command, guest_ssh_options, timeout=min(15.0, remaining),
                        capture_stderr_limit=8192)
                    if type(result) is int:
                        return_code, stderr = result, b""
                    else:
                        return_code, stderr = result
                except GuestSSHTimeout as error:
                    attempts += 1
                    last_failure = _ssh_failure_label(error.stderr, -signal.SIGKILL, timed_out=True)
                    progress("session-contract", attempts, last_failure)
                    time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
                    continue
                except (OSError, subprocess.SubprocessError):
                    attempts += 1
                    last_failure = "SSH_TRANSPORT_OR_HANDSHAKE_FAILED"
                    progress("session-contract", attempts, last_failure)
                    time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
                    continue
                attempts += 1
                if return_code == 0:
                    check_qemu()
                    if verify_workspace_mount and workspace_path == contract.SYSTEM_WORKSPACE:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        _guest_workspace_mount(
                            guest_ssh_options=guest_ssh_options,
                            timeout=min(20.0, remaining))
                    return
                last_failure = _ssh_failure_label(stderr, return_code)
                if last_failure in ("SSH_HOST_KEY_REJECTED", "SSH_AUTH_REJECTED"):
                    raise Refused(last_failure)
                progress("session-contract", attempts, last_failure)
                time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
            raise Refused(f"GUEST_READINESS_DEADLINE_EXPIRED phase=session-contract "
                          f"last={last_failure} attempts={attempts}")
    finally:
        if pidfd is not None and not external_pidfd:
            os.close(pidfd)


def _guest_compatibility_module():
    path = pathlib.Path(__file__).with_name("guest-compatibility.py")
    spec = importlib.util.spec_from_file_location("system_guest_compatibility", path)
    if spec is None or spec.loader is None:
        raise Refused("fixed guest compatibility checker is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _check_guest_compatibility(*, cloud_init_ready_wait_ms: int | None = None,
                               workspace_path: str | None = None,
                               guest_ssh_options: dict | None = None,
                               timeout: float = 20) -> None:
    checker = _guest_compatibility_module()
    workspace_path = checker.SYSTEM_WORKSPACE if workspace_path is None else workspace_path
    command = checker.build_inspection_command(workspace_path)
    try:
        result = _guest_ssh_with_options(
            command, guest_ssh_options, timeout=timeout,
            capture_limit=GUEST_COMPAT_MAX_OUTPUT)
    except (OSError, subprocess.SubprocessError, Refused):
        print("Guest compatibility: INCONCLUSIVE (fixed inspection failed)", file=sys.stderr)
        raise Refused("guest compatibility is inconclusive; normal VM use is blocked")
    if not isinstance(result, tuple) or len(result) != 2 or result[0] != 0:
        print("Guest compatibility: INCONCLUSIVE (fixed inspection unavailable)", file=sys.stderr)
        raise Refused("guest compatibility is inconclusive; normal VM use is blocked")
    expected = (checker.expected_inventory() if workspace_path == checker.SYSTEM_WORKSPACE
                else checker.expected_inventory(workspace_path))
    status, diagnostics = checker.classify_inventory(result[1], expected)
    print(f"Guest compatibility: {status}" +
          (" (" + ", ".join(diagnostics[:16]) + ")" if diagnostics else ""),
          file=sys.stderr if status != "MATCH" else sys.stdout)
    if status != "MATCH":
        try:
            evidence = checker.format_failure_evidence(
                result[1], expected, cloud_init_ready_wait_ms=cloud_init_ready_wait_ms,
                workspace_path=workspace_path)
            if (not isinstance(evidence, str)
                    or not evidence.startswith("Guest compatibility evidence:")
                    or len(evidence.encode("utf-8")) > 1024):
                raise ValueError("compatibility evidence is outside its fixed output contract")
        except Exception:
            evidence = "Guest compatibility evidence: unavailable (diagnostic-error)"
        print(evidence, file=sys.stderr)
        raise Refused("guest compatibility is not established; normal VM use is blocked")


def _workspace_policy():
    path = pathlib.Path(__file__).with_name("system-vm-workspace.py")
    spec = importlib.util.spec_from_file_location("system_vm_workspace_owner", path)
    if spec is None or spec.loader is None:
        raise Refused("fixed workspace assignment helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _guest_workspace_mount(*, guest_ssh_options: dict | None = None,
                           timeout: float = 20) -> None:
    command = (
        "set -eu; test ! -L /workspace; mountpoint -q /workspace; "
        "test \"$(findmnt -n -o FSTYPE --target /workspace)\" = virtiofs; "
        "test \"$(findmnt -n -o SOURCE --target /workspace)\" = workspace; "
        "test -w /workspace")
    if _guest_ssh_with_options(command, guest_ssh_options, timeout=timeout) != 0:
        raise Refused("guest did not mount the approved host directory at /workspace")


def _read_probe(path: pathlib.Path, uid: int) -> tuple[os.stat_result, bytes]:
    info = os.lstat(path)
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise Refused("workspace read/write probe has unsafe file metadata")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise Refused("workspace probe changed while opening")
        value = os.read(fd, 257)
    finally:
        os.close(fd)
    return info, value


def _remove_probe(path: pathlib.Path, identity: tuple[int, int], uid: int, expected: bytes) -> None:
    try:
        info, value = _read_probe(path, uid)
    except FileNotFoundError:
        return
    if ((info.st_dev, info.st_ino) != identity or value != expected):
        raise Refused("workspace probe changed; preserving unexpected evidence")
    os.unlink(path)


def _probe_workspace() -> None:
    policy = _workspace_policy()
    workspace = policy.read_assignment(policy.ASSIGNMENT, uid=os.getuid())
    policy.verify_identity(workspace)
    nonce = secrets.token_hex(16)
    host_path = workspace.path / f".lm-vm-probe-{nonce}"
    guest_path = workspace.path / f".lm-vm-probe-{nonce}.guest"
    payload = (nonce + "\n").encode("ascii")
    fd = os.open(host_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
        host_info = os.fstat(fd)
    finally:
        os.close(fd)
    guest_info = None
    try:
        command = (f"test \"$(cat /workspace/.lm-vm-probe-{nonce})\" = {nonce} && "
                   f"umask 077 && printf '%s\\n' {nonce} > /workspace/.lm-vm-probe-{nonce}.guest")
        if _guest_ssh(command, timeout=20) != 0:
            raise Refused("workspace host-to-guest read or guest-to-host write probe failed")
        if _read_probe(host_path, workspace.uid)[1] != payload:
            raise Refused("guest read of host workspace probe did not match its nonce")
        guest_info, guest_payload = _read_probe(guest_path, workspace.uid)
        if guest_payload != payload:
            raise Refused("guest write to host workspace did not preserve its nonce")
    finally:
        _remove_probe(host_path, (host_info.st_dev, host_info.st_ino), workspace.uid, payload)
        if guest_info is not None:
            _remove_probe(guest_path, (guest_info.st_dev, guest_info.st_ino), workspace.uid, payload)


def _qemu_guest_ready(qemu_pid: int | None = None) -> None:
    _proxy_ready()
    vm_uid = pwd.getpwnam(SERVICE_USER).pw_uid
    _ensure_guest_ready(qemu_pid=qemu_pid, expected_qemu_uid=vm_uid,
                        workspace_path="/workspace", verify_workspace_mount=True)


def _system_unit_active() -> bool:
    result = subprocess.run(["/usr/bin/systemctl", "show", "--property=ActiveState",
                             "--value", "little-mester-vm.service"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, check=False)
    return result.returncode == 0 and result.stdout.strip() == "active"


def _fixed_exec_locked(action: str) -> int:
    commands = {
        "up": ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "start", "little-mester-vm.service"],
        "down": ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", "little-mester-vm.service"],
        "status": ["/usr/bin/systemctl", "show", "--property=ActiveState", "--property=MainPID",
                   "--value", "little-mester-vm.service"],
        "ssh": None,
        "auth": None,
        "tui": None,
    }
    if action not in commands:
        raise Refused("unsupported fixed VM operation")
    if action == "ssh":
        if not _system_unit_active():
            raise Refused("fixed VM service is not active after compatibility-gated startup")
        return _guest_ssh("/bin/bash -l", tty=True, timeout=24 * 3600)
    if action == "auth":
        if not _system_unit_active():
            raise Refused("fixed VM service is not active after compatibility-gated startup")
        return _browser_auth()
    if action == "tui":
        if not _system_unit_active():
            raise Refused("fixed VM service is not active after compatibility-gated startup")
        return _guest_ssh("/usr/local/bin/lm-opencode", tty=True, timeout=24 * 3600)
    if action == "up":
        policy = _workspace_policy()
        try:
            policy.verify_host_assignment(os.getuid())
        except policy.Refused as error:
            raise Refused(f"host workspace preflight failed before VM lifecycle: {error}; "
                          "configure it with ./lab vm up --dir PATH") from error
    if action == "status":
        result = subprocess.run(commands[action], stdin=subprocess.DEVNULL, check=False)
        if result.returncode == 0:
            policy = _workspace_policy()
            try:
                workspace = policy.read_assignment(policy.ASSIGNMENT, uid=os.getuid())
                print(f"Workspace: {workspace.path} -> /workspace")
            except policy.Refused:
                print("Workspace: not assigned")
        return result.returncode
    result = subprocess.run(commands[action], stdin=subprocess.DEVNULL, check=False)
    if action == "up" and result.returncode == 0:
        # ExecStartPost verifies the guest mount inside QEMU's sandbox. The
        # host-side probe runs as the owner, not as the isolated QEMU identity.
        _probe_workspace()
    return result.returncode


def _fixed_exec(action: str) -> int:
    """Serialize supported owner routes with protected code update/rollback."""
    if action == "status":
        # Host-only status remains available while maintenance blocks use.
        return _fixed_exec_locked(action)
    _require_fixed_file(CODE_UPDATE_LOCK, uid=0, mode=0o660, group=CONTROL_GROUP)
    try:
        marker_info = os.lstat(CODE_UPDATE_MARKER)
    except FileNotFoundError:
        marker_info = None
    if marker_info is not None:
        raise Refused("protected code update/rollback is incomplete; fixed VM routes are blocked")
    fd = os.open(CODE_UPDATE_LOCK, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o660
                or info.st_gid != grp.getgrnam(CONTROL_GROUP).gr_gid):
            raise Refused("fixed code-update lock identity is unsafe")
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Refused("fixed VM operation is blocked by an active code update/rollback") from error
        # Recheck after lock acquisition so a marker cannot be raced by a route.
        if CODE_UPDATE_MARKER.exists() or CODE_UPDATE_MARKER.is_symlink():
            raise Refused("protected code update/rollback is incomplete; fixed VM routes are blocked")
        return _fixed_exec_locked(action)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _provision() -> None:
    """Create a new, distinct root-provisioned disk and cloud-init seed once."""
    if os.geteuid() != 0:
        raise Refused("fixed system VM provisioning is root-only")
    if DISK.exists() or DISK.is_symlink() or SEED.exists() or SEED.is_symlink():
        raise Refused("preserving pre-existing fixed system VM disk or seed")
    try:
        service = pwd.getpwnam(SERVICE_USER)
        control_gid = grp.getgrnam(CONTROL_GROUP).gr_gid
        service_gid = grp.getgrnam(SERVICE_USER).gr_gid
    except KeyError as error:
        raise Refused("fixed service/control identity is not installed") from error
    config_info = os.lstat(CONFIG)
    state_info = os.lstat(STATE)
    if (not stat.S_ISDIR(config_info.st_mode) or stat.S_ISLNK(config_info.st_mode)
            or config_info.st_uid != 0 or config_info.st_gid != control_gid
            or stat.S_IMODE(config_info.st_mode) != 0o750
            or not stat.S_ISDIR(state_info.st_mode) or stat.S_ISLNK(state_info.st_mode)
            or state_info.st_uid != 0 or state_info.st_gid != service_gid
            or stat.S_IMODE(state_info.st_mode) != 0o750):
        raise Refused("fixed system VM config/state ancestry is not root-controlled")
    artifacts = (STATE / IMAGE_NAME, STATE / "base-image.qcow2", STATE / DISK_PART,
                 STATE / "seed.iso.part", STATE / "user-data", STATE / "meta-data",
                 CONFIG / "control_ed25519", CONFIG / "control_ed25519.pub",
                 HOST_KEY, pathlib.Path(str(HOST_KEY) + ".pub"), KNOWN_HOSTS)
    if any(path.exists() or path.is_symlink() for path in artifacts):
        raise Refused("preserving pre-existing system VM provisioning artifact")
    if not shutil.which("/usr/bin/curl") or not shutil.which("/usr/bin/qemu-img"):
        raise Refused("fixed curl and qemu-img host tools are required")
    # Provisioning is one-shot and creates only this fixed fresh disk/seed.
    _create_seed_and_disk(service_uid=service.pw_uid, control_gid=control_gid,
                          service_gid=service_gid)


def _create_seed_and_disk(*, service_uid: int, control_gid: int, service_gid: int) -> None:
    """Pinned image and fixed protected cloud-init/session seed creation."""
    # This is intentionally explicit and one-shot; partial evidence is retained.
    base = STATE / IMAGE_NAME
    digest = hashlib.sha256()
    with open(base, "xb") as output:
        os.chmod(base, 0o600)
        result = subprocess.run(["/usr/bin/curl", "-q", "--fail", "--location", "--silent",
                                 "--show-error", "--max-time", "300", "--retry", "2", IMAGE_URL],
                                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                                timeout=310, check=False)
        output.flush()
        os.fsync(output.fileno())
    if result.returncode:
        raise Refused("pinned base image download failed; partial evidence retained")
    with open(base, "rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    if digest.hexdigest() != IMAGE_SHA256:
        raise Refused("pinned base image checksum mismatch; evidence retained")
    # Build one fresh, flattened qcow2 (no backing file or old-disk path).
    os.link(base, STATE / "base-image.qcow2", follow_symlinks=False)
    os.unlink(base)
    os.chown(STATE / "base-image.qcow2", 0, 0, follow_symlinks=False)
    result = subprocess.run(["/usr/bin/qemu-img", "convert", "-f", "qcow2", "-O", "qcow2",
                            str(STATE / "base-image.qcow2"), str(STATE / DISK_PART)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=900, check=False)
    if result.returncode:
        raise Refused("pinned base image conversion failed; evidence retained")
    result = subprocess.run(["/usr/bin/qemu-img", "resize", "-f", "qcow2",
                            str(STATE / DISK_PART), "64G"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=60, check=False)
    if result.returncode:
        raise Refused("initial fixed disk sizing failed; evidence retained")
    info_result = subprocess.run(["/usr/bin/qemu-img", "info", "--output=json", "-f", "qcow2",
                                  str(STATE / DISK_PART)], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 timeout=30, check=False)
    if info_result.returncode:
        raise Refused("qemu-img could not verify the fresh system disk; evidence retained")
    try:
        info = json.loads(info_result.stdout)
        provision_path = pathlib.Path(__file__).with_name("parent-provision.py")
        spec = importlib.util.spec_from_file_location("system_parent_provision", provision_path)
        if spec is None or spec.loader is None:
            raise ValueError("shared qcow2 validator is unavailable")
        provision = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(provision)
        provision.validate_qcow2_info(info, 64 * 1024**3)
    except (UnicodeError, json.JSONDecodeError, ValueError, RuntimeError, OSError) as error:
        raise Refused("fresh system disk failed explicit qcow2/no-backing validation") from error
    os.chmod(STATE / DISK_PART, 0o600)
    os.chown(STATE / DISK_PART, service_uid, service_gid, follow_symlinks=False)
    # Seed generation uses the canonical parent cloud-init/session contract.
    _write_protected_seed(control_gid, service_uid, service_gid)


def _write_protected_seed(control_gid: int, service_uid: int, service_gid: int) -> None:
    """Write common disk-workspace bootstrap plus protected session assets."""
    contract_path = pathlib.Path(__file__).with_name("parent-session-contract.py")
    spec = importlib.util.spec_from_file_location("system_parent_session_contract", contract_path)
    if spec is None or spec.loader is None:
        raise Refused("protected parent session contract is unavailable")
    contract = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(contract)
    bootstrap_path = pathlib.Path(__file__).with_name("guest-bootstrap.py")
    bootstrap_spec = importlib.util.spec_from_file_location("system_guest_bootstrap", bootstrap_path)
    if bootstrap_spec is None or bootstrap_spec.loader is None:
        raise Refused("shared guest bootstrap generator is unavailable")
    bootstrap = importlib.util.module_from_spec(bootstrap_spec)
    bootstrap_spec.loader.exec_module(bootstrap)
    for name in ("control_ed25519", "ssh_host_ed25519_key"):
        if (CONFIG / name).exists() or (CONFIG / name).is_symlink():
            raise Refused("preserving pre-existing system VM SSH identity evidence")
    ssh_keygen = "/usr/bin/ssh-keygen"
    for stem in ("control_ed25519", "ssh_host_ed25519_key"):
        result = subprocess.run([ssh_keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(CONFIG / stem)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=20, check=False)
        if result.returncode:
            raise Refused("fixed system VM SSH identity generation failed; evidence retained")
    control_public = (CONFIG / "control_ed25519.pub").read_text(encoding="ascii").strip()
    host_private = (HOST_KEY).read_text(encoding="ascii")
    host_public = pathlib.Path(str(HOST_KEY) + ".pub").read_text(encoding="ascii").strip()
    user_data = bootstrap.build_foundation(
        name="little-mester-system-vm", uid=1000, ssh_key=control_public,
        workspace="virtiofs", helper="/usr/local/libexec/lm-guest-user-dirs.py")
    contract.add_protected_session(user_data, host_private=host_private,
                                   host_public=host_public,
                                   control_public=control_public,
                                   workspace_path=contract.SYSTEM_WORKSPACE)
    user_data_path = STATE / "user-data"
    metadata_path = STATE / "meta-data"
    iso_part = STATE / "seed.iso.part"
    for path in (user_data_path, metadata_path, iso_part, SEED):
        if path.exists() or path.is_symlink():
            raise Refused("preserving pre-existing system VM cloud-init seed evidence")
    user_data_path.write_bytes(b"#cloud-config\n" + json.dumps(user_data, separators=(",", ":")).encode() + b"\n")
    metadata_path.write_text("instance-id: little-mester-system-vm\nlocal-hostname: little-mester-system-vm\n",
                             encoding="ascii")
    os.chmod(user_data_path, 0o600)
    os.chmod(metadata_path, 0o600)
    iso = "/usr/bin/genisoimage"
    result = subprocess.run([iso, "-quiet", "-rock", "-joliet", "-output", str(iso_part),
                             "-V", "cidata", str(user_data_path), str(metadata_path)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=60, check=False)
    if result.returncode:
        raise Refused("fixed protected cloud-init ISO creation failed; evidence retained")
    os.chmod(iso_part, 0o640)
    os.chown(iso_part, 0, service_gid, follow_symlinks=False)
    os.link(iso_part, SEED, follow_symlinks=False)
    os.chmod(CONFIG / "control_ed25519", 0o640)
    os.chown(CONFIG / "control_ed25519", 0, control_gid, follow_symlinks=False)
    os.chmod(CONFIG / "control_ed25519.pub", 0o644)
    os.chmod(HOST_KEY, 0o600)
    os.chmod(pathlib.Path(str(HOST_KEY) + ".pub"), 0o644)
    # The strict host key is pinned from the root-generated SSH host key, not TOFU.
    known = f"[127.0.0.1]:{SSH_PORT} {host_public}\n"
    KNOWN_HOSTS.write_text(known, encoding="ascii")
    os.chmod(KNOWN_HOSTS, 0o644)
    os.chown(STATE / DISK_PART, service_uid, service_gid, follow_symlinks=False)
    os.link(STATE / DISK_PART, DISK, follow_symlinks=False)
    os.chmod(DISK, 0o600)
    # Keep the imported seed and disk as the complete, fixed VM state only.
    for path in (STATE / "base-image.qcow2", STATE / "disk.qcow2.part", STATE / "seed.iso.part",
                 user_data_path, metadata_path):
        path.unlink()
    directory_fd = os.open(STATE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    qmp = sub.add_parser("qmp-stop")
    qmp.add_argument("pid", type=int)
    sub.add_parser("proxy-ready")
    sub.add_parser("check")
    sub.add_parser("guest-ready")
    sub.add_parser("provision")
    locked = sub.add_parser("locked-service")
    locked.add_argument("role", choices=("qemu", "proxy", "workspace", "metrics"))
    locked.add_argument("--owner")
    owner = sub.add_parser("owner")
    owner.add_argument("operation", choices=("up", "down", "status", "ssh", "auth", "tui"))
    args = parser.parse_args()
    try:
        if args.action == "qmp-stop":
            qmp_powerdown_and_wait(args.pid)
            return 0
        if args.action == "proxy-ready":
            _proxy_ready()
            return 0
        if args.action == "check":
            qemu_argv()
            return 0
        if args.action == "guest-ready":
            _qemu_guest_ready()
            return 0
        if args.action == "provision":
            _provision()
            return 0
        if args.action == "locked-service":
            _locked_service_exec(args.role, args.owner)
            return 2
        if args.action == "owner":
            return _fixed_exec(args.operation)
    except (OSError, Refused, subprocess.SubprocessError, ValueError) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
