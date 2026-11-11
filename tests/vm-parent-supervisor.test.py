#!/usr/bin/env python3
"""REAL_PROCESS tests for terminal-independent supervisor lock handoff."""

import importlib.util
import os
import pathlib
import socket
import stat
import subprocess
import sys
import tempfile
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
        try:
            socket_path = self.state_dir / "supervisor.sock"
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

            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(socket_path))
                client.sendall(b"STATUS\n")
                self.assertEqual(client.recv(32), b"STARTING\n")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(socket_path))
                client.sendall(b"STOP\n")
                self.assertEqual(client.recv(32), b"STOPPING\n")
            self.assertEqual(proc.wait(timeout=3), 0)
            self.assertFalse(socket_path.exists())
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "STOPPED")
            self.assertTrue((self.state_dir / "disk.qcow2").exists())
            with parent_state.lifecycle_lock(home=self.home):
                pass
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)

    def test_missing_commit_does_not_record_supervisor_and_releases_lock(self):
        proc, _run_id = self.spawn_supervisor(send_commit=False)
        self.assertEqual(proc.wait(timeout=3), 2)
        self.assertFalse((self.state_dir / "active-run.json").exists())
        with parent_state.lifecycle_lock(home=self.home):
            pass

    def test_idle_control_client_does_not_block_termination(self):
        proc, _run_id = self.spawn_supervisor()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            socket_path = self.state_dir / "supervisor.sock"
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not socket_path.exists() and proc.poll() is None:
                time.sleep(0.01)
            client.connect(str(socket_path))
            time.sleep(0.05)
            proc.terminate()
            self.assertEqual(proc.wait(timeout=3), 0)
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "STOPPED")
        finally:
            client.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)

    def test_preexisting_control_path_is_preserved_on_startup_refusal(self):
        socket_path = self.state_dir / "supervisor.sock"
        socket_path.write_text("owner evidence\n")
        os.chmod(socket_path, 0o600)
        proc, _run_id = self.spawn_supervisor()
        self.assertEqual(proc.wait(timeout=3), 2)
        self.assertEqual(socket_path.read_text(), "owner evidence\n")
        with parent_state.lifecycle_lock(home=self.home):
            pass

    def test_post_bind_record_failure_removes_only_new_socket(self):
        active_run = self.state_dir / "active-run.json"
        active_run.mkdir(mode=0o700)
        proc, _run_id = self.spawn_supervisor()
        self.assertEqual(proc.wait(timeout=3), 2)
        self.assertFalse((self.state_dir / "supervisor.sock").exists())
        self.assertTrue(active_run.is_dir())
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")

    def test_socket_chmod_failure_cleans_only_newly_bound_socket(self):
        directory_fd = parent_state._open_directory_chain(create=False, home_override=self.home)
        try:
            with mock.patch.object(parent_supervisor.os, "chmod", side_effect=OSError("injected chmod failure")):
                with self.assertRaises(OSError):
                    parent_supervisor._listen(parent_state, directory_fd, str(self.home))
        finally:
            os.close(directory_fd)
        self.assertFalse((self.state_dir / "supervisor.sock").exists())

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

    def test_shutdown_record_failure_still_removes_owned_socket_and_closes_lock(self):
        proc, _run_id = self.spawn_supervisor()
        socket_path = self.state_dir / "supervisor.sock"
        deadline = time.monotonic() + 3
        status = parent_state.read_status(home=self.home)
        while time.monotonic() < deadline and status["status"] == "AMBIGUOUS" and proc.poll() is None:
            time.sleep(0.01)
            status = parent_state.read_status(home=self.home)
        self.assertEqual(status["status"], "STARTING")
        active_run = self.state_dir / "active-run.json"
        active_run.unlink()
        active_run.mkdir(mode=0o700)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(socket_path))
                client.sendall(b"STOP\n")
                self.assertEqual(client.recv(32), b"STOPPING\n")
            self.assertNotEqual(proc.wait(timeout=3), 0)
            self.assertFalse(socket_path.exists())
            with parent_state.lifecycle_lock(home=self.home):
                pass
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)

    def test_atomic_stop_record_failure_still_runs_socket_and_lock_finalizers(self):
        socket_path = self.state_dir / "supervisor.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        info = os.lstat(socket_path)
        socket_identity = (info.st_dev, info.st_ino)
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
                        str(self.home), listener, socket_identity)
            self.assertFalse(socket_path.exists())
            with self.assertRaises(OSError):
                os.fstat(lock_fd)

    def test_cleanup_preserves_replacement_socket_path(self):
        proc, _run_id = self.spawn_supervisor()
        socket_path = self.state_dir / "supervisor.sock"
        deadline = time.monotonic() + 3
        status = parent_state.read_status(home=self.home)
        while time.monotonic() < deadline and status["status"] == "AMBIGUOUS" and proc.poll() is None:
            time.sleep(0.01)
            status = parent_state.read_status(home=self.home)
        self.assertEqual(status["status"], "STARTING")
        original_info = os.lstat(socket_path)
        socket_path.unlink()
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        replacement.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        replacement_info = os.lstat(socket_path)
        try:
            self.assertNotEqual((replacement_info.st_dev, replacement_info.st_ino),
                                (original_info.st_dev, original_info.st_ino))
            # The old listener has no pathname now; request shutdown by signal.
            proc.terminate()
            self.assertEqual(proc.wait(timeout=3), 0)
            self.assertTrue(socket_path.exists())
            self.assertTrue(stat.S_ISSOCK(os.lstat(socket_path).st_mode))
        finally:
            replacement.close()
            socket_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
