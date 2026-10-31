#!/usr/bin/env python3
"""Refuse guest software updates only while an interactive OpenCode TUI is attached."""

from __future__ import annotations

import os
import pwd
import sys
from pathlib import Path

PROC_ROOT = Path(os.environ.get("VM_OPENCODE_PROC_ROOT", "/proc"))
NON_TUI_COMMANDS = {
    "auth",
    "completion",
    "debug",
    "mcp",
    "models",
    "run",
    "serve",
    "upgrade",
    "version",
    "web",
}


def _opencode_cli_index(argv: list[str]) -> int | None:
    if not argv:
        return None

    executable = Path(argv[0]).name.lower()
    if executable in {"opencode", "opencode.js", "opencode.mjs"}:
        return 0

    if executable == "env" and len(argv) > 1 and Path(argv[1]).name.lower() in {
        "opencode",
        "opencode.js",
        "opencode.mjs",
    }:
        return 1

    if executable in {"node", "nodejs", "bun"} and len(argv) > 1:
        script = argv[1].replace("\\", "/")
        if Path(script).name.lower() in {"opencode", "opencode.js", "opencode.mjs"} or "/@opencode/cli/" in script:
            return 1
    return None


def _has_tty_process(proc: Path) -> bool:
    try:
        fields = (proc / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
        # Fields after comm start at state: pgrp is field 5, tty_nr is 7.
        return fields[0] not in {"Z", "X"} and int(fields[4]) != 0
    except (OSError, IndexError, ValueError):
        return False


def find_interactive_tui(
    proc_root: Path = PROC_ROOT, mester_uid: int | None = None
) -> tuple[str, str] | None:
    try:
        if mester_uid is None:
            mester_uid = pwd.getpwnam("mester").pw_uid
        processes = list(proc_root.iterdir())
    except (OSError, KeyError) as exc:
        raise RuntimeError(f"cannot inspect guest processes: {exc}") from exc

    for proc in processes:
        if not proc.name.isdecimal():
            continue
        try:
            status = (proc / "status").read_text(encoding="ascii")
            uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
            if int(uid_line.split()[1]) != mester_uid:
                continue
            argv = [part.decode(errors="replace") for part in (proc / "cmdline").read_bytes().split(b"\0") if part]
        except (OSError, StopIteration, ValueError):
            # Processes commonly exit while /proc is being scanned.
            continue

        cli_index = _opencode_cli_index(argv)
        if cli_index is None or not _has_tty_process(proc):
            continue

        command = next(
            (argument for argument in argv[cli_index + 1 :] if argument and not argument.startswith("-")),
            "",
        )
        if command in NON_TUI_COMMANDS:
            continue

        try:
            stat_fields = (proc / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
            tty_number = int(stat_fields[4])
        except (OSError, IndexError, ValueError):
            continue
        return proc.name, f"tty_nr={tty_number}"

    return None


def main() -> int:
    try:
        running = find_interactive_tui()
    except RuntimeError as exc:
        print(f"REFUSED: {exc}; cannot safely determine whether OpenCode is active.", file=sys.stderr)
        return 4

    if running:
        pid, tty = running
        print(f"REFUSED: OpenCode TUI is running in guest process {pid} ({tty}). Exit it before updating.", file=sys.stderr)
        return 3

    print("No interactive OpenCode TUI is attached to a guest terminal.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
