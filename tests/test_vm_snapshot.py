"""Fixture-only coverage for revision-bound, filtered source snapshots."""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/vm-recursion-workspace.py"
HISTORICAL_CANARY = "historical-snapshot-canary-43eabc"
WORKTREE_CANARY = "current-worktree-canary-8a6d21"


def load_snapshot_module():
    spec = importlib.util.spec_from_file_location("vm_recursion_workspace", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def git(source: Path, *args: str) -> str:
    environment = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    result = subprocess.run(
        ["git", "-C", str(source), *args], capture_output=True,
        text=True, env=environment, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vm-snapshot-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "checkout"
        self.source.mkdir(mode=0o700)
        self.run_root = self.base / "private-run"
        self.run_root.mkdir(mode=0o700)
        git(self.source, "init", "--quiet")
        git(self.source, "config", "user.name", "Snapshot Fixture")
        git(self.source, "config", "user.email", "snapshot-fixture@localhost.invalid")
        (self.source / "app.py").write_text("print('base')\n", encoding="utf-8")
        git(self.source, "add", "--", "app.py")
        git(self.source, "commit", "--quiet", "-m", "fixture baseline")

    def commit_initial_with_historical_secret(self):
        (self.source / "app.py").write_text("print('base')\n", encoding="utf-8")
        (self.source / ".env").write_text(HISTORICAL_CANARY, encoding="utf-8")
        git(self.source, "add", "--force", "--", "app.py", ".env")
        git(self.source, "commit", "--quiet", "-m", "fixture with historical secret")
        head = git(self.source, "rev-parse", "HEAD")
        (self.source / ".env").unlink()
        return head

    def test_dirty_tracked_and_untracked_content_is_snapshotted_without_changing_head(self):
        original_head = self.commit_initial_with_historical_secret()
        (self.source / "app.py").write_text(f"print('{WORKTREE_CANARY}')\n", encoding="utf-8")
        (self.source / "new.py").write_text("untracked = True\n", encoding="utf-8")
        first = load_snapshot_module().prepare(self.source, self.run_root)
        first_workspace = Path(first["workspace"])
        self.assertEqual(first["source_head"], original_head)
        self.assertEqual(git(self.source, "rev-parse", "HEAD"), original_head)
        self.assertEqual((first_workspace / "app.py").read_text(), f"print('{WORKTREE_CANARY}')\n")
        self.assertEqual((first_workspace / "new.py").read_text(), "untracked = True\n")
        self.assertNotEqual(first["tree_sha256"], "")

        second_root = self.base / "private-run-2"
        second_root.mkdir(mode=0o700)
        (self.source / "new.py").write_text("untracked = changed\n", encoding="utf-8")
        second = load_snapshot_module().prepare(self.source, second_root)
        self.assertEqual(second["source_head"], original_head)
        self.assertNotEqual(first["tree_sha256"], second["tree_sha256"])
        self.assertEqual((Path(second["workspace"]) / "new.py").read_text(), "untracked = changed\n")

    def test_non_git_source_is_not_accepted_as_revision_bound(self):
        module = load_snapshot_module()
        source = self.base / "unversioned"
        source.mkdir(mode=0o700)
        (source / "lab-vm").write_text("fixture\n", encoding="ascii")
        run_root = self.base / "unversioned-run"
        run_root.mkdir(mode=0o700)
        with self.assertRaises(module.SnapshotError):
            module.prepare(source, run_root)
        self.assertFalse((run_root / "candidate-snapshot").exists())

    def test_snapshot_excludes_credentials_databases_models_caches_and_git_history(self):
        source_head = self.commit_initial_with_historical_secret()
        (self.source / ".env.local").write_text("env-canary", encoding="utf-8")
        (self.source / "auth.json").write_text("auth-canary", encoding="utf-8")
        (self.source / "config.json").write_text("config-secret-canary", encoding="utf-8")
        (self.source / "id_ed25519").write_text("private-key-canary", encoding="utf-8")
        (self.source / "tls.pem").write_text("pem-canary", encoding="utf-8")
        (self.source / "cache.sqlite3").write_text("db-canary", encoding="utf-8")
        (self.source / "model.gguf").write_text("model-canary", encoding="utf-8")
        (self.source / ".cache").mkdir()
        (self.source / ".cache/data").write_text("cache-canary", encoding="utf-8")
        (self.source / "weights").mkdir()
        (self.source / "weights/model.safetensors").write_text("weights-canary", encoding="utf-8")
        (self.source / "models").mkdir()
        (self.source / "models/private.weights").write_text("model-directory-canary", encoding="utf-8")
        (self.source / ".env.example").write_text("EXAMPLE=true\n", encoding="utf-8")

        result = load_snapshot_module().prepare(self.source, self.run_root)
        workspace = Path(result["workspace"])
        self.assertTrue((workspace / ".env.example").is_file())
        self.assertEqual(git(workspace, "rev-list", "--count", "HEAD"), "1")
        self.assertNotEqual(git(workspace, "rev-parse", "HEAD"), source_head)
        history = git(workspace, "log", "--all", "-p")
        self.assertNotIn(HISTORICAL_CANARY, history)
        for forbidden in (
            ".env", ".env.local", "auth.json", "config.json", "id_ed25519", "tls.pem",
            "cache.sqlite3", "model.gguf", ".cache", "weights", "models",
        ):
            self.assertFalse((workspace / forbidden).exists(), forbidden)
        self.assertNotIn(str(self.source), (Path(result["bundle"]) / "source-manifest.json").read_text())
        self.assertNotIn(str(self.source), str(result))

    def test_file_and_directory_modes_and_internal_symlinks_are_preserved_and_rewritten(self):
        (self.source / "bin").mkdir(mode=0o750)
        (self.source / "bin/run.sh").write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        (self.source / "bin/run.sh").chmod(0o751)
        (self.source / "pkg").mkdir(mode=0o750)
        (self.source / "pkg/module.py").write_text("value = 1\n", encoding="utf-8")
        (self.source / "pkg/module.py").chmod(0o640)
        (self.source / "pkg/run-link").symlink_to("../../checkout/bin/run.sh")

        result = load_snapshot_module().prepare(self.source, self.run_root)
        workspace = Path(result["workspace"])
        self.assertEqual(stat.S_IMODE((workspace / "bin").stat().st_mode), 0o750)
        self.assertEqual(stat.S_IMODE((workspace / "bin/run.sh").stat().st_mode), 0o751)
        self.assertEqual(stat.S_IMODE((workspace / "pkg/module.py").stat().st_mode), 0o640)
        link = workspace / "pkg/run-link"
        self.assertTrue(link.is_symlink())
        self.assertFalse(os.path.isabs(os.readlink(link)))
        self.assertEqual(link.resolve(strict=True), workspace / "bin/run.sh")

    def test_symlink_escape_dangling_or_targeting_excluded_state_is_rejected(self):
        outside = self.base / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        module = load_snapshot_module()
        cases = {
            "escape": "../outside.txt",
            "dangling": "missing-target",
            "excluded": ".env.private",
        }
        for label, target in cases.items():
            with self.subTest(label=label):
                run_root = self.base / f"run-{label}"
                run_root.mkdir(mode=0o700)
                excluded_file = self.source / target
                (self.source / "link").symlink_to(target)
                try:
                    if label == "excluded":
                        excluded_file.write_text("secret", encoding="utf-8")
                    with self.assertRaises(module.SnapshotError):
                        module.prepare(self.source, run_root)
                    self.assertFalse((run_root / "candidate-snapshot").exists())
                finally:
                    (self.source / "link").unlink(missing_ok=True)
                    if label == "excluded":
                        excluded_file.unlink(missing_ok=True)

    def test_special_files_are_rejected_without_installing_snapshot(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO fixtures are unavailable")
        os.mkfifo(self.source / "pipe")
        module = load_snapshot_module()
        with self.assertRaises(module.SnapshotError):
            module.prepare(self.source, self.run_root)
        self.assertFalse((self.run_root / "candidate-snapshot").exists())

    def test_size_cap_and_disk_reserve_refuse_before_copy(self):
        (self.source / "payload.bin").write_bytes(b"1234567890")
        module = load_snapshot_module()
        with self.assertRaises(module.SnapshotError):
            module.prepare(self.source, self.run_root, max_bytes=9)
        self.assertFalse((self.run_root / "candidate-snapshot").exists())

        required = 2 * 10 + module.STAGING_RESERVE_BYTES
        usage = type("Usage", (), {"free": required - 1})()
        with (
            patch.object(module.shutil, "disk_usage", return_value=usage),
            patch.object(module, "copy_snapshot", side_effect=AssertionError("copy ran before disk preflight")),
            self.assertRaises(module.SnapshotError),
        ):
            module.prepare(self.source, self.run_root, max_bytes=10)
        self.assertFalse((self.run_root / "candidate-snapshot").exists())

    def test_source_replacement_race_is_rejected_and_never_installed(self):
        source_file = self.source / "race.txt"
        source_file.write_text("same bytes\n", encoding="utf-8")
        module = load_snapshot_module()
        copy_regular_file = module.copy_regular_file

        def replace_after_copy(source_path, destination, expected):
            copy_regular_file(source_path, destination, expected)
            replacement = source_path.with_name("replacement")
            replacement.write_bytes(source_path.read_bytes())
            replacement.chmod(stat.S_IMODE(source_path.stat().st_mode))
            os.replace(replacement, source_path)

        with (
            patch.object(module, "copy_regular_file", side_effect=replace_after_copy),
            self.assertRaises(module.SnapshotError),
        ):
            module.prepare(self.source, self.run_root)
        self.assertFalse((self.run_root / "candidate-snapshot").exists())

    def test_same_manifest_reuses_only_untouched_snapshot_and_changed_source_requires_fresh_run(self):
        (self.source / "file.txt").write_text("version one\n", encoding="utf-8")
        module = load_snapshot_module()
        first = module.prepare(self.source, self.run_root)
        second = module.prepare(self.source, self.run_root)
        self.assertTrue(second["reused"])
        self.assertEqual(first["workspace"], second["workspace"])

        (Path(first["workspace"]) / "file.txt").write_text("tampered\n", encoding="utf-8")
        with self.assertRaises(module.SnapshotError):
            module.prepare(self.source, self.run_root)
        self.assertEqual((Path(first["workspace"]) / "file.txt").read_text(), "tampered\n")

        clean_root = self.base / "new-run"
        clean_root.mkdir(mode=0o700)
        clean = module.prepare(self.source, clean_root)
        self.assertEqual((Path(clean["workspace"]) / "file.txt").read_text(), "version one\n")
        (self.source / "file.txt").write_text("version two\n", encoding="utf-8")
        with self.assertRaises(module.SnapshotError):
            module.prepare(self.source, clean_root)
        self.assertEqual((Path(clean["workspace"]) / "file.txt").read_text(), "version one\n")

    def test_private_run_root_and_manifest_modes_are_enforced(self):
        (self.source / "file.txt").write_text("safe\n", encoding="utf-8")
        module = load_snapshot_module()
        self.run_root.chmod(0o755)
        with self.assertRaises(module.SnapshotError):
            module.prepare(self.source, self.run_root)
        self.run_root.chmod(0o700)
        result = module.prepare(self.source, self.run_root)
        bundle = Path(result["bundle"])
        self.assertEqual(stat.S_IMODE(bundle.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((bundle / "source-manifest.json").stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
