"""Committed-Git-tree profile-runtime extraction tests."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALLER_PATH = ROOT / "lib/vm/profile-runtime-install.py"
SPEC = importlib.util.spec_from_file_location("vm_profile_runtime_install", INSTALLER_PATH)
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)


class CommittedRuntimeInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="profile-runtime-install-")
        self.root = pathlib.Path(self.temp.name)
        self.repository = self.root / "candidate"
        self.repository.mkdir(mode=0o755)
        self.store = self.root / "runtime-store"
        self.store.mkdir(mode=0o755)
        self._git("init", "--quiet")
        self._git("config", "user.name", "Profile Install Test")
        self._git("config", "user.email", "profile-install@example.invalid")
        self._git("config", "core.filemode", "true")

    def tearDown(self):
        for path in sorted(self.root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            try:
                if path.is_dir() and not path.is_symlink():
                    os.chmod(path, 0o755)
                elif not path.is_symlink():
                    os.chmod(path, 0o644)
            except FileNotFoundError:
                pass
        self.temp.cleanup()

    def _git(self, *args):
        return subprocess.run(
            ["/usr/bin/git", "-C", str(self.repository), *args],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=True,
        ).stdout.decode().strip()

    def _git_raw(self, *args, input_bytes=None):
        return subprocess.run(
            ["/usr/bin/git", "-C", str(self.repository), *args],
            input=input_bytes, stdin=subprocess.DEVNULL if input_bytes is None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout

    def _commit(self, files: dict[str, tuple[bytes, str]], *, extra: dict[str, bytes] | None = None) -> str:
        for relative, (payload, mode) in files.items():
            path = self.repository / relative
            path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            path.write_bytes(payload)
            os.chmod(path, 0o755 if mode == "100755" else 0o644)
        for relative, payload in (extra or {}).items():
            path = self.repository / relative
            path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            path.write_bytes(payload)
            os.chmod(path, 0o644)
        descriptor = {
            "schema_version": 1,
            "profile_runtime_api": 1,
            "control_helper_api_min": 1,
            "control_helper_api_max": 1,
            "entrypoint": "lib/vm/profile-runtime.py",
            "files": [
                {"path": path, "git_mode": mode}
                for path, (_payload, mode) in sorted(files.items())
            ],
        }
        (self.repository / "profile-runtime.json").write_text(
            json.dumps(descriptor, sort_keys=True, separators=(",", ":")), encoding="utf-8",
        )
        os.chmod(self.repository / "profile-runtime.json", 0o644)
        self._git("add", "-A")
        self._git("commit", "--quiet", "-m", "candidate runtime")
        return self._git("rev-parse", "HEAD")

    def _install(self, commit_sha):
        return installer.install_committed_runtime(
            self.repository, commit_sha,
            owner_uid=os.getuid(), owner_gid=os.getgid(), runtime_store=self.store,
        )

    def test_installs_only_declared_committed_blobs_as_immutable_data_and_reuses_sha(self):
        runtime_files = {
            "lib/vm/profile-runtime.py": (b"this candidate is data; do not execute\n", "100755"),
            "lib/vm/support.py": (b"support bytes\n", "100644"),
        }
        commit_sha = self._commit(runtime_files, extra={"not-installed.txt": b"ignore\n"})

        result = self._install(commit_sha)
        self.assertTrue(result.created)
        self.assertEqual(result.runtime_path, self.store / commit_sha)
        self.assertEqual((result.runtime_path / "lib/vm/profile-runtime.py").read_bytes(),
                         runtime_files["lib/vm/profile-runtime.py"][0])
        self.assertFalse((result.runtime_path / "not-installed.txt").exists())
        self.assertEqual(os.stat(result.runtime_path / "lib/vm/profile-runtime.py").st_mode & 0o777, 0o555)
        self.assertEqual(os.stat(result.runtime_path / "lib/vm/support.py").st_mode & 0o777, 0o444)
        self.assertEqual(len(result.manifest["files"]), 3)

        reused = self._install(commit_sha)
        self.assertFalse(reused.created)
        self.assertEqual(reused.manifest, result.manifest)

    def test_different_commits_publish_distinct_sha_roots(self):
        files_a = {"lib/vm/profile-runtime.py": (b"runtime A\n", "100755")}
        sha_a = self._commit(files_a)
        first = self._install(sha_a)
        files_b = {"lib/vm/profile-runtime.py": (b"runtime B\n", "100755")}
        sha_b = self._commit(files_b)
        second = self._install(sha_b)

        self.assertNotEqual(sha_a, sha_b)
        self.assertEqual(first.runtime_path.name, sha_a)
        self.assertEqual(second.runtime_path.name, sha_b)
        self.assertEqual((first.runtime_path / "lib/vm/profile-runtime.py").read_bytes(), b"runtime A\n")
        self.assertEqual((second.runtime_path / "lib/vm/profile-runtime.py").read_bytes(), b"runtime B\n")

    def test_refuses_head_mismatch_symlink_blobs_and_preserves_unknown_sha_target(self):
        commit_sha = self._commit({
            "lib/vm/profile-runtime.py": (b"runtime\n", "100755"),
        })
        other_sha = "f" * 40 if commit_sha != "f" * 40 else "e" * 40
        with self.assertRaises(installer.InstallRefused):
            self._install(other_sha)
        self.assertFalse((self.store / commit_sha).exists())

        existing = self.store / commit_sha
        existing.mkdir(mode=0o700)
        marker = existing / "keep-me"
        marker.write_text("unknown target\n", encoding="utf-8")
        with self.assertRaises(installer.InstallRefused):
            self._install(commit_sha)
        self.assertEqual(marker.read_text(encoding="utf-8"), "unknown target\n")

    def test_symlink_inventory_mode_is_rejected_without_publishing_runtime(self):
        runtime_file = self.repository / "lib/vm/profile-runtime.py"
        runtime_file.parent.mkdir(parents=True)
        (self.repository / "actual-runtime.py").write_text("runtime\n", encoding="utf-8")
        runtime_file.symlink_to("../../actual-runtime.py")
        descriptor = {
            "schema_version": 1,
            "profile_runtime_api": 1,
            "control_helper_api_min": 1,
            "control_helper_api_max": 1,
            "entrypoint": "lib/vm/profile-runtime.py",
            "files": [{"path": "lib/vm/profile-runtime.py", "git_mode": "100644"}],
        }
        (self.repository / "profile-runtime.json").write_text(json.dumps(descriptor), encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "--quiet", "-m", "symlink candidate")
        commit_sha = self._git("rev-parse", "HEAD")

        with self.assertRaises(installer.InstallRefused):
            self._install(commit_sha)
        self.assertFalse((self.store / commit_sha).exists())

    def test_corrupted_loose_blob_is_rejected_against_its_git_object_id(self):
        payload = b"original committed runtime bytes\n"
        commit_sha = self._commit({
            "lib/vm/profile-runtime.py": (payload, "100755"),
        })
        blob_sha = self._git("rev-parse", f"{commit_sha}:lib/vm/profile-runtime.py")
        object_path = self.repository / ".git" / "objects" / blob_sha[:2] / blob_sha[2:]
        raw_object = zlib.decompress(object_path.read_bytes())
        header, separator, stored_payload = raw_object.partition(b"\0")
        self.assertTrue(separator)
        self.assertTrue(header.startswith(b"blob "))
        altered_payload = b"X" + stored_payload[1:]
        os.chmod(object_path, 0o644)
        object_path.write_bytes(zlib.compress(header + b"\0" + altered_payload))

        with self.assertRaisesRegex(installer.InstallRefused, "object ID"):
            self._install(commit_sha)
        self.assertFalse((self.store / commit_sha).exists())

    def test_stalled_blob_pipe_is_bounded_by_a_deadline(self):
        commit_sha = self._commit({
            "lib/vm/profile-runtime.py": (b"runtime bytes\n", "100755"),
        })
        wrapper = self.root / "git-timeout-wrapper"
        wrapper.write_text(
            "#!/usr/bin/python3\n"
            "import os, sys, time\n"
            "args = sys.argv[1:]\n"
            "if args[-2:] == ['cat-file', '--batch']:\n"
            "    object_id = sys.stdin.readline().strip()\n"
            "    print(object_id + ' blob 16', flush=True)\n"
            "    time.sleep(5)\n"
            "else:\n"
            "    os.execv('/usr/bin/git', ['/usr/bin/git', *args])\n",
            encoding="utf-8",
        )
        os.chmod(wrapper, 0o755)
        original_git = installer.GIT
        original_timeout = installer.GIT_TIMEOUT_SECONDS
        installer.GIT = str(wrapper)
        installer.GIT_TIMEOUT_SECONDS = 0.2
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(installer.InstallRefused, "timed out"):
                self._install(commit_sha)
        finally:
            installer.GIT = original_git
            installer.GIT_TIMEOUT_SECONDS = original_timeout
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertFalse((self.store / commit_sha).exists())

    def test_hostile_local_filters_fsmonitor_and_hooks_are_never_executed(self):
        marker = self.root / "hostile-git-config-ran"
        payload = b"committed bytes, not a checkout\n"
        commit_sha = self._commit({
            "lib/vm/profile-runtime.py": (payload, "100755"),
        }, extra={".gitattributes": b"lib/vm/profile-runtime.py filter=hostile\n"})
        script = self.root / "hostile-filter"
        script.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n", encoding="utf-8")
        os.chmod(script, 0o755)
        self._git("config", "core.fsmonitor", str(script))
        self._git("config", "filter.hostile.smudge", str(script))
        self._git("config", "filter.hostile.clean", str(script))
        hooks = self.root / "hostile-hooks"
        hooks.mkdir()
        hook = hooks / "post-checkout"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        os.chmod(hook, 0o755)
        self._git("config", "core.hooksPath", str(hooks))

        result = self._install(commit_sha)

        self.assertEqual((result.runtime_path / "lib/vm/profile-runtime.py").read_bytes(), payload)
        self.assertFalse(marker.exists())

    def test_duplicate_git_tree_names_are_rejected(self):
        commit_sha = self._commit({
            "lib/vm/profile-runtime.py": (b"runtime\n", "100755"),
        })
        records = self._git_raw("ls-tree", "-z", "HEAD").split(b"\0")
        records = [record for record in records if record]
        descriptor_record = next(
            record for record in records if record.split(b"\t", 1)[1] == b"profile-runtime.json"
        )
        encoded = bytearray()
        for record in records:
            metadata, name = record.split(b"\t", 1)
            mode, object_type, object_id = metadata.split(b" ")
            tree_mode = b"40000" if mode == b"040000" else mode
            encoded.extend(tree_mode + b" " + name + b"\0" + bytes.fromhex(object_id.decode("ascii")))
        metadata, name = descriptor_record.split(b"\t", 1)
        mode, _object_type, object_id = metadata.split(b" ")
        encoded.extend(mode + b" " + name + b"\0" + bytes.fromhex(object_id.decode("ascii")))
        root_tree = self._git_raw(
            "hash-object", "--literally", "-w", "-t", "tree", "--stdin",
            input_bytes=bytes(encoded),
        ).decode().strip()
        parent = self._git("rev-parse", "HEAD")
        commit_payload = (
            f"tree {root_tree}\nparent {parent}\n"
            "author Profile Install Test <profile-install@example.invalid> 1 +0000\n"
            "committer Profile Install Test <profile-install@example.invalid> 1 +0000\n\n"
            "malformed duplicate tree name\n"
        ).encode()
        malformed_commit = self._git_raw(
            "hash-object", "-w", "-t", "commit", "--stdin", input_bytes=commit_payload,
        ).decode().strip()
        self._git("update-ref", "HEAD", malformed_commit)

        with self.assertRaisesRegex(installer.InstallRefused, "duplicate"):
            self._install(malformed_commit)
        self.assertFalse((self.store / malformed_commit).exists())

    def test_prepublication_crash_preserves_unknown_stage_and_retry_publishes(self):
        commit_sha = self._commit({
            "lib/vm/profile-runtime.py": (b"runtime\n", "100755"),
        })
        with mock.patch.object(
            installer, "_rename_noreplace",
            side_effect=installer.InstallRefused("injected before atomic publication"),
        ):
            with self.assertRaisesRegex(installer.InstallRefused, "injected"):
                self._install(commit_sha)

        stages = [path for path in self.store.iterdir() if path.name.startswith(f".staging-{commit_sha}-")]
        self.assertEqual(len(stages), 1)
        self.assertEqual(os.stat(stages[0]).st_mode & 0o777, 0o555)
        self.assertFalse((self.store / commit_sha).exists())
        installer.core.verify_runtime_installation(
            stages[0], pinned_sha=commit_sha, expected_uid=os.getuid(), allow_staging=True,
        )

        result = self._install(commit_sha)
        self.assertTrue(result.created)
        self.assertTrue((self.store / commit_sha).is_dir())
        self.assertTrue(stages[0].is_dir(), "interrupted stage is retained, not auto-collected")

    def test_postrename_crash_leaves_a_complete_target_that_retry_reuses(self):
        commit_sha = self._commit({
            "lib/vm/profile-runtime.py": (b"runtime\n", "100755"),
        })
        rename = installer._rename_noreplace

        def rename_then_crash(source_fd, source, destination_fd, destination):
            self.assertEqual(os.fstat(source_fd).st_ino, os.fstat(destination_fd).st_ino)
            self.assertEqual(os.stat(source, dir_fd=source_fd).st_mode & 0o777, 0o555)
            rename(source_fd, source, destination_fd, destination)
            raise RuntimeError("injected after atomic publication")

        with mock.patch.object(installer, "_rename_noreplace", side_effect=rename_then_crash):
            with self.assertRaisesRegex(RuntimeError, "after atomic publication"):
                self._install(commit_sha)

        final = self.store / commit_sha
        self.assertEqual(os.stat(final).st_mode & 0o777, 0o555)
        verified = installer.core.verify_runtime_installation(
            final, pinned_sha=commit_sha, expected_uid=os.getuid(),
        )
        self.assertEqual(verified["commit_sha"], commit_sha)
        reused = self._install(commit_sha)
        self.assertFalse(reused.created)


if __name__ == "__main__":
    unittest.main()
