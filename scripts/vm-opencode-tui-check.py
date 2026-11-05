#!/usr/bin/env python3
"""Detect an OpenCode process attached to a controlling terminal via /proc.

The check compares executable identity and the complete NUL-delimited argv,
then checks the kernel tty number. It never searches a lossy process-name string.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


def process_argv(path: Path) -> list[str] | None:
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    if not raw:
        return None
    return [os.fsdecode(item) for item in raw.rstrip(b"\0").split(b"\0")]


def process_has_tty(stat_path: Path) -> bool | None:
    try:
        raw = stat_path.read_text(encoding="ascii")
    except (FileNotFoundError, ProcessLookupError, PermissionError, UnicodeError):
        return None
    # comm is parenthesized but may itself contain spaces or ')'. Fields after
    # the last ')' start with field 3 (state), making tty_nr field 7 at index 4.
    closing = raw.rfind(")")
    if closing < 0:
        return None
    fields = raw[closing + 1 :].split()
    if len(fields) <= 4:
        return None
    try:
        return int(fields[4], 10) != 0
    except ValueError:
        return None


def is_opencode(executable: str, argv: list[str]) -> bool:
    if Path(executable).name in {"opencode", "opencode-bin"}:
        return True
    # The packaged CLI is a Node entrypoint. Match a complete argv element by
    # path components instead of searching the combined command line.
    for arg in argv:
        candidate = Path(arg)
        parts = candidate.parts
        if (
            "node_modules" in parts
            and "@opencode" in parts
            and "cli" in parts
            and candidate.name in {"opencode", "opencode.js", "index.js"}
        ):
            return True
    return False


def inspect(proc_root: Path) -> tuple[int, list[dict[str, object]]]:
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        raise ValueError("process table is unavailable") from None

    found: list[dict[str, object]] = []
    for process in entries:
        if not re.fullmatch(r"[1-9][0-9]*", process.name):
            continue
        argv = process_argv(process / "cmdline")
        if not argv:
            continue
        try:
            executable = os.readlink(process / "exe")
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            executable = argv[0]
        if not is_opencode(executable, argv):
            continue
        has_tty = process_has_tty(process / "stat")
        if has_tty is None:
            # An unreadable candidate process is ambiguous; do not declare the
            # guest idle while a possibly interactive process may be present.
            found.append({"pid": int(process.name), "argv": argv, "tty": "unknown"})
        elif has_tty:
            found.append({"pid": int(process.name), "argv": argv, "tty": True})
    return len(found), found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proc-root", default="/proc")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        count, processes = inspect(Path(args.proc_root))
    except ValueError as exc:
        print(f"BLOCKED  {exc}", file=sys.stderr)
        return 2

    busy = bool(processes)
    if args.json:
        print(json.dumps({"busy": busy, "count": count}, sort_keys=True))
    elif busy:
        print(f"BUSY  {count} OpenCode process(es) have a controlling terminal")
    else:
        print("IDLE  no OpenCode process has a controlling terminal")
    return 1 if busy else 0


if __name__ == "__main__":
    raise SystemExit(main())
