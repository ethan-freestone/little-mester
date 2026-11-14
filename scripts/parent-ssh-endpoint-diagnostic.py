#!/usr/bin/env python3
"""One-shot, strict SSH endpoint identity diagnostic for an owner-started parent."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import socket
import stat
import subprocess
import time


class DiagnosticRefused(RuntimeError):
    pass


def _private_regular(path: pathlib.Path) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise DiagnosticRefused("required SSH state file is unavailable") from error
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise DiagnosticRefused("required SSH state file is unsafe")


def _fingerprint(data: bytes, *, public_file: pathlib.Path | None = None) -> str | None:
    argv = ["/usr/bin/ssh-keygen", "-lf", str(public_file)] if public_file else [
        "/usr/bin/ssh-keygen", "-lf", "-"]
    try:
        if public_file:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=5, check=False)
        else:
            result = subprocess.run(argv, input=data, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(rb"\bSHA256:[A-Za-z0-9+/]{43}\b", result.stdout)
    return match.group(0).decode("ascii") if result.returncode == 0 and match else None


def _key_lines(payload: bytes) -> list[bytes]:
    keys = set()
    for line in payload.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[1] == b"ssh-ed25519":
            keys.add(fields[1] + b" " + fields[2] + b"\n")
    return sorted(keys)


def _port(state_dir: pathlib.Path) -> int:
    path = state_dir / "ssh_port"
    _private_regular(path)
    raw = path.read_text().strip()
    if not re.fullmatch(r"[0-9]{1,5}", raw):
        raise DiagnosticRefused("recorded SSH port is invalid")
    value = int(raw)
    if not 1024 <= value <= 65535:
        raise DiagnosticRefused("recorded SSH port is outside the valid range")
    return value


def _wait_for_running_attempt(lab_vm: pathlib.Path, state_dir: pathlib.Path,
                              deadline_seconds: float) -> tuple[str, int]:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            status = subprocess.run([str(lab_vm), "parent", "status", "--json"],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, timeout=5, check=False)
            state = json.loads(status.stdout) if status.returncode == 0 else {}
            if state.get("status") in ("STARTING", "RUNNING") and re.fullmatch(
                    r"[0-9a-f]{32}", str(state.get("run_id", ""))):
                port = _port(state_dir)
                return state["run_id"], port
        except (OSError, subprocess.SubprocessError, ValueError, DiagnosticRefused):
            pass
        time.sleep(0.25)
    raise DiagnosticRefused("no validated STARTING/RUNNING attempt with an SSH port appeared")


def _presented_key(port: int) -> list[bytes]:
    try:
        result = subprocess.run(["/usr/bin/ssh-keyscan", "-T", "3", "-t", "ed25519",
                                 "-p", str(port), "127.0.0.1"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    return _key_lines(result.stdout)


def _wait_for_listener(port: int, seconds: int) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _known_hosts_key(known_hosts: pathlib.Path, port: int) -> tuple[str, str | None]:
    lookup = f"[127.0.0.1]:{port}"
    try:
        result = subprocess.run(["/usr/bin/ssh-keygen", "-F", lookup, "-f", str(known_hosts)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return "LOOKUP_ERROR", None
    if result.returncode != 0:
        return "MISSING", None
    keys = _key_lines(result.stdout)
    fps = {_fingerprint(line) for line in keys}
    fps.discard(None)
    if len(fps) != 1:
        return "AMBIGUOUS" if fps else "MISSING", None
    return "FOUND", next(iter(fps))


def _strict_probe(port: int, control_key: pathlib.Path, known_hosts: pathlib.Path,
                  expected: str) -> tuple[str, str, str]:
    argv = ["/usr/bin/ssh", "-F", "/dev/null", "-vvv", "-T", "-p", str(port),
            "-i", str(control_key), "-o", f"UserKnownHostsFile={known_hosts}",
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "StrictHostKeyChecking=yes",
            "-o", "HostKeyAlgorithms=ssh-ed25519", "-o", "IdentitiesOnly=yes",
            "-o", "IdentityAgent=none", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
            "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
            "-o", "ProxyCommand=none", "-o", "ProxyJump=none", "-o", "LogLevel=DEBUG3",
            "mester@127.0.0.1", "true"]
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=8, check=False)
    except subprocess.TimeoutExpired as error:
        stderr = error.stderr or b""
        observed = _server_fingerprint(stderr)
        return "TIMEOUT", _rejection_label(stderr), _compare(observed, expected)
    except OSError:
        return "NOT_RUN", "SSH_EXEC_FAILED", "UNAVAILABLE"
    stderr = result.stderr.lower()
    observed = _server_fingerprint(result.stderr)
    endpoint_match = _compare(observed, expected)
    if b"host key verification failed" in stderr or b"remote host identification has changed" in stderr:
        return "REJECTED", _rejection_label(stderr), endpoint_match
    if b"host key" in stderr and (b"matches" in stderr or b"known and matches" in stderr):
        return "ACCEPTED", "NONE", endpoint_match
    if result.returncode == 0:
        return "ACCEPTED", "NONE", endpoint_match
    if b"permission denied" in stderr:
        return "ACCEPTED", "AUTH_REJECTED_AFTER_HOSTKEY", endpoint_match
    return "INDETERMINATE", "SSH_FAILED_OTHER", endpoint_match


def _server_fingerprint(stderr: bytes) -> str | None:
    match = re.search(rb"server host key: ssh-ed25519 (SHA256:[A-Za-z0-9+/]{43})",
                      stderr, re.IGNORECASE)
    return match.group(1).decode("ascii") if match else None


def _compare(actual: str | None, expected: str) -> str:
    return "UNAVAILABLE" if actual is None else "MATCH" if actual == expected else "MISMATCH"


def _rejection_label(stderr: bytes) -> str:
    text = stderr.lower()
    if b"remote host identification has changed" in text or b"offending ed25519 key" in text:
        return "CHANGED_KEY"
    if b"no ed25519 host key is known" in text or b"no matching host key" in text:
        return "NO_MATCHING_PIN"
    if b"host key verification failed" in text:
        return "HOST_KEY_REJECTED_OTHER"
    return "SSH_REJECTED_OTHER"


def diagnose(install_root: pathlib.Path, state_dir: pathlib.Path,
             wait_seconds: int) -> dict[str, str]:
    if not 1 <= wait_seconds <= 180:
        raise DiagnosticRefused("wait bound must be 1..180 seconds")
    lab_vm = install_root / "lab-vm"
    if not lab_vm.is_file() or not os.access(lab_vm, os.X_OK):
        raise DiagnosticRefused("trusted installed lab-vm is unavailable")
    run_id, port = _wait_for_running_attempt(lab_vm, state_dir, wait_seconds)
    host_public = state_dir / "ssh_host_ed25519_key.pub"
    control_key = state_dir / "id_vm_control"
    known_hosts = state_dir / "known_hosts"
    for path in (host_public, control_key, known_hosts):
        _private_regular(path)

    expected = _fingerprint(b"", public_file=host_public)
    if expected is None:
        raise DiagnosticRefused("expected public host-key fingerprint could not be read")
    reachable = _wait_for_listener(port, min(wait_seconds, 60))
    presented = _presented_key(port) if reachable else []
    endpoint_fps = {_fingerprint(line) for line in presented}
    endpoint_fps.discard(None)
    if len(endpoint_fps) == 1:
        endpoint_match = "MATCH" if expected in endpoint_fps else "MISMATCH"
    elif not endpoint_fps:
        endpoint_match = "UNAVAILABLE"
    else:
        endpoint_match = "AMBIGUOUS"
    lookup, pin_fp = _known_hosts_key(known_hosts, port)
    pin_match = "MATCH" if pin_fp == expected else "MISMATCH" if pin_fp else "UNAVAILABLE"
    strict, reason, strict_endpoint_match = _strict_probe(port, control_key, known_hosts, expected)

    # Refuse to label evidence from a run/port that changed while probing.
    status = _run_status(lab_vm)
    if status.get("run_id") != run_id or _port(state_dir) != port:
        raise DiagnosticRefused("parent run or SSH port changed during diagnostic")
    return {"RUN_STATE_STABLE": "YES", "ENDPOINT_REACHABLE": "YES" if reachable else "NO",
            "ENDPOINT_KEY_VS_EXPECTED": endpoint_match,
            "KNOWN_HOST_LOOKUP": lookup, "PIN_VS_EXPECTED": pin_match,
            "STRICT_HOSTKEY": strict, "STRICT_REASON": reason,
            "STRICT_ENDPOINT_KEY_VS_EXPECTED": strict_endpoint_match}


def _run_status(lab_vm: pathlib.Path) -> dict:
    result = subprocess.run([str(lab_vm), "parent", "status", "--json"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, timeout=5, check=False)
    return json.loads(result.stdout) if result.returncode == 0 else {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-root", type=pathlib.Path, required=True)
    parser.add_argument("--state-dir", type=pathlib.Path, default=pathlib.Path.home() /
                        ".local/state/little-mester/parent-v3/default")
    parser.add_argument("--wait-seconds", type=int, default=90)
    args = parser.parse_args()
    try:
        result = diagnose(args.install_root.resolve(), args.state_dir.resolve(), args.wait_seconds)
    except (DiagnosticRefused, OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"DIAGNOSTIC=REFUSED LABEL={type(error).__name__}")
        return 2
    for key, value in result.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
