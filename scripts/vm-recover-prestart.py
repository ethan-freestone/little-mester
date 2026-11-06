#!/usr/bin/env python3
"""Diagnose/archive one failed pre-QEMU profile; never delete disks or edit its runtime.

Default is read-only. --apply preserves the entire profile outside the state root,
then releases only its proven-dead controller reservation. A fresh run can reuse
the name and capture the fixed source. Guest assets are refused unless explicit
--archive-failed-before-boot selects a failed seed-preparation profile with no ISO
or runs. Its overlay is archived intact, never deleted or silently reused.
--archive-stopped-failed also accepts booted failed profiles, but verifies every
run/recorded identity stopped and refuses profiles capable of child VMs.
It also accepts interrupted starting/provisioning metadata under those same gates.
--archive-unknown-cleanup requires a stopped recorded VM and permits only its
verified proxy/logger to remain live; --apply stops those before whole-profile archival.
"""

import argparse
import fcntl
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


paths = load("prestart_paths", ROOT / "lib/vm/paths.py")
process = load("prestart_process", ROOT / "lib/vm/process.py")
budget = load("prestart_budget", ROOT / "scripts/vm_memory_budget.py")


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def inspect(root, name, archive_failed=False, archive_stopped=False, cleanup_unknown=False):
    archive_stopped = archive_stopped or cleanup_unknown
    paths.state_root(str(root), must_exist=True, scan=False)
    profile = root / "vm/profiles" / name
    for directory in (root / "vm", profile.parent, profile):
        paths.private_entry(directory)
    metadata = profile / "profile.json"
    paths.private_entry(metadata)
    require(metadata.stat().st_size <= 65536, "oversized profile metadata")
    data = json.loads(metadata.read_text(), object_pairs_hook=paths.no_duplicates)
    paths.profile_validate(data, root, name)
    expected = "failed" if archive_failed or archive_stopped else "starting"
    statuses = {"unknown"} if cleanup_unknown else ({"failed", "starting", "provisioning"} if archive_stopped else {expected})
    require(data["status"] in statuses,
            "profile is not in the selected interrupted/failed startup state")
    if archive_stopped:
        live_proxy_pids = set()
        recorded_vm_seen = False
        require(not data["descendants_possible"], "profile may have descendant state; preserved")
        if cleanup_unknown:
            require("runtime_identity" in data, "unknown cleanup requires a recorded stopped VM identity")
            require(Path(data["runtime_identity"]["executable"]).name in
                    {"qemu-system-x86_64", "qemu-system-x86"}, "recorded VM executable is not QEMU")
        if "runtime_identity" in data:
            identity = {key: data["runtime_identity"][key]
                        for key in ("pid", "start_ticks", "executable", "argv")}
            require(process.identity_state(identity) == "stopped", "runtime identity is live or unknown; preserved")
        runs = profile / "runs"
        if runs.exists() or runs.is_symlink():
            paths.private_entry(runs)
            entries = list(runs.iterdir())
            require(len(entries) <= 100, "too many runs for bounded recovery")
            for run in entries:
                paths.private_entry(run)
                status = process.registry_status(run / "processes.json", run.name)
                if cleanup_unknown:
                    require(status["state"] in {"running", "stopped"}, "run registry missing or unknown; preserved")
                    record = process.registry_read(run / "processes.json", run.name)
                    recorded_vm_seen |= any(role.startswith("qemu_") and not role.endswith("_logger")
                                            and item["identity"] == identity
                                            for role, item in record["processes"].items())
                    require(any(role.startswith("qemu_") and not role.endswith("_logger")
                                for role in status["processes"]), "run has no QEMU identity evidence")
                    for role, item in status["processes"].items():
                        require(item["state"] == "stopped" or
                                (role in {"proxy", "proxy_logger"} and item["state"] == "running"),
                                "non-proxy process is live or unknown; preserved")
                        if item["state"] == "running":
                            proxy_identity = record["processes"][role]["identity"]
                            argv = proxy_identity["argv"]
                            expected_args = (("--registry", str(run / "processes.json")),
                                             ("--run-id", run.name)) if role == "proxy_logger" else (
                                                 ("--port-file", str(run / "proxy.port")),
                                                 ("--reload-marker", str(run / "proxy.ready")))
                            require(all(any(argv[index:index + 2] == [flag, value]
                                            for index in range(len(argv) - 1))
                                        for flag, value in expected_args),
                                    "proxy identity is not bound to this exact run; preserved")
                            live_proxy_pids.add(proxy_identity["pid"])
                else:
                    require(status["state"] == "stopped", "run registry is missing, live or unknown; preserved")
        if cleanup_unknown:
            require(runs.is_dir() and any(runs.iterdir()), "unknown cleanup requires run evidence")
            require(recorded_vm_seen, "run registry does not match recorded stopped QEMU identity")
    else:
        require("runtime_identity" not in data, "profile has a recorded runtime identity")
    allowed = {"profile.json", ".profile.lock", "runtime"}
    if archive_failed or archive_stopped:
        allowed |= {"disk.qcow2", "seed.user-data", "control-key"}
    if archive_stopped:
        allowed |= {"seed.iso", "runs"}
    require(all(p.name in allowed or
                ((archive_failed or archive_stopped) and re.fullmatch(r"\.asset\.[A-Za-z0-9_-]{1,64}", p.name))
                for p in profile.iterdir()),
            "profile has guest assets, runs, or unexpected entries; preserved")

    # Catch a process that started but died before recording its identity. Do not
    # print argv. Never signal anything, including unrelated parent/legacy VMs.
    needle = os.fsencode(str(profile))
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        if cleanup_unknown and int(entry.name) in live_proxy_pids:
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            command = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, ProcessLookupError):
            continue
        require(needle not in command, "a live process references this profile; preserved")

    reservation = root / ".reservations" / f"{name}.json"
    if reservation.exists() or reservation.is_symlink():
        record = budget._reservation_read(reservation, name)
        owner_state = process.identity_state(record["owner_identity"])
        require(owner_state == "stopped", "reservation owner is live or unknown; preserved")
    else:
        owner_state = "absent"
    return profile, owner_state


def cleanup_proxy_runs(profile):
    for run in (profile / "runs").iterdir():
        require(process.registry_stop(run / "processes.json", run.name, 5) == "stopped",
                "exact run-owned proxy cleanup did not verify stopped; profile preserved")
        require(process.registry_status(run / "processes.json", run.name)["state"] == "stopped",
                "run process state changed during cleanup; profile preserved")


def orphan_proxy_lock_holders_only(root, profile):
    """Conservative /proc proof before retiring a logger with inherited lock FDs."""
    permitted = set()
    for run in (profile / "runs").iterdir():
        record = process.registry_read(run / "processes.json", run.name)
        for role, item in record["processes"].items():
            if (role in {"proxy", "proxy_logger"} and item["identity"] is not None
                    and process.identity_state(item["identity"]) == "running"):
                permitted.add(item["identity"]["pid"])
    targets = set()
    for path in (root / ".allocation.lock", profile / ".profile.lock"):
        paths.private_entry(path)
        info = path.stat()
        targets.add((info.st_dev, info.st_ino))
    # The kernel records flock acquisition owners (and blocked waiters). Include
    # those plus the verified inheriting supervisors; unrelated non-dumpable
    # applications must not require inspecting their private FD tables.
    candidates = set(permitted)
    for line in Path("/proc/locks").read_text().splitlines():
        match = re.search(r"\b([0-9a-f]+):([0-9a-f]+):(\d+)\b", line)
        if not match or (os.makedev(int(match[1], 16), int(match[2], 16)), int(match[3])) not in targets:
            continue
        fields = line.split()
        require("FLOCK" in fields, "unexpected lifecycle lock type; preserved")
        offset = fields.index("FLOCK")
        pid = int(fields[offset + 3])
        require(pid > 0, "lifecycle lock owner is not identifiable; preserved")
        candidates.add(pid)
    holders = set()
    for pid in candidates:
        entry = Path("/proc") / str(pid)
        try:
            require(entry.stat().st_uid == os.getuid(), "foreign lifecycle lock owner; preserved")
            for fd in (entry / "fd").iterdir():
                try:
                    info = fd.stat()
                except FileNotFoundError:
                    continue
                if (info.st_dev, info.st_ino) in targets:
                    holders.add(pid)
        except (FileNotFoundError, ProcessLookupError):
            continue
    return bool(holders) and holders <= permitted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--apply", action="store_true")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--archive-failed-before-boot", action="store_true")
    modes.add_argument("--archive-stopped-failed", action="store_true")
    modes.add_argument("--archive-unknown-cleanup", action="store_true")
    args = parser.parse_args()
    paths.profile_name(args.profile)
    root = args.state_root
    require(root.is_absolute() and root == root.resolve(), "state root must be absolute and canonical")
    profile, owner_state = inspect(root, args.profile, args.archive_failed_before_boot,
                                   args.archive_stopped_failed, args.archive_unknown_cleanup)
    print(json.dumps({"profile": args.profile, "prestart": "verified",
                      "reservation_owner": owner_state, "mode": "apply" if args.apply else "read-only"}),
          flush=True)
    if not args.apply:
        return 0

    # Existing locks only; fail immediately if another controller holds either.
    descriptors = []
    try:
        def acquire():
            for lock in (root / ".allocation.lock", profile / ".profile.lock"):
                paths.private_entry(lock)
                fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
                descriptors.append(fd)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            acquire()
        except BlockingIOError:
            for fd in reversed(descriptors):
                os.close(fd)
            descriptors.clear()
            require(args.archive_unknown_cleanup, "lifecycle lock is busy; profile preserved")
            inspect(root, args.profile, cleanup_unknown=True)
            require(orphan_proxy_lock_holders_only(root, profile),
                    "lifecycle lock is not held exclusively by verified orphan proxy processes; preserved")
            cleanup_proxy_runs(profile)
            acquire()
        profile, owner_state = inspect(root, args.profile, args.archive_failed_before_boot,
                                       args.archive_stopped_failed, args.archive_unknown_cleanup)
        if args.archive_unknown_cleanup:
            cleanup_proxy_runs(profile)
            # No live references may remain before any rename. Preserve unknown
            # metadata verbatim; this is not an unknown->failed transition.
            inspect(root, args.profile, cleanup_unknown=True)
        archive = Path(tempfile.mkdtemp(prefix=root.name + "-prestart-", dir=root.parent))
        require(stat.S_IMODE(archive.stat().st_mode) == 0o700, "archive is not private")
        destination = archive / args.profile
        os.rename(profile, destination)
        paths.fsync_dir(profile.parent)
        paths.fsync_dir(archive)
        print(json.dumps({"preserved_profile": str(destination)}), flush=True)

        # Normal full-tree policy is required again; do not exempt cache modes.
        paths.state_root(str(root), must_exist=True)
        if owner_state == "stopped":
            reservation = root / ".reservations" / f"{args.profile}.json"
            record = budget._reservation_read(reservation, args.profile)
            require(process.identity_state(record["owner_identity"]) == "stopped",
                    "reservation owner changed; preserved")
            reservation.unlink()
            paths.fsync_dir(reservation.parent)
        print(json.dumps({"recovery": "complete", "next": "fresh owner-run launch"}))
    finally:
        for fd in reversed(descriptors):
            os.close(fd)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        # Policy errors and OS exceptions can contain private metadata/argv.
        message = str(error) if type(error) is RuntimeError else type(error).__name__
        print("REFUSED: " + message, file=sys.stderr)
        sys.exit(2)
