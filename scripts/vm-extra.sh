#!/usr/bin/env bash
# TEMPORARY VM REPAIR/DIAGNOSTIC SCRIPT.
# MUST BE REMOVED once ticket #42's host model, metrics, and TUI gates pass.
# Keep stable host service management in ./lab-vm models, not in this script.
# The ./lab-vm extra dispatcher is intentionally modular so this file and its
# temporary commands can be removed without changing normal VM lifecycle code.

set -euo pipefail
LAB_DIR="$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")/.." && pwd)"
VM_NAME="${2:-default}"
VM_DIR="$LAB_DIR/.mester/vm/$VM_NAME"
: "${VM_VERBOSE:=0}"
: "${VM_DEBUG:=0}"

log_verbose() { [[ "$VM_VERBOSE" -ge 1 ]] && echo "[verbose] $*" || true; }
log_debug() { [[ "$VM_DEBUG" -ge 1 ]] && echo "[debug] $*" || true; }

# Keep a redacted-by-design diagnostic transcript; probes never print proxy
# credentials or environment values.
mkdir -p "$LAB_DIR/.mester/logs"
EXTRA_LOG="$LAB_DIR/.mester/logs/vm-extra-$(date '+%Y%m%d_%H%M%S').log"
exec > >(tee -a "$EXTRA_LOG") 2>&1
echo "Diagnostic log: $EXTRA_LOG"
source "$LAB_DIR/lib/vm/host-services.sh"

usage() {
  cat <<'EOF'
Temporary VM diagnostics (remove after #42 model/metrics/TUI gates pass):
  ./lab-vm [-v|-d] extra diagnose-model-api [vm-name]
  ./lab-vm [-v|-d] extra repair-model-api [vm-name]
    -v shows HTTP request/response headers; -d also shows Compose, container,
       network, Docker firewall/security, and forwarder log diagnostics.
EOF
}

guest_diagnostics() {
  [[ -s "$VM_DIR/ssh_port" && -r "$VM_DIR/id_vm_control" ]] || {
    echo "No SSH control state for VM '$VM_NAME'; run ./lab-vm up first." >&2
    return 1
  }
  local port
  port="$(cat "$VM_DIR/ssh_port")"
  ssh -o BatchMode=yes -o ConnectTimeout=5 \
    -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$VM_DIR/known_hosts" \
    -i "$VM_DIR/id_vm_control" -p "$port" mester@127.0.0.1 \
    "VM_VERBOSE=$VM_VERBOSE VM_DEBUG=$VM_DEBUG bash -s" <<'GUEST'
set +e
echo "guest_no_proxy_has_vm_host=$([[ "${NO_PROXY:-${no_proxy:-}}" == *10.0.2.100* ]] && echo yes || echo no)"
echo '=== guest model APIs ==='
  if [[ "${VM_VERBOSE:-0}" -ge 1 ]]; then curl -v --noproxy '*' --http1.1 --max-time 10 -w '\nhealth_http=%{http_code}\n' http://10.0.2.100:8080/health; else curl -sS -D - --noproxy '*' --http1.1 --max-time 10 -w '\nhealth_http=%{http_code}\n' http://10.0.2.100:8080/health; fi
  if [[ "${VM_VERBOSE:-0}" -ge 1 ]]; then curl -v --noproxy '*' --http1.1 --max-time 10 -w '\nmodels_http=%{http_code}\n' http://10.0.2.100:8080/v1/models; else curl -sS -D - --noproxy '*' --http1.1 --max-time 10 -w '\nmodels_http=%{http_code}\n' http://10.0.2.100:8080/v1/models; fi
echo '=== guest model completion ==='
if [[ "${VM_VERBOSE:-0}" -ge 1 ]]; then
  curl -v --noproxy '*' --http1.1 --max-time 120 -w '\ncompletion_http=%{http_code}\n' \
    http://10.0.2.100:8080/v1/chat/completions -H 'Content-Type: application/json' \
    -d '{"model":"qwen3.6-35b-a3b-q4_k_m","messages":[{"role":"user","content":"Reply exactly: VM_MODEL_OK"}],"stream":false,"max_tokens":32}'
else
  curl -sS -D - --noproxy '*' --http1.1 --max-time 120 -w '\ncompletion_http=%{http_code}\n' \
    http://10.0.2.100:8080/v1/chat/completions -H 'Content-Type: application/json' \
    -d '{"model":"qwen3.6-35b-a3b-q4_k_m","messages":[{"role":"user","content":"Reply exactly: VM_MODEL_OK"}],"stream":false,"max_tokens":32}'
fi
echo '=== guest metrics ==='
if [[ "${VM_VERBOSE:-0}" -ge 1 ]]; then curl -v --noproxy '*' --http1.1 --max-time 10 -w '\nmetrics_http=%{http_code}\n' http://10.0.2.100:19532/summary; else curl -sS -D - --noproxy '*' --http1.1 --max-time 10 -w '\nmetrics_http=%{http_code}\n' http://10.0.2.100:19532/summary; fi
GUEST
}

diagnose_backend() {
  local service="$1" port="$2" path="$3" container ip
  container="$(vm_model_compose ps -q "$service" 2>/dev/null || true)"
  if [[ -z "$container" ]]; then
    echo "$service: no container"
    return 0
  fi
  echo "=== $service backend container ==="
  docker inspect --format 'state={{.State.Status}} network_mode={{.HostConfig.NetworkMode}} port_bindings={{json .HostConfig.PortBindings}} networks={{json .NetworkSettings.Networks}}' "$container" || true
  ip="$(docker inspect --format '{{range .NetworkSettings.Networks}}{{if .IPAddress}}{{.IPAddress}}{{"\n"}}{{end}}{{end}}' "$container" | head -n 1)"
  echo "internal_ip=$ip backend=$ip:$port"
  echo "--- host direct-to-container bridge probe"
  if [[ "$VM_VERBOSE" -ge 1 ]]; then
    curl -v --noproxy '*' --http1.1 --connect-timeout 3 --max-time 10 "http://$ip:$port$path" || true
  else
    curl -sS -D - --noproxy '*' --http1.1 --connect-timeout 3 --max-time 10 -w '\nhttp=%{http_code}\n' "http://$ip:$port$path" || true
  fi
  if [[ "$VM_DEBUG" -ge 1 ]]; then
    docker logs --tail 40 "$container" 2>&1 || true
  fi
}

host_diagnostics() {
  log_verbose "Running host, Docker bridge, loopback-published-port, and guestfwd checks."
  echo "=== Docker Engine ==="
  docker version --format 'server={{.Server.Version}}'
  if [[ "$VM_DEBUG" -ge 1 ]]; then
    docker info --format 'security={{json .SecurityOptions}} firewall={{.FirewallBackend}} root={{.DockerRootDir}}' 2>&1 || docker info --format 'security={{json .SecurityOptions}} root={{.DockerRootDir}}' 2>&1 || true
    command -v ss >/dev/null 2>&1 && { echo '=== host TCP listeners (diagnostic only) ==='; ss -ltnp | awk 'NR == 1 || /:8080|:19532/'; } || true
    echo '=== resolved Compose port configuration ==='
    vm_model_compose config --format json | python3 -c 'import json,sys; d=json.load(sys.stdin); [print("{}: {}".format(n,d["services"].get(n,{}).get("ports"))) for n in ("llamacpp-vm-api","metrics-vm-api")]' || true
  fi
  echo "=== forwarder containers ==="
  "$LAB_DIR/lab-vm" models status
  diagnose_backend llamacpp 11434 /health
  diagnose_backend metrics 19532 /summary
  echo "=== host loopback probes (proxy bypassed) ==="
  for endpoint in http://127.0.0.1:8080/health http://127.0.0.1:8080/v1/models http://127.0.0.1:19532/summary; do
    echo "--- $endpoint"
    if [[ "$VM_VERBOSE" -ge 1 ]]; then
      curl -v --noproxy '*' --http1.1 --connect-timeout 3 --max-time 10 "$endpoint" || true
    else
      curl -sS -D - --noproxy '*' --http1.1 --connect-timeout 3 --max-time 10 -w '\nhttp=%{http_code}\n' "$endpoint" || true
    fi
  done
}

case "${1:-help}" in
  diagnose-model-api)
    host_diagnostics
    guest_diagnostics
    ;;
  repair-model-api)
    if ! "$LAB_DIR/lab-vm" models up; then
      echo '=== model setup failed; collecting diagnostics without further repair ===' >&2
      host_diagnostics
      guest_diagnostics || true
      exit 1
    fi
    host_diagnostics
    guest_diagnostics
    ;;
  help|-h|--help) usage ;;
  *) usage >&2; exit 2 ;;
esac
