#!/usr/bin/env python3
"""Repeatable owner-host acceptance for the staged VM workflow.

Keep this runner after merge: its bounded nested-KVM check can be repeated after
launcher/provisioning changes. ``preflight`` and ``nested`` reconcile only the
known acceptance snapshot and create an outer profile only when needed; prior
snapshot data is retained as a private backup. The ordered ``campaign`` instead
requires already-running named outer/child profiles, then uses one unique
disposable profile/workspace for human auth/permission and workload/lifecycle
gates. It never cleans or mutates the preserved profiles. Human observations
are never inferred from automated probes.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import errno
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from queue import Empty, Queue
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
LAB_VM = ROOT / "lab-vm"
SNAPSHOT_FILES = (
    "lab-vm", "lib/state.sh", "lib/vm/proxy.py", "lib/vm/runner.sh",
    "lib/vm/host-services.sh", "lib/vm/lifecycle.sh", "lib/vm/recursion.sh",
    "scripts/vm-context.py", "scripts/vm-extra.sh", "scripts/vm-guest-isolation-check.py",
    "scripts/vm-install-tools.sh", "scripts/vm-recursion-live-children.py",
    "scripts/vm-recursion-workspace.py", "scripts/vm-update.sh",
    "tests/test_vm_context.py", "tests/test_vm_recursion_live_children.py",
)
DEFAULT_OUTER_PROFILE = "launcher-smoke"
DEFAULT_SNAPSHOT = Path.home() / ".cache/lab-vm-recursion/initial-launcher-smoke/workspace"
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
VERSION_RE = re.compile(r"^v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
REMOTE_PROGRESS_MARKER = "__LM_VM_ACCEPTANCE_PROGRESS__"
REMOTE_SUMMARY_MARKER = "__LM_VM_ACCEPTANCE__"
REMOTE_NESTED_TIMEOUT_SECONDS = 1800
REMOTE_PROGRESS_STAGES = {
    "remote_script", "runtime_overlay", "child_workspace_source", "child_vm_up", "child_control_state",
    "child_cloud_init_status", "child_context", "child_findmnt",
    "child_workspace_boundary", "child_source_parity", "child_recursive_self_tests",
    "child_readiness", "depth_cap_refusal",
    "outer_continuity", "outer_ssh_startup", "cloud-init status",
    "lab-vm context --json", "findmnt /workspace",
    "child SSH or remote readiness probe",
    "child SSH transport or remote script startup",
}
REMOTE_PROGRESS_STATUSES = {"started", "completed", "failed", "timed_out", "waiting", "interrupted"}

# This is the complete host-only contract for one campaign PASS.  These are
# scoped evidence slices, not blanket ticket closures; see docs/vm-host-acceptance.md.
CAMPAIGN_GATE_COVERAGE: dict[str, dict[str, object]] = {
    "host_prerequisites": {
        "tickets": {"#41": ["A11", "A15"]},
        "scope": "host tool/distro/package-map/default-no-install behavior; no host package installation",
    },
    "clean_provisioning": {
        "tickets": {"#41": ["A1", "A10", "A14", "A15"]},
        "scope": "fresh isolated base image and depth-0 guest provisioning/readiness; does not claim CPU-only-host support",
    },
    "existing_nested_boundary": {
        "tickets": {"#41": ["A4"], "#43": ["A3", "A4", "A7"]},
        "scope": "the named existing outer/child read-only continuation and its implemented representative network probes",
    },
    "depth_two_recursion": {
        "tickets": {
            "#41": ["A14"], "#42": ["A5"], "#43": ["A5", "A6", "A7", "A8"], "#52": ["A6"],
        },
        "scope": "disposable depth 0→1→2 boot, persisted policy, depth-3 pre-state refusal, child cleanup, and parent continuity",
    },
    "direct_guest_model_tui": {
        "tickets": {"#41": ["A25", "A26"], "#42": ["A1", "A6", "A8", "A9", "A10", "A11"]},
        "scope": "both configured local model paths plus human direct-guest completion/TUI/metrics/exit observations",
    },
    "native_auth": {
        "tickets": {"#45": ["A12", "A13", "A14", "A15", "A16"]},
        "scope": "the host-browser-bridge callback path, auth-list/provider completion, human cleanup checks and disposable-VM export/import slices; does not claim one-approval UX, host-Docker recreation, refreshed-token acceptance or recursive credential forwarding",
    },
    "actual_permissions": {
        "tickets": {"#50": ["A4"]},
        "scope": "same-version human observed shared-service versus --standalone allow/deny behavior only; not the unsafe-recurse/auto-accept launcher case or an upstream-version diagnosis/workaround",
    },
    "guest_workload": {
        "tickets": {"#41": ["A10", "A14", "A19"], "#42": ["A2", "A3", "A4"], "#43": ["A1", "A2", "A3", "A7"]},
        "scope": "agent-controlled PostgreSQL feedback, guest-loopback port, selected-workspace bind mount, named volume, and no physical-host port exposure",
    },
    "guest_boundary": {
        "tickets": {"#41": ["A4"], "#42": ["A3"], "#43": ["A3", "A4", "A5", "A7", "A8"]},
        "scope": "disposable root/child/grandchild QEMU and representative guest-originated boundary checks; not exhaustive penetration testing",
    },
    "non_destructive_lifecycle": {
        "tickets": {"#41": ["A6", "A24"], "#43": ["A6"], "#52": ["A3"]},
        "scope": "suspend/resume state-retention slices only; does not claim the complete daily lifecycle documentation/default-launch contract",
    },
    "disposable_teardown": {
        "tickets": {"#41": ["A10", "A14"], "#43": ["A6"]},
        "scope": "explicitly approved stop/down, auth export/import, and cleanup of generated profiles/workspaces only",
    },
}

CAMPAIGN_NOT_CLAIMED = {
    "#35": ["A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9", "A10", "A11", "A12", "A13", "A14", "A15", "A16", "A17", "A18", "A19", "A20", "A21", "A22"],
    "#41": ["A2", "A3", "A5", "A7", "A8", "A9", "A12", "A13", "A16", "A17", "A18", "A20", "A21", "A22", "A23"],
    "#42": ["A7", "A12", "A13", "A14"],
    "#43": [],
    "#45": ["A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9", "A10", "A11", "A17", "A18"],
    "#50": ["A1", "A2", "A3"],
    "#52": ["A1", "A2", "A4", "A5", "A7", "A8"],
}


class AcceptanceError(RuntimeError):
    """A fail-closed preflight or evidence error."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Evidence:
    """Atomically persist a minimal report in a private user-state directory."""

    def __init__(self, command: str, scope: str) -> None:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        self.directory = base / "little-mester" / "host-acceptance"
        for directory in (base, base / "little-mester", self.directory):
            if directory.is_symlink():
                raise AcceptanceError(f"Refusing symlinked evidence directory: {directory}")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        for directory in (base / "little-mester", self.directory):
            os.chmod(directory, 0o700)
            if directory.stat().st_uid != os.getuid():
                raise AcceptanceError(f"Evidence directory is not owned by this user: {directory}")
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path = self.directory / f"vm-acceptance-{stamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}.json"
        self.data: dict[str, object] = {
            "schema_version": 1,
            "started_at": utc_now(),
            "command": command,
            "scope": scope,
            "host": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
            "checks": [],
            "observations": [],
        }
        if command == "campaign":
            self.data["campaign_gate_coverage"] = {
                gate: {**details, "result": "NOT_RUN"}
                for gate, details in CAMPAIGN_GATE_COVERAGE.items()
            }
            self.data["ticket_ac_not_claimed"] = CAMPAIGN_NOT_CLAIMED
        self.save()

    def save(self) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(self.data, output, indent=2, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def check(self, name: str, passed: bool, detail: str) -> None:
        result = "PASS" if passed else "FAIL"
        print(f"{result} {name}: {detail}")
        checks = self.data["checks"]
        assert isinstance(checks, list)
        checks.append({"name": name, "result": result, "detail": detail})
        self.save()

    def blocked(self, name: str, detail: str) -> None:
        print(f"BLOCKED {name}: {detail}")
        checks = self.data["checks"]
        assert isinstance(checks, list)
        checks.append({"name": name, "result": "BLOCKED", "detail": detail})
        self.save()

    def finish(self, outcome: str) -> None:
        self.data["finished_at"] = utc_now()
        self.data["outcome"] = outcome
        self.save()
        result = "PASS" if outcome == "PASS" else "BLOCKED" if outcome == "BLOCKED" else "FAIL" if outcome == "FAIL" else "RECORDED"
        print(f"{result} {self.data['command']}: {outcome}")
        print(f"Evidence: {self.path} (private, mode 0600)")


def record_campaign_gate(evidence: Evidence, gate: str, result: str, detail: str) -> None:
    if gate not in CAMPAIGN_GATE_COVERAGE or result not in ("PASS", "FAIL", "BLOCKED"):
        raise AcceptanceError("Invalid campaign gate result.")
    contract = evidence.data.get("campaign_gate_coverage")
    if not isinstance(contract, dict) or not isinstance(contract.get(gate), dict):
        raise AcceptanceError("Campaign gate coverage ledger is missing or invalid.")
    entry = contract[gate]
    assert isinstance(entry, dict)
    if entry.get("result") != "NOT_RUN":
        raise AcceptanceError(f"Campaign gate was recorded more than once: {gate}")
    entry["result"] = result
    entry["detail"] = detail
    if result == "BLOCKED":
        evidence.blocked(f"campaign gate: {gate}", detail)
    else:
        evidence.check(f"campaign gate: {gate}", result == "PASS", detail)
    evidence.save()


def finish_campaign(evidence: Evidence, requested_outcome: str) -> str:
    """Fail closed if any agreed host-only gate is absent or not passing."""
    contract = evidence.data.get("campaign_gate_coverage")
    if not isinstance(contract, dict):
        evidence.blocked("campaign coverage contract", "required gate-to-ticket coverage is missing")
        evidence.finish("BLOCKED")
        return "BLOCKED"

    checks = evidence.data.get("checks", [])
    if not isinstance(checks, list):
        checks = []
        evidence.data["checks"] = checks
    for gate in CAMPAIGN_GATE_COVERAGE:
        entry = contract.get(gate)
        if isinstance(entry, dict) and entry.get("result") == "NOT_RUN":
            entry["result"] = "BLOCKED"
            entry["detail"] = "not reached; the campaign did not complete the required gate"
            evidence.blocked(f"campaign gate: {gate}", "not reached; the campaign did not complete the required gate")

    incomplete = [
        gate for gate in CAMPAIGN_GATE_COVERAGE
        if not isinstance(contract.get(gate), dict) or contract[gate].get("result") != "PASS"
    ]
    has_failure = any(isinstance(item, dict) and item.get("result") == "FAIL" for item in checks)
    has_block = any(isinstance(item, dict) and item.get("result") == "BLOCKED" for item in checks)
    if requested_outcome == "PASS" and (incomplete or has_failure or has_block):
        if has_failure:
            requested_outcome = "FAIL"
        else:
            requested_outcome = "BLOCKED"
    elif has_failure:
        requested_outcome = "FAIL"
    elif has_block and requested_outcome == "PASS":
        requested_outcome = "BLOCKED"
    evidence.data["campaign_gate_coverage_complete"] = not incomplete
    evidence.data["campaign_gate_coverage_missing"] = incomplete
    evidence.finish(requested_outcome)
    return requested_outcome


def run(args: list[str], *, env: dict[str, str] | None = None, cwd: Path = ROOT,
        capture: bool = True, input_text: str | None = None,
        no_input: bool = False, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=cwd, env=env, input=input_text, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        stdin=subprocess.DEVNULL if no_input and input_text is None else None,
        timeout=timeout,
        check=False,
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def active_source_overlay_payload(repo: Path = ROOT) -> dict[str, dict[str, object]]:
    """Return the narrow, integrity-checked runtime files needed for an active snapshot overlay."""
    payload: dict[str, dict[str, object]] = {}
    root = repo.resolve(strict=True)
    for relative in SNAPSHOT_FILES:
        source = repo / relative
        if (path_has_symlink(source) or not source.is_file()
                or not source.resolve(strict=True).is_relative_to(root)):
            raise AcceptanceError(f"Unsafe or missing active-snapshot runtime source: {relative}")
        data = source.read_bytes()
        payload[relative] = {
            "content": base64.b64encode(data).decode("ascii"),
            "mode": stat.S_IMODE(source.stat().st_mode),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    return payload


def safe_snapshot_target(snapshot: Path, target: Path) -> bool:
    try:
        relative = target.relative_to(snapshot)
        resolved_root = snapshot.resolve()
        if not target.resolve().is_relative_to(resolved_root):
            return False
    except (OSError, ValueError):
        return False
    current = snapshot
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    return True


def safe_context(raw: str) -> dict[str, object] | None:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    runtime, recursion, kvm = value.get("runtime"), value.get("recursion"), value.get("kvm")
    if not all(isinstance(part, dict) for part in (runtime, recursion, kvm)):
        return None
    cpu = kvm.get("cpu_virtualization", [])
    return {
        "runtime_kind": runtime.get("kind"),
        "depth": recursion.get("depth"),
        "max_depth": recursion.get("max_depth"),
        "child_depth_available": recursion.get("child_depth_available"),
        "device_present": kvm.get("device_present"),
        "device_read_write": kvm.get("device_read_write"),
        "nested_kvm_preflight_ready": kvm.get("nested_kvm_preflight_ready"),
        "cpu_virtualization": sorted(x for x in cpu if x in ("vmx", "svm")) if isinstance(cpu, list) else [],
        "workspace_filesystem": value.get("workspace_filesystem"),
    }


def parse_json_object(raw: str) -> dict[str, object] | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(raw[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def parse_isolation_result(output: str, returncode: int) -> dict[str, object]:
    """Parse only named counts and required boundary markers; never retain raw output."""
    passed_lines = len(re.findall(r"^PASS:", output, re.MULTILINE))
    failed_lines = len(re.findall(r"^FAIL:", output, re.MULTILINE))
    skipped_lines = len(re.findall(r"^SKIP:", output, re.MULTILINE))
    result = re.search(r"^Result: (\d+) passed, (\d+) failed$", output, re.MULTILINE)
    summary_matches = bool(
        result and int(result.group(1)) == passed_lines
        and int(result.group(2)) == failed_lines
    )
    qemu_validated = (
        "QEMU configuration: restrict=on; SSH bound to 127.0.0.1:" in output
        and "; only the four reviewed guestfwd channels are configured." in output
    )
    listeners_validated = "QEMU TCP listeners: SSH and egress proxy are loopback-only." in output
    model_skipped = output.count(
        "SKIP: selected model availability (not part of isolation-only acceptance)"
    ) == 1
    passed = (
        returncode == 0 and qemu_validated and listeners_validated
        and summary_matches and passed_lines > 0 and failed_lines == 0
        and skipped_lines == 1 and model_skipped
    )
    return {
        "returncode": returncode,
        "qemu_argv_validated": qemu_validated,
        "loopback_listeners_validated": listeners_validated,
        "result_summary_parsed": summary_matches,
        "model_check_skipped": model_skipped,
        "passed_checks": passed_lines,
        "failed_checks": failed_lines,
        "skipped_checks": skipped_lines,
        "passed": passed,
    }


def run_isolation_check(
    profile: str,
    evidence: Evidence,
    label: str,
    repo: Path = ROOT,
) -> bool:
    """Run read-only QEMU/listener and guest network checks for one live profile."""
    if not PROFILE_RE.fullmatch(profile):
        evidence.check(f"{label} QEMU and guest network boundary", False, "invalid VM profile name")
        return False
    env = os.environ.copy()
    env["VM_ISOLATION_SKIP_MODEL"] = "1"
    timed_out = False
    try:
        completed = subprocess.run(
            [str(repo / "lab-vm"), "verify-isolation", profile],
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
        parsed = parse_isolation_result(completed.stdout, completed.returncode)
    except subprocess.TimeoutExpired as error:
        raw_output = error.stdout or ""
        if isinstance(raw_output, bytes):
            raw_output = raw_output.decode("utf-8", "replace")
        parsed = parse_isolation_result(raw_output, 124)
        timed_out = True
        parsed["passed"] = False
    except OSError as error:
        parsed = parse_isolation_result("", 127)
        parsed["error_category"] = type(error).__name__
    parsed["timed_out"] = timed_out
    evidence.data[f"{label}_isolation"] = parsed
    evidence.save()
    passed = parsed["passed"] is True
    if passed:
        detail = (
            f"QEMU arguments and loopback listeners passed; "
            f"{parsed['passed_checks']} guest probes passed; model check explicitly skipped"
        )
    elif timed_out:
        detail = "isolation command timed out; preserve VM state and inspect its private diagnostics"
    else:
        detail = (
            f"boundary failed or output was incomplete; {parsed['failed_checks']} guest probes failed; "
            "preserve VM state and inspect diagnostics"
        )
    evidence.check(f"{label} QEMU and guest network boundary", passed, detail)
    return passed


def profile_base(repo: Path = ROOT) -> Path:
    # Match lifecycle.sh: an empty override falls back, and checkout-local
    # runtime state is never implicitly selected.
    if "VM_DIR_BASE" in os.environ:
        # Match lifecycle.sh's explicit override selection by variable presence.
        # An explicitly empty profile root consequently resolves as /<profile>.
        return Path(os.environ["VM_DIR_BASE"] or "/")
    xdg_data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    state_dir = Path(
        os.environ.get("VM_STATE_DIR")
        or os.environ.get("MESTER_STATE_DIR")
        or str(Path(xdg_data_home) / "little-mester")
    )
    return state_dir / "vm"


def profile_dir(name: str, repo: Path = ROOT) -> Path:
    if not PROFILE_RE.fullmatch(name):
        raise AcceptanceError("Profile names must start with a letter or digit and use only letters, digits, dot, underscore, or hyphen (max 64 characters).")
    return profile_base(repo) / name


def process_matches_profile(vm_dir: Path) -> bool:
    pid_path = vm_dir / "qemu.pid"
    if pid_path.is_symlink() or not pid_path.is_file():
        return False
    try:
        pid = int(pid_path.read_text(encoding="ascii").strip())
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except (OSError, ValueError):
        return False
    return "qemu-system-x86_64" in command and str(vm_dir / "disk.qcow2") in command


def outer_control_files_safe(directory: Path) -> bool:
    port_file, key_file, known_file = (directory / name for name in ("ssh_port", "id_vm_control", "known_hosts"))
    if any(path.is_symlink() or not path.is_file() for path in (port_file, key_file, known_file)):
        return False
    try:
        port = port_file.read_text(encoding="ascii").strip()
        key_mode = stat.S_IMODE(key_file.stat().st_mode)
    except (OSError, UnicodeError):
        return False
    return port.isdecimal() and 1 <= int(port) <= 65535 and not (key_mode & 0o077)


def host_context(evidence: Evidence) -> dict[str, object] | None:
    result = run([str(LAB_VM), "context", "--json"])
    context = safe_context(result.stdout) if result.returncode == 0 else None
    evidence.check("host context", context is not None, "lab-vm context is readable" if context else "context command failed or returned invalid JSON; no VM action attempted")
    if context is None:
        return None
    is_host = context["runtime_kind"] == "host"
    evidence.check("owner-host runtime", is_host, f"runtime kind is {context['runtime_kind']!r}; run on the owner KVM host, not inside a guest/container")
    kvm = (context["device_present"] is True and context["device_read_write"] is True
           and context["nested_kvm_preflight_ready"] is True and bool(context["cpu_virtualization"]))
    evidence.check("host KVM", kvm, "usable /dev/kvm and nested VMX/SVM are present" if kvm else "usable nested KVM device and VMX/SVM are required")
    return context


def host_tools(evidence: Evidence) -> bool:
    required = ("qemu-system-x86_64", "qemu-img", "ssh", "python3", "socat")
    missing = [tool for tool in required if shutil.which(tool) is None]
    virtiofs = bool(shutil.which("virtiofsd")) or any(
        Path(path).is_file() and os.access(path, os.X_OK)
        for path in ("/usr/lib/qemu/virtiofsd", "/usr/lib/qemu/qemu-virtiofsd", "/usr/libexec/virtiofsd")
    )
    if not virtiofs:
        missing.append("virtiofsd")
    if not (shutil.which("genisoimage") or shutil.which("mkisofs")):
        missing.append("genisoimage/mkisofs")
    evidence.check("host VM tools", not missing, "required tools found" if not missing else f"missing: {', '.join(missing)}")
    return not missing


def lexists(path: Path) -> bool:
    """Like exists(), but also sees dangling symlinks."""
    return path.exists() or path.is_symlink()


def path_has_symlink(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.absolute().parts[1:]:
        current /= part
        if current.is_symlink():
            return True
    return False


def snapshot_state_visible(snapshot: Path) -> bool:
    workspace = snapshot / ".workspace"
    return (
        lexists(snapshot / ".mester")
        or lexists(workspace / ".mester")
        or workspace.is_symlink()
    )


def active_snapshot_users(snapshot: Path) -> list[str]:
    """Find QEMU/virtiofsd command lines that still export this snapshot."""
    try:
        target = str(snapshot.resolve())
    except OSError:
        return ["unresolvable-snapshot"]
    users: list[str] = []
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError:
        return ["process-table-unreadable"]
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
            args = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
        except OSError:
            continue
        if not args:
            continue
        executable = Path(args[0]).name
        if (executable.startswith("qemu-system") or executable == "virtiofsd") and target in " ".join(args):
            users.append(entry.name)
    return users


def snapshot_status(snapshot: Path, repo: Path = ROOT, *, require_known_path: bool = True) -> tuple[bool, dict[str, bool]]:
    if snapshot.is_symlink() or path_has_symlink(snapshot):
        return False, {relative: False for relative in SNAPSHOT_FILES}
    expected = not require_known_path or snapshot.resolve() == DEFAULT_SNAPSHOT.resolve()
    state_excluded = not snapshot_state_visible(snapshot)
    parity: dict[str, bool] = {}
    for relative in SNAPSHOT_FILES:
        source, target = repo / relative, snapshot / relative
        safe = (source.is_file() and not source.is_symlink() and target.is_file()
                and safe_snapshot_target(snapshot, target))
        parity[relative] = bool(safe and sha256(source) == sha256(target))
    return expected and state_excluded and all(parity.values()), parity


def _backup_root(evidence: Evidence, label: str) -> Path:
    root = evidence.directory / f"{label}-{uuid.uuid4().hex}"
    root.mkdir(mode=0o700)
    return root


def sync_snapshot_sources(snapshot: Path, evidence: Evidence, repo: Path = ROOT) -> bool:
    """Replace only stale allowlisted sources, retaining old bytes privately."""
    users = active_snapshot_users(snapshot)
    if users:
        evidence.check("snapshot source repair", False, "snapshot is exported by an active VM; no files changed")
        evidence.data["snapshot_export_processes"] = users
        evidence.save()
        return False

    backup_root: Path | None = None
    changed: list[str] = []
    for relative in SNAPSHOT_FILES:
        source, target = repo / relative, snapshot / relative
        if (not source.is_file() or source.is_symlink() or not target.is_file()
                or not safe_snapshot_target(snapshot, target)):
            return False
        if sha256(source) == sha256(target):
            continue
        users = active_snapshot_users(snapshot)
        if users:
            evidence.check("snapshot source repair", False, "snapshot became active before replacement; no further files changed")
            evidence.data["snapshot_export_processes"] = users
            evidence.save()
            return False
        if backup_root is None:
            backup_root = _backup_root(evidence, "snapshot-source-backup")
            evidence.data["snapshot_source_backup"] = str(backup_root)
            evidence.save()
        backup = backup_root / relative
        backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(target, backup)
        if sha256(target) != sha256(backup):
            evidence.check("snapshot source repair", False, f"could not verify private backup of {relative}; original target retained")
            return False
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, stat.S_IMODE(source.stat().st_mode))
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        if sha256(source) != sha256(target):
            evidence.check("snapshot source repair", False, f"byte verification failed for {relative}; preserve snapshot and stop")
            return False
        changed.append(relative)
    if backup_root is not None:
        evidence.data["snapshot_source_backup"] = str(backup_root)
        evidence.save()
    evidence.check(
        "snapshot source repair", True,
        "byte-verified: " + ", ".join(changed) if changed else "all allowlisted files already match",
    )
    return True


def rebuild_safe_snapshot(snapshot: Path, evidence: Evidence, repo: Path = ROOT) -> bool:
    """Rebuild the exact stable snapshot with the recursive-workspace exclusions.

    The old tree is moved, never deleted, to a sibling backup. Rebuilding is
    refused while a QEMU/virtiofsd process is exporting the current tree.
    """
    if snapshot.resolve() != DEFAULT_SNAPSHOT.resolve() or snapshot.is_symlink() or path_has_symlink(snapshot.parent):
        evidence.check("snapshot rebuild", False, "refused non-stable or symlinked snapshot path")
        return False
    users = active_snapshot_users(snapshot)
    if users:
        evidence.check("snapshot rebuild", False, "snapshot is still exported by an active VM; no files changed")
        evidence.data["snapshot_export_processes"] = users
        evidence.save()
        return False

    parent = snapshot.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink() or parent.stat().st_uid != os.getuid():
        evidence.check("snapshot rebuild", False, "snapshot parent is symlinked or not owned by this user")
        return False
    stage_session = parent / f".vm-host-acceptance-stage-{uuid.uuid4().hex}"
    builder = repo / "scripts/vm-recursion-workspace.py"
    result = run([
        sys.executable, str(builder), "prepare", "--source", str(repo), "--session", str(stage_session),
    ], cwd=repo)
    if result.returncode != 0:
        if result.stderr:
            sys.stderr.write(result.stderr)
        evidence.check("snapshot rebuild", False, f"safe workspace builder exited {result.returncode}; old snapshot retained")
        shutil.rmtree(stage_session, ignore_errors=True)
        return False
    staged = stage_session / "workspace"
    staged_ok, _ = snapshot_status(staged, repo, require_known_path=False)
    if not staged_ok:
        shutil.rmtree(stage_session, ignore_errors=True)
        evidence.check("snapshot rebuild", False, "staged safe snapshot failed source/state validation; old snapshot retained")
        return False

    # Workspace preparation may take a while; do not replace the path if a VM
    # began exporting the current tree while the staged copy was being built.
    users = active_snapshot_users(snapshot)
    if users:
        shutil.rmtree(stage_session, ignore_errors=True)
        evidence.check("snapshot rebuild", False, "snapshot became active while a replacement was staged; old tree retained")
        evidence.data["snapshot_export_processes"] = users
        evidence.save()
        return False

    backup: Path | None = None
    if lexists(snapshot):
        if not snapshot.is_dir() or snapshot.is_symlink():
            shutil.rmtree(stage_session, ignore_errors=True)
            evidence.check("snapshot rebuild", False, "existing snapshot is not a real directory; it was not replaced")
            return False
        backup = parent / f"workspace.acceptance-backup-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        try:
            os.replace(snapshot, backup)
        except OSError:
            shutil.rmtree(stage_session, ignore_errors=True)
            evidence.check("snapshot rebuild", False, "could not preserve the existing snapshot; no replacement installed")
            return False
        evidence.data["snapshot_backup"] = str(backup)
        evidence.save()
    try:
        os.replace(staged, snapshot)
    except OSError:
        if backup is not None and not lexists(snapshot):
            os.replace(backup, snapshot)
        shutil.rmtree(stage_session, ignore_errors=True)
        evidence.check("snapshot rebuild", False, "could not install staged snapshot; previous snapshot restored when possible")
        return False
    shutil.rmtree(stage_session, ignore_errors=True)
    evidence.check("snapshot rebuild", True, "fresh safe snapshot installed; prior tree retained privately" if backup else "fresh safe snapshot installed at the known path")
    return True


def snapshot_checks(
    snapshot: Path,
    evidence: Evidence,
    repo: Path = ROOT,
    *,
    allow_active_source_overlay: bool = False,
    allow_repair: bool = True,
) -> bool:
    active_source_overlay = False
    expected = snapshot.resolve() == DEFAULT_SNAPSHOT.resolve()
    evidence.check("known snapshot path", expected, "using the stable snapshot named in the current handoff" if expected else "only the stable snapshot named in the current handoff may be repaired")
    if not expected or snapshot.is_symlink() or path_has_symlink(snapshot.parent):
        evidence.check("existing safe snapshot", False, "stable snapshot path is unsafe; no changes attempted")
        return False

    _, parity = snapshot_status(snapshot, repo)
    state_visible = snapshot_state_visible(snapshot) if snapshot.is_dir() else False
    active_users = active_snapshot_users(snapshot) if allow_active_source_overlay and snapshot.is_dir() else []
    source_targets_safe = all(
        (repo / relative).is_file()
        and not path_has_symlink(repo / relative)
        and (repo / relative).resolve().is_relative_to(repo.resolve())
        and safe_snapshot_target(snapshot, snapshot / relative)
        and (
            not lexists(snapshot / relative)
            or ((snapshot / relative).is_file() and not (snapshot / relative).is_symlink())
        )
        for relative in SNAPSHOT_FILES
    )
    missing_sources = any(not lexists(snapshot / relative) for relative in SNAPSHOT_FILES)
    active_overlay_can_supply_missing = bool(
        active_users and not state_visible and source_targets_safe
    )
    needs_rebuild = (
        not snapshot.is_dir()
        or state_visible
        or not source_targets_safe
        or (missing_sources and not active_overlay_can_supply_missing)
    )
    if needs_rebuild and not allow_repair:
        evidence.check(
            "snapshot preparation", False,
            "safe snapshot is missing or requires repair; read-only continuation refused without changing it",
        )
        evidence.check("existing safe snapshot", False, "snapshot is missing, contains control state, or has an unsafe target")
        return False
    if needs_rebuild:
        rebuilt = rebuild_safe_snapshot(snapshot, evidence, repo)
        status_ok, parity = snapshot_status(snapshot, repo)
        if not rebuilt and not status_ok:
            evidence.check("existing safe snapshot", False, "snapshot could not be safely rebuilt")
            evidence.check("host control state excluded", not snapshot_state_visible(snapshot), "unsafe state remains visible" if snapshot_state_visible(snapshot) else "no .mester control state is in the snapshot")
            for relative, same in parity.items():
                evidence.check(f"snapshot source {relative}", same, "matches checkout" if same else "mismatch remains")
            return False
    elif not all(parity.values()):
        if active_users and allow_active_source_overlay:
            # Never replace files beneath a live virtiofs export. The nested
            # command can use a private /tmp source overlay inside the outer
            # guest while leaving its active /workspace mount untouched.
            active_source_overlay = True
            evidence.data["snapshot_source_overlay_required"] = True
            evidence.data["snapshot_export_processes"] = active_users
            evidence.check(
                "active snapshot source overlay", True,
                "active snapshot left unchanged; nested launcher will use a private guest runtime overlay",
            )
            evidence.save()
        elif not allow_repair:
            evidence.check(
                "snapshot source repair", False,
                "allowlisted sources differ from checkout; read-only continuation refused without synchronizing them",
            )
            evidence.data["snapshot_file_parity"] = parity
            evidence.save()
            return False
        else:
            repaired = sync_snapshot_sources(snapshot, evidence, repo)
            status_ok, parity = snapshot_status(snapshot, repo)
            if not repaired and not status_ok:
                evidence.check("existing safe snapshot", False, "allowlisted source repair failed")
                return False

    path_ok = snapshot.is_dir() and not snapshot.is_symlink()
    evidence.check("existing safe snapshot", path_ok, "stable snapshot exists as a real directory" if path_ok else "stable snapshot is absent or unsafe")
    state_excluded = not snapshot_state_visible(snapshot)
    evidence.check("host control state excluded", state_excluded, "no .mester control state is visible in the snapshot" if state_excluded else "host control state remains visible")
    for relative, same in parity.items():
        if same:
            detail = "matches checkout"
        elif active_source_overlay:
            detail = "differs from checkout; private runtime overlay will supply current source without changing the active snapshot"
        else:
            detail = "mismatch remains"
        evidence.check(f"snapshot source {relative}", same or active_source_overlay, detail)
    evidence.data["snapshot_file_parity"] = parity
    evidence.save()
    return expected and path_ok and state_excluded and (all(parity.values()) or active_source_overlay)


def outer_profile_checks(name: str, snapshot: Path, repo: Path, evidence: Evidence) -> Path | None:
    try:
        directory = profile_dir(name, repo)
    except AcceptanceError as exc:
        evidence.check("outer profile name", False, str(exc))
        return None
    exists = directory.is_dir() and not directory.is_symlink()
    evidence.check("outer profile state", exists, "profile state directory exists" if exists else "named outer profile is missing or unsafe")
    if not exists:
        return None
    private = not (stat.S_IMODE(directory.stat().st_mode) & 0o077)
    evidence.check("outer profile permissions", private, "profile directory is private" if private else "profile directory permissions are broader than 0700")
    running = process_matches_profile(directory)
    evidence.check("outer profile running", running, "recorded QEMU PID matches this profile's disk" if running else "profile is not verifiably running; runner will not start/restart it")
    workspace_file = directory / "workspace"
    policy = directory / "recursion_policy"
    try:
        workspace = workspace_file.read_text(encoding="utf-8").strip() if workspace_file.is_file() and not workspace_file.is_symlink() else ""
        policy_fields = policy.read_text(encoding="ascii").split() if policy.is_file() and not policy.is_symlink() else []
    except (OSError, UnicodeError):
        workspace, policy_fields = "", []
    mounted_snapshot = bool(workspace) and Path(workspace).resolve() == snapshot.resolve()
    evidence.check("outer workspace", mounted_snapshot, "outer profile uses reviewed safe snapshot" if mounted_snapshot else "outer profile does not use the reviewed safe snapshot; no retry/restart")
    state_path = directory.resolve()
    workspace_path = snapshot.resolve()
    state_overlap = state_path == workspace_path or state_path.is_relative_to(workspace_path) or workspace_path.is_relative_to(state_path)
    evidence.check("host state boundary", not state_overlap, "outer profile control state is outside the shared workspace" if not state_overlap else "outer VM control state overlaps the shared workspace; stop")
    policy_ok = len(policy_fields) == 3 and policy_fields == ["0", "1", "1"]
    evidence.check("outer recursion policy", policy_ok, "persisted depth 0 / max 1 with child state enabled" if policy_ok else "expected persisted policy 0 1 1; no VM action attempted")
    control_files_ok = outer_control_files_safe(directory)
    evidence.check("outer SSH controls", control_files_ok, "SSH metadata/key are regular and private" if control_files_ok else "SSH control metadata is missing, unsafe, or too permissive")
    return directory if private and running and mounted_snapshot and not state_overlap and policy_ok and control_files_ok else None


def outer_profile_is_ready(name: str, snapshot: Path, repo: Path = ROOT) -> bool:
    """Read-only suitability test, used before choosing a profile to reuse."""
    try:
        directory = profile_dir(name, repo)
    except AcceptanceError:
        return False
    if (directory.is_symlink() or not directory.is_dir()
            or stat.S_IMODE(directory.stat().st_mode) & 0o077
            or not process_matches_profile(directory)
            or not outer_control_files_safe(directory)):
        return False
    workspace_file, policy_file = directory / "workspace", directory / "recursion_policy"
    try:
        if any(path.is_symlink() or not path.is_file() for path in (workspace_file, policy_file)):
            return False
        workspace = workspace_file.read_text(encoding="utf-8").strip()
        fields = policy_file.read_text(encoding="ascii").split()
    except (OSError, UnicodeError):
        return False
    if not workspace or Path(workspace).resolve() != snapshot.resolve() or fields != ["0", "1", "1"]:
        return False
    state, shared = directory.resolve(), snapshot.resolve()
    return not (state == shared or state.is_relative_to(shared) or shared.is_relative_to(state))


def discover_outer_profiles(snapshot: Path, repo: Path = ROOT) -> list[str]:
    base = profile_base(repo)
    if base.is_symlink() or not base.is_dir():
        return []
    matches = []
    try:
        candidates = list(base.iterdir())
    except OSError:
        return []
    for candidate in candidates:
        if candidate.is_symlink() or not candidate.is_dir() or not PROFILE_RE.fullmatch(candidate.name):
            continue
        if outer_profile_is_ready(candidate.name, snapshot, repo):
            matches.append(candidate.name)
    return sorted(matches)


def unique_profile_name(prefix: str, repo: Path = ROOT) -> str:
    safe_prefix = prefix if PROFILE_RE.fullmatch(prefix) else "vm-acceptance"
    for _ in range(10):
        name = f"{safe_prefix}-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        directory = profile_dir(name, repo)
        if not lexists(directory) and not active_snapshot_users(directory):
            return name
    raise AcceptanceError("Could not select a fresh profile name without colliding with existing state")


def memory_budget_ok(requested_mib: int, evidence: Evidence, label: str) -> bool:
    result = run([
        sys.executable, str(ROOT / "scripts/vm_memory_budget.py"),
        "--requested-mib", str(requested_mib), "--reserve-mib", "512",
    ])
    output = (result.stdout or "") + (result.stderr or "")
    match = re.search(r"projected=(\d+) MiB", output)
    projected = int(match.group(1)) if match else None
    evidence.data[f"{label.replace(' ', '_')}_memory_budget"] = {
        "requested_mib": requested_mib,
        "reserve_mib": 512,
        "projected_mib": projected,
    }
    evidence.check(label, result.returncode == 0, "requested RAM plus 512-MiB reserve is available" if result.returncode == 0 else "insufficient or unverifiable RAM budget; no VM start attempted")
    evidence.save()
    return result.returncode == 0


def ensure_outer_profile(name: str, snapshot: Path, repo: Path, evidence: Evidence,
                         *, allow_create: bool = True,
                         allow_discovery: bool = True) -> tuple[str, Path] | None:
    """Reuse a matching live profile or provision a fresh non-destructive one.

    Existing incomplete, stopped, symlinked, or mismatched state is never
    rewritten. A new uniquely named profile is used instead, preserving it.
    """
    base = profile_base(repo)
    if base.is_symlink() or path_has_symlink(base):
        evidence.check("outer profile storage", False, "profile state root is symlinked; no VM start attempted")
        return None
    anchor = base
    while not anchor.exists() and anchor != anchor.parent:
        anchor = anchor.parent
    if (not anchor.is_dir() or anchor.stat().st_uid != os.getuid()
            or not os.access(anchor, os.W_OK | os.X_OK)):
        evidence.check("outer profile storage", False, "profile state root is not safely writable by this user; no VM start attempted")
        return None
    if base.exists() and (not base.is_dir() or base.stat().st_uid != os.getuid()):
        evidence.check("outer profile storage", False, "profile state root is not a user-owned directory; no VM start attempted")
        return None
    if outer_profile_is_ready(name, snapshot, repo):
        evidence.check("outer profile reuse", True, "existing named profile is live and uses the safe snapshot")
        return name, profile_dir(name, repo)

    if not allow_create:
        evidence.check(
            "outer profile reuse", False,
            "the exact named outer profile is not already live on the safe snapshot; no profile was started",
        )
        return None

    if allow_discovery:
        discovered = discover_outer_profiles(snapshot, repo)
        if len(discovered) == 1:
            selected = discovered[0]
            evidence.check("outer profile discovery", True, f"reused the single live safe profile {selected}")
            return selected, profile_dir(selected, repo)
        if len(discovered) > 1:
            evidence.check("outer profile discovery", False, "multiple live profiles use the stable snapshot; no additional VM was started")
            evidence.data["outer_profile_candidates"] = discovered
            evidence.save()
            return None

    active_users = active_snapshot_users(snapshot)
    if active_users:
        detail = (
            "stable snapshot is exported by a different live profile; the explicitly requested outer was not substituted"
            if not allow_discovery else
            "snapshot is exported by a VM whose profile cannot be verified; no additional VM was started"
        )
        evidence.check("outer profile discovery", False, detail)
        evidence.data["snapshot_export_processes"] = active_users
        evidence.save()
        return None

    requested = profile_dir(name, repo)
    if not lexists(requested) and not active_snapshot_users(requested):
        selected = name
        reason = "named profile is absent"
    elif not allow_discovery:
        evidence.check(
            "outer profile selection", False,
            "the explicitly requested outer profile already has unsafe or incomplete state; no alternate profile was selected",
        )
        return None
    else:
        selected = unique_profile_name(f"{name}-accept", repo)
        reason = "existing or unsafe profile state is preserved; using a fresh isolated profile"
    evidence.check("outer profile selection", True, f"{reason}; selected {selected}")

    # A new depth-0 profile defaults to 8 GiB. Check the same allocation before
    # invoking lab-vm so a failed budget never leaves partial profile state.
    if not memory_budget_ok(8192, evidence, "outer profile memory budget"):
        return None
    command = [
        str(LAB_VM), "up", selected, "--workspace", str(snapshot),
        "--recurse", "--max-depth", "1", "--memory", "8",
    ]
    # Never auto-approve host package installation or replacement of an
    # invalid base image. Missing prerequisites fail closed without prompting.
    result = run(command, cwd=repo, capture=False, no_input=True)
    evidence.check("outer profile bootstrap", result.returncode == 0, "new outer profile is ready" if result.returncode == 0 else f"lab-vm up exited {result.returncode}; profile and diagnostics are preserved")
    if result.returncode != 0:
        return None
    return selected, profile_dir(selected, repo)


def ssh_command(vm_dir: Path, remote: str) -> list[str]:
    for name in ("ssh_port", "id_vm_control", "known_hosts"):
        path = vm_dir / name
        if path.is_symlink() or not path.is_file():
            raise AcceptanceError(f"Missing or unsafe SSH control file: {name}")
    port = (vm_dir / "ssh_port").read_text(encoding="ascii").strip()
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise AcceptanceError("Outer SSH port metadata is invalid")
    if stat.S_IMODE((vm_dir / "id_vm_control").stat().st_mode) & 0o077:
        raise AcceptanceError("Outer VM control key permissions are broader than 0600")
    return [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={vm_dir / 'known_hosts'}", "-i", str(vm_dir / "id_vm_control"),
        "-p", port, "mester@127.0.0.1", remote,
    ]


REMOTE_PREFLIGHT = r'''import base64, hashlib, json, os, pathlib, re, shutil, subprocess, sys, tempfile
def command(args):
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
result = {"checks": {}, "context": None, "tools": {}}
context_payload = json.loads(CONTEXT_SOURCE_PAYLOAD_JSON)
ctx_run = None
raw = None
context_source_category = "ok"
try:
    encoded = context_payload.get("content")
    expected_hash = context_payload.get("sha256")
    if not isinstance(encoded, str) or not isinstance(expected_hash, str):
        raise ValueError("invalid source metadata")
    context_source = base64.b64decode(encoded, validate=True)
    if hashlib.sha256(context_source).hexdigest() != expected_hash:
        raise ValueError("source hash mismatch")
    with tempfile.TemporaryDirectory(prefix="little-mester-context-") as directory:
        script = pathlib.Path(directory) / "vm-context.py"
        script.write_bytes(context_source)
        ctx_run = command([sys.executable, str(script), "--repo", "/workspace", "--format", "json"])
        try:
            raw = json.loads(ctx_run.stdout) if ctx_run.returncode == 0 else None
        except json.JSONDecodeError:
            raw = None
except (ValueError, OSError, TypeError):
    context_source_category = "source_payload_invalid"
if ctx_run is not None:
    if ctx_run.returncode != 0:
        context_category = "command_failed"
    elif not ctx_run.stdout.strip():
        context_category = "empty_stdout"
    elif not isinstance(raw, dict):
        context_category = "invalid_json"
    elif not all(isinstance(raw.get(key), dict) for key in ("runtime", "recursion", "kvm")):
        context_category = "invalid_context_shape"
    else:
        context_category = "ok"
else:
    context_category = context_source_category
result["context_probe"] = {
    "returncode": ctx_run.returncode if ctx_run is not None else None,
    "stdout_present": bool(ctx_run and ctx_run.stdout),
    "stderr_present": bool(ctx_run and ctx_run.stderr),
    "failure_category": context_category,
}
if isinstance(raw, dict) and context_category == "ok":
    runtime, recursion, kvm = raw.get("runtime", {}), raw.get("recursion", {}), raw.get("kvm", {})
    result["context"] = {"runtime_kind": runtime.get("kind"), "depth": recursion.get("depth"), "max_depth": recursion.get("max_depth"),
      "child_depth_available": recursion.get("child_depth_available"),
      "device_present": kvm.get("device_present"), "device_read_write": kvm.get("device_read_write"),
      "nested_kvm_preflight_ready": kvm.get("nested_kvm_preflight_ready"),
      "cpu_virtualization": sorted(x for x in kvm.get("cpu_virtualization", []) if x in ("vmx", "svm"))}
launcher_context = command(["./lab-vm", "context", "--json"])
try:
    launcher_context_json = json.loads(launcher_context.stdout) if launcher_context.returncode == 0 else None
except json.JSONDecodeError:
    launcher_context_json = None
if launcher_context.returncode != 0:
    launcher_category = "command_failed"
elif not launcher_context.stdout.strip():
    launcher_category = "empty_stdout"
elif not isinstance(launcher_context_json, dict):
    launcher_category = "invalid_json"
elif not all(isinstance(launcher_context_json.get(key), dict) for key in ("runtime", "recursion", "kvm")):
    launcher_category = "invalid_context_shape"
else:
    launcher_category = "ok"
result["launcher_context_probe"] = {
    "returncode": launcher_context.returncode,
    "stdout_present": bool(launcher_context.stdout),
    "stderr_present": bool(launcher_context.stderr),
    "failure_category": launcher_category,
}
result["checks"]["context_valid"] = result["context"] is not None
ctx = result["context"] or {}
result["checks"]["outer_guest"] = ctx.get("runtime_kind") == "vm" and ctx.get("depth") == 0 and ctx.get("max_depth") == 1 and ctx.get("child_depth_available") is True
result["checks"]["nested_kvm"] = ctx.get("device_present") is True and ctx.get("device_read_write") is True and ctx.get("nested_kvm_preflight_ready") is True and bool(ctx.get("cpu_virtualization"))
mount = command(["findmnt", "-n", "-o", "FSTYPE", "--target", "/workspace"])
result["workspace_fstype"] = mount.stdout.strip() if mount.returncode == 0 else ""
result["checks"]["workspace_virtiofs"] = "virtiofs" in result["workspace_fstype"].lower()
result["checks"]["workspace_root"] = os.getcwd() == "/workspace"
result["checks"]["host_state_excluded"] = not os.path.lexists("/workspace/.mester") and not os.path.lexists("/workspace/.workspace/.mester")
required = ("qemu-system-x86_64", "qemu-img", "ssh", "ssh-keygen", "ssh-keyscan", "socat", "curl", "python3", "findmnt", "cloud-init")
for name in required:
    result["tools"][name] = shutil.which(name) is not None
result["tools"]["virtiofsd"] = shutil.which("virtiofsd") is not None or any(os.path.isfile(p) and os.access(p, os.X_OK) for p in ("/usr/lib/qemu/virtiofsd", "/usr/lib/qemu/qemu-virtiofsd", "/usr/libexec/virtiofsd"))
result["tools"]["iso_writer"] = shutil.which("genisoimage") is not None or shutil.which("mkisofs") is not None
result["missing_tools"] = sorted(name for name, available in result["tools"].items() if not available)
result["checks"]["tools"] = all(result["tools"].values())
budget = command([sys.executable, "scripts/vm_memory_budget.py", "--requested-mib", "4096", "--reserve-mib", "512"])
budget_text = budget.stdout + " " + budget.stderr
match = re.search(
    r"MemAvailable=(\d+) MiB, cgroup remaining=(unlimited|\d+ MiB), "
    r"releasable QEMU RSS=(\d+) MiB, projected=(\d+) MiB, "
    r"requested=(\d+) MiB \+ reserve=(\d+) MiB",
    budget_text,
)
if match:
    cgroup_remaining = match.group(2)
    cgroup_remaining_mib = None if cgroup_remaining == "unlimited" else int(cgroup_remaining.split()[0])
    projected_mib = int(match.group(4))
    error_category = None if budget.returncode == 0 else "insufficient_memory_budget"
    memory_budget = {
        "memavailable_mib": int(match.group(1)),
        "cgroup_remaining": cgroup_remaining,
        "cgroup_remaining_mib": cgroup_remaining_mib,
        "releasable_qemu_rss_mib": int(match.group(3)),
        "projected_available_mib": projected_mib,
        "available_mib": projected_mib,
        "requested_mib": int(match.group(5)),
        "reserve_mib": int(match.group(6)),
        "measurement_parsed": True,
        "command_returncode": budget.returncode,
        "stderr_present": bool(budget.stderr),
        "error_category": error_category,
    }
else:
    error_category = "memory_budget_measurement_unavailable" if budget.returncode == 2 else "memory_budget_output_unparseable"
    memory_budget = {
        "memavailable_mib": None,
        "cgroup_remaining": "unavailable",
        "cgroup_remaining_mib": None,
        "releasable_qemu_rss_mib": None,
        "projected_available_mib": None,
        "available_mib": None,
        "requested_mib": 4096,
        "reserve_mib": 512,
        "measurement_parsed": False,
        "command_returncode": budget.returncode,
        "stdout_present": bool(budget.stdout),
        "stderr_present": bool(budget.stderr),
        "error_category": error_category,
    }
result["memory_budget"] = memory_budget
result["checks"]["memory_budget"] = budget.returncode == 0 and memory_budget["measurement_parsed"]
print(json.dumps(result, sort_keys=True))
sys.exit(0 if all(result["checks"].values()) else 2)
'''


def build_remote_preflight_script(repo: Path = ROOT) -> str:
    """Use the checked-out context reporter, not a possibly stale active snapshot copy."""
    payload = active_source_overlay_payload(repo)["scripts/vm-context.py"]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"CONTEXT_SOURCE_PAYLOAD_JSON = {encoded!r}\n" + REMOTE_PREFLIGHT


REMOTE_ENSURE_NESTED_TOOLS = r'''import json, os, pathlib, shutil, subprocess, sys
def run(args):
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
def inventory():
    tools = {name: shutil.which(name) is not None for name in (
        "qemu-system-x86_64", "qemu-img", "ssh", "ssh-keygen", "ssh-keyscan",
        "socat", "curl", "python3", "findmnt", "cloud-init")}
    tools["virtiofsd"] = shutil.which("virtiofsd") is not None or any(
        os.path.isfile(path) and os.access(path, os.X_OK) for path in (
            "/usr/lib/qemu/virtiofsd", "/usr/lib/qemu/qemu-virtiofsd", "/usr/libexec/virtiofsd"))
    tools["iso_writer"] = shutil.which("genisoimage") is not None or shutil.which("mkisofs") is not None
    return tools
tools = inventory()
missing = sorted(name for name, available in tools.items() if not available)
packages_for_tool = {
    "qemu-system-x86_64": "qemu-system-x86", "qemu-img": "qemu-utils",
    "ssh": "openssh-client", "ssh-keygen": "openssh-client", "ssh-keyscan": "openssh-client",
    "socat": "socat", "curl": "curl", "python3": "python3", "findmnt": "util-linux",
    "cloud-init": "cloud-init", "virtiofsd": "virtiofsd", "iso_writer": "genisoimage",
}
result = {"missing_tools": missing, "tools": tools, "packages": [], "install_attempted": False}
if not missing:
    print(json.dumps(result, sort_keys=True))
    sys.exit(0)
unknown = sorted(set(missing) - set(packages_for_tool))
if unknown:
    result["failure"] = "no safe package mapping for missing tools"
    result["unmapped_tools"] = unknown
    print(json.dumps(result, sort_keys=True))
    sys.exit(2)
try:
    release = pathlib.Path("/etc/os-release").read_text(encoding="utf-8")
except OSError:
    release = ""
release_fields = {}
for line in release.splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        release_fields[key] = value.strip().strip('"')
if release_fields.get("ID") not in ("ubuntu", "debian"):
    result["failure"] = "nested tool bootstrap supports only the Ubuntu/Debian guest images"
    result["guest_os"] = release_fields.get("ID", "unknown")
    print(json.dumps(result, sort_keys=True))
    sys.exit(2)
if (shutil.which("apt-get") is None or shutil.which("sudo") is None
        or run(["sudo", "-n", "true"]).returncode != 0):
    result["failure"] = "non-interactive guest package management is unavailable"
    print(json.dumps(result, sort_keys=True))
    sys.exit(2)
packages = sorted({packages_for_tool[name] for name in missing})
result["packages"] = packages
updated = run(["sudo", "-n", "apt-get", "-qq", "update"])
result["apt_update_status"] = updated.returncode
if updated.returncode != 0:
    result["failure"] = "guest apt metadata update failed; no install attempted"
    print(json.dumps(result, sort_keys=True))
    sys.exit(2)
installed = run(["sudo", "-n", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install",
    "-y", "--no-install-recommends", "--no-remove", "--no-upgrade", *packages])
result["apt_install_status"] = installed.returncode
result["install_attempted"] = True
result["tools"] = inventory()
result["missing_tools"] = sorted(name for name, available in result["tools"].items() if not available)
if installed.returncode != 0:
    result["failure"] = "guest apt install failed; no package removal or upgrade was requested"
if result["missing_tools"]:
    result["failure"] = "one or more required tools remain unavailable after package installation"
print(json.dumps(result, sort_keys=True))
sys.exit(0 if installed.returncode == 0 and not result["missing_tools"] else 2)
'''


REMOTE_NESTED = r"""import base64, hashlib, json, os, pathlib, re, shutil, subprocess, sys, tempfile
profile, probe = sys.argv[1:3]
resume_existing = len(sys.argv) > 3 and sys.argv[3] == "--resume-existing"
root = pathlib.Path("/workspace")
sessions = pathlib.Path.home() / ".cache/lab-vm-recursion"
session, vm_dir = sessions / profile, sessions / profile / "vm" / profile
source_overlay_payload = json.loads(SOURCE_OVERLAY_PAYLOAD_JSON)
summary = {"profile": profile, "probe": probe, "checks": {}, "child": {}, "depth_limit": {}}
def progress(stage, status="started", **fields):
    event = {"stage": stage, "status": status}
    event.update(fields)
    print("__LM_VM_ACCEPTANCE_PROGRESS__" + json.dumps(event, sort_keys=True), flush=True)
def emit(code):
    print("__LM_VM_ACCEPTANCE__" + json.dumps(summary, sort_keys=True), flush=True)
    raise SystemExit(code)
progress("remote_script", "started")
if resume_existing:
    if session.is_symlink() or not session.is_dir():
        summary["checks"]["existing_profile"] = False
        summary["failure"] = "requested existing child profile is missing or unsafe"
        emit(2)
    if session.stat().st_mode & 0o077:
        summary["checks"]["existing_profile"] = False
        summary["failure"] = "requested existing child profile is not private"
        emit(2)
    summary["checks"]["existing_profile"] = True
else:
    if session.exists() or session.is_symlink():
        summary["checks"]["fresh_profile"] = False
        summary["failure"] = "child profile already exists"
        emit(2)
    summary["checks"]["fresh_profile"] = True
workspace_path, state_path = root.resolve(), session.resolve()
if workspace_path == state_path or workspace_path in state_path.parents or state_path in workspace_path.parents:
    summary["checks"]["state_boundary"] = False
    summary["failure"] = "child control state overlaps the shared workspace"
    emit(2)
summary["checks"]["state_boundary"] = True
progress("runtime_overlay", "started")
def stage_runtime_overlay():
    if not source_overlay_payload:
        return root
    expected = {"lab-vm", "lib/state.sh", "lib/vm/proxy.py", "lib/vm/runner.sh",
        "lib/vm/host-services.sh", "lib/vm/lifecycle.sh", "lib/vm/recursion.sh",
        "scripts/vm-context.py", "scripts/vm-extra.sh", "scripts/vm-guest-isolation-check.py",
        "scripts/vm-install-tools.sh", "scripts/vm-recursion-live-children.py",
        "scripts/vm-recursion-workspace.py", "scripts/vm-update.sh",
        "tests/test_vm_context.py", "tests/test_vm_recursion_live_children.py"}
    if set(source_overlay_payload) != expected:
        raise ValueError("runtime source overlay has an unexpected file set")
    stage = pathlib.Path(tempfile.mkdtemp(prefix="little-mester-acceptance-", dir="/tmp"))
    try:
        os.chmod(stage, 0o700)
        populate_runtime_overlay(stage)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return stage
def populate_runtime_overlay(stage):
    workspace = root.resolve(strict=True)
    excluded = {".aws", ".bench-models", ".cache", ".config", ".git", ".gnupg", ".kube",
        ".local", ".mester", ".pytest_cache", ".ruff_cache", ".secrets", ".ssh", ".tox",
        ".venv", "__pycache__", "auth.json", "credentials", "credentials.json", "node_modules",
        "opencode.db", "secrets", "secrets.env"}
    def safe_link(source, destination):
        target = source.resolve(strict=True)
        if not target.is_relative_to(workspace):
            raise ValueError("runtime overlay link escapes the safe workspace")
        destination.symlink_to(target, target_is_directory=target.is_dir())
    overlay_scripts = any(relative.startswith("scripts/") for relative in source_overlay_payload)
    overlay_tests = any(relative.startswith("tests/") for relative in source_overlay_payload)
    for item in root.iterdir():
        if (item.name in excluded or item.name.startswith(".")
                or item.name in ("lab-vm", "lib")
                or (overlay_scripts and item.name == "scripts")
                or (overlay_tests and item.name == "tests")):
            continue
        if item.name.endswith((".db", ".key", ".pem", ".secret", ".sqlite", ".sqlite3", ".token")):
            continue
        safe_link(item, stage / item.name)
    def write_source(relative):
        item = source_overlay_payload[relative]
        encoded, expected_hash, mode = item.get("content"), item.get("sha256"), item.get("mode")
        if not isinstance(encoded, str) or not isinstance(expected_hash, str) or not isinstance(mode, int) or not 0 <= mode <= 0o777:
            raise ValueError("runtime source metadata is invalid")
        content = base64.b64decode(encoded, validate=True)
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise ValueError("runtime source hash does not match")
        destination = stage / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with destination.open("xb") as output:
            output.write(content)
        os.chmod(destination, mode)
    launcher = root / "lab-vm"
    if launcher.is_symlink() or (launcher.exists() and not launcher.is_file()):
        raise ValueError("active workspace lab-vm entry point is unsafe")
    write_source("lab-vm")
    lib_source, lib_stage = root / "lib", stage / "lib"
    vm_source = lib_source / "vm"
    if (lib_source.is_symlink() or not lib_source.is_dir()
            or vm_source.is_symlink() or not vm_source.is_dir()):
        raise ValueError("active workspace VM source directories are unsafe")
    lib_stage.mkdir(mode=0o700)
    for item in lib_source.iterdir():
        if item.name != "vm":
            relative = "lib/" + item.name
            if relative in source_overlay_payload:
                write_source(relative)
            else:
                safe_link(item, lib_stage / item.name)
    for relative in source_overlay_payload:
        if relative.startswith("lib/") and relative.count("/") == 1:
            destination = stage / relative
            if not destination.exists() and not destination.is_symlink():
                write_source(relative)
    vm_stage = lib_stage / "vm"
    vm_stage.mkdir(mode=0o700)
    for item in vm_source.iterdir():
        relative = "lib/vm/" + item.name
        if relative in source_overlay_payload:
            write_source(relative)
        elif item.name != "__pycache__":
            safe_link(item, vm_stage / item.name)
    if overlay_scripts:
        scripts_source, scripts_stage = root / "scripts", stage / "scripts"
        if scripts_source.is_symlink() or (scripts_source.exists() and not scripts_source.is_dir()):
            raise ValueError("active workspace scripts directory is unsafe")
        scripts_stage.mkdir(mode=0o700)
        if scripts_source.is_dir():
            for item in scripts_source.iterdir():
                relative = "scripts/" + item.name
                if relative in source_overlay_payload:
                    write_source(relative)
                elif (item.name.startswith(".") or item.name == "__pycache__"
                        or item.name.endswith((".db", ".key", ".pem", ".secret", ".sqlite", ".sqlite3", ".token"))):
                    continue
                else:
                    safe_link(item, scripts_stage / item.name)
        for relative in source_overlay_payload:
            if relative.startswith("scripts/"):
                destination = stage / relative
                if not destination.exists() and not destination.is_symlink():
                    write_source(relative)
    if overlay_tests:
        tests_source, tests_stage = root / "tests", stage / "tests"
        if tests_source.is_symlink() or (tests_source.exists() and not tests_source.is_dir()):
            raise ValueError("active workspace tests directory is unsafe")
        tests_stage.mkdir(mode=0o700)
        if tests_source.is_dir():
            for item in tests_source.iterdir():
                relative = "tests/" + item.name
                if relative in source_overlay_payload:
                    write_source(relative)
                elif (item.name.startswith(".") or item.name == "__pycache__"
                        or item.name.endswith((".db", ".key", ".pem", ".secret", ".sqlite", ".sqlite3", ".token"))):
                    continue
                else:
                    safe_link(item, tests_stage / item.name)
        for relative in source_overlay_payload:
            if relative.startswith("tests/"):
                destination = stage / relative
                if not destination.exists() and not destination.is_symlink():
                    write_source(relative)

class SourceWorkspaceFailure(Exception):
    def __init__(self, category, returncode=None, stderr_present=False):
        super().__init__(category)
        self.category = category
        self.returncode = returncode
        self.stderr_present = stderr_present

def prepare_current_source_workspace(launcher_root, root, source_payload):
    try:
        session_root = pathlib.Path(tempfile.mkdtemp(
            prefix="little-mester-child-source-", dir="/tmp"))
        os.chmod(session_root, 0o700)
    except OSError:
        raise SourceWorkspaceFailure("workspace_session_unavailable") from None
    builder = launcher_root / "scripts/vm-recursion-workspace.py"
    try:
        prepared = subprocess.run(
            [sys.executable, str(builder), "prepare", "--source", str(root),
             "--session", str(session_root / "snapshot")],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
    except OSError:
        raise SourceWorkspaceFailure("workspace_builder_unavailable") from None
    if prepared.returncode != 0:
        raise SourceWorkspaceFailure("workspace_builder_failed", prepared.returncode, bool(prepared.stderr))
    try:
        child_workspace = pathlib.Path(prepared.stdout.strip()).resolve(strict=True)
        if (not child_workspace.is_dir()
                or not child_workspace.is_relative_to(session_root.resolve(strict=True))):
            raise ValueError("unsafe builder path")
        for relative, item in source_payload.items():
            encoded, expected_hash, mode = item.get("content"), item.get("sha256"), item.get("mode")
            if not isinstance(encoded, str) or not isinstance(expected_hash, str) or not isinstance(mode, int) or not 0 <= mode <= 0o777:
                raise ValueError("invalid source metadata")
            content = base64.b64decode(encoded, validate=True)
            if hashlib.sha256(content).hexdigest() != expected_hash:
                raise ValueError("source hash mismatch")
            destination = child_workspace / relative
            parent = destination.parent
            while parent != child_workspace.parent:
                if parent.is_symlink() or not parent.is_dir():
                    raise ValueError("unsafe source parent")
                if parent == child_workspace:
                    break
                parent = parent.parent
            if destination.is_symlink() or (destination.exists() and not destination.is_file()):
                raise ValueError("unsafe source target")
            descriptor, temporary_name = tempfile.mkstemp(
                prefix="." + destination.name + ".acceptance-", dir=destination.parent)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(temporary_name, mode)
                os.replace(temporary_name, destination)
            finally:
                pathlib.Path(temporary_name).unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        raise SourceWorkspaceFailure("source_materialization_failed", prepared.returncode,
            bool(prepared.stderr)) from None
    return session_root, child_workspace

try:
    launcher_root = stage_runtime_overlay()
except (OSError, ValueError, KeyError, TypeError) as exc:
    progress("runtime_overlay", "failed", error_type=type(exc).__name__)
    summary["checks"]["runtime_source_overlay"] = False
    summary["failure"] = "verified runtime sources could not be staged safely; no child VM was started"
    summary["source_overlay_error"] = type(exc).__name__
    emit(2)
summary["checks"]["runtime_source_overlay"] = True
progress("runtime_overlay", "completed", private_overlay=launcher_root != root)
if launcher_root != root:
    print("Using a private verified runtime overlay; the active /workspace snapshot is unchanged.", flush=True)
    summary["runtime_source_overlay_path"] = str(launcher_root)
child_workspace = root
source_workspace_session = None
if not resume_existing:
    progress("child_workspace_source", "started")
    try:
        source_workspace_session, child_workspace = prepare_current_source_workspace(
            launcher_root, root, source_overlay_payload)
    except SourceWorkspaceFailure as exc:
        progress("child_workspace_source", "failed", reason=exc.category)
        summary["checks"]["child_workspace_source"] = False
        summary["child_workspace_source"] = {
            "failure_category": exc.category,
            "builder_returncode": exc.returncode,
            "stderr_present": exc.stderr_present,
        }
        summary["failure"] = "current-source child workspace could not be prepared safely; no child VM was started"
        emit(2)
    summary["checks"]["child_workspace_source"] = True
    summary["child_workspace_source"] = {"source_files_verified": len(source_overlay_payload)}
    progress("child_workspace_source", "completed", passed=True)
env = os.environ.copy()
env["LAB_VM_RECURSION_DEPTH"], env["LAB_VM_MAX_DEPTH"] = "0", "1"
# The active stable snapshot may still contain older CLI/helper sources. Route
# generic state into this child's private session and force downloads through
# the existing parent egress proxy, even if a non-login SSH shell omitted the
# guest profile or inherited a broader NO_PROXY value.
env["VM_STATE_DIR"] = str(session)
env["LM_ACCEPTANCE_LAUNCHER"] = str(launcher_root / "lab-vm")
os.environ["LM_ACCEPTANCE_LAUNCHER"] = env["LM_ACCEPTANCE_LAUNCHER"]
parent_proxy = "http://10.0.2.100:3128"
env.update({key: parent_proxy for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")})
env.update({key: "localhost,127.0.0.1,10.0.2.100" for key in ("NO_PROXY", "no_proxy")})
if resume_existing:
    print("Reusing the existing nested child; no VM will be started or stopped.", flush=True)
else:
    print("Starting one new nested child; no model/TUI or teardown will run.", flush=True)
    progress("child_vm_up", "started")
    up = subprocess.run([str(launcher_root / "lab-vm"), "-d", "up", profile, "--workspace", str(child_workspace), "--recurse"], cwd=launcher_root, env=env, check=False)
    progress("child_vm_up", "completed", returncode=up.returncode)
    summary["checks"]["child_up"] = up.returncode == 0
    if up.returncode != 0:
        summary["failure"] = "child up failed; preserve the profile and its logs"
        emit(3)
    # Nested `lab-vm up --recurse` has copied this verified source into the
    # child profile's own recursion session before returning. The live VM mounts
    # that profile-owned copy, so the one-shot staging tree is now disposable.
    if source_workspace_session is not None:
        shutil.rmtree(source_workspace_session, ignore_errors=True)
progress("child_control_state", "started")
if not vm_dir.is_dir() or vm_dir.is_symlink():
    progress("child_control_state", "failed", reason="profile_state_missing_or_unsafe")
    summary["checks"]["child_state"] = False
    summary["failure"] = "child profile state is absent or unsafe"
    emit(4)
summary["checks"]["child_state"] = True
progress("child_control_state", "completed")
port_file, key_file, known_file = vm_dir / "ssh_port", vm_dir / "id_vm_control", vm_dir / "known_hosts"
if any(p.is_symlink() or not p.is_file() for p in (port_file, key_file, known_file)):
    progress("child_control_state", "failed", reason="ssh_controls_missing_or_unsafe")
    summary["failure"] = "child SSH control files are missing or unsafe"
    emit(5)
try:
    port = int(port_file.read_text(encoding="ascii").strip())
except (OSError, ValueError):
    progress("child_control_state", "failed", reason="ssh_port_invalid")
    summary["failure"] = "child SSH port metadata is invalid"
    emit(5)
if not 1 <= port <= 65535:
    progress("child_control_state", "failed", reason="ssh_port_out_of_range")
    summary["failure"] = "child SSH port metadata is out of range"
    emit(5)
ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
       "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "StrictHostKeyChecking=yes",
       "-o", "UserKnownHostsFile=" + str(known_file), "-i", str(key_file), "-p", str(port), "mester@127.0.0.1"]
if resume_existing:
    progress("child_vm_up", "started", timeout_seconds=10, reused=True)
    try:
        reachable = subprocess.run(ssh + ["true"], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False, timeout=10)
    except subprocess.TimeoutExpired:
        progress("child_vm_up", "timed_out", timeout_seconds=10, reused=True)
        summary["checks"]["child_up"] = False
        summary["failure"] = "existing child did not answer SSH; it was not restarted"
        emit(3)
    summary["checks"]["child_up"] = reachable.returncode == 0
    progress("child_vm_up", "completed", returncode=reachable.returncode,
        stderr_present=bool(reachable.stderr), reused=True)
    if not summary["checks"]["child_up"]:
        summary["failure"] = "existing child did not answer SSH; it was not restarted"
        emit(3)
def run_child_stage(stage, remote, timeout, input_text=None):
    progress(stage, "started", timeout_seconds=timeout)
    try:
        result = subprocess.run(ssh + [remote], input=input_text, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL if input_text is None else None,
            check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        progress(stage, "timed_out", timeout_seconds=timeout)
        summary["checks"]["child_acceptance"] = False
        summary["child"] = {"result": "timed out", "failed_stage": stage,
            "timeout_seconds": timeout}
        summary["failure"] = "child readiness subcommand timed out; preserve the child VM and diagnostics"
        emit(6)
    progress(stage, "completed", returncode=result.returncode,
        stderr_present=bool(result.stderr))
    return result
def obj(text):
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
print("Checking child readiness: cloud-init 30s, context 30s, findmnt 10s, workspace boundary 10s.", flush=True)
cloud_run = run_child_stage("child_cloud_init_status", "sudo -n cloud-init status --format json", 30)
cloud = obj(cloud_run.stdout)
context_run = run_child_stage("child_context", "cd /workspace && ./lab-vm context --json", 30)
context = obj(context_run.stdout)
mount = run_child_stage("child_findmnt", "findmnt -n -o FSTYPE --target /workspace", 10)
fstype = mount.stdout.strip() if mount.returncode == 0 else ""
workspace_probe = r'''import json, pathlib
paths = (pathlib.Path("/workspace/.mester"), pathlib.Path("/workspace/.workspace/.mester"))
print(json.dumps({"host_state_excluded": not any(p.exists() or p.is_symlink() for p in paths)}, sort_keys=True))
'''
boundary = run_child_stage("child_workspace_boundary", "cd /workspace && python3 -", 10, workspace_probe)
boundary_data = obj(boundary.stdout)
source_hashes = {relative: item.get("sha256") for relative, item in source_overlay_payload.items()}
source_probe = r'''import hashlib, json, pathlib, sys
expected = json.loads(sys.stdin.read())
checks = {}
for relative, digest in expected.items():
    path = pathlib.Path("/workspace") / relative
    checks[relative] = (isinstance(digest, str) and path.is_file() and not path.is_symlink()
        and hashlib.sha256(path.read_bytes()).hexdigest() == digest)
print(json.dumps({"checks": checks}, sort_keys=True))
sys.exit(0 if checks and all(checks.values()) else 2)
'''
source_run = run_child_stage("child_source_parity", "cd /workspace && python3 -", 30,
    json.dumps(source_hashes, sort_keys=True))
source_result = obj(source_run.stdout)
source_checks = source_result.get("checks", {}) if isinstance(source_result, dict) else {}
source_parity_ok = (source_run.returncode == 0 and isinstance(source_checks, dict)
    and set(source_checks) == set(source_hashes) and all(value is True for value in source_checks.values()))
self_tests = run_child_stage(
    "child_recursive_self_tests",
    "cd /workspace && python3 tests/test_vm_context.py && python3 tests/test_vm_recursion_live_children.py",
    180,
)
self_test_output = self_tests.stdout + "\n" + self_tests.stderr
test_run_counts = [int(value) for value in re.findall(r"^Ran (\d+) tests?$", self_test_output, re.MULTILINE)]
failed_test_names = re.findall(r"^(?:FAIL|ERROR): (test_[A-Za-z0-9_]+)", self_test_output, re.MULTILINE)
self_tests_ok = self_tests.returncode == 0 and len(test_run_counts) == 2 and sum(test_run_counts) > 0
child_summary = {"cloud_status": cloud.get("status") if cloud else None,
  "reused_existing_profile": resume_existing,
  "cloud_extended_status": cloud.get("extended_status") if cloud else None,
 "cloud_errors_empty": cloud is not None and isinstance(cloud.get("errors"), list) and not cloud["errors"],
 "cloud_recoverable_errors_empty": cloud is not None and isinstance(cloud.get("recoverable_errors"), dict) and not cloud["recoverable_errors"],
 "cloud_command_ok": cloud_run.returncode == 0,
 "context_command_ok": context_run.returncode == 0 and context is not None,
 "runtime_kind": (context or {}).get("runtime", {}).get("kind"),
 "depth": (context or {}).get("recursion", {}).get("depth"),
 "max_depth": (context or {}).get("recursion", {}).get("max_depth"),
 "child_depth_available": (context or {}).get("recursion", {}).get("child_depth_available"),
 "nested_kvm_ready": (context or {}).get("kvm", {}).get("nested_kvm_preflight_ready"),
  "workspace_fstype": fstype,
  "host_state_excluded": (boundary_data or {}).get("host_state_excluded") is True,
  "current_source_files_match": source_parity_ok,
   "recursive_self_tests_passed": self_tests_ok,
  "probe_stages": {
  "child_cloud_init_status": {"returncode": cloud_run.returncode, "stderr_present": bool(cloud_run.stderr)},
  "child_context": {"returncode": context_run.returncode, "stderr_present": bool(context_run.stderr)},
  "child_findmnt": {"returncode": mount.returncode, "stderr_present": bool(mount.stderr)},
   "child_workspace_boundary": {"returncode": boundary.returncode, "stderr_present": bool(boundary.stderr)},
   "child_source_parity": {"returncode": source_run.returncode, "stderr_present": bool(source_run.stderr),
       "files_checked": len(source_hashes), "files_matching": sum(value is True for value in source_checks.values()) if isinstance(source_checks, dict) else 0},
    "child_recursive_self_tests": {"returncode": self_tests.returncode,
        "tests_run": sum(test_run_counts), "test_programs_run": len(test_run_counts),
        "failed_test_names": failed_test_names, "stderr_present": bool(self_tests.stderr)}}}
checks = {
 "cloud_command_ok": child_summary["cloud_command_ok"],
 "cloud_status_done": child_summary["cloud_status"] == "done",
 "cloud_extended_status_done": child_summary["cloud_extended_status"] == "done",
 "cloud_errors_empty": child_summary["cloud_errors_empty"],
 "cloud_recoverable_errors_empty": child_summary["cloud_recoverable_errors_empty"],
 "context_command_ok": child_summary["context_command_ok"],
 "runtime_is_vm": child_summary["runtime_kind"] == "vm",
 "depth_is_1": child_summary["depth"] == 1,
 "max_depth_is_1": child_summary["max_depth"] == 1,
 "child_depth_unavailable": child_summary["child_depth_available"] is False,
  "nested_kvm_ready": child_summary["nested_kvm_ready"] is True,
  "workspace_is_virtiofs": "virtiofs" in fstype.lower(),
  "host_state_excluded": child_summary["host_state_excluded"],
  "current_source_files_match": child_summary["current_source_files_match"],
  "recursive_self_tests_passed": child_summary["recursive_self_tests_passed"]}
child_summary["checks"] = checks
summary["child"] = child_summary
summary["checks"]["child_acceptance"] = all(checks.values())
progress("child_readiness", "completed", passed=summary["checks"]["child_acceptance"])
if not summary["checks"]["child_acceptance"]:
    summary["failure"] = "child readiness checks failed; preserve state and inspect named results"
    emit(6)
progress("nested_isolation", "started", timeout_seconds=150)
print("Checking nested QEMU forwarding and guest-originated isolation (model availability excluded; timeout: 150s).", flush=True)
isolation_env = env.copy()
isolation_env["VM_ISOLATION_SKIP_MODEL"] = "1"
try:
    isolation_run = subprocess.run(
        [str(launcher_root / "lab-vm"), "verify-isolation", profile],
        cwd=launcher_root, env=isolation_env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=150,
    )
except subprocess.TimeoutExpired as error:
    stdout = error.stdout.decode("utf-8", "replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
    summary["nested_isolation"] = {
        "timed_out": True,
        "timeout_seconds": 150,
        "pass_lines": len(re.findall(r"^PASS:", stdout, re.MULTILINE)),
        "fail_lines": len(re.findall(r"^FAIL:", stdout, re.MULTILINE)),
    }
    summary["checks"]["nested_isolation"] = False
    progress("nested_isolation", "timed_out", timeout_seconds=150)
    summary["failure"] = "nested isolation probes timed out; preserve state and stop"
    emit(7)
isolation_passes = len(re.findall(r"^PASS:", isolation_run.stdout, re.MULTILINE))
isolation_failures = len(re.findall(r"^FAIL:", isolation_run.stdout, re.MULTILINE))
isolation_skips = len(re.findall(r"^SKIP:", isolation_run.stdout, re.MULTILINE))
qemu_argv_validated = (
    "QEMU configuration: restrict=on; SSH bound to 127.0.0.1:" in isolation_run.stdout
    and "; only the four reviewed guestfwd channels are configured." in isolation_run.stdout
)
loopback_listeners_validated = (
    "QEMU TCP listeners: SSH and egress proxy are loopback-only." in isolation_run.stdout
)
result_match = re.search(r"^Result: (\d+) passed, (\d+) failed$", isolation_run.stdout, re.MULTILINE)
result_matches_output = bool(result_match and int(result_match.group(1)) == isolation_passes
    and int(result_match.group(2)) == isolation_failures)
isolation_ok = (isolation_run.returncode == 0 and qemu_argv_validated
    and loopback_listeners_validated and result_matches_output
    and isolation_passes > 0 and isolation_failures == 0)
summary["nested_isolation"] = {
    "returncode": isolation_run.returncode,
    "qemu_argv_validated": qemu_argv_validated,
    "loopback_listeners_validated": loopback_listeners_validated,
    "passed_checks": isolation_passes,
    "failed_checks": isolation_failures,
    "skipped_checks": isolation_skips,
    "result_summary_parsed": result_matches_output,
    "stderr_present": bool(isolation_run.stderr),
}
summary["checks"]["nested_isolation"] = isolation_ok
progress("nested_isolation", "completed", returncode=isolation_run.returncode,
    passed=isolation_ok, passed_checks=isolation_passes,
    failed_checks=isolation_failures, skipped_checks=isolation_skips,
    stderr_present=bool(isolation_run.stderr))
if not isolation_ok:
    summary["failure"] = "nested QEMU or guest isolation checks failed; preserve state and stop"
    emit(7)
overflow_probe = r'''import json, os, pathlib, subprocess, sys
probe = sys.argv[1]
session = pathlib.Path.home() / ".cache/lab-vm-recursion" / probe
if session.exists() or session.is_symlink():
    print(json.dumps({"preexisting": True}))
    sys.exit(11)
env = os.environ.copy()
env["LAB_VM_RECURSION_DEPTH"], env["LAB_VM_MAX_DEPTH"] = "1", "1"
# This probe runs inside the child guest; the outer guest's private /tmp
# launcher path and LM_ACCEPTANCE_LAUNCHER environment do not cross the VM.
launcher = pathlib.Path("/workspace/lab-vm")
try:
 attempt = subprocess.run([str(launcher), "up", probe, "--workspace", "/workspace", "--recurse"], cwd=launcher.parent, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, timeout=90)
 summary = {"returncode": attempt.returncode,
  "expected_refusal": attempt.returncode == 1 and "maximum nesting depth 1 reached; no child VM or agent was started." in attempt.stdout,
  "state_created": session.exists() or session.is_symlink()}
except subprocess.TimeoutExpired:
 summary = {"timed_out": True, "expected_refusal": False,
  "state_created": session.exists() or session.is_symlink()}
print(json.dumps(summary, sort_keys=True))
sys.exit(0 if summary["expected_refusal"] and not summary["state_created"] else 12)
'''
progress("depth_cap_refusal", "started", timeout_seconds=120)
print("Checking depth-cap refusal before state creation (timeout: 120s).", flush=True)
try:
    overflow = subprocess.run(ssh + ["python3 -", probe], input=overflow_probe, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=120)
except subprocess.TimeoutExpired:
    progress("depth_cap_refusal", "timed_out", timeout_seconds=120)
    summary["depth_limit"] = {"result": "timed out; preserve state and stop"}
    summary["checks"]["depth_limit_refused_before_state"] = False
    summary["failure"] = "depth-cap probe timed out; preserve any generated state and stop"
    emit(8)
try:
    overflow_summary = json.loads(overflow.stdout)
except json.JSONDecodeError:
    overflow_summary = None
summary["depth_limit"] = overflow_summary if isinstance(overflow_summary, dict) else {
    "result": "unparseable",
    "probe_returncode": overflow.returncode,
    "stdout_bytes": len(overflow.stdout),
    "stderr_present": bool(overflow.stderr),
}
summary["checks"]["depth_limit_refused_before_state"] = overflow.returncode == 0 and isinstance(overflow_summary, dict)
progress("depth_cap_refusal", "completed", returncode=overflow.returncode,
    passed=summary["checks"]["depth_limit_refused_before_state"],
    summary_json=isinstance(overflow_summary, dict), stderr_present=bool(overflow.stderr))
if not summary["checks"]["depth_limit_refused_before_state"]:
    summary["failure"] = "over-depth launch did not refuse before state; stop and preserve evidence"
    emit(8)
progress("outer_continuity", "started", timeout_seconds=30)
print("Checking outer-session continuity (timeout: 30s).", flush=True)
try:
    parent_run = subprocess.run([str(launcher_root / "lab-vm"), "context", "--json"], cwd=launcher_root,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=30)
except subprocess.TimeoutExpired:
    progress("outer_continuity", "timed_out", timeout_seconds=30)
    summary["checks"]["outer_session_usable"] = False
    summary["failure"] = "outer-session continuity probe timed out"
    emit(9)
try:
    parent = json.loads(parent_run.stdout)
except json.JSONDecodeError:
    parent = None
recursion = parent.get("recursion", {}) if isinstance(parent, dict) else {}
parent_ok = parent_run.returncode == 0 and recursion.get("depth") == 0 and recursion.get("max_depth") == 1 and recursion.get("child_depth_available") is True
summary["checks"]["outer_session_usable"] = parent_ok
progress("outer_continuity", "completed", returncode=parent_run.returncode, passed=parent_ok)
if not parent_ok:
    summary["failure"] = "outer session context changed or became unavailable"
    emit(9)
summary["outcome"] = "PASS"
emit(0)
"""


def add_context_checks(evidence: Evidence, context: dict[str, object], label: str) -> bool:
    checks = {
        "runtime": (context.get("runtime_kind") == "vm", f"runtime kind is {context.get('runtime_kind')!r}"),
        "recursion policy": (context.get("depth") == 0 and context.get("max_depth") == 1 and context.get("child_depth_available") is True, f"depth/max/child-available are {context.get('depth')}/{context.get('max_depth')}/{context.get('child_depth_available')}; expected 0/1/true"),
        "nested KVM": (context.get("nested_kvm_preflight_ready") is True and context.get("device_read_write") is True and bool(context.get("cpu_virtualization")), "usable nested KVM is exposed"),
        "workspace transport": ("virtiofs" in str(context.get("workspace_filesystem", "")).lower(), f"filesystem is {context.get('workspace_filesystem')!r}"),
    }
    for name, (passed, detail) in checks.items():
        evidence.check(f"{label} {name}", passed, detail)
    evidence.data[f"{label.replace(' ', '_')}_context"] = context
    evidence.save()
    return all(passed for passed, _ in checks.values())


def outer_guest_preflight(
    vm_dir: Path,
    evidence: Evidence,
    *,
    repo: Path = ROOT,
    require_child_memory_budget: bool = True,
    allow_tool_bootstrap: bool = True,
) -> bool:
    non_tool_checks = [
        "context_valid", "outer_guest", "nested_kvm", "workspace_root",
        "workspace_virtiofs", "host_state_excluded",
    ]
    if require_child_memory_budget:
        non_tool_checks.append("memory_budget")

    try:
        preflight_script = build_remote_preflight_script(repo)
    except (AcceptanceError, OSError) as exc:
        evidence.check(
            "outer guest context source", False,
            f"current context source is unavailable ({type(exc).__name__}); no child was started",
        )
        return False

    def query(script: str):
        result = run(ssh_command(vm_dir, "cd /workspace && python3 -"), input_text=script)
        return result, parse_json_object(result.stdout)

    try:
        _result, parsed = query(preflight_script)
    except (AcceptanceError, OSError) as exc:
        evidence.check("outer guest SSH", False, f"connection failed ({type(exc).__name__}); no child was started")
        return False
    if parsed is None:
        evidence.check(
            "outer guest preflight", False,
            f"SSH returned no valid JSON; command rc={_result.returncode}; "
            f"stdout-present={bool(_result.stdout)}; stderr-present={bool(_result.stderr)}; no child was started",
        )
        return False

    probe_categories = {
        "ok", "command_failed", "empty_stdout", "invalid_json",
        "invalid_context_shape", "source_payload_invalid",
    }

    def sanitize_probe(value: object) -> dict[str, object] | None:
        if not isinstance(value, dict):
            return None
        category = value.get("failure_category")
        returncode = value.get("returncode")
        return {
            "failure_category": (
                category if isinstance(category, str) and category in probe_categories
                else "invalid_probe"
            ),
            "returncode": returncode if type(returncode) is int else None,
            "stdout_present": value.get("stdout_present") is True,
            "stderr_present": value.get("stderr_present") is True,
        }

    context_probes = {}
    for name in ("context_probe", "launcher_context_probe"):
        sanitized = sanitize_probe(parsed.get(name))
        if sanitized is not None:
            context_probes[name] = sanitized
    if context_probes:
        evidence.data["outer_guest_context_probes"] = context_probes
        evidence.save()
    context_probe = context_probes.get("context_probe", {})

    initial_checks = parsed.get("checks", {})
    initial_tools = parsed.get("tools", {})
    ignored_initial_checks = {"tools"}
    if not require_child_memory_budget:
        ignored_initial_checks.add("memory_budget")
    if (allow_tool_bootstrap
            and isinstance(initial_checks, dict) and initial_checks.get("tools") is False
            and isinstance(initial_tools, dict)
            and all(name in initial_checks and initial_checks[name] is True for name in non_tool_checks)
            and all(value is True for name, value in initial_checks.items() if name not in ignored_initial_checks)):
        missing = sorted(key for key, available in initial_tools.items() if available is not True)
        evidence.check(
            "outer guest nested tools",
            False,
            f"missing or unavailable: {', '.join(missing) or 'tool inventory incomplete'}; attempting bounded guest-only bootstrap",
        )
        try:
            install_result, install = query(REMOTE_ENSURE_NESTED_TOOLS)
        except (AcceptanceError, OSError) as exc:
            evidence.check("outer guest nested-tool bootstrap", False, f"guest-only bootstrap could not run: {exc}")
            return False
        if install is None:
            evidence.check("outer guest nested-tool bootstrap", False, "installer returned invalid data; no child was started")
            return False
        packages = install.get("packages", [])
        package_names = ", ".join(packages) if isinstance(packages, list) else "package list unavailable"
        installed = install_result.returncode == 0 and not install.get("missing_tools")
        if installed:
            attempted = install.get("install_attempted") is True
            detail = (f"guest-only apt install completed; requested packages: {package_names}"
                      if attempted else "tools became available; no package install was needed")
            evidence.data["outer_guest_bootstrap_packages"] = packages if attempted else []
            evidence.save()
            evidence.check("outer guest nested-tool bootstrap", True, detail)
            try:
                _result, parsed = query(preflight_script)
            except (AcceptanceError, OSError) as exc:
                evidence.check(
                    "outer guest preflight retry", False,
                    f"SSH check failed after guest bootstrap ({type(exc).__name__})",
                )
                return False
            if parsed is None:
                evidence.check(
                    "outer guest preflight retry", False,
                    f"SSH returned no valid JSON after guest bootstrap; command rc={_result.returncode}; "
                    f"stdout-present={bool(_result.stdout)}; stderr-present={bool(_result.stderr)}",
                )
                return False
        else:
            failure = install.get("failure")
            detail = f"guest-only install failed; requested packages: {package_names}"
            if failure:
                detail += f"; {failure}"
            evidence.data["outer_guest_tool_bootstrap"] = install
            evidence.save()
            evidence.check("outer guest nested-tool bootstrap", False, detail)
            return False
    elif (not allow_tool_bootstrap and isinstance(initial_checks, dict)
          and initial_checks.get("tools") is False):
        missing = sorted(key for key, available in initial_tools.items() if available is not True)
        evidence.check(
            "outer guest nested tools", False,
            f"missing or unavailable: {', '.join(missing) or 'tool inventory incomplete'}; read-only continuation will not install tools",
        )

    ctx = parsed.get("context", {})
    if isinstance(ctx, dict):
        outer_context = safe_context(json.dumps({
            "runtime": {"kind": ctx.get("runtime_kind")},
            "recursion": {"depth": ctx.get("depth"), "max_depth": ctx.get("max_depth"), "child_depth_available": ctx.get("child_depth_available")},
            "kvm": {key: ctx.get(key) for key in ("device_present", "device_read_write", "nested_kvm_preflight_ready", "cpu_virtualization")},
            "workspace_filesystem": parsed.get("workspace_fstype"),
        }))
    else:
        outer_context = None
    context_ok = outer_context is not None and add_context_checks(evidence, outer_context, "outer guest")
    checks = parsed.get("checks", {})
    if not isinstance(checks, dict):
        evidence.check("outer guest response", False, "preflight response has invalid shape")
        return False
    names = ["context_valid", "outer_guest", "nested_kvm", "workspace_root", "workspace_virtiofs", "host_state_excluded", "tools"]
    if require_child_memory_budget:
        names.append("memory_budget")
    tools = parsed.get("tools", {})
    for name in names:
        passed = checks.get(name) is True
        if passed:
            detail = "check passed"
        elif name == "tools" and isinstance(tools, dict):
            missing = sorted(key for key, available in tools.items() if available is not True)
            detail = f"missing or unavailable: {', '.join(missing) or 'tool inventory incomplete'}; no child was started"
        elif name == "memory_budget":
            budget = parsed.get("memory_budget", {})
            if not isinstance(budget, dict):
                budget = {}
            cgroup = budget.get("cgroup_remaining", "unavailable")
            detail = (
                f"{budget.get('error_category', 'memory_budget_failed')}; "
                f"MemAvailable={budget.get('memavailable_mib', 'unknown')} MiB, "
                f"cgroup remaining={cgroup}, "
                f"releasable QEMU RSS={budget.get('releasable_qemu_rss_mib', 'unknown')} MiB, "
                f"projected={budget.get('projected_available_mib', 'unknown')} MiB, "
                f"requested={budget.get('requested_mib', 'unknown')} MiB + "
                f"reserve={budget.get('reserve_mib', 'unknown')} MiB, "
                f"budget command rc={budget.get('command_returncode', 'unknown')}; "
                "no child was started"
            )
        elif name == "context_valid" and isinstance(context_probe, dict):
            detail = (
                f"current-source context {context_probe.get('failure_category', 'unavailable')}; "
                f"command rc={context_probe.get('returncode', 'unknown')}; "
                f"stdout-present={context_probe.get('stdout_present') is True}; "
                f"stderr-present={context_probe.get('stderr_present') is True}; no child was started"
            )
        else:
            detail = "failed; no child was started"
        evidence.check(f"outer guest {name}", passed, detail)
    evidence.data["outer_guest_tools"] = tools
    evidence.data["nested_memory_budget"] = parsed.get("memory_budget", {})
    if not require_child_memory_budget:
        evidence.data["nested_memory_budget_skipped_reason"] = (
            "Reusing an already-running child; this continuation starts no QEMU process."
        )
        print("INFO outer guest memory budget: no new VM will be started; existing-child continuation.", flush=True)
    evidence.save()
    return context_ok and all(checks.get(name) is True for name in names)


def run_preflight(
    evidence: Evidence,
    outer_profile: str,
    snapshot: Path,
    repo: Path = ROOT,
    *,
    allow_active_source_overlay: bool = False,
    require_child_memory_budget: bool = True,
    allow_outer_create: bool = True,
    allow_outer_discovery: bool = True,
    read_only: bool = False,
) -> tuple[bool, Path | None]:
    checkout_ok = repo.is_dir() and (repo / ".git").exists() and (repo / "lab-vm").is_file()
    evidence.check("checkout", checkout_ok, "repository and lab-vm found" if checkout_ok else "run from the owner checkout")
    cwd_ok = Path.cwd().resolve() == repo.resolve()
    evidence.check("working directory", cwd_ok, "at repository root" if cwd_ok else "run from the owner checkout root")
    revision = run(["git", "rev-parse", "--short", "HEAD"], cwd=repo)
    if revision.returncode == 0:
        evidence.data["revision"] = revision.stdout.strip()
        status = run(["git", "status", "--porcelain"], cwd=repo)
        evidence.data["checkout_dirty"] = bool(status.stdout.strip())
    context = host_context(evidence)
    tools_ok = host_tools(evidence)
    host_ok = (context is not None and context.get("runtime_kind") == "host"
               and context.get("device_present") is True and context.get("device_read_write") is True
               and context.get("nested_kvm_preflight_ready") is True and bool(context.get("cpu_virtualization")))
    checkout_ready = checkout_ok and cwd_ok and revision.returncode == 0
    prerequisites_ready = host_ok and tools_ok and checkout_ready
    if prerequisites_ready:
        snapshot_ok = snapshot_checks(
            snapshot, evidence, repo,
            allow_active_source_overlay=allow_active_source_overlay,
            allow_repair=not read_only,
        )
    else:
        evidence.check("snapshot preparation", False, "owner-host, checkout, and VM-tool prerequisites are required; no snapshot/profile changes attempted")
        snapshot_ok = False

    selected_profile = outer_profile
    outer: Path | None = None
    if prerequisites_ready and snapshot_ok:
        selected = ensure_outer_profile(
            outer_profile, snapshot, repo, evidence,
            allow_create=allow_outer_create,
            allow_discovery=allow_outer_discovery,
        )
        if selected is not None:
            selected_profile, _directory = selected
            outer = outer_profile_checks(selected_profile, snapshot, repo, evidence)
    guest_ok = outer_guest_preflight(
        outer, evidence,
        repo=repo,
        require_child_memory_budget=require_child_memory_budget,
        allow_tool_bootstrap=not read_only,
    ) if outer is not None else False
    if not host_ok:
        evidence.check("outer guest access", False, "host/KVM context is not ready; no SSH or VM action attempted")
    elif outer is None:
        evidence.check("outer guest access", False, "safe outer profile could not be prepared or verified; no SSH attempted")
    evidence.data["outer_profile"] = selected_profile
    evidence.data["snapshot"] = "~/.cache/lab-vm-recursion/initial-launcher-smoke/workspace"
    evidence.save()
    ok = checkout_ready and host_ok and tools_ok and snapshot_ok and outer is not None and guest_ok
    return ok, outer


def terminate_local_ssh(process: subprocess.Popen[str]) -> None:
    """Close the runner's SSH client without sending a VM teardown command."""
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def parse_remote_progress(line: str) -> dict[str, object] | None:
    if not line.startswith(REMOTE_PROGRESS_MARKER):
        return None
    try:
        event = json.loads(line[len(REMOTE_PROGRESS_MARKER):])
    except json.JSONDecodeError:
        return {}
    if (not isinstance(event, dict)
            or event.get("stage") not in REMOTE_PROGRESS_STAGES
            or event.get("status") not in REMOTE_PROGRESS_STATUSES):
        return {}
    safe: dict[str, object] = {"stage": event["stage"], "status": event["status"]}
    for key in ("timeout_seconds", "returncode", "elapsed_seconds", "silence_seconds"):
        value = event.get(key)
        if type(value) is int and value >= 0:
            safe[key] = value
    for key in ("passed", "private_overlay", "stderr_present"):
        value = event.get(key)
        if isinstance(value, bool):
            safe[key] = value
    reason = event.get("reason")
    if isinstance(reason, str) and reason in {
        "profile_state_missing_or_unsafe", "ssh_controls_missing_or_unsafe",
        "ssh_port_invalid", "ssh_port_out_of_range",
    }:
        safe["reason"] = reason
    return safe


def record_remote_progress(evidence: Evidence, event: dict[str, object]) -> None:
    history = evidence.data.setdefault("nested_progress", [])
    if not isinstance(history, list):
        history = []
        evidence.data["nested_progress"] = history
    history.append({"observed_at": utc_now(), **event})
    if len(history) > 120:
        del history[:-120]
    evidence.save()
    details = []
    for key, label in (
        ("timeout_seconds", "timeout"), ("returncode", "rc"),
        ("elapsed_seconds", "elapsed"), ("silence_seconds", "quiet"),
        ("passed", "passed"), ("private_overlay", "private-overlay"),
        ("stderr_present", "stderr-present"), ("reason", "reason"),
    ):
        if key in event:
            value = event[key]
            if key in ("timeout_seconds", "elapsed_seconds", "silence_seconds"):
                value = f"{value}s"
            details.append(f"{label}={value}")
    suffix = " " + " ".join(details) if details else ""
    print(f"PROGRESS {event['stage']}: {event['status']}{suffix}", flush=True)


def run_nested_remote(
    vm_dir: Path,
    profile: str,
    probe: str,
    evidence: Evidence,
    repo: Path = ROOT,
    *,
    resume_existing: bool = False,
) -> tuple[int, dict[str, object] | None]:
    # Always create a current-source child workspace. The snapshot may be an
    # active virtiofs export, so neither the outer workspace nor an existing
    # child's source tree is repaired in place.
    try:
        source_payload = active_source_overlay_payload(repo)
        remote_script = build_remote_nested_script(repo, True, source_payload)
    except (AcceptanceError, OSError) as exc:
        evidence.check(
            "current-source nested workspace", False,
            f"could not prepare verified host sources ({type(exc).__name__}); no child was started",
        )
        return 2, {"checks": {}, "failure": "runtime source overlay could not be prepared; no child was started"}
    evidence.data["runtime_source_overlay_files"] = {
        relative: item["sha256"] for relative, item in source_payload.items()
    }
    evidence.save()
    resume_arg = " --resume-existing" if resume_existing else ""
    command = ssh_command(vm_dir, f"cd /workspace && python3 - {profile} {probe}{resume_arg}")
    process = subprocess.Popen(command, cwd=repo, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert process.stdin is not None and process.stdout is not None
    try:
        process.stdin.write(remote_script)
        process.stdin.close()
    except BrokenPipeError:
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
    marker = REMOTE_SUMMARY_MARKER
    summary: dict[str, object] | None = None
    output_lines: Queue[str | None] = Queue()

    def read_output() -> None:
        assert process.stdout is not None
        for output_line in process.stdout:
            output_lines.put(output_line)
        output_lines.put(None)

    reader = Thread(target=read_output, daemon=True)
    reader.start()
    started = time.monotonic()
    deadline = started + REMOTE_NESTED_TIMEOUT_SECONDS
    last_output = started
    last_stage = "outer_ssh_startup"
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                elapsed = int(time.monotonic() - started)
                record_remote_progress(evidence, {
                    "stage": last_stage,
                    "status": "timed_out",
                    "timeout_seconds": REMOTE_NESTED_TIMEOUT_SECONDS,
                    "elapsed_seconds": elapsed,
                })
                terminate_local_ssh(process)
                return 124, {
                    "checks": {},
                    "failure": "nested acceptance exceeded its overall deadline; preserve outer and child state",
                }
            try:
                line = output_lines.get(timeout=min(30, remaining))
            except Empty:
                now = time.monotonic()
                silence = int(now - last_output)
                record_remote_progress(evidence, {
                    "stage": last_stage,
                    "status": "waiting",
                    "elapsed_seconds": int(now - started),
                    "silence_seconds": silence,
                })
                continue
            if line is None:
                break
            last_output = time.monotonic()
            event = parse_remote_progress(line)
            if event is not None:
                if event:
                    last_stage = str(event["stage"])
                    record_remote_progress(evidence, event)
                else:
                    print("Ignoring malformed remote progress event.", flush=True)
                continue
            if line.startswith(marker):
                try:
                    value = json.loads(line[len(marker):])
                    summary = value if isinstance(value, dict) else None
                except json.JSONDecodeError:
                    summary = None
                if summary is not None:
                    # The remote script emits this marker only as its terminal
                    # result. Close our SSH client now instead of waiting for
                    # EOF from unrelated descendants that may inherit stdout.
                    terminate_local_ssh(process)
                    return 0, summary
            else:
                sys.stdout.write(line)
                sys.stdout.flush()
    except KeyboardInterrupt:
        # Interrupt only the local SSH client. The nested VM is intentionally
        # left running so its profile and diagnostics remain available.
        record_remote_progress(evidence, {
            "stage": last_stage,
            "status": "interrupted",
            "elapsed_seconds": int(time.monotonic() - started),
        })
        terminate_local_ssh(process)
        raise
    return process.wait(), summary


def build_remote_nested_script(
    repo: Path = ROOT,
    use_source_overlay: bool = False,
    source_payload: dict[str, dict[str, object]] | None = None,
) -> str:
    payload = source_payload
    if payload is None:
        payload = active_source_overlay_payload(repo) if use_source_overlay else {}
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"SOURCE_OVERLAY_PAYLOAD_JSON = {payload_json!r}\n" + REMOTE_NESTED


def nested_gate(
    profile: str | None,
    outer_profile: str,
    snapshot: Path,
    repo: Path,
    *,
    reuse_existing: bool = False,
    require_existing_outer: bool = False,
) -> int:
    command = "nested-resume" if reuse_existing else "nested"
    objective = (
        "read-only revalidation of one existing nested child, depth-1 readiness, nested QEMU/network isolation, depth-2 refusal before state, and parent continuity; no snapshot repair, guest-tool install, profile creation, new QEMU, or teardown"
        if reuse_existing else
        "one named disposable nested VM boot, depth-1 readiness, nested QEMU/network isolation, depth-2 refusal before state, and parent continuity; no model/TUI/auth/workload/teardown"
    )
    evidence = Evidence(command, objective)
    if reuse_existing and profile is None:
        evidence.check("existing child profile", False, "--reuse-child requires a profile name")
        evidence.finish("FAIL")
        return 2
    if profile is not None and not PROFILE_RE.fullmatch(profile):
        evidence.check("child profile name", False, "invalid profile name")
        evidence.finish("FAIL")
        return 2
    ready, outer = run_preflight(
        evidence, outer_profile, snapshot, repo,
        allow_active_source_overlay=True,
        require_child_memory_budget=not reuse_existing,
        allow_outer_create=not (require_existing_outer or reuse_existing),
        allow_outer_discovery=False,
        read_only=reuse_existing,
    )
    if not ready or outer is None:
        evidence.check(command, False, "preflight blocked; existing child was not changed")
        evidence.finish("BLOCKED")
        return 2
    if not run_isolation_check(outer.name, evidence, "outer", repo):
        evidence.check(command, False, "outer QEMU/listener or guest network boundary failed; child was not changed")
        evidence.finish("FAIL")
        return 1
    if profile is None:
        profile = f"nested-accept-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    evidence.data["child_profile"] = profile
    evidence.data["reused_existing_child"] = reuse_existing
    evidence.save()
    probe = f"depth-cap-{uuid.uuid4().hex[:10]}"
    evidence.data["depth_limit_probe"] = probe
    evidence.save()
    rc, summary = run_nested_remote(
        outer, profile, probe, evidence, repo, resume_existing=reuse_existing,
    )
    checks = summary.get("checks", {}) if isinstance(summary, dict) else {}
    evidence.data["nested_summary"] = summary or {"result": "missing remote summary"}
    if isinstance(checks, dict):
        if reuse_existing:
            existing_ok = checks.get("existing_profile") is True
            evidence.check(
                "existing child profile",
                existing_ok,
                "private existing child profile verified" if existing_ok else "profile missing or unsafe; no VM was started",
            )
        for name, detail in (
            ("child_up", "existing child answered SSH; no VM was started" if reuse_existing else "new child profile provisioned"),
            ("child_acceptance", "cloud-init, recursion policy, KVM and safe workspace passed"),
            ("nested_isolation", "child QEMU restrict/forwarding and guest network-boundary probes passed; model availability skipped"),
            ("depth_limit_refused_before_state", "depth-2 launch refused before profile state"),
            ("outer_session_usable", "outer guest remained usable"),
        ):
            label = name.replace("_", " ")
            if name not in checks:
                evidence.blocked(label, "not reached after an earlier nested-gate failure; preserve state and stop")
            else:
                evidence.check(label, checks[name] is True,
                               detail if checks[name] is True else "failed; preserve child state and stop")
    required = ["child_up", "child_acceptance", "nested_isolation", "depth_limit_refused_before_state", "outer_session_usable"]
    if reuse_existing:
        required.append("existing_profile")
    passed = rc == 0 and isinstance(checks, dict) and all(checks.get(k) is True for k in required)
    if rc != 0 and summary is None:
        evidence.check("nested runner transport", False, "SSH gate stopped before a summary; preserve outer/child diagnostics")
    evidence.finish("PASS" if passed else "FAIL")
    return 0 if passed else (rc or 1)


def classify_interrupted_process(args: list[str]) -> str | None:
    """Match only the acceptance runner and its exact nested-probe SSH command."""
    if not args:
        return None
    executable = Path(args[0]).name
    if (executable.startswith("python")
            and any("vm-host-acceptance.py" in arg for arg in args)
            and "nested" in args):
        return "nested-acceptance-runner"
    if executable != "ssh" or "mester@127.0.0.1" not in args:
        return None
    remote = args[-1].split()
    if (len(remote) in (7, 8) and remote[:5] == ["cd", "/workspace", "&&", "python3", "-"]
            and PROFILE_RE.fullmatch(remote[5])
            and re.fullmatch(r"depth-cap-[0-9a-f]{10}", remote[6])
            and (len(remote) == 7 or remote[7] == "--resume-existing")):
        return "nested-acceptance-ssh-to-outer"
    return None


def interrupted_processes() -> list[dict[str, str]]:
    """Describe matching local runner/SSH processes without exposing argv or signaling them."""
    found: list[dict[str, str]] = []
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdecimal() or int(entry.name) == os.getpid():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            args = [part.decode("utf-8", "replace") for part in (entry / "cmdline").read_bytes().split(b"\0") if part]
        except OSError:
            continue
        if not args:
            continue
        kind = classify_interrupted_process(args)
        if kind is None:
            continue
        try:
            details = subprocess.run(
                ["ps", "-o", "pid=,ppid=,stat=,etime=", "-p", entry.name],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                check=False, timeout=2,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            details = f"pid={entry.name} status=unavailable"
        found.append({"kind": kind, "details": details or f"pid={entry.name} status=exited"})
    return found


def print_report_diagnostics(path: Path, report: dict[str, object]) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    print(f"Report: {path} (mode {mode:04o})")
    if mode & 0o077:
        print("WARNING: report permissions are broader than 0600; do not share the file.")
    print(f"Started: {report.get('started_at', 'unknown')}")
    print(f"Finished: {report.get('finished_at', 'not recorded')}")
    print(f"Outcome: {report.get('outcome', 'interrupted or unfinished')}")
    checks = report.get("checks", [])
    if isinstance(checks, list) and checks:
        print("Recorded checks:")
        for item in checks:
            if isinstance(item, dict):
                print(f"  {item.get('result', 'UNKNOWN')}: {item.get('name', 'unnamed check')}")
    else:
        print("Recorded checks: none")

    budget = report.get("nested_memory_budget")
    if isinstance(budget, dict) and budget:
        print("Outer guest memory budget:")
        numeric_fields = (
            ("memavailable_mib", "MemAvailable MiB"),
            ("cgroup_remaining_mib", "cgroup remaining MiB"),
            ("releasable_qemu_rss_mib", "releasable QEMU RSS MiB"),
            ("projected_available_mib", "projected available MiB"),
            ("available_mib", "available MiB"),
            ("requested_mib", "requested MiB"),
            ("reserve_mib", "reserve MiB"),
            ("command_returncode", "command return code"),
        )
        for key, label in numeric_fields:
            value = budget.get(key)
            if type(value) is int:
                print(f"  {label}: {value}")
        cgroup = budget.get("cgroup_remaining")
        if isinstance(cgroup, str) and re.fullmatch(r"(?:unlimited|unavailable|[0-9]+ MiB)", cgroup):
            print(f"  cgroup remaining: {cgroup}")
        category = budget.get("error_category")
        if category in ("insufficient_memory_budget", "memory_budget_measurement_unavailable", "memory_budget_output_unparseable"):
            print(f"  result: {category}")
        if isinstance(budget.get("measurement_parsed"), bool):
            print(f"  measurement parsed: {'yes' if budget['measurement_parsed'] else 'no'}")
        if isinstance(budget.get("stderr_present"), bool):
            print(f"  stderr present: {'yes' if budget['stderr_present'] else 'no'}")

    progress = report.get("nested_progress")
    if isinstance(progress, list) and progress:
        print("Recent remote stage events:")
        for event in progress[-12:]:
            if not isinstance(event, dict):
                continue
            stage, status = event.get("stage"), event.get("status")
            if not isinstance(stage, str) or stage not in REMOTE_PROGRESS_STAGES:
                continue
            if not isinstance(status, str) or status not in REMOTE_PROGRESS_STATUSES:
                continue
            fields = []
            for key in ("returncode", "timeout_seconds", "elapsed_seconds", "silence_seconds"):
                value = event.get(key)
                if type(value) is int and value >= 0:
                    fields.append(f"{key}={value}")
            at = event.get("observed_at")
            timestamp = at if isinstance(at, str) and re.fullmatch(r"[0-9T:+.-]+", at) else "time-unknown"
            suffix = " " + " ".join(fields) if fields else ""
            print(f"  {timestamp} {stage}: {status}{suffix}")

    nested = report.get("nested_summary")
    nested_checks = nested.get("checks") if isinstance(nested, dict) else None
    if isinstance(nested_checks, dict):
        print("Remote nested checks:")
        for name, result in nested_checks.items():
            if isinstance(name, str):
                print(f"  {'PASS' if result is True else 'FAIL' if result is False else 'UNKNOWN'}: {name}")
    child = nested.get("child") if isinstance(nested, dict) else None
    if isinstance(child, dict):
        stage = child.get("failed_stage")
        if isinstance(stage, str) and stage in REMOTE_PROGRESS_STAGES:
            print(f"Child readiness failed stage: {stage}")
        result = child.get("result")
        if result in ("timed out", "no structured readiness response"):
            print(f"Child readiness result: {result}")
        child_checks = child.get("checks")
        if isinstance(child_checks, dict):
            print("Child readiness checks:")
            for name, result in child_checks.items():
                if isinstance(name, str) and isinstance(result, bool):
                    print(f"  {'PASS' if result else 'FAIL'}: {name}")


def inspect_interrupted(profile: str, state_dir: Path | None = None) -> int:
    """Read a partial private report and local process state; never changes or kills anything."""
    if not PROFILE_RE.fullmatch(profile):
        print("Invalid profile name.", file=sys.stderr)
        return 2
    directory = state_dir or (Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
                              / "little-mester" / "host-acceptance")
    print(f"Inspection target: {profile}")
    report_matches: list[tuple[float, Path, dict[str, object]]] = []
    if directory.is_symlink() or not directory.is_dir():
        print("No private host-acceptance report directory found.")
    else:
        for path in directory.glob("vm-acceptance-*.json"):
            try:
                metadata = path.lstat()
                if path.is_symlink() or not path.is_file() or metadata.st_uid != os.getuid():
                    continue
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if (isinstance(report, dict) and report.get("command") == "nested"
                    and report.get("child_profile") == profile):
                report_matches.append((metadata.st_mtime, path, report))
    if report_matches:
        _, path, report = max(report_matches, key=lambda item: item[0])
        print_report_diagnostics(path, report)
    else:
        print("No matching partial/final report found for this child profile.")

    processes = interrupted_processes()
    relevant = [item for item in processes if item["kind"] in (
        "nested-acceptance-runner", "nested-acceptance-ssh-to-outer",
    )]
    if relevant:
        print("Matching local processes (inspection only; none were signaled):")
        for item in relevant:
            print(f"  {item['kind']}: {item['details']}")
    else:
        print("No matching local acceptance runner or outer-guest SSH process found.")
    print("Read-only inspection complete. No VM, process, report, or workspace state was changed.")
    return 0


def inspect_report(report_path: Path) -> int:
    """Display allowlisted diagnostics from one private report, including preflight-only reports."""
    try:
        metadata = report_path.lstat()
        if report_path.is_symlink() or not report_path.is_file() or metadata.st_uid != os.getuid():
            print("Refusing report that is not a regular file owned by this user.", file=sys.stderr)
            return 2
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"Could not read report: {type(exc).__name__}", file=sys.stderr)
        return 2
    if not isinstance(report, dict) or report.get("command") != "nested":
        print("Report is not a nested acceptance report.", file=sys.stderr)
        return 2
    print_report_diagnostics(report_path, report)
    print("Read-only report inspection complete; no state was changed.")
    return 0


OBSERVATIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "tui": (
        "Record only when the latest #41/#42 handoff permits it. In this direct guest session, compare built-in/custom dialogs with host-Docker in the same terminal, exercise cancel/confirm, select both configured local providers (Ollama and llama.cpp), complete repeated streams on each, and verify metrics. Do not restart QEMU or alter the preserved overlay.",
        ("built_in_dialog_legible", "custom_dialog_legible", "exit_cancel_stays_in_tui", "exit_confirm_exits", "same_terminal_comparison_done", "metrics_displayed", "ollama_completion_succeeded", "llamacpp_completion_succeeded", "repeated_streams_no_socket_disconnects"),
    ),
    "auth": (
        "Record only when the latest #41/#45 handoff permits it. Never enter or save OAuth URLs, state, codes, tokens, prompts, or raw logs.",
        ("browser_callback_completed", "auth_list_openai_stored", "approved_provider_completion_succeeded", "cancellation_cleanup_checked", "interruption_cleanup_checked", "occupied_callback_port_failed_closed"),
    ),
    "permissions": (
        "Record only when the latest #35/#50 prerequisites pass. First test shared-service `--auto` in the disposable project, then repeat the same harmless allow and exact deny in a separate `--standalone --auto` session. Record whether the behavior differs and what it isolates; do not add blanket allows or recreate the host container.",
        ("displayed_mode_matches_requested", "shared_harmless_tool_behavior_correct", "shared_explicit_deny_still_blocks", "disposable_project_used", "no_blanket_allow_added"),
    ),
    "permissions_standalone": (
        "Repeat the exact harmless allow and explicit deny in `opencode --standalone --auto`, using the same disposable project/config and OpenCode version. Compare with the preceding shared-service run and record the difference; do not change policy or add blanket allows.",
        ("standalone_harmless_tool_behavior_correct", "standalone_explicit_deny_still_blocks", "shared_vs_standalone_difference_recorded", "no_blanket_allow_added"),
    ),
    "workload": (
        "From the disposable guest TUI, use only the generated /workspace/compose.yaml: start PostgreSQL, wait for health, query 42, check the selected-workspace bind mount and named volume, verify the guest-loopback-only port, restart/query again, then run compose down -v. The owner host must not expose that guest port; do not touch another project.",
        ("agent_compose_started_stack", "agent_health_and_query_succeeded", "agent_restart_preserved_service", "agent_tore_down_stack", "guest_loopback_port_only", "bind_mount_and_named_volume_work", "no_physical_host_port_exposed"),
    ),
}


def prompt_result(label: str) -> str:
    while True:
        value = input(f"  {label} [y/n/u=unknown]: ").strip().lower()
        if value in ("y", "n", "u"):
            return {"y": "pass", "n": "fail", "u": "unknown"}[value]
        print("Enter y, n, or u.")


def observe_gate(
    gate: str, evidence: Evidence | None = None, *, opencode_argv: list[str] | None = None,
) -> int:
    instruction, questions = OBSERVATIONS[gate]
    owns_evidence = evidence is None
    if evidence is None:
        evidence = Evidence("observe", f"human-reported {gate} observations; not a ticket update or independent acceptance proof")
    print(instruction)
    if prompt_result("Have you followed the current ticket handoff and prerequisites?") != "pass":
        evidence.blocked("observation prerequisites", "operator did not confirm the current ordered handoff")
        if owns_evidence:
            evidence.finish("BLOCKED")
        return 2
    observation: dict[str, str] = {"gate": gate, "recorded_at": utc_now()}
    if gate == "auth" or gate.startswith("permissions"):
        while True:
            version = input("OpenCode version (for example v2.2.5; no other text): ").strip()
            if VERSION_RE.fullmatch(version):
                observation["opencode_version"] = version
                break
        print("Enter only a version; do not paste logs or other content.")
    if gate == "auth":
        while True:
            method = input("Observed auth method [host-browser-bridge/other]: ").strip().lower()
            if method in ("host-browser-bridge", "other"):
                observation["auth_method"] = method
                break
            print("Enter host-browser-bridge or other; do not paste URLs, codes, or tokens.")
    if gate.startswith("permissions"):
        while True:
            mode = input("Displayed/requested permission mode [ask/auto]: ").strip().lower()
            if mode in ("ask", "auto"):
                observation["permission_mode"] = mode
                break
            print("Enter ask or auto.")
        if opencode_argv is not None:
            observation["opencode_argv"] = list(opencode_argv)
    for question in questions:
        observation[question] = prompt_result(question.replace("_", " "))
    observation["sensitive_material_recorded"] = "no"
    observations = evidence.data["observations"]
    assert isinstance(observations, list)
    observations.append(observation)
    result = human_observation_result(evidence, gate)
    if result == "PASS":
        evidence.check("human observation", True, "all requested observations and mode/version checks passed")
    elif result == "FAIL":
        evidence.check("human observation", False, "a required observation or mode/version check failed")
    else:
        evidence.blocked("human observation", "one or more required observations or mode/version checks remain unknown")
    if owns_evidence:
        evidence.finish("REPORTED_PASS" if result == "PASS" else "REPORTED_INCOMPLETE")
    return 0 if result == "PASS" else 1


def human_observation_result(evidence: Evidence, gate: str) -> str:
    observations = evidence.data.get("observations", [])
    if not isinstance(observations, list):
        return "BLOCKED"
    matching = [item for item in observations if isinstance(item, dict) and item.get("gate") == gate]
    if not matching:
        return "BLOCKED"
    observation = matching[-1]
    questions = OBSERVATIONS[gate][1]
    values = [observation.get(question) for question in questions]
    if any(value == "fail" for value in values):
        return "FAIL"
    if any(value != "pass" for value in values):
        return "BLOCKED"
    if gate == "auth":
        if observation.get("auth_method") == "other":
            return "FAIL"
        if observation.get("auth_method") != "host-browser-bridge":
            return "BLOCKED"
        if not VERSION_RE.fullmatch(str(observation.get("opencode_version", ""))):
            return "BLOCKED"
    if gate.startswith("permissions"):
        expected_argv = ["opencode", "--auto"] if gate == "permissions" else ["opencode", "--standalone", "--auto"]
        if observation.get("permission_mode") != "auto" or observation.get("opencode_argv") != expected_argv:
            return "FAIL"
        if not VERSION_RE.fullmatch(str(observation.get("opencode_version", ""))):
            return "BLOCKED"
    return "PASS"


def permission_versions_match(evidence: Evidence) -> bool:
    observations = evidence.data.get("observations", [])
    if not isinstance(observations, list):
        return False
    matching = [
        item for item in observations
        if isinstance(item, dict) and item.get("gate") in ("permissions", "permissions_standalone")
    ]
    gates = [item.get("gate") for item in matching]
    versions = {item.get("opencode_version") for item in matching}
    return (
        len(matching) == 2
        and set(gates) == {"permissions", "permissions_standalone"}
        and len(versions) == 1
    )


def vm_state_root() -> Path:
    explicit = os.environ.get("VM_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(data_home).expanduser() / "little-mester"


def campaign_guest_port(profile: str) -> int:
    if not PROFILE_RE.fullmatch(profile):
        raise AcceptanceError("Invalid disposable Compose project name.")
    return 20000 + int(hashlib.sha256(profile.encode("utf-8")).hexdigest()[:8], 16) % 20000


def host_port_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def host_port_unreachable(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(2)
            result = probe.connect_ex(("127.0.0.1", port))
        return result == errno.ECONNREFUSED
    except TimeoutError:
        return False
    except OSError as exc:
        return exc.errno == errno.ECONNREFUSED


def write_acceptance_project(workspace: Path, profile: str) -> None:
    if not PROFILE_RE.fullmatch(profile):
        raise AcceptanceError("Invalid disposable project profile name.")
    port = campaign_guest_port(profile)
    bind_dir = workspace / "acceptance-bind"
    bind_dir.mkdir(mode=0o700, exist_ok=True)
    marker = bind_dir / "marker.txt"
    marker.write_text(f"VM_ACCEPTANCE_BIND_{profile}\n", encoding="utf-8")
    os.chmod(bind_dir, 0o700)
    os.chmod(marker, 0o600)

    compose = f'''services:
  db:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_PASSWORD: acceptance-only-not-a-secret
    ports:
      - "127.0.0.1:{port}:5432"
    volumes:
      - pg-data:/var/lib/postgresql/data
      - ./acceptance-bind:/acceptance-bind:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 2s
      timeout: 2s
      retries: 45
volumes:
  pg-data:
'''
    project_config = {
        "$schema": "https://opencode.ai/config.json",
        "permissions": [
            {"action": "shell", "resource": "pwd", "effect": "ask"},
            {"action": "shell", "resource": "echo VM_ACCEPTANCE_DENIED", "effect": "deny"},
        ],
    }
    (workspace / "compose.yaml").write_text(compose, encoding="utf-8")
    (workspace / "opencode.json").write_text(json.dumps(project_config, indent=2) + "\n", encoding="utf-8")
    os.chmod(workspace / "compose.yaml", 0o600)
    os.chmod(workspace / "opencode.json", 0o600)


def private_acceptance_workspace(profile: str) -> Path:
    if not PROFILE_RE.fullmatch(profile):
        raise AcceptanceError("Invalid disposable workspace profile name.")
    root = Path.home() / ".cache/little-mester/host-acceptance"
    if root.is_symlink() or path_has_symlink(root):
        raise AcceptanceError("Disposable workspace root contains a symlink.")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.is_dir() or root.stat().st_uid != os.getuid():
        raise AcceptanceError("Disposable workspace root is not a user-owned directory.")
    os.chmod(root, 0o700)
    workspace = root / f"{profile}-workspace"
    workspace.mkdir(mode=0o700)
    os.chmod(workspace, 0o700)
    if workspace.resolve().is_relative_to(ROOT.resolve()):
        raise AcceptanceError("Disposable workspace must remain outside the source checkout.")

    write_acceptance_project(workspace, profile)
    return workspace


def private_recursive_source_workspace(profile: str, repo: Path = ROOT) -> tuple[Path, Path]:
    if not PROFILE_RE.fullmatch(profile):
        raise AcceptanceError("Invalid disposable recursive workspace profile name.")
    if not recursive_source_checkout_clean(repo):
        raise AcceptanceError("Recursive proof requires a clean committed source checkout; no source copy or VM was created.")
    root = Path.home() / ".cache/little-mester/host-acceptance"
    if root.is_symlink() or path_has_symlink(root):
        raise AcceptanceError("Recursive source workspace root contains a symlink.")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.is_dir() or root.stat().st_uid != os.getuid():
        raise AcceptanceError("Recursive source workspace root is not a user-owned directory.")
    os.chmod(root, 0o700)
    session = root / f"{profile}-recursive-source"
    if lexists(session):
        raise AcceptanceError("Unique recursive source workspace already exists; refusing to reuse it.")
    result = run([
        sys.executable, str(repo / "scripts/vm-recursion-workspace.py"),
        "prepare", "--source", str(repo.resolve()), "--session", str(session),
    ], timeout=600)
    if result.returncode != 0:
        raise AcceptanceError("Could not create the revision-bound private recursive source snapshot.")
    workspace = Path((result.stdout or "").strip())
    if (not workspace.is_absolute() or workspace.resolve().parent != session.resolve()
            or workspace.name != "workspace" or not campaign_project_dir_safe(workspace)):
        raise AcceptanceError("Recursive source snapshot resolved outside its unique private session.")
    write_acceptance_project(workspace, profile)
    return workspace, session


def recursive_source_checkout_clean(repo: Path = ROOT) -> bool:
    status = run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"])
    return status.returncode == 0 and not (status.stdout or "").strip()


def private_campaign_escrow(profile: str, state_root: Path | None = None) -> Path:
    if not PROFILE_RE.fullmatch(profile):
        raise AcceptanceError("Invalid disposable credential escrow profile name.")
    directory = (state_root or vm_state_root()) / "secrets"
    if directory.is_symlink() or path_has_symlink(directory):
        raise AcceptanceError("Host credential escrow directory contains a symlink.")
    directory = directory.resolve()
    workspace = (Path.home() / ".cache/little-mester/host-acceptance" / f"{profile}-workspace").resolve()
    if directory.is_relative_to(ROOT.resolve()) or directory.is_relative_to(workspace):
        raise AcceptanceError("Host credential escrow directory must be outside the checkout and disposable workspace.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not directory.is_dir() or directory.stat().st_uid != os.getuid():
        raise AcceptanceError("Host credential escrow directory is not a user-owned directory.")
    os.chmod(directory, 0o700)
    target = directory / f"{profile}-auth.json"
    if lexists(target):
        raise AcceptanceError("Unique campaign credential escrow path already exists; refusing to overwrite it.")
    return target


def create_campaign_state_root() -> tuple[Path, Path]:
    """Create a fresh isolated VM state namespace; never use the shared base/profile root."""
    shared_root = vm_state_root().expanduser().absolute()
    if path_has_symlink(shared_root) or shared_root.resolve().is_relative_to(ROOT.resolve()):
        raise AcceptanceError("Shared VM state root is symlinked or inside the checkout; refusing campaign provisioning.")
    parent = shared_root / "host-acceptance"
    if parent.is_symlink() or path_has_symlink(parent):
        raise AcceptanceError("Campaign state parent contains a symlink.")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.stat().st_uid != os.getuid() or not parent.is_dir():
        raise AcceptanceError("Campaign state parent is not a user-owned directory.")
    os.chmod(parent, 0o700)
    root = parent / f"campaign-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:10]}"
    root.mkdir(mode=0o700)
    if root.stat().st_uid != os.getuid() or root.is_symlink():
        raise AcceptanceError("New campaign state root is not a safe user-owned directory.")
    return root, shared_root


def campaign_state_root_safe(root: Path, shared_root: Path) -> bool:
    try:
        parent_path = shared_root / "host-acceptance"
        parent_metadata = parent_path.lstat()
        metadata = root.lstat()
        parent = parent_path.resolve(strict=True)
        resolved = root.resolve(strict=True)
    except OSError:
        return False
    return (
        not path_has_symlink(parent_path)
        and stat.S_ISDIR(parent_metadata.st_mode)
        and parent_metadata.st_uid == os.getuid()
        and stat.S_IMODE(parent_metadata.st_mode) == 0o700
        and stat.S_ISDIR(metadata.st_mode) and not root.is_symlink()
        and metadata.st_uid == os.getuid() and stat.S_IMODE(metadata.st_mode) == 0o700
        and root.parent.resolve() == parent and resolved.parent == parent
        and re.fullmatch(r"campaign-[0-9]{14}-[0-9a-f]{10}", root.name) is not None
        and not resolved.is_relative_to(ROOT.resolve())
    )


def unique_campaign_profile(prefix: str, state_root: Path) -> str:
    safe_prefix = prefix if PROFILE_RE.fullmatch(prefix) else "vm-acceptance"
    for _ in range(10):
        name = f"{safe_prefix}-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        directory = state_root / "vm" / name
        if not lexists(directory):
            return name
    raise AcceptanceError("Could not allocate a fresh disposable campaign profile.")


def campaign_env(*, auth_forward: bool, escrow: Path | None = None,
                 state_root: Path | None = None, skip_model_isolation: bool = False) -> dict[str, str]:
    env = os.environ.copy()
    env["VM_AUTH_FORWARD"] = "1" if auth_forward else "0"
    # Campaign teardown is gated by its own explicit human confirmation; do
    # not inherit a shell-wide auto-accept setting for unrelated prompts.
    env["VM_YES"] = "0"
    env["LAB_YES"] = "0"
    if state_root is not None:
        env["VM_STATE_DIR"] = str(state_root)
        env["VM_DIR_BASE"] = str(state_root / "vm")
    if skip_model_isolation:
        env["VM_ISOLATION_SKIP_MODEL"] = "1"
    else:
        env.pop("VM_ISOLATION_SKIP_MODEL", None)
    if escrow is not None:
        env["VM_AUTH_ESCROW_FILE"] = str(escrow)
    else:
        env.pop("VM_AUTH_ESCROW_FILE", None)
    return env


def run_campaign_vm(args: list[str], *, auth_forward: bool = False,
                    escrow: Path | None = None, capture: bool = True,
                    timeout: float | None = None, state_root: Path | None = None,
                    skip_model_isolation: bool = False) -> subprocess.CompletedProcess[str]:
    return run(
        [str(LAB_VM), *args],
        env=campaign_env(
            auth_forward=auth_forward, escrow=escrow, state_root=state_root,
            skip_model_isolation=skip_model_isolation,
        ),
        capture=capture, no_input=True, timeout=timeout,
    )


def launch_campaign_agent(profile: str, evidence: Evidence, *, browser_auth: bool,
                          auto: bool = False, state_root: Path | None = None,
                          standalone: bool = False) -> bool:
    if standalone:
        if browser_auth:
            raise AcceptanceError("Standalone permission reproduction cannot start browser OAuth.")
        vm_dir = (state_root / "vm" / profile) if state_root is not None else profile_dir(profile, ROOT)
        try:
            policy = (vm_dir / "recursion_policy").read_text(encoding="ascii").split()
        except (OSError, UnicodeError):
            policy = ["0", "0"]
        if len(policy) < 2 or not all(value.isdecimal() for value in policy[:2]):
            evidence.check("standalone agent recursion policy", False, "profile policy was unavailable or invalid")
            return False
        cli_config = json.dumps({
            "plugins": [
                "/etc/opencode/plugins/little-mester-logo",
                "/etc/opencode/plugins/host-metrics-tui",
                "/etc/opencode/plugins/little-mester-exit-confirm",
            ],
            "keybinds": {"app.exit": "none", "prompt.clear": "none"},
        }, separators=(",", ":"))
        command = "set -eu; cd /workspace && . /etc/profile.d/mester.sh && " + (
            f"export LAB_VM_RECURSION_DEPTH={policy[0]} LAB_VM_MAX_DEPTH={policy[1]} "
            f"OPENCODE_CONFIG=/etc/opencode/opencode.json METRICS_URL=http://10.0.2.100:19532 "
            f"OPENCODE_CLI_CONFIG_CONTENT={shlex.quote(cli_config)} && "
            "exec opencode --standalone" + (" --auto" if auto else "")
        )
        ssh_args = ssh_command(vm_dir, command)
        ssh_args.insert(1, "-tt")
        print("\nStarting a direct guest OpenCode --standalone permission comparison; output is not captured or written to the report.", flush=True)
        result = run(ssh_args, env=campaign_env(auth_forward=False, state_root=state_root),
                     capture=False, no_input=False)
        passed = result.returncode == 0
        evidence.check(
            "standalone agent command", passed,
            "standalone guest OpenCode session exited successfully" if passed else f"standalone guest OpenCode session exited {result.returncode}; terminal output was not captured",
        )
        return passed

    command = [str(LAB_VM), "agent", profile]
    if auto:
        command.append("--auto")
    command.append("--browser-auth" if browser_auth else "--no-browser-auth")
    label = "browser-auth" if browser_auth else "direct guest"
    print(f"\nStarting {label} OpenCode session for profile {profile}; output is not captured or written to the report.", flush=True)
    if auto:
        print("The disposable project contains only an ask rule for `pwd` and one exact harmless shell deny; no blanket allow is configured.", flush=True)
    result = run(
        command, env=campaign_env(auth_forward=False, state_root=state_root),
        capture=False, no_input=False,
    )
    evidence.check(
        f"{label} agent command", result.returncode == 0,
        "guest OpenCode session exited successfully" if result.returncode == 0 else f"guest OpenCode session exited {result.returncode}; terminal output was not captured",
    )
    return result.returncode == 0


def run_guest_script(vm_dir: Path, script: str, *, timeout: float = 120,
                     stream: bool = False) -> tuple[bool, str, str]:
    try:
        result = subprocess.run(
            ssh_command(vm_dir, "bash -s"), input=script, text=True,
            capture_output=not stream, timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "", "timed_out"
    except (OSError, AcceptanceError):
        return False, "", "transport_error"
    return result.returncode == 0, result.stdout or "", f"exit_{result.returncode}"


def guest_openai_auth_stored(vm_dir: Path) -> tuple[bool | None, str]:
    try:
        result = subprocess.run(
            ssh_command(vm_dir, "opencode auth list"), text=True,
            capture_output=True, timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "timed_out"
    except (OSError, AcceptanceError):
        return None, "transport_error"
    if result.returncode != 0:
        return None, f"exit_{result.returncode}"
    lines = (result.stdout or "").splitlines()
    stored = any(
        "openai" in line.lower()
        and any(word in line.lower() for word in ("stored", "connected", "oauth", "chatgpt"))
        and "not connected" not in line.lower()
        for line in lines
    )
    return stored, "stored" if stored else "not_stored"


def escrow_file_is_private(path: Path) -> bool:
    try:
        metadata = path.lstat()
        parent = path.parent.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode) and not path.is_symlink()
        and metadata.st_uid == os.getuid() and stat.S_IMODE(metadata.st_mode) == 0o600
        and stat.S_ISDIR(parent.st_mode) and parent.st_uid == os.getuid()
        and stat.S_IMODE(parent.st_mode) == 0o700 and metadata.st_size > 0
    )


def compose_feedback_script(profile: str) -> str:
    if not PROFILE_RE.fullmatch(profile):
        raise AcceptanceError("Invalid disposable Compose project name.")
    port = campaign_guest_port(profile)
    bind_marker = f"VM_ACCEPTANCE_BIND_{profile}"
    template = r'''set -euo pipefail
cd /workspace
project='__PROFILE__'
guest_port='__PORT__'
bind_marker='__BIND_MARKER__'
docker() { command docker -H "unix:///run/user/$(id -u)/docker.sock" "$@"; }
compose() { docker compose --project-name "$project" -f /workspace/compose.yaml "$@"; }
test ! -S /var/run/docker.sock
test -S "/run/user/$(id -u)/docker.sock"
docker info --format '{{json .SecurityOptions}}' | python3 -c 'import json,sys; x=json.load(sys.stdin); raise SystemExit(0 if any("rootless" in str(v).lower() for v in x) else 1)'
if docker ps --format '{{.Image}}' | grep -qi 'little-mester-agent'; then
  echo 'agent container unexpectedly present' >&2
  exit 1
fi
compose config --quiet
compose up -d --wait --wait-timeout 120
container="$(compose ps -q db)"
test -n "$container"
bindings="$(docker inspect --format '{{json .HostConfig.PortBindings}}' "$container")"
python3 - "$bindings" "$guest_port" <<'PY'
import json, sys
value = json.loads(sys.argv[1])
expected = {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": sys.argv[2]}]}
if value != expected:
    raise SystemExit(1)
PY
mounts="$(docker inspect --format '{{json .Mounts}}' "$container")"
python3 - "$mounts" "$project" <<'PY'
import json, pathlib, sys
mounts = json.loads(sys.argv[1])
project = sys.argv[2]
binds = [m for m in mounts if m.get("Destination") == "/acceptance-bind"]
volumes = [m for m in mounts if m.get("Destination") == "/var/lib/postgresql/data"]
if len(binds) != 1 or binds[0].get("Type") != "bind" or pathlib.Path(binds[0].get("Source", "")).resolve() != pathlib.Path("/workspace/acceptance-bind"):
    raise SystemExit(1)
if len(volumes) != 1 or volumes[0].get("Type") != "volume" or volumes[0].get("Name") != f"{project}_pg-data":
    raise SystemExit(1)
PY
docker volume inspect "${project}_pg-data" >/dev/null
python3 - "$guest_port" <<'PY'
import socket, sys
with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=5):
    pass
PY
test "$(compose exec -T db cat /acceptance-bind/marker.txt | tr -d '[:space:]')" = "$bind_marker"
value="$(compose exec -T db psql -U postgres -d postgres -tAc 'SELECT 42' | tr -d '[:space:]')"
test "$value" = 42
printf 'WORKLOAD_QUERY_FIRST\n'
compose restart db
for attempt in $(seq 1 60); do
  health="$(docker inspect --format '{{.State.Health.Status}}' "$container" 2>/dev/null || true)"
  [[ "$health" == healthy ]] && break
  sleep 1
done
[[ "$(docker inspect --format '{{.State.Health.Status}}' "$container")" == healthy ]]
value="$(compose exec -T db psql -U postgres -d postgres -tAc 'SELECT 42' | tr -d '[:space:]')"
test "$value" = 42
printf 'WORKLOAD_RESTART_QUERY\n'
printf 'GUEST_LOOPBACK_PORT_ONLY\n'
printf 'BIND_MOUNT_AND_VOLUME_OK\n'
printf 'WORKLOAD_READY_FOR_HOST_BOUNDARY\n'
'''
    return template.replace("__PROFILE__", profile).replace("__PORT__", str(port)).replace("__BIND_MARKER__", bind_marker)


def compose_feedback_teardown_script(profile: str) -> str:
    if not PROFILE_RE.fullmatch(profile):
        raise AcceptanceError("Invalid disposable Compose project name.")
    template = r'''set -euo pipefail
cd /workspace
project='__PROFILE__'
docker() { command docker -H "unix:///run/user/$(id -u)/docker.sock" "$@"; }
compose() { docker compose --project-name "$project" -f /workspace/compose.yaml "$@"; }
compose down -v --remove-orphans
test -z "$(compose ps -q)"
test -z "$(docker network ls -q --filter "label=com.docker.compose.project=$project")"
test -z "$(docker volume ls -q --filter "label=com.docker.compose.project=$project")"
printf 'WORKLOAD_DOWN\n'
'''
    return template.replace("__PROFILE__", profile)


def lifecycle_workload_start_script(profile: str) -> str:
    if not PROFILE_RE.fullmatch(profile):
        raise AcceptanceError("Invalid disposable lifecycle profile name.")
    marker = f"/var/tmp/little-mester-acceptance-{profile}.state"
    template = r'''set -euo pipefail
cd /workspace
project='__PROFILE__'
marker='__MARKER__'
docker() { command docker -H "unix:///run/user/$(id -u)/docker.sock" "$@"; }
compose() { docker compose --project-name "$project" -f /workspace/compose.yaml "$@"; }
compose up -d --wait --wait-timeout 120
compose exec -T db psql -U postgres -d postgres -v ON_ERROR_STOP=1 -c 'CREATE TABLE IF NOT EXISTS lifecycle_probe (value integer PRIMARY KEY)'
compose exec -T db psql -U postgres -d postgres -v ON_ERROR_STOP=1 -c 'INSERT INTO lifecycle_probe(value) VALUES (271828) ON CONFLICT DO NOTHING'
printf 'overlay-state-__PROFILE__\n' >"$marker"
chmod 600 "$marker"
printf 'LIFECYCLE_DATA_WRITTEN\n'
'''
    return template.replace("__PROFILE__", profile).replace("__MARKER__", marker)


def lifecycle_workload_check_script(profile: str) -> str:
    if not PROFILE_RE.fullmatch(profile):
        raise AcceptanceError("Invalid disposable lifecycle profile name.")
    marker = f"/var/tmp/little-mester-acceptance-{profile}.state"
    template = r'''set -euo pipefail
cd /workspace
project='__PROFILE__'
marker='__MARKER__'
docker() { command docker -H "unix:///run/user/$(id -u)/docker.sock" "$@"; }
compose() { docker compose --project-name "$project" -f /workspace/compose.yaml "$@"; }
test "$(cat "$marker")" = 'overlay-state-__PROFILE__'
compose up -d --wait --wait-timeout 120
value="$(compose exec -T db psql -U postgres -d postgres -tAc 'SELECT count(*) FROM lifecycle_probe WHERE value=271828' | tr -d '[:space:]')"
test "$value" = 1
printf 'LIFECYCLE_DATA_PERSISTED\n'
'''
    return template.replace("__PROFILE__", profile).replace("__MARKER__", marker)


def campaign_project_dir_safe(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and not path.is_symlink() and metadata.st_uid == os.getuid()


def campaign_profile_storage_safe(repo: Path = ROOT) -> bool:
    base = profile_base(repo)
    if base.is_symlink() or path_has_symlink(base) or base.resolve().is_relative_to(repo.resolve()):
        return False
    anchor = base
    while not anchor.exists() and anchor != anchor.parent:
        anchor = anchor.parent
    if not anchor.is_dir() or anchor.stat().st_uid != os.getuid() or not os.access(anchor, os.W_OK | os.X_OK):
        return False
    return not base.exists() or (base.is_dir() and base.stat().st_uid == os.getuid())


def prerequisite_prompt_probe(repo: Path = ROOT) -> tuple[bool, str]:
    """Exercise the real missing-tool mapping/default-N path without invoking a package manager."""
    script = r'''set -u
source "$1/lib/vm/prereqs.sh"
log() { printf '%s\n' "$*" >&2; }
_vm_prereqs_detect_missing() { printf 'qemu-img\n'; }
sudo() { printf 'UNSAFE_SUDO_INVOKED\n'; return 99; }
distro="$(_vm_prereqs_distro)"
manager="$(_vm_prereqs_pkg_manager)"
mapping="$(_vm_prereqs_map_packages "$distro" "$manager" qemu-img 2>/dev/null)" || exit 10
if _vm_prereqs_install </dev/null; then
  install_rc=0
else
  install_rc=$?
fi
printf 'DISTRO=%s\nMANAGER=%s\nMAPPING=%s\nINSTALL_RC=%s\n' "$distro" "$manager" "$mapping" "$install_rc"
if [[ "$manager" == pacman || "$distro" =~ ^(arch|cachyos|manjaro)$ ]]; then
  command() {
    if [[ "${1:-}" == -v && "${2:-}" == virtiofsd ]]; then return 1; fi
    builtin command "$@"
  }
  _vm_prereqs_fix_virtiofsd_path || exit 11
  declare -F virtiofsd >/dev/null || exit 12
  printf 'VIRTIOFSD_FALLBACK=%s\n' "${_VM_VIRTIOFSD:-}"
fi
'''
    env = os.environ.copy()
    env["VM_YES"] = "0"
    env["LAB_YES"] = "0"
    result = run(["bash", "-c", script, "host-prereq-probe", str(repo)], env=env, timeout=30)
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0 or "UNSAFE_SUDO_INVOKED" in output or "Install these packages? [y/N]" not in output:
        return False, "safe missing-tool prompt probe did not reach default-N cancellation"
    manager_match = re.search(r"^MANAGER=(\S+)$", output, re.MULTILINE)
    mapping_match = re.search(r"^MAPPING=REQUIRED:\s*(\S+)\s*$", output, re.MULTILINE)
    install_rc = re.search(r"^INSTALL_RC=(\d+)$", output, re.MULTILINE)
    expected = {"apt": "qemu-utils", "dnf": "qemu-img", "yum": "qemu-img", "pacman": "qemu-img"}
    manager = manager_match.group(1) if manager_match else ""
    if manager not in expected or not mapping_match or mapping_match.group(1) != expected[manager]:
        return False, "host distro/package-manager mapping did not match the expected qemu-img package"
    if not install_rc or install_rc.group(1) != "1" or "Aborted. Install manually and re-run." not in output:
        return False, "missing dependency did not default to no-install"
    fallback = re.search(r"^VIRTIOFSD_FALLBACK=(\S+)$", output, re.MULTILINE)
    distro_match = re.search(r"^DISTRO=(\S+)$", output, re.MULTILINE)
    distro = distro_match.group(1) if distro_match else ""
    if (
        (manager == "pacman" or distro in ("arch", "cachyos", "manjaro"))
        and (not fallback or not Path(fallback.group(1)).is_file())
    ):
        return False, "Arch-derived virtiofsd fallback did not resolve an installed executable"
    return True, "real prerequisite script displayed distro/package mapping and defaulted to N; package manager was not invoked"


def disposable_guest_readiness_script(expected_depth: int, maximum: int) -> str:
    if not 0 <= expected_depth <= maximum <= 2:
        raise AcceptanceError("Invalid disposable guest recursion policy.")
    expected_policy = f"{expected_depth} {maximum}"
    return f'''set -euo pipefail
test "$(cat /etc/lab-vm-recursion)" = {shlex.quote(expected_policy)}
sudo cloud-init status --format json | python3 -c 'import json,sys; x=json.load(sys.stdin); raise SystemExit(0 if x.get("status")=="done" and x.get("extended_status")=="done" and x.get("errors")==[] and x.get("recoverable_errors")=={{}} else 1)'
test "$(findmnt -n -o FSTYPE /workspace)" = virtiofs
test -f /var/lib/mester-provisioned
test -x /workspace/lab-vm
test ! -S /var/run/docker.sock
sock="unix:///run/user/$(id -u)/docker.sock"
test -S "/run/user/$(id -u)/docker.sock"
docker -H "$sock" info --format '{{{{json .SecurityOptions}}}}' | python3 -c 'import json,sys; x=json.load(sys.stdin); raise SystemExit(0 if any("rootless" in str(v).lower() for v in x) else 1)'
docker -H "$sock" compose version >/dev/null
version="$(opencode --version)"
[[ "$version" =~ [0-9]+\\.[0-9]+\\.[0-9]+ ]]
printf 'DISPOSABLE_GUEST_READY depth=%s max=%s\\n' {expected_depth} {maximum}
'''


def recursion_chain_script(depth1: str, depth2: str, depth3: str) -> str:
    if any(not PROFILE_RE.fullmatch(item) for item in (depth1, depth2, depth3)) or len({depth1, depth2, depth3}) != 3:
        raise AcceptanceError("Invalid or colliding disposable recursion profile names.")
    q1, q2, q3 = (shlex.quote(item) for item in (depth1, depth2, depth3))
    q1_env = recursive_profile_state_env(depth1)
    q2_env = recursive_profile_state_env(depth2)
    inner_refusal = (
        "cd /workspace && export LAB_VM_RECURSION_DEPTH=2 LAB_VM_MAX_DEPTH=2 && "
        f"session=\"$HOME/.cache/lab-vm-recursion/{depth3}\" && test ! -e \"$session\" && "
        f"if output=$(./lab-vm up {q3} --workspace /workspace --recurse 2>&1); then "
        "echo 'over-depth launch unexpectedly succeeded' >&2; exit 21; else rc=$?; fi && "
        "[[ $rc -ne 0 && $output == *'maximum nesting depth 2 reached'* ]] && "
        "test ! -e \"$session\" && printf 'DEPTH3_REFUSED_BEFORE_STATE\\n'"
    )
    child2_remote = (
        "cd /workspace && export LAB_VM_RECURSION_DEPTH=1 LAB_VM_MAX_DEPTH=2 && "
        f"{q2_env} ./lab-vm ssh {q2} {shlex.quote(inner_refusal)}"
    )
    child1_remote = (
        "cd /workspace && export LAB_VM_RECURSION_DEPTH=0 LAB_VM_MAX_DEPTH=2 && "
        f"{q1_env} ./lab-vm ssh {q1} {shlex.quote(child2_remote)}"
    )
    child2_up_remote = (
        "cd /workspace && export LAB_VM_RECURSION_DEPTH=1 LAB_VM_MAX_DEPTH=2 && "
        f"./lab-vm up {q2} --workspace /workspace --recurse"
    )
    child2_policy_remote = (
        "cd /workspace && export LAB_VM_RECURSION_DEPTH=1 LAB_VM_MAX_DEPTH=2 && "
        f"{q2_env} ./lab-vm ssh {q2} 'cat /etc/lab-vm-recursion'"
    )
    child2_isolation_remote = (
        "cd /workspace && export LAB_VM_RECURSION_DEPTH=1 LAB_VM_MAX_DEPTH=2 && "
        f"VM_ISOLATION_SKIP_MODEL=1 {q2_env} ./lab-vm verify-isolation {q2}"
    )
    child1_mem_remote = "awk '/^MemTotal:/ {print $2}' /proc/meminfo"
    child2_mem_remote = (
        "cd /workspace && export LAB_VM_RECURSION_DEPTH=1 LAB_VM_MAX_DEPTH=2 && "
        f"{q2_env} ./lab-vm ssh {q2} {shlex.quote(child1_mem_remote)}"
    )
    child1_readiness_remote = nested_guest_script_command(
        depth1, disposable_guest_readiness_script(1, 2), env_prefix=q1_env,
    )
    child2_readiness_remote = nested_guest_script_command(
        depth2, disposable_guest_readiness_script(2, 2), env_prefix=q2_env,
    )
    parent_context_check = r'''context="$(./lab-vm context --json)"
python3 - "$context" <<'PY'
import json, sys
x = json.loads(sys.argv[1])
r = x.get("recursion", {})
raise SystemExit(0 if r.get("metadata") == "configured" and r.get("depth") == 0 and r.get("max_depth") == 2 and r.get("child_depth_available") is True else 1)
PY'''
    return f'''set -euo pipefail
cd /workspace
export LAB_VM_RECURSION_DEPTH=0 LAB_VM_MAX_DEPTH=2
test "$(cat /etc/lab-vm-recursion)" = '0 2'
root_mem_kib="$(awk '/^MemTotal:/ {{print $2}}' /proc/meminfo)"
(( root_mem_kib >= 7 * 1024 * 1024 ))
printf 'CHAIN_STAGE depth1-up\\n'
./lab-vm up {q1} --workspace /workspace --recurse
child1_policy="$({q1_env} ./lab-vm ssh {q1} 'cat /etc/lab-vm-recursion')"
[[ "${{child1_policy##*$'\\n'}}" == '1 2' ]]
child1_ready="$({child1_readiness_remote})"
[[ "$child1_ready" == *'DISPOSABLE_GUEST_READY depth=1 max=2'* ]]
child1_mem_kib="$({q1_env} ./lab-vm ssh {q1} {shlex.quote(child1_mem_remote)} | tail -n 1)"
(( child1_mem_kib >= 3 * 1024 * 1024 && child1_mem_kib < 6 * 1024 * 1024 ))
printf 'CHAIN_STAGE depth1-isolation\\n'
VM_ISOLATION_SKIP_MODEL=1 {q1_env} ./lab-vm verify-isolation {q1}
printf 'CHAIN_STAGE depth2-up\\n'
{q1_env} ./lab-vm ssh {q1} {shlex.quote(child2_up_remote)}
child2_policy="$({q1_env} ./lab-vm ssh {q1} {shlex.quote(child2_policy_remote)})"
[[ "${{child2_policy##*$'\\n'}}" == '2 2' ]]
child2_ready="$({q1_env} ./lab-vm ssh {q1} {shlex.quote(child2_readiness_remote)})"
[[ "$child2_ready" == *'DISPOSABLE_GUEST_READY depth=2 max=2'* ]]
child2_mem_kib="$({q1_env} ./lab-vm ssh {q1} {shlex.quote(child2_mem_remote)} | tail -n 1)"
(( child2_mem_kib >= 1400 * 1024 && child2_mem_kib < 3500 * 1024 ))
printf 'CHAIN_STAGE depth2-isolation\\n'
{q1_env} ./lab-vm ssh {q1} {shlex.quote(child2_isolation_remote)}
printf 'CHAIN_STAGE depth3-refusal\\n'
{child1_remote}
printf 'CHAIN_STAGE parent-continuity\\n'
test -w /workspace
{parent_context_check}
continuity="/workspace/.vm-acceptance-continuity-{depth1}"
printf 'continuity\\n' >"$continuity"
test "$(cat "$continuity")" = continuity
rm -f -- "$continuity"
printf 'CHAIN_PARENT_CONTINUITY_PASS\\n'
printf 'CHAIN_STAGE child2-cleanup\\n'
{q1_env} ./lab-vm ssh {q1} "cd /workspace && export LAB_VM_RECURSION_DEPTH=1 LAB_VM_MAX_DEPTH=2 && {q2_env} ./lab-vm stop {q2} --discard-recursive && VM_YES=1 {q2_env} ./lab-vm recursion-clean {q2} && test ! -e '$HOME/.cache/lab-vm-recursion/{depth2}'"
printf 'CHAIN_STAGE child1-cleanup\\n'
{q1_env} ./lab-vm stop {q1} --discard-recursive
VM_YES=1 {q1_env} ./lab-vm recursion-clean {q1}
test ! -e "$HOME/.cache/lab-vm-recursion/{depth1}"
printf 'CHAIN_NESTED_STATE_CLEAN\\n'
'''


def recursive_profile_state_env(profile: str) -> str:
    if not PROFILE_RE.fullmatch(profile):
        raise AcceptanceError("Invalid recursive VM profile name.")
    return (
        f'VM_STATE_DIR="$HOME/.cache/lab-vm-recursion/{profile}" '
        f'VM_DIR_BASE="$HOME/.cache/lab-vm-recursion/{profile}/vm"'
    )


def nested_guest_script_command(profile: str, script: str, *, env_prefix: str = "") -> str:
    if not PROFILE_RE.fullmatch(profile):
        raise AcceptanceError("Invalid nested guest profile name.")
    digest = hashlib.sha256((profile + script).encode("utf-8")).hexdigest()[:12]
    delimiter = f"VM_ACCEPTANCE_{digest}"
    while f"\n{delimiter}\n" in script:
        delimiter += "X"
    return (
        f"{env_prefix + ' ' if env_prefix else ''}./lab-vm ssh {shlex.quote(profile)} 'bash -s' <<'{delimiter}'\n"
        f"{script.rstrip()}\n{delimiter}\n"
    )


def campaign_profile_dir(state_root: Path, profile: str) -> Path:
    if not PROFILE_RE.fullmatch(profile):
        raise AcceptanceError("Invalid campaign profile name.")
    return state_root / "vm" / profile


def campaign_profile_storage_safe_for_root(state_root: Path, shared_root: Path) -> bool:
    base = state_root / "vm"
    return (
        campaign_state_root_safe(state_root, shared_root)
        and not base.is_symlink() and not path_has_symlink(base)
        and not base.resolve().is_relative_to(ROOT.resolve())
        and (not base.exists() or (base.is_dir() and base.stat().st_uid == os.getuid()))
    )


def campaign_state_residue_free(state_root: Path, shared_root: Path, profiles: list[str]) -> bool:
    if not campaign_profile_storage_safe_for_root(state_root, shared_root):
        return False
    for profile in profiles:
        directory = campaign_profile_dir(state_root, profile)
        if process_matches_profile(directory):
            return False
        if any((directory / name).exists() for name in (
            "disk.qcow2", "seed.iso", "id_vm_control", "id_vm_control.pub",
            "qemu.pid", "virtiofsd.pid", "proxy.pid",
        )):
            return False
    return True


def private_tree_is_owned_non_symlink(root: Path) -> bool:
    if not campaign_project_dir_safe(root):
        return False
    for directory, dirs, files in os.walk(root, topdown=True, followlinks=False):
        for name in [*dirs, *files]:
            entry = Path(directory) / name
            try:
                metadata = entry.lstat()
            except OSError:
                return False
            if stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.getuid():
                return False
    return True


def refuse_campaign_teardown_after_incomplete_gates(
    evidence: Evidence, *, require_all_gates: bool = False,
) -> int | None:
    checks = evidence.data.get("checks", [])
    if not isinstance(checks, list):
        evidence.blocked("disposable teardown", "campaign check ledger is invalid; disposable state was preserved")
        return 2
    failed = any(isinstance(item, dict) and item.get("result") == "FAIL" for item in checks)
    blocked = any(isinstance(item, dict) and item.get("result") == "BLOCKED" for item in checks)
    contract = evidence.data.get("campaign_gate_coverage", {})
    unpassed = []
    if require_all_gates and isinstance(contract, dict):
        unpassed = [
            gate for gate in CAMPAIGN_GATE_COVERAGE if gate != "disposable_teardown"
            and (not isinstance(contract.get(gate), dict) or contract[gate].get("result") != "PASS")
        ]
    if not failed and not blocked and not unpassed:
        return None
    evidence.blocked(
        "disposable teardown",
        "an earlier gate failed, was blocked, or is missing; disposable state was preserved",
    )
    return 1 if failed else 2


def vm_campaign(outer_profile: str, reuse_child: str, snapshot: Path) -> int:
    if not PROFILE_RE.fullmatch(outer_profile) or not PROFILE_RE.fullmatch(reuse_child):
        raise AcceptanceError("Campaign requires valid, existing outer and child profile names.")
    if outer_profile == reuse_child:
        raise AcceptanceError("Outer and child profile names must be distinct.")
    if not sys.stdin.isatty():
        raise AcceptanceError("The ordered VM campaign requires a terminal for TUI, permissions, and OAuth approval.")

    evidence = Evidence(
        "campaign",
        "finite host-only campaign: existing boundary continuation, clean isolated provisioning, depth-0→1→2 recursion, model/TUI, auth, permissions, workload, isolation and lifecycle; no broader VM-release claim",
    )
    evidence.data["preserved_profiles"] = {"outer": outer_profile, "child": reuse_child}
    evidence.save()
    try:
        print("=== 1/10: Existing-child nested continuation (read-only; no repairs, installs, or VM lifecycle changes) ===", flush=True)
        nested_result = nested_gate(
            reuse_child, outer_profile, snapshot, ROOT,
            reuse_existing=True, require_existing_outer=True,
        )
        if nested_result == 2:
            evidence.blocked(
                "existing-child nested continuation",
                "the read-only continuation was blocked; preserve both named profiles and its private report, then stop",
            )
        else:
            evidence.check(
                "existing-child nested continuation",
                nested_result == 0,
                "outer/child loopback and network-boundary continuation passed" if nested_result == 0 else "nested continuation failed; preserve both named profiles and its private report, then stop",
            )
        record_campaign_gate(
            evidence, "existing_nested_boundary",
            "PASS" if nested_result == 0 else "BLOCKED" if nested_result == 2 else "FAIL",
            "read-only existing-child continuation passed" if nested_result == 0 else "continuation was blocked or failed; preserved profiles were not changed",
        )
        if nested_result != 0:
            finish_campaign(evidence, "BLOCKED" if nested_result == 2 else "FAIL")
            return nested_result

        print("=== 2/10: Reload approved egress policy, then verify both model endpoints and direct-guest UI ===", flush=True)
        reload_result = run_campaign_vm(["egress-reload", outer_profile])
        reload_ready = reload_result.returncode == 0
        evidence.check(
            "outer egress policy reload",
            reload_ready,
            "running guest proxy reloaded the current approved policy" if reload_ready else f"lab-vm egress-reload exited {reload_result.returncode}; model/auth/TUI stages were not started",
        )
        if not reload_ready:
            record_campaign_gate(evidence, "direct_guest_model_tui", "FAIL", "approved egress-policy reload failed")
            finish_campaign(evidence, "FAIL")
            return reload_result.returncode or 1
        model_status = run_campaign_vm(["models", "status", "all"])
        model_ready = model_status.returncode == 0
        evidence.check(
            "both host-side model endpoint statuses",
            model_ready,
            "Ollama and llama.cpp loopback runtime-port and HTTP health checks passed; no service was started or changed" if model_ready else f"lab-vm models status all exited {model_status.returncode}; no model service was started or changed",
        )
        if not model_ready:
            record_campaign_gate(evidence, "direct_guest_model_tui", "FAIL", "one or both read-only host model endpoint checks failed")
            finish_campaign(evidence, "FAIL")
            return model_status.returncode or 1
        if not launch_campaign_agent(outer_profile, evidence, browser_auth=False):
            record_campaign_gate(evidence, "direct_guest_model_tui", "FAIL", "preserved outer direct-guest agent did not exit successfully")
            finish_campaign(evidence, "FAIL")
            return 1
        observe_gate("tui", evidence)
        tui_result = human_observation_result(evidence, "tui")
        record_campaign_gate(
            evidence, "direct_guest_model_tui", tui_result,
            "both model/TUI human observations passed" if tui_result == "PASS" else "one or more model/TUI observations failed or were not confirmed",
        )
        if tui_result != "PASS":
            finish_campaign(evidence, "FAIL" if tui_result == "FAIL" else "BLOCKED")
            return 1 if tui_result == "FAIL" else 2

        print("=== 3/10: Safe prerequisite path and clean isolated provisioning ===", flush=True)
        prerequisite_ok, prerequisite_detail = prerequisite_prompt_probe(ROOT)
        evidence.check("non-mutating missing-prerequisite prompt probe", prerequisite_ok, prerequisite_detail)
        if not prerequisite_ok:
            record_campaign_gate(evidence, "host_prerequisites", "FAIL", prerequisite_detail)
            finish_campaign(evidence, "FAIL")
            return 1
        record_campaign_gate(evidence, "host_prerequisites", "PASS", prerequisite_detail)

        source_clean = recursive_source_checkout_clean(ROOT)
        if not source_clean:
            evidence.blocked(
                "committed recursive proof source",
                "recursive proof requires a clean committed checkout; no campaign state root, source snapshot, or VM was created",
            )
            record_campaign_gate(evidence, "clean_provisioning", "BLOCKED", "recursive proof source checkout was dirty or could not be verified")
            finish_campaign(evidence, "BLOCKED")
            return 2
        evidence.check("committed recursive proof source", True, "checkout has no tracked changes or untracked files")

        if not memory_budget_ok(14 * 1024, evidence, "depth-2 chain aggregate memory budget"):
            record_campaign_gate(evidence, "clean_provisioning", "BLOCKED", "no disposable depth-0/1/2 VM was started because the aggregate RAM budget did not pass")
            finish_campaign(evidence, "BLOCKED")
            return 2
        state_root, shared_state_root = create_campaign_state_root()
        evidence.data["disposable_state_root_created"] = True
        evidence.save()
        base = run_campaign_vm(["base"], state_root=state_root, timeout=1800)
        base_path = state_root / "vm" / "base.qcow2"
        base_info = run(["qemu-img", "info", "--output=json", str(base_path)]) if base_path.is_file() else None
        base_valid = False
        if base_info is not None and base_info.returncode == 0:
            try:
                base_json = json.loads(base_info.stdout)
                base_valid = base_json.get("format") == "qcow2" and int(base_json.get("virtual-size", 0)) > 0
            except (json.JSONDecodeError, TypeError, ValueError):
                base_valid = False
        base_ready = base.returncode == 0 and base_valid and "All VM dependencies found. OK" in (base.stdout or "")
        evidence.check(
            "fresh isolated base image and actual host prerequisite check",
            base_ready,
            "lab-vm base found all host prerequisites and created a valid fresh qcow2 base in the isolated campaign root" if base_ready else f"isolated base/prerequisite check exited {base.returncode}; no shared profile/base was changed",
        )
        if not base_ready:
            record_campaign_gate(evidence, "clean_provisioning", "FAIL", "fresh isolated base image or host prerequisite check failed")
            finish_campaign(evidence, "FAIL")
            return base.returncode or 1

        recursion_profile = unique_campaign_profile("vm-acceptance-depth2", state_root)
        recursion_workspace, recursion_session = private_recursive_source_workspace(recursion_profile, ROOT)
        recursion_vm_dir = campaign_profile_dir(state_root, recursion_profile)
        evidence.data["recursion_profile"] = recursion_profile
        evidence.data["recursive_source_snapshot_created"] = True
        evidence.save()
        recursive_up = run_campaign_vm(
            ["up", recursion_profile, "--workspace", str(recursion_workspace), "--recurse", "--max-depth", "2"],
            capture=False, timeout=3600, state_root=state_root,
        )
        recursive_status = run_campaign_vm(["status", recursion_profile], state_root=state_root) if recursive_up.returncode == 0 else None
        root_ready, root_output, root_reason = run_guest_script(
            recursion_vm_dir, disposable_guest_readiness_script(0, 2), timeout=180,
        ) if recursive_status is not None and recursive_status.returncode == 0 else (False, "", "provisioning_failed")
        clean_ready = recursive_up.returncode == 0 and recursive_status is not None and recursive_status.returncode == 0 and root_ready and "DISPOSABLE_GUEST_READY depth=0 max=2" in root_output
        evidence.check(
            "fresh depth-0/max-2 guest readiness",
            clean_ready,
            "isolated guest cloud-init, virtiofs, direct harness, rootless Docker/Compose, and persisted recursion policy passed" if clean_ready else f"fresh guest provisioning/readiness failed ({root_reason}); preserve isolated profile/workspace and diagnostics",
        )
        if not clean_ready:
            record_campaign_gate(evidence, "clean_provisioning", "FAIL", "fresh isolated depth-0 guest did not complete provisioning/readiness")
            finish_campaign(evidence, "FAIL")
            return recursive_up.returncode or 1
        record_campaign_gate(evidence, "clean_provisioning", "PASS", "fresh isolated base and depth-0/max-2 guest provisioning/readiness passed")

        chain_isolation = run_campaign_vm(
            ["verify-isolation", recursion_profile], state_root=state_root,
            skip_model_isolation=True, timeout=180,
        )
        chain_isolation_ok = chain_isolation.returncode == 0
        evidence.check(
            "disposable depth-0 boundary probes",
            chain_isolation_ok,
            "QEMU/listener/workspace and representative guest-originated boundary probes passed" if chain_isolation_ok else f"depth-0 isolation probe exited {chain_isolation.returncode}; preserve generated state",
        )
        if not chain_isolation_ok:
            record_campaign_gate(evidence, "guest_boundary", "FAIL", "depth-0 disposable boundary probes failed")
            record_campaign_gate(evidence, "depth_two_recursion", "BLOCKED", "recursive chain was not started after the depth-0 boundary failure")
            finish_campaign(evidence, "FAIL")
            return chain_isolation.returncode or 1

        depth1 = unique_campaign_profile("vm-acceptance-depth1", state_root)
        depth2 = unique_campaign_profile("vm-acceptance-depth2-child", state_root)
        depth3 = f"vm-acceptance-depth3-refused-{uuid.uuid4().hex[:8]}"
        if not PROFILE_RE.fullmatch(depth3):
            raise AcceptanceError("Could not allocate a safe depth-3 refusal profile name.")
        evidence.data["recursive_profiles"] = {"depth1": depth1, "depth2": depth2, "depth3_probe": depth3}
        evidence.save()
        chain_approval = prompt_result(
            f"Do you authorize stop/down and recursion-clean only for generated profiles {recursion_profile}, {depth1}, {depth2} and their private scratch snapshots after the depth-2 probes pass?"
        )
        if chain_approval != "pass":
            record_campaign_gate(evidence, "depth_two_recursion", "BLOCKED", "explicit disposable recursion-chain teardown approval was declined; generated state was preserved")
            record_campaign_gate(evidence, "guest_boundary", "BLOCKED", "recursive child boundary probes were not reached because teardown authorization was declined")
            finish_campaign(evidence, "BLOCKED")
            return 2
        print("=== 4/10: Disposable depth-0→1→2 boot, depth-3 refusal, and child isolation ===", flush=True)
        chain_ok, _, chain_reason = run_guest_script(
            recursion_vm_dir, recursion_chain_script(depth1, depth2, depth3),
            timeout=7200, stream=True,
        )
        evidence.check(
            "disposable depth-0-to-2 recursive chain",
            chain_ok,
            "depth-1 and depth-2 guests booted with propagated policy/memory, depth-3 refused before state, child boundaries passed, and nested state was explicitly cleaned" if chain_ok else f"recursive chain did not complete ({chain_reason}); preserve generated profiles, workspaces, and logs",
        )
        if not chain_ok:
            record_campaign_gate(evidence, "depth_two_recursion", "FAIL", "disposable depth-0/1/2 proof or its approved nested cleanup failed")
            record_campaign_gate(evidence, "guest_boundary", "FAIL", "one or more recursive-level QEMU/network isolation probes failed")
            finish_campaign(evidence, "FAIL")
            return 1

        root_stop = run_campaign_vm(["stop", recursion_profile, "--discard-recursive"], state_root=state_root, timeout=300)
        root_down = run_campaign_vm(["down", recursion_profile, "--discard-recursive"], state_root=state_root, timeout=120) if root_stop.returncode == 0 else None
        chain_residue_free = root_stop.returncode == 0 and root_down is not None and root_down.returncode == 0 and not process_matches_profile(recursion_vm_dir)
        evidence.check(
            "approved depth-0 recursion profile stop/down",
            chain_residue_free,
            "only the generated depth-0 profile was stopped/down after both child sessions were cleaned" if chain_residue_free else "depth-0 stop/down failed; preserve generated state",
        )
        if not chain_residue_free:
            record_campaign_gate(evidence, "depth_two_recursion", "FAIL", "approved root profile teardown failed")
            record_campaign_gate(evidence, "guest_boundary", "BLOCKED", "main disposable boundary probes were not reached after recursive profile teardown failed")
            finish_campaign(evidence, "FAIL")
            return root_stop.returncode or (root_down.returncode if root_down else 1) or 1
        if campaign_project_dir_safe(recursion_workspace) and recursion_session.resolve().parent == (Path.home() / ".cache/little-mester/host-acceptance").resolve():
            shutil.rmtree(recursion_session)
            recursion_workspace_clean = not recursion_session.exists()
        else:
            recursion_workspace_clean = False
        evidence.check(
            "recursive source workspace cleanup",
            recursion_workspace_clean,
            "only the generated revision-bound recursive workspace was removed" if recursion_workspace_clean else "recursive source workspace identity changed; it was left untouched",
        )
        if not recursion_workspace_clean:
            record_campaign_gate(evidence, "depth_two_recursion", "FAIL", "generated recursive source workspace could not be safely cleaned")
            record_campaign_gate(evidence, "guest_boundary", "BLOCKED", "main disposable boundary probes were not reached after recursive workspace cleanup failed")
            finish_campaign(evidence, "FAIL")
            return 1
        record_campaign_gate(evidence, "depth_two_recursion", "PASS", "disposable depth-0→1→2 boot, depth-3 pre-state refusal, parent continuity, isolation and cleanup passed")

        profile = unique_campaign_profile("vm-acceptance-disposable", state_root)
        vm_dir = campaign_profile_dir(state_root, profile)
        evidence.data["disposable_profile"] = profile
        evidence.save()

        print("=== 5/10: Fresh disposable auth/permission/workload profile ===", flush=True)
        if not memory_budget_ok(2048, evidence, "disposable VM memory budget"):
            record_campaign_gate(evidence, "native_auth", "BLOCKED", "auth/workload profile was not started because the isolated-profile RAM budget failed")
            record_campaign_gate(evidence, "actual_permissions", "BLOCKED", "permission UI was not reached")
            record_campaign_gate(evidence, "guest_workload", "BLOCKED", "workload was not reached")
            record_campaign_gate(evidence, "guest_boundary", "BLOCKED", "main disposable guest boundary was not reached")
            record_campaign_gate(evidence, "non_destructive_lifecycle", "BLOCKED", "lifecycle profile was not started")
            record_campaign_gate(evidence, "disposable_teardown", "BLOCKED", "campaign stopped before final teardown approval")
            finish_campaign(evidence, "BLOCKED")
            return 2
        workspace = private_acceptance_workspace(profile)
        evidence.data["disposable_workspace_created"] = True
        evidence.save()
        port = campaign_guest_port(profile)
        if not host_port_available(port):
            raise AcceptanceError("Generated guest-loopback Compose port is already occupied on the owner host; no Compose workload was started.")
        escrow = private_campaign_escrow(profile, shared_state_root)
        evidence.data["disposable_escrow_path_allocated"] = True
        evidence.save()
        up = run_campaign_vm(
            ["up", profile, "--workspace", str(workspace), "--memory", "2", "--recurse", "--max-depth", "0"],
            capture=False, state_root=state_root,
        )
        main_ready, main_output, main_reason = run_guest_script(
            vm_dir, disposable_guest_readiness_script(0, 0), timeout=180,
        ) if up.returncode == 0 else (False, "", "provisioning_failed")
        profile_ready = up.returncode == 0 and main_ready and "DISPOSABLE_GUEST_READY depth=0 max=0" in main_output
        evidence.check(
            "fresh auth/workload guest provisioning",
            profile_ready,
            "unique guest is ready with max depth 0 and a private project workspace" if profile_ready else f"lab-vm up/readiness failed ({main_reason}); preserve profile, workspace, and diagnostics",
        )
        if not profile_ready:
            for gate in ("native_auth", "actual_permissions", "guest_workload", "guest_boundary", "non_destructive_lifecycle", "disposable_teardown"):
                record_campaign_gate(evidence, gate, "BLOCKED", "profile provisioning failed before this required gate")
            finish_campaign(evidence, "FAIL")
            return up.returncode or 1

        main_isolation = run_campaign_vm(
            ["verify-isolation", profile], state_root=state_root,
            skip_model_isolation=True, timeout=180,
        )
        main_isolation_ok = main_isolation.returncode == 0
        evidence.check(
            "auth/workload guest boundary probes",
            main_isolation_ok,
            "QEMU/listener/workspace and guest-originated boundary checks passed" if main_isolation_ok else f"main disposable isolation exited {main_isolation.returncode}; preserve state",
        )
        if not main_isolation_ok:
            record_campaign_gate(evidence, "guest_boundary", "FAIL", "main disposable guest boundary probes failed")
            for gate in ("native_auth", "actual_permissions", "guest_workload", "non_destructive_lifecycle", "disposable_teardown"):
                record_campaign_gate(evidence, gate, "BLOCKED", "campaign stopped before the gate after an isolation failure")
            finish_campaign(evidence, "FAIL")
            return main_isolation.returncode or 1
        record_campaign_gate(evidence, "guest_boundary", "PASS", "preserved and disposable outer/recursive guest boundary probes passed")

        auth_prerequisites = prompt_result(
            "Have you reviewed the current #45 handoff and confirmed its prerequisites authorize this browser-auth test?"
        )
        approval = prompt_result(
            "Do you separately approve starting the real host-browser OAuth flow in this disposable VM now?"
        ) if auth_prerequisites == "pass" else "unknown"
        auth_attempted = auth_prerequisites == "pass" and approval == "pass"
        if not auth_attempted:
            record_campaign_gate(evidence, "native_auth", "BLOCKED", "current #45 prerequisites and separate real-OAuth approval were not both confirmed; no browser flow started")
            for gate in ("actual_permissions", "guest_workload", "non_destructive_lifecycle", "disposable_teardown"):
                record_campaign_gate(evidence, gate, "BLOCKED", "campaign stopped before the gate because OAuth was not approved")
            finish_campaign(evidence, "BLOCKED")
            return 2

        print("OAuth safety: approve in your browser; never copy or record a URL, state, code, or token. TUI output is not captured.", flush=True)
        if not launch_campaign_agent(profile, evidence, browser_auth=True, state_root=state_root):
            record_campaign_gate(evidence, "native_auth", "FAIL", "native browser-auth agent session did not exit successfully")
            for gate in ("actual_permissions", "guest_workload", "non_destructive_lifecycle", "disposable_teardown"):
                record_campaign_gate(evidence, gate, "BLOCKED", "campaign stopped after browser-auth launch failure")
            finish_campaign(evidence, "FAIL")
            return 1
        observe_gate("auth", evidence)
        auth_observation = human_observation_result(evidence, "auth")

        auth_result, auth_reason = guest_openai_auth_stored(vm_dir)
        evidence.check(
            "guest OpenAI auth list",
            auth_result is True,
            "OpenAI reports a stored guest account" if auth_result is True else (
                "OpenAI account was not reported stored; raw auth-list output was not retained" if auth_result is False
                else f"auth-list check could not be verified ({auth_reason}); preserve the profile"
            ),
        )
        auth_ready = auth_result is True
        if auth_observation != "PASS" or auth_result is not True:
            auth_gate_result = "FAIL" if auth_observation == "FAIL" or auth_result is False else "BLOCKED"
            record_campaign_gate(evidence, "native_auth", auth_gate_result, "browser callback/failure cleanup/auth-list acceptance was not fully verified")
            for gate in ("actual_permissions", "guest_workload", "non_destructive_lifecycle", "disposable_teardown"):
                record_campaign_gate(evidence, gate, "BLOCKED", "campaign stopped before the gate after incomplete native-auth acceptance")
            finish_campaign(evidence, "FAIL" if auth_gate_result == "FAIL" else "BLOCKED")
            return 1 if auth_gate_result == "FAIL" else 2
        record_campaign_gate(evidence, "native_auth", "PASS", "native browser callback, required human failure-cleanup observations, auth list and provider observation passed")

        print("=== 6/10: Actual shared-service and standalone permission behavior ===", flush=True)
        print(
            "In the disposable TUI, use the ask rule for `pwd` under `--auto` and confirm the exact "
            "`echo VM_ACCEPTANCE_DENIED` rule still blocks. Repeat in a separate `--standalone --auto` session. "
            "Then use only the generated Compose project; its PostgreSQL port is guest-loopback-only, never a physical-host port.",
            flush=True,
        )
        shared_agent_ok = launch_campaign_agent(profile, evidence, browser_auth=False, auto=True, state_root=state_root)
        observe_gate("permissions", evidence, opencode_argv=["opencode", "--auto"])
        shared_permission_result = human_observation_result(evidence, "permissions")
        standalone_agent_ok = launch_campaign_agent(
            profile, evidence, browser_auth=False, auto=True,
            state_root=state_root, standalone=True,
        )
        observe_gate(
            "permissions_standalone", evidence,
            opencode_argv=["opencode", "--standalone", "--auto"],
        )
        standalone_permission_result = human_observation_result(evidence, "permissions_standalone")
        permission_results = (shared_permission_result, standalone_permission_result)
        permission_gate_result = (
            "PASS" if shared_agent_ok and standalone_agent_ok
            and all(result == "PASS" for result in permission_results)
            and permission_versions_match(evidence)
            else "FAIL" if "FAIL" in permission_results or not shared_agent_ok or not standalone_agent_ok
            else "BLOCKED"
        )
        record_campaign_gate(
            evidence, "actual_permissions", permission_gate_result,
            "shared-service and --standalone actual allow/deny observations passed" if permission_gate_result == "PASS" else "one or both actual permission-mode sessions failed or were not confirmed",
        )
        if permission_gate_result != "PASS":
            for gate in ("guest_workload", "non_destructive_lifecycle", "disposable_teardown"):
                record_campaign_gate(evidence, gate, "BLOCKED", "campaign stopped before the gate after incomplete actual-permission acceptance")
            finish_campaign(evidence, "FAIL" if permission_gate_result == "FAIL" else "BLOCKED")
            return 1 if permission_gate_result == "FAIL" else 2

        observe_gate("workload", evidence)

        workload_observation = human_observation_result(evidence, "workload")
        if workload_observation != "PASS":
            record_campaign_gate(evidence, "guest_workload", workload_observation, "agent-controlled workload observations failed or were not confirmed")
            for gate in ("non_destructive_lifecycle", "disposable_teardown"):
                record_campaign_gate(evidence, gate, "BLOCKED", "campaign stopped before the gate after incomplete workload observations")
            finish_campaign(evidence, "FAIL" if workload_observation == "FAIL" else "BLOCKED")
            return 1 if workload_observation == "FAIL" else 2

        print("=== 7/10: Guest Compose workload, bind/volume, and host-boundary feedback ===", flush=True)
        if not host_port_available(port):
            record_campaign_gate(evidence, "guest_workload", "BLOCKED", "the reserved physical-host loopback port became occupied before workload startup")
            for gate in ("non_destructive_lifecycle", "disposable_teardown"):
                record_campaign_gate(evidence, gate, "BLOCKED", "workload was not started")
            finish_campaign(evidence, "BLOCKED")
            return 2
        ok, output, reason = run_guest_script(vm_dir, compose_feedback_script(profile), timeout=300)
        markers = (
            "WORKLOAD_QUERY_FIRST", "WORKLOAD_RESTART_QUERY", "GUEST_LOOPBACK_PORT_ONLY",
            "BIND_MOUNT_AND_VOLUME_OK", "WORKLOAD_READY_FOR_HOST_BOUNDARY",
        )
        guest_workload_ready = ok and all(marker in output for marker in markers)
        host_port_still_closed = (
            guest_workload_ready and host_port_available(port) and host_port_unreachable(port)
        )
        evidence.check(
            "guest Docker/Compose rootless boundary",
            guest_workload_ready,
            "guest rootless socket works, host Docker socket and agent container are absent" if guest_workload_ready else f"guest runtime or Compose setup failed ({reason}); preserve profile/workspace and diagnostics",
        )
        evidence.check(
            "guest-loopback port is not exposed on physical host",
            host_port_still_closed,
            "the physical host can bind the port and receives connection refusal; the guest-only listener is not host-reachable" if host_port_still_closed else "the owner-host loopback port was reachable or could not be proven closed; preserve workload state",
        )
        if not guest_workload_ready or not host_port_still_closed:
            record_campaign_gate(evidence, "guest_workload", "FAIL", "guest Compose or physical-host port-isolation check failed")
            for gate in ("non_destructive_lifecycle", "disposable_teardown"):
                record_campaign_gate(evidence, gate, "BLOCKED", "campaign stopped before lifecycle teardown after workload/boundary failure")
            finish_campaign(evidence, "FAIL")
            return 1
        teardown_workload, teardown_output, teardown_reason = run_guest_script(
            vm_dir, compose_feedback_teardown_script(profile), timeout=180,
        )
        compose_down_ok = teardown_workload and "WORKLOAD_DOWN" in teardown_output
        evidence.check(
            "guest Compose workload teardown",
            compose_down_ok,
            "PostgreSQL containers, project network and named volume were removed by the guest agent" if compose_down_ok else f"guest Compose down failed ({teardown_reason}); preserve profile/workspace and diagnostics",
        )
        if not compose_down_ok:
            record_campaign_gate(evidence, "guest_workload", "FAIL", "Compose resources were not verified removed")
            for gate in ("non_destructive_lifecycle", "disposable_teardown"):
                record_campaign_gate(evidence, gate, "BLOCKED", "campaign stopped after workload cleanup failure")
            finish_campaign(evidence, "FAIL")
            return 1
        record_campaign_gate(evidence, "guest_workload", "PASS", "agent-controlled PostgreSQL health/query/restart, guest loopback/bind/named-volume behavior and physical-host non-exposure passed")

        print("=== 8/10: Non-destructive suspend/resume with guest workload data ===", flush=True)
        state_ok, state_output, state_reason = run_guest_script(
            vm_dir, lifecycle_workload_start_script(profile), timeout=180,
        )
        state_ok = state_ok and "LIFECYCLE_DATA_WRITTEN" in state_output
        evidence.check(
            "guest workload state prepared for suspend/resume",
            state_ok,
            "persistent PostgreSQL row and guest overlay marker were written" if state_ok else f"could not prepare persistence probe ({state_reason})",
        )
        if not state_ok:
            record_campaign_gate(evidence, "non_destructive_lifecycle", "FAIL", "could not prepare persistent guest workload state")
            record_campaign_gate(evidence, "disposable_teardown", "BLOCKED", "campaign stopped before destructive lifecycle because suspend/resume setup failed")
            finish_campaign(evidence, "FAIL")
            return 1
        disk_path, workspace_record = vm_dir / "disk.qcow2", vm_dir / "workspace"
        try:
            before_inode = disk_path.stat().st_ino
            before_workspace = workspace_record.read_text(encoding="utf-8").strip()
        except OSError:
            before_inode, before_workspace = None, ""
        suspend = run_campaign_vm(["suspend", profile], timeout=120, state_root=state_root)
        suspended = suspend.returncode == 0 and not process_matches_profile(vm_dir)
        evidence.check(
            "disposable VM suspend",
            suspended,
            "guest shut down without deleting the overlay" if suspended else f"suspend failed or QEMU remains active (exit {suspend.returncode}); preserve state",
        )
        if not suspended:
            record_campaign_gate(evidence, "non_destructive_lifecycle", "FAIL", "suspend failed or left QEMU running")
            record_campaign_gate(evidence, "disposable_teardown", "BLOCKED", "campaign stopped after suspend failure; disposable state was preserved")
            finish_campaign(evidence, "FAIL")
            return suspend.returncode or 1
        resume = run_campaign_vm(["resume", profile], timeout=600, state_root=state_root)
        resumed = resume.returncode == 0 and process_matches_profile(vm_dir)
        evidence.check(
            "disposable VM resume",
            resumed,
            "same profile restarted" if resumed else f"resume failed (exit {resume.returncode}); preserve state",
        )
        if not resumed:
            record_campaign_gate(evidence, "non_destructive_lifecycle", "FAIL", "resume failed or the same profile did not restart")
            record_campaign_gate(evidence, "disposable_teardown", "BLOCKED", "campaign stopped after resume failure; disposable state was preserved")
            finish_campaign(evidence, "FAIL")
            return resume.returncode or 1
        try:
            same_overlay = disk_path.stat().st_ino == before_inode
            same_workspace = workspace_record.read_text(encoding="utf-8").strip() == before_workspace == str(workspace.resolve())
        except OSError:
            same_overlay, same_workspace = False, False
        evidence.check(
            "suspend/resume overlay and workspace identity", same_overlay and same_workspace,
            "overlay inode and recorded workspace were retained" if same_overlay and same_workspace else "overlay or workspace selection changed",
        )
        persisted, persisted_output, persisted_reason = run_guest_script(
            vm_dir, lifecycle_workload_check_script(profile), timeout=180,
        )
        persisted = persisted and "LIFECYCLE_DATA_PERSISTED" in persisted_output
        evidence.check(
            "guest workload data after resume",
            persisted,
            "PostgreSQL row and guest overlay marker survived suspend/resume" if persisted else f"state was not verified after resume ({persisted_reason})",
        )
        if not same_overlay or not same_workspace or not persisted:
            record_campaign_gate(evidence, "non_destructive_lifecycle", "FAIL", "overlay/workspace identity or workload persistence changed across resume")
            record_campaign_gate(evidence, "disposable_teardown", "BLOCKED", "campaign stopped after suspend/resume persistence failure")
            finish_campaign(evidence, "FAIL")
            return 1
        record_campaign_gate(evidence, "non_destructive_lifecycle", "PASS", "suspend/resume retained the same overlay, workspace and guest workload data")

        print("=== 9/10: Explicit disposable stop/down and credential export/import ===", flush=True)
        teardown_blocked = refuse_campaign_teardown_after_incomplete_gates(evidence, require_all_gates=True)
        if teardown_blocked is not None:
            finish_campaign(evidence, "FAIL" if teardown_blocked == 1 else "BLOCKED")
            return teardown_blocked
        print(f"Only these generated disposable resources may be removed: {profile}, {recursion_profile}, and {workspace}; the escrow remains outside the disposable VM root.", flush=True)
        teardown_approval = prompt_result("Do you separately authorize stop/down and removal of only the generated profiles/workspaces and their private base image?")
        if teardown_approval != "pass":
            record_campaign_gate(evidence, "disposable_teardown", "BLOCKED", "operator declined; disposable VM, workspace and isolated state root were preserved")
            finish_campaign(evidence, "BLOCKED")
            return 2

        # Teardown and credential re-import continue in the following block.
        evidence.data["campaign_teardown_authorized"] = True
        evidence.save()

        if auth_ready:
            stop = run_campaign_vm(["stop", profile], auth_forward=True, escrow=escrow, timeout=300, state_root=state_root)
            stop_export_ok = stop.returncode == 0 and escrow_file_is_private(escrow)
            evidence.check(
                "stop exported credentials before teardown",
                stop_export_ok,
                "credential export was saved privately before workload/overlay deletion" if stop_export_ok else f"stop/export failed (exit {stop.returncode}); guest state was not force-deleted",
            )
            if not stop_export_ok:
                record_campaign_gate(evidence, "disposable_teardown", "FAIL", "credential export failed before disposable profile teardown")
                finish_campaign(evidence, "FAIL")
                return stop.returncode or 1

            idempotent_stop = run_campaign_vm(["stop", profile], auth_forward=True, escrow=escrow, timeout=60, state_root=state_root)
            evidence.check(
                "destructive stop idempotency", idempotent_stop.returncode == 0,
                "second stop was a safe no-op" if idempotent_stop.returncode == 0 else f"second stop exited {idempotent_stop.returncode}",
            )
            absent_after_stop = not (vm_dir / "disk.qcow2").exists() and not (vm_dir / "id_vm_control").exists()
            evidence.check(
                "stop removed disposable overlay and control keys", absent_after_stop,
                "overlay and control keys are absent" if absent_after_stop else "disposable overlay/control state remains",
            )
            if not absent_after_stop:
                record_campaign_gate(evidence, "disposable_teardown", "FAIL", "stop left disposable overlay or control-key residue")
                finish_campaign(evidence, "FAIL")
                return 1

            imported_up = run_campaign_vm(
                ["up", profile, "--workspace", str(workspace), "--recurse", "--max-depth", "0"],
                auth_forward=True, escrow=escrow, capture=False, state_root=state_root,
            )
            evidence.check(
                "fresh overlay imports host credential escrow",
                imported_up.returncode == 0,
                "new overlay booted with opt-in credential import" if imported_up.returncode == 0 else f"importing up exited {imported_up.returncode}; preserve escrow and profile",
            )
            if imported_up.returncode != 0:
                record_campaign_gate(evidence, "disposable_teardown", "FAIL", "fresh overlay could not be booted with private credential import")
                finish_campaign(evidence, "FAIL")
                return imported_up.returncode or 1

            imported_auth, imported_reason = guest_openai_auth_stored(vm_dir)
            evidence.check(
                "OpenAI account available after fresh-overlay import",
                imported_auth is True,
                "OpenAI auth list reports stored without another browser login" if imported_auth is True else f"fresh overlay did not report stored OpenAI auth ({imported_reason}); preserve VM and escrow",
            )
            if imported_auth is not True:
                record_campaign_gate(evidence, "disposable_teardown", "FAIL" if imported_auth is False else "BLOCKED", "OpenAI account was not verified after fresh-overlay import")
                finish_campaign(evidence, "FAIL" if imported_auth is False else "BLOCKED")
                return 1 if imported_auth is False else 2

            down = run_campaign_vm(["down", profile], auth_forward=True, escrow=escrow, timeout=300, state_root=state_root)
            down_export_ok = down.returncode == 0 and escrow_file_is_private(escrow)
            evidence.check(
                "direct down exported credentials before overlay deletion",
                down_export_ok,
                "latest credentials atomically saved before overlay deletion" if down_export_ok else f"down/export failed (exit {down.returncode}); preserve guest state",
            )
            if not down_export_ok:
                record_campaign_gate(evidence, "disposable_teardown", "FAIL", "direct down credential export failed")
                finish_campaign(evidence, "FAIL")
                return down.returncode or 1
            idempotent_down = run_campaign_vm(["down", profile], auth_forward=True, escrow=escrow, timeout=60, state_root=state_root)
            evidence.check(
                "direct down idempotency", idempotent_down.returncode == 0,
                "second down was a safe no-op" if idempotent_down.returncode == 0 else f"second down exited {idempotent_down.returncode}",
            )
            evidence.data["credential_escrow_retained_private"] = escrow_file_is_private(escrow)
        else:
            evidence.blocked("credential export/import persistence", "no stored OpenAI account was verified; no credential-persistence pass is inferred")
            record_campaign_gate(evidence, "disposable_teardown", "FAIL", "auth acceptance was not verified, so credential persistence and teardown cannot pass")
            finish_campaign(evidence, "FAIL")
            return 1

        residue_free = campaign_state_residue_free(
            state_root, shared_state_root, [recursion_profile, profile],
        )
        evidence.check(
            "all disposable VM residue", residue_free,
            "no generated QEMU process, overlay, seed, SSH key or helper PID remains in the isolated state root" if residue_free else "generated VM residue remains; preserve and inspect it",
        )
        if not residue_free:
            record_campaign_gate(evidence, "disposable_teardown", "FAIL", "one or more generated VM processes or state files remain")
            finish_campaign(evidence, "FAIL")
            return 1

        workspace_root = Path.home() / ".cache/little-mester/host-acceptance"
        workspace_safe = (
            private_tree_is_owned_non_symlink(workspace)
            and workspace.resolve().parent == workspace_root.resolve()
        )
        if workspace_safe:
            shutil.rmtree(workspace)
            workspace_removed = not workspace.exists()
        else:
            workspace_removed = False
        evidence.check(
            "disposable workspace cleanup", workspace_removed,
            "only the generated auth/workload workspace was removed" if workspace_removed else "workspace identity changed or cleanup failed; it was left untouched",
        )
        state_root_safe = (
            campaign_state_root_safe(state_root, shared_state_root)
            and private_tree_is_owned_non_symlink(state_root)
        )
        if state_root_safe and workspace_removed:
            shutil.rmtree(state_root)
            isolated_root_removed = not state_root.exists()
        else:
            isolated_root_removed = False
        evidence.check(
            "isolated base/profile state cleanup",
            isolated_root_removed,
            "only the generated campaign state root and its private base image were removed" if isolated_root_removed else "isolated state root identity was unsafe or workspace cleanup did not pass; state was preserved",
        )
        evidence.data["credential_escrow_retained_private"] = escrow_file_is_private(escrow)
        if not evidence.data["credential_escrow_retained_private"]:
            evidence.check("credential escrow retained privately", False, "credential escrow is missing or has unsafe permissions")
            record_campaign_gate(evidence, "disposable_teardown", "FAIL", "credential escrow did not remain private outside the deleted state root")
            finish_campaign(evidence, "FAIL")
            return 1

        teardown_ok = workspace_removed and isolated_root_removed and evidence.data["credential_escrow_retained_private"]
        record_campaign_gate(
            evidence, "disposable_teardown", "PASS" if teardown_ok else "FAIL",
            "approved disposable stop/down, export/import, residue checks, scratch cleanup and private escrow retention passed" if teardown_ok else "one or more generated cleanup/persistence checks failed",
        )
        outcome = finish_campaign(evidence, "PASS" if teardown_ok else "FAIL")
        return 0 if outcome == "PASS" else 1 if outcome == "FAIL" else 2
    except KeyboardInterrupt:
        evidence.blocked("campaign interrupted", "interactive campaign was interrupted; no automatic VM cleanup was attempted")
        finish_campaign(evidence, "BLOCKED")
        return 130
    except (AcceptanceError, OSError, subprocess.SubprocessError) as exc:
        evidence.check("campaign runner", False, f"{type(exc).__name__}; profile/workspace state was not automatically cleaned")
        finish_campaign(evidence, "FAIL")
        print(f"FAIL campaign runner: {type(exc).__name__}", file=sys.stderr)
        return 2


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    subs = root.add_subparsers(dest="command", required=True)
    for name in ("preflight", "nested"):
        cmd = subs.add_parser(name)
        cmd.add_argument("--outer-profile", default=DEFAULT_OUTER_PROFILE)
        cmd.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
        if name == "nested":
            profiles = cmd.add_mutually_exclusive_group()
            profiles.add_argument("--profile", help="optional new child profile name; otherwise a unique name is generated and retained")
            profiles.add_argument(
                "--reuse-child", metavar="PROFILE",
                help="revalidate an existing child without starting another VM (for continuing a partially completed gate)",
            )
    inspect = subs.add_parser(
        "inspect-interrupted",
        help="read a nested run's private report and matching local processes without changing state",
    )
    inspect.add_argument("profile", help="child profile name from the interrupted nested run")
    report = subs.add_parser(
        "inspect-report",
        help="read allowlisted diagnostics from one private nested report, including preflight-only runs",
    )
    report.add_argument("report", type=Path, help="path printed by the acceptance runner")
    observe = subs.add_parser("observe", help="record guided human TUI/auth/permission observations")
    observe.add_argument("--gate", choices=sorted(OBSERVATIONS), required=True)
    campaign = subs.add_parser(
        "campaign",
        help="run the ordered existing-child, human TUI/auth/permissions, and disposable workload/lifecycle campaign",
    )
    campaign.add_argument("--outer-profile", required=True, help="preserved running outer profile")
    campaign.add_argument("--reuse-child", required=True, help="preserved running child for the read-only nested continuation")
    campaign.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "observe":
            return observe_gate(args.gate)
        if args.command == "campaign":
            return vm_campaign(args.outer_profile, args.reuse_child, args.snapshot.expanduser())
        if args.command == "inspect-interrupted":
            return inspect_interrupted(args.profile)
        if args.command == "inspect-report":
            return inspect_report(args.report.expanduser())
        if args.command == "preflight":
            evidence = Evidence("preflight", "owner-host checks with safe snapshot reconciliation and non-destructive outer-profile bootstrap; no model services, agent, or teardown")
            ready, _ = run_preflight(evidence, args.outer_profile, args.snapshot.expanduser())
            evidence.finish("PASS" if ready else "BLOCKED")
            return 0 if ready else 2
        if args.command == "nested":
            reuse_existing = args.reuse_child is not None
            profile = args.reuse_child if reuse_existing else args.profile
            return nested_gate(
                profile, args.outer_profile, args.snapshot.expanduser(), ROOT,
                reuse_existing=reuse_existing,
            )
    except (AcceptanceError, OSError, subprocess.SubprocessError) as exc:
        print(f"FAIL runner: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
