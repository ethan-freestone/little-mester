# shellcheck shell=bash
# All hosted interactive SSH output is captured before any public reporting.

capture_tui_probe() {
  [[ "$#" == 7 ]] || return 2
  local phase=$1 runtime_seconds=$2 capture_dir=$3 retain_directory=$4
  local ssh_tty_runner=$5 diagnostic_script=$6 failure_reporter=$7
  local raw_capture="$capture_dir/protected-tui-$phase.raw"
  local -a pipeline_status=()
  declare -F "$ssh_tty_runner" >/dev/null && declare -F "$failure_reporter" >/dev/null \
    || return 2
  [[ -f "$diagnostic_script" && -r "$diagnostic_script" ]] || {
    "$failure_reporter" 'protected-TUI diagnostic script is unavailable' || true
    return 1
  }
  if "$ssh_tty_runner" "cd /home/mester/workspace && timeout --signal=TERM --kill-after=2 $runtime_seconds /usr/local/bin/lm-opencode" 2>&1 \
      | python3 "$diagnostic_script" capture "$raw_capture"; then
    pipeline_status=("${PIPESTATUS[@]}")
  else
    pipeline_status=("${PIPESTATUS[@]}")
  fi
  if [[ "${pipeline_status[1]}" != 0 ]]; then
    "$failure_reporter" 'private protected-TUI terminal capture failed' || true
    return 1
  fi
  python3 "$diagnostic_script" report --input "$raw_capture" --phase "$phase" \
    --actual-exit "${pipeline_status[0]}" --expected-exit 124 \
    --retain-directory "$retain_directory" \
    || {
      "$failure_reporter" "protected standalone OpenCode TUI probe failed phase=$phase" || true
      return 1
    }
}
