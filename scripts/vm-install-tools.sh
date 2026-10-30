#!/usr/bin/env bash
# Install optional developer validation tools into an existing VM guest.
# This is intentionally separate from cloud-init/base VM provisioning.
set -euo pipefail

LAB_DIR="$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")/.." && pwd)"
VM_DIR_BASE="${VM_DIR_BASE:-$LAB_DIR/.mester/vm}"
vm_name="${1:-default}"

die() {
  echo "REFUSED: $*" >&2
  exit 1
}

log() { echo "$@"; }
log_verbose() {
  if [[ "${VM_VERBOSE:-0}" -ge 1 ]]; then echo "[verbose] $*"; fi
}
log_debug() {
  if [[ "${VM_DEBUG:-0}" -ge 1 ]]; then echo "[debug] $(date '+%H:%M:%S') $*"; fi
}

[[ $# -le 1 ]] || die 'Usage: ./lab-vm install-tools [name]'
[[ "$vm_name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || die 'Invalid VM profile name.'

# shellcheck source=../lib/vm/runner.sh
source "$LAB_DIR/lib/vm/runner.sh"
# shellcheck source=../lib/vm/ssh.sh
source "$LAB_DIR/lib/vm/ssh.sh"

vm_dir="$VM_DIR_BASE/$vm_name"
[[ -d "$vm_dir" ]] || die "No VM profile found at: $vm_dir"
status="$(vm_run_status "$vm_dir" 2>/dev/null)" || die "VM '$vm_name' is not running; start it with './lab-vm up $vm_name'."
[[ "$status" == running* ]] || die "VM '$vm_name' is not running; start it with './lab-vm up $vm_name'."
for required_file in id_vm_control known_hosts ssh_port; do
  [[ -s "$vm_dir/$required_file" ]] || die "VM '$vm_name' is missing SSH control state: $required_file"
done

[[ "${VM_DEBUG:-0}" =~ ^[0-9]+$ ]] || die 'VM_DEBUG must be a non-negative integer.'
log "Installing Bats and ShellCheck into the running VM '$vm_name'."
log_verbose "Guest package manager: apt; packages: bats shellcheck."
log_debug "VM profile directory: $vm_dir"
log_debug "VM status: $status"

vm_ssh "$vm_dir" "sudo bash -s -- '${VM_DEBUG:-0}'" <<'GUEST_INSTALL'
set -euo pipefail
debug_level="$1"
if (( debug_level >= 1 )); then
  set -x
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y bats shellcheck
printf 'Bats: '
bats --version
printf 'ShellCheck: '
shellcheck --version | sed -n '1,2p'
GUEST_INSTALL

log "Bats and ShellCheck are installed and verified in VM '$vm_name'."
