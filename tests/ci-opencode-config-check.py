#!/usr/bin/env python3
"""Check protected OpenCode config isolation with bounded failure diagnostics."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys


def failure_cause(stderr: str) -> str:
    """Map CLI stderr to fixed labels; never return or print the source text."""
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", stderr).lower()
    if re.search(
        r"eacces|eperm|erofs|permission denied|operation not permitted|read-only file system",
        text,
    ):
        return "filesystem-permission"
    if re.search(
        r"unknown (?:option|flag|command)|unrecognized (?:option|flag|argument)|unexpected argument",
        text,
    ):
        return "cli-invocation"
    if re.search(r"enoent|no such file or directory", text):
        return "missing-path"
    if "database is not empty and has no session table" in text:
        return "invalid-state-database"
    if re.search(r"database.{0,40}(?:locked|corrupt|malformed)", text):
        return "database-startup"
    if re.search(
        r"(?:invalid|malformed|parse error|failed to parse).{0,80}(?:config|json)"
        r"|(?:config|json).{0,80}(?:invalid|malformed|parse error)",
        text,
    ):
        return "invalid-config"
    if re.search(r"plugin.{0,80}(?:error|fail|cannot|unable)|(?:error|fail).{0,80}plugin", text):
        return "plugin-startup"
    if re.search(r"eaddrinuse|econnrefused|listen.{0,40}(?:error|fail)", text):
        return "server-startup"
    return "opencode-exit"


class ConfigCheckFailure(Exception):
    def __init__(self, phase: str, cause: str, exit_code: int):
        super().__init__(phase, cause, exit_code)
        self.phase = phase
        self.cause = cause
        self.exit_code = exit_code


def run_config_sources(
    binary: str, workspace: pathlib.Path, environment: dict[str, str], phase: str
) -> list[dict]:
    try:
        result = subprocess.run(
            [binary, "debug", "config", "--standalone", "--print-logs"],
            cwd=workspace,
            env=environment,
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ConfigCheckFailure(phase, "deadline-exceeded", 124) from None
    except OSError:
        raise ConfigCheckFailure(phase, "executable-or-permission", 126) from None

    if result.returncode:
        raise ConfigCheckFailure(
            phase, failure_cause(result.stderr), result.returncode
        )
    try:
        documents = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        raise ConfigCheckFailure(phase, "invalid-output", 65) from None
    if not isinstance(documents, list):
        raise ConfigCheckFailure(phase, "invalid-output", 65)
    return documents


def document_paths(documents: list[dict]) -> set[str]:
    return {
        item["path"]
        for item in documents
        if isinstance(item, dict)
        and item.get("type") == "document"
        and isinstance(item.get("path"), str)
    }


def make_environment(home: pathlib.Path, protected_config: str,
                     protected_plugin: str, *, protect_project_config: bool) -> dict[str, str]:
    cli_config = json.dumps(
        {"plugins": [protected_plugin],
         "keybinds": {"app.exit": "none", "prompt.clear": "none"}},
        separators=(",", ":"),
    )
    environment = {
        "HOME": str(home),
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "TERM": "xterm-256color",
        "LANG": "C.UTF-8",
        "XDG_DATA_HOME": str(home / ".local/share"),
        # Match the actual protected product runner, not the test harness's
        # former user-writable XDG config directory.
        "XDG_CONFIG_HOME": "/etc",
        "OPENCODE_CONFIG": protected_config,
        "OPENCODE_CLI_CONFIG_CONTENT": cli_config,
        "HTTP_PROXY": "http://10.0.2.100:3128",
        "HTTPS_PROXY": "http://10.0.2.100:3128",
        "http_proxy": "http://10.0.2.100:3128",
        "https_proxy": "http://10.0.2.100:3128",
        "NO_PROXY": "localhost,127.0.0.1,10.0.2.100",
        "no_proxy": "localhost,127.0.0.1,10.0.2.100",
    }
    if protect_project_config:
        environment["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
    return environment


def initialize_database(binary: str, home: pathlib.Path,
                        protected_config: str, protected_plugin: str) -> None:
    """Initialize the pinned OpenCode DB before adding CI synthetic history."""
    env = make_environment(
        home, protected_config, protected_plugin, protect_project_config=True
    )
    run_config_sources(binary, home, env, "database-bootstrap")


def check(binary: str, workspace: pathlib.Path, home: pathlib.Path,
          protected_config: str, protected_plugin: str) -> None:
    hostile_plugin = "/tmp/lm-ci-hostile-plugin-sentinel"
    hostile_paths = {
        str(workspace / "opencode.json"),
        str(workspace / ".opencode/opencode.json"),
    }
    base_environment = make_environment(
        home, protected_config, protected_plugin, protect_project_config=False
    )

    # Positive control: demonstrate these workspace config files and plugin path
    # are discoverable when the product protection switch is absent.
    enabled_sources = run_config_sources(
        binary, workspace, base_environment, "positive-control"
    )
    enabled_paths = document_paths(enabled_sources)
    if not hostile_paths <= enabled_paths or hostile_plugin not in json.dumps(enabled_sources):
        raise ConfigCheckFailure("positive-control", "hostile-fixture-not-discovered", 1)

    protected_environment = make_environment(
        home, protected_config, protected_plugin, protect_project_config=True
    )
    protected_sources = run_config_sources(
        binary, workspace, protected_environment, "protected-config"
    )
    protected_paths = document_paths(protected_sources)
    if hostile_paths & protected_paths or hostile_plugin in json.dumps(protected_sources):
        raise ConfigCheckFailure("protected-config", "hostile-config-not-suppressed", 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", default="/usr/local/bin/opencode")
    parser.add_argument("--workspace", type=pathlib.Path, required=True)
    parser.add_argument("--home", type=pathlib.Path, required=True)
    parser.add_argument("--protected-config", required=True)
    parser.add_argument("--protected-plugin", required=True)
    parser.add_argument("--initialize-database", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.initialize_database:
            initialize_database(
                args.binary, args.home,
                args.protected_config, args.protected_plugin,
            )
        else:
            check(args.binary, args.workspace, args.home,
                  args.protected_config, args.protected_plugin)
    except ConfigCheckFailure as error:
        print(
            "KVM_M2_CONFIG_FAIL "
            f"operation={'opencode-schema-bootstrap' if args.initialize_database else 'opencode-debug-config'} "
            f"phase={error.phase} "
            f"cause={error.cause} exit={error.exit_code}",
            file=sys.stderr,
        )
        return 44
    marker = (
        "KVM_M2_CONFIG_DB_PASS operation=opencode-schema-bootstrap"
        if args.initialize_database
        else "KVM_M2_CONFIG_PASS operation=opencode-debug-config"
    )
    print(marker)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
