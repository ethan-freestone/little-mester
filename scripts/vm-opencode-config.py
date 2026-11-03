#!/usr/bin/env python3
"""Render a native OpenCode V2 guest config and a separate CLI plugin config."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_PLUGIN_ROOT = "/home/mester/.local/share/little-mester/guest-runtime/opencode/plugins"
DEFAULT_MCP_CWD = "/home/mester/.local/share/little-mester/guest-runtime/mcp"
PLUGIN_PATH_PREFIX = "/etc/opencode/plugins/"
SECRET_FIELD = re.compile(
    r"(?:api[_-]?key|authorization|token|secret|password|credential)",
    re.IGNORECASE,
)
REFERENCE_VALUE = re.compile(r"\{(?:env|file):[^{}\r\n]+\}")


def _object(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise TypeError(f"OpenCode config field {label!r} must be an object")
    return value


def _check_secret_values(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            current = f"{path}.{key}" if path else str(key)
            if (
                SECRET_FIELD.search(str(key))
                and isinstance(child, str)
                and child
                and REFERENCE_VALUE.fullmatch(child) is None
            ):
                raise ValueError("OpenCode config contains a literal credential value")
            _check_secret_values(child, current)
    elif isinstance(value, list):
        for child in value:
            _check_secret_values(child, path)


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _native_provider(provider: dict) -> dict:
    result = copy.deepcopy(provider)
    npm_package = result.pop("npm", None)
    if npm_package is not None:
        if not isinstance(npm_package, str) or not npm_package:
            raise ValueError("OpenCode provider package is invalid")
        result.setdefault("package", f"aisdk:{npm_package}")
    options = result.pop("options", None)
    if options is not None:
        _object(options, "provider.options")
        settings = result.get("settings", {})
        _object(settings, "provider.settings")
        result["settings"] = _deep_merge(options, settings)

    models = result.get("models", {})
    _object(models, "provider.models")
    native_models = {}
    for model_id, model in models.items():
        _object(model, f"provider.models.{model_id}")
        item = copy.deepcopy(model)
        if "id" in item and "modelID" not in item:
            item["modelID"] = item.pop("id")
        else:
            item.pop("id", None)
        if "release_date" in item and "releaseDate" not in item:
            item["releaseDate"] = item.pop("release_date")
        else:
            item.pop("release_date", None)
        if "options" in item:
            options = item.pop("options")
            _object(options, "provider model options")
            item["settings"] = _deep_merge(options, item.get("settings", {}))
        native_models[model_id] = item
    if "models" in result:
        result["models"] = native_models
    return result


def _legacy_permission_rules(permission: Any) -> list[dict]:
    permission = _object(permission, "permission")
    action_names = {"bash": "shell", "task": "subagent", "write": "edit", "patch": "edit"}
    rules = []
    for action, spec in permission.items():
        action = action_names.get(action, action)
        if isinstance(spec, str):
            if spec not in {"allow", "deny", "ask"}:
                raise ValueError("OpenCode permission effect is invalid")
            rules.append({"action": action, "resource": "*", "effect": spec})
            continue
        resources = _object(spec, "permission rule")
        for resource, effect in resources.items():
            if not isinstance(resource, str) or not isinstance(effect, str) or effect not in {"allow", "deny", "ask"}:
                raise ValueError("OpenCode permission rule is invalid")
            rules.append({"action": action, "resource": resource, "effect": effect})
    return rules


def _native_permission_rules(value: Any) -> list[dict]:
    if not isinstance(value, list):
        raise TypeError("OpenCode config field 'permissions' must be an array")
    for rule in value:
        if not isinstance(rule, dict) or not all(
            isinstance(rule.get(field), str) for field in ("action", "resource", "effect")
        ) or rule["effect"] not in {"allow", "deny", "ask"}:
            raise ValueError("OpenCode permission rule is invalid")
    return copy.deepcopy(value)


def _native_mcp_server(server: Any, name: str) -> dict:
    server = _object(server, f"MCP server {name}")
    result = copy.deepcopy(server)
    if "enabled" in result:
        enabled = result.pop("enabled")
        if type(enabled) is not bool:
            raise ValueError(f"MCP server {name} has an invalid enabled value")
        result.setdefault("disabled", not enabled)
    command = result.get("command")
    if command is not None:
        if result.get("type") not in (None, "local"):
            raise ValueError(f"MCP server {name} local command conflicts with its type")
        result.setdefault("type", "local")
        if isinstance(command, str):
            command = [command]
        if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
            raise ValueError(f"MCP server {name} has an invalid command")
        args = result.pop("args", [])
        if not isinstance(args, list) or not all(isinstance(part, str) for part in args):
            raise ValueError(f"MCP server {name} has invalid args")
        result["command"] = [*command, *args]
    environment = result.pop("env", None)
    if environment is not None:
        _object(environment, f"MCP server {name} env")
        current = result.get("environment", {})
        _object(current, f"MCP server {name} environment")
        result["environment"] = _deep_merge(environment, current)
    timeout = result.get("timeout")
    if isinstance(timeout, int) and not isinstance(timeout, bool):
        if timeout <= 0:
            raise ValueError(f"MCP server {name} has an invalid timeout")
        result["timeout"] = {"catalog": timeout, "execution": timeout}
    return result


def _plugin_name(value: str) -> str | None:
    if not value.startswith(PLUGIN_PATH_PREFIX):
        return None
    name = value[len(PLUGIN_PATH_PREFIX):]
    if not name or "/" in name or name in {".", ".."}:
        raise ValueError("OpenCode plugin path is invalid")
    return name


def _cli_only_plugin(name: str, plugin_source: str | Path | None) -> bool:
    if plugin_source is not None:
        package_path = Path(plugin_source) / name / "package.json"
        if package_path.is_file() and not package_path.is_symlink():
            try:
                package = json.loads(package_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raise ValueError("OpenCode plugin package metadata is invalid") from None
            exports = package.get("exports", {})
            if isinstance(exports, dict):
                return "./tui" in exports and "." not in exports
    # These are the known TUI-only package locations in the source config. If a
    # caller has no source tree, retain their separate CLI placement.
    return name in {"host-metrics-tui", "little-mester-exit-confirm"}


def _partition_plugins(config: dict, plugin_root: str, plugin_source: str | Path | None) -> tuple[list, list]:
    plugins = config.get("plugins", [])
    if not isinstance(plugins, list):
        raise TypeError("OpenCode config field 'plugins' must be an array")
    server_plugins = []
    cli_plugins = []
    for plugin in plugins:
        if not isinstance(plugin, str):
            server_plugins.append(copy.deepcopy(plugin))
            continue
        name = _plugin_name(plugin)
        if name is None:
            server_plugins.append(plugin)
            continue
        guest_path = str(Path(plugin_root) / name)
        if _cli_only_plugin(name, plugin_source):
            cli_plugins.append(guest_path)
        else:
            server_plugins.append(guest_path)
    return server_plugins, cli_plugins


def guest_config(
    source: dict,
    mcp_cwd: str = DEFAULT_MCP_CWD,
    plugin_root: str = DEFAULT_PLUGIN_ROOT,
    plugin_source: str | Path | None = None,
) -> dict:
    """Convert supported legacy fields to native V2 while preserving other values."""
    if not isinstance(source, dict):
        raise TypeError("OpenCode config root must be an object")
    _check_secret_values(source)
    config = copy.deepcopy(source)

    # Native V2 provider settings take precedence over their migrated V1 forms.
    legacy_providers = config.pop("provider", {})
    native_providers = config.get("providers", {})
    _object(legacy_providers, "provider")
    _object(native_providers, "providers")
    converted = {name: _native_provider(value) for name, value in legacy_providers.items()}
    for name, value in native_providers.items():
        _object(value, f"providers.{name}")
        converted[name] = _deep_merge(converted.get(name, {}), value)
    if converted or "providers" in config or legacy_providers:
        config["providers"] = converted
    for provider_id, old_url, guest_url in (
        ("ollama", "http://ollama:11434", "http://10.0.2.100:11434"),
        ("llamacpp", "http://llamacpp:11434", "http://10.0.2.100:8080"),
    ):
        provider = converted.get(provider_id)
        if not isinstance(provider, dict):
            continue
        settings = provider.get("settings", {})
        if not isinstance(settings, dict):
            raise TypeError(f"OpenCode provider {provider_id!r} settings must be an object")
        base_url = settings.get("baseURL")
        if isinstance(base_url, str) and (base_url == old_url or base_url.startswith(old_url + "/")):
            settings["baseURL"] = guest_url + base_url[len(old_url):]

    if "autoupdate" in config:
        old_update = config.pop("autoupdate")
        if type(old_update) is not bool:
            raise ValueError("OpenCode autoupdate setting is invalid")
        config.setdefault("update", "auto" if old_update else "disable")

    if "small_model" in config:
        old_small_model = config.pop("small_model")
        agents = _object(config.setdefault("agents", {}), "agents")
        title_agent = _object(agents.setdefault("title", {}), "agents.title")
        title_agent.setdefault("model", old_small_model)

    legacy_permission = config.pop("permission", None)
    native_permissions = config.get("permissions", [])
    migrated_permissions = _legacy_permission_rules(legacy_permission) if legacy_permission is not None else []
    migrated_permissions.extend(_native_permission_rules(native_permissions))
    if "tools" in config:
        tools = _object(config.pop("tools"), "tools")
        for tool, enabled in tools.items():
            if type(enabled) is not bool:
                raise ValueError("OpenCode tool toggle is invalid")
            if not enabled:
                migrated_permissions.append({"action": tool, "resource": "*", "effect": "deny"})
    if migrated_permissions or "permissions" in config:
        config["permissions"] = migrated_permissions

    mcp = _object(config.setdefault("mcp", {}), "mcp")
    servers = _object(mcp.setdefault("servers", {}), "mcp.servers")
    legacy_servers = config.pop("mcpServers", {})
    legacy_servers = _object(legacy_servers, "mcpServers")
    for name, legacy in legacy_servers.items():
        if name in servers:
            continue
        servers[name] = _native_mcp_server(legacy, name)
    for name, server in list(servers.items()):
        servers[name] = _native_mcp_server(server, name)

    little_mester = servers.get("little-mester", {})
    if not isinstance(little_mester, dict):
        raise TypeError("OpenCode MCP server 'little-mester' must be an object")
    if little_mester.get("type", "local") != "local":
        raise ValueError("OpenCode MCP server 'little-mester' must be local")
    little_mester["type"] = "local"
    little_mester["command"] = ["node", "dist/mcp-server.js"]
    little_mester.pop("args", None)
    little_mester.pop("env", None)
    little_mester["cwd"] = mcp_cwd
    environment = _object(little_mester.setdefault("environment", {}), "mcp.servers.little-mester.environment")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in environment.items()):
        raise ValueError("OpenCode MCP server environment is invalid")
    servers["little-mester"] = little_mester

    server_plugins, _ = _partition_plugins(config, plugin_root, plugin_source)
    if "plugins" in config:
        config["plugins"] = server_plugins
    if config.get("otel") == {}:
        config.pop("otel")
    return config


def guest_cli_config(
    source: dict,
    plugin_root: str = DEFAULT_PLUGIN_ROOT,
    plugin_source: str | Path | None = None,
) -> dict:
    """Return TUI-only plugin entries for the independent OpenCode CLI config."""
    if not isinstance(source, dict):
        raise TypeError("OpenCode config root must be an object")
    _, cli_plugins = _partition_plugins(source, plugin_root, plugin_source)
    return {"plugins": cli_plugins}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".vm-opencode-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mcp-cwd", default=DEFAULT_MCP_CWD)
    parser.add_argument("--plugin-root", default=DEFAULT_PLUGIN_ROOT)
    parser.add_argument("--plugin-source", type=Path)
    parser.add_argument("--cli-output", type=Path)
    args = parser.parse_args()
    try:
        source = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(source, dict):
            raise TypeError("OpenCode config root must be an object")
        rendered = guest_config(source, args.mcp_cwd, args.plugin_root, args.plugin_source)
        _write_json(args.output, rendered)
        if args.cli_output is not None:
            _write_json(args.cli_output, guest_cli_config(source, args.plugin_root, args.plugin_source))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        print("REFUSED: OpenCode guest config could not be rendered safely.", file=__import__("sys").stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
