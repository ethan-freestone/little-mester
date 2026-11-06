"""Fixture-only tests for the bounded redacting synthetic-canary checker."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/vm-canary-check.py"
SPEC = importlib.util.spec_from_file_location("vm_canary_check", SCRIPT)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)

CANARY = b"synthetic-canary-fixture-0123456789"


class CanaryCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lm-canary-check-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)

    def artifact(self, artifact_id, path, kind="file"):
        return {"id": artifact_id, "path": str(path), "kind": kind}

    def manifest(self, artifacts, required=None):
        return {"schema_version": 1,
                "required_ids": required if required is not None else [item["id"] for item in artifacts],
                "artifacts": artifacts}

    def scan(self, artifacts, required=None, roots=None):
        return checker.scan_manifest(
            CANARY, self.manifest(artifacts, required), roots or [self.root],
        )

    def test_positive_raw_and_encoded_matches_are_redacted(self):
        path = self.root / "fixture.log"
        path.write_bytes(b"prefix\n" + CANARY.hex().encode() + b"\nsuffix\n")
        path.chmod(0o600)
        result = self.scan([self.artifact("campaign_log", path)])
        serialized = json.dumps(result, sort_keys=True)
        self.assertEqual(result["result"], "FAIL")
        self.assertEqual(result["checks"][0]["reason_code"], "canary_present")
        self.assertNotIn(CANARY.decode(), serialized)
        self.assertNotIn(CANARY.hex(), serialized)
        self.assertNotIn(path.read_text(), serialized)

    def test_negative_scan_reports_only_digest_and_fixed_metadata(self):
        path = self.root / "report.json"
        path.write_bytes(b'{"result":"BLOCKED"}\n')
        path.chmod(0o600)
        result = self.scan([self.artifact("campaign_report", path)])
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["checks"][0]["artifact_sha256"],
                         hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertNotIn("contents", result["checks"][0])

    def test_streaming_matcher_detects_value_split_across_read_chunks(self):
        path = self.root / "split.log"
        path.write_bytes(b"x" * 5 + CANARY + b"y" * 5)
        path.chmod(0o600)
        with mock.patch.object(checker, "CHUNK_BYTES", 7):
            result = self.scan([self.artifact("split_log", path)])
        self.assertEqual(result["checks"][0]["status"], "FAIL")

    def test_tree_scans_all_regular_files_and_rejects_escaping_symlink(self):
        tree = self.root / "tree"
        tree.mkdir(mode=0o700)
        nested = tree / "nested"
        nested.mkdir(mode=0o700)
        leak = nested / "payload"
        leak.write_bytes(b"prefix" + CANARY + b"suffix")
        leak.chmod(0o600)
        self.assertEqual(self.scan([self.artifact("tree", tree, "tree")])["result"], "FAIL")

        outside = self.root / "outside"
        outside.write_bytes(b"outside")
        outside.chmod(0o600)
        (tree / "escape").symlink_to(outside)
        blocked = self.scan([self.artifact("tree", tree, "tree")])
        self.assertEqual(blocked["result"], "BLOCKED")
        self.assertEqual(blocked["checks"][0]["reason_code"], "unsafe_symlink")

    def test_required_coverage_missing_or_unsupported_stays_blocked(self):
        image = self.root / "base.qcow2"
        image.write_bytes(b"fixture image")
        image.chmod(0o600)
        incomplete = self.scan([], required=["snapshot", "seed"])
        self.assertEqual(incomplete["result"], "BLOCKED")
        self.assertEqual([item["reason_code"] for item in incomplete["checks"]],
                         ["manifest_incomplete", "manifest_incomplete"])
        opaque = self.scan([self.artifact("base_image", image, "opaque-qcow2")])
        self.assertEqual(opaque["result"], "BLOCKED")
        self.assertEqual(opaque["checks"][0]["reason_code"], "qcow2_content_not_inspected")
        image.write_bytes(CANARY)
        image.chmod(0o600)
        raw_match = self.scan([self.artifact("base_image", image, "opaque-qcow2")])
        self.assertEqual(raw_match["result"], "FAIL")
        self.assertEqual(raw_match["checks"][0]["reason_code"], "canary_present")

    def test_empty_required_artifact_set_is_not_a_vacuous_pass(self):
        with self.assertRaises(checker.CanaryCheckError):
            checker.scan_manifest(
                CANARY, {"schema_version": 1, "required_ids": [], "artifacts": []},
                [self.root],
            )

    def test_unsafe_file_modes_hardlinks_and_paths_are_blocked(self):
        path = self.root / "artifact"
        path.write_bytes(b"no secret")
        path.chmod(0o664)
        unsafe_mode = self.scan([self.artifact("file", path)])
        self.assertEqual(unsafe_mode["result"], "BLOCKED")
        self.assertEqual(unsafe_mode["checks"][0]["reason_code"], "unsafe_file")

        path.chmod(0o600)
        link = self.root / "hardlink"
        os.link(path, link)
        hardlink = self.scan([self.artifact("file", path)])
        self.assertEqual(hardlink["result"], "BLOCKED")

        outside = self.root.parent / (self.root.name + "-outside")
        outside.write_bytes(b"outside")
        outside.chmod(0o600)
        try:
            escaped = self.scan([self.artifact("outside", outside)], roots=[self.root])
            self.assertEqual(escaped["result"], "BLOCKED")
        finally:
            outside.unlink(missing_ok=True)

    def test_iso9660_seed_raw_content_is_scanned(self):
        path = self.root / "seed.iso"
        payload = bytearray(18 * 2048)
        payload[16 * 2048:16 * 2048 + 7] = b"\x01CD001\x01"
        payload[-len(CANARY):] = CANARY
        path.write_bytes(payload)
        path.chmod(0o600)
        result = self.scan([self.artifact("seed_iso", path, "iso9660")])
        self.assertEqual(result["result"], "FAIL")
        payload[-len(CANARY):] = b"\x00" * len(CANARY)
        path.write_bytes(payload)
        path.chmod(0o600)
        clean = self.scan([self.artifact("seed_iso", path, "iso9660")])
        self.assertEqual(clean["result"], "PASS")

    def test_git_loose_object_history_is_decompressed_and_scanned(self):
        git = self.root / ".git"
        objects = git / "objects"
        prefix = hashlib.sha1(b"blob " + str(len(CANARY)).encode() + b"\0" + CANARY).hexdigest()
        directory = objects / prefix[:2]
        directory.mkdir(mode=0o700, parents=True)
        git.chmod(0o700)
        objects.chmod(0o700)
        directory.chmod(0o700)
        object_path = directory / prefix[2:]
        body = b"blob " + str(len(CANARY)).encode() + b"\0" + CANARY
        object_path.write_bytes(zlib.compress(body))
        object_path.chmod(0o600)
        result = self.scan([self.artifact("history", git, "git-loose")])
        self.assertEqual(result["result"], "FAIL")
        self.assertNotIn(CANARY.decode(), json.dumps(result))

    def test_log_manifest_requires_logs_and_timeout_is_blocked(self):
        runs = self.root / "runs"
        runs.mkdir(mode=0o700)
        runs.chmod(0o700)
        run = runs / "0123456789abcdef0123456789abcdef"
        run.mkdir(mode=0o700, parents=True)
        run.chmod(0o700)
        log = run / "qemu_1.log"
        log.write_text("safe log\n", encoding="ascii")
        log.chmod(0o600)
        ok = self.scan([self.artifact("campaign_logs", runs, "logs")])
        self.assertEqual(ok["result"], "PASS")
        metadata = run / "processes.json"
        metadata.write_bytes(b'{"diagnostic":"' + CANARY + b'"}')
        metadata.chmod(0o600)
        leaked_metadata = self.scan([self.artifact("campaign_logs", runs, "logs")])
        self.assertEqual(leaked_metadata["result"], "FAIL")
        self.assertNotIn(CANARY.decode(), json.dumps(leaked_metadata))
        metadata.unlink()
        log.unlink()
        no_logs = self.scan([self.artifact("campaign_logs", runs, "logs")])
        self.assertEqual(no_logs["result"], "BLOCKED")
        self.assertEqual(no_logs["checks"][0]["reason_code"], "required_logs_missing")

        path = self.root / "slow"
        path.write_bytes(b"x")
        path.chmod(0o600)
        with mock.patch.object(checker.time, "monotonic", side_effect=[0.0, 100.0]):
            timeout = self.scan([self.artifact("slow", path)])
        self.assertEqual(timeout["result"], "BLOCKED")
        self.assertEqual(timeout["checks"][0]["reason_code"], "scan_timeout")


if __name__ == "__main__":
    unittest.main()
