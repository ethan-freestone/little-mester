"""Fixture-only prestart recovery: preserve evidence; refuse live or valuable state."""

import fcntl
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/vm-recover-prestart.py"
spec = importlib.util.spec_from_file_location("recover_fixture", SCRIPT)
recover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recover)


class PrestartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "state"
        self.profile = self.root / "vm/profiles/test"
        self.profile.mkdir(parents=True, mode=0o700)
        for directory in (self.root, self.root / "vm", self.profile.parent):
            directory.chmod(0o700)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.data = {
            "schema_version": 1, "profile_id": str(uuid.uuid4()), "name": "test",
            "workspace": str(self.workspace), "image_sha256": None, "image_path": None,
            "memory_gib": 8, "depth": 0, "max_depth": 0, "descendants_possible": False,
            "opencode_version": None, "guest_payload_sha256": None,
            "runtime_payload_sha256": None, "status": "starting",
        }
        recover.paths.atomic_json(self.profile / "profile.json", self.data)
        for lock in (self.root / ".allocation.lock", self.profile / ".profile.lock"):
            lock.touch(mode=0o600)
        cache = self.profile / "runtime/lib/vm/__pycache__"
        cache.mkdir(parents=True)
        self.cache = cache

    def cli(self, *args, expected=0):
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--state-root", str(self.root),
             "--profile", "test", *args], capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def test_diagnosis_is_readonly(self):
        before = sorted(str(p.relative_to(self.base)) for p in self.base.rglob("*"))
        self.cli()
        self.assertEqual(before, sorted(str(p.relative_to(self.base)) for p in self.base.rglob("*")))

    def test_startup_inventory_reads_through_cache_violation(self):
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts/vm-start-diagnostics.py"),
             "--state-root", str(self.root), "--profile", "test"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["status"], "starting")
        self.assertEqual(data["runs"], [])
        self.assertEqual(data["metrics"]["state"], "absent")
        self.assertNotIn("argv", result.stdout)

    def test_apply_preserves_entire_profile_and_restores_policy(self):
        result = self.cli("--apply")
        archive = Path(json.loads(result.stdout.splitlines()[1])["preserved_profile"])
        self.assertFalse(self.profile.exists())
        self.assertEqual((archive / "profile.json").read_text(), recover.paths.encoded(self.data).decode())
        self.assertTrue((archive / "runtime/lib/vm/__pycache__").is_dir())
        recover.paths.state_root(str(self.root), must_exist=True)
        inventory = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts/vm-start-diagnostics.py"),
             "--state-root", str(self.root), "--profile", "test"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(inventory.returncode, 0, inventory.stderr)
        self.assertEqual(json.loads(inventory.stdout)["status"], "absent")

    def test_guest_disk_or_run_is_never_archived(self):
        for name in ("disk.qcow2", "runs", "seed.iso"):
            with self.subTest(name=name):
                path = self.profile / name
                path.touch()
                self.cli("--apply", expected=2)
                self.assertTrue(self.profile.exists())
                path.unlink()

    def test_live_process_reference_is_refused(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", str(self.profile)])
        try:
            self.cli("--apply", expected=2)
            self.assertTrue(self.profile.exists())
        finally:
            child.terminate()
            child.wait()

    def failed_overlay(self):
        self.data["status"] = "failed"
        recover.paths.atomic_json(self.profile / "profile.json", self.data)
        disk = self.profile / "disk.qcow2"
        disk.write_bytes(b"valuable-overlay-fixture")
        disk.chmod(0o600)

    def test_failed_overlay_requires_explicit_archive_and_preserves_bytes(self):
        self.failed_overlay()
        self.cli("--apply", expected=2)
        result = self.cli("--apply", "--archive-failed-before-boot")
        archive = Path(json.loads(result.stdout.splitlines()[1])["preserved_profile"])
        self.assertEqual((archive / "disk.qcow2").read_bytes(), b"valuable-overlay-fixture")
        self.assertTrue((archive / "runtime/lib/vm/__pycache__").exists())

    def test_failed_overlay_with_iso_or_runs_is_refused(self):
        self.failed_overlay()
        for name in ("seed.iso", "runs"):
            with self.subTest(name=name):
                path = self.profile / name
                path.touch()
                self.cli("--apply", "--archive-failed-before-boot", expected=2)
                self.assertEqual((self.profile / "disk.qcow2").read_bytes(), b"valuable-overlay-fixture")
                path.unlink()

    def stopped_run(self, live=False):
        self.failed_overlay()
        (self.profile / "seed.iso").touch(mode=0o600)
        run = self.profile / "runs/run-one"
        run.mkdir(parents=True, mode=0o700)
        run.parent.chmod(0o700)
        recover.process.registry_initialize(run / "processes.json", run.name)
        if live:
            recover.process.registry_add(run / "processes.json", run.name, "qemu_1",
                                         recover.process.capture_identity(os.getpid()))
        else:
            recover.process.registry_add_exited(run / "processes.json", run.name, "qemu_1", 0)
        return run

    def test_explicit_stopped_booted_archive_preserves_overlay_and_run(self):
        self.stopped_run()
        self.cli("--apply", "--archive-failed-before-boot", expected=2)
        result = self.cli("--apply", "--archive-stopped-failed")
        archive = Path(json.loads(result.stdout.splitlines()[1])["preserved_profile"])
        self.assertEqual((archive / "disk.qcow2").read_bytes(), b"valuable-overlay-fixture")
        self.assertTrue((archive / "runs/run-one/processes.json").is_file())

    def test_booted_archive_refuses_live_run(self):
        self.stopped_run(live=True)
        self.cli("--apply", "--archive-stopped-failed", expected=2)
        self.assertTrue(self.profile.exists())

    def test_interrupted_provisioning_requires_stopped_runs(self):
        self.stopped_run(live=True)
        self.data["status"] = "provisioning"
        recover.paths.atomic_json(self.profile / "profile.json", self.data)
        self.cli("--apply", "--archive-stopped-failed", expected=2)
        self.assertTrue(self.profile.exists())

    def test_stopped_interrupted_provisioning_can_be_archived_without_metadata_edit(self):
        self.stopped_run()
        self.data["status"] = "provisioning"
        recover.paths.atomic_json(self.profile / "profile.json", self.data)
        result = self.cli("--apply", "--archive-stopped-failed")
        archive = Path(json.loads(result.stdout.splitlines()[1])["preserved_profile"])
        self.assertEqual(json.loads((archive / "profile.json").read_text())["status"], "provisioning")

    def unknown_run(self):
        run = self.stopped_run()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            identity = recover.process.capture_identity(child.pid)
        finally:
            child.terminate()
            child.wait()
        identity["executable"] = "/usr/bin/qemu-system-x86_64"
        identity["argv"] = [identity["executable"], "-drive",
                            f"file={self.profile}/disk.qcow2,format=qcow2,if=virtio"]
        self.data["status"] = "unknown"
        self.data["runtime_identity"] = dict(identity, disk_path=str(self.profile / "disk.qcow2"),
                                             socket_path=str(self.base / "vfs.sock"), ssh_port=40222)
        recover.paths.atomic_json(self.profile / "profile.json", self.data)
        record = recover.process.registry_read(run / "processes.json", run.name)
        record["processes"]["qemu_1"]["identity"] = identity
        recover.paths.atomic_json(run / "processes.json", record)
        return run

    def test_unknown_cleanup_archives_only_after_owned_proxy_supervisor_stops(self):
        run = self.unknown_run()
        # Reproduce an old supervisor retaining both controller locks after the
        # controller exits. Disable only the new FD-release hook in this fixture.
        descriptors = []
        for lock in (self.root / ".allocation.lock", self.profile / ".profile.lock"):
            fd = os.open(lock, os.O_RDWR)
            fcntl.flock(fd, fcntl.LOCK_EX)
            descriptors.append(fd)
        wrapper = (
            "import importlib.util,sys; "
            "s=importlib.util.spec_from_file_location('old_process',sys.argv.pop(1)); "
            "p=importlib.util.module_from_spec(s); s.loader.exec_module(p); "
            "p.release_supervisor_lifecycle_fds=lambda:None; sys.exit(p.main())"
        )
        supervisor = subprocess.Popen(
            [sys.executable, "-B", "-c", wrapper, str(ROOT / "lib/vm/process.py"), "run-logged",
             "--registry", str(run / "processes.json"), "--run-id", run.name,
             "--role", "proxy", "--log", str(run / "proxy.log"), "--",
             sys.executable, "-c", "import time; time.sleep(60)",
             "--port-file", str(run / "proxy.port"), "--reload-marker", str(run / "proxy.ready")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=tuple(descriptors),
        )
        for fd in descriptors:
            os.close(fd)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = recover.process.registry_status(run / "processes.json", run.name)
                if state["processes"].get("proxy", {}).get("state") == "running":
                    break
                time.sleep(0.02)
            self.assertEqual(state["processes"].get("proxy", {}).get("state"), "running")
            self.cli("--archive-unknown-cleanup")
            self.assertIsNone(supervisor.poll())
            result = self.cli("--apply", "--archive-unknown-cleanup")
            archive = Path(json.loads(result.stdout.splitlines()[1])["preserved_profile"])
            self.assertEqual(json.loads((archive / "profile.json").read_text())["status"], "unknown")
            self.assertEqual((archive / "disk.qcow2").read_bytes(), b"valuable-overlay-fixture")
            supervisor.wait(timeout=5)
        finally:
            if supervisor.poll() is None:
                if (run / "processes.json").exists():
                    recover.process.registry_stop(run / "processes.json", run.name, 1)
                supervisor.terminate()
                supervisor.wait(timeout=5)

    def test_unknown_cleanup_refuses_nonproxy_live_identity(self):
        run = self.unknown_run()
        recover.process.registry_add(run / "processes.json", run.name, "virtiofsd",
                                     recover.process.capture_identity(os.getpid()))
        self.cli("--apply", "--archive-unknown-cleanup", expected=2)
        self.assertTrue(self.profile.exists())

    def test_unknown_cleanup_refuses_a_real_controller_lock_holder(self):
        self.unknown_run()
        with (self.root / ".allocation.lock").open("rb") as controller:
            fcntl.flock(controller, fcntl.LOCK_EX)
            self.cli("--apply", "--archive-unknown-cleanup", expected=2)
            self.assertTrue(self.profile.exists())

    def test_booted_archive_refuses_missing_registry(self):
        run = self.stopped_run()
        (run / "processes.json").unlink()
        self.cli("--apply", "--archive-stopped-failed", expected=2)
        self.assertTrue(self.profile.exists())

    def test_log_report_collates_stopped_profile_without_keys(self):
        run = self.stopped_run()
        log = run / "qemu_1.log"
        log.write_text("download failed HTTP 403\nssh-ed25519 " + "A" * 60 + "\n")
        log.chmod(0o600)
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts/vm-start-diagnostics.py"),
             "--state-root", str(self.root), "--profile", "test", "--logs"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)["log_report"]
        self.assertIn("HTTP 403", report["host"]["qemu_1.log"]["excerpt"])
        self.assertNotIn("A" * 60, result.stdout)
        self.assertEqual(report["guest"]["state"], "unavailable-no-unique-verified-listener")

    def reservation(self, identity):
        directory = self.root / ".reservations"
        directory.mkdir(mode=0o700)
        path = directory / "test.json"
        recover.paths.atomic_json(path, {"schema_version": 1, "profile": "test",
                                        "requested_mib": 8192, "owner_identity": identity})
        return path

    def test_live_reservation_owner_is_preserved(self):
        path = self.reservation(recover.process.capture_identity(os.getpid()))
        self.cli("--apply", expected=2)
        self.assertTrue(path.exists())
        self.assertTrue(self.profile.exists())

    def test_stopped_owner_reservation_is_released(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            identity = recover.process.capture_identity(child.pid)
        finally:
            child.terminate()
            child.wait()
        path = self.reservation(identity)
        self.cli("--apply")
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
