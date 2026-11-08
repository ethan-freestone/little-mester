#!/usr/bin/env bats

setup() {
  TEST_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
  TEST_TMP="$(mktemp -d)"
  TEST_OWNED_PROCESS_GROUP_FILE=""
  TEST_UNRELATED_PID_FILE=""
  VM_HOST_SCRIPT_ROOT="$TEST_ROOT"
  source "$TEST_ROOT/lib/vm/host-launcher.sh"
  source "$TEST_ROOT/lib/vm/cloud-init.sh"
}

teardown() {
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
    fi
  fi
  rm -rf -- "$TEST_TMP"
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

@test "private state directory is created without following a symlink" {
  mkdir "$TEST_TMP/home" "$TEST_TMP/target"
  chmod 0755 "$TEST_TMP/home"
  ln -s "$TEST_TMP/target" "$TEST_TMP/home/.local"

  run vm_host_prepare_state_root "$TEST_TMP/home"

  [ "$status" -ne 0 ]
  [[ "$output" == *"REFUSED"* || "$output" == *"Too many levels"* ]]
  [ ! -e "$TEST_TMP/target/state" ]
}

@test "credential file requires private regular owner-owned OpenAI JSON" {
  cat >"$TEST_TMP/provider.json" <<'JSON'
{"provider":"openai","model":"openai/gpt-6.1-sol","api_key":"unit-test-not-a-secret"}
JSON
  chmod 0644 "$TEST_TMP/provider.json"

  run vm_host_validate_credentials "$TEST_TMP/provider.json"

  [ "$status" -ne 0 ]
  [[ "$output" == *"mode 0600"* ]]
  [[ "$output" != *"unit-test-not-a-secret"* ]]
}

@test "credential parser rejects unknown fields and never prints key material" {
  cat >"$TEST_TMP/provider.json" <<'JSON'
{"provider":"openai","model":"openai/gpt-6.1-sol","api_key":"unit-test-not-a-secret","workspace":"/etc"}
JSON
  chmod 0600 "$TEST_TMP/provider.json"

  run vm_host_validate_credentials "$TEST_TMP/provider.json"

  [ "$status" -ne 0 ]
  [[ "$output" != *"unit-test-not-a-secret"* ]]
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
  vm_host_validate_credentials() { return 0; }
  vm_host_check_workspace_filesystem() { return 73; }
  vm_host_is_under() { return 1; }
  vm_host_credential_model() { printf 'openai/test-model\n'; }
  vm_host_prepare_state_root() { touch "$prepared"; printf '%s/state\n' "$TEST_TMP"; }

  run vm_host_smoke --workspace "$TEST_TMP/workspace" --credentials-file "$TEST_TMP/credentials.json"

  [ "$status" -ne 0 ]
  [ ! -e "$prepared" ]
  [[ "$output" != *"VM_UP"* ]]
}

@test "guest cloud-init uses rootless Docker and direct OpenCode without lifecycle authority" {
  run vm_host_cloud_init smoke-v3 1234 1234 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE7zJmK1W1h3V4eA7qQKX8U4n9x0z8m1c2b3d4e5f6g7 test'

  [ "$status" -eq 0 ] || {
    printf 'cloud-init generation failed (status %s); captured output follows:\n%s\n' "$status" "$output" >&2
    return 1
  }
  [[ "$output" == *"docker-ce-rootless-extras"* ]]
  [[ "$output" == *"dockerd-rootless-setuptool.sh install"* ]]
  [[ "$output" == *"systemctl disable --now docker.service docker.socket containerd.service"* ]]
  [[ "$output" == *"systemctl --user enable --now docker"* ]]
  [[ "$output" == *"opencode"* ]]
  [[ "$output" == *"@opencode/cli@2.2.5"* ]]
  [[ "$output" != *"/dev/kvm"* ]]
  [[ "$output" != *"VM_CREATE"* ]]
  [[ "$output" != *"recursion"* ]]
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
      vm_host_smoke --workspace "$test_tmp/workspace" --credentials-file "$test_tmp/credentials.json"
    ' _ "$TEST_ROOT" "$TEST_TMP" "$unsafe_command" "$fake_command"

    [ "$status" -ne 0 ]
    [[ "$output" == *"host command is not from a protected root-owned path: $unsafe_command"* ]]
    [[ "$output" != *"KEY_PROMOTION_REACHED"* ]]
    [ ! -e "$marker" ]
  done
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
    "$TEST_ROOT/lab-vm" smoke --workspace "$TEST_TMP" --credentials-file "$TEST_TMP/credentials.json"

  [ "$status" -ne 0 ]
  [[ "$output" == *"must be root-owned"* ]]
  [ ! -e "$marker" ]
}

@test "intermediate installation symlink is refused before resolving its target" {
  local fixture="$TEST_TMP/protected-shape" output
  mkdir -p "$fixture"
  cp "$TEST_ROOT/lab-vm" "$fixture/lab-vm"
  ln -s "$TEST_ROOT/lib" "$fixture/lib"

  run "$fixture/lab-vm" smoke --workspace "$TEST_TMP" --credentials-file "$TEST_TMP/credentials.json"

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
  for stage in vm_host_write_cloud_seed vm_host_prepare_disk vm_host_start_proxy vm_host_start_virtiofs vm_host_start_qemu vm_host_wait_ssh vm_host_provisioning_ready vm_host_import_credentials vm_host_model_smoke vm_host_docker_smoke cleanup; do
    run bash -c '
      source "$1/lib/vm/host-launcher.sh"
      source "$1/lib/vm/cloud-init.sh"
      VM_HOST_SCRIPT_ROOT="$1"
      vm_host_require_kvm() { :; }
      vm_host_require_commands() { :; }
      vm_host_account_home() { printf "%s\n" "$FAKE_HOME"; }
      vm_host_validate_workspace() { printf "%s/workspace\n" "$FAKE_HOME"; }
      vm_host_validate_credentials() { :; }
      vm_host_check_workspace_filesystem() { :; }
      vm_host_is_under() { return 1; }
      vm_host_credential_model() { printf "openai/test-model\n"; }
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
      vm_host_import_credentials() { injected_failure vm_host_import_credentials; }
      vm_host_model_smoke() { injected_failure vm_host_model_smoke || return $?; printf "VM_RESPONSE_OK\n"; }
      vm_host_docker_smoke() { injected_failure vm_host_docker_smoke || return $?; printf "DOCKER_CONTAINER_OK\n"; }
      vm_host_cleanup() { injected_failure cleanup; }
      id() { case "$1" in -u) printf "1234\n";; -g) printf "1234\n";; esac; }
      ssh-keygen() { local file="${@: -1}"; printf "private-key\n" >"$file"; printf "ssh-ed25519 test-key\n" >"$file.pub"; }
      FAKE_HOME="$2"
      FAIL_STAGE="$3"
      vm_host_smoke --workspace "$2/workspace" --credentials-file "$2/credentials"
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

  run vm_host_write_cloud_seed "$TEST_TMP" smoke-v3 1234 1234 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE7zJmK1W1h3V4eA7qQKX8U4n9x0z8m1c2b3d4e5f6g7 test'

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
  virtiofsd() { return 73; }
  vm_host_record_process_identity() { return 78; }

  run vm_host_start_virtiofs "$TEST_TMP" "$TEST_TMP/workspace"

  [ "$status" -ne 0 ]
  [ ! -S "$TEST_TMP/virtiofs.sock" ]
}

@test "QEMU argv includes shareable memfd RAM required by vhost-user virtiofs" {
  local argv_file="$TEST_TMP/qemu.argv"
  VM_HOST_PROXY_PORT=32123
  python3() { printf '%s\n' "$@" >"$argv_file"; }
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
  grep -F -- 'restrict=on' "$argv_file"
}

@test "QEMU launch identity failure is nonzero and does not claim readiness" {
  VM_HOST_PROXY_PORT=32123
  python3() { return 73; }
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

@test "credential delivery uses private control stdin and keeps fake key out of argv and logs" {
  local key='sk-inert-test-only-000000000000000000000000000000'
  local fake_bin="$TEST_TMP/fake-bin"
  mkdir -p "$TEST_TMP/run" "$fake_bin"
  printf '43210\n' >"$TEST_TMP/run/ssh_port"
  cat >"$TEST_TMP/provider.json" <<JSON
{"provider":"openai","model":"openai/gpt-6.1-sol","api_key":"$key"}
JSON
  chmod 0600 "$TEST_TMP/provider.json"
  cat >"$fake_bin/ssh" <<'SH'
#!/bin/sh
printf '%s\n' "$@" >"$VM_HOST_TEST_SSH_ARGV"
cat >"$VM_HOST_TEST_SSH_STDIN"
printf 'fake interactive prompt output\n'
SH
  chmod 0700 "$fake_bin/ssh"
  PATH="$fake_bin:$PATH"
  export PATH
  VM_HOST_TEST_SSH_ARGV="$TEST_TMP/ssh-argv"
  VM_HOST_TEST_SSH_STDIN="$TEST_TMP/ssh-stdin"
  export VM_HOST_TEST_SSH_ARGV VM_HOST_TEST_SSH_STDIN
  vm_host_ssh() { :; } # The post-import database permission check is isolated here.

  if vm_host_import_credentials "$TEST_TMP/run" "$TEST_TMP/provider.json" >"$TEST_TMP/import-output" 2>&1; then status=0; else status=$?; fi

  [ "$status" -eq 0 ]
  [ "$(<"$TEST_TMP/ssh-stdin")" = "$key" ]
  grep -Fx -- '-tt' "$TEST_TMP/ssh-argv"
  grep -Fx -- '-F' "$TEST_TMP/ssh-argv"
  grep -Fx -- '/dev/null' "$TEST_TMP/ssh-argv"
  [[ "$(<"$TEST_TMP/ssh-argv")" != *"$key"* ]]
  ! grep -Fq -- "$key" "$TEST_TMP/import-output"
  ! grep -Fq -- 'fake interactive prompt output' "$TEST_TMP/import-output"
  [ "${VM_HOST_CREDENTIALS_IMPORTED:-0}" = 1 ]
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

  run bash -c 'source "$1/lib/vm/host-launcher.sh"; VM_HOST_RUN_DIR="$2"; VM_HOST_UNTRACKED_CHILD=1; vm_host_cleanup' _ "$TEST_ROOT" "$run_dir"

  [ "$status" -ne 0 ]
  [[ "$output" == *"cleanup incomplete"* ]]
  [ -d "$run_dir" ]
}

@test "EXIT trap changes a successful smoke status to failure when cleanup is ambiguous" {
  local run_dir="$TEST_TMP/ambiguous-exit-run"
  mkdir -m 0700 "$run_dir"

  run bash -c 'source "$1/lib/vm/host-launcher.sh"; VM_HOST_RUN_DIR="$2"; VM_HOST_UNTRACKED_CHILD=1; trap vm_host_exit_cleanup EXIT; exit 0' _ "$TEST_ROOT" "$run_dir"

  [ "$status" -eq 1 ]
  [[ "$output" == *"cleanup incomplete"* ]]
  [ -d "$run_dir" ]
}

@test "EXIT trap preserves the original stage failure when cleanup is ambiguous" {
  local run_dir="$TEST_TMP/failed-stage-run"
  mkdir -m 0700 "$run_dir"

  run bash -c 'source "$1/lib/vm/host-launcher.sh"; VM_HOST_RUN_DIR="$2"; VM_HOST_UNTRACKED_CHILD=1; trap vm_host_exit_cleanup EXIT; exit 73' _ "$TEST_ROOT" "$run_dir"

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
