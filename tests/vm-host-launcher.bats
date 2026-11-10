#!/usr/bin/env bats

setup() {
  TEST_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
  TEST_TMP="$(mktemp -d)"
  TEST_OWNED_PROCESS_GROUP_FILE=""
  TEST_UNRELATED_PID_FILE=""
  TEST_CLEANUP_RUN_DIRS=()
  TEST_POLICY_TMP=""
  VM_HOST_SCRIPT_ROOT="$TEST_ROOT"
  source "$TEST_ROOT/lib/vm/host-launcher.sh"
  source "$TEST_ROOT/lib/vm/cloud-init.sh"
}

teardown() {
  local run_dir emergency_dir resource
  for run_dir in "${TEST_CLEANUP_RUN_DIRS[@]}"; do
    [[ -d "$run_dir" ]] || continue
    VM_HOST_RUN_DIR="$run_dir"
    emergency_dir="$run_dir.emergency"
    # These are independent synthetic bounded-log sleepers. Retry only their
    # saved exact identities, so cleanup still works if the code under test
    # removed a production identity before stopping its process.
    for resource in qemu virtiofsd proxy; do
      vm_host_cleanup_one "$emergency_dir/$resource.identity" "$run_dir/$resource.complete" \
        >/dev/null 2>&1 || true
    done
  done
  if [[ -n "$TEST_OWNED_PROCESS_GROUP_FILE" && -s "$TEST_OWNED_PROCESS_GROUP_FILE" ]]; then
    python3 - "$TEST_OWNED_PROCESS_GROUP_FILE" <<'PY' || true
import os, pathlib, signal, sys
try:
    group = int(pathlib.Path(sys.argv[1]).read_text().strip())
    with os.scandir("/proc") as entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                fields = pathlib.Path(entry.path, "stat").read_text().rsplit(")", 1)[1].split()
                if int(fields[2]) == group and int(fields[3]) == group:
                    os.killpg(group, signal.SIGKILL)
                    break
            except (FileNotFoundError, ProcessLookupError):
                pass
except (OSError, ValueError):
    pass
PY
  fi
  if [[ -n "$TEST_UNRELATED_PID_FILE" && -s "$TEST_UNRELATED_PID_FILE" ]]; then
    unrelated_pid="$(<"$TEST_UNRELATED_PID_FILE")"
    if [[ "$unrelated_pid" =~ ^[1-9][0-9]*$ ]]; then
      kill "$unrelated_pid" 2>/dev/null || true
      wait "$unrelated_pid" 2>/dev/null || true
    fi
  fi
  if [[ -n "$TEST_POLICY_TMP" && -d "$TEST_POLICY_TMP" ]]; then
    rm -rf -- "$TEST_POLICY_TMP"
  fi
  rm -rf -- "$TEST_TMP"
}

setup_virtiofsd_policy_test() {
  local policy_home
  policy_home="$(python3 -c 'import os,pwd; print(pwd.getpwuid(os.getuid()).pw_dir)')"
  TEST_POLICY_TMP="$(mktemp -d "$policy_home/.vm-host-launcher-test.XXXXXXXX")"
  TEST_POLICY_CALLS="$TEST_TMP/virtiofsd-policy-calls"
  TEST_POLICY_FORCE_OWNER_PATH=""
  TEST_REAL_PYTHON="$(builtin command -v python3)"
  : >"$TEST_POLICY_CALLS"
  unset -f virtiofsd 2>/dev/null || true
}

install_virtiofsd_policy_test_shim() {
  python3() {
    if [[ "${1:-}" == -I && "${2:-}" == "$VM_HOST_SCRIPT_ROOT/lib/vm/executable-policy.py" ]]; then
      printf '%s\n' "${3:-}" >>"$TEST_POLICY_CALLS"
      "$TEST_REAL_PYTHON" -I - "$VM_HOST_SCRIPT_ROOT/lib/vm/executable-policy.py" \
        "${3:-}" "$TEST_POLICY_FORCE_OWNER_PATH" <<'PY'
import importlib.util
import os
import pathlib
import sys

policy_path, candidate, synthetic_owner_path = sys.argv[1:]
spec = importlib.util.spec_from_file_location("executable_policy_test", policy_path)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
owners = {0, os.getuid()}
original_lstat = policy.os.lstat
if synthetic_owner_path:
    def lstat_with_untrusted_owner(raw_path, *args, **kwargs):
        info = original_lstat(raw_path, *args, **kwargs)
        if pathlib.Path(os.fsdecode(os.fspath(raw_path))) == pathlib.Path(synthetic_owner_path):
            values = list(info)
            values[4] = os.getuid() + 1
            return os.stat_result(values)
        return info
    policy.os.lstat = lstat_with_untrusted_owner
try:
    policy.validate_executable(
        candidate,
        directory_owners=owners,
        executable_owners=owners,
        symlink_owners=owners,
    )
except (OSError, ValueError) as error:
    print(f"REFUSED: {error}", file=sys.stderr)
    raise SystemExit(1)
finally:
    policy.os.lstat = original_lstat
PY
    else
      "$TEST_REAL_PYTHON" "$@"
    fi
  }
}

write_virtiofsd_test_executable() {
  local path="$1" directory="${1%/*}"
  mkdir -p "$directory"
  while [[ "$directory" == "$TEST_POLICY_TMP/"* ]]; do
    chmod 0755 "$directory"
    directory="${directory%/*}"
  done
  printf '#!/bin/sh\nexit 0\n' >"$path"
  chmod 0755 "$path"
}

assert_virtiofsd_refused() {
  local output_file="$1"
  if vm_host_resolve_virtiofsd >"$output_file" 2>&1; then
    printf 'virtiofsd discovery unexpectedly accepted a candidate\n' >&2
    return 1
  fi
  [ -z "$VM_HOST_VIRTIOFSD_EXECUTABLE" ]
}

write_cleanup_cascade_driver() {
  local path="$1"
  cat >"$path" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
umask 077
root="$1" run_dir="$2" scenario="$3" port_source="$4" attempt_log="$5" pid_file="$6"
emergency_dir="$run_dir.emergency"
mkdir -m 0700 "$emergency_dir"
source "$root/lib/vm/host-launcher.sh"
VM_HOST_SCRIPT_ROOT="$root"
VM_HOST_RUN_DIR="$run_dir"
VM_HOST_UNTRACKED_CHILD=0
VM_HOST_AUTH_STARTED=0
VM_HOST_SMOKE_COMPLETE_PENDING=1
export VM_HOST_SCRIPT_ROOT VM_HOST_RUN_DIR VM_HOST_UNTRACKED_CHILD VM_HOST_AUTH_STARTED VM_HOST_SMOKE_COMPLETE_PENDING

cleanup_one_definition="$(declare -f vm_host_cleanup_one)"
cleanup_one_definition="${cleanup_one_definition/vm_host_cleanup_one /vm_host_cleanup_one_exact }"
eval "$cleanup_one_definition"
vm_host_cleanup_one() {
  case "${1##*/}" in
    qemu.identity|virtiofsd.identity|proxy.identity) printf '%s\n' "${1##*/}" >>"$attempt_log" ;;
  esac
  vm_host_cleanup_one_exact "$@"
}

printf 'synthetic disk state\n' >"$run_dir/disk.qcow2"
printf 'synthetic auth state\n' >"$run_dir/user-data"
case "$scenario" in
  occupied)
    cat -- "$port_source" >"$run_dir/auth-callback.port"
    chmod 0600 "$run_dir/auth-callback.port"
    ;;
  missing-proof)
    : >"$run_dir/auth-session.complete"
    chmod 0600 "$run_dir/auth-session.complete"
    ;;
  invalid-proof)
    : >"$run_dir/auth-session.complete"
    printf 'not-json\n' >"$run_dir/auth-session.proof"
    chmod 0600 "$run_dir/auth-session.complete" "$run_dir/auth-session.proof"
    ;;
  *) exit 2 ;;
esac

python_exe="$(readlink -f -- "$(command -v python3)")"
for resource in qemu virtiofsd proxy; do
  python3 -I "$root/lib/vm/bounded-log.py" "$run_dir/$resource.log" 4096 \
    --completion-file "$run_dir/$resource.complete" -- /usr/bin/sleep 60 </dev/null &
  resource_pid="$!"
  printf '%s %s\n' "$resource" "$resource_pid" >>"$pid_file"
  vm_host_record_process_identity "$run_dir/$resource.identity" "$resource_pid" \
    "$python_exe" "$root/lib/vm/bounded-log.py"
  cp -- "$run_dir/$resource.identity" "$emergency_dir/$resource.identity"
done

trap vm_host_exit_cleanup EXIT
exit 0
SH
  chmod 0700 "$path"
}

assert_cleanup_cascade_result() {
  local run_dir="$1" attempt_log="$2" pid_file="$3" result_status="$4" result_output="$5" kind pid
  [ "$result_status" -eq 1 ]
  [[ "$result_output" != *"Smoke acceptance stages completed."* ]]
  [ "$(<"$attempt_log")" = $'qemu.identity\nvirtiofsd.identity\nproxy.identity' ]
  [ -d "$run_dir" ]
  [ "$(<"$run_dir/disk.qcow2")" = 'synthetic disk state' ]
  [ "$(<"$run_dir/user-data")" = 'synthetic auth state' ]
  while read -r kind pid; do
    [ ! -e "/proc/$pid" ] || { printf 'resource process remained: %s %s\n' "$kind" "$pid" >&2; return 1; }
    [ ! -e "$run_dir/$kind.identity" ]
    [ ! -e "$run_dir/$kind.complete" ]
  done <"$pid_file"
}

@test "canonical workspace rejects a symlinked selected path" {
  mkdir "$TEST_TMP/real-workspace"
  ln -s "$TEST_TMP/real-workspace" "$TEST_TMP/workspace-link"

  run vm_host_validate_workspace "$TEST_TMP/workspace-link" "$TEST_TMP/install" "$TEST_TMP/state"

  [ "$status" -ne 0 ]
  [[ "$output" == *"symlink"* ]]
}

@test "canonical workspace rejects overlap with host state or installed code" {
  mkdir -p "$TEST_TMP/workspace/project" "$TEST_TMP/workspace/.state" "$TEST_TMP/install"

  run vm_host_validate_workspace "$TEST_TMP/workspace" "$TEST_TMP/install" "$TEST_TMP/workspace/.state"

  [ "$status" -ne 0 ]
  [[ "$output" == *"overlap"* ]]
}

@test "workspace inside private socket runtime root is rejected before export" {
  local runtime_root="$TEST_TMP/private-runtime"
  mkdir -p "$runtime_root/workspace" "$TEST_TMP/install"

  run vm_host_validate_workspace "$runtime_root/workspace" "$TEST_TMP/install" \
    "$TEST_TMP/private-state" "$runtime_root"

  [ "$status" -ne 0 ]
  [[ "$output" == *"overlaps private short-socket runtime root"* ]]
}

@test "smoke rejects an export overlapping socket runtime before workspace checks or launch" {
  local runtime_root="$TEST_TMP/private-runtime" home="$TEST_TMP/home" marker="$TEST_TMP/lifecycle-reached"
  mkdir -p "$runtime_root/workspace" "$TEST_TMP/install" "$home"
  export VM_HOST_TEST_RUNTIME_ROOT="$runtime_root" VM_HOST_TEST_HOME="$home" VM_HOST_TEST_MARKER="$marker"
  eval "$(declare -f vm_host_validate_workspace | sed 's/vm_host_validate_workspace/vm_host_validate_workspace_real/')"
  vm_host_validate_workspace() {
    vm_host_validate_workspace_real "$1" "$2" "$3" "$VM_HOST_TEST_RUNTIME_ROOT"
  }
  vm_host_require_kvm() { :; }
  vm_host_require_commands() { :; }
  vm_host_account_home() { printf '%s\n' "$VM_HOST_TEST_HOME"; }
  vm_host_check_workspace_filesystem() { : >"$VM_HOST_TEST_MARKER"; }
  VM_HOST_SCRIPT_ROOT="$TEST_TMP/install"

  run vm_host_smoke --workspace "$runtime_root/workspace" --model openai/test-model

  [ "$status" -ne 0 ]
  [[ "$output" == *"overlaps private short-socket runtime root"* ]]
  [ ! -e "$marker" ]
}

@test "private state directory is created without following a symlink" {
  mkdir "$TEST_TMP/home" "$TEST_TMP/target"
  chmod 0755 "$TEST_TMP/home"
  ln -s "$TEST_TMP/target" "$TEST_TMP/home/.local"

  run vm_host_prepare_state_root "$TEST_TMP/home"

  [ "$status" -ne 0 ]
  [[ "$output" == *"REFUSED"* || "$output" == *"Too many levels"* ]]
  [ ! -e "$TEST_TMP/target/state" ]
}

@test "browser-auth model selector accepts only bounded OpenAI model identifiers" {
  run vm_host_validate_model 'openai/gpt-6-luna#max'
  [ "$status" -eq 0 ]

  run vm_host_validate_model 'openai/gpt-6-luna#max;touch /tmp/not-allowed'
  [ "$status" -ne 0 ]
  [[ "$output" != *"not-allowed"* ]]
}

@test "host callback cleanup rejects IPv4-mapped IPv6 listeners and verifies closure" {
  local port_file="$TEST_TMP/listener-port" pid
  python3 - "$port_file" <<'PY' &
import os, socket, sys, time
listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
listener.bind(("::ffff:127.0.0.1", 0))
listener.listen(1)
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as stream:
    stream.write(str(listener.getsockname()[1]) + "\n")
    stream.flush()
while True:
    time.sleep(1)
PY
  pid="$!"
  for _ in {1..100}; do
    [[ -s "$port_file" ]] && break
    sleep 0.01
  done
  [ -s "$port_file" ]
  chmod 0600 "$port_file"

  run vm_host_auth_listener_closed "$port_file"
  [ "$status" -ne 0 ]

  kill "$pid"
  wait "$pid" 2>/dev/null || true
  run vm_host_auth_listener_closed "$port_file"
  [ "$status" -eq 0 ]
}

@test "resource envelope is bounded and does not select guest admin or nested KVM" {
  run vm_host_resource_limits

  [ "$status" -eq 0 ]
  [[ "$output" == *'"cpus":2'* ]]
  [[ "$output" == *'"memory_mib":4096'* ]]
  [[ "$output" == *'"disk_gib":24'* ]]
  [[ "$output" == *'"startup_timeout_seconds":300'* ]]
  [[ "$output" == *'"ssh_control_timeout_seconds":30'* ]]
  [[ "$output" != *"nested"* ]]
}

@test "workspace filesystem bound requires a mount root no larger than eight GiB" {
  run vm_host_workspace_capacity_allowed yes 8589934592
  [ "$status" -eq 0 ]

  run vm_host_workspace_capacity_allowed yes 8589934593
  [ "$status" -ne 0 ]
  [[ "$output" == *"exceeds the 8-GiB"* ]]

  run vm_host_workspace_capacity_allowed no 1048576
  [ "$status" -ne 0 ]
  [[ "$output" == *"dedicated owner-provisioned bounded filesystem"* ]]
}

@test "smoke refuses an unbounded workspace before allocating runtime state" {
  local prepared="$TEST_TMP/state-was-prepared"
  vm_host_require_kvm() { return 0; }
  vm_host_require_commands() { return 0; }
  vm_host_account_home() { printf '%s\n' "$TEST_TMP"; }
  vm_host_validate_workspace() { printf '%s\n' "$TEST_TMP/workspace"; }
  vm_host_check_workspace_filesystem() { return 73; }
  vm_host_is_under() { return 1; }
  vm_host_prepare_state_root() { touch "$prepared"; printf '%s/state\n' "$TEST_TMP"; }

  run vm_host_smoke --workspace "$TEST_TMP/workspace" --model openai/test-model

  [ "$status" -ne 0 ]
  [ ! -e "$prepared" ]
  [[ "$output" != *"VM_UP"* ]]
}

@test "guest cloud-init uses rootless Docker and direct OpenCode without lifecycle authority" {
  unset HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy
  run vm_host_cloud_init smoke-v3 1234 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE7zJmK1W1h3V4eA7qQKX8U4n9x0z8m1c2b3d4e5f6g7 test'

  [ "$status" -eq 0 ] || {
    printf 'cloud-init generation failed (status %s); captured output follows:\n%s\n' "$status" "$output" >&2
    return 1
  }
  [[ "$output" == *"export http_proxy=http://10.0.2.100:3128 https_proxy=http://10.0.2.100:3128"* ]]
  [[ "$output" == *"docker-ce-rootless-extras"* ]]
  [[ "$output" == *"dockerd-rootless-setuptool.sh install"* ]]
  [[ "$output" == *"mester-v3-user-dirs.py prepare"* ]]
  [[ "$output" == *"mester-v3-user-dirs.py verify-write"* ]]
  [[ "$output" == *"systemctl --user show-environment"* ]]
  [[ "$output" == *"timeout --signal=TERM --kill-after=1s 2s runuser"* ]]
  [[ "$output" != *"install -d -o mester -g mester -m 0700 /home/mester/.config/systemd/user/docker.service.d"* ]]
  [[ "$output" == *"systemctl disable --now docker.service docker.socket containerd.service"* ]]
  [[ "$output" == *"systemctl --user enable --now docker"* ]]
  [[ "$output" == *"opencode"* ]]
  [[ "$output" == *"@opencode/cli@2.2.5"* ]]
  [[ "$output" != *"/dev/kvm"* ]]
  [[ "$output" != *"VM_CREATE"* ]]
  [[ "$output" != *"recursion"* ]]

  export HTTP_PROXY=http://hostile.invalid:9999 HTTPS_PROXY=http://hostile.invalid:9999
  export http_proxy=http://hostile.invalid:9999 https_proxy=http://hostile.invalid:9999
  export NO_PROXY=hostile.invalid no_proxy=hostile.invalid
  run vm_host_cloud_init smoke-v3 1234 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE7zJmK1W1h3V4eA7qQKX8U4n9x0z8m1c2b3d4e5f6g7 test'

  [ "$status" -eq 0 ]
  [[ "$output" == *"export http_proxy=http://10.0.2.100:3128 https_proxy=http://10.0.2.100:3128"* ]]
  [[ "$output" != *"hostile.invalid"* ]]
}

@test "fresh run directories use exclusive identities and preserve stale socket/PID evidence" {
  local state_root="$TEST_TMP/private-state" old_dir first unrelated_pid token_counter="$TEST_TMP/token-counter" calls
  mkdir -m 0700 "$state_root"
  old_dir="$state_root/20261109T154005Z-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  mkdir -m 0700 "$old_dir"
  printf 'preserve historical marker\n' >"$old_dir/postmortem.txt"
  sleep 30 &
  unrelated_pid=$!
  printf '%s\n' "$unrelated_pid" >"$old_dir/virtiofs.sock.pid"
  chmod 0600 "$old_dir/virtiofs.sock.pid"

  printf '0\n' >"$token_counter"
  date() { printf '20261109T154005Z\n'; }
  python3() {
    read -r calls <"$token_counter"
    calls=$((calls + 1))
    printf '%s\n' "$calls" >"$token_counter"
    if (( calls == 1 )); then printf 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n'; else printf 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n'; fi
  }
  first="$(vm_host_create_run_dir "$state_root")"

  [ "$first" = "$state_root/20261109T154005Z-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" ]
  [ "$(stat -c %a "$first")" = 700 ]
  [ "$(<"$old_dir/postmortem.txt")" = 'preserve historical marker' ]
  [ "$(<"$old_dir/virtiofs.sock.pid")" = "$unrelated_pid" ]
  kill -0 "$unrelated_pid"

  kill "$unrelated_pid"
  wait "$unrelated_pid" 2>/dev/null || true
}

@test "virtiofs startup refuses stale socket or PID paths without trusting their PID" {
  local run_dir="$TEST_TMP/stale-virtiofs" pid
  mkdir -m 0700 "$run_dir"
  sleep 30 &
  pid=$!
  printf '%s\n' "$pid" >"$run_dir/virtiofs.sock.pid"
  chmod 0600 "$run_dir/virtiofs.sock.pid"
  : >"$run_dir/virtiofs.sock"
  VM_HOST_VIRTIOFSD_EXECUTABLE=/usr/bin/true

  run vm_host_start_virtiofs "$run_dir" "$TEST_TMP"

  [ "$status" -ne 0 ]
  [[ "$output" == *'could not securely allocate a short per-run virtiofs socket path'* ]]
  [ -e "$run_dir/virtiofs.sock" ]
  [ "$(<"$run_dir/virtiofs.sock.pid")" = "$pid" ]
  kill -0 "$pid"
  kill "$pid"
  wait "$pid" 2>/dev/null || true
}

@test "generated cloud-init uses cloud-init 26.1 users-groups schema for the host UID" {
  local user_data
  user_data="$(vm_host_cloud_init smoke-v3 1234 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE7zJmK1W1h3V4eA7qQKX8U4n9x0z8m1c2b3d4e5f6g7 test')"

  # cloud-init 26.1's users-groups schema accepts users[].uid as an integer,
  # primary_group as a string, and ssh_authorized_keys as a non-empty string
  # list. An omitted users[].groups uses the default empty supplementary set;
  # if specified, that list must be non-empty. Top-level groups must be group
  # names (or a name-to-members mapping), not {name, gid} records.
  [[ "$user_data" == *$'users:\n  - name: mester\n    uid: 1234\n    primary_group: mester\n    shell: /bin/bash\n    lock_passwd: true\n    ssh_authorized_keys:\n      - ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE7zJmK1W1h3V4eA7qQKX8U4n9x0z8m1c2b3d4e5f6g7 test\n'* ]]
  [[ "$user_data" == *$'groups:\n  - mester\n'* ]]
  [[ "$user_data" != *$'    groups: []\n'* ]]
  [[ "$user_data" == *$'ssh_pwauth: false\n'* ]]
  [[ "$user_data" == *$'disable_root: true\n'* ]]

  run vm_host_cloud_init 'bad/name' 1234 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE7zJmK1W1h3V4eA7qQKX8U4n9x0z8m1c2b3d4e5f6g7 test'
  [ "$status" -eq 2 ]
  run vm_host_cloud_init smoke-v3 0 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE7zJmK1W1h3V4eA7qQKX8U4n9x0z8m1c2b3d4e5f6g7 test'
  [ "$status" -eq 2 ]
  run vm_host_cloud_init smoke-v3 1234 'not-an-ssh-key'
  [ "$status" -eq 2 ]
}

@test "cloud-init creates the primary group before a user with primary_group" {
  run vm_host_cloud_init smoke-v3 1234 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE7zJmK1W1h3V4eA7qQKX8U4n9x0z8m1c2b3d4e5f6g7 fixture'

  [ "$status" -eq 0 ]
  [[ "$output" == *$'groups:\n  - mester\nusers:\n'* ]]
  [[ "$output" == *'name: mester'* ]]
  [[ "$output" == *'uid: 1234'* ]]
  [[ "$output" == *'primary_group: mester'* ]]
  [[ "$output" == *'lock_passwd: true'* ]]
  [[ "$output" == *$'ssh_pwauth: false\n'* ]]
  [[ "$output" == *$'disable_root: true\n'* ]]
}

@test "runbook evidence command writes through its exclusive open descriptor" {
  local evidence="$TEST_TMP/v3-smoke-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.log"
  local fake_command="$TEST_TMP/fake-smoke" marker="$TEST_TMP/fake-smoke-ran"
  cat >"$fake_command" <<'SH'
#!/usr/bin/bash
printf 'FAKE_COMMAND_RAN\n' >"$1"
printf 'SMOKE_OUTPUT_CAPTURED\n'
SH
  chmod 0700 "$fake_command"
  local procedure
  procedure="$(python3 - "$TEST_ROOT/docs/vm-v3-phase1-smoke.md" <<'PY'
import pathlib, sys

text = pathlib.Path(sys.argv[1]).read_text()
start = text.index('EVIDENCE="$PRIVATE_DIR/v3-smoke-$CANDIDATE_SHA.log"\n')
end = text.index("\n```", start)
block = text[start:end]
original = 'if "$INSTALL_ROOT/lab-vm" smoke --workspace "$WORKSPACE" --model "$MODEL" >&"$evidence_fd" 2>&1; then'
replacement = 'if "$FAKE_COMMAND" "$FAKE_MARKER" >&"$evidence_fd" 2>&1; then'
assert original in block
block = block.replace(original, replacement, 1)
block += '\nif { exec {reopen_fd}> "$EVIDENCE"; } 2>/dev/null; then exit 91; fi\n'
print("set -euo pipefail\n" + block)
PY
)"

  run env PRIVATE_DIR="$TEST_TMP" CANDIDATE_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
    MODEL=openai/test-model FAKE_COMMAND="$fake_command" FAKE_MARKER="$marker" bash -c "$procedure"

  [ "$status" -eq 0 ]
  [ "$(<"$marker")" = 'FAKE_COMMAND_RAN' ]
  [ "$(stat -c %a "$evidence")" = 600 ]
  [[ "$output" == *'SMOKE_OUTPUT_CAPTURED'* ]]
}

@test "ChatGPT browser-auth regressions cover OAuth validation and live-only URL handling" {
  run python3 -I "$TEST_ROOT/tests/vm-browser-auth.test.py"

  [ "$status" -eq 0 ] || {
    printf 'browser-auth regression failed (status %s); output follows:\n%s\n' "$status" "$output" >&2
    return 1
  }
  [[ "$output" == *'BROWSER_AUTH_TESTS_OK'* ]]
}

@test "Phase 1 runbook requires ChatGPT browser login, not an API key" {
  local doc="$TEST_ROOT/docs/vm-v3-phase1-smoke.md"
  grep -Fq 'opencode auth login openai --method chatgpt-browser' "$doc"
  grep -Fq 'No API key or API credits are used' "$doc"
  grep -Fq 'http://localhost:1455/auth/callback' "$doc"
  grep -Fq 'http://localhost:1457/auth/callback' "$doc"
  grep -Fq 'org.freedesktop.portal.OpenURI' "$doc"
  grep -Fq "MODEL='openai/gpt-6-luna#max'" "$doc"
  grep -Fq 'opencode auth logout openai' "$doc"
  grep -Fq 'not from other processes running as `mester`, guest root' "$doc"
  grep -Fq 'does not establish remote session revocation or secure erasure' "$doc"
  grep -Fq 'may invalidate the working old-VM session' "$doc"
  grep -Fq 'Persistence across VM restart remains out of scope and is a separate concern' "$doc"
  ! grep -Fq -- '--credentials-file' "$doc"
  ! grep -Fq 'api_key' "$doc"
}

@test "runbook ENOSPC boundary logic makes at most one extra byte attempt" {
  run python3 - "$TEST_ROOT/docs/vm-v3-phase1-smoke.md" <<'PY'
import ast, errno, os, pathlib, sys

text = pathlib.Path(sys.argv[1]).read_text()
marker = '/usr/bin/python3 -I - "$WORKSPACE" <<\'PY\'\n'
start = text.index(marker) + len(marker)
end = text.index("\nPY\n", start)
tree = ast.parse(text[start:end])
function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name == "write_until_enospc")
def fail(message): raise SystemExit(message)
namespace = {"os": os, "errno": errno, "fail": fail}
exec(compile(ast.Module(body=[function], type_ignores=[]), "<runbook-enospc-function>", "exec"), namespace)
write_until_enospc = namespace["write_until_enospc"]

requests = []
def boundary_writer(fd, data):
    requests.append(len(data))
    if len(data) == 1:
        raise OSError(errno.ENOSPC, "mock full")
    return len(data)
written, attempted = write_until_enospc(99, 8, boundary_writer)
assert (written, attempted, requests) == (8, 9, [8, 1])

early_requests = []
def early_writer(fd, data):
    early_requests.append(len(data))
    raise OSError(errno.ENOSPC, "mock full early")
written, attempted = write_until_enospc(99, 8, early_writer)
assert (written, attempted, early_requests) == (0, 8, [8])
print("RUNBOOK_ENOSPC_BOUND_MOCK_OK")
PY

  [ "$status" -eq 0 ]
  [[ "$output" == *'RUNBOOK_ENOSPC_BOUND_MOCK_OK'* ]]
}

@test "runbook protected-install dirfd logic refuses unsafe paths and existing targets" {
  run python3 - "$TEST_ROOT/docs/vm-v3-phase1-smoke.md" "$TEST_TMP" <<'PY'
import ast, os, pathlib, re, stat, sys

text = pathlib.Path(sys.argv[1]).read_text()
marker = 'sudo /usr/bin/python3 -I - "$CANDIDATE_SHA" <<\'PY\'\n'
start = text.index(marker) + len(marker)
end = text.index("\nPY\n", start)
tree = ast.parse(text[start:end])
names = {"refuse", "open_protected_dir", "create_install_root"}
functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
assert {node.name for node in functions} == names
namespace = {"os": os, "re": re, "stat": stat}
exec(compile(ast.Module(body=functions, type_ignores=[]), "<runbook-install-functions>", "exec"), namespace)
open_protected_dir = namespace["open_protected_dir"]
create_install_root = namespace["create_install_root"]

base = pathlib.Path(sys.argv[2]) / "runbook-install-fixture"
base.mkdir(mode=0o700)
outside = pathlib.Path(sys.argv[2]) / "runbook-install-outside"
outside.mkdir(mode=0o700)
sentinel = outside / "sentinel"
sentinel.write_text("preserve")
os.symlink(outside, base / "linked")
fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
uid = os.getuid()
try:
    try:
        open_protected_dir(fd, "linked", uid, create_missing=True)
    except SystemExit:
        pass
    else:
        raise AssertionError("symlink ancestor accepted")
    writable = base / "writable"
    writable.mkdir(mode=0o700)
    writable.chmod(0o777)
    try:
        open_protected_dir(fd, "writable", uid, create_missing=True)
    except SystemExit:
        pass
    else:
        raise AssertionError("group/world-writable ancestor accepted")
    created_fd = open_protected_dir(fd, "safe-created", uid, create_missing=True)
    created = os.fstat(created_fd)
    os.close(created_fd)
    assert created.st_uid == uid and stat.S_IMODE(created.st_mode) == 0o755
    sha = "a" * 40
    existing = base / sha
    existing.mkdir(mode=0o700)
    (existing / "keep").write_text("do not overwrite")
    try:
        create_install_root(fd, sha, uid)
    except SystemExit:
        pass
    else:
        raise AssertionError("existing candidate tree accepted")
    assert (existing / "keep").read_text() == "do not overwrite"
    new_fd = create_install_root(fd, "b" * 40, uid)
    os.close(new_fd)
finally:
    os.close(fd)
assert sentinel.read_text() == "preserve"
archive_start = text.index("git -C \"$REPO\" archive --format=tar \"$CANDIDATE_SHA\"", start)
tar_extract = text.index("| sudo /usr/bin/python3", archive_start)
assert start < archive_start < tar_extract
assert "os.umask(0o077)" in text[tar_extract:]
assert '"--no-same-permissions"' in text[tar_extract:]
assert "os.fchown" in text[tar_extract:]
assert "os.fchmod" in text[tar_extract:]
assert "sudo install -d" not in text[start:tar_extract]
print("RUNBOOK_INSTALL_GUARD_MOCK_OK")
PY

  [ "$status" -eq 0 ]
  [[ "$output" == *'RUNBOOK_INSTALL_GUARD_MOCK_OK'* ]]
}

@test "runbook normalizes permissions after real archive extraction" {
  run python3 - "$TEST_ROOT/docs/vm-v3-phase1-smoke.md" "$TEST_TMP" <<'PY'
import io, os, pathlib, shlex, stat, subprocess, sys, tarfile

text = pathlib.Path(sys.argv[1]).read_text()
marker = 'sudo /usr/bin/python3 -I - "$INSTALL_ROOT" 0 0 <<\'PY\'\n'
start = text.index(marker) + len(marker)
end = text.index("\nPY\n", start)
normalizer = text[start:end]
root = pathlib.Path(sys.argv[2]) / "archive-install"
root.mkdir(mode=0o700)
archive = pathlib.Path(sys.argv[2]) / "permissive-install.tar"
directories = ["lib", "lib/vm"]
files = {
    "lab-vm": 0o777,
    "lib/vm/host-launcher.sh": 0o666,
    "lib/vm/cloud-init.sh": 0o666,
    "lib/vm/proxy.py": 0o666,
    "lib/vm/bounded-log.py": 0o666,
    "lib/vm/executable-policy.py": 0o666,
    "lib/vm/browser-auth-session.py": 0o666,
    "lib/vm/virtiofs-socket.py": 0o666,
    "lib/vm/guest-user-dirs.py": 0o666,
}
with tarfile.open(archive, "w") as bundle:
    for directory in directories:
        entry = tarfile.TarInfo(directory)
        entry.type = tarfile.DIRTYPE
        entry.mode = 0o777
        bundle.addfile(entry)
    for name, mode in files.items():
        payload = ("fixture:" + name + "\n").encode()
        entry = tarfile.TarInfo(name)
        entry.size = len(payload)
        entry.mode = mode
        bundle.addfile(entry, io.BytesIO(payload))

extract_line = next(
    line.strip().split("|", 1)[1].strip()
    for line in text.splitlines()
    if line.lstrip().startswith("| sudo /usr/bin/python3")
)
extract_command = shlex.split(extract_line)
assert extract_command[0] == "sudo"
extract_command = extract_command[1:]
assert extract_command[-1] == "$INSTALL_ROOT"
extract_command[-1] = str(root)
subprocess.run(extract_command, input=archive.read_bytes(), check=True)

expected_paths = [root, root / "lib", root / "lib/vm"] + [root / name for name in files]
for path in expected_paths:
    mode = stat.S_IMODE(path.lstat().st_mode)
    assert mode & (stat.S_IWGRP | stat.S_IWOTH) == 0, (path, oct(mode))

subprocess.run([
    sys.executable, "-I", "-c", normalizer, str(root), str(os.getuid()), str(os.getgid())
], check=True)

expected_modes = {
    root: 0o755,
    root / "lib": 0o755,
    root / "lib/vm": 0o755,
    root / "lab-vm": 0o755,
    **{root / name: 0o644 for name in files if name != "lab-vm"},
}
for path, expected_mode in expected_modes.items():
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
    assert (info.st_uid, info.st_gid) == (os.getuid(), os.getgid()), path
    assert stat.S_IMODE(info.st_mode) == expected_mode, (path, oct(stat.S_IMODE(info.st_mode)))
assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) == sorted(
    ["lib", "lib/vm", *files]
)
print("RUNBOOK_ARCHIVE_PERMISSION_NORMALIZATION_OK")
PY

  [ "$status" -eq 0 ]
  [[ "$output" == *'RUNBOOK_ARCHIVE_PERMISSION_NORMALIZATION_OK'* ]]
}

@test "host command preflight rejects untrusted mv and cat before lifecycle stages" {
  local unsafe_command fake_command marker
  for unsafe_command in mv cat; do
    fake_command="$TEST_TMP/untrusted-$unsafe_command"
    marker="$TEST_TMP/$unsafe_command-was-invoked"
    cat >"$fake_command" <<SH
#!/bin/sh
printf 'invoked\\n' >'$marker'
exec /usr/bin/$unsafe_command "\$@"
SH
    chmod 0700 "$fake_command"

    run bash -c '
      root="$1" test_tmp="$2" unsafe_command="$3" fake_command="$4"
      trusted_python="$(builtin command -v python3)"
      source "$root/lib/vm/host-launcher.sh"
      VM_HOST_SCRIPT_ROOT="$root"
      vm_host_require_kvm() { :; }
      vm_host_wait_ssh() { printf "KEY_PROMOTION_REACHED\\n" >&2; }
      command() {
        if [[ "${1:-}" == -v ]]; then
          if [[ "${2:-}" == "$unsafe_command" ]]; then
            printf "%s\\n" "$fake_command"
          elif [[ "${2:-}" == python3 ]]; then
            printf "%s\\n" "$trusted_python"
          else
            printf "/usr/bin/true\\n"
          fi
          return 0
        fi
        builtin command "$@"
      }
      vm_host_smoke --workspace "$test_tmp/workspace" --model openai/test-model
    ' _ "$TEST_ROOT" "$TEST_TMP" "$unsafe_command" "$fake_command"

    [ "$status" -ne 0 ]
    [[ "$output" == *"host command is not from a protected root-owned path: $unsafe_command"* ]]
    [[ "$output" != *"KEY_PROMOTION_REACHED"* ]]
    [ ! -e "$marker" ]
  done
}

@test "virtiofsd discovery prefers the trusted PATH executable and validates its exact path" {
  setup_virtiofsd_policy_test
  install_virtiofsd_policy_test_shim
  local path_dir="$TEST_POLICY_TMP/path-bin" fallback_dir="$TEST_POLICY_TMP/usr/lib"
  local path_executable="$path_dir/virtiofsd" fallback_executable="$fallback_dir/virtiofsd"
  write_virtiofsd_test_executable "$path_executable"
  write_virtiofsd_test_executable "$fallback_executable"
  VM_HOST_TRUSTED_PATH="$path_dir"
  VM_HOST_VIRTIOFSD_PATHS=("$fallback_executable")

  vm_host_resolve_virtiofsd >"$TEST_TMP/virtiofsd-resolve.out" 2>&1 || {
    cat "$TEST_TMP/virtiofsd-resolve.out" >&2
    return 1
  }

  [ "$VM_HOST_VIRTIOFSD_EXECUTABLE" = "$path_executable" ]
  [ "$(<"$TEST_POLICY_CALLS")" = "$path_executable" ]
}

@test "virtiofsd discovery skips an unsafe trusted PATH candidate for an acceptable fallback" {
  setup_virtiofsd_policy_test
  install_virtiofsd_policy_test_shim
  local unsafe_dir="$TEST_TMP/unsafe-bin" unsafe_candidate="$TEST_TMP/unsafe-bin/virtiofsd"
  local safe_candidate="$TEST_POLICY_TMP/usr/lib/virtiofsd" expected_calls
  write_virtiofsd_test_executable "$unsafe_candidate"
  write_virtiofsd_test_executable "$safe_candidate"
  VM_HOST_TRUSTED_PATH="$unsafe_dir"
  VM_HOST_VIRTIOFSD_PATHS=("$safe_candidate")

  vm_host_resolve_virtiofsd >"$TEST_TMP/virtiofsd-resolve.out" 2>&1 || {
    cat "$TEST_TMP/virtiofsd-resolve.out" >&2
    return 1
  }

  expected_calls="$(printf '%s\n%s' "$unsafe_candidate" "$safe_candidate")"
  [ "$VM_HOST_VIRTIOFSD_EXECUTABLE" = "$safe_candidate" ]
  [ "$(<"$TEST_POLICY_CALLS")" = "$expected_calls" ]
}

@test "virtiofsd discovery supports the evidenced Arch, exec, and QEMU package paths" {
  setup_virtiofsd_policy_test
  install_virtiofsd_policy_test_shim
  [ "${VM_HOST_VIRTIOFSD_PATHS[*]}" = '/usr/lib/virtiofsd /usr/libexec/virtiofsd /usr/lib/qemu/virtiofsd' ]
  VM_HOST_TRUSTED_PATH="$TEST_POLICY_TMP/empty-path"
  mkdir -p "$VM_HOST_TRUSTED_PATH"

  local relative fixture candidate
  for relative in usr/lib/virtiofsd usr/libexec/virtiofsd usr/lib/qemu/virtiofsd; do
    fixture="$TEST_POLICY_TMP/fixture-${relative//\//-}"
    candidate="$fixture/$relative"
    write_virtiofsd_test_executable "$candidate"
    VM_HOST_VIRTIOFSD_PATHS=(
      "$fixture/usr/lib/virtiofsd"
      "$fixture/usr/libexec/virtiofsd"
      "$fixture/usr/lib/qemu/virtiofsd"
    )
    : >"$TEST_POLICY_CALLS"

    vm_host_resolve_virtiofsd >"$TEST_TMP/virtiofsd-resolve.out" 2>&1 || {
      cat "$TEST_TMP/virtiofsd-resolve.out" >&2
      return 1
    }

    [ "$VM_HOST_VIRTIOFSD_EXECUTABLE" = "$candidate" ]
    [ "$(<"$TEST_POLICY_CALLS")" = "$candidate" ]
  done
}

@test "virtiofsd discovery refuses missing and invalid candidate files" {
  setup_virtiofsd_policy_test
  install_virtiofsd_policy_test_shim
  VM_HOST_TRUSTED_PATH="$TEST_POLICY_TMP/empty-path"
  mkdir -p "$VM_HOST_TRUSTED_PATH"
  VM_HOST_VIRTIOFSD_PATHS=("$TEST_POLICY_TMP/missing/virtiofsd")

  assert_virtiofsd_refused "$TEST_TMP/virtiofsd-refusal.out"
  [[ "$(<"$TEST_TMP/virtiofsd-refusal.out")" == *'required host command missing: virtiofsd'* ]]
  [ ! -s "$TEST_POLICY_CALLS" ]

  local invalid="$TEST_POLICY_TMP/invalid/virtiofsd"
  write_virtiofsd_test_executable "$invalid"
  chmod 0644 "$invalid"
  VM_HOST_VIRTIOFSD_PATHS=("$invalid")
  assert_virtiofsd_refused "$TEST_TMP/virtiofsd-refusal.out"
  [[ "$(<"$TEST_TMP/virtiofsd-refusal.out")" == *'virtiofsd executable is not from a protected root-owned path'* ]]
  [ "$(<"$TEST_POLICY_CALLS")" = "$invalid" ]
}

@test "virtiofsd discovery propagates unsafe ownership, writable ancestry, and symlink refusal" {
  setup_virtiofsd_policy_test
  install_virtiofsd_policy_test_shim
  VM_HOST_TRUSTED_PATH="$TEST_POLICY_TMP/empty-path"
  mkdir -p "$VM_HOST_TRUSTED_PATH"

  local owner_dir="$TEST_POLICY_TMP/unsafe-owner" owner_candidate="$TEST_POLICY_TMP/unsafe-owner/bin/virtiofsd"
  write_virtiofsd_test_executable "$owner_candidate"
  VM_HOST_VIRTIOFSD_PATHS=("$owner_candidate")
  TEST_POLICY_FORCE_OWNER_PATH="$owner_dir"
  assert_virtiofsd_refused "$TEST_TMP/virtiofsd-refusal.out"
  [[ "$(<"$TEST_TMP/virtiofsd-refusal.out")" == *'virtiofsd executable is not from a protected root-owned path'* ]]
  [ "$(<"$TEST_POLICY_CALLS")" = "$owner_candidate" ]

  local writable_dir="$TEST_POLICY_TMP/unsafe-writable" writable_candidate="$TEST_POLICY_TMP/unsafe-writable/bin/virtiofsd"
  write_virtiofsd_test_executable "$writable_candidate"
  chmod 0777 "$writable_dir"
  TEST_POLICY_FORCE_OWNER_PATH=''
  VM_HOST_VIRTIOFSD_PATHS=("$writable_candidate")
  : >"$TEST_POLICY_CALLS"
  assert_virtiofsd_refused "$TEST_TMP/virtiofsd-refusal.out"
  [ "$(<"$TEST_POLICY_CALLS")" = "$writable_candidate" ]

  local safe_dir="$TEST_POLICY_TMP/safe-alias/bin" unsafe_target="$TEST_POLICY_TMP/unsafe-target/bin/virtiofsd"
  local symlink_candidate="$safe_dir/virtiofsd"
  write_virtiofsd_test_executable "$unsafe_target"
  chmod 0777 "${unsafe_target%/bin/virtiofsd}"
  mkdir -p "$safe_dir"
  chmod 0755 "$TEST_POLICY_TMP/safe-alias" "$safe_dir"
  ln -s "$unsafe_target" "$symlink_candidate"
  VM_HOST_VIRTIOFSD_PATHS=("$symlink_candidate")
  : >"$TEST_POLICY_CALLS"
  assert_virtiofsd_refused "$TEST_TMP/virtiofsd-refusal.out"
  [ "$(<"$TEST_POLICY_CALLS")" = "$symlink_candidate" ]
}

@test "unsafe virtiofsd preflight fails before allocating state or starting lifecycle stages" {
  setup_virtiofsd_policy_test
  install_virtiofsd_policy_test_shim
  local fake_bin="$TEST_TMP/untrusted-bin" state_marker="$TEST_TMP/state-preparation" stage_marker="$TEST_TMP/lifecycle-stage"
  mkdir -p "$fake_bin"
  write_virtiofsd_test_executable "$fake_bin/virtiofsd"
  VM_HOST_TRUSTED_PATH="$fake_bin"
  VM_HOST_VIRTIOFSD_PATHS=("$TEST_POLICY_TMP/missing/virtiofsd")
  VM_HOST_SCRIPT_ROOT="$TEST_ROOT"
  command() {
    if [[ "${1:-}" == -v ]]; then
      if [[ "${2:-}" == python3 ]]; then
        printf '/usr/bin/python3\n'
      else
        printf '/usr/bin/true\n'
      fi
      return 0
    fi
    builtin command "$@"
  }
  vm_host_require_kvm() { :; }
  vm_host_prepare_state_root() { touch "$state_marker"; printf '%s/state\n' "$TEST_TMP"; }
  vm_host_prepare_disk() { touch "$stage_marker"; }

  if vm_host_smoke --workspace "$TEST_TMP/workspace" --model openai/test-model \
    >"$TEST_TMP/virtiofsd-preflight.out" 2>&1; then
    status=0
  else
    status=$?
  fi

  [ "$status" -ne 0 ]
  [[ "$(<"$TEST_TMP/virtiofsd-preflight.out")" == *'virtiofsd executable is not from a protected root-owned path'* ]]
  [ ! -e "$state_marker" ]
  [ ! -e "$stage_marker" ]
}

@test "virtiofs startup executes the exact absolute path selected and validated during preflight" {
  setup_virtiofsd_policy_test
  install_virtiofsd_policy_test_shim
  local run_dir="$TEST_TMP/virtiofs-run" executable
  local invocation="$TEST_TMP/virtiofsd-invocation" expected_invocation
  executable="$TEST_POLICY_TMP/selected-virtiofsd"
  mkdir -m 0700 "$run_dir"
  cat >"$executable" <<'PY'
#!/usr/bin/python3
import os
import pathlib
import socket
import sys

pathlib.Path(os.environ["VM_HOST_TEST_VIRTIOFSD_INVOCATION"]).write_text(
    "\n".join([sys.argv[0], *sys.argv[1:]]) + "\n", encoding="utf-8"
)
socket_path = sys.argv[sys.argv.index("--socket-path") + 1]
listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(socket_path)
listener.close()
with open(socket_path + ".pid", "w", encoding="ascii") as stream:
    stream.write(str(os.getpid()) + "\n")
os.chmod(socket_path + ".pid", 0o600)
PY
  chmod 0755 "$executable"
  VM_HOST_TRUSTED_PATH="$TEST_POLICY_TMP/empty-path"
  mkdir -p "$VM_HOST_TRUSTED_PATH"
  VM_HOST_VIRTIOFSD_PATHS=("$executable")
  vm_host_resolve_virtiofsd >"$TEST_TMP/virtiofsd-resolve.out" 2>&1 || {
    cat "$TEST_TMP/virtiofsd-resolve.out" >&2
    return 1
  }
  [ "$VM_HOST_VIRTIOFSD_EXECUTABLE" = "$executable" ]
  [ "$(<"$TEST_POLICY_CALLS")" = "$executable" ]
  VM_HOST_TEST_VIRTIOFSD_INVOCATION="$invocation"
  export VM_HOST_TEST_VIRTIOFSD_INVOCATION
  mkdir -m 0700 "$TEST_TMP/virtiofs-runtime"
  python3() {
    if [[ "${2:-}" == *virtiofs-socket.py ]]; then
      case "${3:-}" in
        allocate) printf '%s\n' "$TEST_TMP/virtiofs-runtime/virtiofs.sock" ;;
        create-log) : >"$4/virtiofsd.log"; chmod 0600 "$4/virtiofsd.log" ;;
        startup|stopped) return 0 ;;
        *) return 1 ;;
      esac
    else
      command python3 "$@"
    fi
  }
  vm_host_record_process_identity() {
    local log_path="${1%/virtiofsd.identity}/virtiofsd.log"
    : >"$1"
    for _ in {1..100}; do
      [[ -e "$log_path" ]] && return 0
      sleep 0.01
    done
    return 1
  }
  vm_host_process_identity_status() { return 0; }

  run vm_host_start_virtiofs "$run_dir" "$TEST_TMP/workspace"

  [ "$status" -eq 0 ] || { printf 'virtiofs start output: %s\n' "$output" >&2; return 1; }
  expected_invocation="$(printf '%s\n' "$executable" --shared-dir "$TEST_TMP/workspace" \
    --socket-path "$TEST_TMP/virtiofs-runtime/virtiofs.sock" --rlimit-nofile=1048576)"
  [ "$(<"$invocation")" = "$expected_invocation" ]
  [ -S "$TEST_TMP/virtiofs-runtime/virtiofs.sock" ]
  [ -f "$TEST_TMP/virtiofs-runtime/virtiofs.sock.pid" ]
  [ "$(stat -c %a "$TEST_TMP/virtiofs-runtime/virtiofs.sock.pid")" = 600 ]
}

@test "cleanup retains stopped run socket/PID artifacts and never signals PID-file contents" {
  local run_dir="$TEST_TMP/retained-virtiofs" pid
  mkdir -m 0700 "$run_dir"
  mkdir -m 0700 "$TEST_TMP/virtiofs-runtime"
  python3 - "$TEST_TMP/virtiofs-runtime/virtiofs.sock" <<'PY'
import socket, sys
listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(sys.argv[1])
listener.close()
PY
  sleep 30 &
  pid=$!
  printf '%s\n' "$pid" >"$TEST_TMP/virtiofs-runtime/virtiofs.sock.pid"
  chmod 0600 "$TEST_TMP/virtiofs-runtime/virtiofs.sock.pid"
  printf '%s\n' "$TEST_TMP/virtiofs-runtime/virtiofs.sock" >"$run_dir/virtiofs.socket-path"
  chmod 0600 "$run_dir/virtiofs.socket-path"
  vm_host_validate_virtiofs_artifacts() { return 0; }
  VM_HOST_RUN_DIR="$run_dir"

  vm_host_cleanup

  [ -S "$TEST_TMP/virtiofs-runtime/virtiofs.sock" ]
  [ "$(<"$TEST_TMP/virtiofs-runtime/virtiofs.sock.pid")" = "$pid" ]
  kill -0 "$pid"
  kill "$pid"
  wait "$pid" 2>/dev/null || true
}

@test "cleanup safely retires transient state after failure before virtiofs allocation" {
  local run_dir="$TEST_TMP/early-startup-failure"
  mkdir -m 0700 "$run_dir"
  printf 'partial image download\n' >"$run_dir/ubuntu.img.part"
  printf 'temporary converted disk\n' >"$run_dir/disk.qcow2"
  VM_HOST_RUN_DIR="$run_dir"
  VM_HOST_UNTRACKED_CHILD=0
  VM_HOST_VIRTIOFS_ALLOCATED=0

  vm_host_cleanup

  [ ! -e "$run_dir/ubuntu.img.part" ]
  [ ! -e "$run_dir/disk.qcow2" ]
  [ ! -e "$run_dir/virtiofs.socket-path" ]
  [ ! -e "$TEST_TMP/early-startup-failure/virtiofsd.sock" ]
}

@test "virtiofs socket uses a short path when the retained state root is longer than SUN_LEN" {
  local long_state="$TEST_TMP/$(printf '%050d' 1)/$(printf '%050d' 2)/$(printf '%050d' 3)"
  local run_dir="$long_state/20261109T195256Z-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  local fake_daemon="$TEST_TMP/virtiofsd-short-path" invocation="$TEST_TMP/socket-path"
  local log_path="$run_dir/virtiofsd.log" identity_path="$run_dir/virtiofsd.identity"
  mkdir -p "$run_dir" "$TEST_TMP/workspace"
  chmod 0700 "$run_dir"
  cat >"$fake_daemon" <<'PY'
#!/usr/bin/python3
import os, pathlib, signal, socket, sys, time
path = sys.argv[sys.argv.index("--socket-path") + 1]
pathlib.Path(os.environ["VM_HOST_TEST_INVOCATION"]).write_text(path, encoding="utf-8")
if len(os.fsencode(path)) > 107:
    print("Error creating listener: socket error: path must be shorter than SUN_LEN", flush=True)
    raise SystemExit(1)
listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(path)
listener.listen(1)
pathlib.Path(path + ".pid").write_text(str(os.getpid()) + "\n", encoding="ascii")
listener.close()
PY
  chmod 0700 "$fake_daemon"
  mkdir -m 0700 "$TEST_TMP/short-socket-dir"
  export VM_HOST_TEST_INVOCATION="$invocation"
  VM_HOST_VIRTIOFSD_EXECUTABLE="$fake_daemon"
  python3() {
    if [[ "${2:-}" == *virtiofs-socket.py ]]; then
      case "${3:-}" in
        allocate) printf '%s\n' "$TEST_TMP/short-socket-dir/virtiofs.sock" ;;
        create-log) : >"$4/virtiofsd.log"; chmod 0600 "$4/virtiofsd.log" ;;
        startup|stopped) return 0 ;;
        *) return 1 ;;
      esac
    else
      command python3 "$@"
    fi
  }
  vm_host_record_process_identity() {
    # Isolate the path behavior from the separate asynchronous log-open race.
    local log="${1%/virtiofsd.identity}/virtiofsd.log"
    for _ in {1..100}; do
      [[ -e "$log" ]] && break
      sleep 0.01
    done
    [ -f "$log" ] || return 1
    printf '{"test_identity":true}\n' >"$1"
  }
  vm_host_process_identity_status() {
    for _ in {1..100}; do
      [[ -s "$invocation" ]] && break
      sleep 0.01
    done
    [[ -s "$invocation" && -S "$(<"$invocation")" ]]
  }
  VM_HOST_RUN_DIR="$run_dir"

  if vm_host_start_virtiofs "$run_dir" "$TEST_TMP/workspace"; then
    status=0
  else
    status=$?
  fi

  for _ in {1..200}; do
    [[ -e "$run_dir/virtiofsd.complete" ]] && break
    sleep 0.01
  done
  [ -e "$run_dir/virtiofsd.complete" ]
  [ "$status" -eq 0 ]
  local socket_path="$(<"$invocation")"
  [ "$(python3 -c 'import os,sys; print(len(os.fsencode(sys.argv[1])))' "$socket_path")" -le 107 ]
  [[ "$socket_path" != "$run_dir"/* ]]
  [[ "$socket_path" == *"/virtiofs.sock" ]]
  vm_host_validate_virtiofs_artifacts "$run_dir" stopped
}

@test "virtiofs log exists privately before a delayed bounded logger starts" {
  local run_dir="$TEST_TMP/delayed-logger-run" fake_root="$TEST_TMP/slow-logger-root"
  local fake_daemon="$TEST_TMP/virtiofsd-immediate-failure" real_logger="$TEST_ROOT/lib/vm/bounded-log.py"
  local identity_path="$run_dir/virtiofsd.identity" complete_path="$run_dir/virtiofsd.complete"
  mkdir -m 0700 "$run_dir" "$fake_root"
  mkdir -p "$fake_root/lib/vm"
  cat >"$fake_root/lib/vm/bounded-log.py" <<'PY'
import os, runpy, sys, time
time.sleep(0.5)
real = os.environ["VM_HOST_TEST_REAL_LOGGER"]
sys.argv = [real, *sys.argv[1:]]
runpy.run_path(real, run_name="__main__")
PY
  cat >"$fake_daemon" <<'SH'
#!/usr/bin/bash
printf 'intentional virtiofsd startup failure\n' >&2
exit 23
SH
  chmod 0700 "$fake_daemon"
  export VM_HOST_TEST_REAL_LOGGER="$real_logger"
  VM_HOST_SCRIPT_ROOT="$fake_root"
  VM_HOST_VIRTIOFSD_EXECUTABLE="$fake_daemon"
  python3() {
    if [[ "${2:-}" == *virtiofs-socket.py ]]; then
      case "${3:-}" in
        allocate) printf '%s\n' "$TEST_TMP/delayed-socket/virtiofs.sock" ;;
        create-log) : >"$4/virtiofsd.log"; chmod 0600 "$4/virtiofsd.log" ;;
        *) return 0 ;;
      esac
    else
      command python3 "$@"
    fi
  }
  vm_host_record_process_identity() {
    local log="${1%/virtiofsd.identity}/virtiofsd.log"
    if [[ -f "$log" ]] && [[ "$(stat -c %a "$log")" == 600 ]]; then
      printf 'yes\n' >"$TEST_TMP/log-precreated"
    else
      printf 'no\n' >"$TEST_TMP/log-precreated"
    fi
    printf '{"test_identity":true}\n' >"$1"
  }
  vm_host_process_identity_status() {
    for _ in {1..200}; do
      [[ -e "$complete_path" ]] && return 1
      sleep 0.01
    done
    return 2
  }
  VM_HOST_RUN_DIR="$run_dir"

  if vm_host_start_virtiofs "$run_dir" "$TEST_TMP/workspace"; then
    status=0
  else
    status=$?
  fi

  [ "$status" -ne 0 ]
  for _ in {1..200}; do
    [[ -e "$complete_path" ]] && break
    sleep 0.01
  done
  [ -e "$complete_path" ]
  [ "$(<"$TEST_TMP/log-precreated")" = yes ]
  [ -f "$run_dir/virtiofsd.log" ]
  [ "$(stat -c %a "$run_dir/virtiofsd.log")" = 600 ]
  grep -Fq 'intentional virtiofsd startup failure' "$run_dir/virtiofsd.log"
  [ ! -e "$run_dir/virtiofs.sock" ]
}

@test "stopped virtiofs validation preserves repeated spaces while checking listener paths" {
  local run_dir="$TEST_TMP/run with  repeated spaces"
  mkdir -m 0700 "$run_dir"
  python3 - "$run_dir/virtiofs.sock" <<'PY' &
import socket, sys, time
listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
listener.bind(sys.argv[1])
time.sleep(30)
PY
  local listener_pid=$!
  sleep 0.1

  run python3 -I - "$run_dir/virtiofs.sock" "$TEST_ROOT/lib/vm/virtiofs-socket.py" <<'PY'
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location("socket_helper", sys.argv[2])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(module._listener_present(sys.argv[1]))
PY

  [ "$status" -eq 0 ]
  [ "$output" = True ]
  kill "$listener_pid"
  wait "$listener_pid" 2>/dev/null || true
}

@test "network builder restricts QEMU and binds only loopback SSH control" {
  run vm_host_qemu_network_args 43210 32123

  [ "$status" -eq 0 ]
  [[ "$output" == *"hostfwd=tcp:127.0.0.1:43210-:22"* ]]
  [[ "$output" == *"guestfwd=tcp:10.0.2.100:3128"* ]]
  [[ "$output" == *"restrict=on"* ]]
  [[ "$output" != *"hostfwd=tcp:0.0.0.0"* ]]
  [[ "$output" != *"2375"* ]]
}

@test "lifecycle preflight requires host KVM before creating private state" {
  run vm_host_require_kvm

  if [[ ! -r /dev/kvm || ! -w /dev/kvm ]]; then
    [ "$status" -ne 0 ]
    [[ "$output" == *"/dev/kvm"* ]]
  else
    [ "$status" -eq 0 ]
  fi
}

@test "mutable checkout refuses before host execution under forged identity and workspace config" {
  local marker="$TEST_TMP/host-core-was-run" startup_file="$TEST_TMP/hostile-bash-env" fake_bin="$TEST_TMP/fake-bin"
  printf 'MESTER_VM_HOST_MODE=1\nMESTER_VM_CORE=touch %s\nMESTER_VM_ROLE=owner\n' "$marker" >"$TEST_TMP/.little-mester.conf"
  chmod 0666 "$TEST_TMP/.little-mester.conf"
  printf 'touch %q\n' "$marker" >"$startup_file"
  mkdir "$fake_bin"
  cat >"$fake_bin/python3" <<SH
#!/bin/sh
touch "$marker"
exit 0
SH
  chmod 0700 "$fake_bin/python3"

  run env PATH="$fake_bin:/usr/bin:/bin" BASH_ENV="$startup_file" \
    "BASH_FUNC_python3%%=() { touch $marker; }" \
    MESTER_VM_HOST_MODE=1 MESTER_VM_INSTALL_ROOT=/tmp/forged \
    MESTER_VM_CORE="touch $marker" MESTER_VM_ROLE=owner \
    "$TEST_ROOT/lab-vm" smoke --workspace "$TEST_TMP" --model openai/test-model

  [ "$status" -ne 0 ]
  [[ "$output" == *"must be root-owned"* ]]
  [ ! -e "$marker" ]
}

@test "intermediate installation symlink is refused before resolving its target" {
  local fixture="$TEST_TMP/protected-shape" output
  mkdir -p "$fixture"
  cp "$TEST_ROOT/lab-vm" "$fixture/lab-vm"
  ln -s "$TEST_ROOT/lib" "$fixture/lib"

  run "$fixture/lab-vm" smoke --workspace "$TEST_TMP" --model openai/test-model

  [ "$status" -ne 0 ]
  [[ "$output" == *"symlinked path component"* ]]
  [[ "$output" != *"must be root-owned"* ]]
}

@test "mutable virtiofs socket helper is rejected by launcher installation validation" {
  local fixture="$TEST_TMP/protected-helper-shape"
  mkdir -p "$fixture/lib/vm"
  cp "$TEST_ROOT/lab-vm" "$fixture/lab-vm"
  for name in host-launcher.sh cloud-init.sh proxy.py bounded-log.py executable-policy.py browser-auth-session.py guest-user-dirs.py; do
    cp "$TEST_ROOT/lib/vm/$name" "$fixture/lib/vm/$name"
  done
  ln -s "$TEST_ROOT/lib/vm/virtiofs-socket.py" "$fixture/lib/vm/virtiofs-socket.py"

  run "$fixture/lab-vm" smoke --workspace "$TEST_TMP" --model openai/test-model

  [ "$status" -ne 0 ]
  [[ "$output" == *"symlinked path component"* ]]
  [[ "$output" != *"must be root-owned"* ]]
}

@test "readiness fails closed when cloud-init SSH command fails" {
  vm_host_ssh() {
    case "$2" in
      cloud-init*) return 73 ;;
      *) printf '["name=rootless"]\n' ;;
    esac
  }

  run vm_host_provisioning_ready "$TEST_TMP/no-vm-used"

  [ "$status" -ne 0 ]
  [[ "$output" != *"ROOTLESS_DOCKER_OK"* ]]
  [[ "$output" == *"cloud-init, workspace mount, or rootless bootstrap readiness failed"* ]]
}

@test "essential smoke stages propagate injected failures and never print completion markers" {
  local stage
  for stage in vm_host_write_cloud_seed vm_host_prepare_disk vm_host_start_proxy vm_host_start_virtiofs vm_host_start_qemu vm_host_wait_ssh vm_host_provisioning_ready vm_host_browser_auth_smoke vm_host_model_smoke vm_host_docker_smoke cleanup; do
    run bash -c '
      source "$1/lib/vm/host-launcher.sh"
      source "$1/lib/vm/cloud-init.sh"
      VM_HOST_SCRIPT_ROOT="$1"
      vm_host_require_kvm() { :; }
      vm_host_require_commands() { :; }
      vm_host_account_home() { printf "%s\n" "$FAKE_HOME"; }
      vm_host_validate_workspace() { printf "%s/workspace\n" "$FAKE_HOME"; }
      vm_host_check_workspace_filesystem() { :; }
      vm_host_is_under() { return 1; }
      vm_host_prepare_state_root() { mkdir -p "$1/state"; printf "%s/state\n" "$1"; }
      vm_host_check_run_capacity() { :; }
      vm_host_write_cloud_seed() { injected_failure vm_host_write_cloud_seed; }
      injected_failure() { [[ "$FAIL_STAGE" != "$1" ]] || { printf "INJECTED_FAILURE:%s\n" "$1" >&2; return 73; }; }
      vm_host_prepare_disk() { injected_failure vm_host_prepare_disk; }
      vm_host_start_proxy() { injected_failure vm_host_start_proxy || return $?; VM_HOST_PROXY_PORT=12345; }
      vm_host_start_virtiofs() { injected_failure vm_host_start_virtiofs; }
      vm_host_free_port() { printf "43210\n"; }
      vm_host_start_qemu() { injected_failure vm_host_start_qemu; }
      vm_host_wait_ssh() { injected_failure vm_host_wait_ssh; }
      vm_host_provisioning_ready() { injected_failure vm_host_provisioning_ready; }
      vm_host_browser_auth_smoke() { injected_failure vm_host_browser_auth_smoke; }
      vm_host_model_smoke() { injected_failure vm_host_model_smoke || return $?; printf "VM_RESPONSE_OK\n"; }
      vm_host_docker_smoke() { injected_failure vm_host_docker_smoke || return $?; printf "DOCKER_CONTAINER_OK\n"; }
      vm_host_cleanup() { injected_failure cleanup; }
      id() { case "$1" in -u) printf "1234\n";; -g) printf "1234\n";; esac; }
      ssh-keygen() { local file="${@: -1}"; printf "private-key\n" >"$file"; printf "ssh-ed25519 test-key\n" >"$file.pub"; }
      FAKE_HOME="$2"
      FAIL_STAGE="$3"
      vm_host_smoke --workspace "$2/workspace" --model openai/test-model
    ' _ "$TEST_ROOT" "$TEST_TMP" "$stage"
    [ "$status" -ne 0 ]
    [[ "$output" == *"INJECTED_FAILURE:$stage"* ]] || { printf 'stage=%s output=%s\n' "$stage" "$output" >&2; return 1; }
    [[ "$output" != *"Smoke acceptance stages completed."* ]]
  done
  run bash -c '
    source "$1/lib/vm/host-launcher.sh"
    VM_HOST_RUN_DIR="$2/run"
    mkdir -p "$VM_HOST_RUN_DIR"
    VM_HOST_UNTRACKED_CHILD=1
    trap vm_host_exit_cleanup EXIT
    exit 0
  ' _ "$TEST_ROOT" "$TEST_TMP"
  [ "$status" -eq 1 ]
  [[ "$output" == *"cleanup incomplete"* ]]
}

@test "cloud seed stops before metadata and ISO creation when bootstrap fails" {
  vm_host_cloud_init() { return 73; }

  run vm_host_write_cloud_seed "$TEST_TMP" smoke-v3 1234 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE7zJmK1W1h3V4eA7qQKX8U4n9x0z8m1c2b3d4e5f6g7 test'

  [ "$status" -ne 0 ]
  [ ! -e "$TEST_TMP/meta-data" ]
  [ ! -e "$TEST_TMP/seed.iso" ]
}

@test "image download failure stops before conversion" {
  local converted="$TEST_TMP/converted"
  curl() { [ "$1" = -q ] || return 89; return 73; }
  qemu-img() { touch "$converted"; }

  run vm_host_prepare_disk "$TEST_TMP"

  [ "$status" -ne 0 ]
  [ ! -e "$converted" ]
}

@test "new smoke disk preparation cannot resize the legacy default VM disk" {
  local run_dir="$TEST_TMP/fresh-run" legacy="$TEST_TMP/home/.local/share/little-mester/vm/default/disk.qcow2"
  mkdir -m 0700 "$run_dir"
  mkdir -p "$(dirname "$legacy")"
  printf 'legacy disk sentinel\n' >"$legacy"
  local invocations="$TEST_TMP/qemu-img.calls"
  curl() {
    while (($#)); do
      if [[ "$1" == --output ]]; then printf 'new candidate image\n' >"$2"; return 0; fi
      shift
    done
    return 89
  }
  sha256sum() { return 0; }
  qemu-img() {
    printf '%s\n' "$*" >>"$invocations"
    if [[ "$1" == convert ]]; then printf 'fresh run disk\n' >"${@: -1}"; fi
  }

  run vm_host_prepare_disk "$run_dir"

  [ "$status" -eq 0 ]
  [ "$(<"$run_dir/disk.qcow2")" = 'fresh run disk' ]
  [ "$(<"$legacy")" = 'legacy disk sentinel' ]
  grep -F -- "$run_dir/disk.qcow2" "$invocations"
  [[ "$(<"$invocations")" != *"$legacy"* ]]
}

@test "proxy startup fails closed when launch cannot produce an owned process" {
  VM_HOST_SCRIPT_ROOT="$TEST_ROOT"
  python3() { return 73; }
  vm_host_record_process_identity() { return 78; }

  run vm_host_start_proxy "$TEST_TMP"

  [ "$status" -ne 0 ]
  [ ! -s "$TEST_TMP/proxy.port" ]
  [[ "$output" != *"PROXY_READY"* ]]
}

@test "virtiofs startup fails closed when launch cannot produce an owned process" {
  local fake_virtiofsd="$TEST_TMP/virtiofsd"
  printf '#!/bin/sh\nexit 73\n' >"$fake_virtiofsd"
  chmod 0755 "$fake_virtiofsd"
  VM_HOST_VIRTIOFSD_EXECUTABLE="$fake_virtiofsd"
  vm_host_record_process_identity() { return 78; }

  run vm_host_start_virtiofs "$TEST_TMP" "$TEST_TMP/workspace"

  [ "$status" -ne 0 ]
  [ ! -S "$TEST_TMP/virtiofs.sock" ]
}

@test "QEMU argv includes shareable memfd RAM required by vhost-user virtiofs" {
  local argv_file="$TEST_TMP/qemu.argv"
  VM_HOST_PROXY_PORT=32123
  python3() {
    if [[ "${2:-}" == *virtiofs-socket.py && "${3:-}" == path ]]; then
      printf '%s\n' "$TEST_TMP/virtiofs.sock"
    else
      printf '%s\n' "$@" >"$argv_file"
    fi
  }
  readlink() {
    printf '/usr/bin/python3\n'
  }
  vm_host_record_process_identity() { : >"$1"; }
  vm_host_process_identity_status() { return 0; }

  run vm_host_start_qemu "$TEST_TMP" smoke-v3 "$TEST_TMP/workspace" 43210

  [ "$status" -eq 0 ]
  grep -Fx -- '-object' "$argv_file"
  grep -Fx -- 'memory-backend-memfd,id=mem,size=4096M,share=on' "$argv_file"
  grep -Fx -- '-numa' "$argv_file"
  grep -Fx -- 'node,memdev=mem' "$argv_file"
  grep -Fx -- '-serial' "$argv_file"
  grep -Fx -- 'stdio' "$argv_file"
  grep -Fx -- "socket,id=charfs,path=$TEST_TMP/virtiofs.sock" "$argv_file"
  grep -F -- 'restrict=on' "$argv_file"
}

@test "QEMU launch identity failure is nonzero and does not claim readiness" {
  VM_HOST_PROXY_PORT=32123
  python3() {
    if [[ "${2:-}" == *virtiofs-socket.py && "${3:-}" == path ]]; then
      printf '%s\n' "$TEST_TMP/virtiofs.sock"
    else
      return 73
    fi
  }
  readlink() {
    printf '/usr/bin/python3\n'
  }
  vm_host_record_process_identity() { return 78; }

  run vm_host_start_qemu "$TEST_TMP" smoke-v3 "$TEST_TMP/workspace" 43210

  [ "$status" -ne 0 ]
  [[ "$output" == *"exact run-owned process identity"* ]]
  [[ "$output" != *"VM_UP"* ]]
}

@test "cleanup leaves an unrelated reused PID alone when argv identity differs" {
  local pid executable
  sleep 60 &
  pid="$!"
  executable="$(readlink -f "$(command -v sleep)")"
  vm_host_record_process_identity "$TEST_TMP/process.identity" "$pid" "$executable" 60
  python3 - "$TEST_TMP/process.identity" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
d = json.loads(p.read_text())
d["argv"].append("not-the-recorded-process")
p.write_text(json.dumps(d))
PY
  VM_HOST_RUN_DIR="$TEST_TMP"

  run vm_host_cleanup_one "$TEST_TMP/process.identity"

  [ "$status" -ne 0 ]
  kill -0 "$pid"
  kill "$pid"
  wait "$pid" 2>/dev/null || true
}

@test "pidfd cleanup stops only the exact recorded child process" {
  local pid executable
  sleep 60 &
  pid="$!"
  executable="$(readlink -f "$(command -v sleep)")"
  vm_host_record_process_identity "$TEST_TMP/process.identity" "$pid" "$executable" 60

  run vm_host_cleanup_one "$TEST_TMP/process.identity"

  [ "$status" -eq 0 ]
  [ ! -e "$TEST_TMP/process.identity" ]
  run vm_host_process_identity_status "$TEST_TMP/process.identity"
  [ "$status" -ne 0 ]
  wait "$pid" 2>/dev/null || true
}

@test "guest SSH capture disables ambient config and keeps outputs within the fixed cap" {
  local fake_bin="$TEST_TMP/fake-bin" run_dir="$TEST_TMP/ssh-run" output_file="$TEST_TMP/guest.out"
  mkdir -p "$fake_bin" "$run_dir"
  printf '43210\n' >"$run_dir/ssh_port"
  cat >"$fake_bin/ssh" <<'SH'
#!/bin/sh
printf '%s\n' "$@" >"$VM_HOST_TEST_SSH_ARGV"
printf 'bounded response\n'
SH
  chmod 0700 "$fake_bin/ssh"
  PATH="$fake_bin:$PATH"
  VM_HOST_TEST_SSH_ARGV="$TEST_TMP/ssh-argv"
  export PATH VM_HOST_TEST_SSH_ARGV

  run vm_host_ssh_capture "$run_dir" "$output_file" 'test guest command'

  [ "$status" -eq 0 ]
  [ "$(<"$output_file")" = 'bounded response' ]
  grep -Fx -- '-F' "$TEST_TMP/ssh-argv"
  grep -Fx -- '/dev/null' "$TEST_TMP/ssh-argv"
  grep -Fx -- 'ProxyCommand=none' "$TEST_TMP/ssh-argv"
  grep -Fx -- 'ProxyJump=none' "$TEST_TMP/ssh-argv"
  grep -Fx -- 'IdentityAgent=none' "$TEST_TMP/ssh-argv"
  grep -Fx -- 'GlobalKnownHostsFile=/dev/null' "$TEST_TMP/ssh-argv"
}

@test "oversized guest SSH output fails while receiving and kills its producer" {
  local fake_bin="$TEST_TMP/fake-bin" run_dir="$TEST_TMP/ssh-run"
  local output_file="$TEST_TMP/oversized-guest.out" pid_file="$TEST_TMP/ssh.pid" pid
  mkdir -p "$fake_bin" "$run_dir"
  printf '43210\n' >"$run_dir/ssh_port"
  cat >"$fake_bin/ssh" <<'PY'
#!/usr/bin/python3
import os, pathlib, sys, time
pathlib.Path(os.environ['VM_HOST_TEST_SSH_PID']).write_text(str(os.getpid()))
sys.stdout.write('x' * 200000)
sys.stdout.flush()
time.sleep(30)
PY
  chmod 0700 "$fake_bin/ssh"
  PATH="$fake_bin:$PATH"
  VM_HOST_TEST_SSH_PID="$pid_file"
  export PATH VM_HOST_TEST_SSH_PID

  run vm_host_ssh_capture "$run_dir" "$output_file" 'oversized guest command'

  [ "$status" -eq 125 ]
  [ "$(stat -c %s "$output_file")" -le "$VM_HOST_GUEST_OUTPUT_LIMIT_BYTES" ]
  [ -s "$pid_file" ]
  pid="$(<"$pid_file")"
  [ ! -e "/proc/$pid" ]
}

@test "SSH host-key scan caps combined diagnostics and retains only a valid key line" {
  local fake_bin="$TEST_TMP/fake-bin" run_dir="$TEST_TMP/ssh-run"
  mkdir -p "$fake_bin" "$run_dir"
  cat >"$fake_bin/ssh-keyscan" <<'SH'
#!/bin/sh
printf 'bounded diagnostic\n' >&2
printf '|1|salt==|hash== ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA==\n'
SH
  chmod 0700 "$fake_bin/ssh-keyscan"
  PATH="$fake_bin:$PATH"
  export PATH
  VM_HOST_BOOT_TIMEOUT=10
  vm_host_process_identity_status() { return 0; }

  run vm_host_wait_ssh "$run_dir" 43210

  [ "$status" -eq 0 ]
  [ "$(<"$run_dir/known_hosts")" = '|1|salt==|hash== ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA==' ]
  ! grep -Fq 'bounded diagnostic' "$run_dir/known_hosts"
  [ "$(stat -c %a "$run_dir/known_hosts")" = 600 ]
}

@test "CLI exposes no unbounded internal smoke worker" {
  run grep -F '/usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/nonexistent LANG=C LC_ALL=C' "$TEST_ROOT/lab-vm"
  [ "$status" -eq 0 ]
  run grep -F '/usr/bin/timeout --signal=TERM --kill-after=15 1200' "$TEST_ROOT/lab-vm"
  [ "$status" -eq 0 ]
  run grep -F '#!/usr/bin/bash -p' "$TEST_ROOT/lab-vm"
  [ "$status" -eq 0 ]
  run grep -E '^[[:space:]]*_smoke\)' "$TEST_ROOT/lab-vm"
  [ "$status" -ne 0 ]
  run grep -F '"$bounded_logger" "$output" "$VM_HOST_GUEST_OUTPUT_LIMIT_BYTES"' "$TEST_ROOT/lib/vm/host-launcher.sh"
  [ "$status" -eq 0 ]
}

@test "cleanup deletes transient keys/seeds, bounds retained logs, and keeps diagnostics private" {
  local run_dir="$TEST_TMP/private-run"
  mkdir -m 0700 "$run_dir"
  printf 'private key material\n' >"$run_dir/id_vm_control"
  printf 'credential-free seed metadata\n' >"$run_dir/user-data"
  printf 'temporary cloud image\n' >"$run_dir/disk.qcow2"
  python3 - "$run_dir/qemu.log" "$VM_HOST_LOG_LIMIT_BYTES" <<'PY'
import pathlib, sys
pathlib.Path(sys.argv[1]).write_bytes(b"x" * (int(sys.argv[2]) + 73))
PY
  VM_HOST_RUN_DIR="$run_dir"
  VM_HOST_UNTRACKED_CHILD=0
  vm_host_validate_virtiofs_artifacts() { return 0; }

  if vm_host_cleanup; then status=0; else status=$?; fi

  [ "$status" -eq 0 ]
  [ ! -e "$run_dir/id_vm_control" ]
  [ ! -e "$run_dir/user-data" ]
  [ ! -e "$run_dir/disk.qcow2" ]
  [ "$(stat -c %s "$run_dir/qemu.log")" -le "$VM_HOST_LOG_LIMIT_BYTES" ]
  [ "$(stat -c %a "$run_dir/qemu.log")" = 600 ]
}

@test "cleanup reports an untracked child instead of returning success" {
  local run_dir="$TEST_TMP/untracked-run"
  mkdir -m 0700 "$run_dir"

  run bash -c 'source "$1/lib/vm/host-launcher.sh"; vm_host_validate_virtiofs_artifacts() { return 0; }; VM_HOST_RUN_DIR="$2"; VM_HOST_UNTRACKED_CHILD=1; vm_host_cleanup' _ "$TEST_ROOT" "$run_dir"

  [ "$status" -ne 0 ]
  [[ "$output" == *"cleanup incomplete"* ]]
  [ -d "$run_dir" ]
}

@test "EXIT cleanup cascades after an occupied callback port and retains run artifacts" {
  local run_dir="$TEST_TMP/occupied-auth-cleanup" driver="$TEST_TMP/cleanup-cascade.sh"
  local listener_port="$TEST_TMP/occupied-port" listener_pid_file="$TEST_TMP/occupied-listener.pid"
  local attempt_log="$TEST_TMP/occupied-attempts" pids_file="$TEST_TMP/occupied-resource-pids"
  local listener_pid run_status run_output
  mkdir -m 0700 "$run_dir"
  TEST_CLEANUP_RUN_DIRS+=("$run_dir")
  python3 - "$listener_port" <<'PY' &
import os, socket, sys, time
listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.bind(("127.0.0.1", 0))
listener.listen(1)
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as stream:
    stream.write(str(listener.getsockname()[1]) + "\n")
    stream.flush()
while True:
    time.sleep(1)
PY
  listener_pid="$!"
  printf '%s\n' "$listener_pid" >"$listener_pid_file"
  TEST_UNRELATED_PID_FILE="$listener_pid_file"
  for _ in {1..100}; do
    [[ -s "$listener_port" ]] && break
    sleep 0.01
  done
  [ -s "$listener_port" ]
  write_cleanup_cascade_driver "$driver"
  : >"$attempt_log"

  run timeout --signal=TERM --kill-after=3 15 bash "$driver" \
    "$TEST_ROOT" "$run_dir" occupied "$listener_port" "$attempt_log" "$pids_file"
  run_status="$status"
  run_output="$output"
  assert_cleanup_cascade_result "$run_dir" "$attempt_log" "$pids_file" "$run_status" "$run_output"
  [ -e "/proc/$listener_pid" ]
  ! vm_host_auth_listener_closed "$run_dir/auth-callback.port"
  [ -e "$run_dir/auth-callback.port" ]
}

@test "EXIT cleanup cascades after missing or invalid browser-auth proof and retains artifacts" {
  local scenario run_dir driver="$TEST_TMP/cleanup-cascade.sh" attempt_log pids_file
  local run_status run_output kind
  write_cleanup_cascade_driver "$driver"
  for scenario in missing-proof invalid-proof; do
    run_dir="$TEST_TMP/$scenario-cleanup"
    attempt_log="$TEST_TMP/$scenario-attempts"
    pids_file="$TEST_TMP/$scenario-resource-pids"
    mkdir -m 0700 "$run_dir"
    TEST_CLEANUP_RUN_DIRS+=("$run_dir")
    : >"$attempt_log"

    run timeout --signal=TERM --kill-after=3 15 bash "$driver" \
      "$TEST_ROOT" "$run_dir" "$scenario" - "$attempt_log" "$pids_file"
    run_status="$status"
    run_output="$output"
    assert_cleanup_cascade_result "$run_dir" "$attempt_log" "$pids_file" "$run_status" "$run_output"
    [ -e "$run_dir/auth-session.complete" ]
    if [[ "$scenario" == missing-proof ]]; then
      [ ! -e "$run_dir/auth-session.proof" ]
    else
      [ "$(<"$run_dir/auth-session.proof")" = 'not-json' ]
    fi
  done
}

@test "EXIT trap changes a successful smoke status to failure when cleanup is ambiguous" {
  local run_dir="$TEST_TMP/ambiguous-exit-run"
  mkdir -m 0700 "$run_dir"

  run bash -c 'source "$1/lib/vm/host-launcher.sh"; vm_host_validate_virtiofs_artifacts() { return 0; }; VM_HOST_RUN_DIR="$2"; VM_HOST_UNTRACKED_CHILD=1; trap vm_host_exit_cleanup EXIT; exit 0' _ "$TEST_ROOT" "$run_dir"

  [ "$status" -eq 1 ]
  [[ "$output" == *"cleanup incomplete"* ]]
  [ -d "$run_dir" ]
}

@test "EXIT trap preserves the original stage failure when cleanup is ambiguous" {
  local run_dir="$TEST_TMP/failed-stage-run"
  mkdir -m 0700 "$run_dir"

  run bash -c 'source "$1/lib/vm/host-launcher.sh"; vm_host_validate_virtiofs_artifacts() { return 0; }; VM_HOST_RUN_DIR="$2"; VM_HOST_UNTRACKED_CHILD=1; trap vm_host_exit_cleanup EXIT; exit 73' _ "$TEST_ROOT" "$run_dir"

  [ "$status" -eq 73 ]
  [[ "$output" == *"cleanup incomplete"* ]]
  [ -d "$run_dir" ]
}

@test "EXIT trap rejects ambiguous process ownership without killing that process" {
  local run_dir="$TEST_TMP/ambiguous-process-run" pid executable
  mkdir -m 0700 "$run_dir"
  sleep 60 &
  pid="$!"
  executable="$(readlink -f "$(command -v sleep)")"
  vm_host_record_process_identity "$run_dir/qemu.identity" "$pid" "$executable" 60
  python3 - "$run_dir/qemu.identity" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
d = json.loads(p.read_text())
d["argv"].append("forged-identity")
p.write_text(json.dumps(d))
PY

  run bash -c 'source "$1/lib/vm/host-launcher.sh"; VM_HOST_RUN_DIR="$2"; trap vm_host_exit_cleanup EXIT; exit 0' _ "$TEST_ROOT" "$run_dir"

  [ "$status" -eq 1 ]
  [[ "$output" == *"cleanup incomplete"* ]]
  kill -0 "$pid"
  kill "$pid"
  wait "$pid" 2>/dev/null || true
}

@test "EXIT cleanup waits for bounded logger group proof and preserves unrelated processes" {
  local run_dir="$TEST_TMP/logger-supervisor-run" child_script="$TEST_TMP/fork-ignore-term.py"
  local exit_script="$TEST_TMP/exit-with-logger.sh" descendant_file="$TEST_TMP/logger-descendant.pid"
  local parent_file="$TEST_TMP/logger-parent.pid" unrelated_file="$TEST_TMP/unrelated.pid"
  TEST_OWNED_PROCESS_GROUP_FILE="$parent_file"
  TEST_UNRELATED_PID_FILE="$unrelated_file"
  mkdir -m 0700 "$run_dir"
  cat >"$child_script" <<'PY'
import os, pathlib, signal, time
child = os.fork()
if child:
    pathlib.Path(os.environ["TEST_PARENT_PID_FILE"]).write_text(str(os.getpid()))
    print("SUPERVISED_READY", flush=True)
    while True:
        time.sleep(1)
else:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.close(1)
    os.close(2)
    pathlib.Path(os.environ["TEST_DESCENDANT_PID_FILE"]).write_text(str(os.getpid()))
    while True:
        time.sleep(1)
PY
  cat >"$exit_script" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
root="$1" run_dir="$2" child_script="$3" parent_file="$4" descendant_file="$5" unrelated_file="$6"
source "$root/lib/vm/host-launcher.sh"
vm_host_validate_virtiofs_artifacts() { return 0; }
VM_HOST_SCRIPT_ROOT="$root"
VM_HOST_RUN_DIR="$run_dir"
VM_HOST_UNTRACKED_CHILD=0
VM_HOST_SMOKE_COMPLETE_PENDING=1
export VM_HOST_SCRIPT_ROOT VM_HOST_RUN_DIR VM_HOST_UNTRACKED_CHILD VM_HOST_SMOKE_COMPLETE_PENDING
trap vm_host_exit_cleanup EXIT
sleep 60 </dev/null >/dev/null 2>&1 &
printf '%s\n' "$!" >"$unrelated_file"
export TEST_PARENT_PID_FILE="$parent_file" TEST_DESCENDANT_PID_FILE="$descendant_file"
python3 -I "$root/lib/vm/bounded-log.py" "$run_dir/proxy.log" 1048576 \
  --completion-file "$run_dir/proxy.complete" -- python3 -I "$child_script" </dev/null &
logger_pid="$!"
VM_HOST_UNTRACKED_CHILD=1
vm_host_record_process_identity "$run_dir/proxy.identity" "$logger_pid" \
  "$(readlink -f -- "$(command -v python3)")" "$root/lib/vm/bounded-log.py"
VM_HOST_UNTRACKED_CHILD=0
for ((attempt=0; attempt<150; attempt++)); do [[ -s "$descendant_file" ]] && break; sleep 0.02; done
[[ -s "$parent_file" && -s "$descendant_file" ]]
exit 0
SH
  chmod 0700 "$exit_script"

  run timeout --signal=TERM --kill-after=15 25 bash "$exit_script" \
    "$TEST_ROOT" "$run_dir" "$child_script" "$parent_file" "$descendant_file" "$unrelated_file"

  [ "$status" -eq 0 ]
  [[ "$output" == *"Smoke acceptance stages completed."* ]]
  [ ! -e "/proc/$(<"$descendant_file")" ]
  [ ! -e "$run_dir/proxy.identity" ]
  [ ! -e "$run_dir/proxy.complete" ]
  kill -0 "$(<"$unrelated_file")"
}

@test "EXIT cleanup fails closed when supervisor escalation kills an unverified logger" {
  local run_dir="$TEST_TMP/logger-unproved-run" start_script="$TEST_TMP/escalate-logger.sh"
  local child_script="$TEST_TMP/unproved-fork.py" parent_file="$TEST_TMP/unproved-parent.pid"
  local child_pid_file="$TEST_TMP/unproved-descendant.pid" group_file="$TEST_TMP/unproved-group.pid"
  TEST_OWNED_PROCESS_GROUP_FILE="$group_file"
  mkdir -m 0700 "$run_dir"
  cat >"$child_script" <<'PY'
import os, pathlib, signal, time
child = os.fork()
if child:
    pathlib.Path(os.environ["TEST_PARENT_PID_FILE"]).write_text(str(os.getpid()))
    print("READY", flush=True)
    while True:
        time.sleep(1)
else:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.close(1)
    os.close(2)
    pathlib.Path(os.environ["TEST_CHILD_PID_FILE"]).write_text(str(os.getpid()))
    while True:
        time.sleep(1)
PY
  cat >"$start_script" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
root="$1" run_dir="$2" child_script="$3" parent_file="$4" child_pid_file="$5" group_file="$6"
source "$root/lib/vm/host-launcher.sh"
VM_HOST_SCRIPT_ROOT="$root"
VM_HOST_RUN_DIR="$run_dir"
VM_HOST_UNTRACKED_CHILD=0
VM_HOST_SMOKE_COMPLETE_PENDING=1
VM_HOST_PROCESS_STOP_GRACE_MS=100
VM_HOST_PROCESS_KILL_SETTLE_MS=200
export VM_HOST_SCRIPT_ROOT VM_HOST_RUN_DIR VM_HOST_UNTRACKED_CHILD VM_HOST_SMOKE_COMPLETE_PENDING
trap vm_host_exit_cleanup EXIT
export TEST_PARENT_PID_FILE="$parent_file" TEST_CHILD_PID_FILE="$child_pid_file"
python3 -I "$root/lib/vm/bounded-log.py" "$run_dir/proxy.log" 1048576 \
  --completion-file "$run_dir/proxy.complete" -- python3 -I "$child_script" </dev/null &
logger_pid="$!"
VM_HOST_UNTRACKED_CHILD=1
vm_host_record_process_identity "$run_dir/proxy.identity" "$logger_pid" \
  "$(readlink -f -- "$(command -v python3)")" "$root/lib/vm/bounded-log.py"
VM_HOST_UNTRACKED_CHILD=0
for ((attempt=0; attempt<150; attempt++)); do [[ -s "$child_pid_file" ]] && break; sleep 0.02; done
[[ -s "$parent_file" && -s "$child_pid_file" ]]
printf '%s\n' "$(<"$parent_file")" >"$group_file"
exit 0
SH
  chmod 0700 "$start_script"

  run timeout --signal=TERM --kill-after=15 10 bash "$start_script" \
    "$TEST_ROOT" "$run_dir" "$child_script" "$parent_file" "$child_pid_file" "$group_file"

  [ "$status" -eq 1 ]
  [[ "$output" == *"completion is unverified"* ]]
  [[ "$output" != *"Smoke acceptance stages completed."* ]]
  [ -d "$run_dir" ]
  [ -e "/proc/$(<"$child_pid_file")" ]
  [ ! -e "$run_dir/proxy.complete" ]
}

@test "logger and supervisor teardown grace periods fit inside the outer timeout grace" {
  [ "$VM_HOST_PROCESS_STOP_GRACE_MS" -eq 11000 ]
  [ "$VM_HOST_PROCESS_KILL_SETTLE_MS" -eq 2000 ]
  [ $((VM_HOST_PROCESS_STOP_GRACE_MS + VM_HOST_PROCESS_KILL_SETTLE_MS)) -lt 15000 ]
  [ "$VM_HOST_PROCESS_STOP_GRACE_MS" -ge 10000 ]
}

@test "filesystem preflight rejects an ordinary workspace directory" {
  mkdir "$TEST_TMP/unbounded-workspace"

  run vm_host_check_workspace_filesystem "$TEST_TMP/unbounded-workspace"

  [ "$status" -ne 0 ]
  [[ "$output" == *"dedicated owner-provisioned bounded filesystem"* ]]
}

@test "cleanup is a no-op before state allocation when no resources exist" {
  run bash -c 'source "$1/lib/vm/host-launcher.sh"; unset VM_HOST_RUN_DIR; VM_HOST_UNTRACKED_CHILD=0; VM_HOST_SMOKE_COMPLETE_PENDING=0; vm_host_cleanup' _ "$TEST_ROOT"

  [ "$status" -eq 0 ]
}

@test "EXIT trap rejects missing allocated run state before success" {
  local missing="$TEST_TMP/missing-run-state"

  run bash -c 'source "$1/lib/vm/host-launcher.sh"; VM_HOST_RUN_DIR="$2"; VM_HOST_UNTRACKED_CHILD=1; VM_HOST_SMOKE_COMPLETE_PENDING=1; trap vm_host_exit_cleanup EXIT; exit 0' _ "$TEST_ROOT" "$missing"

  [ "$status" -eq 1 ]
  [[ "$output" == *"cleanup incomplete"* ]]
  [[ "$output" != *"Smoke acceptance stages completed."* ]]
}

@test "EXIT trap rejects nondirectory allocated run state before success" {
  local run_state="$TEST_TMP/not-a-run-directory"
  : >"$run_state"

  run bash -c 'source "$1/lib/vm/host-launcher.sh"; VM_HOST_RUN_DIR="$2"; VM_HOST_UNTRACKED_CHILD=0; VM_HOST_SMOKE_COMPLETE_PENDING=1; trap vm_host_exit_cleanup EXIT; exit 0' _ "$TEST_ROOT" "$run_state"

  [ "$status" -eq 1 ]
  [[ "$output" == *"cleanup incomplete"* ]]
  [[ "$output" != *"Smoke acceptance stages completed."* ]]
}

@test "EXIT trap rejects symlinked allocated run state without touching its target" {
  local target="$TEST_TMP/run-target" link="$TEST_TMP/run-link"
  mkdir -m 0700 "$target"
  printf 'preserve this file\n' >"$target/disk.qcow2"
  ln -s "$target" "$link"

  run bash -c 'source "$1/lib/vm/host-launcher.sh"; VM_HOST_RUN_DIR="$2"; VM_HOST_UNTRACKED_CHILD=0; VM_HOST_SMOKE_COMPLETE_PENDING=1; trap vm_host_exit_cleanup EXIT; exit 0' _ "$TEST_ROOT" "$link"

  [ "$status" -eq 1 ]
  [[ "$output" == *"cleanup incomplete"* ]]
  [[ "$output" != *"Smoke acceptance stages completed."* ]]
  [ "$(<"$target/disk.qcow2")" = "preserve this file" ]
}

@test "EXIT trap preserves the original stage failure when allocated run state is missing" {
  local missing="$TEST_TMP/missing-failed-run-state"

  run bash -c 'source "$1/lib/vm/host-launcher.sh"; VM_HOST_RUN_DIR="$2"; VM_HOST_UNTRACKED_CHILD=1; trap vm_host_exit_cleanup EXIT; exit 73' _ "$TEST_ROOT" "$missing"

  [ "$status" -eq 73 ]
  [[ "$output" == *"cleanup incomplete"* ]]
  [[ "$output" != *"Smoke acceptance stages completed."* ]]
}

@test "vm_host_ssh has a real finite default deadline and reaps a timed-out child" {
  local fake_bin="$TEST_TMP/fake-bin" run_dir="$TEST_TMP/ssh-run" pid_file="$TEST_TMP/ssh.pid"
  local started elapsed pid
  mkdir -p "$fake_bin" "$run_dir"
  printf '43210\n' >"$run_dir/ssh_port"
  cat >"$fake_bin/ssh" <<'SH'
#!/bin/sh
printf '%s\n' "$$" >"$VM_HOST_TEST_SSH_PID"
exec /usr/bin/sleep 30
SH
  chmod 0700 "$fake_bin/ssh"
  started=$SECONDS

  run timeout --kill-after=1 5 bash -c 'source "$1/lib/vm/host-launcher.sh"; VM_HOST_SCRIPT_ROOT="$1"; VM_HOST_CONTROL_TIMEOUT=1; VM_HOST_TEST_SSH_PID="$3"; PATH="$4:/usr/bin:/bin"; export VM_HOST_SCRIPT_ROOT VM_HOST_TEST_SSH_PID PATH; vm_host_ssh "$2" "bounded test command"' _ "$TEST_ROOT" "$run_dir" "$pid_file" "$fake_bin"

  elapsed=$((SECONDS - started))
  [ "$status" -eq 124 ]
  [ "$elapsed" -lt 4 ]
  [ -s "$pid_file" ]
  pid="$(<"$pid_file")"
  [ ! -e "/proc/$pid" ]

  run bash -c 'source "$1/lib/vm/host-launcher.sh"; VM_HOST_SCRIPT_ROOT="$1"; export VM_HOST_SCRIPT_ROOT; timeout() { :; }; ssh() { printf "SSH_WAS_CALLED\n"; }; vm_host_ssh "$2" "invalid deadline" 0' _ "$TEST_ROOT" "$run_dir"

  [ "$status" -ne 0 ]
  [[ "$output" == *"deadline must be a positive integer"* ]]
  [[ "$output" != *"SSH_WAS_CALLED"* ]]
}
