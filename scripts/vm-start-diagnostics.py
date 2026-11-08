#!/usr/bin/env python3
"""Read-only startup inventory; --logs adds bounded, best-effort redacted logs."""

import argparse
import importlib.util
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location(
    "startup_recovery", Path(__file__).with_name("vm-recover-prestart.py"))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--logs", action="store_true", help="collect redacted host/verified-SSH guest log excerpts")
    args = parser.parse_args()
    paths, process = helpers.paths, helpers.process
    paths.profile_name(args.profile)
    root = paths.state_root(str(args.state_root), must_exist=True, scan=False)
    profile = root / "vm/profiles" / args.profile
    if not profile.exists() and not profile.is_symlink():
        print(json.dumps({"name": args.profile, "status": "absent", "runs": [],
                          "metrics": process.registry_status(
                              root / "vm/metrics/processes.json", "metrics_host")},
                         indent=2, sort_keys=True))
        return
    for path in (root / "vm", profile.parent, profile, profile / "profile.json"):
        paths.private_entry(path)
    metadata = profile / "profile.json"
    helpers.require(metadata.stat().st_size <= 65536, "oversized profile metadata")
    data = json.loads(metadata.read_text(), object_pairs_hook=paths.no_duplicates)
    paths.profile_validate(data, root, args.profile)
    result = {key: data[key] for key in ("name", "status", "depth")}
    result["runtime_identity_present"] = "runtime_identity" in data
    result["assets_present"] = {name: (profile / name).exists()
                                for name in ("disk.qcow2", "seed.user-data", "control-key", "seed.iso", "runtime")}
    metrics = helpers.load("startup_metrics", helpers.ROOT / "lib/vm/metrics_service.py")
    result["metrics_listener_present"] = metrics._listener_present(metrics.DEFAULT_PORT)
    result["runs"] = []
    runs = profile / "runs"
    if runs.exists() or runs.is_symlink():
        paths.private_entry(runs)
        entries = list(runs.iterdir())
        helpers.require(len(entries) <= 100, "too many runs for bounded inventory")
        for run in sorted(entries):
            paths.private_entry(run)
            result["runs"].append(process.registry_status(run / "processes.json", run.name))
        if args.logs and entries:
            # Only the latest run: bounded work, not an archive-wide scan.
            latest = max(entries, key=lambda item: item.stat().st_mtime_ns)
            logs = helpers.load("startup_logs", Path(__file__).with_name("vm-log-report.py"))
            result["log_report"] = {"run_id": latest.name,
                                    "redaction": "best-effort; review before sharing",
                                    "host": {name: logs.local_tail(latest / name, paths)
                                             for name in ("qemu_1.log", "qemu_2.log", "qemu_3.log",
                                                          "qemu_1.log.1", "qemu_2.log.1", "qemu_3.log.1",
                                                          "proxy.log", "virtiofsd.log")},
                                    "guest": logs.guest_logs(profile, latest, process, helpers.ROOT)}
    result["metrics"] = process.registry_status(root / "vm/metrics/processes.json", "metrics_host")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        print("REFUSED: startup inventory: " + type(error).__name__, file=sys.stderr)
        sys.exit(2)
