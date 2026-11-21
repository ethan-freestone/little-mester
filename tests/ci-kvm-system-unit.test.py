#!/usr/bin/env python3
"""Static and isolated fail-closed checks for hosted production-unit KVM CI."""

from __future__ import annotations

import ast
import base64
import importlib.util
import io
import json
import os
import pathlib
import re
import shlex
import subprocess
import tempfile
import textwrap
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
    def test_workflow_separates_production_acceptance_from_transient_guest_job(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        transient = workflow.split("  transient-qemu-integration:", 1)[1].split(
            "  # ── 5. Exact installed production", 1)[0]
        production = workflow.split("  production-systemd-vm-acceptance:", 1)[1].split(
            "  # ── 6.", 1)[0]
        self.assertIn("name: Transient QEMU guest integration", transient)
        self.assertIn("name: Production systemd VM acceptance", production)
        self.assertIn("runs-on: ubuntu-24.04", production)
        self.assertIn("GITHUB_SHA", production)
        self.assertIn("tests/ci-kvm-system-unit.py", production)
        self.assertIn("timeout-minutes: 45", production)
        self.assertIn("timeout-minutes: 90", production)
        self.assertNotIn("runs-on: self-hosted", production)
        self.assertNotIn("tests/ci-kvm-system-unit.py", transient)
        self.assertNotIn("needs:", transient + production)
        self.assertNotIn("steps.smoke_cleanup", production)
        self.assertIn("steps.host_dependencies.outcome == 'success'", production)
        self.assertIn("LM_CI_SOURCE_HEAD_SHA", production)
        self.assertIn("pr_merge_commit_second_parent", production)
        self.assertIn("LM_CI_PR_MERGE_PARENT_SHA", production)
        self.assertNotIn("workflow_dispatch:", workflow)
        self.assertIn("workflow_sha={sha} source_head_sha={source_sha}", SCRIPT.read_text())
        self.assertIn("pr_merge_commit_second_parent={merge_parent}", SCRIPT.read_text())
        self.assertIn("LM_CI_SOURCE_HEAD_SHA", SCRIPT.read_text())
        self.assertEqual(unit.CI_INSTALL_PREFIX, pathlib.Path("/srv/little-mester-ci"))
        self.assertEqual(unit.CI_INSTALL_PARENT, unit.CI_INSTALL_PREFIX / "v3")
        self.assertLess(production.index("Exercise exact pinned production systemd VM unit"),
                        production.index("Require production systemd VM and proxy stopped"))
        self.assertIn("if: always()", production)
        self.assertIn("python3 tests/ci-kvm-system-unit.py --cleanup", production)
        self.assertIn("production_unit_forced_stop=PASS", SCRIPT.read_text())
        self.assertIn("cancel-in-progress: true", workflow)
        self.assertNotIn("concurrency:", transient)
        self.assertNotIn("concurrency:", production)
    def test_boot_readiness_has_bounded_three_minute_window(self):
        stage = (ROOT / "tests/ci-kvm-stage.sh").read_text()
        boot = stage.split('if [[ "$requested_stage" == boot ]]; then', 1)[1].split(
            'if [[ "$requested_stage" == "restricted-reboot-egress-persistence" ]]; then', 1
        )[0]
        self.assertIn("stage_start boot 180", boot)
        self.assertIn("readiness_deadline=$((qemu_started + 180))", boot)
        self.assertIn("readiness_limit=180s integration_limit=300s", boot)
        self.assertIn("readiness within 180s", boot)

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

    def test_default_install_preflight_uses_the_selected_ci_prefix(self):
        sha = "f" * 40
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {
                "GITHUB_SHA": sha, "RUNNER_TEMP": temporary,
                "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
        }), mock.patch.object(unit, "CI_INSTALL_PREFIX", pathlib.Path(temporary) / "ci-prefix"), \
                mock.patch.object(unit, "CI_INSTALL_PARENT", pathlib.Path(temporary) / "ci-prefix/v3"):
            target = unit.CI_INSTALL_PARENT / sha
            target.mkdir(parents=True)
            with self.assertRaisesRegex(RuntimeError, "preserving pre-existing pinned install"):
                unit.fixed_name_preflight(paths=(), users=(), groups=())

    def test_preflight_preserves_preexisting_fixed_service_identity(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
                os.environ, {"GITHUB_SHA": "b" * 40}), \
                mock.patch.object(unit.pwd, "getpwnam", return_value=object()), \
                mock.patch.object(unit.grp, "getgrnam", side_effect=KeyError), \
                self.assertRaisesRegex(RuntimeError, "pre-existing fixed system VM user"):
            unit.fixed_name_preflight(paths=(), users=("lm-vm",), groups=(),
                                      install_parent=pathlib.Path(temp))

    def test_sudoers_preflight_uses_root_lstat_and_absence_is_the_only_allow(self):
        dest = unit.PROTECTED_PREFLIGHT_PATH
        command = ["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-c",
                   unit.PROTECTED_PREFLIGHT_CHECK, dest]
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {
                "GITHUB_SHA": "c" * 40, "RUNNER_TEMP": temp,
                "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
        }), mock.patch.object(unit.subprocess, "run", return_value=SimpleNamespace(
                returncode=1)) as run:
            unit.fixed_name_preflight(paths=(dest,), users=(), groups=(),
                                      install_parent=pathlib.Path(temp) / "v3")
        run.assert_called_once_with(command, stdin=unit.subprocess.DEVNULL,
                                    stdout=unit.subprocess.DEVNULL,
                                    stderr=unit.subprocess.DEVNULL, timeout=5, check=False)

        # lstat succeeds for any directory entry, including a symlink; status 0
        # must always preserve it rather than treating it as absent.
        for code, message in ((0, "pre-existing fixed system VM path"),
                              (2, "unable to safely inspect fixed system VM path"),
                              (127, "unable to safely inspect fixed system VM path")):
            with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {
                    "GITHUB_SHA": "c" * 40, "RUNNER_TEMP": temp,
                    "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
            }), mock.patch.object(unit.subprocess, "run", return_value=SimpleNamespace(
                    returncode=code)), self.assertRaisesRegex(RuntimeError, message):
                unit.fixed_name_preflight(paths=(dest,), users=(), groups=(),
                                          install_parent=pathlib.Path(temp) / "v3")

    def test_preflight_other_paths_still_use_unprivileged_path_checks(self):
        original_exists = pathlib.Path.exists
        original_is_symlink = pathlib.Path.is_symlink
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {
                "GITHUB_SHA": "d" * 40, "RUNNER_TEMP": temp,
                "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
        }), mock.patch.object(pathlib.Path, "exists", autospec=True,
                              side_effect=original_exists) as exists, \
                mock.patch.object(pathlib.Path, "is_symlink", autospec=True,
                                  side_effect=original_is_symlink) as is_symlink, \
                mock.patch.object(unit.subprocess, "run") as run:
            ordinary = pathlib.Path(temp) / "ordinary"
            unit.fixed_name_preflight(paths=(str(ordinary),),
                                      users=(), groups=(),
                                      install_parent=pathlib.Path(temp) / "v3")
            run.assert_not_called()
            exists.assert_any_call(ordinary)
            is_symlink.assert_any_call(ordinary)

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
            original_read_bytes = pathlib.Path.read_bytes

            def guarded_read_bytes(path):
                if path == candidate:
                    raise PermissionError("candidate sudoers is unreadable")
                return original_read_bytes(path)

            with mock.patch.dict(unit.PINNED_INSTALL_ASSETS, {
                    dest: ("sudoers/little-mester-vm", 0o440)}, clear=True), \
                 mock.patch.dict(os.environ, {"GITHUB_SHA": "b" * 40}), \
                 mock.patch.object(unit.subprocess, "run", return_value=SimpleNamespace(
                     returncode=0, stdout=b"8120:0\n")) as metadata_run, \
                 mock.patch.object(pathlib.Path, "read_bytes", guarded_read_bytes), \
                 mock.patch.object(unit, "installed_asset_bytes", return_value=payload) as installed_bytes, \
                 mock.patch.object(unit, "git_object_bytes", return_value=payload) as git_bytes, \
                 mock.patch.object(unit, "run"):
                unit.verify_installed_files()
            metadata_run.assert_called_once_with([
                "/usr/bin/sudo", "-n", "/usr/bin/stat", "--format=%f:%u", "--", dest],
                stdin=unit.subprocess.DEVNULL, capture_output=True, check=False)
            installed_bytes.assert_called_once_with(dest)
            git_bytes.assert_called_once_with("b" * 40, "sudoers/little-mester-vm")

    def test_sudoers_privileged_metadata_parsing_and_policy_refusal(self):
        dest = "/etc/sudoers.d/little-mester-vm"
        command = ["/usr/bin/sudo", "-n", "/usr/bin/stat", "--format=%f:%u", "--", dest]
        with mock.patch.object(unit.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout=b"8120:0\n")) as run:
            self.assertEqual(unit.installed_asset_metadata(dest), (0o100440, 0))
        run.assert_called_once_with(command, stdin=unit.subprocess.DEVNULL,
                                    capture_output=True, check=False)

        for metadata in (b"8180:0\n", b"8120:1\n"):
            with mock.patch.dict(unit.PINNED_INSTALL_ASSETS, {
                    dest: ("sudoers/little-mester-vm", 0o440)}, clear=True), \
                 mock.patch.dict(os.environ, {"GITHUB_SHA": "c" * 40}), \
                 mock.patch.object(unit.subprocess, "run", return_value=SimpleNamespace(
                     returncode=0, stdout=metadata)), \
                 self.assertRaisesRegex(RuntimeError, "installed production asset metadata mismatch"):
                unit.verify_installed_files()

        for malformed in (b"8120:0", b"8120:x\n", b"not-metadata\n"):
            with mock.patch.object(unit.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout=malformed)), \
                 self.assertRaisesRegex(RuntimeError, "invalid privileged installed asset metadata"):
                unit.installed_asset_metadata(dest)

    def test_shared_acceptance_reads_group_protected_ssh_assets_without_root_interface(self):
        source = (ROOT / "lib/vm/system-acceptance.py").read_text()
        self.assertIn('pathlib.Path("/etc/little-mester-vm/control_ed25519").read_bytes()', source)
        self.assertIn('pathlib.Path("/etc/little-mester-vm/known_hosts").read_bytes()', source)
        self.assertIn('"/usr/bin/sudo", "-n", "/usr/bin/cat", "--", destination', source)

    def test_ci_invokes_exact_installed_owner_acceptance_command(self):
        source = SCRIPT.read_text()
        self.assertIn('installed = CI_INSTALL_PARENT / sha / "tools/vm-owner-acceptance"', source)
        self.assertIn('"--system-vm", "accept", "--approve-system-vm-lifecycle"', source)
        self.assertIn("invoke_shared_acceptance(sha)", source)
        self.assertNotIn('invoke_lab(installed_root, "up", 16 * 60 + 45)', source)

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
                {"LoadState": "loaded"}, {"LoadState": "loaded"},
                {"LoadState": "loaded"}, {"LoadState": "not-found"}]) as state, \
                self.assertRaisesRegex(RuntimeError, "not all loaded"):
            unit.require_fixed_units_loaded()
        self.assertEqual(state.call_args_list, [mock.call(unit.UNIT), mock.call(unit.PROXY_UNIT),
                                                mock.call(unit.WORKSPACE_UNIT),
                                                mock.call(unit.METRICS_UNIT)])

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

    def test_verified_cleanup_passes_the_selected_install_prefix(self):
        with mock.patch.object(unit, "run") as run:
            unit.invoke_verified_cleanup("a" * 40)
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["/usr/bin/sg", "lm-vm-control", "-c"])
        self.assertIn("--prefix", shlex.split(command[3]))
        self.assertEqual(shlex.split(command[3])[shlex.split(command[3]).index("--prefix") + 1],
                         str(unit.CI_INSTALL_PREFIX))

    def test_ci_activates_post_install_control_group_for_shared_acceptance(self):
        sha = "a" * 40
        with mock.patch.object(unit, "run") as run:
            unit.invoke_shared_acceptance(sha)
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["/usr/bin/sg", "lm-vm-control", "-c"])
        self.assertIn(f"/tools/vm-owner-acceptance --system-vm accept", command[3])
        self.assertIn("--approve-system-vm-lifecycle", command[3])

    def test_fresh_production_install_configures_disposable_host_source_before_real_unit_start(self):
        source = SCRIPT.read_text()
        configure = source.index("def configure_disposable_workspace")
        configure_body_end = source.index("def remove_disposable_workspace", configure)
        config_body = source[configure:configure_body_end]
        main = source[source.index("def main()") :]
        self.assertIn("disposable_workspace_path()", config_body)
        self.assertIn('"owner_uid": os.getuid()', config_body)
        self.assertIn('"unsafe_recurse": False', config_body)
        self.assertIn("expected_bind = WORKSPACE_MODULE._systemd_bind_path(workspace)", config_body)
        self.assertNotIn(":/workspace", config_body)
        self.assertLess(main.index("configure_disposable_workspace(installed_root)"),
                        main.index("invoke_shared_acceptance(sha)"))
        self.assertIn("/run/little-mester-vm-workspace-share", source)

    def test_hosted_acceptance_requires_real_workspace_exec_start_post_success(self):
        source = SCRIPT.read_text()
        checker = source[source.index("def verify_workspace_startup_post"):
                         source.index("def disposable_workspace_path")]
        main = source[source.index("def main()") :]
        self.assertIn('"--property=ExecStartPost"', checker)
        self.assertIn('"socket-permissions"', checker)
        self.assertIn('"code=exited"', checker)
        self.assertIn('"production workspace ExecStartPost did not complete', checker)
        self.assertLess(main.index("invoke_shared_acceptance(sha)"),
                        main.index("verify_workspace_startup_post()"))
        self.assertLess(main.index("verify_workspace_startup_post()"),
                        main.index("verify_guest_cannot_read_escape()"))

    def test_workspace_service_preserves_home_isolation_while_allowing_explicit_bind(self):
        unit = (ROOT / "systemd/little-mester-vm-workspace.service").read_text()
        self.assertIn("ProtectHome=tmpfs", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("RuntimeDirectory=little-mester-vm-workspace little-mester-vm-workspace-share", unit)
        self.assertNotIn("ProtectHome=yes", unit)
        helper = (ROOT / "lib/vm/system-vm-workspace.py").read_text()
        self.assertIn('"destination_mount": target_mount', helper)
        self.assertIn('"dropin_matches_assignment": dropin_matches', helper)
        self.assertIn("WORKSPACE_BIND_DIAGNOSTIC ", helper)
        self.assertIn("approved host workspace source changed after assignment", helper)

    def test_hosted_ci_runs_real_systemd_bind_matrix_before_production_start(self):
        source = SCRIPT.read_text()
        repro = source[source.index("def _bind_repro_case"):
                       source.index("def remove_disposable_workspace")]
        main = source[source.index("def main()") :]
        self.assertIn("/usr/bin/systemd-run", repro)
        self.assertIn("RuntimeDirectoryPreserve=no", repro)
        self.assertIn("_run_transient_bind_case(", repro)
        self.assertIn("_report_transient_bind_unit(unit_name)", source)
        self.assertIn('"workspace_bind_transient_unit_effective=PASS"', source)
        self.assertIn("/run/little-mester-vm-workspace-share", repro)
        self.assertIn('(workspace, "home", "yes", True)', repro)
        self.assertIn('(workspace, "home", "tmpfs", True)', repro)
        self.assertIn('(outside, "outside-home", "yes", True)', repro)
        self.assertIn('(outside, "outside-home", "tmpfs", True)', repro)
        self.assertIn("WORKSPACE_BIND_REPRO_TARGET", repro)
        self.assertIn("WORKSPACE_BIND_REPRO_IDENTITY", repro)
        self.assertIn("unrelated_home_hidden", repro)
        self.assertIn("identity_matches", repro)
        self.assertIn("_require_runner_directory(source, source_stat)", repro)
        self.assertIn("controller_euid", repro)
        self.assertIn("tempfile.mkdtemp(", source)
        self.assertIn('temp_parent = pathlib.Path("/var/tmp")', source)
        self.assertIn("workspace_bind_checkpoint=", source)
        self.assertIn("workspace_bind_exception=", source)
        self.assertIn('"RuntimeDirectory,RuntimeDirectoryMode,RuntimeDirectoryPreserve,"', source)
        self.assertIn('"BindPaths,ReadWritePaths,ProtectHome,ProtectSystem,PrivateTmp,"', source)
        self.assertNotIn('outside = runtime / f"lm-vm-bind-control-', source)
        self.assertIn("stat.S_IMODE(info.st_mode) == 0o700", source)
        self.assertIn("workspace_effective_systemd=", source)
        self.assertIn("workspace_production_prestart_paths=", source)
        module = ast.parse(source)
        bind_case = next(node for node in module.body
                         if isinstance(node, ast.FunctionDef) and node.name == "_bind_repro_case")
        code_assignment = next(node for node in ast.walk(bind_case)
                               if isinstance(node, ast.Assign)
                               and any(isinstance(target, ast.Name) and target.id == "code"
                                       for target in node.targets))
        code = textwrap.dedent(ast.literal_eval(code_assignment.value.args[0]))
        compile(code, "<workspace-bind-reproduction>", "exec")
        self.assertLess(main.index("report_workspace_unit_configuration()"),
                        main.index("run_workspace_bind_reproductions(workspace_path)"))
        self.assertLess(main.index("run_workspace_bind_reproductions(workspace_path)"),
                        main.index("invoke_shared_acceptance(sha)"))
        matrix = source[source.index("def run_workspace_bind_reproductions"):
                        source.index("def run_production_dependency_reproduction")]
        self.assertIn("run_production_dependency_reproduction(workspace)", matrix)

    def test_production_dependency_reproduction_uses_exact_bind_and_sandbox_before_vm_start(self):
        source = SCRIPT.read_text()
        template = (ROOT / "systemd/little-mester-vm-workspace.service").read_text()
        config = source[source.index("def production_workspace_reproduction_config"):
                        source.index("def run_production_dependency_reproduction")]
        repro = config + source[source.index("def run_production_dependency_reproduction"):
                                source.index("def remove_disposable_workspace")]
        self.assertIn('systemd/little-mester-vm-workspace.service', repro)
        self.assertIn('ExecStart={workspace_bind_probe_exec(workspace, destination)}', repro)
        self.assertIn("WORKSPACE_FILE_BIND_PROBE", repro)
        self.assertIn("exact_destination_mountpoint", repro)
        self.assertIn("unrelated_home_hidden", repro)
        self.assertIn('ExecStartPost=/usr/bin/true', repro)
        for directive in (
            "Type=exec", "RuntimeDirectory=little-mester-vm-workspace little-mester-vm-workspace-share",
            "ExecStartPost=", "NoExecPaths=/", "ExecPaths=/usr/bin /usr/lib",
            "PrivateTmp=yes", "ProtectHome=tmpfs", "ProtectSystem=strict",
            "ProtectControlGroups=yes", "ReadWritePaths=/run/little-mester-vm-workspace-share",
            "PartOf=little-mester-vm.service", "Before=little-mester-vm.service",
        ):
            self.assertIn(directive, template)
        for directive in ("systemctl", "daemon-reload", "O_EXCL", "O_NOFOLLOW"):
            self.assertIn(directive, repro)
        module = ast.parse(source)
        function = next(node for node in module.body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "run_production_dependency_reproduction")
        for name in ("writer", "remover"):
            assignment = next(node for node in ast.walk(function)
                              if isinstance(node, ast.Assign)
                              and any(isinstance(target, ast.Name) and target.id == name
                                      for target in node.targets))
            code = textwrap.dedent(ast.literal_eval(assignment.value.args[0]))
            compile(code, f"<{name}>", "exec")
        for directive in ("PartOf={vm_unit}", "Before={vm_unit}", "Requires={proxy_unit}",
                          "BindsTo={proxy_unit}", "After={proxy_unit}"):
            self.assertIn(directive, repro)
        self.assertIn("workspace_dropin", repro)
        self.assertIn("production_workspace_reproduction_config", repro)
        self.assertIn('"/usr/bin/systemctl", "cat"', repro)
        self.assertIn("merged_workspace_dropin_matches", repro)

    def test_namespace_classifier_requires_explicit_226_or_healthy_active_service(self):
        classify = unit.classify_namespace_reproduction
        self.assertEqual(classify({"Result": "resources"}, "", start_exit=1),
                         "INCONCLUSIVE")
        # systemctl show represents CLD_EXITED as the numeric enum value 1.
        self.assertEqual(classify({"ExecMainCode": "1", "ExecMainStatus": "226"}, "",
                                  start_exit=1), "INCONCLUSIVE")
        self.assertEqual(classify({}, "Main process exited, code=exited, status=226/NAMESPACE",
                                  start_exit=1), "REPRODUCED_226_NAMESPACE")
        self.assertEqual(classify({}, "Failed to set up mount namespacing", start_exit=1),
                         "INCONCLUSIVE")
        self.assertEqual(classify({"ExecMainCode": "1", "ExecMainStatus": "1",
                                   "ActiveState": "failed", "SubState": "failed"}, "",
                                  start_exit=1), "INCONCLUSIVE")
        self.assertEqual(classify({"ExecMainCode": "2", "ExecMainStatus": "2",
                                   "ActiveState": "failed", "SubState": "failed"}, "",
                                  start_exit=1), "INCONCLUSIVE")
        healthy_sleep = {"ActiveState": "active", "SubState": "running", "MainPID": "4242"}
        self.assertEqual(classify(healthy_sleep, "", start_exit=0,
                                  merged_dropin_matches=True),
                         "NOT_REPRODUCED")
        self.assertEqual(classify(healthy_sleep, "", start_exit=0), "INCONCLUSIVE")
        self.assertEqual(classify(healthy_sleep, "", start_exit=1,
                                  merged_dropin_matches=True), "INCONCLUSIVE")

    def test_reproduced_standalone_control_is_recorded_and_only_explicit_226_advances(self):
        require_control = unit.require_reproduced_control
        require_control("REPRODUCED_226_NAMESPACE")
        for classification in ("INCONCLUSIVE", "NOT_REPRODUCED",
                               "EXECUTION_FAILURE_INCONCLUSIVE"):
            with self.subTest(classification=classification):
                with self.assertRaisesRegex(RuntimeError, classification):
                    require_control(classification)

        source = SCRIPT.read_text()
        reproduction = source[source.index("def run_production_dependency_reproduction"):
                              source.index("def remove_disposable_workspace")]
        self.assertIn('standalone = run_case("dropin-standalone"', reproduction)
        self.assertIn('standalone["_raw_classification"] == "REPRODUCED_226_NAMESPACE"',
                      reproduction)
        self.assertIn('set(standalone["_configuration_mismatches"]) == {"BindPaths"}',
                      reproduction)
        # The outcome is returned only after stop/state/runtime/config/reset gates.
        self.assertIn("verify_stopped(start_unit)", reproduction)
        self.assertIn("verify_runtime_baseline()", reproduction)
        self.assertIn("require(effective_matches", reproduction)
        self.assertIn('original_error={type(error).__name__}: {error}', reproduction)
        self.assertIn("if sys.exc_info()[0] is None:\n                raise RuntimeError(", reproduction)
        self.assertIn("            raise\n        remover = textwrap.dedent", reproduction)
        self.assertNotIn('raise RuntimeError("diagnostic reproduction was ambiguous', reproduction)

    def test_reset_failed_diagnostics_capture_command_identity_and_before_after_state(self):
        calls = []
        emitted = []
        states = iter(("LoadState=loaded\nActiveState=failed\nMainPID=0\n",
                       "LoadState=loaded\nActiveState=inactive\nMainPID=0\n"))

        def command_runner(argv, **kwargs):
            calls.append((argv, kwargs))
            if "show" in argv:
                return subprocess.CompletedProcess(argv, 0, next(states), "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        result = unit.reset_failed_with_diagnostics(
            ("lm-test.service",), phase="standalone-control-reuse",
            command_runner=command_runner, emit=emitted.append)
        self.assertEqual(result["reset"]["returncode"], 0)
        self.assertEqual(result["before"]["lm-test.service"]["state"]["ActiveState"], "failed")
        self.assertEqual(result["after"]["lm-test.service"]["state"]["ActiveState"], "inactive")
        self.assertEqual(len(calls), 3)
        reset_argv = calls[1][0]
        self.assertEqual(reset_argv[:4], ["/usr/bin/sudo", "-n", "/usr/bin/systemctl",
                                          "reset-failed"])
        self.assertEqual(emitted[0], "systemd_reset_attempt=" + json.dumps(
            result, sort_keys=True, separators=(",", ":")))
        self.assertIn("euid", result["controller"])
        self.assertIn("egid", result["controller"])

    def test_reset_failed_nonzero_preserves_bounded_command_and_state_evidence(self):
        calls = []
        emitted = []
        state_outputs = iter(("LoadState=loaded\nActiveState=failed\nMainPID=0\n",
                              "LoadState=loaded\nActiveState=failed\nMainPID=0\n"))

        def command_runner(argv, **kwargs):
            calls.append(argv)
            if "show" in argv:
                return subprocess.CompletedProcess(argv, 0, next(state_outputs), "")
            return subprocess.CompletedProcess(argv, 1, "reset stdout", "authorization detail")

        with self.assertRaisesRegex(RuntimeError, "authorization detail"):
            unit.reset_failed_with_diagnostics(
                ("lm-test.service",), phase="standalone-control-reuse",
                command_runner=command_runner, emit=emitted.append)
        self.assertEqual(len(calls), 3)
        payload = json.loads(emitted[0].removeprefix("systemd_reset_attempt="))
        self.assertEqual(payload["reset"]["returncode"], 1)
        self.assertEqual(payload["reset"]["stderr"], "authorization detail")
        self.assertEqual(payload["reset"]["stdout"], "reset stdout")
        self.assertEqual(payload["before"]["lm-test.service"]["state"]["ActiveState"], "failed")
        self.assertEqual(payload["after"]["lm-test.service"]["state"]["ActiveState"], "failed")

    def test_reset_failed_command_output_is_bounded(self):
        states = iter(("LoadState=loaded\nActiveState=failed\nMainPID=0\n",
                       "LoadState=loaded\nActiveState=inactive\nMainPID=0\n"))

        def command_runner(argv, **kwargs):
            if "show" in argv:
                return subprocess.CompletedProcess(argv, 0, next(states), "")
            output = "x" * (unit.DIAGNOSTIC_OUTPUT_LIMIT + 32)
            return subprocess.CompletedProcess(argv, 0, output, output)

        result = unit.reset_failed_with_diagnostics(
            ("lm-test.service",), phase="final-cleanup", command_runner=command_runner,
            emit=lambda _line: None)
        self.assertEqual(len(result["reset"]["stdout"]), unit.DIAGNOSTIC_OUTPUT_LIMIT)
        self.assertEqual(len(result["reset"]["stderr"]), unit.DIAGNOSTIC_OUTPUT_LIMIT)

    def test_reset_failed_stopped_state_verification_remains_fail_closed(self):
        states = iter(("LoadState=loaded\nActiveState=failed\nMainPID=0\n",
                       "LoadState=loaded\nActiveState=activating\nMainPID=123\n"))

        def command_runner(argv, **kwargs):
            if "show" in argv:
                return subprocess.CompletedProcess(argv, 0, next(states), "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with self.assertRaisesRegex(RuntimeError, "did not reach stopped state"):
            unit.reset_failed_with_diagnostics(
                ("lm-test.service",), phase="final-cleanup",
                command_runner=command_runner, emit=lambda _line: None)

    def test_namespace_matrix_avoids_redundant_and_per_variant_intermediate_resets(self):
        source = SCRIPT.read_text()
        reproduction = source[source.index("def run_production_dependency_reproduction"):
                              source.index("def remove_disposable_workspace")]
        run_case = reproduction[reproduction.index("def run_case"):
                                reproduction.index("# The graph uses a separate")]
        self.assertNotIn("reset_failed_and_verify(", run_case)
        self.assertIn('standalone["_raw_classification"] == "REPRODUCED_226_NAMESPACE"',
                      reproduction)
        self.assertNotIn("reset-failed", run_case)
        self.assertIn("graph_workspace_unit =", reproduction)
        self.assertIn("reset_failed_with_diagnostics(names, phase=\"final-cleanup\")", reproduction)
        self.assertIn("systemd_reset_attempt=", SCRIPT.read_text())

    def test_journal_cursor_and_current_attempt_classification(self):
        cursor = "s=abc123;i=4;b=boot"
        self.assertEqual(unit.parse_journal_cursor(f"-- cursor: {cursor}\n", 0), cursor)
        for output, code in (("", 0), (f"-- cursor: {cursor}\n", 1),
                             ("-- cursor: malformed space\n", 0)):
            with self.assertRaisesRegex(RuntimeError, "journal cursor"):
                unit.parse_journal_cursor(output, code)
        stale_226 = {"ExecMainCode": "1", "ExecMainStatus": "226",
                     "ActiveState": "failed", "SubState": "failed"}
        self.assertEqual(unit.classify_namespace_reproduction(
            stale_226, "", start_exit=1, merged_dropin_matches=True), "INCONCLUSIVE")
        self.assertEqual(unit.classify_namespace_reproduction(
            stale_226, "new entry: status=226/NAMESPACE", start_exit=1,
            merged_dropin_matches=True), "REPRODUCED_226_NAMESPACE")
        execution_failure = unit.classify_namespace_reproduction(
            {"ActiveState": "failed", "SubState": "failed", "MainPID": "0"},
            "new entry: ExecPaths execution denied", start_exit=1,
            merged_dropin_matches=True)
        self.assertEqual(execution_failure, "INCONCLUSIVE")
        self.assertIn("EXECUTION_FAILURE", unit.classify_namespace_diagnostic(
            execution_failure, "new entry: ExecPaths execution denied"))

    def test_effective_configuration_comparison_allows_only_declared_delta(self):
        baseline = {"ProtectHome": "tmpfs", "PrivateTmp": "yes", "BindPaths": "/a:/b",
                    "FragmentPath": "/run/a.service", "DropInPaths": "/run/a.d/20.conf"}
        self.assertEqual(unit.compare_effective_configuration(
            baseline, dict(baseline, PrivateTmp="no"), allowed_setting="PrivateTmp"), {})
        self.assertTrue(unit.compare_effective_configuration(
            baseline, dict(baseline, ProtectHome="no"), allowed_setting="PrivateTmp"))
        self.assertEqual(unit.compare_effective_configuration(
            baseline, dict(baseline, FragmentPath="/run/b.service", DropInPaths="",
                           BindPaths="/a:/b"), allow_bind_fragment_placement=True), {})
        self.assertTrue(unit.compare_effective_configuration(
            baseline, dict(baseline, BindPaths="/wrong:/b"),
            allow_bind_fragment_placement=True))

    def test_bind_semantic_comparison_rejects_equal_but_duplicated_malformed_values(self):
        self.assertTrue(unit.bind_paths_semantically_match("/source:/target:rbind",
                                                           "/source", "/target"))
        self.assertFalse(unit.bind_paths_semantically_match(
            "/source:/target:/source:/target:rbind", "/source", "/target"))
        malformed = {"BindPaths": "/source:/target:/source:/target:rbind",
                     "ProtectHome": "tmpfs"}
        mismatches = unit.compare_effective_configuration(
            malformed, dict(malformed), expected_bind=("/source", "/target"))
        self.assertEqual(mismatches["BindPaths"]["baseline_semantic_match"], False)
        self.assertEqual(mismatches["BindPaths"]["candidate_semantic_match"], False)
        self.assertEqual(mismatches["BindPaths"]["baseline"], malformed["BindPaths"])
        self.assertEqual(mismatches["BindPaths"]["candidate"], malformed["BindPaths"])
        self.assertEqual(unit.compare_effective_configuration(
            malformed, dict(malformed, BindPaths="/source:/target:rbind"),
            allowed_setting="BindPaths", expected_bind=("/source", "/target")), {})
        self.assertTrue(unit.compare_effective_configuration(
            malformed, dict(malformed, BindPaths="/source:/target:/source:/target:rbind"),
            allowed_setting="BindPaths", expected_bind=("/source", "/target")))

    def test_hosted_bind_quote_matrix_runs_quoted_unquoted_then_reverse(self):
        source = SCRIPT.read_text()
        reproduction = source[source.index("def run_production_dependency_reproduction"):
                              source.index("def remove_disposable_workspace")]
        quoted = reproduction.index('run_case("dropin-standalone"')
        unquoted = reproduction.index('run_case("bind-unquoted"')
        reverse = reproduction.index('run_case("bind-quoted-reverse"')
        self.assertLess(quoted, unquoted)
        self.assertLess(unquoted, reverse)
        self.assertIn('unquoted["_raw_classification"] == "NOT_REPRODUCED"', reproduction)
        self.assertIn('and unquoted["_probe_valid"]', reproduction)
        self.assertIn("require(quoted_reverse[\"_raw_classification\"] == standalone[\"_raw_classification\"]",
                      reproduction)
        self.assertIn("WORKSPACE_MODULE._systemd_bind_path(workspace)", source)
        self.assertIn("legacy_quoted_dropin = f'[Service]", reproduction)
        self.assertIn('unquoted_dropin = workspace_dropin', reproduction)

    def test_dependency_exec_expectation_is_derived_from_generated_probe_command(self):
        template = (ROOT / "systemd/little-mester-vm-workspace.service").read_text()
        generated, _dropin = unit.production_workspace_reproduction_config(
            template, user="ci-owner", group="ci-group", vm_unit="sample-vm.service",
            workspace=pathlib.Path("/tmp/ci-workspace"),
            destination="/run/little-mester-vm-workspace-share")
        self.assertEqual(unit.unit_executable_from_text(generated, "ExecStart"),
                         "/usr/bin/python3")
        self.assertEqual(unit.unit_executable_from_text(generated, "ExecStartPost"),
                         "/usr/bin/true")
        reproduction = SCRIPT.read_text()[SCRIPT.read_text().index(
            "def run_production_dependency_reproduction"):]
        self.assertIn("unit_executable_from_text(workspace_text, key)", reproduction)
        self.assertNotIn('("ExecStart", "/usr/bin/sleep")', reproduction)

    def test_generated_namespace_probe_is_valid_python_and_quoted_as_one_systemd_argument(self):
        command = unit.workspace_bind_probe_exec(
            pathlib.Path("/var/tmp/source workspace"),
            "/run/little-mester-vm-workspace-share")
        match = re.fullmatch(
            r"/usr/bin/python3 -I -c 'import base64;exec\(base64\.b64decode\(\"([A-Za-z0-9+/=]+)\"\)\)'",
            command)
        self.assertIsNotNone(match)
        probe_code = base64.b64decode(match.group(1)).decode("utf-8")
        compile(probe_code, "<workspace-bind-probe>", "exec")
        self.assertIn("exact_destination_mountpoint", probe_code)
        self.assertIn("unrelated_home_hidden", probe_code)
        self.assertIn("service_identity_matches", probe_code)
        probe_gate = probe_code.split("valid = all(", 1)[1].split("if not valid:", 1)[0]
        self.assertNotIn("unrelated_home_hidden", probe_gate)
        self.assertIn("target_matches_prestart_source", SCRIPT.read_text())

    def test_namespace_probe_mount_identity_uses_prestart_host_source_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            source_path = pathlib.Path(temporary) / "source"
            source_path.mkdir()
            source = unit._host_path_evidence(source_path)
            source["lstat"]["device"] = 8
            source["lstat"]["inode"] = 42
        probe = {"identity": {}, "evidence": {
            "target": {"device": 8, "inode": 42},
            "exact_destination_mountpoint": True,
            "unrelated_home_hidden": True,
            "service_identity_matches": True,
        }}
        enriched, valid = unit.validate_workspace_bind_probe(probe, source)
        self.assertTrue(valid)
        self.assertTrue(enriched["host_comparison"]["target_matches_prestart_source"])
        changed = {**probe, "evidence": {**probe["evidence"],
                                          "target": {"device": 8, "inode": 43}}}
        _evidence, valid = unit.validate_workspace_bind_probe(changed, source)
        self.assertFalse(valid)

    def test_namespace_probe_accepts_expected_home_visibility_for_protect_home_variant(self):
        source = {"lstat": {"kind": "directory", "device": 8, "inode": 42}}
        probe = {"evidence": {"target": {"device": 8, "inode": 42},
                              "exact_destination_mountpoint": True,
                              "unrelated_home_hidden": False,
                              "service_identity_matches": True}}
        _evidence, valid = unit.validate_workspace_bind_probe(
            probe, source, expected_home_hidden=False)
        self.assertTrue(valid)
        _evidence, valid = unit.validate_workspace_bind_probe(probe, source)
        self.assertFalse(valid, "quoted controls must still require unrelated-home isolation")

    def test_namespace_probe_collection_waits_for_record_and_stops_at_known_failure_or_deadline(self):
        now = [0.0]
        journals = iter((
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "WORKSPACE_FILE_BIND_PROBE {}\n", ""),
        ))
        result, status = unit.wait_for_workspace_bind_probe(
            lambda: next(journals), lambda: {"ActiveState": "active", "MainPID": "10"},
            timeout=1.0, interval=0.1, clock=lambda: now[0],
            sleep=lambda duration: now.__setitem__(0, now[0] + duration))
        self.assertEqual(status, "PROBE_OBSERVED")
        self.assertIn("WORKSPACE_FILE_BIND_PROBE", result.stdout)

        result, status = unit.wait_for_workspace_bind_probe(
            lambda: subprocess.CompletedProcess([], 0, "226/NAMESPACE", ""),
            lambda: {"ActiveState": "failed", "MainPID": "0"})
        self.assertEqual(status, "NAMESPACE_FAILURE_BEFORE_PROBE")

        now[0] = 0.0
        result, status = unit.wait_for_workspace_bind_probe(
            lambda: subprocess.CompletedProcess([], 0, "", ""),
            lambda: {"ActiveState": "active", "MainPID": "10"},
            timeout=0.25, interval=0.1, clock=lambda: now[0],
            sleep=lambda duration: now.__setitem__(0, now[0] + duration))
        self.assertEqual(status, "PROBE_DEADLINE_EXPIRED")

        result, status = unit.wait_for_workspace_bind_probe(
            lambda: subprocess.CompletedProcess([], 0, "", ""),
            lambda: {"ActiveState": "failed", "MainPID": "0"})
        self.assertEqual(status, "UNIT_STOPPED_BEFORE_PROBE")
        result, status = unit.wait_for_workspace_bind_probe(
            lambda: subprocess.CompletedProcess([], 1, "", "journal unavailable"),
            lambda: self.fail("state must not be queried when journal fails"))
        self.assertEqual(status, "JOURNAL_UNAVAILABLE")

    def test_effective_comparison_treats_dependency_lists_as_sets_and_limits_private_tmp_delta(self):
        # v255 unit_add_exec_dependencies adds tmp.mount and tmpfiles edges;
        # RequiresMountsFor(/var/tmp) additionally walks each real mount prefix.
        topology = unit.private_tmp_mount_topology({"/", "/tmp", "/var", "/var/tmp"})
        self.assertEqual(topology, {
            "/tmp": ("-.mount", "tmp.mount"),
            "/var/tmp": ("-.mount", "var.mount", "var-tmp.mount"),
        })
        self.assertEqual(json.dumps(topology, sort_keys=True, separators=(",", ":")),
                         '{"/tmp":["-.mount","tmp.mount"],'
                         '"/var/tmp":["-.mount","var.mount","var-tmp.mount"]}')
        baseline = {
            "PrivateTmp": "yes", "RequiresMountsFor": "/var/tmp",
            "Requires": "network.target -.mount var.mount var-tmp.mount",
            "After": "network.target -.mount tmp.mount var.mount var-tmp.mount systemd-tmpfiles-setup.service",
            "Before": "shutdown.target", "Wants": "tmp.mount a.service",
            "BindsTo": "a.service", "PartOf": "parent.service",
            "Conflicts": "conflict.service", "Requisite": "required.service",
        }
        reordered = dict(baseline, Requires="network.target -.mount var.mount var-tmp.mount",
                         After="systemd-tmpfiles-setup.service tmp.mount network.target -.mount var.mount var-tmp.mount",
                         Before="shutdown.target another.target",
                         Wants="a.service tmp.mount", BindsTo="z.service a.service",
                         PartOf="parent-b.service parent-a.service",
                         Conflicts="other-conflict.service conflict.service",
                         Requisite="second-required.service required.service",
                         RequiresMountsFor="/var/tmp")
        baseline["Before"] = "another.target shutdown.target"
        baseline["BindsTo"] = "a.service z.service"
        baseline["PartOf"] = "parent-a.service parent-b.service"
        baseline["Conflicts"] = "conflict.service other-conflict.service"
        baseline["Requisite"] = "required.service second-required.service"
        self.assertEqual(unit.compare_effective_configuration(baseline, reordered), {})
        private_tmp_no = dict(baseline, PrivateTmp="no", RequiresMountsFor="",
                              Requires="network.target", Wants="a.service",
                              After="network.target")
        self.assertEqual(unit.compare_effective_configuration(
            baseline, private_tmp_no, allowed_setting="PrivateTmp",
            mount_topology=topology), {})

        # v257 uses WantsMountsFor; every actual ancestor for those paths can
        # produce Wants/After edges. There is no Requires exception in this profile.
        v257_baseline = {
            "PrivateTmp": "yes", "WantsMountsFor": "/tmp /var/tmp",
            "Requires": "network.target",
            "After": "network.target tmp.mount var.mount var-tmp.mount systemd-tmpfiles-setup.service",
            "Wants": "a.service tmp.mount var.mount var-tmp.mount",
        }
        v257_candidate = dict(v257_baseline, PrivateTmp="no", WantsMountsFor="",
                              After="network.target", Wants="a.service")
        self.assertEqual(unit.compare_effective_configuration(
            v257_baseline, v257_candidate, allowed_setting="PrivateTmp",
            mount_topology=topology), {})
        missing_var_tmp_dependency = dict(v257_baseline, WantsMountsFor="/tmp")
        rejected_var_tmp_removal = dict(missing_var_tmp_dependency, PrivateTmp="no",
                                        After="network.target", Wants="a.service",
                                        WantsMountsFor="")
        self.assertIn("After", unit.compare_effective_configuration(
            missing_var_tmp_dependency, rejected_var_tmp_removal,
            allowed_setting="PrivateTmp", mount_topology=topology))
        self.assertIn("Wants", unit.compare_effective_configuration(
            missing_var_tmp_dependency, rejected_var_tmp_removal,
            allowed_setting="PrivateTmp", mount_topology=topology))

        self.assertEqual(unit.private_tmp_mount_topology({"/", "/tmp"}), {
            "/tmp": ("-.mount", "tmp.mount"), "/var/tmp": ("-.mount",)})
        self.assertEqual(unit.private_tmp_mount_topology({"/", "/tmp", "/var", "/var/tmp"}), topology)
        unexpected = (
            dict(private_tmp_no, Requires="rogue.service"),
            dict(private_tmp_no, BindsTo=""),
            dict(private_tmp_no, PartOf=""),
            dict(private_tmp_no, Wants=""),
            dict(private_tmp_no, After="network.target rogue.service"),
            dict(private_tmp_no, RequiresMountsFor="/tmp"),
            dict(private_tmp_no, RequiresMountsFor="/other"),
            dict(private_tmp_no, WantsMountsFor="/other"),
            # var-tmp.mount is not an unconditional v255 Wants edge.
            dict(private_tmp_no, Wants="a.service var-tmp.mount"),
        )
        for candidate in unexpected:
            with self.subTest(candidate=candidate):
                mismatches = unit.compare_effective_configuration(
                    baseline, candidate, allowed_setting="PrivateTmp")
                self.assertTrue(mismatches)
                self.assertEqual(json.loads(json.dumps(mismatches)), mismatches)

        unrelated_baseline = dict(baseline, RequiresMountsFor="/var/tmp /srv/unrelated")
        unrelated_mount_removal = dict(unrelated_baseline, PrivateTmp="no",
                                       RequiresMountsFor="/var/tmp",
                                       After="network.target", Wants="a.service",
                                       Requires="network.target")
        self.assertIn("RequiresMountsFor", unit.compare_effective_configuration(
            unrelated_baseline, unrelated_mount_removal,
            allowed_setting="PrivateTmp", mount_topology=topology))

        unknown_topology = unit.compare_effective_configuration(
            v257_baseline, v257_candidate, allowed_setting="PrivateTmp",
            mount_topology=None)
        self.assertTrue({"After", "Wants"} & set(unknown_topology),
                        "unknown runtime topology must remain unsupported/inconclusive")

        unknown_profile = {"PrivateTmp": "yes", "After": "tmp.mount rogue.service",
                           "Wants": "tmp.mount", "RequiresMountsFor": "/other"}
        unknown_profile_candidate = dict(unknown_profile, PrivateTmp="no", After="",
                                         Wants="", RequiresMountsFor="")
        self.assertTrue(unit.compare_effective_configuration(
            unknown_profile, unknown_profile_candidate, allowed_setting="PrivateTmp",
            mount_topology=topology), "unknown systemd profile must fail closed")

        wrong_v257_requires = dict(v257_baseline, Requires="network.target var.mount")
        self.assertIn("Requires", unit.compare_effective_configuration(
            v257_baseline, wrong_v257_requires, allowed_setting="PrivateTmp",
            mount_topology=topology))

        for property_name in ("Requires", "BindsTo", "PartOf", "Requisite", "Conflicts"):
            with self.subTest(forbidden_mount_unit_property=property_name):
                before = {"PrivateTmp": "yes", property_name: "tmp.mount app.service"}
                after = {"PrivateTmp": "no", property_name: "app.service"}
                self.assertIn(property_name, unit.compare_effective_configuration(
                    before, after, allowed_setting="PrivateTmp"))

        no_private_tmp_transition = dict(private_tmp_no, PrivateTmp="yes")
        self.assertIn("Wants", unit.compare_effective_configuration(
            baseline, no_private_tmp_transition, allowed_setting="PrivateTmp"))

    def test_runner_shaped_private_tmp_profiles_allow_unrelated_runtime_requirements(self):
        runtime_paths = (
            "/run/little-mester-vm-workspace "+
            "/run/little-mester-vm-workspace-share"
        )
        topology = unit.private_tmp_mount_topology({"/", "/tmp", "/var", "/var/tmp"})
        # systemctl show includes both path properties even when one is empty.
        # Keep the other dependency properties representative of the full runner
        # query, including unrelated runtime mount requirements.
        v255 = {
            "PrivateTmp": "yes", "RequiresMountsFor": f"/var/tmp {runtime_paths}",
            "WantsMountsFor": "", "Requires": "network.target -.mount var.mount var-tmp.mount",
            "Wants": "tmp.mount app.service",
            "After": "network.target -.mount tmp.mount var.mount var-tmp.mount systemd-tmpfiles-setup.service",
            "Before": "shutdown.target", "BindsTo": "app.service",
            "PartOf": "parent.service", "Conflicts": "conflict.service",
            "Requisite": "required.service",
        }
        v255_without_private_tmp = dict(
            v255, PrivateTmp="no", RequiresMountsFor=runtime_paths,
            Requires="network.target", Wants="app.service", After="network.target")
        self.assertEqual(unit.private_tmp_systemd_profile(v255), "v255")
        self.assertEqual(unit.compare_effective_configuration(
            v255, v255_without_private_tmp, allowed_setting="PrivateTmp",
            mount_topology=topology), {})

        v257 = {
            "PrivateTmp": "yes", "RequiresMountsFor": runtime_paths,
            "WantsMountsFor": "/tmp /var/tmp", "Requires": "network.target",
            "Wants": "app.service tmp.mount var.mount var-tmp.mount",
            "After": "network.target tmp.mount var.mount var-tmp.mount systemd-tmpfiles-setup.service",
            "Before": "shutdown.target", "BindsTo": "app.service",
            "PartOf": "parent.service", "Conflicts": "conflict.service",
            "Requisite": "required.service",
        }
        v257_without_private_tmp = dict(
            v257, PrivateTmp="no", WantsMountsFor="", Wants="app.service",
            After="network.target")
        self.assertEqual(unit.private_tmp_systemd_profile(v257), "v257")
        self.assertEqual(unit.compare_effective_configuration(
            v257, v257_without_private_tmp, allowed_setting="PrivateTmp",
            mount_topology=topology), {})

    def test_private_tmp_profile_detection_rejects_mixed_or_ambiguous_paths(self):
        ambiguous = (
            # Partial WantsMountsFor sets are neither supported upstream shape.
            {"RequiresMountsFor": "/var/tmp", "WantsMountsFor": "/tmp"},
            {"RequiresMountsFor": "", "WantsMountsFor": "/var/tmp"},
            # A private-tmp path in both properties mixes v255 and v257 semantics.
            {"RequiresMountsFor": "/var/tmp /run/little-mester-vm-workspace",
             "WantsMountsFor": "/tmp /var/tmp"},
        )
        for profile in ambiguous:
            with self.subTest(profile=profile):
                self.assertIsNone(unit.private_tmp_systemd_profile(profile))
                baseline = dict(profile, PrivateTmp="yes", After="tmp.mount rogue.service",
                                Wants="tmp.mount rogue.service")
                candidate = dict(baseline, PrivateTmp="no", After="", Wants="")
                self.assertTrue(unit.compare_effective_configuration(
                    baseline, candidate, allowed_setting="PrivateTmp",
                    mount_topology={"/tmp": ("-.mount", "tmp.mount"),
                                    "/var/tmp": ("-.mount", "var.mount", "var-tmp.mount")}))

    def test_effective_configuration_mismatches_are_json_serializable(self):
        mismatch = unit.compare_effective_configuration(
            {"After": "a.service b.service"}, {"After": "a.service c.service"})
        self.assertEqual(json.loads(json.dumps(mismatch)), {
            "After": {"baseline": ["a.service", "b.service"],
                      "candidate": ["a.service", "c.service"]},
        })

    def test_exec_configuration_normalization_discards_only_dynamic_metadata(self):
        before = ("{ path=/usr/bin/sleep ; argv[]=/usr/bin/sleep 90 ; ignore_errors=no ; "
                  "start_time=[Mon 2026-11-02 10:00:00 UTC] ; stop_time=n/a ; pid=123 ; "
                  "code=(null) ; status=0 }")
        after = ("{ path=/usr/bin/sleep ; argv[]=/usr/bin/sleep 90 ; ignore_errors=no ; "
                 "start_time=[Sat 2026-11-21 13:17:42 UTC] ; stop_time=n/a ; pid=987 ; "
                 "code=exited ; status=0 }")
        self.assertEqual(unit.normalize_systemd_exec(before), unit.normalize_systemd_exec(after))
        changed = after.replace("argv[]=/usr/bin/sleep 90", "argv[]=/usr/bin/sleep 91")
        self.assertNotEqual(unit.normalize_systemd_exec(before), unit.normalize_systemd_exec(changed))
        changed = after.replace("ignore_errors=no", "ignore_errors=yes")
        self.assertNotEqual(unit.normalize_systemd_exec(before), unit.normalize_systemd_exec(changed))

    def test_preserved_runtime_lifecycle_captures_healthy_identity_before_stop_and_restarts(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "runtime"
            path.mkdir(mode=0o700)
            events = []

            def stop():
                events.append("stop")

            def restart():
                events.append("start")
                return True

            result = unit.run_preserved_runtime_lifecycle(
                (path,), first_start_healthy=True, stop=stop, restart=restart)
            self.assertEqual(events, ["stop", "start", "stop"])
            self.assertEqual(result, "EXERCISED")
            events.clear()
            self.assertEqual(unit.run_preserved_runtime_lifecycle(
                (path,), first_start_healthy=False, stop=stop, restart=restart),
                "NOT_EXERCISED")
            self.assertEqual(events, [])

    def test_preserved_runtime_lifecycle_rejects_identity_replacement_during_stop_or_restart(self):
        for replace_during in ("stop", "restart"):
            with self.subTest(replace_during=replace_during), tempfile.TemporaryDirectory() as temporary:
                path = pathlib.Path(temporary) / "runtime"
                path.mkdir(mode=0o700)

                def replace():
                    replacement = pathlib.Path(temporary) / "replacement"
                    replacement.mkdir(mode=0o700)
                    path.rmdir()
                    replacement.rename(path)

                def stop():
                    if replace_during == "stop":
                        replace()

                def restart():
                    if replace_during == "restart":
                        replace()
                    return True

                with self.assertRaisesRegex(RuntimeError, "identity"):
                    unit.run_preserved_runtime_lifecycle(
                        (path,), first_start_healthy=True, stop=stop, restart=restart)

    def test_preserved_restart_uses_a_fresh_distinct_journal_cursor(self):
        cursors = iter(("first-cursor", "restart-cursor"))
        started = []

        def attempt(previous=None):
            return unit.run_fresh_cursor_attempt(
                lambda: next(cursors), lambda: started.append("start") or True,
                previous_cursor=previous)

        first_cursor, first_result = attempt()
        restart_cursor, restart_result = attempt(first_cursor)
        self.assertTrue(first_result and restart_result)
        self.assertNotEqual(first_cursor, restart_cursor)
        self.assertEqual(started, ["start", "start"])
        with self.assertRaisesRegex(RuntimeError, "fresh journal cursor"):
            unit.run_fresh_cursor_attempt(lambda: first_cursor, lambda: started.append("bad"),
                                          previous_cursor=first_cursor)
        self.assertEqual(started, ["start", "start"])

    def test_parent_graph_captures_paths_before_cursor_and_start(self):
        events = []
        paths, cursor, result = unit.run_attempt_with_path_snapshot(
            lambda: events.append("paths") or {"runtime": "before"},
            lambda: events.append("cursor") or "fresh-cursor",
            lambda: events.append("start") or "started")
        self.assertEqual(paths, {"runtime": "before"})
        self.assertEqual((cursor, result), ("fresh-cursor", "started"))
        self.assertEqual(events, ["paths", "cursor", "start"])

        module = ast.parse(SCRIPT.read_text())
        reproduction = next(node for node in module.body
                            if isinstance(node, ast.FunctionDef)
                            and node.name == "run_production_dependency_reproduction")
        graph_attempts = [node for node in ast.walk(reproduction)
                          if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                          and node.func.id == "run_attempt_with_path_snapshot"]
        self.assertEqual(len(graph_attempts), 1)
        graph_snapshot_assignment = next(node for node in ast.walk(reproduction)
                                         if isinstance(node, ast.Assign)
                                         and any(isinstance(target, ast.Tuple)
                                                 and any(isinstance(item, ast.Name)
                                                         and item.id == "graph_prestart"
                                                         for item in target.elts)
                                                 for target in node.targets))
        self.assertIs(graph_snapshot_assignment.value, graph_attempts[0])

    def test_runner_wires_real_attempt_and_final_cleanup_helpers(self):
        module = ast.parse(SCRIPT.read_text())
        reproduction = next(node for node in module.body
                            if isinstance(node, ast.FunctionDef)
                            and node.name == "run_production_dependency_reproduction")
        run_case = next(node for node in ast.walk(reproduction)
                        if isinstance(node, ast.FunctionDef) and node.name == "run_case")
        attempt_calls = [node for node in ast.walk(run_case)
                         if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                         and node.func.id == "run_fresh_cursor_attempt"]
        self.assertEqual(len(attempt_calls), 1)
        self.assertTrue(any(isinstance(node, ast.Name) and node.id == "preserve_cursor"
                            for node in ast.walk(attempt_calls[0])))
        finalizers = [node for node in ast.walk(reproduction)
                      if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                      and node.func.id == "finalize_disposable_units"]
        self.assertEqual(len(finalizers), 1)

        cursors = iter(("first", "restart"))
        started = []
        first, _ = unit.run_fresh_cursor_attempt(
            lambda: next(cursors), lambda: started.append("first"), previous_cursor=None)
        restart, _ = unit.run_fresh_cursor_attempt(
            lambda: next(cursors), lambda: started.append("restart"), previous_cursor=first)
        self.assertNotEqual(first, restart)
        self.assertEqual(started, ["first", "restart"])
        with self.assertRaisesRegex(RuntimeError, "identity"):
            with tempfile.TemporaryDirectory() as temporary:
                path = pathlib.Path(temporary) / "runtime"
                path.mkdir()

                def replace_identity():
                    replacement = pathlib.Path(temporary) / "replacement"
                    replacement.mkdir()
                    path.rmdir()
                    replacement.rename(path)

                unit.run_preserved_runtime_lifecycle(
                    (path,), first_start_healthy=True, stop=replace_identity,
                    restart=lambda: True)

    def test_final_cleanup_stops_cleans_runtime_resets_then_removes_evidence_and_reloads(self):
        events = []
        units = ("one.service", "two.service")
        unit.finalize_disposable_units(
            units=units,
            stop_units=lambda names: events.append(("stop", names)),
            verify_stopped=lambda name: events.append(("verify", name)),
            reset_failed=lambda names: events.append(("reset", names)),
            runtime_cleanup=lambda: events.append(("runtime",)),
            evidence_cleanup=lambda: events.append(("evidence",)),
            daemon_reload=lambda: events.append(("reload",)))
        self.assertEqual(events, [
            ("stop", units), ("verify", units[0]), ("verify", units[1]),
            ("runtime",), ("reset", units),
            ("verify", units[0]), ("verify", units[1]),
            ("evidence",), ("reload",),
        ])

    def test_final_reset_failure_keeps_unit_evidence_but_cleans_stopped_runtime_paths(self):
        events = []

        def reset_failed(names):
            events.append(("reset", names))
            raise RuntimeError("reset diagnostics failure")

        with self.assertRaisesRegex(RuntimeError, "reset diagnostics failure"):
            unit.finalize_disposable_units(
                units=("failed.service",),
                stop_units=lambda names: events.append(("stop", names)),
                verify_stopped=lambda name: events.append(("verify", name)),
                reset_failed=reset_failed,
                runtime_cleanup=lambda: events.append(("runtime-cleanup",)),
                evidence_cleanup=lambda: events.append(("evidence-cleanup",)),
                daemon_reload=lambda: events.append(("daemon-reload",)))
        self.assertEqual(events, [
            ("stop", ("failed.service",)),
            ("verify", "failed.service"),
            ("runtime-cleanup",),
            ("reset", ("failed.service",)),
        ])

    def test_final_unit_name_helper_covers_every_file_and_runner_wires_complete_tuple(self):
        variants = {"private-tmp": "repro-private-tmp.service",
                    "protect-home": "repro-protect-home.service"}
        quote_units = ("repro-unquoted.service", "repro-quoted-reverse.service")
        expected = ("repro-workspace.service", "repro-graph-workspace.service",
                    "repro-fragment.service",
                    "repro-proxy.service", "repro-metrics.service", "repro-vm.service",
                    *quote_units, *variants.values())
        names = unit.disposable_final_unit_names(
            "repro-workspace.service", "repro-fragment.service", "repro-graph-workspace.service",
            "repro-proxy.service", "repro-metrics.service", "repro-vm.service",
            quote_units, variants)
        self.assertEqual(names, expected)
        self.assertEqual(len(names), len(set(names)))
        observed = []
        unit.finalize_disposable_units(
            units=names,
            stop_units=lambda selected: observed.append(("stop", selected)),
            verify_stopped=lambda selected: observed.append(("verify", selected)),
            reset_failed=lambda selected: observed.append(("reset", selected)),
            runtime_cleanup=lambda: observed.append(("runtime",)),
            evidence_cleanup=lambda: observed.append(("evidence",)),
            daemon_reload=lambda: observed.append(("reload",)))
        self.assertEqual(observed[0], ("stop", expected))
        self.assertEqual(observed[1:1 + len(expected)],
                         [("verify", name) for name in expected])
        runtime_index = 1 + len(expected)
        self.assertEqual(observed[runtime_index], ("runtime",))
        reset_index = runtime_index + 1
        self.assertEqual(observed[reset_index], ("reset", expected))
        self.assertEqual(observed[reset_index + 1:reset_index + 1 + len(expected)],
                         [("verify", name) for name in expected])

        module = ast.parse(SCRIPT.read_text())
        reproduction = next(node for node in module.body
                            if isinstance(node, ast.FunctionDef)
                            and node.name == "run_production_dependency_reproduction")
        assignment = next(node for node in ast.walk(reproduction)
                          if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name)
                                  and target.id == "final_unit_names" for target in node.targets))
        self.assertEqual(assignment.value.func.id, "disposable_final_unit_names")
        finalizer = next(node for node in ast.walk(reproduction)
                         if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                         and node.func.id == "finalize_disposable_units")
        unit_arg = next(keyword.value for keyword in finalizer.keywords if keyword.arg == "units")
        self.assertIsInstance(unit_arg, ast.Name)
        self.assertEqual(unit_arg.id, "final_unit_names")
        # Every entry in the writer's files payload is represented by the helper's
        # production call inputs; variants are added as a complete values tuple.
        self.assertIn("*tuple(variant_units.values())", ast.get_source_segment(
            SCRIPT.read_text(), next(node for node in ast.walk(module)
                                     if isinstance(node, ast.FunctionDef)
                                     and node.name == "disposable_final_unit_names")))

    def test_final_cleanup_runtime_failure_skips_evidence_removal_callback(self):
        events = []

        def failing_runtime_cleanup():
            events.append("runtime")
            raise RuntimeError("runtime cleanup failed")

        with self.assertRaisesRegex(RuntimeError, "runtime cleanup failed"):
            unit.finalize_disposable_units(
                units=("variant.service",), stop_units=lambda names: events.append("stop"),
                verify_stopped=lambda name: events.append(("verify", name)),
                reset_failed=lambda names: events.append("reset"),
                runtime_cleanup=failing_runtime_cleanup,
                evidence_cleanup=lambda: events.append("evidence"),
                daemon_reload=lambda: events.append("reload"))
        self.assertIn("runtime", events)
        self.assertNotIn("evidence", events)
        self.assertNotIn("reload", events)

    def test_final_cleanup_failures_leave_unit_and_dropin_evidence_untouched(self):
        for failing_stage in ("preflight", "destructive"):
            with self.subTest(failing_stage=failing_stage), tempfile.TemporaryDirectory() as temporary:
                root = pathlib.Path(temporary)
                artifact = root / "unit.service"
                artifact.write_text("unit evidence")
                dropin = root / "unit.service.d"
                dropin.mkdir()
                (dropin / "20-test.conf").write_text("drop-in evidence")
                events = []

                def runtime_cleanup():
                    events.append("runtime-preflight")
                    if failing_stage == "preflight":
                        raise RuntimeError("runtime preflight failed")
                    events.append("runtime-destructive")
                    raise RuntimeError("runtime destructive cleanup failed")

                def evidence_cleanup():
                    events.append("evidence")
                    artifact.unlink()

                with self.assertRaisesRegex(RuntimeError, "runtime"):
                    unit.finalize_disposable_units(
                        units=("unit.service",), stop_units=lambda names: events.append("stop"),
                        verify_stopped=lambda name: events.append("verify"),
                        reset_failed=lambda names: events.append("reset"),
                        runtime_cleanup=runtime_cleanup, evidence_cleanup=evidence_cleanup,
                        daemon_reload=lambda: events.append("reload"))
                self.assertNotIn("evidence", events)
                self.assertNotIn("reload", events)
                self.assertEqual(artifact.read_text(), "unit evidence")
                self.assertEqual((dropin / "20-test.conf").read_text(), "drop-in evidence")

    def test_runtime_cleanup_preflight_failure_preserves_unit_and_dropin_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            artifact = root / "unit.service"
            artifact.write_text("unit evidence")
            dropin = root / "unit.service.d"
            dropin.mkdir()
            (dropin / "20-test.conf").write_text("drop-in evidence")
            runtime = root / "runtime"
            runtime.mkdir()
            expected = {str(runtime): unit.runtime_directory_identity(os.lstat(runtime))}
            (runtime / "unexpected").touch()
            evidence_removed = []

            with self.assertRaisesRegex(RuntimeError, "non-empty"):
                unit.cleanup_runtime_before_evidence(
                    lambda: unit.remove_runtime_directories(expected),
                    lambda: evidence_removed.append(artifact.unlink()))
            self.assertEqual(evidence_removed, [])
            self.assertEqual(artifact.read_text(), "unit evidence")
            self.assertEqual((dropin / "20-test.conf").read_text(), "drop-in evidence")

            (runtime / "unexpected").unlink()
            replacement = root / "replacement"
            replacement.mkdir()
            runtime.rmdir()
            replacement.rename(runtime)
            with self.assertRaisesRegex(RuntimeError, "identity"):
                unit.cleanup_runtime_before_evidence(
                    lambda: unit.remove_runtime_directories(expected),
                    lambda: evidence_removed.append(artifact.unlink()))
            self.assertEqual(evidence_removed, [])
            self.assertEqual(artifact.read_text(), "unit evidence")
            self.assertEqual((dropin / "20-test.conf").read_text(), "drop-in evidence")
            self.assertTrue(runtime.is_dir())

    def test_retained_runtime_cleanup_requires_exact_inode_and_preflights_all(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            first, second = root / "first", root / "second"
            first.mkdir(mode=0o700)
            second.mkdir(mode=0o700)
            expected = {str(path): unit.runtime_directory_identity(os.lstat(path))
                        for path in (first, second)}
            (first / "payload").touch()
            with self.assertRaisesRegex(RuntimeError, "non-empty"):
                unit.remove_runtime_directories(expected)
            self.assertTrue(second.is_dir(), "cleanup must preflight all paths before deletion")
            (first / "payload").unlink()
            replacement = root / "replacement"
            replacement.mkdir(mode=0o700)
            first.rmdir()
            replacement.rename(first)
            with self.assertRaisesRegex(RuntimeError, "identity"):
                unit.remove_runtime_directories({str(first): expected[str(first)]})
            self.assertTrue(first.is_dir(), "replacement inode must not be removed")
            first.rmdir()
            unit.remove_runtime_directories({str(second): expected[str(second)]})
            self.assertFalse(second.exists())

    def test_embedded_privileged_writer_rolls_back_owned_files_and_directories(self):
        source = SCRIPT.read_text()
        module = ast.parse(source)
        function = next(node for node in module.body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "run_production_dependency_reproduction")
        assignment = next(node for node in ast.walk(function)
                          if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == "writer"
                                  for target in node.targets))
        writer = textwrap.dedent(ast.literal_eval(assignment.value.args[0]))
        self.assertIn("import json, os, pathlib, stat, sys", writer)
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            writer = writer.replace("root = pathlib.Path('/run/systemd/system')",
                                    f"root = pathlib.Path({str(root)!r})")
            payload = {
                "units": {
                    "lm-vm-workspace-repro-test.service": "unit content",
                },
                "dropins": {
                    "lm-vm-workspace-repro-test.service.d/20-workspace.conf": "drop-in",
                },
            }
            failure = OSError("injected drop-in create failure")
            original_open = os.open

            def fail_dropin_open(path, *args, **kwargs):
                if str(path).endswith("20-workspace.conf"):
                    raise failure
                return original_open(path, *args, **kwargs)

            with mock.patch.object(unit.sys, "stdin", io.StringIO(json.dumps(payload))), \
                    mock.patch.object(unit.os, "open", side_effect=fail_dropin_open):
                with self.assertRaises(OSError) as raised:
                    exec(compile(writer, "<privileged-writer>", "exec"), {})
            self.assertIs(raised.exception, failure)
            self.assertEqual(list(root.iterdir()), [])

    def test_production_shaped_dependency_reproduction_uses_unit_dropin(self):
        template = (ROOT / "systemd/little-mester-vm-workspace.service").read_text()
        unit_text, dropin_text = unit.production_workspace_reproduction_config(
            template, user="ci-owner", group="ci-group", vm_unit="sample-vm.service",
            workspace=pathlib.Path("/tmp/ci-workspace"),
            destination="/run/little-mester-vm-workspace-share")
        self.assertIn("ExecStart=/usr/bin/python3 -I -c '", unit_text)
        self.assertNotIn("BindPaths=", unit_text)
        self.assertEqual(dropin_text, unit.WORKSPACE_MODULE._systemd_bind_path(
            pathlib.Path("/tmp/ci-workspace")))
        self.assertIn("BindPaths=", dropin_text)
        merged = ("# /run/systemd/system/sample-vm-workspace.service\n" + unit_text
                  + "# /run/systemd/system/sample-vm-workspace.service.d/20-workspace.conf\n"
                  + dropin_text)
        self.assertTrue(unit.merged_workspace_dropin_matches(merged, 0, dropin_text))
        self.assertFalse(unit.merged_workspace_dropin_matches(merged, 1, dropin_text))
        self.assertFalse(unit.merged_workspace_dropin_matches(unit_text, 0, dropin_text))

    def test_hosted_workspace_matrix_compares_standalone_graph_and_bind_placement(self):
        template = (ROOT / "systemd/little-mester-vm-workspace.service").read_text()
        base, dropin = unit.production_workspace_reproduction_config(
            template, user="ci-owner", group="ci-group", vm_unit="sample-vm.service",
            workspace=pathlib.Path("/tmp/ci-workspace"),
            destination="/run/little-mester-vm-workspace-share")
        directive = 'BindPaths=/tmp/ci-workspace:/run/little-mester-vm-workspace-share\n'
        fragment = unit.production_workspace_fragment_config(base, directive)
        self.assertEqual(dropin, unit.WORKSPACE_MODULE._systemd_bind_path(
            pathlib.Path("/tmp/ci-workspace")))
        self.assertEqual(directive, dropin.replace("[Service]\n", "", 1))
        self.assertEqual(fragment.replace(directive, "", 1), base)
        self.assertNotIn("BindPaths=", base)
        self.assertIn(directive, fragment)

        source = SCRIPT.read_text()
        reproduction = source[source.index("def run_production_dependency_reproduction"):
                              source.index("def remove_disposable_workspace")]
        self.assertIn('"dropin-standalone"', reproduction)
        self.assertIn('"dropin-parent-graph"', reproduction)
        self.assertIn('"fragment-standalone"', reproduction)
        self.assertIn('"start_unit": vm_unit', reproduction)
        self.assertIn('"host_before_start": prestart', reproduction)
        self.assertIn('"host_before_start": graph_prestart', reproduction)
        self.assertIn('_host_path_evidence(pathlib.Path(destination))', reproduction)
        self.assertIn('"/usr/bin/systemctl", "show", "--no-pager"', reproduction)
        self.assertIn('"/usr/bin/systemctl", "cat", "--no-pager"', reproduction)
        self.assertIn("journal_cursor_command()", reproduction)
        self.assertIn("journal_after_cursor_command(cursor, effective_unit)", reproduction)
        self.assertIn('"classification": classify_namespace_diagnostic(classification, diagnostic)',
                      reproduction)
        self.assertIn('"classification": graph_classification', reproduction)
        self.assertIn("journal_cursor_command()", reproduction)
        self.assertIn("journal_after_cursor_command(graph_cursor, graph_workspace_unit)", reproduction)

    def test_workspace_sandbox_variants_change_only_the_named_dropin_setting(self):
        template = (ROOT / "systemd/little-mester-vm-workspace.service").read_text()
        base, bind_dropin = unit.production_workspace_reproduction_config(
            template, user="ci-owner", group="ci-group", vm_unit="sample-vm.service",
            workspace=pathlib.Path("/tmp/ci-workspace"),
            destination="/run/little-mester-vm-workspace-share")
        variants = unit.production_workspace_diagnostic_variants()
        expected = {
            "no-exec-paths": "NoExecPaths", "exec-paths": "ExecPaths",
            "protect-control-groups": "ProtectControlGroups", "protect-home": "ProtectHome",
            "protect-system": "ProtectSystem", "read-write-paths": "ReadWritePaths",
            "private-tmp": "PrivateTmp", "runtime-directory-preserve": "RuntimeDirectoryPreserve",
        }
        self.assertEqual(set(variants), set(expected))
        self.assertEqual(bind_dropin, '[Service]\nBindPaths=/tmp/ci-workspace:/run/little-mester-vm-workspace-share\n')
        self.assertNotIn("BindPaths=", base)
        for label, fragment in variants.items():
            directives = [line.split("=", 1)[0] for line in fragment.splitlines()
                          if line and not line.startswith("[")]
            self.assertEqual(directives, [expected[label]], label)
            self.assertNotIn("BindPaths=", fragment, label)
            # The variant is a separate drop-in layered onto the exact same unit and
            # production-shaped BindPaths drop-in, not an inline/template mutation.
            self.assertEqual(base, unit.production_workspace_reproduction_config(
                template, user="ci-owner", group="ci-group", vm_unit="sample-vm.service",
                workspace=pathlib.Path("/tmp/ci-workspace"),
                destination="/run/little-mester-vm-workspace-share")[0])
            self.assertEqual(bind_dropin, unit.production_workspace_reproduction_config(
                template, user="ci-owner", group="ci-group", vm_unit="sample-vm.service",
                workspace=pathlib.Path("/tmp/ci-workspace"),
                destination="/run/little-mester-vm-workspace-share")[1])

        source = SCRIPT.read_text()
        reproduction = source[source.index("def run_production_dependency_reproduction"):
                              source.index("def remove_disposable_workspace")]
        self.assertIn('variant_units = {label: f"{prefix}-{label}.service"', reproduction)
        self.assertIn('f"{name}.d/30-{label}.conf"', reproduction)
        self.assertIn('[workspace_dropin, variant_dropins[label]]', reproduction)
        self.assertIn('"properties": state', reproduction)
        self.assertIn('"classification": classify_namespace_diagnostic(classification, diagnostic)',
                      reproduction)
        self.assertIn('"host_before_start": prestart', reproduction)
        self.assertIn('"host_after_start":', reproduction)

    def test_runtime_directory_lifetime_variant_records_and_cleans_preserved_paths(self):
        variants = unit.production_workspace_diagnostic_variants()
        self.assertEqual(variants["runtime-directory-preserve"],
                         "RuntimeDirectoryPreserve=yes\n")
        source = SCRIPT.read_text()
        reproduction = source[source.index("def run_production_dependency_reproduction"):
                              source.index("def remove_disposable_workspace")]
        self.assertIn('"runtime_directories_after_stop": after_stop', reproduction)
        self.assertIn('"RuntimeDirectoryPreserve=yes intentionally retains service runtime paths"',
                      reproduction)
        generated = unit.runtime_cleanup_script(pathlib.Path("/tmp/root"), {})
        self.assertIn("preserving changed or foreign runtime directory identity mismatch", generated)
        self.assertIn("preserving non-empty runtime directory", generated)
        self.assertIn('"lifetime_probe": "NOT_EXERCISED"', reproduction)
        self.assertIn('classification == "NOT_REPRODUCED"', reproduction)
        self.assertNotIn("reset_failed_and_verify((start_unit,))", reproduction)

    def test_preserved_runtime_fixture_requires_expected_service_owner_group_and_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "runtime"
            path.mkdir(mode=0o750)
            info = os.lstat(path)
            identity = unit.runtime_directory_identity(
                info, expected_uid=os.getuid(), expected_gid=os.getgid(), expected_mode=0o750)
            self.assertEqual(identity["uid"], os.getuid())
            self.assertEqual(identity["gid"], os.getgid())
            self.assertEqual(identity["mode"], 0o750)
            for expected in ({"expected_uid": os.getuid() + 1},
                             {"expected_gid": os.getgid() + 1},
                             {"expected_mode": 0o700}):
                with self.subTest(expected=expected), self.assertRaisesRegex(RuntimeError,
                                                                            "expected service"):
                    unit.runtime_directory_identity(info, **expected)

    def test_journal_cursor_is_global_then_attempt_slice_is_unit_filtered(self):
        self.assertEqual(unit.journal_cursor_command(), [
            "/usr/bin/sudo", "-n", "/usr/bin/journalctl", "--no-pager",
            "--show-cursor", "-n", "0",
        ])
        cursor = "s=global-cursor"
        self.assertEqual(unit.journal_after_cursor_command(cursor, "fresh.service"), [
            "/usr/bin/sudo", "-n", "/usr/bin/journalctl", "--no-pager",
            f"--after-cursor={cursor}", "-n", "40", "-u", "fresh.service",
        ])
        self.assertNotIn("-u", unit.journal_cursor_command())

    def test_generated_privileged_runtime_cleanup_preflights_before_deleting(self):
        def identity(path):
            info = os.lstat(path)
            return {"device": info.st_dev, "inode": info.st_ino, "uid": info.st_uid,
                    "gid": info.st_gid, "mode": unit.stat.S_IMODE(info.st_mode)}

        def execute(root, expected):
            script = unit.runtime_cleanup_script(root, expected)
            return unit.subprocess.run([unit.sys.executable, "-I", "-c", script],
                                       capture_output=True, text=True, check=False)

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            valid = root / "a-valid"
            replacement = root / "z-replacement"
            valid.mkdir()
            replacement.mkdir()
            expected = {"a-valid": identity(valid), "z-replacement": identity(replacement)}
            preflight = unit.runtime_directory_preflight_script(root, expected)
            checked = unit.subprocess.run([unit.sys.executable, "-I", "-c", preflight],
                                          capture_output=True, text=True, check=False)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertTrue(valid.is_dir(), "preflight must not remove runtime paths")
            replacement.rename(root / "z-original-inode")
            replacement.mkdir()
            result = execute(root, expected)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(valid.is_dir(), "valid path was deleted before identity preflight completed")
            self.assertTrue(replacement.is_dir())

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            valid = root / "a-valid"
            nonempty = root / "z-nonempty"
            valid.mkdir()
            nonempty.mkdir()
            (nonempty / "unexpected").write_text("preserve")
            expected = {"a-valid": identity(valid), "z-nonempty": identity(nonempty)}
            result = execute(root, expected)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(valid.is_dir(), "valid path was deleted before emptiness preflight completed")
            self.assertTrue((nonempty / "unexpected").is_file())

    def test_bind_reproduction_separates_controller_source_from_systemd_runtime_lifetime(self):
        source = SCRIPT.read_text()
        section = source[source.index("def run_workspace_bind_reproductions"):
                         source.index("def remove_disposable_workspace")]
        case = source[source.index("def _bind_repro_case"):
                      source.index("def run_workspace_bind_reproductions")]
        self.assertIn('temp_parent = pathlib.Path("/var/tmp")', section)
        self.assertIn("tempfile.mkdtemp(", section)
        self.assertIn('outside_created = os.lstat(outside)', section)
        self.assertIn('_require_runner_directory(outside, outside_identity)', section)
        self.assertIn('"--property=RuntimeDirectoryPreserve=no"', case)
        self.assertNotIn('"--collect"', case)
        self.assertIn('"stop-transient-systemd-unit"', case)
        self.assertIn('"verify-systemd-runtime-directory-cleanup"', case)
        self.assertIn("primary_error: BaseException | None = None", case)
        self.assertIn("if cleanup_errors and primary_error is None", case)
        self.assertIn('"before-case-{kind}-{protect}"', section)
        self.assertIn('"after-case-{kind}-{protect}"', section)
        self.assertIn("if primary_error is None:", section)
        self.assertIn('"remove-exact-harness-owned-control-source"', section)

    def test_script_uses_production_fixed_operations_and_bounded_stop_cleanup(self):
        source = SCRIPT.read_text()
        shared = (ROOT / "lib/vm/system-acceptance.py").read_text()
        for required in (
            '"--install", "--sha", sha',
            '"--prefix", str(CI_INSTALL_PREFIX)',
            'installed_root = CI_INSTALL_PARENT / sha',
            'root = CI_INSTALL_PARENT / sha',
            '"tools/vm-system-install"',
            'f"{root}/lab-vm system {operation}"',
            '"up", 16 * 60 + 45',
            '"down", 45',
            'invoke_lab(root, "down", 60, check=False)',
            'same_disk=true',
            '"/usr/bin/systemctl", "kill", "--signal=SIGSTOP"',
            '"--kill-whom=main", UNIT',
            'stopped.get("Result") == "exit-code"',
            'system_qemu_pids()',
            'fixed production disk identity changed during forced fallback',
            '"reset-failed", UNIT',
            'production_unit_forced_stop_recovery=PASS same_disk=true',
            'poller.poll(1000)',
            'TimeoutStopSec=25s',
            'KillMode=control-group',
            'SendSIGKILL=yes',
            'production_unit_forced_stop=PASS',
            'production_unit_cleanup=PASS',
            'installed_asset_bytes(dest)',
            '"--system-vm", "down"',
            'invoke_verified_cleanup(sha)',
        ):
            self.assertIn(required, source)
        for required in ('b"restrict=on"', 'b"hostfwd=tcp:127.0.0.1:22222-:22"',
                         '1.1.1.1', 'child.expect(b"Ask anything"',
                         'child.sendcontrol("c")', 'same_disk_persistence:PASS',
                         '_proxy_policy_checks()', '_is_allowed_public_destination'):
            self.assertIn(required, shared)
        for forbidden in ("systemctl enable", "systemctl disable", "shutil.rmtree", "rm -rf"):
            self.assertNotIn(forbidden, source)
        repro = source[source.index("def run_production_dependency_reproduction"):
                       source.index("def remove_disposable_workspace")]
        self.assertIn("actual != text.encode()", repro)
        self.assertIn("os.unlink(path)", repro)
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        production = workflow.split("  production-systemd-vm-acceptance:", 1)[1].split(
            "  # ── 6.", 1)[0]
        host_dependencies = (ROOT / "tests/ci-kvm-host-dependencies.sh").read_text()
        shared_dependencies = (ROOT / "lib/vm/host-dependencies.py").read_text()
        self.assertIn("host-dependencies.py\" ci --list-packages apt", host_dependencies)
        self.assertIn('("apt", "ncurses-bin")', shared_dependencies)

    def test_fixed_disk_identity_uses_privileged_stat_for_only_fixed_regular_disk(self):
        command = ["/usr/bin/sudo", "-n", "/usr/bin/stat", "--format=%F:%d:%i", "--",
                   "/var/lib/little-mester-vm/disk.qcow2"]
        with mock.patch.object(unit.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout=b"regular file:2049:123456\n")) as run:
            self.assertEqual(unit.fixed_disk_identity(), (2049, 123456))
        run.assert_called_once_with(command, stdin=unit.subprocess.DEVNULL,
                                    stdout=unit.subprocess.PIPE,
                                    stderr=unit.subprocess.DEVNULL, timeout=5, check=False)

        for code, output in ((1, b""), (0, b"symbolic link:2049:123456\n"),
                             (0, b"regular file:not-a-device:123456\n")):
            with mock.patch.object(unit.subprocess, "run", return_value=SimpleNamespace(
                    returncode=code, stdout=output)), \
                    self.assertRaisesRegex(RuntimeError, "fixed production disk"):
                unit.fixed_disk_identity()

    def test_system_assets_are_installed_before_pinned_file_verification(self):
        source = SCRIPT.read_text()
        tree = ast.parse(source)
        main = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "main")
        calls = []
        for node in ast.walk(main):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "verify_installed_files":
                calls.append((node.lineno, "verify"))
            elif isinstance(node.func, ast.Name) and node.func.id == "run":
                args = ast.get_source_segment(source, node)
                if args and "tools/vm-system-install" in args:
                    calls.append((node.lineno, "system-install"))
        self.assertEqual([name for _, name in sorted(calls)], ["system-install", "verify"])

    def test_startup_diagnostic_sanitizer_redacts_multiline_credentials_and_keys(self):
        diagnostic = "\n".join((
            "Authorization: Bearer bearer-secret-value",
            'password="two words containing a secret"',
            "token='first line\nsecond secret line'",
            ("-----BEGIN OPENSSH PRIVATE KEY-----\nprivate-key-bytes\n"
             "-----END OPENSSH PRIVATE KEY-----"),
            "sk-" + "x" * 32,
        ))
        sanitized = unit.sanitize_diagnostic_output(diagnostic)
        for secret in ("bearer-secret-value", "two words containing a secret",
                       "second secret line", "private-key-bytes", "sk-" + "x" * 32):
            self.assertNotIn(secret, sanitized)
        self.assertIn("[REDACTED PRIVATE KEY BLOCK]", sanitized)
        multibyte = unit.sanitize_diagnostic_output("界" * 20_000)
        self.assertLessEqual(len(multibyte.encode("utf-8")), unit.DIAGNOSTIC_OUTPUT_LIMIT)

    def test_startup_diagnostic_command_caps_captured_output(self):
        code, output, truncated = unit.bounded_diagnostic_command([
            "/usr/bin/python3", "-c", "import sys; sys.stdout.write('x' * 400000)",
        ])
        self.assertEqual(len(output.encode("utf-8")), unit.DIAGNOSTIC_CAPTURE_LIMIT)
        self.assertTrue(truncated)
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
