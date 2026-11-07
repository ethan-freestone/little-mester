"""Fixture-only tests for exact-scope VM acceptance orchestration."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
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
UNSPECIFIED_MODELS = object()


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
                "unique_run_evidence", "unique_run_canary", "private_evidence_directory",
            },
        )
        self.assertFalse(acceptance._namespace_path(self.state_root, acceptance._run_name(RUN_ID)).exists())

    def _provider_config_file(self, value):
        path = self.base / "provider-config.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_provider_configuration_is_private_explicit_and_credential_free(self):
        path = self._provider_config_file({
            "schema_version": 1,
            "providers": [
                {"provider": "ollama", "endpoint": "http://127.0.0.1:11434", "model_id": "qwen3:8b"},
                {"provider": "llamacpp", "endpoint": "http://127.0.0.1:8080", "model_id": "coder-q4"},
                {"provider": "openai", "model_id": "openai/gpt-4.1"},
            ],
        })
        config, digest = acceptance.load_provider_config(path)
        self.assertEqual(digest, acceptance.hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual([item["provider"] for item in config["providers"]],
                         ["ollama", "llamacpp", "openai"])
        self.assertNotIn("api_key", path.read_text(encoding="utf-8"))

    def test_provider_configuration_rejects_unsafe_endpoints_and_permissions(self):
        for endpoint in ("http://localhost:11434", "http://127.0.0.1:11434/path",
                         "https://127.0.0.1:11434", "http://127.0.0.1:11434@evil.invalid"):
            path = self._provider_config_file({"schema_version": 1, "providers": [
                {"provider": "ollama", "endpoint": endpoint, "model_id": "qwen3:8b"},
            ]})
            with self.subTest(endpoint=endpoint), self.assertRaises(acceptance.AcceptanceError):
                acceptance.load_provider_config(path)
        path.chmod(0o644)
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.load_provider_config(path)

    def test_provider_probe_uses_only_the_exact_selected_loopback_endpoint(self):
        calls = []

        class Response:
            status = 200

            def __init__(self, body):
                self.body = body

            def read(self, _limit):
                return self.body

        class Connection:
            def __init__(self, host, port, timeout):
                self.host, self.port, self.timeout = host, port, timeout

            def request(self, method, route, headers):
                calls.append((self.host, self.port, self.timeout, method, route, headers["Connection"]))

            def getresponse(self):
                return Response(b'{"data":[{"id":"coder-q4"}]}')

            def close(self):
                pass

        with mock.patch.object(acceptance.http.client, "HTTPConnection", Connection):
            result = acceptance._probe_provider_models("llamacpp", "127.0.0.1", 43210)
        self.assertEqual(result, ["coder-q4"])
        self.assertEqual(calls, [("127.0.0.1", 43210, 2.0, "GET", "/v1/models", "close")])

    def test_provider_probe_caps_response_and_timeout(self):
        observed = []

        class Response:
            status = 200

            def read(self, limit):
                observed.append(("read", limit))
                return b"{" * limit

        class Connection:
            def __init__(self, host, port, timeout):
                observed.append(("connect", host, port, timeout))

            def request(self, method, route, headers):
                observed.append(("request", method, route))

            def getresponse(self):
                return Response()

            def close(self):
                observed.append(("close",))

        with mock.patch.object(acceptance.http.client, "HTTPConnection", Connection):
            result = acceptance._probe_provider_models("ollama", "127.0.0.1", 43210)
        self.assertIsNone(result)
        self.assertIn(("read", acceptance.MAX_PROVIDER_RESPONSE_BYTES + 1), observed)
        self.assertIn(("connect", "127.0.0.1", 43210, 2.0), observed)
        self.assertEqual(observed[-1], ("close",))

    def _topology_profile(self, *, depth=0, provider="ollama", model_port=43210,
                          forward_port=None, model_id="qwen3:8b"):
        name = "topology-fixture"
        forward_port = model_port if forward_port is None else forward_port
        guest_model_port = {"ollama": 11434, "llamacpp": 8080}.get(provider)
        netdev = (
            "user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22,"
            "guestfwd=tcp:10.0.2.100:3128-cmd:/usr/bin/socat - TCP:127.0.0.1:60000"
        )
        if guest_model_port is not None:
            netdev += (f",guestfwd=tcp:10.0.2.100:{guest_model_port}-cmd:/usr/bin/socat "
                       f"- TCP:127.0.0.1:{forward_port}")
        netdev += ",restrict=on"
        socket_path = str(self.base / "profile-control.sock")
        disk_path = str(self.state_root / "vm/profiles" / name / "disk.qcow2")
        profile = {
            "name": name, "status": "ready", "profile_id": "12345678-1234-5678-1234-567812345678",
            "depth": depth, "max_depth": 3, "guest_payload_sha256": "a" * 64,
            "runtime_identity": {
                "pid": 12345, "start_ticks": 98765, "executable": "/usr/bin/qemu-system-x86_64",
                "disk_path": disk_path, "socket_path": socket_path, "ssh_port": 2222,
                "argv": [
                    "/usr/bin/qemu-system-x86_64", "-enable-kvm",
                    "-drive", "file=/tmp/seed.iso,format=raw,if=none,id=cloud_disk,readonly=on",
                    "-drive", f"file={disk_path},format=qcow2,if=virtio",
                    "-chardev", f"socket,id=charfs,path={socket_path}", "-netdev", netdev,
                ],
            },
        }
        marker = {
            "schema_version": 1, "status": "ready", "agent": "opencode", "workload_runtime": "none",
            "payload_sha256": "a" * 64, "opencode_version": "2.2.0",
            "opencode_version_selector": "latest", "aider_version": "", "model_provider": provider,
            "model_port": model_port if guest_model_port is not None else None,
            "model_id": model_id, "workspace_mount": "virtiofs", "mcp_build": True,
            "recursion_tools": True, "depth": depth, "max_depth": 3,
        }
        return name, profile, marker

    def _discover_topology_fixture(self, *, depth=0, provider="ollama", model_port=43210,
                                   forward_port=None, model_id="qwen3:8b", marker=None,
                                   context=None, process_live=True, runtime_valid=True,
                                   listener_state="PRESENT",
                                   models=UNSPECIFIED_MODELS):
        name, profile, ready_marker = self._topology_profile(
            depth=depth, provider=provider, model_port=model_port,
            forward_port=forward_port, model_id=model_id,
        )
        if marker is not None:
            ready_marker = marker
        if context is None:
            context = {"state": "ABSENT", "depth": None, "max_depth": None}
        probe = mock.Mock(return_value=[model_id] if models is UNSPECIFIED_MODELS else models)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(acceptance, "_profile_record", return_value=profile))
            stack.enter_context(mock.patch.object(
                acceptance, "_validate_profile_runtime", return_value=runtime_valid,
            ))
            stack.enter_context(mock.patch.object(acceptance, "_listener_owned_loopback", return_value=process_live))
            stack.enter_context(mock.patch.object(
                acceptance, "_read_discovery_guest_marker", return_value=ready_marker,
            ))
            stack.enter_context(mock.patch.object(acceptance, "_read_current_recursion_identity", return_value=context))
            stack.enter_context(mock.patch.object(acceptance, "_loopback_listener_state", return_value=listener_state))
            stack.enter_context(mock.patch.object(acceptance, "_probe_provider_models", probe))
            report = acceptance.discover_profile_topology(self.state_root, name)
        return report, probe

    def test_nested_profile_reports_immediate_parent_and_probes_only_its_selected_forward(self):
        report, probe = self._discover_topology_fixture(
            depth=1, context={"state": "PERSISTED", "depth": 0, "max_depth": 3},
        )
        self.assertEqual(report["status"], "AVAILABLE")
        self.assertEqual(report["execution_context"], {"identity": "PERSISTED", "level": "G0", "depth": 0})
        self.assertEqual(report["profile"], {"name": "topology-fixture", "level": "G1", "depth": 1})
        self.assertEqual(report["forward"]["destination_class"], "immediate_parent_loopback")
        self.assertEqual(report["forward"]["destination_port"], 43210)
        self.assertEqual(report["relay"]["status"], "MISSING")
        self.assertEqual(report["capability_boundary"]["status"], "MISSING")
        probe.assert_called_once_with("ollama", "127.0.0.1", 43210, timeout=2.0)
        self.assertNotIn("argv", json.dumps(report))

    def test_unmarked_outer_context_is_not_mislabeled_as_physical_host(self):
        report, _probe = self._discover_topology_fixture()
        self.assertEqual(report["status"], "AVAILABLE")
        self.assertEqual(report["execution_context"]["level"], "UNKNOWN")
        self.assertEqual(report["forward"]["destination_class"], "outer_launcher_loopback")
        self.assertEqual(report["relay"]["status"], "NOT_REQUIRED")
        self.assertEqual(report["capability_boundary"]["status"], "MISSING")

    def test_missing_selected_loopback_listener_is_unavailable_without_contact(self):
        report, probe = self._discover_topology_fixture(
            depth=1, context={"state": "PERSISTED", "depth": 0, "max_depth": 3},
            listener_state="ABSENT",
        )
        self.assertEqual(report["status"], "ENDPOINT_UNAVAILABLE")
        self.assertEqual(report["reason_code"], "selected_loopback_listener_absent")
        self.assertEqual(report["forward"]["destination_class"], "immediate_parent_loopback")
        probe.assert_not_called()

    def test_selected_endpoint_failure_is_distinct_from_missing_model(self):
        report, probe = self._discover_topology_fixture(models=None)
        self.assertEqual(report["status"], "ENDPOINT_UNAVAILABLE")
        self.assertEqual(report["reason_code"], "selected_endpoint_unavailable")
        probe.assert_called_once()
        report, probe = self._discover_topology_fixture(models=[])
        # The valid empty model list means the selected model is not listed.
        self.assertEqual(report["status"], "MODEL_NOT_LISTED")
        self.assertEqual(report["reason_code"], "selected_model_not_listed")
        probe.assert_called_once()

    def test_wrong_profile_port_never_probes_any_endpoint(self):
        report, probe = self._discover_topology_fixture(model_port=43210, forward_port=43211)
        self.assertEqual(report["status"], "IDENTITY_UNKNOWN")
        self.assertEqual(report["reason_code"], "profile_selection_forward_mismatch")
        probe.assert_not_called()

    def test_stale_profile_identity_never_probes_any_endpoint(self):
        report, probe = self._discover_topology_fixture(process_live=False)
        self.assertEqual(report["status"], "IDENTITY_UNKNOWN")
        probe.assert_not_called()

    def test_unverified_frozen_runtime_never_probes_any_endpoint(self):
        report, probe = self._discover_topology_fixture(runtime_valid=False)
        self.assertEqual(report["status"], "IDENTITY_UNKNOWN")
        probe.assert_not_called()

    def test_nonprivate_state_root_never_probes_any_endpoint(self):
        self.state_root.chmod(0o755)
        report, probe = self._discover_topology_fixture()
        self.assertEqual(report["status"], "IDENTITY_UNKNOWN")
        self.assertEqual(report["reason_code"], "profile_or_forward_identity_unverified")
        probe.assert_not_called()

    def test_malformed_guest_marker_never_probes_any_endpoint(self):
        report, probe = self._discover_topology_fixture(marker={"model_provider": "ollama"})
        self.assertEqual(report["status"], "IDENTITY_UNKNOWN")
        probe.assert_not_called()

    def test_wrong_execution_context_never_probes_any_endpoint(self):
        report, probe = self._discover_topology_fixture(
            depth=1, context={"state": "ABSENT", "depth": None, "max_depth": None},
        )
        self.assertEqual(report["status"], "WRONG_CONTEXT")
        self.assertEqual(report["reason_code"], "nested_parent_identity_absent")
        probe.assert_not_called()

    def test_malformed_recursion_identity_never_probes_any_endpoint(self):
        report, probe = self._discover_topology_fixture(
            context={"state": "INVALID", "depth": None, "max_depth": None},
        )
        self.assertEqual(report["status"], "WRONG_CONTEXT")
        probe.assert_not_called()

    def test_no_capability_never_probes_any_endpoint(self):
        report, probe = self._discover_topology_fixture(provider="none", model_id="")
        self.assertEqual(report["status"], "NO_CAPABILITY")
        probe.assert_not_called()

    def test_local_provider_without_an_explicit_model_id_is_not_probed(self):
        report, probe = self._discover_topology_fixture(model_id="")
        self.assertEqual(report["status"], "NO_CAPABILITY")
        self.assertEqual(report["forward"]["status"], "MATCHED")
        self.assertEqual(report["reason_code"], "selected_model_id_absent")
        probe.assert_not_called()

    def test_profile_forward_parser_rejects_non_loopback_destination(self):
        name, profile, _marker = self._topology_profile()
        netdev_index = profile["runtime_identity"]["argv"].index("-netdev") + 1
        profile["runtime_identity"]["argv"][netdev_index] = profile["runtime_identity"]["argv"][
            netdev_index
        ].replace("TCP:127.0.0.1:43210", "TCP:10.0.0.1:43210")
        self.assertIsNone(acceptance._profile_model_forward_identity(profile, self.state_root, name))

    def test_persisted_recursion_identity_is_private_bounded_and_context_matched(self):
        path = self.base / "recursion-identity"
        path.write_text("0 3\n", encoding="ascii")
        path.chmod(0o600)
        self.assertEqual(acceptance._read_current_recursion_identity(path, {}),
                         {"state": "PERSISTED", "depth": 0, "max_depth": 3})
        self.assertEqual(acceptance._read_current_recursion_identity(
            path, {"LAB_VM_RECURSION_DEPTH": "1", "LAB_VM_MAX_DEPTH": "3"},
        )["state"], "INVALID")
        path.write_text("0 33\n", encoding="ascii")
        self.assertEqual(acceptance._read_current_recursion_identity(path, {})["state"], "INVALID")

    def test_provider_summary_never_contacts_unbound_endpoints(self):
        config = {"schema_version": 1, "providers": [
            {"provider": "ollama", "endpoint": "http://127.0.0.1:11434", "port": 11434,
             "model_id": "missing-model"},
            {"provider": "openai", "model_id": "openai/gpt-4.1"},
        ]}
        with mock.patch.object(acceptance, "_probe_provider_models", side_effect=AssertionError("unexpected endpoint contact")):
            result = acceptance.summarize_provider_selections(config)
        self.assertEqual(result["result"], "NOT_PROBED")
        self.assertEqual([item["status"] for item in result["providers"]],
                         ["PROFILE_BOUND_DISCOVERY_REQUIRED", "OWNER_AUTH_REQUIRED"])

    def test_discovery_cli_rejects_non_discovery_options(self):
        for extra in (("--memory-gib", "8"), ("--provider-config", "unused.json"),
                      ("--suite", "model")):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                acceptance.main([
                    "--discover-providers", "--state-root", "/unused/state",
                    "--profile", "exact-profile", *extra,
                ])

    def test_preflight_provider_config_is_not_a_probe_capability(self):
        path = self._provider_config_file({"schema_version": 1, "providers": [
            {"provider": "ollama", "endpoint": "http://127.0.0.1:45678", "model_id": "qwen3:8b"},
        ]})
        with self.successful_preflight_environment(), mock.patch.object(
                acceptance, "_probe_provider_models", side_effect=AssertionError("unbound endpoint contact")):
            plan = acceptance.preflight(
                self.source, self.state_root, RUN_ID, 8, provider_config_path=path,
            )
        self.assertEqual(plan["provider_config"]["selections"][0]["status"],
                         "PROFILE_BOUND_DISCOVERY_REQUIRED")

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
                approved_total_minutes=60,
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
        workspace_bundle = run_root / "candidate-snapshot"
        workspace_bundle.mkdir(mode=0o700)
        workspace = workspace_bundle / "workspace"
        workspace.mkdir(mode=0o700)
        report_root = run_root.parent / "reports"
        report_root.mkdir(mode=0o700)
        report = {
            "schema_version": 1, "workflow_version": acceptance.WORKFLOW_VERSION, "run_id": RUN_ID,
            "run_name": acceptance._run_name(RUN_ID),
            "created_utc": "2026-11-05T00:00:00+00:00",
            "source": {"source_head": "a" * 40, "tree_sha256": "b" * 64,
                       "entry_count": 10, "total_bytes": 4096},
            "snapshot": {"tree_sha256": "c" * 64, "entry_count": 10, "total_bytes": 4096},
            "suites": suites,
            "required_gates": acceptance._required_gates(suites),
            "acceptance_scope": acceptance._acceptance_scope(suites),
            "campaign_scope": acceptance._campaign_scope(suites, 8, None, False, 3600),
            "synthetic_canary": {"status": "PENDING", "encoding_checks": ["raw", "hex", "base64"]},
            "stages": {stage: "NOT_RUN" for stage in acceptance.STAGE_ORDER},
            "stage_elapsed_ms": {stage: 0 for stage in acceptance.STAGE_ORDER},
            "legacy_elapsed_ms": 0,
            "approved_total_budget_seconds": 3600,
            "provider_config_sha256": None,
            "owned_profiles": [],
            "gates": {
                name: ({"status": "DEFERRED", "reason_code": acceptance.DEFERRED_GATE_REASONS[name]}
                       if name in acceptance.DEFERRED_GATE_REASONS else {"status": "NOT_RUN"})
                for name in acceptance.GATE_IDS
            },
            "result": "BLOCKED",
        }
        report_path = report_root / f"{acceptance._run_name(RUN_ID)}.json"
        acceptance._atomic_json(report_path, report)
        return run_root, child_state, workspace, report_path

    def test_campaign_lock_serializes_exact_run_mutations(self):
        first = acceptance._campaign_lock(self.state_root, RUN_ID)
        first.__enter__()
        try:
            with self.assertRaises(acceptance.AcceptanceError), \
                    acceptance._campaign_lock(self.state_root, RUN_ID):
                self.fail("a second stage acquired the active campaign lock")
        finally:
            first.__exit__(None, None, None)
        lock_path = self.state_root / ".vm-host-acceptance.lock"
        self.assertTrue(lock_path.is_file())
        self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)

    def test_campaign_cannot_report_pass_before_exact_teardown(self):
        gates = {gate: {"status": "PASS"} for gate in acceptance._required_gates(["clean"])}
        report = {
            "suites": ["clean"],
            "required_gates": acceptance._required_gates(["clean"]),
            "acceptance_scope": acceptance._acceptance_scope(["clean"]),
            "gates": gates,
            "cleanup": {"status": "PASS", "synthetic_canary_removed": True},
            "synthetic_canary": {"status": "REMOVED"},
            "stages": {stage: "PASS" for stage in acceptance.STAGE_ORDER},
        }
        report["gates"]["SEC-SECRET"] = {
            "status": "NOT_RUN", "final_report_check": {"status": "PASS"},
        }
        report["stages"]["teardown"] = "NOT_RUN"
        self.assertEqual(acceptance._campaign_result(report), "BLOCKED")
        report["stages"]["teardown"] = "PASS"
        report["synthetic_canary"]["status"] = "GENERATED"
        self.assertEqual(acceptance._campaign_result(report), "BLOCKED")
        report["synthetic_canary"]["status"] = "REMOVED"
        self.assertEqual(acceptance._campaign_result(report), "PASS")

    def test_deferred_live_auth_does_not_mask_required_local_failure(self):
        suites = ["human"]
        required = acceptance._required_gates(suites)
        gates = {gate: {"status": "PASS"} for gate in required}
        gates["HUMAN-AUTH"] = {
            "status": "DEFERRED", "reason_code": acceptance.DEFERRED_GATE_REASONS["HUMAN-AUTH"],
        }
        report = {
            "suites": suites, "required_gates": required,
            "acceptance_scope": acceptance._acceptance_scope(suites), "gates": gates,
            "cleanup": {"status": "PASS", "synthetic_canary_removed": True},
            "synthetic_canary": {"status": "REMOVED"},
            "stages": {stage: "PASS" for stage in acceptance.STAGE_ORDER},
        }
        report["gates"]["SEC-SECRET"] = {
            "status": "NOT_RUN", "final_report_check": {"status": "PASS"},
        }
        self.assertNotIn("HUMAN-AUTH", required)
        self.assertEqual(acceptance._campaign_result(report), "PASS")
        report["gates"]["HUMAN-PERM"] = {"status": "BLOCKED", "reason_code": "fixture_failure"}
        self.assertEqual(acceptance._campaign_result(report), "BLOCKED")

    def test_nested_campaign_scope_requires_tui_and_reserves_teardown_budget(self):
        required = acceptance._required_gates(["boundary"])
        self.assertIn("HUMAN-TUI", required)
        self.assertEqual(
            acceptance._remaining_campaign_seconds(3600, 0, 0, stage="nested-boundaries-agents"),
            3600 - acceptance.TEARDOWN_RESERVE_SECONDS,
        )
        self.assertEqual(
            acceptance._remaining_campaign_seconds(3600, 0, 0, stage="teardown"), 3600,
        )

    def test_canary_subchecks_reflect_only_their_manifested_artifacts(self):
        scan = {"checks": [
            {"id": "snapshot_workspace", "status": "PASS"},
            {"id": "snapshot_git_history", "status": "PASS"},
            {"id": "profile_overlay_outer", "status": "BLOCKED"},
            {"id": "campaign_logs_outer", "status": "PASS"},
        ]}
        self.assertEqual(acceptance._canary_check_status(
            scan, exact_ids={"snapshot_workspace", "snapshot_git_history"},
        ), "PASS")
        self.assertEqual(acceptance._canary_check_status(
            scan, prefixes=("profile_overlay_", "campaign_logs_"),
        ), "BLOCKED")
        scan["checks"][0]["status"] = "FAIL"
        self.assertEqual(acceptance._canary_check_status(
            scan, exact_ids={"snapshot_workspace", "snapshot_git_history"},
        ), "FAIL")
        self.assertEqual(acceptance._canary_check_status(
            scan, exact_ids={"snapshot_workspace", "snapshot_git_history", "missing"},
        ), "BLOCKED")

    def test_teardown_can_follow_an_early_stop_or_stale_stage_marker(self):
        report = {"stages": {stage: "NOT_RUN" for stage in acceptance.STAGE_ORDER}}
        acceptance._mark_stage_running(report, "teardown")
        self.assertEqual(report["stages"]["teardown"], "RUNNING")
        report["stages"]["teardown"] = "NOT_RUN"
        report["stages"]["clean-usability"] = "RUNNING"
        acceptance._mark_stage_running(report, "teardown")
        self.assertEqual(report["stages"]["teardown"], "RUNNING")

    def test_teardown_is_a_budgeted_final_stage_and_persists_report(self):
        _run_root, report_path = self._campaign_report(["clean"], owned=[])

        def cleanup(_state, _run_id, _confirm, *, staged_teardown=False):
            self.assertTrue(staged_teardown)
            report = acceptance._read_private_json(report_path)
            report["cleanup"] = {"status": "PASS", "deleted_profiles": []}
            report["stages"]["teardown"] = "PASS"
            report["stage_elapsed_ms"]["teardown"] = acceptance._stage_elapsed_ms()
            acceptance._atomic_replace_json(report_path, report)
            return {"cleanup": "PASS"}

        with mock.patch.object(acceptance, "cleanup_campaign", side_effect=cleanup):
            report, actual_path = acceptance.execute_teardown_stage(
                self.state_root, RUN_ID, RUN_ID, 60,
            )
        self.assertEqual(actual_path, report_path)
        self.assertEqual(report["stages"]["teardown"], "PASS")
        self.assertEqual(report["cleanup"]["status"], "PASS")

    def test_teardown_failure_remains_blocked_and_budget_cannot_change(self):
        _run_root, report_path = self._campaign_report(["clean"], owned=[])
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.execute_teardown_stage(self.state_root, RUN_ID, RUN_ID, 61)
        with mock.patch.object(acceptance, "cleanup_campaign", side_effect=acceptance.AcceptanceError("fixture")), \
                self.assertRaises(acceptance.AcceptanceError):
            acceptance.execute_teardown_stage(self.state_root, RUN_ID, RUN_ID, 60)
        report = acceptance._read_private_json(report_path)
        self.assertEqual(report["stages"]["teardown"], "BLOCKED")

    def test_cleanup_report_pass_is_written_only_after_run_namespace_removal(self):
        run_root, report_path = self._campaign_report(["clean"], owned=[])
        report = acceptance._read_private_json(report_path)
        report["stages"]["teardown"] = "RUNNING"
        acceptance._atomic_replace_json(report_path, report)
        def exact_delete(_state_root, run_name, confirm_name, _proc):
            self.assertEqual(run_name, acceptance._run_name(RUN_ID))
            self.assertEqual(confirm_name, run_name)
            during_cleanup = acceptance._read_private_json(report_path)
            self.assertEqual(during_cleanup["cleanup"]["status"], "RUNNING")
            self.assertEqual(during_cleanup["stages"]["teardown"], "RUNNING")
            self.assertEqual(during_cleanup["result"], "BLOCKED")
            shutil.rmtree(run_root)

        with mock.patch.object(acceptance, "_load_module", return_value=SimpleNamespace(
                recursion_run_delete=exact_delete)):
            result = acceptance.cleanup_campaign(
                self.state_root, RUN_ID, RUN_ID, staged_teardown=True,
            )
        report = acceptance._read_private_json(report_path)
        self.assertFalse(run_root.exists())
        self.assertEqual(result["cleanup"], "PASS")
        self.assertEqual(report["cleanup"]["status"], "PASS")
        self.assertEqual(report["stages"]["teardown"], "PASS")
        self.assertEqual(report["result"], "BLOCKED")

    def test_teardown_postcondition_failure_cannot_leave_a_stale_campaign_pass(self):
        _run_root, report_path = self._campaign_report(["clean"], owned=[])
        report = acceptance._read_private_json(report_path)
        for gate in acceptance.SUITE_GATES["clean"]:
            report["gates"][gate] = {"status": "PASS"}
        acceptance._atomic_replace_json(report_path, report)

        def fail_after_cleanup_report(_state_root, _run_id, _confirm, *, staged_teardown=False):
            self.assertTrue(staged_teardown)
            value = acceptance._read_private_json(report_path)
            value["cleanup"] = {"status": "PASS"}
            value["stages"]["teardown"] = "PASS"
            value["result"] = acceptance._campaign_result(value)
            acceptance._atomic_replace_json(report_path, value)
            raise acceptance.AcceptanceError("postcondition fixture")

        with mock.patch.object(acceptance, "cleanup_campaign", side_effect=fail_after_cleanup_report), \
                self.assertRaises(acceptance.AcceptanceError):
            acceptance.execute_teardown_stage(self.state_root, RUN_ID, RUN_ID, 60)
        report = acceptance._read_private_json(report_path)
        self.assertEqual(report["cleanup"]["status"], "BLOCKED")
        self.assertEqual(report["stages"]["teardown"], "BLOCKED")
        self.assertNotEqual(report["result"], "PASS")

    def test_campaign_resume_rejects_revision_and_provider_config_changes(self):
        run_root, _child_state, _workspace, report_path = self._new_campaign_fixture(["clean", "model"])
        report = acceptance._read_private_json(report_path)
        report["provider_config_sha256"] = "d" * 64
        acceptance._atomic_replace_json(report_path, report)
        identity = {"source_head": "a" * 40, "tree_sha256": "b" * 64}
        with mock.patch.object(acceptance, "_source_identity", return_value={"tree_sha256": "c" * 64}):
            resumed = acceptance._resume_campaign(
                self.state_root, self.source, RUN_ID, ["clean", "model"], "d" * 64, identity,
            )
            self.assertEqual(resumed[1], run_root)
            with self.assertRaises(acceptance.AcceptanceError):
                acceptance._resume_campaign(
                    self.state_root, self.source, RUN_ID, ["clean", "model"], "e" * 64, identity,
                )
            with self.assertRaises(acceptance.AcceptanceError):
                acceptance._resume_campaign(
                    self.state_root, self.source, RUN_ID, ["clean", "model"], "d" * 64,
                    {**identity, "tree_sha256": "f" * 64},
                )
            changed_scope = {**report["campaign_scope"], "memory_gib": 4}
            with self.assertRaises(acceptance.AcceptanceError):
                acceptance._resume_campaign(
                    self.state_root, self.source, RUN_ID, ["clean", "model"], "d" * 64,
                    identity, changed_scope,
                )

    def test_campaign_resume_revalidates_profile_and_listener_identity(self):
        _run_root, _child_state, _workspace, report_path = self._new_campaign_fixture(["clean"])
        report = acceptance._read_private_json(report_path)
        report["gates"]["HOST-CLEAN"] = {
            "status": "PASS", "profile_id": "expected-profile", "image_sha256": "a" * 64,
            "guest_payload_sha256": "b" * 64, "runtime_payload_sha256": "c" * 64,
        }
        acceptance._atomic_replace_json(report_path, report)
        identity = {"source_head": "a" * 40, "tree_sha256": "b" * 64}
        current = {
            "name": acceptance._profile_name(RUN_ID), "depth": 0, "status": "ready",
            "profile_id": "expected-profile", "image_sha256": "a" * 64,
            "guest_payload_sha256": "b" * 64, "runtime_payload_sha256": "c" * 64,
        }
        with mock.patch.object(acceptance, "_source_identity", return_value={"tree_sha256": "c" * 64}), \
                mock.patch.object(acceptance, "_profile_record", return_value=current), \
                mock.patch.object(acceptance, "_listener_owned_loopback", return_value=True):
            acceptance._resume_campaign(
                self.state_root, self.source, RUN_ID, ["clean"], None, identity,
            )
            with mock.patch.object(acceptance, "_listener_owned_loopback", return_value=False), \
                    self.assertRaises(acceptance.AcceptanceError):
                acceptance._resume_campaign(
                    self.state_root, self.source, RUN_ID, ["clean"], None, identity,
                )

    def test_stage_order_allows_independent_progress_after_explicit_block(self):
        report = {"stages": {stage: "NOT_RUN" for stage in acceptance.STAGE_ORDER}}
        acceptance._mark_stage_running(report, "clean-usability")
        report["stages"]["clean-usability"] = "BLOCKED"
        acceptance._mark_stage_running(report, "early-auth")
        report["stages"]["early-auth"] = "DEFERRED"
        acceptance._mark_stage_running(report, "providers-daily-permissions")
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance._mark_stage_running(report, "providers-daily-permissions")

    def test_openai_auth_compatibility_stages_are_explicitly_deferred_without_auth_calls(self):
        run_root, _child_state, workspace, report_path = self._new_campaign_fixture(["clean", "human"])
        report = acceptance._read_private_json(report_path)
        self.assertEqual(report["gates"]["HUMAN-AUTH"]["status"], "DEFERRED")
        report["campaign_scope"] = acceptance._campaign_scope(
            ["clean", "human"], 8, "", False, 3600,
        )
        report["provider_config_sha256"] = ""
        report["requested_effects"] = {
            "selected_guest_update_targets": [], "selected_guest_updates_authorized": False,
        }
        report["stages"]["clean-usability"] = "PASS"
        acceptance._atomic_replace_json(report_path, report)
        preflight = {"result": "PASS", "provider_config": {"sha256": ""},
                     "source": report["source"]}

        with mock.patch.object(acceptance, "preflight", return_value=preflight), \
                mock.patch.object(acceptance, "_resume_campaign",
                                  return_value=(report, run_root, run_root / "state", workspace)), \
                mock.patch.object(acceptance, "_openai_auth_checkpoint") as auth_checkpoint, \
                mock.patch.object(acceptance, "_provider_stream_checks") as stream_checks:
            early, _ = acceptance.execute_stage(
                self.source, self.state_root, RUN_ID, ["clean", "human"], 8, RUN_ID,
                "early-auth", approved_total_minutes=60,
            )
            self.assertEqual(early["early_auth"]["status"], "DEFERRED")
            self.assertEqual(early["stages"]["early-auth"], "DEFERRED")
            self.assertEqual(early["early_auth"]["reason_code"], "owner_deferred_live_openai")
            auth_checkpoint.assert_not_called()
            stream_checks.assert_not_called()

    def test_approved_time_budget_is_explicit_and_tightens_automation_deadline(self):
        self.assertEqual(acceptance._approved_budget_seconds(30), 1800)
        for value in (None, 0, -1, acceptance.MAX_APPROVED_TOTAL_MINUTES + 1, True):
            with self.subTest(value=value), self.assertRaises(acceptance.AcceptanceError):
                acceptance._approved_budget_seconds(value)
        with acceptance._stage_budget():
            acceptance._tighten_stage_budget(5)
            self.assertLessEqual(acceptance._bounded_timeout(60), 5)

    def test_insufficient_teardown_reserve_is_rejected_before_attempt_registration(self):
        initial = {
            "result": "PASS", "provider_config": {"sha256": "c" * 64},
            "source": {"source_head": "a" * 40, "tree_sha256": "b" * 64},
        }
        with mock.patch.object(acceptance, "preflight", return_value=initial), \
                mock.patch.object(acceptance, "_register_campaign_attempt") as register, \
                mock.patch.object(acceptance, "_new_campaign") as create, \
                self.assertRaises(acceptance.AcceptanceError):
            acceptance.execute_campaign(
                self.source, self.state_root, RUN_ID, ["clean"], 8, RUN_ID,
                workflow_stage="clean-usability", approved_total_minutes=30,
            )
        register.assert_not_called()
        create.assert_not_called()

    def test_attempt_ledger_allows_one_diagnosed_retry_and_binds_exact_revision(self):
        source_identity = {"source_head": "a" * 40, "tree_sha256": "b" * 64}
        first = acceptance._register_campaign_attempt(
            self.state_root, RUN_ID, source_identity, "c" * 64,
            retry_of=None, retry_reason=None, approved_total_budget_seconds=3600,
        )
        self.assertEqual(first["ordinal"], 1)

        prior_id = RUN_ID
        prior_root = acceptance._namespace_path(self.state_root, acceptance._run_name(prior_id))
        prior_root.mkdir(mode=0o700, parents=True)
        prior_report_root = prior_root.parent / "reports"
        prior_report_root.mkdir(mode=0o700)
        prior_report = {
            "schema_version": 1, "run_id": prior_id,
            "run_name": acceptance._run_name(prior_id), "result": "BLOCKED",
            "source": source_identity, "provider_config_sha256": "c" * 64,
            "campaign_scope": {
                "scope_id": acceptance.LOCAL_FIRST_SCOPE_ID, "suites": [], "memory_gib": 8,
                "provider_config_sha256": "c" * 64, "apply_selected_updates": False,
                "approved_total_budget_seconds": 3600,
            },
            "approved_total_budget_seconds": 3600,
            "stages": {stage: ("PASS" if stage == "teardown" else "BLOCKED")
                       for stage in acceptance.STAGE_ORDER},
            "stage_elapsed_ms": {stage: 100_000 for stage in acceptance.STAGE_ORDER},
            "cleanup": {"status": "BLOCKED"},
            "gates": {"E2E-MODEL": {"status": "BLOCKED",
                                    "reason_code": "provider_stream_incomplete"}},
        }
        acceptance._atomic_json(
            prior_report_root / f"{acceptance._run_name(prior_id)}.json", prior_report,
        )
        retry_id = "abcdefabcdefabcdefabcdefabcdefab"
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance._register_campaign_attempt(
                self.state_root, retry_id, source_identity, "c" * 64,
                retry_of=prior_id, retry_reason="provider_stream_incomplete",
                approved_total_budget_seconds=3600,
            )
        prior_report["cleanup"] = {"status": "PASS"}
        acceptance._atomic_replace_json(
            prior_report_root / f"{acceptance._run_name(prior_id)}.json", prior_report,
        )
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance._register_campaign_attempt(
                self.state_root, retry_id, source_identity, "c" * 64,
                retry_of=prior_id, retry_reason="provider_stream_incomplete",
                approved_total_budget_seconds=7200,
            )
        retry = acceptance._register_campaign_attempt(
            self.state_root, retry_id, source_identity, "c" * 64,
            retry_of=prior_id, retry_reason="provider_stream_incomplete",
            approved_total_budget_seconds=3600,
        )
        self.assertEqual(retry, {"ordinal": 2, "retry_of": prior_id,
                                 "retry_reason": "provider_stream_incomplete",
                                 "prior_elapsed_ms": 700_000})
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance._register_campaign_attempt(
                self.state_root, "f" * 32, source_identity, "c" * 64,
                retry_of=prior_id, retry_reason="provider_stream_incomplete",
                approved_total_budget_seconds=3600,
            )
        changed_config = acceptance._register_campaign_attempt(
            self.state_root, "e" * 32, source_identity, "d" * 64,
            retry_of=None, retry_reason=None, approved_total_budget_seconds=3600,
        )
        self.assertEqual(changed_config["ordinal"], 1)

    def test_attempt_retry_requires_reason_from_the_prior_report(self):
        source_identity = {"source_head": "a" * 40, "tree_sha256": "b" * 64}
        acceptance._register_campaign_attempt(
            self.state_root, RUN_ID, source_identity, "c" * 64,
            retry_of=None, retry_reason=None, approved_total_budget_seconds=3600,
        )
        run_root = acceptance._namespace_path(self.state_root, acceptance._run_name(RUN_ID))
        run_root.mkdir(mode=0o700, parents=True)
        reports = run_root.parent / "reports"
        reports.mkdir(mode=0o700)
        acceptance._atomic_json(reports / f"{acceptance._run_name(RUN_ID)}.json", {
            "run_id": RUN_ID, "run_name": acceptance._run_name(RUN_ID),
            "result": "BLOCKED", "source": source_identity,
            "provider_config_sha256": "c" * 64,
            "gates": {"E2E-MODEL": {"reason_code": "provider_stream_incomplete"}},
        })
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance._register_campaign_attempt(
                self.state_root, "d" * 32, source_identity, "c" * 64,
                retry_of=RUN_ID, retry_reason="not_run", approved_total_budget_seconds=3600,
            )

    def test_retry_cannot_change_local_first_scope_or_teardown_budget(self):
        source_identity = {"source_head": "a" * 40, "tree_sha256": "b" * 64}
        scope = acceptance._campaign_scope(["clean", "boundary"], 8, "c" * 64, False, 3600)
        acceptance._register_campaign_attempt(
            self.state_root, RUN_ID, source_identity, "c" * 64,
            retry_of=None, retry_reason=None, approved_total_budget_seconds=3600,
            campaign_scope=scope,
        )
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance._register_campaign_attempt(
                self.state_root, "abcdefabcdefabcdefabcdefabcdefab", source_identity, "c" * 64,
                retry_of=RUN_ID, retry_reason="diagnosed_failure", approved_total_budget_seconds=3600,
                campaign_scope={**scope, "memory_gib": 4},
            )

    def test_workload_gate_requires_compose_retention_and_exact_cleanup_before_owner_step(self):
        setup = {"status": "PASS", "checks": {"query_ok": True}}
        retention = {"status": "PASS", "database_canary_retained": True,
                     "compose_stopped": True}
        cleanup = {"volume_removed": True, "workspace_artifacts_removed": True}
        ready = acceptance._workload_gate_result(setup, retention, cleanup)
        self.assertEqual(ready["status"], "BLOCKED")
        self.assertEqual(ready["automated_status"], "PASS")
        self.assertEqual(ready["reason_code"], "owner_live_evidence_required")
        self.assertIn("failing-test-fix-pass", ready["pending_owner_checklist"])
        self.assertEqual(
            acceptance._workload_gate_result(setup, {**retention, "status": "FAIL"}, cleanup)["status"],
            "FAIL",
        )
        self.assertEqual(
            acceptance._workload_gate_result(setup, retention, None)["reason_code"],
            "exact_workload_cleanup_unconfirmed",
        )
        self.assertEqual(
            acceptance._workload_gate_result({"status": "BLOCKED", "reason_code": "image_missing"},
                                              None, cleanup)["status"],
            "BLOCKED",
        )

    def test_guest_workload_reports_are_schema_bound_and_use_only_generated_paths(self):
        checks = {
            "postgres_image_available": True, "compose_health": True, "query_ok": True,
            "bind_mount_readable": True, "volume_survives_container_restart": True,
            "no_published_service": True, "guest_loopback_only": True,
            "compose_down": True, "volume_retained_for_vm_suspend": True,
        }
        completed = subprocess.CompletedProcess([], 0, json.dumps({
            "schema_version": 1, "result": "PASS", "checks": checks,
        }), "")
        with mock.patch.object(acceptance, "_guest_exec", return_value=completed) as guest_exec:
            result = acceptance._guest_workload_checks(self.state_root, "accept-123456789abc-outer", RUN_ID)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(guest_exec.call_args.kwargs["timeout"], 480)
        command = guest_exec.call_args.args[2]
        self.assertIn(".lm-acceptance-work-123456781234", command)
        self.assertIn("lmacc-123456781234", command)
        self.assertIn('DOCKER_HOST"]="unix:///var/run/docker.sock"', command)
        self.assertNotIn(" -v /var/run/docker.sock", command)
        self.assertNotIn("--volume /var/run/docker.sock", command)

        unsafe = subprocess.CompletedProcess([], 0, json.dumps({
            "schema_version": 1, "result": "PASS", "checks": {**checks, "transcript": True},
        }), "")
        with mock.patch.object(acceptance, "_guest_exec", return_value=unsafe), \
                self.assertRaises(acceptance.AcceptanceError):
            acceptance._run_guest_workload_code(
                self.state_root, "accept-123456789abc-outer", RUN_ID, acceptance.GUEST_WORKLOAD_CODE,
            )

    def test_guest_workload_pulls_missing_postgres_only_inside_guest_with_a_deadline(self):
        def run_fixture(image_available_after_pull):
            calls = []
            image_inspections = 0
            compose_active = False

            def completed(returncode=0, stdout=b""):
                return subprocess.CompletedProcess([], returncode, stdout, b"")

            def fake_run(argv, **kwargs):
                nonlocal image_inspections, compose_active
                args = list(argv)
                calls.append((args, kwargs))
                self.assertEqual(kwargs["env"]["DOCKER_HOST"], "unix:///var/run/docker.sock")
                self.assertNotIn("DOCKER_CONTEXT", kwargs["env"])
                if args[:3] == ["docker", "image", "inspect"]:
                    image_inspections += 1
                    available = image_inspections > 1 and image_available_after_pull
                    return completed(0 if available else 1)
                if args[:2] == ["docker", "pull"]:
                    docker_config = Path(kwargs["env"]["DOCKER_CONFIG"])
                    self.assertTrue(docker_config.is_dir())
                    self.assertFalse((docker_config / "config.json").exists())
                    return completed(0 if image_available_after_pull else 1)
                if args[:2] == ["docker", "inspect"]:
                    return completed(stdout=b"{}")
                if args[:2] == ["docker", "volume"]:
                    return completed()
                if args[:2] == ["docker", "compose"]:
                    operation = args[args.index("-f") + 2]
                    if operation == "up":
                        compose_active = True
                    elif operation == "down":
                        compose_active = False
                    elif operation == "ps":
                        return completed(stdout=b"container-id\n" if compose_active else b"")
                    elif (operation == "exec" and "psql" in args
                          and args[-1].startswith("SELECT marker")):
                        return completed(stdout=RUN_ID.encode("ascii"))
                    return completed()
                self.fail(f"unexpected guest command: {args!r}")

            with tempfile.TemporaryDirectory(prefix="lm-guest-workload-") as guest_workspace:
                code = acceptance.GUEST_WORKLOAD_CODE.replace(
                    'Path("/workspace")', f"Path({guest_workspace!r})",
                )
                env = {
                    "LM_WORKLOAD_ID": RUN_ID,
                    "LM_WORKLOAD_RELATIVE": f".lm-acceptance-work-{RUN_ID[:12]}",
                    "LM_WORKLOAD_PROJECT": f"lmacc-{RUN_ID[:12]}",
                }
                output = io.StringIO()
                with mock.patch.dict("os.environ", env, clear=False), \
                        mock.patch("subprocess.run", side_effect=fake_run), \
                        contextlib.redirect_stdout(output):
                    exec(compile(code, "<guest-workload-fixture>", "exec"), {})  # noqa: S102 -- Execute fixed guest script with Docker mocked.
                compose_path = (Path(guest_workspace) / env["LM_WORKLOAD_RELATIVE"] / "compose.yaml")
                compose_text = compose_path.read_text(encoding="ascii") if compose_path.exists() else None
                return json.loads(output.getvalue()), calls, compose_text

        report, calls, compose_text = run_fixture(image_available_after_pull=True)
        self.assertEqual(report["result"], "PASS")
        self.assertTrue(report["checks"]["postgres_image_available"])
        docker_steps = [args[1:4] for args, _kwargs in calls if args[:1] == ["docker"]]
        self.assertEqual(docker_steps[:3], [
            ["image", "inspect", "postgres:16-alpine"],
            ["pull", "postgres:16-alpine"],
            ["image", "inspect", "postgres:16-alpine"],
        ])
        pull_call = next((args, kwargs) for args, kwargs in calls if args[:2] == ["docker", "pull"])
        self.assertEqual(pull_call[1]["timeout"], 180)
        self.assertIn("pull_policy: never", compose_text)
        self.assertTrue(any(args[:2] == ["docker", "compose"] and "--pull" in args
                            and args[args.index("--pull") + 1] == "never" for args, _ in calls))

        blocked, blocked_calls, compose_text = run_fixture(image_available_after_pull=False)
        self.assertEqual(blocked["result"], "BLOCKED")
        self.assertFalse(blocked["checks"]["postgres_image_available"])
        self.assertEqual(sum(args[:2] == ["docker", "pull"] for args, _ in blocked_calls), 1)
        self.assertFalse(any(args[:2] == ["docker", "compose"] for args, _ in blocked_calls))
        self.assertIsNone(compose_text)

    def test_lifecycle_checks_report_workload_canary_retention_after_vm_resume(self):
        profile = acceptance._profile_name(RUN_ID)
        ready = {"status": "ready", "profile_id": "profile", "image_sha256": "a" * 64,
                 "workspace": str(self.source), "runtime_payload_sha256": "b" * 64,
                 "opencode_version": "2.2.5"}
        suspended = {**ready, "status": "suspended"}
        low = {**ready, "memory_gib": 4}
        restored = {**ready, "memory_gib": 8}
        profile_records = [ready, suspended, ready, low, restored]
        calls = []

        def lifecycle(_state, _profile, _workspace, operation, _memory, **_kwargs):
            calls.append(operation)
            text = "CURRENT  selected guest targets already match" if operation == "update-scan" else ""
            return subprocess.CompletedProcess([], 0, text.encode(), b"")

        workload_evidence = {}
        with mock.patch.object(acceptance, "_profile_record", side_effect=profile_records), \
                mock.patch.object(acceptance, "_lifecycle_call", side_effect=lifecycle), \
                mock.patch.object(acceptance, "_hash_file", return_value="c" * 64), \
                mock.patch.object(acceptance, "_overlay_identity", return_value=(1, 2)), \
                mock.patch.object(acceptance, "_listener_owned_loopback", return_value=True), \
                mock.patch.object(acceptance, "_guest_workload_retention", return_value={
                    "database_canary_retained": True, "compose_stopped": True,
                }), \
                mock.patch.object(acceptance, "_guest_memory_mib", side_effect=[8000, 4000, 8000]):
            result = acceptance._lifecycle_retention_checks(
                self.state_root, profile, self.source, 8, False, RUN_ID, workload_evidence,
            )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(workload_evidence["status"], "PASS")
        self.assertTrue(result["workload_retention"]["database_canary_retained"])
        self.assertEqual(calls, ["suspend", "resume", "update-scan", "resize", "resize"])

    def test_provider_stream_evidence_is_schema_only_and_redacted(self):
        valid = json.dumps({
            "provider": "ollama", "streams_attempted": 5,
            "streams_passed": 5, "schema_valid": True,
        }).encode("ascii")
        parsed = acceptance._validate_provider_stream_report(valid, "ollama")
        self.assertEqual(parsed["streams_passed"], 5)
        self.assertNotIn("response", parsed)
        self.assertIsNone(acceptance._validate_provider_stream_report(
            valid[:-1] + b',"response":"model-transcript"}', "ollama",
        ))
        self.assertIsNone(acceptance._validate_provider_stream_report(b"not json", "ollama"))

    def test_openai_live_auth_is_deferred_without_blocking_local_provider(self):
        report = {"provider_config": {"providers": [{"provider": "openai", "model_id": "openai/gpt-4.1"}]},
                  "gates": {"HOST-CLEAN": {"status": "PASS"}}}
        with mock.patch.object(acceptance.sys, "stdin", SimpleNamespace(isatty=lambda: False)), \
                mock.patch.object(acceptance.subprocess, "run") as run:
            result = acceptance._openai_auth_checkpoint(
                self.state_root, RUN_ID, self.source, 8, report, self.base / "report.json",
            )
        self.assertEqual(result["reason_code"], "interactive_terminal_unavailable")
        run.assert_not_called()

        selections = {
            "provider_config": {"providers": [
                {"provider": "ollama", "model_id": "qwen3:8b", "port": 11434},
                {"provider": "openai", "model_id": "openai/gpt-4.1"},
            ], "selections": [
                {"provider": "ollama", "status": "AVAILABLE"},
                {"provider": "openai", "status": "OWNER_AUTH_REQUIRED"},
            ]},
            "early_auth": {"status": "BLOCKED"},
            "owned_profiles": [],
        }
        data = {"profile_id": "profile", "image_sha256": "a" * 64,
                "guest_payload_sha256": "b" * 64, "runtime_payload_sha256": "c" * 64}
        with mock.patch.object(acceptance, "_ensure_provider_profile", return_value=("provider-ollama", data, None)), \
                mock.patch.object(acceptance, "_provider_stream_check", return_value={
                    "status": "PASS", "streams_attempted": 5, "streams_passed": 5,
                }), \
                mock.patch.object(acceptance, "_suspend_profile", return_value=True), \
                mock.patch.object(acceptance, "_resume_campaign_profile", return_value=(data, None)), \
                mock.patch.object(acceptance, "_atomic_replace_json"):
            outcome = acceptance._provider_stream_checks(
                self.state_root, RUN_ID, self.source, selections, 8,
            )
        self.assertEqual(outcome["status"], "PASS")
        statuses = {item["provider"]: item["status"] for item in outcome["providers"]}
        self.assertEqual(statuses, {"ollama": "PASS", "openai": "DEFERRED"})
        self.assertEqual(next(item for item in outcome["providers"]
                              if item["provider"] == "openai")["reason_code"],
                         "owner_deferred_live_openai")

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
                approved_total_minutes=60,
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
            acceptance.execute_campaign(
                self.source, self.state_root, RUN_ID, ["clean"], 8, RUN_ID,
                approved_total_minutes=60,
            )
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

    def test_one_shot_execute_is_disabled_even_with_an_explicit_budget(self):
        with self.assertRaises(SystemExit) as error, mock.patch.object(acceptance, "preflight") as preflight, \
                mock.patch.object(sys, "stderr", io.StringIO()):
            acceptance.main([
                "--source", str(self.source), "--state-root", str(self.state_root),
                "--suite", "all", "--run-id", RUN_ID, "--execute",
                "--approved-total-minutes", "60",
            ])
        self.assertEqual(error.exception.code, 2)
        preflight.assert_not_called()

    @contextlib.contextmanager
    def dev_inspect_fixture(self, *, listener=True, guest_facts_rc=0, proxy_status=200):
        profile = {
            "name": "branch-dev-fixture", "status": "ready", "depth": 0, "max_depth": 2,
            "memory_gib": 4, "workspace": str(self.source),
            "runtime_payload_sha256": "a" * 64, "guest_payload_sha256": "b" * 64,
            "runtime_identity": {"pid": 12345, "start_ticks": 98765,
                                 "executable": "/usr/bin/qemu-system-x86_64", "argv": ["qemu"]},
        }
        facts = {"mester": True, "opencode": True, "workspace_writable": True,
                 "workspace_virtiofs": True, "kvm": True, "cpu_virtualization": True,
                 "memavailable_mib": 2048,
                 "tools": {name: True for name in ("qemu-system-x86_64", "qemu-img", "virtiofsd",
                                                     "socat", "bats", "shellcheck", "cloud-init")}}
        marker = {"status": "ready", "payload_sha256": "b" * 64,
                  "workspace_mount": "virtiofs", "opencode_version": "2.2.5"}
        metrics = {"summary": True, "json": True, "cpu": True, "memory": True}

        def guest_json(_root, _profile, code):
            if code == acceptance.DEV_GUEST_FACTS and guest_facts_rc:
                return None, guest_facts_rc, "guest_command_failed"
            if code == acceptance.DEV_GUEST_FACTS:
                return facts, 0, "guest_probe_completed"
            if code == acceptance.DEV_GUEST_MARKER:
                return marker, 0, "guest_probe_completed"
            if code == acceptance.DEV_GUEST_PROXY:
                return {"connect_status": proxy_status}, 0, "guest_probe_completed"
            if code == acceptance.DEV_GUEST_METRICS:
                return metrics, 0, "guest_probe_completed"
            raise AssertionError("unexpected guest command")

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

        def load_module(name, path):
            if name == "vm_dev_inspect_identity":
                return SimpleNamespace(identity_state=lambda _identity: "running")
            raise AssertionError(f"unexpected module load: {name}")

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(acceptance, "_source_identity", return_value={
                "source_head": "c" * 40, "tree_sha256": "d" * 64,
            }))
            stack.enter_context(mock.patch.object(acceptance, "_private_directory"))
            stack.enter_context(mock.patch.object(acceptance, "_profile_record", return_value=profile))
            stack.enter_context(mock.patch.object(acceptance, "_validate_profile_runtime", return_value=True))
            stack.enter_context(mock.patch.object(acceptance, "_listener_owned_loopback", return_value=listener))
            stack.enter_context(mock.patch.object(acceptance, "_dev_guest_json", side_effect=guest_json))
            stack.enter_context(mock.patch.object(acceptance, "_dev_proxy_details",
                                                  return_value=(self.base, True, False)))
            stack.enter_context(mock.patch.object(acceptance, "_dev_metrics_port", return_value=43210))
            stack.enter_context(mock.patch.object(acceptance, "_dev_host_metrics", return_value=(True, metrics)))
            stack.enter_context(mock.patch.object(acceptance, "_load_module", side_effect=load_module))
            stack.enter_context(mock.patch.object(acceptance, "_read_mem_budget", return_value={"passed": True,
                                                                                               "memavailable_mib": 16384}))
            stack.enter_context(mock.patch.object(acceptance.os, "access", return_value=True))
            stack.enter_context(mock.patch.object(acceptance.shutil, "which", return_value="/mock/tool"))
            stack.enter_context(mock.patch.object(Path, "stat", fake_stat))
            stack.enter_context(mock.patch.object(Path, "read_text", fake_read_text))
            yield profile

    def test_dev_inspect_rejects_campaign_combination_before_any_probe(self):
        with mock.patch.object(acceptance, "run_dev_step") as inspect, \
                mock.patch.object(sys, "stderr", io.StringIO()), self.assertRaises(SystemExit) as error:
            acceptance.main(["--dev-step", "inspect", "--source", str(self.source),
                             "--state-root", str(self.state_root), "--profile", "branch-dev-fixture",
                             "--run-id", RUN_ID, "--suite", "clean"])
        self.assertEqual(error.exception.code, 2)
        inspect.assert_not_called()

    def test_dev_inspect_without_action_consent_is_read_only(self):
        before = list(self.state_root.iterdir())
        with self.dev_inspect_fixture():
            report = acceptance.run_dev_step(self.source, self.state_root, "branch-dev-fixture", RUN_ID, "inspect")
        self.assertEqual(report["result"], "PASS")
        self.assertEqual(list(self.state_root.iterdir()), before)
        self.assertEqual(report["protected_assets_unchanged"]["credentials"], "NOT_INSPECTED")
        self.assertEqual(report["protected_assets_unchanged"]["selected_profile_metadata"], "UNCHANGED_DURING_INSPECT")

    def test_dev_inspect_stale_listener_performs_no_ssh(self):
        with self.dev_inspect_fixture(listener=False), mock.patch.object(acceptance, "_guest_exec") as ssh:
            report = acceptance.run_dev_step(self.source, self.state_root, "branch-dev-fixture", RUN_ID, "inspect")
        ssh.assert_not_called()
        check = next(item for item in report["checks"] if item["id"] == "exact_qemu_ssh_identity")
        self.assertEqual(check["status"], "BLOCKED_EXTERNAL")
        self.assertEqual(check["repair_packet"], "M0-exact-transport")

    def test_dev_inspect_emits_report_when_exact_ssh_listener_is_unavailable(self):
        output = io.StringIO()
        with self.dev_inspect_fixture(listener=False), contextlib.redirect_stdout(output):
            status = acceptance.main(["--dev-step", "inspect", "--source", str(self.source),
                                      "--state-root", str(self.state_root), "--profile", "branch-dev-fixture",
                                      "--run-id", RUN_ID])
        report = json.loads(output.getvalue())
        self.assertEqual(status, 2)
        self.assertEqual(report["step"], "inspect")
        self.assertEqual(report["result"], "BLOCKED_EXTERNAL")
        self.assertIn("checks", report)
        self.assertEqual(report["process_identity_summary"]["state"], "running")

    def test_dev_inspect_preserves_guest_command_rc_and_continues_independent_checks(self):
        with self.dev_inspect_fixture(guest_facts_rc=37):
            report = acceptance.run_dev_step(self.source, self.state_root, "branch-dev-fixture", RUN_ID, "inspect")
        facts = next(item for item in report["checks"] if item["id"] == "guest_facts")
        self.assertEqual(facts["cause_exit_code"], 37)
        self.assertEqual(facts["repair_packet"], "M1-boot")
        self.assertIn("openai_auth_https_connect", {item["id"] for item in report["checks"]})
        self.assertIn("metrics_guest_http", {item["id"] for item in report["checks"]})

    def test_dev_inspect_proxy_403_does_not_hide_metrics_and_names_repairs(self):
        with self.dev_inspect_fixture(proxy_status=403):
            report = acceptance.run_dev_step(self.source, self.state_root, "branch-dev-fixture", RUN_ID, "inspect")
        by_id = {item["id"]: item for item in report["checks"]}
        self.assertEqual(by_id["openai_auth_https_connect"]["observed"]["diagnosed_layer"],
                         "denied_beyond_selected_workspace_policy")
        self.assertEqual(by_id["openai_auth_https_connect"]["repair_packet"], "M2a-auth-egress")
        self.assertEqual(by_id["metrics_host_http"]["status"], "PASS")
        self.assertEqual(by_id["metrics_guest_http"]["status"], "PASS")

    def test_dev_guest_nonzero_exit_is_retained_without_stderr(self):
        completed = subprocess.CompletedProcess(["ssh"], 37, b"", b"private guest output")
        with mock.patch.object(acceptance, "_guest_exec", return_value=completed) as ssh:
            data, cause, reason = acceptance._dev_guest_json(self.state_root, "branch-dev-fixture", "fixed")
        self.assertIsNone(data)
        self.assertEqual(cause, 37)
        self.assertEqual(reason, "guest_command_failed")
        self.assertTrue(ssh.call_args.kwargs["allow_failure"])

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
            "schema_version": 1, "workflow_version": acceptance.WORKFLOW_VERSION, "run_id": RUN_ID,
            "run_name": acceptance._run_name(RUN_ID), "suites": suites,
            "required_gates": acceptance._required_gates(suites),
            "acceptance_scope": acceptance._acceptance_scope(suites),
            "campaign_scope": acceptance._campaign_scope(suites, 8, None, False, 3600),
            "stages": {stage: ("NOT_RUN" if stage == "teardown" else "BLOCKED")
                       for stage in acceptance.STAGE_ORDER},
            "stage_elapsed_ms": {stage: (0 if stage == "teardown" else 10_000)
                                  for stage in acceptance.STAGE_ORDER},
            "legacy_elapsed_ms": 0,
            "approved_total_budget_seconds": 3600,
            "provider_config_sha256": None,
            "gates": gates or {name: {"status": "BLOCKED"} for name in acceptance.GATE_IDS},
            "synthetic_canary": {"status": "PENDING", "encoding_checks": ["raw", "hex", "base64"]},
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
        self.assertNotIn("automated_status", report["gates"]["HUMAN-TUI"])
        self.assertEqual(report["gates"]["HUMAN-TUI"]["pending_reason_code"],
                         "owner_observation_required")
        self.assertEqual(report["gates"]["HUMAN-PERM"]["status"], "BLOCKED")
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.record_human_observation(
                self.state_root, RUN_ID, "f" * 32, "HUMAN-PERM", "PASS", "2.2.5",
                list(acceptance.HUMAN_CHECKLISTS["HUMAN-PERM"]),
            )

    def test_deferred_auth_does_not_block_completed_owner_stage(self):
        _run_root, report_path = self._campaign_report(["human"])
        report = acceptance._read_private_json(report_path)
        for gate in report["required_gates"]:
            report["gates"][gate] = {"status": "PASS"}
        report["gates"]["HOST-CLEAN"] = {"status": "PASS"}
        report["gates"]["HUMAN-TUI"] = {
            "status": "BLOCKED", "reason_code": "owner_observation_required",
        }
        report["gates"]["HUMAN-AUTH"] = {
            "status": "DEFERRED", "reason_code": "owner_deferred_live_openai",
        }
        report["stages"]["providers-daily-permissions"] = "BLOCKED"
        acceptance._atomic_replace_json(report_path, report)
        acceptance.record_human_observation(
            self.state_root, RUN_ID, RUN_ID, "HUMAN-TUI", "PASS", "2.2.5",
            list(acceptance.HUMAN_CHECKLISTS["HUMAN-TUI"]),
        )
        report = acceptance._read_private_json(report_path)
        self.assertEqual(report["stages"]["providers-daily-permissions"], "PASS")
        self.assertEqual(report["gates"]["HUMAN-AUTH"]["status"], "DEFERRED")
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.record_human_observation(
                self.state_root, RUN_ID, RUN_ID, "HUMAN-AUTH", "PASS", "2.2.5",
                list(acceptance.HUMAN_CHECKLISTS["HUMAN-AUTH"]),
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
        run_root, report_path = self._campaign_report(["clean"], owned=[])
        def remove_run(*_args):
            shutil.rmtree(run_root)
            return {"deleted_run": acceptance._run_name(RUN_ID)}

        remover = mock.Mock(side_effect=remove_run)
        with mock.patch.object(
            acceptance, "_load_module", return_value=SimpleNamespace(recursion_run_delete=remover),
        ):
            result = acceptance.cleanup_campaign(self.state_root, RUN_ID, RUN_ID)
        self.assertEqual(result["cleanup"], "PASS")
        remover.assert_called_once_with(
            self.state_root, acceptance._run_name(RUN_ID), acceptance._run_name(RUN_ID), Path("/proc"),
        )
        self.assertEqual(acceptance._read_private_json(report_path)["cleanup"]["status"], "PASS")

    def test_cleanup_removes_only_the_exact_private_canary_and_redacts_final_report(self):
        run_root, report_path = self._campaign_report(["clean"], owned=[])
        acceptance._create_canary_fixture(self.state_root, RUN_ID)
        canary_path = acceptance._canary_path(self.state_root, RUN_ID)
        checker = acceptance._load_module("fixture_canary", acceptance.CANARY_CHECK_PATH)
        canary = checker.read_canary(canary_path)
        report = acceptance._read_private_json(report_path)
        report["synthetic_canary"] = {
            "status": "GENERATED", "encoding_checks": ["raw", "hex", "base64"],
        }
        acceptance._atomic_replace_json(report_path, report)
        original_load_module = acceptance._load_module

        def remove_run(*_args):
            shutil.rmtree(run_root)
            return {"deleted_run": acceptance._run_name(RUN_ID)}

        def load_module(name, path):
            if name == "vm_host_acceptance_canary_cleanup":
                return checker
            if name == "vm_host_acceptance_canary_finalize":
                return checker
            if name == "vm_host_acceptance_paths_cleanup":
                return SimpleNamespace(recursion_run_delete=remove_run)
            return original_load_module(name, path)

        with mock.patch.object(acceptance, "_load_module", side_effect=load_module):
            result = acceptance.cleanup_campaign(self.state_root, RUN_ID, RUN_ID)

        self.assertEqual(result["cleanup"], "PASS")
        self.assertFalse(canary_path.exists())
        final_report = acceptance._read_private_json(report_path)
        self.assertEqual(final_report["synthetic_canary"]["status"], "REMOVED")
        self.assertTrue(final_report["cleanup"]["synthetic_canary_removed"])
        self.assertEqual(final_report["gates"]["SEC-SECRET"]["final_report_check"]["status"], "PASS")
        self.assertEqual(
            final_report["gates"]["SEC-SECRET"]["required_subchecks"]
            ["canary-absent-from-campaign-and-self-test-reports"], "PASS",
        )
        raw_report = report_path.read_bytes()
        self.assertNotIn(canary, raw_report)
        self.assertNotIn(canary.hex().encode("ascii"), raw_report)

    def test_cleanup_gracefully_suspends_owned_profiles_deepest_first_before_delete(self):
        owned = [
            {"profile_name": acceptance._profile_name(RUN_ID), "depth": 0, "run_id": RUN_ID},
            {"profile_name": acceptance._profile_name(RUN_ID, "agent"), "depth": 1, "run_id": RUN_ID},
        ]
        run_root, _report_path = self._campaign_report(["clean"], owned=owned)
        child_state = run_root / "state"
        (child_state / "vm/profiles").mkdir(mode=0o700, parents=True)
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
            if operation == "delete":
                shutil.rmtree(_state / "vm/profiles" / name)
            return SimpleNamespace(returncode=0)

        def remove_run(*_args):
            shutil.rmtree(run_root)
            return {"deleted_run": acceptance._run_name(RUN_ID)}

        remover = mock.Mock(side_effect=remove_run)
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
        self.assertIn('"$((deadline + 5))"', run.call_args.args[0][2])
        self.assertIn("timeout --foreground --signal=TERM --kill-after=2s ${deadline}s", run.call_args.args[0][2])

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

    def test_lifecycle_suite_does_not_count_update_noop_as_selected_apply(self):
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
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason_code"], "selected_update_apply_not_performed")
        self.assertIn(("update-scan", 8), operations)
        self.assertIn(("update-apply", 8), operations)
        self.assertNotIn(("resize", 4), operations)

    def test_lifecycle_suite_records_a_real_selected_update_apply(self):
        base = {
            "status": "ready", "profile_id": "profile-id", "image_sha256": "a" * 64,
            "workspace": str(self.source), "runtime_payload_sha256": "b" * 64,
            "runtime_identity": {"pid": 1}, "opencode_version": "2.2.5",
        }
        records = [base, {**base, "status": "suspended", "runtime_identity": None}, base, base,
                   {**base, "memory_gib": 4}, {**base, "memory_gib": 8}]
        operations = []

        def lifecycle(_state, _profile, _workspace, operation, memory, **_kwargs):
            operations.append((operation, memory))
            output = {
                "update-scan": b"CANCELLED  guest and profile state were not changed\n",
                "update-apply": b"UPDATED  selected guest updates completed; exact targets applied.\n",
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
        self.assertTrue(result["resize_restore_verified"])
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
