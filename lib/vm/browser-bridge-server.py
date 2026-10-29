#!/usr/bin/env python3
"""Single-capability Streamable HTTP MCP bridge for host browser opening."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit


DEFAULT_PORT = 19533
MAX_BODY_BYTES = 16 * 1024
MAX_URL_LENGTH = 8192
OPEN_TIMEOUT_SECONDS = 15
PROTOCOL_VERSIONS = {"2025-03-26", "2025-06-18", "2025-11-25"}
APPROVED_HOSTS = {"auth.openai.com", "chatgpt.com"}
CALLBACK_SECRET_PARAMETERS = {
    "code",
    "access_token",
    "refresh_token",
    "id_token",
    "code_verifier",
}


@dataclass
class Session:
    protocol_version: str
    initialized: bool = False


def is_authorization_url(raw_url: str) -> bool:
    if not raw_url or len(raw_url) > MAX_URL_LENGTH:
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
        query_keys = {key.lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
        return not query_keys.intersection(CALLBACK_SECRET_PARAMETERS)
    except ValueError:
        return False


def open_with_host_browser(url: str) -> None:
    try:
        result = subprocess.run(
            ["xdg-open", url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=OPEN_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception as exc:
        raise RuntimeError("browser launch failed") from exc
    if result.returncode != 0:
        raise RuntimeError("browser launch failed")


class BrowserBridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, token: str, opener=open_with_host_browser):
        super().__init__(address, BrowserBridgeRequestHandler)
        self.bridge_token = token.encode("ascii")
        self.open_url = opener
        self.sessions: dict[str, Session] = {}
        self.sessions_lock = threading.Lock()


class BrowserBridgeRequestHandler(BaseHTTPRequestHandler):
    server: BrowserBridgeHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "MesterBrowserBridge/1"
    sys_version = ""

    def log_message(self, _format, *_args):
        # HTTP paths, bodies, headers, and tool arguments are never logged.
        return None

    def log_error(self, _format, *_args):
        # Malformed requests must not cause request text to be written to logs.
        return None

    def _send(self, status: int, body: bytes = b"", *, content_type="application/json", headers=None):
        self.send_response(status)
        if body:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status: int, value, *, headers=None):
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self._send(status, body, headers=headers)

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
        if not raw_length.isdigit():
            raise ValueError("invalid content length")
        length = int(raw_length)
        if length < 1 or length > MAX_BODY_BYTES:
            raise ValueError("invalid request size")
        if "application/json" not in self.headers.get("Content-Type", "").lower():
            raise ValueError("unsupported content type")
        if not any(
            item.split(";", 1)[0].strip() in {"application/json", "text/event-stream"}
            for item in self.headers.get("Accept", "").split(",")
        ):
            raise ValueError("unsupported accept type")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _jsonrpc_error(self, request_id, code: int, message: str):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"ok": True})
            return
        self.send_response(405)
        self.send_header("Allow", "POST, DELETE")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if self.path != "/mcp":
            self._send_json(404, {"error": "Not found"})
            return
        if not self._authorized():
            self._send_json(401, {"error": "Unauthorized"}, headers={"WWW-Authenticate": "Bearer"})
            return
        try:
            message = self._read_json()
        except Exception:
            self._send_json(400, {"error": "Invalid MCP request"})
            return
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            self._send_json(400, {"error": "Invalid MCP request"})
            return

        method = message["method"]
        request_id = message.get("id")
        session_header = self.headers.get("Mcp-Session-Id")
        if method == "initialize":
            if session_header or request_id is None:
                self._send_json(400, self._jsonrpc_error(request_id, -32600, "Invalid initialization request"))
                return
            if not isinstance(message.get("params"), dict):
                self._send_json(400, self._jsonrpc_error(request_id, -32602, "Invalid initialization parameters"))
                return
            requested = message["params"].get("protocolVersion")
            version = requested if requested in PROTOCOL_VERSIONS else "2025-03-26"
            with self.server.sessions_lock:
                if len(self.server.sessions) >= 8:
                    self._send_json(429, {"error": "MCP session limit reached"})
                    return
                session_id = secrets.token_urlsafe(24)
                self.server.sessions[session_id] = Session(protocol_version=version)
            self._send_json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": version,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "host-browser-bridge", "version": "0.1.0"},
                    },
                },
                headers={"Mcp-Session-Id": session_id},
            )
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
            self._send_json(400, self._jsonrpc_error(request_id, -32600, "MCP initialization is incomplete"))
            return
        if request_id is None:
            # This server has no server-initiated notifications.
            self._send(202)
            return
        if method == "tools/list":
            self._send_json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "tools": [{
                            "name": "open_auth_url",
                            "title": "Open approved authorization URL in host browser",
                            "description": "Open an HTTPS authorization URL on auth.openai.com or chatgpt.com in the host default browser. Does not handle callbacks or provide other host access.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"url": {"type": "string", "minLength": 1, "maxLength": MAX_URL_LENGTH}},
                                "required": ["url"],
                                "additionalProperties": False,
                            },
                        }],
                    },
                },
            )
            return
        if method == "tools/call":
            params = message.get("params")
            arguments = params.get("arguments") if isinstance(params, dict) else None
            raw_url = arguments.get("url") if isinstance(arguments, dict) else None
            error = not isinstance(raw_url, str) or not is_authorization_url(raw_url)
            if not error:
                try:
                    self.server.open_url(raw_url)
                except Exception:
                    error = True
            result = {
                "content": [{
                    "type": "text",
                    "text": "Host browser request was rejected or could not be opened." if error
                    else "Opened the approved authorization page in the host browser.",
                }],
            }
            if error:
                result["isError"] = True
            self._send_json(200, {"jsonrpc": "2.0", "id": request_id, "result": result})
            return
        self._send_json(200, self._jsonrpc_error(request_id, -32601, "Method not found"))

    def do_DELETE(self):
        if self.path != "/mcp" or not self._authorized():
            self._send_json(401, {"error": "Unauthorized"})
            return
        session_id = self.headers.get("Mcp-Session-Id", "")
        protocol_version = self.headers.get("MCP-Protocol-Version", "")
        with self.server.sessions_lock:
            session = self.server.sessions.get(session_id)
            if session and session.protocol_version == protocol_version:
                self.server.sessions.pop(session_id, None)
                self._send(200)
            else:
                self._send_json(404, {"error": "MCP session not found"})


def main() -> int:
    token = os.environ.get("VM_BROWSER_BRIDGE_TOKEN", "")
    raw_port = os.environ.get("VM_BROWSER_BRIDGE_PORT", str(DEFAULT_PORT))
    try:
        port = int(raw_port)
    except ValueError:
        port = 0
    if len(token) < 32 or not 1 <= port <= 65535:
        print("Host browser MCP bridge configuration is invalid.", file=sys.stderr)
        return 2
    try:
        server = BrowserBridgeHTTPServer(("127.0.0.1", port), token)
    except Exception:
        print("Host browser MCP bridge failed to bind its loopback listener.", file=sys.stderr)
        return 1

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
