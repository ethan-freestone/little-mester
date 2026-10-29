#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
  LAB_DIR="$REPO_ROOT"
  source "$REPO_ROOT/lib/vm/runner.sh"
}

@test "QEMU guestfwd uses lazy command targets, not invalid bare addresses" {
  VM_PROXY_PORT=45678
  run _vm_netdev_arg 22222
  [ "$status" -eq 0 ]
  [[ "$output" == *"hostfwd=tcp:127.0.0.1:22222-:22"* ]]
  [[ "$output" == *"guestfwd=tcp:10.0.2.100:3128-cmd:socat - TCP:127.0.0.1:45678"* ]]
  [[ "$output" == *"guestfwd=tcp:10.0.2.100:19532-cmd:socat - TCP:127.0.0.1:19532"* ]]
  [[ "$output" != *"-10.0.2.2:"* ]]
}

@test "VM-only local model forwarders bind host ports to loopback" {
  local compose="$REPO_ROOT/compose.vm-model-api.yaml"
  [ "$(grep -c '127.0.0.1:' "$compose")" -eq 2 ]
  grep -Fq '127.0.0.1:11434:11434' "$compose"
  grep -Fq '127.0.0.1:8080:8080' "$compose"
  grep -Fq 'TCP:ollama:11434' "$compose"
  grep -Fq 'TCP:llamacpp:11434' "$compose"
  grep -Fq 'profiles: [vm-model-api]' "$compose"
  ! grep -Eq '0\.0\.0\.0:(8080|11434)' "$compose"
  ! grep -Fq 'metrics-vm-api' "$compose"
}

@test "VM metrics service is native, loopback-only, and independent of Docker Compose" {
  source "$REPO_ROOT/lib/vm/host-services.sh"
  body="$(declare -f vm_cmd_metrics)"
  [[ "$body" == *'METRICS_BIND_HOST=127.0.0.1'* ]]
  [[ "$body" == *'METRICS_BIND_PORT=19532'* ]]
  [[ "$body" == *'python3 "$LAB_DIR/agent/metrics/gather.py"'* ]]
  [[ "$body" != *'docker compose'* ]]
  grep -Fq 'guestfwd=tcp:10.0.2.100:19532-cmd:socat - TCP:127.0.0.1:19532' "$REPO_ROOT/lib/vm/runner.sh"
  grep -Fq 'metrics)' "$REPO_ROOT/lab-vm"
}

@test "llama.cpp public model alias matches the direct guest model configuration" {
  grep -Fq -- '--alias qwen3.6-35b-a3b-q4_k_m' "$REPO_ROOT/compose.yaml"
  grep -Fq '"model": "llamacpp/qwen3.6-35b-a3b-q4_k_m"' "$REPO_ROOT/agent/opencode/opencode.json"
  grep -Fq 'http://llamacpp:11434/v1' "$REPO_ROOT/agent/opencode/opencode.json"
}

@test "guest agent preflights the selected local model forwarder before OpenCode" {
  source "$REPO_ROOT/lib/vm/lifecycle.sh"
  body="$(declare -f vm_cmd_agent)"
  [[ "$body" == *'http://10.0.2.100:8080/health'* ]]
  [[ "$body" == *'http://10.0.2.100:11434/api/tags'* ]]
  [[ "$body" == *'curl --noproxy 10.0.2.100'* ]]
  [[ "$body" == *'python3 -c'* ]]
  [[ "$body" == *'./lab-vm models up $model_provider'* ]]
  [[ "$body" == *'METRICS_URL=http://10.0.2.100:19532'* ]]
  [[ "$body" == *'log_verbose "Checking $model_provider API reachability'* ]]
  [[ "$body" == *'Guest model probe: $probe_command'* ]]
  [[ "$body" == *'--verbose --http1.1'* ]]
  [[ "$body" == *'Guest model probe failed after ${elapsed}s'* ]]
}

@test "guest agent remote command preserves CLI JSON quotes and TUI plugin wiring" {
  source "$REPO_ROOT/lib/vm/lifecycle.sh"
  body="$(declare -f vm_cmd_agent)"
  [[ "$body" == *"printf -v cli_config_quoted '%q' \"\$cli_config\""* ]]
  [[ "$body" == *'/etc/opencode/plugins/host-metrics-tui/package.json'* ]]
  [[ "$body" == *'/etc/opencode/plugins/little-mester-exit-confirm/package.json'* ]]
  [[ "$body" == *'OPENCODE_CLI_CONFIG_CONTENT=$cli_config_quoted'* ]]
  remote='export OPENCODE_CLI_CONFIG_CONTENT='"$(printf '%q' '{"plugins":["/etc/opencode/plugins/host-metrics-tui"],"keybinds":{"app.exit":"none"}}')"
  bash -n -c "$remote"
  [[ "$remote" == *'\"plugins\"'* ]]
  [[ "$remote" == *'\"app.exit\":\"none\"'* ]]
  actual="$(bash -c "$remote; printf '%s' \"\$OPENCODE_CLI_CONFIG_CONTENT\"")"
  [ "$actual" = '{"plugins":["/etc/opencode/plugins/host-metrics-tui"],"keybinds":{"app.exit":"none"}}' ]
}

@test "metrics TUI uses configurable VM endpoint and surfaces transport failure" {
  local plugin="$REPO_ROOT/agent/opencode/plugins/host-metrics-tui/tui.tsx"
  grep -Fq 'process.env.METRICS_URL || "http://metrics:19532"' "$plugin"
  grep -Fq 'host metrics unavailable' "$plugin"
  grep -Fq '`${metricsBase()}/history?n=60`' "$plugin"
  grep -Fq '`${metricsBase()}/metrics`' "$plugin"
}

@test "lab-vm models up uses standard loopback-only Compose API publication" {
  source "$REPO_ROOT/lib/vm/host-services.sh"
  LAB_DIR="$REPO_ROOT"
  log() { echo "$*"; }
  die() { echo "REFUSED: $*" >&2; exit 1; }
  docker() {
    case "$*" in
      info*) return 0 ;;
      *llama-id*|*metrics-id*) echo '{"8080/tcp":[{"HostIp":"127.0.0.1","HostPort":"8080"}]}' ;;
    esac
  }
  compose_source="$(declare -f vm_model_compose)"
  [[ "$compose_source" == *'WORKSPACE="${WORKSPACE:-$LAB_DIR}" docker compose'* ]]
  vm_model_compose() {
    if [[ "$*" == 'config --format json' ]]; then
      cat <<'JSON'
{"services":{"llamacpp-vm-api":{"ports":[{"target":8080,"published":"8080","host_ip":"127.0.0.1"}]}}}
JSON
    else printf 'compose:%s\n' "$*"; fi
  }
  curl() { return 0; }
  run vm_cmd_models up
  [ "$status" -eq 0 ]
  [[ "$output" == *'compose:config -q'* ]]
  [[ "$output" == *'compose:up -d llamacpp-vm-api'* ]]
  [[ "$output" == *'127.0.0.1:8080'* ]]
  [[ "$output" == *'reachable through Docker loopback-only published ports'* ]]
}

@test "VM host-service debug traces do not corrupt captured Compose JSON" {
  source "$REPO_ROOT/lib/vm/host-services.sh"
  LAB_DIR="$REPO_ROOT"
  VM_DEBUG=1
  log_debug() { echo "debug:$*"; }
  docker() { echo '{"services":{"llamacpp-vm-api":{"ports":[]}}}'; }
  local debug_output
  debug_output="$(vm_model_compose config --format json 2>"$BATS_TEST_TMPDIR/compose-debug.log")"
  [ "$debug_output" = '{"services":{"llamacpp-vm-api":{"ports":[]}}}' ]
  grep -q 'debug:docker compose' "$BATS_TEST_TMPDIR/compose-debug.log"
}

@test "VM API ports are validated as loopback-only and live probes gate success" {
  grep -Fq '_vm_validate_api_publish_config "$service" 127.0.0.1 8080' "$REPO_ROOT/lib/vm/host-services.sh"
  grep -Fq '_vm_probe_published_api "$service" 8080 /health' "$REPO_ROOT/lib/vm/host-services.sh"
  grep -Fq 'docker port "$container"' "$REPO_ROOT/lib/vm/host-services.sh"
  grep -Fq 'configured_bindings={{json .HostConfig.PortBindings}} actual_bindings={{json .NetworkSettings.Ports}}' "$REPO_ROOT/lib/vm/host-services.sh"
  ! grep -Eq 'TCP-LISTEN:|TCP:\$target_ip|socat.*relay' "$REPO_ROOT/lib/vm/host-services.sh"
}

@test "VM API publication validation rejects non-loopback bindings" {
  source "$REPO_ROOT/lib/vm/host-services.sh"
  LAB_DIR="$REPO_ROOT"
  die() { echo "REFUSED: $*" >&2; return 1; }
  vm_model_compose() {
    cat <<'JSON'
{"services":{"llamacpp-vm-api":{"ports":[{"target":8080,"published":"8080","host_ip":"0.0.0.0"}]}}}
JSON
  }
  run _vm_validate_api_publish_config llamacpp-vm-api 127.0.0.1 8080
  [ "$status" -ne 0 ]
}

@test "failed published API probe is reported as failure, not success" {
  source "$REPO_ROOT/lib/vm/host-services.sh"
  VM_DEBUG=0
  curl() { return 7; }
  sleep() { :; }
  _vm_api_diagnostics() { echo "diagnostic:$*"; }
  run _vm_probe_published_api llamacpp-vm-api 8080 /health
  [ "$status" -ne 0 ]
  [[ "$output" == *'diagnostic:llamacpp-vm-api 8080 /health'* ]]
}

@test "models status summarizes absent Docker runtime publication and fails unhealthy" {
  source "$REPO_ROOT/lib/vm/host-services.sh"
  log() { echo "$*"; }
  die() { echo "REFUSED: $*" >&2; exit 1; }
  vm_model_compose() {
    case "$*" in
      'ps ollama-vm-api llamacpp-vm-api') echo 'SERVICE STATUS' ;;
      'ps -q ollama-vm-api') echo '' ;;
      'ps -q llamacpp-vm-api') echo 'llama-container-id' ;;
    esac
  }
  docker() {
    case "$*" in
      inspect*State.Status*) echo running ;;
      inspect*NetworkSettings.Ports*) echo '{"8080/tcp":null}' ;;
      *) return 0 ;;
    esac
  }
  curl() { return 7; }
  run vm_cmd_models status
  [ "$status" -eq 1 ]
  [[ "$output" == *'llamacpp-vm-api: state=running; expected=127.0.0.1:8080; runtime_ports={"8080/tcp":null}'* ]]
  [[ "$output" == *'Docker reports no active runtime host-port mapping.'* ]]
  [[ "$output" == *"Run './lab-vm models diagnose'"* ]]
}

@test "models diagnose helper is a separate read-only tool command" {
  grep -Fq 'scripts/vm-models-diagnose.sh' "$REPO_ROOT/lab-vm"
  grep -Fq 'docker port "$container"' "$REPO_ROOT/scripts/vm-models-diagnose.sh"
  grep -Fq "journalctl -u docker --since '1 hour ago'" "$REPO_ROOT/scripts/vm-models-diagnose.sh"
  ! grep -Eq 'docker compose (up|down|stop|restart)|docker (start|stop|restart|rm|system prune)' "$REPO_ROOT/scripts/vm-models-diagnose.sh"
}

@test "hygiene ShellCheck invocation includes the VM host-services source dependency" {
  grep -Fq 'shellcheck lab scripts/*.sh lib/vm/cloud-init.sh lib/vm/host-services.sh' "$REPO_ROOT/.github/workflows/ci.yml"
}

@test "models diagnose captures publication failure without mutating Docker state" {
  local fakebin="$BATS_TEST_TMPDIR/diagnose-bin"
  mkdir -p "$fakebin"
  cat >"$fakebin/docker" <<'MOCK_DOCKER'
#!/usr/bin/env bash
printf 'WORKSPACE=%s %s\n' "${WORKSPACE:-}" "$*" >>"$MOCK_DOCKER_CALLS"
case "$*" in
  'info') exit 0 ;;
  'context show') echo default ;;
  'version --format '* ) echo 'client=29.9.2 server=29.9.2' ;;
  'info --format '* ) echo 'root=/var/lib/docker security=["name=seccomp"] firewall=iptables' ;;
  *'config --format json') echo '{"services":{"llamacpp-vm-api":{"ports":[{"host_ip":"127.0.0.1","published":"8080","target":8080}]}}}' ;;
  *' ps -a') echo 'SERVICE STATUS' ;;
  *'ps -a -q llamacpp-vm-api') echo sidecar-id ;;
  *'ps -q llamacpp') echo backend-id ;;
  'port sidecar-id') exit 0 ;;
  *'sidecar-id') echo 'name=/api state=running health=none network_mode=internal configured_bindings={"8080/tcp":[{"HostIp":"127.0.0.1","HostPort":"8080"}]} runtime_ports={"8080/tcp":null} networks={}' ;;
  *'backend-id') echo 'backend=/llamacpp state=running health=healthy networks={}' ;;
  *) echo "unexpected docker invocation: $*" >&2; exit 1 ;;
esac
MOCK_DOCKER
  cat >"$fakebin/curl" <<'MOCK_CURL'
#!/usr/bin/env bash
echo 'curl: (7) Connection refused' >&2
exit 7
MOCK_CURL
  cat >"$fakebin/ss" <<'MOCK_SS'
#!/usr/bin/env bash
echo 'State Recv-Q Send-Q Local Address:Port Peer Address:Port'
MOCK_SS
  cat >"$fakebin/journalctl" <<'MOCK_JOURNAL'
#!/usr/bin/env bash
echo '-- No entries --'
MOCK_JOURNAL
  chmod +x "$fakebin"/*
  export MOCK_DOCKER_CALLS="$BATS_TEST_TMPDIR/docker-calls"

  run env PATH="$fakebin:$PATH" bash -c 'docker info; printf "fake_status=%s\\n" "$?"'
  [ "$status" -eq 0 ]
  [[ "$output" == *'fake_status=0'* ]]
  run env PATH="$fakebin:$PATH" VM_DEBUG=0 bash "$REPO_ROOT/scripts/vm-models-diagnose.sh" llamacpp
  if [ "$status" -ne 1 ]; then cat "$MOCK_DOCKER_CALLS" >&2; fi
  [ "$status" -eq 1 ]
  [[ "$output" == *'runtime_ports={"8080/tcp":null}'* ]]
  [[ "$output" == *'docker port: no active published-port mapping reported.'* ]]
  [[ "$output" == *'Host loopback probe failed.'* ]]
  grep -Fq "WORKSPACE=$REPO_ROOT" "$MOCK_DOCKER_CALLS"
  ! grep -Eq 'compose .* (up|down|stop|restart)| (start|stop|restart|rm) ' "$MOCK_DOCKER_CALLS"
}

@test "lab-vm extra delegates to the temporary script with a removal marker" {
  grep -Fq 'exec bash "$LAB_DIR/scripts/vm-extra.sh" "${remaining[@]:1}"' "$REPO_ROOT/lab-vm"
  grep -Fq 'MUST BE REMOVED once ticket #42' "$REPO_ROOT/scripts/vm-extra.sh"
  grep -Fq 'diagnose-model-api' "$REPO_ROOT/scripts/vm-extra.sh"
  grep -Fq 'repair-model-api' "$REPO_ROOT/scripts/vm-extra.sh"
  grep -Fq 'docker inspect --format' "$REPO_ROOT/scripts/vm-extra.sh"
  grep -Fq 'docker logs --tail 40' "$REPO_ROOT/scripts/vm-extra.sh"
  grep -Fq 'VM_DEBUG' "$REPO_ROOT/scripts/vm-extra.sh"
  grep -Fq 'VM_VERBOSE' "$REPO_ROOT/scripts/vm-extra.sh"
  grep -Fq 'VM_VERBOSE="${VM_VERBOSE:-0}"' "$REPO_ROOT/lab-vm"
  grep -Fq 'VM_DEBUG="${VM_DEBUG:-0}"' "$REPO_ROOT/lab-vm"
}

@test "default host-Docker compose publishes no model ports" {
  ! grep -Eq '^\s*ports:' "$REPO_ROOT/compose.yaml"
}

@test "QEMU command pairs virtiofsd with vhost-user and shareable memory" {
  VM_PROXY_PORT=45678
  _vm_build_qemu_command /state demo /state/disk.qcow2 /state/virtiofs.sock 22222 tcg 0 ""
  joined="$(printf '%s\n' "${VM_QEMU_CMD[@]}")"
  [[ "$joined" == *$'-cpu\nWestmere'* ]]
  [[ "$joined" == *"memory-backend-memfd,id=mem,size=4G,share=on"* ]]
  [[ "$joined" == *"vhost-user-fs-pci,chardev=charfs,tag=workspace"* ]]
  [[ "$joined" != *"virtio-9p"* ]]
  [[ "$joined" != *"host-workspace"* ]]
}

@test "KVM QEMU command passes host CPU features through to the guest" {
  _vm_build_qemu_command /state demo /state/disk.qcow2 /state/virtiofs.sock 22222 kvm 1 ""
  joined="$(printf '%s\n' "${VM_QEMU_CMD[@]}")"
  [[ "$joined" == *$'-cpu\nhost'* ]]
  [[ "$joined" == *"-enable-kvm"* ]]
}

@test "cloud-init mounts the matching workspace tag and provisions rootless Docker" {
  run grep -F "mount -t virtiofs workspace /workspace" "$REPO_ROOT/lib/vm/cloud-init.sh"
  [ "$status" -eq 0 ]
  run grep -F "dockerd-rootless-setuptool.sh install" "$REPO_ROOT/lib/vm/cloud-init.sh"
  [ "$status" -eq 0 ]
}

@test "base image is pinned and checksum verified" {
  run grep -F "VM_IMAGE_SHA256=" "$REPO_ROOT/lib/vm/disk.sh"
  [ "$status" -eq 0 ]
  run grep -F "sha256sum --check --status" "$REPO_ROOT/lib/vm/disk.sh"
  [ "$status" -eq 0 ]
}

@test "VM runtime installs OpenCode in the guest rather than building an agent container" {
  run grep -F '_vm_install_guest_harness "$vm_dir"' "$REPO_ROOT/lib/vm/lifecycle.sh"
  [ "$status" -eq 0 ]
  run grep -F 'npm install --global "@opencode/cli@$version"' "$REPO_ROOT/lib/vm/lifecycle.sh"
  [ "$status" -eq 0 ]
  run grep -F 'exec opencode' "$REPO_ROOT/lib/vm/lifecycle.sh"
  [ "$status" -eq 0 ]
}

@test "guest OpenCode version must be pinned to semantic version" {
  source "$REPO_ROOT/lib/vm/lifecycle.sh"
  log() { echo "$*"; }
  VM_OPENCODE_VERSION=latest
  run _vm_install_guest_harness /nonexistent
  [ "$status" -eq 2 ]
  [[ "$output" == *"must be a pinned semantic version"* ]]
}

@test "lab-vm exposes an interactive direct guest agent command" {
  run grep -F 'vm_cmd_agent "$vm_name"' "$REPO_ROOT/lab-vm"
  [ "$status" -eq 0 ]
  run grep -F 'ssh -t' "$REPO_ROOT/lib/vm/lifecycle.sh"
  [ "$status" -eq 0 ]
}

@test "agent launch checks installed runtime and does not reinstall it" {
  source "$REPO_ROOT/lib/vm/lifecycle.sh"
  body="$(declare -f vm_cmd_agent)"
  [[ "$body" == *'opencode --version'* ]]
  [[ "$body" == *'Run ./lab-vm up'* ]]
  [[ "$body" != *'_vm_install_guest_harness'* ]]
  [[ "$body" == *'grep -Eo'* ]]
  [[ "$body" != *'sub(/^v/, \\"'* ]]
  [[ "$body" == *'/etc/opencode/opencode.json'* ]]
  [[ "$body" != *'/etc/opencode/opencode/opencode.json'* ]]
  [[ "$body" == *'/etc/opencode/plugins/little-mester-exit-confirm'* ]]
  [[ "$body" == *'"app.exit":"none"'* ]]
}

@test "guest install copies config to the same path used by the launcher" {
  source "$REPO_ROOT/lib/vm/lifecycle.sh"
  installer="$(declare -f _vm_install_guest_harness)"
  [[ "$installer" == *'tar -C "$LAB_DIR/agent/opencode" -cf - opencode.json plugins'* ]]
  [[ "$installer" == *'/etc/opencode/opencode.json'* ]]
  [[ "$installer" != *'/etc/opencode/opencode/opencode.json'* ]]
}

@test "guest compose teardown runs from the mounted workspace" {
  run grep -F 'cd /workspace && docker -H' "$REPO_ROOT/lib/vm/lifecycle.sh"
  [ "$status" -eq 0 ]
}

@test "cloud-init failure saves diagnostics before cleanup and preserves status" {
  source "$REPO_ROOT/lib/vm/lifecycle.sh"
  log() { echo "$*"; }
  vm_ssh() {
    case "$2" in
      *"status --wait --long"*) echo 'status: done'; return 2 ;;
      *"status --format json"*) echo '{"status":"done","extended_status":"degraded done","recoverable_errors":{"WARNING":["schema validation failed"]}}'; return 2 ;;
      *"schema --system"*) echo 'schema error: unexpected configuration'; return 1 ;;
      *) echo 'rootless setup completed' ;;
    esac
  }
  vm_dir="$(mktemp -d)"
  run _vm_wait_for_cloud_init "$vm_dir"
  [ "$status" -eq 2 ]
  [[ "$output" == *"exited with status 2"* ]]
  [[ "$output" == *"schema error: unexpected configuration"* ]]
  [[ "$output" == *"Preserve logs before --clean"* ]]
  run cat "$vm_dir/provision.log"
  [ "$status" -eq 0 ]
  [[ "$output" == *"rootless setup completed"* ]]
  run cat "$vm_dir/cloud-init-status.json"
  [[ "$output" == *'"recoverable_errors"'* ]]
  run cat "$vm_dir/cloud-init-schema.log"
  [[ "$output" == *"schema error"* ]]
  rm -rf "$vm_dir"
}

@test "diagnostic failures do not mask cloud-init crash timeout or SSH status" {
  source "$REPO_ROOT/lib/vm/lifecycle.sh"
  log() { echo "$*"; }
  vm_ssh() {
    if [[ "$2" == *"status --wait --long"* ]]; then return "$TEST_CLOUD_STATUS"; fi
    return 255
  }
  vm_dir="$(mktemp -d)"
  for TEST_CLOUD_STATUS in 1 124 255; do
    run _vm_wait_for_cloud_init "$vm_dir"
    [ "$status" -eq "$TEST_CLOUD_STATUS" ]
    [[ "$output" == *"exited with status $TEST_CLOUD_STATUS"* ]]
  done
  rm -rf "$vm_dir"
}

@test "cloud-init success does not fetch failure diagnostics" {
  source "$REPO_ROOT/lib/vm/lifecycle.sh"
  log() { :; }
  vm_ssh() { [[ "$2" == *"cloud-init status"* ]]; }
  vm_dir="$(mktemp -d)"
  run _vm_wait_for_cloud_init "$vm_dir"
  [ "$status" -eq 0 ]
  [ ! -e "$vm_dir/provision.log" ]
  rm -rf "$vm_dir"
}

mock_keyscan() {
  mkdir -p "$1/bin"
  export SCAN_ARGS="$1/scan-args"
  cat > "$1/bin/ssh-keyscan" <<'MOCK'
#!/bin/bash
printf '%s\n' "$@" > "$SCAN_ARGS"
printf '%s\n' "${SCAN_KEY:-|1|test|hash ssh-ed25519 AAAATEST}"
echo 'mock scanner diagnostic' >&2
exit "${SCAN_EXIT:-0}"
MOCK
  chmod +x "$1/bin/ssh-keyscan"
  export PATH="$1/bin:$PATH"
}

@test "SSH readiness scans only ed25519 with bounded timeout and saves complete key" {
  log_debug() { echo "$*"; }
  vm_dir="$(mktemp -d)"
  mock_keyscan "$vm_dir"
  run _vm_scan_ssh_host_key "$vm_dir" 22222
  [ "$status" -eq 0 ]
  [ "$(cat "$SCAN_ARGS")" = $'-T\n2\n-t\ned25519\n-p\n22222\n-H\n127.0.0.1' ]
  [ "$(cat "$vm_dir/known_hosts")" = '|1|test|hash ssh-ed25519 AAAATEST' ]
  grep -q 'mock scanner diagnostic' "$vm_dir/ssh-keyscan.log"
  rm -rf "$vm_dir"
}

@test "failed SSH scan logs status and never trusts partial output" {
  log_debug() { echo "$*"; }
  vm_dir="$(mktemp -d)"
  mock_keyscan "$vm_dir"
  echo 'previous trusted key' > "$vm_dir/known_hosts"
  export SCAN_EXIT=124
  run _vm_scan_ssh_host_key "$vm_dir" 22222
  [ "$status" -ne 0 ]
  [[ "$output" == *"exited with status 124"* ]]
  [ "$(cat "$vm_dir/known_hosts")" = 'previous trusted key' ]
  rm -rf "$vm_dir"
}

@test "SSH scan with no ed25519 key does not claim readiness" {
  log_debug() { echo "$*"; }
  vm_dir="$(mktemp -d)"
  mock_keyscan "$vm_dir"
  export SCAN_KEY='|1|test|hash ssh-rsa AAAATEST'
  run _vm_scan_ssh_host_key "$vm_dir" 22222
  [ "$status" -ne 0 ]
  [[ "$output" == *"no ed25519 key"* ]]
  [ ! -f "$vm_dir/known_hosts" ]
  rm -rf "$vm_dir"
}

@test "agent Dockerfile prints sanitized npm diagnostics when OpenCode install fails" {
  dockerfile="$REPO_ROOT/agent/Dockerfile"
  grep -Fq 'npm install -g --ignore-scripts @opencode/cli || {' "$dockerfile"
  grep -Fq '/root/.npm/_logs/*-debug-0.log' "$dockerfile"
  grep -Fq 'grep -viE '\''authorization|_auth|token|password'\''' "$dockerfile"
  grep -Fq 'tail -n 160' "$dockerfile"
  grep -Fq 'exit "$status"' "$dockerfile"
  grep -Fq 'node /tmp/diagnose-opencode-install.cjs "$npm_root"' "$dockerfile"
  grep -Fq 'node ./postinstall.mjs' "$dockerfile"
  grep -Fq 'platform package files and postinstall source are printed above' "$dockerfile"
  [[ -f "$REPO_ROOT/agent/diagnose-opencode-install.cjs" ]]
  grep -Fq 'postinstall_source_begin' "$REPO_ROOT/agent/diagnose-opencode-install.cjs"
  grep -Fq 'exports: pkg.exports' "$REPO_ROOT/agent/diagnose-opencode-install.cjs"
}

@test "rootless Docker startup diagnostics are run before startup failure cleanup" {
  source "$REPO_ROOT/lib/vm/lifecycle.sh"
  diagnostics="$(declare -f _vm_rootless_diagnostics)"
  [[ "$diagnostics" == *"loginctl show-user mester -p Linger -p State -p RuntimePath"* ]]
  [[ "$diagnostics" == *"systemctl --user is-enabled docker"* ]]
  [[ "$diagnostics" == *"cat -n /etc/subuid /etc/subgid"* ]]
  [[ "$diagnostics" == *"cat /home/mester/.config/docker/daemon.json"* ]]
  [[ "$diagnostics" == *"systemctl --user show docker -p Environment"* ]]
  [[ "$diagnostics" == *"getsubids mester"* ]]
  [[ "$diagnostics" == *"journalctl --user -u docker -b"* ]]
  [[ "$diagnostics" == *'rootless-diagnostics.log'* ]]
  body="$(declare -f vm_cmd_up)"
  [[ "$body" == *'_vm_rootless_diagnostics "$vm_dir"'* ]]
  [[ "$body" == *'ERROR: guest Docker is not running rootless'* ]]
  ensure="$(declare -f _vm_ensure_rootless_docker)"
  [[ "$ensure" == *'vm_cloud_init_subid_script | vm_ssh'* ]]
  [[ "$ensure" == *'/home/mester/.config/docker/daemon.json'* ]]
  [[ "$ensure" == *'/home/mester/.config/systemd/user/docker.service.d/proxy.conf'* ]]
  [[ "$ensure" == *'DOCKERD_ROOTLESS_ROOTLESSKIT_FLAGS=--cidr=10.0.3.0/24'* ]]
  [[ "$ensure" == *'Environment=HTTP_PROXY=http://10.0.2.100:3128'* ]]
  [[ "$ensure" == *'systemctl --user restart docker'* ]]
}
