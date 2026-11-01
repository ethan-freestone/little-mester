#!/usr/bin/env python3
"""Render the host OpenCode config for the direct guest runtime."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def guest_config(source: dict, mcp_cwd: str) -> dict:
    config = json.loads(json.dumps(source))

    for provider, old_url, guest_url in (
        ("ollama", "http://ollama:11434", "http://10.0.2.100:11434"),
        ("llamacpp", "http://llamacpp:11434", "http://10.0.2.100:8080"),
    ):
        provider_config = config.get("provider", {}).get(provider, {})
        options = provider_config.get("options", {})
        base_url = options.get("baseURL")
        if isinstance(base_url, str) and (base_url == old_url or base_url.startswith(old_url + "/")):
            options["baseURL"] = guest_url + base_url[len(old_url):]

    mcp = config.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        raise ValueError("OpenCode config field 'mcp' must be an object")
    servers = mcp.setdefault("servers", {})
    if not isinstance(servers, dict):
        raise ValueError("OpenCode config field 'mcp.servers' must be an object")

    legacy_servers = config.pop("mcpServers", {})
    if not isinstance(legacy_servers, dict):
        raise ValueError("Legacy OpenCode config field 'mcpServers' must be an object")
    for name, legacy in legacy_servers.items():
        if name in servers:
            continue
        if not isinstance(legacy, dict):
            raise ValueError(f"Legacy MCP server '{name}' must be an object")
        command = legacy.get("command")
        args = legacy.get("args", [])
        if isinstance(command, str):
            command = [command]
        if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
            raise ValueError(f"Legacy MCP server '{name}' has an invalid command")
        if not isinstance(args, list) or not all(isinstance(part, str) for part in args):
            raise ValueError(f"Legacy MCP server '{name}' has invalid args")
        environment = legacy.get("environment", legacy.get("env", {}))
        if not isinstance(environment, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in environment.items()
        ):
            raise ValueError(f"Legacy MCP server '{name}' has an invalid environment")
        servers[name] = {
            "type": "local",
            "command": [*command, *args],
            "cwd": legacy.get("cwd", "."),
            "environment": environment,
        }

    server = servers.get("little-mester")
    if not isinstance(server, dict) or server.get("type", "local") != "local":
        raise ValueError("OpenCode config must define a local 'little-mester' MCP server")
    server["type"] = "local"
    server["command"] = ["node", "dist/mcp-server.js"]
    server.pop("args", None)
    server.pop("env", None)
    server["cwd"] = mcp_cwd
    server.setdefault("environment", {})
    if not isinstance(server["environment"], dict):
        raise ValueError("OpenCode MCP server environment must be an object")

    return config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mcp-cwd", required=True)
    args = parser.parse_args()
    source = json.loads(args.input.read_text())
    if not isinstance(source, dict):
        raise SystemExit("OpenCode config root must be an object")
    rendered = guest_config(source, args.mcp_cwd)
    args.output.write_text(json.dumps(rendered, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
