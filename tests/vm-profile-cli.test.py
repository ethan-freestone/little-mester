#!/usr/bin/env python3
"""Parser and fixed-helper dispatch tests for the public named-profile CLI."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader(
    "vm_profile_cli_test", str(ROOT / "tools/vm-profile-cli"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)
CORE_LOADER = importlib.machinery.SourceFileLoader(
    "vm_profile_control_core_test", str(ROOT / "lib/vm/profile-control-core.py"))
CORE_SPEC = importlib.util.spec_from_loader(CORE_LOADER.name, CORE_LOADER)
core = importlib.util.module_from_spec(CORE_SPEC)
CORE_SPEC.loader.exec_module(core)


class ProfileCliTests(unittest.TestCase):
    def parse(self, *argv):
        return cli.parser().parse_args(list(argv))

    def test_default_profile_and_option_position(self):
        args = self.parse("up")
        self.assertEqual(cli.request_for(args), {
            "protocol": 1, "operation": "up", "profile": "default",
        })
        args = self.parse("--profile", "amber", "status")
        self.assertEqual(cli.request_for(args)["profile"], "amber")
        with self.assertRaises(SystemExit):
            self.parse("status", "--profile", "amber")

    def test_profile_names_fail_closed_for_reserved_or_malformed_names(self):
        for name in ("System", "system", "profiles", "Bad_name", "a" * 33):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "profile name"):
                cli.request_for(self.parse("--profile", name, "status"))

    def test_workspace_precedence_and_unsafe_recurse_defaults(self):
        args = self.parse("--profile", "dev", "up", "positional", "--dir", "explicit",
                          "--unsafe-recurse", "--reassign")
        request = cli.request_for(args)
        self.assertEqual(request, {
            "protocol": 1, "operation": "up", "profile": "dev",
            "workspace": str(pathlib.Path("explicit").absolute()),
            "unsafe_recurse": True, "reassign": True,
        })
        self.assertEqual(core.validate_request(request), request)
        args = self.parse("up", "--unsafe-recurse")
        request = cli.request_for(args)
        self.assertEqual(request["workspace"], str(pathlib.Path(".").absolute()))
        self.assertEqual(core.validate_request(request), request)
        with self.assertRaisesRegex(ValueError, "requires a newly selected"):
            cli.request_for(self.parse("up", "--reassign"))

    def test_install_pins_only_committed_full_head_and_checkout_path(self):
        with tempfile.TemporaryDirectory(prefix="vm-profile-cli-git-") as temporary:
            repository = pathlib.Path(temporary)
            subprocess = cli.subprocess
            subprocess.run(["/usr/bin/git", "init", "-q", str(repository)], check=True)
            (repository / "source.txt").write_text("committed\n", encoding="utf-8")
            subprocess.run(["/usr/bin/git", "-C", str(repository), "add", "source.txt"], check=True)
            subprocess.run(["/usr/bin/git", "-C", str(repository), "-c", "user.name=test",
                            "-c", "user.email=test@example.invalid", "commit", "-qm", "candidate"],
                           check=True)
            sha = subprocess.run(["/usr/bin/git", "-C", str(repository), "rev-parse", "HEAD"],
                                 check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
            (repository / "source.txt").write_text("uncommitted\n", encoding="utf-8")
            request = cli.request_for(self.parse("install"), repository=repository)
            self.assertEqual(request, {
                "protocol": 1, "operation": "install", "profile": "default",
                "repository": str(repository.resolve()), "commit_sha": sha,
            })
            self.assertEqual(core.validate_request(request), request)

    def test_profile_command_requests_match_the_fixed_helper_protocol(self):
        requests = (
            cli.request_for(self.parse("status")),
            cli.request_for(self.parse("down")),
            cli.request_for(self.parse("settings", "show")),
            cli.request_for(self.parse("settings", "set", "--model", "openai/gpt-4.1")),
            cli.request_for(self.parse("profiles", "list")),
            cli.request_for(self.parse("profiles", "show", "amber")),
            cli.request_for(self.parse("installations")),
            cli.request_for(self.parse("prune", "--dry-run")),
        )
        for request in requests:
            with self.subTest(request=request):
                self.assertEqual(core.validate_request(request), request)

    def test_settings_request_accepts_only_model_and_theme(self):
        self.assertEqual(cli.request_for(self.parse(
            "settings", "set", "--model", "openai/gpt-4.1", "--theme", "dark")), {
                "protocol": 1, "operation": "settings.set", "profile": "default",
                "model": "openai/gpt-4.1", "theme": "dark",
            })
        self.assertEqual(cli.request_for(self.parse("settings", "show")), {
            "protocol": 1, "operation": "settings.show", "profile": "default",
        })
        for model in ("ollama/qwen3", "openai/../escape", "openai/bad model"):
            with self.subTest(model=model), self.assertRaisesRegex(ValueError, "OpenAI model ID"):
                cli.request_for(self.parse("settings", "set", "--model", model))
        with self.assertRaisesRegex(ValueError, "needs --model"):
            cli.request_for(self.parse("settings", "set"))

    def test_global_commands_reject_profile_and_prune_has_no_confirmation_bypass(self):
        for argv in (("--profile", "dev", "installations"),
                     ("--profile", "dev", "profiles", "list"),
                     ("--profile", "dev", "prune", "--dry-run")):
            with self.subTest(argv=argv), self.assertRaisesRegex(ValueError, "not valid with global"):
                cli.request_for(self.parse(*argv))
        self.assertEqual(cli.request_for(self.parse("prune", "--dry-run")), {
            "protocol": 1, "operation": "prune", "dry_run": True,
        })
        with self.assertRaises(SystemExit):
            self.parse("prune", "--yes")

    def test_profiles_list_and_show_use_global_protocol_operations(self):
        self.assertEqual(cli.request_for(self.parse("profiles", "list")), {
            "protocol": 1, "operation": "profiles.list",
        })
        self.assertEqual(cli.request_for(self.parse("profiles", "show", "amber")), {
            "protocol": 1, "operation": "profiles.show", "profile": "amber",
        })

    def test_dispatch_sends_only_protocol_record_to_fixed_sudo_helper(self):
        with tempfile.TemporaryDirectory(prefix="vm-profile-helper-") as temporary:
            helper = pathlib.Path(temporary) / "profile-control"
            helper.touch()
            captured = {}

            class Completed:
                returncode = 0

            def run(argv, **kwargs):
                captured["argv"] = argv
                captured.update(kwargs)
                return Completed()

            request = {"protocol": 1, "operation": "settings.set", "profile": "default",
                       "theme": "dark"}
            self.assertEqual(cli.dispatch(request, helper=str(helper), runner=run), 0)
            self.assertEqual(captured["argv"], [cli.SUDO, str(helper)])
            self.assertEqual(json.loads(captured["input"]), request)
            self.assertEqual(set(json.loads(captured["input"])),
                             {"protocol", "operation", "profile", "theme"})

    def test_missing_helper_is_a_bootstrap_prerequisite_without_execution(self):
        runner = mock.Mock()
        with mock.patch("builtins.print") as output:
            self.assertEqual(cli.dispatch({"protocol": 1, "operation": "status", "profile": "default"},
                                          helper="/missing/profile-control", runner=runner), 3)
        runner.assert_not_called()
        self.assertIn("./lab vm bootstrap", str(output.call_args))

    def test_signed_bootstrap_is_not_replaced_by_mutable_checkout_execution(self):
        self.assertIsNone(cli.request_for(self.parse("bootstrap")))
        self.assertIsNone(cli.request_for(self.parse("control", "update")))
        with self.assertRaisesRegex(ValueError, "not valid with global"):
            cli.request_for(self.parse("--profile", "dev", "bootstrap"))


if __name__ == "__main__":
    unittest.main()
