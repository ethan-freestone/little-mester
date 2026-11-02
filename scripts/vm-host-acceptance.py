#!/usr/bin/env python3
"""Repeatable owner-host acceptance for the staged VM workflow.

Keep this runner after merge: its bounded nested-KVM check can be repeated after
launcher/provisioning changes. ``preflight`` and ``nested`` reconcile only the
known acceptance snapshot and create an outer profile only when needed; prior
snapshot data is retained as a private backup. They never stop/down/clean a VM
or remove a child profile. Human TUI/auth/permission observations are separate
and are never inferred from the automated nested gate.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from queue import Empty, Queue
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
LAB_VM = ROOT / "lab-vm"
SNAPSHOT_FILES = (
    "lib/vm/proxy.py", "lib/vm/runner.sh", "lib/vm/lifecycle.sh",
    "scripts/vm-guest-isolation-check.py",
)
DEFAULT_OUTER_PROFILE = "launcher-smoke"
DEFAULT_SNAPSHOT = Path.home() / ".cache/lab-vm-recursion/initial-launcher-smoke/workspace"
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
VERSION_RE = re.compile(r"^v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
REMOTE_PROGRESS_MARKER = "__LM_VM_ACCEPTANCE_PROGRESS__"
REMOTE_SUMMARY_MARKER = "__LM_VM_ACCEPTANCE__"
REMOTE_NESTED_TIMEOUT_SECONDS = 1800
REMOTE_PROGRESS_STAGES = {
    "remote_script", "runtime_overlay", "child_vm_up", "child_control_state",
    "child_cloud_init_status", "child_context", "child_findmnt",
    "child_workspace_boundary", "child_readiness", "depth_cap_refusal",
    "outer_continuity", "outer_ssh_startup", "cloud-init status",
    "lab-vm context --json", "findmnt /workspace",
    "child SSH or remote readiness probe",
    "child SSH transport or remote script startup",
}
REMOTE_PROGRESS_STATUSES = {"started", "completed", "failed", "timed_out", "waiting", "interrupted"}


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


def run(args: list[str], *, env: dict[str, str] | None = None, cwd: Path = ROOT,
        capture: bool = True, input_text: str | None = None,
        no_input: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=cwd, env=env, input=input_text, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        stdin=subprocess.DEVNULL if no_input and input_text is None else None,
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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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
    # Mirror lifecycle.sh exactly: VM_DIR_BASE is selected by variable
    # presence (even an empty value), while VM_STATE_DIR/XDG_DATA_HOME use
    # shell ``:-`` semantics (empty means use the fallback).
    if "VM_DIR_BASE" in os.environ:
        # lifecycle.sh resolves an explicitly empty VM_DIR_BASE as
        # "$VM_DIR_BASE/$name", i.e. /<name>.
        return Path(os.environ["VM_DIR_BASE"] or "/")
    xdg_data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    state_dir = Path(os.environ.get("VM_STATE_DIR") or str(Path(xdg_data_home) / "little-mester"))
    legacy = repo / ".mester/vm"
    if legacy.is_dir() and not (state_dir / "vm").exists():
        return legacy
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
) -> bool:
    active_source_overlay = False
    expected = snapshot.resolve() == DEFAULT_SNAPSHOT.resolve()
    evidence.check("known snapshot path", expected, "using the stable snapshot named in the current handoff" if expected else "only the stable snapshot named in the current handoff may be repaired")
    if not expected or snapshot.is_symlink() or path_has_symlink(snapshot.parent):
        evidence.check("existing safe snapshot", False, "stable snapshot path is unsafe; no changes attempted")
        return False

    _, parity = snapshot_status(snapshot, repo)
    state_visible = snapshot_state_visible(snapshot) if snapshot.is_dir() else False
    needs_rebuild = not snapshot.is_dir() or state_visible or any(
        not (repo / relative).is_file() or (repo / relative).is_symlink()
        or not safe_snapshot_target(snapshot, snapshot / relative)
        for relative in SNAPSHOT_FILES
    )
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
        active_users = active_snapshot_users(snapshot)
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


def ensure_outer_profile(name: str, snapshot: Path, repo: Path, evidence: Evidence) -> tuple[str, Path] | None:
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
        evidence.check("outer profile discovery", False, "snapshot is exported by a VM whose profile cannot be verified; no additional VM was started")
        evidence.data["snapshot_export_processes"] = active_users
        evidence.save()
        return None

    requested = profile_dir(name, repo)
    if not lexists(requested) and not active_snapshot_users(requested):
        selected = name
        reason = "named profile is absent"
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


REMOTE_PREFLIGHT = r'''import json, os, re, shutil, subprocess, sys
def command(args):
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
result = {"checks": {}, "context": None, "tools": {}}
ctx_run = command(["./lab-vm", "context", "--json"])
try:
    raw = json.loads(ctx_run.stdout) if ctx_run.returncode == 0 else None
except json.JSONDecodeError:
    raw = None
if isinstance(raw, dict):
    runtime, recursion, kvm = raw.get("runtime", {}), raw.get("recursion", {}), raw.get("kvm", {})
    result["context"] = {"runtime_kind": runtime.get("kind"), "depth": recursion.get("depth"), "max_depth": recursion.get("max_depth"),
      "child_depth_available": recursion.get("child_depth_available"),
      "device_present": kvm.get("device_present"), "device_read_write": kvm.get("device_read_write"),
      "nested_kvm_preflight_ready": kvm.get("nested_kvm_preflight_ready"),
      "cpu_virtualization": sorted(x for x in kvm.get("cpu_virtualization", []) if x in ("vmx", "svm"))}
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
    expected = {"lib/vm/proxy.py", "lib/vm/runner.sh", "lib/vm/lifecycle.sh",
        "scripts/vm-guest-isolation-check.py"}
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
    for item in root.iterdir():
        if (item.name in excluded or item.name.startswith(".")
                or item.name in ("lab-vm", "lib")
                or (overlay_scripts and item.name == "scripts")):
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
    if launcher.is_symlink() or not launcher.is_file():
        raise ValueError("active workspace lab-vm entry point is unsafe")
    shutil.copy2(launcher, stage / "lab-vm")
    lib_source, lib_stage = root / "lib", stage / "lib"
    vm_source = lib_source / "vm"
    if (lib_source.is_symlink() or not lib_source.is_dir()
            or vm_source.is_symlink() or not vm_source.is_dir()):
        raise ValueError("active workspace VM source directories are unsafe")
    lib_stage.mkdir(mode=0o700)
    for item in lib_source.iterdir():
        if item.name != "vm":
            safe_link(item, lib_stage / item.name)
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
        if scripts_source.is_symlink() or not scripts_source.is_dir():
            raise ValueError("active workspace scripts directory is unsafe")
        scripts_stage.mkdir(mode=0o700)
        for item in scripts_source.iterdir():
            relative = "scripts/" + item.name
            if relative in source_overlay_payload:
                write_source(relative)
            elif (item.name.startswith(".") or item.name == "__pycache__"
                    or item.name.endswith((".db", ".key", ".pem", ".secret", ".sqlite", ".sqlite3", ".token"))):
                continue
            else:
                safe_link(item, scripts_stage / item.name)
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
    print("Using a private /tmp runtime source overlay; the active /workspace snapshot is unchanged.", flush=True)
    summary["runtime_source_overlay_path"] = str(launcher_root)
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
    up = subprocess.run([str(launcher_root / "lab-vm"), "-d", "up", profile, "--workspace", "/workspace", "--recurse"], cwd=launcher_root, env=env, check=False)
    progress("child_vm_up", "completed", returncode=up.returncode)
    summary["checks"]["child_up"] = up.returncode == 0
    if up.returncode != 0:
        summary["failure"] = "child up failed; preserve the profile and its logs"
        emit(3)
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
 "probe_stages": {
  "child_cloud_init_status": {"returncode": cloud_run.returncode, "stderr_present": bool(cloud_run.stderr)},
  "child_context": {"returncode": context_run.returncode, "stderr_present": bool(context_run.stderr)},
  "child_findmnt": {"returncode": mount.returncode, "stderr_present": bool(mount.stderr)},
  "child_workspace_boundary": {"returncode": boundary.returncode, "stderr_present": bool(boundary.stderr)}}}
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
 "host_state_excluded": child_summary["host_state_excluded"]}
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
    require_child_memory_budget: bool = True,
) -> bool:
    non_tool_checks = [
        "context_valid", "outer_guest", "nested_kvm", "workspace_root",
        "workspace_virtiofs", "host_state_excluded",
    ]
    if require_child_memory_budget:
        non_tool_checks.append("memory_budget")

    def query(script: str):
        result = run(ssh_command(vm_dir, "cd /workspace && python3 -"), input_text=script)
        return result, parse_json_object(result.stdout)

    try:
        result, parsed = query(REMOTE_PREFLIGHT)
    except (AcceptanceError, OSError) as exc:
        evidence.check("outer guest SSH", False, str(exc))
        return False
    if parsed is None:
        evidence.check("outer guest preflight", False, "SSH check failed or returned invalid data; no child was started")
        return False

    initial_checks = parsed.get("checks", {})
    initial_tools = parsed.get("tools", {})
    ignored_initial_checks = {"tools"}
    if not require_child_memory_budget:
        ignored_initial_checks.add("memory_budget")
    if (isinstance(initial_checks, dict) and initial_checks.get("tools") is False
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
                result, parsed = query(REMOTE_PREFLIGHT)
            except (AcceptanceError, OSError) as exc:
                evidence.check("outer guest preflight retry", False, f"SSH check failed after guest bootstrap: {exc}")
                return False
            if parsed is None:
                evidence.check("outer guest preflight retry", False, "SSH check returned invalid data after guest bootstrap")
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
        )
    else:
        evidence.check("snapshot preparation", False, "owner-host, checkout, and VM-tool prerequisites are required; no snapshot/profile changes attempted")
        snapshot_ok = False

    selected_profile = outer_profile
    outer: Path | None = None
    if prerequisites_ready and snapshot_ok:
        selected = ensure_outer_profile(outer_profile, snapshot, repo, evidence)
        if selected is not None:
            selected_profile, directory = selected
            outer = outer_profile_checks(selected_profile, snapshot, repo, evidence)
    guest_ok = outer_guest_preflight(
        outer, evidence, require_child_memory_budget=require_child_memory_budget,
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
    use_source_overlay = evidence.data.get("snapshot_source_overlay_required") is True
    try:
        source_payload = active_source_overlay_payload(repo) if use_source_overlay else {}
        remote_script = build_remote_nested_script(repo, use_source_overlay, source_payload)
    except (AcceptanceError, OSError) as exc:
        evidence.check("active snapshot runtime overlay", False, f"could not prepare verified host sources: {exc}")
        return 2, {"checks": {}, "failure": "runtime source overlay could not be prepared; no child was started"}
    if use_source_overlay:
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
) -> int:
    command = "nested-resume" if reuse_existing else "nested"
    objective = (
        "revalidate one existing nested child, depth-1 readiness, nested QEMU/network isolation, depth-2 refusal before state, and parent continuity; no new QEMU, teardown, model, or workload"
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
        "Record only when the latest #41 handoff permits it. Compare built-in and custom dialogs with host-Docker in the same terminal; do not restart QEMU or alter the overlay.",
        ("built_in_dialog_legible", "custom_dialog_legible", "exit_cancel_stays_in_tui", "same_terminal_comparison_done"),
    ),
    "auth": (
        "Record only when the latest #41/#45 handoff permits it. Never enter or save OAuth URLs, state, codes, tokens, prompts, or raw logs.",
        ("browser_callback_completed", "auth_list_openai_stored", "approved_provider_completion_succeeded", "cancellation_cleanup_checked", "interruption_cleanup_checked", "occupied_callback_port_failed_closed"),
    ),
    "permissions": (
        "Record only when the latest #35/#50 prerequisites pass. Use a disposable project and one narrow explicit deny; do not add blanket allows or recreate the host container.",
        ("displayed_mode_matches_requested", "harmless_tool_behavior_correct", "explicit_deny_still_blocks", "disposable_project_used"),
    ),
}


def prompt_result(label: str) -> str:
    while True:
        value = input(f"  {label} [y/n/u=unknown]: ").strip().lower()
        if value in ("y", "n", "u"):
            return {"y": "pass", "n": "fail", "u": "unknown"}[value]
        print("Enter y, n, or u.")


def observe_gate(gate: str) -> int:
    instruction, questions = OBSERVATIONS[gate]
    evidence = Evidence("observe", f"human-reported {gate} observations; not a ticket update or independent acceptance proof")
    print(instruction)
    if prompt_result("Have you followed the current ticket handoff and prerequisites?") != "pass":
        evidence.check("observation prerequisites", False, "operator did not confirm the current ordered handoff")
        evidence.finish("BLOCKED")
        return 2
    observation: dict[str, str] = {"gate": gate, "recorded_at": utc_now()}
    if gate in ("auth", "permissions"):
        while True:
            version = input("OpenCode version (for example v2.2.5; no other text): ").strip()
            if VERSION_RE.fullmatch(version):
                observation["opencode_version"] = version
                break
            print("Enter only a version; do not paste logs or other content.")
    if gate == "permissions":
        while True:
            mode = input("Displayed/requested permission mode [ask/auto]: ").strip().lower()
            if mode in ("ask", "auto"):
                observation["permission_mode"] = mode
                break
            print("Enter ask or auto.")
    for question in questions:
        observation[question] = prompt_result(question.replace("_", " "))
    observation["sensitive_material_recorded"] = "no"
    observations = evidence.data["observations"]
    assert isinstance(observations, list)
    observations.append(observation)
    passed = all(observation[item] == "pass" for item in questions)
    evidence.check("human observation", passed, "all requested observations reported passing" if passed else "one or more observations failed or remain unknown; acceptance is not inferred")
    evidence.finish("REPORTED_PASS" if passed else "REPORTED_INCOMPLETE")
    return 0 if passed else 1


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
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "observe":
            return observe_gate(args.gate)
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
