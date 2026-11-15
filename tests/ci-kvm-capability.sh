#!/usr/bin/env bash
# Credential-free hosted-runner M2 integration on a disposable Ubuntu guest.
# OAuth/provider credentials and real model access are never used here.
# Shell snippets passed through SSH are intentionally single-quoted locally.
# shellcheck disable=SC2016
set -Eeuo pipefail
umask 077
process_signal_helper="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/ci-process-signal.py"
# shellcheck source=tests/ci-kvm-launch.sh
source "$(dirname -- "$process_signal_helper")/ci-kvm-launch.sh"
# shellcheck source=tests/ci-kvm-contract.sh
source "$(dirname -- "$process_signal_helper")/ci-kvm-contract.sh"
# shellcheck source=tests/ci-kvm-qemu-stop.sh
source "$(dirname -- "$process_signal_helper")/ci-kvm-qemu-stop.sh"
# shellcheck source=tests/ci-tui-capture.sh
source "$(dirname -- "$process_signal_helper")/ci-tui-capture.sh"
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
# Reuse the smoke harness's exact-identity/pidfd stop primitive for this direct
# disposable-QEMU child; lifecycle and disk policy remain CI-specific.
# shellcheck source=lib/vm/host-launcher.sh
source "$repo_root/lib/vm/host-launcher.sh"
VM_HOST_SCRIPT_ROOT="$repo_root"

readonly image_url='https://cloud-images.ubuntu.com/releases/noble/release-20261017/ubuntu-24.04-server-cloudimg-amd64.img'
readonly image_sha256='04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94'
readonly guest_disk_gib=24
readonly guest_disk_bytes=$((guest_disk_gib * 1024 * 1024 * 1024))

fail() {
  printf 'KVM_TRIAL_FAIL: %s\n' "$1" >&2
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

qemu_identity_matches() {
  local actual_start actual_uid comm stat_line rest parent
  [[ -n "$qemu_pid" && -n "$qemu_starttime" && -n "$qemu_uid" && -n "$qemu_parent" ]] || return 1
  actual_start="$(process_starttime "$qemu_pid")" || return 1
  [[ "$actual_start" == "$qemu_starttime" ]] || return 1
  actual_uid="$(process_uid "$qemu_pid")"
  [[ "$actual_uid" == "$qemu_uid" ]] || return 1
  stat_line="$(<"/proc/$qemu_pid/stat")" || return 1
  rest="${stat_line##*) }"
  local -a fields
  read -r -a fields <<<"$rest"
  parent="${fields[1]:-}"
  [[ "$parent" == "$qemu_parent" ]] || return 1
  comm="$(</proc/"$qemu_pid"/comm)" || return 1
  [[ "$comm" == qemu-system-x86 ]] \
    && grep -azFq -- 'little-mester-hosted-kvm-integration' "/proc/$qemu_pid/cmdline" \
    && grep -azFq -- "$overlay" "/proc/$qemu_pid/cmdline" \
    && grep -azFq -- "hostfwd=tcp:127.0.0.1:$port-:22" "/proc/$qemu_pid/cmdline" \
    && grep -azFq -- 'accel=kvm' "/proc/$qemu_pid/cmdline" \
    && ! grep -azFq -- 'accel=tcg' "/proc/$qemu_pid/cmdline" \
    && { (( ! qemu_restricted )) || grep -azFq -- 'restrict=on' "/proc/$qemu_pid/cmdline"; } \
    && { (( qemu_restricted )) || ! grep -azFq -- 'restrict=on' "/proc/$qemu_pid/cmdline"; }
}

capture_qemu_identity() {
  local stat_line rest parent comm
  for _ in {1..20}; do
    if [[ -r "/proc/$qemu_pid/stat" ]]; then
      stat_line="$(<"/proc/$qemu_pid/stat")" || { sleep 0.05; continue; }
      rest="${stat_line##*) }"
      local -a fields
      read -r -a fields <<<"$rest"
      parent="${fields[1]:-}"
      comm="$(</proc/"$qemu_pid"/comm)"
      if [[ "$parent" == "$BASHPID" && "$comm" == qemu-system-x86 ]] \
          && grep -azFq -- 'little-mester-hosted-kvm-integration' "/proc/$qemu_pid/cmdline" \
          && grep -azFq -- "$overlay" "/proc/$qemu_pid/cmdline"; then
        qemu_starttime="$(process_starttime "$qemu_pid")"
        qemu_uid="$(process_uid "$qemu_pid")"
        qemu_parent="$BASHPID"
        qemu_identity_matches && return 0
      fi
    fi
    sleep 0.05
  done
  return 1
}

oauth_evidence_is_clear() {
  local first_entry
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

clear_qemu_tracking() {
  qemu_pid=''
  qemu_starttime=''
  qemu_uid=''
  qemu_parent=''
}

reap_exited_qemu() {
  local state
  [[ -n "$qemu_pid" ]] || return 0
  state="$(process_state "$qemu_pid")"
  [[ -z "$state" || "$state" == Z ]] || return 1
  wait "$qemu_pid" 2>/dev/null || true
  clear_qemu_tracking
}

stop_owned_qemu() {
  local mode=${1:-stage} identity_file='' stop_deadline_ms
  [[ -n "$qemu_pid" ]] || return 0
  stop_deadline_ms="$(ci_qemu_stop_deadline_ms "$mode" "$stage_deadline" "$qemu_deadline" "$SECONDS")"
  (( stop_deadline_ms > 0 )) || return 1
  reap_exited_qemu && return 0

  if [[ -z "$qemu_starttime" || -z "$qemu_uid" || -z "$qemu_parent" ]]; then
    # Recover only the direct child identity captured by this owning shell.
    capture_qemu_identity || true
  fi
  qemu_identity_matches || return 1

  identity_file="$tmp/qemu-$qemu_pid.identity"
  if [[ ! -e "$identity_file" && ! -L "$identity_file" ]]; then
    vm_host_record_process_identity "$identity_file" "$qemu_pid" \
      "$(command -v qemu-system-x86_64)" little-mester-hosted-kvm-integration \
      || { reap_exited_qemu && return 0; return 1; }
  fi
  vm_host_process_identity_status "$identity_file" \
    || { reap_exited_qemu && return 0; return 1; }
  qemu_identity_matches || { reap_exited_qemu && return 0; return 1; }
  if ! ci_qemu_stop_identity_and_reap "$identity_file" "$qemu_pid" \
      "$(command -v qemu-system-x86_64)" little-mester-hosted-kvm-integration "$stop_deadline_ms"; then
    reap_exited_qemu && return 0
    return 1
  fi
  clear_qemu_tracking
}

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

tmp_root="${RUNNER_TEMP:-/tmp/opencode}"
mkdir -p "$tmp_root"
tmp="$(mktemp -d "$tmp_root/lm-kvm-trial.XXXXXXXX")"
oauth_evidence_root="$tmp/oauth-evidence"
mkdir -m 700 "$oauth_evidence_root"
qemu_pid=''
qemu_starttime=''
qemu_uid=''
qemu_parent=''
qemu_restricted=0
overlay_device=''
overlay_inode=''
oauth_pid=''
oauth_starttime=''
oauth_uid=''
oauth_parent=''
oauth_cleanup_unproved=0
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
  printf 'KVM_STAGE_PASS name=%s\n::endgroup::\n' "$1"
  stage_name=''
  stage_deadline=0
}

cleanup() {
  local status=$?
  # Bash may run the inherited EXIT trap in the async launch subshell as well;
  # only the owning top-level shell may stop processes or remove artifacts.
  [[ "$BASHPID" == "$$" ]] || return "$status"
  # A repeated cancellation must not interrupt ownership verification or
  # bounded child reaping once cleanup has started.
  trap '' INT TERM
  if [[ "$status" == 130 || "$status" == 143 ]]; then
    printf 'KVM_TRIAL_FAIL: cancelled by signal (status %s); cleaning only the disposable guest\n' "$status" >&2
  fi
  if [[ -n "$oauth_pid" ]] && ! stop_oauth_harness; then
    printf 'KVM_TRIAL_FAIL: OAuth harness did not stop; preserving disposable guest diagnostics at %s\n' "$tmp" >&2
    status=1
  fi
  if [[ -n "$qemu_pid" ]] && ! stop_owned_qemu cleanup; then
    printf 'KVM_TRIAL_FAIL: test-owned QEMU stop was not proved; preserving diagnostics at %s\n' "$tmp" >&2
    status=1
  fi
  if (( oauth_cleanup_unproved )) || ! oauth_evidence_is_clear; then
    printf 'KVM_TRIAL_FAIL: OAuth descendant cleanup/evidence is not proved; retaining diagnostics at %s\n' "$tmp" >&2
    status=1
  fi
  if [[ -z "$qemu_pid" && -z "$oauth_pid" ]] \
      && (( ! oauth_cleanup_unproved )) && oauth_evidence_is_clear; then
    rm -rf -- "$tmp"
  else
    printf 'KVM_TRIAL_FAIL: disposable VM artifacts retained at %s because process cleanup was not proved\n' "$tmp" >&2
  fi
  if [[ -n "$stage_name" ]]; then
    printf 'KVM_STAGE_ABORT name=%s cleanup_status=%s\n::endgroup::\n' "$stage_name" "$status" >&2
  fi
  exit "$status"
}
trap cleanup EXIT
  trap 'ci_handle_cancel 130' INT
  trap 'ci_handle_cancel 143' TERM

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

printf 'KVM_CANDIDATE source_head_sha=%s workflow_sha=%s event=%s\n' \
  "${LM_CI_SOURCE_HEAD_SHA:-not-provided}" "${GITHUB_SHA:-local}" "${GITHUB_EVENT_NAME:-local}"
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
provision_error() {
  rc=$?
  trap - ERR
  printf 'KVM_GUEST_PROVISION_FAIL stage=unexpected exit=%s\n' "$rc" >&2
  storage_report unexpected-failure || true
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
apt-get update -qq || { rc=$?; storage_report apt-update-failed; printf 'KVM_GUEST_PROVISION_FAIL stage=apt-update exit=%s\n' "$rc" >&2; exit "$rc"; }
apt-get install -y --no-install-recommends tmux nodejs npm ca-certificates || { rc=$?; storage_report apt-install-failed; printf 'KVM_GUEST_PROVISION_FAIL stage=apt-install exit=%s\n' "$rc" >&2; exit "$rc"; }
npm install --global @opencode/cli@2.2.5 || { rc=$?; storage_report opencode-install-failed; printf 'KVM_GUEST_PROVISION_FAIL stage=opencode-install exit=%s\n' "$rc" >&2; exit "$rc"; }
install -d -o mester -g mester -m 0700 /home/mester/workspace
/usr/bin/python3 -I /usr/local/libexec/lm-guest-user-dirs.py prepare-opencode 1000
# cloud-init's ssh module generates keys in its init stage, before runcmd.
# Keep early socket failures recoverable and report only key metadata.
ssh_diagnostic before-ensure
systemctl reset-failed ssh.service || { ssh_diagnostic reset-failed-error; printf 'KVM_GUEST_SSH_SERVICE_FAIL stage=reset-failed\n' >&2; exit 78; }
if ! systemctl is-active --quiet ssh.socket; then
  if ! systemctl enable --now ssh.socket >/dev/null 2>&1; then
    ssh_diagnostic socket-start-failed
    printf 'KVM_GUEST_SSH_SERVICE_FAIL stage=ssh-socket\n' >&2
    exit 79
  fi
fi
ssh_diagnostic after-user-script
if ! ss -H -lnt4 'sport = :22' | awk '$4 == "0.0.0.0:22" || ($4 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:22$/ && $4 !~ /^127\./) { found=1 } END { exit !found }'; then
  printf 'KVM_GUEST_SSH_SOCKET_FAIL reason=no-nonloopback-ipv4-listener port=22\n' >&2
  ssh_diagnostic socket-listener-missing
  exit 74
fi
printf 'KVM_GUEST_SSH_SOCKET state=ready address=nonloopback-ipv4 port=22\n'
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
stage_end prepare

stage_start boot 120
printf 'KVM_TRIAL: starting pinned guest with accel=kvm (no TCG fallback)\n'
qemu_started=$SECONDS
qemu_deadline=$((qemu_started + 300))
initial_qemu_args=()
ci_kvm_qemu_args initial "$overlay" "$seed" "$serial_log" "$port" initial_qemu_args
ci_launch_tracked qemu_pid qemu-system-x86_64 "${initial_qemu_args[@]}"
qemu_starttime=''
qemu_uid=''
capture_qemu_identity || fail 'could not prove ownership of the launched QEMU process'
printf 'KVM_PROGRESS phase=qemu-started readiness_limit=120s integration_limit=300s\n'

ssh_args=()
ssh_tty_args=()
ci_kvm_ssh_args 'mester@127.0.0.1' "$key" "$port" "$known_hosts" batch ssh_args
ci_kvm_ssh_args 'mester@127.0.0.1' "$key" "$port" "$known_hosts" tty ssh_tty_args

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
readiness_deadline=$((qemu_started + 120))
stage_remaining=$((stage_deadline - SECONDS))
(( readiness_deadline - SECONDS > stage_remaining )) && readiness_deadline=$((SECONDS + stage_remaining))
next_progress=$((SECONDS + 15))
next_serial_tail=$((SECONDS + 30))
last_ssh_rc=not-attempted
while (( SECONDS < readiness_deadline && SECONDS < qemu_deadline )); do
  qemu_identity_matches || fail 'test-owned QEMU process exited or changed identity before guest readiness'
  qemu_state="$(process_state "$qemu_pid")"
  [[ -n "$qemu_state" && "$qemu_state" != Z ]] || fail 'QEMU exited before guest readiness'
  remaining=$((readiness_deadline - SECONDS))
  (( remaining > 0 )) || break
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
      if grep -aEq 'KVM_GUEST_STORAGE_PREFLIGHT_FAIL|KVM_GUEST_PROVISION_FAIL|KVM_GUEST_SSH_SOCKET_FAIL' "$serial_log"; then
        printf '%s\n' 'KVM_GUEST_PROVISIONING_DIAGNOSTIC: explicit cloud-init provisioning/SSH failure marker observed' >&2
        grep -aE 'KVM_GUEST_(STORAGE|PROVISION|SSH)' "$serial_log" | tail -n 24 >&2 || true
        fail 'guest cloud-init provisioning or SSH socket failed before SSH readiness'
      fi
    fi
  fi
  report_kvm_progress initial-readiness "$readiness_deadline" "$last_ssh_rc"
  remaining=$((readiness_deadline - SECONDS))
  (( remaining > 0 )) && sleep 1
done
[[ "$ready" == 1 ]] || fail "guest did not reach cloud-init/SSH readiness within 120s (last ssh status: $last_ssh_rc)"
guest_boot_id="$(ssh_guest 'cat /proc/sys/kernel/random/boot_id')"
[[ "$guest_boot_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] \
  || fail 'initial guest boot identity was not a valid kernel boot ID'
known_hosts_sha256="$(sha256sum "$known_hosts" | awk '{print $1}')"
[[ -n "$known_hosts_sha256" && "$(stat -c %a "$known_hosts")" == 600 ]] \
  || fail 'serial-pinned guest host identity is not held in a private known_hosts file'
printf 'KVM_TRIAL_PASS: pinned guest booted with accel=kvm and passed cloud-init readiness\n'
stage_end boot

ssh_guest() {
  local remaining=$((qemu_deadline - SECONDS)) limit=${2:-20} stage_left
  (( remaining > 0 )) || fail 'QEMU integration exceeded its overall bounded deadline'
  stage_left="$(stage_remaining)"
  (( remaining > stage_left )) && remaining=$stage_left
  qemu_identity_matches || fail 'test-owned QEMU identity changed before guest SSH stage'
  verify_overlay_identity || fail 'disposable guest disk identity changed before guest SSH stage'
  (( remaining < limit )) && limit=$remaining
  timeout --signal=TERM --kill-after=1 "$limit" ssh "${ssh_args[@]}" "$1"
}
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

ssh_guest 'test -x /usr/local/bin/opencode && tmux -V >/dev/null'
ssh_guest 'test "$(stat -c %u:%a /etc/opencode/opencode.json)" = "0:644" && grep -Fq "\"share\":\"disabled\"" /etc/opencode/opencode.json'
# Initialize the disposable OpenCode DB with the pinned CLI before adding the
# test-only history table; a nonempty DB without its session schema is refused.
ssh_guest '/usr/bin/python3 -I /usr/local/libexec/lm-ci-opencode-config-check --workspace /home/mester/workspace --home /home/mester --protected-config /etc/opencode/opencode.json --protected-plugin /etc/opencode/plugins/little-mester-exit-confirm --initialize-database' 30
# Guest-shell payloads are intentionally single-quoted so the local shell does
# not expand substitutions before they execute inside the disposable guest.
# shellcheck disable=SC2016
ssh_guest 'mkdir -p /home/mester/.local/share/opencode /home/mester/workspace/.opencode && chmod 0775 /home/mester/.local /home/mester/.local/share /home/mester/.local/share/opencode && python3 -c '\''import sqlite3; db=sqlite3.connect("/home/mester/.local/share/opencode/opencode.db"); db.execute("create table session_fixture(value text)"); db.execute("insert into session_fixture values (?)", ("synthetic-session-history",)); db.commit(); db.close()'\'' && chmod 0664 /home/mester/.local/share/opencode/opencode.db && printf "{\"provider\":\"synthetic-fixture\",\"marker\":\"preserve-auth-bytes\"}" > /home/mester/.local/share/opencode/auth.json && chmod 0664 /home/mester/.local/share/opencode/auth.json && db_before=$(sha256sum /home/mester/.local/share/opencode/opencode.db) && auth_before=$(sha256sum /home/mester/.local/share/opencode/auth.json) && printf persistent-fixture > /home/mester/workspace/persisted.txt && chmod 0600 /home/mester/workspace/persisted.txt && printf "{\"model\":\"hostile-direct-marker\",\"plugins\":[\"/tmp/lm-ci-hostile-plugin-sentinel\"]}" > /home/mester/workspace/opencode.json && printf "{\"model\":\"hostile-nested-marker\",\"plugins\":[\"/tmp/lm-ci-hostile-plugin-sentinel\"]}" > /home/mester/workspace/.opencode/opencode.json && /usr/bin/python3 -I /usr/local/libexec/lm-guest-user-dirs.py prepare-auth && test "$(stat -c %a /home/mester/.local/share/opencode/opencode.db)" = 600 && test "$(stat -c %a /home/mester/.local/share/opencode/auth.json)" = 600 && test "$(sha256sum /home/mester/.local/share/opencode/opencode.db)" = "$db_before" && test "$(sha256sum /home/mester/.local/share/opencode/auth.json)" = "$auth_before"'
ssh_guest 'printf unsafe-target-preserved > /tmp/lm-auth-unsafe-target && ln -s /tmp/lm-auth-unsafe-target /home/mester/.local/share/opencode/opencode.db-wal; if /usr/bin/python3 -I /usr/local/libexec/lm-guest-user-dirs.py prepare-auth >/dev/null 2>&1; then exit 1; fi; test "$(cat /tmp/lm-auth-unsafe-target)" = unsafe-target-preserved; rm /home/mester/.local/share/opencode/opencode.db-wal'
ssh_guest '/usr/bin/python3 -I /usr/local/libexec/lm-ci-opencode-config-check --workspace /home/mester/workspace --home /home/mester --protected-config /etc/opencode/opencode.json --protected-plugin /etc/opencode/plugins/little-mester-exit-confirm' 50
capture_tui_probe startup 8 "$tmp" "$tmp_root"
# shellcheck disable=SC2016
ssh_guest 'test "$(stat -c %a /home/mester/.local/share/opencode/opencode.db)" = 600 && test "$(stat -c %a /home/mester/.local/share/opencode/auth.json)" = 600 && test "$(stat -c %a /home/mester/.local/share/opencode)" = 700 && test ! -e /etc/opencode/service.json && TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux has-session -t little-mester-opencode && test "$(TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux display-message -p -t little-mester-opencode:0.0 "#{pane_dead}")" = 0 && TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux show-environment -g OPENCODE_CONFIG | grep -Fx OPENCODE_CONFIG=/etc/opencode/opencode.json && TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux show-environment -g OPENCODE_DISABLE_PROJECT_CONFIG | grep -Fx OPENCODE_DISABLE_PROJECT_CONFIG=1'
pane_before="$(ssh_guest 'TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux display-message -p -t little-mester-opencode:0.0 "#{pane_id}:#{pane_pid}"')"
capture_tui_probe reattach 4 "$tmp" "$tmp_root"
pane_after="$(ssh_guest 'TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux display-message -p -t little-mester-opencode:0.0 "#{pane_id}:#{pane_pid}"')"
[[ "$pane_before" == "$pane_after" ]] || fail 'tmux reattachment did not preserve the live OpenCode pane identity'
printf 'KVM_M2_INTEGRATION_PASS: real KVM guest, SSH, pinned OpenCode 2.2.5, protected config, tmux TUI process and persistent workspace fixture\n'
stage_end config-tmux

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
stop_owned_qemu || fail 'test-owned QEMU did not stop or could not be reaped; preserving its artifacts'
verify_overlay_identity || fail 'disposable guest disk identity changed before restricted reboot'
[[ "$(sha256sum "$known_hosts" | awk '{print $1}')" == "$known_hosts_sha256" ]] \
  || fail 'pinned guest host identity changed before restricted reboot'
qemu_restricted=1
restricted_qemu_args=()
ci_kvm_qemu_args restricted-restart "$overlay" "$seed" "$serial_log" "$port" restricted_qemu_args
ci_launch_tracked qemu_pid qemu-system-x86_64 "${restricted_qemu_args[@]}"
qemu_starttime=''
qemu_uid=''
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
ssh_guest "python3 -c 'import socket,sys; targets=[(ip,443) for ip in sys.argv[1:]]+[(\"10.0.2.100\",3128)]; connected=[target for target in targets if (lambda s: (s.settimeout(2),s.connect_ex(target)==0,s.close())[1])(socket.socket())]; assert not connected, \"restricted guest unexpectedly reached an upstream/proxy endpoint\"' $probe_argv"
printf 'KVM_EGRESS_BOUNDARY_PASS: a bounded sample of resolved provider HTTPS addresses and the configured proxy TCP endpoint were unreachable in restricted QEMU mode\n'
ssh_guest 'test "$(cat /home/mester/workspace/persisted.txt)" = persistent-fixture && test "$(cat /home/mester/.local/share/opencode/auth.json)" = "{\"provider\":\"synthetic-fixture\",\"marker\":\"preserve-auth-bytes\"}" && test "$(stat -c %a /home/mester/.local/share/opencode/auth.json)" = 600 && python3 -c '\''import sqlite3; db=sqlite3.connect("/home/mester/.local/share/opencode/opencode.db"); assert db.execute("select value from session_fixture").fetchone() == ("synthetic-session-history",); db.close()'\'''
capture_tui_probe reboot 8 "$tmp" "$tmp_root"
ssh_guest 'TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux has-session -t little-mester-opencode && test "$(TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux display-message -p -t little-mester-opencode:0.0 "#{pane_dead}")" = 0 && test "$(TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux display-message -p -t little-mester-opencode:0.0 "#{pane_current_path}")" = /home/mester/workspace'
printf 'KVM_PERSISTENCE_PASS: synthetic OpenCode DB/auth fixtures, workspace data, and TUI recreation survived disposable guest reboot\n'
stage_end restricted-reboot-egress-persistence
stage_start synthetic-oauth 120
qemu_identity_matches || fail 'test-owned QEMU identity changed before synthetic OAuth stage'
verify_overlay_identity || fail 'disposable guest disk identity changed before synthetic OAuth stage'
oauth_remaining=$((qemu_deadline - SECONDS))
(( oauth_remaining > 0 )) || fail 'QEMU integration exceeded its overall bounded deadline before OAuth simulation'
(( oauth_remaining > 120 )) && oauth_remaining=120
ci_launch_tracked oauth_pid env TMPDIR=/tmp python3 tests/ci-qemu-oauth-bridge.py \
  --port "$port" --identity "$key" --known-hosts "$known_hosts" \
  --evidence-root "$oauth_evidence_root" --max-runtime-seconds "$oauth_remaining"
oauth_starttime="$(process_starttime "$oauth_pid" 2>/dev/null || true)"
oauth_uid="$(process_uid "$oauth_pid")"
oauth_parent="$BASHPID"
oauth_status=0
wait "$oauth_pid" || oauth_status=$?
oauth_pid=''
oauth_starttime=''
oauth_uid=''
oauth_parent=''
[[ "$oauth_status" == 0 ]] || fail "synthetic OAuth harness failed or was cancelled (status $oauth_status)"
printf 'KVM_M2_AUTH_BOUNDARY: live browser approval, provider token exchange and real model access are NOT_RUN\n'
stage_end synthetic-oauth
