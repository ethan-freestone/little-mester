# Benchmarking System

A lightweight, fast iteration system for tuning llama.cpp parameters without full rebuilds.

## Quick Reference

| Command | What it does | When to use | Time |
|---------|-------------|-------------|------|
| `./lab bench` | Measure throughput/latency against running server | Anytime, to get current performance numbers | ~10-30s |
| `./lab bench --native` | Run llama.cpp `--test` inside container | Test raw inference speed, no HTTP overhead | ~5s |
| `./lab bench --json` | Machine-readable output | Send results to AI for analysis | ~10-30s |
| `./lab restart-llama` | Restart server with new params | After editing compose.yaml llamacpp command | ~20s |
| `./lab restart-llama --cache` | Restart with KV cache persistence | Iterating fast between tests (same model) | ~3s |
| `./lab tune` | Sweep parameters, produce comparison table | When you want to find optimal settings | 5-30 min |

## The Three Benchmark Modes

### 1. HTTP Mode (default — `./lab bench`)

Sends completion/chat requests to the running llamacpp server and measures
real-world throughput and latency.

**Use when:**
- You want to know how fast the server actually responds to requests
- You're testing the server as it will be used (with OpenAI-compatible API)
- You're NOT on the target GPU machine (bench container doesn't need GPU)

**Don't use when:**
- You need the absolute fastest benchmark (HTTP adds ~1-2ms overhead)
- You need to test parameters that change the server (threads, cpu-moe) — the server needs restarting for that

### 2. Native Mode (`./lab bench --native`)

Runs `llama.cpp --test` inside the bench container, which has access to the
GPU and model volume. Zero HTTP overhead.

**Use when:**
- You want the most accurate inference benchmark (no HTTP wrapper)
- You're iterating between thread counts or micro-batch sizes (instant switches)
- You're on the target GPU machine (bench container needs GPU access)

**Don't use when:**
- The llamacpp server is actively being used (native mode blocks the container)
- You're not on the target machine (need GPU access)

### 3. Tune Mode (`./lab tune`)

Automatically cycles through parameter combinations and produces a comparison
table. Best for initial hardware setup.

**Use when:**
- Setting up on a new machine for the first time
- You want to find optimal threads, micro-batch, cpu-moe settings
- You want JSON output to send to AI for analysis

**Don't use when:**
- You just want a quick single measurement (use `./lab bench`)
- You only need to test one specific parameter (use `./lab bench --native -t 10`)

## Workflow

### Baseline measurement

```bash
./lab bench                  # HTTP mode — real server throughput
./lab bench --json           # JSON output for AI analysis
```

### Test a different parameter

```bash
# 1. Edit compose.yaml → llamacpp command (e.g., change -t 8 to -t 10)
# 2. Restart the server
./lab restart-llama
# 3. Benchmark the new config
./lab bench --native
```

### Full tuning sweep

```bash
# Test multiple thread counts
./lab tune -t 8,10,12

# Test all combinations
./lab tune --full

# Save results for AI analysis
./lab tune --json > tune-results-$(date +%Y%m%d).json
```

## JSON Output for AI Analysis

All benchmark commands support `--json` for machine-readable output:

```bash
./lab bench --json > bench-result.json
./lab tune --json > tune-results.json
```

The JSON structure includes:

**HTTP mode (`./lab bench --json`):**
```json
{
  "server": "http://llamacpp:11434",
  "repeats": 5,
  "completion": {
    "short": { "avg_t/s": 142.3, "latency_std_s": 0.15 },
    "medium": { "avg_t/s": 128.7, "latency_std_s": 0.22 },
    "long": { "avg_t/s": 95.4, "latency_std_s": 0.31 }
  },
  "chat": { ... },
  "wall_s": 45.2
}
```

**Native mode (`./lab bench --native --json`):**
```json
{
  "mode": "native",
  "config": {
    "threads": 8,
    "n_cpu_moe": 32,
    "micro_batch": 2048,
    "context": 65536,
    "gpu_layers": 99
  },
  "results": {
    "inference_tps": 145.3
  }
}
```

**Tune mode (`./lab tune --json`):**
```json
{
  "mode": "tune",
  "configs_tested": 12,
  "results": [
    {"config": {"threads": 8, "micro_batch": 1024, "n_cpu_moe": 32, "context": 65536}, "tps": "132.4"},
    {"config": {"threads": 10, "micro_batch": 1024, "n_cpu_moe": 32, "context": 65536}, "tps": "145.7"},
    ...
  ]
}
```

## When NOT to Use These Tools

| Scenario | Instead |
|----------|---------|
| You need to rebuild the agent image | `./lab build` or `./lab setup --no-cache` |
| You changed the model file (GGUF) | `./lab restart-llama` (without `--cache`) |
| You want to pull a new model | `./lab pull` |
| You want to test the full agent pipeline | Use the agent normally with `./lab <workspace>` |
| You're not on the GPU machine | Use HTTP mode only (`./lab bench`) |

## Machine Configuration

Copy `machine.conf.example` to `machine.conf` and fill in your hardware:

```bash
cp machine.conf.example machine.conf
```

This file is `.gitignore`'d and stores your machine-specific specs. The AI reads
this to give informed tuning advice.

## Understanding the Metrics

| Metric | What it means | Good value |
|--------|--------------|------------|
| **t/s (throughput)** | Tokens generated per second | Higher is better |
| **latency** | Time from request to response | Lower is better |
| **prompt_t/s** | Prompt processing speed | Higher is better |
| **latency_std** | Consistency of response time | Lower is better |
| **VRAM usage** | GPU memory consumed | Must stay under limit (typically ~7.6GB for 8GB GPU) |

## Tips

1. **Always warm up first.** Run one request before benchmarking to let GPU kernels initialize.
2. **Check VRAM before tuning.** If VRAM is already near the limit, reducing `--n-cpu-moe` further will cause OOM.
3. **Use native mode for quick iteration.** Changing `-t` or `-ub` between native bench runs is instant (no model reload).
4. **Use HTTP mode for production-like tests.** This measures the full request/response cycle including the HTTP layer.
5. **Save JSON results.** Compare different machines or model versions with `--json` output.
