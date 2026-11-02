"""Safe local regressions for the owner-host acceptance runner."""

from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch


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


class HostAcceptanceTests(unittest.TestCase):
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

    def test_profile_base_matches_shell_empty_variable_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            home.mkdir()
            repo = Path(directory) / "repo"
            repo.mkdir()
            with patch.dict(os.environ, {"HOME": str(home), "VM_DIR_BASE": "", "VM_STATE_DIR": "", "XDG_DATA_HOME": ""}, clear=True):
                self.assertEqual(acceptance.profile_base(repo), Path("/"))
            with patch.dict(os.environ, {"HOME": str(home), "VM_STATE_DIR": "", "XDG_DATA_HOME": "/data"}, clear=True):
                self.assertEqual(acceptance.profile_base(repo), Path("/data/little-mester/vm"))

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
            (checkout / "lib/vm").mkdir(parents=True)
            (snapshot / "lib/vm").mkdir(parents=True)
            for relative in ("lib/vm/proxy.py", "lib/vm/runner.sh", "lib/vm/lifecycle.sh"):
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
            for root in (checkout, snapshot):
                (root / "lib/vm").mkdir(parents=True)
            for relative in acceptance.SNAPSHOT_FILES:
                (checkout / relative).write_text(f"current:{relative}")
                (snapshot / relative).write_text(f"old:{relative}")
            evidence = DummyEvidence(base / "evidence")
            with patch.object(acceptance, "active_snapshot_users", return_value=["123"]):
                self.assertFalse(acceptance.sync_snapshot_sources(snapshot, evidence, checkout))
            for relative in acceptance.SNAPSHOT_FILES:
                self.assertEqual((snapshot / relative).read_text(), f"old:{relative}")

    def test_snapshot_rebuild_excludes_host_state_and_preserves_old_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            checkout = base / "checkout"
            snapshot = base / ".cache/lab-vm-recursion/initial-launcher-smoke/workspace"
            checkout.mkdir()
            (checkout / "lib/vm").mkdir(parents=True)
            (checkout / "scripts").mkdir()
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
        answers = ["y", "v2.2.5", "y", "y", "y", "y", "y", "y"]
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_STATE_HOME": directory}), \
             patch("builtins.input", side_effect=answers), redirect_stdout(io.StringIO()):
            result = acceptance.observe_gate("auth")
            report = next((Path(directory) / "little-mester/host-acceptance").glob("*.json"))
            data = json.loads(report.read_text())
        self.assertEqual(result, 0)
        observation = data["observations"][0]
        self.assertEqual(observation["opencode_version"], "v2.2.5")
        self.assertEqual(observation["sensitive_material_recorded"], "no")
        self.assertFalse(any(key in observation for key in ("url", "token", "prompt", "raw_log")))

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

    def test_nested_gate_blocks_checks_not_reached_after_child_up_failure(self):
        evidence = DummyEvidence()
        summary = {"checks": {"child_up": False}}
        with patch.object(acceptance, "Evidence", return_value=evidence), \
             patch.object(acceptance, "run_preflight", return_value=(True, Path("/outer"))), \
             patch.object(acceptance, "run_nested_remote", return_value=(3, summary)), \
             redirect_stdout(io.StringIO()):
            result = acceptance.nested_gate(None, "launcher-smoke", Path("/snapshot"), ROOT)
        self.assertEqual(result, 3)
        outcomes = {check["name"]: check["result"] for check in evidence.data["checks"]}
        self.assertEqual(outcomes["child up"], "FAIL")
        self.assertEqual(outcomes["child acceptance"], "BLOCKED")
        self.assertEqual(outcomes["depth limit refused before state"], "BLOCKED")
        self.assertEqual(outcomes["outer session usable"], "BLOCKED")

    def test_nested_command_is_explicit_and_contains_no_teardown_or_tui(self):
        compile(acceptance.REMOTE_PREFLIGHT, "remote-preflight", "exec")
        compile(acceptance.REMOTE_ENSURE_NESTED_TOOLS, "remote-ensure-nested-tools", "exec")
        compile(acceptance.REMOTE_NESTED, "remote-nested", "exec")
        child = re.search(r"child_probe = r'''(.*?)'''", acceptance.REMOTE_NESTED, re.DOTALL)
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
        self.assertIn('parent_proxy = "http://10.0.2.100:3128"', acceptance.REMOTE_NESTED)
        self.assertIn(
            'env.update({key: parent_proxy for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")})',
            acceptance.REMOTE_NESTED,
        )
        self.assertIn(
            'env.update({key: "localhost,127.0.0.1,10.0.2.100" for key in ("NO_PROXY", "no_proxy")})',
            acceptance.REMOTE_NESTED,
        )
        self.assertIn('["./lab-vm", "-d", "up", profile, "--workspace", "/workspace", "--recurse"]', acceptance.REMOTE_NESTED)
        self.assertNotIn('"down"', acceptance.REMOTE_NESTED)
        self.assertNotIn('"stop"', acceptance.REMOTE_NESTED)
        self.assertNotIn('"--clean"', acceptance.REMOTE_NESTED)
        self.assertNotIn('"agent"', acceptance.REMOTE_NESTED)
        self.assertNotIn('"run"', acceptance.REMOTE_NESTED)

    def test_nested_profile_is_automatically_generated_by_default(self):
        args = acceptance.parser().parse_args(["nested"])
        self.assertIsNone(args.profile)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            acceptance.parser().parse_args(["sync-snapshot", "--confirm-reviewed"])

    def test_empty_xdg_state_home_uses_private_default_state_path(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"HOME": directory, "XDG_STATE_HOME": ""}, clear=True):
                evidence = acceptance.Evidence("test", "scope")
            self.assertEqual(evidence.directory, Path(directory) / ".local/state/little-mester/host-acceptance")
            self.assertEqual(evidence.path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
