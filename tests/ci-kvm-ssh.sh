# shellcheck shell=bash
# Shared SSH call path used by the hosted KVM boot-stage and later guest stages.

# shellcheck disable=SC2154
ssh_guest() {
  local remaining=$((qemu_deadline - SECONDS)) limit=${2:-20} stage_left output_file=${3:-}
  (( remaining > 0 )) || fail 'QEMU integration exceeded its overall bounded deadline'
  stage_left="$(stage_remaining)"
  (( remaining > stage_left )) && remaining=$stage_left
  qemu_identity_matches || fail 'test-owned QEMU identity changed before guest SSH stage'
  verify_overlay_identity || fail 'disposable guest disk identity changed before guest SSH stage'
  (( remaining < limit )) && limit=$remaining
  if [[ -n "$output_file" ]]; then
    python3 -I "$repo_root/lib/vm/bounded-log.py" "$output_file" 8192 --fail-on-overflow -- \
      timeout --signal=TERM --kill-after=1 "$limit" ssh "${ssh_args[@]}" "$1"
  else
    timeout --signal=TERM --kill-after=1 "$limit" ssh "${ssh_args[@]}" "$1"
  fi
}
