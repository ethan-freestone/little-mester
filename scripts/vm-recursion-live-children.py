#!/usr/bin/env python3
"""Read-only inventory of nested QEMU processes before suspending a guest."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path


def _profile_disks(session_root: Path) -> dict[Path, Path]:
    profiles: dict[Path, Path] = {}
    if session_root.is_symlink() or not session_root.is_dir():
        return profiles
    try:
        root_resolved = session_root.resolve(strict=True)
    except OSError:
        return profiles
    for vm_root in session_root.glob("*/vm"):
        session_dir = vm_root.parent
        if session_dir.is_symlink() or vm_root.is_symlink() or not vm_root.is_dir():
            continue
        for profile_dir in vm_root.iterdir():
            if profile_dir.is_symlink() or not profile_dir.is_dir():
                continue
            disk = profile_dir / "disk.qcow2"
            if disk.is_symlink() or not disk.is_file():
                continue
            try:
                resolved_profile = profile_dir.resolve(strict=True)
                resolved_profile.relative_to(root_resolved)
                profiles[disk.resolve(strict=True)] = resolved_profile
            except (OSError, ValueError):
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


def live_nested_qemu_profiles(
    proc_root: Path = Path("/proc"),
    session_root: Path | None = None,
) -> tuple[list[Path], list[int]]:
    """Return managed live child profile paths and unrecognized QEMU PIDs."""
    if session_root is None:
        session_root = Path.home() / ".cache/lab-vm-recursion"
    profiles = _profile_disks(session_root)
    live_profiles: set[Path] = set()
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


def live_nested_qemus(
    proc_root: Path = Path("/proc"),
    session_root: Path | None = None,
) -> tuple[list[str], list[int]]:
    """Return managed live child profile names and unrecognized QEMU PIDs."""
    profiles, unknown_pids = live_nested_qemu_profiles(proc_root, session_root)
    return [profile.name for profile in profiles], unknown_pids


def find_lab_cli(workspace: Path = Path("/workspace")) -> Path | None:
    """Find one executable Little Mester checkout in the selected guest workspace."""
    if workspace.is_symlink() or not workspace.is_dir():
        return None
    candidates: set[Path] = set()
    for directory, names, files in os.walk(workspace, followlinks=False):
        current = Path(directory)
        try:
            depth = len(current.relative_to(workspace).parts)
        except ValueError:
            names[:] = []
            continue
        names[:] = [name for name in names if name not in {".git", "node_modules", ".venv", ".cache"}]
        if depth > 4:
            names[:] = []
            continue
        if "lab-vm" not in files:
            continue
        candidate = current / "lab-vm"
        if candidate.is_symlink() or not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        if not (current / "lib/vm/lifecycle.sh").is_file():
            continue
        candidates.add(candidate.resolve())
    return next(iter(candidates)) if len(candidates) == 1 else None


def _profile_state_is_preserved(profile_dir: Path) -> bool:
    required = ("disk.qcow2", "seed.iso", "id_vm_control", "id_vm_control.pub", "known_hosts", "workspace")
    return all((profile_dir / name).is_file() and not (profile_dir / name).is_symlink() for name in required)


def poweroff_recursive_children(
    proc_root: Path = Path("/proc"),
    session_root: Path | None = None,
    lab_cli: Path | None = None,
    runner=subprocess.run,
) -> int:
    """Gracefully suspend managed children, stopping before any unsafe parent shutdown."""
    if session_root is None:
        session_root = Path.home() / ".cache/lab-vm-recursion"
    profiles, unknown_pids = live_nested_qemu_profiles(proc_root, session_root)
    if unknown_pids:
        print("REFUSED: an untracked QEMU process is running; refusing recursive suspend.", file=sys.stderr)
        return 4
    if not profiles:
        print("No live nested QEMU processes were found.")
        return 0
    if lab_cli is None:
        lab_cli = find_lab_cli()
    if lab_cli is None or lab_cli.is_symlink() or not lab_cli.is_file() or not os.access(lab_cli, os.X_OK):
        print("REFUSED: could not identify one Little Mester CLI in /workspace; child VMs were left running.", file=sys.stderr)
        return 4

    for profile_dir in profiles:
        session = profile_dir.parents[1]
        if profile_dir.is_symlink() or session.is_symlink() or not _profile_state_is_preserved(profile_dir):
            print(f"REFUSED: child profile state is incomplete or unsafe ({profile_dir.name}); no parent shutdown.", file=sys.stderr)
            return 4
        env = os.environ.copy()
        env["VM_STATE_DIR"] = str(session)
        env["VM_DIR_BASE"] = str(session / "vm")
        print(f"Gracefully suspending nested VM '{profile_dir.name}' before its parent.")
        try:
            result = runner(
                [str(lab_cli), "--poweroff-recursive", "suspend", profile_dir.name],
                env=env,
                check=False,
            )
        except OSError as exc:
            print(f"REFUSED: could not run the child lifecycle command ({exc}); parent remains running.", file=sys.stderr)
            return 4
        if result.returncode != 0:
            print(f"REFUSED: child '{profile_dir.name}' did not suspend safely; parent remains running.", file=sys.stderr)
            return result.returncode or 4
        remaining, unknown_pids = live_nested_qemu_profiles(proc_root, session_root)
        if unknown_pids:
            print("REFUSED: an untracked QEMU process remains; parent remains running.", file=sys.stderr)
            return 4
        if profile_dir in remaining:
            print(f"REFUSED: child '{profile_dir.name}' is still running; parent remains running.", file=sys.stderr)
            return 4
        if not _profile_state_is_preserved(profile_dir):
            print(f"REFUSED: child '{profile_dir.name}' state changed during shutdown; parent remains running.", file=sys.stderr)
            return 4

    print("All managed child VMs were gracefully suspended; their profile state was preserved.")
    return 0


def main() -> int:
    if "--poweroff-recursive" in sys.argv[1:]:
        return poweroff_recursive_children()
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
