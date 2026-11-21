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


SERVICE_USER = "lm-vm"
PROXY_USER = "lm-vm-proxy"
CONTROL_GROUP = "lm-vm-control"
KVM_GROUP = "kvm"
STATE = pathlib.Path("/var/lib/little-mester-vm")
CONFIG = pathlib.Path("/etc/little-mester-vm")
DISK = STATE / "disk.qcow2"
SEED = STATE / "seed.iso"
CONTROL_KEY = CONFIG / "control_ed25519"
HOST_KEY = CONFIG / "ssh_host_ed25519_key"
KNOWN_HOSTS = CONFIG / "known_hosts"
QMP_RUNTIME = pathlib.Path("/run/little-mester-vm")
QMP_SOCKET_PATH = QMP_RUNTIME / "qmp.sock"
PROXY_RUNTIME = pathlib.Path("/run/lm-vm-proxy")
PROXY_PORT_FILE = PROXY_RUNTIME / "port"
SSH_PORT = 22222
PROXY_PORT = 32128
WORKSPACE_SOCKET = pathlib.Path("/run/little-mester-vm-workspace/virtiofs.sock")
QMP_STOP_TIMEOUT = 18.0
SSH_READY_TIMEOUT = 900.0
IMAGE_SHA256 = "04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94"
IMAGE_URL = "https://cloud-images.ubuntu.com/releases/noble/release-20261017/ubuntu-24.04-server-cloudimg-amd64.img"
IMAGE_NAME = "base-image.part"
DISK_PART = "disk.qcow2.part"
CODE_UPDATE_LOCK = CONFIG / "code-update.lock"
CODE_UPDATE_MARKER = CONFIG / "code-update.in-progress"
GUEST_COMPAT_MAX_OUTPUT = 8192


class Refused(RuntimeError):
    pass


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


def _validate_runtime_directory() -> None:
    try:
        info = os.lstat(QMP_RUNTIME)
        service_uid = pwd.getpwnam(SERVICE_USER).pw_uid
    except (KeyError, OSError) as error:
        raise Refused("private QMP RuntimeDirectory is unavailable") from error
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != service_uid or stat.S_IMODE(info.st_mode) != 0o700):
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
        vm_name="little-mester-system-vm", virtiofs_socket=str(WORKSPACE_SOCKET))


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


def qmp_powerdown_and_wait(pid: int, *, timeout_seconds: float = QMP_STOP_TIMEOUT) -> None:
    """Send identity-bound QMP powerdown and wait for actual QEMU process exit."""
    if type(pid) is not int or pid <= 1 or not 0 < timeout_seconds <= 20:
        raise Refused("invalid fixed QMP stop request")
    _validate_runtime_directory()
    try:
        service_uid = os.geteuid()
        sock_info = os.lstat(QMP_SOCKET_PATH)
    except OSError as error:
        raise Refused("private QMP socket is unavailable") from error
    if not stat.S_ISSOCK(sock_info.st_mode) or sock_info.st_uid != service_uid:
        raise Refused("QMP socket has unsafe type or owner")
    if not hasattr(os, "pidfd_open"):
        raise Refused("pidfd support is required to verify bounded QEMU exit")
    try:
        pidfd = os.pidfd_open(pid, 0)
    except OSError as error:
        # systemd can race ExecStop after QEMU has already exited.
        if error.errno == errno.ESRCH:
            return
        raise Refused("could not bind shutdown to the systemd MainPID") from error
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    deadline = time.monotonic() + timeout_seconds
    try:
        client.settimeout(min(2.0, timeout_seconds))
        client.connect(str(QMP_SOCKET_PATH))
        peer = struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if peer[0] != pid or peer[1] != service_uid:
            raise Refused("QMP peer differs from systemd's exact QEMU MainPID identity")
        buffer = bytearray()
        if not isinstance(_recv_qmp_line(client, deadline, buffer).get("QMP"), dict):
            raise Refused("QMP peer did not send its protocol greeting")
        for command in ("qmp_capabilities", "system_powerdown"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Refused("QMP exchange exceeded its bounded deadline")
            client.settimeout(remaining)
            client.sendall((json.dumps({"execute": command}, separators=(",", ":")) + "\r\n").encode())
            while True:
                response = _recv_qmp_line(client, deadline, buffer)
                if "event" in response:
                    continue
                if "error" in response or not isinstance(response.get("return"), dict):
                    raise Refused(f"QMP rejected fixed command {command}")
                break
        poller = select.poll()
        poller.register(pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        if not poller.poll(remaining_ms):
            raise Refused("QEMU did not exit after bounded QMP system_powerdown; systemd KILL fallback required")
    except (OSError, TimeoutError) as error:
        raise Refused("could not complete identity-bound bounded QMP shutdown") from error
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


def _guest_ssh(command: str, *, tty: bool = False, timeout: int = 20,
               capture_limit: int | None = None):
    _require_fixed_file(CONTROL_KEY, uid=0, mode=0o640, group=CONTROL_GROUP)
    _require_fixed_file(KNOWN_HOSTS, uid=0, mode=0o644)
    service_uid = pwd.getpwnam(SERVICE_USER).pw_uid
    uid = os.getuid()
    if uid == service_uid:
        runtime = QMP_RUNTIME
    else:
        runtime = pathlib.Path(f"/run/user/{uid}")
    try:
        runtime_info = os.lstat(runtime)
    except OSError as error:
        raise Refused("private SSH identity runtime is unavailable") from error
    if (not stat.S_ISDIR(runtime_info.st_mode) or stat.S_ISLNK(runtime_info.st_mode)
            or runtime_info.st_uid != uid or stat.S_IMODE(runtime_info.st_mode) != 0o700):
        raise Refused("private SSH identity runtime has unsafe owner or mode")
    key_dir = runtime / f"lm-system-ssh-{secrets.token_hex(12)}"
    key_dir.mkdir(mode=0o700)
    key_path = key_dir / "identity"
    key_fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        payload = CONTROL_KEY.read_bytes()
        view = memoryview(payload)
        while view:
            written = os.write(key_fd, view)
            if written <= 0:
                raise Refused("private SSH identity staging made no progress")
            view = view[written:]
        os.fsync(key_fd)
    finally:
        os.close(key_fd)
    ssh = ["/usr/bin/ssh", "-F", "/dev/null", "-p", str(SSH_PORT),
           "-i", str(key_path), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
           "-o", "IdentityAgent=none", "-o", "StrictHostKeyChecking=yes",
           "-o", f"UserKnownHostsFile={KNOWN_HOSTS}", "-o", "GlobalKnownHostsFile=/dev/null",
           "-o", "ProxyCommand=none", "-o", "ProxyJump=none", "-o", "ConnectTimeout=5",
           "-o", "ClearAllForwardings=yes", "-o", "RequestTTY=force" if tty else "RequestTTY=no",
           "mester@127.0.0.1", command]
    try:
        if capture_limit is None:
            result = subprocess.run(ssh, stdin=None if tty else subprocess.DEVNULL,
                                     timeout=timeout, check=False)
            return result.returncode
        if tty or type(capture_limit) is not int or not 0 < capture_limit <= GUEST_COMPAT_MAX_OUTPUT:
            raise Refused("bounded guest inspection parameters are invalid")
        process = subprocess.Popen(ssh, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, close_fds=True)
        assert process.stdout is not None
        output = bytearray()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    process.wait()
                    raise Refused("fixed guest compatibility inspection timed out")
                events = selector.select(remaining)
                if not events and process.poll() is not None:
                    break
                for key, _ in events:
                    chunk = os.read(key.fd, min(4096, capture_limit + 1 - len(output)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    if len(output) > capture_limit:
                        process.kill()
                        process.wait()
                        raise Refused("fixed guest compatibility response exceeded its bound")
            return_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.wait()
            raise
        finally:
            selector.close()
            process.stdout.close()
        return return_code, bytes(output)
    finally:
        info = os.lstat(key_path)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise Refused("temporary SSH identity changed; preserving the evidence")
        os.unlink(key_path)
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


def _ensure_guest_ready() -> None:
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "system_parent_session_contract", pathlib.Path(__file__).with_name("parent-session-contract.py"))
    if spec is None or spec.loader is None:
        raise Refused("protected parent session contract is unavailable")
    contract = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(contract)
    cloud_init_command = "cloud-init status --long >/dev/null 2>&1 || exit 41"
    deadline = time.monotonic() + SSH_READY_TIMEOUT
    while time.monotonic() < deadline:
        try:
            if _guest_ssh(cloud_init_command, timeout=15) == 0:
                break
        except (OSError, subprocess.SubprocessError):
            pass
        time.sleep(2)
    else:
        raise Refused("cloud-init or SSH did not complete before its bound")

    _check_guest_compatibility()
    command = contract.readiness_checks(workspace_path=contract.SYSTEM_WORKSPACE)
    deadline = time.monotonic() + SSH_READY_TIMEOUT
    while time.monotonic() < deadline:
        try:
            if _guest_ssh(command, timeout=15) == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
        time.sleep(2)
    raise Refused("protected session readiness did not complete before its bound")


def _guest_compatibility_module():
    path = pathlib.Path(__file__).with_name("guest-compatibility.py")
    spec = importlib.util.spec_from_file_location("system_guest_compatibility", path)
    if spec is None or spec.loader is None:
        raise Refused("fixed guest compatibility checker is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _check_guest_compatibility() -> None:
    checker = _guest_compatibility_module()
    command = checker.build_inspection_command(checker.SYSTEM_WORKSPACE)
    try:
        result = _guest_ssh(command, timeout=20, capture_limit=GUEST_COMPAT_MAX_OUTPUT)
    except (OSError, subprocess.SubprocessError, Refused):
        print("Guest compatibility: INCONCLUSIVE (fixed inspection failed)", file=sys.stderr)
        raise Refused("guest compatibility is inconclusive; normal VM use is blocked")
    if not isinstance(result, tuple) or len(result) != 2 or result[0] != 0:
        print("Guest compatibility: INCONCLUSIVE (fixed inspection unavailable)", file=sys.stderr)
        raise Refused("guest compatibility is inconclusive; normal VM use is blocked")
    status, diagnostics = checker.classify_inventory(result[1], checker.expected_inventory())
    print(f"Guest compatibility: {status}" +
          (" (" + ", ".join(diagnostics[:16]) + ")" if diagnostics else ""),
          file=sys.stderr if status != "MATCH" else sys.stdout)
    if status != "MATCH":
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


def _guest_workspace_mount() -> None:
    command = (
        "set -eu; test ! -L /workspace; mountpoint -q /workspace; "
        "test \"$(findmnt -n -o FSTYPE --target /workspace)\" = virtiofs; "
        "test \"$(findmnt -n -o SOURCE --target /workspace)\" = workspace; "
        "test -w /workspace")
    if _guest_ssh(command, timeout=20) != 0:
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


def _qemu_guest_ready() -> None:
    _proxy_ready()
    _ensure_guest_ready()
    _guest_workspace_mount()


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
