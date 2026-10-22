#!/usr/bin/env bash
# bench/entrypoint.sh — decides native vs HTTP benchmark mode
#
# Native mode (fast, in-container): MODEL_DIR must be set, runs llama.cpp --test
# HTTP mode (against running server): MODEL_DIR unset, uses bench.py

set -euo pipefail

if [[ -n "${MODEL_DIR:-}" ]]; then
  # ── Native mode: run llama.cpp --test inside the model container ──────────
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

  # Run llama.cpp --test (this is the built-in benchmark mode)
  # We capture both stdout and stderr, parse the timing info
  OUTPUT=$(llama.cpp \
    -m "$MODEL" \
    --threads "$THREADS" \
    --threads-batch "$THREADS" \
    --n-gpu-layers "$N_GPU_LAYERS" \
    --n-cpu-moe "$N_CPU_MOE" \
    --ctx-size "$CONTEXT" \
    --cache-type-k "$K_TYPE" \
    --cache-type-v "$V_TYPE" \
    --mlock \
    --log-disable \
    --no-warmup \
    -ngl "$N_GPU_LAYERS" \
    -n "$N_PROMPTS" \
    --repeat-penalty 1.1 \
    -ub "$MICRO_BATCH" \
    2>&1) || true

  # Parse llama.cpp --test output for timing stats
  # The output contains lines like:
  #   "prompt eval time =    xx.x ms / N tokens"
  #   "        eval time =    xx.x ms / N tokens"
  #   "         total time =    xx.x s"
  #   "benchmark (inference): request sent, waiting for completion..."
  #   "benchmark (inference): xx.xx tokens per second"

  # Extract key metrics
  echo ""
  echo "--- Raw Output ---"
  echo "$OUTPUT"
  echo "--- End Raw ---"
  echo ""

  # Try to parse structured info
  parse_and_report() {
    local output="$1"

    # Total inference tokens per second (the key metric)
    local tps
    tps=$(echo "$output" | grep -oP 'benchmark \(inference\):\s*[\d.]+\s*tokens per second' | grep -oP '[\d.]+' | head -1 || echo "")

    if [[ -n "$tps" ]]; then
      echo "  ██████  INFERENCE THROUGHPUT: ${tps} tokens/sec"
    fi

    # Prompt eval time
    local prompt_ms
    prompt_ms=$(echo "$output" | grep 'prompt eval time' | tail -1 | grep -oP '[\d.]+\s*ms' || echo "")
    if [[ -n "$prompt_ms" ]]; then
      echo "  Prompt eval: $prompt_ms"
    fi

    # Eval time
    local eval_ms
    eval_ms=$(echo "$output" | grep 'eval time' | tail -1 | grep -oP '[\d.]+\s*ms' || echo "")
    if [[ -n "$eval_ms" ]]; then
      echo "  Decode:      $eval_ms"
    fi

    # Total time
    local total_s
    total_s=$(echo "$output" | grep 'total time' | tail -1 | grep -oP '[\d.]+\s*s' || echo "")
    if [[ -n "$total_s" ]]; then
      echo "  Total:       $total_s"
    fi
  }

  parse_and_report "$OUTPUT"

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
    if [[ -n "$tps" ]]; then
      echo "    \"inference_tps\": $tps"
    fi
    echo "  }"
    echo "}"
  fi

else
  # ── HTTP mode: run bench.py against running server ────────────────────────
  exec python3 /bench/bench.py
fi
