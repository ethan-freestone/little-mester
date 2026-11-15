#!/usr/bin/env python3
"""Exercise the hosted SSH TUI capture wrapper against PTY terminal output."""

from __future__ import annotations

import pathlib
import shlex
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CAPTURE = ROOT / "tests/ci-tui-capture.sh"


class TuiCaptureIntegrationTests(unittest.TestCase):
    def test_fake_ssh_terminal_device_output_never_reaches_public_stdout(self):
        with tempfile.TemporaryDirectory(prefix="ci-tui-fake-ssh-") as directory:
            root = pathlib.Path(directory)
            capture_dir = root / "capture"
            retained = root / "retained"
            capture_dir.mkdir(mode=0o700)
            retained.mkdir(mode=0o700)
            writer = root / "fake_ssh_tty.py"
            writer.write_text(
                "import os\n"
                "fd = os.open('/dev/tty', os.O_WRONLY)\n"
                "os.write(fd, b'\\x1b[32mAsk anything: private-ssh-pty-secret\\x1b[0m\\n')\n"
                "os.close(fd)\n",
                encoding="utf-8",
            )
            script = f'''set -euo pipefail
source {CAPTURE!s}
fail() {{ printf 'PUBLIC_FAILURE=%s\\n' "$1"; exit 1; }}
ssh_tty_guest() {{
  script -qefc {shlex.quote(f'python3 {writer}')} /dev/null
  return 124
}}
capture_tui_probe reattach 4 {capture_dir!s} {retained!s}
'''
            result = subprocess.run(
                ["bash", "-c", script], cwd=ROOT, capture_output=True,
                text=True, timeout=15, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("KVM_TUI_DIAGNOSTIC phase=reattach result=PASS", result.stdout)
            self.assertNotIn("private-ssh-pty-secret", result.stdout)
            self.assertNotIn("\x1b", result.stdout)
            raw = capture_dir / "protected-tui-reattach.raw"
            self.assertTrue(raw.is_file())
            self.assertIn(b"private-ssh-pty-secret", raw.read_bytes())
            self.assertIn(b"\x1b[32m", raw.read_bytes())
            self.assertEqual(raw.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
