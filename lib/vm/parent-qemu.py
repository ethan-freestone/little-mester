#!/usr/bin/env python3
"""Build the explicit persistent-parent QEMU block and network argv."""

from __future__ import annotations

import fcntl
import os
import re
import stat
import sys


def _port(value: int, name: str) -> None:
    if type(value) is not int or not 1024 <= value <= 65535:
        raise ValueError(f"{name} must be an unprivileged TCP port")


def _machine_memory(*, name: str, cpus: int, memory_mib: int,
                    machine: str = "q35,accel=kvm") -> list[str]:
    if (not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name)):
        raise ValueError("VM display name is outside its fixed safe grammar")
    if type(cpus) is not int or not 1 <= cpus <= 16:
        raise ValueError("VM CPU count is outside its fixed safe range")
    if type(memory_mib) is not int or not 256 <= memory_mib <= 65536:
        raise ValueError("VM memory is outside its fixed safe range")
    return ["-name", name, "-machine", machine, "-cpu", "host", "-smp", str(cpus),
            "-m", str(memory_mib)]


def _virtio_scsi_seed(seed_iso: str) -> list[str]:
    if (not isinstance(seed_iso, str) or not seed_iso.startswith("/")
            or any(character in seed_iso for character in ("\0", "\n", "\r", ","))):
        raise ValueError("cloud-init seed path is not safe for QEMU option encoding")
    return ["-drive", f"file={seed_iso},format=raw,if=none,id=cloud,readonly=on",
            "-device", "virtio-scsi-pci,id=scsi0", "-device", "scsi-cd,drive=cloud"]


def _virtio_qcow2_drive(disk_path: str) -> list[str]:
    if (not isinstance(disk_path, str) or not disk_path.startswith("/")
            or any(character in disk_path for character in ("\0", "\n", "\r", ","))):
        raise ValueError("qcow2 disk path is not safe for QEMU option encoding")
    return ["-drive", f"file={disk_path},format=qcow2,if=virtio"]


def _network_args(*, ssh_port: int, restricted: bool,
                  proxy_port: int | None = None, metrics: bool = False,
                  metrics_port: int = 32129) -> list[str]:
    return ["-netdev", build_user_netdev(ssh_port=ssh_port, restricted=restricted,
                                         proxy_port=proxy_port, metrics=metrics,
                                         metrics_port=metrics_port),
            "-device", "virtio-net-pci,netdev=net0"]


def _profile_fd(fd: int, *, label: str, access: int, mode: int) -> tuple[int, int]:
    if type(fd) is not int or fd < 3:
        raise ValueError(f"{label} descriptor must be an open non-standard file descriptor")
    try:
        info = os.fstat(fd)
        access_mode = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE
    except OSError as error:
        raise ValueError(f"{label} descriptor is not open") from error
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != mode or access_mode != access):
        raise ValueError(f"{label} descriptor has an unsafe type, mode, or access mode")
    return info.st_dev, info.st_ino


def build_user_netdev(*, ssh_port: int, restricted: bool, proxy_port: int | None = None,
                      metrics: bool = False, metrics_port: int = 32129) -> str:
    """Build the reviewed user-mode networking policy used by all launch paths."""
    _port(ssh_port, "SSH port")
    if type(restricted) is not bool:
        raise ValueError("restricted networking selector must be boolean")
    if type(metrics) is not bool:
        raise ValueError("host metrics network selector must be boolean")
    if metrics:
        _port(metrics_port, "metrics port")
    if metrics and (not restricted or proxy_port is None):
        raise ValueError("host metrics forwarding requires the fixed restricted proxy network")
    if restricted:
        if proxy_port is None:
            if metrics:
                raise ValueError("host metrics forwarding requires the fixed restricted proxy network")
            return f"user,id=net0,restrict=on,hostfwd=tcp:127.0.0.1:{ssh_port}-:22"
        _port(proxy_port, "proxy port")
        metrics_forward = (
            f",guestfwd=tcp:10.0.2.100:19532-cmd:socat - TCP:127.0.0.1:{metrics_port}"
            if metrics else "")
        return (f"user,id=net0,hostfwd=tcp:127.0.0.1:{ssh_port}-:22,"
                f"guestfwd=tcp:10.0.2.100:3128-cmd:socat - TCP:127.0.0.1:{proxy_port}"
                f"{metrics_forward},restrict=on")
    if proxy_port is not None:
        raise ValueError("unrestricted networking does not accept a proxy port")
    return f"user,id=net0,hostfwd=tcp:127.0.0.1:{ssh_port}-:22"


def build_qemu_argv(*, executable: str, seed_iso: str, ssh_port: int, proxy_port: int,
                    qmp_socket: str, vm_name: str = "little-mester-parent-default") -> list[str]:
    """Return direct QEMU argv; parent disk is fd 3, never a mutable path."""
    if (not isinstance(executable, str) or not executable.startswith("/")
            or os.path.realpath(executable) != executable):
        raise ValueError("QEMU executable must be an absolute canonical path")
    if (not isinstance(seed_iso, str) or not seed_iso.startswith("/")
            or "\0" in seed_iso or "\n" in seed_iso or "," in seed_iso):
        raise ValueError("cloud-init seed path is not safe for QEMU option encoding")
    if (not isinstance(qmp_socket, str) or not qmp_socket.startswith("/")
            or any(character in qmp_socket for character in "\0\n,\r")
            or len(os.fsencode(qmp_socket)) > 100):
        raise ValueError("private QMP socket path is not safe for Unix-socket option encoding")
    _port(ssh_port, "SSH port")
    _port(proxy_port, "proxy port")
    return [
        executable,
        *_machine_memory(name=vm_name, cpus=2, memory_mib=4096),
        "-enable-kvm",
        "-add-fd", "fd=3,set=1,opaque=parent-disk",
        "-blockdev", "driver=file,node-name=parent-file,filename=/dev/fdset/1,locking=on,auto-read-only=off",
        "-blockdev", "driver=qcow2,node-name=parent-qcow2,file=parent-file,auto-read-only=off",
        "-device", "virtio-blk-pci,drive=parent-qcow2",
        *_virtio_scsi_seed(seed_iso),
        *_network_args(ssh_port=ssh_port, restricted=True, proxy_port=proxy_port),
        "-display", "none",
        "-serial", "stdio",
        "-monitor", "none",
        "-qmp", f"unix:{qmp_socket},server=on,wait=off",
        "-no-reboot",
    ]


def build_system_qemu_argv(*, executable: str, disk_path: str | None, seed_iso: str | None,
                           ssh_port: int, proxy_port: int, qmp_socket: str,
                           vm_name: str, virtiofs_socket: str | None = None,
                           disk_fd: int | None = None, base_fd: int | None = None,
                           cpus: int = 2, memory_mib: int = 4096,
                           metrics_port: int = 32129, seed_fd: int | None = None) -> list[str]:
    """Build fixed-instance QEMU argv, optionally binding an overlay to exact FDs.

    With no FDs, the fixed singleton keeps its established direct disk path and
    block graph. Profile callers pass the already-open overlay and approved base
    descriptors; QEMU then receives no backing path from the mutable overlay
    header and can only use the explicitly wired read-only base node.
    """
    if (not isinstance(executable, str) or not executable.startswith("/")
            or os.path.realpath(executable) != executable):
        raise ValueError("QEMU executable must be an absolute canonical path")
    profile_fds = disk_fd is not None or base_fd is not None or seed_fd is not None
    if profile_fds and (disk_fd is None or base_fd is None or seed_fd is None):
        raise ValueError("profile overlay, approved base, and seed descriptors must be supplied together")
    handoff_fds: tuple[int, ...] = ()
    if disk_fd is None:
        disk_paths = (("system disk", disk_path),)
        seed_args = _virtio_scsi_seed(seed_iso) if seed_fd is None else None
        block_graph = [
            "-blockdev", f"driver=file,node-name=system-file,filename={disk_path},locking=on,auto-read-only=off",
            "-blockdev", "driver=qcow2,node-name=system-qcow2,file=system-file,auto-read-only=off",
            "-device", "virtio-blk-pci,drive=system-qcow2",
        ]
    else:
        if any(type(fd) is not int or fd < 3 for fd in (disk_fd, base_fd, seed_fd)):
            raise ValueError("profile QEMU descriptors must be open non-standard file descriptors")
        if len({disk_fd, base_fd, seed_fd}) != 3:
            raise ValueError("profile overlay, approved base, and seed descriptors must be distinct")
        if disk_path is not None:
            raise ValueError("profile QEMU launch must use its verified overlay descriptor, not a path")
        if seed_iso is not None:
            raise ValueError("profile QEMU launch must use its verified seed descriptor, not a path")
        identities = (
            _profile_fd(disk_fd, label="profile overlay", access=os.O_RDWR, mode=0o600),
            _profile_fd(base_fd, label="approved base", access=os.O_RDONLY, mode=0o444),
            _profile_fd(seed_fd, label="profile seed", access=os.O_RDONLY, mode=0o600),
        )
        if len(set(identities)) != 3:
            raise ValueError("profile overlay, approved base, and seed must be distinct files")
        handoff_fds = (disk_fd, base_fd, seed_fd)
        disk_paths = ()
        seed_args = [
            "-add-fd", f"fd={seed_fd},set=3,opaque=profile-seed",
            "-drive", "file=/dev/fdset/3,format=raw,if=none,id=cloud,readonly=on",
            "-device", "virtio-scsi-pci,id=scsi0", "-device", "scsi-cd,drive=cloud",
        ]
        block_graph = [
            "-add-fd", f"fd={base_fd},set=1,opaque=profile-base",
            "-add-fd", f"fd={disk_fd},set=2,opaque=profile-overlay",
            "-blockdev", "driver=file,node-name=profile-base-file,filename=/dev/fdset/1,read-only=on",
            "-blockdev", "driver=qcow2,node-name=profile-base-qcow2,file=profile-base-file,read-only=on",
            "-blockdev", "driver=file,node-name=profile-overlay-file,filename=/dev/fdset/2,locking=on,auto-read-only=off",
            "-blockdev", "driver=qcow2,node-name=profile-overlay-qcow2,file=profile-overlay-file,backing=profile-base-qcow2,auto-read-only=off",
            "-device", "virtio-blk-pci,drive=profile-overlay-qcow2",
        ]
    path_inputs = list(disk_paths)
    if seed_fd is None:
        path_inputs.append(("cloud-init seed", seed_iso))
    for label, path in path_inputs:
        if (not isinstance(path, str) or not path.startswith("/")
                or any(character in path for character in ("\0", "\n", "\r", ","))):
            raise ValueError(f"{label} path is not safe for QEMU option encoding")
    if (not isinstance(qmp_socket, str) or not qmp_socket.startswith("/")
            or any(character in qmp_socket for character in "\0\n,\r")
            or len(os.fsencode(qmp_socket)) > 100):
        raise ValueError("private QMP socket path is not safe for Unix-socket option encoding")
    if virtiofs_socket is not None and (
            not isinstance(virtiofs_socket, str) or not virtiofs_socket.startswith("/")
            or any(character in virtiofs_socket for character in ("\0", "\n", "\r", ","))
            or len(os.fsencode(virtiofs_socket)) > 100):
        raise ValueError("private virtiofs socket path is not safe for QEMU option encoding")
    _port(ssh_port, "SSH port")
    _port(proxy_port, "proxy port")
    shared_memory = ([] if virtiofs_socket is None else [
        "-object", "memory-backend-memfd,id=workspace-mem,size=4096M,share=on",
        "-numa", "node,memdev=workspace-mem",
    ])
    machine_memory = _machine_memory(name=vm_name, cpus=cpus, memory_mib=memory_mib)
    argv = [
        executable,
        *machine_memory,
        *shared_memory,
        "-enable-kvm",
        *block_graph,
        *seed_args,
        *([] if virtiofs_socket is None else [
            "-chardev", f"socket,id=workspace,path={virtiofs_socket}",
            "-device", "vhost-user-fs-pci,chardev=workspace,tag=workspace"]),
        *_network_args(ssh_port=ssh_port, restricted=True, proxy_port=proxy_port,
                       metrics=virtiofs_socket is not None, metrics_port=metrics_port),
        "-display", "none",
        "-serial", "stdio",
        "-monitor", "none",
        "-qmp", f"unix:{qmp_socket},server=on,wait=off",
        "-no-reboot",
    ]
    changed_inheritance = []
    try:
        for fd in handoff_fds:
            if not os.get_inheritable(fd):
                os.set_inheritable(fd, True)
                changed_inheritance.append(fd)
    except OSError as error:
        for fd in changed_inheritance:
            os.set_inheritable(fd, False)
        raise ValueError("profile QEMU descriptors could not be prepared for exact-FD handoff") from error
    return argv


def build_smoke_qemu_argv(*, executable: str, vm_name: str, cpus: int,
                          memory_mib: int, seed_iso: str, disk_path: str,
                          virtiofs_socket: str, ssh_port: int,
                          proxy_port: int) -> list[str]:
    """Build the unchanged smoke launch with common machine/block/network args."""
    if (not isinstance(executable, str) or not executable.startswith("/")
            or "\0" in executable or "\n" in executable):
        raise ValueError("QEMU executable must be an absolute path")
    if (not isinstance(virtiofs_socket, str) or not virtiofs_socket.startswith("/")
            or any(character in virtiofs_socket for character in ("\0", "\n", ","))):
        raise ValueError("virtiofs socket path is not safe for QEMU option encoding")
    _port(ssh_port, "SSH port")
    _port(proxy_port, "proxy port")
    return [executable, "-enable-kvm",
            *_machine_memory(name=vm_name, cpus=cpus, memory_mib=memory_mib),
            "-object", f"memory-backend-memfd,id=mem,size={memory_mib}M,share=on",
            "-numa", "node,memdev=mem",
            *_virtio_scsi_seed(seed_iso),
            *_virtio_qcow2_drive(disk_path),
            "-chardev", f"socket,id=charfs,path={virtiofs_socket}",
            "-device", "vhost-user-fs-pci,chardev=charfs,tag=workspace",
            "-display", "none", "-serial", "stdio", "-monitor", "none",
            *_network_args(ssh_port=ssh_port, restricted=True, proxy_port=proxy_port),
            "-no-reboot"]


def build_ci_qemu_argv(*, mode: str, disk_path: str, seed_iso: str,
                       serial_log: str, ssh_port: int) -> list[str]:
    """Build the hosted CI adapter argv while leaving its lifecycle owner intact."""
    if mode not in ("initial", "restricted-restart"):
        raise ValueError("unsupported hosted KVM launch mode")
    if (not isinstance(seed_iso, str) or not seed_iso.startswith("/")
            or any(character in seed_iso for character in ("\0", "\n", "\r", ","))):
        raise ValueError("cloud-init seed path is not safe for QEMU option encoding")
    for label, path in (("serial log", serial_log),):
        if (not isinstance(path, str) or not path.startswith("/")
                or any(character in path for character in ("\0", "\n", "\r"))):
            raise ValueError(f"{label} path is not safe for QEMU")
    _port(ssh_port, "SSH port")
    restricted = mode == "restricted-restart"
    return [*_machine_memory(name="little-mester-hosted-kvm-integration", cpus=2,
                             memory_mib=4096),
            "-nodefaults", "-no-user-config", "-display", "none", "-monitor", "none",
            "-serial", f"file:{serial_log}", *_virtio_qcow2_drive(disk_path),
            "-drive", f"file={seed_iso},if=virtio,format=raw,readonly=on",
            *_network_args(ssh_port=ssh_port, restricted=restricted),]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--netdev", action="store_true")
    parser.add_argument("--ssh-port", type=int, required=True)
    parser.add_argument("--restricted", action="store_true")
    parser.add_argument("--proxy-port", type=int)
    parser.add_argument("--ci-argv", action="store_true")
    parser.add_argument("--smoke-argv", action="store_true")
    parser.add_argument("--mode", choices=("initial", "restricted-restart"))
    parser.add_argument("--disk")
    parser.add_argument("--seed")
    parser.add_argument("--serial-log")
    parser.add_argument("--executable")
    parser.add_argument("--vm-name")
    parser.add_argument("--cpus", type=int)
    parser.add_argument("--memory-mib", type=int)
    parser.add_argument("--virtiofs-socket")
    parser.add_argument("--nul-argv", action="store_true")
    args = parser.parse_args()
    try:
        if args.netdev:
            print(build_user_netdev(ssh_port=args.ssh_port, restricted=args.restricted,
                                    proxy_port=args.proxy_port))
        elif args.ci_argv:
            if not all((args.mode, args.disk, args.seed, args.serial_log)):
                parser.error("--ci-argv requires --mode, --disk, --seed, and --serial-log")
            argv = build_ci_qemu_argv(mode=args.mode, disk_path=args.disk,
                                      seed_iso=args.seed, serial_log=args.serial_log,
                                      ssh_port=args.ssh_port)
        elif args.smoke_argv:
            if not all((args.executable, args.vm_name, args.cpus, args.memory_mib,
                        args.seed, args.disk, args.virtiofs_socket, args.proxy_port)):
                parser.error("--smoke-argv requires its fixed launch parameters")
            argv = build_smoke_qemu_argv(
                executable=args.executable, vm_name=args.vm_name, cpus=args.cpus,
                memory_mib=args.memory_mib, seed_iso=args.seed, disk_path=args.disk,
                virtiofs_socket=args.virtiofs_socket, ssh_port=args.ssh_port,
                proxy_port=args.proxy_port)
        else:
            parser.error("choose exactly one shared argv builder")
        if not args.netdev:
            if args.nul_argv:
                sys.stdout.buffer.write(b"".join(os.fsencode(item) + b"\0" for item in argv))
            else:
                for item in argv:
                    print(item)
    except ValueError as error:
        parser.error(str(error))
