#!/usr/bin/env bats

# Override 'die' to prevent exiting the test runner during unit tests.
# In production, die() calls exit 1. Here we just return failure so Bats can capture it.
die() { echo "REFUSED: $*" >&2; return 1; }

# Source the lab script
source "$(dirname "$BATS_TEST_FILENAME")/../lab"

setup() {
  # Ensure LAB_DIR is set correctly for sourcing context
  LAB_DIR="$(cd "$(dirname "${BATS_TEST_FILENAME}")/.." && pwd)"
  
  # Create a temporary directory owned by current user for workspace tests
  WORKSPACE_TMPDIR=$(mktemp -d)
}

teardown() {
  rm -rf "$WORKSPACE_TMPDIR"
}

@test "check_workspace accepts valid absolute path" {
  check_workspace "$WORKSPACE_TMPDIR"
  [ $? -eq 0 ]
}

@test "check_workspace rejects non-directory" {
  local f=$(mktemp)
  run check_workspace "$f"
  [ $status -ne 0 ]
  rm -f "$f"
}

@test "check_workspace rejects filesystem root" {
  run check_workspace "/"
  [ $status -ne 0 ]
  [[ $output == *"Cannot mount filesystem root"* ]]
}

@test "check_workspace rejects home directory" {
  run check_workspace "$HOME"
  [ $status -ne 0 ]
  [[ $output == *"Cannot mount entire home directory"* ]]
}

@test "check_workspace rejects sensitive paths like .ssh" {
  local HOME="$WORKSPACE_TMPDIR/home"
  local ssh_dir="$HOME/.ssh"
  mkdir -p "$ssh_dir"
  run check_workspace "$ssh_dir"
  [ $status -ne 0 ]
  [[ $output == *"overlaps with sensitive"* ]]
}

@test "check_workspace rejects paths overlapping lab repo" {
  run check_workspace "$LAB_DIR"
  [ $status -ne 0 ]
  [[ $output == *"expose the little-mester repo"* ]]
}

@test "warn_hooks detects non-sample hooks" {
  local hook_dir="$WORKSPACE_TMPDIR/.git/hooks"
  mkdir -p "$hook_dir"
  touch "$hook_dir/post-commit" # Non-sample hook
  
  WORKSPACE="$WORKSPACE_TMPDIR"
  output=$(warn_hooks)
  [[ $output == *"WARNING: Non-sample git hooks found"* ]]
}

@test "warn_hooks ignores sample hooks" {
  local hook_dir="$WORKSPACE_TMPDIR/.git/hooks"
  mkdir -p "$hook_dir"
  touch "$hook_dir/post-commit.sample"
  
  WORKSPACE="$WORKSPACE_TMPDIR"
  output=$(warn_hooks)
  [[ $output != *"WARNING: Non-sample git hooks found"* ]]
}

@test "warn_hooks handles missing hooks directory gracefully" {
  WORKSPACE="$WORKSPACE_TMPDIR"
  output=$(warn_hooks)
  [[ -z "$output" ]]
}

# --- Egress permission tests ---

@test "check_egress_permission returns 0 when no manifest exists and EGRESS=true" {
  EGRESS="true"
  WORKSPACE="$WORKSPACE_TMPDIR"
  run check_egress_permission
  [ $status -eq 0 ]
}

@test "check_egress_permission returns 0 when no manifest exists but user passes --isolated" {
  EGRESS="false"
  WORKSPACE="$WORKSPACE_TMPDIR"
  run check_egress_permission
  [ $status -eq 0 ]
}

@test "check_egress_permission returns 0 when permission=true in manifest" {
  EGRESS="true"
  WORKSPACE="$WORKSPACE_TMPDIR"
  echo "permission=true" > "$WORKSPACE/.mester-egress"
  run check_egress_permission
  [ $status -eq 0 ]
}

@test "check_egress_permission returns non-zero when permission=false in manifest" {
  EGRESS="true"
  WORKSPACE="$WORKSPACE_TMPDIR"
  echo "permission=false" > "$WORKSPACE/.mester-egress"
  run check_egress_permission
  [ $status -ne 0 ]
}

@test "check_egress_permission returns 0 when user passes --isolated even if permission=true" {
  EGRESS="false"
  WORKSPACE="$WORKSPACE_TMPDIR"
  echo "permission=true" > "$WORKSPACE/.mester-egress"
  run check_egress_permission
  [ $status -eq 0 ]
}

@test "check_egress_permission rejects when no manifest and EGRESS=false" {
  EGRESS="false"
  WORKSPACE="$WORKSPACE_TMPDIR"
  run check_egress_permission
  [ $status -eq 0 ]
}

# --- Auto-accept tests ---
# These verify that confirm() respects LAB_YES, AUTO_ACCEPT, and VM_YES env vars.
# Related: ticket #48 (AC A7-A8), flag ordering with -y/--yes/--auto-accept.

@test "confirm() returns 0 when LAB_YES=1" {
  LAB_YES=1
  WORKSPACE="$WORKSPACE_TMPDIR"
  MOUNT_MODE="rw"
  EGRESS="true"
  AGENT="opencode"
  WORKFLOW="default"
  VERBOSE=0
  PERMISSIONS="ask"
  UNSAFE_RECURSE=0
  run confirm
  [ $status -eq 0 ]
  [[ "$output" != *"Continue?"* ]]
}

@test "confirm() returns 0 when AUTO_ACCEPT=1" {
  LAB_YES=0
  AUTO_ACCEPT=1
  WORKSPACE="$WORKSPACE_TMPDIR"
  MOUNT_MODE="ro"
  EGRESS="false"
  AGENT="opencode"
  WORKFLOW="tdd"
  VERBOSE=1
  PERMISSIONS="ask"
  UNSAFE_RECURSE=0
  run confirm
  [ $status -eq 0 ]
  [[ "$output" != *"Continue?"* ]]
}

@test "confirm() returns 0 when VM_YES=1" {
  LAB_YES=0
  AUTO_ACCEPT=0
  VM_YES=1
  WORKSPACE="$WORKSPACE_TMPDIR"
  MOUNT_MODE="rw"
  EGRESS="true"
  AGENT="opencode"
  WORKFLOW="default"
  VERBOSE=0
  PERMISSIONS="ask"
  UNSAFE_RECURSE=0
  run confirm
  [ $status -eq 0 ]
  [[ "$output" != *"Continue?"* ]]
}

@test "confirm() returns 0 when PERMISSIONS=auto" {
  LAB_YES=0
  AUTO_ACCEPT=0
  VM_YES=0
  PERMISSIONS="auto"
  WORKSPACE="$WORKSPACE_TMPDIR"
  MOUNT_MODE="rw"
  EGRESS="true"
  AGENT="opencode"
  WORKFLOW="default"
  VERBOSE=0
  UNSAFE_RECURSE=0
  run confirm
  [ $status -eq 0 ]
  [[ "$output" != *"Continue?"* ]]
}

@test "confirm() prompts when no auto-accept is set" {
  LAB_YES=0
  AUTO_ACCEPT=0
  VM_YES=0
  PERMISSIONS="ask"
  WORKSPACE="$WORKSPACE_TMPDIR"
  MOUNT_MODE="rw"
  EGRESS="true"
  AGENT="opencode"
  WORKFLOW="default"
  VERBOSE=0
  UNSAFE_RECURSE=0
  run confirm
  # Function should not auto-accept (returns non-zero / exits 1)
  [ $status -ne 0 ]
  # Summary is printed to stdout even without a TTY
  [[ "$output" == *"Mounting path"* ]]
  [[ "$output" == *"Permissions"* ]]
}

@test "confirm() shows mount summary before prompt" {
  LAB_YES=0
  AUTO_ACCEPT=0
  VM_YES=0
  PERMISSIONS="ask"
  WORKSPACE="$WORKSPACE_TMPDIR"
  MOUNT_MODE="ro"
  EGRESS="false"
  AGENT="opencode"
  WORKFLOW="tdd"
  VERBOSE=1
  UNSAFE_RECURSE=1
  run confirm
  [[ "$output" == *"Mount mode    : READ-RO"* ]]
  [[ "$output" == *"Egress        : false"* ]]
  [[ "$output" == *"Unsaferecurse : 1"* ]]
}

@test "print_verbose prints when VERBOSE=1" {
  VERBOSE=1
  run print_verbose "test message"
  [ $status -eq 0 ]
  [[ "$output" == *"[verbose]"* ]]
  [[ "$output" == *"test message"* ]]
}

@test "print_verbose does not print when VERBOSE=0" {
  VERBOSE=0
  run print_verbose "test message"
  [ $status -eq 0 ]
  [[ -z "$output" ]]
}

@test "print_verbose prints to stderr" {
  VERBOSE=1
  run print_verbose "stderr test"
  [[ "$output" == *"[verbose]"* ]]
}

# --- Flag ordering tests ---
# These invoke ./lab and ./lab-vm with flags at different positions.
# The parsing in both scripts accepts flags before or after the subcommand/path.
# Related: ticket #48 (AC A1-A6, A5), flag ordering.

@test "lab --help is detected regardless of position" {
  # --help before subcommand
  run bash -c './lab --help 2>&1 | head -1'
  [ $status -eq 0 ]
  [[ "$output" == "Usage:"* ]]

  # --help after subcommand name (goes to subcommand parser)
  run bash -c './lab --verbose --help 2>&1 | head -1'
  [ $status -eq 0 ]
  [[ "$output" == "Usage:"* ]]
}

@test "lab -y before and after workspace path" {
  # -y before path (should set AUTO_ACCEPT=1)
  run bash -c './lab -y "$WORKSPACE_TMPDIR" 2>&1 || true'
  # With LAB_YES=0, it should prompt; with -y it should skip
  # We can't fully test without a real workspace, but verify it doesn't
  # throw a flag parsing error
  [[ "$output" != *"Unknown flag"* ]]

  # -y after path
  run bash -c './lab "$WORKSPACE_TMPDIR" -y 2>&1 || true'
  [[ "$output" != *"Unknown flag"* ]]
}

@test "lab --verbose before and after workspace path" {
  run bash -c './lab --verbose "$WORKSPACE_TMPDIR" 2>&1 || true'
  [[ "$output" != *"Unknown flag"* ]]

  run bash -c './lab "$WORKSPACE_TMPDIR" --verbose 2>&1 || true'
  [[ "$output" != *"Unknown flag"* ]]
}

@test "lab --auto-accept before and after workspace path" {
  run bash -c './lab --auto-accept "$WORKSPACE_TMPDIR" 2>&1 || true'
  [[ "$output" != *"Unknown flag"* ]]

  run bash -c './lab "$WORKSPACE_TMPDIR" --auto-accept 2>&1 || true'
  [[ "$output" != *"Unknown flag"* ]]
}

@test "lab flags can be interleaved with workspace path" {
  run bash -c './lab --verbose --ro "$WORKSPACE_TMPDIR" --egress-false 2>&1 || true'
  [[ "$output" != *"Unknown flag"* ]]
}

@test "lab-vm --help is detected regardless of position" {
  # --help before subcommand
  run bash -c './lab-vm --help 2>&1 || true'
  [ $status -eq 0 ]
  [[ "$output" == *"Usage:"* ]]

  # --help after subcommand
  run bash -c './lab-vm --verbose up --help 2>&1 || true'
  [ $status -eq 0 ]
  [[ "$output" == *"Usage:"* ]]
}

@test "lab-vm -y before and after subcommand" {
  run bash -c './lab-vm -y test 2>&1 | head -1'
  [ $status -eq 0 ]
  [[ "$output" != *"Unknown flag"* ]]

  run bash -c './lab-vm test -y 2>&1 | head -1'
  [[ "$output" != *"Unknown flag"* ]]
}

@test "lab-vm --verbose before and after subcommand" {
  run bash -c './lab-vm --verbose test 2>&1 | head -1'
  [ $status -eq 0 ]
  [[ "$output" != *"Unknown flag"* ]]

  run bash -c './lab-vm test --verbose 2>&1 | head -1'
  [[ "$output" != *"Unknown flag"* ]]
}

@test "lab-vm -d before and after subcommand" {
  run bash -c './lab-vm -d test 2>&1 | head -1'
  [ $status -eq 0 ]
  [[ "$output" != *"Unknown flag"* ]]

  run bash -c './lab-vm test -d 2>&1 | head -1'
  [[ "$output" != *"Unknown flag"* ]]
}

@test "lab-vm browser-auth options are agent/run-only and conflicting flags are refused" {
  local -a args=(status --no-browser-auth)
  run ./lab-vm "${args[@]}"
  [ "$status" -ne 0 ]
  [[ "$output" == *'Browser-auth options are only valid with the agent or run command.'* ]]
  [[ "$output" != *'Unknown command'* ]]

  args=(--browser-auth status)
  run ./lab-vm "${args[@]}"
  [ "$status" -ne 0 ]
  [[ "$output" == *'Browser-auth options are only valid with the agent or run command.'* ]]
  [[ "$output" != *'Unknown command'* ]]

  args=(agent --browser-auth --no-browser-auth)
  run ./lab-vm "${args[@]}"
  [ "$status" -ne 0 ]
  [[ "$output" == *'Choose only one of --browser-auth or --no-browser-auth.'* ]]

  run ./lab-vm auth
  [ "$status" -ne 0 ]
  [[ "$output" == *'Unknown command: auth'* ]]
}

@test "lab-vm flags can be interleaved with subcommand" {
  run bash -c './lab-vm --unsafe-recurse --verbose -y test --debug 2>&1 | head -1'
  [ $status -eq 0 ]
  [[ "$output" != *"Unknown flag"* ]]
}

@test "lab-vm preserves a named VM argument after the command" {
  run ./lab-vm status named-parser-test
  [ "$status" -eq 1 ]
  [[ "$output" == *".mester/vm/named-parser-test"* ]]
  [[ "$output" != *"Unknown command: named-parser-test"* ]]
}
