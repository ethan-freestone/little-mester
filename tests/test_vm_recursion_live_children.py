import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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
        for asset in ("seed.iso", "id_vm_control", "id_vm_control.pub", "known_hosts", "workspace"):
            (profile / asset).touch()
        return profile

    def add_process(self, pid, disk, *, comm="qemu-system-x86_64"):
        process = self.proc / str(pid)
        process.mkdir()
        (process / "comm").write_text(f"{comm}\n")
        (process / "cmdline").write_bytes(
            f"qemu-system-x86_64\0-drive\0file={disk},format=qcow2,if=virtio\0".encode()
        )
        return process

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

    def test_recursive_poweroff_suspends_children_and_preserves_profile_state(self):
        profile = self.add_profile("child-a")
        process = self.add_process(4321, profile / "disk.qcow2")
        cli = self.root / "workspace/lab-vm"
        cli.parent.mkdir()
        cli.touch()
        cli.chmod(0o755)
        commands = []

        def suspend(command, *, env, check):
            commands.append((command, env, check))
            shutil.rmtree(process)
            return SimpleNamespace(returncode=0)

        status = module.poweroff_recursive_children(
            self.proc, self.sessions, cli, runner=suspend
        )
        self.assertEqual(status, 0)
        self.assertEqual(len(commands), 1)
        command, env, check = commands[0]
        self.assertEqual(command, [str(cli), "--poweroff-recursive", "suspend", "child-a"])
        self.assertEqual(env["VM_STATE_DIR"], str(self.sessions / "child-a"))
        self.assertEqual(env["VM_DIR_BASE"], str(self.sessions / "child-a/vm"))
        self.assertFalse(check)
        self.assertTrue((profile / "disk.qcow2").exists())
        self.assertTrue((profile / "id_vm_control").exists())

    def test_recursive_poweroff_stops_on_child_failure_and_never_kills_it(self):
        profile = self.add_profile("child-a")
        process = self.add_process(4321, profile / "disk.qcow2")
        cli = self.root / "workspace/lab-vm"
        cli.parent.mkdir()
        cli.touch()
        cli.chmod(0o755)

        def fail_suspend(command, *, env, check):
            return SimpleNamespace(returncode=17)

        status = module.poweroff_recursive_children(
            self.proc, self.sessions, cli, runner=fail_suspend
        )
        self.assertEqual(status, 17)
        self.assertTrue(process.exists())
        self.assertTrue((profile / "disk.qcow2").exists())

    def test_recursive_poweroff_refuses_unknown_qemu_before_any_child_action(self):
        profile = self.add_profile("child-a")
        self.add_process(4321, profile / "disk.qcow2")
        self.add_process(9876, self.root / "untracked.qcow2")
        calls = []

        def suspend(*args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=0)

        status = module.poweroff_recursive_children(
            self.proc, self.sessions, self.root / "lab-vm", runner=suspend
        )
        self.assertEqual(status, 4)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
