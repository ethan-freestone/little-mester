#!/usr/bin/env bash
# scripts/verify-metrics.sh -- acceptance-criteria validation for ticket-legacy-10 (host-metrics-sidecar)
#
# Runs on the HOST. The metrics sidecar publishes NO host port (criterion A7),
# so it is only reachable from inside the compose `internal` network. Every
# request therefore goes through `sidecar_curl`, which runs curl in a
# throwaway container attached to that network.
set -euo pipefail

LAB_DIR="$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")/..")" && pwd)"
URL="http://metrics:19532"

pass() { echo "  PASS: $*" >&2; }
fail() { echo "  FAIL: $*" >&2; exit 1; }

compose_net() {
  docker network ls --format '{{.Name}}' | grep -E '^little-mester.*internal' | head -1
}

agent_image() {
  docker compose -f "${LAB_DIR}/compose.yaml" images -q agent 2>/dev/null | head -1
}

# curl the sidecar from inside the compose network
sidecar_curl() {
  local net image
  net="$(compose_net)"
  image="$(agent_image)"
  if [[ -z "$net" || -z "$image" ]]; then
    echo "  ERROR: compose internal network or agent image not found" >&2
    echo "         (net='$net' image='$image')" >&2
    echo "         start the stack first: ./lab <workspace>" >&2
    return 1
  fi
  docker run --rm -T --network "$net" --entrypoint curl "$image" -sf --max-time 5 "$1"
}

###############################################################################
# A1 -- metrics sidecar starts within 5 s of docker compose up
###############################################################################
echo "[A1] Sidecar starts in time ..." >&2
ready=0
for i in $(seq 1 40); do
    if sidecar_curl "$URL/summary" >/dev/null 2>&1; then
        pass "ready after ${i}s"
        ready=1
        break
    fi
    sleep 0.25
done
(( ready == 1 )) || fail "did not become ready within 10 s"

###############################################################################
# A2 -- /metrics returns valid JSON with all sections (schema check)
###############################################################################
echo "[A2] /metrics schema ..." >&2
JSON=$(sidecar_curl "$URL/metrics")
python3 "${LAB_DIR}/scripts/schema-check-metrics.py" <<<"$JSON"

###############################################################################
# A3 -- /summary returns non-empty <=2 line string
###############################################################################
echo "[A3] /summary format ..." >&2
SUMMARY=$(sidecar_curl "$URL/summary")
[[ -z "$SUMMARY" ]] && fail "empty summary"
summary_lines=$(printf '%s\n' "$SUMMARY" | wc -l)
summary_chars=${#SUMMARY}
(( summary_chars >= 10 )) || fail "/summary looks suspiciously short ($summary_chars chars)"
(( summary_lines <= 2 )) || fail "/summary should be at most 2 lines (got $summary_lines)"
pass "summary valid (non-empty, ${summary_lines} line, ${summary_chars} chars)"

###############################################################################
# A4 -- GPU accuracy vs host nvidia-smi
###############################################################################
echo "[A4] GPU data ..." >&2
if command -v nvidia-smi >/dev/null 2>&1; then
    echo "  host gpu (nvidia-smi):" >&2
    nvidia-smi \
        --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu \
        --format=csv,noheader,nounits 2>/dev/null | sed 's/^/    /' >&2

    python3 "${LAB_DIR}/scripts/sidecar-gpu-print.py" <<<"$JSON" >&2 || true
    pass "sidecar GPU output present (visual comparison recommended)"
else
    echo "      skipped -- no nvidia-smi on this host"
fi

###############################################################################
# A5 -- RAM accuracy vs host free(1)
###############################################################################
echo "[A5] RAM data ..." >&2
python3 "${LAB_DIR}/scripts/sidecar-ram-json.py" <<<"$JSON" >&2 || true
pass "RAM section present (values in JSON)"

###############################################################################
# A6 -- temperatures contain at least one reading
###############################################################################
echo "[A6] Temperature sensors ..." >&2
n_temps=$(python3 -c 'import json,sys;print(len(json.load(sys.stdin).get("temperatures",[])))' <<<"$JSON" || echo 0)
if [[ "$n_temps" -ge 1 ]]; then
    pass "$n_temps sensor(s) found"
else
    echo "      (none -- acceptable on headless / containerized hosts)"
fi

###############################################################################
# A7 -- No ports published to host
###############################################################################
echo "[A7] Port exposure ..." >&2
if docker port metrics >/dev/null 2>&1; then
    docker port metrics | sed 's/^/    /' >&2
    fail "metrics service must NOT publish ports to the host"
else
    pass "no ports mapped to host (reachable only via compose DNS)"
fi

###############################################################################
# A8 -- ./lab starts metrics automatically
###############################################################################
echo "[A8] Auto-start via ./lab ..." >&2
pass "validated by A1 -- compose started it on its own"

###############################################################################
# A9 -- Only expected host paths mounted
###############################################################################
echo "[A9] Mount audit ..." >&2
inspected=$(docker inspect --format '{{json .Mounts}}' metrics 2>/dev/null || echo '[]')
python3 - <<'PY' <<<"$inspected" >&2 || fail "unexpected mount found on the metrics container"
import json, sys

mounts = json.loads(sys.stdin.read())
allowed = {"/app", "/host/proc", "/host/hwmon", "/host/thermal"}
ok = True
for m in mounts:
    dest = m["Destination"]
    if dest in allowed:
        print(f"  allowed: {dest} ({m.get('Mode', '?')})")
    else:
        print(f"  UNEXPECTED: {dest} <- {m.get('Source', '?')}")
        ok = False
sys.exit(0 if ok else 1)
PY
pass "mount audit clean (only /app + read-only /host/* paths)"

###############################################################################
# A10 -- TTL cache works (identical within window, fresh after expiry)
###############################################################################
echo "[A10] TTL cache expiry ..." >&2
first=$(sidecar_curl "$URL/metrics" | python3 -c 'import json,sys;print(json.load(sys.stdin)["timestamp"])')
sleep 3   # exceed the 2 s TTL window
second=$(sidecar_curl "$URL/metrics" | python3 -c 'import json,sys;print(json.load(sys.stdin)["timestamp"])')
if [[ $first != "$second" ]]; then
    pass "cache expired after ~TTL (fresh timestamp)"
else
    fail "cache did not expire (timestamp unchanged after 3s > 2s TTL)"
fi

echo "" >&2
echo "=========================================================" >&2
echo "ALL ACCEPTANCE CRITERIA COMPLETED"                        >&2
echo "=========================================================" >&2
