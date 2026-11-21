"""Pure contract tests for the fixed guest compatibility inventory checker.

These tests intentionally use only local temporary fixtures. They do not connect
to or operate a VM. The production helper is planned at
``lib/vm/guest-compatibility.py``.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parent.parent
HELPER_PATH = ROOT / "lib/vm/guest-compatibility.py"
SPEC = importlib.util.spec_from_file_location("guest_compatibility", HELPER_PATH)
compatibility = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = compatibility
SPEC.loader.exec_module(compatibility)


def _entry(digest="a" * 64, *, kind="file", mode=0o644, uid=0, gid=0):
    return {
        "kind": kind,
        "sha256": digest,
        "mode": mode,
        "uid": uid,
        "gid": gid,
    }


def _expected():
    return {
        "/etc/opencode/opencode.json": _entry(),
        "/etc/opencode/plugins/little-mester-exit-confirm/package.json": _entry("b" * 64),
        "/etc/opencode/plugins/little-mester-exit-confirm/tui.tsx": _entry("c" * 64),
        "/workspace": {"kind": "mount", "fstype": "virtiofs", "source": "workspace"},
    }


def _response(expected=None):
    expected = _expected() if expected is None else expected
    assets = {path: value for path, value in expected.items() if value["kind"] != "mount"}
    return {
        "schema": 1,
        "inventory": assets,
        "mount": {"path": "/workspace", "fstype": "virtiofs", "source": "workspace"},
    }


def _classify(document, expected=None, *, max_bytes=8192):
    return compatibility.classify_inventory(
        json.dumps(document, separators=(",", ":")).encode(),
        _expected() if expected is None else expected,
        max_bytes=max_bytes,
    )


class GuestCompatibilityInventoryTests(unittest.TestCase):
    def test_exact_complete_inventory_matches(self):
        status, diagnostics = _classify(_response())
        self.assertEqual(status, "MATCH")
        self.assertIsInstance(diagnostics, tuple)

    def test_missing_expected_asset_is_mismatch(self):
        document = _response()
        del document["inventory"]["/etc/opencode/opencode.json"]
        self.assertEqual(_classify(document)[0], "MISMATCH")

    def test_wrong_digest_type_mode_uid_or_gid_is_mismatch(self):
        for field, wrong in (
            ("sha256", "d" * 64),
            ("kind", "directory"),
            ("mode", 0o600),
            ("uid", 1000),
            ("gid", 1000),
        ):
            with self.subTest(field=field):
                document = _response()
                document["inventory"]["/etc/opencode/opencode.json"][field] = wrong
                self.assertEqual(_classify(document)[0], "MISMATCH")

    def test_workspace_mount_mismatch_is_mismatch(self):
        document = _response()
        document["mount"]["fstype"] = "9p"
        self.assertEqual(_classify(document)[0], "MISMATCH")

    def test_malformed_incomplete_or_unexpected_schema_is_inconclusive(self):
        cases = (
            b"not-json",
            json.dumps({"schema": 1}).encode(),
            json.dumps({"schema": 1, "inventory": [], "mount": {}}).encode(),
            json.dumps({"schema": 1, "inventory": {}, "mount": {}, "extra": True}).encode(),
        )
        expected = _expected()
        for raw in cases:
            with self.subTest(raw=raw[:30]):
                status, _ = compatibility.classify_inventory(raw, expected)
                self.assertEqual(status, "INCONCLUSIVE")

    def test_unexpected_inventory_path_is_inconclusive_not_accepted(self):
        document = _response()
        document["inventory"]["/home/mester/.local/share/opencode/auth.json"] = _entry()
        self.assertEqual(_classify(document)[0], "INCONCLUSIVE")

    def test_oversized_response_is_inconclusive(self):
        raw = json.dumps(_response()).encode()
        status, _ = compatibility.classify_inventory(raw, _expected(), max_bytes=len(raw) - 1)
        self.assertEqual(status, "INCONCLUSIVE")

    def test_candidate_inventory_is_fixed_and_excludes_mutable_user_state(self):
        # Expected paths are fixed guest contract paths, not host fixture paths.
        expected = compatibility.expected_inventory("/workspace")

        self.assertIn("/workspace", expected)
        self.assertEqual(expected["/workspace"].get("kind"), "mount")
        serialized = json.dumps(expected, sort_keys=True)
        self.assertIn("/etc/opencode/opencode.json", serialized)
        self.assertIn("/etc/opencode/opencode-model.json", serialized)
        self.assertIn("/etc/opencode/plugins/little-mester-exit-confirm/", serialized)
        self.assertIn("/etc/opencode/plugins/little-mester-host-metrics-tui/", serialized)
        self.assertIn("/usr/local/lib/node_modules/@opencode/cli/package.json", serialized)
        self.assertEqual(expected[compatibility.OPENCODE_PACKAGE_JSON]["version"], "2.2.5")
        self.assertNotIn("/home/mester/.local/state/opencode/preferences.json", serialized)
        self.assertNotIn("/home/mester/.local/share/opencode/auth.json", serialized)
        self.assertNotIn("/home/mester/.local/share/opencode/opencode.db", serialized)
        self.assertNotIn("/home/mester/.local/share/opencode/sessions", serialized)
        self.assertNotIn("/workspace/", serialized)

    def test_expected_digests_are_candidate_derived_and_metadata_complete(self):
        expected = compatibility.expected_inventory("/workspace")

        for path, item in expected.items():
            with self.subTest(path=path):
                self.assertIsInstance(path, str)
                self.assertIn(item.get("kind"), ("file", "version-json", "mount"))
                if item["kind"] == "file":
                    self.assertRegex(item.get("sha256", ""), r"^[0-9a-f]{64}$")
                    for metadata in ("mode", "uid", "gid"):
                        self.assertIsInstance(item.get(metadata), int)
                elif item["kind"] == "version-json":
                    self.assertEqual(item.get("version"), "2.2.5")
                    for metadata in ("mode", "uid", "gid"):
                        self.assertIsInstance(item.get(metadata), int)

    def test_remote_command_is_fixed_python_inspection_not_runtime_launch(self):
        command = compatibility.build_inspection_command("/workspace")
        self.assertIsInstance(command, str)
        lowered = command.lower()
        self.assertIn("python3", lowered)
        self.assertIn("/workspace", command)
        # Immutable asset paths can contain these words. The inspection command
        # must not execute the application, launcher, or plugin code.
        self.assertNotIn("/usr/local/bin/opencode --version", lowered)
        self.assertNotRegex(lowered, r"(?:^|[\s;&|`])/?usr/local/bin/lm-opencode(?:\s|$)")
        self.assertNotRegex(lowered, r"(?:^|[;&|`\s])opencode(?:\s|$)")
        self.assertNotRegex(lowered, r"(?:sh|bash|python3)\s+[^\n]*(?:plugin|launcher)[^\n]*\.(?:js|ts|tsx)")
        self.assertIn("/etc/opencode/opencode.json", lowered)
        self.assertIn("/etc/opencode/plugins/", lowered)
        self.assertNotIn("$1", command)
        self.assertNotIn("{path}", command)
        self.assertNotIn("guest_path", lowered)
        self.assertEqual(compatibility.build_inspection_command("/workspace"), command)

    def test_command_contract_includes_no_follow_and_per_file_size_bound(self):
        # The command carries the fixed inspection program. Check semantic safety
        # primitives in that program rather than requiring a particular shell quote
        # layout or one exact command rendering.
        command = compatibility.build_inspection_command("/workspace")
        lowered = command.lower()
        no_follow_tokens = ("o_nofollow", "nofollow", "follow_symlinks=false")
        size_bound_tokens = ("max_file_bytes", "max_asset_bytes", "file_size_limit")
        self.assertTrue(any(token in lowered for token in no_follow_tokens))
        self.assertTrue(any(token in lowered for token in size_bound_tokens))


if __name__ == "__main__":
    unittest.main()
