#!/usr/bin/env python3
"""Build the explicit persistent-parent QEMU block and network argv."""

from __future__ import annotations

import os


def _port(value: int, name: str) -> None:
    if type(value) is not int or not 1024 <= value <= 65535:
        raise ValueError(f"{name} must be an unprivileged TCP port")


def build_qemu_argv(*, executable: str, seed_iso: str, ssh_port: int, proxy_port: int,
                    qmp_socket: str) -> list[str]:
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
    netdev = (f"user,id=net0,hostfwd=tcp:127.0.0.1:{ssh_port}-:22,"
              f"guestfwd=tcp:10.0.2.100:3128-cmd:socat - TCP:127.0.0.1:{proxy_port},restrict=on")
    return [
        executable,
        "-name", "little-mester-parent-default",
        "-machine", "q35,accel=kvm",
        "-enable-kvm",
        "-cpu", "host",
        "-smp", "2",
        "-m", "4096",
        "-add-fd", "fd=3,set=1,opaque=parent-disk",
        "-blockdev", "driver=file,node-name=parent-file,filename=/dev/fdset/1,locking=on,auto-read-only=off",
        "-blockdev", "driver=qcow2,node-name=parent-qcow2,file=parent-file,auto-read-only=off",
        "-device", "virtio-blk-pci,drive=parent-qcow2",
        "-drive", f"file={seed_iso},format=raw,if=none,id=cloud,readonly=on",
        "-device", "virtio-scsi-pci,id=scsi0",
        "-device", "scsi-cd,drive=cloud",
        "-netdev", netdev,
        "-device", "virtio-net-pci,netdev=net0",
        "-display", "none",
        "-serial", "stdio",
        "-monitor", "none",
        "-qmp", f"unix:{qmp_socket},server=on,wait=off",
        "-no-reboot",
    ]


if __name__ == "__main__":
    raise SystemExit("parent-qemu.py is an internal argv builder, not a VM launcher")
