#!/usr/bin/env bash
# Install/refresh validation tools in an existing guest without reprovisioning.
# Bats/ShellCheck are optional; the Python CLI toolchain also matches base setup.
set -euo pipefail

LAB_DIR="$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")/.." && pwd)"
VM_STATE_DIR="${VM_STATE_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/little-mester}"
if [[ -n "${VM_DIR_BASE+x}" ]]; then
  :
elif [[ -d "$LAB_DIR/.mester/vm" && ! -e "$VM_STATE_DIR/vm" ]]; then
  VM_DIR_BASE="$LAB_DIR/.mester/vm"
else
  VM_DIR_BASE="$VM_STATE_DIR/vm"
fi
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
# shellcheck disable=SC1091
source "$LAB_DIR/lib/vm/runner.sh"
# shellcheck source=../lib/vm/ssh.sh
# shellcheck disable=SC1091
source "$LAB_DIR/lib/vm/ssh.sh"

vm_dir="$VM_DIR_BASE/$vm_name"
[[ -d "$vm_dir" ]] || die "No VM profile found at: $vm_dir"
status="$(vm_run_status "$vm_dir" 2>/dev/null)" || die "VM '$vm_name' is not running; start it with './lab-vm up $vm_name'."
[[ "$status" == running* ]] || die "VM '$vm_name' is not running; start it with './lab-vm up $vm_name'."
for required_file in id_vm_control known_hosts ssh_port; do
  [[ -s "$vm_dir/$required_file" ]] || die "VM '$vm_name' is missing SSH control state: $required_file"
done

[[ "${VM_DEBUG:-0}" =~ ^[0-9]+$ ]] || die 'VM_DEBUG must be a non-negative integer.'
python_tools_script="$LAB_DIR/scripts/vm-python-tools.sh"
[[ -r "$python_tools_script" ]] || die 'The managed Python tool installer is missing.'
log "Installing/updating Bats, ShellCheck, pip, Ruff, and uv in the running VM '$vm_name'."
log_verbose "Guest package manager: apt; Python CLI tools use a dedicated virtual environment."
log_debug "VM profile directory: $vm_dir"
log_debug "VM status: $status"

{
  cat <<'GUEST_INSTALL'
set -euo pipefail
debug_level="$1"
if (( debug_level >= 1 )); then
  set -x
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y bats shellcheck python3-pip python3-venv
shift
GUEST_INSTALL
  cat "$python_tools_script"
  cat <<'GUEST_VERIFY'
printf 'Bats: '
bats --version
printf 'ShellCheck: '
shellcheck --version | sed -n '1,2p'
GUEST_VERIFY
} | vm_ssh "$vm_dir" "sudo bash -s -- '${VM_DEBUG:-0}' install"

log "Bats, ShellCheck, pip, Ruff, and uv are installed and verified in VM '$vm_name'."
