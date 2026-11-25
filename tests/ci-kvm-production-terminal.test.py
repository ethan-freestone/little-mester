#!/usr/bin/env python3
"""Deterministic safety and state-machine tests for the hosted terminal gate."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests/ci-kvm-production-terminal.py"
SPEC = importlib.util.spec_from_file_location("ci_kvm_production_terminal_tested", SCRIPT)
gate = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(gate)


def observation(*, dialog=False, clients=1, width=100, height=32):
    return {
        "server_available": True,
        "sessions": [{"session_id": "$4", "session_name": gate.SESSION_NAME, "windows": 1}],
        "panes": [{"session_id": "$4", "session_name": gate.SESSION_NAME,
                   "window_id": "@5", "pane_id": "%6", "pid": 7654,
                   "dead": "0", "width": width, "height": height}],
        "clients": ([{"session_name": gate.SESSION_NAME, "width": width, "height": height}]
                    * clients),
        "escape_time": "escape-time 10", "global_status": "off",
        "session_status": "off", "dialog_visible": dialog,
        "idle_prompt_visible": not dialog,
    }


class EvidenceLedgerTests(unittest.TestCase):
    def test_private_bounded_ledger_retains_first_failure_and_marks_required_skips(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = pathlib.Path(temp)
            os.chmod(directory, 0o700)
            ledger = gate.EvidenceLedger(directory, "a" * 40)
            self.assertEqual(os.stat(ledger.path).st_mode & 0o777, 0o600)

            with contextlib.redirect_stdout(io.StringIO()):
                ledger.check("hosted_prerequisites", False, reason="test refusal")
                ledger.skip_unattempted()
            ledger.close()

            entries = [json.loads(line) for line in ledger.path.read_text().splitlines()]
            self.assertEqual(ledger.first_failure, "hosted_prerequisites")
            self.assertEqual(len(ledger.completed), len(gate.REQUIRED_CHECKS))
            self.assertEqual(entries[1]["status"], "FAIL")
            self.assertTrue(all(
                entry.get("status") == "SKIP" and entry.get("required") is True
                for entry in entries[2:]
            ))
            self.assertLessEqual(ledger.bytes_written, gate.EVIDENCE_MAX_BYTES)

    def test_credential_failure_reports_only_a_fixed_prerequisite_code(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = gate.EvidenceLedger(pathlib.Path(temp), "a" * 40)
            instance = gate.ProductionTerminalGate(
                ROOT, pathlib.Path("/srv/little-mester-ci"), "a" * 40, ledger, env={})
            output = io.StringIO()
            with contextlib.redirect_stdout(output), self.assertRaises(gate.GateStop):
                instance._must(
                    "credentials",
                    lambda: (_ for _ in ()).throw(
                        gate.CredentialPreflightError("control_key_lstat")))
            ledger.close()

            self.assertIn(
                "production_terminal_credential=FAIL check=control_key_lstat",
                output.getvalue())
            self.assertNotIn("/etc/little-mester-vm", output.getvalue())
            records = [json.loads(line) for line in ledger.path.read_text().splitlines()]
            self.assertEqual(records[1]["reason"], "control_key_lstat")

    def test_credential_diagnostic_rejects_unlisted_or_sensitive_values(self):
        for value in ("/etc/little-mester-vm/control_ed25519", "token=secret"):
            with self.subTest(value=value), self.assertRaises(gate.TerminalGateError):
                gate.CredentialPreflightError(value)

    def test_ledger_rejects_unbounded_or_unknown_check_records(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = gate.EvidenceLedger(pathlib.Path(temp), "b" * 40)
            with self.assertRaises(gate.TerminalGateError):
                ledger.check("not-a-required-check", True)
            with self.assertRaises(gate.TerminalGateError):
                ledger.record("test", "oversized", value="x" * gate.EVIDENCE_MAX_LINE)
            ledger.close()


class HostedGuardTests(unittest.TestCase):
    def test_local_invocation_fails_closed_before_kvm_probe(self):
        with (mock.patch.object(gate.pathlib.Path, "is_char_device") as kvm,
              self.assertRaisesRegex(gate.TerminalGateError, "GitHub Actions")):
            gate.verify_hosted_prerequisites(ROOT, env={})
        kvm.assert_not_called()

    def test_hosted_guard_requires_read_write_kvm_and_exact_checkout(self):
        env = {
            "GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
            "RUNNER_OS": "Linux", "RUNNER_ARCH": "X64",
            "GITHUB_WORKSPACE": str(ROOT),
        }
        with (mock.patch.object(gate.pathlib.Path, "is_char_device", return_value=True),
              mock.patch.object(gate.os, "access", return_value=True)):
            result = gate.verify_hosted_prerequisites(ROOT, env=env)
        self.assertEqual(result["kvm"], "read-write")

        with (mock.patch.object(gate.pathlib.Path, "is_char_device", return_value=False),
              mock.patch.object(gate.os, "access", return_value=True),
              self.assertRaisesRegex(gate.TerminalGateError, "KVM")):
            gate.verify_hosted_prerequisites(ROOT, env=env)

        env["GITHUB_WORKSPACE"] = str(ROOT / "other")
        with (mock.patch.object(gate.pathlib.Path, "is_char_device", return_value=True),
              mock.patch.object(gate.os, "access", return_value=True),
              self.assertRaisesRegex(gate.TerminalGateError, "candidate checkout")):
            gate.verify_hosted_prerequisites(ROOT, env=env)


class CredentialPreflightTests(unittest.TestCase):
    def test_database_group_membership_does_not_substitute_for_effective_access(self):
        group = SimpleNamespace(gr_gid=42, gr_mem=["runner"])
        with mock.patch.object(gate.os, "getuid", return_value=1001), \
             mock.patch.object(gate.os, "geteuid", return_value=1001), \
             mock.patch.object(gate.grp, "getgrnam", return_value=group), \
             mock.patch.object(gate.os, "getgroups", return_value=[]), \
             mock.patch.object(gate.os, "getegid", return_value=100), \
             mock.patch.object(gate.os, "lstat") as lstat, \
             self.assertRaises(gate.CredentialPreflightError) as raised:
            gate.verify_credentials_and_host_tools()

        self.assertEqual(raised.exception.check, "effective_control_group")
        lstat.assert_not_called()

    def test_key_traversal_failure_has_a_fixed_preflight_code(self):
        group = SimpleNamespace(gr_gid=42)
        runtime = SimpleNamespace(st_mode=gate.stat.S_IFDIR | 0o700, st_uid=1001)
        with mock.patch.object(gate.os, "getuid", return_value=1001), \
             mock.patch.object(gate.os, "geteuid", return_value=1001), \
             mock.patch.object(gate.grp, "getgrnam", return_value=group), \
             mock.patch.object(gate.os, "getgroups", return_value=[42]), \
             mock.patch.object(gate.os, "getegid", return_value=100), \
             mock.patch.object(gate.os, "lstat",
                               side_effect=[runtime, PermissionError("sensitive detail")]), \
             self.assertRaises(gate.CredentialPreflightError) as raised:
            gate.verify_credentials_and_host_tools()

        self.assertEqual(raised.exception.check, "control_key_lstat")
        self.assertEqual(str(raised.exception), "credential preflight failed")

    def test_root_effective_uid_cannot_substitute_for_runner_identity(self):
        with mock.patch.object(gate.os, "getuid", return_value=1001), \
             mock.patch.object(gate.os, "geteuid", return_value=0), \
             mock.patch.object(gate.grp, "getgrnam",
                               return_value=SimpleNamespace(gr_gid=42)), \
             mock.patch.object(gate.os, "lstat") as lstat, \
             self.assertRaises(gate.CredentialPreflightError) as raised:
            gate.verify_credentials_and_host_tools()

        self.assertEqual(raised.exception.check, "effective_runner_uid")
        lstat.assert_not_called()


class HostTerminalPreflightTests(unittest.TestCase):
    def test_host_prerequisite_failure_emits_only_fixed_id(self):
        output = io.StringIO()
        with mock.patch.object(gate.sys, "argv", [str(SCRIPT), "--host-preflight"]), \
             mock.patch.object(gate, "verify_host_terminal_tools",
                               side_effect=gate.CredentialPreflightError("python_pexpect")), \
             contextlib.redirect_stdout(output):
            result = gate.main()

        self.assertEqual(result, 1)
        self.assertEqual(output.getvalue(),
                         "production_terminal_preflight=FAIL check=python_pexpect\n")


class CandidatePreflightTests(unittest.TestCase):
    def test_candidate_requires_exact_sha_clean_checkout_and_matching_cli_blobs(self):
        sha = "c" * 40
        with tempfile.TemporaryDirectory() as temp:
            repo = pathlib.Path(temp)
            files = {"lab": b"#!/bin/sh\n", "tools/vm-owner-acceptance": b"#!/usr/bin/python3\n"}
            for name, payload in files.items():
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                path.chmod(0o755)
            env = {"GITHUB_SHA": sha}
            completed = lambda payload=b"", code=0: SimpleNamespace(
                returncode=code, stdout=payload)
            with mock.patch.object(gate, "_run", side_effect=[
                    completed((sha + "\n").encode()), completed(),
                    completed(files["lab"]), completed(files["tools/vm-owner-acceptance"])]):
                self.assertTrue(gate.verify_candidate(repo, sha, env)["checkout_clean"])

        with mock.patch.object(gate, "_run", return_value=completed(b"d" * 40 + b"\n")), \
                self.assertRaisesRegex(gate.TerminalGateError, "HEAD"):
            gate.verify_candidate(repo, sha, env)

    def test_live_entrypoint_fails_closed_locally_and_retains_private_evidence(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            env = {"RUNNER_TEMP": temp}
            with self.assertRaisesRegex(gate.TerminalGateError, "hosted_prerequisites"):
                gate.run_production_terminal_acceptance(
                    ROOT, pathlib.Path("/srv/little-mester-ci"), "e" * 40, env=env)
            directories = list(pathlib.Path(temp).iterdir())
            self.assertEqual(len(directories), 1)
            directory = directories[0]
            self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)
            evidence = directory / "production-terminal-evidence.jsonl"
            self.assertEqual(os.stat(evidence).st_mode & 0o777, 0o600)
            records = [json.loads(line) for line in evidence.read_text().splitlines()]
            self.assertEqual(records[0]["candidate_sha"], "e" * 40)
            self.assertEqual(records[-1]["first_failure"], "hosted_prerequisites")


class PublicCliAndPtyTests(unittest.TestCase):
    def test_public_cli_command_is_the_singleton_route_with_disposable_prefix(self):
        argv = gate.public_cli_command(ROOT, pathlib.Path("/srv/little-mester-ci"))
        self.assertEqual(argv, [str(ROOT / "lab"), "vm", "system", "tui", "--prefix",
                                "/srv/little-mester-ci"])
        self.assertNotIn("--via-tmux", argv)
        self.assertNotIn("tui-key", argv)

    def test_host_pty_spawns_sanitized_public_cli_under_control_group(self):
        class FakeChild:
            def __init__(self):
                self.delaybeforesend = 0.05

            def isalive(self):
                return True

            def close(self, force=False):
                pass

        calls = []
        fake_pexpect = SimpleNamespace(spawn=lambda *args, **kwargs: (
            calls.append((args, kwargs)) or FakeChild()))
        secret_env = {"OPENAI_API_KEY": "must-not-cross-the-PTY-boundary",
                      "GITHUB_TOKEN": "also-not-forwarded"}
        with mock.patch.dict(os.environ, secret_env, clear=True), \
                mock.patch.dict("sys.modules", {"pexpect": fake_pexpect}):
            client = gate.HostPtyClient(ROOT, pathlib.Path("/srv/little-mester-ci"))
        (argv, kwargs), = calls
        self.assertEqual(argv[0], "/usr/bin/sg")
        self.assertEqual(argv[1][:2], ["lm-vm-control", "-c"])
        shell_command = argv[1][2]
        self.assertEqual(shell_command, "exec " + __import__("shlex").join(
            [str(ROOT / "lab"), "vm", "system", "tui", "--prefix",
             "/srv/little-mester-ci"]))
        self.assertEqual(kwargs["dimensions"], (32, 100))
        self.assertIsNone(kwargs["encoding"])
        self.assertEqual(set(kwargs["env"]), {"PATH", "HOME", "TERM", "LANG", "LC_ALL"})
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertNotIn("GITHUB_TOKEN", kwargs["env"])
        client.close()

    def test_fast_escape_write_timing_includes_both_writes_and_excludes_drain(self):
        class Clock:
            now = 100

            @classmethod
            def monotonic_ns(cls):
                return cls.now

            @classmethod
            def sleep(cls, seconds):
                cls.now += round(seconds * 1_000_000_000)

        class Child:
            delaybeforesend = 0.05

            def __init__(self):
                self.sent = []

            def isalive(self):
                return True

            def send(self, value):
                self.sent.append((value, self.delaybeforesend))
                Clock.now += 1_000_000

        child = Child()
        client = object.__new__(gate.HostPtyClient)
        client.child = child
        with (mock.patch.object(gate.time, "monotonic_ns", side_effect=Clock.monotonic_ns),
              mock.patch.object(gate.time, "sleep", side_effect=Clock.sleep)):
            elapsed = client.send_fast_escape_n()
        self.assertEqual(child.sent, [(b"\x1b", None), (b"n", None)])
        self.assertEqual(elapsed, 32_000_000)
        self.assertEqual(child.delaybeforesend, 0.05)
        self.assertLessEqual(elapsed, gate.FAST_ESCAPE_MAX_NS)

    def test_clean_tui_environment_has_no_inherited_secrets(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "TERM": "bad-term"}):
            env = gate._clean_tui_environment()
        self.assertEqual(env["TERM"], "xterm-256color")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("TERMINFO", env)


class FastEscapeStateMachineTests(unittest.TestCase):
    class FakeObserver:
        def __init__(self, *, dialog=True, fail=False):
            self.value = observation(dialog=dialog)
            self.fail = fail

        def observe(self):
            if self.fail:
                raise OSError("hidden test-only observer failure")
            return self.value

    class FakeClient:
        def __init__(self, observer, *, elapsed_ns, cancel=True):
            self.observer = observer
            self.elapsed_ns = elapsed_ns
            self.cancel = cancel
            self.sends = 0

        def drain(self):
            pass

        def alive(self):
            return True

        def send_control(self, key):
            if key == "c":
                self.observer.value = observation(
                    dialog=not self.observer.value["dialog_visible"])

        def send_fast_escape_n(self):
            self.sends += 1
            if self.cancel:
                self.observer.value = observation(dialog=False)
            return self.elapsed_ns

    def make_gate(self, directory, *, elapsed_ns, cancel=True, observer_fail=False):
        os.chmod(directory, 0o700)
        ledger = gate.EvidenceLedger(directory, "a" * 40)
        observer = self.FakeObserver(fail=observer_fail)
        client = self.FakeClient(observer, elapsed_ns=elapsed_ns, cancel=cancel)
        instance = gate.ProductionTerminalGate(
            ROOT, pathlib.Path("/srv/little-mester-ci"), "a" * 40, ledger,
        )
        instance.observer = observer
        instance.client = client
        instance.identity = gate._identity(observer.value)
        return instance, ledger, observer, client

    def test_timing_failures_do_not_block_independent_cancellation_checks(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            instance, ledger, _observer, client = self.make_gate(
                pathlib.Path(temp), elapsed_ns=gate.FAST_ESCAPE_MAX_NS + 1)
            instance._run_fast_escape_trials()
            self.assertEqual(client.sends, 10)
            self.assertEqual(ledger.first_failure, "fast_escape_timing_01")
            for trial in range(1, 11):
                self.assertIn(f"fast_escape_timing_{trial:02d}", ledger.completed)
                self.assertIn(f"fast_escape_cancel_{trial:02d}", ledger.completed)
            entries = [json.loads(line) for line in ledger.path.read_text().splitlines()]
            states = {entry["name"]: entry["status"] for entry in entries
                      if entry["kind"] == "check"}
            self.assertTrue(all(states[f"fast_escape_timing_{i:02d}"] == "FAIL"
                                for i in range(1, 11)))
            self.assertTrue(all(states[f"fast_escape_cancel_{i:02d}"] == "PASS"
                                for i in range(1, 11)))
            ledger.close()

    def test_cancellation_failures_do_not_rewrite_successful_timing(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            instance, ledger, _observer, client = self.make_gate(
                pathlib.Path(temp), elapsed_ns=30_000_000, cancel=False)
            class FakeClock:
                now = 0.0

                @classmethod
                def monotonic(cls):
                    return cls.now

                @classmethod
                def sleep(cls, seconds):
                    cls.now += seconds

            with (mock.patch.object(gate.time, "monotonic", side_effect=FakeClock.monotonic),
                  mock.patch.object(gate.time, "sleep", side_effect=FakeClock.sleep)):
                instance._run_fast_escape_trials()
            self.assertEqual(client.sends, 10)
            self.assertEqual(ledger.first_failure, "fast_escape_cancel_01")
            entries = [json.loads(line) for line in ledger.path.read_text().splitlines()]
            states = {entry["name"]: entry["status"] for entry in entries
                      if entry["kind"] == "check"}
            self.assertTrue(all(states[f"fast_escape_timing_{i:02d}"] == "PASS"
                                for i in range(1, 11)))
            self.assertTrue(all(states[f"fast_escape_cancel_{i:02d}"] == "FAIL"
                                for i in range(1, 11)))
            ledger.close()

    def test_unavailable_trial_state_makes_every_remaining_assertion_required_failure(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            instance, ledger, _observer, client = self.make_gate(
                pathlib.Path(temp), elapsed_ns=30_000_000, observer_fail=True)
            instance._run_fast_escape_trials()
            self.assertEqual(client.sends, 0)
            self.assertEqual(ledger.first_failure, "fast_escape_timing_01")
            entries = [json.loads(line) for line in ledger.path.read_text().splitlines()]
            skips = [entry for entry in entries if entry.get("required_unattempted")]
            self.assertEqual(len(skips), 20)
            self.assertTrue(all(entry["status"] == "FAIL" for entry in skips))
            ledger.close()


class GuestObservationTests(unittest.TestCase):
    def test_observation_identity_requires_one_live_session_and_pane(self):
        value = observation()
        gate._validate_observation(value)
        identity = gate._identity(value)
        self.assertEqual(identity, {"session_id": "$4", "session_name": gate.SESSION_NAME,
                                    "pane_id": "%6", "pane_pid": 7654})
        self.assertTrue(gate._same_live_identity(value, identity))

        changed = observation()
        changed["panes"][0]["pid"] += 1
        self.assertFalse(gate._same_live_identity(changed, identity))
        changed = observation()
        changed["panes"][0]["dead"] = "1"
        self.assertFalse(gate._same_live_identity(changed, identity))
        changed = observation(clients=2)
        self.assertTrue(gate._same_live_identity(changed, identity))
        instance = object.__new__(gate.ProductionTerminalGate)
        self.assertFalse(instance._attached(changed, identity))

    def test_guest_observer_is_strictly_read_only_and_bounded(self):
        for query in ("list-sessions", "list-panes", "list-clients", "capture-pane",
                      "show-options"):
            self.assertIn(query, gate.REMOTE_OBSERVER)
        for mutation in ("send-keys", "set-option", "kill-session", "new-session",
                         "attach-session", "detach-client"):
            self.assertNotIn(mutation, gate.REMOTE_OBSERVER)
        self.assertIn("_guest_ssh(command, timeout=15, capture_limit=8192)",
                      gate.OBSERVATION_WRAPPER)
        self.assertNotIn("tty=True", gate.OBSERVATION_WRAPPER)

    def test_malformed_guest_observation_fails_closed(self):
        value = observation()
        value["panes"][0]["pane_id"] = "invalid;run-shell"
        with self.assertRaisesRegex(gate.TerminalGateError, "pane identity"):
            gate._validate_observation(value)

    def test_detached_identity_uses_bounded_read_only_observation_retries(self):
        class Observer:
            def __init__(self):
                self.values = [observation(clients=1), observation(clients=0)]

            def observe(self):
                return self.values.pop(0)

        instance = object.__new__(gate.ProductionTerminalGate)
        instance.identity = gate._identity(observation())
        instance.observer = Observer()
        with (mock.patch.object(gate.time, "monotonic", side_effect=(0, 0.1, 0.2)),
              mock.patch.object(gate.time, "sleep")):
            detached = instance._detached_identity()
        self.assertIsNotNone(detached)
        self.assertEqual(detached["clients"], [])


class WorkflowWiringTests(unittest.TestCase):
    def test_live_gate_runs_after_shared_acceptance_and_before_forced_stop(self):
        source = (ROOT / "tests/ci-kvm-system-unit.py").read_text()
        main = source[source.index("def main()") :]
        self.assertIn("def invoke_production_terminal_acceptance()", source)
        self.assertIn("invoke_production_terminal_acceptance()", main)
        self.assertLess(main.index("invoke_shared_acceptance(sha)"),
                        main.index("invoke_production_terminal_acceptance()"))
        self.assertLess(main.index("invoke_production_terminal_acceptance()"),
                        main.index("force_systemd_fallback(installed_root)"))
        self.assertIn('"production_terminal_regression"', main)
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        self.assertIn("python3 tests/ci-kvm-production-terminal.test.py", workflow)

    def test_driver_is_not_a_protected_runtime_asset(self):
        candidate = (ROOT / "tests/ci-kvm-production-terminal.py").read_text()
        self.assertIn("CI orchestration, not an installed/runtime asset", candidate)
        for relative in ("tools/vm-owner-acceptance", "tools/vm-system-install",
                         "lab-vm", "lib/vm/system-acceptance.py"):
            self.assertNotIn("tests/ci-kvm-production-terminal.py",
                             (ROOT / relative).read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
