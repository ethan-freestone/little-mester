#!/usr/bin/env python3
"""Run bounded, fixed-destination network and mount probes inside a VM guest.

The default is a read-only plan. Live probes require both ``--execute`` and an
exact ``--confirm-run-id``; reports contain fixed check IDs and result enums,
never response bodies, addresses, URLs, credentials, or arbitrary command output.
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import os
import re
import socket
import stat
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

SCHEMA_VERSION = 1
RUN_ID_RE = re.compile(r"[a-f0-9]{32}")
PROXY = "http://10.0.2.100:3128"
CHECKS = (
    "guest_identity",
    "workspace_mount",
    "ancestor_capabilities_absent",
    "qemu_host_gateway_blocked",
    "metadata_endpoint_blocked",
    "direct_private_subnet_blocked",
    "direct_public_tcp_blocked",
    "direct_dns_blocked",
    "direct_udp_blocked",
    "proxy_private_destination_denied",
    "proxy_public_http_available",
    "proxy_public_https_available",
)
GATEWAY_PORTS = (22, 80, 443, 2375, 2376, 3128, 8080, 11434, 19532)
PUBLIC_TCP_PORTS = (80, 443)
PRIVATE_TARGETS = (
    ("10.0.0.1", (22, 80, 443)),
    ("172.16.0.1", (22, 80, 443)),
    ("192.168.0.1", (22, 80, 443)),
)
PROXY_PRIVATE_TARGETS = (
    "http://127.0.0.1:80/",
    "http://10.0.2.2:80/",
    "http://169.254.169.254:80/",
    "http://10.0.0.1:80/",
    "http://172.16.0.1:80/",
    "http://192.168.0.1:80/",
)


class IsolationCheckError(ValueError):
    """Unsafe report destination or malformed run identity."""


def _run_id(value: str | None) -> str:
    selected = value or os.urandom(16).hex()
    if RUN_ID_RE.fullmatch(selected) is None:
        raise IsolationCheckError("run ID must be 32 lowercase hexadecimal characters")
    return selected


def _guest_identity(expected_depth: int | None = None, expected_max_depth: int | None = None) -> bool:
    identity = Path("/etc/lab-vm-recursion")
    try:
        info = identity.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid not in {0, os.getuid()} or stat.S_IMODE(info.st_mode) & 0o022
                or info.st_size > 128):
            return False
        match = re.fullmatch(r"(0|[1-9][0-9]?) (0|[1-9][0-9]?)\n?",
                             identity.read_text(encoding="ascii"))
        if not match:
            return False
        depth, maximum = map(int, match.groups())
        return (depth <= maximum <= 32
                and (expected_depth is None or depth == expected_depth)
                and (expected_max_depth is None or maximum == expected_max_depth))
    except (OSError, UnicodeError):
        return False


def _workspace_mount() -> bool:
    try:
        for line in Path("/proc/mounts").read_text(encoding="ascii").splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[1] == "/workspace":
                return fields[2] == "virtiofs"
    except (OSError, UnicodeError):
        pass
    return False


def _ancestor_capabilities_absent() -> bool:
    forbidden = (
        "VM_SSH_CONTROL_PATH", "VM_BROWSER_BRIDGE_DIR", "VM_BROWSER_BRIDGE_TOKEN_FILE",
        "VM_BROWSER_BRIDGE_PORT", "VM_BROWSER_BRIDGE_SSH_KEY",
    )
    return (all(name not in os.environ for name in forbidden)
            and os.environ.get("METRICS_URL") in {None, "", "disabled"})


def _tcp_connects(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _direct_dns_responds(timeout: float = 1.5) -> bool:
    """Send a single fixed DNS question directly; any reply means egress."""
    transaction_id = b"LM"
    question = b"\x07example\x03com\x00\x00\x01\x00\x01"
    packet = transaction_id + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + question
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.settimeout(timeout)
        probe.connect(("1.1.1.1", 53))
        probe.send(packet)
        response = probe.recv(4096)
        # Any correlated reply proves the direct DNS path is reachable, including
        # NXDOMAIN responses and answers with an empty answer section.
        return len(response) >= 12 and response[:2] == transaction_id and response[2] & 0x80 != 0
    except OSError:
        return False
    finally:
        probe.close()


def _direct_udp_responds(timeout: float = 1.5) -> bool:
    """Send one fixed STUN binding request; only a correlated response proves egress."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    transaction = os.urandom(12)
    request = b"\x00\x01\x00\x00\x21\x12\xa4\x42" + transaction
    try:
        probe.settimeout(timeout)
        probe.connect(("74.125.250.129", 19302))
        probe.send(request)
        response = probe.recv(1024)
        return (len(response) >= 20 and response[:2] == b"\x01\x01"
                and response[4:8] == b"\x21\x12\xa4\x42"
                and response[8:20] == transaction)
    except OSError:
        return False
    finally:
        probe.close()


def _proxy_private_status(url: str, timeout: float = 4.0) -> int | None:
    connection = http.client.HTTPConnection("10.0.2.100", 3128, timeout=timeout)
    try:
        connection.request("GET", url, headers={"Connection": "close"})
        response = connection.getresponse()
        response.read(256)
        return response.status
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def _proxy_public_https(timeout: float = 8.0) -> int | None:
    opener = build_opener(ProxyHandler({"http": PROXY, "https": PROXY}))
    request = Request("https://example.com/", headers={"Connection": "close"})
    try:
        with opener.open(request, timeout=timeout) as response:
            response.read(256)
            return response.status
    except HTTPError as error:
        return error.code
    except (OSError, http.client.HTTPException, URLError, TimeoutError):
        return None


def _proxy_public_http(timeout: float = 8.0) -> int | None:
    connection = http.client.HTTPConnection("10.0.2.100", 3128, timeout=timeout)
    try:
        connection.request("GET", "http://example.com/", headers={"Connection": "close"})
        response = connection.getresponse()
        response.read(256)
        return response.status
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def _report_path(path_value: str | None) -> Path | None:
    if path_value is None:
        return None
    path = Path(path_value)
    if not path.is_absolute() or ".." in path.parts or path.resolve(strict=False) != path:
        raise IsolationCheckError("report path must be absolute and canonical")
    parent = path.parent
    info = parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700 or parent.is_symlink()):
        raise IsolationCheckError("report parent must be an existing private 0700 directory")
    if path.exists() or path.is_symlink():
        raise IsolationCheckError("report path already exists")
    return path


def _write_report(path: Path, report: dict[str, Any]) -> None:
    encoded = (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    if len(encoded) > 128 * 1024:
        raise IsolationCheckError("isolation report exceeds its size limit")
    fd, name = tempfile.mkstemp(prefix=".isolation-", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def run_probes(
    *,
    run_id: str,
    tcp_probe: Callable[[str, int], bool] = _tcp_connects,
    dns_probe: Callable[[], bool] = _direct_dns_responds,
    udp_probe: Callable[[], bool] = _direct_udp_responds,
    private_proxy_probe: Callable[[str], int | None] | None = None,
    public_http_probe: Callable[[], int | None] = _proxy_public_http,
    public_proxy_probe: Callable[[], int | None] = _proxy_public_https,
    guest_identity_probe: Callable[[], bool] | None = None,
    workspace_probe: Callable[[], bool] = _workspace_mount,
    ancestor_probe: Callable[[], bool] = _ancestor_capabilities_absent,
    expected_depth: int | None = None,
    expected_max_depth: int | None = None,
) -> dict[str, Any]:
    """Run fixed, bounded checks and retain only booleans/status enums."""
    started = time.monotonic()
    checks: list[dict[str, str]] = []

    def add(name: str, passed: bool) -> None:
        checks.append({"id": name, "status": "PASS" if passed else "FAIL"})

    identity_ok = (guest_identity_probe() if guest_identity_probe is not None
                   else _guest_identity(expected_depth, expected_max_depth))
    add("guest_identity", identity_ok)
    add("workspace_mount", workspace_probe())
    add("ancestor_capabilities_absent", ancestor_probe())
    add("qemu_host_gateway_blocked", not any(
        tcp_probe("10.0.2.2", port) for port in GATEWAY_PORTS
    ))
    add("metadata_endpoint_blocked", not any(
        tcp_probe("169.254.169.254", port) for port in (80, 443)
    ))
    add("direct_private_subnet_blocked", not any(
        tcp_probe(host, port)
        for host, ports in PRIVATE_TARGETS
        for port in ports
    ))
    add("direct_public_tcp_blocked", not any(
        tcp_probe("1.1.1.1", port) for port in PUBLIC_TCP_PORTS
    ))
    add("direct_dns_blocked", not dns_probe())
    add("direct_udp_blocked", not udp_probe())
    private_probe = private_proxy_probe or (lambda url: _proxy_private_status(url))
    add("proxy_private_destination_denied", all(
        private_probe(url) == 403 for url in PROXY_PRIVATE_TARGETS
    ))
    public_http_status = public_http_probe()
    add("proxy_public_http_available", type(public_http_status) is int and 200 <= public_http_status < 400)
    public_status = public_proxy_probe()
    add("proxy_public_https_available", type(public_status) is int and 200 <= public_status < 400)

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "created_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "result": "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL",
        "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", help="32-character lowercase hexadecimal evidence ID")
    parser.add_argument("--execute", action="store_true", help="run the bounded guest probes")
    parser.add_argument("--confirm-run-id", help="must exactly match --run-id to authorize probes")
    parser.add_argument("--expected-depth", type=int, choices=(0, 1, 2),
                        help="require this exact persisted guest recursion depth")
    parser.add_argument("--expected-max-depth", type=int, choices=(2,),
                        help="require the campaign recursion cap (2)")
    parser.add_argument("--report", help="new report path in an existing private 0700 directory")
    args = parser.parse_args(argv)
    try:
        run_id = _run_id(args.run_id)
        report_path = _report_path(args.report)
    except (IsolationCheckError, OSError) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2

    if not args.execute:
        print(json.dumps({
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "result": "NOT_RUN",
            "planned_checks": list(CHECKS),
            "authorization": "rerun with --execute --confirm-run-id <same run ID>",
        }, sort_keys=True))
        return 0
    if args.confirm_run_id != run_id:
        print("BLOCKED: --execute requires an exact matching --confirm-run-id.", file=sys.stderr)
        return 2
    if (args.expected_depth is None) != (args.expected_max_depth is None):
        print("BLOCKED: --expected-depth and --expected-max-depth must be supplied together.", file=sys.stderr)
        return 2

    try:
        result = run_probes(run_id=run_id, expected_depth=args.expected_depth,
                            expected_max_depth=args.expected_max_depth)
        if report_path is not None:
            _write_report(report_path, result)
        print(json.dumps(result, sort_keys=True))
    except (OSError, IsolationCheckError) as error:
        print(f"BLOCKED: isolation probes could not be completed ({type(error).__name__}).", file=sys.stderr)
        return 2
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
