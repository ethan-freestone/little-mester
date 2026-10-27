#!/usr/bin/env python3
"""Fail when SECURITY.md / README.md drift from the compose defaults.

This is intentionally a small, dependency-free check so it can run on the host before
Docker starts (verify-lab.sh calls it). It covers the claims that have drifted before:
agent memory/PID limits, egress defaults, the workspace mount, and local markdown links.

Usage: python3 scripts/check-docs-consistency.py
Exit: 0 = consistent, 1 = drift or broken link.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CHECKS = 0
FAILURES: list[str] = []


def ok(message: str) -> None:
    global CHECKS
    CHECKS += 1
    print(f"PASS  {message}")


def fail(message: str) -> None:
    global CHECKS
    CHECKS += 1
    FAILURES.append(message)
    print(f"FAIL  {message}")


def check(condition: bool, message: str) -> None:
    ok(message) if condition else fail(message)


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def agent_block(compose: str) -> str:
    """Return the text of the top-level `agent:` service only."""
    match = re.search(
        r"^  agent:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|^networks:)",
        compose,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise SystemExit("compose.yaml: could not find the top-level agent service")
    return match.group("body")


def field(block: str, key: str) -> str | None:
    match = re.search(rf"^\s+{re.escape(key)}:\s*(.+)$", block, re.MULTILINE)
    return match.group(1).strip() if match else None


def main() -> int:
    compose = read("compose.yaml")
    agent = agent_block(compose)
    security = read("SECURITY.md")
    readme = read("README.md")

    mem = field(agent, "mem_limit")
    pids = field(agent, "pids_limit")
    networks = field(agent, "networks") or ""
    read_only = field(agent, "read_only") or ""
    cap_drop = field(agent, "cap_drop") or ""
    security_opt = field(agent, "security_opt") or ""

    # ── Compose is the source of truth for the agent limits ─────────
    check(mem == "8g", f"compose agent mem_limit is 8g (found {mem!r})")
    check(pids == "512", f"compose agent pids_limit is 512 (found {pids!r})")
    check("egress" in networks and "internal" in networks,
          f"compose agent networks include egress + internal (found {networks!r})")
    check(read_only == "true", "compose agent root filesystem is read-only")
    check("ALL" in cap_drop, "compose agent drops ALL capabilities")
    check("no-new-privileges:true" in security_opt,
          "compose agent sets no-new-privileges")

    # ── SECURITY.md / README.md must repeat the same facts ──────────
    for name, text in (("SECURITY.md", security), ("README.md", readme)):
        check("mem_limit: 8g" in text, f"{name} states mem_limit: 8g")
        check("pids_limit: 512" in text, f"{name} states pids_limit: 512")
        check("enabled by default" in text.lower(),
              f"{name} states that egress is enabled by default")
        check("--isolated" in text, f"{name} documents --isolated as the disable switch")

    stale = [
        ("No internet by default", "the old 'no internet by default' claim"),
        ("no internet by default", "the old 'no internet by default' claim"),
        ("mem_limit: 4g", "the old 4g memory limit"),
        ("Egress is opt-in", "the old 'egress is opt-in' claim"),
        ("egress is opt-in", "the old 'egress is opt-in' claim"),
        ("Internet off at runtime", "the old 'internet off at runtime' claim"),
        ("zero route to the internet", "the old 'zero route' claim"),
        ("No proxy.", "the old 'no proxy' claim"),
    ]
    for name, text in (("SECURITY.md", security), ("README.md", readme)):
        for phrase, label in stale:
            check(phrase not in text, f"{name} does not repeat {label}")

    # ── One live decision log ───────────────────────────────────────
    root = read("DECISIONS.md")
    check("(moved)" in root.splitlines()[0],
          "root DECISIONS.md is a thin pointer, not a second log")
    check("docs/DECISIONS.md" in root,
          "root DECISIONS.md points at the live product decision log")
    check("## DECISION-" not in root,
          "root DECISIONS.md carries no competing decision entries")
    check((ROOT / "docs/vm-architecture.md").is_file(),
          "docs/vm-architecture.md exists")
    check("trust boundary" in read("docs/vm-architecture.md").lower(),
          "docs/vm-architecture.md describes the trust boundary")

    # ── Local markdown links must resolve ───────────────────────────
    link_files = [
        "README.md",
        "SECURITY.md",
        "DECISIONS.md",
        "docs/DECISIONS.md",
        "docs/vm-architecture.md",
        "docs/tickets/architecture/DECISIONS.md",
        "docs/tickets/archive/root-decisions-v1-2026-10-21.md",
    ]
    link_re = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)")
    for rel in link_files:
        path = ROOT / rel
        if not path.is_file():
            fail(f"{rel} exists for the link check")
            continue
        text = path.read_text(encoding="utf-8")
        # Ignore fenced code blocks, where links are examples rather than links.
        text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        for target in link_re.findall(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = target.split("#", 1)[0]
            if not target:
                continue
            resolved = (path.parent / target).resolve()
            check(resolved.exists(), f"{rel} link resolves: {target}")

        for marker in ("<<<<<<<", ">>>>>>>"):
            check(marker not in text, f"{rel} has no conflict marker {marker}")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} drift/link failure(s) across {CHECKS} checks")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print(f"All {CHECKS} documentation checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
