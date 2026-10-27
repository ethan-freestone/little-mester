#!/usr/bin/env bash
# disk.sh — Manage qcow2 disk images for VMs.
#
# Provides a cached base disk (read-only, OS + cloud-init) and per-VM
# qcow2 overlays (writable, deleted on teardown).
#
# Usage:
#   vm_disk_create_base <base_disk> <size_giB>
#   vm_disk_create_overlay <base_disk> <overlay_disk> <size_giB>
#   vm_disk_delete <disk_path>
#   vm_disk_exists <disk_path>

set -euo pipefail

VM_BASE_SIZE="${VM_BASE_SIZE:-20}"  # GiB, adjustable via env

vm_disk_create_base() {
  local base_disk="$1"
  local size="${2:-$VM_BASE_SIZE}"

  [[ -f "$base_disk" ]] && { log "Base disk already exists: $base_disk"; return 0; }

  mkdir -p "$(dirname "$base_disk")"
  qemu-img create -f qcow2 "$base_disk" "${size}G"
  log "Base disk created: $base_disk (${size}G)"
}

vm_disk_create_overlay() {
  local base_disk="$1"
  local overlay_disk="$2"
  local size="${3:-$VM_BASE_SIZE}"

  if [[ -f "$overlay_disk" ]]; then
    log "Overlay already exists: $overlay_disk"
    return 0
  fi

  if [[ ! -f "$base_disk" ]]; then
    log "ERROR: Base disk not found: $base_disk"
    return 1
  fi

  mkdir -p "$(dirname "$overlay_disk")"
  qemu-img create -f qcow2 -b "$base_disk" -F qcow2 "$overlay_disk" "${size}G"
  log "Overlay created: $overlay_disk (backing: $(basename "$base_disk"))"
}

vm_disk_delete() {
  local disk="$1"
  if [[ -f "$disk" ]]; then
    rm -f "$disk"
    log "Disk deleted: $disk"
  else
    log "Disk not found (skipping): $disk"
  fi
}

vm_disk_exists() {
  [[ -f "$1" ]]
}
