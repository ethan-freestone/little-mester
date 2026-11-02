"""Local regressions for bounded guest-originated isolation probes."""

from __future__ import annotations

import ast
from pathlib import Path
import socket
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/vm-guest-isolation-check.py"


def load_dns_probe(fake_socket):
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "direct_dns_responds"
    )
    namespace = {
        "socket": types.SimpleNamespace(
            AF_INET=socket.AF_INET,
            SOCK_DGRAM=socket.SOCK_DGRAM,
            socket=lambda *args: fake_socket,
        ),
    }
    module = ast.Module(body=[function], type_ignores=[])
    exec(compile(module, str(SCRIPT), "exec"), namespace)
    return namespace["direct_dns_responds"]


class GuestIsolationProbeTests(unittest.TestCase):
    def test_dns_probe_targets_only_fixed_public_resolver_and_harmless_name(self):
        class FakeSocket:
            def __init__(self):
                self.address = None
                self.packet = None
                self.closed = False

            def settimeout(self, timeout):
                self.timeout = timeout

            def connect(self, address):
                self.address = address

            def send(self, packet):
                self.packet = packet

            def recv(self, _size):
                return b"LM\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00"

            def close(self):
                self.closed = True

        fake = FakeSocket()
        self.assertTrue(load_dns_probe(fake)())
        self.assertEqual(fake.address, ("1.1.1.1", 53))
        self.assertIn(b"\x07example\x03com\x00", fake.packet)
        self.assertTrue(fake.closed)

    def test_dns_probe_fails_closed_when_no_response_arrives(self):
        class FakeSocket:
            def settimeout(self, _timeout):
                pass

            def connect(self, _address):
                pass

            def send(self, _packet):
                pass

            def recv(self, _size):
                raise socket.timeout

            def close(self):
                self.closed = True

        fake = FakeSocket()
        fake.closed = False
        self.assertFalse(load_dns_probe(fake)())
        self.assertTrue(fake.closed)


if __name__ == "__main__":
    unittest.main()
