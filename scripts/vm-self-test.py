#!/usr/bin/env python3
"""Bounded, evidence-gated driver for changed-VM self-development tests.

The live runtime adapter is intentionally unavailable until the TASK-15 handoff.
The state machine and disposable source mutations are fixture-testable meanwhile.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import stat
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
                         "seed_sha256", "bootstrap_sha256", "runner_sha256", "runner_test_sha256"},
    "expected_boot_failure": {"profile_name", "source_head", "candidate_tree_sha256", "manifest_sha256",
                              "seed_sha256", "bootstrap_sha256", "runner_sha256", "runner_test_sha256",
                              "depth", "failure_stage", "observed_exit_code",
                              "sentinel", "qemu_vcpus", "effective_qemu_argv_sha256",
                              "failure_evidence_preserved", "diagnostic_refs"},
    "fixed_snapshot": {"profile_name", "source_head", "base_source_tree_sha256",
                       "candidate_tree_sha256", "candidate_payload_sha256", "manifest_sha256",
                       "seed_sha256", "bootstrap_sha256", "runner_sha256", "runner_test_sha256"},
    "fixed_boot": {"profile_name", "ready", "depth", "max_depth", "sentinel", "qemu_vcpus",
                   "guest_vcpus", "seed_sha256", "image_sha256", "opencode_version",
                   "manifest_sha256", "bootstrap_sha256", "runner_sha256", "runner_test_sha256",
                   "guest_payload_sha256", "guest_source_tree_sha256"},
    "source_parity": {"source_head", "guest_source_tree_sha256", "test_source_tree_sha256",
                      "expected_payload_sha256", "installed_payload_sha256"},
    "candidate_tests": {"source_tree_sha256", "exit_code", "tests_passed", "tests_failed"},
    "workload": {"harmless_workload_observed", "workload_exit_code"},
    "boundary": {"restricted_network_observed", "direct_egress_blocked", "ancestor_capabilities_absent"},
    "depth": {"g1_profile_name", "g2_profile_name", "g1_depth", "g2_depth", "max_depth",
              "g3_refused", "refused_before_state", "refused_before_qemu", "parent_chain_verified"},
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
            and evidence["parent_source_tree_sha256"] != source["tree_sha256"]
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
            and _hash(evidence.get("seed_sha256"))
            and _hash(evidence.get("bootstrap_sha256"))
            and _hash(evidence.get("runner_sha256"))
            and _hash(evidence.get("runner_test_sha256"))
        )
    if name == "expected_boot_failure":
        return (
            evidence.get("profile_name") == expected_profile[name]
            and evidence.get("source_head") == source["source_head"]
            and evidence.get("candidate_tree_sha256") == broken.get("candidate_tree_sha256")
            and evidence.get("manifest_sha256") == broken.get("manifest_sha256")
            and evidence.get("seed_sha256") == broken.get("seed_sha256")
            and evidence.get("bootstrap_sha256") == broken.get("bootstrap_sha256")
            and evidence.get("runner_sha256") == broken.get("runner_sha256")
            and evidence.get("runner_test_sha256") == broken.get("runner_test_sha256")
            and type(evidence.get("depth")) is int and evidence["depth"] == 1
            and evidence.get("failure_stage") == "guest_provisioning"
            and type(evidence.get("observed_exit_code")) is int and evidence["observed_exit_code"] == 42
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
            and _hash(evidence.get("seed_sha256"))
            and evidence.get("seed_sha256") != broken.get("seed_sha256")
            and _hash(evidence.get("bootstrap_sha256"))
            and evidence.get("bootstrap_sha256") != broken.get("bootstrap_sha256")
            and _hash(evidence.get("runner_sha256"))
            and evidence.get("runner_sha256") == broken.get("runner_sha256")
            and _hash(evidence.get("runner_test_sha256"))
            and evidence.get("runner_test_sha256") == broken.get("runner_test_sha256")
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
            and evidence.get("seed_sha256") == fixed.get("seed_sha256")
            and evidence.get("manifest_sha256") == fixed.get("manifest_sha256")
            and evidence.get("bootstrap_sha256") == fixed.get("bootstrap_sha256")
            and evidence.get("runner_sha256") == fixed.get("runner_sha256")
            and evidence.get("runner_test_sha256") == fixed.get("runner_test_sha256")
            and _hash(evidence.get("image_sha256"))
            and _opencode_version(evidence.get("opencode_version"))
            and evidence.get("guest_payload_sha256") == fixed.get("candidate_payload_sha256")
            and evidence.get("guest_source_tree_sha256") == fixed.get("candidate_tree_sha256")
        )
    if name == "source_parity":
        return (
            evidence.get("source_head") == source["source_head"]
            and evidence.get("guest_source_tree_sha256") == fixed.get("candidate_tree_sha256")
            and evidence.get("test_source_tree_sha256") == fixed.get("candidate_tree_sha256")
            and _hash(evidence.get("expected_payload_sha256"))
            and evidence.get("expected_payload_sha256") == context.get("fixed_boot", {}).get("guest_payload_sha256")
            and evidence.get("installed_payload_sha256") == evidence.get("expected_payload_sha256")
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
    """Fail-closed until an owner-approved frozen-runtime adapter is available."""

    fixture_only = False
    execution_enabled = False

    def __init__(self, reason_code: str = "backend_unavailable"):
        self.reason_code = reason_code

    def run_stage(self, name: str, context: dict[str, Any]) -> dict[str, Any]:
        del name, context
        return {"status": "BLOCKED", "reason_code": self.reason_code}


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
                status, evidence, cause, reason = self._parse_result(result)
                expected_cause = 42 if name == "expected_boot_failure" else None
                if status == "PASS" and (reason is not None or cause != expected_cause):
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
                    self._add_owned_profile(report, evidence["profile_name"], 1)
                elif name == "fixed_snapshot":
                    context["fixed_snapshot"] = evidence
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
                return self._finish(report, started, 130)
            except SelfTestTermination as exc:
                elapsed = max(0, int((self.clock() - stage_started) * 1000))
                reason = "interrupted" if exc.exit_code == 130 else "terminated"
                self._record(report, name, "FAIL", elapsed, exc.exit_code, reason)
                return self._finish(report, started, exc.exit_code)
            except TimeoutError:
                elapsed = max(0, int((self.clock() - stage_started) * 1000))
                self._record(report, name, "FAIL", elapsed, 124, "deadline_exceeded")
                return self._finish(report, started, 124)
            except Exception:  # noqa: BLE001 - adapter failures must become a partial report.
                elapsed = max(0, int((self.clock() - stage_started) * 1000))
                self._record(report, name, "FAIL", elapsed, 1, "stage_failed")
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
            "failed_seed_sha256": broken_snapshot.get("seed_sha256"),
            "failed_bootstrap_sha256": broken_snapshot.get("bootstrap_sha256"),
            "failed_runner_sha256": broken_snapshot.get("runner_sha256"),
            "failed_runner_test_sha256": broken_snapshot.get("runner_test_sha256"),
            "fixed_candidate_tree_sha256": fixed_snapshot.get("candidate_tree_sha256"),
            "fixed_candidate_payload_sha256": fixed_snapshot.get("candidate_payload_sha256"),
            "fixed_manifest_sha256": fixed_snapshot.get("manifest_sha256"),
            "fixed_seed_sha256": fixed_snapshot.get("seed_sha256"),
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
    parser.add_argument("--source", required=True, help="canonical self-write checkout to snapshot")
    parser.add_argument("--max-depth", type=int, required=True, help="self-test recursion cap (must be 2)")
    parser.add_argument("--yes", action="store_true", help="authorize effects for this generated run only")
    args = parser.parse_args(argv)
    if args.max_depth != SUPPORTED_MAX_DEPTH:
        parser.error("--max-depth must be 2 for the G1/G2/G3 self-test")
    state_root = os.environ.get("MESTER_STATE_DIR")
    if not state_root:
        print("BLOCKED: a private in-guest MESTER_STATE_DIR is required; no state was changed.", file=sys.stderr)
        return 2
    try:
        report_root = _report_root_from_state(state_root)
        if not _guest_identity_matches(os.environ):
            adapter = UnavailableLiveAdapter("environment_unavailable")
        else:
            # The live controller is deliberately not wired to mutable source or
            # shell commands. TASK-15 installs an owner-approved frozen adapter.
            adapter = UnavailableLiveAdapter()
        runner = SelfTestRunner(args.source, report_root, adapter, max_depth=args.max_depth, yes=args.yes)
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
    }, sort_keys=True))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
