#!/usr/bin/env python3
"""schema-check-metrics.py -- validate /metrics JSON against schema.
Usage: <script_or_stdin> <<< json_string"""
import json
import sys

data = json.load(sys.stdin)

# Top-level keys
for key in ("timestamp", "cpu", "memory", "gpu", "temperatures"):
    if key not in data:
        sys.exit(f"missing section: {key}")

assert isinstance(data["cpu"], dict), "cpu must be a dict"
assert isinstance(data["memory"], dict), "memory must be a dict"
assert isinstance(data["gpu"], list), "gpu must be a list"
assert isinstance(data["temperatures"], list), "temperatures must be a list"

# gpu items
for i, g in enumerate(data["gpu"]):
    if "index" not in g:
        sys.exit(f"gpu[{i}] missing index")
    if "name" not in g:
        sys.exit(f"gpu[{i}] missing name")

# memory keys
for key in ("total_gb", "used_gb", "available_gb", "percent"):
    if key not in data["memory"]:
        sys.exit(f"memory section missing '{key}'")

print("Schema valid: all sections present with correct types")
