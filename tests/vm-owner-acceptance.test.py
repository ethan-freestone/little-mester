import argparse
import hashlib
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENTRY = ROOT / "tools/vm-owner-acceptance"
loader = importlib.machinery.SourceFileLoader("vm_owner_acceptance", str(ENTRY))
spec = importlib.util.spec_from_loader(loader.name, loader)
acceptance = importlib.util.module_from_spec(spec)
loader.exec_module(acceptance)


class TTYBuffer(io.StringIO):
    def isatty(self):
        return True


class OwnerAcceptanceTests(unittest.TestCase):
    def test_failed_reset_then_provision_report_is_accepted_as_resume_provenance(self):
        with tempfile.TemporaryDirectory(prefix="owner-resume-origin-") as temporary:
            reports = pathlib.Path(temporary)
            os.chmod(reports, 0o700)
            attempt_id = "20261116T222130Z-" + "a" * 32
            attempt = reports / attempt_id
            attempt.mkdir(mode=0o700)
            report = {
                "candidate_sha": "1" * 40,
                "mode": "parent_accept",
                "attempt_id": attempt_id,
                "status": "FAIL",
                "gates": {"retirement": "PASS", "fresh_provision": "FAIL"},
                "evidence": {"retirement": {"status": "FACTORY_RESET"}},
                "notes": ["protected parent operation up failed (exit=2; reason=refused)"],
            }
            path = attempt / "report.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            os.chmod(path, 0o600)
            with mock.patch.object(acceptance, "runtime_dir", return_value=reports):
                found = acceptance._replacement_provision_origin("1" * 40)
            self.assertEqual(found, {"attempt_id": attempt_id,
                                     "factory_reset_attempt_id": attempt_id,
                                     "disk_identity": None,
                                     "reconciled_attempt_ids": []})

    def test_interrupted_approved_first_start_is_resumable_without_failure_exit_record(self):
        with tempfile.TemporaryDirectory(prefix="owner-interrupted-origin-") as temporary:
            reports = pathlib.Path(temporary)
            os.chmod(reports, 0o700)
            attempt_id = "20261116T224000Z-" + "f" * 32
            attempt = reports / attempt_id
            attempt.mkdir(mode=0o700)
            content = {
                "candidate_sha": "5" * 40, "mode": "parent_accept",
                "attempt_id": attempt_id, "status": "BLOCKED",
                "gates": {"retirement": "PASS", "fresh_provision": "BLOCKED"},
                "evidence": {
                    "retirement": {"status": "FACTORY_RESET"},
                    "fresh_provision_approval": {
                        "owner_approved": True, "candidate_sha": "5" * 40,
                        "capacity_gib": 64, "from_status": "NOT_CREATED",
                    },
                },
                "notes": ["interrupted before acceptance completed"],
            }
            path = attempt / "report.json"
            path.write_text(json.dumps(content), encoding="utf-8")
            os.chmod(path, 0o600)
            with mock.patch.object(acceptance, "runtime_dir", return_value=reports):
                found = acceptance._replacement_provision_origin("5" * 40)
            self.assertEqual(found, {"attempt_id": attempt_id,
                                     "factory_reset_attempt_id": attempt_id,
                                     "disk_identity": None,
                                     "reconciled_attempt_ids": []})

    def test_reset_checkpoint_before_provision_approval_remains_a_valid_origin(self):
        with tempfile.TemporaryDirectory(prefix="owner-reset-interruption-") as temporary:
            reports = pathlib.Path(temporary)
            os.chmod(reports, 0o700)
            attempt_id = "20261116T225000Z-" + "1" * 32
            attempt = reports / attempt_id
            attempt.mkdir(mode=0o700)
            content = {
                "candidate_sha": "9" * 40, "mode": "parent_accept",
                "attempt_id": attempt_id, "status": "BLOCKED",
                "gates": {"retirement": "PASS", "fresh_provision": "BLOCKED"},
                "evidence": {"retirement": {"status": "FACTORY_RESET"}},
                "notes": [],
            }
            path = attempt / "report.json"
            path.write_text(json.dumps(content), encoding="utf-8")
            os.chmod(path, 0o600)
            with mock.patch.object(acceptance, "runtime_dir", return_value=reports):
                found = acceptance._replacement_provision_origin("9" * 40)
            self.assertEqual(found, {
                "attempt_id": attempt_id,
                "factory_reset_attempt_id": attempt_id,
                "disk_identity": None,
                "reconciled_attempt_ids": [],
            })

    def test_verified_reset_reconciles_older_conflicting_private_attempt_without_deleting_it(self):
        with tempfile.TemporaryDirectory(prefix="owner-reset-reconcile-") as temporary:
            reports = pathlib.Path(temporary)
            os.chmod(reports, 0o700)
            old_id = "20261117T080000Z-" + "a" * 32
            reset_id = "20261117T084000Z-" + "b" * 32
            old_dir = reports / old_id
            reset_dir = reports / reset_id
            old_dir.mkdir(mode=0o700)
            reset_dir.mkdir(mode=0o700)
            old_path = old_dir / "report.json"
            old_path.write_text(json.dumps({
                "candidate_sha": "1" * 40, "mode": "parent_accept",
                "attempt_id": old_id, "status": "BLOCKED",
                "gates": {"retirement": "PASS", "fresh_provision": "BLOCKED"},
                "evidence": {"provisioning_origin": {"corrupt_old_link": True}},
            }), encoding="utf-8")
            os.chmod(old_path, 0o600)
            reset_path = reset_dir / "report.json"
            reset_path.write_text(json.dumps({
                "candidate_sha": "2" * 40, "mode": "parent_accept",
                "attempt_id": reset_id, "status": "BLOCKED",
                "gates": {"retirement": "PASS", "fresh_provision": "BLOCKED"},
                "evidence": {"retirement": {"status": "FACTORY_RESET"}},
            }), encoding="utf-8")
            os.chmod(reset_path, 0o600)
            with (mock.patch.object(acceptance, "runtime_dir", return_value=reports),
                  mock.patch.object(acceptance, "git",
                                    return_value=subprocess.CompletedProcess([], 0, b"", b""))):
                found = acceptance._replacement_provision_origin("3" * 40, pathlib.Path("/repo"))
            self.assertEqual(found, {
                "attempt_id": reset_id,
                "factory_reset_attempt_id": reset_id,
                "disk_identity": None,
                "reconciled_attempt_ids": [old_id],
            })
            self.assertTrue(old_path.is_file(), "reconciliation must preserve older report files")

    def test_newer_unlinked_fresh_approval_cannot_be_ignored_after_verified_reset(self):
        with tempfile.TemporaryDirectory(prefix="owner-newer-unlinked-evidence-") as temporary:
            reports = pathlib.Path(temporary)
            os.chmod(reports, 0o700)
            reset_id = "20261117T084000Z-" + "c" * 32
            newer_id = "20261117T084100Z-" + "d" * 32
            for attempt_id, content in (
                (reset_id, {
                    "candidate_sha": "2" * 40, "mode": "parent_accept",
                    "attempt_id": reset_id, "status": "BLOCKED",
                    "gates": {"retirement": "PASS", "fresh_provision": "BLOCKED"},
                    "evidence": {"retirement": {"status": "FACTORY_RESET"}},
                }),
                (newer_id, {
                    "candidate_sha": "3" * 40, "mode": "parent_accept",
                    "attempt_id": newer_id, "status": "BLOCKED",
                    "gates": {"retirement": "PASS", "fresh_provision": "BLOCKED"},
                    "evidence": {"fresh_provision_approval": {
                        "owner_approved": True, "candidate_sha": "3" * 40,
                        "capacity_gib": 64, "factory_reset_attempt_id": reset_id,
                    }},
                }),
            ):
                directory = reports / attempt_id
                directory.mkdir(mode=0o700)
                path = directory / "report.json"
                path.write_text(json.dumps(content), encoding="utf-8")
                os.chmod(path, 0o600)
            with (mock.patch.object(acceptance, "runtime_dir", return_value=reports),
                  mock.patch.object(acceptance, "git",
                                    return_value=subprocess.CompletedProcess([], 0, b"", b""))):
                with self.assertRaisesRegex(acceptance.GateError, "inconsistent replacement identity") as caught:
                    acceptance._replacement_provision_origin("3" * 40, pathlib.Path("/repo"))
            self.assertEqual(caught.exception.category, "provenance_invalid_descendant")

    def test_sequential_not_created_provision_then_running_resume_uses_verified_reset_root(self):
        with tempfile.TemporaryDirectory(prefix="owner-sequential-reset-") as temporary:
            reports = pathlib.Path(temporary) / "reports"
            reports.mkdir(mode=0o700)
            old_id = "20261117T080000Z-" + "1" * 32
            reset_id = "20261117T084000Z-" + "2" * 32
            first_id = "20261117T084100Z-" + "3" * 32
            second_id = "20261117T084200Z-" + "4" * 32
            third_id = "20261117T084300Z-" + "5" * 32
            old_dir, reset_dir = reports / old_id, reports / reset_id
            old_dir.mkdir(mode=0o700)
            reset_dir.mkdir(mode=0o700)
            old_path = old_dir / "report.json"
            old_path.write_text(json.dumps({
                "candidate_sha": "1" * 40, "mode": "parent_accept",
                "attempt_id": old_id, "status": "BLOCKED",
                "gates": {"retirement": "PASS", "fresh_provision": "BLOCKED"},
                "evidence": {
                    "parent_identity": {"status": "STOPPED",
                                        "disk_identity": {"device": 55, "inode": 11441015}},
                    "provisioning_origin": {"bad_prior_link": True},
                },
            }), encoding="utf-8")
            os.chmod(old_path, 0o600)
            reset_path = reset_dir / "report.json"
            reset_path.write_text(json.dumps({
                "candidate_sha": "2" * 40, "mode": "parent_accept",
                "attempt_id": reset_id, "status": "BLOCKED",
                "gates": {"retirement": "PASS", "fresh_provision": "BLOCKED"},
                "evidence": {"retirement": {"status": "FACTORY_RESET"}},
            }), encoding="utf-8")
            os.chmod(reset_path, 0o600)

            disk = {"path": "disk.qcow2", "device": 55, "inode": 11441015,
                    "format": "qcow2", "provisioned": True,
                    "virtual_capacity_gib": 64, "file_bytes": 2048,
                    "host_allocated_bytes": 4096}
            running = {"status": "RUNNING", "parent_id": "default", "run_id": "b" * 32,
                       "supervisor_pid": 101, "qemu_pid": 202, "disk": disk}
            stopped = {"status": "STOPPED", "parent_id": "default", "run_id": "a" * 32,
                       "disk": disk}

            def run_attempt(attempt_id, initial_status, *, expect_up):
                report_dir = reports / attempt_id
                report_dir.mkdir(mode=0o700)
                data = acceptance.report_template("3" * 40, "parent_accept", report_dir)
                operations = []

                def parent_command(_root, *args, **_kwargs):
                    operations.append(args)
                    if args == ("status", "--json"):
                        status = (running if initial_status == "RUNNING" else
                                  stopped if initial_status == "STOPPED" else
                                  {"status": "NOT_CREATED", "parent_id": "default"})
                        return subprocess.CompletedProcess([], 0, json.dumps(status).encode(), b"")
                    if args and args[0] == "up":
                        return subprocess.CompletedProcess([], 0, b"RUNNING", b"")
                    self.fail(f"unexpected parent command: {args}")

                def parent_json(_root, *args, **_kwargs):
                    if args[0] == "term-check":
                        return None, {"status": "PASS", "terminfo_entry": True,
                                      "clear_capability": True, "clear_command": True}
                    if args == ("status", "--json"):
                        states = ([stopped, running, running] if initial_status == "STOPPED"
                                  else [running, running, running])
                        if not hasattr(parent_json, "statuses"):
                            parent_json.statuses = iter(states)
                        return None, next(parent_json.statuses)
                    self.fail(f"unexpected parent JSON operation: {args}")

                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.object(acceptance.sys, "stdin", TTYBuffer()))
                    stack.enter_context(mock.patch.object(acceptance.sys, "stdout", TTYBuffer()))
                    stack.enter_context(mock.patch.dict(os.environ, {
                        "LM_VM_TERM_VALIDATED": "1", "TERM": "xterm-256color"}))
                    stack.enter_context(mock.patch.object(acceptance, "runtime_dir",
                                                          return_value=reports))
                    stack.enter_context(mock.patch.object(acceptance, "git",
                        return_value=subprocess.CompletedProcess([], 0, b"", b"")))
                    stack.enter_context(mock.patch.object(acceptance, "_parent_command",
                                                          side_effect=parent_command))
                    stack.enter_context(mock.patch.object(acceptance, "_parent_json",
                                                          side_effect=parent_json))
                    approval = stack.enter_context(mock.patch.object(acceptance, "_owner_approval"))
                    stack.enter_context(mock.patch.object(acceptance, "inspect_live_parent",
                        return_value={"m2_contract_compatible": False}))
                    with self.assertRaisesRegex(acceptance.GateError, "guest protected asset"):
                        acceptance.run_parent_acceptance(pathlib.Path("/protected/install"),
                            "openai/gpt-test", data, report_dir, pathlib.Path("/repo"))
                self.assertNotIn("retire", [op[0] for op in operations])
                self.assertNotIn("factory-reset", [op[0] for op in operations])
                if expect_up:
                    self.assertEqual([op[0] for op in operations], ["status", "up", "up"])
                    approval.assert_called_once()
                else:
                    self.assertEqual([op[0] for op in operations], ["status", "up"])
                    approval.assert_not_called()
                if initial_status == "STOPPED":
                    self.assertIn("--expected-disk-device", operations[1])
                    self.assertIn("55", operations[1])
                    self.assertIn("--expected-disk-inode", operations[1])
                    self.assertIn("11441015", operations[1])
                return data

            first = run_attempt(first_id, "NOT_CREATED", expect_up=True)
            self.assertEqual(first["evidence"]["provenance_reconciliation"], {
                "factory_reset_attempt_id": reset_id,
                "superseded_attempt_ids": [old_id],
                "prior_reports_preserved": True,
                "basis": "owner-approved reset completed and protected status is NOT_CREATED",
            })
            self.assertEqual(first["evidence"]["provisioning_origin"]["disk_identity"],
                             {"device": 55, "inode": 11441015})
            self.assertTrue(old_path.is_file())

            second = run_attempt(second_id, "STOPPED", expect_up=True)
            self.assertEqual(second["evidence"]["provisioning_origin"]["disk_identity"],
                             {"device": 55, "inode": 11441015})

            third = run_attempt(third_id, "RUNNING", expect_up=False)
            self.assertEqual(third["evidence"]["fresh_provision"]["status"], "RUNNING")
            self.assertEqual(third["evidence"]["provisioning_origin"]["disk_identity"],
                             {"device": 55, "inode": 11441015})

    def test_not_created_with_unverifiable_replacement_evidence_blocks_without_lifecycle_action(self):
        with tempfile.TemporaryDirectory(prefix="owner-orphaned-reset-") as temporary:
            reports = pathlib.Path(temporary) / "reports"
            reports.mkdir(mode=0o700)
            orphan_id = "20261117T070000Z-" + "6" * 32
            orphan_dir = reports / orphan_id
            orphan_dir.mkdir(mode=0o700)
            orphan_report = orphan_dir / "report.json"
            orphan_report.write_text(json.dumps({
                "candidate_sha": "1" * 40, "mode": "parent_accept",
                "attempt_id": orphan_id, "status": "BLOCKED",
                "gates": {"retirement": "PASS", "fresh_provision": "BLOCKED"},
                "evidence": {"provisioning_origin": {"unknown": True}},
            }), encoding="utf-8")
            os.chmod(orphan_report, 0o600)
            report_dir = reports / ("20261117T084900Z-" + "7" * 32)
            report_dir.mkdir(mode=0o700)
            data = acceptance.report_template("3" * 40, "parent_accept", report_dir)
            operations = []
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(acceptance.sys, "stdin", TTYBuffer()))
                stack.enter_context(mock.patch.object(acceptance.sys, "stdout", TTYBuffer()))
                stack.enter_context(mock.patch.dict(os.environ, {
                    "LM_VM_TERM_VALIDATED": "1", "TERM": "xterm-256color"}))
                stack.enter_context(mock.patch.object(acceptance, "runtime_dir",
                                                      return_value=reports))
                stack.enter_context(mock.patch.object(acceptance, "git",
                    return_value=subprocess.CompletedProcess([], 1, b"", b"")))
                stack.enter_context(mock.patch.object(acceptance, "_parent_command",
                    side_effect=lambda _root, *args, **_kwargs: (
                        operations.append(args) or subprocess.CompletedProcess(
                            [], 0, b'{"parent_id":"default","status":"NOT_CREATED"}', b""))))
                owner_approval = stack.enter_context(mock.patch.object(acceptance, "_owner_approval"))
                parent_ok = stack.enter_context(mock.patch.object(acceptance, "_parent_ok"))
                with self.assertRaisesRegex(acceptance.GateError, "provenance chain") as caught:
                    acceptance.run_parent_acceptance(pathlib.Path("/protected/install"),
                        "openai/gpt-test", data, report_dir, pathlib.Path("/repo"))
            self.assertEqual(caught.exception.category, "provenance_orphaned_evidence")
            self.assertEqual(operations, [("status", "--json")])
            owner_approval.assert_not_called()
            parent_ok.assert_not_called()

    def test_provenance_block_writes_fixed_failure_category_to_report(self):
        with tempfile.TemporaryDirectory(prefix="owner-provenance-category-") as temporary:
            reports = pathlib.Path(temporary) / "reports"
            reports.mkdir(mode=0o700)
            orphan_id = "20261117T070100Z-" + "9" * 32
            orphan_dir = reports / orphan_id
            orphan_dir.mkdir(mode=0o700)
            orphan = orphan_dir / "report.json"
            orphan.write_text(json.dumps({
                "candidate_sha": "a" * 40, "mode": "parent_accept",
                "attempt_id": orphan_id, "status": "BLOCKED",
                "gates": {"retirement": "PASS", "fresh_provision": "BLOCKED"},
                "evidence": {
                    "parent_identity": {"status": "STOPPED",
                                        "disk_identity": {"device": 55, "inode": 11441015}},
                    "provisioning_origin": {"bad_prior_link": True},
                },
            }), encoding="utf-8")
            os.chmod(orphan, 0o600)
            report_dir = reports / ("20261117T085100Z-" + "a" * 32)
            report_dir.mkdir(mode=0o700)
            output = TTYBuffer()
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(acceptance.sys, "stdin", TTYBuffer()))
                stack.enter_context(mock.patch.object(acceptance.sys, "stdout", output))
                stack.enter_context(mock.patch.dict(os.environ, {
                    "LM_VM_TERM_VALIDATED": "1", "TERM": "xterm-256color"}))
                stack.enter_context(mock.patch.object(acceptance, "runtime_dir",
                                                      return_value=reports))
                stack.enter_context(mock.patch.object(acceptance, "new_report_dir",
                                                      return_value=report_dir))
                stack.enter_context(mock.patch.object(acceptance, "candidate_commit"))
                stack.enter_context(mock.patch.object(acceptance, "verify_install",
                    return_value=pathlib.Path("/protected/install")))
                stack.enter_context(mock.patch.object(acceptance, "installed_preflight"))
                parent_command = stack.enter_context(mock.patch.object(acceptance, "_parent_command",
                    return_value=subprocess.CompletedProcess([], 0,
                        b'{"parent_id":"default","status":"NOT_CREATED"}', b"")))
                parent_ok = stack.enter_context(mock.patch.object(acceptance, "_parent_ok"))
                with contextlib.redirect_stderr(io.StringIO()):
                    result = acceptance.main([
                        "--parent-accept", "--milestone", "M2", "--sha", "a" * 40,
                        "--repo", str(ROOT), "--model", "openai/gpt-test",
                    ])
            self.assertEqual(result, 3)
            parent_command.assert_called_once_with(pathlib.Path("/protected/install"),
                                                   "status", "--json")
            parent_ok.assert_not_called()
            report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["evidence"]["failure_category"],
                             "provenance_invalid_descendant")
            self.assertNotIn("bad_prior_link", json.dumps(report))

    def test_ambiguous_reset_then_not_created_provision_is_explicit_and_links_reset(self):
        with tempfile.TemporaryDirectory(prefix="owner-reset-fresh-transition-") as temporary:
            reports = pathlib.Path(temporary)
            report_dir = reports / ("20261117T085000Z-" + "8" * 32)
            report_dir.mkdir(mode=0o700)
            data = acceptance.report_template("a" * 40, "parent_accept", report_dir)
            initial = {"status": "AMBIGUOUS", "parent_id": "default"}
            after_reset = {"status": "NOT_CREATED", "parent_id": "default"}
            running = {"status": "RUNNING", "parent_id": "default", "run_id": "a" * 32,
                       "supervisor_pid": 101, "qemu_pid": 202,
                       "disk": {"path": "disk.qcow2", "device": 55, "inode": 11441015,
                                "format": "qcow2", "provisioned": True,
                                "virtual_capacity_gib": 64, "file_bytes": 2048,
                                "host_allocated_bytes": 4096}}
            statuses = iter((after_reset, running, running))
            commands = []

            def parent_command(_root, *args, **_kwargs):
                commands.append(args)
                if args == ("status", "--json"):
                    return subprocess.CompletedProcess([], 0, json.dumps(initial).encode(), b"")
                if args[0] == "up":
                    return subprocess.CompletedProcess([], 0, b"RUNNING", b"")
                self.fail(f"unexpected command: {args}")

            def parent_json(_root, *args, **_kwargs):
                if args == ("status", "--json"):
                    return None, next(statuses)
                self.fail(f"unexpected JSON command: {args}")

            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(acceptance.sys, "stdin", TTYBuffer()))
                stack.enter_context(mock.patch.object(acceptance.sys, "stdout", TTYBuffer()))
                stack.enter_context(mock.patch.dict(os.environ, {
                    "LM_VM_TERM_VALIDATED": "1", "TERM": "xterm-256color"}))
                stack.enter_context(mock.patch.object(acceptance, "runtime_dir",
                                                      return_value=reports))
                stack.enter_context(mock.patch.object(acceptance, "_parent_command",
                                                      side_effect=parent_command))
                stack.enter_context(mock.patch.object(acceptance, "_parent_json",
                                                      side_effect=parent_json))
                owner_approval = stack.enter_context(mock.patch.object(acceptance, "_owner_approval"))
                parent_ok = stack.enter_context(mock.patch.object(acceptance, "_parent_ok"))
                stack.enter_context(mock.patch.object(acceptance, "inspect_live_parent",
                    return_value={"m2_contract_compatible": False}))
                with self.assertRaisesRegex(acceptance.GateError, "guest protected asset"):
                    acceptance.run_parent_acceptance(pathlib.Path("/protected/install"),
                        "openai/gpt-test", data, report_dir)
            parent_ok.assert_any_call(pathlib.Path("/protected/install"), "factory-reset",
                                     interactive=True, timeout=900)
            owner_approval.assert_called_once()
            self.assertEqual([args[0] for args in commands], ["status", "up"])
            self.assertEqual(data["evidence"]["retirement"]["status"], "FACTORY_RESET")
            self.assertEqual(data["evidence"]["provisioning_origin"]["prior_attempt_id"],
                             data["attempt_id"])
            self.assertEqual(data["evidence"]["provisioning_origin"]["factory_reset_attempt_id"],
                             data["attempt_id"])
            self.assertEqual(data["evidence"]["provisioning_origin"]["disk_identity"],
                             {"device": 55, "inode": 11441015})

    def test_reset_interruption_then_failed_provision_links_disk_to_reset_root(self):
        with tempfile.TemporaryDirectory(prefix="owner-reset-chain-") as temporary:
            reports = pathlib.Path(temporary)
            os.chmod(reports, 0o700)
            reset_id = "20261116T225100Z-" + "2" * 32
            attempt_id = "20261116T225200Z-" + "3" * 32
            reset_dir = reports / reset_id
            attempt_dir = reports / attempt_id
            reset_dir.mkdir(mode=0o700)
            attempt_dir.mkdir(mode=0o700)
            root = {
                "candidate_sha": "2" * 40, "mode": "parent_accept",
                "attempt_id": reset_id, "status": "IN_PROGRESS",
                "gates": {"retirement": "PASS"},
                "evidence": {"retirement": {"status": "FACTORY_RESET"}},
                "notes": [],
            }
            retry = {
                "candidate_sha": "3" * 40, "mode": "parent_accept",
                "attempt_id": attempt_id, "status": "FAIL",
                "gates": {"retirement": "PASS", "fresh_provision": "FAIL"},
                "evidence": {
                    "retirement": {"status": "NO_PRIOR_PARENT"},
                    "fresh_provision_approval": {
                        "owner_approved": True, "candidate_sha": "3" * 40,
                        "capacity_gib": 64, "from_status": "NOT_CREATED",
                        "factory_reset_attempt_id": reset_id,
                    },
                    "fresh_provision_failure": {
                        "operation": "parent up", "exit_status": 2,
                        "post_failure_disk_identity": {"device": 55, "inode": 11441015},
                    },
                    "provisioning_origin": {
                        "prior_attempt_id": reset_id,
                        "factory_reset_attempt_id": reset_id,
                        "candidate_sha": "3" * 40,
                        "disk_identity": {"device": 55, "inode": 11441015},
                        "owner_approved_fresh_provision": True,
                        "owner_confirmed_same_disk_resume": False,
                    },
                },
                "notes": [],
            }
            for directory, content in ((reset_dir, root), (attempt_dir, retry)):
                path = directory / "report.json"
                path.write_text(json.dumps(content), encoding="utf-8")
                os.chmod(path, 0o600)
            with (mock.patch.object(acceptance, "runtime_dir", return_value=reports),
                  mock.patch.object(acceptance, "git",
                                    return_value=subprocess.CompletedProcess([], 0, b"", b""))):
                found = acceptance._replacement_provision_origin("3" * 40, pathlib.Path("/repo"))
            self.assertEqual(found, {
                "attempt_id": attempt_id,
                "factory_reset_attempt_id": reset_id,
                "disk_identity": {"device": 55, "inode": 11441015},
                "reconciled_attempt_ids": [],
            })

    def test_successful_post_reset_provision_preserves_prior_link_for_running_resume(self):
        with tempfile.TemporaryDirectory(prefix="owner-reset-success-chain-") as temporary:
            reports = pathlib.Path(temporary)
            os.chmod(reports, 0o700)
            reset_id = "20261116T225300Z-" + "4" * 32
            pre_disk_id = "20261116T225400Z-" + "5" * 32
            success_id = "20261116T225500Z-" + "6" * 32
            contents = (
                (reset_id, {
                    "candidate_sha": "4" * 40, "mode": "parent_accept",
                    "attempt_id": reset_id, "status": "IN_PROGRESS",
                    "gates": {"retirement": "PASS"},
                    "evidence": {"retirement": {"status": "FACTORY_RESET"}},
                }),
                (pre_disk_id, {
                    "candidate_sha": "5" * 40, "mode": "parent_accept",
                    "attempt_id": pre_disk_id, "status": "IN_PROGRESS",
                    "gates": {"retirement": "PASS", "fresh_provision": "NOT_RUN"},
                    "evidence": {
                        "fresh_provision_approval": {
                            "owner_approved": True, "candidate_sha": "5" * 40,
                            "capacity_gib": 64, "factory_reset_attempt_id": reset_id,
                        },
                        "provisioning_origin": {
                            "prior_attempt_id": reset_id,
                            "factory_reset_attempt_id": reset_id,
                            "candidate_sha": "5" * 40, "disk_identity": None,
                            "owner_approved_fresh_provision": True,
                        },
                    },
                }),
                (success_id, {
                    "candidate_sha": "6" * 40, "mode": "parent_accept",
                    "attempt_id": success_id, "status": "PASS",
                    "gates": {"retirement": "PASS", "fresh_provision": "PASS"},
                    "evidence": {
                        "fresh_provision_approval": {
                            "owner_approved": True, "candidate_sha": "6" * 40,
                            "capacity_gib": 64, "factory_reset_attempt_id": reset_id,
                        },
                        "provisioning_origin": {
                            "prior_attempt_id": pre_disk_id,
                            "factory_reset_attempt_id": reset_id,
                            "candidate_sha": "6" * 40,
                            "disk_identity": {"device": 55, "inode": 11441015},
                            "owner_approved_fresh_provision": True,
                        },
                    },
                }),
            )
            for attempt_id, content in contents:
                directory = reports / attempt_id
                directory.mkdir(mode=0o700)
                path = directory / "report.json"
                path.write_text(json.dumps(content), encoding="utf-8")
                os.chmod(path, 0o600)
            with (mock.patch.object(acceptance, "runtime_dir", return_value=reports),
                  mock.patch.object(acceptance, "git",
                                    return_value=subprocess.CompletedProcess([], 0, b"", b""))):
                found = acceptance._replacement_provision_origin("6" * 40, pathlib.Path("/repo"))
            self.assertEqual(found, {
                "attempt_id": success_id,
                "factory_reset_attempt_id": reset_id,
                "disk_identity": {"device": 55, "inode": 11441015},
                "reconciled_attempt_ids": [],
            })

    def test_continued_resume_provenance_binds_exact_disk_and_supersedes_legacy_attempt(self):
        with tempfile.TemporaryDirectory(prefix="owner-resume-chain-") as temporary:
            reports = pathlib.Path(temporary)
            os.chmod(reports, 0o700)
            first_id = "20261116T222130Z-" + "a" * 32
            second_id = "20261116T223000Z-" + "b" * 32
            old = {
                "candidate_sha": "2" * 40, "mode": "parent_accept", "attempt_id": first_id,
                "status": "FAIL",
                "gates": {"retirement": "PASS", "fresh_provision": "FAIL"},
                "evidence": {"retirement": {"status": "FACTORY_RESET"}},
                "notes": ["protected parent operation up failed (exit=2; reason=refused)"],
            }
            retry = {
                "candidate_sha": "2" * 40, "mode": "parent_accept", "attempt_id": second_id,
                "status": "BLOCKED",
                "gates": {"retirement": "PASS", "fresh_provision": "BLOCKED"},
                "evidence": {
                    "provisioning_origin": {
                        "prior_attempt_id": first_id, "candidate_sha": "2" * 40,
                        "factory_reset_attempt_id": first_id,
                        "disk_identity": {"device": 55, "inode": 11441015},
                        "owner_confirmed_same_disk_resume": True,
                    },
                },
                "notes": [],
            }
            for name, content in ((first_id, old), (second_id, retry)):
                attempt = reports / name
                attempt.mkdir(mode=0o700)
                path = attempt / "report.json"
                path.write_text(json.dumps(content), encoding="utf-8")
                os.chmod(path, 0o600)
            with (mock.patch.object(acceptance, "runtime_dir", return_value=reports),
                  mock.patch.object(acceptance, "git",
                                    return_value=subprocess.CompletedProcess([], 0, b"", b""))):
                found = acceptance._replacement_provision_origin("3" * 40, pathlib.Path("/repo"))
            self.assertEqual(found, {
                "attempt_id": second_id,
                "factory_reset_attempt_id": first_id,
                "disk_identity": {"device": 55, "inode": 11441015},
                "reconciled_attempt_ids": [],
            })
            corrupt_id = "20261116T224000Z-" + "c" * 32
            corrupt_dir = reports / corrupt_id
            corrupt_dir.mkdir(mode=0o700)
            corrupt_path = corrupt_dir / "report.json"
            corrupt_path.write_text(json.dumps({
                "candidate_sha": "2" * 40, "mode": "parent_accept",
                "attempt_id": corrupt_id, "status": "BLOCKED",
                "gates": {"retirement": "PASS", "fresh_provision": "BLOCKED"},
                "evidence": {"provisioning_origin": {
                    "prior_attempt_id": second_id, "factory_reset_attempt_id": first_id,
                    "candidate_sha": "2" * 40,
                    "disk_identity": {"device": 55, "inode": 999},
                    "owner_confirmed_same_disk_resume": True,
                }},
            }), encoding="utf-8")
            os.chmod(corrupt_path, 0o600)
            with (mock.patch.object(acceptance, "runtime_dir", return_value=reports),
                  mock.patch.object(acceptance, "git",
                                    return_value=subprocess.CompletedProcess([], 0, b"", b""))):
                with self.assertRaisesRegex(acceptance.GateError, "inconsistent replacement identity"):
                    acceptance._replacement_provision_origin("3" * 40, pathlib.Path("/repo"))

    def test_parent_up_failure_categories_are_allowlisted_and_do_not_copy_stderr(self):
        cases = (
            (b"REFUSED: QEMU_PROCESS_EXITED_BEFORE_READINESS exit=1", "qemu_boot",
             "qemu_exited_before_readiness"),
            (b"GUEST_READINESS_DEADLINE_EXPIRED last=CLOUD_INIT_STATUS_FAILED attempts=4",
             "guest_readiness", "cloud_init_status_failed"),
            (b"GUEST_READINESS_DEADLINE_EXPIRED last=SSH_HOST_KEY_REJECTED",
             "ssh_readiness", "ssh_host_key_rejected"),
            (b"REFUSED: parent provisioning refused: secret-token-value",
             "provisioning", "provisioning_invariant_refused"),
            (b"REFUSED: startup_failure_stage=supervisor_exit; supervisor_exit_status=2; "
             b"orphan_recovery=refused; recovery_refusal_category=disk_holder_verification",
             "supervisor_exit", "orphan_recovery_holder_verification_refused"),
            (b"REFUSED: startup_failure_stage=supervisor_exit; supervisor_exit_status=2; "
             b"orphan_recovery=STOPPED; disk retained",
             "supervisor_exit", "supervisor_exited_orphan_recovered"),
        )
        for stderr, stage, reason in cases:
            with self.subTest(stage=stage, reason=reason):
                failure = acceptance._parent_up_failure(stderr, 2)
                self.assertEqual(failure["stage"], stage)
                self.assertEqual(failure["reason_category"], reason)
                self.assertEqual(failure["exit_status"], 2)
                self.assertNotIn("secret-token-value", json.dumps(failure))

    def test_m1_clean_preview_gate_rejects_ambiguous_none_action(self):
        acceptance._require_clean_stopped_previews(
            {"action": "none", "status": "STOPPED"},
            {"action": "none", "status": "STOPPED"},
        )
        for recovery, reconciliation in (
            ({"action": "none", "status": "AMBIGUOUS"}, {"action": "none", "status": "STOPPED"}),
            ({"action": "none", "status": "STOPPED"}, {"action": "none", "status": "AMBIGUOUS"}),
        ):
            with self.subTest(recovery=recovery, reconciliation=reconciliation):
                with self.assertRaisesRegex(acceptance.GateError, "ambiguous status"):
                    acceptance._require_clean_stopped_previews(recovery, reconciliation)

    def test_stopped_replacement_resumes_only_same_disk_with_expected_identity_guard(self):
        with tempfile.TemporaryDirectory(prefix="owner-stopped-resume-") as temporary:
            reports = pathlib.Path(temporary) / "reports"
            reports.mkdir(mode=0o700)
            report_dir = reports / ("20261116T230000Z-" + "e" * 32)
            report_dir.mkdir(mode=0o700)
            prior_id = "20261116T222130Z-" + "c" * 32
            prior_dir = reports / prior_id
            prior_dir.mkdir(mode=0o700)
            prior_path = prior_dir / "report.json"
            prior_path.write_text(json.dumps({
                "candidate_sha": "3" * 40, "mode": "parent_accept",
                "attempt_id": prior_id, "status": "FAIL",
                "gates": {"retirement": "PASS", "fresh_provision": "FAIL"},
                "evidence": {
                    "retirement": {"status": "FACTORY_RESET"},
                    "fresh_provision_failure": {
                        "operation": "parent up", "exit_status": 2,
                        "post_failure_disk_identity": {"device": 55, "inode": 11441015},
                    },
                },
                "notes": ["protected parent operation up failed (exit=2; reason=refused)"],
            }), encoding="utf-8")
            os.chmod(prior_path, 0o600)
            stopped = {
                "status": "STOPPED", "parent_id": "default", "run_id": "a" * 32,
                "disk": {"path": "disk.qcow2", "device": 55, "inode": 11441015, "format": "qcow2",
                         "provisioned": True, "virtual_capacity_gib": 64,
                         "file_bytes": 2048, "host_allocated_bytes": 4096},
            }
            running = {
                **stopped, "status": "RUNNING", "run_id": "b" * 32,
                "supervisor_pid": 101, "qemu_pid": 202,
            }
            statuses = iter((stopped, running, running))
            operations = []

            def parent_command(_root, *args, **_kwargs):
                operations.append(args)
                if args == ("status", "--json"):
                    return subprocess.CompletedProcess([], 0, json.dumps(stopped).encode(), b"")
                if args[0] == "up":
                    return subprocess.CompletedProcess([], 0, b"RUNNING", b"")
                self.fail(f"unexpected protected parent operation: {args}")

            def parent_json(_root, *args, **_kwargs):
                if args[0] == "term-check":
                    return None, {"status": "PASS", "terminfo_entry": True,
                                  "clear_capability": True, "clear_command": True}
                if args == ("status", "--json"):
                    return None, next(statuses)
                self.fail(f"unexpected JSON parent operation: {args}")

            data = acceptance.report_template("3" * 40, "parent_accept", report_dir)
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(acceptance.sys, "stdin", TTYBuffer()))
                stack.enter_context(mock.patch.object(acceptance.sys, "stdout", TTYBuffer()))
                stack.enter_context(mock.patch.dict(os.environ, {
                    "LM_VM_TERM_VALIDATED": "1", "TERM": "xterm-256color"}))
                stack.enter_context(mock.patch.object(acceptance, "runtime_dir",
                                                      return_value=reports))
                stack.enter_context(mock.patch.object(acceptance, "_parent_command",
                                                      side_effect=parent_command))
                stack.enter_context(mock.patch.object(acceptance, "_parent_json",
                                                      side_effect=parent_json))
                approval = stack.enter_context(mock.patch.object(acceptance, "_owner_approval"))
                stack.enter_context(mock.patch.object(acceptance, "inspect_live_parent",
                    return_value={"m2_contract_compatible": False}))
                with self.assertRaisesRegex(acceptance.GateError, "guest protected asset"):
                    acceptance.run_parent_acceptance(pathlib.Path("/protected/install"),
                        "openai/gpt-test", data, report_dir)

            self.assertEqual([op[0] for op in operations], ["status", "up", "up"])
            up_args = operations[1]
            self.assertIn("--expected-disk-device", up_args)
            self.assertIn("55", up_args)
            self.assertIn("--expected-disk-inode", up_args)
            self.assertIn("11441015", up_args)
            self.assertNotIn("retire", [op[0] for op in operations])
            self.assertIn("RESUME SAME DISK", approval.call_args.args[1])
            self.assertIn("55:11441015", approval.call_args.args[1])
            self.assertEqual(data["evidence"]["provisioning_origin"]["disk_identity"],
                             {"device": 55, "inode": 11441015})
            self.assertEqual(data["evidence"]["provisioning_origin"][
                "factory_reset_attempt_id"], "20261116T222130Z-" + "c" * 32)
            self.assertFalse(data["evidence"]["provisioning_origin"]["reset_or_retirement_repeated"])

    def test_stopped_parent_with_mismatched_recorded_disk_is_blocked_without_owner_action(self):
        with tempfile.TemporaryDirectory(prefix="owner-stopped-mismatch-") as temporary:
            report_dir = pathlib.Path(temporary)
            stopped = {
                "status": "STOPPED", "parent_id": "default", "run_id": "a" * 32,
                "disk": {"path": "disk.qcow2", "device": 55, "inode": 11441015, "format": "qcow2",
                         "provisioned": True, "virtual_capacity_gib": 64},
            }
            data = acceptance.report_template("4" * 40, "parent_accept", report_dir)
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(acceptance.sys, "stdin", TTYBuffer()))
                stack.enter_context(mock.patch.object(acceptance.sys, "stdout", TTYBuffer()))
                stack.enter_context(mock.patch.dict(os.environ, {
                    "LM_VM_TERM_VALIDATED": "1", "TERM": "xterm-256color"}))
                stack.enter_context(mock.patch.object(acceptance, "_parent_command",
                    return_value=subprocess.CompletedProcess([], 0, json.dumps(stopped).encode(), b"")))
                stack.enter_context(mock.patch.object(acceptance, "_replacement_provision_origin",
                    return_value={"attempt_id": "20261116T222130Z-" + "d" * 32,
                                  "factory_reset_attempt_id": "20261116T222130Z-" + "d" * 32,
                                  "disk_identity": {"device": 55, "inode": 999}}))
                owner_approval = stack.enter_context(mock.patch.object(acceptance, "_owner_approval"))
                parent_ok = stack.enter_context(mock.patch.object(acceptance, "_parent_ok"))
                with self.assertRaisesRegex(acceptance.GateError, "does not match"):
                    acceptance.run_parent_acceptance(pathlib.Path("/protected/install"),
                        "openai/gpt-test", data, report_dir)
            owner_approval.assert_not_called()
            parent_ok.assert_not_called()

    def test_running_disk_identity_mismatch_has_sanitized_report_category_and_no_lifecycle_action(self):
        with tempfile.TemporaryDirectory(prefix="owner-running-mismatch-") as temporary:
            report_dir = pathlib.Path(temporary)
            running = {
                "status": "RUNNING", "parent_id": "default", "run_id": "a" * 32,
                "supervisor_pid": 101, "qemu_pid": 202,
                "disk": {"path": "disk.qcow2", "device": 55, "inode": 999,
                         "format": "qcow2", "provisioned": True,
                         "virtual_capacity_gib": 64},
            }
            data = acceptance.report_template("a" * 40, "parent_accept", report_dir)
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(acceptance.sys, "stdin", TTYBuffer()))
                stack.enter_context(mock.patch.object(acceptance.sys, "stdout", TTYBuffer()))
                stack.enter_context(mock.patch.dict(os.environ, {
                    "LM_VM_TERM_VALIDATED": "1", "TERM": "xterm-256color"}))
                stack.enter_context(mock.patch.object(acceptance, "_parent_command",
                    return_value=subprocess.CompletedProcess([], 0,
                        json.dumps(running).encode(), b"")))
                stack.enter_context(mock.patch.object(acceptance, "_replacement_provision_origin",
                    return_value={"attempt_id": "20261117T070000Z-" + "e" * 32,
                                  "factory_reset_attempt_id": "20261117T060000Z-" + "f" * 32,
                                  "disk_identity": {"device": 55, "inode": 11441015}}))
                owner_approval = stack.enter_context(mock.patch.object(acceptance, "_owner_approval"))
                parent_ok = stack.enter_context(mock.patch.object(acceptance, "_parent_ok"))
                with self.assertRaisesRegex(acceptance.GateError, "RUNNING disk does not match") as caught:
                    acceptance.run_parent_acceptance(pathlib.Path("/protected/install"),
                        "openai/gpt-test", data, report_dir)
            self.assertEqual(caught.exception.category,
                             "provenance_running_disk_identity_mismatch")
            owner_approval.assert_not_called()
            parent_ok.assert_not_called()

    def test_stopped_disk_without_candidate_provenance_is_blocked_not_retired(self):
        with tempfile.TemporaryDirectory(prefix="owner-stopped-unproven-") as temporary:
            report_dir = pathlib.Path(temporary)
            stopped = {
                "status": "STOPPED", "parent_id": "default", "run_id": "a" * 32,
                "disk": {"path": "disk.qcow2", "device": 55, "inode": 11441015,
                         "format": "qcow2", "provisioned": True,
                         "virtual_capacity_gib": 64},
            }
            data = acceptance.report_template("8" * 40, "parent_accept", report_dir)
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(acceptance.sys, "stdin", TTYBuffer()))
                stack.enter_context(mock.patch.object(acceptance.sys, "stdout", TTYBuffer()))
                stack.enter_context(mock.patch.dict(os.environ, {
                    "LM_VM_TERM_VALIDATED": "1", "TERM": "xterm-256color"}))
                stack.enter_context(mock.patch.object(acceptance, "_parent_command",
                    return_value=subprocess.CompletedProcess([], 0, json.dumps(stopped).encode(), b"")))
                stack.enter_context(mock.patch.object(acceptance, "_replacement_provision_origin",
                                                      return_value=None))
                owner_approval = stack.enter_context(mock.patch.object(acceptance, "_owner_approval"))
                parent_ok = stack.enter_context(mock.patch.object(acceptance, "_parent_ok"))
                with self.assertRaisesRegex(acceptance.GateError, "no private reset-and-failed-start provenance"):
                    acceptance.run_parent_acceptance(pathlib.Path("/protected/install"),
                        "openai/gpt-test", data, report_dir)
            owner_approval.assert_not_called()
            parent_ok.assert_not_called()

    def test_starting_parent_is_never_retired_or_factory_reset_on_acceptance_resume(self):
        with tempfile.TemporaryDirectory(prefix="owner-starting-resume-") as temporary:
            report_dir = pathlib.Path(temporary)
            report_dir.mkdir(mode=0o700, exist_ok=True)
            starting = {"status": "STARTING", "parent_id": "default", "run_id": "a" * 32}
            data = acceptance.report_template("7" * 40, "parent_accept", report_dir)
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(acceptance.sys, "stdin", TTYBuffer()))
                stack.enter_context(mock.patch.object(acceptance.sys, "stdout", TTYBuffer()))
                stack.enter_context(mock.patch.dict(os.environ, {
                    "LM_VM_TERM_VALIDATED": "1", "TERM": "xterm-256color"}))
                stack.enter_context(mock.patch.object(acceptance, "_parent_command",
                    return_value=subprocess.CompletedProcess([], 0, json.dumps(starting).encode(), b"")))
                parent_json = stack.enter_context(mock.patch.object(acceptance, "_parent_json"))
                parent_ok = stack.enter_context(mock.patch.object(acceptance, "_parent_ok"))
                with self.assertRaisesRegex(acceptance.GateError, "still STARTING"):
                    acceptance.run_parent_acceptance(pathlib.Path("/protected/install"),
                        "openai/gpt-test", data, report_dir)
            parent_json.assert_not_called()
            parent_ok.assert_not_called()

    def test_failed_start_running_disk_is_reused_not_retired(self):
        with tempfile.TemporaryDirectory(prefix="owner-running-resume-") as temporary:
            reports = pathlib.Path(temporary) / "reports"
            reports.mkdir(mode=0o700)
            report_dir = reports / ("20261116T231000Z-" + "9" * 32)
            report_dir.mkdir(mode=0o700)
            prior_id = "20261116T224000Z-" + "8" * 32
            prior_dir = reports / prior_id
            prior_dir.mkdir(mode=0o700)
            prior_path = prior_dir / "report.json"
            prior_path.write_text(json.dumps({
                "candidate_sha": "6" * 40, "mode": "parent_accept",
                "attempt_id": prior_id, "status": "FAIL",
                "gates": {"retirement": "PASS", "fresh_provision": "FAIL"},
                "evidence": {
                    "retirement": {"status": "FACTORY_RESET"},
                    "fresh_provision_failure": {
                        "operation": "parent up", "exit_status": 2,
                        "post_failure_disk_identity": {"device": 55, "inode": 11441015},
                    },
                    "fresh_provision_approval": {
                        "owner_approved": True, "candidate_sha": "6" * 40,
                        "capacity_gib": 64, "from_status": "NOT_CREATED",
                    },
                },
                "notes": ["interrupted during first start"],
            }), encoding="utf-8")
            os.chmod(prior_path, 0o600)
            running = {
                "status": "RUNNING", "parent_id": "default", "run_id": "f" * 32,
                "supervisor_pid": 301, "qemu_pid": 302,
                "disk": {"path": "disk.qcow2", "device": 55, "inode": 11441015,
                         "format": "qcow2", "provisioned": True,
                         "virtual_capacity_gib": 64, "file_bytes": 2048,
                         "host_allocated_bytes": 4096},
            }
            operations = []

            def parent_command(_root, *args, **_kwargs):
                operations.append(args)
                if args == ("status", "--json"):
                    return subprocess.CompletedProcess([], 0, json.dumps(running).encode(), b"")
                if args[0] == "up":
                    return subprocess.CompletedProcess([], 0, b"RUNNING", b"")
                self.fail(f"unexpected protected parent operation: {args}")

            def parent_json(_root, *args, **_kwargs):
                if args[0] == "term-check":
                    return None, {"status": "PASS", "terminfo_entry": True,
                                  "clear_capability": True, "clear_command": True}
                if args == ("status", "--json"):
                    return None, running
                self.fail(f"unexpected JSON parent operation: {args}")

            data = acceptance.report_template("6" * 40, "parent_accept", report_dir)
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(acceptance.sys, "stdin", TTYBuffer()))
                stack.enter_context(mock.patch.object(acceptance.sys, "stdout", TTYBuffer()))
                stack.enter_context(mock.patch.dict(os.environ, {
                    "LM_VM_TERM_VALIDATED": "1", "TERM": "xterm-256color"}))
                stack.enter_context(mock.patch.object(acceptance, "runtime_dir",
                                                      return_value=reports))
                stack.enter_context(mock.patch.object(acceptance, "_parent_command",
                                                      side_effect=parent_command))
                stack.enter_context(mock.patch.object(acceptance, "_parent_json",
                                                      side_effect=parent_json))
                approval = stack.enter_context(mock.patch.object(acceptance, "_owner_approval"))
                stack.enter_context(mock.patch.object(acceptance, "inspect_live_parent",
                    return_value={"m2_contract_compatible": False}))
                with self.assertRaisesRegex(acceptance.GateError, "guest protected asset"):
                    acceptance.run_parent_acceptance(pathlib.Path("/protected/install"),
                        "openai/gpt-test", data, report_dir)

            self.assertEqual([op[0] for op in operations], ["status", "up"])
            self.assertNotIn("retire", [op[0] for op in operations])
            approval.assert_not_called()
            self.assertEqual(data["evidence"]["fresh_provision"]["status"], "RUNNING")
            self.assertEqual(data["evidence"]["retirement"]["status"],
                             "NO_RETIREMENT_ALREADY_RUNNING_REPLACEMENT")

    def test_missing_a4_behavioral_evidence_is_a_hard_gate(self):
        evidence = {
            "protected_update_disabled": True,
            "runtime_config_update_disabled": True,
            "protected_share_disabled": True,
            "runtime_config_sharing_disabled": True,
            "update_share_positive_control_accepted": True,
            "hostile_fixture_is_expected_negative_control": True,
            "share_command_absent_from_pinned_runtime": True,
            "runtime_is_pinned_v225": True,
            "runtime_autoupdate_disabled": True,
            "runtime_sharing_disabled": True,
            "runtime_telemetry_optout": True,
            "runtime_otel_sdk_disabled": True,
            "runtime_otel_exporters_disabled": True,
            "runtime_otlp_exporter_absent": True,
            "empty_user_data_before_probe": True,
            "selected_model_listed": True,
        }
        required = tuple(evidence)
        acceptance._require_probe(evidence, required, "#55 A4 behavior")
        for omitted in required:
            with self.subTest(omitted=omitted):
                incomplete = dict(evidence)
                incomplete.pop(omitted)
                with self.assertRaises(acceptance.GateError):
                    acceptance._require_probe(incomplete, required, "#55 A4 behavior")

    def test_live_guest_compatibility_uses_fixed_hashes_modes_version_and_identity(self):
        contract_spec = importlib.util.spec_from_file_location(
            "test_guest_contract", ROOT / "lib/vm/parent-session-contract.py")
        contract = importlib.util.module_from_spec(contract_spec)
        contract_spec.loader.exec_module(contract)
        protected = json.dumps(
            {"autoupdate": False, "share": "disabled", "permission": {"edit": "ask", "bash": "ask"}},
            separators=(",", ":"),
        ).encode()
        inventory = {
            path: {"type": "file", "uid": 0, "gid": 0,
                   "mode": "0755" if path in (
                       "/usr/local/bin/lm-opencode", "/usr/local/libexec/lm-opencode",
                       "/usr/local/libexec/lm-opencode-auth") else "0644", "sha256": digest}
            for path, digest in contract.ASSET_SHA256.items()
        }
        inventory["/etc/opencode/opencode.json"] = {
            "type": "file", "uid": 0, "gid": 0, "mode": "0644",
            "sha256": hashlib.sha256(protected).hexdigest(),
        }
        inventory.update({
            "/usr/local/bin/opencode": {
                "type": "symlink", "target": "../lib/node_modules/@opencode/cli/bin/opencode",
                "uid": 0, "gid": 0, "mode": "0777",
            },
            "/usr/local/lib/node_modules/@opencode/cli/package.json": {
                "type": "file", "uid": 0, "gid": 0, "mode": "0644", "version": "2.2.5",
            },
            "/usr/local/lib/node_modules/@opencode/cli/bin/opencode": {
                "type": "file", "uid": 0, "gid": 0, "mode": "0755", "sha256": "1" * 64,
            },
            "/usr/bin/tmux": {"type": "file", "uid": 0, "mode": "0755"},
        })
        parent_status = {"status": "RUNNING", "run_id": "a" * 32,
                         "supervisor_pid": 1234,
                         "disk": {"path": "/private/disk.qcow2", "device": 1, "inode": 2}}
        completed = [
            subprocess.CompletedProcess([], 0, json.dumps(parent_status).encode(), b""),
            subprocess.CompletedProcess([], 0, json.dumps(inventory).encode(), b""),
            subprocess.CompletedProcess([], 0, json.dumps(parent_status).encode(), b""),
        ]
        with mock.patch.object(acceptance.subprocess, "run", side_effect=completed):
            result = acceptance.inspect_live_parent(ROOT)
        self.assertTrue(result["m2_contract_compatible"])
        self.assertEqual(result["parent_identity"]["disk"]["inode"], 2)
        self.assertIn("sha256", result["observed_guest_inventory"]["/usr/local/libexec/lm-opencode-auth"])

        inventory["/usr/local/libexec/lm-opencode-auth"]["sha256"] = "0" * 64
        completed[1] = subprocess.CompletedProcess([], 0, json.dumps(inventory).encode(), b"")
        with mock.patch.object(acceptance.subprocess, "run", side_effect=completed):
            result = acceptance.inspect_live_parent(ROOT)
        self.assertFalse(result["m2_contract_compatible"])

        malformed = [
            subprocess.CompletedProcess([], 0, b"[]", b""),
        ]
        with mock.patch.object(acceptance.subprocess, "run", side_effect=malformed):
            with self.assertRaisesRegex(acceptance.GateError, "invalid evidence shape"):
                acceptance.inspect_live_parent(ROOT)
        malformed_inventory = [
            subprocess.CompletedProcess([], 0, json.dumps(parent_status).encode(), b""),
            subprocess.CompletedProcess([], 0, b"[]", b""),
        ]
        with mock.patch.object(acceptance.subprocess, "run", side_effect=malformed_inventory):
            with self.assertRaisesRegex(acceptance.GateError, "invalid inventory shape"):
                acceptance.inspect_live_parent(ROOT)

    def test_installer_manifest_and_lab_vm_bootstrap_dependencies_stay_in_sync(self):
        source = (ROOT / "lab-vm").read_text(encoding="utf-8")
        for relative in acceptance.INSTALL_FILES:
            if relative == "lab-vm":
                continue
            self.assertIn(f'root / "{relative}"', source,
                          f"installed runtime dependency missing from lab-vm bootstrap: {relative}")
        self.assertIn("accept)", source)
        self.assertIn("--parent-accept", source)
        self.assertIn("--milestone M2", source)

    @staticmethod
    def runtime_fixture(temporary, *, system_mode=0o755, system_uid=0):
        root = pathlib.Path(temporary)
        system_parent = root / "run" / "user"
        system_parent.mkdir(parents=True, mode=0o755)
        user_root = system_parent / str(os.getuid())
        user_root.mkdir(mode=0o700)
        os.chmod(user_root, 0o700)
        for directory in (root, root / "run", system_parent):
            os.chmod(directory, system_mode)
        runtime = user_root / "little-mester-vm-acceptance"

        real_lstat = os.lstat

        def fixture_lstat(path):
            path = pathlib.Path(path)
            info = real_lstat(path)
            if path == user_root or user_root in path.parents:
                return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid)
            return SimpleNamespace(
                st_mode=(info.st_mode & ~0o222) | stat.S_IXOTH,
                st_uid=system_uid,
            )

        return system_parent, user_root, runtime, fixture_lstat

    def test_directory_chain_accepts_owner_private_final_without_other_execute(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, user_root, _, fixture_lstat = self.runtime_fixture(temporary)
            with mock.patch.object(acceptance.os, "lstat", side_effect=fixture_lstat):
                acceptance.check_directory_chain(user_root, os.getuid(), 0o700)

    def test_previous_nested_validation_rejects_owner_owned_parent_as_root_ancestor(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, user_root, runtime, fixture_lstat = self.runtime_fixture(temporary)
            runtime.mkdir(mode=0o700)
            with mock.patch.object(acceptance.os, "lstat", side_effect=fixture_lstat):
                with self.assertRaisesRegex(acceptance.GateError, "owner does not match"):
                    acceptance.check_directory_chain(runtime, os.getuid(), 0o700)

    def test_protected_install_chain_accepts_root_owned_0755_final_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            final = root / "opt" / "little-mester" / "v3" / ("a" * 40)
            final.mkdir(parents=True, mode=0o755)
            for path in (root, root / "opt", root / "opt/little-mester", root / "opt/little-mester/v3", final):
                os.chmod(path, 0o755)
            real_lstat = os.lstat

            def root_lstat(path):
                info = real_lstat(path)
                mode = info.st_mode if pathlib.Path(path) == final else (info.st_mode & ~0o222) | stat.S_IXOTH
                return SimpleNamespace(
                    st_mode=mode,
                    st_uid=0,
                )

            with mock.patch.object(acceptance.os, "lstat", side_effect=root_lstat):
                acceptance.check_directory_chain(final, 0, 0o755)

    def test_protected_install_chain_rejects_unsafe_ancestry_and_final_mode(self):
        for reason in ("symlink", "non-directory", "group-writable", "world-writable", "wrong-owner", "no-traverse", "final-mode"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                root = pathlib.Path(temporary)
                final = root / "opt" / "little-mester" / "v3" / ("b" * 40)
                final.mkdir(parents=True, mode=0o755)
                paths = (root, root / "opt", root / "opt/little-mester", root / "opt/little-mester/v3", final)
                for path in paths:
                    os.chmod(path, 0o755)
                bad = paths[2]

                def unsafe_lstat(path):
                    path = pathlib.Path(path)
                    mode = stat.S_IFDIR | 0o755
                    uid = 0
                    if reason == "symlink" and path == bad:
                        mode = stat.S_IFLNK | 0o777
                    elif reason == "non-directory" and path == bad:
                        mode = stat.S_IFREG | 0o644
                    elif reason == "group-writable" and path == bad:
                        mode |= stat.S_IWGRP
                    elif reason == "world-writable" and path == bad:
                        mode |= stat.S_IWOTH
                    elif reason == "wrong-owner" and path == bad:
                        uid = os.getuid() + 1
                    elif reason == "no-traverse" and path == bad:
                        mode &= ~stat.S_IXOTH
                    elif reason == "final-mode" and path == final:
                        mode = stat.S_IFDIR | 0o751
                    return SimpleNamespace(st_mode=mode, st_uid=uid)

                with mock.patch.object(acceptance.os, "lstat", side_effect=unsafe_lstat):
                    expected = {
                        "symlink": "contains a symlink",
                        "non-directory": "not a directory",
                        "group-writable": "group-writable",
                        "world-writable": "world-writable",
                        "wrong-owner": "owner does not match",
                        "no-traverse": "not traversable",
                        "final-mode": "normalized mode",
                    }[reason]
                    with self.assertRaisesRegex(acceptance.GateError, expected):
                        acceptance.check_directory_chain(final, 0, 0o755)

    def test_install_prefix_defaults_and_accepts_only_normalized_absolute_paths(self):
        sha = "a" * 40
        self.assertEqual(acceptance.install_path(sha), acceptance.INSTALL_PARENT / sha)
        self.assertEqual(acceptance.install_path(sha, pathlib.Path("/usr/local/little-mester")),
                         pathlib.Path("/usr/local/little-mester/v3") / sha)
        self.assertEqual(acceptance.install_prefix("/srv/little-mester"),
                         pathlib.Path("/srv/little-mester"))
        for invalid in ("relative/prefix", "/", "/usr/local/../tmp", "/usr/local/./tmp"):
            with self.subTest(prefix=invalid), self.assertRaises(argparse.ArgumentTypeError):
                acceptance.install_prefix(invalid)

    def test_cli_install_uses_requested_prefix(self):
        sha = "a" * 40
        prefix = pathlib.Path("/srv/little-mester")
        with tempfile.TemporaryDirectory() as temporary:
            report_dir = pathlib.Path(temporary) / "attempt"
            report_dir.mkdir(mode=0o700)
            with (
                mock.patch.object(acceptance, "new_report_dir", return_value=report_dir),
                mock.patch.object(acceptance, "candidate_commit"),
                mock.patch.object(acceptance, "install_candidate",
                                  return_value=prefix / "v3" / sha) as install,
                mock.patch.object(acceptance, "installed_preflight"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = acceptance.main([
                    "--install", "--sha", sha, "--prefix", str(prefix), "--repo", str(ROOT)])
            self.assertEqual(result, 0)
            install.assert_called_once_with(ROOT, sha, prefix)

    def test_runtime_validator_accepts_protected_prefix_and_private_descendants(self):
        with tempfile.TemporaryDirectory() as temporary:
            system_parent, user_root, runtime, fixture_lstat = self.runtime_fixture(temporary)
            runtime.mkdir(mode=0o700)
            report_parent = runtime / "attempt-parent"
            report_parent.mkdir(mode=0o700)
            report_dir = report_parent / "attempt"
            report_dir.mkdir(mode=0o700)
            with mock.patch.object(acceptance, "RUNTIME_ROOT", runtime), \
                    mock.patch.object(acceptance.os, "lstat", side_effect=fixture_lstat):
                acceptance.check_private_runtime_chain(user_root, system_parent)
                acceptance.check_private_runtime_chain(runtime, system_parent)
                acceptance.check_private_runtime_chain(report_parent, system_parent)
                acceptance.check_private_runtime_chain(report_dir, system_parent)

    def test_runtime_validator_rejects_symlink_and_non_directory_system_components(self):
        for make_unsafe in ("symlink", "non-directory"):
            with self.subTest(make_unsafe=make_unsafe), tempfile.TemporaryDirectory() as temporary:
                system_parent, user_root, runtime, fixture_lstat = self.runtime_fixture(temporary)
                displaced = system_parent.with_name("user-original")
                system_parent.rename(displaced)
                if make_unsafe == "symlink":
                    system_parent.symlink_to(displaced)
                else:
                    system_parent.write_text("not a directory", encoding="ascii")
                with mock.patch.object(acceptance.os, "lstat", side_effect=fixture_lstat):
                    with self.assertRaises(acceptance.GateError):
                        acceptance.check_private_runtime_chain(user_root, system_parent)

    def test_runtime_validator_rejects_unsafe_system_ownership_and_permissions(self):
        for reason in ("group-writable", "world-writable", "wrong-owner", "no-traverse"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                system_parent, user_root, runtime, fixture_lstat = self.runtime_fixture(temporary)

                def unsafe_lstat(path):
                    info = fixture_lstat(path)
                    path = pathlib.Path(path)
                    if path == system_parent:
                        mode = info.st_mode
                        uid = info.st_uid
                        if reason == "group-writable":
                            mode |= stat.S_IWGRP
                        elif reason == "world-writable":
                            mode |= stat.S_IWOTH
                        elif reason == "wrong-owner":
                            uid = os.getuid()
                        else:
                            mode &= ~stat.S_IXOTH
                        return SimpleNamespace(st_mode=mode, st_uid=uid)
                    return info

                with mock.patch.object(acceptance.os, "lstat", side_effect=unsafe_lstat):
                    with self.assertRaises(acceptance.GateError):
                        acceptance.check_private_runtime_chain(user_root, system_parent)

    def test_runtime_validator_rejects_private_owner_mode_traversal_and_transition(self):
        for reason in (
            "wrong-owner", "group-writable", "world-writable", "wrong-mode", "no-traverse",
            "transition", "symlink", "child-group-writable", "child-world-writable",
            "child-wrong-mode", "child-no-traverse",
        ):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                system_parent, user_root, runtime, fixture_lstat = self.runtime_fixture(temporary)
                runtime.mkdir(mode=0o700)
                child = runtime / "child"
                child.mkdir(mode=0o700)
                if reason == "group-writable":
                    os.chmod(runtime, 0o720)
                elif reason == "world-writable":
                    os.chmod(runtime, 0o702)
                elif reason == "wrong-mode":
                    os.chmod(runtime, 0o750)
                elif reason == "no-traverse":
                    os.chmod(runtime, 0o600)
                elif reason == "symlink":
                    child.rmdir()
                    child.symlink_to(user_root)
                elif reason == "child-group-writable":
                    os.chmod(child, 0o720)
                elif reason == "child-world-writable":
                    os.chmod(child, 0o702)
                elif reason == "child-wrong-mode":
                    os.chmod(child, 0o750)
                elif reason == "child-no-traverse":
                    os.chmod(child, 0o600)

                def unsafe_lstat(path):
                    info = fixture_lstat(path)
                    path = pathlib.Path(path)
                    if reason == "wrong-owner" and path == user_root:
                        return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)
                    if reason == "transition" and path == child:
                        return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)
                    return info

                with mock.patch.object(acceptance.os, "lstat", side_effect=unsafe_lstat):
                    with self.assertRaises(acceptance.GateError):
                        acceptance.check_private_runtime_chain(child, system_parent)

    def test_report_creation_and_preflight_use_validated_private_runtime_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            system_parent, _, runtime, fixture_lstat = self.runtime_fixture(temporary)
            install = pathlib.Path(temporary) / "protected-install"
            install.mkdir(mode=0o755)
            with mock.patch.object(acceptance, "RUNTIME_ROOT", runtime), \
                    mock.patch.object(acceptance.os, "lstat", side_effect=fixture_lstat), \
                    mock.patch.object(acceptance, "candidate_commit"), \
                    mock.patch.object(acceptance, "install_path", return_value=install), \
                    mock.patch.object(acceptance, "verify_install", return_value=install), \
                    mock.patch.object(acceptance, "installed_preflight"), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = acceptance.main(["--preflight", "--sha", "a" * 40, "--repo", str(ROOT)])
            self.assertEqual(result, 0)
            attempts = list(runtime.iterdir())
            self.assertEqual(len(attempts), 1)
            report = attempts[0] / "report.json"
            self.assertEqual(stat.S_IMODE(os.lstat(report).st_mode), 0o600)
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "PASS")
            self.assertEqual(payload["gates"]["preflight"], "PASS")

    def test_early_runtime_gate_error_is_reported_without_traceback_or_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            system_parent, _, runtime, fixture_lstat = self.runtime_fixture(temporary)
            errors = io.StringIO()
            output = io.StringIO()

            def unsafe_system_lstat(path):
                info = fixture_lstat(path)
                if pathlib.Path(path) == system_parent:
                    return SimpleNamespace(st_mode=info.st_mode | stat.S_IWGRP, st_uid=info.st_uid)
                return info

            with mock.patch.object(acceptance, "RUNTIME_ROOT", runtime), \
                mock.patch.object(acceptance.os, "lstat", side_effect=unsafe_system_lstat), \
                mock.patch.object(acceptance, "candidate_commit") as candidate, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                result = acceptance.main(["--preflight", "--sha", "a" * 40, "--repo", str(ROOT)])
            self.assertEqual(result, 3)
            self.assertIn("BLOCKED: protected system directory", errors.getvalue())
            self.assertNotIn("Traceback", errors.getvalue())
            self.assertEqual(output.getvalue(), "")
            self.assertFalse(runtime.exists())
            candidate.assert_not_called()

    def test_initial_report_write_failure_is_controlled_and_stops_candidate_work(self):
        errors = io.StringIO()
        output = io.StringIO()
        with mock.patch.object(acceptance, "new_report_dir", return_value=pathlib.Path("/trusted/attempt")), \
                mock.patch.object(acceptance, "save_report", side_effect=OSError("disk unavailable")), \
                mock.patch.object(acceptance, "candidate_commit") as candidate, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = acceptance.main(["--preflight", "--sha", "a" * 40, "--repo", str(ROOT)])
        self.assertEqual(result, 1)
        self.assertIn("FAIL: runner initialization failed (OSError)", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())
        self.assertEqual(output.getvalue(), "")
        candidate.assert_not_called()

    def test_final_report_update_failure_does_not_print_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            install = pathlib.Path(temporary) / "install"
            install.mkdir()
            errors = io.StringIO()
            output = io.StringIO()
            with mock.patch.object(acceptance, "new_report_dir", return_value=pathlib.Path("/trusted/attempt")), \
                    mock.patch.object(acceptance, "save_report"), \
                    mock.patch.object(acceptance, "candidate_commit"), \
                    mock.patch.object(acceptance, "install_path", return_value=install), \
                    mock.patch.object(acceptance, "verify_install", return_value=install), \
                    mock.patch.object(acceptance, "installed_preflight") as preflight, \
                    mock.patch.object(acceptance.os, "open", side_effect=OSError("update unavailable")), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                result = acceptance.main(["--preflight", "--sha", "a" * 40, "--repo", str(ROOT)])
        self.assertEqual(result, 1)
        self.assertIn("FAIL: report update failed", errors.getvalue())
        self.assertIn("OVERALL: FAIL", output.getvalue())
        self.assertNotIn("OVERALL: PASS", output.getvalue())
        preflight.assert_called_once_with(install, None)

    def test_report_directory_allocation_refusal_is_a_gate_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = pathlib.Path(temporary)
            os.chmod(parent, 0o700)
            with mock.patch.object(acceptance.os, "mkdir", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(acceptance.GateError, "trusted report directory"):
                    acceptance.new_report_dir(parent)

    def test_unsafe_report_parent_is_refused_by_production_private_directory_check(self):
        for reason in ("symlink", "wrong-mode", "wrong-owner"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                root = pathlib.Path(temporary)
                parent = root / "reports"
                if reason == "symlink":
                    parent.symlink_to(root)
                else:
                    parent.mkdir(mode=0o700)
                    if reason == "wrong-mode":
                        os.chmod(parent, 0o750)
                real_lstat = os.lstat

                def wrong_owner_lstat(path):
                    info = real_lstat(path)
                    if pathlib.Path(path) == parent and reason == "wrong-owner":
                        return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)
                    return info

                with mock.patch.object(acceptance.os, "lstat", side_effect=wrong_owner_lstat):
                    with self.assertRaises(acceptance.GateError):
                        acceptance.new_report_dir(parent)

    def test_cli_requires_full_sha_and_rejects_multiple_operations(self):
        with self.assertRaises(SystemExit):
            acceptance.parser().parse_args(["--install", "--sha", "abc"])
        with self.assertRaises(SystemExit):
            acceptance.parser().parse_args(["--install", "--run", "--sha", "a" * 40])
        parsed = acceptance.parser().parse_args(["--sha", "a" * 40])
        debug = acceptance.parser().parse_args(["--run", "--sha", "a" * 40, "--debug"])
        self.assertTrue(debug.debug)
        self.assertFalse(any(getattr(parsed, name) for name in (
            "preflight", "install", "storage_proof", "run", "verify_cleanup", "all", "parent_accept"
        )))

    def test_parent_acceptance_has_simulated_complete_positive_pass_path(self):
        with tempfile.TemporaryDirectory(prefix="owner-m2-positive-") as temporary:
            report_dir = pathlib.Path(temporary)
            operations = []
            old = {"status": "RUNNING", "run_id": "a" * 32, "parent_id": "default",
                   "supervisor_pid": 101, "qemu_pid": 202,
                   "retired_bundle": "retired-" + "c" * 32,
                   "retired_disk_identity": {"device": 1, "inode": 11},
                    "disk": {"path": "disk.qcow2", "device": 1, "inode": 22, "format": "qcow2", "provisioned": True,
                            "virtual_capacity_gib": 64, "file_bytes": 4096,
                            "host_allocated_bytes": 4096}}
            after = {**old, "run_id": "b" * 32}
            status_results = iter((old, old, old, old, old,
                                   {**old, "status": "STOPPED"}, after, after))
            protections = {name: True for name in (
                "project_config_ignored", "protected_update_disabled", "runtime_config_update_disabled",
                "protected_share_disabled", "runtime_config_sharing_disabled",
                "update_share_positive_control_accepted",
                "hostile_fixture_is_expected_negative_control",
                "share_command_absent_from_pinned_runtime", "runtime_is_pinned_v225",
                "hostile_plugin_not_executed", "runtime_cwd_is_workspace",
                "runtime_project_config_disabled", "runtime_config_path_protected",
                "runtime_plugin_config_protected", "runtime_autoupdate_disabled",
                "runtime_sharing_disabled", "runtime_telemetry_optout", "runtime_error_reporting_off",
                "runtime_otel_sdk_disabled", "runtime_otel_exporters_disabled",
                "runtime_otlp_exporter_absent", "hostile_fixture_cleaned_after_probe")}
            protections["hostile_fixture_cleaned_after_probe"] = False
            post_protections = {**protections,
                                "hostile_fixture_is_expected_negative_control": False,
                                "hostile_fixture_cleaned_after_probe": True}
            protection_checks = iter((protections, post_protections))

            def parent_json(_root, *args, **_kwargs):
                operations.append(("json", args[0]))
                if args[0] == "term-check":
                    return None, {"status": "PASS", "terminfo_entry": True,
                                  "clear_capability": True, "clear_command": True}
                if args[0] == "status":
                    return None, next(status_results)
                if args[0] == "network-check":
                    return None, {"public_proxy": "PASS", "private_proxy": "BLOCKED", "direct_public": "BLOCKED"}
                if args[0] == "models-check":
                    return None, {"status": "OK", "model_list_command_succeeded": True,
                                  "selected_openai_model_listed": True, "empty_user_data_before_probe": True}
                if args[0] == "telemetry-check":
                    return None, {"status": "PASS", "both_runtime_invocations_succeeded": True,
                                  "positive_control_exported_to_loopback": True,
                                  "no_exporter_configured_control_exported_nothing": True}
                if args[0] == "auth-check":
                    return None, {"openai_credential_present": True}
                if args[0] == "protection-check":
                    return None, next(protection_checks)
                if args[0] in ("recover", "reconcile"):
                    return None, {"action": "none", "status": "STOPPED"}
                if args[0] == "inspect":
                    return None, {"/home/mester/.local/share/opencode/tmux": {"socket_count": 0}}
                self.fail(f"unexpected parent JSON operation: {args}")

            def parent_command(_root, *args, **_kwargs):
                operations.append(("command", args[0]))
                if args == ("status", "--json"):
                    return subprocess.CompletedProcess([], 0, json.dumps(next(status_results)).encode(), b"")
                blocked = args[:2] == ("recover", "--dry-run")
                return subprocess.CompletedProcess(
                    [], 2 if blocked else 0, b"",
                    b"REFUSED: live supervisor or lifecycle operation owns the parent lock" if blocked else b"")

            def wait_tui(_root, predicate, timeout=8.0):
                states = (
                    {"session_count": 1, "target_count": 1, "exit_dialog_visible": False,
                     "attached_clients": 0, "size": "107x43"},
                    {"session_count": 1, "target_count": 1, "exit_dialog_visible": True,
                     "attached_clients": 0, "size": "107x43"},
                    {"session_count": 0, "target_count": 0, "exit_dialog_visible": False,
                     "attached_clients": 0, "size": "107x43"},
                )
                return next(state for state in states if predicate(state))

            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(acceptance.sys, "stdin", TTYBuffer()))
                output = TTYBuffer()
                stack.enter_context(mock.patch.object(acceptance.sys, "stdout", output))
                stack.enter_context(mock.patch.dict(os.environ, {"LM_VM_TERM_VALIDATED": "1", "TERM": "xterm-256color"}))
                for target, name, kwargs in (
                    (acceptance, "new_report_dir", {"return_value": report_dir}),
                    (acceptance, "candidate_commit", {}),
                    (acceptance, "verify_install", {"return_value": pathlib.Path("/protected/install")}),
                    (acceptance, "installed_preflight", {}),
                    (acceptance, "inspect_live_parent", {"return_value": {
                        "m2_contract_compatible": True, "guest_opencode_version": "opencode v2.2.5",
                        "guest_assets": {"protected": "verified"}}}),
                    (acceptance, "_parent_json", {"side_effect": parent_json}),
                    (acceptance, "_parent_ok", {"side_effect": lambda _root, *args, **_kwargs: (
                        operations.append(("command", args[0])) or subprocess.CompletedProcess([], 0))}),
                    (acceptance, "_parent_command", {"side_effect": parent_command}),
                    (acceptance, "_replacement_provision_origin", {"return_value": {
                        "attempt_id": "20261116T220000Z-" + "4" * 32,
                        "factory_reset_attempt_id": "20261116T220000Z-" + "4" * 32,
                        "disk_identity": {"device": 1, "inode": 22}}}),
                    (acceptance, "_exercise_simultaneous_attach_and_resize", {"return_value": {
                        "session_count": 1, "resize": "103x41", "simultaneous_attach": True}}),
                    (acceptance, "_tui_key", {}),
                    (acceptance, "_wait_tui", {"side_effect": wait_tui}),
                    (acceptance, "_probe_history", {"side_effect": (
                        {"first_response_persisted": True, "session_id_sha256": "d" * 64},
                        {"first_response_persisted": True, "second_response_persisted": True,
                         "same_session": True, "conversation_ordered": True,
                         "session_id_sha256": "d" * 64})}),
                ):
                    stack.enter_context(mock.patch.object(target, name, **kwargs))
                stack.enter_context(mock.patch("builtins.input", side_effect=(
                    "YES", "xterm test profile", "CLIPBOARD_UNCHANGED",
                    f"STOP-AND-RESTART parent=default run={'a' * 32} disk=1:22")))
                result = acceptance.main([
                    "--parent-accept", "--milestone", "M2", "--sha", "a" * 40,
                    "--repo", str(ROOT), "--model", "openai/gpt-test",
                ])
            report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(result, 0, output.getvalue() + json.dumps(report, indent=2))
            self.assertEqual(report["status"], "PASS")
            self.assertTrue(all(value == "PASS" for value in report["gates"].values()))
            self.assertEqual(report["evidence"]["m1_revalidation"]["result"], "PASS")
            self.assertFalse(report["evidence"]["m1_revalidation"][
                "destructive_stale_boot_recovery_repeated"])
            self.assertIn("M2_OWNER_ACCEPTANCE_PASS", output.getvalue())
            names = [name for _kind, name in operations]
            self.assertGreater(names.index("term-check"), names.index("up"))
            serialized = json.dumps(report)
            self.assertNotIn("LM-CLIPBOARD-CANARY", serialized)
            self.assertNotIn("OAuth", serialized)

    def test_parent_acceptance_blocks_ambiguous_identity_without_lifecycle_actions(self):
        report_dir = pathlib.Path(tempfile.mkdtemp(prefix="owner-m2-ambiguous-"))
        try:
            status_reads = 0
            def parent_json(_root, *args, **_kwargs):
                nonlocal status_reads
                if args[0] == "term-check":
                    return None, {"status": "PASS", "terminfo_entry": True,
                                  "clear_capability": True, "clear_command": True}
                self.assertEqual(args, ("status", "--json"))
                status_reads += 1
                return None, {"status": "NOT_CREATED"}

            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(acceptance.sys, "stdin", TTYBuffer()))
                output = TTYBuffer()
                stack.enter_context(mock.patch.object(acceptance.sys, "stdout", output))
                stack.enter_context(mock.patch.dict(os.environ, {"LM_VM_TERM_VALIDATED": "1", "TERM": "xterm-256color"}))
                for target, name, kwargs in (
                    (acceptance, "new_report_dir", {"return_value": report_dir}),
                    (acceptance, "candidate_commit", {}),
                    (acceptance, "verify_install", {"return_value": pathlib.Path("/protected/install")}),
                    (acceptance, "installed_preflight", {}),
                    (acceptance, "_parent_json", {"side_effect": parent_json}),
                    (acceptance, "_parent_command", {"return_value": subprocess.CompletedProcess(
                        [], 2, b'{"status":"AMBIGUOUS"}', b"")}),
                ):
                    stack.enter_context(mock.patch.object(target, name, **kwargs))
                parent_ok = stack.enter_context(mock.patch.object(acceptance, "_parent_ok"))
                approval = stack.enter_context(mock.patch.object(acceptance, "_owner_approval",
                    side_effect=acceptance.GateError("BLOCKED", "fresh provisioning not approved in test")))
                result = acceptance.main([
                    "--parent-accept", "--milestone", "M2", "--sha", "a" * 40,
                    "--repo", str(ROOT), "--model", "openai/gpt-test",
                ])
            self.assertEqual(result, 3)
            parent_ok.assert_called_once()
            self.assertEqual(parent_ok.call_args.args[1], "factory-reset")
            self.assertIn("factory-reset", approval.call_args.args[0])
            report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "BLOCKED")
            self.assertIn("M2_OWNER_ACCEPTANCE_FAIL", output.getvalue())
        finally:
            import shutil
            shutil.rmtree(report_dir)

    def test_parent_acceptance_cancellation_is_reported_and_does_not_provision(self):
        with tempfile.TemporaryDirectory(prefix="owner-m2-cancel-") as temporary:
            report_dir = pathlib.Path(temporary)
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(acceptance.sys, "stdin", TTYBuffer()))
                output = TTYBuffer()
                stack.enter_context(mock.patch.object(acceptance.sys, "stdout", output))
                stack.enter_context(mock.patch.dict(os.environ, {"LM_VM_TERM_VALIDATED": "1", "TERM": "xterm-256color"}))
                for target, name, kwargs in (
                    (acceptance, "new_report_dir", {"return_value": report_dir}),
                    (acceptance, "candidate_commit", {}),
                    (acceptance, "verify_install", {"return_value": pathlib.Path("/protected/install")}),
                    (acceptance, "installed_preflight", {}),
                    (acceptance, "_parent_json", {"side_effect": lambda _root, *args, **_kwargs: (
                        (None, {"status": "PASS", "terminfo_entry": True,
                                "clear_capability": True, "clear_command": True})
                         if args[0] == "term-check" else
                        (None, {"status": "RETIRED", "retired_bundle": "retired-" + "c" * 32,
                                "retired_disk_identity": {"device": 1, "inode": 2}}))}),
                    (acceptance, "_parent_command", {"return_value": subprocess.CompletedProcess(
                        [], 0, b'{"status":"RETIRED"}', b"")}),
                ):
                    stack.enter_context(mock.patch.object(target, name, **kwargs))
                approve = stack.enter_context(mock.patch.object(acceptance, "_owner_approval",
                    side_effect=acceptance.GateError("BLOCKED", "owner approval cancelled")))
                parent_ok = stack.enter_context(mock.patch.object(acceptance, "_parent_ok"))
                result = acceptance.main([
                    "--parent-accept", "--milestone", "M2", "--sha", "a" * 40,
                    "--repo", str(ROOT), "--model", "openai/gpt-test",
                ])
            self.assertEqual(result, 3)
            approve.assert_called_once()
            parent_ok.assert_not_called()
            report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "BLOCKED")
            self.assertEqual(report["gates"]["fresh_provision"], "BLOCKED")
            self.assertIn("M2_OWNER_ACCEPTANCE_FAIL", output.getvalue())

    def test_simultaneous_attach_drives_two_clients_and_delivers_resize_signal(self):
        class FakeClient:
            def __init__(self):
                self.returncode = None
                self.signals = []

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.returncode = 0
                return 0

            def send_signal(self, value):
                self.signals.append(value)

            def terminate(self):
                self.returncode = -15

            def kill(self):
                self.returncode = -9

        clients = []

        def popen(*_args, **_kwargs):
            client = FakeClient()
            clients.append(client)
            return client

        states = (
            {"attached_clients": 1, "session_count": 1, "target_count": 1},
            {"attached_clients": 2, "session_count": 1, "target_count": 1},
            {"size": "107x43", "session_count": 1},
            {"attached_clients": 1, "session_count": 1},
            {"attached_clients": 0, "session_count": 1},
        )
        observed_sizes = []
        ioctl = acceptance.fcntl.ioctl

        def record_ioctl(fd, request, payload):
            if request == acceptance.termios.TIOCSWINSZ:
                observed_sizes.append(struct.unpack("HHHH", payload)[:2])
            return ioctl(fd, request, payload)

        with (
            mock.patch.object(acceptance.subprocess, "Popen", side_effect=popen),
            mock.patch.object(acceptance, "clean_env", return_value={}),
            mock.patch.object(acceptance, "_wait_tui", side_effect=states),
            mock.patch.object(acceptance.fcntl, "ioctl", side_effect=record_ioctl),
        ):
            result = acceptance._exercise_simultaneous_attach_and_resize(ROOT)
        self.assertEqual(result["session_count"], 1)
        self.assertTrue(result["simultaneous_attach"])
        self.assertEqual(len(clients), 2)
        self.assertEqual(clients[1].signals, [acceptance.signal.SIGWINCH])
        self.assertEqual(observed_sizes, [(37, 91), (41, 103), (43, 107)])

    def test_parent_acceptance_requires_explicit_source_repository(self):
        with self.assertRaises(SystemExit):
            acceptance.main([
                "--parent-accept", "--milestone", "M2", "--sha", "a" * 40,
            ])

    def test_changed_parent_asset_detection_is_full_sha_pinned_and_path_scoped(self):
        sha = "b" * 40
        expected = subprocess.CompletedProcess([], 0, b"lib/vm/guest-user-dirs.py\n", b"")
        with mock.patch.object(acceptance, "git", return_value=expected) as git:
            self.assertEqual(
                acceptance.changed_parent_guest_assets(ROOT, sha),
                ["lib/vm/guest-user-dirs.py"],
            )
        self.assertEqual(git.call_args.args[1:], (
            "diff", "--name-only", acceptance.M1_ACCEPTED_SHA, sha,
            "--", *acceptance.PARENT_GUEST_ASSETS,
        ))

        with (
            mock.patch.object(acceptance, "git", return_value=subprocess.CompletedProcess([], 0, b"lab-vm\n", b"")),
            self.assertRaises(acceptance.GateError),
        ):
            acceptance.changed_parent_guest_assets(ROOT, sha)

    def test_candidate_requires_exact_commit_and_matching_runner_bytes(self):
        with mock.patch.object(acceptance, "git") as git:
            with self.assertRaises(acceptance.GateError):
                acceptance.candidate_commit(ROOT, "not-a-full-sha")
            git.assert_not_called()

            sha = "a" * 40
            git.side_effect = [
                subprocess.CompletedProcess([], 0, (sha + "\n").encode(), b""),
                subprocess.CompletedProcess([], 0, ENTRY.read_bytes(), b""),
            ]
            acceptance.candidate_commit(ROOT, sha)
            self.assertEqual(git.call_count, 2)

    def test_candidate_commit_ignores_repository_replace_refs(self):
        with tempfile.TemporaryDirectory(prefix="owner-acceptance-replace-ref-") as temporary:
            repo = pathlib.Path(temporary)
            env = {**os.environ, **acceptance.clean_env()}

            def git(*args):
                return subprocess.run(
                    [acceptance.GIT, "-C", str(repo), *args], env=env,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, check=True,
                ).stdout.decode("ascii").strip()

            git("init", "-q")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            for name in acceptance.INSTALL_FILES:
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(ENTRY.read_bytes() if name == "tools/vm-owner-acceptance" else f"pinned:{name}\n".encode())
            runner_path = repo / "tools/vm-owner-acceptance"
            runner_path.parent.mkdir(parents=True, exist_ok=True)
            runner_path.write_bytes(ENTRY.read_bytes())
            git("add", "--all")
            git("commit", "-qm", "pinned candidate")
            pinned_sha = git("rev-parse", "HEAD")
            pinned_launcher = (repo / "lab-vm").read_bytes()

            (repo / "tools/vm-owner-acceptance").write_bytes(b"untrusted runner bytes\n")
            (repo / "lab-vm").write_bytes(b"untrusted launcher bytes\n")
            git("commit", "-qam", "replacement tree")
            replacement_sha = git("rev-parse", "HEAD")
            git("replace", pinned_sha, replacement_sha)

            # Model an entrypoint loaded from the replacement tree. A replace
            # ref must not make its bytes appear to belong to the pinned SHA.
            with mock.patch.object(acceptance, "protected_executable", side_effect=lambda _repo, path: path), \
                    mock.patch.object(acceptance, "ENTRY", repo / "tools/vm-owner-acceptance"):
                with self.assertRaises(acceptance.GateError):
                    acceptance.candidate_commit(repo, pinned_sha)

            archive = subprocess.run(
                acceptance.archive_arguments(repo, pinned_sha), env={
                    **acceptance.clean_env(),
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                }, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=True,
            ).stdout
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as pinned_archive:
                self.assertEqual(pinned_archive.extractfile("lab-vm").read(), pinned_launcher)

    def test_existing_installation_is_never_overwritten(self):
        sha = "a" * 40
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary) / sha
            target.mkdir()
            marker = target / "preserve"
            marker.write_text("historical", encoding="utf-8")
            with mock.patch.object(acceptance, "INSTALL_PARENT", pathlib.Path(temporary)):
                with self.assertRaises(acceptance.GateError):
                    acceptance.install_candidate(ROOT, sha)
            self.assertEqual(marker.read_text(encoding="utf-8"), "historical")

    def test_install_archive_contains_exactly_the_worker_allowlisted_runtime(self):
        head = subprocess.run(
            [acceptance.GIT, "-C", str(ROOT), "rev-parse", "HEAD"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=acceptance.clean_env(),
        ).stdout.decode("ascii").strip()
        not_yet_committed = []
        for relative in acceptance.INSTALL_FILES:
            present = subprocess.run(
                [acceptance.GIT, "-C", str(ROOT), "cat-file", "-e", f"{head}:{relative}"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=acceptance.clean_env(), check=False)
            if present.returncode:
                not_yet_committed.append(relative)
        if not_yet_committed:
            self.skipTest("archive exactness requires the new manifest paths to be committed; "
                          "uncommitted paths: " + ", ".join(sorted(not_yet_committed)))
        result = subprocess.run(
            acceptance.archive_arguments(ROOT, head), check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**acceptance.clean_env(), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
        )
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
            names = {member.name.rstrip("/") for member in archive.getmembers()}
        manifest = json.loads(acceptance.installer_manifest())
        expected = set(manifest["files"]) | set(manifest["directories"])
        self.assertEqual(names, expected)
        self.assertTrue({
            "lib/vm/parent-state.py", "lib/vm/parent-launcher.py",
            "lib/vm/parent-gate.py", "lib/vm/parent-supervisor.py",
            "lib/vm/parent-qemu.py", "lib/vm/parent-provision.py",
            "lib/vm/parent-session-contract.py",
            "scripts/parent-readonly-safety-gate.py",
            "agent/opencode/plugins/little-mester-exit-confirm/package.json",
            "agent/opencode/plugins/little-mester-exit-confirm/tui.tsx",
        }.issubset(manifest["files"]))
        self.assertEqual(set(acceptance.archive_arguments(ROOT, head)[6:]), set(manifest["files"]))
        self.assertNotIn(".github", names)
        self.assertIn('json.loads(sys.argv[2])', acceptance.INSTALL_WORKER)
        worker = acceptance.INSTALL_WORKER
        substitutions = (
            ('or os.geteuid() != 0', 'or False'),
            ('fd = os.open("/", flags)', 'fd = os.open(sys.argv[4], flags)'),
            ('if root.st_uid != 0 or root.st_mode & (stat.S_IWGRP | stat.S_IWOTH):', 'if False:'),
            ('or info.st_uid != 0', 'or False'),
            ('os.fchown(child, 0, 0)', 'pass'),
            ('os.fchown(target, 0, 0)', 'pass'),
            ('os.fchown(next_fd, 0, 0)', 'pass'),
            ('os.fchown(stream.fileno(), 0, 0)', 'pass'),
        )
        for original, replacement in substitutions:
            self.assertIn(original, worker)
            worker = worker.replace(original, replacement, 1)
        with tempfile.TemporaryDirectory() as temporary:
            for prefix in ("/prefix-one", "/prefix-two"):
                with self.subTest(prefix=prefix):
                    installed = subprocess.run(
                        [sys.executable, "-I", "-c", worker, head,
                         acceptance.installer_manifest(), prefix, temporary],
                        input=result.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                    )
                    self.assertEqual(installed.returncode, 0,
                                     installed.stderr.decode("utf-8", "replace"))
                    self.assertEqual(installed.stdout, b"PINNED_INSTALL_CREATED\n")
                    target = pathlib.Path(temporary) / prefix.lstrip("/") / "v3" / head
                    observed = {path.relative_to(target).as_posix() for path in target.rglob("*")}
                    self.assertEqual(observed, expected)
                    for name, mode in manifest["files"].items():
                        self.assertEqual(stat.S_IMODE(os.lstat(target / name).st_mode), mode)
                    if prefix == "/prefix-one":
                        helper = target / "scripts/parent-readonly-safety-gate.py"
                        self.assertTrue(helper.is_file())
                        installed_state_spec = importlib.util.spec_from_file_location(
                            "installed_parent_state", target / "lib/vm/parent-state.py")
                        installed_state = importlib.util.module_from_spec(installed_state_spec)
                        installed_state_spec.loader.exec_module(installed_state)
                        scanner_calls = []

                        real_lstat = pathlib.Path.lstat

                        def protected_lstat(path):
                            path = pathlib.Path(path)
                            if path == helper:
                                return SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o644)
                            if path == target or target in path.parents:
                                return SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755)
                            return real_lstat(path)

                        def scanner_run(argv, **_kwargs):
                            scanner_calls.append(argv)
                            if argv[1:2] == ["-v"]:
                                return subprocess.CompletedProcess(argv, 0)
                            owner_pid = int(argv[-2])
                            holders = [[os.getpid(), "fd", 1]] if owner_pid == 0 else []
                            return subprocess.CompletedProcess(
                                argv, 0, json.dumps({"complete": True, "processes_scanned": 2,
                                                     "coverage": {"procfs": True,
                                                                  "pid_namespace": "init-shared",
                                                                  "user_namespace": "init-shared",
                                                                  "descriptors": True,
                                                                  "file_mappings": True},
                                                     "holders": holders}).encode(), b"")

                        with (tempfile.TemporaryFile() as disk,
                              mock.patch.object(pathlib.Path, "lstat", protected_lstat),
                              mock.patch.object(installed_state.subprocess, "run",
                                                side_effect=scanner_run)):
                            identity = os.fstat(disk.fileno())
                            installed_state._scan_stale_disk_holders(
                                disk.fileno(), (identity.st_dev, identity.st_ino))
                        self.assertEqual(len(scanner_calls), 3)
                        self.assertEqual(pathlib.Path(scanner_calls[1][5]), helper)
                        self.assertEqual(scanner_calls[-1][-4:-2],
                                         [str(identity.st_dev), str(identity.st_ino)])
                        helper.unlink()

                        def missing_helper_lstat(path):
                            path = pathlib.Path(path)
                            if path == helper:
                                return real_lstat(path)
                            if path == target or target in path.parents:
                                return SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755)
                            return real_lstat(path)

                        with mock.patch.object(pathlib.Path, "lstat", missing_helper_lstat):
                            with tempfile.TemporaryFile() as disk:
                                info = os.fstat(disk.fileno())
                                with self.assertRaisesRegex(
                                        installed_state.Refused, "privileged disk-holder verification failed"):
                                    installed_state._scan_stale_disk_holders(
                                        disk.fileno(), (info.st_dev, info.st_ino))

    def test_default_operation_is_read_only_preflight_and_reports_blocked_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = pathlib.Path(temporary)
            report_dir = parent / "attempt"
            report_dir.mkdir(mode=0o700)
            with (
                mock.patch.object(acceptance.pathlib.Path, "resolve", autospec=True,
                                  side_effect=lambda path, *a, **kw: pathlib.Path(path)),
                mock.patch.object(acceptance, "new_report_dir", return_value=report_dir),
                mock.patch.object(acceptance, "candidate_commit"),
                mock.patch.object(acceptance, "install_path", return_value=parent / "missing-install"),
                mock.patch.object(acceptance, "install_candidate") as install,
                mock.patch.object(acceptance, "installed_preflight") as preflight,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = acceptance.main(["--sha", "a" * 40])
            install.assert_not_called()
            preflight.assert_not_called()
            self.assertEqual(result, 3)
            report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "BLOCKED")
            self.assertEqual(report["gates"]["candidate"], "PASS")
            self.assertEqual(report["gates"]["preflight"], "BLOCKED")
            self.assertEqual(report["gates"]["install"], "NOT_RUN")
            self.assertEqual(report["gates"]["run"], "NOT_RUN")

    def test_each_report_directory_and_report_file_is_exclusive(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = pathlib.Path(temporary)
            first = acceptance.new_report_dir(parent)
            second = acceptance.new_report_dir(parent)
            self.assertNotEqual(first, second)
            self.assertEqual(stat.S_IMODE(os.lstat(first).st_mode), 0o700)
            data = acceptance.report_template("a" * 40, "preflight", first)
            acceptance.save_report(first, data)
            report = first / "report.json"
            self.assertEqual(stat.S_IMODE(os.lstat(report).st_mode), 0o600)
            self.assertEqual(json.loads(report.read_text())["status"], "IN_PROGRESS")
            with self.assertRaises(FileExistsError):
                acceptance.save_report(first, data)
            self.assertNotIn("authorization_url", report.read_text())

    def test_storage_proof_bounds_attempt_to_capacity_plus_one_and_cleans_only_own_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = pathlib.Path(temporary)
            os.chmod(workspace, 0o700)
            real_fdopen = os.fdopen

            class FullFilesystem:
                def __init__(self, stream):
                    self.stream = stream
                    self.written = 0
                    self.attempts = []

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    self.stream.close()

                def write(self, data):
                    self.attempts.append(len(data))
                    if self.written + len(data) > 8:
                        raise OSError(28, "No space left on device")
                    count = self.stream.write(data)
                    self.written += count
                    return count

            writers = []
            anonymous_files = []

            def fdopen(*args, **kwargs):
                writer = FullFilesystem(real_fdopen(*args, **kwargs))
                writers.append(writer)
                return writer

            usage = SimpleNamespace(f_blocks=8, f_frsize=1)
            with (
                mock.patch.object(acceptance, "WORKSPACE_PROOF_BYTES", 8),
                mock.patch.object(acceptance, "open_anonymous_probe", side_effect=lambda directory: (
                    anonymous_files.append(tempfile.TemporaryFile(dir=directory))
                    or os.dup(anonymous_files[-1].fileno())
                )),
                mock.patch.object(acceptance, "mount_facts", return_value=(
                    "tmpfs", {"nosuid", "nodev", "noexec"},
                    {"mode=700", f"uid={os.getuid()}", f"gid={os.getgid()}", "size=8k"},
                )),
                mock.patch.object(acceptance.os, "statvfs", return_value=usage),
                mock.patch.object(acceptance.os, "fdopen", side_effect=fdopen),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                acceptance.storage_proof(str(workspace))

            self.assertEqual(output.getvalue(), "ENOSPC_CONFIRMED after 67108864 bytes; 67108865 bytes attempted at most\n")
            self.assertEqual(writers[0].written, 8)
            self.assertEqual(writers[0].attempts, [8, 1])
            self.assertEqual(list(workspace.iterdir()), [])
            for anonymous in anonymous_files:
                anonymous.close()

    def test_storage_proof_refuses_non_tmpfs_and_never_fills_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = pathlib.Path(temporary)
            os.chmod(workspace, 0o700)
            usage = SimpleNamespace(f_blocks=64 * 1024 * 1024, f_frsize=1)
            with (
                mock.patch.object(acceptance, "mount_facts", return_value=("ext4", set(), set())),
                mock.patch.object(acceptance.os, "statvfs", return_value=usage),
            ):
                with self.assertRaises(acceptance.GateError):
                    acceptance.storage_proof(str(workspace))
            self.assertEqual(list(workspace.iterdir()), [])

    def test_smoke_runs_without_tty_and_inherits_standard_streams(self):
        class NotTTY(io.StringIO):
            def isatty(self):
                return False

        sha = "b" * 40
        root = pathlib.Path("/protected/install")
        data = acceptance.report_template(sha, "run", pathlib.Path("/private/report"))
        state = pathlib.Path("/private/state")
        with (
            mock.patch.object(acceptance.sys, "stdin", NotTTY()),
            mock.patch.object(acceptance.sys, "stdout", NotTTY()),
            mock.patch.object(acceptance, "verify_workspace_mount", return_value=pathlib.Path("/workspace")),
            mock.patch.object(acceptance.pathlib.Path, "iterdir", return_value=iter(())),
            mock.patch.object(acceptance, "state_root", return_value=state),
            mock.patch.object(acceptance, "list_runs", return_value={"20261109T154005Z-" + "a" * 32}),
            mock.patch.object(acceptance, "inspect_cleanup", return_value={"status": "PASS"}),
            mock.patch.object(acceptance.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run,
        ):
            acceptance.run_smoke(root, "/workspace", "openai/model", set(), data, debug=True)
        args, kwargs = run.call_args
        self.assertEqual(args[0], [str(root / "lab-vm"), "smoke", "--debug", "--workspace", "/workspace", "--model", "openai/model"])
        self.assertIsNone(kwargs["stdin"])
        self.assertIsNone(kwargs["stdout"])
        self.assertIsNone(kwargs["stderr"])
        self.assertEqual(data["gates"]["run"], "PASS")
        self.assertEqual(data["gates"]["cleanup"], "PASS")

    def test_clean_env_preserves_only_validated_owner_session_bus(self):
        uid = 12345
        runtime = pathlib.Path(f"/run/user/{uid}")
        bus = runtime / "bus"

        def fake_stat(path, *args, **kwargs):
            if path == runtime:
                return SimpleNamespace(st_uid=uid, st_mode=stat.S_IFDIR | 0o700)
            if path == bus:
                return SimpleNamespace(st_uid=uid, st_mode=stat.S_IFSOCK | 0o600)
            raise FileNotFoundError

        with mock.patch.object(acceptance.os, "getuid", return_value=uid), \
                mock.patch.object(pathlib.Path, "stat", fake_stat):
            env = acceptance.clean_env(desktop_session=True)
            ordinary_env = acceptance.clean_env()
        self.assertEqual(env["XDG_RUNTIME_DIR"], str(runtime))
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], f"unix:path={bus}")
        self.assertNotIn("XDG_RUNTIME_DIR", ordinary_env)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", ordinary_env)
        self.assertNotIn("DISPLAY", env)
        self.assertNotIn("WAYLAND_DISPLAY", env)

    def test_nonzero_smoke_is_blocked_and_never_claims_cleanup(self):
        class TTY(io.StringIO):
            def isatty(self):
                return True

        run_id = "20261109T154005Z-" + "c" * 32
        data = acceptance.report_template("c" * 40, "run", pathlib.Path("/private/report"))
        with (
            mock.patch.object(acceptance.sys, "stdin", TTY()),
            mock.patch.object(acceptance.sys, "stdout", TTY()),
            mock.patch.object(acceptance, "verify_workspace_mount", return_value=pathlib.Path("/workspace")),
            mock.patch.object(acceptance.pathlib.Path, "iterdir", return_value=iter(())),
            mock.patch.object(acceptance, "state_root", return_value=pathlib.Path("/private/state")),
            mock.patch.object(acceptance, "list_runs", return_value={run_id}),
            mock.patch.object(acceptance.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)),
        ):
            with self.assertRaises(acceptance.GateError):
                acceptance.run_smoke(pathlib.Path("/protected/install"), "/workspace", "openai/model", set(), data)
        self.assertEqual(data["gates"]["run"], "BLOCKED")
        self.assertEqual(data["gates"]["cleanup"], "NOT_RUN")
        self.assertEqual(data["evidence"]["smoke_exit_code"], 1)

    def test_retained_stale_pid_is_never_authority_and_historical_files_are_not_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_id = "20261109T154005Z-" + "d" * 32
            run_dir = pathlib.Path(temporary) / run_id
            run_dir.mkdir(mode=0o700)
            for name in ("qemu.log", "virtiofsd.log", "proxy.log"):
                (run_dir / name).touch(mode=0o600)
            (run_dir / "virtiofs.socket-path").write_text("/run/user/1000/lmv3-test/virtiofs.sock\n", encoding="utf-8")
            os.chmod(run_dir / "virtiofs.socket-path", 0o600)
            child = subprocess.Popen(["/usr/bin/sleep", "30"])
            with mock.patch.object(acceptance, "state_root", return_value=pathlib.Path(temporary)), \
                     mock.patch.object(acceptance, "verify_user_directory_chain"), \
                     mock.patch.object(acceptance, "inspect_virtiofs_artifacts", return_value={
                         "socket_present": True, "pid_present": True, "runtime_directory": "present"
                     }):
                result = acceptance.inspect_cleanup("e" * 40, run_id, pathlib.Path("/protected/install"))
            self.assertEqual(result["status"], "PASS")
            self.assertTrue((run_dir / "virtiofs.socket-path").exists())
            self.assertTrue(any("PID artifact" in artifact for artifact in result["residual_artifacts"]))
            self.assertIsNone(child.poll())
            child.terminate()
            child.wait(timeout=5)

    def test_qemu_debug_stop_is_not_an_allowed_retained_acceptance_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_id = "20261109T154005Z-" + "f" * 32
            run_dir = pathlib.Path(temporary) / run_id
            run_dir.mkdir(mode=0o700)
            for name in ("qemu.log", "virtiofsd.log", "proxy.log", "virtiofs.socket-path"):
                (run_dir / name).touch(mode=0o600)
            (run_dir / "qemu.debug-stop").touch(mode=0o600)
            with mock.patch.object(acceptance, "state_root", return_value=pathlib.Path(temporary)), \
                    mock.patch.object(acceptance, "verify_user_directory_chain"):
                with self.assertRaisesRegex(acceptance.GateError, "unexpected retained artifacts"):
                    acceptance.inspect_cleanup("a" * 40, run_id, pathlib.Path("/protected/install"))

    def test_live_socket_listener_blocks_readonly_cleanup_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_id = "20261109T154005Z-" + "e" * 32
            run_dir = pathlib.Path(temporary) / run_id
            run_dir.mkdir(mode=0o700)
            for name in ("qemu.log", "virtiofsd.log", "proxy.log"):
                (run_dir / name).touch(mode=0o600)
            (run_dir / "virtiofs.socket-path").touch(mode=0o600)
            with mock.patch.object(acceptance, "state_root", return_value=pathlib.Path(temporary)), \
                    mock.patch.object(acceptance, "verify_user_directory_chain"), \
                    mock.patch.object(acceptance, "inspect_virtiofs_artifacts", side_effect=acceptance.GateError("BLOCKED", "listener remains")):
                with self.assertRaises(acceptance.GateError):
                    acceptance.inspect_cleanup("f" * 40, run_id, pathlib.Path("/protected/install"))

    def test_early_startup_without_qemu_log_is_not_cleanup_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_id = "20261109T154005Z-" + "1" * 32
            run_dir = pathlib.Path(temporary) / run_id
            run_dir.mkdir(mode=0o700)
            for name in ("virtiofsd.log", "proxy.log", "virtiofs.socket-path"):
                (run_dir / name).touch(mode=0o600)
            with mock.patch.object(acceptance, "state_root", return_value=pathlib.Path(temporary)), \
                    mock.patch.object(acceptance, "verify_user_directory_chain"):
                with self.assertRaisesRegex(acceptance.GateError, "early startup failure"):
                    acceptance.inspect_cleanup("1" * 40, run_id, pathlib.Path("/protected/install"))

    def test_cleanup_report_requires_completed_run_and_cleanup_gates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            report_dir = root / "report"
            report_dir.mkdir(mode=0o700)
            report = report_dir / "report.json"
            data = acceptance.report_template("2" * 40, "run", report_dir)
            data["evidence"]["vm_run_id"] = "20261109T154005Z-" + "2" * 32
            data["gates"]["run"] = "BLOCKED"
            data["gates"]["cleanup"] = "NOT_RUN"
            report.write_text(json.dumps(data), encoding="utf-8")
            os.chmod(report, 0o600)
            with mock.patch.object(acceptance, "runtime_dir", return_value=root):
                with self.assertRaisesRegex(acceptance.GateError, "early startup failure"):
                    acceptance.verify_cleanup_report("2" * 40, data["evidence"]["vm_run_id"])

    def test_successful_verify_cleanup_reports_overall_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            report_dir = root / "report"
            run_id = "20261109T154005Z-" + "3" * 32

            def allocate_report_dir():
                report_dir.mkdir(mode=0o700)
                return report_dir

            with (
                mock.patch.object(acceptance, "new_report_dir", side_effect=allocate_report_dir),
                mock.patch.object(acceptance, "candidate_commit"),
                mock.patch.object(acceptance, "verify_cleanup_report"),
                mock.patch.object(acceptance, "verify_install", return_value=root / "install"),
                mock.patch.object(acceptance, "inspect_cleanup", return_value={"status": "PASS"}),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                result = acceptance.main([
                    "--verify-cleanup", "--sha", "3" * 40,
                    "--repo", str(ROOT), "--run-id", run_id,
                ])
            self.assertEqual(result, 0)
            self.assertIn("OVERALL: PASS", output.getvalue())
            final_report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(final_report["status"], "PASS")
            self.assertEqual(final_report["gates"]["cleanup"], "PASS")

    def test_proc_unix_listener_path_preserves_repeated_spaces(self):
        import socket
        helper_loader = importlib.machinery.SourceFileLoader(
            "virtiofs_socket_helper", str(ROOT / "lib/vm/virtiofs-socket.py")
        )
        helper_spec = importlib.util.spec_from_loader(helper_loader.name, helper_loader)
        helper = importlib.util.module_from_spec(helper_spec)
        helper_loader.exec_module(helper)
        with tempfile.TemporaryDirectory(prefix="vm owner  ") as temporary:
            path = pathlib.Path(temporary) / "virtiofs  socket.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(path))
                self.assertTrue(helper._listener_present(str(path)))
            finally:
                listener.close()

    def test_cleanup_verification_blocks_unexpected_retained_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_id = "20261109T154005Z-" + "f" * 32
            run_dir = pathlib.Path(temporary) / run_id
            run_dir.mkdir(mode=0o700)
            for name in ("qemu.log", "virtiofsd.log", "proxy.log"):
                (run_dir / name).touch(mode=0o600)
            (run_dir / "virtiofs.socket-path").touch(mode=0o600)
            (run_dir / "oauth-url.txt").write_text("unexpected", encoding="utf-8")
            with mock.patch.object(acceptance, "state_root", return_value=pathlib.Path(temporary)), \
                    mock.patch.object(acceptance, "verify_user_directory_chain"):
                with self.assertRaises(acceptance.GateError):
                    acceptance.inspect_cleanup("a" * 40, run_id, pathlib.Path("/protected/install"))
            self.assertTrue((run_dir / "oauth-url.txt").exists())


if __name__ == "__main__":
    unittest.main()
