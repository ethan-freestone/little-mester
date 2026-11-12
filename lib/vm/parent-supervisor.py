#!/usr/bin/env python3
"""Terminal-independent owner parent supervisor with explicit lock handoff."""

from __future__ import annotations

import argparse
import base64
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
import struct
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
            plugin_root = pathlib.Path(__file__).resolve().parents[2] / "agent/opencode/plugins/little-mester-exit-confirm"
            plugin_files = []
            for name, destination in (("package.json", "/etc/opencode/plugins/little-mester-exit-confirm/package.json"),
                                      ("tui.tsx", "/etc/opencode/plugins/little-mester-exit-confirm/tui.tsx")):
                source = plugin_root / name
                info = os.lstat(source)
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
                    raise state.Refused(f"protected OpenCode plugin asset is unsafe: {name}")
                payload = source.read_bytes()
                plugin_files.append({"path": destination, "owner": "root:root", "permissions": "0644",
                                     "encoding": "b64",
                                     "content": base64.b64encode(payload).decode("ascii")})
            protected_config = json.dumps({"autoupdate": False, "share": "disabled",
                                           "permission": {"edit": "ask", "bash": "ask"}},
                                          separators=(",", ":"))
            cli_config = json.dumps({
                "plugins": ["/etc/opencode/plugins/little-mester-exit-confirm"],
                "keybinds": {"app.exit": "none", "prompt.clear": "none"},
            }, separators=(",", ":"))
            cli_config_export = f"export OPENCODE_CLI_CONFIG_CONTENT='{cli_config}'\n"
            opencode_runner = (
                "#!/bin/sh\n"
                "set -eu\n"
                "export XDG_CONFIG_HOME=/etc\n"
                "export OPENCODE_CONFIG=/etc/opencode/opencode.json\n"
                + cli_config_export +
                "export OPENCODE_DISABLE_PROJECT_CONFIG=1\n"
                "exec /usr/local/bin/opencode\n")
            opencode_launcher = "#!/bin/sh\nset -eu\nexec tmux new-session -A -s little-mester-opencode /usr/local/libexec/lm-opencode\n"
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
                    {"path": "/etc/opencode/opencode.json", "owner": "root:root",
                     "permissions": "0644", "content": protected_config},
                    {"path": "/etc/profile.d/little-mester-opencode.sh", "owner": "root:root",
                     "permissions": "0644", "content": "export OPENCODE_CONFIG=/etc/opencode/opencode.json\n" +
                     cli_config_export + "export OPENCODE_DISABLE_PROJECT_CONFIG=1\n"},
                    {"path": "/usr/local/bin/lm-opencode", "owner": "root:root",
                     "permissions": "0755", "content": opencode_launcher},
                    {"path": "/usr/local/libexec/lm-opencode", "owner": "root:root",
                     "permissions": "0755", "content": opencode_runner},
                    *plugin_files,
                ],
                "packages": ["tmux", "nodejs", "npm"],
                "runcmd": [["bash", "-lc", "install -d -o mester -g mester -m 0750 /home/mester/workspace && npm install --global @opencode/cli@2.2.5 && test \"$(opencode --version)\" = 'opencode v2.2.5' && chown root:root /usr/local/bin/opencode && chmod 0755 /usr/local/bin/opencode && systemctl restart ssh"]],
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


def _record_supervisor(state, directory_fd: int, parent_id: str, run_id: str, process: dict) -> None:
    record = {"schema": state.SCHEMA, "parent_id": parent_id, "run_id": run_id,
              "phase": "STARTING", "supervisor": process}
    state._atomic_json(directory_fd, "active-run.json", record)


def _commit_running_after_readiness(state, *, run_id: str, home: str | None,
                                   lock_fd: int, qemu_transition: dict, readiness) -> dict:
    """Publish RUNNING only after readiness and two exact QEMU transition checks."""
    if (not callable(readiness)
            or not isinstance(qemu_transition, dict)
            or qemu_transition.get("target_path") != "child-qemu.target.json"
            or qemu_transition.get("identity_path") != "child-qemu.identity.json"):
        raise state.Refused("invalid parent readiness transition")
    if state.exec_transition_status(qemu_transition) != "TARGET":
        raise state.Refused("QEMU is not the exact durable target before readiness")
    if readiness() is not True:
        raise state.Refused("guest SSH readiness was not established")
    if state.exec_transition_status(qemu_transition) != "TARGET":
        raise state.Refused("QEMU identity changed during guest readiness")
    qemu = state.observe_process(qemu_transition["pid"])
    target = qemu_transition["target"]
    if qemu["exe"] != target["exe"] or qemu["argv"] != target["argv"]:
        raise state.Refused("observed QEMU process differs from the durable launch target")
    directory_fd = state._open_directory_chain(create=False, home_override=home)
    if directory_fd is None:
        raise state.Refused("parent state disappeared before RUNNING publication")
    try:
        state._verify_lock_for_dir(directory_fd, lock_fd)
        run = state._read_json(directory_fd, "active-run.json")
        if (run.get("run_id") != run_id or run.get("phase") != "STARTING"
                or run.get("supervisor", {}).get("pid") != os.getpid()):
            raise state.Refused("parent STARTING record changed before readiness commit")
        run["phase"] = "RUNNING"
        run["qemu"] = qemu
        state._atomic_json(directory_fd, "active-run.json", run)
        return qemu
    finally:
        os.close(directory_fd)


def _read_private_port(state, home: str | None, name: str) -> int:
    directory_fd = state._open_directory_chain(create=False, home_override=home)
    if directory_fd is None:
        raise state.Refused("parent state disappeared while reading proxy readiness")
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory_fd)
        except OSError as error:
            raise state.Refused("parent proxy readiness port file is unavailable") from error
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or info.st_size > 8):
                raise state.Refused("parent proxy readiness port file is unsafe")
            raw = os.read(fd, 9)
            if len(raw) > 8:
                raise state.Refused("parent proxy readiness port exceeds its size bound")
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)
    try:
        port = int(raw.decode("ascii").strip())
    except (ValueError, UnicodeError) as error:
        raise state.Refused("parent proxy returned a malformed port") from error
    if not 1024 <= port <= 65535:
        raise state.Refused("parent proxy returned an invalid port")
    return port


def _wait_for_proxy(state, home: str | None, transition: dict, port_name: str,
                    *, timeout_seconds: float = 10.0) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        phase = state.exec_transition_status(transition)
        if phase != "TARGET":
            raise state.Refused(f"parent proxy identity became {phase} before readiness")
        try:
            return _read_private_port(state, home, port_name)
        except state.Refused:
            time.sleep(0.05)
    raise state.Refused("parent egress proxy did not become ready before its deadline")


def _wait_for_guest_readiness(state, home: str | None, qemu_transition: dict,
                              *, stop_requested, timeout_seconds: float = 900.0) -> bool:
    """Poll bounded, host-key-pinned SSH readiness while verifying exact QEMU identity."""
    ssh = _protected_tool("ssh", ("/usr/bin/ssh",))
    deadline = time.monotonic() + timeout_seconds
    command = state.PARENT_READY_COMMAND
    while time.monotonic() < deadline:
        if stop_requested():
            return False
        if state.exec_transition_status(qemu_transition) != "TARGET":
            raise state.Refused("QEMU identity changed before guest SSH readiness")
        argv, descriptors = state.parent_ssh_plan(home=home, allow_starting=True,
                                                   remote_command=command, tty=False)
        try:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, close_fds=True,
                                    pass_fds=tuple(descriptors), timeout=8, check=False)
        except subprocess.TimeoutExpired:
            result = None
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
        if result is not None and result.returncode == 0:
            if state.exec_transition_status(qemu_transition) != "TARGET":
                raise state.Refused("QEMU identity changed during guest SSH readiness")
            return True
        time.sleep(1)
    return False


def _start_parent_children(state, args, home: str | None, *, stop_requested) -> None:
    """Start the configured proxy and persistent QEMU, then publish verified readiness."""
    if not isinstance(args.ssh_port, int) or not 1024 <= args.ssh_port <= 65535:
        raise state.Refused("parent SSH forwarding port is invalid")
    directory_fd = state._open_directory_chain(create=False, home_override=home)
    if directory_fd is None:
        raise state.Refused("parent state is absent")
    try:
        parent = state._read_json(directory_fd, "parent.json")
        state.validate_parent(parent)
        if not parent["disk"]["provisioned"]:
            raise state.Refused("parent disk is not provisioned")
        if parent.get("ssh_identity") is None:
            raise state.Refused("persistent parent seed and SSH identity are absent")
        state._validate_seed_identity(directory_fd, parent)
        names = set(os.listdir(directory_fd))
        if "proxy.port" in names:
            raise state.Refused("preserving stale parent proxy port evidence")
        if "qmp.sock" in names:
            raise state.Refused("preserving stale parent QMP socket evidence")
        proxy_port_path = str(pathlib.Path(home if home is not None else state.pwd.getpwuid(os.getuid()).pw_dir)
                              .joinpath(*state.STATE_COMPONENTS, "proxy.port"))
        qmp_path = str(pathlib.Path(home if home is not None else state.pwd.getpwuid(os.getuid()).pw_dir)
                       .joinpath(*state.STATE_COMPONENTS, "qmp.sock"))
        seed_path = str(pathlib.Path(home if home is not None else state.pwd.getpwuid(os.getuid()).pw_dir)
                        .joinpath(*state.STATE_COMPONENTS, "seed.iso"))
        host_key_fd = os.open("ssh_host_ed25519_key.pub", os.O_RDONLY | os.O_NOFOLLOW,
                              dir_fd=directory_fd)
        try:
            host_key = os.read(host_key_fd, 4096)
        finally:
            os.close(host_key_fd)
        _write_ssh_connection(state, directory_fd, args.ssh_port, host_key)
        disk_fd = os.open("disk.qcow2", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                          dir_fd=directory_fd)
    finally:
        os.close(directory_fd)

    try:
        python = _protected_tool("python3", (sys.executable, "/usr/bin/python3"))
        proxy_script = str(pathlib.Path(__file__).with_name("proxy.py").resolve())
        proxy_transition = _spawn_gated_target(
            state, lock_fd=args.lock_fd, role="proxy", executable=python,
            argv=[python, "-I", proxy_script, "--port-file", proxy_port_path], home=home)
        proxy_port = _wait_for_proxy(state, home, proxy_transition, "proxy.port")
        qemu = _protected_tool("qemu-system-x86_64", ("/usr/bin/qemu-system-x86_64",))
        qemu_module = _load_protected_module("parent-qemu")
        qemu_argv = qemu_module.build_qemu_argv(
            executable=qemu, seed_iso=seed_path, ssh_port=args.ssh_port,
            proxy_port=proxy_port, qmp_socket=qmp_path)
        qemu_transition = _spawn_gated_target(
            state, lock_fd=args.lock_fd, role="qemu", executable=qemu,
            argv=qemu_argv, home=home, disk_fd=disk_fd)
    finally:
        os.close(disk_fd)

    if not _wait_for_guest_readiness(state, home, qemu_transition,
                                     stop_requested=stop_requested):
        raise state.Refused("guest OpenCode bootstrap did not become SSH-ready")
    _commit_running_after_readiness(state, run_id=args.run_id, home=home,
                                    lock_fd=args.lock_fd, qemu_transition=qemu_transition,
                                    readiness=lambda: True)


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


def _qmp_powerdown(state, qmp_path: str, qemu_identity: dict, *, timeout_seconds: float = 5.0) -> None:
    """Request guest ACPI shutdown through a private socket bound to exact QEMU identity."""
    if (not isinstance(qmp_path, str) or not qmp_path.startswith("/")
            or len(os.fsencode(qmp_path)) > 100
            or not isinstance(qemu_identity, dict) or not 0 < timeout_seconds <= 10):
        raise state.Refused("invalid QMP shutdown arguments")
    try:
        info = os.lstat(qmp_path)
    except OSError as error:
        raise state.Refused("private QMP socket is unavailable") from error
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise state.Refused("private QMP socket has unsafe owner, type, or mode")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    deadline = time.monotonic() + timeout_seconds

    def set_remaining_timeout() -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise state.Refused("QMP exchange exceeded its bounded deadline")
        client.settimeout(remaining)

    try:
        set_remaining_timeout()
        client.connect(qmp_path)
        credentials = client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        peer_pid, peer_uid, _peer_gid = struct.unpack("3i", credentials)
        if peer_pid != qemu_identity.get("pid") or peer_uid != qemu_identity.get("uid"):
            raise state.Refused("QMP peer does not match the recorded QEMU identity")
        if state._recorded_process_status(qemu_identity, expected_basename="qemu-system-x86_64",
                                         current_boot_id=qemu_identity.get("host_boot_id", "")) != "MATCH":
            raise state.Refused("QMP peer failed exact QEMU process identity verification")

        buffer = bytearray()

        def receive_line() -> dict:
            while b"\n" not in buffer:
                if len(buffer) >= 65536:
                    raise state.Refused("QMP response exceeded its bounded deadline or size")
                set_remaining_timeout()
                chunk = client.recv(min(4096, 65536 - len(buffer)))
                if not chunk:
                    raise state.Refused("QMP peer closed before completing its response")
                buffer.extend(chunk)
            line, _, remaining = buffer.partition(b"\n")
            buffer[:] = remaining
            try:
                response = json.loads(line)
            except (UnicodeError, json.JSONDecodeError) as error:
                raise state.Refused("QMP peer returned malformed JSON") from error
            if not isinstance(response, dict):
                raise state.Refused("QMP peer returned an invalid message")
            return response

        if not isinstance(receive_line().get("QMP"), dict):
            raise state.Refused("QMP peer did not send the protocol greeting")
        for command in ("qmp_capabilities", "system_powerdown"):
            set_remaining_timeout()
            client.sendall((json.dumps({"execute": command}, separators=(",", ":")) + "\r\n").encode())
            while True:
                response = receive_line()
                if "event" in response:
                    continue
                if "error" in response or not isinstance(response.get("return"), dict):
                    raise state.Refused(f"QMP rejected {command}")
                break
    except (OSError, TimeoutError) as error:
        raise state.Refused("could not complete bounded identity-verified QMP shutdown") from error
    finally:
        client.close()


def _shutdown_running_qemu(state, args, home) -> None:
    owned = _CHILD_PROCESSES.get("qemu")
    if owned is None or owned[1] is None:
        raise state.Refused("RUNNING QEMU has no supervisor-owned durable transition")
    proc, transition = owned
    if state.exec_transition_status(transition) != "TARGET":
        raise state.Refused("RUNNING QEMU transition is not the exact live target")
    directory = pathlib.Path(home if home is not None else state.pwd.getpwuid(os.getuid()).pw_dir)
    qmp_path = str(directory.joinpath(*state.STATE_COMPONENTS, "qmp.sock"))
    fd = state._open_directory_chain(create=False, home_override=home)
    if fd is None:
        raise state.Refused("parent state disappeared before QMP shutdown")
    try:
        run = state._read_json(fd, "active-run.json")
        if (run.get("run_id") != args.run_id or run.get("phase") != "RUNNING"
                or run.get("supervisor", {}).get("pid") != os.getpid()
                or not isinstance(run.get("qemu"), dict)):
            raise state.Refused("parent RUNNING record changed before QMP shutdown")
        qemu_identity = run["qemu"]
    finally:
        os.close(fd)
    _qmp_powerdown(state, qmp_path, qemu_identity)
    deadline = time.monotonic() + 60
    had_proxy = "proxy" in _CHILD_PROCESSES
    while time.monotonic() < deadline:
        proc.poll()  # Reap the owned process before evaluating its procfs identity.
        phase = state.exec_transition_status(transition)
        if phase == "STOPPED":
            proc.wait()
            _CHILD_PROCESSES.pop("qemu", None)
            for role in ("metrics", "proxy"):
                helper = _CHILD_PROCESSES.get(role)
                if helper is None:
                    continue
                if helper[1] is None:
                    raise state.Refused(f"supervised {role} child has no durable identity transition")
                if _stop_gated_target(state, role, helper[1]) != "STOPPED":
                    raise state.Refused(f"supervised {role} child did not stop with verified identity")
            if _CHILD_PROCESSES:
                raise state.Refused("supervisor still owns children after ordered shutdown")
            _remove_qmp_socket(state, transition, home)
            if had_proxy:
                _remove_proxy_port_file(state, home)
            return
        if phase != "TARGET":
            raise state.Refused(f"QEMU identity became {phase} during graceful shutdown")
        time.sleep(0.05)
    raise state.Refused("guest/QEMU did not stop before the graceful shutdown deadline")


def _stop_starting_children(state, home: str | None = None) -> None:
    """Roll back only recorded startup children, in bounded identity-bound order."""
    had_proxy = "proxy" in _CHILD_PROCESSES
    had_qemu = "qemu" in _CHILD_PROCESSES
    qemu_transition = _CHILD_PROCESSES.get("qemu", (None, None))[1]
    for role in ("qemu", "metrics", "proxy"):
        owned = _CHILD_PROCESSES.get(role)
        if owned is None:
            continue
        if owned[1] is None:
            raise state.Refused(f"STARTING {role} child has no durable identity transition")
        if _stop_gated_target(state, role, owned[1]) != "STOPPED":
            raise state.Refused(f"STARTING {role} child did not stop with verified identity")
    if _CHILD_PROCESSES:
        raise state.Refused("supervisor owns an unrecognized child during startup rollback")
    if had_qemu:
        _remove_qmp_socket(state, qemu_transition, home)
    if had_proxy:
        _remove_proxy_port_file(state, home)


def _remove_qmp_socket(state, qemu_transition: dict | None,
                       home: str | None = None) -> None:
    """Remove only a private QMP socket after the exact owned QEMU has exited."""
    if (not isinstance(qemu_transition, dict)
            or state.exec_transition_status(qemu_transition) != "STOPPED"):
        raise state.Refused("preserving QMP socket until exact owned QEMU exit is verified")
    directory_fd = state._open_directory_chain(create=False, home_override=home)
    if directory_fd is None:
        raise state.Refused("parent state disappeared before QMP socket cleanup")
    try:
        try:
            info = os.stat("qmp.sock", dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
            raise state.Refused("preserving unsafe or unowned QMP socket evidence")
        os.unlink("qmp.sock", dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _remove_proxy_port_file(state, home: str | None = None) -> None:
    directory_fd = state._open_directory_chain(create=False, home_override=home)
    if directory_fd is None:
        raise state.Refused("parent state disappeared before proxy readiness cleanup")
    try:
        try:
            info = os.stat("proxy.port", dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise state.Refused("preserving unsafe proxy readiness evidence")
        os.unlink("proxy.port", dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _finalize(state, args, home, *, mark_stopped: bool = True) -> None:
    try:
        directory_fd = state._open_directory_chain(create=False, home_override=home)
        if directory_fd is not None:
            try:
                try:
                    run = state._read_json(directory_fd, "active-run.json")
                    if (mark_stopped and run.get("run_id") == args.run_id
                            and run.get("supervisor", {}).get("pid") == os.getpid()):
                        if _CHILD_PROCESSES:
                            raise state.Refused("cannot publish STOPPING while supervised children remain")
                        if run.get("phase") == "RUNNING":
                            boot_id = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
                            if state._recorded_process_status(
                                    run.get("qemu"), expected_basename="qemu-system-x86_64",
                                    current_boot_id=boot_id) != "GONE":
                                raise state.Refused("cannot publish STOPPING before exact QEMU exit")
                        run["phase"] = "STOPPING"
                        run.pop("qemu", None)
                        state._atomic_json(directory_fd, "active-run.json", run)
                except FileNotFoundError:
                    pass
            finally:
                os.close(directory_fd)
    finally:
        os.close(args.lock_fd)


def run(args) -> int:
    state = _load_state()
    os.umask(0o077)
    home = str(pathlib.Path(args.state_home).resolve(strict=True)) if args.state_home else None
    if args.parent_id != state.PARENT_ID or len(args.run_id) != 32 or any(ch not in "0123456789abcdef" for ch in args.run_id):
        print("REFUSED: invalid parent or run identity", file=sys.stderr)
        return 2
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
            _record_supervisor(state, directory_fd, args.parent_id, args.run_id, supervisor_identity)
        finally:
            os.close(directory_fd)
    except (OSError, ValueError, state.Refused) as error:
        print(f"REFUSED: supervisor startup failed: {error}", file=sys.stderr)
        return 2

    if args.ssh_port:
        startup_error = None
        try:
            _start_parent_children(state, args, home, stop_requested=lambda: stop_requested)
        except (OSError, ValueError, subprocess.SubprocessError, state.Refused) as error:
            startup_error = error
            print(f"REFUSED: parent startup/readiness failed: {error}", file=sys.stderr)
        if startup_error is not None:
            try:
                _stop_starting_children(state, home)
            except (OSError, ValueError, subprocess.SubprocessError, state.Refused) as cleanup_error:
                print(f"REFUSED: parent startup cleanup is ambiguous: {cleanup_error}", file=sys.stderr)
                _finalize(state, args, home, mark_stopped=False)
                return 2
            _finalize(state, args, home)
            return 2

    stop_error = None
    try:
        while not stop_requested:
            time.sleep(0.25)
        current = state.read_status(home=home)
        if current["status"] == "STARTING":
            if _CHILD_PROCESSES:
                try:
                    _stop_starting_children(state, home)
                except (OSError, ValueError, state.Refused) as error:
                    stop_error = error
                    print(f"REFUSED: startup child rollback failed: {error}", file=sys.stderr)
        elif current["status"] == "RUNNING":
            try:
                _shutdown_running_qemu(state, args, home)
            except (OSError, ValueError, state.Refused) as error:
                stop_error = error
                print(f"REFUSED: graceful parent QEMU shutdown failed: {error}", file=sys.stderr)
        else:
            stop_error = state.Refused(
                f"supervisor stop refused because parent state is {current['status']}")
            print(f"REFUSED: {stop_error}; preserving ambiguous lifecycle evidence", file=sys.stderr)
    finally:
        _finalize(state, args, home, mark_stopped=stop_error is None)
    return 2 if stop_error is not None else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--lock-fd", type=int, required=True)
    parser.add_argument("--handoff-fd", type=int, required=True)
    parser.add_argument("--state-home")
    parser.add_argument("--ssh-port", type=int, default=0)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
