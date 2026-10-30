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

_vm_install_guest_harness() {
  local vm_dir="$1"
  local version="${VM_OPENCODE_VERSION:-latest}"
  local config_hash
  [[ "$version" == latest || "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    log "ERROR: VM_OPENCODE_VERSION must be 'latest' or a pinned semantic version (for example 2.2.0)"
    return 2
  }
  config_hash="$(
    cd "$LAB_DIR/agent/opencode"
    find opencode.json plugins -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | awk '{print $1}'
  )" || return $?

  log "Ensuring direct guest OpenCode harness (requested version: $version)..."
  vm_ssh "$vm_dir" "sudo bash -s -- '$version'" <<'GUEST_OPENCODE_INSTALL'
set -euo pipefail
requested_version="$1"
. /etc/profile.d/mester.sh
export DEBIAN_FRONTEND=noninteractive
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y -qq nodejs npm
fi
current="$(opencode --version 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i ~ /^v?[0-9]+\.[0-9]+\.[0-9]+$/) {sub(/^v/, "", $i); print $i; exit}}' || true)"
if [[ "$requested_version" == latest && -n "$current" ]]; then
  # `latest` is the fresh-install default, not an implicit upgrade channel.
  # Existing guests are upgraded only by the explicit `lab-vm update` command.
  version="$current"
elif [[ "$requested_version" == latest ]]; then
  version="$(npm view @opencode/cli@latest version --json | tr -d '"[:space:]')"
  [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Unexpected OpenCode package version: $version" >&2; exit 1; }
else
  version="$requested_version"
fi
if [[ "$current" != "$version" ]]; then
  sudo env HTTP_PROXY="$HTTP_PROXY" HTTPS_PROXY="$HTTPS_PROXY" \
    http_proxy="$http_proxy" https_proxy="$https_proxy" \
    npm install --global "@opencode/cli@$version"
fi
installed="$(opencode --version | awk '{for (i=1; i<=NF; i++) if ($i ~ /^v?[0-9]+\.[0-9]+\.[0-9]+$/) {sub(/^v/, "", $i); print $i; exit}}')"
test "$installed" = "$version"
install -d -o root -g root -m 0755 /etc/opencode /etc/opencode/plugins
GUEST_OPENCODE_INSTALL

  if ! tar -C "$LAB_DIR/agent/opencode" -cf - opencode.json plugins | \
    vm_ssh "$vm_dir" "sudo tar -xf - -C /etc/opencode"; then
    log "ERROR: failed to transfer guest OpenCode config/plugins"
    return 1
  fi

  vm_ssh "$vm_dir" "sudo sed -i \
    -e 's#http://ollama:11434#http://10.0.2.100:11434#g' \
    -e 's#http://llamacpp:11434#http://10.0.2.100:8080#g' \
    /etc/opencode/opencode.json && \
    sudo chown -R root:root /etc/opencode && \
    sudo chmod -R a+rX /etc/opencode && \
    printf '%s\\n' '$config_hash' | sudo tee /etc/opencode/.little-mester-config-hash >/dev/null && \
    sudo -iu mester opencode --version"
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
install -d -o mester -g mester /home/mester/.config/systemd/user/docker.service.d
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
cat > /home/mester/.config/systemd/user/docker.service.d/proxy.conf <<'DOCKER_SERVICE_PROXY'
[Service]
Environment=HTTP_PROXY=http://10.0.2.100:3128
Environment=HTTPS_PROXY=http://10.0.2.100:3128
Environment=NO_PROXY=localhost,127.0.0.1,10.0.2.100
Environment=DOCKERD_ROOTLESS_ROOTLESSKIT_FLAGS=--cidr=10.0.3.0/24
DOCKER_SERVICE_PROXY
chown mester:mester /home/mester/.config/systemd/user/docker.service.d/proxy.conf
GUEST_DOCKER_PROXY
  vm_ssh "$vm_dir" "sudo -iu mester env XDG_RUNTIME_DIR=/run/user/${uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus systemctl --user daemon-reload && sudo -iu mester env XDG_RUNTIME_DIR=/run/user/${uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus systemctl --user enable docker && sudo -iu mester env XDG_RUNTIME_DIR=/run/user/${uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus systemctl --user restart docker"
}

# ─── vm up: provision and start VM ──────────────────────────────────
vm_cmd_up() {
  local vm_name="${1:-default}"
  local workspace="${2:-}"
  local vm_dir running_workspace virtiofsd_pid qemu_pid qemu_args
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
    qemu_pid="$(cat "$vm_dir/qemu.pid" 2>/dev/null || true)"
    qemu_args="$(_vm_read_qemu_process_args "$qemu_pid")" || die "Cannot verify the QEMU process for VM '$vm_name'; refusing to reuse or switch it."
    [[ "$qemu_args" == *qemu-system-x86_64* ]] || die "The recorded VM '$vm_name' PID is not QEMU; refusing to signal it."
    virtiofsd_pid="$(cat "$vm_dir/virtiofsd.pid" 2>/dev/null || true)"
    running_workspace="$(_vm_read_virtiofsd_shared_dir "$virtiofsd_pid")" || die "Cannot verify the workspace mounted by VM '$vm_name'; refusing to reuse or switch it."
    running_workspace="$(realpath "$running_workspace")" || die "Cannot resolve the workspace mounted by VM '$vm_name'."
    if [[ "$running_workspace" == "$workspace" ]]; then
      echo "VM '$vm_name' is already running with workspace: $workspace"
      echo "$status"
      _vm_install_guest_harness "$vm_dir"
      return 0
    fi

    echo "VM '$vm_name' is currently mounted on: $running_workspace"
    echo "Requested workspace: $workspace"
    _vm_confirm_workspace_switch "$running_workspace" "$workspace" || die 'Workspace switch cancelled; the running VM was not changed.'
    _vm_shutdown_for_workspace_switch "$vm_dir" || die 'Guest shutdown did not complete; the VM and current workspace were left in place.'
    echo "Guest stopped cleanly. Reusing the existing VM overlay for the new workspace."
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

  # Install the harness in the guest OS. Docker remains available for project
  # workloads; it is not the wrapper or security boundary around the agent.
  _vm_install_guest_harness "$vm_dir" || return $?

  VM_STARTING=0

  echo ""
  echo "VM '$vm_name' is up and running."
  echo "  Workspace: $workspace"
  echo "  SSH:  ./lab-vm ssh $vm_name"
  if [[ "${VM_LAUNCH_AFTER_UP:-0}" == 1 ]]; then
    echo "  Starting OpenCode in the guest now."
  else
    echo "  Agent: ./lab-vm agent $vm_name"
  fi
  echo "  Stop: ./lab-vm down $vm_name"
  echo ""

  # A8: Confirm host no longer runs the agent container
  echo "Host Docker is not running the agent container in VM mode."
  echo "All agent execution is inside the VM."
}

_vm_confirm_workspace_switch() {
  local old_workspace="$1" new_workspace="$2" answer
  if [[ "${VM_YES:-0}" == 1 ]]; then
    echo "Auto-accept enabled: stopping the guest to switch its single workspace mount."
    return 0
  fi
  if [[ ! -t 0 ]]; then
    echo "REFUSED: switching a running VM to another workspace requires an interactive confirmation (or --yes)." >&2
    return 1
  fi
  echo "Switching workspaces will shut down the guest and stop its running processes/workloads."
  echo "The VM overlay, installed tools, and control keys are preserved; neither host workspace is deleted."
  read -r -p "Switch from '$old_workspace' to '$new_workspace'? [y/N] " answer
  [[ "$answer" == [yY] || "$answer" == [yY][eE][sS] ]]
}

_vm_shutdown_for_workspace_switch() {
  local vm_dir="$1" timeout="${VM_WORKSPACE_SWITCH_TIMEOUT:-60}" deadline status
  [[ "$timeout" =~ ^[1-9][0-9]{0,2}$ ]] || {
    echo 'REFUSED: VM_WORKSPACE_SWITCH_TIMEOUT must be an integer from 1 to 999 seconds.' >&2
    return 2
  }
  echo "Requesting a clean guest shutdown before changing the virtiofs workspace."
  # Graceful guest poweroff first. An SSH disconnect during shutdown is normal;
  # the VM process status below decides whether it completed.
  vm_ssh "$vm_dir" 'sudo systemctl poweroff' >/dev/null 2>&1 || true
  deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    status="$(vm_run_status "$vm_dir" 2>/dev/null)" || status="stopped"
    if ! grep -q 'running' <<<"$status"; then
      # Remove only stale host-side processes/sockets. This does not delete the
      # overlay, seed, SSH keys, or either host workspace.
      vm_run_stop "$vm_dir"
      return 0
    fi
    sleep 1
  done
  echo "Timed out waiting for the guest to power off; leaving the running VM and its current workspace untouched." >&2
  return 1
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

# OpenCode 2.2.0 chooses an ephemeral guest-loopback callback port at runtime.
# The authenticated host browser bridge reads only that port from the approved
# authorize URL and asks this SSH control master to forward that exact port.
_vm_validate_auth_callback_port() {
  local port="${1:-}" port_num
  [[ "$port" =~ ^[0-9]{1,5}$ ]] || return 1
  port_num=$((10#$port))
  (( port_num >= 1024 && port_num <= 65535 ))
}

_vm_openai_callback_ssh_args() {
  local port="${1:-}" port_num
  _vm_validate_auth_callback_port "$port" || return 2
  port_num=$((10#$port))
  printf '%s\0' \
    -o ExitOnForwardFailure=yes \
    -L "127.0.0.1:${port_num}:127.0.0.1:${port_num}"
}

_vm_openai_browser_bridge_ssh_args() {
  local guest_port="${1:-}" host_port="${2:-}"
  [[ "$guest_port" =~ ^[0-9]+$ && "$host_port" =~ ^[0-9]+$ ]] || return 2
  (( guest_port >= 1024 && guest_port <= 65535 && host_port >= 1024 && host_port <= 65535 )) || return 2
  printf '%s\0' \
    -o ExitOnForwardFailure=yes \
    -R "127.0.0.1:${guest_port}:127.0.0.1:${host_port}"
}

_vm_browser_auth_enabled() {
  case "${1:-auto}" in
    on) return 0 ;;
    off) return 1 ;;
    auto)
      # The guest agent marks its environment so nested lab-vm launches
      # default to no host-browser bridge. --browser-auth explicitly overrides.
      [[ "${LAB_VM_RECURSION_DEPTH:-0}" =~ ^0+$ ]]
      ;;
    *) return 2 ;;
  esac
}

vm_cmd_auth_forward() {
  local vm_name=default callback_port="" vm_dir
  local -a forward_args=()
  case "$#" in
    1) callback_port="$1" ;;
    2) vm_name="$1"; callback_port="$2" ;;
    *) die 'Usage: ./lab-vm auth-forward [name] <callback-port>' ;;
  esac
  _vm_validate_auth_callback_port "$callback_port" || die 'Usage: ./lab-vm auth-forward [name] <callback-port> (read the port from the redirect_uri locally; do not share the URL).'
  vm_dir="$(_vm_resolve "$vm_name")"
  [[ -d "$vm_dir" ]] || die "No VM profile found at: $vm_dir"
  if ! echo "$(vm_run_status "$vm_dir" 2>/dev/null)" | grep -q running; then
    die "VM '$vm_name' is not running. Start it with: ./lab-vm up $vm_name"
  fi

  mapfile -d '' -t forward_args < <(_vm_openai_callback_ssh_args "$callback_port")
  echo "Forwarding OpenAI browser callback port $callback_port on host loopback to guest loopback."
  echo 'Keep this command running during /connect; press Ctrl-C after success or cancellation to close the tunnel.'
  ssh -F /dev/null -N -o BatchMode=yes -o ConnectTimeout=10 \
    -o IdentitiesOnly=yes -o IdentityAgent=none \
    -o ForwardAgent=no -o ForwardX11=no \
    -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$vm_dir/known_hosts" \
    -i "$vm_dir/id_vm_control" -p "$(cat "$vm_dir/ssh_port")" \
    "${forward_args[@]}" mester@127.0.0.1
}

_vm_browser_bridge_pid_matches() {
  local pid="$1" expected_script="$2"
  [[ "$pid" =~ ^[1-9][0-9]*$ && -r "/proc/$pid/cmdline" ]] || return 1
  python3 - "$pid" "$expected_script" <<'PY'
import os
import sys

pid, expected_script = sys.argv[1:]
try:
    argv = open(f"/proc/{pid}/cmdline", "rb").read().decode().rstrip("\0").split("\0")
except OSError:
    raise SystemExit(1)
if not any(os.path.realpath(arg) == os.path.realpath(expected_script) for arg in argv):
    raise SystemExit(1)
PY
}

_vm_browser_bridge_health() {
  local port="${1:-}"
  [[ "$port" =~ ^[0-9]+$ ]] && (( port >= 1 && port <= 65535 )) || return 1
  python3 - "$port" <<'PY'
import urllib.request
import sys
try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{sys.argv[1]}/health", timeout=0.5) as response:
        raise SystemExit(0 if response.status == 200 else 1)
except Exception:
    raise SystemExit(1)
PY
}

_vm_auth_tunnel_pid_matches() {
  local pid="${1:-}" guest_port="${2:-}" host_port="${3:-}"
  [[ "$guest_port" =~ ^[0-9]+$ && "$host_port" =~ ^[0-9]+$ ]] || return 1
  [[ "$pid" =~ ^[1-9][0-9]*$ && -r "/proc/$pid/cmdline" ]] || return 1
  python3 - "$pid" "$guest_port" "$host_port" <<'PY'
import os
import sys
try:
    argv = open(f"/proc/{sys.argv[1]}/cmdline", "rb").read().decode().rstrip("\0").split("\0")
except OSError:
    raise SystemExit(1)
if not argv or os.path.basename(argv[0]) != "ssh":
    raise SystemExit(1)
expected = f"127.0.0.1:{sys.argv[2]}:127.0.0.1:{sys.argv[3]}"
if expected not in argv:
    raise SystemExit(1)
PY
}

_vm_auth_tunnel_debug_args() {
  # OpenSSH verbose output reports listener/forward connection failures, but
  # never the forwarded HTTP request payload that carries OAuth material.
  if [[ "${VM_DEBUG:-0}" -ge 1 ]]; then
    printf '%s\n' -vv
  fi
}

vm_cmd_agent_with_browser_auth() (
  local vm_name="${1:-default}" vm_dir bridge_script host_bridge_dir="" bridge_pid="" tunnel_pid="" guest_browser_dir="" bridge_token="" tmp_root control_path ssh_port bridge_log ssh_log bridge_port guest_bridge_port session_id
  local -a forward_args=() ssh_debug_args=()
  vm_dir="$(_vm_resolve "$vm_name")"
  [[ -d "$vm_dir" ]] || die "No VM profile found at: $vm_dir"
  if ! echo "$(vm_run_status "$vm_dir" 2>/dev/null)" | grep -q running; then
    die "VM '$vm_name' is not running. Start it with: ./lab-vm up $vm_name"
  fi
  bridge_script="$LAB_DIR/lib/vm/browser-bridge-server.py"
  command -v python3 >/dev/null 2>&1 || die 'The host browser MCP bridge requires Python 3.'
  [[ -f "$bridge_script" ]] || die 'The host browser MCP bridge source is missing.'

  _vm_auth_cleanup() {
    local cleanup_dir="${guest_browser_dir:-}"
    if [[ "$cleanup_dir" =~ ^/tmp/lab-vm-browser\.[A-Za-z0-9]+$ ]]; then
      vm_ssh "$vm_dir" "rm -rf -- '$cleanup_dir'" </dev/null >/dev/null 2>&1 || true
    fi
    if [[ -n "${tunnel_pid:-}" ]] && _vm_auth_tunnel_pid_matches "$tunnel_pid" "${guest_bridge_port:-}" "${bridge_port:-}"; then
      kill -TERM "$tunnel_pid" 2>/dev/null || true
      for _ in $(seq 1 20); do
        _vm_auth_tunnel_pid_matches "$tunnel_pid" "${guest_bridge_port:-}" "${bridge_port:-}" || break
        sleep 0.1
      done
      _vm_auth_tunnel_pid_matches "$tunnel_pid" "${guest_bridge_port:-}" "${bridge_port:-}" && kill -KILL "$tunnel_pid" 2>/dev/null || true
      wait "$tunnel_pid" 2>/dev/null || true
    fi
    if [[ -n "${bridge_pid:-}" ]] && _vm_browser_bridge_pid_matches "$bridge_pid" "$bridge_script"; then
      kill -TERM "$bridge_pid" 2>/dev/null || true
      for _ in $(seq 1 30); do
        _vm_browser_bridge_pid_matches "$bridge_pid" "$bridge_script" || break
        sleep 0.1
      done
      _vm_browser_bridge_pid_matches "$bridge_pid" "$bridge_script" && kill -KILL "$bridge_pid" 2>/dev/null || true
      wait "$bridge_pid" 2>/dev/null || true
    fi
    if [[ -n "${host_bridge_dir:-}" && -d "$host_bridge_dir" && -O "$host_bridge_dir" ]]; then
      rm -rf -- "$host_bridge_dir"
    fi
  }
  trap _vm_auth_cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM HUP

  bridge_token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" || die 'Could not create the temporary browser bridge credential.'
  mkdir -p "$vm_dir"
  tmp_root="${XDG_RUNTIME_DIR:-/tmp}"
  [[ -d "$tmp_root" && -O "$tmp_root" ]] || tmp_root=/tmp
  host_bridge_dir="$(mktemp -d "$tmp_root/lab-vm-browser-host.XXXXXX")" || die 'Could not create a private host runtime directory for the browser bridge.'
  session_id="${host_bridge_dir##*/}"
  chmod 700 "$host_bridge_dir"
  control_path="$host_bridge_dir/ssh-control"
  (( ${#control_path} < 100 )) || die 'Private SSH control socket path is too long; set a shorter XDG_RUNTIME_DIR and retry.'
  bridge_script="$host_bridge_dir/browser-bridge-server.py"
  bridge_log="$vm_dir/browser-bridge-${session_id}.log"
  ssh_log="$vm_dir/browser-bridge-ssh-${session_id}.log"
  install -m 700 "$LAB_DIR/lib/vm/browser-bridge-server.py" "$bridge_script" || die 'Could not stage the host browser bridge into private runtime storage.'
  VM_BROWSER_BRIDGE_TOKEN="$bridge_token" VM_BROWSER_BRIDGE_PORT=0 \
    VM_BROWSER_BRIDGE_SSH_CONTROL_PATH="$control_path" \
    VM_BROWSER_BRIDGE_SSH_PORT="$(cat "$vm_dir/ssh_port")" \
    VM_BROWSER_BRIDGE_SSH_LOG="$ssh_log" \
    VM_BROWSER_BRIDGE_SSH_DEBUG="${VM_DEBUG:-0}" \
    python3 "$bridge_script" </dev/null >>"$bridge_log" 2>&1 &
  bridge_pid=$!

  bridge_port=""
  for _ in $(seq 1 50); do
    kill -0 "$bridge_pid" 2>/dev/null || {
      die "Host browser MCP bridge failed to start; inspect $bridge_log (OAuth URLs and tokens are not logged)."
    }
    bridge_port="$(sed -nE 's/^Host browser MCP bridge listening on 127\.0\.0\.1:([0-9]+)$/\1/p' "$bridge_log" | tail -n 1)"
    if [[ "$bridge_port" =~ ^[0-9]+$ ]] && (( bridge_port >= 1024 && bridge_port <= 65535 )); then
      _vm_browser_bridge_health "$bridge_port" && break
    fi
    sleep 0.1
  done
  [[ "$bridge_port" =~ ^[0-9]+$ ]] && (( bridge_port >= 1024 && bridge_port <= 65535 )) || die "Host browser MCP bridge did not report a valid dynamic loopback port; inspect $bridge_log."
  _vm_browser_bridge_health "$bridge_port" || die "Host browser MCP bridge did not become ready on its dynamic port; inspect $bridge_log."

  guest_bridge_port="$(vm_ssh "$vm_dir" "python3 -c 'import socket; s=socket.socket(); s.bind((\"127.0.0.1\", 0)); print(s.getsockname()[1]); s.close()'")" || die 'Could not allocate a guest-loopback port for this browser session.'
  [[ "$guest_bridge_port" =~ ^[0-9]+$ ]] && (( guest_bridge_port >= 1024 && guest_bridge_port <= 65535 )) || die 'Guest returned an invalid browser bridge port.'
  guest_browser_dir="$(vm_ssh "$vm_dir" 'umask 077 && mktemp -d /tmp/lab-vm-browser.XXXXXX')" || die 'Could not create temporary guest browser bridge files.'
  [[ "$guest_browser_dir" =~ ^/tmp/lab-vm-browser\.[A-Za-z0-9]+$ ]] || die 'Guest returned an unexpected temporary directory; refusing to write bridge files.'
  vm_ssh "$vm_dir" "install -m 700 /dev/stdin '$guest_browser_dir/xdg-open'" <"$LAB_DIR/lib/vm/browser-open.sh" || die 'Could not install the temporary guest browser opener.'
  vm_ssh "$vm_dir" "install -m 700 /dev/stdin '$guest_browser_dir/browser-open.py'" <"$LAB_DIR/lib/vm/browser-open.py" || die 'Could not install the temporary guest MCP client.'
  vm_ssh "$vm_dir" "ln -s xdg-open '$guest_browser_dir/open' && ln -s xdg-open '$guest_browser_dir/gio'" || die 'Could not prepare supported guest browser opener names.'
  printf '%s' "$bridge_token" | vm_ssh "$vm_dir" "install -m 600 /dev/stdin '$guest_browser_dir/token'" || die 'Could not transfer the temporary bridge credential over SSH stdin.'
  unset bridge_token

  mapfile -d '' -t forward_args < <(_vm_openai_browser_bridge_ssh_args "$guest_bridge_port" "$bridge_port")
  mapfile -t ssh_debug_args < <(_vm_auth_tunnel_debug_args)
  ssh_port="$(cat "$vm_dir/ssh_port")"
  if [[ "${VM_DEBUG:-0}" -ge 1 ]]; then
    log_debug "OpenSSH control-master and callback-forward traces are recorded in $ssh_log; forwarded request data is not logged."
  fi
  ssh "${ssh_debug_args[@]}" -F /dev/null -M -S "$control_path" -N \
    -o ControlPersist=no -o BatchMode=yes -o ConnectTimeout=10 \
    -o IdentitiesOnly=yes -o IdentityAgent=none \
    -o ForwardAgent=no -o ForwardX11=no \
    -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$vm_dir/known_hosts" \
    -i "$vm_dir/id_vm_control" -p "$ssh_port" \
    "${forward_args[@]}" mester@127.0.0.1 \
    </dev/null >>"$ssh_log" 2>&1 &
  tunnel_pid=$!
  for _ in $(seq 1 100); do
    kill -0 "$tunnel_pid" 2>/dev/null || die "The temporary callback/browser SSH forwards failed to start; see $ssh_log."
    if ssh -F /dev/null -S "$control_path" -O check -p "$ssh_port" mester@127.0.0.1 >/dev/null 2>&1; then break; fi
    sleep 0.1
  done
  ssh -F /dev/null -S "$control_path" -O check -p "$ssh_port" mester@127.0.0.1 >/dev/null 2>&1 || die "The temporary SSH control master did not become ready; see $ssh_log."
  _vm_auth_tunnel_pid_matches "$tunnel_pid" "$guest_bridge_port" "$bridge_port" || die 'The temporary SSH control master process could not be verified.'

  echo "Starting guest OpenCode with per-session loopback ports (host $bridge_port, guest $guest_bridge_port) and temporary callback forwarding."
  echo 'In the TUI, use /connect → OpenAI → Sign in with ChatGPT. The host browser opens automatically when OpenCode requests it.'
  echo "The callback still returns through loopback SSH to the guest; exit OpenCode after success or cancellation to close all temporary access. Diagnostics: $ssh_log"
  VM_BROWSER_BRIDGE_DIR="$guest_browser_dir" VM_BROWSER_BRIDGE_PORT="$guest_bridge_port" \
    VM_AGENT_SKIP_MODEL_PROBE=1 vm_cmd_agent "$vm_name"
)

_vm_proxy_pid_matches() {
  local proxy_pid="$1" vm_dir="$2" workspace="$3"
  python3 - "$proxy_pid" "$LAB_DIR/lib/vm/proxy.py" "$vm_dir/proxy_port" "$workspace" <<'PY'
import os
import sys

pid, expected_script, expected_port_file, expected_workspace = sys.argv[1:]
try:
    argv = open(f"/proc/{pid}/cmdline", "rb").read().decode().rstrip("\0").split("\0")
except OSError:
    raise SystemExit(1)
if len(argv) < 2 or os.path.realpath(argv[1]) != os.path.realpath(expected_script):
    raise SystemExit(1)
try:
    index = argv.index("--port-file")
    actual_port_file = argv[index + 1]
except (ValueError, IndexError):
    raise SystemExit(1)
if os.path.realpath(actual_port_file) != os.path.realpath(expected_port_file):
    raise SystemExit(1)
try:
    index = argv.index("--workspace")
    actual_workspace = argv[index + 1]
except (ValueError, IndexError):
    raise SystemExit(1)
if os.path.realpath(actual_workspace) != os.path.realpath(expected_workspace):
    raise SystemExit(1)
PY
}

_vm_proxy_supports_reload() {
  local proxy_pid="$1" vm_dir="$2" port="$3" marker_pid marker_port
  [[ -s "$vm_dir/proxy.reload-ready" ]] || return 1
  read -r marker_pid marker_port <"$vm_dir/proxy.reload-ready" || return 1
  [[ "$marker_pid" == "$proxy_pid" && "$marker_port" == "$port" ]] || return 1
  python3 - "$proxy_pid" "$vm_dir/proxy.reload-ready" <<'PY'
import os
import sys

pid, marker = sys.argv[1:]
try:
    argv = open(f"/proc/{pid}/cmdline", "rb").read().decode().rstrip("\0").split("\0")
    index = argv.index("--reload-marker")
    actual = argv[index + 1]
except (OSError, ValueError, IndexError):
    raise SystemExit(1)
if os.path.realpath(actual) != os.path.realpath(marker):
    raise SystemExit(1)
PY
}

_vm_start_proxy_at() {
  local vm_dir="$1" workspace="$2" port="$3" proxy_pid marker_pid marker_port
  rm -f "$vm_dir/proxy.reload-ready"
  python3 "$LAB_DIR/lib/vm/proxy.py" \
    --workspace "$workspace" \
    --port-file "$vm_dir/proxy_port" \
    --port "$port" \
    --reload-marker "$vm_dir/proxy.reload-ready" \
    </dev/null >>"$vm_dir/proxy.log" 2>&1 &
  proxy_pid=$!
  echo "$proxy_pid" >"$vm_dir/proxy.pid"
  for _ in $(seq 1 50); do
    if [[ -s "$vm_dir/proxy.reload-ready" && -s "$vm_dir/proxy_port" ]]; then
      read -r marker_pid marker_port <"$vm_dir/proxy.reload-ready" || true
      if [[ "$marker_pid" == "$proxy_pid" && "$marker_port" == "$port" && "$(cat "$vm_dir/proxy_port")" == "$port" ]]; then
        return 0
      fi
    fi
    kill -0 "$proxy_pid" 2>/dev/null || break
    sleep 0.1
  done
  log "ERROR: egress proxy did not recover on 127.0.0.1:$port; inspect $vm_dir/proxy.log"
  return 1
}

vm_cmd_egress_reload() {
  local vm_name="${1:-default}" vm_dir proxy_pid proxy_port virtiofsd_pid workspace log_file log_offset reload_result
  vm_dir="$(_vm_resolve "$vm_name")"
  [[ -d "$vm_dir" ]] || die "No VM profile found at: $vm_dir"
  if ! echo "$(vm_run_status "$vm_dir" 2>/dev/null)" | grep -q running; then
    die "VM '$vm_name' is not running. Start it with: ./lab-vm up $vm_name"
  fi
  [[ -s "$vm_dir/proxy_port" ]] || die "VM '$vm_name' has no recorded egress proxy port; refusing to guess a QEMU guestfwd destination."
  proxy_port="$(cat "$vm_dir/proxy_port")"
  [[ "$proxy_port" =~ ^[1-9][0-9]*$ ]] && (( proxy_port <= 65535 )) || die "Invalid egress proxy port recorded for VM '$vm_name'."
  virtiofsd_pid="$(cat "$vm_dir/virtiofsd.pid" 2>/dev/null)"
  workspace="$(_vm_read_virtiofsd_shared_dir "$virtiofsd_pid")" || die 'Cannot verify the running VM workspace export; refusing to reload proxy policy.'
  workspace="$(realpath "$workspace")" || die 'Cannot resolve the VM workspace path.'
  [[ -d "$workspace" ]] || die 'The VM workspace export is no longer available.'

  proxy_pid="$(cat "$vm_dir/proxy.pid" 2>/dev/null || true)"
  if [[ -n "$proxy_pid" && ! "$proxy_pid" =~ ^[1-9][0-9]*$ ]]; then
    die "Invalid egress proxy PID recorded for VM '$vm_name'."
  fi

  if [[ -n "$proxy_pid" ]] && kill -0 "$proxy_pid" 2>/dev/null; then
    _vm_proxy_pid_matches "$proxy_pid" "$vm_dir" "$workspace" || die "The recorded PID is not this VM's egress proxy; refusing to signal it."
    if _vm_proxy_supports_reload "$proxy_pid" "$vm_dir" "$proxy_port"; then
      log_file="$vm_dir/proxy.log"
      [[ -f "$log_file" ]] || die "Egress proxy log is missing; cannot confirm policy reload."
      log_offset="$(wc -c <"$log_file")"
      kill -HUP "$proxy_pid" || die "Could not signal the reload-capable egress proxy."
      reload_result=""
      for _ in $(seq 1 20); do
        reload_result="$(tail -c +$((log_offset + 1)) "$log_file" 2>/dev/null | grep -E 'egress policy reload failed|egress policy reloaded' | tail -n 1 || true)"
        [[ -n "$reload_result" ]] && break
        sleep 0.05
      done
      [[ "$reload_result" == *'egress policy reloaded'* ]] || die "Egress policy reload was not confirmed; inspect $log_file. The proxy was not restarted."
      echo "Egress policy reloaded for VM '$vm_name' on its existing loopback listener; QEMU and the overlay were not restarted."
      return 0
    fi

    echo "The recorded proxy predates safe in-place reload; replacing only that host proxy on 127.0.0.1:$proxy_port. Existing proxy connections may briefly drop."
    kill -TERM "$proxy_pid" 2>/dev/null || die 'Could not stop the verified older egress proxy safely.'
    for _ in $(seq 1 20); do
      kill -0 "$proxy_pid" 2>/dev/null || break
      sleep 0.1
    done
    kill -0 "$proxy_pid" 2>/dev/null && die 'Older egress proxy did not exit; leaving QEMU and the overlay untouched.'
  else
    echo "Recorded egress proxy is not running; restoring it on QEMU's existing loopback port 127.0.0.1:$proxy_port."
  fi

  _vm_start_proxy_at "$vm_dir" "$workspace" "$proxy_port" || die "Could not restore the host egress proxy; QEMU and the overlay remain running. Inspect $vm_dir/proxy.log."
  echo "Egress proxy is ready on the existing loopback port 127.0.0.1:$proxy_port with the current workspace policy; QEMU and the overlay were not restarted."
}

_vm_validate_qemu_isolation_args() {
  local process_args="$1" ssh_port="$2" guestfwd_count hostfwd_count netdev_count net_device_count
  [[ "$process_args" == *'restrict=on'* ]] || {
    echo 'QEMU network is missing restrict=on.' >&2
    return 1
  }
  guestfwd_count="$(grep -o 'guestfwd=' <<<"$process_args" | wc -l)"
  [[ "$guestfwd_count" -eq 4 ]] || {
    echo "Expected exactly four explicit guestfwd channels; found $guestfwd_count." >&2
    return 1
  }
  hostfwd_count="$(grep -o 'hostfwd=' <<<"$process_args" | wc -l)"
  [[ "$hostfwd_count" -eq 1 && "$process_args" == *"hostfwd=tcp:127.0.0.1:${ssh_port}-:22"* ]] || {
    echo 'Expected exactly one SSH hostfwd bound to 127.0.0.1.' >&2
    return 1
  }
  netdev_count="$(grep -o -- '-netdev' <<<"$process_args" | wc -l)"
  net_device_count="$(grep -o -- '-device virtio-net-pci,netdev=net0' <<<"$process_args" | wc -l)"
  [[ "$netdev_count" -eq 1 && "$net_device_count" -eq 1 && "$process_args" == *'-netdev user,id=net0,'* ]] || {
    echo 'Expected exactly one QEMU user-mode network device.' >&2
    return 1
  }
  [[ "$process_args" == *'-device vhost-user-fs-pci,'* && "$process_args" == *'tag=workspace'* ]] || {
    echo 'QEMU workspace share is missing the reviewed virtiofs workspace device.' >&2
    return 1
  }
  [[ "$process_args" == *'guestfwd=tcp:10.0.2.100:11434-cmd:socat - TCP:127.0.0.1:11434'* &&
     "$process_args" == *'guestfwd=tcp:10.0.2.100:8080-cmd:socat - TCP:127.0.0.1:8080'* &&
     "$process_args" == *'guestfwd=tcp:10.0.2.100:19532-cmd:socat - TCP:127.0.0.1:19532'* &&
     "$process_args" == *'guestfwd=tcp:10.0.2.100:3128-cmd:socat - TCP:127.0.0.1:'* ]] || {
    echo 'QEMU guestfwd channels do not match the reviewed loopback-only allowlist.' >&2
    return 1
  }
}

_vm_read_qemu_process_args() {
  local qemu_pid="$1"
  [[ "$qemu_pid" =~ ^[0-9]+$ && -r "/proc/$qemu_pid/cmdline" ]] || return 1
  tr '\0' ' ' <"/proc/$qemu_pid/cmdline"
}

_vm_read_virtiofsd_shared_dir() {
  local virtiofsd_pid="$1"
  python3 - "$virtiofsd_pid" <<'PY'
import os
import sys

pid = sys.argv[1]
try:
    argv = open(f"/proc/{pid}/cmdline", "rb").read().decode().rstrip("\0").split("\0")
except OSError:
    raise SystemExit(1)
if not argv or os.path.basename(argv[0]) != "virtiofsd":
    raise SystemExit(1)
try:
    index = argv.index("--shared-dir")
    shared_dir = argv[index + 1]
except (ValueError, IndexError):
    raise SystemExit(1)
print(shared_dir)
PY
}

vm_cmd_verify_isolation() {
  local vm_name="${1:-default}" vm_dir ssh_port qemu_pid process_args virtiofsd_pid shared_dir sensitive_path
  vm_dir="$(_vm_resolve "$vm_name")"
  [[ -d "$vm_dir" ]] || die "No VM profile found at: $vm_dir"
  if ! echo "$(vm_run_status "$vm_dir" 2>/dev/null)" | grep -q running; then
    die "VM '$vm_name' is not running. Start it with: ./lab-vm up $vm_name"
  fi
  ssh_port="$(cat "$vm_dir/ssh_port" 2>/dev/null)"
  qemu_pid="$(cat "$vm_dir/qemu.pid" 2>/dev/null)"
  process_args="$(_vm_read_qemu_process_args "$qemu_pid")" || die 'Cannot inspect the running QEMU process command line.'
  [[ "$process_args" == *qemu-system-x86_64* ]] || die 'The recorded PID is not a QEMU x86_64 process.'
  _vm_validate_qemu_isolation_args "$process_args" "$ssh_port" || die 'QEMU network configuration failed the isolation check.'
  virtiofsd_pid="$(cat "$vm_dir/virtiofsd.pid" 2>/dev/null)"
  shared_dir="$(_vm_read_virtiofsd_shared_dir "$virtiofsd_pid")" || die 'Cannot inspect the running virtiofsd workspace export.'
  shared_dir="$(realpath "$shared_dir")" || die 'Cannot resolve the virtiofsd shared directory.'
  [[ -d "$shared_dir" && "$shared_dir" != '/' && "$shared_dir" != "$HOME" ]] || die "Unsafe or missing virtiofsd shared directory: $shared_dir"
  for sensitive_path in "$HOME/.ssh" "$HOME/.gnupg" "$HOME/.config" "$HOME/.aws" "$HOME/.kube" "$HOME/.local" "$HOME/.bashrc" "$HOME/.zshrc"; do
    if [[ "$shared_dir" == "$sensitive_path" || "$shared_dir" == "$sensitive_path"/* || "$sensitive_path" == "$shared_dir"/* ]]; then
      die "virtiofsd export overlaps a sensitive host path: $sensitive_path"
    fi
  done

  echo "QEMU configuration: restrict=on; SSH bound to 127.0.0.1:$ssh_port; only the four reviewed guestfwd channels are configured."
  echo "Host workspace export: $shared_dir (the guest can read/write this selected directory)."
  echo 'Running network and approved-channel probes inside the guest (no VM or host settings will be changed).'
  vm_ssh "$vm_dir" 'python3 -' <"$LAB_DIR/scripts/vm-guest-isolation-check.py"
}

vm_cmd_agent() {
  local vm_name="${1:-default}"
  local vm_dir
  local started_at elapsed probe_options probe_command probe_status metrics_url
  vm_dir="$(_vm_resolve "$vm_name")"
  log_verbose "Launching guest agent for VM '$vm_name'."
  log_debug "VM directory: $vm_dir"
  [[ -d "$vm_dir" ]] || die "No VM profile found at: $vm_dir"
  if ! echo "$(vm_run_status "$vm_dir" 2>/dev/null)" | grep -q running; then
    die "VM '$vm_name' is not running. Start it with: ./lab-vm up $vm_name"
  fi

  # Metrics are optional to the agent runtime. Start the shared curated host
  # service and check this guest's forward, but never block OpenCode on metrics;
  # the TUI reports transport failure and continues polling if this path is down.
  metrics_url="http://127.0.0.1:1"
  if (vm_cmd_metrics up); then
    metrics_url="http://10.0.2.100:19532"
    if ! _vm_guest_metrics_preflight "$vm_dir"; then
      echo "WARNING: VM host metrics are not ready through this guest's 10.0.2.100:19532 forward; OpenCode will start and the TUI will report/retry that guestfwd. See './lab-vm metrics status'." >&2
    fi
  else
    echo "WARNING: VM host metrics service could not be started; OpenCode will start with metrics disabled for this session. See './lab-vm metrics status'." >&2
  fi

  local version="${VM_OPENCODE_VERSION:-latest}"
  local installed_version
  local model_provider model_url
  log_verbose "Checking guest OpenCode version and configuration."
  log_debug "Expected OpenCode version: $version"
  installed_version="$(vm_ssh "$vm_dir" "opencode --version" | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1)"
  log_debug "Guest OpenCode version: ${installed_version:-unavailable}"
  if [[ ! "$installed_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    die "OpenCode is not installed in VM '$vm_name' (found ${installed_version:-none}). Run ./lab-vm up $vm_name to install it."
  fi
  if [[ "$version" != latest && "$installed_version" != "$version" ]]; then
    die "OpenCode $version is not installed in VM '$vm_name' (found ${installed_version:-none}). Run ./lab-vm up $vm_name to install/update it."
  fi
  if ! vm_ssh "$vm_dir" "test -r /etc/opencode/opencode.json && test -r /etc/opencode/plugins/host-metrics-tui/package.json && test -r /etc/opencode/plugins/little-mester-exit-confirm/package.json && test -r /etc/opencode/plugins/little-mester-logo/package.json"; then
    die "OpenCode guest configuration is missing in VM '$vm_name'. Run ./lab-vm up $vm_name to provision it."
  fi
  model_provider="$(vm_ssh "$vm_dir" "python3 -c 'import json; print(json.load(open(\"/etc/opencode/opencode.json\"))[\"model\"].split(\"/\", 1)[0])'")"
  case "$model_provider" in
    llamacpp) model_url="http://10.0.2.100:8080/health" ;;
    ollama) model_url="http://10.0.2.100:11434/api/tags" ;;
    *) die "OpenCode model provider '$model_provider' has no configured VM host forwarder." ;;
  esac
  if [[ "${VM_AGENT_SKIP_MODEL_PROBE:-0}" != 1 ]]; then
    log_verbose "Checking $model_provider API reachability from the guest at $model_url."
    probe_options="--fail --silent --show-error"
    if [[ "$VM_DEBUG" -ge 1 ]]; then
      probe_options="--fail --show-error --verbose --http1.1"
    fi
    probe_command="curl --noproxy 10.0.2.100 $probe_options --max-time 5 '$model_url' >/dev/null"
    log_debug "Guest model probe: $probe_command"
    started_at=$SECONDS
    if vm_ssh "$vm_dir" "$probe_command"; then
      elapsed=$((SECONDS - started_at))
      log_debug "Guest model probe succeeded in ${elapsed}s."
    else
      probe_status=$?
      elapsed=$((SECONDS - started_at))
      log_debug "Guest model probe failed after ${elapsed}s (ssh/curl status=$probe_status)."
      die "$model_provider API is not reachable from VM '$vm_name' at $model_url. Check the host forwarder with './lab-vm models status'; start it with './lab-vm models up $model_provider'. Metrics are managed separately with './lab-vm metrics status'."
    fi
  else
    log_verbose 'Skipping model API health probe for the browser-only authentication flow.'
  fi
  echo "Launching OpenCode $version directly in VM '$vm_name' at /workspace."
  local cli_config cli_config_quoted
  cli_config='{"plugins":["/etc/opencode/plugins/little-mester-logo","/etc/opencode/plugins/host-metrics-tui","/etc/opencode/plugins/little-mester-exit-confirm"],"keybinds":{"app.exit":"none","prompt.clear":"none"}}'
  printf -v cli_config_quoted '%q' "$cli_config"
  local remote_command browser_dir_quoted
  if [[ -n "${VM_BROWSER_BRIDGE_DIR:-}" ]]; then
    local browser_bridge_port_quoted browser_bridge_port_num
    [[ "${VM_BROWSER_BRIDGE_PORT:-}" =~ ^[0-9]+$ ]] || die 'Browser-auth session has no valid guest bridge port.'
    browser_bridge_port_num=$((10#$VM_BROWSER_BRIDGE_PORT))
    (( browser_bridge_port_num >= 1024 && browser_bridge_port_num <= 65535 )) || die 'Browser-auth session has no valid guest bridge port.'
    printf -v browser_dir_quoted '%q' "$VM_BROWSER_BRIDGE_DIR"
    printf -v browser_bridge_port_quoted '%q' "$browser_bridge_port_num"
    remote_command="set -eu; browser_dir=$browser_dir_quoted; cleanup(){ rm -rf -- \"\$browser_dir\"; }; trap cleanup EXIT; trap 'exit 130' INT; trap 'exit 143' HUP TERM; export PATH=\"\$browser_dir:\$PATH\" VM_BROWSER_BRIDGE_TOKEN_FILE=\"\$browser_dir/token\" VM_BROWSER_BRIDGE_PORT=$browser_bridge_port_quoted LAB_VM_RECURSION_DEPTH=1; cd /workspace && . /etc/profile.d/mester.sh && export OPENCODE_CONFIG=/etc/opencode/opencode.json METRICS_URL=$metrics_url && export OPENCODE_CLI_CONFIG_CONTENT=$cli_config_quoted && opencode"
  else
    remote_command="cd /workspace && . /etc/profile.d/mester.sh && export LAB_VM_RECURSION_DEPTH=1 OPENCODE_CONFIG=/etc/opencode/opencode.json METRICS_URL=$metrics_url && export OPENCODE_CLI_CONFIG_CONTENT=$cli_config_quoted && exec opencode"
  fi
  ssh -t \
    -o BatchMode=yes -o ConnectTimeout=10 \
    -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$vm_dir/known_hosts" \
    -i "$vm_dir/id_vm_control" -p "$(cat "$vm_dir/ssh_port")" \
    mester@127.0.0.1 \
    "$remote_command"
}

_vm_guest_metrics_preflight() {
  local vm_dir="$1"
  vm_ssh "$vm_dir" 'python3 -' <<'PY'
import json
import sys
import time
import urllib.request

base = "http://10.0.2.100:19532"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
deadline = time.monotonic() + 10
last_error = "no response"
while time.monotonic() < deadline:
    try:
        bodies = []
        for path in ("/summary", "/metrics", "/history?n=2"):
            with opener.open(base + path, timeout=4) as response:
                bodies.append(response.read())
        metrics = json.loads(bodies[1])
        history = json.loads(bodies[2])
        if not isinstance(metrics, dict) or not isinstance(history, list):
            raise ValueError("unexpected metrics response shape")
        print("guest metrics forward healthy: summary, metrics, history")
        raise SystemExit(0)
    except Exception as exc:
        last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)

print(f"guest metrics forward did not become ready: {last_error}", file=sys.stderr)
raise SystemExit(1)
PY
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
