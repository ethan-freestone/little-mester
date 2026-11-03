"""Synthetic procfs and private-profile tests for descendant-safe suspension."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/vm-recursion-live-children.py"
SPEC = importlib.util.spec_from_file_location("vm_recursion_live_children", SCRIPT)
children = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(children)


class LiveChildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.proc = self.base / "proc"
        self.proc.mkdir()
        self.controller = self.base / "frozen-control/lab-vm"
        self.controller.parent.mkdir()
        self.controller.write_text("fixture controller\n", encoding="ascii")
        self.controller.chmod(0o700)

    def add_profile(self, name, pid, *, depth=1):
        vm = self.state / "vm"
        vm.mkdir(mode=0o700, exist_ok=True)
        profiles = vm / "profiles"
        profiles.mkdir(mode=0o700, exist_ok=True)
        profiles.chmod(0o700)
        profile = profiles / name
        profile.mkdir(mode=0o700)
        image_hash = "a" * 64
        images = vm / "images"
        images.mkdir(mode=0o700, exist_ok=True)
        images.chmod(0o700)
        base = images / image_hash
        base.mkdir(mode=0o700, exist_ok=True)
        base_disk = base / "base.qcow2"
        base_disk.write_text("fixture base\n", encoding="ascii")
        base_disk.chmod(0o600)
        disk = profile / "disk.qcow2"
        disk.write_text("fixture overlay\n", encoding="ascii")
        disk.chmod(0o600)
        for filename in ("seed.iso", "control-key", "known_hosts"):
            asset = profile / filename
            asset.write_text("fixture\n", encoding="ascii")
            asset.chmod(0o600)
        identity = {
            "pid": pid,
            "start_ticks": 12345 + pid,
            "executable": "/usr/bin/qemu-system-x86_64",
            "disk_path": str(disk),
            "socket_path": str(profile / "runtime/vfs.sock"),
            "ssh_port": 40220 + pid % 100,
            "argv": [
                "/usr/bin/qemu-system-x86_64",
                "-name", name,
                "-drive", f"file={disk},format=qcow2,if=virtio",
            ],
        }
        data = {
            "schema_version": 1,
            "profile_id": f"00000000-0000-4000-8000-{pid:012d}",
            "name": name,
            "workspace": str(self.workspace),
            "image_sha256": image_hash,
            "image_path": str(base_disk),
            "memory_gib": 4,
            "depth": depth,
            "max_depth": max(depth, 2),
            "descendants_possible": True,
            "opencode_version": "2.0.0",
            "guest_payload_sha256": "b" * 64,
            "runtime_payload_sha256": "c" * 64,
            "status": "ready",
            "runtime_identity": identity,
        }
        metadata = profile / "profile.json"
        metadata.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="ascii")
        metadata.chmod(0o600)
        return profile, identity

    def add_process(self, identity, *, argv=None, comm="qemu-system-x86_64", unreadable=False):
        process = self.proc / str(identity["pid"])
        process.mkdir()
        (process / "comm").write_text(comm + "\n", encoding="ascii")
        actual_argv = argv if argv is not None else identity["argv"]
        (process / "cmdline").write_bytes(b"\0".join(item.encode() for item in actual_argv) + b"\0")
        fields = ["R"] + ["0"] * 19
        fields[19] = str(identity["start_ticks"])
        (process / "stat").write_text(f"{identity['pid']} (qemu fixture) " + " ".join(fields), encoding="ascii")
        (process / "exe").symlink_to(identity["executable"])
        if unreadable:
            (process / "cmdline").chmod(0o000)
        return process

    def scan(self):
        return children.live_nested_qemus(self.state, proc_root=self.proc)

    def test_no_child_qemu_is_safe_even_when_descendants_are_permitted(self):
        self.assertEqual(self.scan(), ([], []))

    def test_only_exact_owned_profile_and_full_process_identity_match(self):
        profile, identity = self.add_profile("child-a", 4321)
        self.add_process(identity)
        self.assertEqual(self.scan(), (["child-a"], []))
        self.assertTrue(profile.joinpath("disk.qcow2").is_file())

    def test_unknown_or_foreign_qemu_blocks_parent_shutdown(self):
        _profile, identity = self.add_profile("child-a", 4321)
        self.add_process(identity, argv=[*identity["argv"], "--different-identity"])
        self.assertEqual(self.scan(), ([], [4321]))

    def test_unreadable_qemu_metadata_and_proc_table_fail_closed(self):
        _profile, identity = self.add_profile("child-a", 4321)
        self.add_process(identity, unreadable=True)
        self.assertEqual(self.scan(), ([], [4321]))
        self.proc.rename(self.base / "proc-saved")
        self.assertEqual(children.live_nested_qemus(self.state, proc_root=self.proc), ([], [-1]))

    def test_unrelated_processes_are_not_misclassified_as_qemu(self):
        fake = {"pid": 8765, "start_ticks": 999, "executable": "/usr/bin/python3", "argv": ["python3"]}
        self.add_process(fake, comm="python3")
        self.assertEqual(self.scan(), ([], []))

    def test_symlinked_or_incomplete_profile_is_not_a_managed_child(self):
        profile, identity = self.add_profile("child-a", 4321)
        (profile / "disk.qcow2").unlink()
        (profile / "disk.qcow2").symlink_to(self.base / "foreign-disk")
        self.add_process(identity)
        names, unknown = self.scan()
        self.assertEqual(names, [])
        self.assertTrue(unknown)

    def test_recursive_suspend_uses_explicit_frozen_controller_deepest_first(self):
        _first, identity_first = self.add_profile("child-shallow", 4321, depth=1)
        _second, identity_second = self.add_profile("child-deep", 4322, depth=2)
        process_first = self.add_process(identity_first)
        process_second = self.add_process(identity_second)
        commands = []

        def suspend(command, *, env, check, pass_fds):
            commands.append((command, env.copy(), check, pass_fds))
            name = command[-1]
            current = children._PATHS.profile_read(self.state, name)
            children._PATHS.profile_transition(self.state, name, "ready", "suspending")
            children._PATHS.profile_transition(self.state, name, "suspending", "suspended")
            pid = current["runtime_identity"]["pid"]
            (self.proc / str(pid)).rename(self.proc / f"stopped-{pid}")
            return SimpleNamespace(returncode=0)

        status = children.poweroff_recursive_children(
            self.state, self.proc, self.controller, runner=suspend
        )
        self.assertEqual(status, 0)
        self.assertEqual([item[0][-1] for item in commands], ["child-deep", "child-shallow"])
        self.assertTrue(all(item[0][0] == str(self.controller) for item in commands))
        self.assertTrue(all(item[1]["MESTER_STATE_DIR"] == str(self.state) for item in commands))
        self.assertTrue(all(item[2] is False for item in commands))
        self.assertTrue(all(item[3] == () for item in commands))
        self.assertTrue(process_first.exists() is False)
        self.assertTrue(process_second.exists() is False)
        self.assertTrue((_first / "disk.qcow2").is_file())
        self.assertTrue((_second / "control-key").is_file())

    def test_unknown_child_blocks_before_any_recursive_suspend_call(self):
        _profile, identity = self.add_profile("child-a", 4321)
        self.add_process(identity)
        self.add_process({"pid": 9876, "start_ticks": 1, "executable": "/usr/bin/qemu-system-x86_64",
                          "argv": ["/usr/bin/qemu-system-x86_64", "-drive", "file=/tmp/foreign.qcow2"]})
        calls = []
        status = children.poweroff_recursive_children(
            self.state, self.proc, self.controller,
            runner=lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(returncode=0),
        )
        self.assertEqual(status, 4)
        self.assertEqual(calls, [])

    def test_child_failure_leaves_later_children_uninvoked_and_profiles_preserved(self):
        profile, identity = self.add_profile("child-a", 4321)
        self.add_process(identity)
        calls = []
        status = children.poweroff_recursive_children(
            self.state, self.proc, self.controller,
            runner=lambda command, **kwargs: calls.append(command) or SimpleNamespace(returncode=17),
        )
        self.assertEqual(status, 17)
        self.assertEqual(len(calls), 1)
        self.assertTrue((self.proc / "4321").exists())
        self.assertTrue((profile / "disk.qcow2").exists())

    def test_controller_must_be_explicit_and_never_discovered_from_workspace(self):
        _profile, identity = self.add_profile("child-a", 4321)
        self.add_process(identity)
        status = children.poweroff_recursive_children(self.state, self.proc, None)
        self.assertEqual(status, 4)
        self.assertTrue((self.proc / "4321").exists())
        self.assertFalse(hasattr(children, "find_lab_cli"))


if __name__ == "__main__":
    unittest.main()
