import importlib.util
import socket
import ipaddress
import pathlib
import threading
import time
import unittest
from unittest.mock import patch


MODULE_PATH = pathlib.Path(__file__).parent.parent / "lib/vm/proxy.py"
SPEC = importlib.util.spec_from_file_location("mester_vm_proxy", MODULE_PATH)
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)


class DestinationPolicyTests(unittest.TestCase):
    def test_only_http_and_connect_ports_are_allowed_by_request_handler_policy(self):
        self.assertTrue(proxy.allowed("example.org", *proxy.policy()))
        self.assertFalse(proxy.allowed("example.org", False, set()))

    def test_http_and_connect_port_rules_are_enforced_by_the_handler(self):
        server = proxy.BoundedThreadingHTTPServer(("127.0.0.1", 0), proxy.Proxy)
        server.policy = proxy.PolicyStore(None)
        server.upstream_proxy = None
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(raw):
            with socket.create_connection(server.server_address, timeout=2) as client:
                client.sendall(raw)
                response = bytearray()
                while b"\r\n\r\n" not in response:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    response.extend(chunk)
                return bytes(response).split(b"\r\n", 1)[0]

        try:
            with patch.object(proxy, "connect_public", side_effect=OSError("mocked no-network")) as connect:
                self.assertIn(b"403", request(
                    b"GET http://example.org:443/path HTTP/1.1\r\nHost: example.org\r\n\r\n"
                ))
                self.assertIn(b"403", request(
                    b"CONNECT example.org:80 HTTP/1.1\r\nHost: example.org:80\r\n\r\n"
                ))
                connect.assert_not_called()

                self.assertIn(b"502", request(
                    b"GET http://example.org/path HTTP/1.1\r\nHost: example.org\r\n\r\n"
                ))
                connect.assert_called_with("example.org", 80)
                connect.reset_mock()

                self.assertIn(b"502", request(
                    b"CONNECT example.org:443 HTTP/1.1\r\nHost: example.org:443\r\n\r\n"
                ))
                connect.assert_called_with("example.org", 443)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_redirect_followup_to_private_target_is_denied_before_socket_creation(self):
        server = proxy.BoundedThreadingHTTPServer(("127.0.0.1", 0), proxy.Proxy)
        server.policy = proxy.PolicyStore(None)
        server.upstream_proxy = None
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        real_connect_public = proxy.connect_public
        fixture_peers = []
        fixture_connections = []

        def mocked_connect_public(host, port, blocked_networks=None):
            if (host, port) == ("public.example", 80):
                fixture_connections.append((host, port))
                upstream, peer = socket.socketpair()
                fixture_peers.append(peer)
                peer.sendall(
                    b"HTTP/1.1 302 Found\r\n"
                    b"Location: http://127.0.0.1/private\r\n"
                    b"Content-Length: 0\r\n"
                    b"Connection: close\r\n\r\n"
                )
                peer.shutdown(socket.SHUT_WR)
                return upstream

            # connect_public is also the address-policy gate. A private
            # literal must be rejected there before it creates a socket.
            return real_connect_public(host, port, frozenset())

        def exchange(client, raw):
            client.sendall(raw)
            response = bytearray()
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    return bytes(response)
                response.extend(chunk)

        clients = []
        try:
            with patch.object(proxy, "connect_public", side_effect=mocked_connect_public) as connect:
                first = socket.create_connection(server.server_address, timeout=2)
                first.settimeout(2)
                clients.append(first)
                redirect = exchange(
                    first,
                    b"GET http://public.example/start HTTP/1.1\r\n"
                    b"Host: public.example\r\nConnection: close\r\n\r\n",
                )
                self.assertIn(b"HTTP/1.1 302 Found", redirect)
                self.assertIn(b"Location: http://127.0.0.1/private", redirect)
                self.assertEqual(fixture_connections, [("public.example", 80)])

                # Model the client's explicit redirect follow-up. Pre-connect
                # the local test client before guarding socket construction so
                # any later socket creation can only come from the proxy.
                second = socket.create_connection(server.server_address, timeout=2)
                second.settimeout(2)
                clients.append(second)
                socket_constructor = proxy.socket.socket
                destination_socket_creations = []

                def reject_new_socket(*args, **kwargs):
                    # socketserver wraps accepted local clients using fileno;
                    # only reject new sockets that could initiate a dial.
                    if kwargs.get("fileno") is None:
                        destination_socket_creations.append((args, kwargs))
                        raise AssertionError("private redirect attempted socket creation")
                    return socket_constructor(*args, **kwargs)

                with patch.object(
                    proxy.socket,
                    "socket",
                    side_effect=reject_new_socket,
                ):
                    denied = exchange(
                        second,
                        b"GET http://127.0.0.1/private HTTP/1.1\r\n"
                        b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n",
                    )
                self.assertIn(b"403 Forbidden", denied)
                self.assertEqual(
                    [call.args[:2] for call in connect.call_args_list],
                    [("public.example", 80), ("127.0.0.1", 80)],
                )
                self.assertEqual(fixture_connections, [("public.example", 80)])
                self.assertEqual(destination_socket_creations, [])
        finally:
            for client in clients:
                client.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            for peer in fixture_peers:
                peer.close()

    def test_slow_incomplete_headers_are_admitted_before_thread_creation(self):
        class TrackingServer(proxy.BoundedThreadingHTTPServer):
            def __init__(self, *args, **kwargs):
                self.active_handlers = 0
                self.peak_handlers = 0
                self.counter_lock = threading.Lock()
                super().__init__(*args, **kwargs)

            def process_request_thread(self, request, client_address):
                with self.counter_lock:
                    self.active_handlers += 1
                    self.peak_handlers = max(self.peak_handlers, self.active_handlers)
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    with self.counter_lock:
                        self.active_handlers -= 1

        with patch.object(proxy.Proxy, "timeout", 2):
            server = TrackingServer(("127.0.0.1", 0), proxy.Proxy, max_connections=3)
        server.policy = proxy.PolicyStore(None)
        server.upstream_proxy = None
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        clients = []
        try:
            for _ in range(3):
                client = socket.create_connection(server.server_address, timeout=2)
                client.settimeout(2)
                client.sendall(b"GET / HTTP/1.1\r\nHost: incomplete.example\r\n")
                clients.append(client)
            deadline = time.monotonic() + 2
            while server.active_handlers < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(server.active_handlers, 3)
            self.assertEqual(server.peak_handlers, 3)

            overflow = socket.create_connection(server.server_address, timeout=2)
            overflow.settimeout(2)
            try:
                response = overflow.recv(1024)
                self.assertIn(b"503 Service Unavailable", response)
                self.assertEqual(server.active_handlers, 3)
                self.assertEqual(server.peak_handlers, 3)
            finally:
                overflow.close()
        finally:
            for client in clients:
                client.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_non_public_and_embedded_private_addresses_are_denied(self):
        denied = [
            "127.0.0.1", "10.1.2.3", "172.16.1.1", "192.168.1.10",
            "169.254.169.254", "224.0.0.1", "::1", "fc00::1",
            "::ffff:127.0.0.1", "64:ff9b::a00:1", "2002:0808:0808::1",
        ]
        for raw in denied:
            with self.subTest(address=raw):
                self.assertFalse(proxy._is_allowed_public_destination(
                    ipaddress.ip_address(raw), frozenset()
                ))

    def test_host_local_and_direct_networks_are_denied_even_when_public(self):
        address = ipaddress.ip_address("8.8.8.8")
        blocked = frozenset({ipaddress.ip_network("8.8.8.0/24")})
        self.assertFalse(proxy._is_allowed_public_destination(address, blocked))

    def test_all_dns_answers_must_be_public(self):
        public = (2, 1, 6, "", ("8.8.8.8", 443))
        private = (2, 1, 6, "", ("10.0.0.4", 443))
        with patch.object(proxy.socket, "getaddrinfo", return_value=[public, private]):
            with self.assertRaises(PermissionError):
                proxy.connect_public("mixed.example", 443, frozenset())

    def test_connect_uses_the_validated_address_without_resolving_again(self):
        answer = (2, 1, 6, "", ("8.8.8.8", 443))
        with patch.object(proxy.socket, "getaddrinfo", return_value=[answer]) as lookup:
            with patch.object(proxy.socket, "socket") as create_socket:
                sock = create_socket.return_value
                proxy.connect_public("public.example", 443, frozenset())
                lookup.assert_called_once_with("public.example", 443, type=proxy.socket.SOCK_STREAM)
                sock.connect.assert_called_once_with(answer[4])


if __name__ == "__main__":
    unittest.main()
