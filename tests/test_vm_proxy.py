import http.client
import importlib.util
import ipaddress
import os
import signal
import socket
import socketserver
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


class _RecordingUpstreamHandler(socketserver.BaseRequestHandler):
    def handle(self):
        request = bytearray()
        while not request.endswith(b"\r\n\r\n"):
            chunk = self.request.recv(4096)
            if not chunk:
                return
            request.extend(chunk)
            if len(request) > 65536:
                return
        self.server.requests.append(bytes(request))

        status = self.server.response_status
        if request.startswith(b"CONNECT "):
            reason = b"Connection established" if status == 200 else b"Forbidden"
            self.request.sendall(f"HTTP/1.1 {status} ".encode() + reason + b"\r\n\r\n")
            if status == 200:
                while True:
                    try:
                        payload = self.request.recv(4096)
                    except OSError:
                        return
                    if not payload:
                        return
                    self.request.sendall(payload)
        else:
            body = b"parent-response"
            reason = b"OK" if status == 200 else b"Forbidden"
            response = (
                f"HTTP/1.1 {status} ".encode()
                + reason
                + b"\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            self.request.sendall(response)


class _RecordingUpstreamServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, response_status=200):
        self.requests = []
        self.response_status = response_status
        super().__init__(("127.0.0.1", 0), _RecordingUpstreamHandler)


class _RecordingOriginHandler(socketserver.BaseRequestHandler):
    def handle(self):
        request = bytearray()
        while not request.endswith(b"\r\n\r\n"):
            chunk = self.request.recv(4096)
            if not chunk:
                return
            request.extend(chunk)
        self.server.requests.append(bytes(request))
        body = b"direct-origin"
        self.request.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )


class _RecordingOriginServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.requests = []
        super().__init__(("127.0.0.1", 0), _RecordingOriginHandler)


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
        self.assertTrue(proxy.allowed("api.openai.com", True, {"=api.openai.com"}))
        self.assertFalse(proxy.allowed("v1.api.openai.com", True, {"=api.openai.com"}))
        self.assertFalse(proxy.allowed("example.com", False, set()))

    def test_upstream_proxy_parser_accepts_only_plain_proxy_endpoints(self):
        self.assertEqual(
            proxy.parse_upstream_proxy("http://10.0.2.100:3128"), ("10.0.2.100", 3128)
        )
        for endpoint in (
            "https://10.0.2.100:3128",
            "http://user:secret@10.0.2.100:3128",
            "http://10.0.2.100:3128/path",
            "http://10.0.2.100:3128/?target=example.com",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                proxy.parse_upstream_proxy(endpoint)

    def _start_chained_proxy(self, manifest, upstream):
        server = proxy.ThreadingHTTPServer(("127.0.0.1", 0), proxy.Proxy)
        server.policy = proxy.PolicyStore(manifest)
        server.upstream_proxy = upstream
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def test_nested_http_request_chains_without_direct_origin_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / ".mester-egress"
            manifest.write_text("permission=true\n")
            parent = _RecordingUpstreamServer()
            parent_thread = threading.Thread(target=parent.serve_forever, daemon=True)
            parent_thread.start()
            child, child_thread = self._start_chained_proxy(
                manifest, ("127.0.0.1", parent.server_address[1])
            )
            try:
                with patch.object(proxy, "connect_public") as direct_connect:
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", child.server_port, timeout=3
                    )
                    try:
                        connection.request(
                            "GET",
                            "http://public.example/packages?channel=stable",
                            headers={
                                "Host": "public.example",
                                "Proxy-Authorization": "must-not-forward",
                            },
                        )
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.read(), b"parent-response")
                    finally:
                        connection.close()
                    direct_connect.assert_not_called()

                self.assertEqual(len(parent.requests), 1)
                self.assertTrue(
                    parent.requests[0].startswith(
                        b"GET http://public.example/packages?channel=stable HTTP/1.1\r\n"
                    )
                )
                self.assertNotIn(b"must-not-forward", parent.requests[0])
            finally:
                child.shutdown()
                child_thread.join(timeout=2)
                child.server_close()
                parent.shutdown()
                parent_thread.join(timeout=2)
                parent.server_close()

    def test_nested_child_policy_denial_does_not_reach_parent_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / ".mester-egress"
            manifest.write_text("permission=false\n")
            parent = _RecordingUpstreamServer()
            parent_thread = threading.Thread(target=parent.serve_forever, daemon=True)
            parent_thread.start()
            child, child_thread = self._start_chained_proxy(
                manifest, ("127.0.0.1", parent.server_address[1])
            )
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", child.server_port, timeout=3
                )
                try:
                    connection.request("GET", "http://public.example/")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 403)
                    response.read()
                finally:
                    connection.close()
                self.assertEqual(parent.requests, [])
            finally:
                child.shutdown()
                child_thread.join(timeout=2)
                child.server_close()
                parent.shutdown()
                parent_thread.join(timeout=2)
                parent.server_close()

    def test_nested_proxy_preserves_parent_denial(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / ".mester-egress"
            manifest.write_text("permission=true\n")
            parent = _RecordingUpstreamServer(response_status=403)
            parent_thread = threading.Thread(target=parent.serve_forever, daemon=True)
            parent_thread.start()
            child, child_thread = self._start_chained_proxy(
                manifest, ("127.0.0.1", parent.server_address[1])
            )
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", child.server_port, timeout=3
                )
                try:
                    connection.request("GET", "http://public.example/")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 403)
                    self.assertEqual(response.read(), b"parent-response")
                finally:
                    connection.close()
                self.assertEqual(len(parent.requests), 1)
            finally:
                child.shutdown()
                child_thread.join(timeout=2)
                child.server_close()
                parent.shutdown()
                parent_thread.join(timeout=2)
                parent.server_close()

    def test_nested_connect_is_tunneled_through_parent_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / ".mester-egress"
            manifest.write_text("permission=true\n")
            parent = _RecordingUpstreamServer()
            parent_thread = threading.Thread(target=parent.serve_forever, daemon=True)
            parent_thread.start()
            child, child_thread = self._start_chained_proxy(
                manifest, ("127.0.0.1", parent.server_address[1])
            )
            connection = None
            try:
                connection = socket.create_connection(
                    ("127.0.0.1", child.server_port), timeout=3
                )
                connection.sendall(
                    b"CONNECT public.example:443 HTTP/1.1\r\n"
                    b"Host: public.example:443\r\n\r\n"
                )
                response_headers = bytearray()
                while not response_headers.endswith(b"\r\n\r\n"):
                    response_headers.extend(connection.recv(1))
                self.assertTrue(response_headers.startswith(b"HTTP/1.1 200 "))
                self.assertIn(b"CONNECT public.example:443 HTTP/1.1\r\n", parent.requests[0])

                connection.sendall(b"nested-tunnel-probe")
                self.assertEqual(connection.recv(len(b"nested-tunnel-probe")), b"nested-tunnel-probe")
            finally:
                if connection is not None:
                    connection.close()
                child.shutdown()
                child_thread.join(timeout=2)
                child.server_close()
                parent.shutdown()
                parent_thread.join(timeout=2)
                parent.server_close()

    def test_nested_connect_preserves_parent_denial(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / ".mester-egress"
            manifest.write_text("permission=true\n")
            parent = _RecordingUpstreamServer(response_status=403)
            parent_thread = threading.Thread(target=parent.serve_forever, daemon=True)
            parent_thread.start()
            child, child_thread = self._start_chained_proxy(
                manifest, ("127.0.0.1", parent.server_address[1])
            )
            connection = None
            try:
                connection = socket.create_connection(
                    ("127.0.0.1", child.server_port), timeout=3
                )
                connection.sendall(
                    b"CONNECT public.example:443 HTTP/1.1\r\n"
                    b"Host: public.example:443\r\n\r\n"
                )
                response_headers = bytearray()
                while not response_headers.endswith(b"\r\n\r\n"):
                    response_headers.extend(connection.recv(1))
                self.assertTrue(response_headers.startswith(b"HTTP/1.1 403 "))
                self.assertIn(b"CONNECT public.example:443 HTTP/1.1\r\n", parent.requests[0])
            finally:
                if connection is not None:
                    connection.close()
                child.shutdown()
                child_thread.join(timeout=2)
                child.server_close()
                parent.shutdown()
                parent_thread.join(timeout=2)
                parent.server_close()

    def test_nested_upstream_connection_failure_returns_502(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / ".mester-egress"
            manifest.write_text("permission=true\n")
            unavailable = socket.socket()
            unavailable.bind(("127.0.0.1", 0))
            upstream_port = unavailable.getsockname()[1]
            unavailable.close()
            child, child_thread = self._start_chained_proxy(
                manifest, ("127.0.0.1", upstream_port)
            )
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", child.server_port, timeout=3
                )
                try:
                    connection.request("GET", "http://public.example/")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 502)
                    response.read()
                finally:
                    connection.close()
            finally:
                child.shutdown()
                child_thread.join(timeout=2)
                child.server_close()

    def test_top_level_proxy_uses_direct_path_without_parent_chaining(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / ".mester-egress"
            manifest.write_text("permission=true\n")
            origin = _RecordingOriginServer()
            origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
            origin_thread.start()
            server = proxy.ThreadingHTTPServer(("127.0.0.1", 0), proxy.Proxy)
            server.policy = proxy.PolicyStore(manifest)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.object(
                    proxy,
                    "connect_public",
                    side_effect=lambda _host, _port: socket.create_connection(
                        origin.server_address, timeout=3
                    ),
                ) as direct_connect:
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", server.server_port, timeout=3
                    )
                    try:
                        connection.request("GET", "http://public.example/path")
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.read(), b"direct-origin")
                    finally:
                        connection.close()
                    direct_connect.assert_called_once_with("public.example", 80)
                self.assertTrue(origin.requests[0].startswith(b"GET /path HTTP/1.1\r\n"))
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()
                origin.shutdown()
                origin_thread.join(timeout=2)
                origin.server_close()

    def test_ipv4_mapped_ipv6_uses_ipv4_public_unicast_classification(self):
        self.assertFalse(proxy._is_public_unicast(proxy.ipaddress.ip_address("::ffff:127.0.0.1")))
        self.assertFalse(proxy._is_public_unicast(proxy.ipaddress.ip_address("::ffff:224.0.0.1")))
        self.assertTrue(proxy._is_public_unicast(proxy.ipaddress.ip_address("::ffff:93.184.216.34")))

    def test_ipv6_transition_addresses_apply_embedded_ipv4_policy(self):
        self.assertFalse(proxy._is_public_unicast(ipaddress.ip_address("2002:0a00:0202::1")))
        self.assertFalse(proxy._is_public_unicast(ipaddress.ip_address("2001:0000:0a00:0202::1")))
        self.assertFalse(proxy._is_public_unicast(ipaddress.ip_address("64:ff9b::0a00:0202")))
        self.assertTrue(proxy._is_public_unicast(ipaddress.ip_address("64:ff9b::5db8:d822")))

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
                        proxy.connect_public("auth.openai.com", 443, blocked_networks=())
                    sock.assert_not_called()

    def test_public_destination_in_host_connected_network_is_rejected(self):
        answer = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        blocked = (ipaddress.ip_network("93.184.216.0/24"),)
        with patch.object(proxy.socket, "getaddrinfo", return_value=[answer]), patch.object(
            proxy, "host_networks", return_value=blocked
        ) as routes, patch.object(proxy.socket, "socket") as sock:
            with self.assertRaises(PermissionError):
                proxy.connect_public("public.example", 443)
            routes.assert_called_once_with()
            sock.assert_not_called()

    def test_ipv4_mapped_public_destination_in_host_network_is_rejected(self):
        answer = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:93.184.216.34", 443, 0, 0))
        blocked = (ipaddress.ip_network("93.184.216.0/24"),)
        with patch.object(proxy.socket, "getaddrinfo", return_value=[answer]), patch.object(proxy.socket, "socket") as sock:
            with self.assertRaises(PermissionError):
                proxy.connect_public("public.example", 443, blocked_networks=blocked)
            sock.assert_not_called()

    def test_nat64_public_destination_in_host_network_is_rejected(self):
        answer = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("64:ff9b::5db8:d822", 443, 0, 0))
        blocked = (ipaddress.ip_network("93.184.216.0/24"),)
        with patch.object(proxy.socket, "getaddrinfo", return_value=[answer]), patch.object(proxy.socket, "socket") as sock:
            with self.assertRaises(PermissionError):
                proxy.connect_public("public.example", 443, blocked_networks=blocked)
            sock.assert_not_called()

    def test_host_network_discovery_includes_connected_and_local_prefixes(self):
        route_text = (
            "Iface Destination Gateway Flags RefCnt Use Metric Mask\n"
            "eth0 00000000 010013AC 0003 0 0 0 00000000\n"
            "eth0 000013AC 00000000 0001 0 0 0 0000FFFF\n"
        )
        ipv6_route_text = (
            "20010db8000100000000000000000000 40 "
            "00000000000000000000000000000000 00 "
            "00000000000000000000000000000000 00000000 00000000 00000000 00000001 eth0\n"
        )
        ipv6_addresses_text = "20010db8000200000000000000000001 02 40 00 80 eth0\n"
        fib_trie_text = "Local:\n  |-- 8.8.8.8\n     /32 host LOCAL\n"

        networks = proxy._host_networks_from_proc(
            route_text, ipv6_route_text, ipv6_addresses_text, fib_trie_text
        )

        self.assertIn(ipaddress.ip_network("172.19.0.0/16"), networks)
        self.assertIn(ipaddress.ip_network("2001:db8:1::/64"), networks)
        self.assertIn(ipaddress.ip_network("2001:db8:2::/64"), networks)
        self.assertIn(ipaddress.ip_network("8.8.8.8/32"), networks)
        self.assertNotIn(ipaddress.ip_network("0.0.0.0/0"), networks)
        self.assertFalse(proxy._is_allowed_public_destination(ipaddress.ip_address("8.8.8.8"), networks))

    def test_malformed_host_route_table_fails_closed(self):
        with self.assertRaises(PermissionError):
            proxy._host_networks_from_proc(
                "Iface Destination Gateway Flags RefCnt Use Metric Mask\n"
                "eth0 invalid 00000000 0001 0 0 0 0000FFFF\n",
                "",
                "",
                "",
            )

        with self.assertRaises(PermissionError):
            proxy._host_networks_from_proc(
                "Iface Destination Gateway Flags RefCnt Use Metric Mask\n"
                "eth0\n",
                "",
                "",
                "Local:\n  |-- 127.0.0.1\n     /32 host LOCAL\n",
            )

        with self.assertRaises(PermissionError):
            proxy._host_networks_from_proc(
                "Iface Destination Gateway Flags RefCnt Use Metric Mask\n",
                "",
                "",
                "",
            )

    def test_public_connection_uses_validated_ip_without_second_dns_lookup(self):
        address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        with patch.object(proxy.socket, "getaddrinfo", return_value=[address]) as resolve, patch.object(proxy.socket, "socket") as sock:
            connection = proxy.connect_public("auth.openai.com", 443, blocked_networks=())
            resolve.assert_called_once_with("auth.openai.com", 443, type=socket.SOCK_STREAM)
            self.assertIs(connection, sock.return_value)
            connection.connect.assert_called_once_with(("93.184.216.34", 443))

    def test_project_policy_permits_public_http_domains_through_the_proxy(self):
        enabled, domains = proxy.policy(Path(__file__).parents[1] / ".mester-egress")
        self.assertEqual((enabled, domains), (True, set()))
        for host in ("auth.docker.io", "registry-1.docker.io", "registry.npmjs.org"):
            self.assertTrue(proxy.allowed(host, enabled, domains), host)
        self.assertTrue(proxy.allowed("agent-work.example", enabled, domains))
        self.assertTrue(proxy.allowed("registry.npmjs.org.attacker.invalid", enabled, domains))

    def test_project_policy_permits_chatgpt_hosts_as_public_destinations(self):
        enabled, domains = proxy.policy(Path(__file__).parents[1] / ".mester-egress")
        self.assertTrue(proxy.allowed("auth.openai.com", enabled, domains))
        self.assertTrue(proxy.allowed("chatgpt.com", enabled, domains))
        self.assertTrue(proxy.allowed("backend.chatgpt.com", enabled, domains))
        self.assertTrue(proxy.allowed("api.openai.com", enabled, domains))
        self.assertTrue(proxy.allowed("v1.api.openai.com", enabled, domains))

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
