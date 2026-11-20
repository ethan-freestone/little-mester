#!/usr/bin/env python3
"""Contract tests for pinning cloud-init's host key from the owned QEMU console."""

from __future__ import annotations

import base64
import pathlib
import struct
import tempfile
import unittest

import importlib.util

SPEC = importlib.util.spec_from_file_location(
    "ci_kvm_hostkey", pathlib.Path(__file__).with_name("ci-kvm-hostkey.py")
)
HOSTKEY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOSTKEY)


def public_blob(key: bytes = b"\x42" * 32) -> bytes:
    algorithm = b"ssh-ed25519"
    return struct.pack(">I", len(algorithm)) + algorithm + struct.pack(">I", len(key)) + key


def serial_block(*keys: str) -> bytes:
    body = "\n".join(keys).encode("ascii")
    return b"console-prefix\n" + HOSTKEY.BEGIN + b"\n" + body + b"\n" + HOSTKEY.END


class HostKeyPinTests(unittest.TestCase):
    def test_extracts_only_the_public_key_from_cloud_init_block(self):
        encoded = base64.b64encode(public_blob()).decode("ascii")
        serial = b"ssh-ed25519 unrelated-outside-block\n" + serial_block(
            f"ssh-ed25519 {encoded} guest"
        )
        self.assertEqual(
            HOSTKEY.extract_ed25519_hostkey(serial), f"ssh-ed25519 {encoded}"
        )

    def test_requires_a_complete_block_and_exactly_one_ed25519_key(self):
        with self.assertRaisesRegex(ValueError, "absent"):
            HOSTKEY.extract_ed25519_hostkey(b"no host key output")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            HOSTKEY.extract_ed25519_hostkey(HOSTKEY.BEGIN)
        line = "ssh-ed25519 " + base64.b64encode(public_blob()).decode("ascii")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            HOSTKEY.extract_ed25519_hostkey(serial_block(line, line))

    def test_rejects_malformed_or_non_ed25519_key_blobs(self):
        malformed = base64.b64encode(b"bad").decode("ascii")
        with self.assertRaises(ValueError):
            HOSTKEY.extract_ed25519_hostkey(serial_block(f"ssh-ed25519 {malformed}"))
        other_type = b"ssh-rsa"
        blob = struct.pack(">I", len(other_type)) + other_type + struct.pack(">I", 32) + b"x" * 32
        encoded = base64.b64encode(blob).decode("ascii")
        with self.assertRaisesRegex(ValueError, "algorithm"):
            HOSTKEY.extract_ed25519_hostkey(serial_block(f"ssh-ed25519 {encoded}"))

    def test_reads_only_bounded_tail(self):
        encoded = base64.b64encode(public_blob()).decode("ascii")
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "serial.log"
            path.write_bytes(b"x" * (HOSTKEY.MAX_SERIAL_TAIL + 1) + serial_block(
                f"ssh-ed25519 {encoded} guest"
            ))
            tail = HOSTKEY.read_serial_tail(path)
        self.assertLessEqual(len(tail), HOSTKEY.MAX_SERIAL_TAIL)
        self.assertEqual(
            HOSTKEY.extract_ed25519_hostkey(tail), f"ssh-ed25519 {encoded}"
        )


if __name__ == "__main__":
    unittest.main()
