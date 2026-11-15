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
            bounded_output = pathlib.Path(directory) / "bounded-output"
            bounded_argv = pathlib.Path(directory) / "bounded-ssh-argv"
            guard_log = pathlib.Path(directory) / "guards"
            fake_bin = pathlib.Path(directory) / "bin"
            fake_bin.mkdir()
            fake_timeout = fake_bin / "timeout"
            fake_timeout.write_text(
                "#!/bin/sh\n"
                "test \"$1\" = --signal=TERM && test \"$2\" = --kill-after=1 || exit 91\n"
                "shift 2\n"
                "test \"$1\" = 12 || exit 92\n"
                "shift\n"
                "exec \"$@\"\n"
            )
            fake_timeout.chmod(0o755)
            fake_ssh = fake_bin / "ssh"
            fake_ssh.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$BOUNDED_SSH_ARGV\"\n"
                "printf '%s\\n' CONTROLLED_SSH_OUTPUT\n"
            )
            fake_ssh.chmod(0o755)
            script = f'''set -euo pipefail
source {HELPER!s}
repo_root={str(ROOT)!r}
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
 : > {str(guard_log)!r}
export BOUNDED_SSH_ARGV={str(bounded_argv)!r}
PATH={str(fake_bin)!r}:$PATH ssh_guest 'bounded persistence proof' 12 {str(bounded_output)!r}
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
            self.assertEqual(guard_log.read_text().splitlines(), ["qemu", "overlay"])
            self.assertEqual(bounded_output.read_text().strip(), "CONTROLLED_SSH_OUTPUT")
            self.assertEqual(
                bounded_argv.read_text().splitlines(),
                ["-F", "/dev/null", "-T", "-i", "/tmp/pinned-key", "-p", "42319",
                 "-o", "StrictHostKeyChecking=yes", "-o",
                 "UserKnownHostsFile=/tmp/pinned-known-hosts", "-o",
                 "GlobalKnownHostsFile=/dev/null", "bounded persistence proof"],
            )


if __name__ == "__main__":
    unittest.main()
