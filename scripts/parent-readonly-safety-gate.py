#!/usr/bin/env python3
"""Fail-closed, read-only precondition gate for owner parent-disk inspection."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile


class GateRefused(RuntimeError):
    pass


def scan_open_inode(proc_root: str, device: int, inode: int, *, scanner_pid: int | None = None,
                    minimum_processes: int = 3) -> list[int]:
    """Enumerate every visible process and find descriptors for one exact inode.

    Processes that exit during enumeration are benign races. Any live process
    whose FD table or an FD target cannot be inspected makes the scan fail.
    """
    scanner_pid = os.getpid() if scanner_pid is None else scanner_pid
    try:
        entries = list(os.scandir(proc_root))
    except OSError as error:
        raise GateRefused(f"cannot enumerate process tree: {error}") from error
    pids = {entry.name for entry in entries if entry.name.isdigit()}
    if len(pids) < minimum_processes or "1" not in pids or str(scanner_pid) not in pids:
        raise GateRefused("process scan is empty/incomplete (PID 1 or scanner PID missing)")

    holders: set[int] = set()
    for pid_text in sorted(pids, key=int):
        pid = int(pid_text)
        fd_dir = os.path.join(proc_root, pid_text, "fd")
        try:
            descriptors = list(os.scandir(fd_dir))
        except (FileNotFoundError, ProcessLookupError):
            # The process exited after enumeration.
            if os.path.exists(os.path.join(proc_root, pid_text)):
                raise GateRefused(f"cannot inspect live process {pid} FD table")
            continue
        except OSError as error:
            raise GateRefused(f"cannot inspect process {pid} FD table: {error}") from error
        for descriptor in descriptors:
            try:
                info = os.stat(descriptor.path)
            except (FileNotFoundError, ProcessLookupError):
                # An individual descriptor can close while a process remains.
                continue
            except OSError as error:
                raise GateRefused(f"cannot inspect process {pid} descriptor: {error}") from error
            if (info.st_dev, info.st_ino) == (device, inode):
                holders.add(pid)
    try:
        final_pids = {entry.name for entry in os.scandir(proc_root) if entry.name.isdigit()}
    except OSError as error:
        raise GateRefused(f"cannot verify process enumeration completion: {error}") from error
    if not {"1", str(scanner_pid)} <= final_pids:
        raise GateRefused("process scan became incomplete before its final check")
    if final_pids - pids:
        raise GateRefused("process tree changed during FD scan; repeat the complete scan")
    return sorted(holders)


def _run_json(argv: list[str]) -> dict:
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise GateRefused(f"command could not be verified: {pathlib.Path(argv[0]).name}") from error
    if result.returncode:
        raise GateRefused(f"command refused/failed: {pathlib.Path(argv[0]).name}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise GateRefused(f"invalid JSON from {pathlib.Path(argv[0]).name}") from error
    if not isinstance(value, dict):
        raise GateRefused(f"unexpected result from {pathlib.Path(argv[0]).name}")
    return value


def _disk_identity(disk: pathlib.Path) -> tuple[int, int]:
    info = os.stat(disk, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise GateRefused("persistent disk is not a regular file")
    return info.st_dev, info.st_ino


def _validate_status(lab_vm: pathlib.Path, disk: pathlib.Path,
                     expected: tuple[int, int] | None = None) -> tuple[int, int]:
    status = _run_json([str(lab_vm), "parent", "status", "--json"])
    if status.get("status") != "STOPPED":
        raise GateRefused(f"parent status is {status.get('status')}, not STOPPED")
    disk_record = status.get("disk")
    if not isinstance(disk_record, dict) or disk_record.get("format") != "qcow2":
        raise GateRefused("validated qcow2 disk identity is absent")
    identity = _disk_identity(disk)
    if (disk_record.get("device"), disk_record.get("inode")) != identity:
        raise GateRefused("disk path identity differs from validated parent metadata")
    if expected is not None and identity != expected:
        raise GateRefused("persistent disk device/inode changed during safety checks")
    return identity


def _load_state_module(path: pathlib.Path):
    spec = importlib.util.spec_from_file_location("parent_state_safety_gate", path)
    if spec is None or spec.loader is None:
        raise GateRefused("cannot load installed parent state validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _check_children(state_module, state_dir: pathlib.Path) -> None:
    for name in ("child-proxy.identity.json", "child-qemu.identity.json"):
        path = state_dir / name
        if not path.is_file() or path.is_symlink():
            raise GateRefused(f"missing or unsafe child transition: {name}")
        try:
            identity = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise GateRefused(f"invalid child transition: {name}") from error
        if identity.get("identity_path") != name:
            raise GateRefused(f"child identity path mismatch: {name}")
        phase = state_module.exec_transition_status(identity)
        if phase != "STOPPED":
            raise GateRefused(f"child transition {name} is {phase}, not STOPPED")


def _check_listener(state_dir: pathlib.Path) -> None:
    port_path = state_dir / "ssh_port"
    if not port_path.exists() and not port_path.is_symlink():
        return
    if not port_path.is_file() or port_path.is_symlink():
        raise GateRefused("recorded ssh_port is unsafe")
    text = port_path.read_text().strip()
    if not re.fullmatch(r"[0-9]{1,5}", text):
        raise GateRefused("recorded ssh_port is invalid")
    port = int(text, 10)
    if not 1024 <= port <= 65535:
        raise GateRefused("recorded ssh_port is outside the valid range")
    try:
        result = subprocess.run(["ss", "-H", "-ltn"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise GateRefused("could not reliably inspect TCP listeners") from error
    for line in result.stdout.splitlines():
        columns = line.split()
        if len(columns) < 4:
            raise GateRefused("unparseable TCP listener output")
        if columns[3].rsplit(":", 1)[-1] == str(port):
            raise GateRefused(f"listener remains on recorded SSH port {port}")


def _scan_as_root(script: pathlib.Path, device: int, inode: int) -> list[int]:
    command = ["sudo", sys.executable, str(script), "--scan", str(device), str(inode)]
    try:
        result = subprocess.run(command, stdin=None, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise GateRefused("privileged read-only process scan could not run") from error
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "process scan refused"
        raise GateRefused(detail)
    try:
        holders = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise GateRefused("process scan returned invalid data") from error
    if not isinstance(holders, list) or any(type(pid) is not int for pid in holders):
        raise GateRefused("process scan returned invalid holder list")
    return holders


def run_gate(install_root: pathlib.Path, state_dir: pathlib.Path) -> None:
    lab_vm = install_root / "lab-vm"
    state_module_path = install_root / "lib/vm/parent-state.py"
    disk = state_dir / "disk.qcow2"
    if os.geteuid() == 0:
        raise GateRefused("run this gate as the state owner; it invokes sudo only for FD scans")
    if not lab_vm.is_file() or not os.access(lab_vm, os.X_OK) or not state_module_path.is_file():
        raise GateRefused("trusted installed candidate files are unavailable")

    print("Checking STOPPED status and recorded disk identity")
    identity = _validate_status(lab_vm, disk)
    print("Checking no-action recovery preview/lifecycle lock")
    recovery = _run_json([str(lab_vm), "parent", "recover", "--dry-run", "--json"])
    if not (recovery.get("dry_run") is True and recovery.get("action") == "none"
            and recovery.get("status") == "STOPPED"):
        raise GateRefused("recovery preview did not validate STOPPED/no-action state")
    print("Checking exact child transition identities")
    state_module = _load_state_module(state_module_path)
    _check_children(state_module, state_dir)
    print("Checking stale artifacts and recorded SSH listener")
    for name in ("qmp.sock", "proxy.port"):
        path = state_dir / name
        if path.exists() or path.is_symlink():
            raise GateRefused(f"stale parent artifact exists: {name}")
    _check_listener(state_dir)

    print("Positive control: confirming process scan detects this gate's held test FD")
    with tempfile.TemporaryFile() as held:
        held_info = os.fstat(held.fileno())
        holders = _scan_as_root(pathlib.Path(__file__).resolve(), held_info.st_dev, held_info.st_ino)
        if os.getpid() not in holders:
            raise GateRefused("positive control failed to find the deliberately held test FD")

    print("Running two complete exact device/inode scans (all visible UIDs)")
    for number in (1, 2):
        current = _validate_status(lab_vm, disk, identity)
        holders = _scan_as_root(pathlib.Path(__file__).resolve(), *identity)
        if holders:
            raise GateRefused(f"disk inode has open FD holder PID(s): {holders}")
        _validate_status(lab_vm, disk, current)
        print(f"holder scan {number}/2: PASS; no open FD for device={identity[0]} inode={identity[1]}")
    _validate_status(lab_vm, disk, identity)
    print(f"Validated disk identity unchanged: device={identity[0]} inode={identity[1]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-root", type=pathlib.Path,
                        default=pathlib.Path(os.environ["INSTALL_ROOT"])
                        if os.environ.get("INSTALL_ROOT") else None)
    parser.add_argument("--state-dir", type=pathlib.Path, default=pathlib.Path.home() /
                        ".local/state/little-mester/parent-v3/default")
    parser.add_argument("--scan", nargs=2, metavar=("DEVICE", "INODE"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.scan:
        try:
            holders = scan_open_inode("/proc", int(args.scan[0]), int(args.scan[1]))
            print(json.dumps(holders))
            return 0
        except (GateRefused, ValueError) as error:
            print(f"REFUSED: {error}", file=sys.stderr)
            return 2
    try:
        if args.install_root is None:
            raise GateRefused("pass --install-root for the trusted installed candidate")
        run_gate(args.install_root.resolve(), args.state_dir.resolve())
    except (GateRefused, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        print("SAFETY GATE: FAIL — no disk attachment authorized", file=sys.stderr)
        return 2
    print("SAFETY GATE: PASS — this is not authorization to attach the disk")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
