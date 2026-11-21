#!/usr/bin/env python3
"""Run the fixed production systemd VM unit only on disposable hosted KVM."""

from __future__ import annotations

import grp
import json
import os
import pathlib
import pwd
import re
import secrets
import select
import shlex
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import traceback

REPO = pathlib.Path(__file__).resolve().parent.parent
CI_INSTALL_PREFIX = pathlib.Path("/srv/little-mester-ci")
CI_INSTALL_PARENT = CI_INSTALL_PREFIX / "v3"
UNIT = "little-mester-vm.service"
PROXY_UNIT = "little-mester-vm-proxy.service"
WORKSPACE_UNIT = "little-mester-vm-workspace.service"
METRICS_UNIT = "little-mester-vm-metrics.service"
PINNED_INSTALL_ASSETS = {
    "/usr/lib/little-mester-vm/system-vm.py": ("lib/vm/system-vm.py", 0o644),
    "/usr/lib/little-mester-vm/system-vm-workspace.py":
        ("lib/vm/system-vm-workspace.py", 0o644),
    "/usr/lib/little-mester-vm/parent-qemu.py": ("lib/vm/parent-qemu.py", 0o644),
    "/usr/lib/little-mester-vm/guest-bootstrap.py": ("lib/vm/guest-bootstrap.py", 0o644),
    "/usr/lib/little-mester-vm/guest-opencode-preferences.py":
        ("lib/vm/guest-opencode-preferences.py", 0o644),
    "/etc/systemd/system/little-mester-vm.service": ("systemd/little-mester-vm.service", 0o644),
    "/etc/systemd/system/little-mester-vm-proxy.service":
        ("systemd/little-mester-vm-proxy.service", 0o644),
    "/etc/systemd/system/little-mester-vm-workspace.service":
        ("systemd/little-mester-vm-workspace.service", 0o644),
    "/etc/systemd/system/little-mester-vm-metrics.service":
        ("systemd/little-mester-vm-metrics.service", 0o644),
    "/usr/lib/little-mester-vm/system-vm-metrics.py":
        ("lib/vm/system-vm-metrics.py", 0o644),
    "/usr/lib/little-mester-vm/parent-metrics.py": ("lib/vm/parent-metrics.py", 0o644),
    "/usr/lib/little-mester-vm/parent-metrics-collect.py":
        ("lib/vm/parent-metrics-collect.py", 0o644),
    "/usr/lib/little-mester-vm/parent-metrics.py": ("lib/vm/parent-metrics.py", 0o644),
    "/usr/lib/little-mester-vm/parent-metrics-collect.py":
        ("lib/vm/parent-metrics-collect.py", 0o644),
    "/usr/lib/little-mester-vm/host-metrics-tui/package.json":
        ("agent/opencode/plugins/host-metrics-tui/package.json", 0o644),
    "/usr/lib/little-mester-vm/host-metrics-tui/tui.tsx":
        ("agent/opencode/plugins/host-metrics-tui/tui.tsx", 0o644),
    "/etc/sudoers.d/little-mester-vm": ("sudoers/little-mester-vm", 0o440),
}
PINNED_SOURCE_PATHS = frozenset(source for source, _mode in PINNED_INSTALL_ASSETS.values())
FIXED_FILES = (
    "/etc/systemd/system/little-mester-vm.service",
    "/etc/systemd/system/little-mester-vm-proxy.service",
    "/etc/systemd/system/little-mester-vm-workspace.service",
    "/etc/systemd/system/little-mester-vm-metrics.service",
    "/etc/systemd/system/little-mester-vm.service.d",
    "/etc/systemd/system/little-mester-vm-proxy.service.d",
    "/etc/systemd/system/little-mester-vm-workspace.service.d",
    "/etc/systemd/system/multi-user.target.wants/little-mester-vm.service",
    "/etc/systemd/system/multi-user.target.wants/little-mester-vm-proxy.service",
    "/etc/sudoers.d/little-mester-vm",
    "/usr/lib/little-mester-vm",
    "/usr/lib/little-mester-vm/host-metrics-tui",
    "/etc/little-mester-vm",
    "/var/lib/little-mester-vm",
    "/run/little-mester-vm",
    "/run/lm-vm-proxy",
    "/run/little-mester-vm-workspace",
    "/run/little-mester-vm-workspace-share",
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
         f"--property={properties}", UNIT, PROXY_UNIT, WORKSPACE_UNIT, METRICS_UNIT]),
        ("unit-status", ["/usr/bin/systemctl", "status", "--no-pager", "--full",
         UNIT, PROXY_UNIT, WORKSPACE_UNIT, METRICS_UNIT]),
        # QEMU's serial console is attached to stdout and therefore lands in this journal.
        ("unit-and-guest-journal", ["/usr/bin/journalctl", "--no-pager", "-b",
          "-u", UNIT, "-u", PROXY_UNIT, "-u", WORKSPACE_UNIT, "-u", METRICS_UNIT,
          "-n", "120", "-o", "short-iso"]),
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
    states = {unit: service_state(unit) for unit in
              (UNIT, PROXY_UNIT, WORKSPACE_UNIT, METRICS_UNIT)}
    require(all(state.get("LoadState") == "loaded" for state in states.values()),
            f"installed production systemd units are not all loaded: {states}")


def assert_stopped(*, vm_result: str | None = None) -> None:
    deadline = time.monotonic() + 30
    states: dict[str, dict[str, str]] = {}
    while True:
        states = {unit: service_state(unit) for unit in
                  (UNIT, PROXY_UNIT, WORKSPACE_UNIT, METRICS_UNIT)}
        stopped = all(state.get("ActiveState") in ("inactive", "failed")
                      and state.get("MainPID") == "0" for state in states.values())
        if stopped:
            break
        require(time.monotonic() < deadline,
                f"final service cleanup did not stop all fixed units within 30s: {states}")
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


def invoke_shared_acceptance(sha: str) -> None:
    """Run the exact installed owner-facing acceptance implementation."""
    installed = CI_INSTALL_PARENT / sha / "tools/vm-owner-acceptance"
    args = [str(installed), "--system-vm", "accept", "--approve-system-vm-lifecycle",
            "--sha", sha, "--repo", str(REPO), "--prefix", str(CI_INSTALL_PREFIX)]
    command = " ".join(shlex.quote(value) for value in args)
    run(["/usr/bin/sg", "lm-vm-control", "-c", command], timeout=45 * 60, capture=False)


def verify_workspace_startup_post() -> None:
    """Require the actual fixed workspace ExecStartPost to finish successfully."""
    result = run(["/usr/bin/systemctl", "show", "--no-pager",
                  "--property=ActiveState", "--property=SubState",
                  "--property=ExecStartPost", WORKSPACE_UNIT])
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    require(properties.get("ActiveState") == "active" and properties.get("SubState") == "running",
            "production workspace service is not active after VM acceptance")
    post = properties.get("ExecStartPost", "")
    require("socket-permissions" in post and "code=exited" in post
            and re.search(r"\bstatus=0(?:/|\s|;|})", post) is not None,
            "production workspace ExecStartPost did not complete socket readiness successfully")
    print("production_workspace_exec_start_post=PASS socket_metadata=owner-normalized")


def disposable_workspace_path() -> pathlib.Path:
    temp = pathlib.Path(os.environ["RUNNER_TEMP"])
    run_id, attempt = os.environ.get("GITHUB_RUN_ID", ""), os.environ.get("GITHUB_RUN_ATTEMPT", "")
    require(temp.is_absolute() and temp.is_dir() and not temp.is_symlink()
            and os.lstat(temp).st_uid == os.getuid(), "runner temporary directory is unsafe")
    require(run_id.isdigit() and attempt.isdigit(), "GitHub run identity is unavailable")
    return temp / f"little-mester-system-workspace-{run_id}-{attempt}"


def configure_disposable_workspace(installed_root: pathlib.Path) -> pathlib.Path:
    workspace = disposable_workspace_path()
    require(not workspace.exists() and not workspace.is_symlink(),
            f"preserving pre-existing disposable workspace evidence: {workspace}")
    workspace.mkdir(mode=0o700)
    payload = json.dumps({"dir": str(workspace), "repository": str(REPO),
                          "owner_uid": os.getuid(), "unsafe_recurse": False,
                          "reassign": False})
    command = shlex.join(["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I",
                          str(installed_root / "lib/vm/system-vm-workspace.py"), "configure"])
    result = subprocess.run(["/usr/bin/sg", "lm-vm-control", "-c", command], input=payload,
                            text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=30, check=False)
    require(result.returncode == 0,
            f"disposable workspace configurator failed safely (exit={result.returncode})")
    dropin = pathlib.Path("/etc/systemd/system/little-mester-vm-workspace.service.d/20-workspace.conf")
    expected_bind = f'[Service]\nBindPaths="{workspace}:/run/little-mester-vm-workspace-share"\n'
    require(dropin.read_text(encoding="utf-8") == expected_bind,
            "fresh production unit assignment did not render the explicit disposable host path to its runtime target")
    return workspace


def _host_path_evidence(path: pathlib.Path) -> dict:
    """Capture metadata only; never read directory contents or file payloads."""
    result = {"path": str(path), "canonical": os.path.realpath(path)}
    try:
        info = os.lstat(path)
        result["lstat"] = {
            "kind": "symlink" if stat.S_ISLNK(info.st_mode) else
                    "directory" if stat.S_ISDIR(info.st_mode) else "other",
            "device": info.st_dev, "inode": info.st_ino,
            "uid": info.st_uid, "gid": info.st_gid,
            "mode": stat.S_IMODE(info.st_mode),
        }
    except OSError as error:
        result["lstat_error"] = error.errno
    try:
        parent = os.stat(path.parent, follow_symlinks=False)
        result["parent"] = {"state": "present", "device": parent.st_dev,
                            "inode": parent.st_ino, "uid": parent.st_uid,
                            "gid": parent.st_gid, "mode": stat.S_IMODE(parent.st_mode)}
    except OSError as error:
        result["parent"] = {"state": "unavailable", "errno": error.errno}
    try:
        mounts = pathlib.Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
        match = None
        for line in mounts:
            fields = line.split()
            if len(fields) < 10 or "-" not in fields:
                continue
            mountpoint = fields[4]
            for sequence, character in (("\\040", " "), ("\\011", "\t"),
                                        ("\\012", "\n"), ("\\134", "\\")):
                mountpoint = mountpoint.replace(sequence, character)
            point = pathlib.Path(mountpoint)
            if ((path == point or point in path.parents)
                    and (match is None or len(point.parts) > match[0])):
                match = (len(point.parts), {"mount_id": int(fields[0]),
                         "mountpoint": str(point), "filesystem": fields[fields.index("-") + 1]})
        result["mount"] = match[1] if match else {"state": "unavailable"}
    except (OSError, ValueError) as error:
        result["mount"] = {"state": "unavailable", "error": type(error).__name__}
    return result


def _require_runner_directory(path: pathlib.Path, expected: os.stat_result | None = None) -> os.stat_result:
    """Enforce the unprivileged harness/service's exact private source identity."""
    info = os.lstat(path)
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and info.st_gid == os.getgid() and stat.S_IMODE(info.st_mode) == 0o700,
            f"bind source ownership/mode changed: {_host_path_evidence(path)}")
    require(os.access(path.parent, os.X_OK) and os.access(path, os.R_OK | os.W_OK | os.X_OK),
            f"runner cannot traverse/read/write bind source: {_host_path_evidence(path)}")
    if expected is not None:
        require((info.st_dev, info.st_ino) == (expected.st_dev, expected.st_ino),
                f"bind source identity changed: {_host_path_evidence(path)}")
    return info


def _workspace_bind_checkpoint(label: str, paths: tuple[pathlib.Path, ...],
                               *, operation: str) -> None:
    credentials = {"uid": os.getuid(), "euid": os.geteuid(),
                   "gid": os.getgid(), "egid": os.getegid(),
                   "groups": os.getgroups()}
    evidence = []
    for path in paths:
        evidence.append({
            "path": _host_path_evidence(path),
            "access": {"parent_x": os.access(path.parent, os.X_OK, effective_ids=True),
                       "read": os.access(path, os.R_OK, effective_ids=True),
                       "write": os.access(path, os.W_OK, effective_ids=True),
                       "execute": os.access(path, os.X_OK, effective_ids=True)},
        })
    print("workspace_bind_checkpoint=" + json.dumps({
        "label": label, "operation": operation, "credentials": credentials,
        "paths": evidence,
    }, sort_keys=True, separators=(",", ":")), flush=True)


def _report_workspace_bind_exception(case: str, operation: str, error: BaseException,
                                     paths: tuple[pathlib.Path, ...]) -> None:
    detail = {
        "case": case, "operation": operation,
        "exception": type(error).__name__,
        "errno": error.errno if isinstance(error, OSError) else None,
        "error": str(error)[:500],
        "credentials": {"uid": os.getuid(), "euid": os.geteuid(),
                        "gid": os.getgid(), "egid": os.getegid(),
                        "groups": os.getgroups()},
        "traceback": traceback.format_exc(limit=6)[-2500:],
    }
    print("workspace_bind_exception=" + json.dumps(
        detail, sort_keys=True, separators=(",", ":")), flush=True)
    _workspace_bind_checkpoint("exception", paths, operation=operation)


def _report_transient_bind_unit(unit_name: str, *, emit: bool = True) -> dict[str, str]:
    properties = (
        "LoadState,ActiveState,SubState,Result,MainPID,User,Group,SupplementaryGroups,"
        "RuntimeDirectory,RuntimeDirectoryMode,RuntimeDirectoryPreserve,"
        "BindPaths,ReadWritePaths,ProtectHome,ProtectSystem,PrivateTmp,"
        "RemainAfterExit,KillMode,ExecStop,ExecStopPost"
    )
    result = run(["/usr/bin/systemctl", "show", "--no-pager",
                  f"--property={properties}", unit_name], check=False)
    details = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if emit:
        print("workspace_bind_transient_unit=" + json.dumps({
            "unit": unit_name, "show_exit": result.returncode, "properties": details,
            "stderr": (result.stderr or "")[-1200:],
        }, sort_keys=True, separators=(",", ":")), flush=True)
    return details


def _run_transient_bind_case(argv: list[str], *, unit_name: str, source: pathlib.Path,
                             destination: pathlib.Path, protect_home: str) -> subprocess.CompletedProcess:
    """Hold the service briefly so systemctl can report its loaded properties."""
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 12
    observed: dict[str, str] = {}
    property_error: BaseException | None = None
    observed_active = False
    while time.monotonic() < deadline and process.poll() is None:
        observed = _report_transient_bind_unit(unit_name, emit=False)
        if observed.get("ActiveState") == "active" and int(observed.get("MainPID", "0")) > 1:
            observed_active = True
            _report_transient_bind_unit(unit_name)
            try:
                expected = {
                    "LoadState": "loaded",
                    "User": pwd.getpwuid(os.getuid()).pw_name,
                    "Group": grp.getgrgid(os.getgid()).gr_name,
                    "SupplementaryGroups": "lm-vm-control",
                    "RuntimeDirectory": "little-mester-vm-workspace little-mester-vm-workspace-share",
                    "RuntimeDirectoryMode": "0750",
                    "RuntimeDirectoryPreserve": "no",
                    "ProtectHome": protect_home,
                    "ProtectSystem": "strict",
                    "PrivateTmp": "yes",
                    "RemainAfterExit": "no",
                    "KillMode": "control-group",
                }
                for name, value in expected.items():
                    require(observed.get(name) == value,
                            f"transient systemd property mismatch for {name}: {observed.get(name)!r}")
                bind_paths = observed.get("BindPaths", "")
                require(str(source) in bind_paths and str(destination) in bind_paths,
                        "effective transient BindPaths do not contain the exact source/destination: "
                        f"{bind_paths!r}")
                rw_paths = observed.get("ReadWritePaths", "")
                require("/run/little-mester-vm-workspace" in rw_paths
                        and str(destination) in rw_paths,
                        f"effective transient ReadWritePaths mismatch: {rw_paths!r}")
                print("workspace_bind_transient_unit_effective=PASS", flush=True)
            except Exception as error:  # noqa: BLE001 - retain configuration mismatch while draining child
                property_error = error
            break
        time.sleep(0.05)
    try:
        stdout, stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", unit_name],
            timeout=15, check=False)
        process.kill()
        stdout, stderr = process.communicate()
        raise RuntimeError(f"transient systemd case timed out; stdout={stdout[-1000:]!r} "
                           f"stderr={stderr[-1000:]!r}")
    if not observed_active:
        # Preserve failure diagnostics even when systemd rejected the unit
        # before ExecStart could hold it open for property inspection.
        _report_transient_bind_unit(unit_name)
        raise RuntimeError("transient systemd unit was never observed active with a MainPID")
    if property_error is not None:
        raise property_error
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def report_workspace_unit_configuration() -> None:
    properties = (
        "FragmentPath,DropInPaths,Type,User,Group,SupplementaryGroups,RuntimeDirectory,"
        "RuntimeDirectoryMode,RuntimeDirectoryPreserve,UMask,ExecStart,ExecStartPost,Restart,"
        "KillMode,TimeoutStartUSec,TimeoutStopUSec,NoNewPrivileges,NoExecPaths,ExecPaths,"
        "PrivateTmp,ProtectHome,ProtectSystem,ProtectControlGroups,BindPaths,ReadWritePaths,"
        "PartOf,Before,After,Requires,BindsTo"
    )
    result = run(["/usr/bin/systemctl", "show", "--no-pager",
                  f"--property={properties}", WORKSPACE_UNIT])
    print("workspace_effective_systemd=" + json.dumps(
        dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line),
        sort_keys=True, separators=(",", ":")), flush=True)
    cat = run(["/usr/bin/systemctl", "cat", "--no-pager", WORKSPACE_UNIT])
    print("workspace_merged_unit_BEGIN", flush=True)
    print(cat.stdout[-6000:], flush=True)
    print("workspace_merged_unit_END", flush=True)


def _bind_repro_case(*, source: pathlib.Path, source_kind: str,
                     protect_home: str, token: str,
                     control_source: pathlib.Path) -> tuple[bool, bool]:
    runtime_names = "little-mester-vm-workspace little-mester-vm-workspace-share"
    unit_name = f"lm-vm-bind-repro-{source_kind}-{protect_home}-{token}.service"
    runtime_directories = tuple(pathlib.Path("/run") / name for name in runtime_names.split())
    destination = runtime_directories[1]
    paths = (source, control_source, *runtime_directories)
    operation = "validate-systemd-runtime-directory-absence"
    primary_error: BaseException | None = None
    for path in runtime_directories:
        try:
            os.lstat(path)
        except FileNotFoundError:
            continue
        raise RuntimeError(f"preserving pre-existing bind reproduction RuntimeDirectory: {path}")
    try:
        operation = "validate-private-source-directory"
        source_stat = os.stat(source, follow_symlinks=False)
        _require_runner_directory(source, source_stat)
        _require_runner_directory(control_source)
        marker_name = ".lm-bind-repro-marker"
        marker_path = source / marker_name
        marker = f"LM_BIND_REPRO:{token}:{source_kind}:{protect_home}".encode("ascii")
        operation = "create-source-marker"
        marker_fd = os.open(marker_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(marker_fd, marker)
            os.fsync(marker_fd)
        finally:
            os.close(marker_fd)
        print("workspace_bind_repro_host=" + json.dumps({
            "case": source_kind, "protect_home": protect_home,
            "controller": {"uid": os.getuid(), "euid": os.geteuid(),
                           "gid": os.getgid(), "egid": os.getegid(),
                           "groups": os.getgroups()},
            "source": _host_path_evidence(source),
            "control_source": _host_path_evidence(control_source),
            "destination_before_start": _host_path_evidence(destination),
            "destination_lifecycle": "transient systemd RuntimeDirectory; no preserve",
        }, sort_keys=True, separators=(",", ":")), flush=True)
        code = textwrap.dedent("""\
            import errno, json, os, pathlib, sys, time
            identity = {'uid': os.getuid(), 'euid': os.geteuid(), 'gid': os.getgid(),
                        'egid': os.getegid(), 'groups': os.getgroups()}
            try:
                os.stat(sys.argv[6])
                home_hidden = False
            except OSError as error:
                home_hidden = error.errno in (errno.EACCES, errno.EPERM, errno.ENOENT)
            target = pathlib.Path(sys.argv[1])
            item = target / sys.argv[2]
            source_stat = os.stat(target, follow_symlinks=False)
            mounts = pathlib.Path('/proc/self/mountinfo').read_text().splitlines()
            exact = any((lambda fields: len(fields) > 4 and
                         fields[4].replace('\\\\040', ' ') == str(target))(line.split())
                        for line in mounts)
            evidence = {
                'device': source_stat.st_dev, 'inode': source_stat.st_ino,
                'uid': source_stat.st_uid, 'gid': source_stat.st_gid,
                'mode': source_stat.st_mode & 0o777, 'exact_mountpoint': exact,
                'marker_matches': item.read_bytes() == sys.argv[5].encode(),
                'unrelated_home_hidden': home_hidden,
                'identity_matches': identity['euid'] == int(sys.argv[7])
                    and identity['egid'] == int(sys.argv[8])
                    and int(sys.argv[9]) in identity['groups'],
            }
            print('WORKSPACE_BIND_REPRO_IDENTITY ' + json.dumps(identity, sort_keys=True), flush=True)
            print('WORKSPACE_BIND_REPRO_TARGET ' + json.dumps(evidence, sort_keys=True), flush=True)
            time.sleep(1.5)
            valid = ((source_stat.st_dev, source_stat.st_ino) ==
                     (int(sys.argv[3]), int(sys.argv[4])) and exact
                     and evidence['marker_matches'] and home_hidden
                     and evidence['identity_matches'])
            raise SystemExit(0 if valid else 43)
            """)
        exec_paths = "/usr/bin /usr/lib -/usr/lib64 -/usr/libexec /lib -/lib64"
        argv = [
            "/usr/bin/sudo", "-n", "/usr/bin/systemd-run", "--wait", "--pipe",
            f"--unit={unit_name}", "--service-type=exec",
            f"--property=User={pwd.getpwuid(os.getuid()).pw_name}",
            f"--property=Group={grp.getgrgid(os.getgid()).gr_name}",
            f"--property=RuntimeDirectory={runtime_names}", "--property=RuntimeDirectoryMode=0750",
            "--property=RuntimeDirectoryPreserve=no",
            "--property=UMask=0077", f"--property=ProtectHome={protect_home}",
            "--property=ProtectSystem=strict", "--property=PrivateTmp=yes",
            "--property=NoNewPrivileges=no", "--property=NoExecPaths=/",
            f"--property=ExecPaths={exec_paths}", "--property=ProtectControlGroups=yes",
            "--property=SupplementaryGroups=lm-vm-control",
            f"--property=ReadWritePaths={' '.join(map(str, runtime_directories))}",
            f"--property=BindPaths={source}:{destination}",
            "/usr/bin/python3", "-I", "-c", code, str(destination), marker_name,
            str(source_stat.st_dev), str(source_stat.st_ino), marker.decode("ascii"),
            pwd.getpwuid(os.getuid()).pw_dir, str(os.getuid()), str(os.getgid()),
            str(grp.getgrnam("lm-vm-control").gr_gid),
        ]
        _workspace_bind_checkpoint("before-systemd-run", paths, operation=operation)
        operation = "run-transient-systemd-case"
        result = _run_transient_bind_case(
            argv, unit_name=unit_name, source=source, destination=destination,
            protect_home=protect_home)
        operation = "inspect-effective-transient-unit"
        success = result.returncode == 0 and "WORKSPACE_BIND_REPRO_TARGET " in result.stdout
        namespace_refused = ("226/NAMESPACE" in result.stdout + result.stderr
                            or "Failed to set up mount namespacing" in result.stdout + result.stderr)
        actual = "PASS" if success else "NAMESPACE_REFUSED" if namespace_refused else "OTHER_FAILURE"
        print(f"workspace_bind_repro_result case={source_kind} protect_home={protect_home} "
              f"exit={result.returncode} actual={actual}", flush=True)
        if result.stdout:
            print(result.stdout[-2500:], flush=True)
        if result.stderr:
            print(result.stderr[-2500:], flush=True)
        print("workspace_bind_repro_host_after=" + json.dumps({
            "case": source_kind, "protect_home": protect_home,
            "destination": _host_path_evidence(destination),
            "source": _host_path_evidence(source),
        }, sort_keys=True, separators=(",", ":")), flush=True)
        operation = "verify-source-identity-after-systemd-case"
        _require_runner_directory(source, source_stat)
        return success, namespace_refused
    except BaseException as error:
        primary_error = error
        _report_workspace_bind_exception(source_kind, operation, error, paths)
        raise
    finally:
        cleanup_errors: list[tuple[str, BaseException]] = []
        try:
            operation = "stop-transient-systemd-unit"
            run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", unit_name],
                timeout=15, check=False)
        except BaseException as error:  # noqa: BLE001 - preserve the original systemd operation failure
            _report_workspace_bind_exception(
                f"{source_kind}-cleanup", operation, error, paths)
            cleanup_errors.append((operation, error))
        try:
            if "marker_path" in locals():
                operation = "remove-harness-owned-source-marker"
                try:
                    marker_info = os.lstat(marker_path)
                except FileNotFoundError:
                    marker_info = None
                if marker_info is not None:
                    require(stat.S_ISREG(marker_info.st_mode)
                            and marker_info.st_uid == os.getuid()
                            and stat.S_IMODE(marker_info.st_mode) == 0o600
                            and marker_info.st_nlink == 1,
                            "preserving changed workspace bind reproduction marker")
                    os.remove(marker_path)
        except BaseException as error:  # noqa: BLE001 - preserve the original systemd operation failure
            _report_workspace_bind_exception(
                f"{source_kind}-cleanup", operation, error, paths)
            cleanup_errors.append((operation, error))
        try:
            operation = "verify-systemd-runtime-directory-cleanup"
            for directory in reversed(runtime_directories):
                try:
                    os.lstat(directory)
                except FileNotFoundError:
                    continue
                raise RuntimeError(
                    "systemd did not remove its transient RuntimeDirectory after stop; "
                    f"preserving it for inspection: {_host_path_evidence(directory)}")
        except BaseException as error:  # noqa: BLE001 - preserve the original systemd operation failure
            _report_workspace_bind_exception(
                f"{source_kind}-cleanup", operation, error, paths)
            cleanup_errors.append((operation, error))
        if cleanup_errors and primary_error is None:
            raise cleanup_errors[0][1]


def run_workspace_bind_reproductions(workspace: pathlib.Path) -> None:
    """Compare real transient systemd namespaces for home and non-home sources."""
    temp_parent = pathlib.Path("/var/tmp")
    parent_info = os.lstat(temp_parent)
    require(stat.S_ISDIR(parent_info.st_mode) and not stat.S_ISLNK(parent_info.st_mode)
            and parent_info.st_uid == 0 and stat.S_IMODE(parent_info.st_mode) & 0o1000,
            "preserving unsafe /var/tmp control-source parent")
    production_runtime = (pathlib.Path("/run/little-mester-vm-workspace"),
                          pathlib.Path("/run/little-mester-vm-workspace-share"))
    for path in production_runtime:
        try:
            os.lstat(path)
        except FileNotFoundError:
            continue
        raise RuntimeError(f"preserving pre-existing workspace RuntimeDirectory path: {path}")
    token = secrets.token_hex(6)
    outside = pathlib.Path(tempfile.mkdtemp(
        prefix=f"lm-vm-bind-control-{token}-", dir=temp_parent))
    outside_created = os.lstat(outside)
    outside_identity = _require_runner_directory(outside)
    require((outside_created.st_dev, outside_created.st_ino)
            == (outside_identity.st_dev, outside_identity.st_ino),
            "bind control source changed during private directory creation")
    print("workspace_bind_control_source_ready=" + json.dumps({
        "path": str(outside), "uid": outside_identity.st_uid,
        "gid": outside_identity.st_gid, "mode": stat.S_IMODE(outside_identity.st_mode),
        "controller_uid": os.getuid(), "controller_euid": os.geteuid(),
        "controller_groups": os.getgroups(), "parent_traversable": os.access(temp_parent, os.X_OK),
        "parent_mount": _host_path_evidence(temp_parent).get("mount"),
    }, sort_keys=True, separators=(",", ":")), flush=True)
    cases: list[tuple[pathlib.Path, str, str, bool]] = []
    primary_error: BaseException | None = None
    try:
        # All four source/protection combinations must bind at the /run
        # destination. Each service also proves an unrelated home is hidden,
        # the selected source inode is mounted exactly, and the owner identity
        # is correct. ProtectHome constrains paths it hides, not this /run target.
        cases = [
            (workspace, "home", "yes", True),
            (outside, "outside-home", "yes", True),
            (workspace, "home", "tmpfs", True),
            (outside, "outside-home", "tmpfs", True),
        ]
        outcomes = []
        for source, kind, protect, expected in cases:
            checkpoint_paths = (workspace, outside, *production_runtime)
            case_name = f"{kind}-{protect}"
            _workspace_bind_checkpoint(f"before-case-{kind}-{protect}", checkpoint_paths,
                                       operation="prepare-next-reproduction-case")
            case_error: BaseException | None = None
            try:
                success, namespace_refused = _bind_repro_case(
                    source=source, source_kind=kind, protect_home=protect, token=token,
                    control_source=outside)
                outcomes.append(success if expected else namespace_refused)
            except BaseException as error:
                case_error = error
                _report_workspace_bind_exception(
                    case_name, "case-preparation-or-execution", error,
                    checkpoint_paths)
                raise
            finally:
                try:
                    _workspace_bind_checkpoint(
                        f"after-case-{kind}-{protect}", checkpoint_paths,
                        operation="case-and-transient-unit-cleanup-complete")
                except BaseException as checkpoint_error:
                    _report_workspace_bind_exception(
                        case_name, "after-case-checkpoint", checkpoint_error,
                        checkpoint_paths)
                    if case_error is None:
                        raise
        require(all(outcomes), "real systemd workspace-bind comparison did not match the expected matrix")
        print("workspace_bind_repro_matrix=PASS home/yes=PASS home/tmpfs=PASS "
              "outside-home/yes=PASS outside-home/tmpfs=PASS unrelated_home=HIDDEN", flush=True)
        run_production_dependency_reproduction(workspace)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_operation = "verify-private-control-source-before-cleanup"
        try:
            _workspace_bind_checkpoint("before-control-source-cleanup", (outside,),
                                       operation=cleanup_operation)
            _require_runner_directory(outside, outside_identity)
            cleanup_operation = "verify-control-source-empty"
            require(not any(outside.iterdir()),
                    "preserving unexpected bind reproduction control-source evidence")
            current = os.lstat(outside)
            require((current.st_dev, current.st_ino, current.st_uid, current.st_gid,
                     stat.S_IMODE(current.st_mode))
                    == (outside_identity.st_dev, outside_identity.st_ino, os.getuid(),
                        os.getgid(), 0o700),
                    "preserving changed bind control-source identity")
            cleanup_operation = "remove-exact-harness-owned-control-source"
            outside.rmdir()
        except BaseException as error:
            _report_workspace_bind_exception("control-source-cleanup", cleanup_operation,
                                             error, (outside,))
            if primary_error is None:
                raise


def run_production_dependency_reproduction(workspace: pathlib.Path) -> None:
    """Reproduce the production workspace unit and parent dependency graph with stubs."""
    token = secrets.token_hex(6)
    prefix = f"lm-vm-workspace-repro-{token}"
    workspace_unit, proxy_unit = f"{prefix}.service", f"{prefix}-proxy.service"
    metrics_unit, vm_unit = f"{prefix}-metrics.service", f"{prefix}-vm.service"
    destination = "/run/little-mester-vm-workspace-share"
    user = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    runtime_paths = ("/run/little-mester-vm-workspace", destination, "/run/little-mester-vm")
    for raw in runtime_paths:
        path = pathlib.Path(raw)
        require(not path.exists() and not path.is_symlink(),
                f"preserving pre-existing disposable RuntimeDirectory: {path}")
    workspace_template = (REPO / "systemd/little-mester-vm-workspace.service").read_text(
        encoding="utf-8")
    substitutions = (
        ("@LM_VM_OWNER@", user), ("@LM_VM_OWNER_GROUP@", group),
        ("PartOf=little-mester-vm.service", f"PartOf={vm_unit}"),
        ("Before=little-mester-vm.service", f"Before={vm_unit}"),
        ("ExecStart=/usr/bin/python3 -I /usr/lib/little-mester-vm/system-vm-workspace.py serve",
         "ExecStart=/usr/bin/sleep 90"),
        ("ExecStartPost=/usr/bin/python3 -I /usr/lib/little-mester-vm/system-vm-workspace.py socket-permissions",
         "ExecStartPost=/usr/bin/true"),
    )
    for original, replacement in substitutions:
        require(workspace_template.count(original) == 1,
                f"production workspace unit template changed unexpectedly: {original}")
        workspace_template = workspace_template.replace(original, replacement)
    workspace_text = (workspace_template.rstrip() + f'\nBindPaths="{workspace}:{destination}"\n')
    units = {
        workspace_unit: workspace_text,
        proxy_unit: f"""[Unit]\nDescription=Disposable production proxy dependency\nAfter=network-online.target\nWants=network-online.target\nBefore={vm_unit}\nPartOf={vm_unit}\n\n[Service]\nType=exec\nUser={user}\nGroup={group}\nExecStart=/usr/bin/sleep 90\nRestart=no\n""",
        metrics_unit: f"""[Unit]\nDescription=Disposable production metrics dependency\nBefore={vm_unit}\nPartOf={vm_unit}\n\n[Service]\nType=exec\nUser={user}\nGroup={group}\nExecStart=/usr/bin/sleep 90\nRestart=no\n""",
        vm_unit: f"""[Unit]\nDescription=Disposable parent dependency graph\nRequires={proxy_unit} {workspace_unit} {metrics_unit}\nBindsTo={proxy_unit} {workspace_unit} {metrics_unit}\nAfter={proxy_unit} {workspace_unit} {metrics_unit}\n\n[Service]\nType=exec\nUser={user}\nGroup={group}\nRuntimeDirectory=little-mester-vm\nRuntimeDirectoryMode=0700\nExecStart=/usr/bin/sleep 90\nRestart=no\n""",
    }
    payload = json.dumps(units, sort_keys=True, separators=(",", ":"))
    writer = textwrap.dedent("""\
        import json, os, pathlib, sys
        root = pathlib.Path('/run/systemd/system')
        units = json.loads(sys.stdin.read())
        if not root.is_dir():
            raise SystemExit('system unit directory unavailable')
        created = []
        try:
            for name, text in units.items():
                if not name.startswith('lm-vm-workspace-repro-') or '/' in name:
                    raise SystemExit('invalid disposable unit name')
                path = root / name
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
                created.append((path, text.encode()))
                try:
                    data = memoryview(text.encode())
                    while data:
                        written = os.write(fd, data)
                        if written <= 0:
                            raise OSError('unit file write made no progress')
                        data = data[written:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except BaseException:
            for path, expected in reversed(created):
                try:
                    if path.read_bytes() == expected:
                        path.unlink()
                except OSError:
                    pass
            raise
        """)
    create = subprocess.run(["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-c", writer],
                            input=payload, text=True, capture_output=True,
                            timeout=10, check=False)
    require(create.returncode == 0, "could not safely install disposable systemd reproduction units")
    try:
        run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "daemon-reload"], timeout=15)
        start = run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "start", vm_unit],
                    timeout=35, check=False)
        properties = ("LoadState,ActiveState,SubState,MainPID,Result,Type,User,Group,"
                      "SupplementaryGroups,RuntimeDirectory,RuntimeDirectoryMode,UMask,"
                      "RuntimeDirectoryPreserve,ExecStart,ExecStartPost,Restart,KillMode,"
                      "TimeoutStartUSec,TimeoutStopUSec,"
                      "NoNewPrivileges,NoExecPaths,ExecPaths,PrivateTmp,ProtectHome,"
                      "ProtectSystem,ProtectControlGroups,ReadWritePaths,BindPaths,PartOf,"
                      "Before,After,Requires,BindsTo")
        states = {}
        for name in units:
            result = run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "show", "--no-pager",
                          f"--property={properties}", name], check=False)
            states[name] = dict(line.split("=", 1) for line in result.stdout.splitlines()
                                if "=" in line)
        journal = run(["/usr/bin/sudo", "-n", "/usr/bin/journalctl", "--no-pager",
                       "-n", "40", "-u", workspace_unit], check=False, timeout=10)
        print("workspace_bind_dependency_repro=" + json.dumps({
            "units": states, "start_exit": start.returncode,
            "start_stderr": (start.stderr or "")[-1600:],
            "workspace_journal": (journal.stdout or "")[-5000:],
            "source": _host_path_evidence(workspace),
            "runtime_directories": [_host_path_evidence(pathlib.Path(raw)) for raw in runtime_paths],
        }, sort_keys=True, separators=(",", ":")), flush=True)
        workspace_state = states[workspace_unit]
        expected = {
            "Type": "exec", "User": user, "Group": group,
            "RuntimeDirectory": "little-mester-vm-workspace little-mester-vm-workspace-share",
            "RuntimeDirectoryMode": "0750", "RuntimeDirectoryPreserve": "no",
            "UMask": "0077", "Restart": "no",
            "KillMode": "control-group", "TimeoutStartUSec": "20s", "TimeoutStopUSec": "10s",
            "NoNewPrivileges": "no", "NoExecPaths": "/", "PrivateTmp": "yes",
            "ProtectHome": "tmpfs", "ProtectSystem": "strict", "ProtectControlGroups": "yes",
        }
        mismatches = {key: {"expected": value, "actual": workspace_state.get(key)}
                      for key, value in expected.items() if workspace_state.get(key) != value}
        for key, value in (("SupplementaryGroups", "lm-vm-control"),
                           ("ExecPaths", "/usr/bin"), ("ReadWritePaths", destination),
                           ("BindPaths", f"{workspace}:{destination}"),
                           ("PartOf", vm_unit), ("Before", vm_unit),
                           ("After", "local-fs.target")):
            if value not in workspace_state.get(key, ""):
                mismatches[key] = {"expected_contains": value,
                                   "actual": workspace_state.get(key)}
        for key, executable in (("ExecStart", "/usr/bin/sleep"),
                                ("ExecStartPost", "/usr/bin/true")):
            if f"path={executable}" not in workspace_state.get(key, ""):
                mismatches[key] = {"expected_executable": executable,
                                   "actual": workspace_state.get(key)}
        for key in ("Requires", "BindsTo", "After"):
            for dependency in (proxy_unit, workspace_unit, metrics_unit):
                if dependency not in states[vm_unit].get(key, ""):
                    mismatches[f"vm.{key}.{dependency}"] = {
                        "expected_contains": dependency,
                        "actual": states[vm_unit].get(key),
                    }
        print("workspace_bind_dependency_configuration="
              + ("PASS" if not mismatches else "MISMATCH "
                 + json.dumps(mismatches, sort_keys=True, separators=(",", ":"))), flush=True)
        require(not mismatches,
                "disposable dependency reproduction did not retain the required effective production settings")
        diagnostic = (start.stdout or "") + (start.stderr or "") + (journal.stdout or "")
        reproduced = ("226/NAMESPACE" in diagnostic
                      or "Failed to set up mount namespacing" in diagnostic
                      or workspace_state.get("Result") == "resources")
        print("workspace_bind_dependency_reproduction="
              f"{'REPRODUCED_226_NAMESPACE' if reproduced else 'NOT_REPRODUCED'}", flush=True)
    finally:
        run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", vm_unit, workspace_unit,
             proxy_unit, metrics_unit], timeout=30, check=False)
        for name in units:
            stopped = run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "show", "--no-pager",
                           "--property=ActiveState", "--property=MainPID", name], check=False)
            stopped_state = dict(line.split("=", 1) for line in stopped.stdout.splitlines()
                                 if "=" in line)
            require(stopped_state.get("ActiveState") in ("inactive", "failed")
                    and stopped_state.get("MainPID") == "0",
                    f"preserving disposable systemd units that did not stop: {name} {stopped_state}")
        remover = textwrap.dedent("""\
            import json, os, pathlib, stat, sys
            root = pathlib.Path('/run/systemd/system')
            units = json.loads(sys.stdin.read())
            for name, text in units.items():
                path = root / name
                info = os.lstat(path)
                if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise SystemExit('preserving changed disposable unit file')
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    actual = os.read(fd, len(text.encode()) + 1)
                finally:
                    os.close(fd)
                if actual != text.encode():
                    raise SystemExit('preserving changed disposable unit file')
                os.unlink(path)
            """)
        removed = subprocess.run(["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-c", remover],
                                 input=payload, text=True, capture_output=True,
                                 timeout=10, check=False)
        require(removed.returncode == 0, "preserving changed or unavailable disposable unit files")
        run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "daemon-reload"], timeout=15)
        run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "reset-failed", workspace_unit,
             proxy_unit, metrics_unit, vm_unit], timeout=10, check=False)
        for raw in runtime_paths:
            path = pathlib.Path(raw)
            require(not path.exists() and not path.is_symlink(),
                    f"systemd did not clean disposable RuntimeDirectory: {path}")


def remove_disposable_workspace() -> None:
    workspace = disposable_workspace_path()
    try:
        info = os.lstat(workspace)
    except FileNotFoundError:
        return
    require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
            and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
            "preserving unsafe disposable workspace path")
    require(not any(workspace.iterdir()), "disposable workspace contains unexpected evidence; preserving it")
    workspace.rmdir()


def create_escape_sentinel(workspace: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, tuple[int, int], tuple[int, int], bytes]:
    outside = workspace.parent / f"{workspace.name}-outside-sentinel"
    link = workspace / ".lm-vm-outside-boundary"
    require(not outside.exists() and not outside.is_symlink()
            and not link.exists() and not link.is_symlink(),
            "preserving pre-existing workspace boundary test evidence")
    payload = (f"LM-CI-BOUNDARY:{os.environ['GITHUB_RUN_ID']}:{os.environ['GITHUB_RUN_ATTEMPT']}\n"
               .encode("ascii"))
    fd = os.open(outside, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
        file_info = os.fstat(fd)
    finally:
        os.close(fd)
    os.symlink(outside, link)
    link_info = os.lstat(link)
    return outside, link, (file_info.st_dev, file_info.st_ino), (link_info.st_dev, link_info.st_ino), payload


def verify_guest_cannot_read_escape() -> None:
    command = shlex.join(["/usr/bin/python3", "-I", str(pathlib.Path(__file__).resolve()),
                          "--guest-workspace-boundary"])
    run(["/usr/bin/sg", "lm-vm-control", "-c", command], timeout=30)


def remove_escape_sentinel(evidence) -> None:
    if evidence is None:
        workspace = disposable_workspace_path()
        outside = workspace.parent / f"{workspace.name}-outside-sentinel"
        link = workspace / ".lm-vm-outside-boundary"
        file_identity = link_identity = None
        payload = (f"LM-CI-BOUNDARY:{os.environ['GITHUB_RUN_ID']}:{os.environ['GITHUB_RUN_ATTEMPT']}\n"
                   .encode("ascii"))
        if not outside.exists() and not outside.is_symlink() and not link.exists() and not link.is_symlink():
            return
    else:
        outside, link, file_identity, link_identity, payload = evidence
    link_info = os.lstat(link)
    require(stat.S_ISLNK(link_info.st_mode)
            and (link_identity is None or (link_info.st_dev, link_info.st_ino) == link_identity)
            and os.readlink(link) == str(outside),
            "preserving changed guest escape-symlink evidence")
    file_info = os.lstat(outside)
    require(stat.S_ISREG(file_info.st_mode) and file_info.st_uid == os.getuid()
            and stat.S_IMODE(file_info.st_mode) == 0o600
            and (file_identity is None or (file_info.st_dev, file_info.st_ino) == file_identity)
            and outside.read_bytes() == payload,
            "preserving changed host-only escape sentinel")
    os.remove(link)
    os.remove(outside)


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


def expected_installed_bytes(sha: str, source: str) -> bytes:
    payload = git_object_bytes(sha, source)
    if source == "systemd/little-mester-vm-workspace.service":
        owner = pwd.getpwuid(os.getuid())
        for marker, value in ((b"@LM_VM_OWNER@", owner.pw_name),
                              (b"@LM_VM_OWNER_GROUP@", grp.getgrgid(owner.pw_gid).gr_name)):
            require(payload.count(marker) == 1, "pinned workspace unit identity marker is ambiguous")
            payload = payload.replace(marker, value.encode("ascii"))
    return payload


def verify_installed_files() -> None:
    sha = os.environ.get("GITHUB_SHA", "")
    require(SHA_RE.fullmatch(sha) is not None, "GITHUB_SHA is not a full exact commit SHA")
    for dest, (source, mode) in PINNED_INSTALL_ASSETS.items():
        installed_mode, installed_uid = installed_asset_metadata(dest)
        require(stat.S_ISREG(installed_mode) and installed_uid == 0
                and stat.S_IMODE(installed_mode) == mode,
                f"installed production asset metadata mismatch: {dest}")
        installed = installed_asset_bytes(dest)
        require(installed == expected_installed_bytes(sha, source),
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


def system_qemu_pids() -> list[int]:
    """Return processes with the fixed production VM's unique QEMU name."""
    matches = []
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, ProcessLookupError):
            continue
        if b"qemu-system-x86_64" in argv[0] and any(
                argv[index:index + 2] == [b"-name", b"little-mester-system-vm"]
                for index in range(len(argv) - 1)):
            matches.append(int(entry.name))
    return matches


def force_systemd_fallback(root: pathlib.Path) -> None:
    """Freeze only this unit's verified QEMU MainPID and prove systemd kills it."""
    state = service_state(UNIT)
    require(state.get("ActiveState") == "active", "forced-stop case requires the active fixed unit")
    pid = int(state.get("MainPID", "0"))
    disk_before = fixed_disk_identity()
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
        "ExecStop=/usr/bin/python3 -I /usr/lib/little-mester-vm/system-vm.py qmp-stop $MAINPID",
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
        require(stopped.get("ActiveState") == "failed" and stopped.get("MainPID") == "0",
                f"systemd did not finish forced fallback stop: {stopped}")
        require(stopped.get("Result") == "exit-code",
                f"systemd did not record the failed identity-bound QMP ExecStop: {stopped}")
        poller = select.poll()
        poller.register(pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
        require(bool(poller.poll(1000)), "pidfd did not report exit of the exact frozen QEMU MainPID")
        require(not system_qemu_pids(),
                "a QEMU process with the fixed production VM identity remains after fallback")
        require(fixed_disk_identity() == disk_before,
                "fixed production disk identity changed during forced fallback")
        require(down_result.returncode in (0, 1),
                f"fixed system down returned an unexpected status after proven fallback: {down_result.returncode}")
        print("production_unit_forced_stop=PASS result=exit-code qmp_execstop_failed=true "
              "systemd_term_fallback=true pidfd_exit=true no_qemu=true same_disk=true")

        # A failed unit is expected when ExecStop cannot perform graceful QMP
        # shutdown. Reset only this test's expected failed state, then prove the
        # protected disk is still bootable and retains the canary.
        run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "reset-failed", UNIT], timeout=5)
        invoke_lab(root, "up", 16 * 60 + 45)
        restarted = service_state(UNIT)
        require(restarted.get("ActiveState") == "active"
                and int(restarted.get("MainPID", "0")) > 1,
                f"production VM did not restart after expected forced-stop failure: {restarted}")
        require(fixed_disk_identity() == disk_before,
                "fixed production restart did not retain the same disk identity")
        print("production_unit_forced_stop_recovery=PASS same_disk=true")
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
    unit_paths = tuple(pathlib.Path(path) for path in (
        "/etc/systemd/system/little-mester-vm.service",
        "/etc/systemd/system/little-mester-vm-proxy.service",
        "/etc/systemd/system/little-mester-vm-workspace.service",
        "/etc/systemd/system/little-mester-vm-metrics.service"))
    present = [path.exists() or path.is_symlink() for path in unit_paths]
    if not any(present):
        print("production_unit_cleanup=NOT_APPLICABLE production_unit_absent=true")
        return 0
    require(all(present), "partial fixed unit installation; preserving it without lifecycle action")
    verify_installed_files()
    states = [service_state(UNIT), service_state(PROXY_UNIT), service_state(WORKSPACE_UNIT),
              service_state(METRICS_UNIT)]
    loaded = [state.get("LoadState") == "loaded" for state in states]
    if not any(loaded):
        print("production_unit_cleanup=NOT_APPLICABLE systemd_units_not_loaded=true")
        return 0
    require(all(loaded), "only one fixed system VM unit is loaded; preserving ambiguous state")
    invoke_verified_cleanup(sha)
    assert_stopped()
    remove_escape_sentinel(None)
    remove_disposable_workspace()
    print("production_unit_cleanup_scope=production_systemd_unit")
    return 0


def main() -> int:
    if sys.argv[1:] == ["--guest-workspace-boundary"]:
        hosted_kvm_guard()
        root = CI_INSTALL_PARENT / os.environ["GITHUB_SHA"]
        spec = __import__("importlib.util", fromlist=["spec_from_file_location"]).spec_from_file_location(
            "ci_workspace_boundary_system_vm", root / "lib/vm/system-vm.py")
        if spec is None or spec.loader is None:
            raise RuntimeError("installed fixed system VM helper is unavailable")
        module = __import__("importlib.util", fromlist=["module_from_spec"]).module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        result = module._guest_ssh("test ! -r /workspace/.lm-vm-outside-boundary", timeout=20)
        require(result == 0, "guest read an out-of-boundary host sentinel through a symlink")
        print("production_workspace_boundary=PASS sibling_sentinel=unreadable")
        return 0
    if sys.argv[1:] == ["--cleanup"]:
        try:
            return cleanup_only()
        except BaseException as error:  # noqa: BLE001 - always report cleanup failure, including cancellation
            print(f"production_unit_cleanup=FAIL error={type(error).__name__}: {error}", file=sys.stderr)
            return 1
    require(not sys.argv[1:], "unsupported argument")
    installed_root: pathlib.Path | None = None
    workspace_path: pathlib.Path | None = None
    escape_evidence = None
    start_attempted = False
    failure: BaseException | None = None
    try:
        hosted_kvm_guard()
        production_host_preflight()
        fixed_name_preflight(install_parent=CI_INSTALL_PARENT)
        sha = os.environ["GITHUB_SHA"]
        source_sha = os.environ.get("LM_CI_SOURCE_HEAD_SHA", sha)
        require(SHA_RE.fullmatch(source_sha) is not None,
                "LM_CI_SOURCE_HEAD_SHA is not a full exact commit SHA")
        event = os.environ.get("GITHUB_EVENT_NAME", "unknown")
        merge_parent = os.environ.get("LM_CI_PR_MERGE_PARENT_SHA", "not-applicable")
        if event == "pull_request":
            require(SHA_RE.fullmatch(merge_parent) is not None and merge_parent == source_sha,
                    "verified PR merge parent does not match source HEAD")
        else:
            require(merge_parent == "not-applicable",
                    "unexpected PR merge-parent evidence for a non-PR event")
        print(f"evidence_scope=production_systemd_unit candidate_sha={sha} "
              f"workflow_sha={sha} source_head_sha={source_sha} "
              f"pr_merge_commit_second_parent={merge_parent} event={event} runner=ubuntu-24.04-kvm")
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
        workspace_path = disposable_workspace_path()
        write_start_marker(sha)
        start_attempted = True
        configure_disposable_workspace(installed_root)
        report_workspace_unit_configuration()
        share_target = pathlib.Path("/run/little-mester-vm-workspace-share")
        print("workspace_production_prestart_paths=" + json.dumps({
            "approved_source": _host_path_evidence(workspace_path),
            "runtime_directory_target": _host_path_evidence(share_target),
            "target_creator": "systemd RuntimeDirectory before namespace setup",
        }, sort_keys=True, separators=(",", ":")), flush=True)
        run_workspace_bind_reproductions(workspace_path)
        escape_evidence = create_escape_sentinel(workspace_path)
        invoke_shared_acceptance(sha)
        verify_workspace_startup_post()
        verify_guest_cannot_read_escape()
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
                remove_escape_sentinel(escape_evidence)
                remove_disposable_workspace()
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
