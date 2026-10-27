#!/usr/bin/env bash
# prereqs.sh - Auto-detect and install VM dependencies for ./lab-vm.
#
# Detects the host OS, checks for required/optional commands, and
# prompts the user before installing anything. Optional components
# (KVM, UEFI BIOS) fall back gracefully without prompting.
#
# Auto-accept: if VM_YES=1 (via -y / --yes flag or env var), the
# install prompt is skipped and packages are installed automatically.
#
# Usage: source this file, then call _vm_prereqs_check
#   returns 0 = all good
#   returns 1 = missing required deps (install prompted)

set -euo pipefail

# --- Distro detection ---------------------------------------------------
_vm_prereqs_distro() {
  if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    echo "${ID:-}"
  elif [[ -f /etc/arch-release ]]; then
    echo "arch"
  else
    echo "unknown"
  fi
}

# --- Package manager detection ------------------------------------------
_vm_prereqs_pkg_manager() {
  if command -v apt-get >/dev/null 2>&1; then
    echo "apt"
  elif command -v dnf >/dev/null 2>&1; then
    echo "dnf"
  elif command -v yum >/dev/null 2>&1; then
    echo "yum"
  elif command -v pacman >/dev/null 2>&1; then
    echo "pacman"
  else
    echo "unknown"
  fi
}

# --- Map missing commands to package names per distro -------------------
_vm_prereqs_map_packages() {
  local _map_distro="$1"
  local _map_pkg_mgr="$2"
  shift 2
  local -a _map_missing=("$@")
  local -A _map_required=()
  local -A _map_optional=()

  case "$_map_pkg_mgr" in
    apt)
      for _dep in "${_map_missing[@]}"; do
        case "$_dep" in
          qemu-system-x86_64)  _map_required+=("qemu-system-x86 qemu-arch-extra") ;;
          qemu-img)            _map_required["qemu-img"]="qemu-utils" ;;
          virtiofsd)           _map_required["virtiofsd"]="libvirt-daemon-system" ;;
          genisoimage|mkisofs) _map_required["genisoimage"]="genisoimage" ;;
        esac
      done
      if [[ ! -f /usr/share/OVMF/OVMF.fd ]]; then
        _map_optional["OVMF"]="ovmf"
      fi
      ;;
    dnf|yum)
      for _dep in "${_map_missing[@]}"; do
        case "$_dep" in
          qemu-system-x86_64)  _map_required+=("qemu-system-x86") ;;
          qemu-img)            _map_required["qemu-img"]="qemu-img" ;;
          virtiofsd)           _map_required["virtiofsd"]="libvirt-daemon-config" ;;
          genisoimage|mkisofs) _map_required["genisoimage"]="genisoimage" ;;
        esac
      done
      if [[ ! -f /usr/share/OVMF/OVMF.fd ]]; then
        _map_optional["OVMF"]="edk2-ovmf"
      fi
      ;;
    pacman)
      for _dep in "${_map_missing[@]}"; do
        case "$_dep" in
          qemu-system-x86_64)  _map_required+=("qemu") ;;
          qemu-img)            _map_required["qemu-img"]="qemu" ;;
          virtiofsd)           _map_required["virtiofsd"]="libvirt" ;;
          genisoimage|mkisofs) _map_required["genisoimage"]="cdrtools" ;;
          ssh|ssh-keyscan)     _map_required["ssh"]="openssh" ;;
        esac
      done
      if [[ ! -f /usr/share/OVMF/OVMF.fd ]]; then
        _map_optional["OVMF"]="edk2-ovmf"
      fi
      ;;
    *)
      log "WARNING: Unsupported package manager ($_map_pkg_mgr). Cannot auto-install."
      log "Please install these manually:"
      for _dep in "${_map_missing[@]}"; do log "  - $_dep"; done
      return 1
      ;;
  esac

  local _all_required=""
  for _pkg in "${_map_required[@]}"; do
    _all_required+=" $_pkg"
  done
  echo "REQUIRED:$_all_required"

  local _all_optional=""
  for _key in "${!_map_optional[@]}"; do
    _all_optional+=" ${_map_optional[$_key]}"
  done
  if [[ -n "$_all_optional" ]]; then
    echo "OPTIONAL:$_all_optional"
  fi
}

# --- Resolve virtiofsd on Arch (binary at /usr/lib/qemu/ instead of PATH) ---
_vm_prereqs_fix_virtiofsd_path() {
  # Already on PATH — nothing to do
  command -v virtiofsd >/dev/null 2>&1 && return 0

  local _found=""

  # Common non-standard locations for virtiofsd on Arch-derived distros
  for _cand in /usr/lib/qemu/qemu-virtiofsd /usr/bin/virtiofsd; do
    if [[ -x "$_cand" ]]; then
      _found="$_cand"
      break
    fi
  done

  # If not found in common paths, ask pacman where it lives
  if [[ -z "$_found" ]] && command -v pacman >/dev/null 2>&1; then
    local _pkg_file
    for _pkg in virtiofsd libvirt; do
      _pkg_file="$(pacman -Ql "$_pkg" 2>/dev/null | grep '/virtiofsd$' | head -1 || true)"
      if [[ -n "$_pkg_file" ]]; then
        _found="$(echo "$_pkg_file" | awk '{print $2}')"
        break
      fi
    done
  fi

  # Symlink into /usr/local/bin so it's on PATH without PATH manipulation
  if [[ -n "$_found" && -x "$_found" ]]; then
    if [[ ! -f /usr/local/bin/virtiofsd ]]; then
      sudo ln -sf "$_found" /usr/local/bin/virtiofsd
    fi
    log "Fixed: virtiofsd -> $_found (symlinked to /usr/local/bin/)"
    return 0
  fi

  # Still not found — package likely not installed
  return 1
}

# --- Check which commands are missing -----------------------------------
_vm_prereqs_detect_missing() {
  local _c
  local _c2
  for _c in qemu-system-x86_64 qemu-img virtiofsd ssh ssh-keyscan; do
    command -v "$_c" >/dev/null 2>&1 || echo "$_c"
  done
  # genisoimage or mkisofs — report only if neither exists
  if ! command -v genisoimage >/dev/null 2>&1 && ! command -v mkisofs >/dev/null 2>&1; then
    echo "genisoimage"
    echo "mkisofs"
  fi
}

# --- Main check & install flow ------------------------------------------
_vm_prereqs_install() {
  local _inst_distro _inst_pkg_mgr
  _inst_distro="$(_vm_prereqs_distro)"
  _inst_pkg_mgr="$(_vm_prereqs_pkg_manager)"

  local -a _inst_missing=()
  while IFS= read -r _line; do
    [[ -n "$_line" ]] && _inst_missing+=("$_line")
  done < <(_vm_prereqs_detect_missing)

  [[ ${#_inst_missing[@]} -eq 0 ]] && return 0

  local _pkg_line
  _pkg_line="$(_vm_prereqs_map_packages "$_inst_distro" "$_inst_pkg_mgr" "${_inst_missing[@]}")" || return 1

  local _req_pkgs _opt_pkgs
  _req_pkgs=$(echo "$_pkg_line" | grep '^REQUIRED:' | sed 's/^REQUIRED://')
  _opt_pkgs=$(echo "$_pkg_line" | grep '^OPTIONAL:' | sed 's/^OPTIONAL://' || true)

  local -a _all_pkgs=()
  for _pkg in $_req_pkgs $_opt_pkgs; do
    _all_pkgs+=("$_pkg")
  done

  echo ""
  echo "=== little-mester VM: Missing dependencies ==="
  echo "Distro: $_inst_distro ($_inst_pkg_mgr)"
  echo ""
  echo "Required tools missing: ${_inst_missing[*]}"
  echo "Packages to install: ${_all_pkgs[*]}"
  echo ""
  echo "These are needed for QEMU/KVM virtualization:"
  echo "  qemu-system-x86_64  - Hypervisor"
  echo "  qemu-img            - Disk image tools"
  echo "  virtiofsd           - Filesystem sharing (workspace mount)"
  echo "  genisoimage/mkisofs - Cloud-init seed ISO generation"
  echo ""

  # Respect VM_YES for auto-accept (ticket #48, A7-A8)
  if [[ "${VM_YES:-0}" != "1" ]]; then
    read -r -p "Install these packages? [y/N] " _inst_choice
    if [[ "$_inst_choice" != [yY] && "$_inst_choice" != [yY][eE][sS] ]]; then
      log "Aborted. Install manually and re-run."
      return 1
    fi
  else
    echo "Auto-accept enabled (VM_YES=1) — installing without prompt."
  fi

  echo "Installing..."

  if ! command -v sudo >/dev/null 2>&1; then
    log "ERROR: sudo is not found. Cannot auto-install packages."
    log ""
    log "Please install the following manually:"
    log "  Required packages: ${_all_pkgs[*]}"
    log ""
    log "  Debian/Ubuntu:  sudo apt-get install ${_all_pkgs[*]}"
    log "  Fedora/RHEL:    sudo dnf install ${_all_pkgs[*]}"
    log "  Arch/Manjaro:   sudo pacman -S ${_all_pkgs[*]}"
    log ""
    return 1
  fi

  case "$_inst_pkg_mgr" in
    apt)
      sudo apt-get update
      sudo apt-get install -y "${_all_pkgs[@]}"
      ;;
    dnf)
      sudo dnf install -y "${_all_pkgs[@]}"
      ;;
    yum)
      sudo yum install -y "${_all_pkgs[@]}"
      ;;
    pacman)
      sudo pacman -S --noconfirm "${_all_pkgs[@]}"
      ;;
    *)
      log "ERROR: Cannot install on $_inst_pkg_mgr (unsupported)."
      return 1
      ;;
  esac

  echo ""
  echo "Dependencies installed successfully."
  echo "Re-run: ./lab-vm $_saved_cmd"
  return 0
}

# --- Public entry point -------------------------------------------------
_vm_prereqs_check() {
  local _check_cmd="${1:-}"

  log "Checking VM prerequisites..."

  # Auto-fix: on Arch/CachyOS virtiofsd lives at /usr/lib/qemu/ not on PATH
  _vm_prereqs_fix_virtiofsd_path || true

  local _check_missing
  _check_missing="$(_vm_prereqs_detect_missing)"

  if [[ -z "$_check_missing" ]]; then
    log "All VM dependencies found. OK"
    return 0
  fi

  log "Some dependencies are missing:"
  local _dep
  while IFS= read -r _dep; do
    log "  - $_dep"
  done <<< "$_check_missing"
  log ""

  return 1
}
