#!/usr/bin/env python3
"""Fast signal-path regressions for the credential-free QEMU OAuth harness."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import signal
import shutil
import tempfile
import types
import unittest
from unittest import mock

SCRIPT = pathlib.Path(__file__).with_name("ci-qemu-oauth-bridge.py")
SPEC = importlib.util.spec_from_file_location("ci_qemu_oauth_bridge", SCRIPT)
HARNESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HARNESS)
PROCESS_SPEC = importlib.util.spec_from_file_location(
    "ci_process_signal", pathlib.Path(__file__).with_name("ci-process-signal.py")
)
PROCESS = importlib.util.module_from_spec(PROCESS_SPEC)
PROCESS_SPEC.loader.exec_module(PROCESS)


class HarnessSignalTests(unittest.TestCase):
    def test_pidfd_signal_requires_the_captured_child_identity(self):
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            self.skipTest("Linux pidfd signaling is unavailable")
        child = os.fork()
        if child == 0:
            while True:
                signal.pause()
        try:
            parent, starttime, comm, _cmdline = PROCESS.process_identity(child)
            uid = os.getuid()
            self.assertEqual(parent, os.getpid())
            self.assertFalse(PROCESS.signal_owned_process(
                child, starttime + 1, uid, parent, comm, (), signal.SIGTERM
            ))
            waited, _status = os.waitpid(child, os.WNOHANG)
            self.assertEqual(waited, 0, "identity mismatch must not signal the live child")
            self.assertTrue(PROCESS.signal_owned_process(
                child, starttime, uid, parent, comm, (), signal.SIGTERM
            ))
            waited, status = os.waitpid(child, 0)
            self.assertEqual(waited, child)
            self.assertTrue(os.WIFSIGNALED(status))
        finally:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(child, 0)
            except ChildProcessError:
                pass

    def test_sigterm_during_active_case_runs_session_close(self):
        closed = []

        class FakeSession:
            def __init__(self, *_args):
                pass

            def start(self):
                os.kill(os.getpid(), signal.SIGTERM)

            def close(self):
                closed.append(True)

        previous = HARNESS.install_signal_handlers()
        try:
            with mock.patch.object(HARNESS, "Session", FakeSession):
                with self.assertRaises(HARNESS.HarnessCancelled) as raised:
                    HARNESS.run_case(
                        types.SimpleNamespace(port=12345, identity="unused", known_hosts="unused"),
                        "success",
                    )
            self.assertEqual(raised.exception.signum, signal.SIGTERM)
            self.assertEqual(closed, [True])
        finally:
            HARNESS.restore_signal_handlers(previous)

    def test_first_termination_during_normal_close_is_reported_after_cleanup(self):
        with tempfile.TemporaryDirectory(prefix="oauth-harness-signal-") as directory:
            session = HARNESS.Session.__new__(HARNESS.Session)
            session.case = "signal-cleanup"
            session.pid = -1
            session.master_fd = -1
            session.exit_status = 0
            session.forced_kill = False
            session.closed = False
            session.proof = pathlib.Path(directory) / "proof.json"
            session.proof.write_text(json.dumps({
                "ssh_stopped": True,
                "control_socket_absent": True,
                "listener_verified_closed": True,
                "cleanup_complete": True,
            }))
            cleanup_calls = []
            session.root = pathlib.Path(directory) / "owned-session"
            session.root.mkdir()
            session.root.joinpath("identity").write_text("synthetic")

            def interrupt_and_signal():
                os.kill(os.getpid(), signal.SIGTERM)

            session.interrupt = interrupt_and_signal
            # Exercise the real successful cleanup path without a TemporaryDirectory
            # finalizer; explicit removal is performed only after proof validation.
            with mock.patch.object(HARNESS.shutil, "rmtree", side_effect=lambda _path: cleanup_calls.append(True)):
                previous = HARNESS.install_signal_handlers()
                try:
                    with self.assertRaises(HARNESS.HarnessCancelled) as raised:
                        session.close()
                    self.assertEqual(raised.exception.signum, signal.SIGTERM)
                    self.assertEqual(cleanup_calls, [True])
                    self.assertTrue(session.closed)
                finally:
                    HARNESS.restore_signal_handlers(previous)

    def test_failed_cleanup_proof_retains_owned_temp_directory(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="oauth-harness-retained-"))
        try:
            session = HARNESS.Session.__new__(HARNESS.Session)
            session.case = "missing-proof"
            session.pid = -1
            session.master_fd = -1
            session.exit_status = 0
            session.forced_kill = False
            session.closed = False
            session.root = root
            session.proof = root / "missing-proof.json"
            (root / "identity_file").write_text("synthetic private fixture")
            with self.assertRaisesRegex(RuntimeError, "artifacts retained"):
                session.close()
            self.assertTrue(root.is_dir())
            self.assertTrue((root / "identity_file").is_file())
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_signal_during_pty_fork_is_deferred_until_handles_are_published(self):
        session = HARNESS.Session.__new__(HARNESS.Session)
        session.pid = -1
        session.master_fd = -1
        session.command = ["/unused"]
        previous = HARNESS.install_signal_handlers()

        def fork_then_signal():
            os.kill(os.getpid(), signal.SIGTERM)
            return 43210, 73

        try:
            with mock.patch.object(HARNESS.pty, "fork", side_effect=fork_then_signal):
                with self.assertRaises(HARNESS.HarnessCancelled):
                    session.start()
            self.assertEqual(session.pid, 43210)
            self.assertEqual(session.master_fd, 73)
        finally:
            HARNESS.restore_signal_handlers(previous)

    def test_pty_wait_error_still_kills_and_reaps_owned_child(self):
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read_fd)
            os.setpgid(0, 0)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            os.write(write_fd, b"ready")
            while True:
                signal.pause()
        os.close(write_fd)
        try:
            self.assertEqual(os.read(read_fd, 5), b"ready")
            session = HARNESS.Session.__new__(HARNESS.Session)
            session.case = "pty-read-error"
            session.pid = child
            session.exit_status = None
            session.forced_kill = False
            session.wait = mock.Mock(side_effect=OSError("synthetic PTY capture failure"))
            with self.assertRaisesRegex(OSError, "synthetic PTY capture failure"):
                session.interrupt()
            self.assertEqual(session.pid, -1)
            with self.assertRaises(ChildProcessError):
                os.waitpid(child, os.WNOHANG)
        finally:
            os.close(read_fd)
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(child, 0)
            except ChildProcessError:
                pass


if __name__ == "__main__":
    unittest.main()
