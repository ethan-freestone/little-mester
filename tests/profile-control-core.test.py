#!/usr/bin/env python3
"""Closed v2 mapping and protected instance descriptor regressions."""

from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import sys
import unittest
import uuid


ROOT = pathlib.Path(__file__).resolve().parents[1]
CORE_SPEC = importlib.util.spec_from_file_location(
    "profile_control_core", ROOT / "lib/vm/profile-control-core.py")
core = importlib.util.module_from_spec(CORE_SPEC)
assert CORE_SPEC.loader is not None
sys.modules[CORE_SPEC.name] = core
CORE_SPEC.loader.exec_module(core)
PROVISION_SPEC = importlib.util.spec_from_file_location(
    "profile_core_parent_provision", ROOT / "lib/vm/parent-provision.py")
provision = importlib.util.module_from_spec(PROVISION_SPEC)
assert PROVISION_SPEC.loader is not None
sys.modules[PROVISION_SPEC.name] = provision
PROVISION_SPEC.loader.exec_module(provision)
INSTANCE_SPEC = importlib.util.spec_from_file_location(
    "profile_core_vm_instance", ROOT / "lib/vm/vm-instance.py")
instance_policy = importlib.util.module_from_spec(INSTANCE_SPEC)
assert INSTANCE_SPEC.loader is not None
sys.modules[INSTANCE_SPEC.name] = instance_policy
INSTANCE_SPEC.loader.exec_module(instance_policy)


def ready_mapping() -> dict:
    instance = instance_policy.Instance.profile(
        name="dev", owner_uid=1000, runtime_sha="a" * 40,
        vm_uid=400, vm_gid=401, proxy_uid=402, proxy_gid=403,
        base_source_sha256=provision.IMAGE_SHA256,
        base_manifest_sha256="b" * 64, ssh_port=22223)
    identity = {"device": 1, "inode": 101}
    digest_identity = {**identity, "sha256": "c" * 64}
    return {
        "schema_version": 2, "name": "dev", "owner_uid": 1000,
        "state": "ready", "transaction": None,
        "runtime": {"sha": "a" * 40, "manifest_sha256": "d" * 64,
                    "helper_api": 1, "runtime_api": 1},
        "base": {"source_sha256": provision.IMAGE_SHA256,
                 "manifest_sha256": "b" * 64},
        "resources": {"cpus": 2, "memory_mib": 4096, "ssh_port": 22223},
        "accounts": {
            "vm": {"name": instance.vm_user, "uid": 400, "gid": 401},
            "proxy": {"name": instance.proxy_user, "uid": 402, "gid": 403},
        },
        "units": {"vm": instance.vm_unit, "proxy": instance.proxy_unit,
                  "workspace": instance.workspace_unit, "metrics": instance.metrics_unit},
        "workspace": None,
        "provisioning": {
            "generation": str(uuid.uuid4()),
            "artifacts": {
                "disk": {"device": 1, "inode": 201, "virtual_size": 64 * 1024**3,
                         "allocated_bytes": 200704},
                "seed": digest_identity,
                "host_private": {"device": 1, "inode": 202},
                "host_public": {**digest_identity, "inode": 203},
                "client_private": {"device": 1, "inode": 204},
                "client_public": {**digest_identity, "inode": 205},
                "known_hosts": {**digest_identity, "inode": 206},
            },
        },
    }


class ProfileMappingTests(unittest.TestCase):
    def test_v2_mapping_loads_to_one_derived_instance_descriptor(self):
        mapping = ready_mapping()
        instance = core.instance_from_mapping(mapping, expected_name="dev",
                                              expected_owner_uid=1000, require_ready=True)
        self.assertEqual(instance.kind, "profile")
        self.assertEqual(instance.runtime_sha, "a" * 40)
        self.assertEqual(instance.vm_user, mapping["accounts"]["vm"]["name"])
        self.assertEqual(instance.vm_unit, mapping["units"]["vm"])
        self.assertEqual(instance.disk, pathlib.Path("/var/lib/little-mester-vm/profiles/dev/vm/disk.qcow2"))
        self.assertEqual(instance.base_path,
                         pathlib.Path("/var/lib/little-mester-vm/bases")
                         / provision.IMAGE_SHA256 / "base.qcow2")
        self.assertEqual((instance.ssh_port, instance.proxy_port, instance.metrics_port),
                         (22223, 22224, 22225))

    def test_protected_mapping_schema_is_generated_from_the_authoritative_parser(self):
        schema_path = ROOT / "docs/vm-profile-mapping-v2.schema.json"
        self.assertTrue(schema_path.is_file())
        self.assertEqual(json.loads(schema_path.read_text()), core.mapping_schema())
        self.assertEqual(core.validate_mapping(ready_mapping())["state"], "ready")

    def test_rejects_v1_unknown_fields_and_wrong_owner_before_instance_creation(self):
        mapping = ready_mapping()
        for change in (
                {"schema_version": 1}, {"unknown": "field"}, {"owner_uid": 1001},
                {"name": "other"}, {"runtime": {**mapping["runtime"], "sha": "a" * 39}}):
            candidate = copy.deepcopy(mapping)
            candidate.update(change)
            with self.subTest(change=change), self.assertRaises(core.Refused):
                core.instance_from_mapping(candidate, expected_name="dev",
                                           expected_owner_uid=1000)

    def test_rejects_unapproved_base_port_identity_and_resource_changes(self):
        mutations = (
            lambda m: m["base"].update(source_sha256="e" * 64),
            lambda m: m["resources"].update(ssh_port=22224),
            lambda m: m["resources"].update(memory_mib=True),
            lambda m: m["accounts"]["vm"].update(name="arbitrary"),
            lambda m: m["accounts"]["proxy"].update(uid=m["accounts"]["vm"]["uid"]),
            lambda m: m["units"].update(metrics=m["units"]["vm"]),
            lambda m: m["provisioning"]["artifacts"]["disk"].update(backing_path="/tmp/x"),
        )
        for mutate in mutations:
            candidate = ready_mapping()
            mutate(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(core.Refused):
                core.instance_from_mapping(candidate)

    def test_state_transaction_and_provisioning_evidence_must_cohere(self):
        cases = []
        candidate = ready_mapping()
        candidate["provisioning"] = None
        cases.append(candidate)
        candidate = ready_mapping()
        candidate["state"] = "unprovisioned"
        cases.append(candidate)
        candidate = ready_mapping()
        candidate["state"] = "installing"
        cases.append(candidate)
        candidate = ready_mapping()
        candidate["state"] = "installing"
        candidate["transaction"] = {
            "schema_version": 2, "operation": "install", "transaction_id": str(uuid.uuid4()),
            "phase": "prepared", "mode": "fresh", "generation": "0" * 36 + "4a00",
            "unexpected": "key",
        }
        cases.append(candidate)
        candidate = ready_mapping()
        candidate["state"] = "resetting"
        candidate["transaction"] = {
            "schema_version": 2, "operation": "reset", "transaction_id": str(uuid.uuid4()),
            "phase": "prepared", "mode": "fresh", "generation": "0" * 36 + "4a00",
        }
        cases.append(candidate)
        for candidate in cases:
            with self.subTest(state=candidate["state"]), self.assertRaises(core.Refused):
                core.validate_mapping(candidate)

    def test_duplicate_json_keys_refuse(self):
        with self.assertRaisesRegex(core.Refused, "duplicate"):
            json.loads('{"schema_version":2,"schema_version":2}',
                       object_pairs_hook=core._reject_duplicate_pairs)

    def test_malformed_unhashable_transaction_operation_refuses_cleanly(self):
        mapping = ready_mapping()
        mapping["state"] = "installing"
        mapping["provisioning"] = None
        mapping["transaction"] = {"operation": [], "schema_version": 2}
        with self.assertRaises(core.Refused):
            core.validate_mapping(mapping)

    def test_unprovisioned_mapping_is_valid_but_not_startable(self):
        mapping = ready_mapping()
        mapping.update(state="unprovisioned", provisioning=None)
        self.assertEqual(core.validate_mapping(mapping)["state"], "unprovisioned")
        with self.assertRaisesRegex(core.Refused, "not ready"):
            core.instance_from_mapping(mapping, require_ready=True)

    def test_workspace_must_be_a_canonical_absolute_path(self):
        for path in ("//tmp/work", "/tmp/../etc", "/tmp/./work", "/tmp/work/"):
            mapping = ready_mapping()
            mapping["workspace"] = {"path": path, "device": 1, "inode": 2, "mount_id": 3}
            with self.subTest(path=path), self.assertRaises(core.Refused):
                core.validate_mapping(mapping)


if __name__ == "__main__":
    unittest.main()
