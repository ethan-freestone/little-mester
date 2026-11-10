#!/usr/bin/env python3
"""Run one bounded ChatGPT browser-auth TUI with an exact loopback callback."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import pathlib
import re
import selectors
import shlex
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
import uuid
from urllib.parse import parse_qsl, urlsplit

MAX_AUTH_URL_LENGTH = 8192
MAX_AUTH_OUTPUT_BYTES = 65536
MAX_FIELDS = 32
AUTHORIZATION_URL_PREFIX = b"https://auth.openai.com/oauth/authorize?"
URL_CHARACTERS = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789:/?&=._~%+-")
SSH_STOP_GRACE_SECONDS = 3
FORWARD_COMMAND_TIMEOUT_SECONDS = 5
CONTROL_SOCKET_LIMIT = 100
SECRET_QUERY_KEYS = {"code", "access_token", "refresh_token", "id_token", "code_verifier"}
REQUIRED_QUERY_KEYS = {
    "client_id",
    "response_type",
    "redirect_uri",
    "scope",
    "state",
    "code_challenge_method",
    "code_challenge",
    "id_token_add_organizations",
    "codex_cli_simplified_flow",
    "originator",
}
OPTIONAL_QUERY_KEYS = set()
BASE64URL_43 = re.compile(r"[A-Za-z0-9_-]{43}\Z")
CLIENT_ID = re.compile(r"[A-Za-z0-9._-]{1,256}\Z")
PINNED_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


class AuthSessionError(Exception):
    """A deliberately sanitized browser-auth failure."""

    def __init__(self, message: str, status: int = 75):
        super().__init__(message)
        self.status = status


def _single_value_query(raw_query: str) -> dict[str, str]:
    try:
        fields = parse_qsl(
            raw_query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=MAX_FIELDS,
            encoding="utf-8",
            errors="strict",
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise AuthSessionError("authorization URL query is malformed") from error
    result = {}
    for key, value in fields:
        if key in result:
            raise AuthSessionError("authorization URL has duplicate query fields")
        result[key] = value
    return result


def validate_authorization_url(raw_url: str) -> int:
    """Validate pinned OpenCode 2.2.5 OAuth shape and return its callback port."""
    if not isinstance(raw_url, str) or not raw_url or len(raw_url) > MAX_AUTH_URL_LENGTH:
        raise AuthSessionError("authorization URL length is invalid")
    try:
        raw_url.encode("ascii")
        parsed = urlsplit(raw_url)
        parsed_port = parsed.port
    except (UnicodeEncodeError, ValueError) as error:
        raise AuthSessionError("authorization URL syntax is invalid") from error
    if (
        parsed.scheme != "https"
        or parsed.netloc != "auth.openai.com"
        or parsed.hostname != "auth.openai.com"
        or parsed_port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/oauth/authorize"
        or not parsed.query
        or parsed.fragment
        or any(ord(character) <= 0x20 or ord(character) >= 0x7F for character in raw_url)
    ):
        raise AuthSessionError("authorization URL endpoint is not approved")

    query = _single_value_query(parsed.query)
    keys = set(query)
    if keys.intersection(SECRET_QUERY_KEYS):
        raise AuthSessionError("authorization URL contains callback credentials")
    if not REQUIRED_QUERY_KEYS.issubset(keys) or keys - REQUIRED_QUERY_KEYS - OPTIONAL_QUERY_KEYS:
        raise AuthSessionError("authorization URL query shape is not approved")
    if query.get("response_type") != "code":
        raise AuthSessionError("authorization response type is not approved")
    if (query.get("scope") != "openid profile email offline_access"
            or query.get("id_token_add_organizations") != "true"
            or query.get("codex_cli_simplified_flow") != "true"
            or query.get("originator") != "opencode"):
        raise AuthSessionError("authorization parameters do not match pinned OpenCode 2.2.5")
    if query.get("client_id") != PINNED_CLIENT_ID or not CLIENT_ID.fullmatch(query.get("client_id", "")):
        raise AuthSessionError("authorization client identifier is invalid")
    if (
        not BASE64URL_43.fullmatch(query.get("state", ""))
        or query.get("code_challenge_method") != "S256"
        or not BASE64URL_43.fullmatch(query.get("code_challenge", ""))
    ):
        raise AuthSessionError("OAuth state or PKCE shape is invalid")

    redirect_values = [value for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key == "redirect_uri"]
    if len(redirect_values) != 1:
        raise AuthSessionError("authorization callback URI must appear exactly once")
    redirect_raw = redirect_values[0]
    try:
        redirect = urlsplit(redirect_raw)
        port = redirect.port
    except ValueError as error:
        raise AuthSessionError("authorization callback URI is invalid") from error
    expected_redirects = {
        "http://localhost:1455/auth/callback": 1455,
        "http://localhost:1457/auth/callback": 1457,
    }
    if redirect_raw not in expected_redirects:
        raise AuthSessionError("authorization callback URI is not an exact pinned localhost URI")
    if (
        redirect.hostname != "localhost"
        or redirect.username is not None
        or redirect.password is not None
        or redirect.path != "/auth/callback"
        or redirect.query
        or redirect.fragment
        or port != expected_redirects[redirect_raw]
    ):
        raise AuthSessionError("authorization callback URI is not an exact guest loopback URI")
    return expected_redirects[redirect_raw]


class AuthorizationOutputScanner:
    """Find one pinned CLI authorization URL and keep it out of retained output."""

    def __init__(self):
        self.pending = bytearray()
        self.auth_url: str | None = None
        self.callback_port: int | None = None
        self.fingerprint: bytes | None = None

    def feed(self, data: bytes) -> tuple[bytes, int | None, bytes]:
        self.pending.extend(data)
        visible = bytearray()
        held = bytearray()
        first_port = None
        while True:
            start = self.pending.find(AUTHORIZATION_URL_PREFIX)
            if start < 0:
                # Keep the possible OSC 8 opener too, so the forward-ready
                # message cannot be inserted in the middle of a hyperlink.
                keep = len(AUTHORIZATION_URL_PREFIX) - 1 + len(b"\x1b]8;;")
                if len(self.pending) > keep:
                    portion = len(self.pending) - keep
                    destination = held if first_port is not None else visible
                    destination.extend(self.pending[:portion])
                    del self.pending[:portion]
                return bytes(visible), first_port, bytes(held)
            url_start = start
            if start >= len(b"\x1b]8;;") and self.pending[start - 5 : start] == b"\x1b]8;;":
                start -= 5
            if start:
                destination = held if first_port is not None else visible
                destination.extend(self.pending[:start])
                del self.pending[:start]
                url_start -= start
            else:
                url_start -= start
            url_end = url_start + len(AUTHORIZATION_URL_PREFIX)
            while url_end < len(self.pending) and self.pending[url_end] in URL_CHARACTERS:
                url_end += 1
            if url_end == len(self.pending):
                if url_end > MAX_AUTH_URL_LENGTH:
                    raise AuthSessionError("authorization URL exceeded its fixed bound")
                return bytes(visible), first_port, bytes(held)
            if url_end > MAX_AUTH_URL_LENGTH:
                raise AuthSessionError("authorization URL exceeded its fixed bound")
            if self.pending[url_end] not in b"\x1b\r\n\t ":
                raise AuthSessionError("authorization URL has an invalid trailing character")
            raw = bytes(self.pending[url_start:url_end])
            # Keep the first non-URL byte (usually OSC 8's ESC or a terminal
            # newline) with the held stream so the TUI hyperlink remains intact.
            release_end = url_end + 1
            if self.pending[url_end] == 0x1B and len(self.pending) > url_end + 1 and self.pending[url_end + 1] == ord("\\"):
                release_end += 1
            raw_with_delimiter = bytes(self.pending[:release_end])
            del self.pending[:release_end]
            try:
                url = raw.decode("ascii")
            except UnicodeDecodeError as error:
                raise AuthSessionError("authorization URL is not ASCII") from error
            port = validate_authorization_url(url)
            fingerprint = hashlib.sha256(raw).digest()
            if self.auth_url is not None:
                if fingerprint != self.fingerprint:
                    raise AuthSessionError("a second or changed authorization attempt was refused")
                destination = held if first_port is not None else visible
                destination.extend(raw_with_delimiter)
                continue
            self.auth_url = url
            self.callback_port = port
            self.fingerprint = fingerprint
            first_port = port
            held.extend(raw_with_delimiter)


def control_socket_arguments(
    ssh: str,
    control_path: str,
    operation: str,
    callback_port: int,
    ssh_port: int,
    identity_file: str,
    known_hosts: str,
) -> list[str]:
    """Build the only permitted forwarding operation: one literal-loopback -L."""
    if operation not in {"forward", "cancel"}:
        raise AuthSessionError("unsupported SSH control operation")
    if not isinstance(callback_port, int) or not 1024 <= callback_port <= 65535:
        raise AuthSessionError("callback port is outside the approved range")
    if not isinstance(ssh_port, int) or not 1024 <= ssh_port <= 65535:
        raise AuthSessionError("SSH control port is invalid")
    if not os.path.isabs(control_path) or len(control_path) >= CONTROL_SOCKET_LIMIT:
        raise AuthSessionError("SSH control socket path is invalid")
    # The pinned provider uses server.listen(port, "localhost") in the guest.
    # The host-side listener remains explicitly IPv4-loopback-only.
    forward = f"127.0.0.1:{callback_port}:localhost:{callback_port}"
    return [
        ssh,
        "-F",
        "/dev/null",
        "-S",
        control_path,
        "-O",
        operation,
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ForwardX11=no",
        "-o",
        "ProxyCommand=none",
        "-o",
        "ProxyJump=none",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "LogLevel=ERROR",
        "-L",
        forward,
        "-i",
        identity_file,
        "-p",
        str(ssh_port),
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "mester@127.0.0.1",
    ]


def run_control_command(command: list[str], timeout_seconds: float = FORWARD_COMMAND_TIMEOUT_SECONDS) -> bool:
    """Run a short OpenSSH control request with a hard deadline and no output capture."""
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
            check=False,
            close_fds=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _private_json(path: pathlib.Path, value: dict) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _record_process_identity(path: pathlib.Path, process: subprocess.Popen) -> None:
    try:
        proc = pathlib.Path(f"/proc/{process.pid}")
        actual_exe = os.path.realpath(proc / "exe")
        argv = [os.fsdecode(item) for item in (proc / "cmdline").read_bytes().split(b"\0") if item]
        fields = (proc / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
        start_time = fields[19]
    except (OSError, IndexError) as error:
        raise AuthSessionError("could not record the owned SSH process") from error
    _private_json(
        path,
        {"pid": process.pid, "start_time": start_time, "exe": actual_exe, "argv": argv},
    )


def _write_port(path: pathlib.Path, port: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        stream.write(f"{port}\n")
        stream.flush()
        os.fsync(stream.fileno())


def listener_absent(port: int) -> bool:
    """Prove no TCP listener can accept the exact host-loopback port."""
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        return False
    try:
        expected_loopback = struct.unpack("=I", socket.inet_aton("127.0.0.1"))[0]

        def ipv6_accepts_ipv4(local_address: str) -> bool:
            address = b"".join(
                bytes.fromhex(local_address[index : index + 8])[::-1]
                for index in range(0, 32, 8)
            )
            if address == b"\0" * 16:
                return True
            if address == b"\0" * 15 + b"\1":
                return True
            return address[:12] == b"\0" * 10 + b"\xff\xff" and address[12:] in {
                b"\0" * 4,
                socket.inet_aton("127.0.0.1"),
            }

        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            rows = pathlib.Path(table).read_text(encoding="ascii").splitlines()[1:]
            for row in rows:
                fields = row.split()
                if len(fields) < 4 or fields[3] != "0A":
                    continue
                local_address, local_port = fields[1].split(":", 1)
                if int(local_port, 16) != port:
                    continue
                if table.endswith("tcp"):
                    address = int(local_address, 16)
                    if address in {0, expected_loopback}:
                        return False
                elif ipv6_accepts_ipv4(local_address):
                    # IPv6 wildcard or IPv4-mapped loopback can accept IPv4 too.
                    return False
        return True
    except (OSError, UnicodeError, ValueError, IndexError):
        return False


class BrowserAuthSession:
    """Supervise one interactive OpenCode session and its single callback forward."""

    def __init__(self, args):
        self.args = args
        self.ssh_process: subprocess.Popen | None = None
        self.tty_fd: int | None = None
        self.output_selector = selectors.DefaultSelector()
        self.scanner = AuthorizationOutputScanner()
        self.callback_port: int | None = None
        self.forward_attempted = False
        self.forward_installed = False
        self.listener_was_available = False
        self.listener_verified_closed = True
        self.ipv6_guard: socket.socket | None = None
        self.deadline: float | None = None
        self.auth_process_completed = False
        self.output_bytes = 0
        self.signal_received: int | None = None
        self.status = 1
        self.failure: str | None = None
        self.identity_path = pathlib.Path(args.ssh_identity)
        self.port_path = pathlib.Path(args.callback_port_file)
        self.socket_path = pathlib.Path(args.control_socket)
        self.proof_path = pathlib.Path(args.proof_file)
        self.run_dir = pathlib.Path(args.run_dir)

    def _signal(self, signum, _frame):
        self.signal_received = signum

    def _optional_tty(self):
        try:
            self.tty_fd = os.dup(0)
        except OSError:
            self.tty_fd = None
        if self.tty_fd is not None and not os.isatty(self.tty_fd):
            os.close(self.tty_fd)
            self.tty_fd = None
        if self.tty_fd is None:
            try:
                self.tty_fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
            except OSError:
                self.tty_fd = None

    def _remote_command(self) -> str:
        directory = self.args.guest_auth_dir
        if not re.fullmatch(r"/tmp/mester-v3-auth-[A-Za-z0-9-]{1,80}", directory):
            raise AuthSessionError("temporary guest browser path is invalid")
        values = {
            "auth_dir": directory,
            "path": directory + ":/usr/local/bin",
        }
        return (
            "set -eu; "
            f"auth_dir={shlex.quote(values['auth_dir'])}; "
            "cleanup(){ /bin/rm -rf -- \"$auth_dir\"; }; "
            "trap cleanup EXIT; trap 'exit 130' INT; trap 'exit 143' HUP TERM; "
            f"PATH={shlex.quote(values['path'])}; export PATH; "
            "for opener in xdg-open gio sensible-browser firefox chromium chromium-browser; do "
            "command -v \"$opener\" >/dev/null 2>&1 && exit 126; done; "
            "cd /workspace; "
            "/usr/bin/env -i "
            "HOME=/home/mester "
            f"PATH={shlex.quote(values['path'])} "
            "TERM=xterm-256color LANG=C.UTF-8 "
            "XDG_DATA_HOME=/home/mester/.local/share "
            "XDG_CONFIG_HOME=/home/mester/.config "
            "HTTP_PROXY=http://10.0.2.100:3128 HTTPS_PROXY=http://10.0.2.100:3128 "
            "http_proxy=http://10.0.2.100:3128 https_proxy=http://10.0.2.100:3128 "
            "NO_PROXY=localhost,127.0.0.1,10.0.2.100 no_proxy=localhost,127.0.0.1,10.0.2.100 "
            "/usr/local/bin/opencode auth login openai --method chatgpt-browser"
        )

    def _ssh_command(self) -> list[str]:
        if len(str(self.socket_path)) >= CONTROL_SOCKET_LIMIT:
            raise AuthSessionError("SSH control socket path is too long")
        if self.socket_path.exists() or self.socket_path.is_symlink():
            raise AuthSessionError("SSH control socket path already exists")
        return [
            self.args.ssh,
            "-F",
            "/dev/null",
            "-M",
            "-S",
            str(self.socket_path),
            "-T",
            "-o",
            "ControlMaster=yes",
            "-o",
            "ControlPersist=no",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self.args.known_hosts}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "IdentityAgent=none",
            "-o",
            "ProxyCommand=none",
            "-o",
            "ProxyJump=none",
            "-o",
            "ForwardAgent=no",
            "-o",
            "ForwardX11=no",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "PermitLocalCommand=no",
            "-o",
            "LogLevel=ERROR",
            "-i",
            self.args.identity_file,
            "-p",
            str(self.args.ssh_port),
            "mester@127.0.0.1",
            self._remote_command(),
        ]

    def _write_tty(self, data: bytes) -> None:
        if self.tty_fd is None:
            return
        view = memoryview(data)
        while view:
            try:
                written = os.write(self.tty_fd, view)
            except InterruptedError:
                continue
            if written <= 0:
                raise AuthSessionError("interactive terminal output failed")
            view = view[written:]

    def _install_forward(self, port: int) -> None:
        if self.forward_attempted:
            raise AuthSessionError("only one callback forward is permitted")
        self.callback_port = port
        self.listener_verified_closed = False
        # Reserve the browser's other localhost family before the IPv4 SSH
        # forward is installed. This non-listening guard forces fast IPv6
        # refusal so clients can retry the approved IPv4 callback path.
        self._reserve_ipv6_localhost(port)
        if not listener_absent(port):
            raise AuthSessionError("the exact host-loopback callback port is already occupied", 77)
        self.listener_was_available = True
        self.forward_attempted = True
        self.callback_port = port
        command = control_socket_arguments(
            self.args.ssh,
            str(self.socket_path),
            "forward",
            port,
            self.args.ssh_port,
            self.args.identity_file,
            self.args.known_hosts,
        )
        if not run_control_command(command):
            raise AuthSessionError("exact loopback callback forward could not be created", 77)
        self.forward_installed = True
        self.listener_verified_closed = False
        if not self._loopback_forward_ready(port):
            raise AuthSessionError("verified localhost callback forward did not become ready", 77)
        _write_port(self.port_path, port)
        self._write_tty(b"\r\nVerified localhost callback forward is ready; opening the owner browser.\r\n")

    def _loopback_forward_ready(self, port: int) -> bool:
        try:
            addresses = socket.getaddrinfo("localhost", port, type=socket.SOCK_STREAM)
            if not addresses or any(
                not ipaddress.ip_address(item[4][0].split("%", 1)[0]).is_loopback
                for item in addresses
            ):
                return False
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
            return not listener_absent(port) and self._owned_ssh_process()
        except (OSError, ValueError):
            return False

    def _reserve_ipv6_localhost(self, port: int) -> None:
        try:
            addresses = socket.getaddrinfo("localhost", port, type=socket.SOCK_STREAM)
        except OSError as error:
            raise AuthSessionError("localhost resolution could not be verified", 77) from error
        if not any(
            family == socket.AF_INET6
            and ipaddress.ip_address(address[0].split("%", 1)[0]) == ipaddress.IPv6Address("::1")
            for family, _kind, _protocol, _canonical, address in addresses
        ):
            return
        guard = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            guard.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            guard.bind(("::1", port))
        except OSError as error:
            guard.close()
            raise AuthSessionError("IPv6 localhost callback collision could not be excluded", 77) from error
        self.ipv6_guard = guard

    def _owned_ssh_process(self) -> bool:
        process = self.ssh_process
        if process is None or process.poll() is not None:
            return False
        try:
            proc = pathlib.Path(f"/proc/{process.pid}")
            fields = (proc / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
            identity = json.loads(self.identity_path.read_text(encoding="utf-8"))
            return (
                int(identity["pid"]) == process.pid
                and identity["start_time"] == fields[19]
                and identity["exe"] == os.path.realpath(proc / "exe")
            )
        except (OSError, ValueError, KeyError, IndexError):
            return False

    def _check_auth_abort(self, deadline: float | None = None) -> bool:
        if self.signal_received is not None:
            raise AuthSessionError(
                "interactive browser authentication was cancelled", 128 + self.signal_received
            )
        auth_deadline = deadline if deadline is not None else self.deadline
        if auth_deadline is not None and time.monotonic() >= auth_deadline:
            raise AuthSessionError("browser authentication exceeded its fixed wait period", 124)
        ssh_process = getattr(self, "ssh_process", None)
        if ssh_process is not None and ssh_process.poll() is not None:
            self.auth_process_completed = True
            return True
        return False

    def _assert_auth_live(self, deadline: float | None = None) -> None:
        if self._check_auth_abort(deadline):
            return
        ready = self.callback_port is not None and self._loopback_forward_ready(self.callback_port)
        # The readiness probe can block while signals or process completion
        # arrive; recheck after it and immediately before portal/fallback use.
        if self._check_auth_abort(deadline):
            return
        if not ready:
            raise AuthSessionError("browser opening refused because the callback forward is no longer verified", 77)

    def _open_portal(self, url: str, deadline: float | None = None) -> bool:
        """Ask the invoking owner's desktop portal to open one validated URL."""
        self._assert_auth_live(deadline)
        if self.auth_process_completed:
            return False
        try:
            import dbus
            from dbus.mainloop.glib import DBusGMainLoop
            from gi.repository import GLib

            DBusGMainLoop(set_as_default=True)
            bus = dbus.SessionBus()
            portal = bus.get_object("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop")
            interface = dbus.Interface(portal, "org.freedesktop.portal.OpenURI")
            token = "mester_auth_" + uuid.uuid4().hex
            response = {"code": None}
            loop = GLib.MainLoop()

            def on_response(code, _results):
                response["code"] = int(code)
                loop.quit()

            unique_name = bus.get_unique_name()
            if not unique_name.startswith(":"):
                return False
            sender = unique_name[1:].replace(".", "_")
            expected_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
            receiver = bus.add_signal_receiver(
                on_response,
                signal_name="Response",
                dbus_interface="org.freedesktop.portal.Request",
                path=expected_path,
            )
            request_path = expected_path
            open_attempted = False
            abort = {"error": None}

            def monitor_session():
                try:
                    self._assert_auth_live(deadline)
                except AuthSessionError as error:
                    abort["error"] = error
                    loop.quit()
                    return False
                return True

            def portal_timeout():
                loop.quit()
                return False

            def close_pending_request():
                if not open_attempted or response["code"] is not None:
                    return
                try:
                    request = bus.get_object("org.freedesktop.portal.Desktop", request_path)
                    dbus.Interface(request, "org.freedesktop.portal.Request").Close(timeout=2)
                except Exception as error:
                    raise AuthSessionError("pending desktop portal request could not be closed", 77) from error

            try:
                self._assert_auth_live(deadline)
                if self.auth_process_completed:
                    return False
                open_attempted = True
                call_timeout = 5
                auth_deadline = deadline if deadline is not None else self.deadline
                if auth_deadline is not None:
                    remaining = auth_deadline - time.monotonic()
                    if remaining <= 0:
                        self._assert_auth_live(deadline)
                    call_timeout = min(call_timeout, max(0.001, remaining))
                returned_path = interface.OpenURI("", url, {"handle_token": token}, timeout=call_timeout)
                request_path = str(returned_path)
                self._assert_auth_live(deadline)
                if self.auth_process_completed:
                    return False
                if request_path != expected_path:
                    raise AuthSessionError("desktop portal returned an unexpected request handle", 77)
                if response["code"] is None:
                    timeout_ms = 15000
                    if auth_deadline is not None:
                        timeout_ms = min(timeout_ms, max(1, int((auth_deadline - time.monotonic()) * 1000)))
                    session_source = GLib.timeout_add(100, monitor_session)
                    timeout_source = GLib.timeout_add(timeout_ms, portal_timeout)
                    try:
                        loop.run()
                    finally:
                        GLib.source_remove(session_source)
                        GLib.source_remove(timeout_source)
                if abort["error"] is not None:
                    raise abort["error"]
                if response["code"] is None:
                    return False
            finally:
                try:
                    receiver.remove()
                finally:
                    close_pending_request()
            return response["code"] == 0
        except AuthSessionError:
            raise
        except Exception:
            return False

    def _portal_or_manual(self, url: str, deadline: float | None = None) -> None:
        opened = self._open_portal(url, deadline)
        if getattr(self, "auth_process_completed", False):
            return
        if opened:
            self._assert_auth_live(deadline)
            if getattr(self, "auth_process_completed", False):
                return
            self._write_tty(
                b"Owner browser opened through the desktop portal. Complete ChatGPT consent; this stage continues automatically.\r\n"
            )
            return
        if self.tty_fd is not None:
            self._assert_auth_live(deadline)
            if getattr(self, "auth_process_completed", False):
                return
            fallback = (
                "\r\nDesktop browser portal unavailable or declined; manual fallback: open this validated URL in the host browser:\r\n"
                + url + "\r\n\r\n"
            )
            self._write_tty(fallback.encode("ascii"))
        else:
            self.status = 76
            raise AuthSessionError(
                "desktop browser portal unavailable and no interactive manual fallback is available", 76
            )

    def _start_ssh(self) -> None:
        command = self._ssh_command()
        self.ssh_process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            close_fds=True,
            bufsize=0,
        )
        _record_process_identity(self.identity_path, self.ssh_process)
        if self.ssh_process.stdout is None:
            raise AuthSessionError("interactive SSH output is unavailable")
        os.set_blocking(self.ssh_process.stdout.fileno(), False)
        self.output_selector.register(self.ssh_process.stdout, selectors.EVENT_READ)

    def _run_ui(self) -> None:
        assert self.ssh_process is not None and self.ssh_process.stdout is not None
        deadline = time.monotonic() + self.args.timeout_seconds
        self.deadline = deadline
        output_fd = self.ssh_process.stdout.fileno()
        while True:
            if self.signal_received is not None:
                self.status = 128 + self.signal_received
                self.failure = "interactive browser authentication was cancelled"
                return
            if time.monotonic() >= deadline:
                self.status = 124
                self.failure = "browser authentication exceeded its fixed wait period"
                return
            if self.ssh_process.poll() is not None and not self.output_selector.get_map():
                self.status = self.ssh_process.returncode
                break
            wait = min(0.1, max(0.0, deadline - time.monotonic()))
            for key, _ in self.output_selector.select(wait):
                try:
                    chunk = os.read(key.fd, 16384)
                except BlockingIOError:
                    continue
                if not chunk:
                    self.output_selector.unregister(key.fileobj)
                    continue
                self.output_bytes += len(chunk)
                if self.output_bytes > self.args.output_limit:
                    self.status = 125
                    self.failure = "interactive guest output exceeded its fixed byte limit"
                    return
                _visible, port, _held = self.scanner.feed(chunk)
                if port is not None:
                    self._install_forward(port)
                    if not self._owned_ssh_process():
                        raise AuthSessionError("browser opening refused because SSH ownership is unverified", 77)
                    self._portal_or_manual(self.scanner.auth_url, deadline)
        if self.scanner.auth_url is None:
            self.status = self.status or 1
            self.failure = "OpenCode did not request the approved ChatGPT browser flow"
            self.status = 75

    def _control(self, operation: str, port: int) -> bool:
        command = control_socket_arguments(
            self.args.ssh,
            str(self.socket_path),
            operation,
            port,
            self.args.ssh_port,
            self.args.identity_file,
            self.args.known_hosts,
        )
        return run_control_command(command)

    def _stop_ssh(self) -> bool:
        process = self.ssh_process
        if process is None:
            return True
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=SSH_STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=SSH_STOP_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    return False
        try:
            process.wait(timeout=0)
        except subprocess.TimeoutExpired:
            return False
        if self.ssh_process.stdout is not None:
            try:
                self.output_selector.unregister(self.ssh_process.stdout)
            except (KeyError, ValueError):
                pass
            self.ssh_process.stdout.close()
        return True

    def _cleanup(self) -> bool:
        clean = True
        if self.forward_attempted and self.callback_port is not None and self.ssh_process is not None:
            if self.ssh_process.poll() is None:
                # A cancel failure is tolerated only if the master is then stopped
                # and the exact host port is independently proven closed.
                self._control("cancel", self.callback_port)
        if not self._stop_ssh():
            clean = False
        if self.callback_port is not None:
            if self.forward_installed or (self.forward_attempted and self.listener_was_available):
                self.listener_verified_closed = listener_absent(self.callback_port)
                if not self.listener_verified_closed:
                    clean = False
            elif not self.listener_was_available:
                self.listener_verified_closed = False
                clean = False
            elif self.ssh_process is None or self.ssh_process.poll() is not None:
                # ExitOnForwardFailure means an unsuccessful install never owns
                # a listener; the control master is now absent as an extra check.
                self.listener_verified_closed = True
        if self.socket_path.exists() or self.socket_path.is_symlink():
            try:
                info = os.lstat(self.socket_path)
                if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid():
                    self.socket_path.unlink()
                else:
                    clean = False
            except OSError:
                clean = False
        if self.socket_path.exists() or self.socket_path.is_symlink():
            clean = False
        if self.ipv6_guard is not None:
            try:
                self.ipv6_guard.close()
                self.ipv6_guard = None
            except OSError:
                clean = False
        if self.identity_path.exists() and (self.ssh_process is None or self.ssh_process.poll() is not None):
            try:
                self.identity_path.unlink()
            except OSError:
                clean = False
        return clean

    def _write_proof(self, clean: bool) -> None:
        proof = {
            "version": 1,
            "ssh_stopped": bool(self.ssh_process is None or self.ssh_process.poll() is not None),
            "control_socket_absent": not self.socket_path.exists() and not self.socket_path.is_symlink(),
            "listener_verified_closed": self.listener_verified_closed,
            "cleanup_complete": clean,
        }
        _private_json(self.proof_path, proof)

    def run(self) -> int:
        old_handlers = {}
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            old_handlers[signum] = signal.signal(signum, self._signal)
        clean = False
        try:
            self._optional_tty()
            self._write_tty(b"Starting pinned OpenCode 2.2.5 ChatGPT browser authorization.\r\n")
            self._start_ssh()
            self._run_ui()
        except AuthSessionError as error:
            self.failure = str(error)
            self.status = error.status
        except (OSError, ValueError, subprocess.SubprocessError):
            self.failure = "browser authentication failed safely"
            self.status = 1
        finally:
            clean = self._cleanup()
            if self.tty_fd is not None:
                os.close(self.tty_fd)
                self.tty_fd = None
            self.output_selector.close()
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)
            try:
                self._write_proof(clean)
            except OSError:
                clean = False
        if not clean:
            self.status = self.status or 1
            self.failure = "browser authentication cleanup could not be proved"
        if self.failure:
            print(self.failure, file=sys.stderr)
        elif self.status == 0:
            print("BROWSER_AUTH_COMPLETE")
        return int(self.status or 0)


def _arguments(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--ssh", required=True)
    parser.add_argument("--ssh-port", required=True, type=int)
    parser.add_argument("--identity-file", required=True)
    parser.add_argument("--known-hosts", required=True)
    parser.add_argument("--control-socket", required=True)
    parser.add_argument("--ssh-identity", required=True)
    parser.add_argument("--callback-port-file", required=True)
    parser.add_argument("--proof-file", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--guest-auth-dir", required=True)
    parser.add_argument("--timeout-seconds", required=True, type=int)
    parser.add_argument("--output-limit", required=True, type=int)
    args = parser.parse_args(argv)
    if not 1024 <= args.ssh_port <= 65535 or not 1 <= args.timeout_seconds <= 1200:
        parser.error("SSH port or auth deadline is outside its fixed bounds")
    if not 1024 <= args.output_limit <= MAX_AUTH_OUTPUT_BYTES:
        parser.error("auth output limit is outside its fixed bound")
    run_dir = pathlib.Path(args.run_dir)
    if not run_dir.is_dir() or run_dir.is_symlink() or run_dir.stat().st_uid != os.getuid():
        parser.error("private run directory is invalid")
    for name in ("identity_file", "known_hosts", "control_socket", "ssh_identity", "callback_port_file", "proof_file"):
        value = pathlib.Path(getattr(args, name))
        if not value.is_absolute() or value.parent != run_dir:
            parser.error("auth state path is outside the private run directory")
    return args


def main(argv=None):
    try:
        args = _arguments(sys.argv[1:] if argv is None else argv)
        return BrowserAuthSession(args).run()
    except SystemExit:
        raise
    except Exception:
        print("browser authentication failed safely", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
