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
  local provider env_name auth_path auth_code auth_code_quoted auth_path_quoted
  shift 2
  local -a argv=("$@")
  local -a auth_key_providers=()
  if declare -p VM_SERVICES_AUTH_KEY_PROVIDERS >/dev/null 2>&1; then
    auth_key_providers=("${VM_SERVICES_AUTH_KEY_PROVIDERS[@]}")
  fi
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
  if [[ -n "${VM_SERVICES_BROWSER_DIR:-}" ]]; then
    [[ "$agent" == opencode && "$VM_SERVICES_BROWSER_DIR" =~ ^/tmp/lm-vm-auth\.[A-Za-z0-9]{6}$ &&
       "${VM_SERVICES_BROWSER_PORT:-}" =~ ^[1-9][0-9]{3,4}$ ]] || {
      vm_services_error 'Temporary browser handoff metadata is invalid.'
      return 2
    }
    vm_services_shell_quote "$VM_SERVICES_BROWSER_DIR"
    script+=$'\nexport PATH='"$VM_SERVICES_QUOTED"':$PATH'
    vm_services_shell_quote "$VM_SERVICES_BROWSER_DIR/token"
    script+=$'\nexport VM_BROWSER_BRIDGE_TOKEN_FILE='"$VM_SERVICES_QUOTED"
    script+=$'\nexport VM_BROWSER_BRIDGE_PORT='"$VM_SERVICES_BROWSER_PORT"
  fi
  for provider in "${auth_key_providers[@]}"; do
    case "$provider" in
      anthropic) env_name=ANTHROPIC_API_KEY ;;
      kimi) env_name=MOONSHOT_API_KEY ;;
      *) vm_services_error 'Unsupported runtime auth-key adapter; no secret was forwarded.'; return 2 ;;
    esac
    auth_path="/home/mester/.config/opencode/little-mester-auth/$provider.json"
    auth_code='import json,os,stat,sys
from pathlib import Path
path=Path(sys.argv[2])
try:
 info=path.lstat()
 assert stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
 assert info.st_uid==os.getuid() and info.st_nlink==1 and stat.S_IMODE(info.st_mode)==0o600
 assert info.st_size<=20000 and path.resolve(strict=True)==path
 directory=path.parent
 dinfo=directory.lstat()
 assert stat.S_ISDIR(dinfo.st_mode) and not stat.S_ISLNK(dinfo.st_mode)
 assert dinfo.st_uid==os.getuid() and stat.S_IMODE(dinfo.st_mode)==0o700 and directory.resolve(strict=True)==directory
 value=json.loads(path.read_text(encoding="utf-8"))
 assert set(value)=={"schema_version","provider","api_key"}
 assert type(value["schema_version"]) is int and value["schema_version"]==1
 assert value["provider"]==sys.argv[1] and isinstance(value["api_key"],str) and value["api_key"]
 assert not any(ord(c)<32 or 127<=ord(c)<=159 for c in value["api_key"])
 print(value["api_key"],end="")
except Exception:
 raise SystemExit(2)'
    printf -v auth_code_quoted '%q' "$auth_code"
    vm_services_shell_quote "$auth_path"
    auth_path_quoted="$VM_SERVICES_QUOTED"
    script+=$'\n'"vm_auth_runtime_key=\$(python3 -c $auth_code_quoted $provider $auth_path_quoted) || exit 2"
    script+=$'\n[[ -n "$vm_auth_runtime_key" ]] || exit 2'
    script+=$'\n'"export $env_name=\"\$vm_auth_runtime_key\""
    script+=$'\nunset vm_auth_runtime_key'
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

vm_services_profile_metrics_forward() {
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
        if segment.startswith("guestfwd=tcp:10.0.2.100:19532-"):
            match=re.fullmatch(
                r"guestfwd=tcp:10\.0\.2\.100:19532-cmd:([A-Za-z0-9_./+-]+) - TCP:127\.0\.0\.1:([1-9][0-9]{0,4})",
                segment,
            )
            assert match is not None
            command,host_port=match.groups()
            assert command and int(host_port) <= 65535
            matches.append(int(host_port))
    assert len(matches) <= 1
    print(matches[0] if matches else "")
except Exception:
    raise SystemExit(2)
')" || {
    vm_services_error 'The exact profile QEMU argv has no unambiguous restricted metrics-forward identity.'
    return 2
  }
  VM_SERVICES_METRICS_FORWARD_PORT="$result"
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

vm_services_agent_core() {
  local root="${1:-}" name="${2:-}" permissions="${3:-ask}"
  local requested_cwd="${4:-}" browser_mode="${5:-off}"
  shift $(( $# >= 5 ? 5 : $# ))
  local data workspace info registry run_id role run_dir
  local key known_hosts port agent provider model_id model_port remote_rc metrics_port
  local auth_config auth_selector auth_escrow auth_version auth_fields selected_auth
  local -a agent_args=("$@")

  [[ "$browser_mode" == off ]] || {
    vm_services_error 'Internal browser-session dispatch is invalid.'
    return 2
  }

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

  VM_SERVICES_AUTH_KEY_PROVIDERS=()
  auth_config="${VM_CONFIG_JSON:-}"
  if [[ -n "$auth_config" ]]; then
    auth_fields="$(printf '%s' "$auth_config" | python3 -c '
import json,sys
try:
 config=json.load(sys.stdin)
 selector=config["auth_forward"]
 selector=",".join(selector) if isinstance(selector,list) else selector
 print("\t".join((selector,config["auth_escrow_dir"])))
except Exception: raise SystemExit(2)
')" || {
      vm_services_error 'Validated auth-forward configuration could not be read.'
      return 2
    }
    IFS=$'\t' read -r auth_selector auth_escrow <<<"$auth_fields"
  else
    auth_selector=off
    auth_escrow="$root/secrets"
  fi
  if [[ "$auth_selector" != off ]]; then
    [[ "$agent" == opencode ]] || {
      vm_services_error 'Provider credential adapters are supported only by the native OpenCode harness.'
      return 2
    }
    auth_version="$(vm_lifecycle_field "$data" opencode_version)" || return 2
    if ! declare -F vm_auth_import_if_needed >/dev/null; then
      # shellcheck disable=SC1091 # Auth policy comes from the selected frozen runtime.
      source "$VM_SERVICES_ROOT/lib/vm/auth.sh"
    fi
    vm_auth_import_if_needed "$root" "$name" "$data" "$auth_selector" \
      "$auth_version" "$auth_escrow" "$workspace" "$VM_SERVICES_ROOT" || return $?
    for selected_auth in "${VM_AUTH_ACTIVE_PROVIDERS[@]}"; do
      case "$selected_auth" in
        anthropic|kimi) VM_SERVICES_AUTH_KEY_PROVIDERS+=("$selected_auth") ;;
      esac
    done
  fi

  # VM plugins never guess Docker DNS or inherit an ancestor endpoint. Only use
  # the one guestfwd proven by the host-owned QEMU identity and service registry.
  VM_SERVICES_AGENT_ENV+=("METRICS_URL=disabled")
  if vm_services_profile_metrics_forward "$data"; then
    metrics_port="$VM_SERVICES_METRICS_FORWARD_PORT"
    if [[ -n "$metrics_port" && -f "$VM_SERVICES_ROOT/lib/vm/metrics_service.py" &&
          ! -L "$VM_SERVICES_ROOT/lib/vm/metrics_service.py" ]] &&
      python3 "$VM_SERVICES_ROOT/lib/vm/metrics_service.py" verify \
        --state-root "$root" --port "$metrics_port" --quiet >/dev/null 2>&1; then
      VM_SERVICES_AGENT_ENV[-1]='METRICS_URL=http://10.0.2.100:19532'
    elif [[ -n "$metrics_port" ]]; then
      printf 'WARNING: the selected VM metrics forward is unavailable or unverified; the native agent will start without host metrics.\n' >&2
    fi
  fi

  if [[ -z "${VM_SERVICES_BROWSER_DIR:-}" &&
        ( "$provider" == ollama || "$provider" == llamacpp ) ]]; then
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

vm_services_auth_bridge_health() {
  local port="${1:-}"
  [[ "$port" =~ ^[1-9][0-9]{3,4}$ ]] && (( 10#$port <= 65535 )) || return 1
  python3 - "$port" <<'PY'
import http.client
import json
import sys
try:
    connection = http.client.HTTPConnection("127.0.0.1", int(sys.argv[1]), timeout=1)
    connection.request("GET", "/health")
    response = connection.getresponse()
    body = response.read(128)
    connection.close()
    if response.status != 200 or json.loads(body) != {"ok": True}:
        raise ValueError
except Exception:
    raise SystemExit(1)
PY
}

vm_services_auth_bridge_matches() {
  local pid="${1:-}" expected_script="${2:-}"
  [[ "$pid" =~ ^[1-9][0-9]*$ && -r "/proc/$pid/cmdline" ]] || return 1
  python3 - "$pid" "$expected_script" <<'PY'
import os
import sys
try:
    argv = open(f"/proc/{sys.argv[1]}/cmdline", "rb").read().decode().rstrip("\0").split("\0")
    if len(argv) < 2 or os.path.basename(argv[0]) not in {"python3", "python"}:
        raise ValueError
    if os.path.realpath(argv[1]) != os.path.realpath(sys.argv[2]):
        raise ValueError
except Exception:
    raise SystemExit(1)
PY
}

vm_services_auth_master_matches() {
  local pid="${1:-}" control_path="${2:-}" reverse_forward="${3:-}"
  [[ "$pid" =~ ^[1-9][0-9]*$ && -r "/proc/$pid/cmdline" && -n "$reverse_forward" ]] || return 1
  python3 - "$pid" "$control_path" "$reverse_forward" <<'PY'
import os
import sys
try:
    argv = open(f"/proc/{sys.argv[1]}/cmdline", "rb").read().decode().rstrip("\0").split("\0")
    if not argv or os.path.basename(argv[0]) != "ssh":
        raise ValueError
    if "-M" not in argv or "-S" not in argv or argv[argv.index("-S") + 1] != sys.argv[2]:
        raise ValueError
    if "-R" not in argv or argv[argv.index("-R") + 1] != sys.argv[3]:
        raise ValueError
    if "mester@127.0.0.1" not in argv:
        raise ValueError
except Exception:
    raise SystemExit(1)
PY
}

vm_services_auth_acceptance_marker() {
  local marker="${VM_AUTH_ACCEPTANCE_MARKER:-}"
  [[ -n "$marker" ]] || return 0
  python3 - "$marker" <<'PY'
import os,re,stat,sys
from pathlib import Path
try:
    path=Path(sys.argv[1])
    parent=path.parent
    info=parent.lstat()
    if (not path.is_absolute() or path.resolve(strict=False)!=path
        or parent.is_symlink() or not stat.S_ISDIR(info.st_mode)
        or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700
        or re.fullmatch(r"auth-callback-[a-f0-9]{32}\.json",path.name) is None
        or path.exists() or path.is_symlink()):
        raise ValueError
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_CLOEXEC|os.O_NOFOLLOW,0o600)
    try:
        payload=b'{"callback_cleanup":"PASS"}\n'
        if os.write(fd,payload)!=len(payload):
            raise OSError
        os.fsync(fd)
    finally:
        os.close(fd)
    dfd=os.open(parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try: os.fsync(dfd)
    finally: os.close(dfd)
except Exception:
    raise SystemExit(1)
PY
}

vm_services_auth_cleanup() {
  local root="$1" name="$2" key="$3" known_hosts="$4" ssh_port="$5"
  local bridge_script="$6" bridge_pid="$7" host_dir="$8" guest_dir="$9"
  local control_path="${10}" master_pid="${11}" reverse_forward="${12}"
  local host="${15}" i cleanup_ok=1

  if [[ "$guest_dir" =~ ^/tmp/lm-vm-auth\.[A-Za-z0-9]{6}$ ]] &&
     [[ -S "$control_path" ]] && vm_services_auth_master_matches "$master_pid" "$control_path" "$reverse_forward"; then
    if ! VM_SSH_CONTROL_PATH="$control_path" vm_ssh_noninteractive \
      "$key" "$known_hosts" "$ssh_port" 10 "$host" "rm -rf -- '$guest_dir' && test ! -e '$guest_dir'" \
      </dev/null >/dev/null 2>&1; then
      cleanup_ok=0
    fi
  elif [[ -n "$guest_dir" ]]; then
    cleanup_ok=0
  fi
  if [[ -n "$bridge_pid" ]] && vm_services_auth_bridge_matches "$bridge_pid" "$bridge_script"; then
    kill -TERM "$bridge_pid" 2>/dev/null || true
    for ((i=0; i<30; i++)); do
      vm_services_auth_bridge_matches "$bridge_pid" "$bridge_script" || break
      sleep 0.05
    done
    vm_services_auth_bridge_matches "$bridge_pid" "$bridge_script" && kill -KILL "$bridge_pid" 2>/dev/null || true
    wait "$bridge_pid" 2>/dev/null || true
    vm_services_auth_bridge_matches "$bridge_pid" "$bridge_script" && cleanup_ok=0
  fi
  if [[ -n "$master_pid" ]] && vm_services_auth_master_matches "$master_pid" "$control_path" "$reverse_forward"; then
    ssh -F /dev/null -S "$control_path" -O exit -p "$ssh_port" "$host" \
      </dev/null >/dev/null 2>&1 || true
    for ((i=0; i<30; i++)); do
      vm_services_auth_master_matches "$master_pid" "$control_path" "$reverse_forward" || break
      sleep 0.05
    done
    vm_services_auth_master_matches "$master_pid" "$control_path" "$reverse_forward" && kill -TERM "$master_pid" 2>/dev/null || true
    wait "$master_pid" 2>/dev/null || true
    vm_services_auth_master_matches "$master_pid" "$control_path" "$reverse_forward" && cleanup_ok=0
  fi
  if [[ "$host_dir" =~ ^/tmp/lm-vm-auth\.[A-Za-z0-9]{6}$ && -d "$host_dir" && ! -L "$host_dir" && -O "$host_dir" ]]; then
    rm -rf -- "$host_dir"
    [[ ! -e "$host_dir" && ! -L "$host_dir" ]] || cleanup_ok=0
  elif [[ -n "$host_dir" ]]; then
    cleanup_ok=0
  fi
  if (( cleanup_ok )) && [[ -n "$guest_dir" && -n "$bridge_pid" && -n "$master_pid" ]]; then
    vm_services_auth_acceptance_marker || true
  fi
  unset VM_SSH_CONTROL_PATH VM_SERVICES_BROWSER_DIR VM_SERVICES_BROWSER_PORT
}

vm_services_agent_browser_session() (
  local root="${1:-}" name="${2:-}" permissions="${3:-ask}"
  local requested_cwd="${4:-.}" bridge_script bridge_pid='' master_pid=''
  local host_dir='' guest_dir='' control_path='' host_port='' guest_port=''
  local key known_hosts ssh_port registry_info registry run_id role data reverse_forward auth_version
  local host='mester@127.0.0.1' temp_port='' token_file bridge_log ssh_log guest_port_command
  local i
  shift $(( $# >= 4 ? 4 : $# ))
  local -a agent_args=("$@")

  unset VM_SSH_CONTROL_PATH VM_SERVICES_BROWSER_DIR VM_SERVICES_BROWSER_PORT
  if ! declare -F vm_ssh_noninteractive >/dev/null; then
    # shellcheck disable=SC1091 # SSH transport comes from the selected frozen runtime.
    source "$VM_SERVICES_ROOT/lib/vm/ssh.sh"
  fi
  vm_services_profile_selection "$root" "$name" || return $?
  [[ "$VM_SERVICES_SELECTED_AGENT" == opencode ]] || {
    vm_services_error 'Browser auth handoff is available only for the native OpenCode harness.'
    return 2
  }
  data="$VM_SERVICES_SELECTED_PROFILE_DATA"
  if ! declare -F vm_auth_version_supported >/dev/null; then
    # shellcheck disable=SC1091 # Browser compatibility comes from the selected runtime.
    source "$VM_SERVICES_ROOT/lib/vm/auth.sh"
  fi
  auth_version="$(vm_lifecycle_field "$data" opencode_version)" || return 2
  vm_auth_version_supported "$auth_version" || return $?
  registry_info="$(vm_lifecycle_registry_for_profile "$root" "$name" "$data")" || return 2
  read -r registry run_id role < <(printf '%s' "$registry_info" | python3 -c '
import json,sys
try:
 item=json.load(sys.stdin); print(item["registry"],item["run_id"],item["role"])
except Exception: raise SystemExit(2)
') || return 2
  ssh_port="$(printf '%s' "$data" | python3 -c 'import json,sys; print(json.load(sys.stdin)["runtime_identity"]["ssh_port"])')" || return 2
  key="$root/vm/profiles/$name/control-key"
  known_hosts="$(dirname -- "$registry")/known_hosts"
  vm_ssh_listener_verified "$registry" "$run_id" "$role" "$ssh_port" || {
    vm_services_error 'The selected VM SSH listener failed ownership verification.'
    return 2
  }
  bridge_script="$VM_SERVICES_ROOT/lib/vm/browser-bridge-server.py"
  [[ -f "$bridge_script" && ! -L "$bridge_script" ]] || {
    vm_services_error 'The selected runtime has no verified host-browser bridge.'
    return 2
  }
  host_dir="$(mktemp -d /tmp/lm-vm-auth.XXXXXX)" || {
    vm_services_error 'Could not allocate a private browser-session directory.'
    return 1
  }
  trap 'vm_services_auth_cleanup "$root" "$name" "$key" "$known_hosts" "$ssh_port" "$bridge_script" "$bridge_pid" "$host_dir" "$guest_dir" "$control_path" "$master_pid" "$reverse_forward" "$host_port" "$guest_port"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM HUP
  chmod 700 "$host_dir" || return 1
  control_path="$host_dir/c"
  (( ${#control_path} < 100 )) || {
    vm_services_error 'Private SSH control socket path is too long.'
    return 2
  }
  token_file="$host_dir/token"
  bridge_log="$host_dir/bridge.log"
  ssh_log="$host_dir/ssh.log"
  (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(48))' >"$token_file") || return 1
  chmod 600 "$token_file"
  : >"$bridge_log"; : >"$ssh_log"; chmod 600 "$bridge_log" "$ssh_log"
  VM_BROWSER_BRIDGE_TOKEN_FILE="$token_file" VM_BROWSER_BRIDGE_PORT=0 \
    VM_AUTH_CALLBACK_LEASE_SECONDS="${VM_AUTH_CALLBACK_LEASE_SECONDS:-120}" \
    VM_BROWSER_BRIDGE_SSH_PORT="$ssh_port" VM_BROWSER_BRIDGE_SSH_KEY="$key" \
    VM_BROWSER_BRIDGE_KNOWN_HOSTS="$known_hosts" \
    VM_BROWSER_BRIDGE_SSH_LOG="$ssh_log" \
    VM_BROWSER_BRIDGE_SSH_DEBUG="$([[ "${VM_DEBUG:-0}" =~ ^[1-9][0-9]*$ ]] && printf 1 || printf 0)" \
    python3 "$bridge_script" </dev/null >>"$bridge_log" 2>&1 &
  bridge_pid=$!
  host_port=''
  for ((i=0; i<100; i++)); do
    vm_services_auth_bridge_matches "$bridge_pid" "$bridge_script" || {
      vm_services_error 'Host browser bridge failed to start; no browser was opened.'
      vm_services_auth_cleanup "$root" "$name" "$key" "$known_hosts" "$ssh_port" \
        "$bridge_script" "$bridge_pid" "$host_dir" "$guest_dir" "$control_path" \
        "$master_pid" "$reverse_forward" "$host_port" "$guest_port"
      return 1
    }
    host_port="$(sed -nE 's/^Host browser MCP bridge listening on 127\.0\.0\.1:([0-9]+)$/\1/p' "$bridge_log" | tail -n 1)"
    if [[ "$host_port" =~ ^[1-9][0-9]{3,4}$ ]] && (( 10#$host_port <= 65535 )) &&
       vm_services_auth_bridge_health "$host_port"; then
      break
    fi
    sleep 0.05
  done
  [[ "$host_port" =~ ^[1-9][0-9]{3,4}$ ]] && (( 10#$host_port <= 65535 )) &&
    vm_services_auth_bridge_health "$host_port" || {
      vm_services_error 'Host browser bridge did not become ready; no browser was opened.'
      vm_services_auth_cleanup "$root" "$name" "$key" "$known_hosts" "$ssh_port" \
        "$bridge_script" "$bridge_pid" "$host_dir" "$guest_dir" "$control_path" \
        "$master_pid" "$reverse_forward" "$host_port" "$guest_port"
      return 1
    }

  vm_services_shell_quote 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()'
  guest_port_command="python3 -c $VM_SERVICES_QUOTED"
  temp_port="$(vm_ssh_noninteractive "$key" "$known_hosts" "$ssh_port" 10 "$host" \
    "$guest_port_command")" || {
    vm_services_error 'Could not select a guest-loopback bridge port; no browser was opened.'
    vm_services_auth_cleanup "$root" "$name" "$key" "$known_hosts" "$ssh_port" \
      "$bridge_script" "$bridge_pid" "$host_dir" "$guest_dir" "$control_path" \
      "$master_pid" "$reverse_forward" "$host_port" "$guest_port"
    return 1
  }
  [[ "$temp_port" =~ ^[1-9][0-9]{3,4}$ ]] && (( 10#$temp_port <= 65535 )) || {
    vm_services_error 'Guest returned an invalid bridge port; no browser was opened.'
    vm_services_auth_cleanup "$root" "$name" "$key" "$known_hosts" "$ssh_port" \
      "$bridge_script" "$bridge_pid" "$host_dir" "$guest_dir" "$control_path" \
      "$master_pid" "$reverse_forward" "$host_port" "$guest_port"
    return 2
  }
  guest_port="$temp_port"
  reverse_forward="127.0.0.1:$guest_port:127.0.0.1:$host_port"

  vm_ssh_build_argv "$key" "$known_hosts" "$ssh_port" noninteractive || {
    vm_services_error 'Could not build the isolated SSH browser tunnel.'
    vm_services_auth_cleanup "$root" "$name" "$key" "$known_hosts" "$ssh_port" \
      "$bridge_script" "$bridge_pid" "$host_dir" "$guest_dir" "$control_path" \
      "$master_pid" "$reverse_forward" "$host_port" "$guest_port"
    return 2
  }
  local -a master_argv=("${VM_SSH_ARGV[@]}")
  if [[ "${VM_DEBUG:-0}" =~ ^[1-9][0-9]*$ ]]; then master_argv+=(-v); fi
  "${master_argv[@]}" -M -S "$control_path" -o ControlPersist=no \
    -o ExitOnForwardFailure=yes -R "$reverse_forward" -N "$host" \
    </dev/null >>"$ssh_log" 2>&1 &
  master_pid=$!
  for ((i=0; i<100; i++)); do
    kill -0 "$master_pid" 2>/dev/null || break
    if ssh -F /dev/null -S "$control_path" -O check -p "$ssh_port" "$host" \
      </dev/null >/dev/null 2>&1; then
      break
    fi
    sleep 0.05
  done
  if ! vm_services_auth_master_matches "$master_pid" "$control_path" "$reverse_forward" ||
     ! ssh -F /dev/null -S "$control_path" -O check -p "$ssh_port" "$host" \
       </dev/null >/dev/null 2>&1; then
    vm_services_error 'The temporary SSH browser tunnel failed; no browser was opened.'
    vm_services_auth_cleanup "$root" "$name" "$key" "$known_hosts" "$ssh_port" \
      "$bridge_script" "$bridge_pid" "$host_dir" "$guest_dir" "$control_path" \
      "$master_pid" "$reverse_forward" "$host_port" "$guest_port"
    return 1
  fi

  VM_SSH_CONTROL_PATH="$control_path"
  guest_dir="$(vm_ssh_noninteractive "$key" "$known_hosts" "$ssh_port" 10 "$host" \
    'umask 077 && mktemp -d /tmp/lm-vm-auth.XXXXXX')" || {
    vm_services_error 'Could not create private guest browser helpers; no browser was opened.'
    vm_services_auth_cleanup "$root" "$name" "$key" "$known_hosts" "$ssh_port" \
      "$bridge_script" "$bridge_pid" "$host_dir" "$guest_dir" "$control_path" \
      "$master_pid" "$reverse_forward" "$host_port" "$guest_port"
    return 1
  }
  [[ "$guest_dir" =~ ^/tmp/lm-vm-auth\.[A-Za-z0-9]{6}$ ]] || {
    guest_dir=''
    vm_services_error 'Guest returned an unsafe temporary browser path; no browser was opened.'
    vm_services_auth_cleanup "$root" "$name" "$key" "$known_hosts" "$ssh_port" \
      "$bridge_script" "$bridge_pid" "$host_dir" "$guest_dir" "$control_path" \
      "$master_pid" "$reverse_forward" "$host_port" "$guest_port"
    return 2
  }
  vm_ssh_noninteractive "$key" "$known_hosts" "$ssh_port" 20 "$host" \
    "install -m 700 /dev/stdin '$guest_dir/xdg-open'" \
    <"$VM_SERVICES_ROOT/lib/vm/browser-open.sh" >/dev/null 2>&1 || return 1
  vm_ssh_noninteractive "$key" "$known_hosts" "$ssh_port" 20 "$host" \
    "install -m 600 /dev/stdin '$guest_dir/browser-open.py'" \
    <"$VM_SERVICES_ROOT/lib/vm/browser-open.py" >/dev/null 2>&1 || return 1
  vm_ssh_noninteractive "$key" "$known_hosts" "$ssh_port" 20 "$host" \
    "ln -s xdg-open '$guest_dir/open' && ln -s xdg-open '$guest_dir/gio'" \
    </dev/null >/dev/null 2>&1 || return 1
  vm_ssh_noninteractive "$key" "$known_hosts" "$ssh_port" 20 "$host" \
    "install -m 600 /dev/stdin '$guest_dir/token'" \
    <"$token_file" >/dev/null 2>&1 || return 1

  VM_SERVICES_BROWSER_DIR="$guest_dir"
  VM_SERVICES_BROWSER_PORT="$guest_port"
  if declare -F vm_log_verbose >/dev/null && [[ "${VM_VERBOSE:-0}" =~ ^[1-9][0-9]*$ ]]; then
    vm_log_verbose "Using a temporary loopback-only browser bridge with a ${VM_AUTH_CALLBACK_LEASE_SECONDS:-120}-second callback lease."
  fi
  vm_services_agent_core "$root" "$name" "$permissions" "$requested_cwd" off "${agent_args[@]}"
  local rc=$?
  exit "$rc"
)

vm_services_agent() {
  local root="${1:-}" name="${2:-}" permissions="${3:-ask}"
  local requested_cwd="${4:-}" browser_mode="${5:-auto}" agent auth_version
  shift $(( $# >= 5 ? 5 : $# ))
  local -a agent_args=("$@")
  unset VM_SSH_CONTROL_PATH VM_SERVICES_BROWSER_DIR VM_SERVICES_BROWSER_PORT
  case "$browser_mode" in
    off) vm_services_agent_core "$root" "$name" "$permissions" "$requested_cwd" off "${agent_args[@]}"; return $? ;;
    auto|on) ;;
    *) vm_services_error 'Invalid browser-auth mode.'; return 2 ;;
  esac
  vm_services_profile_selection "$root" "$name" || return $?
  agent="$VM_SERVICES_SELECTED_AGENT"
  if [[ "$agent" != opencode ]]; then
    [[ "$browser_mode" == auto ]] && {
      vm_services_agent_core "$root" "$name" "$permissions" "$requested_cwd" off "${agent_args[@]}"
      return $?
    }
    vm_services_error 'Browser auth handoff is available only for the native OpenCode harness.'
    return 2
  fi
  if ! declare -F vm_auth_version_supported >/dev/null; then
    # shellcheck disable=SC1091 # Browser compatibility comes from the selected runtime.
    source "$VM_SERVICES_ROOT/lib/vm/auth.sh"
  fi
  auth_version="$(vm_lifecycle_field "$VM_SERVICES_SELECTED_PROFILE_DATA" opencode_version)" || return 2
  if [[ "$auth_version" != 2.2.5 ]]; then
    if [[ "$browser_mode" == auto ]]; then
      printf 'Browser handoff is disabled for this unreviewed OpenCode version; no host browser capability was started.\n' >&2
      vm_services_agent_core "$root" "$name" "$permissions" "$requested_cwd" off "${agent_args[@]}"
      return $?
    fi
    vm_auth_version_supported "$auth_version" || return $?
  fi
  ( vm_services_agent_browser_session "$root" "$name" "$permissions" "$requested_cwd" "${agent_args[@]}" )
}
