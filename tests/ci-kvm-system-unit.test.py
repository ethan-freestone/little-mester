#!/usr/bin/env python3
"""Static and isolated fail-closed checks for hosted production-unit KVM CI."""

from __future__ import annotations

import ast
import importlib.util
import os
import pathlib
import shlex
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
        self.assertIn('BindPaths="{workspace}:/run/little-mester-vm-workspace-share', config_body)
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
        repro = source[source.index("def run_production_dependency_reproduction"):
                       source.index("def remove_disposable_workspace")]
        self.assertIn('systemd/little-mester-vm-workspace.service', repro)
        self.assertIn('ExecStart=/usr/bin/sleep 90', repro)
        self.assertIn('ExecStartPost=/usr/bin/true', repro)
        for directive in (
            "Type=exec", "RuntimeDirectory=little-mester-vm-workspace little-mester-vm-workspace-share",
            "ExecStartPost=", "NoExecPaths=/", "ExecPaths=/usr/bin /usr/lib",
            "PrivateTmp=yes", "ProtectHome=tmpfs", "ProtectSystem=strict",
            "ProtectControlGroups=yes", "ReadWritePaths=/run/little-mester-vm-workspace-share",
            "PartOf=little-mester-vm.service", "Before=little-mester-vm.service",
        ):
            self.assertIn(directive, template)
        for directive in ("systemctl", "daemon-reload", "journalctl",
                          "REPRODUCED_226_NAMESPACE", "NOT_REPRODUCED", "O_EXCL", "O_NOFOLLOW"):
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
        for directive in ('BindPaths="{workspace}:{destination}"', "PartOf={vm_unit}",
                          "Before={vm_unit}", "Requires={proxy_unit}",
                          "BindsTo={proxy_unit}", "After={proxy_unit}"):
            self.assertIn(directive, repro)

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
