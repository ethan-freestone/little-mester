#!/usr/bin/env python3
"""Offline regression for the hosted KVM boot-stage SSH call contract."""

from __future__ import annotations

import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests/ci-kvm-capability.sh"
HELPER = ROOT / "tests/ci-kvm-ssh.sh"


class HostedKvmSshCallTests(unittest.TestCase):
    def test_boot_stage_sources_and_calls_the_guarded_ssh_helper(self):
        source = SCRIPT.read_text(encoding="utf-8")
        helper_source = source.index('source "$(dirname -- "$process_signal_helper")/ci-kvm-ssh.sh"')
        boot_call = source.index("guest_boot_id=\"$(ssh_guest '")
        self.assertLess(helper_source, boot_call)

        with tempfile.TemporaryDirectory(prefix="ci-kvm-ssh-call-") as directory:
            output_file = pathlib.Path(directory) / "ssh-argv"
            guard_log = pathlib.Path(directory) / "guards"
            script = f'''set -euo pipefail
source {HELPER!s}
qemu_deadline=$((SECONDS + 40))
stage_remaining() {{ printf '12\\n'; }}
qemu_identity_matches() {{ printf 'qemu\\n' >> {str(guard_log)!r}; }}
verify_overlay_identity() {{ printf 'overlay\\n' >> {str(guard_log)!r}; }}
fail() {{ printf 'unexpected fail: %s\\n' "$1" >&2; return 99; }}
ssh_args=(-F /dev/null -T -i /tmp/pinned-key -p 42319
  -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/tmp/pinned-known-hosts
  -o GlobalKnownHostsFile=/dev/null)
timeout() {{
  [[ "$1 $2 $3 $4" == '--signal=TERM --kill-after=1 12 ssh' ]]
  shift 3
  [[ "$1" == ssh ]]
  shift
  printf '%s\\n' "$@" > {str(output_file)!r}
}}
ssh_guest 'cat /proc/sys/kernel/random/boot_id'
'''
            result = subprocess.run(
                ["bash", "-c", script], cwd=ROOT, capture_output=True,
                text=True, timeout=5, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(guard_log.read_text().splitlines(), ["qemu", "overlay"])
            self.assertEqual(
                output_file.read_text().splitlines(),
                ["-F", "/dev/null", "-T", "-i", "/tmp/pinned-key", "-p", "42319",
                 "-o", "StrictHostKeyChecking=yes", "-o",
                 "UserKnownHostsFile=/tmp/pinned-known-hosts", "-o",
                 "GlobalKnownHostsFile=/dev/null", "cat /proc/sys/kernel/random/boot_id"],
            )


if __name__ == "__main__":
    unittest.main()
