#!/usr/bin/env bash
# Read-only diagnostics for the optional host-to-VM model API forwarders.
set -uo pipefail

LAB_DIR="$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")/.." && pwd)"
VM_VERBOSE="${VM_VERBOSE:-0}"
VM_DEBUG="${VM_DEBUG:-0}"

usage() {
  cat <<'EOF'
Usage: ./lab-vm [-v|-d] models diagnose [ollama|llamacpp|all]
       ./lab-vm [-v|-d] models diagnose [--sudo] [--probe-egress] [ollama|llamacpp|all]

Collect read-only Docker daemon/network publication, host-listener, HTTP,
backend-health, and available Docker journal evidence. --sudo uses
non-interactive sudo only for read-only firewall and daemon-config inspection.
--probe-egress explicitly attempts a TCP connection from each running forwarder
to 1.1.1.1:443; a successful connection shows that this bridge has some egress,
not that all egress is available. Does not start/recreate/stop containers or
change Docker, firewall, or network settings. Defaults to all.
EOF
}

use_sudo=0
probe_egress=0
provider=all
provider_set=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --sudo) use_sudo=1 ;;
    --probe-egress) probe_egress=1 ;;
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
  all) services=("ollama-vm-api|11434|/api/tags|ollama|11434|11434" "llamacpp-vm-api|8080|/health|llamacpp|11434|8080") ;;
  ollama) services=("ollama-vm-api|11434|/api/tags|ollama|11434|11434") ;;
  llama|llamacpp) services=("llamacpp-vm-api|8080|/health|llamacpp|11434|8080") ;;
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

section 'Docker daemon port-publication settings (allowlisted)'
if command -v cat >/dev/null 2>&1; then
  daemon_config="$(run_rule_probe cat /etc/docker/daemon.json 2>&1)"
  if [[ "$daemon_config" == *'No such file or directory'* ]]; then
    echo '/etc/docker/daemon.json not present.'
  elif [[ "$daemon_config" == *'Permission denied'* || "$daemon_config" == *'a password is required'* || "$daemon_config" == *'not allowed to execute'* ]]; then
    echo 'daemon.json unavailable; rerun with --sudo if authorized.'
    [[ "$VM_VERBOSE" -ge 1 ]] && printf '%s\n' "$daemon_config"
  elif ! printf '%s' "$daemon_config" | python3 -c '
import json, sys
try:
    settings = json.load(sys.stdin)
except (json.JSONDecodeError, UnicodeDecodeError) as exc:
    raise SystemExit(f"daemon.json could not be parsed: {exc}")
allowed = ("iptables", "ip6tables", "userland-proxy", "userland-proxy-path", "firewall-backend", "bridge", "bip", "ip-forward", "ip-masq")
print(json.dumps({key: settings[key] for key in allowed if key in settings}, sort_keys=True))
'; then
    echo 'daemon.json settings unavailable or invalid; raw configuration was not printed.'
  fi
else
  echo 'cat unavailable; daemon.json not inspected.'
fi

if command -v systemctl >/dev/null 2>&1; then
  docker_execstart="$(systemctl show docker.service --property=ExecStart --value 2>&1)"
  if [[ "$docker_execstart" == *'not found'* || "$docker_execstart" == *'not loaded'* ]]; then
    echo 'Docker systemd ExecStart unavailable.'
  else
    printf '%s\n' "$docker_execstart" | python3 -c '
import re, sys
text = sys.stdin.read()
pattern = r"--(?:iptables|ip6tables|userland-proxy|userland-proxy-path|firewall-backend|bridge|bip|ip-forward|ip-masq)(?:=|\s+)[^ ;}\]]+"
matches = re.findall(pattern, text)
print("ExecStart publication flags: " + (" ".join(matches) if matches else "none explicitly set"))
'
  fi
else
  echo 'systemctl unavailable; Docker service flags not inspected.'
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
  IFS='|' read -r service host_port path backend backend_port container_port <<<"$spec"
  section "$service (host 127.0.0.1:$host_port$path → $backend:$backend_port)"

  container="$(compose ps -a -q "$service" 2>/dev/null | head -n 1)"
  if [[ -z "$container" ]]; then
    echo 'Forwarder container: not present (diagnostic did not start it).'
    continue
  fi

  forwarder_info="$(docker inspect --format 'name={{.Name}} created={{.Created}} state={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} network_mode={{.HostConfig.NetworkMode}} configured_bindings={{json .HostConfig.PortBindings}} runtime_ports={{json .NetworkSettings.Ports}} networks={{json .NetworkSettings.Networks}}' "$container" 2>&1)" || status=1
  printf '%s\n' "$forwarder_info"
  if [[ "$forwarder_info" != *'state=running'* ]]; then status=1; fi
  if [[ "$forwarder_info" == *"runtime_ports={\"${container_port}/tcp\":null}"* || "$forwarder_info" == *'runtime_ports=null'* ]]; then
    echo 'Docker has no active runtime host-port mapping for this forwarder.'
    status=1
  fi
  network_names="$(docker inspect --format '{{range $name, $config := .NetworkSettings.Networks}}{{$name}} {{end}}' "$container" 2>/dev/null)"
  container_ip=''
  read -r -a network_names_array <<<"$network_names"
  for network_name in "${network_names_array[@]}"; do
    docker network inspect --format 'network={{.Name}} driver={{.Driver}} internal={{.Internal}} options={{json .Options}} ipam={{json .IPAM.Config}}' "$network_name" 2>&1 || status=1
    network_internal="$(docker network inspect --format '{{.Internal}}' "$network_name" 2>/dev/null)"
    if [[ "$network_internal" == 'false' ]]; then
      network_ip="$(docker inspect --format "{{(index .NetworkSettings.Networks \"$network_name\").IPAddress}}" "$container" 2>/dev/null)"
      if [[ -n "$network_ip" ]]; then container_ip="$network_ip"; fi
    fi
  done
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

  if [[ -n "$container_ip" ]]; then
    echo "Host direct-container HTTP probe: http://$container_ip:$container_port$path"
    if curl --noproxy '*' --http1.1 --fail --connect-timeout 2 --max-time 5 \
        --silent --show-error --output /dev/null \
        --write-out 'http_status=%{http_code} remote_ip=%{remote_ip}\n' \
        "http://$container_ip:$container_port$path"; then
      direct_container_ok=1
    else
      direct_container_ok=0
      echo 'Host direct-container probe failed.'
      status=1
    fi
  else
    direct_container_ok=0
    echo 'Host direct-container HTTP probe unavailable (no container IPv4 address).'
    status=1
  fi

  echo "Host loopback HTTP probe: http://127.0.0.1:$host_port$path"
  if curl --noproxy '*' --http1.1 --fail --connect-timeout 2 --max-time 5 \
      --silent --show-error --output /dev/null \
      --write-out 'http_status=%{http_code} remote_ip=%{remote_ip}\n' \
      "http://127.0.0.1:$host_port$path"; then
    loopback_ok=1
  else
    loopback_ok=0
    echo 'Host loopback probe failed.'
    status=1
  fi

  if command -v ip >/dev/null 2>&1; then
    route_source="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')"
  else
    route_source=''
  fi
  if [[ -n "$route_source" && "$route_source" != '127.'* ]]; then
    echo "Host non-loopback-address HTTP probe (local-only; not a remote LAN test): http://$route_source:$host_port$path"
    if curl --noproxy '*' --http1.1 --fail --connect-timeout 2 --max-time 5 \
        --silent --show-error --output /dev/null \
        --write-out 'http_status=%{http_code} remote_ip=%{remote_ip}\n' \
        "http://$route_source:$host_port$path"; then
      echo 'Non-loopback host-address probe succeeded; the published API may be reachable beyond loopback.'
      status=1
    else
      echo 'Non-loopback host-address probe did not connect from this host; verify from a separate LAN device before claiming LAN isolation.'
    fi
  else
    echo 'Non-loopback host-address probe unavailable (no source address from the default IPv4 route). Verify from a separate LAN device.'
  fi

  if [[ "$probe_egress" -eq 1 ]]; then
    section "$service publication-bridge egress probe (TCP 1.1.1.1:443)"
    egress_probe_output="$(docker exec "$container" sh -c 'socat -T3 -u /dev/null TCP:1.1.1.1:443,connect-timeout=3' 2>&1)"
    egress_probe_status=$?
    if [[ "$egress_probe_status" -eq 0 ]]; then
      echo 'Egress probe: TCP connection succeeded; publication-bridge egress is present.'
      status=1
    elif [[ "$egress_probe_output" == *'executable file not found'* || "$egress_probe_output" == *'not found'* ]]; then
      echo 'Egress probe unavailable: socat could not be executed in the forwarder.'
      [[ -n "$egress_probe_output" ]] && printf '%s\n' "$egress_probe_output"
      status=1
    else
      printf 'Egress probe: TCP connection was not established (exit=%s); this single destination/protocol check does not prove all egress is blocked.\n' "$egress_probe_status"
    fi
  else
    echo 'Egress probe not run; pass --probe-egress to attempt the fixed TCP connectivity check.'
  fi

  if [[ "$direct_container_ok" -eq 1 && "$loopback_ok" -eq 0 ]]; then
    echo 'Finding: the forwarder responds on its container IP, but the host-published loopback port is unavailable; investigate Docker port publication, not the model backend.'
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
if [[ "$use_sudo" -eq 1 ]]; then
  if sudo_context="$(sudo -n python3 -c 'import os; cap=int(next(line.split()[1] for line in open("/proc/self/status") if line.startswith("CapEff:")), 16); print(f"effective_uid={os.geteuid()} cap_net_admin={bool(cap & (1 << 12))} CapEff={cap:x}")' 2>&1)"; then
    printf 'Non-interactive sudo context: %s\n' "${sudo_context//$'\n'/; }"
  else
    echo 'Non-interactive sudo context unavailable:'
    printf '%s\n' "$sudo_context"
  fi
fi

if command -v iptables-save >/dev/null 2>&1; then
  nat_rules="$(run_rule_probe iptables-save -t nat 2>&1)"
  if [[ "$nat_rules" == *'Permission denied'* || "$nat_rules" == *'Operation not permitted'* || "$nat_rules" == *'a password is required'* || "$nat_rules" == *'not allowed to execute'* ]]; then
    if [[ "$use_sudo" -eq 1 ]]; then
      echo 'iptables NAT rules still unavailable under the requested non-interactive sudo probe.'
    else
      echo 'iptables NAT rules unavailable to this user; rerun with --sudo after authorization if appropriate.'
    fi
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
    if [[ "$use_sudo" -eq 1 ]]; then
      echo 'nftables rules still unavailable under the requested non-interactive sudo probe.'
    else
      echo 'nftables rules unavailable to this user; rerun with --sudo after authorization if appropriate.'
    fi
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
  echo 'No failed required probes were observed for the selected running forwarders.'
else
  echo 'One or more Docker, listener, HTTP, or requested isolation checks failed; no repairs were attempted.'
fi
exit "$status"
