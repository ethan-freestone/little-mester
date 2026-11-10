#!/usr/bin/env python3
"""Unprivileged regressions for the production short virtiofs socket allocator."""

import importlib.util
import os
import pathlib
import socket
import stat
import subprocess
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("virtiofs_socket", ROOT / "lib/vm/virtiofs-socket.py")
virtiofs_socket = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(virtiofs_socket)


class RuntimeFixture:
    def __init__(self, base):
        self.base = pathlib.Path(base)
        self.uid = os.getuid()
        self.runtime_root = self.base / "run" / "user" / str(self.uid)
        self.runtime_root.mkdir(parents=True)
        os.chmod(self.base, 0o755)
        os.chmod(self.base / "run", 0o755)
        os.chmod(self.base / "run/user", 0o755)
        os.chmod(self.runtime_root, 0o700)
        self.system_uid = self.uid

    def run_dir(self, identity):
        state = self.base / "home" / "long" / "private" / "state" / "v3-smoke"
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = state / identity
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
        return path

    def ownership_patches(self, *, wrong_path=None, wrong_uid=None):
        real_open = os.open
        real_stat = os.stat
        real_fstat = os.fstat
        fd_paths = {}

        def resolved(path, dir_fd):
            path = pathlib.Path(os.fsdecode(path))
            return path if path.is_absolute() else fd_paths[dir_fd] / path

        def synthetic(info, path):
            path = pathlib.Path(path)
            # Model root-owned /tmp-style ancestors and a UID-owned runtime root.
            uid = self.system_uid
            if path == self.base or self.base in path.parents:
                uid = self.uid
            if path == self.runtime_root or self.runtime_root in path.parents:
                uid = self.uid
            if (self.base / "home") == path or (self.base / "home") in path.parents:
                uid = self.uid
            if wrong_path is not None and pathlib.Path(path) == pathlib.Path(wrong_path):
                uid = wrong_uid
            return SimpleNamespace(st_mode=info.st_mode, st_uid=uid,
                                   st_dev=info.st_dev, st_ino=info.st_ino,
                                   st_size=info.st_size, st_nlink=info.st_nlink)

        def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
            fd = real_open(path, flags, mode, dir_fd=dir_fd) if dir_fd is not None else real_open(path, flags, mode)
            fd_paths[fd] = resolved(path, dir_fd) if dir_fd is not None else pathlib.Path(os.fsdecode(path))
            return fd

        def synthetic_stat(path, *, dir_fd=None, follow_symlinks=True):
            absolute = resolved(path, dir_fd) if dir_fd is not None else pathlib.Path(os.fsdecode(path))
            return synthetic(real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks), absolute)

        def synthetic_fstat(fd):
            return synthetic(real_fstat(fd), fd_paths.get(fd, pathlib.Path("/")))

        return (
            mock.patch.object(virtiofs_socket.os, "open", side_effect=tracked_open),
            mock.patch.object(virtiofs_socket.os, "stat", side_effect=synthetic_stat),
            mock.patch.object(virtiofs_socket.os, "fstat", side_effect=synthetic_fstat),
        )


class VirtiofsSocketTests(unittest.TestCase):
    @staticmethod
    def identity(index):
        return f"20261109T195256Z-{index:032x}"

    def allocate_with_fixture(self, fixture, run_dir):
        patches = fixture.ownership_patches()
        with patches[0], patches[1], patches[2]:
            return virtiofs_socket.allocate(
                run_dir, fixture.runtime_root, fixture.uid, fixture.system_uid
            )

    def test_long_state_root_still_gets_a_short_socket_path(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()) as temporary:
            fixture = RuntimeFixture(temporary)
            run_dir = fixture.run_dir(self.identity(1))
            # Add enough lexical depth to exceed sun_path while keeping each component legal.
            long_state = run_dir
            for index in range(3):
                long_state = long_state / (f"state-{index}-" + "x" * 45)
                long_state.mkdir(mode=0o700)
            long_run = long_state / self.identity(2)
            long_run.mkdir(mode=0o700)
            path = self.allocate_with_fixture(fixture, long_run)
            self.assertGreater(len(os.fsencode(long_run / "virtiofs.sock")), 107)
            self.assertLessEqual(len(os.fsencode(path)), 107)
            self.assertNotEqual(path.parent.parent, long_run)
            self.assertEqual(path.parent.name, f"lmv3-{long_run.name}")
            metadata = long_run / "virtiofs.socket-path"
            self.assertEqual(metadata.read_text(encoding="utf-8"), str(path) + "\n")
            self.assertEqual(stat.S_IMODE(os.lstat(metadata).st_mode), 0o600)

    def test_auth_control_socket_uses_the_same_private_per_run_directory(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()) as temporary:
            fixture = RuntimeFixture(temporary)
            run_dir = fixture.run_dir(self.identity(20))
            runtime_root = pathlib.Path(f"/run/user/{fixture.uid}")
            virtiofs_path = virtiofs_socket._expected_socket_path(
                run_dir, runtime_root, fixture.uid
            )
            auth_path = virtiofs_socket._expected_auth_control_path(
                run_dir, runtime_root, fixture.uid
            )
            self.assertEqual(auth_path.parent, virtiofs_path.parent)
            self.assertEqual(auth_path.name, "auth-control.sock")
            self.assertLess(len(os.fsencode(auth_path)), 100)
            self.assertFalse(auth_path.exists())

    def test_allocator_refuses_a_preexisting_auth_control_socket_artifact(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()) as temporary:
            fixture = RuntimeFixture(temporary)
            run_dir = fixture.run_dir(self.identity(21))
            socket_dir = fixture.runtime_root / f"lmv3-{run_dir.name}"
            socket_dir.mkdir(mode=0o700)
            (socket_dir / "auth-control.sock").touch(mode=0o600)
            patches = fixture.ownership_patches()
            with patches[0], patches[1], patches[2]:
                with self.assertRaisesRegex(virtiofs_socket.Refusal, "pre-existing short virtiofs socket directory"):
                    virtiofs_socket.allocate(
                        run_dir, fixture.runtime_root, fixture.uid, fixture.system_uid
                    )
            self.assertTrue((socket_dir / "auth-control.sock").exists())

    def test_explicit_overlong_socket_candidate_fails_before_allocation(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()) as temporary:
            fixture = RuntimeFixture(temporary)
            run_dir = fixture.run_dir(self.identity(3))
            long_root = fixture.runtime_root / ("r" * 50) / ("s" * 50)
            long_root.mkdir(parents=True, mode=0o700)
            with self.assertRaisesRegex(virtiofs_socket.Refusal, "at most 107"):
                virtiofs_socket._expected_socket_path(run_dir, long_root, fixture.uid)
            self.assertFalse((run_dir / "virtiofs.socket-path").exists())

    def test_concurrent_runs_allocate_exclusive_distinct_names(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()) as temporary:
            fixture = RuntimeFixture(temporary)
            runs = [fixture.run_dir(self.identity(index)) for index in (4, 5)]
            barrier = threading.Barrier(2)
            results = []
            failures = []

            def allocate_one(run_dir):
                barrier.wait()
                try:
                    results.append(virtiofs_socket.allocate(
                        run_dir, fixture.runtime_root, fixture.uid, fixture.system_uid
                    ))
                except Exception as error:  # pragma: no cover - assertion reports the worker error
                    failures.append(error)

            workers = [threading.Thread(target=allocate_one, args=(run,)) for run in runs]
            patches = fixture.ownership_patches()
            with patches[0], patches[1], patches[2]:
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=5)
            self.assertFalse(any(worker.is_alive() for worker in workers))
            self.assertEqual(failures, [])
            self.assertEqual(len(set(results)), 2)

    def test_same_run_collision_and_preexisting_socket_pid_are_refused(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()) as temporary:
            fixture = RuntimeFixture(temporary)
            run_dir = fixture.run_dir(self.identity(6))
            path = self.allocate_with_fixture(fixture, run_dir)
            (run_dir / "virtiofs.socket-path").unlink()
            patches = fixture.ownership_patches()
            with patches[0], patches[1], patches[2]:
                with self.assertRaisesRegex(virtiofs_socket.Refusal, "collision"):
                    virtiofs_socket.allocate(run_dir, fixture.runtime_root, fixture.uid, fixture.system_uid)

            legacy_run = fixture.run_dir(self.identity(7))
            (legacy_run / "virtiofs.sock.pid").write_text("4242\n", encoding="ascii")
            with self.assertRaisesRegex(virtiofs_socket.Refusal, "pre-existing"):
                self.allocate_with_fixture(fixture, legacy_run)
            self.assertFalse(path.with_suffix(".sock").is_symlink())

    def test_symlink_writable_wrong_owner_and_mode_runtime_ancestry_are_refused(self):
        for attack in ("symlink", "writable", "wrong-owner", "mode"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory(dir=pathlib.Path.home()) as temporary:
                fixture = RuntimeFixture(temporary)
                run_dir = fixture.run_dir(self.identity(8))
                protected = fixture.base / "run"
                wrong_path = None
                if attack == "symlink":
                    (fixture.base / "run/user").rename(fixture.base / "run/user-original")
                    (fixture.base / "run/user").symlink_to(fixture.base / "run/user-original")
                elif attack == "writable":
                    os.chmod(protected, 0o775)
                elif attack == "mode":
                    os.chmod(fixture.runtime_root, 0o750)
                else:
                    wrong_path = protected
                patches = fixture.ownership_patches(wrong_path=wrong_path, wrong_uid=fixture.uid + 1)
                with patches[0], patches[1], patches[2]:
                    with self.assertRaises(virtiofs_socket.Refusal):
                        virtiofs_socket.allocate(run_dir, fixture.runtime_root, fixture.uid, fixture.system_uid)
                self.assertFalse((run_dir / "virtiofs.socket-path").exists())

    def test_startup_and_stopped_inspection_validate_socket_pid_and_never_signal_pid(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()) as temporary:
            fixture = RuntimeFixture(temporary)
            run_dir = fixture.run_dir(self.identity(9))
            socket_path = self.allocate_with_fixture(fixture, run_dir)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            child = subprocess.Popen(["/usr/bin/sleep", "30"])
            try:
                listener.bind(str(socket_path))
                listener.listen(1)
                pid_path = pathlib.Path(str(socket_path) + ".pid")
                pid_path.write_text(f"{child.pid}\n", encoding="ascii")
                os.chmod(pid_path, 0o600)
                patches = fixture.ownership_patches()
                with patches[0], patches[1], patches[2]:
                    startup = virtiofs_socket.inspect(
                        run_dir, fixture.runtime_root, fixture.uid, fixture.system_uid, startup=True
                    )
                self.assertTrue(startup["socket_present"])
                self.assertTrue(startup["pid_present"])
                self.assertIsNone(child.poll())
                patches = fixture.ownership_patches()
                with patches[0], patches[1], patches[2]:
                    with self.assertRaisesRegex(virtiofs_socket.Refusal, "live listener"):
                        virtiofs_socket.inspect(
                            run_dir, fixture.runtime_root, fixture.uid, fixture.system_uid, startup=False
                        )
                listener.close()
                patches = fixture.ownership_patches()
                with patches[0], patches[1], patches[2]:
                    stopped = virtiofs_socket.inspect(
                        run_dir, fixture.runtime_root, fixture.uid, fixture.system_uid, startup=False
                    )
                self.assertTrue(stopped["socket_present"])
                self.assertIsNone(child.poll(), "PID artifact is evidence, never a signal target")
            finally:
                listener.close()
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=5)

    def test_absent_volatile_runtime_dir_is_distinguished_from_a_live_socket(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()) as temporary:
            fixture = RuntimeFixture(temporary)
            run_dir = fixture.run_dir(self.identity(10))
            self.allocate_with_fixture(fixture, run_dir)
            socket_dir = fixture.runtime_root / f"lmv3-{run_dir.name}"
            socket_dir.rmdir()
            patches = fixture.ownership_patches()
            with patches[0], patches[1], patches[2]:
                result = virtiofs_socket.inspect(
                    run_dir, fixture.runtime_root, fixture.uid, fixture.system_uid, startup=False
                )
            self.assertEqual(result["runtime_directory"], "expired")
            self.assertFalse(result["socket_present"])

    def test_virtiofs_log_precreation_is_exclusive_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()) as temporary:
            fixture = RuntimeFixture(temporary)
            run_dir = fixture.run_dir(self.identity(11))
            self.assertEqual(
                virtiofs_socket.main(["create-log", str(run_dir), "virtiofsd.log"]), 0
            )
            with self.assertRaisesRegex(virtiofs_socket.Refusal, "pre-existing"):
                virtiofs_socket.create_log(run_dir, "virtiofsd.log")
            log_path = run_dir / "virtiofsd.log"
            self.assertTrue(stat.S_ISREG(os.lstat(log_path).st_mode))
            self.assertEqual(stat.S_IMODE(os.lstat(log_path).st_mode), 0o600)

        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()) as temporary:
            fixture = RuntimeFixture(temporary)
            run_dir = fixture.run_dir(self.identity(12))
            target = pathlib.Path(temporary) / "target.log"
            target.write_text("must remain untouched", encoding="utf-8")
            (run_dir / "virtiofsd.log").symlink_to(target)
            patches = fixture.ownership_patches()
            with patches[0], patches[1], patches[2]:
                with self.assertRaises(virtiofs_socket.Refusal):
                    virtiofs_socket.create_log(run_dir, "virtiofsd.log")
            self.assertEqual(target.read_text(encoding="utf-8"), "must remain untouched")

    def test_unlinked_live_listener_is_not_misreported_as_stopped_or_expired(self):
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()) as temporary:
            fixture = RuntimeFixture(temporary)
            run_dir = fixture.run_dir(self.identity(13))
            socket_path = self.allocate_with_fixture(fixture, run_dir)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(socket_path))
                listener.listen(1)
                pid_path = pathlib.Path(str(socket_path) + ".pid")
                pid_path.write_text("4242\n", encoding="ascii")
                os.chmod(pid_path, 0o600)
                socket_path.unlink()
                patches = fixture.ownership_patches()
                with patches[0], patches[1], patches[2]:
                    with self.assertRaisesRegex(virtiofs_socket.Refusal, "live listener"):
                        virtiofs_socket.inspect(
                            run_dir, fixture.runtime_root, fixture.uid, fixture.system_uid, startup=False
                        )
                pid_path.unlink()
                socket_path.parent.rmdir()
                patches = fixture.ownership_patches()
                with patches[0], patches[1], patches[2]:
                    with self.assertRaisesRegex(virtiofs_socket.Refusal, "live listener"):
                        virtiofs_socket.inspect(
                            run_dir, fixture.runtime_root, fixture.uid, fixture.system_uid, startup=False
                        )
            finally:
                listener.close()
            patches = fixture.ownership_patches()
            with patches[0], patches[1], patches[2]:
                result = virtiofs_socket.inspect(
                    run_dir, fixture.runtime_root, fixture.uid, fixture.system_uid, startup=False
                )
            self.assertEqual(result["runtime_directory"], "expired")


if __name__ == "__main__":
    unittest.main()
