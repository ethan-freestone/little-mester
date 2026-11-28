#!/usr/bin/env python3
"""Small shared, secret-free VM lifecycle stage reporter."""

from __future__ import annotations

import contextlib
import datetime
import json
import sys
import time


TIMED_STAGE_LABELS = frozenset({
    "initial_vm_start_readiness", "acceptance_checks", "clean_vm_shutdown",
    "same_disk_restart_readiness", "forced_stop_recovery", "final_cleanup",
    "candidate_install", "fresh_provisioning", "image_download_checksum",
    "image_conversion_resize", "cloud_seed_generation", "guest_readiness_ssh",
    "network_boundaries", "persistence_fixture", "host_metrics_transport",
    "direct_egress_blocked", "opencode_tui", "profile_vm_start_readiness",
})


class StageTimer:
    """Emit bounded, secret-free, flushed stage timing records."""

    def __init__(self, stream=None, *, wall_clock=None, monotonic=None):
        self.stream = sys.stdout if stream is None else stream
        self.wall_clock = (wall_clock if wall_clock is not None else
                           lambda: datetime.datetime.now(datetime.timezone.utc))
        self.monotonic = time.monotonic if monotonic is None else monotonic

    @contextlib.contextmanager
    def timed_stage(self, label: str):
        if label not in TIMED_STAGE_LABELS:
            raise ValueError("stage label is not allowlisted")
        started = self.monotonic()
        self._emit("START", label, self.wall_clock())
        outcome = "PASS"
        try:
            yield
        except BaseException:
            outcome = "FAIL"
            raise
        finally:
            elapsed_ms = max(0, round((self.monotonic() - started) * 1000))
            try:
                self._emit("END", label, self.wall_clock(),
                           elapsed_ms=elapsed_ms, outcome=outcome)
            except BaseException:
                if outcome == "PASS":
                    raise

    def _emit(self, event: str, label: str, timestamp: datetime.datetime, **extra) -> None:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("stage clock must return a timezone-aware timestamp")
        timestamp = timestamp.astimezone(datetime.timezone.utc)
        record = {"event": event, "label": label,
                  "timestamp": timestamp.isoformat().replace("+00:00", "Z"), **extra}
        self.stream.write("system_vm_stage=" + json.dumps(record, separators=(",", ":")) + "\n")
        self.stream.flush()
