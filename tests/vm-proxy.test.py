import errno
import importlib.util
import ipaddress
import pathlib
import socket
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
        with patch.object(proxy.socket, "getaddrinfo", return_value=[public, private]), \
                self.assertRaises(PermissionError):
            proxy.connect_public("mixed.example", 443, frozenset())

    def test_connect_uses_the_validated_address_without_resolving_again(self):
        answer = (2, 1, 6, "", ("8.8.8.8", 443))
        with patch.object(proxy.socket, "getaddrinfo", return_value=[answer]) as lookup, \
                patch.object(proxy.socket, "socket") as create_socket:
            sock = create_socket.return_value
            sock.connect_ex.return_value = 0
            proxy.connect_public("public.example", 443, frozenset())
            lookup.assert_called_once_with("public.example", 443, type=proxy.socket.SOCK_STREAM)
            sock.connect_ex.assert_called_once_with(answer[4])
            sock.setblocking.assert_any_call(False)
            sock.setblocking.assert_any_call(True)
            sock.settimeout.assert_called_once_with(20)

    def _dial_fixture(self, records, outcomes, ready_order=(), fail_status=(),
                      fail_winner_setup=None, exceptional_order=()):
        class Clock:
            now = 0.0

            def monotonic(self):
                return self.now

        clock = Clock()
        made, ready = [], list(ready_order)

        class FakeSocket:
            def __init__(self, family, _kind, _proto):
                self.family, self.closed = family, False
                self.timeout, self.blocking, self.address = "unset", None, None
                made.append(self)

            def setblocking(self, value):
                if value and fail_winner_setup == "blocking":
                    raise OSError(errno.EIO, "winner setup secret")
                self.blocking = value

            def settimeout(self, value):
                if value == proxy._CONNECT_DEADLINE and fail_winner_setup == "timeout":
                    raise OSError(errno.EIO, "winner timeout secret")
                self.timeout = value

            def connect_ex(self, address):
                self.address = address
                result = outcomes.get(address[0], errno.EINPROGRESS)
                if isinstance(result, Exception):
                    raise result
                return result

            def getsockopt(self, *_args):
                if self.address[0] in fail_status:
                    raise OSError(errno.EIO, "synthetic failure")
                return 0

            def close(self):
                self.closed = True

        def fake_select(_read, write, _exceptional, timeout):
            selected = [sock for address in ready for sock in write
                        if sock.address[0] == address]
            exceptional = [sock for address in exceptional_order for sock in write
                           if sock.address[0] == address]
            if len(ready) + len(exceptional_order) > 1 and len(write) == 1:
                selected = []
                exceptional = []
            clock.peak_active = max(getattr(clock, "peak_active", 0), len(write))
            clock.now += min(timeout, 0.01) if selected or exceptional else timeout
            return [], selected, exceptional

        def getaddrinfo(_host, port, type):
            return [(family, socket.SOCK_STREAM, 6, "", (ip, port)) for family, ip in records]

        patches = [
            patch.object(proxy.socket, "getaddrinfo", side_effect=getaddrinfo),
            patch.object(proxy.socket, "socket", side_effect=FakeSocket),
            patch.object(proxy, "_monotonic", side_effect=clock.monotonic),
            patch.object(proxy.select, "select", side_effect=fake_select),
        ]
        return patches, made, clock

    def test_staggered_fallback_both_family_orders_and_same_family(self):
        cases = [
            [(socket.AF_INET6, "2606:4700:4700::1111"), (socket.AF_INET, "8.8.8.8")],
            [(socket.AF_INET, "8.8.8.8"), (socket.AF_INET6, "2606:4700:4700::1111")],
            [(socket.AF_INET, "8.8.8.8"), (socket.AF_INET, "1.1.1.1")],
        ]
        for records in cases:
            patches, made, _ = self._dial_fixture(records, {}, ready_order=[records[1][1]])
            with patches[0], patches[1], patches[2], patches[3]:
                winner = proxy.connect_public("fallback.example", 443, frozenset())
            self.assertIs(winner, made[1])
            self.assertTrue(winner.blocking)
            self.assertEqual(winner.timeout, 20)
            self.assertTrue(made[0].closed)

    def test_immediate_refusal_falls_through(self):
        records = [(socket.AF_INET, "8.8.8.8"), (socket.AF_INET, "1.1.1.1")]
        patches, made, _ = self._dial_fixture(records, {"8.8.8.8": errno.ECONNREFUSED},
                                               ready_order=["1.1.1.1"])
        with patches[0], patches[1], patches[2], patches[3]:
            winner = proxy.connect_public("refusal.example", 443, frozenset())
        self.assertIs(winner, made[1])
        self.assertTrue(made[0].closed)

    def test_total_candidate_attempts_are_capped(self):
        records = [(socket.AF_INET, f"8.8.{i // 250}.{(i % 250) + 1}")
                   for i in range(20)]
        refused = {ip: errno.ECONNREFUSED for _, ip in records}
        patches, made, _ = self._dial_fixture(records, refused)
        with patches[0], patches[1], patches[2], patches[3], self.assertRaises(OSError):
            proxy.connect_public("candidate-cap.example", 443, frozenset())
        self.assertEqual(len(made), proxy._MAX_CANDIDATES)
        self.assertTrue(all(sock.closed for sock in made))

    def test_deadline_and_active_bound_close_pending_sockets(self):
        records = [(socket.AF_INET, f"8.8.8.{i}") for i in range(1, 20)]
        patches, made, clock = self._dial_fixture(records, {})
        with patches[0], patches[1], patches[2], patches[3], self.assertRaises(OSError) as caught:
            proxy.connect_public("blackhole.example", 443, frozenset())
        self.assertEqual(caught.exception.errno, errno.ETIMEDOUT)
        self.assertEqual(clock.now, 20.0)
        self.assertGreater(len(made), proxy._MAX_ACTIVE_DIALS)
        self.assertLessEqual(len(made), proxy._MAX_CANDIDATES)
        self.assertLessEqual(clock.peak_active, proxy._MAX_ACTIVE_DIALS)
        self.assertTrue(all(sock.closed for sock in made))

    def test_candidate_timeout_progresses_to_third_and_later_addresses(self):
        records = [(socket.AF_INET, f"8.8.8.{i}") for i in range(1, 5)]
        for target in ("8.8.8.3", "8.8.8.4"):
            patches, made, clock = self._dial_fixture(records, {}, ready_order=[target])
            with patches[0], patches[1], patches[2], patches[3]:
                winner = proxy.connect_public("later-candidate.example", 443, frozenset())
            self.assertEqual(winner.address[0], target)
            self.assertEqual(len(made), int(target.rsplit(".", 1)[1]))
            self.assertGreaterEqual(clock.now, proxy._CANDIDATE_TIMEOUT)
            self.assertLess(clock.now, proxy._CONNECT_DEADLINE)
            self.assertTrue(all(sock.closed for sock in made[:-1]))
            self.assertFalse(winner.closed)
            self.assertLessEqual(clock.peak_active, proxy._MAX_ACTIVE_DIALS)

    def test_simultaneous_writable_successes_close_loser(self):
        records = [(socket.AF_INET, "8.8.8.8"), (socket.AF_INET, "1.1.1.1")]
        patches, made, _ = self._dial_fixture(records, {}, ready_order=["8.8.8.8", "1.1.1.1"])
        with patches[0], patches[1], patches[2], patches[3]:
            winner = proxy.connect_public("race.example", 443, frozenset())
        loser = made[1] if winner is made[0] else made[0]
        self.assertIn(winner, made)
        self.assertTrue(loser.closed)

    def test_getsockopt_failure_closes_sockets(self):
        records = [(socket.AF_INET, "8.8.8.8"), (socket.AF_INET, "1.1.1.1")]
        patches, made, _ = self._dial_fixture(records, {}, ready_order=["8.8.8.8"],
                                               fail_status=["8.8.8.8"])
        with patches[0], patches[1], patches[2], patches[3], self.assertRaises(OSError):
            proxy.connect_public("status.example", 443, frozenset())
        self.assertTrue(all(sock.closed for sock in made))

    def test_ready_candidate_wins_even_if_another_ready_socket_status_fails(self):
        records = [(socket.AF_INET, "8.8.8.8"), (socket.AF_INET, "1.1.1.1")]
        winner_address, bad_address = "8.8.8.8", "1.1.1.1"
        cases = [
            {"ready_order": [winner_address, bad_address]},
            {"ready_order": [bad_address, winner_address]},
            {"ready_order": [winner_address], "exceptional_order": [bad_address]},
        ]
        for case in cases:
            patches, made, _ = self._dial_fixture(
                records, {}, fail_status=[bad_address], **case)
            with patches[0], patches[1], patches[2], patches[3]:
                winner = proxy.connect_public("status-loser.example", 443, frozenset())
            self.assertEqual(winner.address[0], winner_address)
            self.assertFalse(winner.closed)
            bad_socket = next(sock for sock in made if sock.address[0] == bad_address)
            self.assertTrue(bad_socket.closed)
            self.assertTrue(all(sock.closed for sock in made if sock is not winner))

    def test_connect_ex_failure_closes_candidate_before_fallback(self):
        records = [(socket.AF_INET, "8.8.8.8"), (socket.AF_INET, "1.1.1.1")]
        patches, made, _ = self._dial_fixture(
            records, {"8.8.8.8": OSError(errno.EIO, "private synthetic failure")},
            ready_order=["1.1.1.1"])
        with patches[0], patches[1], patches[2], patches[3]:
            winner = proxy.connect_public("connect-error.example", 443, frozenset())
        self.assertIs(winner, made[1])
        self.assertTrue(made[0].closed)

    def test_select_failure_closes_pending_socket(self):
        records = [(socket.AF_INET, "8.8.8.8")]
        patches, made, _ = self._dial_fixture(records, {})
        patches[3] = patch.object(proxy.select, "select", side_effect=OSError(errno.EIO, "private"))
        with patches[0], patches[1], patches[2], patches[3], self.assertRaises(OSError):
            proxy.connect_public("select-error.example", 443, frozenset())
        self.assertTrue(made[0].closed)

    def test_winner_mode_or_timeout_failure_closes_socket_and_logs_fixed_reason(self):
        for failure in ("blocking", "timeout"):
            patches, made, _ = self._dial_fixture(
                [(socket.AF_INET, "8.8.8.8")], {"8.8.8.8": 0},
                fail_winner_setup=failure)
            with patches[0], patches[1], patches[2], patches[3], \
                    self.assertLogs("vm-proxy", level="WARNING") as captured, self.assertRaises(OSError):
                proxy.connect_public("winner-setup.example", 443, frozenset())
            self.assertTrue(made[0].closed)
            text = "\n".join(captured.output)
            self.assertIn("reason_code=winner-setup-error", text)
            self.assertNotIn("winner setup secret", text)
            self.assertNotIn("winner timeout secret", text)

    def test_dns_failure_and_slow_dns_logs_are_fixed_and_redacted(self):
        secret = "resolver-secret-198.51.100.77"
        with patch.object(proxy.socket, "getaddrinfo", side_effect=OSError(errno.EIO, secret)), \
                self.assertLogs("vm-proxy", level="WARNING") as captured, \
                self.assertRaises(OSError):
            proxy.connect_public("dns.example", 443, frozenset())
        text = "\n".join(captured.output)
        self.assertIn("reason=dns-failed", text)
        self.assertIn("reason_code=resolve-error", text)
        self.assertNotIn(secret, text)

        records = [(socket.AF_INET, "8.8.8.8")]
        patches, _, clock = self._dial_fixture(records, {"8.8.8.8": 0})
        old_resolver = patches[0]

        def slow_resolution(_host, port, type):
            clock.now += 1.25
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

        patches[0] = patch.object(proxy.socket, "getaddrinfo", side_effect=slow_resolution)
        with patches[0], patches[1], patches[2], patches[3], \
                self.assertLogs("vm-proxy", level="WARNING") as captured:
            proxy.connect_public("slow-dns.example", 443, frozenset())
        self.assertIn("reason=dns-slow", "\n".join(captured.output))
        del old_resolver

    def test_slow_or_failed_host_network_inspection_is_separately_attributed(self):
        class Clock:
            now = 0.0

            def monotonic(self):
                return self.now

        clock = Clock()
        def resolve(_host, port, type):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

        def slow_network_read():
            clock.now += 1.25
            return frozenset()

        with patch.object(proxy.socket, "getaddrinfo", side_effect=resolve), \
                patch.object(proxy, "host_networks", side_effect=slow_network_read), \
                patch.object(proxy, "_monotonic", side_effect=clock.monotonic), \
                patch.object(proxy.socket, "socket") as create_socket, \
                self.assertLogs("vm-proxy", level="WARNING") as captured:
            create_socket.return_value.connect_ex.return_value = 0
            proxy.connect_public("slow-policy.example", 443)
        text = "\n".join(captured.output)
        self.assertIn("reason=host-networks-slow", text)
        self.assertIn("phase=policy", text)
        self.assertIn("elapsed_ms=1250", text)

        with patch.object(proxy.socket, "getaddrinfo", side_effect=resolve), \
                patch.object(proxy, "host_networks", side_effect=PermissionError("private detail")), \
                self.assertLogs("vm-proxy", level="WARNING") as captured, \
                self.assertRaises(PermissionError):
            proxy.connect_public("failed-policy.example", 443)
        text = "\n".join(captured.output)
        self.assertIn("reason=policy-failed", text)
        self.assertIn("phase=host-networks", text)
        self.assertNotIn("private detail", text)

    def test_candidate_failure_log_is_correlated_bounded_and_redacted(self):
        address = "8.8.8.8"
        patches, _, _ = self._dial_fixture(
            [(socket.AF_INET, address)], {address: errno.ECONNREFUSED})
        _REQUEST_ID = "diag-test-42"
        proxy._REQUEST_CONTEXT.request_id = _REQUEST_ID
        try:
            with patches[0], patches[1], patches[2], patches[3], \
                    self.assertLogs("vm-proxy", level="WARNING") as captured, self.assertRaises(OSError):
                proxy.connect_public("connect.example", 443, frozenset())
        finally:
            proxy._REQUEST_CONTEXT.request_id = "-"
        text = "\n".join(captured.output)
        for expected in (f"request={_REQUEST_ID}", "reason=connect-candidate-failed",
                         "family=ipv4", "ordinal=1", "reason_code=refused"):
            self.assertIn(expected, text)
        self.assertNotIn(address, text)
        self.assertNotIn("connect failed", text)

    def _forward_fixture(self, path, *, idle=False, io_error=None, connect_delay=0.0,
                         empty_upstream=False):
        clock = type("Clock", (), {"now": 0.0})()

        class Client:
            def __init__(self):
                self.sent = []

            def settimeout(self, _value):
                pass

            def sendall(self, data):
                self.sent.append(data)

            def recv(self, _size):
                return b""

        class Upstream:
            def __init__(self):
                self.reads = 0

            def settimeout(self, _value):
                pass

            def sendall(self, _data):
                pass

            def recv(self, _size):
                self.reads += 1
                if empty_upstream and self.reads == 1:
                    return b""
                return b"body-secret" if self.reads == 1 else b""

            def close(self):
                pass

        class Handler:
            command = "GET"

            def __init__(self):
                self.path = path
                self.headers = {"Authorization": "Bearer token-secret",
                                "X-Private": "header-secret"}
                self.connection = Client()
                self.server = type("Server", (), {
                    "upstream_proxy": None,
                    "policy": type("Policy", (), {"allows": lambda _self, _host: True})(),
                })()
                self.errors = []

            def send_error(self, status):
                self.errors.append(status)

            def send_response(self, *_args):
                pass

            def end_headers(self):
                pass

        handler, upstream = Handler(), Upstream()

        def fake_select(read, _write, _exceptional, _timeout):
            if io_error:
                raise OSError(errno.EIO, io_error)
            if idle:
                clock.now += 60
                return [], [], []
            clock.now += 1.25
            if upstream.reads == 0:
                return [upstream], [], []
            return [handler.connection], [], []

        def connect(_host, _port):
            clock.now += connect_delay
            return upstream

        return handler, upstream, clock, fake_select, connect

    def test_forward_denial_idle_first_byte_and_io_logs_are_redacted(self):
        secrets = ("/private/path", "query-secret", "header-secret", "token-secret",
                   "body-secret", "8.8.8.8", "198.51.100.9", "exception-secret")
        denied, _, _, _, _ = self._forward_fixture(
            "http://8.8.8.8/private/path?token=query-secret")
        with patch.object(proxy, "connect_public", side_effect=PermissionError("private")), \
                self.assertLogs("vm-proxy", level="WARNING") as captured:
            proxy.Proxy.forward(denied, False)
        denial_log = "\n".join(captured.output)
        self.assertIn("reason=request-denied", denial_log)
        self.assertIn("phase=destination", denial_log)
        self.assertIn("reason=forward-summary", denial_log)
        self.assertRegex(denial_log, r"request=\d+")
        for secret in secrets:
            self.assertNotIn(secret, denial_log)

        idle, idle_upstream, idle_clock, idle_select, idle_connect = self._forward_fixture(
            "http://public.example/safe?query-secret", idle=True, connect_delay=2.0)
        with patch.object(proxy, "connect_public", side_effect=idle_connect), \
                patch.object(proxy.select, "select", side_effect=idle_select), \
                patch.object(proxy, "_wall_monotonic", side_effect=lambda: idle_clock.now), \
                self.assertLogs("vm-proxy", level="WARNING") as captured:
            proxy.Proxy.forward(idle, False)
        idle_log = "\n".join(captured.output)
        self.assertIn("reason=transfer-idle-timeout", idle_log)
        self.assertIn("side=upstream-and-client", idle_log)
        self.assertRegex(idle_log, r"total_ms=\d+")
        self.assertNotIn("query-secret", idle_log)

        fast, _, fast_clock, fast_select, fast_connect = self._forward_fixture(
            "http://public.example/secret?query-secret", connect_delay=3.0)
        with patch.object(proxy, "connect_public", side_effect=fast_connect), \
                patch.object(proxy.select, "select", side_effect=fast_select), \
                patch.object(proxy, "_wall_monotonic", side_effect=lambda: fast_clock.now), \
                self.assertLogs("vm-proxy", level="WARNING") as captured:
            proxy.Proxy.forward(fast, False)
        first_byte_log = "\n".join(captured.output)
        self.assertIn("reason=upstream-first-byte-slow", first_byte_log)
        self.assertIn("upstream_ms=1250", first_byte_log)
        self.assertIn("request_ms=4250", first_byte_log)
        self.assertNotIn("query-secret", first_byte_log)
        self.assertIsNotNone(idle_upstream)

        failed, _, failed_clock, failing_select, fail_connect = self._forward_fixture(
            "http://public.example/safe", io_error="exception-secret 198.51.100.9")
        with patch.object(proxy, "connect_public", side_effect=fail_connect), \
                patch.object(proxy.select, "select", side_effect=failing_select), \
                patch.object(proxy, "_wall_monotonic", side_effect=lambda: failed_clock.now), \
                self.assertLogs("vm-proxy", level="WARNING") as captured:
            proxy.Proxy.forward(failed, False)
        error_log = "\n".join(captured.output)
        self.assertIn("reason=transfer-io-error", error_log)
        self.assertIn("phase=transfer", error_log)
        self.assertIn("result=io-error", error_log)
        for secret in secrets:
            self.assertNotIn(secret, error_log)

        empty, _, empty_clock, empty_select, empty_connect = self._forward_fixture(
            "http://public.example/safe", empty_upstream=True)
        with patch.object(proxy, "connect_public", side_effect=empty_connect), \
                patch.object(proxy.select, "select", side_effect=empty_select), \
                patch.object(proxy, "_wall_monotonic", side_effect=lambda: empty_clock.now), \
                self.assertLogs("vm-proxy", level="WARNING") as captured:
            proxy.Proxy.forward(empty, False)
        empty_log = "\n".join(captured.output)
        self.assertIn("reason=upstream-eof", empty_log)
        self.assertIn("result=upstream-eof-before-response", empty_log)
        self.assertEqual(empty.errors, [502])

    def test_denied_dns_answer_after_candidate_cap_still_blocks_all_sockets(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"8.8.8.{i}", 443))
                   for i in range(1, 18)]
        answers.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)))
        with patch.object(proxy.socket, "getaddrinfo", return_value=answers), \
                patch.object(proxy.socket, "socket") as create_socket, \
                self.assertRaises(PermissionError):
            proxy.connect_public("mixed.example", 443, frozenset())
        create_socket.assert_not_called()


if __name__ == "__main__":
    unittest.main()
