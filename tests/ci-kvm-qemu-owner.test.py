#!/usr/bin/env python3
"""Offline tests for private lifecycle-owner process/socket boundaries."""

from __future__ import annotations

import importlib.util
import os
import pathlib
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STATE = load("ci_kvm_stage_state_test", ROOT / "tests/ci-kvm-stage-state.py")
OWNER = load("ci_kvm_qemu_owner_test", ROOT / "tests/ci-kvm-qemu-owner.py")
OWNER_CLI = ROOT / "tests/ci-kvm-qemu-owner.py"


class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ci-kvm-owner-")
        self.run_dir = pathlib.Path(self.temporary.name) / "run"
        self.run_dir.mkdir(mode=0o700)
        self.work = self.run_dir / "work"
        self.work.mkdir(mode=0o700)
        self.ctx = {
            "version": 1, "uid": os.getuid(), "run_id": "123456", "attempt": "3",
            "source_sha": "a" * 40, "workflow_sha": "b" * 40,
            "host_boot_id": pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
        self.state = {
            **self.ctx, "work": str(self.work), "overall_seconds": 120,
            "deadline_monotonic": time.monotonic() + 120,
            "qemu_generation": 0,
        }
        for name in ("noble.img", "seed.iso", "guest.qcow2", "serial.log", "id_ed25519", "known_hosts"):
            path = self.work / name
            path.write_text("offline fixture\n", encoding="utf-8")
            path.chmod(0o600)
            self.state[{
                "noble.img": "image", "seed.iso": "seed", "guest.qcow2": "overlay",
                "serial.log": "serial_log", "id_ed25519": "private_key", "known_hosts": "known_hosts",
            }[name]] = str(path)
        overlay_info = os.stat(self.state["overlay"])
        self.state["overlay_dev"] = overlay_info.st_dev
        self.state["overlay_ino"] = overlay_info.st_ino
        self.state["port"] = 22022
        self.args = type("Args", (), {
            "run_dir": str(self.run_dir), "run_id": self.ctx["run_id"],
            "attempt": self.ctx["attempt"], "source_sha": self.ctx["source_sha"],
            "workflow_sha": self.ctx["workflow_sha"],
        })()
        STATE.write_state(self.run_dir, self.state)
        self.command = ["python3", "-I", str(OWNER_CLI),
                        "--run-dir", str(self.run_dir),
                        "--run-id", self.ctx["run_id"], "--attempt", self.ctx["attempt"],
                        "--source-sha", self.ctx["source_sha"], "--workflow-sha", self.ctx["workflow_sha"]]
        self.owner = None

    def tearDown(self):
        if self.owner and self.owner.poll() is None:
            self.owner.send_signal(signal.SIGTERM)
            try:
                self.owner.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.owner.kill()
                self.owner.wait(timeout=2)
        self.temporary.cleanup()

    def _call(self, operation):
        return subprocess.run(self.command[:3] + [operation] + self.command[3:],
                              check=True, capture_output=True, text=True, timeout=15)

    def test_owner_owns_only_lifecycle_and_direct_action_processes_can_call_it(self):
        start = self._call("start-owner")
        self.assertIn('"status": "stopped"', start.stdout)
        owner_record = OWNER.load_json(self.run_dir / "owner.json")
        self.assertEqual(owner_record["owner"]["uid"], os.getuid())
        self.assertEqual(owner_record["owner"]["session"], owner_record["owner"]["pid"])
        self.assertIsNone(owner_record["qemu"])
        self.assertIn('"status": "stopped"', self._call("status").stdout)
        self.assertIn('"shutdown": true', self._call("shutdown").stdout)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                OWNER.proc_record(owner_record["owner"]["pid"])
            except OSError:
                break
            time.sleep(0.02)
        self.assertTrue(OWNER.verify_stopped(self.run_dir, self.ctx)["owner_gone"])

    def test_owner_sigterm_runs_its_bounded_exit_path(self):
        self._call("start-owner")
        record = OWNER.load_json(self.run_dir / "owner.json")["owner"]
        self.assertTrue(OWNER.PROCESS.signal_owned_process(
            record["pid"], record["starttime"], record["uid"], None,
            record["comm"], (str(OWNER_CLI),), signal.SIGTERM,
        ))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                current = OWNER.proc_record(record["pid"])
            except OSError:
                break
            if current["starttime"] != record["starttime"]:
                break
            time.sleep(0.05)
        self.assertTrue(OWNER.verify_stopped(self.run_dir, self.ctx)["owner_gone"])

    def test_overlay_replacement_and_wrong_run_identity_are_refused(self):
        changed = self.work / "replacement.qcow2"
        changed.write_text("replacement", encoding="utf-8")
        changed.chmod(0o600)
        self.state["overlay"] = str(changed)
        STATE.write_state(self.run_dir, self.state)
        with self.assertRaisesRegex(OWNER.Refused, "overlay identity changed"):
            OWNER.state_paths(self.run_dir, self.ctx)
        wrong = dict(self.ctx, run_id="999999")
        with self.assertRaisesRegex(OWNER.Refused, "workflow attempt"):
            OWNER.state_paths(self.run_dir, wrong)

    def test_reused_qemu_argument_builder_preserves_kvm_and_restricted_boundary(self):
        initial = OWNER.qemu_args(self.state, "initial")
        restricted = OWNER.qemu_args(self.state, "restricted-restart")
        self.assertIn("-machine", initial)
        self.assertTrue(any("accel=kvm" in arg for arg in initial))
        self.assertFalse(any("accel=tcg" in arg for arg in initial + restricted))
        self.assertFalse(any("restrict=on" in arg for arg in initial))
        self.assertTrue(any("restrict=on" in arg for arg in restricted))
        self.assertTrue(any(self.state["overlay"] in arg for arg in initial))
        self.assertTrue(any(self.state["overlay"] in arg for arg in restricted))

    def test_owner_refuses_symlinked_socket_or_state(self):
        state_path = self.run_dir / "state.json"
        saved = self.run_dir / "saved.json"
        state_path.rename(saved)
        state_path.symlink_to(saved)
        with self.assertRaises(OWNER.Refused):
            OWNER.load_json(state_path)

    def test_owner_accepts_approved_cap_and_refuses_above_maximum(self):
        self.state["overall_seconds"] = 2400
        self.state["deadline_monotonic"] = time.monotonic() + 2400
        STATE.write_state(self.run_dir, self.state)
        owner = OWNER.Owner(self.run_dir, self.ctx)
        self.assertGreater(owner.deadline, time.monotonic())
        self.state["overall_seconds"] = 3001
        STATE.write_state(self.run_dir, self.state)
        with self.assertRaisesRegex(OWNER.Refused, "validated bound"):
            OWNER.Owner(self.run_dir, self.ctx)

    def test_shutdown_remains_allowed_after_overall_deadline(self):
        owner = OWNER.Owner(self.run_dir, self.ctx)
        owner.deadline = time.monotonic() - 1
        owner.validate_owner = lambda: None
        self.assertTrue(owner.dispatch("shutdown")["shutdown"])
        with self.assertRaisesRegex(OWNER.Refused, "overall deadline expired"):
            owner.dispatch("start-initial")


if __name__ == "__main__":
    unittest.main()
