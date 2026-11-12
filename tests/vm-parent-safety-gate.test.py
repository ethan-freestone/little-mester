#!/usr/bin/env python3
"""Focused tests for the read-only parent safety-gate process scanner."""

import importlib.util
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "parent_readonly_safety_gate", ROOT / "scripts/parent-readonly-safety-gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class ProcessScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self._process(1)
        self._process(2)
        self._process(3)

    def tearDown(self):
        self.temp.cleanup()

    def _process(self, pid):
        (self.proc / str(pid) / "fd").mkdir(parents=True)

    def test_process_fixture_with_no_holders_passes(self):
        holders = gate.scan_open_inode(str(self.proc), 123, 456, scanner_pid=2)
        self.assertEqual(holders, [])

    def test_empty_process_tree_is_refused(self):
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(gate.GateRefused, "empty/incomplete"):
            gate.scan_open_inode(str(empty), 123, 456, scanner_pid=2)

    def test_scan_missing_pid_one_or_scanner_pid_is_refused(self):
        (self.proc / "1").rename(self.proc / "10")
        with self.assertRaisesRegex(gate.GateRefused, "PID 1 or scanner PID"):
            gate.scan_open_inode(str(self.proc), 123, 456, scanner_pid=2)

    def test_inaccessible_live_fd_table_is_refused_even_if_holder_seen(self):
        real_scandir = os.scandir

        def scandir(path):
            if os.fspath(path) == str(self.proc / "2" / "fd"):
                raise PermissionError("fixture inaccessible FD table")
            return real_scandir(path)

        with tempfile.NamedTemporaryFile() as held:
            info = os.fstat(held.fileno())
            (self.proc / "1" / "fd" / "9").symlink_to(held.name)
            with mock.patch.object(gate.os, "scandir", side_effect=scandir):
                with self.assertRaisesRegex(gate.GateRefused, "cannot inspect process 2 FD table"):
                    gate.scan_open_inode(str(self.proc), info.st_dev, info.st_ino, scanner_pid=2)

    def test_real_locally_held_temporary_file_is_detected(self):
        with tempfile.NamedTemporaryFile() as held:
            info = os.fstat(held.fileno())
            # The fixture's PID 1 holds a proc-style descriptor symlink to a
            # real temporary file that remains open throughout the scan.
            live_fd = pathlib.Path(f"/proc/{os.getpid()}/fd/{held.fileno()}")
            (self.proc / "1" / "fd" / "17").symlink_to(live_fd)
            self.assertEqual(
                gate.scan_open_inode(str(self.proc), info.st_dev, info.st_ino, scanner_pid=2),
                [1],
            )

    def test_incomplete_scan_cannot_hide_a_matching_open_descriptor(self):
        with tempfile.NamedTemporaryFile() as held:
            info = os.fstat(held.fileno())
            (self.proc / "1" / "fd" / "17").symlink_to(held.name)
            # Removing the scanner PID makes the entire result invalid rather
            # than allowing a holder result from a partial process enumeration.
            (self.proc / "2").rename(self.proc / "4")
            with self.assertRaisesRegex(gate.GateRefused, "empty/incomplete"):
                gate.scan_open_inode(str(self.proc), info.st_dev, info.st_ino, scanner_pid=2)


class OpenSSHHostLookupTests(unittest.TestCase):
    def test_parent_bracketed_loopback_port_matches_only_that_port(self):
        keygen = shutil.which("ssh-keygen")
        ssh = shutil.which("ssh")
        if not keygen or not ssh:
            self.skipTest("OpenSSH tools unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            private = root / "host"
            subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(private)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10)
            key_type, public_key, *_ = private.with_suffix(".pub").read_text().split()
            known_hosts = root / "known_hosts"
            known_hosts.write_text(f"[127.0.0.1]:46617 {key_type} {public_key}\n")
            exact = subprocess.run([keygen, "-F", "[127.0.0.1]:46617", "-f", str(known_hosts)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=5, check=False)
            other_port = subprocess.run(
                [keygen, "-F", "[127.0.0.1]:46618", "-f", str(known_hosts)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
            self.assertEqual(exact.returncode, 0)
            self.assertNotEqual(other_port.returncode, 0)
            config = subprocess.run([ssh, "-G", "-F", "/dev/null", "-p", "46617",
                                     "mester@127.0.0.1"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, timeout=5, check=True).stdout.splitlines()
            values = {line.split(None, 1)[0]: line.split(None, 1)[1]
                      for line in config if line.startswith(("hostname ", "port ", "hostkeyalias "))}
            self.assertEqual(values.get("hostname"), "127.0.0.1")
            self.assertEqual(values.get("port"), "46617")
            self.assertNotIn("hostkeyalias", values)


if __name__ == "__main__":
    unittest.main()
