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

# ─── Prerequisite checks ────────────────────────────────────────────
_vm_check_prereqs() {
  local missing=0
  for _cmd in qemu-system-x86_64 qemu-img; do
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
  [[ -e /dev/kvm ]] && ls -la /dev/kvm 2>/dev/null | grep -q 'crw'
}

# ─── Start QEMU VM (background) ─────────────────────────────────────
_vm_start() {
  local vm_dir="$1"
  local vm_name="$2"
  local workspace="$3"
  local base_disk="$4"
  local overlay_disk="$5"
  local ssh_host_key_pub="$6"

  # Get SSH ephemeral host-forward port (find first free port in range)
  local ssh_port
  log_verbose "Finding free SSH port in range 20000-30000..."
  for p in $(seq 20000 20100); do
    if ! ss -tln 2>/dev/null | grep -qE ":[[:space:]]*${p}$|:[[:space:]]*0\.0\.0\.0:${p}$|:[[:space:]]*\*:${p}$|:[[:space:]]*\[::\]:${p}$"; then
      ssh_port=$p
      log_verbose "Found free port: $p"
      break
    fi
  done
  if [[ -z "${ssh_port:-}" ]]; then
    ssh_port=20000
    log_verbose "No free port found in 20000-20100; defaulting to $ssh_port"
  fi
  log ">>> Starting VM '$vm_name' (SSH port: $ssh_port)"

  # ── Start virtiofsd (workspace export) ───────────────────────────
  local virtiofs_sock="$vm_dir/virtiofs.sock"
  rm -f "$virtiofs_sock"

  virtiofsd \
    --syslog \
    --shared-dir "$workspace" \
    --socket-path "$virtiofs_sock" \
    --rlimit-nofile=1048576 \
    --announce-submounts \
    -f /dev/null &
  local _virtiofsd_pid=$!
  log_verbose "virtiofsd started (PID: $_virtiofsd_pid, socket: $virtiofs_sock)"

  # Wait for virtiofsd socket
  for i in $(seq 1 30); do
    [[ -S "$virtiofs_sock" ]] && break
    sleep 0.5
  done
  [[ -S "$virtiofs_sock" ]] || { log "ERROR: virtiofsd socket not ready after 15s"; return 1; }

  # ── Build QEMU command ───────────────────────────────────────────
  local kvm_flag=""
  local machine_accel="tcg"
  if _vm_kvm_available; then
    kvm_flag="-enable-kvm"
    machine_accel="kvm"
    log_verbose "KVM acceleration available"
  else
    log "WARNING: /dev/kvm not available — running in TCG (CPU emulation) mode"
  fi

  # ── Determine BIOS (must be outside the array since || is a shell
  #    operator, not an array element) ────────────────────────────────
  local bios_opt=""
  if [[ -f /usr/share/OVMF/OVMF.fd ]]; then
    bios_opt="-bios /usr/share/OVMF/OVMF.fd"
    log_verbose "Using OVMF UEFI BIOS"
  fi

  local cmdline=(
    qemu-system-x86_64
    $kvm_flag

    # Identity
    -name "$vm_name"
    -m 4096
    -smp 2

    # Disks: cloud-init seed + base/overlay
    -drive file="$vm_dir/seed.iso",format=raw,if=none,id=cloud_disk,readonly=on
    -device virtio-scsi-pci,id=scsi0
    -device scsi-cd,drive=cloud_disk
    -drive file="$overlay_disk",format=qcow2,if=virtio,aio=native
    -drive file="$base_disk",format=qcow2,if=virtio,read-only=on,id=base

    # Virtiofs workspace export
    -chardev socket,id=charfs,path="$virtiofs_sock"
    -device virtio-9p-pci,id=fs0,fsdev=charfs,mount_tag=host-workspace

    # Cloud-init: serial console for logging
    -serial stdio
    -nographic

    # Network: user-mode with guestfwd for model APIs and egress proxy
    -netdev user,id=net0,hostfwd=tcp:127.0.0.1:${ssh_port}-:22,guestfwd=tcp:10.0.2.100:11434-10.0.2.2:11434,guestfwd=tcp:10.0.2.100:8080-10.0.2.2:8080,guestfwd=tcp:10.0.2.100:3128-10.0.2.2:3128,restrict=on
    -device virtio-net-pci,netdev=net0

    # virtio console for guest-host communication
    -chardev socket,id=mgmt,path="$vm_dir/mgmt.sock",server=on,wait=off
    -device virtserialport,chardev=mgmt,name=mester.mgmt

    # Stop on crash
    -no-reboot

    # Machine type — accel is conditional (kvm when available, tcg fallback)
    -machine type=q35,accel=$machine_accel

    # BIOS (OVMF) — conditional; SeaBIOS is the fallback
    $bios_opt
  )

  log_debug "QEMU command: ${cmdline[*]}"

  # Start QEMU in background (&); capture PID from $! (not -daemonize).
  # All output (stdout + stderr) goes to the log file for inspection.
  "${cmdline[@]}" >> "$vm_dir/qemu.log" 2>&1 &
  local _qemu_pid=$!

  log "QEMU started (PID: $_qemu_pid, log: $vm_dir/qemu.log)"
  echo "$_qemu_pid" > "$vm_dir/qemu.pid"

  # Save SSH port for later
  echo "$ssh_port" > "$vm_dir/ssh_port"
  echo "$_virtiofsd_pid" > "$vm_dir/virtiofsd.pid"

  # ── Wait for SSH to become available ─────────────────────────────
  log "Waiting for SSH..."
  for i in $(seq 1 60); do
    log_verbose "SSH check attempt $i/60..."
    if ssh-keyscan -p "$ssh_port" -H 127.0.0.1 2>/dev/null | grep -q ssh-ed25519; then
      ssh-keyscan -p "$ssh_port" -H 127.0.0.1 2>/dev/null > "$vm_dir/known_hosts"
      log "SSH ready on 127.0.0.1:$ssh_port"
      return 0
    fi
    sleep 1
  done

  log "ERROR: SSH not ready after 60s. Check $vm_dir/qemu.log"
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
