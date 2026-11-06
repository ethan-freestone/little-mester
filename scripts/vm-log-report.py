#!/usr/bin/env python3
"""Bounded, redacted VM log excerpts. No raw logs are saved by this collector."""

import os
import re
import selectors
import signal
import stat
import subprocess
import time
from pathlib import Path

LIMIT = 65536
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|$))")
SENSITIVE = re.compile(
    r"(?i)ssh-(?:ed25519|rsa)|ecdsa-sha2|(?:private|public|host)[ _-]*key|"
    r"fingerprint|authorized.keys|SHA256:|(?:password|passwd|secret|token|credential|"
    r"authorization|cookie|api[_-]?key)|-----BEGIN|-----END|(?:[0-9a-f]{2}:){8}"
)
BLOB = re.compile(r"[A-Za-z0-9_+/=-]{40,}")
URL = re.compile(r"https?://[^\s<>\"']+")


def redact(raw, max_lines=80):
    text = ANSI.sub("", raw.decode("utf-8", errors="replace"))
    lines = []
    first_begin, first_end = text.find("-----BEGIN "), text.find("-----END ")
    block = first_end >= 0 and (first_begin < 0 or first_end < first_begin)
    for line in text.splitlines():
        line = "".join(c if c == "\t" or c.isprintable() else "?" for c in line)
        if "-----BEGIN " in line:
            block = True
        if block:
            lines.append("[REDACTED key/certificate block]")
            if "-----END " in line:
                block = False
            continue
        if SENSITIVE.search(line):
            lines.append("[REDACTED sensitive/key-related line]")
            continue
        # Do not publish URL userinfo, path tokens, queries or fragments.
        line = URL.sub("[URL REDACTED]", line)
        lines.append(BLOB.sub("[REDACTED long identifier]", line)[:1000])
    return "\n".join(lines[-max_lines:])


def local_tail(path, paths):
    try:
        paths.private_entry(path.parent)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1):
                return {"state": "unsafe"}
            os.lseek(fd, max(0, info.st_size - LIMIT), os.SEEK_SET)
            raw = os.read(fd, LIMIT)
        finally:
            os.close(fd)
        # Discard a potentially partial sensitive line at the tail boundary.
        if info.st_size > LIMIT:
            raw = raw.partition(b"\n")[2]
        sanitized = redact(raw, max_lines=LIMIT)
        errors = [line for line in sanitized.splitlines()
                  if re.search(r"(?i)failed|failure|error|NO_PUBKEY|not signed|unable to|denied", line)]
        lines = sanitized.splitlines()
        anchors = [index for index, line in enumerate(lines)
                   if re.search(r"(?i)failed to run module scripts_user|GUEST BOOTSTRAP FAILED|"
                                r"npm (?:ERR!|error)|curl:|command not found|no such file", line)]
        context_indices = set()
        for index in anchors[-3:]:
            context_indices.update(range(max(0, index - 25), min(len(lines), index + 3)))
        return {"state": "collected", "excerpt": "\n".join(sanitized.splitlines()[-80:]),
                "error_excerpt": "\n".join(errors[-30:]),
                "failure_context": "\n".join(lines[index] for index in sorted(context_indices)[-80:]),
                "bounded": True}
    except FileNotFoundError:
        return {"state": "absent"}
    except (OSError, ValueError):
        return {"state": "unavailable"}


def guest_logs(profile, run, process, repo):
    """Use existing isolated SSH transport, without learning/replacing host keys."""
    try:
        record = process.registry_read(run / "processes.json", run.name)
        candidates = []
        for role, item in record["processes"].items():
            identity = item["identity"]
            if not role.startswith("qemu_") or not identity or process.identity_state(identity) != "running":
                continue
            if Path(identity["executable"]).name not in {"qemu-system-x86_64", "qemu-system-x86"}:
                continue
            if f"file={profile}/disk.qcow2,format=qcow2,if=virtio" not in identity["argv"]:
                continue
            ports = re.findall(r"hostfwd=tcp:127\.0\.0\.1:(\d+)-:22", "\n".join(identity["argv"]))
            if len(ports) == 1 and process.listener_owned_by(identity["pid"], int(ports[0])):
                candidates.append((role, ports[0]))
        if len(candidates) != 1:
            return {"state": "unavailable-no-unique-verified-listener"}
        role, port = candidates[0]
        command = ["bash", "-c", '''
set -euo pipefail
unset VM_SSH_CONTROL_PATH
source "$1/lib/vm/ssh.sh"
vm_ssh_listener_verified "$2/processes.json" "$3" "$4" "$6" || exit 2
vm_ssh_noninteractive "$5/control-key" "$2/known_hosts" "$6" 15 mester@127.0.0.1 \
'printf "CLOUD_INIT_STATUS\\n"; sudo -n cloud-init status --long --format=json 2>/dev/null | head -c 4096; printf "\\nCLOUD_INIT_OUTPUT\\n"; sudo -n tail -c 32768 /var/log/cloud-init-output.log 2>/dev/null; printf "\\nCLOUD_INIT_LOG\\n"; sudo -n tail -c 16384 /var/log/cloud-init.log 2>/dev/null; printf "\\nAGENT_EXECUTABLE_USER\\n"; PATH=/opt/little-mester/python-tools/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin command -v opencode || true; printf "\\nAGENT_EXECUTABLE_ROOT\\n"; sudo -n bash -c "command -v opencode" 2>/dev/null || true; printf "\\nAGENT_EXECUTABLE_PATH_MODES\\n"; sudo -n namei -l /usr/local/bin/opencode /usr/bin/opencode 2>/dev/null | head -c 4096; true'
''', "_", str(repo), str(run), run.name, role, str(profile), port]
        env = {k: v for k, v in os.environ.items() if not k.startswith("VM_SSH_")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              env=env, start_new_session=True) as child:
            raw = bytearray()
            reason = "collected"
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(child.stdout, selectors.EVENT_READ)
                    deadline = time.monotonic() + 20
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            reason = "timeout"
                            break
                        if not selector.select(remaining):
                            reason = "timeout"
                            break
                        chunk = os.read(child.stdout.fileno(), min(4096, LIMIT + 1 - len(raw)))
                        if not chunk:
                            try:
                                child.wait(timeout=1)
                            except subprocess.TimeoutExpired:
                                reason = "client-exit-timeout"
                            break
                        raw.extend(chunk)
                        if len(raw) > LIMIT:
                            reason = "output-limit"
                            break
            finally:
                # Terminate only this collector's private SSH client group.
                if child.poll() is None:
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
            if reason == "collected" and child.returncode != 0:
                reason = "ssh-command-failed"
            if reason == "output-limit":
                raw = raw[:LIMIT].rsplit(b"\n", 1)[0]
            return {"state": reason, "excerpt": redact(bytes(raw)), "bounded": True}
    except (OSError, ValueError, KeyError, TypeError):
        return {"state": "unavailable"}
