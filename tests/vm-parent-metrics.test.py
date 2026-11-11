#!/usr/bin/env python3
"""Fixed parent metrics frame contract tests; no hardware or transport required."""

import copy
import datetime
import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("parent_metrics", ROOT / "lib/vm/parent-metrics.py")
parent_metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(parent_metrics)


def valid_snapshot():
    return {
        "version": 1,
        "run_id": "0123456789abcdef0123456789abcdef",
        "sequence": 1,
        "sampled_at": "2026-11-11T16:00:00.123Z",
        "sampling_duration_ms": 14,
        "cpu": parent_metrics.category("ok", {
            "model": "Test CPU", "load_avg_1m": 1.2, "load_avg_5m": 0.8,
            "load_avg_15m": 0.4, "num_cpus": 8,
        }),
        "memory": parent_metrics.category("ok", {
            "total_gb": 64.0, "used_gb": 40.0, "available_gb": 24.0, "percent": 62.5,
        }),
        "gpus": parent_metrics.category("ok", [{
            "index": 0, "name": "Test GPU", "temperature_c": 55,
            "utilization_percent": 60, "memory_total_mb": 8192,
            "memory_used_mb": 4096, "memory_free_mb": 4096,
        }]),
        "temperatures": parent_metrics.category("ok", [
            {"chip": "test", "sensor": "core", "temperature_c": 45.5},
        ]),
    }


class ParentMetricsContractTests(unittest.TestCase):
    def test_valid_fixed_snapshot_encodes_compact_utf8(self):
        payload = parent_metrics.encode_snapshot(valid_snapshot())
        self.assertLessEqual(len(payload), parent_metrics.MAX_PAYLOAD_BYTES)
        self.assertNotIn(b"\": ", payload)
        self.assertNotIn(b", ", payload)
        self.assertEqual(payload.decode("utf-8").count('"version"'), 1)

    def test_unavailable_and_error_categories_require_explicit_code_and_null_value(self):
        for state, code in (("unavailable", "no_device"), ("error", "timeout")):
            snapshot = valid_snapshot()
            snapshot["gpus"] = parent_metrics.category(state, None, code)
            parent_metrics.validate_snapshot(snapshot)
        for bad in (
            parent_metrics.category("unavailable", None, None),
            parent_metrics.category("error", None, "arbitrary-exception-text"),
            parent_metrics.category("error", [], "timeout"),
            parent_metrics.category("ok", None),
        ):
            snapshot = valid_snapshot()
            snapshot["gpus"] = bad
            with self.subTest(category=bad), self.assertRaises(parent_metrics.Refused):
                parent_metrics.validate_snapshot(snapshot)

    def test_absent_gpu_or_temperature_hardware_uses_explicit_unavailable_state(self):
        snapshot = valid_snapshot()
        snapshot["gpus"] = parent_metrics.category("ok", [])
        with self.assertRaises(parent_metrics.Refused):
            parent_metrics.validate_snapshot(snapshot)
        snapshot["gpus"] = parent_metrics.category("unavailable", None, "no_device")
        snapshot["temperatures"] = parent_metrics.category("unavailable", None, "no_sensors")
        parent_metrics.validate_snapshot(snapshot)

    def test_rejects_missing_extra_and_wrong_type_top_level_fields(self):
        for mutate in (
            lambda value: value.pop("gpus"),
            lambda value: value.update(extra="host-data"),
            lambda value: value.update(version=True),
            lambda value: value.update(sequence=0),
            lambda value: value.update(run_id="not-an-opaque-run-id"),
            lambda value: value.update(sampled_at="2026-11-11T16:00:00Z"),
            lambda value: value.update(sampling_duration_ms=5001),
        ):
            snapshot = valid_snapshot()
            mutate(snapshot)
            with self.subTest(snapshot=snapshot), self.assertRaises(parent_metrics.Refused):
                parent_metrics.validate_snapshot(snapshot)

    def test_rejects_cpu_and_memory_bounds_and_inconsistent_values(self):
        bad_cpu = [
            {"model": "x" * 129, "load_avg_1m": 0, "load_avg_5m": 0,
             "load_avg_15m": 0, "num_cpus": 1},
            {"model": "cpu", "load_avg_1m": float("nan"), "load_avg_5m": 0,
             "load_avg_15m": 0, "num_cpus": 1},
            {"model": "cpu", "load_avg_1m": 0, "load_avg_5m": 0,
             "load_avg_15m": 0, "num_cpus": True},
            {"model": "cpu", "load_avg_1m": 10**10000, "load_avg_5m": 0,
             "load_avg_15m": 0, "num_cpus": 1},
            {"model": "cpu\x1b[31m", "load_avg_1m": 0, "load_avg_5m": 0,
             "load_avg_15m": 0, "num_cpus": 1},
            {"model": "cpu\u202e", "load_avg_1m": 0, "load_avg_5m": 0,
             "load_avg_15m": 0, "num_cpus": 1},
        ]
        for value in bad_cpu:
            snapshot = valid_snapshot()
            snapshot["cpu"] = parent_metrics.category("ok", value)
            with self.assertRaises(parent_metrics.Refused):
                parent_metrics.validate_snapshot(snapshot)
        for value in (
            {"total_gb": 64, "used_gb": 65, "available_gb": 1, "percent": 50},
            {"total_gb": 64, "used_gb": 1, "available_gb": 65, "percent": 1},
            {"total_gb": 64, "used_gb": 1, "available_gb": 1, "percent": 101},
            {"total_gb": 64, "used_gb": 30, "available_gb": 24, "percent": 46.9},
            {"total_gb": 0, "used_gb": 0, "available_gb": 0, "percent": 0},
        ):
            snapshot = valid_snapshot()
            snapshot["memory"] = parent_metrics.category("ok", value)
            with self.assertRaises(parent_metrics.Refused):
                parent_metrics.validate_snapshot(snapshot)

    def test_rejects_gpu_and_temperature_schema_bounds(self):
        mutations = (
            lambda gpu: gpu.update(index=True),
            lambda gpu: gpu.update(name="é" * 65),
            lambda gpu: gpu.update(temperature_c=251),
            lambda gpu: gpu.update(utilization_percent=101),
            lambda gpu: gpu.update(memory_used_mb=8193),
        )
        for mutate in mutations:
            snapshot = valid_snapshot()
            gpu = copy.deepcopy(snapshot["gpus"]["value"][0])
            mutate(gpu)
            snapshot["gpus"]["value"] = [gpu]
            with self.subTest(gpu=gpu), self.assertRaises(parent_metrics.Refused):
                parent_metrics.validate_snapshot(snapshot)
        snapshot = valid_snapshot()
        snapshot["gpus"]["value"].append(copy.deepcopy(snapshot["gpus"]["value"][0]))
        with self.assertRaises(parent_metrics.Refused):
            parent_metrics.validate_snapshot(snapshot)
        snapshot = valid_snapshot()
        snapshot["temperatures"]["value"] = [{"chip": "c", "sensor": "s", "temperature_c": True}]
        with self.assertRaises(parent_metrics.Refused):
            parent_metrics.validate_snapshot(snapshot)
        snapshot = valid_snapshot()
        snapshot["temperatures"]["value"] = [
            {"chip": "c", "sensor": "s", "temperature_c": 20}
        ] * 65
        with self.assertRaises(parent_metrics.Refused):
            parent_metrics.validate_snapshot(snapshot)

    def test_worst_case_current_field_bounds_remain_below_payload_cap(self):
        snapshot = valid_snapshot()
        snapshot["cpu"]["value"]["model"] = "c" * 128
        snapshot["gpus"]["value"] = [{
            "index": index, "name": "g" * 128, "temperature_c": 50,
            "utilization_percent": 50, "memory_total_mb": 8192,
            "memory_used_mb": 4096, "memory_free_mb": 4096,
        } for index in range(8)]
        snapshot["temperatures"]["value"] = [{
            "chip": "c" * 64, "sensor": "s" * 64, "temperature_c": 50,
        } for _ in range(64)]
        payload = parent_metrics.encode_snapshot(snapshot)
        self.assertGreater(len(payload), 12 * 1024)
        self.assertLessEqual(len(payload), parent_metrics.MAX_PAYLOAD_BYTES)

    def test_payload_size_cap_rejects_oversized_encoding(self):
        snapshot = valid_snapshot()
        original_limit = parent_metrics.MAX_PAYLOAD_BYTES
        try:
            parent_metrics.MAX_PAYLOAD_BYTES = 32
            with self.assertRaisesRegex(parent_metrics.Refused, "16-KiB"):
                parent_metrics.encode_snapshot(snapshot)
        finally:
            parent_metrics.MAX_PAYLOAD_BYTES = original_limit

    def test_timestamp_is_utc_millisecond_precision_and_rejects_naive_input(self):
        instant = datetime.datetime(2026, 11, 11, 16, 0, 0, 987654,
                                    tzinfo=datetime.timezone(datetime.timedelta(hours=2)))
        self.assertEqual(parent_metrics.utc_sample_time(instant), "2026-11-11T14:00:00.987Z")
        with self.assertRaises(parent_metrics.Refused):
            parent_metrics.utc_sample_time(datetime.datetime(2026, 11, 11))

    def test_absolute_schedule_uses_500ms_slots_and_runs_callbacks_serially(self):
        class Clock:
            now = 10.0
            sleeps = []

            def monotonic(self):
                return round(self.now * 1_000_000_000)

            def wait(self, duration):
                self.sleeps.append(duration)
                self.now += duration

        clock = Clock()
        frames = []
        active = 0
        maximum_active = 0

        def callback(slot, deadline):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            frames.append((clock.now, slot, deadline))
            clock.now += 0.1
            active -= 1

        skipped = parent_metrics.run_absolute_schedule(
            callback, lambda: len(frames) == 3, clock=clock.monotonic, wait=clock.wait)
        self.assertEqual(skipped, 0)
        self.assertEqual([frame[0] for frame in frames], [10.0, 10.5, 11.0])
        self.assertEqual([frame[2] - frame[1] for frame in frames], [0.5] * 3)
        self.assertEqual(maximum_active, 1)
        self.assertEqual(len(clock.sleeps), 2)
        for duration in clock.sleeps:
            self.assertAlmostEqual(duration, 0.4)

    def test_absolute_schedule_skips_missed_slots_without_catchup(self):
        class Clock:
            now = 0.0

            def monotonic(self):
                return round(self.now * 1_000_000_000)

            def wait(self, duration):
                self.now += duration

        clock = Clock()
        frames = []

        def slow_callback(slot, deadline):
            frames.append((clock.now, slot, deadline))
            clock.now += 0.8

        skipped = parent_metrics.run_absolute_schedule(
            slow_callback, lambda: len(frames) == 3,
            clock=clock.monotonic, wait=clock.wait)
        self.assertEqual([frame[0] for frame in frames], [0.0, 1.0, 2.0])
        self.assertEqual(skipped, 2)

    def test_absolute_schedule_runs_slot_when_callback_finishes_exactly_on_deadline(self):
        class Clock:
            now = 0.0

            def monotonic(self):
                return round(self.now * 1_000_000_000)

            def wait(self, duration):
                self.now += duration

        clock = Clock()
        starts = []

        def callback(slot, deadline):
            starts.append((clock.now, slot, deadline))
            clock.now = deadline

        skipped = parent_metrics.run_absolute_schedule(
            callback, lambda: len(starts) == 3,
            clock=clock.monotonic, wait=clock.wait)
        self.assertEqual([frame[0] for frame in starts], [0.0, 0.5, 1.0])
        self.assertEqual(skipped, 0)

    def test_absolute_schedule_skips_intermediate_slots_but_runs_exact_current_slot(self):
        class Clock:
            now = 0.1

            def monotonic(self):
                return round(self.now * 1_000_000_000)

            def wait(self, duration):
                self.now += duration

        clock = Clock()
        starts = []

        def callback(slot, deadline):
            starts.append(clock.now)
            clock.now += 1.0

        skipped = parent_metrics.run_absolute_schedule(
            callback, lambda: len(starts) == 2,
            clock=clock.monotonic, wait=clock.wait)
        self.assertEqual(starts, [0.1, 1.1])
        self.assertEqual(skipped, 1)

    def test_absolute_schedule_rejects_invalid_clock(self):
        with self.assertRaises(parent_metrics.Refused):
            parent_metrics.run_absolute_schedule(lambda *_: None, lambda: False,
                                                 clock=lambda: float("nan"), wait=lambda _: None)


if __name__ == "__main__":
    unittest.main()
