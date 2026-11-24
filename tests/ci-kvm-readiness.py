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
        "opencode-binary-missing",
    )
    persistence_checks = (
        "identity-mismatch",
        "home-mismatch",
        "working-directory-mismatch",
        "parent-path-noncanonical",
        "parent-owner-or-mode",
        "parent-inaccessible",
        "fixture-path-preoccupied",
        "create-failed",
        "filesystem-flush-failed",
        "fixture-path-noncanonical",
        "fixture-symlink",
        "fixture-missing",
        "fixture-not-regular",
        "fixture-owner-mismatch",
        "fixture-mode-mismatch",
        "fixture-inaccessible",
        "fixture-unreadable",
        "fixture-content-mismatch",
        "mount-identity-unavailable",
        "fixture-not-on-root-filesystem",
        "mount-identity-changed",
    )
    if "kvm_persistence_create_authenticated" in text:
        if exit_status == 124:
            return "persistence-create-timeout"
        if exit_status == 255:
            return "persistence-create-session-lost"
        for check in persistence_checks:
            if f"kvm_persistence_check={check}" in text:
                return f"persistence-{check}"
        if "kvm_persistence_create=pass" in text and exit_status == 0:
            return "persistence-create-pass"
        return "persistence-create-command-failed"
    if "kvm_persistence_flush_authenticated" in text:
        if exit_status == 124:
            return "persistence-flush-timeout"
        if exit_status == 255:
            return "persistence-flush-session-lost"
        for check in persistence_checks:
            if f"kvm_persistence_check={check}" in text:
                return f"persistence-{check}"
        if "kvm_persistence_flush=pass" in text and exit_status == 0:
            return "persistence-flush-pass"
        return "persistence-flush-command-failed"
    if "kvm_persistence_remote_authenticated" in text:
        if exit_status == 124:
            return "persistence-check-timeout"
        if exit_status == 255:
            return "persistence-check-session-lost"
        for check in persistence_checks:
            if f"kvm_persistence_check={check}" in text:
                return f"persistence-{check}"
        content_hash = re.search(r"kvm_persistence_content_sha256=([0-9a-f]{64})", text)
        mount_hash = re.search(r"kvm_persistence_mount_sha256=([0-9a-f]{64})", text)
        if ("kvm_persistence_check=pass" in text and content_hash and mount_hash
                and exit_status == 0):
            return "persistence-pass"
        return "persistence-check-command-failed"
    if "kvm_restart_remote_authenticated" in text:
        if exit_status == 124:
            return "remote-readiness-timeout"
        if exit_status == 255:
            return "remote-readiness-session-lost"
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
