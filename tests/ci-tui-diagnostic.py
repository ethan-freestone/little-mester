#!/usr/bin/env python3
"""Bound and sanitize protected OpenCode TUI terminal diagnostics."""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import stat
import sys
import tempfile


MAX_CAPTURE_BYTES = 256 * 1024


def strip_ansi(data: bytes) -> bytes:
    """Remove CSI/OSC/DCS-style terminal strings in one bounded linear scan."""
    output = bytearray()
    index = 0
    size = len(data)
    while index < size:
        if data[index] != 0x1B:
            output.append(data[index])
            index += 1
            continue
        if index + 1 >= size:
            break
        introducer = data[index + 1]
        if introducer == ord("["):
            index += 2
            while index < size:
                byte = data[index]
                index += 1
                if 0x40 <= byte <= 0x7E:
                    break
        elif introducer == ord("]"):
            index += 2
            while index < size:
                if data[index] == 0x07:
                    index += 1
                    break
                if data[index] == 0x1B and index + 1 < size and data[index + 1] == ord("\\"):
                    index += 2
                    break
                index += 1
        elif introducer in (ord("P"), ord("^"), ord("_")):
            index += 2
            while index < size:
                if data[index] == 0x1B and index + 1 < size and data[index + 1] == ord("\\"):
                    index += 2
                    break
                index += 1
        else:
            index += 2
    return bytes(output)


def read_private(path: pathlib.Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CAPTURE_BYTES:
            raise ValueError("invalid private terminal capture")
        data = bytearray()
        while len(data) < MAX_CAPTURE_BYTES:
            chunk = os.read(fd, min(8192, MAX_CAPTURE_BYTES - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)
    finally:
        os.close(fd)


def capture(path: pathlib.Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    retained = 0
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("terminal capture target is not a regular file")
        while True:
            chunk = sys.stdin.buffer.read(8192)
            if not chunk:
                break
            remaining = MAX_CAPTURE_BYTES - retained
            if remaining > 0:
                kept = chunk[:remaining]
                view = memoryview(kept)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                retained += len(kept)
        os.fsync(fd)
    finally:
        os.close(fd)
    return 0


def classify(raw: bytes) -> str:
    text = strip_ansi(raw).decode("utf-8", errors="replace").lower()
    if "service.json" in text and re.search(
        r"eacces|eperm|permission denied|read-only file system", text
    ):
        return "protected-service-config-write"
    if re.search(r"remote host identification has changed|host key verification failed", text):
        return "ssh-host-key-verification"
    if re.search(r"permission denied|eacces|eperm|read-only file system", text):
        return "filesystem-permission"
    if "plugin" in text and re.search(r"error|failed|cannot|unable", text):
        return "plugin-startup-failed"
    if "server process exited" in text:
        return "standalone-server-exit"
    if "ask anything" in text:
        return "tui-prompt-reached"
    return "tui-prompt-not-observed"


def retain_private(source: pathlib.Path, directory: pathlib.Path) -> pathlib.Path:
    raw = read_private(source)
    fd, name = tempfile.mkstemp(
        prefix="lm-kvm-tui-failure-", suffix=".raw", dir=directory
    )
    target = pathlib.Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        target.chmod(0o600)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("path", type=pathlib.Path)
    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("--input", required=True, type=pathlib.Path)
    report_parser.add_argument(
        "--phase", required=True, choices=("startup", "reattach", "reboot")
    )
    report_parser.add_argument("--actual-exit", required=True, type=int)
    report_parser.add_argument("--expected-exit", required=True, type=int)
    report_parser.add_argument("--retain-directory", required=True, type=pathlib.Path)
    args = parser.parse_args(argv)

    try:
        if args.operation == "capture":
            return capture(args.path)
        raw = read_private(args.input)
        cause = classify(raw)
        passed = args.actual_exit == args.expected_exit and cause == "tui-prompt-reached"
        if passed:
            print(
                f"KVM_TUI_DIAGNOSTIC phase={args.phase} result=PASS "
                f"cause={cause} captured_bytes={len(raw)}"
            )
            return 0
        retained = retain_private(args.input, args.retain_directory)
        print(
            f"KVM_TUI_DIAGNOSTIC phase={args.phase} result=FAIL cause={cause} "
            f"ssh_exit={args.actual_exit} expected_exit={args.expected_exit} "
            f"captured_bytes={len(raw)} private_raw={retained}"
        )
        return 1
    except (OSError, ValueError):
        print("KVM_TUI_DIAGNOSTIC result=FAIL cause=private-capture-error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
