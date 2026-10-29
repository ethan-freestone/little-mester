import importlib.util
import http.client
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


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

    def test_ipv4_mapped_ipv6_uses_ipv4_public_unicast_classification(self):
        self.assertFalse(proxy._is_public_unicast(proxy.ipaddress.ip_address("::ffff:127.0.0.1")))
        self.assertFalse(proxy._is_public_unicast(proxy.ipaddress.ip_address("::ffff:224.0.0.1")))
        self.assertTrue(proxy._is_public_unicast(proxy.ipaddress.ip_address("::ffff:93.184.216.34")))

    def test_http_and_connect_reject_nonpublic_and_multicast_targets(self):
        # Empty domain policy deliberately isolates the IP boundary from the
        # allowlist. Exercise real proxy request handling without external I/O.
        server = proxy.ThreadingHTTPServer(("127.0.0.1", 0), proxy.Proxy)
        server.policy = Mock(allows=lambda _host: True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for host in (
                "0.0.0.0", "127.0.0.1", "10.0.2.2", "172.16.0.1", "192.168.1.1",
                "169.254.169.254", "100.64.0.1", "192.0.2.1", "224.0.0.1", "255.255.255.255",
                "[::1]", "[fc00::1]", "[fe80::1]", "[ff02::1]",
                "[::ffff:127.0.0.1]", "[::ffff:224.0.0.1]",
            ):
                for method, target in (("GET", f"http://{host}/"), ("CONNECT", f"{host}:443")):
                    with self.subTest(host=host, method=method):
                        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                        try:
                            connection.request(method, target)
                            response = connection.getresponse()
                            response.read()
                            self.assertEqual(response.status, 403)
                        finally:
                            connection.close()
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_unapproved_chatgpt_model_endpoint_is_denied_before_dns_or_connect(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / ".mester-egress"
            manifest.write_text("permission=true\nallowed_domains=auth.openai.com,chatgpt.com\n")
            server = proxy.ThreadingHTTPServer(("127.0.0.1", 0), proxy.Proxy)
            server.policy = proxy.PolicyStore(manifest)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.object(proxy, "connect_public") as connect:
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    try:
                        connection.request("CONNECT", "api.openai.com:443")
                        response = connection.getresponse()
                        response.read()
                        self.assertEqual(response.status, 403)
                    finally:
                        connection.close()
                    connect.assert_not_called()
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()

    def test_allowlisted_hostname_with_mixed_dns_answers_cannot_reach_host(self):
        unsafe_addresses = (
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:127.0.0.1", 443, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("224.0.0.1", 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("ff02::1", 443, 0, 0)),
        )
        for unsafe in unsafe_addresses:
            with self.subTest(unsafe=unsafe[4][0]):
                answers = [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
                    unsafe,
                ]
                with patch.object(proxy.socket, "getaddrinfo", return_value=answers), patch.object(proxy.socket, "socket") as sock:
                    with self.assertRaises(PermissionError):
                        proxy.connect_public("auth.openai.com", 443)
                    sock.assert_not_called()

    def test_public_connection_uses_validated_ip_without_second_dns_lookup(self):
        address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        with patch.object(proxy.socket, "getaddrinfo", return_value=[address]) as resolve, patch.object(proxy.socket, "socket") as sock:
            connection = proxy.connect_public("auth.openai.com", 443)
            resolve.assert_called_once_with("auth.openai.com", 443, type=socket.SOCK_STREAM)
            self.assertIs(connection, sock.return_value)
            connection.connect.assert_called_once_with(("93.184.216.34", 443))

    def test_project_policy_permits_build_registries_without_lookalike_domains(self):
        enabled, domains = proxy.policy(Path(__file__).parents[1] / ".mester-egress")
        for host in ("auth.docker.io", "registry-1.docker.io", "registry.npmjs.org"):
            self.assertTrue(proxy.allowed(host, enabled, domains), host)
        self.assertFalse(proxy.allowed("registry.npmjs.org.attacker.invalid", enabled, domains))

    def test_project_policy_permits_only_the_openai_hosts_used_by_chatgpt(self):
        enabled, domains = proxy.policy(Path(__file__).parents[1] / ".mester-egress")
        self.assertTrue(proxy.allowed("auth.openai.com", enabled, domains))
        self.assertTrue(proxy.allowed("chatgpt.com", enabled, domains))
        self.assertTrue(proxy.allowed("backend.chatgpt.com", enabled, domains))
        self.assertTrue(proxy.allowed("api.openai.com", enabled, domains))
        self.assertFalse(proxy.allowed("v1.api.openai.com", enabled, domains))
        self.assertFalse(proxy.allowed("auth.openai.com.attacker.invalid", enabled, domains))
        self.assertFalse(proxy.allowed("api.openai.com.attacker.invalid", enabled, domains))
        self.assertFalse(proxy.allowed("openai.com", enabled, domains))

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
