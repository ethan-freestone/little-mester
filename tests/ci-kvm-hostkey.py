#!/usr/bin/env python3
"""Extract one cloud-init-published Ed25519 host public key from QEMU serial."""

from __future__ import annotations

import base64
import pathlib
import re
import struct
import sys

MAX_SERIAL_TAIL = 1024 * 1024
BEGIN = b"-----BEGIN SSH HOST KEY KEYS-----"
END = b"-----END SSH HOST KEY KEYS-----"
KEY_LINE = re.compile(rb"(?<![A-Za-z0-9/+])ssh-ed25519[ \t]+([A-Za-z0-9+/]+={0,2})")


def extract_ed25519_hostkey(serial: bytes) -> str:
    """Return a validated OpenSSH public-key line from cloud-init's key block."""
    start = serial.rfind(BEGIN)
    if start < 0:
        raise ValueError("cloud-init host-key block is absent")
    start += len(BEGIN)
    end = serial.find(END, start)
    if end < 0:
        raise ValueError("cloud-init host-key block is incomplete")
    matches = KEY_LINE.findall(serial[start:end])
    if len(matches) != 1:
        raise ValueError("cloud-init host-key block must contain exactly one Ed25519 key")

    encoded = matches[0]
    blob = base64.b64decode(encoded, validate=True)
    if base64.b64encode(blob) != encoded:
        raise ValueError("cloud-init host public key is not canonical base64")
    if len(blob) < 4:
        raise ValueError("cloud-init host public key blob is truncated")
    algorithm_length = struct.unpack(">I", blob[:4])[0]
    offset = 4 + algorithm_length
    if blob[4:offset] != b"ssh-ed25519" or offset + 4 > len(blob):
        raise ValueError("cloud-init host public key algorithm is invalid")
    key_length = struct.unpack(">I", blob[offset : offset + 4])[0]
    key = blob[offset + 4 :]
    if key_length != 32 or len(key) != key_length:
        raise ValueError("cloud-init Ed25519 public key has an invalid length")
    return "ssh-ed25519 " + encoded.decode("ascii")


def read_serial_tail(path: pathlib.Path) -> bytes:
    with path.open("rb") as stream:
        stream.seek(0, 2)
        length = stream.tell()
        stream.seek(max(0, length - MAX_SERIAL_TAIL))
        return stream.read(MAX_SERIAL_TAIL)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: ci-kvm-hostkey.py SERIAL_LOG", file=sys.stderr)
        return 2
    try:
        print(extract_ed25519_hostkey(read_serial_tail(pathlib.Path(sys.argv[1]))))
    except (OSError, ValueError, base64.binascii.Error, struct.error) as exc:
        print(f"KVM_HOSTKEY_PIN_FAIL reason={exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
