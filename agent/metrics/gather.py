#!/usr/bin/env python3
"""Host metrics endpoint for the agent's deliberately narrow metrics view.

The host-Docker deployment listens on 0.0.0.0:19532 inside its private container
namespace so sibling containers can use Docker DNS (http://metrics:19532); compose.yaml
publishes no host port. The separate VM deployment explicitly sets METRICS_BIND_HOST to
127.0.0.1 and forwards only this API endpoint through QEMU guestfwd. Neither deployment
exposes host files or a general host route to the agent.

Exposes three endpoints:
    GET /metrics  -- JSON blob with all four metric categories
    GET /summary  -- one-line human-readable string
    GET /history?n=N -- last N ring-buffer samples (default 120 = 60s)
"""

import collections
import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Host paths (mounted via compose bind mounts as :ro).
# These MUST match the mount destinations in compose.yaml:
#   /proc            -> /host/proc
#   /sys/class/hwmon -> /host/hwmon
#   /sys/class/thermal -> /host/thermal
# Env overrides exist so the collector can be run outside Docker for debugging.
HOST_PROC = os.environ.get("METRICS_HOST_PROC", "/host/proc")
HOST_HWMON = os.environ.get("METRICS_HOST_HWMON", "/host/hwmon")
HOST_THERMAL = os.environ.get("METRICS_HOST_THERMAL", "/host/thermal")

# The Docker deployment uses all interfaces in its private container namespace;
# the VM host process overrides this to 127.0.0.1 before launch.
BIND_HOST = os.environ.get("METRICS_BIND_HOST", "0.0.0.0")
BIND_PORT = int(os.environ.get("METRICS_BIND_PORT", "19532"))


# ---------------------------------------------------------------------------
# Ring-buffer sampler - replaces TTL cache for low-latency live sampling
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_latest: dict | None = None          # Current snapshot (served by GET /metrics)
_history: collections.deque = collections.deque(maxlen=240)  # 120 s at 2 Hz


def _collect_gpu():
    """Collect GPU metrics via NVML or nvidia-smi fallback."""
    if not (_NVML_OK or _HAS_SMI):
        return []
    if _NVML_OK:
        try:
            return _collect_gpu_nvml()
        except Exception:  # noqa: S110, BLE001 — hardware probing
            pass
    if _HAS_SMI:
        return _collect_gpu_smi()
    return []


def _collect_cpu():
    """Read /host/proc/loadavg and return load averages + core count."""
    try:
        with open(f"{HOST_PROC}/loadavg", "r") as fh:
            parts = fh.read().strip().split()
        load_1 = float(parts[0])
        load_5 = float(parts[1])
        load_15 = float(parts[2])
    except (OSError, IndexError, ValueError):
        load_1 = load_5 = load_15 = 0.0

    num_cpus = os.cpu_count() or 1

    return {
        "model": _collect_cpu_model(),
        "load_avg_1m": round(load_1, 2),
        "load_avg_5m": round(load_5, 2),
        "load_avg_15m": round(load_15, 2),
        "num_cpus": num_cpus,
    }


def _aggregate():
    """Collect *all* hardware metrics and return as a single dict."""
    return {
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cpu": _collect_cpu(),
        "memory": _collect_ram(),
        "gpu": _collect_gpu(),
        "temperatures": _collect_temperatures(),
    }


def _format_summary(data: dict):
    """Create a compact one-liner for the /summary endpoint."""
    cpu = data.get("cpu", {})
    mem = data.get("memory", {})
    gpus = data.get("gpu", [])
    temps = data.get("temperatures", [])

    load_str = f"load {cpu.get('load_avg_1m', 0)}/{cpu.get('load_avg_5m', 0)}"
    ram_str = f"RAM {mem.get('used_gb', '?')}/{mem.get('total_gb', '?')}GB ({mem.get('percent', '?')}%)"

    gpu_parts = []
    for g in gpus:
        name_short = g["name"][:12]
        used_gb = int(g.get("memory_used_mb", 0)) >> 10
        total_gb = int(g.get("memory_total_mb", 0) >> 10)
        gpu_parts.append(
            f"{g['index']} {name_short}: "
            f"util={g['utilization_percent']}%, "
            f"VRAM {used_gb}/{total_gb}GB, "
            f"{g.get('temperature_c', '?')}C"
        )

    temp_parts = [
        f"{t['chip']}/{t['sensor']}={t['temperature_c']}C"
        for t in temps
        if t.get("temperature_c") is not None
    ]

    gpu_str = " | ".join(gpu_parts) if gpu_parts else "no GPU data"
    temp_str = f" | T: {' / '.join(temp_parts)}" if temp_parts else ""

    return (
        f"[{data['timestamp']}] CPU: {load_str}  |  {ram_str}  |  "
        f"{gpu_str}{temp_str}"
    )


def _sampler():
    """Background thread that polls _aggregate() every 500 ms and maintains
    the `_latest` snapshot plus a rolling-ring of recent samples."""
    global _latest
    while True:
        with _lock:
            snap = _aggregate()
            g = (snap["gpu"] or [{}])[0]   # first GPU, empty dict if no data
            vram_pct = round(
                100 * g.get("memory_used_mb", 0)
                / max(g.get("memory_total_mb", 1), 1), 4
            )

        history_entry = collections.OrderedDict([
            ("t", snap["timestamp"]),
            ("ts", time.monotonic()),       # monotonic for interval logic
            ("gpu_util", g.get("utilization_percent", 0)),
            ("vram_pct", vram_pct),
            ("gpu_temp", int(g.get("temperature_c", 0))),
            ("cpu_load", snap["cpu"]["load_avg_1m"]),
        ])
        with _lock:
            _latest = snap
            _history.append(history_entry)

        time.sleep(0.5)


# ---------------------------------------------------------------------------
# GPU collectors (NVML first, nvidia-smi fallback)
# ---------------------------------------------------------------------------

_safe_int_count_lock = threading.Lock()

def _safe_int(value, default: int = 0):
    """Parse a string as int; return *default* on any failure."""
    if value is None:
        return default
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return default


# Probe NVML at import time — if pynvml isn't available or the device
# nodes aren't readable, we fall back to nvidia-smi subprocess calls.
_NVML_OK = False
try:
    import pynvml
    pynvml.nvmlInit()
    _NVML_OK = True
except Exception:  # noqa: S110, BLE001 — pynvml may not be installed
    pass

# Probe for nvidia-smi binary at import time (avoids a subprocess check
# on every call).
_HAS_SMI = False
try:
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=True, timeout=5,
    )
    _HAS_SMI = bool(result.stdout.strip())
except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
    pass

# ── Start sampler thread ─────────────────────────────────────
threading.Thread(target=_sampler, daemon=True).start()


def _collect_gpu_nvml():
    gpus = []
    for i in range(pynvml.nvmlDeviceGetCount()):
        h = pynvml.nvmlDeviceGetHandleByIndex(i)
        name = pynvml.nvmlDeviceGetName(h)
        if isinstance(name, bytes):
            name = name.decode()
        util = pynvml.nvmlDeviceGetUtilizationRates(h)
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        gpus.append({
            "index": i,
            "name": name,
            "temperature_c": pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU),
            "utilization_percent": util.gpu,
            "memory_total_mb": mem.total // 2**20,
            "memory_used_mb": mem.used // 2**20,
            "memory_free_mb": mem.free // 2**20,
        })
    return gpus


def _collect_gpu_smi():
    """Parse `nvidia-smi --query-gpu` CSV output and return list of per-GPU dicts."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                ("--query-gpu="
                "name,"
                "temperature.gpu,"
                "utilization.gpu,"
                "memory.total,"
                "memory.used,"
                "memory.free"),
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return []

    lines = result.stdout.strip().split("\n")
    if not lines or not lines[0]:
        return []

    gpus = []
    for idx, line in enumerate(lines):
        fields = [f.strip() for f in line.split(",")]
        name  = fields[0]   if len(fields) > 0 else "unknown"
        temp_c = _safe_int(fields[1])
        util_pct = _safe_int(fields[2])
        mem_total_mb = _safe_int(fields[3])       # memory.total in MB
        mem_used_mb  = _safe_int(fields[4])       # memory.used in MB (nvidia-smi already computes used)
        mem_free_mb  = _safe_int(fields[5]) if len(fields) > 5 else 0

        gpus.append({
            "index": idx,
            "name": name,
            "temperature_c": temp_c,
            "utilization_percent": util_pct,
            "memory_total_mb": mem_total_mb,
            "memory_used_mb": mem_used_mb,
            "memory_free_mb": mem_free_mb,
        })
    return gpus


def _collect_cpu_model():
    """Return the CPU model string from /host/proc/cpuinfo (best effort)."""
    try:
        with open(f"{HOST_PROC}/cpuinfo", "r") as fh:
            for line in fh:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


def _collect_ram():
    """Parse /host/proc/meminfo and return memory stats."""
    total_kb = avail_kb = 0
    try:
        with open(f"{HOST_PROC}/meminfo", "r") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    total_kb = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    avail_kb = int(line.split()[1])
                    break  # found it, stop reading
    except OSError:
        pass

    used_kb = max(0, total_kb - avail_kb) if total_kb else 0
    percent = round(used_kb / total_kb * 100, 1) if total_kb else 0.0

    return {
        "total_gb": round(total_kb / (1024 * 1024), 1),
        "used_gb": round(used_kb / (1024 * 1024), 1),
        "available_gb": round(avail_kb / (1024 * 1024), 1),
        "percent": percent,
    }


def _collect_temperatures():
    """Walk hwmon and thermal-zone dirs for sensor readings."""
    temps = []

    # hwmon (CPU chip temp, motherboard/chipset)
    try:
        hwmon_path = Path(HOST_HWMON)
        if hwmon_path.is_dir():
            for mon in sorted(hwmon_path.iterdir()):
                if not mon.is_dir():
                    continue
                name_files = list(mon.glob("*name*"))
                chip_name = name_files[0].read_text().strip() if name_files else "unknown"
                for tf in sorted(mon.glob("temp*_input")):
                    sensor_label = tf.name.replace("_input", "").replace("temp", "", 1) or "Tctl"
                    raw = float(tf.read_text().strip()) / 1000.0
                    temps.append({
                        "chip": chip_name,
                        "sensor": sensor_label,
                        "temperature_c": round(raw, 1),
                    })
    except OSError:
        pass

    # thermal-zone (GPU die temp, VRAM junction, fan trips)
    try:
        thermal_path = Path(HOST_THERMAL)
        if thermal_path.is_dir():
            for tz in sorted(thermal_path.iterdir()):
                if not tz.is_dir():
                    continue
                type_files = list(tz.glob("*type*")) + list(tz.glob("type"))
                zone_type = type_files[0].read_text().strip() if type_files else "thermal_zone"
                temp_files = list(tz.glob("temp*_input"))
                if not temp_files:
                    continue
                raw = float(temp_files[0].read_text().strip()) / 1000.0
                temps.append({
                    "chip": f"thermal/{tz.name}",
                    "sensor": zone_type,
                    "temperature_c": round(raw, 1),
                })
    except OSError:
        pass

    return temps


# ---------------------------------------------------------------------------
# HTTP server - binds to 0.0.0.0:19532 inside this container's netns
# ---------------------------------------------------------------------------

class _MetricsHandler(BaseHTTPRequestHandler):
    """- /metrics JSON blob   - /summary one-liner   - /history last N samples   - anything else 404."""

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/metrics":
            with _lock:
                data = dict(_latest) if _latest else _aggregate()
            payload = json.dumps(data, indent=2)
            content_type = "application/json"

        elif parsed.path == "/summary":
            with _lock:
                data = dict(_latest) if _latest else _aggregate()
            payload = _format_summary(data)
            content_type = "text/plain"

        elif parsed.path == "/history":
            n = int(query.get("n", ["120"])[0])
            with _lock:
                recent = list(_history)[-n:]
            payload = json.dumps(recent, indent=2)
            content_type = "application/json"

        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Not found\n")
            return

        body = payload.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        """Suppress per-request logs to keep docker logs clean."""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    host = BIND_HOST
    port = BIND_PORT
    server = HTTPServer((host, port), _MetricsHandler)
    print(f"[metrics-sidecar] listening on {host}:{port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("[metrics-sidecar] shut down", file=sys.stderr)
        server.server_close()


if __name__ == "__main__":
    main()
