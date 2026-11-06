#!/usr/bin/env python3
"""Bounded, evidence-gated driver for changed-VM self-development tests.

The default command is fixture-independent and fails closed. ``--live`` selects
the explicit nested-VM acceptance adapter, which uses the frozen lifecycle and
recursion APIs and requires a run-ID confirmation plus verified parent metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_PATH = ROOT / "scripts/vm-recursion-workspace.py"
PATHS_PATH = ROOT / "lib/vm/paths.py"
FIXTURE_BOOTSTRAP = Path("lib/vm/guest-bootstrap.sh")
FIXTURE_LIFECYCLE = Path("lib/vm/lifecycle.sh")
FIXTURE_RUNNER = Path("lib/vm/runner.sh")
FIXTURE_RUNNER_TEST = Path("tests/vm-process.bats")
FIXTURE_SENTINEL = "selftest-candidate"
FIXTURE_BEGIN = "# VM_SELF_TEST_FIXTURE_PREFIX_BEGIN\n"
FIXTURE_END = "# VM_SELF_TEST_FIXTURE_PREFIX_END\n"
MAX_SNAPSHOT_BYTES = 2 * 1024**3
MAX_REPORT_BYTES = 2 * 1024**2
MAX_FIXTURE_FILE_BYTES = 64 * 1024**2
TOTAL_DEADLINE_SECONDS = 60 * 60
BOOT_DEADLINE_SECONDS = 30 * 60
NORMAL_STAGE_DEADLINE_SECONDS = 120
CLEANUP_DEADLINE_SECONDS = 60
SUPPORTED_MAX_DEPTH = 2

STATUSES = {"PASS", "FAIL", "BLOCKED", "NOT_RUN"}
STAGE_ORDER = (
    "preflight",
    "broken_snapshot",
    "expected_boot_failure",
    "fixed_snapshot",
    "fixed_boot",
    "source_parity",
    "candidate_tests",
    "workload",
    "boundary",
    "depth",
    "agent_task",
    "parent_continuity",
)
BOOT_STAGES = {"expected_boot_failure", "fixed_boot"}
REASON_CODES = {
    "backend_unavailable", "environment_unavailable", "invalid_evidence",
    "stage_failed", "deadline_exceeded", "interrupted", "terminated", "report_write_failed",
}
EVIDENCE_FIELDS = {
    "preflight": {"parent_identity_verified", "current_depth", "max_depth", "kvm_usable",
                  "cpu_virtualization", "ram_budget_passed", "disk_budget_passed",
                  "parent_source_tree_sha256", "parent_image_sha256",
                  "parent_runtime_payload_sha256", "parent_opencode_version"},
    "broken_snapshot": {"profile_name", "source_head", "base_source_tree_sha256",
                           "candidate_tree_sha256", "candidate_payload_sha256", "manifest_sha256",
                           "bootstrap_sha256", "runner_sha256", "runner_test_sha256",
                           "base_lifecycle_sha256", "candidate_lifecycle_sha256"},
    "expected_boot_failure": {"profile_name", "source_head", "candidate_tree_sha256", "manifest_sha256",
                                "seed_sha256", "bootstrap_sha256", "runner_sha256", "runner_test_sha256",
                                "candidate_lifecycle_sha256",
                               "depth", "failure_stage", "observed_exit_code",
                              "sentinel", "qemu_vcpus", "effective_qemu_argv_sha256",
                              "failure_evidence_preserved", "diagnostic_refs"},
    "fixed_snapshot": {"profile_name", "source_head", "base_source_tree_sha256",
                         "candidate_tree_sha256", "candidate_payload_sha256", "manifest_sha256",
                         "bootstrap_sha256", "runner_sha256", "runner_test_sha256",
                         "base_lifecycle_sha256", "candidate_lifecycle_sha256"},
    "fixed_boot": {"profile_name", "ready", "depth", "max_depth", "sentinel", "qemu_vcpus",
                   "guest_vcpus", "seed_sha256", "image_sha256", "opencode_version",
                   "manifest_sha256", "bootstrap_sha256", "runner_sha256", "runner_test_sha256",
                    "guest_payload_sha256", "guest_source_tree_sha256",
                    "candidate_lifecycle_sha256", "installed_lifecycle_sha256"},
    "source_parity": {"source_head", "guest_source_tree_sha256", "test_source_tree_sha256",
                       "expected_payload_sha256", "installed_payload_sha256", "guest_lifecycle_sha256"},
    "candidate_tests": {"source_tree_sha256", "exit_code", "tests_passed", "tests_failed"},
    "workload": {"harmless_workload_observed", "workload_exit_code"},
    "boundary": {"restricted_network_observed", "direct_egress_blocked", "ancestor_capabilities_absent"},
    "depth": {"g1_profile_name", "g2_profile_name", "g1_depth", "g2_depth", "max_depth",
              "g3_refused", "refused_before_state", "refused_before_qemu", "parent_chain_verified",
              "reset_refused", "increase_refused", "missing_kvm_refused",
              "candidate_lifecycle_sha256", "g2_runtime_payload_sha256",
              "g2_installed_lifecycle_sha256", "lifecycle_mutation_observed",
              "g2_boundary_verified", "g2_boundary_report_sha256"},
    "agent_task": {"agent_completed", "task_id", "result_sha256"},
    "parent_continuity": {"parent_session_alive", "source_mount_readable", "parent_workload_alive"},
}


class SelfTestError(ValueError):
    """Unsafe input, invalid evidence, or an incomplete self-test contract."""


class SelfTestTermination(Exception):
    """A handled termination signal with its CLI-compatible exit status."""

    def __init__(self, exit_code: int):
        super().__init__("self-test interrupted")
        self.exit_code = exit_code


class SelfTestAdapter(Protocol):
    """Trusted stage adapter; production implementations must use frozen APIs."""

    fixture_only: bool

    def run_stage(self, name: str, context: dict[str, Any]) -> dict[str, Any]: ...


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SelfTestError("self-test dependency unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _snapshot_module():
    return _load_module("vm_self_test_snapshot", SNAPSHOT_PATH)


def _paths_module():
    return _load_module("vm_self_test_paths", PATHS_PATH)


def _hash(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _head(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", value) is not None


def _safe_diagnostic_ref(value: object) -> bool:
    if not isinstance(value, str) or not value or value.startswith("/") or len(value) > 512:
        return False
    path = Path(value)
    return (
        not path.is_absolute() and str(path) == value and ".." not in path.parts
        and all(part not in {"", "."} for part in path.parts)
        and not any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)
    )


def _opencode_version(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) <= 128
        and re.fullmatch(r"2\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", value) is not None
    )


def _safe_profile(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"selftest-[a-f0-9]{12}-(?:bad1|fix1|fix2)", value) is not None


def _canonical_directory(value: str | os.PathLike[str], *, must_exist: bool = True) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise SelfTestError("path must be absolute and canonical")
    try:
        resolved = path.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise SelfTestError("path is unavailable") from exc
    if resolved != path or (must_exist and (not path.is_dir() or path.is_symlink())):
        raise SelfTestError("path must be a canonical directory")
    return path


def _private_directory(path: Path, *, mode: int = 0o700) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != mode or path.resolve(strict=True) != path):
        raise SelfTestError("private directory policy failed")


def _source_manifest(source: Path) -> dict[str, Any]:
    manifest, _fingerprints = _snapshot_module().inventory(source, MAX_SNAPSHOT_BYTES)
    if not _head(manifest.get("source_head")) or not _hash(manifest.get("tree_sha256")):
        raise SelfTestError("revision-bound source manifest is invalid")
    return manifest


def _manifest_digest(manifest: dict[str, Any]) -> str:
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _expected_profile(run_id: str, suffix: str) -> str:
    return f"selftest-{run_id[:12]}-{suffix}"


def _stage_evidence_valid(name: str, evidence: object, context: dict[str, Any]) -> bool:
    if not isinstance(evidence, dict) or not evidence.keys() <= EVIDENCE_FIELDS.get(name, set()):
        return False
    run_id = context["run_id"]
    source = context["source"]
    fixed = context.get("fixed_snapshot", {})
    broken = context.get("broken_snapshot", {})
    expected_profile = {
        "broken_snapshot": _expected_profile(run_id, "bad1"),
        "expected_boot_failure": _expected_profile(run_id, "bad1"),
        "fixed_snapshot": _expected_profile(run_id, "fix1"),
        "fixed_boot": _expected_profile(run_id, "fix1"),
    }
    if name == "preflight":
        return (
            evidence.get("parent_identity_verified") is True
            and type(evidence.get("current_depth")) is int and evidence["current_depth"] == 0
            and type(evidence.get("max_depth")) is int and evidence["max_depth"] == SUPPORTED_MAX_DEPTH
            and evidence.get("kvm_usable") is True
            and evidence.get("cpu_virtualization") is True
            and evidence.get("ram_budget_passed") is True
            and evidence.get("disk_budget_passed") is True
            and _hash(evidence.get("parent_source_tree_sha256"))
            and _hash(evidence.get("parent_image_sha256"))
            and _hash(evidence.get("parent_runtime_payload_sha256"))
            and _opencode_version(evidence.get("parent_opencode_version"))
        )
    if name == "broken_snapshot":
        return (
            evidence.get("profile_name") == expected_profile[name]
            and evidence.get("source_head") == source["source_head"]
            and evidence.get("base_source_tree_sha256") == source["tree_sha256"]
            and _hash(evidence.get("candidate_tree_sha256"))
            and evidence["candidate_tree_sha256"] != source["tree_sha256"]
            and _hash(evidence.get("candidate_payload_sha256"))
            and _hash(evidence.get("manifest_sha256"))
            and _hash(evidence.get("bootstrap_sha256"))
            and _hash(evidence.get("runner_sha256"))
            and _hash(evidence.get("runner_test_sha256"))
            and _hash(evidence.get("base_lifecycle_sha256"))
            and _hash(evidence.get("candidate_lifecycle_sha256"))
            and evidence["candidate_lifecycle_sha256"] != evidence["base_lifecycle_sha256"]
        )
    if name == "expected_boot_failure":
        return (
            evidence.get("profile_name") == expected_profile[name]
            and evidence.get("source_head") == source["source_head"]
            and evidence.get("candidate_tree_sha256") == broken.get("candidate_tree_sha256")
            and evidence.get("manifest_sha256") == broken.get("manifest_sha256")
            and evidence.get("bootstrap_sha256") == broken.get("bootstrap_sha256")
            and evidence.get("runner_sha256") == broken.get("runner_sha256")
            and evidence.get("runner_test_sha256") == broken.get("runner_test_sha256")
            and evidence.get("candidate_lifecycle_sha256") == broken.get("candidate_lifecycle_sha256")
            and type(evidence.get("depth")) is int and evidence["depth"] == 1
            and evidence.get("failure_stage") == "guest_provisioning"
            and type(evidence.get("observed_exit_code")) is int and evidence["observed_exit_code"] > 0
            and _hash(evidence.get("seed_sha256"))
            and evidence.get("sentinel") == "A"
            and type(evidence.get("qemu_vcpus")) is int and evidence["qemu_vcpus"] == 1
            and _hash(evidence.get("effective_qemu_argv_sha256"))
            and evidence.get("failure_evidence_preserved") is True
            and isinstance(evidence.get("diagnostic_refs"), list)
            and 1 <= len(evidence["diagnostic_refs"]) <= 32
            and all(_safe_diagnostic_ref(item) for item in evidence["diagnostic_refs"])
        )
    if name == "fixed_snapshot":
        return (
            evidence.get("profile_name") == expected_profile[name]
            and evidence.get("source_head") == source["source_head"]
            and evidence.get("base_source_tree_sha256") == source["tree_sha256"]
            and _hash(evidence.get("candidate_tree_sha256"))
            and evidence["candidate_tree_sha256"] != broken.get("candidate_tree_sha256")
            and evidence["candidate_tree_sha256"] != source["tree_sha256"]
            and _hash(evidence.get("candidate_payload_sha256"))
            and evidence["candidate_payload_sha256"] != broken.get("candidate_payload_sha256")
            and _hash(evidence.get("manifest_sha256"))
            and evidence["manifest_sha256"] != broken.get("manifest_sha256")
            and _hash(evidence.get("bootstrap_sha256"))
            and evidence.get("bootstrap_sha256") != broken.get("bootstrap_sha256")
            and _hash(evidence.get("runner_sha256"))
            and evidence.get("runner_sha256") == broken.get("runner_sha256")
            and _hash(evidence.get("runner_test_sha256"))
            and evidence.get("runner_test_sha256") == broken.get("runner_test_sha256")
            and evidence.get("base_lifecycle_sha256") == broken.get("base_lifecycle_sha256")
            and evidence.get("candidate_lifecycle_sha256") == broken.get("candidate_lifecycle_sha256")
        )
    if name == "fixed_boot":
        return (
            evidence.get("profile_name") == expected_profile[name]
            and evidence.get("ready") is True
            and type(evidence.get("depth")) is int and evidence["depth"] == 1
            and type(evidence.get("max_depth")) is int and evidence["max_depth"] == SUPPORTED_MAX_DEPTH
            and evidence.get("sentinel") == "B"
            and type(evidence.get("qemu_vcpus")) is int and evidence["qemu_vcpus"] == 1
            and type(evidence.get("guest_vcpus")) is int and evidence["guest_vcpus"] == 1
            and _hash(evidence.get("seed_sha256"))
            and evidence.get("manifest_sha256") == fixed.get("manifest_sha256")
            and evidence.get("bootstrap_sha256") == fixed.get("bootstrap_sha256")
            and evidence.get("runner_sha256") == fixed.get("runner_sha256")
            and evidence.get("runner_test_sha256") == fixed.get("runner_test_sha256")
            and _hash(evidence.get("image_sha256"))
            and _opencode_version(evidence.get("opencode_version"))
            and evidence.get("guest_payload_sha256") == fixed.get("candidate_payload_sha256")
            and evidence.get("guest_source_tree_sha256") == fixed.get("candidate_tree_sha256")
            and evidence.get("candidate_lifecycle_sha256") == fixed.get("candidate_lifecycle_sha256")
            and evidence.get("installed_lifecycle_sha256") == evidence.get("candidate_lifecycle_sha256")
        )
    if name == "source_parity":
        return (
            evidence.get("source_head") == source["source_head"]
            and evidence.get("guest_source_tree_sha256") == fixed.get("candidate_tree_sha256")
            and evidence.get("test_source_tree_sha256") == fixed.get("candidate_tree_sha256")
            and _hash(evidence.get("expected_payload_sha256"))
            and evidence.get("expected_payload_sha256") == context.get("fixed_boot", {}).get("guest_payload_sha256")
            and evidence.get("installed_payload_sha256") == evidence.get("expected_payload_sha256")
            and evidence.get("guest_lifecycle_sha256") == fixed.get("candidate_lifecycle_sha256")
        )
    if name == "candidate_tests":
        return (
            evidence.get("source_tree_sha256") == fixed.get("candidate_tree_sha256")
            and type(evidence.get("exit_code")) is int and evidence["exit_code"] == 0
            and type(evidence.get("tests_passed")) is int and evidence["tests_passed"] > 0
            and type(evidence.get("tests_failed")) is int and evidence["tests_failed"] == 0
        )
    if name == "workload":
        return (evidence.get("harmless_workload_observed") is True
                and type(evidence.get("workload_exit_code")) is int and evidence["workload_exit_code"] == 0)
    if name == "boundary":
        return (
            evidence.get("restricted_network_observed") is True
            and evidence.get("direct_egress_blocked") is True
            and evidence.get("ancestor_capabilities_absent") is True
        )
    if name == "depth":
        return (
            evidence.get("g1_profile_name") == _expected_profile(run_id, "fix1")
            and evidence.get("g2_profile_name") == _expected_profile(run_id, "fix2")
            and type(evidence.get("g1_depth")) is int and evidence["g1_depth"] == 1
            and type(evidence.get("g2_depth")) is int and evidence["g2_depth"] == 2
            and type(evidence.get("max_depth")) is int and evidence["max_depth"] == SUPPORTED_MAX_DEPTH
            and evidence.get("g3_refused") is True
            and evidence.get("refused_before_state") is True
            and evidence.get("refused_before_qemu") is True
            and evidence.get("parent_chain_verified") is True
            and evidence.get("reset_refused") is True
            and evidence.get("increase_refused") is True
            and evidence.get("missing_kvm_refused") is True
            and _hash(evidence.get("candidate_lifecycle_sha256"))
            and evidence.get("candidate_lifecycle_sha256") == fixed.get("candidate_lifecycle_sha256")
            and _hash(evidence.get("g2_runtime_payload_sha256"))
            and evidence.get("g2_installed_lifecycle_sha256") == evidence.get("candidate_lifecycle_sha256")
            and evidence.get("lifecycle_mutation_observed") is True
            and type(evidence.get("g2_boundary_verified")) is bool
            and _hash(evidence.get("g2_boundary_report_sha256"))
        )
    if name == "agent_task":
        return (
            evidence.get("agent_completed") is True
            and evidence.get("task_id") == "harmless-file-test"
            and _hash(evidence.get("result_sha256"))
        )
    if name == "parent_continuity":
        return (
            evidence.get("parent_session_alive") is True
            and evidence.get("source_mount_readable") is True
            and evidence.get("parent_workload_alive") is True
        )
    return False


class UnavailableLiveAdapter:
    """Fail-closed adapter used unless the explicit ``--live`` path is selected."""

    fixture_only = False
    execution_enabled = False

    def __init__(self, reason_code: str = "backend_unavailable"):
        self.reason_code = reason_code

    def run_stage(self, name: str, context: dict[str, Any]) -> dict[str, Any]:
        del name, context
        return {"status": "BLOCKED", "reason_code": self.reason_code}


class LiveSelfTestBlocked(RuntimeError):
    """A live self-test prerequisite is unavailable before a safe action."""


LIFECYCLE_API = r'''set -euo pipefail
repo=$1 state_root=$2 profile=$3 workspace=$4 operation=$5 depth=$6 maximum=$7 memory=$8 confirm=$9
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
  suspend)
    if [[ ! -d "$state_root/vm/profiles/$profile" ]]; then exit 0; fi
    status="$(vm_lifecycle_profile_read "$state_root" "$profile" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
    case "$status" in
      ready) vm_lifecycle_suspend "$state_root" "$profile" 0 60 "$state_root/vm/profiles/$profile/runtime/lab-vm" ;;
      suspended|failed|allocated) exit 0 ;;
      *) exit 2 ;;
    esac
    ;;
  delete)
    [[ "$confirm" == "$profile" ]] || exit 2
    if [[ ! -d "$state_root/vm/profiles/$profile" ]]; then exit 0; fi
    vm_lifecycle_delete "$state_root" "$profile" 0 1 0 60
    ;;
  status)
    vm_lifecycle_status "$state_root" "$profile"
    ;;
  *) exit 2 ;;
esac
'''


SSH_API = r'''set -euo pipefail
repo=$1 state_root=$2 profile=$3 remote_command=$4 deadline=$5
source "$repo/lib/vm/config.sh"
source "$repo/lib/vm/disk.sh"
source "$repo/lib/vm/lifecycle.sh"
source "$repo/lib/vm/ssh.sh"
data="$(vm_lifecycle_profile_read "$state_root" "$profile")"
info="$(vm_lifecycle_registry_for_profile "$state_root" "$profile" "$data")"
registry="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["registry"])' <<<"$info")"
run_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])' <<<"$info")"
role="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["role"])' <<<"$info")"
port="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["runtime_identity"]["ssh_port"])' <<<"$data")"
key="$state_root/vm/profiles/$profile/control-key"
known_hosts="$(dirname "$registry")/known_hosts"
vm_ssh_listener_verified "$registry" "$run_id" "$role" "$port"
vm_ssh_noninteractive "$key" "$known_hosts" "$port" "$deadline" \
  mester@127.0.0.1 "$remote_command"
'''


def _sha256_file(path: Path) -> str:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FIXTURE_FILE_BYTES:
            raise SelfTestError("evidence file is unsafe")
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def _live_bootstrap_prefix(run_id: str, sentinel: str, *, fail: bool) -> bytes:
    if not re.fullmatch(r"[a-f0-9]{32}", run_id) or sentinel not in {"A", "B"}:
        raise SelfTestError("live fixture identity is invalid")
    marker = f"/workspace/.git/lm-selftest-{run_id[:12]}-{sentinel}"
    failure = "exit 42\n" if fail else ""
    return (
        "# VM_SELF_TEST_LIVE_PREFIX_BEGIN\n"
        f"mkdir -p -- /workspace/.git\nprintf '%s\\n' '{sentinel}' > '{marker}'\n"
        + failure
        + "# VM_SELF_TEST_LIVE_PREFIX_END\n"
    ).encode("ascii")


def _insert_live_lifecycle_probe(workspace: Path, run_id: str) -> None:
    """Change only the candidate lifecycle's successful allocation path for proof."""
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise SelfTestError("live lifecycle probe identity is invalid")
    path = workspace / FIXTURE_LIFECYCLE
    payload = _read_regular(path)
    anchor = (
        b'  vm_lifecycle_profile_allocate "$root" "$name" "$depth" "$memory" '
        b'<<<"$data" >/dev/null || return $?\n'
    )
    marker_name = f".vm-self-test-lifecycle-{run_id}"
    if payload.count(anchor) != 1 or marker_name.encode("ascii") in payload:
        raise SelfTestError("candidate lifecycle allocation point is missing or ambiguous")
    insertion = (
        f'  if [[ "${{VM_SELF_TEST_LIFECYCLE_RUN_ID:-}}" == "{run_id}" ]]; then\n'
        f'    local marker="$root/{marker_name}"\n'
        "    [[ ! -e \"$marker\" && ! -L \"$marker\" ]] || { vm_lifecycle_error 'self-test marker already exists'; return 2; }\n"
        f"    (umask 077; printf '%s\\n' '{run_id}' > \"$marker\") || return 2\n"
        "  fi\n"
    ).encode("ascii")
    _write_preserving_mode(path, payload.replace(anchor, anchor + insertion, 1))


def create_live_failure_fixture_copy(workspace: str | os.PathLike[str], destination: str | os.PathLike[str],
                                     run_id: str) -> dict[str, Any]:
    """Create an isolated candidate that writes a run-owned A marker then exits 42."""
    source = _canonical_directory(workspace)
    provenance = _load_snapshot_provenance(source)
    target = Path(destination)
    if not target.is_absolute() or ".." in target.parts or target.resolve(strict=False) != target:
        raise SelfTestError("live fixture destination must be canonical and absolute")
    run_root = target.parent / f".{target.name}-snapshot-run"
    if run_root.exists() or run_root.is_symlink():
        raise SelfTestError("live fixture snapshot run directory must be new")
    _copy_workspace(source, target)
    run_root_created = False
    try:
        path = target / FIXTURE_BOOTSTRAP
        payload = _read_regular(path)
        first_line, separator, body = payload.partition(b"\n")
        if not separator or not first_line.startswith(b"#!"):
            raise SelfTestError("live bootstrap fixture insertion point is invalid")
        _write_preserving_mode(path, first_line + b"\n" + _live_bootstrap_prefix(run_id, "A", fail=True) + body)
        _set_one_vcpu(target)
        _insert_live_lifecycle_probe(target, run_id)
        run_root.mkdir(mode=0o700)
        os.chmod(run_root, 0o700)
        run_root_created = True
        fresh = _snapshot_fixture_copy(target, run_root)
        if _load_snapshot_provenance(source) != provenance:
            raise SelfTestError("verified source snapshot changed during live fixture creation")
        return {
            **provenance, **fresh,
            "bootstrap_sha256": hashlib.sha256(_read_regular(Path(fresh["workspace"]) / FIXTURE_BOOTSTRAP)).hexdigest(),
            "runner_sha256": hashlib.sha256(_read_regular(Path(fresh["workspace"]) / FIXTURE_RUNNER)).hexdigest(),
            "runner_test_sha256": hashlib.sha256(_read_regular(Path(fresh["workspace"]) / FIXTURE_RUNNER_TEST)).hexdigest(),
        }
    except BaseException:
        if target.exists() and not target.is_symlink():
            shutil.rmtree(target)
        if run_root_created and run_root.exists() and not run_root.is_symlink():
            shutil.rmtree(run_root)
        raise


def create_live_fixed_fixture_copy(failed_workspace: str | os.PathLike[str], destination: str | os.PathLike[str],
                                  failure_provenance: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Make a fresh B candidate from A, preserving the selected checkout."""
    source = _canonical_directory(failed_workspace)
    target = Path(destination)
    if not target.is_absolute() or ".." in target.parts or target.resolve(strict=False) != target:
        raise SelfTestError("live fixed fixture destination must be canonical and absolute")
    if _mutated_tree_digest(source) != failure_provenance.get("candidate_tree_sha256"):
        raise SelfTestError("failed live candidate changed before the fix copy")
    run_root = target.parent / f".{target.name}-snapshot-run"
    if run_root.exists() or run_root.is_symlink():
        raise SelfTestError("live fixed fixture snapshot run directory must be new")
    _copy_workspace(source, target)
    run_root_created = False
    try:
        path = target / FIXTURE_BOOTSTRAP
        payload = _read_regular(path)
        begin = b"# VM_SELF_TEST_LIVE_PREFIX_BEGIN\n"
        end = b"# VM_SELF_TEST_LIVE_PREFIX_END\n"
        if payload.count(begin) != 1 or payload.count(end) != 1:
            raise SelfTestError("failed live fixture prefix is missing or ambiguous")
        first = payload.index(begin)
        final = payload.index(end, first) + len(end)
        if b"exit 42\n" not in payload[first:final] or b"'A'" not in payload[first:final]:
            raise SelfTestError("failed live fixture does not contain the expected A/42 injection")
        _write_preserving_mode(path, payload[:first] + _live_bootstrap_prefix(run_id, "B", fail=False) + payload[final:])
        if _mutated_tree_digest(target) == failure_provenance.get("candidate_tree_sha256"):
            raise SelfTestError("live fix did not create a fresh candidate tree")
        run_root.mkdir(mode=0o700)
        os.chmod(run_root, 0o700)
        run_root_created = True
        fresh = _snapshot_fixture_copy(target, run_root)
        return {
            "source_head": failure_provenance["source_head"],
            "base_source_tree_sha256": failure_provenance["base_source_tree_sha256"],
            **fresh,
            "bootstrap_sha256": hashlib.sha256(_read_regular(Path(fresh["workspace"]) / FIXTURE_BOOTSTRAP)).hexdigest(),
            "runner_sha256": hashlib.sha256(_read_regular(Path(fresh["workspace"]) / FIXTURE_RUNNER)).hexdigest(),
            "runner_test_sha256": hashlib.sha256(_read_regular(Path(fresh["workspace"]) / FIXTURE_RUNNER_TEST)).hexdigest(),
        }
    except BaseException:
        if target.exists() and not target.is_symlink():
            shutil.rmtree(target)
        if run_root_created and run_root.exists() and not run_root.is_symlink():
            shutil.rmtree(run_root)
        raise


def _guest_payload_hash(workspace: Path, *, depth: int) -> str:
    result = subprocess.run(
        ["bash", "-c", (
            'source "$1/lib/vm/guest.sh"; '
            'vm_guest_payload_hash "$1" docker opencode latest "$2" 2 none'
        ), "vm-self-test-payload", str(workspace), str(depth)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        check=False, timeout=30,
    )
    value = result.stdout.decode("ascii", "ignore").strip()
    if result.returncode != 0 or not _hash(value):
        raise LiveSelfTestBlocked("candidate guest payload cannot be calculated")
    return value


class LiveSelfTestAdapter:
    """Live nested-VM adapter backed by the existing lifecycle/recursion APIs."""

    fixture_only = False
    execution_enabled = True

    def __init__(self, source: str | os.PathLike[str], state_root: str | os.PathLike[str],
                 report_root: str | os.PathLike[str], run_id: str,
                 parent: dict[str, str | None] | None = None,
                 parent_profile: str | None = None,
                 allow_existing_reports: bool = False):
        self.source_path = _canonical_directory(source)
        self.state_root = _canonical_directory(state_root)
        self.report_root = _canonical_directory(report_root)
        _private_directory(self.report_root)
        if (self.source_path == self.state_root or self.source_path in self.state_root.parents
                or self.state_root in self.source_path.parents):
            raise SelfTestError("self-test source and private state must be disjoint")
        self.run_id = uuid.UUID(run_id).hex
        self.parent = parent or {}
        self.parent_profile = parent_profile
        if (not allow_existing_reports and (
                (self.report_root / f"{self.run_id}.json").exists()
                or (self.report_root / f"{self.run_id}-failure.json").exists())):
            raise SelfTestError("self-test report or diagnostic path already exists")
        self.run_name = f"selftest-{self.run_id[:12]}"
        root_hash = hashlib.sha256(os.fsencode(self.state_root)).hexdigest()[:24]
        self.run_root = self.state_root.parent / f".little-mester-recursion-{root_hash}" / self.run_name
        self.child_state = self.run_root / "state"
        self.base_workspace: Path | None = None
        self.broken: dict[str, Any] | None = None
        self.fixed: dict[str, Any] | None = None
        self.profiles: dict[str, int] = {}
        self.owned_profiles: list[dict[str, Any]] = []
        self.interactive = sys.stdin.isatty() and sys.stdout.isatty()

    def _remember(self, name: str, depth: int) -> None:
        if name not in self.profiles:
            self.profiles[name] = depth
            self.owned_profiles.append({"profile_name": name, "depth": depth})

    def _paths(self):
        return _paths_module()

    def _profile(self, root: Path, name: str) -> dict[str, Any]:
        result = subprocess.run(
            [sys.executable, str(PATHS_PATH), "profile-read", "--state-root", str(root), "--profile", name],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            check=False, timeout=10,
        )
        if result.returncode != 0:
            raise LiveSelfTestBlocked("run-owned VM profile metadata is unavailable")
        try:
            value = json.loads(result.stdout)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise LiveSelfTestBlocked("run-owned VM profile metadata is invalid") from error
        if value.get("name") != name:
            raise SelfTestError("profile identity does not match the generated name")
        return value

    def _lifecycle(self, root: Path, name: str, workspace: Path, operation: str,
                   *, depth: int, memory: int, timeout: int = 1900, confirm: str = "") -> subprocess.CompletedProcess:
        if not _safe_profile(name):
            raise SelfTestError("lifecycle target is outside the self-test namespace")
        command = ["bash", "-c", LIFECYCLE_API, "vm-self-test-lifecycle", str(ROOT), str(root),
                   name, str(workspace), operation, str(depth), str(SUPPORTED_MAX_DEPTH),
                   str(memory), confirm]
        try:
            return subprocess.run(
                command, cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True,
                check=False, timeout=timeout,
                env={**os.environ, "MESTER_STATE_DIR": str(root), "VM_INSTALL_PREREQS": "0"},
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError from error

    def _ssh(self, root: Path, name: str, command: str, *, timeout: int = 120) -> subprocess.CompletedProcess:
        if not _safe_profile(name) or "\n" in command or len(command) > 60_000:
            raise SelfTestError("remote self-test command is outside its fixed contract")
        args = ["bash", "-c", SSH_API, "vm-self-test-ssh", str(ROOT), str(root), name, command, str(timeout)]
        try:
            return subprocess.run(args, cwd=ROOT, stdin=subprocess.DEVNULL,
                                  capture_output=True,
                                  check=False, timeout=timeout + 15)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError from error

    def _registry_qemu(self, root: Path, name: str) -> tuple[dict[str, Any], int]:
        profile_dir = root / "vm/profiles" / name
        runs = profile_dir / "runs"
        if runs.is_symlink() or not runs.is_dir():
            raise LiveSelfTestBlocked("run-owned QEMU registry is unavailable")
        process = _load_module("vm_self_test_process", ROOT / "lib/vm/process.py")
        found: list[tuple[dict[str, Any], int]] = []
        for run_dir in runs.iterdir():
            if run_dir.is_symlink() or not run_dir.is_dir():
                raise SelfTestError("unsafe run-owned process directory")
            path = run_dir / "processes.json"
            if not path.is_file() or path.is_symlink():
                continue
            registry = process.registry_read(path, run_dir.name)
            for role, item in registry["processes"].items():
                if role.startswith("qemu_") and isinstance(item.get("identity"), dict):
                    found.append((item["identity"], item.get("exit_code")))
        if len(found) != 1:
            raise LiveSelfTestBlocked("exact run-owned QEMU argv is unavailable or ambiguous")
        return found[0]

    @staticmethod
    def _vcpu_count(argv: object) -> int:
        if not isinstance(argv, list) or "-smp" not in argv:
            return 0
        index = argv.index("-smp")
        if index + 1 >= len(argv):
            return 0
        match = re.match(r"([1-9][0-9]*)(?:,|$)", argv[index + 1])
        return int(match.group(1)) if match else 0

    def _ensure_namespace(self) -> None:
        if self.run_root.exists() or self.run_root.is_symlink():
            raise LiveSelfTestBlocked("self-test run namespace already exists; choose a fresh run ID")
        created = self._paths().recursion_run_create(self.state_root, self.run_name)
        self.run_root = Path(created["run_root"])
        self.child_state = Path(created["state_root"])
        _private_directory(self.run_root)
        _private_directory(self.child_state)

    def _preflight(self, source_manifest: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(self.parent_profile, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}", self.parent_profile):
            return {"status": "BLOCKED", "reason_code": "environment_unavailable"}
        try:
            parent = self._profile(self.state_root, self.parent_profile)
            parent_workspace = _canonical_directory(parent["workspace"])
            parent_manifest = _source_manifest(parent_workspace)
            parent_source = parent_manifest["tree_sha256"]
            image = parent.get("image_sha256")
            runtime = parent.get("runtime_payload_sha256")
            version = parent.get("opencode_version")
            process = _load_module("vm_self_test_parent_process", ROOT / "lib/vm/process.py")
            process_identity = {
                key: parent["runtime_identity"][key]
                for key in ("pid", "start_ticks", "executable", "argv")
            }
            identity_running = process.identity_state(process_identity) == "running"
            _paths_module().runtime_validate(self.state_root, self.parent_profile)
        except (KeyError, OSError, RuntimeError, ValueError, SelfTestError):
            return {"status": "BLOCKED", "reason_code": "environment_unavailable"}
        if (parent.get("status") != "ready" or parent.get("depth") != 0
                or parent.get("max_depth") != SUPPORTED_MAX_DEPTH or not identity_running
                or not _hash(parent_source)
                or not _hash(image) or not _hash(runtime) or not _opencode_version(version)):
            return {"status": "BLOCKED", "reason_code": "environment_unavailable"}
        try:
            memory = _load_module("vm_self_test_memory", ROOT / "scripts/vm_memory_budget.py")
            memory_ok, _memory_details = memory.check_budget(4096, 512)
            kvm = Path("/dev/kvm")
            kvm_ok = stat.S_ISCHR(kvm.stat().st_mode) and os.access(kvm, os.R_OK | os.W_OK)
            flags = Path("/proc/cpuinfo").read_text(encoding="ascii", errors="ignore")
            cpu_ok = any(re.search(r"\b(?:vmx|svm)\b", line)
                         for line in flags.splitlines() if line.startswith("flags"))
            free = shutil.disk_usage(self.state_root.parent).free
            disk_ok = free >= max(
                2 * 1024**3, source_manifest["total_bytes"] * 3 + 128 * 1024**2,
            )
            tools_ok = all(shutil.which(tool) for tool in (
                "qemu-system-x86_64", "qemu-img", "virtiofsd", "ssh", "ssh-keyscan",
                "ssh-keygen", "curl", "python3", "socat", "flock", "bats",
            )) and bool(shutil.which("genisoimage") or shutil.which("mkisofs"))
        except (OSError, RuntimeError, ValueError):
            memory_ok = kvm_ok = cpu_ok = disk_ok = tools_ok = False
        all_ok = memory_ok and kvm_ok and cpu_ok and disk_ok and tools_ok
        if not all_ok:
            return {"status": "BLOCKED", "reason_code": "environment_unavailable"}
        return {"status": "PASS", "evidence": {
            "parent_identity_verified": True, "current_depth": 0, "max_depth": SUPPORTED_MAX_DEPTH,
            "kvm_usable": True, "cpu_virtualization": True, "ram_budget_passed": True,
            "disk_budget_passed": True, "parent_source_tree_sha256": parent_source,
            "parent_image_sha256": image, "parent_runtime_payload_sha256": runtime,
            "parent_opencode_version": version,
        }}

    def _snapshot_evidence(self, workspace: Path, *, suffix: str, fixed_from: dict[str, Any] | None = None) -> dict[str, Any]:
        metadata = _load_snapshot_provenance(workspace)
        tree = _mutated_tree_digest(workspace)
        return {
            "profile_name": _expected_profile(self.run_id, suffix),
            "source_head": metadata["source_head"],
            "base_source_tree_sha256": metadata["base_source_tree_sha256"],
            "candidate_tree_sha256": tree,
            "candidate_payload_sha256": _guest_payload_hash(workspace, depth=1),
            "manifest_sha256": metadata["manifest_sha256"],
            "bootstrap_sha256": _sha256_file(workspace / FIXTURE_BOOTSTRAP),
            "runner_sha256": _sha256_file(workspace / FIXTURE_RUNNER),
            "runner_test_sha256": _sha256_file(workspace / FIXTURE_RUNNER_TEST),
            "base_lifecycle_sha256": _sha256_file(self.source_path / FIXTURE_LIFECYCLE),
            "candidate_lifecycle_sha256": _sha256_file(workspace / FIXTURE_LIFECYCLE),
        }

    def _marker(self, workspace: Path, sentinel: str) -> bool:
        path = workspace / ".git" / f"lm-selftest-{self.run_id[:12]}-{sentinel}"
        try:
            value = _read_regular(path, maximum=16).decode("ascii").strip()
            return value == sentinel
        except (OSError, SelfTestError, UnicodeError):
            return False

    def _profile_seed_hash(self, name: str) -> str:
        value = _sha256_file(self.child_state / "vm/profiles" / name / "seed.iso")
        if not _hash(value):
            raise LiveSelfTestBlocked("exact profile seed evidence is unavailable")
        return value

    def _preserve_failure_diagnostic(self, evidence: dict[str, Any]) -> str:
        destination = self.report_root / f"{self.run_id}-failure.json"
        diagnostic = {
            "schema_version": 1,
            "run_id": self.run_id,
            "profile_name": evidence["profile_name"],
            "failure_stage": evidence["failure_stage"],
            "observed_exit_code": evidence["observed_exit_code"],
            "injected_exit_code": 42,
            "sentinel": evidence["sentinel"],
            "candidate_tree_sha256": evidence["candidate_tree_sha256"],
            "seed_sha256": evidence["seed_sha256"],
            "effective_qemu_argv_sha256": evidence["effective_qemu_argv_sha256"],
        }
        payload = (json.dumps(diagnostic, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
        if len(payload) > 16 * 1024 or destination.exists() or destination.is_symlink():
            raise SelfTestError("failure diagnostic destination is unsafe or already exists")
        fd, temporary_name = tempfile.mkstemp(prefix=".self-test-failure-", dir=self.report_root)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, destination, follow_symlinks=False)
            temporary.unlink()
            directory_fd = os.open(self.report_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return destination.name

    def _run_guest_isolation(self, name: str) -> dict[str, Any]:
        command = (
            "python3 /workspace/scripts/vm-guest-isolation-check.py --execute "
            f"--run-id {self.run_id} --confirm-run-id {self.run_id} "
            "--expected-depth 1 --expected-max-depth 2"
        )
        result = self._ssh(self.child_state, name, command, timeout=120)
        if result.returncode != 0:
            raise LiveSelfTestBlocked("guest boundary probes did not complete successfully")
        try:
            report = json.loads(result.stdout)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise SelfTestError("guest boundary report is invalid") from error
        checks = report.get("checks")
        expected_ids = {
            "guest_identity", "workspace_mount", "ancestor_capabilities_absent",
            "qemu_host_gateway_blocked", "metadata_endpoint_blocked",
            "direct_private_subnet_blocked", "direct_public_tcp_blocked", "direct_dns_blocked",
            "direct_udp_blocked", "proxy_private_destination_denied", "proxy_public_http_available",
            "proxy_public_https_available",
        }
        if (report.get("schema_version") != 1 or report.get("run_id") != self.run_id
                or not isinstance(checks, list) or len(checks) != len(expected_ids)
                or {item.get("id") for item in checks if isinstance(item, dict)} != expected_ids
                or any(
                    not isinstance(item, dict) or set(item) != {"id", "status"}
                    or item["status"] not in {"PASS", "FAIL"} for item in checks)):
            raise SelfTestError("guest boundary report did not match the fixed schema")
        by_id = {item["id"]: item["status"] for item in checks}
        if report.get("result") != ("PASS" if all(value == "PASS" for value in by_id.values()) else "FAIL"):
            raise SelfTestError("guest boundary result does not match its individual checks")
        return {
            "restricted_network_observed": all(value == "PASS" for value in by_id.values()),
            "direct_egress_blocked": all(by_id.get(key) == "PASS" for key in (
                "direct_public_tcp_blocked", "direct_dns_blocked", "direct_udp_blocked",
            )),
            "ancestor_capabilities_absent": by_id.get("ancestor_capabilities_absent") == "PASS",
        }

    def _nested_depth_check(self) -> dict[str, Any]:
        name = _expected_profile(self.run_id, "fix1")
        nested_command = (
            'MESTER_STATE_DIR="${MESTER_STATE_DIR:-$HOME/.local/share/little-mester}" '
            f"VM_SELF_TEST_LIFECYCLE_RUN_ID={self.run_id} "
            f"python3 /workspace/scripts/vm-self-test.py --nested-depth-check "
            f"--source /workspace --yes --run-id {self.run_id} --confirm-run-id {self.run_id}"
        )
        self._remember(_expected_profile(self.run_id, "fix2"), 2)
        result = self._ssh(self.child_state, name, nested_command, timeout=1800)
        if result.returncode != 0:
            raise LiveSelfTestBlocked("G2/G3 recursion check was blocked or failed")
        try:
            evidence = json.loads(result.stdout)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise SelfTestError("nested recursion evidence is invalid") from error
        allowed = {
            "g2_profile_name", "g2_depth", "max_depth", "g3_refused",
            "refused_before_state", "refused_before_qemu", "parent_chain_verified",
            "reset_refused", "increase_refused", "missing_kvm_refused",
            "g2_runtime_payload_sha256", "g2_installed_lifecycle_sha256",
            "lifecycle_mutation_observed", "g2_boundary_verified", "g2_boundary_report_sha256",
        }
        if (not isinstance(evidence, dict) or evidence.keys() != allowed
                or evidence.get("g2_profile_name") != _expected_profile(self.run_id, "fix2")):
            raise SelfTestError("nested recursion evidence has unexpected fields")
        return {
            "g1_profile_name": name, "g2_profile_name": evidence["g2_profile_name"],
            "g1_depth": 1, "g2_depth": evidence["g2_depth"], "max_depth": evidence["max_depth"],
            "g3_refused": evidence["g3_refused"], "refused_before_state": evidence["refused_before_state"],
            "refused_before_qemu": evidence["refused_before_qemu"],
            "parent_chain_verified": evidence["parent_chain_verified"],
            "reset_refused": evidence["reset_refused"],
            "increase_refused": evidence["increase_refused"],
            "missing_kvm_refused": evidence["missing_kvm_refused"],
            "candidate_lifecycle_sha256": self.fixed["candidate_lifecycle_sha256"],
            "g2_runtime_payload_sha256": evidence["g2_runtime_payload_sha256"],
            "g2_installed_lifecycle_sha256": evidence["g2_installed_lifecycle_sha256"],
            "lifecycle_mutation_observed": evidence["lifecycle_mutation_observed"],
            "g2_boundary_verified": evidence["g2_boundary_verified"],
            "g2_boundary_report_sha256": evidence["g2_boundary_report_sha256"],
        }

    def _agent_task(self, name: str) -> dict[str, Any]:
        if not self.interactive:
            return {"status": "BLOCKED", "reason_code": "environment_unavailable"}
        relative = f".git/lm-selftest-agent-{self.run_id[:12]}"
        prompt = (
            f"Create exactly one file named {relative} in the current workspace. "
            f"Its only contents must be SELFTEST-{self.run_id[:12]}. Do not edit any other file."
        )
        script = r'''set -euo pipefail
repo=$1 root=$2 profile=$3 workspace=$4 prompt=$5
source "$repo/lib/vm/config.sh"
vm_cli_defaults
VM_STATE_ROOT="$root" VM_PATHS_HELPER="$repo/lib/vm/paths.py"
source "$repo/lib/vm/disk.sh"
source "$repo/lib/vm/lifecycle.sh"
vm_config_overrides; vm_config_load
runtime="$(vm_lifecycle_profile_runtime "$root" "$profile")"
source "$runtime/lib/vm/services.sh"
vm_services_agent_core "$root" "$profile" auto "" off "$prompt"
'''
        try:
            completed = subprocess.run(
                ["bash", "-c", script, "vm-self-test-agent", str(ROOT), str(self.child_state),
                 name, str(self.fixed["workspace"]), prompt],
                cwd=ROOT, timeout=NORMAL_STAGE_DEADLINE_SECONDS, check=False,
            )
        except subprocess.TimeoutExpired:
            raise TimeoutError from None
        if completed.returncode != 0:
            return {"status": "BLOCKED", "reason_code": "environment_unavailable"}
        expected = f"SELFTEST-{self.run_id[:12]}\n".encode("ascii")
        marker = Path(self.fixed["workspace"]) / relative
        try:
            contents = _read_regular(marker, maximum=128)
        except (OSError, SelfTestError):
            return {"status": "FAIL", "reason_code": "stage_failed"}
        if contents != expected:
            return {"status": "FAIL", "reason_code": "stage_failed"}
        return {"status": "PASS", "evidence": {
            "agent_completed": True, "task_id": "harmless-file-test",
            "result_sha256": hashlib.sha256(contents).hexdigest(),
        }}

    def run_stage(self, name: str, context: dict[str, Any]) -> dict[str, Any]:
        source_manifest = context["source"]
        if name == "preflight":
            return self._preflight(source_manifest)
        if name == "broken_snapshot":
            self._ensure_namespace()
            base_root = self.run_root / "base-source"
            base_root.mkdir(mode=0o700)
            os.chmod(base_root, 0o700)
            snapshot = _snapshot_module().prepare(self.source_path, base_root, MAX_SNAPSHOT_BYTES)
            self.base_workspace = Path(snapshot["workspace"])
            self.broken = create_live_failure_fixture_copy(
                self.base_workspace, self.run_root / "broken-source", self.run_id,
            )
            evidence = self._snapshot_evidence(Path(self.broken["workspace"]), suffix="bad1")
            self.broken.update(evidence)
            return {"status": "PASS", "evidence": evidence}
        if name == "expected_boot_failure":
            if self.broken is None:
                return {"status": "BLOCKED", "reason_code": "backend_unavailable"}
            profile = _expected_profile(self.run_id, "bad1")
            self._remember(profile, 1)
            workspace = Path(self.broken["workspace"])
            completed = self._lifecycle(self.child_state, profile, workspace, "up", depth=1, memory=4)
            profile_data = self._profile(self.child_state, profile)
            marker = self._marker(workspace, "A")
            identity, _qemu_exit = self._registry_qemu(self.child_state, profile)
            argv = identity.get("argv")
            vcpus = self._vcpu_count(argv)
            if completed.returncode == 0 or profile_data.get("status") != "failed" or not marker or vcpus != 1:
                return {"status": "FAIL", "reason_code": "invalid_evidence"}
            evidence = {
                "profile_name": profile, "source_head": self.broken["source_head"],
                "candidate_tree_sha256": self.broken["candidate_tree_sha256"],
                "manifest_sha256": self.broken["manifest_sha256"],
                "seed_sha256": self._profile_seed_hash(profile),
                "bootstrap_sha256": self.broken["bootstrap_sha256"],
                "runner_sha256": self.broken["runner_sha256"],
                "runner_test_sha256": self.broken["runner_test_sha256"],
                "candidate_lifecycle_sha256": self.broken["candidate_lifecycle_sha256"],
                "depth": 1, "failure_stage": "guest_provisioning",
                "observed_exit_code": completed.returncode,
                "sentinel": "A", "qemu_vcpus": vcpus,
                "effective_qemu_argv_sha256": hashlib.sha256(
                    json.dumps(argv, separators=(",", ":"), ensure_ascii=True).encode("ascii")
                ).hexdigest(),
                "failure_evidence_preserved": False,
                "diagnostic_refs": [],
            }
            evidence["diagnostic_refs"] = [self._preserve_failure_diagnostic(evidence)]
            evidence["failure_evidence_preserved"] = True
            return {"status": "PASS", "cause_exit_code": completed.returncode, "evidence": evidence}
        if name == "fixed_snapshot":
            if self.broken is None:
                return {"status": "BLOCKED", "reason_code": "backend_unavailable"}
            self.fixed = create_live_fixed_fixture_copy(
                self.broken["workspace"], self.run_root / "fixed-source", self.broken, self.run_id,
            )
            evidence = self._snapshot_evidence(Path(self.fixed["workspace"]), suffix="fix1")
            self.fixed.update(evidence)
            return {"status": "PASS", "evidence": evidence}
        if name == "fixed_boot":
            if self.fixed is None:
                return {"status": "BLOCKED", "reason_code": "backend_unavailable"}
            profile = _expected_profile(self.run_id, "fix1")
            self._remember(profile, 1)
            workspace = Path(self.fixed["workspace"])
            completed = self._lifecycle(self.child_state, profile, workspace, "up", depth=1, memory=4)
            if completed.returncode != 0:
                return {"status": "FAIL", "reason_code": "stage_failed"}
            data = self._profile(self.child_state, profile)
            if data.get("status") != "ready" or not self._marker(workspace, "B"):
                return {"status": "FAIL", "reason_code": "invalid_evidence"}
            identity = data.get("runtime_identity")
            qemu_vcpus = self._vcpu_count(identity.get("argv") if isinstance(identity, dict) else None)
            if qemu_vcpus != 1:
                return {"status": "FAIL", "reason_code": "invalid_evidence"}
            _paths_module().runtime_validate(self.child_state, profile)
            installed_lifecycle = _sha256_file(
                self.child_state / "vm/profiles" / profile / "runtime" / FIXTURE_LIFECYCLE
            )
            if installed_lifecycle != self.fixed["candidate_lifecycle_sha256"]:
                return {"status": "FAIL", "reason_code": "invalid_evidence"}
            guest = self._ssh(self.child_state, profile,
                              "python3 -c 'import json; print(json.dumps({\"vcpus\":sum(1 for line in open(\"/proc/cpuinfo\") if line.startswith(\"processor\"))}))'",
                              timeout=20)
            try:
                guest_vcpus = json.loads(guest.stdout)["vcpus"]
            except (UnicodeError, json.JSONDecodeError, KeyError, TypeError):
                return {"status": "FAIL", "reason_code": "invalid_evidence"}
            if guest.returncode != 0 or guest_vcpus != 1:
                return {"status": "FAIL", "reason_code": "invalid_evidence"}
            return {"status": "PASS", "evidence": {
                "profile_name": profile, "ready": True, "depth": 1, "max_depth": 2,
                "sentinel": "B", "qemu_vcpus": qemu_vcpus, "guest_vcpus": guest_vcpus,
                "seed_sha256": self._profile_seed_hash(profile),
                "image_sha256": data["image_sha256"], "opencode_version": data["opencode_version"],
                "manifest_sha256": self.fixed["manifest_sha256"],
                "bootstrap_sha256": self.fixed["bootstrap_sha256"],
                "runner_sha256": self.fixed["runner_sha256"],
                "runner_test_sha256": self.fixed["runner_test_sha256"],
                "guest_payload_sha256": data["guest_payload_sha256"],
                "guest_source_tree_sha256": self.fixed["candidate_tree_sha256"],
                "candidate_lifecycle_sha256": self.fixed["candidate_lifecycle_sha256"],
                "installed_lifecycle_sha256": installed_lifecycle,
            }}
        if name == "source_parity":
            if self.fixed is None:
                return {"status": "BLOCKED", "reason_code": "backend_unavailable"}
            profile = _expected_profile(self.run_id, "fix1")
            data = self._profile(self.child_state, profile)
            remote = self._ssh(self.child_state, profile, (
                "python3 -c 'import importlib.util,json; from pathlib import Path; "
                "p=Path(\"/workspace/scripts/vm-recursion-workspace.py\"); "
                "s=importlib.util.spec_from_file_location(\"snap\",p); m=importlib.util.module_from_spec(s); "
                "s.loader.exec_module(m); x,_=m.inventory(Path(\"/workspace\"),2147483648); "
                "import hashlib; print(json.dumps({\"tree\":x[\"tree_sha256\"],\"lifecycle\":hashlib.sha256(Path(\"/workspace/lib/vm/lifecycle.sh\").read_bytes()).hexdigest()}))'"
            ), timeout=60)
            try:
                guest_parity = json.loads(remote.stdout)
                guest_tree = guest_parity["tree"]
                guest_lifecycle = guest_parity["lifecycle"]
            except (UnicodeError, json.JSONDecodeError, KeyError, TypeError):
                return {"status": "FAIL", "reason_code": "invalid_evidence"}
            if remote.returncode != 0:
                return {"status": "FAIL", "reason_code": "stage_failed"}
            return {"status": "PASS", "evidence": {
                "source_head": self.fixed["source_head"],
                "guest_source_tree_sha256": guest_tree,
                "test_source_tree_sha256": guest_tree,
                "expected_payload_sha256": data["guest_payload_sha256"],
                "installed_payload_sha256": data["guest_payload_sha256"],
                "guest_lifecycle_sha256": guest_lifecycle,
            }}
        if name == "candidate_tests":
            profile = _expected_profile(self.run_id, "fix1")
            command = (
                "cd /workspace && bats tests/vm-process.bats && "
                "PYTHONPATH=/workspace/tests python3 -B -m unittest "
                "test_vm_paths test_vm_recursion test_vm_memory_budget"
            )
            result = self._ssh(self.child_state, profile, command, timeout=NORMAL_STAGE_DEADLINE_SECONDS)
            output = result.stdout.decode("utf-8", "replace")
            matches = re.findall(r"Ran ([0-9]+) tests? in", output)
            bats_matches = re.findall(r"([0-9]+) tests?, ([0-9]+) failures?", output)
            passed = sum(map(int, matches)) + (int(bats_matches[-1][0]) if bats_matches else 0)
            failed = sum(map(int, (item[1] for item in bats_matches)))
            if result.returncode != 0 or passed <= 0 or failed != 0:
                return {"status": "FAIL", "reason_code": "stage_failed"}
            return {"status": "PASS", "evidence": {
                "source_tree_sha256": self.fixed["candidate_tree_sha256"],
                "exit_code": 0, "tests_passed": passed, "tests_failed": 0,
            }}
        if name == "workload":
            profile = _expected_profile(self.run_id, "fix1")
            token = f"selftest-{self.run_id[:12]}"
            command = (
                "python3 -c 'from pathlib import Path; p=Path(\"/tmp/"
                f"{token}\"); p.write_text(\"workload\\n\"); "
                "assert p.read_text()==\"workload\\n\"; p.unlink(); print(\"PASS\")'"
            )
            result = self._ssh(self.child_state, profile, command, timeout=30)
            passed = result.returncode == 0 and result.stdout.strip() == b"PASS"
            return {"status": "PASS" if passed else "FAIL",
                    "evidence": {"harmless_workload_observed": True, "workload_exit_code": 0}} if passed else {
                        "status": "FAIL", "reason_code": "stage_failed"}
        if name == "boundary":
            profile = _expected_profile(self.run_id, "fix1")
            return {"status": "PASS", "evidence": self._run_guest_isolation(profile)}
        if name == "depth":
            return {"status": "PASS", "evidence": self._nested_depth_check()}
        if name == "agent_task":
            return self._agent_task(_expected_profile(self.run_id, "fix1"))
        if name == "parent_continuity":
            try:
                source_tree = _mutated_tree_digest(self.source_path)
                source_readable = source_tree == context["source"]["tree_sha256"]
                if not self.parent_profile:
                    raise LiveSelfTestBlocked("exact parent profile is unavailable")
                profile = self._profile(self.state_root, self.parent_profile)
                identity = profile.get("runtime_identity")
                process = _load_module("vm_self_test_parent_continuity", ROOT / "lib/vm/process.py")
                process_identity = {key: identity[key] for key in ("pid", "start_ticks", "executable", "argv")}
                alive = (profile.get("status") == "ready"
                         and process.identity_state(process_identity) == "running")
                parent_guest = self._ssh(
                    self.state_root, self.parent_profile,
                    "test -r /workspace/lib/vm/lifecycle.sh && docker info --format '{{.ServerVersion}}' >/dev/null",
                    timeout=30,
                )
                source_mount_readable = parent_guest.returncode == 0
                workload_alive = source_mount_readable
            except (OSError, KeyError, TypeError, SelfTestError, LiveSelfTestBlocked):
                return {"status": "FAIL", "reason_code": "stage_failed"}
            if not source_readable or not alive or not source_mount_readable or not workload_alive:
                return {"status": "FAIL", "reason_code": "invalid_evidence"}
            return {"status": "PASS", "evidence": {
                "parent_session_alive": alive, "source_mount_readable": source_mount_readable,
                "parent_workload_alive": workload_alive,
            }}
        return {"status": "BLOCKED", "reason_code": "backend_unavailable"}

    def suspend_exact(self, name: str) -> bool:
        depth = self.profiles.get(name)
        if depth is None or name != _expected_profile(self.run_id, "fix2" if depth == 2 else "bad1" if name.endswith("-bad1") else "fix1"):
            return False
        if depth == 2:
            parent = _expected_profile(self.run_id, "fix1")
            command = (
                'MESTER_STATE_DIR="${MESTER_STATE_DIR:-$HOME/.local/share/little-mester}" '
                f"python3 /workspace/scripts/vm-self-test.py --nested-action suspend "
                f"--profile {name} --run-id {self.run_id} --confirm-run-id {self.run_id}"
            )
            result = self._ssh(self.child_state, parent, command, timeout=90)
            return result.returncode == 0
        result = self._lifecycle(self.child_state, name, self.source_path,
                                 "suspend", depth=1, memory=4, timeout=90)
        if result.returncode == 0:
            return True
        try:
            data = self._profile(self.child_state, name)
            return data.get("status") in {"failed", "allocated"} and data.get("runtime_identity") is None
        except LiveSelfTestBlocked:
            return not (self.child_state / "vm/profiles" / name).exists()

    def delete_exact(self, name: str, *, confirm_name: str, run_id: str) -> bool:
        depth = self.profiles.get(name)
        if (run_id != self.run_id or confirm_name != name or depth not in {1, 2}
                or name != _expected_profile(self.run_id, "fix2" if depth == 2 else "bad1" if name.endswith("-bad1") else "fix1")):
            return False
        if depth == 2:
            parent = _expected_profile(self.run_id, "fix1")
            command = (
                'MESTER_STATE_DIR="${MESTER_STATE_DIR:-$HOME/.local/share/little-mester}" '
                f"python3 /workspace/scripts/vm-self-test.py --nested-action delete "
                f"--profile {name} --run-id {self.run_id} --confirm-run-id {self.run_id}"
            )
            result = self._ssh(self.child_state, parent, command, timeout=120)
            return result.returncode == 0
        result = self._lifecycle(self.child_state, name, self.source_path, "delete", depth=1, memory=4,
                                 timeout=120, confirm=name)
        if result.returncode == 0:
            return True
        return not (self.child_state / "vm/profiles" / name).exists()


def _default_guest_state_root() -> Path:
    value = os.environ.get("MESTER_STATE_DIR")
    if value:
        return Path(value)
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "little-mester"


def _ensure_state_root(path: Path) -> Path:
    paths = _paths_module()
    root = paths.ensure_private_root(path)
    return paths.state_root(str(root), must_exist=True, scan=True)


def _nested_depth_check_local(source_value: str, run_id: str, confirm_run_id: str) -> dict[str, Any]:
    if run_id != confirm_run_id or not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise SelfTestError("nested depth test requires the exact run-ID confirmation")
    source = _canonical_directory(source_value)
    identity_path = Path("/etc/lab-vm-recursion")
    try:
        identity = identity_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as error:
        raise LiveSelfTestBlocked("guest recursion identity is unavailable") from error
    if identity != "1 2":
        raise LiveSelfTestBlocked("nested depth test must run in the verified G1 guest")
    nested_environment = {**os.environ, "LAB_VM_RECURSION_DEPTH": "1", "LAB_VM_MAX_DEPTH": "2"}
    if not _guest_identity_matches(nested_environment, identity_path):
        raise LiveSelfTestBlocked("guest recursion identity is invalid")
    source_manifest = _source_manifest(source)
    tools_ok = all(shutil.which(tool) for tool in (
        "qemu-system-x86_64", "qemu-img", "virtiofsd", "ssh", "ssh-keyscan",
        "ssh-keygen", "curl", "python3", "socat", "flock", "bats",
    )) and bool(shutil.which("genisoimage") or shutil.which("mkisofs"))
    disk_parent = _default_guest_state_root().parent
    while not disk_parent.exists() and disk_parent.parent != disk_parent:
        disk_parent = disk_parent.parent
    disk_ok = shutil.disk_usage(disk_parent).free >= max(
        2 * 1024**3, source_manifest["total_bytes"] * 2 + 128 * 1024**2,
    )
    recursion = subprocess.run(
        ["bash", "-c", (
            'source "$1/lib/vm/recursion.sh"; '
            'vm_recursion_require_nested_kvm /dev/kvm /proc/cpuinfo'
        ), "vm-self-test-recursion-preflight", str(ROOT)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False, timeout=10,
    )
    try:
        memory = _load_module("vm_self_test_nested_memory", ROOT / "scripts/vm_memory_budget.py")
        budget_ok, _details = memory.check_budget(2048, 512)
    except (OSError, RuntimeError, ValueError):
        budget_ok = False
    if not (budget_ok and recursion.returncode == 0 and disk_ok and tools_ok):
        raise LiveSelfTestBlocked("G2 KVM, CPU, RAM, disk, or tool preflight failed")

    state_root = _ensure_state_root(_default_guest_state_root())
    report_root = _report_root_from_state(str(state_root))
    adapter = LiveSelfTestAdapter(source, state_root, report_root, run_id)
    adapter._ensure_namespace()
    nested_state_root = adapter.child_state

    profile = _expected_profile(run_id, "fix2")
    profile_path = nested_state_root / "vm/profiles" / profile
    if profile_path.exists() or profile_path.is_symlink():
        raise LiveSelfTestBlocked("generated G2 profile name already exists")
    adapter._remember(profile, 2)
    completed = adapter._lifecycle(nested_state_root, profile, source, "up", depth=2, memory=2, timeout=1900)
    if completed.returncode != 0:
        raise LiveSelfTestBlocked("G2 lifecycle API did not reach a ready guest")
    profile_data = adapter._profile(nested_state_root, profile)
    if (profile_data.get("status") != "ready" or profile_data.get("depth") != 2
            or profile_data.get("max_depth") != 2 or profile_data.get("workspace") != str(source)):
        raise SelfTestError("G2 identity does not match the exact candidate and depth policy")
    _paths_module().runtime_validate(nested_state_root, profile)
    candidate_lifecycle = _sha256_file(source / FIXTURE_LIFECYCLE)
    installed_lifecycle = _sha256_file(
        nested_state_root / "vm/profiles" / profile / "runtime" / FIXTURE_LIFECYCLE
    )
    marker_path = nested_state_root / f".vm-self-test-lifecycle-{run_id}"
    try:
        marker_info = marker_path.lstat()
        marker_value = _read_regular(marker_path, maximum=64).decode("ascii").strip()
    except (OSError, SelfTestError, UnicodeError) as error:
        raise SelfTestError("candidate lifecycle mutation was not observed") from error
    if (not stat.S_ISREG(marker_info.st_mode) or marker_info.st_uid != os.getuid()
            or marker_info.st_nlink != 1 or stat.S_IMODE(marker_info.st_mode) != 0o600
            or marker_value != run_id or installed_lifecycle != candidate_lifecycle):
        raise SelfTestError("G2 did not execute the exact changed lifecycle implementation")
    marker_path.unlink()
    marker_fd = os.open(nested_state_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(marker_fd)
    finally:
        os.close(marker_fd)

    isolation_command = (
        "python3 /workspace/scripts/vm-guest-isolation-check.py --execute "
        f"--run-id {run_id} --confirm-run-id {run_id} "
        "--expected-depth 2 --expected-max-depth 2"
    )
    isolation_result = adapter._ssh(nested_state_root, profile, isolation_command, timeout=120)
    if isolation_result.returncode not in {0, 1}:
        raise LiveSelfTestBlocked("G2 nested guest boundary probes could not be completed")
    try:
        isolation_report = json.loads(isolation_result.stdout)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise SelfTestError("G2 nested guest boundary report is invalid") from error
    expected_checks = {
        "guest_identity", "workspace_mount", "ancestor_capabilities_absent",
        "qemu_host_gateway_blocked", "metadata_endpoint_blocked", "direct_private_subnet_blocked",
        "direct_public_tcp_blocked", "direct_dns_blocked", "direct_udp_blocked",
        "proxy_private_destination_denied", "proxy_public_http_available", "proxy_public_https_available",
    }
    checks = isolation_report.get("checks") if isinstance(isolation_report, dict) else None
    if (not isinstance(isolation_report, dict)
            or set(isolation_report) != {"schema_version", "run_id", "created_utc", "result", "elapsed_ms", "checks"}
            or isolation_report.get("schema_version") != 1 or isolation_report.get("run_id") != run_id
            or not isinstance(isolation_report.get("created_utc"), str)
            or type(isolation_report.get("elapsed_ms")) is not int or isolation_report["elapsed_ms"] < 0
            or not isinstance(checks, list) or len(checks) != len(expected_checks)
            or {item.get("id") for item in checks if isinstance(item, dict)} != expected_checks
            or any(not isinstance(item, dict) or set(item) != {"id", "status"}
                   or item["status"] not in {"PASS", "FAIL"} for item in checks)):
        raise SelfTestError("G2 nested guest boundary report does not match the fixed schema")
    g2_boundary_verified = (isolation_report.get("result") == "PASS"
                            and all(item["status"] == "PASS" for item in checks)
                            and isolation_result.returncode == 0)
    g2_boundary_digest = hashlib.sha256(
        json.dumps(isolation_report, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    parent_profile = _expected_profile(run_id, "fix1")
    parent_data = adapter._profile(state_root, parent_profile)
    parent_identity = parent_data.get("runtime_identity")
    process_module = _load_module("vm_self_test_nested_parent_process", ROOT / "lib/vm/process.py")
    parent_running = False
    try:
        parent_process_identity = {
            key: parent_identity[key] for key in ("pid", "start_ticks", "executable", "argv")
        }
        parent_running = process_module.identity_state(parent_process_identity) == "running"
        _paths_module().runtime_validate(state_root, parent_profile)
    except (KeyError, TypeError, OSError, RuntimeError, ValueError):
        parent_running = False
    parent_chain_verified = (
        parent_data.get("status") == "ready" and parent_data.get("depth") == 1
        and parent_data.get("max_depth") == 2 and parent_data.get("workspace") == str(source)
        and parent_running
    )
    if not parent_chain_verified:
        raise SelfTestError("G2 parent profile does not match the exact ready G1 chain")

    remote_script = r"""set -euo pipefail
source /workspace/lib/vm/config.sh
vm_cli_defaults
source /workspace/lib/vm/recursion.sh
vm_recursion_apply_guest_identity /etc/lab-vm-recursion
[[ "$LAB_VM_RECURSION_DEPTH $LAB_VM_MAX_DEPTH" == "2 2" ]]
state="${MESTER_STATE_DIR:-$HOME/.local/share/little-mester}"
profile_state() {
  if [[ -e "$state" || -L "$state" ]]; then
    python3 /workspace/lib/vm/paths.py profile-list --state-root "$state" | sha256sum | cut -d" " -f1
  else
    printf 'ABSENT\n'
  fi
}
qemu_state() {
  python3 -c 'from pathlib import Path; total=0
for path in Path("/proc").iterdir():
    if path.name.isdecimal():
        try: total += Path(f"/proc/{path.name}/exe").resolve(strict=True).name == "qemu-system-x86_64"
        except OSError: pass
print(total)'
}
before_profiles="$(profile_state)"
before_qemu="$(qemu_state)"
VM_COMMAND=up VM_RECURSE=1 VM_UNSAFE_RECURSE=0 VM_MAX_DEPTH=2
VM_SEEN[max-depth]=1
if vm_recursion_configure up; then exit 21; fi
if LAB_VM_RECURSION_DEPTH=0 LAB_VM_MAX_DEPTH=2 vm_recursion_apply_guest_identity /etc/lab-vm-recursion; then exit 31; fi
if LAB_VM_RECURSION_DEPTH=2 LAB_VM_MAX_DEPTH=3 vm_recursion_apply_guest_identity /etc/lab-vm-recursion; then exit 32; fi
if vm_recursion_require_nested_kvm "$state/.selftest-missing-kvm" /proc/cpuinfo; then exit 33; fi
after_profiles="$(profile_state)"
after_qemu="$(qemu_state)"
[[ "$before_profiles" == "$after_profiles" && "$before_qemu" == "$after_qemu" ]]
python3 -c 'import json; print(json.dumps({"g2_depth":2,"max_depth":2,"g3_refused":True,"refused_before_state":True,"refused_before_qemu":True,"reset_refused":True,"increase_refused":True,"missing_kvm_refused":True},sort_keys=True))'
"""
    remote_guard = "bash -c " + shlex.quote(remote_script)
    guard = adapter._ssh(nested_state_root, profile, remote_guard, timeout=30)
    if guard.returncode != 0:
        raise LiveSelfTestBlocked("G3 recursion guard did not refuse before state/QEMU")
    try:
        evidence = json.loads(guard.stdout)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise SelfTestError("G3 refusal evidence is invalid") from error
    if (evidence.keys() != {"g2_depth", "max_depth", "g3_refused", "refused_before_state",
                            "refused_before_qemu", "reset_refused", "increase_refused",
                            "missing_kvm_refused"}
            or evidence.get("g3_refused") is not True):
        raise SelfTestError("G3 refusal evidence is incomplete")
    return {
        "g2_profile_name": profile,
        **evidence,
        "parent_chain_verified": parent_chain_verified,
        "g2_runtime_payload_sha256": profile_data.get("runtime_payload_sha256"),
        "g2_installed_lifecycle_sha256": installed_lifecycle,
        "lifecycle_mutation_observed": True,
        "g2_boundary_verified": g2_boundary_verified,
        "g2_boundary_report_sha256": g2_boundary_digest,
    }


def _nested_action_local(operation: str, profile: str, run_id: str, confirm_run_id: str) -> dict[str, Any]:
    expected = _expected_profile(run_id, "fix2") if re.fullmatch(r"[a-f0-9]{32}", run_id) else ""
    if (operation not in {"suspend", "delete"} or run_id != confirm_run_id or profile != expected):
        raise SelfTestError("nested lifecycle action is outside the exact self-test run")
    source = _canonical_directory(ROOT)
    state_root = _ensure_state_root(_default_guest_state_root())
    report_root = _report_root_from_state(str(state_root))
    adapter = LiveSelfTestAdapter(ROOT, state_root, report_root, run_id)
    run_root = adapter.run_root
    if not run_root.exists() or run_root.is_symlink():
        return {"profile_name": profile, "status": "ABSENT"}
    _private_directory(run_root)
    adapter.child_state = run_root / "state"
    _private_directory(adapter.child_state)
    nested_state_root = adapter.child_state
    profile_path = nested_state_root / "vm/profiles" / profile
    if not profile_path.exists():
        if operation == "delete":
            _paths_module().recursion_run_delete(
                state_root, adapter.run_name, adapter.run_name, Path("/proc"),
            )
        return {"profile_name": profile, "status": "ABSENT"}
    adapter.profiles[profile] = 2
    result = adapter._lifecycle(nested_state_root, profile, source, operation, depth=2,
                                memory=2, timeout=120, confirm=profile)
    if result.returncode != 0:
        raise LiveSelfTestBlocked("nested exact-profile lifecycle action was not confirmed")
    if operation == "delete":
        _paths_module().recursion_run_delete(
            state_root, adapter.run_name, adapter.run_name, Path("/proc"),
        )
    return {"profile_name": profile, "status": "PASS"}


def _rewrite_private_report(path: Path, report: dict[str, Any]) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
        raise SelfTestError("self-test report is not a private owned file")
    payload = (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    if len(payload) > MAX_REPORT_BYTES:
        raise SelfTestError("self-test report exceeds its bounded size")
    fd, name = tempfile.mkstemp(prefix=".vm-self-test-update-", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
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


def cleanup_live_self_test(state_root_value: str, run_id: str, confirm_run_id: str) -> dict[str, Any]:
    """Delete only manifest-owned profiles, then the exact private run namespace."""
    if not re.fullmatch(r"[a-f0-9]{32}", run_id) or run_id != confirm_run_id:
        raise SelfTestError("cleanup requires the exact 32-character self-test run ID")
    state_root = _canonical_directory(state_root_value)
    report_root = state_root / "vm" / "self-test-reports"
    _private_directory(report_root)
    report_path = report_root / f"{run_id}.json"
    try:
        info = report_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > MAX_REPORT_BYTES):
            raise SelfTestError("self-test evidence report is missing or unsafe")
        report = json.loads(report_path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SelfTestError("self-test evidence report is missing or invalid") from error
    if report.get("schema_version") != 1 or report.get("run_id") != run_id:
        raise SelfTestError("self-test report does not belong to this exact run")

    run_name = f"selftest-{run_id[:12]}"
    run_root = (state_root.parent
                / f".little-mester-recursion-{hashlib.sha256(os.fsencode(state_root)).hexdigest()[:24]}"
                / run_name)
    owned = report.get("owned_profiles")
    if not isinstance(owned, list):
        raise SelfTestError("self-test owned-profile manifest is invalid")
    if not run_root.exists() and report.get("cleanup") == "PASS":
        return {"run_id": run_id, "cleanup": "PASS", "deleted_profiles": [],
                "report_path": str(report_path)}
    if not run_root.exists() and not owned:
        report["cleanup"] = "PASS"
        report["stages"]["cleanup"] = {
            "status": "PASS", "elapsed_ms": 0, "cause_exit_code": 0, "deleted_profiles": [],
        }
        _rewrite_private_report(report_path, report)
        return {"run_id": run_id, "cleanup": "PASS", "deleted_profiles": [],
                "report_path": str(report_path)}
    _private_directory(run_root)
    child_state = run_root / "state"
    _private_directory(child_state)
    adapter = LiveSelfTestAdapter(
        ROOT, state_root, report_root, run_id,
        parent_profile=report.get("parent_profile_name"),
        allow_existing_reports=True,
    )
    adapter.run_root = run_root
    adapter.child_state = child_state
    for item in owned:
        if (not isinstance(item, dict) or item.keys() != {"profile_name", "depth", "run_id"}
                or item.get("run_id") != run_id or not _safe_profile(item.get("profile_name"))
                or type(item.get("depth")) is not int or item["depth"] not in {1, 2}):
            raise SelfTestError("self-test owned-profile manifest is invalid")
        adapter.profiles[item["profile_name"]] = item["depth"]
        adapter.owned_profiles.append({"profile_name": item["profile_name"], "depth": item["depth"]})
    references = report.get("preserved_diagnostic_refs", [])
    if not isinstance(references, list):
        raise SelfTestError("self-test diagnostic manifest is invalid")
    diagnostics_preserved = not owned
    if references:
        diagnostics_preserved = True
        for reference in references:
            if (not isinstance(reference, dict) or reference.get("stage") != "expected_boot_failure"
                    or reference.get("path") != f"{run_id}-failure.json"):
                diagnostics_preserved = False
                break
            diagnostic_path = report_root / reference["path"]
            try:
                diagnostic_info = diagnostic_path.lstat()
                diagnostic = json.loads(diagnostic_path.read_text(encoding="ascii"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                diagnostics_preserved = False
                break
            if (not stat.S_ISREG(diagnostic_info.st_mode) or diagnostic_info.st_uid != os.getuid()
                    or diagnostic_info.st_nlink != 1 or stat.S_IMODE(diagnostic_info.st_mode) != 0o600
                    or diagnostic_info.st_size > 16 * 1024 or diagnostic.get("run_id") != run_id
                    or diagnostic.get("schema_version") != 1):
                diagnostics_preserved = False
                break
    deleted = cleanup_owned_profiles(
        adapter, run_id, owned, reports_preserved=True,
        diagnostics_preserved=diagnostics_preserved,
    )
    cleanup_result = _paths_module().recursion_run_delete(
        state_root, run_name, run_name, Path("/proc"),
    )
    report["cleanup"] = "PASS"
    report["stages"]["cleanup"] = {
        "status": "PASS", "elapsed_ms": 0, "cause_exit_code": 0,
        "deleted_profiles": deleted,
        "archived_reports": len(cleanup_result.get("archived_reports", [])),
    }
    _rewrite_private_report(report_path, report)
    return {"run_id": run_id, "cleanup": "PASS", "deleted_profiles": deleted,
            "report_path": str(report_path)}


class SelfTestRunner:
    """Execute a strict evidence state machine through a trusted adapter."""

    def __init__(
        self,
        source: str | os.PathLike[str],
        report_root: str | os.PathLike[str],
        adapter: SelfTestAdapter,
        *,
        max_depth: int = SUPPORTED_MAX_DEPTH,
        yes: bool = False,
        run_id: str | None = None,
        total_deadline_seconds: int = TOTAL_DEADLINE_SECONDS,
        clock=time.monotonic,
        report_writer=None,
    ):
        self.source_path = _canonical_directory(source)
        self.report_root = _canonical_directory(report_root)
        _private_directory(self.report_root)
        if (self.source_path == self.report_root or self.source_path in self.report_root.parents
                or self.report_root in self.source_path.parents):
            raise SelfTestError("source and self-test report state must be disjoint")
        if max_depth != SUPPORTED_MAX_DEPTH:
            raise SelfTestError("self-test requires --max-depth 2")
        self.max_depth = max_depth
        self.yes = bool(yes)
        self.adapter = adapter
        self.run_id = uuid.UUID(run_id).hex if run_id else uuid.uuid4().hex
        self.clock = clock
        if type(total_deadline_seconds) is not int or total_deadline_seconds < 0:
            raise SelfTestError("total deadline must be a nonnegative integer")
        self.total_deadline_seconds = total_deadline_seconds
        self.report_writer = report_writer or write_report

    def run(self) -> tuple[dict[str, Any], int]:
        started = self.clock()
        context: dict[str, Any] = {"run_id": self.run_id, "max_depth": self.max_depth}
        report: dict[str, Any] = {
            "schema_version": 1,
            "run_id": self.run_id,
            "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "evidence_level": (
                "FIXTURE_ONLY" if self.adapter.fixture_only else
                "LIVE_ADAPTER_UNAVAILABLE" if not getattr(self.adapter, "execution_enabled", True) else
                "LIVE_CANDIDATE"
            ),
            "authorization_yes": self.yes,
            "max_depth": self.max_depth,
            "parent_profile_name": getattr(self.adapter, "parent_profile", None),
            "deadlines_seconds": {
                "total": self.total_deadline_seconds,
                "boot_stage": BOOT_DEADLINE_SECONDS,
                "normal_stage": NORMAL_STAGE_DEADLINE_SECONDS,
                "cleanup": CLEANUP_DEADLINE_SECONDS,
            },
            "source": None,
            "provenance": {},
            "result": "BLOCKED",
            "e2e_self_status": "NOT_RUN",
            "cause_exit_code": 2,
            "stages": {
                name: {
                    "status": "NOT_RUN", "elapsed_ms": 0, "cause_exit_code": None,
                    "deadline_seconds": (
                        BOOT_DEADLINE_SECONDS if name in BOOT_STAGES else
                        CLEANUP_DEADLINE_SECONDS if name == "cleanup" else
                        NORMAL_STAGE_DEADLINE_SECONDS if name in STAGE_ORDER else 0
                    ),
                }
                for name in (*STAGE_ORDER, "cleanup", "report")
            },
            "owned_profiles": [],
            "preserved_diagnostic_refs": [],
        }
        try:
            manifest = _source_manifest(self.source_path)
            context["source"] = manifest
            report["source"] = {
                "source_head": manifest["source_head"],
                "tree_sha256": manifest["tree_sha256"],
                "manifest_sha256": _manifest_digest(manifest),
                "entry_count": manifest["entry_count"],
                "total_bytes": manifest["total_bytes"],
            }
        except KeyboardInterrupt:
            self._record(report, "preflight", "FAIL", 0, 130, "interrupted")
            return self._finish(report, started, 130)
        except SelfTestTermination as exc:
            reason = "interrupted" if exc.exit_code == 130 else "terminated"
            self._record(report, "preflight", "FAIL", 0, exc.exit_code, reason)
            return self._finish(report, started, exc.exit_code)
        except (OSError, SelfTestError, ValueError):
            self._record(report, "preflight", "BLOCKED", 0, 2, "environment_unavailable")
            return self._finish(report, started, 2)

        deadline = started + self.total_deadline_seconds
        for name in STAGE_ORDER:
            now = self.clock()
            if now >= deadline:
                self._record(report, name, "FAIL", 0, 124, "deadline_exceeded")
                return self._finish(report, started, 124)
            timeout = BOOT_DEADLINE_SECONDS if name in BOOT_STAGES else NORMAL_STAGE_DEADLINE_SECONDS
            timeout = min(timeout, max(1, int(deadline - now)))
            stage_started = now
            try:
                result = self.adapter.run_stage(name, {
                    "run_id": self.run_id,
                    "max_depth": self.max_depth,
                    "yes": self.yes,
                    "source": context["source"],
                    "deadline_monotonic": min(deadline, now + timeout),
                    "stage_timeout_seconds": timeout,
                    "previous": {key: context[key] for key in ("preflight", "broken_snapshot", "fixed_snapshot")
                                 if key in context},
                })
                elapsed = max(0, int((self.clock() - stage_started) * 1000))
                if self.clock() > min(deadline, stage_started + timeout):
                    raise TimeoutError
                self._sync_owned_profiles(report)
                status, evidence, cause, reason = self._parse_result(result)
                expected_failure = name == "expected_boot_failure"
                cause_valid = (type(cause) is int and cause > 0) if expected_failure else cause is None
                if status == "PASS" and (reason is not None or not cause_valid):
                    status, cause, reason, evidence = "FAIL", 1, "invalid_evidence", {}
                if status == "PASS" and not _stage_evidence_valid(name, evidence, context):
                    status, cause, reason = "FAIL", 1, "invalid_evidence"
                    evidence = {}
                elif status != "PASS":
                    # Failure diagnostics are represented only by stable codes;
                    # arbitrary adapter output is never copied into the report.
                    evidence = {}
                self._record(report, name, status, elapsed, cause, reason, evidence)
                if status != "PASS":
                    return self._finish(report, started, self._exit_for(status, cause))
                context[name] = evidence
                if name == "broken_snapshot":
                    pass
                elif name == "expected_boot_failure":
                    self._add_owned_profile(report, evidence["profile_name"], 1)
                elif name == "fixed_snapshot":
                    context["fixed_snapshot"] = evidence
                elif name == "fixed_boot":
                    self._add_owned_profile(report, evidence["profile_name"], 1)
                elif name == "depth":
                    self._add_owned_profile(report, evidence["g1_profile_name"], 1)
                    self._add_owned_profile(report, evidence["g2_profile_name"], 2)
                if name == "expected_boot_failure":
                    report["preserved_diagnostic_refs"] = [
                        {"stage": name, "path": value} for value in evidence["diagnostic_refs"]
                    ]
            except KeyboardInterrupt:
                elapsed = max(0, int((self.clock() - stage_started) * 1000))
                self._record(report, name, "FAIL", elapsed, 130, "interrupted")
                self._sync_owned_profiles(report)
                return self._finish(report, started, 130)
            except SelfTestTermination as exc:
                elapsed = max(0, int((self.clock() - stage_started) * 1000))
                reason = "interrupted" if exc.exit_code == 130 else "terminated"
                self._record(report, name, "FAIL", elapsed, exc.exit_code, reason)
                self._sync_owned_profiles(report)
                return self._finish(report, started, exc.exit_code)
            except TimeoutError:
                elapsed = max(0, int((self.clock() - stage_started) * 1000))
                self._record(report, name, "FAIL", elapsed, 124, "deadline_exceeded")
                self._sync_owned_profiles(report)
                return self._finish(report, started, 124)
            except LiveSelfTestBlocked:
                elapsed = max(0, int((self.clock() - stage_started) * 1000))
                self._record(report, name, "BLOCKED", elapsed, 2, "environment_unavailable")
                self._sync_owned_profiles(report)
                return self._finish(report, started, 2)
            except Exception:  # noqa: BLE001 - adapter failures must become a partial report.
                elapsed = max(0, int((self.clock() - stage_started) * 1000))
                self._record(report, name, "FAIL", elapsed, 1, "stage_failed")
                self._sync_owned_profiles(report)
                return self._finish(report, started, 1)
        return self._finish(report, started, 0)

    @staticmethod
    def _parse_result(result: object) -> tuple[str, dict[str, Any], int | None, str | None]:
        if not isinstance(result, dict) or not result.keys() <= {"status", "evidence", "cause_exit_code", "reason_code"}:
            return "FAIL", {}, 1, "invalid_evidence"
        status = result.get("status")
        if status not in STATUSES - {"NOT_RUN"}:
            return "FAIL", {}, 1, "invalid_evidence"
        evidence = result.get("evidence", {})
        cause = result.get("cause_exit_code")
        reason = result.get("reason_code")
        if cause is not None and (type(cause) is not int or not 0 <= cause <= 255):
            return "FAIL", {}, 1, "invalid_evidence"
        if reason is not None and reason not in REASON_CODES:
            return "FAIL", {}, 1, "invalid_evidence"
        if not isinstance(evidence, dict):
            return "FAIL", {}, 1, "invalid_evidence"
        if status == "BLOCKED" and reason is None:
            return "FAIL", {}, 1, "invalid_evidence"
        if status == "FAIL" and reason is None:
            reason = "stage_failed"
        return status, evidence, cause, reason

    @staticmethod
    def _record(report: dict[str, Any], name: str, status: str, elapsed: int,
                cause: int | None, reason: str | None, evidence: dict[str, Any] | None = None) -> None:
        if status not in STATUSES:
            status = "FAIL"
            cause = 1
            reason = "invalid_evidence"
            evidence = {}
        stage: dict[str, Any] = {"status": status, "elapsed_ms": elapsed, "cause_exit_code": cause}
        stage["deadline_seconds"] = (
            BOOT_DEADLINE_SECONDS if name in BOOT_STAGES else
            CLEANUP_DEADLINE_SECONDS if name == "cleanup" else
            NORMAL_STAGE_DEADLINE_SECONDS if name in STAGE_ORDER else 0
        )
        if reason:
            stage["reason_code"] = reason if reason in REASON_CODES else "stage_failed"
        if evidence:
            # Evidence has already passed stage-specific validation before being
            # stored; it contains identifiers, hashes and booleans, never raw output.
            stage["evidence"] = evidence
        report["stages"][name] = stage

    @staticmethod
    def _exit_for(status: str, cause: int | None) -> int:
        if status == "BLOCKED":
            return 2
        if cause in {124, 130, 143}:
            return cause
        return 1

    def _add_owned_profile(self, report: dict[str, Any], name: str, depth: int) -> None:
        item = {"profile_name": name, "depth": depth, "run_id": self.run_id}
        if item not in report["owned_profiles"]:
            report["owned_profiles"].append(item)

    def _sync_owned_profiles(self, report: dict[str, Any]) -> None:
        owned = getattr(self.adapter, "owned_profiles", None)
        if not isinstance(owned, list):
            return
        for item in owned:
            if not isinstance(item, dict) or set(item) != {"profile_name", "depth"}:
                continue
            name, depth = item["profile_name"], item["depth"]
            if (not _safe_profile(name) or not name.startswith(f"selftest-{self.run_id[:12]}-")
                    or type(depth) is not int or depth not in {1, 2}):
                continue
            self._add_owned_profile(report, name, depth)

    def _finish(self, report: dict[str, Any], started: float, exit_code: int) -> tuple[dict[str, Any], int]:
        elapsed = max(0, int((self.clock() - started) * 1000))
        report["elapsed_ms"] = elapsed
        failing = next((item for item in report["stages"].values() if item["status"] == "FAIL"), None)
        blocked = any(item["status"] == "BLOCKED" for item in report["stages"].values())
        if failing:
            report["result"] = "FAIL"
            report["cause_exit_code"] = failing.get("cause_exit_code") or 1
        elif blocked:
            report["result"] = "BLOCKED"
            report["cause_exit_code"] = 2
        elif exit_code == 0:
            report["result"] = "PASS"
            report["cause_exit_code"] = 0
        if self.adapter.fixture_only or not getattr(self.adapter, "execution_enabled", True):
            report["e2e_self_status"] = "NOT_RUN"
        elif report["result"] == "PASS":
            report["e2e_self_status"] = "PASS"
        else:
            report["e2e_self_status"] = report["result"]
        preflight = report["stages"]["preflight"].get("evidence", {})
        broken_snapshot = report["stages"]["broken_snapshot"].get("evidence", {})
        fixed_snapshot = report["stages"]["fixed_snapshot"].get("evidence", {})
        fixed_boot = report["stages"]["fixed_boot"].get("evidence", {})
        report["provenance"] = {
            "parent_image_sha256": preflight.get("parent_image_sha256"),
            "parent_runtime_payload_sha256": preflight.get("parent_runtime_payload_sha256"),
            "parent_opencode_version": preflight.get("parent_opencode_version"),
            "failed_candidate_tree_sha256": broken_snapshot.get("candidate_tree_sha256"),
            "failed_candidate_payload_sha256": broken_snapshot.get("candidate_payload_sha256"),
            "failed_manifest_sha256": broken_snapshot.get("manifest_sha256"),
            "failed_seed_sha256": report["stages"]["expected_boot_failure"].get("evidence", {}).get("seed_sha256"),
            "failed_bootstrap_sha256": broken_snapshot.get("bootstrap_sha256"),
            "failed_runner_sha256": broken_snapshot.get("runner_sha256"),
            "failed_runner_test_sha256": broken_snapshot.get("runner_test_sha256"),
            "fixed_candidate_tree_sha256": fixed_snapshot.get("candidate_tree_sha256"),
            "fixed_candidate_payload_sha256": fixed_snapshot.get("candidate_payload_sha256"),
            "fixed_manifest_sha256": fixed_snapshot.get("manifest_sha256"),
            "fixed_seed_sha256": fixed_boot.get("seed_sha256"),
            "fixed_bootstrap_sha256": fixed_snapshot.get("bootstrap_sha256"),
            "fixed_runner_sha256": fixed_snapshot.get("runner_sha256"),
            "fixed_runner_test_sha256": fixed_snapshot.get("runner_test_sha256"),
            "candidate_image_sha256": fixed_boot.get("image_sha256"),
            "candidate_guest_payload_sha256": fixed_boot.get("guest_payload_sha256"),
            "candidate_opencode_version": fixed_boot.get("opencode_version"),
        }
        report["report_ref"] = f"{self.run_id}.json"
        report["stages"]["report"] = {"status": "PASS", "elapsed_ms": 0, "cause_exit_code": None}
        try:
            path = self.report_writer(self.report_root, self.run_id, report)
            report["report_path"] = str(path)
        except Exception:  # noqa: BLE001 - report persistence failure must change the result.
            report["stages"]["report"] = {
                "status": "FAIL", "elapsed_ms": 0, "cause_exit_code": 1,
                "reason_code": "report_write_failed",
            }
            report["result"] = "FAIL"
            report["e2e_self_status"] = "NOT_RUN"
            report["cause_exit_code"] = 1
            return report, 1
        return report, exit_code


def write_report(report_root: Path, run_id: str, report: dict[str, Any]) -> Path:
    """Atomically create one bounded 0600 report without replacing prior evidence."""
    _private_directory(report_root)
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise SelfTestError("report run ID is invalid")
    destination = report_root / f"{run_id}.json"
    if destination.exists() or destination.is_symlink():
        raise SelfTestError("report already exists")
    payload = (json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")
    if len(payload) > MAX_REPORT_BYTES:
        raise SelfTestError("report exceeds its bounded size")
    fd, temporary_name = tempfile.mkstemp(prefix=".vm-self-test-report-", dir=report_root)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, destination, follow_symlinks=False)
        temporary.unlink()
        directory_fd = os.open(report_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def _read_regular(path: Path, *, maximum: int = MAX_FIXTURE_FILE_BYTES) -> bytes:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.getuid() or before.st_size > maximum):
            raise SelfTestError("fixture source file is unsafe")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        data = b"".join(chunks)
        signature = lambda item: (item.st_dev, item.st_ino, item.st_mode, item.st_nlink,
                                  item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if len(data) > maximum or signature(before) != signature(after) or len(data) != before.st_size:
            raise SelfTestError("fixture source changed while being read")
        return data
    finally:
        os.close(fd)


def _write_preserving_mode(path: Path, payload: bytes) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
        raise SelfTestError("fixture target file is unsafe")
    mode = stat.S_IMODE(info.st_mode)
    fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise SelfTestError("fixture target changed before write")
        os.ftruncate(fd, 0)
        view = memoryview(payload)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise SelfTestError("fixture write failed")
            view = view[count:]
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_workspace(source: Path, destination: Path) -> None:
    source = _canonical_directory(source)
    parent = _canonical_directory(destination.parent)
    _private_directory(parent)
    if (destination.exists() or destination.is_symlink() or source == destination
            or source in destination.parents or destination in source.parents):
        raise SelfTestError("fixture destination must be new and disjoint")
    try:
        shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)
    except BaseException:
        if destination.exists() and not destination.is_symlink():
            shutil.rmtree(destination)
        raise


def _mutated_tree_digest(workspace: Path) -> str:
    manifest, _ = _snapshot_module().inventory(workspace, MAX_SNAPSHOT_BYTES)
    return str(manifest["tree_sha256"])


def _bootstrap_prefix(sentinel: str, *, fail: bool) -> str:
    if sentinel not in {"A", "B"}:
        raise SelfTestError("invalid fixture sentinel")
    suffix = "exit 42\n" if fail else ""
    return (
        FIXTURE_BEGIN
        + 'VM_SELF_TEST_GUEST_ROOT="${VM_SELF_TEST_GUEST_ROOT:-/var/lib/little-mester}"\n'
        + 'mkdir -p -- "$VM_SELF_TEST_GUEST_ROOT"\n'
        + f"printf '%s\\n' '{sentinel}' > \"$VM_SELF_TEST_GUEST_ROOT/{FIXTURE_SENTINEL}\"\n"
        + suffix
        + FIXTURE_END
    )


def _insert_bootstrap_prefix(workspace: Path, sentinel: str, *, fail: bool) -> None:
    path = workspace / FIXTURE_BOOTSTRAP
    payload = _read_regular(path)
    first_line, separator, body = payload.partition(b"\n")
    if not separator or not first_line.startswith(b"#!") or FIXTURE_BEGIN.encode() in payload:
        raise SelfTestError("bootstrap fixture insertion point is invalid")
    changed = first_line + b"\n" + _bootstrap_prefix(sentinel, fail=fail).encode("ascii") + body
    _write_preserving_mode(path, changed)


def _set_one_vcpu(workspace: Path) -> None:
    for relative, expected in ((FIXTURE_RUNNER, b"-smp 2"), (FIXTURE_RUNNER_TEST, b"-smp 2")):
        path = workspace / relative
        payload = _read_regular(path)
        if payload.count(expected) != 1:
            raise SelfTestError("fixture vCPU assertion is missing or ambiguous")
        _write_preserving_mode(path, payload.replace(expected, b"-smp 1", 1))


def _load_snapshot_provenance(workspace: Path) -> dict[str, Any]:
    bundle = workspace.parent
    verified = _snapshot_module().verify_bundle(str(bundle))
    if Path(verified["workspace"]) != workspace:
        raise SelfTestError("fixture source is not the exact verified snapshot")
    manifest_path = bundle / "source-manifest.json"
    value = json.loads(_read_regular(manifest_path, maximum=32 * 1024 * 1024).decode("ascii"))
    source = value["source_manifest"]
    return {
        "source_head": source["source_head"],
        "base_source_tree_sha256": source["tree_sha256"],
        "manifest_sha256": _manifest_digest(source),
    }


def _snapshot_fixture_copy(source: Path, run_root: Path) -> dict[str, Any]:
    _private_directory(run_root)
    result = _snapshot_module().prepare(source, run_root, MAX_SNAPSHOT_BYTES)
    bundle = Path(result["bundle"])
    metadata = json.loads(_read_regular(bundle / "source-manifest.json", maximum=32 * 1024 * 1024).decode("ascii"))
    return {
        "bundle": str(bundle),
        "workspace": result["workspace"],
        "candidate_tree_sha256": result["tree_sha256"],
        "manifest_sha256": _manifest_digest(metadata["source_manifest"]),
    }


def create_failure_fixture_copy(workspace: str | os.PathLike[str], destination: str | os.PathLike[str]) -> dict[str, Any]:
    """Make full filtered-source COPY A: guest sentinel A, exit 42, QEMU -smp 1."""
    source = _canonical_directory(workspace)
    provenance = _load_snapshot_provenance(source)
    target = Path(destination)
    if not target.is_absolute() or ".." in target.parts or target.resolve(strict=False) != target:
        raise SelfTestError("fixture destination must be canonical and absolute")
    run_root = target.parent / f".{target.name}-snapshot-run"
    if run_root.exists() or run_root.is_symlink():
        raise SelfTestError("fixture snapshot run directory must be new")
    _copy_workspace(source, target)
    run_root_created = False
    try:
        _insert_bootstrap_prefix(target, "A", fail=True)
        _set_one_vcpu(target)
        run_root.mkdir(mode=0o700)
        os.chmod(run_root, 0o700)
        run_root_created = True
        fresh = _snapshot_fixture_copy(target, run_root)
        source_after = _load_snapshot_provenance(source)
        if source_after != provenance:
            raise SelfTestError("verified source snapshot changed during fixture copy")
        return {
            **provenance,
            **fresh,
            "bootstrap_sha256": hashlib.sha256(_read_regular(Path(fresh["workspace"]) / FIXTURE_BOOTSTRAP)).hexdigest(),
            "runner_sha256": hashlib.sha256(_read_regular(Path(fresh["workspace"]) / FIXTURE_RUNNER)).hexdigest(),
            "runner_test_sha256": hashlib.sha256(_read_regular(Path(fresh["workspace"]) / FIXTURE_RUNNER_TEST)).hexdigest(),
        }
    except BaseException:
        if target.exists() and not target.is_symlink():
            shutil.rmtree(target)
        if run_root_created and run_root.exists() and not run_root.is_symlink():
            shutil.rmtree(run_root)
        raise


def create_fixed_fixture_copy(failed_workspace: str | os.PathLike[str], destination: str | os.PathLike[str],
                              failure_provenance: dict[str, Any]) -> dict[str, Any]:
    """Fix only a new copy of A: sentinel B, no injected exit, fresh tree/seed input."""
    source = _canonical_directory(failed_workspace)
    target = Path(destination)
    if not target.is_absolute() or ".." in target.parts or target.resolve(strict=False) != target:
        raise SelfTestError("fixture destination must be canonical and absolute")
    if (not _head(failure_provenance.get("source_head"))
            or not _hash(failure_provenance.get("base_source_tree_sha256"))
            or not _hash(failure_provenance.get("candidate_tree_sha256"))
            or not _hash(failure_provenance.get("manifest_sha256"))):
        raise SelfTestError("failure provenance is invalid")
    source_before = _mutated_tree_digest(source)
    if source_before != failure_provenance["candidate_tree_sha256"]:
        raise SelfTestError("failed candidate changed before fix copy")
    run_root = target.parent / f".{target.name}-snapshot-run"
    if run_root.exists() or run_root.is_symlink():
        raise SelfTestError("fixture snapshot run directory must be new")
    _copy_workspace(source, target)
    run_root_created = False
    try:
        path = target / FIXTURE_BOOTSTRAP
        payload = _read_regular(path)
        begin = FIXTURE_BEGIN.encode("ascii")
        end = FIXTURE_END.encode("ascii")
        if payload.count(begin) != 1 or payload.count(end) != 1:
            raise SelfTestError("failed fixture prefix is missing or ambiguous")
        start = payload.index(begin)
        finish = payload.index(end, start) + len(end)
        if b"printf '%s\\n' 'A'" not in payload[start:finish] or b"exit 42\n" not in payload[start:finish]:
            raise SelfTestError("failed fixture is not the expected A/42 injection")
        _write_preserving_mode(path, payload[:start] + _bootstrap_prefix("B", fail=False).encode("ascii") + payload[finish:])
        current_tree = _mutated_tree_digest(target)
        if current_tree == failure_provenance["candidate_tree_sha256"]:
            raise SelfTestError("fixture fix did not produce a fresh source tree")
        run_root.mkdir(mode=0o700)
        os.chmod(run_root, 0o700)
        run_root_created = True
        fresh = _snapshot_fixture_copy(target, run_root)
        if _mutated_tree_digest(source) != source_before:
            raise SelfTestError("failed candidate changed during fix copy")
        return {
            "source_head": failure_provenance["source_head"],
            "base_source_tree_sha256": failure_provenance["base_source_tree_sha256"],
            **fresh,
            "bootstrap_sha256": hashlib.sha256(_read_regular(Path(fresh["workspace"]) / FIXTURE_BOOTSTRAP)).hexdigest(),
            "runner_sha256": hashlib.sha256(_read_regular(Path(fresh["workspace"]) / FIXTURE_RUNNER)).hexdigest(),
            "runner_test_sha256": hashlib.sha256(_read_regular(Path(fresh["workspace"]) / FIXTURE_RUNNER_TEST)).hexdigest(),
        }
    except BaseException:
        if target.exists() and not target.is_symlink():
            shutil.rmtree(target)
        if run_root_created and run_root.exists() and not run_root.is_symlink():
            shutil.rmtree(run_root)
        raise


def cleanup_owned_profiles(adapter: Any, run_id: str, owned_profiles: list[dict[str, Any]],
                           *, reports_preserved: bool, diagnostics_preserved: bool) -> list[str]:
    """Gracefully clean only manifest-owned profiles, deepest-first, by exact name."""
    if (not reports_preserved or not diagnostics_preserved
            or not re.fullmatch(r"[a-f0-9]{32}", run_id)):
        raise SelfTestError("exact-run cleanup requires preserved reports, diagnostics, and a valid run ID")
    seen: set[str] = set()
    checked: list[dict[str, Any]] = []
    for item in owned_profiles:
        if not isinstance(item, dict) or item.keys() != {"profile_name", "depth", "run_id"}:
            raise SelfTestError("owned profile manifest is invalid")
        name, depth, owner = item["profile_name"], item["depth"], item["run_id"]
        if (not _safe_profile(name) or not name.startswith(f"selftest-{run_id[:12]}-")
                or name in seen or type(depth) is not int or not 1 <= depth <= SUPPORTED_MAX_DEPTH
                or owner != run_id or (depth == 1 and not name.endswith(("-bad1", "-fix1")))
                or (depth == 2 and not name.endswith("-fix2"))):
            raise SelfTestError("cleanup target is outside this exact self-test run")
        seen.add(name)
        checked.append(item)
    deleted: list[str] = []
    for item in sorted(checked, key=lambda value: (-value["depth"], value["profile_name"])):
        name = item["profile_name"]
        if adapter.suspend_exact(name) is not True:
            raise SelfTestError("owned profile did not stop gracefully")
        if adapter.delete_exact(name, confirm_name=name, run_id=run_id) is not True:
            raise SelfTestError("exact owned profile cleanup was not confirmed")
        deleted.append(name)
    return deleted


def _report_root_from_state(state_root_value: str) -> Path:
    paths = _paths_module()
    root = paths.state_root(state_root_value, must_exist=True, scan=True)
    destination = paths.ensure_private_root(root / "vm" / "self-test-reports")
    _private_directory(destination)
    return destination


def _guest_identity_matches(environment: dict[str, str], path: Path = Path("/etc/lab-vm-recursion")) -> bool:
    depth = environment.get("LAB_VM_RECURSION_DEPTH")
    maximum = environment.get("LAB_VM_MAX_DEPTH")
    if depth is None or maximum is None or not re.fullmatch(r"0|[1-9][0-9]?", depth) \
            or not re.fullmatch(r"0|[1-9][0-9]?", maximum):
        return False
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.getuid()}
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o022 or info.st_size > 128):
            return False
        match = re.fullmatch(r"(0|[1-9][0-9]?) (0|[1-9][0-9]?)\n?", path.read_text(encoding="ascii"))
        return bool(match and match.groups() == (depth, maximum) and int(depth) <= int(maximum) <= 32)
    except (OSError, UnicodeError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", help="canonical self-write checkout to snapshot")
    parser.add_argument("--max-depth", type=int, default=SUPPORTED_MAX_DEPTH,
                        help="self-test recursion cap (must be 2; default: 2)")
    parser.add_argument("--yes", action="store_true", help="authorize effects for this generated run only")
    parser.add_argument("--state-root", help="exact private state root (defaults to MESTER_STATE_DIR)")
    parser.add_argument("--parent-profile", help="exact ready depth-0 control profile for --live")
    parser.add_argument("--run-id", help="32 lowercase hexadecimal evidence ID")
    parser.add_argument("--confirm-run-id", help="repeat --run-id to authorize live execution or cleanup")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--live", action="store_true", help="run the explicit nested-VM live adapter")
    modes.add_argument("--cleanup", action="store_true", help="clean only the exact run in its saved ownership manifest")
    modes.add_argument("--nested-depth-check", action="store_true", help=argparse.SUPPRESS)
    modes.add_argument("--nested-action", choices=("suspend", "delete"), help=argparse.SUPPRESS)
    parser.add_argument("--profile", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.max_depth != SUPPORTED_MAX_DEPTH:
        parser.error("--max-depth must be 2 for the G1/G2/G3 self-test")

    if args.nested_depth_check:
        if not args.source or not args.yes or not args.run_id or not args.confirm_run_id:
            parser.error("nested depth check requires --source, --yes, --run-id and --confirm-run-id")
        try:
            result = _nested_depth_check_local(args.source, args.run_id, args.confirm_run_id)
        except (OSError, SelfTestError, LiveSelfTestBlocked, ValueError, RuntimeError) as error:
            print(f"BLOCKED: nested depth check unavailable ({type(error).__name__}).", file=sys.stderr)
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0

    if args.nested_action:
        if not args.profile or not args.run_id or not args.confirm_run_id:
            parser.error("nested lifecycle action requires --profile, --run-id and --confirm-run-id")
        try:
            result = _nested_action_local(args.nested_action, args.profile, args.run_id, args.confirm_run_id)
        except (OSError, SelfTestError, LiveSelfTestBlocked, ValueError, RuntimeError) as error:
            print(f"BLOCKED: nested cleanup unavailable ({type(error).__name__}).", file=sys.stderr)
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0

    state_root = args.state_root or os.environ.get("MESTER_STATE_DIR")
    if not state_root:
        print("BLOCKED: a private in-guest MESTER_STATE_DIR is required; no state was changed.", file=sys.stderr)
        return 2
    try:
        if args.cleanup:
            if not args.run_id or not args.confirm_run_id:
                parser.error("cleanup requires --run-id and --confirm-run-id")
            result = cleanup_live_self_test(state_root, args.run_id, args.confirm_run_id)
            print(json.dumps(result, sort_keys=True))
            return 0
        if not args.source:
            parser.error("--source is required for a self-test run")
        source = _canonical_directory(args.source)
        report_root = _report_root_from_state(state_root)
        if args.live:
            if not args.yes or not args.run_id or args.run_id != args.confirm_run_id:
                parser.error("--live requires --yes and the exact matching --run-id/--confirm-run-id")
            if not args.parent_profile:
                parser.error("--live requires an exact --parent-profile")
            if not re.fullmatch(r"[a-f0-9]{32}", args.run_id):
                parser.error("--run-id must be 32 lowercase hexadecimal characters")
            adapter: Any = LiveSelfTestAdapter(
                source, _canonical_directory(state_root), report_root, args.run_id,
                parent_profile=args.parent_profile,
            )
        elif not _guest_identity_matches(os.environ):
            adapter = UnavailableLiveAdapter("environment_unavailable")
        else:
            adapter = UnavailableLiveAdapter()
        runner = SelfTestRunner(
            source, report_root, adapter, max_depth=args.max_depth, yes=args.yes,
            run_id=args.run_id,
        )
        previous_handlers = {
            signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
        }

        def terminate(signum: int, _frame: object) -> None:
            raise SelfTestTermination(130 if signum == signal.SIGINT else 143)

        try:
            for signum in previous_handlers:
                signal.signal(signum, terminate)
            report, result = runner.run()
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
    except (OSError, SelfTestError, ValueError):
        print("BLOCKED: self-test preflight or private report setup failed; no VM was started.", file=sys.stderr)
        return 2
    print(json.dumps({
        "run_id": report["run_id"], "result": report["result"],
        "e2e_self_status": report["e2e_self_status"], "report_path": report.get("report_path"),
        "cleanup_command": (
            f"python3 scripts/vm-self-test.py --cleanup --run-id {report['run_id']} "
            f"--confirm-run-id {report['run_id']}"
            if getattr(adapter, "execution_enabled", False) else None
        ),
    }, sort_keys=True))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
