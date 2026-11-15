# shellcheck shell=bash
# Shared exact command fragments for the two disposable hosted-KVM boots.

ci_kvm_netdev() {
  local port=$1 restricted=$2
  [[ "$port" =~ ^[0-9]{1,5}$ ]] && (( port >= 1024 && port <= 65535 )) || return 2
  case "$restricted" in
    0) printf 'user,id=net0,hostfwd=tcp:127.0.0.1:%s-:22\n' "$port" ;;
    1) printf 'user,id=net0,restrict=on,hostfwd=tcp:127.0.0.1:%s-:22\n' "$port" ;;
    *) return 2 ;;
  esac
}

ci_kvm_qemu_args() {
  local mode=$1 overlay=$2 seed=$3 serial_log=$4 port=$5
  local restricted=0
  # shellcheck disable=SC2178
  local -n output_ref=$6
  case "$mode" in
    initial) restricted=0 ;;
    restricted-restart) restricted=1 ;;
    *) return 2 ;;
  esac
  # output_ref is a nameref; its target array is filled for the caller.
  # shellcheck disable=SC2034,SC2054
  output_ref=(
    -name little-mester-hosted-kvm-integration
    -machine q35,accel=kvm -cpu host -smp 2 -m 4096
    -nodefaults -no-user-config -display none -monitor none
    -serial "file:$serial_log"
    -drive "file=$overlay,if=virtio,format=qcow2"
    -drive "file=$seed,if=virtio,format=raw,readonly=on"
    -netdev "$(ci_kvm_netdev "$port" "$restricted")"
    -device virtio-net-pci,netdev=net0
  )
}

ci_kvm_ssh_args() {
  local destination=$1 key=$2 port=$3 known_hosts=$4 tty_mode=$5
  # shellcheck disable=SC2178
  local -n output_ref=$6
  local pty_flag=-T
  [[ "$tty_mode" == tty ]] && pty_flag=-tt
  [[ "$tty_mode" == tty || "$tty_mode" == batch ]] || return 2
  # output_ref is a nameref; its target array is filled for the caller.
  # shellcheck disable=SC2034
  output_ref=(
    -F /dev/null "$pty_flag" -a -x -i "$key" -p "$port"
    -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityAgent=none
    -o "UserKnownHostsFile=$known_hosts" -o GlobalKnownHostsFile=/dev/null
    -o StrictHostKeyChecking=yes -o ConnectTimeout=3
    -o ProxyCommand=none -o ProxyJump=none "$destination"
  )
}

ci_kvm_persistence_create_command() {
  local path=$1 content=$2 uid=$3 gid=$4 home=$5
  [[ "$path" == /* && "$path" != *$'\n'* && "$content" != *$'\n'* ]] || return 2
  [[ "$uid" =~ ^[0-9]+$ && "$gid" =~ ^[0-9]+$ && "$home" == /* ]] || return 2
  printf 'set -eu\npath=%q\ncontent=%q\nexpected_uid=%q\nexpected_gid=%q\nexpected_home=%q\n' \
    "$path" "$content" "$uid" "$gid" "$home"
  cat <<'REMOTE'
printf '%s\n' KVM_PERSISTENCE_CREATE_AUTHENTICATED
if [[ "$(id -u)" != "$expected_uid" || "$(id -g)" != "$expected_gid" ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=identity-mismatch
  exit 50
fi
if [[ "$HOME" != "$expected_home" ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=home-mismatch
  exit 50
fi
if [[ "$PWD" != "$expected_home" ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=working-directory-mismatch
  exit 50
fi
parent="${path%/*}"
if [[ ! -d "$parent" || -L "$parent" ]] || [[ "$(realpath -e -- "$parent" 2>/dev/null || true)" != "$parent" ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=parent-path-noncanonical
  exit 50
fi
if [[ "$(stat -c '%u:%g:%a' -- "$parent" 2>/dev/null || true)" != "$expected_uid:$expected_gid:700" ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=parent-owner-or-mode
  exit 50
fi
if [[ ! -w "$parent" || ! -x "$parent" ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=parent-inaccessible
  exit 50
fi
if [[ -e "$path" || -L "$path" ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=fixture-path-preoccupied
  exit 50
fi
umask 077
set -o noclobber
if ! printf '%s' "$content" >"$path"; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=create-failed
  exit 50
fi
chmod 0600 -- "$path" || { printf '%s\n' KVM_PERSISTENCE_CHECK=create-failed; exit 50; }
printf '%s\n' KVM_PERSISTENCE_CREATE=PASS
REMOTE
}

ci_kvm_persistence_flush_command() {
  local path=$1 uid=$2 gid=$3 home=$4
  [[ "$path" == /* && "$path" != *$'\n'* ]] || return 2
  [[ "$uid" =~ ^[0-9]+$ && "$gid" =~ ^[0-9]+$ && "$home" == /* ]] || return 2
  printf 'set -eu\npath=%q\nexpected_uid=%q\nexpected_gid=%q\nexpected_home=%q\n' \
    "$path" "$uid" "$gid" "$home"
  cat <<'REMOTE'
printf '%s\n' KVM_PERSISTENCE_FLUSH_AUTHENTICATED
if [[ "$(id -u)" != "$expected_uid" || "$(id -g)" != "$expected_gid" ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=identity-mismatch
  exit 50
fi
if [[ "$HOME" != "$expected_home" ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=home-mismatch
  exit 50
fi
if [[ "$PWD" != "$expected_home" ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=working-directory-mismatch
  exit 50
fi
if [[ ! -f "$path" || -L "$path" ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=fixture-not-regular
  exit 50
fi
if ! sync -f -- "$path"; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=filesystem-flush-failed
  exit 50
fi
printf '%s\n' KVM_PERSISTENCE_FLUSH=PASS
REMOTE
}

ci_kvm_persistence_check_command() {
  local path=$1 uid=$2 gid=$3 home=$4 expected_sha256=$5 expected_mount_sha256=${6:-}
  [[ "$path" == /* && "$path" != *$'\n'* ]] || return 2
  [[ "$uid" =~ ^[0-9]+$ && "$gid" =~ ^[0-9]+$ && "$home" == /* ]] || return 2
  [[ "$expected_sha256" =~ ^[0-9a-f]{64}$ ]] || return 2
  [[ -z "$expected_mount_sha256" || "$expected_mount_sha256" =~ ^[0-9a-f]{64}$ ]] || return 2
  printf 'set -eu\npath=%q\nexpected_uid=%q\nexpected_gid=%q\nexpected_home=%q\nexpected_content_sha256=%q\nexpected_mount_sha256=%q\n' \
    "$path" "$uid" "$gid" "$home" "$expected_sha256" "$expected_mount_sha256"
  cat <<'REMOTE'
printf '%s\n' KVM_PERSISTENCE_REMOTE_AUTHENTICATED
if [[ "$(id -u)" != "$expected_uid" || "$(id -g)" != "$expected_gid" ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=identity-mismatch
  exit 50
fi
if [[ "$HOME" != "$expected_home" ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=home-mismatch
  exit 50
fi
if [[ "$PWD" != "$expected_home" ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=working-directory-mismatch
  exit 50
fi
parent="${path%/*}"
if [[ -L "$path" ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=fixture-symlink
  exit 50
fi
if [[ ! -e "$path" ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=fixture-missing
  exit 50
fi
if [[ ! -f "$path" ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=fixture-not-regular
  exit 50
fi
if [[ "$(realpath -e -- "$path" 2>/dev/null || true)" != "$path" || -L "$parent" || "$(realpath -e -- "$parent" 2>/dev/null || true)" != "$parent" ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=fixture-path-noncanonical
  exit 50
fi
if [[ "$(stat -c '%u:%g' -- "$path" 2>/dev/null || true)" != "$expected_uid:$expected_gid" ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=fixture-owner-mismatch
  exit 50
fi
if [[ "$(stat -c '%a' -- "$path" 2>/dev/null || true)" != 600 ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=fixture-mode-mismatch
  exit 50
fi
if [[ "$(stat -c '%u:%g:%a' -- "$parent" 2>/dev/null || true)" != "$expected_uid:$expected_gid:700" ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=parent-owner-or-mode
  exit 50
fi
if [[ ! -r "$path" || ! -w "$path" || ! -r "$parent" || ! -w "$parent" || ! -x "$parent" ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=fixture-inaccessible
  exit 50
fi
content_record="$(sha256sum -- "$path" 2>/dev/null)" || { printf '%s\n' KVM_PERSISTENCE_CHECK=fixture-unreadable; exit 50; }
content_sha256="${content_record%% *}"
if [[ "$content_sha256" != "$expected_content_sha256" ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=fixture-content-mismatch
  exit 50
fi
mount_record="$(findmnt -n --raw -o TARGET,MAJ:MIN,FSTYPE --target "$path" 2>/dev/null)" || { printf '%s\n' KVM_PERSISTENCE_CHECK=mount-identity-unavailable; exit 50; }
read -r mount_target mount_device mount_type <<<"$mount_record"
if [[ "$mount_target" != / || ! "$mount_device" =~ ^[0-9]+:[0-9]+$ || -z "$mount_type" ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=fixture-not-on-root-filesystem
  exit 50
fi
mount_sha256="$(printf '%s\n' "$mount_record" | sha256sum)"
mount_sha256="${mount_sha256%% *}"
if [[ -n "$expected_mount_sha256" && "$mount_sha256" != "$expected_mount_sha256" ]]; then
  printf '%s\n' KVM_PERSISTENCE_CHECK=mount-identity-changed
  exit 50
fi
printf '%s\n' KVM_PERSISTENCE_CHECK=PASS
printf 'KVM_PERSISTENCE_CONTENT_SHA256=%s\n' "$content_sha256"
printf 'KVM_PERSISTENCE_MOUNT_SHA256=%s\n' "$mount_sha256"
REMOTE
}

ci_kvm_restart_readiness_command() {
  local previous_boot_id=${1:-}
  local opencode_binary=${2:-/usr/local/bin/opencode}
  local boot_finished=${3:-/var/lib/cloud/instance/boot-finished}
  local boot_id_file=${4:-/proc/sys/kernel/random/boot_id}
  [[ "$previous_boot_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || return 2
  printf 'printf "%%s\\n" KVM_RESTART_REMOTE_AUTHENTICATED\n'
  # An authenticated SSH command is the readiness proof. Socket activation can
  # serve it while ssh.service itself is inactive, so unit state is diagnostic,
  # not a readiness gate.
  printf 'if ! systemctl is-active --quiet ssh.service; then printf "%%s\\n" KVM_RESTART_REMOTE_NOTE=ssh-service-inactive; fi\n'
  # These printf format strings deliberately emit guest-shell substitutions.
  # shellcheck disable=SC2016
  printf 'current_guest_boot_id="$(cat %q 2>/dev/null)" || { printf "%%s\\n" KVM_RESTART_REMOTE_CHECK=guest-boot-id-unavailable; exit 47; }\n' "$boot_id_file"
  # shellcheck disable=SC2016
  printf 'if ! printf "%%s\\n" "$current_guest_boot_id" | grep -Eq "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"; then printf "%%s\\n" KVM_RESTART_REMOTE_CHECK=guest-boot-id-invalid; exit 48; fi\n'
  # shellcheck disable=SC2016
  printf 'if [[ "$current_guest_boot_id" == %q ]]; then printf "%%s\\n" KVM_RESTART_REMOTE_CHECK=guest-boot-id-unchanged; exit 44; fi\n' "$previous_boot_id"
  printf 'if ! test -f %q; then printf "%%s\\n" KVM_RESTART_REMOTE_CHECK=cloud-init-not-ready; exit 45; fi\n' "$boot_finished"
  # shellcheck disable=SC2016
  printf 'cloud_init_rc=0; cloud_init_status="$(cloud-init status --long 2>/dev/null)" || cloud_init_rc=$?\n'
  # shellcheck disable=SC2016
  printf 'if (( cloud_init_rc != 0 )); then printf "%%s\\n" KVM_RESTART_REMOTE_CHECK=cloud-init-error; exit 46; fi\n'
  # shellcheck disable=SC2016
  printf 'if printf "%%s\\n" "$cloud_init_status" | grep -Eq "^(status|extended_status): (error|degraded)"; then printf "%%s\\n" KVM_RESTART_REMOTE_CHECK=cloud-init-error; exit 46; fi\n'
  # shellcheck disable=SC2016
  printf 'if ! printf "%%s\\n" "$cloud_init_status" | grep -Eq "^status: done$" || ! printf "%%s\\n" "$cloud_init_status" | grep -Eq "^extended_status: done$"; then printf "%%s\\n" KVM_RESTART_REMOTE_CHECK=cloud-init-not-ready; exit 45; fi\n'
  # Emit guest-shell code with the path safely shell-quoted by printf %q.
  # shellcheck disable=SC2016
  printf 'if ! test -x %q; then printf "%%s\\n" KVM_RESTART_REMOTE_CHECK=opencode-binary-missing; exit 43; fi\n' "$opencode_binary"
  cat <<'REMOTE'
printf '%s\n' KVM_RESTART_REMOTE_READY
REMOTE
}
