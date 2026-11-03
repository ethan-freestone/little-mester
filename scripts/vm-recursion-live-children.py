#!/usr/bin/env python3
"""Read-only inventory and deepest-first graceful suspension of child VMs."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_helper(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("VM child-inventory dependency is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_PATHS = _load_helper("vm_children_paths", ROOT / "lib/vm/paths.py")
_PROCESS = _load_helper("vm_children_process", ROOT / "lib/vm/process.py")


class ChildInventoryError(ValueError):
    """Unable to establish an exact managed/foreign QEMU inventory."""


def _runtime_process_identity(identity: dict) -> dict:
    required = {"pid", "start_ticks", "executable", "argv"}
    if not isinstance(identity, dict) or not required <= identity.keys():
        raise ChildInventoryError("profile process identity is incomplete")
    return {key: identity[key] for key in required}


def _drive_files(argv: list[str]) -> list[Path]:
    paths: list[Path] = []
    index = 0
    while index < len(argv):
        value = argv[index]
        if value == "-drive" and index + 1 < len(argv):
            index += 1
            value = argv[index]
        elif value.startswith("-drive="):
            value = value.removeprefix("-drive=")
        else:
            index += 1
            continue
        for option in value.split(","):
            if option.startswith("file="):
                path = Path(option.removeprefix("file="))
                if path.is_absolute() and ".." not in path.parts:
                    paths.append(path)
        index += 1
    return paths


def _private_regular(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)


def _profile_records(state_root: Path) -> list[tuple[Path, dict]]:
    root = _PATHS.state_root(str(state_root), must_exist=True, scan=True)
    profiles_dir = root / "vm/profiles"
    if not profiles_dir.exists() and not profiles_dir.is_symlink():
        return []
    if profiles_dir.is_symlink() or not profiles_dir.is_dir():
        raise ChildInventoryError("profile namespace is unsafe")
    _PATHS.private_entry(profiles_dir)
    records: list[tuple[Path, dict]] = []
    for profile in sorted(profiles_dir.iterdir()):
        if profile.is_symlink() or not profile.is_dir():
            raise ChildInventoryError("profile entry is unsafe")
        _PATHS.profile_name(profile.name)
        _PATHS.private_entry(profile)
        data = _PATHS.profile_read(root, profile.name)
        records.append((profile, data))
    return records


def live_nested_qemu_profiles(
    state_root: Path,
    proc_root: Path = Path("/proc"),
) -> tuple[list[Path], list[int]]:
    """Return exact live child profile paths and unrecognized QEMU PIDs."""
    state_root = Path(state_root)
    try:
        records = _profile_records(state_root)
        process_dirs = list(Path(proc_root).iterdir())
    except (OSError, ValueError, RuntimeError):
        return [], [-1]

    owned: dict[int, tuple[Path, dict, dict]] = {}
    unknown_pids: set[int] = set()
    for profile, data in records:
        status = data["status"]
        identity = data.get("runtime_identity")
        if status == "unknown":
            unknown_pids.add(identity["pid"] if isinstance(identity, dict) else -1)
            continue
        if identity is None:
            if status in {"starting", "provisioning", "ready", "suspending"}:
                unknown_pids.add(-1)
            continue
        process_identity = _runtime_process_identity(identity)
        try:
            state = _PROCESS.identity_state(process_identity, proc_root=proc_root)
        except (OSError, ValueError, TypeError):
            state = "unknown"
        if state == "stopped":
            if status in {"starting", "provisioning", "ready", "suspending"}:
                unknown_pids.add(identity["pid"])
            continue
        if state != "running" or status != "ready":
            unknown_pids.add(identity["pid"])
            continue
        disk = profile / "disk.qcow2"
        if (not _private_regular(disk) or identity.get("disk_path") != str(disk)
                or Path(identity["executable"]).name not in {"qemu-system-x86_64", "qemu-system-x86"}
                or disk not in _drive_files(identity["argv"])):
            unknown_pids.add(identity["pid"])
            continue
        if identity["pid"] in owned:
            unknown_pids.add(identity["pid"])
            continue
        owned[identity["pid"]] = (profile, data, process_identity)

    seen: set[int] = set()
    for process_dir in process_dirs:
        if not process_dir.name.isdecimal():
            continue
        pid = int(process_dir.name)
        try:
            comm = (process_dir / "comm").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            if process_dir.exists():
                unknown_pids.add(pid)
            continue
        if not comm.startswith("qemu-system"):
            continue
        try:
            identity = _PROCESS.capture_identity(pid, proc_root=proc_root)
        except (OSError, ValueError, RuntimeError):
            if process_dir.exists():
                unknown_pids.add(pid)
            continue
        expected = owned.get(pid)
        if expected is None or identity != expected[2]:
            unknown_pids.add(pid)
            continue
        seen.add(pid)

    for pid in owned:
        if pid not in seen:
            unknown_pids.add(pid)
    profiles = sorted(
        (item[0] for pid, item in owned.items() if pid in seen and pid not in unknown_pids),
        key=lambda path: path.name,
    )
    return profiles, sorted(unknown_pids)


def live_nested_qemus(
    state_root: Path,
    proc_root: Path = Path("/proc"),
) -> tuple[list[str], list[int]]:
    profiles, unknown_pids = live_nested_qemu_profiles(state_root, proc_root)
    return [profile.name for profile in profiles], unknown_pids


def _controller_valid(controller: Path | None) -> Path:
    if controller is None:
        raise ChildInventoryError("frozen child controller must be supplied explicitly")
    controller = Path(controller)
    if not controller.is_absolute() or controller.is_symlink() or not controller.is_file():
        raise ChildInventoryError("frozen child controller is not a regular executable")
    try:
        resolved = controller.resolve(strict=True)
        mode = controller.stat(follow_symlinks=False).st_mode
    except OSError as error:
        raise ChildInventoryError("frozen child controller is unavailable") from error
    if resolved != controller or not stat.S_ISREG(mode) or not os.access(controller, os.X_OK):
        raise ChildInventoryError("frozen child controller is unsafe")
    return controller


def _profile_state_preserved(profile: Path, workspace: Path) -> bool:
    for name in ("disk.qcow2", "seed.iso", "control-key", "known_hosts"):
        if not _private_regular(profile / name):
            return False
    return workspace.is_absolute() and workspace.is_dir() and not workspace.is_symlink()


def _inherited_lock_fds(state_root: Path) -> tuple[int, ...]:
    if "VM_HELD_ALLOCATION_ROOT" not in os.environ and "VM_HELD_ALLOCATION_FD" not in os.environ:
        return ()
    inherited_name = os.environ.get("VM_HELD_PROFILE_NAME", "")
    inherited_profile = _PATHS.profile_path(state_root, inherited_name)
    locks = _PATHS._inherited_profile_locks(state_root, inherited_name, inherited_profile)
    if locks is None or locks[1] is None:
        raise ChildInventoryError("inherited lifecycle locks are incomplete")
    return locks


def poweroff_recursive_children(
    state_root: Path,
    proc_root: Path = Path("/proc"),
    controller: Path | None = None,
    runner=subprocess.run,
) -> int:
    """Gracefully suspend known children deepest-first; never signal a QEMU PID."""
    try:
        root = _PATHS.state_root(str(state_root), must_exist=True, scan=True)
        frozen_controller = _controller_valid(controller)
        profiles, unknown = live_nested_qemu_profiles(root, proc_root)
        if unknown:
            print("REFUSED: an unknown or unmanaged QEMU process blocks parent suspend.", file=sys.stderr)
            return 4
        if not profiles:
            print("No live nested QEMU processes were found.")
            return 0
        data_by_name = {path.name: _PATHS.profile_read(root, path.name) for path in profiles}
        for profile in profiles:
            workspace = Path(data_by_name[profile.name]["workspace"])
            if not _profile_state_preserved(profile, workspace):
                print("REFUSED: child profile assets are incomplete or unsafe; parent remains running.", file=sys.stderr)
                return 4
            if frozen_controller == workspace or workspace in frozen_controller.parents:
                print("REFUSED: child controller is not outside the shared workspace.", file=sys.stderr)
                return 4
        ordered = sorted(
            profiles,
            key=lambda profile: (-data_by_name[profile.name]["depth"], profile.name),
        )
        environment = os.environ.copy()
        environment["MESTER_STATE_DIR"] = str(root)
        environment.pop("VM_STATE_DIR", None)
        environment.pop("VM_DIR_BASE", None)
        inherited_fds = _inherited_lock_fds(root)
        for profile in ordered:
            data_before = _PATHS.profile_read(root, profile.name)
            identity_before = data_before["runtime_identity"]
            command = [str(frozen_controller), "--poweroff-recursive", "suspend", profile.name]
            try:
                result = runner(command, env=environment.copy(), check=False, pass_fds=inherited_fds)
            except OSError:
                print("REFUSED: frozen child controller could not run; parent remains running.", file=sys.stderr)
                return 4
            if result.returncode != 0:
                print("REFUSED: child suspend failed; parent remains running.", file=sys.stderr)
                return result.returncode or 4
            remaining, unknown = live_nested_qemu_profiles(root, proc_root)
            if unknown or profile in remaining:
                print("REFUSED: child QEMU remains live or unknown; parent remains running.", file=sys.stderr)
                return 4
            data_after = _PATHS.profile_read(root, profile.name)
            if (data_after["status"] != "suspended"
                    or data_after["runtime_identity"] != identity_before
                    or data_after["workspace"] != data_before["workspace"]
                    or not _profile_state_preserved(profile, Path(data_after["workspace"]))):
                print("REFUSED: child state was not preserved after suspend; parent remains running.", file=sys.stderr)
                return 4
        print("All managed child VMs were suspended; profiles and overlays were preserved.")
        return 0
    except (OSError, ValueError, TypeError, RuntimeError):
        print("REFUSED: child inventory or profile identity is unsafe; parent remains running.", file=sys.stderr)
        return 4


class Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise ChildInventoryError("invalid child inventory command")


def main() -> int:
    parser = Parser(description=__doc__)
    parser.add_argument("--state-root", type=Path, default=os.environ.get("MESTER_STATE_DIR"))
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"))
    parser.add_argument("--poweroff-recursive", action="store_true")
    parser.add_argument("--controller", type=Path)
    args = parser.parse_args()
    if args.state_root is None:
        print("REFUSED: explicit private VM state root is required.", file=sys.stderr)
        return 4
    if args.poweroff_recursive:
        return poweroff_recursive_children(args.state_root, args.proc_root, args.controller)
    profiles, unknown = live_nested_qemu_profiles(args.state_root, args.proc_root)
    print(json.dumps({"profiles": [path.name for path in profiles], "unknown_pids": unknown}, sort_keys=True))
    if unknown:
        return 4
    return 3 if profiles else 0


if __name__ == "__main__":
    raise SystemExit(main())
