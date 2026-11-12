#!/usr/bin/env python3
"""Owner-invoked persistent parent up orchestration; never auto-launched."""

from __future__ import annotations

import argparse
import importlib.util
import os
import pathlib
import secrets
import socket
import subprocess
import sys
import time


PARENT_LAUNCH_ENABLED = True
STARTUP_TIMEOUT_SECONDS = 900


class Refused(RuntimeError):
    pass


def _load(name: str):
    path = pathlib.Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"parent_{name.replace('-', '_')}", path)
    if spec is None or spec.loader is None:
        raise Refused(f"protected parent module is unavailable: {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _spawn_supervisor(state, lock_fd: int, run_id: str, ssh_port: int,
                      *, home: str | None = None) -> subprocess.Popen:
    handoff_read, handoff_write = os.pipe2(os.O_CLOEXEC)
    proc = None
    try:
        args = [os.path.realpath(sys.executable), "-I",
                str(pathlib.Path(__file__).with_name("parent-supervisor.py").resolve()),
                "--parent-id", state.PARENT_ID, "--run-id", run_id,
                "--lock-fd", str(lock_fd), "--handoff-fd", str(handoff_read)]
        if home is not None:
            args.extend(("--state-home", str(pathlib.Path(home).resolve(strict=True))))
        args.extend(("--ssh-port", str(ssh_port)))
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=None, close_fds=True,
                                pass_fds=(lock_fd, handoff_read), start_new_session=True)
        os.write(handoff_write, b"COMMIT\n")
        return proc
    except BaseException:
        for descriptor in (handoff_read, handoff_write):
            try:
                os.close(descriptor)
            except OSError:
                pass
        if proc is not None:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                # Do not signal an uncommitted supervisor by a recyclable PID.
                pass
        raise
    finally:
        for descriptor in (handoff_read, handoff_write):
            try:
                os.close(descriptor)
            except OSError:
                pass


def up(capacity_gib: int = 64, *, home: str | None = None,
       startup_timeout_seconds: int = STARTUP_TIMEOUT_SECONDS) -> dict:
    """Create/reuse one parent and return only after verified RUNNING readiness."""
    if not PARENT_LAUNCH_ENABLED:
        raise Refused("parent launch is disabled by its protected configuration gate")
    if (type(capacity_gib) is not int or not 1 <= capacity_gib <= 4096
            or type(startup_timeout_seconds) is not int
            or not 1 <= startup_timeout_seconds <= 1800):
        raise Refused("parent capacity or startup timeout is outside the supported bound")
    state = _load("parent-state")
    provision = _load("parent-provision")
    supervisor = _load("parent-supervisor")
    current = state.read_status(home=home)
    if current["status"] == "RUNNING":
        if current["disk"]["virtual_capacity_gib"] != capacity_gib:
            raise Refused("running parent capacity differs from the requested capacity")
        return current
    run_id = secrets.token_hex(16)
    ssh_port = _free_loopback_port()
    try:
        with state.lifecycle_lock(home=home) as lock_fd:
            current = state.read_status(home=home)
            if current["status"] == "RUNNING":
                if current["disk"]["virtual_capacity_gib"] != capacity_gib:
                    raise Refused("running parent capacity differs from the requested capacity")
                return current
            if current["status"] not in ("NOT_CREATED", "UNPROVISIONED", "STOPPED"):
                raise Refused(f"parent cannot start from status {current['status']}; preserve evidence")
            parent = state.initialize(capacity_gib, home=home, lock_fd=lock_fd)
            parent = provision.create_initial_disk(
                state=state, capacity_gib=capacity_gib,
                qemu_img=supervisor._protected_tool("qemu-img", ("/usr/bin/qemu-img",)),
                curl=supervisor._protected_tool("curl", ("/usr/bin/curl",)),
                lock_fd=lock_fd, home=home)
            supervisor._prepare_parent_seed(state, lock_fd, home)
            proc = _spawn_supervisor(state, lock_fd, run_id, ssh_port, home=home)
    except (OSError, ValueError, state.Refused, provision.Refused,
            subprocess.SubprocessError, RuntimeError) as error:
        raise Refused(f"parent provisioning refused: {error}") from error

    deadline = time.monotonic() + startup_timeout_seconds
    last_status = {"status": "AMBIGUOUS"}
    try:
        while time.monotonic() < deadline:
            last_status = state.read_status(home=home)
            if last_status["status"] == "RUNNING" and last_status.get("run_id") == run_id:
                return last_status
            if last_status["status"] in ("STARTING", "RUNNING") and last_status.get("run_id") != run_id:
                raise Refused("a different parent run appeared during supervisor handoff")
            if proc.poll() is not None:
                raise Refused(f"parent supervisor/startup failed; status is {last_status['status']}")
            time.sleep(0.1)
        raise Refused("parent startup/readiness exceeded its bounded deadline")
    except Refused:
        status = state.read_status(home=home)
        if status["status"] in ("STARTING", "RUNNING") and status.get("run_id") == run_id:
            try:
                state.request_supervisor_stop(home=home, timeout_seconds=60)
            except state.Refused as error:
                raise Refused(f"startup failed and bounded rollback is ambiguous: {error}") from error
        raise
    finally:
        proc.poll()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capacity-gib", type=int, default=64)
    args = parser.parse_args()
    try:
        status = up(args.capacity_gib)
    except (OSError, ValueError, subprocess.SubprocessError, Refused) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2
    print(f"RUNNING parent_id={status['parent_id']} run_id={status['run_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
