#!/usr/bin/env python3
"""Static and isolated fail-closed checks for hosted production-unit KVM CI."""

from __future__ import annotations

import importlib.util
import os
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests/ci-kvm-system-unit.py"
SPEC = importlib.util.spec_from_file_location("ci_kvm_system_unit", SCRIPT)
unit = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(unit)
PROBE_SCRIPT = ROOT / "tests/ci-kvm-shared-launch-probe.py"
PROBE_SPEC = importlib.util.spec_from_file_location("ci_kvm_shared_launch_probe", PROBE_SCRIPT)
probe = importlib.util.module_from_spec(PROBE_SPEC)
assert PROBE_SPEC.loader is not None
PROBE_SPEC.loader.exec_module(probe)


class HostedSystemUnitTests(unittest.TestCase):
    def test_workflow_runs_production_unit_only_in_existing_ephemeral_hosted_kvm_job(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        job = workflow.split("  real-qemu-kvm-trial:", 1)[1].split("\n  # ── 4.", 1)[0]
        self.assertIn("runs-on: ubuntu-24.04", job)
        self.assertIn("GITHUB_SHA", job)
        self.assertIn("tests/ci-kvm-system-unit.py", job)
        self.assertIn("timeout-minutes: 45", job)
        self.assertIn("timeout-minutes: 90", job)
        self.assertNotIn("runs-on: self-hosted", job)
        self.assertNotIn("workflow_dispatch:", workflow)
        self.assertLess(job.index("Probe shared QEMU builder"),
                        job.index("Exercise exact pinned production systemd VM unit"))
        self.assertIn("evidence_scope=production_systemd_unit", SCRIPT.read_text())
        self.assertIn("ci-kvm-shared-launch-probe.py", job)
        self.assertIn("python3 tests/ci-kvm-system-unit.py --cleanup", job)
        self.assertIn("if: always()", job)

    def test_preflight_refuses_existing_fixed_path_account_group_and_install(self):
        sha = "a" * 40
        with tempfile.TemporaryDirectory() as temp:
            base = pathlib.Path(temp)
            install_parent = base / "v3"
            with mock.patch.dict(os.environ, {"GITHUB_SHA": sha}):
                with self.assertRaisesRegex(RuntimeError, "pre-existing fixed system VM path"):
                    (base / "collision").touch()
                    unit.fixed_name_preflight(paths=(str(base / "collision"),), users=(), groups=(),
                                              install_parent=install_parent)
                with mock.patch.object(unit.pwd, "getpwnam", side_effect=KeyError), \
                     mock.patch.object(unit.grp, "getgrnam", side_effect=KeyError):
                    (install_parent / sha).mkdir(parents=True)
                    with self.assertRaisesRegex(RuntimeError, "pre-existing pinned install"):
                        unit.fixed_name_preflight(paths=(), users=(), groups=(),
                                                  install_parent=install_parent)

    def test_preflight_preserves_preexisting_fixed_service_identity(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
                os.environ, {"GITHUB_SHA": "b" * 40}), \
                mock.patch.object(unit.pwd, "getpwnam", return_value=object()), \
                mock.patch.object(unit.grp, "getgrnam", side_effect=KeyError), \
                self.assertRaisesRegex(RuntimeError, "pre-existing fixed system VM user"):
            unit.fixed_name_preflight(paths=(), users=("lm-vm",), groups=(),
                                      install_parent=pathlib.Path(temp))

    def test_sudoers_bytes_use_only_the_fixed_read_only_sudo_cat(self):
        payload = b"%lm-vm-control ALL=(root) NOPASSWD: /usr/bin/systemctl stop little-mester-vm.service\n"
        with mock.patch.object(unit.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout=payload)) as run:
            self.assertEqual(unit.installed_asset_bytes("/etc/sudoers.d/little-mester-vm"),
                             payload)
        run.assert_called_once_with([
            "/usr/bin/sudo", "-n", "/usr/bin/cat", "--", "/etc/sudoers.d/little-mester-vm"],
            stdin=unit.subprocess.DEVNULL, capture_output=True, check=False)
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(unit, "run") as run:
            ordinary = pathlib.Path(temp) / "unit.service"
            ordinary.write_bytes(b"[Service]\n")
            self.assertEqual(unit.installed_asset_bytes(str(ordinary)), b"[Service]\n")
            run.assert_not_called()

    def test_proxy_unit_is_required_and_installed_bytes_and_metadata_must_match(self):
        proxy_dest = "/etc/systemd/system/little-mester-vm-proxy.service"
        self.assertEqual(unit.PINNED_INSTALL_ASSETS.get(proxy_dest),
                         ("systemd/little-mester-vm-proxy.service", 0o644))
        with tempfile.TemporaryDirectory() as temp:
            destination = pathlib.Path(temp) / "installed-proxy.service"
            payload = b"[Unit]\nDescription=pinned proxy\n"
            destination.write_bytes(payload)
            root_info = SimpleNamespace(st_mode=0o100644, st_uid=0)
            expected = {str(destination): ("systemd/little-mester-vm-proxy.service", 0o644)}
            with mock.patch.dict(unit.PINNED_INSTALL_ASSETS, expected, clear=True), \
                 mock.patch.dict(os.environ, {"GITHUB_SHA": "a" * 40}), \
                 mock.patch.object(unit.os, "lstat", return_value=root_info), \
                 mock.patch.object(unit, "installed_asset_bytes", return_value=payload), \
                 mock.patch.object(unit, "git_object_bytes", return_value=payload) as git_bytes, \
                 mock.patch.object(unit, "run") as run:
                unit.verify_installed_files()
                git_bytes.assert_called_once_with("a" * 40, "systemd/little-mester-vm-proxy.service")
                run.assert_called_once_with([
                    "/usr/bin/sudo", "-n", "/usr/sbin/visudo", "-cf",
                    "/etc/sudoers.d/little-mester-vm"])
                destination.write_bytes(b"[Unit]\nDescription=changed proxy\n")
                with mock.patch.object(unit, "installed_asset_bytes", return_value=destination.read_bytes()):
                    git_bytes.reset_mock()
                    git_bytes.return_value = payload
                    with self.assertRaisesRegex(RuntimeError, "installed production asset bytes differ"):
                        unit.verify_installed_files()
                with mock.patch.object(unit.os, "lstat",
                                       return_value=SimpleNamespace(st_mode=0o100600, st_uid=0)), \
                     self.assertRaisesRegex(RuntimeError, "installed production asset metadata mismatch"):
                    unit.verify_installed_files()

    def test_sudoers_compare_to_git_object_without_reading_candidate_file(self):
        dest = "/etc/sudoers.d/little-mester-vm"
        payload = b"%lm-vm-control ALL=(root) NOPASSWD: /usr/bin/systemctl stop\xff\n"
        with tempfile.TemporaryDirectory() as temp:
            candidate = pathlib.Path(temp) / "sudoers/little-mester-vm"
            candidate.parent.mkdir(parents=True)
            candidate.write_bytes(b"mutable candidate bytes")
            installed = SimpleNamespace(st_mode=0o100440, st_uid=0)
            original_read_bytes = pathlib.Path.read_bytes

            def guarded_read_bytes(path):
                if path == candidate:
                    raise PermissionError("candidate sudoers is unreadable")
                return original_read_bytes(path)

            with mock.patch.dict(unit.PINNED_INSTALL_ASSETS, {
                    dest: ("sudoers/little-mester-vm", 0o440)}, clear=True), \
                 mock.patch.dict(os.environ, {"GITHUB_SHA": "b" * 40}), \
                 mock.patch.object(unit.os, "lstat", return_value=installed), \
                 mock.patch.object(pathlib.Path, "read_bytes", guarded_read_bytes), \
                 mock.patch.object(unit, "installed_asset_bytes", return_value=payload) as installed_bytes, \
                 mock.patch.object(unit, "git_object_bytes", return_value=payload) as git_bytes, \
                 mock.patch.object(unit, "run"):
                unit.verify_installed_files()
            installed_bytes.assert_called_once_with(dest)
            git_bytes.assert_called_once_with("b" * 40, "sudoers/little-mester-vm")

    def test_known_hosts_is_read_only_through_fixed_sudo_cat_and_fails_closed(self):
        payload = b"[127.0.0.1]:22222 ssh-ed25519 AAAA\n"
        command = ["/usr/bin/sudo", "-n", "/usr/bin/cat", "--",
                   "/etc/little-mester-vm/known_hosts"]
        completed = SimpleNamespace(returncode=0, stdout=payload)
        with mock.patch.object(unit.subprocess, "run", return_value=completed) as run:
            self.assertEqual(unit.read_known_hosts(), payload)
        run.assert_called_once_with(command, stdin=unit.subprocess.DEVNULL,
                                    capture_output=True, check=False)

        source = SCRIPT.read_text()
        self.assertEqual(source.count("known_hosts = read_known_hosts()"), 1)
        self.assertEqual(source.count("known = read_known_hosts()"), 1)
        self.assertNotIn('pathlib.Path("/etc/little-mester-vm/known_hosts")', source)

        with mock.patch.object(unit.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stdout=b"")) as run, \
                self.assertRaisesRegex(RuntimeError, "command failed \\(1\\)"):
            unit.read_known_hosts()
        run.assert_called_once_with(command, stdin=unit.subprocess.DEVNULL,
                                    capture_output=True, check=False)

    def test_git_object_fetch_uses_exact_sha_path_and_raw_bytes(self):
        sha = "c" * 40
        payload = b"\xff\x00raw\n"
        completed = SimpleNamespace(returncode=0, stdout=payload, stderr=b"")
        with mock.patch.object(unit.subprocess, "run", return_value=completed) as run:
            self.assertEqual(unit.git_object_bytes(sha, "sudoers/little-mester-vm"), payload)
        run.assert_called_once_with([
            "/usr/bin/git", "-C", str(unit.REPO), "show",
            f"{sha}:sudoers/little-mester-vm"], stdin=unit.subprocess.DEVNULL,
            capture_output=True, check=False)
        run.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "full exact commit SHA"):
            unit.git_object_bytes("not-a-sha", "sudoers/little-mester-vm")
        with self.assertRaisesRegex(RuntimeError, "untrusted pinned source path"):
            unit.git_object_bytes(sha, "../mutable-candidate")
        run.assert_not_called()

    def test_system_manager_starting_state_is_not_a_preflight_gate(self):
        completed = SimpleNamespace(returncode=0, stdout=b"257\n")
        with mock.patch.object(probe.shutil, "which", return_value="/usr/bin/tool"), \
             mock.patch.object(probe.subprocess, "run", side_effect=[completed,
                                                                         SimpleNamespace(returncode=0)]) as run:
            available, reasons = probe.systemd_preflight()
        self.assertTrue(available)
        self.assertEqual(reasons, [])
        self.assertEqual(run.call_args_list[0].args[0],
                         ["systemctl", "show", "--property=Version", "--value"])

        with mock.patch.object(unit.pathlib.Path, "is_file", return_value=True), \
             mock.patch.object(unit.subprocess, "run", side_effect=[
                 SimpleNamespace(returncode=0, stdout="257\n"),
                  SimpleNamespace(returncode=0)]):
            unit.production_host_preflight()

    def test_nonresponsive_system_manager_rejects_before_install(self):
        with mock.patch.object(unit, "hosted_kvm_guard"), \
             mock.patch.object(unit.pathlib.Path, "is_file", return_value=True), \
             mock.patch.object(unit.subprocess, "run", side_effect=[
                 SimpleNamespace(returncode=1, stdout=""),
                 SimpleNamespace(returncode=0)]), \
             mock.patch.object(unit, "run") as run, \
             mock.patch.object(unit.sys, "argv", [str(SCRIPT)]):
            self.assertEqual(unit.main(), 1)
        run.assert_not_called()

    def test_shared_probe_separates_system_manager_and_noninteractive_sudo_failures(self):
        def completed(code, output=b""):
            return SimpleNamespace(returncode=code, stdout=output)

        with mock.patch.object(probe.shutil, "which", return_value="/usr/bin/tool"), \
             mock.patch.object(probe.subprocess, "run", side_effect=[completed(1, b""),
                                                                         completed(0)]) as run:
            available, reasons = probe.systemd_preflight()
        self.assertFalse(available)
        self.assertEqual(reasons, ["system-manager(scope=system,reason=command-failed,exit=1)"])
        self.assertEqual(run.call_args_list[0].args[0],
                         ["systemctl", "show", "--property=Version", "--value"])
        self.assertNotIn("--user", run.call_args_list[0].args[0])

        with mock.patch.object(probe.shutil, "which", return_value="/usr/bin/tool"), \
             mock.patch.object(probe.subprocess, "run", side_effect=[completed(0, b"257\n"),
                                                                         completed(1)]):
            available, reasons = probe.systemd_preflight()
        self.assertFalse(available)
        self.assertEqual(reasons, ["sudo(mode=noninteractive,exit=1)"])

    def test_shared_probe_reports_timeout_separately(self):
        with mock.patch.object(probe.shutil, "which", return_value="/usr/bin/tool"), \
                 mock.patch.object(probe.subprocess, "run", side_effect=[
                  __import__("subprocess").TimeoutExpired("systemctl", 2),
                  SimpleNamespace(returncode=0, stdout=b"")]):
            available, reasons = probe.systemd_preflight()
        self.assertFalse(available)
        self.assertEqual(reasons, ["system-manager(scope=system,reason=timeout)"])

    def test_shared_probe_rejects_empty_manager_version(self):
        with mock.patch.object(probe.shutil, "which", return_value="/usr/bin/tool"), \
             mock.patch.object(probe.subprocess, "run", side_effect=[
                 SimpleNamespace(returncode=0, stdout=b""),
                 SimpleNamespace(returncode=0)]):
            available, reasons = probe.systemd_preflight()
        self.assertFalse(available)
        self.assertEqual(reasons, ["system-manager(scope=system,reason=empty-version)"])

    def test_fixed_units_must_be_loaded_before_start(self):
        with mock.patch.object(unit, "service_state", side_effect=[
                {"LoadState": "loaded"}, {"LoadState": "not-found"}]) as state, \
                self.assertRaisesRegex(RuntimeError, "not both loaded"):
            unit.require_fixed_units_loaded()
        self.assertEqual(state.call_args_list, [mock.call(unit.UNIT), mock.call(unit.PROXY_UNIT)])

    def test_cleanup_requires_this_attempts_durable_start_marker(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {
                "RUNNER_TEMP": temp, "GITHUB_RUN_ID": "12345", "GITHUB_RUN_ATTEMPT": "2",
                "GITHUB_SHA": "c" * 40,
        }):
            self.assertFalse(unit.has_valid_start_marker("c" * 40))
            path = unit.write_start_marker("c" * 40)
            self.assertTrue(unit.has_valid_start_marker("c" * 40))
            with self.assertRaisesRegex(RuntimeError, "mismatched production-unit start evidence"):
                unit.has_valid_start_marker("d" * 40)
            self.assertTrue(path.exists())

    def test_cleanup_performs_no_lifecycle_operation_when_start_was_never_attempted(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {
                "RUNNER_TEMP": temp, "GITHUB_RUN_ID": "12345", "GITHUB_RUN_ATTEMPT": "2",
                "GITHUB_SHA": "e" * 40,
        }), mock.patch.object(unit, "hosted_kvm_guard"), \
                mock.patch.object(unit, "invoke_lab") as invoke, \
                mock.patch.object(unit, "invoke_verified_cleanup") as verified:
            self.assertEqual(unit.cleanup_only(), 0)
            invoke.assert_not_called()
            verified.assert_not_called()

    def test_script_uses_production_fixed_operations_and_bounded_stop_cleanup(self):
        source = SCRIPT.read_text()
        for required in (
            '"--install", "--sha", sha',
            '"tools/vm-system-install"',
            'f"{root}/lab-vm system {operation}"',
            '"up", 16 * 60 + 45',
            '"down", 45',
            'invoke_lab(root, "down", 60, check=False)',
            'same_disk=true',
            'production_unit_normal_stop=PASS qmp_execstop=true graceful=true',
            'signal.pidfd_send_signal(pidfd, signal.SIGSTOP)',
            'stopped.get("Result") == "timeout"',
            'poller.poll(1000)',
            'TimeoutStopSec=25s',
            'KillMode=control-group',
            'SendSIGKILL=yes',
            'production_unit_forced_stop=PASS',
            'restrict=on',
            'hostfwd=tcp:127.0.0.1:22222-:22',
            'production_tui_probe(installed_root)',
            '"/usr/bin/sg", ["lm-vm-control", "-c", f"{root}/lab-vm system tui"]',
            'child.expect(b"Ask anything"',
            'child.sendcontrol("c")',
            'child.expect(b"Close Little Mester?"',
            'child.send(b"q")',
            'production_unit_direct_egress=PASS target=192.0.2.1:443 connected=false',
            'production_proxy_private_target_policy=PASS existing_unit_tests=true',
            'production_unit_cleanup=PASS',
            'installed_asset_bytes(dest)',
            '"--system-vm", "down"',
            'invoke_verified_cleanup(sha)',
        ):
            self.assertIn(required, source)
        for forbidden in ("systemctl enable", "systemctl disable", "unlink(", "shutil.rmtree",
                          "rm -rf"):
            self.assertNotIn(forbidden, source)
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        kvm_job = workflow.split("  real-qemu-kvm-trial:", 1)[1].split("\n  # ── 4.", 1)[0]
        self.assertIn("python3-pexpect", kvm_job)
        self.assertIn("ncurses-bin", kvm_job)


if __name__ == "__main__":
    unittest.main()
