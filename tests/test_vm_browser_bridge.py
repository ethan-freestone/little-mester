"""Fixture-only tests for the narrow per-session OpenAI browser capability."""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


browser_open = load_module("vm_browser_open", ROOT / "lib/vm/browser-open.py")
bridge_server = load_module("vm_browser_bridge_server", ROOT / "lib/vm/browser-bridge-server.py")


def auth_url(port: int = 40391, state: str = "synthetic-state") -> str:
    redirect = quote(f"http://127.0.0.1:{port}/auth/callback", safe="")
    return f"https://auth.openai.com/api/accounts/authorize?state={state}&redirect_uri={redirect}"


def start_server(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def initialize(connection, token):
    response, session = browser_open._request(
        connection,
        token,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "fixture", "version": "1"},
        }},
    )
    version = response["result"]["protocolVersion"]
    browser_open._request(
        connection, token, {"jsonrpc": "2.0", "method": "notifications/initialized"},
        notification=True, session_id=session, protocol_version=version,
    )
    return session, version


class BrowserBridgeClientTests(unittest.TestCase):
    def test_only_approved_authorization_urls_are_sent(self):
        self.assertTrue(browser_open.is_authorization_url(auth_url()))
        self.assertTrue(browser_open.is_authorization_url("https://chatgpt.com/auth/login?state=x"))
        for url in (
            "http://auth.openai.com/oauth/authorize?state=x",
            "https://evil.example/oauth/authorize?state=x",
            "https://auth.openai.com.evil.example/oauth/authorize?state=x",
            "https://user@auth.openai.com/oauth/authorize?state=x",
            "https://auth.openai.com:444/oauth/authorize?state=x",
            "https://auth.openai.com/callback?code=synthetic-code",
            "https://auth.openai.com/oauth/authorize?code_verifier=synthetic-verifier",
            "https://auth.openai.com/oauth/callback?state=x",
            "https://auth.openai.com/oauth/authorize#fragment",
        ):
            with self.subTest(url=url.split(":", 1)[0]):
                self.assertFalse(browser_open.is_authorization_url(url))

    def test_host_mcp_server_accepts_one_tool_and_supports_repeated_handoffs(self):
        opened, forwarded, events = [], [], []
        url1, url2 = auth_url(40391, "state-one"), auth_url(40392, "state-two")
        server = bridge_server.BrowserBridgeHTTPServer(
            ("127.0.0.1", 0), "d" * 48,
            opener=lambda url: (opened.append(url), events.append(("open", url))),
            callback_forwarder=lambda port: (forwarded.append(port), events.append(("forward", port))),
            callback_canceller=lambda port: events.append(("cancel", port)),
        )
        thread = start_server(server)
        captured = []
        try:
            for url in (url1, url1, url2):
                browser_open.call_host_bridge(url, "d" * 48, port=server.server_port)
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            version_session, version = initialize(connection, "d" * 48)
            tools, _ = browser_open._request(
                connection, "d" * 48,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                session_id=version_session, protocol_version=version,
            )
            self.assertEqual([item["name"] for item in tools["result"]["tools"]], ["open_auth_url"])
            result, _ = browser_open._request(
                connection, "d" * 48,
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                    "name": "open_auth_url", "arguments": {"url": url1, "extra": "no"},
                }},
                session_id=version_session, protocol_version=version,
            )
            self.assertTrue(result["result"]["isError"])
            browser_open._close_session(connection, "d" * 48, version_session, version)
            connection.close()
            with self.assertRaises(ValueError):
                browser_open.call_host_bridge(url1, "wrong-token", port=server.server_port)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()
        self.assertEqual(opened, [url1, url1, url2])
        self.assertEqual(forwarded, [40391, 40391, 40392])
        self.assertEqual(events, [
            ("forward", 40391), ("open", url1),
            ("forward", 40391), ("open", url1),
            ("cancel", 40391), ("forward", 40392), ("open", url2),
            ("cancel", 40392),
        ])
        self.assertEqual(server.sessions, {})
        self.assertNotIn("state-one", json.dumps(captured))

    def test_client_uses_per_session_port_from_environment(self):
        opened = []
        server = bridge_server.BrowserBridgeHTTPServer(
            ("127.0.0.1", 0), "p" * 48, opener=opened.append,
            callback_forwarder=lambda _port: None,
        )
        thread = start_server(server)
        try:
            with patch.dict(os.environ, {"VM_BROWSER_BRIDGE_PORT": str(server.server_port)}):
                browser_open.call_host_bridge(auth_url(), "p" * 48)
        finally:
            server.shutdown(); thread.join(timeout=2); server.server_close()
        self.assertEqual(opened, [auth_url()])

    def test_client_refuses_to_guess_a_shared_bridge_port(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "port is not configured"):
            browser_open.call_host_bridge(auth_url(), "r" * 48)

    def test_two_sessions_can_bind_independent_dynamic_host_ports(self):
        first = bridge_server.BrowserBridgeHTTPServer(("127.0.0.1", 0), "q" * 48)
        second = bridge_server.BrowserBridgeHTTPServer(("127.0.0.1", 0), "q" * 48)
        try:
            self.assertNotEqual(first.server_port, second.server_port)
            self.assertEqual(first.server_address[0], "127.0.0.1")
            self.assertEqual(second.server_address[0], "127.0.0.1")
        finally:
            first.server_close(); second.server_close()

    def test_callback_forward_uses_exact_loopback_and_ignores_ssh_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            key, known_hosts = Path(temporary) / "control-key", Path(temporary) / "known-hosts"
            key.write_text("fixture", encoding="ascii"); known_hosts.write_text("fixture", encoding="ascii")
            key.chmod(0o600); known_hosts.chmod(0o600)
            with patch.dict(os.environ, {
                "VM_BROWSER_BRIDGE_SSH_PORT": "22222",
                "VM_BROWSER_BRIDGE_SSH_KEY": str(key),
                "VM_BROWSER_BRIDGE_KNOWN_HOSTS": str(known_hosts),
            }, clear=True):
                command = bridge_server.callback_forward_command(40391)
        self.assertEqual(command[command.index("-F") + 1], "/dev/null")
        self.assertIn("ForwardAgent=no", command)
        self.assertIn("ForwardX11=no", command)
        self.assertIn("ExitOnForwardFailure=yes", command)
        self.assertEqual(command[command.index("-L") + 1], "127.0.0.1:40391:127.0.0.1:40391")
        self.assertNotIn("0.0.0.0", " ".join(command))
        self.assertNotIn("-R", command)
        with (
            patch.dict(os.environ, {"VM_BROWSER_BRIDGE_SSH_PORT": "22222",
                                    "VM_BROWSER_BRIDGE_SSH_KEY": "/missing",
                                    "VM_BROWSER_BRIDGE_KNOWN_HOSTS": "/missing"}, clear=True),
            self.assertRaises(RuntimeError),
        ):
            bridge_server.callback_forward_command(22)

    def test_callback_readiness_probe_requires_the_native_server_not_just_a_local_bind(self):
        class Handler(bridge_server.BaseHTTPRequestHandler):
            status = 404
            def do_GET(self):
                self.send_response(self.status)
                self.end_headers()
            def log_message(self, *_args):
                pass

        for status, expected in ((404, True), (200, False)):
            with self.subTest(status=status):
                Handler.status = status
                server = bridge_server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                thread = start_server(server)
                try:
                    self.assertEqual(
                        bridge_server._callback_listener_is_ready(server.server_port), expected
                    )
                finally:
                    server.shutdown(); thread.join(timeout=2); server.server_close()

    def test_callback_uri_validation_fails_closed_for_unexpected_redirects(self):
        for redirect_uri in (
            "http://localhost:40391/auth/callback", "http://127.0.0.1:80/auth/callback",
            "http://127.0.0.1:40391/other", "http://127.0.0.1:40391/auth/callback?extra=1",
            "https://127.0.0.1:40391/auth/callback", "http://127.0.0.1:65536/auth/callback",
            "http://127.0.0.1:40391/auth/callback#fragment", "http://user@127.0.0.1:40391/auth/callback",
            "http://192.168.1.1:40391/auth/callback", "http://[::1]:40391/auth/callback",
        ):
            with self.subTest(redirect=redirect_uri), self.assertRaises(ValueError):
                bridge_server.openai_callback_port(
                    "https://auth.openai.com/api/accounts/authorize?redirect_uri="
                    + quote(redirect_uri, safe="")
                )
        with self.assertRaises(ValueError):
            bridge_server.openai_callback_port("https://auth.openai.com/api/accounts/authorize")

    def test_host_rejects_changed_authorize_endpoint_or_duplicate_callback(self):
        opened, forwarded = Mock(), Mock()
        server = bridge_server.BrowserBridgeHTTPServer(
            ("127.0.0.1", 0), "d" * 48, opener=opened, callback_forwarder=forwarded,
        )
        thread = start_server(server)
        redirect = "redirect_uri=" + quote("http://127.0.0.1:40391/auth/callback", safe="")
        try:
            for url in (
                "https://auth.openai.com/oauth/authorize?" + redirect,
                "https://chatgpt.com/auth/login?" + redirect,
                "https://auth.openai.com/api/accounts/authorize?" + redirect + "&" + redirect,
                "https://auth.openai.com/api/accounts/authorize?state=missing-redirect",
            ):
                with self.subTest(endpoint=url.split("?", 1)[0]), self.assertRaises(ValueError):
                    browser_open.call_host_bridge(url, "d" * 48, port=server.server_port)
        finally:
            server.shutdown(); thread.join(timeout=2); server.server_close()
        opened.assert_not_called(); forwarded.assert_not_called()

    def test_callback_replacement_stops_if_previous_forward_cannot_be_removed(self):
        forward, opener = Mock(), Mock()
        cancel = Mock(side_effect=RuntimeError("cancel failed"))
        server = bridge_server.BrowserBridgeHTTPServer(
            ("127.0.0.1", 0), "d" * 48, callback_forwarder=forward,
            callback_canceller=cancel, opener=opener,
        )
        try:
            server.open_authorization_url(auth_url(40391))
            with self.assertRaises(RuntimeError):
                server.open_authorization_url(auth_url(40392))
            forward.assert_called_once_with(40391)
            cancel.assert_called_once_with(40391)
            opener.assert_called_once()
        finally:
            server.server_close()

    def test_concurrent_handoffs_do_not_cancel_an_active_browser_flow(self):
        events = []
        first_started, release_first, second_waiting = threading.Event(), threading.Event(), threading.Event()

        class ObservedLock:
            def __init__(self): self.lock = threading.Lock()
            def __enter__(self):
                if threading.current_thread().name == "second-handoff": second_waiting.set()
                self.lock.acquire(); return self
            def __exit__(self, *_args): self.lock.release()

        def opener(url):
            port = bridge_server.openai_callback_port(url)
            events.append(("open-start", port))
            if port == 40391:
                first_started.set()
                if not release_first.wait(3): raise RuntimeError("fixture timeout")
            events.append(("open-end", port))

        server = bridge_server.BrowserBridgeHTTPServer(
            ("127.0.0.1", 0), "d" * 48, opener=opener,
            callback_forwarder=lambda port: events.append(("forward", port)),
            callback_canceller=lambda port: events.append(("cancel", port)),
        )
        server.callback_lock = ObservedLock()
        first = threading.Thread(target=server.open_authorization_url, args=(auth_url(40391),))
        second = threading.Thread(target=server.open_authorization_url, args=(auth_url(40392),), name="second-handoff")
        try:
            first.start(); self.assertTrue(first_started.wait(2))
            second.start(); self.assertTrue(second_waiting.wait(2))
            self.assertEqual(events, [("forward", 40391), ("open-start", 40391)])
            release_first.set(); first.join(3); second.join(3)
            self.assertFalse(first.is_alive()); self.assertFalse(second.is_alive())
            self.assertEqual(events, [
                ("forward", 40391), ("open-start", 40391), ("open-end", 40391),
                ("cancel", 40391), ("forward", 40392), ("open-start", 40392), ("open-end", 40392),
            ])
        finally:
            release_first.set(); first.join(3); second.join(3); server.server_close()

    def test_failed_callback_ssh_forward_is_not_cached_and_never_opens_browser(self):
        opener, forward = Mock(), Mock(side_effect=[RuntimeError("unavailable"), None])
        server = bridge_server.BrowserBridgeHTTPServer(
            ("127.0.0.1", 0), "f" * 48, opener=opener, callback_forwarder=forward,
        )
        thread = start_server(server)
        try:
            with self.assertRaises(ValueError):
                browser_open.call_host_bridge(auth_url(), "f" * 48, port=server.server_port)
            browser_open.call_host_bridge(auth_url(), "f" * 48, port=server.server_port)
            self.assertEqual(forward.call_count, 2)
            opener.assert_called_once()
        finally:
            server.shutdown(); thread.join(timeout=2); server.server_close()

    def test_repeated_handoff_revalidates_callback_forward_before_opening(self):
        opener, forward = Mock(), Mock(side_effect=[None, RuntimeError("forward lost")])
        server = bridge_server.BrowserBridgeHTTPServer(
            ("127.0.0.1", 0), "z" * 48, opener=opener, callback_forwarder=forward,
        )
        try:
            server.open_authorization_url(auth_url(40391, "first"))
            with self.assertRaises(RuntimeError):
                server.open_authorization_url(auth_url(40391, "retry"))
            self.assertEqual(forward.call_count, 2)
            opener.assert_called_once()
            self.assertEqual(server.callback_port, 40391)
        finally:
            server.server_close()

    def test_browser_is_not_opened_when_dynamic_callback_forward_fails(self):
        opened = []
        server = bridge_server.BrowserBridgeHTTPServer(
            ("127.0.0.1", 0), "f" * 48, opener=opened.append,
            callback_forwarder=lambda _port: (_ for _ in ()).throw(RuntimeError("forward failed")),
        )
        thread = start_server(server)
        try:
            with self.assertRaises(ValueError):
                browser_open.call_host_bridge(auth_url(state="synthetic-private-state"), "f" * 48,
                                              port=server.server_port)
        finally:
            server.shutdown(); thread.join(timeout=2); server.server_close()
        self.assertEqual(opened, [])

    def test_host_rejects_unsafe_url_even_if_guest_validation_is_bypassed(self):
        opened = []
        server = bridge_server.BrowserBridgeHTTPServer(("127.0.0.1", 0), "e" * 48, opener=opened.append)
        thread = start_server(server)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            session, version = initialize(connection, "e" * 48)
            result, _ = browser_open._request(
                connection, "e" * 48,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                    "name": "open_auth_url", "arguments": {"url": "https://auth.openai.com/callback?code=synthetic-code"},
                }}, session_id=session, protocol_version=version,
            )
            self.assertTrue(result["result"]["isError"])
            self.assertNotIn("synthetic-code", json.dumps(result))
        finally:
            connection.close(); server.shutdown(); thread.join(timeout=2); server.server_close()
        self.assertEqual(opened, [])

    def test_absent_or_invalid_bearer_token_is_rejected(self):
        server = bridge_server.BrowserBridgeHTTPServer(("127.0.0.1", 0), "t" * 48)
        thread = start_server(server)
        try:
            for headers in ({}, {"Authorization": "Bearer wrong"}):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                connection.request("POST", "/mcp", body=b"{}", headers={"Content-Type": "application/json", **headers})
                response = connection.getresponse()
                self.assertEqual(response.status, 401)
                response.read(); connection.close()
        finally:
            server.shutdown(); thread.join(timeout=2); server.server_close()

    def test_oversized_and_malformed_requests_are_rejected_without_echo(self):
        server = bridge_server.BrowserBridgeHTTPServer(("127.0.0.1", 0), "u" * 48)
        thread = start_server(server)
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("POST", "/mcp", body=b"x" * 100,
                               headers={"Authorization": "Bearer " + "u" * 48,
                                        "Content-Type": "application/json", "Accept": "application/json",
                                        "Content-Length": str(16 * 1024 + 1)})
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            body = response.read().decode()
            self.assertNotIn("x" * 100, body)
            connection.close()
        finally:
            server.shutdown(); thread.join(timeout=2); server.server_close()

    def test_callback_lease_expires_and_session_close_releases_forward(self):
        released = threading.Event()
        cancelled = []
        server = bridge_server.BrowserBridgeHTTPServer(
            ("127.0.0.1", 0), "l" * 48, opener=lambda _url: None,
            callback_forwarder=lambda _port: None,
            callback_canceller=lambda port: (cancelled.append(port), released.set()),
            lease_seconds=1,
        )
        try:
            server.open_authorization_url(auth_url(40391))
            self.assertTrue(released.wait(2.5))
            self.assertEqual(cancelled, [40391])
            self.assertIsNone(server.callback_port)

            released.clear()
            server.open_authorization_url(auth_url(40392))
            server.server_close()
            self.assertTrue(released.wait(1))
            self.assertEqual(cancelled, [40391, 40392])
        finally:
            server.server_close()

    def test_callback_lease_limit_is_bounded_to_ten_minutes(self):
        for seconds in (0, -1, 601, True, "120"):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                bridge_server.BrowserBridgeHTTPServer(
                    ("127.0.0.1", 0), "m" * 48, lease_seconds=seconds,
                )

    def test_token_file_client_rejects_symlink_and_non_private_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "token"
            path.write_text("n" * 48, encoding="ascii")
            path.chmod(0o644)
            with patch.dict(os.environ, {"VM_BROWSER_BRIDGE_TOKEN_FILE": str(path),
                                         "VM_BROWSER_BRIDGE_PORT": "43123"}):
                with self.assertRaises(ValueError):
                    browser_open.load_bridge_token()
                path.chmod(0o600)
                self.assertEqual(browser_open.load_bridge_token(), "n" * 48)
                symlink = Path(temporary) / "token-link"
                symlink.symlink_to(path)
                with patch.dict(os.environ, {"VM_BROWSER_BRIDGE_TOKEN_FILE": str(symlink)}), self.assertRaises(ValueError):
                    browser_open.load_bridge_token()


if __name__ == "__main__":
    unittest.main()
