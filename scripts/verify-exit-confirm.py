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

import importlib.util
import json
import os
import pathlib
import re
import shlex
import shutil
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


_EXPECTED_TMUX_SOURCES = {
    b"source-file -q /etc/tmux.conf\n",
    b"source-file -q ~/.tmux.conf\n",
}
_TMUX_ALLOWED_SET_OPTIONS = (
    re.compile(rb"set-option -s escape-time [0-9]+"),
    re.compile(rb"set-option -g status-keys (?:vi|emacs)"),
)
_TMUX_COMMENT = re.compile(rb" *#[\x20-\x7e]*")


def isolated_tmux_config(canonical_config):
    """Retain only the deliberately small, byte-exact tmux config grammar."""
    if not isinstance(canonical_config, bytes):
        raise TypeError("canonical tmux config must be bytes")

    lines = canonical_config.split(b"\n")
    final_newline = canonical_config.endswith(b"\n")
    if final_newline:
        lines.pop()

    retained = []
    for index, body in enumerate(lines):
        has_newline = index < len(lines) - 1 or final_newline
        line = body + (b"\n" if has_newline else b"")
        if line in _EXPECTED_TMUX_SOURCES:
            continue

        if body == b"" or any(pattern.fullmatch(body) for pattern in _TMUX_ALLOWED_SET_OPTIONS):
            retained.append(line)
            continue
        if _TMUX_COMMENT.fullmatch(body) and b"\\" not in body:
            retained.append(line)
            continue
        raise ValueError(f"unsupported tmux config line: {line!r}")

    return b"".join(retained)


def _replace_once(template, anchor, replacement, label):
    count = template.count(anchor)
    if count != 1:
        raise RuntimeError(f"expected exactly one {label} template anchor; found {count}")
    return template.replace(anchor, replacement, 1)


def _shell_word(value):
    return shlex.quote(os.fspath(value)).encode("utf-8")


def render_test_launcher(template, *, workspace, home, tmux_tmp, tmux_config,
                         server_config, runner, tmux_binary, socket):
    """Render redirected paths into the exact protected launcher template.

    The workspace and environment assignments belong to the outer launcher
    shell. The tmux and runner paths belong to the /bin/sh -c payload, which is
    itself quoted as one outer-shell word after its inner arguments are built.
    """
    if not isinstance(template, bytes):
        raise TypeError("launcher template must be bytes")

    launcher = _replace_once(
        template, b"@WORKSPACE@", _shell_word(workspace), "workspace",
    )
    launcher = _replace_once(
        launcher,
        b"/usr/bin/python3 -I /usr/local/libexec/lm-guest-user-dirs.py prepare-auth",
        b"/usr/bin/true",
        "auth preparation",
    )
    for anchor, value, label in (
        (b" HOME=/home/mester ", b" HOME=" + _shell_word(home) + b" ", "HOME"),
        (b" TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux ",
         b" TMUX_TMPDIR=" + _shell_word(tmux_tmp) + b" ", "TMUX_TMPDIR"),
        (b" XDG_DATA_HOME=/home/mester/.local/share ",
         b" XDG_DATA_HOME=" + _shell_word(os.path.join(os.fspath(home), ".local/share")) + b" ",
         "XDG_DATA_HOME"),
        (b" XDG_CONFIG_HOME=/home/mester/.config ",
         b" XDG_CONFIG_HOME=" + _shell_word(os.path.join(os.fspath(home), ".config")) + b" ",
         "XDG_CONFIG_HOME"),
        (b" OPENCODE_CONFIG=/etc/opencode/opencode.json ",
         b" OPENCODE_CONFIG=" + _shell_word(server_config) + b" ", "OPENCODE_CONFIG"),
    ):
        launcher = _replace_once(launcher, anchor, value, label)

    command_start = b"/bin/sh -c '"
    command_end = b"' lm-opencode-launcher \"$@\""
    if launcher.count(command_start) != 1 or launcher.count(command_end) != 1:
        raise RuntimeError("expected exact protected-launcher /bin/sh -c anchors")
    start = launcher.index(command_start)
    payload_start = start + len(command_start)
    payload_end = launcher.find(command_end, payload_start)
    if payload_end < 0:
        raise RuntimeError("protected-launcher /bin/sh -c anchors are out of order")

    payload = launcher[payload_start:payload_end]
    tmux_anchor = b"/usr/bin/tmux -f /etc/little-mester-tmux.conf"
    if payload.count(tmux_anchor) != 3:
        raise RuntimeError("expected three exact tmux command anchors in launcher payload")
    tmux_command = b" ".join(
        _shell_word(value)
        for value in (tmux_binary, "-L", socket, "-f", tmux_config)
    )
    payload = payload.replace(tmux_anchor, tmux_command)
    # tmux passes its shell-command argument to a pane shell. First quote the
    # runner for that pane shell, then quote the resulting command string for
    # the /bin/sh -c payload that invokes tmux.
    pane_command = shlex.quote(os.fspath(runner))
    payload = _replace_once(
        payload, b"/usr/local/libexec/lm-opencode", _shell_word(pane_command),
        "OpenCode runner",
    )

    # Quote the completed inner script for the outer launcher's shell. This
    # second quoting layer is required because inner shell words can contain
    # apostrophes, spaces, dollar signs, and shell metacharacters.
    new_command = (
        b"/bin/sh -c " + _shell_word(payload.decode("utf-8"))
        + b" lm-opencode-launcher \"$@\""
    )
    command_end_index = payload_end + len(command_end)
    return launcher[:start] + new_command + launcher[command_end_index:]


def spawn_pty(command, *, launcher_script=False, **kwargs):
    """Spawn direct argv normally, or an explicit /bin/sh argv for a launcher."""
    if launcher_script:
        # A one-element args list avoids pexpect's empty-args command splitting,
        # which treats a launcher path containing spaces as multiple words.
        return pexpect.spawn("/bin/sh", list(command), **kwargs)
    return pexpect.spawn(command[0], command[1:], **kwargs)


class Driver:
    def __init__(self, home, *, via_tmux=False, via_launcher=False,
                 tmux_config_source=None):
        contract_path = os.path.join(ROOT, "lib", "vm", "parent-session-contract.py")
        contract_spec = importlib.util.spec_from_file_location("exit_confirm_session_contract", contract_path)
        if contract_spec is None or contract_spec.loader is None:
            raise RuntimeError("the protected guest session contract is unavailable")
        contract = importlib.util.module_from_spec(contract_spec)
        contract_spec.loader.exec_module(contract)
        self.session_contract = contract
        config_source = contract.TMUX_CONFIG if tmux_config_source is None else tmux_config_source
        isolated_config = isolated_tmux_config(config_source)

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
        self.tmux_config = os.path.join(home, "little-mester-tmux.conf")
        pathlib.Path(self.tmux_config).write_bytes(isolated_config)
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
                pane_command = " ".join(shlex.quote(argument) for argument in (
                    OPENCODE_BINARY, "--standalone", workspace,
                ))
                command = [
                    self.tmux, "-L", self.tmux_socket, "-f", self.tmux_config,
                    "new-session", "-A", "-s", self.tmux_session, "-c", workspace,
                    pane_command,
                ]
        else:
            command = [OPENCODE_BINARY, "--standalone", workspace]
        try:
            self.child = spawn_pty(
                command, launcher_script=via_launcher, cwd=workspace, env=env, encoding="utf-8",
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
        launcher = render_test_launcher(
            self.session_contract.TUI_LAUNCHER_TEMPLATE,
            workspace=workspace,
            home=home,
            tmux_tmp=os.path.join(home, "tmux"),
            tmux_config=self.tmux_config,
            server_config=server_config,
            runner=runner_path,
            tmux_binary=self.tmux,
            socket=self.tmux_socket,
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
