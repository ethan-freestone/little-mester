#!/usr/bin/env python3
"""Run one child while retaining at most a fixed prefix of combined output."""

import ctypes
import json
import os
import pathlib
import selectors
import signal
import subprocess
import sys
import time

PR_SET_CHILD_SUBREAPER = 36
TERMINATION_GRACE_SECONDS = 8
KILL_SETTLE_SECONDS = 2


def main(arguments):
    if len(arguments) < 4:
        print("usage: bounded-log.py LOG_PATH MAX_BYTES [--fail-on-overflow] [--stdin] [--completion-file PATH] -- COMMAND [ARG ...]", file=sys.stderr)
        return 2
    log_name = arguments[0]
    try:
        limit = int(arguments[1])
    except ValueError:
        print("invalid log size limit", file=sys.stderr)
        return 2
    options = set()
    completion_path = None
    index = 2
    while index < len(arguments) and arguments[index].startswith("--") and arguments[index] != "--":
        option = arguments[index]
        if option == "--completion-file":
            if (completion_path is not None or index + 1 >= len(arguments)
                    or not arguments[index + 1] or arguments[index + 1] == "--"):
                print("invalid bounded-log completion file", file=sys.stderr)
                return 2
            completion_path = arguments[index + 1]
            index += 2
            continue
        if option not in ("--fail-on-overflow", "--stdin") or option in options:
            print("invalid bounded-log option", file=sys.stderr)
            return 2
        options.add(option)
        index += 1
    if index >= len(arguments) or arguments[index] != "--" or not arguments[index + 1:]:
        print("usage: bounded-log.py LOG_PATH MAX_BYTES [--fail-on-overflow] [--stdin] [--completion-file PATH] -- COMMAND [ARG ...]", file=sys.stderr)
        return 2
    fail_on_overflow = "--fail-on-overflow" in options
    pass_stdin = "--stdin" in options
    discard_output = log_name == "-"
    if limit < 128:
        print("invalid log size limit or empty command", file=sys.stderr)
        return 2

    marker = b"\n[bounded-log: output truncated]\n"
    content_limit = limit - len(marker)
    output = None
    if not discard_output:
        log_path = pathlib.Path(log_name)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(log_path, flags, 0o600)
            os.fchmod(descriptor, 0o600)
            output = os.fdopen(descriptor, "wb", buffering=0)
        except OSError as error:
            print(f"could not open bounded log: {error}", file=sys.stderr)
            return 1

    # The pidfd pins the session leader's numeric PID while its descendants
    # may outlive it. That prevents a later killpg() from targeting a reused
    # process-group number. Linux PR_SET_CHILD_SUBREAPER lets this wrapper reap
    # grandchildren after their parent exits during bounded teardown.
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))
    except (AttributeError, OSError) as error:
        if output is not None:
            output.close()
        print(f"could not enable safe child reaping: {error}", file=sys.stderr)
        return 1

    child = None
    group_identity = None
    termination_deadline = [None]
    kill_sent_at = [None]
    termination_requested = [False]
    pending_signal = [None]

    def request_termination(signum):
        if child is None or group_identity is None:
            pending_signal[0] = signum
            return
        if termination_requested[0]:
            return
        termination_requested[0] = True
        termination_deadline[0] = time.monotonic() + TERMINATION_GRACE_SECONDS
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass

    def forward(signum, _frame):
        request_termination(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    signal.signal(signal.SIGHUP, forward)

    try:
        child = subprocess.Popen(
            arguments[index + 1:],
            stdin=None if pass_stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as error:
        if output is not None:
            output.close()
        print(f"could not start logged child: {error}", file=sys.stderr)
        return 1

    try:
        group_identity = os.pidfd_open(child.pid, 0)
    except OSError as error:
        try:
            child.terminate()
            child.wait(timeout=KILL_SETTLE_SECONDS)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        if output is not None:
            output.close()
        print(f"could not pin logged child process-group identity: {error}", file=sys.stderr)
        return 1

    if pending_signal[0] is not None:
        request_termination(pending_signal[0])
    written = 0
    truncated = False
    cleanup_failed = False
    group_clean = False
    selector = selectors.DefaultSelector()

    def group_members():
        members = []
        try:
            entries = os.scandir("/proc")
        except OSError:
            return None
        with entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                try:
                    fields = pathlib.Path(entry.path, "stat").read_text().rsplit(")", 1)[1].split()
                    process_group = int(fields[2])
                    session = int(fields[3])
                except FileNotFoundError:
                    continue
                except (OSError, ValueError, IndexError):
                    return None
                if process_group == child.pid and session == child.pid:
                    members.append(int(entry.name))
        return members

    def reap_adopted_children():
        try:
            entries = os.scandir("/proc")
        except OSError:
            return
        with entries:
            for entry in entries:
                if not entry.name.isdigit() or int(entry.name) == child.pid:
                    continue
                try:
                    fields = pathlib.Path(entry.path, "stat").read_text().rsplit(")", 1)[1].split()
                    parent = int(fields[1])
                except (OSError, ValueError, IndexError):
                    continue
                if parent == os.getpid():
                    try:
                        os.waitpid(int(entry.name), os.WNOHANG)
                    except (ChildProcessError, ProcessLookupError):
                        pass

    try:
        assert child.stdout is not None
        stream_fd = child.stdout.fileno()
        os.set_blocking(stream_fd, False)
        selector.register(stream_fd, selectors.EVENT_READ)
        while True:
            child_done = child.poll() is not None
            if child_done or termination_deadline[0] is not None or kill_sent_at[0] is not None:
                members = group_members()
            else:
                members = []
            members_present = members is None or bool(members)
            if child_done and members_present and not termination_requested[0]:
                # A command must not leave same-session descendants behind,
                # even when they close both output streams before exiting.
                request_termination(signal.SIGTERM)

            if (termination_deadline[0] is not None and child_done
                    and members == [] and not selector.get_map()):
                # Nothing owned by this session remains; do not burn the full
                # grace period just because the direct child honored TERM.
                termination_deadline[0] = None

            if (not selector.get_map() and child_done and members == []
                    and termination_deadline[0] is None and kill_sent_at[0] is None):
                break
            if kill_sent_at[0] is not None and members == [] and child_done:
                break
            if (kill_sent_at[0] is not None
                    and time.monotonic() >= kill_sent_at[0] + KILL_SETTLE_SECONDS):
                cleanup_failed = True
                break

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
                    if output is not None:
                        output.write(portion)
                    written += len(portion)
                if len(chunk) > max(remaining, 0):
                    truncated = True
                    if fail_on_overflow:
                        request_termination(signal.SIGTERM)
            reap_adopted_children()
            if (termination_deadline[0] is not None
                    and time.monotonic() >= termination_deadline[0]):
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                termination_deadline[0] = None
                kill_sent_at[0] = time.monotonic()
        if child.poll() is None:
            try:
                child.wait(timeout=KILL_SETTLE_SECONDS)
            except subprocess.TimeoutExpired:
                cleanup_failed = True
        status = child.returncode if child.returncode is not None else 125
        reap_adopted_children()
        if truncated and output is not None:
            output.write(marker)
    finally:
        if output is not None:
            output.close()
        child.stdout.close()
        try:
            # Also cover exceptional exits from the drain loop. The open
            # pidfd keeps this original process-group ID from being reused.
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        settle_deadline = (
            kill_sent_at[0] + KILL_SETTLE_SECONDS
            if kill_sent_at[0] is not None
            else time.monotonic() + KILL_SETTLE_SECONDS
        )
        while True:
            if child.poll() is None:
                try:
                    child.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    pass
            reap_adopted_children()
            members = group_members()
            if child.returncode is not None and members == []:
                group_clean = True
                break
            if time.monotonic() >= settle_deadline:
                cleanup_failed = True
                break
            time.sleep(0.05)
        os.close(group_identity)
        selector.close()
    if completion_path is not None and not cleanup_failed and group_clean:
        try:
            fields = pathlib.Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()
            completion = {
                "logger_pid": os.getpid(),
                "logger_start_time": fields[19],
                "child_pid": child.pid,
                "process_group_clean": True,
            }
            descriptor = os.open(
                completion_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(completion, stream, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except (OSError, IndexError, ValueError) as error:
            cleanup_failed = True
            print(f"could not write bounded-log cleanup proof: {error}", file=sys.stderr)
    if cleanup_failed:
        print("logged child process group did not finish cleanup within its bounded grace", file=sys.stderr)
        return 125
    if fail_on_overflow and truncated:
        return 125
    if status < 0:
        return 128 - status
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
