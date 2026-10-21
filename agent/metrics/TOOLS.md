# tools/get_host_metrics — Host Hardware Metrics Tool

The sidecar's `/metrics` payload schema, and the agent tool that exposes it.

## Tool

`get_host_metrics` is registered by the
[`host-metrics` plugin](../opencode/plugins/host-metrics/) — see
[its README](../opencode/plugins/host-metrics/README.md) for the tool contract
(arguments, output format, endpoint resolution order).

From inside the agent container the same data is available over raw HTTP:

```bash
curl -s http://metrics:19532/metrics   # JSON
curl -s http://metrics:19532/summary   # one-line string
```

`metrics` is the compose service name on the shared `internal` network. The sidecar
publishes **no** host port, so this URL does not resolve from the host — only from
containers attached to that network.

## `/metrics` schema

```json
{
  "timestamp": "2026-10-21T14:30:00Z",
  "cpu": {
    "model": "AMD Ryzen AI 9 HX 370 w/ Radeon 890M",
    "load_avg_1m": 1.25,
    "load_avg_5m": 0.98,
    "load_avg_15m": 0.75,
    "num_cpus": 24
  },
  "memory": {
    "total_gb": 64.0,
    "used_gb": 41.0,
    "available_gb": 23.0,
    "percent": 64.1
  },
  "gpu": [
    {
      "index": 0,
      "name": "NVIDIA GeForce RTX 5090",
      "temperature_c": 62,
      "utilization_percent": 67,
      "memory_total_mb": 32768,
      "memory_used_mb": 8192,
      "memory_free_mb": 24576
    }
  ],
  "temperatures": [
    { "chip": "k10temp", "sensor": "1", "temperature_c": 58.3 },
    { "chip": "nvme", "sensor": "1", "temperature_c": 34.1 }
  ]
}
```

| Section | Notes |
|---------|-------|
| `cpu` | `model` comes from `/host/proc/cpuinfo`; load averages from `/host/proc/loadavg`. `num_cpus` is the sidecar container's CPU count. |
| `memory` | From `/host/proc/meminfo`. `used_gb = total - available`. |
| `gpu` | One entry per GPU, parsed from `nvidia-smi` CSV. Empty list when no NVIDIA GPU is present. |
| `temperatures` | From `/host/hwmon` and `/host/thermal` (CPU die, chipset, NVMe, GPU junction). Empty list when no sensors are exposed. |

Validate a payload with:

```bash
curl -s http://metrics:19532/metrics | python3 scripts/schema-check-metrics.py
```

## Caching

All values are cached for 2 seconds (TTL), so repeated polls are effectively free.
`timestamp` reflects collection time, not request time — two calls within the TTL
window return the same timestamp.

## When to Use

- Before proposing large model operations that depend on available VRAM
- When the user asks about host hardware status ("what's my GPU doing?")
- When diagnosing unexpected behaviour (e.g. model fell back to CPU — is RAM
  pressure a cause?)

## Notes

- Available only when the stack is started with the metrics sidecar (the default).
- If the endpoint is unreachable (e.g. the sidecar is not running), the tool
  returns a diagnostic naming every URL it tried rather than throwing.
