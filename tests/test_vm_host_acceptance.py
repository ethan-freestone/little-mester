"""Safe local regressions for the owner-host acceptance runner."""

from __future__ import annotations

import ast
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from unittest.mock import call, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("vm_host_acceptance", ROOT / "scripts/vm-host-acceptance.py")
acceptance = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(acceptance)


class DummyEvidence:
    def __init__(self, directory=None):
        self.directory = Path(directory or tempfile.mkdtemp(prefix="vm-acceptance-test-"))
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.data = {"checks": [], "observations": []}

    def check(self, name, passed, detail):
        self.data["checks"].append({"name": name, "result": "PASS" if passed else "FAIL", "detail": detail})

    def blocked(self, name, detail):
        self.data["checks"].append({"name": name, "result": "BLOCKED", "detail": detail})

    def save(self):
        pass

    def finish(self, outcome):
        self.data["outcome"] = outcome


def payload_bytes(item):
    return base64.b64decode(item["content"], validate=True)


def create_snapshot_source_parents(*roots):
    for root in roots:
        for relative in acceptance.SNAPSHOT_FILES:
            (root / relative).parent.mkdir(parents=True, exist_ok=True)


class HostAcceptanceTests(unittest.TestCase):
    def test_isolation_result_requires_qemu_listener_and_guest_probe_passes(self):
        output = (
            "QEMU configuration: restrict=on; SSH bound to 127.0.0.1:23456; only the four reviewed guestfwd channels are configured.\n"
            "QEMU TCP listeners: SSH and egress proxy are loopback-only.\n"
            "PASS: direct route: blocked\n"
            "SKIP: selected model availability (not part of isolation-only acceptance)\n"
            "Result: 1 passed, 0 failed\n"
        )
        result = acceptance.parse_isolation_result(output, 0)
        self.assertTrue(result["qemu_argv_validated"])
        self.assertTrue(result["loopback_listeners_validated"])
        self.assertTrue(result["result_summary_parsed"])
        self.assertTrue(result["model_check_skipped"])
        self.assertTrue(result["passed"])
        self.assertEqual(result["passed_checks"], 1)
        self.assertEqual(result["skipped_checks"], 1)

        self.assertFalse(acceptance.parse_isolation_result(
            output.replace("QEMU TCP listeners: SSH and egress proxy are loopback-only.\n", ""), 0
        )["passed"])
        self.assertFalse(acceptance.parse_isolation_result(
            output.replace("Result: 1 passed, 0 failed", "Result: 2 passed, 0 failed"), 0
        )["passed"])
        self.assertFalse(acceptance.parse_isolation_result(
            output.replace("PASS: direct route: blocked", "FAIL: direct route: reachable")
                .replace("Result: 1 passed, 0 failed", "Result: 0 passed, 1 failed"), 0
        )["passed"])
        self.assertFalse(acceptance.parse_isolation_result(output, 1)["passed"])

    def test_live_isolation_check_is_read_only_skips_model_and_records_sanitized_counts(self):
        output = (
            "QEMU configuration: restrict=on; SSH bound to 127.0.0.1:23456; only the four reviewed guestfwd channels are configured.\n"
            "QEMU TCP listeners: SSH and egress proxy are loopback-only.\n"
            "PASS: private destination denied\n"
            "SKIP: selected model availability (not part of isolation-only acceptance)\n"
            "Result: 1 passed, 0 failed\n"
        )
        evidence = DummyEvidence()
        with patch.dict(os.environ, {"VM_ISOLATION_SKIP_MODEL": "0"}), \
             patch.object(acceptance.subprocess, "run", return_value=subprocess.CompletedProcess(
                 ["lab-vm"], 0, output, "sensitive stderr must not be recorded"
             )) as mocked_run:
            passed = acceptance.run_isolation_check("profile-safe", evidence, "outer", ROOT)

        self.assertTrue(passed)
        command = mocked_run.call_args.args[0]
        self.assertEqual(command, [str(ROOT / "lab-vm"), "verify-isolation", "profile-safe"])
        self.assertEqual(mocked_run.call_args.kwargs["env"]["VM_ISOLATION_SKIP_MODEL"], "1")
        self.assertEqual(mocked_run.call_args.kwargs["timeout"], 120)
        serialized = json.dumps(evidence.data, sort_keys=True)
        self.assertNotIn("23456", serialized)
        self.assertNotIn("sensitive stderr", serialized)
        self.assertEqual(evidence.data["outer_isolation"]["passed_checks"], 1)

    def test_live_isolation_timeout_is_a_sanitized_failure(self):
        evidence = DummyEvidence()
        timeout = subprocess.TimeoutExpired(["lab-vm"], 120, output="private-port 23456")
        with patch.object(acceptance.subprocess, "run", side_effect=timeout):
            self.assertFalse(acceptance.run_isolation_check("profile-safe", evidence, "outer", ROOT))
        self.assertTrue(evidence.data["outer_isolation"]["timed_out"])
        self.assertFalse(evidence.data["outer_isolation"]["passed"])
        self.assertNotIn("23456", json.dumps(evidence.data))

    def test_live_isolation_refuses_invalid_profile_before_subprocess(self):
        evidence = DummyEvidence()
        with patch.object(acceptance.subprocess, "run") as mocked_run:
            self.assertFalse(acceptance.run_isolation_check("../default", evidence, "outer", ROOT))
        mocked_run.assert_not_called()

    def test_context_summary_drops_paths_and_unrelated_cpu_flags(self):
        raw = '{"runtime":{"kind":"vm","root":"/private/path"},"recursion":{"depth":0,"max_depth":1},"kvm":{"device_present":true,"device_read_write":true,"nested_kvm_preflight_ready":true,"cpu_virtualization":["svm","aes"]},"workspace_filesystem":"virtiofs"}'
        result = acceptance.safe_context(raw)
        self.assertEqual(result["runtime_kind"], "vm")
        self.assertEqual(result["cpu_virtualization"], ["svm"])
        self.assertNotIn("root", result)
        self.assertIsNone(acceptance.safe_context("not-json"))

    def test_profile_name_validation_rejects_path_traversal(self):
        for name in ("../default", "a/b", "", "-leading"):
            with self.subTest(name=name), self.assertRaises(acceptance.AcceptanceError):
                acceptance.profile_dir(name)
        self.assertTrue(acceptance.PROFILE_RE.fullmatch("recursion-proxy-20261101"))

    def test_profile_base_preserves_explicit_override_and_never_falls_back_to_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir()
            repo = Path(directory) / "repo"
            repo.mkdir()
            (repo / ".mester/vm/default").mkdir(parents=True)
            with patch.dict(os.environ, {"HOME": str(home), "VM_DIR_BASE": "", "VM_STATE_DIR": "", "MESTER_STATE_DIR": "", "XDG_DATA_HOME": ""}, clear=True):
                self.assertEqual(acceptance.profile_base(repo), Path("/"))
                os.environ.pop("VM_DIR_BASE")
                self.assertEqual(acceptance.profile_base(repo), home / ".local/share/little-mester/vm")
            with patch.dict(os.environ, {"HOME": str(home), "VM_STATE_DIR": "", "XDG_DATA_HOME": "/data"}, clear=True):
                self.assertEqual(acceptance.profile_base(repo), Path("/data/little-mester/vm"))
            with patch.dict(os.environ, {
                "HOME": str(home),
                "MESTER_STATE_DIR": str(home / "custom-state"),
                "XDG_DATA_HOME": "/data",
            }, clear=True):
                self.assertEqual(acceptance.profile_base(repo), home / "custom-state/vm")
            with patch.dict(os.environ, {"VM_DIR_BASE": "/private/test-vm"}, clear=True):
                self.assertEqual(acceptance.profile_base(repo), Path("/private/test-vm"))

    def test_snapshot_target_rejects_symlinked_parent_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            snapshot, outside = base / "snapshot", base / "outside"
            snapshot.mkdir()
            outside.mkdir()
            (outside / "proxy.py").write_text("outside")
            (snapshot / "lib").symlink_to(outside, target_is_directory=True)
            self.assertFalse(acceptance.safe_snapshot_target(snapshot, snapshot / "lib/proxy.py"))

    def test_snapshot_preflight_auto_syncs_stale_allowlisted_sources_with_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            checkout, snapshot = base / "checkout", base / "snapshot"
            checkout.mkdir()
            snapshot.mkdir()
            create_snapshot_source_parents(checkout, snapshot)
            for relative in acceptance.SNAPSHOT_FILES:
                (checkout / relative).write_text(relative)
                (snapshot / relative).write_text(relative)
            evidence = DummyEvidence(base / "evidence")
            with patch.object(acceptance, "ROOT", checkout), patch.object(acceptance, "DEFAULT_SNAPSHOT", snapshot):
                self.assertTrue(acceptance.snapshot_checks(snapshot, evidence, checkout))
                (snapshot / "lib/vm/proxy.py").write_text("unreviewed difference")
                self.assertTrue(acceptance.snapshot_checks(snapshot, evidence, checkout))
            self.assertEqual((snapshot / "lib/vm/proxy.py").read_text(), "lib/vm/proxy.py")
            backup = Path(evidence.data["snapshot_source_backup"])
            self.assertEqual((backup / "lib/vm/proxy.py").read_text(), "unreviewed difference")

    def test_snapshot_source_repair_refuses_active_export_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            checkout, snapshot = base / "checkout", base / "snapshot"
            create_snapshot_source_parents(checkout, snapshot)
            for relative in acceptance.SNAPSHOT_FILES:
                (checkout / relative).write_text(f"current:{relative}")
                (snapshot / relative).write_text(f"old:{relative}")
            evidence = DummyEvidence(base / "evidence")
            with patch.object(acceptance, "active_snapshot_users", return_value=["123"]):
                self.assertFalse(acceptance.sync_snapshot_sources(snapshot, evidence, checkout))
            for relative in acceptance.SNAPSHOT_FILES:
                self.assertEqual((snapshot / relative).read_text(), f"old:{relative}")

    def test_nested_snapshot_check_uses_private_overlay_instead_of_mutating_active_export(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            checkout, snapshot = base / "checkout", base / ".cache/lab-vm-recursion/initial-launcher-smoke/workspace"
            create_snapshot_source_parents(checkout, snapshot)
            for relative in acceptance.SNAPSHOT_FILES:
                (checkout / relative).write_text(f"current:{relative}")
                (snapshot / relative).write_text(f"old:{relative}")
            before = {relative: (snapshot / relative).read_bytes() for relative in acceptance.SNAPSHOT_FILES}
            evidence = DummyEvidence(base / "evidence")
            with patch.object(acceptance, "DEFAULT_SNAPSHOT", snapshot), \
                 patch.object(acceptance, "active_snapshot_users", return_value=["123"]):
                self.assertTrue(acceptance.snapshot_checks(
                    snapshot, evidence, checkout, allow_active_source_overlay=True,
                ))
            self.assertTrue(evidence.data["snapshot_source_overlay_required"])
            self.assertEqual(evidence.data["snapshot_file_parity"], {relative: False for relative in acceptance.SNAPSHOT_FILES})
            for relative in acceptance.SNAPSHOT_FILES:
                self.assertEqual((snapshot / relative).read_bytes(), before[relative])
            preflight_evidence = DummyEvidence(base / "preflight-evidence")
            with patch.object(acceptance, "DEFAULT_SNAPSHOT", snapshot), \
                 patch.object(acceptance, "active_snapshot_users", return_value=["123"]):
                self.assertFalse(acceptance.snapshot_checks(snapshot, preflight_evidence, checkout))
            for relative in acceptance.SNAPSHOT_FILES:
                self.assertEqual((snapshot / relative).read_bytes(), before[relative])

    def test_read_only_snapshot_preflight_refuses_rebuild_and_source_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            checkout = base / "checkout"
            snapshot = base / ".cache/lab-vm-recursion/initial-launcher-smoke/workspace"
            create_snapshot_source_parents(checkout, snapshot)
            for relative in acceptance.SNAPSHOT_FILES:
                (checkout / relative).write_text(f"current:{relative}")
                (snapshot / relative).write_text(f"old:{relative}")
            before = {relative: (snapshot / relative).read_bytes() for relative in acceptance.SNAPSHOT_FILES}
            evidence = DummyEvidence(base / "evidence")
            with patch.object(acceptance, "DEFAULT_SNAPSHOT", snapshot), \
                 patch.object(acceptance, "active_snapshot_users", return_value=[]), \
                 patch.object(acceptance, "sync_snapshot_sources", side_effect=AssertionError("source sync is forbidden")), \
                 patch.object(acceptance, "rebuild_safe_snapshot", side_effect=AssertionError("snapshot rebuild is forbidden")):
                self.assertFalse(acceptance.snapshot_checks(
                    snapshot, evidence, checkout,
                    allow_active_source_overlay=True, allow_repair=False,
                ))
            self.assertEqual(
                next(item for item in evidence.data["checks"] if item["name"] == "snapshot source repair")["result"],
                "FAIL",
            )
            for relative in acceptance.SNAPSHOT_FILES:
                self.assertEqual((snapshot / relative).read_bytes(), before[relative])

            missing = base / "missing-snapshot"
            missing_evidence = DummyEvidence(base / "missing-evidence")
            with patch.object(acceptance, "DEFAULT_SNAPSHOT", missing), \
                 patch.object(acceptance, "rebuild_safe_snapshot", side_effect=AssertionError("snapshot rebuild is forbidden")):
                self.assertFalse(acceptance.snapshot_checks(
                    missing, missing_evidence, checkout, allow_repair=False,
                ))
            self.assertFalse(missing.exists())

    def test_active_source_overlay_payload_is_allowlisted_and_hash_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            create_snapshot_source_parents(repo)
            contents = {}
            for relative in acceptance.SNAPSHOT_FILES:
                contents[relative] = f"patched:{relative}".encode()
                (repo / relative).write_bytes(contents[relative])
            payload = acceptance.active_source_overlay_payload(repo)
        self.assertEqual(set(payload), set(acceptance.SNAPSHOT_FILES))
        for relative, item in payload.items():
            data = base64.b64decode(item["content"], validate=True)
            self.assertEqual(data, contents[relative])
            self.assertEqual(item["sha256"], hashlib.sha256(data).hexdigest())

    def test_remote_active_source_overlay_stages_verified_files_privately(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            create_snapshot_source_parents(workspace)
            (workspace / ".secrets").mkdir()
            launcher = workspace / "lab-vm"
            launcher.write_text("#!/bin/sh\nexit 0\n")
            launcher.chmod(0o755)
            (workspace / "scripts/helper.py").write_text("helper")
            (workspace / ".secrets/token").write_text("private")
            (workspace / "lib/vm/support.sh").write_text("support")
            original = {}
            for relative in acceptance.SNAPSHOT_FILES:
                original[relative] = f"snapshot:{relative}".encode()
                (workspace / relative).write_bytes(original[relative])
            payload = acceptance.active_source_overlay_payload(workspace)
            tree = ast.parse(acceptance.REMOTE_NESTED)
            definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                           and node.name in {"stage_runtime_overlay", "populate_runtime_overlay"}]
            namespace = {
                "base64": base64,
                "hashlib": hashlib,
                "os": os,
                "pathlib": __import__("pathlib"),
                "shutil": shutil,
                "tempfile": tempfile,
                "root": workspace,
                "source_overlay_payload": payload,
            }
            exec(compile(ast.Module(body=definitions, type_ignores=[]), "remote-overlay", "exec"), namespace)
            stage = namespace["stage_runtime_overlay"]()
            try:
                self.assertEqual(stage.stat().st_mode & 0o777, 0o700)
                self.assertEqual((stage / "lab-vm").read_bytes(), launcher.read_bytes())
                self.assertFalse((stage / "lab-vm").is_symlink())
                self.assertFalse((stage / ".secrets").exists())
                self.assertFalse((stage / "scripts").is_symlink())
                self.assertTrue((stage / "scripts").is_dir())
                self.assertTrue((stage / "scripts/helper.py").is_symlink())
                self.assertFalse((stage / "scripts/vm-guest-isolation-check.py").is_symlink())
                self.assertTrue((stage / "lib/vm/support.sh").is_symlink())
                for relative in acceptance.SNAPSHOT_FILES:
                    self.assertEqual((stage / relative).read_bytes(), payload_bytes(payload[relative]))
                    self.assertEqual((workspace / relative).read_bytes(), original[relative])
            finally:
                shutil.rmtree(stage)

    def test_remote_active_source_overlay_rejects_tampering_and_cleans_partial_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            create_snapshot_source_parents(workspace)
            launcher = workspace / "lab-vm"
            launcher.write_text("#!/bin/sh\nexit 0\n")
            for relative in acceptance.SNAPSHOT_FILES:
                (workspace / relative).write_text(relative)
            payload = acceptance.active_source_overlay_payload(workspace)
            payload[acceptance.SNAPSHOT_FILES[0]]["sha256"] = "0" * 64
            stage = Path(directory) / "overlay"
            tree = ast.parse(acceptance.REMOTE_NESTED)
            definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                           and node.name in {"stage_runtime_overlay", "populate_runtime_overlay"}]
            namespace = {
                "base64": base64,
                "hashlib": hashlib,
                "os": os,
                "pathlib": __import__("pathlib"),
                "shutil": shutil,
                "tempfile": type("Temp", (), {"mkdtemp": staticmethod(lambda **_: (stage.mkdir() or str(stage)))}),
                "root": workspace,
                "source_overlay_payload": payload,
            }
            exec(compile(ast.Module(body=definitions, type_ignores=[]), "remote-overlay", "exec"), namespace)
            with self.assertRaisesRegex(ValueError, "hash does not match"):
                namespace["stage_runtime_overlay"]()
            self.assertFalse(stage.exists())

    def test_snapshot_rebuild_excludes_host_state_and_preserves_old_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            checkout = base / "checkout"
            snapshot = base / ".cache/lab-vm-recursion/initial-launcher-smoke/workspace"
            checkout.mkdir()
            create_snapshot_source_parents(checkout)
            (checkout / "scripts").mkdir(exist_ok=True)
            (checkout / "scripts/vm-recursion-workspace.py").write_bytes((ROOT / "scripts/vm-recursion-workspace.py").read_bytes())
            for relative in acceptance.SNAPSHOT_FILES:
                (checkout / relative).write_text(f"source:{relative}")
            snapshot.mkdir(parents=True)
            (snapshot / ".mester").mkdir()
            (snapshot / ".mester/private-marker").write_text("preserve me")
            (snapshot / ".workspace/.mester").mkdir(parents=True)
            (snapshot / ".workspace/.mester/nested-marker").write_text("preserve me too")
            evidence = DummyEvidence(base / "evidence")
            with patch.object(acceptance, "ROOT", checkout), patch.object(acceptance, "DEFAULT_SNAPSHOT", snapshot):
                with patch.object(acceptance, "active_snapshot_users", return_value=[]):
                    self.assertTrue(acceptance.snapshot_checks(snapshot, evidence, checkout))
            for relative in acceptance.SNAPSHOT_FILES:
                self.assertEqual((snapshot / relative).read_text(), f"source:{relative}")
            self.assertFalse((snapshot / ".mester").exists())
            self.assertFalse((snapshot / ".workspace/.mester").exists())
            backup = Path(evidence.data["snapshot_backup"])
            self.assertEqual((backup / ".mester/private-marker").read_text(), "preserve me")
            self.assertEqual((backup / ".workspace/.mester/nested-marker").read_text(), "preserve me too")

    def test_snapshot_rebuild_refuses_an_active_export(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            snapshot = base / ".cache/lab-vm-recursion/initial-launcher-smoke/workspace"
            snapshot.mkdir(parents=True)
            (snapshot / ".mester").mkdir()
            evidence = DummyEvidence(base / "evidence")
            with patch.object(acceptance, "DEFAULT_SNAPSHOT", snapshot), \
                 patch.object(acceptance, "active_snapshot_users", return_value=["123"]):
                self.assertFalse(acceptance.rebuild_safe_snapshot(snapshot, evidence))
            self.assertTrue((snapshot / ".mester").is_dir())
            self.assertFalse(any("backup" in p.name for p in snapshot.parent.iterdir()))

    def test_missing_outer_profile_is_bootstrapped_without_destructive_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            snapshot = base / "snapshot"
            snapshot.mkdir()
            profile_root = base / "profiles"
            evidence = DummyEvidence(base / "evidence")
            captured = {}

            def fake_run(args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs
                return subprocess.CompletedProcess(args, 0, "", "")

            with patch.dict(os.environ, {"VM_DIR_BASE": str(profile_root)}), \
                 patch.object(acceptance, "LAB_VM", base / "lab-vm"), \
                 patch.object(acceptance, "memory_budget_ok", return_value=True), \
                 patch.object(acceptance, "active_snapshot_users", return_value=[]), \
                 patch.object(acceptance, "run", side_effect=fake_run):
                selected = acceptance.ensure_outer_profile("launcher-smoke", snapshot, ROOT, evidence)
            self.assertEqual(selected, ("launcher-smoke", profile_root / "launcher-smoke"))
            args = captured["args"]
            kwargs = captured["kwargs"]
            self.assertIn("--workspace", args)
            self.assertIn("--max-depth", args)
            self.assertIn("--memory", args)
            self.assertTrue(kwargs["no_input"])
            self.assertNotIn("--yes", args)
            self.assertNotIn("--clean", args)
            self.assertNotIn("stop", args)
            self.assertNotIn("down", args)

    def test_evidence_file_and_directory_are_private(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_STATE_HOME": directory}):
            evidence = acceptance.Evidence("test", "private report")
            evidence.check("sample", True, "ok")
            self.assertEqual(evidence.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(evidence.directory.stat().st_mode & 0o777, 0o700)

    def test_auth_observation_records_only_fixed_answers_and_version(self):
        answers = ["y", "v2.2.5", "host-browser-bridge", "y", "y", "y", "y", "y", "y"]
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_STATE_HOME": directory}), \
             patch("builtins.input", side_effect=answers), redirect_stdout(io.StringIO()):
            result = acceptance.observe_gate("auth")
            report = next((Path(directory) / "little-mester/host-acceptance").glob("*.json"))
            data = json.loads(report.read_text())
        self.assertEqual(result, 0)
        observation = data["observations"][0]
        self.assertEqual(observation["opencode_version"], "v2.2.5")
        self.assertEqual(observation["auth_method"], "host-browser-bridge")
        self.assertEqual(observation["sensitive_material_recorded"], "no")
        self.assertFalse(any(key in observation for key in ("url", "token", "prompt", "raw_log")))

    def test_unknown_human_observation_is_blocked_not_recorded_as_failure(self):
        answers = ["y", "v2.2.5", "host-browser-bridge", "u", "y", "y", "y", "y", "y"]
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_STATE_HOME": directory}), \
             patch("builtins.input", side_effect=answers), redirect_stdout(io.StringIO()):
            result = acceptance.observe_gate("auth")
            report = next((Path(directory) / "little-mester/host-acceptance").glob("*.json"))
            data = json.loads(report.read_text())
        self.assertEqual(result, 1)
        self.assertEqual(data["checks"][-1]["result"], "BLOCKED")
        self.assertEqual(data["outcome"], "REPORTED_INCOMPLETE")

    def test_preflight_never_ssh_connects_from_non_host_context(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "abc", "")

        with patch.object(acceptance, "host_context", return_value={
            "runtime_kind": "vm", "device_present": True, "device_read_write": True,
            "nested_kvm_preflight_ready": True, "cpu_virtualization": ["svm"],
        }), patch.object(acceptance, "host_tools", return_value=True), \
             patch.object(acceptance, "snapshot_checks") as snapshot_checks, \
             patch.object(acceptance, "outer_profile_checks", side_effect=AssertionError("must not inspect/SSH outer profile")), \
             patch.object(acceptance, "ensure_outer_profile", side_effect=AssertionError("must not start a VM")), \
             patch.object(acceptance, "run", side_effect=fake_run):
            evidence = DummyEvidence()
            ready, outer = acceptance.run_preflight(evidence, "launcher-smoke", Path("/unused"))
        self.assertFalse(ready)
        self.assertIsNone(outer)
        snapshot_checks.assert_not_called()
        self.assertFalse(any("up" in call for call in calls))

    def test_outer_guest_missing_tools_are_bootstrapped_then_rechecked(self):
        base_payload = {
            "context": {
                "runtime_kind": "vm", "depth": 0, "max_depth": 1,
                "child_depth_available": True, "device_present": True,
                "device_read_write": True, "nested_kvm_preflight_ready": True,
                "cpu_virtualization": ["svm"],
            },
            "workspace_fstype": "virtiofs",
            "checks": {
                "context_valid": True, "outer_guest": True, "nested_kvm": True,
                "workspace_root": True, "workspace_virtiofs": True,
                "host_state_excluded": True, "memory_budget": True,
            },
            "tools": {
                "qemu-system-x86_64": False, "qemu-img": False,
                "socat": True, "virtiofsd": True,
            },
            "memory_budget": {"requested_mib": 4096},
        }
        evidence = DummyEvidence()

        def fake_run(args, **kwargs):
            script = kwargs.get("input_text")
            if script == acceptance.REMOTE_PREFLIGHT:
                payload = json.loads(json.dumps(base_payload))
                if not hasattr(fake_run, "rechecked"):
                    fake_run.rechecked = True
                    payload["checks"]["tools"] = False
                    payload["tools"]["qemu-system-x86_64"] = False
                    payload["tools"]["qemu-img"] = False
                else:
                    payload["checks"]["tools"] = True
                    payload["tools"]["qemu-system-x86_64"] = True
                    payload["tools"]["qemu-img"] = True
                return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
            self.assertEqual(script, acceptance.REMOTE_ENSURE_NESTED_TOOLS)
            install = {"packages": ["qemu-system-x86", "qemu-utils"], "missing_tools": [], "install_attempted": True}
            return subprocess.CompletedProcess(args, 0, json.dumps(install), "")

        with patch.object(acceptance, "ssh_command", return_value=["ssh"]), \
             patch.object(acceptance, "run", side_effect=fake_run), \
             redirect_stdout(io.StringIO()):
            self.assertTrue(acceptance.outer_guest_preflight(Path("/profile"), evidence))
        tool_check = next(check for check in evidence.data["checks"] if check["name"] == "outer guest nested tools")
        self.assertIn("qemu-img, qemu-system-x86_64", tool_check["detail"])
        bootstrap = next(check for check in evidence.data["checks"] if check["name"] == "outer guest nested-tool bootstrap")
        self.assertEqual(bootstrap["result"], "PASS")
        self.assertEqual(evidence.data["outer_guest_bootstrap_packages"], ["qemu-system-x86", "qemu-utils"])

    def test_read_only_outer_guest_preflight_refuses_nested_tool_install(self):
        payload = {
            "context": {"runtime_kind": "vm", "depth": 0, "max_depth": 1,
                        "child_depth_available": True, "device_present": True,
                        "device_read_write": True, "nested_kvm_preflight_ready": True,
                        "cpu_virtualization": ["svm"]},
            "workspace_fstype": "virtiofs",
            "checks": {"context_valid": True, "outer_guest": True, "nested_kvm": True,
                        "workspace_root": True, "workspace_virtiofs": True,
                        "host_state_excluded": True, "tools": False},
            "tools": {"qemu-system-x86_64": False, "qemu-img": False},
            "memory_budget": {},
        }
        evidence = DummyEvidence()
        response = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with patch.object(acceptance, "ssh_command", return_value=["ssh"]), \
             patch.object(acceptance, "run", return_value=response) as mocked_run, \
             redirect_stdout(io.StringIO()):
            self.assertFalse(acceptance.outer_guest_preflight(
                Path("/profile"), evidence, allow_tool_bootstrap=False,
            ))
        mocked_run.assert_called_once()
        self.assertEqual(mocked_run.call_args.kwargs["input_text"], acceptance.REMOTE_PREFLIGHT)
        no_install = next(item for item in evidence.data["checks"] if item["name"] == "outer guest nested tools")
        self.assertIn("read-only continuation will not install tools", no_install["detail"])
        self.assertFalse(any(item["name"] == "outer guest nested-tool bootstrap" for item in evidence.data["checks"]))

    def test_nested_guest_tools_are_not_installed_if_another_preflight_fails(self):
        payload = {
            "context": {"runtime_kind": "vm", "depth": 0, "max_depth": 1,
                        "child_depth_available": True, "device_present": True,
                        "device_read_write": True, "nested_kvm_preflight_ready": False,
                        "cpu_virtualization": []},
            "workspace_fstype": "virtiofs",
            "checks": {"context_valid": True, "outer_guest": True, "nested_kvm": False,
                        "workspace_root": True, "workspace_virtiofs": True,
                        "host_state_excluded": True, "tools": False, "memory_budget": True},
            "tools": {"qemu-system-x86_64": False},
            "memory_budget": {},
        }
        incomplete = json.loads(json.dumps(payload))
        incomplete["checks"]["nested_kvm"] = True
        del incomplete["checks"]["workspace_virtiofs"]
        unexpected_failure = json.loads(json.dumps(incomplete))
        unexpected_failure["checks"]["workspace_virtiofs"] = True
        unexpected_failure["checks"]["future_gate"] = False
        for blocked_payload in (payload, incomplete, unexpected_failure):
            with self.subTest(checks=blocked_payload["checks"]):
                response = subprocess.CompletedProcess([], 0, json.dumps(blocked_payload), "")
                evidence = DummyEvidence()
                with patch.object(acceptance, "ssh_command", return_value=["ssh"]), \
                     patch.object(acceptance, "run", return_value=response) as mocked_run, \
                     redirect_stdout(io.StringIO()):
                    self.assertFalse(acceptance.outer_guest_preflight(Path("/profile"), evidence))
                mocked_run.assert_called_once()
                self.assertFalse(any(check["name"] == "outer guest nested-tool bootstrap" for check in evidence.data["checks"]))

    def test_outer_guest_memory_block_records_component_budget_and_reason(self):
        payload = {
            "context": {
                "runtime_kind": "vm", "depth": 0, "max_depth": 1,
                "child_depth_available": True, "device_present": True,
                "device_read_write": True, "nested_kvm_preflight_ready": True,
                "cpu_virtualization": ["svm"],
            },
            "workspace_fstype": "virtiofs",
            "checks": {
                "context_valid": True, "outer_guest": True, "nested_kvm": True,
                "workspace_root": True, "workspace_virtiofs": True,
                "host_state_excluded": True, "tools": True, "memory_budget": False,
            },
            "tools": {"qemu-system-x86_64": True},
            "memory_budget": {
                "memavailable_mib": 3800, "cgroup_remaining": "4096 MiB",
                "cgroup_remaining_mib": 4096, "releasable_qemu_rss_mib": 0,
                "projected_available_mib": 3800, "available_mib": 3800,
                "requested_mib": 4096, "reserve_mib": 512,
                "measurement_parsed": True, "command_returncode": 1,
                "stderr_present": True, "error_category": "insufficient_memory_budget",
            },
        }
        evidence = DummyEvidence()
        response = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with patch.object(acceptance, "ssh_command", return_value=["ssh"]), \
             patch.object(acceptance, "run", return_value=response), \
             redirect_stdout(io.StringIO()):
            self.assertFalse(acceptance.outer_guest_preflight(Path("/profile"), evidence))
        memory_check = next(check for check in evidence.data["checks"] if check["name"] == "outer guest memory_budget")
        self.assertIn("MemAvailable=3800 MiB", memory_check["detail"])
        self.assertIn("cgroup remaining=4096 MiB", memory_check["detail"])
        self.assertIn("requested=4096 MiB + reserve=512 MiB", memory_check["detail"])
        self.assertEqual(evidence.data["nested_memory_budget"], payload["memory_budget"])

        resumed = DummyEvidence()
        with patch.object(acceptance, "ssh_command", return_value=["ssh"]), \
             patch.object(acceptance, "run", return_value=response), \
             redirect_stdout(io.StringIO()):
            self.assertTrue(acceptance.outer_guest_preflight(
                Path("/profile"), resumed, require_child_memory_budget=False,
            ))
        self.assertFalse(any(check["name"] == "outer guest memory_budget" for check in resumed.data["checks"]))
        self.assertIn("already-running child", resumed.data["nested_memory_budget_skipped_reason"])

    def test_nested_gate_blocks_checks_not_reached_after_child_up_failure(self):
        evidence = DummyEvidence()
        summary = {"checks": {"child_up": False}}
        with patch.object(acceptance, "Evidence", return_value=evidence), \
             patch.object(acceptance, "run_preflight", return_value=(True, Path("/vm/launcher-smoke"))) as mock_preflight, \
             patch.object(acceptance, "run_isolation_check", return_value=True), \
             patch.object(acceptance, "run_nested_remote", return_value=(3, summary)), \
             redirect_stdout(io.StringIO()):
            result = acceptance.nested_gate(None, "launcher-smoke", Path("/snapshot"), ROOT)
        self.assertTrue(mock_preflight.call_args.kwargs["allow_active_source_overlay"])
        self.assertEqual(result, 3)
        outcomes = {check["name"]: check["result"] for check in evidence.data["checks"]}
        self.assertEqual(outcomes["child up"], "FAIL")
        self.assertEqual(outcomes["child acceptance"], "BLOCKED")
        self.assertEqual(outcomes["depth limit refused before state"], "BLOCKED")
        self.assertEqual(outcomes["outer session usable"], "BLOCKED")

    def test_nested_resume_reuses_child_and_skips_new_child_memory_budget(self):
        evidence = DummyEvidence()
        summary = {"checks": {
            "existing_profile": True, "child_up": True, "child_acceptance": True,
            "nested_isolation": True,
            "depth_limit_refused_before_state": True, "outer_session_usable": True,
        }}
        with patch.object(acceptance, "Evidence", return_value=evidence), \
             patch.object(acceptance, "run_preflight", return_value=(True, Path("/vm/outer-8g"))) as mock_preflight, \
             patch.object(acceptance, "run_isolation_check", return_value=True) as mock_outer_isolation, \
             patch.object(acceptance, "run_nested_remote", return_value=(0, summary)) as mock_remote, \
             redirect_stdout(io.StringIO()):
            result = acceptance.nested_gate(
                "nested-accept-existing", "outer-8g", Path("/snapshot"), ROOT,
                reuse_existing=True,
            )

        self.assertEqual(result, 0)
        self.assertFalse(mock_preflight.call_args.kwargs["require_child_memory_budget"])
        self.assertTrue(mock_preflight.call_args.kwargs["read_only"])
        self.assertFalse(mock_preflight.call_args.kwargs["allow_outer_create"])
        self.assertTrue(mock_remote.call_args.kwargs["resume_existing"])
        mock_outer_isolation.assert_called_once_with("outer-8g", evidence, "outer", ROOT)
        outcomes = {check["name"]: check["result"] for check in evidence.data["checks"]}
        self.assertEqual(outcomes["existing child profile"], "PASS")
        self.assertEqual(outcomes["child up"], "PASS")
        self.assertEqual(outcomes["nested isolation"], "PASS")
        self.assertEqual(evidence.data["reused_existing_child"], True)

    def test_nested_gate_does_not_accept_failed_live_isolation_probe(self):
        evidence = DummyEvidence()
        summary = {"checks": {
            "existing_profile": True, "child_up": True, "child_acceptance": True,
            "nested_isolation": False,
            "depth_limit_refused_before_state": True, "outer_session_usable": True,
        }}
        with patch.object(acceptance, "Evidence", return_value=evidence), \
             patch.object(acceptance, "run_preflight", return_value=(True, Path("/vm/outer-8g"))), \
             patch.object(acceptance, "run_isolation_check", return_value=True), \
             patch.object(acceptance, "run_nested_remote", return_value=(7, summary)), \
             redirect_stdout(io.StringIO()):
            result = acceptance.nested_gate(
                "nested-accept-existing", "outer-8g", Path("/snapshot"), ROOT,
                reuse_existing=True,
            )

        self.assertNotEqual(result, 0)
        outcomes = {check["name"]: check["result"] for check in evidence.data["checks"]}
        self.assertEqual(outcomes["nested isolation"], "FAIL")
        self.assertEqual(evidence.data["outcome"], "FAIL")

    def test_nested_gate_stops_before_child_when_outer_boundary_fails(self):
        evidence = DummyEvidence()

        def fail_outer(_profile, target, _label, _repo):
            target.check("outer QEMU and guest network boundary", False, "listener was not loopback-only")
            return False

        with patch.object(acceptance, "Evidence", return_value=evidence), \
             patch.object(acceptance, "run_preflight", return_value=(True, Path("/vm/outer-8g"))), \
             patch.object(acceptance, "run_isolation_check", side_effect=fail_outer), \
             patch.object(acceptance, "run_nested_remote") as mock_remote, \
             redirect_stdout(io.StringIO()):
            result = acceptance.nested_gate(
                "nested-accept-existing", "outer-8g", Path("/snapshot"), ROOT,
                reuse_existing=True,
            )

        self.assertNotEqual(result, 0)
        mock_remote.assert_not_called()
        self.assertEqual(evidence.data["outcome"], "FAIL")
        self.assertEqual(evidence.data["checks"][-1]["name"], "nested-resume")
        self.assertEqual(evidence.data["checks"][-2]["result"], "FAIL")

    def test_nested_command_is_explicit_and_contains_no_teardown_or_tui(self):
        compile(acceptance.REMOTE_PREFLIGHT, "remote-preflight", "exec")
        compile(acceptance.REMOTE_ENSURE_NESTED_TOOLS, "remote-ensure-nested-tools", "exec")
        remote_script = acceptance.build_remote_nested_script(ROOT)
        compile(remote_script, "remote-nested", "exec")
        overlay_script = acceptance.build_remote_nested_script(ROOT, use_source_overlay=True)
        compile(overlay_script, "remote-nested-overlay", "exec")
        child = re.search(r"workspace_probe = r'''(.*?)'''", acceptance.REMOTE_NESTED, re.DOTALL)
        overflow = re.search(r"overflow_probe = r'''(.*?)'''", acceptance.REMOTE_NESTED, re.DOTALL)
        self.assertIsNotNone(child)
        self.assertIsNotNone(overflow)
        compile(child.group(1), "remote-child-probe", "exec")
        compile(overflow.group(1), "remote-overflow-probe", "exec")
        self.assertIn("--no-remove", acceptance.REMOTE_ENSURE_NESTED_TOOLS)
        self.assertIn("--no-upgrade", acceptance.REMOTE_ENSURE_NESTED_TOOLS)
        self.assertIn("--no-install-recommends", acceptance.REMOTE_ENSURE_NESTED_TOOLS)
        self.assertIn('"qemu-system-x86_64": "qemu-system-x86"', acceptance.REMOTE_ENSURE_NESTED_TOOLS)
        self.assertIn('"qemu-img": "qemu-utils"', acceptance.REMOTE_ENSURE_NESTED_TOOLS)
        self.assertIn('"virtiofsd": "virtiofsd"', acceptance.REMOTE_ENSURE_NESTED_TOOLS)
        self.assertIn('"iso_writer": "genisoimage"', acceptance.REMOTE_ENSURE_NESTED_TOOLS)
        self.assertIn('"sudo", "-n", "apt-get", "-qq", "update"', acceptance.REMOTE_ENSURE_NESTED_TOOLS)
        self.assertIn("supports only the Ubuntu/Debian guest images", acceptance.REMOTE_ENSURE_NESTED_TOOLS)
        self.assertIn('env["VM_STATE_DIR"] = str(session)', acceptance.REMOTE_NESTED)
        self.assertIn('resume_existing = len(sys.argv) > 3 and sys.argv[3] == "--resume-existing"', acceptance.REMOTE_NESTED)
        self.assertIn('"reused_existing_profile": resume_existing', acceptance.REMOTE_NESTED)
        self.assertIn('isolation_env["VM_ISOLATION_SKIP_MODEL"] = "1"', acceptance.REMOTE_NESTED)
        self.assertIn('"verify-isolation", profile', acceptance.REMOTE_NESTED)
        self.assertIn('"nested_isolation"] = isolation_ok', acceptance.REMOTE_NESTED)
        self.assertIn('"QEMU TCP listeners: SSH and egress proxy are loopback-only."', acceptance.REMOTE_NESTED)
        self.assertIn('Reusing the existing nested child; no VM will be started or stopped.', acceptance.REMOTE_NESTED)
        self.assertIn(
            'session, vm_dir = sessions / profile, sessions / profile / "vm" / profile',
            acceptance.REMOTE_NESTED,
        )
        self.assertIn('port_file, key_file, known_file = vm_dir / "ssh_port",', acceptance.REMOTE_NESTED)
        self.assertIn('vm_dir / "id_vm_control", vm_dir / "known_hosts"', acceptance.REMOTE_NESTED)
        self.assertIn('parent_proxy = "http://10.0.2.100:3128"', acceptance.REMOTE_NESTED)
        self.assertIn(
            'env.update({key: parent_proxy for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")})',
            acceptance.REMOTE_NESTED,
        )
        self.assertIn(
            'env.update({key: "localhost,127.0.0.1,10.0.2.100" for key in ("NO_PROXY", "no_proxy")})',
            acceptance.REMOTE_NESTED,
        )
        self.assertIn('tempfile.mkdtemp(prefix="little-mester-acceptance-", dir="/tmp")', acceptance.REMOTE_NESTED)
        self.assertIn('subprocess.run([str(launcher_root / "lab-vm"), "-d", "up", profile, "--workspace", "/workspace", "--recurse"]', acceptance.REMOTE_NESTED)
        self.assertIn('str(launcher), "up", probe', acceptance.REMOTE_NESTED)
        self.assertIn('launcher = pathlib.Path("/workspace/lab-vm")', overflow.group(1))
        self.assertNotIn('env["LM_ACCEPTANCE_LAUNCHER"]', overflow.group(1))
        self.assertIn('"stdout_bytes": len(overflow.stdout)', acceptance.REMOTE_NESTED)
        self.assertIn('"stderr_present": bool(overflow.stderr)', acceptance.REMOTE_NESTED)
        self.assertIn('the active /workspace snapshot is unchanged', acceptance.REMOTE_NESTED)
        self.assertNotIn('"down"', acceptance.REMOTE_NESTED)
        self.assertNotIn('"stop"', acceptance.REMOTE_NESTED)
        self.assertNotIn('"--clean"', acceptance.REMOTE_NESTED)
        self.assertNotIn('"agent"', acceptance.REMOTE_NESTED)
        self.assertNotIn('"run"', acceptance.REMOTE_NESTED)

    def test_depth_cap_probe_uses_child_workspace_launcher_without_outer_only_env(self):
        overflow = re.search(r"overflow_probe = r'''(.*?)'''", acceptance.REMOTE_NESTED, re.DOTALL)
        self.assertIsNotNone(overflow)
        expected_refusal = "REFUSED: maximum nesting depth 1 reached; no child VM or agent was started."
        attempt = subprocess.CompletedProcess(
            args=["/workspace/lab-vm", "up"], returncode=1,
            stdout=expected_refusal, stderr="",
        )
        output = io.StringIO()

        with tempfile.TemporaryDirectory(prefix="depth-cap-probe-test-") as home:
            with patch.dict(os.environ):
                os.environ["HOME"] = home
                os.environ["LAB_VM_RECURSION_DEPTH"] = "0"
                os.environ["LAB_VM_MAX_DEPTH"] = "1"
                os.environ.pop("LM_ACCEPTANCE_LAUNCHER", None)
                with patch.object(acceptance.subprocess, "run", return_value=attempt) as mocked_run, \
                     patch.object(acceptance.sys, "argv", ["probe", "depth-cap-test"]), \
                     redirect_stdout(output):
                    with self.assertRaises(SystemExit) as exited:
                        exec(overflow.group(1), {})

        self.assertEqual(exited.exception.code, 0)
        self.assertIn('"expected_refusal": true', output.getvalue())
        self.assertIn('"state_created": false', output.getvalue())
        call = mocked_run.call_args
        self.assertEqual(call.args[0][:3], ["/workspace/lab-vm", "up", "depth-cap-test"])
        self.assertEqual(call.kwargs["cwd"], Path("/workspace"))

    def test_remote_resume_rechecks_child_without_starting_a_vm(self):
        profile = "nested-accept-resume"
        probe = "depth-cap-0123456789"
        child_context = {
            "runtime": {"kind": "vm"},
            "recursion": {"depth": 1, "max_depth": 1, "child_depth_available": False},
            "kvm": {"nested_kvm_preflight_ready": True},
        }
        parent_context = {
            "recursion": {"depth": 0, "max_depth": 1, "child_depth_available": True},
        }
        cloud_status = {"status": "done", "extended_status": "done", "errors": [], "recoverable_errors": {}}
        workspace_boundary = {"host_state_excluded": True}
        cap_result = {"returncode": 1, "expected_refusal": True, "state_created": False}
        isolation_output = (
            "QEMU configuration: restrict=on; SSH bound to 127.0.0.1:23456; only the four reviewed guestfwd channels are configured.\n"
            "QEMU TCP listeners: SSH and egress proxy are loopback-only.\n"
            "PASS: direct route: blocked\n"
            "SKIP: selected model availability (not part of isolation-only acceptance)\n"
            "Result: 1 passed, 0 failed\n"
        )
        results = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, json.dumps(cloud_status), ""),
            subprocess.CompletedProcess([], 0, json.dumps(child_context), ""),
            subprocess.CompletedProcess([], 0, "virtiofs\n", ""),
            subprocess.CompletedProcess([], 0, json.dumps(workspace_boundary), ""),
            subprocess.CompletedProcess([], 0, isolation_output, ""),
            subprocess.CompletedProcess([], 0, json.dumps(cap_result), ""),
            subprocess.CompletedProcess([], 0, json.dumps(parent_context), ""),
        ]
        output = io.StringIO()

        with tempfile.TemporaryDirectory(prefix="nested-resume-test-") as home:
            profile_dir = Path(home) / ".cache/lab-vm-recursion" / profile
            vm_dir = profile_dir / "vm" / profile
            vm_dir.mkdir(mode=0o700, parents=True)
            profile_dir.chmod(0o700)
            (vm_dir / "ssh_port").write_text("23456\n", encoding="ascii")
            (vm_dir / "id_vm_control").write_text("key", encoding="ascii")
            (vm_dir / "known_hosts").write_text("host", encoding="ascii")
            script = acceptance.build_remote_nested_script(ROOT)

            with patch.dict(os.environ):
                os.environ["HOME"] = home
                with patch.object(Path, "home", return_value=Path(home)), \
                     patch.object(acceptance.subprocess, "run", side_effect=results) as mocked_run, \
                     patch.object(acceptance.sys, "argv", ["remote", profile, probe, "--resume-existing"]), \
                     redirect_stdout(output):
                    with self.assertRaises(SystemExit) as exited:
                        exec(script, {})

        self.assertEqual(exited.exception.code, 0)
        summary_line = next(line for line in output.getvalue().splitlines() if line.startswith(acceptance.REMOTE_SUMMARY_MARKER))
        summary = json.loads(summary_line[len(acceptance.REMOTE_SUMMARY_MARKER):])
        self.assertTrue(summary["child"]["reused_existing_profile"])
        self.assertTrue(summary["checks"]["child_up"])
        self.assertTrue(summary["checks"]["child_acceptance"])
        self.assertTrue(summary["checks"]["nested_isolation"])
        self.assertTrue(summary["nested_isolation"]["qemu_argv_validated"])
        self.assertTrue(summary["nested_isolation"]["loopback_listeners_validated"])
        self.assertEqual(summary["nested_isolation"]["passed_checks"], 1)
        self.assertEqual(summary["nested_isolation"]["skipped_checks"], 1)
        self.assertTrue(summary["checks"]["depth_limit_refused_before_state"])
        self.assertTrue(summary["checks"]["outer_session_usable"])
        self.assertEqual(summary["nested_isolation"]["failed_checks"], 0)
        serialized_summary = json.dumps(summary, sort_keys=True)
        self.assertNotIn("QEMU configuration:", serialized_summary)
        self.assertNotIn("127.0.0.1:23456", serialized_summary)
        self.assertEqual(mocked_run.call_count, 8)
        self.assertTrue(all("-d" not in call.args[0] and "up" not in call.args[0] for call in mocked_run.call_args_list))
        isolation_call = next(
            call for call in mocked_run.call_args_list
            if call.args[0][:2] == ["/workspace/lab-vm", "verify-isolation"]
        )
        self.assertEqual(isolation_call.args[0], ["/workspace/lab-vm", "verify-isolation", profile])
        self.assertEqual(isolation_call.kwargs["env"]["VM_ISOLATION_SKIP_MODEL"], "1")
        depth_call = next(
            index for index, call in enumerate(mocked_run.call_args_list)
            if call.args[0][-1] == probe
        )
        self.assertLess(mocked_run.call_args_list.index(isolation_call), depth_call)

    def test_nested_probes_are_visible_and_bounded(self):
        for marker in (
            "Checking child readiness: cloud-init 30s, context 30s, findmnt 10s, workspace boundary 10s.",
            "Checking depth-cap refusal before state creation (timeout: 120s).",
            "Checking outer-session continuity (timeout: 30s).",
        ):
            self.assertIn(marker, acceptance.REMOTE_NESTED)
        for timeout in (120, 90, 30):
            self.assertIn(f"timeout={timeout}", acceptance.REMOTE_NESTED)
        self.assertIn('"-o", "ServerAliveInterval=15"', acceptance.REMOTE_NESTED)

    def test_child_readiness_reports_bounded_subprobe_stage(self):
        for source in (
            'run_child_stage("child_cloud_init_status", "sudo -n cloud-init status --format json", 30)',
            'run_child_stage("child_context", "cd /workspace && ./lab-vm context --json", 30)',
            'run_child_stage("child_findmnt", "findmnt -n -o FSTYPE --target /workspace", 10)',
            'run_child_stage("child_workspace_boundary", "cd /workspace && python3 -", 10, workspace_probe)',
        ):
            self.assertIn(source, acceptance.REMOTE_NESTED)
        self.assertIn('progress(stage, "started", timeout_seconds=timeout)', acceptance.REMOTE_NESTED)
        self.assertIn('progress(stage, "timed_out", timeout_seconds=timeout)', acceptance.REMOTE_NESTED)
        self.assertIn('"probe_stages": {', acceptance.REMOTE_NESTED)
        self.assertIn('progress("child_vm_up", "started")', acceptance.REMOTE_NESTED)
        self.assertIn('progress("child_vm_up", "completed", returncode=up.returncode)', acceptance.REMOTE_NESTED)

    def test_interrupt_terminates_only_local_ssh_client(self):
        class FakeProcess:
            def __init__(self, timeout_first_wait=False):
                self.timeout_first_wait = timeout_first_wait
                self.wait_calls = 0
                self.terminated = False
                self.killed = False

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                self.wait_calls += 1
                if self.timeout_first_wait and self.wait_calls == 1:
                    raise subprocess.TimeoutExpired("ssh", timeout)
                return 0

            def kill(self):
                self.killed = True

        normal = FakeProcess()
        acceptance.terminate_local_ssh(normal)
        self.assertTrue(normal.terminated)
        self.assertFalse(normal.killed)

        delayed = FakeProcess(timeout_first_wait=True)
        acceptance.terminate_local_ssh(delayed)
        self.assertTrue(delayed.terminated)
        self.assertTrue(delayed.killed)
        self.assertEqual(delayed.wait_calls, 2)

    def test_nested_remote_returns_on_terminal_summary_even_if_ssh_stream_stays_open(self):
        class BlockingStream:
            def __init__(self, lines):
                from threading import Event
                self.lines = lines
                self.released = Event()

            def __iter__(self):
                yield from self.lines
                self.released.wait(timeout=2)

        class FakeProcess:
            def __init__(self, lines):
                self.stdin = io.StringIO()
                self.stdout = BlockingStream(lines)
                self.terminated = False
                self.killed = False

            def terminate(self):
                self.terminated = True
                self.stdout.released.set()

            def wait(self, timeout=None):
                return -15 if self.terminated else 0

            def kill(self):
                self.killed = True
                self.stdout.released.set()

        profile = "nested-accept-20261102095253-7d2d810a"
        progress = acceptance.REMOTE_PROGRESS_MARKER + json.dumps({
            "stage": "child_control_state", "status": "failed",
            "reason": "ssh_controls_missing_or_unsafe",
        }) + "\n"
        expected = {
            "checks": {"child_up": True},
            "failure": "child SSH control files are missing or unsafe",
        }
        result = acceptance.REMOTE_SUMMARY_MARKER + json.dumps(expected) + "\n"
        process = FakeProcess([progress, result])
        evidence = DummyEvidence()

        with patch.object(acceptance, "ssh_command", return_value=["ssh"]), \
             patch.object(acceptance.subprocess, "Popen", return_value=process), \
             redirect_stdout(io.StringIO()):
            returncode, summary = acceptance.run_nested_remote(
                Path("/outer"), profile, "depth-cap-0123456789", evidence, ROOT
            )

        self.assertEqual(returncode, 0)
        self.assertEqual(summary, expected)
        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)
        self.assertEqual(evidence.data["nested_progress"][-1]["status"], "failed")

    def test_nested_remote_has_overall_deadline_when_no_terminal_summary_arrives(self):
        class BlockingStream:
            def __init__(self):
                from threading import Event
                self.released = Event()

            def __iter__(self):
                yield from ()
                self.released.wait(timeout=2)

        class FakeProcess:
            def __init__(self):
                self.stdin = io.StringIO()
                self.stdout = BlockingStream()
                self.terminated = False

            def terminate(self):
                self.terminated = True
                self.stdout.released.set()

            def wait(self, timeout=None):
                return -15 if self.terminated else 0

            def kill(self):
                self.stdout.released.set()

        process = FakeProcess()
        evidence = DummyEvidence()
        with patch.object(acceptance, "REMOTE_NESTED_TIMEOUT_SECONDS", 0.01), \
             patch.object(acceptance, "ssh_command", return_value=["ssh"]), \
             patch.object(acceptance.subprocess, "Popen", return_value=process), \
             redirect_stdout(io.StringIO()):
            returncode, summary = acceptance.run_nested_remote(
                Path("/outer"), "nested-accept-test", "depth-cap-0123456789", evidence, ROOT
            )

        self.assertEqual(returncode, 124)
        self.assertIn("overall deadline", summary["failure"])
        self.assertTrue(process.terminated)
        self.assertEqual(evidence.data["nested_progress"][-1]["status"], "timed_out")

    def test_remote_progress_events_are_allowlisted_and_persisted(self):
        line = acceptance.REMOTE_PROGRESS_MARKER + json.dumps({
            "stage": "child_vm_up", "status": "waiting", "elapsed_seconds": 60,
            "silence_seconds": 30, "raw": "must-not-be-recorded",
        })
        event = acceptance.parse_remote_progress(line)
        self.assertEqual(event, {
            "stage": "child_vm_up", "status": "waiting",
            "elapsed_seconds": 60, "silence_seconds": 30,
        })
        self.assertIsNone(acceptance.parse_remote_progress("ordinary output"))
        invalid = acceptance.parse_remote_progress(
            acceptance.REMOTE_PROGRESS_MARKER + '{"stage":"arbitrary","status":"started"}'
        )
        self.assertEqual(invalid, {})

        evidence = DummyEvidence()
        output = io.StringIO()
        with redirect_stdout(output):
            acceptance.record_remote_progress(evidence, event)
        self.assertEqual(evidence.data["nested_progress"][0]["stage"], "child_vm_up")
        self.assertIn("PROGRESS child_vm_up: waiting", output.getvalue())
        self.assertNotIn("must-not-be-recorded", output.getvalue())

    def test_inspect_interrupted_reads_summary_without_mutating_or_leaking_details(self):
        profile = "nested-accept-20261102005812-6494102d"
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            report_path = state_dir / "vm-acceptance-interrupted.json"
            report_path.write_text(json.dumps({
                "command": "nested",
                "started_at": "2026-11-02T01:00:00+00:00",
                "child_profile": profile,
                "checks": [{"name": "child up", "result": "PASS", "detail": "private-detail-marker"}],
                "nested_summary": {
                    "checks": {"child_up": True, "child_acceptance": False},
                    "child": {
                        "result": "timed out",
                        "failed_stage": "lab-vm context --json",
                        "timeout_seconds": 30,
                        "ssh_returncode": 20,
                        "checks": {"cloud_command_ok": True, "context_command_ok": False},
                    },
                },
                "nested_memory_budget": {
                    "memavailable_mib": 3800,
                    "cgroup_remaining": "4096 MiB",
                    "cgroup_remaining_mib": 4096,
                    "releasable_qemu_rss_mib": 0,
                    "projected_available_mib": 3800,
                    "requested_mib": 4096,
                    "reserve_mib": 512,
                    "error_category": "insufficient_memory_budget",
                },
                "nested_progress": [{
                    "observed_at": "2026-11-02T01:01:00+00:00",
                    "stage": "child_vm_up", "status": "waiting", "silence_seconds": 30,
                }],
            }))
            report_path.chmod(0o600)
            before = report_path.read_bytes()
            output = io.StringIO()
            with patch.object(acceptance, "interrupted_processes", return_value=[]), \
                 redirect_stdout(output):
                result = acceptance.inspect_interrupted(profile, state_dir)
            self.assertEqual(result, 0)
            self.assertIn("Outcome: interrupted or unfinished", output.getvalue())
            self.assertIn("PASS: child up", output.getvalue())
            self.assertIn("Child readiness failed stage: lab-vm context --json", output.getvalue())
            self.assertIn("FAIL: context_command_ok", output.getvalue())
            self.assertIn("MemAvailable MiB: 3800", output.getvalue())
            self.assertIn("projected available MiB: 3800", output.getvalue())
            self.assertIn("child_vm_up: waiting", output.getvalue())
            self.assertNotIn("private-detail-marker", output.getvalue())
            self.assertIn("No matching local acceptance runner", output.getvalue())
            self.assertEqual(report_path.read_bytes(), before)
            self.assertEqual(report_path.stat().st_mode & 0o777, 0o600)
            report_output = io.StringIO()
            with redirect_stdout(report_output):
                self.assertEqual(acceptance.inspect_report(report_path), 0)
            self.assertIn("Outer guest memory budget:", report_output.getvalue())
            self.assertNotIn("private-detail-marker", report_output.getvalue())
        parsed = acceptance.parser().parse_args(["inspect-interrupted", profile])
        self.assertEqual(parsed.command, "inspect-interrupted")
        self.assertEqual(parsed.profile, profile)
        report_args = acceptance.parser().parse_args(["inspect-report", "/tmp/report.json"])
        self.assertEqual(report_args.command, "inspect-report")

    def test_interrupted_process_classifier_excludes_live_browser_and_tui_ssh(self):
        browser_bridge = [
            "ssh", "-F", "/dev/null", "-M", "-S", "/run/user/1000/ssh-control",
            "-N", "-R", "127.0.0.1:53719:127.0.0.1:52367", "mester@127.0.0.1",
        ]
        tui = [
            "ssh", "-t", "-o", "BatchMode=yes", "mester@127.0.0.1",
            "set -eu; cd /workspace && opencode",
        ]
        nested_probe = [
            "ssh", "-o", "BatchMode=yes", "mester@127.0.0.1",
            "cd /workspace && python3 - nested-accept-20261102005812-6494102d depth-cap-0123456789",
        ]
        resumed_probe = [
            "ssh", "-o", "BatchMode=yes", "mester@127.0.0.1",
            "cd /workspace && python3 - nested-accept-20261102005812-6494102d depth-cap-0123456789 --resume-existing",
        ]
        self.assertIsNone(acceptance.classify_interrupted_process(browser_bridge))
        self.assertIsNone(acceptance.classify_interrupted_process(tui))
        self.assertEqual(
            acceptance.classify_interrupted_process(nested_probe),
            "nested-acceptance-ssh-to-outer",
        )
        self.assertEqual(
            acceptance.classify_interrupted_process(resumed_probe),
            "nested-acceptance-ssh-to-outer",
        )

    def test_nested_profile_is_automatically_generated_by_default(self):
        args = acceptance.parser().parse_args(["nested"])
        self.assertIsNone(args.profile)
        self.assertIsNone(args.reuse_child)
        resumed = acceptance.parser().parse_args(["nested", "--reuse-child", "nested-accept-existing"])
        self.assertEqual(resumed.reuse_child, "nested-accept-existing")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            acceptance.parser().parse_args([
                "nested", "--profile", "nested-accept-new",
                "--reuse-child", "nested-accept-existing",
            ])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            acceptance.parser().parse_args(["sync-snapshot", "--confirm-reviewed"])

    def test_empty_xdg_state_home_uses_private_default_state_path(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"HOME": directory, "XDG_STATE_HOME": ""}, clear=True):
                evidence = acceptance.Evidence("test", "scope")
            self.assertEqual(evidence.directory, Path(directory) / ".local/state/little-mester/host-acceptance")
            self.assertEqual(evidence.path.stat().st_mode & 0o777, 0o600)

    def test_campaign_contract_classifies_each_ticket_ac_without_unclaimed_overlap(self):
        ac_counts = {"#35": 22, "#41": 26, "#42": 14, "#43": 8, "#45": 18, "#50": 4, "#52": 8}
        covered = {ticket: set() for ticket in ac_counts}
        for gate in acceptance.CAMPAIGN_GATE_COVERAGE.values():
            self.assertIn("scope", gate)
            for ticket, ids in gate["tickets"].items():
                self.assertIn(ticket, ac_counts)
                covered[ticket].update(ids)
        self.assertEqual(set(acceptance.CAMPAIGN_NOT_CLAIMED), set(ac_counts))
        for ticket, count in ac_counts.items():
            existing = {f"A{number}" for number in range(1, count + 1)}
            not_claimed = set(acceptance.CAMPAIGN_NOT_CLAIMED[ticket])
            self.assertTrue(covered[ticket] <= existing, f"nonexistent AC mapped for {ticket}")
            self.assertTrue(not_claimed <= existing, f"nonexistent AC listed as unclaimed for {ticket}")
            self.assertFalse(covered[ticket] & not_claimed, f"AC both covered and unclaimed for {ticket}")
            self.assertEqual(covered[ticket] | not_claimed, existing, f"AC classification incomplete for {ticket}")

    def test_campaign_outcome_fails_closed_for_missing_failed_and_blocked_gates(self):
        def evidence():
            item = DummyEvidence()
            item.data["command"] = "campaign"
            item.data["campaign_gate_coverage"] = {
                gate: {**details, "result": "NOT_RUN"}
                for gate, details in acceptance.CAMPAIGN_GATE_COVERAGE.items()
            }
            return item

        skipped = evidence()
        self.assertEqual(acceptance.finish_campaign(skipped, "PASS"), "BLOCKED")
        self.assertEqual(skipped.data["outcome"], "BLOCKED")
        self.assertTrue(all(entry["result"] == "BLOCKED" for entry in skipped.data["campaign_gate_coverage"].values()))

        failed = evidence()
        acceptance.record_campaign_gate(failed, "host_prerequisites", "FAIL", "probe failed")
        self.assertEqual(acceptance.finish_campaign(failed, "PASS"), "FAIL")
        self.assertEqual(failed.data["campaign_gate_coverage"]["host_prerequisites"]["result"], "FAIL")

        blocked = evidence()
        acceptance.record_campaign_gate(blocked, "native_auth", "BLOCKED", "OAuth approval declined")
        self.assertEqual(acceptance.finish_campaign(blocked, "PASS"), "BLOCKED")
        self.assertEqual(blocked.data["campaign_gate_coverage"]["native_auth"]["result"], "BLOCKED")

    def test_campaign_record_rejects_duplicate_gate_results(self):
        evidence = DummyEvidence()
        evidence.data["campaign_gate_coverage"] = {
            gate: {**details, "result": "NOT_RUN"}
            for gate, details in acceptance.CAMPAIGN_GATE_COVERAGE.items()
        }
        acceptance.record_campaign_gate(evidence, "host_prerequisites", "PASS", "first result")
        with self.assertRaisesRegex(acceptance.AcceptanceError, "recorded more than once"):
            acceptance.record_campaign_gate(evidence, "host_prerequisites", "PASS", "duplicate")

    def test_recursive_source_requires_clean_committed_checkout(self):
        clean = subprocess.CompletedProcess(["git"], 0, "", "")
        dirty = subprocess.CompletedProcess(["git"], 0, " M scripts/changed.py\n", "")
        unavailable = subprocess.CompletedProcess(["git"], 128, "", "fatal: not a git repository")
        with patch.object(acceptance, "run", side_effect=[clean, dirty, unavailable]):
            self.assertTrue(acceptance.recursive_source_checkout_clean(ROOT))
            self.assertFalse(acceptance.recursive_source_checkout_clean(ROOT))
            self.assertFalse(acceptance.recursive_source_checkout_clean(ROOT))

    def test_prerequisite_probe_clears_inherited_auto_accept(self):
        output = (
            "DISTRO=ubuntu\nMANAGER=apt\nMAPPING=REQUIRED: qemu-utils\n"
            "INSTALL_RC=1\nInstall these packages? [y/N] Aborted. Install manually and re-run.\n"
        )
        with patch.dict(os.environ, {"VM_YES": "1", "LAB_YES": "1"}), \
             patch.object(
                 acceptance, "run",
                 return_value=subprocess.CompletedProcess([], 0, output, ""),
             ) as run:
            ok, _ = acceptance.prerequisite_prompt_probe(ROOT)
        self.assertTrue(ok)
        self.assertEqual(run.call_args.kwargs["env"]["VM_YES"], "0")
        self.assertEqual(run.call_args.kwargs["env"]["LAB_YES"], "0")

    def test_generated_campaign_guest_scripts_have_valid_shell_syntax(self):
        scripts = {
            "root readiness": acceptance.disposable_guest_readiness_script(0, 2),
            "recursive chain": acceptance.recursion_chain_script("depth-one", "depth-two", "depth-three"),
            "nested readiness": acceptance.nested_guest_script_command(
                "depth-one", acceptance.disposable_guest_readiness_script(1, 2),
                env_prefix=acceptance.recursive_profile_state_env("depth-one"),
            ),
            "Compose workload": acceptance.compose_feedback_script("campaign-profile"),
            "Compose teardown": acceptance.compose_feedback_teardown_script("campaign-profile"),
        }
        for name, script in scripts.items():
            with self.subTest(script=name):
                result = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
        chain = scripts["recursive chain"]
        self.assertIn("= '1 2'", scripts["nested readiness"])
        self.assertIn("DEPTH3_REFUSED_BEFORE_STATE", chain)
        self.assertIn("CHAIN_PARENT_CONTINUITY_PASS", chain)
        self.assertIn("VM_DIR_BASE=\"$HOME/.cache/lab-vm-recursion/depth-one/vm\"", chain)
        self.assertIn(
            'child2_ready="$(VM_STATE_DIR="$HOME/.cache/lab-vm-recursion/depth-one" '
            'VM_DIR_BASE="$HOME/.cache/lab-vm-recursion/depth-one/vm" ./lab-vm ssh depth-one ',
            chain,
        )
        self.assertIn("VM_ISOLATION_SKIP_MODEL=1", chain)

    def test_human_permission_observations_validate_mode_argv_and_same_version(self):
        evidence = DummyEvidence()
        evidence.data["observations"] = [
            {
                "gate": "permissions", "opencode_version": "2.2.5", "permission_mode": "auto",
                "opencode_argv": ["opencode", "--auto"],
                **{question: "pass" for question in acceptance.OBSERVATIONS["permissions"][1]},
            },
            {
                "gate": "permissions_standalone", "opencode_version": "2.2.5", "permission_mode": "auto",
                "opencode_argv": ["opencode", "--standalone", "--auto"],
                **{question: "pass" for question in acceptance.OBSERVATIONS["permissions_standalone"][1]},
            },
        ]
        self.assertEqual(acceptance.human_observation_result(evidence, "permissions"), "PASS")
        self.assertEqual(acceptance.human_observation_result(evidence, "permissions_standalone"), "PASS")
        self.assertTrue(acceptance.permission_versions_match(evidence))
        evidence.data["observations"][1]["opencode_version"] = "2.2.6"
        self.assertFalse(acceptance.permission_versions_match(evidence))
        evidence.data["observations"][1]["permission_mode"] = "ask"
        self.assertEqual(acceptance.human_observation_result(evidence, "permissions_standalone"), "FAIL")

    def test_auth_gate_requires_the_host_browser_bridge_method(self):
        evidence = DummyEvidence()
        evidence.data["observations"] = [{
            "gate": "auth", "auth_method": "other", "opencode_version": "v2.2.5",
            **{question: "pass" for question in acceptance.OBSERVATIONS["auth"][1]},
        }]
        self.assertEqual(acceptance.human_observation_result(evidence, "auth"), "FAIL")
        evidence.data["observations"][0]["auth_method"] = "host-browser-bridge"
        self.assertEqual(acceptance.human_observation_result(evidence, "auth"), "PASS")

    def test_standalone_campaign_launch_is_direct_and_uses_exact_opencode_args(self):
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory)
            profile_dir = state_root / "vm" / "disposable-profile"
            profile_dir.mkdir(parents=True)
            (profile_dir / "recursion_policy").write_text("0 0 0\n")
            evidence = DummyEvidence()
            with patch.object(acceptance, "ssh_command", side_effect=lambda _vm_dir, remote: ["ssh", "profile", remote]), \
                 patch.object(acceptance, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run, \
                 redirect_stdout(io.StringIO()):
                passed = acceptance.launch_campaign_agent(
                    "disposable-profile", evidence, browser_auth=False, auto=True,
                    state_root=state_root, standalone=True,
                )
        self.assertTrue(passed)
        self.assertEqual(run.call_args.args[0][:2], ["ssh", "-tt"])
        remote = run.call_args.args[0][-1]
        self.assertIn("exec opencode --standalone --auto", remote)
        self.assertIn("OPENCODE_CONFIG=/etc/opencode/opencode.json", remote)
        self.assertIn("VM_STATE_DIR", run.call_args.kwargs["env"])
        self.assertFalse(run.call_args.kwargs["capture"])
        self.assertFalse(run.call_args.kwargs["no_input"])

    def test_campaign_cli_requires_exact_preserved_outer_and_child_names(self):
        args = acceptance.parser().parse_args([
            "campaign", "--outer-profile", "nested-8g-20261102094820",
            "--reuse-child", "nested-accept-20261102124501-ee957d5c",
        ])
        self.assertEqual(args.command, "campaign")
        self.assertEqual(args.outer_profile, "nested-8g-20261102094820")
        self.assertEqual(args.reuse_child, "nested-accept-20261102124501-ee957d5c")

    def test_campaign_refuses_noninteractive_execution_before_creating_evidence_or_state(self):
        class NonTerminal:
            @staticmethod
            def isatty():
                return False

        with patch.object(acceptance.sys, "stdin", NonTerminal()), \
             patch.object(acceptance, "Evidence", side_effect=AssertionError("evidence must not be created")), \
             patch.object(acceptance, "nested_gate", side_effect=AssertionError("no VM action allowed")):
            with self.assertRaisesRegex(acceptance.AcceptanceError, "requires a terminal"):
                acceptance.vm_campaign("outer-safe", "nested-accept-safe", Path("/snapshot"))

    def test_campaign_stops_on_existing_nested_continuation_failure(self):
        class Terminal:
            @staticmethod
            def isatty():
                return True

        created = []

        def make_evidence(command, scope):
            evidence = DummyEvidence()
            evidence.data["command"] = command
            if command == "campaign":
                evidence.data["campaign_gate_coverage"] = {
                    gate: {**details, "result": "NOT_RUN"}
                    for gate, details in acceptance.CAMPAIGN_GATE_COVERAGE.items()
                }
                evidence.data["ticket_ac_not_claimed"] = acceptance.CAMPAIGN_NOT_CLAIMED
            created.append(evidence)
            return evidence

        with patch.object(acceptance.sys, "stdin", Terminal()), \
             patch.object(acceptance, "Evidence", side_effect=make_evidence), \
             patch.object(acceptance, "nested_gate", return_value=1) as nested, \
             patch.object(acceptance, "run_campaign_vm", side_effect=AssertionError("downstream gate must not run")), \
             patch.object(acceptance, "unique_profile_name", side_effect=AssertionError("no disposable profile")), \
             redirect_stdout(io.StringIO()):
            result = acceptance.vm_campaign(
                "nested-8g-20261102094820",
                "nested-accept-20261102124501-ee957d5c",
                Path("/safe-snapshot"),
            )

        self.assertEqual(result, 1)
        nested.assert_called_once_with(
            "nested-accept-20261102124501-ee957d5c",
            "nested-8g-20261102094820",
            Path("/safe-snapshot"), ROOT,
            reuse_existing=True, require_existing_outer=True,
        )
        self.assertEqual(created[0].data["preserved_profiles"], {
            "outer": "nested-8g-20261102094820",
            "child": "nested-accept-20261102124501-ee957d5c",
        })
        self.assertEqual(created[0].data["outcome"], "FAIL")

    def test_campaign_preflight_does_not_bootstrap_a_replacement_outer_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "profiles"
            base.mkdir(mode=0o700)
            evidence = DummyEvidence()
            with patch.object(acceptance, "profile_base", return_value=base), \
                 patch.object(acceptance, "outer_profile_is_ready", return_value=False), \
                 patch.object(acceptance, "run", side_effect=AssertionError("must not start a VM")):
                selected = acceptance.ensure_outer_profile(
                    "nested-8g-20261102094820", Path("/snapshot"), ROOT,
                    evidence, allow_create=False,
                )
            self.assertIsNone(selected)
            self.assertEqual(evidence.data["checks"][-1]["name"], "outer profile reuse")
            self.assertEqual(evidence.data["checks"][-1]["result"], "FAIL")

    def test_campaign_stops_before_guest_tui_if_read_only_model_status_fails(self):
        class Terminal:
            @staticmethod
            def isatty():
                return True

        created = []

        def make_evidence(command, scope):
            evidence = DummyEvidence()
            evidence.data["command"] = command
            if command == "campaign":
                evidence.data["campaign_gate_coverage"] = {
                    gate: {**details, "result": "NOT_RUN"}
                    for gate, details in acceptance.CAMPAIGN_GATE_COVERAGE.items()
                }
                evidence.data["ticket_ac_not_claimed"] = acceptance.CAMPAIGN_NOT_CLAIMED
            created.append(evidence)
            return evidence

        def run_vm(args):
            status = 0 if args[0] == "egress-reload" else 1
            return subprocess.CompletedProcess(args, status, "", "")

        with patch.object(acceptance.sys, "stdin", Terminal()), \
             patch.object(acceptance, "Evidence", side_effect=make_evidence), \
             patch.object(acceptance, "nested_gate", return_value=0) as nested, \
             patch.object(acceptance, "run_campaign_vm", side_effect=run_vm) as vm, \
             patch.object(acceptance, "launch_campaign_agent", side_effect=AssertionError("model gate must pass before TUI")), \
             patch.object(acceptance, "unique_profile_name", side_effect=AssertionError("no disposable profile")), \
             redirect_stdout(io.StringIO()):
            result = acceptance.vm_campaign(
                "nested-8g-20261102094820",
                "nested-accept-20261102124501-ee957d5c",
                Path("/safe-snapshot"),
            )

        self.assertEqual(result, 1)
        nested.assert_called_once()
        self.assertEqual(vm.call_args_list, [
            call(["egress-reload", "nested-8g-20261102094820"]),
            call(["models", "status", "all"]),
        ])
        self.assertEqual(created[0].data["outcome"], "FAIL")
        model_gate = next(
            item for item in created[0].data["checks"]
            if item["name"] == "campaign gate: direct_guest_model_tui"
        )
        self.assertEqual(model_gate["result"], "FAIL")

    def test_campaign_blocks_dirty_recursive_source_before_creating_state_or_base(self):
        class Terminal:
            @staticmethod
            def isatty():
                return True

        evidence_items = []

        def make_evidence(command, scope):
            evidence = DummyEvidence()
            evidence.data["command"] = command
            evidence.data["campaign_gate_coverage"] = {
                gate: {**details, "result": "NOT_RUN"}
                for gate, details in acceptance.CAMPAIGN_GATE_COVERAGE.items()
            }
            evidence.data["ticket_ac_not_claimed"] = acceptance.CAMPAIGN_NOT_CLAIMED
            evidence_items.append(evidence)
            return evidence

        vm_calls = []

        def run_vm(args):
            vm_calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch.object(acceptance.sys, "stdin", Terminal()), \
             patch.object(acceptance, "Evidence", side_effect=make_evidence), \
             patch.object(acceptance, "nested_gate", return_value=0), \
             patch.object(acceptance, "run_campaign_vm", side_effect=run_vm), \
             patch.object(acceptance, "launch_campaign_agent", return_value=True), \
             patch.object(acceptance, "observe_gate", return_value=0), \
             patch.object(acceptance, "human_observation_result", return_value="PASS"), \
             patch.object(acceptance, "prerequisite_prompt_probe", return_value=(True, "default-N passed")), \
             patch.object(acceptance, "recursive_source_checkout_clean", return_value=False), \
             patch.object(acceptance, "memory_budget_ok", side_effect=AssertionError("no VM budget/start after dirty checkout")), \
             patch.object(acceptance, "create_campaign_state_root", side_effect=AssertionError("no campaign state after dirty checkout")), \
             redirect_stdout(io.StringIO()):
            result = acceptance.vm_campaign("preserved-outer", "preserved-child", Path("/snapshot"))

        self.assertEqual(result, 2)
        self.assertEqual(vm_calls, [["egress-reload", "preserved-outer"], ["models", "status", "all"]])
        self.assertEqual(evidence_items[0].data["outcome"], "BLOCKED")
        self.assertEqual(
            evidence_items[0].data["campaign_gate_coverage"]["clean_provisioning"]["result"],
            "BLOCKED",
        )

    def test_campaign_order_coverage_approvals_and_generated_profile_scope(self):
        class Terminal:
            @staticmethod
            def isatty():
                return True

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            workspace_root = home / ".cache/little-mester/host-acceptance"
            workspace_root.mkdir(mode=0o700, parents=True)
            workspace = workspace_root / "vm-acceptance-main-workspace"
            workspace.mkdir(mode=0o700)
            recursion_session = workspace_root / "vm-acceptance-recursion-recursive-source"
            recursion_session.mkdir(mode=0o700)
            recursion_workspace = recursion_session / "workspace"
            recursion_workspace.mkdir(mode=0o700)
            shared_state = home / "shared-state"
            state_parent = shared_state / "host-acceptance"
            state_parent.mkdir(mode=0o700, parents=True)
            state_root = state_parent / "campaign-20261102000000-0123456789"
            (state_root / "vm").mkdir(mode=0o700, parents=True)
            profile_names = iter((
                "vm-acceptance-recursion", "vm-acceptance-depth1",
                "vm-acceptance-depth2", "vm-acceptance-main",
            ))
            recursion_profile = "vm-acceptance-recursion"
            profile = "vm-acceptance-main"
            escrow = shared_state / "secrets" / f"{profile}-auth.json"
            evidence_items = []
            events = []
            vm_calls = []
            running = {}
            chain_scripts = []

            def make_evidence(command, scope):
                evidence = DummyEvidence()
                evidence.data["command"] = command
                if command == "campaign":
                    evidence.data["campaign_gate_coverage"] = {
                        gate: {**details, "result": "NOT_RUN"}
                        for gate, details in acceptance.CAMPAIGN_GATE_COVERAGE.items()
                    }
                    evidence.data["ticket_ac_not_claimed"] = acceptance.CAMPAIGN_NOT_CLAIMED
                evidence_items.append(evidence)
                return evidence

            def run_vm(args, **kwargs):
                args = list(args)
                vm_calls.append(args)
                events.append("vm:" + " ".join(args))
                if args[0] == "base":
                    (state_root / "vm" / "base.qcow2").write_text("base")
                    return subprocess.CompletedProcess(args, 0, "All VM dependencies found. OK\n", "")
                if args[0] == "up":
                    name = args[1]
                    vm_dir = state_root / "vm" / name
                    vm_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                    (vm_dir / "disk.qcow2").write_text("overlay")
                    (vm_dir / "id_vm_control").write_text("key")
                    ws_arg = args[args.index("--workspace") + 1]
                    (vm_dir / "workspace").write_text(ws_arg)
                    running[name] = True
                elif args[0] in ("stop", "down"):
                    name = args[1]
                    running[name] = False
                    vm_dir = state_root / "vm" / name
                    for asset in ("disk.qcow2", "id_vm_control", "seed.iso", "qemu.pid", "virtiofsd.pid", "proxy.pid"):
                        try:
                            (vm_dir / asset).unlink()
                        except FileNotFoundError:
                            pass
                elif args[0] == "suspend":
                    running[args[1]] = False
                elif args[0] == "resume":
                    running[args[1]] = True
                return subprocess.CompletedProcess(args, 0, "", "")

            def launch(profile_name, evidence, *, browser_auth, auto=False, state_root=None, standalone=False):
                events.append(f"agent:{profile_name}:browser={browser_auth}:auto={auto}:standalone={standalone}")
                return True

            def observe(gate, evidence, **kwargs):
                events.append("observe:" + gate)
                observation = {"gate": gate, "sensitive_material_recorded": "no"}
                for question in acceptance.OBSERVATIONS[gate][1]:
                    observation[question] = "pass"
                if gate.startswith("permissions"):
                    observation["permission_mode"] = "auto"
                    observation["opencode_version"] = "2.2.5"
                    observation["opencode_argv"] = kwargs["opencode_argv"]
                if gate == "auth":
                    observation["opencode_version"] = "v2.2.5"
                    observation["auth_method"] = "host-browser-bridge"
                evidence.data["observations"].append(observation)
                return 0

            def guest_script(vm_dir, script, *, timeout, stream=False):
                events.append("guest-script")
                if "CHAIN_STAGE depth1-up" in script:
                    chain_scripts.append(script)
                    return True, "", "exit_0"
                if "test \"$(cat /etc/lab-vm-recursion)\" = '0 2'" in script:
                    return True, "DISPOSABLE_GUEST_READY depth=0 max=2\n", "exit_0"
                if "test \"$(cat /etc/lab-vm-recursion)\" = '0 0'" in script:
                    return True, "DISPOSABLE_GUEST_READY depth=0 max=0\n", "exit_0"
                if "WORKLOAD_READY_FOR_HOST_BOUNDARY" in script:
                    return True, "WORKLOAD_QUERY_FIRST\nWORKLOAD_RESTART_QUERY\nGUEST_LOOPBACK_PORT_ONLY\nBIND_MOUNT_AND_VOLUME_OK\nWORKLOAD_READY_FOR_HOST_BOUNDARY\n", "exit_0"
                if "WORKLOAD_DOWN" in script:
                    return True, "WORKLOAD_DOWN\n", "exit_0"
                if "LIFECYCLE_DATA_WRITTEN" in script:
                    return True, "LIFECYCLE_DATA_WRITTEN\n", "exit_0"
                if "LIFECYCLE_DATA_PERSISTED" in script:
                    return True, "LIFECYCLE_DATA_PERSISTED\n", "exit_0"
                self.fail("unexpected guest script")

            def approval(label):
                if "depth-2 probes" in label:
                    event = "approval:recursive-teardown"
                elif "prerequisites" in label:
                    event = "approval:oauth-prerequisites"
                elif "OAuth" in label:
                    event = "approval:oauth-start"
                else:
                    event = "approval:final-teardown"
                events.append(event)
                return "pass"

            with ExitStack() as stack:
                stack.enter_context(patch.object(acceptance.Path, "home", return_value=home))
                stack.enter_context(patch.object(acceptance.sys, "stdin", Terminal()))
                stack.enter_context(patch.object(acceptance, "Evidence", side_effect=make_evidence))
                stack.enter_context(patch.object(acceptance, "nested_gate", side_effect=lambda *a, **k: events.append("nested") or 0))
                stack.enter_context(patch.object(acceptance, "run_campaign_vm", side_effect=run_vm))
                stack.enter_context(patch.object(acceptance, "launch_campaign_agent", side_effect=launch))
                stack.enter_context(patch.object(acceptance, "observe_gate", side_effect=observe))
                stack.enter_context(patch.object(acceptance, "memory_budget_ok", return_value=True))
                stack.enter_context(patch.object(acceptance, "create_campaign_state_root", return_value=(state_root, shared_state)))
                stack.enter_context(patch.object(acceptance, "unique_campaign_profile", side_effect=lambda *a, **k: next(profile_names)))
                stack.enter_context(patch.object(acceptance, "private_recursive_source_workspace", return_value=(recursion_workspace, recursion_session)))
                stack.enter_context(patch.object(acceptance, "private_acceptance_workspace", return_value=workspace))
                stack.enter_context(patch.object(acceptance, "private_campaign_escrow", return_value=escrow))
                stack.enter_context(patch.object(acceptance, "prerequisite_prompt_probe", return_value=(True, "safe default-N prompt passed")))
                stack.enter_context(patch.object(acceptance, "host_port_available", return_value=True))
                stack.enter_context(patch.object(acceptance, "host_port_unreachable", return_value=True))
                stack.enter_context(patch.object(acceptance, "guest_openai_auth_stored", return_value=(True, "stored")))
                stack.enter_context(patch.object(acceptance, "run_guest_script", side_effect=guest_script))
                stack.enter_context(patch.object(acceptance, "process_matches_profile", side_effect=lambda path: running.get(path.name, False)))
                stack.enter_context(patch.object(acceptance, "campaign_state_residue_free", return_value=True))
                stack.enter_context(patch.object(acceptance, "campaign_state_root_safe", return_value=True))
                stack.enter_context(patch.object(acceptance, "private_tree_is_owned_non_symlink", return_value=True))
                stack.enter_context(patch.object(acceptance, "campaign_project_dir_safe", return_value=True))
                stack.enter_context(patch.object(acceptance, "campaign_profile_dir", side_effect=lambda root, name: root / "vm" / name))
                stack.enter_context(patch.object(acceptance, "escrow_file_is_private", return_value=True))
                def fake_run(args, **kwargs):
                    if args and args[0] == "git":
                        return subprocess.CompletedProcess(args, 0, "", "")
                    return subprocess.CompletedProcess(args, 0, '{"format":"qcow2","virtual-size":21474836480}', "")
                stack.enter_context(patch.object(acceptance, "run", side_effect=fake_run))
                stack.enter_context(patch.object(acceptance, "prompt_result", side_effect=approval))
                stack.enter_context(redirect_stdout(io.StringIO()))
                result = acceptance.vm_campaign("preserved-outer", "preserved-child", Path("/safe-snapshot"))

            self.assertEqual(result, 0, repr(evidence_items[0].data.get("checks", [])))
            evidence = evidence_items[0]
            self.assertEqual(evidence.data["outcome"], "PASS")
            self.assertTrue(evidence.data["campaign_gate_coverage_complete"])
            self.assertTrue(all(item["result"] == "PASS" for item in evidence.data["campaign_gate_coverage"].values()))
            self.assertEqual(events[0], "nested")
            self.assertLess(events.index("vm:models status all"), events.index("observe:tui"))
            self.assertLess(events.index("observe:tui"), events.index("approval:recursive-teardown"))
            self.assertLess(events.index("approval:recursive-teardown"), events.index("approval:oauth-prerequisites"))
            self.assertLess(events.index("approval:oauth-prerequisites"), events.index("approval:oauth-start"))
            self.assertLess(events.index("approval:oauth-start"), next(i for i, event in enumerate(events) if event.startswith("agent:vm-acceptance-main:browser=True")))
            self.assertLess(events.index("approval:final-teardown"), events.index(f"vm:stop {profile}"))
            destructive = [args for args in vm_calls if args[0] in ("stop", "down")]
            self.assertTrue(destructive)
            self.assertEqual({args[1] for args in destructive}, {recursion_profile, profile})
            self.assertIn("DEPTH3_REFUSED_BEFORE_STATE", chain_scripts[0])
            self.assertIn("vm-acceptance-depth1", chain_scripts[0])
            self.assertIn("vm-acceptance-depth2", chain_scripts[0])
            self.assertFalse(workspace.exists())
            self.assertFalse(state_root.exists())
            self.assertFalse(recursion_session.exists())

    def test_campaign_vm_environment_does_not_inherit_yes_or_unrelated_escrow(self):
        escrow = Path("/private/campaign-auth.json")
        with patch.dict(os.environ, {"VM_YES": "1", "LAB_YES": "1", "VM_AUTH_ESCROW_FILE": "/unexpected/escrow"}, clear=True), \
             patch.object(acceptance, "LAB_VM", Path("/repo/lab-vm")), \
             patch.object(acceptance, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as mocked:
            acceptance.run_campaign_vm(["down", "disposable"], auth_forward=True, escrow=escrow)
            enabled_env = mocked.call_args.kwargs["env"]
            self.assertEqual(enabled_env["VM_YES"], "0")
            self.assertEqual(enabled_env["LAB_YES"], "0")
            self.assertEqual(enabled_env["VM_AUTH_FORWARD"], "1")
            self.assertEqual(enabled_env["VM_AUTH_ESCROW_FILE"], str(escrow))

            acceptance.run_campaign_vm(["up", "disposable"], auth_forward=False)
            disabled_env = mocked.call_args.kwargs["env"]
            self.assertEqual(disabled_env["VM_AUTH_FORWARD"], "0")
            self.assertNotIn("VM_AUTH_ESCROW_FILE", disabled_env)

    def test_campaign_agent_launch_disables_inherited_credential_forwarding(self):
        evidence = DummyEvidence()
        with patch.dict(os.environ, {
            "VM_AUTH_FORWARD": "1", "VM_AUTH_ESCROW_FILE": "/private/other-profile-auth.json",
            "VM_YES": "1", "LAB_YES": "1",
        }, clear=True), \
             patch.object(acceptance, "LAB_VM", Path("/repo/lab-vm")), \
             patch.object(acceptance, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as mocked, \
             redirect_stdout(io.StringIO()):
            self.assertTrue(acceptance.launch_campaign_agent(
                "vm-acceptance-disposable", evidence, browser_auth=True,
            ))

        self.assertEqual(mocked.call_args.args[0], [
            "/repo/lab-vm", "agent", "vm-acceptance-disposable", "--browser-auth",
        ])
        env = mocked.call_args.kwargs["env"]
        self.assertEqual(env["VM_AUTH_FORWARD"], "0")
        self.assertNotIn("VM_AUTH_ESCROW_FILE", env)
        self.assertEqual(env["VM_YES"], "0")
        self.assertEqual(env["LAB_YES"], "0")
        self.assertFalse(mocked.call_args.kwargs["capture"])
        self.assertFalse(mocked.call_args.kwargs["no_input"])

    def test_campaign_refuses_destructive_teardown_after_any_failed_or_blocked_gate(self):
        for result, expected in (("FAIL", 1), ("BLOCKED", 2)):
            with self.subTest(result=result):
                evidence = DummyEvidence()
                evidence.data["checks"].append({
                    "name": "earlier gate", "result": result, "detail": "simulated result",
                })
                self.assertEqual(
                    acceptance.refuse_campaign_teardown_after_incomplete_gates(evidence),
                    expected,
                )
                teardown = next(item for item in evidence.data["checks"] if item["name"] == "disposable teardown")
                self.assertEqual(teardown["result"], "BLOCKED")
                self.assertIn("disposable state was preserved", teardown["detail"])

        passing = DummyEvidence()
        passing.check("earlier gate", True, "simulated result")
        self.assertIsNone(acceptance.refuse_campaign_teardown_after_incomplete_gates(passing))

    def test_disposable_workspace_is_private_and_publishes_only_guest_loopback_port(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            state = home / "state"
            with patch.object(acceptance.Path, "home", return_value=home), \
                 patch.dict(os.environ, {"VM_STATE_DIR": str(state)}, clear=True):
                workspace = acceptance.private_acceptance_workspace("vm-acceptance-test")
                escrow = acceptance.private_campaign_escrow("vm-acceptance-test")

            self.assertEqual(workspace.stat().st_mode & 0o777, 0o700)
            self.assertEqual((workspace / "compose.yaml").stat().st_mode & 0o777, 0o600)
            compose = (workspace / "compose.yaml").read_text()
            self.assertIn('"127.0.0.1:', compose)
            self.assertNotIn('"0.0.0.0:', compose)
            self.assertIn("./acceptance-bind:/acceptance-bind:ro", compose)
            self.assertIn("pg-data:/var/lib/postgresql/data", compose)
            config = json.loads((workspace / "opencode.json").read_text())
            self.assertEqual(config["permissions"], [
                {"action": "shell", "resource": "pwd", "effect": "ask"},
                {"action": "shell", "resource": "echo VM_ACCEPTANCE_DENIED", "effect": "deny"},
            ])
            self.assertEqual(escrow.parent.stat().st_mode & 0o777, 0o700)
            self.assertFalse(escrow.exists())
            feedback = acceptance.compose_feedback_script("vm-acceptance-test")
            self.assertIn("compose down -v", acceptance.compose_feedback_teardown_script("vm-acceptance-test"))
            self.assertIn("PortBindings", feedback)
            self.assertIn("BIND_MOUNT_AND_VOLUME_OK", feedback)
            self.assertIn("LIFECYCLE_DATA_PERSISTED", acceptance.lifecycle_workload_check_script("vm-acceptance-test"))
            with self.assertRaises(acceptance.AcceptanceError):
                acceptance.private_acceptance_workspace("../../unsafe")

    def test_campaign_escrow_refuses_checkout_path_before_creating_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "checkout"
            state_root = repo / ".local-state"
            repo.mkdir()
            with patch.object(acceptance, "ROOT", repo), \
                 patch.object(acceptance, "vm_state_root", return_value=state_root):
                with self.assertRaisesRegex(acceptance.AcceptanceError, "outside the checkout"):
                    acceptance.private_campaign_escrow("vm-acceptance-test")
            self.assertFalse((state_root / "secrets").exists())


if __name__ == "__main__":
    unittest.main()
