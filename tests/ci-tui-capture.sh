# shellcheck shell=bash
# All hosted interactive SSH output is captured before any public reporting.

capture_tui_probe() {
  local phase=$1 runtime_seconds=$2 capture_dir=$3 retain_directory=$4
  local raw_capture="$capture_dir/protected-tui-$phase.raw"
  local -a pipeline_status=()
  if ssh_tty_guest "cd /home/mester/workspace && timeout --signal=TERM --kill-after=2 $runtime_seconds /usr/local/bin/lm-opencode" 2>&1 \
      | python3 tests/ci-tui-diagnostic.py capture "$raw_capture"; then
    pipeline_status=("${PIPESTATUS[@]}")
  else
    pipeline_status=("${PIPESTATUS[@]}")
  fi
  [[ "${pipeline_status[1]}" == 0 ]] || fail 'private protected-TUI terminal capture failed'
  python3 tests/ci-tui-diagnostic.py report --input "$raw_capture" --phase "$phase" \
    --actual-exit "${pipeline_status[0]}" --expected-exit 124 \
    --retain-directory "$retain_directory" \
    || fail "protected standalone OpenCode TUI probe failed phase=$phase"
}
