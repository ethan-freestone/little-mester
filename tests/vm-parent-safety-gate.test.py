#!/usr/bin/env python3
"""Focused tests for the read-only parent safety-gate process scanner."""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
OWNER_ACCEPTANCE_PATH = ROOT / "tools/vm-owner-acceptance"
OWNER_ACCEPTANCE_LOADER = importlib.machinery.SourceFileLoader(
    "scanner_fixture_owner_acceptance", str(OWNER_ACCEPTANCE_PATH))
OWNER_ACCEPTANCE_SPEC = importlib.util.spec_from_loader(
    OWNER_ACCEPTANCE_LOADER.name, OWNER_ACCEPTANCE_LOADER)
owner_acceptance = importlib.util.module_from_spec(OWNER_ACCEPTANCE_SPEC)
OWNER_ACCEPTANCE_LOADER.exec_module(owner_acceptance)
SPEC = importlib.util.spec_from_file_location(
    "parent_readonly_safety_gate", ROOT / "scripts/parent-readonly-safety-gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
DIAG_SPEC = importlib.util.spec_from_file_location(
    "parent_ssh_endpoint_diagnostic", ROOT / "scripts/parent-ssh-endpoint-diagnostic.py")
diagnostic = importlib.util.module_from_spec(DIAG_SPEC)
DIAG_SPEC.loader.exec_module(diagnostic)


SAFE_SCANNER_REFUSALS = frozenset({
    "insufficient_privilege_or_invalid_identity", "invalid_caller_fd_exemption",
    "procfs_unavailable", "procfs_restricted", "pid_namespace_restricted",
    "user_namespace_restricted", "procfs_visibility_failed", "procfs_incomplete",
    "procfs_enumeration_failed", "malformed_process_identity",
    "incomplete_task_enumeration", "incomplete_fd_enumeration",
    "unreadable_process_stat", "unreadable_task_enumeration",
    "unreadable_fd_enumeration", "unreadable_fd_target_stat",
    "descriptor_changed_during_observation", "unreadable_task_maps",
    "incomplete_mapping_observation", "malformed_mapping_identity",
    "malformed_mapping_observation", "incomplete_procfs_scan",
    "task_enumeration_limit_exceeded", "descriptor_enumeration_limit_exceeded",
    "mapping_output_limit_exceeded", "process_changed_during_scan",
    "incomplete_live_process_observation", "unreadable_live_process",
    "unreadable_process_post_stat", "caller_changed_during_scan",
    "protected_helper_unavailable", "protected_helper_path_untrusted",
})
SAFE_RETRY_DIAGNOSTIC = re.compile(
    r"DIAGNOSTIC: phase=fd_target_retry classification=fd_slot_reappeared "
    r"retry_identity=(matches_requested|differs_from_requested)")
SAFE_CHILD_STATES = frozenset({"yes", "no", "unknown"})
SAFE_EXECUTION_STATES = frozenset({"not_started", "completed", "timed_out", "launch_failed"})
SAFE_PRIVILEGE_STATES = frozenset({"not_started", "confirmed", "unconfirmed"})
SAFE_RESULT_CATEGORIES = frozenset({
    "map_holder_for_child", "map_holder_missing", "scanner_refused",
    "scanner_result_malformed", "scanner_result_incomplete", "scanner_output_oversized",
    "scanner_stderr", "scan_timed_out", "scan_launch_failed", "fixture_precondition_failed",
})


def _safe_scanner_stderr(stderr):
    """Keep only scanner refusal and fixed diagnostic enums; redact everything else."""
    if isinstance(stderr, bytes):
        text = stderr[:4096].decode("utf-8", "replace")
    elif isinstance(stderr, str):
        text = stderr[:4096]
    else:
        text = ""
    safe_lines = []
    for line in text.splitlines():
        if line.startswith("REFUSED: "):
            reason = line.removeprefix("REFUSED: ")
            if reason in SAFE_SCANNER_REFUSALS:
                safe_lines.append(f"REFUSED: {reason}")
        elif SAFE_RETRY_DIAGNOSTIC.fullmatch(line):
            safe_lines.append(line)
    return " | ".join(safe_lines)[:256] or "redacted_or_unrecognized"


def _safe_scanner_reason(stderr):
    summary = _safe_scanner_stderr(stderr)
    for item in summary.split(" | "):
        if item.startswith("REFUSED: "):
            return item.removeprefix("REFUSED: ")
    return "none_or_redacted"


def _mapping_scan_failure_context(*, execution, privilege, return_code, stderr, elapsed_ms,
                                  before, after, expected, observed, assertion):
    """Build bounded test diagnostics from allowlisted states only."""
    execution = execution if execution in SAFE_EXECUTION_STATES else "not_started"
    privilege = privilege if privilege in SAFE_PRIVILEGE_STATES else "unconfirmed"
    expected = expected if expected in SAFE_RESULT_CATEGORIES else "other"
    observed = observed if observed in SAFE_RESULT_CATEGORIES else "other"
    assertion = (assertion if assertion in {
        "assertEqual(observed,map_holder_for_child)",
        "fixture map present and original fd closed"}
                 else "test assertion failed")
    def child_state(value):
        return value if value in SAFE_CHILD_STATES else "unknown"

    def descriptor_state(value):
        return value if value in {"closed", "open", "unknown"} else "unknown"

    code = str(return_code) if type(return_code) is int else "none"
    elapsed = max(0, min(60000, int(elapsed_ms)))
    report = (
        "operation=sudo_noninteractive_protected_scanner_scan_inode "
        f"privileged_execution={privilege} execution={execution} "
        f"reason={_safe_scanner_reason(stderr)} return_code={code} elapsed_ms={elapsed} "
        f"child_before[alive={child_state(before.get('alive'))},"
        f"mapping={child_state(before.get('mapping'))},"
        f"original_fd={descriptor_state(before.get('original_fd'))}] "
        f"child_after[alive={child_state(after.get('alive'))},"
        f"mapping={child_state(after.get('mapping'))},"
        f"original_fd={descriptor_state(after.get('original_fd'))}] "
        f"expected={expected} observed={observed} assertion={assertion} "
        f"stderr={_safe_scanner_stderr(stderr)}")
    return report[:1024]


class ScannerFailureDiagnosticUnitTests(unittest.TestCase):
    def test_simulated_descriptor_refusal_reports_only_allowlisted_retry_classification(self):
        for matches, retry_identity in ((True, "matches_requested"),
                                        (False, "differs_from_requested")):
            with self.subTest(retry_identity=retry_identity):
                error = gate.DescriptorChangedDuringObservation(matches)
                output, errors = io.StringIO(), io.StringIO()
                with (mock.patch.object(sys, "argv", ["scanner", "--scan-inode",
                                                        "11", "22", "0", "-1"]),
                      mock.patch.object(gate, "_scan_proc_inode", side_effect=error),
                      contextlib.redirect_stdout(output),
                      contextlib.redirect_stderr(errors)):
                    code = gate.main()
                self.assertEqual(code, 2)
                self.assertEqual(output.getvalue(), "")
                expected = (
                    "REFUSED: descriptor_changed_during_observation\n"
                    "DIAGNOSTIC: phase=fd_target_retry classification=fd_slot_reappeared "
                    f"retry_identity={retry_identity}\n"
                    "SAFETY GATE: FAIL — no disk attachment authorized\n")
                self.assertEqual(errors.getvalue(), expected)

    def test_simulated_failure_context_redacts_unallowlisted_stderr(self):
        stderr = (
            b"REFUSED: descriptor_changed_during_observation\n"
            b"DIAGNOSTIC: phase=fd_target_retry classification=fd_slot_reappeared "
            b"retry_identity=matches_requested\n"
            b"/private/path SUDO_UID=314159 credential=secret pid=2718 fd=23 "
            b"device=88 inode=9911\n")
        before = {"alive": "yes", "mapping": "yes", "original_fd": "closed"}
        after = {"alive": "yes", "mapping": "yes", "original_fd": "closed"}
        context = _mapping_scan_failure_context(
            execution="completed", privilege="confirmed", return_code=2,
            stderr=stderr, elapsed_ms=41, before=before, after=after,
            expected="map_holder_for_child", observed="scanner_refused",
            assertion="assertEqual(observed,map_holder_for_child)")
        self.assertIn("reason=descriptor_changed_during_observation", context)
        self.assertIn("retry_identity=matches_requested", context)
        self.assertIn("return_code=2 elapsed_ms=41", context)
        self.assertIn("child_before[alive=yes,mapping=yes,original_fd=closed]", context)
        self.assertIn("child_after[alive=yes,mapping=yes,original_fd=closed]", context)
        self.assertIn("expected=map_holder_for_child observed=scanner_refused", context)
        for secret in ("/private/path", "SUDO_UID", "credential", "secret", "2718",
                       "314159", "device=88", "inode=9911", "fd=23"):
            self.assertNotIn(secret, context)
        self.assertLessEqual(len(context), 1024)

    def test_unprivileged_scanner_refuses_before_procfs_observation(self):
        with mock.patch.object(gate.os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(
                    gate.GateRefused, "insufficient_privilege_or_invalid_identity"):
                gate._scan_proc_inode(1, 1)

    def test_failure_context_does_not_misreport_unprivileged_scanner_as_root(self):
        run = {"execution": "completed", "return_code": 2,
               "stderr": b"REFUSED: insufficient_privilege_or_invalid_identity\n"}
        self.assertEqual(ProcessScanTests._privilege_status(run, "scanner_refused"),
                         "unconfirmed")
        run["stderr"] = b"REFUSED: descriptor_changed_during_observation\n"
        self.assertEqual(ProcessScanTests._privilege_status(run, "scanner_refused"),
                         "confirmed")

    def test_target_snapshot_reports_only_alive_mapping_and_closed_states(self):
        with tempfile.NamedTemporaryFile() as held:
            held.write(b"x" * 4096)
            held.flush()
            info = os.fstat(held.fileno())
            code = (
                "import mmap,os,signal,sys\n"
                "f=os.open(sys.argv[1],os.O_RDONLY)\n"
                "m=mmap.mmap(f,0,access=mmap.ACCESS_READ)\n"
                "os.close(f)\n"
                "try:\n    os.fstat(f)\n"
                "except OSError:\n    pass\n"
                "else:\n    raise AssertionError('original mapping fd remained open')\n"
                "print('READY',f,flush=True)\n"
                "signal.pause()\n")
            child = subprocess.Popen([sys.executable, "-c", code, held.name],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, start_new_session=True)
            try:
                ready = child.stdout.readline().split()
                self.assertEqual(len(ready), 2)
                self.assertEqual(ready[0], "READY")
                original_fd = int(ready[1])
                before = ProcessScanTests._target_child_state(child.pid, original_fd, info)
                after = ProcessScanTests._target_child_state(child.pid, original_fd, info)
                expected = {"alive": "yes", "mapping": "yes", "original_fd": "closed"}
                self.assertEqual(before, expected)
                self.assertEqual(after, expected)
            finally:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                child.wait(timeout=5)
                child.stdout.close()
                child.stderr.close()


class ScannerPermissionCategoryUnitTests(unittest.TestCase):
    def _proc_read_text(self, path, *args, **kwargs):
        if os.fspath(path) == "/proc/self/mountinfo":
            return "1 0 0:1 / /proc rw - proc proc rw\n"
        if os.fspath(path) == "/proc/1/stat":
            return "1 (init) S " + " ".join(["0"] * 18 + ["123"])
        return self.real_read_text(path, *args, **kwargs)

    def setUp(self):
        self.real_read_text = pathlib.Path.read_text

    def _mock_proc_read_text(self):
        return mock.patch.object(pathlib.Path, "read_text", autospec=True,
                                 side_effect=self._proc_read_text)

    def _stat_with_denied_proc_fd_stat(self, held, before_fallback=None):
        path = pathlib.Path(f"/proc/self/fd/{held.fileno()}")
        real_stat = gate.os.stat
        real_open = gate.os.open

        def denied_stat(candidate, *args, **kwargs):
            if os.fspath(candidate) == os.fspath(path):
                if before_fallback is not None:
                    before_fallback(real_open, path, args, kwargs)
                raise PermissionError(13, "injected target-stat denial")
            return real_stat(candidate, *args, **kwargs)

        with mock.patch.object(gate.os, "stat", side_effect=denied_stat):
            return gate._stat_fd_target(path)

    def test_o_path_fallback_pins_real_proc_fd_target(self):
        with tempfile.NamedTemporaryFile() as held:
            expected = os.fstat(held.fileno())
            actual = self._stat_with_denied_proc_fd_stat(held)
        self.assertEqual((actual.st_dev, actual.st_ino), (expected.st_dev, expected.st_ino))

    def test_o_path_fallback_identifies_pipe_fd_without_opening_data(self):
        read_fd, write_fd = os.pipe()
        try:
            with os.fdopen(read_fd, "rb") as held:
                expected = os.fstat(held.fileno())
                actual = self._stat_with_denied_proc_fd_stat(held)
            self.assertEqual((actual.st_dev, actual.st_ino),
                             (expected.st_dev, expected.st_ino))
        finally:
            os.close(write_fd)

    def test_full_scanner_uses_o_path_fallback_for_real_proc_fd(self):
        with tempfile.NamedTemporaryFile() as held:
            process = os.getpid()
            held_fd = held.fileno()
            held_info = os.fstat(held_fd)
            device, inode = held_info.st_dev, held_info.st_ino
            denied = [False]
            real_scandir = gate.os.scandir
            real_stat = gate.os.stat
            real_read_text = pathlib.Path.read_text

            class Entry:
                def __init__(self, name):
                    self.name = name

            class Entries:
                def __init__(self, entries):
                    self.entries = entries

                def __iter__(self):
                    return iter(self.entries)

            def fake_scandir(path):
                if os.fspath(path) == "/proc":
                    return Entries([Entry("1"), Entry(str(process))])
                return real_scandir(path)

            def deny_once_for_target(path, *args, **kwargs):
                text = os.fspath(path)
                match = re.fullmatch(r"/proc/(\d+)/task/(\d+)/fd/(\d+)", text)
                if text == "/proc/1":
                    raise FileNotFoundError(2, "synthetic PID disappeared")
                if (match and int(match.group(1)) == process
                        and int(match.group(3)) == held_fd and not denied[0]):
                    denied[0] = True
                    raise PermissionError(13, "injected target-stat denial")
                return real_stat(path, *args, **kwargs)

            def fake_read_text(path, *args, **kwargs):
                if os.fspath(path) == "/proc/1/stat":
                    raise FileNotFoundError(2, "synthetic PID disappeared")
                return real_read_text(path, *args, **kwargs)

            with mock.patch.object(gate.os, "geteuid", return_value=0), \
                    mock.patch.object(gate.os, "readlink", return_value="namespace"), \
                    mock.patch.object(gate, "_verify_protected_helper"), \
                    mock.patch.object(gate.os, "scandir", side_effect=fake_scandir), \
                    mock.patch.object(gate.os, "stat", side_effect=deny_once_for_target), \
                    mock.patch.object(pathlib.Path, "read_text", autospec=True,
                                     side_effect=fake_read_text):
                result = gate._scan_proc_inode(device, inode)

        self.assertTrue(denied[0])
        self.assertIn([process, "fd", held_fd], result["holders"])

    def test_o_path_fallback_observes_fd_replaced_before_open(self):
        with tempfile.NamedTemporaryFile() as held, tempfile.NamedTemporaryFile() as replacement:
            replacement_info = os.fstat(replacement.fileno())

            def replace_before_open(_real_open, _path, _args, _kwargs):
                os.dup2(replacement.fileno(), held.fileno())

            actual = self._stat_with_denied_proc_fd_stat(held, replace_before_open)
        self.assertEqual((actual.st_dev, actual.st_ino),
                         (replacement_info.st_dev, replacement_info.st_ino))

    def test_o_path_fallback_keeps_identity_when_fd_replaced_after_open(self):
        with tempfile.NamedTemporaryFile() as held, tempfile.NamedTemporaryFile() as replacement:
            held_info = os.fstat(held.fileno())
            path = pathlib.Path(f"/proc/self/fd/{held.fileno()}")
            real_stat = gate.os.stat
            real_open = gate.os.open

            def denied_stat(candidate, *args, **kwargs):
                if os.fspath(candidate) == os.fspath(path):
                    raise PermissionError(13, "injected target-stat denial")
                return real_stat(candidate, *args, **kwargs)

            def replace_after_open(candidate, flags, *args, **kwargs):
                fd = real_open(candidate, flags, *args, **kwargs)
                if os.fspath(candidate) == os.fspath(path):
                    os.dup2(replacement.fileno(), held.fileno())
                return fd

            with mock.patch.object(gate.os, "stat", side_effect=denied_stat), \
                    mock.patch.object(gate.os, "open", side_effect=replace_after_open):
                actual = gate._stat_fd_target(path)
        self.assertEqual((actual.st_dev, actual.st_ino), (held_info.st_dev, held_info.st_ino))

    def test_o_path_fallback_refuses_when_target_remains_unresolved(self):
        with tempfile.NamedTemporaryFile() as held:
            path = pathlib.Path(f"/proc/self/fd/{held.fileno()}")

            def denied(*_args, **_kwargs):
                raise PermissionError(13, "injected denial")

            with mock.patch.object(gate.os, "stat", side_effect=denied), \
                    mock.patch.object(gate.os, "open", side_effect=PermissionError(13, "denied")):
                with self.assertRaisesRegex(gate.GateRefused, "unreadable_fd_target_stat"):
                    gate._stat_fd_target(path)

    def test_o_path_fallback_refuses_if_fstat_fails(self):
        with tempfile.NamedTemporaryFile() as held:
            path = pathlib.Path(f"/proc/self/fd/{held.fileno()}")
            real_stat = gate.os.stat

            def denied_stat(candidate, *args, **kwargs):
                if os.fspath(candidate) == os.fspath(path):
                    raise PermissionError(13, "injected target-stat denial")
                return real_stat(candidate, *args, **kwargs)

            with mock.patch.object(gate.os, "stat", side_effect=denied_stat), \
                    mock.patch.object(gate.os, "fstat", side_effect=PermissionError(13, "denied")):
                with self.assertRaisesRegex(gate.GateRefused, "unreadable_fd_target_stat"):
                    gate._stat_fd_target(path)

    def test_o_path_fallback_refuses_if_closing_pinned_fd_fails(self):
        with tempfile.NamedTemporaryFile() as held:
            path = pathlib.Path(f"/proc/self/fd/{held.fileno()}")
            real_stat = gate.os.stat
            real_close = gate.os.close

            def denied_stat(candidate, *args, **kwargs):
                if os.fspath(candidate) == os.fspath(path):
                    raise PermissionError(13, "injected target-stat denial")
                return real_stat(candidate, *args, **kwargs)

            def failed_close(fd):
                real_close(fd)
                raise OSError(5, "injected close failure")

            with mock.patch.object(gate.os, "stat", side_effect=denied_stat), \
                    mock.patch.object(gate.os, "close", side_effect=failed_close):
                with self.assertRaisesRegex(gate.GateRefused, "unreadable_fd_target_stat"):
                    gate._stat_fd_target(path)

    def test_task_enumeration_permission_refusal_has_operation_category(self):
        real_scandir = gate.os.scandir

        class Entry:
            def __init__(self, name):
                self.name = name

        class Entries:
            def __init__(self, entries):
                self.entries = entries

            def __iter__(self):
                return iter(self.entries)

        class DeniedIteration:
            def __iter__(self):
                raise PermissionError(13, "denied during iteration")
                yield None

        def deny_task_enumeration(path):
            path = os.fspath(path)
            if path == "/proc":
                return Entries([Entry("1"), Entry("2")])
            if path == "/proc/1/task":
                return DeniedIteration()
            return real_scandir(path)

        with mock.patch.object(gate.os, "geteuid", return_value=0), \
                mock.patch.object(gate.os, "readlink", return_value="namespace"), \
                mock.patch.object(gate, "_verify_protected_helper"), \
                mock.patch.object(gate.os, "scandir", side_effect=deny_task_enumeration), \
                self._mock_proc_read_text():
            with self.assertRaisesRegex(gate.GateRefused, "unreadable_task_enumeration"):
                gate._scan_proc_inode(1, 1)

    def _scan_with_fd_stat_failures(self, failures):
        real_scandir = gate.os.scandir
        real_open = gate.os.open

        class Entry:
            def __init__(self, name, path=None):
                self.name = name
                self.path = path or name

        class Entries:
            def __init__(self, entries):
                self.entries = entries

            def __iter__(self):
                return iter(self.entries)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def fake_scandir(path):
            path = os.fspath(path)
            if path == "/proc":
                return Entries([Entry("1"), Entry("2")])
            if path == "/proc/1/task":
                return Entries([Entry("1")])
            if path == "/proc/1/task/1/fd":
                return Entries([Entry("9", "/proc/1/task/1/fd/9")])
            return real_scandir(path)

        def denied_stat(path, *args, **kwargs):
            if os.fspath(path) == "/proc/1/task/1/fd/9":
                error = failures.pop(0)
                raise error
            return real_stat(path, *args, **kwargs)

        def denied_open(path, *args, **kwargs):
            if os.fspath(path) == "/proc/1/task/1/fd/9":
                raise PermissionError(13, "denied fallback")
            return real_open(path, *args, **kwargs)

        real_stat = gate.os.stat
        with mock.patch.object(gate.os, "geteuid", return_value=0), \
                mock.patch.object(gate.os, "readlink", return_value="namespace"), \
                mock.patch.object(gate, "_verify_protected_helper"), \
                mock.patch.object(gate.os, "scandir", side_effect=fake_scandir), \
                mock.patch.object(gate.os, "stat", side_effect=denied_stat), \
                mock.patch.object(gate.os, "open", side_effect=denied_open), \
                self._mock_proc_read_text():
            with self.assertRaisesRegex(gate.GateRefused, "unreadable_fd_target_stat"):
                gate._scan_proc_inode(1, 1)

    def test_fd_target_stat_permission_refusal_has_operation_category(self):
        self._scan_with_fd_stat_failures([PermissionError(13, "denied")])

    def test_fd_target_stat_retry_permission_refusal_has_operation_category(self):
        self._scan_with_fd_stat_failures([
            FileNotFoundError(2, "slot disappeared"), PermissionError(13, "denied on retry")])

    def test_maps_permission_refusal_has_operation_category(self):
        real_scandir = gate.os.scandir
        real_open = pathlib.Path.open

        class Entry:
            def __init__(self, name):
                self.name = name

        class Entries:
            def __init__(self, entries):
                self.entries = entries

            def __iter__(self):
                return iter(self.entries)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def fake_scandir(path):
            path = os.fspath(path)
            if path == "/proc":
                return Entries([Entry("1"), Entry("2")])
            if path == "/proc/1/task":
                return Entries([Entry("1")])
            if path == "/proc/1/task/1/fd":
                return Entries([])
            return real_scandir(path)

        def denied_maps_open(path, *args, **kwargs):
            if os.fspath(path) == "/proc/1/task/1/maps":
                raise PermissionError(13, "denied")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(gate.os, "geteuid", return_value=0), \
                mock.patch.object(gate.os, "readlink", return_value="namespace"), \
                mock.patch.object(gate, "_verify_protected_helper"), \
                mock.patch.object(gate.os, "scandir", side_effect=fake_scandir), \
                mock.patch.object(pathlib.Path, "open", denied_maps_open), \
                self._mock_proc_read_text():
            with self.assertRaisesRegex(gate.GateRefused, "unreadable_task_maps"):
                gate._scan_proc_inode(1, 1)


class ProcessScanTests(unittest.TestCase):
    @staticmethod
    def _ancestry_diagnostic(prefix, install_root):
        paths = [pathlib.Path("/")]
        current = pathlib.Path("/")
        for component in (*prefix.parts[1:], "v3", install_root.name):
            current /= component
            paths.append(current)
        report = []
        for path in paths:
            try:
                info = path.lstat()
            except FileNotFoundError:
                report.append(f"{path}: not created")
                continue
            mode = stat.S_IMODE(info.st_mode)
            problems = []
            if stat.S_ISLNK(info.st_mode):
                problems.append("symlink")
            if not stat.S_ISDIR(info.st_mode):
                problems.append("not-directory")
            if info.st_uid != 0:
                problems.append(f"uid={info.st_uid}, expected root(0)")
            if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                problems.append("group/world-writable")
            if not info.st_mode & stat.S_IXOTH:
                problems.append("not-other-traversable")
            report.append(
                f"{path}: uid={info.st_uid} gid={info.st_gid} mode={mode:04o} "
                f"type={'dir' if stat.S_ISDIR(info.st_mode) else 'other'} "
                f"checks={'PASS' if not problems else ','.join(problems)}")
        return "\n".join(report)

    @classmethod
    def setUpClass(cls):
        sudo = "/usr/bin/sudo"
        authorization = subprocess.run([sudo, "-n", "-v"], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       timeout=10, check=False)
        if authorization.returncode:
            if (os.environ.get("CI", "").lower() == "true"
                    or os.environ.get("GITHUB_ACTIONS", "").lower() == "true"):
                raise AssertionError(
                    "CI requires noninteractive sudo for privileged scanner tests; "
                    "authorization unavailable")
            raise unittest.SkipTest(
                "NOT RUN: privileged scanner tests require noninteractive sudo; "
                "authorization unavailable")
        if os.environ.get("LM_SKIP_PRIVILEGED_SCANNER_INSTALL") == "1":
            if (os.environ.get("CI", "").lower() == "true"
                    or os.environ.get("GITHUB_ACTIONS", "").lower() == "true"):
                raise AssertionError(
                    "CI cannot skip the privileged scanner installation tests")
            raise unittest.SkipTest(
                "NOT RUN: local validation skips the privileged scanner installation "
                "under /usr/local/lib")
        resolved = owner_acceptance.git(ROOT, "rev-parse", "--verify", "HEAD^{commit}")
        if resolved.returncode:
            raise AssertionError("could not resolve exact source candidate for trusted fixture")
        cls.candidate_sha = resolved.stdout.decode("ascii").strip()
        # /usr/local/lib is a standard root-owned, non-writable, traversable
        # system prefix on the CI image. The production installer creates the
        # UUID subtree and all version/candidate directories beneath it.
        cls.install_prefix = pathlib.Path("/usr/local/lib") / f"lm-procfs-test-{uuid.uuid4().hex}"
        if cls.install_prefix.exists() or cls.install_prefix.is_symlink():
            raise AssertionError(f"disposable install prefix unexpectedly exists: {cls.install_prefix}")
        prefix_report = cls._ancestry_diagnostic(
            cls.install_prefix, owner_acceptance.install_path(cls.candidate_sha,
                                                              cls.install_prefix))
        failed_ancestors = [line for line in prefix_report.splitlines()
                            if "checks=" in line and "checks=PASS" not in line]
        if failed_ancestors:
            raise AssertionError(
                f"CI protected install ancestry precheck failed for {cls.install_prefix}:\n"
                f"{prefix_report}")
        cls.protected_root = owner_acceptance.install_path(
            cls.candidate_sha, cls.install_prefix)
        try:
            cls.protected_root = owner_acceptance.install_candidate(
                ROOT, cls.candidate_sha, cls.install_prefix)
        except Exception as error:
            report = cls._ancestry_diagnostic(cls.install_prefix, cls.protected_root)
            try:
                cls._remove_install_fixture()
            except Exception as cleanup_error:
                report += f"\nfixture cleanup failed: {cleanup_error}"
            raise AssertionError(
                f"protected scanner install failed for prefix {cls.install_prefix}: {error}\n"
                f"ancestry diagnostic:\n{report}") from error
        cls.protected_helper = cls.protected_root / "scripts/parent-readonly-safety-gate.py"
        cls.protected_state = cls.protected_root / "lib/vm/parent-state.py"

    @classmethod
    def _remove_install_fixture(cls):
        sudo = "/usr/bin/sudo"
        root = getattr(cls, "protected_root", None)
        if root is not None and root.exists():
            for relative in owner_acceptance.INSTALL_FILES:
                path = root / relative
                if path.exists() or path.is_symlink():
                    subprocess.run([sudo, "-n", "/usr/bin/rm", "--", str(path)],
                                   check=True, timeout=10)
            for relative in sorted(owner_acceptance.INSTALL_DIRS,
                                   key=lambda item: len(pathlib.PurePosixPath(item).parts),
                                   reverse=True):
                path = root / relative
                if path.exists():
                    subprocess.run([sudo, "-n", "/usr/bin/rmdir", "--", str(path)],
                                   check=True, timeout=10)
            subprocess.run([sudo, "-n", "/usr/bin/rmdir", "--", str(root)],
                           check=True, timeout=10)
        prefix = getattr(cls, "install_prefix", None)
        if prefix is not None and prefix.exists():
            if (prefix / "v3").exists():
                subprocess.run([sudo, "-n", "/usr/bin/rmdir", "--", str(prefix / "v3")],
                               check=True, timeout=10)
            subprocess.run([sudo, "-n", "/usr/bin/rmdir", "--", str(prefix)],
                           check=True, timeout=10)

    @classmethod
    def tearDownClass(cls):
        cls._remove_install_fixture()

    def _invoke_privileged_scan(self, info, allowed_pid=0, allowed_fd=-1):
        if not pathlib.Path("/usr/bin/sudo").exists():
            return {"execution": "not_started", "return_code": None, "stdout": b"",
                    "stderr": b"", "elapsed_ms": 0}
        started = time.monotonic()
        try:
            result = subprocess.run(
                ["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-B",
                 str(self.protected_helper), "--scan-inode",
                 str(info.st_dev), str(info.st_ino), str(allowed_pid), str(allowed_fd)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=40, check=False)
        except subprocess.TimeoutExpired as error:
            return {"execution": "timed_out", "return_code": getattr(error, "returncode", None),
                    "stdout": error.output or b"", "stderr": error.stderr or b"",
                    "elapsed_ms": min(60000, int((time.monotonic() - started) * 1000))}
        except OSError:
            return {"execution": "launch_failed", "return_code": None, "stdout": b"",
                    "stderr": b"", "elapsed_ms": min(60000, int((time.monotonic() - started) * 1000))}
        return {"execution": "completed", "return_code": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr,
                "elapsed_ms": min(60000, int((time.monotonic() - started) * 1000))}

    def _privileged_scan(self, info, allowed_pid=0, allowed_fd=-1):
        if not pathlib.Path("/usr/bin/sudo").exists():
            self.fail("CI requires sudo for the real protected scanner integration")
        result = subprocess.run(
            ["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-B",
             str(self.protected_helper), "--scan-inode",
             str(info.st_dev), str(info.st_ino), str(allowed_pid), str(allowed_fd)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=40, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        value = json.loads(result.stdout)
        self.assertIs(value["complete"], True)
        self.assertGreater(value["processes_scanned"], 0)
        self.assertEqual(value["coverage"], {"procfs": True,
                                             "pid_namespace": "init-shared",
                                             "user_namespace": "init-shared",
                                              "descriptors": True, "file_mappings": True})
        return value["holders"]

    @staticmethod
    def _target_child_state(child_pid, original_fd, info):
        state = {"alive": "unknown", "mapping": "unknown", "original_fd": "unknown"}
        try:
            process_fields = (pathlib.Path("/proc") / str(child_pid) / "stat").read_text()
            process_fields = process_fields.rsplit(")", 1)[1].split()
            state["alive"] = ("yes" if process_fields and process_fields[0] not in {"Z", "X"}
                               else "no")
        except FileNotFoundError:
            state["alive"] = "no"
        except (OSError, IndexError):
            pass
        try:
            maps = (pathlib.Path("/proc") / str(child_pid) / "maps").read_text()
            wanted = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
            found = False
            for line in maps.splitlines():
                columns = line.split(None, 5)
                if len(columns) < 5:
                    continue
                major_text, minor_text = columns[3].split(":", 1)
                if (int(major_text, 16), int(minor_text, 16), int(columns[4])) == wanted:
                    found = True
                    break
            state["mapping"] = "yes" if found else "no"
        except (OSError, ValueError, IndexError):
            state["mapping"] = "unknown"
        try:
            os.stat(f"/proc/{child_pid}/fd/{original_fd}")
        except FileNotFoundError:
            state["original_fd"] = "closed"
        except OSError:
            state["original_fd"] = "unknown"
        else:
            state["original_fd"] = "open"
        return state

    @staticmethod
    def _mapping_scan_category(run, child_pid):
        if run["execution"] == "timed_out":
            return "scan_timed_out"
        if run["execution"] != "completed":
            return "scan_launch_failed"
        if run["return_code"] != 0:
            return "scanner_refused"
        if run["stderr"]:
            return "scanner_stderr"
        if len(run["stdout"]) > 65536:
            return "scanner_output_oversized"
        try:
            value = json.loads(run["stdout"])
        except (ValueError, UnicodeError):
            return "scanner_result_malformed"
        if (not isinstance(value, dict) or value.get("complete") is not True
                or type(value.get("processes_scanned")) is not int
                or value["processes_scanned"] < 1
                or value.get("coverage") != {"procfs": True, "pid_namespace": "init-shared",
                                               "user_namespace": "init-shared",
                                               "descriptors": True, "file_mappings": True}
                or not isinstance(value.get("holders"), list)):
            return "scanner_result_incomplete"
        if any(item == [child_pid, "map", 0] for item in value["holders"]):
            return "map_holder_for_child"
        return "map_holder_missing"

    @staticmethod
    def _privilege_status(run, observed):
        if run["execution"] == "not_started":
            return "not_started"
        if run["execution"] == "completed":
            reason = _safe_scanner_reason(run["stderr"])
            if reason == "insufficient_privilege_or_invalid_identity":
                return "unconfirmed"
            if (reason != "none_or_redacted"
                    or observed in {"map_holder_for_child", "map_holder_missing"}):
                return "confirmed"
        return "unconfirmed"

    def test_real_privileged_scanner_finds_invoking_users_scratch_fd(self):
        with tempfile.NamedTemporaryFile() as held:
            info = os.fstat(held.fileno())
            holders = self._privileged_scan(info)
            self.assertIn([os.getpid(), "fd", held.fileno()], holders)

    def test_protected_installed_parent_state_invokes_protected_scanner(self):
        spec = importlib.util.spec_from_file_location("installed_parent_state_scanner_test",
                                                      self.protected_state)
        state = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(state)
        with tempfile.NamedTemporaryFile() as disk:
            info = os.fstat(disk.fileno())
            result = state._scan_stale_disk_holders(
                disk.fileno(), (info.st_dev, info.st_ino),
                scratch_dir=pathlib.Path(tempfile.gettempdir()))
            self.assertIsNone(result)

    def test_real_privileged_scanner_rejects_untrusted_helper_path(self):
        with tempfile.NamedTemporaryFile(prefix="untrusted-parent-scanner-") as helper:
            helper.write((ROOT / "scripts/parent-readonly-safety-gate.py").read_bytes())
            helper.flush()
            os.chmod(helper.name, 0o644)
            result = subprocess.run(
                ["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-B", helper.name,
                 "--scan-inode", "1", "1", "0", "-1"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=10, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"REFUSED: protected_helper_path_untrusted", result.stderr)

    def test_installer_verification_rejects_tampered_installed_helper(self):
        with tempfile.NamedTemporaryFile(prefix="tampered-parent-scanner-") as tampered:
            tampered.write(b"# deliberately altered disposable fixture\n")
            tampered.flush()
            subprocess.run(["/usr/bin/sudo", "-n", "/usr/bin/install", "-o", "0", "-g", "0",
                            "-m", "0644", tampered.name, str(self.protected_helper)],
                           check=True, timeout=10)
            try:
                with self.assertRaisesRegex(owner_acceptance.GateError,
                                            "installed bytes do not match"):
                    owner_acceptance.verify_install(
                        ROOT, self.candidate_sha, self.install_prefix)
            finally:
                subprocess.run(["/usr/bin/sudo", "-n", "/usr/bin/install", "-o", "0", "-g", "0",
                                "-m", "0644",
                                str(ROOT / "scripts/parent-readonly-safety-gate.py"),
                                str(self.protected_helper)], check=True, timeout=10)

    def test_real_privileged_scanner_refuses_forged_caller_fd_exemption(self):
        with tempfile.NamedTemporaryFile() as held:
            info = os.fstat(held.fileno())
            result = subprocess.run(
                ["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-B",
                 str(self.protected_helper), "--scan-inode", str(info.st_dev),
                 str(info.st_ino), str(os.getpid()), "0"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=10, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn(b"invalid_caller_fd_exemption", result.stderr)

    def test_real_privileged_scanner_finds_cross_uid_holder(self):
        with tempfile.NamedTemporaryFile() as held:
            info = os.fstat(held.fileno())
            child = subprocess.Popen(
                ["/usr/bin/sudo", "-n", "/usr/bin/python3", "-c",
                 "import os,sys,time; f=open(sys.argv[1],'rb'); print('READY',os.getpid(),flush=True); time.sleep(20)",
                 held.name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True)
            try:
                ready = child.stdout.readline().split()
                self.assertEqual(ready[0], "READY")
                holder_pid = int(ready[1])
                holders = self._privileged_scan(info, os.getpid(), held.fileno())
                self.assertTrue(any(item[0] == holder_pid and item[1] == "fd" for item in holders),
                                holders)
            finally:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=5)
                child.stdout.close()
                child.stderr.close()

    def test_real_privileged_scanner_finds_mapping_after_fd_close(self):
        with tempfile.NamedTemporaryFile() as held:
            held.write(b"x" * 4096)
            held.flush()
            info = os.fstat(held.fileno())
            child = subprocess.Popen(
                [sys.executable, "-c",
                 "import mmap,os,signal,sys\n"
                 "f=os.open(sys.argv[1],os.O_RDONLY)\n"
                 "m=mmap.mmap(f,0,access=mmap.ACCESS_READ)\n"
                 "os.close(f)\n"
                 "try:\n    os.fstat(f)\n"
                 "except OSError:\n    pass\n"
                 "else:\n    raise AssertionError('original mapping fd remained open')\n"
                 "print('READY',f,flush=True)\n"
                 "signal.pause()\n",
                 held.name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True)
            unknown = {"alive": "unknown", "mapping": "unknown", "original_fd": "unknown"}
            before = dict(unknown)
            after = dict(unknown)
            run = {"execution": "not_started", "return_code": None, "stdout": b"",
                   "stderr": b"", "elapsed_ms": 0}
            try:
                ready = child.stdout.readline().split()
                if len(ready) != 2 or ready[0] != "READY":
                    context = _mapping_scan_failure_context(
                        execution="not_started", privilege="not_started", return_code=None,
                        stderr=b"", elapsed_ms=0, before=before, after=after,
                        expected="map_holder_for_child", observed="fixture_precondition_failed",
                        assertion="fixture map present and original fd closed")
                    self.fail(context)
                try:
                    original_fd = int(ready[1])
                except ValueError:
                    context = _mapping_scan_failure_context(
                        execution="not_started", privilege="not_started", return_code=None,
                        stderr=b"", elapsed_ms=0, before=before, after=after,
                        expected="map_holder_for_child", observed="fixture_precondition_failed",
                        assertion="fixture map present and original fd closed")
                    self.fail(context)
                before = self._target_child_state(child.pid, original_fd, info)
                if (before["alive"] != "yes" or before["mapping"] != "yes"
                        or before["original_fd"] != "closed"):
                    context = _mapping_scan_failure_context(
                        execution="not_started", privilege="not_started", return_code=None,
                        stderr=b"", elapsed_ms=0, before=before, after=after,
                        expected="map_holder_for_child", observed="fixture_precondition_failed",
                        assertion="fixture map present and original fd closed")
                    self.fail(context)

                run = self._invoke_privileged_scan(info, os.getpid(), held.fileno())
                after = self._target_child_state(child.pid, original_fd, info)
                observed = self._mapping_scan_category(run, child.pid)
                context = _mapping_scan_failure_context(
                    execution=run["execution"], privilege=self._privilege_status(run, observed),
                    return_code=run["return_code"], stderr=run["stderr"],
                    elapsed_ms=run["elapsed_ms"], before=before, after=after,
                    expected="map_holder_for_child", observed=observed,
                    assertion="assertEqual(observed,map_holder_for_child)")
                self.assertEqual(observed, "map_holder_for_child", context)
            finally:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                child.wait(timeout=5)
                child.stdout.close()
                child.stderr.close()

class PrivilegedPrerequisiteBranchTests(unittest.TestCase):
    @staticmethod
    def _failed_authorization():
        return subprocess.CompletedProcess(
            args=["sudo", "-n", "-v"], returncode=1, stdout=b"",
            stderr=b"sudo: a password is required")

    def test_local_missing_privileged_prerequisite_remains_skip(self):
        with (mock.patch.object(subprocess, "run", return_value=self._failed_authorization()),
              mock.patch.dict(os.environ, {"CI": "false", "GITHUB_ACTIONS": "false"})):
            with self.assertRaises(unittest.SkipTest):
                ProcessScanTests.setUpClass()

    def test_ci_missing_privileged_prerequisite_fails_instead_of_skipping(self):
        for environment in ({"CI": "true", "GITHUB_ACTIONS": "false"},
                            {"CI": "false", "GITHUB_ACTIONS": "true"}):
            with self.subTest(environment=environment):
                with (mock.patch.object(subprocess, "run",
                                        return_value=self._failed_authorization()),
                      mock.patch.dict(os.environ, environment)):
                    with self.assertRaisesRegex(
                            AssertionError, "CI requires noninteractive sudo"):
                        ProcessScanTests.setUpClass()


class SafetyGateCliIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.install = self.root / "install"
        (self.install / "lib/vm").mkdir(parents=True)
        self.state = self.root / "state"
        self.state.mkdir()
        self.disk = self.state / "disk.qcow2"
        self.disk.write_bytes(b"isolated fixture disk")
        disk_info = os.stat(self.disk)
        self.lab_vm = self.install / "lab-vm"
        self.lab_vm.write_text(
            "#!" + sys.executable + "\nimport json,sys\n"
            f"disk={{'format':'qcow2','device':{disk_info.st_dev},'inode':{disk_info.st_ino}}}\n"
            "if sys.argv[1:] == ['parent','status','--json']:\n"
            " print(json.dumps({'status':'STOPPED','disk':disk}))\n"
            "elif sys.argv[1:] == ['parent','recover','--dry-run','--json']:\n"
            " print(json.dumps({'dry_run':True,'action':'none','status':'STOPPED'}))\n"
            "else: raise SystemExit(2)\n")
        self.lab_vm.chmod(0o700)
        (self.install / "lib/vm/parent-state.py").write_text(
            "def exec_transition_status(identity):\n"
            "    return 'STOPPED' if identity.get('identity_path') in "
            "('child-proxy.identity.json','child-qemu.identity.json') else 'AMBIGUOUS'\n")
        for name in ("child-proxy.identity.json", "child-qemu.identity.json"):
            (self.state / name).write_text(json.dumps({"identity_path": name}))

    def tearDown(self):
        self.temp.cleanup()

    def _invoke(self):
        output, errors = io.StringIO(), io.StringIO()
        with (mock.patch.object(sys, "argv", ["parent-readonly-safety-gate.py",
                                               "--install-root", str(self.install),
                                               "--state-dir", str(self.state)]),
              mock.patch.object(gate.os, "geteuid", return_value=1000),
              mock.patch.object(gate, "_scan_with_privileged_procfs", side_effect=[
                  {"complete": True, "processes_scanned": 2,
                   "coverage": {"procfs": True, "pid_namespace": "init-shared",
                                "user_namespace": "init-shared", "descriptors": True,
                                "file_mappings": True},
                   "holders": [[os.getpid(), "fd", 9]]},
                  {"complete": True, "processes_scanned": 2,
                   "coverage": {"procfs": True, "pid_namespace": "init-shared",
                                "user_namespace": "init-shared", "descriptors": True,
                                "file_mappings": True}, "holders": []}]),
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors)):
            code = gate.main()
        return code, output.getvalue(), errors.getvalue()

    def test_actual_cli_loads_installed_module_and_validates_both_children(self):
        real_load = gate._load_state_module
        real_check = gate._check_children
        with (mock.patch.object(gate, "_load_state_module", wraps=real_load) as load,
              mock.patch.object(gate, "_check_children", wraps=real_check) as check):
            code, output, errors = self._invoke()
        self.assertEqual(code, 0, errors)
        self.assertIn("SAFETY GATE: PASS", output)
        self.assertEqual(load.call_count, 1)
        self.assertEqual(check.call_count, 1)

    def test_actual_cli_converts_unexpected_failure_to_refused_nonzero(self):
        with mock.patch.object(gate, "_check_children", side_effect=NameError("fixture")):
            code, output, errors = self._invoke()
        self.assertEqual(code, 2)
        self.assertNotIn("SAFETY GATE: PASS", output)
        self.assertIn("REFUSED: unexpected gate failure (NameError)", errors)
        self.assertIn("SAFETY GATE: FAIL", errors)


class OpenSSHHostLookupTests(unittest.TestCase):
    def test_parent_bracketed_loopback_port_matches_only_that_port(self):
        keygen = shutil.which("ssh-keygen")
        ssh = shutil.which("ssh")
        if not keygen or not ssh:
            self.skipTest("OpenSSH tools unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            private = root / "host"
            subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(private)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10)
            key_type, public_key, *_ = private.with_suffix(".pub").read_text().split()
            known_hosts = root / "known_hosts"
            known_hosts.write_text(f"[127.0.0.1]:46617 {key_type} {public_key}\n")
            exact = subprocess.run([keygen, "-F", "[127.0.0.1]:46617", "-f", str(known_hosts)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=5, check=False)
            other_port = subprocess.run(
                [keygen, "-F", "[127.0.0.1]:46618", "-f", str(known_hosts)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
            self.assertEqual(exact.returncode, 0)
            self.assertNotEqual(other_port.returncode, 0)
            config = subprocess.run([ssh, "-G", "-F", "/dev/null", "-p", "46617",
                                     "mester@127.0.0.1"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, timeout=5, check=True).stdout.splitlines()
            values = {line.split(None, 1)[0]: line.split(None, 1)[1]
                      for line in config if line.startswith(("hostname ", "port ", "hostkeyalias "))}
            self.assertEqual(values.get("hostname"), "127.0.0.1")
            self.assertEqual(values.get("port"), "46617")
            self.assertNotIn("hostkeyalias", values)

    def test_diagnostic_fingerprints_only_keys_and_sanitizes_rejection(self):
        keygen = shutil.which("ssh-keygen")
        if not keygen:
            self.skipTest("ssh-keygen unavailable")
        with tempfile.TemporaryDirectory() as directory:
            private = pathlib.Path(directory) / "host"
            subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(private)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10)
            public_line = private.with_suffix(".pub").read_bytes()
            expected = diagnostic._fingerprint(b"", public_file=private.with_suffix(".pub"))
            self.assertEqual(diagnostic._fingerprint(public_line), expected)
            self.assertEqual(
                diagnostic._rejection_label(
                    b"WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! secret-looking text"),
                "CHANGED_KEY",
            )
            self.assertEqual(
                diagnostic._server_fingerprint(
                    b"debug1: Server host key: ssh-ed25519 SHA256:"
                    b"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
                "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            )


if __name__ == "__main__":
    unittest.main()
