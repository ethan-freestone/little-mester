# TICKET-017: `./lab tune` fails — `WORKSPACE` not exported in `bench-native.sh`, model extraction broken, no model cache

**Status**: IMPLEMENTED
**Priority**: HIGH
**Date**: 2026-10-22

---

## Problem

Running `./lab tune --verbose --analyse` (or `./lab tune` without `--analyse`) produces no usable results. All 9 benchmark configurations return `N/A` t/s due to a cascade of failures:

1. `bench-native.sh` calls `docker compose ps llamacpp` which fails on Docker Compose interpolation because `WORKSPACE` is not exported
2. The container status check fails, so `bench-native.sh` reports "llamacpp container is not running" even though the tune script started it
3. Model files are never extracted from the container to the host cache (`.bench-models/`), so `llama-bench` has no model to benchmark
4. The AI analysis step cannot reach Ollama (expected — not running), but the benchmark data itself is worthless anyway

---

## Attempted fixes

### Attempt: TICKET-016 fix (WORKSPACE shell variable)

**What was done**: Added `WORKSPACE="${WORKSPACE:-$LAB_DIR}"` at line 24 of `bench-native.sh`.

**Result**: ❌ Incomplete fix. The variable is assigned as a shell variable but not exported. Docker Compose child processes do not inherit non-exported shell variables. The `compose.yaml` agent service has `${WORKSPACE:?run via ./lab}` which fails because WORKSPACE is effectively unset from Docker Compose's perspective.

**Why it failed**: The `./lab` script's `dc()` wrapper explicitly passes WORKSPACE via `env "WORKSPACE=..."`, but `bench-native.sh` calls `docker compose` directly without this wrapper and without exporting the variable.

### Attempt: Run `./lab setup` first

**What was done**: Ensure models are pulled into the Docker volume and the host cache is populated.

**Result**: ⚠️ Does not solve the WORKSPACE issue. The model extraction from container to host would still fail at the container status check step.

---

## Implementation

### Fix 1: Export WORKSPACE in `bench-native.sh`

**File**: `bench/bench-native.sh`, line 24

**Change**: Added `export` keyword so WORKSPACE is available to child processes (docker compose).

```diff
-WORKSPACE="${WORKSPACE:-$LAB_DIR}"
+export WORKSPACE="${WORKSPACE:-$LAB_DIR}"
```

**Why**: `docker compose` reads the entire `compose.yaml` (including all services) before executing the requested command. The `agent` service has `${WORKSPACE:?run via ./lab}` in its volume mount. Without WORKSPACE in the process environment, Docker Compose fails with:

```
required variable WORKSPACE is missing a value: run via ./lab
```

The `export` keyword makes the variable available to all child processes, matching how the `dc()` wrapper in `./lab` passes it.

### Fix 2: Pre-populate model cache in `./lab tune`

**File**: `lab`, lines 529-544 (new)

**Change**: Added a model cache pre-population step between the llamacpp startup/wait and the benchmark loop. The tune script now copies models from the container to `.bench-models/` once, before iterating over benchmark configs.

```bash
# Pre-populate host model cache so bench-native.sh skips container extraction per-run
echo ">> Pre-populating model cache from container..."
MODEL_CACHE_DIR="$LAB_DIR/.bench-models"
mkdir -p "$MODEL_CACHE_DIR"
COPY_ERR=$(docker compose ... cp llamacpp:/models/*.gguf "$MODEL_CACHE_DIR/" 2>&1) || \
  COPY_ERR=$(docker compose ... cp llamacpp:/models/ "$MODEL_CACHE_DIR/" 2>&1)
if [[ -z "$COPY_ERR" ]] || ! echo "$COPY_ERR" | grep -qi "error"; then
  COPY_COUNT=$(find "$MODEL_CACHE_DIR" -maxdepth 1 -name "*.gguf" -type f | wc -l)
  echo "  Copied $COPY_COUNT model file(s) to $MODEL_CACHE_DIR"
else
  echo "  WARNING: Failed to copy models..." >&2
fi
```

**Why**: Previously, every iteration of the benchmark loop caused `bench-native.sh` to:
1. Search for models (fail — empty cache)
2. Try to copy from container (fail — WORKSPACE not exported)
3. Report N/A TPS

With this fix, models are copied once before the loop. Each `bench-native.sh` run finds the model in the cache immediately (first search pass) and skips the container extraction entirely, making each iteration faster and independent of WORKSPACE.

---

## Root cause analysis

### Cause 1: WORKSPACE not exported in `bench-native.sh`

`bench-native.sh` sets `WORKSPACE` as a local shell variable:
```bash
WORKSPACE="${WORKSPACE:-$LAB_DIR}"
```

But this variable is never exported, so when `docker compose` is invoked (e.g., `docker compose -f "$LAB_COMPOSE" --project-directory "$LAB_DIR" ps llamacpp`), Docker Compose reads the entire `compose.yaml`, including the `agent` service which has:

```yaml
volumes:
  - "${WORKSPACE:?run via ./lab}:/workspace:${MOUNT_MODE:-rw}"
```

Docker Compose requires WORKSPACE for the interpolation, and since it's not in the process environment, it fails with:
```
required variable WORKSPACE is missing a value: run via ./lab
```

**The fix**: Export the variable:
```bash
export WORKSPACE="${WORKSPACE:-$LAB_DIR}"
```
Or explicitly pass it via `env` on each docker compose call (less maintainable).

### Cause 2: `./lab tune` never sets `WORKSPACE`

The `tune` command handler in `./lab` (lines 408-648) never calls `check_workspace()`. This is intentional — the tune command doesn't need a workspace mounted. But it means `WORKSPACE=""` at line 7 of `./lab` stays empty when `bench-native.sh` is spawned. The `bench-native.sh` fix in Cause 1 handles the default, but only if exported properly.

### Cause 3: Model cache is empty / model extraction fails

The `.bench-models/` directory is empty (no `.gguf` files). The `bench-native.sh` script falls back to extracting the model from the llamacpp container via `docker compose cp`. This path is broken because:

1. In `--keep-running` mode, the first docker compose call (`ps llamacpp`) fails (Cause 1)
2. The error output includes the interpolation failure, confusing the container status check
3. Even though `bench-native.sh` then falls back to `docker compose cp` for the copy, the copy also fails with the same WORKSPACE interpolation error

### Cause 4: Ollama not reachable for AI analysis

`./lab ai-analyze` tries `localhost:11434` (host Ollama) then `ollama:11434` (Docker internal network). Both fail:
- No host Ollama instance running
- No Docker Ollama container on the internal network

This is an expected operational issue when Ollama isn't started, not a bug. But it means the AI analysis step provides no value when benchmark data is also broken.

---

## Impact (Before Fixes)

| Command | Result |
|---------|--------|
| `./lab tune` | All configs return N/A TPS |
| `./lab tune --json` | JSON with all N/A results |
| `./lab tune --analyse` | Same as above + AI analysis fails |
| `./lab bench --native` | May work if WORKSPACE is set (e.g., from a prior `check_workspace` call) |
| `./lab bench` (HTTP mode) | Works — does not call `bench-native.sh` or use WORKSPACE |

---

## Files Changed

| File | Change | Line(s) |
|------|--------|---------|
| `bench/bench-native.sh` | Added `export` to WORKSPACE variable assignment | 24 |
| `lab` | Added model cache pre-population step in `tune` handler | 529-544 |

---

## Verification

After the fix:
1. `./lab tune --verbose` — should pre-populate the model cache, then run `llama-bench` for each config and report TPS values (not N/A)
2. `./lab tune --verbose --json` — should produce valid JSON with numeric TPS values
3. `./lab tune --verbose --analyse` — same as above, plus AI analysis if Ollama is running
4. WORKSPACE should be visible in the docker compose environment when bench-native.sh calls it (no interpolation errors)

### Expected verbose output (key lines)

```
>> Starting llamacpp container for model access...
  Waiting for llamacpp to start...
>> Pre-populating model cache from container...
  Copied 1 model file(s) to /home/ethan/Development/little-mester/.bench-models
>> Running 9 benchmark configurations (native)...

--- [1/9] t=8  ub=1024  moe=32  ctx=65536 ---
  Model already on host: /home/ethan/Development/little-mester/.bench-models/xxx.gguf
  -> 42.3 t/s
```

---

## Known Limitations

- **Ollama for AI analysis**: The `./lab ai-analyze` step requires a running Ollama instance at `localhost:11434` or `ollama:11434` (Docker internal). This is an operational requirement, not a code issue. Start with `./lab start ollama` and load a model first.
- **Model availability**: If the llamacpp container has no model files in `/models/` (e.g., models haven't been pulled yet via `./lab pull`), the pre-population step will fail gracefully with a warning, and `bench-native.sh` will retry per-run (which will also fail without models). Run `./lab pull` first to ensure models are in the container.
