#!/usr/bin/env python3
"""Credential-free OpenCode OAuth bridge test against the disposable QEMU guest.

The real OpenCode CLI generates the authorization URL, the host bridge creates
the pinned SSH forward, and these local fixtures act only as the browser/IdP
callback. The restricted second-boot QEMU network prevents a token exchange
from reaching any upstream provider.
"""

from __future__ import annotations

import argparse
import errno
import importlib.util
import os
import pathlib
import pty
import select
import shutil
import signal
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "lib/vm/browser-auth-session.py"
SPEC = importlib.util.spec_from_file_location("browser_auth_contract", BRIDGE)
AUTH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTH)


def parse_live_url(data: bytes) -> str | None:
    prefix = AUTH.AUTHORIZATION_URL_PREFIX
    start = data.find(prefix)
    if start < 0:
        return None
    end = start + len(prefix)
    while end < len(data) and data[end] in AUTH.URL_CHARACTERS:
        end += 1
    if end == len(data):
        return None
    url = data[start:end].decode("ascii")
    AUTH.validate_authorization_url(url)
    return url


def request_callback(port: int, query: dict[str, str]) -> tuple[int, bytes]:
    url = f"http://127.0.0.1:{port}/auth/callback?{urllib.parse.urlencode(query)}"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read(4096)
    except urllib.error.HTTPError as error:
        return error.code, error.read(4096)


class Session:
    def __init__(self, port: int, key: pathlib.Path, known_hosts: pathlib.Path, case: str):
        self.case = case
        self.temp = tempfile.TemporaryDirectory(prefix="lm-qemu-auth-")
        self.root = pathlib.Path(self.temp.name)
        os.chmod(self.root, 0o700)
        self.run_dir = self.root / f"{case}-{os.getpid()}"
        self.run_dir.mkdir(mode=0o700)
        self.control_dir = self.root / f"lmv3-{self.run_dir.name}"
        self.control_dir.mkdir(mode=0o700)
        self.control_socket = self.control_dir / "auth-control.sock"
        socket_path_bytes = len(os.fsencode(self.control_socket))
        if socket_path_bytes >= AUTH.CONTROL_SOCKET_LIMIT:
            raise RuntimeError(
                "hosted CI private SSH control socket path exceeds the production limit "
                f"({socket_path_bytes} bytes)"
            )
        self.proof = self.run_dir / "proof.json"
        identity = self.run_dir / "identity_file"
        pinned_hosts = self.run_dir / "known_hosts"
        shutil.copyfile(key, identity)
        shutil.copyfile(known_hosts, pinned_hosts)
        os.chmod(identity, 0o600)
        os.chmod(pinned_hosts, 0o600)
        self.command = [
            "/usr/bin/python3", "-I", str(BRIDGE), "--ssh", "/usr/bin/ssh",
            "--ssh-port", str(port), "--identity-file", str(identity),
            "--known-hosts", str(pinned_hosts),
            "--control-socket", str(self.control_socket),
            "--ssh-identity", str(self.run_dir / "ssh.identity"),
            "--callback-port-file", str(self.run_dir / "callback.port"),
            "--proof-file", str(self.proof), "--run-dir", str(self.run_dir),
            "--guest-auth-dir", "/tmp/mester-v3-auth-ci", "--timeout-seconds", "90",
            "--output-limit", "65536", "--persistent-parent",
        ]
        self.pid = -1
        self.master_fd = -1
        self.output = bytearray()
        self.exit_status: int | None = None
        self.forced_kill = False

    def start(self) -> str:
        self.pid, self.master_fd = pty.fork()
        if self.pid == 0:
            for name in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "DISPLAY", "WAYLAND_DISPLAY"):
                os.environ.pop(name, None)
            os.execv(self.command[0], self.command)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.master_fd], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(self.master_fd, 8192)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                self.output.extend(chunk)
                if len(self.output) > 65536:
                    raise RuntimeError("auth bridge exceeded the bounded CI capture")
                url = parse_live_url(bytes(self.output))
                if url is not None:
                    return url
            waited, status = os.waitpid(self.pid, os.WNOHANG)
            if waited:
                self.exit_status = os.waitstatus_to_exitcode(status)
                self.pid = -1
                raise RuntimeError(f"OpenCode auth exited before producing its URL (status={self.exit_status})")
        raise RuntimeError("OpenCode auth URL did not reach the local simulator before the deadline")

    def wait(self, seconds: float = 30) -> None:
        deadline = time.monotonic() + seconds
        while self.pid > 0 and time.monotonic() < deadline:
            ready, _, _ = select.select([self.master_fd], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(self.master_fd, 8192)
                except OSError as error:
                    if error.errno == errno.EIO:
                        chunk = b""
                    else:
                        raise
                self.output.extend(chunk)
                if len(self.output) > 65536:
                    raise RuntimeError("auth bridge exceeded the bounded CI capture")
            waited, status = os.waitpid(self.pid, os.WNOHANG)
            if waited:
                self.exit_status = os.waitstatus_to_exitcode(status)
                self.pid = -1
                break

    def interrupt(self) -> None:
        if self.pid > 0:
            os.kill(self.pid, signal.SIGINT)
            self.wait(10)
            if self.pid > 0:
                self.forced_kill = True
                os.kill(self.pid, signal.SIGKILL)
                _, status = os.waitpid(self.pid, 0)
                self.exit_status = os.waitstatus_to_exitcode(status)
                self.pid = -1

    def close(self) -> None:
        self.interrupt()
        if self.master_fd >= 0:
            os.close(self.master_fd)
            self.master_fd = -1
        if not self.proof.is_file():
            raise RuntimeError(f"{self.case}: auth bridge did not publish cleanup proof")
        proof = __import__("json").loads(self.proof.read_text(encoding="utf-8"))
        if (not proof.get("ssh_stopped") or not proof.get("control_socket_absent")
                or not proof.get("listener_verified_closed") or not proof.get("cleanup_complete")):
            raise RuntimeError(f"{self.case}: complete SSH/listener/control-socket cleanup was not proved")
        if self.exit_status is None or self.forced_kill:
            raise RuntimeError(f"{self.case}: auth process did not exit cleanly after its bounded interruption")
        self.temp.cleanup()


def run_case(args, case: str) -> None:
    session = Session(args.port, pathlib.Path(args.identity), pathlib.Path(args.known_hosts), case)
    try:
        url = session.start()
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        callback = urllib.parse.urlsplit(query["redirect_uri"][0])
        state = query["state"][0]
        if case == "success":
            status, _ = request_callback(callback.port, {"code": "synthetic-ci-code", "state": state})
            if status != 200:
                raise RuntimeError(f"synthetic valid callback returned unexpected HTTP status {status}")
        elif case == "mismatched-state":
            status, _ = request_callback(callback.port, {"code": "synthetic-ci-code", "state": "X" * 43})
            if status != 400:
                raise RuntimeError(f"callback listener did not reject mismatched OAuth state (HTTP {status})")
        elif case == "denial":
            status, _ = request_callback(callback.port, {"error": "access_denied", "state": state})
            if status != 400:
                raise RuntimeError(f"pinned OpenCode callback did not reject provider denial (HTTP {status})")
        elif case == "expiration":
            status, _ = request_callback(callback.port, {"error": "expired", "state": state})
            if status != 400:
                raise RuntimeError(f"pinned OpenCode callback did not reject expiration response (HTTP {status})")
        elif case == "malformed":
            status, _ = request_callback(callback.port, {"code": "", "state": state})
            if status != 400:
                raise RuntimeError(f"callback listener did not reject malformed authorization response (HTTP {status})")
        elif case == "interruption":
            session.interrupt()
        if case not in ("mismatched-state", "interruption"):
            session.wait(20)
            if session.pid > 0:
                raise RuntimeError(f"{case}: OpenCode did not finish the synthetic callback path before deadline")
            if session.exit_status != 78:
                raise RuntimeError(
                    f"{case}: expected sanitized auth/token-exchange failure status 78, got {session.exit_status}"
                )
        elif case == "mismatched-state":
            session.interrupt()
        if case in ("mismatched-state", "interruption") and session.exit_status != 130:
            raise RuntimeError(f"{case}: expected bounded SIGINT exit 130, got {session.exit_status}")
    except BaseException as original:
        try:
            session.close()
        except Exception as cleanup_error:
            raise RuntimeError(
                f"{case}: test failed ({original}); cleanup proof also failed ({cleanup_error})"
            ) from original
        raise
    else:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--known-hosts", required=True)
    args = parser.parse_args()
    # The pinned 2.2.5 callback handler does not guarantee duplicate-callback
    # rejection; replay protection is exercised by the deterministic bridge
    # simulator in vm-browser-auth.test.py instead.
    for case in ("success", "mismatched-state", "denial", "expiration", "malformed", "interruption"):
        run_case(args, case)
        print(f"QEMU_OAUTH_SIMULATOR_PASS: {case} (synthetic; no token/provider/model access)")
    print("QEMU_OAUTH_UPSTREAM_BOUNDARY: token exchange, real identity provider and model response NOT_RUN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
