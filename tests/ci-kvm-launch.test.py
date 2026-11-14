#!/usr/bin/env python3
"""Exercise deferred cancellation across Bash async-job PID publication."""

from __future__ import annotations

import pathlib
import os
import re
import signal
import subprocess
import unittest


HELPER = pathlib.Path(__file__).with_name("ci-kvm-launch.sh")


class TrackedLaunchTests(unittest.TestCase):
    def test_deferred_cancellation_waits_until_child_pid_is_published(self):
        script = r'''
set -Eeuo pipefail
source "$1"
child_pid=''
cleanup() {
  local status=$?
  [[ "$BASHPID" == "$$" ]] || return "$status"
  trap - EXIT
  trap '' INT TERM
  if [[ -z "$child_pid" ]]; then
    echo MISSING_CHILD_PID >&2
    exit 90
  fi
  kill -TERM "$child_pid" 2>/dev/null || true
  wait "$child_pid" 2>/dev/null || true
  printf 'TRACKED_CHILD_REAPED=%s\n' "$child_pid"
  exit "$status"
}
trap cleanup EXIT
trap 'ci_handle_cancel 143' TERM
CI_LAUNCH_PENDING=1
kill -TERM "$BASHPID"
CI_LAUNCH_PENDING=0
ci_launch_tracked child_pid bash -c 'exec sleep 30 >/dev/null 2>&1'
exit 0
'''
        process = subprocess.Popen(
            ["bash", "--noprofile", "--norc", "-c", script, "launch-test", str(HELPER)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # subprocess.run(timeout=...) kills only its direct child. The
            # async job can inherit the capture pipes and survive that kill,
            # obscuring whether Bash itself exited and leaking `sleep`.
            # Isolate this test process and clean only its test-owned session.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate(timeout=2)
            self.fail(f"tracked cancellation exceeded 5s; stdout={stdout!r} stderr={stderr!r}")
        self.assertEqual(process.returncode, 143, stderr + stdout)
        match = re.search(r"TRACKED_CHILD_REAPED=([0-9]+)", stdout)
        self.assertIsNotNone(match, stderr + stdout)
        self.assertNotIn("MISSING_CHILD_PID", stderr)
        child_pid = int(match.group(1))
        self.assertFalse(pathlib.Path(f"/proc/{child_pid}").exists(),
                         f"tracked child PID {child_pid} was not reaped")


if __name__ == "__main__":
    unittest.main()
