#!/usr/bin/env python3
"""Host-owned HTTP(S) egress proxy. Public destinations only.

Policy is fixed by the host launcher and never read from exported workspace
files. CONNECT is restricted to 443; plain HTTP to 80. Every resolved address
must be public and outside host-local/directly-connected networks.
"""
import argparse
import errno
import ipaddress
import itertools
import logging
import os
import select
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

_NAT64_WELL_KNOWN_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")
_CONNECT_DEADLINE = 20.0
_CONNECT_STAGGER = 0.25
# Retire a blackholed nonblocking attempt so later same-family answers progress.
_CANDIDATE_TIMEOUT = 3.0
_MAX_ACTIVE_DIALS = 2
_MAX_CANDIDATES = 16
_SLOW_LOG_THRESHOLD = 1.0
_LOG = logging.getLogger("vm-proxy")
_REQUEST_IDS = itertools.count(1)
_REQUEST_CONTEXT = threading.local()
_monotonic = time.monotonic
_wall_monotonic = time.monotonic


def _safe_host(host):
    """Return a short hostname token without accepting log-control characters."""
    value = str(host).lower().rstrip(".")
    try:
        ipaddress.ip_address(value.strip("[]"))
        return "ip-literal"
    except ValueError:
        pass
    if len(value) > 120 or not value or any(
        char not in "abcdefghijklmnopqrstuvwxyz0123456789.-:" for char in value
    ):
        return "invalid-host"
    return value


def _errno_category(error):
    number = getattr(error, "errno", None)
    return {
        errno.ECONNREFUSED: "refused", errno.ETIMEDOUT: "timeout",
        errno.ENETUNREACH: "network-unreachable", errno.EHOSTUNREACH: "host-unreachable",
        errno.EADDRNOTAVAIL: "address-unavailable", errno.EACCES: "access-denied",
    }.get(number, "connect-error")


def _proxy_log(host, reason, **fields):
    safe = _safe_host(host)
    bounded = []
    for key, value in list(fields.items())[:8]:
        if isinstance(value, int):
            value = max(0, min(value, 999_999_999))
        elif (not isinstance(value, str) or len(value) > 120
              or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                     "0123456789_.:-;" for char in value)):
            value = "invalid"
        bounded.append(f"{key}={value}")
    summary = " ".join(bounded)
    request_id = getattr(_REQUEST_CONTEXT, "request_id", "-")
    _LOG.warning("proxy request=%s host=%s reason=%s %s", request_id, safe, reason, summary[:400])


def _quiet_close(sock):
    try:
        sock.close()
    except OSError:
        pass


def policy(_path=None):
    # #49/#51 owner-approved public HTTP(S) egress. There is intentionally no
    # workspace policy file: the proxy itself enforces public-only destinations.
    return True, set()


def allowed(host, enabled, domains):
    host = host.lower().rstrip(".")
    if not enabled:
        return False
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
        raise PermissionError("egress policy is host-owned and immutable for this run")


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
    dns_started = _monotonic()
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except Exception:
        _proxy_log(host, "dns-failed", elapsed_ms=int((_monotonic()-dns_started)*1000),
                   reason_code="resolve-error")
        raise
    dns_elapsed = _monotonic() - dns_started
    if dns_elapsed > _SLOW_LOG_THRESHOLD:
        _proxy_log(host, "dns-slow", elapsed_ms=int(dns_elapsed * 1000))
    if blocked_networks is None:
        network_started = _monotonic()
        try:
            blocked_networks = host_networks()
        except Exception:
            _proxy_log(host, "policy-failed", phase="host-networks",
                       elapsed_ms=int((_monotonic()-network_started)*1000),
                       reason_code="policy-error")
            raise
        network_elapsed = _monotonic() - network_started
        if network_elapsed > _SLOW_LOG_THRESHOLD:
            _proxy_log(host, "host-networks-slow", phase="policy",
                       elapsed_ms=int(network_elapsed * 1000))
    try:
        resolved = [ipaddress.ip_address(address[4][0]) for address in addresses]
    except (IndexError, TypeError, ValueError):
        _proxy_log(host, "policy-failed", phase="dns-answer",
                   reason_code="invalid-answer")
        raise PermissionError("resolver returned an invalid address") from None
    if not resolved or any(
        not _is_allowed_public_destination(address, blocked_networks) for address in resolved
    ):
        _proxy_log(host, "policy-denied", elapsed_ms=int((_monotonic()-dns_started)*1000),
                   reason_code="destination-denied")
        raise PermissionError("non-public, host-local, or non-unicast destination")
    unique, seen = [], set()
    for family, kind, proto, _, sockaddr in addresses:
        key = (family, sockaddr)
        if key not in seen:
            seen.add(key)
            unique.append((family, kind, proto, sockaddr))
    # Preserve resolver preference while interleaving families and retaining
    # same-family fallbacks. Validation above intentionally precedes this cap.
    preferred = unique[0][0] if unique else None
    order = []
    queues = {}
    for item in unique:
        queues.setdefault(item[0], []).append(item)
    while any(queues.values()):
        for family in (preferred, socket.AF_INET6 if preferred == socket.AF_INET else socket.AF_INET):
            if family is not None and queues.get(family):
                order.append(queues[family].pop(0))
        for family, queue in queues.items():
            if family not in (preferred, socket.AF_INET, socket.AF_INET6) and queue:
                order.append(queue.pop(0))
    candidates = order[:_MAX_CANDIDATES]
    if not candidates:
        raise OSError(errno.EHOSTUNREACH, "no address candidates")

    started_all = _monotonic()
    deadline = started_all + _CONNECT_DEADLINE
    active, winner = {}, None
    index, launch_at = 0, started_all
    last_error = OSError(errno.EHOSTUNREACH, "no connection candidate succeeded")
    attempts = []
    try:
        while _monotonic() < deadline:
            now = _monotonic()
            expired = []
            for sock, (family, ordinal, started) in list(active.items()):
                elapsed = now - started
                if elapsed >= _CANDIDATE_TIMEOUT:
                    active.pop(sock, None)
                    _quiet_close(sock)
                    attempts.append((family, ordinal, elapsed, "timeout"))
                    last_error = OSError(errno.ETIMEDOUT, "candidate connect deadline exceeded")
                    _proxy_log(host, "connect-candidate-failed", phase="connect",
                               elapsed_ms=int(elapsed * 1000), ordinal=ordinal,
                               family="ipv6" if family == socket.AF_INET6 else "ipv4",
                               reason_code="timeout")
                    expired.append(sock)
            if expired and index < len(candidates):
                launch_at = min(launch_at, now)
            if (index < len(candidates) and len(active) < _MAX_ACTIVE_DIALS
                    and now >= launch_at):
                family, kind, proto, sockaddr = candidates[index]
                ordinal = index + 1
                index += 1
                try:
                    sock = socket.socket(family, kind, proto)
                except Exception:
                    _proxy_log(host, "connect-failed", elapsed_ms=int((_monotonic()-started_all)*1000),
                               ordinal=ordinal, family="ipv6" if family == socket.AF_INET6 else "ipv4",
                               reason_code="socket-error")
                    raise
                started = _monotonic()
                active[sock] = (family, ordinal, started)
                try:
                    sock.setblocking(False)
                except Exception:
                    _proxy_log(host, "connect-failed", elapsed_ms=int((_monotonic()-started_all)*1000),
                               ordinal=ordinal,
                               family="ipv6" if family == socket.AF_INET6 else "ipv4",
                               reason_code="socket-setup-error")
                    raise
                try:
                    result = sock.connect_ex(sockaddr)
                except Exception as error:
                    active.pop(sock, None)
                    attempts.append((family, ordinal, _monotonic()-started,
                                     _errno_category(error) if isinstance(error, OSError)
                                     else "connect-error"))
                    _quiet_close(sock)
                    last_error = error if isinstance(error, OSError) else OSError(
                        errno.ECONNABORTED, "connect failed")
                    _proxy_log(host, "connect-candidate-failed", phase="connect",
                               elapsed_ms=int((_monotonic()-started)*1000), ordinal=ordinal,
                               family="ipv6" if family == socket.AF_INET6 else "ipv4",
                               reason_code=_errno_category(error) if isinstance(error, OSError)
                               else "connect-error")
                    launch_at = _monotonic()
                    continue
                if result in (0, errno.EISCONN):
                    active.pop(sock, None)
                    winner = sock
                    break
                if result not in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, errno.EINTR):
                    last_error = OSError(result, "connect failed")
                    attempts.append((family, ordinal, _monotonic() - started,
                                     _errno_category(last_error)))
                    _proxy_log(host, "connect-candidate-failed",
                               phase="connect", family="ipv6" if family == socket.AF_INET6 else "ipv4",
                               ordinal=ordinal, elapsed_ms=int((_monotonic()-started)*1000),
                               reason_code=_errno_category(last_error))
                    active.pop(sock, None)
                    _quiet_close(sock)
                    launch_at = _monotonic()
                else:
                    active[sock] = (family, ordinal, started)
                    launch_at = _monotonic() + _CONNECT_STAGGER
            if active:
                wake = deadline
                wake = min(wake, min(
                    started + _CANDIDATE_TIMEOUT
                    for _, _, started in active.values()
                ))
                if index < len(candidates) and len(active) < _MAX_ACTIVE_DIALS:
                    wake = min(wake, launch_at)
                try:
                    _, writable, exceptional = select.select([], list(active), list(active),
                                                               max(0, wake - _monotonic()))
                except Exception:
                    _proxy_log(host, "connect-failed", elapsed_ms=int((_monotonic()-started_all)*1000),
                               reason_code="select-error")
                    raise
                for sock in dict.fromkeys(writable + exceptional):
                    if sock not in active:
                        continue
                    family, ordinal, started = active.pop(sock)
                    try:
                        result = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                    except Exception:
                        attempts.append((family, ordinal, _monotonic()-started,
                                         "socket-status-error"))
                        last_error = OSError(errno.ECONNABORTED, "socket status check failed")
                        _quiet_close(sock)
                        _proxy_log(host, "connect-candidate-failed", phase="connect",
                                   elapsed_ms=int((_monotonic()-started)*1000), ordinal=ordinal,
                                   family="ipv6" if family == socket.AF_INET6 else "ipv4",
                                   reason_code="socket-status-error")
                        continue
                    if result == 0:
                        if winner is None:
                            winner = sock
                        else:
                            _quiet_close(sock)
                    else:
                        last_error = OSError(result or errno.ECONNABORTED, "connect failed")
                        attempts.append((family, ordinal, _monotonic() - started,
                                         _errno_category(last_error)))
                        _quiet_close(sock)
                if winner is not None:
                    break
            elif index >= len(candidates):
                break
            else:
                select.select([], [], [], max(0, min(launch_at, deadline) - _monotonic()))
        if winner is None:
            if _monotonic() >= deadline:
                last_error = OSError(errno.ETIMEDOUT, "connect deadline exceeded")
            pending = [(family, ordinal, _monotonic()-started)
                       for family, ordinal, started in active.values()]
            _proxy_log(host, "connect-failed", elapsed_ms=int((_monotonic()-started_all)*1000),
                       attempts=len(attempts), reason_code=_errno_category(last_error),
                       candidates=";".join(
                           f"{ 'ipv6' if family == socket.AF_INET6 else 'ipv4' }:{ordinal}:"
                           f"{int(elapsed*1000)}:{category}"
                           for family, ordinal, elapsed, category in attempts[:4]
                       ), pending=";".join(
                           f"{'ipv6' if family == socket.AF_INET6 else 'ipv4'}:{ordinal}:{int(elapsed*1000)}"
                           for family, ordinal, elapsed in pending[:_MAX_ACTIVE_DIALS]
                       ))
            raise last_error
        try:
            winner.setblocking(True)
            winner.settimeout(_CONNECT_DEADLINE)
        except Exception:
            _quiet_close(winner)
            _proxy_log(host, "connect-failed", phase="winner-setup",
                       family="ipv6" if winner.family == socket.AF_INET6 else "ipv4",
                       reason_code="winner-setup-error")
            raise
        elapsed = _monotonic() - started_all
        if elapsed > _SLOW_LOG_THRESHOLD:
            _proxy_log(host, "connect-slow", elapsed_ms=int(elapsed*1000),
                       family="ipv6" if winner.family == socket.AF_INET6 else "ipv4")
        return winner
    finally:
        for sock in active:
            if sock is not winner:
                _quiet_close(sock)


def parse_upstream_proxy(value):
    """Parse an unauthenticated HTTP parent-proxy endpoint."""
    if value is None:
        return None
    target = urlsplit(value)
    try:
        host = target.hostname
        port = target.port or 80
    except ValueError as error:
        raise ValueError("invalid upstream proxy endpoint") from error
    if (
        target.scheme.lower() != "http"
        or not host
        or target.username
        or target.password
        or target.path not in ("", "/")
        or target.query
        or target.fragment
        or not 1 <= port <= 65535
    ):
        raise ValueError("upstream proxy must be a plain HTTP proxy endpoint without credentials or a path")
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


class Proxy(BaseHTTPRequestHandler):
    rbufsize = 0
    timeout = 30

    def log_message(self, *_args):
        pass  # Never log URL paths, headers, tokens or request bodies.

    def do_CONNECT(self):
        self.forward(True)

    def do_GET(self):
        self.forward(False)

    do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_GET

    def forward(self, tunnel):
        upstream = None
        response_started = False
        request_started = _wall_monotonic()
        transfer_started = request_started
        first_byte = False
        byte_count = 0
        client_bytes = 0
        log_host = "invalid-host"
        io_side = "setup"
        request_id = next(_REQUEST_IDS)
        _REQUEST_CONTEXT.request_id = request_id
        outcome = "complete"
        parent_proxy = getattr(self.server, "upstream_proxy", None)
        try:
            target = urlsplit("//" + self.path if tunnel else self.path)
            host = target.hostname
            log_host = host or "invalid-host"
            port = target.port or (443 if tunnel else 80)
            if (not host or target.username or target.password or
                    (not tunnel and target.scheme != "http") or
                    port != (443 if tunnel else 80) or
                    not self.server.policy.allows(host)):
                outcome = "denied"
                _proxy_log(log_host, "request-denied", phase="validation",
                           reason_code="policy-denied")
                self.send_error(403)
                return

            if parent_proxy:
                # Nested guests can reach public HTTP(S) only by chaining through
                # the parent VM's already-restricted egress proxy. The parent
                # remains responsible for DNS/IP and host-route validation.
                upstream = socket.create_connection(parent_proxy, timeout=_CONNECT_DEADLINE)
                if tunnel:
                    authority_host = f"[{host}]" if ":" in host else host
                    authority = f"{authority_host}:{port}"
                    request = (
                        f"CONNECT {authority} HTTP/1.1\r\n"
                        f"Host: {authority}\r\n"
                        "Proxy-Connection: keep-alive\r\n\r\n"
                    )
                    upstream.sendall(request.encode("ascii"))
                    response_headers = _receive_http_headers(upstream)
                    status_code = _http_status_code(response_headers)
                    self.connection.sendall(response_headers)
                    response_started = True
                    if not 200 <= status_code < 300:
                        # Preserve the parent's denial/error response body.
                        self.close_connection = True
                        while True:
                            data = upstream.recv(65536)
                            if not data:
                                return
                            self.connection.sendall(data)
                else:
                    path = target.path or "/"
                    if target.query:
                        path += "?" + target.query
                    request_target = f"http://{target.netloc}{path}"
                    headers = [
                        f"{self.command} {request_target} HTTP/1.1",
                        f"Host: {target.netloc}",
                        "Connection: close",
                    ]
                    for key, value in self.headers.items():
                        if key.lower() not in {
                            "host", "connection", "proxy-connection", "proxy-authorization"
                        }:
                            headers.append(f"{key}: {value}")
                    upstream.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("iso-8859-1"))
            else:
                upstream = connect_public(host, port)
            transfer_started = _wall_monotonic()
            _REQUEST_CONTEXT.request_connected_at = transfer_started
            if not parent_proxy:
                if tunnel:
                    self.send_response(200, "Connection established")
                    self.end_headers()
                    response_started = True
                else:
                    path = target.path or "/"
                    if target.query:
                        path += "?" + target.query
                    headers = [
                        f"{self.command} {path} HTTP/1.1",
                        f"Host: {target.netloc}",
                        "Connection: close",
                    ]
                    for key, value in self.headers.items():
                        if key.lower() not in {
                            "host", "connection", "proxy-connection", "proxy-authorization"
                        }:
                            headers.append(f"{key}: {value}")
                    upstream.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("iso-8859-1"))

            self.close_connection = True
            self.connection.settimeout(60)
            upstream.settimeout(60)
            while True:
                ready, _, _ = select.select([self.connection, upstream], [], [], 60)
                if not ready:
                    outcome = "idle-timeout"
                    _proxy_log(log_host, "transfer-idle-timeout",
                               elapsed_ms=int((_wall_monotonic()-transfer_started)*1000),
                               side="upstream-and-client", upstream_bytes=byte_count,
                               client_bytes=client_bytes)
                    if not response_started:
                        self.send_error(502)
                    break
                for source in ready:
                    io_side = "upstream" if source is upstream else "client"
                    data = source.recv(65536)
                    if not data:
                        elapsed = _wall_monotonic() - transfer_started
                        side = "upstream" if source is upstream else "client"
                        if source is upstream and not response_started:
                            outcome = "upstream-eof-before-response"
                        elif source is self.connection:
                            outcome = "client-eof"
                        if elapsed > _SLOW_LOG_THRESHOLD:
                            _proxy_log(log_host, f"{side}-eof", elapsed_ms=int(elapsed*1000),
                                       upstream_bytes=byte_count, client_bytes=client_bytes,
                                       first_byte=int(first_byte))
                        if source is upstream and not response_started:
                            self.send_error(502)
                        return
                    (upstream if source is self.connection else self.connection).sendall(data)
                    if source is upstream:
                        byte_count += len(data)
                        if not first_byte:
                            first_byte = True
                            elapsed = _wall_monotonic() - transfer_started
                            request_elapsed = _wall_monotonic() - request_started
                            if elapsed > _SLOW_LOG_THRESHOLD or request_elapsed > _SLOW_LOG_THRESHOLD:
                                _proxy_log(log_host, "upstream-first-byte-slow",
                                           phase="first-byte",
                                           upstream_ms=int(elapsed*1000),
                                           request_ms=int(request_elapsed*1000))
                        response_started = True
                    else:
                        client_bytes += len(data)
        except PermissionError:
            outcome = "denied"
            _proxy_log(log_host, "request-denied", phase="destination", reason_code="policy-denied")
            if parent_proxy and not response_started:
                self.send_error(502)
            elif not response_started:
                self.send_error(403)
        except (OSError, ValueError):
            outcome = "io-error"
            _proxy_log(log_host, "transfer-io-error", side=io_side,
                       upstream_bytes=byte_count, client_bytes=client_bytes)
            if not response_started:
                self.send_error(502)
        finally:
            elapsed = _wall_monotonic() - transfer_started
            request_elapsed = _wall_monotonic() - request_started
            if request_elapsed > _SLOW_LOG_THRESHOLD or outcome != "complete":
                _proxy_log(log_host, "forward-summary", total_ms=int(request_elapsed*1000),
                           phase="transfer", result=outcome, first_byte=int(first_byte),
                           upstream_bytes=byte_count, client_bytes=client_bytes)
            if upstream:
                upstream.close()
            _REQUEST_CONTEXT.request_id = "-"


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Bound accepted handlers before ThreadingMixIn creates a thread."""

    request_queue_size = 64

    def __init__(self, *args, max_connections=64, **kwargs):
        if max_connections < 1:
            raise ValueError("max_connections must be positive")
        self._connection_slots = threading.BoundedSemaphore(max_connections)
        super().__init__(*args, **kwargs)

    def handle_error(self, _request, _client_address):
        pass  # Client disconnects must not emit headers or request data to logs.

    def process_request(self, request, client_address):
        if not self._connection_slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Connection: close\r\nContent-Length: 0\r\n\r\n"
                )
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0, help="Bind a fixed loopback port instead of choosing one")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    server = BoundedThreadingHTTPServer(("127.0.0.1", args.port), Proxy)
    server.policy = PolicyStore(None)
    server.upstream_proxy = None
    # The service runtime directory is private, and the readiness record is
    # deliberately private as well. Create exclusively so stale evidence is
    # not silently overwritten by a second proxy instance.
    descriptor = os.open(args.port_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600)
    try:
        os.write(descriptor, f"{server.server_port}\n".encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

    def stop_proxy(_signum, _frame):
        # shutdown() must run from a thread other than serve_forever's thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_proxy)
    print(f"egress proxy listening on 127.0.0.1:{server.server_port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
