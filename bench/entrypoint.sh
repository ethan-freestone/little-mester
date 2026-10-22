#!/usr/bin/env bash
# bench/entrypoint.sh — decides native vs HTTP benchmark mode
#
# Native mode (fast, in-container): MODEL_DIR must be set, runs llama.cpp --test
# HTTP mode (against running server): MODEL_DIR unset, uses bench.py

set -euo pipefail

if [[ -n "${MODEL_DIR:-}" ]]; then
  # ── Native mode: run llama-bench inside the model container ──────────────
  # Benefits: zero HTTP overhead, instant between-test switches, same process
  # Cost: blocks the container (can't serve requests during benchmark)

  MODEL="${MODEL_FILE:-/models/qwen3.6-35b-a3b-q4_k_m.gguf}"
  N_GPU_LAYERS="${NGPU_LAYERS:-99}"
  N_CPU_MOE="${NCPU_MOE:-32}"
  THREADS="${THREADS:-8}"
  MICRO_BATCH="${MICRO_BATCH:-2048}"
  CONTEXT="${CONTEXT:-65536}"
  K_TYPE="${K_TYPE:-q8_0}"
  V_TYPE="${V_TYPE:-q8_0}"
  N_REPEATS="${N_REPEATS:-5}"
  N_PROMPTS="${N_PROMPTS:-4}"
  OUTPUT_JSON="${BENCH_JSON:-0}"

  echo "============================================================"
  echo " llama.cpp Native Benchmark"
  echo "  Model     : $MODEL"
  echo "  Threads   : $THREADS"
  echo "  CPU-MOE   : $N_CPU_MOE"
  echo "  Micro-bat : $MICRO_BATCH"
  echo "  Context   : $CONTEXT"
  echo "  KV K/V    : $K_TYPE / $V_TYPE"
  echo "  GPU layers: $N_GPU_LAYERS"
  echo "  Repeats   : $N_REPEATS"
  echo "============================================================"

  # Run llama-bench (dedicated benchmarking binary shipped with server-cuda)
  BENCH_JSON_RAW=$(llama-bench \
    -m "$MODEL" \
    -t "$THREADS" \
    --n-gpu-layers "$N_GPU_LAYERS" \
    --n-cpu-moe "$N_CPU_MOE" \
    --ctx-size "$CONTEXT" \
    --cache-type-k "$K_TYPE" \
    --cache-type-v "$V_TYPE" \
    -ub "$MICRO_BATCH" \
    --n-prompt "$N_PROMPTS" \
    --repeat "$N_REPEATS" \
    --json 2>&1) || true

  echo ""
  echo "--- Raw JSON Output ---"
  echo "$BENCH_JSON_RAW"
  echo "--- End Raw ---"
  echo ""

  # Parse JSON output from llama-bench
  TPS=""
  PROMPT_TPS=""
  AVG_LATENCY=""

  # Try jq first, fall back to grep
  if command -v jq >/dev/null 2>&1; then
    TPS=$(echo "$BENCH_JSON_RAW" | jq -r '.result.eval.avg // empty' 2>/dev/null || true)
    PROMPT_TPS=$(echo "$BENCH_JSON_RAW" | jq -r '.result.prompt_eval.avg // empty' 2>/dev/null || true)
    AVG_LATENCY=$(echo "$BENCH_JSON_RAW" | jq -r '.result.eval.latency_avg_ms // empty' 2>/dev/null || true)
  else
    # Fallback: parse with grep/sed
    TPS=$(echo "$BENCH_JSON_RAW" | grep -oP '"eval".*"avg"\s*:\s*\K[\d.]+' | head -1 || true)
    PROMPT_TPS=$(echo "$BENCH_JSON_RAW" | grep -oP '"prompt_eval".*"avg"\s*:\s*\K[\d.]+' | head -1 || true)
  fi

  echo "  ██████  INFERENCE THROUGHPUT: ${TPS:-N/A} tokens/sec"
  [[ -n "$PROMPT_TPS" ]] && echo "  Prompt eval:     ${PROMPT_TPS} tokens/sec"
  [[ -n "$AVG_LATENCY" ]] && echo "  Gen latency:     ${AVG_LATENCY} ms avg"

  if [[ "$OUTPUT_JSON" == "1" ]]; then
    echo ""
    # Output JSON summary (parsable by AI)
    echo "{"
    echo "  \"mode\": \"native\","
    echo "  \"config\": {"
    echo "    \"threads\": $THREADS,"
    echo "    \"n_cpu_moe\": $N_CPU_MOE,"
    echo "    \"micro_batch\": $MICRO_BATCH,"
    echo "    \"context\": $CONTEXT,"
    echo "    \"gpu_layers\": $N_GPU_LAYERS"
    echo "  },"
    echo "  \"results\": {"
    if [[ -n "$TPS" ]]; then
      echo "    \"inference_tps\": $TPS"
    fi
    echo "  }"
    echo "}"
  fi

else
  # ── HTTP mode: run bench.py against running server ────────────────────────
  exec python3 /bench/bench.py
fi
