"""Tests for guest-specific OpenCode MCP config rendering."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "vm_opencode_config", ROOT / "scripts/vm-opencode-config.py"
)
vm_opencode_config = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(vm_opencode_config)


class GuestConfigTests(unittest.TestCase):
    def test_non_object_mcp_sections_raise_type_error(self):
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

    def test_legacy_mcp_config_is_converted_to_v2_and_guest_private_runtime(self):
        source = json.loads((ROOT / "agent/opencode/opencode.json").read_text())
        result = vm_opencode_config.guest_config(
            source, "/home/mester/.local/share/little-mester/mcp"
        )

        server = result["mcp"]["servers"]["little-mester"]
        self.assertEqual(server["type"], "local")
        self.assertEqual(server["command"], ["node", "dist/mcp-server.js"])
        self.assertEqual(server["cwd"], "/home/mester/.local/share/little-mester/mcp")
        self.assertEqual(server["environment"]["TICKET_DIR"], "/workspace/docs/tickets")
        self.assertNotIn("mcpServers", result)
        self.assertEqual(
            result["provider"]["ollama"]["options"]["baseURL"],
            "http://10.0.2.100:11434/v1",
        )
        self.assertEqual(
            result["provider"]["llamacpp"]["options"]["baseURL"],
            "http://10.0.2.100:8080/v1",
        )
        self.assertEqual(result["model"], source["model"])

    def test_existing_v2_server_preserves_environment_and_unrelated_settings(self):
        source = {
            "model": "provider/model",
            "mcp": {
                "servers": {
                    "little-mester": {
                        "type": "local",
                        "command": ["old-command"],
                        "environment": {"TICKET_DIR": "/tickets"},
                    }
                }
            },
        }
        result = vm_opencode_config.guest_config(source, "/private/mcp")
        server = result["mcp"]["servers"]["little-mester"]
        self.assertEqual(server["command"], ["node", "dist/mcp-server.js"])
        self.assertEqual(server["environment"], {"TICKET_DIR": "/tickets"})
        self.assertEqual(result["model"], "provider/model")
        self.assertEqual(source["mcp"]["servers"]["little-mester"]["command"], ["old-command"])

    def test_cli_writes_valid_json(self):
        source = ROOT / "agent/opencode/opencode.json"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "guest.json"
            import subprocess
            subprocess.run(
                [
                    "python3", str(ROOT / "scripts/vm-opencode-config.py"),
                    "--input", str(source), "--output", str(output),
                    "--mcp-cwd", "/private/mcp",
                ],
                check=True,
            )
            parsed = json.loads(output.read_text())
        self.assertEqual(parsed["mcp"]["servers"]["little-mester"]["cwd"], "/private/mcp")


if __name__ == "__main__":
    unittest.main()
