#!/usr/bin/env python3
"""Read-only inventory of nested QEMU processes before suspending a guest."""

from __future__ import annotations

import errno
from pathlib import Path
import sys


def _profile_disks(session_root: Path) -> dict[Path, str]:
    profiles: dict[Path, str] = {}
    if session_root.is_symlink() or not session_root.is_dir():
        return profiles
    for vm_root in session_root.glob("*/vm"):
        if vm_root.is_symlink() or not vm_root.is_dir():
            continue
        for profile_dir in vm_root.iterdir():
            if profile_dir.is_symlink() or not profile_dir.is_dir():
                continue
            disk = profile_dir / "disk.qcow2"
            if disk.is_symlink() or not disk.is_file():
                continue
            try:
                profiles[disk.resolve()] = profile_dir.name
            except OSError:
                continue
    return profiles


def _drive_files(argv: list[str]) -> list[Path]:
    paths: list[Path] = []
    for index, value in enumerate(argv):
        if value == "-drive" and index + 1 < len(argv):
            value = argv[index + 1]
        elif not value.startswith("-drive="):
            continue
        elif value.startswith("-drive="):
            value = value.removeprefix("-drive=")
        for option in value.split(","):
            if option.startswith("file="):
                try:
                    paths.append(Path(option.removeprefix("file=")).resolve())
                except OSError:
                    pass
    return paths


def live_nested_qemus(
    proc_root: Path = Path("/proc"),
    session_root: Path | None = None,
) -> tuple[list[str], list[int]]:
    """Return managed live child profile names and unrecognized QEMU PIDs."""
    if session_root is None:
        session_root = Path.home() / ".cache/lab-vm-recursion"
    profiles = _profile_disks(session_root)
    live_profiles: set[str] = set()
    unknown_pids: list[int] = []

    try:
        process_dirs = list(proc_root.iterdir())
    except OSError:
        return [], [-1]

    for process_dir in process_dirs:
        if not process_dir.name.isdecimal():
            continue
        try:
            comm = (process_dir / "comm").read_bytes().decode("ascii", errors="replace").strip()
            if not comm.startswith("qemu-system"):
                continue
            raw_argv = (process_dir / "cmdline").read_bytes()
        except OSError as exc:
            # A process may exit between the procfs scan and read. Other read
            # failures leave its identity unknown, so suspension must fail closed.
            if exc.errno in (errno.ENOENT, errno.ESRCH):
                continue
            unknown_pids.append(int(process_dir.name))
            continue
        argv = [part.decode("utf-8", errors="replace") for part in raw_argv.split(b"\0") if part]
        matched = False
        for disk in _drive_files(argv):
            profile = profiles.get(disk)
            if profile is not None:
                live_profiles.add(profile)
                matched = True
                break
        if not matched:
            unknown_pids.append(int(process_dir.name))

    return sorted(live_profiles), sorted(unknown_pids)


def main() -> int:
    live_profiles, unknown_pids = live_nested_qemus()
    if unknown_pids:
        if unknown_pids == [-1]:
            print("REFUSED: cannot inspect the guest process table.", file=sys.stderr)
        else:
            print(
                "REFUSED: an untracked QEMU process is running; refusing to suspend.",
                file=sys.stderr,
            )
        return 4
    if live_profiles:
        for name in live_profiles:
            print(f"LIVE nested VM profile: {name}")
        return 3
    print("No live nested QEMU processes were found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
