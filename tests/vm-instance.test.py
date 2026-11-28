#!/usr/bin/env python3
"""Validated fixed and named VM instance descriptors."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("vm_instance", ROOT / "lib/vm/vm-instance.py")
instance_module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = instance_module
SPEC.loader.exec_module(instance_module)


class InstanceDescriptorTests(unittest.TestCase):
    def profile(self, **changes):
        values = {
            "name": "dev", "owner_uid": 1000, "runtime_sha": "a" * 40,
            "vm_uid": 400, "vm_gid": 401, "proxy_uid": 402, "proxy_gid": 403,
            "base_source_sha256": "b" * 64,
            "base_manifest_sha256": "c" * 64, "ssh_port": 22223,
        }
        values.update(changes)
        return instance_module.Instance.profile(**values)

    def test_singleton_descriptor_preserves_existing_identity_paths_ports_and_resources(self):
        instance = instance_module.Instance.singleton()
        self.assertEqual(instance.kind, "singleton")
        self.assertEqual(instance.name, "little-mester-system-vm")
        self.assertEqual(instance.vm_user, "lm-vm")
        self.assertEqual(instance.proxy_user, "lm-vm-proxy")
        self.assertEqual(instance.state_root, pathlib.Path("/var/lib/little-mester-vm"))
        self.assertEqual(instance.config_root, pathlib.Path("/etc/little-mester-vm"))
        self.assertEqual(instance.disk, pathlib.Path("/var/lib/little-mester-vm/disk.qcow2"))
        self.assertEqual(instance.seed, pathlib.Path("/var/lib/little-mester-vm/seed.iso"))
        self.assertEqual(instance.control_key, pathlib.Path("/etc/little-mester-vm/control_ed25519"))
        self.assertEqual(instance.known_hosts, pathlib.Path("/etc/little-mester-vm/known_hosts"))
        self.assertEqual(instance.qmp_socket, pathlib.Path("/run/little-mester-vm/qmp.sock"))
        self.assertEqual(instance.proxy_port_file, pathlib.Path("/run/lm-vm-proxy/port"))
        self.assertEqual(instance.workspace_socket,
                         pathlib.Path("/run/little-mester-vm-workspace/virtiofs.sock"))
        self.assertEqual((instance.ssh_port, instance.proxy_port, instance.metrics_port),
                         (22222, 32128, 32129))
        self.assertEqual((instance.cpus, instance.memory_mib), (2, 4096))
        self.assertEqual(instance.base_path, None)

    def test_profile_paths_service_names_and_port_block_are_derived(self):
        instance = self.profile(workspace_path="/home/owner/project")
        self.assertEqual(instance.kind, "profile")
        self.assertEqual(instance.state_root, pathlib.Path("/var/lib/little-mester-vm/profiles/dev"))
        self.assertEqual(instance.config_root, pathlib.Path("/etc/little-mester-vm/profiles/dev"))
        self.assertEqual(instance.disk, instance.state_root / "vm/disk.qcow2")
        self.assertEqual(instance.seed, instance.state_root / "vm/seed.iso")
        self.assertEqual(instance.control_key, instance.state_root / "client/control_ed25519")
        self.assertEqual(instance.host_key, instance.state_root / "vm/ssh_host_ed25519_key")
        self.assertEqual(instance.known_hosts, instance.state_root / "client/known_hosts")
        self.assertEqual(instance.qmp_socket,
                         pathlib.Path("/run/little-mester-vm-profiles/dev/vm/qmp.sock"))
        self.assertEqual(instance.workspace_socket,
                         pathlib.Path("/run/little-mester-vm-profiles/dev/workspace/virtiofs.sock"))
        self.assertEqual((instance.ssh_port, instance.proxy_port, instance.metrics_port),
                         (22223, 22224, 22225))
        self.assertEqual(instance.base_path,
                         pathlib.Path("/var/lib/little-mester-vm/bases") / ("b" * 64) / "base.qcow2")
        self.assertTrue(instance.vm_user.startswith("lmv-"))
        self.assertTrue(instance.proxy_user.startswith("lmp-"))
        self.assertNotEqual(instance.vm_user, instance.proxy_user)
        self.assertEqual(len({instance.vm_unit, instance.proxy_unit,
                              instance.workspace_unit, instance.metrics_unit}), 4)
        self.assertEqual(instance.workspace_path, pathlib.Path("/home/owner/project"))

    def test_profile_resources_and_accounts_are_independent_of_mapping_paths(self):
        first = self.profile()
        other = self.profile(name="test", owner_uid=1001, ssh_port=22226,
                             vm_uid=410, vm_gid=411, proxy_uid=412, proxy_gid=413)
        self.assertNotEqual(first.vm_user, other.vm_user)
        self.assertNotEqual(first.vm_unit, other.vm_unit)
        self.assertNotEqual(first.disk, other.disk)
        self.assertEqual(first.runtime_sha, "a" * 40)
        self.assertEqual(first.cpus, 2)
        self.assertEqual(first.memory_mib, 4096)
        self.assertEqual(other.proxy_port, 22227)
        self.assertEqual(other.metrics_port, 22228)

    def test_profile_descriptor_refuses_malformed_or_unbounded_record_values(self):
        for changes in (
                {"name": "../dev"}, {"name": "system"}, {"name": "Upper"},
                {"runtime_sha": "a" * 39}, {"runtime_sha": "A" * 40},
                {"owner_uid": True}, {"owner_uid": 0}, {"owner_uid": 2**64},
                {"vm_uid": 1000}, {"proxy_uid": 400}, {"proxy_gid": 401},
                {"base_source_sha256": "../" + "b" * 61},
                {"base_manifest_sha256": "C" * 64},
                {"ssh_port": 22224}, {"ssh_port": 23221},
                {"cpus": 0}, {"cpus": True}, {"memory_mib": 65537},
                {"memory_mib": False}, {"workspace_path": "relative/path"},
                {"workspace_path": "/tmp/x\nExecStart=/bin/sh"}):
            with self.subTest(changes=changes), self.assertRaises(instance_module.InvalidInstance):
                self.profile(**changes)


if __name__ == "__main__":
    unittest.main()
