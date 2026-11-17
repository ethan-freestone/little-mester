#!/usr/bin/env bash
# Smoke cloud-init is rendered by the shared guest bootstrap generator.

vm_host_cloud_init() {
  local name="${1:?VM name required}" uid="${2:?host uid required}"
  local ssh_key="${3:?SSH key required}"
  [[ -n "${VM_HOST_SCRIPT_ROOT:-}" ]] || return 2
  python3 -I "$VM_HOST_SCRIPT_ROOT/lib/vm/guest-bootstrap.py" smoke-cloud-init \
    --name "$name" --uid "$uid" --ssh-key "$ssh_key"
}
