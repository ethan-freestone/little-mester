#!/usr/bin/env python3
"""REAL_PROCESS tests for gated same-process child exec authority."""

import importlib.util
import fcntl
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE_SPEC = importlib.util.spec_from_file_location("parent_state", ROOT / "lib/vm/parent-state.py")
parent_state = importlib.util.module_from_spec(STATE_SPEC)
STATE_SPEC.loader.exec_module(parent_state)
GATE = ROOT / "lib/vm/parent-gate.py"


class ParentGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.marker = self.root / "workload-started"
        self.state_dir = self.root / "state"
        self.state_dir.mkdir(mode=0o700)
        os.chmod(self.state_dir, 0o700)
        self.state_fd = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
        self.lock_fd = os.open(self.state_dir / "lifecycle.lock", os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(self.lock_fd, 0o600)
        os.set_inheritable(self.lock_fd, False)
        fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.target_name = "child-qemu.target.json"
        self.identity_name = "child-qemu.identity.json"
        self.target = self.state_dir / self.target_name
        executable = str(pathlib.Path(sys.executable).resolve())
        code = f"import pathlib,time; pathlib.Path({str(self.marker)!r}).write_text('started'); time.sleep(20)"
        self.target_argv = [executable, "-c", code, ""]
        parent_state._atomic_json(self.state_fd, self.target_name,
                                  {"executable": executable, "argv": self.target_argv})

    def tearDown(self):
        os.close(self.lock_fd)
        os.close(self.state_fd)
        self.temp.cleanup()

    def start_gate(self, token):
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        proc = subprocess.Popen([sys.executable, "-I", str(GATE), "--release-fd", str(read_fd),
                                 "--token", token, "--target", self.target_name,
                                 "--identity", self.identity_name, "--state-dir-fd", str(self.state_fd)],
                                pass_fds=(read_fd, self.state_fd), close_fds=True, start_new_session=True,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        os.close(read_fd)
        return proc, write_fd

    def identity_for_gate(self, proc):
        gate = parent_state.observe_process(proc.pid)
        return parent_state.create_exec_transition(
            gate, self.target_argv[0], self.target_argv, release_token=self.current_token,
            target_path=self.target_name, identity_path=self.identity_name)

    def test_eof_before_durable_authorization_exits_without_workload(self):
        token = "0123456789abcdef0123456789abcdef"
        self.current_token = token
        proc, write_fd = self.start_gate(token)
        try:
            identity = self.identity_for_gate(proc)
            self.assertEqual(parent_state.exec_transition_status(identity), "GATE")
            os.close(write_fd)  # Supervisor death before commit: gate self-exits.
            self.assertEqual(proc.wait(timeout=3), 0)
            self.assertEqual(parent_state.exec_transition_status(identity), "STOPPED")
            self.assertFalse(self.marker.exists())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)
            try:
                os.close(write_fd)
            except OSError:
                pass

    def test_durable_transition_then_release_execs_same_pid_and_is_pidfd_stoppable(self):
        token = "abcdef0123456789abcdef0123456789"
        self.current_token = token
        proc, write_fd = self.start_gate(token)
        try:
            identity = self.identity_for_gate(proc)
            oversized = {**identity, "pid": 2**100}
            self.assertEqual(parent_state.exec_transition_status(oversized), "AMBIGUOUS")
            self.assertEqual(parent_state.stop_exec_transition(oversized), "AMBIGUOUS")
            parent_state.persist_exec_transition(self.state_fd, self.identity_name, identity, lock_fd=self.lock_fd)
            persisted = parent_state._read_json(self.state_fd, self.identity_name)
            self.assertEqual(persisted, identity)
            self.assertEqual(parent_state.exec_transition_status(identity), "GATE")
            os.write(write_fd, token.encode("ascii"))
            os.close(write_fd)
            write_fd = -1
            deadline = time.monotonic() + 3
            phase = "GATE"
            while time.monotonic() < deadline and phase == "GATE":
                phase = parent_state.exec_transition_status(identity)
                time.sleep(0.01)
            self.assertEqual(phase, "TARGET")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not self.marker.exists() and proc.poll() is None:
                time.sleep(0.01)
            self.assertTrue(self.marker.exists())
            self.assertEqual(proc.pid, identity["pid"])
            self.assertEqual(parent_state.stop_exec_transition(identity, term_grace_ms=1000), "STOPPED")
            self.assertEqual(proc.wait(timeout=3), -15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)
            if write_fd >= 0:
                os.close(write_fd)

    def test_state_commit_failure_leaves_gate_unreleased_and_workload_absent(self):
        token = "fedcba9876543210fedcba9876543210"
        self.current_token = token
        proc, write_fd = self.start_gate(token)
        try:
            identity = self.identity_for_gate(proc)
            with mock.patch.object(parent_state.os, "fsync", side_effect=OSError("injected fsync failure")):
                with self.assertRaises(OSError):
                    parent_state.persist_exec_transition(self.state_fd, self.identity_name, identity,
                                                         lock_fd=self.lock_fd)
            self.assertFalse((self.state_dir / self.identity_name).exists())
            os.close(write_fd)
            write_fd = -1
            self.assertEqual(proc.wait(timeout=3), 0)
            self.assertFalse(self.marker.exists())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)
            if write_fd >= 0:
                os.close(write_fd)

    def test_full_release_token_without_durable_identity_cannot_exec(self):
        token = "0123456789abcdef0123456789abcdef"
        self.current_token = token
        proc, write_fd = self.start_gate(token)
        try:
            os.write(write_fd, token.encode("ascii"))
            os.close(write_fd)
            write_fd = -1
            self.assertEqual(proc.wait(timeout=3), 2)
            self.assertFalse(self.marker.exists())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)
            if write_fd >= 0:
                os.close(write_fd)

    def test_incomplete_or_wrong_authorization_never_executes_target(self):
        token = "0123456789abcdef0123456789abcdef"
        proc, write_fd = self.start_gate(token)
        try:
            os.write(write_fd, b"ffffffffffffffffffffffffffffffff")
            os.close(write_fd)
            write_fd = -1
            self.assertEqual(proc.wait(timeout=3), 2)
            self.assertFalse(self.marker.exists())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)
            if write_fd >= 0:
                os.close(write_fd)


if __name__ == "__main__":
    unittest.main()
