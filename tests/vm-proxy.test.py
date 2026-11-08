import importlib.util
import socket
import ipaddress
import pathlib
import threading
import unittest
from http.server import ThreadingHTTPServer
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
        server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Proxy)
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
