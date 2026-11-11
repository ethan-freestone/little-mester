#!/usr/bin/env python3
"""REAL_PROCESS tests for terminal-independent supervisor lock handoff."""

import importlib.util
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("parent_state", ROOT / "lib/vm/parent-state.py")
parent_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(parent_state)
SUPERVISOR = ROOT / "lib/vm/parent-supervisor.py"
SUPERVISOR_SPEC = importlib.util.spec_from_file_location("parent_supervisor", SUPERVISOR)
parent_supervisor = importlib.util.module_from_spec(SUPERVISOR_SPEC)
SUPERVISOR_SPEC.loader.exec_module(parent_supervisor)


class SupervisorHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.temp.name) / "home"
        self.home.mkdir(mode=0o700)
        os.chmod(self.home, 0o700)
        self.parent = parent_state.initialize(home=self.home)
        self.state_dir = self.home / ".local/state/little-mester/parent-v3/default"
        disk = self.state_dir / "disk.qcow2"
        disk.write_bytes(b"qcow2-test-placeholder")
        os.chmod(disk, 0o600)
        info = disk.stat()
        self.parent["disk"].update({"provisioned": True, "device": info.st_dev, "inode": info.st_ino})
        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            fd = parent_state._open_directory_chain(create=False, home_override=self.home)
            try:
                parent_state._atomic_json(fd, "parent.json", self.parent)
            finally:
                os.close(fd)

    def tearDown(self):
        self.temp.cleanup()

    def cleanup_gated_child(self, role, transition=None):
        owned = parent_supervisor._CHILD_PROCESSES.get(role)
        if transition is None and owned is not None:
            transition = owned[1]
        if transition is not None:
            self.assertEqual(parent_supervisor._stop_gated_target(parent_state, role, transition), "STOPPED")
        elif owned is not None:
            owned[0].wait(timeout=3)
            parent_supervisor._CHILD_PROCESSES.pop(role, None)

    @staticmethod
    def cleanup_test_supervisor(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)

    def spawn_supervisor(self, *, send_commit=True):
        run_id = "0123456789abcdef0123456789abcdef"
        handoff_read, handoff_write = os.pipe2(os.O_CLOEXEC)
        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            args = [sys.executable, "-I", str(SUPERVISOR), "--parent-id", "default", "--run-id", run_id,
                    "--lock-fd", str(lock_fd), "--handoff-fd", str(handoff_read),
                    "--state-home", str(self.home)]
            proc = subprocess.Popen(args, pass_fds=(lock_fd, handoff_read), close_fds=True,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
            os.close(lock_fd)  # Drop this open-file-description reference; do not LOCK_UN.
            if send_commit:
                os.write(handoff_write, b"COMMIT\n")
            os.close(handoff_write)
        os.close(handoff_read)
        return proc, run_id

    def test_handoff_records_exact_supervisor_and_retains_lock_until_owner_stop(self):
        proc, run_id = self.spawn_supervisor()
        reaper = threading.Thread(target=proc.wait)
        reaper.start()
        try:
            deadline = time.monotonic() + 3
            status = parent_state.read_status(home=self.home)
            while time.monotonic() < deadline and status["status"] == "AMBIGUOUS" and proc.poll() is None:
                time.sleep(0.01)
                status = parent_state.read_status(home=self.home)
            self.assertEqual(status["status"], "STARTING")
            self.assertEqual(status["run_id"], run_id)
            with self.assertRaises(parent_state.Refused):
                with parent_state.lifecycle_lock(home=self.home):
                    self.fail("supervisor did not retain exclusive lifecycle lock")
            self.assertFalse((self.state_dir / "supervisor.sock").exists())
            result = parent_state.stop_starting_supervisor(home=self.home)
            self.assertTrue(result["disk_retained"])
            reaper.join(timeout=3)
            self.assertFalse(reaper.is_alive())
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "STOPPED")
            self.assertTrue((self.state_dir / "disk.qcow2").exists())
            with parent_state.lifecycle_lock(home=self.home):
                pass
        finally:
            self.cleanup_test_supervisor(proc)

    def test_parent_stop_cli_helper_requests_only_verified_starting_supervisor(self):
        proc, run_id = self.spawn_supervisor()
        reaper = threading.Thread(target=proc.wait)
        reaper.start()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and parent_state.read_status(home=self.home)["status"] != "STARTING":
                time.sleep(0.01)
            result = parent_state.stop_starting_supervisor(home=self.home)
            self.assertEqual(result["status"], "STOPPED")
            self.assertEqual(result["run_id"], run_id)
            self.assertTrue(result["disk_retained"])
            reaper.join(timeout=3)
            self.assertFalse(reaper.is_alive())
            self.assertEqual(proc.returncode, 0)
            self.assertTrue((self.state_dir / "disk.qcow2").exists())
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "STOPPED")
        finally:
            self.cleanup_test_supervisor(proc)

    def test_parent_stop_refuses_unverified_supervisor_identity_without_signal(self):
        proc, _run_id = self.spawn_supervisor()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and parent_state.read_status(home=self.home)["status"] != "STARTING":
                time.sleep(0.01)
            fd = parent_state._open_directory_chain(create=False, home_override=self.home)
            try:
                run = parent_state._read_json(fd, "active-run.json")
                run["supervisor"]["start_time"] = str(int(run["supervisor"]["start_time"]) + 1)
                parent_state._atomic_json(fd, "active-run.json", run)
            finally:
                os.close(fd)
            with self.assertRaises(parent_state.Refused):
                parent_state.stop_starting_supervisor(home=self.home)
            self.assertIsNone(proc.poll())
            self.assertTrue((self.state_dir / "disk.qcow2").exists())
        finally:
            self.cleanup_test_supervisor(proc)

    def test_parent_stop_refuses_when_pidfd_api_is_unavailable(self):
        proc, _run_id = self.spawn_supervisor()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and parent_state.read_status(home=self.home)["status"] != "STARTING":
                time.sleep(0.01)
            with mock.patch.object(parent_state.os, "pidfd_open", side_effect=AttributeError):
                with self.assertRaisesRegex(parent_state.Refused, "pidfd"):
                    parent_state.stop_starting_supervisor(home=self.home)
            self.assertIsNone(proc.poll())
            self.assertTrue((self.state_dir / "disk.qcow2").exists())
        finally:
            self.cleanup_test_supervisor(proc)

    def test_sigkill_supervisor_releases_kernel_lock_but_reports_ambiguous_without_recovery(self):
        proc, _run_id = self.spawn_supervisor()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and parent_state.read_status(home=self.home)["status"] != "STARTING":
                time.sleep(0.01)
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "STARTING")
            proc.kill()
            self.assertNotEqual(proc.wait(timeout=3), 0)
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")
            preview = parent_state.recovery_preview(home=self.home)
            self.assertEqual(preview["status"], "AMBIGUOUS")
            self.assertEqual(preview["action"], "none")
            self.assertTrue((self.state_dir / "disk.qcow2").exists())
            with parent_state.lifecycle_lock(home=self.home):
                pass
        finally:
            self.cleanup_test_supervisor(proc)

    def test_missing_commit_does_not_record_supervisor_and_releases_lock(self):
        proc, _run_id = self.spawn_supervisor(send_commit=False)
        try:
            self.assertEqual(proc.wait(timeout=3), 2)
            self.assertFalse((self.state_dir / "active-run.json").exists())
            with parent_state.lifecycle_lock(home=self.home):
                pass
        finally:
            self.cleanup_test_supervisor(proc)

    def test_supervisor_gated_spawn_records_exact_same_pid_target_before_release(self):
        marker = pathlib.Path(self.temp.name) / "gated-target.started"
        replacement_marker = pathlib.Path(self.temp.name) / "must-not-replace"
        executable = os.path.realpath(sys.executable)
        code = f"import pathlib,time; pathlib.Path({str(marker)!r}).write_text('ready'); time.sleep(30)"
        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            transition = None
            try:
                transition = parent_supervisor._spawn_gated_target(
                    parent_state, lock_fd=lock_fd, role="qemu", executable=executable,
                    argv=[executable, "-c", code], home=str(self.home))
                self.assertEqual(parent_state.exec_transition_status(transition), "TARGET")
                self.assertEqual(transition["gate"]["exe"], executable)
                self.assertEqual(transition["target"]["exe"], executable)
                self.assertEqual(transition["gate"]["argv"][0], executable)
                self.assertEqual(transition["pid"], parent_state.observe_process(transition["pid"])["pid"])
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not marker.exists():
                    time.sleep(0.01)
                self.assertTrue(marker.exists())
                target_record = (self.state_dir / "child-qemu.target.json").read_bytes()
                identity_record = (self.state_dir / "child-qemu.identity.json").read_bytes()
                with self.assertRaises(parent_state.Refused):
                    parent_supervisor._spawn_gated_target(
                        parent_state, lock_fd=lock_fd, role="qemu", executable=executable,
                        argv=[executable, "-c", f"import pathlib; pathlib.Path({str(replacement_marker)!r}).touch()"],
                        home=str(self.home))
                self.assertEqual((self.state_dir / "child-qemu.target.json").read_bytes(), target_record)
                self.assertEqual((self.state_dir / "child-qemu.identity.json").read_bytes(), identity_record)
                self.assertEqual(parent_state.exec_transition_status(transition), "TARGET")
                self.assertFalse(replacement_marker.exists())
                with self.assertRaises(parent_state.Refused):
                    with parent_state.lifecycle_lock(home=self.home):
                        self.fail("gated target caused supervisor lock release")
            finally:
                self.cleanup_gated_child("qemu", transition)
            self.assertEqual(parent_state.exec_transition_status(transition), "STOPPED")

    def test_gated_spawn_refuses_lock_fd_alias_without_closing_callers_fd(self):
        executable = os.path.realpath(sys.executable)
        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            with self.assertRaises(parent_state.Refused):
                parent_supervisor._spawn_gated_target(
                    parent_state, lock_fd=lock_fd, role="qemu", executable=executable,
                    argv=[executable, "-c", "pass"], home=str(self.home), disk_fd=lock_fd)
            self.assertEqual(os.fstat(lock_fd).st_ino, (self.state_dir / "lifecycle.lock").stat().st_ino)

    def test_parent_seed_persists_control_and_host_identity_without_host_exports(self):
        calls = []
        public_by_private = {}

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if "-t" in argv:
                key_path = pathlib.Path(argv[-1])
                private = f"private:{key_path.name}".encode()
                public = f"ssh-ed25519 {('A' * 40)} {key_path.name}\n".encode()
                key_path.write_bytes(private)
                pathlib.Path(f"{key_path}.pub").write_bytes(public)
                public_by_private[private] = public
                return subprocess.CompletedProcess(argv, 0)
            if "-y" in argv:
                private = pathlib.Path(argv[-1]).read_bytes()
                return subprocess.CompletedProcess(argv, 0, stdout=public_by_private[private])
            if "-output" in argv:
                pathlib.Path(argv[argv.index("-output") + 1]).write_bytes(b"seed-iso-fixture")
                return subprocess.CompletedProcess(argv, 0)
            raise AssertionError(argv)

        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            with (mock.patch.object(parent_supervisor, "_protected_tool",
                                    side_effect=lambda name, _paths: "/usr/bin/ssh-keygen" if name == "ssh-keygen" else "/usr/bin/genisoimage"),
                  mock.patch.object(parent_supervisor.subprocess, "run", side_effect=fake_run)):
                host_public = parent_supervisor._prepare_parent_seed(
                    parent_state, lock_fd, str(self.home))
                seed = (self.state_dir / "seed.iso").read_bytes()
                self.assertEqual(seed, b"seed-iso-fixture")
                self.assertEqual(stat.S_IMODE((self.state_dir / "seed.iso").stat().st_mode), 0o600)
                user_data = (self.state_dir / "user-data").read_text()
                config = json.loads(user_data.split("\n", 1)[1])
                self.assertFalse(any("virtiofs" in json.dumps(entry) for entry in config.get("runcmd", [])))
                self.assertIn("ssh-ed25519", config["users"][0]["ssh_authorized_keys"][0])
                self.assertEqual(config["ssh_keys"]["ed25519_private"],
                                 (self.state_dir / "ssh_host_ed25519_key").read_text())
                sshd_config = next(item for item in config["write_files"]
                                   if item["path"].endswith("99-little-mester-parent.conf"))
                self.assertEqual(sshd_config["content"].count("\n"), 1)
                port = 22345
                directory_fd = parent_state._open_directory_chain(create=False, home_override=self.home)
                try:
                    parent_supervisor._write_ssh_connection(parent_state, directory_fd, port, host_public)
                finally:
                    os.close(directory_fd)
                self.assertIn(f"[127.0.0.1]:{port} ssh-ed25519".encode(),
                              (self.state_dir / "known_hosts").read_bytes())
                before_calls = len(calls)
                same_public = parent_supervisor._prepare_parent_seed(
                    parent_state, lock_fd, str(self.home))
                self.assertEqual(same_public, host_public)
                self.assertEqual((self.state_dir / "seed.iso").read_bytes(), seed)
                self.assertEqual(len(calls[before_calls:]), 2)
                self.assertTrue(all("-y" in command for command in calls[before_calls:]))
                directory_fd = parent_state._open_directory_chain(create=False, home_override=self.home)
                try:
                    recorded_parent = parent_state._read_json(directory_fd, "parent.json")
                    parent_state._validate_seed_identity(directory_fd, recorded_parent)
                finally:
                    os.close(directory_fd)
                private_path = self.state_dir / "id_vm_control"
                original_private = private_path.read_bytes()
                private_path.unlink()
                directory_fd = parent_state._open_directory_chain(create=False, home_override=self.home)
                try:
                    with self.assertRaises(parent_state.Refused):
                        parent_state._validate_seed_identity(
                            directory_fd, parent_state._read_json(directory_fd, "parent.json"))
                finally:
                    os.close(directory_fd)
                private_path.write_bytes(original_private)
                os.chmod(private_path, 0o600)
                private_path.write_bytes(b"replacement private key")
                directory_fd = parent_state._open_directory_chain(create=False, home_override=self.home)
                try:
                    with self.assertRaises(parent_state.Refused):
                        parent_state._validate_seed_identity(
                            directory_fd, parent_state._read_json(directory_fd, "parent.json"))
                finally:
                    os.close(directory_fd)
                private_path.unlink()
                os.mkfifo(private_path, 0o600)
                directory_fd = parent_state._open_directory_chain(create=False, home_override=self.home)
                try:
                    with self.assertRaises(parent_state.Refused):
                        parent_state._validate_seed_identity(
                            directory_fd, parent_state._read_json(directory_fd, "parent.json"))
                finally:
                    os.close(directory_fd)
                (self.state_dir / "seed.iso").write_bytes(b"substituted seed")
                directory_fd = parent_state._open_directory_chain(create=False, home_override=self.home)
                try:
                    with self.assertRaises(parent_state.Refused):
                        parent_state._validate_seed_identity(
                            directory_fd, parent_state._read_json(directory_fd, "parent.json"))
                finally:
                    os.close(directory_fd)
                (self.state_dir / "seed.iso").write_bytes(seed)

    def test_partial_seed_iso_is_refused_and_reported_ambiguous(self):
        part = self.state_dir / "seed.iso.part"
        part.write_bytes(b"partial evidence")
        os.chmod(part, 0o600)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")
        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            with self.assertRaises(parent_state.Refused):
                parent_supervisor._prepare_parent_seed(parent_state, lock_fd, str(self.home))
        self.assertEqual(part.read_bytes(), b"partial evidence")

    def test_existing_seed_refuses_missing_persistent_key_material(self):
        (self.state_dir / "seed.iso").write_bytes(b"seed")
        os.chmod(self.state_dir / "seed.iso", 0o600)
        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            with mock.patch.object(parent_supervisor, "_protected_tool", return_value="/usr/bin/ssh-keygen"):
                with self.assertRaises(RuntimeError):
                    parent_supervisor._prepare_parent_seed(parent_state, lock_fd, str(self.home))

    def test_supervisor_gated_spawn_eof_fails_closed_on_transition_persistence_error(self):
        marker = pathlib.Path(self.temp.name) / "must-not-start"
        executable = os.path.realpath(sys.executable)
        code = f"import pathlib; pathlib.Path({str(marker)!r}).write_text('started')"
        captured = {}

        def fail_commit(_directory_fd, _name, identity, **_kwargs):
            captured["identity"] = identity
            raise parent_state.Refused("injected authorization persistence failure")

        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            try:
                with mock.patch.object(parent_state, "persist_exec_transition", side_effect=fail_commit):
                    with self.assertRaises(parent_state.Refused):
                        parent_supervisor._spawn_gated_target(
                            parent_state, lock_fd=lock_fd, role="qemu", executable=executable,
                            argv=[executable, "-c", code], home=str(self.home))
                transition = captured["identity"]
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and parent_state.exec_transition_status(transition) != "STOPPED":
                    time.sleep(0.01)
                self.assertEqual(parent_state.exec_transition_status(transition), "STOPPED")
                self.assertFalse(marker.exists())
            finally:
                self.cleanup_gated_child("qemu", captured.get("identity"))

    def test_post_bind_record_failure_removes_only_new_socket(self):
        active_run = self.state_dir / "active-run.json"
        active_run.mkdir(mode=0o700)
        proc, _run_id = self.spawn_supervisor()
        try:
            self.assertEqual(proc.wait(timeout=3), 2)
            self.assertTrue(active_run.is_dir())
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")
        finally:
            self.cleanup_test_supervisor(proc)

    def test_commit_eof_barrier_is_bounded_when_writer_stays_open(self):
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        try:
            os.write(write_fd, b"COMMIT\n")
            started = time.monotonic()
            self.assertFalse(parent_supervisor._read_commit(read_fd, timeout_seconds=0.05))
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_atomic_stop_record_failure_still_closes_supervisor_lock(self):
        directory_fd = parent_state._open_directory_chain(create=False, home_override=self.home)
        try:
            parent_state._atomic_json(directory_fd, "active-run.json",
                                      {"run_id": "test-run", "supervisor": {"pid": os.getpid()}})
        finally:
            os.close(directory_fd)
        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            with mock.patch.object(parent_state, "_atomic_json", side_effect=OSError("injected final write failure")):
                with self.assertRaises(OSError):
                    parent_supervisor._finalize(
                        parent_state, SimpleNamespace(run_id="test-run", lock_fd=lock_fd),
                        str(self.home))
            with self.assertRaises(OSError):
                os.fstat(lock_fd)


if __name__ == "__main__":
    unittest.main()
