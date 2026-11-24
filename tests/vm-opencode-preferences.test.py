#!/usr/bin/env python3
"""Safe allowlisted OpenCode preference persistence tests."""

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "guest_opencode_preferences", ROOT / "lib/vm/guest-opencode-preferences.py")
preferences = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = preferences
SPEC.loader.exec_module(preferences)


class PreferenceValidationTests(unittest.TestCase):
    def test_only_model_and_theme_are_accepted(self):
        self.assertEqual(preferences.parse_preferences('{"model":"openai/gpt-5","theme":"dark"}'),
                         {"model": "openai/gpt-5", "theme": "dark"})
        for raw in (
            '{"plugins":["/tmp/plugin"]}',
            '{"permission":{"bash":"allow"}}',
            '{"share":"manual"}',
            '{"model":"openai/gpt-5","model":"evil/model"}',
            '{"model":"openai/gpt-5\n--unsafe"}',
            '{"theme":"dark\\nplugins"}',
        ):
            with self.subTest(raw=raw), self.assertRaises(preferences.Refused):
                preferences.parse_preferences(raw)

    def test_cli_config_keeps_protected_plugin_and_permissions(self):
        config = preferences.build_cli_config({"model": "openai/gpt-5", "theme": "dark"})
        self.assertEqual(config["plugins"], ["/etc/opencode/plugins/little-mester-exit-confirm"])
        self.assertEqual(config["keybinds"], {"app.exit": "none", "prompt.clear": "none"})
        self.assertNotIn("model", config)
        self.assertEqual(config["theme"], {"name": "dark"})
        for key in ("permission", "share", "autoupdate", "provider", "mcp", "server"):
            self.assertNotIn(key, config)
        with_metrics = preferences.build_cli_config({"theme": "dark"}, host_metrics=True)
        self.assertIn(preferences.PROTECTED_METRICS_PLUGIN, with_metrics["plugins"])
        self.assertIn(preferences.PROTECTED_PLUGIN, with_metrics["plugins"])

    def test_missing_preferences_yield_only_protected_runtime_config(self):
        config = preferences.build_cli_config({})
        self.assertEqual(config["plugins"], ["/etc/opencode/plugins/little-mester-exit-confirm"])
        self.assertNotIn("model", config)
        self.assertNotIn("theme", config)
        self.assertNotIn(preferences.PROTECTED_METRICS_PLUGIN, config["plugins"])

    def test_model_is_only_passed_to_root_protected_server_config(self):
        self.assertEqual(preferences.parse_preferences('{"model":"openai/gpt-5"}')["model"],
                         "openai/gpt-5")
        contract = (ROOT / "lib/vm/parent-session-contract.py").read_text()
        self.assertIn('"model": "{env:LM_OPENCODE_MODEL}"', contract)
        self.assertIn("opencode-model.json", contract)
        runner = contract.split("OPENCODE_RUNNER = b\"\"\"", 1)[1].split('"""', 1)[0]
        self.assertIn("lm-guest-opencode-preferences.py model", runner)
        self.assertIn("LM_OPENCODE_MODEL=", runner)
        self.assertIn("export METRICS_MODE=vm", runner)
        self.assertNotIn("config.update(safe)", (ROOT / "lib/vm/guest-opencode-preferences.py").read_text())

    def test_atomic_preferences_round_trip_and_refuse_unsafe_file_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = pathlib.Path(temp) / "state"
            parent.mkdir(mode=0o700)
            parent.chmod(0o700)
            path = parent / "preferences.json"
            preferences.write_preferences(path, {"model": "openai/gpt-5", "theme": "dark"},
                                         uid=os.getuid())
            self.assertEqual(preferences.read_preferences(path, uid=os.getuid()),
                             {"model": "openai/gpt-5", "theme": "dark"})
            preferences.write_preferences(path, {"model": "openai/gpt-5.1"}, uid=os.getuid())
            self.assertEqual(preferences.read_preferences(path, uid=os.getuid()),
                             {"model": "openai/gpt-5.1"})
            path.chmod(0o644)
            with self.assertRaises(preferences.Refused):
                preferences.read_preferences(path, uid=os.getuid())

    def test_preference_writer_does_not_replace_existing_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = pathlib.Path(temp)
            parent.chmod(0o700)
            path = parent / "preferences.json"
            path.write_text('{"model":"openai/old"}\n')
            path.chmod(0o644)
            with self.assertRaises(preferences.Refused):
                preferences.write_preferences(path, {"model": "openai/gpt-5"}, uid=os.getuid())
            self.assertEqual(path.read_text(), '{"model":"openai/old"}\n')


if __name__ == "__main__":
    unittest.main()
