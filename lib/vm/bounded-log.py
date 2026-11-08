#!/usr/bin/env python3
"""Run one child while retaining at most a fixed prefix of combined output."""

import os
import pathlib
import selectors
import signal
import subprocess
import sys
import time


def main(arguments):
    if len(arguments) < 4 or arguments[2] != "--":
        print("usage: bounded-log.py LOG_PATH MAX_BYTES -- COMMAND [ARG ...]", file=sys.stderr)
        return 2
    log_path = pathlib.Path(arguments[0])
    try:
        limit = int(arguments[1])
    except ValueError:
        print("invalid log size limit", file=sys.stderr)
        return 2
    if limit < 128 or not arguments[3:]:
        print("invalid log size limit or empty command", file=sys.stderr)
        return 2

    marker = b"\n[bounded-log: output truncated]\n"
    content_limit = limit - len(marker)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(log_path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        output = os.fdopen(descriptor, "wb", buffering=0)
    except OSError as error:
        print(f"could not open bounded log: {error}", file=sys.stderr)
        return 1

    try:
        child = subprocess.Popen(
            arguments[3:],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as error:
        output.close()
        print(f"could not start logged child: {error}", file=sys.stderr)
        return 1

    termination_deadline = [None]

    def forward(signum, _frame):
        termination_deadline[0] = time.monotonic() + 8
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    written = 0
    truncated = False
    selector = selectors.DefaultSelector()
    try:
        assert child.stdout is not None
        stream_fd = child.stdout.fileno()
        os.set_blocking(stream_fd, False)
        selector.register(stream_fd, selectors.EVENT_READ)
        while selector.get_map() or child.poll() is None:
            for key, _ in selector.select(timeout=0.1):
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fd)
                    continue
                remaining = content_limit - written
                if remaining > 0:
                    portion = chunk[:remaining]
                    output.write(portion)
                    written += len(portion)
                if len(chunk) > max(remaining, 0):
                    truncated = True
            if (termination_deadline[0] is not None
                    and time.monotonic() >= termination_deadline[0]
                    and child.poll() is None):
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                termination_deadline[0] = None
        status = child.wait()
        if truncated:
            output.write(marker)
    finally:
        output.close()
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            except ProcessLookupError:
                child.wait()
        selector.close()
    if status < 0:
        return 128 - status
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
