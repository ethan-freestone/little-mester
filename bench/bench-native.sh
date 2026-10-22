#!/usr/bin/env bash
# bench-native.sh — llama.cpp native benchmark (runs on the host)
#
# Lifecycle:
#   1. Start llamacpp container (if needed and model not on host)
#   2. Copy model from container to host
#   3. Extract llama-bench binary from Docker image
#   4. Run native benchmark with llama-bench
#   5. Stop llamacpp container
#
# Usage:
#   ./bench/bench-native.sh [-t THREADS] [-ub MICRO_BATCH] [-moe NCPU_MOE] ...
#   ./bench/bench-native.sh --json -t 8 -ub 1024

set -uo pipefail

# ── Verbose mode ──────────────────────────────────────────────────────────────
VERBOSE=0
vlog() { [[ "$VERBOSE" == "1" ]] && echo "[verbose] $*" >&2 || true; }
vlog2() [[ "$VERBOSE" == "1" ]] && echo "[verbose]   $*" >&2 || true;

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAB_COMPOSE="$LAB_DIR/compose.yaml"
export WORKSPACE="${WORKSPACE:-$LAB_DIR}"

# ── Defaults ─────────────────────────────────────────────────────────────────
THREADS="${THREADS:-8}"
MICRO_BATCH="${MICRO_BATCH:-2048}"
NCPU_MOE="${NCPU_MOE:-32}"
NGPU_LAYERS="${NGPU_LAYERS:-99}"
CONTEXT="${CONTEXT:-65536}"
K_TYPE="${K_TYPE:-q8_0}"
V_TYPE="${V_TYPE:-q8_0}"
N_REPEATS="${N_REPEATS:-3}"
N_PROMPTS="${N_PROMPTS:-4}"
OUTPUT_JSON="${BENCH_JSON:-0}"

# Host-side model cache
MODEL_HOST_DIR="$LAB_DIR/.bench-models"
MODEL_FILE="${MODEL_FILE:-}"

# llama-bench extraction
LLAMA_BENCH="$LAB_DIR/.llama-bench"
LLAMA_BENCH_IMAGE="ghcr.io/ggml-org/llama.cpp:server-cuda"

# Container management
CONTAINER_STARTED=0  # Track if we started the container
KEEP_CONTAINER=0     # If 1, skip container start/stop (managed by caller)

# ── Parse args ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    -t)            THREADS="$2"; shift 2 ;;
    -ub)           MICRO_BATCH="$2"; shift 2 ;;
    --moe|-moe)    NCPU_MOE="$2"; shift 2 ;;
    -ngl|--ngpu)   NGPU_LAYERS="$2"; shift 2 ;;
    -c|--ctx)      CONTEXT="$2"; shift 2 ;;
    -nr|--repeats) N_REPEATS="$2"; shift 2 ;;
    -np|--prompts) N_PROMPTS="$2"; shift 2 ;;
    --keep-running)  KEEP_CONTAINER=1; shift ;;
    --json)          OUTPUT_JSON=1; shift ;;
    --verbose)       VERBOSE=1; shift ;;
    --help|-h)
      cat <<'HELPTEXT'
Usage: ./bench/bench-native.sh [OPTIONS]

Native llama.cpp benchmark — runs on the host using llama-bench.

Options:
  -t N              Threads (default: 8)
  -ub N             Micro-batch size (default: 2048)
  --moe N           CPU-MoE experts (default: 32)
  -ngl N            GPU layers (default: 99)
  -c N              Context length (default: 65536)
  --repeats N       Number of repetitions (default: 3)
  --keep-running    Don't start/stop llamacpp (caller manages container)
  --json            JSON output to stdout
  --verbose         Print detailed debug output to stderr
  --help            Show this help
HELPTEXT
      exit 0
      ;;
    *) echo "Unknown flag: $1" >&2; exit 1 ;;
  esac
done

# ── Cleanup handler ──────────────────────────────────────────────────────────
cleanup() {
  echo ""
  echo ">> Stopping llamacpp container (if started)..."
  if [[ $KEEP_CONTAINER -eq 0 ]] && [[ $CONTAINER_STARTED -eq 1 ]]; then
    docker compose -f "$LAB_COMPOSE" --project-directory "$LAB_DIR" stop llamacpp 2>/dev/null || true
  fi
  # Remove temp model cache (unless keep-running mode)
  if [[ $KEEP_CONTAINER -eq 0 ]]; then
    rm -rf "$MODEL_HOST_DIR" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# ── Locate model ─────────────────────────────────────────────────────────────
if [[ -z "$MODEL_FILE" ]]; then
  vlog "Searching for model files..."
  # Find the first GGUF file in MODEL_HOST_DIR
  if [[ -d "$MODEL_HOST_DIR" ]]; then
    MODEL_FILE=$(find "$MODEL_HOST_DIR" -maxdepth 1 -name "*.gguf" -type f | head -1) || true
    vlog "  Checked $MODEL_HOST_DIR → $( [[ -n "$MODEL_FILE" ]] && echo "found: $(basename "$MODEL_FILE")" || echo "nothing" )"
  else
    vlog "  $MODEL_HOST_DIR does not exist"
  fi
  # Fallback: check LAB_DIR/models
  if [[ -z "$MODEL_FILE" ]] && [[ -d "$LAB_DIR/models" ]]; then
    MODEL_FILE=$(find "$LAB_DIR/models" -maxdepth 1 -name "*.gguf" -type f | head -1) || true
    vlog "  Checked $LAB_DIR/models → $( [[ -n "$MODEL_FILE" ]] && echo "found: $(basename "$MODEL_FILE")" || echo "nothing" )"
  else
    vlog "  $LAB_DIR/models does not exist"
  fi
fi

if [[ -n "$MODEL_FILE" ]] && [[ -f "$MODEL_FILE" ]]; then
  vlog "Model already on host: $MODEL_FILE"
fi

# ── Get model from container if needed ───────────────────────────────────────
if [[ -z "$MODEL_FILE" ]] || [[ ! -f "$MODEL_FILE" ]]; then

  echo "  Model not on host — extracting from container..."
  vlog "MODEL_FILE is empty or not a file: '${MODEL_FILE:-<empty>}'"

  # Ensure model cache dir exists
  mkdir -p "$MODEL_HOST_DIR"
  vlog "Model cache directory: $MODEL_HOST_DIR"

  if [[ $KEEP_CONTAINER -eq 1 ]]; then
    # Caller manages the container — verify it's running, then copy the model
    echo "  --keep-running mode: container should be managed externally."
    vlog "KEEP_CONTAINER=1 — will not start/stop container"

    # Resolve container name using docker ps directly (docker compose ps fails without WORKSPACE)
    CONTAINER_NAME=$(docker ps --filter "name=llamacpp" --format '{{.Names}}' 2>/dev/null | head -1)
    if [[ -z "$CONTAINER_NAME" ]]; then
      CONTAINER_NAME="llamacpp"
    fi

    # Verify the container is actually running
    if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
      echo "  ERROR: llamacpp container is not running (keep-running mode requires it to be up)" >&2
      echo "  Run: docker compose -f compose.yaml up -d llamacpp" >&2
      exit 1
    fi
    vlog "Container is running ($CONTAINER_NAME) — proceeding to copy model"

    # List files in container model dir for debugging
    vlog "Listing /models/ in container..."
    ls_models=$(docker exec "$CONTAINER_NAME" sh -c 'ls -la /models/' 2>&1 || true)
    vlog2 "$ls_models"

    # Copy model files using docker cp (not docker compose cp — which hangs)
    echo "  Copying model files from container..."
    local_copy_err=""
    local_copy_ok=0
    # Use sh -c so glob expands inside the container, not on the host
    local_gguf_list=$(docker exec "$CONTAINER_NAME" sh -c 'ls /models/*.gguf' 2>/dev/null || true)

    if [[ -n "$local_gguf_list" ]]; then
      while IFS= read -r gguf_path; do
        gguf_basename=$(basename "$gguf_path")
        if ! docker cp "$CONTAINER_NAME:$gguf_path" "$MODEL_HOST_DIR/" 2>&1; then
          local_copy_err="${local_copy_err}Failed to copy $gguf_basename\n"
          local_copy_ok=1
        else
          vlog "  Copied $gguf_basename"
        fi
      done <<< "$local_gguf_list"
    else
      local_copy_err="No .gguf files found in /models/ inside container\n"
      local_copy_ok=1
    fi

    if [[ $local_copy_ok -ne 0 ]]; then
      echo "  ERROR: Failed to copy model from llamacpp container" >&2
      echo "  Copy error output:" >&2
      echo -e "$local_copy_err" | sed 's/^/    /' >&2
      echo "  Tip: Check container is running with: docker compose ps llamacpp" >&2
      exit 1
    fi
    vlog "  Copy succeeded"

    # List what we got
    vlog "Files copied to $MODEL_HOST_DIR:"
    ls -la "$MODEL_HOST_DIR"/*.gguf 2>/dev/null | sed 's/^/    /' >&2 || vlog2 "  (no .gguf files found)"

    MODEL_FILE=$(find "$MODEL_HOST_DIR" -maxdepth 1 -name "*.gguf" -type f | head -1)
    if [[ -z "$MODEL_FILE" ]]; then
      echo "  ERROR: No model file found in llamacpp container after copy" >&2
      exit 1
    fi
    vlog "  Selected model: $(basename "$MODEL_FILE")"
  else
    echo "  Model not on host — starting llamacpp container to extract model..."

    # Check if llamacpp is running
    vlog "Checking llamacpp container status..."
    if ! docker compose -f "$LAB_COMPOSE" --project-directory "$LAB_DIR" ps llamacpp 2>/dev/null | grep -q "Up"; then
      echo "  llamacpp not running — starting it..."
      vlog "Running: docker compose up -d llamacpp"
      docker compose -f "$LAB_COMPOSE" --project-directory "$LAB_DIR" up -d llamacpp 2>&1 || {
        echo "ERROR: Failed to start llamacpp container" >&2
        exit 1
      }
      CONTAINER_STARTED=1
      # Wait for container to be ready
      echo "  Waiting for llamacpp to start (up to 2 minutes)..."
      local_started=0
      for i in $(seq 1 60); do
        vlog2 "  Waiting... (attempt $i/60)"
        # Use docker ps (not compose) + docker exec sh -c (glob expands in container)
        if docker ps --filter "name=llamacpp" --format '{{.Names}}' | grep -q llamacpp && \
           docker exec "$(docker ps --filter "name=llamacpp" --format '{{.Names}}' | head -1)" sh -c 'ls /models/*.gguf' >/dev/null 2>&1; then
          local_started=1
          vlog2 "  Model files detected after $i attempts"
          break
        fi
        sleep 2
      done
      if [[ $local_started -eq 0 ]]; then
        echo "  WARNING: Container may not have model files yet — will attempt copy anyway" >&2
      fi
    else
      echo "  llamacpp already running, extracting model..."
    fi

    # Resolve container name using docker ps directly (not docker compose)
    CONTAINER_NAME=$(docker ps --filter "name=llamacpp" --format '{{.Names}}' 2>/dev/null | head -1 || echo "llamacpp")

    # Copy model files using docker cp (not docker compose cp — which hangs)
    echo "  Copying model files from container..."
    local_copy_err=""
    local_copy_ok=0
    # Use sh -c so glob expands inside the container, not on the host
    local_gguf_list=$(docker exec "$CONTAINER_NAME" sh -c 'ls /models/*.gguf' 2>/dev/null || true)

    if [[ -n "$local_gguf_list" ]]; then
      while IFS= read -r gguf_path; do
        gguf_basename=$(basename "$gguf_path")
        if ! docker cp "$CONTAINER_NAME:$gguf_path" "$MODEL_HOST_DIR/" 2>&1; then
          local_copy_err="${local_copy_err}Failed to copy $gguf_basename\n"
          local_copy_ok=1
        else
          vlog "  Copied $gguf_basename"
        fi
      done <<< "$local_gguf_list"
    else
      local_copy_err="No .gguf files found in /models/ inside container\n"
      local_copy_ok=1
    fi

    if [[ $local_copy_ok -ne 0 ]]; then
      echo "  ERROR: Failed to copy model from llamacpp container" >&2
      echo "  Copy error output:" >&2
      echo -e "$local_copy_err" | sed 's/^/    /' >&2
      exit 1
    fi
    vlog "  Copy succeeded"

    MODEL_FILE=$(find "$MODEL_HOST_DIR" -maxdepth 1 -name "*.gguf" -type f | head -1)
    if [[ -z "$MODEL_FILE" ]]; then
      echo "  ERROR: No model file found in llamacpp container" >&2
      exit 1
    fi
    vlog "  Selected model: $(basename "$MODEL_FILE")"
  fi
fi

echo "  Model: $MODEL_FILE"
vlog "Model file details:"
vlog2 "  Path: $MODEL_FILE"
vlog2 "  Size: $(du -h "$MODEL_FILE" | cut -f1)"
vlog2 "  Keep container mode: $( [[ $KEEP_CONTAINER -eq 1 ]] && echo "YES" || echo "NO" )"
vlog2 "  Container started by this script: $CONTAINER_STARTED"

# ── Extract llama-bench + required .so files from Docker image (idempotent) ─────
# Check if cached llama-bench exists AND is executable AND has reasonable size
if [[ ! -f "$LLAMA_BENCH" ]] || [[ ! -x "$LLAMA_BENCH" ]] || [[ ! -s "$LLAMA_BENCH" ]]; then
  echo "  Extracting llama-bench and shared libraries..."
  # Try multiple image tags that might contain llama-bench
  BENCH_IMAGES=(
    "ghcr.io/ggml-org/llama.cpp:full-cuda"
    "ghcr.io/ggml-org/llama.cpp:light-cuda"
    "ghcr.io/ggml-org/llama.cpp:server-cuda"
  )
  EXTRACTED=0
  for img in "${BENCH_IMAGES[@]}"; do
    vlog "Trying image: $img"
    TEMP_DIR=$(mktemp -d)
    # Extract binary
    if docker run --rm --entrypoint sh "$img" -c 'cat /app/llama-bench 2>/dev/null || cat /usr/local/bin/llama-bench 2>/dev/null || cat /usr/bin/llama-bench 2>/dev/null || find / -name llama-bench -exec cat {} \; 2>/dev/null' > "$TEMP_DIR/llama-bench" 2>&1; then
      if [[ -s "$TEMP_DIR/llama-bench" ]] && file "$TEMP_DIR/llama-bench" | grep -q "executable"; then
        # Extract ALL llama shared libraries from /app/
        vlog "  Extracting shared libraries..."
        for lib in libllama.so libllama-bench-impl.so libllama-batched-bench-impl.so libllama-cli-impl.so libllama-common.so libllama-completion-impl.so libllama-fit-params-impl.so libllama-perplexity-impl.so libllama-quantize-impl.so libllama-server-impl.so; do
          docker run --rm --entrypoint sh "$img" -c "cat /app/$lib" > "$TEMP_DIR/$lib" 2>&1 || true
        done
        # Copy all to cache dir
        mv "$TEMP_DIR/llama-bench" "$LLAMA_BENCH"
        chmod +x "$LLAMA_BENCH"
        for so in "$TEMP_DIR"/*.so; do
          [[ -f "$so" && -s "$so" ]] && cp "$so" "$(dirname "$LLAMA_BENCH")/"
        done
        echo "  llama-bench + libraries extracted from $img"
        vlog "  llama-bench binary size: $(du -h "$LLAMA_BENCH" | cut -f1)"
        EXTRACTED=1
        rm -rf "$TEMP_DIR"
        break
      fi
    fi
    rm -rf "$TEMP_DIR"
  done
  if [[ $EXTRACTED -eq 0 ]]; then
    echo "ERROR: Could not extract llama-bench from any image" >&2
    echo "  Tried: ${BENCH_IMAGES[*]}" >&2
    exit 1
  fi
else
  vlog "Using cached llama-bench: $LLAMA_BENCH"
  vlog2 "  Binary size: $(du -h "$LLAMA_BENCH" | cut -f1)"
fi

# ── Run benchmark ────────────────────────────────────────────────────────────
echo ""
echo "============================================================"
echo " llama.cpp Native Benchmark (host)"
echo "  Model     : $(basename "$MODEL_FILE")"
echo "  Threads   : $THREADS"
echo "  CPU-MoE   : $NCPU_MOE"
echo "  Micro-bat : $MICRO_BATCH"
echo "  Context   : $CONTEXT"
echo "  KV K/V    : $K_TYPE / $V_TYPE"
echo "  GPU layers: $NGPU_LAYERS"
echo "  Repeats   : $N_REPEATS"
echo "============================================================"

# Run llama-bench
vlog "Running llama-bench with config:"
vlog2 "  Command: $LLAMA_BENCH -m $MODEL_FILE -t $THREADS --n-gpu-layers $NGPU_LAYERS --n-cpu-moe $NCPU_MOE --ctx-size $CONTEXT --cache-type-k $K_TYPE --cache-type-v $V_TYPE -ub $MICRO_BATCH --n-prompt $N_PROMPTS --repetitions $N_REPEATS --json"
vlog2 "  (llama-bench will take ~1-3 minutes per run)"
vlog "Executing llama-bench (this may take a while)..."
BENCH_DIR=$(dirname "$LLAMA_BENCH")
LLAMA_BENCH_OUTPUT=$(LD_LIBRARY_PATH="$BENCH_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" "$LLAMA_BENCH" \
  -m "$MODEL_FILE" \
  -t "$THREADS" \
  --n-gpu-layers "$NGPU_LAYERS" \
  --n-cpu-moe "$NCPU_MOE" \
  --ctx-size "$CONTEXT" \
  --cache-type-k "$K_TYPE" \
  --cache-type-v "$V_TYPE" \
  -ub "$MICRO_BATCH" \
  --n-prompt "$N_PROMPTS" \
  --repetitions "$N_REPEATS" \
  --json 2>&1) || true

echo ""
echo "--- Raw JSON Output ---"
echo "$LLAMA_BENCH_OUTPUT"
echo "--- End Raw ---"
echo ""

# Extract inference TPS (from generation test, n_gen > 0)
vlog "Extracting TPS from llama-bench JSON output..."
vlog2 "  Output length: $(echo "$LLAMA_BENCH_OUTPUT" | wc -c) bytes"
vlog2 "  Searching for 'avg_ts' field..."
TPS=$(echo "$LLAMA_BENCH_OUTPUT" | grep -oP '"avg_ts"\s*:\s*\K[\d.]+' | head -1 || true)

# Fallback: try 'avg_ns' (nanoseconds per token) if avg_ts not found
if [[ -z "$TPS" ]]; then
  vlog2 "  'avg_ts' not found — trying fallback fields..."
  local_ns=$(echo "$LLAMA_BENCH_OUTPUT" | grep -oP '"avg_ns"\s*:\s*\K[\d.]+' | head -1 || true)
  if [[ -n "$local_ns" ]]; then
    TPS=$(awk "BEGIN {printf \"%.1f\", 1000000000 / $local_ns}" 2>/dev/null || echo "")
    vlog2 "  Derived TPS from avg_ns ($local_ns ns/token): $TPS"
  fi
fi

# Another fallback: try 'avg_t' (tokens per second directly)
if [[ -z "$TPS" ]]; then
  vlog2 "  'avg_ts' and 'avg_ns' not found — trying 'avg_t'..."
  TPS=$(echo "$LLAMA_BENCH_OUTPUT" | grep -oP '"avg_t"\s*:\s*\K[\d.]+' | head -1 || true)
  vlog2 "  Found 'avg_t': ${TPS:-not found}"
fi

# List all numeric fields for debugging
vlog2 "  All numeric fields in JSON:"
echo "$LLAMA_BENCH_OUTPUT" | grep -oP '"[^"]*"\s*:\s*\K[\d.]+' | head -20 | sed 's/^/    /' >&2

if [[ "$OUTPUT_JSON" == "1" ]]; then
  echo "{"
  echo "  \"mode\": \"native\","
  echo "  \"config\": {"
  echo "    \"threads\": $THREADS,"
  echo "    \"n_cpu_moe\": $NCPU_MOE,"
  echo "    \"micro_batch\": $MICRO_BATCH,"
  echo "    \"context\": $CONTEXT,"
  echo "    \"gpu_layers\": $NGPU_LAYERS"
  echo "  },"
  echo "  \"results\": {"
  if [[ -n "$TPS" ]]; then
    echo "    \"inference_tps\": $TPS"
  fi
  echo "  }"
  echo "}"
else
  if [[ -n "$TPS" ]]; then
    echo "  ██████  INFERENCE THROUGHPUT: ${TPS} tokens/sec"
  else
    echo "  ██████  INFERENCE THROUGHPUT: N/A tokens/sec"
  fi
fi
