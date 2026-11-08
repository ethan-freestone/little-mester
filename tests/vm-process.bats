#!/usr/bin/env bats
# V-PROCESS: argv and subprocess fixtures only; never starts real QEMU/KVM.

setup() {
  ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
  TEST_ROOT="$(mktemp -d /tmp/little-mester-process.XXXXXX)"
  chmod 700 "$TEST_ROOT"
  mkdir -m 700 "$TEST_ROOT/bin" "$TEST_ROOT/profile"
  source "$ROOT/lib/vm/runner.sh"
  source "$ROOT/lib/vm/ssh.sh"
}

teardown() {
  if [[ -f "$TEST_ROOT/profile/runs/fake-start-a/processes.json" ]]; then
    python3 "$ROOT/lib/vm/process.py" stop \
      --registry "$TEST_ROOT/profile/runs/fake-start-a/processes.json" \
      --run-id fake-start-a --wait 2 >/dev/null 2>&1 || true
    if [[ -f "$TEST_ROOT/fake-socket-path" ]]; then
      socket_path="$(cat "$TEST_ROOT/fake-socket-path")"
      [[ ! -S "$socket_path" ]] || rm -f -- "$socket_path"
    fi
  fi
  if [[ "$TEST_ROOT" == /tmp/little-mester-process.* && -O "$TEST_ROOT" ]]; then
    rm -rf -- "$TEST_ROOT"
  fi
}

vm_test_netdev() {
  local index
  for index in "${!VM_QEMU_ARGV[@]}"; do
    if [[ "${VM_QEMU_ARGV[$index]}" == -netdev ]]; then
      printf '%s\n' "${VM_QEMU_ARGV[$((index + 1))]}"
      return 0
    fi
  done
  return 1
}

@test "argv device matrix is KVM-only, shareable, restricted and loopback-bound" {
  vm_runner_build_qemu_argv \
    demo 4 "$TEST_ROOT/seed.iso" "$TEST_ROOT/disk.qcow2" \
    /tmp/lm-vfs-test.sock 40222 43128 none '' ''
  [ "${#VM_QEMU_ARGV[@]}" -gt 0 ]
  [[ " ${VM_QEMU_ARGV[*]} " == *' -enable-kvm '* ]]
  [[ " ${VM_QEMU_ARGV[*]} " == *' -machine type=q35,accel=kvm '* ]]
  [[ " ${VM_QEMU_ARGV[*]} " == *' -cpu host '* ]]
  [[ " ${VM_QEMU_ARGV[*]} " == *' -smp 2 '* ]]
  [[ " ${VM_QEMU_ARGV[*]} " == *' memory-backend-memfd,id=mem,size=4G,share=on '* ]]
  [[ " ${VM_QEMU_ARGV[*]} " == *'vhost-user-fs-pci,chardev=charfs,tag=workspace'* ]]
  [[ " ${VM_QEMU_ARGV[*]} " != *'tcg'* ]]
  local netdev
  netdev="$(vm_test_netdev)"
  [[ "$netdev" == *'hostfwd=tcp:127.0.0.1:40222-:22'* ]]
  [[ "$netdev" == *'guestfwd=tcp:10.0.2.100:3128-cmd:socat - TCP:127.0.0.1:43128'* ]]
  [[ "$netdev" == *'restrict=on'* ]]
  [[ "$netdev" != *'guestfwd=tcp:10.0.2.100:11434'* ]]
  [[ "$netdev" != *'guestfwd=tcp:10.0.2.100:19532'* ]]
}

@test "selected crossings include only requested model and metrics endpoints" {
  vm_runner_build_qemu_argv \
    demo 2 "$TEST_ROOT/seed.iso" "$TEST_ROOT/disk.qcow2" \
    /tmp/lm-vfs-test.sock 40223 43129 ollama 11434 19532
  [ "${#VM_QEMU_ARGV[@]}" -gt 0 ]
  local netdev
  netdev="$(vm_test_netdev)"
  [[ "$netdev" == *'guestfwd=tcp:10.0.2.100:11434-cmd:socat - TCP:127.0.0.1:11434'* ]]
  [[ "$netdev" == *'guestfwd=tcp:10.0.2.100:19532-cmd:socat - TCP:127.0.0.1:19532'* ]]
  [[ "$netdev" != *'guestfwd=tcp:10.0.2.100:8080'* ]]

  vm_runner_build_qemu_argv \
    demo 2 "$TEST_ROOT/seed.iso" "$TEST_ROOT/disk.qcow2" \
    /tmp/lm-vfs-test.sock 40223 43129 llamacpp 9911 ''
  [ "${#VM_QEMU_ARGV[@]}" -gt 0 ]
  netdev="$(vm_test_netdev)"
  [[ "$netdev" == *'guestfwd=tcp:10.0.2.100:8080-cmd:socat - TCP:127.0.0.1:9911'* ]]
  [[ "$netdev" != *'guestfwd=tcp:10.0.2.100:11434'* ]]
  [[ "$netdev" != *'guestfwd=tcp:10.0.2.100:19532'* ]]
}

@test "long socket and hostile QEMU-sensitive paths refuse before argv construction" {
  run vm_runner_validate_socket_path "/tmp/$(printf 'x%.0s' {1..110}).sock"
  [ "$status" -eq 2 ]
  run vm_runner_build_qemu_argv \
    demo 4 "$TEST_ROOT/seed,unsafe.iso" "$TEST_ROOT/disk.qcow2" \
    /tmp/lm-vfs-test.sock 40222 43128 none '' ''
  [ "$status" -eq 2 ]
  run vm_runner_build_qemu_argv \
    $'demo\n-inject' 4 "$TEST_ROOT/seed.iso" "$TEST_ROOT/disk.qcow2" \
    /tmp/lm-vfs-test.sock 40222 43128 none '' ''
  [ "$status" -eq 2 ]
}

@test "SSH ignores user config and agent and distinguishes explicit PTY" {
  vm_ssh_build_argv "$TEST_ROOT/control-key" "$TEST_ROOT/known_hosts" 40222 noninteractive
  [ "${#VM_SSH_ARGV[@]}" -gt 0 ]
  [[ " ${VM_SSH_ARGV[*]} " == *' -F /dev/null '* ]]
  [[ " ${VM_SSH_ARGV[*]} " == *' IdentityAgent=none '* ]]
  [[ " ${VM_SSH_ARGV[*]} " == *' IdentitiesOnly=yes '* ]]
  [[ " ${VM_SSH_ARGV[*]} " == *' ForwardAgent=no '* ]]
  [[ " ${VM_SSH_ARGV[*]} " == *' ForwardX11=no '* ]]
  [[ " ${VM_SSH_ARGV[*]} " == *' ClearAllForwardings=yes '* ]]
  [[ " ${VM_SSH_ARGV[*]} " == *' -T '* ]]
  [[ " ${VM_SSH_ARGV[*]} " != *' -t '* ]]

  vm_ssh_build_argv "$TEST_ROOT/control-key" "$TEST_ROOT/known_hosts" 40222 interactive
  [ "${#VM_SSH_ARGV[@]}" -gt 0 ]
  [[ " ${VM_SSH_ARGV[*]} " == *' -tt '* ]]
}

@test "SSH control socket opt-in requires an owned private socket" {
  local rc
  python3 - "$TEST_ROOT/control.sock" <<'PY'
import os,socket,sys
listener=socket.socket(socket.AF_UNIX)
listener.bind(sys.argv[1])
os.chmod(sys.argv[1],0o600)
listener.close()
PY
  export VM_SSH_CONTROL_PATH="$TEST_ROOT/control.sock"
  vm_ssh_build_argv "$TEST_ROOT/control-key" "$TEST_ROOT/known_hosts" 40222 noninteractive
  [[ " ${VM_SSH_ARGV[*]} " == *" -S $TEST_ROOT/control.sock "* ]]

  chmod 644 "$TEST_ROOT/control.sock"
  if vm_ssh_build_argv "$TEST_ROOT/control-key" "$TEST_ROOT/known_hosts" 40222 noninteractive; then
    rc=0
  else
    rc=$?
  fi
  [ "$rc" -eq 2 ]

  chmod 600 "$TEST_ROOT/control.sock"
  mv "$TEST_ROOT/control.sock" "$TEST_ROOT/real.sock"
  ln -s "$TEST_ROOT/real.sock" "$TEST_ROOT/control.sock"
  if vm_ssh_build_argv "$TEST_ROOT/control-key" "$TEST_ROOT/known_hosts" 40222 noninteractive; then
    rc=0
  else
    rc=$?
  fi
  [ "$rc" -eq 2 ]
  unset VM_SSH_CONTROL_PATH
}

@test "partial Ed25519 key scan never creates or replaces known_hosts" {
  cat >"$TEST_ROOT/bin/ssh-keyscan" <<'SCAN'
#!/bin/sh
printf '%s\n' '127.0.0.1 ssh-ed25519 AAAA'
SCAN
  chmod 700 "$TEST_ROOT/bin/ssh-keyscan"
  export PATH="$TEST_ROOT/bin:$PATH"
  vm_ssh_listener_verified() { return 0; }
  run vm_ssh_trust_host_key "$TEST_ROOT/known_hosts" 40222 "$TEST_ROOT/registry.json" run-a qemu
  [ "$status" -eq 2 ]
  [ ! -e "$TEST_ROOT/known_hosts" ]

  printf '%s\n' 'established-key' >"$TEST_ROOT/known_hosts"
  chmod 600 "$TEST_ROOT/known_hosts"
  run vm_ssh_trust_host_key "$TEST_ROOT/known_hosts" 40222 "$TEST_ROOT/registry.json" run-a qemu
  [ "$status" -eq 2 ]
  [ "$(cat "$TEST_ROOT/known_hosts")" = established-key ]
}

@test "SSH readiness records fixed ownership and scan failure reasons without argv" {
  vm_ssh_listener_verified() { return 1; }
  if vm_ssh_trust_host_key "$TEST_ROOT/known_hosts" 40222 "$TEST_ROOT/registry.json" run-a qemu; then
    return 1
  fi
  [ "$VM_SSH_TRUST_STAGE" = listener-ownership-before-scan ]
  vm_ssh_listener_verified() { return 0; }
  VM_SSH_KEYSCAN_BIN=/bin/false
  if vm_ssh_trust_host_key "$TEST_ROOT/known_hosts" 40222 "$TEST_ROOT/registry.json" run-a qemu; then
    return 1
  fi
  [ "$VM_SSH_TRUST_STAGE" = keyscan-unavailable ]
  [ ! -e "$TEST_ROOT/known_hosts" ]
}

@test "remote command deadline bounds SSH transport" {
  cat >"$TEST_ROOT/bin/fake-ssh" <<'SSH'
#!/bin/sh
exec sleep 10
SSH
  chmod 700 "$TEST_ROOT/bin/fake-ssh"
  printf 'private-key\n' >"$TEST_ROOT/control-key"
  printf '127.0.0.1 ssh-ed25519 AAAA\n' >"$TEST_ROOT/known_hosts"
  chmod 600 "$TEST_ROOT/control-key" "$TEST_ROOT/known_hosts"
  VM_SSH_BIN="$TEST_ROOT/bin/fake-ssh"
  export VM_SSH_BIN
  start=$SECONDS
  run vm_ssh_exec "$TEST_ROOT/control-key" "$TEST_ROOT/known_hosts" 40222 \
    1 noninteractive mester@127.0.0.1 'true'
  elapsed=$((SECONDS-start))
  [ "$status" -eq 124 ]
  [ "$elapsed" -le 4 ]
}

@test "rollback signals only recorded startup-owned processes" {
  python3 "$ROOT/lib/vm/process.py" init --registry "$TEST_ROOT/runner-state.json" --run-id run-a
  python3 "$ROOT/lib/vm/process.py" run-logged \
    --registry "$TEST_ROOT/runner-state.json" --run-id run-a --role virtiofsd \
    --log "$TEST_ROOT/virtiofsd.log" -- python3 -c 'import time; time.sleep(30)' \
    >/dev/null 2>&1 &
  logger_job=$!
  python3 -c 'import time; time.sleep(30)' &
  foreign_job=$!
  for _ in {1..100}; do
    grep -q '"virtiofsd"' "$TEST_ROOT/runner-state.json" && break
    sleep 0.02
  done
  if ! vm_runner_rollback "$TEST_ROOT/runner-state.json" run-a; then
    return 1
  fi
  run python3 - "$ROOT/lib/vm/process.py" "$TEST_ROOT/runner-state.json" <<'PY'
import importlib.util, sys
spec=importlib.util.spec_from_file_location("vm_process",sys.argv[1])
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
result=module.registry_status(sys.argv[2],"run-a")
assert result["processes"]["virtiofsd"]["state"] == "stopped"
assert result["processes"]["virtiofsd_logger"]["state"] == "stopped"
PY
  [ "$status" -eq 0 ]
  run kill -0 "$foreign_job"
  [ "$status" -eq 0 ]
  kill "$foreign_job" 2>/dev/null || true
  wait "$foreign_job" 2>/dev/null || true
  wait "$logger_job" 2>/dev/null || true
}

@test "port collision retries only the new QEMU attempt and never signals the owner" {
  cat >"$TEST_ROOT/fake-qemu" <<'QEMU'
#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
counter = Path(os.environ["FAKE_QEMU_COUNT"])
count = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(count))
if count == 1:
    print("Could not set up host forwarding rule: Address already in use", file=sys.stderr)
    raise SystemExit(1)
time.sleep(30)
QEMU
  sed -i "1c#!$(command -v python3)" "$TEST_ROOT/fake-qemu"
  chmod 700 "$TEST_ROOT/fake-qemu"
  export FAKE_QEMU_COUNT="$TEST_ROOT/qemu-count"
  VM_RUNNER_QEMU_BIN="$TEST_ROOT/fake-qemu"
  export VM_RUNNER_QEMU_BIN

  python3 -c 'import socket,time; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(("127.0.0.1",43128)); s.listen(); time.sleep(30)' &
  owner_pid=$!
  python3 "$ROOT/lib/vm/process.py" init --registry "$TEST_ROOT/processes.json" --run-id collision
  export VM_TEST_PORT_INDEX="$TEST_ROOT/port-index"
  vm_runner_allocate_port() {
    local index=0
    [[ ! -f "$VM_TEST_PORT_INDEX" ]] || index="$(cat "$VM_TEST_PORT_INDEX")"
    ((index+=1))
    printf '%s\n' "$index" >"$VM_TEST_PORT_INDEX"
    if ((index == 1)); then printf '43128\n'; else printf '43129\n'; fi
  }
  if ! vm_runner_launch_qemu "$TEST_ROOT/processes.json" collision "$TEST_ROOT" demo 2 \
    "$TEST_ROOT/seed.iso" "$TEST_ROOT/disk.qcow2" /tmp/lm-vfs-collision.sock \
    43130 none '' ''; then
    return 1
  fi
  [ "$(cat "$VM_TEST_PORT_INDEX")" -eq 2 ]
  [ "$(cat "$FAKE_QEMU_COUNT")" -eq 2 ]
  run kill -0 "$owner_pid"
  [ "$status" -eq 0 ]
  vm_runner_rollback "$TEST_ROOT/processes.json" collision
  kill "$owner_pid" 2>/dev/null || true
  wait "$owner_pid" 2>/dev/null || true
}

@test "early QEMU exit preserves original status and bounded diagnostics" {
  cat >"$TEST_ROOT/fake-qemu" <<'QEMU'
#!/bin/sh
printf 'fixture early qemu failure\n' >&2
exit 37
QEMU
  chmod 700 "$TEST_ROOT/fake-qemu"
  VM_RUNNER_QEMU_BIN="$TEST_ROOT/fake-qemu"
  export VM_RUNNER_QEMU_BIN
  python3 "$ROOT/lib/vm/process.py" init --registry "$TEST_ROOT/processes.json" --run-id early
  vm_runner_allocate_port() { printf '43131\n'; }
  run vm_runner_launch_qemu "$TEST_ROOT/processes.json" early "$TEST_ROOT" demo 2 \
    "$TEST_ROOT/seed.iso" "$TEST_ROOT/disk.qcow2" /tmp/lm-vfs-early.sock \
    43130 none '' ''
  [ "$status" -eq 1 ]
  [ -f "$TEST_ROOT/failure.json" ]
  run python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["stage"]=="qemu" and d["cause_exit_code"]==37' "$TEST_ROOT/failure.json"
  [ "$status" -eq 0 ]
  grep -Fq 'fixture early qemu failure' "$TEST_ROOT/qemu_1.log"
}

@test "startup orders proxy virtiofs and QEMU and trusts only its owned SSH listener" {
  local profile="$TEST_ROOT/profile" workspace="$TEST_ROOT/workspace"
  mkdir -m 700 "$workspace"
  printf 'permission=false\n' >"$workspace/.mester-egress"
  printf 'fixture seed\n' >"$profile/seed.iso"
  printf 'fixture overlay\n' >"$profile/disk.qcow2"
  chmod 600 "$profile/seed.iso" "$profile/disk.qcow2"
  export FAKE_START_EVENTS="$TEST_ROOT/events"
  export FAKE_RUN_DIR="$profile/runs/fake-start-a"
  export FAKE_SOCKET_PATH="$TEST_ROOT/fake-socket-path"

  cat >"$TEST_ROOT/fake-proxy" <<'PROXY'
#!/usr/bin/env python3
import json, os, socket, sys, time
from pathlib import Path
args=sys.argv[1:]
assert args[args.index('--upstream-proxy')+1] == 'http://10.0.2.100:3128'
port_file=Path(args[args.index('--port-file')+1])
marker_file=Path(args[args.index('--reload-marker')+1])
listener=socket.socket()
listener.bind(('127.0.0.1',0))
listener.listen(4)
port=listener.getsockname()[1]
port_file.write_text(f'{port}\n')
port_file.chmod(0o600)
raw=Path('/proc/self/stat').read_text()
fields=raw[raw.rfind(')')+1:].split()
record={"schema_version":1,"pid":os.getpid(),"start_ticks":fields[19],
        "executable":os.readlink('/proc/self/exe'),"port":port}
marker_file.write_text(json.dumps(record,sort_keys=True,separators=(',',':'))+'\n')
marker_file.chmod(0o600)
with open(os.environ['FAKE_START_EVENTS'],'a') as stream: stream.write('proxy\n')
time.sleep(60)
PROXY
  sed -i "1c#!$(command -v python3)" "$TEST_ROOT/fake-proxy"
  chmod 700 "$TEST_ROOT/fake-proxy"

  cat >"$TEST_ROOT/fake-virtiofsd" <<'VFS'
#!/usr/bin/env python3
import os, socket, sys, time
from pathlib import Path
args=sys.argv[1:]
path=args[args.index('--socket-path')+1]
s=socket.socket(socket.AF_UNIX)
s.bind(path)
s.listen()
with open(os.environ['FAKE_START_EVENTS'],'a') as stream: stream.write('virtiofsd\n')
Path(os.environ['FAKE_SOCKET_PATH']).write_text(path)
time.sleep(60)
VFS
  sed -i "1c#!$(command -v python3)" "$TEST_ROOT/fake-virtiofsd"
  chmod 700 "$TEST_ROOT/fake-virtiofsd"

  cat >"$TEST_ROOT/fake-qemu" <<'QEMU'
#!/usr/bin/env python3
import os, re, socket, sys, time
from pathlib import Path
args=sys.argv[1:]
netdev=args[args.index('-netdev')+1]
match=re.search(r'hostfwd=tcp:127\.0\.0\.1:(\d+)-:22', netdev)
assert match
port=int(match.group(1))
listener=socket.socket()
listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
listener.bind(('127.0.0.1',port))
listener.listen()
assert Path(os.environ['FAKE_RUN_DIR'],'proxy.port').is_file()
assert Path(os.environ['FAKE_START_EVENTS']).read_text() == 'proxy\nvirtiofsd\n'
chardev=args[args.index('-chardev')+1]
socket_path=re.search(r'path=([^,]+)',chardev).group(1)
assert Path(socket_path).is_socket()
with open(os.environ['FAKE_START_EVENTS'],'a') as stream: stream.write('qemu\n')
time.sleep(60)
QEMU
  sed -i "1c#!$(command -v python3)" "$TEST_ROOT/fake-qemu"
  chmod 700 "$TEST_ROOT/fake-qemu"

  cat >"$TEST_ROOT/bin/ssh-keyscan" <<'SCAN'
#!/usr/bin/env python3
import base64, struct, sys
args=sys.argv[1:]
port=args[args.index('-p')+1]
blob=struct.pack('!I',11)+b'ssh-ed25519'+struct.pack('!I',32)+b'k'*32
print(f'[127.0.0.1]:{port} ssh-ed25519 {base64.b64encode(blob).decode()}')
SCAN
  sed -i "1c#!$(command -v python3)" "$TEST_ROOT/bin/ssh-keyscan"
  chmod 700 "$TEST_ROOT/bin/ssh-keyscan"

  VM_RUNNER_QEMU_BIN="$TEST_ROOT/fake-qemu"
  VM_RUNNER_VIRTIOFSD_BIN="$TEST_ROOT/fake-virtiofsd"
  VM_RUNNER_PROXY="$TEST_ROOT/fake-proxy"
  VM_RUNNER_SSH_TIMEOUT=5
  VM_SSH_KEYSCAN_BIN="$TEST_ROOT/bin/ssh-keyscan"
  VM_RUNNER_SOCKET_ROOT="$TEST_ROOT/s"
  export VM_RUNNER_QEMU_BIN VM_RUNNER_VIRTIOFSD_BIN VM_RUNNER_PROXY VM_RUNNER_SSH_TIMEOUT \
    VM_SSH_KEYSCAN_BIN VM_RUNNER_SOCKET_ROOT
  vm_runner_preflight() { return 0; }
  vm_proxy_upstream_args() {
    VM_PROXY_UPSTREAM_ARGS=(--upstream-proxy http://10.0.2.100:3128)
  }

  if ! vm_runner_start "$profile" fake-start-a demo "$workspace" \
    "$profile/seed.iso" "$profile/disk.qcow2" 2 none '' ''; then
    return 1
  fi
  [ "$VM_RUNNER_QEMU_ROLE" = qemu_1 ]
  [ -f "$FAKE_RUN_DIR/known_hosts" ]
  [ "$(stat -c '%a' "$FAKE_RUN_DIR/known_hosts")" = 600 ]
  [ "$(cat "$FAKE_START_EVENTS")" = $'proxy\nvirtiofsd\nqemu' ]
  vm_runner_rollback "$VM_RUNNER_PROCESS_REGISTRY" fake-start-a
}

@test "KVM-unavailable preflight refuses before allocating profile run state" {
  local workspace="$TEST_ROOT/workspace"
  mkdir -m 700 "$workspace"
  printf 'fixture seed\n' >"$TEST_ROOT/profile/seed.iso"
  printf 'fixture disk\n' >"$TEST_ROOT/profile/disk.qcow2"
  chmod 600 "$TEST_ROOT/profile/seed.iso" "$TEST_ROOT/profile/disk.qcow2"
  vm_runner_preflight() { vm_runner_require_kvm "$TEST_ROOT/not-a-kvm-device"; }
  run vm_runner_start "$TEST_ROOT/profile" kvm-refused demo "$workspace" \
    "$TEST_ROOT/profile/seed.iso" "$TEST_ROOT/profile/disk.qcow2" 2 none '' ''
  [ "$status" -eq 2 ]
  [ ! -e "$TEST_ROOT/profile/runs" ]
  [ ! -e "$TEST_ROOT/profile/processes.json" ]
}
