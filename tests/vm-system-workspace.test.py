#!/usr/bin/env python3
"""Pure policy and argv regressions for the fixed system VM workspace export."""

import importlib.util
import os
import pathlib
import socket
import stat
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


workspace = load("system_vm_workspace", ROOT / "lib/vm/system-vm-workspace.py")
qemu = load("parent_qemu_workspace", ROOT / "lib/vm/parent-qemu.py")


class WorkspaceValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.repo = self.home / "little-mester"
        self.repo.mkdir()
        self.project = self.home / "project with spaces"
        self.project.mkdir()
        self.protected = (self.root / "etc/lm-vm", self.root / "var/lib/lm-vm")
        for path in (self.home, self.repo, self.project):
            path.chmod(0o755)

    def validate(self, path, **kwargs):
        options = {"mountpoints": (), **kwargs}
        return workspace.validate_workspace(
            str(path), uid=os.getuid(), home=self.home, repository=self.repo,
            protected_roots=self.protected, **options)

    def test_canonicalizes_symlink_argument_and_accepts_spaces(self):
        link = self.home / "project-link"
        link.symlink_to(self.project, target_is_directory=True)
        result = self.validate(link)
        self.assertEqual(result.path, self.project.resolve())
        self.assertEqual(result.identity, (self.project.stat().st_dev, self.project.stat().st_ino))

    def test_refuses_repository_without_explicit_unsafe_recurse(self):
        with self.assertRaisesRegex(workspace.Refused, "unsafe-recurse"):
            self.validate(self.repo)

    def test_unsafe_recurse_allows_repository_or_child_but_not_repository_parent(self):
        child = self.repo / "src"
        child.mkdir()
        child.chmod(0o755)
        self.assertEqual(self.validate(self.repo, unsafe_recurse=True).path, self.repo)
        self.assertEqual(self.validate(child, unsafe_recurse=True).path, child)
        with self.assertRaises(workspace.Refused):
            self.validate(self.home, unsafe_recurse=True)

    def test_refuses_root_home_and_protected_state(self):
        for path in (pathlib.Path("/"), self.home, self.protected[0], self.protected[1]):
            with self.subTest(path=path), self.assertRaises(workspace.Refused):
                self.validate(path)

    def test_refuses_nested_mount_boundary(self):
        nested = self.project / "mounted"
        nested.mkdir()
        with self.assertRaisesRegex(workspace.Refused, "mount"):
            self.validate(self.project, mountpoints=(nested,))

    def test_rechecks_identity_before_start(self):
        checked = self.validate(self.project)
        replacement = self.home / "replacement"
        replacement.mkdir()
        self.project.rmdir()
        self.project.symlink_to(replacement, target_is_directory=True)
        with self.assertRaisesRegex(workspace.Refused, "changed"):
            workspace.verify_identity(checked)

    def test_assignment_is_strict_and_atomic(self):
        checked = self.validate(self.project)
        assignment_path = self.root / "assignment.json"
        workspace.write_assignment(assignment_path, checked, uid=os.getuid())
        self.assertEqual(workspace.read_assignment(assignment_path, uid=os.getuid()), checked)
        assignment_path.write_text('{"version":1,"path":"/tmp","extra":true}\n')
        with self.assertRaises(workspace.Refused):
            workspace.read_assignment(assignment_path, uid=os.getuid())

    def test_assignment_envelope_can_be_checked_without_host_path_visibility(self):
        checked = self.validate(self.project)
        assignment_path = self.root / "assignment.json"
        workspace.write_assignment(assignment_path, checked, uid=os.getuid())
        self.project.rename(self.home / "renamed-while-sandboxed")
        with self.assertRaises(workspace.Refused):
            workspace.read_assignment(assignment_path, uid=os.getuid())
        # The workspace service validates the visible /workspace mount against
        # this envelope instead of reopening the hidden original home path.
        self.assertEqual(workspace.read_assignment(
            assignment_path, uid=os.getuid(), verify_path=False), checked)

    def test_assignment_refuses_wrong_owner_or_permissions(self):
        checked = self.validate(self.project)
        path = self.root / "assignment.json"
        workspace.write_assignment(path, checked, uid=os.getuid())
        path.chmod(0o644)
        with self.assertRaises(workspace.Refused):
            workspace.read_assignment(path, uid=os.getuid())

    def test_vm_host_preflight_requires_assignment_and_exact_generated_dropin(self):
        assigned = self.validate(self.project)
        assignment = self.root / "workspace.json"
        dropin = self.root / "20-workspace.conf"
        workspace.write_assignment(assignment, assigned, uid=os.getuid())
        dropin.write_bytes(workspace._systemd_bind_path(self.project).encode())
        with mock.patch.object(workspace, "ASSIGNMENT", assignment), \
                mock.patch.object(workspace, "DROPIN", dropin):
            self.assertEqual(workspace.verify_host_assignment(os.getuid()), assigned)
            dropin.write_text('[Service]\nBindPaths="/tmp:/workspace"\n')
            with self.assertRaisesRegex(workspace.Refused, "disagree"):
                workspace.verify_host_assignment(os.getuid())

    def test_systemd_bind_mount_quoting_preserves_spaces_and_rejects_ambiguity(self):
        rendered = workspace._systemd_bind_path(self.project)
        self.assertEqual(rendered, f'[Service]\nBindPaths="{self.project}:{workspace.SHARE_DIR}"\n')
        specifier_path = self.home / "literal%u-name"
        specifier_path.mkdir()
        self.assertEqual(workspace._systemd_bind_path(specifier_path),
                         f'[Service]\nBindPaths="{specifier_path.as_posix().replace("%", "%%")}:{workspace.SHARE_DIR}"\n')
        with self.assertRaisesRegex(workspace.Refused, "unsafe for systemd"):
            workspace._systemd_bind_path(pathlib.Path("/tmp/work:other"))


class QemuWorkspaceArgvTests(unittest.TestCase):
    def test_system_qemu_requires_exact_vhost_user_socket(self):
        args = qemu.build_system_qemu_argv(
            executable="/usr/bin/qemu-system-x86_64", disk_path="/var/lib/lm/disk.qcow2",
            seed_iso="/var/lib/lm/seed.iso", ssh_port=22222, proxy_port=32128,
            qmp_socket="/run/lm-vm/qmp.sock", vm_name="little-mester-system-vm",
            virtiofs_socket="/run/lm-vm-workspace/virtiofs.sock")
        self.assertIn("-chardev", args)
        self.assertIn("memory-backend-memfd,id=workspace-mem,size=4096M,share=on", args)
        index = args.index("-chardev")
        self.assertEqual(args[index + 1],
                         "socket,id=workspace,path=/run/lm-vm-workspace/virtiofs.sock")
        self.assertEqual(args[args.index("-device", index) + 1],
                         "vhost-user-fs-pci,chardev=workspace,tag=workspace")

    def test_system_qemu_rejects_unsafe_socket_paths(self):
        for path in ("relative.sock", "/tmp/a,b.sock", "/tmp/a\nsock"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                qemu.build_system_qemu_argv(
                    executable="/usr/bin/qemu-system-x86_64", disk_path="/var/lib/lm/disk.qcow2",
                    seed_iso="/var/lib/lm/seed.iso", ssh_port=22222, proxy_port=32128,
                    qmp_socket="/run/lm-vm/qmp.sock", vm_name="little-mester-system-vm",
                    virtiofs_socket=path)


class ServiceBoundaryTests(unittest.TestCase):
    def test_post_start_normalizes_socket_metadata_as_owner_without_root_privileges(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = pathlib.Path(temp)
            socket_path = runtime / "virtiofs.sock"
            with socket.socket(socket.AF_UNIX) as sock:
                sock.bind(str(socket_path))
            socket_path.chmod(0o770)
            runtime.chmod(0o750)
            owner_uid, control_gid = os.getuid(), os.getgid()
            owner = SimpleNamespace(pw_uid=owner_uid, pw_gid=control_gid)
            with mock.patch.object(workspace, "SOCKET", socket_path), \
                    mock.patch.object(workspace, "SOCKET_DIR", runtime), \
                    mock.patch.object(workspace, "assignment_owner_uid", return_value=owner_uid), \
                    mock.patch.object(workspace, "read_assignment"), \
                    mock.patch.object(workspace.pwd, "getpwuid", return_value=owner), \
                    mock.patch.object(workspace.grp, "getgrnam",
                                      return_value=SimpleNamespace(gr_gid=control_gid)), \
                    mock.patch.object(workspace.os, "geteuid", return_value=owner_uid), \
                    mock.patch.object(workspace.os, "chown", wraps=os.chown) as chown:
                workspace.fix_socket_permissions()
            chown.assert_called_once_with(runtime, owner_uid, control_gid,
                                          follow_symlinks=False)
            info = socket_path.lstat()
            self.assertTrue(stat.S_ISSOCK(info.st_mode))
            self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)),
                             (owner_uid, control_gid, 0o660))

    def test_virtiofsd_fixed_host_identity_mapping(self):
        argv = workspace.build_virtiofsd_argv("/usr/libexec/virtiofsd", uid=1001, gid=1002)
        self.assertIn("--uid-map=:1000:1001:1:", argv)
        self.assertIn("--gid-map=:1000:1002:1:", argv)
        self.assertEqual(argv[argv.index("--socket-group") + 1], "lm-vm-control")
        self.assertIn("--no-announce-submounts", argv)
        with self.assertRaises(workspace.Refused):
            workspace.build_virtiofsd_argv("/tmp/virtiofsd", uid=1001, gid=1002)

    def test_nested_mounts_are_rejected_in_the_export_namespace(self):
        workspace.reject_nested_mounts((pathlib.Path("/"), workspace.SHARE_DIR))
        with self.assertRaisesRegex(workspace.Refused, "nested mount"):
            workspace.reject_nested_mounts((workspace.SHARE_DIR,
                                            workspace.SHARE_DIR / "foreign-mount"))

    def test_qemu_is_ordered_after_and_bound_to_owner_scoped_share(self):
        unit = (ROOT / "systemd/little-mester-vm.service").read_text()
        self.assertIn("little-mester-vm-workspace.service", unit.split("Requires=", 1)[1].splitlines()[0])
        self.assertIn("little-mester-vm-workspace.service", unit.split("BindsTo=", 1)[1].splitlines()[0])
        self.assertIn("little-mester-vm-workspace.service", unit.split("After=", 1)[1].splitlines()[0])
        self.assertIn("little-mester-vm-metrics.service", unit.split("Requires=", 1)[1].splitlines()[0])
        self.assertIn("ExecStartPre=/usr/bin/python3 -I /usr/lib/little-mester-vm/system-vm.py check", unit)

    def test_share_service_is_owner_scoped_and_path_limited(self):
        unit_path = ROOT / "systemd/little-mester-vm-workspace.service"
        self.assertTrue(unit_path.is_file())
        unit = unit_path.read_text()
        self.assertIn("Type=exec", unit)
        self.assertNotIn("ExecStartPre=", unit)
        self.assertIn("ExecStart=/usr/bin/python3 -I /usr/lib/little-mester-vm/system-vm-workspace.py serve", unit)
        helper = (ROOT / "lib/vm/system-vm-workspace.py").read_text()
        self.assertIn('"--shared-dir", str(SHARE_DIR)', helper)
        self.assertIn('"--sandbox", "namespace"', helper)
        self.assertIn('"/usr/libexec/virtiofsd"', helper)
        self.assertIn("build_virtiofsd_argv", helper)
        self.assertIn("verify_service_workspace()", helper)
        self.assertNotIn("--announce-submounts", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("ProtectHome=yes", unit)
        self.assertIn("NoNewPrivileges=no", unit)
        self.assertIn("NoExecPaths=/", unit)
        self.assertIn("ExecPaths=/usr/bin /usr/lib -/usr/lib64 -/usr/libexec /lib -/lib64", unit)
        for forced_nnp in ("PrivateDevices=", "ProtectKernelTunables=",
                           "ProtectKernelModules=", "RestrictSUIDSGID=",
                           "LockPersonality=", "RestrictAddressFamilies="):
            self.assertNotIn(forced_nnp, unit)
        self.assertIn("RuntimeDirectory=little-mester-vm-workspace little-mester-vm-workspace-share", unit)
        self.assertIn("ReadWritePaths=/run/little-mester-vm-workspace-share", unit)
        self.assertIn("ExecStartPost=/usr/bin/python3 -I /usr/lib/little-mester-vm/system-vm-workspace.py socket-permissions", unit)
        self.assertNotIn("ExecStartPost=+", unit)
        self.assertIn('"--socket-group", "lm-vm-control"', helper)
        self.assertIn("stat.S_IMODE(info.st_mode) != 0o660", helper)
        self.assertNotIn("os.chown(SOCKET,", helper)
        self.assertIn("os.chown(SOCKET_DIR", helper)
        self.assertIn("os.chmod(SOCKET, 0o660, follow_symlinks=False)", helper)

    def test_systemd_host_bind_target_is_created_runtime_path_not_guest_mountpoint(self):
        unit = (ROOT / "systemd/little-mester-vm-workspace.service").read_text()
        dropin = workspace._systemd_bind_path(pathlib.Path("/selected/disposable-workspace"))
        self.assertIn(str(workspace.SHARE_DIR), dropin)
        self.assertNotIn(":/workspace\n", dropin)
        self.assertNotIn("/workspace", unit)
        self.assertIn("RuntimeDirectoryMode=0750", unit)

    def test_metrics_endpoint_is_loopback_only_and_get_only(self):
        source = (ROOT / "lib/vm/system-vm-metrics.py").read_text()
        self.assertIn('HOST = "127.0.0.1"', source)
        self.assertIn("PORT = 32129", source)
        self.assertIn('parsed.path == "/metrics"', source)
        self.assertIn('parsed.path == "/history"', source)
        self.assertIn("def do_POST", source)
        self.assertIn('self._respond(405, {"error": "method_not_allowed"})', source)
    def test_metrics_sidecar_is_required_and_lifecycle_bound(self):
        unit = (ROOT / "systemd/little-mester-vm.service").read_text()
        for relationship in ("Requires=", "BindsTo=", "After="):
            self.assertIn("little-mester-vm-metrics.service",
                          unit.split(relationship, 1)[1].splitlines()[0])
        metrics = (ROOT / "systemd/little-mester-vm-metrics.service").read_text()
        self.assertIn("User=lm-vm", metrics)
        self.assertIn("NoNewPrivileges=yes", metrics)
        self.assertIn("system-vm-metrics.py", metrics)
        self.assertIn("RestrictAddressFamilies=AF_INET", metrics)
        self.assertIn("ProtectSystem=strict", metrics)
        self.assertIn("DevicePolicy=closed", metrics)
        self.assertIn("DeviceAllow=/dev/nvidia0 rw", metrics)
        self.assertNotIn("PrivateDevices=yes", metrics)


if __name__ == "__main__":
    unittest.main()
