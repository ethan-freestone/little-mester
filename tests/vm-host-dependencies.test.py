#!/usr/bin/env python3
"""Mock-only safety tests for shared host dependency preparation."""

from __future__ import annotations

import importlib.util
import importlib.machinery
import pathlib
import subprocess
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("host_dependencies_test", ROOT / "lib/vm/host-dependencies.py")
dependencies = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dependencies
SPEC.loader.exec_module(dependencies)
OWNER_SPEC = importlib.util.spec_from_loader(
    "owner_acceptance_cli_test", importlib.machinery.SourceFileLoader(
        "owner_acceptance_cli_test", str(ROOT / "tools/vm-owner-acceptance")))
owner_cli = importlib.util.module_from_spec(OWNER_SPEC)
sys.modules[OWNER_SPEC.name] = owner_cli
OWNER_SPEC.loader.exec_module(owner_cli)


class HostDependencyTests(unittest.TestCase):
    def test_kvm_capability_does_not_depend_on_sg_invokers_group_list(self):
        requirement = dependencies.Requirement("KVM", capability="kvm")
        device = type("Device", (), {
            "st_mode": dependencies.stat.S_IFCHR | 0o660,
            "st_gid": 4242,
        })()
        with mock.patch.object(dependencies.grp, "getgrnam", return_value=type("Group", (), {"gr_gid": 4242})()), \
             mock.patch.object(dependencies.os, "stat", return_value=device), \
             mock.patch.object(dependencies.os, "getgroups", return_value=[4343]), \
             mock.patch.object(dependencies.os, "getegid", return_value=4343), \
             mock.patch.object(dependencies.os, "access", return_value=False) as owner_access:
            okay, detail = dependencies._capability(requirement)
        self.assertTrue(okay)
        self.assertIn("group read/write", detail)
        owner_access.assert_not_called()

    def test_kvm_capability_accepts_world_rw_device_without_owner_kvm_membership(self):
        requirement = dependencies.Requirement("KVM", capability="kvm")
        device = type("Device", (), {
            "st_mode": dependencies.stat.S_IFCHR | 0o666,
            "st_gid": 4242,
        })()
        with mock.patch.object(dependencies.grp, "getgrnam", return_value=type("Group", (), {"gr_gid": 4242})()), \
             mock.patch.object(dependencies.os, "stat", return_value=device), \
             mock.patch.object(dependencies.os, "getgroups", return_value=[]), \
             mock.patch.object(dependencies.os, "getegid", return_value=4343), \
             mock.patch.object(dependencies.os, "access", return_value=False):
            okay, _detail = dependencies._capability(requirement)
        self.assertTrue(okay)

    def test_kvm_capability_still_requires_service_compatible_device_contract(self):
        requirement = dependencies.Requirement("KVM", capability="kvm")
        kvm_group = type("Group", (), {"gr_gid": 4242})()
        for mode, gid in ((dependencies.stat.S_IFREG | 0o660, 4242),
                          (dependencies.stat.S_IFCHR | 0o660, 4343),
                          (dependencies.stat.S_IFCHR | 0o640, 4242)):
            device = type("Device", (), {"st_mode": mode, "st_gid": gid})()
            with self.subTest(mode=oct(mode), gid=gid), \
                 mock.patch.object(dependencies.grp, "getgrnam", return_value=kvm_group), \
                 mock.patch.object(dependencies.os, "stat", return_value=device):
                okay, _detail = dependencies._capability(requirement)
            self.assertFalse(okay)

    def test_all_present_has_no_package_manager_or_prompt(self):
        with mock.patch.object(dependencies, "missing", return_value=([], [])), \
             mock.patch.object(dependencies, "detect_backend", return_value="apt") as backend, \
             mock.patch("builtins.input") as prompt:
            dependencies.ensure("production", interactive=True)
        backend.assert_called_once()
        prompt.assert_not_called()

    def test_all_present_still_rejects_an_unsupported_distribution(self):
        with mock.patch.object(dependencies, "missing", return_value=([], [])), \
             mock.patch.object(dependencies, "detect_backend", side_effect=dependencies.DependencyError("unsupported distro")), \
             self.assertRaisesRegex(dependencies.DependencyError, "unsupported distro"):
            dependencies.ensure("production", interactive=True)

    def test_read_only_check_only_validates_supported_backend(self):
        with mock.patch.object(dependencies, "_os_release", return_value={"ID": "fedora"}), \
             mock.patch.object(dependencies, "_executable", side_effect=lambda p: p == dependencies.APT), \
             mock.patch.object(dependencies, "missing", return_value=([], [])):
            self.assertEqual(dependencies.main(["production", "--check-only"]), 3)

    def test_missing_installable_apt_packages_preview_and_execute_then_recheck(self):
        absent = [dependencies.Requirement("curl", "/usr/bin/curl", packages=(("apt", "curl"),))]
        with mock.patch.object(dependencies, "missing", side_effect=[(absent, []), ([], [])]), \
             mock.patch.object(dependencies, "detect_backend", return_value="apt"), \
             mock.patch.object(dependencies, "_requested_packages", return_value=["curl"]), \
             mock.patch.object(dependencies, "_plan", side_effect=[("curl=1.0",), ("curl=1.0",)]), \
             mock.patch.object(dependencies, "_run", return_value=subprocess.CompletedProcess([], 0, "")) as run, \
             mock.patch("builtins.input", return_value="yes"):
            dependencies.ensure("production", interactive=True)
        self.assertEqual(run.call_args.args[0], [
            dependencies.SUDO, dependencies.APT, "install", "--no-install-recommends",
            "--no-upgrade", "--no-remove", "--", "curl=1.0",
        ])
        self.assertFalse(run.call_args.kwargs["capture"])

    def test_pacman_install_keeps_native_confirmation_and_never_syncs_or_upgrades(self):
        absent = [dependencies.Requirement("QEMU", "/usr/bin/qemu-system-x86_64",
                                           packages=(("pacman", "qemu-desktop"),))]
        with mock.patch.object(dependencies, "missing", side_effect=[(absent, []), ([], [])]), \
             mock.patch.object(dependencies, "detect_backend", return_value="pacman"), \
             mock.patch.object(dependencies, "_requested_packages", return_value=["qemu-desktop"]), \
             mock.patch.object(dependencies, "_plan", side_effect=[("qemu-desktop=9.2.0",),
                                                                    ("qemu-desktop=9.2.0",)]), \
             mock.patch.object(dependencies, "_run", return_value=subprocess.CompletedProcess([], 0, "")) as run, \
             mock.patch("builtins.input", return_value="yes"):
            dependencies.ensure("production", interactive=True)
        command = run.call_args.args[0]
        self.assertEqual(command, [dependencies.SUDO, dependencies.PACMAN, "-S", "--needed", "--", "qemu-desktop=9.2.0"])
        self.assertNotIn("--noconfirm", command)
        self.assertNotIn("-Sy", command)
        self.assertNotIn("-Syu", command)

    def test_missing_noninstallable_host_capability_reports_before_stopping(self):
        absent = [dependencies.Requirement("KVM", capability="kvm")]
        with mock.patch.object(dependencies, "missing", return_value=(absent, ["/dev/kvm"])), \
             mock.patch.object(dependencies, "detect_backend") as backend, \
             mock.patch("builtins.input") as prompt, \
             self.assertRaisesRegex(dependencies.DependencyError, "no missing prerequisites can be installed"):
            dependencies.ensure("production", interactive=True)
        backend.assert_called_once()
        prompt.assert_not_called()

    def test_installable_packages_are_still_offered_beside_noninstallable_capabilities(self):
        missing = [dependencies.Requirement("KVM", capability="kvm"),
                   dependencies.Requirement("curl", "/usr/bin/curl", packages=(("apt", "curl"),))]
        with mock.patch.object(dependencies, "missing", side_effect=[(missing, ["/dev/kvm"]),
                                                                      (missing, ["/dev/kvm"])]), \
             mock.patch.object(dependencies, "detect_backend", return_value="apt"), \
             mock.patch.object(dependencies, "_requested_packages", return_value=["curl"]) as requested, \
             mock.patch.object(dependencies, "_plan", return_value=("curl=1.0",)), \
             mock.patch.object(dependencies, "_run", return_value=subprocess.CompletedProcess([], 0, "")), \
             mock.patch("builtins.input", return_value="yes"), \
             self.assertRaisesRegex(dependencies.DependencyError, "post-install prerequisite recheck"):
            dependencies.ensure("production", interactive=True)
        self.assertEqual(requested.call_args.args[1], [missing[1]])

    def test_unsupported_package_manager_is_clear_and_never_guesses(self):
        with mock.patch.object(dependencies, "_os_release", return_value={"ID": "fedora"}), \
             mock.patch.object(dependencies, "_executable", side_effect=lambda p: p == dependencies.APT), \
             self.assertRaisesRegex(dependencies.DependencyError, "not in the validated package mapping"):
            dependencies.detect_backend()

    def test_only_validated_distribution_ids_select_backends(self):
        for distro, path, expected in (("ubuntu", dependencies.APT, "apt"),
                                       ("debian", dependencies.APT, "apt"),
                                       ("cachyos", dependencies.PACMAN, "pacman"),
                                       ("arch", dependencies.PACMAN, "pacman")):
            with self.subTest(distro=distro), \
                 mock.patch.object(dependencies, "_os_release", return_value={"ID": distro}), \
                 mock.patch.object(dependencies, "_executable", side_effect=lambda p, selected=path: p == selected):
                self.assertEqual(dependencies.detect_backend(), expected)
        with mock.patch.object(dependencies, "_os_release", return_value={"ID": "linuxmint", "ID_LIKE": "ubuntu"}), \
             mock.patch.object(dependencies, "_executable", side_effect=lambda p: p == dependencies.APT), \
             self.assertRaisesRegex(dependencies.DependencyError, "not in the validated package mapping"):
            dependencies.detect_backend()

    def test_debian_uses_the_explicit_apt_package_map(self):
        with mock.patch.object(dependencies, "_os_release", return_value={"ID": "debian"}), \
             mock.patch.object(dependencies, "_executable", side_effect=lambda p: p == dependencies.APT):
            self.assertEqual(dependencies.detect_backend(), "apt")
        package_list = dependencies.package_names("apt", "production")
        self.assertIn("qemu-system-x86", package_list)
        self.assertIn("virtiofsd", package_list)
        self.assertEqual(package_list, dependencies.package_names("apt", "production"))

    def test_decline_stops_before_privileged_transaction(self):
        absent = [dependencies.Requirement("curl", "/usr/bin/curl", packages=(("apt", "curl"),))]
        with mock.patch.object(dependencies, "missing", return_value=(absent, [])), \
             mock.patch.object(dependencies, "detect_backend", return_value="apt"), \
             mock.patch.object(dependencies, "_requested_packages", return_value=["curl"]), \
             mock.patch.object(dependencies, "_plan", return_value=("curl=1.0",)), \
             mock.patch.object(dependencies, "_run") as run, \
             mock.patch("builtins.input", return_value="n"), \
             self.assertRaisesRegex(dependencies.DependencyError, "declined"):
            dependencies.ensure("production", interactive=True)
        run.assert_not_called()

    def test_changed_transaction_after_approval_is_rejected(self):
        absent = [dependencies.Requirement("curl", "/usr/bin/curl", packages=(("apt", "curl"),))]
        with mock.patch.object(dependencies, "missing", return_value=(absent, [])), \
             mock.patch.object(dependencies, "detect_backend", return_value="apt"), \
             mock.patch.object(dependencies, "_requested_packages", return_value=["curl"]), \
             mock.patch.object(dependencies, "_plan", side_effect=[("curl=1.0",), ("curl=1.0", "extra=2")]), \
             mock.patch.object(dependencies, "_run") as run, \
             mock.patch("builtins.input", return_value="y"), \
             self.assertRaisesRegex(dependencies.DependencyError, "changed after approval"):
            dependencies.ensure("production", interactive=True)
        run.assert_not_called()

    def test_failed_or_interrupted_install_is_reported_without_retry(self):
        absent = [dependencies.Requirement("curl", "/usr/bin/curl", packages=(("apt", "curl"),))]
        for status in (100, 130):
            with self.subTest(status=status), \
                 mock.patch.object(dependencies, "missing", return_value=(absent, [])), \
                 mock.patch.object(dependencies, "detect_backend", return_value="apt"), \
                 mock.patch.object(dependencies, "_requested_packages", return_value=["curl"]), \
                 mock.patch.object(dependencies, "_plan", return_value=("curl=1.0",)), \
                 mock.patch.object(dependencies, "_run", return_value=subprocess.CompletedProcess([], status, "")), \
                 mock.patch("builtins.input", return_value="y"), \
                 self.assertRaisesRegex(dependencies.DependencyError, "failed or was interrupted"):
                dependencies.ensure("production", interactive=True)

    def test_post_install_recheck_must_pass(self):
        absent = [dependencies.Requirement("curl", "/usr/bin/curl", packages=(("apt", "curl"),))]
        with mock.patch.object(dependencies, "missing", side_effect=[(absent, []), (absent, [])]), \
             mock.patch.object(dependencies, "detect_backend", return_value="apt"), \
             mock.patch.object(dependencies, "_requested_packages", return_value=["curl"]), \
             mock.patch.object(dependencies, "_plan", return_value=("curl=1.0",)), \
             mock.patch.object(dependencies, "_run", return_value=subprocess.CompletedProcess([], 0, "")), \
             mock.patch("builtins.input", return_value="y"), \
             self.assertRaisesRegex(dependencies.DependencyError, "post-install prerequisite recheck"):
            dependencies.ensure("production", interactive=True)

    def test_noninteractive_never_prompts_or_installs(self):
        absent = [dependencies.Requirement("curl", "/usr/bin/curl", packages=(("apt", "curl"),))]
        with mock.patch.object(dependencies, "missing", return_value=(absent, [])), \
             mock.patch.object(dependencies, "detect_backend", return_value="apt"), \
             mock.patch.object(dependencies, "_requested_packages", return_value=["curl"]), \
             mock.patch.object(dependencies, "_plan", return_value=("curl",)), \
             mock.patch.object(dependencies, "_run") as run, \
             mock.patch("builtins.input") as prompt, \
             self.assertRaisesRegex(dependencies.DependencyError, "noninteractive"):
            dependencies.ensure("production", interactive=False)
        run.assert_not_called()
        prompt.assert_not_called()

    def test_apt_preview_rejects_upgrade_and_removal(self):
        for output in ("Inst curl [1] (2 repo)", "Remv unrelated 1"):
            with self.subTest(output=output), \
                 mock.patch.object(dependencies, "_run", return_value=subprocess.CompletedProcess([], 0, output)), \
                 self.assertRaisesRegex(dependencies.DependencyError, "removal or upgrade"):
                dependencies._apt_plan(["curl"])

    def test_apt_preview_reports_exact_install_versions(self):
        with mock.patch.object(dependencies, "_run", return_value=subprocess.CompletedProcess(
                [], 0, "Inst curl (8.1.2-1 Ubuntu:24.04/noble-updates)\n")):
            self.assertEqual(dependencies._apt_plan(["curl"]), ("curl=8.1.2-1",))

    def test_pacman_mapping_and_preview_reject_conflict_or_installed_target(self):
        pexpect_requirement = next(item for item in dependencies.REQUIREMENTS["acceptance"] if item.module == "pexpect")
        self.assertEqual(dict(pexpect_requirement.packages)["pacman"], "python-pexpect")
        output = "qemu-desktop 9.2.0\nqemu-img 9.2.0"
        with mock.patch.object(dependencies, "_run", side_effect=[
                subprocess.CompletedProcess([], 0, ""), subprocess.CompletedProcess([], 0, output)]), \
             mock.patch.object(dependencies, "_package_installed", side_effect=lambda _b, p: p == "qemu-img"), \
             self.assertRaisesRegex(dependencies.DependencyError, "unsupported partial upgrade"):
            dependencies._pacman_plan(["qemu-desktop"])
        for unsafe_output in ("qemu-desktop 9.2.0\nconflict: replacing package\n",
                              "qemu-desktop 9.2.0\nremoving unrelated package\n"):
            with self.subTest(unsafe_output=unsafe_output), \
                 mock.patch.object(dependencies, "_run", side_effect=[
                     subprocess.CompletedProcess([], 0, ""),
                     subprocess.CompletedProcess([], 0, unsafe_output)]), \
                 self.assertRaisesRegex(dependencies.DependencyError, "could not prove a safe"):
                dependencies._pacman_plan(["qemu-desktop"])

    def test_pacman_accepts_empty_no_upgrade_result_with_exit_zero(self):
        with mock.patch.object(dependencies, "_run", side_effect=[
                subprocess.CompletedProcess([], 0, ""),
                subprocess.CompletedProcess([], 0, "qemu-desktop 9.2.0\n")]) as run, \
             mock.patch.object(dependencies, "_package_installed", return_value=False):
            self.assertEqual(dependencies._pacman_plan(["qemu-desktop"]), ("qemu-desktop=9.2.0",))
        self.assertEqual(run.call_args_list[0].args[0], [dependencies.PACMAN, "-Qu"])

    def test_pacman_accepts_empty_no_upgrade_result_with_exit_one(self):
        with mock.patch.object(dependencies, "_run", side_effect=[
                subprocess.CompletedProcess([], 1, ""),
                subprocess.CompletedProcess([], 0, "qemu-desktop 9.2.0\n")]) as run, \
             mock.patch.object(dependencies, "_package_installed", return_value=False):
            self.assertEqual(dependencies._pacman_plan(["qemu-desktop"]), ("qemu-desktop=9.2.0",))
        self.assertEqual(run.call_args_list[0].args[0], [dependencies.PACMAN, "-Qu"])

    def test_pacman_refuses_pending_full_system_upgrades_without_syncing(self):
        for status in (0, 1):
            with self.subTest(status=status), \
                 mock.patch.object(dependencies, "_run", return_value=subprocess.CompletedProcess(
                     [], status, "linux 6.12.1-1 -> 6.12.2-1\n")) as run, \
                 self.assertRaisesRegex(dependencies.DependencyError, "partial upgrade"):
                dependencies._pacman_plan(["qemu-desktop"])
            self.assertEqual(run.call_args.args[0], [dependencies.PACMAN, "-Qu"])

    def test_pacman_refuses_query_errors_and_unknown_results(self):
        for status, output in ((1, "error: failed to synchronize all databases\n"),
                               (2, ""), (127, "")):
            with self.subTest(status=status, output=output), \
                 mock.patch.object(dependencies, "_run", return_value=subprocess.CompletedProcess(
                     [], status, output)) as run, \
                 self.assertRaisesRegex(dependencies.DependencyError, "cannot verify package state") as raised:
                dependencies._pacman_plan(["qemu-desktop"])
            self.assertEqual(run.call_args.args[0], [dependencies.PACMAN, "-Qu"])
            if output:
                self.assertIn(output.strip(), str(raised.exception))

    def test_pacman_query_fails_closed_on_captured_stderr_diagnostic(self):
        with mock.patch.object(dependencies.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 1, "pacman diagnostic\n")) as run, \
             self.assertRaisesRegex(dependencies.DependencyError, "pacman diagnostic"):
            dependencies._pacman_plan(["qemu-desktop"])
        self.assertIs(run.call_args.kwargs["stderr"], subprocess.STDOUT)

    def test_interrupted_package_process_is_reported_and_not_retried(self):
        absent = [dependencies.Requirement("curl", "/usr/bin/curl", packages=(("apt", "curl"),))]
        with mock.patch.object(dependencies, "missing", return_value=(absent, [])), \
             mock.patch.object(dependencies, "detect_backend", return_value="apt"), \
             mock.patch.object(dependencies, "_requested_packages", return_value=["curl"]), \
             mock.patch.object(dependencies, "_plan", return_value=("curl=1.0",)), \
             mock.patch.object(dependencies, "_run", side_effect=KeyboardInterrupt), \
             mock.patch("builtins.input", return_value="y"), \
             self.assertRaisesRegex(dependencies.DependencyError, "was interrupted"):
            dependencies.ensure("production", interactive=True)

    def test_both_backend_maps_and_ci_specific_sg_boundary(self):
        for scope in ("production", "acceptance", "system-install"):
            for requirement in dependencies.REQUIREMENTS[scope]:
                if requirement.executable or requirement.module:
                    self.assertTrue({"apt", "pacman"}.issubset(dict(requirement.packages)), requirement.name)
        source = (ROOT / "lib/vm/system-acceptance.py").read_text()
        self.assertNotIn("/usr/bin/sg", source)
        ci = (ROOT / "tests/ci-kvm-system-unit.py").read_text()
        self.assertIn('"/usr/bin/sg", "lm-vm-control", "-c"', ci)

    def test_owner_verify_stays_read_only_and_accept_keeps_independent_lifecycle_gate(self):
        source = (ROOT / "tools/vm-owner-acceptance").read_text()
        verify_branch = source.split('if args.system_vm not in ("verify", "accept"):', 1)[1].split("current_gate = \"candidate\"", 1)[0]
        self.assertNotIn("ensure(\"acceptance\")", verify_branch)
        self.assertIn("if args.system_vm == \"accept\":", source)
        self.assertIn("--approve-system-vm-lifecycle", source)
        self.assertIn("--prepare-dependencies", source)

    def test_vm_dispatcher_is_thin_and_preserves_docker_commands(self):
        source = (ROOT / "lab").read_text()
        self.assertIn('vm)', source)
        self.assertIn('dependency_scope=production', source)
        self.assertIn('dependency_scope=acceptance', source)
        self.assertIn('exec "$LAB_DIR/tools/vm-owner-acceptance" --system-vm', source)
        self.assertIn('setup)', source)
        self.assertIn('dc --profile setup', source)

    def test_standalone_dependency_preparation_needs_no_candidate_sha(self):
        args = owner_cli.parser().parse_args(["--prepare-dependencies", "production"])
        self.assertIsNone(args.sha)

    def test_production_vm_implementation_is_not_duplicated_or_modified(self):
        system_vm = (ROOT / "lib/vm/system-vm.py").read_text()
        self.assertIn('"up": ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "start", "little-mester-vm.service"]', system_vm)
        install = (ROOT / "tools/vm-system-install").read_text()
        self.assertIn('"lib/vm/host-dependencies.py": ("/usr/lib/little-mester-vm/host-dependencies.py", 0o644)', install)


if __name__ == "__main__":
    unittest.main()
