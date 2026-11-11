#!/usr/bin/env python3
"""MOCK/static tests for the persistent-parent QEMU argv builder."""

import importlib.util
import os
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("parent_qemu", ROOT / "lib/vm/parent-qemu.py")
parent_qemu = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(parent_qemu)


class ParentQemuArgvTests(unittest.TestCase):
    def test_disk_is_fdset_backed_with_explicit_qcow2_and_writer_locking(self):
        argv = parent_qemu.build_qemu_argv(
            executable="/usr/bin/qemu-system-x86_64", seed_iso="/private/parent/seed.iso",
            ssh_port=22222, proxy_port=23456)
        self.assertEqual(argv[argv.index("-add-fd") + 1], "fd=3,set=1,opaque=parent-disk")
        file_node = argv[argv.index("-blockdev") + 1]
        self.assertIn("driver=file", file_node)
        self.assertIn("filename=/dev/fdset/1", file_node)
        self.assertIn("locking=on", file_node)
        self.assertIn("auto-read-only=off", file_node)
        qcow2_node = argv[argv.index("-blockdev", argv.index("-blockdev") + 1) + 1]
        self.assertIn("driver=qcow2", qcow2_node)
        self.assertIn("file=parent-file", qcow2_node)
        self.assertEqual(qcow2_node.split(",", 1)[0], "driver=qcow2")  # Explicit format node, not probing.
        self.assertEqual(argv[argv.index("-device") + 1], "virtio-blk-pci,drive=parent-qcow2")
        self.assertFalse(any("disk.qcow2" in arg for arg in argv))
        seed_drive = argv[argv.index("-drive") + 1]
        self.assertIn("format=raw", seed_drive)
        self.assertIn("readonly=on", seed_drive)
        self.assertEqual(argv[argv.index("-device", argv.index("-device") + 1) + 1], "virtio-scsi-pci,id=scsi0")
        self.assertIn("-device", argv)
        self.assertIn("scsi-cd,drive=cloud", argv)

    def test_guest_egress_and_ssh_are_loopback_forwarded_with_qemu_restrict(self):
        argv = parent_qemu.build_qemu_argv(
            executable="/usr/bin/qemu-system-x86_64", seed_iso="/private/parent/seed.iso",
            ssh_port=22222, proxy_port=23456)
        netdev = argv[argv.index("-netdev") + 1]
        self.assertIn("hostfwd=tcp:127.0.0.1:22222-:22", netdev)
        self.assertIn("guestfwd=tcp:10.0.2.100:3128-cmd:socat - TCP:127.0.0.1:23456", netdev)
        self.assertIn("restrict=on", netdev)
        self.assertNotIn("virtiofs", " ".join(argv))
        self.assertEqual(argv[argv.index("-display") + 1], "none")

    def test_qemu_builder_is_a_protected_launcher_dependency(self):
        lab = (ROOT / "lab-vm").read_text()
        self.assertIn('root / "lib/vm/parent-qemu.py"', lab)
        self.assertEqual(os.lstat(ROOT / "lib/vm/parent-qemu.py").st_mode & 0o170000, 0o100000)

    def test_refuses_untrusted_executable_paths_seed_option_injection_and_invalid_ports(self):
        with self.assertRaises(ValueError):
            parent_qemu.build_qemu_argv(executable="qemu-system-x86_64", seed_iso="/seed.iso",
                                        ssh_port=22222, proxy_port=23456)
        with self.assertRaises(ValueError):
            parent_qemu.build_qemu_argv(executable="/usr/bin/qemu-system-x86_64",
                                        seed_iso="/seed.iso,format=qcow2", ssh_port=22222, proxy_port=23456)
        with self.assertRaises(ValueError):
            parent_qemu.build_qemu_argv(executable="/usr/bin/qemu-system-x86_64", seed_iso="/seed.iso",
                                        ssh_port=22, proxy_port=23456)


if __name__ == "__main__":
    unittest.main()
