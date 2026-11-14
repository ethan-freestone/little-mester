#!/usr/bin/env python3
"""MOCK tests for one-time sparse parent-disk creation and qcow2 validation."""

import importlib.util
import hashlib
import json
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE_SPEC = importlib.util.spec_from_file_location("parent_state", ROOT / "lib/vm/parent-state.py")
parent_state = importlib.util.module_from_spec(STATE_SPEC)
STATE_SPEC.loader.exec_module(parent_state)
PROVISION_SPEC = importlib.util.spec_from_file_location("parent_provision", ROOT / "lib/vm/parent-provision.py")
parent_provision = importlib.util.module_from_spec(PROVISION_SPEC)
PROVISION_SPEC.loader.exec_module(parent_provision)


class ParentProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.temp.name) / "home"
        self.home.mkdir(mode=0o700)
        os.chmod(self.home, 0o700)
        self.state_dir = self.home / ".local/state/little-mester/parent-v3/default"
        self.parent = parent_state.initialize(64, home=self.home)
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def fake_run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[0] == "/usr/bin/curl":
            os.write(kwargs["stdout"], b"pinned-image-fixture")
            return subprocess.CompletedProcess(argv, 0)
        if argv[1] == "info":
            path = argv[-1]
            size = (32 * 1024**3 if "base-image" in path else 64 * 1024**3)
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({
                "format": "qcow2", "virtual-size": size, "actual-size": 4096,
                "backing-filename": "", "data-file": "", "data-file-raw": False,
            }).encode())
        if argv[1] == "convert":
            pathlib.Path(argv[-1]).write_bytes(b"flattened-qcow2-fixture")
            return subprocess.CompletedProcess(argv, 0)
        if argv[1] == "resize":
            return subprocess.CompletedProcess(argv, 0)
        raise AssertionError(f"unexpected command: {argv}")

    def test_creation_flattens_and_publishes_private_disk_with_configured_initial_size(self):
        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            with mock.patch.object(parent_provision.subprocess, "run", side_effect=self.fake_run):
                with (mock.patch.object(parent_provision, "IMAGE_SHA256",
                                        hashlib.sha256(b"pinned-image-fixture").hexdigest()),
                      mock.patch.object(parent_provision, "_validate_host_tool")):
                    result = parent_provision.create_initial_disk(
                        state=parent_state, capacity_gib=64, qemu_img="/usr/bin/qemu-img",
                        curl="/usr/bin/curl", lock_fd=lock_fd, home=self.home)
        disk = self.state_dir / "disk.qcow2"
        info = disk.stat()
        self.assertTrue(result["disk"]["provisioned"])
        self.assertEqual(result["disk"]["format"], "qcow2")
        self.assertEqual(result["disk"]["virtual_capacity_gib"], 64)
        self.assertEqual((result["disk"]["device"], result["disk"]["inode"]), (info.st_dev, info.st_ino))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertFalse((self.state_dir / "base-image.qcow2").exists())
        self.assertFalse((self.state_dir / "disk.qcow2.part").exists())
        self.assertIn("-f", self.calls[1][0])
        convert_call = next((argv, kwargs) for argv, kwargs in self.calls if len(argv) > 1 and argv[1] == "convert")
        output_fd = int(convert_call[0][-1].rsplit("/", 1)[1])
        self.assertTrue(convert_call[0][-1].startswith("/proc/self/fd/"))
        self.assertIn(output_fd, convert_call[1]["pass_fds"])
        resize = next(argv for argv, _kwargs in self.calls if len(argv) > 1 and argv[1] == "resize")
        self.assertEqual(resize[-1], "64G")
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")
        state_fd = parent_state._open_directory_chain(create=False, home_override=self.home)
        try:
            disk_status = parent_state._disk_status(state_fd, result)
            self.assertEqual(disk_status["host_allocated_bytes"], info.st_blocks * 512)
        finally:
            os.close(state_fd)
        with (self.state_dir / "base-image.qcow2").open("wb") as leftover:
            leftover.write(b"unexpected leftover")
        os.chmod(self.state_dir / "base-image.qcow2", 0o600)
        state_fd = parent_state._open_directory_chain(create=False, home_override=self.home)
        try:
            with self.assertRaises(parent_state.Refused):
                parent_state._disk_status(state_fd, result)
        finally:
            os.close(state_fd)
        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            with mock.patch.object(parent_provision.subprocess, "run", side_effect=self.fake_run) as runner:
                with mock.patch.object(parent_provision, "_validate_host_tool"):
                    with self.assertRaises(parent_provision.Refused):
                        parent_provision.create_initial_disk(
                            state=parent_state, capacity_gib=64, qemu_img="/usr/bin/qemu-img",
                            curl="/usr/bin/curl", lock_fd=lock_fd, home=self.home)
            runner.assert_not_called()

    def test_existing_disk_is_idempotent_and_never_runs_creation_resize(self):
        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            with (mock.patch.object(parent_provision.subprocess, "run", side_effect=self.fake_run) as runner,
                  mock.patch.object(parent_provision, "IMAGE_SHA256",
                                   hashlib.sha256(b"pinned-image-fixture").hexdigest()),
                  mock.patch.object(parent_provision, "_validate_host_tool")):
                created = parent_provision.create_initial_disk(
                    state=parent_state, capacity_gib=64, qemu_img="/usr/bin/qemu-img",
                    curl="/usr/bin/curl", lock_fd=lock_fd, home=self.home)
                identity = (created["disk"]["device"], created["disk"]["inode"])
                self.calls.clear()
                runner.reset_mock()
                existing = parent_provision.create_initial_disk(
                    state=parent_state, capacity_gib=64, qemu_img="/usr/bin/qemu-img",
                    curl="/usr/bin/curl", lock_fd=lock_fd, home=self.home)
                self.assertEqual((existing["disk"]["device"], existing["disk"]["inode"]), identity)
                self.assertEqual(runner.call_count, 1)
                self.assertEqual(runner.call_args.args[0][1], "info")
                with self.assertRaises(parent_provision.Refused):
                    parent_provision.create_initial_disk(
                        state=parent_state, capacity_gib=32, qemu_img="/usr/bin/qemu-img",
                        curl="/usr/bin/curl", lock_fd=lock_fd, home=self.home)

    def test_rejects_backing_files_external_data_files_wrong_format_and_wrong_capacity(self):
        for data in (
                {"format": "raw", "virtual-size": 64 * 1024**3},
                {"format": "qcow2", "virtual-size": 64 * 1024**3, "backing-filename": "/tmp/base"},
                {"format": "qcow2", "virtual-size": 64 * 1024**3,
                 "format-specific": {"type": "qcow2", "data": {"data-file": "/tmp/data"}}},
                {"format": "qcow2", "virtual-size": 64 * 1024**3,
                 "format-specific": {"type": "qcow2", "data": {"data-file-raw": True}}},
                {"format": "qcow2", "virtual-size": 32 * 1024**3}):
            with self.subTest(data=data):
                with self.assertRaises(parent_provision.Refused):
                    parent_provision.validate_qcow2_info(data, 64 * 1024**3)

    def test_failed_image_conversion_preserves_partial_evidence_and_never_publishes_disk(self):
        def fail_convert(argv, **kwargs):
            if argv[0] == "/usr/bin/curl":
                os.write(kwargs["stdout"], b"pinned-image-fixture")
                return subprocess.CompletedProcess(argv, 0)
            if argv[1] == "info":
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({
                    "format": "qcow2", "virtual-size": 32 * 1024**3,
                }).encode())
            if argv[1] == "convert":
                return subprocess.CompletedProcess(argv, 1)
            raise AssertionError(argv)

        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            with mock.patch.object(parent_provision.subprocess, "run", side_effect=fail_convert):
                with (mock.patch.object(parent_provision, "IMAGE_SHA256",
                                        hashlib.sha256(b"pinned-image-fixture").hexdigest()),
                      mock.patch.object(parent_provision, "_validate_host_tool")):
                    with self.assertRaises(parent_provision.Refused):
                        parent_provision.create_initial_disk(
                            state=parent_state, capacity_gib=64, qemu_img="/usr/bin/qemu-img",
                            curl="/usr/bin/curl", lock_fd=lock_fd, home=self.home)
        self.assertTrue((self.state_dir / "base-image.qcow2").exists())
        self.assertFalse((self.state_dir / "disk.qcow2").exists())
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")


if __name__ == "__main__":
    unittest.main()
