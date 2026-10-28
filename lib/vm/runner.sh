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

# Build the user-mode network argument in one place so its syntax can be
# regression-tested without starting QEMU. guestfwd's right-hand side is a
# per-connection command, so optional model services need not be up at boot.
# A persistent tcp chardev connects at QEMU startup and fails if absent.
_vm_netdev_arg() {
  local ssh_port="$1"
  printf '%s' "user,id=net0,hostfwd=tcp:127.0.0.1:${ssh_port}-:22,guestfwd=tcp:10.0.2.100:11434-cmd:socat - TCP:127.0.0.1:11434,guestfwd=tcp:10.0.2.100:8080-cmd:socat - TCP:127.0.0.1:8080,guestfwd=tcp:10.0.2.100:19532-cmd:socat - TCP:127.0.0.1:19532,guestfwd=tcp:10.0.2.100:3128-cmd:socat - TCP:127.0.0.1:${VM_PROXY_PORT:-3128},restrict=on"
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
  local cpu_model="Westmere"
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
    -m 4096
    -smp 2
    -cpu "$cpu_model"
    -object memory-backend-memfd,id=mem,size=4G,share=on
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

# ─── Start QEMU VM (background) ─────────────────────────────────────
_vm_start() {
  local vm_dir="$1"
  local vm_name="$2"
  local workspace="$3"
  local base_disk="$4"
  local overlay_disk="$5"
  local ssh_host_key_pub="$6"

  # Ask the kernel for an available loopback port.
  local ssh_port
  ssh_port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
  log ">>> Starting VM '$vm_name' (SSH port: $ssh_port)"

  # ── Start virtiofsd (workspace export) ───────────────────────────
  VM_STARTING=1
  rm -f "$vm_dir/proxy_port"
  python3 "$LAB_DIR/lib/vm/proxy.py" --workspace "$workspace" --port-file "$vm_dir/proxy_port" \
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
  local virtiofs_sock="$vm_dir/virtiofs.sock"
  rm -f "$virtiofs_sock"

  virtiofsd \
    --syslog \
    --shared-dir "$workspace" \
    --socket-path "$virtiofs_sock" \
    --rlimit-nofile=1048576 \
    --announce-submounts \
    >> "$vm_dir/virtiofsd.log" 2>&1 &
  local _virtiofsd_pid=$!
  echo "$_virtiofsd_pid" > "$vm_dir/virtiofsd.pid"
  log_verbose "virtiofsd started (PID: $_virtiofsd_pid, socket: $virtiofs_sock)"

  # Wait for virtiofsd socket
  for i in $(seq 1 30); do
    [[ -S "$virtiofs_sock" ]] && break
    sleep 0.5
  done
  if [[ ! -S "$virtiofs_sock" ]]; then
    log "ERROR: virtiofsd socket not ready after 15s"
    tail -50 "$vm_dir/virtiofsd.log"
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
    "$ssh_port" "$machine_accel" "$kvm_enabled" ""

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

  # Clean up stale socket
  if [[ -f "$vm_dir/proxy.pid" ]]; then
    kill "$(cat "$vm_dir/proxy.pid")" 2>/dev/null || true
    rm -f "$vm_dir/proxy.pid" "$vm_dir/proxy_port"
  fi
  rm -f "$vm_dir/virtiofs.sock" "$vm_dir/mgmt.sock"

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
