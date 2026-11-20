#!/usr/bin/env python3
"""Offline tests for versioned, private state shared by direct stage calls."""

from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ci_kvm_stage_state_test", ROOT / "tests/ci-kvm-stage-state.py")
assert SPEC and SPEC.loader
STATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STATE)
STATE_CLI = ROOT / "tests/ci-kvm-stage-state.py"


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ci-kvm-state-")
        self.parent = pathlib.Path(self.temporary.name)
        self.run_dir = self.parent / "run"
        self.args = SimpleNamespace(
            run_dir=str(self.run_dir), run_id="7654321", attempt="2",
            source_sha="a" * 40, workflow_sha="b" * 40,
            overall_seconds=180,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_private_run_state_is_exclusive_versioned_and_atomic(self):
        STATE.init(self.args)
        state = STATE.read_state(self.run_dir)
        self.assertEqual(state["version"], 1)
        self.assertEqual(os.stat(self.run_dir).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.run_dir / "state.json").st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            STATE.init(self.args)

    def test_wrong_attempt_expired_deadline_symlinks_and_overlay_replacement_refuse(self):
        STATE.init(self.args)
        state = STATE.read_state(self.run_dir)
        self.args.stage = "cleanup"
        STATE.validate_context(state, self.args)
        wrong_values = vars(self.args).copy()
        wrong_values["attempt"] = "9"
        wrong = SimpleNamespace(**wrong_values)
        with self.assertRaisesRegex(STATE.StateError, "provenance mismatch"):
            STATE.validate_context(state, wrong)
        work = pathlib.Path(state["work"])
        overlay = work / "guest.qcow2"
        overlay.write_text("original", encoding="utf-8")
        info = os.stat(overlay)
        state.update({
            "image": str(work / "noble.img"), "seed": str(work / "seed.iso"),
            "overlay": str(overlay), "serial_log": str(work / "serial.log"),
            "private_key": str(work / "id_ed25519"), "known_hosts": str(work / "known_hosts"),
            "overlay_dev": info.st_dev, "overlay_ino": info.st_ino,
        })
        for name in ("noble.img", "seed.iso", "serial.log", "id_ed25519", "known_hosts"):
            (work / name).write_text("private fixture", encoding="utf-8")
            (work / name).chmod(0o600)
        STATE.write_state(self.run_dir, state)
        self.args.stage = "config-tmux"
        state["stage_results"] = {"prepare": {"status": 0}, "boot": {"status": 0}}
        STATE.write_state(self.run_dir, state)
        STATE.validate_context(STATE.read_state(self.run_dir), self.args)
        replacement = work / "other.qcow2"
        replacement.write_text("replacement", encoding="utf-8")
        state["overlay"] = str(replacement)
        state["overlay_dev"] = info.st_dev
        state["overlay_ino"] = info.st_ino
        STATE.write_state(self.run_dir, state)
        with self.assertRaisesRegex(STATE.StateError, "identity changed"):
            STATE.validate_context(STATE.read_state(self.run_dir), self.args)
        state["deadline_monotonic"] = time.monotonic() - 1
        self.assertLess(state["deadline_monotonic"], time.monotonic())

    def test_wrong_modes_and_symlink_state_are_rejected(self):
        STATE.init(self.args)
        state_file = self.run_dir / "state.json"
        state_file.chmod(0o644)
        with self.assertRaises(STATE.StateError):
            STATE.read_state(self.run_dir)
        state_file.chmod(0o600)
        backup = self.run_dir / "backup.json"
        state_file.rename(backup)
        state_file.symlink_to(backup)
        with self.assertRaises(OSError):
            STATE.read_state(self.run_dir)

    def test_direct_stage_requires_successful_predecessor_record(self):
        STATE.init(self.args)
        state = STATE.read_state(self.run_dir)
        self.args.stage = "boot"
        with self.assertRaisesRegex(STATE.StateError, "prerequisite prepare"):
            STATE.validate_context(state, self.args)

    def test_prepare_initialization_allows_private_path_registration_and_partial_cleanup(self):
        STATE.init(self.args)
        command = [
            sys.executable, "-I", str(STATE_CLI), "--run-dir", str(self.run_dir),
            "--run-id", self.args.run_id, "--attempt", self.args.attempt,
            "--source-sha", self.args.source_sha, "--workflow-sha", self.args.workflow_sha,
        ]
        partial_get = subprocess.run(
            command[:3] + ["get"] + command[3:] + ["--stage", "cleanup", "--field", "work", "image"],
            check=False, capture_output=True, timeout=5,
        )
        self.assertEqual(partial_get.returncode, 0, partial_get.stderr.decode())
        self.assertIn(str(self.run_dir / "work").encode(), partial_get.stdout)
        registration = subprocess.run(
            command[:3] + ["set"] + command[3:] + ["--field", "port", "--value", "23456"],
            check=False, capture_output=True, timeout=5,
        )
        self.assertEqual(registration.returncode, 0, registration.stderr.decode())
        self.assertEqual(STATE.read_state(self.run_dir)["port"], 23456)


if __name__ == "__main__":
    unittest.main()
