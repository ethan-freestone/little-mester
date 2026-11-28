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
    def test_metrics_mode_is_fixed_at_each_trusted_launch_boundary(self):
        compose = (ROOT / "compose.yaml").read_text()
        agent = compose.split("\n  agent:\n", 1)[1].split("\n  # ───", 1)[0]
        metrics = compose.split("\n  metrics:\n", 1)[1].split("\n  # ───", 1)[0]
        self.assertIn("METRICS_MODE: docker", agent)
        self.assertNotIn("ports:", metrics)
        self.assertIn("networks: [internal]", metrics)

        contract = (ROOT / "lib/vm/parent-session-contract.py").read_text()
        runner = contract.split('OPENCODE_RUNNER = b"""', 1)[1].split('"""', 1)[0]
        self.assertIn("export METRICS_MODE=vm", runner)

        argv = parent_qemu.build_system_qemu_argv(
            executable="/usr/bin/qemu-system-x86_64",
            disk_path="/var/lib/little-mester-vm/disk.qcow2",
            seed_iso="/var/lib/little-mester-vm/seed.iso", ssh_port=22222,
            proxy_port=32128, qmp_socket="/run/little-mester-vm/qmp.sock",
            vm_name="little-mester-system-vm",
            virtiofs_socket="/run/little-mester-vm-workspace/virtiofs.sock")
        netdev = argv[argv.index("-netdev") + 1]
        self.assertIn("guestfwd=tcp:10.0.2.100:19532-cmd:socat - TCP:127.0.0.1:32129", netdev)
        self.assertIn("restrict=on", netdev)
        self.assertNotIn("hostfwd=tcp:0.0.0.0", netdev)

    def test_shared_network_builder_covers_parent_smoke_and_ci_modes(self):
        self.assertEqual(
            parent_qemu.build_user_netdev(ssh_port=22222, restricted=True, proxy_port=23456),
            "user,id=net0,hostfwd=tcp:127.0.0.1:22222-:22,"
            "guestfwd=tcp:10.0.2.100:3128-cmd:socat - TCP:127.0.0.1:23456,restrict=on")
        self.assertEqual(
            parent_qemu.build_user_netdev(ssh_port=22222, restricted=False),
            "user,id=net0,hostfwd=tcp:127.0.0.1:22222-:22")
        self.assertEqual(
            parent_qemu.build_user_netdev(ssh_port=22222, restricted=True),
            "user,id=net0,restrict=on,hostfwd=tcp:127.0.0.1:22222-:22")

    def test_shared_network_builder_rejects_malformed_policy_inputs(self):
        for kwargs in (
            {"ssh_port": 22, "restricted": False},
            {"ssh_port": 22222, "restricted": True, "proxy_port": 80},
            {"ssh_port": 22222, "restricted": True, "proxy_port": 23456.0},
            {"ssh_port": 22222, "restricted": 1},
            {"ssh_port": 22222, "restricted": False, "proxy_port": 23456},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                parent_qemu.build_user_netdev(**kwargs)

    def test_fixed_system_unit_argv_uses_only_direct_locked_disk_and_shared_network(self):
        argv = parent_qemu.build_system_qemu_argv(
            executable="/usr/bin/qemu-system-x86_64",
            disk_path="/var/lib/little-mester-vm/disk.qcow2",
            seed_iso="/var/lib/little-mester-vm/seed.iso", ssh_port=22222,
            proxy_port=32128, qmp_socket="/run/little-mester-vm/qmp.sock",
            vm_name="little-mester-system-vm",
            virtiofs_socket="/run/little-mester-vm-workspace/virtiofs.sock")
        self.assertEqual(argv[0], "/usr/bin/qemu-system-x86_64")
        self.assertIn("filename=/var/lib/little-mester-vm/disk.qcow2,locking=on", " ".join(argv))
        self.assertNotIn("/dev/fdset", " ".join(argv))
        self.assertIn("guestfwd=tcp:10.0.2.100:3128-cmd:socat - TCP:127.0.0.1:32128", " ".join(argv))
        self.assertIn("vhost-user-fs-pci,chardev=workspace,tag=workspace", " ".join(argv))
        self.assertEqual(argv[argv.index("-m") + 1], "4096")
        self.assertIn("size=4096M", " ".join(argv))
        self.assertIn("guestfwd=tcp:10.0.2.100:19532-cmd:socat - TCP:127.0.0.1:32129", " ".join(argv))
        with self.assertRaises(ValueError):
            parent_qemu.build_system_qemu_argv(
                executable="/usr/bin/qemu-system-x86_64",
                disk_path="/safe/disk.qcow2,readonly=on", seed_iso="/seed.iso",
                ssh_port=22222, proxy_port=32128, qmp_socket="/qmp.sock",
                vm_name="little-mester-system-vm",
                virtiofs_socket="/run/little-mester-vm-workspace/virtiofs.sock")

    def test_profile_system_argv_uses_explicit_overlay_and_read_only_base_fdsets(self):
        argv = parent_qemu.build_system_qemu_argv(
            executable="/usr/bin/qemu-system-x86_64", disk_path=None,
            seed_iso="/var/lib/little-mester-vm/profiles/dev/vm/seed.iso",
            ssh_port=22223, proxy_port=22224,
            qmp_socket="/run/little-mester-vm-profiles/dev/qmp.sock",
            vm_name="little-mester-profile-dev", virtiofs_socket=None,
            disk_fd=7, base_fd=8, cpus=2, memory_mib=4096)
        self.assertEqual(argv[argv.index("-add-fd") + 1],
                         "fd=8,set=1,opaque=profile-base")
        second_add = argv.index("-add-fd", argv.index("-add-fd") + 1)
        self.assertEqual(argv[second_add + 1], "fd=7,set=2,opaque=profile-overlay")
        joined = " ".join(argv)
        self.assertIn("filename=/dev/fdset/1,read-only=on", joined)
        self.assertIn("filename=/dev/fdset/2,locking=on,auto-read-only=off", joined)
        self.assertIn("backing=profile-base-qcow2", joined)
        self.assertIn("drive=profile-overlay-qcow2", joined)
        self.assertNotIn("/var/lib/little-mester-vm/profiles/dev/vm/disk.qcow2", joined)
        self.assertEqual(argv[argv.index("-m") + 1], "4096")

    def test_profile_qemu_fd_graph_rejects_partial_or_unsafe_descriptors(self):
        base = {
            "executable": "/usr/bin/qemu-system-x86_64", "disk_path": None,
            "seed_iso": "/seed.iso", "ssh_port": 22223, "proxy_port": 22224,
            "qmp_socket": "/run/profile/qmp.sock", "vm_name": "profile",
        }
        for overlay_fd, base_fd in ((7, None), (None, 8), (7, 7), (2, 8), (True, 8)):
            with self.subTest(overlay_fd=overlay_fd, base_fd=base_fd), self.assertRaises(ValueError):
                parent_qemu.build_system_qemu_argv(
                    **base, disk_fd=overlay_fd, base_fd=base_fd)
        with self.assertRaisesRegex(ValueError, "must use its verified overlay descriptor"):
            parent_qemu.build_system_qemu_argv(
                **{**base, "disk_path": "/disk.qcow2"}, disk_fd=7, base_fd=8)

    def test_disk_is_fdset_backed_with_explicit_qcow2_and_writer_locking(self):
        argv = parent_qemu.build_qemu_argv(
            executable="/usr/bin/qemu-system-x86_64", seed_iso="/private/parent/seed.iso",
            ssh_port=22222, proxy_port=23456, qmp_socket="/private/parent/qmp.sock")
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
            ssh_port=22222, proxy_port=23456, qmp_socket="/private/parent/qmp.sock")
        netdev = argv[argv.index("-netdev") + 1]
        self.assertIn("hostfwd=tcp:127.0.0.1:22222-:22", netdev)
        self.assertIn("guestfwd=tcp:10.0.2.100:3128-cmd:socat - TCP:127.0.0.1:23456", netdev)
        self.assertIn("restrict=on", netdev)
        self.assertNotIn("virtiofs", " ".join(argv))
        self.assertEqual(argv[argv.index("-display") + 1], "none")
        self.assertEqual(argv[argv.index("-serial") + 1], "stdio")
        self.assertEqual(argv[argv.index("-qmp") + 1],
                         "unix:/private/parent/qmp.sock,server=on,wait=off")

    def test_qemu_builder_is_a_protected_launcher_dependency(self):
        lab = (ROOT / "lab-vm").read_text()
        self.assertIn('root / "lib/vm/parent-qemu.py"', lab)
        self.assertEqual(os.lstat(ROOT / "lib/vm/parent-qemu.py").st_mode & 0o170000, 0o100000)

    def test_refuses_untrusted_executable_paths_seed_option_injection_and_invalid_ports(self):
        with self.assertRaises(ValueError):
            parent_qemu.build_qemu_argv(executable="qemu-system-x86_64", seed_iso="/seed.iso",
                                        ssh_port=22222, proxy_port=23456, qmp_socket="/qmp.sock")
        with self.assertRaises(ValueError):
            parent_qemu.build_qemu_argv(executable="/usr/bin/qemu-system-x86_64",
                                        seed_iso="/seed.iso,format=qcow2", ssh_port=22222, proxy_port=23456,
                                        qmp_socket="/qmp.sock")
        with self.assertRaises(ValueError):
            parent_qemu.build_qemu_argv(executable="/usr/bin/qemu-system-x86_64", seed_iso="/seed.iso",
                                        ssh_port=22, proxy_port=23456, qmp_socket="/qmp.sock")
        with self.assertRaises(ValueError):
            parent_qemu.build_qemu_argv(executable="/usr/bin/qemu-system-x86_64", seed_iso="/seed.iso",
                                        ssh_port=22222, proxy_port=23456, qmp_socket="/tmp/qmp.sock,server=on")


if __name__ == "__main__":
    unittest.main()
