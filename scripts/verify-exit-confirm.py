#!/usr/bin/env python3
"""Live PTY verification for the Little Mester exit-confirmation plugin.

This launches a private OpenCode TUI (--standalone) with a temporary HOME and
only the plugin under test, then drives it through a pty. It checks the
behaviour that static tests cannot see:

  * Ctrl+D does not exit.
  * y/q type into the prompt while the confirmation dialog is closed.
  * Ctrl+C opens a visible "Close Little Mester?" dialog.
  * Esc, n and Ctrl+C cancel the dialog without exiting.
  * q confirms and exits.

Usage:
    python3 scripts/verify-exit-confirm.py [repo-root]

Requires: Python 3 and pexpect (installed in the lab venv). Exits non-zero on
any failed check. Set OPENCODE_CONFIG to override the server config; the script
only overrides the CLI plugin list and keybinds.
"""

import json
import os
import shutil
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
CLI_CONFIG = {
    "plugins": [PLUGIN],
    "keybinds": {"app.exit": "none", "prompt.clear": "none"},
}


class Driver:
    def __init__(self, home):
        env = dict(os.environ)
        for key in ("OPENCODE_SESSION_ID", "OPENCODE", "OPENCODE_TERMINAL", "OPENCODE_CLI_CONFIG_CONTENT"):
            env.pop(key, None)
        env.update(
            {
                "HOME": home,
                "XDG_CONFIG_HOME": os.path.join(home, ".config"),
                "XDG_DATA_HOME": os.path.join(home, ".local/share"),
                "XDG_STATE_HOME": os.path.join(home, ".local/state"),
                "XDG_CACHE_HOME": os.path.join(home, ".cache"),
                "TERM": "xterm-256color",
                "COLORTERM": "truecolor",
                "OPENCODE_CLI_CONFIG_CONTENT": json.dumps(CLI_CONFIG),
            }
        )
        self.child = pexpect.spawn(
            "opencode",
            ["--standalone", ROOT],
            cwd=ROOT,
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
    home = tempfile.mkdtemp(prefix="opencode-exit-confirm-")
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
        driver.send("q")
        time.sleep(1.5)
        failures.append(not check("q confirms and exits", not driver.alive()))
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
