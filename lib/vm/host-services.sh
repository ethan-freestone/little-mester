#!/usr/bin/env bash
# Host-side services that are intentionally exposed to the VM through narrow
# loopback binds and QEMU guestfwd. This does not start or stop the VM itself.

vm_model_compose() {
  log_debug "docker compose --project-directory $LAB_DIR -f compose.yaml -f compose.vm-model-api.yaml --profile vm-model-api $*" >&2
  WORKSPACE="${WORKSPACE:-$LAB_DIR}" docker compose \
    --project-directory "$LAB_DIR" \
    -f "$LAB_DIR/compose.yaml" \
    -f "$LAB_DIR/compose.vm-model-api.yaml" \
    --profile vm-model-api "$@"
}

_vm_check_docker() {
  command -v docker >/dev/null 2>&1 || die "Docker CLI not found; install Docker Engine to manage host model APIs."
  docker info >/dev/null 2>&1 || die "Docker Engine is not reachable; start it before ./lab-vm models up."
}

_vm_default_model_provider() {
  python3 - "$LAB_DIR/agent/opencode/opencode.json" <<'PY'
import json
import sys
try:
    model = json.load(open(sys.argv[1], encoding="utf-8"))["model"]
    provider = model.split("/", 1)[0]
except (OSError, KeyError, TypeError, ValueError) as exc:
    raise SystemExit(f"cannot determine configured OpenCode model provider: {exc}")
if provider not in {"ollama", "llamacpp"}:
    raise SystemExit(f"no VM host forwarder is configured for provider {provider!r}")
print(provider)
PY
}

_vm_validate_api_publish_config() {
  local service="$1" expected_ip="$2" expected_port="$3" resolved
  resolved="$(vm_model_compose config --format json)" || die "Could not resolve VM API Compose configuration."
  python3 - "$resolved" "$service" "$expected_ip" "$expected_port" <<'PY'
import json
import sys

config = json.loads(sys.argv[1])
service, host_ip, port = sys.argv[2:]
ports = config.get("services", {}).get(service, {}).get("ports", [])
if not isinstance(ports, list) or not ports or not all(
    isinstance(p, dict)
    and str(p.get("target")) == port
    and str(p.get("published")) == port
    and p.get("host_ip") == host_ip
    for p in ports
):
    raise SystemExit(f"{service} must publish {host_ip}:{port}; resolved ports={ports!r}")
PY
}

_vm_api_diagnostics() {
  local service="$1" port="$2" path="$3" container
  echo "=== Docker publication diagnostics: $service ($port) ==="
  docker context show 2>&1 || true
  docker version --format 'client={{.Client.Version}} server={{.Server.Version}}' 2>&1 || true
  container="$(vm_model_compose ps -q "$service" 2>/dev/null || true)"
  if [[ -n "$container" ]]; then
    docker inspect --format 'state={{.State.Status}} network={{.HostConfig.NetworkMode}} configured_bindings={{json .HostConfig.PortBindings}} actual_bindings={{json .NetworkSettings.Ports}}' "$container" 2>&1 || true
    docker port "$container" 2>&1 || true
  else
    echo "Compose service $service has no container."
  fi
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>&1 | awk -v p=":$port" 'NR == 1 || index($0,p)'
  fi
  if [[ "$VM_DEBUG" -ge 1 ]]; then
    docker info 2>&1 | grep -Ei 'rootless|firewall|iptables|proxy|server version|operating system' || true
    docker network ls 2>&1 || true
    if command -v nft >/dev/null 2>&1; then nft list ruleset 2>&1 | grep -E -C 2 "dport $port|:$port" || true; fi
    if command -v iptables >/dev/null 2>&1; then iptables -t nat -S 2>&1 | grep -E -- "--dport $port|--to-destination .*:$port" || true; fi
  fi
  echo "--- host loopback HTTP probe"
  curl --noproxy '*' --http1.1 -v --connect-timeout 3 --max-time 8 "http://127.0.0.1:$port$path" 2>&1 || true
}

_vm_probe_published_api() {
  local service="$1" port="$2" path="$3" url="http://127.0.0.1:$2$3"
  for _attempt in {1..20}; do
    if curl --noproxy '*' --fail --silent --show-error --connect-timeout 2 --max-time 5 "$url" >/dev/null 2>&1; then
      log "$service is reachable at $url"
      return 0
    fi
    sleep 0.25
  done
  _vm_api_diagnostics "$service" "$port" "$path"
  return 1
}

# The direct guest forward targets host loopback. Support a user-managed native
# API on that exact address before falling back to the optional Compose API
# forwarder; never accept wildcard/non-loopback listeners as VM endpoints.
_vm_probe_host_model_api() {
  local provider="$1" port path service listeners probe_status
  case "$provider" in
    ollama) port=11434; path=/api/tags; service=Ollama ;;
    llamacpp|llama) port=8080; path=/health; service=llama.cpp ;;
    *) return 2 ;;
  esac

  if ! command -v ss >/dev/null 2>&1; then
    log "Cannot verify host listeners for $service: ss is unavailable; refusing to use or replace the endpoint."
    return 4
  fi
  if ! listeners="$(ss -H -ltn "sport = :$port" 2>/dev/null)"; then
    log "Cannot verify host listeners for $service: ss failed; refusing to use or replace the endpoint."
    return 4
  fi
  [[ -n "$listeners" ]] || return 1

  if ! VM_MODEL_PORT="$port" VM_MODEL_LISTENERS="$listeners" python3 - <<'PY'
import os
import sys

port = os.environ["VM_MODEL_PORT"]
lines = os.environ["VM_MODEL_LISTENERS"].splitlines()
if not lines:
    raise SystemExit(1)
for line in lines:
    fields = line.split()
    if len(fields) < 4 or fields[0] != "LISTEN" or fields[3] != f"127.0.0.1:{port}":
        raise SystemExit(1)
PY
  then
    log "$service API has a listener on port $port that is not loopback-only; refusing to use it for the VM."
    return 2
  fi

  if ! curl --noproxy '*' --fail --silent --show-error --connect-timeout 2 --max-time 5 \
    "http://127.0.0.1:$port$path" >/dev/null 2>&1; then
    log "$service API is listening on 127.0.0.1:$port but its $path health check failed."
    return 3
  fi

  log "$service API is healthy on host loopback 127.0.0.1:$port."
  return 0
}

_vm_metrics_state_dir() {
  printf '%s\n' "$LAB_DIR/.mester/vm-services/metrics"
}

_vm_metrics_pid_matches() {
  local pid="$1" command_line
  [[ "$pid" =~ ^[0-9]+$ && -r "/proc/$pid/cmdline" ]] || return 1
  command_line="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  [[ "$command_line" == *"$LAB_DIR/agent/metrics/gather.py"* ]]
}

vm_cmd_metrics() {
  local action="${1:-status}"
  local state_dir pid_file log_file lock_fd pid
  state_dir="$(_vm_metrics_state_dir)"
  pid_file="$state_dir/service.pid"
  log_file="$state_dir/service.log"

  case "$action" in
    up|start|down|stop)
      mkdir -p "$state_dir"
      command -v flock >/dev/null 2>&1 || die 'VM metrics lifecycle requires flock (util-linux).'
      exec {lock_fd}>"$state_dir/service.lock"
      flock -x "$lock_fd" || die 'Could not lock the VM metrics service state.'
      ;;
  esac

  case "$action" in
    up|start)
      mkdir -p "$state_dir"
      if [[ -f "$pid_file" ]]; then
        pid="$(cat "$pid_file" 2>/dev/null || true)"
        if _vm_metrics_pid_matches "$pid" && kill -0 "$pid" 2>/dev/null; then
          if curl --noproxy '*' --fail --silent --max-time 2 http://127.0.0.1:19532/health >/dev/null; then
            log "VM host metrics service already running at 127.0.0.1:19532"
            flock -u "$lock_fd" || true
            exec {lock_fd}>&-
            return 0
          fi
          die "Metrics PID $pid exists but its endpoint is unhealthy; preserve $log_file and investigate before restarting it."
        fi
        rm -f "$pid_file"
      fi

      if curl --noproxy '*' --fail --silent --max-time 1 http://127.0.0.1:19532/health >/dev/null 2>&1; then
        die "127.0.0.1:19532 is already serving another metrics endpoint; refusing to adopt or replace it."
      fi

      log "Starting curated VM host metrics endpoint on 127.0.0.1:19532"
      env METRICS_HOST_PROC=/proc \
          METRICS_HOST_HWMON=/sys/class/hwmon \
          METRICS_HOST_THERMAL=/sys/class/thermal \
          METRICS_BIND_HOST=127.0.0.1 \
          METRICS_BIND_PORT=19532 \
          python3 "$LAB_DIR/agent/metrics/gather.py" \
            </dev/null >>"$log_file" 2>&1 &
      pid=$!
      printf '%s\n' "$pid" > "$pid_file"
      for _attempt in {1..30}; do
        if ! kill -0 "$pid" 2>/dev/null; then
          rm -f "$pid_file"
          tail -n 60 "$log_file" >&2 || true
          die "VM host metrics service exited during startup (status unavailable); see $log_file."
        fi
        if curl --noproxy '*' --fail --silent --max-time 1 http://127.0.0.1:19532/health >/dev/null 2>&1; then
          log "VM host metrics service is healthy at 127.0.0.1:19532"
          flock -u "$lock_fd" || true
          exec {lock_fd}>&-
          return 0
        fi
        sleep 0.1
      done
      kill "$pid" 2>/dev/null || true
      rm -f "$pid_file"
      tail -n 60 "$log_file" >&2 || true
      die "VM host metrics endpoint did not become healthy; see $log_file."
      ;;
    status)
      if [[ -f "$pid_file" ]]; then
        pid="$(cat "$pid_file" 2>/dev/null || true)"
        if _vm_metrics_pid_matches "$pid" && kill -0 "$pid" 2>/dev/null; then
          if curl --noproxy '*' --fail --silent --max-time 2 http://127.0.0.1:19532/health >/dev/null; then
            printf 'VM host metrics service is running at 127.0.0.1:19532; '
            if curl --noproxy '*' --fail --silent --show-error --max-time 2 http://127.0.0.1:19532/summary; then
              printf '\n'
            else
              printf 'first hardware sample is warming up.\n'
            fi
            return 0
          fi
        fi
      fi
      echo "VM host metrics service is stopped or unhealthy."
      return 1
      ;;
    down|stop)
      if [[ -f "$pid_file" ]]; then
        pid="$(cat "$pid_file" 2>/dev/null || true)"
        if _vm_metrics_pid_matches "$pid" && kill -0 "$pid" 2>/dev/null; then
          kill "$pid" 2>/dev/null || true
          for _attempt in {1..30}; do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.1
          done
          if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
          fi
        fi
        rm -f "$pid_file"
      fi
      log "Stopped only the VM host metrics process; the host-Docker metrics service is untouched."
      flock -u "$lock_fd" || true
      exec {lock_fd}>&-
      ;;
    *)
      die "Usage: ./lab-vm metrics {up|status|down}"
      ;;
  esac
}

vm_cmd_models() {
  local action="${1:-status}"
  shift || true
  local provider service port path container state actual mapping_ok status=0 item probe_status
  local -a services=()
  local -a services_to_start=()

  case "$action" in
    up)
      if [[ $# -eq 0 || "$1" == default ]]; then
        provider="$(_vm_default_model_provider)"
      else
        provider="$1"
      fi
      case "$provider" in
        llama|llamacpp) services+=(llamacpp-vm-api) ;;
        ollama) services+=(ollama-vm-api) ;;
        all) services+=(ollama-vm-api llamacpp-vm-api) ;;
          *) die "Usage: ./lab-vm models up [default|ollama|llamacpp|all]" ;;
      esac

      for service in "${services[@]}"; do
        case "$service" in
          ollama-vm-api) provider=ollama ;;
          llamacpp-vm-api) provider=llamacpp ;;
        esac
        if _vm_probe_host_model_api "$provider"; then
          log "Using the existing host API for $provider; no model forwarder was started."
        else
          probe_status=$?
          case "$probe_status" in
            1) services_to_start+=("$service") ;;
            *) die "Refusing to start a model forwarder while the existing $provider endpoint cannot be verified safely." ;;
          esac
        fi
      done
      ((${#services_to_start[@]})) || return 0
      services=("${services_to_start[@]}")
      _vm_check_docker
      vm_model_compose config -q
      for service in "${services[@]}"; do
        case "$service" in
          ollama-vm-api) _vm_validate_api_publish_config "$service" 127.0.0.1 11434 || die "Unsafe or missing port mapping for $service." ;;
          llamacpp-vm-api) _vm_validate_api_publish_config "$service" 127.0.0.1 8080 || die "Unsafe or missing port mapping for $service." ;;
        esac
      done
      if [[ $VM_VERBOSE -ge 1 ]]; then
        log_verbose "Resolved Compose publication config:"
        vm_model_compose config --format json | python3 -c 'import json,sys; d=json.load(sys.stdin); [print("  {}: {}".format(n,d["services"][n].get("ports", []))) for n in sys.argv[1:]]' "${services[@]}"
      fi
      log "Starting loopback-only Compose VM API services: ${services[*]}"
      vm_model_compose up -d "${services[@]}"
      for service in "${services[@]}"; do
        case "$service" in
          llamacpp-vm-api) _vm_probe_published_api "$service" 8080 /health || die "$service is configured but unreachable via Docker loopback publication. No firewall or network changes were made; preserve diagnostics for review." ;;
          ollama-vm-api) _vm_probe_published_api "$service" 11434 /api/tags || die "$service is configured but unreachable via Docker loopback publication. No firewall or network changes were made; preserve diagnostics for review." ;;
          metrics-vm-api) _vm_probe_published_api "$service" 19532 /summary || die "$service is configured but unreachable via Docker loopback publication. No firewall or network changes were made; preserve diagnostics for review." ;;
        esac
      done
      log "VM API services are reachable through Docker loopback-only published ports."
      ;;
    status)
      (($# <= 1)) || die "Usage: ./lab-vm models status [default|ollama|llamacpp|all]"
      if [[ $# -eq 0 || "$1" == default ]]; then
        provider="$(_vm_default_model_provider)" || die 'Could not determine the configured local model provider.'
      else
        provider="$1"
      fi
      case "$provider" in
        llama|llamacpp) services+=("llamacpp-vm-api|llamacpp|8080|/health") ;;
        ollama) services+=("ollama-vm-api|ollama|11434|/api/tags") ;;
        all) services+=("ollama-vm-api|ollama|11434|/api/tags" "llamacpp-vm-api|llamacpp|8080|/health") ;;
        *) die "Usage: ./lab-vm models status [default|ollama|llamacpp|all]" ;;
      esac
      for item in "${services[@]}"; do
        IFS='|' read -r service provider port path <<<"$item"
        if _vm_probe_host_model_api "$provider"; then
          continue
        else
          probe_status=$?
        fi
        if [[ "$probe_status" -eq 2 || "$probe_status" -eq 3 || "$probe_status" -eq 4 ]]; then
          status=1
          continue
        fi
        if ! command -v docker >/dev/null 2>&1; then
          log "$service: no healthy loopback API at 127.0.0.1:$port and Docker CLI is unavailable."
          status=1
          continue
        fi
        container="$(vm_model_compose ps -q "$service" 2>/dev/null || true)"
        if [[ -z "$container" ]]; then
          log "$service: not started. Start it with './lab-vm models up $provider'."
          status=1
          continue
        fi
        state="$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null || echo unknown)"
        actual="$(docker inspect --format '{{json .NetworkSettings.Ports}}' "$container" 2>/dev/null || echo unavailable)"
        log "$service: state=$state; expected=127.0.0.1:$port; runtime_ports=$actual"
        if [[ "$state" != running ]]; then
          log "$service: container is not running."
          status=1
        fi
        mapping_ok=0
        if python3 - "$actual" "$port" <<'PY'
import json
import sys

try:
    mappings = json.loads(sys.argv[1])
except (TypeError, json.JSONDecodeError):
    raise SystemExit(1)

if not isinstance(mappings, dict):
    raise SystemExit(1)
port = sys.argv[2]
entries = mappings.get(f"{port}/tcp") or []
if isinstance(entries, list) and entries and all(
    isinstance(item, dict) and item.get("HostIp") == "127.0.0.1" and item.get("HostPort") == port
    for item in entries
):
    raise SystemExit(0)
raise SystemExit(1)
PY
        then
          mapping_ok=1
        fi
        if [[ "$mapping_ok" -ne 1 ]]; then
          log "$service: Docker has no active 127.0.0.1:$port runtime mapping; run './lab-vm models diagnose' for read-only diagnostics."
          status=1
        fi
        if [[ "$state" == running && "$mapping_ok" == 1 ]]; then
          if curl --noproxy '*' --fail --silent --show-error --connect-timeout 2 --max-time 5 "http://127.0.0.1:$port$path" >/dev/null 2>&1; then
            log "$service: loopback HTTP probe passed."
          else
            log "$service: loopback HTTP probe failed. Run './lab-vm models diagnose' for read-only diagnostics."
            status=1
          fi
        fi
      done
      return "$status"
      ;;
    down|stop)
      if command -v docker >/dev/null 2>&1; then
        vm_model_compose stop ollama-vm-api llamacpp-vm-api || true
      fi
      log "Stopped only VM model API Compose services; model containers and VM were left running."
      ;;
    *)
      die "Usage: ./lab-vm models {up [default|ollama|llamacpp|all]|status [default|ollama|llamacpp|all]|down}"
      ;;
  esac
}
