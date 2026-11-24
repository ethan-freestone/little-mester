# shellcheck shell=bash
# Small, independently testable budget/reap adapter for the direct CI QEMU child.

ci_qemu_wait_process_identity() {
  local pid=$1 executable=$2 required_arg=$3 timeout_ms=${4:-3000}
  [[ "$pid" =~ ^[1-9][0-9]*$ && "$timeout_ms" =~ ^[1-9][0-9]*$ ]] || return 2
  python3 -I - "$pid" "$executable" "$required_arg" "$timeout_ms" <<'PY'
import os, pathlib, sys, time

pid, expected_exe, required_arg = int(sys.argv[1]), os.path.realpath(sys.argv[2]), sys.argv[3]
deadline = time.monotonic() + int(sys.argv[4]) / 1000
while time.monotonic() < deadline:
    try:
        actual_exe = os.path.realpath(f"/proc/{pid}/exe")
        argv = [os.fsdecode(item) for item in pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if item]
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        raise SystemExit(1)
    if actual_exe == expected_exe and required_arg in argv:
        raise SystemExit(0)
    time.sleep(0.005)
raise SystemExit(1)
PY
}

ci_qemu_stop_budget_ms() {
  local mode=$1 stage_deadline=$2 overall_deadline=$3 now=$4
  local stage_left overall_left budget_ms=13000
  if [[ "$mode" == cleanup ]]; then
    printf '%s\n' "$budget_ms"
    return 0
  fi
  [[ "$mode" == stage ]] || return 2
  (( stage_deadline > 0 && overall_deadline > 0 )) || { printf '0\n'; return 0; }
  stage_left=$((stage_deadline - now))
  overall_left=$((overall_deadline - now))
  (( stage_left > 0 && overall_left > 0 )) || { printf '0\n'; return 0; }
  (( stage_left * 1000 < budget_ms )) && budget_ms=$((stage_left * 1000))
  (( overall_left * 1000 < budget_ms )) && budget_ms=$((overall_left * 1000))
  printf '%s\n' "$budget_ms"
}

ci_qemu_stop_deadline_ms() {
  local mode=$1 stage_deadline=$2 overall_deadline=$3 now=$4 budget_ms
  local uptime uptime_seconds uptime_fraction monotonic_ms
  # /proc/uptime is CLOCK_MONOTONIC at centisecond resolution and avoids
  # starting Python after the deadline anchor has already been sampled.
  read -r uptime _ </proc/uptime || return 1
  uptime_seconds=${uptime%%.*}
  uptime_fraction=${uptime#*.}
  uptime_fraction="${uptime_fraction}00"
  monotonic_ms=$((uptime_seconds * 1000 + 10#${uptime_fraction:0:2} * 10))
  budget_ms="$(ci_qemu_stop_budget_ms "$mode" "$stage_deadline" "$overall_deadline" "$now")" || return
  # Bash SECONDS is whole-second resolution. Reserve one second rather than
  # accidentally granting up to that much extra time near a deadline.
  if [[ "$mode" == stage ]] && (( budget_ms > 0 )); then
    budget_ms=$((budget_ms - 1000))
  fi
  (( budget_ms >= 2 )) || { printf '0\n'; return 0; }
  printf '%s\n' "$((monotonic_ms + budget_ms))"
}

ci_qemu_stop_identity_and_reap() {
  local identity_file=$1 pid=$2 executable=$3 required_arg=$4 deadline_ms=$5
  (( deadline_ms > 0 )) || return 1
  python3 -I - "$identity_file" "$pid" "$executable" "$required_arg" <<'PY' || return 1
import json, os, pathlib, sys
path, raw_pid, expected_exe, required_arg = sys.argv[1:]
try:
    record = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    expected_pid = int(raw_pid)
    expected_exe = os.path.realpath(expected_exe)
except (OSError, ValueError, TypeError, json.JSONDecodeError):
    raise SystemExit(1)
if (record.get("pid") != expected_pid or record.get("exe") != expected_exe
        or required_arg not in record.get("argv", [])):
    raise SystemExit(1)
PY
  vm_host_process_identity_status "$identity_file" || return 1
  # The shared helper computes remaining time again after its own identity and
  # pidfd checks, and allocates TERM/KILL waits only within this absolute bound.
  export VM_HOST_PROCESS_STOP_GRACE_MS=1
  export VM_HOST_PROCESS_KILL_SETTLE_MS=1
  vm_host_stop_process_identity "$identity_file" "$deadline_ms" || return 1
  # The caller owns this direct child; pidfd readiness is followed by wait(2)
  # so Bash reaps it before its PID/identity state is cleared.
  wait "$pid" 2>/dev/null || true
}
