#!/usr/bin/env bash
# Read-only diagnostics for the optional host-to-VM model API forwarders.
set -uo pipefail

LAB_DIR="$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")/.." && pwd)"
VM_VERBOSE="${VM_VERBOSE:-0}"
VM_DEBUG="${VM_DEBUG:-0}"

usage() {
  cat <<'EOF'
Usage: ./lab-vm [-v|-d] models diagnose [ollama|llamacpp|all]
       ./lab-vm [-v|-d] models diagnose [--sudo] [ollama|llamacpp|all]

Collect read-only Docker publication, host-listener, HTTP, backend-health, and
available Docker journal evidence. --sudo uses non-interactive sudo only for
read-only iptables/nftables rule inspection. Does not start/recreate/stop
containers or change Docker, firewall, or network settings. Defaults to all.
EOF
}

use_sudo=0
provider=all
provider_set=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --sudo) use_sudo=1 ;;
    all|ollama|llama|llamacpp)
      if [[ "$provider_set" -eq 1 ]]; then usage >&2; exit 2; fi
      provider="$1"
      provider_set=1
      ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done

case "$provider" in
  all) services=("ollama-vm-api|11434|/api/tags|ollama|11434" "llamacpp-vm-api|8080|/health|llamacpp|11434") ;;
  ollama) services=("ollama-vm-api|11434|/api/tags|ollama|11434") ;;
  llama|llamacpp) services=("llamacpp-vm-api|8080|/health|llamacpp|11434") ;;
esac

compose() {
  WORKSPACE="${WORKSPACE:-$LAB_DIR}" docker compose --project-directory "$LAB_DIR" \
    -f "$LAB_DIR/compose.yaml" -f "$LAB_DIR/compose.vm-model-api.yaml" \
    --profile vm-model-api "$@"
}

section() { printf '\n=== %s ===\n' "$*"; }
status=0
run_rule_probe() {
  if [[ "$use_sudo" -eq 1 ]]; then
    if ! command -v sudo >/dev/null 2>&1; then
      echo 'sudo unavailable.'
      return 127
    fi
    sudo -n "$@"
  else
    "$@"
  fi
}

printf 'Read-only VM model forwarding diagnostics\n'
printf 'Repository: %s\n' "$LAB_DIR"
if ! command -v docker >/dev/null 2>&1; then
  echo 'Docker CLI unavailable; cannot inspect the model forwarders.'
  exit 2
fi
if ! docker info >/dev/null 2>&1; then
  echo 'Docker Engine unavailable; no containers or host settings were changed.'
  exit 2
fi

section 'Docker Engine'
docker context show 2>&1 || status=1
docker version --format 'client={{.Client.Version}} server={{.Server.Version}}' 2>&1 || status=1
docker info --format 'root={{.DockerRootDir}} security={{json .SecurityOptions}} firewall={{.FirewallBackend}}' 2>&1 || status=1
if [[ "$VM_DEBUG" -ge 1 ]]; then
  docker info 2>&1 | grep -E '^( Operating System| Firewall Backend| EnableUserlandProxy| UserlandProxyPath):' || true
fi

section 'Resolved Compose publication configuration'
resolved="$(compose config --format json 2>&1)" || {
  printf '%s\n' "$resolved"
  echo 'Could not resolve Compose config; stopping before container probes.'
  exit 1
}
python3 - "$resolved" "${services[@]}" <<'PY'
import json
import sys

config = json.loads(sys.argv[1])
for spec in sys.argv[2:]:
    service = spec.split("|", 1)[0]
    item = config.get("services", {}).get(service)
    if item is None:
        print(f"{service}: absent from resolved Compose configuration")
    else:
        print(f"{service}: ports={item.get('ports', [])}")
PY

section 'Compose containers'
compose ps -a 2>&1 || status=1

for spec in "${services[@]}"; do
  IFS='|' read -r service host_port path backend backend_port <<<"$spec"
  section "$service (host 127.0.0.1:$host_port$path → $backend:$backend_port)"

  container="$(compose ps -a -q "$service" 2>/dev/null | head -n 1)"
  if [[ -z "$container" ]]; then
    echo 'Forwarder container: not present (diagnostic did not start it).'
    continue
  fi

  forwarder_info="$(docker inspect --format 'name={{.Name}} created={{.Created}} state={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} network_mode={{.HostConfig.NetworkMode}} configured_bindings={{json .HostConfig.PortBindings}} runtime_ports={{json .NetworkSettings.Ports}} networks={{json .NetworkSettings.Networks}}' "$container" 2>&1)" || status=1
  printf '%s\n' "$forwarder_info"
  if [[ "$forwarder_info" != *'state=running'* ]]; then status=1; fi
  if [[ "$forwarder_info" == *'runtime_ports='*null* ]]; then
    echo 'Docker has no active runtime host-port mapping for this forwarder.'
    status=1
  fi
  ports="$(docker port "$container" 2>&1)"
  if [[ -n "$ports" ]]; then
    printf 'docker port:\n%s\n' "$ports"
  else
    echo 'docker port: no active published-port mapping reported.'
    status=1
  fi

  backend_id="$(compose ps -q "$backend" 2>/dev/null | head -n 1)"
  if [[ -n "$backend_id" ]]; then
    backend_info="$(docker inspect --format 'backend={{.Name}} state={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} networks={{json .NetworkSettings.Networks}}' "$backend_id" 2>&1)" || status=1
    printf '%s\n' "$backend_info"
    if [[ "$backend_info" != *'state=running'* || "$backend_info" == *'health=unhealthy'* ]]; then status=1; fi
  else
    echo "backend $backend: container not present"
    status=1
  fi

  if command -v ss >/dev/null 2>&1; then
    echo "Host TCP listener for :$host_port:"
    ss -ltnp 2>&1 | awk -v p=":$host_port" 'NR == 1 || index($0,p)'
  else
    echo 'Host listener probe unavailable (ss not installed).'
  fi

  echo "Host loopback HTTP probe: http://127.0.0.1:$host_port$path"
  if curl --noproxy '*' --http1.1 --fail --connect-timeout 2 --max-time 5 \
      --silent --show-error --output /dev/null \
      --write-out 'http_status=%{http_code} remote_ip=%{remote_ip}\n' \
      "http://127.0.0.1:$host_port$path"; then
    :
  else
    echo 'Host loopback probe failed.'
    status=1
  fi

  if [[ "$VM_DEBUG" -ge 1 ]]; then
    echo 'Recent forwarder logs (last 30 lines; no environment/config dump):'
    docker logs --tail 30 "$container" 2>&1 || echo 'Forwarder logs unavailable.'
  fi
done

section 'Host listeners for known VM service ports'
if command -v ss >/dev/null 2>&1; then
  ss -ltnp 2>&1 | awk 'NR == 1 || /:8080|:11434|:19532/'
else
  echo 'Unavailable (ss not installed).'
fi

section 'Host Docker port-publication rules (read-only)'
if command -v iptables-save >/dev/null 2>&1; then
  nat_rules="$(run_rule_probe iptables-save -t nat 2>&1)"
  if [[ "$nat_rules" == *'Permission denied'* || "$nat_rules" == *'Operation not permitted'* || "$nat_rules" == *'a password is required'* || "$nat_rules" == *'not allowed to execute'* ]]; then
    echo 'iptables NAT rules unavailable; no additional privilege was requested.'
    [[ "$VM_VERBOSE" -ge 1 ]] && printf '%s\n' "$nat_rules"
  elif [[ -n "$nat_rules" ]]; then
    printf '%s\n' "$nat_rules" | grep -E '^(\*nat|:DOCKER|:DOCKER-USER|:DOCKER-FORWARD|:DOCKER-INGRESS|-A DOCKER|COMMIT)|8080' || echo 'No Docker or port-8080 NAT rules found.'
  else
    echo 'iptables-save returned no NAT rules.'
  fi
else
  echo 'iptables-save unavailable.'
fi

if command -v nft >/dev/null 2>&1; then
  nft_rules="$(run_rule_probe nft list ruleset 2>&1)"
  if [[ "$nft_rules" == *'Permission denied'* || "$nft_rules" == *'Operation not permitted'* || "$nft_rules" == *'a password is required'* || "$nft_rules" == *'not allowed to execute'* ]]; then
    echo 'nftables rules unavailable; no additional privilege was requested.'
    [[ "$VM_VERBOSE" -ge 1 ]] && printf '%s\n' "$nft_rules"
  else
    nft_matches="$(printf '%s\n' "$nft_rules" | grep -Ei -C 2 'docker|8080' || true)"
    if [[ -n "$nft_matches" ]]; then printf '%s\n' "$nft_matches"; else echo 'No Docker or port-8080 nftables rules found.'; fi
  fi
else
  echo 'nft unavailable.'
fi

section 'Recent Docker daemon publication/network errors (best effort, last hour)'
if command -v journalctl >/dev/null 2>&1; then
  journal="$(journalctl -u docker --since '1 hour ago' --no-pager -n 200 2>&1)"
  case "$journal" in
    *'Permission denied'*|*'not seeing messages'*|*'No journal files were found'*)
      echo 'Docker journal unavailable to this user; no elevated command was run.'
      [[ "$VM_VERBOSE" -ge 1 ]] && printf '%s\n' "$journal"
      ;;
    *)
      filtered="$(printf '%s\n' "$journal" | grep -Ei '8080|11434|19532|port|proxy|iptables|nft|publish|endpoint|network' | tail -n 60 || true)"
      filtered="$(printf '%s\n' "$filtered" | sed -E \
        -e 's#(https?://)[^/@[:space:]]+:[^/@[:space:]]+@#\1[REDACTED]@#g' \
        -e 's/([Aa]uthorization[=: ]+).*/\1[REDACTED]/' \
        -e 's/((token|password|secret|api[_-]?key)[=: ]+)[^ ,;]+/\1[REDACTED]/Ig')"
      if [[ -n "$filtered" ]]; then printf '%s\n' "$filtered"; else echo 'No matching recent daemon messages.'; fi
      ;;
  esac
else
  echo 'Unavailable (journalctl not installed).'
fi

section 'Result'
if [[ "$status" -eq 0 ]]; then
  echo 'No failed probes were observed for the selected running forwarders.'
else
  echo 'One or more Docker, listener, or HTTP checks failed; no repairs were attempted.'
fi
exit "$status"
