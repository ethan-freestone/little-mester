#!/usr/bin/env bash
# bench/tune.sh -- parameter sweep using llama-bench from the full-cuda image.
#
# One llama-bench invocation sweeps every combination (comma lists), writes
# JSONL as each test completes (so an OOM at a low --n-cpu-moe keeps the earlier
# results), then summarises with jq. No binary extraction, no model copying.
set -euo pipefail

LAB_DIR="$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")/.." && pwd)"
export WORKSPACE="${WORKSPACE:-$LAB_DIR}"   # compose interpolates the whole file

IMAGE="${BENCH_IMAGE:-ghcr.io/ggml-org/llama.cpp:full-cuda}"
VOLUME="${MODELS_VOLUME:-little-mester_llamacpp-models}"

THREADS="8,10"
UB="2048"
MOE="36,32,28,24"       # safest first: if the lowest OOMs, earlier results survive
DEPTH="0,16384"         # 0 = best case; 16384 = closer to a real agent session
REPS=2
PP=512
TG=128
JSON=0
ANALYSE=0
VERBOSE="${VERBOSE:-0}"
MODEL_PATH=""

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo ">> $*" >&2; }

usage() {
  cat <<'EOF'
Usage: ./lab tune [OPTIONS]

  -t N,N        Thread counts           (default: 8,10)
  -ub N,N       Micro-batch sizes       (default: 2048)
  --moe N,N     --n-cpu-moe values      (default: 36,32,28,24)
  --depth N,N   Context depths to test  (default: 0,16384)
  -r N          Repetitions per test    (default: 2)
  --model NAME  Model name without .gguf (default: MODEL_FILE in compose.yaml)
  --json        Print result JSON on stdout (table goes to stderr)
  --analyse     Ask the local llama.cpp model to interpret the results
  --verbose     Show llama-bench stderr
EOF
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -t)            THREADS="${2:?}"; shift 2 ;;
    -ub)           UB="${2:?}"; shift 2 ;;
    --moe|--n-cpu-moe) MOE="${2:?}"; shift 2 ;;
    --depth)       DEPTH="${2:?}"; shift 2 ;;
    -r|--repeats)  REPS="${2:?}"; shift 2 ;;
    --model)       MODEL_PATH="/models/${2:?}.gguf"; shift 2 ;;
    --json)        JSON=1; shift ;;
    --analyse|--analyze|--ai) ANALYSE=1; shift ;;
    --verbose)     VERBOSE=1; shift ;;
    -h|--help)     usage ;;
    *) die "unknown flag: $1" ;;
  esac
done

command -v docker >/dev/null || die "docker not found"
command -v jq >/dev/null     || die "jq not found (sudo pacman -S jq)"
docker volume inspect "$VOLUME" >/dev/null 2>&1 \
  || die "volume '$VOLUME' not found. Check: docker volume ls | grep llamacpp"

if [[ -z "$MODEL_PATH" ]]; then
  MODEL_PATH="$(grep -oP 'MODEL_FILE:\s*"\K[^"]+' "$LAB_DIR/compose.yaml" | head -1 || true)"
  [[ -n "$MODEL_PATH" ]] || die "could not read MODEL_FILE from compose.yaml; use --model NAME"
fi

# Largest micro-batch must fit in -b, or llama-bench silently clamps it.
BATCH="$(tr ',' '\n' <<<"$UB" | sort -n | tail -1)"
(( BATCH < 2048 )) && BATCH=2048

count() { tr ',' '\n' <<<"$1" | grep -c .; }
N_CFG=$(( $(count "$THREADS") * $(count "$UB") * $(count "$MOE") * $(count "$DEPTH") ))

echo "============================================================" >&2
echo " Tuning sweep (llama-bench, $IMAGE)" >&2
echo "  Model      : $MODEL_PATH" >&2
echo "  Threads    : $THREADS" >&2
echo "  Micro-batch: $UB" >&2
echo "  CPU-MoE    : $MOE" >&2
echo "  Depth      : $DEPTH" >&2
echo "  Configs    : $N_CFG (x2 tests x $REPS reps)" >&2
echo "============================================================" >&2

# The server holds the model in VRAM; the benchmark needs that VRAM.
log "Stopping llamacpp server to free VRAM..."
docker compose -f "$LAB_DIR/compose.yaml" --project-directory "$LAB_DIR" \
  stop llamacpp >&2 2>&1 || true

log "Checking model exists in volume..."
docker run --rm --entrypoint test -v "$VOLUME":/models:ro "$IMAGE" -f "$MODEL_PATH" \
  || die "$MODEL_PATH not found in volume $VOLUME (run ./lab pull?)"

OUT_DIR="$LAB_DIR/.tune-results"
mkdir -p "$OUT_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
RAW="$OUT_DIR/raw-$STAMP.jsonl"
ERR="$OUT_DIR/stderr-$STAMP.log"
RESULT="$OUT_DIR/tune-$STAMP.json"

log "Running llama-bench (this takes a few minutes)..."
set +e
docker run --rm --gpus all --entrypoint /app/llama-bench \
  -v "$VOLUME":/models:ro "$IMAGE" \
  -m "$MODEL_PATH" \
  -ngl 99 --n-cpu-moe "$MOE" -t "$THREADS" -b "$BATCH" -ub "$UB" \
  -fa 1 -ctk q8_0 -ctv q8_0 \
  -p "$PP" -n "$TG" -d "$DEPTH" -r "$REPS" -o jsonl \
  >"$RAW" 2>"$ERR"
RC=$?
set -e

[[ "$VERBOSE" == "1" ]] && sed 's/^/  [llama-bench] /' "$ERR" >&2

if ! jq -e 'select(.n_gen > 0)' "$RAW" >/dev/null 2>&1; then
  echo "---- llama-bench stderr (tail) ----" >&2
  tail -n 25 "$ERR" >&2
  die "no generation results produced (exit $RC). Full log: $ERR"
fi
if (( RC != 0 )); then
  echo "WARNING: llama-bench exited $RC; keeping partial results." >&2
  echo "         Likely OOM at the lowest --moe. Last stderr lines:" >&2
  tail -n 5 "$ERR" | sed 's/^/         /' >&2
fi

jq -s --arg model "$MODEL_PATH" --arg ts "$STAMP" '
  def r: if . == null then null else (. * 10 | round / 10) end;
  { mode: "tune", model: $model, timestamp: $ts,
    results: (
      map({t: .n_threads, ub: .n_ubatch, moe: .n_cpu_moe, d: .n_depth,
           kind: (if .n_gen > 0 then "tg" else "pp" end), ts: .avg_ts})
      | group_by([.t, .ub, .moe, .d])
      | map({
          config: {threads: .[0].t, micro_batch: .[0].ub,
                   n_cpu_moe: .[0].moe, depth: .[0].d},
          pp_tps: ((map(select(.kind == "pp").ts)[0]) | r),
          tg_tps: ((map(select(.kind == "tg").ts)[0]) | r)
        })
    ) }' "$RAW" > "$RESULT"

echo "" >&2
echo "============================================================" >&2
echo " RESULTS  (pp = prompt t/s, tg = decode t/s)" >&2
echo "============================================================" >&2
jq -r '["threads","ub","moe","depth","pp_t/s","tg_t/s"],
       (.results[] | [.config.threads, .config.micro_batch, .config.n_cpu_moe,
                      .config.depth, .pp_tps, .tg_tps]) | @tsv' "$RESULT" \
  | column -t -s$'\t' >&2
echo "" >&2
jq -r '(.results | map(.config.depth) | max) as $d
       | [.results[] | select(.config.depth == $d and .tg_tps != null)]
       | max_by(.tg_tps)
       | "Best at depth \($d): threads=\(.config.threads) ub=\(.config.micro_batch) moe=\(.config.n_cpu_moe) -> \(.tg_tps) t/s decode"' \
  "$RESULT" >&2
echo "" >&2
echo "Saved: $RESULT" >&2
echo "NOTE: VRAM use is NOT measured here. Validate the winner with ./lab restart-llama" >&2
echo "      and watch nvidia-smi at full -c 65536 before committing it to compose.yaml." >&2

[[ "$JSON" == "1" ]] && cat "$RESULT"

if [[ "$ANALYSE" == "1" ]]; then
  echo "" >&2
  "$LAB_DIR/bench/ai-analyze.sh" "$RESULT"
fi