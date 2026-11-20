#!/usr/bin/env bash
# Local sequential composition of the independently callable hosted-KVM stages.
set -Eeuo pipefail
umask 077

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
tmp_root="${RUNNER_TEMP:-/tmp/opencode}"
mkdir -p -- "$tmp_root"
run_suffix="$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
run_dir="$tmp_root/lm-kvm-local-${BASHPID}-${run_suffix}"
source_sha="${LM_CI_SOURCE_HEAD_SHA:-$(git -C "$repo_root" rev-parse HEAD)}"
workflow_sha="${GITHUB_SHA:-$source_sha}"
export LM_CI_STATE_DIR="$run_dir"
export LM_CI_RUN_ID="${GITHUB_RUN_ID:-$(date +%s)${BASHPID}}"
export LM_CI_RUN_ATTEMPT="${GITHUB_RUN_ATTEMPT:-1}"
export LM_CI_SOURCE_HEAD_SHA="$source_sha"
export GITHUB_SHA="$workflow_sha"
export GITHUB_RUN_ATTEMPT="$LM_CI_RUN_ATTEMPT"

finish() {
  local status=$?
  trap - EXIT INT TERM
  if [[ -d "$run_dir" && ! -L "$run_dir" ]]; then
    if ! bash "$repo_root/tests/ci-kvm-stage.sh" --stage cleanup --run-dir "$run_dir"; then
      (( status != 0 )) || status=1
    fi
  fi
  exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

run_stage() {
  bash "$repo_root/tests/ci-kvm-stage.sh" --stage "$1" --run-dir "$run_dir"
}

run_stage prepare
run_stage boot
run_stage config-tmux
run_stage restricted-reboot-egress-persistence
run_stage synthetic-oauth
python3 "$repo_root/tests/vm-browser-auth.test.py"
