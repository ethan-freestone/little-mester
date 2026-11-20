#!/usr/bin/env python3
"""Host metrics HTTP boundary tests with a real loopback listener."""

import importlib.util
import json
import pathlib
import sys
import threading
import urllib.error
import urllib.request
import unittest


ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "system_vm_metrics_test_subject", ROOT / "lib/vm/system-vm-metrics.py")
metrics = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = metrics
SPEC.loader.exec_module(metrics)


class MetricsHttpBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.old_port = metrics.PORT
        metrics.PORT = 0
        self.server = metrics.LoopbackServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.old_sample = metrics.sample
        self.sample_payload = {
            "source": "host", "timestamp": "2026-11-20T00:00:00.000Z",
            "cpu": None, "memory": None, "gpu": [],
            "availability": {"cpu": "unsupported", "memory": "permission_denied",
                              "gpus": "no_device", "temperatures": "no_sensors"},
            "temperatures": None,
        }
        metrics.sample = lambda: self.sample_payload

    def tearDown(self):
        metrics.sample = self.old_sample
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        metrics.PORT = self.old_port

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
        self.assertIn("availability", data)
        self.assertEqual(self.request("/metrics?other=1")[0], 404)
        self.assertEqual(self.request("/history?n=60")[0], 200)
        self.assertEqual(self.request("/history?n=61")[0], 400)
        self.assertEqual(self.request("/history?n=1&n=2")[0], 400)
        self.assertEqual(self.request("/metrics", host="attacker.example")[0], 400)

    def test_writes_and_arbitrary_paths_are_rejected(self):
        status, data = self.request("/metrics", method="POST")
        self.assertEqual(status, 405)
        self.assertEqual(data["error"], "method_not_allowed")
        self.assertEqual(self.request("/anything")[0], 404)

    def test_canonical_snapshot_is_labeled_and_projected_without_guest_values(self):
        cpu = {"model": "Host CPU", "load_avg_1m": 0.1, "load_avg_5m": 0.2,
               "load_avg_15m": 0.3, "num_cpus": 8}
        memory = {"total_gb": 32, "used_gb": 12, "available_gb": 20, "percent": 37.5}
        gpu = [{"index": 0, "name": "Host GPU", "temperature_c": 48,
                "utilization_percent": 10, "memory_total_mb": 8192,
                "memory_used_mb": 1024, "memory_free_mb": 7168}]
        snapshot = {
            "sampled_at": "2026-11-20T00:00:00.000Z",
            "cpu": {"status": "ok", "error": None, "value": cpu},
            "memory": {"status": "ok", "error": None, "value": memory},
            "gpus": {"status": "ok", "error": None, "value": gpu},
            "temperatures": {"status": "unavailable", "error": "no_sensors", "value": None},
        }
        result = metrics._public_snapshot(snapshot)
        self.assertEqual(result["source"], "host")
        self.assertEqual(result["gpu"], gpu)
        self.assertEqual(result["cpu"], cpu)
        self.assertEqual(result["memory"], memory)
        self.assertEqual(result["availability"]["temperatures"], "no_sensors")


if __name__ == "__main__":
    unittest.main()
