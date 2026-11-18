#!/usr/bin/env python3
"""Non-host policy, CLI and QMP protocol tests for the systemd VM slice."""

from __future__ import annotations

import importlib.util
import importlib.machinery
import contextlib
import io
import os
import pathlib
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


class SystemdPolicyTests(unittest.TestCase):
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
        self.assertIn("ExecStop=/usr/bin/python3 -I /usr/libexec/little-mester-vm/system-vm.py qmp-stop $MAINPID", unit)
        self.assertIn("DeviceAllow=/dev/kvm rw", unit)
        self.assertIn("SupplementaryGroups=kvm lm-vm-control", unit)
        self.assertIn("ReadWritePaths=/var/lib/little-mester-vm/disk.qcow2 /run/little-mester-vm", unit)
        self.assertNotIn("parent-supervisor", unit)
        self.assertIn("-blockdev", exec_line)
        self.assertIn("locking=on", exec_line)
        with mock.patch.object(vm, "_verify_kvm_access"), \
             mock.patch.object(vm, "_require_fixed_file"), \
             mock.patch.object(vm, "_validate_runtime_directory"), \
             mock.patch.object(vm, "read_proxy_port", return_value=vm.PROXY_PORT):
            expected = vm.qemu_argv()
        self.assertEqual(shlex.split(exec_line[len("ExecStart="):]), expected)

    def test_proxy_is_separate_and_qemu_requires_successful_proxy_probe(self):
        vm_unit = (ROOT / "systemd/little-mester-vm.service").read_text()
        proxy = (ROOT / "systemd/little-mester-vm-proxy.service").read_text()
        self.assertIn("Requires=little-mester-vm-proxy.service", vm_unit)
        self.assertIn("BindsTo=little-mester-vm-proxy.service", vm_unit)
        self.assertIn("After=little-mester-vm-proxy.service", vm_unit)
        self.assertIn("ExecStartPost=/usr/bin/python3 -I /usr/libexec/little-mester-vm/system-vm.py proxy-ready", proxy)
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

    def test_sudoers_and_owner_cli_have_only_fixed_unit_and_operations(self):
        sudoers = (ROOT / "sudoers/little-mester-vm").read_text().splitlines()
        entries = [line for line in sudoers if line.startswith("%")]
        self.assertEqual(entries, [
            "%lm-vm-control ALL=(root) NOPASSWD: /usr/bin/systemctl start little-mester-vm.service",
            "%lm-vm-control ALL=(root) NOPASSWD: /usr/bin/systemctl stop little-mester-vm.service",
        ])
        self.assertFalse(any(token in "\n".join(entries) for token in ("*", "?", "[", "\"")))
        with mock.patch.object(vm.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(vm._fixed_exec("up"), 0)
            self.assertEqual(run.call_args.args[0], [
                "/usr/bin/sudo", "-n", "/usr/bin/systemctl", "start", "little-mester-vm.service"])
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
            "systemd/little-mester-vm-proxy.service", "sudoers/little-mester-vm",
            "tools/vm-system-install", "lib/vm/guest-bootstrap.py",
        }
        self.assertTrue(required.issubset(owner.INSTALL_FILES))
        self.assertTrue({"systemd", "sudoers"}.issubset(owner.INSTALL_DIRS))
        self.assertEqual(owner.INSTALL_FILES["tools/vm-system-install"], 0o755)
        installer = (ROOT / "tools/vm-system-install").read_text()
        self.assertIn("does not change host device policy", installer)
        self.assertIn("system-vm.py\",\n                        \"provision", installer)
        self.assertIn('"lib/vm/guest-bootstrap.py": ("/usr/libexec/little-mester-vm/guest-bootstrap.py"',
                      installer)

    def test_owner_acceptance_is_only_an_adapter_to_lab_vm(self):
        with mock.patch.object(owner.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(owner.owner_system_vm(pathlib.Path("/opt/fixed"), "down"), 0)
            self.assertEqual(run.call_args.args[0], ["/opt/fixed/lab-vm", "system", "down"])
        with self.assertRaises(owner.GateError):
            owner.owner_system_vm(pathlib.Path("/opt/fixed"), "down;id")

    def test_system_disk_paths_are_new_fixed_paths_and_never_parent_v3(self):
        self.assertEqual(str(vm.DISK), "/var/lib/little-mester-vm/disk.qcow2")
        self.assertEqual(str(vm.SEED), "/var/lib/little-mester-vm/seed.iso")
        self.assertNotIn("parent-v3", str(vm.DISK))
        self.assertNotIn("v3-smoke", str(vm.DISK))
        self.assertNotIn("/workspace", str(vm.DISK))

    def test_system_guest_seed_uses_canonical_parent_cloud_init_and_session_contract(self):
        data = bootstrap.build_foundation(
            name="little-mester-system-vm", uid=1000,
            ssh_key="ssh-ed25519 AAAA control", workspace="disk",
            helper="/usr/local/libexec/lm-guest-user-dirs.py")
        contract.add_protected_session(
            data, host_private="private-key\n", host_public="ssh-ed25519 AAAA host\n",
            control_public="ssh-ed25519 AAAA control\n")
        self.assertEqual(data["hostname"], "little-mester-system-vm")
        self.assertTrue(data["ssh_keys"]["ed25519_private"].startswith("private-key"))
        files = {item["path"]: item for item in data["write_files"]}
        self.assertIn("/usr/local/bin/lm-opencode", files)
        self.assertIn("/usr/local/libexec/lm-opencode-auth", files)
        self.assertIn("/usr/local/libexec/lm-guest-user-dirs.py", files)
        self.assertIn("/etc/opencode/plugins/little-mester-exit-confirm/tui.tsx", files)
        provision = files["/usr/local/sbin/mester-v3-provision"]["content"]
        self.assertIn("docker-ce-rootless-extras", provision)
        self.assertIn("dockerd-rootless-setuptool.sh install", provision)
        self.assertIn("prepare 1000", provision)
        self.assertIn("prepare 1000", provision)
        self.assertIn("@opencode/cli@2.2.5", provision)
        self.assertIn("prepare-auth", files["/usr/local/bin/lm-opencode"]["content"])
        self.assertIn("/home/mester/workspace", provision)
        self.assertNotIn("virtiofs", provision)
        system_source = (ROOT / "lib/vm/system-vm.py").read_text()
        self.assertIn("bootstrap.build_foundation(", system_source)
        self.assertIn("contract.add_protected_session(", system_source)
        self.assertFalse(data["ssh_pwauth"])
        self.assertTrue(data["disable_root"])
        self.assertTrue(any(item["path"] == "/etc/apt/apt.conf.d/80mester-proxy"
                            for item in data["write_files"]))
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
            1000, "disk", "/usr/local/libexec/lm-guest-user-dirs.py")
        self.assertIn("guest-bootstrap.py\" smoke-cloud-init", (ROOT / "lib/vm/cloud-init.sh").read_text())
        self.assertIn("apt-get install -y docker-ce docker-ce-cli containerd.io", smoke_script)
        self.assertIn("apt-get install -y docker-ce docker-ce-cli containerd.io", system_script)
        self.assertIn("/workspace virtiofs", smoke_script)
        self.assertNotIn("virtiofs", system_script)
        self.assertIn("/home/mester/workspace", system_script)
        self.assertIn("export DOCKER_HOST=unix:///run/user/1234/docker.sock", smoke_yaml)
        self.assertIn("NO_PROXY=localhost,127.0.0.1,10.0.2.100", smoke_yaml)
        self.assertIn("disable_root: true", smoke_yaml)
        self.assertIn("docker-ce-rootless-extras", smoke_yaml)

    def test_installed_session_contract_resolves_colocated_plugin_assets(self):
        with tempfile.TemporaryDirectory(prefix="lm-session-install-layout-") as temporary:
            installed = pathlib.Path(temporary) / "libexec/little-mester-vm"
            plugin_dir = installed / "plugins"
            plugin_dir.mkdir(parents=True)
            shutil.copyfile(ROOT / "lib/vm/guest-user-dirs.py", installed / "guest-user-dirs.py")
            shutil.copyfile(ROOT / "lib/vm/guest-bootstrap.py", installed / "guest-bootstrap.py")
            shutil.copyfile(ROOT / "lib/vm/parent-session-contract.py",
                            installed / "parent-session-contract.py")
            source_plugins = ROOT / "agent/opencode/plugins/little-mester-exit-confirm"
            shutil.copyfile(source_plugins / "package.json", plugin_dir / "package.json")
            shutil.copyfile(source_plugins / "tui.tsx", plugin_dir / "tui.tsx")
            (plugin_dir / "package.json").chmod(0o644)
            (plugin_dir / "tui.tsx").chmod(0o644)
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


if __name__ == "__main__":
    unittest.main()
