#!/usr/bin/env python3
"""Call the host's single-purpose browser MCP tool over reverse SSH."""

from __future__ import annotations

import http.client
import json
import os
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl


BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 19533
BRIDGE_PATH = "/mcp"
MAX_URL_LENGTH = 8192
APPROVED_HOSTS = {"auth.openai.com", "chatgpt.com"}
CALLBACK_SECRET_PARAMETERS = {
    "code", "access_token", "refresh_token", "id_token", "code_verifier"
}


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


def read_token(token_path: str | Path) -> str:
    path = Path(token_path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("bridge token file is not private")
    token = path.read_text(encoding="ascii").strip()
    if not 32 <= len(token) <= 256 or any(char.isspace() for char in token):
        raise ValueError("bridge token is invalid")
    return token


def _request(
    connection: http.client.HTTPConnection,
    token: str,
    message: dict,
    *,
    notification=False,
    session_id=None,
    protocol_version=None,
):
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    if protocol_version:
        headers["MCP-Protocol-Version"] = protocol_version
    connection.request(
        "POST",
        BRIDGE_PATH,
        body=body,
        headers=headers,
    )
    response = connection.getresponse()
    returned_session_id = response.getheader("Mcp-Session-Id")
    raw = response.read(16 * 1024 + 1)
    if len(raw) > 16 * 1024:
        raise ValueError("bridge response too large")
    if notification:
        if response.status != 202:
            raise ValueError("bridge initialization failed")
        return None, returned_session_id
    if response.status != 200:
        raise ValueError("bridge request failed")

    content_type = response.getheader("Content-Type", "")
    if "text/event-stream" in content_type:
        for line in raw.decode("utf-8", errors="strict").splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip()), returned_session_id
        raise ValueError("bridge response missing")
    return json.loads(raw.decode("utf-8")), returned_session_id


def _close_session(connection, token, session_id, protocol_version):
    if not session_id:
        return
    try:
        connection.request(
            "DELETE",
            BRIDGE_PATH,
            headers={
                "Authorization": f"Bearer {token}",
                "Mcp-Session-Id": session_id,
                "MCP-Protocol-Version": protocol_version,
            },
        )
        response = connection.getresponse()
        response.read(1024)
    except Exception:
        pass


def call_host_bridge(
    raw_url: str,
    token: str,
    *,
    host: str = BRIDGE_HOST,
    port: int = BRIDGE_PORT,
) -> None:
    if not is_authorization_url(raw_url):
        raise ValueError("authorization URL rejected")

    # A replacement can cancel an old forward (10s), create the new one (10s)
    # and launch the host browser (15s). Let the server report its bounded error.
    connection = http.client.HTTPConnection(host, port, timeout=40)
    session_id = None
    protocol_version = "2025-03-26"
    try:
        initialized, session_id = _request(
            connection,
            token,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "lab-vm-browser-opener", "version": "1.0.0"},
                },
            },
        )
        if initialized.get("error") or not initialized.get("result"):
            raise ValueError("bridge initialization failed")
        protocol_version = initialized["result"].get("protocolVersion", protocol_version)
        if not session_id:
            raise ValueError("bridge did not create a session")
        _request(
            connection,
            token,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            notification=True,
            session_id=session_id,
            protocol_version=protocol_version,
        )
        result = _request(
            connection,
            token,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "open_auth_url",
                    "arguments": {"url": raw_url},
                },
            },
            session_id=session_id,
            protocol_version=protocol_version,
        )[0]
        if result.get("error") or result.get("result", {}).get("isError"):
            raise ValueError("host browser rejected request")
    finally:
        _close_session(connection, token, session_id, protocol_version)
        connection.close()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    token_file = os.environ.get("VM_BROWSER_BRIDGE_TOKEN_FILE", "")
    if len(argv) != 1 or not token_file:
        print("Host browser bridge unavailable or authorization URL rejected.", file=sys.stderr)
        return 2
    try:
        token = read_token(token_file)
        call_host_bridge(argv[0], token)
    except Exception:
        # The URL and token are never included in diagnostics.
        print("Host browser bridge unavailable or authorization URL rejected.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
