#!/usr/bin/env python3
"""sidecar-ram-json.py -- print the memory section of the sidecar /metrics JSON.
Usage: <script> <<< json_string"""
import json
import sys

try:
    data = json.load(sys.stdin)
except json.JSONDecodeError as err:
    sys.exit(f"invalid JSON: {err}")

mem = data.get("memory") or {}
print(
    f"  Sidecar RAM: {mem.get('used_gb', '?')}GB / {mem.get('total_gb', '?')}GB "
    f"({mem.get('percent', '?')}%) -- {mem.get('available_gb', '?')}GB available"
)
