#!/usr/bin/env bash
# ssh.sh — SSH key management for VM lifecycle.
#
# Generates per-VM SSH keys for host→VM control and manages host keys
# for the SSH server inside the VM (via cloud-init).
#
# Usage:
#   vm_ssh_init_keys <vm_dir>
#   vm_ssh_get_host_key <vm_dir>   # prints public key
#   vm_ssh_clean <vm_dir>          # remove per-VM keys

set -euo pipefail

vm_ssh() {
  local vm_dir="$1"
  shift
  ssh -o BatchMode=yes -o ConnectTimeout=10 \
    -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$vm_dir/known_hosts" \
    -i "$vm_dir/id_vm_control" -p "$(cat "$vm_dir/ssh_port")" \
    mester@127.0.0.1 "$@"
}

vm_ssh_init_keys() {
  local vm_dir="$1"

  mkdir -p "$vm_dir"

  local host_key="$vm_dir/id_vm_control"

  if [[ -f "$host_key" ]]; then
    log "SSH key already exists: $host_key (reusing)"
    return 0
  fi

  ssh-keygen -t ed25519 -f "$host_key" -N '' -C "mester-vm-control" -q
  log "SSH key pair generated: $host_key"
}

vm_ssh_get_host_key() {
  local vm_dir="$1"
  local host_key="$vm_dir/id_vm_control"
  if [[ -f "${host_key}.pub" ]]; then
    cat "${host_key}.pub"
  else
    log "ERROR: SSH host key not found. Run vm_ssh_init_keys first."
    return 1
  fi
}

vm_ssh_clean() {
  local vm_dir="$1"
  rm -f "$vm_dir/id_vm_control" "$vm_dir/id_vm_control.pub"
  rm -f "$vm_dir/known_hosts"
  log "SSH keys cleaned: $vm_dir"
}
