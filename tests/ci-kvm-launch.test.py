#!/usr/bin/env python3
"""Exercise deferred cancellation across Bash async-job PID publication."""

from __future__ import annotations

import pathlib
import os
import re
import signal
import subprocess
import tempfile
import unittest


HELPER = pathlib.Path(__file__).with_name("ci-kvm-launch.sh")


class TrackedLaunchTests(unittest.TestCase):
    @staticmethod
    def _process_group_diagnostics(pgid: int) -> str:
        rows = []
        for entry in pathlib.Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                stat_fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                # Fields after comm start with state; pgrp and session are
                # offsets 2 and 3 in this suffix.
                state, parent, process_group, session = stat_fields[:4]
                if int(process_group) != pgid:
                    continue
                status = {}
                for line in (entry / "status").read_text().splitlines():
                    key, separator, value = line.partition(":")
                    if separator and key in {
                        "Name", "State", "Pid", "PPid", "SigBlk", "SigIgn", "SigCgt"
                    }:
                        status[key] = value.strip()
                argv = (entry / "cmdline").read_bytes().split(b"\0", 1)[0]
                command = pathlib.Path(os.fsdecode(argv)).name if argv else "[empty]"
                wchan = (entry / "wchan").read_text().strip()
                rows.append(
                    f"pid={entry.name} ppid={parent} pgid={process_group} sid={session} "
                    f"state={state} wchan={wchan[:80]} comm={command[:80]} signals={status}"
                )
                if len(rows) >= 32:
                    rows.append("<additional process-group members omitted>")
                    break
            except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
                continue
        return "\n".join(rows) if rows else "<no processes remain in isolated test group>"

    def test_deferred_cancellation_waits_until_child_pid_is_published(self):
        script = r'''
set -Eeuo pipefail -T
source "$1"
# The two FIFOs synchronize an exact cancellation window: the child waits
# before resetting inherited traps, then the parent injects TERM before
# publishing $! to the caller. No scheduler-dependent sleep is involved.
TRACE_FILE=$2
READY_FIFO="$TRACE_FILE.ready"
RELEASE_FIFO="$TRACE_FILE.release"
mkfifo "$READY_FIFO" "$RELEASE_FIFO"
exec 8<>"$READY_FIFO"
exec 9<>"$RELEASE_FIFO"
child_pid=''
unpublished_child_pid=''
top_bash_pid=$BASHPID
record_event() {
  printf 'uptime=%s event=%s owner_pid=%s bashpid=%s pending=%s deferred=%s launched_pid=%s published_pid=%s\n' \
    "$(cut -d' ' -f1 /proc/uptime)" "$1" "$top_bash_pid" "$BASHPID" \
    "${CI_LAUNCH_PENDING:-?}" "${CI_DEFERRED_EXIT_STATUS:-?}" \
    "${launched_pid:-unset}" "${child_pid:-unset}" >>"$TRACE_FILE"
}
cleanup() {
  local status=$?
  [[ "$BASHPID" == "$top_bash_pid" ]] || return "$status"
  trap - EXIT
  trap '' INT TERM
  record_event cleanup-enter
  cleanup_pid="${child_pid:-$unpublished_child_pid}"
  if [[ -z "$cleanup_pid" ]]; then
    echo MISSING_CHILD_PID >&2
    exit 90
  fi
  record_event child-term
  kill -TERM "$cleanup_pid" 2>/dev/null || true
  printf 'continue\n' >&9
  record_event child-gate-released
  record_event child-wait
  wait "$cleanup_pid" 2>/dev/null || true
  record_event child-reaped
  printf 'TRACKED_CHILD_PUBLISHED=%s\n' "${child_pid:-unset}"
  printf 'TRACKED_CHILD_REAPED=%s\n' "$cleanup_pid"
  exit "$status"
}
trap cleanup EXIT
trap 'record_event signal-caught; ci_handle_cancel 143; record_event signal-deferred' TERM
debug_before_pid_publish() {
  if [[ "$BASHPID" != "$top_bash_pid" && "$BASH_COMMAND" == 'trap - INT TERM HUP QUIT' ]]; then
    record_event child-before-signal-reset
    printf 'ready\n' >&8
    IFS= read -r _ <&9 || true
    record_event child-after-signal-reset-gate
    trap - DEBUG
  elif [[ "$BASHPID" == "$top_bash_pid" && "$BASH_COMMAND" == printf\ -v*launched_pid* ]]; then
    record_event pre-pid-publication
    trap - DEBUG
    unpublished_child_pid="$launched_pid"
    if ! IFS= read -r -t 2 _ <&8; then
      record_event child-ready-timeout
      exit 91
    fi
    kill -TERM "$top_bash_pid"
  fi
}
trap debug_before_pid_publish DEBUG
ci_launch_tracked child_pid sleep 30
exit 0
'''
        trace_dir = tempfile.TemporaryDirectory(prefix="ci-kvm-launch-")
        self.addCleanup(trace_dir.cleanup)
        trace_file = pathlib.Path(trace_dir.name) / "events.log"
        process = subprocess.Popen(
            ["bash", "--noprofile", "--norc", "-c", script,
             "launch-test", str(HELPER), str(trace_file)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # Snapshot the isolated session before cleanup: this captures
            # event ordering, PID publication, wait channels and signal masks
            # while keeping argv/environment secrets out of diagnostics.
            trace = trace_file.read_text() if trace_file.exists() else "<no trace>"
            processes = self._process_group_diagnostics(process.pid)
            diagnostic = f"events:\n{trace}\nprocesses:\n{processes}"
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate(timeout=2)
            self.fail(
                f"tracked cancellation exceeded 5s; stdout={stdout!r} stderr={stderr!r}\n"
                f"bounded process diagnostics:\n{diagnostic}"
            )
        self.assertEqual(process.returncode, 143, stderr + stdout)
        match = re.search(r"TRACKED_CHILD_REAPED=([0-9]+)", stdout)
        self.assertIsNotNone(match, stderr + stdout)
        self.assertNotIn("MISSING_CHILD_PID", stderr)
        events = trace_file.read_text()
        self.assertIn("event=child-before-signal-reset", events)
        self.assertIn("event=pre-pid-publication", events)
        self.assertIn("event=signal-caught", events)
        self.assertRegex(events, r"event=signal-deferred[^\n]*deferred=143")
        self.assertIn("published_pid=unset", events)
        self.assertIn("event=child-reaped", events)
        expected_pid = re.search(
            r"event=pre-pid-publication[^\n]*launched_pid=(\d+) published_pid=unset", events
        )
        self.assertIsNotNone(expected_pid, events)
        published_pid = re.search(r"TRACKED_CHILD_PUBLISHED=([0-9]+)", stdout)
        self.assertIsNotNone(published_pid, stdout)
        child_gate = re.search(
            r"event=child-before-signal-reset owner_pid=(\d+) bashpid=(\d+)", events
        )
        self.assertIsNotNone(child_gate, events)
        self.assertNotEqual(child_gate.group(1), child_gate.group(2), events)
        child_pid = int(match.group(1))
        self.assertEqual(published_pid.group(1), expected_pid.group(1), events + stdout)
        self.assertEqual(child_pid, int(published_pid.group(1)), events + stdout)
        self.assertFalse(pathlib.Path(f"/proc/{child_pid}").exists(),
                         f"tracked child PID {child_pid} was not reaped")


if __name__ == "__main__":
    unittest.main()
