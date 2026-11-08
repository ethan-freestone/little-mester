#!/usr/bin/env bats
# V-AGENT: pure adapter and fixture transport tests; no guest or provider sockets.

setup() {
  ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
  VM_TEST_ROOT="$(mktemp -d /tmp/little-mester-agent.XXXXXX)"
  mkdir -m 700 "$VM_TEST_ROOT/workspace"
  cat > "$VM_TEST_ROOT/profile-stubs.sh" <<'SH'
vm_lifecycle_profile_read() { printf '%s\n' "$FAKE_PROFILE"; }
vm_lifecycle_field() { python3 -c "import json,sys; print(json.loads(sys.argv[1])[sys.argv[2]])" "$1" "$2"; }
vm_lifecycle_identity_state() { printf running; }
vm_lifecycle_profile_runtime() { printf '%s' "$VM_SERVICES_ROOT"; }
vm_lifecycle_guest_readiness() { printf '%s\n' "$FAKE_MARKER"; }
vm_lifecycle_registry_for_profile() { printf '{"registry":"/run/processes.json","run_id":"fixture","role":"qemu_parent"}'; }
SH
}

teardown() {
  if [[ "$VM_TEST_ROOT" == /tmp/little-mester-agent.* && -O "$VM_TEST_ROOT" ]]; then
    rm -rf -- "$VM_TEST_ROOT"
  fi
}

@test "OpenCode ask omits auto and auto adds it exactly once before passthrough args" {
  run bash -c '
    source "$1/lib/vm/services.sh"
    vm_services_build_agent_argv opencode ask none "" --title "two words"
    printf "<%s>\n" "${VM_SERVICES_AGENT_ARGV[@]}"
  ' _ "$ROOT"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = '<opencode>' ]
  [ "${lines[1]}" = '<--title>' ]
  [ "${lines[2]}" = '<two words>' ]

  run bash -c '
    source "$1/lib/vm/services.sh"
    vm_services_build_agent_argv opencode auto none "" --title "two words"
    printf "<%s>\n" "${VM_SERVICES_AGENT_ARGV[@]}"
  ' _ "$ROOT"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = '<opencode>' ]
  [ "${lines[1]}" = '<--auto>' ]
  [ "${lines[2]}" = '<--title>' ]
  [ "${lines[3]}" = '<two words>' ]
}

@test "OpenCode permission overrides in passthrough are refused rather than duplicated" {
  local flag
  for flag in --auto --auto-accept; do
    run bash -c 'source "$1/lib/vm/services.sh"; vm_services_build_agent_argv opencode ask none "" "$2"' \
      _ "$ROOT" "$flag"
    [ "$status" -eq 2 ]
  done
  run bash -c 'source "$1/lib/vm/services.sh"; vm_services_build_agent_argv opencode ask none "" --model ollama/qwen3:8b' \
    _ "$ROOT"
  [ "$status" -eq 2 ]
  run bash -c 'source "$1/lib/vm/services.sh"; vm_services_build_agent_argv opencode ask none "" --cwd /etc' \
    _ "$ROOT"
  [ "$status" -eq 2 ]
  run bash -c 'source "$1/lib/vm/services.sh"; vm_services_build_agent_argv opencode auto none "" --auto' \
    _ "$ROOT"
  [ "$status" -eq 2 ]
}

@test "Aider adapter maps local provider endpoints without OpenCode permission flags" {
  run bash -c '
    source "$1/lib/vm/services.sh"
    vm_services_build_agent_argv aider ask ollama qwen3:14b
    printf "argv=<%s>\n" "${VM_SERVICES_AGENT_ARGV[@]}"
    printf "env=<%s>\n" "${VM_SERVICES_AGENT_ENV[@]}"
  ' _ "$ROOT"
  [ "$status" -eq 0 ]
  [[ "$output" == *'argv=</opt/little-mester/python-tools/bin/aider>'* ]]
  [[ "$output" == *'argv=<--model>'* ]]
  [[ "$output" == *'argv=<ollama_chat/qwen3:14b>'* ]]
  [[ "$output" == *'env=<OLLAMA_API_BASE=http://10.0.2.100:11434>'* ]]
  [[ "$output" != *'--auto'* ]]

  run bash -c '
    source "$1/lib/vm/services.sh"
    vm_services_build_agent_argv aider ask llamacpp coder-q4
    printf "<%s>\n" "${VM_SERVICES_AGENT_ARGV[@]}"
    printf "<%s>\n" "${VM_SERVICES_AGENT_ENV[@]}"
  ' _ "$ROOT"
  [ "$status" -eq 0 ]
  [[ "$output" == *'<openai/coder-q4>'* ]]
  [[ "$output" == *'<OPENAI_API_BASE=http://10.0.2.100:8080/v1>'* ]]
}

@test "Aider rejects unsupported automatic permission mode and OpenCode-only flags" {
  run bash -c 'source "$1/lib/vm/services.sh"; vm_services_build_agent_argv aider auto none ""' _ "$ROOT"
  [ "$status" -eq 2 ]
  for flag in --auto --auto-accept; do
    run bash -c 'source "$1/lib/vm/services.sh"; vm_services_build_agent_argv aider ask none "" "$2"' \
      _ "$ROOT" "$flag"
    [ "$status" -eq 2 ]
  done
  run bash -c 'source "$1/lib/vm/services.sh"; vm_services_build_agent_argv aider ask none "" --yes-always' _ "$ROOT"
  [ "$status" -eq 2 ]
}

@test "Aider remote and model-none adapters do not invent or forward local provider URLs" {
  run bash -c '
    source "$1/lib/vm/services.sh"
    vm_services_build_agent_argv aider ask remote openai/gpt-4.1
    printf "<%s>\n" "${VM_SERVICES_AGENT_ARGV[@]}"
    printf "<%s>\n" "${VM_SERVICES_AGENT_ENV[@]}"
  ' _ "$ROOT"
  [ "$status" -eq 0 ]
  [[ "$output" == *'<openai/gpt-4.1>'* ]]
  [[ "$output" != *'_API_BASE='* ]]

  run bash -c '
    source "$1/lib/vm/services.sh"
    vm_services_build_agent_argv aider ask none ""
    printf "<%s>\n" "${VM_SERVICES_AGENT_ARGV[@]}"
    printf "<%s>\n" "${VM_SERVICES_AGENT_ENV[@]}"
  ' _ "$ROOT"
  [ "$status" -eq 0 ]
  [[ "$output" != *'--model'* ]]
  [[ "$output" != *'_API_BASE='* ]]
}

@test "cwd resolves only to directories inside the canonical workspace mount" {
  mkdir -p "$VM_TEST_ROOT/workspace/trees/one"
  mkdir -p "$VM_TEST_ROOT/outside"
  ln -s "$VM_TEST_ROOT/outside" "$VM_TEST_ROOT/workspace/escape"

  run bash -c '
    source "$1/lib/vm/services.sh"
    vm_services_resolve_cwd "$2" trees/one
    printf "%s\n" "$VM_SERVICES_CWD"
  ' _ "$ROOT" "$VM_TEST_ROOT/workspace"
  [ "$status" -eq 0 ]
  [ "$output" = 'trees/one' ]

  for path in ../outside /etc escape; do
    run bash -c 'source "$1/lib/vm/services.sh"; vm_services_resolve_cwd "$2" "$3"' \
      _ "$ROOT" "$VM_TEST_ROOT/workspace" "$path"
    [ "$status" -eq 2 ]
  done
}

@test "model diagnostics are read-only and model-none does not call a provider" {
  local calls="$VM_TEST_ROOT/curl.log"
  cat > "$VM_TEST_ROOT/curl" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_CURL_LOG"
printf '200'
SH
  chmod 755 "$VM_TEST_ROOT/curl"

  local profile marker
  profile="{\"status\":\"ready\",\"workspace\":\"$VM_TEST_ROOT/workspace\",\"runtime_identity\":{\"argv\":[\"qemu-system-x86_64\",\"-netdev\",\"user,id=net0,guestfwd=tcp:10.0.2.100:3128-cmd:socat - TCP:127.0.0.1:3128,restrict=on\"]}}"
  marker='{"agent":"opencode","model_provider":"none","model_port":null,"model_id":""}'
  run env VM_SERVICES_CURL_BIN="$VM_TEST_ROOT/curl" FAKE_CURL_LOG="$calls" \
    FAKE_PROFILE="$profile" FAKE_MARKER="$marker" \
    bash -c 'source "$1/lib/vm/services.sh"; source "$2"; vm_services_models status "" /fixture p' \
    _ "$ROOT" "$VM_TEST_ROOT/profile-stubs.sh"
  [ "$status" -eq 0 ]
  [ ! -e "$calls" ]

  profile="{\"status\":\"ready\",\"workspace\":\"$VM_TEST_ROOT/workspace\",\"runtime_identity\":{\"argv\":[\"qemu-system-x86_64\",\"-netdev\",\"user,id=net0,guestfwd=tcp:10.0.2.100:3128-cmd:socat - TCP:127.0.0.1:3128,guestfwd=tcp:10.0.2.100:11434-cmd:socat - TCP:127.0.0.1:23456,restrict=on\"]}}"
  marker='{"agent":"opencode","model_provider":"ollama","model_port":23456,"model_id":"qwen3:8b"}'
  run env VM_SERVICES_CURL_BIN="$VM_TEST_ROOT/curl" FAKE_CURL_LOG="$calls" \
    FAKE_PROFILE="$profile" FAKE_MARKER="$marker" \
    bash -c 'source "$1/lib/vm/services.sh"; source "$2"; vm_services_models status ollama /fixture p' \
    _ "$ROOT" "$VM_TEST_ROOT/profile-stubs.sh"
  [ "$status" -eq 0 ]
  [[ "$(cat "$calls")" == *'http://127.0.0.1:23456/api/tags' ]]
  [[ "$(cat "$calls")" == *'--max-time 3'* ]]
  [[ "$(cat "$calls")" == *"--noproxy * --proxy "* ]]
}

@test "tampered guest marker cannot trigger an arbitrary loopback provider probe" {
  local calls="$VM_TEST_ROOT/curl.log"
  cat > "$VM_TEST_ROOT/curl" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_CURL_LOG"
printf '200'
SH
  chmod 755 "$VM_TEST_ROOT/curl"
  local profile marker
  profile="{\"status\":\"ready\",\"workspace\":\"$VM_TEST_ROOT/workspace\",\"runtime_identity\":{\"argv\":[\"qemu-system-x86_64\",\"-netdev\",\"user,id=net0,guestfwd=tcp:10.0.2.100:3128-cmd:socat - TCP:127.0.0.1:3128,guestfwd=tcp:10.0.2.100:11434-cmd:socat - TCP:127.0.0.1:23456,restrict=on\"]}}"
  marker='{"agent":"opencode","model_provider":"ollama","model_port":33333,"model_id":"qwen3:8b"}'
  run env VM_SERVICES_CURL_BIN="$VM_TEST_ROOT/curl" FAKE_CURL_LOG="$calls" \
    FAKE_PROFILE="$profile" FAKE_MARKER="$marker" \
    bash -c 'source "$1/lib/vm/services.sh"; source "$2"; vm_services_models status "" /fixture p' \
    _ "$ROOT" "$VM_TEST_ROOT/profile-stubs.sh"
  [ "$status" -eq 2 ]
  [ ! -e "$calls" ]

  marker='{"agent":"opencode","model_provider":"llamacpp","model_port":23456,"model_id":"qwen3:8b"}'
  run env VM_SERVICES_CURL_BIN="$VM_TEST_ROOT/curl" FAKE_CURL_LOG="$calls" \
    FAKE_PROFILE="$profile" FAKE_MARKER="$marker" \
    bash -c 'source "$1/lib/vm/services.sh"; source "$2"; vm_services_models status "" /fixture p' \
    _ "$ROOT" "$VM_TEST_ROOT/profile-stubs.sh"
  [ "$status" -eq 2 ]
  [ ! -e "$calls" ]
}

@test "QEMU model forward extraction accepts exact absent and rejects ambiguous forwards" {
  local profile
  profile='{"runtime_identity":{"argv":["qemu-system-x86_64","-netdev","user,id=net0,guestfwd=tcp:10.0.2.100:11434-cmd:socat - TCP:127.0.0.1:23456,restrict=on"]}}'
  run bash -c 'source "$1/lib/vm/services.sh"; vm_services_profile_model_forward "$2"; printf "%s:%s\n" "$VM_SERVICES_FORWARD_PROVIDER" "$VM_SERVICES_FORWARD_PORT"' \
    _ "$ROOT" "$profile"
  [ "$status" -eq 0 ]
  [ "$output" = 'ollama:23456' ]

  profile='{"runtime_identity":{"argv":["qemu-system-x86_64","-netdev","user,id=net0,guestfwd=tcp:10.0.2.100:3128-cmd:socat - TCP:127.0.0.1:3128,restrict=on"]}}'
  run bash -c 'source "$1/lib/vm/services.sh"; vm_services_profile_model_forward "$2"; printf "%s:%s\n" "$VM_SERVICES_FORWARD_PROVIDER" "$VM_SERVICES_FORWARD_PORT"' \
    _ "$ROOT" "$profile"
  [ "$status" -eq 0 ]
  [ "$output" = 'none:' ]

  profile='{"runtime_identity":{"argv":["qemu-system-x86_64","-netdev","user,id=net0,guestfwd=tcp:10.0.2.100:11434-cmd:socat - TCP:127.0.0.1:23456,guestfwd=tcp:10.0.2.100:8080-cmd:socat - TCP:127.0.0.1:34567,restrict=on"]}}'
  run bash -c 'source "$1/lib/vm/services.sh"; vm_services_profile_model_forward "$2"' _ "$ROOT" "$profile"
  [ "$status" -eq 2 ]
}

@test "agent launch keeps SSH sessions independent and propagates the child exit status" {
  local commands="$VM_TEST_ROOT/commands.log"
  mkdir -p "$VM_TEST_ROOT/workspace/trees/one"
  run env VM_SERVICES_TEST_COMMAND_LOG="$commands" \
    VM_SERVICES_TEST_WORKSPACE="$VM_TEST_ROOT/workspace" \
    VM_SERVICES_TEST_STATE="$VM_TEST_ROOT/state" \
    VM_SERVICES_TEST_PROFILE="{\"status\":\"ready\",\"workspace\":\"$VM_TEST_ROOT/workspace\",\"runtime_identity\":{\"ssh_port\":2200,\"argv\":[\"qemu-system-x86_64\",\"-netdev\",\"user,id=net0,guestfwd=tcp:10.0.2.100:3128-cmd:socat - TCP:127.0.0.1:3128,restrict=on\"]}}" \
    bash -c '
      source "$1/lib/vm/services.sh"
      source "$2/profile-stubs.sh"
      export VM_SERVICES_ROOT=/frozen/runtime
      export FAKE_PROFILE="$VM_SERVICES_TEST_PROFILE"
      export FAKE_MARKER="{\"agent\":\"opencode\",\"model_provider\":\"none\",\"model_port\":null,\"model_id\":\"\"}"
      vm_ssh_listener_verified() { return 0; }
      vm_ssh_interactive() { printf "%s\n" "$*" >> "$VM_SERVICES_TEST_COMMAND_LOG"; return "${FAKE_AGENT_RC:-0}"; }
      VM_REPO=/frozen/runtime
      VM_LIFECYCLE_PYTHON=python3
      if FAKE_AGENT_RC=23 vm_services_agent "$VM_SERVICES_TEST_STATE" p ask "" off --title "two words"; then rc1=0; else rc1=$?; fi
      if FAKE_AGENT_RC=17 vm_services_agent "$VM_SERVICES_TEST_STATE" p ask "trees/one" off --title second; then rc2=0; else rc2=$?; fi
      [[ "$rc1" == 23 && "$rc2" == 17 ]]
    ' _ "$ROOT" "$VM_TEST_ROOT"
  [ "$status" -eq 0 ]
  [ "$(wc -l < "$commands")" -eq 2 ]
  [[ "$(cat "$commands")" == *'bash -lc'* ]]
  [[ "$(cat "$commands")" == *'two\\ words'* ]]
  [[ "$(cat "$commands")" == *'/workspace/trees/one'* ]]
  [[ "$(cat "$commands")" == *'pwd -P'* ]]
}
