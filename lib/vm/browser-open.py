#!/usr/bin/env python3
"""Call the session's sole host-browser MCP tool over guest loopback."""

from __future__ import annotations

import http.client
import json
import os
import stat
import sys
from urllib.parse import parse_qsl, urlsplit

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PATH = "/mcp"
MAX_URL_LENGTH = 8192
MAX_RESPONSE_BYTES = 16 * 1024
APPROVED_HOSTS = {"auth.openai.com", "chatgpt.com"}
CALLBACK_SECRET_PARAMETERS = {"code", "access_token", "refresh_token", "id_token", "code_verifier"}


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


def load_bridge_token() -> str:
    raw = os.environ.get("VM_BROWSER_BRIDGE_TOKEN_FILE", "")
    if not raw.startswith("/") or "\n" in raw or "\r" in raw:
        raise ValueError("bridge token is not configured")
    try:
        fd = os.open(raw, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError("bridge token is unavailable") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > 256):
            raise ValueError("bridge token is not private")
        token = os.read(fd, 257).decode("ascii").strip()
        if len(token) < 32 or any(ch.isspace() for ch in token):
            raise ValueError("bridge token is invalid")
        return token
    finally:
        os.close(fd)


def bridge_port() -> int:
    raw = os.environ.get("VM_BROWSER_BRIDGE_PORT", "")
    if not raw.isascii() or not raw.isdecimal() or not 1024 <= int(raw) <= 65535:
        raise ValueError("bridge port is not configured")
    return int(raw)


def _request(
    connection: http.client.HTTPConnection,
    token: str,
    message: dict,
    *,
    session_id: str | None = None,
    protocol_version: str | None = None,
    notification: bool = False,
):
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("host browser request was rejected")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    if protocol_version:
        headers["MCP-Protocol-Version"] = protocol_version
    connection.request("POST", BRIDGE_PATH, body=body, headers=headers)
    response = connection.getresponse()
    if response.length is None or response.length > MAX_RESPONSE_BYTES:
        response.close()
        raise ValueError("host browser request was rejected")
    response_body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(response_body) > MAX_RESPONSE_BYTES:
        raise ValueError("host browser request was rejected")
    if notification:
        if response.status != 202:
            raise ValueError("host browser request was rejected")
        return None, response.getheader("Mcp-Session-Id") or session_id
    if response.status != 200:
        raise ValueError("host browser request was rejected")
    try:
        result = json.loads(response_body.decode("utf-8"))
    except Exception as exc:
        raise ValueError("host browser request was rejected") from exc
    if not isinstance(result, dict) or "error" in result:
        raise ValueError("host browser request was rejected")
    return result, response.getheader("Mcp-Session-Id") or session_id


def _close_session(connection, token: str, session_id: str | None, protocol_version: str):
    if not session_id:
        return
    try:
        connection.request("DELETE", BRIDGE_PATH, headers={
            "Authorization": f"Bearer {token}",
            "Mcp-Session-Id": session_id,
            "MCP-Protocol-Version": protocol_version,
        })
        response = connection.getresponse()
        response.read(MAX_RESPONSE_BYTES)
    except (OSError, http.client.HTTPException):
        pass


def call_host_bridge(raw_url: str, token: str | None = None, *, port: int | None = None):
    if not is_authorization_url(raw_url):
        raise ValueError("authorization URL rejected")
    token = load_bridge_token() if token is None else token
    port = bridge_port() if port is None else port
    if not isinstance(token, str) or len(token) < 32 or not token.isascii():
        raise ValueError("bridge token is invalid")
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("bridge port is invalid")
    connection = http.client.HTTPConnection(BRIDGE_HOST, port, timeout=5)
    session_id = None
    version = None
    try:
        initialized, session_id = _request(connection, token, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "little-mester-browser-open", "version": "1"},
            },
        })
        result = initialized.get("result")
        if not isinstance(result, dict) or result.get("protocolVersion") not in {
            "2025-03-26", "2025-06-18", "2025-11-25",
        }:
            raise ValueError("host browser request was rejected")
        version = result["protocolVersion"]
        _request(connection, token, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 session_id=session_id, protocol_version=version, notification=True)
        response, _ = _request(connection, token, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "open_auth_url", "arguments": {"url": raw_url}},
        }, session_id=session_id, protocol_version=version)
        tool_result = response.get("result")
        if not isinstance(tool_result, dict) or tool_result.get("isError") is True:
            raise ValueError("host browser request was rejected")
        return tool_result
    except Exception as exc:
        if isinstance(exc, ValueError) and str(exc) in {
            "authorization URL rejected", "host browser request was rejected",
        }:
            raise
        raise ValueError("host browser bridge is unavailable") from exc
    finally:
        if version:
            _close_session(connection, token, session_id, version)
        connection.close()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("Host browser bridge unavailable or authorization URL rejected.", file=sys.stderr)
        return 2
    try:
        call_host_bridge(args[0])
        return 0
    except Exception:  # noqa: BLE001 - do not expose authorization URL or response details.
        # The URL and all request/response bodies are deliberately omitted.
        print("Host browser bridge unavailable or authorization URL rejected.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
