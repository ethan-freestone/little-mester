#!/usr/bin/env python3
"""Parity checks for the single supported OpenCode version declaration."""

from __future__ import annotations

import importlib.util
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


policy = load("opencode_version_policy_test", ROOT / "lib/vm/opencode-version.py")
contract = load("opencode_version_session_contract_test",
                ROOT / "lib/vm/parent-session-contract.py")
bootstrap = load("opencode_version_guest_bootstrap_test", ROOT / "lib/vm/guest-bootstrap.py")
compatibility = load("opencode_version_guest_compatibility_test",
                     ROOT / "lib/vm/guest-compatibility.py")
parent_state = load("opencode_version_parent_state_test", ROOT / "lib/vm/parent-state.py")


class OpenCodeVersionPolicyTests(unittest.TestCase):
    def test_supported_version_remains_225_and_package_and_cli_expectations_derive_from_it(self):
        self.assertEqual(policy.SUPPORTED_VERSION, "2.2.5")
        self.assertEqual(policy.PACKAGE_NAME, "@opencode/cli")
        self.assertEqual(policy.PACKAGE_SPEC, f"@opencode/cli@{policy.SUPPORTED_VERSION}")
        self.assertEqual(policy.EXPECTED_EXECUTABLE_OUTPUT,
                         f"opencode v{policy.SUPPORTED_VERSION}")
        self.assertEqual(contract.SUPPORTED_OPENCODE_VERSION, policy.SUPPORTED_VERSION)
        self.assertEqual(contract.EXPECTED_OPENCODE_EXECUTABLE_OUTPUT,
                         policy.EXPECTED_EXECUTABLE_OUTPUT)
        self.assertEqual(contract.OPENCODE_PACKAGE_SPEC, policy.PACKAGE_SPEC)
        self.assertEqual(compatibility.OPENCODE_VERSION, policy.SUPPORTED_VERSION)

    def test_provisioning_and_runtime_checks_render_the_same_exact_contract(self):
        provision = bootstrap.provision_script(
            1000, "disk", "/usr/local/libexec/lm-guest-user-dirs.py")
        legacy = " ".join(bootstrap.build_legacy_parent_base(
            name="parent", uid=1000, ssh_key="ssh-ed25519 AAAA test")["runcmd"][0])
        for rendered in (provision, legacy):
            self.assertIn(f"npm install --global {policy.PACKAGE_SPEC}", rendered)
            self.assertNotIn("@OPENCODE_", rendered)
        self.assertIn(policy.EXPECTED_EXECUTABLE_OUTPUT, legacy)

        with tempfile.TemporaryDirectory(prefix="lm-standalone-bootstrap-") as temporary:
            standalone = pathlib.Path(temporary) / "guest-bootstrap.py"
            shutil.copyfile(ROOT / "lib/vm/guest-bootstrap.py", standalone)
            help_result = subprocess.run(
                [sys.executable, "-I", str(standalone), "--help"],
                stdin=subprocess.DEVNULL, capture_output=True,
                check=False, timeout=5)
            self.assertEqual(help_result.returncode, 0, help_result.stderr.decode("utf-8", "replace"))
            self.assertIn(b"install-npm", help_result.stdout)

        checks = contract.readiness_checks()
        self.assertIn(policy.EXPECTED_EXECUTABLE_OUTPUT, checks)
        self.assertIn(f"expected=opencode-v{policy.SUPPORTED_VERSION}", checks)
        self.assertIn("opencode_version_safe", checks)
        self.assertIn("tr -cd '[:alnum:].+ -'", checks)

        probe = parent_state._M2_PROTECTION_CHECK_SCRIPT
        self.assertIn(policy.SUPPORTED_VERSION, probe)
        self.assertIn(policy.EXPECTED_EXECUTABLE_OUTPUT, probe)
        self.assertIn("observed_opencode_executable_version", probe)
        self.assertIn("runtime_opencode_version_supported", probe)
        self.assertNotIn("runtime_is_pinned_v225", probe)

    def test_parent_state_probe_accepts_only_supported_executable_version_bytes(self):
        lines = [line.strip() for line in parent_state._M2_PROTECTION_CHECK_SCRIPT.splitlines()
                 if "'runtime_opencode_version_supported':" in line]
        self.assertEqual(len(lines), 1)
        expression = lines[0].partition(":")[2].strip().removesuffix("}").removesuffix(",")

        supported = subprocess.CompletedProcess(
            ["opencode", "--version"], 0,
            stdout=(policy.EXPECTED_EXECUTABLE_OUTPUT + "\n").encode("ascii"), stderr=b"")
        unsupported = subprocess.CompletedProcess(
            ["opencode", "--version"], 0, stdout=b"opencode v9.9.9\n", stderr=b"")
        failed = subprocess.CompletedProcess(
            ["opencode", "--version"], 1,
            stdout=(policy.EXPECTED_EXECUTABLE_OUTPUT + "\n").encode("ascii"), stderr=b"")

        self.assertTrue(eval(expression, {"version": supported}))
        self.assertFalse(eval(expression, {"version": unsupported}))
        self.assertFalse(eval(expression, {"version": failed}))

    def test_ci_readiness_and_installation_derive_the_policy_without_literal_pins(self):
        readiness = (ROOT / "lib/vm/opencode-readiness.sh").read_text()
        self.assertIn("@EXPECTED_OPENCODE_EXECUTABLE_OUTPUT@", readiness)
        self.assertIn("executable_version=%s package_version=%s", readiness)
        self.assertIn("reported_version()", readiness)
        self.assertIn('head -n 1 "$1"', readiness)

        stage = (ROOT / "tests/ci-kvm-stage.sh").read_text()
        self.assertIn("contract.OPENCODE_PACKAGE_SPEC", stage)
        self.assertIn("contract.EXPECTED_OPENCODE_EXECUTABLE_OUTPUT", stage)

        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        self.assertIn('runpy.run_path("lib/vm/opencode-version.py")', workflow)
        self.assertIn('"PACKAGE_SPEC"', workflow)
        self.assertIn('"EXPECTED_EXECUTABLE_OUTPUT"', workflow)

        live_consumers = (
            "lib/vm/guest-bootstrap.py", "lib/vm/guest-compatibility.py",
            "lib/vm/parent-session-contract.py", "lib/vm/parent-state.py",
            "lib/vm/opencode-readiness.sh", "lib/vm/browser-auth-session.py",
            "scripts/verify-exit-confirm.py", "tools/vm-owner-acceptance",
            ".github/workflows/ci.yml", "tests/ci-kvm-stage.sh",
        )
        for relative in live_consumers:
            with self.subTest(relative=relative):
                self.assertNotIn("2.2.5", (ROOT / relative).read_text())

    def test_authority_is_in_the_protected_runtime_and_acceptance_inventories(self):
        installer = (ROOT / "tools/vm-system-install").read_text()
        self.assertIn('"lib/vm/opencode-version.py":', installer)
        self.assertIn('"lib/vm/opencode-version.py",', installer)
        owner_acceptance = (ROOT / "tools/vm-owner-acceptance").read_text()
        self.assertIn('"lib/vm/opencode-version.py": 0o644', owner_acceptance)
        system_acceptance = (ROOT / "lib/vm/system-acceptance.py").read_text()
        self.assertIn('"opencode-version.py"', system_acceptance)
        ci_installer = (ROOT / "tests/ci-kvm-system-unit.py").read_text()
        self.assertIn('"lib/vm/opencode-version.py"', ci_installer)


if __name__ == "__main__":
    unittest.main(verbosity=2)
