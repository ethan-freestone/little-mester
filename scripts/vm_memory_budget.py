#!/usr/bin/env python3
"""Fail-closed RAM preflight and identity-bound concurrent VM reservations."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

MIB = 1024 * 1024
DEFAULT_RESERVE_MIB = 512
MAX_RESERVATIONS = 128
ROOT = Path(__file__).resolve().parents[1]


def _load_helper(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("VM resource helper dependency is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_PATHS = _load_helper("vm_memory_paths", ROOT / "lib/vm/paths.py")
_PROCESS = _load_helper("vm_memory_process", ROOT / "lib/vm/process.py")


class ResourceRefusal(ValueError):
    """Unsafe or insufficient resource state; no profile is modified."""


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer at least {minimum}")
    return value


def default_memory_gib(depth: int) -> int:
    """Return the fixed 8/4/2/1 GiB allocation for recursion depth 0..32."""
    _integer(depth, "depth")
    if depth > 32:
        raise ValueError("depth must be from 0 to 32")
    return max(1, 8 >> depth)


def validate_memory_gib(value: int) -> int:
    _integer(value, "memory", minimum=1)
    if value > 128:
        raise ValueError("memory must be from 1 to 128 GiB")
    return value


def projected_available_mib(
    memavailable_mib: int,
    cgroup_remaining_mib: int | None,
    release_rss_mib: int,
) -> int:
    """Conservatively cap host capacity by cgroup capacity after target release."""
    _integer(memavailable_mib, "MemAvailable")
    _integer(release_rss_mib, "releasable RSS")
    if cgroup_remaining_mib is None:
        return memavailable_mib + release_rss_mib
    _integer(cgroup_remaining_mib, "cgroup remaining")
    return min(memavailable_mib + release_rss_mib, cgroup_remaining_mib + release_rss_mib)


def budget_satisfies(requested_mib: int, available_mib: int, reserve_mib: int) -> bool:
    _integer(requested_mib, "requested memory", minimum=1)
    _integer(available_mib, "available memory")
    _integer(reserve_mib, "reserve")
    return available_mib >= requested_mib + reserve_mib


def read_memavailable_mib(proc_root: Path = Path("/proc")) -> int:
    try:
        lines = (Path(proc_root) / "meminfo").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as error:
        raise RuntimeError("MemAvailable is unavailable; refusing VM resource allocation") from error
    matches = [line for line in lines if line.startswith("MemAvailable:")]
    if len(matches) != 1:
        raise RuntimeError("MemAvailable is unavailable; refusing VM resource allocation")
    fields = matches[0].split()
    if len(fields) != 3 or fields[2] != "kB" or not fields[1].isdecimal():
        raise RuntimeError("MemAvailable is malformed; refusing VM resource allocation")
    return int(fields[1]) // 1024


def read_process_private_rss_mib(pid: int, proc_root: Path = Path("/proc")) -> int:
    _integer(pid, "release PID", minimum=1)
    try:
        lines = (Path(proc_root) / str(pid) / "smaps_rollup").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as error:
        raise RuntimeError("target private resident memory is unavailable") from error
    totals: dict[str, int] = {}
    for line in lines:
        key, separator, value = line.partition(":")
        if not separator or key not in {"Private_Clean", "Private_Dirty", "Private_Hugetlb"}:
            continue
        fields = value.split()
        if key in totals or len(fields) != 2 or fields[1] != "kB" or not fields[0].isdecimal():
            raise RuntimeError("target private resident memory is malformed")
        totals[key] = int(fields[0])
    if not totals:
        raise RuntimeError("target private resident memory is unavailable")
    return (sum(totals.values()) + 1023) // 1024


def _cgroup_membership(pid: int, proc_root: Path) -> tuple[str, str] | None:
    try:
        lines = (proc_root / str(pid) / "cgroup").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as error:
        raise RuntimeError("process cgroup membership is unavailable") from error
    v2: list[str] = []
    v1: list[str] = []
    if not lines:
        raise RuntimeError("process cgroup membership is empty")
    for line in lines:
        parts = line.split(":", 2)
        if len(parts) != 3 or not parts[0].isdecimal():
            raise RuntimeError("process cgroup membership is malformed")
        hierarchy, controllers, path = parts
        if not path.startswith("/") or "\x00" in path:
            raise RuntimeError("process cgroup path is unsafe")
        relative = Path(path.lstrip("/"))
        if ".." in relative.parts:
            raise RuntimeError("process cgroup path escapes its mount")
        if hierarchy == "0" and not controllers:
            v2.append(path)
        elif "memory" in controllers.split(","):
            v1.append(path)
    if len(v2) > 1 or len(v1) > 1 or (v2 and v1):
        raise RuntimeError("process has ambiguous memory cgroup membership")
    if v2:
        return "v2", v2[0]
    if v1:
        return "v1", v1[0]
    return None


def _read_decimal(path: Path, label: str) -> int:
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as error:
        raise RuntimeError(f"{label} is unreadable; refusing to assume unlimited memory") from error
    if not value.isdecimal():
        raise RuntimeError(f"{label} is malformed; refusing to assume unlimited memory")
    return int(value)


def read_cgroup_remaining_mib(
    pid: int = os.getpid(),
    *,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> int | None:
    """Return the tightest remaining memory limit; missing files are errors."""
    _integer(pid, "cgroup PID", minimum=1)
    membership = _cgroup_membership(pid, Path(proc_root))
    if membership is None:
        return None
    version, relative = membership
    root = Path(cgroup_root)
    if version == "v1":
        root = root / "memory"
        limit_name, usage_name = "memory.limit_in_bytes", "memory.usage_in_bytes"
    else:
        limit_name, usage_name = "memory.max", "memory.current"
    try:
        root_real = root.resolve(strict=True)
        current = (root / relative.lstrip("/")).resolve(strict=True)
        current.relative_to(root_real)
    except (OSError, ValueError) as error:
        raise RuntimeError("memory cgroup path is unavailable or escapes its controller mount") from error

    remaining: list[int] = []
    while True:
        try:
            current.relative_to(root_real)
        except ValueError as error:
            raise RuntimeError("memory cgroup ancestry escapes its controller mount") from error
        if not current.is_dir() or current.is_symlink():
            raise RuntimeError("memory cgroup ancestry is unreadable")
        limit_path, usage_path = current / limit_name, current / usage_name
        if not limit_path.is_file() or not usage_path.is_file():
            raise RuntimeError("memory cgroup limit or usage is missing; refusing to assume unlimited")
        if version == "v2":
            try:
                limit_text = limit_path.read_text(encoding="ascii").strip()
            except (OSError, UnicodeError) as error:
                raise RuntimeError("memory cgroup limit is unreadable") from error
            usage = _read_decimal(usage_path, "memory cgroup usage")
            if limit_text != "max":
                if not limit_text.isdecimal():
                    raise RuntimeError("memory cgroup limit is malformed")
                remaining.append(max(0, int(limit_text) - usage) // MIB)
        else:
            limit = _read_decimal(limit_path, "memory cgroup limit")
            usage = _read_decimal(usage_path, "memory cgroup usage")
            # cgroup v1's documented very-large values represent explicit unlimited.
            if limit < (1 << 60):
                remaining.append(max(0, limit - usage) // MIB)
        if current == root_real:
            break
        parent = current.parent
        if parent == current:
            raise RuntimeError("memory cgroup ancestry does not reach its mount root")
        current = parent
    return min(remaining) if remaining else None


def check_budget(
    requested_mib: int,
    reserve_mib: int = DEFAULT_RESERVE_MIB,
    release_pid: int | None = None,
    *,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> tuple[bool, dict[str, int | None]]:
    _integer(requested_mib, "requested memory", minimum=1)
    _integer(reserve_mib, "reserve")
    memavailable = read_memavailable_mib(proc_root)
    cgroup_remaining = read_cgroup_remaining_mib(os.getpid(), proc_root=proc_root, cgroup_root=cgroup_root)
    release_rss = read_process_private_rss_mib(release_pid, proc_root) if release_pid is not None else 0
    available = projected_available_mib(memavailable, cgroup_remaining, release_rss)
    details: dict[str, int | None] = {
        "memavailable_mib": memavailable,
        "cgroup_remaining_mib": cgroup_remaining,
        "release_rss_mib": release_rss,
        "projected_available_mib": available,
        "requested_mib": requested_mib,
        "reserve_mib": reserve_mib,
    }
    return budget_satisfies(requested_mib, available, reserve_mib), details


def capture_owner_identity() -> dict:
    """Bind a pending reservation to the still-running launcher Bash process."""
    return _PROCESS.capture_identity(os.getppid())


def identity_state(identity: dict) -> str:
    return _PROCESS.identity_state(identity)


def _reservation_path(root: Path, profile: str) -> Path:
    return root / ".reservations" / f"{_PATHS.profile_name(profile)}.json"


def _reservation_read(path: Path, expected_profile: str) -> dict:
    _PATHS.private_entry(path)
    if path.stat().st_size > 4096:
        raise ResourceRefusal("allocation reservation is oversized")
    try:
        data = json.loads(path.read_text(encoding="ascii"), object_pairs_hook=_PATHS.no_duplicates,
                          parse_constant=lambda _value: _PATHS.require(False))
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise ResourceRefusal("allocation reservation is malformed") from error
    if not isinstance(data, dict) or data.keys() != {
        "schema_version", "profile", "requested_mib", "owner_identity"
    }:
        raise ResourceRefusal("allocation reservation is malformed")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ResourceRefusal("allocation reservation is unsupported")
    try:
        _PATHS.profile_name(data["profile"])
        _integer(data["requested_mib"], "reserved memory", minimum=1)
        _PROCESS._validate_identity(data["owner_identity"])
    except (ValueError, TypeError) as error:
        raise ResourceRefusal("allocation reservation is malformed") from error
    if data["profile"] != expected_profile or path.name != f"{expected_profile}.json":
        raise ResourceRefusal("allocation reservation name does not match its path")
    return data


def _reservations_locked(root: Path) -> list[tuple[Path, dict]]:
    directory = root / ".reservations"
    if not directory.exists() and not directory.is_symlink():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise ResourceRefusal("allocation reservation directory is unsafe")
    try:
        _PATHS.private_entry(directory)
        entries = sorted(directory.iterdir())
    except (OSError, ValueError) as error:
        raise ResourceRefusal("allocation reservation directory is unreadable") from error
    if len(entries) > MAX_RESERVATIONS:
        raise ResourceRefusal("too many pending allocation reservations")
    current: list[tuple[Path, dict]] = []
    stale: list[Path] = []
    for path in entries:
        if path.suffix != ".json" or path.is_symlink() or not path.is_file():
            raise ResourceRefusal("unexpected allocation reservation entry")
        data = _reservation_read(path, path.stem)
        state = identity_state(data["owner_identity"])
        if state == "stopped":
            stale.append(path)
        elif state == "running":
            current.append((path, data))
        else:
            raise ResourceRefusal("an allocation reservation owner is unknown; state was preserved")
    for path in stale:
        _PATHS.private_entry(path)
        path.unlink()
    if stale:
        _PATHS.fsync_dir(directory)
    return current


def reserve_allocation(root: Path, profile: str, requested_mib: int) -> dict:
    """Check and reserve one startup budget under the root allocation lock."""
    _PATHS.profile_name(profile)
    _integer(requested_mib, "requested memory", minimum=1)
    okay, _details = check_budget(requested_mib, DEFAULT_RESERVE_MIB)
    if not okay:
        raise ResourceRefusal("insufficient RAM budget; no profile or reservation was written")
    root = _PATHS.ensure_private_root(Path(root))
    with _PATHS.lock_file(root / ".allocation.lock"):
        reservations = _reservations_locked(root)
        target = _reservation_path(root, profile)
        owner = capture_owner_identity()
        _PROCESS._validate_identity(owner)
        existing = next((data for path, data in reservations if path == target), None)
        if existing is not None:
            if existing["owner_identity"] == owner and existing["requested_mib"] == requested_mib:
                return {"profile": profile, "requested_mib": requested_mib,
                        "reserved_mib": sum(item["requested_mib"] for _, item in reservations)}
            raise ResourceRefusal("this profile already has a pending allocation reservation")
        reserved = sum(data["requested_mib"] for _, data in reservations)
        okay, details = check_budget(requested_mib + reserved, DEFAULT_RESERVE_MIB)
        if not okay:
            raise ResourceRefusal("insufficient RAM budget; no profile or reservation was written")
        directory = root / ".reservations"
        if not directory.exists():
            _PATHS.private_mkdir(directory)
        else:
            _PATHS.private_entry(directory)
        payload = _PATHS.encoded({
            "schema_version": 1,
            "profile": profile,
            "requested_mib": requested_mib,
            "owner_identity": owner,
        })
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(target, flags, 0o600)
        except FileExistsError as error:
            raise ResourceRefusal("this profile already has a pending allocation reservation") from error
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _PATHS.fsync_dir(directory)
        except BaseException:
            try:
                target.unlink()
                _PATHS.fsync_dir(directory)
            except OSError:
                pass
            raise
        return {"profile": profile, "requested_mib": requested_mib,
                "reserved_mib": reserved + requested_mib,
                "projected_available_mib": details.get("projected_available_mib")}


def release_allocation(root: Path, profile: str, owner_identity: dict | None = None) -> dict:
    """Release only the reservation owned by this exact controller identity."""
    _PATHS.profile_name(profile)
    root = _PATHS.state_root(str(root), must_exist=True)
    owner = capture_owner_identity() if owner_identity is None else owner_identity
    _PROCESS._validate_identity(owner)
    with _PATHS.lock_file(root / ".allocation.lock"):
        target = _reservation_path(root, profile)
        if not target.exists() and not target.is_symlink():
            return {"profile": profile, "released": False}
        data = _reservation_read(target, profile)
        if data["owner_identity"] != owner:
            raise ResourceRefusal("allocation reservation belongs to another controller; state was preserved")
        _PATHS.private_entry(target)
        target.unlink()
        _PATHS.fsync_dir(target.parent)
        return {"profile": profile, "released": True}


class Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise ResourceRefusal("invalid VM resource command")


def main() -> int:
    parser = Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    defaults = commands.add_parser("defaults")
    defaults.add_argument("--depth", type=int, required=True)
    check = commands.add_parser("check")
    check.add_argument("--requested-mib", type=int, required=True)
    check.add_argument("--reserve-mib", type=int, default=DEFAULT_RESERVE_MIB)
    check.add_argument("--release-pid", type=int)
    reserve = commands.add_parser("reserve")
    reserve.add_argument("--state-root", type=Path, required=True)
    reserve.add_argument("--profile", required=True)
    reserve.add_argument("--requested-mib", type=int, required=True)
    release = commands.add_parser("release")
    release.add_argument("--state-root", type=Path, required=True)
    release.add_argument("--profile", required=True)
    args = parser.parse_args()
    try:
        if args.command == "defaults":
            print(json.dumps({"depth": args.depth, "memory_gib": default_memory_gib(args.depth)}, sort_keys=True))
        elif args.command == "check":
            okay, details = check_budget(args.requested_mib, args.reserve_mib, args.release_pid)
            print(json.dumps({"ok": okay, **details}, sort_keys=True))
            if not okay:
                print("REFUSED: insufficient memory budget for requested VM RAM.", file=sys.stderr)
                return 1
        elif args.command == "reserve":
            print(json.dumps(reserve_allocation(args.state_root, args.profile, args.requested_mib), sort_keys=True))
        else:
            print(json.dumps(release_allocation(args.state_root, args.profile), sort_keys=True))
    except (OSError, ResourceRefusal, ValueError, TypeError, RuntimeError) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
