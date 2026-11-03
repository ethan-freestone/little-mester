#!/usr/bin/env bash
# Isolated, bounded SSH transport for one verified loopback VM listener.
# Sourcing this file performs no network or SSH operation.

VM_SSH_SOURCE_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
VM_SSH_PROCESS_HELPER="${VM_SSH_PROCESS_HELPER:-$(dirname "$VM_SSH_SOURCE_FILE")/process.py}"
VM_SSH_PYTHON="${VM_SSH_PYTHON:-python3}"
VM_SSH_BIN="${VM_SSH_BIN:-ssh}"
VM_SSH_KEYSCAN_BIN="${VM_SSH_KEYSCAN_BIN:-ssh-keyscan}"
VM_SSH_TIMEOUT_BIN="${VM_SSH_TIMEOUT_BIN:-timeout}"

vm_ssh_error() {
  printf 'REFUSED: %s\n' "$*" >&2
  return 2
}

vm_ssh_port_valid() {
  [[ "${1:-}" =~ ^[1-9][0-9]{0,4}$ ]] && (( 10#$1 <= 65535 ))
}

vm_ssh_build_argv() {
  local key="${1:-}" known_hosts="${2:-}" port="${3:-}" mode="${4:-noninteractive}"
  [[ "$key" == /* && "$known_hosts" == /* && "$key" != *','* &&
     "$known_hosts" != *','* && "$key" != *$'\n'* && "$known_hosts" != *$'\n'* ]] || return 2
  vm_ssh_port_valid "$port" || return 2
  [[ "$mode" == noninteractive || "$mode" == interactive ]] || return 2
  VM_SSH_ARGV=(
    "$VM_SSH_BIN"
    -F /dev/null
    -o BatchMode=yes
    -o "ConnectTimeout=10"
    -o "ConnectionAttempts=1"
    -o "StrictHostKeyChecking=yes"
    -o "UserKnownHostsFile=$known_hosts"
    -o GlobalKnownHostsFile=/dev/null
    -o HostKeyAlias=127.0.0.1
    -o IdentitiesOnly=yes
    -o IdentityAgent=none
    -o "IdentityFile=$key"
    -o ForwardAgent=no
    -o ForwardX11=no
    -o ClearAllForwardings=yes
    -o PreferredAuthentications=publickey
    -o PasswordAuthentication=no
    -o KbdInteractiveAuthentication=no
    -o UpdateHostKeys=no
    -p "$port"
  )
  if [[ "$mode" == interactive ]]; then
    VM_SSH_ARGV+=(-tt)
  else
    VM_SSH_ARGV+=(-T)
  fi
  return 0
}

vm_ssh_listener_verified() {
  local registry="$1" run_id="$2" role="$3" port="$4"
  "$VM_SSH_PYTHON" "$VM_SSH_PROCESS_HELPER" listener-owned --registry "$registry" --run-id "$run_id" \
    --role "$role" --port "$port" >/dev/null 2>&1
}

vm_ssh_trust_host_key() {
  local known_hosts="$1" port="$2" registry="$3" run_id="$4" role="$5"
  vm_ssh_port_valid "$port" || return 2
  vm_ssh_listener_verified "$registry" "$run_id" "$role" "$port" || {
    vm_ssh_error 'SSH host-key scan requires a verified VM-owned loopback listener.'
    return 2
  }
  local scan_output scan_status
  if scan_output="$("$VM_SSH_TIMEOUT_BIN" --foreground --signal=TERM --kill-after=1s \
    4s "$VM_SSH_KEYSCAN_BIN" -T 3 -t ed25519 -p "$port" 127.0.0.1 2>/dev/null)"; then
    scan_status=0
  else
    scan_status=$?
  fi
  (( scan_status == 0 )) || return 1
  vm_ssh_listener_verified "$registry" "$run_id" "$role" "$port" || {
    vm_ssh_error 'SSH listener identity changed during host-key scan.'
    return 2
  }
  printf '%s\n' "$scan_output" | "$VM_SSH_PYTHON" "$VM_SSH_PROCESS_HELPER" trust-key \
    --known-hosts "$known_hosts" --host 127.0.0.1 --port "$port" >/dev/null 2>&1
}

vm_ssh_exec() {
  local key="$1" known_hosts="$2" port="$3" deadline="$4" mode="$5" host="$6" remote_command="$7"
  [[ "$deadline" =~ ^[1-9][0-9]{0,3}$ ]] && (( 10#$deadline <= 1800 )) || return 2
  [[ "$host" == mester@127.0.0.1 && -n "$remote_command" &&
     ${#remote_command} -le 65536 ]] || return 2
  local path owner mode_bits links
  for path in "$key" "$known_hosts"; do
    [[ -f "$path" && ! -L "$path" ]] || return 2
    read -r owner mode_bits links < <(stat -c '%u %a %h' -- "$path") || return 2
    [[ "$owner" == "$(id -u)" && "$mode_bits" == 600 && "$links" == 1 ]] || return 2
  done
  vm_ssh_build_argv "$key" "$known_hosts" "$port" "$mode" || return $?
  "$VM_SSH_TIMEOUT_BIN" --foreground --signal=TERM --kill-after=2s \
    "${deadline}s" "${VM_SSH_ARGV[@]}" "$host" "$remote_command"
}

vm_ssh_noninteractive() {
  local key="$1" known_hosts="$2" port="$3" deadline="$4" host="$5"
  shift 5
  [[ "$#" -eq 1 ]] || return 2
  vm_ssh_exec "$key" "$known_hosts" "$port" "$deadline" noninteractive "$host" "$1"
}

vm_ssh_interactive() {
  local key="$1" known_hosts="$2" port="$3" deadline="$4" host="$5"
  shift 5
  [[ "$#" -eq 1 ]] || return 2
  vm_ssh_exec "$key" "$known_hosts" "$port" "$deadline" interactive "$host" "$1"
}
