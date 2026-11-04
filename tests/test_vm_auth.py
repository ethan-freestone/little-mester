"""Fixture-only auth compatibility, bridge-adapter, and escrow policy tests."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTH = ROOT / "lib/vm/auth.sh"
SYNTHETIC_SECRET = "synthetic-auth-canary-7f4b1d"


def run_auth(script: str, *args: str, env: dict[str, str] | None = None):
    return subprocess.run(
        ["bash", "-c", 'source "$1"; shift; ' + script, "_", str(AUTH), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        check=False,
    )


def exported_credential(secret: str = SYNTHETIC_SECRET, integration: str = "openai"):
    return [{
        "id": "fixture-credential-id",
        "integrationID": integration,
        "label": "fixture-account",
        "active": True,
        "value": {"type": "oauth", "methodID": "fixture-oauth", "access": secret,
                  "refresh": "synthetic-refresh-canary", "expires": 4102444800000},
    }]


class AuthCompatibilityTests(unittest.TestCase):
    def test_only_reviewed_opencode_version_is_auth_compatible(self):
        supported = run_auth(
            'vm_auth_version_supported "$1"', "2.2.5"
        )
        self.assertEqual(supported.returncode, 0, supported.stderr)
        for version in ("2.2.4", "2.3.0", "latest", "", "v2.2.5-local"):
            with self.subTest(version=version):
                result = run_auth('vm_auth_version_supported "$1"', version)
                self.assertNotEqual(result.returncode, 0)
                if version:
                    self.assertNotIn(version, result.stdout + result.stderr)

    def test_auth_selectors_are_scoped_and_unsupported_adapters_fail_closed(self):
        off = run_auth('vm_auth_resolve_providers "$1" "$2"', "off", "2.2.5")
        auto = run_auth('vm_auth_resolve_providers "$1" "$2"', "auto", "2.2.5")
        explicit = run_auth('vm_auth_resolve_providers "$1" "$2"', "openai", "2.2.5")
        self.assertEqual(off.returncode, 0, off.stderr)
        self.assertEqual(off.stdout.strip(), "")
        self.assertEqual(auto.returncode, 0, auto.stderr)
        self.assertEqual(auto.stdout.splitlines(), ["openai", "anthropic", "kimi"])
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        self.assertEqual(explicit.stdout.splitlines(), ["openai"])
        for provider in ("anthropic", "kimi"):
            with self.subTest(provider=provider):
                selected = run_auth('vm_auth_resolve_providers "$1" "$2"', provider, "2.2.5")
                self.assertEqual(selected.returncode, 0, selected.stderr)
                self.assertEqual(selected.stdout.splitlines(), [provider])
        refused = run_auth('vm_auth_resolve_providers "$1" "$2"', "unknown", "2.2.5")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("unsupported", refused.stderr.lower())

    def test_export_validation_requires_only_selected_integration_and_never_prints_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "export.json"
            path.write_text(json.dumps(exported_credential()), encoding="utf-8")
            path.chmod(0o600)
            valid = run_auth('vm_auth_validate_export "$1" "$2"', "openai", str(path))
            self.assertEqual(valid.returncode, 0, valid.stderr)
            self.assertNotIn(SYNTHETIC_SECRET, valid.stdout + valid.stderr)

            for content in (
                "not-json",
                "{}",
                "[]garbage",
                json.dumps(exported_credential(integration="anthropic")),
                json.dumps([{"id": "x", "integrationID": "openai", "active": "yes"}]),
                '[{"id":"first","id":"second","integrationID":"openai","label":"x","active":true,"value":{"type":"key","key":"synthetic"}}]',
            ):
                path.write_text(content, encoding="utf-8")
                with self.subTest(content=content[:12]):
                    invalid = run_auth('vm_auth_validate_export "$1" "$2"', "openai", str(path))
                    self.assertNotEqual(invalid.returncode, 0)
                    self.assertNotIn(SYNTHETIC_SECRET, invalid.stdout + invalid.stderr)

    def test_atomic_export_retains_old_escrow_on_invalid_input_and_sets_private_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "secrets"
            directory.mkdir(mode=0o700)
            directory.chmod(0o700)
            source = Path(temporary) / "guest-export.json"
            target = directory / "openai.json"
            source.touch(mode=0o600)
            source.chmod(0o600)
            target.write_text('{"old":"last-known-good"}\n', encoding="utf-8")
            target.chmod(0o600)

            source.write_text("{invalid", encoding="utf-8")
            rejected = run_auth(
                'vm_auth_atomic_export "$1" "$2" "$3"', str(source), str(target), "openai"
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("last-known-good", target.read_text(encoding="utf-8"))

            source.write_text(json.dumps(exported_credential()), encoding="utf-8")
            accepted = run_auth(
                'vm_auth_atomic_export "$1" "$2" "$3"', str(source), str(target), "openai"
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), exported_credential())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(target.stat().st_uid, os.getuid())
            self.assertNotIn(SYNTHETIC_SECRET, accepted.stdout + accepted.stderr)
            self.assertEqual({item.name for item in directory.iterdir()}, {"openai.json"})

    def test_escrow_path_rejects_symlinks_permissive_modes_and_workspace_overlap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            workspace = root / "workspace"
            secrets = root / "secrets"
            repository.mkdir()
            workspace.mkdir()
            secrets.mkdir(mode=0o700)
            secrets.chmod(0o700)
            valid = run_auth(
                'vm_auth_escrow_path "$1" "$2" "$3" "$4"',
                str(secrets), "openai", str(workspace), str(repository),
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)
            self.assertEqual(Path(valid.stdout.strip()), secrets / "openai.json")

            permissive = run_auth(
                'vm_auth_escrow_path "$1" "$2" "$3" "$4"',
                str(secrets), "openai", str(workspace), str(repository),
                env={},
            )
            self.assertEqual(permissive.returncode, 0, permissive.stderr)
            secrets.chmod(0o755)
            denied_mode = run_auth(
                'vm_auth_escrow_path "$1" "$2" "$3" "$4"',
                str(secrets), "openai", str(workspace), str(repository),
            )
            self.assertNotEqual(denied_mode.returncode, 0)

            secrets.chmod(0o700)
            workspace_secrets = workspace / "private"
            workspace_secrets.mkdir(mode=0o700)
            overlap = run_auth(
                'vm_auth_escrow_path "$1" "$2" "$3" "$4"',
                str(workspace_secrets), "openai", str(workspace), str(repository),
            )
            self.assertNotEqual(overlap.returncode, 0)

            link = root / "secret-link"
            link.symlink_to(secrets, target_is_directory=True)
            symlink = run_auth(
                'vm_auth_escrow_path "$1" "$2" "$3" "$4"',
                str(link), "openai", str(workspace), str(repository),
            )
            self.assertNotEqual(symlink.returncode, 0)

    def test_escrow_directory_creation_is_private_and_rejects_workspace_before_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            workspace = root / "workspace"
            repository.mkdir(mode=0o700)
            workspace.mkdir(mode=0o700)
            safe = root / "secrets"
            created = run_auth(
                'vm_auth_ensure_escrow_dir "$1" "$2" "$3"',
                str(safe), str(workspace), str(repository),
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertEqual(stat.S_IMODE(safe.stat().st_mode), 0o700)

            unsafe = workspace / "secrets"
            refused = run_auth(
                'vm_auth_ensure_escrow_dir "$1" "$2" "$3"',
                str(unsafe), str(workspace), str(repository),
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertFalse(unsafe.exists())

    def test_import_is_scoped_once_and_transfers_credentials_only_over_stdin(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            profile = root / "vm/profiles/fixture"
            profile.mkdir(parents=True, mode=0o700)
            escrow = root / "secrets"
            escrow.mkdir(mode=0o700)
            escrow.chmod(0o700)
            credential_file = escrow / "openai.json"
            credential_file.write_text(json.dumps(exported_credential()), encoding="utf-8")
            credential_file.chmod(0o600)
            capture = Path(temporary) / "stdin.capture"
            calls = Path(temporary) / "calls.log"
            data = json.dumps({"status": "ready", "opencode_version": "2.2.5"})
            script = r'''
vm_auth_guest_command() {
  printf '%s\n' "$4" >>"$CALLS"
  cat >"$CAPTURE"
}
vm_auth_import_if_needed "$ROOT" fixture "$DATA" auto 2.2.5 "$ESCROW" "$WORKSPACE" "$REPO"
'''
            env = {
                "ROOT": str(root), "DATA": data, "ESCROW": str(escrow),
                "WORKSPACE": str(root / "workspace"), "REPO": str(ROOT),
                "CAPTURE": str(capture), "CALLS": str(calls),
            }
            first = run_auth(script, env=env)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(capture.read_text(encoding="utf-8"), credential_file.read_text(encoding="utf-8"))
            self.assertEqual(calls.read_text(encoding="utf-8").splitlines(), ["opencode auth import"])
            marker = profile / "auth-imported-openai"
            self.assertTrue(marker.is_file())
            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
            self.assertNotIn(SYNTHETIC_SECRET, marker.read_text(encoding="utf-8"))

            second = run_auth(script, env=env)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(calls.read_text(encoding="utf-8").splitlines(), ["opencode auth import"])
            off = run_auth(
                'vm_auth_import_if_needed "$1" fixture "$2" off 2.2.5 "$3" "$4" "$5"',
                str(root), data, str(escrow), str(root / "workspace"), str(ROOT),
            )
            self.assertEqual(off.returncode, 0, off.stderr)

    def test_auto_skips_absent_escrow_but_explicit_selector_refuses(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            profile = root / "vm/profiles/fixture"
            profile.mkdir(parents=True, mode=0o700)
            escrow = root / "secrets"
            escrow.mkdir(mode=0o700)
            data = json.dumps({"status": "ready", "opencode_version": "2.2.5"})
            calls = Path(temporary) / "calls.log"
            script = r'''
vm_auth_guest_command() { printf called >>"$CALLS"; }
vm_auth_import_if_needed "$ROOT" fixture "$DATA" "$SELECTOR" 2.2.5 "$ESCROW" "$WORKSPACE" "$REPO"
'''
            common = {
                "ROOT": str(root), "DATA": data, "ESCROW": str(escrow),
                "WORKSPACE": str(root / "workspace"), "REPO": str(ROOT),
                "CALLS": str(calls),
            }
            automatic = run_auth(script, env={**common, "SELECTOR": "auto"})
            self.assertEqual(automatic.returncode, 0, automatic.stderr)
            self.assertFalse(calls.exists())
            explicit = run_auth(script, env={**common, "SELECTOR": "openai"})
            self.assertNotEqual(explicit.returncode, 0)
            self.assertFalse(calls.exists())

    def test_import_validates_every_auto_escrow_before_mutating_guest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            profile = root / "vm/profiles/fixture"
            profile.mkdir(parents=True, mode=0o700)
            escrow = root / "secrets"
            escrow.mkdir(mode=0o700)
            openai = escrow / "openai.json"
            openai.write_text(json.dumps(exported_credential()), encoding="utf-8")
            openai.chmod(0o600)
            anthropic = escrow / "anthropic.json"
            anthropic.write_text('{"invalid":"later-provider"}', encoding="utf-8")
            anthropic.chmod(0o600)
            calls = Path(temporary) / "calls.log"
            data = json.dumps({"status": "ready", "opencode_version": "2.2.5"})
            result = run_auth(
                r'''
vm_auth_guest_command() { printf called >>"$CALLS"; cat >/dev/null; }
vm_auth_import_if_needed "$ROOT" fixture "$DATA" auto 2.2.5 "$ESCROW" "$WORKSPACE" "$REPO"
''',
                env={
                    "ROOT": str(root), "DATA": data, "ESCROW": str(escrow),
                    "WORKSPACE": str(root / "workspace"), "REPO": str(ROOT),
                    "CALLS": str(calls),
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(calls.exists())
            self.assertFalse((profile / "auth-imported-openai").exists())

    def test_stopped_guest_is_not_woken_for_export_and_old_escrow_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            profile = root / "vm/profiles/fixture"
            profile.mkdir(parents=True, mode=0o700)
            escrow = root / "secrets"
            escrow.mkdir(mode=0o700)
            target = escrow / "openai.json"
            target.write_text('{"old":"last-known-good"}\n', encoding="utf-8")
            target.chmod(0o600)
            calls = Path(temporary) / "calls.log"
            data = json.dumps({"status": "suspended", "opencode_version": "2.2.5"})
            result = run_auth(
                r'''
vm_auth_guest_export() { printf called >>"$CALLS"; printf '%s' "$SYNTHETIC"; }
vm_auth_export_before_delete "$ROOT" fixture "$DATA" auto 2.2.5 "$ESCROW" "$WORKSPACE" "$REPO"
''',
                env={
                    "ROOT": str(root), "DATA": data, "ESCROW": str(escrow),
                    "WORKSPACE": str(root / "workspace"), "REPO": str(ROOT),
                    "CALLS": str(calls), "SYNTHETIC": SYNTHETIC_SECRET,
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(calls.exists())
            self.assertIn("last-known-good", target.read_text(encoding="utf-8"))
            self.assertNotIn(SYNTHETIC_SECRET, result.stdout + result.stderr)

    def test_export_failure_does_not_replace_escrow_or_emit_synthetic_secret(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            profile = root / "vm/profiles/fixture"
            profile.mkdir(parents=True, mode=0o700)
            escrow = root / "secrets"
            escrow.mkdir(mode=0o700)
            target = escrow / "openai.json"
            target.write_text('{"old":"last-known-good"}\n', encoding="utf-8")
            target.chmod(0o600)
            data = json.dumps({"status": "ready", "opencode_version": "2.2.5"})
            result = run_auth(
                r'''
vm_lifecycle_identity_state() { printf running; }
vm_auth_guest_export() { printf '[{"bad":"%s"}]' "$SYNTHETIC"; }
vm_auth_export_before_delete "$ROOT" fixture "$DATA" openai 2.2.5 "$ESCROW" "$WORKSPACE" "$REPO"
''',
                env={
                    "ROOT": str(root), "DATA": data, "ESCROW": str(escrow),
                    "WORKSPACE": str(root / "workspace"), "REPO": str(ROOT),
                    "SYNTHETIC": SYNTHETIC_SECRET,
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("last-known-good", target.read_text(encoding="utf-8"))
            self.assertNotIn(SYNTHETIC_SECRET, result.stdout + result.stderr)

    def test_auto_export_stages_all_selected_providers_before_replacing_escrow(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            profile = root / "vm/profiles/fixture"
            profile.mkdir(parents=True, mode=0o700)
            marker = profile / "auth-imported-anthropic"
            marker.write_text("v1:anthropic:2.2.5\n", encoding="ascii")
            marker.chmod(0o600)
            escrow = root / "secrets"
            escrow.mkdir(mode=0o700)
            target = escrow / "openai.json"
            old = '[{"old":"last-known-good"}]\n'
            target.write_text(old, encoding="utf-8")
            target.chmod(0o600)
            data = json.dumps({"status": "ready", "opencode_version": "2.2.5"})
            result = run_auth(
                r'''
vm_lifecycle_identity_state() { printf running; }
vm_auth_guest_export() {
  if [[ "$4" == openai ]]; then printf '[]\n'; return 0; fi
  printf '%s' "$SYNTHETIC"
  return 7
}
vm_auth_export_before_delete "$ROOT" fixture "$DATA" auto 2.2.5 "$ESCROW" "$WORKSPACE" "$REPO"
''',
                env={
                    "ROOT": str(root), "DATA": data, "ESCROW": str(escrow),
                    "WORKSPACE": str(root / "workspace"), "REPO": str(ROOT),
                    "SYNTHETIC": SYNTHETIC_SECRET,
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target.read_text(encoding="utf-8"), old)
            self.assertEqual({item.name for item in escrow.iterdir()}, {"openai.json"})
            self.assertNotIn(SYNTHETIC_SECRET, result.stdout + result.stderr)

    def test_auto_export_refuses_a_corrupt_import_marker_and_preserves_prior_escrow(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            profile = root / "vm/profiles/fixture"
            profile.mkdir(parents=True, mode=0o700)
            marker = profile / "auth-imported-anthropic"
            marker.write_text("corrupt-marker\n", encoding="ascii")
            marker.chmod(0o600)
            escrow = root / "secrets"
            escrow.mkdir(mode=0o700)
            target = escrow / "openai.json"
            old = '[{"old":"last-known-good"}]\n'
            target.write_text(old, encoding="utf-8")
            target.chmod(0o600)
            calls = Path(temporary) / "exports.log"
            data = json.dumps({"status": "ready", "opencode_version": "2.2.5"})
            result = run_auth(
                r'''
vm_lifecycle_identity_state() { printf running; }
vm_auth_guest_export() { printf '%s\n' "$4" >>"$CALLS"; printf '[]\n'; }
vm_auth_export_before_delete "$ROOT" fixture "$DATA" auto 2.2.5 "$ESCROW" "$WORKSPACE" "$REPO"
''',
                env={
                    "ROOT": str(root), "DATA": data, "ESCROW": str(escrow),
                    "WORKSPACE": str(root / "workspace"), "REPO": str(ROOT),
                    "CALLS": str(calls),
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target.read_text(encoding="utf-8"), old)
            self.assertEqual(calls.read_text(encoding="utf-8").splitlines(), ["openai"])
            self.assertEqual({item.name for item in escrow.iterdir()}, {"openai.json"})

    def test_remote_agent_command_uses_runtime_key_files_and_private_bridge_paths(self):
        result = subprocess.run(
            ["bash", "-c", r'''
source "$1/lib/vm/services.sh"
VM_SERVICES_AGENT_ARGV=(opencode)
VM_SERVICES_AGENT_ENV=()
VM_SERVICES_AUTH_KEY_PROVIDERS=(anthropic kimi)
VM_SERVICES_BROWSER_DIR=/tmp/lm-vm-auth.abcdef
VM_SERVICES_BROWSER_PORT=43123
vm_services_build_remote_command opencode . opencode
printf '%s\n' "$VM_SERVICES_REMOTE_COMMAND"
''', "_", str(ROOT)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        command = result.stdout
        self.assertIn("VM_BROWSER_BRIDGE_TOKEN_FILE=/tmp/lm-vm-auth.abcdef/token", command)
        self.assertIn("VM_BROWSER_BRIDGE_PORT=43123", command)
        self.assertIn("ANTHROPIC_API_KEY=\"$vm_auth_runtime_key\"", command)
        self.assertIn("MOONSHOT_API_KEY=\"$vm_auth_runtime_key\"", command)
        self.assertIn("anthropic.json", command)
        self.assertIn("kimi.json", command)
        self.assertNotIn(SYNTHETIC_SECRET, command)
        self.assertNotIn("OPENAI_API_KEY=", command)

    def test_runtime_key_adapter_installs_only_private_validated_synthetic_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "guest-home"
            home.mkdir(mode=0o700)
            payload = json.dumps({"schema_version": 1, "provider": "anthropic",
                                  "api_key": SYNTHETIC_SECRET})
            result = subprocess.run(
                ["bash", "-c", r'''
source "$1/lib/vm/auth.sh"
command="$(vm_auth_key_install_command anthropic)" || exit $?
printf '%s' "$PAYLOAD" | HOME="$GUEST_HOME" bash -c "$command"
''', "_", str(ROOT)],
                env={**os.environ, "GUEST_HOME": str(home), "PAYLOAD": payload},
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            target = home / ".config/opencode/little-mester-auth/anthropic.json"
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), json.loads(payload))
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)
            self.assertNotIn(SYNTHETIC_SECRET, result.stdout + result.stderr)

    def test_browser_session_rejects_unreviewed_profile_before_starting_resources(self):
        profile = json.dumps({"opencode_version": "2.2.4"})
        result = subprocess.run(
            ["bash", "-c", r'''
source "$1/lib/vm/services.sh"
source "$1/lib/vm/auth.sh"
vm_services_profile_selection() {
  VM_SERVICES_SELECTED_AGENT=opencode
  VM_SERVICES_SELECTED_PROFILE_DATA=$PROFILE
}
vm_lifecycle_field() { python3 -c 'import json,sys; print(json.loads(sys.argv[1])[sys.argv[2]])' "$1" "$2"; }
export VM_SERVICES_ROOT=$1
vm_services_agent /unused fixture ask "" on
''', "_", str(ROOT)],
            env={**os.environ, "PROFILE": profile},
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("reviewed OpenCode v2.2.5", result.stderr)
        self.assertNotIn("Host browser MCP bridge listening", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
