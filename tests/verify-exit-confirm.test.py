#!/usr/bin/env python3
"""Focused regression tests for the protected-launcher PTY verifier."""

import importlib.util
import json
import pathlib
import shlex
import subprocess
import sys
import tempfile
import unittest
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


class IsolatedTmuxConfigTests(unittest.TestCase):
    def test_canonical_config_is_only_stripped_of_the_two_host_sources(self):
        expected = b"set-option -s escape-time 10\n"
        self.assertEqual(verifier.isolated_tmux_config(contract.TMUX_CONFIG), expected)

    def test_non_source_mutations_and_bytes_are_preserved(self):
        canonical = contract.TMUX_CONFIG
        mutated = canonical.replace(
            b"set-option -s escape-time 10\n",
            b"# keep this comment\nset-option -s escape-time 500\nset-option -g status-keys vi\n",
        )
        self.assertEqual(
            verifier.isolated_tmux_config(mutated),
            b"# keep this comment\nset-option -s escape-time 500\nset-option -g status-keys vi\n",
        )

    def test_supported_lines_and_missing_final_newline_are_preserved(self):
        cases = (
            (b"set-option -s escape-time 500", b"set-option -s escape-time 500"),
            (b"set-option -s escape-time 0\n", b"set-option -s escape-time 0\n"),
            (b"set-option -s escape-time 12345678901234567890\n",
             b"set-option -s escape-time 12345678901234567890\n"),
            (b"set-option -g status-keys vi\n", b"set-option -g status-keys vi\n"),
            (b"set-option -g status-keys emacs", b"set-option -g status-keys emacs"),
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
        self.assertEqual(len(calls), 3)

        prefix = ["-L", self.socket, "-f", str(self.tmux_config)]
        self.assertEqual(
            [call["argv"] for call in calls],
            [
                prefix + ["show-options", "-s", "escape-time"],
                prefix + ["set-option", "-s", "escape-time", "10"],
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

    def run_driver_case(self, *, via_launcher, config_source=None, expected_escape_time):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
