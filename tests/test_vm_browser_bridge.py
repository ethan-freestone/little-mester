import importlib.util
import json
import contextlib
import io
import http.client
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import quote
from unittest.mock import Mock, patch


MODULE_PATH = Path(__file__).parents[1] / "lib" / "vm" / "browser-open.py"
SPEC = importlib.util.spec_from_file_location("vm_browser_open", MODULE_PATH)
browser_open = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = browser_open
SPEC.loader.exec_module(browser_open)
SERVER_PATH = Path(__file__).parents[1] / "lib" / "vm" / "browser-bridge-server.py"
SERVER_SPEC = importlib.util.spec_from_file_location("vm_browser_bridge_server", SERVER_PATH)
bridge_server = importlib.util.module_from_spec(SERVER_SPEC)
sys.modules[SERVER_SPEC.name] = bridge_server
SERVER_SPEC.loader.exec_module(bridge_server)


class BrowserBridgeClientTests(unittest.TestCase):
    def test_only_approved_authorization_urls_are_sent(self):
        self.assertTrue(browser_open.is_authorization_url(
            "https://auth.openai.com/oauth/authorize?state=ephemeral&code_challenge=x"
        ))
        self.assertTrue(browser_open.is_authorization_url(
            "https://chatgpt.com/auth/login?state=ephemeral"
        ))
        for url in (
            "http://auth.openai.com/oauth/authorize?state=x",
            "https://evil.example/oauth/authorize?state=x",
            "https://auth.openai.com.evil.example/oauth/authorize?state=x",
            "https://user@auth.openai.com/oauth/authorize?state=x",
            "https://auth.openai.com:444/oauth/authorize?state=x",
            "https://auth.openai.com/callback?code=callback-secret",
            "https://auth.openai.com/callback?access_token=callback-secret",
            "https://auth.openai.com/oauth/authorize?code_verifier=secret",
            "https://auth.openai.com/oauth/callback?state=secret",
            "https://auth.openai.com/oauth/authorize#fragment",
        ):
            with self.subTest(url_type=url.split(":", 1)[0]):
                self.assertFalse(browser_open.is_authorization_url(url))

    def test_host_mcp_server_accepts_only_one_tool_and_supports_repeated_handoffs(self):
        token = "d" * 48
        opened = []
        forwarded = []
        events = []
        callback_url = (
            "https://auth.openai.com/api/accounts/authorize?state=first&code_challenge=x&redirect_uri="
            + quote("http://127.0.0.1:40391/auth/callback", safe="")
        )
        def open_url(url):
            opened.append(url)
            events.append(("open", url))

        def forward_callback(port):
            forwarded.append(port)
            events.append(("forward", port))

        server = bridge_server.BrowserBridgeHTTPServer(
            ("127.0.0.1", 0), token, opener=open_url,
            callback_forwarder=forward_callback,
            callback_canceller=lambda port: events.append(("cancel", port)),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        urls = [
            callback_url,
            callback_url,
            callback_url.replace("40391", "40392").replace("first", "second"),
        ]
        captured = io.StringIO()
        try:
            with contextlib.redirect_stderr(captured), contextlib.redirect_stdout(captured):
                for url in urls:
                    browser_open.call_host_bridge(url, token, port=server.server_port)

                # Verify the advertised MCP surface using the same negotiated
                # protocol and bearer authorization as the guest opener.
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                initialized, session_id = browser_open._request(
                    connection,
                    token,
                    {
                        "jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1"},
                        },
                    },
                )
                version = initialized["result"]["protocolVersion"]
                browser_open._request(
                    connection, token,
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    notification=True, session_id=session_id, protocol_version=version,
                )
                tools, _ = browser_open._request(
                    connection, token,
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                    session_id=session_id, protocol_version=version,
                )
                self.assertEqual([tool["name"] for tool in tools["result"]["tools"]], ["open_auth_url"])
                for params in (
                    {"name": "run_command", "arguments": {"url": callback_url}},
                    {"name": "open_auth_url", "arguments": {"url": callback_url, "command": "ignored"}},
                ):
                    result, _ = browser_open._request(
                        connection, token,
                        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": params},
                        session_id=session_id, protocol_version=version,
                    )
                    self.assertTrue(result["result"]["isError"])
                browser_open._close_session(connection, token, session_id, version)
                connection.close()

                with self.assertRaises(Exception):
                    browser_open.call_host_bridge(urls[0], "wrong-token", port=server.server_port)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(opened, urls)
        self.assertEqual(forwarded, [40391, 40392])
        self.assertEqual(events, [
            ("forward", 40391),
            ("open", callback_url),
            ("open", callback_url),
            ("cancel", 40391),
            ("forward", 40392),
            ("open", urls[2]),
        ])
        self.assertEqual(server.sessions, {})
        self.assertNotIn("state=first", captured.getvalue())
        self.assertNotIn("state=second", captured.getvalue())

    def test_callback_forward_uses_runtime_redirect_uri_and_only_loopback(self):
        auth_url = (
            "https://auth.openai.com/api/accounts/authorize?state=not-logged&redirect_uri="
            + quote("http://127.0.0.1:40391/auth/callback", safe="")
        )
        self.assertEqual(bridge_server.openai_callback_port(auth_url), 40391)
        with patch.dict(os.environ, {
            "VM_BROWSER_BRIDGE_SSH_CONTROL_PATH": "/tmp/lab-vm-browser-host.test/ssh-control",
            "VM_BROWSER_BRIDGE_SSH_PORT": "22222",
        }, clear=False):
            command = bridge_server.callback_forward_command(40391)
        self.assertIn("-O", command)
        self.assertIn("forward", command)
        self.assertEqual(command[1:3], ["-F", "/dev/null"])
        self.assertIn("ForwardAgent=no", command)
        self.assertIn("ForwardX11=no", command)
        self.assertEqual(
            command[command.index("-L") + 1],
            "127.0.0.1:40391:127.0.0.1:40391",
        )
        self.assertNotIn("0.0.0.0", " ".join(command))
        with patch.dict(os.environ, {
            "VM_BROWSER_BRIDGE_SSH_CONTROL_PATH": "/tmp/lab-vm-browser-host.test/ssh-control",
            "VM_BROWSER_BRIDGE_SSH_PORT": "22222",
        }, clear=False):
            cancel = bridge_server.callback_forward_command(40391, "cancel")
        self.assertEqual(cancel[cancel.index("-O") + 1], "cancel")
        self.assertEqual(cancel[cancel.index("-L") + 1], "127.0.0.1:40391:127.0.0.1:40391")

    def test_callback_uri_validation_fails_closed_for_unexpected_redirects(self):
        for redirect_uri in (
            "http://localhost:40391/auth/callback",
            "http://127.0.0.1:80/auth/callback",
            "http://127.0.0.1:40391/other",
            "http://127.0.0.1:40391/auth/callback?extra=1",
            "https://127.0.0.1:40391/auth/callback",
            "http://127.0.0.1:65536/auth/callback",
            "http://127.0.0.1:40391/auth/callback#fragment",
            "http://user@127.0.0.1:40391/auth/callback",
            "http://192.168.1.1:40391/auth/callback",
            "http://[::1]:40391/auth/callback",
            "http://127.0.0.1:40391/auth/call\nback",
        ):
            url = (
                "https://auth.openai.com/api/accounts/authorize?state=hidden&redirect_uri="
                + quote(redirect_uri, safe="")
            )
            with self.subTest(redirect_uri=redirect_uri):
                with self.assertRaises(ValueError):
                    bridge_server.openai_callback_port(url)

        missing_redirect = "https://auth.openai.com/api/accounts/authorize?state=hidden"
        with self.assertRaises(ValueError):
            bridge_server.openai_callback_port(missing_redirect)

    def test_host_rejects_changed_authorize_endpoint_or_duplicate_callback(self):
        opened = Mock()
        forwarded = Mock()
        server = bridge_server.BrowserBridgeHTTPServer(
            ("127.0.0.1", 0), "d" * 48, opener=opened, callback_forwarder=forwarded,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        redirect = "redirect_uri=" + quote("http://127.0.0.1:40391/auth/callback", safe="")
        try:
            for url in (
                "https://auth.openai.com/oauth/authorize?" + redirect,
                "https://chatgpt.com/auth/login?" + redirect,
                "https://auth.openai.com/api/accounts/authorize?" + redirect + "&" + redirect,
                "https://auth.openai.com/api/accounts/authorize?state=missing-redirect",
            ):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    browser_open.call_host_bridge(url, "d" * 48, port=server.server_port)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()
        opened.assert_not_called()
        forwarded.assert_not_called()

    def test_callback_replacement_stops_if_previous_forward_cannot_be_removed(self):
        forward = Mock()
        cancel = Mock(side_effect=RuntimeError("cancel failed"))
        opener = Mock()
        server = bridge_server.BrowserBridgeHTTPServer(
            ("127.0.0.1", 0), "d" * 48, callback_forwarder=forward, callback_canceller=cancel,
            opener=opener,
        )
        try:
            first_url = "https://auth.openai.com/api/accounts/authorize?redirect_uri=" + quote(
                "http://127.0.0.1:40391/auth/callback", safe=""
            )
            second_url = first_url.replace("40391", "40392")
            server.open_authorization_url(first_url)
            with self.assertRaises(RuntimeError):
                server.open_authorization_url(second_url)
            forward.assert_called_once_with(40391)
            cancel.assert_called_once_with(40391)
            opener.assert_called_once_with(first_url)
        finally:
            server.server_close()

    def test_concurrent_authorization_handoffs_do_not_cancel_an_active_browser_flow(self):
        events = []
        first_open_started = threading.Event()
        release_first_open = threading.Event()
        second_lock_attempted = threading.Event()

        class ObservedLock:
            def __init__(self):
                self.lock = threading.Lock()

            def __enter__(self):
                if threading.current_thread().name == "second-auth-handoff":
                    second_lock_attempted.set()
                self.lock.acquire()
                return self

            def __exit__(self, *_args):
                self.lock.release()

        def open_url(url):
            port = bridge_server.openai_callback_port(url)
            events.append(("open-start", port))
            if port == 40391:
                first_open_started.set()
                if not release_first_open.wait(timeout=3):
                    raise RuntimeError("test timed out waiting to release browser open")
            events.append(("open-end", port))

        server = bridge_server.BrowserBridgeHTTPServer(
            ("127.0.0.1", 0), "d" * 48, opener=open_url,
            callback_forwarder=lambda port: events.append(("forward", port)),
            callback_canceller=lambda port: events.append(("cancel", port)),
        )
        server.callback_lock = ObservedLock()
        url1 = "https://auth.openai.com/api/accounts/authorize?redirect_uri=" + quote(
            "http://127.0.0.1:40391/auth/callback", safe=""
        )
        url2 = url1.replace("40391", "40392")
        first = threading.Thread(target=server.open_authorization_url, args=(url1,))
        second = threading.Thread(
            target=server.open_authorization_url,
            args=(url2,),
            name="second-auth-handoff",
        )
        try:
            first.start()
            self.assertTrue(first_open_started.wait(timeout=2))
            second.start()
            self.assertTrue(second_lock_attempted.wait(timeout=2))
            self.assertEqual(events, [("forward", 40391), ("open-start", 40391)])
            release_first_open.set()
            first.join(timeout=3)
            second.join(timeout=3)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(events, [
                ("forward", 40391), ("open-start", 40391), ("open-end", 40391),
                ("cancel", 40391), ("forward", 40392), ("open-start", 40392),
                ("open-end", 40392),
            ])
        finally:
            release_first_open.set()
            first.join(timeout=3)
            second.join(timeout=3)
            server.server_close()

    def test_failed_ssh_forward_is_not_cached_and_never_opens_browser(self):
        opened = Mock()
        server = bridge_server.BrowserBridgeHTTPServer(("127.0.0.1", 0), "d" * 48, opener=opened)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "https://auth.openai.com/api/accounts/authorize?state=not-logged&redirect_uri=" + quote(
            "http://127.0.0.1:40391/auth/callback", safe=""
        )
        try:
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "VM_BROWSER_BRIDGE_SSH_CONTROL_PATH": str(Path(directory) / "control"),
                "VM_BROWSER_BRIDGE_SSH_PORT": "22222",
                "VM_BROWSER_BRIDGE_SSH_LOG": str(Path(directory) / "ssh.log"),
            }), patch.object(bridge_server.subprocess, "run") as run:
                run.side_effect = [
                    subprocess.CompletedProcess([], 255),
                    subprocess.TimeoutExpired("ssh", 10),
                    subprocess.CompletedProcess([], 0),
                ]
                for _ in range(2):
                    with self.assertRaises(ValueError):
                        browser_open.call_host_bridge(url, "d" * 48, port=server.server_port)
                    opened.assert_not_called()
                browser_open.call_host_bridge(url, "d" * 48, port=server.server_port)
                self.assertEqual(run.call_count, 3)
                opened.assert_called_once_with(url)
                self.assertNotIn("not-logged", (Path(directory) / "ssh.log").read_text())
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_browser_is_not_opened_if_dynamic_callback_forward_fails(self):
        opened = []

        def fail_forward(_port):
            raise RuntimeError("forward failed")

        server = bridge_server.BrowserBridgeHTTPServer(
            ("127.0.0.1", 0), "f" * 48,
            opener=opened.append,
            callback_forwarder=fail_forward,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        token = "f" * 48
        auth_url = (
            "https://auth.openai.com/api/accounts/authorize?state=private-state&redirect_uri="
            + quote("http://127.0.0.1:40391/auth/callback", safe="")
        )
        try:
            initialized, session_id = browser_open._request(
                connection,
                token,
                {
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
            )
            version = initialized["result"]["protocolVersion"]
            browser_open._request(
                connection, token,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                notification=True, session_id=session_id, protocol_version=version,
            )
            result, _ = browser_open._request(
                connection, token,
                {
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "open_auth_url", "arguments": {"url": auth_url}},
                },
                session_id=session_id, protocol_version=version,
            )
            browser_open._close_session(connection, token, session_id, version)
        finally:
            connection.close()
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertTrue(result["result"]["isError"])
        self.assertEqual(opened, [])
        self.assertNotIn("private-state", json.dumps(result))

    def test_host_server_rejects_unapproved_url_even_if_guest_validation_is_bypassed(self):
        opened = []
        server = bridge_server.BrowserBridgeHTTPServer(
            ("127.0.0.1", 0), "e" * 48, opener=opened.append
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        token = "e" * 48
        unsafe_url = "https://auth.openai.com/callback?code=never-echo"
        try:
            initialized, session_id = browser_open._request(
                connection,
                token,
                {
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
            )
            version = initialized["result"]["protocolVersion"]
            browser_open._request(
                connection, token,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                notification=True, session_id=session_id, protocol_version=version,
            )
            result, _ = browser_open._request(
                connection, token,
                {
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "open_auth_url", "arguments": {"url": unsafe_url}},
                },
                session_id=session_id, protocol_version=version,
            )
            browser_open._close_session(connection, token, session_id, version)
        finally:
            connection.close()
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertTrue(result["result"]["isError"])
        self.assertNotIn("never-echo", json.dumps(result))
        self.assertEqual(opened, [])


if __name__ == "__main__":
    unittest.main()
