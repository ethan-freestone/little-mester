#!/usr/bin/env python3
"""Host metrics HTTP boundary tests with a real loopback listener."""

import importlib.util
import errno
import json
import pathlib
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "system_vm_metrics_test_subject", ROOT / "lib/vm/system-vm-metrics.py")
metrics = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = metrics
SPEC.loader.exec_module(metrics)


class MetricsHttpBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.old_port = metrics.PORT
        self.old_sample = metrics.sample
        self.old_run_id = metrics.RUN_ID
        self.old_sequence = metrics.SEQUENCE
        self.old_last_sample = metrics.LAST_SAMPLE
        self.old_min_sample_interval = metrics.MIN_SAMPLE_INTERVAL
        self.old_history = list(metrics.HISTORY)
        metrics.PORT = 0
        self.sample_payload = {
            "source": "host", "run_id": "a" * 32, "sequence": 1,
            "timestamp": "2026-11-20T00:00:00.000Z",
            "cpu": None, "memory": None, "gpu": [],
            "availability": {"cpu": "unsupported", "memory": "permission_denied",
                              "gpus": "no_device", "temperatures": "no_sensors"},
            "temperatures": None,
        }
        metrics.sample = lambda: self.sample_payload
        self.server = self.thread = None
        self.addCleanup(self._restore_globals)
        self.addCleanup(self._stop_fixture_server)
        self.server, self.thread = self._start_server()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    @staticmethod
    def _start_server():
        server = metrics.LoopbackServer()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    @staticmethod
    def _stop_server(server, thread):
        if thread.is_alive():
            server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    def _restore_globals(self):
        metrics.sample = self.old_sample
        metrics.PORT = self.old_port
        metrics.RUN_ID = self.old_run_id
        metrics.SEQUENCE = self.old_sequence
        metrics.LAST_SAMPLE = self.old_last_sample
        metrics.MIN_SAMPLE_INTERVAL = self.old_min_sample_interval
        metrics.HISTORY.clear()
        metrics.HISTORY.extend(self.old_history)

    def _stop_fixture_server(self):
        if self.server is not None:
            self._stop_server(self.server, self.thread)

    def tearDown(self):
        self.doCleanups()

    def request(self, path, method="GET", host="10.0.2.100:19532"):
        request = urllib.request.Request(self.base + path, method=method,
                                         headers={"Host": host})
        try:
            response = urllib.request.urlopen(request, timeout=2)
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())
        with response:
            return response.status, json.loads(response.read())

    def test_only_fixed_get_routes_and_guest_host_are_served(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        status, data = self.request("/metrics")
        self.assertEqual(status, 200)
        self.assertEqual(data["source"], "host")
        self.assertEqual(data["run_id"], "a" * 32)
        self.assertEqual(data["sequence"], 1)
        self.assertIn("availability", data)
        self.assertEqual(self.request("/metrics?other=1")[0], 404)
        self.assertEqual(self.request("/history?n=60")[0], 200)
        self.assertEqual(self.request("/history?n=61")[0], 400)
        self.assertEqual(self.request("/history?n=1&n=2")[0], 400)
        self.assertEqual(self.request("/metrics", host="attacker.example")[0], 400)

    def test_maximum_valid_history_uses_32_kib_budget_while_metrics_stays_capped(self):
        self.assertEqual(metrics.MAX_RESPONSE, 16 * 1024)
        self.assertEqual(metrics.MAX_HISTORY_RESPONSE, 32 * 1024)
        metrics.HISTORY.clear()
        for sequence in range(1, metrics.MAX_HISTORY + 1):
            metrics.HISTORY.append({
                "payload": self.sample_payload,
                "history": {
                    "run_id": "a" * 32,
                    "sequence": sequence,
                    "timestamp": "2026-11-20T00:00:00.000Z",
                    "gpu_util": None,
                    "vram_pct": None,
                    "gpu_temp": None,
                    "availability": {
                        name: "permission_denied"
                        for name in ("cpu", "memory", "gpus", "temperatures")
                    },
                },
            })

        request = urllib.request.Request(self.base + "/history?n=60",
                                         headers={"Host": "10.0.2.100:19532"})
        with urllib.request.urlopen(request, timeout=2) as response:
            body = response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get("Content-Length"), str(len(body)))
        self.assertGreater(len(body), metrics.MAX_RESPONSE,
                           "a valid 60-row history can exceed the unchanged metrics budget")
        self.assertLessEqual(len(body), metrics.MAX_HISTORY_RESPONSE)
        self.assertEqual(len(json.loads(body)), metrics.MAX_HISTORY)

        metrics.sample = lambda: {"padding": "x" * metrics.MAX_RESPONSE}
        status, data = self.request("/metrics")
        self.assertEqual(status, 503)
        self.assertEqual(data, {"source": "host", "unavailable": True})

    def test_history_response_over_budget_fails_closed(self):
        metrics.HISTORY.clear()
        metrics.HISTORY.append({
            "payload": self.sample_payload,
            "history": {"padding": "x" * metrics.MAX_HISTORY_RESPONSE},
        })
        status, data = self.request("/history?n=1")
        self.assertEqual(status, 503)
        self.assertEqual(data, {"source": "host", "unavailable": True})

    def test_listener_port_override_is_bounded_for_profile_instances(self):
        self.assertEqual(metrics.configured_port(None), metrics.DEFAULT_PORT)
        self.assertEqual(metrics.configured_port("23456"), 23456)
        for value in ("", "80", "0", "65536", "+23456", " 23456", "２３４５６"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                metrics.configured_port(value)

    def test_writes_and_arbitrary_paths_are_rejected(self):
        status, data = self.request("/metrics", method="POST")
        self.assertEqual(status, 405)
        self.assertEqual(data["error"], "method_not_allowed")
        self.assertEqual(self.request("/anything")[0], 404)

    def test_shutdown_releases_port_for_immediate_rebind(self):
        port = self.server.server_address[1]
        self.assertEqual(self.request("/metrics")[0], 200)
        old_server, old_thread = self.server, self.thread
        self._stop_server(old_server, old_thread)
        self.server = self.thread = None

        metrics.PORT = port
        rebound, rebound_thread = self._start_server()
        self.server, self.thread = rebound, rebound_thread
        self.base = f"http://127.0.0.1:{port}"
        self.assertEqual(self.request("/metrics")[0], 200)

    def test_live_listener_rejects_second_bind_and_remains_responsive(self):
        address = self.server.server_address
        metrics.PORT = address[1]
        with self.assertRaises(OSError) as raised:
            metrics.LoopbackServer()
        self.assertEqual(raised.exception.errno, errno.EADDRINUSE)
        self.assertEqual(self.request("/metrics")[0], 200)

    def test_canonical_snapshot_is_labeled_and_projected_without_guest_values(self):
        cpu = {"model": "Host CPU", "load_avg_1m": 0.1, "load_avg_5m": 0.2,
               "load_avg_15m": 0.3, "num_cpus": 8}
        memory = {"total_gb": 32, "used_gb": 12, "available_gb": 20, "percent": 37.5}
        gpu = [{"index": 0, "name": "Host GPU", "temperature_c": 48,
                "utilization_percent": 10, "memory_total_mb": 8192,
                "memory_used_mb": 1024, "memory_free_mb": 7168}]
        snapshot = {
            "run_id": "a" * 32,
            "sequence": 7,
            "sampled_at": "2026-11-20T00:00:00.000Z",
            "cpu": {"status": "ok", "error": None, "value": cpu},
            "memory": {"status": "ok", "error": None, "value": memory},
            "gpus": {"status": "ok", "error": None, "value": gpu},
            "temperatures": {"status": "unavailable", "error": "no_sensors", "value": None},
        }
        result = metrics._public_snapshot(snapshot)
        self.assertEqual(result["source"], "host")
        self.assertEqual(result["run_id"], snapshot["run_id"])
        self.assertEqual(result["sequence"], snapshot["sequence"])
        self.assertEqual(result["timestamp"], snapshot["sampled_at"])
        self.assertEqual(result["gpu"], gpu)
        self.assertEqual(result["cpu"], cpu)
        self.assertEqual(result["memory"], memory)
        self.assertEqual(result["availability"]["temperatures"], "no_sensors")

    @staticmethod
    def _collector_snapshot(run_id, sequence, gpus):
        category = metrics.contract.category
        return {
            "version": 1,
            "run_id": run_id,
            "sequence": sequence,
            "sampled_at": "2026-11-20T00:00:00.000Z",
            "sampling_duration_ms": 1,
            "cpu": category("ok", {
                "model": "Fixture CPU", "load_avg_1m": 0.0, "load_avg_5m": 0.0,
                "load_avg_15m": 0.0, "num_cpus": 4,
            }),
            "memory": category("ok", {
                "total_gb": 4.0, "used_gb": 2.0,
                "available_gb": 2.0, "percent": 50.0,
            }),
            "gpus": gpus,
            "temperatures": category("unavailable", None, "no_sensors"),
        }

    def _prepare_real_sample(self, *, gpus):
        metrics.RUN_ID = "b" * 32
        metrics.SEQUENCE = 0
        metrics.LAST_SAMPLE = 0
        metrics.HISTORY.clear()
        adapters = {"cpu": None, "memory": None, "gpus": None, "temperatures": None}
        with mock.patch.object(metrics.collector, "linux_proc_adapters", return_value=adapters), \
                mock.patch.object(metrics.collector, "collect_snapshot",
                                  side_effect=lambda *, run_id, sequence, **_kwargs:
                                  self._collector_snapshot(run_id, sequence, gpus)):
            return self.old_sample()

    def test_cached_response_keeps_sample_identity_and_history_nulls(self):
        metrics.MIN_SAMPLE_INTERVAL = 60
        unavailable = metrics.contract.category("unavailable", None, "no_device")
        payload = self._prepare_real_sample(gpus=unavailable)
        cached = self.old_sample()

        self.assertEqual(payload["run_id"], "b" * 32)
        self.assertEqual(payload["sequence"], 1)
        self.assertEqual(cached["run_id"], payload["run_id"])
        self.assertEqual(cached["sequence"], payload["sequence"])
        self.assertEqual(len(metrics.HISTORY), 1)
        self.assertEqual(metrics.HISTORY[0]["history"], {
            "run_id": "b" * 32,
            "sequence": 1,
            "timestamp": payload["timestamp"],
            "gpu_util": None,
            "vram_pct": None,
            "gpu_temp": None,
            "availability": payload["availability"],
        })

        metrics.sample = self.old_sample
        with urllib.request.urlopen(urllib.request.Request(
                self.base + "/metrics", headers={"Host": "10.0.2.100:19532"}), timeout=2) as response:
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertEqual(json.loads(response.read()), payload)
        status, history = self.request("/history?n=60")
        self.assertEqual(status, 200)
        self.assertEqual(history, [metrics.HISTORY[0]["history"]])
        self.assertEqual(len(metrics.HISTORY), 1, "cached endpoint requests must not append history")

    def test_history_uses_null_for_invalid_vram_denominator_but_keeps_real_zeroes(self):
        gpu = {
            "index": 0, "name": "Fixture GPU", "temperature_c": 0,
            "utilization_percent": 0, "memory_total_mb": 0,
            "memory_used_mb": 0, "memory_free_mb": 0,
        }
        payload = self._prepare_real_sample(
            gpus=metrics.contract.category("ok", [gpu]))
        history = metrics.HISTORY[0]["history"]
        self.assertEqual(payload["gpu"][0]["utilization_percent"], 0)
        self.assertEqual(history["gpu_util"], 0)
        self.assertEqual(history["gpu_temp"], 0)
        self.assertIsNone(history["vram_pct"])

    def test_new_collection_advances_sequence_within_the_same_service_run(self):
        unavailable = metrics.contract.category("unavailable", None, "no_device")
        first = self._prepare_real_sample(gpus=unavailable)
        metrics.LAST_SAMPLE = time.monotonic() - 1
        adapters = {"cpu": None, "memory": None, "gpus": None, "temperatures": None}
        with mock.patch.object(metrics.collector, "linux_proc_adapters", return_value=adapters), \
                mock.patch.object(metrics.collector, "collect_snapshot",
                                  side_effect=lambda *, run_id, sequence, **_kwargs:
                                  self._collector_snapshot(run_id, sequence, unavailable)):
            second = self.old_sample()

        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual((first["sequence"], second["sequence"]), (1, 2))
        self.assertEqual([item["payload"]["sequence"] for item in metrics.HISTORY], [1, 2])


if __name__ == "__main__":
    unittest.main()
