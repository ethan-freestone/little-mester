#!/usr/bin/env python3
"""Unprivileged REAL_PROCESS tests for persistent-parent state primitives."""

import contextlib
import base64
import importlib.util
import io
import json
import os
import pathlib
import re
import sqlite3
import signal
import stat
import subprocess
import shlex
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULE = ROOT / "lib/vm/parent-state.py"
SPEC = importlib.util.spec_from_file_location("parent_state", MODULE)
parent_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(parent_state)


def _script_for_fixture_uid(source: str, uid: int | None = None) -> str:
    """Adapt only the guest's fixed mester UID for host-owned temp fixtures."""
    fixture_uid = os.getuid() if uid is None else uid
    source_uids = [int(value) for value in re.findall(r"(?:s\.st_uid==|st\.st_uid!=)(\d+)", source)]
    if not source_uids or any(value != 1000 for value in source_uids):
        raise AssertionError("extracted guest source no longer pins mester UID 1000")
    adapted = re.sub(r"(s\.st_uid==)\d+", rf"\g<1>{fixture_uid}", source)
    adapted = re.sub(r"(st\.st_uid!=)\d+", rf"\g<1>{fixture_uid}", adapted)
    return adapted


class ParentStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.temp.name) / "home"
        self.home.mkdir(mode=0o700)
        os.chmod(self.home, 0o700)

    def tearDown(self):
        self.temp.cleanup()

    def parent_dir(self):
        return self.home / ".local/state/little-mester/parent-v3/default"

    def test_fixed_m2_guest_probe_scripts_are_valid_python(self):
        for name in (
            "PARENT_CANARY_CREATE_COMMAND", "PARENT_CANARY_CHECK_COMMAND",
            "PARENT_CANARY_REMOVE_COMMAND", "PARENT_AUTH_CHECK_COMMAND",
            "PARENT_NETWORK_CHECK_COMMAND", "PARENT_TUI_STATE_COMMAND",
            "PARENT_PROTECTION_PREPARE_COMMAND", "PARENT_PROTECTION_CHECK_COMMAND",
            "PARENT_PROTECTION_CLEANUP_COMMAND",
        ):
            with self.subTest(command=name):
                command = getattr(parent_state, name)
                wrapper = shlex.split(command)[3]
                encoded = re.search(r"b64decode\('([^']+)'\)", wrapper).group(1)
                compile(base64.b64decode(encoded), name, "exec")
        compile(parent_state._M2_HISTORY_PROBE_SCRIPT, "history-probe", "exec")
        compile(parent_state._M2_MODELS_PROBE_SCRIPT, "models-probe", "exec")
        compile(parent_state._M2_TELEMETRY_BEHAVIOR_SCRIPT, "telemetry-behavior", "exec")

    def test_model_list_probe_is_exactly_allowlisted_and_redacts_output(self):
        command = parent_state.parent_models_probe_command("openai/gpt-test")
        self.assertTrue(parent_state._allowed_models_probe(command))
        self.assertFalse(parent_state._allowed_models_probe(command + "; touch /tmp/unsafe"))
        with self.assertRaises(parent_state.Refused):
            parent_state.parent_models_probe_command("attacker/model")
        output = io.StringIO()
        completed = subprocess.CompletedProcess([], 0, b"openai/gpt-test\nopenai/other\n", b"")
        with mock.patch.object(sys, "argv", ["models-probe", "openai/gpt-test"]), \
                mock.patch.object(subprocess, "run", return_value=completed), \
                contextlib.redirect_stdout(output):
            exec(compile(parent_state._M2_MODELS_PROBE_SCRIPT, "models-probe", "exec"),
                 {"__name__": "__main__"})
        report = json.loads(output.getvalue())
        self.assertTrue(report["selected_openai_model_listed"])
        self.assertNotIn("openai/gpt-test", output.getvalue())

    def test_guest_term_probe_is_exactly_allowlisted(self):
        command = parent_state.parent_term_probe_command("xterm-256color")
        self.assertTrue(parent_state._allowed_term_probe(command))
        self.assertFalse(parent_state._allowed_term_probe(command + " extra"))
        with self.assertRaises(parent_state.Refused):
            parent_state.parent_term_probe_command("xterm;touch")

    def test_history_probe_recognizes_instructed_prompt_and_exact_pinned_model_answers(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = pathlib.Path(temporary) / "opencode.db"
            db = sqlite3.connect(database)
            db.execute("CREATE TABLE session_message(session_id TEXT, type TEXT, seq INTEGER, data TEXT)")
            session_id = "ses-private-history-fixture"
            first = "AB12CD34EF56"
            second = "9876ZYXWVU12"
            rows = (
                (session_id, "user", 1, json.dumps({
                    "text": f"Reverse this string and return only the result: {first}"})),
                (session_id, "assistant", 2, json.dumps({
                    "content": [{"type": "text", "text": first[::-1]}],
                    "model": {"providerID": "openai", "id": "gpt-test"}})),
                (session_id, "user", 3, json.dumps({
                    "text": f"Reverse this string and return only the result: {second}"})),
                (session_id, "assistant", 4, json.dumps({
                    "content": [{"type": "text", "text": second[::-1]}],
                    "model": {"providerID": "openai", "id": "gpt-test"}})),
            )
            db.executemany("INSERT INTO session_message VALUES (?,?,?,?)", rows)
            db.commit()
            db.close()
            database.chmod(0o600)
            source = parent_state._M2_HISTORY_PROBE_SCRIPT.replace(
                "p='/home/mester/.local/share/opencode/opencode.db'",
                f"p={str(database)!r}")
            namespace = {"__name__": "__main__"}
            output = io.StringIO()
            wrong_uid_source = _script_for_fixture_uid(source, os.getuid() + 1)
            with mock.patch.object(sys, "argv", ["history-probe", second, second[::-1],
                                                   "openai/gpt-test", first]), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    exec(compile(wrong_uid_source, "history-probe-wrong-owner", "exec"), namespace)
            self.assertEqual(error.exception.code, 44)
            source = _script_for_fixture_uid(source)
            with mock.patch.object(sys, "argv", ["history-probe", second, second[::-1],
                                                   "openai/gpt-test", first]), \
                    contextlib.redirect_stdout(output):
                exec(compile(source, "history-probe", "exec"), namespace)
            result = json.loads(output.getvalue())
            self.assertTrue(result["first_response_persisted"])
            self.assertTrue(result["second_response_persisted"])
            self.assertTrue(result["same_session"])
            self.assertTrue(result["conversation_ordered"])
            self.assertEqual(result["session_id_sha256"],
                             __import__("hashlib").sha256(session_id.encode()).hexdigest())
            self.assertNotIn(first, output.getvalue())
            self.assertNotIn(second, output.getvalue())

            db = sqlite3.connect(database)
            db.execute("UPDATE session_message SET data=? WHERE seq=2", (json.dumps({
                "content": [{"type": "text", "text": "The answer is " + first[::-1]}],
                "snapshot": {"start": first[::-1]},
                "model": {"providerID": "openai", "id": "gpt-test"}}),))
            db.execute("UPDATE session_message SET seq=seq+10 WHERE seq IN (1, 2)")
            db.commit()
            db.close()
            output = io.StringIO()
            with mock.patch.object(sys, "argv", ["history-probe", first, first[::-1],
                                                   "openai/gpt-test", "-"]), \
                    contextlib.redirect_stdout(output):
                exec(compile(source, "history-probe-negative", "exec"), {"__name__": "__main__"})
            self.assertFalse(json.loads(output.getvalue())["first_response_persisted"])

            db = sqlite3.connect(database)
            db.execute("UPDATE session_message SET data=? WHERE seq=12", (json.dumps({
                "content": [{"type": "text", "text": first[::-1]}],
                "model": {"providerID": "openai", "id": "gpt-test"}}),))
            db.commit()
            db.close()
            output = io.StringIO()
            with mock.patch.object(sys, "argv", ["history-probe", second, second[::-1],
                                                   "openai/gpt-test", first]), \
                    contextlib.redirect_stdout(output):
                exec(compile(source, "history-probe-out-of-order", "exec"), {"__name__": "__main__"})
            self.assertFalse(json.loads(output.getvalue())["conversation_ordered"])
            db = sqlite3.connect(database)
            db.execute("UPDATE session_message SET seq=seq-10 WHERE seq IN (11, 12)")
            db.commit()
            db.close()

    def test_hostile_config_cleanup_validates_complete_anchored_inventory_before_unlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = pathlib.Path(temporary) / "workspace"
            workspace.mkdir(mode=0o750)
            os.chmod(workspace, 0o750)
            opencode_config = parent_state._M2_HOSTILE_CONFIG.encode() + b"\n"
            plugin = parent_state._M2_HOSTILE_PLUGIN.encode()
            config_path = workspace / "opencode.json"
            config_path.write_bytes(opencode_config)
            config_path.chmod(0o600)
            probe_dir = workspace / ".opencode/plugins/lm-m2-hostile-probe"
            probe_dir.mkdir(parents=True, mode=0o700)
            for directory in (workspace / ".opencode", workspace / ".opencode/plugins", probe_dir):
                directory.chmod(0o700)
            plugin_path = probe_dir / "index.ts"
            plugin_path.write_bytes(plugin)
            plugin_path.chmod(0o600)
            source = parent_state._M2_PROTECTION_CLEANUP_SCRIPT.replace(
                "w='/home/mester/workspace'", f"w={str(workspace)!r}")
            wrong_uid_source = _script_for_fixture_uid(source, os.getuid() + 1)
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    exec(compile(wrong_uid_source, "protection-cleanup-wrong-owner", "exec"),
                         {"__name__": "__main__"})
            self.assertEqual(error.exception.code, 44)
            self.assertTrue(config_path.exists())
            self.assertTrue(plugin_path.exists())
            source = _script_for_fixture_uid(source)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                exec(compile(source, "protection-cleanup", "exec"), {"__name__": "__main__"})
            self.assertEqual(json.loads(out.getvalue())["status"], "REMOVED")
            self.assertFalse(config_path.exists())
            self.assertFalse((workspace / ".opencode").exists())

    def test_hostile_config_cleanup_refuses_symlinked_parent_before_deleting_any_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = pathlib.Path(temporary) / "workspace"
            outside = pathlib.Path(temporary) / "outside"
            workspace.mkdir(mode=0o750)
            outside.mkdir(mode=0o700)
            os.chmod(workspace, 0o750)
            config_path = workspace / "opencode.json"
            config_path.write_bytes(parent_state._M2_HOSTILE_CONFIG.encode() + b"\n")
            config_path.chmod(0o600)
            marker = outside / "index.ts"
            marker.write_bytes(parent_state._M2_HOSTILE_PLUGIN.encode())
            marker.chmod(0o600)
            (workspace / ".opencode").symlink_to(outside, target_is_directory=True)
            source = _script_for_fixture_uid(parent_state._M2_PROTECTION_CLEANUP_SCRIPT.replace(
                "w='/home/mester/workspace'", f"w={str(workspace)!r}"))
            with self.assertRaises(OSError):
                exec(compile(source, "protection-cleanup", "exec"), {"__name__": "__main__"})
            self.assertTrue(config_path.exists())
            self.assertTrue(marker.exists())

    def test_workspace_canary_check_requires_exact_payload_not_just_file_presence(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = pathlib.Path(temporary) / "workspace"
            workspace.mkdir(mode=0o750)
            os.chmod(workspace, 0o750)
            create_source = parent_state._WORKSPACE_CANARY_CREATE_SCRIPT.replace(
                "'/home/mester/workspace'", repr(str(workspace)))
            create_source = _script_for_fixture_uid(create_source)
            with contextlib.redirect_stdout(io.StringIO()):
                exec(compile(create_source, "canary-create", "exec"), {"__name__": "__main__"})
            check_source = parent_state._WORKSPACE_CANARY_CHECK_SCRIPT.replace(
                "'/home/mester/workspace'", repr(str(workspace)))
            wrong_uid_check = _script_for_fixture_uid(check_source, os.getuid() + 1)
            check_source = _script_for_fixture_uid(check_source)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(compile(check_source, "canary-check", "exec"), {"__name__": "__main__"})
            self.assertEqual(json.loads(output.getvalue())["status"], "PRESENT")
            with self.assertRaises(SystemExit) as error:
                exec(compile(wrong_uid_check, "canary-check-wrong-owner", "exec"), {"__name__": "__main__"})
            self.assertEqual(error.exception.code, 44)
            marker = workspace / ".lm-m2-owner-acceptance-canary"
            marker.write_bytes(b"different contents")
            marker.chmod(0o600)
            with self.assertRaises(SystemExit) as error:
                exec(compile(check_source, "canary-check-tampered", "exec"), {"__name__": "__main__"})
            self.assertEqual(error.exception.code, 45)

    def test_retirement_requires_exact_confirmation_and_preserves_unknown_state(self):
        parent_state.initialize(home=self.home)
        parent_dir = self.parent_dir()
        metadata = parent_dir / "parent.json"
        original = metadata.read_bytes()
        status = parent_state.read_status(home=self.home)
        phrase = parent_state._retirement_phrase(status.get("run_id"), status["disk"]["device"], status["disk"]["inode"])
        with self.assertRaisesRegex(parent_state.Refused, "did not match"):
            parent_state.retire_parent(confirmation="yes", home=self.home)
        self.assertEqual(metadata.read_bytes(), original)

        unknown = parent_dir / "owner-note.txt"
        unknown.write_text("preserve", encoding="utf-8")
        unknown.chmod(0o600)
        with self.assertRaisesRegex(parent_state.Refused, "unexpected parent state entry"):
            parent_state.retire_parent(confirmation=phrase, home=self.home)
        self.assertEqual(metadata.read_bytes(), original)
        self.assertEqual(unknown.read_text(encoding="utf-8"), "preserve")

    def test_retirement_rejects_parent_run_changed_since_confirmation(self):
        parent_state.initialize(home=self.home)
        displayed = parent_state.read_status(home=self.home)
        phrase = parent_state._retirement_phrase(displayed.get("run_id"),
                                                 displayed["disk"]["device"], displayed["disk"]["inode"])
        changed = dict(displayed, run_id="different-run")
        with mock.patch.object(parent_state, "read_status", return_value=changed):
            with self.assertRaisesRegex(parent_state.Refused, "changed while awaiting"):
                parent_state.retire_parent(confirmation=phrase, expected_identity=displayed, home=self.home)

    def test_authorized_stop_refuses_replacement_run_before_any_process_lookup(self):
        status = {"status": "RUNNING", "run_id": "replacement",
                  "disk": {"device": 10, "inode": 20}}
        with mock.patch.object(parent_state, "read_status", return_value=status):
            with self.assertRaisesRegex(parent_state.Refused, "changed before the authorized stop"):
                parent_state.request_supervisor_stop(
                    home=self.home, expected_run_id="authorized",
                    expected_disk_identity=(10, 20))

    def test_retirement_preserves_complete_bundle_and_lock_inode(self):
        parent_state.initialize(home=self.home)
        directory = self.parent_dir()
        directory_fd = parent_state._open_directory_chain(create=False, home_override=self.home)
        try:
            parent_state._atomic_bytes(directory_fd, "known_hosts", b"old-pinned-host-key")
        finally:
            os.close(directory_fd)
        lock_path = directory / "lifecycle.lock"
        lock_identity = (lock_path.stat().st_dev, lock_path.stat().st_ino)
        status = parent_state.read_status(home=self.home)
        phrase = parent_state._retirement_phrase(status.get("run_id"), None, None)
        result = parent_state.retire_parent(confirmation=phrase, expected_identity=status, home=self.home)
        self.assertTrue(result["retired"])
        bundle = directory / result["retired_bundle"]
        self.assertTrue((bundle / "parent.json").is_file())
        self.assertTrue((bundle / "known_hosts").is_file())
        self.assertEqual((bundle / "known_hosts").read_bytes(), b"old-pinned-host-key")
        self.assertEqual((lock_path.stat().st_dev, lock_path.stat().st_ino), lock_identity)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "RETIRED")
        fresh = parent_state.initialize(home=self.home)
        self.assertFalse(fresh.get("ssh_identity"))
        self.assertTrue((bundle / "parent.json").is_file())
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "UNPROVISIONED")

    def test_retirement_resumes_after_interrupted_rename_without_losing_old_bundle(self):
        parent_state.initialize(home=self.home)
        directory = self.parent_dir()
        directory_fd = parent_state._open_directory_chain(create=False, home_override=self.home)
        try:
            parent_state._atomic_bytes(directory_fd, "known_hosts", b"retained-identity")
        finally:
            os.close(directory_fd)
        displayed = parent_state.read_status(home=self.home)
        phrase = parent_state._retirement_phrase(None, None, None)
        real_rename = os.rename
        count = 0

        def interrupt_after_first(*args, **kwargs):
            nonlocal count
            real_rename(*args, **kwargs)
            count += 1
            if count == 1:
                raise OSError("injected interruption after atomic move")

        with mock.patch.object(parent_state.os, "rename", side_effect=interrupt_after_first):
            with self.assertRaisesRegex(OSError, "injected interruption"):
                parent_state.retire_parent(confirmation=phrase, expected_identity=displayed, home=self.home)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "RETIRING")
        resumed = parent_state.read_status(home=self.home)
        result = parent_state.retire_parent(confirmation=phrase, expected_identity=resumed, home=self.home)
        self.assertTrue(result["retired"])
        bundle = directory / result["retired_bundle"]
        self.assertEqual((bundle / "known_hosts").read_bytes(), b"retained-identity")
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "RETIRED")

    def test_retirement_resumes_after_journal_creation_and_before_first_move(self):
        parent_state.initialize(home=self.home)
        directory = self.parent_dir()
        directory_fd = parent_state._open_directory_chain(create=False, home_override=self.home)
        try:
            parent_state._atomic_bytes(directory_fd, "known_hosts", b"retained-at-journal-boundary")
        finally:
            os.close(directory_fd)
        displayed = parent_state.read_status(home=self.home)
        phrase = parent_state._retirement_phrase(None, None, None)
        with mock.patch.object(parent_state, "_resume_retirement",
                               side_effect=OSError("injected after journal commit")):
            with self.assertRaisesRegex(OSError, "after journal commit"):
                parent_state.retire_parent(confirmation=phrase, expected_identity=displayed, home=self.home)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "RETIRING")
        directory_fd = os.open(directory, parent_state.DIRECTORY_FLAGS)
        try:
            journal = parent_state._read_json(directory_fd, "retirement.json")
        finally:
            os.close(directory_fd)
        self.assertEqual(journal["phase"], "PREPARING")
        resumed = parent_state.read_status(home=self.home)
        result = parent_state.retire_parent(confirmation=phrase, expected_identity=resumed, home=self.home)
        self.assertTrue(result["retired"])
        self.assertEqual((directory / result["retired_bundle"] / "known_hosts").read_bytes(),
                         b"retained-at-journal-boundary")

    def test_retirement_resumes_after_bundle_creation_before_identity_commit(self):
        parent_state.initialize(home=self.home)
        directory = self.parent_dir()
        directory_fd = parent_state._open_directory_chain(create=False, home_override=self.home)
        try:
            parent_state._atomic_bytes(directory_fd, "known_hosts", b"retained-at-bundle-boundary")
        finally:
            os.close(directory_fd)
        displayed = parent_state.read_status(home=self.home)
        phrase = parent_state._retirement_phrase(None, None, None)
        atomic_json = parent_state._atomic_json

        def fail_moving_commit(fd, name, value):
            if name == "retirement.json" and value.get("phase") == "MOVING":
                raise OSError("injected after bundle creation")
            return atomic_json(fd, name, value)

        with mock.patch.object(parent_state, "_atomic_json", side_effect=fail_moving_commit):
            with self.assertRaisesRegex(OSError, "after bundle creation"):
                parent_state.retire_parent(confirmation=phrase, expected_identity=displayed, home=self.home)
        status = parent_state.read_status(home=self.home)
        self.assertEqual(status["status"], "RETIRING")
        directory_fd = os.open(directory, parent_state.DIRECTORY_FLAGS)
        try:
            journal = parent_state._read_json(directory_fd, "retirement.json")
        finally:
            os.close(directory_fd)
        bundle = directory / journal["bundle"]
        self.assertTrue(bundle.is_dir())
        self.assertEqual(list(bundle.iterdir()), [])
        result = parent_state.retire_parent(confirmation=phrase, expected_identity=status, home=self.home)
        self.assertTrue(result["retired"])
        self.assertEqual((directory / result["retired_bundle"] / "known_hosts").read_bytes(),
                         b"retained-at-bundle-boundary")

    def test_retirement_resumes_after_all_moves_before_retired_commit(self):
        parent_state.initialize(home=self.home)
        directory = self.parent_dir()
        directory_fd = parent_state._open_directory_chain(create=False, home_override=self.home)
        try:
            parent_state._atomic_bytes(directory_fd, "known_hosts", b"retained-before-final-journal")
        finally:
            os.close(directory_fd)
        displayed = parent_state.read_status(home=self.home)
        phrase = parent_state._retirement_phrase(None, None, None)
        atomic_json = parent_state._atomic_json

        def fail_final_commit(fd, name, value):
            if name == "retirement.json" and value.get("phase") == "RETIRED":
                raise OSError("injected before retired commit")
            return atomic_json(fd, name, value)

        with mock.patch.object(parent_state, "_atomic_json", side_effect=fail_final_commit):
            with self.assertRaisesRegex(OSError, "before retired commit"):
                parent_state.retire_parent(confirmation=phrase, expected_identity=displayed, home=self.home)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "RETIRING")
        resumed = parent_state.read_status(home=self.home)
        result = parent_state.retire_parent(confirmation=phrase, expected_identity=resumed, home=self.home)
        self.assertTrue(result["retired"])
        self.assertEqual((directory / result["retired_bundle"] / "known_hosts").read_bytes(),
                         b"retained-before-final-journal")

    def write_stale_running_fixture(self):
        parent = parent_state.initialize(home=self.home)
        directory = self.parent_dir()
        disk_path = directory / "disk.qcow2"
        disk_path.write_bytes(b"retained-disk-fixture")
        disk_path.chmod(0o600)
        disk_info = disk_path.stat()
        parent["disk"].update({"provisioned": True, "device": disk_info.st_dev,
                               "inode": disk_info.st_ino})
        run_id = "0123456789abcdef0123456789abcdef"
        old_boot = "00000000-0000-0000-0000-000000000000"
        executable = os.path.realpath(sys.executable)
        supervisor_path = "/opt/little-mester/v3/" + "a" * 40 + "/lib/vm/parent-supervisor.py"
        supervisor_argv = [executable, "-I", supervisor_path, "--parent-id", "default",
                           "--run-id", run_id, "--lock-fd", "3", "--handoff-fd", "4",
                           "--state-home", str(self.home)]
        supervisor = {"schema": 1, "pid": 1234567, "uid": os.getuid(),
                      "host_boot_id": old_boot, "start_time": "123", "exe": executable,
                      "argv": supervisor_argv}
        fd = parent_state._open_directory_chain(create=False, home_override=self.home)
        try:
            parent_state._atomic_json(fd, "parent.json", parent)
            transitions = {}
            for role, token in (("qemu", "1"), ("proxy", "2")):
                target_path = f"child-{role}.target.json"
                identity_path = f"child-{role}.identity.json"
                if role == "qemu":
                    target_exe = "/usr/bin/qemu-system-x86_64"
                    target_argv = [target_exe, "30"]
                else:
                    target_exe = executable
                    proxy_script = str((ROOT / "lib/vm/proxy.py").resolve())
                    target_argv = [target_exe, "-I", proxy_script, "--port-file",
                                   str(self.parent_dir() / "proxy.port")]
                contracts = ([{"fd": 3, "purpose": "parent-disk", "device": disk_info.st_dev,
                               "inode": disk_info.st_ino, "access": "read-write", "fdset": 1}]
                             if role == "qemu" else [])
                gate_argv = [executable, "-I", str((ROOT / "lib/vm/parent-gate.py").resolve()),
                             "--release-fd", "10", "--token", token * 32,
                             "--target", target_path, "--identity", identity_path,
                             "--state-dir-fd", "20", "--disk-fd", "30" if contracts else "-1"]
                gate_identity = {"schema": 1, "pid": 1234568 if role == "qemu" else 1234569,
                                 "uid": os.getuid(), "host_boot_id": old_boot,
                                 "start_time": "124", "exe": executable, "argv": gate_argv}
                transition = parent_state.create_exec_transition(
                    gate_identity, target_exe, target_argv, release_token=token * 32,
                    target_path=target_path, identity_path=identity_path, fd_contracts=contracts)
                transition["host_boot_id"] = old_boot
                parent_state._atomic_json(fd, target_path,
                    {"executable": target_exe, "argv": target_argv, "fd_contracts": contracts})
                parent_state._atomic_json(fd, identity_path, transition)
                transitions[role] = transition
            qemu = {"schema": 1, "pid": transitions["qemu"]["pid"], "uid": os.getuid(),
                    "host_boot_id": old_boot, "start_time": transitions["qemu"]["start_time"],
                    "exe": transitions["qemu"]["target"]["exe"],
                    "argv": transitions["qemu"]["target"]["argv"]}
            run = {"schema": 1, "parent_id": "default", "run_id": run_id,
                   "phase": "RUNNING", "supervisor": supervisor, "qemu": qemu}
            parent_state._atomic_json(fd, "active-run.json", run)
            # Existing recovery artifacts are private and validated before cleanup.
            (directory / "proxy.port").write_text("12345\n")
            (directory / "proxy.port").chmod(0o600)
        finally:
            os.close(fd)
        return run, disk_info, disk_path

    def stale_reconcile_patches(self):
        return (mock.patch.object(parent_state, "_disk_status", side_effect=lambda _fd, p: {
                    **p["disk"], "host_allocated_bytes": 4096, "file_bytes": 22}),
                mock.patch.object(parent_state, "_trusted_parent_runtime_script", return_value=True),
                mock.patch.object(parent_state, "_scan_stale_disk_holders"))

    def test_stale_boot_reconciliation_preview_and_execute_preserve_history_and_disk(self):
        run, _disk_info, disk_path = self.write_stale_running_fixture()
        before = disk_path.read_bytes()
        active_before = (self.parent_dir() / "active-run.json").read_bytes()
        with self.stale_reconcile_patches()[0], self.stale_reconcile_patches()[1], \
                self.stale_reconcile_patches()[2] as scan:
            preview = parent_state.stale_boot_reconciliation(home=self.home)
            self.assertEqual((preview["action"], preview["status"]), ("reconcile", "RECONCILABLE"))
            self.assertTrue(preview["dry_run"])
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")
            self.assertEqual((self.parent_dir() / "active-run.json").read_bytes(), active_before)
            self.assertFalse((self.parent_dir() / parent_state._stale_reconciliation_filename(
                run["run_id"])).exists())
            with mock.patch.object(parent_state.signal, "pidfd_send_signal") as send_signal:
                result = parent_state.stale_boot_reconciliation(home=self.home, execute=True)
                send_signal.assert_not_called()
            self.assertEqual(result["status"], "STOPPED")
            self.assertTrue(result["disk_retained"])
            scan.assert_called()
        self.assertEqual(disk_path.read_bytes(), before)
        audit_path = self.parent_dir() / parent_state._stale_reconciliation_filename(run["run_id"])
        audited = json.loads(audit_path.read_text())
        self.assertEqual(audited["historical_run"], run)
        self.assertEqual(audited["transaction"], "COMPLETE")
        self.assertEqual(audited["historical_transitions"]["qemu"]["identity_path"],
                         "child-qemu.identity.json")
        self.assertEqual(audited["historical_transitions"]["proxy"]["identity_path"],
                         "child-proxy.identity.json")
        self.assertNotIn("release_token", audited["historical_transitions"]["qemu"])
        self.assertNotIn("1" * 32, json.dumps(audited["historical_transitions"]["qemu"]))
        with self.stale_reconcile_patches()[0], self.stale_reconcile_patches()[1]:
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "STOPPED")
            fd = parent_state._open_directory_chain(create=False, home_override=self.home)
            try:
                parent_state._validated_recovery_plan(fd, home=self.home)
            finally:
                os.close(fd)
            self.assertEqual(parent_state.recovery_preview(home=self.home)["status"], "STOPPED")

    def test_repeated_stop_returns_run_id_and_accepts_shared_shutdown_budget(self):
        run_id = "0123456789abcdef0123456789abcdef"
        with (mock.patch.object(parent_state, "read_status", return_value={
                "status": "STOPPED", "run_id": run_id}),
              mock.patch.object(parent_state, "_require_completed_stop") as verify):
            result = parent_state.request_supervisor_stop(
                home=self.home, timeout_seconds=61)
        self.assertEqual(result["status"], "STOPPED")
        self.assertEqual(result["run_id"], run_id)
        self.assertTrue(result["disk_retained"])
        verify.assert_called_once_with(home=self.home, expected_run_id=run_id)

    def test_stale_boot_reconciliation_audit_tampering_remains_ambiguous(self):
        self.write_stale_running_fixture()
        with self.stale_reconcile_patches()[0], self.stale_reconcile_patches()[1], \
                self.stale_reconcile_patches()[2]:
            parent_state.stale_boot_reconciliation(home=self.home, execute=True)
        audit_path = self.parent_dir() / parent_state._stale_reconciliation_filename(
            "0123456789abcdef0123456789abcdef")
        audit = json.loads(audit_path.read_text())
        audit["operator_uid"] = os.getuid() + 1
        audit_path.write_text(json.dumps(audit) + "\n")
        audit_path.chmod(0o600)
        with self.stale_reconcile_patches()[0], self.stale_reconcile_patches()[1]:
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")

    def test_stale_boot_reconciliation_retries_after_interrupted_cleanup(self):
        _run, _disk_info, _disk_path = self.write_stale_running_fixture()
        cleanup = mock.patch.object(parent_state, "_recovery_remove_artifact",
                                    side_effect=parent_state.Refused("simulated interruption"))
        with self.stale_reconcile_patches()[0], self.stale_reconcile_patches()[1], \
                self.stale_reconcile_patches()[2], cleanup:
            with self.assertRaisesRegex(parent_state.Refused, "simulated interruption"):
                parent_state.stale_boot_reconciliation(home=self.home, execute=True)
        with mock.patch.object(parent_state, "_trusted_parent_runtime_script", return_value=True):
            self.assertEqual(parent_state.read_status(home=self.home)["status"],
                             "RECONCILIATION_PENDING")
        audit = json.loads((self.parent_dir() / parent_state._stale_reconciliation_filename(
            "0123456789abcdef0123456789abcdef")).read_text())
        self.assertEqual(audit["transaction"], "PENDING")
        self.assertTrue((self.parent_dir() / parent_state._stale_reconciliation_filename(
            "0123456789abcdef0123456789abcdef")).exists())
        with self.stale_reconcile_patches()[0], self.stale_reconcile_patches()[1], \
                self.stale_reconcile_patches()[2]:
            retried = parent_state.stale_boot_reconciliation(home=self.home, execute=True)
            self.assertEqual(retried["status"], "STOPPED")

    def test_stale_reconciliation_failure_after_durable_publication_is_safe(self):
        artifact_names = ("supervisor.sock", "qmp.sock", "proxy.port")
        for boundary in ("pending-audit", "stopping-tombstone", *artifact_names,
                         "complete-audit"):
            with self.subTest(boundary=boundary):
                fixture = type(self)()
                fixture.setUp()
                fixture.write_stale_running_fixture()
                original_atomic = parent_state._atomic_json
                original_remove = parent_state._recovery_remove_artifact
                seen = {"audit": 0}

                def atomic(fd, name, value):
                    original_atomic(fd, name, value)
                    if name.startswith(parent_state.STALE_RECONCILIATION_PREFIX):
                        seen["audit"] += 1
                        if boundary == "pending-audit" and seen["audit"] == 1:
                            raise parent_state.Refused("injected after pending audit")
                        if boundary == "complete-audit" and value.get("transaction") == "COMPLETE":
                            raise parent_state.Refused("injected after completion audit")
                    if boundary == "stopping-tombstone" and name == "active-run.json":
                        raise parent_state.Refused("injected after tombstone")

                def remove(fd, name, kind, *, expected=None):
                    original_remove(fd, name, kind, expected=expected)
                    if boundary == name:
                        raise parent_state.Refused("injected after artifact removal")

                try:
                    with (fixture.stale_reconcile_patches()[0], fixture.stale_reconcile_patches()[1],
                          fixture.stale_reconcile_patches()[2],
                          mock.patch.object(parent_state, "_atomic_json", side_effect=atomic),
                          mock.patch.object(parent_state, "_recovery_remove_artifact",
                                           side_effect=remove)):
                        with fixture.assertRaises(parent_state.Refused):
                            parent_state.stale_boot_reconciliation(home=fixture.home, execute=True)
                    with mock.patch.object(parent_state, "_trusted_parent_runtime_script",
                                           return_value=True):
                        state = parent_state.read_status(home=fixture.home)["status"]
                    if boundary == "complete-audit":
                        self.assertEqual(state, "STOPPED")
                    else:
                        self.assertEqual(state, "RECONCILIATION_PENDING")
                finally:
                    fixture.tearDown()

    def test_stale_boot_audit_is_scoped_to_its_run_id(self):
        run, _disk_info, _disk_path = self.write_stale_running_fixture()
        with self.stale_reconcile_patches()[0], self.stale_reconcile_patches()[1], \
                self.stale_reconcile_patches()[2]:
            parent_state.stale_boot_reconciliation(home=self.home, execute=True)
        new_run = {"schema": 1, "parent_id": "default", "run_id": "f" * 32,
                   "phase": "STOPPING", "supervisor": run["supervisor"]}
        fd = parent_state._open_directory_chain(create=False, home_override=self.home)
        try:
            parent = parent_state._read_json(fd, "parent.json")
            with self.stale_reconcile_patches()[0]:
                disk = parent_state._disk_status(fd, parent)
                self.assertIsNone(parent_state._valid_stale_reconciliation(
                    fd, parent, new_run, disk))
        finally:
            os.close(fd)
        current_boot = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        new_supervisor = dict(run["supervisor"], pid=2**31 - 1,
                              host_boot_id=current_boot)
        new_supervisor["argv"] = list(new_supervisor["argv"])
        new_supervisor["argv"][6] = "f" * 32
        new_run["supervisor"] = new_supervisor
        with self.stale_reconcile_patches()[0], self.stale_reconcile_patches()[1]:
            projected = parent_state.status_for_run(
                parent, new_run, disk, home=self.home)
        self.assertEqual(projected["status"], "STOPPED")

    def test_stale_disk_scan_requires_installed_privileged_positive_control(self):
        from types import SimpleNamespace
        import subprocess

        with tempfile.TemporaryFile() as disk:
            info = os.fstat(disk.fileno())
            calls = []
            real_lstat = pathlib.Path.lstat
            install_root = pathlib.Path(parent_state.__file__).resolve().parents[2]
            script_path = install_root / "scripts/parent-readonly-safety-gate.py"

            def protected_lstat(path):
                path = pathlib.Path(path)
                if path == script_path:
                    return SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o644)
                if path == install_root or install_root in path.parents:
                    return SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755)
                return real_lstat(path)

            def run(argv, **_kwargs):
                calls.append(argv)
                if argv[1:2] == ["-v"]:
                    return subprocess.CompletedProcess(argv, 0)
                device, inode, owner_pid, owner_fd = map(int, argv[-4:])
                holders = [[os.getpid(), "fd", owner_fd]] if owner_pid == 0 else []
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps({"complete": True, "processes_scanned": 3,
                                          "coverage": {"procfs": True,
                                                       "pid_namespace": "init-shared",
                                                       "user_namespace": "init-shared",
                                                       "descriptors": True,
                                                       "file_mappings": True},
                                          "holders": holders}).encode(), b"")

            with (mock.patch.object(pathlib.Path, "lstat", protected_lstat),
                  mock.patch.object(parent_state.subprocess, "run", side_effect=run)):
                parent_state._scan_stale_disk_holders(
                    disk.fileno(), (info.st_dev, info.st_ino))
            self.assertEqual(calls[0][1:2], ["-v"])
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[-1][-4:-2], [str(info.st_dev), str(info.st_ino)])

    def test_stale_boot_reconciliation_refuses_malformed_history(self):
        self.write_stale_running_fixture()
        fd = parent_state._open_directory_chain(create=False, home_override=self.home)
        try:
            run = parent_state._read_json(fd, "active-run.json")
            run["supervisor"]["argv"][3] = "--wrong"
            parent_state._atomic_json(fd, "active-run.json", run)
        finally:
            os.close(fd)
        with self.stale_reconcile_patches()[0], self.stale_reconcile_patches()[1], \
                self.stale_reconcile_patches()[2]:
            with self.assertRaises(parent_state.Refused):
                parent_state.stale_boot_reconciliation(home=self.home, execute=True)
        self.assertFalse((self.parent_dir() / parent_state._stale_reconciliation_filename(
            "0123456789abcdef0123456789abcdef")).exists())

    def test_stale_boot_reconciliation_refuses_disk_holder_and_scan_failure(self):
        self.write_stale_running_fixture()
        for failure in (parent_state.Refused("holder present"), OSError("lsof unavailable")):
            with self.subTest(failure=type(failure).__name__), \
                    self.stale_reconcile_patches()[0], self.stale_reconcile_patches()[1], \
                    mock.patch.object(parent_state, "_scan_stale_disk_holders",
                                      side_effect=failure):
                with self.assertRaises((parent_state.Refused, OSError)):
                    parent_state.stale_boot_reconciliation(home=self.home, execute=True)
        self.assertTrue((self.parent_dir() / "disk.qcow2").exists())
        self.assertFalse((self.parent_dir() / parent_state._stale_reconciliation_filename(
            "0123456789abcdef0123456789abcdef")).exists())

    def test_stale_reconciliation_refuses_replaced_cleanup_artifact_before_mutation(self):
        run, _disk_info, _disk_path = self.write_stale_running_fixture()
        artifact = self.parent_dir() / "proxy.port"
        original = artifact.stat()

        def replace_after_scan(_disk_fd, _expected, *, scratch_dir=None):
            with tempfile.NamedTemporaryFile(dir=artifact.parent, delete=False) as replacement:
                replacement.write(b"replacement\n")
                replacement.flush()
                os.fchmod(replacement.fileno(), 0o600)
                replacement_path = pathlib.Path(replacement.name)
            os.replace(replacement_path, artifact)

        with (self.stale_reconcile_patches()[0], self.stale_reconcile_patches()[1],
              mock.patch.object(parent_state, "_scan_stale_disk_holders",
                                side_effect=replace_after_scan)):
            with self.assertRaisesRegex(parent_state.Refused, "evidence changed during holder scan"):
                parent_state.stale_boot_reconciliation(home=self.home, execute=True)
        replacement = artifact.stat()
        self.assertNotEqual((replacement.st_dev, replacement.st_ino),
                            (original.st_dev, original.st_ino))
        self.assertFalse((self.parent_dir() / parent_state._stale_reconciliation_filename(
            run["run_id"])).exists())

    def test_stale_boot_reconciliation_refuses_lifecycle_lock_contention(self):
        self.write_stale_running_fixture()
        with parent_state.lifecycle_lock(home=self.home):
            with self.assertRaisesRegex(parent_state.Refused, "exclusive verified lifecycle"):
                parent_state.stale_boot_reconciliation(home=self.home, execute=True)

    def test_stale_boot_reconciliation_refuses_disk_identity_change_and_current_boot_run(self):
        run, _disk_info, _disk_path = self.write_stale_running_fixture()
        wrong_disk = mock.patch.object(parent_state, "_disk_status", return_value={
            "path": "disk.qcow2", "format": "qcow2", "virtual_capacity_gib": 64,
            "provisioned": True, "device": 1, "inode": 2,
            "host_allocated_bytes": 1, "file_bytes": 1})
        with wrong_disk, self.stale_reconcile_patches()[1], self.stale_reconcile_patches()[2]:
            with self.assertRaises(parent_state.Refused):
                parent_state.stale_boot_reconciliation(home=self.home, execute=True)

        fd = parent_state._open_directory_chain(create=False, home_override=self.home)
        try:
            run["supervisor"]["host_boot_id"] = pathlib.Path(
                "/proc/sys/kernel/random/boot_id").read_text().strip()
            run["qemu"]["host_boot_id"] = run["supervisor"]["host_boot_id"]
            parent_state._atomic_json(fd, "active-run.json", run)
        finally:
            os.close(fd)
        with self.stale_reconcile_patches()[0], self.stale_reconcile_patches()[1], \
                self.stale_reconcile_patches()[2]:
            with self.assertRaisesRegex(parent_state.Refused, "not a consistent prior-boot"):
                parent_state.stale_boot_reconciliation(home=self.home, execute=True)

    def test_read_only_status_does_not_create_state(self):
        self.assertEqual(parent_state.read_status(home=self.home),
                         {"status": "NOT_CREATED", "parent_id": "default"})
        self.assertFalse((self.home / ".local").exists())

    def test_recovery_preview_is_non_mutating_and_does_not_create_state(self):
        preview = parent_state.recovery_preview(home=self.home)
        self.assertEqual(preview["status"], "NOT_CREATED")
        self.assertEqual(preview["action"], "none")
        self.assertTrue(preview["dry_run"])
        default = parent_state.recover_parent(home=self.home)
        self.assertEqual(default, preview)
        self.assertFalse((self.home / ".local").exists())

    def test_recovery_preview_refuses_while_supervisor_owns_lifecycle_lock(self):
        parent_state.initialize(home=self.home)
        with parent_state.lifecycle_lock(home=self.home):
            with self.assertRaisesRegex(parent_state.Refused, "owns the parent lock"):
                parent_state.recovery_preview(home=self.home)

    def test_recovery_execute_refuses_while_supervisor_owns_lifecycle_lock(self):
        parent_state.initialize(home=self.home)
        with parent_state.lifecycle_lock(home=self.home):
            with self.assertRaisesRegex(parent_state.Refused, "exclusive lifecycle lock"):
                parent_state.recover_parent(home=self.home, execute=True)

    def test_recoverable_preview_exit_code_matches_text_and_json(self):
        recoverable = {"dry_run": True, "action": "recover", "status": "RECOVERABLE",
                       "parent_id": "default", "run_id": "0" * 32,
                       "children": {"qemu": "TARGET", "proxy": "TARGET"}}
        with mock.patch.object(parent_state, "recover_parent", return_value=recoverable):
            with mock.patch.object(sys, "argv", [str(MODULE), "recover"]), contextlib.redirect_stdout(io.StringIO()):
                text_status = parent_state.main()
            with mock.patch.object(sys, "argv", [str(MODULE), "recover", "--json"]), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                json_status = parent_state.main()
        self.assertEqual(text_status, 0)
        self.assertEqual(json_status, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "RECOVERABLE")

    def test_recovery_preview_refuses_symlinked_lock_without_following_it(self):
        parent_state.initialize(home=self.home)
        lock = self.parent_dir() / "lifecycle.lock"
        lock.unlink()
        target = pathlib.Path(self.temp.name) / "outside-lock"
        target.write_bytes(b"untouched")
        lock.symlink_to(target)
        with self.assertRaisesRegex(parent_state.Refused, "unsafe or unreadable"):
            parent_state.recovery_preview(home=self.home)
        self.assertEqual(target.read_bytes(), b"untouched")

    def test_recovery_preview_reports_unprovisioned_state_without_mutation(self):
        parent_state.initialize(home=self.home)
        before = sorted(item.name for item in self.parent_dir().iterdir())
        preview = parent_state.recovery_preview(home=self.home)
        self.assertEqual(preview["status"], "UNPROVISIONED")
        self.assertEqual(preview["action"], "none")
        self.assertEqual(sorted(item.name for item in self.parent_dir().iterdir()), before)

    def test_initialize_writes_private_atomic_parent_record_and_is_idempotent(self):
        record = parent_state.initialize(64, home=self.home)
        directory = self.parent_dir()
        self.assertEqual(record["disk"]["format"], "qcow2")
        self.assertEqual(record["disk"]["virtual_capacity_gib"], 64)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((directory / "parent.json").stat().st_mode), 0o600)
        self.assertEqual(list(directory.glob("*.tmp")), [])
        self.assertEqual(parent_state.initialize(64, home=self.home), record)
        with self.assertRaises(parent_state.Refused):
            parent_state.initialize(32, home=self.home)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "UNPROVISIONED")

    def test_status_refuses_unsafe_directory_without_repairing_it(self):
        parent_state.initialize(home=self.home)
        directory = self.parent_dir()
        os.chmod(directory, 0o755)
        with self.assertRaises(parent_state.Refused):
            parent_state.read_status(home=self.home)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)

    def test_symlink_in_parent_state_path_is_refused_without_touching_target(self):
        target = pathlib.Path(self.temp.name) / "target"
        target.mkdir()
        (self.home / ".local").symlink_to(target, target_is_directory=True)
        with self.assertRaises(parent_state.Refused):
            parent_state.initialize(home=self.home)
        self.assertEqual(list(target.iterdir()), [])

    def test_parent_record_symlink_and_malformed_json_are_refused(self):
        parent_state.initialize(home=self.home)
        record_path = self.parent_dir() / "parent.json"
        target = pathlib.Path(self.temp.name) / "outside.json"
        target.write_text("{}\n")
        record_path.unlink()
        record_path.symlink_to(target)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")
        record_path.unlink()
        record_path.write_text("{not-json\n")
        os.chmod(record_path, 0o600)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")

    def test_parent_record_rejects_unknown_schema_fields(self):
        record = parent_state.initialize(home=self.home)
        record["unexpected"] = "not part of schema"
        with parent_state.lifecycle_lock(home=self.home):
            fd = parent_state._open_directory_chain(create=False, home_override=self.home)
            try:
                parent_state._atomic_json(fd, "parent.json", record)
            finally:
                os.close(fd)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")

        with self.assertRaises(parent_state.Refused):
            parent_state.initialize(home=self.home)

    def test_parent_record_rejects_null_seed_identity_extension(self):
        record = parent_state.initialize(home=self.home)
        record["ssh_identity"] = None
        with self.assertRaises(parent_state.Refused):
            parent_state.validate_parent(record)

    def test_fifo_state_files_do_not_block_status(self):
        parent_state.initialize(home=self.home)
        directory = self.parent_dir()
        record_path = directory / "parent.json"
        record_path.unlink()
        os.mkfifo(record_path, 0o600)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")
        record_path.unlink()
        record_path.write_text("{not-json\n")
        os.chmod(record_path, 0o600)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")

    def test_metadata_or_disk_inconsistency_is_ambiguous(self):
        parent_state.initialize(home=self.home)
        directory = self.parent_dir()
        run = {"schema": 1, "parent_id": "default", "run_id": "0123456789abcdef",
               "supervisor": {}}
        with parent_state.lifecycle_lock(home=self.home):
            fd = parent_state._open_directory_chain(create=False, home_override=self.home)
            try:
                parent_state._atomic_json(fd, "active-run.json", run)
            finally:
                os.close(fd)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")
        (directory / "active-run.json").unlink()
        (directory / "disk.qcow2").write_bytes(b"unexpected")
        os.chmod(directory / "disk.qcow2", 0o600)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")

    def test_run_without_parent_metadata_is_ambiguous(self):
        with parent_state.lifecycle_lock(home=self.home):
            fd = parent_state._open_directory_chain(create=False, home_override=self.home)
            try:
                parent_state._atomic_json(fd, "active-run.json", {"schema": 1})
            finally:
                os.close(fd)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")

    def test_lifecycle_lock_is_exclusive_and_not_inherited_by_exec_child(self):
        parent_state.initialize(home=self.home)
        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            self.assertFalse(os.get_inheritable(lock_fd))
            with self.assertRaises(parent_state.Refused):
                with parent_state.lifecycle_lock(home=self.home):
                    self.fail("second lifecycle lock unexpectedly acquired")
            directory = self.parent_dir()
            os.chmod(directory, 0o755)
            with self.assertRaises(parent_state.Refused):
                parent_state.initialize(home=self.home)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)
            os.chmod(directory, 0o700)
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.5)"],
                                     close_fds=False)
        try:
            # The exec'd child is still alive, but must not retain the lock.
            self.assertIsNone(child.poll())
            with parent_state.lifecycle_lock(home=self.home):
                pass
        finally:
            child.wait(timeout=3)

    def test_running_status_requires_exact_process_identity(self):
        parent = parent_state.initialize(home=self.home)
        run = {"run_id": "test-run", "supervisor": {"pid": os.getpid(), "schema": 1,
                "uid": os.getuid(), "host_boot_id": "mismatch", "start_time": "0",
                "exe": sys.executable, "argv": [sys.executable]}}
        self.assertEqual(parent_state.status_for_run(parent, run)["status"], "AMBIGUOUS")

    def test_supervisor_identity_accepts_exact_up_ssh_port_arguments(self):
        self.assertTrue(parent_state._valid_supervisor_options(["--ssh-port", "23456"], None))
        self.assertTrue(parent_state._valid_supervisor_options(
            ["--state-home", str(self.home), "--ssh-port", "23456"], self.home))
        self.assertFalse(parent_state._valid_supervisor_options(
            ["--ssh-port", "23456"], self.home))
        self.assertFalse(parent_state._valid_supervisor_options(
            ["--state-home", str(self.home), "--ssh-port", "0"], self.home))

    def test_parent_auth_socket_path_stays_below_openssh_limit_for_five_digit_uid(self):
        runtime = pathlib.Path("/run/user/10000")
        run_dir, control_dir, socket_path = parent_state.parent_auth_paths(
            runtime, "a-" + "a" * 24)
        self.assertEqual(run_dir.name, "a-" + "a" * 24)
        self.assertEqual(control_dir.name, "lmv3-a-" + "a" * 24)
        self.assertLess(len(os.fsencode(socket_path)), parent_state.AUTH_CONTROL_SOCKET_LIMIT)
        with self.assertRaises(parent_state.Refused):
            parent_state.parent_auth_paths(runtime, "too-long-or-untrusted")

    def test_parent_auth_staging_signal_removes_copied_key_before_bridge_start(self):
        runtime = pathlib.Path(self.temp.name) / "runtime"
        runtime.mkdir(mode=0o700)
        os.chmod(runtime, 0o700)
        original_write = parent_state._private_write
        writes = []

        def write_then_interrupt(path, payload):
            original_write(path, payload)
            writes.append(path.name)
            if len(writes) == 1:
                os.kill(os.getpid(), signal.SIGTERM)

        fake_bridge = mock.Mock()
        fake_spec = mock.Mock()
        fake_spec.loader.exec_module.return_value = None
        with (mock.patch.object(parent_state, "_parent_auth_credentials",
                                return_value=(2222, b"synthetic-private-key", b"synthetic-host-pin\n")),
              mock.patch.object(parent_state, "_private_write", side_effect=write_then_interrupt),
              mock.patch.object(parent_state.importlib.util, "spec_from_file_location", return_value=fake_spec),
              mock.patch.object(parent_state.importlib.util, "module_from_spec", return_value=fake_bridge)):
            result = parent_state.parent_browser_auth(runtime_root=runtime)

        self.assertEqual(result, 128 + signal.SIGTERM)
        self.assertEqual(writes, ["identity_file", "known_hosts"])
        fake_bridge.main.assert_not_called()
        self.assertEqual(list(runtime.iterdir()), [])

    def test_previous_candidate_runtime_script_requires_protected_sha_install_chain(self):
        from types import SimpleNamespace

        install_root = pathlib.Path(self.temp.name) / "protected" / "v3"
        sha_root = install_root / ("a" * 40)
        script = sha_root / "lib/vm/parent-supervisor.py"
        script.parent.mkdir(parents=True)
        script.write_text("# trusted fixture\n")
        os.chmod(script, 0o644)
        real_lstat = os.lstat

        def root_owned_lstat(path, *, override=None):
            candidate = pathlib.Path(path)
            if override and candidate == override.get("missing"):
                raise FileNotFoundError(candidate)
            if override and candidate == override.get("symlink"):
                return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0)
            if override and candidate == override.get("writable"):
                return SimpleNamespace(st_mode=stat.S_IFDIR | 0o775, st_uid=0)
            info = real_lstat(candidate)
            if candidate == script:
                mode = override.get("file_mode", info.st_mode) if override else info.st_mode
                uid = override.get("file_uid", 0) if override else 0
                return SimpleNamespace(st_mode=mode, st_uid=uid)
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)

        def check(override=None, candidate=script, filename="parent-supervisor.py", raw=None):
            with mock.patch.object(parent_state.os, "lstat",
                                   side_effect=lambda path: root_owned_lstat(path, override=override)):
                return parent_state._trusted_parent_runtime_script(
                    str(candidate) if raw is None else raw, filename)

        self.assertTrue(check())
        self.assertFalse(check({"file_mode": stat.S_IFREG | 0o664}))
        self.assertFalse(check({"file_uid": os.getuid() + 1}))
        self.assertFalse(check({"writable": sha_root}))
        self.assertFalse(check({"symlink": sha_root / "lib"}))
        self.assertFalse(check({"missing": script}))
        self.assertFalse(check(candidate=install_root / "not-a-sha" / "lib/vm/parent-supervisor.py"))
        self.assertFalse(check(candidate=script, filename="parent-gate.py"))
        self.assertFalse(check(raw=str(script).replace("/lib/vm/", "/lib//vm/")))
        self.assertFalse(check(raw=str(script).replace("/lib/vm/", "/lib/./vm/")))
        self.assertFalse(check(raw=str(script) + "/"))

    def test_installed_runtime_root_derives_from_its_own_prefix(self):
        for prefix in (pathlib.Path("/opt/little-mester"),
                       pathlib.Path("/usr/local/share/little-mester")):
            source = prefix / "v3" / ("c" * 40) / "lib/vm/parent-state.py"
            self.assertEqual(parent_state._installed_runtime_root(source), prefix / "v3")
        self.assertIsNone(parent_state._installed_runtime_root(
            ROOT / "lib/vm/parent-state.py"))

    def test_previous_sha_is_trusted_from_any_protected_configured_prefix(self):
        from types import SimpleNamespace

        prefix_root = pathlib.Path("/srv/little-mester/v3")
        sha_root = prefix_root / ("d" * 40)
        source = sha_root / "lib/vm/parent-supervisor.py"

        def protected_lstat(path):
            candidate = pathlib.Path(path)
            if candidate.name == "parent-supervisor.py":
                return SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0)
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)

        with mock.patch.object(parent_state.os, "lstat", side_effect=protected_lstat):
            self.assertTrue(parent_state._trusted_parent_runtime_script(
                str(source), "parent-supervisor.py"))
            other_prefix = pathlib.Path("/opt/little-mester/v3") / ("e" * 40) / "lib/vm/parent-supervisor.py"
            self.assertTrue(parent_state._trusted_parent_runtime_script(
                str(other_prefix), "parent-supervisor.py"))

    def test_stopping_tombstone_from_previous_trusted_candidate_projects_stopped(self):
        parent = parent_state.initialize(home=self.home)
        directory = self.parent_dir()
        disk = directory / "disk.qcow2"
        disk.write_bytes(b"persistent-parent-disk")
        os.chmod(disk, 0o600)
        info = disk.stat()
        parent["disk"].update({"provisioned": True, "device": info.st_dev, "inode": info.st_ino})
        run_id = "0123456789abcdef0123456789abcdef"
        old_script = "/opt/little-mester/v3/" + "a" * 40 + "/lib/vm/parent-supervisor.py"
        argv = [os.path.realpath(sys.executable), "-I", old_script, "--parent-id", "default",
                "--run-id", run_id, "--lock-fd", "3", "--handoff-fd", "5",
                "--ssh-port", "55719"]
        identity = {"schema": 1, "pid": 2**31 - 1, "uid": os.getuid(),
                    "host_boot_id": pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                    "start_time": "1", "exe": os.path.realpath(sys.executable), "argv": argv}
        run = {"schema": 1, "parent_id": "default", "run_id": run_id,
               "phase": "STOPPING", "supervisor": identity}
        with mock.patch.object(parent_state, "_trusted_parent_runtime_script", return_value=True):
            result = parent_state.status_for_run(parent, run, parent["disk"])
        self.assertEqual(result["status"], "STOPPED")

    def test_gated_transition_accepts_previous_candidate_only_through_trusted_path_check(self):
        token = "1" * 32
        old_gate = "/opt/little-mester/v3/" + "b" * 40 + "/lib/vm/parent-gate.py"
        argv = [os.path.realpath(sys.executable), "-I", old_gate,
                "--release-fd", "4", "--token", token,
                "--target", "child-proxy.target.json", "--identity", "child-proxy.identity.json",
                "--state-dir-fd", "5", "--disk-fd", "-1"]
        with mock.patch.object(parent_state, "_trusted_parent_runtime_script",
                               side_effect=lambda path, filename: path == old_gate and filename == "parent-gate.py"):
            parent_state._validate_gate_invocation(
                argv, os.path.realpath(sys.executable), token,
                "child-proxy.target.json", "child-proxy.identity.json", [], "invalid fixture")
            argv[2] = "/tmp/untrusted-parent-gate.py"
            with self.assertRaises(parent_state.Refused):
                parent_state._validate_gate_invocation(
                    argv, os.path.realpath(sys.executable), token,
                    "child-proxy.target.json", "child-proxy.identity.json", [], "invalid fixture")

    def test_status_rejects_supervisor_with_malformed_handoff_arguments(self):
        parent = parent_state.initialize(home=self.home)
        directory = self.parent_dir()
        supervisor_path = pathlib.Path(self.temp.name) / "lib/vm/parent-supervisor.py"
        supervisor_path.parent.mkdir(parents=True)
        supervisor_path.write_text("import time\ntime.sleep(5)\n")
        os.chmod(supervisor_path, 0o600)
        disk_path = directory / "disk.qcow2"
        with disk_path.open("wb") as disk_file:
            disk_file.truncate(1024 * 1024)
        os.chmod(disk_path, 0o600)
        disk_info = disk_path.stat()
        parent["disk"].update({"provisioned": True, "device": disk_info.st_dev,
                               "inode": disk_info.st_ino})
        run_id = "0123456789abcdef0123456789abcdef"
        child = subprocess.Popen([sys.executable, "-I", str(supervisor_path),
                                  "--parent-id", "default", "--run-id", run_id])
        try:
            identity = parent_state.observe_process(child.pid)
            identity["argv"] = [sys.executable, "-I", str(ROOT / "lib/vm/parent-supervisor.py"),
                                "--parent-id", "default", "--run-id", run_id,
                                "--lock-fd", "3", "--control-fd", "4"]
            with parent_state.lifecycle_lock(home=self.home) as lock_fd:
                fd = parent_state._open_directory_chain(create=False, home_override=self.home)
                try:
                    parent_state._verify_lock_fd(lock_fd, home=self.home)
                    parent_state._atomic_json(fd, "parent.json", parent)
                    parent_state._atomic_json(fd, "active-run.json", {"schema": 1, "parent_id": "default",
                                               "run_id": run_id, "phase": "STARTING", "supervisor": identity})
                finally:
                    os.close(fd)
            result = parent_state.read_status(home=self.home)
            self.assertEqual(result["status"], "AMBIGUOUS")
        finally:
            child.terminate()
            child.wait(timeout=3)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")
        os.unlink(disk_path)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")


if __name__ == "__main__":
    unittest.main()
