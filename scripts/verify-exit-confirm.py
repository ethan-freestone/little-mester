#!/usr/bin/env python3
"""Live PTY verification for the Little Mester exit-confirmation plugin.

This launches a private OpenCode TUI (--standalone) with a temporary HOME and
only the plugin under test, then drives it through a pty. It checks the
behaviour that static tests cannot see:

  * Ctrl+D does not exit.
  * y/q type into the prompt while the confirmation dialog is closed.
  * Ctrl+C opens a visible "Close Little Mester?" dialog.
  * Esc, n and Ctrl+C cancel the dialog without exiting.
  * Y and Q confirm and exit.

Usage:
    python3 scripts/verify-exit-confirm.py [repo-root] [--via-tmux]
    python3 scripts/verify-exit-confirm.py [repo-root] [--via-launcher]

Requires: Python 3 and pexpect (installed in the lab venv). Exits non-zero on
any failed check. Requires exactly OpenCode v2.2.5. The script ignores inherited
OpenCode configuration, credentials, and session variables; it supplies a minimal
private server config and overrides the CLI plugin list/keybinds.
"""

import json
import importlib.util
import os
import pathlib
import shutil
import shlex
import stat
import subprocess
import sys
import tempfile
import time

try:
    import pexpect
except ImportError:  # pragma: no cover - environment guard
    sys.exit("pexpect is required: use /opt/venv/bin/python or `pip install pexpect`")

DEFAULT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ROOT = DEFAULT_ROOT
PLUGIN = os.path.join(ROOT, "agent", "opencode", "plugins", "little-mester-exit-confirm")

DIALOG_TEXT = "Close Little Mester?"
OPENCODE_BINARY = None
CLI_CONFIG = {
    "plugins": [PLUGIN],
    "keybinds": {"app.exit": "none", "prompt.clear": "none"},
}


class Driver:
    def __init__(self, home, *, via_tmux=False, via_launcher=False):
        workspace = os.path.join(home, "workspace")
        server_config = os.path.join(home, "opencode.json")
        with open(server_config, "w", encoding="utf-8") as stream:
            json.dump({"$schema": "https://opencode.ai/config.json",
                       "autoupdate": False, "share": "disabled"}, stream)
        os.chmod(server_config, 0o600)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        env.update(
            {
                "HOME": home,
                "XDG_CONFIG_HOME": os.path.join(home, ".config"),
                "XDG_DATA_HOME": os.path.join(home, ".local/share"),
                "XDG_STATE_HOME": os.path.join(home, ".local/state"),
                "XDG_CACHE_HOME": os.path.join(home, ".cache"),
                "TERM": "xterm-256color",
                "COLORTERM": "truecolor",
                "OPENCODE_CONFIG": server_config,
                "OPENCODE_CLI_CONFIG_CONTENT": json.dumps(CLI_CONFIG),
            }
        )
        self.via_tmux = via_tmux or via_launcher
        self.via_launcher = via_launcher
        self.tmux = "/usr/bin/tmux"
        self.tmux_socket = "lm-exit-confirm"
        self.tmux_session = "little-mester-opencode"
        contract_path = os.path.join(ROOT, "lib", "vm", "parent-session-contract.py")
        contract_spec = importlib.util.spec_from_file_location("exit_confirm_session_contract", contract_path)
        if contract_spec is None or contract_spec.loader is None:
            raise RuntimeError("the protected guest session contract is unavailable")
        contract = importlib.util.module_from_spec(contract_spec)
        contract_spec.loader.exec_module(contract)
        self.session_contract = contract
        self.tmux_config = os.path.join(home, "little-mester-tmux.conf")
        # Do not source host-wide or user tmux configuration in this disposable
        # test server; retain the production setting under test.
        pathlib.Path(self.tmux_config).write_bytes(b"set-option -s escape-time 10\n")
        os.chmod(self.tmux_config, 0o600)
        self.env = env
        self.child = None
        self.log_path = os.path.join(home, "pty.log")
        self.log = None
        self.buffer = ""
        if self.via_tmux:
            if not os.path.isfile(self.tmux):
                raise RuntimeError("tmux is required for a tmux-backed exit-confirm check")
            tmux_tmp = os.path.join(home, "tmux")
            try:
                os.mkdir(tmux_tmp, 0o700)
            except FileExistsError:
                info = os.lstat(tmux_tmp)
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o700):
                    raise RuntimeError("private tmux test directory has unsafe ownership or mode")
            env["TMUX_TMPDIR"] = tmux_tmp
            if via_launcher:
                self._prepare_launcher_test(home, workspace, server_config)
                try:
                    subprocess.run(
                        [self.tmux, "-L", self.tmux_socket, "-f", self.tmux_config,
                         "new-session", "-d", "-s", "launcher-bootstrap", "/bin/sleep", "300"],
                        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE, text=True, timeout=5, check=True,
                    )
                    subprocess.run(
                        [self.tmux, "-L", self.tmux_socket, "set-option", "-s", "escape-time", "500"],
                        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE, text=True, timeout=5, check=True,
                    )
                    if self.tmux_escape_time() != "escape-time 500":
                        raise RuntimeError("test tmux server did not retain the 500 ms precondition")
                except BaseException:
                    self._kill_tmux_server()
                    raise
                command = [os.path.join(home, "lm-opencode-launcher")]
            else:
                command = [
                    self.tmux, "-L", self.tmux_socket, "-f", self.tmux_config,
                    "new-session", "-A", "-s", self.tmux_session, "-c", workspace,
                    OPENCODE_BINARY, "--standalone", workspace,
                ]
        else:
            command = [OPENCODE_BINARY, "--standalone", workspace]
        try:
            self.child = pexpect.spawn(
                command[0], command[1:], cwd=workspace, env=env, encoding="utf-8",
                codec_errors="replace", dimensions=(40, 120), timeout=20,
            )
            self.log = open(self.log_path, "w", encoding="utf-8")  # noqa: SIM115
            self.wait(10)
        except BaseException:
            self.close()
            raise

    def _prepare_launcher_test(self, home, workspace, server_config):
        """Render the protected guest launcher with only its fixed paths redirected."""
        runner_path = os.path.join(home, "opencode-test-runner.py")
        runner_source = (
            "#!/usr/bin/python3\n"
            "import json, os\n"
            f"binary = {OPENCODE_BINARY!r}\n"
            f"workspace = {workspace!r}\n"
            f"cli_config = {json.dumps(CLI_CONFIG)!r}\n"
            "environment = os.environ.copy()\n"
            "environment['OPENCODE_CLI_CONFIG_CONTENT'] = cli_config\n"
            "os.execve(binary, [binary, '--standalone', workspace], environment)\n"
        )
        pathlib.Path(runner_path).write_text(runner_source, encoding="utf-8")
        os.chmod(runner_path, 0o700)

        launcher_path = os.path.join(home, "lm-opencode-launcher")
        launcher = self.session_contract.TUI_LAUNCHER_TEMPLATE.replace(
            b"@WORKSPACE@", shlex.quote(workspace).encode(),
        )
        launcher = launcher.replace(
            b"/usr/bin/python3 -I /usr/local/libexec/lm-guest-user-dirs.py prepare-auth",
            b"/usr/bin/true",
        )
        launcher = launcher.replace(
            b"/home/mester/.local/share/opencode/tmux",
            shlex.quote(os.path.join(home, "tmux")).encode(),
        )
        launcher = launcher.replace(b"/home/mester", shlex.quote(home).encode())
        launcher = launcher.replace(
            b"/etc/little-mester-tmux.conf", shlex.quote(self.tmux_config).encode(),
        )
        launcher = launcher.replace(
            b"/etc/opencode/opencode.json", shlex.quote(server_config).encode(),
        )
        launcher = launcher.replace(
            b"/usr/local/libexec/lm-opencode", shlex.quote(runner_path).encode(),
        )
        launcher = launcher.replace(
            b"/usr/bin/tmux -f",
            b"/usr/bin/tmux -L " + self.tmux_socket.encode("ascii") + b" -f",
        )
        pathlib.Path(launcher_path).write_bytes(launcher)
        os.chmod(launcher_path, 0o700)

    def wait(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            try:
                data = self.child.read_nonblocking(size=65536, timeout=0.2)
                if data:
                    self.buffer += data
                    self.log.write(data)
                    self.log.flush()
            except pexpect.TIMEOUT:
                pass
            except pexpect.EOF:
                break

    def send(self, text):
        self.child.send(text)
        self.wait(1.2)

    def send_fast(self, first, second, *, gap=0.035):
        self.child.send(first)
        time.sleep(gap)
        self.child.send(second)
        self.wait(0.25)

    def ctrl(self, key):
        self.child.sendcontrol(key)
        self.wait(1.2)

    def alive(self):
        return self.child.isalive()

    def dialog_visible(self):
        if self.via_tmux:
            result = subprocess.run(
                [self.tmux, "-L", self.tmux_socket, "capture-pane", "-p", "-t", self.tmux_session],
                env=self.env, capture_output=True, text=True, timeout=5, check=False,
            )
            return result.returncode == 0 and DIALOG_TEXT in result.stdout
        return DIALOG_TEXT in self.buffer

    def prompt_text_visible(self, text):
        if self.via_tmux:
            result = subprocess.run(
                [self.tmux, "-L", self.tmux_socket, "capture-pane", "-p", "-t", self.tmux_session],
                env=self.env, capture_output=True, text=True, timeout=5, check=False,
            )
            return result.returncode == 0 and text in result.stdout
        return text in self.buffer

    def pane_pid(self):
        result = subprocess.run(
            [self.tmux, "-L", self.tmux_socket, "display-message", "-p", "-t",
             self.tmux_session, "#{pane_pid}"],
            env=self.env, capture_output=True, text=True, timeout=5, check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def tmux_escape_time(self):
        result = subprocess.run(
            [self.tmux, "-L", self.tmux_socket, "show-options", "-s", "escape-time"],
            env=self.env, capture_output=True, text=True, timeout=5, check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def resize_and_check(self, rows, columns):
        self.child.setwinsize(rows, columns)
        deadline = time.monotonic() + 3
        observed = "unavailable"
        while time.monotonic() < deadline:
            result = subprocess.run(
                [self.tmux, "-L", self.tmux_socket, "display-message", "-p", "-t",
                 self.tmux_session, "#{client_width}x#{client_height}"],
                env=self.env, capture_output=True, text=True, timeout=5, check=False,
            )
            observed = result.stdout.strip()[:32] if result.returncode == 0 else "unavailable"
            if observed == f"{columns}x{rows}":
                return True, observed
            time.sleep(0.05)
        return False, observed

    def detach_and_reattach(self):
        if not self.via_tmux:
            raise RuntimeError("tmux detach requested without --via-tmux")
        pane_pid = self.pane_pid()
        self.child.sendcontrol("b")
        self.child.send("d")
        self.wait(0.5)
        detached = not self.child.isalive()
        if self.child.isalive():
            return False
        self.child.close()
        self.child = pexpect.spawn(
            self.tmux,
            ["-L", self.tmux_socket, "-f", self.tmux_config,
             "attach-session", "-t", self.tmux_session],
            cwd=os.path.join(self.log_path.rsplit("/", 1)[0], "workspace"),
            env=self.env, encoding="utf-8", codec_errors="replace",
            dimensions=(40, 120), timeout=20,
        )
        self.wait(0.5)
        return detached and self.child.isalive() and pane_pid == self.pane_pid()

    def close(self):
        if self.child is not None and self.child.isalive():
            self.child.close(force=True)
        if self.via_tmux:
            self._kill_tmux_server()
        if self.log is not None and not self.log.closed:
            self.log.close()

    def _kill_tmux_server(self):
        subprocess.run(
            [self.tmux, "-L", self.tmux_socket, "kill-server"], env=self.env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=5, check=False,
        )


def check(label, condition):
    print(("ok   " if condition else "FAIL ") + label)
    return bool(condition)


def main():
    global OPENCODE_BINARY
    global ROOT, PLUGIN
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo_root", nargs="?", default=DEFAULT_ROOT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--via-tmux", action="store_true",
                      help="exercise the pinned TUI through the guest's tmux config")
    mode.add_argument("--via-launcher", action="store_true",
                      help="drive the production guest launcher template through a local tmux/OpenCode PTY")
    args = parser.parse_args()
    ROOT = os.path.abspath(args.repo_root)
    PLUGIN = os.path.join(ROOT, "agent", "opencode", "plugins", "little-mester-exit-confirm")
    CLI_CONFIG["plugins"] = [PLUGIN]
    OPENCODE_BINARY = shutil.which("opencode")
    if OPENCODE_BINARY is None:
        sys.exit("opencode v2.2.5 is required on PATH")
    try:
        version = subprocess.run([OPENCODE_BINARY, "--version"], check=False,
                                 capture_output=True, text=True, timeout=5,
                                 env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
    except (OSError, subprocess.TimeoutExpired) as error:
        sys.exit(f"could not verify pinned OpenCode version: {error}")
    if version.returncode != 0 or version.stdout.strip() != "opencode v2.2.5":
        sys.exit("OpenCode v2.2.5 is required; refusing to test an unpinned binary")
    home = tempfile.mkdtemp(prefix="opencode-exit-confirm-")
    workspace = os.path.join(home, "workspace")
    os.mkdir(workspace, 0o700)
    driver = None
    failures = []
    try:
        driver = Driver(home, via_tmux=args.via_tmux, via_launcher=args.via_launcher)
        failures.append(not check("TUI started", driver.alive() and
                                  driver.prompt_text_visible("Ask anything")))
        if args.via_launcher:
            failures.append(not check(
                "production guest launcher repairs existing tmux escape-time from 500 ms to 10 ms",
                driver.tmux_escape_time() == "escape-time 10",
            ))
        elif args.via_tmux:
            failures.append(not check("tmux escape-time is 10 ms",
                                      driver.tmux_escape_time() == "escape-time 10"))
        if driver.via_tmux:
            resized, dimensions = driver.resize_and_check(32, 100)
            failures.append(not check(f"tmux PTY resize reached client ({dimensions})", resized))

        driver.ctrl("d")
        failures.append(not check("Ctrl+D does not exit", driver.alive()))

        driver.buffer = ""
        driver.send("yq")
        failures.append(not check("yq types with the dialog closed",
                                  driver.alive() and driver.prompt_text_visible("yq")))

        driver.buffer = ""
        driver.ctrl("c")
        failures.append(not check("Ctrl+C opens the confirmation dialog", driver.alive() and driver.dialog_visible()))

        # The guest's tmux client must not merge a fast Escape+n sequence into
        # an Alt+n key before OpenCode receives Escape as its own event.
        if driver.via_tmux:
            fast_escape_cancelled = True
            for trial in range(1, 11):
                driver.send_fast("\x1b", "n")
                driver.buffer = ""
                driver.ctrl("c")
                trial_cancelled = driver.alive() and driver.dialog_visible()
                fast_escape_cancelled = fast_escape_cancelled and trial_cancelled
                failures.append(not check(
                    f"fast Escape+n cancels before Ctrl+C reopens ({trial}/10)",
                    trial_cancelled))
                if not trial_cancelled and driver.alive():
                    # Ctrl+C cancelled the dialog instead of reopening it; restore
                    # the open-dialog precondition for the next deterministic trial.
                    driver.buffer = ""
                    driver.ctrl("c")
            driver.buffer = ""
            driver.ctrl("c")
            failures.append(not check("Ctrl+C cancels after fast Escape trials",
                                      driver.alive() and not driver.dialog_visible()))

            if fast_escape_cancelled:
                failures.append(not check("tmux disconnect and reattach preserve the TUI pane",
                                          driver.detach_and_reattach()))

            driver.buffer = ""
            driver.ctrl("c")
            failures.append(not check("Ctrl+C opens after tmux reattach",
                                      driver.alive() and driver.dialog_visible()))

        driver.send("\x1b")
        driver.buffer = ""
        driver.ctrl("c")
        failures.append(not check("Esc cancels and Ctrl+C reopens", driver.alive() and driver.dialog_visible()))
        driver.buffer = ""
        driver.ctrl("c")
        failures.append(not check("Ctrl+C cancels without exiting", driver.alive() and not driver.dialog_visible()))

        driver.buffer = ""
        driver.ctrl("c")
        failures.append(not check("Ctrl+C reopens after cancel", driver.alive() and driver.dialog_visible()))
        driver.buffer = ""
        driver.send("n")
        failures.append(not check("n cancels without exiting", driver.alive() and not driver.dialog_visible()))

        driver.buffer = ""
        driver.ctrl("c")
        driver.buffer = ""
        driver.ctrl("c")
        failures.append(not check("Ctrl+C cancels without exiting", driver.alive() and not driver.dialog_visible()))

        driver.buffer = ""
        driver.ctrl("c")
        driver.send("Y")
        time.sleep(1.5)
        failures.append(not check("Y confirms and exits", not driver.alive()))
        driver.close()

        driver = Driver(home, via_tmux=args.via_tmux, via_launcher=args.via_launcher)
        driver.ctrl("c")
        driver.send("Q")
        time.sleep(1.5)
        failures.append(not check("Q confirms and exits", not driver.alive()))
    finally:
        if driver:
            driver.close()
        if any(failures):
            print(f"\nPTY log kept at: {driver.log_path if driver else home}")
        else:
            shutil.rmtree(home, ignore_errors=True)

    failed = sum(failures)
    if failed:
        print(f"\n{failed} check(s) failed")
        return 1
    print("\nAll live exit-confirmation checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
