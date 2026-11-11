#!/usr/bin/env python3
"""Fixed, bounded host-push metrics snapshot contract (transport independent)."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from datetime import UTC, datetime


MAX_PAYLOAD_BYTES = 16 * 1024
MAX_SEQUENCE = 2**63 - 1
ERROR_CODES = frozenset({"collector_failed", "malformed", "no_device", "no_sensors",
                         "permission_denied", "timeout", "unsupported"})
TOP_KEYS = {"version", "run_id", "sequence", "sampled_at", "sampling_duration_ms",
            "cpu", "memory", "gpus", "temperatures"}
CATEGORY_KEYS = {"status", "error", "value"}


class Refused(ValueError):
    """Snapshot is outside the fixed transport contract."""


def _number(value, *, minimum: float, maximum: float, integer: bool = False) -> bool:
    if type(value) is int:
        return minimum <= value <= maximum
    if type(value) is not float or integer:
        return False
    return math.isfinite(value) and minimum <= value <= maximum


def _text(value, maximum_bytes: int) -> bool:
    if (not isinstance(value, str)
            or any(unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} for char in value)):
        return False
    try:
        return len(value.encode("utf-8", errors="strict")) <= maximum_bytes
    except UnicodeEncodeError:
        return False


def _exact_object(value, keys: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == keys


def _category(value, payload_check) -> bool:
    if not _exact_object(value, CATEGORY_KEYS):
        return False
    status, error, payload = value["status"], value["error"], value["value"]
    if status == "ok":
        return error is None and payload_check(payload)
    return (status in ("unavailable", "error") and payload is None
            and isinstance(error, str) and error in ERROR_CODES)


def _cpu(value) -> bool:
    return (_exact_object(value, {"model", "load_avg_1m", "load_avg_5m", "load_avg_15m", "num_cpus"})
            and _text(value["model"], 128)
            and all(_number(value[name], minimum=0, maximum=1_000_000)
                    for name in ("load_avg_1m", "load_avg_5m", "load_avg_15m"))
            and _number(value["num_cpus"], minimum=1, maximum=4096, integer=True))


def _memory(value) -> bool:
    if (not _exact_object(value, {"total_gb", "used_gb", "available_gb", "percent"})
            or not all(_number(value[name], minimum=0, maximum=1_000_000)
                       for name in ("total_gb", "used_gb", "available_gb"))
            or not _number(value["percent"], minimum=0, maximum=100)):
        return False
    total, used, available = (value["total_gb"], value["used_gb"], value["available_gb"])
    # Legacy values are independently rounded to 0.1 GB; allow their maximum
    # combined rounding error, but reject materially contradictory snapshots.
    # `percent` is computed from the source's unrounded kB values in the legacy
    # collector, so it is range-checked but intentionally not recomputed here.
    return (total > 0 and used <= total and available <= total
            and abs((used + available) - total) <= 0.16)


def _gpus(value) -> bool:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        return False
    indexes = set()
    for gpu in value:
        if (not _exact_object(gpu, {"index", "name", "temperature_c", "utilization_percent",
                                    "memory_total_mb", "memory_used_mb", "memory_free_mb"})
                or not _number(gpu["index"], minimum=0, maximum=7, integer=True)
                or not _text(gpu["name"], 128)
                or not _number(gpu["temperature_c"], minimum=-100, maximum=250)
                or not _number(gpu["utilization_percent"], minimum=0, maximum=100, integer=True)
                or not all(_number(gpu[name], minimum=0, maximum=2**31 - 1, integer=True)
                           for name in ("memory_total_mb", "memory_used_mb", "memory_free_mb"))
                or gpu["memory_used_mb"] > gpu["memory_total_mb"]
                or gpu["memory_free_mb"] > gpu["memory_total_mb"]):
            return False
        if gpu["index"] in indexes:
            return False
        indexes.add(gpu["index"])
    return True


def _temperatures(value) -> bool:
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        return False
    return all(_exact_object(item, {"chip", "sensor", "temperature_c"})
               and _text(item["chip"], 64) and _text(item["sensor"], 64)
               and _number(item["temperature_c"], minimum=-100, maximum=250)
               for item in value)


def validate_snapshot(snapshot: object) -> None:
    """Raise Refused unless the object is exactly a valid v1 snapshot."""
    if not _exact_object(snapshot, TOP_KEYS):
        raise Refused("metrics snapshot has unexpected or missing top-level fields")
    if (type(snapshot["version"]) is not int or snapshot["version"] != 1
            or not isinstance(snapshot["run_id"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", snapshot["run_id"])
            or not _number(snapshot["sequence"], minimum=1, maximum=MAX_SEQUENCE, integer=True)
            or not isinstance(snapshot["sampled_at"], str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", snapshot["sampled_at"])
            or not _number(snapshot["sampling_duration_ms"], minimum=0, maximum=5000, integer=True)
            or not _category(snapshot["cpu"], _cpu)
            or not _category(snapshot["memory"], _memory)
            or not _category(snapshot["gpus"], _gpus)
            or not _category(snapshot["temperatures"], _temperatures)):
        raise Refused("metrics snapshot violates the fixed v1 schema")
    try:
        datetime.strptime(snapshot["sampled_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as error:
        raise Refused("metrics timestamp is not a valid UTC instant") from error


def encode_snapshot(snapshot: object) -> bytes:
    """Validate and compactly encode one frame within the fixed 16-KiB limit."""
    validate_snapshot(snapshot)
    try:
        payload = json.dumps(snapshot, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
    except (UnicodeError, ValueError, TypeError) as error:
        raise Refused("metrics snapshot cannot be encoded as strict UTF-8 JSON") from error
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise Refused("metrics snapshot exceeds the 16-KiB payload limit")
    return payload


def category(status: str, value=None, error: str | None = None) -> dict:
    """Construct a category envelope; validation is performed by encode_snapshot."""
    return {"status": status, "error": error, "value": value}


def utc_sample_time(now: datetime | None = None) -> str:
    """Format a diagnostic UTC timestamp at fixed millisecond precision."""
    instant = now if now is not None else datetime.now(UTC)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise Refused("metrics sample timestamp must be timezone-aware")
    return instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{instant.astimezone(UTC).microsecond // 1000:03d}Z"
