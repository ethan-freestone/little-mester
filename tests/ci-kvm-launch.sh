# shellcheck shell=bash
# Shared launch/signal bookkeeping for the hosted disposable-KVM test.
# Source this file from a non-interactive Bash process.

CI_LAUNCH_PENDING=0
CI_DEFERRED_EXIT_STATUS=0

ci_handle_cancel() {
  local status=$1
  if (( CI_LAUNCH_PENDING )); then
    if (( CI_DEFERRED_EXIT_STATUS == 0 )); then
      CI_DEFERRED_EXIT_STATUS=$status
    fi
    return 0
  fi
  exit "$status"
}

ci_launch_tracked() {
  local variable=$1 launched_pid saved_traps
  shift
  CI_LAUNCH_PENDING=1
  # Bash's async wrapper can retain a shell PID and inherited signal traps.
  # Temporarily remove the parent's EXIT/ERR traps and have the child reset
  # cancellation traps before exec. Then $! remains the stable PID across the
  # exec, and TERM sent before exec kills the wrapper instead of being deferred
  # in it (which could otherwise orphan the real child).
  saved_traps="$(trap -p EXIT ERR)"
  trap - EXIT ERR
  (
    trap - INT TERM HUP QUIT
    exec "$@"
  ) &
  launched_pid=$!
  if [[ -n "$saved_traps" ]]; then
    eval "$saved_traps"
  fi
  printf -v "$variable" '%s' "$launched_pid"
  CI_LAUNCH_PENDING=0
  if (( CI_DEFERRED_EXIT_STATUS )); then
    local status=$CI_DEFERRED_EXIT_STATUS
    CI_DEFERRED_EXIT_STATUS=0
    exit "$status"
  fi
}
