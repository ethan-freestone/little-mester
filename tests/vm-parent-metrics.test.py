#!/usr/bin/env python3
"""Fixed parent metrics frame contract tests; no hardware or transport required."""

import copy
import datetime
import importlib.util
import os
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("parent_metrics", ROOT / "lib/vm/parent-metrics.py")
parent_metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(parent_metrics)
COLLECT_SPEC = importlib.util.spec_from_file_location(
    "parent_metrics_collect", ROOT / "lib/vm/parent-metrics-collect.py")
parent_metrics_collect = importlib.util.module_from_spec(COLLECT_SPEC)
COLLECT_SPEC.loader.exec_module(parent_metrics_collect)


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

    def test_historical_host_parsers_accept_injected_cpu_ram_gpu_and_thermal_fixtures(self):
        cpu = parent_metrics_collect.parse_cpu(
            "processor: 0\nmodel name: Example CPU\n", "1.25 0.80 0.40 1/20 123\n", 8)
        self.assertEqual(cpu, {"model": "Example CPU", "load_avg_1m": 1.25,
                               "load_avg_5m": 0.8, "load_avg_15m": 0.4, "num_cpus": 8})
        memory = parent_metrics_collect.parse_memory(
            "MemTotal: 67108864 kB\nMemAvailable: 25165824 kB\n")
        self.assertEqual(memory, {"total_gb": 64.0, "used_gb": 40.0,
                                  "available_gb": 24.0, "percent": 62.5})
        gpu = parent_metrics_collect.parse_gpu_csv(
            '"Example, GPU",55,60,8192,4096,4096\n')[0]
        self.assertEqual(gpu["name"], "Example, GPU")
        self.assertEqual(gpu["memory_free_mb"], 4096)
        temps = parent_metrics_collect.parse_temperatures([("chip", "temp1", 42.26)])
        self.assertEqual(temps, [{"chip": "chip", "sensor": "temp1", "temperature_c": 42.3}])

    def test_collector_assembles_a_valid_snapshot_from_injected_adapters(self):
        class Clock:
            now = 1_000_000_000

            def read(self):
                return self.now

        clock = Clock()
        adapters = {
            "cpu": lambda _deadline: ("model name: Test CPU", "1 0.5 0.25", 4),
            "memory": lambda _deadline: "MemTotal: 1048576 kB\nMemAvailable: 524288 kB\n",
            "gpus": lambda _deadline: "GPU,50,20,2048,512,1536\n",
            "temperatures": lambda _deadline: [("chip", "core", 45.0)],
        }
        result = parent_metrics_collect.collect_snapshot(
            run_id="0123456789abcdef0123456789abcdef", sequence=7,
            adapters=adapters, clock=clock.read,
            wall_clock=lambda: datetime.datetime(2026, 11, 11, tzinfo=datetime.timezone.utc))
        parent_metrics.validate_snapshot(result)
        self.assertEqual(result["sequence"], 7)
        self.assertEqual(result["sampling_duration_ms"], 0)

    def test_collector_marks_late_adapter_timeout_and_does_not_run_later_adapters(self):
        class Clock:
            now = 0

            def read(self):
                return self.now

        clock = Clock()
        called = []

        def slow_cpu(deadline):
            called.append("cpu")
            clock.now = deadline + 1
            return "model name: Late CPU", "1 1 1", 2

        def should_not_run(name, value):
            def adapter(_deadline):
                called.append(name)
                return value
            return adapter

        adapters = {
            "cpu": slow_cpu,
            "memory": should_not_run("memory", "MemTotal: 1024 kB\nMemAvailable: 512 kB\n"),
            "gpus": should_not_run("gpus", "GPU,50,20,2048,512,1536\n"),
            "temperatures": should_not_run("temperatures", [("chip", "core", 45)]),
        }
        result = parent_metrics_collect.collect_snapshot(
            run_id="0123456789abcdef0123456789abcdef", sequence=1,
            adapters=adapters, budget_seconds=0.1, clock=clock.read,
            wall_clock=lambda: datetime.datetime(2026, 11, 11, tzinfo=datetime.timezone.utc))
        self.assertEqual(called, ["cpu"])
        self.assertEqual(result["cpu"], parent_metrics.category("unavailable", None, "timeout"))
        self.assertTrue(all(result[name]["status"] == "unavailable"
                            and result[name]["error"] == "timeout"
                            for name in ("memory", "gpus", "temperatures")))
        self.assertEqual(result["sampling_duration_ms"], 100)

    def test_collector_maps_missing_optional_hardware_and_bad_sources_explicitly(self):
        adapters = {"cpu": None, "memory": None, "gpus": None, "temperatures": None}
        result = parent_metrics_collect.collect_snapshot(
            run_id="0123456789abcdef0123456789abcdef", sequence=1,
            adapters=adapters, clock=lambda: 2_000_000_000,
            wall_clock=lambda: datetime.datetime(2026, 11, 11, tzinfo=datetime.timezone.utc))
        self.assertEqual(result["gpus"], parent_metrics.category("unavailable", None, "no_device"))
        self.assertEqual(result["temperatures"],
                         parent_metrics.category("unavailable", None, "no_sensors"))
        adapters["memory"] = lambda _deadline: "broken"
        result = parent_metrics_collect.collect_snapshot(
            run_id="0123456789abcdef0123456789abcdef", sequence=2,
            adapters=adapters, clock=lambda: 2_000_000_000,
            wall_clock=lambda: datetime.datetime(2026, 11, 11, tzinfo=datetime.timezone.utc))
        self.assertEqual(result["memory"], parent_metrics.category("unavailable", None, "malformed"))

    def test_collector_refuses_sampling_duration_outside_fixed_schema(self):
        class Clock:
            calls = 0

            def read(self):
                self.calls += 1
                return 0 if self.calls == 1 else 6_000_000_000

        with self.assertRaisesRegex(ValueError, "duration exceeded"):
            parent_metrics_collect.collect_snapshot(
                run_id="0123456789abcdef0123456789abcdef", sequence=1,
                adapters={"cpu": None, "memory": None, "gpus": None, "temperatures": None},
                clock=Clock().read,
                wall_clock=lambda: datetime.datetime(2026, 11, 11, tzinfo=datetime.timezone.utc))

    def test_linux_proc_adapters_are_lazy_and_read_only_fixed_bounded_fixture_files(self):
        with tempfile.TemporaryDirectory(prefix="parent-proc-fixture-") as temporary:
            root = pathlib.Path(temporary)
            (root / "cpuinfo").write_text("model name: Fixture CPU\n", encoding="utf-8")
            (root / "loadavg").write_text("0.5 0.25 0.1 1/1 2\n", encoding="utf-8")
            (root / "meminfo").write_text(
                "MemTotal: 2097152 kB\nMemAvailable: 1048576 kB\n", encoding="utf-8")
            reads = []

            def read_text(path, maximum):
                reads.append((pathlib.Path(path).name, maximum))
                return parent_metrics_collect.read_bounded_file(path, maximum)

            adapters = parent_metrics_collect.linux_proc_adapters(
                proc_root=root, cpu_count=lambda: 4, read_text=read_text,
                clock=lambda: 0)
            self.assertEqual(reads, [])
            cpu = parent_metrics_collect.parse_cpu(*adapters["cpu"](100))
            memory = parent_metrics_collect.parse_memory(adapters["memory"](100))
            self.assertEqual(cpu["num_cpus"], 4)
            self.assertEqual(memory["total_gb"], 2.0)
            self.assertEqual({name for name, _limit in reads}, {"cpuinfo", "loadavg", "meminfo"})
            self.assertIsNone(adapters["gpus"])
            self.assertIsNone(adapters["temperatures"])

    def test_bounded_file_reader_rejects_symlink_nonregular_and_oversize(self):
        with tempfile.TemporaryDirectory(prefix="parent-proc-fixture-") as temporary:
            root = pathlib.Path(temporary)
            target = root / "target"
            target.write_text("private", encoding="utf-8")
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaises(parent_metrics_collect.CollectorError):
                parent_metrics_collect.read_bounded_file(link, 64)
            with self.assertRaises(parent_metrics_collect.CollectorError):
                parent_metrics_collect.read_bounded_file(root, 64)
            with self.assertRaises(parent_metrics_collect.CollectorError):
                parent_metrics_collect.read_bounded_file(target, 3)

    def test_linux_proc_adapter_stops_between_reads_after_deadline(self):
        class Clock:
            now = 0

            def read(self):
                return self.now

        clock = Clock()
        reads = []

        def read_text(path, _maximum):
            name = pathlib.Path(path).name
            reads.append(name)
            if name == "cpuinfo":
                clock.now = 100
                return "model name: Fixture CPU"
            return "0 0 0"

        adapters = parent_metrics_collect.linux_proc_adapters(
            proc_root="/proc-fixture", cpu_count=lambda: 4,
            read_text=read_text, clock=clock.read)
        with self.assertRaises(TimeoutError):
            adapters["cpu"](100)
        self.assertEqual(reads, ["cpuinfo"])

    def test_historical_parsers_reject_malformed_or_out_of_contract_values(self):
        with self.assertRaises(parent_metrics_collect.CollectorError):
            parent_metrics_collect.parse_cpu("", "not-loadavg", 4)
        with self.assertRaises(parent_metrics_collect.CollectorError):
            parent_metrics_collect.parse_memory("MemTotal: 100 kB\nMemAvailable: 101 kB\n")
        with self.assertRaises(parent_metrics_collect.CollectorError):
            parent_metrics_collect.parse_gpu_csv("GPU,50,101,2048,512,1536\n")
        with self.assertRaises(parent_metrics_collect.CollectorError):
            parent_metrics_collect.parse_temperatures([("chip\x1b", "core", 40)])
        with self.assertRaises(parent_metrics_collect.CollectorError):
            parent_metrics_collect.parse_gpu_csv("G" * 8193)
        with self.assertRaises(parent_metrics_collect.CollectorError):
            parent_metrics_collect.parse_temperatures([(object(), "core", 40)])

    def test_category_discards_data_when_adapter_finishes_at_deadline(self):
        reads = iter((0, 100))
        result = parent_metrics_collect._category(
            lambda _deadline: "value", lambda value: value, deadline_ns=100,
            clock=lambda: next(reads), unavailable_code="unsupported")
        self.assertEqual(result, parent_metrics.category("unavailable", None, "timeout"))

    def test_category_discards_data_when_parsing_crosses_deadline(self):
        reads = iter((0, 1, 100))

        def parse(_value):
            return "parsed"

        result = parent_metrics_collect._category(
            lambda _deadline: "value", parse, deadline_ns=100,
            clock=lambda: next(reads), unavailable_code="unsupported")
        self.assertEqual(result, parent_metrics.category("unavailable", None, "timeout"))

    def test_category_deadline_takes_precedence_over_late_errors_and_missing_adapters(self):
        reads = iter((0, 1, 100))

        def parse_error(_value):
            raise parent_metrics_collect.CollectorError("malformed")

        result = parent_metrics_collect._category(
            lambda _deadline: "value", parse_error, deadline_ns=100,
            clock=lambda: next(reads), unavailable_code="unsupported")
        self.assertEqual(result, parent_metrics.category("unavailable", None, "timeout"))
        reads = iter((0, 100))
        result = parent_metrics_collect._category(
            lambda _deadline: (_ for _ in ()).throw(PermissionError()), lambda value: value,
            deadline_ns=100, clock=lambda: next(reads), unavailable_code="unsupported")
        self.assertEqual(result, parent_metrics.category("unavailable", None, "timeout"))
        result = parent_metrics_collect._category(
            None, lambda value: value, deadline_ns=100, clock=lambda: 100,
            unavailable_code="no_device")
        self.assertEqual(result, parent_metrics.category("unavailable", None, "timeout"))


if __name__ == "__main__":
    unittest.main()
