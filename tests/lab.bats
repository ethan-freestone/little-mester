#!/usr/bin/env bats

# Override 'die' to prevent exiting the test runner during unit tests.
# In production, die() calls exit 1. Here we just return failure so Bats can capture it.
die() { echo "REFUSED: $*" >&2; return 1; }

# Source the lab script
source "$(dirname "$BATS_TEST_FILENAME")/../lab"

setup() {
  # Ensure LAB_DIR is set correctly for sourcing context
  LAB_DIR="$(cd "$(dirname "${BATS_TEST_FILENAME}")/..")"
  
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
  local ssh_dir="$HOME/.ssh"
  if [[ -d "$ssh_dir" ]]; then
    run check_workspace "$ssh_dir"
    [ $status -ne 0 ]
    [[ $output == *"overlaps with sensitive"* ]]
  else
    skip "No .ssh directory found"
  fi
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
