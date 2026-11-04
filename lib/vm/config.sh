#!/usr/bin/env bash
# One parser and explicit read-only prerequisite discovery for the VM CLI.
# Sourcing this file performs no host operations or configuration evaluation.

vm_error() { printf 'REFUSED: %s\n' "$*" >&2; return 2; }
vm_log() { printf '%s\n' "$*" >&2; }
vm_log_verbose() { if (( VM_VERBOSE )); then vm_log "[verbose] $*"; fi; }
vm_log_debug() { if (( VM_DEBUG )); then vm_log "[debug] $*"; fi; }

vm_cli_defaults() {
  VM_ENV_YES="${VM_YES:-${LAB_YES:-0}}"
  VM_ENV_VERBOSE="${VM_VERBOSE:-0}"
  VM_ENV_DEBUG="${VM_DEBUG:-0}"
  VM_COMMAND='' VM_PROFILE=default VM_WORKSPACE='' VM_MEMORY='' VM_RESIZE_MEMORY='' VM_CWD=''
  VM_YES=0 VM_PERMISSIONS=ask VM_VERBOSE=0 VM_DEBUG=0 VM_JSON=0 VM_HELP=0
  VM_RECURSE=0 VM_UNSAFE_RECURSE=0 VM_MAX_DEPTH=0 VM_BROWSER_AUTH=auto
  VM_INSTALL_PREREQS=0 VM_POWEROFF_RECURSIVE=0 VM_DISCARD_RECURSIVE=0 VM_CLEAN=0
  VM_ACTION='' VM_CALLBACK_PORT='' VM_CONFIG_JSON='' VM_MODEL_PROVIDER_QUERY=''
  VM_AGENT_ARGS=() VM_POSITIONALS=() VM_TARGETS=()
  declare -gA VM_SEEN=()
  declare -gA VM_OPTIONS=()
}

vm_cli_singleton() {
  local key="$1" value="$2"
  [[ ! -v "VM_SEEN[$key]" ]] || { vm_error 'Duplicate value option.'; return 2; }
  VM_SEEN["$key"]=1
  VM_OPTIONS["$key"]="$value"
}

vm_profile_name_valid() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; }
vm_memory_valid() { [[ "$1" =~ ^[1-9][0-9]{0,2}$ ]] && (( 10#$1 <= 128 )); }
vm_depth_valid() { [[ "$1" =~ ^(0|[1-9][0-9]?)$ ]] && (( 10#$1 <= 32 )); }

vm_proxy_upstream_args() {
  local depth max_depth
  VM_PROXY_UPSTREAM_ARGS=()
  if [[ ! -v LAB_VM_RECURSION_DEPTH && ! -v LAB_VM_MAX_DEPTH ]]; then
    return 0
  fi
  if [[ ! -v LAB_VM_RECURSION_DEPTH || ! -v LAB_VM_MAX_DEPTH ]]; then
    vm_error 'Incomplete inherited recursion identity for proxy chaining.'
    return 2
  fi
  depth="$LAB_VM_RECURSION_DEPTH"
  max_depth="$LAB_VM_MAX_DEPTH"
  if ! vm_depth_valid "$depth" || ! vm_depth_valid "$max_depth" || (( 10#$depth > 10#$max_depth )); then
    vm_error 'Invalid inherited recursion identity for proxy chaining.'
    return 2
  fi
  if (( 10#$depth > 0 )); then
    # The endpoint is the immediate parent guest's public-only proxy exposed by
    # its restricted guestfwd channel; never infer it from profile/workspace paths.
    # shellcheck disable=SC2034 # Consumed as an argv array by the runner in TASK-05.
    VM_PROXY_UPSTREAM_ARGS=(--upstream-proxy http://10.0.2.100:3128)
  fi
}

vm_cli_parse() {
  local option key value item profile_arg='' permissions_seen='' passthrough=0
  while (($#)); do
    option="$1"
    case "$option" in
      --)
        shift
        # shellcheck disable=SC2034 # Public parser result consumed by native-agent dispatch in later checkpoints.
        VM_AGENT_ARGS=("$@")
        passthrough=1
        break ;;
      -h|--help)
        # shellcheck disable=SC2034 # Public parser result consumed by lab-vm.
        VM_HELP=1 ;;
      -v|--verbose) VM_VERBOSE=1; VM_SEEN[verbose]=1 ;;
      -d|--debug) VM_DEBUG=1; VM_VERBOSE=1; VM_SEEN[debug]=1 ;;
      -y|--yes) VM_YES=1; VM_SEEN[yes]=1 ;;
      --json) VM_JSON=1 ;;
      --auto-accept)
        [[ "$permissions_seen" != ask ]] || { vm_error 'Auto and ask permission modes conflict.'; return 2; }
        VM_PERMISSIONS=auto; VM_SEEN[auto-accept]=1 ;;
      --browser-auth|--no-browser-auth)
        [[ ! -v 'VM_SEEN[browser]' ]] || { vm_error 'Browser modes are mutually exclusive.'; return 2; }
        VM_SEEN[browser]=1
        # shellcheck disable=SC2034 # Public parser result consumed by the auth adapter.
        if [[ "$option" == --browser-auth ]]; then VM_BROWSER_AUTH=on; else VM_BROWSER_AUTH=off; fi ;;
      --recurse) VM_RECURSE=1 ;;
      --unsafe-recurse)
        VM_RECURSE=1
        # shellcheck disable=SC2034 # Public result passed by lab-vm to workspace validation.
        VM_UNSAFE_RECURSE=1 ;;
      --install-prereqs) VM_INSTALL_PREREQS=1 ;;
      --poweroff-recursive) VM_POWEROFF_RECURSIVE=1 ;;
      --discard-recursive) VM_DISCARD_RECURSIVE=1 ;;
      --clean) VM_CLEAN=1 ;;
      --all) VM_SEEN[all]=1 ;;
      --name|--workspace|--memory|--max-depth|--permissions|--opencode-version|--agent|\
      --host-profile|--workload-runtime|--model-provider|--model-port|--model-id|\
      --metrics|--auth-forward|--auth-escrow-dir|--cwd)
        if (($# < 2)) || [[ -z "$2" ]]; then vm_error 'Option requires a value.'; return 2; fi
        key="${option#--}"; value="$2"
        [[ "$value" != --* ]] || { vm_error 'Option requires a value.'; return 2; }
        vm_cli_singleton "$key" "$value" || return 2
        # shellcheck disable=SC2034 # VM_CWD is part of the parser result consumed by lab-vm.
        case "$key" in
          name) VM_PROFILE="$value" ;;
          workspace) VM_WORKSPACE="$value" ;;
          cwd) VM_CWD="$value" ;;
          memory) VM_MEMORY="$value" ;;
          max-depth) VM_MAX_DEPTH="$value" ;;
          permissions)
            [[ "$value" == ask || "$value" == auto ]] || { vm_error 'Supported permission modes: ask or auto.'; return 2; }
            [[ "$value" != ask || ! -v 'VM_SEEN[auto-accept]' ]] || { vm_error 'Auto and ask permission modes conflict.'; return 2; }
            permissions_seen="$value"
            # shellcheck disable=SC2034 # Public result consumed by lab-vm and native-agent dispatch.
            VM_PERMISSIONS="$value" ;;
        esac
        shift ;;
      -*) vm_error 'Unknown launcher option (use -- before agent arguments).'; return 2 ;;
      *)
        if [[ -z "$VM_COMMAND" ]]; then
          case "$option" in
            run|up|agent|ssh|status|list|context|doctor|setup|suspend|resume|resize|down|stop|\
            rebuild|base|update|install-tools|egress-reload|models|metrics|auth-forward|recursion-clean)
              VM_COMMAND="$option" ;;
            *) VM_COMMAND=run; VM_POSITIONALS+=("$option") ;;
          esac
        else
          VM_POSITIONALS+=("$option")
        fi ;;
    esac
    shift
  done
  VM_COMMAND="${VM_COMMAND:-run}"
  case "$VM_COMMAND" in
    run)
      ((${#VM_POSITIONALS[@]} <= 1)) || { vm_error 'run accepts one workspace.'; return 2; }
      if ((${#VM_POSITIONALS[@]})); then
        [[ ! -v 'VM_SEEN[workspace]' ]] || { vm_error 'Workspace specified twice.'; return 2; }
        VM_WORKSPACE="${VM_POSITIONALS[0]}"
      fi
      VM_WORKSPACE="${VM_WORKSPACE:-$PWD}" ;;
    setup|context|list|base)
      ((${#VM_POSITIONALS[@]} == 0)) || { vm_error 'Unexpected positional argument.'; return 2; } ;;
    resize)
      ((${#VM_POSITIONALS[@]} == 2)) || { vm_error 'Usage: resize PROFILE GiB'; return 2; }
      profile_arg="${VM_POSITIONALS[0]}"; VM_RESIZE_MEMORY="${VM_POSITIONALS[1]}"
      vm_memory_valid "$VM_RESIZE_MEMORY" || { vm_error 'Memory must be whole GiB from 1 to 128.'; return 2; } ;;
    models)
      ((${#VM_POSITIONALS[@]} >= 1 && ${#VM_POSITIONALS[@]} <= 2)) || { vm_error 'Usage: models status|diagnose [provider]'; return 2; }
      VM_ACTION="${VM_POSITIONALS[0]}"
      [[ "$VM_ACTION" == status || "$VM_ACTION" == diagnose ]] || { vm_error 'Model service mutation is not a VM operation.'; return 2; }
      if ((${#VM_POSITIONALS[@]} == 2)); then
        VM_MODEL_PROVIDER_QUERY="${VM_POSITIONALS[1]}"
        case "$VM_MODEL_PROVIDER_QUERY" in none|ollama|llamacpp|remote) ;;
          *) vm_error 'Unknown model provider selector.'; return 2 ;;
        esac
      fi ;;
    metrics)
      ((${#VM_POSITIONALS[@]} == 1)) || { vm_error 'Usage: metrics up|status|down'; return 2; }
      VM_ACTION="${VM_POSITIONALS[0]}"
      [[ "$VM_ACTION" == up || "$VM_ACTION" == status || "$VM_ACTION" == down ]] || { vm_error 'Unknown metrics action.'; return 2; } ;;
    auth-forward)
      ((${#VM_POSITIONALS[@]} >= 1 && ${#VM_POSITIONALS[@]} <= 2)) || { vm_error 'Usage: auth-forward [PROFILE] PORT'; return 2; }
      if ((${#VM_POSITIONALS[@]} == 2)); then profile_arg="${VM_POSITIONALS[0]}"; fi
      VM_CALLBACK_PORT="${VM_POSITIONALS[-1]}"
      if [[ ! "$VM_CALLBACK_PORT" =~ ^[1-9][0-9]{3,4}$ ]] || (( 10#$VM_CALLBACK_PORT > 65535 || 10#$VM_CALLBACK_PORT < 1024 )); then
        vm_error 'Callback port must be from 1024 to 65535.'; return 2
      fi ;;
    update)
      VM_TARGETS=("${VM_POSITIONALS[@]}")
      for item in "${VM_TARGETS[@]}"; do
        case "$item" in opencode|config|tools|docker|system) ;; *) vm_error 'Unknown update target.'; return 2 ;; esac
      done
      [[ ! -v 'VM_SEEN[all]' || ${#VM_TARGETS[@]} == 0 ]] || { vm_error '--all conflicts with selected update targets.'; return 2; } ;;
    *)
      ((${#VM_POSITIONALS[@]} <= 1)) || { vm_error 'Command accepts at most one profile.'; return 2; }
      if ((${#VM_POSITIONALS[@]})); then profile_arg="${VM_POSITIONALS[0]}"; fi ;;
  esac
  if [[ -n "$profile_arg" ]]; then
    [[ ! -v 'VM_SEEN[name]' ]] || { vm_error 'Profile specified twice.'; return 2; }
    VM_PROFILE="$profile_arg"
  fi
  vm_profile_name_valid "$VM_PROFILE" || { vm_error 'Invalid profile name.'; return 2; }
  vm_depth_valid "$VM_MAX_DEPTH" || { vm_error 'Maximum depth must be from 0 to 32.'; return 2; }
  if [[ -n "$VM_MEMORY" ]]; then
    vm_memory_valid "$VM_MEMORY" || { vm_error 'Memory must be whole GiB from 1 to 128.'; return 2; }
    [[ "$VM_COMMAND" == run || "$VM_COMMAND" == up ]] || { vm_error '--memory configures a new run/up profile only.'; return 2; }
  fi
  if [[ -v 'VM_SEEN[max-depth]' ]]; then
    (( VM_RECURSE )) || { vm_error '--max-depth requires explicit recursion.'; return 2; }
  fi
  if (( VM_RECURSE )); then
    [[ "$VM_COMMAND" == run || "$VM_COMMAND" == up ]] || { vm_error 'Recursion options apply to run/up only.'; return 2; }
  fi
  (( !VM_INSTALL_PREREQS )) || [[ "$VM_COMMAND" == setup ]] || { vm_error '--install-prereqs applies only to setup.'; return 2; }
  (( !VM_POWEROFF_RECURSIVE )) || [[ "$VM_COMMAND" == suspend ]] || { vm_error '--poweroff-recursive applies only to suspend.'; return 2; }
  (( !VM_CLEAN )) || [[ "$VM_COMMAND" == up ]] || { vm_error '--clean applies only to up.'; return 2; }
  if (( VM_DISCARD_RECURSIVE )); then
    case "$VM_COMMAND" in down|stop|rebuild|up) ;; *) vm_error 'Discard acknowledgement is invalid for this command.'; return 2 ;; esac
  fi
  if (( passthrough )) && [[ "$VM_COMMAND" != run && "$VM_COMMAND" != agent && "$VM_COMMAND" != ssh ]]; then
    vm_error 'Only run, agent or ssh accepts arguments after --.'; return 2
  fi
  if [[ -v 'VM_SEEN[permissions]' || -v 'VM_SEEN[auto-accept]' || -v 'VM_SEEN[browser]' || -v 'VM_SEEN[cwd]' ]]; then
    [[ "$VM_COMMAND" == run || "$VM_COMMAND" == agent ]] || { vm_error 'Agent options apply only to run/agent.'; return 2; }
  fi
  if [[ -v 'VM_SEEN[workspace]' ]]; then
    [[ "$VM_COMMAND" == up || "$VM_COMMAND" == run ]] || { vm_error '--workspace applies only to run/up.'; return 2; }
  fi
  if [[ "$VM_COMMAND" == agent || "$VM_COMMAND" == models ]]; then
    if [[ -v 'VM_SEEN[agent]' || -v 'VM_SEEN[workload-runtime]' ||
      -v 'VM_SEEN[opencode-version]' || -v 'VM_SEEN[model-provider]' ||
      -v 'VM_SEEN[model-port]' || -v 'VM_SEEN[model-id]' ]]; then
      vm_error 'Harness/provider selection is fixed by the ready profile for agent/models queries.'
      return 2
    fi
  fi
  if (( VM_JSON )); then
    case "$VM_COMMAND" in context|status|list|doctor) ;; *) vm_error '--json applies only to read-only queries.'; return 2 ;; esac
  fi
  [[ ! -v 'VM_SEEN[all]' || "$VM_COMMAND" == update ]] || { vm_error '--all applies only to update.'; return 2; }
  return 0
}

vm_config_load() {
  local config key env_value
  [[ "$VM_ENV_YES" =~ ^[01]$ && "$VM_ENV_VERBOSE" =~ ^[012]$ && "$VM_ENV_DEBUG" =~ ^[012]$ ]] || {
    vm_error 'Invalid compatibility logging/confirmation environment.'; return 2;
  }
  [[ -v 'VM_SEEN[yes]' ]] || VM_YES="$VM_ENV_YES"
  [[ -v 'VM_SEEN[verbose]' || -v 'VM_SEEN[debug]' ]] || VM_VERBOSE="$VM_ENV_VERBOSE"
  [[ -v 'VM_SEEN[debug]' ]] || VM_DEBUG="$VM_ENV_DEBUG"
  if (( VM_DEBUG )); then VM_VERBOSE=1; fi
  for key in LAB_VM_RECURSION_DEPTH LAB_VM_MAX_DEPTH; do
    if [[ -v "$key" ]]; then
      env_value="${!key}"
      vm_depth_valid "$env_value" || { vm_error 'Invalid inherited recursion identity.'; return 2; }
    fi
  done
  if [[ -v LAB_VM_MAX_DEPTH && ! -v LAB_VM_RECURSION_DEPTH ]]; then
    vm_error 'Inherited maximum requires inherited depth.'; return 2
  fi
  if [[ -v LAB_VM_RECURSION_DEPTH ]]; then
    if [[ ! -v LAB_VM_MAX_DEPTH ]] || (( LAB_VM_RECURSION_DEPTH > LAB_VM_MAX_DEPTH )); then
      vm_error 'Incomplete or inconsistent inherited recursion identity.'; return 2
    fi
    if [[ -v 'VM_SEEN[max-depth]' ]]; then
      (( VM_MAX_DEPTH <= LAB_VM_MAX_DEPTH && VM_MAX_DEPTH >= LAB_VM_RECURSION_DEPTH )) || {
        vm_error 'Cannot increase inherited maximum or lower it below current depth.'; return 2;
      }
    else
      VM_MAX_DEPTH="$LAB_VM_MAX_DEPTH"
    fi
  fi
  if [[ -v LAB_VM_RECURSION_DEPTH ]] && (( 10#$LAB_VM_RECURSION_DEPTH > 0 )) &&
    [[ -v 'VM_SEEN[metrics]' && "${VM_OPTIONS[metrics]}" == on ]]; then
    vm_error 'Nested profiles cannot forward ancestor host metrics; a child-local metrics endpoint is not implemented.'
    return 2
  fi
  config="$(python3 "$VM_PATHS_HELPER" config --state-root "$VM_STATE_ROOT")" || return 2
  # JSON parsing only. Keys come from a fixed CLI whitelist, never evaluated shell code.
  config="$(printf '%s' "$config" | python3 -c '
import json,os,sys
c=json.load(sys.stdin)
args=sys.argv[1:]
if os.environ.get("LAB_VM_RECURSION_DEPTH") not in (None,"0"):
    c["metrics"]="off"
if os.environ.get("VM_OPENCODE_VERSION"):
    c["opencode_version"]=os.environ["VM_OPENCODE_VERSION"]
for key,value in zip(args[::2],args[1::2]):
    if key.startswith("model-"):
        field=key[6:]
        if field == "provider" and value != c["model"]["provider"]:
            c["model"]={"provider":value}
        if field == "port":
            if not value.isascii() or not value.isdecimal() or len(value)>5:
                raise SystemExit(2)
            value=int(value)
        c["model"][field]=value
    elif key == "auth-forward":
        c["auth_forward"]=value if value in ("off","auto") else value.split(",")
    else:
        c[key.replace("-","_")]=value
print(json.dumps(c))
' "${VM_CONFIG_OVERRIDES[@]}")" || { vm_error 'Invalid configuration override.'; return 2; }
  # shellcheck disable=SC2034 # Validated JSON is consumed by lab-vm, never evaluated as shell.
  VM_CONFIG_JSON="$(printf '%s' "$config" | python3 "$VM_PATHS_HELPER" validate-config --state-root "$VM_STATE_ROOT")" || return 2
}

vm_config_overrides() {
  local key
  VM_CONFIG_OVERRIDES=()
  for key in host-profile workload-runtime opencode-version agent model-provider model-port model-id metrics auth-forward auth-escrow-dir; do
    if [[ -v "VM_SEEN[$key]" ]]; then VM_CONFIG_OVERRIDES+=("$key" "${VM_OPTIONS[$key]}"); fi
  done
}

vm_host_system() { uname -s; }
vm_host_arch() { uname -m; }
vm_host_kvm() { [[ -c /dev/kvm && -r /dev/kvm && -w /dev/kvm ]]; }
vm_host_distro() {
  python3 -c '
from pathlib import Path
data={}
try:
    for line in Path("/etc/os-release").read_text().splitlines():
        key,sep,value=line.partition("=")
        if sep: data[key]=value.strip("\"\x27")
except OSError: pass
print(data.get("ID","unknown"))
'
}

vm_virtiofsd_candidates() {
  printf '%s\n' /usr/lib/qemu/virtiofsd /usr/lib/qemu/qemu-virtiofsd /usr/libexec/virtiofsd
}
vm_virtiofsd() {
  local candidate
  if candidate="$(type -P virtiofsd)"; then printf '%s\n' "$candidate"; return 0; fi
  while IFS= read -r candidate; do
    if [[ -f "$candidate" && -x "$candidate" ]]; then printf '%s\n' "$candidate"; return 0; fi
  done < <(vm_virtiofsd_candidates)
  return 1
}

vm_host_missing() {
  local tool
  for tool in qemu-system-x86_64 qemu-img ssh ssh-keyscan ssh-keygen curl python3 socat flock; do
    command -v "$tool" >/dev/null 2>&1 || printf '%s\n' "$tool"
  done
  vm_virtiofsd >/dev/null || printf 'virtiofsd\n'
  if ! command -v genisoimage >/dev/null 2>&1 && ! command -v mkisofs >/dev/null 2>&1; then
    printf 'genisoimage\n'
  fi
  return 0
}

vm_package_for() {
  local distro="$1" tool="$2"
  case "$distro" in
    ubuntu|debian)
      case "$tool" in
        qemu-system-x86_64) printf 'qemu-system-x86\n' ;;
        qemu-img) printf 'qemu-utils\n' ;;
        virtiofsd|genisoimage|curl|python3|socat) printf '%s\n' "$tool" ;;
        ssh|ssh-keyscan|ssh-keygen) printf 'openssh-client\n' ;;
        flock) printf 'util-linux\n' ;;
        *) return 2 ;;
      esac ;;
    arch|cachyos)
      case "$tool" in
        qemu-system-x86_64) printf 'qemu-system-x86\n' ;;
        qemu-img|virtiofsd|curl|socat) printf '%s\n' "$tool" ;;
        genisoimage) printf 'cdrtools\n' ;;
        python3) printf 'python\n' ;;
        ssh|ssh-keyscan|ssh-keygen) printf 'openssh\n' ;;
        flock) printf 'util-linux\n' ;;
        *) return 2 ;;
      esac ;;
    *) return 2 ;;
  esac
}

vm_install_packages() {
  local manager="$1" package; shift
  # Package names vary across releases. Verify the installed repository metadata
  # before any privileged action; never add repositories or refresh them silently.
  for package in "$@"; do
    case "$manager" in
      apt)
        if ! command -v apt-cache >/dev/null 2>&1 || ! apt-cache show -- "$package" >/dev/null 2>&1; then
          vm_error 'A required userland package is unavailable in local repository metadata; review setup manually.'; return 2
        fi ;;
      pacman)
        if ! command -v pacman >/dev/null 2>&1 || ! pacman -Si -- "$package" >/dev/null 2>&1; then
          vm_error 'A required userland package is unavailable in local repository metadata; review setup manually.'; return 2
        fi ;;
      *) vm_error 'Unsupported userland package manager.'; return 2 ;;
    esac
  done
  command -v sudo >/dev/null || { vm_error 'Install the listed packages manually; sudo is unavailable.'; return 2; }
  # Only called by explicit setup --install-prereqs AFTER confirmation.
  case "$manager" in
    apt) sudo apt-get install --no-install-recommends -y -- "$@" ;;
    pacman) sudo pacman -S --needed --noconfirm -- "$@" ;;
    *) vm_error 'Unsupported userland package manager.'; return 2 ;;
  esac
}

vm_setup() {
  local missing distro manager tool package answer remaining install_status
  local -a packages=()
  local -A seen_packages=()
  if [[ "$(vm_host_system)" != Linux || "$(vm_host_arch)" != x86_64 ]]; then
    vm_error 'Only the experimental Linux x86_64 host profile is supported; no TCG or weaker fallback.'; return 2
  fi
  vm_host_kvm || { vm_error 'Usable /dev/kvm is required. No host group, kernel or network settings were changed.'; return 2; }
  missing="$(vm_host_missing)"
  if [[ -z "$missing" ]]; then vm_log 'VM userland prerequisites found. Real VM acceptance remains separate.'; return 0; fi
  vm_log 'Missing VM userland tools:'
  printf '%s\n' "$missing" >&2
  distro="$(vm_host_distro)"
  case "$distro" in
    ubuntu|debian) manager=apt ;;
    arch|cachyos) manager=pacman ;;
    *) vm_error 'No tested package map for this distro. Install missing userland tools manually.'; return 2 ;;
  esac
  while IFS= read -r tool; do
    package="$(vm_package_for "$distro" "$tool")" || { vm_error 'No package mapping for a required tool.'; return 2; }
    if [[ ! -v "seen_packages[$package]" ]]; then packages+=("$package"); seen_packages["$package"]=1; fi
  done <<< "$missing"
  vm_log "Userland packages ($manager): ${packages[*]}"
  if (( !VM_INSTALL_PREREQS )); then
    vm_error 'Read-only setup: install prerequisites manually or explicitly request setup --install-prereqs.'; return 2
  fi
  # Ambient LAB_YES/VM_YES is not an explicit grant to install host packages.
  if (( !VM_YES )) || [[ ! -v 'VM_SEEN[yes]' ]]; then
    printf 'Install only these userland packages? [y/N] ' >&2
    IFS= read -r answer || answer=''
    [[ "$answer" == y || "$answer" == Y || "$answer" == yes ]] || { vm_error 'Installation declined; nothing installed.'; return 2; }
  fi
  if vm_install_packages "$manager" "${packages[@]}"; then
    :
  else
    install_status=$?
    (( install_status != 2 )) || return 2
    vm_log "Host userland installation failed (cause_exit_code=$install_status); no VM was started."
    return 1
  fi
  remaining="$(vm_host_missing)"
  [[ -z "$remaining" ]] || { vm_error 'Required tools remain unavailable after setup; inspect installed packages.'; return 2; }
  vm_log 'VM userland prerequisites found. No VM was started.'
}
