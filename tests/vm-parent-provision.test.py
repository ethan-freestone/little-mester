#!/usr/bin/env python3
"""MOCK tests for one-time sparse parent-disk creation and qcow2 validation."""

import importlib.util
import hashlib
import json
import os
import pathlib
import shutil
import stat
import struct
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

    def test_profile_overlay_validator_requires_exact_base_and_closed_qcow2_features(self):
        base = "/var/lib/little-mester-vm/bases/approved/base.qcow2"
        valid = {
            "format": "qcow2", "virtual-size": 64 * 1024**3,
            "cluster-size": 65536,
            "backing-filename": base, "full-backing-filename": base,
            "backing-filename-format": "qcow2",
            "format-specific": {"type": "qcow2", "data": {
                "compat": "1.1", "compression-type": "zlib",
                "refcount-bits": 16, "lazy-refcounts": False,
                "corrupt": False, "extended-l2": False,
            }},
        }
        self.assertEqual(parent_provision.validate_profile_overlay_info(
            valid, expected_base=base), parent_provision.PROFILE_OVERLAY_SIZE)
        for change in (
                {"backing-filename": "/tmp/foreign.qcow2"},
                {"full-backing-filename": "/tmp/foreign.qcow2"},
                {"backing-filename-format": "raw"},
                {"data-file": "/tmp/external-data"},
                {"cluster-size": 131072},
                {"format-specific": {"data": {**valid["format-specific"]["data"],
                                                "extended-l2": True}}},
                {"format-specific": {"data": {**valid["format-specific"]["data"],
                                                "corrupt": True}}}):
            with self.subTest(change=change), self.assertRaises(parent_provision.Refused):
                parent_provision.validate_profile_overlay_info(
                    {**valid, **change}, expected_base=base)

    def test_approved_base_validator_requires_exact_pinned_image_features(self):
        valid = {
            "format": "qcow2", "virtual-size": parent_provision.PROFILE_BASE_VIRTUAL_SIZE,
            "cluster-size": 65536, "format-specific": {"type": "qcow2", "data": {
                "compat": "1.1", "compression-type": "zlib", "refcount-bits": 16,
                "lazy-refcounts": False, "corrupt": False, "extended-l2": False,
            }},
        }
        self.assertEqual(parent_provision.validate_profile_base_info(valid),
                         parent_provision.PROFILE_BASE_VIRTUAL_SIZE)
        for change in (
                {"backing-filename": "/tmp/other.qcow2"},
                {"full-backing-filename": "/tmp/other.qcow2"},
                {"data-file": "/tmp/data"},
                {"format-specific": {"type": "qcow2", "data": {
                    **valid["format-specific"]["data"], "corrupt": True}}},
                {"format-specific": {"type": "qcow2", "data": {
                    **valid["format-specific"]["data"], "lazy-refcounts": True}}},
                {"format-specific": {"type": "qcow2", "data": {
                    **valid["format-specific"]["data"], "refcount-bits": 32}}}):
            with self.subTest(change=change), self.assertRaises(parent_provision.Refused):
                parent_provision.validate_profile_base_info({**valid, **change})

    def test_fd_only_overlay_inspector_accepts_fixture_without_spawning_or_opening_paths(self):
        with tempfile.TemporaryDirectory(prefix="vm-profile-header-fixture-") as temporary:
            root = pathlib.Path(temporary)
            base = root / "approved-base.qcow2"
            base.write_bytes(b"not opened by the header inspector")
            overlay = root / "overlay.qcow2"
            path = str(base).encode("utf-8")
            backing_offset = 528
            header = struct.pack(
                ">IIQI I Q I I Q Q I I Q Q Q Q I I",
                parent_provision.PROFILE_QCOW_MAGIC, 3,
                backing_offset, len(path), 16, parent_provision.PROFILE_OVERLAY_SIZE,
                0, 128, 196608, 65536, 1, 0, 0,
                0, 0, 0, 4, parent_provision.PROFILE_QCOW_HEADER_LENGTH)
            self.assertEqual(len(header), 104)
            feature_table = parent_provision._profile_feature_table()
            extension_bytes = (
                struct.pack(">II", parent_provision.PROFILE_QCOW_BACKING_FORMAT, 5)
                + b"qcow2" + b"\0" * 3
                + struct.pack(">II", parent_provision.PROFILE_QCOW_FEATURE_TABLE,
                              len(feature_table))
                + feature_table
                + struct.pack(">II", 0, 0))
            self.assertEqual(len(extension_bytes), backing_offset - 112)
            fd = os.open(overlay, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.ftruncate(fd, 262144)
                os.pwrite(fd, header + b"\0" * 8, 0)
                os.pwrite(fd, extension_bytes, 112)
                os.pwrite(fd, path, backing_offset)
                with mock.patch.object(parent_provision.os, "open",
                                       side_effect=AssertionError("inspector opened a path")), \
                        mock.patch.object(parent_provision.subprocess, "run",
                                          side_effect=AssertionError("inspector launched a tool")):
                    inspected = parent_provision.inspect_profile_overlay_fd(
                        fd, expected_base=str(base), expected_uid=os.getuid())
                self.assertEqual(inspected["virtual_size"], parent_provision.PROFILE_OVERLAY_SIZE)
                self.assertEqual(inspected["backing_file"], str(base))

                mutations = (
                    (112, (0x44415441).to_bytes(4, "big"), "unsupported qcow2 extension"),
                    (72, (1 << 2).to_bytes(8, "big"), "unsupported geometry or features"),
                    (104, b"X" + b"\0" * 7, "unsupported geometry or features"),
                    (backing_offset, b"/" + b"x" * (len(path) - 1), "backing reference"),
                )
                restores = (extension_bytes[:4], b"\0" * 8, b"\0" * 8, path)
                offsets = (112, 72, 104, backing_offset)
                for (mutation_offset, changed, message), original, restore_offset in zip(
                        mutations, restores, offsets, strict=True):
                    os.pwrite(fd, changed, mutation_offset)
                    with self.assertRaisesRegex(parent_provision.Refused, message):
                        parent_provision.inspect_profile_overlay_fd(fd, expected_base=str(base))
                    os.pwrite(fd, original, restore_offset)
            finally:
                os.close(fd)

    @unittest.skipUnless(shutil.which("qemu-img") and shutil.which("qemu-io"),
                         "qemu-img and qemu-io are required for the real overlay probe")
    def test_real_sparse_overlays_are_independent_and_preserve_the_base(self):
        qemu_img = shutil.which("qemu-img")
        qemu_io = shutil.which("qemu-io")
        assert qemu_img and qemu_io
        with tempfile.TemporaryDirectory(prefix="vm-profile-overlay-") as temporary:
            root = pathlib.Path(temporary)
            base = root / "base.qcow2"
            created = subprocess.run(
                [qemu_img, "create", "-q", "-f", "qcow2", str(base), "1G"],
                capture_output=True, timeout=20, check=False)
            self.assertEqual(created.returncode, 0, created.stderr.decode(errors="replace"))
            base.chmod(0o444)
            base_digest = hashlib.sha256(base.read_bytes()).hexdigest()
            base_virtual_size = 1024**3
            vm_a = root / "vm-a"
            vm_b = root / "vm-b"
            vm_a.mkdir(mode=0o700)
            vm_b.mkdir(mode=0o700)
            fds = [os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                   for path in (vm_a, vm_b)]
            try:
                with mock.patch.object(parent_provision, "IMAGE_SHA256", base_digest), \
                        mock.patch.object(parent_provision, "PROFILE_BASE_VIRTUAL_SIZE",
                                          base_virtual_size):
                    results = [parent_provision.create_profile_overlay(
                        base_path=str(base), base_sha256=base_digest,
                        base_virtual_size=base_virtual_size, directory_fd=directory_fd,
                        directory_uid=os.getuid(), directory_gid=os.getgid(),
                        qemu_img=qemu_img)
                        for directory_fd in fds]
                overlay_fd = os.open("disk.qcow2", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                                     dir_fd=fds[0])
                try:
                    with mock.patch.object(parent_provision.os, "open",
                                           side_effect=AssertionError("inspector reopened a path")), \
                            mock.patch.object(parent_provision.subprocess, "run",
                                              side_effect=AssertionError("inspector launched a tool")):
                        observed = parent_provision.inspect_profile_overlay_fd(
                            overlay_fd, expected_base=str(base),
                            expected_identity=(results[0]["device"], results[0]["inode"]),
                            expected_uid=os.getuid())
                    self.assertEqual(observed["virtual_size"], 64 * 1024**3)
                    self.assertEqual(observed["backing_file"], str(base))

                    header = os.pread(overlay_fd, 112, 0)
                    backing_offset = int.from_bytes(header[8:16], "big")
                    backing_length = int.from_bytes(header[16:20], "big")
                    original_backing = os.pread(overlay_fd, backing_length, backing_offset)
                    poisoned = (b"/" + b"x" * (backing_length - 1))
                    self.assertEqual(len(poisoned), backing_length)
                    os.pwrite(overlay_fd, poisoned, backing_offset)
                    with self.assertRaisesRegex(parent_provision.Refused, "backing reference"):
                        parent_provision.inspect_profile_overlay_fd(
                            overlay_fd, expected_base=str(base))
                    os.pwrite(overlay_fd, original_backing, backing_offset)

                    extension = os.pread(overlay_fd, 8, 112)
                    external_data_extension = (0x44415441).to_bytes(4, "big") + extension[4:]
                    os.pwrite(overlay_fd, external_data_extension, 112)
                    with self.assertRaisesRegex(parent_provision.Refused, "unsupported qcow2 extension"):
                        parent_provision.inspect_profile_overlay_fd(
                            overlay_fd, expected_base=str(base))
                    os.pwrite(overlay_fd, extension, 112)

                    os.pwrite(overlay_fd, (1 << 2).to_bytes(8, "big"), 72)
                    with self.assertRaisesRegex(parent_provision.Refused, "unsupported geometry or features"):
                        parent_provision.inspect_profile_overlay_fd(
                            overlay_fd, expected_base=str(base))
                    os.pwrite(overlay_fd, header[72:80], 72)

                    read_only_fd = os.open("disk.qcow2", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                           dir_fd=fds[0])
                    try:
                        with self.assertRaisesRegex(parent_provision.Refused, "descriptor identity"):
                            parent_provision.inspect_profile_overlay_fd(
                                read_only_fd, expected_base=str(base))
                    finally:
                        os.close(read_only_fd)
                finally:
                    os.close(overlay_fd)
            finally:
                for directory_fd in fds:
                    os.close(directory_fd)

            self.assertEqual(results[0]["virtual_size"], 64 * 1024**3)
            self.assertEqual(results[1]["virtual_size"], 64 * 1024**3)
            self.assertLess(results[0]["allocated_bytes"], 64 * 1024**3)
            self.assertLess(results[1]["allocated_bytes"], 64 * 1024**3)
            self.assertNotEqual(results[0]["inode"], results[1]["inode"])
            self.assertEqual(stat.S_IMODE((vm_a / "disk.qcow2").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((vm_b / "disk.qcow2").stat().st_mode), 0o600)

            write = subprocess.run(
                [qemu_io, "-f", "qcow2", "-c", "write -P 0x5a 0 4096",
                 str(vm_a / "disk.qcow2")], capture_output=True, timeout=20, check=False)
            self.assertEqual(write.returncode, 0, write.stderr.decode(errors="replace"))
            read_a = subprocess.run(
                [qemu_io, "-f", "qcow2", "-r", "-c", "read -P 0x5a 0 4096",
                 str(vm_a / "disk.qcow2")], capture_output=True, timeout=20, check=False)
            read_b = subprocess.run(
                [qemu_io, "-f", "qcow2", "-r", "-c", "read -P 0x00 0 4096",
                 str(vm_b / "disk.qcow2")], capture_output=True, timeout=20, check=False)
            self.assertEqual(read_a.returncode, 0, read_a.stderr.decode(errors="replace"))
            self.assertEqual(read_b.returncode, 0, read_b.stderr.decode(errors="replace"))
            self.assertEqual(hashlib.sha256(base.read_bytes()).hexdigest(), base_digest)

    def test_profile_overlay_refuses_bad_digest_or_preexisting_leaf_without_mutation(self):
        with tempfile.TemporaryDirectory(prefix="vm-profile-overlay-refusal-") as temporary:
            root = pathlib.Path(temporary)
            base = root / "base.qcow2"
            base.write_bytes(b"fixture")
            base.chmod(0o444)
            directory = root / "vm"
            directory.mkdir(mode=0o700)
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                with mock.patch.object(parent_provision, "_validate_host_tool"), \
                        mock.patch.object(parent_provision, "IMAGE_SHA256",
                                          hashlib.sha256(b"fixture").hexdigest()), \
                        mock.patch.object(parent_provision, "PROFILE_BASE_VIRTUAL_SIZE", 1024):
                    with self.assertRaisesRegex(parent_provision.Refused, "exact approved base"):
                        parent_provision.create_profile_overlay(
                            base_path=str(base), base_sha256="0" * 64,
                            base_virtual_size=1024, directory_fd=fd,
                            directory_uid=os.getuid(), directory_gid=os.getgid())
                    existing = directory / "disk.qcow2"
                    existing.write_bytes(b"foreign")
                    with self.assertRaisesRegex(parent_provision.Refused, "pre-existing"):
                        parent_provision.create_profile_overlay(
                            base_path=str(base), base_sha256=hashlib.sha256(b"fixture").hexdigest(),
                            base_virtual_size=1024, directory_fd=fd,
                            directory_uid=os.getuid(), directory_gid=os.getgid())
                    self.assertEqual(existing.read_bytes(), b"foreign")
            finally:
                os.close(fd)

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
