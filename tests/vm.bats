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
  [[ "$output" != *"-10.0.2.2:"* ]]
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
