#!/usr/bin/env python3
"""VM HTTP(S) egress proxy. Loopback listener; public destinations only.

The selected workspace manifest is loaded at startup and can be safely reloaded
with SIGHUP without rebinding the listener. No shell evaluation. CONNECT is
restricted to 443; plain HTTP to 80. Local model APIs use separate QEMU
guestfwd channels; public HTTP(S) uses this proxy, which denies non-public and
host-local/directly connected destinations.
"""
import argparse
import http.client
import ipaddress
import json
import os
import re
import select
import signal
import socket
import stat
import sys
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import urlsplit

_NAT64_WELL_KNOWN_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")


def policy(path, *, missing_default=True):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        if missing_default:
            return True, set()
        raise
    with os.fdopen(fd, "rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("egress policy exceeds its size limit")
    settings = {}
    for line in raw.decode("utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError("malformed egress policy")
        key, value = (item.strip() for item in line.split("=", 1))
        value = value.strip("\"'")
        if key not in {"permission", "allowed_domains", "intent"} or key in settings:
            raise ValueError("malformed egress policy")
        settings[key] = value
    permission = settings.get("permission", "true")
    if permission not in {"true", "false"}:
        raise ValueError("malformed egress permission")
    enabled = permission == "true"
    domains = {
        _normalize_policy_host(item.strip())
        for item in settings.get("allowed_domains", "").split(",")
        if item.strip()
    }
    return enabled, domains


def _normalize_policy_host(value):
    exact = value.startswith("=")
    host = value[1:] if exact else value
    host = host.rstrip(".").lower()
    if not host or len(host) > 253 or any(ord(character) > 127 for character in host):
        raise ValueError("malformed egress domain")
    try:
        normalized = str(ipaddress.ip_address(host))
    except ValueError:
        labels = host.split(".")
        if any(
            not label
            or len(label) > 63
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
            for label in labels
        ):
            raise ValueError("malformed egress domain") from None
        normalized = host
    return f"={normalized}" if exact else normalized


def allowed(host, enabled, domains):
    if not enabled:
        return False
    try:
        host = _normalize_policy_host(host)
    except ValueError:
        return False
    host = host.removeprefix("=")
    if not domains:
        return True
    for domain in domains:
        if domain.startswith("="):
            if host == domain[1:]:
                return True
        elif host == domain or host.endswith("." + domain):
            return True
    return False


class PolicyStore:
    """Thread-safe snapshot of the current workspace egress policy."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._policy = policy(path)

    def allows(self, host):
        with self._lock:
            current = self._policy
        return allowed(host, *current)

    def reload(self):
        # Parse first; a failed read must leave the current policy in place.
        current = policy(self.path, missing_default=False)
        with self._lock:
            self._policy = current


def _policy_address(address):
    """Normalize IPv4-embedding IPv6 forms before applying destination policy."""
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
        elif address.sixtofour is not None or address.teredo is not None:
            return None
        elif address in _NAT64_WELL_KNOWN_PREFIX:
            return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return address


def _is_public_unicast(address):
    # Some Python versions do not consistently inherit IPv4 classification
    # for IPv4-mapped IPv6 addresses, so classify their embedded IPv4 value.
    address = _policy_address(address)
    return address is not None and address.is_global and not address.is_multicast


def _host_networks_from_proc(route_text, ipv6_route_text, ipv6_addresses_text, fib_trie_text):
    """Return host-local and directly connected networks from Linux proc tables."""
    networks = set()

    route_lines = route_text.splitlines()
    if not route_lines or not {"Iface", "Destination", "Gateway", "Flags", "Mask"}.issubset(
        set(route_lines[0].split())
    ):
        raise PermissionError("could not parse host IPv4 route header")
    for line in route_lines[1:]:
        fields = line.split()
        if not fields:
            continue
        if len(fields) < 8:
            raise PermissionError("could not parse host IPv4 route row")
        try:
            destination = ipaddress.IPv4Address(bytes.fromhex(fields[1])[::-1])
            gateway = ipaddress.IPv4Address(bytes.fromhex(fields[2])[::-1])
            flags = int(fields[3], 16)
            netmask = ipaddress.IPv4Address(bytes.fromhex(fields[7])[::-1])
            network = ipaddress.IPv4Network((destination, str(netmask)), strict=False)
        except ValueError as error:
            raise PermissionError("could not parse host IPv4 routes") from error
        # Do not block the default route. Directly connected routes have no
        # gateway; private ranges are independently rejected by is_global.
        if network.prefixlen and gateway.is_unspecified and not flags & 0x2:
            networks.add(network)

    for line in ipv6_route_text.splitlines():
        fields = line.split()
        if not fields:
            continue
        if len(fields) < 10:
            raise PermissionError("could not parse host IPv6 route row")
        try:
            destination = ipaddress.IPv6Address(bytes.fromhex(fields[0]))
            prefix = int(fields[1], 16)
            gateway = ipaddress.IPv6Address(bytes.fromhex(fields[4]))
            network = ipaddress.IPv6Network((destination, prefix), strict=False)
        except ValueError as error:
            raise PermissionError("could not parse host IPv6 routes") from error
        if prefix and gateway.is_unspecified:
            networks.add(network)

    for address, prefix in _ipv6_interface_records(ipv6_addresses_text):
        network = ipaddress.IPv6Network((address, prefix), strict=False)
        if prefix:
            networks.add(network)

    for address in _ipv4_host_addresses_from_fib_trie(fib_trie_text):
        networks.add(ipaddress.IPv4Network((address, 32)))

    if not networks:
        raise PermissionError("could not determine host-local networks")
    return frozenset(networks)


def _ipv6_interface_records(ipv6_addresses_text):
    records = set()
    for line in ipv6_addresses_text.splitlines():
        fields = line.split()
        if not fields:
            continue
        if len(fields) < 6:
            raise PermissionError("could not parse host IPv6 address row")
        try:
            address = ipaddress.IPv6Address(bytes.fromhex(fields[0]))
            prefix = int(fields[2], 16)
            ipaddress.IPv6Network((address, prefix), strict=False)
        except ValueError as error:
            raise PermissionError("could not parse host IPv6 addresses") from error
        records.add((address, prefix))
    return frozenset(records)


def _ipv4_host_addresses_from_fib_trie(fib_trie_text):
    addresses = set()
    trie_lines = fib_trie_text.splitlines()
    found_local_ipv4 = False
    for index, line in enumerate(trie_lines):
        if "host LOCAL" not in line:
            continue
        found_local_ipv4 = True
        candidates = [line]
        if index:
            candidates.append(trie_lines[index - 1])
        found_address = False
        for candidate in candidates:
            for token in candidate.split():
                try:
                    address = ipaddress.IPv4Address(token)
                except ValueError:
                    continue
                addresses.add(address)
                found_address = True
        if not found_address:
            raise PermissionError("could not parse host-local IPv4 address")

    if not found_local_ipv4:
        raise PermissionError("could not determine host-local IPv4 addresses")
    return frozenset(addresses)


def host_local_addresses():
    """Return local IPv4/IPv6 interface addresses for bounded isolation probes."""
    proc_net = Path("/proc/net")
    try:
        ipv4 = _ipv4_host_addresses_from_fib_trie(
            (proc_net / "fib_trie").read_text(encoding="ascii")
        )
        ipv6 = {address for address, _ in _ipv6_interface_records(
            (proc_net / "if_inet6").read_text(encoding="ascii")
        )}
    except (OSError, ValueError) as error:
        raise PermissionError("could not safely inspect host-local addresses") from error
    addresses = frozenset(ipv4 | ipv6)
    if not addresses:
        raise PermissionError("could not determine host-local interface addresses")
    return addresses


def host_networks():
    """Read local/directly connected host routes; fail closed if unavailable."""
    proc_net = Path("/proc/net")
    try:
        return _host_networks_from_proc(
            (proc_net / "route").read_text(encoding="ascii"),
            (proc_net / "ipv6_route").read_text(encoding="ascii"),
            (proc_net / "if_inet6").read_text(encoding="ascii"),
            (proc_net / "fib_trie").read_text(encoding="ascii"),
        )
    except (OSError, ValueError) as error:
        raise PermissionError("could not safely inspect host-local networks") from error


def _is_allowed_public_destination(address, blocked_networks):
    address = _policy_address(address)
    if address is None or not _is_public_unicast(address):
        return False
    return not any(
        network.version == address.version and address in network
        for network in blocked_networks
    )


def connect_public(host, port, blocked_networks=None):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if blocked_networks is None:
        blocked_networks = host_networks()
    resolved = [ipaddress.ip_address(address[4][0]) for address in addresses]
    if not resolved or any(
        getattr(address, "scope_id", None) is not None
        or not _is_allowed_public_destination(address, blocked_networks)
        for address in resolved
    ):
        raise PermissionError("non-public, host-local, or non-unicast destination")
    # Connect to the IP we validated, avoiding a second DNS resolution.
    last_error = None
    for family, kind, proto, _, address in addresses:
        sock = socket.socket(family, kind, proto)
        sock.settimeout(20)
        try:
            sock.connect(address)
            return sock
        except OSError as error:
            last_error = error
            sock.close()
    raise last_error


def parse_upstream_proxy(value):
    """Parse an unauthenticated HTTP parent-proxy endpoint."""
    if value is None:
        return None
    target = urlsplit(value)
    try:
        host = target.hostname
        port = target.port if target.port is not None else 80
    except ValueError as error:
        raise ValueError("invalid upstream proxy endpoint") from error
    try:
        parent_address = ipaddress.ip_address(host) if host else None
    except ValueError:
        parent_address = None
    if (
        target.scheme.lower() != "http"
        or not host
        or target.username is not None
        or target.password is not None
        or target.path not in ("", "/")
        or target.query
        or target.fragment
        or target.port != 3128
        or parent_address is None
        or parent_address != ipaddress.IPv4Address("10.0.2.100")
    ):
        raise ValueError("upstream proxy must be the explicit immediate-parent endpoint 10.0.2.100:3128")
    return host, port


def _receive_http_headers(sock):
    """Read one bounded HTTP response header block without buffering tunnel data."""
    headers = bytearray()
    while not headers.endswith(b"\r\n\r\n"):
        byte = sock.recv(1)
        if not byte:
            raise OSError("upstream proxy closed before completing its response headers")
        headers.extend(byte)
        if len(headers) > 65536:
            raise OSError("upstream proxy response headers are too large")
    return bytes(headers)


def _http_status_code(headers):
    try:
        fields = headers.split(b"\r\n", 1)[0].split()
        return int(fields[1])
    except (IndexError, ValueError) as error:
        raise ValueError("upstream proxy returned an invalid HTTP status line") from error


class _BoundedHeaderReader:
    def __init__(self, reader, total_limit, line_limit):
        self.reader = reader
        self.total_limit = total_limit
        self.line_limit = line_limit
        self.total = 0

    def readline(self, size=-1):
        limit = self.line_limit + 1
        if size >= 0:
            limit = min(limit, size)
        remaining = self.total_limit - self.total
        line = self.reader.readline(min(limit, remaining + 1))
        self.total += len(line)
        if len(line) > self.line_limit:
            raise http.client.LineTooLong("proxy request header line")
        if self.total > self.total_limit:
            raise http.client.HTTPException("proxy request headers exceed limit")
        return line


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    rbufsize = 0
    timeout = 30
    MAX_REQUEST_LINE = 8192
    MAX_HEADER_LINE = 8192
    MAX_HEADER_BYTES = 32768
    IDLE_TIMEOUT = 30
    CONNECT_TIMEOUT = 20
    slots = threading.BoundedSemaphore(64)

    def log_message(self, *_args):
        pass  # Never log URL paths, headers, tokens or request bodies.

    def send_error(self, code, message=None, explain=None):
        del message, explain
        self.close_connection = True
        self.send_response_only(code)
        self.send_header("Connection", "close")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def handle_one_request(self):
        try:
            self.raw_requestline = self.rfile.readline(self.MAX_REQUEST_LINE + 1)
            if len(self.raw_requestline) > self.MAX_REQUEST_LINE:
                self.send_error(HTTPStatus.REQUEST_URI_TOO_LONG)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return
            method = getattr(self, "do_" + self.command, None)
            if method is None:
                self.send_error(HTTPStatus.NOT_IMPLEMENTED)
                return
            method()
            self.wfile.flush()
        except (TimeoutError, OSError):
            self.close_connection = True

    def parse_request(self):
        original_reader = self.rfile
        self.rfile = _BoundedHeaderReader(
            original_reader, self.MAX_HEADER_BYTES, self.MAX_HEADER_LINE
        )
        try:
            valid = super().parse_request()
        finally:
            self.rfile = original_reader
        if not valid:
            return False
        if not self._valid_request_headers():
            self.send_error(HTTPStatus.BAD_REQUEST)
            return False
        return True

    def handle_expect_100(self):
        self.send_error(HTTPStatus.EXPECTATION_FAILED)
        return False

    def _valid_request_headers(self):
        if self.request_version not in {"HTTP/1.0", "HTTP/1.1"}:
            return False
        if any(ord(char) < 32 and char != "\t" or ord(char) == 127
               for _, value in self.headers.items() for char in value):
            return False
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) > 1 or (lengths and not re.fullmatch(r"[0-9]+", lengths[0].strip())):
            return False
        if self.headers.get_all("Transfer-Encoding", []) or self.headers.get_all("Expect", []):
            return False
        for value in self.headers.get_all("Connection", []):
            tokens = [item.strip() for item in value.split(",")]
            if any(not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", token) for token in tokens):
                return False
        hosts = self.headers.get_all("Host", [])
        return len(hosts) <= 1 and (self.request_version == "HTTP/1.0" or len(hosts) == 1)

    def do_CONNECT(self):
        self.forward(True)

    def do_GET(self):
        self.forward(False)

    def do_HEAD(self):
        self.forward(False)

    def do_POST(self):
        self.forward(False)

    def do_PUT(self):
        self.forward(False)

    def do_DELETE(self):
        self.forward(False)

    def do_PATCH(self):
        self.forward(False)

    def do_OPTIONS(self):
        self.forward(False)

    def _authority(self, raw, default_port, *, require_port=False):
        if not raw or "@" in raw or raw.endswith(":"):
            raise ValueError("invalid proxy request authority")
        parsed = urlsplit("//" + raw)
        host = parsed.hostname
        port = parsed.port if parsed.port is not None else default_port
        if (not host or parsed.path or parsed.query or parsed.fragment
                or parsed.username is not None or parsed.password is not None
                or not 1 <= port <= 65535 or (require_port and parsed.port is None)):
            raise ValueError("invalid proxy request authority")
        host = _normalize_policy_host(host)
        host = host.removeprefix("=")
        host_text = f"[{host}]" if ":" in host else host
        explicit_port = parsed.port is not None
        authority = f"{host_text}:{port}" if explicit_port or require_port else host_text
        return host, port, authority

    def _request_target(self, tunnel):
        if any(ord(char) <= 32 or ord(char) == 127 for char in self.path):
            raise ValueError("invalid proxy request target")
        if tunnel:
            host, port, authority = self._authority(self.path, 443, require_port=True)
            if port != 443:
                raise ValueError("CONNECT is limited to port 443")
            path = ""
        else:
            target = urlsplit(self.path)
            if (target.scheme.lower() != "http" or not target.netloc or target.fragment
                    or target.username is not None or target.password is not None):
                raise ValueError("HTTP proxy requests require an absolute HTTP URL")
            host, port, authority = self._authority(target.netloc, 80)
            if port != 80:
                raise ValueError("plain HTTP is limited to port 80")
            path = target.path or "/"
            if target.query:
                path += "?" + target.query
        hosts = self.headers.get_all("Host", [])
        if hosts:
            header_host, header_port, _ = self._authority(hosts[0], 443 if tunnel else 80,
                                                          require_port=tunnel)
            if header_host != host or header_port != port:
                raise ValueError("Host header does not match request target")
        return host, port, authority, path

    def _forward_request_headers(self, authority, request_target):
        nominated = set()
        for value in self.headers.get_all("Connection", []):
            for item in value.split(","):
                token = item.strip().lower()
                if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", token):
                    raise ValueError("invalid Connection header")
                nominated.add(token)
        blocked = {
            "connection", "proxy-connection", "proxy-authorization", "proxy-authenticate",
            "keep-alive", "te", "trailer", "transfer-encoding", "upgrade", "host",
        } | nominated
        lines = [f"{self.command} {request_target} HTTP/1.1", f"Host: {authority}", "Connection: close"]
        for key, value in self.headers.items():
            if key.lower() not in blocked:
                lines.append(f"{key}: {value}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1")

    def _relay(self, upstream, response_started):
        self.close_connection = True
        self.connection.settimeout(self.IDLE_TIMEOUT)
        upstream.settimeout(self.IDLE_TIMEOUT)
        readers = {self.connection, upstream}
        while readers:
            ready, _, _ = select.select(list(readers), [], [], self.IDLE_TIMEOUT)
            if not ready:
                if not response_started:
                    self.send_error(HTTPStatus.BAD_GATEWAY)
                return
            for source in ready:
                data = source.recv(65536)
                if not data:
                    if source is upstream and not response_started:
                        self.send_error(HTTPStatus.BAD_GATEWAY)
                        return
                    readers.remove(source)
                    destination = upstream if source is self.connection else self.connection
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                destination = upstream if source is self.connection else self.connection
                destination.sendall(data)
                if source is upstream:
                    response_started = True

    def forward(self, tunnel):
        upstream = None
        response_started = False
        parent_proxy = getattr(self.server, "upstream_proxy", None)
        try:
            try:
                host, port, authority, path = self._request_target(tunnel)
            except (ValueError, UnicodeError):
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            if not self.server.policy.allows(host):
                self.send_error(HTTPStatus.FORBIDDEN)
                return

            if parent_proxy:
                # Nested guests contact only their explicit immediate parent.
                upstream = socket.create_connection(parent_proxy, timeout=self.CONNECT_TIMEOUT)
                if tunnel:
                    upstream.sendall(
                        f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode("ascii")
                    )
                    response_headers = _receive_http_headers(upstream)
                    status_code = _http_status_code(response_headers)
                    self.connection.sendall(response_headers)
                    response_started = True
                    if not 200 <= status_code < 300:
                        self.close_connection = True
                        upstream.settimeout(self.IDLE_TIMEOUT)
                        while True:
                            ready, _, _ = select.select([upstream], [], [], self.IDLE_TIMEOUT)
                            if not ready:
                                return
                            data = upstream.recv(65536)
                            if not data:
                                return
                            self.connection.sendall(data)
                        return
                else:
                    request_target = f"http://{authority}{path}"
                    upstream.sendall(self._forward_request_headers(authority, request_target))
            else:
                upstream = connect_public(host, port)
                if tunnel:
                    self.send_response(HTTPStatus.OK, "Connection established")
                    self.end_headers()
                    response_started = True
                else:
                    upstream.sendall(self._forward_request_headers(authority, path))
            self._relay(upstream, response_started)
        except PermissionError:
            if parent_proxy and not response_started:
                self.send_error(HTTPStatus.BAD_GATEWAY)
            elif not response_started:
                self.send_error(HTTPStatus.FORBIDDEN)
        except (OSError, ValueError, UnicodeError):
            if not response_started:
                self.send_error(HTTPStatus.BAD_GATEWAY)
        finally:
            if upstream is not None:
                upstream.close()


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64

    def process_request(self, request, client_address):
        if not Proxy.slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            Proxy.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            Proxy.slots.release()


def _private_parent(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise PermissionError("proxy readiness path is not an absolute private path")
    for item in [*reversed(path.parents), path.parent]:
        if item.is_symlink():
            raise PermissionError("proxy readiness path contains a symlink")
    parent = path.parent
    info = parent.stat(follow_symlinks=False)
    if not parent.is_dir() or info.st_uid != os.getuid() or (info.st_mode & 0o777) != 0o700:
        raise PermissionError("proxy readiness directory is not private and owned")
    return path


def _atomic_private_create(path, payload):
    path = _private_parent(path)
    if path.exists() or path.is_symlink():
        raise FileExistsError("proxy readiness record already exists")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        os.unlink(temporary)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _remove_private_if_matches(path, expected):
    try:
        path = _private_parent(path)
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    try:
        info = os.fstat(fd)
        if (info.st_uid != os.getuid() or (info.st_mode & 0o777) != 0o600
                or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            return
        with os.fdopen(fd, "rb", closefd=False) as stream:
            value = stream.read(4097)
        if value == expected:
            path.unlink()
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        os.close(fd)


def _cleanup_private_if_matches(path, expected):
    try:
        _remove_private_if_matches(path, expected)
    except OSError:
        pass


def _process_identity(pid):
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    end = raw.rfind(")")
    if end < 0:
        raise OSError("could not inspect proxy process identity")
    fields = raw[end + 1 :].split()
    if len(fields) <= 19:
        raise OSError("could not inspect proxy process identity")
    return fields[19], os.path.realpath(f"/proc/{pid}/exe")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--port-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0, help="Bind a fixed loopback port instead of choosing one")
    parser.add_argument("--upstream-proxy", help="Optional parent HTTP proxy for nested VM egress")
    parser.add_argument("--reload-marker", type=Path, required=True)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        upstream_proxy = parse_upstream_proxy(args.upstream_proxy)
    except ValueError as error:
        parser.error(str(error))
    if args.port_file == args.reload_marker:
        parser.error("--port-file and --reload-marker must be different private files")
    try:
        workspace_valid = (
            args.workspace.is_absolute()
            and args.workspace.resolve(strict=True) == args.workspace
            and args.workspace.is_dir()
        )
    except OSError:
        workspace_valid = False
    if not workspace_valid:
        parser.error("--workspace must name an existing non-symlink directory")
    try:
        policy_store = PolicyStore(args.workspace / ".mester-egress")
    except (OSError, UnicodeError, ValueError):
        parser.error("could not load a valid egress policy")
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Proxy)
    except OSError:
        parser.error("could not bind the requested loopback proxy port")
    server.policy = policy_store
    server.upstream_proxy = upstream_proxy

    def reload_policy(_signum, _frame):
        try:
            server.policy.reload()
        except (OSError, UnicodeError, ValueError):
            print("egress policy reload failed; previous policy retained", file=sys.stderr, flush=True)
            return
        print("egress policy reloaded", file=sys.stderr, flush=True)

    signal.signal(signal.SIGHUP, reload_policy)

    # Readiness is visible only after both files are private and complete. The
    # marker binds the listener to this process incarnation, executable and port.
    port_created = False
    marker_created = False
    try:
        start_ticks, executable = _process_identity(os.getpid())
        port_value = f"{server.server_port}\n".encode("ascii")
        marker_value = (
            json.dumps({
                "schema_version": 1,
                "pid": os.getpid(),
                "start_ticks": start_ticks,
                "executable": executable,
                "port": server.server_port,
            }, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("ascii")
        _atomic_private_create(args.port_file, port_value)
        port_created = True
        _atomic_private_create(args.reload_marker, marker_value)
        marker_created = True
    except (OSError, ValueError):
        if marker_created:
            _cleanup_private_if_matches(args.reload_marker, marker_value)
        if port_created:
            _cleanup_private_if_matches(args.port_file, port_value)
        server.server_close()
        parser.error("could not publish private proxy readiness identity")

    def stop_proxy(_signum, _frame):
        # shutdown() must run from a thread other than serve_forever's thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_proxy)
    signal.signal(signal.SIGINT, stop_proxy)
    print(f"egress proxy listening on 127.0.0.1:{server.server_port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        _cleanup_private_if_matches(args.reload_marker, marker_value)
        _cleanup_private_if_matches(args.port_file, port_value)


if __name__ == "__main__":
    main()
