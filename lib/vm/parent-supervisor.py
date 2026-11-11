#!/usr/bin/env python3
"""Terminal-independent owner parent supervisor with explicit lock handoff."""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import os
import pathlib
import select
import signal
import socket
import stat
import sys
import time
import time


def _load_state():
    path = pathlib.Path(__file__).with_name("parent-state.py")
    spec = importlib.util.spec_from_file_location("parent_state", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("protected parent state module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_commit(fd: int, timeout_seconds: float = 60.0) -> bool:
    chunks = bytearray()
    deadline = time.monotonic() + timeout_seconds
    poller = select.poll()
    poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    while len(chunks) <= len(b"COMMIT\n"):
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        if not poller.poll(remaining_ms):
            return False
        chunk = os.read(fd, len(b"COMMIT\n") + 1 - len(chunks))
        if not chunk:
            return False
        chunks.extend(chunk)
        if len(chunks) >= len(b"COMMIT\n"):
            break
    if bytes(chunks) != b"COMMIT\n":
        return False
    while True:
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        if not poller.poll(remaining_ms):
            return False
        if os.read(fd, 1) == b"":
            return True
        return False


def _listen(state, directory_fd: int, home: str | None = None) -> tuple[socket.socket, tuple[int, int]]:
    home = pathlib.Path(home if home is not None else state.pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
    state_path = home.joinpath(*state.STATE_COMPONENTS)
    socket_path = state_path / "supervisor.sock"
    try:
        info = os.stat("supervisor.sock", dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise state.Refused(f"refusing pre-existing supervisor socket inode {info.st_ino}")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    created = False
    socket_identity = None
    try:
        listener.bind(str(socket_path))
        created = True
        info = os.lstat(socket_path)
        socket_identity = (info.st_dev, info.st_ino)
        os.chmod(socket_path, 0o600, follow_symlinks=False)
        info = os.lstat(socket_path)
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or socket_identity != (info.st_dev, info.st_ino)):
            raise state.Refused("supervisor control socket has unsafe owner, type, or mode")
        listener.listen(8)
        listener.settimeout(0.5)
        return listener, socket_identity
    except BaseException:
        if created:
            try:
                path_info = os.stat("supervisor.sock", dir_fd=directory_fd, follow_symlinks=False)
                if socket_identity == (path_info.st_dev, path_info.st_ino):
                    os.unlink("supervisor.sock", dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        listener.close()
        raise


def _record_supervisor(state, directory_fd: int, parent_id: str, run_id: str, process: dict) -> None:
    record = {"schema": state.SCHEMA, "parent_id": parent_id, "run_id": run_id,
              "phase": "STARTING", "supervisor": process}
    state._atomic_json(directory_fd, "active-run.json", record)


def _unlink_owned_socket(state, listener, socket_identity, home: str | None) -> None:
    if listener is None or socket_identity is None:
        return
    directory_fd = state._open_directory_chain(create=False, home_override=home)
    if directory_fd is None:
        return
    try:
        try:
            path_info = os.stat("supervisor.sock", dir_fd=directory_fd, follow_symlinks=False)
            if (stat.S_ISSOCK(path_info.st_mode) and socket_identity == (path_info.st_dev, path_info.st_ino)
                    and path_info.st_uid == os.getuid()):
                os.unlink("supervisor.sock", dir_fd=directory_fd)
                os.fsync(directory_fd)
        except FileNotFoundError:
            pass
    finally:
        os.close(directory_fd)


def _finalize(state, args, home, listener, socket_identity) -> None:
    try:
        directory_fd = state._open_directory_chain(create=False, home_override=home)
        if directory_fd is not None:
            try:
                try:
                    run = state._read_json(directory_fd, "active-run.json")
                    if run.get("run_id") == args.run_id and run.get("supervisor", {}).get("pid") == os.getpid():
                        run["phase"] = "STOPPING"
                        state._atomic_json(directory_fd, "active-run.json", run)
                except FileNotFoundError:
                    pass
            finally:
                os.close(directory_fd)
    finally:
        try:
            _unlink_owned_socket(state, listener, socket_identity, home)
        finally:
            listener.close()
            os.close(args.lock_fd)


def run(args) -> int:
    state = _load_state()
    home = str(pathlib.Path(args.state_home).resolve(strict=True)) if args.state_home else None
    if args.parent_id != state.PARENT_ID or len(args.run_id) != 32 or any(ch not in "0123456789abcdef" for ch in args.run_id):
        print("REFUSED: invalid parent or run identity", file=sys.stderr)
        return 2
    listener = None
    socket_identity = None
    try:
        if args.lock_fd < 3 or args.handoff_fd < 3 or args.lock_fd == args.handoff_fd:
            raise state.Refused("invalid or overlapping supervisor descriptors")
        os.set_inheritable(args.lock_fd, False)
        os.set_inheritable(args.handoff_fd, False)
        fcntl.flock(args.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state._verify_lock_fd(args.lock_fd, home=home)
        if not _read_commit(args.handoff_fd):
            return 2
        os.close(args.handoff_fd)
        directory_fd = state._open_directory_chain(create=False, home_override=home)
        if directory_fd is None:
            raise state.Refused("parent state disappeared during lock handoff")
        try:
            parent = state._read_json(directory_fd, "parent.json")
            state.validate_parent(parent)
            supervisor_identity = state.observe_process(os.getpid())
            if (supervisor_identity["exe"] != os.path.realpath(sys.executable)
                    or supervisor_identity["argv"] != [os.fsdecode(value) for value in pathlib.Path(
                            f"/proc/{os.getpid()}/cmdline").read_bytes().split(b"\0")[:-1]]):
                raise state.Refused("supervisor exact process identity changed during startup")
            listener, socket_identity = _listen(state, directory_fd, home)
            _record_supervisor(state, directory_fd, args.parent_id, args.run_id, supervisor_identity)
        finally:
            os.close(directory_fd)
    except (OSError, ValueError, state.Refused) as error:
        if listener is not None:
            _unlink_owned_socket(state, listener, socket_identity, home)
            listener.close()
        print(f"REFUSED: supervisor startup failed: {error}", file=sys.stderr)
        return 2

    stop_requested = False

    def request_stop(_signum, _frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        while not stop_requested:
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            with connection:
                try:
                    connection.settimeout(1.0)
                    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                    _pid, uid, _gid = __import__("struct").unpack("3i", credentials)
                    if uid != os.getuid():
                        connection.sendall(b"REFUSED\n")
                        continue
                    command = connection.recv(32)
                    if command != b"STATUS\n" and command != b"STOP\n":
                        connection.sendall(b"REFUSED\n")
                        continue
                    if command == b"STATUS\n":
                        connection.sendall(b"STARTING\n")
                        continue
                    connection.sendall(b"STOPPING\n")
                    stop_requested = True
                except OSError:
                    continue
    finally:
        _finalize(state, args, home, listener, socket_identity)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--lock-fd", type=int, required=True)
    parser.add_argument("--handoff-fd", type=int, required=True)
    parser.add_argument("--state-home")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
