"""Fixture-only tests for exact-scope VM acceptance orchestration."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/vm-host-acceptance.py"
SPEC = importlib.util.spec_from_file_location("vm_host_acceptance", SCRIPT)
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)

RUN_ID = "12345678123456781234567812345678"


class HostAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lm-host-acceptance-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir(mode=0o700)
        self.state_root = self.base / "state"
        self.state_root.mkdir(mode=0o700)

    @contextlib.contextmanager
    def successful_preflight_environment(self):
        original_stat = Path.stat
        original_read_text = Path.read_text

        def fake_stat(path, *args, **kwargs):
            if str(path) == "/dev/kvm":
                return SimpleNamespace(st_mode=stat.S_IFCHR | 0o660)
            return original_stat(path, *args, **kwargs)

        def fake_read_text(path, *args, **kwargs):
            if str(path) == "/proc/cpuinfo":
                return "processor : 0\nflags : fpu vmx sse\n"
            return original_read_text(path, *args, **kwargs)

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(acceptance, "_private_directory"))
            stack.enter_context(mock.patch.object(acceptance, "_source_identity", return_value={
                "source_head": "a" * 40, "tree_sha256": "b" * 64,
                "entry_count": 20, "total_bytes": 4096,
            }))
            stack.enter_context(mock.patch.object(acceptance, "_read_mem_budget", return_value={
                "passed": True, "memavailable_mib": 32768, "cgroup_remaining_mib": 32768,
                "projected_available_mib": 32768, "requested_mib": 8192, "reserve_mib": 512,
            }))
            stack.enter_context(mock.patch.object(acceptance, "_socket_listener_summary", return_value={
                "readable": True, "listeners": 3, "loopback": 2,
            }))
            stack.enter_context(mock.patch.object(acceptance.platform, "system", return_value="Linux"))
            stack.enter_context(mock.patch.object(acceptance.platform, "machine", return_value="x86_64"))
            stack.enter_context(mock.patch.object(acceptance.os, "access", return_value=True))
            stack.enter_context(mock.patch.object(Path, "stat", fake_stat))
            stack.enter_context(mock.patch.object(Path, "read_text", fake_read_text))
            stack.enter_context(mock.patch.object(acceptance.shutil, "which", return_value="/mock/tool"))
            stack.enter_context(mock.patch.object(
                acceptance.shutil, "disk_usage",
                return_value=SimpleNamespace(total=8 * 1024**3, used=0, free=8 * 1024**3),
            ))
            yield

    def test_preflight_is_read_only_and_records_all_fixed_checks(self):
        with self.successful_preflight_environment():
            plan = acceptance.preflight(self.source, self.state_root, RUN_ID, 8)
        self.assertEqual(plan["result"], "PASS")
        self.assertEqual(
            {item["id"] for item in plan["checks"]},
            {
                "supported_linux_x86_64", "kvm_device_read_write", "cpu_virtualization_exposed",
                "required_tools", "memory_budget", "snapshot_disk_budget",
                "listener_inventory_readable", "unique_run_namespace", "private_campaign_parent",
                "unique_run_evidence", "private_evidence_directory",
            },
        )
        self.assertFalse(acceptance._namespace_path(self.state_root, acceptance._run_name(RUN_ID)).exists())

    def test_preflight_collision_blocks_before_campaign_mutation(self):
        namespace = acceptance._namespace_path(self.state_root, acceptance._run_name(RUN_ID))
        (namespace.parent / "reports").mkdir(mode=0o700, parents=True)
        collision_report = namespace.parent / "reports" / f"{acceptance._run_name(RUN_ID)}.json"
        collision_report.write_text("owned evidence remains\n", encoding="ascii")
        with self.successful_preflight_environment():
            plan = acceptance.preflight(self.source, self.state_root, RUN_ID, 8)
        self.assertEqual(plan["result"], "BLOCKED")
        self.assertIn("unique_run_evidence", {item["id"] for item in plan["checks"]})
        self.assertEqual(collision_report.read_text(encoding="ascii"), "owned evidence remains\n")

    def test_lifecycle_preflight_requires_memory_for_the_4_gib_resize_check(self):
        with self.successful_preflight_environment():
            plan = acceptance.preflight(self.source, self.state_root, RUN_ID, 4, ["lifecycle"])
        self.assertEqual(plan["result"], "BLOCKED")
        check = next(item for item in plan["checks"] if item["id"] == "lifecycle_resize_memory")
        self.assertEqual(check["status"], "BLOCKED")

    def test_execute_refuses_blocked_preflight_without_creating_a_namespace(self):
        with mock.patch.object(acceptance, "preflight", return_value={"result": "BLOCKED"}), \
                mock.patch.object(acceptance, "_new_campaign") as create:
            result, report_path = acceptance.execute_campaign(
                self.source, self.state_root, RUN_ID, ["clean"], 8, RUN_ID,
            )
        self.assertEqual(result["result"], "BLOCKED")
        self.assertEqual(report_path, Path())
        create.assert_not_called()
        self.assertFalse(acceptance._namespace_path(self.state_root, acceptance._run_name(RUN_ID)).exists())

    def _new_campaign_fixture(self, suites):
        run_root = acceptance._namespace_path(self.state_root, acceptance._run_name(RUN_ID))
        run_root.mkdir(mode=0o700, parents=True)
        child_state = run_root / "state"
        child_state.mkdir(mode=0o700)
        report_root = run_root.parent / "reports"
        report_root.mkdir(mode=0o700)
        report = {
            "schema_version": 1, "run_id": RUN_ID,
            "run_name": acceptance._run_name(RUN_ID),
            "created_utc": "2026-11-05T00:00:00+00:00",
            "source": {"source_head": "a" * 40, "tree_sha256": "b" * 64,
                       "entry_count": 10, "total_bytes": 4096},
            "snapshot": {"tree_sha256": "c" * 64, "entry_count": 10, "total_bytes": 4096},
            "suites": suites, "owned_profiles": [],
            "gates": {name: {"status": "NOT_RUN"} for name in acceptance.GATE_IDS},
            "result": "BLOCKED",
        }
        report_path = report_root / f"{acceptance._run_name(RUN_ID)}.json"
        acceptance._atomic_json(report_path, report)
        return run_root, child_state, self.source, report_path

    def test_execute_timeout_retains_gate_cause_and_exact_run_report(self):
        run_root, child_state, workspace, report_path = self._new_campaign_fixture(["clean"])
        preflight = {
            "result": "PASS", "checks": [], "memory": {},
            "source": {"source_head": "a" * 40, "tree_sha256": "b" * 64},
            "execution_context": {"system": "Linux", "machine": "x86_64"},
        }
        with mock.patch.object(acceptance, "preflight", return_value=preflight), \
                mock.patch.object(acceptance, "_new_campaign",
                                  return_value=(run_root, child_state, workspace)), \
                mock.patch.object(acceptance, "_source_identity", return_value={"tree_sha256": "c" * 64}), \
                mock.patch.object(acceptance, "_lifecycle_call", side_effect=TimeoutError):
            report, actual_path = acceptance.execute_campaign(
                self.source, self.state_root, RUN_ID, ["clean"], 8, RUN_ID,
            )
        self.assertEqual(actual_path, report_path)
        self.assertEqual(report["result"], "FAIL")
        self.assertEqual(report["gates"]["HOST-PREFLIGHT"]["status"], "PASS")
        self.assertEqual(report["gates"]["HOST-CLEAN"]["reason_code"], "deadline_exceeded")
        self.assertNotIn("active_gate", report)

    def test_execute_interrupt_persists_exact_manifest_and_independent_gate_state(self):
        run_root, child_state, workspace, report_path = self._new_campaign_fixture(["clean"])
        preflight = {
            "result": "PASS", "checks": [], "memory": {},
            "source": {"source_head": "a" * 40, "tree_sha256": "b" * 64},
            "execution_context": {"system": "Linux", "machine": "x86_64"},
        }
        with mock.patch.object(acceptance, "preflight", return_value=preflight), \
                mock.patch.object(acceptance, "_new_campaign",
                                  return_value=(run_root, child_state, workspace)), \
                mock.patch.object(acceptance, "_source_identity", return_value={"tree_sha256": "c" * 64}), \
                mock.patch.object(acceptance, "_lifecycle_call", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            acceptance.execute_campaign(self.source, self.state_root, RUN_ID, ["clean"], 8, RUN_ID)
        report = acceptance._read_private_json(report_path)
        self.assertEqual(report["result"], "BLOCKED")
        self.assertEqual(report["gates"]["HOST-PREFLIGHT"]["status"], "PASS")
        self.assertEqual(report["gates"]["HOST-CLEAN"]["reason_code"], "interrupted")
        self.assertEqual(report["owned_profiles"], [{
            "profile_name": acceptance._profile_name(RUN_ID), "depth": 0, "run_id": RUN_ID,
        }])
        self.assertNotIn("active_gate", report)

    def test_dry_run_cli_never_executes_or_creates_campaign_state(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
                mock.patch.object(acceptance, "preflight", return_value={
                    "result": "PASS", "checks": [], "source": {"tree_sha256": "b" * 64},
                }) as preflight, \
                mock.patch.object(acceptance, "execute_campaign") as execute:
            status = acceptance.main([
                "--source", str(self.source), "--state-root", str(self.state_root),
                "--suite", "clean", "--run-id", RUN_ID,
            ])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["result"], "PASS")
        preflight.assert_called_once()
        execute.assert_not_called()
        self.assertFalse(acceptance._namespace_path(self.state_root, acceptance._run_name(RUN_ID)).exists())

    def test_dry_run_lists_explicit_update_effect_without_execution(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
                mock.patch.object(acceptance, "preflight", return_value={
                    "result": "PASS", "checks": [], "source": {"tree_sha256": "b" * 64},
                }), \
                mock.patch.object(acceptance, "execute_campaign") as execute:
            status = acceptance.main([
                "--source", str(self.source), "--state-root", str(self.state_root),
                "--suite", "lifecycle", "--run-id", RUN_ID, "--apply-selected-updates",
            ])
        plan = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(plan["requested_effects"], {
            "selected_guest_update_targets": ["config", "tools"],
            "selected_guest_updates_authorized": True,
        })
        execute.assert_not_called()
        self.assertFalse(acceptance._namespace_path(self.state_root, acceptance._run_name(RUN_ID)).exists())

    def _campaign_report(self, suites, gates=None, owned=None):
        run_root = acceptance._namespace_path(self.state_root, acceptance._run_name(RUN_ID))
        run_root.mkdir(mode=0o700, parents=True)
        (run_root / "state").mkdir(mode=0o700)
        report_root = run_root.parent / "reports"
        report_root.mkdir(mode=0o700, exist_ok=True)
        report_path = report_root / f"{acceptance._run_name(RUN_ID)}.json"
        report = {
            "schema_version": 1, "run_id": RUN_ID,
            "run_name": acceptance._run_name(RUN_ID), "suites": suites,
            "gates": gates or {name: {"status": "BLOCKED"} for name in acceptance.GATE_IDS},
            "owned_profiles": owned or [], "result": "BLOCKED",
            "verified_guest_opencode_version": "2.2.5",
        }
        acceptance._atomic_json(report_path, report)
        return run_root, report_path

    def test_human_observation_requires_exact_run_and_never_promotes_other_gates(self):
        gates = {name: {"status": "BLOCKED", "reason_code": "owner_observation_required"}
                 for name in acceptance.GATE_IDS}
        gates["HOST-CLEAN"] = {"status": "PASS", "guest_opencode_version": "2.2.5"}
        _run_root, report_path = self._campaign_report(["human"], gates=gates)
        checklist = list(acceptance.HUMAN_CHECKLISTS["HUMAN-TUI"])
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.record_human_observation(
                self.state_root, RUN_ID, RUN_ID, "HUMAN-TUI", "PASS", "2.2.5", checklist[:-1],
            )
        result = acceptance.record_human_observation(
            self.state_root, RUN_ID, RUN_ID, "HUMAN-TUI", "PASS", "2.2.5", checklist,
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["campaign_result"], "BLOCKED")
        report = acceptance._read_private_json(report_path)
        self.assertEqual(report["gates"]["HUMAN-TUI"]["observation"], "OWNER_RECORDED")
        self.assertEqual(report["gates"]["HUMAN-PERM"]["status"], "BLOCKED")
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.record_human_observation(
                self.state_root, RUN_ID, "f" * 32, "HUMAN-PERM", "PASS", "2.2.5",
                list(acceptance.HUMAN_CHECKLISTS["HUMAN-PERM"]),
            )

    def test_owner_cannot_override_missing_docker_rollback_prerequisite(self):
        gates = {name: {"status": "BLOCKED", "reason_code": "not_run"}
                 for name in acceptance.GATE_IDS}
        gates["HOST-CLEAN"] = {"status": "PASS", "guest_opencode_version": "2.2.5"}
        gates["E2E-DAILY"] = {"status": "BLOCKED", "reason_code": "rollback_route_not_staged"}
        self._campaign_report(["daily"], gates=gates)
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.record_human_observation(
                self.state_root, RUN_ID, RUN_ID, "E2E-DAILY", "PASS", "2.2.5",
                list(acceptance.OWNER_CHECKLISTS["E2E-DAILY"]),
            )

    def test_cleanup_uses_only_exact_manifest_and_preserves_report(self):
        _run_root, report_path = self._campaign_report(["clean"], owned=[])
        remover = mock.Mock(return_value={"deleted_run": acceptance._run_name(RUN_ID)})
        with mock.patch.object(
            acceptance, "_load_module", return_value=SimpleNamespace(recursion_run_delete=remover),
        ):
            result = acceptance.cleanup_campaign(self.state_root, RUN_ID, RUN_ID)
        self.assertEqual(result["cleanup"], "PASS")
        remover.assert_called_once_with(
            self.state_root, acceptance._run_name(RUN_ID), acceptance._run_name(RUN_ID), Path("/proc"),
        )
        self.assertEqual(acceptance._read_private_json(report_path)["cleanup"]["status"], "PASS")

    def test_cleanup_gracefully_suspends_owned_profiles_deepest_first_before_delete(self):
        owned = [
            {"profile_name": acceptance._profile_name(RUN_ID), "depth": 0, "run_id": RUN_ID},
            {"profile_name": acceptance._profile_name(RUN_ID, "agent"), "depth": 1, "run_id": RUN_ID},
        ]
        run_root, _report_path = self._campaign_report(["clean"], owned=owned)
        child_state = run_root / "state"
        for item in owned:
            (child_state / "vm/profiles" / item["profile_name"]).mkdir(mode=0o700, parents=True)
        statuses = {}
        for item in owned:
            statuses[item["profile_name"]] = [
                {"status": "ready", "workspace": str(self.source), "memory_gib": 8},
                {"status": "suspended", "workspace": str(self.source), "memory_gib": 8},
            ]
        calls = []

        def profile_record(_state, name):
            return statuses[name].pop(0)

        def lifecycle(_state, name, _workspace, operation, _memory, **_kwargs):
            calls.append((name, operation))
            return SimpleNamespace(returncode=0)

        remover = mock.Mock(return_value={"deleted_run": acceptance._run_name(RUN_ID)})
        with mock.patch.object(acceptance, "_profile_record", side_effect=profile_record), \
                mock.patch.object(acceptance, "_lifecycle_call", side_effect=lifecycle), \
                mock.patch.object(
                    acceptance, "_load_module", return_value=SimpleNamespace(recursion_run_delete=remover),
                ):
            result = acceptance.cleanup_campaign(self.state_root, RUN_ID, RUN_ID)
        agent = acceptance._profile_name(RUN_ID, "agent")
        outer = acceptance._profile_name(RUN_ID)
        self.assertEqual(calls, [(agent, "suspend"), (agent, "delete"),
                                 (outer, "suspend"), (outer, "delete")])
        self.assertEqual(result["cleanup"], "PASS")

    def test_cleanup_rejects_foreign_profile_manifest_without_lifecycle_calls(self):
        _run_root, _report_path = self._campaign_report(["clean"], owned=[{
            "profile_name": "default", "depth": 0, "run_id": RUN_ID,
        }])
        with mock.patch.object(acceptance, "_load_module") as load, \
                self.assertRaises(acceptance.AcceptanceError):
            acceptance.cleanup_campaign(self.state_root, RUN_ID, RUN_ID)
        load.assert_not_called()

    def test_guest_commands_require_exact_owned_loopback_listener_first(self):
        ready = {"name": acceptance._profile_name(RUN_ID), "status": "ready"}
        with mock.patch.object(acceptance, "_profile_record", return_value=ready), \
                mock.patch.object(acceptance, "_listener_owned_loopback", return_value=False), \
                mock.patch.object(acceptance.subprocess, "run") as run, \
                self.assertRaises(acceptance.AcceptanceError):
            acceptance._guest_exec(self.state_root, acceptance._profile_name(RUN_ID), "true")
        run.assert_not_called()

    def test_guest_ssh_deadline_matches_bounded_host_deadline(self):
        profile = acceptance._profile_name(RUN_ID)
        runs = self.state_root / "vm/profiles" / profile / "runs" / "run-1"
        runs.mkdir(mode=0o700, parents=True)
        (runs / "processes.json").write_text("{}\n", encoding="ascii")
        record = {"status": "ready", "runtime_identity": {"pid": 1}}
        with mock.patch.object(acceptance, "_profile_record", return_value=record), \
                mock.patch.object(acceptance, "_listener_owned_loopback", return_value=True), \
                mock.patch.object(acceptance, "_profile_run_directory", return_value=runs), \
                mock.patch.object(acceptance.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            acceptance._guest_exec(self.state_root, profile, "true", timeout=17)
        self.assertEqual(run.call_args.kwargs["timeout"], 47)
        self.assertEqual(run.call_args.args[0][-1], "17")
        self.assertIn('"$deadline"', run.call_args.args[0][2])

    def test_profile_run_directory_matches_identity_not_the_number_of_retained_runs(self):
        profile = acceptance._profile_name(RUN_ID)
        runs = self.state_root / "vm/profiles" / profile / "runs"
        runs.mkdir(mode=0o700, parents=True)
        old_run = runs / "old-run"
        current_run = runs / "current-run"
        old_run.mkdir(mode=0o700)
        current_run.mkdir(mode=0o700)
        identity = {"pid": 99, "start_ticks": 100, "executable": "/qemu", "argv": ["qemu", "new"]}
        unrelated = {**identity, "pid": 98, "argv": ["qemu", "old"]}
        records = {
            "old-run": {"processes": {"qemu_main": {"identity": unrelated}}},
            "current-run": {"processes": {"qemu_main": {"identity": identity}}},
        }
        fake_process = SimpleNamespace(registry_read=lambda _path, name: records[name])
        with mock.patch.object(acceptance, "_load_module", return_value=fake_process):
            selected = acceptance._profile_run_directory(
                self.state_root, profile, {"runtime_identity": identity},
            )
        self.assertEqual(selected, current_run)
        records["old-run"]["processes"]["qemu_main"]["identity"] = identity
        with mock.patch.object(acceptance, "_load_module", return_value=fake_process), \
                self.assertRaises(acceptance.AcceptanceError):
            acceptance._profile_run_directory(self.state_root, profile, {"runtime_identity": identity})

    def test_guest_clean_report_fails_if_guest_docker_or_version_is_unavailable(self):
        checks = {
            "cloud_init_clean": True, "workspace_mount": True,
            "workspace_uid_gid_match": True, "node_available": True,
            "mcp_config_valid": True, "mcp_build_present": True,
            "guest_docker_available": False,
        }
        completed = SimpleNamespace(stdout=json.dumps({
            "checks": checks, "opencode_version": "2.2.5",
        }).encode("ascii"))
        with mock.patch.object(acceptance, "_guest_exec", return_value=completed):
            result = acceptance._guest_clean_checks(self.state_root, "profile", self.source, 1000, 1000)
        self.assertEqual(result["result"], "FAIL")

    def test_daily_suite_uses_only_frozen_warm_and_workspace_switch_apis(self):
        run_root = self.base / "run"
        run_root.mkdir(mode=0o700)
        identity_a = {"pid": 10, "start_ticks": 100, "executable": "/qemu", "argv": ["qemu", "A"]}
        identity_b = {"pid": 11, "start_ticks": 200, "executable": "/qemu", "argv": ["qemu", "B"]}
        records = [
            {"status": "ready", "workspace": str(self.source), "profile_id": "profile-id",
             "image_sha256": "a" * 64, "guest_payload_sha256": "b" * 64,
             "runtime_identity": identity_a},
            {"status": "ready", "runtime_identity": identity_a},
            {"status": "ready", "runtime_identity": identity_a},
            {"status": "ready", "workspace": str(run_root / "workspace-b-snapshot/workspace"),
             "profile_id": "profile-id", "image_sha256": "a" * 64,
             "guest_payload_sha256": "b" * 64, "runtime_identity": identity_b},
            {"status": "ready", "workspace": str(run_root / "workspace-b-snapshot/workspace"),
             "runtime_identity": identity_b},
            {"status": "ready", "workspace": str(run_root / "workspace-b-snapshot/workspace"),
             "runtime_identity": identity_b},
        ]
        calls = []

        def prepare(_source, destination, _limit):
            workspace_b = destination / "workspace"
            workspace_b.mkdir(mode=0o700)
            return {"workspace": str(workspace_b), "tree_sha256": "d" * 64}

        def lifecycle(_state, _profile, workspace, operation, memory, **kwargs):
            calls.append((operation, workspace, memory, kwargs.get("timeout")))
            return SimpleNamespace(returncode=0)

        with mock.patch.object(acceptance, "_profile_record", side_effect=records), \
                mock.patch.object(acceptance, "_listener_owned_loopback", return_value=True), \
                mock.patch.object(acceptance, "_overlay_identity", return_value=(1, 2)), \
                mock.patch.object(acceptance, "_hash_file", return_value="c" * 64), \
                mock.patch.object(acceptance, "_source_identity", return_value={"tree_sha256": "d" * 64}), \
                mock.patch.object(acceptance, "_load_module", return_value=SimpleNamespace(prepare=prepare)), \
                mock.patch.object(acceptance, "_lifecycle_call", side_effect=lifecycle):
            result = acceptance._daily_lifecycle_checks(
                self.state_root, acceptance._profile_name(RUN_ID), self.source, run_root, 8,
            )
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["same_overlay_and_key"])
        self.assertTrue(result["warm_process_identity_stable"])
        self.assertEqual([item[0] for item in calls], ["resume"] * 5)
        self.assertTrue(result["docker_rollback_route_staged"])

    def test_lifecycle_suite_scans_cancel_and_applies_only_explicit_targets(self):
        base = {
            "status": "ready", "profile_id": "profile-id", "image_sha256": "a" * 64,
            "workspace": str(self.source), "runtime_payload_sha256": "b" * 64,
            "runtime_identity": {"pid": 1}, "opencode_version": "2.2.5",
        }
        records = [
            base,
            {**base, "status": "suspended", "runtime_identity": None},
            base,
            base,
            {**base, "memory_gib": 4},
            {**base, "memory_gib": 8},
        ]
        operations = []

        def lifecycle(_state, _profile, _workspace, operation, memory, **_kwargs):
            operations.append((operation, memory))
            output = {
                "update-scan": b"CURRENT  selected guest targets already match\n",
                "update-apply": b"CURRENT  selected guest updates completed\n",
            }.get(operation, b"")
            return SimpleNamespace(returncode=0, stdout=output)

        with mock.patch.object(acceptance, "_profile_record", side_effect=records), \
                mock.patch.object(acceptance, "_overlay_identity", return_value=(1, 2)), \
                mock.patch.object(acceptance, "_hash_file", return_value="c" * 64), \
                mock.patch.object(acceptance, "_listener_owned_loopback", return_value=True), \
                mock.patch.object(acceptance, "_lifecycle_call", side_effect=lifecycle), \
                mock.patch.object(acceptance, "_guest_memory_mib", side_effect=(8192, 4096, 8192)):
            result = acceptance._lifecycle_retention_checks(
                self.state_root, "profile", self.source, 8, True,
            )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["selected_update_apply"], "PASS")
        self.assertIn(("update-scan", 8), operations)
        self.assertIn(("update-apply", 8), operations)
        self.assertIn(("resize", 4), operations)
        self.assertIn(("resize", 8), operations)

    def test_cached_image_check_is_pin_bound_and_retains_only_artifact_digest(self):
        digest = "a" * 64
        artifact = "b" * 64
        cache = self.state_root / "vm/images" / digest
        cache.mkdir(mode=0o700, parents=True)
        image = cache / "base.qcow2"
        image.write_bytes(b"fixture qcow bytes\n")
        image.chmod(0o600)
        acceptance._atomic_json(cache / "manifest.json", {
            "artifact_sha256": artifact,
        })
        profile = {"name": "profile", "image_sha256": digest, "image_path": str(image)}
        with mock.patch.object(acceptance, "_profile_record", return_value=profile), \
                mock.patch.object(acceptance.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            result = acceptance._cached_image_integrity(self.state_root, "profile")
        self.assertEqual(result, {"status": "PASS", "artifact_sha256": artifact})
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["bash", "-c"])
        self.assertIn(str(cache), command)

    def test_help_is_available_without_running_any_live_operation(self):
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"], cwd=ROOT,
            stdin=subprocess.DEVNULL, capture_output=True,
            check=False, timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
        self.assertIn(b"--execute", completed.stdout)
        self.assertIn(b"--record-observation", completed.stdout)


if __name__ == "__main__":
    unittest.main()
