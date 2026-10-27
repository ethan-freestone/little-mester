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
VM_IMAGE_URL="${VM_IMAGE_URL:-https://cloud-images.ubuntu.com/releases/noble/release-20261017/ubuntu-24.04-server-cloudimg-amd64.img}"
VM_IMAGE_SHA256="${VM_IMAGE_SHA256:-04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94}"

vm_disk_validate_base() {
  local base_disk="$1"
  # Older lab-vm created an empty qcow2. Inspect the guest-visible boot sector,
  # not the qcow2 file size (which is sparse/compressed and says nothing).
  qemu-img info --output=json "$base_disk" | python3 -c '
import json,sys
i=json.load(sys.stdin)
assert i["format"] == "qcow2" and not i.get("backing-filename"), "base must be standalone qcow2"
' || return 1
  local sector
  sector=$(mktemp)
  if ! qemu-img dd -f qcow2 -O raw "if=$base_disk" "of=$sector" bs=512 count=1 >/dev/null 2>&1; then
    rm -f "$sector"
    return 1
  fi
  local result=0
  python3 -c 'import sys; d=open(sys.argv[1],"rb").read(); sys.exit(0 if d[510:512] == b"\x55\xaa" else 1)' "$sector" || result=1
  rm -f "$sector"
  return "$result"
}

vm_disk_create_base() (
  set -e
  local base_disk="$1"
  local size="${2:-$VM_BASE_SIZE}"

  if [[ -f "$base_disk" ]]; then
    if vm_disk_validate_base "$base_disk"; then
      log "Base disk already exists: $base_disk"
      return 0
    fi
    if [[ "${VM_YES:-0}" != "1" ]]; then
      log "ERROR: base disk is empty or invalid (older lab-vm created blank disks)."
      log "Run ./lab-vm rebuild, or repeat with --yes to replace it automatically."
      return 1
    fi
    log "Replacing empty/invalid legacy base disk (--yes enabled)."
    rm -f "$base_disk"
  fi

  mkdir -p "$(dirname "$base_disk")"
  local staging
  staging=$(mktemp -d "$(dirname "$base_disk")/.image.XXXXXX")
  trap 'rm -rf "$staging"' EXIT
  log "Downloading pinned Ubuntu 24.04 cloud image (20 GiB is virtual disk size, not download size)..."
  curl --fail --location --retry 3 --output "$staging/image" "$VM_IMAGE_URL"
  printf '%s  %s\n' "$VM_IMAGE_SHA256" "$staging/image" | sha256sum --check --status
  qemu-img convert -f qcow2 -O qcow2 "$staging/image" "$staging/base.qcow2"
  qemu-img resize "$staging/base.qcow2" "${size}G"
  vm_disk_validate_base "$staging/base.qcow2"
  mv "$staging/base.qcow2" "$base_disk"
  log "Verified bootable base disk: $base_disk (${size} GiB virtual)"
)

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
