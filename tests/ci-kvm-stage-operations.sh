# shellcheck shell=bash
# shellcheck disable=SC2154
# Reusable substantive guest operations shared by the independently invoked
# config/TUI and restricted-reboot stages.

persistence_fixture_content='persistent-fixture'
persistence_content_sha256="$(printf '%s' "$persistence_fixture_content" | sha256sum | awk '{print $1}')"

ssh_tty_guest() {
  local remaining=$((qemu_deadline - SECONDS)) limit=15 stage_left
  (( remaining > 0 )) || fail 'QEMU integration exceeded its overall bounded deadline'
  stage_left="$(stage_remaining)"
  (( remaining > stage_left )) && remaining=$stage_left
  qemu_identity_matches || fail 'test-owned QEMU identity changed before guest PTY stage'
  verify_overlay_identity || fail 'disposable guest disk identity changed before guest PTY stage'
  (( remaining < limit )) && limit=$remaining
  timeout --signal=TERM --kill-after=1 "$limit" ssh \
    "${ssh_tty_args[@]}" "$1"
}

run_persistence_create() {
  local command raw rc classification
  raw="$tmp/persistence-create.raw"
  command="$(ci_kvm_persistence_create_command \
    /home/mester/workspace/persisted.txt "$persistence_fixture_content" \
    1000 1000 /home/mester)" \
    || fail 'could not construct the guest persistence fixture creation command'
  qemu_identity_matches || fail 'test-owned QEMU identity changed before persistence fixture creation'
  verify_overlay_identity || fail 'disposable guest overlay identity changed before persistence fixture creation'
  if ssh_guest "$command" 12 "$raw"; then rc=0; else rc=$?; fi
  classification="$(python3 "$repo_root/tests/ci-kvm-readiness.py" --exit-status "$rc" --input "$raw")"
  [[ "$classification" == persistence-create-pass ]] || {
    printf 'KVM_PERSISTENCE_CREATE_FAILURE class=%s exit=%s\n' "$classification" "$rc" >&2
    fail "pre-reboot persistence fixture creation failed ($classification)"
  }
  printf 'KVM_PERSISTENCE_CREATE_PASS path=workspace/persisted.txt uid=1000 gid=1000 mode=600\n'
}

run_persistence_proof() {
  local phase=$1 expected_mount_sha256=${2:-} command raw rc classification content_line mount_line
  raw="$tmp/persistence-$phase.raw"
  command="$(ci_kvm_persistence_check_command \
    /home/mester/workspace/persisted.txt 1000 1000 /home/mester \
    "$persistence_content_sha256" "$expected_mount_sha256")" \
    || fail 'could not construct the bounded guest persistence proof'
  qemu_identity_matches || fail 'test-owned QEMU identity changed before persistence proof'
  verify_overlay_identity || fail 'disposable guest overlay identity changed before persistence proof'
  if ssh_guest "$command" 12 "$raw"; then rc=0; else rc=$?; fi
  classification="$(python3 "$repo_root/tests/ci-kvm-readiness.py" --exit-status "$rc" --input "$raw")"
  if [[ "$classification" != persistence-pass ]]; then
    if [[ "$phase" == post-reboot ]]; then
      printf 'KVM_RESTART_PERSISTENCE_FAILURE class=%s exit=%s\n' "$classification" "$rc" >&2
      fail "restart persistence acceptance failed ($classification)"
    fi
    printf 'KVM_PERSISTENCE_PREREQUISITE_FAILURE phase=%s class=%s exit=%s\n' \
      "$phase" "$classification" "$rc" >&2
    fail "pre-reboot persistence prerequisite failed ($classification)"
  fi
  content_line="$(grep -E '^KVM_PERSISTENCE_CONTENT_SHA256=[0-9a-f]{64}$' "$raw" || true)"
  mount_line="$(grep -E '^KVM_PERSISTENCE_MOUNT_SHA256=[0-9a-f]{64}$' "$raw" || true)"
  [[ "$(grep -Ec '^KVM_PERSISTENCE_CONTENT_SHA256=[0-9a-f]{64}$' "$raw" || true)" == 1 \
      && "${content_line#*=}" == "$persistence_content_sha256" \
      && "$(grep -Ec '^KVM_PERSISTENCE_MOUNT_SHA256=[0-9a-f]{64}$' "$raw" || true)" == 1 ]] \
    || fail 'guest persistence proof returned malformed or mismatched fingerprints'
  persist_mount_sha256="${mount_line#*=}"
  [[ -z "$expected_mount_sha256" || "$persist_mount_sha256" == "$expected_mount_sha256" ]] \
    || fail 'guest root filesystem mount identity changed during persistence proof'
  if [[ "$phase" == post-reboot ]]; then
    printf 'KVM_RESTART_PERSISTENCE_PASS path=workspace/persisted.txt uid=1000 gid=1000 mode=600 content_sha256=%s mount_sha256=%s\n' \
      "$persistence_content_sha256" "$persist_mount_sha256"
  else
    printf 'KVM_PERSISTENCE_PREREQUISITE_PASS phase=%s path=workspace/persisted.txt uid=1000 gid=1000 mode=600 content_sha256=%s mount_sha256=%s\n' \
      "$phase" "$persistence_content_sha256" "$persist_mount_sha256"
  fi
}

run_persistence_flush() {
  local command raw rc classification
  raw="$tmp/persistence-flush.raw"
  command="$(ci_kvm_persistence_flush_command \
    /home/mester/workspace/persisted.txt 1000 1000 /home/mester)" \
    || fail 'could not construct the bounded guest filesystem flush command'
  qemu_identity_matches || fail 'test-owned QEMU identity changed before persistence filesystem flush'
  verify_overlay_identity || fail 'disposable guest overlay identity changed before persistence filesystem flush'
  if ssh_guest "$command" 12 "$raw"; then rc=0; else rc=$?; fi
  classification="$(python3 "$repo_root/tests/ci-kvm-readiness.py" --exit-status "$rc" --input "$raw")"
  [[ "$classification" == persistence-flush-pass ]] || {
    printf 'KVM_PERSISTENCE_FLUSH_FAILURE class=%s exit=%s\n' "$classification" "$rc" >&2
    fail "pre-reboot persistence filesystem flush failed ($classification)"
  }
  printf 'KVM_PERSISTENCE_FLUSH_PASS scope=guest-root-filesystem\n'
}
