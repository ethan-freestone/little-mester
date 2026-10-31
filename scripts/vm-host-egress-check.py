#!/usr/bin/env python3
"""Verify the host VM proxy refuses connections to host-local addresses."""
import argparse
import http.client
import importlib.util
import ipaddress
import socket
import sys
from pathlib import Path

PROXY_PATH = Path(__file__).parents[1] / "lib" / "vm" / "proxy.py"
SPEC = importlib.util.spec_from_file_location("vm_proxy", PROXY_PATH)
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)


def select_targets(addresses, limit=16):
    candidates = {
        address
        for address in addresses
        if not address.is_loopback and not address.is_unspecified and not address.is_multicast
    }
    return sorted(
        candidates,
        key=lambda address: (
            not proxy._is_public_unicast(address),
            address.version,
            int(address),
        ),
    )[:limit]


def proxy_connect_status(port, address, timeout=5):
    host = f"[{address}]" if isinstance(address, ipaddress.IPv6Address) else str(address)
    request = (
        f"CONNECT {host}:443 HTTP/1.1\r\n"
        f"Host: {host}:443\r\n"
        "Proxy-Connection: close\r\n\r\n"
    )
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as connection:
        connection.settimeout(timeout)
        connection.sendall(request.encode("ascii"))
        response = http.client.HTTPResponse(connection)
        response.begin()
        return response.status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proxy_port", type=int)
    args = parser.parse_args(argv)
    if not 1 <= args.proxy_port <= 65535:
        parser.error("proxy_port must be between 1 and 65535")

    try:
        targets = select_targets(proxy.host_local_addresses())
    except (OSError, ValueError) as error:
        print(f"FAIL: host-local proxy targets unavailable ({type(error).__name__})")
        return 1
    if not targets:
        print("FAIL: no non-loopback host-local addresses available for proxy checks")
        return 1

    passed = 0
    failed = 0
    for index, address in enumerate(targets, start=1):
        family = "IPv4" if address.version == 4 else "IPv6"
        scope = "public" if proxy._is_public_unicast(address) else "non-public"
        name = f"host-local/directly-connected {scope} {family} target {index}"
        try:
            status = proxy_connect_status(args.proxy_port, address)
            ok = status == 403
            detail = f"HTTP {status}; expected HTTP 403"
        except (OSError, http.client.HTTPException) as error:
            ok = False
            detail = f"proxy unavailable or invalid response ({type(error).__name__})"
        result = "PASS" if ok else "FAIL"
        print(f"{result}: {name}: {detail}")
        if ok:
            passed += 1
        else:
            failed += 1

    print(f"Host proxy route checks: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
