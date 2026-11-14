#!/usr/bin/env python3
"""Fast signal-path regressions for the credential-free QEMU OAuth harness."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import signal
import tempfile
import types
import unittest
from unittest import mock

SCRIPT = pathlib.Path(__file__).with_name("ci-qemu-oauth-bridge.py")
SPEC = importlib.util.spec_from_file_location("ci_qemu_oauth_bridge", SCRIPT)
HARNESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HARNESS)


class HarnessSignalTests(unittest.TestCase):
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

    def test_close_ignores_second_termination_while_verifying_cleanup(self):
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
            session.temp = types.SimpleNamespace(cleanup=lambda: cleanup_calls.append(True))

            def interrupt_and_signal():
                os.kill(os.getpid(), signal.SIGTERM)

            session.interrupt = interrupt_and_signal
            previous = HARNESS.install_signal_handlers()
            try:
                session.close()
                self.assertEqual(cleanup_calls, [True])
                self.assertTrue(session.closed)
            finally:
                HARNESS.restore_signal_handlers(previous)


if __name__ == "__main__":
    unittest.main()
