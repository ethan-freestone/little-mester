#!/usr/bin/env python3
"""Signal one exact, still-owned CI child through a Linux pidfd."""

from __future__ import annotations

import argparse
import os
import pathlib
import signal
import sys


def process_identity(pid: int) -> tuple[int, int, str, bytes]:
    stat = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    rest = stat[stat.rfind(") ") + 2 :].split()
    if len(rest) <= 19:
        raise RuntimeError("process stat record is incomplete")
    parent = int(rest[1])
    starttime = int(rest[19])
    status = pathlib.Path(f"/proc/{pid}/status").read_text(encoding="ascii")
    uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
    uid = int(uid_line.split()[1])
    comm = pathlib.Path(f"/proc/{pid}/comm").read_text(encoding="ascii").rstrip("\n")
    cmdline = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
    return parent, starttime, comm, cmdline


def signal_owned_process(
    pid: int,
    expected_starttime: int,
    expected_uid: int,
    expected_parent: int,
    expected_comm: str,
    required_args: tuple[str, ...],
    signum: int,
) -> bool:
    """Return False on identity mismatch; never fall back to numeric kill()."""
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("pidfd signaling is unavailable; refusing numeric-PID fallback")
    try:
        pidfd = os.pidfd_open(pid, 0)
    except ProcessLookupError:
        return False
    try:
        parent, starttime, comm, cmdline = process_identity(pid)
        status = pathlib.Path(f"/proc/{pid}/status").read_text(encoding="ascii")
        uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
        uid = int(uid_line.split()[1])
        argv = cmdline.split(b"\0")
        if (parent != expected_parent or starttime != expected_starttime
                or uid != expected_uid or comm != expected_comm
                or any(not any(argument.encode() in item for item in argv)
                       for argument in required_args)):
            return False
        try:
            signal.pidfd_send_signal(pidfd, signum)
        except ProcessLookupError:
            return False
        return True
    finally:
        os.close(pidfd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--starttime", type=int, required=True)
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--comm", required=True)
    parser.add_argument("--contains", action="append", default=[])
    parser.add_argument("--signal", choices=("TERM", "KILL"), required=True)
    args = parser.parse_args(argv)
    try:
        sent = signal_owned_process(
            args.pid, args.starttime, args.uid, args.parent, args.comm,
            tuple(args.contains), getattr(signal, f"SIG{args.signal}"),
        )
    except (OSError, RuntimeError, StopIteration, ValueError) as error:
        print(f"ci-process-signal: refused ({error})", file=sys.stderr)
        return 2
    return 0 if sent else 1


if __name__ == "__main__":
    raise SystemExit(main())
