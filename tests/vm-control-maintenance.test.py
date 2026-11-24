#!/usr/bin/env python3
"""Protected maintenance substrate and nonblocking lease regressions."""

from __future__ import annotations

import fcntl
import importlib.util
import multiprocessing
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "lib/vm/control-maintenance.py"
SPEC = importlib.util.spec_from_file_location("vm_control_maintenance", MODULE_PATH)
maintenance = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = maintenance
SPEC.loader.exec_module(maintenance)


def _contend(path, owner_uid, owner_gid, exclusive, attempted, result, release):
    attempted.set()
    try:
        with maintenance._test_maintenance_lease(
            path, exclusive=exclusive, owner_uid=owner_uid, owner_gid=owner_gid,
        ):
            result.put("acquired")
            if not release.wait(5):
                result.put("release-timeout")
    except maintenance.Busy:
        result.put("busy")
    except BaseException as error:
        result.put(f"error:{type(error).__name__}:{error}")


def _exit_while_leased(path, owner_uid, owner_gid, acquired):
    with maintenance._test_maintenance_lease(
        path, exclusive=True, owner_uid=owner_uid, owner_gid=owner_gid,
    ):
        acquired.set()
        os._exit(0)


class MaintenanceFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vm-maintenance-")
        self.root = pathlib.Path(self.temp.name)
        self.path = self.root / "control"
        self.owner_uid = os.geteuid()
        self.owner_gid = os.getegid()

    def tearDown(self):
        # Restore fixture permissions so TemporaryDirectory can clean up even
        # after tests deliberately create unsafe directory modes.
        for item in sorted(self.root.rglob("*"), key=lambda path: len(path.parts), reverse=True):
            try:
                if item.is_dir() and not item.is_symlink():
                    os.chmod(item, 0o755)
            except FileNotFoundError:
                pass
        self.temp.cleanup()

    def ensure(self):
        maintenance._test_ensure_substrate(
            self.path, owner_uid=self.owner_uid, owner_gid=self.owner_gid,
        )

    def lease(self, *, exclusive):
        return maintenance._test_maintenance_lease(
            self.path, exclusive=exclusive,
            owner_uid=self.owner_uid, owner_gid=self.owner_gid,
        )

    def test_ensure_is_idempotent_and_creates_exact_private_fixture_objects(self):
        self.ensure()
        directory_before = os.stat(self.path, follow_symlinks=False)
        lock_before = os.stat(self.path / maintenance.MAINTENANCE_LOCK, follow_symlinks=False)
        self.ensure()
        directory_after = os.stat(self.path, follow_symlinks=False)
        lock_after = os.stat(self.path / maintenance.MAINTENANCE_LOCK, follow_symlinks=False)

        self.assertEqual((directory_before.st_dev, directory_before.st_ino),
                         (directory_after.st_dev, directory_after.st_ino))
        self.assertEqual((lock_before.st_dev, lock_before.st_ino),
                         (lock_after.st_dev, lock_after.st_ino))
        self.assertEqual(directory_after.st_uid, self.owner_uid)
        self.assertEqual(directory_after.st_gid, self.owner_gid)
        self.assertEqual(stat.S_IMODE(directory_after.st_mode), 0o755)
        self.assertEqual(lock_after.st_uid, self.owner_uid)
        self.assertEqual(lock_after.st_gid, self.owner_gid)
        self.assertEqual(stat.S_IMODE(lock_after.st_mode), 0o600)
        self.assertEqual(lock_after.st_nlink, 1)

        with self.lease(exclusive=True) as lock_fd:
            self.assertFalse(os.get_inheritable(lock_fd))
            self.assertEqual(os.fstat(lock_fd).st_ino, lock_after.st_ino)

    @unittest.skipUnless(os.geteuid() == 0, "root-only: verify root-owned fixture substrate")
    def test_root_owned_fixture_matches_fixed_substrate_contract(self):
        root_path = self.root / "root-control"
        maintenance._test_ensure_substrate(root_path, owner_uid=0, owner_gid=0)
        directory = os.stat(root_path, follow_symlinks=False)
        lock = os.stat(root_path / maintenance.MAINTENANCE_LOCK, follow_symlinks=False)

        self.assertEqual(maintenance.CONTROL_DIRECTORY, "/run/little-mester-vm-control")
        self.assertEqual((directory.st_uid, directory.st_gid, stat.S_IMODE(directory.st_mode)),
                         (0, 0, 0o755))
        self.assertEqual((lock.st_uid, lock.st_gid, stat.S_IMODE(lock.st_mode), lock.st_nlink),
                         (0, 0, 0o600, 1))

    def test_normal_lease_refuses_missing_substrate_without_creating_it(self):
        with self.assertRaises(maintenance.Refused):
            with self.lease(exclusive=False):
                self.fail("a missing substrate must not be created by lease acquisition")
        self.assertFalse(self.path.exists())

    def test_retry_after_directory_creation_interruption(self):
        # Exercise the production creation path through the private fixture
        # seam; the fixed /run substrate itself remains root-only.
        with mock.patch.object(maintenance.os, "fsync", side_effect=OSError("simulated stop")):
            with self.assertRaises(maintenance.Refused):
                self.ensure()
        self.assertTrue(self.path.is_dir())
        info = os.stat(self.path, follow_symlinks=False)
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o755)
        self.assertFalse((self.path / maintenance.MAINTENANCE_LOCK).exists())

        self.ensure()
        self.assertTrue((self.path / maintenance.MAINTENANCE_LOCK).is_file())

    def test_retry_after_lock_creation_interruption(self):
        self.path.mkdir(mode=0o755)
        os.chmod(self.path, 0o755)
        with mock.patch.object(maintenance.os, "fsync", side_effect=OSError("simulated stop")):
            with self.assertRaises(maintenance.Refused):
                self.ensure()
        lock_path = self.path / maintenance.MAINTENANCE_LOCK
        lock_info = os.stat(lock_path, follow_symlinks=False)
        self.assertTrue(stat.S_ISREG(lock_info.st_mode))
        self.assertEqual(stat.S_IMODE(lock_info.st_mode), 0o600)

        self.ensure()
        self.assertEqual(os.stat(lock_path, follow_symlinks=False).st_ino, lock_info.st_ino)

    def test_symlinked_and_unsafe_ancestry_refuse_without_mutation(self):
        actual = self.root / "actual-parent"
        actual.mkdir()
        link = self.root / "linked-parent"
        link.symlink_to(actual, target_is_directory=True)
        symlink_target = link / "control"
        with self.assertRaises(maintenance.Refused):
            maintenance._test_ensure_substrate(
                symlink_target, owner_uid=self.owner_uid, owner_gid=self.owner_gid,
            )
        self.assertTrue(link.is_symlink())
        self.assertFalse((actual / "control").exists())

        unsafe = self.root / "writable-parent"
        unsafe.mkdir()
        os.chmod(unsafe, 0o777)
        try:
            with self.assertRaises(maintenance.Refused):
                maintenance._test_ensure_substrate(
                    unsafe / "control", owner_uid=self.owner_uid, owner_gid=self.owner_gid,
                )
            self.assertFalse((unsafe / "control").exists())
        finally:
            os.chmod(unsafe, 0o700)

    def test_existing_substrate_symlink_is_preserved(self):
        sentinel = self.root / "sentinel"
        sentinel.write_text("do not touch", encoding="utf-8")
        self.path.symlink_to(sentinel)
        with self.assertRaises(maintenance.Refused):
            self.ensure()
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "do not touch")

    def test_existing_directory_with_wrong_mode_is_refused_and_preserved(self):
        self.ensure()
        marker = self.path / "keep-me"
        marker.write_text("preserve", encoding="utf-8")
        os.chmod(self.path, 0o700)
        with self.assertRaises(maintenance.Refused):
            self.ensure()
        with self.assertRaises(maintenance.Refused):
            with self.lease(exclusive=True):
                self.fail("unsafe substrate was accepted")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o700)
        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    def test_wrong_lock_mode_and_hardlink_count_are_refused_without_repair(self):
        self.ensure()
        lock_path = self.path / maintenance.MAINTENANCE_LOCK
        os.chmod(lock_path, 0o640)
        with self.assertRaises(maintenance.Refused):
            self.ensure()
        self.assertEqual(stat.S_IMODE(os.stat(lock_path).st_mode), 0o640)

        os.chmod(lock_path, 0o600)
        alias = self.path / "hardlink-alias"
        os.link(lock_path, alias)
        with self.assertRaises(maintenance.Refused):
            with self.lease(exclusive=False):
                self.fail("multiply-linked maintenance lock was accepted")
        self.assertEqual(os.stat(lock_path).st_ino, os.stat(alias).st_ino)
        self.assertEqual(os.stat(lock_path).st_nlink, 2)

    @unittest.skipUnless(os.geteuid() == 0, "root-only: change lock ownership to simulate foreign owner")
    def test_wrong_owner_is_root_only_and_preserved(self):
        self.ensure()
        lock_path = self.path / maintenance.MAINTENANCE_LOCK
        foreign_uid = 65534
        os.chown(lock_path, foreign_uid, self.owner_gid)
        with self.assertRaises(maintenance.Refused):
            self.ensure()
        self.assertEqual(os.stat(lock_path).st_uid, foreign_uid)
        self.assertEqual(stat.S_IMODE(os.stat(lock_path).st_mode), 0o600)

    def test_lock_symlink_directory_and_fifo_are_rejected_and_preserved(self):
        self.path.mkdir(mode=0o755)
        os.chmod(self.path, 0o755)
        lock_path = self.path / maintenance.MAINTENANCE_LOCK
        sentinel = self.root / "lock-target"
        sentinel.write_text("keep", encoding="utf-8")
        lock_path.symlink_to(sentinel)
        with self.assertRaises(maintenance.Refused):
            self.ensure()
        self.assertTrue(lock_path.is_symlink())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

        lock_path.unlink()
        lock_path.mkdir(mode=0o700)
        with self.assertRaises(maintenance.Refused):
            self.ensure()
        self.assertTrue(lock_path.is_dir())
        lock_path.rmdir()

        os.mkfifo(lock_path, mode=0o600)
        # O_NONBLOCK is required: opening this hostile FIFO must not hang.
        with self.assertRaises(maintenance.Refused):
            with self.lease(exclusive=True):
                self.fail("FIFO maintenance lock was accepted")
        self.assertTrue(stat.S_ISFIFO(os.stat(lock_path, follow_symlinks=False).st_mode))

    def test_replacement_between_path_stat_and_open_is_refused_and_preserved(self):
        self.ensure()
        lock_path = self.path / maintenance.MAINTENANCE_LOCK
        saved_path = self.path / "saved-lock"
        original_info = os.stat(lock_path, follow_symlinks=False)
        real_open = maintenance.os.open
        replaced = False

        def replace_on_open(name, flags, mode=0o777, *, dir_fd=None):
            nonlocal replaced
            if name == maintenance.MAINTENANCE_LOCK and dir_fd is not None and not replaced:
                os.rename(lock_path, saved_path)
                replacement_fd = real_open(
                    maintenance.MAINTENANCE_LOCK,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o600,
                    dir_fd=dir_fd,
                )
                os.write(replacement_fd, b"replacement preserved")
                os.close(replacement_fd)
                replaced = True
            return real_open(name, flags, mode, dir_fd=dir_fd)

        with mock.patch.object(maintenance.os, "open", side_effect=replace_on_open):
            with self.assertRaises(maintenance.Refused):
                with self.lease(exclusive=True):
                    self.fail("replaced lock identity was accepted")
        self.assertTrue(replaced)
        self.assertEqual(os.stat(saved_path).st_ino, original_info.st_ino)
        self.assertEqual((lock_path).read_bytes(), b"replacement preserved")

    def test_replacement_after_flock_is_refused_and_both_objects_are_preserved(self):
        self.ensure()
        lock_path = self.path / maintenance.MAINTENANCE_LOCK
        saved_path = self.path / "held-old-lock"
        original_info = os.stat(lock_path, follow_symlinks=False)
        real_flock = maintenance.fcntl.flock
        replaced = False

        def replace_after_lock(fd, operation):
            nonlocal replaced
            result = real_flock(fd, operation)
            if operation & fcntl.LOCK_NB and not replaced:
                os.rename(lock_path, saved_path)
                replacement_fd = os.open(
                    lock_path,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o600,
                )
                os.close(replacement_fd)
                replaced = True
            return result

        with mock.patch.object(maintenance.fcntl, "flock", side_effect=replace_after_lock):
            with self.assertRaises(maintenance.Refused):
                with self.lease(exclusive=True):
                    self.fail("post-flock replacement was accepted")
        self.assertTrue(replaced)
        self.assertEqual(os.stat(saved_path).st_ino, original_info.st_ino)
        self.assertTrue(stat.S_ISREG(os.stat(lock_path).st_mode))
        self.assertEqual(stat.S_IMODE(os.stat(lock_path).st_mode), 0o600)

    def test_replacement_of_substrate_directory_after_flock_is_refused(self):
        self.ensure()
        saved_path = self.root / "saved-control"
        real_flock = maintenance.fcntl.flock
        replaced = False

        def replace_after_lock(fd, operation):
            nonlocal replaced
            result = real_flock(fd, operation)
            if operation & fcntl.LOCK_NB and not replaced:
                os.rename(self.path, saved_path)
                self.path.mkdir(mode=0o755)
                os.chmod(self.path, 0o755)
                replaced = True
            return result

        with mock.patch.object(maintenance.fcntl, "flock", side_effect=replace_after_lock):
            with self.assertRaises(maintenance.Refused):
                with self.lease(exclusive=True):
                    self.fail("post-flock directory replacement was accepted")
        self.assertTrue(replaced)
        self.assertTrue((saved_path / maintenance.MAINTENANCE_LOCK).is_file())
        self.assertEqual(list(self.path.iterdir()), [])

    def test_shared_shared_succeeds_and_all_conflicting_modes_are_immediately_busy(self):
        self.ensure()
        context = multiprocessing.get_context("fork")
        cases = ((False, False, "acquired"), (False, True, "busy"),
                 (True, False, "busy"), (True, True, "busy"))
        for parent_exclusive, child_exclusive, expected in cases:
            with self.subTest(parent_exclusive=parent_exclusive, child_exclusive=child_exclusive):
                attempted = context.Event()
                release = context.Event()
                result = context.Queue()
                with self.lease(exclusive=parent_exclusive):
                    child = context.Process(
                        target=_contend,
                        args=(str(self.path), self.owner_uid, self.owner_gid,
                              child_exclusive, attempted, result, release),
                    )
                    child.start()
                    self.assertTrue(attempted.wait(5), "child did not reach the lock attempt")
                    self.assertEqual(result.get(timeout=5), expected)
                    release.set()
                    child.join(5)
                    self.assertFalse(child.is_alive(), "child did not exit after lease release")
                    self.assertEqual(child.exitcode, 0)

    def test_lease_is_released_on_exception_and_process_exit(self):
        self.ensure()
        with self.assertRaisesRegex(RuntimeError, "fixture exception"):
            with self.lease(exclusive=True):
                raise RuntimeError("fixture exception")
        with self.lease(exclusive=True):
            pass

        context = multiprocessing.get_context("fork")
        acquired = context.Event()
        child = context.Process(
            target=_exit_while_leased,
            args=(str(self.path), self.owner_uid, self.owner_gid, acquired),
        )
        child.start()
        self.assertTrue(acquired.wait(5), "child did not acquire its lease")
        child.join(5)
        self.assertFalse(child.is_alive(), "lease holder did not exit")
        self.assertEqual(child.exitcode, 0)
        with self.lease(exclusive=True):
            pass

    def test_lease_descriptor_is_not_inherited_across_exec(self):
        self.ensure()
        with self.lease(exclusive=True) as lock_fd:
            locked = os.fstat(lock_fd)
            child = os.fork()
            if child == 0:
                code = (
                    "import os,sys; fd=int(sys.argv[1]); expected=(int(sys.argv[2]),int(sys.argv[3])); "
                    "\ntry: s=os.fstat(fd)\nexcept OSError: raise SystemExit(0)\n"
                    "raise SystemExit((s.st_dev,s.st_ino)==expected)"
                )
                try:
                    os.execv(sys.executable, [
                        sys.executable, "-c", code, str(lock_fd),
                        str(locked.st_dev), str(locked.st_ino),
                    ])
                except BaseException:
                    os._exit(127)
            _waited, status = os.waitpid(child, 0)
            self.assertTrue(os.WIFEXITED(status))
            self.assertEqual(os.WEXITSTATUS(status), 0, "maintenance fd survived exec")

    def test_non_boolean_exclusive_argument_is_refused(self):
        self.ensure()
        with self.assertRaises(maintenance.Refused):
            with maintenance._test_maintenance_lease(
                self.path, exclusive=1, owner_uid=self.owner_uid, owner_gid=self.owner_gid,
            ):
                self.fail("non-boolean lock mode was accepted")


if __name__ == "__main__":
    unittest.main()
