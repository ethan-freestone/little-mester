#!/usr/bin/env python3
"""Small private state record shared by independently invoked KVM stages."""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import stat
import sys
import tempfile
import time

VERSION = 1
FIELDS = {
    "work", "image", "seed", "overlay", "serial_log", "private_key",
    "known_hosts", "port", "overlay_dev", "overlay_ino", "ssh_public_key_sha256",
    "known_hosts_sha256", "guest_boot_id", "persistence_content_sha256",
    "persistence_mount_sha256", "qemu_mode",
}


class StateError(RuntimeError):
    pass


def fail(message: str) -> None:
    raise StateError(message)


def read_state(run_dir: pathlib.Path) -> dict:
    info = os.lstat(run_dir)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        fail("run directory must be an owner-owned mode-0700 directory")
    fd = os.open(run_dir / "state.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 16384):
            fail("state file ownership, mode, or size is invalid")
        raw = bytearray()
        while len(raw) <= 16384:
            block = os.read(fd, 16385 - len(raw))
            if not block:
                break
            raw.extend(block)
        if len(raw) > 16384:
            fail("state file exceeds its read bound")
        state = json.loads(raw.decode("utf-8"))
    finally:
        os.close(fd)
    if not isinstance(state, dict) or state.get("version") != VERSION or state.get("uid") != os.getuid():
        fail("state schema or owner identity is invalid")
    if not isinstance(state.get("stage_results"), dict):
        fail("stage result ledger is malformed")
    return state


def write_state(run_dir: pathlib.Path, state: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".state.", dir=run_dir)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, run_dir / "state.json")
        directory = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def validate_context(state: dict, args: argparse.Namespace) -> None:
    expected = {
        "run_id": args.run_id, "attempt": args.attempt,
        "source_sha": args.source_sha, "workflow_sha": args.workflow_sha,
        "host_boot_id": pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    }
    for key, value in expected.items():
        if state.get(key) != value:
            fail(f"state provenance mismatch: {key}")
    for key in ("source_sha", "workflow_sha"):
        value = getattr(args, key)
        if len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
            fail(f"{key} is invalid")
    if not args.run_id.isdigit() or not args.attempt.isdigit():
        fail("workflow attempt identity is invalid")
    deadline = state.get("deadline_monotonic")
    if not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        fail("absolute monotonic overall deadline is missing")
    prerequisites = {
        "boot": ("prepare",),
        "config-tmux": ("boot",),
        "restricted-reboot-egress-persistence": ("config-tmux",),
        "synthetic-oauth": ("restricted-reboot-egress-persistence",),
    }
    for previous in prerequisites.get(getattr(args, "stage", ""), ()):
        result = state.get("stage_results", {}).get(previous)
        if (not isinstance(result, dict) or type(result.get("status")) is not int
                or result.get("status") != 0):
            fail(f"stage prerequisite {previous} has not passed")
    work = pathlib.Path(state.get("work", ""))
    if work != pathlib.Path(args.run_dir) / "work":
        fail("private work directory does not match its run directory")
    work_info = os.lstat(work)
    if (not stat.S_ISDIR(work_info.st_mode) or work_info.st_uid != os.getuid()
            or stat.S_IMODE(work_info.st_mode) != 0o700):
        fail("private work directory ownership or mode is invalid")
    for name in ("image", "seed", "overlay", "serial_log", "private_key", "known_hosts"):
        value = state.get(name)
        if getattr(args, "stage", "") == "cleanup" and not value:
            continue
        if not isinstance(value, str) or pathlib.Path(value).parent != work:
            fail(f"private artifact path {name} is missing or escapes the work directory")
        path = pathlib.Path(value)
        if path.exists() or path.is_symlink():
            artifact = os.lstat(path)
            if not stat.S_ISREG(artifact.st_mode) or artifact.st_uid != os.getuid():
                fail(f"private artifact {name} is not an owner-owned regular file")
            if name in ("private_key", "known_hosts") and stat.S_IMODE(artifact.st_mode) != 0o600:
                fail(f"private SSH artifact {name} must have mode 0600")
    if state.get("overlay_dev") not in (None, ""):
        overlay = pathlib.Path(state["overlay"])
        info = os.lstat(overlay)
        if info.st_dev != state["overlay_dev"] or info.st_ino != state.get("overlay_ino"):
            fail("disposable overlay device/inode identity changed")


def init(args: argparse.Namespace) -> int:
    run_dir = pathlib.Path(args.run_dir)
    run_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    work = run_dir / "work"
    work.mkdir(mode=0o700)
    if not args.run_id.isdigit() or not args.attempt.isdigit():
        fail("workflow attempt identity is invalid")
    for key in ("source_sha", "workflow_sha"):
        value = getattr(args, key)
        if len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
            fail(f"{key} is invalid")
    state = {
        "version": VERSION, "uid": os.getuid(), "run_id": args.run_id,
        "attempt": args.attempt, "source_sha": args.source_sha,
        "workflow_sha": args.workflow_sha,
        "host_boot_id": pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "work": str(work), "started_monotonic": time.monotonic(),
        "deadline_monotonic": time.monotonic() + args.overall_seconds,
        "overall_seconds": args.overall_seconds, "stage_results": {},
        "qemu_mode": "stopped",
        "image": str(work / "noble.img"), "seed": str(work / "seed.iso"),
        "overlay": str(work / "guest.qcow2"), "serial_log": str(work / "serial.log"),
        "private_key": str(work / "id_ed25519"), "known_hosts": str(work / "known_hosts"),
    }
    write_state(run_dir, state)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("init", "get", "set", "result", "validate"))
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--field", action="extend", nargs="+", default=[])
    parser.add_argument("--value", action="append", default=[])
    parser.add_argument("--stage")
    parser.add_argument("--status", type=int)
    parser.add_argument("--overall-seconds", type=int, default=1080)
    args = parser.parse_args(argv)
    run_dir = pathlib.Path(args.run_dir)
    try:
        if args.command == "init":
            if not 60 <= args.overall_seconds <= 1200:
                fail("overall deadline is outside the supported bound")
            return init(args)
        state = read_state(run_dir)
        validate_context(state, args)
        if args.command == "validate":
            return 0
        if args.command == "get":
            if not args.field:
                fail("at least one state field must be requested")
            for name in args.field:
                value = state.get(name, "")
                if not isinstance(value, (str, int, float)) or "\n" in str(value):
                    fail(f"state field {name} cannot be safely transferred")
                sys.stdout.write(f"{value}\0")
            return 0
        if args.command == "set":
            if len(args.field) != len(args.value) or not args.field:
                fail("state update fields and values do not match")
            for name, value in zip(args.field, args.value, strict=True):
                if name not in FIELDS or "\n" in value or len(value) > 4096:
                    fail(f"state update field {name} is not permitted")
                if name in ("port", "overlay_dev", "overlay_ino"):
                    if not value.isdigit():
                        fail(f"state field {name} must be an integer")
                    state[name] = int(value)
                else:
                    state[name] = value
            write_state(run_dir, state)
            return 0
        if args.command == "result":
            if not args.stage or args.status not in (0, 1):
                fail("stage result is invalid")
            state.setdefault("stage_results", {})[args.stage] = {
                "status": args.status, "recorded_monotonic": time.monotonic(),
            }
            write_state(run_dir, state)
            return 0
    except (OSError, ValueError, StateError) as error:
        print(f"KVM_STAGE_STATE_REFUSED: {error}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
