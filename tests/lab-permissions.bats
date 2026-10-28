#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
  CASE_DIR="$(mktemp -d)"
  mkdir -p "$CASE_DIR/bin" "$CASE_DIR/workspace"
  export CAPTURE="$CASE_DIR/argv"
  cat > "$CASE_DIR/bin/docker" <<'MOCK'
#!/bin/bash
printf '%s\n' "$@" > "$CAPTURE"
exit "${DOCKER_TEST_EXIT:-0}"
MOCK
  chmod +x "$CASE_DIR/bin/docker"
  export PATH="$CASE_DIR/bin:$PATH"
}

teardown() { rm -rf "$CASE_DIR"; }

@test "auto-accept before and after path produces identical clean OpenCode argv" {
  run "$REPO_ROOT/lab" --auto-accept "$CASE_DIR/workspace" --session example
  [ "$status" -eq 0 ]
  cp "$CAPTURE" "$CASE_DIR/before"
  run "$REPO_ROOT/lab" "$CASE_DIR/workspace" --auto-accept --session example
  [ "$status" -eq 0 ]
  cmp "$CAPTURE" "$CASE_DIR/before"
  [ "$(grep -cx -- '--auto' "$CAPTURE")" -eq 1 ]
  ! grep -qx -- '--auto-accept' "$CAPTURE"
  [ "$(tail -4 "$CAPTURE")" = $'opencode\n--auto\n--session\nexample' ]
  run "$REPO_ROOT/lab" bench --help
  [ "$status" -eq 0 ]
  [[ "$output" == *"Usage: ./lab bench"* ]]
}

@test "no-path unsafe-recurse consumes flags and preserves agent arguments" {
  cd "$CASE_DIR/workspace"
  run "$REPO_ROOT/lab" --unsafe-recurse --auto-accept -- --session example
  [ "$status" -eq 0 ]
  [ "$(tail -4 "$CAPTURE")" = $'opencode\n--auto\n--session\nexample' ]
}

@test "permissions auto requests CLI auto mode without changing config" {
  run "$REPO_ROOT/lab" "$CASE_DIR/workspace" --permissions auto
  [ "$status" -eq 0 ]
  [ "$(tail -2 "$CAPTURE")" = $'opencode\n--auto' ]
  ! grep -qx -- '--permissions' "$CAPTURE"
}

@test "permissions ask does not inject auto and explicit conflict is refused" {
  run bash -c 'source "$1/lab"; dc() { printf "%s\n" "$@"; }; PERMISSIONS=ask; agent_run' _ "$REPO_ROOT"
  [ "$status" -eq 0 ]
  [[ "$output" != *"--auto"* ]]
  run "$REPO_ROOT/lab" "$CASE_DIR/workspace" --permissions ask --auto-accept
  [ "$status" -ne 0 ]
  [[ "$output" == *"conflicts"* ]]
  [ ! -e "$CAPTURE" ]
}

@test "unsupported deny mode and missing mode value fail clearly" {
  run "$REPO_ROOT/lab" -y "$CASE_DIR/workspace" --permissions deny
  [ "$status" -ne 0 ]
  [[ "$output" == *"Unsupported --permissions"* ]]
  run "$REPO_ROOT/lab" "$CASE_DIR/workspace" --permissions
  [ "$status" -ne 0 ]
  [[ "$output" == *"requires a value"* ]]
  [ ! -e "$CAPTURE" ]
}

@test "aider never receives the OpenCode auto flag" {
  run "$REPO_ROOT/lab" "$CASE_DIR/workspace" --aider --auto-accept
  [ "$status" -eq 0 ]
  [ "$(tail -1 "$CAPTURE")" = aider ]
  ! grep -qx -- '--auto' "$CAPTURE"
}

@test "Docker launch failures are not swallowed" {
  export DOCKER_TEST_EXIT=17
  run "$REPO_ROOT/lab" "$CASE_DIR/workspace" --auto-accept
  [ "$status" -eq 17 ]
}
