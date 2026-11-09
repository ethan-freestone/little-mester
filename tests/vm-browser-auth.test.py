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
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse
from types import SimpleNamespace
from socketserver import BaseRequestHandler, ThreadingTCPServer


ROOT = pathlib.Path(__file__).parent.parent
SESSION_PATH = ROOT / "lib/vm/browser-auth-session.py"
SPEC = importlib.util.spec_from_file_location("mester_browser_auth", SESSION_PATH)
auth = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(auth)


def authorization_url(port=43210, *, state="S" * 43, nonce="N" * 43, challenge="C" * 43):
    query = {
        "client_id": "dynamic_agent_client",
        "agent_name_hint": "OpenCode",
        "ext_agent_host_id": "urn:uuid:12345678-1234-1234-1234-123456789abc",
        "response_type": "code",
        "redirect_uri": f"http://127.0.0.1:{port}/auth/callback",
        "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
        "resource": "https://api.openai.com/v1",
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    return "https://auth.openai.com/api/accounts/authorize?" + urllib.parse.urlencode(query)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


class AuthorizationValidationTests(unittest.TestCase):
    def test_pinned_open_code_authorize_shape_and_dynamic_callback_port(self):
        value = authorization_url(54321)
        self.assertEqual(auth.validate_authorization_url(value), 54321)
        command = auth.control_socket_arguments(
            "/usr/bin/ssh", "/tmp/private/auth.sock", "forward", 54321, 43210,
            "/tmp/private/id", "/tmp/private/known_hosts",
        )
        self.assertEqual(command.count("-L"), 1)
        self.assertEqual(command[command.index("-L") + 1], "127.0.0.1:54321:127.0.0.1:54321")
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
            valid.replace("/api/accounts/authorize", "/api/other", 1),
            valid.replace("redirect_uri=http%3A%2F%2F127.0.0.1%3A43210%2Fauth%2Fcallback",
                          "redirect_uri=http%3A%2F%2F127.0.0.1%3A43210%2Fauth%2Fcallback%3Fextra%3D1"),
            valid + "&redirect_uri=http%3A%2F%2F127.0.0.1%3A43210%2Fauth%2Fcallback",
            valid + "#fragment",
            valid.replace("state=" + "S" * 43, "state=forged"),
            valid.replace("nonce=" + "N" * 43, "nonce=forged"),
            valid.replace("code_challenge_method=S256", "code_challenge_method=plain"),
            valid.replace("code_challenge=" + "C" * 43, "code_challenge=short"),
            valid + "&code_verifier=synthetic-canary",
            valid.replace("http%3A%2F%2F127.0.0.1%3A43210", "http%3A%2F%2Flocalhost%3A43210"),
            valid.replace("43210", "1023"),
            valid.replace("43210", "65536"),
            valid.replace("43210", "043210"),
        ]
        for candidate in rejected:
            with self.subTest(candidate=candidate[:96]):
                with self.assertRaises(auth.AuthSessionError):
                    auth.validate_authorization_url(candidate)

    def test_rejects_duplicate_oauth_state_and_callback_secret_fields(self):
        valid = authorization_url()
        for candidate in (valid + "&state=" + "T" * 43, valid + "&code=synthetic-code-canary"):
            with self.subTest(candidate=candidate[-60:]):
                with self.assertRaises(auth.AuthSessionError):
                    auth.validate_authorization_url(candidate)

    def test_requires_exact_pinned_open_code_scope_and_auth_path_hides_openers(self):
        valid = authorization_url()
        with self.assertRaises(auth.AuthSessionError):
            auth.validate_authorization_url(valid.replace(
                "scope=openid+profile+email+offline_access+resource.invoke+chatgpt.tokens.use.direct",
                "scope=openid+profile+email+offline_access+resource.invoke+chatgpt.tokens.use.direct+admin",
            ))
        session = auth.BrowserAuthSession.__new__(auth.BrowserAuthSession)
        session.args = SimpleNamespace(guest_auth_dir="/tmp/mester-v3-auth-unit")
        remote = session._remote_command()
        self.assertIn("PATH=/tmp/mester-v3-auth-unit:/usr/local/bin", remote)
        self.assertIn("for opener in xdg-open gio sensible-browser firefox chromium chromium-browser", remote)
        self.assertNotIn("auth.openai.com", remote)

    def test_listener_absence_detects_ipv4_mapped_ipv6_callback_sockets(self):
        listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            listener.bind(("::ffff:127.0.0.1", 0))
            listener.listen(1)
            self.assertFalse(auth.listener_absent(listener.getsockname()[1]))
        finally:
            listener.close()

    def test_native_tui_url_scanner_holds_url_until_forward_and_refuses_races(self):
        first = authorization_url(43210)
        encoded = b"\x1b]8;;" + first.encode("ascii") + b"\x1b\\"
        scanner = auth.AuthorizationOutputScanner()
        prefix = b"TUI-prefix:" + b"x" * 64
        visible, port, held = scanner.feed(prefix + encoded[:19])
        self.assertTrue(visible.startswith(b"TUI-prefix:"))
        self.assertNotIn(first.encode("ascii"), visible)
        self.assertIsNone(port)
        self.assertEqual(held, b"")
        visible, port, held = scanner.feed(encoded[19:] + b"URL-visible-copy")
        self.assertEqual(port, 43210)
        self.assertNotIn(first.encode("ascii"), visible)
        self.assertIn(first.encode("ascii"), held)
        visible, port, held = scanner.feed(encoded)
        self.assertIsNone(port)  # An identical TUI redraw is harmless.
        self.assertIn(first.encode("ascii"), visible)
        self.assertEqual(held, b"")
        second = authorization_url(43211, state="T" * 43)
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
forwards = {}

class RelayHandler(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.create_connection(("127.0.0.1", target_port), timeout=2) as target:
            data = self.request.recv(4096)
            target.sendall(data)
            self.request.sendall(target.recv(4096))

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
    "client_id": "dynamic_agent_client",
    "agent_name_hint": "OpenCode",
    "ext_agent_host_id": "urn:uuid:12345678-1234-1234-1234-123456789abc",
    "response_type": "code",
    "redirect_uri": f"http://127.0.0.1:{callback_port}/auth/callback",
    "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
    "resource": "https://api.openai.com/v1",
    "state": "Q" * 43,
    "nonce": "N" * 43,
    "code_challenge_method": "S256",
    "code_challenge": "C" * 43,
}
url = "https://auth.openai.com/api/accounts/authorize?" + urllib.parse.urlencode(query)
if mode == "invalid":
    url = url.replace("auth.openai.com", "auth.openai.com.evil.invalid", 1)
payload = b"\x1b]8;;" + url.encode("ascii") + b"\x1b\\"
if mode == "race":
    raced = dict(query)
    raced["redirect_uri"] = f"http://127.0.0.1:{callback_port + 1}/auth/callback"
    raced["state"] = "R" * 43
    second = "https://auth.openai.com/api/accounts/authorize?" + urllib.parse.urlencode(raced)
    payload += b"\x1b]8;;" + second.encode("ascii") + b"\x1b\\"
os.write(1, payload)
if mode == "invalid":
    time.sleep(10)
else:
    answer = sys.stdin.readline()
    if mode == "cancel":
        raise SystemExit(130)
    if mode == "failure":
        raise SystemExit(23)
raise SystemExit(0)
'''


class BrowserAuthProcessTests(unittest.TestCase):
    def _run_session(self, mode, *, occupied=False, timeout_seconds=5):
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
        callback_port = free_port()
        occupied_socket = None
        if occupied:
            occupied_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            occupied_socket.bind(("127.0.0.1", callback_port))
            occupied_socket.listen(1)
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
        pid, master = pty.fork()
        if pid == 0:
            os.environ["AUTH_TEST_MODE"] = mode
            os.environ["AUTH_TEST_CALLBACK_PORT"] = str(callback_port)
            os.environ["AUTH_TEST_TARGET_PORT"] = str(target.server_address[1])
            os.environ["AUTH_TEST_CAPTURE"] = str(process_capture)
            evidence_fd = os.open(evidence, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.dup2(evidence_fd, 1)
            os.dup2(evidence_fd, 2)
            os.execv(command[0], command)
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
        ready_marker = b"Temporary loopback callback is ready."
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
                waited, raw_status = os.waitpid(pid, os.WNOHANG)
                if waited == pid:
                    status = os.waitstatus_to_exitcode(raw_status)
                    reaped = True
                    break
                if mode == "success" and ready_marker in terminal and expected_url in terminal and not sent_input:
                    self.assertGreater(terminal.find(expected_url), terminal.find(ready_marker))
                    with socket.create_connection(("127.0.0.1", callback_port), timeout=2) as client:
                        client.sendall(b"SYNTHETIC_CALLBACK_CANARY")
                        self.assertEqual(client.recv(1024), b"LOCAL_FAKE_TARGET_OK:SYNTHETIC_CALLBACK_CANARY")
                    os.write(master, b"exit\n")
                    sent_input = True
                elif mode in {"cancel", "failure"} and ready_marker in terminal and not sent_input:
                    os.write(master, b"continue\n")
                    sent_input = True
            if occupied:
                with socket.create_connection(("127.0.0.1", callback_port), timeout=1):
                    pass
            if not reaped:
                waited, raw_status = os.waitpid(pid, os.WNOHANG)
                if waited == pid:
                    status = os.waitstatus_to_exitcode(raw_status)
                    reaped = True
            if not reaped:
                os.kill(pid, 9)
                _, raw_status = os.waitpid(pid, 0)
                status = os.waitstatus_to_exitcode(raw_status)
                self.fail("browser-auth process exceeded its outer test deadline")
        finally:
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
            self.assertTrue(auth.listener_absent(callback_port))
        evidence_bytes = evidence.read_bytes()
        exposure_bytes = process_capture.read_bytes() if process_capture.exists() else b""
        self.assertNotIn(b"state=", evidence_bytes)
        self.assertNotIn(b"Q" * 43, evidence_bytes)
        self.assertNotIn(expected_url, evidence_bytes)
        self.assertTrue(own_argv and own_environment, "must inspect live browser-auth process argv and environment")
        self.assertNotIn(expected_url, own_argv + own_environment + exposure_bytes)
        self.assertNotIn(b"state=", own_argv + own_environment + exposure_bytes)
        self.assertNotIn(b"nonce=", own_argv + own_environment + exposure_bytes)
        if mode != "timeout" and not occupied and mode not in {"invalid", "race"}:
            self.assertIn(expected_url, terminal)
        temporary.cleanup()
        return status, bytes(terminal), evidence_bytes

    def test_success_shows_url_only_after_forward_and_reaches_fake_callback_target(self):
        status, terminal, _evidence = self._run_session("success")
        self.assertEqual(status, 0)
        self.assertIn(b"Temporary loopback callback is ready.", terminal)

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
        self.assertEqual(auth.validate_authorization_url(authorization_url(callback_port)), callback_port)
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
