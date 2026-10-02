#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$(realpath "$0")")"
export LAB_YES=1
WS="$PWD/workspaces/.verify"; mkdir -p "$WS"
pass=0; fail=0

# check <name> <0=should succeed | 1=should fail> <shell command run inside container>
check() {
  local got=1
  ./lab exec "$WS" "$3" >/dev/null 2>&1 && got=0
  if [[ $got -eq $2 ]]; then echo "PASS  $1"; ((pass++)); else echo "FAIL  $1"; ((fail++)); fi
}

check "runs as non-root"           0 '[ "$(id -u)" != 0 ]'
check "root fs is read-only"       1 'touch /usr/x'
check "workspace is writable"      0 'touch /workspace/.t && rm /workspace/.t'
check "no internet (https)"        1 'curl -sS -m 4 https://example.com'
check "no internet (raw IP)"       1 'curl -sS -m 4 http://1.1.1.1'
check "ollama reachable"           0 'curl -sf -m 5 http://ollama:11434/api/tags'
check "zero capabilities"          0 "grep -q '^CapEff:[[:space:]]*0000000000000000' /proc/self/status"
check "no-new-privileges set"      0 "grep -q '^NoNewPrivs:[[:space:]]*1' /proc/self/status"
check "no docker socket"           0 '[ ! -e /var/run/docker.sock ]'
check "no host ssh keys visible"   0 "[ ! -e /home/$USER/.ssh ] && [ ! -e /root/.ssh ]"
check "no other home dirs"         0 '[ -z "$(ls -A /home | grep -v "^agent$")" ]'

if ss -tln | grep -q ':11434\b'; then
  echo "FAIL  something on the host is listening on 11434"; ((fail++))
else
  echo "PASS  nothing on host listens on 11434"; ((pass++))
fi

echo "---- $pass passed, $fail failed"
[[ $fail -eq 0 ]]