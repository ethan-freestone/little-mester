"""Behavioral contract tests for the planned VM code-update transaction helper.

All filesystem paths under test are temporary fixtures. Logical target paths are
fixed guest paths and are never used as host paths.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
HELPER_PATH = ROOT / "lib/vm/code-update-transaction.py"
SPEC = importlib.util.spec_from_file_location("vm_code_update_transaction", HELPER_PATH)
transaction = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = transaction
SPEC.loader.exec_module(transaction)


CANDIDATE_SHA = "a" * 40
PREVIOUS_SHA = "b" * 40
TARGET_A = "/etc/opencode/opencode.json"
TARGET_B = "/etc/opencode/plugins/example/plugin.js"
TARGET_NEW = "/etc/opencode/plugins/example/new.js"
DIR_PRESENT = "/etc/opencode/plugins/example"
DIR_NEW = "/etc/opencode/plugins/new-plugin"


def _identity(path: pathlib.Path):
    info = path.lstat()
    return stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid


class CodeUpdateTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vm-code-update-")
        self.root = pathlib.Path(self.temp.name)
        self.a = self.root / "target-a"
        self.b = self.root / "target-b"
        self.new = self.root / "target-new"
        self.present_dir = self.root / "managed-present"
        self.new_dir = self.root / "managed-new"
        self.present_dir.mkdir(mode=0o751)
        self.a.write_bytes(b"old config\x00\n")
        self.b.write_bytes(b"old plugin\n")
        os.chmod(self.a, 0o640)
        os.chmod(self.b, 0o751)
        self.targets = {TARGET_A: self.a, TARGET_B: self.b, TARGET_NEW: self.new}
        uid, gid = os.getuid(), os.getgid()
        self.managed = {
            DIR_PRESENT: (self.present_dir, 0o751, uid, gid),
            DIR_NEW: (self.new_dir, 0o700, uid, gid),
        }
        self.snapshot = self.root / "snapshot.bin"
        self.expected = {
            TARGET_A: (b"candidate config\n", 0o600, uid, gid),
            TARGET_B: (b"candidate plugin\n", 0o600, uid, gid),
            TARGET_NEW: (b"candidate-only", 0o644, uid, gid),
        }

    def tearDown(self):
        self.temp.cleanup()

    def publish(self, *, candidate_sha=CANDIDATE_SHA, previous_sha=PREVIOUS_SHA):
        transaction.publish_snapshot(
            self.snapshot,
            self.targets,
            candidate_sha=candidate_sha,
            previous_sha=previous_sha,
            managed_dirs=self.managed,
        )

    def update_to_candidate(self):
        self.a.write_bytes(self.expected[TARGET_A][0])
        self.b.write_bytes(self.expected[TARGET_B][0])
        os.chmod(self.a, self.expected[TARGET_A][1])
        os.chmod(self.b, self.expected[TARGET_B][1])

    def restore(self, *, candidate_sha=CANDIDATE_SHA):
        transaction.restore_snapshot(
            self.snapshot,
            self.targets,
            self.expected,
            candidate_sha=candidate_sha,
            managed_dirs=self.managed,
        )

    def restore_preserving(self, paths):
        transaction.restore_snapshot(
            self.snapshot, self.targets, self.expected, candidate_sha=CANDIDATE_SHA,
            managed_dirs=self.managed, preserve_candidate_paths=frozenset(paths))

    def test_update_then_rollback_restores_bytes_mode_owner_and_inventory(self):
        old = {path: (target.read_bytes(), _identity(target)) for path, target in
               ((TARGET_A, self.a), (TARGET_B, self.b))}
        old_dir_identity = _identity(self.present_dir)
        self.publish()
        self.update_to_candidate()
        self.new.write_bytes(self.expected[TARGET_NEW][0])
        os.chmod(self.new, self.expected[TARGET_NEW][1])
        self.new_dir.mkdir(mode=0o700)
        os.chmod(self.new_dir, self.managed[DIR_NEW][1])

        self.restore()

        self.assertEqual((self.a.read_bytes(), _identity(self.a)), old[TARGET_A])
        self.assertEqual((self.b.read_bytes(), _identity(self.b)), old[TARGET_B])
        self.assertFalse(self.new.exists())
        self.assertFalse(self.new_dir.exists())
        self.assertEqual(_identity(self.present_dir), old_dir_identity)
        self.assertTrue(self.snapshot.exists(), "rollback must retain its snapshot")

    def test_candidate_only_file_and_absent_managed_directory_are_removed(self):
        self.publish()
        self.update_to_candidate()
        self.new.write_bytes(self.expected[TARGET_NEW][0])
        os.chmod(self.new, self.expected[TARGET_NEW][1])
        self.new_dir.mkdir(mode=0o700)
        os.chmod(self.new_dir, self.managed[DIR_NEW][1])
        self.restore()
        self.assertFalse(self.new.exists())
        self.assertFalse(self.new_dir.exists())

    def test_rollback_removes_inode_journaled_candidate_staging_files(self):
        self.publish()
        self.update_to_candidate()
        staged = transaction.stage_candidate(
            self.snapshot, self.targets, logical_target=TARGET_A,
            candidate_sha=CANDIDATE_SHA, index=0,
            payload=self.expected[TARGET_A][0], mode=self.expected[TARGET_A][1],
            uid=self.expected[TARGET_A][2], gid=self.expected[TARGET_A][3])
        self.restore()
        self.assertFalse(staged.exists())
        self.assertEqual(self.a.read_bytes(), b"old config\x00\n")

    def test_incomplete_known_rollback_stage_is_rebuilt_from_snapshot(self):
        self.publish()
        self.update_to_candidate()
        staged = self.a.parent / f".lm-code-rollback-{CANDIDATE_SHA}-0"
        staged.write_bytes(b"old con")
        os.chmod(staged, 0o600)
        self.restore()
        self.assertEqual(self.a.read_bytes(), b"old config\x00\n")
        self.assertFalse(staged.exists())

    def test_rollback_preserves_permanent_guard_target_and_its_directory(self):
        guard_target = self.new_dir / "guard.conf"
        self.targets[TARGET_NEW] = guard_target
        self.publish()
        self.update_to_candidate()
        self.new_dir.mkdir(mode=0o700)
        os.chmod(self.new_dir, self.managed[DIR_NEW][1])
        guard_target.write_bytes(self.expected[TARGET_NEW][0])
        os.chmod(guard_target, self.expected[TARGET_NEW][1])
        self.restore_preserving({TARGET_NEW})
        self.assertTrue(guard_target.exists())
        self.assertTrue(self.new_dir.exists())
        self.assertEqual(self.a.read_bytes(), b"old config\x00\n")

    def test_rollback_keeps_candidate_guard_even_when_old_inventory_had_a_file(self):
        self.publish()
        self.update_to_candidate()
        self.restore_preserving({TARGET_A})
        self.assertEqual(self.a.read_bytes(), self.expected[TARGET_A][0])
        self.assertEqual(_identity(self.a), self.expected[TARGET_A][1:])
        self.assertEqual(self.b.read_bytes(), b"old plugin\n")

    def test_rollback_refuses_unexpected_interrupted_update_staging_file(self):
        self.publish()
        self.update_to_candidate()
        staged = self.a.parent / f".{self.a.name}.lm-update-{CANDIDATE_SHA}-0.tmp"
        staged.write_bytes(b"unknown staging data")
        os.chmod(staged, 0o600)
        with self.assertRaises(Exception):
            self.restore()
        self.assertTrue(staged.exists())
        self.assertTrue(self.snapshot.exists())

    def test_unrecorded_staging_inode_is_preserved_and_blocks_rollback(self):
        self.publish()
        self.update_to_candidate()
        staged = self.a.parent / f".{self.a.name}.lm-update-{CANDIDATE_SHA}-0.tmp"
        staged.write_bytes(self.expected[TARGET_A][0][:7])
        os.chmod(staged, 0o600)
        with self.assertRaises(Exception):
            self.restore()
        self.assertTrue(staged.exists())
        self.assertTrue(self.snapshot.exists())

    def test_unknown_target_drift_refuses_and_preserves_snapshot(self):
        self.publish()
        before = self.snapshot.read_bytes()
        self.a.write_bytes(b"third-party edit")
        with self.assertRaises(Exception):
            self.restore()
        self.assertEqual(self.snapshot.read_bytes(), before)
        self.assertEqual(self.a.read_bytes(), b"third-party edit")

    def test_corrupt_and_partial_snapshots_refuse_without_replacement(self):
        self.publish()
        original = self.snapshot.read_bytes()
        for malformed in (b"not a snapshot", original[:-1], b"{}"):
            with self.subTest(snapshot=malformed[:20]):
                self.snapshot.write_bytes(malformed)
                with self.assertRaises(Exception):
                    self.restore()
        self.snapshot.write_bytes(original)

    def test_wrong_candidate_sha_refuses(self):
        self.publish()
        self.update_to_candidate()
        before = self.snapshot.read_bytes()
        with self.assertRaises(Exception):
            self.restore(candidate_sha="c" * 40)
        self.assertEqual(self.snapshot.read_bytes(), before)
        self.assertEqual(self.a.read_bytes(), self.expected[TARGET_A][0])

    def test_interrupted_replace_can_be_retried_to_complete_rollback(self):
        self.publish()
        self.update_to_candidate()
        real_replace = os.replace
        calls = 0

        def fail_once(source, destination, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected interruption")
            return real_replace(source, destination, *args, **kwargs)

        with mock.patch.object(transaction.os, "replace", side_effect=fail_once):
            with self.assertRaises(OSError):
                self.restore()

        # A partial first attempt is an allowed old-or-candidate mixture.
        self.restore()
        self.assertEqual(self.a.read_bytes(), b"old config\x00\n")
        self.assertEqual(self.b.read_bytes(), b"old plugin\n")
        self.assertTrue(self.snapshot.exists())

    def test_symlink_and_non_regular_targets_are_refused(self):
        self.publish()
        self.a.unlink()
        self.a.symlink_to(self.b)
        with self.assertRaises(Exception):
            self.restore()
        self.a.unlink()
        self.a.mkdir()
        with self.assertRaises(Exception):
            self.restore()

    def test_publish_refuses_symlink_and_non_regular_targets(self):
        self.a.unlink()
        self.a.symlink_to(self.b)
        with self.assertRaises(Exception):
            self.publish()
        self.a.unlink()
        self.a.mkdir()
        with self.assertRaises(Exception):
            self.publish()

    def test_sha_values_are_strictly_parsed(self):
        invalid = ("", "a" * 39, "a" * 41, "A" * 40, "g" * 40, "a" * 40 + "\n")
        for digest in invalid:
            with self.subTest(digest=digest):
                with self.assertRaises(Exception):
                    transaction.publish_snapshot(
                        self.snapshot,
                        self.targets,
                        candidate_sha=digest,
                        previous_sha=PREVIOUS_SHA,
                        managed_dirs=self.managed,
                    )
        with self.assertRaises(Exception):
            transaction.publish_snapshot(
                self.snapshot,
                self.targets,
                candidate_sha=CANDIDATE_SHA,
                previous_sha="z" * 40,
                managed_dirs=self.managed,
            )

    def test_verified_completion_phase_is_durable_and_cannot_move_backwards(self):
        self.publish()
        transaction.set_snapshot_phase(
            self.snapshot, candidate_sha=CANDIDATE_SHA, phase="verified-update")
        document = transaction._load_snapshot(self.snapshot)
        self.assertEqual(document["phase"], "verified-update")
        transaction.set_snapshot_phase(
            self.snapshot, candidate_sha=CANDIDATE_SHA, phase="verified-rollback")
        self.assertEqual(transaction._load_snapshot(self.snapshot)["phase"], "verified-rollback")
        with self.assertRaises(transaction.Refused):
            transaction.set_snapshot_phase(
                self.snapshot, candidate_sha=CANDIDATE_SHA, phase="verified-update")

    def test_managed_directory_with_unexpected_contents_is_never_recursively_removed(self):
        self.publish()
        self.update_to_candidate()
        self.new.write_bytes(self.expected[TARGET_NEW][0])
        os.chmod(self.new, self.expected[TARGET_NEW][1])
        self.new_dir.mkdir(mode=0o700)
        os.chmod(self.new_dir, self.managed[DIR_NEW][1])
        unexpected = self.new_dir / "unmanaged.txt"
        unexpected.write_bytes(b"keep me")
        with self.assertRaises(Exception):
            self.restore()
        self.assertEqual(unexpected.read_bytes(), b"keep me")
        self.assertTrue(self.snapshot.exists())

    def test_candidate_target_and_managed_directory_mode_are_validated(self):
        self.publish()
        self.update_to_candidate()
        self.new.write_bytes(self.expected[TARGET_NEW][0])
        os.chmod(self.new, self.expected[TARGET_NEW][1])
        self.new_dir.mkdir(mode=0o700)
        os.chmod(self.new_dir, self.managed[DIR_NEW][1])

        os.chmod(self.a, 0o644)
        with self.assertRaises(Exception):
            self.restore()
        os.chmod(self.a, self.expected[TARGET_A][1])

        os.chmod(self.new_dir, 0o755)
        with self.assertRaises(Exception):
            self.restore()
        self.assertTrue(self.snapshot.is_file())

    def test_candidate_target_and_managed_directory_owner_are_validated(self):
        if os.geteuid() != 0:
            self.skipTest("changing fixture ownership requires root")
        self.publish()
        self.update_to_candidate()
        self.new.write_bytes(self.expected[TARGET_NEW][0])
        os.chmod(self.new, self.expected[TARGET_NEW][1])
        self.new_dir.mkdir(mode=0o700)
        os.chmod(self.new_dir, self.managed[DIR_NEW][1])

        os.chown(self.a, 65534, 65534)
        with self.assertRaises(Exception):
            self.restore()
        os.chown(self.a, self.expected[TARGET_A][2], self.expected[TARGET_A][3])

        os.chown(self.new_dir, 65534, 65534)
        with self.assertRaises(Exception):
            self.restore()
        self.assertTrue(self.snapshot.is_file())

    def test_candidate_only_metadata_uses_fixture_identity(self):
        uid, gid = os.getuid(), os.getgid()
        self.assertEqual(self.expected[TARGET_NEW][2:], (uid, gid))
        self.assertEqual(self.managed[DIR_NEW][2:], (uid, gid))

    def test_snapshot_is_a_single_file_and_snapshot_symlink_is_refused(self):
        self.publish()
        self.assertTrue(self.snapshot.is_file())
        self.assertIsInstance(self.snapshot.read_bytes(), bytes)
        original = self.root / "snapshot-original.bin"
        os.replace(self.snapshot, original)
        self.snapshot.symlink_to(original)
        with self.assertRaises(Exception):
            self.restore()


if __name__ == "__main__":
    unittest.main()
