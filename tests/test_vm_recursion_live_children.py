import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "vm-recursion-live-children.py"
SPEC = importlib.util.spec_from_file_location("vm_recursion_live_children", HELPER)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class VmRecursionLiveChildrenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.proc = self.root / "proc"
        self.sessions = self.root / "sessions"
        self.proc.mkdir()
        self.sessions.mkdir()

    def add_profile(self, name):
        profile = self.sessions / name / "vm" / name
        profile.mkdir(parents=True)
        (profile / "disk.qcow2").touch()
        (profile / "qemu.pid").write_text("4321\n")
        return profile

    def add_process(self, pid, disk, *, comm="qemu-system-x86_64"):
        process = self.proc / str(pid)
        process.mkdir()
        (process / "comm").write_text(f"{comm}\n")
        (process / "cmdline").write_bytes(
            f"qemu-system-x86_64\0-drive\0file={disk},format=qcow2,if=virtio\0".encode()
        )

    def scan(self):
        return module.live_nested_qemus(self.proc, self.sessions)

    def test_no_qemu_processes_is_safe_even_when_descendants_are_permitted(self):
        self.add_profile("child-a")
        self.assertEqual(self.scan(), ([], []))

    def test_matches_live_child_to_managed_profile_using_its_disk_argument(self):
        profile = self.add_profile("child-a")
        self.add_process(4321, profile / "disk.qcow2")
        self.assertEqual(self.scan(), (["child-a"], []))

    def test_untracked_live_qemu_fails_closed(self):
        self.add_process(9876, self.root / "untracked.qcow2")
        self.assertEqual(self.scan(), ([], [9876]))

    def test_unreadable_live_qemu_metadata_fails_closed(self):
        process = self.proc / "8765"
        process.mkdir()
        (process / "comm").write_text("qemu-system-x86_64\n")
        (process / "cmdline").mkdir()
        self.assertEqual(self.scan(), ([], [8765]))

    def test_unrelated_processes_are_ignored(self):
        self.add_process(5555, self.root / "ordinary.qcow2", comm="python3")
        self.assertEqual(self.scan(), ([], []))

    def test_symlinked_profile_disk_is_not_trusted_as_a_child_identity(self):
        profile = self.add_profile("child-a")
        (profile / "disk.qcow2").unlink()
        (profile / "disk.qcow2").symlink_to(self.root / "other-disk.qcow2")
        self.add_process(4321, profile / "disk.qcow2")
        self.assertEqual(self.scan(), ([], [4321]))


if __name__ == "__main__":
    unittest.main()
