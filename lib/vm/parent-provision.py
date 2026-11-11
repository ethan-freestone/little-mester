#!/usr/bin/env python3
"""Create the persistent parent qcow2 once, at its chosen initial capacity."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import stat
import subprocess
import importlib.util


IMAGE_URL = "https://cloud-images.ubuntu.com/releases/noble/release-20261017/ubuntu-24.04-server-cloudimg-amd64.img"
IMAGE_SHA256 = "04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94"
MAX_DOWNLOAD_SECONDS = 300


class Refused(RuntimeError):
    pass


def _validate_host_tool(path: str) -> None:
    policy_path = pathlib.Path(__file__).with_name("executable-policy.py")
    spec = importlib.util.spec_from_file_location("parent_executable_policy", policy_path)
    if spec is None or spec.loader is None:
        raise Refused("protected host executable policy is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        module.validate_executable(path)
    except (OSError, ValueError) as error:
        raise Refused("parent provisioning tool is not from a protected host path") from error


def validate_qcow2_info(data: object, expected_size: int | None = None) -> int:
    """Require explicit qcow2 format, expected virtual size and no file chain."""
    format_specific = data.get("format-specific") if isinstance(data, dict) else None
    format_data = format_specific.get("data") if isinstance(format_specific, dict) else None
    if not isinstance(format_data, dict):
        format_data = {}
    if (not isinstance(data, dict) or data.get("format") != "qcow2"
            or type(data.get("virtual-size")) is not int or data["virtual-size"] <= 0
            or (expected_size is not None and data["virtual-size"] != expected_size)
            or data.get("backing-filename") not in (None, "")
            or data.get("data-file") not in (None, "")
            or data.get("data-file-raw") not in (None, False)
            or format_data.get("data-file") not in (None, "")
            or format_data.get("data-file-raw") not in (None, False)):
        raise Refused("qcow2 image has an unexpected format, capacity, backing file, or external data file")
    return data["virtual-size"]


def _info(qemu_img: str, path: str, *, directory_fd: int) -> dict:
    result = subprocess.run([qemu_img, "info", "--output=json", "-f", "qcow2", path],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, check=False, timeout=30, close_fds=True,
                            pass_fds=(directory_fd,))
    if result.returncode != 0:
        raise Refused("qemu-img could not inspect the explicitly qcow2 image")
    try:
        value = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise Refused("qemu-img returned malformed image metadata") from error
    if not isinstance(value, dict):
        raise Refused("qemu-img returned invalid image metadata")
    return value


def _private_regular_at(directory_fd: int, name: str, *, required: bool = True) -> os.stat_result | None:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        if required:
            raise Refused(f"expected private parent file is missing: {name}")
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise Refused(f"unsafe private parent file: {name}")
    return info


def create_initial_disk(*, state, capacity_gib: int, qemu_img: str, curl: str,
                        lock_fd: int, home: str | os.PathLike | None = None) -> dict:
    """Download/verify/flatten the pinned base image and publish one sparse disk.

    This is creation-time sizing only. There is no post-creation resize path.
    All partial files are retained on failure for owner inspection.
    """
    if type(capacity_gib) is not int or not 1 <= capacity_gib <= 4096:
        raise Refused("initial virtual capacity must be an integer from 1 through 4096 GiB")
    for name, executable in (("qemu-img", qemu_img), ("curl", curl)):
        if (not isinstance(executable, str) or not executable.startswith("/")
                or os.path.realpath(executable) != executable):
            raise Refused(f"{name} executable must be an absolute canonical path")
        _validate_host_tool(executable)
    state._verify_lock_fd(lock_fd, home=home)
    directory_fd = state._open_directory_chain(create=False, home_override=home)
    if directory_fd is None:
        raise Refused("private parent state is absent")
    try:
        parent = state._read_json(directory_fd, "parent.json")
        state.validate_parent(parent)
        if parent["disk"]["provisioned"]:
            artifacts = {"base-image.part", "base-image.qcow2", "disk.qcow2.part"}
            if artifacts.intersection(os.listdir(directory_fd)):
                raise Refused("provisioned disk has leftover creation artifacts requiring owner inspection")
            info = _private_regular_at(directory_fd, "disk.qcow2")
            if info.st_dev != parent["disk"]["device"] or info.st_ino != parent["disk"]["inode"]:
                raise Refused("existing parent disk identity does not match metadata")
            validate_qcow2_info(_info(qemu_img, f"/proc/self/fd/{directory_fd}/disk.qcow2",
                                      directory_fd=directory_fd),
                                capacity_gib * 1024**3)
            return parent
        if parent["disk"]["virtual_capacity_gib"] != capacity_gib:
            raise Refused("initial capacity differs from the capacity recorded at parent creation")
        for name in ("base-image.part", "base-image.qcow2", "disk.qcow2.part", "disk.qcow2"):
            _private_regular_at(directory_fd, name, required=False)
            try:
                os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise Refused(f"preserving existing creation artifact for owner inspection: {name}")

        base_fd = os.open("base-image.part", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          0o600, dir_fd=directory_fd)
        try:
            os.fchmod(base_fd, 0o600)
            result = subprocess.run([curl, "-q", "--fail", "--location", "--silent", "--show-error",
                                     "--max-time", str(MAX_DOWNLOAD_SECONDS), "--retry", "2", IMAGE_URL],
                                    stdin=subprocess.DEVNULL, stdout=base_fd, stderr=subprocess.DEVNULL,
                                    check=False, timeout=MAX_DOWNLOAD_SECONDS + 10, close_fds=True)
            os.fsync(base_fd)
            if result.returncode != 0:
                raise Refused("pinned Ubuntu base image download failed; partial evidence retained")
        finally:
            os.close(base_fd)
        digest = hashlib.sha256()
        read_fd = os.open("base-image.part", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        try:
            while block := os.read(read_fd, 1024 * 1024):
                digest.update(block)
        finally:
            os.close(read_fd)
        if digest.hexdigest() != IMAGE_SHA256:
            raise Refused("pinned Ubuntu image checksum mismatch; partial evidence retained")
        base_info = validate_qcow2_info(_info(qemu_img, f"/proc/self/fd/{directory_fd}/base-image.part",
                                              directory_fd=directory_fd))
        capacity_bytes = capacity_gib * 1024**3
        if base_info > capacity_bytes:
            raise Refused("requested initial capacity is smaller than the pinned base image")
        os.link("base-image.part", "base-image.qcow2", src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd, follow_symlinks=False)
        os.unlink("base-image.part", dir_fd=directory_fd)
        os.fsync(directory_fd)

        part_path = f"/proc/self/fd/{directory_fd}/disk.qcow2.part"
        base_path = f"/proc/self/fd/{directory_fd}/base-image.qcow2"
        convert = subprocess.run([qemu_img, "convert", "-f", "qcow2", "-O", "qcow2",
                                  base_path, part_path], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 check=False, timeout=600, close_fds=True,
                                 pass_fds=(directory_fd,))
        if convert.returncode != 0:
            raise Refused("flattening the pinned base image failed; partial evidence retained")
        part_fd = os.open("disk.qcow2.part", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        try:
            os.fchmod(part_fd, 0o600)
            os.fsync(part_fd)
        finally:
            os.close(part_fd)
        resize = subprocess.run([qemu_img, "resize", "-f", "qcow2", part_path, f"{capacity_gib}G"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, check=False, timeout=60,
                                close_fds=True, pass_fds=(directory_fd,))
        if resize.returncode != 0:
            raise Refused("initial virtual-capacity sizing failed; partial evidence retained")
        disk_info_json = _info(qemu_img, part_path, directory_fd=directory_fd)
        validate_qcow2_info(disk_info_json, capacity_bytes)
        part_info = _private_regular_at(directory_fd, "disk.qcow2.part")
        os.link("disk.qcow2.part", "disk.qcow2", src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd, follow_symlinks=False)
        os.unlink("disk.qcow2.part", dir_fd=directory_fd)
        os.fsync(directory_fd)
        parent["disk"].update({"provisioned": True, "device": part_info.st_dev, "inode": part_info.st_ino})
        state._atomic_json(directory_fd, "parent.json", parent)
        os.unlink("base-image.qcow2", dir_fd=directory_fd)
        os.fsync(directory_fd)
        return parent
    finally:
        os.close(directory_fd)


if __name__ == "__main__":
    raise SystemExit("parent-provision.py is an internal state primitive, not an installer")
