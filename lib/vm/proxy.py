#!/usr/bin/env python3
"""VM HTTP(S) egress proxy. Loopback listener; public destinations only.

The selected workspace manifest is loaded at startup and can be safely reloaded
with SIGHUP without rebinding the listener. No shell evaluation. CONNECT is
restricted to 443; plain HTTP to 80. Local model APIs use separate QEMU
guestfwd channels; approved public provider APIs may use this proxy.
"""
import argparse
import ipaddress
import os
import select
import signal
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def policy(path):
    settings = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                key, value = line.split("=", 1)
                settings[key.strip()] = value.strip().strip("\"'")
    enabled = settings.get("permission", "true") == "true"
    domains = {s.strip().lower() for s in settings.get("allowed_domains", "").split(",") if s.strip()}
    return enabled, domains


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
        # Parse first; a failed read must leave the current policy in place.
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        current = policy(self.path)
        with self._lock:
            self._policy = current


def _is_public_unicast(address):
    # Some Python versions do not consistently inherit IPv4 classification
    # for IPv4-mapped IPv6 addresses. Apply the IPv4 policy to the mapped value.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


def connect_public(host, port):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    resolved = [ipaddress.ip_address(address[4][0]) for address in addresses]
    if not resolved or any(not _is_public_unicast(address) for address in resolved):
        raise PermissionError("non-public or non-unicast destination")
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


class Proxy(BaseHTTPRequestHandler):
    rbufsize = 0
    timeout = 30
    slots = threading.BoundedSemaphore(64)

    def log_message(self, *_args):
        pass  # Never log URL paths, headers, tokens or request bodies.

    def do_CONNECT(self):
        self.forward(True)

    def do_GET(self):
        self.forward(False)

    do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_GET

    def forward(self, tunnel):
        if not self.slots.acquire(blocking=False):
            self.send_error(503)
            return
        upstream = None
        try:
            target = urlsplit("//" + self.path if tunnel else self.path)
            host = target.hostname
            port = target.port or (443 if tunnel else 80)
            if (not host or target.username or target.password or
                    (not tunnel and target.scheme != "http") or
                    port != (443 if tunnel else 80) or
                    not self.server.policy.allows(host)):
                self.send_error(403)
                return
            upstream = connect_public(host, port)
            if tunnel:
                self.send_response(200, "Connection established")
                self.end_headers()
            else:
                path = target.path or "/"
                if target.query:
                    path += "?" + target.query
                headers = [f"{self.command} {path} HTTP/1.1", f"Host: {host}", "Connection: close"]
                for key, value in self.headers.items():
                    if key.lower() not in {"host", "connection", "proxy-connection", "proxy-authorization"}:
                        headers.append(f"{key}: {value}")
                upstream.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("iso-8859-1"))
            self.close_connection = True
            self.connection.settimeout(60)
            while True:
                ready, _, _ = select.select([self.connection, upstream], [], [], 60)
                if not ready:
                    break
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is self.connection else self.connection).sendall(data)
        except PermissionError:
            self.send_error(403)
        except (OSError, ValueError):
            if upstream is None:
                self.send_error(502)
        finally:
            if upstream:
                upstream.close()
            self.slots.release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--port-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0, help="Bind a fixed loopback port instead of choosing one")
    parser.add_argument("--reload-marker", type=Path, required=True)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Proxy)
    server.policy = PolicyStore(args.workspace / ".mester-egress")

    def reload_policy(_signum, _frame):
        try:
            server.policy.reload()
        except (OSError, UnicodeError):
            print("egress policy reload failed; previous policy retained", file=sys.stderr, flush=True)
            return
        print("egress policy reloaded", file=sys.stderr, flush=True)

    signal.signal(signal.SIGHUP, reload_policy)

    # A marker lets the lifecycle command distinguish this reload-capable
    # process from an older proxy before sending SIGHUP (whose default action
    # would terminate that older process).
    marker_value = f"{os.getpid()} {server.server_port}\n"
    marker_tmp = args.reload_marker.with_name(f"{args.reload_marker.name}.{os.getpid()}.tmp")
    marker_tmp.write_text(marker_value, encoding="ascii")
    os.replace(marker_tmp, args.reload_marker)
    args.port_file.write_text(str(server.server_port))

    def stop_proxy(_signum, _frame):
        # shutdown() must run from a thread other than serve_forever's thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_proxy)
    print(f"egress proxy listening on 127.0.0.1:{server.server_port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            if args.reload_marker.read_text(encoding="ascii") == marker_value:
                args.reload_marker.unlink()
        except OSError:
            pass
