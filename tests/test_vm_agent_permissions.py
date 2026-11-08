"""Temporary guest-tree fixtures only; no sudo, SSH or VM operations."""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("agent_permissions", ROOT / "scripts/vm-agent-permissions.py")
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


class AgentPermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / "usr/lib/node_modules/@opencode"
        self.executable = self.base / "cli/bin/opencode.exe"
        self.executable.parent.mkdir(parents=True)
        for directory in (self.root / "usr", self.root / "usr/lib", self.base.parent):
            directory.chmod(0o755)
        self.executable.write_text("fixture executable\n")
        self.executable.chmod(0o755)
        (self.root / "usr/bin").mkdir()
        (self.root / "usr/bin/opencode").symlink_to("../lib/node_modules/@opencode/cli/bin/opencode.exe")
        self.directories = [self.base, self.base / "cli", self.base / "cli/bin"]
        for directory in self.directories:
            directory.chmod(0o700)
        self.private = self.base / "cli/private-fixture"
        self.private.write_text("keep private\n")
        self.private.chmod(0o600)

    def run_guest(self, apply=False, repair_home=False):
        # Model root ownership with this fixture's own UID; retain real kernel
        # path, symlink, type and chmod behavior without needing root privileges.
        program = repair.GUEST_PROGRAM.replace('"/usr', '"' + str(self.root) + '/usr')
        program = program.replace('"/home/mester"', '"' + str(self.root / "home/mester") + '"')
        program = program.replace("os.geteuid() == 0", "os.geteuid() == os.getuid()")
        program = program.replace("info.st_uid == 0", "info.st_uid == os.getuid()")
        # Use this temporary tree's owner as the synthetic guest account, also
        # when CI itself runs as root. Never chown to an unrelated real account.
        program = program.replace('pwd.getpwnam("mester")',
                                  'type("FixtureAccount", (), {"pw_uid":os.getuid(),"pw_gid":os.getgid()})()')
        program = program.replace("account.pw_uid > 0", "account.pw_uid >= 0")
        mode = "apply-home" if repair_home else ("apply" if apply else "audit")
        return subprocess.run([sys.executable, "-c", program, mode],
                              capture_output=True, text=True, check=False)

    def prepare_home(self):
        home = self.root / "home/mester"
        (home / ".local/share/opencode").mkdir(parents=True)
        (home / ".config").mkdir()
        for path in (home / ".local", home / ".local/share", home / ".config"):
            path.chmod(0o755)
        home.chmod(0o750)
        secret = home / ".local/share/opencode/auth-fixture"
        secret.write_text("SECRET_CANARY")
        secret.chmod(0o600)
        return home, secret

    def test_home_repair_preserves_parent_modes_and_never_recurses_into_data(self):
        home, secret = self.prepare_home()
        before = secret.stat()
        result = self.run_guest(apply=True, repair_home=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("guest_home_parent_ownership=corrected", result.stdout)
        self.assertEqual(secret.stat(), before)
        self.assertEqual(secret.read_text(), "SECRET_CANARY")
        self.assertNotIn("SECRET_CANARY", result.stdout + result.stderr)
        self.assertEqual(home.stat().st_mode & 0o777, 0o750)
        for path in (home / ".local", home / ".local/share", home / ".config"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o755)

    def test_unsafe_home_parent_refuses_before_package_or_home_changes(self):
        home, secret = self.prepare_home()
        (home / ".config").rmdir()
        (home / ".config").symlink_to(self.base)
        before = secret.stat()
        result = self.run_guest(apply=True, repair_home=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("check=home-parent-2", result.stderr)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o700 for path in self.directories))
        self.assertEqual(secret.stat(), before)

    def test_audit_does_not_change_modes(self):
        result = self.run_guest()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o700 for path in self.directories))

    def test_audit_reports_only_fixed_home_metadata_without_reading_data(self):
        share = self.root / "home/mester/.local/share"
        share.mkdir(parents=True)
        share.chmod(0o755)
        secret = share / "do-not-read"
        secret.write_text("SECRET_CANARY")
        result = self.run_guest()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("guest_home_share=directory mode=0o755", result.stdout)
        self.assertIn("guest_home_opencode=absent", result.stdout)
        self.assertNotIn("SECRET_CANARY", result.stdout + result.stderr)
        self.assertNotIn("do-not-read", result.stdout + result.stderr)
        self.assertEqual(share.stat().st_mode & 0o777, 0o755)

    def test_apply_changes_only_three_public_directories_and_is_idempotent(self):
        for _ in range(2):
            result = self.run_guest(apply=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o755 for path in self.directories))
        self.assertEqual(self.private.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.private.read_text(), "keep private\n")

    def test_unexpected_mode_refuses_before_any_chmod(self):
        self.executable.chmod(0o777)
        result = self.run_guest(apply=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("check=executable-mode mode=0o777", result.stderr)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o700 for path in self.directories))

    def test_hardlinked_executable_is_unchanged_by_directory_only_repair(self):
        other = self.root / "second-link"
        os.link(self.executable, other)
        before = self.executable.stat()
        result = self.run_guest(apply=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o755 for path in self.directories))
        for path in (self.executable, other):
            after = path.stat()
            self.assertEqual((after.st_ino, after.st_mode, after.st_uid, after.st_nlink, after.st_mtime_ns),
                             (before.st_ino, before.st_mode, before.st_uid, before.st_nlink, before.st_mtime_ns))
            self.assertEqual(path.read_text(), "fixture executable\n")
        self.assertNotIn("fixture executable", result.stdout + result.stderr)

    def test_symlink_substitution_refuses_before_any_chmod(self):
        link = self.root / "usr/bin/opencode"
        link.unlink()
        link.symlink_to("/unrelated")
        result = self.run_guest(apply=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("check=bin-symlink", result.stderr)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o700 for path in self.directories))

    def test_nonready_profile_refuses_before_ssh(self):
        with (mock.patch.object(sys, "argv", ["repair", "--state-root", str(self.root),
                                             "--profile", "fixture", "--run-id", "run-one"]),
              mock.patch.object(repair.helpers.paths, "state_root", return_value=self.root),
              mock.patch.object(repair.helpers.paths, "profile_read", return_value={"status": "failed"}),
              mock.patch.object(repair.subprocess, "run") as start):
            with self.assertRaises(RuntimeError):
                repair.main()
            start.assert_not_called()

    def test_unknown_run_or_foreign_listener_refuses_before_ssh(self):
        run = self.root / "vm/profiles/fixture/runs/run-one"
        run.mkdir(parents=True, mode=0o700)
        identity = {"pid": 123, "start_ticks": 456, "executable": "/usr/bin/qemu-system-x86_64",
                    "argv": ["fixture-qemu"]}
        data = {"status": "ready", "runtime_identity": {**identity, "ssh_port": 40222}}
        for state, owned, registered in (("unknown", True, True), ("stopped", True, True),
                                         ("running", False, True), ("running", True, False)):
            with (self.subTest(state=state, owned=owned, registered=registered),
                  mock.patch.object(sys, "argv", ["repair", "--state-root", str(self.root),
                                                 "--profile", "fixture", "--run-id", "run-one", "--apply"]),
                  mock.patch.object(repair.helpers.paths, "state_root", return_value=self.root),
                  mock.patch.object(repair.helpers.paths, "profile_read", return_value=data),
                  mock.patch.object(repair.helpers.process, "registry_read", return_value={
                      "processes": {"qemu_1": {"identity": identity}} if registered else {}}),
                  mock.patch.object(repair.helpers.process, "identity_state", return_value=state),
                  mock.patch.object(repair.helpers.process, "listener_owned_by", return_value=owned),
                  mock.patch.object(repair.subprocess, "run") as start):
                with self.assertRaises(RuntimeError):
                    repair.main()
                start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
