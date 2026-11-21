#!/usr/bin/env python3
"""Shared owner/CI acceptance for the fixed production systemd VM."""

from __future__ import annotations

import os
import contextlib
import datetime
import pathlib
import grp
import importlib.util
import ipaddress
import json
import pwd
import re
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid


UNIT = "little-mester-vm.service"
PROXY_UNIT = "little-mester-vm-proxy.service"
WORKSPACE_UNIT = "little-mester-vm-workspace.service"
METRICS_UNIT = "little-mester-vm-metrics.service"
SYSTEM_UNITS = (UNIT, PROXY_UNIT, WORKSPACE_UNIT, METRICS_UNIT)
CONFIG = pathlib.Path("/etc/little-mester-vm")
DISK = "/var/lib/little-mester-vm/disk.qcow2"
GUEST_PERSISTENCE_STATE = "/home/mester/.local/state"
GUEST_PERSISTENCE_MARKER_SCRIPT = r'''import os, stat, sys
ROOT_PATH = "/"
SYSFS_ROOT = "/sys"
action, name, expected = sys.argv[1:4]
if "/" in name or name in ("", ".", ".."):
    raise SystemExit("invalid persistence marker name")
flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
root_fd = os.open(ROOT_PATH, flags)
fds = [root_fd]
try:
    for component in ("home", "mester", ".local", "state"):
        fd = os.open(component, flags, dir_fd=fds[-1])
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise SystemExit("persistence directory component is not a directory")
        if component == "home" and stat.S_IMODE(info.st_mode) & 0o022:
            raise SystemExit("guest home parent is group or world writable")
        if component == "mester" and info.st_uid != os.getuid():
            raise SystemExit("guest home owner mismatch")
        if component == "mester" and stat.S_IMODE(info.st_mode) & 0o022:
            raise SystemExit("guest home is group or world writable")
        if component in (".local", "state") and (
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise SystemExit("private guest state ownership or mode mismatch")
        fds.append(fd)
    state_fd = fds[-1]
    root_info = os.fstat(root_fd)
    state_info = os.fstat(state_fd)

    def mount_id(fd):
        with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as stream:
            for line in stream:
                if line.startswith("mnt_id:"):
                    return int(line.split()[1])
        raise SystemExit("mount identity unavailable")

    root_mount_id = mount_id(root_fd)
    if mount_id(state_fd) != root_mount_id or state_info.st_dev != root_info.st_dev:
        raise SystemExit("guest state is not on the root mount")
    mount_types = {}
    with open("/proc/self/mountinfo", encoding="ascii") as stream:
        for line in stream:
            fields = line.split()
            dash = fields.index("-")
            mount_types[int(fields[0])] = fields[dash + 1]
    if root_mount_id not in mount_types or mount_types[root_mount_id] == "virtiofs":
        raise SystemExit("guest root mount identity/type unavailable or virtiofs")

    major, minor = os.major(root_info.st_dev), os.minor(root_info.st_dev)
    block = os.path.realpath(f"{SYSFS_ROOT}/dev/block/{major}:{minor}")
    if not os.path.exists(block):
        raise SystemExit("guest root block topology unavailable")
    if os.path.exists(os.path.join(block, "partition")):
        block = os.path.dirname(block)
    driver = os.path.realpath(os.path.join(block, "device", "driver"))
    if os.path.basename(driver) != "virtio_blk":
        raise SystemExit("guest root is not backed by virtio_blk")

    if action == "create":
        marker_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600, dir_fd=state_fd)
        try:
            info = os.fstat(marker_fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_dev != state_info.st_dev
                    or mount_id(marker_fd) != root_mount_id):
                raise SystemExit("new persistence marker identity mismatch")
            data = expected.encode("ascii")
            if os.write(marker_fd, data) != len(data):
                raise SystemExit("short persistence marker write")
            os.fsync(marker_fd)
        finally:
            os.close(marker_fd)
    elif action in ("verify", "remove"):
        marker_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                            dir_fd=state_fd)
        try:
            info = os.fstat(marker_fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_dev != state_info.st_dev
                    or mount_id(marker_fd) != root_mount_id):
                raise SystemExit("persistence marker identity mismatch")
            if os.read(marker_fd, 256) != expected.encode("ascii"):
                raise SystemExit("persistence marker content mismatch")
        finally:
            os.close(marker_fd)
        if action == "remove":
            current = os.stat(name, dir_fd=state_fd, follow_symlinks=False)
            if (current.st_dev != info.st_dev or current.st_ino != info.st_ino
                    or not stat.S_ISREG(current.st_mode)):
                raise SystemExit("persistence marker changed before removal")
            os.unlink(name, dir_fd=state_fd)
            os.fsync(state_fd)
    else:
        raise SystemExit("invalid persistence marker action")
finally:
    for fd in reversed(fds):
        os.close(fd)
'''


def _guest_persistence_marker_command(action: str, marker: str, expected: str) -> str:
    """Validate guest root storage and operate on one marker via verified dirfds."""
    if action not in {"create", "verify", "remove"}:
        raise ValueError("invalid guest persistence marker action")
    if pathlib.PurePosixPath(marker).name != marker or not marker.startswith(".lm-system-accept-"):
        raise ValueError("invalid guest persistence marker name")
    return "python3 -c " + shlex.quote(GUEST_PERSISTENCE_MARKER_SCRIPT) + " " + " ".join(
        shlex.quote(value) for value in (action, marker, expected))


class AcceptanceError(RuntimeError):
    safe_report_message = True


_DIAGNOSTIC_OUTPUT_LIMIT = 1200
_SENSITIVE_OUTPUT_LINE = re.compile(
    r"(?i)(auth|permission denied|publickey|private key|identity|password|token|secret|"
    r"credential|bearer|authorization|ssh.agent|host key)"
)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?(?:-----END [^-\r\n]*PRIVATE KEY-----|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_URL_CREDENTIALS = re.compile(r"(https?://)[^/@\s]+@", re.IGNORECASE)
_BEARER_CREDENTIAL = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_GITHUB_TOKEN = re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{10,}\b")
_GITHUB_FINE_GRAINED_TOKEN = re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")

_TIMED_STAGE_LABELS = frozenset({
    "initial_vm_start_readiness", "acceptance_checks", "clean_vm_shutdown",
    "same_disk_restart_readiness", "forced_stop_recovery", "final_cleanup",
    "candidate_install", "fresh_provisioning", "image_download_checksum",
    "image_conversion_resize", "cloud_seed_generation", "guest_readiness_ssh",
    "network_boundaries", "persistence_fixture", "host_metrics_transport",
    "direct_egress_blocked", "opencode_tui",
})


class StageTimer:
    """Emit bounded, secret-free, flushed stage timing records."""

    def __init__(self, stream=None, *, wall_clock=None, monotonic=None):
        self.stream = sys.stdout if stream is None else stream
        self.wall_clock = (wall_clock if wall_clock is not None else
                           lambda: datetime.datetime.now(datetime.timezone.utc))
        self.monotonic = time.monotonic if monotonic is None else monotonic

    @contextlib.contextmanager
    def timed_stage(self, label: str):
        if label not in _TIMED_STAGE_LABELS:
            raise ValueError("stage label is not allowlisted")
        started = self.monotonic()
        self._emit("START", label, self.wall_clock())
        outcome = "PASS"
        try:
            yield
        except BaseException:
            outcome = "FAIL"
            raise
        finally:
            elapsed_ms = max(0, round((self.monotonic() - started) * 1000))
            try:
                self._emit("END", label, self.wall_clock(),
                           elapsed_ms=elapsed_ms, outcome=outcome)
            except BaseException:
                # Timing must never replace an operation's original failure.
                if outcome == "PASS":
                    raise

    def _emit(self, event: str, label: str, timestamp: datetime.datetime, **extra) -> None:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("stage clock must return a timezone-aware timestamp")
        timestamp = timestamp.astimezone(datetime.timezone.utc)
        record = {"event": event, "label": label,
                  "timestamp": timestamp.isoformat().replace("+00:00", "Z"), **extra}
        self.stream.write("system_vm_stage=" + json.dumps(record, separators=(",", ":")) + "\n")
        self.stream.flush()


def _safe_output(value: bytes | str | None) -> str:
    if not value:
        return ""
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
    text = _ANSI_ESCAPE.sub("", text)
    text = _CONTROL_CHARS.sub("", text)
    text = _PRIVATE_KEY_BLOCK.sub("[private-key-redacted]", text)
    text = _URL_CREDENTIALS.sub(r"\1[credentials-redacted]@", text)
    text = _BEARER_CREDENTIAL.sub("Bearer [redacted]", text)
    text = _GITHUB_TOKEN.sub("[github-token-redacted]", text)
    text = _GITHUB_FINE_GRAINED_TOKEN.sub("[github-token-redacted]", text)
    lines = [line.strip() for line in text.splitlines()
             if line.strip() and not _SENSITIVE_OUTPUT_LINE.search(line)]
    joined = "\n".join(lines)
    return joined[:_DIAGNOSTIC_OUTPUT_LIMIT] + (
        "…[truncated]" if len(joined) > _DIAGNOSTIC_OUTPUT_LIMIT else "")


def run(argv: list[str], *, timeout: float = 30, capture: bool = True,
        operation: str = "fixed acceptance command") -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE if capture else None,
                                stderr=subprocess.PIPE if capture else None,
                                timeout=timeout, check=False,
                                env={**os.environ, "LC_ALL": "C"})
    except subprocess.TimeoutExpired as error:
        output = _safe_output(error.stderr)
        detail = f"; stderr={output}" if output else ""
        raise AcceptanceError(f"{operation} timed out after {timeout}s{detail}") from error
    except OSError as error:
        reason = error.strerror or type(error).__name__
        raise AcceptanceError(f"{operation} failed ({type(error).__name__}: {reason})") from error
    if result.returncode:
        output = _safe_output(result.stderr)
        detail = f"; stderr={output}" if output else ""
        raise AcceptanceError(f"{operation} failed (exit={result.returncode}){detail}")
    return result


def service_state(unit: str, *, timeout: float = 30) -> dict[str, str]:
    result = run(["/usr/bin/systemctl", "show", "--no-pager", "--property=LoadState",
                  "--property=ActiveState", "--property=MainPID", "--property=Result", unit],
                 timeout=timeout)
    return dict(line.split("=", 1) for line in result.stdout.decode().splitlines() if "=" in line)


def _verify_workspace_daemon_privileges() -> None:
    state = service_state(WORKSPACE_UNIT)
    if state.get("ActiveState") != "active":
        raise AcceptanceError("workspace daemon is not active for runtime privilege verification")
    try:
        pid = int(state.get("MainPID", "0"))
        if pid <= 1:
            raise ValueError("missing daemon PID")
        status = pathlib.Path(f"/proc/{pid}/status").read_text(encoding="ascii")
    except (OSError, UnicodeError, ValueError) as error:
        raise AcceptanceError("workspace daemon runtime privilege evidence is unavailable") from error
    values = {}
    for line in status.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key] = value.strip()
    if values.get("NoNewPrivs") != "0":
        raise AcceptanceError("workspace daemon has effective NoNewPrivs set; virtiofsd ID mapping cannot work")


def _effective_argv(value: str) -> tuple[str, str]:
    if (value.count("{ path=") != 1 or value.count("; argv[]=") != 1
            or value.count("; ignore_errors=") != 1):
        raise AcceptanceError("systemd returned multiple effective commands for a fixed operation")
    match = re.fullmatch(r"\{\s*path=([^ ;]+)\s*;\s*argv\[\]=(.*?)\s*;\s*ignore_errors=.*\}", value)
    if not match:
        raise AcceptanceError("systemd returned malformed effective command evidence")
    raw_argv = match.group(2)
    if not raw_argv.startswith(match.group(1)):
        raise AcceptanceError("effective systemd executable differs from its argv")
    # systemd's show format joins argv elements with spaces and does not retain
    # quoting for an argument that itself contains spaces. Compare its complete
    # serialization to the exact candidate argv, not a lossy shlex split.
    return match.group(1), raw_argv


def _effective_argvs(value: str) -> tuple[tuple[str, str], ...]:
    """Parse the complete ordered systemd command list for multi-command properties."""
    pattern = re.compile(
        r"\{\s*path=([^ ;]+)\s*;\s*argv\[\]=(.*?)\s*;\s*ignore_errors=.*?\}")
    found = tuple(pattern.finditer(value))
    if (not found or value.count("{ path=") != len(found)
            or value.count("; argv[]=") != len(found)
            or value.count("; ignore_errors=") != len(found)):
        raise AcceptanceError("systemd returned malformed effective command-list evidence")
    previous_end = 0
    for match in found:
        if value[previous_end:match.start()].strip() not in ("", ";"):
            raise AcceptanceError("systemd returned extra effective command-list data")
        if not match.group(2).startswith(match.group(1)):
            raise AcceptanceError("effective systemd executable differs from its argv")
        previous_end = match.end()
    if value[previous_end:].strip():
        raise AcceptanceError("systemd returned trailing effective command-list data")
    return tuple((match.group(1), match.group(2)) for match in found)


def disk_identity() -> tuple[int, int]:
    result = run(["/usr/bin/sudo", "-n", "/usr/bin/stat", "--format=%f:%d:%i", "--", DISK])
    match = re.fullmatch(rb"([0-9a-fA-F]+):([0-9]+):([0-9]+)\n", result.stdout)
    if not match or not stat.S_ISREG(int(match.group(1), 16)):
        raise AcceptanceError("fixed VM disk is missing or is not a regular file; preserving state")
    return int(match.group(2)), int(match.group(3))


def _mark(progress, name: str, **evidence) -> None:
    if progress is not None:
        progress(name, **evidence)


def render_workspace_unit_template(payload: bytes, *, owner: str, owner_group: str) -> bytes:
    """Render the exact fixed owner identity in both workspace unit positions."""
    markers = ((b"@LM_VM_OWNER@", owner, 2),
               (b"@LM_VM_OWNER_GROUP@", owner_group, 1))
    for marker, value, count in markers:
        if payload.count(marker) != count:
            raise AcceptanceError("candidate workspace unit identity marker is ambiguous")
        try:
            encoded = value.encode("ascii")
        except UnicodeEncodeError as error:
            raise AcceptanceError("candidate workspace unit identity is not ASCII") from error
        payload = payload.replace(marker, encoded)
    return payload


def verify(root: pathlib.Path, progress=None) -> tuple[dict[str, str], dict[str, str]]:
    """Read-only verification: never starts, stops, installs, or repairs anything."""
    manager = run(["/usr/bin/systemctl", "show", "--property=Version", "--value"], timeout=5)
    if not manager.stdout.strip():
        raise AcceptanceError("system service manager is unavailable")
    kvm = os.stat("/dev/kvm", follow_symlinks=False)
    kvm_group = grp.getgrnam("kvm")
    if not stat.S_ISCHR(kvm.st_mode) or kvm.st_gid != kvm_group.gr_gid or stat.S_IMODE(kvm.st_mode) & 0o060 != 0o060:
        raise AcceptanceError("/dev/kvm is not group-readable/writable by the existing kvm group")
    for user, group in (("lm-vm", "lm-vm"), ("lm-vm-proxy", "lm-vm-proxy")):
        account = pwd.getpwnam(user)
        if (not 1 <= account.pw_uid < 1000 or account.pw_gid != grp.getgrnam(group).gr_gid
                or account.pw_shell != "/usr/sbin/nologin"):
            raise AcceptanceError(f"fixed service identity does not match policy: {user}")
    states = tuple(service_state(unit) for unit in SYSTEM_UNITS)
    if any(item.get("LoadState") != "loaded" for item in states):
        raise AcceptanceError("all fixed production systemd units must already be loaded")
    # Validate the immutable installed runtime and protected disk identity without mutation.
    runtime = pathlib.Path("/usr/lib/little-mester-vm")
    for name in ("system-vm.py", "guest-compatibility.py", "system-vm-workspace.py", "system-vm-metrics.py",
                 "parent-metrics.py", "parent-metrics-collect.py", "guest-opencode-preferences.py",
                 "guest-bootstrap.py", "parent-qemu.py", "parent-provision.py",
                 "parent-session-contract.py", "guest-user-dirs.py", "proxy.py",
                 "browser-auth-session.py", "callback-listener.py"):
        path = runtime / name
        info = os.lstat(path)
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022):
            raise AcceptanceError(f"deployed runtime asset is not protected: {name}")
        if path.read_bytes() != (root / "lib/vm" / name).read_bytes():
            raise AcceptanceError(f"deployed runtime asset differs from the installed candidate: {name}")
    for source, destination, mode in (
             ("systemd/little-mester-vm.service", "/etc/systemd/system/little-mester-vm.service", 0o644),
             ("systemd/little-mester-vm-proxy.service", "/etc/systemd/system/little-mester-vm-proxy.service", 0o644),
              ("systemd/little-mester-vm-workspace.service", "/etc/systemd/system/little-mester-vm-workspace.service", 0o644),
              ("systemd/little-mester-vm-metrics.service", "/etc/systemd/system/little-mester-vm-metrics.service", 0o644),
              ("systemd/little-mester-vm.service.d/90-code-update-guard.conf", "/etc/systemd/system/little-mester-vm.service.d/90-code-update-guard.conf", 0o644),
              ("systemd/little-mester-vm-proxy.service.d/90-code-update-guard.conf", "/etc/systemd/system/little-mester-vm-proxy.service.d/90-code-update-guard.conf", 0o644),
              ("systemd/little-mester-vm-workspace.service.d/90-code-update-guard.conf", "/etc/systemd/system/little-mester-vm-workspace.service.d/90-code-update-guard.conf", 0o644),
              ("systemd/little-mester-vm-metrics.service.d/90-code-update-guard.conf", "/etc/systemd/system/little-mester-vm-metrics.service.d/90-code-update-guard.conf", 0o644),
             ("sudoers/little-mester-vm", "/etc/sudoers.d/little-mester-vm", 0o440)):
        target = pathlib.Path(destination)
        if destination.endswith("sudoers.d/little-mester-vm"):
            metadata = run(["/usr/bin/sudo", "-n", "/usr/bin/stat",
                            "--format=%f:%u:%g:%h", "--", destination]).stdout
            match = re.fullmatch(rb"([0-9a-fA-F]+):0:0:1\n", metadata)
            if not match:
                raise AcceptanceError("deployed sudoers metadata is unsafe")
            mode_bits = int(match.group(1), 16)
            if not stat.S_ISREG(mode_bits) or stat.S_IMODE(mode_bits) != mode:
                raise AcceptanceError("deployed sudoers type or mode is unsafe")
        else:
            info = os.lstat(target)
            if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_uid != 0 or info.st_gid != 0 or stat.S_IMODE(info.st_mode) != mode
                    or info.st_nlink != 1):
                raise AcceptanceError(f"deployed production asset metadata is unsafe: {source}")
        expected = (root / source).read_bytes()
        if source == "systemd/little-mester-vm-workspace.service":
            owner_info = pwd.getpwuid(os.getuid())
            expected = render_workspace_unit_template(
                expected, owner=owner_info.pw_name,
                owner_group=grp.getgrgid(owner_info.pw_gid).gr_name)
        if destination.endswith("sudoers.d/little-mester-vm"):
            actual = run(["/usr/bin/sudo", "-n", "/usr/bin/cat", "--", destination]).stdout
        else:
            actual = target.read_bytes()
        if actual != expected:
            raise AcceptanceError(f"deployed production asset differs from candidate: {source}")
    for name in ("package.json", "tui.tsx"):
        source = root / "agent/opencode/plugins/little-mester-exit-confirm" / name
        target = runtime / "plugins" / name
        info = os.lstat(target)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o644 or target.read_bytes() != source.read_bytes()):
            raise AcceptanceError(f"deployed OpenCode plugin differs from candidate: {name}")
    for name in ("package.json", "tui.tsx", "metrics-endpoint.js", "metrics-endpoint.d.ts"):
        source = root / "agent/opencode/plugins/host-metrics-tui" / name
        target = runtime / "host-metrics-tui" / name
        info = os.lstat(target)
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o644 or target.read_bytes() != source.read_bytes()):
            raise AcceptanceError(f"deployed host metrics plugin differs from candidate: {name}")
    workspace_spec = importlib.util.spec_from_file_location(
        "verify_system_vm_workspace", root / "lib/vm/system-vm-workspace.py")
    if workspace_spec is None or workspace_spec.loader is None:
        raise AcceptanceError("workspace assignment verifier is unavailable")
    workspace_policy = importlib.util.module_from_spec(workspace_spec)
    sys.modules[workspace_spec.name] = workspace_policy
    workspace_spec.loader.exec_module(workspace_policy)
    assignment = workspace_policy.read_assignment(workspace_policy.ASSIGNMENT, uid=os.getuid())
    properties = {}
    for unit in SYSTEM_UNITS:
        output = run(["/usr/bin/systemctl", "show", "--no-pager",
                      "--property=FragmentPath", "--property=DropInPaths",
            "--property=ConditionPathExists",
            "--property=User", "--property=Group", "--property=ProtectSystem",
                      "--property=NoNewPrivileges", "--property=DevicePolicy",
                      "--property=NeedDaemonReload", "--property=ExecStart",
                       "--property=ExecStartPre", "--property=ExecStartPost", "--property=ExecStop",
                       "--property=ExecStopPost", "--property=ProtectHome",
                       "--property=RestrictAddressFamilies", "--property=ReadWritePaths",
                       "--property=BindPaths",
                       "--property=Requires", "--property=BindsTo", "--property=After",
                       "--property=SupplementaryGroups",
                       "--property=NoExecPaths", "--property=ExecPaths",
                      unit]).stdout.decode()
        values = {}
        for line in output.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key in values:
                raise AcceptanceError("systemd returned duplicate effective property evidence")
            values[key] = value
        properties[unit] = values
    vm_properties = properties[UNIT]
    proxy_properties = properties[PROXY_UNIT]
    workspace_properties = properties[WORKSPACE_UNIT]
    metrics_properties = properties[METRICS_UNIT]
    qemu_spec = importlib.util.spec_from_file_location(
        "candidate_parent_qemu", root / "lib/vm/parent-qemu.py")
    if qemu_spec is None or qemu_spec.loader is None:
        raise AcceptanceError("candidate QEMU argument builder is unavailable")
    qemu = importlib.util.module_from_spec(qemu_spec)
    qemu_spec.loader.exec_module(qemu)
    expected_qemu = qemu.build_system_qemu_argv(
        executable="/usr/bin/qemu-system-x86_64", disk_path=DISK,
        seed_iso="/var/lib/little-mester-vm/seed.iso", ssh_port=22222,
        proxy_port=32128, qmp_socket="/run/little-mester-vm/qmp.sock",
        vm_name="little-mester-system-vm",
        virtiofs_socket="/run/little-mester-vm-workspace/virtiofs.sock")
    helper = ["/usr/bin/python3", "-I", "/usr/lib/little-mester-vm/system-vm.py",
              "locked-service"]
    expected_qemu_command = [*helper, "qemu"]
    expected_proxy = [*helper, "proxy"]
    owner_account = pwd.getpwuid(assignment.uid)
    owner_name = owner_account.pw_name
    expected_workspace = [*helper, "workspace", f"--owner={owner_name}"]
    expected_metrics = [*helper, "metrics"]
    guard_command = ["/usr/bin/flock", "--shared", "--nonblock",
                     "/etc/little-mester-vm/code-update.lock", "/usr/bin/true"]
    expected_unit_commands = {
        "ExecStart": expected_qemu_command,
        "ExecStartPre": (["/usr/bin/python3", "-I", "/usr/lib/little-mester-vm/system-vm.py", "check"],
                          guard_command),
        "ExecStartPost": ["/usr/bin/python3", "-I", "/usr/lib/little-mester-vm/system-vm.py", "guest-ready"],
        "ExecStop": ["/usr/bin/python3", "-I", "/usr/lib/little-mester-vm/system-vm.py", "qmp-stop", "$MAINPID"],
    }
    expected_proxy_commands = {
        "ExecStart": expected_proxy,
        "ExecStartPre": (guard_command,),
        "ExecStartPost": ["/usr/bin/python3", "-I", "/usr/lib/little-mester-vm/system-vm.py", "proxy-ready"],
    }
    expected_workspace_commands = {
        "ExecStart": expected_workspace,
        "ExecStartPre": (guard_command,),
        "ExecStartPost": ["/usr/bin/python3", "-I",
                          "/usr/lib/little-mester-vm/system-vm-workspace.py", "socket-permissions"],
    }
    expected_metrics_commands = {"ExecStart": expected_metrics, "ExecStartPre": (guard_command,)}

    def commands_match(unit_properties, expected_commands):
        for name, expected in expected_commands.items():
            commands = expected if name == "ExecStartPre" else (expected,)
            wanted = tuple((command[0], " ".join(command)) for command in commands)
            if _effective_argvs(unit_properties.get(name, "")) != wanted:
                return False
        return True

    commands_match_vm = commands_match(vm_properties, expected_unit_commands)
    proxy_commands_match = commands_match(proxy_properties, expected_proxy_commands)
    workspace_commands_match = commands_match(workspace_properties, expected_workspace_commands)
    metrics_commands_match = commands_match(metrics_properties, expected_metrics_commands)
    expected_dropins = {
        UNIT: "/etc/systemd/system/little-mester-vm.service.d/90-code-update-guard.conf",
        PROXY_UNIT: "/etc/systemd/system/little-mester-vm-proxy.service.d/90-code-update-guard.conf",
        WORKSPACE_UNIT: "/etc/systemd/system/little-mester-vm-workspace.service.d/90-code-update-guard.conf",
        METRICS_UNIT: "/etc/systemd/system/little-mester-vm-metrics.service.d/90-code-update-guard.conf",
    }
    dropins_match = all(
        expected_dropins[unit] in unit_properties.get("DropInPaths", "").split()
        and f"!{CONFIG}/code-update.in-progress" in unit_properties.get("ConditionPathExists", "")
        for unit, unit_properties in ((UNIT, vm_properties), (PROXY_UNIT, proxy_properties),
                                     (WORKSPACE_UNIT, workspace_properties),
                                     (METRICS_UNIT, metrics_properties))
    )
    workspace_dropin = "/etc/systemd/system/little-mester-vm-workspace.service.d/20-workspace.conf"
    workspace_bind_matches = workspace_policy.effective_bind_paths_match(
        workspace_properties.get("BindPaths", ""), assignment.path,
        workspace_policy.SHARE_DIR)
    if (vm_properties.get("FragmentPath") != "/etc/systemd/system/little-mester-vm.service"
            or proxy_properties.get("FragmentPath") != "/etc/systemd/system/little-mester-vm-proxy.service"
            or workspace_properties.get("FragmentPath") != "/etc/systemd/system/little-mester-vm-workspace.service"
            or set(workspace_properties.get("DropInPaths", "").split())
               != {workspace_dropin, expected_dropins[WORKSPACE_UNIT]}
            or metrics_properties.get("FragmentPath") != "/etc/systemd/system/little-mester-vm-metrics.service"
            or vm_properties.get("User") != "lm-vm" or vm_properties.get("Group") != "lm-vm"
            or vm_properties.get("ProtectSystem") != "strict"
            or vm_properties.get("NoNewPrivileges") != "yes"
            or vm_properties.get("DevicePolicy") != "closed"
            or vm_properties.get("NeedDaemonReload") != "no"
            or not commands_match_vm or not dropins_match
             or any(name not in vm_properties.get("Requires", "") for name in
                    (PROXY_UNIT, WORKSPACE_UNIT, METRICS_UNIT))
             or any(name not in vm_properties.get("BindsTo", "") for name in
                    (PROXY_UNIT, WORKSPACE_UNIT, METRICS_UNIT))
            or vm_properties.get("ExecStopPost", "") not in ("", "-")
            or proxy_properties.get("User") != "lm-vm-proxy"
            or proxy_properties.get("Group") != "lm-vm-proxy"
            or proxy_properties.get("NoNewPrivileges") != "yes"
            or proxy_properties.get("ProtectSystem") != "strict"
            or proxy_properties.get("NeedDaemonReload") != "no"
            or not proxy_commands_match
             or proxy_properties.get("ExecStop", "") not in ("", "-")
            or proxy_properties.get("ExecStopPost", "") not in ("", "-")):
        raise AcceptanceError("effective loaded systemd VM properties differ from the protected unit policy")
    if (workspace_properties.get("User") != owner_name
            or workspace_properties.get("Group") != grp.getgrgid(owner_account.pw_gid).gr_name
            or "lm-vm-control" not in workspace_properties.get("SupplementaryGroups", "")
            or workspace_properties.get("ProtectHome") != "tmpfs"
            or workspace_properties.get("ProtectSystem") != "strict"
            or workspace_properties.get("NoNewPrivileges") != "no"
            or workspace_properties.get("NoExecPaths") != "/"
            or any(path not in workspace_properties.get("ExecPaths", "")
                   for path in ("/usr/bin", "/usr/lib", "/usr/libexec", "/lib"))
            or workspace_properties.get("NeedDaemonReload") != "no"
            or not workspace_commands_match
            or not workspace_bind_matches
            or str(workspace_policy.SHARE_DIR) not in workspace_properties.get("ReadWritePaths", "")
            or metrics_properties.get("User") != "lm-vm"
            or metrics_properties.get("Group") != "lm-vm"
            or metrics_properties.get("ProtectSystem") != "strict"
            or metrics_properties.get("NoNewPrivileges") != "yes"
            or metrics_properties.get("DevicePolicy") != "closed"
            or metrics_properties.get("RestrictAddressFamilies") != "AF_INET"
            or metrics_properties.get("NeedDaemonReload") != "no"
            or not metrics_commands_match):
        raise AcceptanceError("effective workspace or metrics systemd policy differs from the fixed owner boundary")
    dropin = pathlib.Path(workspace_dropin)
    dropin_info = os.lstat(dropin)
    if (not stat.S_ISREG(dropin_info.st_mode) or stat.S_ISLNK(dropin_info.st_mode)
            or dropin_info.st_uid != 0 or stat.S_IMODE(dropin_info.st_mode) != 0o644
            or dropin_info.st_nlink != 1
            or dropin.read_bytes() != workspace_policy._systemd_bind_path(assignment.path).encode("utf-8")):
        raise AcceptanceError("workspace systemd drop-in differs from the validated owner assignment")
    metrics_service = service_state(METRICS_UNIT)
    if metrics_service.get("ActiveState") == "active":
        _metrics_loopback_check()
    identity = disk_identity()
    _mark(progress, "deployed_candidate_verification",
          initial_vm_state=states[0], initial_proxy_state=states[1],
          disk_identity={"device": identity[0], "inode": identity[1]})
    print("system_vm_gate=deployed_candidate_verification:PASS")
    print(f"system_vm_verify=PASS disk_device={identity[0]} disk_inode={identity[1]}")
    return states[0], states[1]


def _wait_active(unit: str, timeout: int = 900, *,
                 operation: str = "wait for VM readiness") -> dict[str, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = service_state(unit)
        if state.get("ActiveState") == "active" and int(state.get("MainPID", "0")) > 1:
            return state
        if state.get("ActiveState") == "failed":
            raise AcceptanceError(f"{operation}: production unit entered failed state")
        time.sleep(1)
    raise AcceptanceError(f"{operation}: production unit readiness timed out after {timeout}s")


def _network_checks(vm: dict[str, str]) -> None:
    pid = int(vm["MainPID"])
    argv = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
    if b"restrict=on" not in argv or b"hostfwd=tcp:127.0.0.1:22222-:22" not in argv:
        raise AcceptanceError("QEMU network restrictions or loopback SSH forwarding differ")
    proxy = service_state(PROXY_UNIT)
    if proxy.get("ActiveState") != "active" or int(proxy.get("MainPID", "0")) <= 1:
        raise AcceptanceError("restricted egress proxy is not active")
    with socket.create_connection(("127.0.0.1", 32128), timeout=2):
        pass
    listeners = run(["/usr/bin/ss", "-H", "-ltn"]).stdout
    if (b"127.0.0.1:32128" not in listeners or b"0.0.0.0:32128" in listeners
            or b"[::]:32128" in listeners):
        raise AcceptanceError("proxy listener is not loopback-only")
    _metrics_loopback_check()


def _metrics_loopback_check() -> None:
    listeners = run(["/usr/bin/ss", "-H", "-ltn"]).stdout
    if (b"127.0.0.1:32129" not in listeners
            or b"0.0.0.0:32129" in listeners
            or b"[::]:32129" in listeners
            or b"*:32129" in listeners):
        raise AcceptanceError("host metrics listener is not bound exclusively to IPv4 loopback")


def _host_metrics_check() -> None:
    command = (
        "python3 -I -c 'import http.client,json,urllib.request; "
        "u=\"http://10.0.2.100:19532/metrics\"; r=urllib.request.urlopen(u,timeout=3); "
        "b=r.read(16385); assert len(b)<=16384; d=json.loads(b); "
        "assert set(d)=={\"source\",\"timestamp\",\"cpu\",\"memory\",\"gpu\",\"availability\",\"temperatures\"}; "
        "assert d[\"source\"]==\"host\" and isinstance(d[\"gpu\"],list); "
        "assert set(d[\"availability\"])=={\"cpu\",\"memory\",\"gpus\",\"temperatures\"}; "
        "allowed={\"available\",\"unsupported\",\"no_device\",\"no_sensors\",\"timeout\","
        "\"permission_denied\",\"collector_failed\",\"malformed\"}; "
        "assert all(v in allowed for v in d[\"availability\"].values()); "
        "c=http.client.HTTPConnection(\"10.0.2.100\",19532,timeout=3); "
        "c.request(\"POST\",\"/metrics\"); p=c.getresponse(); assert p.status==405; c.close()'")
    _guest_ssh(command, operation="verify_host_metrics_transport")


def _guest_ssh(command: str, *, timeout: int = 30,
               operation: str = "run fixed guest SSH check") -> None:
    key = pathlib.Path("/etc/little-mester-vm/control_ed25519").read_bytes()
    known = pathlib.Path("/etc/little-mester-vm/known_hosts").read_bytes()
    runtime = pathlib.Path(f"/run/user/{os.getuid()}")
    info = os.lstat(runtime)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise AcceptanceError("private owner runtime directory is unavailable or unsafe")
    with tempfile.TemporaryDirectory(prefix="lm-system-accept-", dir=runtime) as directory:
        key_path, hosts_path = pathlib.Path(directory) / "identity", pathlib.Path(directory) / "known_hosts"
        key_path.write_bytes(key)
        key_path.chmod(0o600)
        hosts_path.write_bytes(known)
        hosts_path.chmod(0o600)
        run(["/usr/bin/ssh", "-F", "/dev/null", "-p", "22222", "-i", str(key_path),
             "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
             "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={hosts_path}",
             "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ProxyCommand=none", "-o", "ProxyJump=none",
             "-o", "ConnectTimeout=5", "-o", "ClearAllForwardings=yes",
             "mester@127.0.0.1", f"/bin/bash -lc {shlex.quote(command)}"],
            timeout=timeout, operation=operation)


def _tui_check(root: pathlib.Path) -> None:
    try:
        import pexpect
    except ImportError as error:
        raise AcceptanceError("python3-pexpect is required for the OpenCode TUI acceptance check") from error
    child = pexpect.spawn(str(root / "lab-vm"), ["system", "tui"],
                          env={**os.environ, "TERM": "xterm-256color", "LM_VM_TERM_VALIDATED": "1"},
                          encoding=None, timeout=90, dimensions=(32, 100))
    try:
        child.expect(b"Ask anything", timeout=90)
        child.sendcontrol("c")
        child.expect(b"Close Little Mester?", timeout=10)
        child.send(b"q")
        child.expect(pexpect.EOF, timeout=20)
        child.close()
        if child.exitstatus != 0 or child.signalstatus is not None:
            raise AcceptanceError("OpenCode TUI did not close cleanly")
    finally:
        if child.isalive():
            child.close(force=True)


def _lifecycle_preflight(root: pathlib.Path) -> None:
    """Resolve owner-side dependencies and protected credentials before starting QEMU."""
    try:
        import pexpect  # noqa: F401
    except ImportError as error:
        raise AcceptanceError("python3-pexpect is required for OpenCode TUI acceptance") from error
    for tool in ("/usr/bin/ssh", "/usr/bin/ss", "/usr/bin/stat"):
        if not os.path.isfile(tool) or not os.access(tool, os.X_OK):
            raise AcceptanceError(f"fixed owner acceptance dependency is unavailable: {pathlib.Path(tool).name}")
    runtime = pathlib.Path(f"/run/user/{os.getuid()}")
    info = os.lstat(runtime)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise AcceptanceError("private owner runtime directory is unavailable or unsafe")
    control_gid = grp.getgrnam("lm-vm-control").gr_gid
    if control_gid not in os.getgroups() and control_gid != os.getegid():
        raise AcceptanceError("invoking process lacks effective lm-vm-control group membership")
    for path, gid, mode in ((pathlib.Path("/etc/little-mester-vm/control_ed25519"), control_gid, 0o640),
                            (pathlib.Path("/etc/little-mester-vm/known_hosts"), 0, 0o644)):
        info = os.lstat(path)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != gid
                or stat.S_IMODE(info.st_mode) != mode or info.st_nlink != 1):
            raise AcceptanceError(f"fixed SSH credential asset is not safely deployed: {path.name}")


def _proxy_policy_checks() -> None:
    path = pathlib.Path("/usr/lib/little-mester-vm/proxy.py")
    spec = importlib.util.spec_from_file_location("installed_vm_proxy_policy", path)
    if spec is None or spec.loader is None:
        raise AcceptanceError("deployed egress policy module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    blocked = module.host_networks()
    for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1"):
        if module._is_allowed_public_destination(ipaddress.ip_address(address), blocked):
            raise AcceptanceError("egress proxy policy allowed a private or link-local destination")


def _initial_running_state(vm: dict[str, str], proxy: dict[str, str]) -> bool:
    states = {UNIT: vm, PROXY_UNIT: proxy}
    states.update({unit: service_state(unit) for unit in (WORKSPACE_UNIT, METRICS_UNIT)})
    running = []
    for unit, state in states.items():
        active = state.get("ActiveState") == "active"
        pid = int(state.get("MainPID", "0"))
        if ((active and pid <= 1)
                or (state.get("ActiveState") in ("inactive", "failed") and pid != 0)
                or state.get("ActiveState") not in ("active", "inactive", "failed")):
            raise AcceptanceError(f"unit state is ambiguous; preserving it without lifecycle changes: {unit}")
        running.append(active)
    if any(value != running[0] for value in running[1:]):
        raise AcceptanceError("fixed VM service lifecycle state is ambiguous; preserving all units")
    return running[0]


def _restore_initial_state(root: pathlib.Path, was_running: bool, *,
                           timeout: int = 45) -> tuple[dict[str, str], dict[str, str]]:
    operation = "up" if was_running else "down"
    run([str(root / "lab-vm"), "system", operation], timeout=1005 if was_running else 60,
        operation=f"restore_initial_vm_state_{operation}")
    deadline = time.monotonic() + timeout
    previous: tuple[tuple[str | None, str | None, str | None], ...] | None = None
    stable_samples = 0
    states: tuple[dict[str, str], ...] = ()
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        states = tuple(service_state(unit, timeout=remaining) for unit in SYSTEM_UNITS)
        if was_running:
            restored = all(state.get("ActiveState") == "active"
                           and int(state.get("MainPID", "0")) > 1 for state in states)
        else:
            restored = all(state.get("ActiveState") in ("inactive", "failed")
                           and state.get("MainPID") == "0" for state in states)
        signature = tuple((state.get("ActiveState"), state.get("MainPID"), state.get("Result"))
                          for state in states)
        if restored and signature == previous:
            stable_samples += 1
            if stable_samples >= 2:
                return states[0], states[1]
        else:
            stable_samples = 0
        previous = signature
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.1, remaining))
    raise AcceptanceError(
        f"initial lifecycle state did not settle within {timeout}s "
        f"(initially_running={was_running}): {states}")


def _require_normal_stop(timeout: int = 30) -> None:
    """Wait for both systemd stop jobs to finish, rejecting failures and live PIDs."""
    deadline = time.monotonic() + timeout
    states: dict[str, dict[str, str]] = {}
    while True:
        states = {}
        pending = False
        for unit in SYSTEM_UNITS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AcceptanceError(f"unit shutdown did not complete within {timeout}s: {states}")
            state = service_state(unit, timeout=remaining)
            states[unit] = state
            active = state.get("ActiveState")
            raw_pid = state.get("MainPID")
            if raw_pid is None:
                raise AcceptanceError(f"systemd omitted MainPID during normal shutdown: {unit} ({state})")
            pid = int(raw_pid)
            if active == "failed":
                raise AcceptanceError(f"unit entered failed state during normal shutdown: {unit} ({state})")
            if active == "inactive":
                if pid != 0:
                    raise AcceptanceError(f"inactive unit has an unexpected live MainPID: {unit} ({state})")
                if state.get("Result") != "success":
                    raise AcceptanceError(f"unit did not stop cleanly: {unit} ({state})")
                continue
            if active == "deactivating":
                # A live MainPID is expected only while systemd completes this stop job.
                if pid < 0 or pid == 1:
                    raise AcceptanceError(f"deactivating unit has an unexpected MainPID: {unit} ({state})")
                pending = True
                continue
            raise AcceptanceError(f"unit was not stopped and is not completing shutdown: {unit} ({state})")
        if not pending:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AcceptanceError(f"unit shutdown did not complete within {timeout}s: {states}")
        time.sleep(min(0.1, remaining))


def accept(root: pathlib.Path, progress=None) -> int:
    timer = StageTimer()
    initial_vm, initial_proxy = verify(root, progress=progress)
    vm_running = _initial_running_state(initial_vm, initial_proxy)
    sys.dont_write_bytecode = True
    _lifecycle_preflight(root)
    _proxy_policy_checks()
    before_disk = disk_identity()
    _mark(progress, "preflight", initial_vm_running=vm_running,
          initial_vm_state=initial_vm, initial_proxy_state=initial_proxy,
          disk_identity={"device": before_disk[0], "inode": before_disk[1]})
    _mark(progress, "proxy_private_target_policy")
    print("system_vm_gate=proxy_private_target_policy:PASS")
    marker = f".lm-system-accept-{uuid.uuid4().hex}"
    expected = f"systemd-owner-acceptance-{uuid.uuid4().hex}"
    _mark(progress, "lifecycle_started", lifecycle_started=True,
          restoration_outcome="PENDING")
    stage = "guest_readiness_ssh"
    operation = "start_initial_vm"
    try:
        with timer.timed_stage("initial_vm_start_readiness"):
            run([str(root / "lab-vm"), "system", "up"], timeout=1005, operation=operation)
            operation = "wait_for_initial_guest_readiness"
            first = _wait_active(UNIT, operation=operation)
            _verify_workspace_daemon_privileges()
        _mark(progress, "guest_readiness_ssh")
        print("system_vm_gate=guest_readiness_ssh:PASS")
        stage = "network_boundaries"
        operation = "verify_duplicate_start_is_idempotent"
        run([str(root / "lab-vm"), "system", "up"], timeout=60, operation=operation)
        duplicate = service_state(UNIT)
        if duplicate.get("MainPID") != first.get("MainPID"):
            raise AcceptanceError("duplicate start replaced the active QEMU MainPID")
        operation = "verify_initial_network_boundaries"
        with timer.timed_stage("network_boundaries"):
            _network_checks(first)
        _mark(progress, "network_boundaries")
        print("system_vm_gate=network_boundaries:PASS")
        stage = "host_metrics_transport"
        operation = "verify_host_metrics_transport"
        with timer.timed_stage("host_metrics_transport"):
            _host_metrics_check()
        _mark(progress, "host_metrics_transport")
        print("system_vm_gate=host_metrics_transport:PASS source=host")
        stage = "persistence_fixture"
        operation = "create_and_verify_persistence_marker"
        with timer.timed_stage("persistence_fixture"):
            _guest_ssh(_guest_persistence_marker_command("create", marker, expected),
                       operation=operation)
        stage = "direct_egress_blocked"
        operation = "verify_direct_egress_is_blocked"
        with timer.timed_stage("direct_egress_blocked"):
            _guest_ssh("python3 -c 'import socket; s=socket.socket(); s.settimeout(2); "
                       "r=s.connect_ex((\"1.1.1.1\",443)); s.close(); assert r != 0'",
                       operation=operation)
        _mark(progress, "direct_egress_blocked")
        print("system_vm_gate=direct_egress_blocked:PASS")
        stage = "opencode_tui"
        operation = "exercise_opencode_tui"
        with timer.timed_stage("opencode_tui"):
            _tui_check(root)
        _mark(progress, "opencode_tui")
        print("system_vm_gate=opencode_tui:PASS")
        stage = "same_disk_persistence"
        with timer.timed_stage("clean_vm_shutdown"):
            operation = "stop_vm_cleanly_before_restart"
            run([str(root / "lab-vm"), "system", "down"], timeout=60, operation=operation)
            operation = "verify_clean_vm_shutdown"
            _require_normal_stop()
            operation = "verify_disk_identity_after_shutdown"
            if disk_identity() != before_disk:
                raise AcceptanceError("fixed disk identity changed during shutdown")
        with timer.timed_stage("same_disk_restart_readiness"):
            operation = "restart_vm_on_same_disk"
            run([str(root / "lab-vm"), "system", "up"], timeout=1005, operation=operation)
            operation = "wait_for_restarted_guest_readiness"
            restarted = _wait_active(UNIT, operation=operation)
            operation = "verify_disk_identity_after_restart"
            if disk_identity() != before_disk:
                raise AcceptanceError("fixed disk identity changed across restart")
            operation = "verify_persistence_marker_after_restart"
            _guest_ssh(_guest_persistence_marker_command("verify", marker, expected),
                       operation=operation)
            operation = "remove_persistence_marker"
            _guest_ssh(_guest_persistence_marker_command("remove", marker, expected),
                       operation=operation)
            operation = "verify_restarted_network_boundaries"
            with timer.timed_stage("network_boundaries"):
                _network_checks(restarted)
        _mark(progress, "same_disk_persistence")
        _mark(progress, "final_vm_state", final_vm_state=restarted,
              final_proxy_state=service_state(PROXY_UNIT),
              final_workspace_state=service_state(WORKSPACE_UNIT),
              final_metrics_state=service_state(METRICS_UNIT),
              disk_identity={"device": before_disk[0], "inode": before_disk[1]},
              restoration_outcome="NOT_REQUIRED_SUCCESS_VM_LEFT_RUNNING")
        print("system_vm_gate=same_disk_persistence:PASS")
        print("system_vm_gate=final_vm_state:PASS")
        print(f"system_vm_accept=PASS disk_device={before_disk[0]} disk_inode={before_disk[1]} final=RUNNING")
        print("system_vm_result=PASS")
        return 0
    except BaseException as failure:
        print("system_vm_result=FAIL")
        if isinstance(failure, AcceptanceError):
            reason = str(failure)
        elif isinstance(failure, OSError):
            reason = f"{type(failure).__name__}: {failure.strerror or 'operation failed'}"
        elif isinstance(failure, subprocess.TimeoutExpired):
            reason = f"command timed out after {failure.timeout}s; captured output omitted"
        else:
            reason = "exception detail omitted to avoid recording guest or terminal output"
        diagnostic = {
            "stage": stage,
            "operation": operation,
            "exception_type": type(failure).__name__,
            "cause_type": type(failure.__cause__).__name__ if failure.__cause__ else None,
            "reason": reason[:1600],
        }
        try:
            _mark(progress, "failure_diagnostic", failure_diagnostic=diagnostic)
        except BaseException:
            # Diagnostic persistence must never prevent lifecycle restoration.
            pass
        # Restore the initial lifecycle state on failure, including an already-running owner VM.
        try:
            restored = _restore_initial_state(root, vm_running)
        except BaseException as restore_error:
            observed = {}
            for unit in SYSTEM_UNITS:
                try:
                    observed[unit] = service_state(unit)
                except BaseException:
                    observed[unit] = {"ActiveState": "UNAVAILABLE", "MainPID": "UNAVAILABLE"}
            restore_reason = (str(restore_error) if isinstance(restore_error, AcceptanceError)
                              else f"{type(restore_error).__name__}; details omitted for safety")
            try:
                _mark(progress, "restoration", restoration_outcome="FAIL",
                      restoration_error=type(restore_error).__name__,
                      restoration_reason=restore_reason[:1600],
                      restoration_observed_state=observed)
            except BaseException:
                pass
            raise AcceptanceError(
                f"{stage}/{operation} failed ({type(failure).__name__}: {reason}); "
                f"initial running={vm_running} state could not be restored "
                f"({restore_reason[:500]})") from restore_error
        try:
            _mark(progress, "restoration", restoration_outcome="PASS",
                  restored_vm_state=restored[0], restored_proxy_state=restored[1])
        except BaseException:
            # State restoration succeeded; a report write failure cannot undo it.
            pass
        if isinstance(failure, (KeyboardInterrupt, SystemExit)):
            raise
        raise AcceptanceError(
            f"{stage}/{operation} failed ({type(failure).__name__}: {reason})") from failure
