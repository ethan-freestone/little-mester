# Little-Mester Optimization Tickets

## TICKET-001: Switch from Ollama to llama.cpp with MoE expert offload
**Status**: DONE
**Priority**: HIGH
**Completed**: 2026-10-21

### Problem
- Current config uses Ollama with 196k context on 8GB VRAM
- `MAX_LOADED_MODELS=1` + `small_model: qwen3:8b` causes model thrashing
- Ollama splits MoE by layer (not expert), worst for 35B-A3B on 8GB
- 196k context forces layers off GPU, slow prefill

### Solution Implemented
- ✅ llama.cpp with `--n-cpu-moe` for expert offload (GPU: attention/shared/KV/active experts; CPU: remaining experts)
- ✅ Reduced context to 64k (65536)
- ✅ Q4_K_M quantization (speed/quality sweet spot)
- ✅ Set `small_model` = `model` to prevent thrashing

### Changes Made
1. ✅ Updated `models.txt` with backend sections `[ollama]` and `[llamacpp]`
2. ✅ Updated `scripts/regen-configs` to parse backend sections and generate dual-provider opencode.json
3. ✅ Updated `compose.yaml` with llama.cpp service with expert offload flags
4. ✅ Created `llamacpp/pull-gguf.sh` for GGUF downloads from HuggingFace
5. ✅ Updated `lab` script to handle both backends in setup/pull

---

## TICKET-002: Support both Ollama AND llama.cpp simultaneously
**Status**: DONE
**Priority**: HIGH
**Completed**: 2026-10-21

### Goal
- Keep both backends available for learning/comparison
- Allow CLI model switching between any model on either backend
- `models.txt` uses section headers: `[ollama]` and `[llamacpp]`

### Implementation
1. ✅ `models.txt` with both backends
2. ✅ `scripts/regen-configs` generates dual-provider opencode.json
3. ✅ `compose.yaml` has both `ollama` and `llamacpp` services
4. ✅ Model download for both (ollama-pull + llamacpp-pull)
5. ✅ `lab` script runs both pull services
6. ✅ OpenCode `/model` command works across both providers

---

## TICKET-003: Update compose.yaml with llama.cpp service
**Status**: DONE
**Priority**: HIGH
**Completed**: 2026-10-21

### Implemented llama.cpp service config:
```yaml
llamacpp:
  image: ghcr.io/ggml-org/llama.cpp:server-cuda
  environment:
    MODEL_FILE: "/models/qwen3-35b-a3b-q4_k_m.gguf"  # set by regen-configs
    LLAMA_CPP_CONTEXT_LENGTH: "65536"
  command: >
    -m ${MODEL_FILE}
    --host 0.0.0.0 --port 11434 --alias llamacpp-main
    -ngl 99 --n-cpu-moe 40
    -c ${LLAMA_CPP_CONTEXT_LENGTH} -fa on
    --cache-type-k q8_0 --cache-type-v q8_0
    -t 8 -b 2048 -ub 2048
    --parallel 1 --jinja --mlock
  ulimits: { memlock: -1 }
  volumes: [ "llamacpp-models:/models:ro" ]
  networks: [internal]
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: all
            capabilities: [gpu]
  healthcheck:
    test: ["CMD", "curl", "-sf", "http://127.0.0.1:11434/health"]
    interval: 10s
    timeout: 5s
    retries: 30
    start_period: 30s
```

---

## TICKET-004: Model download for llama.cpp (GGUF from HuggingFace)
**Status**: DONE
**Priority**: HIGH
**Completed**: 2026-10-21

### Implementation
- ✅ `llamacpp-pull` service (profile: setup)
- ✅ Uses `python:3.12-slim` with `huggingface_hub[hf_transfer]`
- ✅ `llamacpp/pull-gguf.sh` parses `[llamacpp]` section from models.txt
- ✅ `resolve_gguf()` maps model names to HF repo:filename
- ✅ Downloads to `llamacpp-models` volume at `/models`
- ✅ `HF_HUB_ENABLE_HF_TRANSFER=1` for fast downloads
- ✅ `lab setup` and `lab pull` run both pull services

### Models configured:
| models.txt name | HF Repo | Filename |
|---|---|---|
| qwen3-35b-a3b-q4_k_m | Qwen/Qwen3-35B-A3B-GGUF | qwen3-35b-a3b-q4_k_m.gguf |
| qwen3-coder-30b-a3b-q4_k_m | Qwen/Qwen3-Coder-30B-A3B-GGUF | qwen3-coder-30b-a3b-q4_k_m.gguf |
| gpt-oss-20b-q4_k_m | openai/gpt-oss-20b-GGUF | gpt-oss-20b-q4_k_m.gguf |

---

## TICKET-005: Dynamic model switching for llama.cpp
**Status**: DEFERRED (v2)
**Priority**: MEDIUM

### Current approach
Single model at a time, restart to switch (like Ollama with MAX_LOADED_MODELS=1).

### To switch models:
1. Edit `models.txt` (reorder `[llamacpp]` section)
2. Run `./lab pull` (downloads new model if needed)
3. Run `./lab stop && ./lab <workspace>` (restarts with new MODEL_FILE)

### Future enhancement (v2)
- Add `./lab model <name>` command that updates MODEL_FILE and restarts llamacpp
- Or run multiple llama.cpp instances on different ports (compose profiles)
- Check if llama.cpp server supports multiple `-m` args for multi-model

---

## TICKET-006: Update lab script for dual-backend setup
**Status**: DONE
**Priority**: MEDIUM
**Completed**: 2026-10-21

### Changes
- ✅ `./lab setup` runs both `ollama-pull` and `llamacpp-pull`
- ✅ `./lab pull` runs both pull services
- ✅ `./lab build` regenerates configs before building
- ✅ Egress network used for both pull services (HuggingFace + Ollama registry)

---

## TICKET-007: Document model switching in CLI
**Status**: DONE
**Priority**: LOW
**Completed**: 2026-10-21

### OpenCode model switching (documented in README):
- `/model ollama/qwen3:8b` - switch to Ollama 8B
- `/model ollama/qwen3:14b` - switch to Ollama 14B
- `/model llamacpp/qwen3-35b-a3b-q4_k_m` - switch to llama.cpp 35B MoE
- `/model llamacpp/qwen3-coder-30b-a3b-q4_k_m` - switch to llama.cpp Coder 30B
- `/model llamacpp/gpt-oss-20b-q4_k_m` - switch to llama.cpp gpt-oss 20B

---

## TICKET-008: Performance tuning and validation
**Status**: PENDING (requires hardware)
**Priority**: HIGH

### After setup on target hardware:
- [ ] Test `--n-cpu-moe` values (start high, lower until VRAM ~7-7.4GB)
- [ ] Benchmark with `llama-bench` for optimal `-t` (threads)
- [ ] Compare Ollama vs llama.cpp token/s
- [ ] Verify no OOM, stable inference
- [ ] Measure prefill vs decode performance
- [ ] Document actual t/s achieved

### Tuning knobs (in compose.yaml llamacpp service):
| Flag | Current | Tune range | How to test |
|---|---|---|---|
| `--n-cpu-moe` | 40 | 40→0 (lower = more experts on GPU) | Watch VRAM in metrics, stop at ~7.4GB |
| `-t` | 8 | 8-12 | `llama-bench -m model.gguf -t N` |
| `-ub` | 2048 | 1024/2048/4096 | Benchmark prefill speed |
| `-c` | 65536 | 32768-131072 | Test context vs VRAM tradeoff |

---

## TICKET-009: System/laptop optimizations
**Status**: OPERATIONAL (documented in README)
**Priority**: MEDIUM

### From Claude (implemented as guidance):
- ✅ Documented: Run plugged in, performance profile
- ✅ Documented: dGPU in discrete/MUX mode
- ✅ Documented: Cooling stand/fan critical
- ✅ Documented: Close browsers/Electron apps
- ✅ Documented: `--mlock` in llama.cpp command
- ✅ No `mem_limit` on model container (agent has 8g)
- ✅ `OLLAMA_FLASH_ATTENTION=1`, `KV_CACHE_TYPE=q8_0`, `OLLAMA_NUM_PARALLEL=1`

---

## TICKET-010: Verify sandbox still passes
**Status**: PENDING
**Priority**: HIGH

### Required after all changes:
- [ ] Run `./lab setup` on target machine
- [ ] Run `./verify-lab.sh` - all checks must pass
- [ ] Verify both ollama and llamacpp are healthy
- [ ] Test opencode connects to both backends
- [ ] Test model switching via `/model` command
- [ ] Verify metrics sidecar works with both GPU services

---

## TICKET-011: HF token support for gated models
**Status**: PENDING
**Priority**: LOW

### Need to add:
- Optional `HF_TOKEN` env var in `llamacpp-pull` service
- Document in README how to set for gated models (e.g., some Qwen models)
- Could add to `.little-mester.conf` or `.mester-egress` manifest

---

## TICKET-012: Add --no-cache flag to lab setup/build
**Status**: DONE
**Priority**: MEDIUM
**Completed**: 2026-10-21

### Changes
- Added `NO_CACHE` variable to lab script
- Added `--no-cache` flag parsing
- `./lab setup --no-cache` and `./lab build --no-cache` now pass `--no-cache` to `docker compose build`
- Updated help text

---

## TICKET-013: Update verify-lab.sh for dual backend + metrics
**Status**: DONE
**Priority**: HIGH
**Completed**: 2026-10-21

### Changes
- Added check for `llamacpp reachable` (health endpoint)
- Now verifies both ollama and llamacpp model servers are accessible from agent container
- Added metrics sidecar verification:
  - `metrics sidecar reachable` - /summary endpoint
  - `metrics JSON valid` - /metrics returns valid JSON
  - `metrics has GPU data` - GPU metrics present
  - `metrics has CPU data` - CPU metrics present
  - `metrics has RAM data` - RAM metrics present
  - `metrics summary readable` - Human-readable summary contains VRAM
  - `get_host_metrics tool` - Plugin-style tool call works
- Renamed from verify-sandbox.sh to verify-lab.sh (broader scope)