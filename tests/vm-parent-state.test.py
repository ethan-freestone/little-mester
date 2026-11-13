#!/usr/bin/env python3
"""Unprivileged REAL_PROCESS tests for persistent-parent state primitives."""

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULE = ROOT / "lib/vm/parent-state.py"
SPEC = importlib.util.spec_from_file_location("parent_state", MODULE)
parent_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(parent_state)


class ParentStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.temp.name) / "home"
        self.home.mkdir(mode=0o700)
        os.chmod(self.home, 0o700)

    def tearDown(self):
        self.temp.cleanup()

    def parent_dir(self):
        return self.home / ".local/state/little-mester/parent-v3/default"

    def write_stale_running_fixture(self):
        parent = parent_state.initialize(home=self.home)
        directory = self.parent_dir()
        disk_path = directory / "disk.qcow2"
        disk_path.write_bytes(b"retained-disk-fixture")
        disk_path.chmod(0o600)
        disk_info = disk_path.stat()
        parent["disk"].update({"provisioned": True, "device": disk_info.st_dev,
                               "inode": disk_info.st_ino})
        run_id = "0123456789abcdef0123456789abcdef"
        old_boot = "00000000-0000-0000-0000-000000000000"
        executable = os.path.realpath(sys.executable)
        supervisor_path = "/opt/little-mester/v3/" + "a" * 40 + "/lib/vm/parent-supervisor.py"
        supervisor_argv = [executable, "-I", supervisor_path, "--parent-id", "default",
                           "--run-id", run_id, "--lock-fd", "3", "--handoff-fd", "4",
                           "--state-home", str(self.home)]
        supervisor = {"schema": 1, "pid": 1234567, "uid": os.getuid(),
                      "host_boot_id": old_boot, "start_time": "123", "exe": executable,
                      "argv": supervisor_argv}
        fd = parent_state._open_directory_chain(create=False, home_override=self.home)
        try:
            parent_state._atomic_json(fd, "parent.json", parent)
            transitions = {}
            for role, token in (("qemu", "1"), ("proxy", "2")):
                target_path = f"child-{role}.target.json"
                identity_path = f"child-{role}.identity.json"
                if role == "qemu":
                    target_exe = "/usr/bin/qemu-system-x86_64"
                    target_argv = [target_exe, "30"]
                else:
                    target_exe = executable
                    proxy_script = str((ROOT / "lib/vm/proxy.py").resolve())
                    target_argv = [target_exe, "-I", proxy_script, "--port-file",
                                   str(self.parent_dir() / "proxy.port")]
                contracts = ([{"fd": 3, "purpose": "parent-disk", "device": disk_info.st_dev,
                               "inode": disk_info.st_ino, "access": "read-write", "fdset": 1}]
                             if role == "qemu" else [])
                gate_argv = [executable, "-I", str((ROOT / "lib/vm/parent-gate.py").resolve()),
                             "--release-fd", "10", "--token", token * 32,
                             "--target", target_path, "--identity", identity_path,
                             "--state-dir-fd", "20", "--disk-fd", "30" if contracts else "-1"]
                gate_identity = {"schema": 1, "pid": 1234568 if role == "qemu" else 1234569,
                                 "uid": os.getuid(), "host_boot_id": old_boot,
                                 "start_time": "124", "exe": executable, "argv": gate_argv}
                transition = parent_state.create_exec_transition(
                    gate_identity, target_exe, target_argv, release_token=token * 32,
                    target_path=target_path, identity_path=identity_path, fd_contracts=contracts)
                transition["host_boot_id"] = old_boot
                parent_state._atomic_json(fd, target_path,
                    {"executable": target_exe, "argv": target_argv, "fd_contracts": contracts})
                parent_state._atomic_json(fd, identity_path, transition)
                transitions[role] = transition
            qemu = {"schema": 1, "pid": transitions["qemu"]["pid"], "uid": os.getuid(),
                    "host_boot_id": old_boot, "start_time": transitions["qemu"]["start_time"],
                    "exe": transitions["qemu"]["target"]["exe"],
                    "argv": transitions["qemu"]["target"]["argv"]}
            run = {"schema": 1, "parent_id": "default", "run_id": run_id,
                   "phase": "RUNNING", "supervisor": supervisor, "qemu": qemu}
            parent_state._atomic_json(fd, "active-run.json", run)
            # Existing recovery artifacts are private and validated before cleanup.
            (directory / "proxy.port").write_text("12345\n")
            (directory / "proxy.port").chmod(0o600)
        finally:
            os.close(fd)
        return run, disk_info, disk_path

    def stale_reconcile_patches(self):
        return (mock.patch.object(parent_state, "_disk_status", side_effect=lambda _fd, p: {
                    **p["disk"], "host_allocated_bytes": 4096, "file_bytes": 22}),
                mock.patch.object(parent_state, "_trusted_parent_runtime_script", return_value=True),
                mock.patch.object(parent_state, "_scan_stale_disk_holders"))

    def test_stale_boot_reconciliation_preview_and_execute_preserve_history_and_disk(self):
        run, _disk_info, disk_path = self.write_stale_running_fixture()
        before = disk_path.read_bytes()
        active_before = (self.parent_dir() / "active-run.json").read_bytes()
        with self.stale_reconcile_patches()[0], self.stale_reconcile_patches()[1], \
                self.stale_reconcile_patches()[2] as scan:
            preview = parent_state.stale_boot_reconciliation(home=self.home)
            self.assertEqual((preview["action"], preview["status"]), ("reconcile", "RECONCILABLE"))
            self.assertTrue(preview["dry_run"])
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")
            self.assertEqual((self.parent_dir() / "active-run.json").read_bytes(), active_before)
            self.assertFalse((self.parent_dir() / parent_state._stale_reconciliation_filename(
                run["run_id"])).exists())
            result = parent_state.stale_boot_reconciliation(home=self.home, execute=True)
            self.assertEqual(result["status"], "STOPPED")
            self.assertTrue(result["disk_retained"])
            scan.assert_called()
        self.assertEqual(disk_path.read_bytes(), before)
        audit_path = self.parent_dir() / parent_state._stale_reconciliation_filename(run["run_id"])
        audited = json.loads(audit_path.read_text())
        self.assertEqual(audited["historical_run"], run)
        with self.stale_reconcile_patches()[0], self.stale_reconcile_patches()[1]:
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "STOPPED")
            fd = parent_state._open_directory_chain(create=False, home_override=self.home)
            try:
                parent_state._validated_recovery_plan(fd, home=self.home)
            finally:
                os.close(fd)
            self.assertEqual(parent_state.recovery_preview(home=self.home)["status"], "STOPPED")

    def test_stale_boot_reconciliation_audit_tampering_remains_ambiguous(self):
        self.write_stale_running_fixture()
        with self.stale_reconcile_patches()[0], self.stale_reconcile_patches()[1], \
                self.stale_reconcile_patches()[2]:
            parent_state.stale_boot_reconciliation(home=self.home, execute=True)
        audit_path = self.parent_dir() / parent_state._stale_reconciliation_filename(
            "0123456789abcdef0123456789abcdef")
        audit = json.loads(audit_path.read_text())
        audit["operator_uid"] = os.getuid() + 1
        audit_path.write_text(json.dumps(audit) + "\n")
        audit_path.chmod(0o600)
        with self.stale_reconcile_patches()[0], self.stale_reconcile_patches()[1]:
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")

    def test_stale_boot_reconciliation_retries_after_interrupted_cleanup(self):
        _run, _disk_info, _disk_path = self.write_stale_running_fixture()
        cleanup = mock.patch.object(parent_state, "_recovery_remove_artifact",
                                    side_effect=parent_state.Refused("simulated interruption"))
        with self.stale_reconcile_patches()[0], self.stale_reconcile_patches()[1], \
                self.stale_reconcile_patches()[2], cleanup:
            with self.assertRaisesRegex(parent_state.Refused, "simulated interruption"):
                parent_state.stale_boot_reconciliation(home=self.home, execute=True)
        self.assertTrue((self.parent_dir() / parent_state._stale_reconciliation_filename(
            "0123456789abcdef0123456789abcdef")).exists())
        with self.stale_reconcile_patches()[0], self.stale_reconcile_patches()[1], \
                self.stale_reconcile_patches()[2]:
            retried = parent_state.stale_boot_reconciliation(home=self.home, execute=True)
            self.assertEqual(retried["status"], "STOPPED")

    def test_stale_boot_audit_is_scoped_to_its_run_id(self):
        run, _disk_info, _disk_path = self.write_stale_running_fixture()
        with self.stale_reconcile_patches()[0], self.stale_reconcile_patches()[1], \
                self.stale_reconcile_patches()[2]:
            parent_state.stale_boot_reconciliation(home=self.home, execute=True)
        new_run = {"schema": 1, "parent_id": "default", "run_id": "f" * 32,
                   "phase": "STOPPING", "supervisor": run["supervisor"]}
        fd = parent_state._open_directory_chain(create=False, home_override=self.home)
        try:
            parent = parent_state._read_json(fd, "parent.json")
            with self.stale_reconcile_patches()[0]:
                disk = parent_state._disk_status(fd, parent)
                self.assertIsNone(parent_state._valid_stale_reconciliation(
                    fd, parent, new_run, disk))
        finally:
            os.close(fd)
        current_boot = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        new_supervisor = dict(run["supervisor"], pid=2**31 - 1,
                              host_boot_id=current_boot)
        new_supervisor["argv"] = list(new_supervisor["argv"])
        new_supervisor["argv"][6] = "f" * 32
        new_run["supervisor"] = new_supervisor
        with self.stale_reconcile_patches()[0], self.stale_reconcile_patches()[1]:
            projected = parent_state.status_for_run(
                parent, new_run, disk, home=self.home)
        self.assertEqual(projected["status"], "STOPPED")

    def test_stale_disk_scan_requires_positive_control_and_runs_two_inode_scans(self):
        from types import SimpleNamespace

        with tempfile.TemporaryFile() as disk:
            info = os.fstat(disk.fileno())
            scan = mock.Mock(return_value=[os.getpid()])
            scanner = SimpleNamespace(
                GateRefused=RuntimeError,
                _check_proc_visibility=mock.Mock(),
                _check_pid_one_with_lsof=mock.Mock(),
                _scan_with_lsof=scan)

            class Loader:
                @staticmethod
                def exec_module(_module):
                    return None

            protected_spec = SimpleNamespace(loader=Loader())
            real_lstat = pathlib.Path.lstat
            install_root = pathlib.Path(parent_state.__file__).resolve().parents[2]
            script_path = install_root / "scripts/parent-readonly-safety-gate.py"

            def protected_lstat(path):
                path = pathlib.Path(path)
                if path == script_path:
                    return SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o644)
                if path == install_root or install_root in path.parents:
                    return SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755)
                return real_lstat(path)

            with (mock.patch.object(pathlib.Path, "lstat", protected_lstat),
                  mock.patch.object(parent_state.importlib.util, "spec_from_file_location",
                                   return_value=protected_spec),
                  mock.patch.object(parent_state.importlib.util, "module_from_spec",
                                   return_value=scanner)):
                parent_state._scan_stale_disk_holders(
                    disk.fileno(), (info.st_dev, info.st_ino))
            scanner._check_proc_visibility.assert_called_once()
            scanner._check_pid_one_with_lsof.assert_called_once()
            self.assertEqual(scan.call_count, 3)  # positive control plus two disk scans
            self.assertEqual(scan.call_args_list[-1].args[1:], (info.st_dev, info.st_ino))

    def test_stale_boot_reconciliation_refuses_malformed_history(self):
        self.write_stale_running_fixture()
        fd = parent_state._open_directory_chain(create=False, home_override=self.home)
        try:
            run = parent_state._read_json(fd, "active-run.json")
            run["supervisor"]["argv"][3] = "--wrong"
            parent_state._atomic_json(fd, "active-run.json", run)
        finally:
            os.close(fd)
        with self.stale_reconcile_patches()[0], self.stale_reconcile_patches()[1], \
                self.stale_reconcile_patches()[2]:
            with self.assertRaises(parent_state.Refused):
                parent_state.stale_boot_reconciliation(home=self.home, execute=True)
        self.assertFalse((self.parent_dir() / parent_state._stale_reconciliation_filename(
            "0123456789abcdef0123456789abcdef")).exists())

    def test_stale_boot_reconciliation_refuses_disk_holder_and_scan_failure(self):
        self.write_stale_running_fixture()
        for failure in (parent_state.Refused("holder present"), OSError("lsof unavailable")):
            with self.subTest(failure=type(failure).__name__), \
                    self.stale_reconcile_patches()[0], self.stale_reconcile_patches()[1], \
                    mock.patch.object(parent_state, "_scan_stale_disk_holders",
                                      side_effect=failure):
                with self.assertRaises((parent_state.Refused, OSError)):
                    parent_state.stale_boot_reconciliation(home=self.home, execute=True)
        self.assertTrue((self.parent_dir() / "disk.qcow2").exists())
        self.assertFalse((self.parent_dir() / parent_state._stale_reconciliation_filename(
            "0123456789abcdef0123456789abcdef")).exists())

    def test_stale_boot_reconciliation_refuses_lifecycle_lock_contention(self):
        self.write_stale_running_fixture()
        with parent_state.lifecycle_lock(home=self.home):
            with self.assertRaisesRegex(parent_state.Refused, "exclusive verified lifecycle"):
                parent_state.stale_boot_reconciliation(home=self.home, execute=True)

    def test_stale_boot_reconciliation_refuses_disk_identity_change_and_current_boot_run(self):
        run, _disk_info, _disk_path = self.write_stale_running_fixture()
        wrong_disk = mock.patch.object(parent_state, "_disk_status", return_value={
            "path": "disk.qcow2", "format": "qcow2", "virtual_capacity_gib": 64,
            "provisioned": True, "device": 1, "inode": 2,
            "host_allocated_bytes": 1, "file_bytes": 1})
        with wrong_disk, self.stale_reconcile_patches()[1], self.stale_reconcile_patches()[2]:
            with self.assertRaises(parent_state.Refused):
                parent_state.stale_boot_reconciliation(home=self.home, execute=True)

        fd = parent_state._open_directory_chain(create=False, home_override=self.home)
        try:
            run["supervisor"]["host_boot_id"] = pathlib.Path(
                "/proc/sys/kernel/random/boot_id").read_text().strip()
            run["qemu"]["host_boot_id"] = run["supervisor"]["host_boot_id"]
            parent_state._atomic_json(fd, "active-run.json", run)
        finally:
            os.close(fd)
        with self.stale_reconcile_patches()[0], self.stale_reconcile_patches()[1], \
                self.stale_reconcile_patches()[2]:
            with self.assertRaisesRegex(parent_state.Refused, "not a consistent prior-boot"):
                parent_state.stale_boot_reconciliation(home=self.home, execute=True)

    def test_read_only_status_does_not_create_state(self):
        self.assertEqual(parent_state.read_status(home=self.home),
                         {"status": "NOT_CREATED", "parent_id": "default"})
        self.assertFalse((self.home / ".local").exists())

    def test_recovery_preview_is_non_mutating_and_does_not_create_state(self):
        preview = parent_state.recovery_preview(home=self.home)
        self.assertEqual(preview["status"], "NOT_CREATED")
        self.assertEqual(preview["action"], "none")
        self.assertTrue(preview["dry_run"])
        default = parent_state.recover_parent(home=self.home)
        self.assertEqual(default, preview)
        self.assertFalse((self.home / ".local").exists())

    def test_recovery_preview_refuses_while_supervisor_owns_lifecycle_lock(self):
        parent_state.initialize(home=self.home)
        with parent_state.lifecycle_lock(home=self.home):
            with self.assertRaisesRegex(parent_state.Refused, "owns the parent lock"):
                parent_state.recovery_preview(home=self.home)

    def test_recovery_execute_refuses_while_supervisor_owns_lifecycle_lock(self):
        parent_state.initialize(home=self.home)
        with parent_state.lifecycle_lock(home=self.home):
            with self.assertRaisesRegex(parent_state.Refused, "exclusive lifecycle lock"):
                parent_state.recover_parent(home=self.home, execute=True)

    def test_recoverable_preview_exit_code_matches_text_and_json(self):
        recoverable = {"dry_run": True, "action": "recover", "status": "RECOVERABLE",
                       "parent_id": "default", "run_id": "0" * 32,
                       "children": {"qemu": "TARGET", "proxy": "TARGET"}}
        with mock.patch.object(parent_state, "recover_parent", return_value=recoverable):
            with mock.patch.object(sys, "argv", [str(MODULE), "recover"]), contextlib.redirect_stdout(io.StringIO()):
                text_status = parent_state.main()
            with mock.patch.object(sys, "argv", [str(MODULE), "recover", "--json"]), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                json_status = parent_state.main()
        self.assertEqual(text_status, 0)
        self.assertEqual(json_status, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "RECOVERABLE")

    def test_recovery_preview_refuses_symlinked_lock_without_following_it(self):
        parent_state.initialize(home=self.home)
        lock = self.parent_dir() / "lifecycle.lock"
        lock.unlink()
        target = pathlib.Path(self.temp.name) / "outside-lock"
        target.write_bytes(b"untouched")
        lock.symlink_to(target)
        with self.assertRaisesRegex(parent_state.Refused, "unsafe or unreadable"):
            parent_state.recovery_preview(home=self.home)
        self.assertEqual(target.read_bytes(), b"untouched")

    def test_recovery_preview_reports_unprovisioned_state_without_mutation(self):
        parent_state.initialize(home=self.home)
        before = sorted(item.name for item in self.parent_dir().iterdir())
        preview = parent_state.recovery_preview(home=self.home)
        self.assertEqual(preview["status"], "UNPROVISIONED")
        self.assertEqual(preview["action"], "none")
        self.assertEqual(sorted(item.name for item in self.parent_dir().iterdir()), before)

    def test_initialize_writes_private_atomic_parent_record_and_is_idempotent(self):
        record = parent_state.initialize(64, home=self.home)
        directory = self.parent_dir()
        self.assertEqual(record["disk"]["format"], "qcow2")
        self.assertEqual(record["disk"]["virtual_capacity_gib"], 64)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((directory / "parent.json").stat().st_mode), 0o600)
        self.assertEqual(list(directory.glob("*.tmp")), [])
        self.assertEqual(parent_state.initialize(64, home=self.home), record)
        with self.assertRaises(parent_state.Refused):
            parent_state.initialize(32, home=self.home)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "UNPROVISIONED")

    def test_status_refuses_unsafe_directory_without_repairing_it(self):
        parent_state.initialize(home=self.home)
        directory = self.parent_dir()
        os.chmod(directory, 0o755)
        with self.assertRaises(parent_state.Refused):
            parent_state.read_status(home=self.home)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)

    def test_symlink_in_parent_state_path_is_refused_without_touching_target(self):
        target = pathlib.Path(self.temp.name) / "target"
        target.mkdir()
        (self.home / ".local").symlink_to(target, target_is_directory=True)
        with self.assertRaises(parent_state.Refused):
            parent_state.initialize(home=self.home)
        self.assertEqual(list(target.iterdir()), [])

    def test_parent_record_symlink_and_malformed_json_are_refused(self):
        parent_state.initialize(home=self.home)
        record_path = self.parent_dir() / "parent.json"
        target = pathlib.Path(self.temp.name) / "outside.json"
        target.write_text("{}\n")
        record_path.unlink()
        record_path.symlink_to(target)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")
        record_path.unlink()
        record_path.write_text("{not-json\n")
        os.chmod(record_path, 0o600)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")

    def test_parent_record_rejects_unknown_schema_fields(self):
        record = parent_state.initialize(home=self.home)
        record["unexpected"] = "not part of schema"
        with parent_state.lifecycle_lock(home=self.home):
            fd = parent_state._open_directory_chain(create=False, home_override=self.home)
            try:
                parent_state._atomic_json(fd, "parent.json", record)
            finally:
                os.close(fd)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")

        with self.assertRaises(parent_state.Refused):
            parent_state.initialize(home=self.home)

    def test_parent_record_rejects_null_seed_identity_extension(self):
        record = parent_state.initialize(home=self.home)
        record["ssh_identity"] = None
        with self.assertRaises(parent_state.Refused):
            parent_state.validate_parent(record)

    def test_fifo_state_files_do_not_block_status(self):
        parent_state.initialize(home=self.home)
        directory = self.parent_dir()
        record_path = directory / "parent.json"
        record_path.unlink()
        os.mkfifo(record_path, 0o600)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")
        record_path.unlink()
        record_path.write_text("{not-json\n")
        os.chmod(record_path, 0o600)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")

    def test_metadata_or_disk_inconsistency_is_ambiguous(self):
        parent_state.initialize(home=self.home)
        directory = self.parent_dir()
        run = {"schema": 1, "parent_id": "default", "run_id": "0123456789abcdef",
               "supervisor": {}}
        with parent_state.lifecycle_lock(home=self.home):
            fd = parent_state._open_directory_chain(create=False, home_override=self.home)
            try:
                parent_state._atomic_json(fd, "active-run.json", run)
            finally:
                os.close(fd)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")
        (directory / "active-run.json").unlink()
        (directory / "disk.qcow2").write_bytes(b"unexpected")
        os.chmod(directory / "disk.qcow2", 0o600)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")

    def test_run_without_parent_metadata_is_ambiguous(self):
        with parent_state.lifecycle_lock(home=self.home):
            fd = parent_state._open_directory_chain(create=False, home_override=self.home)
            try:
                parent_state._atomic_json(fd, "active-run.json", {"schema": 1})
            finally:
                os.close(fd)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")

    def test_lifecycle_lock_is_exclusive_and_not_inherited_by_exec_child(self):
        parent_state.initialize(home=self.home)
        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            self.assertFalse(os.get_inheritable(lock_fd))
            with self.assertRaises(parent_state.Refused):
                with parent_state.lifecycle_lock(home=self.home):
                    self.fail("second lifecycle lock unexpectedly acquired")
            directory = self.parent_dir()
            os.chmod(directory, 0o755)
            with self.assertRaises(parent_state.Refused):
                parent_state.initialize(home=self.home)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)
            os.chmod(directory, 0o700)
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"],
                                     close_fds=False)
        try:
            # The exec'd child is still alive, but must not retain the lock.
            self.assertIsNone(child.poll())
            with parent_state.lifecycle_lock(home=self.home):
                pass
        finally:
            child.wait(timeout=3)

    def test_running_status_requires_exact_process_identity(self):
        parent = parent_state.initialize(home=self.home)
        run = {"run_id": "test-run", "supervisor": {"pid": os.getpid(), "schema": 1,
                "uid": os.getuid(), "host_boot_id": "mismatch", "start_time": "0",
                "exe": sys.executable, "argv": [sys.executable]}}
        self.assertEqual(parent_state.status_for_run(parent, run)["status"], "AMBIGUOUS")

    def test_supervisor_identity_accepts_exact_up_ssh_port_arguments(self):
        self.assertTrue(parent_state._valid_supervisor_options(["--ssh-port", "23456"], None))
        self.assertTrue(parent_state._valid_supervisor_options(
            ["--state-home", str(self.home), "--ssh-port", "23456"], self.home))
        self.assertFalse(parent_state._valid_supervisor_options(
            ["--ssh-port", "23456"], self.home))
        self.assertFalse(parent_state._valid_supervisor_options(
            ["--state-home", str(self.home), "--ssh-port", "0"], self.home))

    def test_previous_candidate_runtime_script_requires_protected_sha_install_chain(self):
        from types import SimpleNamespace

        install_root = pathlib.Path(self.temp.name) / "protected" / "v3"
        sha_root = install_root / ("a" * 40)
        script = sha_root / "lib/vm/parent-supervisor.py"
        script.parent.mkdir(parents=True)
        script.write_text("# trusted fixture\n")
        os.chmod(script, 0o644)
        real_lstat = os.lstat

        def root_owned_lstat(path, *, override=None):
            candidate = pathlib.Path(path)
            if override and candidate == override.get("missing"):
                raise FileNotFoundError(candidate)
            if override and candidate == override.get("symlink"):
                return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0)
            if override and candidate == override.get("writable"):
                return SimpleNamespace(st_mode=stat.S_IFDIR | 0o775, st_uid=0)
            info = real_lstat(candidate)
            if candidate == script:
                mode = override.get("file_mode", info.st_mode) if override else info.st_mode
                uid = override.get("file_uid", 0) if override else 0
                return SimpleNamespace(st_mode=mode, st_uid=uid)
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)

        def check(override=None, candidate=script, filename="parent-supervisor.py", raw=None):
            with mock.patch.object(parent_state.os, "lstat",
                                   side_effect=lambda path: root_owned_lstat(path, override=override)):
                return parent_state._trusted_parent_runtime_script(
                    str(candidate) if raw is None else raw, filename)

        self.assertTrue(check())
        self.assertFalse(check({"file_mode": stat.S_IFREG | 0o664}))
        self.assertFalse(check({"file_uid": os.getuid() + 1}))
        self.assertFalse(check({"writable": sha_root}))
        self.assertFalse(check({"symlink": sha_root / "lib"}))
        self.assertFalse(check({"missing": script}))
        self.assertFalse(check(candidate=install_root / "not-a-sha" / "lib/vm/parent-supervisor.py"))
        self.assertFalse(check(candidate=script, filename="parent-gate.py"))
        self.assertFalse(check(raw=str(script).replace("/lib/vm/", "/lib//vm/")))
        self.assertFalse(check(raw=str(script).replace("/lib/vm/", "/lib/./vm/")))
        self.assertFalse(check(raw=str(script) + "/"))

    def test_installed_runtime_root_derives_from_its_own_prefix(self):
        for prefix in (pathlib.Path("/opt/little-mester"),
                       pathlib.Path("/usr/local/share/little-mester")):
            source = prefix / "v3" / ("c" * 40) / "lib/vm/parent-state.py"
            self.assertEqual(parent_state._installed_runtime_root(source), prefix / "v3")
        self.assertIsNone(parent_state._installed_runtime_root(
            ROOT / "lib/vm/parent-state.py"))

    def test_previous_sha_is_trusted_from_any_protected_configured_prefix(self):
        from types import SimpleNamespace

        prefix_root = pathlib.Path("/srv/little-mester/v3")
        sha_root = prefix_root / ("d" * 40)
        source = sha_root / "lib/vm/parent-supervisor.py"

        def protected_lstat(path):
            candidate = pathlib.Path(path)
            if candidate.name == "parent-supervisor.py":
                return SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0)
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)

        with mock.patch.object(parent_state.os, "lstat", side_effect=protected_lstat):
            self.assertTrue(parent_state._trusted_parent_runtime_script(
                str(source), "parent-supervisor.py"))
            other_prefix = pathlib.Path("/opt/little-mester/v3") / ("e" * 40) / "lib/vm/parent-supervisor.py"
            self.assertTrue(parent_state._trusted_parent_runtime_script(
                str(other_prefix), "parent-supervisor.py"))

    def test_stopping_tombstone_from_previous_trusted_candidate_projects_stopped(self):
        parent = parent_state.initialize(home=self.home)
        directory = self.parent_dir()
        disk = directory / "disk.qcow2"
        disk.write_bytes(b"persistent-parent-disk")
        os.chmod(disk, 0o600)
        info = disk.stat()
        parent["disk"].update({"provisioned": True, "device": info.st_dev, "inode": info.st_ino})
        run_id = "0123456789abcdef0123456789abcdef"
        old_script = "/opt/little-mester/v3/" + "a" * 40 + "/lib/vm/parent-supervisor.py"
        argv = [os.path.realpath(sys.executable), "-I", old_script, "--parent-id", "default",
                "--run-id", run_id, "--lock-fd", "3", "--handoff-fd", "5",
                "--ssh-port", "55719"]
        identity = {"schema": 1, "pid": 2**31 - 1, "uid": os.getuid(),
                    "host_boot_id": pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                    "start_time": "1", "exe": os.path.realpath(sys.executable), "argv": argv}
        run = {"schema": 1, "parent_id": "default", "run_id": run_id,
               "phase": "STOPPING", "supervisor": identity}
        with mock.patch.object(parent_state, "_trusted_parent_runtime_script", return_value=True):
            result = parent_state.status_for_run(parent, run, parent["disk"])
        self.assertEqual(result["status"], "STOPPED")

    def test_gated_transition_accepts_previous_candidate_only_through_trusted_path_check(self):
        token = "1" * 32
        old_gate = "/opt/little-mester/v3/" + "b" * 40 + "/lib/vm/parent-gate.py"
        argv = [os.path.realpath(sys.executable), "-I", old_gate,
                "--release-fd", "4", "--token", token,
                "--target", "child-proxy.target.json", "--identity", "child-proxy.identity.json",
                "--state-dir-fd", "5", "--disk-fd", "-1"]
        with mock.patch.object(parent_state, "_trusted_parent_runtime_script",
                               side_effect=lambda path, filename: path == old_gate and filename == "parent-gate.py"):
            parent_state._validate_gate_invocation(
                argv, os.path.realpath(sys.executable), token,
                "child-proxy.target.json", "child-proxy.identity.json", [], "invalid fixture")
            argv[2] = "/tmp/untrusted-parent-gate.py"
            with self.assertRaises(parent_state.Refused):
                parent_state._validate_gate_invocation(
                    argv, os.path.realpath(sys.executable), token,
                    "child-proxy.target.json", "child-proxy.identity.json", [], "invalid fixture")

    def test_status_rejects_supervisor_with_malformed_handoff_arguments(self):
        parent = parent_state.initialize(home=self.home)
        directory = self.parent_dir()
        supervisor_path = pathlib.Path(self.temp.name) / "lib/vm/parent-supervisor.py"
        supervisor_path.parent.mkdir(parents=True)
        supervisor_path.write_text("import time\ntime.sleep(5)\n")
        os.chmod(supervisor_path, 0o600)
        disk_path = directory / "disk.qcow2"
        with disk_path.open("wb") as disk_file:
            disk_file.truncate(1024 * 1024)
        os.chmod(disk_path, 0o600)
        disk_info = disk_path.stat()
        parent["disk"].update({"provisioned": True, "device": disk_info.st_dev,
                               "inode": disk_info.st_ino})
        run_id = "0123456789abcdef0123456789abcdef"
        child = subprocess.Popen([sys.executable, "-I", str(supervisor_path),
                                  "--parent-id", "default", "--run-id", run_id])
        try:
            identity = parent_state.observe_process(child.pid)
            identity["argv"] = [sys.executable, "-I", str(ROOT / "lib/vm/parent-supervisor.py"),
                                "--parent-id", "default", "--run-id", run_id,
                                "--lock-fd", "3", "--control-fd", "4"]
            with parent_state.lifecycle_lock(home=self.home) as lock_fd:
                fd = parent_state._open_directory_chain(create=False, home_override=self.home)
                try:
                    parent_state._verify_lock_fd(lock_fd, home=self.home)
                    parent_state._atomic_json(fd, "parent.json", parent)
                    parent_state._atomic_json(fd, "active-run.json", {"schema": 1, "parent_id": "default",
                                               "run_id": run_id, "phase": "STARTING", "supervisor": identity})
                finally:
                    os.close(fd)
            result = parent_state.read_status(home=self.home)
            self.assertEqual(result["status"], "AMBIGUOUS")
        finally:
            child.terminate()
            child.wait(timeout=3)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")
        os.unlink(disk_path)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")


if __name__ == "__main__":
    unittest.main()
