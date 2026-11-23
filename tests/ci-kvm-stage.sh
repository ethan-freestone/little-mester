#!/usr/bin/env bash
# Credential-free hosted-runner M2 integration on a disposable Ubuntu guest.
# OAuth/provider credentials and real model access are never used here.
# Shell snippets passed through SSH are intentionally single-quoted locally.
# shellcheck disable=SC2016
set -Eeuo pipefail
umask 077
requested_stage=''
state_dir="${LM_CI_STATE_DIR:-}"
while (($#)); do
  case "$1" in
    --stage) (($# >= 2)) || exit 2; requested_stage=$2; shift 2 ;;
    --run-dir) (($# >= 2)) || exit 2; state_dir=$2; shift 2 ;;
    *) printf 'KVM_TRIAL_FAIL: unknown argument %s\n' "$1" >&2; exit 2 ;;
  esac
done
case "$requested_stage" in
  prepare|boot|config-tmux|restricted-reboot-egress-persistence|synthetic-oauth|cleanup) ;;
  *) printf 'KVM_TRIAL_FAIL: --stage is required and must name a supported operation\n' >&2; exit 2 ;;
esac
if [[ "$requested_stage" == cleanup && ! -e "$state_dir" && ! -L "$state_dir" ]]; then
  printf 'KVM_STAGE_PASS name=cleanup resources=none\n'
  exit 0
fi
process_signal_helper="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/ci-process-signal.py"
# shellcheck source=tests/ci-kvm-launch.sh
source "$(dirname -- "$process_signal_helper")/ci-kvm-launch.sh"
# shellcheck source=tests/ci-kvm-contract.sh
source "$(dirname -- "$process_signal_helper")/ci-kvm-contract.sh"
# shellcheck source=tests/ci-kvm-ssh.sh
source "$(dirname -- "$process_signal_helper")/ci-kvm-ssh.sh"
# shellcheck source=tests/ci-tui-capture.sh
source "$(dirname -- "$process_signal_helper")/ci-tui-capture.sh"
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
# Reuse the smoke harness's exact-identity/pidfd stop primitive for this direct
# disposable-QEMU child; lifecycle and disk policy remain CI-specific.
source "$repo_root/tests/ci-kvm-stage-operations.sh"
state_helper="$repo_root/tests/ci-kvm-stage-state.py"
owner_helper="$repo_root/tests/ci-kvm-qemu-owner.py"
[[ "$state_dir" == /* && "${LM_CI_RUN_ID:-}" =~ ^[0-9]+$ \
   && "${GITHUB_RUN_ATTEMPT:-${LM_CI_RUN_ATTEMPT:-1}}" =~ ^[0-9]+$ \
   && "${LM_CI_SOURCE_HEAD_SHA:-${GITHUB_SHA:-}}" =~ ^[0-9a-f]{40}$ \
   && "${GITHUB_SHA:-${LM_CI_SOURCE_HEAD_SHA:-}}" =~ ^[0-9a-f]{40}$ ]] \
  || { printf '%s\n' 'KVM_TRIAL_FAIL: workflow/run identity is invalid' >&2; exit 2; }
run_attempt="${GITHUB_RUN_ATTEMPT:-${LM_CI_RUN_ATTEMPT:-1}}"
source_sha="${LM_CI_SOURCE_HEAD_SHA:-${GITHUB_SHA}}"
workflow_sha="${GITHUB_SHA:-$source_sha}"
state_args=(--run-dir "$state_dir" --run-id "$LM_CI_RUN_ID" --attempt "$run_attempt"
  --source-sha "$source_sha" --workflow-sha "$workflow_sha")

readonly image_url='https://cloud-images.ubuntu.com/releases/noble/release-20261017/ubuntu-24.04-server-cloudimg-amd64.img'
readonly image_sha256='04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94'
readonly guest_disk_gib=24
readonly guest_disk_bytes=$((guest_disk_gib * 1024 * 1024 * 1024))

fail() {
  printf 'KVM_TRIAL_FAIL: %s\n' "$1" >&2
  [[ -z "${stage_name:-}" ]] || printf 'KVM_TRIAL_FAILURE_DETAIL stage=%s\n' "$stage_name" >&2
  if [[ -n "${tmp:-}" && -s "$tmp/readiness.log" ]]; then
    printf '%s\n' '--- last bounded guest SSH/readiness probe ---' >&2
    tail -n 12 "$tmp/readiness.log" >&2 || true
    if grep -Eqi 'REMOTE HOST IDENTIFICATION HAS CHANGED|Host key verification failed' "$tmp/readiness.log"; then
      printf '%s\n' 'KVM_SSH_FAILURE_CLASS=host-key-verification' >&2
    elif grep -Eqi 'Permission denied|no mutual signature|no matching .* found' "$tmp/readiness.log"; then
      printf '%s\n' 'KVM_SSH_FAILURE_CLASS=authentication-or-ssh-negotiation' >&2
    elif grep -Eqi 'kex_exchange_identification.*(reset|closed)|Connection refused|Connection reset by peer' "$tmp/readiness.log"; then
      if [[ -n "${serial_log:-}" ]] && grep -aq 'KVM_GUEST_SSH_HOSTKEY_PINNED source=qemu-serial-cloud-init' "$serial_log"; then
        printf '%s\n' 'KVM_SSH_FAILURE_CLASS=pre-identification-transport-reset guest_hostkey=serial-pinned inspect_socket-activation-or-qemu-hostfwd' >&2
      else
        printf '%s\n' 'KVM_SSH_FAILURE_CLASS=pre-identification-transport-reset guest_hostkeys=unconfirmed inspect_guest_cloud-init-or-network' >&2
      fi
    fi
  fi
  if [[ -n "${serial_log:-}" && -f "$serial_log" ]]; then
    printf '%s\n' '--- bounded SSH/provisioning serial markers ---' >&2
    grep -aE 'KVM_GUEST_(SSH|PROVISION)' "$serial_log" | tail -n 24 >&2 || true
    printf '%s\n' '--- bounded guest serial tail ---' >&2
    tail -n 80 "$serial_log" >&2 || true
  fi
  exit 1
}

report_unhandled_failure() {
  local status=$1 line=$2
  printf 'KVM_TRIAL_FAIL: command failed at line %s (status %s)\n' "$line" "$status" >&2
  if [[ -n "${serial_log:-}" && -f "$serial_log" ]]; then
    printf '%s\n' '--- bounded guest serial tail ---' >&2
    tail -n 80 "$serial_log" >&2 || true
  fi
  return "$status"
}
trap 'report_unhandled_failure "$?" "$LINENO"' ERR

# BEGIN: live OAuth harness cleanup primitives; exercised by ci-kvm-oauth-cleanup.test.py.
process_state() {
  local stat_line rest
  [[ -d "/proc/$1" ]] || return 0
  [[ -r "/proc/$1/stat" ]] || { printf '?'; return 0; }
  if ! stat_line="$(<"/proc/$1/stat")"; then
    [[ -d "/proc/$1" ]] && printf '?'
    return 0
  fi
  rest="${stat_line##*) }"
  printf '%s' "${rest%% *}"
}

process_starttime() {
  local pid=$1 stat_line rest
  [[ -r "/proc/$pid/stat" ]] || return 1
  stat_line="$(<"/proc/$pid/stat")"
  rest="${stat_line##*) }"
  local -a fields
  read -r -a fields <<<"$rest"
  ((${#fields[@]} > 19)) || return 1
  printf '%s' "${fields[19]}"
}

process_uid() {
  awk '$1 == "Uid:" { print $2; exit }' "/proc/$1/status" 2>/dev/null
}

signal_owned_process() {
  local pid=$1 starttime=$2 uid=$3 parent=$4 comm=$5 signame=$6 required
  shift 6
  local -a args=()
  for required in "$@"; do
    args+=(--contains "$required")
  done
  python3 -I "$process_signal_helper" --pid "$pid" --starttime "$starttime" \
    --uid "$uid" --parent "$parent" --comm "$comm" --signal "$signame" "${args[@]}"
}

report_kvm_progress() {
  local phase=$1 deadline=$2 ssh_rc=$3 ssh_class=${4:-not-classified}
  local now serial_bytes readiness_detail
  now=$SECONDS
  (( now >= next_progress )) || return 0
  next_progress=$((now + 15))
  serial_bytes=0
  [[ -f "$serial_log" ]] && serial_bytes="$(wc -c <"$serial_log")"
  printf 'KVM_PROGRESS phase=%s elapsed=%ss remaining=%ss qemu_state=%s last_ssh_rc=%s ssh_class=%s serial_bytes=%s\n' \
    "$phase" "$((now - qemu_started))" "$((deadline - now))" \
    "$(process_state "$qemu_pid")" "$ssh_rc" "$ssh_class" "$serial_bytes"
  if [[ -s "$tmp/readiness.log" ]]; then
    readiness_detail="$(tail -n 3 "$tmp/readiness.log" | tr '\n' ' ' | cut -c1-300)"
    [[ -n "$readiness_detail" ]] && printf 'KVM_PROGRESS detail=%s\n' "$readiness_detail"
  fi
  if (( now >= next_serial_tail )); then
    next_serial_tail=$((now + 30))
    if [[ -s "$serial_log" ]]; then
      printf '%s\n' 'KVM_PROGRESS guest-serial-tail:'
      tail -n 5 "$serial_log" || true
    fi
  fi
}

owner_call() {
  python3 -I "$owner_helper" "$1" "${state_args[@]}"
}

qemu_identity_matches() {
  local response mode
  response="$(owner_call status)" || return 1
  mode="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("status", ""))' <<<"$response")" || return 1
  [[ "$mode" == initial || "$mode" == restricted-restart ]] || return 1
  [[ -z "${expected_qemu_mode:-}" || "$mode" == "$expected_qemu_mode" ]] || return 1
  [[ "$overlay" == "$state_dir/work/guest.qcow2" ]] || return 1
  verify_overlay_identity
}

capture_qemu_identity() {
  local response
  response="$(owner_call status)" || return 1
  qemu_pid="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["qemu"]["pid"])' <<<"$response")" || return 1
  qemu_identity_matches
}

oauth_evidence_is_clear() {
  local first_entry
  [[ -z "$oauth_evidence_root" && -z "$oauth_pid" ]] && return 0
  [[ ! -e "$oauth_evidence_root" && ! -L "$oauth_evidence_root" ]] && return 0
  [[ -n "$oauth_evidence_root" && -d "$oauth_evidence_root" && -r "$oauth_evidence_root" ]] || return 1
  first_entry="$(find "$oauth_evidence_root" -mindepth 1 -maxdepth 1 -print -quit)" || return 1
  [[ -z "$first_entry" ]]
}

clear_oauth_tracking() {
  oauth_pid=''
  oauth_starttime=''
  oauth_uid=''
  oauth_parent=''
}

terminate_oauth_bridge_groups() {
  python3 -I "$process_signal_helper" --terminate-marked-groups "$oauth_evidence_root"
}

stop_oauth_harness() {
  local state='' current_start stat_line rest parent
  [[ -n "$oauth_pid" ]] || { oauth_evidence_is_clear; return; }
  state="$(process_state "$oauth_pid")"
  if [[ "$state" == '?' ]]; then
    return 1
  fi
  current_start="$(process_starttime "$oauth_pid" 2>/dev/null || true)"
  if [[ -z "$state" || "$state" == Z ]]; then
    wait "$oauth_pid" 2>/dev/null || true
    clear_oauth_tracking
    terminate_oauth_bridge_groups || { oauth_cleanup_unproved=1; return 1; }
    oauth_evidence_is_clear || { oauth_cleanup_unproved=1; return 1; }
    return 0
  fi
  [[ -n "$current_start" ]] || return 1
  if [[ -z "$oauth_starttime" || -z "$oauth_uid" || -z "$oauth_parent" ]]; then
    # The child may still be the launcher's pre-exec job. Recover only from a
    # live direct Bash job; never block in wait on an unidentified process.
    stat_line="$(<"/proc/$oauth_pid/stat")" || return 1
    rest="${stat_line##*) }"
    local -a fields
    read -r -a fields <<<"$rest"
    parent="${fields[1]:-}"
    jobs -pr | grep -Fxq -- "$oauth_pid" || return 1
    [[ "$parent" == "$BASHPID" ]] || return 1
    oauth_starttime="$current_start"
    oauth_uid="$(process_uid "$oauth_pid")"
    oauth_parent="$BASHPID"
  fi
  [[ "$current_start" == "$oauth_starttime" && "$oauth_parent" == "$BASHPID" ]] || return 1
  signal_owned_process "$oauth_pid" "$oauth_starttime" "$oauth_uid" \
    "$oauth_parent" '*' TERM || {
      state="$(process_state "$oauth_pid")"
      [[ -z "$state" || "$state" == Z ]] || return 1
    }
  # Allow the harness its 10-second child interruption plus bounded process-
  # group kill/reap and proof check before escalating the harness itself.
  for _ in {1..200}; do
    state="$(process_state "$oauth_pid")"
    [[ -z "$state" || "$state" == Z ]] && break
    [[ "$state" != '?' ]] || return 1
    sleep 0.1
  done
  state="$(process_state "$oauth_pid")"
  if [[ -n "$state" && "$state" != Z ]]; then
    # If the harness did not complete its proof-bearing shutdown, kill any
    # still-owned PTY bridge process groups before escalating the harness.
    terminate_oauth_bridge_groups || oauth_cleanup_unproved=1
    signal_owned_process "$oauth_pid" "$oauth_starttime" "$oauth_uid" \
      "$oauth_parent" '*' KILL || {
        state="$(process_state "$oauth_pid")"
        [[ -z "$state" || "$state" == Z ]] || return 1
      }
    oauth_cleanup_unproved=1
    for _ in {1..20}; do
      state="$(process_state "$oauth_pid")"
      [[ -z "$state" || "$state" == Z ]] && break
      [[ "$state" != '?' ]] || return 1
      sleep 0.1
    done
  fi
  state="$(process_state "$oauth_pid")"
  [[ -z "$state" || "$state" == Z ]] || return 1
  wait "$oauth_pid" 2>/dev/null || true
  terminate_oauth_bridge_groups || { oauth_cleanup_unproved=1; return 1; }
  clear_oauth_tracking
  oauth_evidence_is_clear || { oauth_cleanup_unproved=1; return 1; }
}
# END: live OAuth harness cleanup primitives.

stop_owned_qemu() {
  owner_call stop >/dev/null
}

# Initialize ownership fields before preflight and EXIT handling so a missing
# KVM device still publishes failure/cleanup state without signalling anything.
tmp=''
oauth_evidence_root=''
qemu_pid=''
overlay_device=''
overlay_inode=''
oauth_pid=''
oauth_starttime=''
oauth_uid=''
oauth_parent=''
oauth_cleanup_unproved=0
oauth_cleanup_status=0
qemu_deadline=0
stage_deadline=0
stage_name=''

verify_overlay_identity() {
  local actual_device actual_inode
  [[ -n "$overlay_device" && -n "$overlay_inode" && -f "$overlay" && ! -L "$overlay" ]] || return 1
  actual_device="$(stat -c %d -- "$overlay")" || return 1
  actual_inode="$(stat -c %i -- "$overlay")" || return 1
  [[ "$actual_device" == "$overlay_device" && "$actual_inode" == "$overlay_inode" ]]
}

stage_start() {
  stage_name=$1
  stage_deadline=$((SECONDS + $2))
  next_progress=$((SECONDS + 15))
  printf '::group::KVM_STAGE_START name=%s source_head_sha=%s workflow_sha=%s deadline_seconds=%s\n' \
    "$1" "${LM_CI_SOURCE_HEAD_SHA:-not-provided}" "${GITHUB_SHA:-local}" "$2"
}

stage_remaining() {
  local remaining=$((stage_deadline - SECONDS))
  (( remaining > 0 )) || fail "bounded KVM stage deadline expired: $stage_name"
  printf '%s' "$remaining"
}

stage_end() {
  python3 -I "$state_helper" result "${state_args[@]}" --stage "$1" --status 0 \
    || fail "could not persist successful stage result for $1"
  printf 'KVM_STAGE_PASS name=%s\n::endgroup::\n' "$1"
  stage_name=''
  stage_deadline=0
}

cleanup() {
  local primary_status=$? status cleanup_failed=0
  status=$primary_status
  local state_exists=0 owner_socket="$state_dir/owner.sock"
  trap '' INT TERM
  if [[ "$status" == 130 || "$status" == 143 ]]; then
    printf 'KVM_TRIAL_CANCELLED: status=%s; attempting bounded disposable-guest shutdown\n' "$status" >&2
  fi
  if [[ -n "$oauth_pid" ]] && ! stop_oauth_harness; then
    printf 'KVM_TRIAL_FAIL: OAuth harness did not stop; preserving disposable guest diagnostics at %s\n' "$tmp" >&2
    cleanup_failed=1
  fi
  if (( oauth_cleanup_unproved )) || ! oauth_evidence_is_clear; then
    printf 'KVM_TRIAL_FAIL: OAuth descendant cleanup/evidence is not proved; retaining diagnostics at %s\n' "$tmp" >&2
    cleanup_failed=1
  fi
  [[ -f "$state_dir/state.json" && ! -L "$state_dir/state.json" ]] && state_exists=1
  if (( state_exists )) && [[ -S "$owner_socket" ]] \
      && { (( primary_status != 0 )) || [[ "$requested_stage" == cleanup ]]; }; then
    owner_call shutdown >/dev/null || {
      printf 'KVM_TRIAL_FAIL: lifecycle owner could not prove bounded QEMU stop; preserving %s\n' "$state_dir" >&2
      cleanup_failed=1
    }
  fi
  if (( state_exists )) && (( primary_status != 0 )); then
    python3 -I "$state_helper" result "${state_args[@]}" --stage "$requested_stage" --status 1 || true
  fi
  if [[ "$requested_stage" == cleanup ]] && (( primary_status == 0 && cleanup_failed == 0 )) && (( state_exists )); then
    if [[ -S "$owner_socket" ]]; then
      printf '%s\n' 'KVM_TRIAL_FAIL: owner socket remains after shutdown' >&2
      cleanup_failed=1
    elif owner_call verify-stopped >/dev/null; then
      python3 -I "$state_helper" result "${state_args[@]}" --stage cleanup --status 0 || cleanup_failed=1
      if (( cleanup_failed == 0 )); then
        if rm -rf -- "$state_dir"; then
        printf 'KVM_STAGE_PASS name=cleanup qemu_stopped=true private_state_removed=true\n'
        else
          printf 'KVM_TRIAL_FAIL: private run state could not be removed; preserving %s\n' "$state_dir" >&2
          cleanup_failed=1
        fi
      fi
    else
      printf 'KVM_TRIAL_FAIL: owner/QEMU exit and cleanup could not be proved; preserving %s\n' "$state_dir" >&2
      cleanup_failed=1
    fi
  fi
  if (( primary_status != 0 )); then
    status=$primary_status
  elif (( cleanup_failed )); then
    if (( state_exists )) && [[ "$requested_stage" == cleanup ]]; then
      python3 -I "$state_helper" result "${state_args[@]}" --stage cleanup --status 1 || true
    fi
    status=1
  fi
  if (( status != 0 )); then
    printf 'KVM_STAGE_ABORT name=%s status=%s\n' "$requested_stage" "$status" >&2
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'ci_handle_cancel 130' INT
trap 'ci_handle_cancel 143' TERM

if [[ "$requested_stage" == prepare || "$requested_stage" == boot \
   || "$requested_stage" == restricted-reboot-egress-persistence ]]; then
[[ "$(uname -m)" == x86_64 ]] || fail 'runner architecture is not x86_64'
[[ -e /dev/kvm || -L /dev/kvm ]] || fail '/dev/kvm is absent; TCG fallback is forbidden'
[[ -c /dev/kvm ]] || fail '/dev/kvm exists but is not a character device'
[[ -r /dev/kvm && -w /dev/kvm ]] \
  || fail '/dev/kvm is not readable and writable after the udev permission step; TCG fallback is forbidden'

for command in qemu-system-x86_64 qemu-img curl ssh ssh-keygen socat genisoimage python3 timeout sha256sum; do
  command -v "$command" >/dev/null 2>&1 || fail "required host command is missing: $command"
done

virtiofsd=''
for candidate in /usr/lib/virtiofsd /usr/libexec/virtiofsd /usr/lib/qemu/virtiofsd; do
  if [[ -x "$candidate" && ! -L "$candidate" ]]; then
    virtiofsd="$candidate"
    break
  fi
done
[[ -n "$virtiofsd" ]] || fail 'required virtiofsd executable is unavailable'
fi

tmp_root="${RUNNER_TEMP:-/tmp/opencode}"
mkdir -p "$tmp_root"
if [[ "$requested_stage" == prepare ]]; then
  python3 -I "$state_helper" init "${state_args[@]}" --overall-seconds 2400 \
    || fail 'could not create private stage handoff state'
  tmp="$state_dir/work"
  port="$(python3 - <<'PY'
import socket
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    print(listener.getsockname()[1])
PY
)"
  image="$tmp/noble.img"
  seed="$tmp/seed.iso"
  overlay="$tmp/guest.qcow2"
  key="$tmp/id_ed25519"
  known_hosts="$tmp/known_hosts"
  serial_log="$tmp/serial.log"
else
  python3 -I "$state_helper" validate "${state_args[@]}" --stage "$requested_stage" \
    || fail 'private stage state is absent, stale, or invalid'
  mapfile -d '' -t state_values < <(python3 -I "$state_helper" get "${state_args[@]}" \
    --stage "$requested_stage" \
    --field work image seed overlay serial_log private_key known_hosts port overlay_dev overlay_ino)
  ((${#state_values[@]} == 10)) || fail 'private stage state is incomplete'
  tmp=${state_values[0]}; image=${state_values[1]}; seed=${state_values[2]}
  overlay=${state_values[3]}; serial_log=${state_values[4]}; key=${state_values[5]}
  known_hosts=${state_values[6]}; port=${state_values[7]}
  overlay_device=${state_values[8]}; overlay_inode=${state_values[9]}
fi
oauth_evidence_root="$tmp/oauth-evidence"
if [[ "$requested_stage" == prepare ]]; then
  mkdir -m 700 "$oauth_evidence_root"
fi

if [[ "$requested_stage" != prepare && "$requested_stage" != cleanup ]]; then
  mapfile -d '' -t persisted_values < <(python3 -I "$state_helper" get "${state_args[@]}" \
    --stage "$requested_stage" \
    --field guest_boot_id known_hosts_sha256 persistence_content_sha256 persistence_mount_sha256 ssh_public_key_sha256 qemu_mode)
  # Later results may not exist until their prerequisite stage succeeds.
  guest_boot_id=${persisted_values[0]:-}
  known_hosts_sha256=${persisted_values[1]:-}
  persistence_content_sha256=${persisted_values[2]:-}
  persist_mount_sha256=${persisted_values[3]:-}
  ssh_public_key_sha256=${persisted_values[4]:-}
  expected_qemu_mode=${persisted_values[5]:-}
  [[ -f "$key" && ! -L "$key" && "$(stat -c %u:%a "$key")" == "$(id -u):600" ]] \
    || fail 'private SSH identity file is missing or unsafe'
  [[ "$(sha256sum "$key.pub" | awk '{print $1}')" == "$ssh_public_key_sha256" ]] \
    || fail 'SSH public identity differs from the prepared run state'
  if [[ -n "$known_hosts_sha256" ]]; then
    [[ -f "$known_hosts" && ! -L "$known_hosts" \
       && "$(stat -c %u:%a "$known_hosts")" == "$(id -u):600" \
       && "$(sha256sum "$known_hosts" | awk '{print $1}')" == "$known_hosts_sha256" ]] \
      || fail 'pinned SSH host-key file identity changed between stages'
  fi
fi
overall_remaining="$(python3 -I "$state_helper" get "${state_args[@]}" --field deadline_monotonic \
  --stage "$requested_stage" \
  | python3 -c 'import sys,time; print(max(0, int(float(sys.stdin.read().strip("\0")) - time.monotonic())))')"
if (( overall_remaining > 0 )); then
  qemu_deadline=$((SECONDS + overall_remaining))
fi
qemu_started=$SECONDS
if [[ "$requested_stage" != prepare && "$requested_stage" != cleanup ]]; then
  ssh_args=()
  ssh_tty_args=()
  ci_kvm_ssh_args 'mester@127.0.0.1' "$key" "$port" "$known_hosts" batch ssh_args
  ci_kvm_ssh_args 'mester@127.0.0.1' "$key" "$port" "$known_hosts" tty ssh_tty_args
  if [[ "$requested_stage" != boot ]]; then
    capture_qemu_identity || fail 'existing guest lifecycle identity could not be revalidated'
  fi
fi

printf 'KVM_CANDIDATE source_head_sha=%s workflow_sha=%s event=%s\n' \
  "${LM_CI_SOURCE_HEAD_SHA:-not-provided}" "${GITHUB_SHA:-local}" "${GITHUB_EVENT_NAME:-local}"
if [[ "$requested_stage" == prepare ]]; then
stage_start prepare 180
printf 'KVM_PROGRESS phase=pinned-image-download max_seconds=120\n'
curl --fail --location --silent --show-error --connect-timeout 20 --max-time 120 \
  "$image_url" --output "$image"
printf '%s  %s\n' "$image_sha256" "$image" | sha256sum --check --status \
  || fail 'pinned guest image SHA-256 mismatch'

ssh-keygen -q -t ed25519 -N '' -f "$key"
: >"$known_hosts"
chmod 0600 "$known_hosts"
LM_CI_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" \
LM_CI_PUBLIC_KEY="$(<"$key.pub")" \
python3 - <<'PY' >"$tmp/user-data"
import base64, importlib.util, json, os, pathlib
root = pathlib.Path(os.environ["LM_CI_REPO"])
spec = importlib.util.spec_from_file_location("session_contract", root / "lib/vm/parent-session-contract.py")
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)
files = [{"path": "/etc/opencode/opencode.json", "owner": "root:root", "permissions": "0644",
          "content": '{"autoupdate":false,"share":"disabled","permission":{"edit":"ask","bash":"ask"}}\n'}]
files.append({"path": "/etc/opencode/opencode-model.json", "owner": "root:root",
              "permissions": "0644", "content": contract.PROTECTED_CONFIG_MODEL})
files.extend({"path": path, "owner": "root:root", "permissions": mode, "encoding": "b64",
               "content": base64.b64encode(content).decode("ascii")}
              for path, (content, mode) in contract.ASSETS.items())
opencode_readiness = root / "lib/vm/opencode-readiness.sh"
files.append({"path": "/usr/local/libexec/lm-ci-opencode-readiness", "owner": "root:root",
              "permissions": "0755", "encoding": "b64",
              "content": base64.b64encode(opencode_readiness.read_bytes()).decode("ascii")})
opencode_config_check = root / "tests/ci-opencode-config-check.py"
files.append({"path": "/usr/local/libexec/lm-ci-opencode-config-check", "owner": "root:root",
              "permissions": "0755", "encoding": "b64",
              "content": base64.b64encode(opencode_config_check.read_bytes()).decode("ascii")})
for path, content in contract.PROTECTED_PLUGIN_ASSETS.items():
    files.append({"path": path,
                  "owner": "root:root", "permissions": "0644", "encoding": "b64",
                  "content": base64.b64encode(content).decode("ascii")})
install_command = r'''set -Eeuo pipefail
storage_report() {
  phase=$1
  root_source=$(findmnt -n -o SOURCE --target /)
  root_type=$(findmnt -n -o FSTYPE --target /)
  read -r block_total block_used block_available < <(df -B1 --output=size,used,avail / | tail -n 1)
  read -r inode_total inode_used inode_available < <(df --output=itotal,iused,iavail / | tail -n 1)
  printf 'KVM_GUEST_STORAGE phase=%s source=%s fstype=%s block_total=%s block_used=%s block_available=%s inode_total=%s inode_used=%s inode_available=%s\n' "$phase" "$root_source" "$root_type" "$block_total" "$block_used" "$block_available" "$inode_total" "$inode_used" "$inode_available"
  lsblk -b -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS
}
ssh_diagnostic() {
  phase=$1
  local key_check_log=/run/lm-hostkey-check.log key_meta=absent hostkey_state=absent key_error=''
  package_state=$(dpkg-query -W -f='${Status}' openssh-server 2>/dev/null || printf absent)
  service_active=$(systemctl is-active ssh.service 2>/dev/null || true)
  service_enabled=$(systemctl is-enabled ssh.service 2>/dev/null || true)
  socket_active=$(systemctl is-active ssh.socket 2>/dev/null || true)
  [[ -n "$service_active" ]] || service_active=unknown
  [[ -n "$service_enabled" ]] || service_enabled=unknown
  [[ -n "$socket_active" ]] || socket_active=unknown
  if [[ -f /etc/ssh/ssh_host_ed25519_key && ! -L /etc/ssh/ssh_host_ed25519_key ]]; then
    key_meta=$(stat -c 'mode=%a uid=%u gid=%g bytes=%s' /etc/ssh/ssh_host_ed25519_key 2>/dev/null || printf stat-failed)
    if timeout --signal=TERM --kill-after=1 3 ssh-keygen -y -P '' -f /etc/ssh/ssh_host_ed25519_key >/dev/null 2>"$key_check_log"; then
      hostkey_state=usable
    else
      hostkey_state=unusable
      key_error=$(sed -n '1p' "$key_check_log" 2>/dev/null | cut -c1-180 | tr -cd '\11\12\15\40-\176' || true)
    fi
  fi
  rm -f "$key_check_log"
  printf 'KVM_GUEST_SSH_DIAG phase=%s package=%s service_active=%s service_enabled=%s socket_active=%s ed25519_private=%s %s\n' "$phase" "$package_state" "$service_active" "$service_enabled" "$socket_active" "$hostkey_state" "$key_meta"
  [[ -z "$key_error" ]] || printf 'KVM_GUEST_SSH_KEY_ERROR detail=%s\n' "$key_error"
  systemctl show ssh.service -p ActiveState -p SubState -p Result -p ExecMainStatus --no-pager 2>/dev/null | cut -c1-160 || true
  printf '%s\n' 'KVM_GUEST_SSH_NETWORK ipv4-addresses:'
  ip -brief -4 address show 2>/dev/null | sed -n '1,6p' | cut -c1-180 || true
  printf '%s\n' 'KVM_GUEST_SSH_NETWORK ipv4-routes:'
  ip -4 route show 2>/dev/null | sed -n '1,6p' | cut -c1-180 || true
  printf '%s\n' 'KVM_GUEST_SSH_LISTENERS ipv4:'
  ss -H -lnt4 2>/dev/null | sed -n '1,8p' | cut -c1-180 || true
  printf '%s\n' 'KVM_GUEST_SSH_SERVICE_LOG:'
  journalctl -b -u ssh.service --no-pager -n 24 -o cat 2>/dev/null \
    | grep -Ei 'error|fail|listen|starting|started|host key|sshd' \
    | tail -n 8 | cut -c1-220 | tr -cd '\11\12\15\40-\176' || true
}
guest_stage() {
  stage=$1; shift
  current_provision_stage=$stage
  started=$SECONDS
  printf 'KVM_GUEST_PROVISION_STAGE event=START name=%s elapsed_s=0\n' "$stage"
  rc=0
  "$@" || rc=$?
  elapsed=$((SECONDS - started))
  if (( rc != 0 )); then
    printf 'KVM_GUEST_PROVISION_STAGE event=FAIL name=%s elapsed_s=%s exit=%s\n' "$stage" "$elapsed" "$rc" >&2
    storage_report "$stage-failed" || true
    printf 'KVM_GUEST_PROVISION_FAIL stage=%s exit=%s\n' "$stage" "$rc" >&2
    return "$rc"
  fi
  printf 'KVM_GUEST_PROVISION_STAGE event=END name=%s elapsed_s=%s\n' "$stage" "$elapsed"
  current_provision_stage=''
}
provision_error() {
  rc=$?
  trap - ERR
  failed_stage=${current_provision_stage:-unexpected}
  [[ "$failed_stage" == unexpected ]] || printf 'KVM_GUEST_PROVISION_STAGE event=FAIL name=%s elapsed_s=unknown exit=%s\n' "$failed_stage" "$rc" >&2
  printf 'KVM_GUEST_PROVISION_FAIL stage=%s exit=%s\n' "$failed_stage" "$rc" >&2
  storage_report "$failed_stage-failed" || true
  exit "$rc"
}
trap provision_error ERR
storage_report before-packages
min_root_bytes=$((8 * 1024 * 1024 * 1024))
min_root_total_bytes=$((20 * 1024 * 1024 * 1024))
min_free_inodes=100000
if (( block_total < min_root_total_bytes || block_available < min_root_bytes || inode_available < min_free_inodes )); then
  printf 'KVM_GUEST_STORAGE_PREFLIGHT_FAIL block_total=%s required_total=%s block_available=%s required_available=%s inode_available=%s required_inodes=%s\n' "$block_total" "$min_root_total_bytes" "$block_available" "$min_root_bytes" "$inode_available" "$min_free_inodes" >&2
  exit 70
fi
export DEBIAN_FRONTEND=noninteractive
guest_stage apt-update apt-get update || exit $?
guest_stage apt-install apt-get install -y --no-install-recommends tmux nodejs npm ca-certificates || exit $?
guest_stage opencode-install npm install --global @opencode/cli@2.2.5 || exit $?
guest_stage workspace-user-dirs install -d -o mester -g mester -m 0700 /home/mester/workspace || exit $?
guest_stage opencode-user-dirs /usr/bin/python3 -I /usr/local/libexec/lm-guest-user-dirs.py prepare-opencode 1000 || exit $?
# cloud-init's ssh module generates keys in its init stage, before runcmd.
# Keep early socket failures recoverable and report only key metadata.
current_provision_stage=ssh-service-listener-setup
ssh_stage_started=$SECONDS
printf 'KVM_GUEST_PROVISION_STAGE event=START name=ssh-service-listener-setup elapsed_s=0\n'
ssh_diagnostic before-ensure
systemctl reset-failed ssh.service || { ssh_diagnostic reset-failed-error; printf 'KVM_GUEST_SSH_SERVICE_FAIL stage=reset-failed\n' >&2; printf 'KVM_GUEST_PROVISION_STAGE event=FAIL name=ssh-service-listener-setup elapsed_s=%s exit=78\n' "$((SECONDS - ssh_stage_started))" >&2; storage_report ssh-setup-failed || true; printf 'KVM_GUEST_PROVISION_FAIL stage=ssh-service-listener-setup exit=78\n' >&2; exit 78; }
if ! systemctl is-active --quiet ssh.socket; then
  if ! systemctl enable --now ssh.socket >/dev/null 2>&1; then
    ssh_diagnostic socket-start-failed
    printf 'KVM_GUEST_SSH_SERVICE_FAIL stage=ssh-socket\n' >&2
    printf 'KVM_GUEST_PROVISION_STAGE event=FAIL name=ssh-service-listener-setup elapsed_s=%s exit=79\n' "$((SECONDS - ssh_stage_started))" >&2
    storage_report ssh-setup-failed || true
    printf 'KVM_GUEST_PROVISION_FAIL stage=ssh-service-listener-setup exit=79\n' >&2
    exit 79
  fi
fi
ssh_diagnostic after-user-script
if ! ss -H -lnt4 'sport = :22' | awk '$4 == "0.0.0.0:22" || ($4 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:22$/ && $4 !~ /^127\./) { found=1 } END { exit !found }'; then
  printf 'KVM_GUEST_SSH_SOCKET_FAIL reason=no-nonloopback-ipv4-listener port=22\n' >&2
  ssh_diagnostic socket-listener-missing
  printf 'KVM_GUEST_PROVISION_STAGE event=FAIL name=ssh-service-listener-setup elapsed_s=%s exit=74\n' "$((SECONDS - ssh_stage_started))" >&2
  storage_report ssh-setup-failed || true
  printf 'KVM_GUEST_PROVISION_FAIL stage=ssh-service-listener-setup exit=74\n' >&2
  exit 74
fi
printf 'KVM_GUEST_SSH_SOCKET state=ready address=nonloopback-ipv4 port=22\n'
printf 'KVM_GUEST_PROVISION_STAGE event=END name=ssh-service-listener-setup elapsed_s=%s\n' "$((SECONDS - ssh_stage_started))"
current_provision_stage=''
storage_report after-packages
printf ready > /var/tmp/lm-kvm-trial-ready
printf 'KVM_GUEST_PROVISION_DONE exit=0\n'
'''
config = {
    "ssh_pwauth": False,
    "disable_root": True,
    "ssh_deletekeys": True,
    "ssh_genkeytypes": ["ed25519"],
    "ssh_quiet_keygen": True,
    "growpart": {"mode": "growpart", "devices": ["/"], "ignore_growroot_disabled": True},
    "resize_rootfs": True,
    "users": [{"name": "mester", "uid": 1000, "primary_group": "mester",
               "shell": "/bin/bash", "lock_passwd": True,
               "ssh_authorized_keys": [os.environ["LM_CI_PUBLIC_KEY"]]}],
    "groups": ["mester"],
    "write_files": files,
    "runcmd": [["bash", "-lc", install_command]],
}
print("#cloud-config\n" + json.dumps(config, separators=(",", ":")))
PY
cat >"$tmp/meta-data" <<'EOF'
instance-id: little-mester-hosted-kvm-trial
local-hostname: little-mester-kvm-trial
EOF
genisoimage -quiet -output "$seed" -volid cidata -joliet -rock \
  "$tmp/user-data" "$tmp/meta-data"
base_virtual_bytes="$(qemu-img info --output=json "$image" | python3 -c 'import json,sys; print(json.load(sys.stdin)["virtual-size"])')"
[[ "$base_virtual_bytes" =~ ^[0-9]+$ ]] || fail 'pinned guest image reported an invalid virtual capacity'
(( base_virtual_bytes > 0 && base_virtual_bytes < guest_disk_bytes )) \
  || fail "pinned guest image capacity ${base_virtual_bytes} is not smaller than the disposable ${guest_disk_gib}-GiB target"
printf 'KVM_DISK_PLAN base_virtual_bytes=%s overlay_virtual_bytes=%s target_gib=%s resize_rootfs=true\n' \
  "$base_virtual_bytes" "$guest_disk_bytes" "$guest_disk_gib"
qemu-img create -q -f qcow2 -F qcow2 -b "$image" "$overlay"
qemu-img resize -f qcow2 "$overlay" "$guest_disk_bytes"
overlay_virtual_bytes="$(qemu-img info --output=json "$overlay" | python3 -c 'import json,sys; print(json.load(sys.stdin)["virtual-size"])')"
[[ "$overlay_virtual_bytes" == "$guest_disk_bytes" ]] \
  || fail "disposable overlay capacity mismatch: got ${overlay_virtual_bytes}, expected ${guest_disk_bytes}"
overlay_device="$(stat -c %d -- "$overlay")"
overlay_inode="$(stat -c %i -- "$overlay")"
verify_overlay_identity || fail 'fresh disposable overlay identity could not be recorded'
ssh_public_key_sha256="$(sha256sum "$key.pub" | awk '{print $1}')"
python3 -I "$state_helper" set "${state_args[@]}" \
  --field work --value "$tmp" --field image --value "$image" \
  --field seed --value "$seed" --field overlay --value "$overlay" \
  --field serial_log --value "$serial_log" --field private_key --value "$key" \
  --field known_hosts --value "$known_hosts" --field port --value "$port" \
  --field overlay_dev --value "$overlay_device" --field overlay_ino --value "$overlay_inode" \
  --field ssh_public_key_sha256 --value "$ssh_public_key_sha256" \
  || fail 'could not persist prepared guest identity'
python3 -I "$owner_helper" start-owner "${state_args[@]}" \
  || fail 'could not start the isolated QEMU lifecycle owner'
stage_end prepare
exit 0
fi

if [[ "$requested_stage" == boot ]]; then
stage_start boot 960
printf 'KVM_TRIAL: starting pinned guest with accel=kvm (no TCG fallback)\n'
qemu_started=$SECONDS
owner_call start-initial >/dev/null || fail 'lifecycle owner could not start initial QEMU guest'
expected_qemu_mode=initial
capture_qemu_identity || fail 'could not prove ownership of the launched QEMU process'
printf 'KVM_PROGRESS phase=qemu-started readiness_limit=900s stage_limit=960s\n'

pin_guest_host_key_from_serial() {
  local public_key fingerprint known_hosts_tmp public_key_file
  public_key="$(python3 "$(dirname -- "$process_signal_helper")/ci-kvm-hostkey.py" "$serial_log")" \
    || fail 'cloud-init completed without one valid Ed25519 host public key in the owned serial log'
  public_key_file="$tmp/guest_host_ed25519.pub"
  printf '%s\n' "$public_key" >"$public_key_file"
  chmod 0600 "$public_key_file"
  fingerprint="$(ssh-keygen -lf "$public_key_file" | awk '{print $2}')" \
    || fail 'cloud-init serial host public key failed OpenSSH validation'
  known_hosts_tmp="$tmp/known_hosts.tmp"
  printf '[127.0.0.1]:%s %s\n' "$port" "$public_key" >"$known_hosts_tmp"
  chmod 0600 "$known_hosts_tmp"
  mv -- "$known_hosts_tmp" "$known_hosts"
  printf 'KVM_GUEST_SSH_HOSTKEY_PINNED source=qemu-serial-cloud-init fingerprint=%s\n' "$fingerprint"
}

ready=0
qemu_started=$SECONDS
readiness_deadline=$((qemu_started + 900))
stage_remaining=$((stage_deadline - SECONDS))
(( readiness_deadline - SECONDS > stage_remaining )) && readiness_deadline=$((SECONDS + stage_remaining))
(( readiness_deadline > qemu_deadline )) && readiness_deadline=$qemu_deadline
next_progress=$((SECONDS + 15))
next_serial_tail=$((SECONDS + 30))
last_ssh_rc=not-attempted
while (( SECONDS < readiness_deadline && SECONDS < qemu_deadline )); do
  qemu_identity_matches || fail 'test-owned QEMU process exited or changed identity before guest readiness'
  qemu_state="$(process_state "$qemu_pid")"
  [[ -n "$qemu_state" && "$qemu_state" != Z ]] || fail 'QEMU exited before guest readiness'
  remaining=$((readiness_deadline - SECONDS))
  (( remaining > 0 )) || break
  # These markers are emitted by the fixed provisioning script only after a
  # definitive storage/package/provisioning failure. Inspect serial output
  # before waiting for cloud-init completion or SSH host-key publication.
  if grep -aEq 'KVM_GUEST_STORAGE_PREFLIGHT_FAIL|KVM_GUEST_PROVISION_FAIL|KVM_GUEST_SSH_SOCKET_FAIL' "$serial_log"; then
    printf '%s\n' 'KVM_GUEST_PROVISIONING_DIAGNOSTIC: explicit cloud-init provisioning/SSH failure marker observed' >&2
    grep -aE 'KVM_GUEST_(STORAGE|PROVISION|SSH)' "$serial_log" | tail -n 24 >&2 || true
    fail 'guest cloud-init provisioning or SSH socket failed before SSH readiness'
  fi
  attempt_timeout=8
  (( remaining < attempt_timeout )) && attempt_timeout=$remaining
  if grep -aEq 'KVM_GUEST_SSH_SERVICE_FAIL|KVM_GUEST_SSH_SOCKET_FAIL' "$serial_log" \
      || grep -aFq 'sshd: no hostkeys available -- exiting' "$serial_log"; then
    printf '%s\n' 'KVM_GUEST_SSH_DIAGNOSTIC: cloud-init host-key/socket preparation failed' >&2
    grep -aE 'KVM_GUEST_(SSH|PROVISION)' "$serial_log" | tail -n 24 >&2 || true
    fail 'guest SSH host-key or socket preparation failed before readiness'
  fi
  # The single-quoted payload is intentionally interpreted only by the guest shell.
  # shellcheck disable=SC2016
  # Do not connect through the already-listening ssh.socket until cloud-init
  # has completed and published its public host key, or sshd may start keyless.
  if ! grep -aEq 'Cloud-init v\..* finished at ' "$serial_log"; then
    last_ssh_rc=waiting-for-cloud-init-finish
  else
    if [[ ! -s "$known_hosts" ]]; then
      pin_guest_host_key_from_serial
    fi
  fi
  if [[ -s "$known_hosts" ]]; then
    if timeout --signal=TERM --kill-after=1 "$attempt_timeout" ssh "${ssh_args[@]}" \
        'systemctl is-active --quiet ssh.service && { state=$(cloud-init status --long 2>&1 || true); printf "%s\n" "$state"; if printf "%s\n" "$state" | grep -Eq "^(status|extended_status): error"; then exit 42; fi; test -f /var/lib/cloud/instance/boot-finished && test "$(cat /var/tmp/lm-kvm-trial-ready)" = ready; }' \
        >"$tmp/readiness.log" 2>&1; then
      ready=1
      break
    else
      last_ssh_rc=$?
      if grep -Eq '^(status|extended_status): error' "$tmp/readiness.log"; then
        cat "$tmp/readiness.log" >&2
        fail 'cloud-init reported an error before guest readiness'
      fi
    fi
  fi
  report_kvm_progress initial-readiness "$readiness_deadline" "$last_ssh_rc"
  remaining=$((readiness_deadline - SECONDS))
  (( remaining > 0 )) && sleep 1
done
[[ "$ready" == 1 ]] || fail "guest did not reach cloud-init/SSH readiness within 900s (last ssh status: $last_ssh_rc)"
guest_boot_id="$(ssh_guest 'cat /proc/sys/kernel/random/boot_id')"
[[ "$guest_boot_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] \
  || fail 'initial guest boot identity was not a valid kernel boot ID'
known_hosts_sha256="$(sha256sum "$known_hosts" | awk '{print $1}')"
[[ -n "$known_hosts_sha256" && "$(stat -c %a "$known_hosts")" == 600 ]] \
  || fail 'serial-pinned guest host identity is not held in a private known_hosts file'
printf 'KVM_TRIAL_PASS: pinned guest booted with accel=kvm and passed cloud-init readiness\n'
python3 -I "$state_helper" set "${state_args[@]}" \
  --field guest_boot_id --value "$guest_boot_id" \
  --field known_hosts_sha256 --value "$known_hosts_sha256" \
  --field qemu_mode --value initial \
  || fail 'could not persist pinned SSH and boot identity'
stage_end boot
exit 0
fi

if [[ "$requested_stage" == config-tmux ]]; then
guest_asset_checks="$(python3 - <<'PY'
import importlib.util, pathlib
path = pathlib.Path("lib/vm/parent-session-contract.py")
spec = importlib.util.spec_from_file_location("contract", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print("test -x /usr/local/libexec/lm-ci-opencode-readiness || exit 44; "
      "/usr/local/libexec/lm-ci-opencode-readiness || exit 44; "
      + module.readiness_checks(include_opencode=False))
PY
)"
stage_start config-tmux 180
ssh_guest "cloud-init status --long >/dev/null 2>&1 || exit 41; $guest_asset_checks"
ssh_guest 'test -x /usr/local/bin/opencode && tmux -V >/dev/null'
ssh_guest 'test "$(stat -c %u:%a /etc/opencode/opencode.json)" = "0:644" && grep -Fq "\"share\":\"disabled\"" /etc/opencode/opencode.json'
ssh_guest 'test "$(stat -c %u:%g:%a /etc/opencode/opencode-model.json)" = "0:0:644"'
# Initialize the disposable OpenCode DB with the pinned CLI before adding the
# test-only history table; a nonempty DB without its session schema is refused.
ssh_guest '/usr/bin/python3 -I /usr/local/libexec/lm-ci-opencode-config-check --workspace /home/mester/workspace --home /home/mester --protected-config /etc/opencode/opencode.json --protected-plugin /etc/opencode/plugins/little-mester-exit-confirm --initialize-database' 30
# Guest-shell payloads are intentionally single-quoted so the local shell does
# not expand substitutions before they execute inside the disposable guest.
# shellcheck disable=SC2016
ssh_guest 'mkdir -p /home/mester/.local/share/opencode /home/mester/workspace/.opencode && chmod 0775 /home/mester/.local /home/mester/.local/share /home/mester/.local/share/opencode && python3 -c '\''import sqlite3; db=sqlite3.connect("/home/mester/.local/share/opencode/opencode.db"); db.execute("create table session_fixture(value text)"); db.execute("insert into session_fixture values (?)", ("synthetic-session-history",)); db.commit(); db.close()'\'' && chmod 0664 /home/mester/.local/share/opencode/opencode.db && printf "{\"provider\":\"synthetic-fixture\",\"marker\":\"preserve-auth-bytes\"}" > /home/mester/.local/share/opencode/auth.json && chmod 0664 /home/mester/.local/share/opencode/auth.json && db_before=$(sha256sum /home/mester/.local/share/opencode/opencode.db) && auth_before=$(sha256sum /home/mester/.local/share/opencode/auth.json) && printf "{\"model\":\"hostile-direct-marker\",\"plugins\":[\"/tmp/lm-ci-hostile-plugin-sentinel\"]}" > /home/mester/workspace/opencode.json && printf "{\"model\":\"hostile-nested-marker\",\"plugins\":[\"/tmp/lm-ci-hostile-plugin-sentinel\"]}" > /home/mester/workspace/.opencode/opencode.json && /usr/bin/python3 -I /usr/local/libexec/lm-guest-user-dirs.py prepare-auth && test "$(stat -c %a /home/mester/.local/share/opencode/opencode.db)" = 600 && test "$(stat -c %a /home/mester/.local/share/opencode/auth.json)" = 600 && test "$(sha256sum /home/mester/.local/share/opencode/opencode.db)" = "$db_before" && test "$(sha256sum /home/mester/.local/share/opencode/auth.json)" = "$auth_before"'
readonly persistence_fixture_content='persistent-fixture'
persistence_content_sha256="$(printf '%s' "$persistence_fixture_content" | sha256sum | awk '{print $1}')"
readonly persistence_content_sha256
persist_mount_sha256=''
run_persistence_create
run_persistence_proof initial-creation ''
ssh_guest 'printf unsafe-target-preserved > /tmp/lm-auth-unsafe-target && ln -s /tmp/lm-auth-unsafe-target /home/mester/.local/share/opencode/opencode.db-wal; if /usr/bin/python3 -I /usr/local/libexec/lm-guest-user-dirs.py prepare-auth >/dev/null 2>&1; then exit 1; fi; test "$(cat /tmp/lm-auth-unsafe-target)" = unsafe-target-preserved; rm /home/mester/.local/share/opencode/opencode.db-wal'
ssh_guest '/usr/bin/python3 -I /usr/local/libexec/lm-ci-opencode-config-check --workspace /home/mester/workspace --home /home/mester --protected-config /etc/opencode/opencode.json --protected-plugin /etc/opencode/plugins/little-mester-exit-confirm --model-config /etc/opencode/opencode-model.json --model openai/gpt-5' 50
capture_tui_probe startup 8 "$tmp" "$tmp_root" ssh_tty_guest "$repo_root/tests/ci-tui-diagnostic.py" fail
# shellcheck disable=SC2016
ssh_guest 'test "$(stat -c %a /home/mester/.local/share/opencode/opencode.db)" = 600 && test "$(stat -c %a /home/mester/.local/share/opencode/auth.json)" = 600 && test "$(stat -c %a /home/mester/.local/share/opencode)" = 700 && test ! -e /etc/opencode/service.json && TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux has-session -t little-mester-opencode && test "$(TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux display-message -p -t little-mester-opencode:0.0 "#{pane_dead}")" = 0 && TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux show-environment -g OPENCODE_CONFIG | grep -Fx OPENCODE_CONFIG=/etc/opencode/opencode.json && TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux show-environment -g OPENCODE_DISABLE_PROJECT_CONFIG | grep -Fx OPENCODE_DISABLE_PROJECT_CONFIG=1'
pane_before="$(ssh_guest 'TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux display-message -p -t little-mester-opencode:0.0 "#{pane_id}:#{pane_pid}"')"
capture_tui_probe reattach 4 "$tmp" "$tmp_root" ssh_tty_guest "$repo_root/tests/ci-tui-diagnostic.py" fail
pane_after="$(ssh_guest 'TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux display-message -p -t little-mester-opencode:0.0 "#{pane_id}:#{pane_pid}"')"
[[ "$pane_before" == "$pane_after" ]] || fail 'tmux reattachment did not preserve the live OpenCode pane identity'
printf 'KVM_M2_INTEGRATION_PASS: real KVM guest, SSH, pinned OpenCode 2.2.5, protected config, tmux TUI process and persistent workspace fixture\n'
python3 -I "$state_helper" set "${state_args[@]}" \
  --field persistence_content_sha256 --value "$persistence_content_sha256" \
  --field persistence_mount_sha256 --value "$persist_mount_sha256" \
  || fail 'could not persist guest persistence fingerprints'
stage_end config-tmux
exit 0
fi

if [[ "$requested_stage" == restricted-reboot-egress-persistence ]]; then
stage_start restricted-reboot-egress-persistence 90
provider_ips="$(timeout --signal=TERM --kill-after=2 12 python3 - <<'PY'
import ipaddress, socket
addresses = set()
for host in ("api.openai.com", "auth.openai.com"):
    for family, _kind, _proto, _canon, address in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM):
        ip = ipaddress.ip_address(address[0])
        if ip.is_global:
            addresses.add(str(ip))
for item in sorted(addresses)[:4]:
    print(item)
PY
)"
[[ -n "$provider_ips" ]] || fail 'could not resolve public OpenAI HTTPS endpoints for the restricted-egress probe'
probe_argv="$(python3 - "$provider_ips" <<'PY'
import ipaddress, shlex, sys
items = sys.argv[1].split()
if not items or len(items) > 4:
    raise SystemExit("invalid bounded provider IP list")
for item in items:
    if ipaddress.ip_address(item).version != 4:
        raise SystemExit("egress probe expected IPv4 literals")
print(" ".join(shlex.quote(item) for item in items))
PY
)"

# Reboot the same writable overlay to prove guest-disk durability without
# copying or reinitializing the VM disk. The in-memory tmux socket is expected
# to be recreated; the session/workspace files remain on the persistent disk.
run_persistence_flush
run_persistence_proof pre-reboot "$persist_mount_sha256"
stop_owned_qemu || fail 'test-owned QEMU did not stop or could not be reaped; preserving its artifacts'
verify_overlay_identity || fail 'disposable guest disk identity changed before restricted reboot'
[[ "$(sha256sum "$known_hosts" | awk '{print $1}')" == "$known_hosts_sha256" ]] \
  || fail 'pinned guest host identity changed before restricted reboot'
owner_call restart-restricted >/dev/null \
  || fail 'lifecycle owner could not restart the same overlay in restricted mode'
expected_qemu_mode='restricted-restart'
capture_qemu_identity || fail 'could not prove ownership of the restricted-boot QEMU process'
[[ "$(sha256sum "$known_hosts" | awk '{print $1}')" == "$known_hosts_sha256" ]] \
  || fail 'pinned guest host identity changed while restarting restricted QEMU'
ready=0
readiness_deadline=$((SECONDS + 60))
stage_left=$((stage_deadline - SECONDS))
(( readiness_deadline - SECONDS > stage_left )) && readiness_deadline=$((SECONDS + stage_left))
next_progress=$((SECONDS + 15))
next_serial_tail=$((SECONDS + 30))
last_ssh_rc=not-attempted
last_ssh_class=not-attempted
restart_readiness_command="$(ci_kvm_restart_readiness_command "$guest_boot_id")"
while (( SECONDS < readiness_deadline && SECONDS < qemu_deadline )); do
  qemu_identity_matches || fail 'test-owned QEMU process exited or changed identity during persistent-disk reboot'
  verify_overlay_identity || fail 'disposable guest disk identity changed during restricted reboot readiness'
  qemu_state="$(process_state "$qemu_pid")"
  [[ -n "$qemu_state" && "$qemu_state" != Z ]] || fail 'QEMU exited during persistent-disk reboot'
  remaining=$((readiness_deadline - SECONDS))
  overall_remaining=$((qemu_deadline - SECONDS))
  (( overall_remaining < remaining )) && remaining=$overall_remaining
  (( remaining > 0 )) || break
  attempt_timeout=8
  (( remaining < attempt_timeout )) && attempt_timeout=$remaining
  # shellcheck disable=SC2016
  if python3 -I "$repo_root/lib/vm/bounded-log.py" \
      "$tmp/restart-readiness.raw" 8192 --fail-on-overflow -- \
      timeout --signal=TERM --kill-after=1 "$attempt_timeout" ssh "${ssh_args[@]}" \
        "$restart_readiness_command"; then
    last_ssh_rc=0
  else
    last_ssh_rc=$?
  fi
  last_ssh_class="$(python3 tests/ci-kvm-readiness.py --exit-status "$last_ssh_rc" \
    --input "$tmp/restart-readiness.raw")"
  if [[ "$last_ssh_class" == ready ]]; then
    ready=1
    break
  fi
  if (( SECONDS == readiness_deadline || SECONDS == qemu_deadline )); then
    break
  fi
  report_kvm_progress restricted-reboot-readiness "$readiness_deadline" \
    "$last_ssh_rc" "$last_ssh_class"
  remaining=$((readiness_deadline - SECONDS))
  (( remaining > 0 )) && sleep 1
done
[[ "$ready" == 1 ]] || {
  printf 'KVM_RESTART_SSH_READINESS_FAILURE class=%s exit=%s\n' "$last_ssh_class" "$last_ssh_rc" >&2
  fail "restricted reboot did not become SSH-ready within 60s (last ssh status: $last_ssh_rc)"
}
run_persistence_proof post-reboot "$persist_mount_sha256"
ssh_guest "python3 -c 'import socket,sys; targets=[(ip,443) for ip in sys.argv[1:]]+[(\"10.0.2.100\",3128)]; connected=[target for target in targets if (lambda s: (s.settimeout(2),s.connect_ex(target)==0,s.close())[1])(socket.socket())]; assert not connected, \"restricted guest unexpectedly reached an upstream/proxy endpoint\"' $probe_argv"
printf 'KVM_EGRESS_BOUNDARY_PASS: a bounded sample of resolved provider HTTPS addresses and the configured proxy TCP endpoint were unreachable in restricted QEMU mode\n'
ssh_guest 'test "$(cat /home/mester/.local/share/opencode/auth.json)" = "{\"provider\":\"synthetic-fixture\",\"marker\":\"preserve-auth-bytes\"}" && test "$(stat -c %a /home/mester/.local/share/opencode/auth.json)" = 600 && python3 -c '\''import sqlite3; db=sqlite3.connect("/home/mester/.local/share/opencode/opencode.db"); assert db.execute("select value from session_fixture").fetchone() == ("synthetic-session-history",); db.close()'\'''
capture_tui_probe reboot 8 "$tmp" "$tmp_root" ssh_tty_guest "$repo_root/tests/ci-tui-diagnostic.py" fail
ssh_guest 'TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux has-session -t little-mester-opencode && test "$(TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux display-message -p -t little-mester-opencode:0.0 "#{pane_dead}")" = 0 && test "$(TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux display-message -p -t little-mester-opencode:0.0 "#{pane_current_path}")" = /home/mester/workspace'
printf 'KVM_PERSISTENCE_PASS: synthetic OpenCode DB/auth fixtures, workspace data, and TUI recreation survived disposable guest reboot\n'
python3 -I "$state_helper" set "${state_args[@]}" \
  --field qemu_mode --value restricted-restart \
  || fail 'could not persist restricted-restart generation'
stage_end restricted-reboot-egress-persistence
exit 0
fi

if [[ "$requested_stage" == synthetic-oauth ]]; then
stage_start synthetic-oauth 120
qemu_identity_matches || fail 'test-owned QEMU identity changed before synthetic OAuth stage'
verify_overlay_identity || fail 'disposable guest disk identity changed before synthetic OAuth stage'
oauth_remaining="$(ci_kvm_stage_runtime_seconds \
  "$stage_deadline" "$qemu_deadline" "$SECONDS" 120)" \
  || fail 'synthetic OAuth stage had no remaining bounded stage/overall runtime'
ci_launch_tracked oauth_pid env TMPDIR=/tmp python3 tests/ci-qemu-oauth-bridge.py \
  --port "$port" --identity "$key" --known-hosts "$known_hosts" \
  --evidence-root "$oauth_evidence_root" --max-runtime-seconds "$oauth_remaining"
oauth_starttime="$(process_starttime "$oauth_pid" 2>/dev/null || true)"
oauth_uid="$(process_uid "$oauth_pid")"
oauth_parent="$BASHPID"
oauth_status=0
oauth_cleanup_status=0
if ! ci_wait_tracked_and_cleanup oauth_pid oauth_status oauth_cleanup_status stop_oauth_harness; then
  printf 'KVM_OAUTH_CLEANUP_FAILURE primary_exit=%s cleanup_exit=%s evidence_retained=%s\n' \
    "$oauth_status" "$oauth_cleanup_status" "$oauth_evidence_root" >&2
  fail 'synthetic OAuth harness cleanup/descendant proof failed'
fi
[[ "$oauth_status" == 0 ]] || fail "synthetic OAuth harness failed or was cancelled (status $oauth_status)"
printf 'KVM_M2_AUTH_BOUNDARY: live browser approval, provider token exchange and real model access are NOT_RUN\n'
stage_end synthetic-oauth
exit 0
fi

if [[ "$requested_stage" == cleanup ]]; then
  stage_start cleanup 120
  if [[ -S "$state_dir/owner.sock" ]]; then
    owner_call shutdown >/dev/null || fail 'lifecycle owner did not stop/reap QEMU'
  elif [[ -e "$state_dir/owner.sock" || -L "$state_dir/owner.sock" ]]; then
    fail 'lifecycle owner socket path has an unexpected type'
  fi
  stage_end cleanup
  exit 0
fi
