"""Fixture-only metrics collector and loopback-service policy tests."""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gather = load_module("vm_metrics_gather_test", ROOT / "agent/metrics/gather.py")
service = load_module("vm_metrics_service_test", ROOT / "lib/vm/metrics_service.py")


class CollectorHTTPTests(unittest.TestCase):
    def setUp(self):
        self.original_latest = gather._latest
        self.original_history = list(gather._history)
        self.original_gpu = gather._gpu_latest
        self.original_collect_gpu = gather._collect_gpu
        self.original_collect_cpu = gather._collect_cpu
        gather._latest = {
            "timestamp": "2026-11-04T00:00:00Z",
            "cpu": {"model": "fixture CPU", "load_avg_1m": 0.1, "num_cpus": 2},
            "memory": {"total_gb": 4.0, "used_gb": 1.0, "available_gb": 3.0, "percent": 25.0},
            "gpu": [{"index": 0, "name": "fixture GPU", "utilization_percent": 7}],
            "temperatures": [],
        }
        gather._history.clear()
        gather._history.extend({"t": str(index), "gpu_util": index} for index in range(300))
        self.server = gather.ThreadingHTTPServer(("127.0.0.1", 0), gather._MetricsHandler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        gather._latest = self.original_latest
        gather._history.clear()
        gather._history.extend(self.original_history)
        gather._gpu_latest = self.original_gpu
        gather._collect_gpu = self.original_collect_gpu
        gather._collect_cpu = self.original_collect_cpu

    def get(self, path: str, timeout: float = 2.0):
        with urlopen(self.base + path, timeout=timeout) as response:
            return response.status, response.headers.get_content_type(), response.read()

    def test_metrics_and_summary_keep_curated_schema(self):
        status, content_type, body = self.get("/metrics")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "application/json")
        self.assertEqual(set(data), {"timestamp", "cpu", "memory", "gpu", "temperatures"})
        self.assertEqual(data["gpu"][0]["name"], "fixture GPU")
        status, content_type, body = self.get("/summary")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "text/plain")
        self.assertIn("fixture GPU", body.decode())

    def test_history_is_bounded_and_rejects_out_of_range_requests(self):
        _, _, body = self.get("/history")
        self.assertEqual(len(json.loads(body)), 120)
        _, _, body = self.get("/history?n=60")
        self.assertEqual(len(json.loads(body)), 60)
        _, _, body = self.get("/history?n=240")
        self.assertEqual(len(json.loads(body)), gather.MAX_HISTORY)
        _, _, body = self.get("/history?n=0")
        self.assertEqual(json.loads(body), [])
        for query in ("n=-1", "n=241", "n=1.2", "n=1&n=2"):
            with self.assertRaises(HTTPError) as raised:
                urlopen(f"{self.base}/history?{query}", timeout=2)
            self.assertEqual(raised.exception.code, 400)

    def test_unknown_endpoint_returns_404(self):
        with self.assertRaises(HTTPError) as raised:
            urlopen(self.base + "/unknown", timeout=2)
        self.assertEqual(raised.exception.code, 404)

    def test_uninitialized_snapshot_does_not_deadlock_on_aggregate(self):
        gather._latest = None
        started = time.monotonic()
        with urlopen(self.base + "/summary", timeout=1) as response:
            self.assertEqual(response.status, 200)
        self.assertLess(time.monotonic() - started, 0.75)

    def test_slow_gpu_collection_does_not_block_http(self):
        gather._latest = None
        entered = threading.Event()

        def slow_gpu():
            entered.set()
            time.sleep(0.75)
            return []

        # The GPU worker may be slow, but a request snapshot uses the latest
        # asynchronous GPU value instead of calling this function inline.
        gather._collect_gpu = slow_gpu
        worker = threading.Thread(target=gather._collect_gpu, daemon=True)
        worker.start()
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        with urlopen(self.base + "/metrics", timeout=0.5) as response:
            self.assertEqual(response.status, 200)
        self.assertLess(time.monotonic() - started, 0.4)
        worker.join(timeout=1)

    def test_parallel_clients_are_served_concurrently(self):
        gather._latest = None
        original_cpu = gather._collect_cpu
        barrier = threading.Barrier(8)

        def slow_cpu():
            barrier.wait(timeout=3)
            return original_cpu()

        gather._collect_cpu = slow_cpu
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _item: self.get("/metrics")[0], range(8)))
        self.assertEqual(results, [200] * 8)


class HostServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vm-metrics-service-")
        self.root = Path(self.temp.name) / "state"
        self.root.mkdir(mode=0o700)
        self.collector = Path(self.temp.name) / "fixture_collector.py"
        self.collector.write_text(textwrap.dedent("""\
            import os
            from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    if self.path != "/summary":
                        self.send_error(404)
                        return
                    body = b"fixture metrics ready"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                def log_message(self, *_args):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", int(os.environ["METRICS_BIND_PORT"])), Handler)
            server.serve_forever()
        """), encoding="utf-8")
        self.port = self._free_port()
        self.started = False

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def tearDown(self):
        if self.started:
            try:
                service.stop_service(self.root, self.port)
            except service.MetricsServiceError:
                pass
        self.temp.cleanup()

    def test_service_is_loopback_verified_and_idempotent(self):
        first = service.ensure_service(self.root, self.collector, self.port)
        self.started = True
        second = service.ensure_service(self.root, self.collector, self.port)
        self.assertEqual(first["state"], "running")
        self.assertEqual(first["pid"], second["pid"])
        self.assertEqual(service.status_service(self.root, self.port)["state"], "running")
        with urlopen(f"http://127.0.0.1:{self.port}/summary", timeout=1) as response:
            self.assertEqual(response.read(), b"fixture metrics ready")
        stopped = service.stop_service(self.root, self.port)
        self.started = False
        self.assertEqual(stopped["state"], "stopped")

    def test_concurrent_ensure_calls_share_one_owned_service(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda _item: service.ensure_service(self.root, self.collector, self.port),
                range(2),
            ))
        self.started = True
        self.assertEqual(results[0]["pid"], results[1]["pid"])
        status = subprocess.run(
            [sys.executable, str(ROOT / "lib/vm/metrics_service.py"), "verify",
             "--state-root", str(self.root), "--port", str(self.port), "--quiet"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(status.returncode, 0, status.stderr)

    def test_unknown_listener_is_never_hijacked_or_signalled(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", self.port))
            listener.listen(1)
            with self.assertRaises(service.MetricsServiceError):
                service.ensure_service(self.root, self.collector, self.port)
            self.assertEqual(service.status_service(self.root, self.port)["state"], "unknown")
            self.assertTrue(service._listener_present(self.port))

    def test_missing_transport_is_reported_without_creating_state(self):
        missing_root = Path(self.temp.name) / "not-created"
        result = service.status_service(missing_root, self.port)
        self.assertEqual(result["state"], "absent")
        self.assertFalse(missing_root.exists())

    def test_root_depth_disables_metrics_and_explicit_child_enable_is_refused(self):
        state = Path(self.temp.name) / "child-state"
        state.mkdir(mode=0o700)
        common = (
            "set -e; source \"$1/lib/vm/config.sh\"; vm_cli_defaults; "
            "VM_PATHS_HELPER=\"$1/lib/vm/paths.py\"; VM_STATE_ROOT=\"$2\"; "
            "vm_cli_parse run --recurse --max-depth 2 \"${@:3}\"; "
            "vm_config_overrides; vm_config_load; printf '%s' \"$VM_CONFIG_JSON\""
        )
        result = subprocess.run(
            ["bash", "-c", common, "child-config", str(ROOT), str(state)],
            capture_output=True, text=True, check=False,
            env={**os.environ, "LAB_VM_RECURSION_DEPTH": "1", "LAB_VM_MAX_DEPTH": "2"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["metrics"], "off")
        refused = subprocess.run(
            ["bash", "-c", common, "child-config", str(ROOT), str(state), "--metrics", "on"],
            capture_output=True, text=True, check=False,
            env={**os.environ, "LAB_VM_RECURSION_DEPTH": "1", "LAB_VM_MAX_DEPTH": "2"},
        )
        self.assertEqual(refused.returncode, 2)
        self.assertIn("child-local metrics endpoint is not implemented", refused.stderr)

    def test_docker_metrics_remains_private_and_default_bind_is_unchanged(self):
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        metrics_section = compose.split("\n  metrics:\n", 1)[1].split("\n  #", 1)[0]
        self.assertIn("networks: [internal]", metrics_section)
        self.assertNotIn("ports:", metrics_section)
        self.assertEqual(gather.BIND_HOST, "0.0.0.0")
        self.assertIn("METRICS_URL=disabled", (ROOT / "lib/vm/services.sh").read_text())
        self.assertIn("unset METRICS_URL", (ROOT / "lab-vm").read_text())


if __name__ == "__main__":
    unittest.main()
