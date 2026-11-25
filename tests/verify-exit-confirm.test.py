#!/usr/bin/env python3
"""Focused regression tests for the protected-launcher PTY verifier."""

import contextlib
import importlib.util
import io
import json
import pathlib
import shlex
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "verify-exit-confirm.py"
CONTRACT_PATH = ROOT / "lib" / "vm" / "parent-session-contract.py"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load test module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


verifier = load_module("verify_exit_confirm", SCRIPT_PATH)
contract = load_module("verify_exit_confirm_contract", CONTRACT_PATH)


class FakeClock:
    def __init__(self):
        self.now_ns = 123
        self.sleeps = []

    def monotonic_ns(self):
        return self.now_ns

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now_ns += round(seconds * 1_000_000_000)

    def advance(self, nanoseconds):
        self.now_ns += nanoseconds


class DelayAwareChild:
    def __init__(self, clock, *, delaybeforesend=0.05, write_ns=(0, 0),
                 hidden_second_delay_ns=0, fail_on_send=None):
        self.clock = clock
        self.delaybeforesend = delaybeforesend
        self.write_ns = write_ns
        self.hidden_second_delay_ns = hidden_second_delay_ns
        self.fail_on_send = fail_on_send
        self.calls = []

    def send(self, text):
        send_number = len(self.calls) + 1
        self.calls.append((text, self.delaybeforesend))
        if self.delaybeforesend is not None:
            self.clock.sleep(self.delaybeforesend)
        if send_number == 2 and self.hidden_second_delay_ns:
            self.clock.sleep(self.hidden_second_delay_ns / 1_000_000_000)
        self.clock.advance(self.write_ns[send_number - 1])
        if send_number == self.fail_on_send:
            raise RuntimeError("injected send failure")


class FastSendTimingTests(unittest.TestCase):
    def make_driver(self, clock, child, *, drain_ns=0):
        driver = object.__new__(verifier.Driver)
        driver.child = child
        waits = []

        def drain(seconds):
            waits.append((seconds, child.delaybeforesend))
            clock.advance(drain_ns)

        driver.wait = drain
        return driver, waits

    def run_fast_send(self, driver, clock):
        with (
            mock.patch.object(verifier.time, "monotonic_ns", side_effect=clock.monotonic_ns),
            mock.patch.object(verifier.time, "sleep", side_effect=clock.sleep),
        ):
            return driver.send_fast("\x1b", "n")

    def test_fast_send_measures_both_writes_but_not_drain_and_restores_before_drain(self):
        clock = FakeClock()
        child = DelayAwareChild(clock, write_ns=(1_000_000, 2_000_000))
        driver, waits = self.make_driver(clock, child, drain_ns=500_000_000)

        elapsed_ns = self.run_fast_send(driver, clock)

        self.assertEqual(child.calls, [("\x1b", None), ("n", None)])
        self.assertEqual(clock.sleeps, [0.030])
        self.assertEqual(elapsed_ns, 33_000_000)
        self.assertEqual(waits, [(0.25, 0.05)])
        self.assertEqual(clock.now_ns - 123, elapsed_ns + 500_000_000)
        self.assertEqual(child.delaybeforesend, 0.05)

    def test_fast_send_restores_nondefault_and_none_delays(self):
        for previous_delay in (0.017, None):
            with self.subTest(previous_delay=previous_delay):
                clock = FakeClock()
                child = DelayAwareChild(clock, delaybeforesend=previous_delay)
                driver, waits = self.make_driver(clock, child)

                self.assertEqual(self.run_fast_send(driver, clock), 30_000_000)

                self.assertEqual(child.calls, [("\x1b", None), ("n", None)])
                self.assertEqual(waits, [(0.25, previous_delay)])
                self.assertIs(child.delaybeforesend, previous_delay)

    def test_fast_send_restores_delay_on_either_send_exception(self):
        for previous_delay in (0.017, None):
            for fail_on_send in (1, 2):
                with self.subTest(previous_delay=previous_delay, fail_on_send=fail_on_send):
                    clock = FakeClock()
                    child = DelayAwareChild(
                        clock, delaybeforesend=previous_delay, fail_on_send=fail_on_send,
                    )
                    driver, waits = self.make_driver(clock, child)

                    with self.assertRaisesRegex(RuntimeError, "injected send failure"):
                        self.run_fast_send(driver, clock)

                    self.assertTrue(all(delay is None for _, delay in child.calls))
                    self.assertIs(child.delaybeforesend, previous_delay)
                    self.assertEqual(waits, [])

    def test_hidden_second_send_delay_fails_the_35ms_timing_bound(self):
        clock = FakeClock()
        child = DelayAwareChild(clock, write_ns=(1, 2), hidden_second_delay_ns=50_000_000)
        driver, _ = self.make_driver(clock, child)

        elapsed_ns = self.run_fast_send(driver, clock)

        self.assertGreater(elapsed_ns, verifier.FAST_ESCAPE_MAX_ELAPSED_NS)
        self.assertFalse(elapsed_ns <= verifier.FAST_ESCAPE_MAX_ELAPSED_NS)

    def test_exact_35ms_is_accepted_and_one_nanosecond_over_is_rejected(self):
        boundary = verifier.FAST_ESCAPE_MAX_ELAPSED_NS
        for extra_write_ns, expected_elapsed, expected_valid in (
            (0, boundary, True),
            (1, boundary + 1, False),
        ):
            with self.subTest(extra_write_ns=extra_write_ns):
                clock = FakeClock()
                child = DelayAwareChild(
                    clock, write_ns=(2_500_000, 2_500_000 + extra_write_ns),
                )
                driver, _ = self.make_driver(clock, child)

                elapsed_ns = self.run_fast_send(driver, clock)

                self.assertEqual(elapsed_ns, expected_elapsed)
                self.assertEqual(
                    elapsed_ns <= verifier.FAST_ESCAPE_MAX_ELAPSED_NS,
                    expected_valid,
                )

    def test_ordinary_send_keeps_the_configured_delay(self):
        clock = FakeClock()
        child = DelayAwareChild(clock, delaybeforesend=0.05, write_ns=(1,))
        driver, waits = self.make_driver(clock, child)

        driver.send("x")

        self.assertEqual(child.calls, [("x", 0.05)])
        self.assertEqual(clock.sleeps, [0.05])
        self.assertEqual(waits, [(1.2, 0.05)])
        self.assertEqual(child.delaybeforesend, 0.05)

    def test_real_pexpect_fast_sends_skip_only_the_implicit_delay(self):
        child = verifier.pexpect.spawn("/bin/cat", [], encoding="utf-8", timeout=2)
        child.delaybeforesend = 0.05
        clock = FakeClock()
        driver, waits = self.make_driver(clock, child)
        try:
            with (
                mock.patch.object(verifier.time, "monotonic_ns", side_effect=clock.monotonic_ns),
                mock.patch.object(verifier.time, "sleep", side_effect=clock.sleep),
            ):
                elapsed_ns = driver.send_fast("\x1b", "n")
                self.assertEqual(clock.sleeps, [0.030])
                self.assertEqual(elapsed_ns, 30_000_000)
                self.assertEqual(child.delaybeforesend, 0.05)

                driver.send("x")

            self.assertEqual(clock.sleeps, [0.030, 0.05])
            self.assertEqual(waits, [(0.25, 0.05), (1.2, 0.05)])
            self.assertEqual(child.delaybeforesend, 0.05)
        finally:
            child.close(force=True)


class PhaseRecorder:
    def __init__(self):
        self.entries = []

    def record(self, phase, event, **details):
        self.entries.append({"phase": phase, "event": event, **details})


class DialogStateDriver:
    def __init__(self, outcomes=(), *, elapsed_ns=30_000_000, reattach_result=True,
                 reattach_error=None, live=True, dialog_open=True, log=None):
        self.via_tmux = True
        self.outcomes = list(outcomes)
        self.elapsed_ns = elapsed_ns
        self.reattach_result = reattach_result
        self.reattach_error = reattach_error
        self.is_live = live
        self.dialog_open = dialog_open
        self.reattach_calls = 0
        self.dialog_state_at_reattach = []
        self.ctrl_calls = []
        self.calls = []
        self.buffer = ""
        self.log = log

    def live_tui(self):
        return self.is_live

    def alive(self):
        return self.is_live

    def dialog_visible(self):
        return self.dialog_open

    def ctrl(self, key):
        self.calls.append(("ctrl", key))
        self.ctrl_calls.append(key)
        if key == "c" and self.is_live:
            self.dialog_open = not self.dialog_open

    def send_fast(self, first, second):
        self.calls.append(("send_fast", first, second))
        cancelled = self.outcomes.pop(0)
        if cancelled:
            self.dialog_open = False
        return self.elapsed_ns

    def detach_and_reattach(self):
        self.calls.append(("detach_and_reattach",))
        self.reattach_calls += 1
        self.dialog_state_at_reattach.append(self.dialog_open)
        if self.log is not None:
            self.log.write("reattach-output-sentinel\n")
            self.log.flush()
        if self.reattach_error is not None:
            raise self.reattach_error
        return self.reattach_result


class FastEscapeStateMachineTests(unittest.TestCase):
    def run_trials(self, outcomes):
        driver = DialogStateDriver(outcomes)
        phase_log = PhaseRecorder()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            failures, summary = verifier.run_fast_escape_trials(
                driver, phase_log=phase_log,
            )
        return driver, phase_log, output.getvalue(), failures, summary

    def test_zero_mixed_and_ten_of_ten_cancellation_outcomes_are_distinct(self):
        cases = (
            ([False] * 10, 0),
            ([True, False, True, False, False, True, False, True, False, True], 5),
            ([True] * 10, 10),
        )
        for outcomes, expected_cancelled in cases:
            with self.subTest(expected_cancelled=expected_cancelled):
                driver, phase_log, output, failures, summary = self.run_trials(outcomes)
                self.assertEqual(summary, {
                    "attempted": 10, "cancelled": expected_cancelled, "requested": 10,
                })
                self.assertEqual(sum(failures), 10 - expected_cancelled)
                self.assertIn(
                    f"Fast Escape+n cancellation: {expected_cancelled}/10 attempted; "
                    "10/10 trials attempted",
                    output,
                )
                self.assertEqual(
                    len([entry for entry in phase_log.entries
                         if entry["event"] == "result"]),
                    10,
                )
                self.assertEqual(
                    len([call for call in driver.calls if call[0] == "send_fast"]), 10,
                )
                # Ctrl+C is issued only when state observation proves the dialog is closed.
                self.assertEqual(len(driver.ctrl_calls), sum(outcomes[:-1]))

    def test_timing_and_cancellation_failures_do_not_block_independent_reattach(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="v-reattach-") as temp:
            log_path = pathlib.Path(temp) / "pty-session-1.log"
            with log_path.open("x", encoding="utf-8") as session_log:
                driver = DialogStateDriver(
                    [False] * 10, elapsed_ns=verifier.FAST_ESCAPE_MAX_ELAPSED_NS + 1,
                    log=session_log,
                )
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    trial_failures, summary = verifier.run_fast_escape_trials(driver)
                    reattach_failures = verifier.run_reattach_evidence(driver)
                self.assertEqual(summary["cancelled"], 0)
                self.assertTrue(all(trial_failures))
                self.assertFalse(any(reattach_failures))
                self.assertEqual(driver.reattach_calls, 1)
                self.assertEqual(driver.dialog_state_at_reattach, [False])
                self.assertIn(("detach_and_reattach",), driver.calls)
                self.assertIn("PASS tmux disconnect and reattach preserve the TUI pane",
                              output.getvalue())
                self.assertIn("PASS Ctrl+C opens after tmux reattach", output.getvalue())
                self.assertEqual(len(trial_failures), 20)
            self.assertIn("reattach-output-sentinel", log_path.read_text(encoding="utf-8"))

    def test_unsafe_recovery_skips_both_reattach_assertions(self):
        driver = DialogStateDriver(live=False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            failures = verifier.run_reattach_evidence(driver)
        self.assertTrue(all(failures))
        self.assertEqual(driver.reattach_calls, 0)
        self.assertIn("safe recovery unavailable", output.getvalue())
        self.assertIn("SKIP tmux disconnect and reattach preserve the TUI pane", output.getvalue())
        self.assertIn("SKIP Ctrl+C opens after tmux reattach", output.getvalue())
        self.assertNotIn("PASS Ctrl+C opens after tmux reattach", output.getvalue())

    def test_unavailable_trial_state_marks_every_unattempted_check_as_required_skip(self):
        driver = DialogStateDriver(live=False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            failures, summary = verifier.run_fast_escape_trials(driver)
        self.assertEqual(sum(failures), 20)
        self.assertEqual(summary, {"attempted": 0, "cancelled": 0, "requested": 10})
        self.assertFalse(any(call[0] == "send_fast" for call in driver.calls))
        self.assertIn("SKIP fast Escape+n timing bound (1/10)", output.getvalue())
        self.assertIn("SKIP fast Escape+n cancellation (10/10)", output.getvalue())

    def test_failed_and_exceptional_reattach_never_pass_dependent_assertion(self):
        cases = (
            (False, None, "did not succeed"),
            (True, RuntimeError("injected reattach exception"), "raised RuntimeError"),
        )
        for reattach_result, reattach_error, expected_reason in cases:
            with self.subTest(reattach_error=reattach_error):
                driver = DialogStateDriver(
                    reattach_result=reattach_result, reattach_error=reattach_error,
                )
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    failures = verifier.run_reattach_evidence(driver)
                self.assertTrue(any(failures))
                self.assertEqual(driver.reattach_calls, 1)
                self.assertIn(expected_reason, output.getvalue())
                self.assertIn("SKIP Ctrl+C opens after tmux reattach", output.getvalue())
                self.assertNotIn("PASS Ctrl+C opens after tmux reattach", output.getvalue())


class SessionLogTests(unittest.TestCase):
    class FakeChild:
        def __init__(self, output=""):
            self.output = output
            self.alive = True

        def isalive(self):
            return self.alive

        def close(self, force=False):
            self.alive = False

        def read_nonblocking(self, size, timeout):
            if self.output:
                output, self.output = self.output, ""
                return output
            raise verifier.pexpect.TIMEOUT("empty fixture")

    def test_driver_logs_are_exclusive_distinct_and_reattach_output_stays_in_session_one(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="v-session-logs-") as temp:
            home = pathlib.Path(temp)
            (home / "workspace").mkdir(mode=0o700)
            previous_binary = verifier.OPENCODE_BINARY
            verifier.OPENCODE_BINARY = "/fake/opencode"
            children = [self.FakeChild(), self.FakeChild()]
            try:
                with (
                    mock.patch.object(verifier, "spawn_pty", side_effect=children),
                    mock.patch.object(verifier.Driver, "wait", lambda _self, _seconds: None),
                ):
                    first = verifier.Driver(str(home), session_number=1)
                    first.log.write("first-Y-sentinel\n")
                    first.log.flush()
                    second = verifier.Driver(str(home), session_number=2)
                    second.log.write("second-Q-sentinel\n")
                    second.log.flush()

                    with self.assertRaises(FileExistsError):
                        verifier.Driver(str(home), session_number=1)

                first.child = self.FakeChild("reattach-output-sentinel\n")
                with mock.patch.object(verifier.time, "time", side_effect=(10, 10, 12)):
                    first.wait(1)
                first.close()
                second.close()
            finally:
                verifier.OPENCODE_BINARY = previous_binary

            first_log = home / "pty-session-1.log"
            second_log = home / "pty-session-2.log"
            self.assertIn("first-Y-sentinel", first_log.read_text(encoding="utf-8"))
            self.assertIn("reattach-output-sentinel", first_log.read_text(encoding="utf-8"))
            self.assertNotIn("second-Q-sentinel", first_log.read_text(encoding="utf-8"))
            self.assertIn("second-Q-sentinel", second_log.read_text(encoding="utf-8"))

    def test_actual_detach_reattach_spawns_attach_and_logs_its_output_to_origin_session(self):
        class DetachingChild:
            def __init__(self, *, live):
                self.live = live
                self.sent = []
                self.closed = False

            def sendcontrol(self, key):
                self.sent.append(("control", key))

            def send(self, text):
                self.sent.append(("send", text))

            def isalive(self):
                return self.live

            def close(self, force=False):
                self.closed = True

        class AttachedChild(DetachingChild):
            def __init__(self):
                super().__init__(live=True)
                self.output = "reattached-pane-output-sentinel\n"

            def read_nonblocking(self, size, timeout):
                if self.output:
                    output, self.output = self.output, ""
                    return output
                raise verifier.pexpect.TIMEOUT("empty fixture")

        with tempfile.TemporaryDirectory(dir="/tmp", prefix="v-reattach-driver-") as temp:
            home = pathlib.Path(temp)
            (home / "workspace").mkdir(mode=0o700)
            log_path = home / "pty-session-1.log"
            log = log_path.open("x", encoding="utf-8")
            driver = object.__new__(verifier.Driver)
            driver.via_tmux = True
            driver.child = DetachingChild(live=False)
            driver.log = log
            driver.log_path = str(log_path)
            driver.tmux = "/usr/bin/tmux"
            driver.tmux_socket = "private-test"
            driver.tmux_config = str(home / "tmux.conf")
            driver.tmux_session = "little-mester-opencode"
            driver.env = {"PATH": "/usr/bin:/bin"}
            driver.buffer = ""
            driver.pane_pid = mock.Mock(side_effect=("4321", "4321"))
            attached = AttachedChild()
            detaching = driver.child
            try:
                with (
                    mock.patch.object(verifier.pexpect, "spawn", return_value=attached) as spawn,
                    mock.patch.object(verifier.time, "time", side_effect=(0, 1, 0, 0, 1)),
                ):
                    self.assertTrue(driver.detach_and_reattach())
                self.assertEqual(driver.child, attached)
                self.assertEqual(detaching.sent, [("control", "b"), ("send", "d")])
                self.assertTrue(detaching.closed)
                self.assertEqual(driver.pane_pid.call_count, 2)
                self.assertEqual(
                    driver.pane_pid.call_args_list,
                    [mock.call(), mock.call()],
                )
                spawn.assert_called_once_with(
                    "/usr/bin/tmux",
                    ["-L", "private-test", "-f", str(home / "tmux.conf"),
                     "attach-session", "-t", "little-mester-opencode"],
                    cwd=str(home / "workspace"), env=driver.env, encoding="utf-8",
                    codec_errors="replace", dimensions=(40, 120), timeout=20,
                )
            finally:
                log.close()
            self.assertIn(
                "reattached-pane-output-sentinel", log_path.read_text(encoding="utf-8"),
            )

    def test_detach_reattach_rejects_changed_pane_identity(self):
        class Child:
            def __init__(self, *, live):
                self.live = live

            def sendcontrol(self, key):
                pass

            def send(self, text):
                pass

            def isalive(self):
                return self.live

            def close(self, force=False):
                pass

        with tempfile.TemporaryDirectory(dir="/tmp", prefix="v-reattach-identity-") as temp:
            home = pathlib.Path(temp)
            (home / "workspace").mkdir(mode=0o700)
            driver = object.__new__(verifier.Driver)
            driver.via_tmux = True
            driver.child = Child(live=False)
            driver.log_path = str(home / "pty-session-1.log")
            driver.tmux = "/usr/bin/tmux"
            driver.tmux_socket = "private-test"
            driver.tmux_config = str(home / "tmux.conf")
            driver.tmux_session = "little-mester-opencode"
            driver.env = {"PATH": "/usr/bin:/bin"}
            driver.pane_pid = mock.Mock(side_effect=("4321", "9999"))
            driver.wait = mock.Mock()
            with mock.patch.object(verifier.pexpect, "spawn", return_value=Child(live=True)):
                self.assertFalse(driver.detach_and_reattach())
            self.assertEqual(driver.pane_pid.call_count, 2)

    def test_detach_refuses_missing_pane_identity_without_attempting_attach(self):
        driver = object.__new__(verifier.Driver)
        driver.via_tmux = True
        driver.pane_pid = mock.Mock(return_value=None)
        driver.child = mock.Mock()
        self.assertFalse(driver.detach_and_reattach())
        driver.pane_pid.assert_called_once_with()
        driver.child.sendcontrol.assert_not_called()


class ArtifactRetentionTests(unittest.TestCase):
    class MainDriver:
        def __init__(self, home, *, session_number, fail_early=False):
            self.log_path = pathlib.Path(home) / f"pty-session-{session_number}.log"
            self.log = self.log_path.open("x", encoding="utf-8")
            self.log.write("first-session-sentinel\n")
            self.log.flush()
            self.via_tmux = False
            self.is_live = True
            self.dialog_open = False
            self.buffer = ""
            self.fail_early = fail_early

        def alive(self):
            return self.is_live

        def prompt_text_visible(self, text):
            return self.is_live and (text == "Ask anything" or text in self.buffer)

        def dialog_visible(self):
            return self.dialog_open

        def ctrl(self, key):
            if key == "c" and self.is_live:
                self.dialog_open = not self.dialog_open

        def send(self, text):
            if self.fail_early and text == "yq":
                raise RuntimeError("injected early verifier failure")
            if text == "yq":
                self.buffer += text
            elif text in ("\x1b", "n"):
                self.dialog_open = False
            elif text in ("Y", "Q"):
                self.is_live = False
                self.dialog_open = False

        def close(self):
            if not self.log.closed:
                self.log.close()

    def run_main_exception_case(self, *, second_constructor_fails):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="v-retained-") as root:
            home = pathlib.Path(root) / "artifacts"
            home.mkdir(mode=0o700)
            constructed = []

            def fake_driver(path, *, session_number, **_kwargs):
                if session_number == 2 and second_constructor_fails:
                    second_log = pathlib.Path(path) / "pty-session-2.log"
                    with second_log.open("x", encoding="utf-8") as stream:
                        stream.write("second-constructor-sentinel\n")
                    raise RuntimeError("injected second-driver construction failure")
                driver = self.MainDriver(
                    path, session_number=session_number,
                    fail_early=not second_constructor_fails,
                )
                constructed.append(driver)
                return driver

            output = io.StringIO()
            previous_globals = (
                verifier.OPENCODE_BINARY, verifier.ROOT, verifier.PLUGIN,
                list(verifier.CLI_CONFIG["plugins"]),
            )
            version = SimpleNamespace(returncode=0, stdout="opencode v2.2.5\n", stderr="")
            try:
                with (
                    mock.patch.object(verifier.tempfile, "mkdtemp", return_value=str(home)),
                    mock.patch.object(verifier.shutil, "which", return_value="/fake/opencode"),
                    mock.patch.object(verifier.subprocess, "run", return_value=version),
                    mock.patch.object(verifier, "Driver", side_effect=fake_driver),
                    mock.patch.object(verifier.time, "sleep"),
                    mock.patch.object(sys, "argv", ["verify-exit-confirm.py"]),
                    contextlib.redirect_stdout(output),
                    self.assertRaisesRegex(RuntimeError, "injected"),
                ):
                    verifier.main()
            finally:
                (verifier.OPENCODE_BINARY, verifier.ROOT, verifier.PLUGIN,
                 plugins) = previous_globals
                verifier.CLI_CONFIG["plugins"] = plugins

            first_log = home / "pty-session-1.log"
            self.assertTrue(first_log.is_file())
            self.assertIn("first-session-sentinel", first_log.read_text(encoding="utf-8"))
            self.assertIn(f"PTY session log: {first_log}", output.getvalue())
            if second_constructor_fails:
                second_log = home / "pty-session-2.log"
                self.assertIn("second-constructor-sentinel", second_log.read_text(encoding="utf-8"))
                self.assertIn(f"PTY session log: {second_log}", output.getvalue())
            phase_log = home / "verification-phases.jsonl"
            self.assertTrue(phase_log.is_file())
            self.assertIn('"event": "start"', phase_log.read_text(encoding="utf-8"))
            self.assertNotIn("first-session-sentinel", phase_log.read_text(encoding="utf-8"))
            self.assertEqual(len(constructed), 1)

    def test_second_driver_construction_failure_retains_and_enumerates_first_log(self):
        self.run_main_exception_case(second_constructor_fails=True)

    def test_early_session_exception_retains_and_enumerates_first_log(self):
        self.run_main_exception_case(second_constructor_fails=False)


class IsolatedTmuxConfigTests(unittest.TestCase):
    def test_canonical_config_is_only_stripped_of_the_two_host_sources(self):
        expected = b"set-option -s escape-time 10\nset-option -g status off\n"
        self.assertEqual(verifier.isolated_tmux_config(contract.TMUX_CONFIG), expected)

    def test_non_source_mutations_and_bytes_are_preserved(self):
        canonical = contract.TMUX_CONFIG
        mutated = canonical.replace(
            b"set-option -s escape-time 10\n",
            b"# keep this comment\nset-option -s escape-time 500\nset-option -g status-keys vi\n",
        )
        self.assertEqual(
            verifier.isolated_tmux_config(mutated),
            b"# keep this comment\nset-option -s escape-time 500\nset-option -g status-keys vi\n"
            b"set-option -g status off\n",
        )

    def test_supported_lines_and_missing_final_newline_are_preserved(self):
        cases = (
            (b"set-option -s escape-time 500", b"set-option -s escape-time 500"),
            (b"set-option -s escape-time 0\n", b"set-option -s escape-time 0\n"),
            (b"set-option -s escape-time 12345678901234567890\n",
             b"set-option -s escape-time 12345678901234567890\n"),
            (b"set-option -g status-keys vi\n", b"set-option -g status-keys vi\n"),
            (b"set-option -g status-keys emacs", b"set-option -g status-keys emacs"),
            (b"set-option -g status off\n", b"set-option -g status off\n"),
            (b"set-option -g status off", b"set-option -g status off"),
            (b"\n  # printable ASCII comment; source-file is inert\n",
             b"\n  # printable ASCII comment; source-file is inert\n"),
        )
        for config, expected in cases:
            with self.subTest(config=config):
                self.assertEqual(verifier.isolated_tmux_config(config), expected)

    def test_unsupported_config_lines_are_rejected_as_a_whole(self):
        rejected = (
            # Quoted command names or arguments.
            b'"set-option" -s escape-time 10\n',
            b"'set-option' -s escape-time 10\n",
            b'set-option -s "escape-time" 10\n',
            b"set-option -s escape-time '10'\n",
            # Source commands and command-name abbreviations.
            b"source-file -q /tmp/other-tmux.conf\n",
            b"source ~/.config/tmux.conf\n",
            b"sour -q /tmp/other-tmux.conf\n",
            b"set -s escape-time 10\n",
            b"set-option -s escape 10\n",
            # Backslash commands, escaped separators, and continuations.
            b"set-option -s escape-time 10\\; display-message unsafe\n",
            b"set-option -s escape-time 10\\\n",
            b"set-option -s escape-time \\\n10\n",
            # Chained commands, including an otherwise removable source line.
            b"set-option -s escape-time 10; display-message unsafe\n",
            b"source-file -q /etc/tmux.conf; display-message unsafe\n",
            # Control flow, shell commands, hooks, and format substitutions.
            b"{\n",
            b"}\n",
            b"%if 1\n",
            b"%endif\n",
            b"run-shell 'touch /tmp/unsafe'\n",
            b"if-shell 'true' 'display-message unsafe'\n",
            b"set-hook -g after-new-window 'run-shell unsafe'\n",
            b"set-option -s escape-time #{run-shell unsafe}\n",
            b"set-option -s escape-time $(unsafe)\n",
            # Unsupported options, flags, values, and extra arguments.
            b"set-option -s escape-time\n",
            b"set-option -s escape-time 10 extra\n",
            b"set-option -- -s escape-time 10\n",
            b"set-option -g escape-time 10\n",
            b"set-option -s escape-time -1\n",
            b"set-option -s escape-time 10ms\n",
            b"set-option -s escape-time default\n",
            b"set-option -g status-keys on\n",
            b"set-option -g status-keys vi extra\n",
            b"set-option -g status on\n",
            b"set-option -g status off extra\n",
            b"set-option -g status off; run-shell unsafe\n",
            b"set-option -s escape-time 10 # inline comment\n",
            # Almost-matching host sources must not be stripped.
            b"source-file -q /etc/tmux.conf \n",
            b" source-file -q /etc/tmux.conf\n",
            b"source-file  -q /etc/tmux.conf\n",
            b"source-file -q /etc/tmux.conf",
            # Non-LF line endings and control/non-ASCII bytes.
            b"set-option -s escape-time 10\r\n",
            b"set-option -s escape-time 10\r",
            b"set-option -s escape-time 10\x00\n",
            b"set-option -s escape-time\t10\n",
            b"# comment with tab\t\n",
            b"# comment with control \x01\n",
            b"# comment with DEL \x7f\n",
            b"# non-ASCII comment \x80\n",
        )
        for config in rejected:
            with self.subTest(config=config), self.assertRaises(ValueError):
                verifier.isolated_tmux_config(config)

    def test_invalid_config_is_rejected_before_any_file_or_tmux_launch(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="v-invalid-") as home:
            with (
                mock.patch.object(verifier.subprocess, "run") as run,
                mock.patch.object(verifier.pexpect, "spawn") as spawn,
                self.assertRaises(ValueError),
            ):
                verifier.Driver(
                    home, via_tmux=True,
                    tmux_config_source=b"set-option -s escape-time 10; run-shell unsafe\n",
                )
            self.assertEqual(list(pathlib.Path(home).iterdir()), [])
            run.assert_not_called()
            spawn.assert_not_called()


class LauncherRenderingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="v-")
        base = pathlib.Path(self.temp.name)
        self.home = base / "h space's;$(x)"
        self.home.mkdir(mode=0o700)
        self.workspace = self.home / "workspace ; $' (test)"
        self.workspace.mkdir(mode=0o700)
        self.tmux_tmp = self.home / "tmux tmp"
        self.tmux_config = self.home / "tmux ' config;$(x)"
        self.tmux_binary = self.home / "fake tmux; '$()"
        self.server_config = self.home / "server config ' ;$.json"
        self.runner = self.home / "runner ' ;$()"
        self.socket = "private socket"
        self.record_path = self.home / "tmux calls.jsonl"

        fake_tmux = (
            "#!/usr/bin/python3\n"
            "import json, os, pathlib, sys\n"
            f"record = pathlib.Path({str(self.record_path)!r})\n"
            "with record.open('a', encoding='utf-8') as stream:\n"
            "    stream.write(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ), "
            "'cwd': os.getcwd()}) + '\\n')\n"
        )
        self.tmux_binary.write_text(fake_tmux, encoding="utf-8")
        self.tmux_binary.chmod(0o700)

        self.launcher = self.home / "launcher path with spaces"
        rendered = verifier.render_test_launcher(
            contract.TUI_LAUNCHER_TEMPLATE,
            workspace=str(self.workspace),
            home=str(self.home),
            tmux_tmp=str(self.tmux_tmp),
            tmux_config=str(self.tmux_config),
            server_config=str(self.server_config),
            runner=str(self.runner),
            tmux_binary=str(self.tmux_binary),
            socket=self.socket,
        )
        self.launcher.write_bytes(rendered)
        self.launcher.chmod(0o700)

    def tearDown(self):
        self.temp.cleanup()

    def test_rendered_launcher_preserves_argv_environment_cwd_and_order(self):
        inherited = {
            "PATH": "/usr/bin:/bin",
            "TERM": "xterm-256color",
            "INHERITED_SECRET": "must-not-leak",
        }
        refused = subprocess.run(
            [str(self.launcher), "--unsupported"], env=inherited,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(refused.returncode, 64)
        self.assertFalse(self.record_path.exists())

        launched = subprocess.run(
            [str(self.launcher), "--continue"], env=inherited,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(launched.returncode, 0, launched.stderr)
        calls = [json.loads(line) for line in self.record_path.read_text().splitlines()]
        self.assertEqual(len(calls), 6)

        prefix = ["-L", self.socket, "-f", str(self.tmux_config)]
        self.assertEqual(
            [call["argv"] for call in calls],
            [
                prefix + ["show-options", "-s", "escape-time"],
                prefix + ["set-option", "-s", "escape-time", "10"],
                prefix + ["set-option", "-g", "status", "off"],
                prefix + ["has-session", "-t", "little-mester-opencode"],
                prefix + ["set-option", "-t", "little-mester-opencode", "status", "off"],
                prefix + [
                    "new-session", "-A", "-s", "little-mester-opencode",
                    shlex.quote(str(self.runner)), "--continue",
                ],
            ],
        )

        expected_env = {
            "HOME": str(self.home),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
            "PWD": str(self.workspace),
            "TMUX_TMPDIR": str(self.tmux_tmp),
            "XDG_DATA_HOME": str(self.home / ".local/share"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "OPENCODE_CONFIG": str(self.server_config),
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_SHARE": "1",
            "OPENCODE_DISABLE_TELEMETRY": "1",
            "DO_NOT_TRACK": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "SENTRY_DSN": "",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "",
            "OTEL_EXPORTER_OTLP_HEADERS": "",
            "HTTP_PROXY": "http://10.0.2.100:3128",
            "HTTPS_PROXY": "http://10.0.2.100:3128",
            "http_proxy": "http://10.0.2.100:3128",
            "https_proxy": "http://10.0.2.100:3128",
            "NO_PROXY": "localhost,127.0.0.1,10.0.2.100",
            "no_proxy": "localhost,127.0.0.1,10.0.2.100",
        }
        for call in calls:
            self.assertEqual(call["cwd"], str(self.workspace))
            self.assertEqual(call["env"], expected_env)
            self.assertNotIn("INHERITED_SECRET", call["env"])
            self.assertNotIn("OPENCODE_CLI_CONFIG_CONTENT", call["env"])

    def test_renderer_rejects_changed_or_ambiguous_template_anchors(self):
        with self.assertRaises(RuntimeError):
            verifier.render_test_launcher(
                contract.TUI_LAUNCHER_TEMPLATE + b"\n/bin/sh -c 'changed'",
                workspace=str(self.workspace), home=str(self.home),
                tmux_tmp=str(self.tmux_tmp), tmux_config=str(self.tmux_config),
                server_config=str(self.server_config), runner=str(self.runner),
                tmux_binary=str(self.tmux_binary), socket=self.socket,
            )

    def test_pexpect_uses_explicit_shell_argv_for_a_spaced_launcher_path(self):
        launcher = self.home / "launcher with spaces and 'quotes'"
        launcher.write_text(
            "#!/bin/sh\nprintf '%s\\n' 'pexpect-spaced-launcher-ok'\nexec /bin/sleep 3\n",
            encoding="utf-8",
        )
        launcher.chmod(0o700)
        child = verifier.spawn_pty(
            [str(launcher)], launcher_script=True, cwd=str(self.home),
            env={"PATH": "/usr/bin:/bin"}, encoding="utf-8", timeout=3,
        )
        try:
            child.expect("pexpect-spaced-launcher-ok")
            self.assertTrue(child.isalive())
        finally:
            child.close(force=True)


@unittest.skipUnless(pathlib.Path("/usr/bin/tmux").is_file(), "local tmux is unavailable")
class LocalTmuxPtyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake_opencode_dir = tempfile.TemporaryDirectory(dir="/tmp", prefix="vb-")
        cls.fake_opencode = pathlib.Path(cls.fake_opencode_dir.name) / "opencode with spaces"
        cls.fake_opencode.write_text(
            "#!/bin/sh\nprintf '%s\\n' 'fixture-tui-ready'\nexec /bin/sleep 30\n",
            encoding="utf-8",
        )
        cls.fake_opencode.chmod(0o700)

    @classmethod
    def tearDownClass(cls):
        cls.fake_opencode_dir.cleanup()

    def run_driver_case(self, *, via_launcher, config_source=None, expected_escape_time,
                        reuse_existing_session=False):
        temp = tempfile.TemporaryDirectory(dir="/tmp", prefix="v-")
        home = pathlib.Path(temp.name) / "h space"
        home.mkdir(mode=0o700)
        (home / "workspace").mkdir(mode=0o700)
        previous_binary = verifier.OPENCODE_BINARY
        verifier.OPENCODE_BINARY = str(self.fake_opencode)
        unrelated_started = False
        driver = None
        original_wait = verifier.Driver.wait

        def brief_wait(instance, seconds):
            original_wait(instance, min(seconds, 0.4))

        try:
            with mock.patch.object(verifier.Driver, "wait", brief_wait):
                driver = verifier.Driver(
                    str(home), via_tmux=not via_launcher, via_launcher=via_launcher,
                    tmux_config_source=config_source,
                    reuse_existing_session=reuse_existing_session,
                )
            self.assertTrue(
                driver.alive(),
                f"PTY driver exited: status={driver.child.exitstatus}, buffer={driver.buffer!r}",
            )
            self.assertTrue(driver.prompt_text_visible("fixture-tui-ready"))
            self.assertEqual(driver.tmux_escape_time(), expected_escape_time)

            subprocess.run(
                [driver.tmux, "-L", "unrelated-exit-confirm-test", "new-session", "-d",
                 "-s", "unrelated", "/bin/sleep", "30"],
                env=driver.env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, text=True, timeout=5, check=True,
            )
            unrelated_started = True
            private_env = driver.env
            private_socket = driver.tmux_socket
            driver.close()
            driver = None

            private = subprocess.run(
                ["/usr/bin/tmux", "-L", private_socket, "has-session"],
                env=private_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=5, check=False,
            )
            unrelated = subprocess.run(
                ["/usr/bin/tmux", "-L", "unrelated-exit-confirm-test", "has-session", "-t", "unrelated"],
                env=private_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=5, check=False,
            )
            self.assertNotEqual(private.returncode, 0, "the verifier left its private tmux socket alive")
            self.assertEqual(unrelated.returncode, 0, "cleanup affected an unrelated tmux socket")
        finally:
            if driver is not None:
                private_env = driver.env
                driver.close()
            else:
                private_env = {"PATH": "/usr/bin:/bin", "TMUX_TMPDIR": str(home / "tmux")}
            if unrelated_started:
                subprocess.run(
                    ["/usr/bin/tmux", "-L", "unrelated-exit-confirm-test", "kill-server"],
                    env=private_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=5, check=False,
                )
            verifier.OPENCODE_BINARY = previous_binary
            temp.cleanup()

    def test_via_tmux_uses_canonical_and_mutated_config_values(self):
        self.run_driver_case(
            via_launcher=False,
            expected_escape_time="escape-time 10",
        )
        mutated = contract.TMUX_CONFIG.replace(
            b"set-option -s escape-time 10\n", b"set-option -s escape-time 500\n"
        )
        self.run_driver_case(
            via_launcher=False,
            config_source=mutated,
            expected_escape_time="escape-time 500",
        )

    def test_via_launcher_repairs_500ms_using_the_canonical_launcher(self):
        self.run_driver_case(
            via_launcher=True,
            expected_escape_time="escape-time 10",
        )

    def test_via_launcher_hides_status_bar_on_existing_session(self):
        self.run_driver_case(
            via_launcher=True,
            expected_escape_time="escape-time 10",
            reuse_existing_session=True,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
