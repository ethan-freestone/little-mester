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
import uuid

ROOT = Path(__file__).resolve().parents[1]
LAB_VM = ROOT / "lab-vm"
SNAPSHOT_FILES = ("lib/vm/proxy.py", "lib/vm/runner.sh", "lib/vm/lifecycle.sh")
DEFAULT_OUTER_PROFILE = "launcher-smoke"
DEFAULT_SNAPSHOT = Path.home() / ".cache/lab-vm-recursion/initial-launcher-smoke/workspace"
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
VERSION_RE = re.compile(r"^v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")


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
match = re.search(r"MemAvailable=(\d+) MiB, cgroup remaining=([^,]+), releasable QEMU RSS=(\d+) MiB, projected=(\d+) MiB", budget_text)
result["memory_budget"] = {"requested_mib": 4096, "reserve_mib": 512, "available_mib": int(match.group(4)) if match else None}
result["checks"]["memory_budget"] = budget.returncode == 0
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
root = pathlib.Path("/workspace")
sessions = pathlib.Path.home() / ".cache/lab-vm-recursion"
session, vm_dir = sessions / profile, sessions / profile / "vm"
source_overlay_payload = json.loads(SOURCE_OVERLAY_PAYLOAD_JSON)
summary = {"profile": profile, "probe": probe, "checks": {}, "child": {}, "depth_limit": {}}
def emit(code):
    print("__LM_VM_ACCEPTANCE__" + json.dumps(summary, sort_keys=True), flush=True)
    raise SystemExit(code)
if session.exists() or session.is_symlink():
    summary["checks"]["fresh_profile"] = False
    summary["failure"] = "child profile already exists"
    emit(2)
workspace_path, state_path = root.resolve(), session.resolve()
if workspace_path == state_path or workspace_path in state_path.parents or state_path in workspace_path.parents:
    summary["checks"]["state_boundary"] = False
    summary["failure"] = "child control state overlaps the shared workspace"
    emit(2)
summary["checks"]["state_boundary"] = True
summary["checks"]["fresh_profile"] = True
def stage_runtime_overlay():
    if not source_overlay_payload:
        return root
    expected = {"lib/vm/proxy.py", "lib/vm/runner.sh", "lib/vm/lifecycle.sh"}
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
    for item in root.iterdir():
        if item.name in excluded or item.name.startswith(".") or item.name in ("lab-vm", "lib"):
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
try:
    launcher_root = stage_runtime_overlay()
except (OSError, ValueError, KeyError, TypeError) as exc:
    summary["checks"]["runtime_source_overlay"] = False
    summary["failure"] = "verified runtime sources could not be staged safely; no child VM was started"
    summary["source_overlay_error"] = type(exc).__name__
    emit(2)
summary["checks"]["runtime_source_overlay"] = True
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
print("Starting one new nested child; no model/TUI or teardown will run.", flush=True)
up = subprocess.run([str(launcher_root / "lab-vm"), "-d", "up", profile, "--workspace", "/workspace", "--recurse"], cwd=launcher_root, env=env, check=False)
summary["checks"]["child_up"] = up.returncode == 0
if up.returncode != 0:
    summary["failure"] = "child up failed; preserve the profile and its logs"
    emit(3)
if not vm_dir.is_dir() or vm_dir.is_symlink():
    summary["checks"]["child_state"] = False
    summary["failure"] = "child profile state is absent or unsafe"
    emit(4)
summary["checks"]["child_state"] = True
port_file, key_file, known_file = vm_dir / "ssh_port", vm_dir / "id_vm_control", vm_dir / "known_hosts"
if any(p.is_symlink() or not p.is_file() for p in (port_file, key_file, known_file)):
    summary["failure"] = "child SSH control files are missing or unsafe"
    emit(5)
try:
    port = int(port_file.read_text(encoding="ascii").strip())
except (OSError, ValueError):
    summary["failure"] = "child SSH port metadata is invalid"
    emit(5)
if not 1 <= port <= 65535:
    summary["failure"] = "child SSH port metadata is out of range"
    emit(5)
ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
       "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "StrictHostKeyChecking=yes",
       "-o", "UserKnownHostsFile=" + str(known_file), "-i", str(key_file), "-p", str(port), "mester@127.0.0.1"]
child_probe = r'''import json, pathlib, subprocess, sys
def run(args):
    return subprocess.run(args, cwd="/workspace", text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
def obj(text):
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
cloud_run = run(["sudo", "-n", "cloud-init", "status", "--format", "json"])
cloud = obj(cloud_run.stdout)
context_run = run(["./lab-vm", "context", "--json"])
context = obj(context_run.stdout)
mount = run(["findmnt", "-n", "-o", "FSTYPE", "--target", "/workspace"])
fstype = mount.stdout.strip() if mount.returncode == 0 else ""
summary = {"cloud_status": cloud.get("status") if cloud else None,
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
 "host_state_excluded": not pathlib.Path("/workspace/.mester").exists() and not pathlib.Path("/workspace/.mester").is_symlink() and not pathlib.Path("/workspace/.workspace/.mester").exists() and not pathlib.Path("/workspace/.workspace/.mester").is_symlink()}
checks = [summary["cloud_command_ok"], summary["cloud_status"] == "done", summary["cloud_extended_status"] == "done",
 summary["cloud_errors_empty"], summary["cloud_recoverable_errors_empty"], summary["context_command_ok"],
 summary["runtime_kind"] == "vm",
 summary["depth"] == 1, summary["max_depth"] == 1, summary["child_depth_available"] is False, summary["nested_kvm_ready"] is True,
 "virtiofs" in fstype.lower(), summary["host_state_excluded"]]
print(json.dumps(summary, sort_keys=True))
sys.exit(0 if all(checks) else 10)
'''
print("Checking child SSH/cloud-init/context readiness (timeout: 180s).", flush=True)
try:
    child = subprocess.run(ssh + ["python3 -"], input=child_probe, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=180)
except subprocess.TimeoutExpired:
    summary["checks"]["child_acceptance"] = False
    summary["child"] = {"result": "timed out"}
    summary["failure"] = "child readiness probe timed out; preserve the child VM and diagnostics"
    emit(6)
try:
    child_summary = json.loads(child.stdout)
except json.JSONDecodeError:
    child_summary = None
summary["child"] = child_summary if isinstance(child_summary, dict) else {"result": "unparseable"}
summary["checks"]["child_acceptance"] = child.returncode == 0 and isinstance(child_summary, dict)
if not summary["checks"]["child_acceptance"]:
    summary["failure"] = "child readiness/context/workspace check failed; preserve state and diagnostics"
    emit(6)
overflow_probe = r'''import json, os, pathlib, subprocess, sys
probe = sys.argv[1]
session = pathlib.Path.home() / ".cache/lab-vm-recursion" / probe
if session.exists() or session.is_symlink():
    print(json.dumps({"preexisting": True}))
    sys.exit(11)
env = os.environ.copy()
env["LAB_VM_RECURSION_DEPTH"], env["LAB_VM_MAX_DEPTH"] = "1", "1"
launcher = pathlib.Path(env["LM_ACCEPTANCE_LAUNCHER"])
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
print("Checking depth-cap refusal before state creation (timeout: 120s).", flush=True)
try:
    overflow = subprocess.run(ssh + ["python3 -", probe], input=overflow_probe, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=120)
except subprocess.TimeoutExpired:
    summary["depth_limit"] = {"result": "timed out; preserve state and stop"}
    summary["checks"]["depth_limit_refused_before_state"] = False
    summary["failure"] = "depth-cap probe timed out; preserve any generated state and stop"
    emit(8)
try:
    overflow_summary = json.loads(overflow.stdout)
except json.JSONDecodeError:
    overflow_summary = None
summary["depth_limit"] = overflow_summary if isinstance(overflow_summary, dict) else {"result": "unparseable"}
summary["checks"]["depth_limit_refused_before_state"] = overflow.returncode == 0 and isinstance(overflow_summary, dict)
if not summary["checks"]["depth_limit_refused_before_state"]:
    summary["failure"] = "over-depth launch did not refuse before state; stop and preserve evidence"
    emit(8)
print("Checking outer-session continuity (timeout: 30s).", flush=True)
try:
    parent_run = subprocess.run([str(launcher_root / "lab-vm"), "context", "--json"], cwd=launcher_root,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=30)
except subprocess.TimeoutExpired:
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


def outer_guest_preflight(vm_dir: Path, evidence: Evidence) -> bool:
    non_tool_checks = (
        "context_valid", "outer_guest", "nested_kvm", "workspace_root",
        "workspace_virtiofs", "host_state_excluded", "memory_budget",
    )

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
    if (isinstance(initial_checks, dict) and initial_checks.get("tools") is False
            and isinstance(initial_tools, dict)
            and all(name in initial_checks and initial_checks[name] is True for name in non_tool_checks)
            and all(value is True for name, value in initial_checks.items() if name != "tools")):
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
    names = ("context_valid", "outer_guest", "nested_kvm", "workspace_root", "workspace_virtiofs", "host_state_excluded", "tools", "memory_budget")
    tools = parsed.get("tools", {})
    for name in names:
        passed = checks.get(name) is True
        if passed:
            detail = "check passed"
        elif name == "tools" and isinstance(tools, dict):
            missing = sorted(key for key, available in tools.items() if available is not True)
            detail = f"missing or unavailable: {', '.join(missing) or 'tool inventory incomplete'}; no child was started"
        else:
            detail = "failed; no child was started"
        evidence.check(f"outer guest {name}", passed, detail)
    evidence.data["outer_guest_tools"] = tools
    evidence.data["nested_memory_budget"] = parsed.get("memory_budget", {})
    evidence.save()
    return context_ok and all(checks.get(name) is True for name in names)


def run_preflight(
    evidence: Evidence,
    outer_profile: str,
    snapshot: Path,
    repo: Path = ROOT,
    *,
    allow_active_source_overlay: bool = False,
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
    guest_ok = outer_guest_preflight(outer, evidence) if outer is not None else False
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


def run_nested_remote(
    vm_dir: Path,
    profile: str,
    probe: str,
    evidence: Evidence,
    repo: Path = ROOT,
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
    command = ssh_command(vm_dir, f"cd /workspace && python3 - {profile} {probe}")
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
    marker = "__LM_VM_ACCEPTANCE__"
    summary: dict[str, object] | None = None
    try:
        for line in process.stdout:
            if line.startswith(marker):
                try:
                    value = json.loads(line[len(marker):])
                    summary = value if isinstance(value, dict) else None
                except json.JSONDecodeError:
                    summary = None
            else:
                sys.stdout.write(line)
                sys.stdout.flush()
    except KeyboardInterrupt:
        # Interrupt only the local SSH client. The nested VM is intentionally
        # left running so its profile and diagnostics remain available.
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


def nested_gate(profile: str | None, outer_profile: str, snapshot: Path, repo: Path) -> int:
    evidence = Evidence("nested", "one named disposable nested VM boot, depth-1 context, depth-2 refusal before state, and parent continuity; no model/TUI/auth/workload/isolation matrix/teardown")
    if profile is not None and not PROFILE_RE.fullmatch(profile):
        evidence.check("child profile name", False, "invalid profile name")
        evidence.finish("FAIL")
        return 2
    ready, outer = run_preflight(
        evidence, outer_profile, snapshot, repo,
        allow_active_source_overlay=True,
    )
    if not ready or outer is None:
        evidence.check("nested launch", False, "preflight blocked; no child VM was started")
        evidence.finish("BLOCKED")
        return 2
    if profile is None:
        profile = f"nested-accept-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    evidence.data["child_profile"] = profile
    evidence.save()
    probe = f"depth-cap-{uuid.uuid4().hex[:10]}"
    evidence.data["depth_limit_probe"] = probe
    evidence.save()
    rc, summary = run_nested_remote(outer, profile, probe, evidence, repo)
    checks = summary.get("checks", {}) if isinstance(summary, dict) else {}
    evidence.data["nested_summary"] = summary or {"result": "missing remote summary"}
    if isinstance(checks, dict):
        for name, detail in (
            ("child_up", "new child profile provisioned"),
            ("child_acceptance", "cloud-init, recursion policy, KVM and safe workspace passed"),
            ("depth_limit_refused_before_state", "depth-2 launch refused before profile state"),
            ("outer_session_usable", "outer guest remained usable"),
        ):
            label = name.replace("_", " ")
            if name not in checks:
                evidence.blocked(label, "not reached after an earlier nested-gate failure; preserve state and stop")
            else:
                evidence.check(label, checks[name] is True,
                               detail if checks[name] is True else "failed; preserve child state and stop")
    passed = rc == 0 and isinstance(checks, dict) and all(checks.get(k) is True for k in ("child_up", "child_acceptance", "depth_limit_refused_before_state", "outer_session_usable"))
    if rc != 0 and summary is None:
        evidence.check("nested runner transport", False, "SSH gate stopped before a summary; preserve outer/child diagnostics")
    evidence.finish("PASS" if passed else "FAIL")
    return 0 if passed else (rc or 1)


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
            cmd.add_argument("--profile", help="optional child profile name; otherwise a unique name is generated and retained")
    observe = subs.add_parser("observe", help="record guided human TUI/auth/permission observations")
    observe.add_argument("--gate", choices=sorted(OBSERVATIONS), required=True)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "observe":
            return observe_gate(args.gate)
        if args.command == "preflight":
            evidence = Evidence("preflight", "owner-host checks with safe snapshot reconciliation and non-destructive outer-profile bootstrap; no model services, agent, or teardown")
            ready, _ = run_preflight(evidence, args.outer_profile, args.snapshot.expanduser())
            evidence.finish("PASS" if ready else "BLOCKED")
            return 0 if ready else 2
        if args.command == "nested":
            return nested_gate(args.profile, args.outer_profile, args.snapshot.expanduser(), ROOT)
    except (AcceptanceError, OSError, subprocess.SubprocessError) as exc:
        print(f"FAIL runner: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
