import http.client
import importlib.util
import ipaddress
import json
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


class _SilentUpstreamHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.accepted.set()
        while self.request.recv(4096):
            pass


class _SilentUpstreamServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.accepted = threading.Event()
        super().__init__(("127.0.0.1", 0), _SilentUpstreamHandler)


class ProxyTests(unittest.TestCase):
    def test_policy_defaults_to_public_egress_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(proxy.policy(Path(directory) / ".mester-egress"), (True, set()))

    def test_empty_domain_policy_retains_public_default_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / ".mester-egress"
            manifest.write_text("permission=true\nallowed_domains=\n")
            enabled, domains = proxy.policy(manifest)
            self.assertEqual((enabled, domains), (True, set()))
            for host in (
                "auth.docker.io", "registry-1.docker.io", "registry.npmjs.org",
                "auth.openai.com", "chatgpt.com", "backend.chatgpt.com", "api.openai.com",
            ):
                self.assertTrue(proxy.allowed(host, enabled, domains), host)

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
            "http://@10.0.2.100:3128",
            "http://93.184.216.34:3128",
            "http://parent.example:3128",
            "http://10.0.2.101:3128",
            "http://10.0.2.100:3129",
            "http://10.0.2.100",
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
                                "Connection": "X-Private",
                                "X-Private": "hop-by-hop-canary",
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
                self.assertNotIn(b"hop-by-hop-canary", parent.requests[0])
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
                with patch.object(proxy, "connect_public") as direct_connect:
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
                    direct_connect.assert_not_called()
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
                            if method == "CONNECT":
                                connection.putrequest(method, target, skip_host=True)
                                connection.putheader("Host", target)
                                connection.endheaders()
                            else:
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
                        connection.putrequest("CONNECT", "api.openai.com:443", skip_host=True)
                        connection.putheader("Host", "api.openai.com:443")
                        connection.endheaders()
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
        self.assertEqual(
            (enabled, domains),
            (True, {"pypi.org", "github.com", "npmjs.com", "ubuntu.com", "deb.debian.org"}),
        )
        for host in ("pypi.org", "github.com", "npmjs.com", "pkg.npmjs.com"):
            self.assertTrue(proxy.allowed(host, enabled, domains), host)
        self.assertFalse(proxy.allowed("files.pythonhosted.org", enabled, domains))
        self.assertFalse(proxy.allowed("agent-work.example", enabled, domains))
        self.assertFalse(proxy.allowed("github.com.attacker.invalid", enabled, domains))

    def test_project_policy_denies_unlisted_chatgpt_hosts(self):
        enabled, domains = proxy.policy(Path(__file__).parents[1] / ".mester-egress")
        for host in ("auth.openai.com", "chatgpt.com", "backend.chatgpt.com", "api.openai.com"):
            self.assertFalse(proxy.allowed(host, enabled, domains), host)

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

    def test_denied_reload_keeps_policy_after_malformed_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / ".mester-egress"
            manifest.write_text("permission=true\nallowed_domains=auth.openai.com\n")
            store = proxy.PolicyStore(manifest)
            manifest.write_text("permission=maybe\nallowed_domains=chatgpt.com\n")
            with self.assertRaises(ValueError):
                store.reload()
            self.assertTrue(store.allows("auth.openai.com"))
            self.assertFalse(store.allows("chatgpt.com"))

    def test_denied_request_does_not_log_target_or_query(self):
        from contextlib import redirect_stderr
        from io import StringIO

        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / ".mester-egress"
            manifest.write_text("permission=false\n")
            server = proxy.ThreadingHTTPServer(("127.0.0.1", 0), proxy.Proxy)
            server.policy = proxy.PolicyStore(manifest)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            captured = StringIO()
            try:
                with redirect_stderr(captured):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    try:
                        connection.request("GET", "http://private.invalid/path?token=SECRET_CANARY")
                        response = connection.getresponse()
                        self.assertEqual(response.status, 403)
                        response.read()
                    finally:
                        connection.close()
                self.assertNotIn("private.invalid", captured.getvalue())
                self.assertNotIn("SECRET_CANARY", captured.getvalue())
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()

    def test_ambiguous_request_rejected_before_upstream_contact(self):
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
                connection = socket.create_connection(("127.0.0.1", child.server_port), timeout=3)
                connection.sendall(
                    b"POST http://public.example/upload HTTP/1.1\r\n"
                    b"Host: public.example\r\n"
                    b"Content-Length: 0\r\n"
                    b"Content-Length: 9\r\n"
                    b"Transfer-Encoding: chunked\r\n\r\n"
                )
                response = bytearray()
                while b"\r\n" not in response:
                    response.extend(connection.recv(1))
                self.assertIn(b" 400 ", response)
                self.assertEqual(parent.requests, [])
            finally:
                if connection is not None:
                    connection.close()
                child.shutdown()
                child_thread.join(timeout=2)
                child.server_close()
                parent.shutdown()
                parent_thread.join(timeout=2)
                parent.server_close()

    def test_header_limit_rejects_before_parent_contact(self):
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
                connection = socket.create_connection(("127.0.0.1", child.server_port), timeout=3)
                connection.sendall(
                    b"GET http://public.example/ HTTP/1.1\r\n"
                    b"Host: public.example\r\n"
                    b"X-Bounded: " + b"x" * 40000 + b"\r\n\r\n"
                )
                response = bytearray()
                while b"\r\n" not in response:
                    response.extend(connection.recv(1))
                self.assertIn(b" 431 ", response)
                self.assertEqual(parent.requests, [])
            finally:
                if connection is not None:
                    connection.close()
                child.shutdown()
                child_thread.join(timeout=2)
                child.server_close()
                parent.shutdown()
                parent_thread.join(timeout=2)
                parent.server_close()

    def test_slot_exhaustion_recovers_for_next_request(self):
        original_slots = proxy.Proxy.slots
        proxy.Proxy.slots = threading.BoundedSemaphore(1)
        try:
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
                    self.assertTrue(proxy.Proxy.slots.acquire(blocking=False))
                    connection = http.client.HTTPConnection("127.0.0.1", child.server_port, timeout=3)
                    try:
                        connection.request("GET", "http://public.example/")
                        response = connection.getresponse()
                        self.assertEqual(response.status, 503)
                        response.read()
                    finally:
                        connection.close()
                        proxy.Proxy.slots.release()

                    connection = http.client.HTTPConnection("127.0.0.1", child.server_port, timeout=3)
                    try:
                        connection.request("GET", "http://public.example/")
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
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
        finally:
            proxy.Proxy.slots = original_slots

    def test_idle_timeout_releases_slot(self):
        original_slots = proxy.Proxy.slots
        proxy.Proxy.slots = threading.BoundedSemaphore(1)
        proxy.Proxy.IDLE_TIMEOUT = 0.05
        real_select = proxy.select.select
        observed_timeouts = []

        def bounded_select(readers, writers, errors, timeout):
            observed_timeouts.append(timeout)
            return real_select(readers, writers, errors, min(timeout, 0.05))

        def wait_for_slot():
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                if proxy.Proxy.slots.acquire(blocking=False):
                    proxy.Proxy.slots.release()
                    return True
                time.sleep(0.01)
            return False

        try:
            with tempfile.TemporaryDirectory() as directory:
                manifest = Path(directory) / ".mester-egress"
                manifest.write_text("permission=true\n")
                parent = _SilentUpstreamServer()
                parent_thread = threading.Thread(target=parent.serve_forever, daemon=True)
                parent_thread.start()
                child, child_thread = self._start_chained_proxy(
                    manifest, ("127.0.0.1", parent.server_address[1])
                )
                try:
                    with patch.object(proxy.select, "select", side_effect=bounded_select):
                        for index in range(2):
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
                            if index == 0:
                                self.assertTrue(wait_for_slot(), "idle handler did not return its connection slot")
                    self.assertTrue(parent.accepted.wait(timeout=1))
                    self.assertTrue(observed_timeouts)
                    self.assertTrue(all(timeout <= 0.05 for timeout in observed_timeouts))
                    self.assertTrue(wait_for_slot(), "idle handler did not return its connection slot")
                finally:
                    child.shutdown()
                    child_thread.join(timeout=2)
                    child.server_close()
                    parent.shutdown()
                    parent_thread.join(timeout=2)
                    parent.server_close()
        finally:
            proxy.Proxy.slots = original_slots
            proxy.Proxy.IDLE_TIMEOUT = 30

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
                    while not marker_file.exists() and time.monotonic() < deadline:
                        if process.poll() is not None:
                            self.fail("proxy exited before publishing its readiness identity")
                        time.sleep(0.02)
                    self.assertTrue(marker_file.exists())
                    self.assertEqual(port_file.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(marker_file.stat().st_mode & 0o777, 0o600)
                    identity = json.loads(marker_file.read_text(encoding="ascii"))
                    self.assertEqual(identity["pid"], process.pid)
                    self.assertEqual(identity["port"], recorded_port)
                    self.assertTrue(identity["start_ticks"])
                    self.assertTrue(identity["executable"].startswith("/"))
                    self.assertFalse(list(workspace.glob(".*.tmp")))
                    with socket.create_connection(("127.0.0.1", recorded_port), timeout=1):
                        pass
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=5)

    def test_proxy_does_not_replace_existing_readiness_files(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".mester-egress").write_text("permission=false\n")
            port_file = workspace / "proxy.port"
            marker_file = workspace / "proxy.reload-ready"
            port_file.write_text("previous-port\n")
            marker_file.write_text("previous-identity\n")
            port_file.chmod(0o600)
            marker_file.chmod(0o600)
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).parents[1] / "lib" / "vm" / "proxy.py"),
                    "--workspace", str(workspace),
                    "--port-file", str(port_file),
                    "--reload-marker", str(marker_file),
                ],
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(port_file.read_text(), "previous-port\n")
            self.assertEqual(marker_file.read_text(), "previous-identity\n")
            self.assertEqual(port_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(marker_file.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
