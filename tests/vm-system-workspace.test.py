#!/usr/bin/env python3
"""Pure policy and argv regressions for the fixed system VM workspace export."""

import importlib.util
import os
import pathlib
import socket
import stat
import subprocess
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
        self.safe_project = self.home / "project-safe_2"
        self.safe_project.mkdir()
        self.protected = (self.root / "etc/lm-vm", self.root / "var/lib/lm-vm")
        for path in (self.home, self.repo, self.project, self.safe_project):
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
        assigned = self.validate(self.safe_project)
        assignment = self.root / "workspace.json"
        dropin = self.root / "20-workspace.conf"
        workspace.write_assignment(assignment, assigned, uid=os.getuid())
        dropin.write_bytes(workspace._systemd_bind_path(self.safe_project).encode())
        with mock.patch.object(workspace, "ASSIGNMENT", assignment), \
                mock.patch.object(workspace, "DROPIN", dropin):
            self.assertEqual(workspace.verify_host_assignment(os.getuid()), assigned)
            dropin.write_text('[Service]\nBindPaths="/tmp:/workspace"\n')
            with self.assertRaisesRegex(workspace.Refused, "disagree"):
                workspace.verify_host_assignment(os.getuid())

    def test_systemd_bind_writer_emits_only_unquoted_parser_safe_paths(self):
        safe_path = self.home / "project-with.safe_chars_2"
        self.assertEqual(workspace._systemd_bind_path(safe_path),
                         f"[Service]\nBindPaths={safe_path}:{workspace.SHARE_DIR}\n")
        with self.assertRaisesRegex(workspace.Refused, "characters unsupported"):
            workspace._systemd_bind_path(self.project)
        specifier_path = self.home / "literal%u-name"
        specifier_path.mkdir()
        with self.assertRaisesRegex(workspace.Refused, "characters unsupported"):
            workspace._systemd_bind_path(specifier_path)
        with self.assertRaisesRegex(workspace.Refused, "characters unsupported"):
            workspace._systemd_bind_path(pathlib.Path("/tmp/work:other"))

    def test_systemd_bind_writer_rejects_parser_sensitive_paths(self):
        for raw in ("/tmp/with space", "/tmp/with\ttab", "/tmp/a%b", "/tmp/a\\b",
                    '/tmp/a"b', "/tmp/a'b", "/tmp/a:b", "/tmp/a\nb", "/tmp/a\rb",
                    "/tmp/a\x00b", "/tmp/a\x1fb", "/tmp/café"):
            with self.subTest(raw=raw), self.assertRaises(workspace.Refused):
                workspace._systemd_bind_path(pathlib.Path(raw))

    def test_effective_bind_property_requires_exactly_one_intended_recursive_bind(self):
        source = self.home / "path-safe_2"
        destination = workspace.SHARE_DIR
        dropin = workspace._systemd_bind_path(source)
        self.assertEqual(dropin,
                         f"[Service]\nBindPaths={source}:{destination}\n")
        self.assertTrue(workspace.effective_bind_paths_match(
            f"{source}:{destination}:rbind", source, destination))
        self.assertFalse(workspace.effective_bind_paths_match(
            f"{source}:{destination}:{source}:{destination}:rbind", source, destination))
        self.assertFalse(workspace.effective_bind_paths_match(
            f"/wrong:{destination}:rbind", source, destination))

    def test_rollback_pair_preserves_both_files_on_assignment_conflict(self):
        assignment_path = self.root / "workspace.json"
        dropin_path = self.root / "20-workspace.conf"
        assignment_path.write_bytes(b"unexpected concurrent assignment")
        dropin_path.write_bytes(b"candidate drop-in")
        with mock.patch.object(workspace, "_atomic_root_file") as write_file, \
                mock.patch.object(workspace, "_remove_root_file_if_unchanged") as remove_file:
            with self.assertRaisesRegex(workspace.Refused, "changed during rollback"):
                workspace._restore_workspace_pair(
                    assignment_path, dropin_path, assignment=b"candidate assignment",
                    previous_assignment=None, dropin=b"candidate drop-in",
                    previous_dropin=None, control_gid=55)
        write_file.assert_not_called()
        remove_file.assert_not_called()
        self.assertEqual(assignment_path.read_bytes(), b"unexpected concurrent assignment")
        self.assertEqual(dropin_path.read_bytes(), b"candidate drop-in")

    def test_rollback_pair_preserves_both_files_on_dropin_conflict(self):
        assignment_path = self.root / "workspace.json"
        dropin_path = self.root / "20-workspace.conf"
        assignment_path.write_bytes(b"candidate assignment")
        dropin_path.write_bytes(b"unexpected concurrent drop-in")
        with mock.patch.object(workspace, "_atomic_root_file") as write_file, \
                mock.patch.object(workspace, "_remove_root_file_if_unchanged") as remove_file:
            with self.assertRaisesRegex(workspace.Refused, "changed during rollback"):
                workspace._restore_workspace_pair(
                    assignment_path, dropin_path, assignment=b"candidate assignment",
                    previous_assignment=None, dropin=b"candidate drop-in",
                    previous_dropin=None, control_gid=55)
        write_file.assert_not_called()
        remove_file.assert_not_called()
        self.assertEqual(assignment_path.read_bytes(), b"candidate assignment")
        self.assertEqual(dropin_path.read_bytes(), b"unexpected concurrent drop-in")

    def test_workspace_configuration_rejects_unrepresentable_path_before_any_atomic_write(self):
        account = SimpleNamespace(pw_uid=1234, pw_gid=1234, pw_dir=str(self.home))
        fake_lock = os.open(os.devnull, os.O_RDONLY)
        with mock.patch.object(workspace.os, "geteuid", return_value=0), \
                mock.patch.object(workspace.os, "fstat", return_value=SimpleNamespace(
                    st_mode=stat.S_IFREG | 0o600, st_uid=0, st_nlink=1)), \
                mock.patch.object(workspace.pwd, "getpwuid", return_value=account), \
                mock.patch.object(workspace.grp, "getgrnam", return_value=SimpleNamespace(gr_gid=55)), \
                mock.patch.object(workspace.subprocess, "run",
                                  return_value=SimpleNamespace(returncode=0, stdout="inactive\n")), \
                mock.patch.object(workspace, "validate_workspace",
                                  return_value=workspace.Workspace(
                                      pathlib.Path("/tmp/unrepresentable workspace"), (1, 2), 1234)), \
                mock.patch.object(workspace, "read_assignment",
                                  side_effect=workspace.Refused("workspace assignment is missing or malformed")), \
                mock.patch.object(workspace, "_atomic_root_file") as write_file, \
                mock.patch.object(workspace.os, "open", return_value=fake_lock):
            with self.assertRaisesRegex(workspace.Refused, "characters unsupported"):
                workspace.configure("ignored", self.repo, owner_uid=1234,
                                    unsafe_recurse=False, reassign=False)
        write_file.assert_not_called()

    def test_dropin_publication_failure_rolls_back_new_assignment(self):
        account = SimpleNamespace(pw_uid=1234, pw_gid=1234, pw_dir=str(self.home))
        assigned = workspace.Workspace(self.safe_project, (1, 2), 1234)
        assignment_path = self.root / "workspace.json"
        dropin_path = self.root / "20-workspace.conf"
        fake_lock = os.open(os.devnull, os.O_RDONLY)
        published = {}
        original_open = os.open

        def open_lock(path, flags, *args, **kwargs):
            if str(path) == "/run/little-mester-vm-workspace-config.lock":
                return fake_lock
            return original_open(path, flags, *args, **kwargs)

        def atomic_write(path, payload, **kwargs):
            if pathlib.Path(path) == assignment_path:
                pathlib.Path(path).write_bytes(payload)
                return
            pathlib.Path(path).write_bytes(payload)
            raise OSError("injected drop-in publication failure")

        original_lstat = os.lstat
        original_stat = os.stat
        original_fstat = os.fstat
        fstat_calls = 0

        def lstat_as_root(path, *args, **kwargs):
            path = pathlib.Path(path)
            if path == assignment_path and assignment_path.exists():
                return SimpleNamespace(st_mode=stat.S_IFREG | 0o640, st_uid=0,
                                       st_gid=55, st_nlink=1)
            if path == dropin_path and dropin_path.exists():
                return SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0,
                                       st_gid=0, st_nlink=1)
            return original_lstat(path, *args, **kwargs)

        def stat_as_root(path, *args, **kwargs):
            path = pathlib.Path(path)
            if path == pathlib.Path(assignment_path.name) and kwargs.get("dir_fd") is not None:
                actual = original_stat(path, dir_fd=kwargs.get("dir_fd"))
                return SimpleNamespace(st_mode=stat.S_IFREG | 0o640, st_uid=0,
                                       st_gid=55, st_nlink=1, st_dev=actual.st_dev,
                                       st_ino=actual.st_ino)
            if path == pathlib.Path(dropin_path.name) and kwargs.get("dir_fd") is not None:
                actual = original_stat(path, dir_fd=kwargs.get("dir_fd"))
                return SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0,
                                       st_gid=0, st_nlink=1, st_dev=actual.st_dev,
                                       st_ino=actual.st_ino)
            return original_stat(path, *args, **kwargs)

        def fstat_with_root_lock(fd):
            nonlocal fstat_calls
            fstat_calls += 1
            if fstat_calls == 1:
                return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_nlink=1)
            return original_fstat(fd)

        with mock.patch.object(workspace.os, "geteuid", return_value=0), \
                mock.patch.object(workspace.os, "open", side_effect=open_lock), \
                mock.patch.object(workspace.os, "fstat", side_effect=fstat_with_root_lock), \
                mock.patch.object(workspace.pwd, "getpwuid", return_value=account), \
                mock.patch.object(workspace.grp, "getgrnam", return_value=SimpleNamespace(gr_gid=55)), \
                mock.patch.object(workspace.subprocess, "run", side_effect=lambda args, **kwargs:
                                  SimpleNamespace(returncode=0, stdout="inactive\n")), \
                mock.patch.object(workspace, "_mountpoints", return_value=()), \
                mock.patch.object(workspace, "validate_workspace", return_value=assigned), \
                mock.patch.object(workspace, "read_assignment",
                                  side_effect=workspace.Refused("workspace assignment is missing or malformed")), \
                mock.patch.object(workspace.os, "lstat", side_effect=lstat_as_root), \
                mock.patch.object(workspace.os, "stat", side_effect=stat_as_root), \
                mock.patch.object(workspace, "_atomic_root_file", side_effect=atomic_write) as write_file, \
                mock.patch.object(workspace, "ASSIGNMENT", assignment_path), \
                mock.patch.object(workspace, "DROPIN", dropin_path):
            with self.assertRaisesRegex(OSError, "injected drop-in"):
                workspace.configure(str(self.safe_project), self.repo, owner_uid=1234,
                                    unsafe_recurse=False, reassign=False)

        self.assertEqual(write_file.call_count, 2)
        self.assertFalse(assignment_path.exists(), "failed drop-in write must remove new assignment")
        self.assertFalse(dropin_path.exists(), "partially published drop-in must be removed")

    def test_daemon_reload_failure_restores_existing_assignment_and_dropin(self):
        account = SimpleNamespace(pw_uid=1234, pw_gid=1234, pw_dir=str(self.home))
        previous = workspace.Workspace(self.home / "previous", (3, 4), 1234)
        assigned = workspace.Workspace(self.safe_project, (1, 2), 1234)
        assignment_path = self.root / "workspace.json"
        dropin_path = self.root / "20-workspace.conf"
        old_assignment = b"previous assignment\n"
        old_dropin = b"previous drop-in\n"
        assignment_path.write_bytes(old_assignment)
        dropin_path.write_bytes(old_dropin)
        fake_lock = os.open(os.devnull, os.O_RDONLY)
        original_open, original_lstat, original_fstat = os.open, os.lstat, os.fstat
        writes = []
        systemd_calls = 0

        def open_lock(path, flags, *args, **kwargs):
            if str(path) == "/run/little-mester-vm-workspace-config.lock":
                return fake_lock
            return original_open(path, flags, *args, **kwargs)

        def lstat_root_dropin(path, *args, **kwargs):
            if pathlib.Path(path) == dropin_path:
                actual = original_lstat(path, *args, **kwargs)
                return SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0,
                                       st_gid=0, st_nlink=1, st_dev=actual.st_dev,
                                       st_ino=actual.st_ino)
            return original_lstat(path, *args, **kwargs)

        def fstat_root_lock(fd):
            if fd == fake_lock:
                return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_nlink=1)
            return original_fstat(fd)

        def atomic_write(path, payload, **kwargs):
            pathlib.Path(path).write_bytes(payload)
            writes.append((pathlib.Path(path), payload))

        def systemd_run(args, **kwargs):
            nonlocal systemd_calls
            systemd_calls += 1
            if args[-1:] == ["daemon-reload"] and systemd_calls == 5:
                raise subprocess.CalledProcessError(1, args)
            return SimpleNamespace(returncode=0, stdout="inactive\n")

        with mock.patch.object(workspace.os, "geteuid", return_value=0), \
                mock.patch.object(workspace.os, "open", side_effect=open_lock), \
                mock.patch.object(workspace.os, "fstat", side_effect=fstat_root_lock), \
                mock.patch.object(workspace.os, "lstat", side_effect=lstat_root_dropin), \
                mock.patch.object(workspace.pwd, "getpwuid", return_value=account), \
                mock.patch.object(workspace.grp, "getgrnam", return_value=SimpleNamespace(gr_gid=55)), \
                mock.patch.object(workspace.subprocess, "run", side_effect=systemd_run), \
                mock.patch.object(workspace, "_mountpoints", return_value=()), \
                mock.patch.object(workspace, "validate_workspace", return_value=assigned), \
                mock.patch.object(workspace, "read_assignment", return_value=previous), \
                mock.patch.object(workspace, "_atomic_root_file", side_effect=atomic_write), \
                mock.patch.object(workspace, "ASSIGNMENT", assignment_path), \
                mock.patch.object(workspace, "DROPIN", dropin_path):
            with self.assertRaises(subprocess.CalledProcessError):
                workspace.configure(str(self.safe_project), self.repo, owner_uid=1234,
                                    unsafe_recurse=False, reassign=True)

        self.assertEqual(assignment_path.read_bytes(), old_assignment)
        self.assertEqual(dropin_path.read_bytes(), old_dropin)
        self.assertEqual([payload for _path, payload in writes[-2:]],
                         [old_dropin, old_assignment])
        self.assertEqual(systemd_calls, 6, "rollback must reload the restored unit files")


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
    def test_exact_bind_identity_accepts_only_assignment_inode_at_mount_target(self):
        identity = (123, 456)
        target = pathlib.Path("/run/little-mester-test-share")
        assigned = workspace.Workspace(pathlib.Path("/tmp/approved"), identity, os.getuid())
        present = {"state": "present", "kind": "directory",
                   "device": identity[0], "inode": identity[1]}
        with mock.patch.object(workspace, "_path_identity", return_value=present), \
                mock.patch.object(workspace, "_mount_context",
                                  return_value={"is_exact_mountpoint": True}):
            workspace._verify_bind_identity(assigned, target, True)

        hidden = {"state": "unavailable", "errno": workspace.errno.ENOENT,
                  "error": "ENOENT"}
        home_assigned = workspace.Workspace(pathlib.Path("/home/runner/approved"),
                                            identity, os.getuid())
        with mock.patch.object(workspace, "_path_identity", side_effect=[hidden, present]), \
                mock.patch.object(workspace, "_mount_context", side_effect=[
                    {"filesystem": "tmpfs"}, {"is_exact_mountpoint": True}]):
            workspace._verify_bind_identity(home_assigned, target, True)

        replaced = {**present, "inode": identity[1] + 1}
        with mock.patch.object(workspace, "_path_identity", side_effect=[replaced, present]), \
                mock.patch.object(workspace, "_mount_context",
                                  return_value={"is_exact_mountpoint": True}), \
                mock.patch.object(workspace, "_log_workspace_bind_diagnostic"):
            with self.assertRaisesRegex(workspace.Refused, "source changed"):
                workspace._verify_bind_identity(assigned, target, True)

        missing = {"state": "unavailable", "errno": workspace.errno.ENOENT,
                   "error": "ENOENT"}
        with mock.patch.object(workspace, "_path_identity", side_effect=[missing, present]), \
                mock.patch.object(workspace, "_mount_context", side_effect=[{}, {
                    "is_exact_mountpoint": True}]), \
                mock.patch.object(workspace, "_log_workspace_bind_diagnostic"):
            with self.assertRaisesRegex(workspace.Refused, "missing or inaccessible"):
                workspace._verify_bind_identity(assigned, target, True)

    def test_bind_failure_diagnostic_distinguishes_protected_source_and_wrong_target(self):
        identity = (123, 456)
        assigned = workspace.Workspace(pathlib.Path("/home/runner/workspace"),
                                       identity, os.getuid())
        hidden = {"state": "unavailable", "errno": workspace.errno.ENOENT,
                  "error": "ENOENT"}
        target = pathlib.Path("/run/little-mester-test-share")
        present = {"state": "present", "kind": "directory",
                   "device": identity[0], "inode": identity[1]}
        with mock.patch.object(workspace, "_path_identity", side_effect=[hidden, present]), \
                mock.patch.object(workspace, "_mount_context", side_effect=[
                    {"mount_id": 9, "filesystem": "tmpfs"},
                    {"mount_id": 10, "filesystem": "ext4", "is_exact_mountpoint": True}]):
            diagnostic = workspace._workspace_bind_diagnostic(assigned, target, True)
        self.assertEqual(diagnostic["source_visibility"], "hidden-by-ProtectHome-tmpfs")
        self.assertEqual(diagnostic["diagnosis"], "approved-bind-present")

        mismatch = {**present, "inode": identity[1] + 1}
        with mock.patch.object(workspace, "_path_identity", side_effect=[present, mismatch]), \
                mock.patch.object(workspace, "_mount_context", side_effect=[{}, {
                    "mount_id": 10, "filesystem": "ext4", "is_exact_mountpoint": True}]):
            diagnostic = workspace._workspace_bind_diagnostic(assigned, target, True)
        self.assertEqual(diagnostic["diagnosis"], "bind-target-identity-mismatch")

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
        self.assertIn("ExecStart=+/usr/bin/python3 -I /usr/lib/little-mester-vm/system-vm.py locked-service workspace --owner=@LM_VM_OWNER@", unit)
        self.assertIn("code-update.lock", unit)
        helper = (ROOT / "lib/vm/system-vm-workspace.py").read_text()
        self.assertIn('"--shared-dir", str(SHARE_DIR)', helper)
        self.assertIn('"--sandbox", "namespace"', helper)
        self.assertIn('"/usr/libexec/virtiofsd"', helper)
        self.assertIn("build_virtiofsd_argv", helper)
        self.assertIn("verify_service_workspace()", helper)
        self.assertNotIn("--announce-submounts", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("ProtectHome=tmpfs", unit)
        self.assertNotIn("ProtectHome=yes", unit)
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
        self.assertIn("locked-service metrics", metrics)
        self.assertIn('"system-vm-metrics.py"',
                      (ROOT / "lib/vm/system-vm.py").read_text())
        self.assertIn("code-update.lock", metrics)
        self.assertIn("RestrictAddressFamilies=AF_INET", metrics)
        self.assertIn("ProtectSystem=strict", metrics)
        self.assertIn("DevicePolicy=closed", metrics)
        self.assertIn("DeviceAllow=/dev/nvidia0 rw", metrics)
        self.assertNotIn("PrivateDevices=yes", metrics)


if __name__ == "__main__":
    unittest.main()
