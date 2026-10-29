import importlib.util
import ipaddress
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "vm_proxy", Path(__file__).parents[1] / "lib" / "vm" / "proxy.py"
)
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)


class ProxyTests(unittest.TestCase):
    def test_policy_defaults_to_public_egress_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(proxy.policy(Path(directory) / ".mester-egress"), (True, set()))

    def test_policy_reads_deny_and_allowlist_without_shell_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / ".mester-egress"
            manifest.write_text("permission=false\nallowed_domains=example.com, pypi.org\n")
            self.assertEqual(proxy.policy(manifest), (False, {"example.com", "pypi.org"}))

    def test_allowlist_accepts_domain_and_real_subdomains_only(self):
        self.assertTrue(proxy.allowed("example.com", True, {"example.com"}))
        self.assertTrue(proxy.allowed("packages.example.com", True, {"example.com"}))
        self.assertFalse(proxy.allowed("evil-example.com", True, {"example.com"}))
        self.assertFalse(proxy.allowed("example.com", False, set()))

    def test_private_and_loopback_addresses_are_not_global(self):
        for value in ("127.0.0.1", "10.0.2.2", "169.254.1.1", "::1"):
            self.assertFalse(ipaddress.ip_address(value).is_global)

    def test_project_policy_permits_build_registries_without_lookalike_domains(self):
        enabled, domains = proxy.policy(Path(__file__).parents[1] / ".mester-egress")
        for host in ("auth.docker.io", "registry-1.docker.io", "registry.npmjs.org"):
            self.assertTrue(proxy.allowed(host, enabled, domains), host)
        self.assertFalse(proxy.allowed("registry.npmjs.org.attacker.invalid", enabled, domains))

    def test_project_policy_permits_only_the_openai_hosts_used_by_chatgpt_oauth(self):
        enabled, domains = proxy.policy(Path(__file__).parents[1] / ".mester-egress")
        self.assertTrue(proxy.allowed("auth.openai.com", enabled, domains))
        self.assertTrue(proxy.allowed("chatgpt.com", enabled, domains))
        self.assertTrue(proxy.allowed("backend.chatgpt.com", enabled, domains))
        self.assertFalse(proxy.allowed("auth.openai.com.attacker.invalid", enabled, domains))
        self.assertFalse(proxy.allowed("api.openai.com", enabled, domains))

    def test_policy_store_reloads_changed_manifest_without_restarting_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / ".mester-egress"
            manifest.write_text("permission=true\nallowed_domains=auth.openai.com\n")
            store = proxy.PolicyStore(manifest)
            self.assertTrue(store.allows("auth.openai.com"))
            self.assertFalse(store.allows("chatgpt.com"))

            manifest.write_text("permission=true\nallowed_domains=chatgpt.com\n")
            # A running proxy retains a coherent snapshot until explicitly signaled.
            self.assertTrue(store.allows("auth.openai.com"))
            self.assertFalse(store.allows("chatgpt.com"))

            store.reload()
            self.assertFalse(store.allows("auth.openai.com"))
            self.assertTrue(store.allows("chatgpt.com"))

    def test_failed_policy_reload_keeps_last_known_good_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / ".mester-egress"
            manifest.write_text("permission=true\nallowed_domains=auth.openai.com\n")
            store = proxy.PolicyStore(manifest)
            manifest.write_bytes(b"\xff")
            with self.assertRaises(UnicodeDecodeError):
                store.reload()
            self.assertTrue(store.allows("auth.openai.com"))
            self.assertFalse(store.allows("chatgpt.com"))

            manifest.unlink()
            with self.assertRaises(FileNotFoundError):
                store.reload()
            self.assertTrue(store.allows("auth.openai.com"))
            self.assertFalse(store.allows("chatgpt.com"))

    def test_sighup_reloads_policy_without_rebinding_proxy_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            manifest = workspace / ".mester-egress"
            manifest.write_text("permission=true\nallowed_domains=example.com\n")
            port_file = workspace / "proxy.port"
            marker_file = workspace / "proxy.reload-ready"
            log_file = workspace / "proxy.log"
            with log_file.open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).parents[1] / "lib" / "vm" / "proxy.py"),
                        "--workspace",
                        str(workspace),
                        "--port-file",
                        str(port_file),
                        "--reload-marker",
                        str(marker_file),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=log,
                )
                try:
                    deadline = time.monotonic() + 5
                    while not port_file.exists() and time.monotonic() < deadline:
                        if process.poll() is not None:
                            self.fail("proxy exited before writing its listener port")
                        time.sleep(0.02)
                    self.assertTrue(port_file.exists(), "proxy did not become ready")
                    listener_port = int(port_file.read_text(encoding="utf-8"))

                    manifest.write_text("permission=false\nallowed_domains=example.com\n")
                    os.kill(process.pid, signal.SIGHUP)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if "egress policy reloaded" in log_file.read_text(encoding="utf-8"):
                            break
                        if process.poll() is not None:
                            self.fail("proxy exited while reloading policy")
                        time.sleep(0.02)
                    self.assertIn("egress policy reloaded", log_file.read_text(encoding="utf-8"))
                    self.assertIsNone(process.poll())

                    with socket.create_connection(("127.0.0.1", listener_port), timeout=1):
                        pass
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=5)
                    self.assertFalse(marker_file.exists(), "proxy should remove its reload marker on shutdown")

    def test_proxy_can_rebind_the_same_recorded_loopback_port(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".mester-egress").write_text("permission=false\n")
            port_file = workspace / "proxy.port"
            marker_file = workspace / "proxy.reload-ready"
            log_file = workspace / "proxy.log"
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                recorded_port = reservation.getsockname()[1]
            with log_file.open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).parents[1] / "lib" / "vm" / "proxy.py"),
                        "--workspace",
                        str(workspace),
                        "--port-file",
                        str(port_file),
                        "--port",
                        str(recorded_port),
                        "--reload-marker",
                        str(marker_file),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=log,
                )
                try:
                    deadline = time.monotonic() + 5
                    while not port_file.exists() and time.monotonic() < deadline:
                        if process.poll() is not None:
                            self.fail("proxy exited before binding the recorded listener port")
                        time.sleep(0.02)
                    self.assertEqual(int(port_file.read_text(encoding="utf-8")), recorded_port)
                    self.assertTrue(marker_file.exists())
                    with socket.create_connection(("127.0.0.1", recorded_port), timeout=1):
                        pass
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
