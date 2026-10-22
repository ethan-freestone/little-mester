# Little-Mester Architecture Decisions

## DECISION-001: Dual-backend support (Ollama + llama.cpp)
**Date**: 2026-10-21
**Status**: ACCEPTED, IMPLEMENTED

### Context
User wants to learn about both backends and compare performance. The system should support both simultaneously with CLI model switching.

### Decision
- `models.txt` uses section headers: `[ollama]` and `[llamacpp]`
- `regen-configs` generates opencode.json with both providers configured
- Both services run in compose.yaml
- OpenCode can switch between any model on either backend via `/model` command

### Implementation
- ✅ `models.txt` with section headers
- ✅ `scripts/regen-configs` parses sections, generates dual-provider opencode.json
- ✅ `compose.yaml` has both `ollama` and `llamacpp` services
- ✅ `llamacpp/pull-gguf.sh` downloads GGUF from HuggingFace
- ✅ `lab` script runs both pull services during `setup` and `pull`

### Alternatives Considered
- Single backend only (rejected: user wants learning/comparison)
- Prefix model names with backend (rejected: sections are cleaner)

---

## DECISION-002: llama.cpp with expert offload for MoE models
**Date**: 2026-10-21
**Status**: ACCEPTED, IMPLEMENTED

### Context
- Hardware: Strix Point (HX 370), 12 cores, 16-CU Radeon 890M, 8GB VRAM, ~120 GB/s RAM bandwidth
- Model: Qwen 35B-A3B MoE (~20GB at Q4)
- Ollama splits by layer, not expert - worst for MoE on 8GB
- llama.cpp supports `--n-cpu-moe` for expert-level offload

### Decision
Use llama.cpp server with:
- `-ngl 99` (offload all possible layers to GPU)
- `--n-cpu-moe 40` (start high, tune down until VRAM ~7-7.4GB)
- `-c 65536` (64k context)
- `--cache-type-k q8_0 --cache-type-v q8_0` (quantized KV cache)
- `-t 8` (8 threads, not 24 - decode is memory-bandwidth bound)
- `-ub 2048` (micro-batch for faster prefill)
- `--mlock` (prevent CPU experts from paging)

### Implementation
- ✅ `compose.yaml` llamacpp service with all flags
- ✅ `MODEL_FILE` env var set by `regen-configs` to first llamacpp model
- ✅ Context length configurable per model in `models.txt`

### Alternatives Considered
- Stay on Ollama with reduced context (rejected: layer-split still suboptimal)
- Use 890M iGPU (rejected: shares RAM bandwidth, no speedup)

---

## DECISION-003: Q4_K_M quantization for main model
**Date**: 2026-10-21
**Status**: ACCEPTED, IMPLEMENTED

### Context
- Q4 is speed/quality sweet spot per Claude
- Q6/Q8 doubles bytes read per token, slows decode proportionally
- Q4_K_M is standard; UD-Q4_K_XL is extended with better outlier handling

### Decision
Use Q4_K_M for main 35B-A3B model. Theoretical ceiling ~60 t/s, expect 20-35 t/s practice.

### Implementation
- ✅ Model names in `models.txt` specify `-q4_k_m`
- ✅ Pull script maps to correct HF repos with Q4_K_M files

---

## DECISION-004: Context window = 64k (65536)
**Date**: 2026-10-21
**Status**: ACCEPTED, IMPLEMENTED

### Context
- 196k context forces layers off GPU, slow prefill
- 8GB VRAM constraint
- Claude recommends 32k-64k

### Decision
Use 65536 as default for llama.cpp models. Can be tuned per-model in models.txt.

### Implementation
- ✅ `models.txt` has 65536 for all models
- ✅ `regen-configs` updates both `OLLAMA_CONTEXT_LENGTH` and `LLAMA_CPP_CONTEXT_LENGTH`
- ✅ llama.cpp command uses `${LLAMA_CPP_CONTEXT_LENGTH}`

---

## DECISION-005: small_model = model (prevent thrashing)
**Date**: 2026-10-21
**Status**: ACCEPTED, IMPLEMENTED

### Context
- `MAX_LOADED_MODELS=1` + different `small_model` causes eviction/reload on every title/summary call
- OpenCode uses small_model for titles and summaries

### Decision
Set `small_model` to same as `model` in generated opencode.json. This prevents thrashing entirely.

### Implementation
- ✅ `regen-configs` sets both `model` and `small_model` to first llamacpp model (or first ollama if no llamacpp)

---

## DECISION-006: Model download strategy for llama.cpp
**Date**: 2026-10-21
**Status**: ACCEPTED, IMPLEMENTED

### Decision
- **llamacpp-pull service** - Similar to ollama-pull, downloads GGUFs from HuggingFace during setup
- Uses `python:3.12-slim` image with `huggingface_hub[hf_transfer]`
- Maps model names in `models.txt` to HF repo:filename in `resolve_gguf()` function
- Volume `llamacpp-models` mounted at `/models` in both pull and server containers
- `HF_HUB_ENABLE_HF_TRANSFER=1` for fast downloads

### Implementation
- ✅ `llamacpp/pull-gguf.sh` with HF repo mapping
- ✅ `compose.yaml` llamacpp-pull service with egress network
- ✅ `lab` script runs both pull services

### Need to decide (future)
- HF token handling for gated models (add `HF_TOKEN` env var support)
- Egress allowlist for huggingface.co (currently uses open egress network)

---

## DECISION-007: Dynamic model switching for llama.cpp
**Date**: 2026-10-21
**Status**: ACCEPTED (single model, restart to switch)

### Problem
llama.cpp server loads one model at startup via `-m`. Switching requires restart.

### Decision
Option 1 for simplicity: Single model, restart to switch. User can:
- Edit `models.txt` (reorder llamacpp section), run `./lab pull`, then `./lab stop && ./lab <workspace>`
- Or manually set `MODEL_FILE` env var when starting

### Future enhancement
- Add `./lab model <name>` command that updates MODEL_FILE and restarts llamacpp
- Or run multiple llama.cpp instances on different ports (compose profiles)

---

## DECISION-008: System optimizations (from Claude)
**Date**: 2026-10-21
**Status**: ACCEPTED (as operational guidance, documented in README)

- Run plugged in, OS performance profile
- dGPU in discrete/MUX mode (not through iGPU)
- Cooling stand/fan critical - throttling costs more than any flag
- Close browsers/Electron apps to free RAM
- `--mlock` keeps CPU-side experts from paging
- No `mem_limit` on model container (agent's 8GB limit is fine)
- Keep `OLLAMA_FLASH_ATTENTION=1` and `KV_CACHE_TYPE=q8_0` for Ollama
- Set `OLLAMA_NUM_PARALLEL=1` (parallel slots multiply KV memory)

---

## DECISION-009: Model candidates for models.txt
**Date**: 2026-10-21
**Status**: ACCEPTED (as starting config)

### llama.cpp (MoE with expert offload)
- `qwen3-35b-a3b-q4_k_m` - Main coder, 35B-A3B, Q4
- `qwen3-coder-30b-a3b-q4_k_m` - Coding-tuned variant
- `gpt-oss-20b-q4_k_m` - MXFP4, ~12GB, ~3.6B active, good tool calling

### Ollama (fully on GPU small models)
- `qwen3:8b` - Fits entirely in 8GB, very fast
- `qwen3:14b` - May fit with reduced context

### Skip
- 70B dense - unusably slow
- gpt-oss-120b - won't fit in 64GB alongside OS/Docker

---

## DECISION-010: Container-aware documentation
**Date**: 2026-10-21
**Status**: ACCEPTED, IMPLEMENTED

### Context
User emphasized: "remember you are in a docker container" and "too often I need to remind the agents that they are in a container"

### Decision
All documentation (README, DECISIONS) explicitly states:
- The agent runs inside a Docker container
- Model servers run in separate containers
- Workspace is bind-mounted at `/workspace` inside the agent container
- Host filesystem is NOT accessible except for that one mount
- All commands run inside containers, not on host

### Implementation
- ✅ README "Architecture" section shows container boundaries
- ✅ README "How a session works" explains container flow
- ✅ README "Quick start" and "Everyday use" reference `./lab` which runs compose
- ✅ Troubleshooting references container-specific commands (`docker logs`, `docker compose ps`)