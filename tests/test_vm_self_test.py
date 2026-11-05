"""Fixture-only policy, evidence and fail→fix coverage for vm-self-test.py."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts/vm-self-test.py"
FIXTURE_CONTRACT = ROOT / "tests/fixtures/vmselftest/mutation-contract.json"
SPEC = importlib.util.spec_from_file_location("vm_self_test", SCRIPT_PATH)
self_test = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(self_test)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


class FixtureAdapter:
    fixture_only = True

    def __init__(self, changes: dict[str, object] | None = None):
        self.changes = changes or {}
        self.calls: list[str] = []

    def run_stage(self, name: str, context: dict):
        self.calls.append(name)
        change = self.changes.get(name)
        if isinstance(change, BaseException):
            raise change
        if change is not None:
            return change
        source = context["source"]
        run_id = context["run_id"]
        bad_profile = self_test._expected_profile(run_id, "bad1")
        fix_profile = self_test._expected_profile(run_id, "fix1")
        broken_tree = digest("candidate-A")
        fixed_tree = digest("candidate-B")
        payload = digest("guest-payload")
        base_lifecycle = digest("base-lifecycle")
        candidate_lifecycle = digest("candidate-lifecycle")
        common_snapshot = {
            "source_head": source["source_head"],
            "base_source_tree_sha256": source["tree_sha256"],
        }
        evidence = {
            "preflight": {
                "parent_identity_verified": True, "current_depth": 0, "max_depth": 2,
                "kvm_usable": True, "cpu_virtualization": True, "ram_budget_passed": True,
                "disk_budget_passed": True, "parent_source_tree_sha256": digest("parent-runtime"),
                "parent_image_sha256": digest("parent-image"),
                "parent_runtime_payload_sha256": digest("parent-runtime-payload"),
                "parent_opencode_version": "2.1.0",
            },
            "broken_snapshot": {
                **common_snapshot, "profile_name": bad_profile,
                "candidate_tree_sha256": broken_tree, "candidate_payload_sha256": digest("payload-A"),
                "manifest_sha256": digest("manifest-A"),
                "bootstrap_sha256": digest("bootstrap-A"),
                "runner_sha256": digest("runner-A"), "runner_test_sha256": digest("runner-test-A"),
                "base_lifecycle_sha256": base_lifecycle,
                "candidate_lifecycle_sha256": candidate_lifecycle,
            },
            "expected_boot_failure": {
                "profile_name": bad_profile, "source_head": source["source_head"],
                "candidate_tree_sha256": broken_tree, "manifest_sha256": digest("manifest-A"),
                "seed_sha256": digest("seed-A"), "bootstrap_sha256": digest("bootstrap-A"),
                "runner_sha256": digest("runner-A"), "runner_test_sha256": digest("runner-test-A"),
                "candidate_lifecycle_sha256": candidate_lifecycle,
                "depth": 1, "failure_stage": "guest_provisioning",
                "observed_exit_code": 42, "sentinel": "A", "qemu_vcpus": 1,
                "effective_qemu_argv_sha256": digest("qemu-smp-1"),
                "failure_evidence_preserved": True,
                "diagnostic_refs": [f"{run_id}-failure.json"],
            },
            "fixed_snapshot": {
                **common_snapshot, "profile_name": fix_profile,
                "candidate_tree_sha256": fixed_tree, "candidate_payload_sha256": payload,
                "manifest_sha256": digest("manifest-B"),
                "bootstrap_sha256": digest("bootstrap-B"),
                "runner_sha256": digest("runner-A"), "runner_test_sha256": digest("runner-test-A"),
                "base_lifecycle_sha256": base_lifecycle,
                "candidate_lifecycle_sha256": candidate_lifecycle,
            },
            "fixed_boot": {
                "profile_name": fix_profile, "ready": True, "depth": 1, "max_depth": 2,
                "sentinel": "B", "qemu_vcpus": 1, "guest_vcpus": 1,
                "seed_sha256": digest("seed-B"), "guest_payload_sha256": payload,
                "image_sha256": digest("candidate-image"), "opencode_version": "2.1.0",
                "manifest_sha256": digest("manifest-B"), "bootstrap_sha256": digest("bootstrap-B"),
                "runner_sha256": digest("runner-A"), "runner_test_sha256": digest("runner-test-A"),
                "guest_source_tree_sha256": fixed_tree,
                "candidate_lifecycle_sha256": candidate_lifecycle,
                "installed_lifecycle_sha256": candidate_lifecycle,
            },
            "source_parity": {
                "source_head": source["source_head"], "guest_source_tree_sha256": fixed_tree,
                "test_source_tree_sha256": fixed_tree, "expected_payload_sha256": payload,
                "installed_payload_sha256": payload,
                "guest_lifecycle_sha256": candidate_lifecycle,
            },
            "candidate_tests": {
                "source_tree_sha256": fixed_tree, "exit_code": 0, "tests_passed": 3, "tests_failed": 0,
            },
            "workload": {"harmless_workload_observed": True, "workload_exit_code": 0},
            "boundary": {
                "restricted_network_observed": True, "direct_egress_blocked": True,
                "ancestor_capabilities_absent": True,
            },
            "depth": {
                "g1_profile_name": fix_profile,
                "g2_profile_name": self_test._expected_profile(run_id, "fix2"),
                "g1_depth": 1, "g2_depth": 2, "max_depth": 2, "g3_refused": True,
                "refused_before_state": True, "refused_before_qemu": True,
                "parent_chain_verified": True, "reset_refused": True,
                "increase_refused": True, "missing_kvm_refused": True,
                "candidate_lifecycle_sha256": candidate_lifecycle,
                "g2_runtime_payload_sha256": digest("g2-runtime-payload"),
                "g2_installed_lifecycle_sha256": candidate_lifecycle,
                "lifecycle_mutation_observed": True,
            },
            "agent_task": {
                "agent_completed": True, "task_id": "harmless-file-test",
                "result_sha256": digest("agent-result"),
            },
            "parent_continuity": {
                "parent_session_alive": True, "source_mount_readable": True,
                "parent_workload_alive": True,
            },
        }[name]
        return {"status": "PASS", "cause_exit_code": 42 if name == "expected_boot_failure" else None,
                "evidence": evidence}


class CleanupAdapter:
    def __init__(self):
        self.calls = []

    def suspend_exact(self, name: str):
        self.calls.append(("suspend", name))
        return True

    def delete_exact(self, name: str, *, confirm_name: str, run_id: str):
        self.calls.append(("delete", name, confirm_name, run_id))
        return True


class SelfTestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lm-vm-self-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        (self.source / "src").mkdir(mode=0o755, parents=True)
        (self.source / "src/fixture.txt").write_text("tracked source\n", encoding="ascii")
        self.git(self.source, "init", "--quiet", "--template=/dev/null")
        self.git(self.source, "config", "user.name", "Self Test Fixture")
        self.git(self.source, "config", "user.email", "self-test@localhost.invalid")
        self.git(self.source, "add", "--", "src/fixture.txt")
        self.git(self.source, "commit", "--quiet", "-m", "fixture baseline")
        self.report_root = self.base / "reports"
        self.report_root.mkdir(mode=0o700)

    @staticmethod
    def git(*args):
        return subprocess.run(["git", "-C", str(args[0]), *args[1:]], check=True,
                              capture_output=True, text=True,
                              env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})

    def new_runner(self, adapter=None, *, run_id=None, report_writer=None, total_deadline=3600):
        return self_test.SelfTestRunner(
            self.source, self.report_root, adapter or FixtureAdapter(), max_depth=2,
            yes=True, run_id=run_id, report_writer=report_writer,
            total_deadline_seconds=total_deadline,
        )

    def test_full_fixture_pipeline_is_not_reported_as_live_acceptance(self):
        runner = self.new_runner(run_id="12345678123456781234567812345678")
        report, status = runner.run()
        self.assertEqual(status, 0)
        self.assertEqual(report["result"], "PASS")
        self.assertEqual(report["evidence_level"], "FIXTURE_ONLY")
        self.assertEqual(report["e2e_self_status"], "NOT_RUN")
        self.assertEqual(runner.adapter.calls, list(self_test.STAGE_ORDER))
        self.assertEqual(report["stages"]["expected_boot_failure"]["cause_exit_code"], 42)
        self.assertEqual(report["deadlines_seconds"]["total"], 3600)
        self.assertEqual(report["stages"]["fixed_boot"]["deadline_seconds"], 1800)
        self.assertEqual(report["provenance"]["parent_image_sha256"], digest("parent-image"))
        self.assertEqual(report["provenance"]["candidate_image_sha256"], digest("candidate-image"))
        self.assertEqual(report["preserved_diagnostic_refs"], [{
            "stage": "expected_boot_failure", "path": f"{report['run_id']}-failure.json",
        }])
        self.assertEqual(len(report["owned_profiles"]), 3)
        report_path = Path(report["report_path"])
        self.assertEqual(report_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("source_path", json.loads(report_path.read_text()))
        self.assertNotIn(str(self.source), report_path.read_text())
        self.assertEqual(report_path.stat().st_size, len(report_path.read_bytes()))
        stored = json.loads(report_path.read_text())
        self.assertEqual(stored["report_ref"], report_path.name)
        self.assertNotIn("report_path", stored)

    def test_parent_identity_and_mismatched_candidate_parity_cannot_pass(self):
        invalid_parent = FixtureAdapter()
        original = invalid_parent.run_stage

        def missing_parent_identity(name, context):
            result = original(name, context)
            if name == "preflight":
                result["evidence"]["parent_identity_verified"] = False
            return result

        invalid_parent.run_stage = missing_parent_identity
        parent_report, parent_status = self.new_runner(invalid_parent).run()
        self.assertEqual(parent_status, 1)
        self.assertEqual(parent_report["stages"]["preflight"]["status"], "FAIL")
        self.assertEqual(parent_report["stages"]["broken_snapshot"]["status"], "NOT_RUN")

        parity = FixtureAdapter()
        original = parity.run_stage

        def wrong_parity(name, context):
            result = original(name, context)
            if name == "source_parity":
                result["evidence"]["guest_source_tree_sha256"] = "f" * 64
            return result

        parity.run_stage = wrong_parity
        report, status = self.new_runner(parity).run()
        self.assertEqual(status, 1)
        self.assertEqual(report["stages"]["source_parity"]["reason_code"], "invalid_evidence")
        self.assertNotIn("evidence", report["stages"]["source_parity"])

    def test_failure_must_be_guest_provisioning_with_exact_single_vcpu_and_safe_diagnostics(self):
        for field, value in (
            ("failure_stage", "preflight"),
            ("qemu_vcpus", True),
            ("diagnostic_refs", ["../../etc/passwd"]),
        ):
            with self.subTest(field=field):
                adapter = FixtureAdapter()
                original = adapter.run_stage

                def mutate(name, context, field=field, value=value, original=original):
                    result = original(name, context)
                    if name == "expected_boot_failure":
                        result["evidence"][field] = value
                    return result

                adapter.run_stage = mutate
                report, status = self.new_runner(adapter).run()
                self.assertEqual(status, 1)
                self.assertEqual(report["stages"]["expected_boot_failure"]["status"], "FAIL")
                self.assertEqual(report["stages"]["expected_boot_failure"]["reason_code"], "invalid_evidence")
                self.assertEqual(report["stages"]["fixed_snapshot"]["status"], "NOT_RUN")

    def test_live_lifecycle_mutation_and_installed_runtime_hash_are_required(self):
        for stage, field, value in (
            ("broken_snapshot", "candidate_lifecycle_sha256", digest("base-lifecycle")),
            ("fixed_boot", "installed_lifecycle_sha256", digest("different-installed-lifecycle")),
        ):
            with self.subTest(stage=stage):
                adapter = FixtureAdapter()
                original = adapter.run_stage

                def mutate(name, context, stage=stage, field=field, value=value, original=original):
                    result = original(name, context)
                    if name == stage:
                        result["evidence"][field] = value
                    return result

                adapter.run_stage = mutate
                report, status = self.new_runner(adapter).run()
                self.assertEqual(status, 1)
                self.assertEqual(report["stages"][stage]["reason_code"], "invalid_evidence")

    def test_live_lifecycle_probe_inserts_only_at_exact_allocation_anchor(self):
        candidate = self.base / "candidate"
        lifecycle = candidate / self_test.FIXTURE_LIFECYCLE
        lifecycle.parent.mkdir(parents=True)
        anchor = (
            b'  vm_lifecycle_profile_allocate "$root" "$name" "$depth" "$memory" '
            b'<<<"$data" >/dev/null || return $?\n'
        )
        lifecycle.write_bytes(b"#!/usr/bin/env bash\nvm_lifecycle_up() {\n" + anchor + b"}\n")
        lifecycle.chmod(0o755)
        run_id = "12345678123456781234567812345678"
        self_test._insert_live_lifecycle_probe(candidate, run_id)
        modified = lifecycle.read_bytes()
        self.assertIn(f".vm-self-test-lifecycle-{run_id}".encode(), modified)
        self.assertIn(b"VM_SELF_TEST_LIFECYCLE_RUN_ID", modified)
        self.assertEqual(lifecycle.stat().st_mode & 0o777, 0o755)
        with self.assertRaises(self_test.SelfTestError):
            self_test._insert_live_lifecycle_probe(candidate, run_id)

    def test_nested_depth_adapter_transports_and_validates_live_runtime_evidence(self):
        run_id = "12345678123456781234567812345678"
        adapter = self_test.LiveSelfTestAdapter.__new__(self_test.LiveSelfTestAdapter)
        adapter.run_id = run_id
        adapter.child_state = Path("/fixture/state")
        adapter.fixed = {"candidate_lifecycle_sha256": digest("candidate-lifecycle")}
        remembered = []
        adapter._remember = lambda name, depth: remembered.append((name, depth))
        payload = {
            "g2_profile_name": self_test._expected_profile(run_id, "fix2"),
            "g2_depth": 2, "max_depth": 2, "g3_refused": True,
            "refused_before_state": True, "refused_before_qemu": True,
            "parent_chain_verified": True, "reset_refused": True,
            "increase_refused": True, "missing_kvm_refused": True,
            "g2_runtime_payload_sha256": digest("runtime-payload"),
            "g2_installed_lifecycle_sha256": digest("candidate-lifecycle"),
            "lifecycle_mutation_observed": True,
        }
        adapter._ssh = lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps(payload).encode("ascii"),
        )
        evidence = adapter._nested_depth_check()
        self.assertEqual(remembered, [(self_test._expected_profile(run_id, "fix2"), 2)])
        self.assertEqual(evidence["candidate_lifecycle_sha256"], digest("candidate-lifecycle"))
        self.assertEqual(evidence["g2_installed_lifecycle_sha256"], digest("candidate-lifecycle"))
        self.assertTrue(evidence["lifecycle_mutation_observed"])

        payload["unexpected"] = True
        with mock.patch.object(adapter, "_ssh", return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(payload).encode("ascii"))), \
                self.assertRaises(self_test.SelfTestError):
            adapter._nested_depth_check()

    def test_missing_agent_is_blocked_and_untrusted_evidence_is_not_serialized(self):
        adapter = FixtureAdapter({"agent_task": {
            "status": "BLOCKED", "reason_code": "environment_unavailable",
            "evidence": {"raw_output": "credential-canary", "agent_completed": True},
        }})
        report, status = self.new_runner(adapter).run()
        self.assertEqual(status, 2)
        self.assertEqual(report["result"], "BLOCKED")
        self.assertEqual(report["e2e_self_status"], "NOT_RUN")
        self.assertNotIn("evidence", report["stages"]["agent_task"])
        self.assertEqual(report["stages"]["parent_continuity"]["status"], "NOT_RUN")
        self.assertNotIn("credential-canary", Path(report["report_path"]).read_text())

    def test_invalid_agent_or_human_claim_cannot_become_pass(self):
        adapter = FixtureAdapter()
        original = adapter.run_stage

        def fabricated(name, context):
            result = original(name, context)
            if name == "agent_task":
                result["evidence"]["task_id"] = "human-approved"
                result["evidence"]["human_result"] = "PASS"
            return result

        adapter.run_stage = fabricated
        report, status = self.new_runner(adapter).run()
        self.assertEqual(status, 1)
        self.assertEqual(report["result"], "FAIL")
        self.assertEqual(report["e2e_self_status"], "NOT_RUN")
        self.assertNotIn("evidence", report["stages"]["agent_task"])

    def test_stage_faults_preserve_first_cause_and_leave_later_stages_not_run(self):
        for stage in ("broken_snapshot", "expected_boot_failure", "fixed_snapshot", "fixed_boot",
                      "source_parity", "candidate_tests", "workload", "boundary", "depth",
                      "agent_task", "parent_continuity"):
            with self.subTest(stage=stage):
                adapter = FixtureAdapter({stage: {"status": "FAIL", "cause_exit_code": 37,
                                                   "evidence": {"raw_output": "must-not-leak"}}})
                report, status = self.new_runner(adapter).run()
                self.assertEqual(status, 1)
                self.assertEqual(report["stages"][stage]["cause_exit_code"], 37)
                later = list(self_test.STAGE_ORDER)[list(self_test.STAGE_ORDER).index(stage) + 1 :]
                self.assertTrue(all(report["stages"][item]["status"] == "NOT_RUN" for item in later))
                self.assertNotIn("must-not-leak", Path(report["report_path"]).read_text())

    def test_timeout_interrupt_total_deadline_and_report_failure_are_truthful(self):
        timed, code = self.new_runner(FixtureAdapter({"broken_snapshot": TimeoutError()})).run()
        self.assertEqual(code, 124)
        self.assertEqual(timed["stages"]["broken_snapshot"]["cause_exit_code"], 124)

        interrupted, code = self.new_runner(FixtureAdapter({"candidate_tests": KeyboardInterrupt()})).run()
        self.assertEqual(code, 130)
        self.assertEqual(interrupted["stages"]["candidate_tests"]["cause_exit_code"], 130)

        expired, code = self.new_runner(total_deadline=0).run()
        self.assertEqual(code, 124)
        self.assertEqual(expired["stages"]["preflight"]["status"], "FAIL")

        terminated, code = self.new_runner(
            FixtureAdapter({"fixed_boot": self_test.SelfTestTermination(143)})
        ).run()
        self.assertEqual(code, 143)
        self.assertEqual(terminated["stages"]["fixed_boot"]["cause_exit_code"], 143)

        report_failed, code = self.new_runner(report_writer=lambda *_: (_ for _ in ()).throw(OSError())).run()
        self.assertEqual(code, 1)
        self.assertEqual(report_failed["stages"]["report"]["reason_code"], "report_write_failed")

    def test_report_creation_is_no_clobber(self):
        run_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        path = self.report_root / f"{run_id}.json"
        self_test.write_report(self.report_root, run_id, {"schema_version": 1})
        original = path.read_bytes()
        with self.assertRaises(self_test.SelfTestError):
            self_test.write_report(self.report_root, run_id, {"schema_version": 2})
        with self.assertRaises(self_test.SelfTestError):
            self_test.write_report(self.report_root, "../outside", {"schema_version": 3})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_only_manifest_owned_profiles_are_cleaned_deepest_first(self):
        run_id = "abcdefabcdefabcdefabcdefabcdefab"
        profiles = [
            {"profile_name": self_test._expected_profile(run_id, "bad1"), "depth": 1, "run_id": run_id},
            {"profile_name": self_test._expected_profile(run_id, "fix1"), "depth": 1, "run_id": run_id},
            {"profile_name": self_test._expected_profile(run_id, "fix2"), "depth": 2, "run_id": run_id},
        ]
        adapter = CleanupAdapter()
        deleted = self_test.cleanup_owned_profiles(
            adapter, run_id, profiles, reports_preserved=True, diagnostics_preserved=True,
        )
        self.assertEqual(deleted, [profiles[2]["profile_name"], profiles[0]["profile_name"], profiles[1]["profile_name"]])
        self.assertEqual(adapter.calls[0], ("suspend", profiles[2]["profile_name"]))
        self.assertTrue(all(call[0] != "delete" or call[2] == call[1] for call in adapter.calls))

        foreign = [{"profile_name": "default", "depth": 1, "run_id": run_id}]
        untouched = CleanupAdapter()
        with self.assertRaises(self_test.SelfTestError):
            self_test.cleanup_owned_profiles(
                untouched, run_id, foreign, reports_preserved=True, diagnostics_preserved=True,
            )
        self.assertEqual(untouched.calls, [])
        with self.assertRaises(self_test.SelfTestError):
            self_test.cleanup_owned_profiles(
                untouched, run_id, profiles, reports_preserved=True, diagnostics_preserved=False,
            )

    def test_full_filtered_source_copy_observes_boot_failure_then_fresh_fix(self):
        contract = json.loads(FIXTURE_CONTRACT.read_text(encoding="ascii"))
        self.assertEqual(contract["failed_exit_code"], 42)
        self.assertEqual(contract["bootstrap_path"], str(self_test.FIXTURE_BOOTSTRAP))
        self.assertEqual(contract["runner_path"], str(self_test.FIXTURE_RUNNER))
        self.assertEqual(contract["runner_test_path"], str(self_test.FIXTURE_RUNNER_TEST))
        self.assertEqual(contract["failed_sentinel"], "A")
        self.assertEqual(contract["fixed_sentinel"], "B")
        self.assertEqual(contract["candidate_vcpus"], 1)
        before = {
            relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
            for relative in ("lib/vm/guest-bootstrap.sh", "lib/vm/runner.sh", "tests/vm-process.bats")
        }
        base_run = self.base / "base-snapshot-run"
        base_run.mkdir(mode=0o700)
        snapshot = self_test._snapshot_module().prepare(ROOT, base_run, self_test.MAX_SNAPSHOT_BYTES)
        source_workspace = Path(snapshot["workspace"])
        selected_source_tree = snapshot["tree_sha256"]

        failed_copy = self.base / "failed-source-copy"
        failed = self_test.create_failure_fixture_copy(source_workspace, failed_copy)
        failed_workspace = Path(failed["workspace"])
        self.assertNotEqual(failed["candidate_tree_sha256"], failed["base_source_tree_sha256"])
        self.assertTrue(failed["bundle"])

        fixed_copy = self.base / "fixed-source-copy"
        fixed = self_test.create_fixed_fixture_copy(failed_workspace, fixed_copy, failed)
        fixed_workspace = Path(fixed["workspace"])
        self.assertNotEqual(failed["candidate_tree_sha256"], fixed["candidate_tree_sha256"])
        self.assertNotEqual(failed["bundle"], fixed["bundle"])
        self.assertEqual(fixed["source_head"], failed["source_head"])
        self.assertEqual(fixed["base_source_tree_sha256"], failed["base_source_tree_sha256"])

        def run_prefix(workspace: Path, label: str):
            bootstrap = (workspace / "lib/vm/guest-bootstrap.sh").read_text(encoding="utf-8")
            lines = bootstrap.splitlines(keepends=True)
            self.assertTrue(lines[0].startswith("#!"))
            first = next(i for i, line in enumerate(lines) if line == self_test.FIXTURE_BEGIN)
            final = next(i for i, line in enumerate(lines[first + 1 :], first + 1)
                         if line == self_test.FIXTURE_END)
            fixture_script = self.base / f"{label}-prefix.sh"
            fixture_script.write_text(lines[0] + "".join(lines[first : final + 1]), encoding="utf-8")
            guest_root = self.base / f"{label}-guest-root"
            guest_root.mkdir(mode=0o700)
            return subprocess.run(
                ["bash", str(fixture_script)], capture_output=True, text=True, check=False,
                env={**os.environ, "VM_SELF_TEST_GUEST_ROOT": str(guest_root)}, timeout=5,
            ), guest_root

        broken_boot, broken_root = run_prefix(failed_workspace, "failed-A")
        self.assertEqual(broken_boot.returncode, 42)
        self.assertEqual((broken_root / "selftest-candidate").read_text().strip(), "A")
        fixed_boot, fixed_root = run_prefix(fixed_workspace, "fixed-B")
        self.assertEqual(fixed_boot.returncode, 0)
        self.assertEqual((fixed_root / "selftest-candidate").read_text().strip(), "B")

        for workspace in (failed_workspace, fixed_workspace):
            qemu = subprocess.run(
                ["bash", "-c", (
                 'source "$1/lib/vm/runner.sh"; VM_RUNNER_QEMU_BIN=qemu-fixture; '
                 'vm_runner_build_qemu_argv fixture 2 /tmp/seed.iso /tmp/disk.qcow2 '
                 '/tmp/vm-fixture.sock 42222 42223 none "" "" || exit $?; '
                 'printf "%s\\n" "${VM_QEMU_ARGV[@]}"'), "_", str(workspace)],
                capture_output=True, text=True, check=False, timeout=5,
            )
            self.assertEqual(qemu.returncode, 0, qemu.stderr)
            argv = qemu.stdout.splitlines()
            smp_index = argv.index("-smp")
            self.assertEqual(argv[smp_index + 1], "1")
            bats = subprocess.run(
                ["bats", "tests/vm-process.bats"], cwd=workspace,
                capture_output=True, text=True, check=False, timeout=45,
            )
            self.assertEqual(bats.returncode, 0, bats.stdout + bats.stderr)

        after = {
            relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
            for relative in before
        }
        self.assertEqual(before, after, "fixture injection/fix must never edit the selected source")
        self.assertEqual(self_test._mutated_tree_digest(ROOT), selected_source_tree)

    def test_cli_blocks_without_trusted_runtime_and_writes_private_report(self):
        state_root = self.base / "state"
        state_root.mkdir(mode=0o700)
        stdout = io.StringIO()
        with (mock.patch.dict(os.environ, {"MESTER_STATE_DIR": str(state_root)}, clear=False),
              contextlib.redirect_stdout(stdout)):
            code = self_test.main(["--source", str(self.source), "--max-depth", "2"])
        self.assertEqual(code, 2)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["result"], "BLOCKED")
        self.assertEqual(result["e2e_self_status"], "NOT_RUN")


if __name__ == "__main__":
    unittest.main()
