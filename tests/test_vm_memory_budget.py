import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "vm_memory_budget.py"
SPEC = importlib.util.spec_from_file_location("vm_memory_budget", MODULE_PATH)
memory_budget = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(memory_budget)


class MemoryBudgetTests(unittest.TestCase):
    def test_uses_memavailable_and_keeps_the_requested_reserve(self):
        available = memory_budget.projected_available_mib(
            memavailable_mib=10_000,
            cgroup_remaining_mib=None,
            release_rss_mib=0,
        )
        self.assertEqual(available, 10_000)
        self.assertTrue(memory_budget.budget_satisfies(8_000, available, 512))
        self.assertFalse(memory_budget.budget_satisfies(9_500, available, 512))

    def test_tighter_cgroup_limit_is_authoritative(self):
        available = memory_budget.projected_available_mib(
            memavailable_mib=20_000,
            cgroup_remaining_mib=4_096,
            release_rss_mib=0,
        )
        self.assertEqual(available, 4_096)
        self.assertFalse(memory_budget.budget_satisfies(4_096, available, 512))

    def test_resize_projection_counts_only_memory_released_by_target_qemu(self):
        available = memory_budget.projected_available_mib(
            memavailable_mib=5_000,
            cgroup_remaining_mib=6_000,
            release_rss_mib=2_000,
        )
        self.assertEqual(available, 7_000)
        self.assertTrue(memory_budget.budget_satisfies(6_000, available, 512))
        self.assertFalse(memory_budget.budget_satisfies(7_000, available, 512))

    def test_cgroup_or_host_capacity_below_target_refuses(self):
        available = memory_budget.projected_available_mib(
            memavailable_mib=1_024,
            cgroup_remaining_mib=None,
            release_rss_mib=0,
        )
        self.assertFalse(memory_budget.budget_satisfies(4_096, available, 512))

    def test_reads_cgroup_v2_limit_from_process_membership(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            proc = root / "proc"
            cgroup = root / "cgroup"
            process = proc / "123"
            group = cgroup / "slice" / "mester"
            process.mkdir(parents=True)
            group.mkdir(parents=True)
            (process / "cgroup").write_text("0::/slice/mester\n", encoding="ascii")
            (group / "memory.max").write_text(str(8_000 * 1024 * 1024), encoding="ascii")
            (group / "memory.current").write_text(str(3_000 * 1024 * 1024), encoding="ascii")
            (cgroup / "slice" / "memory.max").write_text(str(6_000 * 1024 * 1024), encoding="ascii")
            (cgroup / "slice" / "memory.current").write_text(str(2_000 * 1024 * 1024), encoding="ascii")
            self.assertEqual(
                memory_budget.read_cgroup_remaining_mib(
                    123, proc_root=proc, cgroup_root=cgroup
                ),
                4_000,
            )

    def test_releasable_memory_uses_private_rss_not_shared_rss(self):
        with tempfile.TemporaryDirectory() as temp:
            proc = Path(temp)
            process = proc / "123"
            process.mkdir()
            (process / "smaps_rollup").write_text(
                "Rss: 4096 kB\nPss: 1024 kB\n"
                "Private_Clean: 100 kB\nPrivate_Dirty: 1900 kB\n"
                "Private_Hugetlb: 48 kB\nShared_Clean: 2048 kB\n",
                encoding="ascii",
            )
            self.assertEqual(memory_budget.read_process_rss_mib(123, proc_root=proc), 2)

    def test_missing_cgroup_budget_files_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            process = root / "proc" / "123"
            process.mkdir(parents=True)
            (process / "cgroup").write_text("0::/limited\n", encoding="ascii")
            group = root / "cgroup" / "limited"
            group.mkdir(parents=True)
            (group / "memory.max").write_text("8192", encoding="ascii")
            with self.assertRaises(RuntimeError):
                memory_budget.read_cgroup_remaining_mib(
                    123, proc_root=root / "proc", cgroup_root=root / "cgroup"
                )


if __name__ == "__main__":
    unittest.main()
