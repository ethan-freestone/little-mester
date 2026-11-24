#!/usr/bin/env python3
"""Validate that a private callback port has no accepting host listener."""

from __future__ import annotations

import argparse
import os
import pathlib
import socket
import stat
import struct


def listener_absent(port: int) -> bool:
    """Return whether no IPv4/IPv6 listener can accept host-loopback traffic."""
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        return False
    try:
        expected_loopback = struct.unpack("=I", socket.inet_aton("127.0.0.1"))[0]

        def ipv6_accepts_ipv4(local_address: str) -> bool:
            address = b"".join(
                bytes.fromhex(local_address[index : index + 8])[::-1]
                for index in range(0, 32, 8)
            )
            if address in {b"\0" * 16, b"\0" * 15 + b"\1"}:
                return True
            return address[:12] == b"\0" * 10 + b"\xff\xff" and address[12:] in {
                b"\0" * 4,
                socket.inet_aton("127.0.0.1"),
            }

        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            rows = pathlib.Path(table).read_text(encoding="ascii").splitlines()[1:]
            for row in rows:
                fields = row.split()
                if len(fields) < 4 or fields[3] != "0A":
                    continue
                local_address, local_port = fields[1].split(":", 1)
                if int(local_port, 16) != port:
                    continue
                if table.endswith("tcp") and int(local_address, 16) in {
                    0,
                    expected_loopback,
                }:
                    return False
                if table.endswith("tcp6") and ipv6_accepts_ipv4(local_address):
                    return False
    except (OSError, UnicodeError, ValueError, IndexError):
        return False
    return True


def port_file_listener_absent(path: str | os.PathLike[str]) -> bool:
    """Validate the private port-file contract and check its listener state."""
    descriptor = -1
    try:
        file_path = pathlib.Path(path)
        descriptor = os.open(
            file_path,
            os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            return False
        chunks = bytearray()
        while len(chunks) <= 32:
            chunk = os.read(descriptor, 33 - len(chunks))
            if not chunk:
                break
            chunks.extend(chunk)
        if len(chunks) > 32:
            return False
        raw = bytes(chunks).decode("ascii").strip()
        if not raw.isdigit():
            return False
        return listener_absent(int(raw))
    except (OSError, ValueError, UnicodeError):
        return False
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("port_file")
    args = parser.parse_args()
    if not port_file_listener_absent(args.port_file):
        print("REFUSED: callback listener absence could not be verified")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
