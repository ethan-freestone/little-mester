#!/usr/bin/env python3
"""Offline tests for hosted QEMU stop budgeting and direct-child reaping."""

from __future__ import annotations

import pathlib
import os
import re
import signal
import subprocess
import tempfile
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER = ROOT / "tests/ci-kvm-qemu-stop.sh"
LAUNCHER = ROOT / "lib/vm/host-launcher.sh"


class QemuStopAdapterTests(unittest.TestCase):
    def run_bash(self, script: str, *, timeout: int = 10) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            ["bash", "-c", script], cwd=ROOT, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Background fixture children inherit the captured pipe descriptors.
            # Kill the whole test-owned process group before draining those pipes.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = process.communicate(timeout=2)
            raise AssertionError(
                f"bash fixture exceeded {timeout}s; process group was cleaned up\n"
                f"stdout={stdout}\nstderr={stderr}"
            )
        return subprocess.CompletedProcess(
            process.args, process.returncode, stdout, stderr
        )

    def test_stop_budgets_expiry_near_expiry_and_cleanup_allowance(self):
        script = f'''set -euo pipefail
source {HELPER!s}
[[ "$(ci_qemu_stop_budget_ms stage 100 200 200)" == 0 ]]
[[ "$(ci_qemu_stop_budget_ms stage 201 203 200)" == 1000 ]]
[[ "$(ci_qemu_stop_budget_ms stage 203 201 200)" == 1000 ]]
[[ "$(ci_qemu_stop_budget_ms stage 0 201 200)" == 0 ]]
[[ "$(ci_qemu_stop_budget_ms cleanup 100 100 200)" == 13000 ]]
deadline="$(ci_qemu_stop_deadline_ms cleanup 100 100 200)"
now="$(python3 -I -c 'import time; print(time.monotonic_ns() // 1000000)')"
(( deadline >= now + 12900 && deadline <= now + 13000 ))
'''
        result = self.run_bash(script)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_exact_identity_stops_and_reaps_direct_child(self):
        with tempfile.TemporaryDirectory(prefix="ci-qemu-stop-") as directory:
            identity = pathlib.Path(directory) / "qemu.identity"
            waited = pathlib.Path(directory) / "wait-called"
            script = f'''set -euo pipefail
source {LAUNCHER!s}
source {HELPER!s}
VM_HOST_SCRIPT_ROOT={str(ROOT)!r}
wait() {{ printf 'wait\\n' >> {str(waited)!r}; builtin wait "$@"; }}
sleep 60 &
child=$!
cleanup_child() {{ kill "$child" 2>/dev/null || true; wait "$child" 2>/dev/null || true; }}
trap cleanup_child EXIT
exe="$(readlink -f "$(command -v sleep)")"
vm_host_record_process_identity {str(identity)!r} "$child" "$exe" 60
deadline="$(ci_qemu_stop_deadline_ms cleanup 0 0 "$SECONDS")"
ci_qemu_stop_identity_and_reap {str(identity)!r} "$child" "$exe" 60 "$deadline"
[[ -s {str(waited)!r} ]]
! kill -0 "$child" 2>/dev/null
'''
            result = self.run_bash(script, timeout=8)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_identity_mismatch_refuses_without_signalling_child(self):
        with tempfile.TemporaryDirectory(prefix="ci-qemu-stop-mismatch-") as directory:
            identity = pathlib.Path(directory) / "qemu.identity"
            script = f'''set -euo pipefail
source {LAUNCHER!s}
source {HELPER!s}
VM_HOST_SCRIPT_ROOT={str(ROOT)!r}
sleep 60 &
child=$!
cleanup_child() {{ kill "$child" 2>/dev/null || true; wait "$child" 2>/dev/null || true; }}
trap cleanup_child EXIT
exe="$(readlink -f "$(command -v sleep)")"
vm_host_record_process_identity {str(identity)!r} "$child" "$exe" 60
if ci_qemu_stop_identity_and_reap {str(identity)!r} "$child" "$exe" wrong 1000; then
  kill "$child" 2>/dev/null || true
  wait "$child" 2>/dev/null || true
  exit 8
fi
kill -0 "$child"
cleanup_deadline="$(ci_qemu_stop_deadline_ms cleanup 0 0 "$SECONDS")"
ci_qemu_stop_identity_and_reap {str(identity)!r} "$child" "$exe" 60 "$cleanup_deadline"
! kill -0 "$child" 2>/dev/null
'''
            result = self.run_bash(script)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_deadline_expiring_during_preparation_refuses_to_signal(self):
        with tempfile.TemporaryDirectory(prefix="ci-qemu-stop-expiry-") as directory:
            identity = pathlib.Path(directory) / "qemu.identity"
            script = f'''set -euo pipefail
source {LAUNCHER!s}
source {HELPER!s}
VM_HOST_SCRIPT_ROOT={str(ROOT)!r}
sleep 60 &
child=$!
cleanup_child() {{ kill "$child" 2>/dev/null || true; wait "$child" 2>/dev/null || true; }}
trap cleanup_child EXIT
exe="$(readlink -f "$(command -v sleep)")"
vm_host_record_process_identity {str(identity)!r} "$child" "$exe" 60
deadline="$(ci_qemu_stop_deadline_ms stage "$((SECONDS + 2))" "$((SECONDS + 4))" "$SECONDS")"
sleep 1.1
if ci_qemu_stop_identity_and_reap {str(identity)!r} "$child" "$exe" 60 "$deadline"; then
  kill -0 "$child" 2>/dev/null && kill "$child" || true
  wait "$child" 2>/dev/null || true
  exit 8
fi
kill -0 "$child"
cleanup_deadline="$(ci_qemu_stop_deadline_ms cleanup 0 0 "$SECONDS")"
ci_qemu_stop_identity_and_reap {str(identity)!r} "$child" "$exe" 60 "$cleanup_deadline"
! kill -0 "$child" 2>/dev/null
'''
            result = self.run_bash(script, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_fixture_timeout_kills_child_that_inherited_capture_pipes(self):
        script = '''sleep 60 &
child=$!
printf '%s\\n' "$child"
wait "$child"
'''
        with self.assertRaisesRegex(AssertionError, "process group was cleaned up") as caught:
            self.run_bash(script, timeout=1)
        match = re.search(r"stdout=(\d+)\n", str(caught.exception))
        self.assertIsNotNone(match, str(caught.exception))
        child_pid = int(match.group(1))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                stat_fields = pathlib.Path(f"/proc/{child_pid}/stat").read_text().split()
            except FileNotFoundError:
                break
            # A zombie has stopped consuming resources; the container's init
            # may reap it asynchronously after the killed parent exits.
            if len(stat_fields) > 2 and stat_fields[2] == "Z":
                break
            time.sleep(0.02)
        else:
            self.fail(f"fixture child {child_pid} remained alive after timeout cleanup")

    def test_term_resistant_child_is_escalated_inside_absolute_budget(self):
        with tempfile.TemporaryDirectory(prefix="ci-qemu-stop-escalate-") as directory:
            identity = pathlib.Path(directory) / "qemu.identity"
            ready = pathlib.Path(directory) / "term-ignored-ready"
            waited = pathlib.Path(directory) / "wait-status"
            script = f'''set -euo pipefail
source {LAUNCHER!s}
source {HELPER!s}
VM_HOST_SCRIPT_ROOT={str(ROOT)!r}
program='import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); open(os.environ["READY_FILE"],"w").close(); time.sleep(60)'
export READY_FILE={str(ready)!r}
wait() {{ rc=0; builtin wait "$@" || rc=$?; printf '%s\\n' "$rc" > {str(waited)!r}; return 0; }}
python3 -c "$program" &
child=$!
cleanup_child() {{ kill "$child" 2>/dev/null || true; wait "$child" 2>/dev/null || true; }}
trap cleanup_child EXIT
exe="$(readlink -f "$(command -v python3)")"
vm_host_record_process_identity {str(identity)!r} "$child" "$exe" "$program"
for _ in {{1..100}}; do [[ -e "$READY_FILE" ]] && break; sleep 0.01; done
[[ -e "$READY_FILE" ]]
read uptime _ </proc/uptime
up="${{uptime%%.*}}"; frac="${{uptime#*.}}"
frac="${{frac}}00"
now=$((up * 1000 + 10#${{frac:0:2}} * 10))
deadline=$((now + 700))
ci_qemu_stop_identity_and_reap {str(identity)!r} "$child" "$exe" "$program" "$deadline"
! kill -0 "$child" 2>/dev/null
[[ "$(< {str(waited)!r})" == 137 ]]
read uptime _ </proc/uptime
up="${{uptime%%.*}}"; frac="${{uptime#*.}}"
frac="${{frac}}00"
finished=$((up * 1000 + 10#${{frac:0:2}} * 10))
(( finished - now <= 700 ))
'''
            result = self.run_bash(script, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
