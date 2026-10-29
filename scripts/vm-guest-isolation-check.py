#!/usr/bin/env python3
"""Run bounded guest-originated probes for the QEMU VM boundary."""
import http.client
import json
import socket
import sys
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener


passed = 0
failed = 0


def report(name, ok, detail):
    global passed, failed
    result = "PASS" if ok else "FAIL"
    print(f"{result}: {name}: {detail}")
    if ok:
        passed += 1
    else:
        failed += 1


def tcp_connects(host, port, timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def fetch_direct(url, timeout=5):
    opener = build_opener(ProxyHandler({}))
    with opener.open(Request(url, headers={"Connection": "close"}), timeout=timeout) as response:
        return response.status


print("Guest-originated QEMU isolation checks")
print("These bounded probes check representative paths; they are not a proof against every possible host service.")

# QEMU SLIRP's host-side gateway address must not become a general route to
# host services. The explicitly configured guestfwd uses 10.0.2.100 instead.
for port in (22, 80, 443, 2375, 2376, 3128, 8080, 11434, 19532):
    reachable = tcp_connects("10.0.2.2", port)
    report(
        f"QEMU host gateway 10.0.2.2:{port}",
        not reachable,
        "connection blocked" if not reachable else "TCP connection succeeded unexpectedly",
    )

# Direct Internet access must remain unavailable; selected egress is through
# the separate allowlisting proxy at the guestfwd address.
for port in (80, 443):
    reachable = tcp_connects("1.1.1.1", port)
    report(
        f"direct Internet 1.1.1.1:{port}",
        not reachable,
        "connection blocked" if not reachable else "TCP connection succeeded unexpectedly",
    )

# The approved proxy endpoint should be reachable, but must refuse a private
# destination even if the workspace's domain allowlist is empty.
proxy = http.client.HTTPConnection("10.0.2.100", 3128, timeout=5)
try:
    proxy.request("GET", "http://127.0.0.1:1/", headers={"Connection": "close"})
    proxy_response = proxy.getresponse()
    proxy_status = proxy_response.status
    proxy_response.read()
    report(
        "approved egress proxy rejects private destinations",
        proxy_status == 403,
        f"HTTP {proxy_status}; expected HTTP 403",
    )
except (OSError, http.client.HTTPException) as exc:
    report("approved egress proxy", False, f"unreachable or invalid response ({type(exc).__name__})")
finally:
    proxy.close()

# Verify the selected model API's narrow guestfwd exception. This is a
# required dependency for the guest agent, not arbitrary host access.
try:
    config = json.loads(Path("/etc/opencode/opencode.json").read_text())
    provider = config["model"].split("/", 1)[0]
except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
    report("selected model configuration", False, f"cannot read provider ({type(exc).__name__})")
    provider = ""

model_urls = {
    "llamacpp": "http://10.0.2.100:8080/health",
    "ollama": "http://10.0.2.100:11434/api/tags",
}
if provider in model_urls:
    try:
        code = fetch_direct(model_urls[provider])
        report(f"approved {provider} model guestfwd", 200 <= code < 300, f"HTTP {code}")
    except Exception as exc:  # report transport/TLS errors without dumping request data
        report(f"approved {provider} model guestfwd", False, f"unreachable ({type(exc).__name__})")
elif provider:
    report("selected model configuration", False, f"unsupported VM model provider {provider!r}")

# The mounted workspace is an intentional, narrow host data channel. Verify
# the mount type so the report does not imply that the guest has no host data.
try:
    mount_lines = Path("/proc/mounts").read_text().splitlines()
    workspace_types = [line.split()[2] for line in mount_lines if len(line.split()) >= 3 and line.split()[1] == "/workspace"]
    report(
        "selected workspace mount",
        "virtiofs" in workspace_types,
        f"filesystem={','.join(workspace_types) if workspace_types else 'not mounted'} (workspace access is intentional)",
    )
except OSError as exc:
    report("selected workspace mount", False, f"cannot inspect mount table ({type(exc).__name__})")

print(f"Result: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
