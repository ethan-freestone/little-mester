#!/usr/bin/env python3
"""Minimal private lifecycle owner for one disposable hosted-CI QEMU guest.

The owner starts, restarts, stops, and reaps QEMU only. Acceptance checks run in
the foreground stage commands and communicate with this process over a private
Unix socket.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import pathlib
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
CONTRACT = HERE / "ci-kvm-contract.sh"
SIGNAL_HELPER = HERE / "ci-process-signal.py"
SIGNAL_SPEC = importlib.util.spec_from_file_location("ci_process_signal", SIGNAL_HELPER)
assert SIGNAL_SPEC and SIGNAL_SPEC.loader
PROCESS = importlib.util.module_from_spec(SIGNAL_SPEC)
SIGNAL_SPEC.loader.exec_module(PROCESS)
MARKER = "little-mester-hosted-kvm-integration"
MAX_MESSAGE = 4096


class Refused(RuntimeError):
    pass


def refuse(message: str) -> None:
    raise Refused(message)


def load_json(path: pathlib.Path, *, maximum: int = 16384) -> dict:
    fd = -1
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > maximum):
            refuse("private state file ownership, mode, or size is invalid")
        content = bytearray()
        while len(content) <= maximum:
            chunk = os.read(fd, maximum + 1 - len(content))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > maximum:
            refuse("private state file exceeds its read bound")
        value = json.loads(content.decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise Refused("private state file is missing or malformed") from error
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(value, dict):
        refuse("private state record is not an object")
    return value


def write_json(path: pathlib.Path, value: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def context(args: argparse.Namespace) -> dict:
    for name in ("source_sha", "workflow_sha"):
        value = getattr(args, name)
        if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
            refuse(f"{name} is invalid")
    if not args.run_id.isdigit() or not args.attempt.isdigit():
        refuse("workflow run identity is invalid")
    return {"version": 1, "uid": os.getuid(), "run_id": args.run_id,
            "attempt": args.attempt, "source_sha": args.source_sha,
            "workflow_sha": args.workflow_sha,
            "host_boot_id": pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()}


def proc_record(pid: int) -> dict:
    parent, group, session, starttime, comm, cmdline = PROCESS.process_identity(pid)
    status = pathlib.Path(f"/proc/{pid}/status").read_text(encoding="ascii")
    uid = int(next(line for line in status.splitlines() if line.startswith("Uid:")).split()[1])
    return {"pid": pid, "ppid": parent, "uid": uid, "pgrp": group,
            "session": session, "starttime": starttime, "comm": comm,
            "exe": os.path.realpath(f"/proc/{pid}/exe"),
            "argv_sha256": hashlib.sha256(cmdline).hexdigest(),
            "argv": [os.fsdecode(item) for item in cmdline.split(b"\0") if item]}


def state_paths(state_dir: pathlib.Path, ctx: dict) -> tuple[dict, pathlib.Path, pathlib.Path]:
    info = os.lstat(state_dir)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        refuse("run directory must be owner-owned mode 0700")
    state = load_json(state_dir / "state.json")
    if any(state.get(key) != value for key, value in ctx.items()):
        refuse("stage state does not belong to this workflow attempt")
    for field in ("overlay", "seed", "serial_log", "private_key", "known_hosts"):
        value = state.get(field)
        if not isinstance(value, str) or not pathlib.Path(value).is_absolute():
            refuse(f"state field {field} is invalid")
        path = pathlib.Path(value)
        if path.parent != state_dir and path.parent.parent != state_dir:
            refuse(f"state path {field} escapes the private run directory")
    overlay = pathlib.Path(state["overlay"])
    overlay_info = os.lstat(overlay)
    if (not stat.S_ISREG(overlay_info.st_mode) or overlay_info.st_uid != os.getuid()
            or overlay_info.st_dev != state.get("overlay_dev")
            or overlay_info.st_ino != state.get("overlay_ino")):
        refuse("disposable overlay identity changed")
    return state, overlay, state_dir / "owner.json"


def qemu_args(state: dict, mode: str) -> list[str]:
    command = [
        "/usr/bin/bash", "-c",
        'source "$1"; args=(); ci_kvm_qemu_args "$2" "$3" "$4" "$5" "$6" args; printf "%s\\0" "${args[@]}"',
        "ci-kvm-qemu-owner", str(CONTRACT), mode, state["overlay"], state["seed"],
        state["serial_log"], str(state["port"]),
    ]
    completed = subprocess.run(command, check=True, capture_output=True,
                               timeout=5, close_fds=True)
    args = [item.decode() for item in completed.stdout.split(b"\0") if item]
    if (not args or "-machine" not in args or not any("accel=kvm" in item for item in args)
            or any("accel=tcg" in item for item in args)):
        refuse("shared QEMU argument builder did not produce KVM-only arguments")
    return args


class Owner:
    def __init__(self, run_dir: pathlib.Path, ctx: dict):
        self.run_dir = run_dir
        self.ctx = ctx
        self.state, self.overlay, self.record_path = state_paths(run_dir, ctx)
        self.child: subprocess.Popen | None = None
        self.child_identity: dict | None = None
        self.mode = "stopped"
        overall = self.state.get("overall_seconds")
        if not isinstance(overall, int) or not 60 <= overall <= 1200:
            refuse("overall QEMU lifecycle deadline is outside its validated bound")
        self.deadline = self.state.get("deadline_monotonic")
        if (not isinstance(self.deadline, (int, float)) or not math.isfinite(self.deadline)
                or self.deadline <= time.monotonic()
                or self.deadline > time.monotonic() + overall):
            refuse("absolute overall QEMU lifecycle deadline is invalid")

    def save(self) -> None:
        write_json(self.record_path, {**self.ctx, "owner": proc_record(os.getpid()),
                    "qemu": self.child_identity, "mode": self.mode,
                    "generation": self.state.get("qemu_generation", 0),
                    "deadline_monotonic": self.deadline})

    def validate_owner(self) -> None:
        owner = load_json(self.record_path)
        if any(owner.get(key) != value for key, value in self.ctx.items()):
            refuse("lifecycle owner record belongs to another run")
        expected = owner.get("owner")
        actual = proc_record(os.getpid())
        if not isinstance(expected, dict) or any(actual.get(key) != expected.get(key)
                for key in ("pid", "uid", "starttime", "session", "pgrp", "exe", "argv_sha256")):
            refuse("lifecycle owner process identity changed")
        if actual["uid"] != os.getuid() or actual["session"] != actual["pid"]:
            refuse("lifecycle owner is not the private session leader")

    def launch(self, mode: str) -> dict:
        if mode not in ("initial", "restricted-restart"):
            refuse("unsupported QEMU mode")
        if self.child is not None and self.child.poll() is None:
            refuse("QEMU is already running")
        argv = ["qemu-system-x86_64", *qemu_args(self.state, mode)]
        if mode == "restricted-restart" and not any("restrict=on" in arg for arg in argv):
            refuse("restricted restart is missing restrict=on")
        if mode == "initial" and any("restrict=on" in arg for arg in argv):
            refuse("initial guest unexpectedly has restricted network mode")
        self.child = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      close_fds=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.child.poll() is not None:
                refuse("QEMU exited before identity capture")
            try:
                identity = proc_record(self.child.pid)
            except (OSError, ValueError):
                time.sleep(0.01)
                continue
            if (identity["ppid"] == os.getpid() and identity["uid"] == os.getuid()
                    and identity["comm"] == "qemu-system-x86"
                    and MARKER in identity["argv"]
                    and any(self.state["overlay"] in arg for arg in identity["argv"])
                    and any("accel=kvm" in arg and "accel=tcg" not in arg for arg in identity["argv"])):
                self.child_identity = identity
                self.mode = mode
                self.state["qemu_generation"] = int(self.state.get("qemu_generation", 0)) + 1
                self.save()
                return {"status": "started", "mode": mode,
                        "generation": self.state["qemu_generation"], "qemu": identity}
            time.sleep(0.01)
        # Popen retains the unreaped direct-child relationship, so this is the
        # only safe fallback before a full /proc identity record exists.
        if self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                if self.child.poll() is None:
                    self.child.kill()
                self.child.wait(timeout=2)
        self.child = None
        self.child_identity = None
        self.mode = "stopped"
        self.save()
        refuse("QEMU identity could not be established; the direct child was stopped and reaped")

    def stop(self) -> dict:
        if self.child is None or self.child.poll() is not None:
            if self.child is not None:
                self.child.wait(timeout=0)
            self.child = None
            self.child_identity = None
            self.mode = "stopped"
            self.save()
            return {"status": "stopped", "proved": True}
        identity = self.child_identity
        if not isinstance(identity, dict):
            refuse("QEMU has no captured identity; refusing to signal")
        actual = proc_record(self.child.pid)
        keys = ("pid", "ppid", "uid", "starttime", "comm", "exe", "argv_sha256")
        if any(actual.get(key) != identity.get(key) for key in keys) or actual["ppid"] != os.getpid():
            refuse("QEMU process identity changed; refusing to signal or remove artifacts")
        signalled = PROCESS.signal_owned_process(
            self.child.pid, identity["starttime"], identity["uid"], os.getpid(),
            "qemu-system-x86", (MARKER, self.state["overlay"], "accel=kvm"), signal.SIGTERM)
        if not signalled and self.child.poll() is None:
            refuse("pidfd TERM failed for the recorded QEMU process")
        try:
            self.child.wait(timeout=8)
        except subprocess.TimeoutExpired:
            if self.child.poll() is None:
                actual = proc_record(self.child.pid)
                if any(actual.get(key) != identity.get(key) for key in keys):
                    refuse("QEMU identity changed before bounded KILL escalation")
                PROCESS.signal_owned_process(
                    self.child.pid, identity["starttime"], identity["uid"], os.getpid(),
                    "qemu-system-x86", (MARKER, self.state["overlay"], "accel=kvm"), signal.SIGKILL)
            try:
                self.child.wait(timeout=3)
            except subprocess.TimeoutExpired as error:
                refuse(f"QEMU did not exit within the bounded cleanup period: {error}")
        self.child = None
        self.child_identity = None
        self.mode = "stopped"
        self.save()
        return {"status": "stopped", "proved": True}

    def dispatch(self, operation: str) -> dict:
        if operation not in ("stop", "shutdown") and time.monotonic() >= self.deadline:
            refuse("lifecycle owner overall deadline expired")
        self.validate_owner()
        state, overlay, _ = state_paths(self.run_dir, self.ctx)
        if state.get("overlay_dev") != overlay.stat().st_dev or state.get("overlay_ino") != overlay.stat().st_ino:
            refuse("overlay identity changed")
        if operation == "start-initial":
            return self.launch("initial")
        if operation == "restart-restricted":
            if self.mode != "stopped":
                self.stop()
            return self.launch("restricted-restart")
        if operation == "stop":
            return self.stop()
        if operation == "status":
            if self.child is not None:
                if self.child.poll() is None:
                    actual = proc_record(self.child.pid)
                    if actual != self.child_identity:
                        refuse("running QEMU identity differs from captured identity")
                else:
                    self.child.wait(timeout=0)
                    self.child = None
                    self.child_identity = None
                    self.mode = "stopped"
                    self.save()
            return {"status": self.mode, "qemu": self.child_identity,
                    "generation": state.get("qemu_generation", 0)}
        if operation == "shutdown":
            result = self.stop()
            return {**result, "shutdown": True}
        refuse("unknown lifecycle operation")

    def serve(self) -> None:
        socket_path = self.run_dir / "owner.sock"
        if len(os.fsencode(socket_path)) >= 104:
            refuse("private owner socket path exceeds the Unix socket bound")
        if socket_path.exists() or socket_path.is_symlink():
            refuse("owner socket path is already occupied")
        self.save()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        server.listen(4)
        server.settimeout(0.2)
        stopping = False

        def on_signal(_signum, _frame):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        try:
            while not stopping and time.monotonic() < self.deadline:
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                with connection:
                    connection.settimeout(2)
                    try:
                        payload = bytearray()
                        while len(payload) <= MAX_MESSAGE:
                            chunk = connection.recv(MAX_MESSAGE + 1 - len(payload))
                            if not chunk or b"\n" in chunk:
                                payload.extend(chunk.split(b"\n", 1)[0])
                                break
                            payload.extend(chunk)
                        if len(payload) > MAX_MESSAGE:
                            refuse("owner request exceeds the message bound")
                        message = json.loads(payload.decode("utf-8"))
                        if not isinstance(message, dict) or any(message.get(k) != v for k, v in self.ctx.items()):
                            refuse("owner request identity is invalid")
                        result = self.dispatch(message.get("operation", ""))
                        response = {"ok": True, **result}
                        if message.get("operation") == "shutdown":
                            stopping = True
                    except (Refused, OSError, ValueError, TypeError, KeyError,
                            subprocess.SubprocessError) as error:
                        response = {"ok": False, "error": str(error)[:300]}
                    connection.sendall(json.dumps(response, separators=(",", ":")).encode() + b"\n")
        finally:
            try:
                if self.child is not None and self.child.poll() is None:
                    self.stop()
            finally:
                server.close()
                try:
                    socket_path.unlink()
                except FileNotFoundError:
                    pass


def call_owner(run_dir: pathlib.Path, ctx: dict, operation: str) -> dict:
    sock_path = run_dir / "owner.sock"
    info = os.lstat(sock_path)
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        refuse("owner socket type, owner, or mode is invalid")
    request = {**ctx, "operation": operation}
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(12)
    try:
        client.connect(str(sock_path))
        client.sendall(json.dumps(request, separators=(",", ":")).encode() + b"\n")
        response = bytearray()
        while len(response) <= MAX_MESSAGE:
            chunk = client.recv(MAX_MESSAGE + 1 - len(response))
            if not chunk or b"\n" in chunk:
                response.extend(chunk.split(b"\n", 1)[0])
                break
            response.extend(chunk)
        value = json.loads(response.decode("utf-8"))
    finally:
        client.close()
    if not isinstance(value, dict) or value.get("ok") is not True:
        refuse(f"lifecycle owner refused {operation}: {value.get('error', 'invalid response')}")
    return value


def verify_stopped(run_dir: pathlib.Path, ctx: dict) -> dict:
    record_path = run_dir / "owner.json"
    if not record_path.exists() and not record_path.is_symlink():
        if (run_dir / "owner.sock").exists() or (run_dir / "owner.sock").is_symlink():
            refuse("owner socket exists without its identity record")
        return {"status": "stopped", "owner_started": False}
    record = load_json(record_path)
    if any(record.get(key) != value for key, value in ctx.items()):
        refuse("owner record provenance does not match this run")
    if record.get("qemu") is not None or record.get("mode") != "stopped":
        refuse("owner did not publish a stopped QEMU state")
    identity = record.get("owner")
    if not isinstance(identity, dict):
        refuse("owner process identity is missing")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            current = proc_record(identity["pid"])
            stat_fields = pathlib.Path(f"/proc/{identity['pid']}/stat").read_text(encoding="ascii")
            process_state = stat_fields[stat_fields.rfind(") ") + 2 :].split()[0]
        except (OSError, ValueError, KeyError):
            current = None
            process_state = ""
        if (current is None or current.get("starttime") != identity.get("starttime")
                or process_state == "Z"):
            break
        time.sleep(0.05)
    else:
        refuse("lifecycle owner is still running")
    try:
        info = os.lstat(run_dir / "owner.sock")
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISSOCK(info.st_mode):
            refuse("lifecycle owner socket remains after shutdown")
        refuse("owner socket path was replaced by an unexpected object")
    return {"status": "stopped", "owner_gone": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("start-owner", "serve", "start-initial", "restart-restricted", "stop", "status", "shutdown", "verify-stopped"))
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--workflow-sha", required=True)
    args = parser.parse_args(argv)
    run_dir = pathlib.Path(args.run_dir)
    ctx = context(args)
    try:
        if args.operation == "start-owner":
            info = os.lstat(run_dir)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                refuse("run directory must be owner-owned mode 0700")
            log_fd = os.open(run_dir / "owner.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            owner_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                         "HOME": os.environ.get("HOME", str(pathlib.Path.home()))}
            if "RUNNER_TRACKING_ID" in os.environ:
                owner_env["RUNNER_TRACKING_ID"] = os.environ["RUNNER_TRACKING_ID"]
            try:
                process = subprocess.Popen(
                    [sys.executable, "-I", str(pathlib.Path(__file__).resolve()), "serve",
                     "--run-dir", str(run_dir), "--run-id", args.run_id,
                     "--attempt", args.attempt, "--source-sha", args.source_sha,
                     "--workflow-sha", args.workflow_sha],
                    stdin=subprocess.DEVNULL, stdout=log_fd, stderr=subprocess.STDOUT,
                    close_fds=True, start_new_session=True, env=owner_env)
            finally:
                os.close(log_fd)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        refuse("lifecycle owner exited during startup")
                    try:
                        result = call_owner(run_dir, ctx, "status")
                        print(json.dumps(result, sort_keys=True))
                        return 0
                    except (OSError, Refused):
                        time.sleep(0.02)
                refuse("lifecycle owner did not become ready")
            except Refused:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=2)
                raise
        if args.operation == "serve":
            Owner(run_dir, ctx).serve()
            return 0
        if args.operation == "verify-stopped":
            print(json.dumps(verify_stopped(run_dir, ctx), sort_keys=True))
            return 0
        result = call_owner(run_dir, ctx, args.operation)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, Refused, subprocess.SubprocessError) as error:
        print(f"KVM_OWNER_REFUSED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
