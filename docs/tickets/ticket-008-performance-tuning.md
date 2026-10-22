# TICKET-008: Performance tuning and validation

**Status**: IN PROGRESS
**Priority**: HIGH

## Benchmark tool: `./lab bench`

- [x] `bench/bench.py` — lightweight benchmark runner
- [x] `bench/Dockerfile` — python:3.12-slim, no GPU needed
- [x] `compose.yaml` bench service (internal network, connects to llamacpp:11434)
- [x] `./lab bench` subcommand — builds in ~2s, no model reload
- [x] Tests 3 prompt lengths (short/medium/long) × 2 APIs (completion/chat)
- [x] Reports: tokens/sec, latency, prompt throughput, ±std deviation
- [x] `--json` flag for machine-readable output

## After setup on target hardware

- [ ] Test `--n-cpu-moe` values (start high, lower until VRAM ~7-7.4GB)
- [ ] Benchmark with `llama-bench` for optimal `-t` (threads)
- [ ] Compare Ollama vs llama.cpp token/s
- [ ] Verify no OOM, stable inference
- [ ] Measure prefill vs decode performance
- [ ] Document actual t/s achieved

## Tuning knobs (in compose.yaml llamacpp service)

| Flag          | Current | Tune range      | How to test                                      |
|---------------|---------|-----------------|--------------------------------------------------|
| `--n-cpu-moe` | 40      | 40→0 (lower = more experts on GPU) | Watch VRAM in metrics, stop at ~7.4GB       |
| `-t`          | 8       | 8-12            | `llama-bench -m model.gguf -t N`                 |
| `-ub`         | 2048    | 1024/2048/4096  | Benchmark prefill speed                          |
| `-c`          | 65536   | 32768-131072    | Test context vs VRAM tradeoff                    |
