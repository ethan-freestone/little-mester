#!/usr/bin/env python3
"""Terminal-independent owner parent supervisor with explicit lock handoff."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import secrets
import select
import signal
import socket
import stat
import subprocess
import sys
import time

_CHILD_PROCESSES: dict[str, tuple[subprocess.Popen, dict | None]] = {}


def _load_state():
    path = pathlib.Path(__file__).with_name("parent-state.py")
    spec = importlib.util.spec_from_file_location("parent_state", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("protected parent state module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_protected_module(name: str):
    path = pathlib.Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"parent_{name.replace('-', '_')}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"protected {name} module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _protected_tool(name: str, candidates: tuple[str, ...]) -> str:
    policy = _load_protected_module("executable-policy")
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            return str(policy.validate_executable(path))
        except (OSError, ValueError):
            continue
    raise RuntimeError(f"no protected {name} executable is available")


def _ensure_ssh_keypair(directory_fd: int, stem: str, ssh_keygen: str, *, create: bool) -> tuple[bytes, bytes]:
    private_name, public_name = stem, f"{stem}.pub"
    present = []
    for name in (private_name, public_name):
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            present.append(False)
            continue
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise RuntimeError(f"unsafe persistent SSH key file: {name}")
        present.append(True)
    if not any(present) and create:
        result = subprocess.run([ssh_keygen, "-q", "-t", "ed25519", "-N", "",
                                 "-f", f"/proc/self/fd/{directory_fd}/{private_name}"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, close_fds=True,
                                pass_fds=(directory_fd,), timeout=15, check=False)
        if result.returncode != 0:
            raise RuntimeError("persistent SSH key generation failed")
        for name in (private_name, public_name):
            os.chmod(name, 0o600, dir_fd=directory_fd, follow_symlinks=False)
        os.fsync(directory_fd)
    elif not all(present):
        raise RuntimeError(f"incomplete persistent SSH key pair: {stem}")
    private_fd = os.open(private_name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory_fd)
    public_fd = os.open(public_name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        private_info = os.fstat(private_fd)
        public_info = os.fstat(public_fd)
        for name, info in ((private_name, private_info), (public_name, public_info)):
            path_info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if ((info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino)
                    or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise RuntimeError(f"persistent SSH key changed during validation: {name}")
        private = os.read(private_fd, 65536)
        public = os.read(public_fd, 65536)
        line = public.decode("ascii").strip()
        if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?", line):
            raise RuntimeError(f"invalid persistent SSH public key: {public_name}")
        check = subprocess.run([ssh_keygen, "-y", "-f", f"/proc/self/fd/{private_fd}"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, close_fds=True,
                               pass_fds=(private_fd,), timeout=15, check=False)
        if check.returncode != 0 or check.stdout.decode("ascii").strip().split()[:2] != line.split()[:2]:
            raise RuntimeError(f"persistent SSH key pair does not match: {stem}")
        os.fsync(private_fd)
        os.fsync(public_fd)
        return private, public
    finally:
        os.close(private_fd)
        os.close(public_fd)


def _prepare_parent_seed(state, lock_fd: int, home: str | None = None) -> bytes:
    directory_fd = state._open_directory_chain(create=False, home_override=home)
    if directory_fd is None:
        raise state.Refused("private parent state is absent")
    try:
        state._verify_lock_for_dir(directory_fd, lock_fd)
        parent = state._read_json(directory_fd, "parent.json")
        state.validate_parent(parent)
        try:
            seed_info = os.stat("seed.iso", dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            seed_info = None
        create = seed_info is None
        if create and parent.get("ssh_identity") is not None:
            raise state.Refused("recorded persistent SSH seed was lost; refusing identity regeneration")
        if create and "seed.iso.part" in os.listdir(directory_fd):
            raise state.Refused("preserving existing incomplete seed ISO artifact")
        ssh_keygen = _protected_tool("ssh-keygen", ("/usr/bin/ssh-keygen",))
        _control_private, control_public = _ensure_ssh_keypair(
            directory_fd, "id_vm_control", ssh_keygen, create=create)
        host_private, host_public = _ensure_ssh_keypair(
            directory_fd, "ssh_host_ed25519_key", ssh_keygen, create=create)
        if seed_info is not None:
            if (not stat.S_ISREG(seed_info.st_mode) or seed_info.st_uid != os.getuid()
                    or stat.S_IMODE(seed_info.st_mode) != 0o600 or seed_info.st_nlink != 1):
                raise state.Refused("persistent cloud-init seed is unsafe")
            state._validate_seed_identity(directory_fd, parent)
        else:
            user_data = {
                "hostname": "little-mester-parent",
                "ssh_pwauth": False,
                "disable_root": True,
                "ssh_deletekeys": False,
                "ssh_keys": {"ed25519_private": host_private.decode("ascii"),
                             "ed25519_public": host_public.decode("ascii").strip()},
                "users": [{"name": "mester", "uid": 1000, "primary_group": "mester",
                           "groups": ["users"], "shell": "/bin/bash", "lock_passwd": True,
                           "ssh_authorized_keys": [control_public.decode("ascii").strip()]}],
                "groups": ["mester"],
                "write_files": [
                    {"path": "/etc/ssh/sshd_config.d/99-little-mester-parent.conf", "owner": "root:root",
                     "permissions": "0644", "content": "HostKeyAlgorithms ssh-ed25519\n"},
                    {"path": "/etc/profile.d/little-mester-parent.sh", "owner": "root:root",
                     "permissions": "0644", "content": "export HTTP_PROXY=http://10.0.2.100:3128\nexport HTTPS_PROXY=http://10.0.2.100:3128\nexport http_proxy=http://10.0.2.100:3128\nexport https_proxy=http://10.0.2.100:3128\n"},
                ],
                "runcmd": [["bash", "-lc", "install -d -o mester -g mester -m 0750 /home/mester/workspace && systemctl restart ssh"]],
            }
            userdata = b"#cloud-config\n" + json.dumps(user_data, separators=(",", ":")).encode("utf-8") + b"\n"
            metadata = b"instance-id: little-mester-parent-default\nlocal-hostname: little-mester-parent\n"
            state._atomic_bytes(directory_fd, "user-data", userdata)
            state._atomic_bytes(directory_fd, "meta-data", metadata)
            iso = _protected_tool("genisoimage", ("/usr/bin/genisoimage", "/usr/bin/mkisofs"))
            part_name = "seed.iso.part"
            try:
                os.stat(part_name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise state.Refused("preserving existing incomplete seed ISO artifact")
            output_fd = os.open(part_name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                0o600, dir_fd=directory_fd)
            try:
                result = subprocess.run([iso, "-quiet", "-rock", "-joliet", "-output",
                                         f"/proc/self/fd/{output_fd}", "-V", "cidata",
                                         f"/proc/self/fd/{directory_fd}/user-data",
                                         f"/proc/self/fd/{directory_fd}/meta-data"],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, close_fds=True,
                                        pass_fds=(directory_fd, output_fd), timeout=30, check=False)
                if result.returncode != 0:
                    raise RuntimeError("parent cloud-init seed creation failed; state retained")
                os.fchmod(output_fd, 0o600)
                os.fsync(output_fd)
                output_info = os.fstat(output_fd)
                path_info = os.stat(part_name, dir_fd=directory_fd, follow_symlinks=False)
                if (not stat.S_ISREG(output_info.st_mode) or output_info.st_uid != os.getuid()
                        or stat.S_IMODE(output_info.st_mode) != 0o600 or output_info.st_nlink != 1
                        or (output_info.st_dev, output_info.st_ino) != (path_info.st_dev, path_info.st_ino)):
                    raise state.Refused("generated parent seed ISO has unsafe or substituted identity")
                seed_digest = _hash_fd(output_fd)
                os.link(part_name, "seed.iso", src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                        follow_symlinks=False)
                published = os.stat("seed.iso", dir_fd=directory_fd, follow_symlinks=False)
                if (published.st_dev, published.st_ino) != (output_info.st_dev, output_info.st_ino):
                    raise state.Refused("published parent seed ISO identity changed")
                os.unlink(part_name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            finally:
                os.close(output_fd)
            parent["ssh_identity"] = {
                "seed_sha256": seed_digest,
                "control_private_sha256": hashlib.sha256(_control_private).hexdigest(),
                "control_public_sha256": hashlib.sha256(control_public).hexdigest(),
                "host_private_sha256": hashlib.sha256(host_private).hexdigest(),
                "host_public_sha256": hashlib.sha256(host_public).hexdigest(),
            }
            state._atomic_json(directory_fd, "parent.json", parent)
        state._validate_seed_identity(directory_fd, parent)
        return host_public
    finally:
        os.close(directory_fd)


def _hash_fd(fd: int) -> str:
    digest = hashlib.sha256()
    position = 0
    while chunk := os.pread(fd, 1024 * 1024, position):
        digest.update(chunk)
        position += len(chunk)
    return digest.hexdigest()


def _write_ssh_connection(state, directory_fd: int, port: int, host_public: bytes) -> None:
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("invalid parent SSH forwarding port")
    host_key = host_public.decode("ascii").strip().split()
    if len(host_key) < 2 or host_key[0] != "ssh-ed25519":
        raise ValueError("invalid persistent parent SSH host key")
    state._atomic_bytes(directory_fd, "known_hosts",
                        f"[127.0.0.1]:{port} {host_key[0]} {host_key[1]}\n".encode("ascii"))
    state._atomic_bytes(directory_fd, "ssh_port", f"{port}\n".encode("ascii"))


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


def _spawn_gated_target(state, *, lock_fd: int, role: str, executable: str,
                        argv: list[str], home: str | None = None,
                        disk_fd: int = -1) -> dict:
    """Borrow descriptors; spawn only after durably recording the gate transition."""
    if (not isinstance(role, str) or not re.fullmatch(r"[a-z0-9-]{1,48}", role)
            or not isinstance(executable, str) or not executable.startswith("/")
            or os.path.realpath(executable) != executable
            or not isinstance(argv, list) or not argv or argv[0] != executable
            or not all(isinstance(item, str) for item in argv)
            or type(lock_fd) is not int or lock_fd < 3
            or type(disk_fd) is not int or disk_fd != -1 and disk_fd < 3
            or disk_fd >= 0 and disk_fd == lock_fd):
        raise state.Refused("invalid gated child target")
    if role in _CHILD_PROCESSES:
        raise state.Refused("a child with this role is already owned by the supervisor")
    directory_fd = state._open_directory_chain(create=False, home_override=home)
    if directory_fd is None:
        raise state.Refused("parent state is absent")
    reader = writer = -1
    proc = None
    transition = None
    durable = False
    completed = False
    target_name = f"child-{role}.target.json"
    identity_name = f"child-{role}.identity.json"
    try:
        if disk_fd >= 0 and disk_fd == directory_fd:
            raise state.Refused("parent disk descriptor aliases the state directory descriptor")
        state._verify_lock_for_dir(directory_fd, lock_fd)
        if disk_fd >= 0:
            info = os.fstat(disk_fd)
            path_info = os.stat("disk.qcow2", dir_fd=directory_fd, follow_symlinks=False)
            access_mode = fcntl.fcntl(disk_fd, fcntl.F_GETFL) & os.O_ACCMODE
            parent = state._read_json(directory_fd, "parent.json")
            state.validate_parent(parent)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or access_mode != os.O_RDWR or (info.st_dev, info.st_ino) !=
                    (path_info.st_dev, path_info.st_ino)
                    or (info.st_dev, info.st_ino) !=
                    (parent["disk"]["device"], parent["disk"]["inode"])):
                raise state.Refused("parent disk descriptor does not match its private recorded inode")
            fd_contracts = [{"fd": 3, "purpose": "parent-disk", "device": info.st_dev,
                             "inode": info.st_ino, "access": "read-write", "fdset": 1}]
        else:
            fd_contracts = []
        state._atomic_json(directory_fd, target_name,
                           {"executable": executable, "argv": argv, "fd_contracts": fd_contracts})
        token = secrets.token_hex(16)
        reader, writer = os.pipe2(os.O_CLOEXEC)
        state_fd_arg = str(directory_fd)
        disk_fd_arg = str(disk_fd)
        gate_path = str(pathlib.Path(__file__).with_name("parent-gate.py").resolve())
        python = os.path.realpath(sys.executable)
        gate_argv = [python, "-I", gate_path, "--release-fd", str(reader), "--token", token,
                     "--target", target_name, "--identity", identity_name,
                     "--state-dir-fd", state_fd_arg, "--disk-fd", disk_fd_arg]
        pass_fds = tuple(dict.fromkeys(fd for fd in (reader, directory_fd, disk_fd) if fd >= 0))
        proc = subprocess.Popen(gate_argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, close_fds=True, pass_fds=pass_fds,
                                start_new_session=True)
        _CHILD_PROCESSES[role] = (proc, None)
        os.close(reader)
        reader = -1
        deadline = time.monotonic() + 3
        while True:
            if proc.poll() is not None:
                raise state.Refused("gated child exited before durable transition")
            try:
                gate_identity = state.observe_process(proc.pid)
                transition = state.create_exec_transition(
                    gate_identity, executable, argv, release_token=token,
                    target_path=target_name, identity_path=identity_name,
                    fd_contracts=fd_contracts)
                break
            except state.Refused:
                if time.monotonic() >= deadline:
                    raise state.Refused("gated child did not reach its exact protected invocation")
                time.sleep(0.01)
        state.persist_exec_transition(directory_fd, identity_name, transition, lock_fd=lock_fd)
        durable = True
        _CHILD_PROCESSES[role] = (proc, transition)
        payload = token.encode("ascii")
        written = 0
        while written < len(payload):
            written += os.write(writer, payload[written:])
        os.close(writer)
        writer = -1
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            phase = state.exec_transition_status(transition)
            if phase == "TARGET":
                completed = True
                return transition
            if phase in ("STOPPED", "AMBIGUOUS"):
                raise state.Refused(f"gated child target failed identity transition: {phase}")
            time.sleep(0.01)
        raise state.Refused("gated child did not reach its exact target identity")
    finally:
        for fd in (reader, writer):
            if fd >= 0:
                os.close(fd)
        os.close(directory_fd)
        if proc is not None and not completed:
            cleanup_ok = False
            try:
                if durable and transition is not None:
                    cleanup_ok = state.stop_exec_transition(transition) == "STOPPED"
                else:
                    proc.wait(timeout=3)
                    cleanup_ok = True
            except (OSError, subprocess.TimeoutExpired):
                cleanup_ok = False
            if cleanup_ok:
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    cleanup_ok = False
            if cleanup_ok and _CHILD_PROCESSES.get(role, (None, None))[0] is proc:
                _CHILD_PROCESSES.pop(role, None)


def _stop_gated_target(state, role: str, transition: dict, *, term_grace_ms: int = 5000,
                       settle_ms: int = 2000) -> str:
    """Stop only the durable gate→target identity, then reap its owned child."""
    if (not isinstance(transition, dict)
            or transition.get("target_path") != f"child-{role}.target.json"
            or transition.get("identity_path") != f"child-{role}.identity.json"):
        return "AMBIGUOUS"
    owned = _CHILD_PROCESSES.get(role)
    if owned is not None and owned[1] != transition:
        return "AMBIGUOUS"
    result = state.stop_exec_transition(transition, term_grace_ms=term_grace_ms,
                                        settle_ms=settle_ms)
    if result != "STOPPED":
        return result
    proc = owned[0] if owned is not None else None
    if proc is not None:
        try:
            proc.wait(timeout=(term_grace_ms + settle_ms) / 1000 + 1)
        except subprocess.TimeoutExpired:
            return "AMBIGUOUS"
        if _CHILD_PROCESSES.get(role, (None, None))[0] is proc:
            _CHILD_PROCESSES.pop(role, None)
    return "STOPPED"


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
    stop_requested = False

    def request_stop(_signum, _frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
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
