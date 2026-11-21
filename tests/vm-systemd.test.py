#!/usr/bin/env python3
"""Non-host policy, CLI and QMP protocol tests for the systemd VM slice."""

from __future__ import annotations

import importlib.util
import json
import importlib.machinery
import inspect
import contextlib
import datetime
import io
import os
import pathlib
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import textwrap
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(name: str, path: pathlib.Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vm = load("system_vm", ROOT / "lib/vm/system-vm.py")
owner = load("vm_owner_acceptance", ROOT / "tools/vm-owner-acceptance")
system_installer = load("vm_system_install", ROOT / "tools/vm-system-install")
contract = load("parent_session_contract", ROOT / "lib/vm/parent-session-contract.py")
bootstrap = load("guest_bootstrap", ROOT / "lib/vm/guest-bootstrap.py")
acceptance = load("system_acceptance", ROOT / "lib/vm/system-acceptance.py")


class SystemdPolicyTests(unittest.TestCase):
    def test_workspace_daemon_acceptance_checks_kernel_nnp_not_unit_property(self):
        active = {"ActiveState": "active", "MainPID": "123"}
        with mock.patch.object(acceptance, "service_state", return_value=active), \
                mock.patch.object(pathlib.Path, "read_text", return_value="Name:\tvirtiofsd\nNoNewPrivs:\t0\n"):
            acceptance._verify_workspace_daemon_privileges()
        with mock.patch.object(acceptance, "service_state", return_value=active), \
                mock.patch.object(pathlib.Path, "read_text", return_value="NoNewPrivs:\t1\n"), \
                self.assertRaisesRegex(acceptance.AcceptanceError, "effective NoNewPrivs"):
            acceptance._verify_workspace_daemon_privileges()

    def test_system_installer_discovers_root_from_protected_installed_location(self):
        for prefix in (pathlib.Path("/opt/little-mester"),
                       pathlib.Path("/srv/little-mester-ci")):
            with self.subTest(prefix=prefix):
                root = prefix / "v3" / ("a" * 40)
                entry = root / "tools/vm-system-install"

                def safe_lstat(path):
                    if pathlib.Path(path) == entry:
                        return type("Info", (), {
                            "st_mode": stat.S_IFREG | 0o755, "st_uid": 0,
                        })()
                    return type("Info", (), {
                        "st_mode": stat.S_IFDIR | 0o755, "st_uid": 0,
                    })()

                with mock.patch.object(system_installer, "__file__", str(entry)), \
                     mock.patch.object(system_installer.os, "lstat", side_effect=safe_lstat):
                    self.assertEqual(system_installer.source_root(), root)

    def test_system_installer_refuses_unsafe_alternate_prefix_ancestry(self):
        prefix = pathlib.Path("/srv/little-mester-ci")
        root = prefix / "v3" / ("b" * 40)
        entry = root / "tools/vm-system-install"

        def unsafe_lstat(path):
            path = pathlib.Path(path)
            if path == prefix:
                return type("Info", (), {
                    "st_mode": stat.S_IFDIR | 0o777, "st_uid": 0,
                })()
            if path == entry:
                return type("Info", (), {
                    "st_mode": stat.S_IFREG | 0o755, "st_uid": 0,
                })()
            return type("Info", (), {
                "st_mode": stat.S_IFDIR | 0o755, "st_uid": 0,
            })()

        with mock.patch.object(system_installer, "__file__", str(entry)), \
             mock.patch.object(system_installer.os, "lstat", side_effect=unsafe_lstat), \
             self.assertRaisesRegex(RuntimeError, "ancestry must be root-owned"):
            system_installer.source_root()

    def test_qemu_is_main_process_and_service_policy_has_bounded_cgroup_fallback(self):
        unit = (ROOT / "systemd/little-mester-vm.service").read_text()
        self.assertIn("Type=exec", unit)
        exec_line = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
        self.assertTrue(exec_line.startswith("ExecStart=/usr/bin/qemu-system-x86_64 "))
        self.assertNotIn("system-vm.py qemu", exec_line)
        self.assertIn("Restart=no", unit)
        self.assertIn("KillMode=control-group", unit)
        self.assertIn("TimeoutStopSec=25s", unit)
        self.assertIn("SendSIGKILL=yes", unit)
        self.assertIn("ExecStop=/usr/bin/python3 -I /usr/lib/little-mester-vm/system-vm.py qmp-stop $MAINPID", unit)
        self.assertIn("DeviceAllow=/dev/kvm rw", unit)
        self.assertIn("SupplementaryGroups=kvm lm-vm-control", unit)
        self.assertIn("ReadWritePaths=/var/lib/little-mester-vm/disk.qcow2 /run/little-mester-vm", unit)
        self.assertNotIn("parent-supervisor", unit)
        self.assertIn("-blockdev", exec_line)
        self.assertIn("locking=on", exec_line)
        with mock.patch.object(vm, "_verify_kvm_access"), \
             mock.patch.object(vm, "_require_fixed_file"), \
             mock.patch.object(vm, "_validate_runtime_directory"), \
             mock.patch.object(vm, "_verify_workspace_share"), \
             mock.patch.object(vm, "read_proxy_port", return_value=vm.PROXY_PORT):
            expected = vm.qemu_argv()
        self.assertEqual(shlex.split(exec_line[len("ExecStart="):]), expected)

    def test_proxy_is_separate_and_qemu_requires_successful_proxy_probe(self):
        vm_unit = (ROOT / "systemd/little-mester-vm.service").read_text()
        proxy = (ROOT / "systemd/little-mester-vm-proxy.service").read_text()
        self.assertIn("Requires=little-mester-vm-proxy.service", vm_unit)
        self.assertIn("BindsTo=little-mester-vm-proxy.service", vm_unit)
        self.assertIn("After=little-mester-vm-proxy.service", vm_unit)
        self.assertIn("ExecStartPost=/usr/bin/python3 -I /usr/lib/little-mester-vm/system-vm.py proxy-ready", proxy)
        self.assertIn("User=lm-vm-proxy", proxy)
        self.assertIn("PartOf=little-mester-vm.service", proxy)
        self.assertIn("--port 32128", proxy)
        self.assertIn("def _verify_kvm_access", (ROOT / "lib/vm/system-vm.py").read_text())

    def test_kvm_device_group_dac_is_checked_in_addition_to_deviceallow(self):
        device = type("Device", (), {"st_mode": stat.S_IFCHR | 0o660, "st_gid": 4242})()
        with mock.patch.object(vm.grp, "getgrnam", return_value=type("Group", (), {"gr_gid": 4242})()), \
             mock.patch.object(vm.os, "stat", return_value=device), \
             mock.patch.object(vm.os, "getgroups", return_value=[4242]), \
             mock.patch.object(vm.os, "access", return_value=True):
            vm._verify_kvm_access()
        no_group_rw = type("Device", (), {"st_mode": stat.S_IFCHR | 0o600, "st_gid": 4242})()
        with mock.patch.object(vm.grp, "getgrnam", return_value=type("Group", (), {"gr_gid": 4242})()), \
             mock.patch.object(vm.os, "stat", return_value=no_group_rw), \
             mock.patch.object(vm.os, "getgroups", return_value=[4242]), \
             mock.patch.object(vm.os, "access", return_value=True):
            with self.assertRaisesRegex(vm.Refused, "group-owned by kvm"):
                vm._verify_kvm_access()

    def test_service_kvm_gate_still_requires_its_own_group_and_effective_access(self):
        world_rw_device = type("Device", (), {"st_mode": stat.S_IFCHR | 0o666, "st_gid": 4242})()
        group = type("Group", (), {"gr_gid": 4242})()
        with mock.patch.object(vm.grp, "getgrnam", return_value=group), \
             mock.patch.object(vm.os, "stat", return_value=world_rw_device), \
             mock.patch.object(vm.os, "getgroups", return_value=[]), \
             mock.patch.object(vm.os, "access", return_value=True):
            with self.assertRaisesRegex(vm.Refused, "exact supplementary group"):
                vm._verify_kvm_access()
        with mock.patch.object(vm.grp, "getgrnam", return_value=group), \
             mock.patch.object(vm.os, "stat", return_value=world_rw_device), \
             mock.patch.object(vm.os, "getgroups", return_value=[4242]), \
             mock.patch.object(vm.os, "access", return_value=False) as access:
            with self.assertRaisesRegex(vm.Refused, "lacks DAC read/write access"):
                vm._verify_kvm_access()
        access.assert_called_once_with("/dev/kvm", vm.os.R_OK | vm.os.W_OK, effective_ids=True)

    def test_sudoers_and_owner_cli_have_only_fixed_unit_and_operations(self):
        sudoers = (ROOT / "sudoers/little-mester-vm").read_text().splitlines()
        entries = [line for line in sudoers if line.startswith("%")]
        self.assertEqual(entries, [
            "%lm-vm-control ALL=(root) NOPASSWD: /usr/bin/systemctl start little-mester-vm.service",
            "%lm-vm-control ALL=(root) NOPASSWD: /usr/bin/systemctl stop little-mester-vm.service",
            "%lm-vm-control ALL=(root) NOPASSWD: /usr/bin/python3 -I /usr/lib/little-mester-vm/system-vm-workspace.py configure",
            "%lm-vm-control ALL=(root) NOPASSWD: /usr/bin/stat --format=%f\\:%d\\:%i -- /var/lib/little-mester-vm/disk.qcow2",
            "%lm-vm-control ALL=(root) NOPASSWD: /usr/bin/cat -- /etc/sudoers.d/little-mester-vm",
            "%lm-vm-control ALL=(root) NOPASSWD: /usr/bin/stat --format=%f\\:%u\\:%g\\:%h -- /etc/sudoers.d/little-mester-vm",
        ])
        self.assertFalse(any(token in "\n".join(entries) for token in ("*", "?", "[", "\"")))
        policy = type("Policy", (), {
            "ASSIGNMENT": pathlib.Path("/etc/little-mester-vm/workspace.json"),
            "verify_host_assignment": staticmethod(lambda *_args, **_kwargs: object()),
        })()
        with mock.patch.object(vm, "_workspace_policy", return_value=policy), \
             mock.patch.object(vm.subprocess, "run") as run, \
             mock.patch.object(vm, "_probe_workspace") as probe:
            run.return_value.returncode = 0
            self.assertEqual(vm._fixed_exec("up"), 0)
            self.assertEqual(run.call_args.args[0], [
                "/usr/bin/sudo", "-n", "/usr/bin/systemctl", "start", "little-mester-vm.service"])
            probe.assert_called_once_with()
        invalid_policy = type("Policy", (), {
            "Refused": RuntimeError,
            "verify_host_assignment": staticmethod(
                lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("bad drop-in"))),
        })()
        with mock.patch.object(vm, "_workspace_policy", return_value=invalid_policy), \
                mock.patch.object(vm.subprocess, "run") as run:
            with self.assertRaisesRegex(vm.Refused, "before VM lifecycle.*bad drop-in"):
                vm._fixed_exec("up")
        run.assert_not_called()
        with self.assertRaises(vm.Refused):
            vm._fixed_exec("systemctl")
        with mock.patch.object(vm.sys, "argv", ["system-vm.py", "owner", "up"]), \
             mock.patch.object(vm, "_fixed_exec", return_value=0) as dispatch:
            self.assertEqual(vm.main(), 0)
            dispatch.assert_called_once_with("up")
        with mock.patch.object(vm.sys, "argv", ["system-vm.py", "owner", "up", "--unit", "evil"]):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    vm.main()
        with mock.patch.object(vm, "_guest_ssh", return_value=0) as ssh, \
             mock.patch.object(vm, "_browser_auth", return_value=0) as auth:
            self.assertEqual(vm._fixed_exec("auth"), 0)
            auth.assert_called_once_with()
            self.assertEqual(vm._fixed_exec("tui"), 0)
            self.assertEqual(ssh.call_args.args[0], "/usr/local/bin/lm-opencode")

    def test_install_manifest_contains_all_fixed_assets(self):
        required = {
            "lib/vm/system-vm.py", "systemd/little-mester-vm.service",
            "lib/vm/system-vm-workspace.py", "systemd/little-mester-vm-workspace.service",
            "systemd/little-mester-vm-metrics.service",
            "lib/vm/guest-opencode-preferences.py",
            "lib/vm/system-vm-metrics.py", "lib/vm/parent-metrics.py",
            "lib/vm/parent-metrics-collect.py",
            "agent/opencode/plugins/host-metrics-tui/package.json",
            "agent/opencode/plugins/host-metrics-tui/tui.tsx",
            "agent/opencode/plugins/host-metrics-tui/metrics-endpoint.js",
            "agent/opencode/plugins/host-metrics-tui/metrics-endpoint.d.ts",
            "systemd/little-mester-vm-proxy.service", "sudoers/little-mester-vm",
            "tools/vm-system-install", "lib/vm/guest-bootstrap.py",
            "lib/vm/desktop-session-env.sh",
        }
        self.assertTrue(required.issubset(owner.INSTALL_FILES))
        self.assertIn("lib/vm/system-acceptance.py", owner.INSTALL_FILES)
        self.assertIn("lib/vm/host-dependencies.py", owner.INSTALL_FILES)
        self.assertEqual(owner.INSTALL_FILES["sudoers/little-mester-vm"], 0o444)
        self.assertTrue({"systemd", "sudoers"}.issubset(owner.INSTALL_DIRS))
        self.assertEqual(owner.INSTALL_FILES["tools/vm-system-install"], 0o755)
        installer = (ROOT / "tools/vm-system-install").read_text()
        self.assertIn("does not change host device policy", installer)
        self.assertIn('"provision"], check=True, stdin=subprocess.DEVNULL)', installer)
        self.assertIn('"lib/vm/guest-bootstrap.py": ("/usr/lib/little-mester-vm/guest-bootstrap.py"',
                      installer)
        self.assertIn('"lib/vm/system-vm.py": ("/usr/lib/little-mester-vm/system-vm.py"', installer)
        self.assertIn('"lib/vm/system-vm-workspace.py": ("/usr/lib/little-mester-vm/system-vm-workspace.py"', installer)
        self.assertIn('"lib/vm/system-vm-metrics.py":', installer)
        self.assertIn('"lib/vm/parent-metrics-collect.py":', installer)
        self.assertIn('"lib/vm/guest-opencode-preferences.py":', installer)
        self.assertIn('"agent/opencode/plugins/host-metrics-tui/tui.tsx":', installer)
        self.assertIn('"agent/opencode/plugins/host-metrics-tui/metrics-endpoint.js":', installer)
        self.assertIn('"systemd/little-mester-vm-metrics.service":', installer)
        self.assertIn('"systemd/little-mester-vm-workspace.service":', installer)

    def test_code_update_is_explicit_stopped_and_state_preserving(self):
        installer = (ROOT / "tools/vm-system-install").read_text()
        for required in (
            'sys.argv[1:] == ["--update-code-only"]',
            "def update_code_only(root: pathlib.Path, owner: str)",
            '"little-mester-vm-workspace.service"',
            "before_state = _state_fingerprint(state_paths)",
            "if _state_fingerprint(state_paths) != before_state:",
            '"/usr/bin/systemctl", "daemon-reload"',
            "services remain stopped",
            "UPDATE_ADDITIONALS",
            "ensure_dir(pathlib.Path(\"/etc/systemd/system/little-mester-vm-workspace.service.d\")",
        ):
            self.assertIn(required, installer)
        self.assertIn("--update-code-only", installer)
        self.assertIn('"lib/vm/host-dependencies.py": ("/usr/lib/little-mester-vm/host-dependencies.py"', installer)
        self.assertIn('Parse the exact pinned policy before creating accounts', installer)
        self.assertIn('preflight(root, owner)', installer)
        self.assertLess(installer.index('preflight(root, owner)'),
                        installer.index('ensure_group("lm-vm")'))

    def test_owner_acceptance_is_only_an_adapter_to_lab_vm(self):
        with mock.patch.object(owner.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(owner.owner_system_vm(pathlib.Path("/opt/fixed"), "down"), 0)
            self.assertEqual(run.call_args.args[0], ["/opt/fixed/lab-vm", "system", "down"])
        with self.assertRaises(owner.GateError):
            owner.owner_system_vm(pathlib.Path("/opt/fixed"), "down;id")

    def test_lifecycle_acceptance_requires_explicit_consent_without_tty(self):
        with mock.patch.object(owner.sys.stdin, "isatty", return_value=False), \
                self.assertRaisesRegex(owner.GateError, "requires --approve-system-vm-lifecycle"):
            owner.owner_system_vm(pathlib.Path("/opt/fixed"), "accept")

    def test_owner_acceptance_cli_has_read_only_verify_and_explicit_accept_modes(self):
        cli = (ROOT / "tools/vm-owner-acceptance").read_text()
        self.assertIn('choices=("verify", "accept", "up", "down", "status", "ssh", "auth", "tui")', cli)
        self.assertIn('"--approve-system-vm-lifecycle"', cli)

    def test_acceptance_refuses_ambiguous_start_states_without_stopping_vm(self):
        with mock.patch.object(acceptance, "service_state", return_value={
                "ActiveState": "inactive", "MainPID": "0"}), \
                self.assertRaisesRegex(acceptance.AcceptanceError, "lifecycle state is ambiguous"):
            acceptance._initial_running_state(
                {"ActiveState": "active", "MainPID": "123"},
                {"ActiveState": "inactive", "MainPID": "0"})
        with mock.patch.object(acceptance, "service_state", return_value={
                "ActiveState": "inactive", "MainPID": "0"}), \
                self.assertRaisesRegex(acceptance.AcceptanceError, "unit state is ambiguous"):
            acceptance._initial_running_state(
                {"ActiveState": "active", "MainPID": "0"},
                {"ActiveState": "active", "MainPID": "456"})

    def test_acceptance_failure_restores_preexisting_running_state(self):
        with mock.patch.object(acceptance, "run") as run, \
                mock.patch.object(acceptance, "service_state", return_value={
                    "ActiveState": "active", "MainPID": "987"}):
            acceptance._restore_initial_state(pathlib.Path("/opt/candidate"), True)
        self.assertEqual(run.call_args.args[0], [
            "/opt/candidate/lab-vm", "system", "up"])
        self.assertEqual(run.call_args.kwargs["timeout"], 1005)

    def test_acceptance_restoration_waits_for_dependency_shutdown_to_settle(self):
        stopped = {"ActiveState": "inactive", "MainPID": "0", "Result": "success"}
        stopping = {"ActiveState": "deactivating", "MainPID": "987", "Result": "success"}
        states = [stopping, stopped, stopped, stopped] + [stopped] * 12
        with mock.patch.object(acceptance, "run"), \
                mock.patch.object(acceptance, "service_state", side_effect=states) as state, \
                mock.patch.object(acceptance.time, "sleep") as sleep:
            acceptance._restore_initial_state(pathlib.Path("/opt/candidate"), False)
        self.assertEqual(state.call_count, 16)
        self.assertEqual(sleep.call_count, 3)

    def test_acceptance_restoration_fails_closed_when_services_do_not_settle(self):
        stopping = {"ActiveState": "deactivating", "MainPID": "987", "Result": "success"}
        with mock.patch.object(acceptance, "run"), \
                mock.patch.object(acceptance, "service_state", return_value=stopping), \
                mock.patch.object(acceptance.time, "monotonic", side_effect=[0, 0, 0, 0, 2]), \
                mock.patch.object(acceptance.time, "sleep"), \
                self.assertRaisesRegex(acceptance.AcceptanceError, "did not settle"):
            acceptance._restore_initial_state(pathlib.Path("/opt/candidate"), False, timeout=1)

    def test_acceptance_requires_effective_control_group_before_lifecycle_preflight(self):
        fake_pexpect = type("Pexpect", (), {})()
        runtime_dir = type("Info", (), {
            "st_mode": stat.S_IFDIR | 0o700, "st_uid": 1234,
        })()
        with mock.patch.dict("sys.modules", {"pexpect": fake_pexpect}), \
             mock.patch.object(acceptance.os.path, "isfile", return_value=True), \
             mock.patch.object(acceptance.os, "access", return_value=True), \
             mock.patch.object(acceptance.os, "getuid", return_value=1234), \
             mock.patch.object(acceptance.os, "getgroups", return_value=[99]), \
             mock.patch.object(acceptance.os, "getegid", return_value=100), \
             mock.patch.object(acceptance.os, "lstat", return_value=runtime_dir), \
             mock.patch.object(acceptance.grp, "getgrnam", return_value=type("Group", (), {"gr_gid": 42})()), \
             self.assertRaisesRegex(acceptance.AcceptanceError, "lacks effective lm-vm-control"):
            acceptance._lifecycle_preflight(pathlib.Path("/opt/candidate"))

    def test_acceptance_accepts_effective_supplementary_control_group_and_checks_key_metadata(self):
        fake_pexpect = type("Pexpect", (), {})()
        runtime_dir = type("Info", (), {
            "st_mode": stat.S_IFDIR | 0o700, "st_uid": 1234,
        })()
        control_key = type("Info", (), {
            "st_mode": stat.S_IFREG | 0o640, "st_uid": 0, "st_gid": 42, "st_nlink": 1,
        })()
        known_hosts = type("Info", (), {
            "st_mode": stat.S_IFREG | 0o644, "st_uid": 0, "st_gid": 0, "st_nlink": 1,
        })()
        with mock.patch.dict("sys.modules", {"pexpect": fake_pexpect}), \
             mock.patch.object(acceptance.os.path, "isfile", return_value=True), \
             mock.patch.object(acceptance.os, "access", return_value=True), \
             mock.patch.object(acceptance.os, "getuid", return_value=1234), \
             mock.patch.object(acceptance.os, "getgroups", return_value=[42]), \
             mock.patch.object(acceptance.os, "getegid", return_value=100), \
             mock.patch.object(acceptance.os, "lstat", side_effect=[runtime_dir, control_key, known_hosts]), \
             mock.patch.object(acceptance.grp, "getgrnam", return_value=type("Group", (), {"gr_gid": 42})()):
            acceptance._lifecycle_preflight(pathlib.Path("/opt/candidate"))

    def test_tui_acceptance_executes_installed_owner_interface_directly(self):
        class FakeChild:
            exitstatus = 0
            signalstatus = None

            def expect(self, _pattern, timeout=None):
                return None

            def sendcontrol(self, _value):
                return None

            def send(self, _value):
                return None

            def close(self, force=False):
                return None

            def isalive(self):
                return False

        child = FakeChild()
        fake_pexpect = type("Pexpect", (), {"EOF": object(), "spawn": mock.Mock(return_value=child)})()
        with mock.patch.dict("sys.modules", {"pexpect": fake_pexpect}):
            acceptance._tui_check(pathlib.Path("/opt/candidate"))
        args, kwargs = fake_pexpect.spawn.call_args
        self.assertEqual(args[:2], ("/opt/candidate/lab-vm", ["system", "tui"]))
        self.assertEqual(kwargs["encoding"], None)
        self.assertEqual(kwargs["timeout"], 90)

    def test_owner_acceptance_no_longer_depends_on_sg_but_ci_group_activation_remains(self):
        acceptance_source = (ROOT / "lib/vm/system-acceptance.py").read_text()
        ci_source = (ROOT / "tests/ci-kvm-system-unit.py").read_text()
        self.assertNotIn("/usr/bin/sg", acceptance_source)
        self.assertIn('"/usr/bin/sg", "lm-vm-control", "-c"', ci_source)

    def test_workspace_acceptance_requires_bind_compatible_home_isolation(self):
        acceptance_source = (ROOT / "lib/vm/system-acceptance.py").read_text()
        unit_source = (ROOT / "systemd/little-mester-vm-workspace.service").read_text()
        self.assertIn('workspace_properties.get("ProtectHome") != "tmpfs"', acceptance_source)
        self.assertIn("workspace_bind_matches = workspace_policy.effective_bind_paths_match(",
                      acceptance_source)
        self.assertIn("or not workspace_bind_matches", acceptance_source)
        self.assertIn("ProtectHome=tmpfs", unit_source)
        self.assertIn("ProtectSystem=strict", unit_source)

    def test_normal_acceptance_stop_rejects_systemd_fallback_failure(self):
        with mock.patch.object(acceptance, "service_state", side_effect=[
                {"ActiveState": "failed", "MainPID": "0", "Result": "exit-code"},
                {"ActiveState": "inactive", "MainPID": "0", "Result": "success"}]), \
                self.assertRaisesRegex(acceptance.AcceptanceError, "failed state"):
            acceptance._require_normal_stop()

    def test_normal_acceptance_stop_waits_for_proxy_deactivation_then_requires_clean_exit(self):
        vm_stopped = {"ActiveState": "inactive", "MainPID": "0", "Result": "success"}
        proxy_stopping = {"ActiveState": "deactivating", "MainPID": "4321", "Result": "success"}
        proxy_stopped = {"ActiveState": "inactive", "MainPID": "0", "Result": "success"}
        with mock.patch.object(acceptance, "service_state", side_effect=[
                vm_stopped, proxy_stopping, vm_stopped, vm_stopped,
                proxy_stopped, vm_stopped, vm_stopped, vm_stopped]) as state, \
                mock.patch.object(acceptance.time, "sleep") as sleep:
            acceptance._require_normal_stop()
        self.assertEqual(state.call_count, 8)
        sleep.assert_called_once()

    def test_normal_acceptance_stop_times_out_if_deactivating_unit_never_exits(self):
        vm_stopped = {"ActiveState": "inactive", "MainPID": "0", "Result": "success"}
        proxy_stopping = {"ActiveState": "deactivating", "MainPID": "4321", "Result": "success"}
        with mock.patch.object(acceptance, "service_state", side_effect=[
                vm_stopped, proxy_stopping, vm_stopped, vm_stopped]), \
                mock.patch.object(acceptance.time, "monotonic",
                                  side_effect=[10.0, 10.0, 10.0, 41.0]), \
                self.assertRaisesRegex(acceptance.AcceptanceError, "did not complete within 30s"):
            acceptance._require_normal_stop(timeout=30)

    def test_normal_acceptance_stop_rejects_inactive_unit_with_live_mainpid(self):
        vm_unexpected = {"ActiveState": "inactive", "MainPID": "4321", "Result": "success"}
        proxy_stopped = {"ActiveState": "inactive", "MainPID": "0", "Result": "success"}
        with mock.patch.object(acceptance, "service_state", side_effect=[
                vm_unexpected, proxy_stopped, proxy_stopped, proxy_stopped]), \
                self.assertRaisesRegex(acceptance.AcceptanceError, "unexpected live MainPID"):
            acceptance._require_normal_stop()

    def test_normal_acceptance_stop_rejects_missing_mainpid_evidence(self):
        vm_without_pid = {"ActiveState": "inactive", "Result": "success"}
        with mock.patch.object(acceptance, "service_state", return_value=vm_without_pid), \
                self.assertRaisesRegex(acceptance.AcceptanceError, "omitted MainPID"):
            acceptance._require_normal_stop()

    def test_effective_systemd_command_parser_preserves_exact_argument_boundaries(self):
        value = ('{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 -I '
                 '/usr/lib/little-mester-vm/system-vm.py qmp-stop $MAINPID ; '
                 'ignore_errors=no ; start_time=[x] }')
        self.assertEqual(acceptance._effective_argv(value), (
            "/usr/bin/python3", "/usr/bin/python3 -I /usr/lib/little-mester-vm/system-vm.py qmp-stop $MAINPID"))
        qemu = ('{ path=/usr/bin/qemu-system-x86_64 ; argv[]=/usr/bin/qemu-system-x86_64 '
                '-netdev user,id=net0,guestfwd=cmd:socat - TCP:127.0.0.1:32128,restrict=on ; '
                'ignore_errors=no ; start_time=[x] }')
        self.assertEqual(acceptance._effective_argv(qemu), (
            "/usr/bin/qemu-system-x86_64",
            "/usr/bin/qemu-system-x86_64 -netdev user,id=net0,guestfwd=cmd:socat - TCP:127.0.0.1:32128,restrict=on"))
        with self.assertRaisesRegex(acceptance.AcceptanceError, "effective command"):
            acceptance._effective_argv("ExecStart=/usr/bin/false")
        multiple = value + ' { path=/usr/bin/false ; argv[]=/usr/bin/false ; ignore_errors=no }'
        with self.assertRaisesRegex(acceptance.AcceptanceError, "multiple effective commands"):
            acceptance._effective_argv(multiple)

    def test_system_acceptance_report_contains_protected_lifecycle_gates(self):
        report = owner.report_template("a" * 40, "system_vm_accept", pathlib.Path("attempt"))
        self.assertEqual(report["evidence"]["restoration_outcome"], "NOT_REQUIRED")
        self.assertIn("candidate", report["gates"])
        self.assertIn("deployed_candidate_verification", report["gates"])
        self.assertIn("same_disk_persistence", report["gates"])
        self.assertIn("final_vm_state", report["gates"])

    def test_acceptance_command_failure_has_bounded_sanitized_operation_diagnostic(self):
        result = subprocess.CompletedProcess(
            ["/fixed/command"], 17, b"", (
                b"safe failure detail\n"
                b"Permission denied (publickey).\n"
                b"https://alice:secret@example.com/repo\n"
                b"https://abc123456789@example.com/repo\n"
                b"Bearer ghp_1234567890abcdefghijklmnop\n"
                b"github_pat_abcdefghijklmnopqrstuvwxyz1234567890\n"
                b"-----BEGIN OPENSSH PRIVATE KEY-----\nprivate-key-material\n"
                b"-----END OPENSSH PRIVATE KEY-----\n"
                b"-----BEGIN OPENSSH PRIVATE KEY-----\nincomplete-private-key-material\n"
                b"ghp_1234567890abcdefghijklmnop\n" + b"x" * 5000))
        with mock.patch.object(acceptance.subprocess, "run", return_value=result):
            with self.assertRaises(acceptance.AcceptanceError) as raised:
                acceptance.run(["/fixed/command"], operation="verify_persistence_marker_after_restart")
        message = str(raised.exception)
        self.assertIn("verify_persistence_marker_after_restart", message)
        self.assertIn("exit=17", message)
        self.assertIn("safe failure detail", message)
        self.assertNotIn("Permission denied", message)
        self.assertNotIn("alice:secret", message)
        self.assertNotIn("abc123456789", message)
        self.assertNotIn("ghp_1234567890", message)
        self.assertNotIn("github_pat_abcdefghijklmnopqrstuvwxyz", message)
        self.assertNotIn("private-key-material", message)
        self.assertNotIn("incomplete-private-key-material", message)
        self.assertLess(len(message), 1400)

    def test_system_acceptance_failure_diagnostic_is_printed_to_ci_output(self):
        report = owner.report_template("a" * 40, "system_vm_accept", pathlib.Path("attempt"))
        report["evidence"]["failure_diagnostic"] = {
            "stage": "same_disk_persistence",
            "operation": "restart_vm_on_same_disk",
            "exception_type": "AcceptanceError",
            "reason": "restart_vm_on_same_disk failed (exit=1)",
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            owner.print_report(report)
        self.assertIn('system_vm_failure_diagnostic={"exception_type":"AcceptanceError"', output.getvalue())
        self.assertIn('"operation":"restart_vm_on_same_disk"', output.getvalue())

    def test_failure_report_write_error_does_not_skip_initial_state_restoration(self):
        stopped = {"ActiveState": "inactive", "MainPID": "0", "Result": "success"}

        def progress(name, **_evidence):
            if name == "failure_diagnostic":
                raise OSError("report storage unavailable")

        with mock.patch.object(acceptance, "verify", return_value=(stopped, stopped)), \
                mock.patch.object(acceptance, "_initial_running_state", return_value=False), \
                mock.patch.object(acceptance, "_lifecycle_preflight"), \
                mock.patch.object(acceptance, "_proxy_policy_checks"), \
                mock.patch.object(acceptance, "disk_identity", return_value=(1, 2)), \
                mock.patch.object(acceptance, "run", side_effect=[
                    acceptance.AcceptanceError("initial VM start failed"),
                    subprocess.CompletedProcess([], 0, b"", b""),
                ]) as run, \
                mock.patch.object(acceptance, "service_state", return_value=stopped):
            with self.assertRaisesRegex(acceptance.AcceptanceError, "start_initial_vm"):
                acceptance.accept(pathlib.Path("/opt/candidate"), progress=progress)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.kwargs["operation"], "restore_initial_vm_state_down")

    def test_system_disk_paths_are_new_fixed_paths_and_never_parent_v3(self):
        self.assertEqual(str(vm.DISK), "/var/lib/little-mester-vm/disk.qcow2")
        self.assertEqual(str(vm.SEED), "/var/lib/little-mester-vm/seed.iso")
        self.assertNotIn("parent-v3", str(vm.DISK))
        self.assertNotIn("v3-smoke", str(vm.DISK))
        self.assertNotIn("/workspace", str(vm.DISK))

    def test_acceptance_persistence_marker_is_on_guest_disk_and_verified_before_removal(self):
        source = inspect.getsource(acceptance.accept)
        self.assertRegex(
            source,
            r'marker\s*=\s*f"\.lm-system-accept-[^" ]*',
        )
        self.assertNotIn('marker = f"/workspace/', source)
        self.assertNotIn('marker = f"/var/lib/', source)
        self.assertIn('GUEST_PERSISTENCE_STATE = "/home/mester/.local/state"',
                      inspect.getsource(acceptance))

        tags = list(re.finditer(r'(?m)^\s*operation = "([^"]+)"', source))
        positions = {match.group(1): match.start() for match in tags}

        def operation_segment(name, stop_name=None):
            start = positions[name]
            if stop_name is not None:
                end = positions[stop_name]
            else:
                end = next((match.start() for match in tags if match.start() > start), len(source))
            return source[start:end]

        create = operation_segment("create_and_verify_persistence_marker")
        self.assertIn("_guest_persistence_marker_command(", create)
        self.assertIn('"create"', create)
        self.assertNotIn("mkdir", create)

        proof = operation_segment("verify_persistence_marker_after_restart")
        self.assertIn("_guest_ssh(", proof)
        self.assertIn("_guest_persistence_marker_command(", proof)
        self.assertIn('"verify"', proof)

        remove_position = positions["remove_persistence_marker"]
        failure_position = source.index("except BaseException as failure:")
        self.assertLess(positions["verify_persistence_marker_after_restart"], remove_position)
        self.assertLess(remove_position, failure_position)
        remove = operation_segment("remove_persistence_marker")
        self.assertIn("_guest_ssh(", remove)
        self.assertIn('"remove"', remove)

    def test_guest_persistence_marker_script_uses_no_follow_dirfds_and_virtio_blk_topology(self):
        script = acceptance.GUEST_PERSISTENCE_MARKER_SCRIPT
        for component in ('"home"', '"mester"', '".local"', '"state"'):
            self.assertIn(component, script)
        self.assertIn("os.open(component", script)
        self.assertIn("dir_fd=fds[-1]", script)
        self.assertIn("os.O_DIRECTORY | os.O_NOFOLLOW", script)
        self.assertIn("os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW", script)
        self.assertIn("os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK", script)
        self.assertIn("os.unlink(name, dir_fd=state_fd)", script)
        self.assertIn("mnt_id", script)
        self.assertIn("st_dev", script)
        self.assertIn("virtiofs", script)
        self.assertIn("virtio_blk", script)
        self.assertIn('SYSFS_ROOT = "/sys"', script)
        self.assertIn("/dev/block", script)
        self.assertNotIn("/dev/vda", script)
        self.assertNotIn("mkdir", script)

    def test_guest_persistence_marker_script_accepts_virtio_blk_and_uses_verified_dirfd(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            guest_root = base / "guest"
            sysfs_root = base / "sys"
            marker = ".lm-system-accept-testnonce"
            expected = "systemd-owner-acceptance-testnonce"
            (guest_root / "home/mester/.local/state").mkdir(parents=True, mode=0o700)
            (guest_root / "home").chmod(0o755)
            (guest_root / "home/mester").chmod(0o755)
            for path in (guest_root / "home/mester/.local", guest_root / "home/mester/.local/state"):
                path.chmod(0o700)
            whole_disk = sysfs_root / "devices/pci0000:00/virtio0/block/vdb"
            (whole_disk / "device").mkdir(parents=True)
            driver = sysfs_root / "bus/virtio/drivers/virtio_blk"
            driver.mkdir(parents=True)
            (whole_disk / "device/driver").symlink_to(driver)
            root_partition = whole_disk / "vdb1"
            root_partition.mkdir()
            (root_partition / "partition").write_text("1\n")
            device = os.stat(guest_root).st_dev
            dev_block = sysfs_root / "dev/block"
            dev_block.mkdir(parents=True)
            (dev_block / f"{os.major(device)}:{os.minor(device)}").symlink_to(root_partition)

            script = acceptance.GUEST_PERSISTENCE_MARKER_SCRIPT.replace(
                'ROOT_PATH = "/"', f"ROOT_PATH = {str(guest_root)!r}").replace(
                'SYSFS_ROOT = "/sys"', f"SYSFS_ROOT = {str(sysfs_root)!r}")

            def invoke(action):
                return subprocess.run(
                    ["python3", "-c", script, action, marker, expected],
                    text=True, capture_output=True, check=False)

            created = invoke("create")
            self.assertEqual(created.returncode, 0, created.stderr)
            marker_path = guest_root / "home/mester/.local/state" / marker
            self.assertEqual(marker_path.read_text(), expected)
            verified = invoke("verify")
            self.assertEqual(verified.returncode, 0, verified.stderr)
            removed = invoke("remove")
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertFalse(marker_path.exists())

    def test_guest_persistence_marker_refuses_symlink_at_every_path_component(self):
        components = ("home", "mester", ".local", "state")
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            sysfs_root = base / "sys"
            script_template = acceptance.GUEST_PERSISTENCE_MARKER_SCRIPT
            expected = "systemd-owner-acceptance-testnonce"
            marker = ".lm-system-accept-testnonce"
            for index, component in enumerate(components):
                guest_root = base / f"guest-{index}"
                target = base / f"outside-{index}"
                target.mkdir()
                parent = guest_root
                parent.mkdir()
                for prior in components[:index]:
                    parent = parent / prior
                    parent.mkdir(mode=0o700)
                (parent / component).symlink_to(target, target_is_directory=True)
                script = script_template.replace(
                    'ROOT_PATH = "/"', f"ROOT_PATH = {str(guest_root)!r}").replace(
                    'SYSFS_ROOT = "/sys"', f"SYSFS_ROOT = {str(sysfs_root)!r}")
                result = subprocess.run(
                    ["python3", "-c", script, "create", marker, expected],
                    text=True, capture_output=True, check=False)
                self.assertNotEqual(result.returncode, 0, component)
                self.assertFalse((target / marker).exists(), component)

    def test_guest_persistence_marker_fails_closed_for_non_virtio_blk_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            guest_root = base / "guest"
            sysfs_root = base / "sys"
            (guest_root / "home/mester/.local/state").mkdir(parents=True, mode=0o700)
            (guest_root / "home").chmod(0o755)
            (guest_root / "home/mester").chmod(0o755)
            for path in (guest_root / "home/mester/.local", guest_root / "home/mester/.local/state"):
                path.chmod(0o700)
            block = sysfs_root / "devices/pci0000:00/block/vdb"
            (block / "device").mkdir(parents=True)
            other_driver = sysfs_root / "bus/pci/drivers/other"
            other_driver.mkdir(parents=True)
            (block / "device/driver").symlink_to(other_driver)
            device = os.stat(guest_root).st_dev
            dev_block = sysfs_root / "dev/block"
            dev_block.mkdir(parents=True)
            (dev_block / f"{os.major(device)}:{os.minor(device)}").symlink_to(block)
            script = acceptance.GUEST_PERSISTENCE_MARKER_SCRIPT.replace(
                'ROOT_PATH = "/"', f"ROOT_PATH = {str(guest_root)!r}").replace(
                'SYSFS_ROOT = "/sys"', f"SYSFS_ROOT = {str(sysfs_root)!r}")
            result = subprocess.run(
                ["python3", "-c", script, "create", ".lm-system-accept-testnonce",
                 "systemd-owner-acceptance-testnonce"],
                text=True, capture_output=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((guest_root / "home/mester/.local/state/.lm-system-accept-testnonce").exists())

    def test_system_guest_seed_uses_canonical_parent_cloud_init_and_session_contract(self):
        data = bootstrap.build_foundation(
            name="little-mester-system-vm", uid=1000,
            ssh_key="ssh-ed25519 AAAA control", workspace="virtiofs",
            helper="/usr/local/libexec/lm-guest-user-dirs.py")
        contract.add_protected_session(
            data, host_private="private-key\n", host_public="ssh-ed25519 AAAA host\n",
            control_public="ssh-ed25519 AAAA control\n", workspace_path=contract.SYSTEM_WORKSPACE)
        self.assertEqual(data["hostname"], "little-mester-system-vm")
        self.assertTrue(data["ssh_keys"]["ed25519_private"].startswith("private-key"))
        files = {item["path"]: item for item in data["write_files"]}
        self.assertIn("/usr/local/bin/lm-opencode", files)
        self.assertIn("/usr/local/libexec/lm-opencode-auth", files)
        self.assertIn("/usr/local/libexec/lm-guest-user-dirs.py", files)
        self.assertIn("/etc/opencode/plugins/little-mester-exit-confirm/tui.tsx", files)
        self.assertIn("/etc/opencode/plugins/little-mester-host-metrics-tui/tui.tsx", files)
        self.assertIn("/etc/opencode/plugins/little-mester-host-metrics-tui/metrics-endpoint.js", files)
        self.assertIn("/etc/opencode/opencode-model.json", files)
        model_config = json.loads(files["/etc/opencode/opencode-model.json"]["content"])
        self.assertEqual(model_config["model"], "{env:LM_OPENCODE_MODEL}")
        provision = files["/usr/local/sbin/mester-v3-provision"]["content"]
        self.assertIn("docker-ce-rootless-extras", provision)
        self.assertIn("dockerd-rootless-setuptool.sh install", provision)
        self.assertIn("prepare 1000", provision)
        self.assertIn("prepare 1000", provision)
        self.assertIn("@opencode/cli@2.2.5", provision)
        self.assertIn("prepare-auth", files["/usr/local/bin/lm-opencode"]["content"])
        self.assertIn("cd /workspace", files["/usr/local/bin/lm-opencode"]["content"])
        self.assertIn("lm-guest-opencode-preferences.py model", files["/usr/local/libexec/lm-opencode"]["content"])
        self.assertIn("workspace /workspace virtiofs", provision)
        self.assertNotIn("/home/mester/workspace", provision)
        system_source = (ROOT / "lib/vm/system-vm.py").read_text()
        self.assertIn("bootstrap.build_foundation(", system_source)
        self.assertIn("contract.add_protected_session(", system_source)
        self.assertFalse(data["ssh_pwauth"])
        self.assertTrue(data["disable_root"])
        self.assertTrue(any(item["path"] == "/etc/apt/apt.conf.d/80mester-proxy"
                            for item in data["write_files"]))
        legacy = contract.parent_cloud_init_data(
            host_private="private-key\n", host_public="ssh-ed25519 AAAA host\n",
            control_public="ssh-ed25519 AAAA control\n")
        legacy_launcher = next(item["content"] for item in legacy["write_files"]
                               if item.get("path") == "/usr/local/bin/lm-opencode")
        self.assertIn("cd /home/mester/workspace", legacy_launcher)
        self.assertNotIn("--host-metrics", legacy_launcher)
        legacy_profile = next(item["content"] for item in legacy["write_files"]
                              if item.get("path") == "/etc/profile.d/little-mester-opencode.sh")
        self.assertNotIn("host-metrics-tui", legacy_profile)
        with self.assertRaises(ValueError):
            bootstrap.build_foundation(name="bad/name", uid=1000, ssh_key="key",
                                       workspace="disk", helper="/usr/local/libexec/lm-guest-user-dirs.py")

    def test_smoke_and_system_use_same_provisioning_builder_with_workspace_specific_adaptation(self):
        helper = "/usr/local/sbin/mester-v3-user-dirs.py"
        smoke_script = bootstrap.provision_script(1234, "virtiofs", helper)
        smoke_yaml = bootstrap.render_smoke_cloud_init(
            "smoke-v3", 1234,
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE7zJmK1W1h3V4eA7qQKX8U4n9x0z8m1c2b3d4e5f6g7 test")
        system_script = bootstrap.provision_script(
            1000, "virtiofs", "/usr/local/libexec/lm-guest-user-dirs.py")
        self.assertIn("guest-bootstrap.py\" smoke-cloud-init", (ROOT / "lib/vm/cloud-init.sh").read_text())
        self.assertIn("apt-get install -y docker-ce docker-ce-cli containerd.io", smoke_script)
        self.assertIn("apt-get install -y docker-ce docker-ce-cli containerd.io", system_script)
        self.assertIn("/workspace virtiofs", smoke_script)
        self.assertIn("workspace /workspace virtiofs", system_script)
        self.assertNotIn("/home/mester/workspace", system_script)
        self.assertIn("mountpoint -q /workspace", vm._guest_workspace_mount.__code__.co_consts[1])
        self.assertNotIn("sudo -n", vm._guest_workspace_mount.__code__.co_consts[1])
        self.assertIn("export DOCKER_HOST=unix:///run/user/1234/docker.sock", smoke_yaml)
        self.assertIn("NO_PROXY=localhost,127.0.0.1,10.0.2.100", smoke_yaml)
        self.assertIn("disable_root: true", smoke_yaml)
        self.assertIn("docker-ce-rootless-extras", smoke_yaml)

    def test_installed_session_contract_resolves_colocated_plugin_assets(self):
        with tempfile.TemporaryDirectory(prefix="lm-session-install-layout-") as temporary:
            installed = pathlib.Path(temporary) / "libexec/little-mester-vm"
            plugin_dir = installed / "plugins"
            plugin_dir.mkdir(parents=True)
            metrics_plugin_dir = installed / "host-metrics-tui"
            metrics_plugin_dir.mkdir()
            shutil.copyfile(ROOT / "lib/vm/guest-user-dirs.py", installed / "guest-user-dirs.py")
            shutil.copyfile(ROOT / "lib/vm/guest-opencode-preferences.py",
                            installed / "guest-opencode-preferences.py")
            shutil.copyfile(ROOT / "lib/vm/guest-bootstrap.py", installed / "guest-bootstrap.py")
            shutil.copyfile(ROOT / "lib/vm/parent-session-contract.py",
                            installed / "parent-session-contract.py")
            source_plugins = ROOT / "agent/opencode/plugins/little-mester-exit-confirm"
            shutil.copyfile(source_plugins / "package.json", plugin_dir / "package.json")
            shutil.copyfile(source_plugins / "tui.tsx", plugin_dir / "tui.tsx")
            source_metrics_plugin = ROOT / "agent/opencode/plugins/host-metrics-tui"
            shutil.copyfile(source_metrics_plugin / "package.json", metrics_plugin_dir / "package.json")
            shutil.copyfile(source_metrics_plugin / "tui.tsx", metrics_plugin_dir / "tui.tsx")
            shutil.copyfile(source_metrics_plugin / "metrics-endpoint.js", metrics_plugin_dir / "metrics-endpoint.js")
            shutil.copyfile(source_metrics_plugin / "metrics-endpoint.d.ts", metrics_plugin_dir / "metrics-endpoint.d.ts")
            (plugin_dir / "package.json").chmod(0o644)
            (plugin_dir / "tui.tsx").chmod(0o644)
            for asset in metrics_plugin_dir.iterdir():
                asset.chmod(0o644)
            installed_contract = load("installed_parent_session_contract",
                                      installed / "parent-session-contract.py")
            self.assertEqual(installed_contract.PLUGIN_ROOT, plugin_dir)
            data = installed_contract.parent_cloud_init_data(
                host_private="private-key\n", host_public="ssh-ed25519 AAAA host\n",
                control_public="ssh-ed25519 AAAA control\n")
            self.assertTrue(any(item["path"].endswith("/tui.tsx") for item in data["write_files"]))

    def test_qmp_handshake_powerdown_and_pidfd_wait(self):
        with tempfile.TemporaryDirectory(prefix="lm-qmp-test-") as directory:
            path = pathlib.Path(directory) / "qmp.sock"
            transcript = pathlib.Path(directory) / "commands"
            server = textwrap.dedent("""
                import os, socket, sys
                path, transcript = sys.argv[1:]
                server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                server.bind(path); os.chmod(path, 0o600); server.listen(1)
                connection, _ = server.accept()
                connection.sendall(b'{"QMP":{"version":{},"capabilities":[]}}\\r\\n')
                with open(transcript, 'w') as output:
                    for _ in range(2):
                        line = connection.makefile('rb').readline()
                        output.write(line.decode()); output.flush()
                        connection.sendall(b'{"return":{}}\\r\\n')
                connection.close(); server.close()
            """)
            process = subprocess.Popen(["/usr/bin/python3", "-c", server, str(path), str(transcript)])
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not path.exists():
                time.sleep(0.01)
            try:
                with mock.patch.object(vm, "QMP_SOCKET_PATH", path), \
                     mock.patch.object(vm, "_validate_runtime_directory"):
                    vm.qmp_powerdown_and_wait(process.pid, timeout_seconds=3)
                self.assertEqual(process.wait(timeout=2), 0)
                self.assertEqual(transcript.read_text().splitlines(), [
                    '{"execute":"qmp_capabilities"}', '{"execute":"system_powerdown"}'])
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_qmp_timeout_is_reported_for_systemd_cgroup_fallback(self):
        source = (ROOT / "lib/vm/system-vm.py").read_text()
        self.assertIn("QEMU did not exit after bounded QMP system_powerdown; systemd KILL fallback required", source)
        self.assertIn("TimeoutStopSec=25s", (ROOT / "systemd/little-mester-vm.service").read_text())

    def test_qmp_powerdown_timeout_returns_failure_for_systemd_fallback(self):
        with tempfile.TemporaryDirectory(prefix="lm-qmp-timeout-") as directory:
            path = pathlib.Path(directory) / "qmp.sock"
            server = textwrap.dedent("""
                import os, socket, sys, time
                path = sys.argv[1]
                server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                server.bind(path); os.chmod(path, 0o600); server.listen(1)
                connection, _ = server.accept()
                connection.sendall(b'{"QMP":{"version":{},"capabilities":[]}}\\r\\n')
                stream = connection.makefile('rb')
                for _ in range(2):
                    stream.readline()
                    connection.sendall(b'{"return":{}}\\r\\n')
                time.sleep(3)
            """)
            process = subprocess.Popen(["/usr/bin/python3", "-c", server, str(path)])
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not path.exists():
                time.sleep(0.01)
            try:
                with mock.patch.object(vm, "QMP_SOCKET_PATH", path), \
                     mock.patch.object(vm, "_validate_runtime_directory"):
                    with self.assertRaisesRegex(vm.Refused, "systemd KILL fallback required"):
                        vm.qmp_powerdown_and_wait(process.pid, timeout_seconds=0.2)
            finally:
                process.kill()
                process.wait()


class StageTimerTests(unittest.TestCase):
    """Contract tests for the proposed safe, allowlisted stage timer API.

    Assumed API: StageTimer(stream, wall_clock=..., monotonic=...) exposes a
    ``timed_stage(label)`` context manager. It emits compact JSON records as
    ``system_vm_stage=<json>`` lines. The source implementation is intentionally
    not part of this tests-only change.
    """

    class FlushRecordingStream(io.StringIO):
        def __init__(self):
            super().__init__()
            self.flush_snapshots = []

        def flush(self):
            self.flush_snapshots.append(self.getvalue())
            super().flush()

    @staticmethod
    def fixed_wall_clock():
        values = iter((
            datetime.datetime(2026, 11, 21, 12, 0, 0, tzinfo=datetime.timezone.utc),
            datetime.datetime(2026, 11, 21, 12, 0, 1, 250000, tzinfo=datetime.timezone.utc),
            datetime.datetime(2026, 11, 21, 12, 0, 2, tzinfo=datetime.timezone.utc),
            datetime.datetime(2026, 11, 21, 12, 0, 2, 500000, tzinfo=datetime.timezone.utc),
        ))
        return lambda: next(values)

    @staticmethod
    def line(event, label, timestamp, **extra):
        record = {"event": event, "label": label, "timestamp": timestamp, **extra}
        return "system_vm_stage=" + json.dumps(record, separators=(",", ":")) + "\n"

    def test_allowlisted_records_are_exact_utc_paired_timed_and_flushed(self):
        output = self.FlushRecordingStream()
        timer = acceptance.StageTimer(
            output, wall_clock=self.fixed_wall_clock(), monotonic=iter((4.0, 4.125, 8.0, 8.010)).__next__)

        with timer.timed_stage("guest_readiness_ssh"):
            pass
        with timer.timed_stage("guest_readiness_ssh"):
            pass

        expected = (
            self.line("START", "guest_readiness_ssh", "2026-11-21T12:00:00Z")
            + self.line("END", "guest_readiness_ssh", "2026-11-21T12:00:01.250000Z",
                        elapsed_ms=125, outcome="PASS")
            + self.line("START", "guest_readiness_ssh", "2026-11-21T12:00:02Z")
            + self.line("END", "guest_readiness_ssh", "2026-11-21T12:00:02.500000Z",
                        elapsed_ms=10, outcome="PASS")
        )
        self.assertEqual(output.getvalue(), expected)
        expected_records = expected.splitlines(keepends=True)
        self.assertEqual(output.flush_snapshots, [
            "".join(expected_records[:count]) for count in range(1, len(expected_records) + 1)])
        records = [json.loads(line.partition("=")[2]) for line in output.getvalue().splitlines()]
        self.assertEqual([(row["event"], row["label"]) for row in records], [
            ("START", "guest_readiness_ssh"), ("END", "guest_readiness_ssh"),
            ("START", "guest_readiness_ssh"), ("END", "guest_readiness_ssh")])
        self.assertTrue(all(row["timestamp"].endswith("Z") for row in records))

    def test_failure_records_fail_and_reraises_original_baseexception(self):
        output = io.StringIO()
        failure = KeyboardInterrupt("secret command and exception detail")
        timer = acceptance.StageTimer(
            output,
            wall_clock=lambda: datetime.datetime(2026, 11, 21, tzinfo=datetime.timezone.utc),
            monotonic=iter((1.0, 3.5)).__next__)
        with self.assertRaises(KeyboardInterrupt) as raised:
            with timer.timed_stage("network_boundaries"):
                raise failure
        self.assertIs(raised.exception, failure)
        self.assertEqual(output.getvalue(),
                         self.line("START", "network_boundaries", "2026-11-21T00:00:00Z")
                         + self.line("END", "network_boundaries", "2026-11-21T00:00:00Z",
                                     elapsed_ms=2500, outcome="FAIL"))

    def test_invalid_label_is_rejected_before_any_output(self):
        output = io.StringIO()
        timer = acceptance.StageTimer(output, wall_clock=self.fixed_wall_clock(), monotonic=lambda: 0)
        with self.assertRaises(ValueError):
            with timer.timed_stage("run-command-with-secret-token"):
                self.fail("invalid stage must not enter")
        self.assertEqual(output.getvalue(), "")

    def test_records_never_include_command_exception_or_secret_values(self):
        output = io.StringIO()
        timer = acceptance.StageTimer(
            output,
            wall_clock=lambda: datetime.datetime(2026, 11, 21, tzinfo=datetime.timezone.utc),
            monotonic=iter((0.0, 0.001)).__next__)
        with self.assertRaisesRegex(RuntimeError, "command-secret-value"):
            with timer.timed_stage("persistence_fixture"):
                raise RuntimeError("command-secret-value ghp_1234567890abcdefghijklmnop")
        rendered = output.getvalue()
        self.assertIn('"outcome":"FAIL"', rendered)
        for forbidden in ("command-secret-value", "ghp_1234567890", "RuntimeError", "secret"):
            self.assertNotIn(forbidden, rendered)


if __name__ == "__main__":
    unittest.main()
