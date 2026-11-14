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


def _verify_protected_helper(path: pathlib.Path) -> pathlib.Path:
    path = pathlib.Path(os.path.abspath(path))
    current = pathlib.Path(path.anchor)
    components = list(path.parts[1:])
    for index, component in enumerate(components):
        current /= component
        try:
            info = current.lstat()
        except OSError as error:
            raise GateRefused("protected_helper_unavailable") from error
        final = index == len(components) - 1
        if (stat.S_ISLNK(info.st_mode) or info.st_uid != 0
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or final and (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o644)
                or not final and not stat.S_ISDIR(info.st_mode)):
            raise GateRefused("protected_helper_path_untrusted")
    return path


def _scan_proc_inode(device: int, inode: int, allowed_pid: int = 0,
                     allowed_fd: int = -1) -> dict:
    """Root-only, read-only scan of process FDs and file-backed mappings."""
    if os.geteuid() != 0 or type(device) is not int or device < 0 \
            or type(inode) is not int or inode <= 0:
        raise GateRefused("insufficient_privilege_or_invalid_identity")
    _verify_protected_helper(pathlib.Path(__file__))
    owner_start = None
    if allowed_pid == 0 and allowed_fd == -1:
        pass  # Positive control: no holder exemption is authorized.
    else:
        try:
            invoking_uid = int(os.environ["SUDO_UID"])
            if allowed_pid <= 1 or allowed_fd < 0:
                raise ValueError("invalid caller descriptor allowance")
            owner_path = pathlib.Path("/proc") / str(allowed_pid)
            owner_stat = (owner_path / "stat").read_text().rsplit(")", 1)[1].split()
            owner_start = owner_stat[19]
            uid_line = next(line for line in (owner_path / "status").read_text().splitlines()
                            if line.startswith("Uid:"))
            if int(uid_line.split()[1]) != invoking_uid:
                raise ValueError("caller uid mismatch")
            held = os.stat(owner_path / "fd" / str(allowed_fd))
            if (held.st_dev, held.st_ino) != (device, inode):
                raise ValueError("caller descriptor identity mismatch")
        except (OSError, ValueError, KeyError, IndexError, StopIteration) as error:
            raise GateRefused("invalid_caller_fd_exemption") from error
    try:
        proc_mount = pathlib.Path("/proc/self/mountinfo").read_text()
        proc_entries = []
        for line in proc_mount.splitlines():
            fields = line.split()
            if len(fields) > 6 and fields[4] == "/proc" and "-" in fields:
                separator = fields.index("-")
                if len(fields) > separator + 1 and fields[separator + 1] == "proc":
                    proc_entries.append(fields)
        if not proc_entries:
            raise GateRefused("procfs_unavailable")
        separator = proc_entries[0].index("-")
        if len(proc_entries[0]) <= separator + 3:
            raise GateRefused("procfs_unavailable")
        mount_options = set(proc_entries[0][5].split(","))
        super_options = set(proc_entries[0][separator + 3].split(","))
        if any(option.startswith("hidepid=") and option != "hidepid=0"
               for option in mount_options | super_options) or "subset=pid" in super_options:
            raise GateRefused("procfs_restricted")
        if os.readlink("/proc/self/ns/pid") != os.readlink("/proc/1/ns/pid"):
            raise GateRefused("pid_namespace_restricted")
        if os.readlink("/proc/self/ns/user") != os.readlink("/proc/1/ns/user"):
            raise GateRefused("user_namespace_restricted")
    except OSError as error:
        raise GateRefused("procfs_visibility_failed") from error
    holders = set()
    scanned = 0
    try:
        pids = sorted((entry.name for entry in os.scandir("/proc") if entry.name.isdigit()),
                      key=int)
    except OSError as error:
        raise GateRefused("procfs_enumeration_failed") from error
    if len(pids) < 2 or len(pids) > 65536 or "1" not in pids:
        raise GateRefused("procfs_incomplete")

    def exited(pid_path: pathlib.Path, before: str) -> bool:
        try:
            after = (pid_path / "stat").read_text().rsplit(")", 1)[1].split()
        except FileNotFoundError:
            try:
                pid_path.stat()
            except FileNotFoundError:
                return True
            return False
        except OSError:
            return False
        if len(after) <= 19:
            raise GateRefused("malformed_process_identity")
        # PID reuse is not a harmless exit: this pass did not inspect the new
        # process, so fail closed instead of treating it as gone.
        if after[19] != before:
            raise GateRefused("process_changed_during_scan")
        return False

    for pid_text in pids:
        pid_path = pathlib.Path("/proc") / pid_text
        start_time = ""
        try:
            before = (pid_path / "stat").read_text().rsplit(")", 1)[1].split()
            if len(before) <= 19:
                raise GateRefused("malformed_process_identity")
            start_time = before[19]
            task_names = [item.name for item in os.scandir(pid_path / "task") if item.name.isdigit()]
            if len(task_names) > 4096:
                raise GateRefused("task_enumeration_limit_exceeded")
            if not task_names:
                if exited(pid_path, start_time):
                    continue
                raise GateRefused("incomplete_task_enumeration")
            for tid in task_names:
                task = pid_path / "task" / tid
                fd_dir = task / "fd"
                try:
                    fds = list(os.scandir(fd_dir))
                    if len(fds) > 65536:
                        raise GateRefused("descriptor_enumeration_limit_exceeded")
                    for entry in fds:
                        if not entry.name.isdigit():
                            continue
                        try:
                            info = os.stat(entry.path)
                        except FileNotFoundError:
                            # A descriptor can close between readdir and stat. Confirm
                            # that the same slot has disappeared before treating it as
                            # a harmless close race.
                            try:
                                os.stat(entry.path)
                            except FileNotFoundError:
                                continue
                            else:
                                raise GateRefused("descriptor_changed_during_observation")
                        if (info.st_dev, info.st_ino) == (device, inode):
                            pid_value = int(pid_text)
                            if pid_value != allowed_pid or int(entry.name) != allowed_fd:
                                holders.add((pid_value, "fd", int(entry.name)))
                except FileNotFoundError:
                    if not task.exists():
                        continue
                    raise GateRefused("incomplete_fd_enumeration")
                # maps is the kernel's authoritative file-backed mapping inventory;
                # requiring it readable avoids silently ignoring map_files restrictions.
                try:
                    with (task / "maps").open("rb") as stream:
                        raw_maps = stream.read(8 * 1024 * 1024 + 1)
                    if len(raw_maps) > 8 * 1024 * 1024:
                        raise GateRefused("mapping_output_limit_exceeded")
                    maps = raw_maps.decode("ascii", "strict")
                except FileNotFoundError:
                    if not task.exists():
                        continue
                    raise GateRefused("incomplete_mapping_observation")
                except UnicodeError as error:
                    raise GateRefused("malformed_mapping_observation") from error
                for line in maps.splitlines():
                    columns = line.split(None, 5)
                    if len(columns) < 5 or columns[4] == "0":
                        continue
                    try:
                        major_text, minor_text = columns[3].split(":", 1)
                        mapped_device = os.makedev(int(major_text, 16), int(minor_text, 16))
                        mapped_inode = int(columns[4], 10)
                    except (ValueError, OverflowError) as error:
                        raise GateRefused("malformed_mapping_identity") from error
                    if (mapped_device, mapped_inode) == (device, inode):
                        holders.add((int(pid_text), "map", 0))
            if exited(pid_path, start_time):
                continue
            after = (pid_path / "stat").read_text().rsplit(")", 1)[1].split()
            if len(after) <= 19 or after[19] != start_time:
                raise GateRefused("process_changed_during_scan")
            scanned += 1
        except FileNotFoundError:
            if not exited(pid_path, start_time):
                raise GateRefused("incomplete_live_process_observation")
            continue
        except PermissionError as error:
            if exited(pid_path, start_time):
                continue
            raise GateRefused("unreadable_live_process") from error
    if scanned < 1:
        raise GateRefused("incomplete_procfs_scan")
    if owner_start is not None:
        try:
            current = (pathlib.Path("/proc") / str(allowed_pid) / "stat").read_text()
            current = current.rsplit(")", 1)[1].split()
            held = os.stat(pathlib.Path("/proc") / str(allowed_pid) / "fd" / str(allowed_fd))
        except OSError as error:
            raise GateRefused("caller_changed_during_scan") from error
        if (len(current) <= 19 or current[19] != owner_start
                or (held.st_dev, held.st_ino) != (device, inode)):
            raise GateRefused("caller_changed_during_scan")
    return {"complete": True, "processes_scanned": scanned,
            "coverage": {"procfs": True, "pid_namespace": "init-shared",
                         "user_namespace": "init-shared", "descriptors": True,
                         "file_mappings": True},
            "holders": [list(item) for item in sorted(holders)]}


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


def _run_json(argv: list[str]) -> dict:
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=20, check=False)
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
                                capture_output=True, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise GateRefused("could not reliably inspect TCP listeners") from error
    for line in result.stdout.splitlines():
        columns = line.split()
        if len(columns) < 4:
            raise GateRefused("unparseable TCP listener output")
        if columns[3].rsplit(":", 1)[-1] == str(port):
            raise GateRefused(f"listener remains on recorded SSH port {port}")


def _scan_with_privileged_procfs(device: int, inode: int, owner_pid: int,
                                 owner_fd: int) -> dict:
    sudo = _system_executable("sudo")
    python = pathlib.Path("/usr/bin/python3").resolve(strict=True)
    python_info = python.lstat()
    if (not str(python).startswith("/usr/bin/") or not stat.S_ISREG(python_info.st_mode)
            or python_info.st_uid != 0 or python_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or not os.access(python, os.X_OK)):
        raise GateRefused("trusted /usr/bin/python3 target is unavailable")
    script = _verify_protected_helper(pathlib.Path(__file__).resolve())
    command = [str(sudo), "-n", str(python), "-I", "-B", str(script),
               "--scan-inode", str(device), str(inode), str(owner_pid), str(owner_fd)]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=35, check=False,
                                env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
                                     "PYTHONDONTWRITEBYTECODE": "1"})
    except (OSError, subprocess.SubprocessError) as error:
        raise GateRefused("privileged procfs scan failed or timed out") from error
    if result.returncode or result.stderr or len(result.stdout) > 65536:
        raise GateRefused("privileged procfs scan was refused or incomplete")
    try:
        value = json.loads(result.stdout)
    except (ValueError, UnicodeError) as error:
        raise GateRefused("privileged procfs scanner returned malformed evidence") from error
    if (not isinstance(value, dict) or value.get("complete") is not True
            or type(value.get("processes_scanned")) is not int or value["processes_scanned"] < 1
            or value.get("coverage") != {"procfs": True, "pid_namespace": "init-shared",
                                           "user_namespace": "init-shared",
                                           "descriptors": True, "file_mappings": True}
            or not isinstance(value.get("holders"), list)):
        raise GateRefused("privileged procfs scanner did not affirm complete coverage")
    return value


def run_gate(install_root: pathlib.Path, state_dir: pathlib.Path) -> None:
    lab_vm = install_root / "lab-vm"
    state_module_path = install_root / "lib/vm/parent-state.py"
    disk = state_dir / "disk.qcow2"
    if os.geteuid() == 0:
        raise GateRefused("run this gate as the state owner; sudo invokes only the protected scanner")
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

    print("Positive control: confirming privileged procfs scanner detects a same-filesystem FD")
    with tempfile.NamedTemporaryFile(dir=disk.parent) as held:
        held_info = os.fstat(held.fileno())
        holders = _scan_with_privileged_procfs(held_info.st_dev, held_info.st_ino, 0, -1)["holders"]
        if not any(item[0] == os.getpid() and item[1] == "fd" for item in holders):
            raise GateRefused("positive control failed to detect the deliberately held FD")

    print("Running complete exact device/inode scan across the visible procfs domain")
    disk_fd = os.open(disk, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        disk_info = os.fstat(disk_fd)
        if (disk_info.st_dev, disk_info.st_ino) != identity:
            raise GateRefused("opened disk FD differs from validated device/inode")
        _validate_status(lab_vm, disk, identity)
        holders = _scan_with_privileged_procfs(*identity, os.getpid(), disk_fd)["holders"]
        if holders:
            raise GateRefused("disk inode has observed descriptor or mapping holders")
        _validate_status(lab_vm, disk, identity)
        print(f"holder scan PASS; no external holder for device={identity[0]} inode={identity[1]}")
        if (os.fstat(disk_fd).st_dev, os.fstat(disk_fd).st_ino) != identity:
            raise GateRefused("held disk FD identity changed")
    finally:
        os.close(disk_fd)
    _validate_status(lab_vm, disk, identity)
    print(f"Validated disk identity unchanged: device={identity[0]} inode={identity[1]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan-inode", nargs=4, metavar=("DEVICE", "INODE", "OWNER_PID", "OWNER_FD"))
    parser.add_argument("--install-root", type=pathlib.Path,
                        default=pathlib.Path(os.environ["INSTALL_ROOT"])
                        if os.environ.get("INSTALL_ROOT") else None)
    parser.add_argument("--state-dir", type=pathlib.Path, default=pathlib.Path.home() /
                        ".local/state/little-mester/parent-v3/default")
    args = parser.parse_args()
    try:
        if args.scan_inode is not None:
            if args.install_root is not None:
                raise GateRefused("scanner mode does not accept an installation override")
            try:
                device, inode, owner_pid, owner_fd = (int(value, 10) for value in args.scan_inode)
            except ValueError as error:
                raise GateRefused("invalid device/inode arguments") from error
            print(json.dumps(_scan_proc_inode(device, inode, owner_pid, owner_fd), sort_keys=True))
            return 0
        if args.install_root is None:
            raise GateRefused("pass --install-root for the trusted installed candidate")
        run_gate(args.install_root.resolve(), args.state_dir.resolve())
    except (GateRefused, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        print("SAFETY GATE: FAIL — no disk attachment authorized", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 — unexpected failures must fail closed before disk attachment
        print(f"REFUSED: unexpected gate failure ({type(error).__name__})", file=sys.stderr)
        print("SAFETY GATE: FAIL — no disk attachment authorized", file=sys.stderr)
        return 2
    print("SAFETY GATE: PASS — this is not authorization to attach the disk")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
