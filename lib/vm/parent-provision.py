#!/usr/bin/env python3
"""Create the persistent parent qcow2 once, at its chosen initial capacity."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pathlib
import stat
import struct
import subprocess
import importlib.util


IMAGE_URL = "https://cloud-images.ubuntu.com/releases/noble/release-20261017/ubuntu-24.04-server-cloudimg-amd64.img"
IMAGE_SHA256 = "04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94"
MAX_DOWNLOAD_SECONDS = 300
PROFILE_OVERLAY_NAME = "disk.qcow2"
PROFILE_OVERLAY_SIZE = 64 * 1024**3
PROFILE_BASE_VIRTUAL_SIZE = 3_758_096_384
PROFILE_QCOW_MAGIC = 0x514649FB
PROFILE_QCOW_BACKING_FORMAT = 0xE2792ACA
PROFILE_QCOW_FEATURE_TABLE = 0x6803F857
PROFILE_QCOW_CLUSTER_SIZE = 65536
PROFILE_QCOW_HEADER_LENGTH = 112
PROFILE_QCOW_FEATURES = (
    (0, 0, "dirty bit"),
    (0, 1, "corrupt bit"),
    (0, 2, "external data file"),
    (0, 3, "compression type"),
    (0, 4, "extended L2 entries"),
    (1, 0, "lazy refcounts"),
    (2, 0, "bitmaps"),
    (2, 1, "raw external data"),
)


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


def validate_profile_overlay_info(data: object, *, expected_base: str) -> int:
    """Validate the one approved profile overlay-to-base qcow2 relationship.

    This deliberately does not relax ``validate_qcow2_info``: the fixed and
    development singleton disks remain flattened images with no backing chain.
    """
    format_specific = data.get("format-specific") if isinstance(data, dict) else None
    format_data = format_specific.get("data") if isinstance(format_specific, dict) else None
    if not isinstance(format_data, dict):
        format_data = {}
    if (not isinstance(expected_base, str) or not expected_base.startswith("/")
            or os.path.realpath(expected_base) != expected_base
            or not isinstance(data, dict) or data.get("format") != "qcow2"
            or type(data.get("virtual-size")) is not int
            or data["virtual-size"] != PROFILE_OVERLAY_SIZE
            or data.get("cluster-size") != 65536
            or data.get("backing-filename") != expected_base
            or data.get("full-backing-filename") != expected_base
            or data.get("backing-filename-format") != "qcow2"
            or not isinstance(format_specific, dict)
            or format_specific.get("type") != "qcow2"
            or data.get("data-file") not in (None, "")
            or data.get("data-file-raw") not in (None, False)
            or format_data.get("data-file") not in (None, "")
            or format_data.get("data-file-raw") not in (None, False)
            or format_data.get("compat") != "1.1"
            or format_data.get("compression-type") != "zlib"
            or format_data.get("refcount-bits") not in (None, 16)
            or format_data.get("lazy-refcounts") is not False
            or format_data.get("corrupt") is not False
            or format_data.get("extended-l2") is not False):
        raise Refused("profile overlay has unexpected qcow2 geometry, backing, or features")
    return PROFILE_OVERLAY_SIZE


def validate_profile_base_info(data: object) -> int:
    """Require the exact qcow2 features observed for the pinned Noble base."""
    format_specific = data.get("format-specific") if isinstance(data, dict) else None
    format_data = format_specific.get("data") if isinstance(format_specific, dict) else None
    if not isinstance(format_data, dict):
        format_data = {}
    if (not isinstance(data, dict) or data.get("format") != "qcow2"
            or type(data.get("virtual-size")) is not int
            or data["virtual-size"] != PROFILE_BASE_VIRTUAL_SIZE
            or data.get("cluster-size") != PROFILE_QCOW_CLUSTER_SIZE
            or data.get("backing-filename") not in (None, "")
            or data.get("full-backing-filename") not in (None, "")
            or data.get("backing-filename-format") not in (None, "")
            or data.get("data-file") not in (None, "")
            or data.get("data-file-raw") not in (None, False)
            or not isinstance(format_specific, dict)
            or format_specific.get("type") != "qcow2"
            or format_data.get("compat") != "1.1"
            or format_data.get("compression-type") != "zlib"
            or format_data.get("refcount-bits") != 16
            or format_data.get("lazy-refcounts") is not False
            or format_data.get("corrupt") is not False
            or format_data.get("extended-l2") is not False
            or format_data.get("data-file") not in (None, "")
            or format_data.get("data-file-raw") not in (None, False)):
        raise Refused("approved profile base has unexpected qcow2 geometry or features")
    return PROFILE_BASE_VIRTUAL_SIZE


def verify_approved_profile_base_fd(fd: int, *, expected_identity: tuple[int, int] | None = None,
                                    expected_path: str,
                                    qemu_img: str = "/usr/bin/qemu-img") -> dict[str, int | str]:
    """Reverify the pinned immutable base by its already-open read-only FD."""
    if type(fd) is not int or fd < 0:
        raise Refused("approved profile base descriptor is invalid")
    if (not isinstance(qemu_img, str) or not qemu_img.startswith("/")
            or os.path.realpath(qemu_img) != qemu_img):
        raise Refused("qemu-img must be an absolute canonical host executable")
    if (not isinstance(expected_path, str) or not expected_path.startswith("/")
            or os.path.realpath(expected_path) != expected_path):
        raise Refused("approved profile base path is not canonical")
    _validate_host_tool(qemu_img)
    try:
        info = os.fstat(fd)
        access_mode = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE
        opened_path = os.readlink(f"/proc/self/fd/{fd}")
    except OSError as error:
        raise Refused("approved profile base descriptor is unavailable") from error
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o444 or info.st_nlink != 1
            or access_mode != os.O_RDONLY
            or opened_path != expected_path
            or (expected_identity is not None
                and (info.st_dev, info.st_ino) != expected_identity)
            or _sha256_fd(fd) != IMAGE_SHA256):
        raise Refused("approved profile base identity, mode, access, or digest is invalid")
    result = subprocess.run(
        [qemu_img, "info", "--output=json", "-f", "qcow2", f"/proc/self/fd/{fd}"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        check=False, timeout=30, close_fds=True, pass_fds=(fd,))
    if result.returncode != 0:
        raise Refused("qemu-img could not verify the approved profile base descriptor")
    try:
        parsed = json.loads(result.stdout)
        validate_profile_base_info(parsed)
    except (UnicodeError, json.JSONDecodeError, TypeError) as error:
        raise Refused("qemu-img returned malformed approved-base metadata") from error
    return {"device": info.st_dev, "inode": info.st_ino,
            "virtual_size": PROFILE_BASE_VIRTUAL_SIZE, "sha256": IMAGE_SHA256}


def _pread_exact(fd: int, size: int, offset: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        block = os.pread(fd, size - len(chunks), offset + len(chunks))
        if not block:
            raise Refused("profile overlay header is truncated")
        chunks.extend(block)
    return bytes(chunks)


def _profile_feature_table() -> bytes:
    table = bytearray()
    for feature_type, bit, name in PROFILE_QCOW_FEATURES:
        encoded = name.encode("ascii")
        if len(encoded) > 46:
            raise AssertionError("fixed qcow2 feature label exceeds its field")
        table.extend(bytes((feature_type, bit)))
        table.extend(encoded)
        table.extend(b"\0" * (46 - len(encoded)))
    return bytes(table)


def inspect_profile_overlay_fd(fd: int, *, expected_base: str,
                               expected_identity: tuple[int, int] | None = None,
                               expected_uid: int | None = None) -> dict[str, int | str]:
    """Inspect the approved qcow2 v3 overlay using only an already-open FD.

    No tool is launched and no path in the image header is opened or resolved.
    Callers must acquire the profile operation/start serialization before
    opening the leaf and retain the descriptor through QEMU exec.
    """
    if type(fd) is not int or fd < 0:
        raise Refused("profile overlay descriptor is invalid")
    if (not isinstance(expected_base, str) or not expected_base.startswith("/")
            or os.path.realpath(expected_base) != expected_base
            or any(character in expected_base for character in ("\0", "\n", "\r"))):
        raise Refused("approved profile base path is not canonical")
    backing_bytes = expected_base.encode("utf-8", errors="strict")
    if not backing_bytes or len(backing_bytes) > 4096:
        raise Refused("approved profile base path exceeds its fixed bound")
    try:
        info = os.fstat(fd)
        access_mode = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE
    except OSError as error:
        raise Refused("profile overlay descriptor is unavailable") from error
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or access_mode != os.O_RDWR
            or (expected_uid is not None and info.st_uid != expected_uid)
            or (expected_identity is not None
                and (info.st_dev, info.st_ino) != expected_identity)
            or info.st_size < PROFILE_QCOW_CLUSTER_SIZE):
        raise Refused("profile overlay descriptor identity, owner, mode, or sparse allocation is invalid")

    header = _pread_exact(fd, PROFILE_QCOW_HEADER_LENGTH, 0)
    fields = struct.unpack(">IIQI I Q I I Q Q I I Q Q Q Q I I", header[:104])
    (magic, version, backing_offset, backing_length, cluster_bits, virtual_size,
     crypt_method, l1_size, l1_offset, refcount_table_offset,
     refcount_table_clusters, snapshots, snapshots_offset, incompatible,
     compatible, autoclear, refcount_order, header_length) = fields
    cluster_size = 1 << cluster_bits if cluster_bits < 32 else 0
    if (any(header[104:PROFILE_QCOW_HEADER_LENGTH])
            or magic != PROFILE_QCOW_MAGIC or version != 3
            or cluster_size != PROFILE_QCOW_CLUSTER_SIZE
            or virtual_size != PROFILE_OVERLAY_SIZE or crypt_method != 0
            or snapshots != 0 or snapshots_offset != 0
            or incompatible != 0 or compatible != 0 or autoclear != 0
            or refcount_order != 4 or header_length != PROFILE_QCOW_HEADER_LENGTH
            or l1_size <= 0 or l1_size > 1_048_576
            or l1_offset < cluster_size or l1_offset % cluster_size != 0
            or refcount_table_offset < cluster_size
            or refcount_table_offset % cluster_size != 0
            or refcount_table_clusters < 1 or refcount_table_clusters > 1024
            or l1_offset + l1_size * 8 > info.st_size
            or refcount_table_offset + refcount_table_clusters * cluster_size > info.st_size
            or not (l1_offset + l1_size * 8 <= refcount_table_offset
                    or l1_offset >= refcount_table_offset + refcount_table_clusters * cluster_size)
            or backing_length != len(backing_bytes)):
        raise Refused("profile overlay qcow2 header has unsupported geometry or features")

    expected_feature_table = _profile_feature_table()
    position = header_length
    saw_format = False
    saw_feature_table = False
    terminated = False
    while position + 8 <= backing_offset:
        extension_header = _pread_exact(fd, 8, position)
        extension_magic, extension_length = struct.unpack(">II", extension_header)
        position += 8
        if extension_magic == 0:
            if extension_length != 0:
                raise Refused("profile overlay has a malformed qcow2 extension terminator")
            terminated = True
            break
        if extension_length > PROFILE_QCOW_CLUSTER_SIZE or position + extension_length > backing_offset:
            raise Refused("profile overlay qcow2 extension exceeds its fixed bounds")
        payload = _pread_exact(fd, extension_length, position)
        padded_length = (extension_length + 7) & ~7
        padding = _pread_exact(fd, padded_length - extension_length, position + extension_length)
        if any(padding):
            raise Refused("profile overlay qcow2 extension has nonzero padding")
        position += padded_length
        if extension_magic == PROFILE_QCOW_BACKING_FORMAT:
            if saw_format or payload != b"qcow2":
                raise Refused("profile overlay backing-format extension is invalid")
            saw_format = True
        elif extension_magic == PROFILE_QCOW_FEATURE_TABLE:
            if saw_feature_table or payload != expected_feature_table:
                raise Refused("profile overlay feature table is unknown or unsupported")
            saw_feature_table = True
        else:
            # This rejects external-data-file extensions and every future or
            # vendor-specific extension without dereferencing its payload.
            raise Refused("profile overlay contains an unsupported qcow2 extension")
    if (not terminated or position != backing_offset or not saw_format or not saw_feature_table
            or backing_offset + backing_length > info.st_size):
        raise Refused("profile overlay qcow2 extension/backing layout is unsupported")
    recorded_base = _pread_exact(fd, backing_length, backing_offset)
    if recorded_base != backing_bytes:
        raise Refused("profile overlay backing reference does not match the approved base")

    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "virtual_size": virtual_size,
        "allocated_bytes": info.st_blocks * 512,
        "backing_file": expected_base,
    }


def _sha256_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while block := os.read(fd, 1024 * 1024):
        digest.update(block)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def create_profile_overlay(*, base_path: str, base_sha256: str,
                           base_virtual_size: int, directory_fd: int,
                           directory_uid: int, directory_gid: int,
                           qemu_img: str = "/usr/bin/qemu-img") -> dict[str, int | str]:
    """Create one exclusive, sparse profile overlay under an already-open VM dir.

    The caller must derive ``base_path`` from the protected profile/base
    descriptor and hold the profile mutation lock. The directory descriptor
    must refer to that profile's protected ``vm`` directory. This primitive
    independently verifies the immutable input bytes, exact qemu-img backing
    metadata, output inode, sparse allocation and qcow2 consistency. Failed
    creations are retained for inspection; no path-based cleanup is attempted.
    """
    if (not isinstance(base_path, str) or not base_path.startswith("/")
            or os.path.realpath(base_path) != base_path):
        raise Refused("approved profile base path must be absolute and canonical")
    if (not isinstance(base_sha256, str)
            or len(base_sha256) != 64
            or any(character not in "0123456789abcdef" for character in base_sha256)):
        raise Refused("approved profile base digest is malformed")
    if base_sha256 != IMAGE_SHA256 or type(base_virtual_size) is not int \
            or base_virtual_size != PROFILE_BASE_VIRTUAL_SIZE:
        raise Refused("profile overlay source does not match the exact approved base")
    if (not isinstance(qemu_img, str) or not qemu_img.startswith("/")
            or os.path.realpath(qemu_img) != qemu_img):
        raise Refused("qemu-img must be an absolute canonical host executable")
    _validate_host_tool(qemu_img)

    try:
        base_fd = os.open(base_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise Refused("approved profile base image could not be opened safely") from error
    try:
        base_info = os.fstat(base_fd)
        path_info = os.stat(base_path, follow_symlinks=False)
        if (not stat.S_ISREG(base_info.st_mode) or base_info.st_nlink != 1
                or base_info.st_mode & 0o222
                or (base_info.st_dev, base_info.st_ino) != (path_info.st_dev, path_info.st_ino)
                or _sha256_fd(base_fd) != base_sha256):
            raise Refused("approved profile base image identity, mode, or digest is invalid")
    finally:
        os.close(base_fd)

    try:
        directory_info = os.fstat(directory_fd)
        if (not stat.S_ISDIR(directory_info.st_mode)
                or type(directory_uid) is not int or directory_info.st_uid != directory_uid
                or type(directory_gid) is not int or directory_info.st_gid != directory_gid
                or stat.S_IMODE(directory_info.st_mode) != 0o700):
            raise Refused("profile VM directory descriptor has unsafe owner, group, mode, or type")
        try:
            os.stat(PROFILE_OVERLAY_NAME, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise Refused("preserving pre-existing profile disk evidence")
    except OSError as error:
        raise Refused("profile VM directory is unavailable") from error

    overlay_path = f"/proc/self/fd/{directory_fd}/{PROFILE_OVERLAY_NAME}"
    base_result = subprocess.run(
        [qemu_img, "info", "--output=json", "-f", "qcow2", base_path],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        check=False, timeout=30, close_fds=True)
    if base_result.returncode != 0:
        raise Refused("qemu-img could not verify the approved profile base")
    try:
        base_json = json.loads(base_result.stdout)
        if validate_qcow2_info(base_json, base_virtual_size) != base_virtual_size:
            raise Refused("approved profile base geometry does not match its record")
    except (UnicodeError, json.JSONDecodeError, TypeError) as error:
        raise Refused("qemu-img returned malformed approved-base metadata") from error

    old_umask = os.umask(0o077)
    try:
        create = subprocess.run(
            [qemu_img, "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b",
             base_path, overlay_path, "64G"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=60, close_fds=True, pass_fds=(directory_fd,))
    finally:
        os.umask(old_umask)
    if create.returncode != 0:
        raise Refused("sparse profile overlay creation failed; evidence retained")
    try:
        os.chown(PROFILE_OVERLAY_NAME, directory_uid, directory_gid,
                 dir_fd=directory_fd, follow_symlinks=False)
    except OSError as error:
        raise Refused("created profile overlay ownership could not be committed") from error

    try:
        overlay_fd = os.open(PROFILE_OVERLAY_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=directory_fd)
    except OSError as error:
        raise Refused("created profile overlay could not be opened by its directory identity") from error
    try:
        overlay_info = os.fstat(overlay_fd)
        path_info = os.stat(PROFILE_OVERLAY_NAME, dir_fd=directory_fd, follow_symlinks=False)
        allocated = overlay_info.st_blocks * 512
        if (not stat.S_ISREG(overlay_info.st_mode) or overlay_info.st_nlink != 1
                or overlay_info.st_uid != directory_uid or overlay_info.st_gid != directory_gid
                or stat.S_IMODE(overlay_info.st_mode) != 0o600
                or (overlay_info.st_dev, overlay_info.st_ino) != (path_info.st_dev, path_info.st_ino)
                or allocated >= PROFILE_OVERLAY_SIZE):
            raise Refused("created profile overlay has unsafe identity, mode, or allocation")

        info_result = subprocess.run(
            [qemu_img, "info", "--output=json", "-f", "qcow2", overlay_path],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            check=False, timeout=30, close_fds=True, pass_fds=(directory_fd,))
        if info_result.returncode != 0:
            raise Refused("qemu-img could not verify the created profile overlay")
        try:
            image_info = json.loads(info_result.stdout)
            validate_profile_overlay_info(image_info, expected_base=base_path)
        except (UnicodeError, json.JSONDecodeError, TypeError) as error:
            raise Refused("qemu-img returned malformed profile overlay metadata") from error

        check = subprocess.run(
            [qemu_img, "check", "-f", "qcow2", overlay_path],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=60, close_fds=True, pass_fds=(directory_fd,))
        if check.returncode != 0:
            raise Refused("created profile overlay failed qemu-img consistency check")
        os.fsync(overlay_fd)
    finally:
        os.close(overlay_fd)
    os.fsync(directory_fd)

    return {
        "device": overlay_info.st_dev,
        "inode": overlay_info.st_ino,
        "virtual_size": PROFILE_OVERLAY_SIZE,
        "allocated_bytes": allocated,
        "backing_file": base_path,
    }


def _info(qemu_img: str, path: str, *, directory_fd: int, file_fd: int | None = None) -> dict:
    pass_fds = (directory_fd,) if file_fd is None else (directory_fd, file_fd)
    result = subprocess.run([qemu_img, "info", "--output=json", "-f", "qcow2", path],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, check=False, timeout=30, close_fds=True,
                            pass_fds=pass_fds)
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

        part_fd = os.open("disk.qcow2.part", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          0o600, dir_fd=directory_fd)
        os.fchmod(part_fd, 0o600)
        part_path = f"/proc/self/fd/{part_fd}"
        base_path = f"/proc/self/fd/{directory_fd}/base-image.qcow2"
        try:
            convert = subprocess.run([qemu_img, "convert", "-f", "qcow2", "-O", "qcow2",
                                      base_path, part_path], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     check=False, timeout=600, close_fds=True,
                                     pass_fds=(directory_fd, part_fd))
            if convert.returncode != 0:
                raise Refused("flattening the pinned base image failed; partial evidence retained")
            os.fsync(part_fd)
            resize = subprocess.run([qemu_img, "resize", "-f", "qcow2", part_path, f"{capacity_gib}G"],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, check=False, timeout=60,
                                    close_fds=True, pass_fds=(directory_fd, part_fd))
            if resize.returncode != 0:
                raise Refused("initial virtual-capacity sizing failed; partial evidence retained")
            disk_info_json = _info(qemu_img, part_path, directory_fd=directory_fd, file_fd=part_fd)
            validate_qcow2_info(disk_info_json, capacity_bytes)
            part_info = os.fstat(part_fd)
            path_info = _private_regular_at(directory_fd, "disk.qcow2.part")
            if (part_info.st_dev, part_info.st_ino) != (path_info.st_dev, path_info.st_ino):
                raise Refused("qemu-img output path no longer names its exclusively created disk inode")
            os.link("disk.qcow2.part", "disk.qcow2", src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd, follow_symlinks=False)
            published = os.stat("disk.qcow2", dir_fd=directory_fd, follow_symlinks=False)
            if (published.st_dev, published.st_ino) != (part_info.st_dev, part_info.st_ino):
                raise Refused("published parent disk does not match the exclusively created qemu-img output")
            os.unlink("disk.qcow2.part", dir_fd=directory_fd)
            os.fsync(directory_fd)
            parent["disk"].update({"provisioned": True, "device": part_info.st_dev, "inode": part_info.st_ino})
            state._atomic_json(directory_fd, "parent.json", parent)
            os.unlink("base-image.qcow2", dir_fd=directory_fd)
            os.fsync(directory_fd)
            return parent
        finally:
            os.close(part_fd)
    finally:
        os.close(directory_fd)


if __name__ == "__main__":
    raise SystemExit("parent-provision.py is an internal state primitive, not an installer")
