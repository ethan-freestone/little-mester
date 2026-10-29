import importlib.util
import json
import contextlib
import io
import http.client
import sys
import threading
import unittest
from pathlib import Path


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
        server = bridge_server.BrowserBridgeHTTPServer(
            ("127.0.0.1", 0), token, opener=opened.append
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        urls = [
            "https://auth.openai.com/oauth/authorize?state=first&code_challenge=x",
            "https://chatgpt.com/auth/login?state=second",
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
                browser_open._close_session(connection, token, session_id, version)
                connection.close()

                with self.assertRaises(Exception):
                    browser_open.call_host_bridge(urls[0], "wrong-token", port=server.server_port)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(opened, urls)
        self.assertEqual(server.sessions, {})
        self.assertNotIn("state=first", captured.getvalue())
        self.assertNotIn("state=second", captured.getvalue())

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
