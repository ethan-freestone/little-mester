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

readonly image_url='https://cloud-images.ubuntu.com/releases/noble/release-20261017/ubuntu-24.04-server-cloudimg-amd64.img'
readonly image_sha256='04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94'

fail() {
  printf 'KVM_TRIAL_FAIL: %s\n' "$1" >&2
  if [[ -n "${serial_log:-}" && -f "$serial_log" ]]; then
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
    && grep -azFq -- "$overlay" "/proc/$qemu_pid/cmdline"
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

stop_owned_qemu() {
  local state=''
  [[ -n "$qemu_pid" ]] || return 0
  if [[ -z "$qemu_starttime" || -z "$qemu_uid" ]]; then
    # Recover the identity only if this is still the directly launched QEMU
    # child of this shell. Otherwise retain all VM evidence and fail closed.
    capture_qemu_identity || true
  fi
  if ! qemu_identity_matches; then
    state="$(process_state "$qemu_pid")"
    if [[ -z "$state" || "$state" == Z ]]; then
      wait "$qemu_pid" 2>/dev/null || true
      qemu_pid=''
      qemu_starttime=''
      qemu_uid=''
      qemu_parent=''
      return 0
    fi
    return 1
  fi
  signal_owned_process "$qemu_pid" "$qemu_starttime" "$qemu_uid" \
    "$qemu_parent" qemu-system-x86 TERM \
    little-mester-hosted-kvm-integration "$overlay" \
    || { state="$(process_state "$qemu_pid")"; [[ -z "$state" || "$state" == Z ]] || return 1; }
  for _ in {1..150}; do
    if ! qemu_identity_matches; then
      state="$(process_state "$qemu_pid")"
      [[ -z "$state" || "$state" == Z ]] && break
      return 1
    fi
    state="$(process_state "$qemu_pid")"
    [[ "$state" == Z ]] && break
    sleep 0.1
  done
  if [[ -n "$state" && "$state" != Z ]] && qemu_identity_matches; then
    signal_owned_process "$qemu_pid" "$qemu_starttime" "$qemu_uid" \
      "$qemu_parent" qemu-system-x86 KILL \
      little-mester-hosted-kvm-integration "$overlay" \
      || { state="$(process_state "$qemu_pid")"; [[ -z "$state" || "$state" == Z ]] || return 1; }
    for _ in {1..20}; do
      state="$(process_state "$qemu_pid")"
      [[ -z "$state" || "$state" == Z ]] && break
      sleep 0.1
    done
  fi
  state="$(process_state "$qemu_pid")"
  if [[ -n "$state" && "$state" != Z ]]; then
    return 1
  fi
  wait "$qemu_pid" 2>/dev/null || true
  qemu_pid=''
  qemu_starttime=''
  qemu_uid=''
  qemu_parent=''
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
oauth_pid=''
oauth_starttime=''
oauth_uid=''
oauth_parent=''
oauth_cleanup_unproved=0
qemu_deadline=0
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
  if [[ -n "$qemu_pid" ]] && ! stop_owned_qemu; then
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
host_key="$tmp/guest_host_ed25519"
known_hosts="$tmp/known_hosts"
serial_log="$tmp/serial.log"

curl --fail --location --silent --show-error --connect-timeout 20 --max-time 600 \
  "$image_url" --output "$image"
printf '%s  %s\n' "$image_sha256" "$image" | sha256sum --check --status \
  || fail 'pinned guest image SHA-256 mismatch'

ssh-keygen -q -t ed25519 -N '' -f "$key"
ssh-keygen -q -t ed25519 -N '' -f "$host_key"
printf '[127.0.0.1]:%s %s\n' "$port" "$(awk '{print $1 " " $2}' "$host_key.pub")" >"$known_hosts"
LM_CI_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" \
LM_CI_PUBLIC_KEY="$(<"$key.pub")" \
LM_CI_HOST_PRIVATE_KEY="$(<"$host_key")" \
LM_CI_HOST_PUBLIC_KEY="$(<"$host_key.pub")" \
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
for path, content in contract.PROTECTED_PLUGIN_ASSETS.items():
    files.append({"path": path,
                  "owner": "root:root", "permissions": "0644", "encoding": "b64",
                  "content": base64.b64encode(content).decode("ascii")})
install_command = (
    "export DEBIAN_FRONTEND=noninteractive; "
    "apt-get update -qq && apt-get install -y tmux nodejs npm ca-certificates && "
    "npm install --global @opencode/cli@2.2.5 && "
    "install -d -o mester -g mester -m 0700 /home/mester/workspace "
    "/home/mester/.local/share/opencode && printf ready > /var/tmp/lm-kvm-trial-ready"
)
config = {
    "ssh_pwauth": False,
    "disable_root": True,
    "ssh_deletekeys": False,
    "ssh_keys": {"ed25519_private": os.environ["LM_CI_HOST_PRIVATE_KEY"],
                 "ed25519_public": os.environ["LM_CI_HOST_PUBLIC_KEY"]},
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
qemu-img create -q -f qcow2 -F qcow2 -b "$image" "$overlay"

printf 'KVM_TRIAL: starting pinned guest with accel=kvm (no TCG fallback)\n'
ci_launch_tracked qemu_pid qemu-system-x86_64 \
    -name little-mester-hosted-kvm-integration \
    -machine q35,accel=kvm -cpu host -smp 2 -m 4096 \
    -nodefaults -no-user-config -display none -monitor none \
    -serial "file:$serial_log" \
    -drive "file=$overlay,if=virtio,format=qcow2" \
    -drive "file=$seed,if=virtio,format=raw,readonly=on" \
      -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:$port-:22" \
    -device virtio-net-pci,netdev=net0
qemu_starttime=''
qemu_uid=''
capture_qemu_identity || fail 'could not prove ownership of the launched QEMU process'
qemu_deadline=$((SECONDS + 2400))

ssh_args=(
  -F /dev/null -T -a -x
  -i "$key" -p "$port"
  -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityAgent=none
  -o UserKnownHostsFile="$known_hosts" -o GlobalKnownHostsFile=/dev/null
  -o StrictHostKeyChecking=yes -o ConnectTimeout=3
  -o ProxyCommand=none -o ProxyJump=none
    "mester@127.0.0.1"
)

ready=0
readiness_deadline=$((SECONDS + 1500))
while (( SECONDS < readiness_deadline )); do
  qemu_identity_matches || fail 'test-owned QEMU process exited or changed identity before guest readiness'
  qemu_state="$(process_state "$qemu_pid")"
  [[ -n "$qemu_state" && "$qemu_state" != Z ]] || fail 'QEMU exited before guest readiness'
  remaining=$((readiness_deadline - SECONDS))
  (( remaining > 0 )) || break
  attempt_timeout=8
  (( remaining < attempt_timeout )) && attempt_timeout=$remaining
  # The single-quoted payload is intentionally interpreted only by the guest shell.
  # shellcheck disable=SC2016
  if [[ -s "$known_hosts" ]]; then
    if timeout --signal=TERM --kill-after=1 "$attempt_timeout" ssh "${ssh_args[@]}" \
        'state=$(cloud-init status --long 2>&1 || true); printf "%s\n" "$state"; if printf "%s\n" "$state" | grep -Eq "^(status|extended_status): error"; then exit 42; fi; test -f /var/lib/cloud/instance/boot-finished && test "$(cat /var/tmp/lm-kvm-trial-ready)" = ready' \
        >"$tmp/readiness.log" 2>&1; then
      ready=1
      break
    elif grep -Eq '^(status|extended_status): error' "$tmp/readiness.log"; then
      cat "$tmp/readiness.log" >&2
      fail 'cloud-init reported an error before guest readiness'
    fi
  fi
  remaining=$((readiness_deadline - SECONDS))
  (( remaining > 0 )) && sleep 1
done
[[ "$ready" == 1 ]] || fail 'pinned QEMU guest did not reach bounded cloud-init/SSH readiness'
printf 'KVM_TRIAL_PASS: pinned guest booted with accel=kvm and passed cloud-init readiness\n'

ssh_guest() {
  local remaining=$((qemu_deadline - SECONDS)) limit=20
  (( remaining > 0 )) || fail 'QEMU integration exceeded its overall bounded deadline'
  (( remaining < limit )) && limit=$remaining
  timeout --signal=TERM --kill-after=1 "$limit" ssh "${ssh_args[@]}" "$1"
}
guest_asset_checks="$(python3 - <<'PY'
import importlib.util, pathlib
path = pathlib.Path("lib/vm/parent-session-contract.py")
spec = importlib.util.spec_from_file_location("contract", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(module.readiness_checks())
PY
)"
ssh_guest "cloud-init status --long >/dev/null 2>&1 || exit 41; $guest_asset_checks"
ssh_tty_guest() {
  local remaining=$((qemu_deadline - SECONDS)) limit=15
  (( remaining > 0 )) || fail 'QEMU integration exceeded its overall bounded deadline'
  (( remaining < limit )) && limit=$remaining
  timeout --signal=TERM --kill-after=1 "$limit" ssh \
    -F /dev/null -tt -a -x -i "$key" -p "$port" \
    -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityAgent=none \
    -o UserKnownHostsFile="$known_hosts" -o GlobalKnownHostsFile=/dev/null \
    -o StrictHostKeyChecking=yes -o ConnectTimeout=3 \
    -o ProxyCommand=none -o ProxyJump=none "mester@127.0.0.1" "$1"
}

ssh_guest 'test "$(/usr/local/bin/opencode --version)" = "opencode v2.2.5" && tmux -V >/dev/null'
ssh_guest 'test "$(stat -c %u:%a /etc/opencode/opencode.json)" = "0:644" && grep -Fq "\"share\":\"disabled\"" /etc/opencode/opencode.json'
# Guest-shell payloads are intentionally single-quoted so the local shell does
# not expand substitutions before they execute inside the disposable guest.
# shellcheck disable=SC2016
ssh_guest 'mkdir -p /home/mester/.local/share/opencode /home/mester/workspace/.opencode && chmod 0775 /home/mester/.local /home/mester/.local/share /home/mester/.local/share/opencode && python3 -c '\''import sqlite3; db=sqlite3.connect("/home/mester/.local/share/opencode/opencode.db"); db.execute("create table session_fixture(value text)"); db.execute("insert into session_fixture values (?)", ("synthetic-session-history",)); db.commit(); db.close()'\'' && chmod 0664 /home/mester/.local/share/opencode/opencode.db && printf "{\"provider\":\"synthetic-fixture\",\"marker\":\"preserve-auth-bytes\"}" > /home/mester/.local/share/opencode/auth.json && chmod 0664 /home/mester/.local/share/opencode/auth.json && db_before=$(sha256sum /home/mester/.local/share/opencode/opencode.db) && auth_before=$(sha256sum /home/mester/.local/share/opencode/auth.json) && printf persistent-fixture > /home/mester/workspace/persisted.txt && chmod 0600 /home/mester/workspace/persisted.txt && printf "{\"model\":\"hostile-direct-marker\",\"plugins\":[\"/tmp/lm-ci-hostile-plugin-sentinel\"]}" > /home/mester/workspace/opencode.json && printf "{\"model\":\"hostile-nested-marker\",\"plugins\":[\"/tmp/lm-ci-hostile-plugin-sentinel\"]}" > /home/mester/workspace/.opencode/opencode.json && /usr/bin/python3 -I /usr/local/libexec/lm-guest-user-dirs.py prepare-auth && test "$(stat -c %a /home/mester/.local/share/opencode/opencode.db)" = 600 && test "$(stat -c %a /home/mester/.local/share/opencode/auth.json)" = 600 && test "$(sha256sum /home/mester/.local/share/opencode/opencode.db)" = "$db_before" && test "$(sha256sum /home/mester/.local/share/opencode/auth.json)" = "$auth_before"'
ssh_guest 'printf unsafe-target-preserved > /tmp/lm-auth-unsafe-target && ln -s /tmp/lm-auth-unsafe-target /home/mester/.local/share/opencode/opencode.db-wal; if /usr/bin/python3 -I /usr/local/libexec/lm-guest-user-dirs.py prepare-auth >/dev/null 2>&1; then exit 1; fi; test "$(cat /tmp/lm-auth-unsafe-target)" = unsafe-target-preserved; rm /home/mester/.local/share/opencode/opencode.db-wal'
ssh_guest 'cd /home/mester/workspace && /usr/bin/python3 - <<'\''PY'\''
import json, os, pathlib, subprocess

workspace = pathlib.Path("/home/mester/workspace")
protected = {
    "HOME": "/home/mester", "PATH": "/usr/local/bin:/usr/bin:/bin",
    "TERM": "xterm-256color", "LANG": "C.UTF-8",
    "XDG_DATA_HOME": "/home/mester/.local/share",
    "XDG_CONFIG_HOME": "/home/mester/.config",
    "OPENCODE_CONFIG": "/etc/opencode/opencode.json",
    "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
    "OPENCODE_CLI_CONFIG_CONTENT": json.dumps({
        "plugins": ["/etc/opencode/plugins/little-mester-exit-confirm"],
        "keybinds": {"app.exit": "none", "prompt.clear": "none"},
    }, separators=(",", ":")),
}
command = ["/usr/local/bin/opencode", "debug", "config", "--standalone"]
def config_sources(environment):
    result = subprocess.run(command, cwd=workspace, env=environment, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=20, check=True)
    return json.loads(result.stdout)

hostile = {str(workspace / "opencode.json"),
           str(workspace / ".opencode/opencode.json")}
enabled = {key: value for key, value in protected.items()
           if key != "OPENCODE_DISABLE_PROJECT_CONFIG"}
enabled_sources = config_sources(enabled)
assert hostile <= {item["path"] for item in enabled_sources
                   if item.get("type") == "document"}, "positive control did not discover project configs"
assert "/tmp/lm-ci-hostile-plugin-sentinel" in json.dumps(enabled_sources), \
    "positive control did not load hostile plugin configuration"
protected_sources = config_sources(protected)
assert hostile.isdisjoint({item["path"] for item in protected_sources
                           if item.get("type") == "document"}), \
    "protected configuration discovered hostile project files"
assert "/tmp/lm-ci-hostile-plugin-sentinel" not in json.dumps(protected_sources), \
    "protected configuration retained hostile plugin configuration"
PY'
ssh_tty_guest 'cd /home/mester/workspace && timeout --signal=TERM --kill-after=2 8 /usr/local/bin/lm-opencode >/dev/null 2>&1 || test "$?" = 124'
# shellcheck disable=SC2016
ssh_guest 'test "$(stat -c %a /home/mester/.local/share/opencode/opencode.db)" = 600 && test "$(stat -c %a /home/mester/.local/share/opencode/auth.json)" = 600 && test "$(stat -c %a /home/mester/.local/share/opencode)" = 700 && TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux has-session -t little-mester-opencode && test "$(TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux display-message -p -t little-mester-opencode:0.0 "#{pane_dead}")" = 0 && TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux show-environment -g OPENCODE_CONFIG | grep -Fx OPENCODE_CONFIG=/etc/opencode/opencode.json && TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux show-environment -g OPENCODE_DISABLE_PROJECT_CONFIG | grep -Fx OPENCODE_DISABLE_PROJECT_CONFIG=1'
pane_before="$(ssh_guest 'TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux display-message -p -t little-mester-opencode:0.0 "#{pane_id}:#{pane_pid}"')"
ssh_tty_guest 'cd /home/mester/workspace && timeout --signal=TERM --kill-after=2 4 /usr/local/bin/lm-opencode >/dev/null 2>&1 || test "$?" = 124'
pane_after="$(ssh_guest 'TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux display-message -p -t little-mester-opencode:0.0 "#{pane_id}:#{pane_pid}"')"
[[ "$pane_before" == "$pane_after" ]] || fail 'tmux reattachment did not preserve the live OpenCode pane identity'
printf 'KVM_M2_INTEGRATION_PASS: real KVM guest, SSH, pinned OpenCode 2.2.5, protected config, tmux TUI process and persistent workspace fixture\n'

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
stop_owned_qemu || fail 'test-owned QEMU process tree did not stop; preserving its artifacts'
ci_launch_tracked qemu_pid qemu-system-x86_64 \
    -name little-mester-hosted-kvm-integration \
    -machine q35,accel=kvm -cpu host -smp 2 -m 4096 \
    -nodefaults -no-user-config -display none -monitor none \
    -serial "file:$serial_log" \
    -drive "file=$overlay,if=virtio,format=qcow2" \
    -drive "file=$seed,if=virtio,format=raw,readonly=on" \
    -netdev "user,id=net0,restrict=on,hostfwd=tcp:127.0.0.1:$port-:22" \
    -device virtio-net-pci,netdev=net0
qemu_starttime=''
qemu_uid=''
capture_qemu_identity || fail 'could not prove ownership of the restricted-boot QEMU process'
ready=0
readiness_deadline=$((SECONDS + 300))
while (( SECONDS < readiness_deadline && SECONDS < qemu_deadline )); do
  qemu_identity_matches || fail 'test-owned QEMU process exited or changed identity during persistent-disk reboot'
  qemu_state="$(process_state "$qemu_pid")"
  [[ -n "$qemu_state" && "$qemu_state" != Z ]] || fail 'QEMU exited during persistent-disk reboot'
  remaining=$((readiness_deadline - SECONDS))
  overall_remaining=$((qemu_deadline - SECONDS))
  (( overall_remaining < remaining )) && remaining=$overall_remaining
  (( remaining > 0 )) || break
  attempt_timeout=8
  (( remaining < attempt_timeout )) && attempt_timeout=$remaining
# shellcheck disable=SC2016
  if timeout --signal=TERM --kill-after=1 "$attempt_timeout" ssh "${ssh_args[@]}" \
      'test "$(cat /home/mester/workspace/persisted.txt)" = persistent-fixture && test "$(/usr/local/bin/opencode --version)" = "opencode v2.2.5"' \
      >/dev/null 2>&1; then
    ready=1
    break
  fi
  remaining=$((readiness_deadline - SECONDS))
  (( remaining > 0 )) && sleep 1
done
[[ "$ready" == 1 ]] || fail 'persistent guest disk did not retain OpenCode/workspace state across reboot'
ssh_guest "python3 -c 'import socket,sys; targets=[(ip,443) for ip in sys.argv[1:]]+[(\"10.0.2.100\",3128)]; connected=[target for target in targets if (lambda s: (s.settimeout(2),s.connect_ex(target)==0,s.close())[1])(socket.socket())]; assert not connected, \"restricted guest unexpectedly reached an upstream/proxy endpoint\"' $probe_argv"
printf 'KVM_EGRESS_BOUNDARY_PASS: a bounded sample of resolved provider HTTPS addresses and the configured proxy TCP endpoint were unreachable in restricted QEMU mode\n'
ssh_guest 'test "$(cat /home/mester/workspace/persisted.txt)" = persistent-fixture && test "$(cat /home/mester/.local/share/opencode/auth.json)" = "{\"provider\":\"synthetic-fixture\",\"marker\":\"preserve-auth-bytes\"}" && test "$(stat -c %a /home/mester/.local/share/opencode/auth.json)" = 600 && python3 -c '\''import sqlite3; db=sqlite3.connect("/home/mester/.local/share/opencode/opencode.db"); assert db.execute("select value from session_fixture").fetchone() == ("synthetic-session-history",); db.close()'\'''
ssh_tty_guest 'cd /home/mester/workspace && timeout --signal=TERM --kill-after=2 8 /usr/local/bin/lm-opencode >/dev/null 2>&1 || test "$?" = 124'
ssh_guest 'TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux has-session -t little-mester-opencode && test "$(TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux display-message -p -t little-mester-opencode:0.0 "#{pane_dead}")" = 0 && test "$(TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux display-message -p -t little-mester-opencode:0.0 "#{pane_current_path}")" = /home/mester/workspace'
printf 'KVM_PERSISTENCE_PASS: synthetic OpenCode DB/auth fixtures, workspace data, and TUI recreation survived disposable guest reboot\n'
oauth_remaining=$((qemu_deadline - SECONDS))
(( oauth_remaining > 0 )) || fail 'QEMU integration exceeded its overall bounded deadline before OAuth simulation'
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
