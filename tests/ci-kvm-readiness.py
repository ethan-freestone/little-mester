#!/usr/bin/env python3
"""Map bounded SSH readiness output to fixed, non-sensitive classifications."""

from __future__ import annotations

import argparse
import pathlib
import re
import sys


MAX_BYTES = 8192


def classify(exit_status: int, output: bytes) -> str:
    text = output[:MAX_BYTES].decode("utf-8", errors="replace").lower()
    if "[bounded-log: output truncated]" in text:
        return "diagnostic-overflow"
    checks = (
        "ssh-service-inactive",
        "guest-boot-id-unavailable",
        "guest-boot-id-invalid",
        "guest-boot-id-unchanged",
        "cloud-init-not-ready",
        "cloud-init-error",
        "persistence-file-missing",
        "opencode-binary-missing",
    )
    if "kvm_restart_remote_authenticated" in text:
        for check in checks:
            if f"kvm_restart_remote_check={check}" in text:
                return f"remote-{check}"
        if "kvm_restart_remote_ready" in text and exit_status == 0:
            return "ready"
        return "remote-command-failed"
    if exit_status == 124:
        return "attempt-timeout"
    if exit_status == 255:
        if re.search(r"remote host identification has changed|host key verification failed", text):
            return "host-key-verification"
        if re.search(r"permission denied|no mutual signature|no matching .* found", text):
            return "authentication-or-ssh-negotiation"
        if re.search(r"connection refused|connection reset|timed out|no route to host|kex_exchange_identification", text):
            return "transport-or-forwarding"
        return "ssh-client-failure"
    return "unexpected-ssh-exit"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exit-status", required=True, type=int)
    parser.add_argument("--input", required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        with args.input.open("rb") as stream:
            output = stream.read(MAX_BYTES)
    except OSError:
        print("diagnostic-unavailable")
        return 0
    print(classify(args.exit_status, output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
