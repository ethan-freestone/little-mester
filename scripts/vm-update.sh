#!/usr/bin/env bash
# Check and update guest-managed software without recreating VM state.
set -euo pipefail

LAB_DIR="$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")/.." && pwd)"
VM_DIR_BASE="${VM_DIR_BASE:-$LAB_DIR/.mester/vm}"
vm_name="${1:-default}"
shift || true

die() { echo "REFUSED: $*" >&2; exit 1; }
log_verbose() {
  if [[ "${VM_VERBOSE:-0}" -ge 1 ]]; then echo "[verbose] $*"; fi
}
log_debug() {
  if [[ "${VM_DEBUG:-0}" -ge 1 ]]; then echo "[debug] $(date '+%H:%M:%S') $*"; fi
}

[[ "$vm_name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || die 'Invalid VM profile name.'

all=0
targets=()
while (($#)); do
  case "$1" in
    --all) all=1 ;;
    opencode|config|tools|docker|system) targets+=("$1") ;;
    *) die "Unknown update target '$1'. Choose opencode, config, tools, docker, system, or --all." ;;
  esac
  shift
done

if (( all )); then
  ((${#targets[@]} == 0)) || die 'Do not combine --all with individual update targets.'
  targets=(opencode config tools system)
elif ((${#targets[@]} == 0)); then
  # The default is limited to Little Mester-managed harness software. Guest OS
  # and Docker Engine upgrades require the explicit --all or a named target.
  targets=(opencode config tools)
fi

version="${VM_OPENCODE_VERSION:-latest}"
[[ "$version" == latest || "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "VM_OPENCODE_VERSION must be 'latest' or a pinned semantic version."

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

config_hash="$(
  cd "$LAB_DIR/agent/opencode"
  find opencode.json plugins -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | awk '{print $1}'
)" || die 'Could not hash the local OpenCode configuration/plugins.'
[[ "$config_hash" =~ ^[a-f0-9]{64}$ ]] || die 'Generated an invalid OpenCode config hash.'

log_verbose "VM profile directory: $vm_dir"
log_verbose "Checking targets: ${targets[*]}"
log_debug "Requested OpenCode version: $version"

remote_args=("$config_hash" "$version" "${targets[@]}")
printf -v remote_args_quoted '%q ' "${remote_args[@]}"
check_output="$(vm_ssh "$vm_dir" "sudo bash -s -- ${remote_args_quoted% }" <<'GUEST_UPDATE_CHECK'
set -euo pipefail
desired_config_hash="$1"
requested_opencode_version="$2"
shift 2
targets=("$@")

has_target() {
  local target
  for target in "${targets[@]}"; do [[ "$target" == "$1" ]] && return 0; done
  return 1
}

. /etc/profile.d/mester.sh

command -v pgrep >/dev/null 2>&1 || { echo 'REFUSED: pgrep is unavailable; cannot verify whether OpenCode is active.' >&2; exit 3; }
if pgrep -u mester -f '[o]pencode' >/dev/null 2>&1; then
  echo 'REFUSED: OpenCode is running in the guest. Exit the TUI before updating.' >&2
  exit 3
else
  pgrep_status=$?
  if (( pgrep_status != 1 )); then
    echo 'REFUSED: could not verify whether OpenCode is active.' >&2
    exit 3
  fi
fi

needs_apt=0
for target in "${targets[@]}"; do
  case "$target" in tools|docker|system) needs_apt=1 ;; esac
done
if (( needs_apt )); then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
fi

if has_target opencode; then
  current="$(opencode --version 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i ~ /^v?[0-9]+\.[0-9]+\.[0-9]+$/) {sub(/^v/, "", $i); print $i; exit}}' || true)"
  [[ -n "$current" ]] || current=not-installed
  if [[ "$requested_opencode_version" == latest ]]; then
    command -v npm >/dev/null 2>&1 || { echo 'Node/npm is unavailable; run ./lab-vm up first.' >&2; exit 4; }
    candidate="$(npm view @opencode/cli@latest version --json | tr -d '"[:space:]')"
  else
    candidate="$requested_opencode_version"
  fi
  [[ "$candidate" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Unexpected OpenCode package version: $candidate" >&2; exit 4; }
  if [[ "$candidate" != "$current" ]]; then
    printf 'UPDATE  OpenCode CLI  %s -> %s\n' "$current" "$candidate"
  else
    printf 'CURRENT OpenCode CLI  %s\n' "$current"
  fi
fi

installed_version() {
  dpkg-query -W -f='${db:Status-Status}|${Version}' "$1" 2>/dev/null | awk -F'|' '$1 == "installed" {print $2}'
}
check_package_updates() {
  local package="$1" current candidate
  current="$(installed_version "$package" || true)"
  [[ -n "$current" ]] || return 0
  candidate="$(apt-cache policy "$package" | awk '/^[[:space:]]*Candidate:/ {print $2; exit}')"
  [[ -n "$candidate" && "$candidate" != '(none)' ]] || return 0
  if dpkg --compare-versions "$candidate" gt "$current"; then
    printf 'UPDATE  %s  %s -> %s\n' "$package" "$current" "$candidate"
  else
    printf 'CURRENT %s  %s\n' "$package" "$current"
  fi
}

if has_target tools; then
  check_package_updates bats
  check_package_updates shellcheck
fi

if has_target docker; then
  docker_packages=(docker-ce docker-ce-cli containerd.io docker-compose-plugin docker-buildx-plugin docker-ce-rootless-extras)
  for package in "${docker_packages[@]}"; do check_package_updates "$package"; done
fi

if has_target system; then
  upgradable="$(apt-get -s upgrade | awk '$1 == "Inst" {print $2}')"
  if [[ -n "$upgradable" ]]; then
    count="$(wc -l <<<"$upgradable" | tr -d '[:space:]')"
    printf 'UPDATE  Guest OS packages  %s package(s)\n' "$count"
    printf '%s\n' "$upgradable" | sed 's/^/  - /'
  else
    echo 'CURRENT Guest OS packages'
  fi
fi

if has_target config; then
  current_config_hash="$(cat /etc/opencode/.little-mester-config-hash 2>/dev/null || true)"
  if [[ "$current_config_hash" != "$desired_config_hash" ]]; then
    echo 'UPDATE  Little Mester config/plugins  local checkout -> guest'
  else
    echo 'CURRENT Little Mester config/plugins'
  fi
fi

if has_target docker || has_target system; then
  uid="$(id -u mester)"
  docker_socket="/run/user/$uid/docker.sock"
  if [[ -S "$docker_socket" ]]; then
    if ! running_containers="$(runuser -u mester -- env DOCKER_HOST="unix://$docker_socket" docker ps -q 2>/dev/null)"; then
      echo 'REFUSED: could not verify guest Docker workloads; refusing OS/Docker updates.' >&2
      exit 3
    fi
    if [[ -n "$running_containers" ]]; then
      echo 'REFUSED: guest Docker workloads are running. Stop them before updating Docker/OS packages.' >&2
      exit 3
    fi
  fi
fi
GUEST_UPDATE_CHECK
)" || exit $?

if [[ -z "$check_output" ]]; then
  echo 'No update information was returned by the guest.' >&2
  exit 1
fi
printf '%s\n' "$check_output"

if ! grep -q '^UPDATE  ' <<<"$check_output"; then
  echo "No updates available for VM '$vm_name'."
  exit 0
fi

if [[ "${VM_YES:-0}" != 1 ]]; then
  if [[ ! -t 0 ]]; then
    die 'Updates are available, but confirmation requires a terminal. Re-run interactively or pass --yes.'
  fi
  if [[ " ${targets[*]} " == *' system '* || " ${targets[*]} " == *' docker '* ]]; then
    echo 'Guest OS/Docker updates may restart guest services; updates are refused while guest containers are running.'
  fi
  read -r -p "Apply the listed updates to VM '$vm_name'? [y/N] " answer
  [[ "$answer" == [yY] || "$answer" == [yY][eE][sS] ]] || {
    echo 'Update cancelled; the guest packages and config were not changed.'
    exit 0
  }
else
  echo 'Auto-accept enabled: applying the listed guest updates.'
fi

apply_targets=()
for target in "${targets[@]}"; do
  case "$target" in
    opencode)
      grep -q '^UPDATE  OpenCode CLI ' <<<"$check_output" && apply_targets+=(opencode)
      ;;
    config)
      grep -q '^UPDATE  Little Mester config/plugins ' <<<"$check_output" && apply_targets+=(config)
      ;;
    tools)
      grep -Eq '^UPDATE  (bats|shellcheck) ' <<<"$check_output" && apply_targets+=(tools)
      ;;
    docker)
      grep -Eq '^UPDATE  (docker-ce|docker-ce-cli|containerd.io|docker-compose-plugin|docker-buildx-plugin|docker-ce-rootless-extras) ' <<<"$check_output" && apply_targets+=(docker)
      ;;
    system)
      grep -q '^UPDATE  Guest OS packages ' <<<"$check_output" && apply_targets+=(system)
      ;;
  esac
done

apply_version="$version"
if [[ "$version" == latest ]] && grep -q '^UPDATE  OpenCode CLI ' <<<"$check_output"; then
  apply_version="$(sed -nE 's/^UPDATE  OpenCode CLI  [^ ]+ -> ([0-9]+\.[0-9]+\.[0-9]+)$/\1/p' <<<"$check_output" | head -n 1)"
  [[ "$apply_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 'Could not resolve the checked OpenCode update to an exact version.'
fi
apply_args=("$apply_version" "${apply_targets[@]}")
printf -v apply_args_quoted '%q ' "${apply_args[@]}"
if ((${#apply_targets[@]})); then
  vm_ssh "$vm_dir" "sudo bash -s -- ${apply_args_quoted% }" <<'GUEST_UPDATE_APPLY'
set -euo pipefail
requested_opencode_version="$1"
shift
targets=("$@")
. /etc/profile.d/mester.sh
export DEBIAN_FRONTEND=noninteractive

has_target() {
  local target
  for target in "${targets[@]}"; do [[ "$target" == "$1" ]] && return 0; done
  return 1
}

if has_target opencode; then
  echo "Updating OpenCode CLI to $requested_opencode_version..."
  npm install --global "@opencode/cli@$requested_opencode_version"
  installed="$(opencode --version | awk '{for (i=1; i<=NF; i++) if ($i ~ /^v?[0-9]+\.[0-9]+\.[0-9]+$/) {sub(/^v/, "", $i); print $i; exit}}')"
  if [[ ! "$installed" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ || "$installed" != "$requested_opencode_version" ]]; then
    echo "OpenCode verification failed: requested $requested_opencode_version, found $installed" >&2
    exit 1
  fi
  echo "OpenCode CLI installed: $installed"
fi

if has_target tools; then
  packages=()
  for package in bats shellcheck; do
    if [[ "$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true)" == installed ]]; then
      packages+=("$package")
    fi
  done
  if ((${#packages[@]})); then apt-get install -y --only-upgrade "${packages[@]}"; else echo 'Bats/ShellCheck are not installed; skipping.'; fi
fi

if has_target system; then
  apt-get upgrade -y
elif has_target docker; then
  packages=(docker-ce docker-ce-cli containerd.io docker-compose-plugin docker-buildx-plugin docker-ce-rootless-extras)
  installed_packages=()
  for package in "${packages[@]}"; do
    if [[ "$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true)" == installed ]]; then
      installed_packages+=("$package")
    fi
  done
  if ((${#installed_packages[@]})); then apt-get install -y --only-upgrade "${installed_packages[@]}"; else echo 'Guest Docker packages are not installed; skipping.'; fi
fi

if has_target system || has_target docker; then
  uid="$(id -u mester)"
  docker_socket="/run/user/$uid/docker.sock"
  if [[ -S "$docker_socket" ]] && runuser -u mester -- env \
    XDG_RUNTIME_DIR="/run/user/$uid" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
    systemctl --user is-active --quiet docker; then
    runuser -u mester -- env \
      XDG_RUNTIME_DIR="/run/user/$uid" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
      systemctl --user restart docker
  fi
fi

if [[ -f /var/run/reboot-required ]]; then
  echo 'NOTICE: guest packages were updated; Ubuntu reports that a VM reboot is required to finish applying some updates.'
fi
GUEST_UPDATE_APPLY
fi

if grep -q '^UPDATE  Little Mester config/plugins ' <<<"$check_output"; then
  if ! tar -C "$LAB_DIR/agent/opencode" -cf - opencode.json plugins | \
    vm_ssh "$vm_dir" 'sudo tar -xf - -C /etc/opencode'; then
    die 'Failed to transfer Little Mester config/plugins; package updates may already have completed.'
  fi
  vm_ssh "$vm_dir" "sudo sed -i \
    -e 's#http://ollama:11434#http://10.0.2.100:11434#g' \
    -e 's#http://llamacpp:11434#http://10.0.2.100:8080#g' \
    /etc/opencode/opencode.json && \
    sudo chown -R root:root /etc/opencode && \
    sudo chmod -R a+rX /etc/opencode && \
    printf '%s\\n' '$config_hash' | sudo tee /etc/opencode/.little-mester-config-hash >/dev/null"
  echo 'Little Mester guest config/plugins synchronized.'
fi

echo "Guest update completed for VM '$vm_name'. The VM overlay, base image, workspace and credential database were not replaced."
