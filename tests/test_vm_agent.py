"""Fixture-only native VM agent adapter and model-selection tests."""

from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "lib/vm/services.sh"
GUEST = ROOT / "lib/vm/guest.sh"
CONFIG_RENDERER = ROOT / "scripts/vm-opencode-config.py"

SPEC = importlib.util.spec_from_file_location("vm_agent_config", CONFIG_RENDERER)
vm_agent_config = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(vm_agent_config)


def adapter_argv(agent: str, permissions: str, provider: str, model_id: str, *args: str):
    result = subprocess.run(
        [
            "bash", "-c",
            'source "$1"; shift; vm_services_build_agent_argv "$@" || exit $?; '
            'printf "%s\\0" "${VM_SERVICES_AGENT_ARGV[@]}"; '
            'printf "\\1"; printf "%s\\0" "${VM_SERVICES_AGENT_ENV[@]}"',
            "_", str(SERVICES), agent, permissions, provider, model_id, *args,
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, result.args, stderr=result.stderr)
    argv_raw, _, env_raw = result.stdout.partition(b"\1")
    return argv_raw.decode().rstrip("\0").split("\0"), env_raw.decode().rstrip("\0").split("\0")


class NativeAgentAdapterTests(unittest.TestCase):
    def test_opencode_ask_and_auto_use_one_supported_flag_and_keep_args(self):
        args = ("--title", "two words", "task; do not split")
        ask, _ = adapter_argv("opencode", "ask", "none", "", *args)
        auto, _ = adapter_argv("opencode", "auto", "none", "", *args)
        self.assertEqual(ask, ["opencode", *args])
        self.assertEqual(auto, ["opencode", "--auto", *args])
        self.assertEqual(auto.count("--auto"), 1)

    def test_open_code_auto_override_in_agent_args_is_refused(self):
        for args in (("--auto",), ("--auto-accept",)):
            with self.subTest(args=args), self.assertRaises(subprocess.CalledProcessError):
                adapter_argv("opencode", "ask", "none", "", *args)

    def test_passthrough_cannot_override_provider_or_disable_aider_prompts(self):
        for agent, args in (
            ("opencode", ("--model", "ollama/qwen3:8b")),
            ("opencode", ("--cwd", "/etc")),
            ("aider", ("--model", "openai/gpt-4.1")),
            ("aider", ("--yes-always",)),
        ):
            with self.subTest(agent=agent, args=args), self.assertRaises(subprocess.CalledProcessError):
                adapter_argv(agent, "ask", "none", "", *args)

    def test_aider_provider_matrix_has_native_model_and_only_local_endpoint_env(self):
        cases = (
            (
                "ollama", "qwen3:14b", "ollama_chat/qwen3:14b",
                "OLLAMA_API_BASE=http://10.0.2.100:11434",
            ),
            (
                "llamacpp", "coder-q4", "openai/coder-q4",
                "OPENAI_API_BASE=http://10.0.2.100:8080/v1",
            ),
            ("remote", "openai/gpt-4.1", "openai/gpt-4.1", None),
            ("none", "", None, None),
        )
        for provider, model_id, expected_model, expected_env in cases:
            with self.subTest(provider=provider):
                argv, env = adapter_argv("aider", "ask", provider, model_id)
                self.assertEqual(argv[0], "/opt/little-mester/python-tools/bin/aider")
                if expected_model is None:
                    self.assertNotIn("--model", argv)
                else:
                    self.assertEqual(argv[1:3], ["--model", expected_model])
                endpoint_env = [value for value in env if "_API_BASE=" in value]
                self.assertEqual(endpoint_env, [] if expected_env is None else [expected_env])
                self.assertNotIn("--auto", argv)

    def test_aider_refuses_unsupported_auto_and_opencode_permission_flags(self):
        with self.assertRaises(subprocess.CalledProcessError):
            adapter_argv("aider", "auto", "none", "")
        for args in (("--auto",), ("--auto-accept",)):
            with self.subTest(args=args), self.assertRaises(subprocess.CalledProcessError):
                adapter_argv("aider", "ask", "none", "", *args)

    def test_opencode_local_remote_and_none_model_selection(self):
        source = {
            "provider": {
                "ollama": {
                    "npm": "@ai-sdk/openai-compatible",
                    "options": {"baseURL": "http://ollama:11434/v1"},
                    "models": {"qwen3:8b": {}},
                },
                "llamacpp": {
                    "npm": "@ai-sdk/openai-compatible",
                    "options": {"baseURL": "http://llamacpp:11434/v1"},
                    "models": {"coder-q4": {}},
                },
            },
            "model": "llamacpp/coder-q4",
            "small_model": "llamacpp/coder-q4",
            "permissions": [{"action": "edit", "resource": "secrets/*", "effect": "deny"}],
        }
        cases = (
            ("none", "", None, set()),
            ("ollama", "qwen3:8b", "ollama/qwen3:8b", {"ollama"}),
            ("ollama", "qwen3:14b", "ollama/qwen3:14b", {"ollama"}),
            ("llamacpp", "coder-q4", "llamacpp/coder-q4", {"llamacpp"}),
            ("remote", "openai/gpt-4.1", "openai/gpt-4.1", set()),
        )
        for provider, model_id, expected_model, expected_local in cases:
            with self.subTest(provider=provider):
                rendered = vm_agent_config.guest_config(
                    source, model_provider=provider, model_id=model_id
                )
                self.assertEqual(rendered.get("model"), expected_model)
                self.assertEqual(
                    {name for name in ("ollama", "llamacpp") if name in rendered["providers"]},
                    expected_local,
                )
                if provider == "ollama":
                    self.assertIn(model_id, rendered["providers"]["ollama"]["models"])
                self.assertIn(
                    {"action": "edit", "resource": "secrets/*", "effect": "deny"},
                    rendered["permissions"],
                )

    def test_guest_payload_binds_selected_provider_and_model(self):
        def digest(provider: str, port: str, model_id: str) -> str:
            result = subprocess.run(
                [
                    "bash", "-c", 'source "$1"; shift; vm_guest_payload_hash "$@"',
                    "_", str(GUEST), str(ROOT), "none", "opencode", "latest", "0", "0",
                    provider, port, model_id,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        baseline = digest("none", "", "")
        self.assertNotEqual(baseline, digest("ollama", "11434", "qwen3:8b"))
        self.assertNotEqual(baseline, digest("remote", "", "openai/gpt-4.1"))

    def test_aider_is_in_the_private_guest_tool_install_contract(self):
        installer = (ROOT / "scripts/vm-python-tools.sh").read_text(encoding="utf-8")
        bootstrap = (ROOT / "lib/vm/guest-bootstrap.sh").read_text(encoding="utf-8")
        self.assertIn("aider-chat", installer)
        self.assertIn("/opt/little-mester/python-tools/bin/aider", bootstrap)
        self.assertIn("@opencode/cli", bootstrap)
        self.assertNotIn("opencode-ai@", bootstrap)


if __name__ == "__main__":
    unittest.main()
