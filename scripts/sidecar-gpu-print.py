#!/usr/bin/env python3
"""sidecar-gpu-print.py -- print the GPU section of the sidecar /metrics JSON.
Usage: <script> <<< json_string"""
import json
import sys

try:
    data = json.load(sys.stdin)
except json.JSONDecodeError as err:
    sys.exit(f"invalid JSON: {err}")

gpus = data.get("gpu") or []
if not gpus:
    print("  (no GPU data reported by the sidecar)")
    sys.exit(0)

for g in gpus:
    used_gb = g.get("memory_used_mb", 0) / 1024
    total_gb = g.get("memory_total_mb", 0) / 1024
    free_gb = g.get("memory_free_mb", 0) / 1024
    print(
        f"  GPU {g.get('index', '?')}: {g.get('name', 'unknown')}\n"
        f"    utilization : {g.get('utilization_percent', '?')}%\n"
        f"    VRAM used   : {used_gb:.1f} GB\n"
        f"    VRAM total  : {total_gb:.1f} GB\n"
        f"    VRAM free   : {free_gb:.1f} GB\n"
        f"    temperature : {g.get('temperature_c', '?')} C"
    )
