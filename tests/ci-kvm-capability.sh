#!/usr/bin/env bash
# Credential-free hosted-runner M2 integration on a disposable Ubuntu guest.
# OAuth/provider credentials and real model access are never used here.
# Shell snippets passed through SSH are intentionally single-quoted locally.
# shellcheck disable=SC2016
set -euo pipefail
umask 077

readonly image_url='https://cloud-images.ubuntu.com/releases/noble/release-20261017/ubuntu-24.04-server-cloudimg-amd64.img'
readonly image_sha256='04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94'

fail() {
  printf 'KVM_TRIAL_FAIL: %s\n' "$1" >&2
  if [[ -n "${serial_log:-}" && -f "$serial_log" ]]; then
    printf '%s\n' '--- bounded guest serial tail ---' >&2
    tail -n 80 "$serial_log" >&2 || true
  fi
  exit 1
}

[[ "$(uname -m)" == x86_64 ]] || fail 'runner architecture is not x86_64'
[[ -e /dev/kvm || -L /dev/kvm ]] || fail '/dev/kvm is absent; TCG fallback is forbidden'
[[ -c /dev/kvm ]] || fail '/dev/kvm exists but is not a character device'
[[ -r /dev/kvm && -w /dev/kvm ]] \
  || fail '/dev/kvm is not readable and writable after the udev permission step; TCG fallback is forbidden'

for command in qemu-system-x86_64 qemu-img curl ssh ssh-keygen ssh-keyscan socat genisoimage python3 timeout sha256sum; do
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

tmp_root="${RUNNER_TEMP:-/tmp/opencode}"
mkdir -p "$tmp_root"
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
LM_CI_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" \
LM_CI_PUBLIC_KEY="$(<"$key.pub")" \
python3 - <<'PY' >"$tmp/user-data"
import base64, importlib.util, json, os, pathlib
root = pathlib.Path(os.environ["LM_CI_REPO"])
spec = importlib.util.spec_from_file_location("session_contract", root / "lib/vm/parent-session-contract.py")
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)
files = [{"path": "/etc/opencode/opencode.json", "owner": "root:root", "permissions": "0644",
          "content": '{"autoupdate":false,"share":"disabled","permission":{"edit":"ask","bash":"ask"}}\n'}]
files.extend({"path": path, "owner": "root:root", "permissions": mode, "encoding": "b64",
              "content": base64.b64encode(content).decode("ascii")}
             for path, (content, mode) in contract.ASSETS.items())
for path, content in contract.PROTECTED_PLUGIN_ASSETS.items():
    files.append({"path": path,
                  "owner": "root:root", "permissions": "0644", "encoding": "b64",
                  "content": base64.b64encode(content).decode("ascii")})
install_command = (
    "export DEBIAN_FRONTEND=noninteractive; "
    "apt-get update -qq && apt-get install -y tmux nodejs npm ca-certificates && "
    "npm install --global @opencode/cli@2.2.5 && "
    "install -d -o mester -g mester -m 0700 /home/mester/workspace "
    "/home/mester/.local/share/opencode && printf ready > /var/tmp/lm-kvm-trial-ready"
)
config = {
    "ssh_pwauth": False,
    "disable_root": True,
    "users": [{"name": "mester", "uid": 1000, "primary_group": "mester",
               "shell": "/bin/bash", "lock_passwd": True,
               "ssh_authorized_keys": [os.environ["LM_CI_PUBLIC_KEY"]]}],
    "groups": ["mester"],
    "write_files": files,
    "runcmd": [["bash", "-lc", install_command]],
}
print("#cloud-config\n" + json.dumps(config, separators=(",", ":")))
PY
cat >"$tmp/meta-data" <<'EOF'
instance-id: little-mester-hosted-kvm-trial
local-hostname: little-mester-kvm-trial
EOF
genisoimage -quiet -output "$seed" -volid cidata -joliet -rock \
  "$tmp/user-data" "$tmp/meta-data"
qemu-img create -q -f qcow2 -F qcow2 -b "$image" "$overlay"

printf 'KVM_TRIAL: starting pinned guest with accel=kvm (no TCG fallback)\n'
timeout --signal=TERM --kill-after=10 2400 \
  qemu-system-x86_64 \
    -name little-mester-hosted-kvm-integration \
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
    "mester@127.0.0.1"
)

ready=0
for _ in {1..1500}; do
  kill -0 "$qemu_pid" 2>/dev/null || fail 'QEMU exited before guest readiness'
  if [[ ! -s "$known_hosts" ]]; then
    ssh-keyscan -T 2 -p "$port" -t ed25519 127.0.0.1 >"$known_hosts" 2>/dev/null || true
  fi
  # The single-quoted payload is intentionally interpreted only by the guest shell.
  # shellcheck disable=SC2016
  if [[ -s "$known_hosts" ]]; then
    if timeout --signal=TERM --kill-after=2 8 ssh "${ssh_args[@]}" \
        'state=$(cloud-init status --long 2>&1 || true); printf "%s\n" "$state"; if printf "%s\n" "$state" | grep -Eq "^(status|extended_status): error"; then exit 42; fi; test -f /var/lib/cloud/instance/boot-finished && test "$(cat /var/tmp/lm-kvm-trial-ready)" = ready' \
        >"$tmp/readiness.log" 2>&1; then
      ready=1
      break
    elif grep -Eq '^(status|extended_status): error' "$tmp/readiness.log"; then
      cat "$tmp/readiness.log" >&2
      fail 'cloud-init reported an error before guest readiness'
    fi
  fi
  sleep 1
done
[[ "$ready" == 1 ]] || fail 'pinned QEMU guest did not reach bounded cloud-init/SSH readiness'
printf 'KVM_TRIAL_PASS: pinned guest booted with accel=kvm and passed cloud-init readiness\n'

ssh_guest() {
  timeout --signal=TERM --kill-after=2 20 ssh "${ssh_args[@]}" "$1"
}
guest_asset_checks="$(python3 - <<'PY'
import importlib.util, pathlib
path = pathlib.Path("lib/vm/parent-session-contract.py")
spec = importlib.util.spec_from_file_location("contract", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(module.readiness_checks())
PY
)"
ssh_guest "cloud-init status --long >/dev/null 2>&1 || exit 41; $guest_asset_checks"
ssh_tty_guest() {
  timeout --signal=TERM --kill-after=2 15 ssh \
    -F /dev/null -tt -a -x -i "$key" -p "$port" \
    -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityAgent=none \
    -o UserKnownHostsFile="$known_hosts" -o GlobalKnownHostsFile=/dev/null \
    -o StrictHostKeyChecking=yes -o ConnectTimeout=3 \
    -o ProxyCommand=none -o ProxyJump=none "mester@127.0.0.1" "$1"
}

ssh_guest 'test "$(/usr/local/bin/opencode --version)" = "opencode v2.2.5" && tmux -V >/dev/null'
ssh_guest 'test "$(stat -c %u:%a /etc/opencode/opencode.json)" = "0:644" && grep -Fq "\"share\":\"disabled\"" /etc/opencode/opencode.json'
# Guest-shell payloads are intentionally single-quoted so the local shell does
# not expand substitutions before they execute inside the disposable guest.
# shellcheck disable=SC2016
ssh_guest 'mkdir -p /home/mester/.local/share/opencode /home/mester/workspace && chmod 0775 /home/mester/.local /home/mester/.local/share /home/mester/.local/share/opencode && python3 -c '\''import sqlite3; db=sqlite3.connect("/home/mester/.local/share/opencode/opencode.db"); db.close()'\'' && chmod 0664 /home/mester/.local/share/opencode/opencode.db && db_before=$(sha256sum /home/mester/.local/share/opencode/opencode.db) && printf persistent-fixture > /home/mester/workspace/persisted.txt && chmod 0600 /home/mester/workspace/persisted.txt && printf "{\"share\":\"enabled\"}" > /home/mester/workspace/opencode.json && /usr/bin/python3 -I /usr/local/libexec/lm-guest-user-dirs.py prepare-auth && test "$(stat -c %a /home/mester/.local/share/opencode/opencode.db)" = 600 && test "$(sha256sum /home/mester/.local/share/opencode/opencode.db)" = "$db_before"'
ssh_guest 'printf unsafe-target-preserved > /tmp/lm-auth-unsafe-target && ln -s /tmp/lm-auth-unsafe-target /home/mester/.local/share/opencode/opencode.db-wal; if /usr/bin/python3 -I /usr/local/libexec/lm-guest-user-dirs.py prepare-auth >/dev/null 2>&1; then exit 1; fi; test "$(cat /tmp/lm-auth-unsafe-target)" = unsafe-target-preserved; rm /home/mester/.local/share/opencode/opencode.db-wal'
ssh_tty_guest 'timeout --signal=TERM --kill-after=2 8 /usr/local/bin/lm-opencode >/dev/null 2>&1 || test "$?" = 124'
# shellcheck disable=SC2016
ssh_guest 'test "$(stat -c %a /home/mester/.local/share/opencode/opencode.db)" = 600 && test "$(stat -c %a /home/mester/.local/share/opencode)" = 700 && TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux has-session -t little-mester-opencode && test "$(TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux display-message -p -t little-mester-opencode:0.0 "#{pane_dead}")" = 0 && TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux show-environment -g OPENCODE_CONFIG | grep -Fx OPENCODE_CONFIG=/etc/opencode/opencode.json && TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux tmux show-environment -g OPENCODE_DISABLE_PROJECT_CONFIG | grep -Fx OPENCODE_DISABLE_PROJECT_CONFIG=1'
printf 'KVM_M2_INTEGRATION_PASS: real KVM guest, SSH, pinned OpenCode 2.2.5, protected config, tmux TUI process and persistent workspace fixture\n'

# Reboot the same writable overlay to prove guest-disk durability without
# copying or reinitializing the VM disk. The in-memory tmux socket is expected
# to be recreated; the session/workspace files remain on the persistent disk.
kill -TERM "$qemu_pid" 2>/dev/null || true
wait "$qemu_pid" 2>/dev/null || true
qemu_pid=''
timeout --signal=TERM --kill-after=10 600 \
  qemu-system-x86_64 \
    -name little-mester-hosted-kvm-integration \
    -machine q35,accel=kvm -cpu host -smp 2 -m 4096 \
    -nodefaults -no-user-config -display none -monitor none \
    -serial "file:$serial_log" \
    -drive "file=$overlay,if=virtio,format=qcow2" \
    -drive "file=$seed,if=virtio,format=raw,readonly=on" \
    -netdev "user,id=net0,restrict=on,hostfwd=tcp:127.0.0.1:$port-:22" \
    -device virtio-net-pci,netdev=net0 &
qemu_pid=$!
ready=0
for _ in {1..300}; do
  kill -0 "$qemu_pid" 2>/dev/null || fail 'QEMU exited during persistent-disk reboot'
# shellcheck disable=SC2016
  if timeout --signal=TERM --kill-after=2 8 ssh "${ssh_args[@]}" \
      'test "$(cat /home/mester/workspace/persisted.txt)" = persistent-fixture && test "$(/usr/local/bin/opencode --version)" = "opencode v2.2.5"' \
      >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
[[ "$ready" == 1 ]] || fail 'persistent guest disk did not retain OpenCode/workspace state across reboot'
printf 'KVM_PERSISTENCE_PASS: OpenCode runtime and workspace data survived disposable guest reboot\n'
TMPDIR="$tmp" python3 tests/ci-qemu-oauth-bridge.py \
  --port "$port" --identity "$key" --known-hosts "$known_hosts"
printf 'KVM_M2_AUTH_BOUNDARY: live browser approval, provider token exchange and real model access are NOT_RUN\n'
