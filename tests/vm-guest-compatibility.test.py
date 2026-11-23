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


def _candidate_response():
    expected = compatibility.expected_inventory()
    inventory = {}
    for path, item in expected.items():
        if item["kind"] == "file":
            inventory[path] = {
                "kind": "file", "sha256": item["sha256"], "mode": item["mode"],
                "uid": item["uid"], "gid": item["gid"],
            }
        elif item["kind"] == "version-json":
            inventory[path] = {
                "kind": "version-json", "version": item["version"], "mode": item["mode"],
                "uid": item["uid"], "gid": item["gid"],
            }
    return expected, {"schema": 1, "inventory": inventory,
                      "mount": {"path": "/workspace", "fstype": "virtiofs", "source": "workspace"}}


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

    def test_opencode_package_json_contract_matches_and_fails_closed(self):
        expected, document = _candidate_response()
        package_path = compatibility.OPENCODE_PACKAGE_JSON
        status, diagnostics = _classify(document, expected)
        self.assertEqual((status, diagnostics), ("MATCH", ()))

        for field, wrong in (
            ("mode", 0o755),
            ("version", "2.2.4"),
            ("uid", 1000),
            ("gid", 1000),
        ):
            with self.subTest(field=field):
                altered = json.loads(json.dumps(document))
                altered["inventory"][package_path][field] = wrong
                status, diagnostics = _classify(altered, expected)
                self.assertEqual(status, "MISMATCH")
                self.assertIn("asset-14:mismatch", diagnostics)

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

    def test_failure_evidence_reports_only_expected_and_observed_target_fields(self):
        expected, document = _candidate_response()
        package = document["inventory"][compatibility.OPENCODE_PACKAGE_JSON]
        package.update({"version": "2.2.4", "mode": 0o600, "uid": 1000, "gid": 1000})
        document["mount"].update({"fstype": "9p", "source": "/run/private/secret-token"})
        document["inventory"]["/home/mester/.local/share/opencode/auth.json"] = {
            "kind": "file", "sha256": "auth-content-sentinel", "mode": 0o600,
            "uid": 1000, "gid": 1000,
        }

        line = compatibility.format_failure_evidence(
            json.dumps(document, separators=(",", ":")).encode(), expected,
            cloud_init_ready_wait_ms=42)

        self.assertIn(
            "asset-14 expected{present=yes,regular=yes,version=2.2.5,mode=0644,uid=0,gid=0}", line)
        self.assertIn(
            "observed{present=yes,regular=yes,version=2.2.4,mode=0600,uid=1000,gid=1000,category=mismatch}",
            line)
        self.assertIn(
            "workspace-mount expected{record=yes,fstype=virtiofs,source=workspace}", line)
        self.assertIn("observed{record=yes,fstype=9p,source=other,category=mismatch}", line)
        self.assertIn("cloud-init-check=success readiness-wait-ms=42", line)
        self.assertNotIn("/run/private/secret-token", line)
        self.assertNotIn("auth.json", line)
        self.assertNotIn("auth-content-sentinel", line)
        self.assertNotIn("sha256", line)
        self.assertLessEqual(len(line.encode()), compatibility.MAX_FAILURE_EVIDENCE_BYTES)

    def test_failure_evidence_reports_package_read_categories_without_inference(self):
        expected, base = _candidate_response()
        cases = (
            ({"kind": "missing"}, "present=no,regular=no", "category=missing"),
            ({"kind": "special"}, "present=yes,regular=no", "category=special"),
            ({"kind": "unavailable"}, "present=unknown,regular=unknown", "category=unavailable"),
            ({"kind": "oversize"}, "present=yes,regular=yes", "category=oversize"),
            ({"kind": "invalid-version"}, "present=yes,regular=yes", "category=invalid-version"),
        )
        for observation, presence, category in cases:
            with self.subTest(observation=observation):
                document = json.loads(json.dumps(base))
                document["inventory"][compatibility.OPENCODE_PACKAGE_JSON] = observation
                line = compatibility.format_failure_evidence(
                    json.dumps(document, separators=(",", ":")).encode(), expected)
                self.assertIn(presence, line)
                self.assertIn(category, line)

        document = json.loads(json.dumps(base))
        del document["inventory"][compatibility.OPENCODE_PACKAGE_JSON]
        line = compatibility.format_failure_evidence(
            json.dumps(document, separators=(",", ":")).encode(), expected)
        self.assertIn("present=unknown,regular=unknown", line)
        self.assertIn("category=not-reported", line)

    def test_failure_evidence_bounds_malformed_responses_and_cloud_init_timing(self):
        expected, _document = _candidate_response()
        invalid = compatibility.format_failure_evidence(b'{"secret":"raw-response"}', expected)
        self.assertIn("category=response-schema", invalid)
        self.assertNotIn("raw-response", invalid)
        oversized = compatibility.format_failure_evidence(
            b"x" * (compatibility.MAX_RESPONSE_BYTES + 1), expected)
        self.assertIn("category=response-too-large", oversized)
        self.assertNotIn("x" * 32, oversized)

        document = {"schema": 1, "inventory": {},
                    "mount": {"path": "/workspace", "fstype": "virtiofs", "source": "workspace"}}
        no_timing = compatibility.format_failure_evidence(
            json.dumps(document).encode(), expected)
        invalid_timing = compatibility.format_failure_evidence(
            json.dumps(document).encode(), expected, cloud_init_ready_wait_ms=True)
        self.assertNotIn("cloud-init-check", no_timing)
        self.assertNotIn("cloud-init-check", invalid_timing)

    def test_failure_evidence_bounds_mount_categories_and_untrusted_fields(self):
        expected, base = _candidate_response()
        cases = (
            ({"path": "/workspace", "fstype": "missing", "source": ""},
             "record=no,fstype=missing,source=empty,category=missing"),
            ({"path": "/workspace", "fstype": "unavailable", "source": ""},
             "record=unknown,fstype=unavailable,source=unknown,category=unavailable"),
            ({"path": "/private/secret", "fstype": "virtiofs", "source": "workspace"},
             "record=no,fstype=virtiofs,source=workspace,category=mismatch"),
        )
        for mount, expected_text in cases:
            with self.subTest(mount=mount):
                document = json.loads(json.dumps(base))
                document["mount"] = mount
                line = compatibility.format_failure_evidence(
                    json.dumps(document, separators=(",", ":")).encode(), expected)
                self.assertIn(expected_text, line)
                self.assertNotIn("/private/secret", line)

        document = json.loads(json.dumps(base))
        document["mount"]["source"] = "source\nwith-secret"
        document["mount"]["fstype"] = "bad/type"
        document["inventory"][compatibility.OPENCODE_PACKAGE_JSON]["version"] = "2.2.4\nsecret"
        line = compatibility.format_failure_evidence(
            json.dumps(document, separators=(",", ":")).encode(), expected)
        self.assertIn("version=unknown", line)
        self.assertIn("fstype=other,source=other,category=invalid-observation", line)
        self.assertNotIn("with-secret", line)
        self.assertNotIn("bad/type", line)
        self.assertNotIn("2.2.4", line)


if __name__ == "__main__":
    unittest.main()
