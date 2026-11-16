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

# Return the fresh relative runtime budget for one stage. All inputs are integer
# Bash SECONDS values; callers propagate the result to a child that owns its own
# monotonic alarm. Bound by both deadlines and reserve one second for Bash
# resolution/launch accounting; never reuse a pre-stage deadline.
ci_kvm_stage_runtime_seconds() {
  local stage_deadline=$1 overall_deadline=$2 now=$3 maximum=$4
  local stage_left overall_left budget
  [[ "$stage_deadline" =~ ^[0-9]+$ && "$overall_deadline" =~ ^[0-9]+$ \
     && "$now" =~ ^[0-9]+$ && "$maximum" =~ ^[1-9][0-9]*$ ]] || return 2
  stage_left=$((stage_deadline - now))
  overall_left=$((overall_deadline - now))
  (( stage_left > 0 && overall_left > 0 )) || return 1
  budget=$stage_left
  (( overall_left < budget )) && budget=$overall_left
  (( maximum < budget )) && budget=$maximum
  (( budget > 1 )) || return 1
  budget=$((budget - 1))
  printf '%s\n' "$budget"
}

# Reap a tracked direct child, retain its identity fields through cleanup, and
# return the operation and cleanup statuses independently. The cleanup callback
# must prove descendants/evidence clear before it clears the tracked PID.
ci_wait_tracked_and_cleanup() {
  local pid_name=$1 exit_name=$2 cleanup_name=$3 cleanup_callback=$4 pid
  # Nameref outputs are intentionally assigned for the caller's orchestration state.
  # shellcheck disable=SC2034
  local -n pid_ref=$pid_name exit_ref=$exit_name cleanup_ref=$cleanup_name
  [[ "$pid_ref" =~ ^[1-9][0-9]*$ && "$cleanup_callback" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]] || return 2
  pid=$pid_ref
  if wait "$pid"; then
    # Values are written through namerefs to caller-owned result variables.
    # shellcheck disable=SC2034
    exit_ref=0
  else
    # shellcheck disable=SC2034
    exit_ref=$?
  fi
  if "$cleanup_callback"; then
    # shellcheck disable=SC2034
    cleanup_ref=0
  else
    # shellcheck disable=SC2034
    cleanup_ref=$?
  fi
  (( cleanup_ref == 0 ))
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
