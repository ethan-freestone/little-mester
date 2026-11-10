#!/usr/bin/env python3
"""Synthetic-only browser-auth validation and local OpenSSH forwarding tests."""

import errno
import importlib.util
import os
import pathlib
import pty
import pwd
import select
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import types
import unittest
import urllib.parse
from socketserver import BaseRequestHandler, ThreadingTCPServer
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).parent.parent
SESSION_PATH = ROOT / "lib/vm/browser-auth-session.py"
SPEC = importlib.util.spec_from_file_location("mester_browser_auth", SESSION_PATH)
auth = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(auth)


def authorization_url(port=1455, *, state="S" * 43, nonce="N" * 43, challenge="C" * 43):
    if port not in (1455, 1457):
        raise ValueError("pinned OpenCode callback ports only")
    query = {
        "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "response_type": "code",
        "redirect_uri": f"http://localhost:{port}/auth/callback",
        "scope": "openid profile email offline_access",
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": "opencode",
    }
    return "https://auth.openai.com/oauth/authorize?" + urllib.parse.urlencode(query)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


class AuthorizationValidationTests(unittest.TestCase):
    def test_pinned_open_code_authorize_shape_and_literal_callback_forward(self):
        value = authorization_url(1455)
        self.assertEqual(auth.validate_authorization_url(value), 1455)
        command = auth.control_socket_arguments(
            "/usr/bin/ssh", "/tmp/private/auth.sock", "forward", 1455, 43210,
            "/tmp/private/id", "/tmp/private/known_hosts",
        )
        self.assertEqual(command.count("-L"), 1)
        self.assertEqual(command[command.index("-L") + 1], "127.0.0.1:1455:localhost:1455")
        self.assertNotIn("-R", command)
        self.assertNotIn("-D", command)
        self.assertIn("/dev/null", command)
        self.assertIn("ForwardAgent=no", command)
        self.assertIn("ForwardX11=no", command)
        self.assertIn("ProxyCommand=none", command)
        self.assertIn("ProxyJump=none", command)

    def test_rejects_unexpected_endpoint_redirect_and_query_shape(self):
        valid = authorization_url()
        rejected = [
            valid.replace("https://auth.openai.com/", "http://auth.openai.com/", 1),
            valid.replace("auth.openai.com", "auth.openai.com.evil.invalid", 1),
            valid.replace("/oauth/authorize", "/api/other", 1),
            valid.replace("http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback",
                          "http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback%3Fextra%3D1"),
            valid + "&redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback",
            valid + "#fragment",
            valid.replace("state=" + "S" * 43, "state=forged"),
            valid.replace("code_challenge_method=S256", "code_challenge_method=plain"),
            valid.replace("code_challenge=" + "C" * 43, "code_challenge=short"),
            valid + "&code_verifier=synthetic-canary",
            valid.replace("localhost%3A1455", "127.0.0.1%3A1455"),
            valid.replace("localhost%3A1455", "localhost%3A1456"),
            valid.replace("localhost%3A1455", "localhost%3A1023"),
            valid.replace("localhost%3A1455", "evil.invalid%3A1455"),
        ]
        for candidate in rejected:
            with self.subTest(candidate=candidate[:96]):
                with self.assertRaises(auth.AuthSessionError):
                    auth.validate_authorization_url(candidate)

    def test_rejects_duplicate_oauth_state_and_callback_secret_fields(self):
        valid = authorization_url()
        for candidate in (valid + "&state=" + "T" * 43, valid + "&code=synthetic-code-canary",
                          valid + "&unexpected=extra"):
            with self.subTest(candidate=candidate[-60:]):
                with self.assertRaises(auth.AuthSessionError):
                    auth.validate_authorization_url(candidate)

    def test_requires_exact_pinned_open_code_scope_and_auth_path_hides_openers(self):
        valid = authorization_url()
        with self.assertRaises(auth.AuthSessionError):
            auth.validate_authorization_url(valid.replace(
                "scope=openid+profile+email+offline_access",
                "scope=openid+profile+email+offline_access+admin",
            ))
        session = auth.BrowserAuthSession.__new__(auth.BrowserAuthSession)
        session.args = SimpleNamespace(guest_auth_dir="/tmp/mester-v3-auth-unit")
        remote = session._remote_command()
        self.assertIn("PATH=/tmp/mester-v3-auth-unit:/usr/local/bin", remote)
        self.assertIn("for opener in xdg-open gio sensible-browser firefox chromium chromium-browser", remote)
        self.assertIn("opencode auth login openai --method chatgpt-browser", remote)
        self.assertNotIn("auth.openai.com", remote)

    def test_auth_mode_does_not_require_a_pty(self):
        check = subprocess.run(
            ["/usr/bin/python3", "-I", "-c",
             f"import importlib.util; s=importlib.util.spec_from_file_location('a',{str(SESSION_PATH)!r}); "
             "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
             "x=m.BrowserAuthSession.__new__(m.BrowserAuthSession); x.tty_fd=None; x._optional_tty(); "
             "assert x.tty_fd is None"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, timeout=5, check=False,
        )
        self.assertEqual(check.returncode, 0, check.stderr.decode("utf-8", "replace"))

    def test_portal_request_is_after_forward_verification_and_receives_one_validated_url(self):
        url = authorization_url()
        events = []

        class Receiver:
            def remove(self):
                events.append("removed")

        class Bus:
            def get_unique_name(self):
                return ":1.99"

            def call_blocking(self, destination, path, interface, method, signature, args, timeout):
                events.append(("call", destination, path, interface, method, signature, args, timeout))
                if method == "OpenURI":
                    return f"/org/freedesktop/portal/desktop/request/1_99/{args[2]['handle_token']}"
                return None

            def add_signal_receiver(self, callback, **kwargs):
                events.append(("subscribe", kwargs["path"]))
                self.callback = callback
                return Receiver()

        bus = Bus()

        class Loop:
            def __init__(self):
                self.callback = None

            def run(self):
                bus.callback(0, {})

            def quit(self):
                pass

        fake_dbus = types.ModuleType("dbus")
        fake_dbus.SessionBus = lambda: bus
        fake_dbus.String = lambda value, **_kwargs: value
        fake_dbus.Dictionary = lambda value, **_kwargs: value
        mainloop = types.ModuleType("dbus.mainloop")
        glib_dbus = types.ModuleType("dbus.mainloop.glib")
        glib_dbus.DBusGMainLoop = lambda **_kwargs: None
        gi = types.ModuleType("gi")
        repository = types.ModuleType("gi.repository")
        repository.GLib = types.SimpleNamespace(
            MainLoop=Loop,
            timeout_add=lambda _seconds, _callback: 1,
            source_remove=lambda _source: None,
        )
        session = auth.BrowserAuthSession.__new__(auth.BrowserAuthSession)
        session.callback_port = 1455
        session.deadline = 10.0
        session.signal_received = None
        session.auth_process_completed = False
        session.portal_cleanup_failed = False
        clock = [0.0]
        readiness_calls = [0]

        def ready_near_deadline(port):
            readiness_calls[0] += 1
            clock[0] = 9.7 if readiness_calls[0] == 1 else 9.8
            events.append(("ready", port))
            return True

        session._loopback_forward_ready = ready_near_deadline
        with (
            mock.patch.dict("sys.modules", {
                "dbus": fake_dbus,
                "dbus.mainloop": mainloop,
                "dbus.mainloop.glib": glib_dbus,
                "gi": gi,
                "gi.repository": repository,
            }),
            mock.patch.object(auth.time, "monotonic", side_effect=lambda: clock[0]),
        ):
            self.assertTrue(session._open_portal(url, session.deadline))
        self.assertEqual(events[0], ("ready", 1455))
        self.assertEqual(events[1][0], "subscribe")
        self.assertEqual(events[1][1].rsplit("/", 1)[0], "/org/freedesktop/portal/desktop/request/1_99")
        opened = next(event for event in events if isinstance(event, tuple) and event[0] == "call")
        self.assertEqual(opened[1:6], (
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.OpenURI",
            "OpenURI",
            "ssa{sv}",
        ))
        self.assertEqual(opened[6][1], url)
        self.assertGreater(opened[7], 0)
        self.assertLessEqual(opened[7], 0.2)
        self.assertEqual(events[-1], "removed")

    def test_portal_absence_without_tty_fails_closed_without_printing_url(self):
        session = auth.BrowserAuthSession.__new__(auth.BrowserAuthSession)
        session.tty_fd = None
        session.status = 0
        session.callback_port = 1455
        session.signal_received = None
        session.deadline = None
        session._loopback_forward_ready = lambda _port: True
        session._open_portal = lambda *_args: False
        with self.assertRaises(auth.AuthSessionError) as error:
            session._portal_or_manual(authorization_url())
        self.assertEqual(error.exception.status, 76)

    def test_portal_failure_after_cancellation_never_discloses_manual_url(self):
        session = auth.BrowserAuthSession.__new__(auth.BrowserAuthSession)
        session.tty_fd = 1
        session.callback_port = 1455
        session.deadline = time.monotonic() + 30
        session.signal_received = None
        session._loopback_forward_ready = lambda _port: True
        output = []

        def cancel_during_portal(_url, _deadline):
            session.signal_received = signal.SIGTERM
            return False

        session._open_portal = cancel_during_portal
        session._write_tty = output.append
        url = authorization_url()
        with self.assertRaises(auth.AuthSessionError) as error:
            session._portal_or_manual(url, session.deadline)
        self.assertEqual(error.exception.status, 128 + signal.SIGTERM)
        self.assertFalse(any(url.encode("ascii") in chunk for chunk in output))

    def test_cancellation_during_readiness_probe_prevents_manual_url_disclosure(self):
        session = auth.BrowserAuthSession.__new__(auth.BrowserAuthSession)
        session.tty_fd = 1
        session.callback_port = 1455
        session.deadline = time.monotonic() + 30
        session.signal_received = None
        session.auth_process_completed = False
        output = []

        def cancelled_probe(_port):
            session.signal_received = signal.SIGTERM
            return True

        session._loopback_forward_ready = cancelled_probe
        session._open_portal = lambda *_args: (session._assert_auth_live(session.deadline), False)[1]
        session._write_tty = output.append
        url = authorization_url()
        with self.assertRaises(auth.AuthSessionError) as error:
            session._portal_or_manual(url, session.deadline)
        self.assertEqual(error.exception.status, 128 + signal.SIGTERM)
        self.assertFalse(any(url.encode("ascii") in chunk for chunk in output))

    def test_successful_guest_completion_during_readiness_probe_is_not_misclassified(self):
        session = auth.BrowserAuthSession.__new__(auth.BrowserAuthSession)
        session.callback_port = 1455
        session.deadline = time.monotonic() + 30
        session.signal_received = None
        session.auth_process_completed = False

        class Process:
            returncode = 0

            def __init__(self):
                self.polls = 0

            def poll(self):
                self.polls += 1
                return None if self.polls == 1 else 0

        session.ssh_process = Process()
        session._loopback_forward_ready = lambda _port: False
        session._assert_auth_live(session.deadline)
        self.assertTrue(session.auth_process_completed)

    def test_portal_wait_cancellation_closes_pending_request(self):
        for mode in (
            "cancel", "timeout", "mismatch", "open-error", "completion",
            "cancel-close-error", "completion-close-error",
        ):
            with self.subTest(mode=mode):
                closed = []
                handles = []
                callbacks = []
                session = auth.BrowserAuthSession.__new__(auth.BrowserAuthSession)
                session.callback_port = 1455
                session.deadline = time.monotonic() + 30
                session.signal_received = None
                session.auth_process_completed = False
                session.portal_cleanup_failed = False
                session._loopback_forward_ready = lambda _port: True

                class Receiver:
                    def remove(self):
                        pass

                class Bus:
                    def get_unique_name(self):
                        return ":1.7"

                    def call_blocking(self, _destination, path, _interface, method, signature, args, timeout):
                        if method == "OpenURI":
                            token = args[2]["handle_token"]
                            expected = f"/org/freedesktop/portal/desktop/request/1_7/{token}"
                            if mode == "open-error":
                                handles.append(expected)
                                raise RuntimeError("synthetic portal submission error")
                            actual = expected + "_unexpected" if mode == "mismatch" else expected
                            handles.append(actual)
                            return actual
                        closed.append((path, timeout))
                        if mode.endswith("close-error"):
                            raise RuntimeError("synthetic pending request close error")
                        return None

                    def add_signal_receiver(self, _callback, **_kwargs):
                        return Receiver()

                bus = Bus()

                class Loop:
                    def run(self):
                        if mode.startswith("cancel"):
                            session.signal_received = signal.SIGTERM
                            callbacks[0]()
                        elif mode.startswith("completion"):
                            session.ssh_process = SimpleNamespace(returncode=0, poll=lambda: 0)
                            callbacks[0]()
                        else:
                            callbacks[1]()

                    def quit(self):
                        pass

                glib = types.SimpleNamespace(
                    MainLoop=Loop,
                    timeout_add=lambda _ms, callback: callbacks.append(callback) or len(callbacks),
                    source_remove=lambda _source: None,
                )
                fake_dbus = types.ModuleType("dbus")
                fake_dbus.SessionBus = lambda: bus
                fake_dbus.String = lambda value, **_kwargs: value
                fake_dbus.Dictionary = lambda value, **_kwargs: value
                mainloop = types.ModuleType("dbus.mainloop")
                glib_dbus = types.ModuleType("dbus.mainloop.glib")
                glib_dbus.DBusGMainLoop = lambda **_kwargs: None
                gi = types.ModuleType("gi")
                repository = types.ModuleType("gi.repository")
                repository.GLib = glib
                with mock.patch.dict("sys.modules", {
                    "dbus": fake_dbus,
                    "dbus.mainloop": mainloop,
                    "dbus.mainloop.glib": glib_dbus,
                    "gi": gi,
                    "gi.repository": repository,
                }):
                    if mode.startswith("cancel") or mode == "mismatch":
                        with self.assertRaises(auth.AuthSessionError) as error:
                            session._open_portal(authorization_url(), session.deadline)
                        self.assertEqual(
                            error.exception.status,
                            128 + signal.SIGTERM if mode.startswith("cancel") else 77,
                        )
                    else:
                        self.assertFalse(session._open_portal(authorization_url(), session.deadline))
                if mode.startswith("completion"):
                    self.assertTrue(session.auth_process_completed)
                self.assertEqual(closed, [(handles[0], 2)])
                self.assertEqual(session.portal_cleanup_failed, mode.endswith("close-error"))

    def test_listener_absence_detects_ipv4_mapped_ipv6_callback_sockets(self):
        listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            listener.bind(("::ffff:127.0.0.1", 0))
            listener.listen(1)
            self.assertFalse(auth.listener_absent(listener.getsockname()[1]))
        finally:
            listener.close()

    def test_listener_absence_detects_native_ipv6_localhost_collisions(self):
        listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            listener.bind(("::1", 0))
            listener.listen(1)
            self.assertFalse(auth.listener_absent(listener.getsockname()[1]))
        finally:
            listener.close()

    def test_ipv6_localhost_guard_prevents_late_callback_listener_bind(self):
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            probe.bind(("::1", 0))
            port = probe.getsockname()[1]
        finally:
            probe.close()
        session = auth.BrowserAuthSession.__new__(auth.BrowserAuthSession)
        session.ipv6_guard = None
        with mock.patch.object(
            auth.socket,
            "getaddrinfo",
            return_value=[(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("::1", port, 0, 0))],
        ):
            session._reserve_ipv6_localhost(port)
        try:
            self.assertIsNotNone(session.ipv6_guard)
            attacker = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            try:
                attacker.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                with self.assertRaises(OSError):
                    attacker.bind(("::1", port))
            finally:
                attacker.close()
        finally:
            if session.ipv6_guard is not None:
                session.ipv6_guard.close()

    def test_ipv6_reservation_collision_never_claims_listener_cleanup_proof(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            session = auth.BrowserAuthSession.__new__(auth.BrowserAuthSession)
            session.forward_attempted = False
            session.forward_installed = False
            session.callback_port = None
            session.listener_verified_closed = True
            session.listener_was_available = False
            session.ipv6_guard = None
            session.portal_cleanup_failed = False
            session.args = SimpleNamespace()
            session.socket_path = root / "control.sock"
            session.identity_path = root / "ssh.identity"
            session.ssh_process = None
            session.proof_path = root / "proof.json"
            session._reserve_ipv6_localhost = lambda _port: (_ for _ in ()).throw(
                auth.AuthSessionError("IPv6 localhost callback collision could not be excluded", 77)
            )
            with self.assertRaises(auth.AuthSessionError):
                session._install_forward(1455)
            self.assertEqual(session.callback_port, 1455)
            self.assertFalse(session.listener_verified_closed)
            self.assertFalse(session._cleanup())
            session._write_proof(False)
            proof = __import__("json").loads(session.proof_path.read_text())
            self.assertFalse(proof["listener_verified_closed"])
            self.assertFalse(proof["cleanup_complete"])

    def test_native_tui_url_scanner_holds_url_until_forward_and_refuses_races(self):
        first = authorization_url(1455)
        encoded = b"\x1b]8;;" + first.encode("ascii") + b"\x1b\\"
        scanner = auth.AuthorizationOutputScanner()
        prefix = b"TUI-prefix:" + b"x" * 64
        visible, port, held = scanner.feed(prefix + encoded[:19])
        self.assertTrue(visible.startswith(b"TUI-prefix:"))
        self.assertNotIn(first.encode("ascii"), visible)
        self.assertIsNone(port)
        self.assertEqual(held, b"")
        visible, port, held = scanner.feed(encoded[19:] + b"URL-visible-copy")
        self.assertEqual(port, 1455)
        self.assertNotIn(first.encode("ascii"), visible)
        self.assertIn(first.encode("ascii"), held)
        visible, port, held = scanner.feed(encoded)
        self.assertIsNone(port)  # An identical TUI redraw is harmless.
        self.assertIn(first.encode("ascii"), visible)
        self.assertEqual(held, b"")
        second = authorization_url(1457, state="T" * 43)
        with self.assertRaises(auth.AuthSessionError):
            scanner.feed(b"\x1b]8;;" + second.encode("ascii") + b"\x1b\\")


class FakeTargetHandler(BaseRequestHandler):
    def handle(self):
        request = self.request.recv(1024)
        self.request.sendall(b"LOCAL_FAKE_TARGET_OK:" + request)


FAKE_SSH_SOURCE = r'''#!/usr/bin/python3 -I
import os, pathlib, signal, socket, socketserver, sys, threading, time, urllib.parse

def after(flag):
    return sys.argv[sys.argv.index(flag) + 1]

if "-O" in sys.argv:
    operation = after("-O")
    spec = after("-L")
    port = int(spec.split(":")[1])
    try:
        control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        control.connect(after("-S"))
        control.sendall(f"{operation} {port}\n".encode("ascii"))
        reply = control.recv(16)
        control.close()
    except OSError:
        raise SystemExit(1)
    raise SystemExit(0 if reply == b"OK" else 1)

capture_path = os.environ.get("AUTH_TEST_CAPTURE")
if capture_path:
    with open(capture_path, "ab") as capture:
        capture.write(b"ARGV\0" + b"\0".join(os.fsencode(value) for value in sys.argv) + b"\n")
        capture.write(b"ENV\0" + b"\0".join(
            os.fsencode(key + "=" + value) for key, value in os.environ.items()
        ) + b"\n")

mode = os.environ["AUTH_TEST_MODE"]
callback_port = int(os.environ["AUTH_TEST_CALLBACK_PORT"])
target_port = int(os.environ["AUTH_TEST_TARGET_PORT"])
control_path = after("-S")
stop = threading.Event()
callback_seen = threading.Event()
forwards = {}

class RelayHandler(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.create_connection(("127.0.0.1", target_port), timeout=2) as target:
            data = self.request.recv(4096)
            target.sendall(data)
            self.request.sendall(target.recv(4096))
            if data:
                callback_seen.set()

class Forward(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True

def control_server():
    control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    control.bind(control_path)
    os.chmod(control_path, 0o600)
    control.listen(8)
    control.settimeout(0.05)
    try:
        while not stop.is_set():
            try:
                client, _ = control.accept()
            except socket.timeout:
                continue
            with client:
                words = client.recv(128).decode("ascii").split()
                if len(words) != 2:
                    client.sendall(b"NO")
                    continue
                operation, raw_port = words
                port = int(raw_port)
                if operation == "forward" and port not in forwards:
                    try:
                        server = Forward(("127.0.0.1", port), RelayHandler)
                    except OSError:
                        client.sendall(b"NO")
                        continue
                    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
                    thread.start()
                    forwards[port] = (server, thread)
                    client.sendall(b"OK")
                elif operation == "cancel" and port in forwards:
                    server, thread = forwards.pop(port)
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=1)
                    client.sendall(b"OK")
                else:
                    client.sendall(b"NO")
    finally:
        for server, thread in list(forwards.values()):
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)
        control.close()
        try:
            os.unlink(control_path)
        except FileNotFoundError:
            pass

def stop_signal(_signum, _frame):
    raise SystemExit(143)

signal.signal(signal.SIGTERM, stop_signal)
signal.signal(signal.SIGHUP, stop_signal)
thread = threading.Thread(target=control_server, daemon=True)
thread.start()
deadline = time.monotonic() + 3
while not os.path.exists(control_path) and time.monotonic() < deadline:
    time.sleep(0.005)
if not os.path.exists(control_path):
    raise SystemExit(125)

query = {
    "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
    "response_type": "code",
    "redirect_uri": f"http://localhost:{callback_port}/auth/callback",
    "scope": "openid profile email offline_access",
    "state": "Q" * 43,
    "code_challenge_method": "S256",
    "code_challenge": "C" * 43,
    "id_token_add_organizations": "true",
    "codex_cli_simplified_flow": "true",
    "originator": "opencode",
}
url = "https://auth.openai.com/oauth/authorize?" + urllib.parse.urlencode(query)
if mode == "invalid":
    url = url.replace("auth.openai.com", "auth.openai.com.evil.invalid", 1)
payload = b"\x1b]8;;" + url.encode("ascii") + b"\x1b\\"
if mode == "race":
    raced = dict(query)
    raced["redirect_uri"] = f"http://localhost:1457/auth/callback"
    raced["state"] = "R" * 43
    second = "https://auth.openai.com/oauth/authorize?" + urllib.parse.urlencode(raced)
    payload += b"\x1b]8;;" + second.encode("ascii") + b"\x1b\\"
os.write(1, payload)
if mode == "invalid":
    time.sleep(10)
elif mode == "cancel":
    while callback_port not in forwards:
        time.sleep(0.01)
    time.sleep(0.5)
    raise SystemExit(130)
elif mode == "failure":
    while callback_port not in forwards:
        time.sleep(0.01)
    time.sleep(0.5)
    raise SystemExit(23)
else:
    while not stop.is_set() and not (mode == "success" and callback_seen.is_set()):
        time.sleep(0.02)
raise SystemExit(0)
'''


class BrowserAuthProcessTests(unittest.TestCase):
    def _run_session(self, mode, *, occupied=False, timeout_seconds=5, use_pty=True):
        temporary = tempfile.TemporaryDirectory(prefix="mester-auth-session-")
        root = pathlib.Path(temporary.name)
        run_dir = root / "run"
        run_dir.mkdir(mode=0o700)
        identity = run_dir / "identity"
        known_hosts = run_dir / "known_hosts"
        identity.write_text("synthetic test identity\n")
        known_hosts.write_text("synthetic test host key\n")
        identity.chmod(0o600)
        known_hosts.chmod(0o600)
        fake_ssh = root / "fake-ssh"
        fake_ssh.write_text(FAKE_SSH_SOURCE)
        fake_ssh.chmod(0o700)
        target = ThreadingTCPServer(("127.0.0.1", 0), FakeTargetHandler)
        target.daemon_threads = True
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()
        callback_port = None
        occupied_socket = None
        for candidate in (1455, 1457):
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind(("127.0.0.1", candidate))
                if occupied:
                    probe.listen(1)
                    occupied_socket = probe
                else:
                    probe.close()
                callback_port = candidate
                break
            except OSError:
                probe.close()
        if callback_port is None:
            target.shutdown()
            target.server_close()
            target_thread.join(timeout=2)
            temporary.cleanup()
            self.skipTest("both pinned localhost callback ports are occupied")
        proof = run_dir / "auth-session.proof"
        command = [
            "/usr/bin/python3", "-I", str(SESSION_PATH),
            "--ssh", str(fake_ssh), "--ssh-port", "43210",
            "--identity-file", str(identity), "--known-hosts", str(known_hosts),
            "--control-socket", str(run_dir / "auth-control.sock"),
            "--ssh-identity", str(run_dir / "auth-ssh.identity"),
            "--callback-port-file", str(run_dir / "auth-callback.port"),
            "--proof-file", str(proof), "--run-dir", str(run_dir),
            "--guest-auth-dir", "/tmp/mester-v3-auth-test-session",
            "--timeout-seconds", str(timeout_seconds), "--output-limit", "65536",
        ]
        evidence = root / "evidence.log"
        process_capture = root / "ssh-argv-env.capture"
        evidence.touch(mode=0o600)
        evidence.chmod(0o600)
        test_env = os.environ.copy()
        test_env["AUTH_TEST_MODE"] = mode
        test_env["AUTH_TEST_CALLBACK_PORT"] = str(callback_port)
        test_env["AUTH_TEST_TARGET_PORT"] = str(target.server_address[1])
        test_env["AUTH_TEST_CAPTURE"] = str(process_capture)
        # Synthetic sessions must never invoke the real owner's desktop portal.
        test_env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent/auth-test-bus"
        test_env.pop("XDG_RUNTIME_DIR", None)
        test_env.pop("DISPLAY", None)
        test_env.pop("WAYLAND_DISPLAY", None)
        process = None
        if use_pty:
            pid, master = pty.fork()
            if pid == 0:
                os.environ.update(test_env)
                evidence_fd = os.open(evidence, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                os.dup2(evidence_fd, 1)
                os.dup2(evidence_fd, 2)
                os.execv(command[0], command)
        else:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=test_env, start_new_session=True, close_fds=True,
            )
            pid = process.pid
            master = process.stdout.fileno()
        own_argv = b""
        own_environment = b""
        for _ in range(100):
            try:
                proc = pathlib.Path(f"/proc/{pid}")
                candidate = (proc / "cmdline").read_bytes()
                if b"browser-auth-session.py" in candidate:
                    own_argv = candidate
                    own_environment = (proc / "environ").read_bytes()
                    break
            except OSError:
                pass
            time.sleep(0.01)
        terminal = bytearray()
        status = None
        reaped = False
        sent_input = False
        deadline = time.monotonic() + max(8, timeout_seconds + 5)
        ready_marker = b"Verified localhost callback forward is ready"
        expected_url = authorization_url(callback_port, state="Q" * 43).encode("ascii")
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.05)
                if ready:
                    try:
                        chunk = os.read(master, 16384)
                    except OSError as error:
                        if error.errno == errno.EIO:
                            chunk = b""
                        else:
                            raise
                    terminal.extend(chunk)
                if process is not None:
                    status = process.poll()
                    waited = pid if status is not None else 0
                else:
                    waited, raw_status = os.waitpid(pid, os.WNOHANG)
                    if waited == pid:
                        status = os.waitstatus_to_exitcode(raw_status)
                if waited == pid:
                    reaped = True
                    break
                if use_pty and mode == "success" and ready_marker in terminal and not sent_input:
                    with socket.create_connection(("127.0.0.1", callback_port), timeout=2) as client:
                        client.sendall(b"SYNTHETIC_CALLBACK_CANARY")
                        self.assertEqual(client.recv(1024), b"LOCAL_FAKE_TARGET_OK:SYNTHETIC_CALLBACK_CANARY")
                    os.write(master, b"exit\n")
                    sent_input = True
                elif use_pty and mode in {"cancel", "failure"} and ready_marker in terminal and not sent_input:
                    os.write(master, b"continue\n")
                    sent_input = True
            if occupied:
                with socket.create_connection(("127.0.0.1", callback_port), timeout=1):
                    pass
            if not reaped:
                if process is not None:
                    status = process.poll()
                    waited = pid if status is not None else 0
                else:
                    waited, raw_status = os.waitpid(pid, os.WNOHANG)
                    if waited == pid:
                        status = os.waitstatus_to_exitcode(raw_status)
                if waited == pid:
                    reaped = True
            if not reaped:
                if process is not None:
                    process.kill()
                    status = process.wait(timeout=2)
                else:
                    os.kill(pid, 9)
                    _, raw_status = os.waitpid(pid, 0)
                    status = os.waitstatus_to_exitcode(raw_status)
                self.fail("browser-auth process exceeded its outer test deadline")
        finally:
            if process is not None:
                process.stdout.close()
            else:
                os.close(master)
            if occupied_socket is not None:
                occupied_socket.close()
            target.shutdown()
            target.server_close()
            target_thread.join(timeout=2)
        self.assertIsNotNone(status)
        self.assertTrue(proof.exists(), "cleanup proof must exist after every terminal outcome")
        proof_data = __import__("json").loads(proof.read_text())
        self.assertEqual(proof_data["ssh_stopped"], True)
        self.assertEqual(proof_data["control_socket_absent"], True)
        self.assertFalse((run_dir / "auth-control.sock").exists())
        self.assertFalse((run_dir / "auth-ssh.identity").exists())
        if occupied:
            self.assertFalse(proof_data["cleanup_complete"])
        else:
            self.assertTrue(proof_data["listener_verified_closed"])
            self.assertTrue(proof_data["cleanup_complete"])
            closed_deadline = time.monotonic() + 2
            while not auth.listener_absent(callback_port) and time.monotonic() < closed_deadline:
                time.sleep(0.01)
            self.assertTrue(auth.listener_absent(callback_port))
        evidence_bytes = evidence.read_bytes()
        exposure_bytes = process_capture.read_bytes() if process_capture.exists() else b""
        self.assertNotIn(b"state=", evidence_bytes)
        self.assertNotIn(b"Q" * 43, evidence_bytes)
        self.assertNotIn(expected_url, evidence_bytes)
        self.assertTrue(own_argv and own_environment, "must inspect live browser-auth process argv and environment")
        self.assertNotIn(expected_url, own_argv + own_environment + exposure_bytes)
        self.assertNotIn(b"state=", own_argv + own_environment + exposure_bytes)
        temporary.cleanup()
        return status, bytes(terminal), evidence_bytes

    def test_synthetic_oauth_waits_for_forward_and_reaches_fake_callback_target(self):
        status, terminal, _evidence = self._run_session("success")
        self.assertEqual(status, 0, terminal.decode("utf-8", "replace"))
        self.assertIn(b"Verified localhost callback forward is ready", terminal)

    def test_non_tty_portal_failure_is_sanitized_and_cleans_owned_forward(self):
        status, terminal, _evidence = self._run_session("success", use_pty=False)
        self.assertEqual(status, 76)
        self.assertNotIn(b"https://auth.openai.com/", terminal)
        self.assertNotIn(b"state=", terminal)

    def test_cancel_provider_failure_and_timeout_are_nonzero_and_clean(self):
        cancel_status, _, _ = self._run_session("cancel")
        failure_status, _, _ = self._run_session("failure")
        timeout_status, _, _ = self._run_session("timeout", timeout_seconds=1)
        self.assertEqual(cancel_status, 130)
        self.assertEqual(failure_status, 23)
        self.assertEqual(timeout_status, 124)

    def test_invalid_url_and_callback_race_fail_closed_with_cleanup(self):
        invalid_status, _, _ = self._run_session("invalid", timeout_seconds=2)
        race_status, _, _ = self._run_session("race")
        self.assertNotEqual(invalid_status, 0)
        self.assertNotEqual(race_status, 0)

    def test_occupied_host_callback_port_is_not_taken_over(self):
        status, _, _ = self._run_session("success", occupied=True)
        self.assertNotEqual(status, 0)


class LocalOpenSSHForwardingTests(unittest.TestCase):
    """Real local sshd/client/socket lifecycle; no VM or external network."""

    @classmethod
    def setUpClass(cls):
        cls.ssh = shutil.which("ssh")
        cls.sshd = shutil.which("sshd")
        cls.ssh_keygen = shutil.which("ssh-keygen")
        cls.ssh_keyscan = shutil.which("ssh-keyscan")
        if not all((cls.ssh, cls.sshd, cls.ssh_keygen, cls.ssh_keyscan)):
            raise unittest.SkipTest("local OpenSSH server/client tools are unavailable")
        cls.temp = tempfile.TemporaryDirectory(prefix="mester-auth-sshd-")
        cls.root = pathlib.Path(cls.temp.name)
        cls.username = pwd.getpwuid(os.getuid()).pw_name
        cls.client_key = cls.root / "client-key"
        cls.host_key = cls.root / "host-key"
        cls.authorized_keys = cls.root / "authorized_keys"
        cls.known_hosts = cls.root / "known_hosts"
        cls.config = cls.root / "sshd_config"
        cls.pid_file = cls.root / "sshd.pid"
        cls.control_socket = cls.root / "control.sock"
        cls.ssh_port = free_port()
        subprocess.run(
            [cls.ssh_keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(cls.client_key)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        )
        subprocess.run(
            [cls.ssh_keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(cls.host_key)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        )
        cls.authorized_keys.write_text(cls.client_key.with_suffix(".pub").read_text())
        cls.authorized_keys.chmod(0o600)
        permit_root = "yes" if os.getuid() == 0 else "no"
        config_text = f"""Port {cls.ssh_port}
ListenAddress 127.0.0.1
HostKey {cls.host_key}
PidFile {cls.pid_file}
AuthorizedKeysFile {cls.authorized_keys}
StrictModes no
UsePAM no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin {permit_root}
AllowUsers {cls.username}
AllowTcpForwarding local
X11Forwarding no
PermitTunnel no
UseDNS no
LogLevel QUIET
"""
        cls.config.write_text(config_text)
        cls.config.chmod(0o600)
        checked = subprocess.run(
            [cls.sshd, "-t", "-f", str(cls.config)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
        )
        if checked.returncode != 0:
            raise unittest.SkipTest("local sshd cannot start in this environment")
        cls.sshd_process = subprocess.Popen(
            [cls.sshd, "-D", "-e", "-f", str(cls.config)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if cls.sshd_process.poll() is not None:
                raise unittest.SkipTest("temporary local sshd exited during startup")
            try:
                with socket.create_connection(("127.0.0.1", cls.ssh_port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.02)
        else:
            raise unittest.SkipTest("temporary local sshd did not become ready")
        scanned = subprocess.run(
            [cls.ssh_keyscan, "-T", "2", "-t", "ed25519", "-p", str(cls.ssh_port), "127.0.0.1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False,
        )
        if scanned.returncode != 0 or not scanned.stdout:
            raise unittest.SkipTest("local sshd host key scan was unavailable")
        cls.known_hosts.write_bytes(scanned.stdout)
        cls.known_hosts.chmod(0o600)

    @classmethod
    def tearDownClass(cls):
        process = getattr(cls, "sshd_process", None)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        temp = getattr(cls, "temp", None)
        if temp is not None:
            temp.cleanup()

    def setUp(self):
        self.target = ThreadingTCPServer(("127.0.0.1", 0), FakeTargetHandler)
        self.target.daemon_threads = True
        self.target_thread = threading.Thread(target=self.target.serve_forever, daemon=True)
        self.target_thread.start()
        self.target_port = self.target.server_address[1]
        # The local sshd and client share a network namespace. Use a distinct
        # fake target port only in this integration harness; production command
        # construction is separately asserted to use the exact same callback port.
        self.master = subprocess.Popen(
            [
                self.ssh, "-F", "/dev/null", "-M", "-N", "-S", str(self.control_socket),
                "-o", "ControlMaster=yes", "-o", "ControlPersist=no",
                "-o", "ExitOnForwardFailure=yes", "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=3", "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={self.known_hosts}",
                "-o", "GlobalKnownHostsFile=/dev/null", "-o", "IdentitiesOnly=yes",
                "-o", "IdentityAgent=none", "-o", "ProxyCommand=none", "-o", "ProxyJump=none",
                "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "ClearAllForwardings=yes",
                "-i", str(self.client_key), "-p", str(self.ssh_port), f"{self.username}@127.0.0.1",
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            check = subprocess.run(
                [self.ssh, "-F", "/dev/null", "-S", str(self.control_socket), "-O", "check",
                 "-p", str(self.ssh_port), f"{self.username}@127.0.0.1"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=1, check=False,
            )
            if check.returncode == 0:
                break
            if self.master.poll() is not None:
                self.fail("local SSH control master exited before becoming ready")
            time.sleep(0.02)
        else:
            self.fail("local SSH control master did not become ready")

    def tearDown(self):
        if getattr(self, "master", None) is not None and self.master.poll() is None:
            self.master.terminate()
            try:
                self.master.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.master.kill()
                self.master.wait(timeout=2)
        if getattr(self, "target", None) is not None:
            self.target.shutdown()
            self.target.server_close()
            self.target_thread.join(timeout=2)

    def _command(self, operation, callback_port, target_port=None):
        command = auth.control_socket_arguments(
            self.ssh, str(self.control_socket), operation, callback_port, self.ssh_port,
            str(self.client_key), str(self.known_hosts),
        )
        if target_port is not None:
            command[command.index("-L") + 1] = (
                f"127.0.0.1:{callback_port}:127.0.0.1:{target_port}"
            )
        return command

    def test_real_ssh_forward_reaches_local_fake_target_and_cancel_closes_listener(self):
        callback_port = free_port()
        self.assertEqual(auth.validate_authorization_url(authorization_url(1455)), 1455)
        forward = self._command("forward", callback_port, self.target_port)
        self.assertTrue(auth.run_control_command(forward))
        with socket.create_connection(("127.0.0.1", callback_port), timeout=2) as client:
            client.sendall(b"SYNTHETIC_CALLBACK_CANARY")
            self.assertEqual(client.recv(1024), b"LOCAL_FAKE_TARGET_OK:SYNTHETIC_CALLBACK_CANARY")
        self.assertTrue(auth.run_control_command(self._command("cancel", callback_port, self.target_port)))
        self.assertTrue(auth.listener_absent(callback_port))

    def test_forward_failure_does_not_take_over_an_occupied_listener(self):
        occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        callback_port = occupied.getsockname()[1]
        self.assertFalse(auth.listener_absent(callback_port))
        self.assertFalse(auth.run_control_command(self._command("forward", callback_port, self.target_port)))
        occupied.settimeout(1)
        probe = socket.create_connection(("127.0.0.1", callback_port), timeout=1)
        probe.close()
        occupied.close()

    def test_control_request_timeout_is_bounded_and_listener_remains_closed(self):
        control_path = self.root / "stalled-control.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(control_path))
        listener.listen(1)
        stop = threading.Event()

        def accept_without_reply():
            listener.settimeout(0.1)
            while not stop.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if stop.is_set():
                        return
                    raise
                connection.settimeout(0.1)
                while not stop.is_set():
                    try:
                        if not connection.recv(4096):
                            break
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                connection.close()

        thread = threading.Thread(target=accept_without_reply, daemon=True)
        thread.start()
        callback_port = free_port()
        command = auth.control_socket_arguments(
            self.ssh, str(control_path), "forward", callback_port, self.ssh_port,
            str(self.client_key), str(self.known_hosts),
        )
        started = time.monotonic()
        self.assertFalse(auth.run_control_command(command, timeout_seconds=0.2))
        self.assertLess(time.monotonic() - started, 2)
        stop.set()
        listener.close()
        thread.join(timeout=1)
        control_path.unlink(missing_ok=True)
        self.assertTrue(auth.listener_absent(callback_port))

    def test_master_exit_closes_any_owned_callback_listener(self):
        callback_port = free_port()
        self.assertTrue(auth.run_control_command(self._command("forward", callback_port, self.target_port)))
        self.master.terminate()
        self.master.wait(timeout=3)
        self.assertTrue(auth.listener_absent(callback_port))


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print("BROWSER_AUTH_TESTS_OK")
