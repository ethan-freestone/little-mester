#!/usr/bin/env python3
"""Signal one exact, still-owned CI child through a Linux pidfd."""

from __future__ import annotations

import argparse
import errno
import json
import os
import pathlib
import signal
import time
import sys


def process_identity(pid: int) -> tuple[int, int, int, int, str, bytes]:
    stat = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    rest = stat[stat.rfind(") ") + 2 :].split()
    if len(rest) <= 19:
        raise RuntimeError("process stat record is incomplete")
    parent = int(rest[1])
    process_group = int(rest[2])
    session_id = int(rest[3])
    starttime = int(rest[19])
    status = pathlib.Path(f"/proc/{pid}/status").read_text(encoding="ascii")
    uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
    uid = int(uid_line.split()[1])
    comm = pathlib.Path(f"/proc/{pid}/comm").read_text(encoding="ascii").rstrip("\n")
    cmdline = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
    return parent, process_group, session_id, starttime, comm, cmdline


def signal_owned_process(
    pid: int,
    expected_starttime: int,
    expected_uid: int,
    expected_parent: int | None,
    expected_comm: str,
    required_args: tuple[str, ...],
    signum: int,
    process_group: bool = False,
) -> bool:
    """Return False on identity mismatch; never fall back to numeric kill()."""
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("pidfd signaling is unavailable; refusing numeric-PID fallback")
    try:
        pidfd = os.pidfd_open(pid, 0)
    except ProcessLookupError:
        return False
    try:
        parent, actual_group, _session_id, starttime, comm, cmdline = process_identity(pid)
        status = pathlib.Path(f"/proc/{pid}/status").read_text(encoding="ascii")
        uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
        uid = int(uid_line.split()[1])
        argv = cmdline.split(b"\0")
        if ((expected_parent is not None and parent != expected_parent)
                or starttime != expected_starttime
                or uid != expected_uid or (expected_comm not in ("", "*") and comm != expected_comm)
                or any(not any(argument.encode() in item for item in argv)
                       for argument in required_args)):
            return False
        if process_group and (actual_group != pid or os.getpgid(pid) != pid):
            return False
        try:
            if process_group:
                os.killpg(pid, signum)
            else:
                signal.pidfd_send_signal(pidfd, signum)
        except ProcessLookupError:
            return False
        return True
    finally:
        os.close(pidfd)


def process_group_empty(process_group: int) -> bool:
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="ascii")
            rest = stat[stat.rfind(") ") + 2 :].split()
            if len(rest) <= 3:
                return False
            if int(rest[2]) == process_group and rest[0] != "Z":
                return False
        except FileNotFoundError:
            continue
        except (OSError, ValueError, IndexError):
            try:
                entry.stat()
            except FileNotFoundError:
                continue
            except OSError:
                return False
            return False
    return True


def signal_group_members(process_group: int, session_id: int, expected_uid: int) -> bool:
    """Pidfd-signal members of the recorded session if its leader has exited."""
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return False
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        try:
            parent, actual_group, _session_id, starttime, _comm, _cmdline = process_identity(pid)
            stat = (entry / "stat").read_text(encoding="ascii")
            fields = stat[stat.rfind(") ") + 2 :].split()
            state = fields[0]
            actual_session = int(fields[3])
            status = (entry / "status").read_text(encoding="ascii")
            uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
            uid = int(uid_line.split()[1])
        except FileNotFoundError:
            continue
        except (OSError, ValueError, IndexError, StopIteration):
            try:
                entry.stat()
            except FileNotFoundError:
                continue
            return False
        if actual_group != process_group or state == "Z":
            continue
        if actual_session != session_id or uid != expected_uid:
            return False
        try:
            pidfd = os.pidfd_open(pid, 0)
        except ProcessLookupError:
            continue
        try:
            check_parent, check_group, _check_session, check_start, _check_comm, _check_args = process_identity(pid)
            if check_group != process_group or check_start != starttime:
                continue
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except OSError:
            return False
        finally:
            os.close(pidfd)
    return True


def terminate_marked_groups(evidence_root: pathlib.Path) -> bool:
    if not evidence_root.is_dir():
        return False
    try:
        markers = tuple(evidence_root.rglob("bridge-process.json"))
    except OSError:
        return False
    for marker in markers:
        try:
            record = json.loads(marker.read_text(encoding="ascii"))
            pid = record["pid"]
            starttime = record["starttime"]
            uid = record["uid"]
            process_group = record["process_group"]
            session_id = record["session_id"]
            if (type(pid) is not int or pid <= 1 or type(starttime) is not int
                    or type(uid) is not int or process_group != pid or session_id != pid):
                return False
        except (OSError, ValueError, KeyError, TypeError):
            return False
        try:
            _parent, current_group, current_session, current_start, _comm, _args = process_identity(pid)
        except FileNotFoundError:
            leader_present = False
        except (OSError, RuntimeError, ValueError):
            return False
        else:
            if (current_start != starttime or current_group != process_group
                    or current_session != session_id):
                return False
            leader_present = True
        if leader_present:
            try:
                leader_signaled = signal_owned_process(
                    pid, starttime, uid, None, "*", (), signal.SIGKILL, process_group=True
                )
            except OSError as error:
                if error.errno != errno.EINVAL:
                    return False
                leader_signaled = False
            except (RuntimeError, StopIteration, ValueError):
                return False
            if not leader_signaled and not signal_group_members(pid, session_id, uid):
                return False
        elif not signal_group_members(pid, session_id, uid):
            return False
        deadline = time.monotonic() + 3
        while not process_group_empty(process_group) and time.monotonic() < deadline:
            if not signal_group_members(process_group, session_id, uid):
                return False
            time.sleep(0.05)
        if not process_group_empty(process_group):
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int)
    parser.add_argument("--starttime", type=int)
    parser.add_argument("--uid", type=int)
    parser.add_argument("--parent")
    parser.add_argument("--comm")
    parser.add_argument("--contains", action="append", default=[])
    parser.add_argument("--signal", choices=("TERM", "KILL"))
    parser.add_argument("--process-group", action="store_true")
    parser.add_argument("--check-process-group-empty", type=int)
    parser.add_argument("--terminate-marked-groups")
    args = parser.parse_args(argv)
    if args.terminate_marked_groups is not None:
        return 0 if terminate_marked_groups(pathlib.Path(args.terminate_marked_groups)) else 1
    if args.check_process_group_empty is not None:
        return 0 if process_group_empty(args.check_process_group_empty) else 1
    if (args.pid is None or args.starttime is None or args.uid is None
            or args.parent is None or args.comm is None or args.signal is None):
        parser.error("process signaling requires --pid, --starttime, --uid, --parent, --comm, and --signal")
    try:
        expected_parent = None if args.parent == "*" else int(args.parent)
        sent = signal_owned_process(
            args.pid, args.starttime, args.uid, expected_parent, args.comm,
            tuple(args.contains), getattr(signal, f"SIG{args.signal}"), args.process_group,
        )
    except (OSError, RuntimeError, StopIteration, ValueError) as error:
        print(f"ci-process-signal: refused ({error})", file=sys.stderr)
        return 2
    return 0 if sent else 1


if __name__ == "__main__":
    raise SystemExit(main())
