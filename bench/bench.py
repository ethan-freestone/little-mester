#!/usr/bin/env python3
"""
llama.cpp benchmark runner — connects to a running server on the internal
Docker network and measures throughput / latency.

Usage (env-driven, no args):
    BENCH_HOST=<host>      default: llamacpp
    BENCH_PORT=<port>      default: 11434
    BENCH_PROMPTS=<file>   path to newline-separated prompts (or inline via stdin)
    BENCH_N=<int>          number of repeats per config (default: 5)
    BENCH_JSON             write results as JSON to stdout
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

# ── configuration ────────────────────────────────────────────────────────────

HOST = os.environ.get("BENCH_HOST", "llamacpp")
PORT = int(os.environ.get("BENCH_PORT", "11434"))
N_REPEATS = int(os.environ.get("BENCH_N", "5"))
OUTPUT_JSON = os.environ.get("BENCH_JSON", "0") == "1"

BASE_URL = f"http://{HOST}:{PORT}"

# Prompts of increasing length to test context scaling
PROMPTS: list[tuple[str, str]] = [
    ("short",   "Write a haiku about code."),
    ("medium",  "Write a 200-word technical explanation of how Mixture-of-Experts language models work, including details about expert selection, gating networks, and the trade-offs between sparsity and accuracy."),
    ("long",    "You are a senior software architect reviewing a large codebase. The project is a Docker-based local development sandbox for AI coding agents called 'little-mester'. It uses Ollama and llama.cpp as model backends, supports dynamic model switching, enforces strict security via read-only filesystems and capability dropping, and includes a metrics sidecar that exposes host GPU/CPU/RAM data. Write a comprehensive architecture review covering: container isolation strategy, network segmentation, model loading patterns, performance characteristics of MoE expert offload, and recommendations for hardening the sandbox further. Be detailed and include specific Docker compose directives."),
]


# ── helpers ──────────────────────────────────────────────────────────────────

def api_json(method: str, path: str, body: dict) -> dict:
    """Send a JSON request and return parsed response."""
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def completion(prompt: str, n_predict: int = 256, n_ctx: int = 65536) -> dict:
    """Run /completion, return stats dict."""
    body = {
        "prompt": prompt,
        "n_predict": n_predict,
        "stream": False,
        "n_ctx": n_ctx,
        "cache_type_k": "q8_0",
        "cache_type_v": "q8_0",
    }
    t0 = time.perf_counter()
    resp = api_json("POST", "/completion", body)
    elapsed = time.perf_counter() - t0

    # llama.cpp /completion returns timing in the response
    timed = resp.get("timings", {})
    prompt_tokens = timed.get("prompt_n", resp.get("prompt_eval_count", 0))
    generated_tokens = timed.get("eval_n", resp.get("eval_count", 0))

    return {
        "total_s": round(elapsed, 3),
        "prompt_tokens": prompt_tokens,
        "generated_tokens": generated_tokens,
        "t/s": round(generated_tokens / elapsed, 1) if elapsed else 0,
        "prompt_t/s": round(prompt_tokens / timed.get("prompt_ms", 0) * 1000, 1) if timed.get("prompt_ms") else 0,
    }


def chat(messages: list[dict], n_predict: int = 256, n_ctx: int = 65536) -> dict:
    """Run /v1/chat/completions, return stats dict."""
    body = {
        "model": "llamacpp",
        "messages": messages,
        "max_tokens": n_predict,
        "stream": False,
        "n_ctx": n_ctx,
    }
    t0 = time.perf_counter()
    resp = api_json("POST", "/v1/chat/completions", body)
    elapsed = time.perf_counter() - t0

    usage = resp.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    generated_tokens = usage.get("completion_tokens", 0)

    return {
        "total_s": round(elapsed, 3),
        "prompt_tokens": prompt_tokens,
        "generated_tokens": generated_tokens,
        "t/s": round(generated_tokens / elapsed, 1) if elapsed else 0,
        "prompt_t/s": round(prompt_tokens / elapsed, 1),
    }


# ── benchmark runner ─────────────────────────────────────────────────────────

def mean_std(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    m = sum(values) / len(values)
    v = sum((x - m) ** 2 for x in values) / len(values) if len(values) > 1 else 0
    return round(m, 2), round(v ** 0.5, 2)


def run_suite():
    """Run all benchmarks, return structured results."""
    results: dict = {
        "server": f"{BASE_URL}",
        "repeats": N_REPEATS,
        "completion": {},
        "chat": {},
    }

    for label, prompt in PROMPTS:
        print(f"\n--- Completion ({label}, {len(prompt)} chars) ---")
        comp_times, comp_tps, comp_prompt_s = [], [], []
        for i in range(N_REPEATS):
            stats = completion(prompt)
            comp_times.append(stats["total_s"])
            comp_tps.append(stats["t/s"])
            comp_prompt_s.append(stats["prompt_t/s"])
            print(f"  #{i+1}: {stats['generated_tokens']} tok in {stats['total_s']}s "
                  f"({stats['t/s']} t/s, prompt: {stats['prompt_t/s']} t/s)")

        avg_t, std_t = mean_std(comp_times)
        avg_s, std_s = mean_std(comp_tps)
        results["completion"][label] = {
            "avg_latency_s": avg_t, "latency_std_s": std_t,
            "avg_t/s": avg_s, "tps_std": std_s,
            "avg_prompt_t/s": round(mean_std(comp_prompt_s)[0], 1),
        }

    for label, prompt in PROMPTS:
        print(f"\n--- Chat API ({label}, {len(prompt)} chars) ---")
        chat_times, chat_tps = [], []
        for i in range(N_REPEATS):
            stats = chat([{"role": "user", "content": prompt}])
            chat_times.append(stats["total_s"])
            chat_tps.append(stats["t/s"])
            print(f"  #{i+1}: {stats['generated_tokens']} tok in {stats['total_s']}s "
                  f"({stats['t/s']} t/s)")

        avg_t, std_t = mean_std(chat_times)
        avg_s, std_s = mean_std(chat_tps)
        results["chat"][label] = {
            "avg_latency_s": avg_t, "latency_std_s": std_t,
            "avg_t/s": avg_s, "tps_std": std_s,
        }

    return results


# ── health check ─────────────────────────────────────────────────────────────

def health():
    """Verify server is reachable."""
    try:
        req = urllib.request.Request(f"{BASE_URL}/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            return data.get("status") == "ok"
    except Exception as e:
        print(f"FAIL: server unreachable at {BASE_URL} — {e}", file=sys.stderr)
        return False


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    if not health():
        sys.exit(1)

    print("=" * 60)
    print(f"llama.cpp Benchmark Runner")
    print(f"  Server : {BASE_URL}")
    print(f"  Repeats: {N_REPEATS} per config")
    print("=" * 60)

    t_start = time.perf_counter()
    results = run_suite()
    results["wall_s"] = round(time.perf_counter() - t_start, 1)

    if OUTPUT_JSON:
        json.dump(results, sys.stdout, indent=2)
        print()
    else:
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        for api in ("completion", "chat"):
            print(f"\n### {api.upper()}")
            for label, m in results[api].items():
                print(f"  {label:>7}:  {m['avg_t/s']:>6.1f} t/s  (±{m['tps_std']})  |  "
                      f"{m['avg_latency_s']:.1f}s latency (±{m['latency_std_s']})")
        print(f"\n  Wall time: {results['wall_s']}s")


if __name__ == "__main__":
    main()
