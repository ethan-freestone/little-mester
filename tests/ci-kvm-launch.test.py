#!/usr/bin/env python3
"""Exercise deferred cancellation across Bash async-job PID publication."""

from __future__ import annotations

import pathlib
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
ci_launch_tracked child_pid sleep 30
exit 0
'''
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", script, "launch-test", str(HELPER)],
            capture_output=True, text=True, timeout=5, check=False,
        )
        self.assertEqual(result.returncode, 143, result.stderr + result.stdout)
        self.assertIn("TRACKED_CHILD_REAPED=", result.stdout)
        self.assertNotIn("MISSING_CHILD_PID", result.stderr)


if __name__ == "__main__":
    unittest.main()
