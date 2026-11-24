#!/usr/bin/env python3
"""Loopback-only read-only host metrics endpoint for the fixed v3 guest."""

from __future__ import annotations

import collections
import http.server
import importlib.util
import json
import os
import pathlib
import re
import secrets
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit


HOST = "127.0.0.1"
DEFAULT_PORT = 32129
PORT_ENVIRONMENT_VARIABLE = "LM_VM_METRICS_PORT"
GUEST_PORT = 19532
MAX_HISTORY = 60
MAX_RESPONSE = 16 * 1024
MIN_SAMPLE_INTERVAL = 0.4


def configured_port(value: str | None) -> int:
    """Select the fixed singleton port or a validated per-profile loopback port."""
    if value is None:
        return DEFAULT_PORT
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,5}", value):
        raise ValueError(f"{PORT_ENVIRONMENT_VARIABLE} must be a decimal TCP port")
    port = int(value)
    if not 1024 <= port <= 65535:
        raise ValueError(f"{PORT_ENVIRONMENT_VARIABLE} is outside the allowed port range")
    return port


# The fixed singleton retains its established default. The profile helper may
# start one isolated service process per profile with its allocated loopback
# port, keeping each process's run ID and history separate.
PORT = configured_port(os.environ.get(PORT_ENVIRONMENT_VARIABLE))


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).with_name(filename))
    if spec is None or spec.loader is None:
        raise RuntimeError("fixed host metrics helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


contract = load("system_vm_metrics_contract", "parent-metrics.py")
collector = load("system_vm_metrics_collector", "parent-metrics-collect.py")
RUN_ID = secrets.token_hex(16)
HISTORY = collections.deque(maxlen=MAX_HISTORY)
LOCK = threading.Lock()
LAST_SAMPLE = 0.0
SEQUENCE = 0


def _public_snapshot(snapshot: dict) -> dict:
    def available(name: str):
        category = snapshot[name]
        return category["value"] if category["status"] == "ok" else None

    gpu_values = available("gpus")
    cpu = available("cpu")
    memory = available("memory")
    gpus = gpu_values if isinstance(gpu_values, list) else []
    temperatures = available("temperatures")
    return {
        "source": "host",
        "run_id": snapshot["run_id"],
        "sequence": snapshot["sequence"],
        "timestamp": snapshot["sampled_at"],
        "cpu": cpu,
        "memory": memory,
        "gpu": gpus,
        "availability": {
            name: ("available" if snapshot[name]["status"] == "ok"
                   else snapshot[name]["error"])
            for name in ("cpu", "memory", "gpus", "temperatures")
        },
        "temperatures": temperatures,
    }


def sample() -> dict:
    global LAST_SAMPLE, SEQUENCE
    now = time.monotonic()
    with LOCK:
        if HISTORY and now - LAST_SAMPLE < MIN_SAMPLE_INTERVAL:
            return HISTORY[-1]["payload"]
        SEQUENCE += 1
        adapters = collector.linux_proc_adapters(
            gpu_adapter=collector.nvidia_smi_adapter(), temperatures_adapter=None)
        snapshot = collector.collect_snapshot(run_id=RUN_ID, sequence=SEQUENCE, adapters=adapters,
                                              budget_seconds=0.4)
        # Validate the canonical schema before deriving the limited display shape.
        contract.validate_snapshot(snapshot)
        payload = _public_snapshot(snapshot)
        gpu = payload["gpu"][0] if payload["gpu"] else {}
        total = gpu.get("memory_total_mb", 0)
        history_item = {
            "run_id": payload["run_id"],
            "sequence": payload["sequence"],
            "timestamp": payload["timestamp"],
            "gpu_util": gpu.get("utilization_percent"),
            "vram_pct": (round(gpu["memory_used_mb"] * 100 / total, 1)
                         if total > 0 else None),
            "gpu_temp": gpu.get("temperature_c"),
            "availability": payload["availability"],
        }
        HISTORY.append({"payload": payload, "history": history_item})
        LAST_SAMPLE = time.monotonic()
        return payload


class MetricsHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LittleMesterHostMetrics/1"
    sys_version = ""

    def log_message(self, _format, *args):
        # Avoid emitting request-controlled data to the host journal.
        return

    def _respond(self, status: int, payload: object) -> None:
        try:
            body = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            status, body = 503, b'{"source":"host","unavailable":true}'
        if len(body) > MAX_RESPONSE:
            status, body = 503, b'{"source":"host","unavailable":true}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            if self.headers.get("Host") != "10.0.2.100:19532":
                self._respond(400, {"error": "invalid_host"})
                return
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.fragment:
                self._respond(400, {"error": "invalid_request"})
                return
            if parsed.path == "/metrics" and not parsed.query:
                self._respond(200, sample())
                return
            if parsed.path == "/history":
                values = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
                if set(values) != {"n"} or len(values["n"]) != 1:
                    self._respond(400, {"error": "invalid_request"})
                    return
                amount = int(values["n"][0])
                if not 1 <= amount <= MAX_HISTORY:
                    self._respond(400, {"error": "invalid_request"})
                    return
                sample()
                with LOCK:
                    result = [item["history"] for item in list(HISTORY)[-amount:]]
                self._respond(200, result)
                return
            self._respond(404, {"error": "not_found"})
        except (ValueError, TypeError):
            self._respond(400, {"error": "invalid_request"})
        except Exception:
            self._respond(503, {"source": "host", "unavailable": True})

    def do_HEAD(self):  # noqa: N802 - explicit method denial
        self._respond(405, {"error": "method_not_allowed"})

    def do_POST(self):  # noqa: N802 - explicit method denial
        self._respond(405, {"error": "method_not_allowed"})

    def do_PUT(self):  # noqa: N802 - explicit method denial
        self._respond(405, {"error": "method_not_allowed"})

    def do_DELETE(self):  # noqa: N802 - explicit method denial
        self._respond(405, {"error": "method_not_allowed"})


class LoopbackServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        super().__init__((HOST, PORT), MetricsHandler, bind_and_activate=True)


def main() -> int:
    server = LoopbackServer()
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
