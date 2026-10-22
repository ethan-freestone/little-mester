# TICKET-018: `./lab tune --verbose --analyse` — tuning sweep execution and documentation

**Status**: RESOLVED
**Priority**: HIGH
**Date**: 2026-10-22

---

## Objective

Run the automated parameter tuning sweep (`./lab tune --verbose --analyse`) to find the optimal llama.cpp configuration for this machine:
- **Hardware**: NVIDIA RTX 5070 Mobile, 8GB VRAM, Intel HX 370 (12 cores)
- **Model**: Qwen 3.6 35B-A3B MoE (~20GB at Q4_K_M)
- **Expected TPS**: 20–35 t/s (theoretical ceiling ~60 t/s)

---

## Sweep Configuration

| Parameter | Values Tested |
|-----------|--------------|
| Threads | 8, 10, 12 |
| Micro-batch | 1024, 2024, 4096 |
| CPU-MoE | 32 (default from compose.yaml) |
| Context | 65536 (default from compose.yaml) |

Total configurations: **3 × 3 × 1 × 1 = 9** benchmark runs.

---

## Execution Log

### Run 1 — Initial sweep
**Time**: 2026-10-22 afternoon
**Command**: `./lab --verbose tune --analyse`
**Result**: Hangs at "Pre-populating model cache" → exits to prompt after ~2min with no results.

### Run 2 — Repeated identical behavior
**Result**: Same — hangs at pre-population. `.bench-models/` stays empty.

### Root Cause: `docker compose cp` hangs
**Bug in `lab` (lines 533-536) and `bench-native.sh`**:
1. `docker compose cp llamacpp:/models/*.gguf ...` — shell expands `*.gguf` in the **host cwd**, not container. If no local `.gguf`, literal `*.gguf` is passed — `docker compose cp` hangs instead of failing fast.
2. Fallback `docker compose cp llamacpp:/models/ ...` also hangs.
3. **`|| true` masks exit codes**: `local_copy_ok=$?` always reads 0 because `|| true` always succeeds.
4. **`docker compose ps --format json` fails without WORKSPACE** env var — same interpolation issue as TICKET-016. Container name resolution fails silently.
5. **`docker exec -T` not supported** on this Docker version — causes exec to hang/error.
6. **Shell glob expands on host**: `docker exec container ls /models/*.gguf` — the `*.gguf` is expanded by the host shell. Must use `sh -c 'ls /models/*.gguf'` instead.

### Fix Applied v2
Replaced all `docker compose cp` calls with `docker cp` (direct):
- **`lab` (lines 529-565)**: Use `docker ps` for container name (not compose). Use `sh -c` for glob expansion inside container. No `-T` flag.
- **`bench-native.sh` (keep-running path)**: Same fixes — `docker ps`, `sh -c`, no `-T`.
- **`bench-native.sh` (standalone path)**: Same fixes — `docker ps`, `sh -c`, no `-T`.

---

## What Happens After Pre-Population

Once the model cache is populated, the tune script will:

1. Iterate over all 9 configurations (t × ub × moe × ctx)
2. For each config, call `bench-native.sh --keep-running` with the specific parameters
3. Extract TPS from `llama-bench` JSON output (`inference_tps` field)
4. Print a summary table with all results
5. Identify the best configuration
6. If `--analyse` was passed, pipe JSON to `./lab ai-analyze` which:
   - Sends results + machine specs to Ollama's chat API
   - Returns optimal settings with reasoning

### Expected Output Format (when complete)

```
>> Running 9 benchmark configurations (native)...

--- [1/9] t=8  ub=1024  moe=32  ctx=65536 ---
  -> 28.4 t/s
--- [2/9] t=8  ub=2048  moe=32  ctx=65536 ---
  -> 31.2 t/s
...

============================================================
 TUNING RESULTS SUMMARY
============================================================
  Config                      TPS
  -------------------------  -----
  t=8    ub=1024   moe=32   ctx=65536   28.4
  t=8    ub=2048   moe=32   ctx=65536   31.2
  ...

  * Best: t=10  ub=2048  moe=32  ctx=65536 -> 35.1 t/s
```

---

## Known Dependencies

### 1. Model must be pulled first
The `llamacpp-models` Docker volume must contain the `.gguf` file. If not already present, run:
```bash
./lab pull
```

### 2. Ollama required for --analyse
The `--analyse` flag calls Ollama's chat API at `localhost:11434`. If Ollama isn't running or has no model loaded, the AI analysis step will fail gracefully with a warning. The benchmark data collection will still work.

### 3. GPU cooling
The RTX 5070 Mobile will thermally throttle under sustained GPU load. Ensure:
- Laptop is plugged in
- Performance power profile is active
- Cooling stand/external fan is being used
- Browser/Electron apps are closed

---

## Post-Tuning Steps

Once the sweep completes and the best config is identified:

1. **Apply best config** to `compose.yaml` llamacpp command:
   - `-t N` → threads
   - `-ub N` → micro-batch
   - `--n-cpu-moe N` → CPU-MoE
   - Update `machine.conf` defaults

2. **Restart** the server: `./lab restart-llama`

3. **Verify** with a short benchmark: `./lab bench --native`

4. **Update** `machine.conf` with new optimal defaults

5. **Update** this ticket with final results

---

## Files Referenced

| File | Role |
|------|------|
| `lab` (line 408-666) | Tune sweep implementation |
| `bench/bench-native.sh` | Native benchmark runner |
| `bench/Dockerfile` | Bench image definition |
| `compose.yaml` | llamacpp service with command flags |
| `machine.conf` | Hardware profile + tuning defaults |
| `models.txt` | Model list |

---

## Fixes Applied

| File | Lines | Change |
|------|-------|--------|
| `lab` | 529-558 | Replaced `docker compose cp` with `docker exec` list + `docker cp` per-file |
| `bench/bench-native.sh` | keep-running path | Same fix — `docker exec` list + `docker cp` per-file |
| `bench/bench-native.sh` | standalone path | Same fix — `docker exec` list + `docker cp` per-file |

### What changed
- **Before**: Single `docker compose cp` call with shell glob → hangs on invalid paths; `|| true` masks real exit code.
- **After**: `docker exec -T <container> ls /models/*.gguf` to list files, then `docker cp <container>:<path> <dest>` for each file individually. Proper error tracking with `COPY_COUNT` counter.

## Related Tickets

- **TICKET-016**: Fixed `WORKSPACE` env var missing in `bench-native.sh` — RESOLVED
- **TICKET-017**: Fixed model cache pre-population and model extraction — RESOLVED
- **TICKET-008**: Performance tuning — initial decisions about MoE offload

---

## Notes

- The tune script uses `--keep-running` mode in `bench-native.sh` to avoid restarting the container for each config (saves ~20s per iteration)
- Each benchmark run uses `--repeats 3` (hardcoded in tune handler, line 561)
- The `bench-native.sh` cleanup trap only stops the container if it started it — in keep-running mode, the tune script stops the container after all runs
- Model cache at `.bench-models/` is created once before the loop and reused for all 9 iterations
