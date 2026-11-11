#!/usr/bin/env python3
"""Side-effect-free parsing and deadline-aware host metrics snapshot collection."""

from __future__ import annotations

import csv
import importlib.util
import math
import pathlib
import time


def _load_contract():
    path = pathlib.Path(__file__).with_name("parent-metrics.py")
    spec = importlib.util.spec_from_file_location("parent_metrics_contract", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("protected parent metrics contract is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


contract = _load_contract()


class CollectorError(ValueError):
    """A source could not be parsed into the fixed metrics contract."""


def parse_cpu(cpuinfo: str, loadavg: str, online_cpus: int) -> dict:
    model = "unknown"
    for line in cpuinfo.splitlines():
        if line.lower().startswith(("model name", "hardware")) and ":" in line:
            candidate = line.split(":", 1)[1].strip()
            if candidate:
                model = candidate[:128]
                break
    fields = loadavg.split()
    if len(fields) < 3:
        raise CollectorError("malformed")
    try:
        loads = [float(value) for value in fields[:3]]
    except ValueError as error:
        raise CollectorError("malformed") from error
    result = {"model": model, "load_avg_1m": loads[0], "load_avg_5m": loads[1],
              "load_avg_15m": loads[2], "num_cpus": online_cpus}
    if not contract._cpu(result):
        raise CollectorError("malformed")
    return result


def parse_memory(meminfo: str) -> dict:
    values = {}
    try:
        for line in meminfo.splitlines():
            key, separator, remainder = line.partition(":")
            if separator and key in {"MemTotal", "MemAvailable"}:
                fields = remainder.split()
                if len(fields) >= 2 and fields[1] == "kB":
                    values[key] = int(fields[0])
    except ValueError as error:
        raise CollectorError("malformed") from error
    total_kb = values.get("MemTotal", 0)
    available_kb = values.get("MemAvailable", 0)
    if total_kb <= 0 or available_kb < 0 or available_kb > total_kb:
        raise CollectorError("malformed")
    used_kb = total_kb - available_kb
    divisor = 1024 * 1024
    result = {
        "total_gb": round(total_kb / divisor, 1),
        "used_gb": round(used_kb / divisor, 1),
        "available_gb": round(available_kb / divisor, 1),
        "percent": round(used_kb / total_kb * 100, 1),
    }
    if not contract._memory(result):
        raise CollectorError("malformed")
    return result


def parse_gpu_csv(output: str) -> list[dict]:
    """Parse the fixed nvidia-smi query columns; no command is run here."""
    rows = list(csv.reader(output.splitlines(), skipinitialspace=True))
    if not rows or len(rows) > 8:
        raise CollectorError("no_device" if not rows else "malformed")
    gpus = []
    try:
        for index, row in enumerate(rows):
            if len(row) != 6:
                raise CollectorError("malformed")
            name = row[0].strip()
            values = [int(float(value.strip())) for value in row[1:]]
            gpus.append({"index": index, "name": name, "temperature_c": values[0],
                         "utilization_percent": values[1], "memory_total_mb": values[2],
                         "memory_used_mb": values[3], "memory_free_mb": values[4]})
    except (ValueError, OverflowError) as error:
        raise CollectorError("malformed") from error
    if not contract._gpus(gpus):
        raise CollectorError("malformed")
    return gpus


def parse_temperatures(entries) -> list[dict]:
    """Normalize injected (chip, sensor, Celsius) entries from sysfs adapters."""
    if len(entries) > 64:
        raise CollectorError("malformed")
    result = []
    for chip, sensor, raw_celsius in entries:
        try:
            temperature = round(float(raw_celsius), 1)
        except (TypeError, ValueError, OverflowError) as error:
            raise CollectorError("malformed") from error
        result.append({"chip": chip[:64], "sensor": sensor[:64],
                       "temperature_c": temperature})
    if not contract._temperatures(result):
        raise CollectorError("malformed")
    return result


def _category(adapter, parser, *, deadline_ns: int, clock, unavailable_code: str):
    if adapter is None:
        return contract.category("unavailable", None, unavailable_code)
    if clock() >= deadline_ns:
        return contract.category("unavailable", None, "timeout")
    try:
        raw = adapter(deadline_ns)
        if clock() > deadline_ns:
            return contract.category("unavailable", None, "timeout")
        return contract.category("ok", parser(raw))
    except TimeoutError:
        return contract.category("unavailable", None, "timeout")
    except PermissionError:
        return contract.category("unavailable", None, "permission_denied")
    except CollectorError as error:
        return contract.category("unavailable", None, str(error))
    except OSError:
        return contract.category("unavailable", None, "unsupported")
    except Exception:
        # Do not serialize exception text or host paths into the fixed frame.
        return contract.category("error", None, "collector_failed")


def collect_snapshot(*, run_id: str, sequence: int, adapters: dict,
                     budget_seconds: float = 0.4, clock=time.monotonic_ns,
                     wall_clock=None) -> dict:
    """Collect a bounded-schema sample through injected deadline-aware adapters.

    Each adapter is called synchronously with an absolute monotonic-nanosecond
    deadline and must itself honor it. After each call the collector checks for
    overrun and discards late data. This cannot preempt a non-cooperating adapter;
    production process/hardware adapters must enforce their own bounded call.
    """
    if (not isinstance(adapters, dict)
            or set(adapters) != {"cpu", "memory", "gpus", "temperatures"}
            or type(budget_seconds) not in (int, float)
            or not math.isfinite(budget_seconds) or not 0 < budget_seconds <= 0.5):
        raise contract.Refused("metrics collector requires fixed adapters and a budget up to 500ms")
    started = clock()
    if type(started) is not int or started < 0:
        raise contract.Refused("metrics collector clock returned an invalid value")
    deadline = started + int(budget_seconds * 1_000_000_000)
    parsers = {"cpu": lambda value: parse_cpu(*value), "memory": parse_memory,
               "gpus": parse_gpu_csv, "temperatures": parse_temperatures}
    fallbacks = {"cpu": "unsupported", "memory": "unsupported",
                 "gpus": "no_device", "temperatures": "no_sensors"}
    categories = {}
    for name in ("cpu", "memory", "gpus", "temperatures"):
        categories[name] = _category(adapters[name], parsers[name], deadline_ns=deadline,
                                     clock=clock, unavailable_code=fallbacks[name])
    finished = clock()
    if type(finished) is not int or finished < started:
        raise contract.Refused("metrics collector clock moved backwards or returned an invalid value")
    duration_ms = (finished - started) // 1_000_000
    if duration_ms > 5000:
        raise contract.Refused("metrics sampling duration exceeded the fixed schema maximum")
    wall_time = wall_clock() if wall_clock is not None else None
    snapshot = {
        "version": 1, "run_id": run_id, "sequence": sequence,
        "sampled_at": contract.utc_sample_time(wall_time),
        "sampling_duration_ms": duration_ms,
        "cpu": categories["cpu"], "memory": categories["memory"],
        "gpus": categories["gpus"], "temperatures": categories["temperatures"],
    }
    contract.validate_snapshot(snapshot)
    return snapshot
