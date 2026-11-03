#!/usr/bin/env bash
# Native guest agent and read-only model-provider adapters.
# Sourcing this module is inert; it never starts or repairs a provider service.

VM_SERVICES_SOURCE_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
VM_SERVICES_ROOT="$(cd "$(dirname "$VM_SERVICES_SOURCE_FILE")/../.." && pwd)"
VM_SERVICES_CURL_BIN="${VM_SERVICES_CURL_BIN:-curl}"
VM_SERVICES_AGENT_TIMEOUT="${VM_SERVICES_AGENT_TIMEOUT:-1800}"

vm_services_error() {
  printf 'REFUSED: %s\n' "$*" >&2
  return 2
}

vm_services_model_selection_valid() {
  local provider="${1:-}" port="${2:-}" model_id="${3:-}"
  [[ "$provider" == none || "$provider" == ollama ||
     "$provider" == llamacpp || "$provider" == remote ]] || return 1
  [[ "$model_id" != *[[:cntrl:]]* && ${#model_id} -le 512 ]] || return 1
  case "$provider" in
    ollama|llamacpp)
      [[ -z "$port" ]] && return 0
      if [[ ! "$port" =~ ^[1-9][0-9]{0,4}$ ]] || (( 10#$port > 65535 )); then
        return 1
      fi
      return 0 ;;
    none|remote) [[ -z "$port" ]] ;;
  esac
}

vm_services_is_permission_override() {
  case "${1:-}" in
    --auto|--auto-accept|--permissions|--permissions=*|--yes|--yes-always) return 0 ;;
    *) return 1 ;;
  esac
}

vm_services_is_model_override() {
  case "${1:-}" in
    --model|-m|--model=*|-m?*) return 0 ;;
    *) return 1 ;;
  esac
}

vm_services_is_cwd_override() {
  case "${1:-}" in
    --cwd|--cwd=*|--directory|--directory=*|--dir|--dir=*|-C|-C?*) return 0 ;;
    *) return 1 ;;
  esac
}

# This one builder is shared by interactive entry and the later browser-auth
# wrapper. Auth orchestration adds a bounded capability around its output; it
# does not invent another harness command or permission path.
vm_services_build_agent_argv() {
  local agent="${1:-}" permissions="${2:-}" provider="${3:-}"
  local model_id="${4:-}" argument model selected_model
  shift $(( $# >= 4 ? 4 : $# ))
  local -a agent_args=("$@")
  VM_SERVICES_AGENT_ARGV=()
  VM_SERVICES_AGENT_ENV=(
    'HTTP_PROXY=http://10.0.2.100:3128'
    'HTTPS_PROXY=http://10.0.2.100:3128'
    'http_proxy=http://10.0.2.100:3128'
    'https_proxy=http://10.0.2.100:3128'
    'NO_PROXY=localhost,127.0.0.1,10.0.2.100'
    'no_proxy=localhost,127.0.0.1,10.0.2.100'
  )
  [[ "$permissions" == ask || "$permissions" == auto ]] || {
    vm_services_error 'Agent permission mode must be ask or auto.'
    return 2
  }
  vm_services_model_selection_valid "$provider" "${VM_SERVICES_MODEL_PORT:-}" "$model_id" || {
    vm_services_error 'Selected model provider configuration is invalid.'
    return 2
  }
  for argument in "${agent_args[@]}"; do
    if vm_services_is_permission_override "$argument"; then
      vm_services_error 'Permission overrides are not accepted after --; Aider always remains in prompt mode.'
      return 2
    fi
    if vm_services_is_model_override "$argument"; then
      vm_services_error 'Model selection is fixed by the ready profile provider; model override flags are not accepted after --.'
      return 2
    fi
    if vm_services_is_cwd_override "$argument"; then
      vm_services_error 'Use launcher --cwd for workspace selection; agent cwd overrides are not accepted after --.'
      return 2
    fi
  done

  case "$agent" in
    opencode)
      VM_SERVICES_AGENT_ARGV=(opencode)
      if [[ "$permissions" == auto ]]; then VM_SERVICES_AGENT_ARGV+=(--auto); fi
      VM_SERVICES_AGENT_ARGV+=("${agent_args[@]}")
      ;;
    aider)
      [[ "$permissions" == ask ]] || {
        vm_services_error 'The Aider adapter has no supported OpenCode --auto permission mode.'
        return 2
      }
      VM_SERVICES_AGENT_ARGV=(/opt/little-mester/python-tools/bin/aider)
      case "$provider" in
        ollama)
          selected_model="${model_id:-qwen3:8b}"
          model="ollama_chat/$selected_model"
          VM_SERVICES_AGENT_ENV+=("OLLAMA_API_BASE=http://10.0.2.100:11434")
          VM_SERVICES_AGENT_ARGV+=(--model "$model")
          ;;
        llamacpp)
          selected_model="${model_id:-qwen3.6-35b-a3b-q4_k_m}"
          model="openai/$selected_model"
          VM_SERVICES_AGENT_ENV+=("OPENAI_API_BASE=http://10.0.2.100:8080/v1")
          VM_SERVICES_AGENT_ARGV+=(--model "$model")
          ;;
        remote)
          [[ -z "$model_id" ]] || VM_SERVICES_AGENT_ARGV+=(--model "$model_id")
          ;;
        none) ;;
      esac
      VM_SERVICES_AGENT_ENV+=(
        'AIDER_ANALYTICS_DISABLE=true'
        'AIDER_CHECK_UPDATE=false'
      )
      VM_SERVICES_AGENT_ARGV+=("${agent_args[@]}")
      ;;
    *)
      vm_services_error 'Supported native guest agents are OpenCode and Aider.'
      return 2
      ;;
  esac
}

vm_services_resolve_cwd() {
  local workspace="${1:-}" relative="${2:-.}"
  [[ -n "$workspace" && "$workspace" == /* ]] || {
    vm_services_error 'Selected profile workspace is unavailable.'
    return 2
  }
  VM_SERVICES_CWD="$(python3 - "$workspace" "$relative" <<'PY'
import sys
from pathlib import Path

try:
    root = Path(sys.argv[1]).resolve(strict=True)
    raw = Path(sys.argv[2])
    if not root.is_dir() or raw.is_absolute() or ".." in raw.parts:
        raise ValueError
    target = (root / raw).resolve(strict=True)
    if not target.is_dir() or not target.is_relative_to(root):
        raise ValueError
    relative = target.relative_to(root).as_posix()
    print(relative or ".")
except (OSError, RuntimeError, ValueError):
    raise SystemExit(2)
PY
  )" || {
    vm_services_error 'Agent --cwd must resolve to a directory inside the selected workspace mount.'
    return 2
  }
}

vm_services_shell_quote() {
  printf -v VM_SERVICES_QUOTED '%q' "${1-}"
}

vm_services_build_remote_command() {
  local agent="${1:-}" cwd="${2:-.}" value key command script guest_cwd
  shift 2
  local -a argv=("$@")
  [[ "$cwd" == . ]] && guest_cwd=/workspace || guest_cwd="/workspace/$cwd"
  script="export HOME=/home/mester"
  script+=$'\nexport PATH=/opt/little-mester/python-tools/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
  for value in "${VM_SERVICES_AGENT_ENV[@]}"; do
    key="${value%%=*}"
    value="${value#*=}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ && "$value" != *$'\n'* && "$value" != *$'\r'* ]] || {
      vm_services_error 'Agent environment adapter produced an invalid value.'
      return 2
    }
    vm_services_shell_quote "$value"
    script+=$'\nexport '"$key=$VM_SERVICES_QUOTED"
  done
  if [[ "$agent" == opencode ]]; then
    script+=$'\nexport OPENCODE_CLI_CONFIG_CONTENT="$(cat -- /home/mester/.config/opencode/cli.json)"'
  fi
  vm_services_shell_quote "$guest_cwd"
  script+=$'\ncd -- '"$VM_SERVICES_QUOTED"
  script+=$'\nresolved_cwd="$(pwd -P)"'
  script+=$'\ncase "$resolved_cwd" in /workspace|/workspace/*) ;; *) exit 2 ;; esac'
  script+=$'\nexec'
  for command in "${argv[@]}"; do
    vm_services_shell_quote "$command"
    script+=" $VM_SERVICES_QUOTED"
  done
  vm_services_shell_quote "$script"
  VM_SERVICES_REMOTE_COMMAND="bash -lc $VM_SERVICES_QUOTED"
}

vm_services_model_probe() {
  local provider="${1:-}" port="${2:-}" url code
  case "$provider" in
    none|remote) return 0 ;;
    ollama) url="http://127.0.0.1:$port/api/tags" ;;
    llamacpp) url="http://127.0.0.1:$port/health" ;;
    *) vm_services_error 'Unknown model provider; no endpoint was contacted.'; return 2 ;;
  esac
  if [[ ! "$port" =~ ^[1-9][0-9]{0,4}$ ]] || (( 10#$port > 65535 )); then
    vm_services_error 'A valid configured local model port is required for diagnostics.'
    return 2
  fi
  code="$("$VM_SERVICES_CURL_BIN" --disable --silent --show-error --noproxy '*' --proxy '' \
    --connect-timeout 2 --max-time 3 --output /dev/null \
    --write-out '%{http_code}' "$url" 2>/dev/null)" || return 1
  [[ "$code" =~ ^2[0-9]{2}$ ]]
}

vm_services_profile_model_forward() {
  local data="${1:-}" result
  result="$(printf '%s' "$data" | python3 -c '
import json,re,sys
try:
    data=json.load(sys.stdin)
    argv=data["runtime_identity"]["argv"]
    assert isinstance(argv,list) and all(isinstance(item,str) for item in argv)
    netdevs=[argv[index+1] for index,item in enumerate(argv[:-1]) if item == "-netdev"]
    assert len(netdevs) == 1
    segments=netdevs[0].split(",")
    assert "restrict=on" in segments
    matches=[]
    for segment in segments:
        if segment.startswith(("guestfwd=tcp:10.0.2.100:11434-", "guestfwd=tcp:10.0.2.100:8080-")):
            match=re.fullmatch(
                r"guestfwd=tcp:10\.0\.2\.100:(11434|8080)-cmd:([A-Za-z0-9_./+-]+) - TCP:127\.0\.0\.1:([1-9][0-9]{0,4})",
                segment,
            )
            assert match is not None
            guest_port,command,host_port=match.groups()
            assert command and int(host_port) <= 65535
            matches.append(("ollama" if guest_port == "11434" else "llamacpp", int(host_port)))
    assert len(matches) <= 1
    if matches:
        provider,port=matches[0]
        print(f"{provider}\t{port}")
    else:
        print("none\t")
except Exception:
    raise SystemExit(2)
')" || {
    vm_services_error 'The exact profile QEMU argv has no unambiguous restricted model-forward identity.'
    return 2
  }
  IFS=$'\t' read -r VM_SERVICES_FORWARD_PROVIDER VM_SERVICES_FORWARD_PORT <<<"$result"
}

# Resolve a ready profile's provider selection against its host-owned QEMU
# argv. A guest marker never grants a host network destination by itself.
vm_services_profile_selection() {
  local root="${1:-}" name="${2:-}" data status identity_state runtime marker parsed
  local marker_port forward_provider forward_port
  data="$(vm_lifecycle_profile_read "$root" "$name")" || return 2
  status="$(vm_lifecycle_field "$data" status)" || return 2
  [[ "$status" == ready ]] || {
    vm_services_error 'Provider diagnostics and agent attach require an already-ready profile.'
    return 2
  }
  identity_state="$(vm_lifecycle_identity_state "$data" "$root" "$name")" || identity_state=unknown
  [[ "$identity_state" == running ]] || {
    vm_services_error 'The exact profile QEMU identity is not running.'
    return 2
  }
  runtime="$(vm_lifecycle_profile_runtime "$root" "$name")" || return 2
  [[ "$runtime" == "$VM_SERVICES_ROOT" ]] || {
    vm_services_error 'Profile selection must be read by its frozen runtime.'
    return 2
  }
  marker="$(vm_lifecycle_guest_readiness "$root" "$name" "$data" 30)" || return $?
  parsed="$(printf '%s' "$marker" | python3 -c '
import json,sys
try:
    marker=json.load(sys.stdin)
    agent=marker["agent"]
    provider=marker["model_provider"]
    port=marker.get("model_port")
    model_id=marker.get("model_id","")
    assert agent in ("opencode","aider")
    assert provider in ("none","ollama","llamacpp","remote")
    assert port is None or (type(port) is int and 1 <= port <= 65535)
    assert isinstance(model_id,str) and len(model_id) <= 512
    assert not any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in model_id)
    print("\t".join((agent,provider,"" if port is None else str(port),model_id)))
except Exception:
    raise SystemExit(2)
')" || {
    vm_services_error 'Guest readiness marker has no supported native agent/provider selection.'
    return 2
  }
  IFS=$'\t' read -r VM_SERVICES_SELECTED_AGENT VM_SERVICES_SELECTED_PROVIDER marker_port VM_SERVICES_SELECTED_MODEL_ID <<<"$parsed"
  vm_services_profile_model_forward "$data" || return 2
  forward_provider="$VM_SERVICES_FORWARD_PROVIDER"
  forward_port="$VM_SERVICES_FORWARD_PORT"
  if [[ "$VM_SERVICES_SELECTED_PROVIDER" == ollama || "$VM_SERVICES_SELECTED_PROVIDER" == llamacpp ]]; then
    [[ "$forward_provider" == "$VM_SERVICES_SELECTED_PROVIDER" && "$marker_port" == "$forward_port" ]] || {
      vm_services_error 'Guest model selection does not match the exact host-owned QEMU forward; no provider endpoint was probed.'
      return 2
    }
    VM_SERVICES_SELECTED_MODEL_PORT="$forward_port"
  else
    [[ "$forward_provider" == none && -z "$marker_port" ]] || {
      vm_services_error 'Guest marker does not match the exact host-owned QEMU model-forward selection.'
      return 2
    }
    VM_SERVICES_SELECTED_MODEL_PORT=''
  fi
  VM_SERVICES_SELECTED_PROFILE_DATA="$data"
  VM_SERVICES_SELECTED_WORKSPACE="$(vm_lifecycle_field "$data" workspace)" || return 2
}

vm_services_models() {
  local action="${1:-}" requested_provider="${2:-}" root="${3:-}" name="${4:-}"
  local provider port
  case "$action" in status|diagnose) ;; *) vm_services_error 'Models supports only status or diagnose.'; return 2 ;; esac
  vm_services_profile_selection "$root" "$name" || return $?
  provider="$VM_SERVICES_SELECTED_PROVIDER"
  port="$VM_SERVICES_SELECTED_MODEL_PORT"
  if [[ -n "$requested_provider" && "$requested_provider" != "$provider" ]]; then
    vm_services_error 'Requested provider does not match the selected ready profile; no alternate endpoint was probed.'
    return 2
  fi
  case "$provider" in
    none)
      printf 'No model selected; no provider was contacted.\n'
      return 0
      ;;
    remote)
      printf 'Remote provider selected; credentials and availability are guest-native and were not probed.\n'
      return 0
      ;;
    ollama|llamacpp)
      if vm_services_model_probe "$provider" "$port"; then
        printf 'Model provider %s is reachable on the host-owned QEMU-forwarded loopback port.\n' "$provider"
        return 0
      fi
      printf 'Model provider %s is not reachable on its host-owned QEMU-forwarded loopback port; no service was changed.\n' "$provider" >&2
      return 1
      ;;
    *) vm_services_error 'Unsupported model provider; no endpoint was contacted.'; return 2 ;;
  esac
}

vm_services_agent() {
  local root="${1:-}" name="${2:-}" permissions="${3:-ask}"
  local requested_cwd="${4:-}" browser_mode="${5:-auto}"
  shift $(( $# >= 5 ? 5 : $# ))
  local data workspace info registry run_id role run_dir
  local key known_hosts port agent provider model_id model_port remote_rc
  local -a agent_args=("$@")

  case "$browser_mode" in
    off|auto) ;;
    on)
      vm_services_error 'Browser authentication forwarding is not available until TASK-10; no browser was opened.'
      return 2
      ;;
    *) vm_services_error 'Invalid browser-auth mode.'; return 2 ;;
  esac

  vm_services_profile_selection "$root" "$name" || return $?
  data="$VM_SERVICES_SELECTED_PROFILE_DATA"
  workspace="$VM_SERVICES_SELECTED_WORKSPACE"
  vm_services_resolve_cwd "$workspace" "${requested_cwd:-.}" || return 2
  agent="$VM_SERVICES_SELECTED_AGENT"
  provider="$VM_SERVICES_SELECTED_PROVIDER"
  model_id="$VM_SERVICES_SELECTED_MODEL_ID"
  model_port="$VM_SERVICES_SELECTED_MODEL_PORT"
  VM_SERVICES_MODEL_PORT="$model_port" vm_services_build_agent_argv \
    "$agent" "$permissions" "$provider" "$model_id" "${agent_args[@]}" || return $?

  if [[ "$provider" == ollama || "$provider" == llamacpp ]]; then
    if ! vm_services_model_probe "$provider" "$model_port"; then
      printf 'WARNING: selected local model provider %s is unavailable; the native agent will still open for configuration or shell/auth work.\n' \
        "$provider" >&2
    fi
  fi
  info="$(vm_lifecycle_registry_for_profile "$root" "$name" "$data")" || return 2
  read -r registry run_id role < <(printf '%s' "$info" | python3 -c '
import json,sys
try:
    item=json.load(sys.stdin)
    print(item["registry"],item["run_id"],item["role"])
except Exception:
    raise SystemExit(2)
') || return 2
  run_dir="$(dirname "$registry")"
  port="$(printf '%s' "$data" | python3 -c 'import json,sys; print(json.load(sys.stdin)["runtime_identity"]["ssh_port"])')" || return 2
  key="$root/vm/profiles/$name/control-key"
  known_hosts="$run_dir/known_hosts"
  if ! declare -F vm_ssh_interactive >/dev/null; then
    # shellcheck disable=SC1091 # Agent transport uses the frozen profile SSH module.
    source "$VM_SERVICES_ROOT/lib/vm/ssh.sh"
  fi
  vm_ssh_listener_verified "$registry" "$run_id" "$role" "$port" || {
    vm_services_error 'The selected VM SSH listener failed ownership verification.'
    return 2
  }
  vm_services_build_remote_command "$agent" "$VM_SERVICES_CWD" \
    "${VM_SERVICES_AGENT_ARGV[@]}" || return $?
  printf 'WARNING: another agent may be editing this workspace; coordinate concurrent writes.\n' >&2
  if declare -F vm_log_verbose >/dev/null; then
    vm_log_verbose "Starting native $agent in profile $name at /workspace/${VM_SERVICES_CWD}."
  fi
  if vm_ssh_interactive "$key" "$known_hosts" "$port" \
    "$VM_SERVICES_AGENT_TIMEOUT" mester@127.0.0.1 "$VM_SERVICES_REMOTE_COMMAND"; then
    return 0
  else
    remote_rc=$?
    return "$remote_rc"
  fi
}
