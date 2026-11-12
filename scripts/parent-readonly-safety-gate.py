#!/usr/bin/env python3
"""Fail-closed, read-only precondition gate for owner parent-disk inspection."""

from __future__ import annotations

import argparse
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


def _check_proc_visibility() -> None:
    """Require a plausible /proc process listing, without requiring it to freeze."""
    try:
        pids = {entry.name for entry in os.scandir("/proc") if entry.name.isdigit()}
    except OSError as error:
        raise GateRefused(f"cannot enumerate /proc: {error}") from error
    if len(pids) < 3 or "1" not in pids or str(os.getpid()) not in pids:
        raise GateRefused("/proc process listing is empty/incomplete (PID 1 or gate PID missing)")


def _system_executable(name: str) -> pathlib.Path:
    path = pathlib.Path("/usr/bin") / name
    try:
        info = path.lstat()
    except OSError as error:
        raise GateRefused(f"trusted /usr/bin/{name} is unavailable") from error
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or not os.access(path, os.X_OK) or stat.S_IMODE(info.st_mode) & 0o022):
        raise GateRefused(f"/usr/bin/{name} is not a protected root-owned executable")
    return path


def _check_pid_one_with_lsof() -> None:
    sudo, lsof = _system_executable("sudo"), _system_executable("lsof")
    try:
        result = subprocess.run([str(sudo), str(lsof), "-Fpc", "-p", "1"],
                                stdin=None, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise GateRefused("could not verify PID 1 through trusted lsof") from error
    if (result.returncode != 0 or result.stderr.strip()
            or "p1" not in result.stdout.splitlines()):
        raise GateRefused("trusted lsof could not reliably observe PID 1")


def _parse_lsof(stdout: str, stderr: str, returncode: int,
                device: int, inode: int) -> list[int]:
    """Parse lsof machine fields; any warning or inconsistent identity refuses."""
    if returncode != 0 or stderr.strip():
        raise GateRefused("lsof reported an incomplete or failed descriptor scan")
    pid = None
    record = {}
    holders = set()

    def consume() -> None:
        if record:
            if record.get("D") is None or record.get("i") is None:
                raise GateRefused("lsof omitted device/inode from a matching descriptor")
            try:
                found_device = int(record["D"], 0)
            except ValueError:
                found_device = int(record["D"], 10)
            if found_device != device or record["i"] != str(inode):
                raise GateRefused("lsof descriptor identity differs from target device/inode")
            holders.add(pid)

    for line in stdout.splitlines():
        if not line:
            continue
        field, value = line[0], line[1:]
        if field == "p":
            consume()
            if not value.isdigit():
                raise GateRefused("lsof returned an invalid process record")
            pid, record = int(value), {}
        elif field == "f":
            consume()
            record = {"f": value}
        elif field in ("D", "i") and record:
            record[field] = value
    consume()
    if not holders:
        raise GateRefused("lsof returned no matching open descriptor")
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


def _scan_with_lsof(target: pathlib.Path, device: int, inode: int) -> list[int]:
    """Use the root-owned system lsof binary; never execute checkout code as root."""
    sudo, lsof = _system_executable("sudo"), _system_executable("lsof")
    command = [str(sudo), str(lsof), "-FpcfDi", "--", str(target)]
    try:
        result = subprocess.run(command, stdin=None, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise GateRefused("privileged lsof scan could not run") from error
    return _parse_lsof(result.stdout, result.stderr, result.returncode, device, inode)


def run_gate(install_root: pathlib.Path, state_dir: pathlib.Path) -> None:
    lab_vm = install_root / "lab-vm"
    state_module_path = install_root / "lib/vm/parent-state.py"
    disk = state_dir / "disk.qcow2"
    if os.geteuid() == 0:
        raise GateRefused("run this gate as the state owner; sudo invokes only system lsof")
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

    _check_proc_visibility()
    _check_pid_one_with_lsof()
    print("Positive control: confirming privileged lsof detects this gate's held test FD")
    with tempfile.NamedTemporaryFile() as held:
        held_info = os.fstat(held.fileno())
        target = pathlib.Path(f"/proc/{os.getpid()}/fd/{held.fileno()}")
        holders = _scan_with_lsof(target, held_info.st_dev, held_info.st_ino)
        if os.getpid() not in holders:
            raise GateRefused("positive control failed to find the deliberately held test FD")

    print("Running two complete exact device/inode scans (all visible UIDs)")
    disk_fd = os.open(disk, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        disk_info = os.fstat(disk_fd)
        if (disk_info.st_dev, disk_info.st_ino) != identity:
            raise GateRefused("opened disk FD differs from validated device/inode")
        target = pathlib.Path(f"/proc/{os.getpid()}/fd/{disk_fd}")
        for number in (1, 2):
            _validate_status(lab_vm, disk, identity)
            holders = _scan_with_lsof(target, *identity)
            external = [pid for pid in holders if pid != os.getpid()]
            if external:
                raise GateRefused(f"disk inode has open FD holder PID(s): {external}")
            _validate_status(lab_vm, disk, identity)
            print(f"holder scan {number}/2: PASS; no external FD for device={identity[0]} inode={identity[1]}")
        if (os.fstat(disk_fd).st_dev, os.fstat(disk_fd).st_ino) != identity:
            raise GateRefused("held disk FD identity changed")
    finally:
        os.close(disk_fd)
    _validate_status(lab_vm, disk, identity)
    print(f"Validated disk identity unchanged: device={identity[0]} inode={identity[1]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-root", type=pathlib.Path,
                        default=pathlib.Path(os.environ["INSTALL_ROOT"])
                        if os.environ.get("INSTALL_ROOT") else None)
    parser.add_argument("--state-dir", type=pathlib.Path, default=pathlib.Path.home() /
                        ".local/state/little-mester/parent-v3/default")
    args = parser.parse_args()
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
