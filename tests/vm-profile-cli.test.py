#!/usr/bin/env python3
"""Parser and fixed-helper dispatch tests for the public named-profile CLI."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import pathlib
import subprocess
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

    @staticmethod
    def _fixture_git_environment():
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("GIT_")}
        environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
        return environment

    @classmethod
    def _make_repository(cls, repository, *, object_format="sha1"):
        repository.mkdir()
        command = ["/usr/bin/git", "init", "-q"]
        if object_format != "sha1":
            command.append(f"--object-format={object_format}")
        command.append(str(repository))
        environment = cls._fixture_git_environment()
        subprocess.run(command, check=True, env=environment)
        (repository / "source.txt").write_text("committed\n", encoding="utf-8")
        subprocess.run(["/usr/bin/git", "-C", str(repository), "add", "source.txt"],
                       check=True, env=environment)
        subprocess.run(["/usr/bin/git", "-C", str(repository), "-c", "user.name=test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "candidate"],
                       check=True, env=environment)
        result = subprocess.run(["/usr/bin/git", "-C", str(repository), "rev-parse", "HEAD"],
                                check=True, text=True, stdout=subprocess.PIPE, env=environment)
        return repository, result.stdout.strip()

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

    def test_install_pins_committed_sha1_and_sha256_heads_and_ignores_dirty_files(self):
        with tempfile.TemporaryDirectory(prefix="vm-profile-cli-git-") as temporary:
            for object_format, expected_length in (("sha1", 40), ("sha256", 64)):
                with self.subTest(object_format=object_format):
                    repository, sha = self._make_repository(
                        pathlib.Path(temporary) / object_format, object_format=object_format)
                    self.assertEqual(len(sha), expected_length)
                    (repository / "source.txt").write_text("uncommitted\n", encoding="utf-8")
                    request = cli.request_for(self.parse("install"), repository=repository)
                    self.assertEqual(request, {
                        "protocol": 1, "operation": "install", "profile": "default",
                        "repository": str(repository.resolve()), "commit_sha": sha,
                    })
                    self.assertEqual(core.validate_request(request), request)

    def test_candidate_lookup_uses_fixed_git_clean_environment_and_timeout(self):
        sha = "a" * 40
        completed = subprocess.CompletedProcess([], 0, stdout=sha + "\n")
        with mock.patch.object(cli.subprocess, "run", return_value=completed) as run:
            self.assertEqual(cli._candidate_sha(pathlib.Path("/checkout")), sha)
        argv, kwargs = run.call_args
        self.assertEqual(argv[0], [cli.GIT, "--no-replace-objects", "-C", "/checkout",
                                   "rev-parse", "--verify", "HEAD^{commit}"])
        self.assertEqual(kwargs["env"], {
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "XDG_CONFIG_HOME": "/nonexistent",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_LITERAL_PATHSPECS": "1",
        })
        self.assertEqual(kwargs["timeout"], cli.GIT_TIMEOUT_SECONDS)
        self.assertGreater(kwargs["timeout"], 0)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)

    def test_candidate_rejects_sha_outside_exact_lowercase_grammar(self):
        invalid_shas = ("a" * 39, "a" * 41, "a" * 63, "a" * 65,
                        "A" * 40, "g" * 40)
        for sha in invalid_shas:
            with self.subTest(sha=sha):
                completed = subprocess.CompletedProcess([], 0, stdout=sha + "\n")
                with mock.patch.object(cli.subprocess, "run", return_value=completed):
                    with self.assertRaisesRegex(ValueError, "full lowercase 40- or 64-character SHA"):
                        cli._candidate_sha(pathlib.Path("/checkout"))

    def test_hostile_git_environment_and_global_config_are_ignored(self):
        with tempfile.TemporaryDirectory(prefix="vm-profile-cli-hostile-git-") as temporary:
            root = pathlib.Path(temporary)
            repository, sha = self._make_repository(root / "expected")
            other_repository, _ = self._make_repository(root / "other")
            global_config = root / "hostile-gitconfig"
            global_config.write_text("[malformed\n", encoding="utf-8")
            hostile_environment = {
                "GIT_DIR": str(other_repository / ".git"),
                "GIT_WORK_TREE": str(other_repository),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.repositoryformatversion",
                "GIT_CONFIG_VALUE_0": "999",
                "GIT_CONFIG_GLOBAL": str(global_config),
                "GIT_CONFIG_NOSYSTEM": "0",
            }
            with mock.patch.dict(os.environ, hostile_environment):
                request = cli.request_for(self.parse("install"), repository=repository)
            self.assertEqual(request["commit_sha"], sha)
            self.assertEqual(request["repository"], str(repository.resolve()))

    def test_linked_worktree_resolves_its_committed_head(self):
        with tempfile.TemporaryDirectory(prefix="vm-profile-cli-worktree-") as temporary:
            root = pathlib.Path(temporary)
            repository, sha = self._make_repository(root / "repository")
            linked_worktree = root / "linked-worktree"
            subprocess.run(["/usr/bin/git", "-C", str(repository), "worktree", "add", "-q",
                            "--detach", str(linked_worktree), sha],
                           check=True, env=self._fixture_git_environment())
            request = cli.request_for(self.parse("install"), repository=linked_worktree)
            self.assertEqual(request["repository"], str(linked_worktree.resolve()))
            self.assertEqual(request["commit_sha"], sha)

    def test_unborn_nonrepository_and_noncommit_heads_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="vm-profile-cli-invalid-git-") as temporary:
            root = pathlib.Path(temporary)
            nonrepository = root / "not-a-repository"
            nonrepository.mkdir()

            unborn = root / "unborn"
            unborn.mkdir()
            subprocess.run(["/usr/bin/git", "init", "-q", str(unborn)], check=True,
                           env=self._fixture_git_environment())

            noncommit, _ = self._make_repository(root / "noncommit")
            environment = self._fixture_git_environment()
            blob = subprocess.run(["/usr/bin/git", "-C", str(noncommit), "hash-object", "-w",
                                   "--stdin"], input=b"not a commit\n", check=True,
                                  stdout=subprocess.PIPE, text=False, env=environment).stdout.decode().strip()
            (noncommit / ".git" / "refs" / "heads" / "not-a-commit").write_text(
                blob + "\n", encoding="ascii")
            subprocess.run(["/usr/bin/git", "-C", str(noncommit), "symbolic-ref", "HEAD",
                            "refs/heads/not-a-commit"], check=True, env=environment)

            for repository in (nonrepository, unborn, noncommit):
                with self.subTest(repository=repository.name), self.assertRaisesRegex(
                        ValueError, r"cannot resolve checkout HEAD\^\{commit\}"):
                    cli.request_for(self.parse("install"), repository=repository)

    def test_git_failure_and_stderr_are_sanitized(self):
        failure = subprocess.CompletedProcess([], 128, stdout="", stderr="PRIVATE-GIT-DETAIL")
        with mock.patch.object(cli.subprocess, "run", return_value=failure):
            with self.assertRaisesRegex(ValueError, "cannot resolve checkout") as raised:
                cli._candidate_sha(pathlib.Path("/checkout"))
        self.assertNotIn("PRIVATE-GIT-DETAIL", str(raised.exception))

    def test_missing_git_and_timeout_have_sanitized_actionable_errors(self):
        with mock.patch.object(cli.subprocess, "run", side_effect=FileNotFoundError("PRIVATE-MISSING")):
            with self.assertRaisesRegex(ValueError, "fixed Git executable /usr/bin/git is unavailable") as raised:
                cli._candidate_sha(pathlib.Path("/checkout"))
        self.assertNotIn("PRIVATE-MISSING", str(raised.exception))

        timeout = subprocess.TimeoutExpired(
            [cli.GIT], cli.GIT_TIMEOUT_SECONDS, output="PRIVATE-OUTPUT", stderr="PRIVATE-STDERR")
        with mock.patch.object(cli.subprocess, "run", side_effect=timeout):
            with self.assertRaisesRegex(ValueError, "fixed Git timed out") as raised:
                cli._candidate_sha(pathlib.Path("/checkout"))
        self.assertNotIn("PRIVATE-OUTPUT", str(raised.exception))
        self.assertNotIn("PRIVATE-STDERR", str(raised.exception))

    def test_non_install_requests_do_not_resolve_head(self):
        with mock.patch.object(cli.subprocess, "run") as run:
            for argv in (("status",), ("up",), ("settings", "show"),
                         ("profiles", "list"), ("installations",),
                         ("prune", "--dry-run"), ("bootstrap",), ("control", "update")):
                with self.subTest(argv=argv):
                    cli.request_for(self.parse(*argv))
        run.assert_not_called()

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
