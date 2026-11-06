"""Fixture-only recursion identity, preflight and candidate handoff coverage."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECURSION = ROOT / "lib/vm/recursion.sh"
CONFIG = ROOT / "lib/vm/config.sh"
SNAPSHOT = ROOT / "scripts/vm-recursion-workspace.py"


class RecursionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lm-vm-recursion-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.source = self.base / "source"
        self.source.mkdir(mode=0o700)
        (self.source / "lib/vm").mkdir(mode=0o755, parents=True)
        launcher = self.source / "lab-vm"
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        launcher.chmod(0o755)
        (self.source / "lib/vm/runner.sh").write_text("candidate runner\n", encoding="ascii")
        (self.source / "lib/vm/proxy.py").write_text("candidate proxy\n", encoding="ascii")
        (self.source / ".env").write_text("excluded-fixture-secret\n", encoding="ascii")
        self.git("init", "--quiet")
        self.git("config", "user.name", "Recursion Fixture")
        self.git("config", "user.email", "recursion-fixture@localhost.invalid")
        self.git("add", "--", "lab-vm", "lib/vm/runner.sh", "lib/vm/proxy.py")
        self.git("commit", "--quiet", "-m", "fixture baseline")
        self.cpuinfo = self.base / "cpuinfo"
        self.cpuinfo.write_text("processor : 0\nflags : fpu vmx sse\n", encoding="ascii")

    def clean_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = os.environ.copy()
        for key in ("LAB_VM_RECURSION_DEPTH", "LAB_VM_MAX_DEPTH", "LAB_VM_CANDIDATE_BUNDLE"):
            env.pop(key, None)
        if extra:
            env.update(extra)
        return env

    def git(self, *args: str):
        result = subprocess.run(
            ["git", "-C", str(self.source), *args],
            check=True,
            capture_output=True,
            text=True,
            env=self.clean_env({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}),
        )
        return result.stdout.strip()

    def bash(self, script: str, *args: str, env: dict[str, str] | None = None):
        return subprocess.run(
            ["bash", "-c", script, "_", str(ROOT), *map(str, args)],
            capture_output=True,
            text=True,
            check=False,
            env=env or self.clean_env(),
        )

    def test_defaults_to_zero_requires_explicit_nested_opt_in_and_caps_depth(self):
        result = self.bash(
            f'source "{CONFIG}"; source "{RECURSION}"; '
            'vm_cli_defaults; vm_recursion_configure run || exit $?; '
            'printf "%s %s\\n" "$VM_GUEST_RECURSION_DEPTH" "$VM_EFFECTIVE_MAX_DEPTH"'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0 0")

        missing_max = self.bash(
            f'source "{CONFIG}"; source "{RECURSION}"; vm_cli_defaults; '
            'VM_RECURSE=1; vm_recursion_configure run'
        )
        self.assertEqual(missing_max.returncode, 2)
        self.assertIn("explicit --max-depth", missing_max.stderr)

        missing_opt_in = self.bash(
            f'source "{CONFIG}"; source "{RECURSION}"; vm_cli_defaults; '
            'VM_SEEN[max-depth]=1; VM_MAX_DEPTH=2; '
            'export LAB_VM_RECURSION_DEPTH=1 LAB_VM_MAX_DEPTH=2; '
            'vm_recursion_configure up'
        )
        self.assertEqual(missing_opt_in.returncode, 2)
        self.assertIn("explicit --recurse", missing_opt_in.stderr)

        increased = self.bash(
            f'source "{CONFIG}"; source "{RECURSION}"; vm_cli_defaults; '
            'VM_RECURSE=1; VM_SEEN[max-depth]=1; VM_MAX_DEPTH=3; '
            'export LAB_VM_RECURSION_DEPTH=1 LAB_VM_MAX_DEPTH=2; '
            'vm_recursion_configure run'
        )
        self.assertEqual(increased.returncode, 2)
        self.assertIn("cannot raise", increased.stderr.lower())

        at_limit = self.bash(
            f'source "{CONFIG}"; source "{RECURSION}"; vm_cli_defaults; '
            'VM_RECURSE=1; VM_SEEN[max-depth]=1; VM_MAX_DEPTH=2; '
            'export LAB_VM_RECURSION_DEPTH=2 LAB_VM_MAX_DEPTH=2; '
            'vm_recursion_configure run'
        )
        self.assertEqual(at_limit.returncode, 2)
        self.assertIn("Maximum nesting depth", at_limit.stderr)

    def test_persisted_identity_is_private_and_must_match_environment(self):
        identity = self.base / "identity"
        identity.write_text("1 2\n", encoding="ascii")
        identity.chmod(0o600)
        accepted = self.bash(
            f'source "{RECURSION}"; vm_recursion_apply_guest_identity "$2" || exit $?; '
            'printf "%s %s\\n" "$LAB_VM_RECURSION_DEPTH" "$LAB_VM_MAX_DEPTH"',
            str(identity),
            env=self.clean_env({"LAB_VM_RECURSION_DEPTH": "1", "LAB_VM_MAX_DEPTH": "2"}),
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(accepted.stdout.strip(), "1 2")

        for value, mode, inherited in (
            ("1 2\n", 0o666, {"LAB_VM_RECURSION_DEPTH": "1", "LAB_VM_MAX_DEPTH": "2"}),
            ("1 2\n", 0o600, {"LAB_VM_RECURSION_DEPTH": "0", "LAB_VM_MAX_DEPTH": "2"}),
            ("2 1\n", 0o600, {}),
        ):
            with self.subTest(value=value, mode=mode, inherited=inherited):
                identity.write_text(value, encoding="ascii")
                identity.chmod(mode)
                result = self.bash(
                    f'source "{RECURSION}"; vm_recursion_apply_guest_identity "$2"',
                    str(identity),
                    env=self.clean_env(inherited),
                )
                self.assertEqual(result.returncode, 2)

    def test_kvm_and_cpu_preflight_happen_before_any_candidate_state(self):
        for kvm_result, cpu_contents in (("1", "flags : fpu vmx sse\n"), ("0", "flags : fpu sse\n")):
            with self.subTest(kvm_result=kvm_result, cpu_contents=cpu_contents):
                self.cpuinfo.write_text(cpu_contents, encoding="ascii")
                result = self.bash(
                    f'source "{RECURSION}"; VM_NESTED_LAUNCH=1 VM_RECURSE=1 '
                    'VM_EFFECTIVE_MAX_DEPTH=2; '
                    f'vm_recursion_kvm_available() {{ return {kvm_result}; }}; '
                    'vm_recursion_prepare_candidate "$2" child "$3" up fake-kvm "$4"',
                    str(self.state),
                    str(self.source),
                    str(self.cpuinfo),
                )
                self.assertEqual(result.returncode, 2)
                self.assertFalse(list(self.base.glob(".little-mester-recursion-*")))

    def test_candidate_argv_uses_changed_snapshot_and_separate_clean_state(self):
        result = self.bash(
            f'source "{CONFIG}"; source "{RECURSION}"; vm_cli_defaults; '
            'VM_NESTED_LAUNCH=1 VM_RECURSE=1 VM_EFFECTIVE_MAX_DEPTH=2 VM_MAX_DEPTH=2; '
            'VM_SEEN[max-depth]=1; '
            'VM_CONFIG_JSON="{\\"workload_runtime\\":\\"none\\",\\"agent\\":\\"opencode\\",\\"opencode_version\\":\\"2.3.4\\"}"; '
            'export LAB_VM_RECURSION_DEPTH=0 LAB_VM_MAX_DEPTH=2; '
            'vm_recursion_kvm_available() { return 0; }; '
            'vm_recursion_prepare_candidate "$2" child "$3" up fake-kvm "$4" || exit $?; '
            'printf "LAUNCHER\\n%s\\nWORKSPACE\\n%s\\nSTATE\\n%s\\nTREE\\n%s\\nARGV\\n" '
            '"${VM_RECURSION_CANDIDATE_ARGV[0]}" "$VM_RECURSION_CANDIDATE_WORKSPACE" '
            '"$VM_RECURSION_STATE_ROOT" "$VM_RECURSION_SOURCE_TREE_SHA256"; '
            'printf "%s\\n" "${VM_RECURSION_CANDIDATE_ARGV[@]}"; '
            'printf "ENV\\n"; printf "%s\\n" "${VM_RECURSION_CANDIDATE_ENV[@]}"; '
            'printf "CONTEXT\\n%s\\n" "$(vm_recursion_context_json '
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 2.3.4 1 2)"',
            str(self.state),
            str(self.source),
            str(self.cpuinfo),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        launcher = lines[lines.index("LAUNCHER") + 1]
        workspace = Path(lines[lines.index("WORKSPACE") + 1])
        child_state = Path(lines[lines.index("STATE") + 1])
        tree_digest = lines[lines.index("TREE") + 1]
        argv = lines[lines.index("ARGV") + 1 : lines.index("ENV")]
        env = lines[lines.index("ENV") + 1 : lines.index("CONTEXT")]
        context = json.loads(lines[lines.index("CONTEXT") + 1])

        self.assertEqual(Path(launcher), workspace / "lab-vm")
        self.assertTrue(launcher.startswith(str(self.base / ".little-mester-recursion-")))
        self.assertNotEqual(child_state, self.state)
        self.assertEqual(stat.S_IMODE(child_state.stat().st_mode), 0o700)
        manifest = json.loads((workspace.parent / "source-manifest.json").read_text())
        self.assertEqual(tree_digest, manifest["source_manifest"]["tree_sha256"])
        source_head = self.git("rev-parse", "HEAD")
        self.assertEqual(manifest["source_manifest"]["source_head"], source_head)
        self.assertEqual((workspace / "lib/vm/runner.sh").read_text(), "candidate runner\n")
        self.assertEqual((workspace / "lib/vm/proxy.py").read_text(), "candidate proxy\n")
        self.assertFalse((workspace / ".env").exists())
        self.assertEqual(argv[0], launcher)
        self.assertIn("--workspace", argv)
        self.assertIn(str(workspace), argv)
        self.assertIn("--recurse", argv)
        self.assertIn("--max-depth", argv)
        self.assertIn("2", argv)
        self.assertIn("--metrics", argv)
        self.assertIn("off", argv)
        environment = dict(item.split("=", 1) for item in env)
        self.assertEqual(environment["MESTER_STATE_DIR"], str(child_state))
        self.assertEqual(environment["HOME"], str(child_state))
        self.assertEqual(environment["TERM"], "dumb")
        self.assertEqual(environment["LANG"], "C.UTF-8")
        self.assertEqual(environment["LAB_VM_RECURSION_DEPTH"], "0")
        self.assertEqual(environment["LAB_VM_MAX_DEPTH"], "2")
        self.assertEqual(environment["PATH"], "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
        self.assertNotIn("METRICS_URL", environment)
        self.assertNotIn("SSH_AUTH_SOCK", environment)
        self.assertEqual(context["source_tree_sha256"], tree_digest)
        self.assertEqual(context["source_head"], source_head)
        self.assertEqual(context["candidate_payload_sha256"], "a" * 64)
        self.assertEqual(context["opencode_version"], "2.3.4")
        self.assertEqual(context["depth"], 1)
        self.assertEqual(context["max_depth"], 2)
        self.assertEqual(context["nested_kvm_preflight"], "passed")
        self.assertEqual(context["effective_state_root"], str(child_state))
        self.assertNotIn("source_path", context)

    def test_snapshot_verifier_rejects_a_symlink_alias(self):
        run_root = self.base / "run"
        run_root.mkdir(mode=0o700)
        result = subprocess.run(
            ["python3", "-B", str(SNAPSHOT), "prepare", "--source", str(self.source), "--run-root", str(run_root)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        bundle = Path(json.loads(result.stdout)["bundle"])
        alias = self.base / "bundle-alias"
        alias.symlink_to(bundle, target_is_directory=True)
        verified = subprocess.run(
            ["python3", "-B", str(SNAPSHOT), "verify", "--bundle", str(alias)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(verified.returncode, 2)


if __name__ == "__main__":
    unittest.main()
