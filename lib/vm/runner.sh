#!/usr/bin/env bash
# runner.sh — QEMU + virtiofsd lifecycle manager.
#
# Starts/stops the VM, virtiofsd, and manages all QEMU parameters:
#   - KVM acceleration (falls back to TCG on CPU-only hosts)
#   - virtiofs workspace export
#   - guestfwd for model APIs and egress proxy
#   - SSH hostfwd on loopback only
#   - restrict=on for network isolation
#
# Usage:
#   vm_run_start <vm_dir> <vm_name> <workspace> <base_disk> <overlay_disk>
#   vm_run_stop <vm_dir>
#   vm_run_status <vm_dir>    # prints "running" or "stopped"

: "${VM_VERBOSE:=0}"
: "${VM_DEBUG:=0}"
: "${VM_MEMORY_GIB:=4}"

vm_memory_validate_gib() {
  local value="${1:-}"
  [[ "$value" =~ ^[1-9][0-9]{0,2}$ ]] && (( value <= 128 ))
}

vm_memory_default_for_depth() {
  local depth="${1:-0}" value
  [[ "$depth" =~ ^(0|[1-9][0-9]*)$ ]] && (( depth <= 32 )) || {
    echo 'REFUSED: recursion depth must be an integer from 0 to 32.' >&2
    return 1
  }
  value=$((8 >> depth))
  (( value >= 1 )) || value=1
  printf '%s\n' "$value"
}

vm_memory_profile_load() {
  local vm_dir="${1:?VM profile directory required}" value default_gib="${2:-4}"
  local -a lines=()
  vm_memory_validate_gib "$default_gib" || {
    echo 'REFUSED: invalid fallback VM memory allocation.' >&2
    return 1
  }
  VM_MEMORY_GIB="$default_gib"
  if [[ ! -e "$vm_dir/memory_gib" && ! -L "$vm_dir/memory_gib" ]]; then
    return 0
  fi
  [[ -f "$vm_dir/memory_gib" && ! -L "$vm_dir/memory_gib" ]] || {
    echo "REFUSED: VM memory metadata must be a regular file: $vm_dir/memory_gib" >&2
    return 1
  }
  mapfile -t lines <"$vm_dir/memory_gib" || return 1
  (( ${#lines[@]} == 1 )) || {
    echo "REFUSED: invalid VM memory metadata in $vm_dir/memory_gib" >&2
    return 1
  }
  value="${lines[0]}"
  vm_memory_validate_gib "$value" || {
    echo "REFUSED: invalid VM memory metadata in $vm_dir/memory_gib" >&2
    return 1
  }
  VM_MEMORY_GIB="$value"
}

vm_memory_profile_select_for_up() {
  local vm_dir="${1:?VM profile directory required}" depth="${2:-0}" force_fresh="${3:-0}" default_gib
  [[ "$force_fresh" == 0 || "$force_fresh" == 1 ]] || return 2
  if [[ "$force_fresh" == 1 ]]; then
    default_gib="$(vm_memory_default_for_depth "$depth")" || return 1
    VM_MEMORY_GIB="$default_gib"
    return 0
  fi
  if [[ -e "$vm_dir/memory_gib" || -L "$vm_dir/memory_gib" ]]; then
    vm_memory_profile_load "$vm_dir"
    return $?
  fi
  # Pre-metadata profiles keep the old 4-GiB allocation. A disk, seed, key or
  # QEMU PID means setup has begun; do not silently reinterpret it as fresh.
  if [[ -e "$vm_dir/disk.qcow2" || -e "$vm_dir/seed.iso" ||
        -e "$vm_dir/id_vm_control" || -e "$vm_dir/qemu.pid" ]]; then
    VM_MEMORY_GIB=4
    return 0
  fi
  default_gib="$(vm_memory_default_for_depth "$depth")" || return 1
  VM_MEMORY_GIB="$default_gib"
}

vm_memory_profile_apply_setup_override() {
  local vm_dir="${1:?VM profile directory required}" value="${2:-}" force_fresh="${3:-0}"
  vm_memory_validate_gib "$value" || {
    echo 'REFUSED: --memory must be a whole number of GiB from 1 to 128.' >&2
    return 1
  }
  [[ "$force_fresh" == 0 || "$force_fresh" == 1 ]] || return 2
  if [[ "$force_fresh" != 1 &&
        ( -e "$vm_dir/memory_gib" || -L "$vm_dir/memory_gib" ||
          -e "$vm_dir/disk.qcow2" || -e "$vm_dir/seed.iso" ||
          -e "$vm_dir/id_vm_control" || -e "$vm_dir/qemu.pid" ) ]]; then
    echo 'REFUSED: --memory only configures a new profile. Use ./lab-vm resize for an existing VM, or explicitly use up --clean to replace it.' >&2
    return 1
  fi
  VM_MEMORY_GIB="$value"
}

vm_memory_profile_store() {
  local vm_dir="${1:?VM profile directory required}" value="${2:-}" tmp
  vm_memory_validate_gib "$value" || {
    echo 'REFUSED: VM memory must be a whole number of GiB from 1 to 128.' >&2
    return 1
  }
  [[ ! -L "$vm_dir" ]] || {
    echo "REFUSED: VM profile directory must not be a symlink: $vm_dir" >&2
    return 1
  }
  mkdir -p "$vm_dir"
  [[ ! -L "$vm_dir/memory_gib" ]] || {
    echo "REFUSED: VM memory metadata must not be a symlink: $vm_dir/memory_gib" >&2
    return 1
  }
  tmp="$(mktemp "$vm_dir/.memory_gib.tmp.XXXXXX")" || return 1
  (umask 077; printf '%s\n' "$value" >"$tmp") || { rm -f -- "$tmp"; return 1; }
  chmod 600 "$tmp"
  mv -f -- "$tmp" "$vm_dir/memory_gib"
}

vm_memory_preflight() {
  local memory_gib="${1:-${VM_MEMORY_GIB:-4}}" release_pid="${2:-}"
  vm_memory_validate_gib "$memory_gib" || {
    echo 'REFUSED: VM memory must be a whole number of GiB from 1 to 128.' >&2
    return 1
  }
  local -a args=(--requested-mib "$((memory_gib * 1024))" --reserve-mib 512)
  [[ -z "$release_pid" ]] || args+=(--release-pid "$release_pid")
  python3 "$LAB_DIR/scripts/vm_memory_budget.py" "${args[@]}"
}

vm_memory_qemu_matches() {
  local process_args="${1:-}" memory_gib="${2:-4}" expected_mib
  vm_memory_validate_gib "$memory_gib" || return 1
  expected_mib=$((memory_gib * 1024))
  [[ " $process_args " == *" -m $expected_mib "* &&
     "$process_args" == *"memory-backend-memfd,id=mem,size=${memory_gib}G,share=on"* ]]
}

# Build the user-mode network argument in one place so its syntax can be
# regression-tested without starting QEMU. guestfwd's right-hand side is a
# per-connection command, so optional model services need not be up at boot.
# A persistent tcp chardev connects at QEMU startup and fails if absent.
_vm_netdev_arg() {
  local ssh_port="$1"
  printf '%s' "user,id=net0,hostfwd=tcp:127.0.0.1:${ssh_port}-:22,guestfwd=tcp:10.0.2.100:11434-cmd:socat - TCP:127.0.0.1:11434,guestfwd=tcp:10.0.2.100:8080-cmd:socat - TCP:127.0.0.1:8080,guestfwd=tcp:10.0.2.100:19532-cmd:socat - TCP:127.0.0.1:19532,guestfwd=tcp:10.0.2.100:3128-cmd:socat - TCP:127.0.0.1:${VM_PROXY_PORT:-3128},restrict=on"
}

# Nested VM proxies use the outer guest's existing host-proxy guestfwd as an
# upstream. Top-level proxies connect directly, preserving their current path.
_vm_proxy_upstream_args() {
  local recursion_root="${HOME%/}/.cache/lab-vm-recursion/"
  VM_PROXY_UPSTREAM_ARGS=()
  if [[ "${VM_NESTED_LAUNCH:-0}" == 1 ]] || \
    [[ -n "${LAB_VM_RECURSION_DEPTH+x}" && "${VM_DIR_BASE:-}" == "$recursion_root"*/vm ]]; then
    VM_PROXY_UPSTREAM_ARGS=(--upstream-proxy=http://10.0.2.100:3128)
  fi
  return 0
}

# Populate VM_QEMU_CMD. Keeping this separate from process launch lets tests
# inspect every argv entry and catches invalid device combinations early.
_vm_build_qemu_command() {
  local vm_dir="$1"
  local vm_name="$2"
  local overlay_disk="$3"
  local virtiofs_sock="$4"
  local ssh_port="$5"
  local machine_accel="$6"
  local kvm_enabled="$7"
  local bios_path="${8:-}"
  local memory_gib="${9:-${VM_MEMORY_GIB:-4}}"
  local memory_mib
  local cpu_model="Westmere"
  vm_memory_validate_gib "$memory_gib" || return 2
  memory_mib=$((memory_gib * 1024))
  local netdev_arg
  netdev_arg="$(_vm_netdev_arg "$ssh_port")"
  # OpenCode's Bun executable requires SSE4.2 even for its baseline build.
  # KVM should expose the actual host CPU; TCG uses a named model with SSE4.2.
  [[ "$kvm_enabled" == "1" ]] && cpu_model="host"

  VM_QEMU_CMD=(qemu-system-x86_64)
  [[ "$kvm_enabled" == "1" ]] && VM_QEMU_CMD+=(-enable-kvm)

  VM_QEMU_CMD+=(
    # Identity and shareable RAM required by vhost-user/virtiofs.
    -name "$vm_name"
    -m "$memory_mib"
    -smp 2
    -cpu "$cpu_model"
    -object "memory-backend-memfd,id=mem,size=${memory_gib}G,share=on"
    -numa node,memdev=mem

    # Disks: cloud-init seed + writable overlay.
    -drive file="$vm_dir/seed.iso",format=raw,if=none,id=cloud_disk,readonly=on
    -device virtio-scsi-pci,id=scsi0
    -device scsi-cd,drive=cloud_disk
    -drive file="$overlay_disk",format=qcow2,if=virtio

    # virtiofsd is a vhost-user backend, not a 9p fsdev.
    -chardev socket,id=charfs,path="$virtiofs_sock"
    -device vhost-user-fs-pci,chardev=charfs,tag=workspace

    # Capture the guest serial console in qemu.log without allocating a TTY.
    -display none
    -serial stdio
    -monitor none

    # Narrow host crossings only: SSH, model APIs and the egress proxy.
    -netdev "$netdev_arg"
    -device virtio-net-pci,netdev=net0

    -no-reboot
    -machine type=q35,accel="$machine_accel"
  )

  [[ -n "$bios_path" ]] && VM_QEMU_CMD+=(-bios "$bios_path")
  return 0
}

_vm_report_qemu_exit() {
  local vm_dir="$1"
  local qemu_pid="$2"
  local qemu_status

  if wait "$qemu_pid" 2>/dev/null; then
    qemu_status=0
  else
    qemu_status=$?
  fi

  log "ERROR: QEMU exited before SSH became ready (status $qemu_status)"
  if [[ -f "$vm_dir/qemu.log" ]]; then
    log "Last 150 lines of qemu.log:"
    tail -n 150 "$vm_dir/qemu.log" | while IFS= read -r line; do log "  $line"; done
  fi
}

# ─── Prerequisite checks ────────────────────────────────────────────
_vm_check_prereqs() {
  local missing=0
  for _cmd in qemu-system-x86_64 qemu-img socat; do
    if ! command -v "$_cmd" >/dev/null 2>&1; then
      log "ERROR: $_cmd not found. Install QEMU packages."
      missing=1
    fi
  done
  if ! command -v virtiofsd >/dev/null 2>&1; then
    log "ERROR: virtiofsd not found. Install libvirt-daemon-config-network or spice-vdagent."
    missing=1
  fi
  if ! command -v cloud-localds >/dev/null 2>&1 && ! command -v genisoimage >/dev/null 2>&1 && ! command -v mkisofs >/dev/null 2>&1; then
    log "ERROR: cloud-localds, genisoimage, or mkisofs not found."
    missing=1
  fi
  [[ $missing -eq 1 ]] && return 1
  return 0
}

# ─── Check KVM availability ─────────────────────────────────────────
_vm_kvm_available() {
  [[ -c /dev/kvm && -r /dev/kvm && -w /dev/kvm ]]
}

# Probe only the key type we use. Avoid a pipeline: grep -q can close it early,
# and a multi-key scan can time out despite having already emitted our key.
_vm_scan_ssh_host_key() {
  local vm_dir="$1" ssh_port="$2" host_keys rc
  if host_keys=$(timeout 3 ssh-keyscan -T 2 -t ed25519 -p "$ssh_port" -H 127.0.0.1 2> "$vm_dir/ssh-keyscan.log"); then
    if grep -q ' ssh-ed25519 ' <<< "$host_keys"; then
      printf '%s\n' "$host_keys" > "$vm_dir/known_hosts"
      return 0
    fi
    log_debug "SSH host-key probe returned no ed25519 key; see $vm_dir/ssh-keyscan.log"
  else
    rc=$?
    log_debug "SSH host-key probe exited with status $rc; see $vm_dir/ssh-keyscan.log"
  fi
  return 1
}

_vm_virtiofs_runtime_root() {
  printf '/tmp/little-mester-vfs-%s\n' "$(id -u)"
}

_vm_virtiofs_runtime_root_check() {
  local root="${1:-$(_vm_virtiofs_runtime_root)}" uid owner mode
  uid="$(id -u)"
  [[ -d /tmp && ! -L /tmp ]] || {
    echo 'REFUSED: /tmp is not a real directory; cannot safely host the virtiofs socket.' >&2
    return 1
  }
  [[ ! -L "$root" ]] || {
    echo "REFUSED: virtiofs runtime directory is a symlink: $root" >&2
    return 1
  }
  [[ -e "$root" ]] || return 0
  [[ -d "$root" ]] || {
    echo "REFUSED: virtiofs runtime path is not a directory: $root" >&2
    return 1
  }
  owner="$(stat -c '%u' "$root")" || return 1
  [[ "$owner" == "$uid" ]] || {
    echo "REFUSED: virtiofs runtime directory is not owned by UID $uid: $root" >&2
    return 1
  }
  mode="$(stat -c '%a' "$root")" || return 1
  [[ "$mode" == 700 ]] || {
    echo "REFUSED: virtiofs runtime directory must have mode 0700: $root (mode $mode)" >&2
    return 1
  }
}

_vm_virtiofs_runtime_root_prepare() {
  local root uid owner
  root="$(_vm_virtiofs_runtime_root)"
  uid="$(id -u)"
  [[ -d /tmp && ! -L /tmp ]] || {
    echo 'REFUSED: /tmp is not a real directory; cannot safely host the virtiofs socket.' >&2
    return 1
  }
  [[ ! -L "$root" ]] || {
    echo "REFUSED: virtiofs runtime directory is a symlink: $root" >&2
    return 1
  }
  if [[ ! -e "$root" && ! -L "$root" ]]; then
    (umask 077; mkdir -- "$root") || {
      [[ -d "$root" && ! -L "$root" ]] || {
        echo "ERROR: could not create private virtiofs runtime directory: $root" >&2
        return 1
      }
    }
  fi
  [[ -d "$root" && ! -L "$root" ]] || {
    echo "REFUSED: virtiofs runtime path is not a real directory: $root" >&2
    return 1
  }
  owner="$(stat -c '%u' "$root")" || return 1
  [[ "$owner" == "$uid" ]] || {
    echo "REFUSED: virtiofs runtime directory is not owned by UID $uid: $root" >&2
    return 1
  }
  chmod 0700 "$root" || return 1
  _vm_virtiofs_runtime_root_check "$root"
}

_vm_virtiofs_short_socket_path() {
  local vm_dir="${1:?VM profile directory required}" canonical digest root socket
  canonical="$(realpath -m -- "$vm_dir")" || return 1
  digest="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' "$canonical")" || return 1
  [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || return 1
  root="$(_vm_virtiofs_runtime_root)"
  socket="$root/$digest.sock"
  (( ${#socket} < 108 )) || {
    echo "ERROR: calculated virtiofs socket path is too long for AF_UNIX: $socket" >&2
    return 1
  }
  printf '%s\n' "$socket"
}

_vm_virtiofsd_args() {
  local workspace="${1:?workspace directory required}" socket="${2:?socket path required}"
  VM_VIRTIOFSD_ARGS=(
    --shared-dir "$workspace"
    --socket-path "$socket"
    --rlimit-nofile=1048576
    --announce-submounts
  )
  if [[ "${VM_DEBUG:-0}" -ge 1 ]]; then
    VM_VIRTIOFSD_ARGS+=(-d)
  fi
}

_vm_virtiofs_socket_path() {
  local vm_dir="${1:?VM profile directory required}" legacy="$1/virtiofs.sock" short root
  if [[ -L "$legacy" ]]; then
    echo "REFUSED: legacy virtiofs socket path is a symlink: $legacy" >&2
    return 1
  fi
  if [[ -e "$legacy" ]]; then
    [[ -S "$legacy" ]] || {
      echo "REFUSED: legacy virtiofs socket path is not a socket: $legacy" >&2
      return 1
    }
    short="$(_vm_virtiofs_short_socket_path "$vm_dir")" || return 1
    [[ ! -e "$short" && ! -L "$short" ]] || {
      echo "REFUSED: both legacy and short virtiofs sockets exist for $vm_dir" >&2
      return 1
    }
    printf '%s\n' "$legacy"
    return 0
  fi
  root="$(_vm_virtiofs_runtime_root)"
  _vm_virtiofs_runtime_root_check "$root" || return 1
  _vm_virtiofs_short_socket_path "$vm_dir"
}

_vm_virtiofs_socket_prepare() {
  local vm_dir="${1:?VM profile directory required}" uid socket old_pid path
  _vm_virtiofs_runtime_root_prepare || return 1
  socket="$(_vm_virtiofs_short_socket_path "$vm_dir")" || return 1
  uid="$(id -u)"
  for path in "$vm_dir/virtiofs.sock" "$socket"; do
    [[ -e "$path" || -L "$path" ]] || continue
    [[ ! -L "$path" && -S "$path" ]] || {
      echo "REFUSED: existing virtiofs socket path is not a regular socket: $path" >&2
      return 1
    }
    [[ "$(stat -c '%u' "$path")" == "$uid" ]] || {
      echo "REFUSED: existing virtiofs socket is not owned by UID $uid: $path" >&2
      return 1
    }
    old_pid="$(cat "$vm_dir/virtiofsd.pid" 2>/dev/null || true)"
    if [[ "$old_pid" =~ ^[1-9][0-9]*$ ]] && kill -0 "$old_pid" 2>/dev/null; then
      echo "REFUSED: virtiofsd PID $old_pid is still alive with a stale socket; refusing to unlink $path" >&2
      return 1
    fi
    rm -f -- "$path" || return 1
  done
  printf '%s\n' "$socket"
}

_vm_virtiofsd_startup_diagnostics() {
  local vm_dir="$1" socket="$2" pid="$3" reason="$4" log_offset="$5"
  local log_file="$vm_dir/virtiofsd.log" diagnostic_file="$vm_dir/virtiofsd-startup.log"
  {
    printf '\n=== virtiofsd startup failure: %s ===\n' "$(date --iso-8601=seconds)"
    printf 'reason: %s\n' "$reason"
    printf 'pid: %s\n' "$pid"
    printf 'socket: %s (%s bytes; Linux AF_UNIX limit is 107)\n' "$socket" "${#socket}"
    if kill -0 "$pid" 2>/dev/null; then
      printf 'process state:\n'
      ps -o pid,ppid,stat,etime,args -p "$pid" 2>&1 || true
    else
      printf 'process state: not running\n'
    fi
    if [[ -s "$log_file" ]] && (( $(wc -c <"$log_file") > log_offset )); then
      printf 'virtiofsd output from this start attempt:\n'
      tail -c +$((log_offset + 1)) "$log_file" | tail -n 100
    else
      printf 'virtiofsd produced no captured stdout/stderr for this start attempt.\n'
    fi
  } >>"$diagnostic_file" 2>&1
  log "ERROR: virtiofsd startup details saved to $diagnostic_file"
  tail -n 100 "$diagnostic_file" | while IFS= read -r line; do log "  $line"; done
}

# ─── Start QEMU VM (background) ─────────────────────────────────────
_vm_start() {
  local vm_dir="$1"
  local vm_name="$2"
  local workspace="$3"
  local base_disk="$4"
  local overlay_disk="$5"
  local ssh_host_key_pub="$6"
  local virtiofs_sock virtiofsd_log virtiofsd_log_offset wait_status

  # Ask the kernel for an available loopback port.
  local ssh_port
  ssh_port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
  log ">>> Starting VM '$vm_name' (SSH port: $ssh_port)"

  # ── Start virtiofsd (workspace export) ───────────────────────────
  VM_STARTING=1
  virtiofs_sock="$(_vm_virtiofs_socket_prepare "$vm_dir")" || return 1
  rm -f "$vm_dir/proxy_port" "$vm_dir/proxy.reload-ready"
  _vm_proxy_upstream_args
  python3 "$LAB_DIR/lib/vm/proxy.py" "${VM_PROXY_UPSTREAM_ARGS[@]}" \
    --workspace "$workspace" --port-file "$vm_dir/proxy_port" \
    --reload-marker "$vm_dir/proxy.reload-ready" \
    </dev/null > "$vm_dir/proxy.log" 2>&1 &
  echo "$!" > "$vm_dir/proxy.pid"
  for i in $(seq 1 30); do
    [[ -s "$vm_dir/proxy_port" ]] && break
    sleep 0.1
  done
  if [[ ! -s "$vm_dir/proxy_port" ]]; then
    log "ERROR: egress proxy failed to start; see $vm_dir/proxy.log"
    _vm_stop "$vm_dir"
    return 1
  fi
  VM_PROXY_PORT=$(cat "$vm_dir/proxy_port")
  virtiofsd_log="$vm_dir/virtiofsd.log"
  _vm_virtiofsd_args "$workspace" "$virtiofs_sock"
  if [[ "$VM_DEBUG" -ge 1 ]]; then
    # QEMU's virtiofsd -d enables daemon debug messages. Do not use --syslog:
    # startup failures must stay in the private per-profile log for diagnosis.
    log_debug "virtiofsd command: $(printf '%q ' virtiofsd "${VM_VIRTIOFSD_ARGS[@]}")"
    log_debug "virtiofsd socket path length: ${#virtiofs_sock} bytes (Linux AF_UNIX limit: 107)"
  fi
  printf '\n=== virtiofsd start: %s ===\n' "$(date --iso-8601=seconds)" >>"$virtiofsd_log"
  virtiofsd_log_offset="$(wc -c <"$virtiofsd_log")"
  virtiofsd "${VM_VIRTIOFSD_ARGS[@]}" \
    >>"$virtiofsd_log" 2>&1 &
  local _virtiofsd_pid=$!
  echo "$_virtiofsd_pid" > "$vm_dir/virtiofsd.pid"
  log_verbose "virtiofsd started (PID: $_virtiofsd_pid, socket: $virtiofs_sock)"

  # Wait for virtiofsd socket
  for i in $(seq 1 30); do
    [[ -S "$virtiofs_sock" ]] && break
    if ! kill -0 "$_virtiofsd_pid" 2>/dev/null; then
      if wait "$_virtiofsd_pid"; then wait_status=0; else wait_status=$?; fi
      _vm_virtiofsd_startup_diagnostics "$vm_dir" "$virtiofs_sock" "$_virtiofsd_pid" \
        "process exited before the socket was created (status $wait_status)" "$virtiofsd_log_offset"
      _vm_stop "$vm_dir"
      return 1
    fi
    sleep 0.5
  done
  if [[ ! -S "$virtiofs_sock" ]]; then
    _vm_virtiofsd_startup_diagnostics "$vm_dir" "$virtiofs_sock" "$_virtiofsd_pid" \
      'socket did not become ready after 15 seconds' "$virtiofsd_log_offset"
    _vm_stop "$vm_dir"
    return 1
  fi

  # ── Build QEMU command ───────────────────────────────────────────
  local machine_accel="tcg"
  local kvm_enabled=0
  if _vm_kvm_available; then
    kvm_enabled=1
    machine_accel="kvm"
    log_verbose "KVM acceleration available"
  else
    log "WARNING: /dev/kvm not available — running in TCG (CPU emulation) mode"
  fi

  _vm_build_qemu_command \
    "$vm_dir" "$vm_name" "$overlay_disk" "$virtiofs_sock" \
    "$ssh_port" "$machine_accel" "$kvm_enabled" "" "${VM_MEMORY_GIB:-4}"

  log_debug "QEMU command: ${VM_QEMU_CMD[*]}"

  # Start QEMU in background (&); capture PID from $! (not -daemonize).
  # All output (stdout + stderr) goes to the log file for inspection.
  : > "$vm_dir/qemu.log"
  "${VM_QEMU_CMD[@]}" </dev/null >> "$vm_dir/qemu.log" 2>&1 &
  local _qemu_pid=$!

  log "QEMU started (PID: $_qemu_pid, log: $vm_dir/qemu.log)"
  echo "$_qemu_pid" > "$vm_dir/qemu.pid"

  # Save SSH port for later
  echo "$ssh_port" > "$vm_dir/ssh_port"
  echo "$_virtiofsd_pid" > "$vm_dir/virtiofsd.pid"

  # ── Wait for SSH to become available ─────────────────────────────
  log "Waiting for SSH..."
  local ssh_timeout="${VM_SSH_TIMEOUT:-300}"
  local deadline=$((SECONDS + ssh_timeout))
  local i=0
  while (( SECONDS < deadline )); do
    i=$((i + 1))
    # Syntax/device errors terminate QEMU immediately. Report those errors now
    # rather than hiding the real cause behind a 60-second SSH timeout.
    if ! kill -0 "$_qemu_pid" 2>/dev/null; then
      _vm_report_qemu_exit "$vm_dir" "$_qemu_pid"
      _vm_stop "$vm_dir"
      return 1
    fi

    if [[ $VM_VERBOSE -ge 1 ]]; then
      # Print attempt count: every attempt in first 10s, then every 10th
      if [[ $i -le 10 ]] || [[ $((i % 10)) -eq 0 ]]; then
        echo "[verbose] SSH check attempt $i (timeout ${ssh_timeout}s)..."
      fi
    fi
    if _vm_scan_ssh_host_key "$vm_dir" "$ssh_port"; then
      log "SSH ready on 127.0.0.1:$ssh_port"
      return 0
    fi
    if [[ $VM_DEBUG -ge 1 && $((i % 10)) -eq 0 && -f "$vm_dir/qemu.log" ]]; then
      log_debug "Last QEMU output (tail -5):"
      log_debug "$(tail -5 "$vm_dir/qemu.log" 2>/dev/null || echo '(empty)')"
    fi
    sleep 1
  done

  log "ERROR: SSH not ready after ${ssh_timeout}s"
  if [[ -f "$vm_dir/qemu.pid" ]]; then
    local _qpid
    _qpid=$(cat "$vm_dir/qemu.pid" 2>/dev/null)
    if kill -0 "$_qpid" 2>/dev/null; then
      log "QEMU process $_qpid is still alive (guest may be booting but SSH not reachable)"
    else
      log "QEMU process $_qpid has exited (see qemu.log for crash details)"
      _vm_report_qemu_exit "$vm_dir" "$_qpid"
    fi
  else
    log "No QEMU PID file found — QEMU may have failed to start"
  fi
  if [[ -f "$vm_dir/qemu.log" ]]; then
    log "Last 150 lines of qemu.log:"
    tail -n 150 "$vm_dir/qemu.log" | while IFS= read -r line; do log "  $line"; done
  fi
  log "To debug: qemu-img info $vm_dir/disk.qcow2 && ss -ltnp | grep ':$ssh_port'"
  return 1
}

# ─── Stop VM cleanly ────────────────────────────────────────────────
_vm_stop() {
  local vm_dir="$1"
  local virtiofs_sock=""

  virtiofs_sock="$(_vm_virtiofs_socket_path "$vm_dir" 2>/dev/null)" || true

  # Stop QEMU gracefully
  if [[ -f "$vm_dir/qemu.pid" ]]; then
    local pid
    pid=$(cat "$vm_dir/qemu.pid")
    if kill -0 "$pid" 2>/dev/null; then
      log "Stopping QEMU (PID: $pid)..."
      kill "$pid" 2>/dev/null || true
      for i in $(seq 1 15); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
      done
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$vm_dir/qemu.pid"
  fi

  # Stop virtiofsd
  if [[ -f "$vm_dir/virtiofsd.pid" ]]; then
    local pid
    pid=$(cat "$vm_dir/virtiofsd.pid")
    if kill -0 "$pid" 2>/dev/null; then
      log "Stopping virtiofsd (PID: $pid)..."
      kill "$pid" 2>/dev/null || true
      for i in $(seq 1 5); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
      done
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$vm_dir/virtiofsd.pid"
  fi

  # Clean up only socket files in the private runtime root or the legacy
  # per-profile path; never unlink a symlink or unrelated regular file.
  if [[ -n "$virtiofs_sock" ]]; then
    if [[ -S "$virtiofs_sock" && ! -L "$virtiofs_sock" ]]; then
      rm -f -- "$virtiofs_sock"
    elif [[ -e "$virtiofs_sock" || -L "$virtiofs_sock" ]]; then
      log "WARNING: preserving unsafe virtiofs socket path: $virtiofs_sock"
    fi
  fi
  if [[ "$virtiofs_sock" != "$vm_dir/virtiofs.sock" ]]; then
    if [[ -S "$vm_dir/virtiofs.sock" && ! -L "$vm_dir/virtiofs.sock" ]]; then
      rm -f -- "$vm_dir/virtiofs.sock"
    elif [[ -e "$vm_dir/virtiofs.sock" || -L "$vm_dir/virtiofs.sock" ]]; then
      log "WARNING: preserving unsafe legacy virtiofs socket path: $vm_dir/virtiofs.sock"
    fi
  fi

  # Clean up stale socket
  if [[ -f "$vm_dir/proxy.pid" ]]; then
    local pid
    pid=$(cat "$vm_dir/proxy.pid")
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 10); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.1
    done
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$vm_dir/proxy.pid" "$vm_dir/proxy_port" "$vm_dir/proxy.reload-ready"
  fi
  rm -f "$vm_dir/mgmt.sock"

  log "VM stopped."
}

# ─── Check VM status ────────────────────────────────────────────────
_vm_status() {
  local vm_dir="$1"

  if [[ -f "$vm_dir/qemu.pid" ]]; then
    local pid
    pid=$(cat "$vm_dir/qemu.pid")
    if kill -0 "$pid" 2>/dev/null; then
      echo "running (PID: $pid, SSH: $(cat "$vm_dir/ssh_port" 2>/dev/null || echo '?'))"
      return 0
    fi
  fi

  echo "stopped"
  return 1
}

# ─── Public entry points ────────────────────────────────────────────
vm_run_start() {
  local vm_dir="$1"
  local vm_name="$2"
  local workspace="$3"
  local base_disk="$4"
  local overlay_disk="$5"
  local ssh_host_key_pub="$6"

  _vm_check_prereqs || return 1
  _vm_start "$vm_dir" "$vm_name" "$workspace" "$base_disk" "$overlay_disk" "$ssh_host_key_pub"
}

vm_run_stop() {
  local vm_dir="$1"
  _vm_stop "$vm_dir"
}

vm_run_status() {
  local vm_dir="$1"
  _vm_status "$vm_dir"
}
