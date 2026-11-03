"""Fixture-only host/cgroup RAM budget and concurrent reservation tests."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/vm_memory_budget.py"
SPEC = importlib.util.spec_from_file_location("vm_memory_budget", SCRIPT)
memory_budget = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(memory_budget)


class MemoryBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.proc = self.base / "proc"
        self.cgroups = self.base / "cgroups"
        self.proc.mkdir()
        self.cgroups.mkdir()

    def reservation_fixture(self, state, profile, owner, requested_mib):
        reservations = state / ".reservations"
        reservations.mkdir(mode=0o700, exist_ok=True)
        reservations.chmod(0o700)
        path = reservations / f"{profile}.json"
        path.write_text(
            json.dumps({
                "schema_version": 1,
                "profile": profile,
                "requested_mib": requested_mib,
                "owner_identity": owner,
            }, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="ascii",
        )
        path.chmod(0o600)
        return path

    def test_depth_defaults_and_override_validation(self):
        self.assertEqual(
            [memory_budget.default_memory_gib(depth) for depth in range(5)],
            [8, 4, 2, 1, 1],
        )
        for value in (-1, 33, True, "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                memory_budget.default_memory_gib(value)
        self.assertEqual(memory_budget.validate_memory_gib(128), 128)
        for value in (0, 129, True, "4", 4.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                memory_budget.validate_memory_gib(value)

    def test_projected_budget_uses_tightest_limit_and_target_private_release(self):
        self.assertEqual(
            memory_budget.projected_available_mib(
                memavailable_mib=10_000, cgroup_remaining_mib=8_000, release_rss_mib=2_000
            ),
            10_000,
        )
        self.assertTrue(memory_budget.budget_satisfies(9_000, 10_000, 512))
        self.assertFalse(memory_budget.budget_satisfies(9_500, 10_000, 512))
        self.assertEqual(
            memory_budget.projected_available_mib(
                memavailable_mib=5_000, cgroup_remaining_mib=6_000, release_rss_mib=2_000
            ),
            7_000,
        )
        with self.assertRaises(ValueError):
            memory_budget.projected_available_mib(-1, None, 0)

    def test_memavailable_is_required_and_parsed_in_kib(self):
        (self.proc / "meminfo").write_text(
            "MemTotal: 16000000 kB\nMemAvailable: 8192000 kB\n", encoding="ascii"
        )
        self.assertEqual(memory_budget.read_memavailable_mib(self.proc), 8000)
        (self.proc / "meminfo").write_text("MemTotal: 16000000 kB\n", encoding="ascii")
        with self.assertRaises(RuntimeError):
            memory_budget.read_memavailable_mib(self.proc)
        (self.proc / "meminfo").write_text("MemAvailable: nope kB\n", encoding="ascii")
        with self.assertRaises(RuntimeError):
            memory_budget.read_memavailable_mib(self.proc)

    def test_cgroup_v2_uses_minimum_remaining_budget_across_ancestors(self):
        process = self.proc / "123"
        process.mkdir()
        (process / "cgroup").write_text("0::/user.slice/mester\n", encoding="ascii")
        child = self.cgroups / "user.slice/mester"
        child.mkdir(parents=True)
        parent = self.cgroups / "user.slice"
        for path, limit, usage in (
            (child, 8_000, 3_000),
            (parent, 6_000, 2_000),
            (self.cgroups, 12_000, 1_000),
        ):
            (path / "memory.max").write_text(str(limit * 1024 * 1024), encoding="ascii")
            (path / "memory.current").write_text(str(usage * 1024 * 1024), encoding="ascii")
        self.assertEqual(
            memory_budget.read_cgroup_remaining_mib(123, proc_root=self.proc, cgroup_root=self.cgroups),
            4_000,
        )

    def test_cgroup_v1_reads_memory_controller_and_nested_ancestors(self):
        process = self.proc / "321"
        process.mkdir()
        (process / "cgroup").write_text("5:cpu,memory:/tenant/vm\n", encoding="ascii")
        root = self.cgroups / "memory"
        child = root / "tenant/vm"
        child.mkdir(parents=True)
        parent = root / "tenant"
        for path, limit, usage in ((child, 9_000, 4_000), (parent, 7_000, 2_000), (root, 20_000, 0)):
            (path / "memory.limit_in_bytes").write_text(str(limit * 1024 * 1024), encoding="ascii")
            (path / "memory.usage_in_bytes").write_text(str(usage * 1024 * 1024), encoding="ascii")
        self.assertEqual(
            memory_budget.read_cgroup_remaining_mib(321, proc_root=self.proc, cgroup_root=self.cgroups),
            5_000,
        )

    def test_explicit_unlimited_cgroups_are_distinct_from_missing_files(self):
        process = self.proc / "456"
        process.mkdir()
        (process / "cgroup").write_text("0::/unlimited\n", encoding="ascii")
        group = self.cgroups / "unlimited"
        group.mkdir()
        (group / "memory.max").write_text("max\n", encoding="ascii")
        (group / "memory.current").write_text("1048576\n", encoding="ascii")
        (self.cgroups / "memory.max").write_text("max\n", encoding="ascii")
        (self.cgroups / "memory.current").write_text("2097152\n", encoding="ascii")
        self.assertIsNone(
            memory_budget.read_cgroup_remaining_mib(456, proc_root=self.proc, cgroup_root=self.cgroups)
        )
        (group / "memory.max").unlink()
        with self.assertRaises(RuntimeError):
            memory_budget.read_cgroup_remaining_mib(456, proc_root=self.proc, cgroup_root=self.cgroups)

    def test_cgroup_traversal_and_unreadable_usage_fail_closed(self):
        process = self.proc / "789"
        process.mkdir()
        (process / "cgroup").write_text("0::/../outside\n", encoding="ascii")
        with self.assertRaises(RuntimeError):
            memory_budget.read_cgroup_remaining_mib(789, proc_root=self.proc, cgroup_root=self.cgroups)

    def test_empty_cgroup_membership_is_not_treated_as_unlimited(self):
        process = self.proc / "999"
        process.mkdir()
        (process / "cgroup").write_text("", encoding="ascii")
        with self.assertRaises(RuntimeError):
            memory_budget.read_cgroup_remaining_mib(999, proc_root=self.proc, cgroup_root=self.cgroups)
        (process / "cgroup").write_text("0::/tenant\n", encoding="ascii")
        group = self.cgroups / "tenant"
        group.mkdir()
        (group / "memory.max").write_text("4096\n", encoding="ascii")
        with self.assertRaises(RuntimeError):
            memory_budget.read_cgroup_remaining_mib(789, proc_root=self.proc, cgroup_root=self.cgroups)

    def test_releasable_memory_counts_private_rss_only(self):
        process = self.proc / "111"
        process.mkdir()
        (process / "smaps_rollup").write_text(
            "Rss: 4096 kB\nPrivate_Clean: 100 kB\nPrivate_Dirty: 1900 kB\n"
            "Private_Hugetlb: 48 kB\nShared_Clean: 2048 kB\n",
            encoding="ascii",
        )
        self.assertEqual(memory_budget.read_process_private_rss_mib(111, proc_root=self.proc), 2)
        (process / "smaps_rollup").write_text("Rss: 4096 kB\nShared_Clean: 2048 kB\n", encoding="ascii")
        with self.assertRaises(RuntimeError):
            memory_budget.read_process_private_rss_mib(111, proc_root=self.proc)

    def test_budget_requires_requested_gib_plus_reserve(self):
        (self.proc / "meminfo").write_text("MemAvailable: 8388608 kB\n", encoding="ascii")
        process = self.proc / str(os.getpid())
        process.mkdir()
        (process / "cgroup").write_text("5:cpu:/\n", encoding="ascii")
        okay, details = memory_budget.check_budget(8, reserve_mib=512, proc_root=self.proc, cgroup_root=self.cgroups)
        self.assertTrue(okay)
        self.assertEqual(details["projected_available_mib"], 8192)
        okay, _ = memory_budget.check_budget(9000, reserve_mib=0, proc_root=self.proc, cgroup_root=self.cgroups)
        self.assertFalse(okay)

    def test_failed_reservation_has_no_profile_or_reservation_write(self):
        state = self.base / "state"
        state.mkdir(mode=0o700)
        with (
            mock.patch.object(memory_budget, "check_budget", return_value=(False, {"projected_available_mib": 100})),
            self.assertRaises(memory_budget.ResourceRefusal),
        ):
            memory_budget.reserve_allocation(state, "candidate", 4096)
        profiles = state / "vm/profiles"
        self.assertFalse(profiles.exists())
        reservations = state / ".reservations"
        self.assertFalse(reservations.exists())
        self.assertFalse((state / ".allocation.lock").exists())

    def test_reservations_serialize_demand_and_release_only_same_owner(self):
        state = self.base / "state"
        state.mkdir(mode=0o700)
        owner = {"pid": 123, "start_ticks": 99, "executable": "/bin/bash", "argv": ["bash", "lab-vm"]}
        observed = []

        def budget(requested_mib, reserve_mib=512, **_kwargs):
            observed.append((requested_mib, reserve_mib))
            return (requested_mib + 512 <= 6000, {"projected_available_mib": 6000})

        with (mock.patch.object(memory_budget, "capture_owner_identity", return_value=owner),
              mock.patch.object(memory_budget, "identity_state", return_value="running"),
              mock.patch.object(memory_budget, "check_budget", side_effect=budget)):
            first = memory_budget.reserve_allocation(state, "first", 4096)
            self.assertEqual(first["reserved_mib"], 4096)
            with self.assertRaises(memory_budget.ResourceRefusal):
                memory_budget.reserve_allocation(state, "second", 2048)
            self.assertEqual(observed, [(4096, 512), (4096, 512), (2048, 512), (6144, 512)])
            with self.assertRaises(memory_budget.ResourceRefusal):
                memory_budget.release_allocation(state, "first", owner_identity={**owner, "start_ticks": 100})
            self.assertTrue((state / ".reservations/first.json").is_file())
            memory_budget.release_allocation(state, "first", owner_identity=owner)
        self.assertFalse((state / ".reservations/first.json").exists())

    def test_only_confirmed_stopped_owner_clears_a_stale_reservation(self):
        state = self.base / "state"
        state.mkdir(mode=0o700)
        owner = {"pid": 123, "start_ticks": 99, "executable": "/bin/bash", "argv": ["bash", "lab-vm"]}
        with (mock.patch.object(memory_budget, "capture_owner_identity", return_value={"pid": 456, "start_ticks": 1, "executable": "/bin/bash", "argv": ["bash"]}),
              mock.patch.object(memory_budget, "identity_state", return_value="stopped"),
              mock.patch.object(memory_budget, "check_budget", return_value=(True, {"projected_available_mib": 10000}))):
            self.reservation_fixture(state, "old", owner, 7000)
            result = memory_budget.reserve_allocation(state, "new", 2048)
        self.assertEqual(result["reserved_mib"], 2048)
        self.assertFalse((state / ".reservations/old.json").exists())

    def test_unknown_reservation_owner_blocks_new_allocation_without_cleanup(self):
        state = self.base / "state"
        state.mkdir(mode=0o700)
        owner = {"pid": 123, "start_ticks": 99, "executable": "/bin/bash", "argv": ["bash", "lab-vm"]}
        new_owner = {"pid": 456, "start_ticks": 1, "executable": "/bin/bash", "argv": ["bash"]}
        with (mock.patch.object(memory_budget, "capture_owner_identity", return_value=new_owner),
              mock.patch.object(memory_budget, "identity_state", return_value="unknown"),
              mock.patch.object(memory_budget, "check_budget", return_value=(True, {"projected_available_mib": 10000})) as budget):
            old_path = self.reservation_fixture(state, "old", owner, 7000)
            with self.assertRaises(memory_budget.ResourceRefusal):
                memory_budget.reserve_allocation(state, "new", 1024)
        self.assertTrue(old_path.exists())
        budget.assert_called_once_with(1024, memory_budget.DEFAULT_RESERVE_MIB)


if __name__ == "__main__":
    unittest.main()
