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

ci_kvm_restart_readiness_command() {
  local previous_boot_id=${1:-} persistence_file=${2:-/home/mester/workspace/persisted.txt}
  local opencode_binary=${3:-/usr/local/bin/opencode}
  local boot_finished=${4:-/var/lib/cloud/instance/boot-finished}
  local boot_id_file=${5:-/proc/sys/kernel/random/boot_id}
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
  printf 'if ! test "$(cat %q 2>/dev/null)" = persistent-fixture; then printf "%%s\\n" KVM_RESTART_REMOTE_CHECK=persistence-file-missing; exit 42; fi\n' "$persistence_file"
  printf 'if ! test -x %q; then printf "%%s\\n" KVM_RESTART_REMOTE_CHECK=opencode-binary-missing; exit 43; fi\n' "$opencode_binary"
  cat <<'REMOTE'
printf '%s\n' KVM_RESTART_REMOTE_READY
REMOTE
}
