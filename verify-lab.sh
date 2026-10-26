#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$(realpath "$0")")"
export LAB_YES=1
WS="$PWD/workspaces/.verify"; mkdir -p "$WS"
pass=0; fail=0

echo "=== Documentation Drift Checks ==="
if python3 scripts/check-docs-consistency.py; then
  echo "PASS  docs match compose defaults and local links resolve"; ((pass++))
else
  echo "FAIL  docs drift from compose defaults or a local link is broken"; ((fail++))
fi
echo ""

# check <name> <0=should succeed | 1=should fail> <shell command run inside container>
check() {
  local got=1
  ./lab exec "$WS" "$3" >/dev/null 2>&1 && got=0
  if [[ $got -eq $2 ]]; then echo "PASS  $1"; ((pass++)); else echo "FAIL  $1"; ((fail++)); fi
}

echo "=== Sandbox Security Checks ==="
check "runs as non-root"           0 '[ "$(id -u)" != 0 ]'
check "root fs is read-only"       1 'touch /usr/x'
check "workspace is writable"      0 'touch /workspace/.t && rm /workspace/.t'
# These checks only apply when the agent is isolated (EGRESS=false).
# They are skipped when egress is enabled (the default).
if [[ "${EGRESS:-true}" != "true" ]]; then
  check "no internet (https)"        1 'curl -sS -m 4 https://example.com'
  check "no internet (raw IP)"       1 'curl -sS -m 4 http://1.1.1.1'
fi
check "zero capabilities"          0 "grep -q '^CapEff:[[:space:]]*0000000000000000' /proc/self/status"
check "no-new-privileges set"      0 "grep -q '^NoNewPrivs:[[:space:]]*1' /proc/self/status"
check "no docker socket"           0 '[ ! -e /var/run/docker.sock ]'
check "no host ssh keys visible"   0 "[ ! -e /home/\$USER/.ssh ] && [ ! -e /root/.ssh ]"
check "no other home dirs"         0 '[ -z "$(ls -A /home | grep -v "^agent$")" ]'

echo ""
echo "=== Model Server Connectivity (from agent container) ==="
check "ollama reachable"           0 'curl -sf -m 5 http://ollama:11434/api/tags'
check "llamacpp reachable"         0 'curl -sf -m 5 http://llamacpp:11434/health'

echo ""
echo "=== Host Port Isolation ==="
if ss -tln | grep -q ':11434\b'; then
  echo "FAIL  something on the host is listening on 11434"; ((fail++))
else
  echo "PASS  nothing on host listens on 11434"; ((pass++))
fi

echo ""
echo "=== Metrics Sidecar Checks (from agent container) ==="
# Metrics sidecar reachable
check "metrics sidecar reachable"  0 'curl -sf -m 3 http://metrics:19532/summary'

# Metrics returns valid JSON structure
check "metrics JSON valid"         0 'curl -sf -m 3 http://metrics:19532/metrics | python3 -c "import sys,json; json.load(sys.stdin)"'

# Metrics contains GPU data
check "metrics has GPU data"       0 'curl -sf -m 3 http://metrics:19532/metrics | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get(\"gpu\") is not None"'

# Metrics contains CPU data
check "metrics has CPU data"       0 'curl -sf -m 3 http://metrics:19532/metrics | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get(\"cpu\") is not None"'

# Metrics contains RAM data
check "metrics has RAM data"       0 'curl -sf -m 3 http://metrics:19532/metrics | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get(\"memory\") is not None"'

# Metrics summary is human-readable
check "metrics summary readable"   0 'curl -sf -m 3 http://metrics:19532/summary | grep -q "VRAM"'

# get_host_metrics tool works (via opencode plugin)
check "get_host_metrics tool"      0 'python3 -c "
import sys, json, urllib.request
try:
    # This simulates what the host-metrics plugin does
    resp = urllib.request.urlopen(\"http://metrics:19532/summary\", timeout=3)
    data = resp.read().decode()
    assert \"VRAM\" in data or \"GPU\" in data
    print(data)
except Exception as e:
    print(f\"FAIL: {e}\", file=sys.stderr)
    sys.exit(1)
"'

echo ""
echo "---- $pass passed, $fail failed"
[[ $fail -eq 0 ]]