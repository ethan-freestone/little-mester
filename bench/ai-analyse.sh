#!/usr/bin/env bash
# bench/ai-analyze.sh -- ask the local llama.cpp model to interpret tune results.
#
# Uses the llamacpp container (already in the stack, OpenAI-compatible API) and
# talks to it with `docker exec ... curl`, so no host ports are needed. Starts
# the server if it isn't running and stops it again afterwards.
# Request JSON is built with jq, so quoting/newlines can't corrupt it.
set -euo pipefail

LAB_DIR="$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")/.." && pwd)"
export WORKSPACE="${WORKSPACE:-$LAB_DIR}"

FILE=""
VERBOSE="${VERBOSE:-0}"

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo ">> $*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --verbose) VERBOSE=1; shift ;;
    -h|--help)
      echo "Usage: ./lab ai-analyze [--verbose] [results.json]   (or pipe JSON on stdin)"
      exit 0 ;;
    -*) die "unknown flag: $1" ;;
    *)  FILE="$1"; shift ;;
  esac
done

command -v jq >/dev/null || die "jq not found (sudo pacman -S jq)"

if [[ -n "$FILE" ]]; then
  [[ -f "$FILE" ]] || die "file not found: $FILE"
  RESULTS="$(cat "$FILE")"
elif [[ ! -t 0 ]]; then
  RESULTS="$(cat)"
else
  die "no input. Usage: ./lab ai-analyze results.json"
fi

jq -e '.results | length > 0' <<<"$RESULTS" >/dev/null 2>&1 \
  || die "input is not valid tune JSON (no .results)"
jq -e 'any(.results[]; .tg_tps != null)' <<<"$RESULTS" >/dev/null \
  || die "all results are empty -- nothing to analyse. Fix the benchmark first."

# Hardware context (machine.conf is shell-style KEY="value")
SPECS="(machine.conf not found)"
if [[ -f "$LAB_DIR/machine.conf" ]]; then
  set +u; set -a; . "$LAB_DIR/machine.conf"; set +a; set -u
  SPECS="GPU: ${GPU_NAME:-?} (${GPU_VRAM_GB:-?} GB VRAM); CPU: ${CPU_MODEL:-?}, ${CPU_CORES:-?} cores"
fi

cname() {
  docker ps --filter "name=llamacpp" --filter "status=running" \
    --format '{{.Names}}' | head -1
}

STARTED=0
cleanup() {
  if [[ "$STARTED" == "1" ]]; then
    log "Stopping llamacpp (we started it)..."
    docker compose -f "$LAB_DIR/compose.yaml" --project-directory "$LAB_DIR" \
      stop llamacpp >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

C="$(cname)"
if [[ -z "$C" ]]; then
  log "Starting llamacpp server for analysis..."
  docker compose -f "$LAB_DIR/compose.yaml" --project-directory "$LAB_DIR" \
    up -d llamacpp >&2
  STARTED=1
fi

log "Waiting for llamacpp to be healthy (model load can take a minute)..."
ready=0
for _ in $(seq 1 90); do
  C="$(cname)"
  if [[ -n "$C" ]] && docker exec "$C" curl -sf -m 3 http://127.0.0.1:11434/health >/dev/null 2>&1; then
    ready=1; break
  fi
  sleep 2
done
(( ready == 1 )) || die "llamacpp did not become healthy. Check: docker logs ${C:-llamacpp}"

SYSTEM="$(cat <<'EOF'
You are a llama.cpp performance tuning assistant. You get llama-bench results
for a MoE model with expert offload (--n-cpu-moe) on a laptop with 8 GB VRAM.
Fields: pp_tps = prompt processing t/s, tg_tps = token generation (decode) t/s,
depth = context tokens already in the KV cache when measured.

Rules:
- Use ONLY numbers present in the data. Do not invent measurements.
- Weight results at the largest depth most; depth 0 is a best case.
- VRAM usage was NOT measured. Lower --n-cpu-moe puts more experts on the GPU
  and risks OOM at the real 65536 context. Say so, and recommend the config
  to validate first, not a blind change.
- Differences under ~3% are noise; say so rather than ranking them.

Reply with:
1. BEST CONFIG: threads, micro_batch, n_cpu_moe, with its tg_tps and pp_tps.
2. WHY: two or three sentences referencing actual numbers.
3. TRADE-OFFS / RISKS.
4. NEXT STEPS: the exact flags to change in compose.yaml, then ./lab restart-llama.
EOF
)"

BODY="$(jq -n \
  --arg system "$SYSTEM" \
  --arg user "Machine: $SPECS

Benchmark results:
$RESULTS" \
  '{model: "llamacpp-main",
    messages: [{role: "system", content: $system}, {role: "user", content: $user}],
    temperature: 0.2,
    max_tokens: 1500,
    chat_template_kwargs: {enable_thinking: false},
    stream: false}')"

[[ "$VERBOSE" == "1" ]] && { echo "[verbose] request body:" >&2; echo "$BODY" | head -c 1500 >&2; echo >&2; }

log "Asking the model for analysis..."
RESP="$(docker exec -i "$C" curl -sf -m 600 -X POST \
  http://127.0.0.1:11434/v1/chat/completions \
  -H 'Content-Type: application/json' -d @- <<<"$BODY")" \
  || die "request to llamacpp failed. Check: docker logs $C"

CONTENT="$(jq -r '.choices[0].message.content // empty' <<<"$RESP")"
if [[ -z "$CONTENT" ]]; then
  echo "Unexpected response:" >&2
  echo "$RESP" | head -c 1500 >&2
  die "model returned no content"
fi

echo ""
echo "============================================================"
echo " AI ANALYSIS"
echo "============================================================"
echo "$CONTENT"
echo ""