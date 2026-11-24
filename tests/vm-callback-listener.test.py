#!/usr/bin/env python3
"""Offline regressions for the shared callback-listener proof helper."""

from __future__ import annotations

import importlib.util
import os
import pathlib
import socket
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER = ROOT / "lib/vm/callback-listener.py"
SPEC = importlib.util.spec_from_file_location("callback_listener_test", HELPER)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CallbackListenerTests(unittest.TestCase):
    def test_invalid_port_values_fail_closed(self):
        for port in (0, 1023, 65536, "1234", True):
            with self.subTest(port=port):
                self.assertFalse(MODULE.listener_absent(port))

    def test_private_regular_port_file_contract_and_cli_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "port"
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            path.write_text(f"{port}\n", encoding="ascii")
            path.chmod(0o600)
            result = subprocess.run(
                ["python3", "-I", str(HELPER), str(path)],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

            path.write_text("9" * 100_000, encoding="ascii")
            self.assertFalse(MODULE.port_file_listener_absent(path))
            self.assertNotEqual(
                subprocess.run(
                    ["python3", "-I", str(HELPER), str(path)],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                ).returncode,
                0,
            )

    def test_symlink_and_wrong_mode_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            target = root / "target"
            target.write_text("23456\n", encoding="ascii")
            target.chmod(0o600)
            link = root / "link"
            link.symlink_to(target)
            self.assertFalse(MODULE.port_file_listener_absent(link))
            target.chmod(0o640)
            self.assertFalse(MODULE.port_file_listener_absent(target))

    def test_loopback_listener_is_detected(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            self.assertFalse(MODULE.listener_absent(listener.getsockname()[1]))

    def test_ipv6_wildcard_listener_is_detected_when_available(self):
        try:
            listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            listener.bind(("::", 0))
            listener.listen()
        except OSError as error:
            self.skipTest(f"IPv6 wildcard listener unavailable: {error}")
        with listener:
            self.assertFalse(MODULE.listener_absent(listener.getsockname()[1]))


if __name__ == "__main__":
    unittest.main()
