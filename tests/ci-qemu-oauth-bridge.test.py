#!/usr/bin/env python3
"""Fast signal-path regressions for the credential-free QEMU OAuth harness."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import signal
import shutil
import sys
import tempfile
import time
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
    def test_unreadable_proc_entry_does_not_prove_session_empty(self):
        entry = pathlib.Path("/proc/424242")
        with mock.patch.object(pathlib.Path, "iterdir", return_value=iter([entry])), \
             mock.patch.object(pathlib.Path, "read_text", side_effect=PermissionError("hidden proc")), \
             mock.patch.object(pathlib.Path, "stat", return_value=object()):
            self.assertTrue(HARNESS.process_session_has_live_members(424242))

    def test_shell_process_group_scan_fails_closed_on_unreadable_proc(self):
        entry = pathlib.Path("/proc/424243")
        with mock.patch.object(pathlib.Path, "iterdir", return_value=iter([entry])), \
             mock.patch.object(pathlib.Path, "read_text", side_effect=PermissionError("hidden proc")), \
             mock.patch.object(pathlib.Path, "stat", return_value=object()):
            self.assertFalse(PROCESS.process_session_empty(424243))

    def test_session_records_short_control_directory_before_creating_it(self):
        with tempfile.TemporaryDirectory(prefix="oauth-evidence-root-") as directory:
            root = pathlib.Path(directory)
            key = root / "identity"
            known_hosts = root / "known_hosts"
            key.write_text("synthetic identity")
            known_hosts.write_text("synthetic host key")
            session = HARNESS.Session(2222, key, known_hosts, "constructor", root)
            try:
                marker = session.root / "control-directory"
                self.assertEqual(pathlib.Path(marker.read_text()), session.control_dir)
                self.assertTrue(session.control_dir.is_dir())
                self.assertLess(len(os.fsencode(session.control_socket)), HARNESS.AUTH.CONTROL_SOCKET_LIMIT)
            finally:
                shutil.rmtree(session.control_dir, ignore_errors=True)
                shutil.rmtree(session.root, ignore_errors=True)

    def test_startup_setup_failure_keeps_unverified_bridge_behind_launch_gate(self):
        with tempfile.TemporaryDirectory(prefix="oauth-start-gate-") as directory:
            root = pathlib.Path(directory)
            key = root / "identity"
            known_hosts = root / "known_hosts"
            key.write_text("synthetic identity")
            known_hosts.write_text("synthetic host key")
            marker = root / "bridge-executed"
            command = ["/usr/bin/python3", "-c",
                       f"from pathlib import Path; Path({str(marker)!r}).write_text('executed')"]
            original_fork = HARNESS.pty.fork
            for failure in ("pidfd", "identity"):
                session = HARNESS.Session(2222, key, known_hosts, f"gate-{failure}", root)
                session.command = command
                child_pids = []

                def fork_and_capture():
                    pid, master_fd = original_fork()
                    if pid > 0:
                        child_pids.append(pid)
                    return pid, master_fd

                pidfd_patch = (mock.patch.object(HARNESS.os, "pidfd_open", side_effect=OSError("injected pidfd failure"))
                               if failure == "pidfd" else mock.patch.object(HARNESS.os, "pidfd_open", wraps=os.pidfd_open))
                identity_patch = (mock.patch.object(HARNESS, "write_bridge_identity",
                                                    side_effect=OSError("injected marker failure"))
                                  if failure == "identity" else mock.patch.object(HARNESS, "write_bridge_identity",
                                                                                 wraps=HARNESS.write_bridge_identity))
                try:
                    with mock.patch.object(HARNESS.pty, "fork", side_effect=fork_and_capture), \
                         pidfd_patch, identity_patch:
                        with self.assertRaises(OSError):
                            session.start()
                    self.assertEqual(session.pid, -1)
                    self.assertEqual(len(child_pids), 1)
                    self.assertFalse(marker.exists(), "unverified child must not execute")
                    with self.assertRaises(ChildProcessError):
                        os.waitpid(child_pids[0], os.WNOHANG)
                finally:
                    shutil.rmtree(session.control_dir, ignore_errors=True)
                    shutil.rmtree(session.root, ignore_errors=True)

    def test_pidfd_signal_requires_the_captured_child_identity(self):
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            self.skipTest("Linux pidfd signaling is unavailable")
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read_fd)
            os.setsid()
            os.write(write_fd, b"ready")
            while True:
                signal.pause()
        os.close(write_fd)
        try:
            self.assertEqual(os.read(read_fd, 5), b"ready")
            parent, process_group, session_id, starttime, comm, _cmdline = PROCESS.process_identity(child)
            uid = os.getuid()
            self.assertEqual(parent, os.getpid())
            self.assertEqual(process_group, child)
            self.assertFalse(PROCESS.signal_owned_process(
                child, starttime + 1, uid, parent, comm, (), signal.SIGTERM,
                process_group=True, expected_session_id=session_id,
            ))
            waited, _status = os.waitpid(child, os.WNOHANG)
            self.assertEqual(waited, 0, "identity mismatch must not signal the live child")
            with tempfile.TemporaryDirectory(prefix="oauth-bridge-group-") as directory:
                evidence_root = pathlib.Path(directory)
                HARNESS.write_bridge_identity(evidence_root / "bridge-process.json", child)
                self.assertEqual(PROCESS.main(["--terminate-marked-groups", str(evidence_root)]), 0)
            waited, status = os.waitpid(child, 0)
            self.assertEqual(waited, child)
            self.assertTrue(os.WIFSIGNALED(status))
            self.assertTrue(PROCESS.process_session_empty(session_id))
        finally:
            os.close(read_fd)
            try:
                os.killpg(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(child, 0)
            except ChildProcessError:
                pass

    def test_shell_group_recovery_kills_descendant_after_bridge_leader_exits(self):
        read_fd, write_fd = os.pipe()
        leader = os.fork()
        if leader == 0:
            os.close(read_fd)
            os.setsid()
            descendant = os.fork()
            if descendant == 0:
                os.close(write_fd)
                while True:
                    signal.pause()
            os.write(write_fd, str(descendant).encode("ascii"))
            while True:
                signal.pause()
        os.close(write_fd)
        descendant_pid = -1
        try:
            descendant_pid = int(os.read(read_fd, 32))
            parent, process_group, _session, _starttime, _comm, _args = PROCESS.process_identity(leader)
            self.assertEqual(process_group, leader)
            self.assertEqual(parent, os.getpid())
            with tempfile.TemporaryDirectory(prefix="oauth-orphan-group-") as directory:
                evidence_root = pathlib.Path(directory)
                HARNESS.write_bridge_identity(evidence_root / "bridge-process.json", leader)
                os.kill(leader, signal.SIGKILL)
                os.waitpid(leader, 0)
                self.assertEqual(PROCESS.main(["--terminate-marked-groups", str(evidence_root)]), 1)
                # The CLI has no lifetime pidfd for the dead leader, so it
                # must not act on a numeric PGID/SID alone.
                _parent, _group, _sid, start, comm, _args = PROCESS.process_identity(descendant_pid)
                self.assertTrue(PROCESS.signal_owned_process(
                    descendant_pid, start, os.getuid(), None, comm, (), signal.SIGKILL
                ))
                deadline = time.monotonic() + 2
                while not PROCESS.process_session_empty(leader) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(PROCESS.process_session_empty(leader))
        finally:
            os.close(read_fd)
            try:
                os.killpg(leader, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(leader, 0)
            except ChildProcessError:
                pass

    def test_group_termination_stops_members_before_final_absence_proof(self):
        read_fd, write_fd = os.pipe()
        leader = os.fork()
        if leader == 0:
            os.close(read_fd)
            os.setsid()
            forker = os.fork()
            if forker == 0:
                os.close(write_fd)
                for _ in range(80):
                    child = os.fork()
                    if child == 0:
                        while True:
                            signal.pause()
                    time.sleep(0.001)
                while True:
                    signal.pause()
            os.write(write_fd, b"ready")
            while True:
                signal.pause()
        os.close(write_fd)
        try:
            self.assertEqual(os.read(read_fd, 5), b"ready")
            with tempfile.TemporaryDirectory(prefix="oauth-fork-race-") as directory:
                root = pathlib.Path(directory)
                HARNESS.write_bridge_identity(root / "bridge-process.json", leader)
                self.assertEqual(PROCESS.main(["--terminate-marked-groups", str(root)]), 0)
                self.assertFalse(HARNESS.process_session_has_live_members(leader))
                waited, status = os.waitpid(leader, 0)
                self.assertEqual(waited, leader)
                self.assertTrue(os.WIFSIGNALED(status))
        finally:
            os.close(read_fd)
            try:
                _parent, _group, _sid, start, comm, _args = PROCESS.process_identity(leader)
                PROCESS.signal_owned_process(
                    leader, start, os.getuid(), None, comm, (), signal.SIGKILL
                )
            except (OSError, RuntimeError, StopIteration, ValueError):
                pass
            try:
                os.waitpid(leader, 0)
            except ChildProcessError:
                pass

    def test_session_cleanup_includes_descendant_that_changed_process_group(self):
        read_fd, write_fd = os.pipe()
        leader = os.fork()
        if leader == 0:
            os.close(read_fd)
            os.setsid()
            child = os.fork()
            if child == 0:
                os.setpgid(0, 0)
                os.write(write_fd, str(os.getpid()).encode("ascii"))
                os.close(write_fd)
                while True:
                    signal.pause()
            while True:
                signal.pause()
        os.close(write_fd)
        try:
            escaped_group_child = int(os.read(read_fd, 32))
            _parent, child_group, child_session, _start, _comm, _args = PROCESS.process_identity(escaped_group_child)
            self.assertNotEqual(child_group, leader)
            self.assertEqual(child_session, leader)
            with tempfile.TemporaryDirectory(prefix="oauth-session-escape-") as directory:
                root = pathlib.Path(directory)
                HARNESS.write_bridge_identity(root / "bridge-process.json", leader)
                self.assertEqual(PROCESS.main(["--terminate-marked-groups", str(root)]), 0)
                self.assertTrue(PROCESS.process_session_empty(leader))
                waited, status = os.waitpid(leader, 0)
                self.assertEqual(waited, leader)
                self.assertTrue(os.WIFSIGNALED(status))
        finally:
            os.close(read_fd)
            for pid in (leader, locals().get("escaped_group_child", -1)):
                try:
                    _parent, _group, _sid, start, comm, _args = PROCESS.process_identity(pid)
                    PROCESS.signal_owned_process(pid, start, os.getuid(), None, comm, (), signal.SIGKILL)
                except (OSError, RuntimeError, StopIteration, ValueError):
                    pass
            try:
                os.waitpid(leader, 0)
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
                        types.SimpleNamespace(port=12345, identity="unused", known_hosts="unused",
                                              evidence_root="/tmp"),
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
            session.bridge_session_id = -1
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
            session.control_dir = pathlib.Path(directory) / "control-dir"
            session.control_dir.mkdir()

            session.interrupt = lambda: None
            # Exercise the real successful cleanup path without a TemporaryDirectory
            # finalizer; explicit removal is performed only after proof validation.
            with mock.patch.object(HARNESS.shutil, "rmtree", side_effect=lambda _path: cleanup_calls.append(True)):
                previous = HARNESS.install_signal_handlers()
                original_signal = signal.signal
                delivered = []

                def install_and_deliver(signum, handler):
                    result = original_signal(signum, handler)
                    if signum == signal.SIGTERM and getattr(handler, "__name__", "") == "<lambda>" and not delivered:
                        delivered.append(True)
                        os.kill(os.getpid(), signal.SIGTERM)
                    return result

                try:
                    with mock.patch.object(HARNESS.signal, "signal", side_effect=install_and_deliver):
                        with self.assertRaises(HARNESS.HarnessCancelled) as raised:
                            session.close()
                    self.assertEqual(raised.exception.signum, signal.SIGTERM)
                    self.assertEqual(cleanup_calls, [True, True])
                    self.assertTrue(session.closed)
                finally:
                    HARNESS.restore_signal_handlers(previous)

    def test_signal_during_top_level_handler_installation_is_masked(self):
        previous_process_handlers = {
            signum: signal.signal(signum, signal.SIG_DFL)
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM)
        }
        sent = []
        original_signal = signal.signal

        def install_and_signal(signum, handler):
            result = original_signal(signum, handler)
            if signum == signal.SIGTERM and not sent:
                sent.append(True)
                os.kill(os.getpid(), signal.SIGTERM)
            return result

        try:
            with mock.patch.object(HARNESS.signal, "signal", side_effect=install_and_signal), \
                 mock.patch.object(HARNESS, "run_case") as run_case, \
                 mock.patch.object(sys, "argv", ["ci-qemu-oauth-bridge.py", "--port", "1",
                                                  "--identity", "unused", "--known-hosts", "unused",
                                                  "--evidence-root", "/tmp"]):
                self.assertEqual(HARNESS.main(), 128 + signal.SIGTERM)
                run_case.assert_not_called()
        finally:
            signal.alarm(0)
            for signum, handler in previous_process_handlers.items():
                original_signal(signum, handler)

    def test_failed_cleanup_proof_retains_owned_temp_directory(self):
        root = pathlib.Path(tempfile.mkdtemp(prefix="oauth-harness-retained-"))
        try:
            session = HARNESS.Session.__new__(HARNESS.Session)
            session.case = "missing-proof"
            session.pid = -1
            session.bridge_session_id = -1
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

    def test_normal_close_stops_orphaned_group_member_and_retains_evidence(self):
        read_fd, write_fd = os.pipe()
        leader = os.fork()
        if leader == 0:
            os.close(read_fd)
            os.setsid()
            descendant = os.fork()
            if descendant == 0:
                os.close(write_fd)
                while True:
                    signal.pause()
            os.write(write_fd, str(descendant).encode("ascii"))
            while True:
                signal.pause()
        os.close(write_fd)
        try:
            descendant = int(os.read(read_fd, 32))
            with tempfile.TemporaryDirectory(prefix="oauth-normal-close-group-") as directory:
                root = pathlib.Path(directory) / "session"
                root.mkdir(mode=0o700)
                record = HARNESS.write_bridge_identity(root / "bridge-process.json", leader)
                proof = root / "proof.json"
                proof.write_text(json.dumps({
                    "ssh_stopped": True,
                    "control_socket_absent": True,
                    "listener_verified_closed": True,
                    "cleanup_complete": True,
                }))
                session = HARNESS.Session.__new__(HARNESS.Session)
                session.case = "orphan-descendant"
                session.pid = -1
                session.bridge_session_id = leader
                session.bridge_pidfd = os.pidfd_open(leader, 0)
                session.bridge_anchor = (leader, record["starttime"])
                session.master_fd = -1
                session.exit_status = 0
                session.forced_kill = False
                session.closed = False
                session.root = root
                session.proof = proof
                session.control_dir = pathlib.Path(directory) / "control-dir"
                session.control_dir.mkdir()
                session.interrupt = lambda: None
                os.kill(leader, signal.SIGKILL)
                os.waitpid(leader, 0)
                with self.assertRaisesRegex(RuntimeError, "surviving descendants"):
                    session.close()
                self.assertTrue(root.is_dir(), "unexpected survivor must retain its evidence")
                self.assertTrue(PROCESS.process_session_empty(leader))
        finally:
            os.close(read_fd)
            try:
                _parent, _group, _sid, start, comm, _args = PROCESS.process_identity(leader)
                PROCESS.signal_owned_process(leader, start, os.getuid(), None, comm, (), signal.SIGKILL)
            except (OSError, RuntimeError, StopIteration, ValueError):
                pass
            try:
                os.waitpid(leader, 0)
            except ChildProcessError:
                pass

    def test_signal_during_pty_fork_is_deferred_until_handles_are_published(self):
        session = HARNESS.Session.__new__(HARNESS.Session)
        session.pid = -1
        session.bridge_session_id = -1
        session.master_fd = -1
        session.command = ["/unused"]
        session.bridge_identity = pathlib.Path("/unused/bridge-process.json")
        previous = HARNESS.install_signal_handlers()

        def fork_then_signal():
            os.kill(os.getpid(), signal.SIGTERM)
            return 43210, 73

        try:
            with mock.patch.object(HARNESS.pty, "fork", side_effect=fork_then_signal), \
                 mock.patch.object(HARNESS, "bridge_identity_record",
                                   return_value={"pid": 43210, "starttime": 1, "uid": os.getuid(),
                                                 "process_group": 43210, "session_id": 43210}), \
                 mock.patch.object(HARNESS, "write_bridge_identity", return_value={"starttime": 1}), \
                 mock.patch.object(HARNESS.os, "pidfd_open", return_value=91), \
                 mock.patch.object(HARNESS.os, "write", return_value=1), \
                 mock.patch.object(HARNESS.os, "close", wraps=os.close), \
                 mock.patch.object(HARNESS.signal, "pidfd_send_signal"):
                with self.assertRaises(HARNESS.HarnessCancelled):
                    session.start()
            self.assertEqual(session.pid, 43210,
                             (session.bridge_pidfd, session.bridge_anchor, session.master_fd))
            self.assertEqual(session.master_fd, 73)
            self.assertEqual(session.bridge_session_id, 43210)
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
            session.bridge_session_id = child
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
