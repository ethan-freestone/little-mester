#!/usr/bin/env bats

setup() {
  ROOT="$(cd "$(dirname "${BATS_TEST_FILENAME}")/.." && pwd)"
  TEST_ROOT="$(mktemp -d)"
  ROUTE_ROOT="$TEST_ROOT/routes"
  mkdir -p "$ROUTE_ROOT"
  cp "$ROOT/lab" "$ROUTE_ROOT/lab"
  cat >"$ROUTE_ROOT/lab-docker" <<'STUB'
#!/usr/bin/env bash
printf 'docker-route\n'
printf '<%s>\n' "$@"
STUB
  cat >"$ROUTE_ROOT/lab-vm" <<'STUB'
#!/usr/bin/env bash
printf 'vm-route\n'
printf '<%s>\n' "$@"
STUB
  chmod +x "$ROUTE_ROOT/lab" "$ROUTE_ROOT/lab-docker" "$ROUTE_ROOT/lab-vm"

  mkdir -p "$TEST_ROOT/bin" "$TEST_ROOT/workspace"
  export LAB_TEST_DOCKER_LOG="$TEST_ROOT/docker-argv.log"
  export LAB_TEST_DOCKER_ENV_LOG="$TEST_ROOT/docker-env.log"
  cat >"$TEST_ROOT/bin/docker" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$@" >> "$LAB_TEST_DOCKER_LOG"
printf 'WORKSPACE=%s\nMOUNT_MODE=%s\nNETWORKS=%s\n' \
  "${WORKSPACE-}" "${MOUNT_MODE-}" "${NETWORKS-}" >> "$LAB_TEST_DOCKER_ENV_LOG"
STUB
  chmod +x "$TEST_ROOT/bin/docker"
}

teardown() {
  rm -rf "$TEST_ROOT"
}

@test "lab keeps the staged Docker route and forwards argv exactly" {
  run "$ROUTE_ROOT/lab" --ro "$TEST_ROOT/workspace" --agent opencode -- "two words" --title
  [ "$status" -eq 0 ]
  [ "$output" = $'docker-route\n<--ro>\n<'"$TEST_ROOT/workspace"$'>\n<--agent>\n<opencode>\n<-->\n<two words>\n<--title>' ]
}

@test "lab vm selects only the explicit VM route and forwards argv exactly" {
  run "$ROUTE_ROOT/lab" vm --permissions ask run --name 'candidate profile'
  [ "$status" -eq 0 ]
  [ "$output" = $'vm-route\n<--permissions>\n<ask>\n<run>\n<--name>\n<candidate profile>' ]
}

@test "launcher help distinguishes experimental VM entry from the Docker transition default" {
  run "$ROOT/lab" --help
  [ "$status" -eq 0 ]
  [[ "$output" == *"./lab vm COMMAND"* ]]
  [[ "$output" == *"Experimental VM route (run/up available; acceptance pending)"* ]]
  [[ "$output" == *"C14 intends VM-default"* ]]
}

@test "public lab vm help lists experimental entry without creating state" {
  local private_state="$TEST_ROOT/private-vm-state"
  run env MESTER_STATE_DIR="$private_state" "$ROOT/lab" vm --help
  [ "$status" -eq 0 ]
  [[ "$output" == *"run [WORKSPACE] [--name NAME]"* ]]
  [[ "$output" == *"up [PROFILE] --workspace PATH"* ]]
  [[ "$output" == *"Not yet implemented"* ]]
  [ ! -e "$private_state" ]
}

@test "Docker OpenCode auto permission maps to exactly one native --auto before or after workspace" {
  run env PATH="$TEST_ROOT/bin:$PATH" LAB_YES=1 \
    "$ROOT/lab-docker" --permissions auto "$TEST_ROOT/workspace"
  [ "$status" -eq 0 ]
  [ "$(grep -cFx -- '--auto' "$LAB_TEST_DOCKER_LOG")" -eq 1 ]

  : >"$LAB_TEST_DOCKER_LOG"
  run env PATH="$TEST_ROOT/bin:$PATH" LAB_YES=1 \
    "$ROOT/lab-docker" "$TEST_ROOT/workspace" --permissions auto -- --title 'a session'
  [ "$status" -eq 0 ]
  [ "$(grep -cFx -- '--auto' "$LAB_TEST_DOCKER_LOG")" -eq 1 ]
  [ "$(grep -cFx -- 'a session' "$LAB_TEST_DOCKER_LOG")" -eq 1 ]
}

@test "Docker ask omits --auto and --auto-accept adds it exactly once" {
  run env PATH="$TEST_ROOT/bin:$PATH" LAB_YES=1 \
    "$ROOT/lab-docker" --permissions ask "$TEST_ROOT/workspace"
  [ "$status" -eq 0 ]
  [ "$(grep -cFx -- '--auto' "$LAB_TEST_DOCKER_LOG" || true)" -eq 0 ]

  : >"$LAB_TEST_DOCKER_LOG"
  run env PATH="$TEST_ROOT/bin:$PATH" LAB_YES=1 PERMISSIONS=auto \
    "$ROOT/lab-docker" "$TEST_ROOT/workspace"
  [ "$status" -eq 0 ]
  [ "$(grep -cFx -- '--auto' "$LAB_TEST_DOCKER_LOG" || true)" -eq 0 ]

  : >"$LAB_TEST_DOCKER_LOG"
  run env PATH="$TEST_ROOT/bin:$PATH" LAB_YES=1 \
    "$ROOT/lab-docker" --auto-accept "$TEST_ROOT/workspace"
  [ "$status" -eq 0 ]
  [ "$(grep -cFx -- '--auto' "$LAB_TEST_DOCKER_LOG")" -eq 1 ]
}

@test "Docker permission mode conflicts and unsupported modes refuse before Compose" {
  run env PATH="$TEST_ROOT/bin:$PATH" LAB_YES=1 \
    "$ROOT/lab-docker" --permissions ask --auto-accept "$TEST_ROOT/workspace"
  [ "$status" -ne 0 ]
  [[ "$output" == *"conflict"* ]]
  [ ! -e "$LAB_TEST_DOCKER_LOG" ]

  run env PATH="$TEST_ROOT/bin:$PATH" LAB_YES=1 \
    "$ROOT/lab-docker" --permissions deny "$TEST_ROOT/workspace"
  [ "$status" -ne 0 ]
  [[ "$output" == *"ask or auto"* ]]
  [ ! -e "$LAB_TEST_DOCKER_LOG" ]
}

@test "Aider refuses OpenCode auto mode and permission overrides after --" {
  run env PATH="$TEST_ROOT/bin:$PATH" LAB_YES=1 \
    "$ROOT/lab-docker" --aider --permissions auto "$TEST_ROOT/workspace"
  [ "$status" -ne 0 ]
  [[ "$output" == *"Aider"* ]]
  [ ! -e "$LAB_TEST_DOCKER_LOG" ]

  run env PATH="$TEST_ROOT/bin:$PATH" LAB_YES=1 \
    "$ROOT/lab-docker" "$TEST_ROOT/workspace" -- --permissions auto
  [ "$status" -ne 0 ]
  [[ "$output" == *"launcher permission"* ]]
  [ ! -e "$LAB_TEST_DOCKER_LOG" ]
}

@test "egress deny remains effective through the Docker fallback route" {
  printf 'permission=false\n' >"$TEST_ROOT/workspace/.mester-egress"
  run env PATH="$TEST_ROOT/bin:$PATH" LAB_YES=1 \
    "$ROOT/lab-docker" "$TEST_ROOT/workspace"
  [ "$status" -ne 0 ]
  [[ "$output" == *"Egress denied"* ]]
  [ ! -e "$LAB_TEST_DOCKER_LOG" ]
}

@test "Docker route retains its Compose project, workspace, metrics, and isolation wiring" {
  run env PATH="$TEST_ROOT/bin:$PATH" LAB_YES=1 \
    "$ROOT/lab-docker" --ro --egress-false "$TEST_ROOT/workspace"
  [ "$status" -eq 0 ]
  grep -Fx "$ROOT/compose.yaml" "$LAB_TEST_DOCKER_LOG"
  grep -Fx -- '--project-directory' "$LAB_TEST_DOCKER_LOG"
  grep -Fx "WORKSPACE=$TEST_ROOT/workspace" "$LAB_TEST_DOCKER_ENV_LOG"
  grep -Fx 'MOUNT_MODE=ro' "$LAB_TEST_DOCKER_ENV_LOG"
  grep -Fx 'NETWORKS=[internal]' "$LAB_TEST_DOCKER_ENV_LOG"
  grep -Fq '      metrics:' "$ROOT/compose.yaml"
  grep -Fq 'condition: service_healthy' "$ROOT/compose.yaml"
  ! grep -Fq '/var/run/docker.sock' "$ROOT/compose.yaml"
  ! grep -Fq 'docker:dind' "$ROOT/compose.yaml"
}
