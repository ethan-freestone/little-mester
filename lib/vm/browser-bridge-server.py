#!/usr/bin/env python3
"""Single-capability Streamable HTTP MCP bridge for one guest auth session."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

DEFAULT_PORT = 0
DEFAULT_LEASE_SECONDS = 120
MAX_LEASE_SECONDS = 600
MAX_BODY_BYTES = 16 * 1024
MAX_URL_LENGTH = 8192
OPEN_TIMEOUT_SECONDS = 15
PROTOCOL_VERSIONS = {"2025-03-26", "2025-06-18", "2025-11-25"}
APPROVED_HOSTS = {"auth.openai.com", "chatgpt.com"}
CALLBACK_SECRET_PARAMETERS = {"code", "access_token", "refresh_token", "id_token", "code_verifier"}
_callback_processes: dict[int, subprocess.Popen] = {}
_callback_processes_lock = threading.Lock()


@dataclass
class Session:
    protocol_version: str
    initialized: bool = False


def is_authorization_url(raw_url: str) -> bool:
    if not isinstance(raw_url, str) or not raw_url or len(raw_url) > MAX_URL_LENGTH:
        return False
    try:
        parsed = urlsplit(raw_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in APPROVED_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.fragment
        ):
            return False
        if any(part.lower() == "callback" for part in parsed.path.split("/")):
            return False
        keys = {key.lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
        return not keys.intersection(CALLBACK_SECRET_PARAMETERS)
    except (ValueError, UnicodeError):
        return False


def openai_callback_port(raw_url: str) -> int:
    """Return only the exact, validated guest-loopback callback port."""
    if not is_authorization_url(raw_url):
        raise ValueError("authorization URL rejected")
    parsed = urlsplit(raw_url)
    if parsed.hostname != "auth.openai.com" or parsed.path != "/api/accounts/authorize":
        raise ValueError("authorization URL rejected")
    redirects = [value for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key == "redirect_uri"]
    if len(redirects) != 1:
        raise ValueError("authorization URL rejected")
    try:
        redirect = urlsplit(redirects[0])
        port = redirect.port
    except ValueError as exc:
        raise ValueError("authorization URL rejected") from exc
    if (
        redirect.scheme != "http"
        or redirect.hostname != "127.0.0.1"
        or redirect.username is not None
        or redirect.password is not None
        or redirect.path != "/auth/callback"
        or redirect.query
        or redirect.fragment
        or port is None
        or not 1024 <= port <= 65535
        or redirects[0] != f"http://127.0.0.1:{port}/auth/callback"
    ):
        raise ValueError("authorization URL rejected")
    return port


def _private_path(variable: str) -> str:
    value = os.environ.get(variable, "")
    if (not value.startswith("/") or "," in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise RuntimeError("temporary SSH connection is unavailable")
    path = Path(value)
    try:
        info = path.lstat()
    except OSError as exc:
        raise RuntimeError("temporary SSH connection is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        raise RuntimeError("temporary SSH connection is unavailable")
    return value


def callback_forward_command(port: int) -> list[str]:
    """Build one isolated SSH process for the exact loopback callback lease."""
    raw_port = os.environ.get("VM_BROWSER_BRIDGE_SSH_PORT", "")
    if not raw_port.isascii() or not raw_port.isdecimal() or not 1 <= int(raw_port) <= 65535:
        raise RuntimeError("temporary SSH connection is unavailable")
    if type(port) is not int or not 1024 <= port <= 65535:
        raise RuntimeError("invalid callback port")
    key = _private_path("VM_BROWSER_BRIDGE_SSH_KEY")
    known_hosts = _private_path("VM_BROWSER_BRIDGE_KNOWN_HOSTS")
    command = [
        "ssh",
        "-F", "/dev/null",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ConnectionAttempts=1",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=1",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "HostKeyAlias=127.0.0.1",
        "-o", "IdentitiesOnly=yes",
        "-o", "IdentityAgent=none",
        "-o", f"IdentityFile={key}",
        "-o", "ForwardAgent=no",
        "-o", "ForwardX11=no",
        "-o", "ClearAllForwardings=yes",
        "-o", "PermitLocalCommand=no",
        "-L", f"127.0.0.1:{port}:127.0.0.1:{port}",
        "-p", raw_port,
        "mester@127.0.0.1",
        "-N",
    ]
    if os.environ.get("VM_BROWSER_BRIDGE_SSH_DEBUG") == "1":
        command.insert(1, "-v")
    return command


def _process_listens_on_loopback(pid: int, port: int) -> bool:
    """Verify the SSH child, not an unrelated process, owns the requested listener."""
    try:
        inodes = set()
        for entry in Path(f"/proc/{pid}/fd").iterdir():
            try:
                link = os.readlink(entry)
            except OSError:
                continue
            if link.startswith("socket:[") and link.endswith("]"):
                inodes.add(link[8:-1])
        expected = f"{socket.htonl(0x7F000001):08X}:{port:04X}"
        for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
            try:
                lines = table.read_text(encoding="ascii").splitlines()[1:]
            except OSError:
                continue
            for line in lines:
                fields = line.split()
                if len(fields) > 9 and fields[1].upper() == expected and fields[3] == "0A" and fields[9] in inodes:
                    return True
    except OSError:
        return False
    return False


def _callback_listener_is_ready(port: int) -> bool:
    """Probe only the exact forwarded callback port with a harmless GET /."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
            connection.settimeout(2)
            connection.sendall(b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            response = connection.recv(128)
        status = response.split(b"\r\n", 1)[0]
        return status in (b"HTTP/1.0 404 Not Found", b"HTTP/1.1 404 Not Found")
    except OSError:
        return False


def _stop_callback_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    except OSError:
        pass


def forward_openai_callback(port: int) -> None:
    with _callback_processes_lock:
        existing = _callback_processes.get(port)
        if existing is not None:
            if (existing.poll() is None and _process_listens_on_loopback(existing.pid, port)
                    and _callback_listener_is_ready(port)):
                return
            _callback_processes.pop(port, None)
            _stop_callback_process(existing)
    command = callback_forward_command(port)
    log_path = _private_path("VM_BROWSER_BRIDGE_SSH_LOG")
    try:
        with open(log_path, "ab") as log:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=log, close_fds=True, start_new_session=True,
            )
    except Exception as exc:
        raise RuntimeError("callback forwarding failed") from exc
    with _callback_processes_lock:
        old = _callback_processes.get(port)
        if old is not None:
            _stop_callback_process(old)
        _callback_processes[port] = process
    deadline = time.monotonic() + 10
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("callback forwarding failed")
            if (_process_listens_on_loopback(process.pid, port)
                    and _callback_listener_is_ready(port)):
                return
            time.sleep(0.05)
        raise RuntimeError("callback forwarding failed")
    except Exception as exc:
        with _callback_processes_lock:
            if _callback_processes.get(port) is process:
                _callback_processes.pop(port, None)
        _stop_callback_process(process)
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError("callback forwarding failed") from exc


def cancel_openai_callback(port: int) -> None:
    with _callback_processes_lock:
        process = _callback_processes.pop(port, None)
    if process is not None:
        _stop_callback_process(process)


def open_with_host_browser(url: str) -> None:
    if not is_authorization_url(url):
        raise RuntimeError("browser request rejected")
    try:
        result = subprocess.run(
            ["xdg-open", url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=OPEN_TIMEOUT_SECONDS, check=False,
        )
    except Exception as exc:
        raise RuntimeError("browser launch failed") from exc
    if result.returncode != 0:
        raise RuntimeError("browser launch failed")


class BrowserBridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self, address, token: str, opener=open_with_host_browser,
        callback_forwarder=forward_openai_callback, callback_canceller=cancel_openai_callback,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ):
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= MAX_LEASE_SECONDS:
            raise ValueError("callback lease must be between 1 and 600 seconds")
        if not isinstance(token, str) or len(token) < 32 or not token.isascii():
            raise ValueError("bridge configuration is invalid")
        self.bridge_token = token.encode("ascii")
        self.open_url = opener
        self.forward_callback = callback_forwarder
        self.cancel_callback = callback_canceller
        self.lease_seconds = lease_seconds
        self.callback_port: int | None = None
        self.callback_lock = threading.RLock()
        self.callback_timer: threading.Timer | None = None
        self.callback_generation = 0
        self.sessions: dict[str, Session] = {}
        self.sessions_lock = threading.Lock()
        super().__init__(address, BrowserBridgeRequestHandler)

    def _cancel_timer_locked(self):
        if self.callback_timer is not None:
            self.callback_timer.cancel()
            self.callback_timer = None

    def _arm_lease_locked(self, port: int):
        self._cancel_timer_locked()
        self.callback_generation += 1
        generation = self.callback_generation
        timer = threading.Timer(self.lease_seconds, self._expire_callback, args=(port, generation))
        timer.daemon = True
        self.callback_timer = timer
        timer.start()

    def _expire_callback(self, port: int, generation: int):
        with self.callback_lock:
            if generation != self.callback_generation or self.callback_port != port:
                return
            try:
                self.cancel_callback(port)
            except Exception:  # noqa: BLE001 - keep timer cleanup diagnostics out of user output.
                # The agent-session control transport is also closed on session exit.
                return
            self.callback_port = None
            self.callback_timer = None

    def open_authorization_url(self, url: str) -> None:
        port = openai_callback_port(url)
        with self.callback_lock:
            if port != self.callback_port and self.callback_port is not None:
                old_port = self.callback_port
                self.cancel_callback(old_port)
                self._cancel_timer_locked()
                self.callback_port = None
            self.forward_callback(port)
            if port != self.callback_port:
                self.callback_port = port
            self._arm_lease_locked(port)
            try:
                self.open_url(url)
            except Exception:
                try:
                    self.cancel_callback(port)
                finally:
                    self.callback_port = None
                    self._cancel_timer_locked()
                raise

    def server_close(self):
        with self.callback_lock:
            self._cancel_timer_locked()
            if self.callback_port is not None:
                port = self.callback_port
                try:
                    self.cancel_callback(port)
                except Exception:  # noqa: BLE001, S110 - teardown must remain best-effort and quiet.
                    pass
                finally:
                    self.callback_port = None
        super().server_close()


class BrowserBridgeRequestHandler(BaseHTTPRequestHandler):
    server: BrowserBridgeHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "MesterBrowserBridge/1"
    sys_version = ""

    def log_message(self, _format, *_args):
        return None

    def log_error(self, _format, *_args):
        return None

    def _send(self, status: int, body: bytes = b"", *, headers=None):
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status: int, value, *, headers=None):
        self._send(status, json.dumps(value, separators=(",", ":")).encode("utf-8"), headers=headers)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        try:
            supplied = header[7:].encode("ascii")
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(supplied, self.server.bridge_token)

    def _read_json(self):
        raw_length = self.headers.get("Content-Length", "")
        if not raw_length.isascii() or not raw_length.isdecimal():
            raise ValueError
        length = int(raw_length)
        if not 1 <= length <= MAX_BODY_BYTES:
            raise ValueError
        if "application/json" not in self.headers.get("Content-Type", "").lower():
            raise ValueError
        accepts = {item.split(";", 1)[0].strip() for item in self.headers.get("Accept", "").split(",")}
        if not accepts.intersection({"application/json", "text/event-stream"}):
            raise ValueError
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError
        message = json.loads(raw.decode("utf-8"))
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            raise ValueError
        return message

    def _rpc_error(self, request_id, code: int, message: str):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"ok": True})
            return
        self._send(405, headers={"Allow": "POST, DELETE"})

    def do_POST(self):
        if self.path != "/mcp":
            self._send_json(404, {"error": "Not found"})
            return
        if not self._authorized():
            self._send_json(401, {"error": "Unauthorized"}, headers={"WWW-Authenticate": "Bearer"})
            return
        try:
            message = self._read_json()
        except Exception:  # noqa: BLE001 - parser details may contain attacker-controlled content.
            self._send_json(400, {"error": "Invalid MCP request"})
            return
        method, request_id = message["method"], message.get("id")
        session_header = self.headers.get("Mcp-Session-Id")
        if method == "initialize":
            params = message.get("params")
            if session_header or request_id is None or not isinstance(params, dict):
                self._send_json(400, self._rpc_error(request_id, -32600, "Invalid initialization request"))
                return
            requested = params.get("protocolVersion")
            version = requested if requested in PROTOCOL_VERSIONS else "2025-03-26"
            with self.server.sessions_lock:
                if len(self.server.sessions) >= 8:
                    self._send_json(429, {"error": "MCP session limit reached"})
                    return
                session_id = secrets.token_urlsafe(24)
                self.server.sessions[session_id] = Session(protocol_version=version)
            self._send_json(200, {
                "jsonrpc": "2.0", "id": request_id,
                "result": {"protocolVersion": version, "capabilities": {"tools": {}},
                            "serverInfo": {"name": "host-browser-bridge", "version": "0.1.0"}},
            }, headers={"Mcp-Session-Id": session_id})
            return
        if not session_header:
            self._send_json(400, {"error": "MCP initialization required"})
            return
        with self.server.sessions_lock:
            session = self.server.sessions.get(session_header)
        if session is None:
            self._send_json(404, {"error": "MCP session not found"})
            return
        if self.headers.get("MCP-Protocol-Version") != session.protocol_version:
            self._send_json(400, {"error": "MCP protocol version mismatch"})
            return
        if method == "notifications/initialized" and request_id is None:
            session.initialized = True
            self._send(202)
            return
        if not session.initialized:
            self._send_json(400, self._rpc_error(request_id, -32600, "MCP initialization is incomplete"))
            return
        if request_id is None:
            self._send(202)
            return
        if method == "tools/list":
            self._send_json(200, {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{
                "name": "open_auth_url",
                "title": "Open approved authorization URL in host browser",
                "description": "Opens only the supported OpenAI authorization URL after forwarding its exact guest-loopback callback port.",
                "inputSchema": {"type": "object", "properties": {
                    "url": {"type": "string", "minLength": 1, "maxLength": MAX_URL_LENGTH},
                }, "required": ["url"], "additionalProperties": False},
            }]}})
            return
        if method == "tools/call":
            params = message.get("params")
            arguments = params.get("arguments") if isinstance(params, dict) else None
            raw_url = arguments.get("url") if isinstance(arguments, dict) else None
            valid = (
                isinstance(params, dict) and params.get("name") == "open_auth_url"
                and isinstance(arguments, dict) and set(arguments) == {"url"}
                and isinstance(raw_url, str) and is_authorization_url(raw_url)
            )
            if valid:
                try:
                    self.server.open_authorization_url(raw_url)
                except Exception:  # noqa: BLE001 - suppress opener/forwarder details at the MCP boundary.
                    valid = False
            result = {"content": [{"type": "text", "text":
                "Opened the approved authorization page in the host browser." if valid
                else "Host browser request was rejected or could not be opened."}]}
            if not valid:
                result["isError"] = True
            self._send_json(200, {"jsonrpc": "2.0", "id": request_id, "result": result})
            return
        self._send_json(200, self._rpc_error(request_id, -32601, "Method not found"))

    def do_DELETE(self):
        if self.path != "/mcp" or not self._authorized():
            self._send_json(401, {"error": "Unauthorized"})
            return
        session_id = self.headers.get("Mcp-Session-Id", "")
        version = self.headers.get("MCP-Protocol-Version", "")
        with self.server.sessions_lock:
            session = self.server.sessions.get(session_id)
            if session and session.protocol_version == version:
                self.server.sessions.pop(session_id, None)
                self._send(200)
                return
        self._send_json(404, {"error": "MCP session not found"})


def _read_token_file() -> str:
    raw = os.environ.get("VM_BROWSER_BRIDGE_TOKEN_FILE", "")
    if not raw.startswith("/") or "\n" in raw or "\r" in raw:
        raise ValueError
    fd = os.open(raw, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > 256):
            raise ValueError
        token = os.read(fd, 257).decode("ascii").strip()
        if len(token) < 32 or not token.isascii() or any(ch.isspace() for ch in token):
            raise ValueError
        return token
    finally:
        os.close(fd)


def main() -> int:
    try:
        token = _read_token_file()
        raw_port = os.environ.get("VM_BROWSER_BRIDGE_PORT", str(DEFAULT_PORT))
        raw_lease = os.environ.get("VM_AUTH_CALLBACK_LEASE_SECONDS", str(DEFAULT_LEASE_SECONDS))
        if not raw_port.isascii() or not raw_port.isdecimal() or int(raw_port) > 65535:
            raise ValueError
        if not raw_lease.isascii() or not raw_lease.isdecimal() or not 1 <= int(raw_lease) <= MAX_LEASE_SECONDS:
            raise ValueError
        server = BrowserBridgeHTTPServer(
            ("127.0.0.1", int(raw_port)), token, lease_seconds=int(raw_lease),
        )
    except Exception:  # noqa: BLE001 - report only a generic configuration failure.
        print("Host browser MCP bridge configuration is invalid.", file=sys.stderr)
        return 2

    def stop_server(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    print(f"Host browser MCP bridge listening on 127.0.0.1:{server.server_port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
