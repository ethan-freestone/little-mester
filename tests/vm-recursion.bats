#!/usr/bin/env bats
# Recursion policy/snapshot tests use private fixtures and never start a VM.

setup() {
  unset LAB_VM_RECURSION_DEPTH LAB_VM_MAX_DEPTH LAB_VM_CANDIDATE_BUNDLE
  ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
  TEST_ROOT="$(mktemp -d /tmp/lm-vm-recursion.XXXXXX)"
  FIXTURE="$TEST_ROOT/identity"
  printf '1 2\n' >"$FIXTURE"
  chmod 600 "$FIXTURE"
  STATE="$TEST_ROOT/state"
  SOURCE="$TEST_ROOT/source"
  CPUINFO="$TEST_ROOT/cpuinfo"
  mkdir -m 700 "$STATE" "$SOURCE"
  mkdir -m 755 "$SOURCE/lib" "$SOURCE/lib/vm"
  printf '#!/bin/sh\nexit 0\n' >"$SOURCE/lab-vm"
  chmod 755 "$SOURCE/lab-vm"
  printf 'candidate-runner-change\n' >"$SOURCE/lib/vm/runner.sh"
  printf 'candidate-proxy-change\n' >"$SOURCE/lib/vm/proxy.py"
  env GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    git -C "$SOURCE" init --quiet --template=/dev/null
  env GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    git -C "$SOURCE" config user.name 'Recursion Fixture'
  env GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    git -C "$SOURCE" config user.email 'recursion-fixture@localhost.invalid'
  env GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    git -C "$SOURCE" add -- lab-vm lib/vm/runner.sh lib/vm/proxy.py
  env GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    git -C "$SOURCE" commit --quiet -m 'fixture baseline'
  printf 'processor : 0\nflags : fpu vmx sse\n' >"$CPUINFO"
}

teardown() {
  rm -rf -- "$TEST_ROOT"
}

@test "persisted recursion identity controls depth and conflicts fail closed" {
  run bash -c '
    source "$1/lib/vm/recursion.sh"
    source "$1/lib/vm/config.sh"
    export LAB_VM_RECURSION_DEPTH=1 LAB_VM_MAX_DEPTH=2
    vm_recursion_apply_guest_identity "$2" || exit $?
    vm_cli_defaults
    vm_cli_parse --recurse --max-depth 2 run || exit $?
    vm_recursion_configure run || exit $?
    printf "%s %s %s\n" "$VM_GUEST_RECURSION_DEPTH" "$VM_EFFECTIVE_MAX_DEPTH" "$VM_NESTED_LAUNCH"
  ' _ "$ROOT" "$FIXTURE"
  [ "$status" -eq 0 ]
  [ "$output" = "2 2 1" ]

  run bash -c '
    source "$1/lib/vm/recursion.sh"
    export LAB_VM_RECURSION_DEPTH=0 LAB_VM_MAX_DEPTH=2
    vm_recursion_apply_guest_identity "$2"
  ' _ "$ROOT" "$FIXTURE"
  [ "$status" -eq 2 ]

  run bash -c '
    source "$1/lib/vm/recursion.sh"
    export LAB_VM_RECURSION_DEPTH=1 LAB_VM_MAX_DEPTH=3
    vm_recursion_apply_guest_identity "$2"
  ' _ "$ROOT" "$FIXTURE"
  [ "$status" -eq 2 ]
}

@test "recursion defaults to zero, rejects reset/increased limits and refuses at cap" {
  run bash -c '
    source "$1/lib/vm/config.sh"
    source "$1/lib/vm/recursion.sh"
    vm_cli_defaults
    vm_recursion_configure run || exit $?
    printf "%s %s\n" "$VM_GUEST_RECURSION_DEPTH" "$VM_EFFECTIVE_MAX_DEPTH"
  ' _ "$ROOT"
  [ "$status" -eq 0 ]
  [ "$output" = "0 0" ]

  run bash -c '
    source "$1/lib/vm/config.sh"
    source "$1/lib/vm/recursion.sh"
    vm_cli_defaults
    VM_RECURSE=1
    vm_recursion_configure run
  ' _ "$ROOT"
  [ "$status" -eq 2 ]
  [[ "$output" == *"explicit --max-depth"* ]]

  run bash -c '
    source "$1/lib/vm/config.sh"
    source "$1/lib/vm/recursion.sh"
    vm_cli_defaults
    export LAB_VM_RECURSION_DEPTH=1 LAB_VM_MAX_DEPTH=2
    VM_RECURSE=1 VM_MAX_DEPTH=3 VM_SEEN[max-depth]=1
    vm_recursion_configure run
  ' _ "$ROOT"
  [ "$status" -eq 2 ]

  run bash -c '
    source "$1/lib/vm/config.sh"
    source "$1/lib/vm/recursion.sh"
    vm_cli_defaults
    export LAB_VM_RECURSION_DEPTH=2 LAB_VM_MAX_DEPTH=2
    VM_RECURSE=1
    vm_recursion_configure up
  ' _ "$ROOT"
  [ "$status" -eq 2 ]
}

@test "nested KVM and CPU refusal happens before creating a run root or snapshot" {
  run bash -c '
    source "$1/lib/vm/recursion.sh"
    VM_NESTED_LAUNCH=1 VM_RECURSE=1 VM_EFFECTIVE_MAX_DEPTH=2
    vm_recursion_kvm_available() { return 1; }
    vm_recursion_prepare_candidate "$2" child "$3" up fake-kvm "$4"
  ' _ "$ROOT" "$STATE" "$SOURCE" "$CPUINFO"
  [ "$status" -eq 2 ]
  [ -z "$(find "$TEST_ROOT" -maxdepth 1 -name '.little-mester-recursion-*' -print -quit)" ]

  : >"$CPUINFO"
  run bash -c '
    source "$1/lib/vm/recursion.sh"
    VM_NESTED_LAUNCH=1 VM_RECURSE=1 VM_EFFECTIVE_MAX_DEPTH=2
    vm_recursion_kvm_available() { return 0; }
    vm_recursion_prepare_candidate "$2" child "$3" up fake-kvm "$4"
  ' _ "$ROOT" "$STATE" "$SOURCE" "$CPUINFO"
  [ "$status" -eq 2 ]
  [ -z "$(find "$TEST_ROOT" -maxdepth 1 -name '.little-mester-recursion-*' -print -quit)" ]
}

@test "candidate argv binds to the frozen changed launcher and isolates state/capabilities" {
  run bash -c '
    source "$1/lib/vm/config.sh"
    source "$1/lib/vm/recursion.sh"
    vm_cli_defaults
    VM_NESTED_LAUNCH=1 VM_RECURSE=1 VM_EFFECTIVE_MAX_DEPTH=2 VM_MAX_DEPTH=2
    VM_COMMAND=up VM_PROFILE=child VM_WORKSPACE="$3"
    VM_CONFIG_JSON="{\"workload_runtime\":\"none\",\"agent\":\"opencode\",\"opencode_version\":\"2.3.4\",\"model\":{\"provider\":\"none\"}}"
    export LAB_VM_RECURSION_DEPTH=0 LAB_VM_MAX_DEPTH=2
    vm_recursion_kvm_available() { return 0; }
    vm_recursion_prepare_candidate "$2" child "$3" up fake-kvm "$4" || exit $?
    printf "launcher=%s\nworkspace=%s\nstate=%s\ntree=%s\n" \
      "${VM_RECURSION_CANDIDATE_ARGV[0]}" "$VM_RECURSION_CANDIDATE_WORKSPACE" \
      "$VM_RECURSION_STATE_ROOT" "$VM_RECURSION_SOURCE_TREE_SHA256"
    printf "argv=%s\n" "${VM_RECURSION_CANDIDATE_ARGV[*]}"
    printf "env=%s\n" "${VM_RECURSION_CANDIDATE_ENV[*]}"
    printf "candidate-runner=%s\n" "$(cat "$VM_RECURSION_CANDIDATE_WORKSPACE/lib/vm/runner.sh")"
  ' _ "$ROOT" "$STATE" "$SOURCE" "$CPUINFO"
  [ "$status" -eq 0 ]
  [[ "$output" == *"launcher=$TEST_ROOT/.little-mester-recursion-"*"/child/candidate-snapshot/workspace/lab-vm"* ]]
  [[ "$output" == *"--workspace $TEST_ROOT/.little-mester-recursion-"*"/child/candidate-snapshot/workspace"* ]]
  [[ "$output" == *"--recurse --max-depth 2 --metrics off"* ]]
  [[ "$output" == *"MESTER_STATE_DIR=$TEST_ROOT/.little-mester-recursion-"*"/child/state"* ]]
  [[ "$output" == *"LAB_VM_RECURSION_DEPTH=0 LAB_VM_MAX_DEPTH=2"* ]]
  [[ "$output" == *"candidate-runner=candidate-runner-change"* ]]
  [[ "$output" != *METRICS_URL* ]]
  [[ "$output" != *SSH_AUTH_SOCK* ]]
}

@test "candidate handoff rejects workspace substitution and only accepts verified snapshot" {
  mkdir -m 700 "$TEST_ROOT/handoff"
  run bash -c '
    source "$1/lib/vm/recursion.sh"
    result="$(python3 "$1/scripts/vm-recursion-workspace.py" prepare --source "$3" --run-root "$2")" || exit $?
    bundle="$(printf "%s" "$result" | python3 -c "import json,sys; print(json.load(sys.stdin)[\"bundle\"])")"
    workspace="$(printf "%s" "$result" | python3 -c "import json,sys; print(json.load(sys.stdin)[\"workspace\"])")"
    VM_REPO="$workspace" VM_WORKSPACE="$workspace" VM_COMMAND=up
    export LAB_VM_RECURSION_DEPTH=0 LAB_VM_MAX_DEPTH=2 LAB_VM_CANDIDATE_BUNDLE="$bundle"
    vm_recursion_validate_candidate_handoff || exit $?
    printf "%s\n" "$VM_RECURSION_CANDIDATE_HANDOFF"
  ' _ "$ROOT" "$TEST_ROOT/handoff" "$SOURCE"
  [ "$status" -eq 0 ]
  [ "$output" = "1" ]

  run bash -c '
    source "$1/lib/vm/recursion.sh"
    result="$(python3 "$1/scripts/vm-recursion-workspace.py" verify --bundle "$2/candidate-snapshot")" || exit $?
    VM_REPO="$(printf "%s" "$result" | python3 -c "import json,sys; print(json.load(sys.stdin)[\"workspace\"])")"
    VM_WORKSPACE="$3" VM_COMMAND=up
    export LAB_VM_RECURSION_DEPTH=0 LAB_VM_MAX_DEPTH=2 LAB_VM_CANDIDATE_BUNDLE="$2/candidate-snapshot"
    vm_recursion_validate_candidate_handoff
  ' _ "$ROOT" "$TEST_ROOT/handoff" "$SOURCE"
  [ "$status" -eq 2 ]
}

@test "nested profile runner binds proxy startup to its immediate parent depth" {
  run bash -c '
    source "$1/lib/vm/lifecycle.sh"
    vm_runner_start() {
      printf "%s %s %s\\n" "${LAB_VM_RECURSION_DEPTH:-unset}" \
        "${LAB_VM_MAX_DEPTH:-unset}" "$*"
    }
    vm_lifecycle_runner_start_for_depth 2 3 profile run workspace seed overlay 8 none "" ""
    vm_lifecycle_runner_start_for_depth 0 3 top run workspace seed overlay 8 none "" ""
  ' _ "$ROOT"
  [ "$status" -eq 0 ]
  [[ "$output" == *"2 3 profile run workspace seed overlay 8 none"* ]]
  [[ "$output" == *"unset unset top run workspace seed overlay 8 none"* ]]
}
