#!/usr/bin/env python3
"""Fail-closed RAM budget check for lab-vm startup and profile resize."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

MIB = 1024 * 1024


def projected_available_mib(
    *, memavailable_mib: int, cgroup_remaining_mib: int | None, release_rss_mib: int
) -> int:
    """Estimate the post-stop budget, conservatively capped by the tightest limit."""
    host_budget = memavailable_mib + release_rss_mib
    if cgroup_remaining_mib is None:
        return host_budget
    return min(host_budget, cgroup_remaining_mib + release_rss_mib)


def budget_satisfies(requested_mib: int, available_mib: int, reserve_mib: int) -> bool:
    return requested_mib > 0 and reserve_mib >= 0 and available_mib >= requested_mib + reserve_mib


def read_memavailable_mib(proc_root: Path = Path("/proc")) -> int:
    for line in (proc_root / "meminfo").read_text(encoding="ascii").splitlines():
        if line.startswith("MemAvailable:"):
            fields = line.split()
            if len(fields) < 3 or fields[2] != "kB":
                break
            return int(fields[1]) // 1024
    raise RuntimeError("MemAvailable is unavailable; refusing to start QEMU")


def read_process_rss_mib(pid: int, proc_root: Path = Path("/proc")) -> int:
    if pid <= 0:
        raise ValueError("release PID must be a positive integer")
    # Shared RSS may remain mapped after QEMU exits. Count only private pages
    # as reclaimable so the projected post-stop budget cannot be overstated.
    try:
        lines = (proc_root / str(pid) / "smaps_rollup").read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise RuntimeError(f"cannot inspect private resident memory for QEMU PID {pid}: {exc}") from exc
    private_kib = 0
    found = False
    for line in lines:
        key, separator, value = line.partition(":")
        if separator and key in {"Private_Clean", "Private_Dirty", "Private_Hugetlb"}:
            fields = value.split()
            if len(fields) < 2 or fields[1] != "kB":
                raise RuntimeError(f"invalid {key} value in QEMU PID {pid} smaps_rollup")
            private_kib += int(fields[0])
            found = True
    if not found:
        raise RuntimeError(f"private resident memory is unavailable for QEMU PID {pid}")
    return (private_kib + 1023) // 1024


def _cgroup_membership(pid: int, proc_root: Path) -> tuple[str, str] | None:
    for line in (proc_root / str(pid) / "cgroup").read_text(encoding="ascii").splitlines():
        hierarchy, controllers, relative = line.split(":", 2)
        if hierarchy == "0" and not controllers:
            return "v2", relative
        if "memory" in controllers.split(","):
            return "v1", relative
    return None


def read_cgroup_remaining_mib(
    pid: int = os.getpid(),
    *,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> int | None:
    membership = _cgroup_membership(pid, proc_root)
    if membership is None:
        return None
    version, relative = membership
    relative_path = Path(relative.lstrip("/"))
    if ".." in relative_path.parts:
        raise RuntimeError("unsafe cgroup path; refusing to assume an unlimited memory budget")
    if version == "v2":
        filename_limit = "memory.max"
        filename_usage = "memory.current"
    else:
        cgroup_root = cgroup_root / "memory"
        filename_limit = "memory.limit_in_bytes"
        filename_usage = "memory.usage_in_bytes"

    base = cgroup_root / relative_path
    try:
        base.relative_to(cgroup_root)
    except ValueError as exc:
        raise RuntimeError("cgroup path escapes the memory controller mount") from exc

    remaining_budgets: list[int] = []
    current = base
    while True:
        limit_path = current / filename_limit
        usage_path = current / filename_usage
        if limit_path.exists() or usage_path.exists():
            try:
                limit_text = limit_path.read_text(encoding="ascii").strip()
                usage = int(usage_path.read_text(encoding="ascii").strip())
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"cannot inspect cgroup memory budget at {current}: {exc}") from exc
            if not (version == "v2" and limit_text == "max"):
                try:
                    limit = int(limit_text)
                except ValueError as exc:
                    raise RuntimeError(f"invalid cgroup memory limit at {current}") from exc
                # cgroup v1 uses a very large sentinel for an unlimited limit.
                if limit < (1 << 60):
                    remaining_budgets.append(max(0, limit - usage) // MIB)
        if current == cgroup_root:
            break
        parent = current.parent
        if parent == current or not parent.is_relative_to(cgroup_root):
            raise RuntimeError("cgroup path does not lead to the memory controller root")
        current = parent

    return min(remaining_budgets) if remaining_budgets else None


def check_budget(
    requested_mib: int,
    reserve_mib: int = 512,
    release_pid: int | None = None,
    *,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> tuple[bool, dict[str, int | None]]:
    if requested_mib <= 0 or reserve_mib < 0:
        raise ValueError("requested memory must be positive and reserve cannot be negative")
    memavailable = read_memavailable_mib(proc_root)
    cgroup_remaining = read_cgroup_remaining_mib(
        os.getpid(), proc_root=proc_root, cgroup_root=cgroup_root
    )
    release_rss = read_process_rss_mib(release_pid, proc_root) if release_pid else 0
    available = projected_available_mib(
        memavailable_mib=memavailable,
        cgroup_remaining_mib=cgroup_remaining,
        release_rss_mib=release_rss,
    )
    details: dict[str, int | None] = {
        "memavailable_mib": memavailable,
        "cgroup_remaining_mib": cgroup_remaining,
        "release_rss_mib": release_rss,
        "projected_available_mib": available,
        "requested_mib": requested_mib,
        "reserve_mib": reserve_mib,
    }
    return budget_satisfies(requested_mib, available, reserve_mib), details


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requested-mib", required=True, type=int)
    parser.add_argument("--reserve-mib", type=int, default=512)
    parser.add_argument("--release-pid", type=int)
    args = parser.parse_args()
    try:
        ok, details = check_budget(args.requested_mib, args.reserve_mib, args.release_pid)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    cgroup = details["cgroup_remaining_mib"]
    cgroup_text = "unlimited" if cgroup is None else f"{cgroup} MiB"
    print(
        "RAM budget: "
        f"MemAvailable={details['memavailable_mib']} MiB, "
        f"cgroup remaining={cgroup_text}, "
        f"releasable QEMU RSS={details['release_rss_mib']} MiB, "
        f"projected={details['projected_available_mib']} MiB, "
        f"requested={details['requested_mib']} MiB + reserve={details['reserve_mib']} MiB"
    )
    if not ok:
        print("REFUSED: insufficient memory budget for the requested VM RAM.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
