#!/usr/bin/env python3
"""Report the current execution layer and Little Mester checkout context."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def command_output(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def detect_virtualization(mode: str) -> str | None:
    detected = command_output(["systemd-detect-virt", f"--{mode}"])
    if detected and detected.lower() != "none":
        return detected

    if mode == "container":
        if Path("/.dockerenv").exists():
            return "docker"
        if Path("/run/.containerenv").exists():
            return "podman"
        try:
            cgroup = Path("/proc/1/cgroup").read_text(errors="replace").lower()
        except OSError:
            return None
        for marker, name in (("docker", "docker"), ("containerd", "containerd"),
                             ("kubepods", "kubernetes"), ("libpod", "podman"),
                             ("lxc", "lxc")):
            if marker in cgroup:
                return name
        return None

    try:
        vendor = Path("/sys/class/dmi/id/sys_vendor").read_text(errors="replace").strip().lower()
        product = Path("/sys/class/dmi/id/product_name").read_text(errors="replace").strip().lower()
    except OSError:
        vendor, product = "", ""
    if "qemu" in vendor or "kvm" in vendor or "qemu" in product or "kvm" in product:
        return "kvm"
    try:
        hypervisor = Path("/sys/hypervisor/type").read_text(errors="replace").strip()
    except OSError:
        hypervisor = ""
    return hypervisor or None


def recursion_context() -> dict[str, object]:
    depth_raw = os.environ.get("LAB_VM_RECURSION_DEPTH")
    maximum_raw = os.environ.get("LAB_VM_MAX_DEPTH")

    def parse_limit(raw: str | None) -> tuple[int | None, bool]:
        if raw is None:
            return None, True
        if len(raw) > 2 or not re.fullmatch(r"0|[1-9][0-9]*", raw):
            return None, False
        value = int(raw)
        return value, value <= 32

    depth, depth_valid = parse_limit(depth_raw)
    maximum, maximum_valid = parse_limit(maximum_raw)

    if depth_raw is None and maximum_raw is None:
        state = "unmarked"
    elif depth is not None and depth_valid and maximum_raw is None:
        state = "legacy-depth-only"
    elif (depth is None or maximum is None or not depth_valid or not maximum_valid
          or depth > maximum):
        state = "invalid-or-incomplete"
    else:
        state = "configured"

    return {
        "depth": depth,
        "max_depth": maximum,
        "metadata": state,
        "child_depth_available": state == "configured" and depth is not None and maximum is not None and depth < maximum,
    }


def git_context(repo: Path) -> dict[str, object]:
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"

    def git(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(repo), *args],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
        except OSError:
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain=v1", "--untracked-files=normal") if commit else None
    branch = git("branch", "--show-current") if commit else None
    return {
        "root": str(repo),
        "branch": branch or None,
        "commit": commit,
        "dirty": bool(status) if status is not None else None,
    }


def mount_type(path: Path) -> str | None:
    return command_output(["findmnt", "--noheadings", "--output", "FSTYPE", "--target", str(path)])


def collect(repo: Path) -> dict[str, object]:
    container = detect_virtualization("container")
    hypervisor = detect_virtualization("vm")
    cpu_flags: set[str] = set()
    try:
        for line in Path("/proc/cpuinfo").read_text(errors="replace").splitlines():
            if line.startswith("flags") or line.startswith("Features"):
                cpu_flags.update(re.findall(r"(?<!\S)(?:vmx|svm)(?!\S)", line))
    except OSError:
        pass

    kvm_path = Path("/dev/kvm")
    device_present = kvm_path.is_char_device()
    kvm_read_write = device_present and os.access(kvm_path, os.R_OK | os.W_OK)
    layers = []
    if hypervisor:
        layers.append({"kind": "virtual-machine", "technology": hypervisor})
    if container:
        layers.append({"kind": "container", "technology": container})

    return {
        "schema_version": 1,
        "runtime": {
            "kind": "container" if container else "vm" if hypervisor else "host",
            "container": container,
            "virtual_machine": hypervisor,
            "layers": layers,
            "pid1": command_output(["cat", "/proc/1/comm"]),
        },
        "recursion": recursion_context(),
        "kvm": {
            "device_present": device_present,
            "device_read_write": kvm_read_write,
            "cpu_virtualization": sorted(cpu_flags),
            "nested_kvm_preflight_ready": kvm_read_write and bool(cpu_flags),
        },
        "little_mester": git_context(repo),
        "workspace_filesystem": mount_type(repo),
    }


def summary(context: dict[str, object]) -> str:
    runtime = context["runtime"]
    recursion = context["recursion"]
    kvm = context["kvm"]
    checkout = context["little_mester"]
    assert isinstance(runtime, dict)
    assert isinstance(recursion, dict)
    assert isinstance(kvm, dict)
    assert isinstance(checkout, dict)

    if runtime.get("container"):
        layer = f"container ({runtime['container']})"
        if runtime.get("virtual_machine"):
            layer += f" inside VM ({runtime['virtual_machine']})"
    elif runtime.get("virtual_machine"):
        layer = f"VM ({runtime['virtual_machine']})"
    else:
        layer = "host (no VM/container detected)"
    depth = recursion.get("depth")
    maximum = recursion.get("max_depth")
    depth_text = "unset" if depth is None else str(depth)
    maximum_text = "unset" if maximum is None else str(maximum)
    commit = checkout.get("commit")
    commit_text = str(commit)[:12] if commit else "unknown"
    branch = checkout.get("branch") or "detached/unknown"
    dirty_state = checkout.get("dirty")
    dirty = "dirty" if dirty_state is True else "clean" if dirty_state is False else "unknown"
    cpu = ",".join(kvm.get("cpu_virtualization", [])) or "not exposed"
    kvm_access = "preflight-ready" if kvm.get("nested_kvm_preflight_ready") else "not usable by this process"
    mount = context.get("workspace_filesystem") or "unknown"

    return "\n".join((
        f"Environment: {layer}",
        f"Recursion: depth={depth_text}, max={maximum_text} ({recursion['metadata']})",
        f"Nested KVM: {kvm_access}; device={'present' if kvm['device_present'] else 'absent'}, CPU={cpu}",
        f"Little Mester: {commit_text} ({branch}; {dirty}) at {checkout.get('root')}",
        f"Workspace filesystem: {mount}",
    ))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="Little Mester checkout containing lab-vm")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()
    context = collect(args.repo.resolve())
    if args.format == "json":
        print(json.dumps(context, sort_keys=True))
    else:
        print(summary(context))
    return 0


if __name__ == "__main__":
    sys.exit(main())
