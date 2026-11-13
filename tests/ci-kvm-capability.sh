#!/usr/bin/env bash
# Hosted-runner capability trial for the pinned, disposable Ubuntu guest.
# This is deliberately a boot/readiness test, not OAuth/model acceptance.
set -euo pipefail
umask 077

readonly image_url='https://cloud-images.ubuntu.com/releases/noble/release-20261017/ubuntu-24.04-server-cloudimg-amd64.img'
readonly image_sha256='04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94'

fail() {
  printf 'KVM_TRIAL_FAIL: %s\n' "$1" >&2
  exit 1
}

[[ "$(uname -m)" == x86_64 ]] || fail 'runner architecture is not x86_64'
[[ -e /dev/kvm || -L /dev/kvm ]] || fail '/dev/kvm is absent; TCG fallback is forbidden'
[[ -c /dev/kvm ]] || fail '/dev/kvm exists but is not a character device'
[[ -r /dev/kvm && -w /dev/kvm ]] \
  || fail '/dev/kvm is not readable and writable after the udev permission step; TCG fallback is forbidden'

for command in qemu-system-x86_64 qemu-img cloud-localds curl ssh ssh-keygen ssh-keyscan socat genisoimage python3 timeout sha256sum; do
  command -v "$command" >/dev/null 2>&1 || fail "required host command is missing: $command"
done

virtiofsd=''
for candidate in /usr/lib/virtiofsd /usr/libexec/virtiofsd /usr/lib/qemu/virtiofsd; do
  if [[ -x "$candidate" && ! -L "$candidate" ]]; then
    virtiofsd="$candidate"
    break
  fi
done
[[ -n "$virtiofsd" ]] || fail 'required virtiofsd executable is unavailable'

tmp_root="${RUNNER_TEMP:-/tmp}"
tmp="$(mktemp -d "$tmp_root/lm-kvm-trial.XXXXXXXX")"
qemu_pid=''
cleanup() {
  local status=$?
  if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" 2>/dev/null; then
    kill -TERM "$qemu_pid" 2>/dev/null || true
    for _ in {1..50}; do
      kill -0 "$qemu_pid" 2>/dev/null || break
      sleep 0.1
    done
    kill -KILL "$qemu_pid" 2>/dev/null || true
    wait "$qemu_pid" 2>/dev/null || true
  fi
  rm -rf -- "$tmp"
  exit "$status"
}
trap cleanup EXIT INT TERM

port="$(python3 - <<'PY'
import socket
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    print(listener.getsockname()[1])
PY
)"

image="$tmp/noble.img"
seed="$tmp/seed.iso"
overlay="$tmp/guest.qcow2"
key="$tmp/id_ed25519"
known_hosts="$tmp/known_hosts"
serial_log="$tmp/serial.log"

curl --fail --location --silent --show-error --connect-timeout 20 --max-time 600 \
  "$image_url" --output "$image"
printf '%s  %s\n' "$image_sha256" "$image" | sha256sum --check --status \
  || fail 'pinned guest image SHA-256 mismatch'

ssh-keygen -q -t ed25519 -N '' -f "$key"
cat >"$tmp/user-data" <<EOF
#cloud-config
ssh_pwauth: false
disable_root: true
users:
  - default
  - name: lmtrial
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys:
      - $(<"$key.pub")
runcmd:
  - [sh, -c, 'printf ready > /var/tmp/lm-kvm-trial-ready']
EOF
cat >"$tmp/meta-data" <<'EOF'
instance-id: little-mester-hosted-kvm-trial
local-hostname: little-mester-kvm-trial
EOF
cloud-localds "$seed" "$tmp/user-data" "$tmp/meta-data"
qemu-img create -q -f qcow2 -F qcow2 -b "$image" "$overlay"

printf 'KVM_TRIAL: starting pinned guest with accel=kvm (no TCG fallback)\n'
timeout --signal=TERM --kill-after=10 1100 \
  qemu-system-x86_64 \
    -name little-mester-hosted-kvm-trial \
    -machine q35,accel=kvm -cpu host -smp 2 -m 4096 \
    -nodefaults -no-user-config -display none -monitor none \
    -serial "file:$serial_log" \
    -drive "file=$overlay,if=virtio,format=qcow2" \
    -drive "file=$seed,if=virtio,format=raw,readonly=on" \
    -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:$port-:22" \
    -device virtio-net-pci,netdev=net0 &
qemu_pid=$!

ssh_args=(
  -F /dev/null -T -a -x
  -i "$key" -p "$port"
  -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityAgent=none
  -o UserKnownHostsFile="$known_hosts" -o GlobalKnownHostsFile=/dev/null
  -o StrictHostKeyChecking=yes -o ConnectTimeout=3
  -o ProxyCommand=none -o ProxyJump=none
  "lmtrial@127.0.0.1"
)

ready=0
for _ in {1..300}; do
  kill -0 "$qemu_pid" 2>/dev/null || fail 'QEMU exited before guest readiness'
  if [[ ! -s "$known_hosts" ]]; then
    ssh-keyscan -T 2 -p "$port" -t ed25519 127.0.0.1 >"$known_hosts" 2>/dev/null || true
  fi
  # The single-quoted payload is intentionally interpreted only by the guest shell.
  # shellcheck disable=SC2016
  if [[ -s "$known_hosts" ]] && timeout --signal=TERM --kill-after=2 8 ssh "${ssh_args[@]}" \
      'cloud-init status --wait --long >/dev/null 2>&1 && test -f /var/lib/cloud/instance/boot-finished && test "$(cat /var/tmp/lm-kvm-trial-ready)" = ready' \
      >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
[[ "$ready" == 1 ]] || fail 'pinned QEMU guest did not reach bounded cloud-init/SSH readiness'

printf 'KVM_TRIAL_PASS: pinned guest booted with KVM and passed cloud-init readiness\n'
printf 'KVM_TRIAL_LIMIT: OAuth consent, OpenCode model access and owner acceptance are NOT_RUN\n'
