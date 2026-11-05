#!/usr/bin/env python3
"""Plan and run one bounded, run-owned Little Mester VM acceptance campaign.

Planning is read-only. Live work requires ``--execute`` and repeating the exact
plan ID with ``--confirm-run-id``. The runner never selects a pre-existing VM,
installs host packages, changes host networking, or cleans assets outside its
private run manifest. Human observations remain BLOCKED until recorded by a
person through the documented checkpoint.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PATHS_PATH = ROOT / "lib/vm/paths.py"
SNAPSHOT_PATH = ROOT / "scripts/vm-recursion-workspace.py"
MEMORY_PATH = ROOT / "scripts/vm_memory_budget.py"
ISOLATION_PATH = ROOT / "scripts/vm-guest-isolation-check.py"
RUN_NAME_RE = re.compile(r"accept-[a-f0-9]{12}")
PROFILE_RE = re.compile(r"accept-[a-f0-9]{12}-(?:outer|agent|workload)")
RUN_ID_RE = re.compile(r"[a-f0-9]{32}")
MAX_REPORT_BYTES = 2 * 1024 * 1024
DEFAULT_RESERVE_MIB = 512
OWNER_CHECKLISTS = {
    "E2E-DAILY": (
        "native-tui-entry", "two-identical-starts-no-reboot-or-install", "A-B-B-same-overlay-and-keys",
        "warm-start-same-process-identity", "docker-rollback-without-recreation",
    ),
    "E2E-MODEL": (
        "model-none-shell-and-auth", "each-configured-provider-selected", "five-real-streams-per-provider",
        "provider-denial-and-missing-credential-errors", "no-model-response-or-credential-recorded",
    ),
    "E2E-WORK": (
        "guest-only-postgres-compose", "health-and-query", "failing-test-fix-pass",
        "volume-survives-stop-restart", "compose-down", "bind-and-guest-loopback-port",
        "no-host-published-service-or-socket",
    ),
    "E2E-LIFE": (
        "suspend-resume-identity", "auth-db-tools-keys-workspace-retained", "update-scan-cancel-noop",
        "selected-apply-exact-version", "live-child-blocks-parent", "deepest-first-shutdown",
        "resize-memtotal-and-identity", "exact-teardown-no-residue", "base-workspace-reports-preserved",
    ),
    "E2E-AGENTS": (
        "two-authorized-same-guest-tasks", "separate-auth-ports", "clean-agent-cancellation",
        "shared-workspace-risk-explained-no-supervisor-claim",
    ),
    "SEC-FS": (
        "host-file-unreadable", "control-key-and-socket-unreadable", "child-out-of-scope-files-unreadable",
        "escaping-symlinks-refused", "state-root-overlap-blocked", "one-export-verified",
    ),
    "SEC-SECRET": (
        "canary-absent-from-snapshot-and-history", "canary-absent-from-seed-base-log-report",
        "provider-forwarding-opt-in-only", "private-atomic-export-import", "export-failure-prevents-delete",
        "refreshed-state-survives-restart-and-import",
    ),
    "HUMAN-TUI": (
        "single-command-entry", "warm-start-no-reboot", "metrics-sidebar-and-tool",
        "ctrl-c-cancel-and-confirm", "retry-rendering", "version-matched",
    ),
    "HUMAN-PERM": (
        "auto-allowed-tool", "auto-deny-preserved", "ask-mode-prompt",
        "shared-standalone-match", "argv-ui-action-recorded",
    ),
    "HUMAN-AUTH": (
        "browser-success", "cancel-interrupt-timeout-occupied-port-egress-denied",
        "auth-list-and-completion", "temporary-callback-removed", "refresh-persisted",
        "disposable-delete-reimport", "recursive-provider-import",
    ),
}
HUMAN_CHECKLISTS = {key: value for key, value in OWNER_CHECKLISTS.items() if key.startswith("HUMAN-")}
OWNER_OBSERVATION_GATES = frozenset(OWNER_CHECKLISTS)

GATE_IDS = (
    "HOST-PREFLIGHT", "HOST-CLEAN", "E2E-DAILY", "E2E-MODEL", "E2E-WORK",
    "E2E-SELF", "E2E-DEPTH", "E2E-LIFE", "E2E-AGENTS", "SEC-FS", "SEC-NET",
    "SEC-SECRET", "HUMAN-TUI", "HUMAN-PERM", "HUMAN-AUTH",
)
SUITE_GATES = {
    "preflight": ("HOST-PREFLIGHT",),
    "clean": ("HOST-PREFLIGHT", "HOST-CLEAN"),
    "daily": ("HOST-PREFLIGHT", "HOST-CLEAN", "E2E-DAILY"),
    "model": ("HOST-PREFLIGHT", "HOST-CLEAN", "E2E-MODEL"),
    "workload": ("HOST-PREFLIGHT", "HOST-CLEAN", "E2E-WORK"),
    "boundary": ("HOST-PREFLIGHT", "HOST-CLEAN", "SEC-FS", "SEC-NET", "SEC-SECRET"),
    "lifecycle": ("HOST-PREFLIGHT", "HOST-CLEAN", "E2E-LIFE"),
    "agents": ("HOST-PREFLIGHT", "HOST-CLEAN", "E2E-AGENTS"),
    "human": ("HOST-PREFLIGHT", "HOST-CLEAN", "HUMAN-TUI", "HUMAN-PERM", "HUMAN-AUTH"),
    "self": ("HOST-PREFLIGHT", "HOST-CLEAN", "E2E-SELF", "E2E-DEPTH"),
}
ALL_SUITES = tuple(SUITE_GATES)


class AcceptanceError(ValueError):
    """Unsafe scope, unavailable precondition, or invalid evidence."""


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AcceptanceError("acceptance dependency is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _private_directory(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700 or path.is_symlink()
            or path.resolve(strict=True) != path):
        raise AcceptanceError("state and evidence directories must be canonical, owned, private 0700 directories")


def _canonical_directory(value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise AcceptanceError("source and state-root paths must be absolute and canonical")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise AcceptanceError("source or state root is unavailable") from error
    if resolved != path or not path.is_dir() or path.is_symlink():
        raise AcceptanceError("source and state-root paths must be canonical directories")
    return path


def _run_id(value: str | None) -> str:
    selected = value or uuid.uuid4().hex
    if RUN_ID_RE.fullmatch(selected) is None:
        raise AcceptanceError("run ID must be 32 lowercase hexadecimal characters")
    return selected


def _run_name(run_id: str) -> str:
    return f"accept-{run_id[:12]}"


def _profile_name(run_id: str, role: str = "outer") -> str:
    if role not in {"outer", "agent", "workload"}:
        raise AcceptanceError("unknown acceptance profile role")
    return f"{_run_name(run_id)}-{role}"


def _namespace_path(state_root: Path, run_name: str) -> Path:
    digest = hashlib.sha256(os.fsencode(state_root)).hexdigest()[:24]
    return state_root.parent / f".little-mester-recursion-{digest}" / run_name


def _source_identity(source: Path) -> dict[str, Any]:
    snapshot = _load_module("vm_host_acceptance_snapshot", SNAPSHOT_PATH)
    manifest, _fingerprints = snapshot.inventory(source, 2 * 1024**3)
    return {
        "source_head": manifest["source_head"],
        "tree_sha256": manifest["tree_sha256"],
        "entry_count": manifest["entry_count"],
        "total_bytes": manifest["total_bytes"],
    }


def _read_mem_budget(requested_mib: int, reserve_mib: int) -> dict[str, Any]:
    budget = _load_module("vm_host_acceptance_memory", MEMORY_PATH)
    passed, details = budget.check_budget(requested_mib, reserve_mib)
    return {"passed": passed, **details}


def _socket_listener_summary() -> dict[str, int | bool]:
    """Read listener counts without retaining addresses or arbitrary process text."""
    listeners = 0
    loopback = 0
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(name).read_text(encoding="ascii").splitlines()[1:]
        except (OSError, UnicodeError):
            return {"readable": False, "listeners": 0, "loopback": 0}
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            listeners += 1
            local = fields[1].split(":", 1)[0]
            if ((name.endswith("tcp") and local == "0100007F")
                    or (name.endswith("tcp6") and local == "00000000000000000000000001000000")):
                loopback += 1
    return {"readable": True, "listeners": listeners, "loopback": loopback}


def preflight(source: Path, state_root: Path, run_id: str, memory_gib: int = 8,
              suites: list[str] | None = None) -> dict[str, Any]:
    """Read-only capability, ownership, resource, revision and collision check."""
    _private_directory(state_root)
    selected_suites = set(suites or ())
    source_identity = _source_identity(source)
    if source == state_root or source in state_root.parents or state_root in source.parents:
        raise AcceptanceError("source checkout and private VM state root must be disjoint")
    if not 1 <= memory_gib <= 128:
        raise AcceptanceError("requested memory must be between 1 and 128 GiB")

    checks: list[dict[str, Any]] = []

    def record(name: str, passed: bool, detail: dict[str, Any] | None = None, *, blocked: bool = False) -> None:
        checks.append({"id": name, "status": "PASS" if passed else "BLOCKED" if blocked else "FAIL",
                       "detail": detail or {}})

    supported = platform.system() == "Linux" and platform.machine() == "x86_64"
    record("supported_linux_x86_64", supported, {"system": platform.system(), "machine": platform.machine()}, blocked=True)
    kvm = Path("/dev/kvm")
    try:
        kvm_info = kvm.stat()
        kvm_ok = stat.S_ISCHR(kvm_info.st_mode) and os.access(kvm, os.R_OK | os.W_OK)
    except OSError:
        kvm_ok = False
    record("kvm_device_read_write", kvm_ok, blocked=True)
    try:
        flags = Path("/proc/cpuinfo").read_text(encoding="ascii", errors="ignore")
        cpu_ok = any(re.search(r"\b(?:vmx|svm)\b", line) for line in flags.splitlines() if line.startswith("flags"))
    except OSError:
        cpu_ok = False
    record("cpu_virtualization_exposed", cpu_ok, blocked=True)

    required_tools = (
        "qemu-system-x86_64", "qemu-img", "virtiofsd", "ssh", "ssh-keyscan",
        "ssh-keygen", "curl", "python3", "socat", "flock", "timeout", "git",
    )
    missing = [name for name in required_tools if shutil.which(name) is None]
    if not shutil.which("genisoimage") and not shutil.which("mkisofs"):
        missing.append("genisoimage-or-mkisofs")
    record("required_tools", not missing, {"missing_count": len(missing)}, blocked=bool(missing))

    try:
        memory = _read_mem_budget(memory_gib * 1024, DEFAULT_RESERVE_MIB)
        record("memory_budget", memory["passed"], {
            key: memory.get(key) for key in (
                "memavailable_mib", "cgroup_remaining_mib", "projected_available_mib",
                "requested_mib", "reserve_mib",
            )
        }, blocked=not memory["passed"])
    except (OSError, RuntimeError, ValueError):
        memory = {"passed": False}
        record("memory_budget", False, {"readable": False}, blocked=True)

    if "lifecycle" in selected_suites:
        resize_budget_ok = memory_gib >= 8
        record("lifecycle_resize_memory", resize_budget_ok,
               {"minimum_gib": 8, "requested_gib": memory_gib}, blocked=not resize_budget_ok)

    disk_parent = state_root.parent
    disk = shutil.disk_usage(disk_parent)
    copy_factor = 5 if "self" in selected_suites else 2 if "daily" in selected_suites else 1
    estimated = max(
        2 * 1024**3,
        2 * 1024**3 + source_identity["total_bytes"] * copy_factor + 128 * 1024**2,
    )
    disk_ok = disk.free >= estimated
    record("snapshot_disk_budget", disk_ok, {
        "free_bytes": disk.free, "required_bytes": estimated,
    }, blocked=not disk_ok)
    listener_summary = _socket_listener_summary()
    record("listener_inventory_readable", listener_summary["readable"], {
        "listener_count": listener_summary["listeners"],
        "loopback_count": listener_summary["loopback"],
    }, blocked=not listener_summary["readable"])

    namespace = _namespace_path(state_root, _run_name(run_id))
    collision = namespace.exists() or namespace.is_symlink()
    record("unique_run_namespace", not collision, {"collision": collision}, blocked=collision)
    namespace_parent = namespace.parent
    parent_safe = True
    if namespace_parent.exists() or namespace_parent.is_symlink():
        try:
            _private_directory(namespace_parent)
        except AcceptanceError:
            parent_safe = False
    record("private_campaign_parent", parent_safe, blocked=not parent_safe)
    report_path = namespace.parent / "reports" / f"{_run_name(run_id)}.json"
    report_collision = report_path.exists() or report_path.is_symlink()
    record("unique_run_evidence", not report_collision,
           {"collision": report_collision}, blocked=report_collision)
    report_root_safe = True
    if report_path.parent.exists() or report_path.parent.is_symlink():
        try:
            _private_directory(report_path.parent)
        except AcceptanceError:
            report_root_safe = False
    record("private_evidence_directory", report_root_safe, blocked=not report_root_safe)
    return {
        "schema_version": 1,
        "run_id": run_id,
        "created_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "deadlines_seconds": {
            "host_preflight": 30, "guest_probe": 120, "guest_boot": 1900,
            "graceful_suspend": 90, "update_scan": 300, "update_apply": 900,
        },
        "execution_context": {"system": platform.system(), "machine": platform.machine()},
        "source": source_identity,
        "memory": {key: memory.get(key) for key in (
            "memavailable_mib", "cgroup_remaining_mib", "projected_available_mib",
            "requested_mib", "reserve_mib", "passed",
        )},
        "checks": checks,
        "result": "PASS" if all(item["status"] == "PASS" for item in checks) else "BLOCKED",
    }


LIFECYCLE_SCRIPT = r'''set -euo pipefail
repo=$1 state_root=$2 profile=$3 workspace=$4 operation=$5 memory=$6 confirm=$7 depth=$8 maximum=$9
source "$repo/lib/vm/config.sh"
vm_cli_defaults
VM_STATE_ROOT="$state_root"
VM_PATHS_HELPER="$repo/lib/vm/paths.py"
source "$repo/lib/vm/disk.sh"
source "$repo/lib/vm/recursion.sh"
source "$repo/lib/vm/lifecycle.sh"
vm_config_overrides
vm_config_load
case "$operation" in
  up)
    VM_COMMAND=up VM_MAX_DEPTH="$maximum" VM_SEEN[max-depth]=1
    if (( depth > 0 )); then
      LAB_VM_RECURSION_DEPTH=$((depth - 1)) LAB_VM_MAX_DEPTH="$maximum"
      VM_RECURSE=1 VM_UNSAFE_RECURSE=0
    else
      unset LAB_VM_RECURSION_DEPTH LAB_VM_MAX_DEPTH
      VM_RECURSE=0 VM_UNSAFE_RECURSE=0
    fi
    vm_recursion_configure up
    [[ "$VM_GUEST_RECURSION_DEPTH" == "$depth" ]]
    if (( VM_NESTED_LAUNCH )); then vm_recursion_require_nested_kvm /dev/kvm /proc/cpuinfo; fi
    VM_LIFECYCLE_ROOT="$workspace"
    vm_lifecycle_up "$state_root" "$profile" "$workspace" "$workspace" \
      "$VM_GUEST_RECURSION_DEPTH" "$VM_EFFECTIVE_MAX_DEPTH" "$memory" \
      "$VM_RECURSE" "$VM_UNSAFE_RECURSE" 0 60 1800
    ;;
  status)
    vm_lifecycle_status "$state_root" "$profile"
    ;;
  suspend)
    runtime="$state_root/vm/profiles/$profile/runtime/lab-vm"
    vm_lifecycle_suspend "$state_root" "$profile" 0 60 "$runtime"
    ;;
  resume)
    vm_lifecycle_resume "$state_root" "$profile" "$workspace" "$workspace" 1 0 0 60 none "" "" 1800
    ;;
  resize)
    vm_lifecycle_resize "$state_root" "$profile" "$memory" 0 60
    ;;
  update-scan)
    vm_lifecycle_update "$state_root" "$profile" 0 0 0 config
    ;;
  update-apply)
    vm_lifecycle_update "$state_root" "$profile" 1 0 0 config tools
    ;;
  delete)
    [[ "$confirm" == "$profile" ]] || exit 2
    vm_lifecycle_delete "$state_root" "$profile" 0 1 0 60
    ;;
  *) exit 2 ;;
esac
'''


def _lifecycle_call(state_root: Path, profile: str, workspace: Path, operation: str,
                    memory_gib: int = 8, *, depth: int = 0, maximum: int = 2,
                    confirm: str = "", timeout: int = 1900) -> subprocess.CompletedProcess:
    if PROFILE_RE.fullmatch(profile) is None:
        raise AcceptanceError("profile is outside the generated campaign namespace")
    if operation == "delete" and confirm != profile:
        raise AcceptanceError("exact profile confirmation is required for cleanup")
    args = ["bash", "-c", LIFECYCLE_SCRIPT, "vm-acceptance", str(ROOT), str(state_root),
            profile, str(workspace), operation, str(memory_gib), confirm, str(depth), str(maximum)]
    try:
        return subprocess.run(
            args, cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True,
            check=False, timeout=timeout,
            env={**os.environ, "MESTER_STATE_DIR": str(state_root), "VM_INSTALL_PREREQS": "0"},
        )
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("lifecycle operation exceeded its bounded deadline") from error


def _profile_record(state_root: Path, profile: str) -> dict[str, Any]:
    if PROFILE_RE.fullmatch(profile) is None:
        raise AcceptanceError("profile is outside the generated campaign namespace")
    try:
        result = subprocess.run(
            [sys.executable, str(PATHS_PATH), "profile-read", "--state-root", str(state_root), "--profile", profile],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            check=False, timeout=10,
        )
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("profile identity read exceeded its bounded deadline") from error
    if result.returncode != 0:
        raise AcceptanceError("generated profile metadata is unavailable")
    try:
        value = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AcceptanceError("generated profile metadata is invalid") from error
    if value.get("name") != profile:
        raise AcceptanceError("profile name did not match the exact campaign target")
    return value


def _profile_run_directory(state_root: Path, profile: str, data: dict[str, Any]) -> Path:
    """Resolve the one registry that records this exact live QEMU identity."""
    runs = state_root / "vm/profiles" / profile / "runs"
    _private_directory(runs)
    identity = data.get("runtime_identity")
    if not isinstance(identity, dict):
        raise AcceptanceError("exact live process identity is unavailable")
    try:
        expected = {key: identity[key] for key in ("pid", "start_ticks", "executable", "argv")}
        process = _load_module("vm_host_acceptance_registry_process", ROOT / "lib/vm/process.py")
        matches: list[Path] = []
        for run_dir in runs.iterdir():
            _private_directory(run_dir)
            registry = run_dir / "processes.json"
            record = process.registry_read(registry, run_dir.name)
            for role, item in record["processes"].items():
                if role.startswith("qemu_") and item.get("identity") == expected:
                    matches.append(run_dir)
        if len(matches) != 1:
            raise AcceptanceError("exact profile process registry is unavailable or ambiguous")
        return matches[0]
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
        raise AcceptanceError("exact profile process registry is unsafe") from error


def _hash_file(path: Path) -> str | None:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _read_private_json(path: Path, maximum: int = MAX_REPORT_BYTES) -> dict[str, Any]:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise AcceptanceError("private campaign evidence is unavailable") from error
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > maximum):
            raise AcceptanceError("private campaign evidence is unsafe")
        chunks = bytearray()
        while len(chunks) <= maximum:
            block = os.read(fd, min(65536, maximum + 1 - len(chunks)))
            if not block:
                break
            chunks.extend(block)
        if len(chunks) > maximum or len(chunks) != info.st_size:
            raise AcceptanceError("private campaign evidence is oversized or changed")
        value = json.loads(chunks.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AcceptanceError("private campaign evidence is invalid") from error
    finally:
        os.close(fd)
    if not isinstance(value, dict):
        raise AcceptanceError("private campaign evidence has an invalid schema")
    return value


def _listener_owned_loopback(profile: dict[str, Any]) -> bool:
    identity = profile.get("runtime_identity")
    if not isinstance(identity, dict):
        return False
    argv = identity.get("argv")
    port = identity.get("ssh_port")
    pid = identity.get("pid")
    if not isinstance(argv, list) or type(port) is not int or type(pid) is not int:
        return False
    try:
        process = _load_module("vm_host_acceptance_process", ROOT / "lib/vm/process.py")
        process_identity = {key: identity[key] for key in ("pid", "start_ticks", "executable", "argv")}
        if process.identity_state(process_identity) != "running":
            return False
        owned_inodes = {
            os.readlink(descriptor)[8:-1]
            for descriptor in Path(f"/proc/{pid}/fd").iterdir()
            if os.readlink(descriptor).startswith("socket:[") and os.readlink(descriptor).endswith("]")
        }
    except (KeyError, OSError, RuntimeError, ValueError):
        return False
    if "-enable-kvm" not in argv or not any("restrict=on" in item for item in argv):
        return False
    expected = f"hostfwd=tcp:127.0.0.1:{port}-:22"
    if not any(expected in item for item in argv):
        return False
    found: set[str] = set()
    ssh_listener_found = False
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            rows = table.read_text(encoding="ascii").splitlines()[1:]
        except (OSError, UnicodeError):
            return False
        for row in rows:
            columns = row.split()
            if len(columns) < 10 or columns[3] != "0A":
                continue
            inode = columns[9]
            if inode not in owned_inodes:
                continue
            local_hex, _remote = columns[1].split(":", 1)
            actual_port = int(columns[1].rsplit(":", 1)[1], 16)
            if table.name == "tcp":
                loopback = local_hex == "0100007F"
            else:
                loopback = local_hex == "00000000000000000000000001000000"
            if not loopback:
                return False
            found.add(inode)
            if actual_port == port:
                ssh_listener_found = True
    return bool(found) and ssh_listener_found


def _guest_exec(state_root: Path, profile: str, command: str, *, timeout: int = 120) -> subprocess.CompletedProcess:
    if (PROFILE_RE.fullmatch(profile) is None or "\n" in command or len(command) > 60_000
            or type(timeout) is not int or not 1 <= timeout <= 1800):
        raise AcceptanceError("guest command is outside the fixed campaign contract")
    data = _profile_record(state_root, profile)
    if data.get("status") != "ready" or not _listener_owned_loopback(data):
        raise AcceptanceError("profile or owned loopback listener is not verified")
    run_dir = _profile_run_directory(state_root, profile, data)
    key = state_root / "vm/profiles" / profile / "control-key"
    known_hosts = run_dir / "known_hosts"
    remote_script = r'''set -euo pipefail
repo=$1 state_root=$2 profile=$3 key=$4 known_hosts=$5 command=$6 deadline=$7
source "$repo/lib/vm/config.sh"
source "$repo/lib/vm/disk.sh"
source "$repo/lib/vm/lifecycle.sh"
VM_LIFECYCLE_ROOT="$repo"
data="$(vm_lifecycle_profile_read "$state_root" "$profile")"
info="$(vm_lifecycle_registry_for_profile "$state_root" "$profile" "$data")"
registry="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["registry"])' <<<"$info")"
run_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])' <<<"$info")"
role="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["role"])' <<<"$info")"
port="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["runtime_identity"]["ssh_port"])' <<<"$data")"
source "$repo/lib/vm/ssh.sh"
vm_ssh_listener_verified "$registry" "$run_id" "$role" "$port"
vm_ssh_noninteractive "$key" "$known_hosts" "$port" "$deadline" mester@127.0.0.1 "$command"
'''
    try:
        completed = subprocess.run(
            ["bash", "-c", remote_script, "vm-acceptance-isolation", str(ROOT), str(state_root),
             profile, str(key), str(known_hosts), command, str(timeout)],
            stdin=subprocess.DEVNULL, capture_output=True,
            check=False, timeout=timeout + 30, cwd=ROOT,
        )
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("bounded guest command timed out") from error
    if completed.returncode != 0:
        raise AcceptanceError("bounded guest command failed")
    return completed


def _guest_clean_checks(state_root: Path, profile: str, workspace: Path,
                        host_uid: int, host_gid: int) -> dict[str, Any]:
    code = r'''import json,os,stat,subprocess
from pathlib import Path
checks={"cloud_init_clean":False,"workspace_mount":False,"workspace_uid_gid_match":False,"node_available":False,
"mcp_config_valid":False,"mcp_build_present":False,"guest_docker_available":False,
}
opencode_version=None
try:
    cloud=subprocess.run(["cloud-init","status","--wait"],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=45)
    checks["cloud_init_clean"]=(cloud.returncode==0 and len(cloud.stdout)<4096)
    for line in Path("/proc/mounts").read_text().splitlines():
        fields=line.split()
        if len(fields)>=3 and fields[1]=="/workspace": checks["workspace_mount"]=(fields[2]=="virtiofs")
    info=Path("/workspace").stat(); checks["workspace_uid_gid_match"]=(info.st_uid==int(os.environ["LM_EXPECT_UID"]) and info.st_gid==int(os.environ["LM_EXPECT_GID"]))
    node=subprocess.run(["node","--version"],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=5)
    checks["node_available"]=(node.returncode==0 and len(node.stdout)<64)
    opencode=subprocess.run(["opencode","--version"],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=5)
    if opencode.returncode==0 and len(opencode.stdout)<128:
        candidate=opencode.stdout.decode("ascii","ignore").strip()
        if __import__("re").fullmatch(r"2\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?",candidate): opencode_version=candidate
    cfg=Path("/home/mester/.config/opencode/opencode.json"); runtime=Path("/home/mester/.local/share/little-mester/guest-runtime/mcp/dist/mcp-server.js")
    data=json.loads(cfg.read_text()); server=data["mcp"]["servers"]["little-mester"]
    checks["mcp_config_valid"]=(server.get("type")=="local" and server.get("command")==["node","dist/mcp-server.js"] and server.get("cwd")=="/home/mester/.local/share/little-mester/guest-runtime/mcp")
    checks["mcp_build_present"]=(runtime.is_file() and not runtime.is_symlink() and runtime.stat().st_size>0)
    docker=subprocess.run(["docker","info","--format","{{.ServerVersion}}"],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=10)
    checks["guest_docker_available"]=(docker.returncode==0 and len(docker.stdout)<128)
except Exception:
    pass
print(json.dumps({"checks":checks,"opencode_version":opencode_version},sort_keys=True,separators=(",",":")))'''
    command = "env LM_EXPECT_UID=" + str(host_uid) + " LM_EXPECT_GID=" + str(host_gid) + " python3 -c " + shlex.quote(code)
    result = _guest_exec(state_root, profile, command, timeout=75)
    try:
        value = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AcceptanceError("guest clean-check report is invalid") from error
    checks = value.get("checks")
    required = {"cloud_init_clean", "workspace_mount", "workspace_uid_gid_match", "node_available",
                "mcp_config_valid", "mcp_build_present", "guest_docker_available"}
    version = value.get("opencode_version")
    if (value.keys() != {"checks", "opencode_version"} or not isinstance(checks, dict) or checks.keys() != required
            or any(type(item) is not bool for item in checks.values())):
        raise AcceptanceError("guest clean-check report does not match the fixed schema")
    version_ok = isinstance(version, str) and re.fullmatch(r"2\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", version)
    passed = version_ok and all(value is True for value in checks.values())
    return {"checks": checks, "opencode_version": version if version_ok else None,
            "host_docker_dependency": "NOT_REQUIRED", "result": "PASS" if passed else "FAIL"}


def _cached_image_integrity(state_root: Path, profile: str) -> dict[str, Any]:
    data = _profile_record(state_root, profile)
    digest = data.get("image_sha256")
    image_path = data.get("image_path")
    if (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or image_path != str(state_root / "vm/images" / digest / "base.qcow2")):
        return {"status": "FAIL", "artifact_sha256": None}
    command = r'''set -euo pipefail
repo=$1 cache=$2
source "$repo/lib/vm/disk.sh"
[[ "$VM_DISK_PIN_SHA256" == "$(basename "$(dirname "$cache")")" ]]
vm_disk_manifest_valid "$cache" "$VM_DISK_PIN_URL" "$VM_DISK_PIN_SHA256" \
  "$((VM_BASE_SIZE * 1024 * 1024 * 1024))"'''
    try:
        result = subprocess.run(
            ["bash", "-c", command, "vm-acceptance-image", str(ROOT),
             str(Path(image_path).parent)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {"status": "BLOCKED", "artifact_sha256": None, "reason_code": "image_check_timeout"}
    artifact_hash = None
    if result.returncode == 0:
        try:
            manifest = _read_private_json(Path(image_path).parent / "manifest.json", maximum=4096)
            candidate_hash = manifest.get("artifact_sha256")
            if isinstance(candidate_hash, str) and re.fullmatch(r"[a-f0-9]{64}", candidate_hash):
                artifact_hash = candidate_hash
        except AcceptanceError:
            pass
    return {
        "status": "PASS" if artifact_hash is not None else "FAIL",
        "artifact_sha256": artifact_hash,
    }


def _guest_isolation(state_root: Path, profile: str, workspace: Path, run_id: str) -> dict[str, Any]:
    data = _profile_record(state_root, profile)
    depth = data.get("depth")
    maximum = data.get("max_depth")
    if (type(depth) is not int or depth not in {0, 1}
            or type(maximum) is not int or maximum != 2):
        return {"status": "BLOCKED", "reason_code": "profile_recursion_identity_unverified"}
    guest_command = (
        "python3 /workspace/scripts/vm-guest-isolation-check.py --execute "
        f"--run-id {run_id} --confirm-run-id {run_id} "
        f"--expected-depth {depth} --expected-max-depth {maximum}"
    )
    try:
        completed = _guest_exec(state_root, profile, guest_command, timeout=120)
    except TimeoutError:
        return {"status": "FAIL", "reason_code": "probe_timeout"}
    except AcceptanceError:
        return {"status": "BLOCKED", "reason_code": "profile_or_listener_unverified"}
    try:
        report = json.loads(completed.stdout)
    except (UnicodeError, json.JSONDecodeError):
        return {"status": "FAIL", "reason_code": "invalid_guest_report"}
    safe_checks = report.get("checks") if isinstance(report, dict) else None
    if (not isinstance(report, dict) or report.get("schema_version") != 1 or report.get("run_id") != run_id
            or not isinstance(safe_checks, list) or len(safe_checks) != 12
            or {item.get("id") for item in safe_checks if isinstance(item, dict)} != {
                "guest_identity", "workspace_mount", "ancestor_capabilities_absent",
                "qemu_host_gateway_blocked", "metadata_endpoint_blocked",
                "direct_private_subnet_blocked", "direct_public_tcp_blocked", "direct_dns_blocked",
                "direct_udp_blocked", "proxy_private_destination_denied", "proxy_public_http_available",
                "proxy_public_https_available",
            }
            or any(not isinstance(item, dict) or set(item) != {"id", "status"}
                   or item["id"] not in {"guest_identity", "workspace_mount", "ancestor_capabilities_absent",
                                         "qemu_host_gateway_blocked",
                                         "metadata_endpoint_blocked", "direct_private_subnet_blocked",
                                         "direct_public_tcp_blocked", "direct_dns_blocked", "direct_udp_blocked",
                                         "proxy_private_destination_denied", "proxy_public_http_available",
                                         "proxy_public_https_available"}
                   or item["status"] not in {"PASS", "FAIL"} for item in safe_checks)):
        return {"status": "FAIL", "reason_code": "invalid_guest_report"}
    checks_pass = all(item["status"] == "PASS" for item in safe_checks)
    if (type(report.get("elapsed_ms")) is not int or report["elapsed_ms"] < 0
            or not isinstance(report.get("created_utc"), str)
            or report.get("result") != ("PASS" if checks_pass else "FAIL")):
        return {"status": "FAIL", "reason_code": "invalid_guest_report"}
    return {
        "status": "PASS" if checks_pass and _listener_owned_loopback(data) else "FAIL",
        "guest_checks": safe_checks,
        "host_listener_check": "PASS" if _listener_owned_loopback(data) else "FAIL",
        "source_tree_sha256": _source_identity(workspace)["tree_sha256"],
    }


def _runtime_identity_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    keys = ("pid", "start_ticks", "executable", "argv")
    return all(left.get(key) == right.get(key) for key in keys)


def _overlay_identity(state_root: Path, profile: str) -> tuple[int, int] | None:
    path = state_root / "vm/profiles" / profile / "disk.qcow2"
    try:
        info = path.lstat()
    except OSError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
        return None
    return info.st_dev, info.st_ino


def _daily_lifecycle_checks(state_root: Path, profile: str, source: Path, run_root: Path,
                            memory_gib: int) -> dict[str, Any]:
    """Exercise warm identity and A→B→B via the frozen lifecycle API only."""
    initial = _profile_record(state_root, profile)
    if initial.get("status") != "ready" or not _listener_owned_loopback(initial):
        return {"status": "BLOCKED", "reason_code": "outer_profile_not_verified_ready"}
    overlay_before = _overlay_identity(state_root, profile)
    key_path = state_root / "vm/profiles" / profile / "control-key"
    key_before = _hash_file(key_path)
    if overlay_before is None or key_before is None:
        return {"status": "BLOCKED", "reason_code": "profile_state_identity_unavailable"}

    warm_results: list[subprocess.CompletedProcess] = []
    current_identity = initial.get("runtime_identity")
    for _ in range(2):
        result = _lifecycle_call(state_root, profile, Path(initial["workspace"]), "resume",
                                 memory_gib, timeout=1900)
        warm_results.append(result)
        if result.returncode != 0:
            return {
                "status": "BLOCKED" if result.returncode == 2 else "FAIL",
                "reason_code": "warm_start_failed", "cause_exit_code": result.returncode,
            }
        current = _profile_record(state_root, profile)
        identity = current.get("runtime_identity")
        if (current.get("status") != "ready" or not isinstance(identity, dict)
                or not _runtime_identity_equal(current_identity, identity)
                or not _listener_owned_loopback(current)):
            return {"status": "FAIL", "reason_code": "warm_process_identity_changed"}
        current_identity = identity

    snapshot_root = run_root / "workspace-b-snapshot"
    if snapshot_root.exists() or snapshot_root.is_symlink():
        return {"status": "FAIL", "reason_code": "workspace_b_collision"}
    snapshot_root.mkdir(mode=0o700)
    snapshot = _load_module("vm_host_acceptance_daily_snapshot", SNAPSHOT_PATH)
    try:
        prepared = snapshot.prepare(source, snapshot_root, 2 * 1024**3)
    except (OSError, RuntimeError, ValueError):
        return {"status": "BLOCKED", "reason_code": "workspace_b_snapshot_unavailable"}
    workspace_b = Path(prepared["workspace"])
    switch = _lifecycle_call(state_root, profile, workspace_b, "resume", memory_gib, timeout=1900)
    if switch.returncode != 0:
        return {"status": "BLOCKED" if switch.returncode == 2 else "FAIL",
                "reason_code": "workspace_switch_failed", "cause_exit_code": switch.returncode}
    switched = _profile_record(state_root, profile)
    switched_identity = switched.get("runtime_identity")
    retained = (
        switched.get("workspace") == str(workspace_b)
        and switched.get("profile_id") == initial.get("profile_id")
        and switched.get("image_sha256") == initial.get("image_sha256")
        and switched.get("guest_payload_sha256") == initial.get("guest_payload_sha256")
        and _overlay_identity(state_root, profile) == overlay_before
        and _hash_file(key_path) == key_before
        and isinstance(switched_identity, dict)
        and _listener_owned_loopback(switched)
    )
    if not retained:
        return {"status": "FAIL", "reason_code": "workspace_switch_state_not_retained"}

    warm_after_switch: list[subprocess.CompletedProcess] = []
    for _ in range(2):
        result = _lifecycle_call(state_root, profile, workspace_b, "resume", memory_gib, timeout=1900)
        warm_after_switch.append(result)
        if result.returncode != 0:
            return {"status": "BLOCKED" if result.returncode == 2 else "FAIL",
                    "reason_code": "workspace_b_warm_start_failed", "cause_exit_code": result.returncode}
        current = _profile_record(state_root, profile)
        identity = current.get("runtime_identity")
        if (current.get("status") != "ready" or current.get("workspace") != str(workspace_b)
                or not isinstance(identity, dict)
                or not _runtime_identity_equal(switched_identity, identity)
                or _overlay_identity(state_root, profile) != overlay_before
                or _hash_file(key_path) != key_before or not _listener_owned_loopback(current)):
            return {"status": "FAIL", "reason_code": "workspace_b_warm_identity_changed"}
        switched_identity = identity

    return {
        "status": "PASS",
        "profile_name": profile,
        "workspace_a_tree_sha256": _source_identity(Path(initial["workspace"]))["tree_sha256"],
        "workspace_b_tree_sha256": prepared["tree_sha256"],
        "workspace_b_distinct": Path(initial["workspace"]) != workspace_b,
        "same_overlay_and_key": True,
        "warm_process_identity_stable": True,
        "warm_resume_count": len(warm_results) + len(warm_after_switch),
        "docker_rollback_route_staged": (
            (ROOT / "lab-docker").is_file() and not (ROOT / "lab-docker").is_symlink()
            and os.access(ROOT / "lab-docker", os.X_OK)
        ),
    }


def _guest_memory_mib(state_root: Path, profile: str) -> int | None:
    command = (
        "python3 -c 'import json; from pathlib import Path; "
        "value=next(int(line.split()[1]) for line in Path(\"/proc/meminfo\").read_text().splitlines() "
        "if line.startswith(\"MemTotal:\")); print(json.dumps({\"memtotal_kib\":value}))'"
    )
    try:
        result = _guest_exec(state_root, profile, command, timeout=30)
        value = json.loads(result.stdout)
    except (AcceptanceError, TimeoutError, UnicodeError, json.JSONDecodeError, StopIteration):
        return None
    kib = value.get("memtotal_kib") if isinstance(value, dict) else None
    return kib // 1024 if type(kib) is int and kib > 0 else None


def _lifecycle_retention_checks(state_root: Path, profile: str, workspace: Path,
                                memory_gib: int, apply_selected_updates: bool) -> dict[str, Any]:
    if memory_gib < 8:
        return {"status": "BLOCKED", "reason_code": "lifecycle_resize_requires_8_gib_outer_guest"}
    before = _profile_record(state_root, profile)
    key_path = state_root / "vm/profiles" / profile / "control-key"
    key_hash = _hash_file(key_path)
    overlay_identity = _overlay_identity(state_root, profile)
    if (before.get("status") != "ready" or key_hash is None or overlay_identity is None
            or not _listener_owned_loopback(before)):
        return {"status": "BLOCKED", "reason_code": "outer_profile_not_verified_ready"}
    profile_identity = before.get("profile_id")
    image_hash = before.get("image_sha256")
    workspace_path = before.get("workspace")
    runtime_payload_hash = before.get("runtime_payload_sha256")
    suspend = _lifecycle_call(state_root, profile, workspace, "suspend", memory_gib, timeout=90)
    if suspend.returncode != 0:
        return {"status": "BLOCKED" if suspend.returncode == 2 else "FAIL",
                "reason_code": "graceful_suspend_failed", "suspend_exit_code": suspend.returncode}
    suspended = _profile_record(state_root, profile)
    if (suspended.get("status") != "suspended" or suspended.get("profile_id") != profile_identity
            or _overlay_identity(state_root, profile) != overlay_identity
            or _hash_file(key_path) != key_hash):
        return {"status": "FAIL", "reason_code": "suspend_state_not_retained"}
    resume = _lifecycle_call(state_root, profile, workspace, "resume", memory_gib, timeout=1900)
    if resume.returncode != 0:
        return {"status": "BLOCKED" if resume.returncode == 2 else "FAIL",
                "reason_code": "graceful_resume_failed", "resume_exit_code": resume.returncode}
    after = _profile_record(state_root, profile)
    if (after.get("status") != "ready" or after.get("profile_id") != profile_identity
            or after.get("image_sha256") != image_hash or after.get("workspace") != workspace_path
            or after.get("runtime_payload_sha256") != runtime_payload_hash
            or _overlay_identity(state_root, profile) != overlay_identity
            or _hash_file(key_path) != key_hash or not _listener_owned_loopback(after)):
        return {"status": "FAIL", "reason_code": "resume_state_not_retained"}

    scan = _lifecycle_call(state_root, profile, workspace, "update-scan", memory_gib, timeout=360)
    scan_text = scan.stdout.decode("ascii", "ignore")
    if scan.returncode != 0 or not any(marker in scan_text for marker in (
            "CURRENT  selected guest targets already match",
            "CANCELLED  guest and profile state were not changed",
    )):
        return {"status": "BLOCKED" if scan.returncode == 2 else "FAIL",
                "reason_code": "update_scan_or_cancel_failed", "update_scan_exit_code": scan.returncode}
    apply_result = "NOT_AUTHORIZED"
    if apply_selected_updates:
        applied = _lifecycle_call(state_root, profile, workspace, "update-apply", memory_gib, timeout=960)
        apply_text = applied.stdout.decode("ascii", "ignore")
        if (applied.returncode != 0 or not any(marker in apply_text for marker in
                                                ("selected guest updates completed", "CURRENT"))):
            return {"status": "BLOCKED" if applied.returncode == 2 else "FAIL",
                    "reason_code": "selected_update_apply_failed", "update_apply_exit_code": applied.returncode}
        after_update = _profile_record(state_root, profile)
        if (after_update.get("profile_id") != profile_identity
                or after_update.get("workspace") != workspace_path
                or _overlay_identity(state_root, profile) != overlay_identity
                or _hash_file(key_path) != key_hash or not _listener_owned_loopback(after_update)):
            return {"status": "FAIL", "reason_code": "selected_update_state_not_retained"}
        after = after_update
        apply_result = "PASS"
    memory_before = _guest_memory_mib(state_root, profile)
    if memory_gib >= 8:
        down = _lifecycle_call(state_root, profile, workspace, "resize", 4, timeout=300)
        if down.returncode != 0:
            return {"status": "BLOCKED" if down.returncode == 2 else "FAIL",
                    "reason_code": "graceful_resize_down_failed", "resize_down_exit_code": down.returncode}
        low_data = _profile_record(state_root, profile)
        memory_low = _guest_memory_mib(state_root, profile)
        up = _lifecycle_call(state_root, profile, workspace, "resize", memory_gib, timeout=300)
        if up.returncode != 0:
            return {"status": "BLOCKED" if up.returncode == 2 else "FAIL",
                    "reason_code": "graceful_resize_restore_failed", "resize_restore_exit_code": up.returncode}
        restored = _profile_record(state_root, profile)
        memory_restored = _guest_memory_mib(state_root, profile)
        low_ok = memory_low is not None and 3 * 1024 <= memory_low <= 4 * 1024 + 256
        restored_ok = memory_restored is not None and (memory_gib * 1024 - 256) <= memory_restored <= memory_gib * 1024 + 256
        if (not low_ok or not restored_ok or low_data.get("status") != "ready"
                or low_data.get("memory_gib") != 4 or low_data.get("profile_id") != profile_identity
                or restored.get("profile_id") != profile_identity
                or restored.get("memory_gib") != memory_gib
                or restored.get("workspace") != workspace_path
                or _overlay_identity(state_root, profile) != overlay_identity
                or _hash_file(key_path) != key_hash or not _listener_owned_loopback(restored)):
            return {"status": "FAIL", "reason_code": "resize_guest_memory_or_identity_invalid"}
        after = restored
    return {
        "status": "PASS",
        "profile_name": profile,
        "profile_identity_retained": True,
        "workspace_retained": True,
        "base_image_retained": True,
        "overlay_and_key_retained": True,
        "suspend_resume": "PASS",
        "update_scan_cancel": "PASS",
        "selected_update_apply": apply_result,
        "opencode_version": after.get("opencode_version"),
        "resize_memory_before_mib": memory_before,
        "resize_restore_verified": memory_gib >= 8,
    }


def _self_test_gate(state_root: Path, parent_profile: str, workspace: Path,
                    evidence_root: Path) -> dict[str, Any]:
    self_test = _load_module("vm_host_acceptance_self_test", ROOT / "scripts/vm-self-test.py")
    run_id = uuid.uuid4().hex
    report_root = self_test._report_root_from_state(str(state_root))
    adapter = self_test.LiveSelfTestAdapter(
        workspace, state_root, report_root, run_id, parent_profile=parent_profile,
    )
    runner = self_test.SelfTestRunner(
        workspace, report_root, adapter, max_depth=2, yes=True, run_id=run_id,
    )
    report, exit_code = runner.run()
    report_path = report_root / f"{run_id}.json"
    if not report_path.is_file() or report_path.is_symlink():
        return {"status": "FAIL", "reason_code": "self_test_report_missing"}

    cleanup_status = "BLOCKED"
    cleanup_reason = None
    try:
        self_test.cleanup_live_self_test(str(state_root), run_id, run_id)
        cleanup_status = "PASS"
        report = _read_private_json(report_path)
    except (OSError, RuntimeError, ValueError, KeyError):
        cleanup_reason = "exact_self_test_cleanup_unconfirmed"
        try:
            report = _read_private_json(report_path)
        except (OSError, UnicodeError, json.JSONDecodeError):
            cleanup_reason = "self_test_report_unreadable"

    archive_path = evidence_root / f"selftest-{run_id}.json"
    if archive_path.exists() or archive_path.is_symlink():
        return {"status": "FAIL", "reason_code": "self_test_evidence_collision"}
    archived_report = json.loads(json.dumps(report))
    diagnostic_refs: list[str] = []
    failure_evidence = report.get("stages", {}).get("expected_boot_failure", {}).get("evidence", {})
    for reference in failure_evidence.get("diagnostic_refs", []):
        if not isinstance(reference, str) or reference != f"{run_id}-failure.json":
            return {"status": "FAIL", "reason_code": "self_test_diagnostic_manifest_invalid"}
        diagnostic_path = report_root / reference
        info = diagnostic_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > 16 * 1024):
            return {"status": "FAIL", "reason_code": "self_test_diagnostic_unsafe"}
        diagnostic = _read_private_json(diagnostic_path, maximum=16 * 1024)
        if diagnostic.get("run_id") != run_id or diagnostic.get("schema_version") != 1:
            return {"status": "FAIL", "reason_code": "self_test_diagnostic_invalid"}
        diagnostic_archive = evidence_root / f"selftest-{run_id}-failure.json"
        _atomic_json(diagnostic_archive, diagnostic)
        diagnostic_refs.append(diagnostic_archive.name)
    if diagnostic_refs:
        archived_report["preserved_diagnostic_refs"] = [
            {"stage": "expected_boot_failure", "path": name} for name in diagnostic_refs
        ]
        archived_report["stages"]["expected_boot_failure"]["evidence"]["diagnostic_refs"] = diagnostic_refs
    _atomic_json(archive_path, archived_report)
    digest = _hash_file(archive_path)
    stages = report.get("stages", {})
    result = report.get("result")
    depth_status = stages.get("depth", {}).get("status")
    passed = exit_code == 0 and result == "PASS" and cleanup_status == "PASS"
    status = "PASS" if passed else "FAIL" if result == "FAIL" else "BLOCKED"
    return {
        "status": status,
        "reason_code": cleanup_reason,
        "run_id": run_id,
        "self_test_result": result,
        "self_test_exit_code": exit_code,
        "depth_gate_status": depth_status,
        "cleanup_status": cleanup_status,
        "report_ref": archive_path.name,
        "report_sha256": digest,
        "stage_statuses": {
            name: item.get("status") for name, item in stages.items()
            if name in self_test.STAGE_ORDER
        },
    }


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    encoded = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    if len(encoded) > MAX_REPORT_BYTES:
        raise AcceptanceError("acceptance report exceeds the bounded size")
    fd, temporary_name = tempfile.mkstemp(prefix=".acceptance-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _new_campaign(state_root: Path, run_id: str, source: Path, suites: list[str],
                  preflight_report: dict[str, Any]) -> tuple[Path, Path, Path]:
    paths = _load_module("vm_host_acceptance_paths", PATHS_PATH)
    created = paths.recursion_run_create(state_root, _run_name(run_id))
    run_root = Path(created["run_root"])
    child_state = Path(created["state_root"])
    _private_directory(run_root)
    _private_directory(child_state)
    report_root = run_root.parent / "reports"
    if report_root.exists() or report_root.is_symlink():
        _private_directory(report_root)
    else:
        report_root.mkdir(mode=0o700)
        os.chmod(report_root, 0o700)
        _private_directory(report_root)
    snapshot = _load_module("vm_host_acceptance_snapshot", SNAPSHOT_PATH)
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "run_name": _run_name(run_id),
        "created_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "source": preflight_report["source"],
        "snapshot": {"tree_sha256": None, "entry_count": None, "total_bytes": None},
        "suites": suites,
        "protected_assets": ["default profiles", "shared state outside this run", "host Docker session", "credentials"],
        "owned_profiles": [],
        "gates": {gate: {"status": "NOT_RUN"} for gate in GATE_IDS},
        "result": "BLOCKED",
    }
    report_path = report_root / f"{_run_name(run_id)}.json"
    _atomic_json(report_path, report)
    prepared = snapshot.prepare(source, run_root, 2 * 1024**3)
    workspace = Path(prepared["workspace"])
    report["snapshot"] = {
        "tree_sha256": prepared["tree_sha256"],
        "entry_count": prepared["entry_count"],
        "total_bytes": prepared["total_bytes"],
    }
    _atomic_replace_json(report_path, report)
    return run_root, child_state, workspace


def _safe_status(result: subprocess.CompletedProcess) -> str:
    if result.returncode == 0:
        return "PASS"
    if result.returncode == 2:
        return "BLOCKED"
    return "FAIL"


def execute_campaign(source: Path, state_root: Path, run_id: str, suites: list[str],
                     memory_gib: int, confirm_run_id: str, *,
                     apply_selected_updates: bool = False) -> tuple[dict[str, Any], Path]:
    if confirm_run_id != run_id:
        raise AcceptanceError("execution requires the exact plan run ID")
    if apply_selected_updates and "lifecycle" not in suites:
        raise AcceptanceError("selected update apply is valid only with the lifecycle suite")
    initial = preflight(source, state_root, run_id, memory_gib, suites)
    if initial["result"] != "PASS":
        return {"run_id": run_id, "result": "BLOCKED", "preflight": initial, "gates": {}}, Path()
    if not any(suite != "preflight" for suite in suites):
        return {"run_id": run_id, "result": "PASS", "preflight": initial,
                "gates": {"HOST-PREFLIGHT": {"status": "PASS", "checks": initial["checks"]}}}, Path()
    run_root, child_state, workspace = _new_campaign(state_root, run_id, source, suites, initial)
    report_path = run_root.parent / "reports" / f"{_run_name(run_id)}.json"
    report = _read_private_json(report_path)
    report["preflight"] = initial
    report["requested_effects"] = {
        "selected_guest_update_targets": ["config", "tools"] if apply_selected_updates else [],
        "selected_guest_updates_authorized": apply_selected_updates,
    }
    report["execution_context"] = initial["execution_context"]
    report["source"]["workspace_tree_sha256"] = _source_identity(workspace)["tree_sha256"]
    selected_gates = {gate for suite in suites for gate in SUITE_GATES[suite]}
    for gate in selected_gates:
        report["gates"][gate] = {"status": "BLOCKED", "reason_code": "not_run"}

    active_gate: str | None = None
    active_gate_started_at: float | None = None

    def begin_gate(gate: str) -> None:
        nonlocal active_gate, active_gate_started_at
        if gate in selected_gates:
            active_gate = gate
            active_gate_started_at = time.monotonic()
            report["active_gate"] = gate
            _atomic_replace_json(report_path, report)

    def set_gate(gate: str, status: str, **evidence: Any) -> None:
        nonlocal active_gate, active_gate_started_at
        if gate not in selected_gates:
            return
        if status not in {"PASS", "FAIL", "BLOCKED", "NOT_RUN"}:
            raise AcceptanceError("gate result is outside the fixed status enum")
        if active_gate == gate and active_gate_started_at is not None:
            evidence.setdefault("elapsed_ms", max(0, int((time.monotonic() - active_gate_started_at) * 1000)))
        if active_gate == gate:
            active_gate = None
            active_gate_started_at = None
            report.pop("active_gate", None)
        report["gates"][gate] = {"status": status, **evidence}
        _atomic_replace_json(report_path, report)

    set_gate("HOST-PREFLIGHT", "PASS", checks=initial["checks"], memory=initial["memory"])
    profile = _profile_name(run_id)
    guest_ready = False

    try:
        profile_needed = any(suite != "preflight" for suite in suites)
        if profile_needed:
            report["owned_profiles"] = [{"profile_name": profile, "depth": 0, "run_id": run_id}]
            _atomic_replace_json(report_path, report)
            begin_gate("HOST-CLEAN")
            completed = _lifecycle_call(child_state, profile, workspace, "up", memory_gib, timeout=1900)
            if completed.returncode == 0:
                data = _profile_record(child_state, profile)
                lifecycle_ready = data.get("status") == "ready" and data.get("runtime_identity") is not None
                clean = None
                clean_error = None
                image_integrity = _cached_image_integrity(child_state, profile)
                if lifecycle_ready:
                    try:
                        clean = _guest_clean_checks(child_state, profile, workspace, os.getuid(), os.getgid())
                    except TimeoutError:
                        clean_error = "guest_clean_probe_timeout"
                    except AcceptanceError:
                        clean_error = "guest_clean_probe_failed"
                clean_status = "PASS" if (
                    lifecycle_ready and clean is not None and clean.get("result") == "PASS"
                    and image_integrity["status"] == "PASS"
                ) else "FAIL" if (
                    (clean is not None and clean.get("result") == "FAIL")
                    or image_integrity["status"] == "FAIL"
                ) else "BLOCKED"
                guest_ready = clean_status == "PASS"
                set_gate("HOST-CLEAN", clean_status,
                         reason_code=clean_error or image_integrity.get("reason_code"),
                         profile_name=profile, readiness_verified=lifecycle_ready,
                         checks=clean.get("checks") if clean else None,
                         host_docker_dependency=clean.get("host_docker_dependency") if clean else "NOT_REQUIRED",
                         image_integrity=image_integrity["status"],
                         image_artifact_sha256=image_integrity.get("artifact_sha256"),
                         image_sha256=data.get("image_sha256"),
                         guest_payload_sha256=data.get("guest_payload_sha256"),
                          runtime_payload_sha256=data.get("runtime_payload_sha256"),
                          opencode_version=data.get("opencode_version"),
                          guest_opencode_version=clean.get("opencode_version") if clean else None)
                if clean_status == "PASS":
                    report["verified_guest_opencode_version"] = clean.get("opencode_version")
                    _atomic_replace_json(report_path, report)
            else:
                set_gate("HOST-CLEAN", _safe_status(completed), reason_code="fresh_guest_start_failed",
                         cause_exit_code=completed.returncode)

        if "daily" in suites:
            if guest_ready:
                begin_gate("E2E-DAILY")
                try:
                    daily = _daily_lifecycle_checks(child_state, profile, workspace, run_root, memory_gib)
                except TimeoutError:
                    daily = {"status": "FAIL", "reason_code": "daily_deadline_exceeded"}
                except (AcceptanceError, OSError, RuntimeError, ValueError):
                    daily = {"status": "BLOCKED", "reason_code": "daily_environment_unavailable"}
                daily_status = daily.pop("status")
                if daily_status == "FAIL":
                    set_gate("E2E-DAILY", "FAIL", **daily)
                elif daily_status == "BLOCKED":
                    set_gate("E2E-DAILY", "BLOCKED", **daily)
                else:
                    set_gate(
                        "E2E-DAILY", "BLOCKED",
                        reason_code=("docker_rollback_smoke_required" if daily.get("docker_rollback_route_staged")
                                     else "rollback_route_not_staged"),
                        automated_status="PASS", **daily,
                    )
            else:
                set_gate("E2E-DAILY", "BLOCKED", reason_code="outer_guest_not_verified_ready")

        if "boundary" in suites:
            if guest_ready:
                begin_gate("SEC-NET")
                outer_boundary = _guest_isolation(child_state, profile, workspace, run_id)
                network_status = outer_boundary.pop("status")
                child_profile = _profile_name(run_id, "agent")
                nested_budget = _read_mem_budget(4 * 1024, DEFAULT_RESERVE_MIB)
                nested_budget_safe = nested_budget["passed"]
                child_status = "BLOCKED"
                child_boundary: dict[str, Any] = {"reason_code": "nested_ram_budget_blocked"}
                if nested_budget_safe:
                    report["owned_profiles"].append({
                        "profile_name": child_profile, "depth": 1, "run_id": run_id,
                    })
                    _atomic_replace_json(report_path, report)
                    child_up = _lifecycle_call(
                        child_state, child_profile, workspace, "up", 4,
                        depth=1, maximum=2, timeout=1900,
                    )
                    if child_up.returncode == 0:
                        child_data = _profile_record(child_state, child_profile)
                        child_isolation = _guest_isolation(child_state, child_profile, workspace, run_id)
                        child_status = child_isolation.pop("status")
                        child_boundary = {
                            "profile_name": child_profile,
                            "listener_verified": _listener_owned_loopback(child_data),
                            "guest_checks": child_isolation.get("guest_checks"),
                            "source_tree_sha256": child_isolation.get("source_tree_sha256"),
                        }
                    else:
                        child_status = _safe_status(child_up)
                        child_boundary = {"reason_code": "nested_profile_start_failed"}
                network_pass = network_status == "PASS" and child_status == "PASS"
                set_gate("SEC-NET", "PASS" if network_pass else
                         "FAIL" if "FAIL" in {network_status, child_status} else "BLOCKED",
                         outer=outer_boundary, child={"status": child_status, **child_boundary},
                         nested_memory_budget_passed=nested_budget_safe)
            else:
                set_gate("SEC-NET", "BLOCKED", reason_code="outer_guest_not_verified_ready")
            set_gate("SEC-FS", "BLOCKED", reason_code="owner_live_evidence_required")
            set_gate("SEC-SECRET", "BLOCKED", reason_code="owner_live_evidence_required")

        if guest_ready and "lifecycle" in suites:
            begin_gate("E2E-LIFE")
            life_workspace = Path(_profile_record(child_state, profile)["workspace"])
            try:
                life = _lifecycle_retention_checks(
                    child_state, profile, life_workspace, memory_gib, apply_selected_updates,
                )
            except TimeoutError:
                life = {"status": "FAIL", "reason_code": "lifecycle_deadline_exceeded"}
            except (AcceptanceError, OSError, RuntimeError, ValueError):
                life = {"status": "BLOCKED", "reason_code": "lifecycle_environment_unavailable"}
            life_status = life.pop("status")
            if life_status == "FAIL":
                set_gate("E2E-LIFE", "FAIL", **life)
            elif life_status == "BLOCKED":
                set_gate("E2E-LIFE", "BLOCKED", **life)
            else:
                set_gate("E2E-LIFE", "BLOCKED", reason_code="full_lifecycle_suite_incomplete",
                         automated_status="PASS", **life)
            lifecycle_version = report["gates"].get("E2E-LIFE", {}).get("opencode_version")
            if isinstance(lifecycle_version, str):
                report["verified_guest_opencode_version"] = lifecycle_version
                _atomic_replace_json(report_path, report)
        elif "lifecycle" in suites:
            set_gate("E2E-LIFE", "BLOCKED", reason_code="outer_guest_not_verified_ready")

        if "self" in suites:
            if guest_ready:
                try:
                    begin_gate("E2E-SELF")
                    self_evidence = _self_test_gate(child_state, profile, workspace, report_path.parent)
                    self_status = self_evidence.pop("status")
                    depth_status = self_evidence.pop("depth_gate_status", "NOT_RUN")
                    set_gate("E2E-SELF", self_status, **self_evidence)
                    set_gate("E2E-DEPTH", depth_status, source="live self-test depth stage")
                except (AcceptanceError, OSError, RuntimeError, TimeoutError, ValueError):
                    set_gate("E2E-SELF", "BLOCKED", reason_code="live_self_test_unavailable")
                    set_gate("E2E-DEPTH", "BLOCKED", reason_code="live_self_test_unavailable")
            else:
                set_gate("E2E-SELF", "BLOCKED", reason_code="outer_guest_not_verified_ready")
                set_gate("E2E-DEPTH", "BLOCKED", reason_code="outer_guest_not_verified_ready")

        # Owner checkpoints are explicit, version-bound attestations. No health
        # check or default/Enter response is promoted into a behavioral PASS.
        for suite, gates in {
            "model": ("E2E-MODEL",), "workload": ("E2E-WORK",),
            "agents": ("E2E-AGENTS",),
            "human": ("HUMAN-TUI", "HUMAN-PERM", "HUMAN-AUTH"),
        }.items():
            if suite in suites:
                for gate in gates:
                    reason = "owner_observation_required" if gate.startswith("HUMAN-") else "owner_live_evidence_required"
                    set_gate(gate, "BLOCKED", reason_code=reason)

        selected = [report["gates"][gate]["status"] for gate in selected_gates]
        report["result"] = "PASS" if selected and all(value == "PASS" for value in selected) else (
            "FAIL" if "FAIL" in selected else "BLOCKED"
        )
        report["finished_utc"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        _atomic_replace_json(report_path, report)
    except KeyboardInterrupt:
        if active_gate is not None:
            elapsed = (max(0, int((time.monotonic() - active_gate_started_at) * 1000))
                       if active_gate_started_at is not None else 0)
            report["gates"][active_gate] = {
                "status": "BLOCKED", "reason_code": "interrupted", "elapsed_ms": elapsed,
            }
            report.pop("active_gate", None)
        report["result"] = "BLOCKED"
        report["interrupted"] = True
        report["finished_utc"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        _atomic_replace_json(report_path, report)
        raise
    except TimeoutError:
        if active_gate is not None:
            elapsed = (max(0, int((time.monotonic() - active_gate_started_at) * 1000))
                       if active_gate_started_at is not None else 0)
            report["gates"][active_gate] = {
                "status": "FAIL", "reason_code": "deadline_exceeded", "elapsed_ms": elapsed,
            }
            report.pop("active_gate", None)
        report["result"] = "FAIL"
        report["reason_code"] = "deadline_exceeded"
        report["finished_utc"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        _atomic_replace_json(report_path, report)
    except (AcceptanceError, OSError, RuntimeError, ValueError):
        if active_gate is not None:
            elapsed = (max(0, int((time.monotonic() - active_gate_started_at) * 1000))
                       if active_gate_started_at is not None else 0)
            report["gates"][active_gate] = {
                "status": "BLOCKED", "reason_code": "environment_unavailable", "elapsed_ms": elapsed,
            }
            report.pop("active_gate", None)
        report["result"] = "BLOCKED"
        report["reason_code"] = "environment_unavailable"
        report["finished_utc"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        _atomic_replace_json(report_path, report)
    return report, report_path


def _atomic_replace_json(path: Path, data: dict[str, Any]) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
        raise AcceptanceError("acceptance report is not a private owned regular file")
    encoded = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    if len(encoded) > MAX_REPORT_BYTES:
        raise AcceptanceError("acceptance report exceeds the bounded size")
    fd, temporary_name = tempfile.mkstemp(prefix=".acceptance-update-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def cleanup_campaign(state_root: Path, run_id: str, confirm_run_id: str) -> dict[str, Any]:
    if run_id != confirm_run_id:
        raise AcceptanceError("cleanup requires the exact campaign run ID")
    run_root = _namespace_path(state_root, _run_name(run_id))
    _private_directory(run_root)
    report_path = run_root.parent / "reports" / f"{_run_name(run_id)}.json"
    _private_directory(report_path.parent)
    report = _read_private_json(report_path)
    if report.get("run_id") != run_id or report.get("run_name") != _run_name(run_id):
        raise AcceptanceError("campaign manifest ownership does not match the requested run")
    suites = report.get("suites")
    gates = report.get("gates")
    if (not isinstance(suites, list) or not suites or any(item not in ALL_SUITES for item in suites)
            or len(set(suites)) != len(suites) or not isinstance(gates, dict)
            or any(not isinstance(item, dict) or item.get("status") not in
                   {"PASS", "FAIL", "BLOCKED", "NOT_RUN"} for item in gates.values())):
        raise AcceptanceError("campaign suite or gate manifest is invalid")
    owned_profiles = report.get("owned_profiles", [])
    if not isinstance(owned_profiles, list):
        raise AcceptanceError("owned profile manifest is invalid")
    checked_profiles: list[dict[str, Any]] = []
    seen_profiles: set[str] = set()
    role_depth = {"outer": 0, "agent": 1, "workload": 1}
    for item in owned_profiles:
        if not isinstance(item, dict) or set(item) != {"profile_name", "depth", "run_id"}:
            raise AcceptanceError("owned profile manifest is invalid")
        name, depth, owner = item["profile_name"], item["depth"], item["run_id"]
        suffix = next((role for role in role_depth if name == _profile_name(run_id, role)), None) \
            if isinstance(name, str) else None
        if (owner != run_id or suffix is None or type(depth) is not int
                or role_depth[suffix] != depth or name in seen_profiles):
            raise AcceptanceError("cleanup target is outside this exact campaign run")
        seen_profiles.add(name)
        checked_profiles.append(item)
    child_state = run_root / "state"
    self_gate = gates.get("E2E-SELF", {})
    self_run_id = self_gate.get("run_id") if isinstance(self_gate, dict) else None
    if self_run_id is not None and ("self" not in suites or not isinstance(self_run_id, str)
                                    or not RUN_ID_RE.fullmatch(self_run_id)):
        raise AcceptanceError("self-test cleanup reference is outside the selected campaign scope")
    if isinstance(self_run_id, str) and RUN_ID_RE.fullmatch(self_run_id):
        self_test = _load_module("vm_host_acceptance_self_test_cleanup", ROOT / "scripts/vm-self-test.py")
        self_test.cleanup_live_self_test(str(child_state), self_run_id, self_run_id)
        self_report_path = child_state / "vm/self-test-reports" / f"{self_run_id}.json"
        self_archive_ref = self_gate.get("report_ref")
        if isinstance(self_archive_ref, str) and re.fullmatch(r"selftest-[a-f0-9]{32}\.json", self_archive_ref):
            self_archive = report_path.parent / self_archive_ref
            if self_archive.exists() and self_report_path.is_file():
                nested_report = _read_private_json(self_report_path)
                _atomic_replace_json(self_archive, nested_report)
        self_gate["cleanup_status"] = "PASS"
        _atomic_replace_json(report_path, report)
    deleted: list[str] = []
    for item in sorted(checked_profiles, key=lambda value: (-value["depth"], value["profile_name"])):
        name = item["profile_name"]
        profile_path = child_state / "vm/profiles" / name
        if not profile_path.exists():
            continue
        data = _profile_record(child_state, name)
        if data.get("status") in {"starting", "provisioning", "unknown", "deleting"}:
            raise AcceptanceError("ambiguous profile state is preserved for owner review")
        if data.get("status") == "ready":
            suspended = _lifecycle_call(
                child_state, name, Path(data["workspace"]), "suspend",
                int(data["memory_gib"]), timeout=90,
            )
            if suspended.returncode != 0:
                raise AcceptanceError("graceful exact-profile shutdown did not confirm; state was preserved")
            data = _profile_record(child_state, name)
        if data.get("status") not in {"allocated", "failed", "suspended"}:
            raise AcceptanceError("profile did not reach an exact deletable stopped state")
        result = _lifecycle_call(child_state, name, Path(data["workspace"]), "delete",
                                 int(data["memory_gib"]), confirm=name, timeout=180)
        if result.returncode != 0:
            raise AcceptanceError("supported lifecycle cleanup did not confirm exact profile deletion")
        deleted.append(name)
    report["cleanup"] = {"status": "PASS", "deleted_profiles": deleted}
    report["finished_utc"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    _atomic_replace_json(report_path, report)
    paths = _load_module("vm_host_acceptance_paths_cleanup", PATHS_PATH)
    paths.recursion_run_delete(state_root, _run_name(run_id), _run_name(run_id), Path("/proc"))
    return {"run_id": run_id, "cleanup": "PASS", "deleted_profiles": deleted,
            "report_path": str(report_path)}


def record_human_observation(state_root: Path, run_id: str, confirm_run_id: str,
                             gate: str, outcome: str, version: str,
                             checklist: list[str]) -> dict[str, Any]:
    """Record a version-bound owner checklist; never infer a result from automation."""
    if run_id != confirm_run_id or RUN_ID_RE.fullmatch(run_id) is None:
        raise AcceptanceError("human observation requires the exact campaign run ID")
    if gate not in OWNER_CHECKLISTS:
        raise AcceptanceError("owner observation gate is not supported")
    if outcome not in {"PASS", "FAIL", "BLOCKED"}:
        raise AcceptanceError("human observation outcome must be PASS, FAIL, or BLOCKED")
    if not isinstance(checklist, list) or not checklist or len(checklist) != len(set(checklist)):
        raise AcceptanceError("human observation requires unique fixed checklist IDs")
    required = OWNER_CHECKLISTS[gate]
    if any(item not in required for item in checklist):
        raise AcceptanceError("human observation contains an unknown checklist ID")
    if outcome == "PASS" and set(checklist) != set(required):
        raise AcceptanceError("a human PASS requires every gate-specific checklist item")
    if not re.fullmatch(r"2\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", version):
        raise AcceptanceError("observed OpenCode version is invalid")
    _private_directory(state_root)
    run_root = _namespace_path(state_root, _run_name(run_id))
    report_path = run_root.parent / "reports" / f"{_run_name(run_id)}.json"
    _private_directory(report_path.parent)
    report = _read_private_json(report_path)
    if (report.get("schema_version") != 1 or report.get("run_id") != run_id
            or report.get("run_name") != _run_name(run_id)):
        raise AcceptanceError("campaign report does not belong to this exact run")
    selected_suites = report.get("suites")
    report_gates = report.get("gates")
    if (not isinstance(selected_suites, list) or any(item not in ALL_SUITES for item in selected_suites)
            or not isinstance(report_gates, dict)):
        raise AcceptanceError("campaign suite or gate manifest is invalid")
    selected_gates = {name for suite in selected_suites for name in SUITE_GATES[suite]}
    if gate not in selected_gates:
        raise AcceptanceError("this gate was not selected for the exact campaign")
    clean_gate = report_gates.get("HOST-CLEAN", {})
    verified_version = report.get("verified_guest_opencode_version")
    if (not isinstance(clean_gate, dict) or clean_gate.get("status") != "PASS"
            or not isinstance(verified_version, str) or verified_version != version):
        raise AcceptanceError("owner observation version does not match the verified fresh guest")
    existing = report_gates.get(gate)
    if not isinstance(existing, dict) or existing.get("status") not in {"BLOCKED", "NOT_RUN"}:
        raise AcceptanceError("gate is not awaiting an owner observation")
    allowed_reasons = {
        "not_run", "owner_observation_required", "owner_live_evidence_required",
        "docker_rollback_smoke_required",
        "full_lifecycle_suite_incomplete",
    }
    if existing.get("reason_code") not in allowed_reasons:
        raise AcceptanceError("automated failure or prerequisite block cannot be overridden by an owner attestation")
    prior = dict(existing)
    prior_status = prior.pop("status")
    prior_reason = prior.pop("reason_code", None)
    if prior_status == "BLOCKED":
        prior["automated_status"] = "BLOCKED"
    if prior_reason is not None:
        prior["automated_reason_code"] = prior_reason
    report_gates[gate] = {
        **prior,
        "status": outcome,
        "observation": "OWNER_RECORDED",
        "recorded_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "opencode_version": version,
        "checklist_ids": list(checklist),
    }
    statuses = [report_gates.get(name, {}).get("status", "NOT_RUN") for name in selected_gates]
    report["result"] = "PASS" if statuses and all(value == "PASS" for value in statuses) else (
        "FAIL" if "FAIL" in statuses else "BLOCKED"
    )
    report["last_observation_utc"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    _atomic_replace_json(report_path, report)
    return {"run_id": run_id, "gate": gate, "status": outcome,
            "campaign_result": report["result"], "report_path": str(report_path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", help="canonical checkout to snapshot and test")
    parser.add_argument("--state-root", help="existing private 0700 state directory")
    parser.add_argument("--suite", action="append", choices=(*ALL_SUITES, "all"),
                        help="select one or more independent acceptance suites")
    parser.add_argument("--run-id", help="32 lowercase hexadecimal ID; plan output supplies one if omitted")
    parser.add_argument("--memory-gib", type=int, default=8, help="requested outer guest memory (default: 8 GiB)")
    parser.add_argument("--apply-selected-updates", action="store_true",
                        help="plan and explicitly apply only the lifecycle suite's config/tools update targets")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--execute", action="store_true", help="perform the explicitly selected live campaign")
    action.add_argument("--cleanup", action="store_true", help="clean only assets in this run's ownership manifest")
    action.add_argument("--record-observation", action="store_true",
                        help="record one explicit owner observation against a fixed gate checklist")
    parser.add_argument("--confirm-run-id", help="repeat the exact planned ID to authorize execution/cleanup")
    parser.add_argument("--observation-gate", choices=tuple(OWNER_CHECKLISTS))
    parser.add_argument("--observation-result", choices=("PASS", "FAIL", "BLOCKED"))
    parser.add_argument("--observed-version", help="OpenCode version actually observed by the owner")
    parser.add_argument("--observation-check", action="append", default=[],
                        help="completed fixed checklist ID (repeat for each observed item)")
    args = parser.parse_args(argv)

    try:
        if not args.state_root:
            parser.error("--state-root is required")
        state_root = _canonical_directory(args.state_root)
        run_id = _run_id(args.run_id)
        if args.cleanup:
            result = cleanup_campaign(state_root, run_id, args.confirm_run_id or "")
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.record_observation:
            if not args.observation_gate or not args.observation_result or not args.observed_version \
                    or not args.observation_check:
                parser.error("--record-observation requires a gate, result, version and one or more --observation-check IDs")
            result = record_human_observation(
                state_root, run_id, args.confirm_run_id or "", args.observation_gate,
                args.observation_result, args.observed_version, args.observation_check,
            )
            print(json.dumps(result, sort_keys=True))
            return 0 if result["campaign_result"] == "PASS" else 1 if result["campaign_result"] == "FAIL" else 2
        if not args.source:
            parser.error("--source is required for a plan or campaign")
        if not args.suite:
            parser.error("at least one --suite is required for a plan or campaign")
        source = _canonical_directory(args.source)
        suites = list(ALL_SUITES) if "all" in args.suite else list(dict.fromkeys(args.suite))
        if args.apply_selected_updates and "lifecycle" not in suites:
            parser.error("--apply-selected-updates requires --suite lifecycle")
        plan = preflight(source, state_root, run_id, args.memory_gib, suites)
        plan["suites"] = suites
        plan["requested_effects"] = {
            "selected_guest_update_targets": ["config", "tools"] if args.apply_selected_updates else [],
            "selected_guest_updates_authorized": args.apply_selected_updates,
        }
        plan["assets"] = {
            "run_name": _run_name(run_id),
            "profiles": [_profile_name(run_id)] if any(s != "preflight" for s in suites) else [],
            "snapshot": "candidate-snapshot/workspace",
            "private_state": "state/",
            "report": f"reports/{_run_name(run_id)}.json",
        }
        plan["protected"] = ["existing/default profiles", "shared VM bases", "host Docker/auth session", "credentials"]
        if not args.execute:
            print(json.dumps(plan, sort_keys=True))
            return 0 if plan["result"] == "PASS" else 2
        report, report_path = execute_campaign(
            source, state_root, run_id, suites, args.memory_gib, args.confirm_run_id or "",
            apply_selected_updates=args.apply_selected_updates,
        )
        summary = {"run_id": run_id, "result": report["result"],
                   "report_path": str(report_path) if report_path != Path() else None,
                   "gates": {name: item.get("status") for name, item in report.get("gates", {}).items()
                             if name in {gate for suite in suites for gate in SUITE_GATES[suite]}}}
        print(json.dumps(summary, sort_keys=True))
        return 0 if report["result"] == "PASS" else 1 if report["result"] == "FAIL" else 2
    except KeyboardInterrupt:
        print("BLOCKED: campaign interrupted; run-owned report and uncertain assets are preserved.", file=sys.stderr)
        return 130
    except (AcceptanceError, OSError, RuntimeError, TimeoutError, ValueError, KeyError) as error:
        print(f"BLOCKED: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
