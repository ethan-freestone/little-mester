#!/usr/bin/env bash
# Pure QEMU/virtiofs argv construction and owned, bounded process startup.
# Sourcing this file performs no host checks, allocation, process or network work.

VM_RUNNER_SOURCE_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
VM_RUNNER_ROOT="$(cd "$(dirname "$VM_RUNNER_SOURCE_FILE")/../.." && pwd)"
VM_RUNNER_PROCESS_HELPER="${VM_RUNNER_PROCESS_HELPER:-$VM_RUNNER_ROOT/lib/vm/process.py}"
VM_RUNNER_PROXY="${VM_RUNNER_PROXY:-$VM_RUNNER_ROOT/lib/vm/proxy.py}"
VM_RUNNER_PYTHON="${VM_RUNNER_PYTHON:-python3}"
VM_RUNNER_QEMU_BIN="${VM_RUNNER_QEMU_BIN:-qemu-system-x86_64}"
VM_RUNNER_VIRTIOFSD_BIN="${VM_RUNNER_VIRTIOFSD_BIN:-virtiofsd}"
VM_RUNNER_SOCAT_BIN="${VM_RUNNER_SOCAT_BIN:-socat}"
VM_RUNNER_LOG_SEGMENT_BYTES=10485760

vm_runner_error() {
  printf 'REFUSED: %s\n' "$*" >&2
  return 2
}

vm_runner_port_valid() {
  [[ "${1:-}" =~ ^[1-9][0-9]{0,4}$ ]] && (( 10#$1 <= 65535 ))
}

vm_runner_memory_valid() {
  [[ "${1:-}" =~ ^[1-9][0-9]{0,2}$ ]] && (( 10#$1 <= 128 ))
}

vm_runner_sensitive_value_valid() {
  local value="${1:-}"
  [[ -n "$value" && "$value" == /* && "$value" != *','* &&
     "$value" != *$'\n'* && "$value" != *$'\r'* &&
     "$value" != *'/../'* && "$value" != */.. ]]
}

vm_runner_validate_socket_path() {
  local path="${1:-}"
  vm_runner_sensitive_value_valid "$path" || return 2
  (( ${#path} < 108 )) || return 2
  return 0
}

vm_runner_build_virtiofsd_argv() {
  local workspace="${1:-}" socket="${2:-}"
  vm_runner_sensitive_value_valid "$workspace" || return 2
  vm_runner_validate_socket_path "$socket" || return 2
  VM_VIRTIOFSD_ARGV=(
    "$VM_RUNNER_VIRTIOFSD_BIN"
    --shared-dir "$workspace"
    --socket-path "$socket"
    --rlimit-nofile=1048576
    --announce-submounts
  )
}

vm_runner_build_qemu_argv() {
  local name="${1:-}" memory="${2:-}" seed="${3:-}" overlay="${4:-}"
  local socket="${5:-}" ssh_port="${6:-}" proxy_port="${7:-}"
  local model_kind="${8:-none}" model_port="${9:-}" metrics_port="${10:-}"
  local netdev model_guest_port='' path
  [[ "$VM_RUNNER_SOCAT_BIN" =~ ^[A-Za-z0-9_./+-]+$ ]] || return 2
  [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || return 2
  vm_runner_memory_valid "$memory" || return 2
  for path in "$seed" "$overlay"; do
    vm_runner_sensitive_value_valid "$path" || return 2
  done
  vm_runner_validate_socket_path "$socket" || return 2
  vm_runner_port_valid "$ssh_port" && vm_runner_port_valid "$proxy_port" || return 2
  case "$model_kind" in
    none) [[ -z "$model_port" ]] || return 2 ;;
    ollama) vm_runner_port_valid "$model_port" || return 2; model_guest_port=11434 ;;
    llamacpp) vm_runner_port_valid "$model_port" || return 2; model_guest_port=8080 ;;
    *) return 2 ;;
  esac
  [[ -z "$metrics_port" ]] || vm_runner_port_valid "$metrics_port" || return 2

  netdev="user,id=net0,hostfwd=tcp:127.0.0.1:${ssh_port}-:22,guestfwd=tcp:10.0.2.100:3128-cmd:${VM_RUNNER_SOCAT_BIN} - TCP:127.0.0.1:${proxy_port}"
  if [[ -n "$model_guest_port" ]]; then
    netdev+=",guestfwd=tcp:10.0.2.100:${model_guest_port}-cmd:${VM_RUNNER_SOCAT_BIN} - TCP:127.0.0.1:${model_port}"
  fi
  if [[ -n "$metrics_port" ]]; then
    netdev+=",guestfwd=tcp:10.0.2.100:19532-cmd:${VM_RUNNER_SOCAT_BIN} - TCP:127.0.0.1:${metrics_port}"
  fi
  netdev+=",restrict=on"

  VM_QEMU_ARGV=(
    "$VM_RUNNER_QEMU_BIN"
    -enable-kvm
    -name "$name"
    -machine 'type=q35,accel=kvm'
    -cpu host
    -smp 2
    -m "$((10#$memory * 1024))"
    -object "memory-backend-memfd,id=mem,size=${memory}G,share=on"
    -numa 'node,memdev=mem'
    -drive "file=${seed},format=raw,if=none,id=cloud_disk,readonly=on"
    -device 'virtio-scsi-pci,id=scsi0'
    -device 'scsi-cd,drive=cloud_disk'
    -drive "file=${overlay},format=qcow2,if=virtio"
    -chardev "socket,id=charfs,path=${socket}"
    -device 'vhost-user-fs-pci,chardev=charfs,tag=workspace'
    -display none
    -serial stdio
    -monitor none
    -netdev "$netdev"
    -device 'virtio-net-pci,netdev=net0'
    -no-reboot
  )
  return 0
}

vm_runner_require_kvm() {
  local device="${1:-/dev/kvm}"
  [[ "$(uname -s 2>/dev/null)" == Linux && "$(uname -m 2>/dev/null)" == x86_64 ]] || {
    vm_runner_error 'VM execution requires Linux x86_64 with KVM; TCG fallback is disabled.'
    return 2
  }
  [[ -c "$device" && -r "$device" && -w "$device" ]] || {
    vm_runner_error 'KVM is unavailable or inaccessible; no VM processes were started.'
    return 2
  }
}

vm_runner_preflight() {
  vm_runner_require_kvm /dev/kvm || return $?
  local tool
  for tool in "$VM_RUNNER_PYTHON" "$VM_RUNNER_QEMU_BIN" "$VM_RUNNER_VIRTIOFSD_BIN" \
    "$VM_RUNNER_SOCAT_BIN" ssh ssh-keyscan timeout; do
    command -v "$tool" >/dev/null 2>&1 || {
      vm_runner_error "Required VM process tool is unavailable: $tool"
      return 2
    }
  done
  [[ -f "$VM_RUNNER_PROCESS_HELPER" && ! -L "$VM_RUNNER_PROCESS_HELPER" &&
     -f "$VM_RUNNER_PROXY" && ! -L "$VM_RUNNER_PROXY" ]] || {
    vm_runner_error 'VM process or proxy helper is unavailable.'
    return 2
  }
  return 0
}

vm_runner_private_directory() {
  local path="${1:-}" owner mode
  [[ -d "$path" && ! -L "$path" ]] || return 2
  owner="$(stat -c '%u' -- "$path")" || return 2
  mode="$(stat -c '%a' -- "$path")" || return 2
  [[ "$owner" == "$(id -u)" && "$mode" == 700 ]]
}

vm_runner_runtime_socket() {
  local profile_dir="${1:-}" run_id="${2:-}" uid root canonical digest socket
  [[ "$run_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || return 2
  canonical="$(realpath -e -- "$profile_dir")" || return 2
  uid="$(id -u)"
  root="${VM_RUNNER_SOCKET_ROOT:-/tmp/little-mester-vfs-${uid}}"
  [[ "$root" == /* && "$root" != *','* && "$root" != *$'\n'* &&
     "$root" != *'/../'* && "$root" != */.. && ! -L "$root" ]] || return 2
  if [[ ! -e "$root" ]]; then
    (umask 077; mkdir -- "$root") || return 2
  fi
  root="$(realpath -e -- "$root")" || return 2
  vm_runner_private_directory "$root" || return 2
  digest="$("$VM_RUNNER_PYTHON" -c 'import hashlib,sys; print(hashlib.sha256((sys.argv[1]+"/"+sys.argv[2]).encode()).hexdigest())' "$canonical" "$run_id")" || return 2
  [[ "$digest" =~ ^[a-f0-9]{64}$ ]] || return 2
  socket="$root/$digest.sock"
  vm_runner_validate_socket_path "$socket" || return 2
  [[ ! -e "$socket" && ! -L "$socket" ]] || return 2
  printf '%s\n' "$socket"
}

vm_runner_allocate_port() {
  "$VM_RUNNER_PYTHON" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
}

vm_runner_spawn_logged() {
  local registry="$1" run_id="$2" role="$3" log_file="$4"
  shift 4
  "$VM_RUNNER_PYTHON" "$VM_RUNNER_PROCESS_HELPER" run-logged \
    --registry "$registry" --run-id "$run_id" --role "$role" \
    --log "$log_file" --max-bytes "$VM_RUNNER_LOG_SEGMENT_BYTES" -- "$@" \
    </dev/null >/dev/null 2>&1 &
  vm_runner_wait_role "$registry" "$run_id" "${role}_logger" 5 >/dev/null || return $?
  vm_runner_wait_role "$registry" "$run_id" "$role" 5
}

vm_runner_wait_role() {
  local registry="$1" run_id="$2" role="$3" deadline="$4" state
  state="$("$VM_RUNNER_PYTHON" "$VM_RUNNER_PROCESS_HELPER" wait-role \
    --registry "$registry" --run-id "$run_id" --role "$role" --timeout "$deadline" 2>/dev/null)" || {
    printf '%s\n' "${state:-unknown}"
    return 2
  }
  [[ "$state" == running ]] || { printf '%s\n' "$state"; return 1; }
  printf '%s\n' "$state"
}

vm_runner_rollback() {
  local registry="$1" run_id="$2" state
  state="$("$VM_RUNNER_PYTHON" "$VM_RUNNER_PROCESS_HELPER" stop \
    --registry "$registry" --run-id "$run_id" --wait 5 2>/dev/null)" || {
    vm_runner_error 'Startup rollback found an unknown or unresponsive process identity; state was preserved.'
    return 2
  }
  [[ "$state" == stopped ]] || {
    vm_runner_error 'Startup rollback did not verify every run-owned process stopped; state was preserved.'
    return 2
  }
  return 0
}

vm_runner_write_failure() {
  local run_dir="$1" run_id="$2" stage="$3" cause="${4:-}" diagnostic="${5:-}"
  local -a args=("$VM_RUNNER_PYTHON" "$VM_RUNNER_PROCESS_HELPER" failure
    --path "$run_dir/failure.json" --run-id "$run_id" --stage "$stage")
  [[ -z "$cause" ]] || args+=(--cause-exit-code "$cause")
  [[ -z "$diagnostic" ]] || args+=(--diagnostic "$diagnostic")
  "${args[@]}" >/dev/null
}

vm_runner_role_exit_code() {
  "$VM_RUNNER_PYTHON" - "$VM_RUNNER_PROCESS_HELPER" "$1" "$2" "$3" <<'PY'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("vm_process", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
status = module.registry_status(sys.argv[2], sys.argv[3], sys.argv[4])
item = status["processes"].get(sys.argv[4], {})
print("" if item.get("exit_code") is None else item["exit_code"])
PY
}

vm_runner_launch_qemu() {
  local registry="$1" run_id="$2" run_dir="$3" name="$4" memory="$5"
  local seed="$6" overlay="$7" socket="$8" proxy_port="$9"
  local model_kind="${10}" model_port="${11}" metrics_port="${12}"
  local attempt ssh_port role log_file state cause
  for attempt in 1 2 3; do
    ssh_port="$(vm_runner_allocate_port)" || return 1
    vm_runner_port_valid "$ssh_port" || return 1
    vm_runner_build_qemu_argv "$name" "$memory" "$seed" "$overlay" "$socket" \
      "$ssh_port" "$proxy_port" "$model_kind" "$model_port" "$metrics_port" || return 2
    role="qemu_${attempt}"
    log_file="$run_dir/${role}.log"
    vm_runner_spawn_logged "$registry" "$run_id" "$role" "$log_file" "${VM_QEMU_ARGV[@]}" \
      >/dev/null 2>&1 || true
    state="$("$VM_RUNNER_PYTHON" "$VM_RUNNER_PROCESS_HELPER" wait-role \
      --registry "$registry" --run-id "$run_id" --role "$role" --timeout 2 2>/dev/null || true)"
    if [[ "$state" == running ]]; then
      VM_RUNNER_QEMU_ROLE="$role"
      VM_RUNNER_SSH_PORT="$ssh_port"
      return 0
    fi
    if [[ "$state" == unknown || "$state" == absent ]]; then
      vm_runner_write_failure "$run_dir" "$run_id" qemu '' "$log_file" || true
      return 1
    fi
    cause="$(vm_runner_role_exit_code "$registry" "$run_id" "$role" 2>/dev/null || true)"
    if (( attempt < 3 )) && grep -Eiq \
      '(Could not set up host forwarding rule|Failed to bind host forwarding rule).*Address already in use|Address already in use.*(host forwarding|hostfwd)' \
      "$log_file" 2>/dev/null; then
      continue
    fi
    vm_runner_write_failure "$run_dir" "$run_id" qemu "$cause" "$log_file" || true
    return 1
  done
  return 1
}

vm_runner_start() {
  local profile_dir="$1" run_id="$2" name="$3" workspace="$4" seed="$5" overlay="$6"
  local memory="$7" model_kind="${8:-none}" model_port="${9:-}" metrics_port="${10:-}"
  local canonical_profile canonical_workspace run_root run_dir registry socket
  local port_file marker proxy_port virtiofs_state ssh_timeout deadline role state cause
  local -a upstream_args=()

  unset VM_RUNNER_PROFILE_DIR VM_RUNNER_RUN_DIR VM_RUNNER_PROCESS_REGISTRY VM_RUNNER_RUN_ID
  # Every capability check and tool lookup precedes state allocation or process launch.
  vm_runner_preflight || return $?
  if ! declare -F vm_ssh_trust_host_key >/dev/null; then
    # shellcheck disable=SC1091 # Fixed sibling SSH module, resolved from this source file.
    source "$(dirname "$VM_RUNNER_SOURCE_FILE")/ssh.sh"
  fi
  [[ "$run_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || return 2
  canonical_profile="$(realpath -e -- "$profile_dir")" || return 2
  canonical_workspace="$(realpath -e -- "$workspace")" || return 2
  [[ "$profile_dir" == "$canonical_profile" && "$workspace" == "$canonical_workspace" &&
     "$workspace" == /* ]] || return 2
  vm_runner_private_directory "$canonical_profile" || return 2
  [[ -d "$canonical_workspace" && -f "$seed" && ! -L "$seed" &&
     -f "$overlay" && ! -L "$overlay" ]] || return 2
  [[ "$seed" == "$canonical_profile/seed.iso" && "$overlay" == "$canonical_profile/disk.qcow2" ]] || return 2
  vm_runner_sensitive_value_valid "$canonical_workspace" &&
    vm_runner_sensitive_value_valid "$seed" && vm_runner_sensitive_value_valid "$overlay" || return 2
  vm_runner_memory_valid "$memory" || return 2
  ssh_timeout="${VM_RUNNER_SSH_TIMEOUT:-300}"
  if ! [[ "$ssh_timeout" =~ ^[1-9][0-9]{0,2}$ ]] || (( 10#$ssh_timeout > 300 )); then
    vm_runner_error 'SSH startup deadline must be between 1 and 300 seconds.'
    return 2
  fi
  if ! declare -F vm_proxy_upstream_args >/dev/null; then
    # shellcheck disable=SC1091 # Fixed sibling config module, resolved from this source file.
    source "$(dirname "$VM_RUNNER_SOURCE_FILE")/config.sh"
  fi
  vm_proxy_upstream_args || return 2
  upstream_args=("${VM_PROXY_UPSTREAM_ARGS[@]}")
  socket="$(vm_runner_runtime_socket "$canonical_profile" "$run_id")" || return 2
  vm_runner_build_virtiofsd_argv "$canonical_workspace" "$socket" || return 2
  vm_runner_build_qemu_argv "$name" "$memory" "$seed" "$overlay" "$socket" \
    60000 3128 "$model_kind" "$model_port" "$metrics_port" || return 2

  run_root="$canonical_profile/runs"
  if [[ ! -e "$run_root" && ! -L "$run_root" ]]; then
    (umask 077; mkdir -- "$run_root") || return 2
  fi
  vm_runner_private_directory "$run_root" || return 2
  run_dir="$run_root/$run_id"
  [[ ! -e "$run_dir" && ! -L "$run_dir" ]] || {
    vm_runner_error 'Run ID already has state; refusing to reuse or overwrite it.'
    return 2
  }
  (umask 077; mkdir -- "$run_dir") || return 2
  registry="$run_dir/processes.json"
  "$VM_RUNNER_PYTHON" "$VM_RUNNER_PROCESS_HELPER" init --registry "$registry" --run-id "$run_id" || return 2
  # Publish exact owned run paths immediately so lifecycle rollback can distinguish
  # a fully stopped failure from an ambiguous process that must remain reserved.
  # shellcheck disable=SC2034 # Public run identity is consumed by lifecycle orchestration.
  VM_RUNNER_PROFILE_DIR="$canonical_profile"
  # shellcheck disable=SC2034 # Public run identity is consumed by lifecycle orchestration.
  VM_RUNNER_RUN_DIR="$run_dir"
  # shellcheck disable=SC2034 # Public run identity is consumed by lifecycle orchestration.
  VM_RUNNER_PROCESS_REGISTRY="$registry"
  # shellcheck disable=SC2034 # Public run identity is consumed by lifecycle orchestration.
  VM_RUNNER_RUN_ID="$run_id"

  port_file="$run_dir/proxy.port"
  printf '[startup] Starting verified proxy and workspace filesystem service.\n' >&2
  marker="$run_dir/proxy.ready"
  vm_runner_spawn_logged "$registry" "$run_id" proxy "$run_dir/proxy.log" \
    "$VM_RUNNER_PYTHON" "$VM_RUNNER_PROXY" "${upstream_args[@]}" \
    --workspace "$canonical_workspace" --port-file "$port_file" --reload-marker "$marker" \
    >/dev/null 2>&1 || true
  proxy_port=''
  local waited=0
  while (( waited < 50 )); do
    proxy_port="$("$VM_RUNNER_PYTHON" "$VM_RUNNER_PROCESS_HELPER" proxy-ready \
      --registry "$registry" --run-id "$run_id" --port-file "$port_file" \
      --marker-file "$marker" 2>/dev/null)" && break
    state="$("$VM_RUNNER_PYTHON" "$VM_RUNNER_PROCESS_HELPER" wait-role \
      --registry "$registry" --run-id "$run_id" --role proxy --timeout 0 2>/dev/null || true)"
    [[ "$state" == stopped || "$state" == unknown ]] && break
    sleep 0.1
    ((waited+=1))
  done
  if ! vm_runner_port_valid "$proxy_port"; then
    cause="$(vm_runner_role_exit_code "$registry" "$run_id" proxy 2>/dev/null || true)"
    vm_runner_write_failure "$run_dir" "$run_id" proxy "$cause" "$run_dir/proxy.log" || true
    vm_runner_rollback "$registry" "$run_id" || true
    return 1
  fi

  vm_runner_build_virtiofsd_argv "$canonical_workspace" "$socket" || {
    vm_runner_write_failure "$run_dir" "$run_id" virtiofsd '' || true
    vm_runner_rollback "$registry" "$run_id" || true
    return 2
  }
  vm_runner_spawn_logged "$registry" "$run_id" virtiofsd "$run_dir/virtiofsd.log" \
    "${VM_VIRTIOFSD_ARGV[@]}" >/dev/null 2>&1 || true
  virtiofs_state=''
  waited=0
  while (( waited < 150 )); do
    state="$("$VM_RUNNER_PYTHON" "$VM_RUNNER_PROCESS_HELPER" wait-role \
      --registry "$registry" --run-id "$run_id" --role virtiofsd --timeout 0 2>/dev/null || true)"
    if [[ "$state" == running && -S "$socket" ]]; then
      virtiofs_state=ready
      break
    fi
    [[ "$state" == stopped || "$state" == unknown ]] && break
    sleep 0.1
    ((waited+=1))
  done
  if [[ "$virtiofs_state" != ready ]]; then
    cause="$(vm_runner_role_exit_code "$registry" "$run_id" virtiofsd 2>/dev/null || true)"
    vm_runner_write_failure "$run_dir" "$run_id" virtiofsd "$cause" "$run_dir/virtiofsd.log" || true
    vm_runner_rollback "$registry" "$run_id" || true
    [[ ! -S "$socket" ]] || rm -f -- "$socket"
    return 1
  fi

  if ! vm_runner_launch_qemu "$registry" "$run_id" "$run_dir" "$name" "$memory" \
    "$seed" "$overlay" "$socket" "$proxy_port" "$model_kind" "$model_port" "$metrics_port"; then
    vm_runner_rollback "$registry" "$run_id" || true
    [[ ! -S "$socket" ]] || rm -f -- "$socket"
    return 1
  fi

  printf '[startup] QEMU running; waiting up to %ss for guest SSH. Console: %s/%s.log\n' \
    "$ssh_timeout" "$run_dir" "$VM_RUNNER_QEMU_ROLE" >&2
  local ssh_started="$SECONDS" next_progress="$SECONDS"
  deadline=$((SECONDS + 10#$ssh_timeout))
  while (( SECONDS < deadline )); do
    role="$VM_RUNNER_QEMU_ROLE"
    state="$("$VM_RUNNER_PYTHON" "$VM_RUNNER_PROCESS_HELPER" wait-role \
      --registry "$registry" --run-id "$run_id" --role "$role" --timeout 0 2>/dev/null || true)"
    if [[ "$state" == stopped || "$state" == unknown ]]; then
      cause="$(vm_runner_role_exit_code "$registry" "$run_id" "$role" 2>/dev/null || true)"
      vm_runner_write_failure "$run_dir" "$run_id" ssh "$cause" "$run_dir/${role}.log" || true
      vm_runner_rollback "$registry" "$run_id" || true
      return 1
    fi
    if vm_ssh_trust_host_key "$run_dir/known_hosts" "$VM_RUNNER_SSH_PORT" \
      "$registry" "$run_id" "$role"; then
      printf '[startup] Guest SSH host key verified.\n' >&2
      return 0
    fi
    if (( SECONDS >= next_progress )); then
      printf '[startup] Waiting for SSH: elapsed=%ss limit=%ss check=%s\n' \
        "$((SECONDS - ssh_started))" "$ssh_timeout" "${VM_SSH_TRUST_STAGE:-unknown}" >&2
      next_progress=$((SECONDS + 15))
    fi
    sleep 1
  done
  printf '[startup] SSH deadline expired; last check=%s. Console: %s/%s.log\n' \
    "${VM_SSH_TRUST_STAGE:-unknown}" "$run_dir" "$VM_RUNNER_QEMU_ROLE" >&2
  vm_runner_write_failure "$run_dir" "$run_id" ssh 124 "$run_dir/${VM_RUNNER_QEMU_ROLE}.log" || true
  vm_runner_rollback "$registry" "$run_id" || true
  return 124
}
