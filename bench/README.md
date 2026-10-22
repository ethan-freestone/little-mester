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
| `./lab tune --analyse` | Full sweep + AI analysis of results | One-command optimal config find | 5-30 min |
| `./lab ai-analyze` | Analyze benchmark JSON with AI | When you have results and want recommendations | ~30s |

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

### 4. AI Analysis (`./lab tune --analyse` or `./lab ai-analyze`)

> `--ai` is accepted as a shorthand alias for `--analyse`.

After running a benchmark sweep, the AI analyzes the results and tells you
exactly what settings to use — with reasoning, trade-offs, and warnings.

**Use when:**
- You want the AI to pick optimal settings for your hardware
- You're new to llama.cpp tuning and need guidance
- You have benchmark JSON and want expert analysis

**How it works:**
1. Reads benchmark results (from JSON file or piped via stdin)
2. Reads your `machine.conf` for hardware context
3. Sends both to Ollama's local AI model
4. Returns: optimal config + reasoning + trade-offs + compose.yaml changes

**Requirements:**
- Ollama must be running (inside the `little-mester` stack)
- A small model loaded (default: `qwen2.5:1.5b`)

**Don't use when:**
- Ollama isn't running (AI won't be available)
- You want to make a quick manual decision without AI input

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

### One-command sweep + AI analysis

```bash
# Sweep + AI analysis in one command (requires Ollama running)
./lab tune --analyse

# Same thing, two steps (useful if Ollama isn't running)
./lab tune --json > tune-results.json
./lab ai-analyze < tune-results.json

# Pipe results directly to AI
./lab tune --json | ./lab ai-analyze

# Use a different model for analysis
./lab tune --json | ./lab ai-analyze --model phi3:mini
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

This file is `.gitignore`'d and stores your machine-specific specs. It's used by
`./lab ai-analyze` to give context-aware recommendations — the AI needs to know
your GPU, VRAM, and CPU to suggest sensible thread counts, micro-batch sizes, etc.

### What the AI returns

When you run `./lab tune --analyse` or `./lab ai-analyze`, the AI responds with:

```
## RECOMMENDATION (JSON)
{
  "threads": 10,
  "n_cpu_moe": 32,
  "micro_batch": 2048,
  "context": 65536,
  "gpu_layers": 99,
  "reasoning": "10 threads maximizes throughput without CPU contention",
  "confidence": "high"
}

## ANALYSIS
### Optimal Settings

Based on your benchmark results:
- **Threads: 10** — 10 out of 12 cores gives best throughput
  (2 cores reserved for system prevents throttling)
- **CPU-MoE: 32** — Good balance between GPU and CPU expert routing
- **Micro-batch: 2048** — Safe for 8GB VRAM headroom

### Trade-offs
- Going to 4096 micro-batch would give ~5% more TPS but risks KV cache OOM
- Using all 12 threads hurts system responsiveness and may cause throttling

### Next Steps
Edit compose.yaml llamacpp command:
  -t 10 --n-cpu-moe 32 -ub 2048 -c 65536
Then run: ./lab restart-llama && ./lab bench
```

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
6. **Use `./lab tune --analyse`** for one-command optimal config discovery. The AI reads your
   `machine.conf` and benchmark results to give tailored recommendations.
7. **Always verify AI suggestions** with `./lab bench` after applying them — the AI's
   recommendation is a starting point, but actual performance may vary based on your
   specific workload patterns.
