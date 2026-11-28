#!/usr/bin/env python3
"""Run the fixed production systemd VM unit only on disposable hosted KVM."""

from __future__ import annotations

import base64
import contextlib
import grp
import importlib.util
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
_WORKSPACE_MODULE_SPEC = importlib.util.spec_from_file_location(
    "system_vm_workspace", REPO / "lib/vm/system-vm-workspace.py")
if _WORKSPACE_MODULE_SPEC is None or _WORKSPACE_MODULE_SPEC.loader is None:
    raise RuntimeError("could not load the production workspace BindPaths writer")
WORKSPACE_MODULE = importlib.util.module_from_spec(_WORKSPACE_MODULE_SPEC)
sys.modules[_WORKSPACE_MODULE_SPEC.name] = WORKSPACE_MODULE
_WORKSPACE_MODULE_SPEC.loader.exec_module(WORKSPACE_MODULE)
CI_INSTALL_PREFIX = pathlib.Path("/srv/little-mester-ci")
CI_INSTALL_PARENT = CI_INSTALL_PREFIX / "v3"
UNIT = "little-mester-vm.service"
PROXY_UNIT = "little-mester-vm-proxy.service"
WORKSPACE_UNIT = "little-mester-vm-workspace.service"
METRICS_UNIT = "little-mester-vm-metrics.service"
PINNED_INSTALL_ASSETS = {
    "/usr/lib/little-mester-vm/system-vm.py": ("lib/vm/system-vm.py", 0o644),
    "/usr/lib/little-mester-vm/vm-instance.py": ("lib/vm/vm-instance.py", 0o644),
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
    "/usr/lib/little-mester-vm/host-metrics-tui/metrics-endpoint.js":
        ("agent/opencode/plugins/host-metrics-tui/metrics-endpoint.js", 0o644),
    "/usr/lib/little-mester-vm/host-metrics-tui/metrics-endpoint.d.ts":
        ("agent/opencode/plugins/host-metrics-tui/metrics-endpoint.d.ts", 0o644),
    "/usr/lib/little-mester-vm/host-metrics-tui/metrics-state.js":
        ("agent/opencode/plugins/host-metrics-tui/metrics-state.js", 0o644),
    "/usr/lib/little-mester-vm/host-metrics-tui/metrics-state.d.ts":
        ("agent/opencode/plugins/host-metrics-tui/metrics-state.d.ts", 0o644),
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


def _bounded_process_evidence(result: subprocess.CompletedProcess) -> dict[str, object]:
    return {
        "returncode": result.returncode,
        "stdout": (result.stdout or "")[-DIAGNOSTIC_OUTPUT_LIMIT:],
        "stderr": (result.stderr or "")[-DIAGNOSTIC_OUTPUT_LIMIT:],
    }


def _controller_identity() -> dict[str, object]:
    try:
        username = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        username = None
    return {
        "uid": os.getuid(), "euid": os.geteuid(),
        "gid": os.getgid(), "egid": os.getegid(),
        "supplementary_gids": sorted(os.getgroups()), "username": username,
        "privilege_command_prefix": ["/usr/bin/sudo", "-n"],
    }


def reset_failed_with_diagnostics(names: tuple[str, ...], *, phase: str,
                                  command_runner=None, emit=print) -> dict[str, object]:
    """Reset only failed disposable units and account for systemd GC races."""
    command_runner = run if command_runner is None else command_runner
    require(bool(names) and len(names) == len(set(names))
            and all(re.fullmatch(r"lm-vm-workspace-repro-[A-Za-z0-9_.@:-]+\.service", name)
                    for name in names),
            "refusing reset-failed outside uniquely named disposable workspace units")
    before: dict[str, object] = {}
    after: dict[str, object] = {}
    reset_attempts: list[dict[str, object]] = []

    def observe(name: str) -> dict[str, object]:
        argv = ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "show", "--no-pager",
                "--property=LoadState", "--property=ActiveState", "--property=SubState",
                "--property=MainPID", name]
        result = command_runner(argv, timeout=10, check=False)
        state: dict[str, str] = {}
        for line in (result.stdout or "").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            require(key not in state, f"duplicate systemd state property {key}: {name}")
            state[key] = value
        return {"command": argv, **_bounded_process_evidence(result), "state": state}

    for name in names:
        before[name] = observe(name)
    failed = []
    for name, observed in before.items():
        require(observed["returncode"] == 0,
                f"could not read disposable unit state before reset: {name}; evidence={observed}")
        state = observed["state"]
        load_state, active_state = state.get("LoadState"), state.get("ActiveState")
        sub_state, main_pid = state.get("SubState"), state.get("MainPID")
        require(main_pid == "0" and
                ((load_state == "loaded" and active_state in ("inactive", "failed")
                  and sub_state in ("dead", "failed"))
                 or (load_state == "not-found" and active_state == "inactive"
                     and sub_state == "dead")),
                f"unexpected pre-reset disposable unit state; evidence={observed}")
        if load_state == "loaded" and active_state == "failed":
            failed.append(name)

    # A single named unit per invocation makes a GC race attributable. Never
    # call reset-failed without an explicit failed disposable unit name.
    for name in failed:
        argv = ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "reset-failed", name]
        try:
            result = command_runner(argv, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            reset_attempts.append({"unit": name, "command": argv, "returncode": -1,
                                   "stdout": "", "stderr": f"{type(error).__name__}: {error}",
                                   "exact_not_loaded_error": False})
        else:
            not_loaded_error = re.compile(
                rf"(?:Failed to reset failed state of unit {re.escape(name)}: )?"
                rf"Unit {re.escape(name)} not loaded\.?\s*\Z", re.IGNORECASE)
            exact_not_loaded_error = (
                result.returncode != 0 and not (result.stdout or "").strip()
                and not_loaded_error.fullmatch((result.stderr or "").strip()) is not None)
            reset_attempts.append({"unit": name, "command": argv,
                                   **_bounded_process_evidence(result),
                                   "exact_not_loaded_error": exact_not_loaded_error})
    for name in names:
        after[name] = observe(name)
    invalid_after = {}
    for name, observed in after.items():
        state = observed["state"]
        if not (observed["returncode"] == 0
                and state.get("LoadState") in ("loaded", "not-found")
                and state.get("ActiveState") == "inactive"
                and state.get("SubState") == "dead"
                and state.get("MainPID") == "0"):
            invalid_after[name] = observed

    def is_gc_race(attempt: dict[str, object]) -> bool:
        name = attempt["unit"]
        observed = after[name]
        state = observed["state"]
        return (attempt["returncode"] != 0 and attempt["exact_not_loaded_error"]
                and observed["returncode"] == 0
                and state.get("LoadState") in ("loaded", "not-found")
                and state.get("ActiveState") == "inactive"
                and state.get("SubState") == "dead"
                and state.get("MainPID") == "0")

    invalid_attempts = [attempt for attempt in reset_attempts
                        if attempt["returncode"] != 0 and not is_gc_race(attempt)]
    aggregate_returncode = 1 if invalid_attempts or invalid_after else 0
    evidence: dict[str, object] = {
        "phase": phase, "controller": _controller_identity(),
        "reset": {"commands": reset_attempts,
                  "returncode": aggregate_returncode,
                  "gc_races_accepted": [attempt["unit"] for attempt in reset_attempts
                                        if attempt["returncode"] != 0 and is_gc_race(attempt)]},
        "before": before, "after": after, "post_state_errors": invalid_after,
    }
    serialized = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    emit("systemd_reset_attempt=" + serialized)
    if invalid_attempts or invalid_after:
        raise RuntimeError(f"systemctl reset-failed or post-state validation failed; evidence={serialized}")
    return evidence


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def hosted_runner_guard() -> None:
    require(os.environ.get("GITHUB_ACTIONS") == "true", "not running in GitHub Actions")
    require(os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted",
            "runner is not GitHub-hosted; refusing system installation")
    require(os.environ.get("RUNNER_OS") == "Linux" and os.environ.get("RUNNER_ARCH") == "X64",
            "runner is not the ubuntu-24.04 Linux x64 hosted KVM job")
    require(os.environ.get("GITHUB_WORKSPACE") == str(REPO),
            "checkout path differs from this script's repository")
    sha = os.environ.get("GITHUB_SHA", "")
    require(SHA_RE.fullmatch(sha) is not None, "GITHUB_SHA is not a full exact commit SHA")
    require(run(["git", "-C", str(REPO), "rev-parse", "HEAD"]).stdout.strip() == sha,
            "checkout does not equal workflow GITHUB_SHA")


def hosted_kvm_guard() -> None:
    hosted_runner_guard()
    require(pathlib.Path("/dev/kvm").is_char_device(), "hosted KVM device is unavailable")
    require(os.access("/dev/kvm", os.R_OK | os.W_OK), "hosted KVM device is not accessible")


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
    print("production_unit_stopped=PASS")


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
    """Run the exact installed owner-facing acceptance command."""
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
    expected_bind = WORKSPACE_MODULE._systemd_bind_path(workspace)
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
                require(bind_paths_semantically_match(
                    bind_paths, str(source), str(destination)),
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


def classify_namespace_reproduction(state: dict[str, str], diagnostics: str,
                                    *, start_exit: int,
                                    merged_dropin_matches: bool = False) -> str:
    """Classify only explicit namespace evidence from this attempt's journal slice."""
    if re.search(r"\bstatus=226/NAMESPACE\b", diagnostics) is not None:
        return "REPRODUCED_226_NAMESPACE"
    try:
        main_pid = int(state.get("MainPID", "0"))
    except ValueError:
        main_pid = 0
    if (start_exit == 0 and merged_dropin_matches
            and state.get("ActiveState") == "active"
            and state.get("SubState") == "running" and main_pid > 0):
        return "NOT_REPRODUCED"
    return "INCONCLUSIVE"


def expected_quoted_bind_negative_control(config_mismatches: dict, *,
                                          merged_configuration_matches: bool,
                                          raw_classification: str,
                                          start_exit: int) -> bool:
    """Recognize only the intended malformed-bind, fresh-226 quoted control."""
    mismatch = config_mismatches.get("BindPaths", {})
    return (set(config_mismatches) == {"BindPaths"}
            and mismatch.get("baseline_semantic_match") is True
            and mismatch.get("candidate_semantic_match") is False
            and merged_configuration_matches
            and raw_classification == "REPRODUCED_226_NAMESPACE"
            and start_exit != 0)


def parse_journal_cursor(output: str, exit_code: int) -> str:
    """Require a usable systemd journal cursor before starting a diagnostic attempt."""
    match = re.search(r"(?m)^-- cursor: (\S+)\s*$", output)
    require(exit_code == 0 and match is not None,
            "journal cursor unavailable; refusing diagnostic start")
    return match.group(1)


def journal_cursor_command() -> list[str]:
    """Capture a global cursor so a never-before-seen unit still has a boundary."""
    return ["/usr/bin/sudo", "-n", "/usr/bin/journalctl", "--no-pager",
            "--show-cursor", "-n", "0"]


def journal_after_cursor_command(cursor: str, unit: str) -> list[str]:
    return ["/usr/bin/sudo", "-n", "/usr/bin/journalctl", "--no-pager",
            f"--after-cursor={cursor}", "-n", "40", "-u", unit]


def classify_namespace_diagnostic(classification: str, diagnostics: str) -> str:
    if classification == "INCONCLUSIVE" and re.search(
            r"(?i)(execpaths|execution).{0,80}(denied|fail|not permitted)|"
            r"(denied|fail|not permitted).{0,80}(execpaths|execution)", diagnostics):
        return "EXECUTION_FAILURE_INCONCLUSIVE"
    return classification


def require_reproduced_control(classification: str) -> None:
    """Only advance from the standalone control after its expected 226 is proven."""
    if classification != "REPRODUCED_226_NAMESPACE":
        raise RuntimeError(
            "standalone production-template control was not an explicit, "
            f"configuration-matched 226/NAMESPACE: {classification}")


def compare_effective_configuration(baseline: dict[str, str], candidate: dict[str, str],
                                    *, allowed_setting: str | None = None,
                                    allow_bind_fragment_placement: bool = False,
                                    mount_topology: dict[str, str] | None = None,
                                    expected_bind: tuple[str, str] | None = None) -> dict:
    """Compare systemd show properties, allowing one deliberate delta only."""
    ignored = {"FragmentPath", "DropInPaths"} if allow_bind_fragment_placement else set()
    mismatches = {}
    if expected_bind is not None:
        source, destination = expected_bind
        baseline_bind = baseline.get("BindPaths", "")
        candidate_bind = candidate.get("BindPaths", "")
        baseline_valid = bind_paths_semantically_match(
            baseline_bind, source, destination)
        candidate_valid = bind_paths_semantically_match(
            candidate_bind, source, destination)
        if not candidate_valid or (not baseline_valid and allowed_setting != "BindPaths"):
            mismatches["BindPaths"] = {
                "baseline": baseline_bind, "candidate": candidate_bind,
                "baseline_semantic_match": baseline_valid,
                "candidate_semantic_match": candidate_valid,
            }
    for name in sorted(set(baseline) | set(candidate)):
        if name in ignored or name == allowed_setting:
            continue
        before, after = baseline.get(name), candidate.get(name)
        if name in ("ExecStart", "ExecStartPost"):
            before = normalize_systemd_exec(before or "")
            after = normalize_systemd_exec(after or "")
        if name in SYSTEMD_DEPENDENCY_SET_PROPERTIES or name in SYSTEMD_MOUNT_PATH_PROPERTIES:
            before = frozenset((before or "").split())
            after = frozenset((after or "").split())
            if before != after and allowed_setting == "PrivateTmp" \
                    and baseline.get("PrivateTmp") == "yes" \
                    and candidate.get("PrivateTmp") == "no":
                before, after = private_tmp_dependency_delta(
                    name, before, after, baseline, mount_topology=mount_topology)
        if before != after:
            mismatches[name] = {"baseline": _json_comparable(before),
                                "candidate": _json_comparable(after)}
    return mismatches


def bind_paths_semantically_match(value: str, source: str, destination: str) -> bool:
    """Accept only the observed one-bind, recursive effective systemd serialization.

    The hosted owner experiment reports SOURCE:DESTINATION:rbind.  Do not accept
    a two-field form until a hosted systemd experiment establishes its meaning.
    """
    return WORKSPACE_MODULE.effective_bind_paths_match(value, source, destination)


SYSTEMD_DEPENDENCY_SET_PROPERTIES = frozenset({
    "After", "Before", "Requires", "Wants", "BindsTo", "PartOf", "Conflicts",
    "Requisite",
})

SYSTEMD_MOUNT_PATH_PROPERTIES = frozenset({"RequiresMountsFor", "WantsMountsFor"})

PRIVATE_TMP_TMPFILES_UNIT = "systemd-tmpfiles-setup.service"
PRIVATE_TMP_TMP_MOUNT_UNIT = "tmp.mount"


def private_tmp_systemd_profile(baseline_properties: dict[str, str]) -> str | None:
    """Recognize only the upstream v255 and v257 PrivateTmp dependency shapes."""
    requires_paths = set((baseline_properties.get("RequiresMountsFor", "") or "").split())
    wants_paths = set((baseline_properties.get("WantsMountsFor", "") or "").split())
    private_tmp_paths = {"/tmp", "/var/tmp"}
    wanted_private_tmp_paths = wants_paths & private_tmp_paths
    required_private_tmp_paths = requires_paths & private_tmp_paths
    if wanted_private_tmp_paths == private_tmp_paths and not required_private_tmp_paths:
        return "v257"
    if ("/var/tmp" in required_private_tmp_paths
            and not wanted_private_tmp_paths):
        return "v255"
    return None


def _json_comparable(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return value


def private_tmp_dependency_delta(name: str, baseline: frozenset[str],
                                 candidate: frozenset[str],
                                 baseline_properties: dict[str, str], *,
                                 mount_topology: dict[str, tuple[str, ...]] | None
                                 ) -> tuple[frozenset[str], frozenset[str]]:
    """Permit only v255/v257-specific upstream PrivateTmp-generated removals."""
    profile = private_tmp_systemd_profile(baseline_properties)
    if profile is None:
        return baseline, candidate

    removable: set[str] = set()
    if name == "After":
        removable.add(PRIVATE_TMP_TMPFILES_UNIT)
    if profile == "v255":
        if name in ("After", "Wants"):
            # v255 unconditionally adds the tmp.mount dependency for PrivateTmp.
            removable.add(PRIVATE_TMP_TMP_MOUNT_UNIT)
        if name == "RequiresMountsFor":
            removable.add("/var/tmp")
        if name in ("Requires", "After") and mount_topology is not None:
            removable.update(mount_topology.get("/var/tmp", ()))
    else:  # v257
        if name == "WantsMountsFor":
            removable.update({"/tmp", "/var/tmp"})
        if name in ("Wants", "After") and mount_topology is not None:
            paths = set((baseline_properties.get("WantsMountsFor", "") or "").split())
            for path in paths:
                removable.update(mount_topology.get(path, ()))
    removed = baseline - candidate
    added = candidate - baseline
    if not removed <= removable or added:
        return baseline, candidate
    return baseline - removed, candidate


def private_tmp_mount_topology(mountpoints: set[str]) -> dict[str, tuple[str, ...]]:
    """Map each PrivateTmp path to all actual mount-unit ancestors, outermost first."""
    result = {}
    for path in ("/tmp", "/var/tmp"):
        ancestors = [point for point in mountpoints
                     if point == "/" or path == point or path.startswith(point.rstrip("/") + "/")]
        if not ancestors:
            continue
        unit_names = []
        for mountpoint in sorted(ancestors, key=lambda item: len(pathlib.PurePosixPath(item).parts)):
            if mountpoint == "/":
                unit_names.append("-.mount")
            else:
                escaped = mountpoint.strip("/").replace("-", "\\x2d").replace("/", "-")
                unit_names.append(f"{escaped}.mount")
        result[path] = tuple(unit_names)
    return result


def private_tmp_host_mount_topology() -> dict[str, tuple[str, ...]] | None:
    """Read actual mountpoints; None explicitly means the comparison is inconclusive."""
    try:
        mountpoints = set()
        for line in pathlib.Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) < 6:
                continue
            mountpoint = fields[4]
            for sequence, character in (("\\040", " "), ("\\011", "\t"),
                                        ("\\012", "\n"), ("\\134", "\\")):
                mountpoint = mountpoint.replace(sequence, character)
            mountpoints.add(mountpoint)
        return private_tmp_mount_topology(mountpoints)
    except (OSError, UnicodeError):
        return None


def run_fresh_cursor_attempt(capture_cursor, start, *, previous_cursor: str | None = None):
    """Capture a global journal boundary immediately before each start."""
    cursor = capture_cursor()
    require(bool(cursor), "journal cursor unavailable; refusing diagnostic start")
    if previous_cursor is not None:
        require(cursor != previous_cursor,
                "preserved RuntimeDirectory restart did not use a fresh journal cursor")
    return cursor, start()


def run_attempt_with_path_snapshot(capture_paths, capture_cursor, start,
                                   *, previous_cursor: str | None = None):
    """Capture host paths before the cursor/start pair for one systemd attempt."""
    paths = capture_paths()
    cursor, result = run_fresh_cursor_attempt(
        capture_cursor, start, previous_cursor=previous_cursor)
    return paths, cursor, result


def normalize_systemd_exec(value: str) -> str:
    """Remove invocation-dependent fields from systemctl's Exec* serialization."""
    dynamic = {"start_time", "stop_time", "pid", "code", "status"}
    parts = []
    for item in value.split(";"):
        item = item.strip()
        key, separator, _value = item.partition("=")
        if separator and key.strip() in dynamic:
            continue
        if item:
            parts.append(item)
    return " ; ".join(parts)


def runtime_directory_identity(info: os.stat_result, *, expected_uid: int | None = None,
                               expected_gid: int | None = None,
                               expected_mode: int | None = None) -> dict[str, int]:
    require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode),
            "runtime path identity is not a directory")
    identity = {"device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid,
                "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode)}
    expected = (("uid", expected_uid), ("gid", expected_gid), ("mode", expected_mode))
    for name, value in expected:
        if value is not None:
            require(identity[name] == value,
                    f"runtime directory identity does not have expected service {name}: {identity}")
    return identity


def capture_runtime_identities(paths, *, expected_uid: int | None = None,
                               expected_gid: int | None = None,
                               expected_mode: int | None = None) -> dict[str, dict[str, int]]:
    return {str(path): runtime_directory_identity(
        os.lstat(path), expected_uid=expected_uid, expected_gid=expected_gid,
        expected_mode=expected_mode) for path in paths}


def verify_runtime_identities(expected: dict[str, dict[str, int]]) -> None:
    for raw, identity in expected.items():
        actual = runtime_directory_identity(os.lstat(raw))
        require(actual == identity,
                f"preserving changed retained runtime directory identity: {raw}")


def run_preserved_runtime_lifecycle(paths, *, first_start_healthy: bool,
                                    stop, restart, expected_uid: int | None = None,
                                    expected_gid: int | None = None,
                                    expected_mode: int | None = None) -> str:
    """Exercise a healthy preserve stop/restart while retaining the first identity baseline."""
    if not first_start_healthy:
        return "NOT_EXERCISED"
    expected = capture_runtime_identities(
        paths, expected_uid=expected_uid, expected_gid=expected_gid,
        expected_mode=expected_mode)  # Must precede the first stop.
    stop()
    verify_runtime_identities(expected)
    healthy = restart()  # This callback verifies fresh-cursor start, active/running and config.
    require(healthy is True, "preserved RuntimeDirectory restart was not verified healthy")
    verify_runtime_identities(expected)
    stop()
    verify_runtime_identities(expected)
    return "EXERCISED"


def cleanup_runtime_before_evidence(runtime_cleanup, evidence_cleanup) -> None:
    """Do not destroy unit/drop-in diagnostics until retained-path cleanup succeeds."""
    runtime_cleanup()
    evidence_cleanup()


def finalize_disposable_units(*, units: tuple[str, ...], stop_units, verify_stopped,
                              reset_failed, runtime_cleanup, evidence_cleanup, daemon_reload) -> None:
    """Stop units, clean retained paths, reset failed state, then remove evidence."""
    stop_units(units)
    for name in units:
        verify_stopped(name)
    # Identity-checked runtime cleanup is safe once every unit is stopped and
    # prevents a failed reset from leaving RuntimeDirectoryPreserve paths behind.
    runtime_cleanup()
    reset_failed(units)
    for name in units:
        verify_stopped(name)
    evidence_cleanup()
    daemon_reload()


def remove_runtime_directories(expected: dict[str, dict[str, int]]) -> None:
    """Preflight exact identities and emptiness before removing any retained path."""
    present = []
    for raw, identity in expected.items():
        path = pathlib.Path(raw)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        actual = runtime_directory_identity(info)
        require(actual == identity,
                f"preserving runtime directory identity mismatch: {path}")
        require(not any(path.iterdir()), f"preserving non-empty runtime directory: {path}")
        present.append(path)
    for path in present:
        path.rmdir()


def runtime_cleanup_script(root: pathlib.Path,
                           expected: dict[str, dict[str, int]], *, remove: bool = True) -> str:
    """Generate the exact privileged cleanup script; root is an explicit safety boundary."""
    require(root.is_absolute(), "runtime cleanup root must be absolute")
    require(all(pathlib.PurePosixPath(name).name == name and name not in ("", ".", "..")
                for name in expected), "runtime cleanup identities must be direct children of root")
    payload = json.dumps(expected, sort_keys=True, separators=(",", ":"))
    return textwrap.dedent("""\
        import json, os, pathlib, stat
        root = pathlib.Path(ROOT)
        expected = json.loads(EXPECTED)
        remove = []
        for relative, identity in expected.items():
            path = root / relative
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                continue
            actual = {'device': info.st_dev, 'inode': info.st_ino, 'uid': info.st_uid,
                      'gid': info.st_gid, 'mode': stat.S_IMODE(info.st_mode)}
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or actual != identity):
                raise SystemExit('preserving changed or foreign runtime directory identity mismatch')
            if any(path.iterdir()):
                raise SystemExit('preserving non-empty runtime directory')
            remove.append(path)
        REMOVE_PATHS
        """).replace("ROOT", repr(str(root))).replace(
            "EXPECTED", repr(payload)).replace(
                "REMOVE_PATHS", "for path in remove:\n    path.rmdir()" if remove else "pass")


def runtime_directory_preflight_script(root: pathlib.Path,
                                       expected: dict[str, dict[str, int]]) -> str:
    return runtime_cleanup_script(root, expected, remove=False)


def production_workspace_reproduction_config(template: str, *, user: str, group: str,
                                               vm_unit: str, workspace: pathlib.Path,
                                               destination: str) -> tuple[str, str]:
    """Render the disposable service from production template plus its generated drop-in."""
    substitutions = (
        ("ExecStart=+/usr/bin/python3 -I /usr/lib/little-mester-vm/system-vm.py locked-service workspace --owner=@LM_VM_OWNER@",
         f"ExecStart={workspace_bind_probe_exec(workspace, destination)}"),
        ("@LM_VM_OWNER@", user), ("@LM_VM_OWNER_GROUP@", group),
        ("PartOf=little-mester-vm.service", f"PartOf={vm_unit}"),
        ("Before=little-mester-vm.service", f"Before={vm_unit}"),
        ("ExecStartPost=/usr/bin/python3 -I /usr/lib/little-mester-vm/system-vm-workspace.py socket-permissions",
         "ExecStartPost=/usr/bin/true"),
    )
    for original, replacement in substitutions:
        require(template.count(original) == 1,
                f"production workspace unit template changed unexpectedly: {original}")
        template = template.replace(original, replacement)
    bind_directive = WORKSPACE_MODULE._systemd_bind_path(workspace)
    production_destination = str(WORKSPACE_MODULE.SHARE_DIR)
    if destination != production_destination:
        require(bind_directive.count(production_destination) == 1,
                "production BindPaths writer output has ambiguous destination")
        bind_directive = bind_directive.replace(production_destination, destination, 1)
    return template, bind_directive


def unit_executable_from_text(unit_text: str, property_name: str) -> str:
    """Return the exact executable selected by one generated systemd command."""
    matches = re.findall(rf"^{re.escape(property_name)}=(.*)$", unit_text, flags=re.MULTILINE)
    require(len(matches) == 1, f"generated unit must contain exactly one {property_name}")
    try:
        argv = shlex.split(matches[0], posix=True)
    except ValueError as error:
        raise RuntimeError(f"generated {property_name} command has invalid quoting") from error
    require(bool(argv) and argv[0].startswith("/"),
            f"generated {property_name} executable must be absolute")
    return argv[0]


def workspace_bind_probe_exec(workspace: pathlib.Path, destination: str) -> str:
    """Build a disposable in-namespace probe as one systemd-quoted argv item."""
    home = pwd.getpwuid(os.getuid()).pw_dir
    try:
        group_gid = grp.getgrnam("lm-vm-control").gr_gid
    except KeyError:
        # Local unit tests do not install the CI-only control group. The hosted
        # reproduction requires it through the production unit properties.
        group_gid = os.getgid()
    probe = textwrap.dedent(f"""\
        import errno, json, os, pathlib, sys, time
        target = pathlib.Path({destination!r})
        unrelated_home = pathlib.Path({home!r})
        expected_uid, expected_gid, expected_group = {os.getuid()}, {os.getgid()}, {group_gid}
        target_info = os.stat(target, follow_symlinks=False)
        mounts = pathlib.Path('/proc/self/mountinfo').read_text().splitlines()
        mountpoints = set()
        for line in mounts:
            fields = line.split()
            if len(fields) > 4:
                point = fields[4]
                for sequence, character in ((chr(92) + '040', ' '),
                                            (chr(92) + '011', chr(9)),
                                            (chr(92) + '012', chr(10)),
                                            (chr(92) + '134', chr(92))):
                    point = point.replace(sequence, character)
                mountpoints.add(point)
        try:
            os.stat(unrelated_home)
            home_hidden = False
        except OSError as error:
            home_hidden = error.errno in (errno.EACCES, errno.EPERM, errno.ENOENT)
        identity = {{'uid': os.getuid(), 'euid': os.geteuid(), 'gid': os.getgid(),
                    'egid': os.getegid(), 'groups': os.getgroups()}}
        evidence = {{
            'source': {{'path': {str(workspace)!r}}},
            'target': {{'device': target_info.st_dev, 'inode': target_info.st_ino,
                        'uid': target_info.st_uid, 'gid': target_info.st_gid}},
            'exact_destination_mountpoint': str(target) in mountpoints,
            'unrelated_home_hidden': home_hidden,
            'service_identity_matches': identity['euid'] == expected_uid
                and identity['egid'] == expected_gid and expected_group in identity['groups'],
        }}
        print('WORKSPACE_FILE_BIND_PROBE ' + json.dumps(
            {{'identity': identity, 'evidence': evidence}}, sort_keys=True), flush=True)
        # Home visibility is policy-dependent: production/quote controls require
        # it hidden, while the one-variable ProtectHome=no case expects visible.
        valid = all((evidence['exact_destination_mountpoint'],
                     evidence['service_identity_matches']))
        if not valid:
            raise SystemExit(43)
        time.sleep(90)
    """)
    encoded = base64.b64encode(probe.encode("utf-8")).decode("ascii")
    return f"/usr/bin/python3 -I -c 'import base64;exec(base64.b64decode(\"{encoded}\"))'"


def workspace_bind_probe_evidence(journal: str) -> dict | None:
    marker = "WORKSPACE_FILE_BIND_PROBE "
    for line in journal.splitlines():
        index = line.find(marker)
        if index >= 0:
            try:
                return json.loads(line[index + len(marker):])
            except json.JSONDecodeError:
                return {"invalid_json": line[index + len(marker):]}
    return None


def wait_for_workspace_bind_probe(capture_journal, capture_state, *, timeout: float = 6.0,
                                 interval: float = 0.1, clock=time.monotonic,
                                 sleep=time.sleep) -> tuple[subprocess.CompletedProcess, str]:
    """Poll a fresh-cursor journal until the probe, namespace failure, stop, or deadline."""
    deadline = clock() + timeout
    while True:
        result = capture_journal()
        if result.returncode != 0:
            return result, "JOURNAL_UNAVAILABLE"
        output = result.stdout or ""
        if workspace_bind_probe_evidence(output) is not None:
            return result, "PROBE_OBSERVED"
        if ("226/NAMESPACE" in output
                or "Failed to set up mount namespacing" in output):
            return result, "NAMESPACE_FAILURE_BEFORE_PROBE"
        state = capture_state()
        if (state.get("ActiveState") in ("inactive", "failed")
                and state.get("MainPID") == "0"):
            return result, "UNIT_STOPPED_BEFORE_PROBE"
        remaining = deadline - clock()
        if remaining <= 0:
            return result, "PROBE_DEADLINE_EXPIRED"
        sleep(min(interval, remaining))


def validate_workspace_bind_probe(probe: dict | None, source_before: dict, *,
                                  expected_home_hidden: bool = True) -> tuple[dict | None, bool]:
    """Compare namespace target identity with the host source snapshot captured before start."""
    if not probe or not isinstance(probe.get("evidence"), dict):
        return probe, False
    evidence = probe["evidence"]
    source_identity = source_before.get("lstat", {})
    target = evidence.get("target", {})
    identity_matches = (
        source_identity.get("kind") == "directory"
        and target.get("device") == source_identity.get("device")
        and target.get("inode") == source_identity.get("inode"))
    result = {**probe, "host_comparison": {
        "source_device": source_identity.get("device"),
        "source_inode": source_identity.get("inode"),
        "target_matches_prestart_source": identity_matches,
        "expected_unrelated_home_hidden": expected_home_hidden,
    }}
    valid = (identity_matches
             and evidence.get("exact_destination_mountpoint") is True
             and evidence.get("unrelated_home_hidden") is expected_home_hidden
             and evidence.get("service_identity_matches") is True)
    return result, valid


def production_workspace_fragment_config(unit_text: str, bind_directive: str) -> str:
    """Place the same generated BindPaths in Service instead of a drop-in."""
    require(unit_text.count("[Service]\n") == 1 and bind_directive.count("BindPaths=") == 1,
            "workspace fragment comparison input is ambiguous")
    require("BindPaths=" not in unit_text,
            "drop-in baseline unexpectedly already contains BindPaths")
    return unit_text.replace("[Service]\n", "[Service]\n" + bind_directive)


def production_workspace_diagnostic_variants() -> dict[str, str]:
    """Return one-setting drop-in probes; all leave the production BindPaths intact."""
    return {
        "no-exec-paths": "NoExecPaths=\n",
        "exec-paths": "ExecPaths=\n",
        "protect-control-groups": "ProtectControlGroups=no\n",
        "protect-home": "ProtectHome=no\n",
        "protect-system": "ProtectSystem=no\n",
        "read-write-paths": "ReadWritePaths=\n",
        "private-tmp": "PrivateTmp=no\n",
        "runtime-directory-preserve": "RuntimeDirectoryPreserve=yes\n",
    }


def disposable_final_unit_names(workspace_unit: str, fragment_unit: str,
                                graph_workspace_unit: str, proxy_unit: str,
                                metrics_unit: str, vm_unit: str,
                                quote_comparison_units: tuple[str, ...],
                                variant_units: dict[str, str]) -> tuple[str, ...]:
    """Return every disposable unit file that final cleanup removes, exactly once."""
    return (workspace_unit, graph_workspace_unit, fragment_unit, proxy_unit,
            metrics_unit, vm_unit,
            *quote_comparison_units,
            *tuple(variant_units.values()))


def merged_workspace_dropin_matches(output: str, exit_code: int, dropin: str) -> bool:
    """Require successful `systemctl cat` evidence for the exact generated drop-in."""
    return exit_code == 0 and bool(dropin.strip()) and dropin.strip() in output


def run_production_dependency_reproduction(workspace: pathlib.Path) -> None:
    """Reproduce the production workspace unit and parent dependency graph with stubs."""
    token = secrets.token_hex(6)
    prefix = f"lm-vm-workspace-repro-{token}"
    workspace_unit = f"{prefix}.service"
    graph_workspace_unit = f"{prefix}-graph-workspace.service"
    unquoted_workspace_unit = f"{prefix}-unquoted.service"
    quoted_reverse_workspace_unit = f"{prefix}-quoted-reverse.service"
    quote_comparison_units = (unquoted_workspace_unit, quoted_reverse_workspace_unit)
    fragment_unit = f"{prefix}-fragment.service"
    proxy_unit = f"{prefix}-proxy.service"
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
    workspace_text, workspace_dropin = production_workspace_reproduction_config(
        workspace_template, user=user, group=group, vm_unit=vm_unit,
        workspace=workspace, destination=destination)
    require(workspace_dropin.count("[Service]\n") == 1,
            "generated production BindPaths drop-in has ambiguous service section")
    bind_directive = workspace_dropin.replace("[Service]\n", "", 1)
    require(bind_directive.startswith("BindPaths=") and not bind_directive.startswith('BindPaths="'),
            "production writer must emit one unquoted BindPaths item")
    # Preserve a minimal legacy-quoted control so future regressions retain the
    # causal contrast. Production candidates always use the actual writer.
    legacy_source = str(workspace).replace("%", "%%")
    legacy_quoted_dropin = f'[Service]\nBindPaths="{legacy_source}:{destination}"\n'
    quoted_reverse_dropin = legacy_quoted_dropin
    unquoted_dropin = workspace_dropin
    fragment_text = production_workspace_fragment_config(workspace_text, bind_directive)
    variants = production_workspace_diagnostic_variants()
    variant_units = {label: f"{prefix}-{label}.service" for label in variants}
    final_unit_names = disposable_final_unit_names(
        workspace_unit, fragment_unit, graph_workspace_unit, proxy_unit, metrics_unit,
        vm_unit, quote_comparison_units, variant_units)
    variant_dropins = {label: f"[Service]\n{directive}" for label, directive in variants.items()}
    dropin_relative = f"{workspace_unit}.d/20-workspace.conf"
    dropin_directory = pathlib.Path("/run/systemd/system") / f"{workspace_unit}.d"
    require(not dropin_directory.exists() and not dropin_directory.is_symlink(),
            f"preserving pre-existing disposable unit drop-in directory: {dropin_directory}")
    units = {
        workspace_unit: workspace_text,
        graph_workspace_unit: workspace_text,
        unquoted_workspace_unit: workspace_text,
        quoted_reverse_workspace_unit: workspace_text,
        fragment_unit: fragment_text,
        proxy_unit: f"""[Unit]\nDescription=Disposable production proxy dependency\nAfter=network-online.target\nWants=network-online.target\nBefore={vm_unit}\nPartOf={vm_unit}\n\n[Service]\nType=exec\nUser={user}\nGroup={group}\nExecStart=/usr/bin/sleep 90\nRestart=no\n""",
        metrics_unit: f"""[Unit]\nDescription=Disposable production metrics dependency\nBefore={vm_unit}\nPartOf={vm_unit}\n\n[Service]\nType=exec\nUser={user}\nGroup={group}\nExecStart=/usr/bin/sleep 90\nRestart=no\n""",
        vm_unit: f"""[Unit]\nDescription=Disposable parent dependency graph\nRequires={proxy_unit} {graph_workspace_unit} {metrics_unit}\nBindsTo={proxy_unit} {graph_workspace_unit} {metrics_unit}\nAfter={proxy_unit} {graph_workspace_unit} {metrics_unit}\n\n[Service]\nType=exec\nUser={user}\nGroup={group}\nRuntimeDirectory=little-mester-vm\nRuntimeDirectoryMode=0700\nExecStart=/usr/bin/sleep 90\nRestart=no\n""",
    }
    for label, name in variant_units.items():
        units[name] = workspace_text
    dropins = {
        dropin_relative: legacy_quoted_dropin,
        f"{graph_workspace_unit}.d/20-workspace.conf": workspace_dropin,
        f"{unquoted_workspace_unit}.d/20-workspace.conf": unquoted_dropin,
        f"{quoted_reverse_workspace_unit}.d/20-workspace.conf": quoted_reverse_dropin,
    }
    for label, name in variant_units.items():
        dropins[f"{name}.d/20-workspace.conf"] = workspace_dropin
        dropins[f"{name}.d/30-{label}.conf"] = variant_dropins[label]
    payload = json.dumps({"units": units, "dropins": dropins},
                         sort_keys=True, separators=(",", ":"))
    writer = textwrap.dedent("""\
        import json, os, pathlib, stat, sys
        root = pathlib.Path('/run/systemd/system')
        payload = json.loads(sys.stdin.read())
        units, dropins = payload['units'], payload['dropins']
        if not root.is_dir():
            raise SystemExit('system unit directory unavailable')
        created = []
        created_dirs = []
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
            for relative, text in dropins.items():
                parts = pathlib.PurePosixPath(relative).parts
                if (len(parts) != 2 or not parts[0].startswith('lm-vm-workspace-repro-')
                        or not parts[0].endswith('.service.d')
                        or not (parts[1] == '20-workspace.conf'
                                or parts[1].startswith('30-') and parts[1].endswith('.conf'))):
                    raise SystemExit('invalid disposable unit drop-in path')
                directory = root / parts[0]
                if directory not in created_dirs:
                    directory.mkdir(mode=0o755)
                    created_dirs.append(directory)
                path = directory / parts[1]
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
                created.append((path, text.encode()))
                try:
                    data = memoryview(text.encode())
                    while data:
                        written = os.write(fd, data)
                        if written <= 0:
                            raise OSError('drop-in write made no progress')
                        data = data[written:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except BaseException:
            for path, expected in reversed(created):
                try:
                    info = os.lstat(path)
                    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                        continue
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    try:
                        actual = os.read(fd, len(expected) + 1)
                    finally:
                        os.close(fd)
                    if actual == expected:
                        path.unlink()
                except OSError:
                    pass
            for directory in reversed(created_dirs):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            raise
        """)
    create = subprocess.run(["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-c", writer],
                            input=payload, text=True, capture_output=True,
                            timeout=10, check=False)
    require(create.returncode == 0, "could not safely install disposable systemd reproduction units")
    cleanup_authorized = False
    try:
        run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "daemon-reload"], timeout=15)
        properties = ("LoadState,ActiveState,SubState,MainPID,Result,ExecMainCode,ExecMainStatus,"
                      "Type,User,Group,"
                      "SupplementaryGroups,RuntimeDirectory,RuntimeDirectoryMode,UMask,"
                      "RuntimeDirectoryPreserve,ExecStart,ExecStartPost,Restart,KillMode,"
                      "TimeoutStartUSec,TimeoutStopUSec,"
        "NoNewPrivileges,NoExecPaths,ExecPaths,PrivateTmp,ProtectHome,"
        "ProtectSystem,ProtectControlGroups,ReadWritePaths,BindPaths,PartOf,"
        "Before,After,Requires,Wants,BindsTo,Conflicts,Requisite,RequiresMountsFor,WantsMountsFor")

        config_fields = ("Type", "User", "Group", "SupplementaryGroups", "RuntimeDirectory",
                         "RuntimeDirectoryMode", "RuntimeDirectoryPreserve", "UMask", "ExecStart",
                         "ExecStartPost", "Restart", "KillMode", "TimeoutStartUSec",
                         "TimeoutStopUSec", "NoNewPrivileges", "NoExecPaths", "ExecPaths",
                         "PrivateTmp", "ProtectHome", "ProtectSystem", "ProtectControlGroups",
                         "ReadWritePaths", "BindPaths", "PartOf", "Before", "After", "Requires",
                          "Wants", "BindsTo", "Conflicts", "Requisite", "RequiresMountsFor",
                           "WantsMountsFor")
        private_tmp_topology = private_tmp_host_mount_topology()
        baseline_config: dict[str, str] | None = None
        preserve_cursor: str | None = None
        runtime_baseline = {raw: _host_path_evidence(pathlib.Path(raw)) for raw in runtime_paths}
        retained_runtime: dict[str, dict[str, int]] = {}

        def unit_state(name: str) -> dict[str, str]:
            result = run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "show", "--no-pager",
                          "--property=ActiveState", "--property=MainPID", name],
                         check=False, timeout=10)
            return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

        def verify_stopped(name: str) -> None:
            state = unit_state(name)
            require(state.get("ActiveState") in ("inactive", "failed")
                    and state.get("MainPID") == "0",
                    f"preserving ambiguous disposable unit state after stop: {name} {state}")

        def verify_runtime_baseline(*, retained_allowed: bool = False) -> None:
            for raw in runtime_paths:
                current = _host_path_evidence(pathlib.Path(raw))
                if retained_allowed and raw in retained_runtime:
                    entry = current.get("lstat", {})
                    actual = {key: entry.get(key) for key in retained_runtime[raw]}
                    require(actual == retained_runtime[raw],
                            f"preserving changed retained runtime directory: {current}")
                else:
                    require(current == runtime_baseline[raw],
                            f"runtime path did not return to expected baseline: {current}")

        def run_case(label: str, start_unit: str, effective_unit: str,
                     expected_unit_text: str, expected_dropins: list[str] | None,
                     *, allowed_setting: str | None = None,
                     expected_negative_quoted_bind: bool = False,
                     fragment_bind_placement: bool = False,
                     allow_retained: bool = False,
                     keep_active: bool = False) -> dict[str, str]:
            nonlocal baseline_config, preserve_cursor
            verify_runtime_baseline(retained_allowed=allow_retained)
            # Capture both bind endpoints and their parents immediately before each start.
            prestart = {
                "source": _host_path_evidence(workspace),
                "destination": _host_path_evidence(pathlib.Path(destination)),
                "runtime_directories": [_host_path_evidence(pathlib.Path(raw))
                                        for raw in runtime_paths],
            }
            def capture_cursor() -> str:
                result = run(journal_cursor_command(), check=False, timeout=10)
                return parse_journal_cursor(result.stdout or "", result.returncode)

            cursor, start = run_fresh_cursor_attempt(
                capture_cursor,
                lambda: run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "start", start_unit],
                            timeout=35, check=False),
                previous_cursor=(preserve_cursor
                                 if label == "runtime-directory-preserve-restart" else None))
            if label == "runtime-directory-preserve":
                preserve_cursor = cursor
            shown = run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "show", "--no-pager",
                         f"--property={properties}", effective_unit], check=False, timeout=10)
            require(shown.returncode == 0,
                    f"effective systemd properties unavailable for {label}; preserving evidence")
            state = dict(line.split("=", 1) for line in shown.stdout.splitlines() if "=" in line)
            require(state.get("LoadState") == "loaded",
                    f"diagnostic unit did not load for {label}; preserving evidence: {state}")
            journal, probe_collection_status = wait_for_workspace_bind_probe(
                lambda: run(journal_after_cursor_command(cursor, effective_unit),
                            check=False, timeout=10),
                lambda: unit_state(effective_unit))
            refreshed = run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "show", "--no-pager",
                             f"--property={properties}", effective_unit],
                            check=False, timeout=10)
            require(refreshed.returncode == 0,
                    f"post-probe systemd properties unavailable for {label}; preserving evidence")
            state = dict(line.split("=", 1) for line in refreshed.stdout.splitlines() if "=" in line)
            require(state.get("LoadState") == "loaded",
                    f"diagnostic unit disappeared after probe collection for {label}: {state}")
            merged = run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "cat", "--no-pager",
                          effective_unit], check=False, timeout=10)
            merged_text = merged.stdout or ""
            effective_matches = (merged.returncode == 0
                                 and expected_unit_text.strip() in merged_text
                                 and all(dropin.strip() in merged_text
                                         for dropin in (expected_dropins or [])))
            require(journal.returncode == 0,
                    f"current-attempt journal unavailable for {label}; preserving evidence")
            diagnostic = journal.stdout or ""
            probe_evidence, probe_valid = validate_workspace_bind_probe(
                workspace_bind_probe_evidence(diagnostic), prestart["source"],
                expected_home_hidden=state.get("ProtectHome") != "no")
            config = {key: state.get(key, "") for key in config_fields}
            config_mismatches = compare_effective_configuration(
                baseline_config if baseline_config is not None else config, config,
                allowed_setting=allowed_setting,
                allow_bind_fragment_placement=fragment_bind_placement,
                mount_topology=private_tmp_topology,
                expected_bind=(str(workspace), destination))
            if baseline_config is None:
                baseline_config = config
            merged_configuration_matches = effective_matches
            effective_matches = effective_matches and not config_mismatches
            raw_classification = classify_namespace_reproduction(
                state, diagnostic, start_exit=start.returncode,
                merged_dropin_matches=merged_configuration_matches)
            classification = raw_classification
            expected_negative_control = (
                expected_negative_quoted_bind
                and expected_quoted_bind_negative_control(
                    config_mismatches,
                    merged_configuration_matches=merged_configuration_matches,
                    raw_classification=raw_classification,
                    start_exit=start.returncode))
            if expected_negative_control:
                classification = "EXPECTED_NEGATIVE_QUOTED_BIND_226_NAMESPACE"
            elif config_mismatches:
                classification = "INCONCLUSIVE"
            bind_semantics_only_unresolved = (
                bool(config_mismatches) and set(config_mismatches) == {"BindPaths"})
            if raw_classification == "NOT_REPRODUCED" and not probe_valid:
                classification = "INCONCLUSIVE"
            expected_outcome = None
            if label == "exec-paths":
                # Clearing ExecPaths removes Python from the executable allowlist;
                # NoExecPaths=/ remains in force, so systemd must deny ExecStart.
                require(state.get("ExecPaths", "") == ""
                        and state.get("ExecMainStatus") == "203"
                        and start.returncode != 0,
                        "ExecPaths removal did not produce the expected 203/EXEC denial")
                expected_outcome = "EXPECTED_203_EXEC_NOEXEC_PATHS_REMAINS"
            print("workspace_bind_case=" + json.dumps({
                "case": label, "start_unit": start_unit, "effective_unit": effective_unit,
                "start_exit": start.returncode, "start_stderr": (start.stderr or "")[-1600:],
                "properties_exit": shown.returncode, "properties": state,
                "journal_cursor": cursor, "journal_exit": journal.returncode,
                "journal": (journal.stdout or "")[-5000:],
                "bind_probe_collection": probe_collection_status,
                "merged_unit_exit": merged.returncode, "merged_unit": merged_text[-6000:],
                "effective_bind_configuration_matches": effective_matches,
                "merged_unit_configuration_matches": merged_configuration_matches,
                "configuration_mismatches": config_mismatches,
                "configuration_comparison": (
                    "EXPECTED_NEGATIVE_BINDPATHS_MALFORMED" if expected_negative_control else
                    "INVALID_INCONCLUSIVE" if config_mismatches else "MATCH"),
                "private_tmp_mount_topology": (private_tmp_topology if private_tmp_topology is not None
                                                else "UNAVAILABLE; generated mount-edge comparison is INCONCLUSIVE"),
                "classification": classify_namespace_diagnostic(classification, diagnostic),
                "raw_namespace_evidence_classification": raw_classification,
                "expected_negative_control": expected_negative_control,
                "expected_outcome": expected_outcome,
                "in_namespace_bind_probe": probe_evidence or {
                    "status": "NOT_REACHED_BEFORE_NAMESPACE_FAILURE_OR_PROBE_UNAVAILABLE"},
                "in_namespace_bind_probe_valid": probe_valid,
                "execution_failure_note": ("ExecPaths execution denial is an execution failure, not a namespace pass"
                                           if classify_namespace_diagnostic(classification, diagnostic)
                                           == "EXECUTION_FAILURE_INCONCLUSIVE" else None),
                "host_before_start": prestart,
                "namespace_metadata_limit": (
                    "host path metadata is logged immediately before start; when namespace setup "
                    "fails before ExecStart, path metadata inside the namespace is unavailable"),
                "host_after_start": {
                    "source": _host_path_evidence(workspace),
                    "destination": _host_path_evidence(pathlib.Path(destination)),
                    "runtime_directories": [_host_path_evidence(pathlib.Path(raw))
                                            for raw in runtime_paths],
                },
            }, sort_keys=True, separators=(",", ":")), flush=True)
            lifetime_healthy = (
                label in ("runtime-directory-preserve", "runtime-directory-preserve-restart")
                and start.returncode == 0 and merged_configuration_matches
                and (not config_mismatches or bind_semantics_only_unresolved)
                and raw_classification == "NOT_REPRODUCED" and probe_valid
                and state.get("ActiveState") == "active"
                and state.get("SubState") == "running"
                and int(state.get("MainPID", "0") or "0") > 1)
            if label == "runtime-directory-preserve" and lifetime_healthy:
                # Capture the exact first healthy identity before systemd stops the service.
                for raw in runtime_paths[:2]:
                    retained_runtime[raw] = runtime_directory_identity(
                        os.lstat(raw), expected_uid=os.getuid(), expected_gid=os.getgid(),
                        expected_mode=0o750)
            elif label == "runtime-directory-preserve-restart":
                require(lifetime_healthy,
                        "preserved RuntimeDirectory restart did not verify active/running and exact config")
                verify_runtime_identities(retained_runtime)

            if keep_active:
                require(lifetime_healthy,
                        "preserved RuntimeDirectory restart did not remain healthy")
                return {**state, "_classification": classification,
                        "_raw_classification": raw_classification,
                        "_configuration_mismatches": config_mismatches,
                        "_probe_evidence": probe_evidence,
                        "_probe_valid": probe_valid, "_lifetime_healthy": "yes"}

            if label == "runtime-directory-preserve" and lifetime_healthy:
                def stop_preserved_service():
                    stopped = run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", start_unit],
                                  timeout=15, check=False)
                    post_stop_state = unit_state(start_unit)
                    require(stopped.returncode == 0 or post_stop_state.get("ActiveState") in
                            ("inactive", "failed"),
                            "ambiguous RuntimeDirectoryPreserve stop; preserving evidence")
                    verify_stopped(start_unit)

                def restart_preserved_service():
                    restarted = run_case(
                        "runtime-directory-preserve-restart", start_unit, effective_unit,
                        expected_unit_text, expected_dropins,
                        allowed_setting=allowed_setting, allow_retained=True, keep_active=True)
                    return restarted.get("_lifetime_healthy") == "yes"

                lifecycle = run_preserved_runtime_lifecycle(
                    runtime_paths[:2], first_start_healthy=True,
                    stop=stop_preserved_service, restart=restart_preserved_service,
                    expected_uid=os.getuid(), expected_gid=os.getgid(),
                    expected_mode=0o750)
                print("workspace_bind_case_lifecycle=" + json.dumps({
                    "case": label, "lifetime_probe": lifecycle,
                    "runtime_directories_after_stop": [
                        _host_path_evidence(pathlib.Path(raw)) for raw in runtime_paths[:2]],
                }, sort_keys=True, separators=(",", ":")), flush=True)
                return {**state, "_lifetime_healthy": "yes"}

            stopped = run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", start_unit],
                          timeout=15, check=False)
            post_stop_state = unit_state(start_unit)
            require(stopped.returncode == 0 or post_stop_state.get("ActiveState") in
                    ("inactive", "failed"),
                    f"ambiguous stop result for {label}; preserving units and evidence")
            verify_stopped(start_unit)
            if label == "runtime-directory-preserve" and lifetime_healthy:
                verify_runtime_identities(retained_runtime)
            elif label == "runtime-directory-preserve-restart":
                verify_runtime_identities(retained_runtime)
            after_stop = ([_host_path_evidence(pathlib.Path(raw)) for raw in runtime_paths]
                          if label != "runtime-directory-preserve" or lifetime_healthy else None)
            if label == "runtime-directory-preserve":
                if not lifetime_healthy:
                    print("workspace_bind_case_lifecycle=" + json.dumps({
                        "case": label, "lifetime_probe": "NOT_EXERCISED",
                        "reason": "first start was not verified healthy with runtime directories present",
                    }, sort_keys=True, separators=(",", ":")), flush=True)
                    require(effective_matches or (merged_configuration_matches
                                                  and bind_semantics_only_unresolved),
                            f"effective systemd configuration invalid for {label}; preserving units/evidence")
                    return {**state, "_classification": classification,
                            "_raw_classification": raw_classification,
                            "_configuration_mismatches": config_mismatches,
                            "_probe_evidence": probe_evidence,
                            "_probe_valid": probe_valid, "_lifetime_healthy": "no"}
            else:
                if label != "runtime-directory-preserve-restart":
                    verify_runtime_baseline()
            print("workspace_bind_case_lifecycle=" + json.dumps({
                "case": label, "stop_exit": stopped.returncode,
                "runtime_directories_after_stop": after_stop,
                "runtime_directory_lifetime_note": (
                    "RuntimeDirectoryPreserve=yes intentionally retains service runtime paths"
                    if label == "runtime-directory-preserve" else None),
            }, sort_keys=True, separators=(",", ":")), flush=True)
            require(effective_matches or (merged_configuration_matches
                                          and bind_semantics_only_unresolved),
                    f"effective systemd configuration invalid for {label}; preserving units/evidence")
            return {**state, "_classification": classification,
                    "_raw_classification": raw_classification,
                    "_configuration_mismatches": config_mismatches,
                    "_probe_evidence": probe_evidence,
                    "_probe_valid": probe_valid,
                    "_lifetime_healthy": "yes" if lifetime_healthy else "no"}
        # The graph uses a separate, identically rendered workspace unit so
        # failed-state cleanup is not needed between independent attempts.
        # Establish the baseline from the exact writer-produced configuration.
        # Quoted legacy syntax is an explicit expected-negative regression case,
        # never the baseline against which production-equivalent cases are judged.
        unquoted = run_case("bind-unquoted", unquoted_workspace_unit,
                            unquoted_workspace_unit, workspace_text, [unquoted_dropin])
        require(unquoted["_raw_classification"] == "NOT_REPRODUCED"
                and unquoted["_probe_valid"],
                "production-equivalent unquoted BindPaths did not prove identity and isolation")
        standalone = run_case("dropin-standalone", workspace_unit, workspace_unit,
                              workspace_text, [legacy_quoted_dropin],
                              allowed_setting="BindPaths",
                              expected_negative_quoted_bind=True)
        # run_case returns only after effective configuration, stop, and
        # runtime-baseline checks pass. A reproduced 226 is the expected
        # diagnostic control outcome, not an acceptance pass or matrix abort.
        require(standalone["_classification"] == "EXPECTED_NEGATIVE_QUOTED_BIND_226_NAMESPACE"
                and standalone["_raw_classification"] == "REPRODUCED_226_NAMESPACE"
                and set(standalone["_configuration_mismatches"]) == {"BindPaths"},
                "standalone quoted control did not match its explicit malformed-BindPaths negative expectation")

        quoted_reverse = run_case("bind-quoted-reverse", quoted_reverse_workspace_unit,
                                  quoted_reverse_workspace_unit, workspace_text,
                                  [quoted_reverse_dropin], allowed_setting="BindPaths",
                                  expected_negative_quoted_bind=True)
        unquoted_worked = (unquoted["_raw_classification"] == "NOT_REPRODUCED"
                           and unquoted["_probe_valid"])
        quote_comparison = {
            "quoted_initial": standalone["_classification"],
            "quoted_initial_raw_namespace_evidence": standalone["_raw_classification"],
            "unquoted": unquoted["_classification"],
            "unquoted_raw_namespace_evidence": unquoted["_raw_classification"],
            "unquoted_in_namespace_bind_probe": unquoted["_probe_evidence"],
            "unquoted_in_namespace_bind_probe_valid": unquoted["_probe_valid"],
            "quoted_reverse": quoted_reverse["_classification"],
            "quoted_reverse_raw_namespace_evidence": quoted_reverse["_raw_classification"],
            "same_existing_source_destination_and_service_identity": True,
            "reverse_confirmation_required": unquoted_worked,
        }
        print("workspace_bind_quote_comparison=" + json.dumps(
            quote_comparison, sort_keys=True, separators=(",", ":")), flush=True)
        if unquoted_worked:
            require(unquoted["_probe_valid"],
                    "unquoted active service did not prove actual bind identity, mountpoint and home isolation")
            require(quoted_reverse["_classification"]
                    == "EXPECTED_NEGATIVE_QUOTED_BIND_226_NAMESPACE"
                    and quoted_reverse["_raw_classification"] == standalone["_raw_classification"]
                    and set(quoted_reverse["_configuration_mismatches"]) == {"BindPaths"},
                    "quoted reverse comparison did not reproduce initial quoted outcome")

        graph_prestart, graph_cursor, start = run_attempt_with_path_snapshot(
            lambda: {
                "source": _host_path_evidence(workspace),
                "destination": _host_path_evidence(pathlib.Path(destination)),
                "runtime_directories": [_host_path_evidence(pathlib.Path(raw))
                                        for raw in runtime_paths],
            },
            lambda: parse_journal_cursor(
                (result := run(journal_cursor_command(), check=False, timeout=10)).stdout or "",
                result.returncode),
            lambda: run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "start", vm_unit],
                        timeout=35, check=False))
        states = {}
        for name in units:
            result = run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "show", "--no-pager",
                          f"--property={properties}", name], check=False, timeout=10)
            require(result.returncode == 0,
                    f"parent-graph systemd properties unavailable for {name}; preserving evidence")
            states[name] = dict(line.split("=", 1) for line in result.stdout.splitlines()
                                if "=" in line)
            require(states[name].get("LoadState") == "loaded",
                    f"parent-graph unit did not load: {name} {states[name]}")
        journal, graph_probe_collection_status = wait_for_workspace_bind_probe(
            lambda: run(journal_after_cursor_command(graph_cursor, graph_workspace_unit),
                        check=False, timeout=10),
            lambda: unit_state(graph_workspace_unit))
        graph_refreshed = run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "show", "--no-pager",
                               f"--property={properties}", graph_workspace_unit],
                              check=False, timeout=10)
        require(graph_refreshed.returncode == 0,
                "post-probe parent-graph properties unavailable; preserving evidence")
        states[graph_workspace_unit] = dict(
            line.split("=", 1) for line in graph_refreshed.stdout.splitlines() if "=" in line)
        require(states[graph_workspace_unit].get("LoadState") == "loaded",
                "parent-graph workspace unit disappeared after probe collection; preserving evidence")
        merged = run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "cat", "--no-pager",
                      graph_workspace_unit], check=False, timeout=10)
        workspace_state = states[graph_workspace_unit]
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
                           ("PartOf", vm_unit), ("Before", vm_unit),
                           ("After", "local-fs.target")):
            if value not in workspace_state.get(key, ""):
                mismatches[key] = {"expected_contains": value,
                                    "actual": workspace_state.get(key)}
        if not WORKSPACE_MODULE.effective_bind_paths_match(
                workspace_state.get("BindPaths", ""), workspace, destination):
            mismatches["BindPaths"] = {
                "expected_single_recursive_bind": [str(workspace), destination, "rbind"],
                "actual": workspace_state.get("BindPaths"),
            }
        for key in ("ExecStart", "ExecStartPost"):
            executable = unit_executable_from_text(workspace_text, key)
            if f"path={executable}" not in workspace_state.get(key, ""):
                mismatches[key] = {"expected_executable": executable,
                                   "actual": workspace_state.get(key)}
        for key in ("Requires", "BindsTo", "After"):
            for dependency in (proxy_unit, graph_workspace_unit, metrics_unit):
                if dependency not in states[vm_unit].get(key, ""):
                    mismatches[f"vm.{key}.{dependency}"] = {
                        "expected_contains": dependency,
                        "actual": states[vm_unit].get(key),
                    }
        print("workspace_bind_dependency_configuration="
              + ("PASS" if not mismatches else "MISMATCH "
                 + json.dumps(mismatches, sort_keys=True, separators=(",", ":"))), flush=True)
        merged_dropin_ok = merged_workspace_dropin_matches(
            merged.stdout or "", merged.returncode, workspace_dropin)
        require(journal.returncode == 0,
                "current-attempt parent-graph journal unavailable; preserving evidence")
        graph_config = {key: workspace_state.get(key, "") for key in config_fields}
        graph_config_mismatches = compare_effective_configuration(
            baseline_config or {}, graph_config, mount_topology=private_tmp_topology,
            expected_bind=(str(workspace), destination))
        graph_diagnostic = journal.stdout or ""
        graph_merged_unit_matches = merged_dropin_ok
        graph_probe_evidence, graph_probe_valid = validate_workspace_bind_probe(
            workspace_bind_probe_evidence(graph_diagnostic), graph_prestart["source"])
        graph_raw_classification = classify_namespace_reproduction(
            workspace_state, graph_diagnostic, start_exit=start.returncode,
            merged_dropin_matches=graph_merged_unit_matches)
        graph_classification = graph_raw_classification
        if graph_config_mismatches or mismatches:
            graph_classification = "INCONCLUSIVE"
        if graph_raw_classification == "NOT_REPRODUCED" and not graph_probe_valid:
            graph_classification = "INCONCLUSIVE"
        merged_dropin_ok = graph_merged_unit_matches and not graph_config_mismatches
        print("workspace_bind_case=" + json.dumps({
            "case": "dropin-parent-graph", "start_unit": vm_unit,
            "effective_unit": graph_workspace_unit, "start_exit": start.returncode,
            "journal_cursor": graph_cursor,
            "start_stderr": (start.stderr or "")[-1600:],
            "units": states, "properties": workspace_state,
            "configuration_mismatches": {**mismatches, **graph_config_mismatches},
            "configuration_comparison": "INVALID_INCONCLUSIVE"
            if graph_config_mismatches or mismatches else "MATCH",
            "private_tmp_mount_topology": (private_tmp_topology if private_tmp_topology is not None
                                            else "UNAVAILABLE; generated mount-edge comparison is INCONCLUSIVE"),
            "journal": (journal.stdout or "")[-5000:],
            "bind_probe_collection": graph_probe_collection_status,
            "merged_unit_exit": merged.returncode, "merged_unit": (merged.stdout or "")[-6000:],
            "effective_bind_configuration_matches": merged_dropin_ok,
            "merged_unit_configuration_matches": graph_merged_unit_matches,
            "raw_namespace_evidence_classification": graph_raw_classification,
            "in_namespace_bind_probe": graph_probe_evidence or {
                "status": "NOT_REACHED_BEFORE_NAMESPACE_FAILURE_OR_PROBE_UNAVAILABLE"},
            "in_namespace_bind_probe_valid": graph_probe_valid,
            "classification": graph_classification, "host_before_start": graph_prestart,
            "namespace_metadata_limit": (
                "host path metadata is logged immediately before start; when namespace setup "
                "fails before ExecStart, path metadata inside the namespace is unavailable"),
            "host_after_start": {
                "source": _host_path_evidence(workspace),
                "destination": _host_path_evidence(pathlib.Path(destination)),
                "runtime_directories": [_host_path_evidence(pathlib.Path(raw))
                                        for raw in runtime_paths],
            },
        }, sort_keys=True, separators=(",", ":")), flush=True)
        print(f"workspace_bind_case=dropin-parent-graph classification={graph_classification} "
              f"effective_unit={graph_workspace_unit}", flush=True)
        graph_stop = run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", vm_unit,
                          graph_workspace_unit, proxy_unit, metrics_unit], timeout=30, check=False)
        require(graph_stop.returncode == 0 or all(
            unit_state(name).get("ActiveState") in ("inactive", "failed")
            for name in (vm_unit, graph_workspace_unit, proxy_unit, metrics_unit)),
            "ambiguous parent-graph stop; preserving units and evidence")
        for name in (vm_unit, graph_workspace_unit, proxy_unit, metrics_unit):
            verify_stopped(name)
        verify_runtime_baseline()
        require(graph_merged_unit_matches,
                "systemd merged unit mismatch; preserving units/evidence")
        require(not (set(graph_config_mismatches) - {"BindPaths"}),
                "parent-graph systemd properties have unrelated configuration mismatches")
        require(not mismatches,
                "disposable dependency configuration mismatch; preserving units/evidence")
        run_case(
            "fragment-standalone", fragment_unit, fragment_unit, fragment_text, [],
            fragment_bind_placement=True)
        lifetime_result = {"_lifetime_healthy": "no"}
        for label, name in variant_units.items():
            result = run_case(label, name, name, workspace_text,
                              [workspace_dropin, variant_dropins[label]],
                              allowed_setting={
                                  "no-exec-paths": "NoExecPaths", "exec-paths": "ExecPaths",
                                  "protect-control-groups": "ProtectControlGroups",
                                  "protect-home": "ProtectHome", "protect-system": "ProtectSystem",
                                  "read-write-paths": "ReadWritePaths", "private-tmp": "PrivateTmp",
                                  "runtime-directory-preserve": "RuntimeDirectoryPreserve",
                              }[label])
            if label == "runtime-directory-preserve":
                lifetime_result = result
        if lifetime_result.get("_lifetime_healthy") != "yes":
            print("workspace_bind_case_lifecycle=" + json.dumps({
                "case": "runtime-directory-preserve-restart",
                "lifetime_probe": "NOT_EXERCISED",
            }, sort_keys=True, separators=(",", ":")), flush=True)
        verify_runtime_baseline(retained_allowed=True)
        cleanup_authorized = True
    except BaseException as error:
        if not cleanup_authorized:
            print("workspace_bind_reproduction_cleanup=SKIPPED preserving units and evidence; "
                  f"original_error={type(error).__name__}: {error}", flush=True)
        raise
    finally:
        if not cleanup_authorized:
            # Preserve diagnostics on an incomplete matrix. Re-raise an
            # active failure unchanged; use the generic guard only if the
            # matrix ended without an exception for an unexpected reason.
            if sys.exc_info()[0] is None:
                raise RuntimeError("diagnostic reproduction incomplete; preserving units and evidence")
            raise
        remover = textwrap.dedent("""\
            import json, os, pathlib, stat, sys
            root = pathlib.Path('/run/systemd/system')
            payload = json.loads(sys.stdin.read())
            units, dropins = payload['units'], payload['dropins']
            remove_files = []
            for name, text in units.items():
                path = root / name
                info = os.lstat(path)
                if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                        or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o644):
                    raise SystemExit('preserving changed disposable unit file')
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    actual = os.read(fd, len(text.encode()) + 1)
                finally:
                    os.close(fd)
                if actual != text.encode():
                    raise SystemExit('preserving changed disposable unit file')
                remove_files.append(path)
            dropin_dirs = set()
            expected_by_directory = {}
            for relative, text in dropins.items():
                path = root / relative
                directory_info = os.lstat(path.parent)
                if (not stat.S_ISDIR(directory_info.st_mode) or stat.S_ISLNK(directory_info.st_mode)
                        or directory_info.st_uid != 0
                        or stat.S_IMODE(directory_info.st_mode) != 0o755):
                    raise SystemExit('preserving changed disposable unit drop-in directory')
                expected_by_directory.setdefault(path.parent, set()).add(path.name)
                info = os.lstat(path)
                if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                        or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o644):
                    raise SystemExit('preserving changed disposable unit drop-in')
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    actual = os.read(fd, len(text.encode()) + 1)
                finally:
                    os.close(fd)
                if actual != text.encode():
                    raise SystemExit('preserving changed disposable unit drop-in')
                remove_files.append(path)
                dropin_dirs.add(path.parent)
            for directory, expected_names in expected_by_directory.items():
                if set(os.listdir(directory)) != expected_names:
                    raise SystemExit('preserving unexpected disposable unit drop-in evidence')
            for path in remove_files:
                os.unlink(path)
            for directory in dropin_dirs:
                directory.rmdir()
            """)
        runtime_relative = {pathlib.Path(raw).name: identity
                            for raw, identity in retained_runtime.items()}

        def cleanup_runtime():
            # First preflight every retained directory without deleting anything.
            preflight = runtime_directory_preflight_script(pathlib.Path("/run"), runtime_relative)
            checked = subprocess.run(["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-c",
                                      preflight], text=True, capture_output=True,
                                     timeout=10, check=False)
            require(checked.returncode == 0,
                    "preserving unexpected retained RuntimeDirectory evidence")
            # The exact destructive helper repeats the complete preflight atomically
            # before removing any path; unit/drop-in diagnostics still exist here.
            runtime_cleanup = runtime_cleanup_script(pathlib.Path("/run"), runtime_relative)
            cleaned = subprocess.run(["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-c",
                                      runtime_cleanup], text=True, capture_output=True,
                                     timeout=10, check=False)
            require(cleaned.returncode == 0,
                    "preserving unexpected retained RuntimeDirectory evidence")

        def remove_unit_evidence():
            removed = subprocess.run(
                ["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-c", remover],
                input=payload, text=True, capture_output=True, timeout=10, check=False)
            require(removed.returncode == 0,
                    "preserving changed or unavailable disposable unit files")

        def stop_final_units(names):
            result = run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", *names],
                         timeout=30, check=False)
            require(result.returncode == 0 or all(
                unit_state(name).get("ActiveState") in ("inactive", "failed")
                for name in names),
                "ambiguous final disposable-unit stop; preserving units and evidence")

        def verify_final_stopped(name):
            state = unit_state(name)
            require(state.get("ActiveState") in ("inactive", "failed")
                    and state.get("MainPID") == "0",
                    f"preserving disposable systemd units that did not stop: {name} {state}")

        def reset_final_failed(names):
            reset_failed_with_diagnostics(names, phase="final-cleanup")

        finalize_disposable_units(
            units=final_unit_names,
            stop_units=stop_final_units, verify_stopped=verify_final_stopped,
            reset_failed=reset_final_failed, runtime_cleanup=cleanup_runtime,
            evidence_cleanup=remove_unit_evidence,
            daemon_reload=lambda: run(["/usr/bin/sudo", "-n", "/usr/bin/systemctl",
                                       "daemon-reload"], timeout=15))
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
        for marker, value, count in ((b"@LM_VM_OWNER@", owner.pw_name, 2),
                                     (b"@LM_VM_OWNER_GROUP@", grp.getgrgid(owner.pw_gid).gr_name, 1)):
            require(payload.count(marker) == count,
                    "pinned workspace unit identity marker is ambiguous")
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
    print("production_unit_cleanup=PASS")
    return 0


def _validate_diagnostic_groupadd() -> None:
    policy_path = REPO / "lib/vm/executable-policy.py"
    spec = importlib.util.spec_from_file_location("diagnostic_executable_policy", policy_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("trusted groupadd executable policy is unavailable")
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    try:
        policy.validate_executable("/usr/sbin/groupadd")
    except (OSError, ValueError) as error:
        raise RuntimeError("trusted /usr/sbin/groupadd is unavailable or unsafe") from error


def _ensure_diagnostic_control_group() -> None:
    """Create only the disposable diagnostic's fixed supplementary group."""
    create_status = None
    try:
        group = grp.getgrnam("lm-vm-control")
    except KeyError:
        _validate_diagnostic_groupadd()
        try:
            result = subprocess.run(
                ["/usr/bin/sudo", "-n", "/usr/sbin/groupadd", "--system", "lm-vm-control"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError("could not create diagnostic control group with noninteractive sudo") from error
        create_status = result.returncode
        try:
            group = grp.getgrnam("lm-vm-control")
        except KeyError as error:
            raise RuntimeError(
                f"diagnostic control group creation failed (status {create_status}); NSS entry is absent"
            ) from error
    if group.gr_name != "lm-vm-control" or not isinstance(group.gr_gid, int) or group.gr_gid <= 0:
        raise RuntimeError("diagnostic control group has unsafe gid or name")
    try:
        reverse = grp.getgrgid(group.gr_gid)
    except KeyError as error:
        raise RuntimeError("diagnostic control group GID does not resolve through NSS") from error
    if reverse.gr_name != "lm-vm-control":
        raise RuntimeError("diagnostic control group NSS name/GID mismatch")
    # A nonzero groupadd result is accepted only as a concurrent-create race:
    # the coherent NSS entry just validated is the required evidence.


def diagnostic_lock_fixture_script(action: str, *, parent: pathlib.Path,
                                   group_gid: int, owner_uid: int,
                                   expected: dict | None = None) -> str:
    """Build the narrowly scoped lock-fixture operation for the disposable diagnostic."""
    require(action in ("create", "cleanup"), "invalid diagnostic lock fixture action")
    require(parent.is_absolute() and ".." not in parent.parts,
            "diagnostic lock fixture parent must be an absolute normalized path")
    if action == "cleanup":
        require(isinstance(expected, dict), "cleanup requires captured fixture identities")
    payload = json.dumps(expected, sort_keys=True, separators=(",", ":"))
    return textwrap.dedent(f"""\
        import fcntl, json, os, pathlib, stat
        parent = pathlib.Path({str(parent)!r})
        group_gid = {int(group_gid)!r}
        owner_uid = {int(owner_uid)!r}
        action = {action!r}
        expected = json.loads({payload!r})
        name = 'code-update.lock'

        def facts(info):
            return {{'device': info.st_dev, 'inode': info.st_ino, 'uid': info.st_uid,
                    'gid': info.st_gid, 'mode': stat.S_IMODE(info.st_mode)}}

        def validate_parent(info):
            if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_uid != owner_uid or info.st_gid != group_gid
                    or stat.S_IMODE(info.st_mode) != 0o750):
                raise RuntimeError('diagnostic lock fixture parent identity or metadata changed')

        def validate_lock(info):
            if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_uid != owner_uid or info.st_gid != group_gid
                    or stat.S_IMODE(info.st_mode) != 0o660 or info.st_nlink != 1
                    or info.st_size != 0):
                raise RuntimeError('diagnostic lock fixture file is unsafe or changed')

        if action == 'create':
            if parent.exists() or parent.is_symlink():
                raise RuntimeError('preserving pre-existing diagnostic lock fixture path')
            os.mkdir(parent, 0o750)
            parent_created = os.lstat(parent)
            lock_created = False
            lock_identity = None
            try:
                os.chown(parent, owner_uid, group_gid, follow_symlinks=False)
                os.chmod(parent, 0o750, follow_symlinks=False)
                parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    pinfo = os.fstat(parent_fd)
                    validate_parent(pinfo)
                    lock_fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL |
                                      os.O_NOFOLLOW | os.O_CLOEXEC, 0o660, dir_fd=parent_fd)
                    lock_created = True
                    try:
                        lock_identity = os.fstat(lock_fd)
                        os.fchown(lock_fd, owner_uid, group_gid)
                        os.fchmod(lock_fd, 0o660)
                        linfo = os.fstat(lock_fd)
                        validate_lock(linfo)
                        result = {{'parent': facts(pinfo), 'lock': facts(linfo)}}
                    finally:
                        os.close(lock_fd)
                finally:
                    os.close(parent_fd)
            except BaseException:
                # Partial setup is removed only when both objects still match
                # identities created by this invocation; otherwise preserve it.
                try:
                    now_parent = os.lstat(parent)
                    if (stat.S_ISDIR(now_parent.st_mode) and not stat.S_ISLNK(now_parent.st_mode)
                            and (now_parent.st_dev, now_parent.st_ino) ==
                                (parent_created.st_dev, parent_created.st_ino)
                            and now_parent.st_uid == owner_uid and now_parent.st_gid == group_gid
                            and stat.S_IMODE(now_parent.st_mode) == 0o750):
                        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                        try:
                            if lock_created and lock_identity is not None:
                                try:
                                    current = os.stat(name, dir_fd=fd, follow_symlinks=False)
                                    if ((current.st_dev, current.st_ino) ==
                                            (lock_identity.st_dev, lock_identity.st_ino)
                                            and stat.S_ISREG(current.st_mode)
                                            and current.st_nlink == 1 and current.st_size == 0
                                            and current.st_uid == owner_uid
                                            and current.st_gid == group_gid
                                            and stat.S_IMODE(current.st_mode) == 0o660):
                                        os.unlink(name, dir_fd=fd)
                                except FileNotFoundError:
                                    pass
                        finally:
                            os.close(fd)
                        if not any(parent.iterdir()):
                            os.rmdir(parent)
                except OSError:
                    pass
                raise
            print(json.dumps(result, sort_keys=True, separators=(',', ':')))
        else:
            if set(expected) != {{'parent', 'lock'}}:
                raise RuntimeError('diagnostic lock fixture identity record is malformed')
            parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                pinfo = os.fstat(parent_fd)
                validate_parent(pinfo)
                if facts(pinfo) != expected['parent']:
                    raise RuntimeError('diagnostic lock fixture parent identity mismatch')
                entries = os.listdir(parent_fd)
                if entries != [name]:
                    raise RuntimeError('preserving unexpected diagnostic lock fixture contents')
                lock_fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
                try:
                    linfo = os.fstat(lock_fd)
                    validate_lock(linfo)
                    if facts(linfo) != expected['lock']:
                        raise RuntimeError('diagnostic lock fixture file identity mismatch')
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError as error:
                        raise RuntimeError('diagnostic code-update lock is still held') from error
                    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    if facts(current) != expected['lock']:
                        raise RuntimeError('diagnostic lock fixture changed before unlink')
                    os.unlink(name, dir_fd=parent_fd)
                finally:
                    os.close(lock_fd)
                if os.listdir(parent_fd):
                    raise RuntimeError('preserving unexpected diagnostic lock fixture contents')
            finally:
                os.close(parent_fd)
            current_parent = os.lstat(parent)
            if facts(current_parent) != expected['parent']:
                raise RuntimeError('diagnostic lock fixture parent changed before removal')
            os.rmdir(parent)
    """)


def _run_diagnostic_lock_fixture(action: str, *, expected: dict | None = None) -> dict:
    """Create/remove only the standalone diagnostic's protected update-lock fixture."""
    parent = pathlib.Path("/etc/little-mester-vm")
    group_gid = grp.getgrnam("lm-vm-control").gr_gid
    script = diagnostic_lock_fixture_script(
        action, parent=parent, group_gid=group_gid, owner_uid=0, expected=expected)
    result = subprocess.run(
        ["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-c", script],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=30, check=False)
    require(result.returncode == 0,
            f"diagnostic lock fixture {action} failed (exit={result.returncode}; "
            f"stderr={result.stderr[:2048]!r})")
    if action == "cleanup":
        require(not result.stdout.strip(), "diagnostic lock fixture cleanup returned unexpected output")
        return {}
    try:
        identity = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("diagnostic lock fixture returned invalid identity evidence") from error
    require(set(identity) == {"parent", "lock"}, "diagnostic lock fixture identity is incomplete")
    return identity


@contextlib.contextmanager
def _timed_ci_stage(label: str):
    spec = importlib.util.spec_from_file_location(
        "ci_system_acceptance_timing", REPO / "lib/vm/system-acceptance.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("shared system acceptance timing helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with module.StageTimer().timed_stage(label):
        yield


def namespace_diagnostics_only() -> int:
    """Run exploratory namespace controls on their own disposable CI runner."""
    hosted_runner_guard()
    production_host_preflight()
    workspace = disposable_workspace_path()
    require(not workspace.exists() and not workspace.is_symlink(),
            f"preserving pre-existing namespace-diagnostic workspace: {workspace}")
    workspace.mkdir(mode=0o700)
    created = os.lstat(workspace)
    try:
        _ensure_diagnostic_control_group()
        lock_fixture = _run_diagnostic_lock_fixture("create")
        run_workspace_bind_reproductions(workspace)
        # The reproduction returns only after its units are stopped and its
        # unit/runtime cleanup gates pass. On any earlier failure, retain the
        # lock and diagnostic artifacts for investigation on this disposable runner.
        _run_diagnostic_lock_fixture("cleanup", expected=lock_fixture)
    finally:
        current = os.lstat(workspace)
        require(stat.S_ISDIR(current.st_mode) and not stat.S_ISLNK(current.st_mode)
                and (current.st_dev, current.st_ino, current.st_uid,
                     stat.S_IMODE(current.st_mode))
                == (created.st_dev, created.st_ino, os.getuid(), 0o700)
                and not any(workspace.iterdir()),
                "preserving changed or non-empty namespace-diagnostic workspace")
        workspace.rmdir()
    print("systemd_namespace_diagnostics=PASS")
    return 0


def main() -> int:
    if sys.argv[1:] == ["--namespace-diagnostics"]:
        return namespace_diagnostics_only()
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
        with _timed_ci_stage("candidate_install"):
            run([str(REPO / "tools/vm-owner-acceptance"), "--install", "--sha", sha,
                 "--prefix", str(CI_INSTALL_PREFIX), "--repo", str(REPO)], timeout=120)
        installed_root = CI_INSTALL_PARENT / sha
        with _timed_ci_stage("fresh_provisioning"):
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
        escape_evidence = create_escape_sentinel(workspace_path)
        invoke_shared_acceptance(sha)
        verify_workspace_startup_post()
        verify_guest_cannot_read_escape()
        with _timed_ci_stage("forced_stop_recovery"):
            force_systemd_fallback(installed_root)
    except BaseException as error:  # noqa: BLE001 - finally below must stop a started unit on cancellation
        failure = error
        print(f"production_unit_result=FAIL error={type(error).__name__}: {error}", file=sys.stderr)
        if start_attempted:
            report_startup_diagnostics()
    finally:
        if start_attempted and installed_root is not None:
            try:
                with _timed_ci_stage("final_cleanup"):
                    invoke_lab(installed_root, "down", 45)
                    assert_stopped()
                    remove_escape_sentinel(escape_evidence)
                    remove_disposable_workspace()
                    print("production_unit_cleanup=PASS")
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
