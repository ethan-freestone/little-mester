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
from concurrent.futures import ThreadPoolExecutor
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

    def test_health_is_available(self):
        with urllib.request.urlopen(f"{self.base}/health", timeout=2) as response:
            self.assertEqual(response.read(), b"ok\n")

    def test_slow_gpu_sampling_does_not_block_or_reset_http_requests(self):
        root = Path(self.tmp.name) / "slow-sampling"
        fake_bin = root / "bin"
        fake_modules = root / "modules"
        fake_bin.mkdir(parents=True)
        fake_modules.mkdir()
        fake_smi = fake_bin / "nvidia-smi"
        fake_smi.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--query-gpu=name\" ]; then\n"
            "  printf 'slow-test-gpu\\n'\n"
            "else\n"
            "  sleep 2\n"
            "  printf 'slow-test-gpu, 45, 12, 2048, 512, 1536\\n'\n"
            "fi\n"
        )
        fake_smi.chmod(0o755)
        (fake_modules / "pynvml.py").write_text("raise ImportError('disabled in test')\n")

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            slow_port = sock.getsockname()[1]
        slow_proc = root / "proc"
        slow_proc.mkdir()
        (slow_proc / "loadavg").write_text("0.25 0.50 0.75 1/10 123\n")
        (slow_proc / "cpuinfo").write_text("model name\t: slow-test-cpu\n")
        (slow_proc / "meminfo").write_text(
            "MemTotal:       1048576 kB\nMemAvailable:    524288 kB\n"
        )

        env = os.environ.copy()
        env.update(
            METRICS_HOST_PROC=str(slow_proc),
            METRICS_HOST_HWMON=str(root / "no-hwmon"),
            METRICS_HOST_THERMAL=str(root / "no-thermal"),
            METRICS_BIND_HOST="127.0.0.1",
            METRICS_BIND_PORT=str(slow_port),
            PATH=f"{fake_bin}:{env.get('PATH', '')}",
            PYTHONPATH=str(fake_modules),
        )
        process = subprocess.Popen(
            [sys.executable, str(COLLECTOR)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base = f"http://127.0.0.1:{slow_port}"
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self.fail("slow-sampling metrics collector exited")
                try:
                    with urllib.request.urlopen(f"{base}/health", timeout=0.5) as response:
                        self.assertEqual(response.read(), b"ok\n")
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.05)
            else:
                self.fail("slow-sampling health endpoint did not become ready")

            started = time.monotonic()
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(f"{base}/metrics", timeout=1)
            self.assertEqual(raised.exception.code, 503)
            self.assertLess(time.monotonic() - started, 1.0)

            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(f"{base}/metrics", timeout=1) as response:
                        payload = json.load(response)
                    if payload["gpu"]:
                        self.assertEqual(payload["gpu"][0]["name"], "slow-test-gpu")
                        break
                except urllib.error.HTTPError as exc:
                    if exc.code != 503:
                        raise
                    time.sleep(0.1)
            else:
                self.fail("slow GPU sample never became available")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)

    def test_endpoints_support_concurrent_guest_polling(self):
        paths = ["/summary", "/metrics", "/history?n=2"] * 8

        def fetch(path):
            with urllib.request.urlopen(f"{self.base}{path}", timeout=3) as response:
                body = response.read()
                if path != "/summary":
                    json.loads(body)
                return response.status

        with ThreadPoolExecutor(max_workers=12) as pool:
            statuses = list(pool.map(fetch, paths))
        self.assertEqual(statuses, [200] * len(paths))

    def test_unlisted_endpoint_is_not_exposed(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(f"{self.base}/etc/passwd", timeout=2)
        self.assertEqual(raised.exception.code, 404)

    def test_history_rejects_a_malformed_sample_count(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(f"{self.base}/history?n=not-a-number", timeout=2)
        self.assertEqual(raised.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
