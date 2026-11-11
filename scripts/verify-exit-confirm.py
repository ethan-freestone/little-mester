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
    python3 scripts/verify-exit-confirm.py [repo-root]

Requires: Python 3 and pexpect (installed in the lab venv). Exits non-zero on
any failed check. Requires exactly OpenCode v2.2.5. The script ignores inherited
OpenCode configuration, credentials, and session variables; it supplies a minimal
private server config and overrides the CLI plugin list/keybinds.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

try:
    import pexpect
except ImportError:  # pragma: no cover - environment guard
    sys.exit("pexpect is required: use /opt/venv/bin/python or `pip install pexpect`")

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), ".."))
PLUGIN = os.path.join(ROOT, "agent", "opencode", "plugins", "little-mester-exit-confirm")

DIALOG_TEXT = "Close Little Mester?"
OPENCODE_BINARY = None
CLI_CONFIG = {
    "plugins": [PLUGIN],
    "keybinds": {"app.exit": "none", "prompt.clear": "none"},
}


class Driver:
    def __init__(self, home):
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
        self.child = pexpect.spawn(
            OPENCODE_BINARY,
            ["--standalone", workspace],
            cwd=workspace,
            env=env,
            encoding="utf-8",
            codec_errors="replace",
            dimensions=(40, 120),
            timeout=20,
        )
        self.log_path = os.path.join(home, "pty.log")
        self.log = open(self.log_path, "w", encoding="utf-8")  # noqa: SIM115
        self.buffer = ""
        self.wait(10)

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

    def ctrl(self, key):
        self.child.sendcontrol(key)
        self.wait(1.2)

    def alive(self):
        return self.child.isalive()

    def dialog_visible(self):
        return DIALOG_TEXT in self.buffer

    def close(self):
        if self.child.isalive():
            self.child.close(force=True)
        self.log.close()


def check(label, condition):
    print(("ok   " if condition else "FAIL ") + label)
    return bool(condition)


def main():
    global OPENCODE_BINARY
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
        driver = Driver(home)
        failures.append(not check("TUI started", driver.alive()))

        driver.ctrl("d")
        failures.append(not check("Ctrl+D does not exit", driver.alive()))

        driver.buffer = ""
        driver.send("yq")
        failures.append(not check("yq types with the dialog closed", driver.alive() and "yq" in driver.buffer))

        driver.buffer = ""
        driver.ctrl("c")
        failures.append(not check("Ctrl+C opens the confirmation dialog", driver.alive() and driver.dialog_visible()))

        driver.send("\x1b")
        failures.append(not check("Esc cancels without exiting", driver.alive()))

        driver.buffer = ""
        driver.ctrl("c")
        failures.append(not check("Ctrl+C reopens after cancel", driver.alive() and driver.dialog_visible()))
        driver.send("n")
        failures.append(not check("n cancels without exiting", driver.alive()))

        driver.buffer = ""
        driver.ctrl("c")
        driver.ctrl("c")
        failures.append(not check("Ctrl+C cancels without exiting", driver.alive()))

        driver.buffer = ""
        driver.ctrl("c")
        driver.send("Y")
        time.sleep(1.5)
        failures.append(not check("Y confirms and exits", not driver.alive()))
        driver.close()

        driver = Driver(home)
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
