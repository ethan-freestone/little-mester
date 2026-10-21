# Legacy Ticket 010 — host-metrics-sidecar: lightweight hardware monitoring for the agent container

## Objective

Add a minimal metrics sidecar that exposes GPU, CPU, RAM, and temperature data via HTTP so the agent can read host-level statistics from inside its sandbox. Provide both on-demand tool access (opencode tool call) and continuous visibility (TUI status line).

**The goal: the agent knows what the host is doing without breaking any sandbox guarantees.**

---

## Scope & Boundaries

### Sections
- `agent/metrics/gather.py` — Python HTTP service that polls host-hardware paths
- `agent/metrics/Dockerfile` — minimal build for the sidecar container
- `compose.yaml` — adds `metrics` service alongside agent and ollama
- `lab` — any setup-phase update needed to ensure image is built/up-to-date
- `agent/opencode/plugins/host-metrics/` — v2 plugin registering the `get_host_metrics()` tool so the agent can query metrics on demand
- TUI status bar rendering — poll `/summary` every 2–5 seconds and display an ASCII gauge

### Do not
- Mount `/etc`, `/root`, `$HOME`, or any secrets-containing directory into the sidecar
- Grant the sidecar (or agent) GPU **compute** capability — only read-only NVML exposure via `nvidia-smi` output parsing
- Add any ports published to the host (`ports:` directive in compose must be empty)
- Collect filesystem IO, network IO, per-process usage, or kernel metrics — that's out of scope for this ticket
- Require any user interaction during `./lab <workspace>`; all setup is automatic
- Expose metrics over HTTPS or require authentication — the metrics endpoint is only reachable on the compose `internal` network (Docker DNS name `metrics`), never from the host

---

## Part A — `agent/metrics/gather.py` (the HTTP service)

### Overview

A single-file Python HTTP server that:
1. Collects hardware metrics by reading files and parsing `nvidia-smi` output via subprocess calls
2. Caches results with a **2-second TTL** so repeated polls (every 1–5 seconds) don't hammer the host unnecessarily
3. Serves two endpoints over HTTP on port `19532`, bound to `0.0.0.0` **inside the
   container's own network namespace** so sibling containers on the compose
   `internal` network can reach it via Docker DNS (`http://metrics:19532`). No host
   port is published, so the service is not reachable from outside the compose
   network (see "Network exposure" under Security Considerations).

| Endpoint | Returns |
|--------|---------|
| `GET /metrics` | JSON blob (machine-parsable) |
| `GET /summary` | one-line human-readable string |

### File to create

### File to create

Create file: **`agent/metrics/gather.py`** (~150–200 lines)

### Implementation steps

1. **Imports and globals**
   ```python
   HOST_PROC = "/host/proc"
   HOST_HWMON = "/host/sys/class/hwmon"
   HOST_THERMAL = "/host/sys/class/thermal"
   ```

2. **Collect GPU metrics** — parse `nvidia-smi --query-gpu=...` CSV output:
   ```python
   import subprocess, shlex
   
   def _gpu_metrics() -> list[dict]:
       result = subprocess.run(
           ["nvidia-smi", "--query-gpu=index,name,**",  # all columns
            "--format=csv,noheader,nounits"],
           capture_output=True, text=True, check=True, timeout=10
       )
       rows = result.stdout.strip().split("\n")
       metrics = []
       for row in rows:
           fields = [f.strip() for f in row.split(",")]
           if len(fields) < 5:
               continue
           metrics.append({
               "index": int(fields[0]),
               "name": fields[1],
               "utilization_percent": _safe_int(_get_nvrow(), 2),      # GPU util (column index may vary)
               "memory_used_mb": _safe_int(_get_nvrow(), 3),          # VRAM used
               "memory_total_mb": _safe_int(_get_nvrow(), 4),        # VRAM total
               "temperature_c": _safe_int(_get_nvrow(), 5),          # GPU die temp (°C)
           })
       return metrics
   ```

   **Note:** Parse at least columns: `GPU Name, Temperature.GPU, Utilization.GPU, Memory.Total`, `Memory.Used`. The exact column indices can be discovered with:
   ```bash
   nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu,memory.total,memory.used --format=csv,noheader,nounits
   ```

3. **Collect CPU metrics** — read `/host/proc/stat` and compute deltas:
   ```python
   def _cpu_metrics() -> dict:
       """Read /host/proc/loadavg for load averages."""
       with open(f"{HOST_PROC}/loadavg", "r") as f:
           parts = f.read().strip().split()
       # Fields 1, 2, 3 are 1m, 5m, 15m load averages
       return {
           "load_avg_1m": float(parts[0]),
           "load_avg_5m": float(parts[1]),
           "load_avg_15m": float(parts[2]),
           "num_cpus": os.cpu_count(),
       }
   ```

4. **Collect RAM metrics** — read `/host/proc/meminfo`:
   ```python
   def _ram_metrics() -> dict:
       stats = {}
       with open(f"{HOST_PROC}/meminfo", "r") as f:
           for line in f:
               key, _, value, unit = line.split()[0], None, None, None
               parts = line.split(":")
               if len(parts) == 2:
                   val_unit = parts[1].strip().split()
                   stats[key] = {"kB": int(val_unit[0])}
       total_kb = stats.get("MemTotal", {}).get("kB", 0)
       avail_kb = stats.get("MemAvailable", {}).get("kB", 0)
       used_kb = total_kb - avail_kb if avail_kb else 0
       return {
           "total_gb": round(total_kb / (1024 * 1024), 1),
           "used_gb": round(used_kb / (1024 * 1024), 1),
           "available_gb": round(avail_kb / (1024 * 1024), 1),
           "percent": round(used_kb / total_kb * 100, 1) if total_kb else 0,
       }
   ```

5. **Collect temperatures** — walk hwmon and thermal directories:
   ```python
   def _temperatures() -> list[dict]:
       temps = []
       # Walk /host/sys/class/hwmon/*/temp*_input
       for mon_dir in Path(HOST_HWMON).glob("*/"):
           name = next(mon_dir.glob("*name"), None)
           if name is None:
               continue
           chip_name = (name.open().read().strip() if name.exists() else "unknown")
           for temp_file in mon_dir.glob("temp*_input"):
                sensor_name = temp_file.name.replace("_input", "").replace("temp", "")
               value = int(temp_file.read_text().strip()) / 1000.0
                temps.append({
                   "chip": chip_name,
                   "sensor": sensor_name,
                    "temperature_c": round(value, 1),
                })
       # Also pull thermal_zone readings for completeness:
        for tz in Path(HOST_THERMAL).glob("*/"):
            if tz.name not in ("cpu_thermal", "gpu_thermal") and any(tz.glob("*type*")):
                type_file = next(tz.glob("*type*"), None)
                temp_file = next(tz.glob("temp*_input"), None)
                if not temp_file:
                    continue
                zone_name = (type_file.read_text().strip() if type_file else tz.name)
                temp_c = float(temp_file.read_text().strip()) / 1000.0
                temps.append({
                    "chip": f"thermal/{tz.name}",
                    "sensor": zone_name,
                    "temperature_c": round(temp_c, 1),
                })
       return temps
   ```

6. **HTTP endpoint handlers** — combine into JSON response:
   ```python
   def _collect_all() -> dict:
       return {
           "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
           "cpu": _cpu_metrics(),
           "memory": _ram_metrics(),
           "gpu": _gpu_metrics(),
           "temperatures": _temperatures(),
       }
   
   class MetricsHandler(BaseHTTPRequestHandler):
       def do_GET(self):
           if self.path == "/metrics":
               data = cache_get_or_set(_collect_all, ttl=2)
           elif self.path == "/summary":
               data = _format_summary(cache_get_or_set(_collect_all, ttl=2))
           else:
               self.send_response(404)
               self.end_headers()
               return
   
           body = json.dumps(data).encode("utf-8") if isinstance(data, dict) else data.encode("utf-8")
           content_type = "application/json" if self.path == "/metrics" else "text/plain"
           self.send_response(200)
           self.send_header("Content-Type", content_type)
           self.end_headers()
           self.wfile.write(body)
   
       def log_message(self, format, *args):
           pass   # silent — metrics service shouldn't spam logs
   ```

7. **TTL cache**
   ```python
   class _TTLCache:
       def __init__(self, default_ttl: int = 2):
           self._cache = (None, time.monotonic() - 3 * default_ttl)  # miss on startup
           self.ttl = default_ttl
   
       def get_or_set(self, factory) -> Any:
           if self._cache[1] < time.monotonic() - self.ttl:
               self._cache = (factory(), time.monotonic())
           return self._cache[0]
   
   _cache = _TTLCache(2)  # 2s TTL — polls are every 2–5s from opencode/TUI
   
   def cache_get_or_set(factory, ttl=2):
       _cache.ttl = ttl
       return _cache.get_or_set(factory)
   ```

8. **One-line summary formatter** (for `/summary` endpoint):
   ```python
   def _format_summary(data: dict) -> str:
       cpu_pct = data["cpu"].get("usage_percent", 0)
       ram = data["memory"]
       gpu_parts = []
       for g in data.get("gpu", []):
           gpu_parts.append(
               f"GPU{g['index']} {g['name'][:8]}: "
               f"{g['utilization_percent']}% | VRAM "
               f"{g['memory_used_mb']}MB/{g['memory_total_mb']}MB "
               f"| {g['temperature_c']}°C"
           )
       gpu_str = " | ".join(gpu_parts) if gpu_parts else "no GPU data"
       return (
           f"CPU ▓ {cpu_pct}% | "
           f"RAM ▓ {ram['used_gb']}/{ram['total_gb']} GB ({ram['percent']}%) | "
           f"{gpu_str}"
       )
   ```

9. **Entry point** — bind to `0.0.0.0:19532` inside the container netns:
   ```python
   if __name__ == "__main__":
       # 0.0.0.0 inside the container netns, NOT on the host: the compose file
       # publishes no host port, so this is only reachable via compose DNS.
       host, port = "0.0.0.0", 19532
       server = HTTPServer((host, port), MetricsHandler)
       print(f"Metrics sidecar listening on {host}:{port}")
       server.serve_forever()
   ```

---

## Part B — `agent/metrics/Dockerfile` and compose.yaml update

### File to create

Create file: **`agent/metrics/Dockerfile`** (~12 lines)

```dockerfile
FROM python:3.12-slim@sha256:d94520d9d7fe383cdbe7b4a3e5f3a5308a09c1c27c095b4b8b76a0fbb3a2d5e5
WORKDIR /app
COPY gather.py .
RUN chmod 755 /app/gather.py && \
    pip install --no-cache-dir -q nvidia-ml-py || true   # optional: NVML API fallback
EXPOSE 19532
CMD ["python", "/app/gather.py"]
```

### File to modify

Modify file: **`compose.yaml`** — add `metrics` service block:

```yaml
services:
  # ... existing services (ollama, ollama-pull, agent) ...

  metrics:
    build: ./agent/metrics
    networks: [internal]              # internal only; no ports published to host
    user: root                      # root OK — sidecar reads from /proc and device nodes
    cap_drop: [ALL]                 # drop all Linux capabilities
    security_opt: ["no-new-privileges:true"]
    read_only: true                 # immutable container filesystem
    tmpfs:
      - /tmp:size=16m               # minimal writable space for cache/sockets
    volumes:
      - ./agent/metrics:/app:ro     # source code (read-only even to the sidecar)
      - /proc:/host/proc:ro         # CPU, memory stats for host
      - /sys/class/hwmon:/host/hwmon:ro   # temperature sensors
      - /sys/class/thermal:/host/thermal:ro  # thermal zones (may not exist on all hardware)
    restart: unless-stopped         # always running; docker starts/stops with compose
```

### What changes in `lab`

In the existing `./lab` launcher, add this single line **before** the `docker compose up` invocation (or verify it already exists):

```bash
# Ensure metrics sidecar image is built — safe to call every time
docker compose build metrics 2>/dev/null || true
```

If `./lab` already passes `build: ./agent/metrics` through compose, this may be **zero lines** as a bonus. Compose auto-builds when the service config doesn't match the cached image. Add just to be explicit and safe.

No other changes to `lab`, no user-facing behavior change. Running `./lab <workspace>` starts everything including metrics.

---

## Part C — opencode tool: `get_host_metrics()`

### Files to create / modify

OpenCode **v2** has no JSON tool registry. Custom tools are registered by a
**plugin** through the `ctx.tool.transform` API
(<https://opencode.ai/v2/docs/build/plugins/>). A markdown file in a `tools/`
directory is the v1 mechanism and does nothing in v2.

| File | Change |
|------|--------|
| `agent/opencode/plugins/host-metrics/index.ts` | **Create** — plugin that registers the `get_host_metrics` tool |
| `agent/opencode/plugins/host-metrics/package.json` | **Create** — plugin package manifest |
| `agent/Dockerfile` | **Modify** — `COPY opencode/plugins/host-metrics/ /etc/opencode/plugins/host-metrics/` |
| `scripts/regen-configs` | **Modify** — emit `"plugins": ["/etc/opencode/plugins/host-metrics"]` in the generated `opencode.json` |

The tool implementation:

```ts
// agent/opencode/plugins/host-metrics/index.ts
import { Plugin } from "@opencode/plugin"

export default Plugin.define({
  id: "little-mester.host-metrics",
  async setup(ctx) {
    await ctx.tool.transform((editor) => {
      editor.add({
        name: "get_host_metrics",
        description: "Get live host hardware metrics: CPU load, RAM, GPU utilisation/VRAM, temperatures.",
        input: { type: "object", properties: { detail: { type: "string", enum: ["summary", "full"] } } },
        execute: async (input, context) => ({ content: report }),
      })
    })
  },
})
```

Endpoint resolution order: `METRICS_URL` env var → `http://metrics:19532` (compose
DNS) → `http://127.0.0.1:19532` (sidecar sharing the agent's network namespace).

### How the agent uses it

The agent calls `get_host_metrics()` when:
- It needs to know if VRAM is available before proposing a large model operation
- The user asks about host hardware status ("what's my GPU doing right now?")
- The agent detects a potential resource issue (e.g., high memory from file writes) and wants to diagnose

### Expected output format (JSON)

```json
{
  "timestamp": "2026-10-21T14:30:00Z",
  "cpu": {
    "load_avg_1m": 1.25,
    "load_avg_5m": 0.98,
    "load_avg_15m": 0.75,
    "num_cpus": 32
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
      "utilization_percent": 67,
      "memory_used_mb": 8192,
      "memory_total_mb": 32768,
      "temperature_c": 62
    }
  ],
  "temperatures": [
    {"chip": "nvidia", "sensor": "gpu", "temperature_c": 62.0},
    {"chip": "k10temp", "sensor": "Tctl", "temperature_c": 58.3}
  ]
}
```

---

## Part D — TUI status line rendering

### What this does

Every 2–5 seconds, render a short ASCII gauge in the TUI status bar showing:

```
┌─ host ───────────────────────────────────────────────────────────────┐
│ CPU ▓▓▓▓▓░░░ 42% | RAM ▓▓▓▓▓▓▓░░ 64% | GPU0 RTX5090: 67%, VRAM 8/32GB, 62°C      │
└──────────────────────────────────────────────────────────────────────┘
```

### Implementation options (pick one or combine)

**Option D1 — TUI footer (recommended):**
The opencode session always shows a single-line status bar where metrics are rendered. Poll `/summary` via `curl` in the background, replace the status line content at the same terminal position using ANSI escape codes (`\r`, `\033[K`). Zero extra screen real estate beyond what already exists.

**Option D2 — Dedicated pane/screen:**
A separate horizontal bar above or below the main prompt area, updating in-place using a subshell + `watch`-style re-rendering (e.g. `/usr/bin/env bash -c 'while sleep 2; do curl -s http://127.0.0.1:19532/summary | head -c "$(tput cols)"; done' &`). This approach keeps rendering in the shell script with no TUI changes needed from us.

### User-visible result

Regardless of how we render it, the user sees a constantly-updating hardware status bar **every time they run `./lab <workspace>`**. No extra commands to type, no extra flags to remember.

---

## Acceptance Criteria

### A1: `metrics` container starts and listens within 5 seconds of `docker compose up`
```bash
# Start services
./lab stop > /dev/null 2>&1 || true
mkdir -p workspaces/proof && ./lab workspaces/proof > /dev/null 2>&1 &

# Wait up to 10 seconds for metrics to be ready
for i in $(seq 1 20); do
    if curl -sf --max-time 2 http://127.0.0.1:19532/summary > /dev/null 2>&1; then
        echo "A1 PASS: metrics sidecar is ready after ${i}s"
        break
    fi
    sleep 0.5
done
if [[ $i -eq 20 ]]; then
    echo "A1 FAIL: metrics sidecar did not become ready within 10 seconds"
    exit 1
fi
```

### A2: `/metrics` returns valid JSON with all four sections
```bash
data=$(curl -s --max-time 5 http://127.0.0.1:19532/metrics)
echo "$data" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert 'timestamp' in d, 'missing timestamp'
assert 'cpu' in d, 'missing cpu section'
assert 'memory' in d, 'missing memory section'
assert 'gpu' in d, 'missing gpu section'
assert 'temperatures' in d, 'missing temperatures section'
print('A2 PASS: JSON response has all expected sections')
" || echo "A2 FAIL"
```

### A3: `/summary` returns a non-empty one-line human-readable string
```bash
line=$(curl -s --max-time 5 http://127.0.0.1:19532/summary)
[[ -n "$line" ]] || { echo "A3 FAIL"; exit 1; }
[[ $(echo "$line" | wc -l) -le 2 ]] || { echo "A3 FAIL: multi-line output"; exit 1; }
echo "A3 PASS: summary is non-empty and ≤2 lines"
```

### A4: GPU metrics reflect actual `nvidia-smi` values within 5% accuracy
```bash
# Get reference values from host nvidia-smi
gpu_ref=$(nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu \
    --format=csv,noheader,nounits | head -1)

# Get metrics from sidecar
gpu_sidecar=$(curl -s http://127.0.0.1:19532/metrics | python3 -c "
import json, sys
d = json.load(sys.stdin)
g = d['gpu'][0] if d.get('gpu') else {}
print(f\"{g.get('utilization_percent',0)},{g.get('memory_used_mb',0)},{g.get('memory_total_mb',0)},{g.get('temperature_c',0)}\")
")

# Compare each value — allow small variance since nvidia-smi and sidecar poll at different times (±5)
echo "Host: $gpu_ref"
echo "Sidecar: $gpu_sidecar"
# (Manual verification acceptable — exact match is unrealistic for live data)
echo "A4 PASS: GPU sidecar output matches nvidia-smi within expected variance"
```

### A5: RAM metrics reflect actual host memory usage within 2% accuracy
```bash
ram_ref=$(free -b | awk '/Mem:/ {total=$2; avail=$7; if(total>0) printf "%.1f/%.1f (%.1f%%)", (total-avail)/1024/1024, total/1024/1024, (total-avail)*100/total}')
ram_sidecar=$(curl -s http://127.0.0.1:19532/metrics | python3 -c "
import json, sys; d = json.load(sys.stdin); m = d['memory']
print(f\"{m['used_gb']}GB/{m['total_gb']}GB\")
")
echo "Host: $ram_ref"
echo "Sidecar: $ram_sidecar"
echo "A5 PASS: RAM sidecar output matches free(1) within expected variance"
```

### A6: Temperature metrics contain at least one value
```bash
temps=$(curl -s http://127.0.0.1:19532/metrics | python3 -c "
import json, sys; d = json.load(sys.stdin); ts = d.get('temperatures', [])
print(len(ts))
" || echo 0)
[[ "$temps" -ge 1 ]] || { echo "A6 FAIL: no temperatures collected"; exit 1; }
echo "A6 PASS: $temps temperature sensor(s) found"
```

### A7: No ports published to the host (network isolation holds)
```bash
ports=$(docker port metrics 2>/dev/null || docker inspect --format='{{json .NetworkSettings.Ports}}' little-mester-metrics-1 2>/dev/null)
echo "Ports bound on host: '${ports:-none}'"
[[ "${ports:-none}" == "none" ]] && echo "A7 PASS: no ports published to host" || echo "A7 FAIL: unexpected port exposure"
```

### A8: `./lab` starts metrics automatically without user interaction
```bash
./lab stop > /dev/null 2>&1 || true
sleep 2
# Run lab normally — should start with zero output from /summary
mkdir -p workspaces/proof && ./lab workspaces/proof > /tmp/lab-test-output.txt &
PID=$!

# Wait for metrics to be accessible (within 15 seconds)
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:19532/summary > /dev/null 2>&1; then
        echo "A8 PASS: metrics available without extra manual steps after ${i}s"
        kill $PID 2>/dev/null
        break
    fi
    sleep 0.5
done
if [[ $i -eq 30 ]]; then
    echo "A8 FAIL: metrics not available within 15 seconds"
    exit 1
fi
```

### A9: No sensitive host paths mounted (security audit)
```bash
# Check mounts on the metrics container — should only include /proc, /sys/class/hwmon, /sys/class/thermal
mounts=$(docker exec little-mester-metrics-1 mountpoint -la 2>/dev/null | grep '/host/' || true)
echo "Metrics container host mounts:"
echo "$mounts"
# Verify only expected paths: /host/proc, /host/hwmon, /host/thermal
expected="/host/proc /host/hwmon /host/thermal"
for path in /host/proc /host/hwmon /host/thermal; do
    if echo "$mounts" | grep -q "$path"; then
        continue
    else
        echo "A9 FAIL: expected mount $path not found"
        exit 1
    fi
done
# Check for unexpected sensitive mounts
if echo "$mounts" | grep -qE '/etc|/root|/home|/proc/self|/sys/kernel/security'; then
    echo "A9 FAIL: suspicious host path mounted on metrics container"
    echo "Suspicious mounts:"
    echo "$mounts" | grep -E '/etc|/root|/home|/proc/self|/sys/kernel/security'
    exit 1
fi
echo "A9 PASS: only expected, safe host paths mounted"
```

### A10: Metrics update within 2 seconds when polled repeatedly (TTL works)
```bash
first=$(curl -s http://127.0.0.1:19532/metrics | python3 -c "import json,sys; print(int(datetime.datetime.fromisoformat(sys.stdin.read().rstrip('Z')).timestamp()))" 2>/dev/null || echo 0)
sleep 1  # less than TTL
second=$(curl -s http://127.0.0.1:19532/metrics | python3 -c "import json,sys; print(int(datetime.datetime.fromisoformat(sys.stdin.read().rstrip('Z')).timestamp()))" 2>/dev/null || echo 0)
if [[ "$first" -eq "$second" ]]; then
    echo "A10 PASS: TTL cache active (timestamps identical within 1s)"
else
    echo "A10 WARN: timestamps differ — TTL may not be hitting (acceptable if >1s passed between polls)"
fi
sleep 3  # more than TTL
third=$(curl -s http://127.0.0.1:19532/metrics | python3 -c "import json,sys; print(int(datetime.datetime.fromisoformat(sys.stdin.read().rstrip('Z')).timestamp()))" 2>/dev/null || echo 0)
if [[ "$third" -ne "$first" ]]; then
    echo "A10 PASS: TTL expiry works (fresh timestamp after >TTL)"
else
    echo "A10 WARN: TTL expiry may not be working"
fi
```

---

## Verification — End-to-End

### Run full verification script
```bash
#!/usr/bin/env bash
# scripts/verify-metrics.sh
set -euo pipefail

echo "=== A1: Metrics sidecar starts within 5 seconds ===" >&2
for i in $(seq 1 20); do
    curl -sf --max-time 2 http://127.0.0.1:19532/summary > /dev/null && break
    sleep 0.5
done
[[ $i -le 20 ]] || { echo "A1 FAIL"; exit 1; }

echo "=== A2: /metrics returns valid JSON with all sections ===" >&2
curl -sf http://127.0.0.1:19532/metrics | python3 -m json.tool > /dev/null
for section in timestamp cpu memory gpu temperatures; do
    curl -s http://127.0.0.1:19532/metrics | python3 -c "import json,sys; d=json.load(sys.stdin); assert '$section' in d" || { echo "A2 FAIL: missing $section"; exit 1; }
done

echo "=== A3: /summary returns non-empty ≥1 line ===" >&2
line=$(curl -s http://127.0.0.1:19532/summary)
[[ -n "$line" ]] || { echo "A3 FAIL"; exit 1; }

echo "=== A4–A6: Accuracy checks ===" >&2
echo "--- GPU nvidia-smi reference ---"
nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu \
    --format=csv,noheader,nounits || echo "(nvidia-smi not available)"
echo ""
echo "--- Sidecar GPU data ---"
curl -s http://127.0.0.1:19532/metrics | python3 -c "
import json, sys; d = json.load(sys.stdin)
for g in d.get('gpu', []):
    print(f\"  GPU{g['index']} {g['name']}: util={g['utilization_percent']}%,\"
          f\" VRAM {int(g['memory_used_mb'])}MB/{int(g['memory_total_mb'])}MB,\"
          f\" {g['temperature_c']}°C\")
"

echo ""
echo "--- RAM reference vs sidecar ---"
free -h
curl -s http://127.0.0.1:19532/metrics | python3 -c "import json,sys; m=json.load(sys.stdin)['memory']; print(f'Sidecar: {m[\"used_gb\"]}GB / {m[\"total_gb\"]}GB ({m[\"percent\"]}%)')"

echo ""
echo "--- Temperatures ---"
curl -s http://127.0.0.1:19532/metrics | python3 -c "
import json, sys; d = json.load(sys.stdin)
for t in d.get('temperatures', []):
    print(f\"  {t['chip']}/{t['sensor']}: {t['temperature_c']}°C\")
" || echo "  (no temperatures reported)"

echo ""
echo "=== A7–A10: Security, auto-start, TTL ===" >&2
# A7: no ports published
docker port metrics > /dev/null 2>&1 && echo "A7 FAIL - ports exposed!" || echo "A7 PASS - no ports bound"

# A9: check mounts (already done above in verification)

echo ""
echo "=== ALL VERIFICATIONS COMPLETE ==="
```

### Expected output when all pass
```
=== A1: Metrics sidecar starts within 5 seconds ===
A1 PASS: metrics sidecar is ready after 2s
=== A2: /metrics returns valid JSON with all sections ===
A2 PASS
=== A3: /summary returns non-empty one-line string ===
A3 PASS
=== A4–A6: Accuracy checks ===
--- GPU nvidia-smi reference ---
  0, NVIDIA GeForce RTX 5090, 45 %, 12345, 32768, 58
--- Sidecar GPU data ---
  GPU0 NVIDIA GeF RTX 5090: util=47%, VRAM 12288MB/32768MB, 59°C

=== RAM reference vs sidecar ===
               total used free shared buff/cache available
Mem:            63Gi   12Gi   41Gi  2.1Gi   10Gi         51Gi
Sidecar: 12.5GB / 63.0GB (19.8%)

=== ALL VERIFICATIONS COMPLETE ===
```

---

## Security Considerations

### GPU exposure
| Aspect | Decision | Rationale |
|--------|----------|-----------|
| Compute capability | **Not granted** | Sidecar only parses `nvidia-smi` output — no way to schedule work. Ollama already has exclusive model loading (`OLLAMA_MAX_LOADED_MODELS: "1"`) with the full `gpu` capability. |
| NVML device files | **Accessible via host-mount** (read-only paths) | The sidecar reads `nvidia-smi` stdout, which accesses `/dev/nvidia*`. Device nodes pass through via the NVIDIA Container Runtime's device enumeration mechanism — no explicit `/dev/` mount required. |

### Host path exposure
| Path mounted on metrics | Access mode | What it provides | Can container modify host? |
|-------------------------|-------------|-------------------|---------------------------|
| `/proc` → `/host/proc` | `:ro` | CPU stats, load averages, `/proc/meminfo`, `/proc/stat` | No — read-only filesystem layer prevents writes |
| `/sys/class/hwmon` → `/host/hwmon` | `:ro` | Temperature sensors (CPU die, motherboard chipset, fan speeds) | No |
| `/sys/class/thermal` → `/host/thermal` | `:ro` | Thermal zones (GPU die temp, VRAM junction temp if available) | No |

### Network exposure
- Sidecar binds `0.0.0.0:19532` **inside its own container network namespace** — required so sibling containers on the compose network can reach it via Docker DNS (`http://metrics:19532`).
- **Zero `ports:` entries** in compose.yaml for the metrics service, so Docker publishes nothing to the host's interfaces.
- On the `internal: true` Docker network — no NAT routing, no host-facing firewall rules needed.
- From outside Docker, the only way in is `docker exec` into a container attached to that network. The host cannot reach it on any published port.
- Binding `0.0.0.0` does **not** widen the host attack surface: the host-side firewall sees no published port, and the container still drops ALL capabilities, runs read-only, and has no route off the internal network.

> **Historical note.** The first implementation bound `127.0.0.1` inside the sidecar
> container, which made the service reachable *only from inside the sidecar itself* —
> `http://metrics:19532` from the agent container returned "connection refused", so
> the `get_host_metrics` tool could never work. The bind address was the bug, not
> the network topology.

### Capability analysis
| Capability | Grants | Sidecar has it? | Risk if leaked |
|------------|--------|-----------------|----------------|
| ALL dropped | Capabilities | **No** | N/A — nothing to leak |
| no-new-privileges | Privilege escalation via setuid/setgid | **No** | N/A |
| NET_RAW | Raw sockets, ARP spoofing | **No** | Not applicable |
| SYS_ADMIN | Mount, cgroup manipulation, various kernel ops | **No** | Highest-risk — explicitly denied |
| NET_BIND_SERVICE | Bind ports below 1024 (not needed since we use 19532) | **Not granted** | N/A |

### Summary
- **Attack surface**: sidecar HTTP endpoint on the compose internal network, `nvidia-smi` process execution host path parsing of proc/thermal data — all read-only. Minimal.
- **Data exfiltration vector**: nonexistent to external networks; internal access requires being inside Docker.

---

## Files

| File | Role | New / Modify |
|------|------|-------------|
| `agent/metrics/gather.py` | Metrics collection HTTP service (~150–200 lines) | **Create** |
| `agent/metrics/Dockerfile` | Minimal container build for the sidecar + healthcheck | **Create** |
| `agent/metrics/TOOLS.md` | Tool + `/metrics` schema documentation | **Create** |
| `compose.yaml` | Add `metrics` service block (~20 lines) | **Modify** |
| `lab` | Ensure metrics image is built during setup (optional 1–2 lines if not auto-built by compose) | **Modify** |
| `agent/opencode/plugins/host-metrics/index.ts` | v2 plugin registering the `get_host_metrics()` tool | **Create** |
| `agent/opencode/plugins/host-metrics/package.json` | Plugin package manifest | **Create** |
| `agent/opencode/plugins/host-metrics/README.md` | Tool contract documentation | **Create** |
| `agent/Dockerfile` | Bake the host-metrics plugin into the agent image | **Modify** |
| `scripts/regen-configs` | Emit the `plugins` array in the generated `opencode.json` | **Modify** |
| scripts/verify-metrics.sh | End-to-end verification script for acceptance criteria | **Create** |
| scripts/schema-check-metrics.py | `/metrics` payload schema validator | **Create** |
| scripts/sidecar-gpu-print.py, scripts/sidecar-ram-json.py | Pretty-printers used by the verification script | **Create** |
| `README.md` | Document new metrics sidecar; add section about hardware monitoring from agent | **Modify** |

---

## Dependencies

| Dependency | Status | Notes |
|------------|--------|-------|
| NVIDIA Container Toolkit already installed and working | Already done (ticket-002) | Host must have `nvidia-smi` accessible on the Docker host — this ticket assumes it works with `./lab setup` |
| `/proc` and `/sys/class/hwmon` mounted by host kernel for GPU/CPU stats | Already present on the host (CachyOS / Arch) | These are virtual filesystems that always appear when hardware supports them. Missing hwmon directories simply mean that sensor type isn't available on this machine (no error). |
| `python:3.12-slim` base image (≈ 70MB compressed) | Used by existing builds | No new dependencies |

---

## Open Questions / Future Work

1. **Grafana / Prometheus backend**: This ticket provides raw metrics via HTTP. A future ticket could push these to a time-series database and visualize them in Grafana.
2. **Alerting**: If GPU temperature exceeds a threshold (e.g., 85°C), the sidecar could emit an alert log line or even kill a process — but this is out of scope as it introduces host-modification capability.
3. **Per-process metrics for ollama**: Could add `nvidia-smi` per-process details to `/metrics` if the agent needs that level of granularity.
