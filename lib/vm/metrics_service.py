#!/usr/bin/env python3
"""Own and verify the per-user loopback metrics service used by VM guests."""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _load_sibling(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    if spec is None or spec.loader is None:
        raise RuntimeError("VM metrics policy helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_PATHS = _load_sibling("vm_metrics_paths", "paths.py")
_PROCESS = _load_sibling("vm_metrics_process", "process.py")

RUN_ID = "metrics_host"
ROLE = "metrics"
LOGGER_ROLE = "metrics_logger"
DEFAULT_PORT = 19532
START_TIMEOUT = 5.0
STOP_TIMEOUT = 5.0


class MetricsServiceError(Exception):
    """A metrics service state could not be safely established or verified."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise MetricsServiceError(message)


def _port(value: int | str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as error:
        raise MetricsServiceError("Metrics port must be an integer from 1 to 65535") from error
    _require(1 <= port <= 65535, "Metrics port must be an integer from 1 to 65535")
    return port


def _service_dir(state_root: str | Path, *, create: bool) -> Path | None:
    root = Path(state_root)
    if create:
        root = _PATHS.ensure_private_root(root)
    else:
        root = _PATHS.state_root(str(root), must_exist=False, scan=False)
        if not root.exists():
            return None
    vm_dir = root / "vm"
    service = vm_dir / "metrics"
    if create:
        if not vm_dir.exists():
            _PATHS.private_mkdir(vm_dir)
        else:
            _PATHS.private_entry(vm_dir)
        if not service.exists():
            _PATHS.private_mkdir(service)
        else:
            _PATHS.private_entry(service)
    else:
        if not vm_dir.exists() and not vm_dir.is_symlink():
            return None
        _PATHS.private_entry(vm_dir)
        if not service.exists() and not service.is_symlink():
            return None
        _PATHS.private_entry(service)
    return service


def _lock(service: Path, *, create: bool):
    path = service / "manager.lock"
    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    if create:
        flags |= os.O_CREAT
    try:
        fd = os.open(path, flags, 0o600)
        info = os.fstat(fd)
        _require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1,
            "Metrics service lock is not private regular state",
        )
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd
    except OSError as error:
        raise MetricsServiceError("Metrics service state could not be locked safely") from error


def _listener_present(port: int) -> bool:
    """Fail closed if any IPv4 or IPv6 listener owns this port."""
    try:
        tables = (Path("/proc/net/tcp"), Path("/proc/net/tcp6"))
        for table in tables:
            rows = table.read_text(encoding="ascii").splitlines()[1:]
            for row in rows:
                fields = row.split()
                if len(fields) < 4:
                    return True
                local_address, state = fields[1], fields[3]
                try:
                    local_port = int(local_address.rsplit(":", 1)[1], 16)
                except (IndexError, ValueError):
                    return True
                if local_port == port and state == "0A":
                    return True
        return False
    except OSError:
        # An unreadable kernel socket table is not evidence that the port is free.
        return True


def _listener_owned(identity: dict, port: int) -> bool:
    return _PROCESS.listener_owned_by(identity["pid"], port)


def _http_ready(port: int) -> bool:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/summary", headers={"Accept": "text/plain"}
    )
    try:
        with urllib.request.urlopen(request, timeout=0.5) as response:
            return response.status == 200 and response.headers.get_content_type() == "text/plain"
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def _registry_status(service: Path) -> tuple[dict | None, dict]:
    registry = service / "processes.json"
    if not registry.exists() and not registry.is_symlink():
        return None, {"state": "absent", "processes": {}}
    try:
        data = _PROCESS.registry_read(registry, RUN_ID)
        status = _PROCESS.registry_status(registry, RUN_ID)
        return data, status
    except (OSError, _PROCESS.ProcessPolicyError, _PATHS.PolicyError):
        return None, {"state": "unknown", "processes": {}}


def _status_locked(service: Path, port: int) -> dict:
    registry = service / "processes.json"
    data, status = _registry_status(service)
    if status["state"] == "unknown":
        return {"state": "unknown", "port": port}
    if status["state"] == "absent":
        state = "unknown" if _listener_present(port) else "absent"
        return {"state": state, "port": port}

    process_states = status.get("processes", {})
    main_state = process_states.get(ROLE, {}).get("state", "stopped")
    logger_state = process_states.get(LOGGER_ROLE, {}).get("state", "stopped")
    if main_state == "unknown" or logger_state == "unknown":
        return {"state": "unknown", "port": port}
    if main_state == "running":
        identity = data["processes"].get(ROLE, {}).get("identity") if data else None
        if (
            identity is not None
            and _listener_owned(identity, port)
            and _http_ready(port)
            and logger_state == "running"
        ):
            return {"state": "running", "port": port, "pid": identity["pid"]}
        return {"state": "unknown", "port": port}
    if logger_state == "running":
        # This is only the brief startup window after run-logged registered itself.
        if _listener_present(port):
            return {"state": "unknown", "port": port}
        return {"state": "starting", "port": port}
    if _listener_present(port):
        return {"state": "unknown", "port": port}
    return {"state": "stopped", "port": port}


def _detached_exec(payload: str) -> None:
    """Fork a short-lived launcher, then exec the owned process supervisor."""
    try:
        data = json.loads(payload)
        argv, env = data["argv"], data["env"]
        _require(
            isinstance(argv, list)
            and bool(argv)
            and all(isinstance(item, str) and item and "\0" not in item for item in argv)
            and isinstance(env, dict)
            and all(isinstance(key, str) and isinstance(value, str) for key, value in env.items()),
            "Invalid detached metrics process specification",
        )
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise MetricsServiceError("Invalid detached metrics process specification") from error
    try:
        child = os.fork()
    except OSError as error:
        raise MetricsServiceError("Metrics supervisor could not be detached") from error
    if child:
        return
    try:
        os.setsid()
        null_fd = os.open(os.devnull, os.O_RDWR)
        for target in (0, 1, 2):
            os.dup2(null_fd, target)
        if null_fd > 2:
            os.close(null_fd)
        os.execve(argv[0], argv, env)
    except BaseException:
        os._exit(127)


def status_service(state_root: str | Path, port: int | str = DEFAULT_PORT) -> dict:
    port = _port(port)
    service = _service_dir(state_root, create=False)
    if service is None:
        return {"state": "unknown" if _listener_present(port) else "absent", "port": port}
    fd = _lock(service, create=False)
    try:
        return _status_locked(service, port)
    finally:
        os.close(fd)


def ensure_service(
    state_root: str | Path,
    collector: str | Path | None = None,
    port: int | str = DEFAULT_PORT,
    *,
    startup_timeout: float = START_TIMEOUT,
) -> dict:
    port = _port(port)
    if not isinstance(startup_timeout, (int, float)) or not 0 < startup_timeout <= 30:
        raise MetricsServiceError("Metrics startup timeout must be from 0 to 30 seconds")
    service = _service_dir(state_root, create=True)
    assert service is not None
    fd = _lock(service, create=True)
    try:
        current = _status_locked(service, port)
        if current["state"] == "running":
            return current
        if current["state"] in {"unknown", "starting"}:
            if current["state"] == "starting":
                return _wait_ready(service, port, startup_timeout)
            raise MetricsServiceError(
                "Metrics port is occupied by an unverified listener; it was not changed"
            )

        registry = service / "processes.json"
        if current["state"] == "stopped":
            _PATHS.private_entry(registry)
            registry.unlink()
            _PATHS.fsync_dir(service)
        elif current["state"] != "absent":
            raise MetricsServiceError("Metrics service state is not safely restartable")
        if _listener_present(port):
            raise MetricsServiceError(
                "Metrics port is occupied by an unverified listener; it was not changed"
            )

        requested_source = (
            Path(collector)
            if collector is not None
            else Path(__file__).resolve().parents[2] / "agent/metrics/gather.py"
        )
        _require(not requested_source.is_symlink(), "Metrics collector is unavailable")
        source = requested_source.resolve(strict=True)
        _require(source.is_file(), "Metrics collector is unavailable")
        process_helper = Path(__file__).with_name("process.py").resolve(strict=True)
        _PROCESS.registry_initialize(registry, RUN_ID)
        argv = [
            sys.executable,
            str(process_helper),
            "run-logged",
            "--registry",
            str(registry),
            "--run-id",
            RUN_ID,
            "--role",
            ROLE,
            "--log",
            str(service / "metrics.log"),
            "--",
            sys.executable,
            str(source),
        ]
        env = {
            "HOME": str(Path.home()),
            "PATH": os.environ.get("PATH", os.defpath),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "METRICS_BIND_HOST": "127.0.0.1",
            "METRICS_BIND_PORT": str(port),
            "METRICS_HOST_PROC": "/proc",
            "METRICS_HOST_HWMON": "/sys/class/hwmon",
            "METRICS_HOST_THERMAL": "/sys/class/thermal",
        }
        try:
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "_spawn",
                    "--payload",
                    json.dumps({"argv": argv, "env": env}, separators=(",", ":")),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                check=True,
                timeout=3,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise MetricsServiceError("Metrics service process could not be started") from error
        return _wait_ready(service, port, startup_timeout)
    finally:
        os.close(fd)


def _wait_ready(service: Path, port: int, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = _status_locked(service, port)
        if current["state"] == "running":
            return current
        if current["state"] == "unknown":
            data, status = _registry_status(service)
            child = status.get("processes", {}).get(ROLE, {}).get("state")
            logger = status.get("processes", {}).get(LOGGER_ROLE, {}).get("state")
            if child == "running":
                identity = data["processes"].get(ROLE, {}).get("identity") if data else None
                if identity is None or _PROCESS.identity_state(identity) != "running":
                    raise MetricsServiceError(
                        "Metrics service process identity could not be verified"
                    )
                if _listener_present(port) and not _listener_owned(identity, port):
                    raise MetricsServiceError(
                        "Metrics port is occupied by an unverified listener; it was not changed"
                    )
                # The verified child may still be binding or initializing HTTP.
            elif logger == "unknown" or status.get("state") == "unknown":
                raise MetricsServiceError(
                    "Metrics service identity could not be verified"
                )
        time.sleep(0.05)
    raise MetricsServiceError("Metrics service did not become ready before the deadline")


def stop_service(state_root: str | Path, port: int | str = DEFAULT_PORT) -> dict:
    port = _port(port)
    service = _service_dir(state_root, create=False)
    if service is None:
        if _listener_present(port):
            raise MetricsServiceError(
                "Metrics port is occupied by an unverified listener; it was not changed"
            )
        return {"state": "absent", "port": port}
    fd = _lock(service, create=False)
    try:
        current = _status_locked(service, port)
        if current["state"] in {"absent", "stopped"}:
            return current
        if current["state"] != "running":
            raise MetricsServiceError(
                "Metrics service identity is not verified; no process was signalled"
            )
        registry = service / "processes.json"
        result = _PROCESS.registry_signal(registry, RUN_ID, LOGGER_ROLE, signal.SIGTERM, STOP_TIMEOUT)
        if result not in {"stopped", "running"}:
            raise MetricsServiceError("Metrics service shutdown identity became ambiguous")
        deadline = time.monotonic() + STOP_TIMEOUT
        while time.monotonic() < deadline:
            current = _status_locked(service, port)
            if current["state"] == "stopped":
                return current
            if current["state"] == "unknown":
                raise MetricsServiceError("Metrics port became an unverified listener during shutdown")
            time.sleep(0.05)
        raise MetricsServiceError("Metrics service did not stop before the deadline")
    finally:
        os.close(fd)


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    spawn = commands.add_parser("_spawn", help=argparse.SUPPRESS)
    spawn.add_argument("--payload", required=True)
    for name in ("ensure", "status", "verify", "stop"):
        command = commands.add_parser(name)
        command.add_argument("--state-root", required=True)
        command.add_argument("--port", default=DEFAULT_PORT, type=int)
        if name == "ensure":
            command.add_argument("--collector")
        if name in {"status", "verify"}:
            command.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        if args.command == "_spawn":
            _detached_exec(args.payload)
            return 0
        if args.command == "ensure":
            result = ensure_service(args.state_root, args.collector, args.port)
            print(result["port"])
            return 0
        if args.command == "status":
            result = status_service(args.state_root, args.port)
            if not args.quiet:
                print(json.dumps(result, sort_keys=True))
            return 0 if result["state"] in {"running", "stopped", "absent"} else 2
        if args.command == "verify":
            result = status_service(args.state_root, args.port)
            if not args.quiet:
                print(json.dumps(result, sort_keys=True))
            return 0 if result["state"] == "running" else 2
        result = stop_service(args.state_root, args.port)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (MetricsServiceError, _PATHS.PolicyError, _PROCESS.ProcessPolicyError, OSError, ValueError) as error:
        print(f"REFUSED: metrics service operation could not be verified: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
