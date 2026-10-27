#!/usr/bin/env python3
"""VM HTTP(S) egress proxy. Loopback listener; public destinations only.

The selected workspace manifest is read once at startup. No shell evaluation.
CONNECT is restricted to 443; plain HTTP to 80. Model API access uses separate
QEMU forwards and cannot be reached through this proxy.
"""
import argparse
import ipaddress
import select
import socket
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
    return enabled and (
        not domains
        or any(host == domain or host.endswith("." + domain) for domain in domains)
    )


def connect_public(host, port):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise PermissionError("private/reserved destination")
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
                    not allowed(host, *self.server.policy)):
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
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    server.policy = policy(args.workspace / ".mester-egress")
    args.port_file.write_text(str(server.server_port))
    server.serve_forever()
