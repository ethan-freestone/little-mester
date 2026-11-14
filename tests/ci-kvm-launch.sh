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
  # Bash's async wrapper inherits EXIT/ERR traps and may keep a shell PID rather
  # than exec the requested binary. Temporarily remove the parent trap while
  # fork/exec happens so $! identifies the actual launched process.
  saved_traps="$(trap -p EXIT ERR)"
  trap - EXIT ERR
  "$@" &
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
