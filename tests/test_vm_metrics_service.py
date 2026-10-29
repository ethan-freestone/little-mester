import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "agent" / "metrics" / "gather.py"


class VmMetricsEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = Path(self.tmp.name) / "proc"
        self.proc.mkdir()
        (self.proc / "loadavg").write_text("0.25 0.50 0.75 1/10 123\n")
        (self.proc / "cpuinfo").write_text("model name\t: test-host-cpu\n")
        (self.proc / "meminfo").write_text(
            "MemTotal:       1048576 kB\nMemAvailable:    524288 kB\n"
        )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]

        env = os.environ.copy()
        env.update(
            METRICS_HOST_PROC=str(self.proc),
            METRICS_HOST_HWMON=str(Path(self.tmp.name) / "no-hwmon"),
            METRICS_HOST_THERMAL=str(Path(self.tmp.name) / "no-thermal"),
            METRICS_BIND_HOST="127.0.0.1",
            METRICS_BIND_PORT=str(self.port),
        )
        self.process = subprocess.Popen(
            [sys.executable, str(COLLECTOR)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.base = f"http://127.0.0.1:{self.port}"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail("metrics collector exited before becoming ready")
            try:
                with urllib.request.urlopen(f"{self.base}/summary", timeout=0.2):
                    break
            except (OSError, urllib.error.URLError):
                time.sleep(0.05)
        else:
            self.fail("metrics collector did not become ready")

    def tearDown(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.tmp.cleanup()

    def test_curated_metrics_endpoint_reads_only_configured_host_sources(self):
        with urllib.request.urlopen(f"{self.base}/metrics", timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["cpu"]["model"], "test-host-cpu")
        self.assertEqual(payload["cpu"]["load_avg_1m"], 0.25)
        self.assertEqual(payload["memory"]["total_gb"], 1.0)
        self.assertEqual(payload["gpu"], [])
        self.assertEqual(payload["temperatures"], [])

    def test_summary_and_history_endpoints_are_available(self):
        with urllib.request.urlopen(f"{self.base}/summary", timeout=2) as response:
            self.assertIn("load 0.25/0.5", response.read().decode())
        with urllib.request.urlopen(f"{self.base}/history?n=2", timeout=2) as response:
            self.assertIsInstance(json.load(response), list)

    def test_unlisted_endpoint_is_not_exposed(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(f"{self.base}/etc/passwd", timeout=2)
        self.assertEqual(raised.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
