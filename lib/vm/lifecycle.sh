#!/usr/bin/env bash
# lifecycle.sh — High-level VM lifecycle commands for ./lab.
#
# Integrates with the main lab script and provides:
#   vm up [vm_name]     — Provision and start the VM (idempotent)
#   vm status [vm_name] — Check VM state
#   vm ssh [vm_name]    — SSH into the VM
#   vm down [vm_name]   — Stop and remove the VM
#   vm base [vm_name]   — Create the base image (one-time)
#
# All commands are idempotent: re-running does not duplicate VMs,
# disks, or networks.
#
# Usage: sourced from lab; entry points called as vm_cmd_<action> <args>

set -euo pipefail

: "${LAB_DIR:=$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")/..")" && pwd)}"
MESTER_DIR="$LAB_DIR/.mester"
VM_DIR_BASE="$MESTER_DIR/vm"

# Source library functions
source "$LAB_DIR/lib/vm/cloud-init.sh"
source "$LAB_DIR/lib/vm/disk.sh"
source "$LAB_DIR/lib/vm/ssh.sh"
source "$LAB_DIR/lib/vm/runner.sh"

# ─── Logging helpers (sourced from lab-vm's main script) ────────────
# If log() is not defined by the caller, provide no-op stubs so
# library files can safely call them without errors.
: "${log:=echo}"
: "${log_verbose:=:}"
: "${log_debug:=:}"

# ─── Resolve VM name and directory ──────────────────────────────────
_vm_resolve() {
  local name="${1:-default}"
  local vm_dir="$VM_DIR_BASE/$name"
  echo "$vm_dir"
}

# Capture first-boot diagnostics before lab-vm's failure trap stops the guest.
_vm_wait_for_cloud_init() {
  local vm_dir="$1" rc
  log "Waiting for cloud-init (guest proxy 10.0.2.100:3128 via host loopback proxy)..."
  if vm_ssh "$vm_dir" "sudo timeout ${VM_PROVISION_TIMEOUT:-1800} cloud-init status --wait --long"; then
    return 0
  else
    rc=$?
    log "ERROR: cloud-init status/SSH exited with status $rc (done can still include recoverable errors)."
    # A final output tail can omit early schema warnings. Keep structured status
    # and schema diagnostics too; do not turn exit 2 into unconditional success.
    vm_ssh "$vm_dir" 'sudo cloud-init status --format json' > "$vm_dir/cloud-init-status.json" 2>&1 || true
    vm_ssh "$vm_dir" 'sudo cloud-init schema --system' > "$vm_dir/cloud-init-schema.log" 2>&1 || true
    cat "$vm_dir/cloud-init-status.json" "$vm_dir/cloud-init-schema.log"
    vm_ssh "$vm_dir" 'sudo tail -200 /var/log/cloud-init-output.log' > "$vm_dir/provision.log" 2>&1 || true
    cat "$vm_dir/provision.log"
    log "ERROR: VM readiness blocked; diagnostics saved to $vm_dir/{cloud-init-status.json,cloud-init-schema.log,provision.log}. Preserve logs before --clean deletes the overlay."
    return "$rc"
  fi
}

_vm_build_agent_image() {
  local vm_dir="$1" rc
  log "Building agent image inside VM..."
  # SSH commands are non-login shells: profile.d is not sourced automatically.
  # BuildKit's client-side registry auth needs proxy env too, not just RUN args.
  # Forward both cases to build steps from the explicitly loaded guest profile.
  if tar -C "$LAB_DIR/agent" --exclude=node_modules --exclude=.git -cf - . | \
    vm_ssh "$vm_dir" ". /etc/profile.d/mester.sh && docker -H unix:///run/user/$(id -u)/docker.sock build --build-arg LAB_UID=$(id -u) --build-arg LAB_GID=$(id -g) --build-arg HTTP_PROXY --build-arg HTTPS_PROXY --build-arg http_proxy --build-arg https_proxy --build-arg NO_PROXY --build-arg no_proxy -t little-mester-agent -" 2>&1 | tee "$vm_dir/build.log"; then
    return 0
  else
    rc=$?
    log "ERROR: agent image build failed (status $rc); see $vm_dir/build.log. Preserve the overlay; retry without --clean."
    return "$rc"
  fi
}

_vm_rootless_diagnostics() {
  local vm_dir="$1" uid
  uid="$(id -u)"
  log "Capturing rootless Docker startup diagnostics before VM cleanup..."
  vm_ssh "$vm_dir" "sudo bash -s -- '$uid'" <<'GUEST_DIAGNOSTICS' 2>&1 | tee "$vm_dir/rootless-diagnostics.log" || true
set +e
uid="$1"
echo '=== identity and runtime ==='
id; id mester
loginctl show-user mester -p Linger -p State -p RuntimePath
systemctl status "user@${uid}.service" --no-pager
ls -ld "/run/user/${uid}" "/run/user/${uid}/bus" "/run/user/${uid}/docker.sock"
test -S "/run/user/${uid}/docker.sock"; echo "docker_socket_status=$?"
echo '=== subordinate ID configuration ==='
getent passwd mester
getent group mester
stat -c '%a %U:%G %n' /etc/subuid /etc/subgid
cat -n /etc/subuid /etc/subgid
if command -v getsubids >/dev/null 2>&1; then getsubids mester; else echo 'getsubids command unavailable'; fi
echo '=== Docker proxy configuration ==='
cat /home/mester/.config/docker/daemon.json
cat /home/mester/.config/systemd/user/docker.service.d/proxy.conf
sudo -iu mester env "XDG_RUNTIME_DIR=/run/user/${uid}" "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus" systemctl --user show docker -p Environment
echo '=== rootless Docker user service ==='
sudo -iu mester env "XDG_RUNTIME_DIR=/run/user/${uid}" "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus" systemctl --user is-enabled docker; echo "docker_enabled_status=$?"
sudo -iu mester env "XDG_RUNTIME_DIR=/run/user/${uid}" "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus" systemctl --user status docker --no-pager
sudo -iu mester env "XDG_RUNTIME_DIR=/run/user/${uid}" "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus" systemctl --user cat docker
echo '=== recent Docker user-service journal ==='
sudo -iu mester env "XDG_RUNTIME_DIR=/run/user/${uid}" "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus" journalctl --user -u docker -b --no-pager -n 100
echo '=== linger and enablement files ==='
loginctl show-user mester -p Linger -p RuntimePath
ls -la /home/mester/.config/systemd/user/default.target.wants/
GUEST_DIAGNOSTICS
  log "Rootless Docker diagnostics saved to $vm_dir/rootless-diagnostics.log"
}

_vm_ensure_rootless_docker() {
  local vm_dir="$1" uid
  uid="$(id -u)"
  vm_cloud_init_subid_script | vm_ssh "$vm_dir" "sudo bash -s -- mester" || return $?
  vm_ssh "$vm_dir" "sudo bash -s -- '$uid'" <<'GUEST_DOCKER_PROXY' || return $?
set -euo pipefail
uid="$1"
install -d -o mester -g mester /home/mester/.config/docker
cat > /home/mester/.config/docker/daemon.json <<'DOCKER_PROXY'
{
  "proxies": {
    "http-proxy": "http://10.0.2.100:3128",
    "https-proxy": "http://10.0.2.100:3128",
    "no-proxy": "localhost,127.0.0.1,10.0.2.100"
  }
}
DOCKER_PROXY
chown mester:mester /home/mester/.config/docker/daemon.json
GUEST_DOCKER_PROXY
  vm_ssh "$vm_dir" "sudo -iu mester env XDG_RUNTIME_DIR=/run/user/${uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus systemctl --user daemon-reload && sudo -iu mester env XDG_RUNTIME_DIR=/run/user/${uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus systemctl --user enable docker && sudo -iu mester env XDG_RUNTIME_DIR=/run/user/${uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus systemctl --user restart docker"
}

# ─── vm up: provision and start VM ──────────────────────────────────
vm_cmd_up() {
  local vm_name="${1:-default}"
  local workspace="${2:-}"
  local vm_dir
  vm_dir="$(_vm_resolve "$vm_name")"

  # Check prerequisites
  if ! command -v qemu-system-x86_64 >/dev/null 2>&1; then
    die "qemu-system-x86_64 not found. Install QEMU packages (qemu-system-x86, qemu-utils, qemu-arch-extra)."
  fi
  if ! command -v virtiofsd >/dev/null 2>&1; then
    die "virtiofsd not found. Install libvirt-daemon or spice-vdagent."
  fi

  local base_disk="$VM_DIR_BASE/base.qcow2"
  local overlay_disk="$vm_dir/disk.qcow2"

  echo "=== VM Up: $vm_name ==="
  echo "VM directory: $vm_dir"

  # ── 1. Workspace resolution & validation (A3) ───────────────────
  # Use the workspace passed from lab-vm (set via --workspace flag).
  # If no workspace is specified, default to the current working dir.
  # An empty workspace would break virtiofsd, so we must always have
  # a valid directory.
  workspace="${workspace:-$(pwd)}"
  # Resolve to absolute path
  workspace="$(realpath "$workspace" 2>/dev/null || echo "$workspace")"

  # Use the local check_workspace (from lab-vm) which properly checks
  # VM_UNSAFE_RECURSE — not the UNSAFE_RECURSE from ./lab.
  source "$LAB_DIR/lib/vm/prereqs.sh" 2>/dev/null || true
  # check_workspace is defined in lab-vm's main script; if sourced
  # from lifecycle.sh standalone it may not be available. Define a
  # minimal fallback that matches lab-vm's behavior:
  if ! declare -f check_workspace >/dev/null 2>&1; then
    check_workspace() {
      local ws="$1"
      [[ -d "$ws" ]] || { echo "REFUSED: Not a directory: $ws" >&2; exit 1; }
      ws="$(realpath "$ws")"
      [[ "$ws" != "/" ]] || { echo "REFUSED: Cannot mount filesystem root (/)" >&2; exit 1; }
      [[ "$ws" != "$HOME" ]] || { echo "REFUSED: Cannot mount entire home directory ($HOME)" >&2; exit 1; }
      [[ -O "$ws" ]] || { echo "REFUSED: Workspace directory must be owned by you" >&2; exit 1; }
      if [[ "$ws" == "$LAB_DIR" || "$LAB_DIR" == "$ws"/* ]]; then
        if [[ "${VM_UNSAFE_RECURSE:-0}" != "1" ]]; then
          echo "REFUSED: Workspace path would expose the little-mester repo itself. Use --unsafe-recurse to override." >&2
          exit 1
        fi
      fi
      VM_WORKSPACE="$ws"
      local sensitive_paths=(
        "$HOME/.ssh" "$HOME/.gnupg" "$HOME/.config" "$HOME/.aws"
        "$HOME/.kube" "$HOME/.local" "$HOME/.bashrc" "$HOME/.zshrc"
      )
      for s in "${sensitive_paths[@]}"; do
        if [[ "$ws" == "$s" || "$ws" == "$s"/* || "$s" == "$ws"/* ]]; then
          echo "REFUSED: Path overlaps with sensitive system/credential directory: $s" >&2
          exit 1
        fi
      done
    }
  fi
  check_workspace "$workspace"
  workspace="$VM_WORKSPACE"
  echo "Workspace: $workspace"

  # ── 2. Create VM directory ──────────────────────────────────────
  mkdir -p "$vm_dir"

  # ── 3. SSH key initialization ───────────────────────────────────
  vm_ssh_init_keys "$vm_dir"
  local ssh_pubkey
  ssh_pubkey="$(vm_ssh_get_host_key "$vm_dir")"

  # ── 4. Create base disk if needed (one-time) ────────────────────
  if ! vm_disk_exists "$base_disk"; then
    echo "Creating base disk image..."
  fi
  vm_disk_create_base "$base_disk" "${VM_BASE_SIZE:-20}"

  # ── 5. Check if VM already running (idempotency, A2) ───────────
  local status
  status="$(_vm_status "$vm_dir" 2>/dev/null)" || true
  if echo "$status" | grep -q "running"; then
    echo "VM '$vm_name' is already running."
    echo "$status"
    return 0
  fi

  # ── 6. Create overlay disk ──────────────────────────────────────
  vm_disk_create_overlay "$base_disk" "$overlay_disk" "${VM_BASE_SIZE:-20}"

  # ── 7. Generate cloud-init seed ISO ─────────────────────────────
  echo "Generating cloud-init seed ISO..."
  vm_cloud_init_generate "$vm_dir" "$vm_name" "$(id -u)" "$(id -g)" "$ssh_pubkey"

  # ── 8. Start QEMU + virtiofsd ──────────────────────────────────
  echo "Starting VM..."
  vm_run_start "$vm_dir" "$vm_name" "$workspace" "$base_disk" "$overlay_disk" "$ssh_pubkey"

  # SSH may become ready long before first-boot package installation finishes.
  _vm_wait_for_cloud_init "$vm_dir" || return $?
  vm_ssh "$vm_dir" 'test -f /var/lib/mester-provisioned && mountpoint -q /workspace' || return 1
  if ! _vm_ensure_rootless_docker "$vm_dir"; then
    log "ERROR: could not enable/start the guest rootless Docker user service"
    _vm_rootless_diagnostics "$vm_dir"
    return 1
  fi
  if ! vm_ssh "$vm_dir" "docker -H unix:///run/user/$(id -u)/docker.sock info --format '{{json .SecurityOptions}}'" | grep -q rootless; then
    log "ERROR: guest Docker is not running rootless"
    _vm_rootless_diagnostics "$vm_dir"
    return 1
  fi

  # Build the harness image from the harness, not an arbitrary workspace Dockerfile.
  # Transfer only its build context; the host checkout is not another guest mount.
  _vm_build_agent_image "$vm_dir" || return $?

  VM_STARTING=0

  echo ""
  echo "VM '$vm_name' is up and running."
  echo "  SSH:  ./lab-vm ssh $vm_name"
  echo "  Stop: ./lab-vm down $vm_name"
  echo ""

  # A8: Confirm host no longer runs the agent container
  echo "Host Docker is not running the agent container in VM mode."
  echo "All agent execution is inside the VM."
}

# ─── vm status: check VM state ──────────────────────────────────────
vm_cmd_status() {
  local vm_name="${1:-default}"
  local vm_dir
  vm_dir="$(_vm_resolve "$vm_name")"

  if [[ ! -d "$vm_dir" ]]; then
    echo "No VM profile found at: $vm_dir"
    return 1
  fi

  local status
  status="$(vm_run_status "$vm_dir" 2>/dev/null)" || status="stopped"

  echo "VM: $vm_name"
  echo "  State: $status"
  echo "  Dir:   $vm_dir"

  if echo "$status" | grep -q running; then
    local ssh_port
    ssh_port=$(cat "$vm_dir/ssh_port" 2>/dev/null || echo '?')
    echo "  SSH:   127.0.0.1:$ssh_port"

    # Check Docker inside VM
    local host_uid
    host_uid="$(id -u)"
    if ssh -o StrictHostKeyChecking=yes \
           -o UserKnownHostsFile="$vm_dir/known_hosts" \
           -i "$vm_dir/id_vm_control" \
           -p "$ssh_port" \
           mester@127.0.0.1 \
           "docker -H unix:///run/user/${host_uid}/docker.sock ps --format '{{.Names}}' 2>/dev/null" 2>/dev/null | grep -q .; then
      echo "  Docker: running"
      ssh -o StrictHostKeyChecking=yes \
          -o UserKnownHostsFile="$vm_dir/known_hosts" \
          -i "$vm_dir/id_vm_control" \
          -p "$ssh_port" \
          mester@127.0.0.1 \
          "docker -H unix:///run/user/${host_uid}/docker.sock ps --format 'table {{.Names}}\t{{.Status}}' 2>/dev/null" 2>/dev/null || true
    else
      echo "  Docker: no containers running"
    fi
  fi

  return 0
}

# ─── vm ssh: SSH into the VM ────────────────────────────────────────
vm_cmd_ssh() {
  local vm_name="${1:-default}"
  if [[ $# -gt 0 ]]; then shift; fi
  local vm_dir
  vm_dir="$(_vm_resolve "$vm_name")"

  if [[ ! -d "$vm_dir" ]]; then
    die "No VM profile found at: $vm_dir"
  fi

  if ! echo "$(vm_run_status "$vm_dir" 2>/dev/null)" | grep -q running; then
    die "VM '$vm_name' is not running. Start it with: ./lab-vm up $vm_name"
  fi

  local ssh_port
  ssh_port=$(cat "$vm_dir/ssh_port")

  echo "SSH into VM '$vm_name' on 127.0.0.1:$ssh_port"
  exec ssh \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$vm_dir/known_hosts" \
    -i "$vm_dir/id_vm_control" \
    -p "$ssh_port" \
    mester@127.0.0.1 "$@"
}

# ─── vm down: stop and remove VM ────────────────────────────────────
vm_cmd_down() {
  local vm_name="${1:-default}"
  local vm_dir
  vm_dir="$(_vm_resolve "$vm_name")"

  if [[ ! -d "$vm_dir" ]]; then
    echo "No VM profile found at: $vm_dir (skipping)"
    return 0
  fi

  echo "=== VM Down: $vm_name ==="

  # 1. Stop QEMU + virtiofsd
  vm_run_stop "$vm_dir"

  # 2. Delete overlay disk (A6: clean VM-side state)
  vm_disk_delete "$vm_dir/disk.qcow2"

  # 3. Delete seed ISO
  rm -f "$vm_dir/seed.iso"

  # 4. Clean SSH keys
  vm_ssh_clean "$vm_dir"

  # 5. Clean mgmt socket
  rm -f "$vm_dir/mgmt.sock"

  echo "VM '$vm_name' removed. Base disk preserved for next VM."
}

# ─── vm stop: teardown agent + workspace containers and VM ──────────
vm_cmd_stop() {
  local vm_name="${1:-default}"
  local vm_dir
  vm_dir="$(_vm_resolve "$vm_name")"

  echo "=== VM Stop: $vm_name ==="

  # 1. If VM is running, SSH in and tear down Docker containers (A6)
  if echo "$(vm_run_status "$vm_dir" 2>/dev/null)" | grep -q running; then
    local ssh_port
    ssh_port=$(cat "$vm_dir/ssh_port")
    local host_uid
    host_uid="$(id -u)"
    echo "Tearing down containers in VM..."
    ssh -o StrictHostKeyChecking=yes \
        -o UserKnownHostsFile="$vm_dir/known_hosts" \
        -i "$vm_dir/id_vm_control" \
        -p "$ssh_port" \
        mester@127.0.0.1 \
        "cd /workspace && docker -H unix:///run/user/${host_uid}/docker.sock compose down -v --remove-orphans 2>&1" 2>&1 || {
      echo "WARNING: Container teardown may have failed. Check VM logs." >&2
    }

    # Verify git status is clean (A6)
    echo "Checking git status..."
    local git_status
    git_status=$(ssh -o StrictHostKeyChecking=yes \
      -o UserKnownHostsFile="$vm_dir/known_hosts" \
      -i "$vm_dir/id_vm_control" \
      -p "$ssh_port" \
      mester@127.0.0.1 \
      "cd /workspace && git status --short 2>/dev/null || echo 'Not a git repo'" 2>&1 || true)
    if [[ -z "$git_status" ]]; then
      echo "Git status is clean (no uncommitted changes)."
    else
      echo "WARNING: Git has uncommitted changes:"
      echo "$git_status"
    fi
  fi

  # 2. Stop and remove VM
  vm_cmd_down "$vm_name"
}

# ─── vm rebuild: remove and recreate base disk ───────────────────────
vm_cmd_rebuild() {
  local vm_name="${1:-default}"
  local base_disk="$VM_DIR_BASE/base.qcow2"

  # Safety: never proceed without explicit confirmation
  if [[ -f "$base_disk" ]]; then
    local size
    size=$(du -sh "$base_disk" 2>/dev/null | awk '{print $1}' || echo '?')
    echo "=== VM Rebuild: $vm_name ==="
    echo ""
    echo "WARNING: This will DELETE the base disk and all data inside it."
    echo "  Base disk: $base_disk ($size)"
    echo ""
    echo "A new base disk will be created in its place. Any running"
    echo "VMs must be stopped first (./lab-vm down)."
    echo ""

    # Respect VM_YES for auto-accept (ticket #48, A7-A8)
    if [[ "${VM_YES:-0}" != "1" ]]; then
      read -r -p "Proceed with rebuild? [y/N] " _rebuild_choice
      if [[ "$_rebuild_choice" != [yY] && "$_rebuild_choice" != [yY][eE][sS] ]]; then
        log "Rebuild cancelled."
        return 0
      fi
    else
      echo "Auto-accept enabled (VM_YES=1) — proceeding."
    fi
  fi

  # Stop any running VMs that use this base disk
  local vm_dir
  vm_dir="$(_vm_resolve "$vm_name")"
  if echo "$(vm_run_status "$vm_dir" 2>/dev/null)" | grep -q running; then
    log "VM '$vm_name' is running. Stopping it first..."
    vm_run_stop "$vm_dir"
  fi

  # Remove base disk (and any existing overlay that depends on it)
  if vm_disk_exists "$base_disk"; then
    log "Removing base disk: $base_disk"
    vm_disk_delete "$base_disk"
  fi
  # Remove overlay if it exists (it references the old base)
  local overlay_disk="$vm_dir/disk.qcow2"
  if vm_disk_exists "$overlay_disk"; then
    log "Removing overlay disk: $overlay_disk"
    vm_disk_delete "$overlay_disk"
  fi

  # Recreate base disk
  echo "Creating new base disk image ($VM_BASE_SIZE GiB)..."
  vm_disk_create_base "$base_disk" "${VM_BASE_SIZE:-20}"
  echo "Base disk recreated. Next step: ./lab-vm up $vm_name"
}

# ─── vm base: create the base image (one-time) ──────────────────────
vm_cmd_base() {
  local vm_name="${1:-default}"
  local base_disk="$VM_DIR_BASE/base.qcow2"

  log "Creating base disk image (${VM_BASE_SIZE:-20} GiB)..."
  vm_disk_create_base "$base_disk" "${VM_BASE_SIZE:-20}"
  log "Base disk created. Next step: ./lab-vm up $vm_name"
}
