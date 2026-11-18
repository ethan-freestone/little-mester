#!/usr/bin/env python3
"""Run the fixed production systemd VM unit only on disposable hosted KVM."""

from __future__ import annotations

import grp
import importlib.util
import os
import pathlib
import pwd
import re
import select
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid

REPO = pathlib.Path(__file__).resolve().parent.parent
CI_INSTALL_PREFIX = pathlib.Path("/srv/little-mester-ci")
CI_INSTALL_PARENT = CI_INSTALL_PREFIX / "v3"
UNIT = "little-mester-vm.service"
PROXY_UNIT = "little-mester-vm-proxy.service"
PINNED_INSTALL_ASSETS = {
    "/etc/systemd/system/little-mester-vm.service": ("systemd/little-mester-vm.service", 0o644),
    "/etc/systemd/system/little-mester-vm-proxy.service":
        ("systemd/little-mester-vm-proxy.service", 0o644),
    "/etc/sudoers.d/little-mester-vm": ("sudoers/little-mester-vm", 0o440),
}
PINNED_SOURCE_PATHS = frozenset(source for source, _mode in PINNED_INSTALL_ASSETS.values())
FIXED_FILES = (
    "/etc/systemd/system/little-mester-vm.service",
    "/etc/systemd/system/little-mester-vm-proxy.service",
    "/etc/systemd/system/little-mester-vm.service.d",
    "/etc/systemd/system/little-mester-vm-proxy.service.d",
    "/etc/systemd/system/multi-user.target.wants/little-mester-vm.service",
    "/etc/systemd/system/multi-user.target.wants/little-mester-vm-proxy.service",
    "/etc/sudoers.d/little-mester-vm",
    "/usr/libexec/little-mester-vm",
    "/etc/little-mester-vm",
    "/var/lib/little-mester-vm",
    "/run/little-mester-vm",
    "/run/lm-vm-proxy",
)
FIXED_USERS = ("lm-vm", "lm-vm-proxy")
FIXED_GROUPS = ("lm-vm", "lm-vm-proxy", "lm-vm-control")
PROTECTED_PREFLIGHT_PATH = "/etc/sudoers.d/little-mester-vm"
DIAGNOSTIC_CAPTURE_LIMIT = 256 * 1024
DIAGNOSTIC_OUTPUT_LIMIT = 24 * 1024
PROTECTED_PREFLIGHT_CHECK = (
    "import os, sys\n"
    "try:\n"
    "    os.lstat(sys.argv[1])\n"
    "except FileNotFoundError:\n"
    "    raise SystemExit(1)\n"
    "except OSError:\n"
    "    raise SystemExit(2)\n"
)
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")


def run(argv: list[str], *, timeout: int = 30, check: bool = True,
        capture: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        argv, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True, timeout=timeout, check=False,
    )
    if check and result.returncode:
        detail = (result.stderr or "").strip()[-1200:]
        raise RuntimeError(f"command failed ({result.returncode}): {argv[0]} {detail}")
    return result


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def hosted_kvm_guard() -> None:
    require(os.environ.get("GITHUB_ACTIONS") == "true", "not running in GitHub Actions")
    require(os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted",
            "runner is not GitHub-hosted; refusing system installation")
    require(os.environ.get("RUNNER_OS") == "Linux" and os.environ.get("RUNNER_ARCH") == "X64",
            "runner is not the ubuntu-24.04 Linux x64 hosted KVM job")
    require(pathlib.Path("/dev/kvm").is_char_device(), "hosted KVM device is unavailable")
    require(os.access("/dev/kvm", os.R_OK | os.W_OK), "hosted KVM device is not accessible")
    require(os.environ.get("GITHUB_WORKSPACE") == str(REPO),
            "checkout path differs from this script's repository")
    sha = os.environ.get("GITHUB_SHA", "")
    require(SHA_RE.fullmatch(sha) is not None, "GITHUB_SHA is not a full exact commit SHA")
    require(run(["git", "-C", str(REPO), "rev-parse", "HEAD"]).stdout.strip() == sha,
            "checkout does not equal workflow GITHUB_SHA")


def fixed_name_preflight(*, paths: tuple[str, ...] = FIXED_FILES,
                         users: tuple[str, ...] = FIXED_USERS,
                         groups: tuple[str, ...] = FIXED_GROUPS,
                         install_parent: pathlib.Path | None = None) -> None:
    """Fail closed on any existing fixed production identity or install target."""
    install_parent = CI_INSTALL_PARENT if install_parent is None else install_parent
    for raw in paths:
        path = pathlib.Path(raw)
        if raw == PROTECTED_PREFLIGHT_PATH:
            result = subprocess.run(
                ["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-c",
                 PROTECTED_PREFLIGHT_CHECK, PROTECTED_PREFLIGHT_PATH],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=5, check=False,
            )
            require(result.returncode in (0, 1),
                    f"unable to safely inspect fixed system VM path: {raw}")
            require(result.returncode == 1,
                    f"preserving pre-existing fixed system VM path: {raw}")
            continue
        require(not path.exists() and not path.is_symlink(),
                f"preserving pre-existing fixed system VM path: {raw}")
    for name in users:
        try:
            pwd.getpwnam(name)
        except KeyError:
            pass
        else:
            raise RuntimeError(f"preserving pre-existing fixed system VM user: {name}")
    for name in groups:
        try:
            grp.getgrnam(name)
        except KeyError:
            pass
        else:
            raise RuntimeError(f"preserving pre-existing fixed system VM group: {name}")
    install_path = install_parent / os.environ["GITHUB_SHA"]
    require(not install_path.exists() and not install_path.is_symlink(),
            f"preserving pre-existing pinned install: {install_path}")
    marker = start_marker_path()
    require(not marker.exists() and not marker.is_symlink(),
            f"preserving pre-existing production-unit start evidence: {marker}")


def start_marker_path() -> pathlib.Path:
    temp = pathlib.Path(os.environ["RUNNER_TEMP"])
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    require(temp.is_absolute() and temp.is_dir() and not temp.is_symlink()
            and os.lstat(temp).st_uid == os.getuid(),
            "runner temp path is not an owner-controlled directory")
    require(run_id.isdigit() and attempt.isdigit(), "GitHub run identity is unavailable")
    return temp / f"little-mester-system-unit-started-{run_id}-{attempt}"


def write_start_marker(sha: str) -> pathlib.Path:
    path = start_marker_path()
    payload = f"candidate_sha={sha}\n".encode("ascii")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(fd, remaining)
            require(written > 0, "production-unit start marker write made no progress")
            remaining = remaining[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


def has_valid_start_marker(sha: str) -> bool:
    path = start_marker_path()
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    require(stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
            and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1,
            "preserving unsafe production-unit start evidence")
    require(path.read_bytes() == f"candidate_sha={sha}\n".encode("ascii"),
            "preserving mismatched production-unit start evidence")
    return True


def service_state(unit: str) -> dict[str, str]:
    result = run(["/usr/bin/systemctl", "show", "--no-pager",
                  "--property=LoadState", "--property=ActiveState",
                  "--property=SubState", "--property=MainPID",
                  "--property=Result", "--property=TimeoutStopUSec", unit])
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def fixed_disk_identity() -> tuple[int, int]:
    """Read identity of only the fixed protected disk through privileged stat."""
    disk = "/var/lib/little-mester-vm/disk.qcow2"
    result = subprocess.run(
        ["/usr/bin/sudo", "-n", "/usr/bin/stat", "--format=%F:%d:%i", "--", disk],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=5, check=False,
    )
    require(result.returncode == 0, "unable to read fixed production disk identity")
    try:
        output = result.stdout.decode("ascii")
    except UnicodeDecodeError as error:
        raise RuntimeError("invalid fixed production disk identity") from error
    match = re.fullmatch(r"regular file:([0-9]+):([0-9]+)\n", output)
    require(match is not None, "fixed production disk is not a regular file")
    return int(match.group(1)), int(match.group(2))


def bounded_diagnostic_command(argv: list[str]) -> tuple[int, str, bool]:
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
    assert process.stdout is not None
    fd = process.stdout.fileno()
    os.set_blocking(fd, False)
    poller = select.poll()
    poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    deadline = time.monotonic() + 5
    output = bytearray()
    truncated = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                truncated = True
                process.kill()
                break
            events = poller.poll(max(1, min(100, int(remaining * 1000))))
            if events:
                chunk = os.read(fd, min(8192, DIAGNOSTIC_CAPTURE_LIMIT + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > DIAGNOSTIC_CAPTURE_LIMIT:
                    del output[DIAGNOSTIC_CAPTURE_LIMIT:]
                    truncated = True
                    process.kill()
                    break
            elif process.poll() is not None:
                break
        try:
            return_code = process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            return_code = process.wait(timeout=1)
    finally:
        process.stdout.close()
    return return_code, output.decode("utf-8", errors="replace"), truncated


def sanitize_diagnostic_output(value: str) -> str:
    redactions = (
        (re.compile(r"-----BEGIN [^\r\n]*PRIVATE KEY-----.*?-----END [^\r\n]*PRIVATE KEY-----",
                    re.DOTALL), "[REDACTED PRIVATE KEY BLOCK]"),
        (re.compile(r"-----BEGIN [^\r\n]*PRIVATE KEY-----.*\Z", re.DOTALL),
         "[REDACTED INCOMPLETE PRIVATE KEY BLOCK]"),
        (re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1[REDACTED]@"),
        (re.compile(r"(?im)\b(password|passwd|token|secret|authorization|api[_-]?key)"
                    r"\b(\s*[=:]\s*)(?:\".*?\"|'.*?'|[^\r\n,;]*)", re.DOTALL),
         r"\1\2[REDACTED]"),
        (re.compile(r"(?i)\bBearer\s+[^\s,;]+"), "Bearer [REDACTED]"),
        (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,})\b"),
         "[REDACTED TOKEN]"),
    )
    for pattern, replacement in redactions:
        value = pattern.sub(replacement, value)
    return value.encode("utf-8")[-DIAGNOSTIC_OUTPUT_LIMIT:].decode("utf-8", errors="ignore")


def report_startup_diagnostics() -> None:
    """Emit bounded, redacted systemd and QEMU serial evidence before cleanup."""
    properties = (
        "LoadState,ActiveState,SubState,Result,MainPID,ControlPID,ExecMainCode,"
        "ExecMainStatus,ExecMainStartTimestamp,ExecMainExitTimestamp,TimeoutStartUSec,"
        "TimeoutStopUSec,ExecStartPre,ExecStart,ExecStartPost,ExecStop"
    )
    commands = (
        ("unit-properties", ["/usr/bin/systemctl", "show", "--no-pager",
         f"--property={properties}", UNIT, PROXY_UNIT]),
        ("unit-status", ["/usr/bin/systemctl", "status", "--no-pager", "--full", UNIT, PROXY_UNIT]),
        # QEMU's serial console is attached to stdout and therefore lands in this journal.
        ("unit-and-guest-journal", ["/usr/bin/journalctl", "--no-pager", "-b",
         "-u", UNIT, "-u", PROXY_UNIT, "-n", "120", "-o", "short-iso"]),
    )
    print("production_unit_failure_diagnostics=BEGIN", flush=True)
    for label, argv in commands:
        try:
            return_code, output, truncated = bounded_diagnostic_command(argv)
            output = sanitize_diagnostic_output(output)
            suffix = " truncated=true" if truncated else ""
            print(f"--- {label} exit={return_code}{suffix} ---", flush=True)
            print(output or "[no output]", flush=True)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            print(f"--- {label} unavailable ({type(error).__name__}) ---", flush=True)
    print("production_unit_failure_diagnostics=END", flush=True)


def require_fixed_units_loaded() -> None:
    states = {unit: service_state(unit) for unit in (UNIT, PROXY_UNIT)}
    require(all(state.get("LoadState") == "loaded" for state in states.values()),
            f"installed production systemd units are not both loaded: {states}")


def assert_stopped(*, vm_result: str | None = None) -> None:
    deadline = time.monotonic() + 30
    states: dict[str, dict[str, str]] = {}
    while True:
        states = {unit: service_state(unit) for unit in (UNIT, PROXY_UNIT)}
        stopped = all(state.get("ActiveState") in ("inactive", "failed")
                      and state.get("MainPID") == "0" for state in states.values())
        if stopped:
            break
        require(time.monotonic() < deadline,
                f"final service cleanup did not stop both units within 30s: {states}")
        time.sleep(0.1)
    if vm_result is not None:
        require(states[UNIT].get("Result") == vm_result,
                f"production VM unit result differs from {vm_result}: {states[UNIT]}")
    print("production_unit_cleanup=PASS")


def invoke_lab(root: pathlib.Path, operation: str, timeout: int, *, check: bool = True):
    # The installer adds the runner to lm-vm-control. sg supplies that group to
    # this fresh process; it does not alter the runner account's persisted groups.
    return run(["/usr/bin/sg", "lm-vm-control", "-c",
                f"{root}/lab-vm system {operation}"], timeout=timeout,
               capture=False, check=check)


def invoke_verified_cleanup(sha: str) -> None:
    args = [str(REPO / "tools/vm-owner-acceptance"), "--system-vm", "down",
            "--sha", sha, "--repo", str(REPO), "--prefix", str(CI_INSTALL_PREFIX)]
    command = " ".join(shlex.quote(value) for value in args)
    run(["/usr/bin/sg", "lm-vm-control", "-c", command], timeout=90, capture=False)


def installed_asset_bytes(destination: str) -> bytes:
    path = pathlib.Path(destination)
    if destination == "/etc/sudoers.d/little-mester-vm":
        result = subprocess.run(["/usr/bin/sudo", "-n", "/usr/bin/cat", "--", destination],
                                stdin=subprocess.DEVNULL, capture_output=True, check=False)
        require(result.returncode == 0,
                f"command failed ({result.returncode}): /usr/bin/sudo /usr/bin/cat")
        return result.stdout
    return path.read_bytes()


def installed_asset_metadata(destination: str) -> tuple[int, int]:
    """Return mode and owner for the protected sudoers file via fixed sudo stat."""
    if destination != "/etc/sudoers.d/little-mester-vm":
        info = os.lstat(destination)
        return info.st_mode, info.st_uid
    result = subprocess.run(
        ["/usr/bin/sudo", "-n", "/usr/bin/stat", "--format=%f:%u", "--", destination],
        stdin=subprocess.DEVNULL, capture_output=True, check=False,
    )
    require(result.returncode == 0,
            f"command failed ({result.returncode}): /usr/bin/sudo /usr/bin/stat")
    try:
        output = result.stdout.decode("ascii")
    except UnicodeDecodeError as error:
        raise RuntimeError("invalid privileged installed asset metadata") from error
    match = re.fullmatch(r"([0-9a-fA-F]{1,8}):([0-9]+)\n", output)
    require(match is not None, "invalid privileged installed asset metadata")
    return int(match.group(1), 16), int(match.group(2), 10)


def git_object_bytes(sha: str, source: str) -> bytes:
    require(SHA_RE.fullmatch(sha) is not None, "GITHUB_SHA is not a full exact commit SHA")
    require(source in PINNED_SOURCE_PATHS, f"untrusted pinned source path: {source}")
    result = subprocess.run(["/usr/bin/git", "-C", str(REPO), "show", f"{sha}:{source}"],
                            stdin=subprocess.DEVNULL, capture_output=True, check=False)
    require(result.returncode == 0, f"unable to read pinned Git object path: {source}")
    return result.stdout


def verify_installed_files() -> None:
    sha = os.environ.get("GITHUB_SHA", "")
    require(SHA_RE.fullmatch(sha) is not None, "GITHUB_SHA is not a full exact commit SHA")
    for dest, (source, mode) in PINNED_INSTALL_ASSETS.items():
        installed_mode, installed_uid = installed_asset_metadata(dest)
        require(stat.S_ISREG(installed_mode) and installed_uid == 0
                and stat.S_IMODE(installed_mode) == mode,
                f"installed production asset metadata mismatch: {dest}")
        installed = installed_asset_bytes(dest)
        require(installed == git_object_bytes(sha, source),
                f"installed production asset bytes differ from exact candidate: {dest}")
    run(["/usr/bin/sudo", "-n", "/usr/sbin/visudo", "-cf",
         "/etc/sudoers.d/little-mester-vm"])
    print("production_unit_install=PASS pinned_bytes=true sudoers=PASS")


def production_host_preflight() -> None:
    """Require the system manager and noninteractive sudo before any install."""
    systemctl = "/usr/bin/systemctl"
    sudo = "/usr/bin/sudo"
    manager_ok = False
    if not pathlib.Path(systemctl).is_file():
        print("production_preflight_system_manager=FAIL scope=system reason=missing-tool")
    else:
        try:
            manager = subprocess.run([systemctl, "show", "--property=Version", "--value"],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, timeout=3, check=False)
        except subprocess.TimeoutExpired:
            print("production_preflight_system_manager=FAIL scope=system reason=timeout")
        except OSError as error:
            print("production_preflight_system_manager=FAIL scope=system "
                  f"reason={type(error).__name__}")
        else:
            version = manager.stdout.strip()
            manager_ok = manager.returncode == 0 and bool(version)
            if manager_ok:
                print("production_preflight_system_manager=PASS scope=system "
                      f"responsive=true version={version} boot_state=not_a_gate")
            else:
                print("production_preflight_system_manager=FAIL scope=system "
                      f"reason={'empty-version' if manager.returncode == 0 else 'command-failed'} "
                      f"exit={manager.returncode}")

    sudo_ok = False
    if not pathlib.Path(sudo).is_file():
        print("production_preflight_sudo=FAIL mode=noninteractive reason=missing-tool")
    else:
        try:
            sudo_result = subprocess.run([sudo, "-n", "true"], stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         timeout=2, check=False)
        except subprocess.TimeoutExpired:
            print("production_preflight_sudo=FAIL mode=noninteractive reason=timeout")
        except OSError as error:
            print("production_preflight_sudo=FAIL mode=noninteractive "
                  f"reason={type(error).__name__}")
        else:
            sudo_ok = sudo_result.returncode == 0
            print(f"production_preflight_sudo={'PASS' if sudo_ok else 'FAIL'} "
                  f"mode=noninteractive exit={sudo_result.returncode}")

    require(manager_ok, "production preflight system manager unavailable (scope=system)")
    require(sudo_ok, "production preflight noninteractive sudo unavailable")


def guest_command(command: str, *, timeout: int = 30) -> None:
    key_result = run(["/usr/bin/sudo", "-n", "/usr/bin/cat",
                      "/etc/little-mester-vm/control_ed25519"])
    known_hosts = read_known_hosts()
    temp_root = pathlib.Path(os.environ["RUNNER_TEMP"])
    with tempfile.TemporaryDirectory(prefix="lm-system-ssh-", dir=temp_root) as directory:
        key_path = pathlib.Path(directory) / "identity"
        key_path.write_text(key_result.stdout, encoding="ascii")
        key_path.chmod(0o600)
        hosts_path = pathlib.Path(directory) / "known_hosts"
        hosts_path.write_bytes(known_hosts)
        hosts_path.chmod(0o600)
        run([
            "/usr/bin/ssh", "-F", "/dev/null", "-p", "22222", "-i", str(key_path),
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={hosts_path}",
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ProxyCommand=none",
            "-o", "ProxyJump=none", "-o", "ConnectTimeout=5", "-o", "ClearAllForwardings=yes",
            "mester@127.0.0.1", f"/bin/bash -lc {shlex.quote(command)}",
        ], timeout=timeout)


def read_known_hosts() -> bytes:
    """Read only the fixed, group-protected known_hosts path through noninteractive sudo."""
    result = subprocess.run(
        ["/usr/bin/sudo", "-n", "/usr/bin/cat", "--",
         "/etc/little-mester-vm/known_hosts"],
        stdin=subprocess.DEVNULL, capture_output=True, check=False,
    )
    require(result.returncode == 0,
            f"command failed ({result.returncode}): /usr/bin/sudo /usr/bin/cat known_hosts")
    return result.stdout


def unit_identity_checks() -> None:
    checks = (
        ("lm-vm", "lm-vm"), ("lm-vm-proxy", "lm-vm-proxy"),
        ("lm-vm-control", "lm-vm-control"),
    )
    for user, group in checks[:2]:
        account = pwd.getpwnam(user)
        require(1 <= account.pw_uid < 1000 and account.pw_gid == grp.getgrnam(group).gr_gid
                and account.pw_shell == "/usr/sbin/nologin",
                f"fixed service account policy mismatch: {user}")
    require(grp.getgrnam(checks[2][1]).gr_gid < 1000,
            "control group is not a system group")
    data = pathlib.Path("/var/lib/little-mester-vm")
    config = pathlib.Path("/etc/little-mester-vm")
    require(stat.S_IMODE(os.lstat(data).st_mode) == 0o750
            and os.lstat(data).st_uid == 0
            and os.lstat(data).st_gid == grp.getgrnam("lm-vm").gr_gid,
            "fixed VM state directory ownership/mode mismatch")
    require(stat.S_IMODE(os.lstat(config).st_mode) == 0o750
            and os.lstat(config).st_uid == 0
            and os.lstat(config).st_gid == grp.getgrnam("lm-vm-control").gr_gid,
            "fixed VM config directory ownership/mode mismatch")
    print("production_unit_identity=PASS")


def ssh_canary(marker: str, expected: str) -> None:
    remote_path = f"/home/mester/workspace/.lm-system-ci-{marker}"
    command = ("set -eu; test -d /home/mester/workspace; "
               f"test \"$(cat {remote_path})\" = '{expected}'")
    guest_command(command)


def check_direct_egress_blocked() -> None:
    # TEST-NET-1 is reserved and cannot be a legitimate public dependency. The
    # probe makes no HTTP request and uses only the already-pinned guest SSH path.
    remote = ("python3 -c 'import socket; s=socket.socket(); s.settimeout(2); "
              "result=s.connect_ex((\"192.0.2.1\",443)); s.close(); "
              "assert result != 0, \"reserved direct target connected\"'")
    guest_command(remote, timeout=15)
    print("production_unit_direct_egress=PASS target=192.0.2.1:443 connected=false")


def check_proxy_private_target_policy() -> None:
    run([sys.executable, str(REPO / "tests/vm-proxy.test.py")], timeout=30)
    print("production_proxy_private_target_policy=PASS existing_unit_tests=true")


class _BoundedRawCapture:
    def __init__(self, stream, limit: int = 256 * 1024):
        self.stream = stream
        self.limit = limit
        self.retained = 0

    def write(self, data: bytes) -> None:
        remaining = self.limit - self.retained
        if remaining > 0:
            chunk = data[:remaining]
            self.stream.write(chunk)
            self.retained += len(chunk)

    def flush(self) -> None:
        self.stream.flush()


def production_tui_probe(root: pathlib.Path) -> None:
    """Reach the real no-auth prompt through the fixed production owner route."""
    try:
        import pexpect
    except ImportError as error:
        raise RuntimeError("python3-pexpect is required for the production TUI probe") from error

    diagnostic_path = REPO / "tests/ci-tui-diagnostic.py"
    spec = importlib.util.spec_from_file_location("production_tui_diagnostic", diagnostic_path)
    require(spec is not None and spec.loader is not None, "TUI diagnostic helper is unavailable")
    diagnostic = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diagnostic)
    fd, raw_name = tempfile.mkstemp(prefix="lm-system-tui-", suffix=".raw",
                                    dir=os.environ["RUNNER_TEMP"])
    os.fchmod(fd, 0o600)
    raw_path = pathlib.Path(raw_name)
    child = None
    deadline = time.monotonic() + 110
    try:
        with os.fdopen(fd, "wb") as raw_stream:
            child_env = os.environ.copy()
            child_env.update(TERM="xterm-256color", LM_VM_TERM_VALIDATED="1")
            child = pexpect.spawn(
                "/usr/bin/sg", ["lm-vm-control", "-c", f"{root}/lab-vm system tui"],
                env=child_env, encoding=None, dimensions=(32, 100), timeout=30,
                maxread=4096,
            )
            child.logfile_read = _BoundedRawCapture(raw_stream)
            child.expect(b"Ask anything", timeout=max(1, min(90, deadline - time.monotonic())))
            child.sendcontrol("c")
            child.expect(b"Close Little Mester?",
                         timeout=max(1, min(10, deadline - time.monotonic())))
            child.send(b"q")
            child.expect(pexpect.EOF, timeout=max(1, min(20, deadline - time.monotonic())))
            child.close()
            require(child.exitstatus == 0 and child.signalstatus is None,
                    "production TUI did not close cleanly: "
                    f"exit={child.exitstatus} signal={child.signalstatus}")
            raw_stream.flush()
            os.fsync(raw_stream.fileno())
        raw = diagnostic.read_private(raw_path)
        require(diagnostic.classify(raw) == "tui-prompt-reached",
                "bounded production TUI capture did not contain the expected no-auth prompt")
        print("production_unit_tui=PASS route=lab-vm-system-tui prompt=Ask-anything close=ctrl-c,q")
    except BaseException as error:
        if child is not None and child.isalive():
            child.close(force=True)
        print(f"production_unit_tui=FAIL error={type(error).__name__} private_raw={raw_path}",
              file=sys.stderr)
        raise


def check_network_basics() -> None:
    state = service_state(UNIT)
    require(state.get("ActiveState") == "active" and int(state.get("MainPID", "0")) > 1,
            f"production VM unit is not active with a MainPID: {state}")
    pid = state["MainPID"]
    argv = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    require("restrict=on" in argv and "hostfwd=tcp:127.0.0.1:22222-:22" in argv,
            "production QEMU lacks restricted user networking or loopback-only SSH forwarding")
    proxy_state = service_state(PROXY_UNIT)
    require(proxy_state.get("ActiveState") == "active" and int(proxy_state.get("MainPID", "0")) > 1,
            f"production egress proxy is not active: {proxy_state}")
    with socket.create_connection(("127.0.0.1", 32128), timeout=2):
        pass
    listeners = run(["/usr/bin/ss", "-H", "-ltn"]).stdout
    require("127.0.0.1:32128" in listeners and "0.0.0.0:32128" not in listeners
            and "[::]:32128" not in listeners,
            "proxy listener is not loopback-only")
    print("production_unit_network=PASS qemu_restrict=on ssh_forward=127.0.0.1 proxy=loopback")


def force_systemd_fallback(root: pathlib.Path) -> None:
    """Freeze only this unit's verified QEMU MainPID and prove systemd kills it."""
    state = service_state(UNIT)
    require(state.get("ActiveState") == "active", "forced-stop case requires the active fixed unit")
    pid = int(state.get("MainPID", "0"))
    require(pid > 1, "fixed systemd unit has no QEMU MainPID for fallback test")
    cmdline_path = pathlib.Path(f"/proc/{pid}/cmdline")
    argv = cmdline_path.read_bytes().replace(b"\0", b" ").decode()
    require("qemu-system-x86_64" in argv and "little-mester-system-vm" in argv
            and "-enable-kvm" in argv,
            "fixed unit MainPID is not the expected production QEMU process")
    status = pathlib.Path(f"/proc/{pid}/status").read_text(encoding="ascii")
    uid_line = next((line for line in status.splitlines() if line.startswith("Uid:")), "")
    require(uid_line.split()[1:2] == [str(pwd.getpwnam("lm-vm").pw_uid)],
            "fixed unit MainPID is not owned by lm-vm")
    unit_text = pathlib.Path("/etc/systemd/system/little-mester-vm.service").read_text()
    for directive in (
        "ExecStop=/usr/bin/python3 -I /usr/libexec/little-mester-vm/system-vm.py qmp-stop $MAINPID",
        "TimeoutStopSec=25s", "KillMode=control-group", "SendSIGKILL=yes",
    ):
        require(directive in unit_text, f"production systemd fallback directive is absent: {directive}")
    require(state.get("TimeoutStopUSec") == "25s",
            f"loaded systemd stop timeout differs from fixed 25s policy: {state}")
    pidfd = os.pidfd_open(pid, 0)
    try:
        run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "kill", "--signal=SIGSTOP",
             "--kill-whom=main", UNIT], timeout=5)
        print(f"production_unit_forced_stop_started=pidfd_bound_mainpid={pid} signal=SIGSTOP")
        down_result = invoke_lab(root, "down", 60, check=False)
        stopped = service_state(UNIT)
        require(stopped.get("ActiveState") == "inactive" and stopped.get("MainPID") == "0",
                f"systemd did not finish forced fallback stop: {stopped}")
        require(stopped.get("Result") == "timeout",
                f"systemd did not report the bounded stop-timeout fallback: {stopped}")
        poller = select.poll()
        poller.register(pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
        require(bool(poller.poll(1000)), "pidfd did not report exit of the exact frozen QEMU MainPID")
        require(down_result.returncode in (0, 1),
                f"fixed system down returned an unexpected status after proven fallback: {down_result.returncode}")
        print("production_unit_forced_stop=PASS result=timeout timeout_stop=25s "
              "killmode=control-group send_sigkill=yes pidfd_exit=true")
    finally:
        os.close(pidfd)


def cleanup_only() -> int:
    """Workflow-level finally path for cancellation/timeout of the main stage."""
    hosted_kvm_guard()
    sha = os.environ["GITHUB_SHA"]
    if not has_valid_start_marker(sha):
        print("production_unit_cleanup=NOT_APPLICABLE production_start_not_attempted=true")
        return 0
    root = CI_INSTALL_PARENT / sha
    if not root.exists() and not root.is_symlink():
        print("production_unit_cleanup=NOT_APPLICABLE pinned_install_absent=true")
        return 0
    info = os.lstat(root)
    require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
            and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o755,
            "preserving unsafe pinned candidate during cleanup")
    unit_paths = (pathlib.Path(FIXED_FILES[0]), pathlib.Path(FIXED_FILES[1]))
    present = [path.exists() or path.is_symlink() for path in unit_paths]
    if not any(present):
        print("production_unit_cleanup=NOT_APPLICABLE production_unit_absent=true")
        return 0
    require(all(present), "partial fixed unit installation; preserving it without lifecycle action")
    verify_installed_files()
    states = [service_state(UNIT), service_state(PROXY_UNIT)]
    loaded = [state.get("LoadState") == "loaded" for state in states]
    if not any(loaded):
        print("production_unit_cleanup=NOT_APPLICABLE systemd_units_not_loaded=true")
        return 0
    require(all(loaded), "only one fixed system VM unit is loaded; preserving ambiguous state")
    invoke_verified_cleanup(sha)
    assert_stopped()
    print("production_unit_cleanup_scope=production_systemd_unit")
    return 0


def main() -> int:
    if sys.argv[1:] == ["--cleanup"]:
        try:
            return cleanup_only()
        except BaseException as error:  # noqa: BLE001 - always report cleanup failure, including cancellation
            print(f"production_unit_cleanup=FAIL error={type(error).__name__}: {error}", file=sys.stderr)
            return 1
    require(not sys.argv[1:], "unsupported argument")
    installed_root: pathlib.Path | None = None
    start_attempted = False
    failure: BaseException | None = None
    try:
        hosted_kvm_guard()
        production_host_preflight()
        fixed_name_preflight(install_parent=CI_INSTALL_PARENT)
        sha = os.environ["GITHUB_SHA"]
        print(f"evidence_scope=production_systemd_unit candidate_sha={sha} runner=ubuntu-24.04-kvm")
        # owner-acceptance's private report root requires the standard per-user
        # runtime directory. Create only this ephemeral runner-owned directory
        # when absent; reject any existing path that is not the exact safe form.
        runtime = pathlib.Path(f"/run/user/{os.getuid()}")
        if not runtime.exists():
            run(["/usr/bin/sudo", "-n", "/usr/bin/install", "-d", "-m", "0700",
                 "-o", str(os.getuid()), "-g", str(os.getgid()), str(runtime)])
        runtime_info = os.lstat(runtime)
        require(stat.S_ISDIR(runtime_info.st_mode) and runtime_info.st_uid == os.getuid()
                and stat.S_IMODE(runtime_info.st_mode) == 0o700,
                "runner private runtime path is unsafe")
        run([str(REPO / "tools/vm-owner-acceptance"), "--install", "--sha", sha,
             "--prefix", str(CI_INSTALL_PREFIX), "--repo", str(REPO)], timeout=120)
        installed_root = CI_INSTALL_PARENT / sha
        run(["/usr/bin/sudo", "-n", str(installed_root / "tools/vm-system-install")], timeout=300)
        verify_installed_files()
        require_fixed_units_loaded()
        unit_identity_checks()
        marker = uuid.uuid4().hex
        expected = f"systemd-production-canary-{marker}"
        started_at = time.monotonic()
        write_start_marker(sha)
        start_attempted = True
        invoke_lab(installed_root, "up", 16 * 60 + 45)
        first = service_state(UNIT)
        require(first.get("ActiveState") == "active" and int(first.get("MainPID", "0")) > 1,
                f"systemd unit failed readiness/start: {first}")
        invoke_lab(installed_root, "up", 60)
        duplicate = service_state(UNIT)
        require(duplicate.get("ActiveState") == "active"
                and duplicate.get("MainPID") == first.get("MainPID"),
                "duplicate production start replaced the active QEMU MainPID")
        check_network_basics()
        check_direct_egress_blocked()
        check_proxy_private_target_policy()
        status = run([str(installed_root / "lab-vm"), "system", "status"], timeout=20)
        require("active" in status.stdout.lower() and first["MainPID"] in status.stdout,
                "fixed owner status operation did not report the active production MainPID")
        # Create a uniquely named guest workspace marker with the existing SSH
        # identity, then prove the same disk retains it over the real unit stop.
        key = run(["/usr/bin/sudo", "-n", "/usr/bin/cat",
                   "/etc/little-mester-vm/control_ed25519"]).stdout
        known = read_known_hosts()
        with tempfile.TemporaryDirectory(prefix="lm-system-write-", dir=os.environ["RUNNER_TEMP"]) as d:
            key_file, hosts_file = pathlib.Path(d) / "identity", pathlib.Path(d) / "known_hosts"
            key_file.write_text(key, encoding="ascii"); key_file.chmod(0o600)
            hosts_file.write_bytes(known); hosts_file.chmod(0o600)
            remote = f"/home/mester/workspace/.lm-system-ci-{marker}"
            write = f"umask 077; printf '%s' '{expected}' > {remote}; test \"$(cat {remote})\" = '{expected}'"
            run(["/usr/bin/ssh", "-F", "/dev/null", "-p", "22222", "-i", str(key_file),
                 "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                 "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={hosts_file}",
                 "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ProxyCommand=none",
                 "-o", "ProxyJump=none", "-o", "ConnectTimeout=5", "-o", "ClearAllForwardings=yes",
                 "mester@127.0.0.1", f"/bin/bash -lc {shlex.quote(write)}"], timeout=30)
        ssh_canary(marker, expected)
        print("production_unit_canary=PASS before_stop=true")
        production_tui_probe(installed_root)
        before_disk = fixed_disk_identity()
        invoke_lab(installed_root, "down", 45)
        assert_stopped(vm_result="success")
        print("production_unit_normal_stop=PASS qmp_execstop=true graceful=true")
        stopped_disk = fixed_disk_identity()
        require(before_disk == stopped_disk,
                "fixed production disk identity changed across stop")
        invoke_lab(installed_root, "up", 16 * 60 + 45)
        after = service_state(UNIT)
        require(after.get("ActiveState") == "active" and int(after.get("MainPID", "0")) > 1,
                f"production VM did not become ready after same-disk restart: {after}")
        ssh_canary(marker, expected)
        after_disk = fixed_disk_identity()
        require(before_disk == after_disk,
                "production restart did not use the same fresh disk inode")
        print(f"production_unit_restart=PASS same_disk=true elapsed_seconds={int(time.monotonic()-started_at)}")
        force_systemd_fallback(installed_root)
    except BaseException as error:  # noqa: BLE001 - finally below must stop a started unit on cancellation
        failure = error
        print(f"production_unit_result=FAIL error={type(error).__name__}: {error}", file=sys.stderr)
        if start_attempted:
            report_startup_diagnostics()
    finally:
        if start_attempted and installed_root is not None:
            try:
                invoke_lab(installed_root, "down", 45)
                assert_stopped()
            except BaseException as error:  # noqa: BLE001 - preserve both primary and cleanup failures
                print(f"production_unit_cleanup=FAIL error={type(error).__name__}: {error}", file=sys.stderr)
                report_startup_diagnostics()
                failure = failure or error
    if failure:
        return 1
    print("production_unit_result=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
