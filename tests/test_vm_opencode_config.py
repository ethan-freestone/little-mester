"""Guest-specific OpenCode V2 rendering tests; all I/O uses local fixtures."""

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("vm_opencode_config", ROOT / "scripts/vm-opencode-config.py")
vm_opencode_config = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(vm_opencode_config)


class GuestConfigTests(unittest.TestCase):
    def test_non_object_mcp_sections_are_rejected(self):
        malformed = (
            {"mcp": []},
            {"mcp": {"servers": []}},
            {"mcpServers": []},
            {"mcpServers": {"legacy": []}},
            {"mcp": {"servers": {"little-mester": {"environment": []}}}},
        )
        for source in malformed:
            with self.subTest(source=source), self.assertRaises(TypeError):
                vm_opencode_config.guest_config(source, "/private/mcp")

    def test_native_v2_config_preserves_providers_permissions_and_other_settings(self):
        source = json.loads((ROOT / "agent/opencode/opencode.json").read_text(encoding="utf-8"))
        plugin_root = "/home/mester/.local/share/little-mester/guest-runtime/opencode/plugins"
        result = vm_opencode_config.guest_config(
            source,
            "/home/mester/.local/share/little-mester/guest-runtime/mcp",
            plugin_root,
            ROOT / "agent/opencode/plugins",
        )

        self.assertNotIn("provider", result)
        self.assertNotIn("permission", result)
        self.assertNotIn("mcpServers", result)
        self.assertNotIn("small_model", result)
        self.assertNotIn("autoupdate", result)
        self.assertEqual(result["update"], "disable")
        self.assertEqual(result["model"], source["model"])
        self.assertEqual(result["share"], source["share"])

        providers = result["providers"]
        self.assertEqual(providers["ollama"]["package"], "aisdk:@ai-sdk/openai-compatible")
        self.assertNotIn("npm", providers["ollama"])
        self.assertNotIn("options", providers["ollama"])
        self.assertEqual(
            providers["ollama"]["settings"]["baseURL"],
            "http://10.0.2.100:11434/v1",
        )
        self.assertEqual(
            providers["llamacpp"]["settings"]["baseURL"],
            "http://10.0.2.100:8080/v1",
        )
        self.assertEqual(providers["ollama"]["models"], source["provider"]["ollama"]["models"])
        self.assertEqual(result["agents"]["title"]["model"], source["small_model"])
        self.assertEqual(
            {(item["action"], item["effect"]) for item in result["permissions"]},
            {("edit", "ask"), ("shell", "ask")},
        )

        server = result["mcp"]["servers"]["little-mester"]
        self.assertEqual(server["type"], "local")
        self.assertEqual(server["command"], ["node", "dist/mcp-server.js"])
        self.assertEqual(server["cwd"], "/home/mester/.local/share/little-mester/guest-runtime/mcp")
        self.assertEqual(server["environment"]["TICKET_DIR"], "/workspace/docs/tickets")
        self.assertEqual(
            result["plugins"],
            [f"{plugin_root}/host-metrics"],
        )

    def test_cli_only_plugins_are_separate_and_native_denies_survive(self):
        source = {
            "provider": {"local": {"npm": "@ai-sdk/openai-compatible", "options": {"baseURL": "http://model.invalid/v1"}, "models": {"coder": {}}}},
            "permissions": [
                {"action": "edit", "resource": "secrets/*", "effect": "deny"},
                {"action": "shell", "resource": "git push *", "effect": "deny"},
            ],
            "mcpServers": {"little-mester": {"command": "ts-node", "args": ["src/mcp-server.ts"], "env": {"TICKET_DIR": "/workspace/docs/tickets"}}},
            "plugins": [
                "/etc/opencode/plugins/host-metrics",
                "/etc/opencode/plugins/host-metrics-tui",
                "/etc/opencode/plugins/little-mester-exit-confirm",
            ],
            "custom_setting": {"retain": True},
        }
        plugin_root = "/private/plugins"
        plugin_source = ROOT / "agent/opencode/plugins"
        server = vm_opencode_config.guest_config(source, "/private/mcp", plugin_root, plugin_source)
        cli = vm_opencode_config.guest_cli_config(source, plugin_root, plugin_source)
        self.assertEqual(server["custom_setting"], {"retain": True})
        self.assertEqual(server["plugins"], [f"{plugin_root}/host-metrics"])
        self.assertEqual(
            cli,
            {"plugins": [f"{plugin_root}/host-metrics-tui", f"{plugin_root}/little-mester-exit-confirm"]},
        )
        self.assertIn({"action": "edit", "resource": "secrets/*", "effect": "deny"}, server["permissions"])
        self.assertIn({"action": "shell", "resource": "git push *", "effect": "deny"}, server["permissions"])
        self.assertEqual(server["providers"]["local"]["settings"]["baseURL"], "http://model.invalid/v1")
        self.assertEqual(source["plugins"][0], "/etc/opencode/plugins/host-metrics")

    def test_existing_v2_mcp_server_environment_and_provider_settings_are_preserved(self):
        source = {
            "providers": {"custom": {"name": "Local", "settings": {"baseURL": "https://llm.invalid/v1", "timeout": 900000}}},
            "mcp": {
                "timeout": {"startup": 60000},
                "servers": {
                    "little-mester": {
                        "type": "local",
                        "command": ["old-command"],
                        "environment": {"TICKET_DIR": "/private/tickets", "KEEP": "yes"},
                    }
                },
            },
            "permissions": [{"action": "edit", "resource": "*", "effect": "deny"}],
        }
        result = vm_opencode_config.guest_config(source, "/private/mcp")
        self.assertEqual(result["providers"], source["providers"])
        self.assertEqual(result["mcp"]["timeout"], {"startup": 60000})
        server = result["mcp"]["servers"]["little-mester"]
        self.assertEqual(server["environment"], {"TICKET_DIR": "/private/tickets", "KEEP": "yes"})
        self.assertEqual(server["command"], ["node", "dist/mcp-server.js"])
        self.assertEqual(server["cwd"], "/private/mcp")
        self.assertEqual(result["permissions"], source["permissions"])
        self.assertEqual(source["mcp"]["servers"]["little-mester"]["command"], ["old-command"])

    def test_explicit_provider_selection_isolated_and_preserves_denies(self):
        source = json.loads((ROOT / "agent/opencode/opencode.json").read_text(encoding="utf-8"))
        source["permissions"] = [
            {"action": "edit", "resource": "secrets/*", "effect": "deny"},
            {"action": "shell", "resource": "git push *", "effect": "deny"},
        ]
        cases = (
            ("none", "", None, set()),
            ("ollama", "qwen3:14b", "ollama/qwen3:14b", {"ollama"}),
            ("llamacpp", "coder-q4", "llamacpp/coder-q4", {"llamacpp"}),
            ("remote", "openai/gpt-4.1", "openai/gpt-4.1", set()),
            ("remote", "", None, set()),
        )
        for provider, model_id, expected_model, expected_providers in cases:
            with self.subTest(provider=provider, model_id=model_id):
                result = vm_opencode_config.guest_config(
                    source,
                    "/private/mcp",
                    model_provider=provider,
                    model_id=model_id,
                )
                self.assertEqual(result.get("model"), expected_model)
                self.assertEqual(
                    {name for name in ("ollama", "llamacpp") if name in result["providers"]},
                    expected_providers,
                )
                self.assertIn(
                    {"action": "edit", "resource": "secrets/*", "effect": "deny"},
                    result["permissions"],
                )
                self.assertIn(
                    {"action": "shell", "resource": "git push *", "effect": "deny"},
                    result["permissions"],
                )
                if provider == "ollama":
                    self.assertEqual(
                        result["providers"]["ollama"]["settings"]["baseURL"],
                        "http://10.0.2.100:11434/v1",
                    )
                if provider == "llamacpp":
                    self.assertEqual(
                        result["providers"]["llamacpp"]["settings"]["baseURL"],
                        "http://10.0.2.100:8080/v1",
                    )

    def test_unconfigured_model_removes_legacy_default_and_small_model(self):
        source = json.loads((ROOT / "agent/opencode/opencode.json").read_text(encoding="utf-8"))
        result = vm_opencode_config.guest_config(
            source,
            "/private/mcp",
            model_provider="none",
            model_id="",
        )
        self.assertNotIn("model", result)
        self.assertNotIn("model", result.get("agents", {}).get("title", {}))

    def test_other_legacy_command_mcp_servers_receive_native_local_type(self):
        source = {
            "mcpServers": {
                "tooling": {"command": "node", "args": ["server.js"], "env": {"MODE": "safe"}},
                "remote": {"url": "https://mcp.invalid/mcp", "enabled": True},
            }
        }
        result = vm_opencode_config.guest_config(source)
        self.assertEqual(
            result["mcp"]["servers"]["tooling"],
            {"type": "local", "command": ["node", "server.js"], "environment": {"MODE": "safe"}},
        )
        self.assertEqual(result["mcp"]["servers"]["remote"]["disabled"], False)

    def test_literal_provider_credentials_are_refused_but_references_are_kept(self):
        secret_fields = (
            ("apiKey", "CANARY_SECRET_VALUE"),
            ("Authorization", "Bearer CANARY_SECRET_VALUE"),
            ("MCP_API_TOKEN", "CANARY_SECRET_VALUE"),
        )
        for field, value in secret_fields:
            with self.subTest(field=field), self.assertRaises(ValueError):
                vm_opencode_config.guest_config({"providers": {"x": {"settings": {field: value}}}})
        result = vm_opencode_config.guest_config(
            {"providers": {"x": {"settings": {"apiKey": "{env:LOCAL_MODEL_KEY}"}}}}
        )
        self.assertEqual(result["providers"]["x"]["settings"]["apiKey"], "{env:LOCAL_MODEL_KEY}")

    def test_cli_writes_private_valid_json_files(self):
        source = ROOT / "agent/opencode/opencode.json"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "opencode.json"
            cli_output = Path(directory) / "cli.json"
            result = subprocess.run(
                [
                    "python3", str(ROOT / "scripts/vm-opencode-config.py"),
                    "--input", str(source), "--output", str(output),
                    "--cli-output", str(cli_output), "--mcp-cwd", "/private/mcp",
                    "--plugin-root", "/private/plugins",
                    "--plugin-source", str(ROOT / "agent/opencode/plugins"),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            parsed = json.loads(output.read_text(encoding="utf-8"))
            cli = json.loads(cli_output.read_text(encoding="utf-8"))
            output_text = output.read_text(encoding="utf-8")
            output_mode = output.stat().st_mode & 0o777
            cli_output_mode = cli_output.stat().st_mode & 0o777
        self.assertEqual(parsed["mcp"]["servers"]["little-mester"]["cwd"], "/private/mcp")
        self.assertEqual(output_mode, 0o600)
        self.assertEqual(cli_output_mode, 0o600)
        self.assertEqual(cli["plugins"], ["/private/plugins/host-metrics-tui", "/private/plugins/little-mester-exit-confirm"])
        self.assertNotIn("CANARY_SECRET_VALUE", output_text)


if __name__ == "__main__":
    unittest.main()
