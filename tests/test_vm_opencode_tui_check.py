"""Fixture-only /proc process identity and controlling-terminal checks."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts/vm-opencode-tui-check.py"


class TuiCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc = Path(self.temp.name) / "proc"
        self.proc.mkdir()

    def add_process(self, pid, *, executable, argv, tty=0, stat=True):
        process = self.proc / str(pid)
        process.mkdir()
        (process / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")
        (process / "exe").symlink_to(executable)
        if stat:
            # tty_nr is field 7 (index 4 after state/ppid/pgrp/session).
            (process / "stat").write_text(f"{pid} (proc (with parens)) S 1 2 3 {tty} 6\n", encoding="ascii")

    def run_check(self):
        return subprocess.run(
            ["python3", "-B", str(CHECKER), "--proc-root", str(self.proc), "--json"],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_empty_process_table_is_idle(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"busy": False, "count": 0})

    def test_opencode_executable_with_controlling_tty_is_busy(self):
        self.add_process(101, executable="/usr/local/bin/opencode", argv=["opencode", "tui"], tty=34816)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), {"busy": True, "count": 1})

    def test_node_entrypoint_uses_full_argv_components(self):
        self.add_process(
            202,
            executable="/usr/bin/node",
            argv=["node", "/usr/lib/node_modules/@opencode/cli/bin/opencode.js", "run"],
            tty=1,
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["busy"], True)

    def test_lookalike_command_without_opencode_identity_is_ignored(self):
        self.add_process(303, executable="/usr/bin/bash", argv=["bash", "-c", "echo opencode"], tty=1)
        self.add_process(304, executable="/usr/local/bin/opencode", argv=["opencode", "--version"], tty=0)
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"busy": False, "count": 0})

    def test_unreadable_candidate_tty_fails_closed(self):
        self.add_process(405, executable="/usr/local/bin/opencode", argv=["opencode"], stat=False)
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), {"busy": True, "count": 1})


if __name__ == "__main__":
    unittest.main()
