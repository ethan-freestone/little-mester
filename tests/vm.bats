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
  [[ "$joined" == *"memory-backend-memfd,id=mem,size=4G,share=on"* ]]
  [[ "$joined" == *"vhost-user-fs-pci,chardev=charfs,tag=workspace"* ]]
  [[ "$joined" != *"virtio-9p"* ]]
  [[ "$joined" != *"host-workspace"* ]]
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

@test "guest agent image build preserves host workspace ownership" {
  run grep -F -- '--build-arg LAB_UID=$(id -u) --build-arg LAB_GID=$(id -g)' "$REPO_ROOT/lib/vm/lifecycle.sh"
  [ "$status" -eq 0 ]
}

@test "guest compose teardown runs from the mounted workspace" {
  run grep -F 'cd /workspace && docker -H' "$REPO_ROOT/lib/vm/lifecycle.sh"
  [ "$status" -eq 0 ]
}
