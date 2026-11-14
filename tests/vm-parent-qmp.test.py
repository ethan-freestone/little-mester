#!/usr/bin/env python3
"""REAL_PROCESS tests for peer-bound bounded QMP shutdown framing."""

import importlib.util
import os
import pathlib
import stat
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
SUPERVISOR_SPEC = importlib.util.spec_from_file_location("parent_supervisor", ROOT / "lib/vm/parent-supervisor.py")
parent_supervisor = importlib.util.module_from_spec(SUPERVISOR_SPEC)
SUPERVISOR_SPEC.loader.exec_module(parent_supervisor)


SERVER = r'''import json, os, socket, sys
path = sys.argv[1]
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(path)
server.listen(1)
connection, _ = server.accept()
with connection:
    connection.sendall(b'{"QMP":{"version":{},"capabilities":[]}}\r\n')
    stream = connection.makefile("rb")
    for expected in ("qmp_capabilities", "system_powerdown"):
        request = json.loads(stream.readline())
        if request.get("execute") != expected:
            raise SystemExit(3)
        connection.sendall(b'{"return":{}}\r\n')
server.close()
'''

SLOW_SERVER = SERVER.replace(
    'connection.sendall(b\'{"return":{}}\\r\\n\')',
    'time.sleep(0.14); connection.sendall(b\'{"return":{}}\\r\\n\')',
).replace("import json, os, socket, sys", "import json, os, socket, sys, time")


class QmpShutdownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.socket_path = str(pathlib.Path(self.temp.name) / "qmp.sock")
        self.peer = subprocess.Popen([sys.executable, "-I", "-c", SERVER, self.socket_path],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, preexec_fn=lambda: os.umask(0o077))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not pathlib.Path(self.socket_path).exists():
            if self.peer.poll() is not None:
                self.fail("fake QMP peer exited before binding")
            time.sleep(0.01)
        self.assertTrue(pathlib.Path(self.socket_path).exists())
        mode = stat.S_IMODE(pathlib.Path(self.socket_path).stat().st_mode)
        self.assertEqual(mode & 0o077, 0)
        self.assertEqual(mode & 0o600, 0o600)
        self.identity = {"pid": self.peer.pid, "uid": os.getuid(),
                         "host_boot_id": pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()}

    def tearDown(self):
        if self.peer.poll() is None:
            self.peer.terminate()
        self.peer.wait(timeout=3)
        self.temp.cleanup()

    def test_exact_peer_receives_capabilities_then_graceful_powerdown(self):
        with mock.patch.object(parent_state, "_recorded_process_status", return_value="MATCH"):
            parent_supervisor._qmp_powerdown(parent_state, self.socket_path, self.identity)
        self.assertEqual(self.peer.wait(timeout=3), 0)

    def test_mismatched_recorded_peer_is_refused_before_commands(self):
        self.identity["pid"] += 1
        with self.assertRaisesRegex(parent_state.Refused, "peer does not match"):
            parent_supervisor._qmp_powerdown(parent_state, self.socket_path, self.identity)
        self.peer.terminate()
        self.assertNotEqual(self.peer.wait(timeout=3), 0)

    def test_unsafe_socket_mode_is_refused(self):
        os.chmod(self.socket_path, 0o666)
        with self.assertRaisesRegex(parent_state.Refused, "unsafe owner, type, or mode"):
            parent_supervisor._qmp_powerdown(parent_state, self.socket_path, self.identity)

    def test_total_deadline_bounds_multiple_slow_responses(self):
        self.peer.terminate()
        self.peer.wait(timeout=3)
        pathlib.Path(self.socket_path).unlink(missing_ok=True)
        self.peer = subprocess.Popen([sys.executable, "-I", "-c", SLOW_SERVER, self.socket_path],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, preexec_fn=lambda: os.umask(0o077))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not pathlib.Path(self.socket_path).exists():
            time.sleep(0.01)
        self.identity["pid"] = self.peer.pid
        started = time.monotonic()
        with mock.patch.object(parent_state, "_recorded_process_status", return_value="MATCH"):
            with self.assertRaisesRegex(parent_state.Refused, "bounded"):
                parent_supervisor._qmp_powerdown(
                    parent_state, self.socket_path, self.identity, timeout_seconds=0.25)
        self.assertLess(time.monotonic() - started, 0.4)


if __name__ == "__main__":
    unittest.main()
