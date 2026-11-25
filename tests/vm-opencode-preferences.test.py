#!/usr/bin/env python3
"""Safe allowlisted OpenCode preference persistence tests."""

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


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


class PreferenceCommandTests(unittest.TestCase):
    @staticmethod
    def run_command(path, *arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(preferences, "PREFERENCE_PATH", pathlib.Path(path)), \
                mock.patch.object(sys, "argv", ["guest-opencode-preferences.py", *arguments]), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = preferences.main()
        return result, stdout.getvalue(), stderr.getvalue()

    def test_show_unset_is_read_only_and_does_not_create_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = pathlib.Path(temporary) / "not-created"
            path = parent / "preferences.json"
            result, stdout, stderr = self.run_command(path, "show")
            self.assertEqual(result, 0)
            self.assertEqual(stdout, "{}\n")
            self.assertEqual(stderr, "")
            self.assertFalse(parent.exists())
            self.assertFalse(path.exists())

    def test_commands_round_trip_and_partial_updates_preserve_other_preference(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = pathlib.Path(temporary) / "state"
            parent.mkdir(mode=0o700)
            parent.chmod(0o700)
            path = parent / "preferences.json"

            result, _, stderr = self.run_command(
                path, "set", "--model", "openai/gpt-4.1", "--theme", "dark")
            self.assertEqual((result, stderr), (0, ""))
            self.assertEqual(preferences.read_preferences(path, uid=os.getuid()), {
                "model": "openai/gpt-4.1", "theme": "dark",
            })

            before_show = path.stat()
            before_bytes = path.read_bytes()
            result, stdout, stderr = self.run_command(path, "show")
            self.assertEqual((result, stderr), (0, ""))
            self.assertEqual(json.loads(stdout), {"model": "openai/gpt-4.1", "theme": "dark"})
            after_show = path.stat()
            self.assertEqual(path.read_bytes(), before_bytes)
            self.assertEqual((after_show.st_ino, after_show.st_mtime_ns),
                             (before_show.st_ino, before_show.st_mtime_ns))

            result, _, stderr = self.run_command(path, "set", "--model", "openai/gpt-4.2")
            self.assertEqual((result, stderr), (0, ""))
            self.assertEqual(preferences.read_preferences(path, uid=os.getuid()), {
                "model": "openai/gpt-4.2", "theme": "dark",
            })
            result, stdout, stderr = self.run_command(path, "model")
            self.assertEqual((result, stdout, stderr), (0, "openai/gpt-4.2\n", ""))

            result, _, stderr = self.run_command(path, "set", "--theme", "nord")
            self.assertEqual((result, stderr), (0, ""))
            self.assertEqual(preferences.read_preferences(path, uid=os.getuid()), {
                "model": "openai/gpt-4.2", "theme": "nord",
            })
            result, _, stderr = self.run_command(path, "set", "--clear-model")
            self.assertEqual((result, stderr), (0, ""))
            self.assertEqual(preferences.read_preferences(path, uid=os.getuid()), {"theme": "nord"})
            result, _, stderr = self.run_command(
                path, "set", "--model", "openai/gpt-4.1", "--clear-theme")
            self.assertEqual((result, stderr), (0, ""))
            self.assertEqual(preferences.read_preferences(path, uid=os.getuid()), {
                "model": "openai/gpt-4.1",
            })

    def test_metadata_diagnostics_are_bounded_and_do_not_leak_path_or_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = pathlib.Path(temporary) / "ABSOLUTE-PATH-PRIVATE-SENTINEL"
            parent.mkdir(mode=0o700)
            parent.chmod(0o700)
            path = parent / "preferences.json"
            content = '{"model":"PRIVATE-CONTENT-SENTINEL"}'
            path.write_text(content, encoding="utf-8")
            path.chmod(0o644)

            result, stdout, stderr = self.run_command(path, "show")
            self.assertEqual(result, 2)
            self.assertEqual(stdout, "")
            self.assertIn("preferences.json metadata unsafe", stderr)
            self.assertIn(f"expected owner={os.getuid()} type=regular mode=0600 links=1 size<=4096",
                          stderr)
            self.assertIn(f"observed owner={os.getuid()} type=regular mode=0644", stderr)
            self.assertNotIn(str(parent), stderr)
            self.assertNotIn("PRIVATE-CONTENT-SENTINEL", stderr)
            self.assertLess(len(stderr), 512)

            path.chmod(0o600)
            parent.chmod(0o777)
            result, _, stderr = self.run_command(path, "show")
            self.assertEqual(result, 2)
            self.assertIn("preferences.json parent metadata unsafe", stderr)
            self.assertIn("type=directory mode=not-group/world-writable", stderr)
            self.assertIn("observed owner=", stderr)
            self.assertIn("type=directory mode=0777", stderr)
            self.assertNotIn(str(parent), stderr)
            self.assertNotIn("PRIVATE-CONTENT-SENTINEL", stderr)
            self.assertLess(len(stderr), 512)

    def test_set_refuses_malformed_duplicate_and_protected_files_without_replacing_them(self):
        invalid_documents = (
            "{not-json PRIVATE-CONTENT-SENTINEL",
            '{"model":"openai/old","model":"openai/PRIVATE-CONTENT-SENTINEL"}',
            '{"plugins":["/ABSOLUTE-PATH-PRIVATE-SENTINEL"]}',
            '{"permission":{"bash":"allow"}}',
        )
        with tempfile.TemporaryDirectory() as temporary:
            parent = pathlib.Path(temporary)
            parent.chmod(0o700)
            path = parent / "preferences.json"
            for document in invalid_documents:
                with self.subTest(document=document):
                    original = document.encode("utf-8")
                    path.write_bytes(original)
                    path.chmod(0o600)
                    result, stdout, stderr = self.run_command(
                        path, "set", "--theme", "dark")
                    self.assertEqual(result, 2)
                    self.assertEqual(stdout, "")
                    self.assertEqual(path.read_bytes(), original)
                    self.assertNotIn(document, stderr)
                    self.assertNotIn(str(parent), stderr)
                    self.assertNotIn("PRIVATE-CONTENT-SENTINEL", stderr)
                    self.assertNotIn("ABSOLUTE-PATH-PRIVATE-SENTINEL", stderr)


if __name__ == "__main__":
    unittest.main()
