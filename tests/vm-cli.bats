#!/usr/bin/env bats
# V-CONFIG: launcher parsing and read-only setup; never run real installers/VMs.
bats_require_minimum_version 1.5.0

setup() {
  ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
  # Bats intermediates inherit the host umask and can be group writable. Keep
  # private-state fixtures under their own private ancestor, not weaker guards.
  VM_TEST_ROOT="$(mktemp -d /tmp/little-mester-cli.XXXXXX)"
  export HOME="$VM_TEST_ROOT/home"
  export MESTER_STATE_DIR="$HOME/state"
  mkdir -m 700 "$HOME"
  mkdir -p "$BATS_TEST_TMPDIR/workspace"
  unset VM_YES LAB_YES VM_VERBOSE VM_DEBUG VM_OPENCODE_VERSION
  unset LAB_VM_RECURSION_DEPTH LAB_VM_MAX_DEPTH VM_STATE_DIR VM_DIR_BASE
}

teardown() {
  if [[ "$VM_TEST_ROOT" == /tmp/little-mester-cli.* && -O "$VM_TEST_ROOT" ]]; then
    rm -rf -- "$VM_TEST_ROOT"
  fi
}

parse() {
  bash -c '
    source "$1/lib/vm/config.sh"
    shift
    vm_cli_defaults
    vm_cli_parse "$@" || exit $?
    printf "%s\n" "$VM_COMMAND" "$VM_PROFILE" "$VM_WORKSPACE" \
      "$VM_YES" "$VM_PERMISSIONS" "$VM_VERBOSE" "$VM_DEBUG" \
      "$VM_RECURSE" "$VM_UNSAFE_RECURSE" "$VM_MAX_DEPTH" "$VM_MEMORY"
    printf "arg=<%s>\n" "${VM_AGENT_ARGS[@]}"
  ' _ "$ROOT" "$@"
}

@test "CLI flags before and after workspace parse identically" {
  run parse --name candidate -v --permissions auto run "$BATS_TEST_TMPDIR/workspace"
  [ "$status" -eq 0 ]
  local before="$output"
  run parse run "$BATS_TEST_TMPDIR/workspace" --permissions auto -v --name candidate
  [ "$status" -eq 0 ]
  [ "$before" = "$output" ]
  [ "${lines[0]}" = run ]
  [ "${lines[1]}" = candidate ]
  [ "${lines[4]}" = auto ]
}

@test "CLI double dash preserves quoted agent flags without consuming them" {
  run parse run "$BATS_TEST_TMPDIR/workspace" -- --name 'agent value' --permissions deny
  [ "$status" -eq 0 ]
  [ "${lines[1]}" = default ]
  [[ "$output" == *'arg=<--name>'* ]]
  [[ "$output" == *'arg=<agent value>'* ]]
  [[ "$output" == *'arg=<deny>'* ]]
}

@test "CLI yes is confirmation-only and auto is a separate agent request" {
  run parse run "$BATS_TEST_TMPDIR/workspace" --yes
  [ "$status" -eq 0 ]
  [ "${lines[3]}" = 1 ]
  [ "${lines[4]}" = ask ]
  run parse run "$BATS_TEST_TMPDIR/workspace" --auto-accept
  [ "$status" -eq 0 ]
  [ "${lines[3]}" = 0 ]
  [ "${lines[4]}" = auto ]
}

@test "CLI rejects invalid conflicting duplicate and missing values" {
  local args
  for args in '--name ../escape' '--name a --name b' '--name' \
      '--permissions deny' '--permissions' '--permissions ask --auto-accept' \
      '--auto-accept --permissions ask' '--permissions auto --permissions auto' \
      '--memory 0' '--memory 129' '--memory 01' '--memory' \
      '--max-depth 33 --recurse' '--max-depth -1 --recurse' '--max-depth 1' \
      '--browser-auth --no-browser-auth' '--unknown'; do
    # Intentional fixture word splitting: every case above consists of simple tokens.
    # shellcheck disable=SC2086 # Matrix entries are intentionally simple argv tokens.
    run bash -c 'source "$1/lib/vm/config.sh"; vm_cli_defaults; shift; vm_cli_parse run "$@"' \
      _ "$ROOT" $args
    [ "$status" -eq 2 ]
    [ ! -e "$MESTER_STATE_DIR" ]
  done
}

@test "CLI setup-only and command-only options reject misplaced use" {
  run parse context --install-prereqs
  [ "$status" -eq 2 ]
  run parse agent --memory 4
  [ "$status" -eq 2 ]
  run parse context --recurse
  [ "$status" -eq 2 ]
  run parse setup unexpected
  [ "$status" -eq 2 ]
  run parse context -- --secret ignored
  [ "$status" -eq 2 ]
}

@test "CLI unsafe recurse implies recurse but never auto permissions" {
  run parse --unsafe-recurse --max-depth 2 run "$ROOT"
  [ "$status" -eq 0 ]
  [ "${lines[4]}" = ask ]
  [ "${lines[7]}" = 1 ]
  [ "${lines[8]}" = 1 ]
  [ "${lines[9]}" = 2 ]
}

@test "CLI debug implies verbosity and is not a shell trace" {
  run parse run "$BATS_TEST_TMPDIR/workspace" -d
  [ "$status" -eq 0 ]
  [ "${lines[5]}" = 1 ]
  [ "${lines[6]}" = 1 ]
  [[ "$output" != *'+ '* ]]
}

@test "CLI debug explains private-state rejection without exposing file contents" {
  mkdir -m 700 "$MESTER_STATE_DIR"
  mkdir -m 700 "$MESTER_STATE_DIR/logs"
  printf '%s\n' 'CANARY_SECRET_CONTENT' > "$MESTER_STATE_DIR/logs/legacy.log"
  chmod 644 "$MESTER_STATE_DIR/logs/legacy.log"

  run "$ROOT/lab-vm" list -d
  [ "$status" -eq 2 ]
  [[ "$output" == *"[debug] state_root.canonical=PASS"* ]]
  [[ "$output" == *"[debug] state_root.root_metadata=PASS"* ]]
  [[ "$output" == *"[debug] state_root.private_tree=FAIL"* ]]
  [[ "$output" == *'path="logs/legacy.log" reason=file_mode'* ]]
  [[ "$output" == *"mode=0644 links=1"* ]]
  [[ "$output" != *"CANARY_SECRET_CONTENT"* ]]
}

@test "CLI profile positional selection is exact and not duplicated by name flag" {
  run parse up target --workspace "$BATS_TEST_TMPDIR/workspace"
  [ "$status" -eq 0 ]
  [ "${lines[1]}" = target ]
  run parse up target --name other
  [ "$status" -eq 2 ]
}

@test "CLI recognizes explicit base-cache action without doing state work while parsing" {
  run parse base
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = base ]
  [ ! -e "$MESTER_STATE_DIR" ]
  run bash "$ROOT/lab-vm" --help
  [ "$status" -eq 0 ]
  [[ "$output" == *"base                            Verify/download the pinned immutable base image"* ]]
}

@test "update targets and all conflicts are rejected before state or SSH work" {
  run bash -c 'source "$1/lib/vm/config.sh"; vm_cli_defaults; shift; vm_cli_parse "$@"; printf "%s\n" "${VM_COMMAND}" "${VM_TARGETS[*]}"' \
    _ "$ROOT" update config opencode
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = update ]
  [ "${lines[1]}" = "config opencode" ]

  run bash -c 'source "$1/lib/vm/config.sh"; vm_cli_defaults; shift; vm_cli_parse "$@"' \
    _ "$ROOT" --all update
  [ "$status" -eq 0 ]
  run bash -c 'source "$1/lib/vm/config.sh"; vm_cli_defaults; shift; vm_cli_parse "$@"' \
    _ "$ROOT" update --all tools
  [ "$status" -eq 2 ]
  run bash -c 'source "$1/lib/vm/config.sh"; vm_cli_defaults; shift; vm_cli_parse "$@"' \
    _ "$ROOT" update config config
  [ "$status" -eq 2 ]
  run bash -c 'source "$1/lib/vm/config.sh"; vm_cli_defaults; shift; vm_cli_parse "$@"' \
    _ "$ROOT" update host
  [ "$status" -eq 2 ]
  run bash -c 'source "$1/lib/vm/config.sh"; vm_cli_defaults; shift; vm_cli_parse "$@"' \
    _ "$ROOT" install-tools target --all
  [ "$status" -eq 2 ]
  [ ! -e "$MESTER_STATE_DIR" ]
}

@test "update refuses an absent profile before VM state allocation or SSH" {
  run "$ROOT/lab-vm" update
  [ "$status" -eq 2 ]
  [ ! -e "$MESTER_STATE_DIR/vm" ]
}

@test "proxy upstream args are explicit and derived only from valid recursion identity" {
  run bash -c 'source "$1/lib/vm/config.sh"; vm_proxy_upstream_args; declare -p VM_PROXY_UPSTREAM_ARGS' \
    _ "$ROOT"
  [ "$status" -eq 0 ]
  [[ "$output" == *'declare -a VM_PROXY_UPSTREAM_ARGS=()'* ]]
  # shellcheck disable=SC2016 # Child Bash expands the result array after sourcing config.
  run env LAB_VM_RECURSION_DEPTH=1 LAB_VM_MAX_DEPTH=2 bash -c \
    'source "$1/lib/vm/config.sh"; vm_proxy_upstream_args; printf "<%s>\n" "${VM_PROXY_UPSTREAM_ARGS[@]}"' \
    _ "$ROOT"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = '<--upstream-proxy>' ]
  [ "${lines[1]}" = '<http://10.0.2.100:3128>' ]
  # shellcheck disable=SC2016 # Child Bash sources config and validates its inherited identity.
  run env LAB_VM_RECURSION_DEPTH=1 LAB_VM_MAX_DEPTH=0 bash -c \
    'source "$1/lib/vm/config.sh"; vm_proxy_upstream_args' _ "$ROOT"
  [ "$status" -eq 2 ]
  [[ "$output" != *'10.0.2.100'* ]]
}

@test "CLI help works without private config or runtime state" {
  run bash "$ROOT/lab-vm" --help
  [ "$status" -eq 0 ]
  [[ "$output" == *'run [WORKSPACE] [--name NAME]'* ]]
  [[ "$output" == *'Not yet implemented (refuse with exit 2)'* ]]
  [ ! -e "$MESTER_STATE_DIR" ]
}

@test "experimental up invokes lifecycle without attaching an agent" {
  run bash -c '
    source "$1/lib/vm/lifecycle.sh"
    vm_lifecycle_up() { printf "UP"; printf " <%s>" "$@"; printf "\n"; }
    vm_lifecycle_profile_runtime() { printf "RUNTIME_LOOKUP_IS_FORBIDDEN\n"; return 88; }
    VM_CLEAN=0 VM_DISCARD_RECURSIVE=0
    VM_GUEST_RECURSION_DEPTH=0 VM_EFFECTIVE_MAX_DEPTH=0 VM_MEMORY=8
    VM_RECURSE=0 VM_UNSAFE_RECURSE=0 VM_LIFECYCLE_DEFAULT_TIMEOUT=60
    vm_lifecycle_cli_run_up up "$2" dev "$3" "$4"
  ' _ "$ROOT" "$MESTER_STATE_DIR" "$BATS_TEST_TMPDIR/workspace" "$ROOT"
  [ "$status" -eq 0 ]
  [[ "$output" == *"UP <$MESTER_STATE_DIR> <dev> <$BATS_TEST_TMPDIR/workspace> <$ROOT> <0> <0> <8> <0> <0> <0> <60> <1800>"* ]]
  [[ "$output" != *RUNTIME_LOOKUP_IS_FORBIDDEN* ]]
  [ ! -e "$MESTER_STATE_DIR/vm" ]
}

@test "experimental run attaches only after lifecycle readiness and preserves argv" {
  local runtime="$BATS_TEST_TMPDIR/frozen" log="$BATS_TEST_TMPDIR/agent-argv"
  mkdir -m 700 "$runtime"
  cat > "$runtime/lab-vm" <<'SH'
#!/usr/bin/env bash
printf '<%s>\n' "$@" > "$VM_TEST_AGENT_ARGV"
SH
  chmod 700 "$runtime/lab-vm"
  # shellcheck disable=SC2016 # The child Bash receives positional arguments explicitly.
  run env VM_TEST_RUNTIME="$runtime" VM_TEST_AGENT_ARGV="$log" \
    bash -c '
      source "$1/lib/vm/lifecycle.sh"
      vm_lifecycle_up() { printf "UP:<%s>\n" "$@"; }
      vm_lifecycle_profile_runtime() { printf "%s\n" "$VM_TEST_RUNTIME"; }
      VM_CLEAN=0 VM_DISCARD_RECURSIVE=0
      VM_GUEST_RECURSION_DEPTH=0 VM_EFFECTIVE_MAX_DEPTH=0 VM_MEMORY=8
      VM_RECURSE=1 VM_UNSAFE_RECURSE=1 VM_LIFECYCLE_DEFAULT_TIMEOUT=60
      VM_PERMISSIONS=ask VM_CWD="src dir" VM_BROWSER_AUTH=off
      VM_AGENT_ARGS=(--help "literal value")
      declare -A VM_SEEN=([cwd]=1 [browser]=1)
      vm_lifecycle_cli_run_up run "$2" dev "$3" "$4"
    ' _ "$ROOT" "$MESTER_STATE_DIR" "$ROOT" "$ROOT"
  [ "$status" -eq 0 ]
  [[ "$output" == *'UP:<'* ]]
  [ "$(cat "$log")" = $'<agent>\n<dev>\n<--permissions>\n<ask>\n<--cwd>\n<src dir>\n<--no-browser-auth>\n<-->\n<--help>\n<literal value>' ]
}

@test "experimental run does not attach after a failed lifecycle start" {
  run bash -c '
    source "$1/lib/vm/lifecycle.sh"
    vm_lifecycle_up() { return 47; }
    vm_lifecycle_profile_runtime() { printf "RUNTIME_LOOKUP_IS_FORBIDDEN\n"; return 88; }
    VM_CLEAN=0 VM_DISCARD_RECURSIVE=0
    VM_GUEST_RECURSION_DEPTH=0 VM_EFFECTIVE_MAX_DEPTH=0 VM_MEMORY=8
    VM_RECURSE=0 VM_UNSAFE_RECURSE=0 VM_LIFECYCLE_DEFAULT_TIMEOUT=60
    vm_lifecycle_cli_run_up run "$2" dev "$3" "$4"
  ' _ "$ROOT" "$MESTER_STATE_DIR" "$BATS_TEST_TMPDIR/workspace" "$ROOT"
  [ "$status" -eq 47 ]
  [[ "$output" != *RUNTIME_LOOKUP_IS_FORBIDDEN* ]]
}

@test "experimental up refuses unimplemented clean and recursive discard flags" {
  local clean discard
  for flags in '1 0' '0 1'; do
    read -r clean discard <<< "$flags"
    run bash -c '
      source "$1/lib/vm/lifecycle.sh"
      vm_lifecycle_up() { printf "LIFECYCLE_MUST_NOT_RUN\n"; }
      VM_CLEAN="$5" VM_DISCARD_RECURSIVE="$6"
      vm_lifecycle_cli_run_up up "$2" dev "$3" "$4"
    ' _ "$ROOT" "$MESTER_STATE_DIR" "$BATS_TEST_TMPDIR/workspace" "$ROOT" \
      "$clean" "$discard"
    [ "$status" -eq 2 ]
    [[ "$output" != *LIFECYCLE_MUST_NOT_RUN* ]]
    [[ "$output" == *'--clean and --discard-recursive are not enabled'* ]]
  done
}

@test "CLI context is valid JSON and never creates private state" {
  run --separate-stderr bash "$ROOT/lab-vm" -d context --json --name candidate
  [ "$status" -eq 0 ]
  # shellcheck disable=SC2154 # Bats sets stderr when run uses --separate-stderr.
  [[ "$stderr" == *"[debug] state_root.private_tree=PASS"* ]]
  printf '%s' "$output" | python3 -c '
import json,sys
x=json.load(sys.stdin)
assert x["profile_name"] == "candidate"
assert x["effective_config"]["model"]["provider"] == "none"
assert x["launcher"]["permissions"] == "ask"
assert x["launcher"]["yes"] is False
assert "agent_args" not in x
'
  [ ! -e "$MESTER_STATE_DIR" ]
}

@test "CLI configuration is data and errors never print supplied secret values" {
  mkdir -m 700 "$MESTER_STATE_DIR"
  printf '{"schema_version":1,"api_key":"SECRET_CANARY_DO_NOT_PRINT"}\n' > "$MESTER_STATE_DIR/config.json"
  chmod 600 "$MESTER_STATE_DIR/config.json"
  run bash "$ROOT/lab-vm" context --json
  [ "$status" -eq 2 ]
  [[ "$output" != *SECRET_CANARY* ]]
  printf 'touch %s\n' "$BATS_TEST_TMPDIR/executed" > "$MESTER_STATE_DIR/config.json"
  run bash "$ROOT/lab-vm" context --json
  [ "$status" -eq 2 ]
  [ ! -e "$BATS_TEST_TMPDIR/executed" ]
}

@test "CLI config then environment then CLI precedence is explicit" {
  mkdir -m 700 "$MESTER_STATE_DIR"
  printf '{"schema_version":1,"opencode_version":"2.2.0"}\n' > "$MESTER_STATE_DIR/config.json"
  chmod 600 "$MESTER_STATE_DIR/config.json"
  run env VM_OPENCODE_VERSION=2.2.5 bash "$ROOT/lab-vm" context --json
  [ "$status" -eq 0 ]
  [[ "$output" == *'"opencode_version": "2.2.5"'* ]]
  run env VM_OPENCODE_VERSION=2.2.5 bash "$ROOT/lab-vm" context --json --opencode-version 2.2.6
  [ "$status" -eq 0 ]
  [[ "$output" == *'"opencode_version": "2.2.6"'* ]]
}

@test "CLI provider transitions clear incompatible inherited local model fields" {
  mkdir -m 700 "$MESTER_STATE_DIR"
  printf '{"schema_version":1,"model":{"provider":"ollama","port":11434,"id":"old-local"}}\n' > "$MESTER_STATE_DIR/config.json"
  chmod 600 "$MESTER_STATE_DIR/config.json"
  run bash "$ROOT/lab-vm" context --json --model-provider remote --model-id selected-remote
  [ "$status" -eq 0 ]
  printf '%s' "$output" | python3 -c '
import json,sys
assert json.load(sys.stdin)["effective_config"]["model"] == {"provider":"remote","id":"selected-remote"}
'
  run bash "$ROOT/lab-vm" context --json --model-provider none
  [ "$status" -eq 0 ]
  printf '%s' "$output" | python3 -c '
import json,sys
assert json.load(sys.stdin)["effective_config"]["model"] == {"provider":"none"}
'
}

@test "CLI unsafe workspace still refuses home and private state overlap" {
  run bash "$ROOT/lab-vm" up --workspace "$HOME" --unsafe-recurse
  [ "$status" -eq 2 ]
  [ ! -e "$MESTER_STATE_DIR" ]
  mkdir -m 700 "$VM_TEST_ROOT/project"
  mkdir -m 700 "$VM_TEST_ROOT/project/state"
  run env MESTER_STATE_DIR="$VM_TEST_ROOT/project/state" bash "$ROOT/lab-vm" context --json
  [ "$status" -eq 0 ] # Positive control: valid private state/config, not a mode failure.
  run env MESTER_STATE_DIR="$VM_TEST_ROOT/project/state" \
    bash "$ROOT/lab-vm" up --workspace "$VM_TEST_ROOT/project"
  [ "$status" -eq 2 ]
  [ ! -e "$VM_TEST_ROOT/project/state/vm" ]
}

@test "CLI unavailable operations refuse truthfully without state allocation" {
  run bash "$ROOT/lab-vm" doctor
  [ "$status" -eq 2 ]
  [[ "$output" == *'not yet implemented'* ]]
  [ ! -e "$MESTER_STATE_DIR" ]
}

@test "CLI status and list are read-only exact-profile queries" {
  mkdir -m 700 "$MESTER_STATE_DIR"
  python3 - "$ROOT/lib/vm/paths.py" "$MESTER_STATE_DIR" "$BATS_TEST_TMPDIR/workspace" <<'PY'
import importlib.util
import sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("fixture_paths",sys.argv[1])
paths=importlib.util.module_from_spec(spec); spec.loader.exec_module(paths)
root=Path(sys.argv[2]); workspace=Path(sys.argv[3])
data={"schema_version":1,"profile_id":"00000000-0000-4000-8000-000000000001",
 "name":"daily","workspace":str(workspace),"image_sha256":None,"image_path":None,
 "memory_gib":8,"depth":0,"max_depth":0,"descendants_possible":False,
 "opencode_version":None,"guest_payload_sha256":None,"runtime_payload_sha256":None,
 "status":"allocated"}
paths.profile_write(root,"daily",data)
PY
  before="$(find "$MESTER_STATE_DIR" -printf '%P %y %m\n' | sort)"
  run bash "$ROOT/lab-vm" status --json --name daily
  [ "$status" -eq 0 ]
  [[ "$output" == *'"status": "allocated"'* || "$output" == *'"status":"allocated"'* ]]
  run bash "$ROOT/lab-vm" list --json
  [ "$status" -eq 0 ]
  [[ "$output" == *'"name": "daily"'* || "$output" == *'"name":"daily"'* ]]
  after="$(find "$MESTER_STATE_DIR" -printf '%P %y %m\n' | sort)"
  [ "$before" = "$after" ]
}

@test "suspend of an absent exact profile refuses without allocating state" {
  run bash "$ROOT/lab-vm" suspend --name missing
  [ "$status" -eq 2 ]
  [ ! -e "$MESTER_STATE_DIR" ]
}

@test "CLI rejects malformed compatibility environment without side effects" {
  # shellcheck disable=SC2016 # Literal command-substitution payload must remain data.
  run env VM_YES='$(touch forbidden)' bash "$ROOT/lab-vm" context --json
  [ "$status" -eq 2 ]
  [[ "$output" != *'touch forbidden'* ]]
  run env VM_DEBUG=99999999999999999999999 bash "$ROOT/lab-vm" context --json
  [ "$status" -eq 2 ]
  run env LAB_VM_RECURSION_DEPTH=1 LAB_VM_MAX_DEPTH=0 bash "$ROOT/lab-vm" context --json
  [ "$status" -eq 2 ]
  [ ! -e "$MESTER_STATE_DIR" ]
}

@test "setup rejects unsupported host without fallback or installation" {
  run bash -c '
    source "$1/lib/vm/config.sh"; vm_cli_defaults
    vm_host_system() { echo Darwin; }
    vm_install_packages() { echo INSTALL_IS_FORBIDDEN; return 1; }
    vm_setup
  ' _ "$ROOT"
  [ "$status" -eq 2 ]
  [[ "$output" == *'Linux x86_64'* ]]
  [[ "$output" != *INSTALL_IS_FORBIDDEN* ]]
}

@test "setup is default no and never installs merely because yes was inherited" {
  run bash -c '
    source "$1/lib/vm/config.sh"; vm_cli_defaults; VM_YES=1
    vm_host_system() { echo Linux; }; vm_host_arch() { echo x86_64; }
    vm_host_missing() { echo virtiofsd; }
    vm_host_distro() { echo ubuntu; }
    vm_host_kvm() { return 0; }
    vm_install_packages() { echo INSTALL_IS_FORBIDDEN; }
    vm_setup
  ' _ "$ROOT"
  [ "$status" -eq 2 ]
  [[ "$output" != *INSTALL_IS_FORBIDDEN* ]]
}

@test "setup opt-in installation is default no on EOF or negative answer" {
  run bash -c '
    source "$1/lib/vm/config.sh"; vm_cli_defaults; VM_INSTALL_PREREQS=1
    vm_host_system() { echo Linux; }; vm_host_arch() { echo x86_64; }
    vm_host_missing() { echo virtiofsd; }; vm_host_kvm() { return 0; }
    vm_host_distro() { echo ubuntu; }
    vm_install_packages() { echo INSTALL_IS_FORBIDDEN; }
    vm_setup </dev/null
  ' _ "$ROOT"
  [ "$status" -eq 2 ]
  [[ "$output" != *INSTALL_IS_FORBIDDEN* ]]
}

@test "setup inherited yes cannot approve an explicitly requested install" {
  run bash -c '
    source "$1/lib/vm/config.sh"; vm_cli_defaults
    VM_INSTALL_PREREQS=1; VM_YES=1
    vm_host_system() { echo Linux; }; vm_host_arch() { echo x86_64; }
    vm_host_missing() { echo virtiofsd; }; vm_host_kvm() { return 0; }
    vm_host_distro() { echo ubuntu; }
    vm_install_packages() { echo INSTALL_IS_FORBIDDEN; }
    vm_setup </dev/null
  ' _ "$ROOT"
  [ "$status" -eq 2 ]
  [[ "$output" != *INSTALL_IS_FORBIDDEN* ]]
}

@test "setup explicit approval installs only mapped userland packages and rechecks" {
  run bash -c '
    source "$1/lib/vm/config.sh"; vm_cli_defaults
    vm_cli_parse setup --install-prereqs --yes
    vm_host_system() { echo Linux; }; vm_host_arch() { echo x86_64; }
    vm_host_kvm() { return 0; }; vm_host_distro() { echo ubuntu; }
    marker="$2"
    vm_host_missing() { [[ -e "$marker" ]] || echo virtiofsd; return 0; }
    vm_install_packages() { printf "APPROVED_INSTALL:<%s>\n" "$@"; touch "$marker"; }
    vm_setup
  ' _ "$ROOT" "$BATS_TEST_TMPDIR/installed"
  [ "$status" -eq 0 ]
  [[ "$output" == *'APPROVED_INSTALL:<apt>'* ]]
  [[ "$output" == *'APPROVED_INSTALL:<virtiofsd>'* ]]
  [[ "$output" != *libvirt* ]]
  [[ "$output" != *systemctl* ]]
}

@test "setup distro maps contain only required userland packages" {
  run bash -c '
    source "$1/lib/vm/config.sh"
    vm_package_for ubuntu qemu-system-x86_64
    vm_package_for debian qemu-img
    vm_package_for cachyos genisoimage
    vm_package_for arch ssh-keyscan
    vm_package_for arch python3
  ' _ "$ROOT"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = qemu-system-x86 ]
  [ "${lines[1]}" = qemu-utils ]
  [ "${lines[2]}" = cdrtools ]
  [ "${lines[3]}" = openssh ]
  [ "${lines[4]}" = python ]
}

@test "setup installer refuses unavailable package metadata before sudo" {
  run bash -c '
    source "$1/lib/vm/config.sh"
    apt-cache() { return 1; }
    sudo() { echo SUDO_IS_FORBIDDEN; }
    vm_install_packages apt virtiofsd
  ' _ "$ROOT"
  [ "$status" -eq 2 ]
  [[ "$output" != *SUDO_IS_FORBIDDEN* ]]
}

@test "setup virtiofsd nonstandard path is used directly without creating host links" {
  mkdir -p "$BATS_TEST_TMPDIR/tools"
  printf '#!/bin/sh\nexit 0\n' > "$BATS_TEST_TMPDIR/tools/virtiofsd"
  chmod +x "$BATS_TEST_TMPDIR/tools/virtiofsd"
  run bash -c '
    source "$1/lib/vm/config.sh"
    type() { return 1; } # Force the non-PATH discovery branch, even on a provisioned test host.
    vm_virtiofsd_candidates() { printf "%s\n" "$fixture_binary"; }
    fixture_binary="$2"
    vm_virtiofsd
  ' _ "$ROOT" "$BATS_TEST_TMPDIR/tools/virtiofsd"
  [ "$status" -eq 0 ]
  [ "$output" = "$BATS_TEST_TMPDIR/tools/virtiofsd" ]
  [ ! -L "$BATS_TEST_TMPDIR/tools/virtiofsd" ]
}
