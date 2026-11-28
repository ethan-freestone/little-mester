#!/usr/bin/python3 -I
"""Small signed host control plane for one named persistent VM profile.

The fixed helper owns profile metadata and lifecycle effects. Profile QEMU is
started only after the protected v2 mapping and each backing descriptor have
been revalidated; the existing singleton launcher is not modified.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import pathlib
import pwd
import grp
import re
import socket
import stat
import subprocess
import sys
import time
import uuid
from typing import TypedDict


FIXED_LIB = pathlib.Path("/usr/lib/little-mester-vm")
FIXED_HELPER = pathlib.Path("/usr/libexec/little-mester-vm/profile-control")
CONFIG = pathlib.Path("/etc/little-mester-vm")
MAPPING_ROOT = CONFIG / "profiles"
STATE_ROOT = pathlib.Path("/var/lib/little-mester-vm/profiles")
RUNTIME_ROOT = pathlib.Path("/run/little-mester-vm-profiles")
CODE_LOCK = CONFIG / "code-update.lock"
CODE_MARKER = CONFIG / "code-update.in-progress"
OWNER_RECORD = CONFIG / "profile-control-owner.json"
MAX_REQUEST = 4096
QMP_STOP_SECONDS = 18.0
PROFILE_BASE_MANIFEST = hashlib.sha256(
    b"little-mester-approved-noble-base-v1\0"
    b"https://cloud-images.ubuntu.com/releases/noble/release-20261017/"
    b"ubuntu-24.04-server-cloudimg-amd64.img\0"
    b"04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94\0"
    b"3758096384\0qcow2\0read-only\n").hexdigest()
PROFILE_RUNTIME_FILES = {
    "lab-vm": 0o755,
    "lib/vm/system-vm.py": 0o644,
    "lib/vm/vm-instance.py": 0o644,
    "lib/vm/profile-control-core.py": 0o644,
    "lib/vm/parent-qemu.py": 0o644,
    "lib/vm/parent-provision.py": 0o644,
    "lib/vm/guest-bootstrap.py": 0o644,
}


class Refused(RuntimeError):
    """A host/profile identity or lifecycle check failed closed."""


class OwnerEnrollment(TypedDict):
    schema_version: int
    uid: int
    runtime_root: str


def _load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise Refused(f"fixed VM helper is unavailable: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _lib_path(name: str) -> pathlib.Path:
    local = pathlib.Path(__file__).resolve().parent / name
    if local.is_file():
        return local
    return FIXED_LIB / name


def _core():
    return _load_module("profile_control_core_runtime", _lib_path("profile-control-core.py"))


def _provision():
    return _load_module("profile_control_provision_runtime", _lib_path("parent-provision.py"))


def _qemu():
    return _load_module("profile_control_qemu_runtime", _lib_path("parent-qemu.py"))


def _system_vm():
    """Load the installed singleton lifecycle helper used by profile units."""
    return _load_module("profile_control_system_vm_lifecycle", _lib_path("system-vm.py"))


def _safe_root_dir(path: pathlib.Path, *, uid: int = 0, gid: int = 0,
                   mode: int | None = None) -> os.stat_result:
    try:
        info = os.lstat(path)
    except OSError as error:
        raise Refused(f"protected profile directory is unavailable: {path.name}") from error
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != uid or info.st_gid != gid
            or (mode is not None and stat.S_IMODE(info.st_mode) != mode)):
        raise Refused(f"protected profile directory has unsafe identity or mode: {path.name}")
    return info


def _check_protected_ancestry(path: pathlib.Path) -> None:
    if not path.is_absolute():
        raise Refused("protected profile path is not absolute")
    current = pathlib.Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = os.lstat(current)
        except OSError as error:
            raise Refused("protected profile path ancestry is unavailable") from error
        fixed_state_parent = pathlib.Path("/var/lib/little-mester-vm")
        try:
            fixed_state_gid = grp.getgrnam("lm-vm").gr_gid
        except KeyError:
            fixed_state_gid = -1
        fixed_state_ok = (current == fixed_state_parent and info.st_uid == pwd.getpwnam("lm-vm").pw_uid
                          and info.st_gid == fixed_state_gid
                          and stat.S_IMODE(info.st_mode) == 0o750)
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or (info.st_uid != 0 and not fixed_state_ok)
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
            raise Refused("protected profile path ancestry is not root-controlled")


def _mkdir(path: pathlib.Path, *, uid: int = 0, gid: int = 0, mode: int = 0o700) -> None:
    _check_protected_ancestry(path.parent)
    try:
        path.mkdir(mode=mode)
    except FileExistsError as error:
        raise Refused(f"preserving pre-existing profile path: {path.name}") from error
    os.chown(path, uid, gid, follow_symlinks=False)
    os.chmod(path, mode, follow_symlinks=False)
    _safe_root_dir(path, uid=uid, gid=gid, mode=mode)


def _require_regular(path: pathlib.Path, *, uid: int, gid: int | None = None,
                     mode: int, identity: tuple[int, int] | None = None,
                     digest: str | None = None) -> os.stat_result:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise Refused(f"profile artifact is unavailable: {path.name}") from error
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                or (gid is not None and info.st_gid != gid)
                or stat.S_IMODE(info.st_mode) != mode or info.st_nlink != 1
                or (identity is not None and (info.st_dev, info.st_ino) != identity)):
            raise Refused(f"profile artifact identity or permissions are unsafe: {path.name}")
        if digest is not None and _sha256_fd(fd) != digest:
            raise Refused(f"profile artifact digest differs from its protected mapping: {path.name}")
        return info
    finally:
        os.close(fd)


def _sha256_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while block := os.read(fd, 1024 * 1024):
        digest.update(block)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def _file_record(path: pathlib.Path, *, uid: int, gid: int | None,
                 mode: int, digest: bool) -> dict[str, int | str]:
    info = _require_regular(path, uid=uid, gid=gid, mode=mode)
    result: dict[str, int | str] = {"device": info.st_dev, "inode": info.st_ino}
    if digest:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            result["sha256"] = _sha256_fd(fd)
        finally:
            os.close(fd)
    return result


def _read_bounded(path: pathlib.Path, *, uid: int, mode: int, limit: int) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise Refused(f"protected profile file is unavailable: {path.name}") from error
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                or stat.S_IMODE(info.st_mode) != mode or info.st_nlink != 1
                or info.st_size < 0 or info.st_size > limit):
            raise Refused(f"protected profile file has unsafe identity or size: {path.name}")
        data = bytearray()
        while block := os.read(fd, min(8192, limit + 1 - len(data))):
            data.extend(block)
            if len(data) > limit:
                raise Refused(f"protected profile file exceeds its fixed bound: {path.name}")
        return bytes(data)
    finally:
        os.close(fd)


def _read_owner_record() -> OwnerEnrollment:
    try:
        info = os.lstat(OWNER_RECORD)
    except OSError as error:
        raise Refused("profile control is not enrolled for an owner") from error
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != 0 or info.st_gid != 0 or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1 or info.st_size > 8192):
        raise Refused("profile-control owner enrollment is unsafe")
    try:
        record = json.loads(_read_bounded(OWNER_RECORD, uid=0, mode=0o600, limit=8192))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise Refused("profile-control owner enrollment is malformed") from error
    if (not isinstance(record, dict)
            or set(record) != {"schema_version", "uid", "runtime_root"}
            or type(record["schema_version"]) is not int or record["schema_version"] != 2
            or type(record["uid"]) is not int or not 1 <= record["uid"] < 2**32 - 1
            or not isinstance(record["runtime_root"], str)):
        raise Refused("profile-control owner enrollment has an unsupported schema")
    runtime_root = pathlib.Path(record["runtime_root"])
    if (not runtime_root.is_absolute() or runtime_root.name != "v3"
            or str(runtime_root) != os.path.normpath(str(runtime_root))
            or ".." in runtime_root.parts or os.path.realpath(runtime_root) != str(runtime_root)):
        raise Refused("profile-control enrollment has an invalid protected runtime root")
    return OwnerEnrollment(schema_version=record["schema_version"], uid=record["uid"],
                           runtime_root=record["runtime_root"])


def _runtime_root() -> pathlib.Path:
    root = pathlib.Path(_read_owner_record()["runtime_root"])
    _check_protected_ancestry(root)
    return root


def _require_owner(caller_uid: int) -> None:
    if caller_uid == 0 or caller_uid != _read_owner_record()["uid"]:
        raise Refused("profile control request is not from the enrolled owner")
    try:
        owner = pwd.getpwuid(caller_uid)
        group = grp.getgrnam("lm-vm-control")
    except KeyError as error:
        raise Refused("profile-control owner group is unavailable") from error
    if owner.pw_gid != group.gr_gid and owner.pw_name not in group.gr_mem:
        raise Refused("profile control request lacks the enrolled owner group")
    if CODE_MARKER.exists() or CODE_MARKER.is_symlink():
        raise Refused("profile control is blocked by an incomplete fixed-code update")


def _take_code_lock(*, exclusive: bool = False) -> int:
    try:
        control_gid = grp.getgrnam("lm-vm-control").gr_gid
        fd = os.open(CODE_LOCK, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    except (KeyError, OSError) as error:
        raise Refused("fixed VM code-update lock is unavailable") from error
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0
                or info.st_gid != control_gid or stat.S_IMODE(info.st_mode) != 0o660):
            raise Refused("fixed VM code-update lock identity is unsafe")
        try:
            lock = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(fd, lock | fcntl.LOCK_NB)
        except BlockingIOError as error:
            detail = ("profile VM admission is blocked by another fixed VM runtime or code update"
                      if exclusive else "profile operation is blocked by a fixed-code update")
            raise Refused(detail) from error
        if CODE_MARKER.exists() or CODE_MARKER.is_symlink():
            raise Refused("profile operation is blocked by an incomplete fixed-code update")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _runtime_manifest(runtime_sha: str) -> str:
    if not isinstance(runtime_sha, str) or re.fullmatch(r"[0-9a-f]{40}", runtime_sha) is None:
        raise Refused("profile runtime pin must be a complete lowercase commit SHA")
    root = _runtime_root() / runtime_sha
    _check_protected_ancestry(root)
    _safe_root_dir(root, mode=0o755)
    digest_rows = []
    for relative, expected_mode in sorted(PROFILE_RUNTIME_FILES.items()):
        path = root / relative
        _check_protected_ancestry(path.parent)
        try:
            info = os.lstat(path)
        except OSError as error:
            raise Refused("pinned profile runtime is incomplete") from error
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != expected_mode
                or info.st_nlink != 1):
            raise Refused("pinned profile runtime has an unsafe required asset")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            digest_rows.append((relative, _sha256_fd(fd)))
        finally:
            os.close(fd)
    payload = json.dumps(digest_rows, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _instance(name: str, owner_uid: int, runtime_sha: str, *, vm_uid: int = 1,
              vm_gid: int = 1, proxy_uid: int = 2, proxy_gid: int = 2):
    core = _core()
    policy = core._instance_policy()
    return policy.Instance.profile(
        name=name, owner_uid=owner_uid, runtime_sha=runtime_sha,
        vm_uid=vm_uid, vm_gid=vm_gid, proxy_uid=proxy_uid, proxy_gid=proxy_gid,
        base_source_sha256=_provision().IMAGE_SHA256,
        base_manifest_sha256=PROFILE_BASE_MANIFEST, ssh_port=22223,
        cpus=2, memory_mib=4096, workspace_path=None)


def _system_account_absent(name: str) -> None:
    try:
        pwd.getpwnam(name)
    except KeyError:
        pass
    else:
        raise Refused(f"preserving pre-existing profile account: {name}")
    try:
        grp.getgrnam(name)
    except KeyError:
        return
    raise Refused(f"preserving pre-existing profile group: {name}")


def _create_account(name: str, home: pathlib.Path) -> tuple[int, int]:
    _system_account_absent(name)
    subprocess.run(["/usr/sbin/groupadd", "--system", name], stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=20, check=True)
    group = grp.getgrnam(name)
    subprocess.run(["/usr/sbin/useradd", "--system", "--no-create-home", "--home-dir",
                    str(home), "--shell", "/usr/sbin/nologin", "--gid", name, name],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=20, check=True)
    user = pwd.getpwnam(name)
    if (user.pw_uid == 0 or user.pw_uid >= 1000 or user.pw_gid != group.gr_gid
            or user.pw_shell != "/usr/sbin/nologin" or user.pw_dir != str(home)):
        raise Refused("generated profile service identity differs from the fixed account policy")
    return user.pw_uid, group.gr_gid


def _check_empty_catalog() -> None:
    for root in (MAPPING_ROOT, STATE_ROOT):
        if root.exists() or root.is_symlink():
            _safe_root_dir(root, mode=0o700 if root == MAPPING_ROOT else 0o711)
            try:
                if any(root.iterdir()):
                    raise Refused("the minimal profile controller supports one retained named profile")
            except OSError as error:
                raise Refused("could not safely inspect the named profile catalog") from error
        else:
            _mkdir(root, mode=0o700 if root == MAPPING_ROOT else 0o711)


def _ensure_base(instance, provision) -> dict[str, int | str]:
    base_path = instance.base_path
    assert base_path is not None
    base_dir = base_path.parent
    bases_root = base_dir.parent
    if not bases_root.exists():
        _mkdir(bases_root, mode=0o755)
    else:
        _safe_root_dir(bases_root, mode=0o755)
    if not base_dir.exists():
        _mkdir(base_dir, mode=0o755)
    else:
        _safe_root_dir(base_dir, mode=0o755)
    if base_path.exists() or base_path.is_symlink():
        fd = os.open(base_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            return provision.verify_approved_profile_base_fd(
                fd, expected_path=str(base_path), qemu_img="/usr/bin/qemu-img")
        finally:
            os.close(fd)

    partial = base_dir / "base-image.part"
    try:
        part_fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                          0o600)
    except OSError as error:
        raise Refused("preserving pre-existing approved-base download evidence") from error
    try:
        result = subprocess.run(
            ["/usr/bin/curl", "-q", "--fail", "--location", "--silent", "--show-error",
             "--max-time", "300", "--retry", "2", provision.IMAGE_URL],
            stdin=subprocess.DEVNULL, stdout=part_fd, stderr=subprocess.DEVNULL,
            timeout=310, check=False)
        os.fsync(part_fd)
        if result.returncode:
            raise Refused("approved Noble base download failed; partial evidence retained")
    finally:
        os.close(part_fd)
    verify_fd = os.open(partial, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(verify_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or _sha256_fd(verify_fd) != provision.IMAGE_SHA256):
            raise Refused("approved Noble base download has an unsafe identity or digest")
        result = subprocess.run(
            ["/usr/bin/qemu-img", "info", "--output=json", "-f", "qcow2", str(partial)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=30, check=False)
        if result.returncode:
            raise Refused("qemu-img could not verify the approved Noble base")
        try:
            provision.validate_profile_base_info(json.loads(result.stdout))
        except (UnicodeError, json.JSONDecodeError, TypeError) as error:
            raise Refused("approved Noble base metadata is malformed") from error
        os.fchmod(verify_fd, 0o444)
        os.fchown(verify_fd, 0, 0)
        os.fsync(verify_fd)
        os.link(partial, base_path, follow_symlinks=False)
        os.unlink(partial)
        dir_fd = os.open(base_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        os.close(verify_fd)
    final_fd = os.open(base_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        return provision.verify_approved_profile_base_fd(
            final_fd, expected_path=str(base_path), qemu_img="/usr/bin/qemu-img")
    finally:
        os.close(final_fd)


def _generate_key(path: pathlib.Path, *, uid: int, gid: int, label: str) -> None:
    if path.exists() or path.is_symlink() or pathlib.Path(str(path) + ".pub").exists():
        raise Refused(f"preserving pre-existing profile key evidence: {path.name}")
    result = subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                             "-C", label, "-f", str(path)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=20, check=False)
    if result.returncode:
        raise Refused("profile SSH identity generation failed; evidence retained")
    os.chown(path, uid, gid, follow_symlinks=False)
    os.chmod(path, 0o600, follow_symlinks=False)
    os.chown(pathlib.Path(str(path) + ".pub"), uid, gid, follow_symlinks=False)
    os.chmod(pathlib.Path(str(path) + ".pub"), 0o644, follow_symlinks=False)
    _require_regular(path, uid=uid, gid=gid, mode=0o600)
    _require_regular(pathlib.Path(str(path) + ".pub"), uid=uid, gid=gid, mode=0o644)


def _seed_profile(instance, *, owner_uid: int) -> None:
    vm = pwd.getpwnam(instance.vm_user)
    host_private = instance.host_key.read_text(encoding="ascii")
    host_public_path = pathlib.Path(str(instance.host_key) + ".pub")
    host_public = host_public_path.read_text(encoding="ascii").strip()
    client_public = pathlib.Path(str(instance.control_key) + ".pub").read_text(encoding="ascii").strip()
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [A-Za-z0-9_.-]+)?", host_public):
        raise Refused("generated profile host public key is malformed")
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [A-Za-z0-9_.-]+)?", client_public):
        raise Refused("generated profile client public key is malformed")
    if any(character in host_private for character in ("\0", "\r")):
        raise Refused("generated profile host private key is malformed")
    bootstrap = _load_module("profile_guest_bootstrap", _lib_path("guest-bootstrap.py"))
    contract = _load_module("profile_parent_session_contract",
                            _lib_path("parent-session-contract.py"))
    user_data = bootstrap.build_foundation(
        name=f"little-mester-profile-{instance.name}", uid=1000,
        ssh_key=client_public, workspace="disk",
        helper="/usr/local/libexec/lm-guest-user-dirs.py")
    contract.add_protected_session(
        user_data, host_private=host_private, host_public=host_public,
        control_public=client_public, workspace_path=contract.DEFAULT_WORKSPACE)
    user_data_bytes = b"#cloud-config\n" + json.dumps(
        user_data, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"
    meta_data = (f"instance-id: little-mester-profile-{instance.name}\n"
                 f"local-hostname: little-mester-profile-{instance.name}\n").encode("ascii")
    user_data_path = instance.vm_state / "user-data"
    meta_data_path = instance.vm_state / "meta-data"
    seed_part = instance.vm_state / "seed.iso.part"
    for path in (user_data_path, meta_data_path, seed_part, instance.seed):
        if path.exists() or path.is_symlink():
            raise Refused(f"preserving pre-existing profile seed evidence: {path.name}")
    user_data_path.write_bytes(user_data_bytes)
    meta_data_path.write_bytes(meta_data)
    for path in (user_data_path, meta_data_path):
        os.chown(path, vm.pw_uid, vm.pw_gid, follow_symlinks=False)
        os.chmod(path, 0o600, follow_symlinks=False)
    result = subprocess.run(
        ["/usr/bin/genisoimage", "-quiet", "-rock", "-joliet", "-output", str(seed_part),
         "-V", "cidata", str(user_data_path), str(meta_data_path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=60, check=False)
    if result.returncode:
        raise Refused("profile cloud-init seed creation failed; evidence retained")
    os.chown(seed_part, vm.pw_uid, vm.pw_gid, follow_symlinks=False)
    os.chmod(seed_part, 0o600, follow_symlinks=False)
    os.link(seed_part, instance.seed, follow_symlinks=False)
    os.unlink(seed_part)
    user_data_path.unlink()
    meta_data_path.unlink()
    dir_fd = os.open(instance.vm_state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    _require_regular(instance.seed, uid=vm.pw_uid, gid=vm.pw_gid, mode=0o600)


def _write_units(instance) -> None:
    helper = str(FIXED_HELPER)
    proxy = instance.proxy_unit
    vm_unit = instance.vm_unit
    vm_text = (
        "[Unit]\n"
        f"Description=Little Mester named VM profile {instance.name}\n"
        f"Requires={proxy}\nAfter={proxy}\nBindsTo={proxy}\n\n"
        "[Service]\nType=exec\n"
        f"User={instance.owner_uid}\n"
        f"ExecStart=!/usr/bin/python3 -I {helper} service qemu {instance.name}\n"
        f"ExecStartPost=!/usr/bin/python3 -I {helper} service guest-ready {instance.name} $MAINPID\n"
        f"ExecStop=!/usr/bin/python3 -I {helper} service qmp-stop {instance.name} $MAINPID\n"
        "Restart=no\nKillMode=control-group\nKillSignal=SIGTERM\n"
        "TimeoutStartSec=16min\nTimeoutStopSec=25s\nSendSIGKILL=yes\n"
        "DevicePolicy=closed\nDeviceAllow=/dev/kvm rw\nNoNewPrivileges=yes\n"
        "CapabilityBoundingSet=CAP_SETUID CAP_SETGID\nPrivateTmp=yes\nPrivateDevices=no\n"
        "ProtectHome=yes\nProtectSystem=strict\nProtectKernelTunables=yes\n"
        "ProtectKernelModules=yes\nProtectControlGroups=yes\nRestrictSUIDSGID=yes\n"
        "LockPersonality=yes\nRestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n"
        f"ReadOnlyPaths={instance.base_path} {instance.seed}\n"
        f"ReadWritePaths={instance.disk} {instance.qmp_runtime} "
        f"{_profile_readiness_runtime(instance)} {CODE_LOCK}\n"
    ).encode("ascii")
    proxy_text = (
        "[Unit]\n"
        f"Description=Little Mester restricted egress proxy for profile {instance.name}\n"
        "After=network-online.target\nWants=network-online.target\n"
        f"Before={vm_unit}\nPartOf={vm_unit}\n\n"
        "[Service]\nType=exec\n"
        f"ExecStart=/usr/bin/python3 -I {helper} service proxy {instance.name}\n"
        "Restart=no\nKillMode=control-group\nTimeoutStopSec=10s\n"
        "NoNewPrivileges=yes\nCapabilityBoundingSet=CAP_SETUID CAP_SETGID\n"
        "PrivateTmp=yes\nPrivateDevices=yes\nProtectHome=yes\nProtectSystem=strict\n"
        "ProtectKernelTunables=yes\nProtectKernelModules=yes\nProtectControlGroups=yes\n"
        "RestrictSUIDSGID=yes\nLockPersonality=yes\n"
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n"
        f"ReadWritePaths={instance.proxy_runtime} {CODE_LOCK}\n"
    ).encode("ascii")
    unit_root = pathlib.Path("/etc/systemd/system")
    for unit, payload in ((vm_unit, vm_text), (proxy, proxy_text)):
        path = unit_root / unit
        if path.exists() or path.is_symlink():
            raise Refused(f"preserving pre-existing profile unit: {unit}")
        _write_new_root_file(path, payload, 0o644)
    subprocess.run(["/usr/bin/systemctl", "daemon-reload"], stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=30, check=True)
    for unit, payload in ((vm_unit, vm_text), (proxy, proxy_text)):
        _verify_unit_bytes(unit, payload)


def _write_new_root_file(path: pathlib.Path, payload: bytes, mode: int) -> None:
    _check_protected_ancestry(path.parent)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     mode)
    except OSError as error:
        raise Refused(f"could not exclusively create protected profile file: {path.name}") from error
    try:
        view = memoryview(payload)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise Refused("protected profile write made no progress")
            view = view[count:]
        os.fchown(fd, 0, 0)
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _expected_unit_text(instance, *, role: str) -> bytes:
    # Unit bytes are regenerated by the same fixed, signed renderer used at install.
    helper = str(FIXED_HELPER)
    if role == "vm":
        return (
            "[Unit]\n"
            f"Description=Little Mester named VM profile {instance.name}\n"
            f"Requires={instance.proxy_unit}\nAfter={instance.proxy_unit}\n"
            f"BindsTo={instance.proxy_unit}\n\n"
            "[Service]\nType=exec\n"
            f"User={instance.owner_uid}\n"
            f"ExecStart=!/usr/bin/python3 -I {helper} service qemu {instance.name}\n"
            f"ExecStartPost=!/usr/bin/python3 -I {helper} service guest-ready {instance.name} $MAINPID\n"
            f"ExecStop=!/usr/bin/python3 -I {helper} service qmp-stop {instance.name} $MAINPID\n"
            "Restart=no\nKillMode=control-group\nKillSignal=SIGTERM\n"
            "TimeoutStartSec=16min\nTimeoutStopSec=25s\nSendSIGKILL=yes\n"
            "DevicePolicy=closed\nDeviceAllow=/dev/kvm rw\nNoNewPrivileges=yes\n"
            "CapabilityBoundingSet=CAP_SETUID CAP_SETGID\nPrivateTmp=yes\nPrivateDevices=no\n"
            "ProtectHome=yes\nProtectSystem=strict\nProtectKernelTunables=yes\n"
            "ProtectKernelModules=yes\nProtectControlGroups=yes\nRestrictSUIDSGID=yes\n"
            "LockPersonality=yes\nRestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n"
            f"ReadOnlyPaths={instance.base_path} {instance.seed}\n"
            f"ReadWritePaths={instance.disk} {instance.qmp_runtime} "
            f"{_profile_readiness_runtime(instance)} {CODE_LOCK}\n"
        ).encode("ascii")
    if role == "proxy":
        return (
            "[Unit]\n"
            f"Description=Little Mester restricted egress proxy for profile {instance.name}\n"
            "After=network-online.target\nWants=network-online.target\n"
            f"Before={instance.vm_unit}\nPartOf={instance.vm_unit}\n\n"
            "[Service]\nType=exec\n"
            f"ExecStart=/usr/bin/python3 -I {helper} service proxy {instance.name}\n"
            "Restart=no\nKillMode=control-group\nTimeoutStopSec=10s\n"
            "NoNewPrivileges=yes\nCapabilityBoundingSet=CAP_SETUID CAP_SETGID\n"
            "PrivateTmp=yes\nPrivateDevices=yes\nProtectHome=yes\nProtectSystem=strict\n"
            "ProtectKernelTunables=yes\nProtectKernelModules=yes\nProtectControlGroups=yes\n"
            "RestrictSUIDSGID=yes\nLockPersonality=yes\n"
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n"
            f"ReadWritePaths={instance.proxy_runtime} {CODE_LOCK}\n"
        ).encode("ascii")
    raise Refused("unsupported profile unit renderer role")


def _verify_unit_bytes(unit: str, expected: bytes) -> None:
    path = pathlib.Path("/etc/systemd/system") / unit
    try:
        info = os.lstat(path)
        actual = path.read_bytes()
    except OSError as error:
        raise Refused(f"protected profile unit is unavailable: {unit}") from error
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o644 or info.st_nlink != 1
            or actual != expected):
        raise Refused(f"protected profile unit differs from its fixed renderer: {unit}")


def _install_profile(name: str, runtime_sha: str, caller_uid: int) -> None:
    _require_owner(caller_uid)
    lock_fd = _take_code_lock()
    try:
        _install_profile_locked(name, runtime_sha, caller_uid)
    finally:
        os.close(lock_fd)


def _install_profile_locked(name: str, runtime_sha: str, caller_uid: int) -> None:
    _require_owner(caller_uid)
    runtime_manifest = _runtime_manifest(runtime_sha)
    _check_empty_catalog()
    if caller_uid <= 0:
        raise Refused("profile owner UID is outside the accepted range")
    provisional = _instance(name, caller_uid, runtime_sha)
    if not (22223 <= provisional.ssh_port <= 23220):
        raise Refused("fixed named-profile SSH port is outside its approved range")
    for port in (provisional.ssh_port, provisional.proxy_port, provisional.metrics_port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as error:
            raise Refused("fixed named-profile port block is already in use") from error
        finally:
            sock.close()

    vm_uid, vm_gid = _create_account(provisional.vm_user, provisional.vm_state)
    proxy_uid, proxy_gid = _create_account(provisional.proxy_user, pathlib.Path("/var/empty"))
    instance = _instance(name, caller_uid, runtime_sha, vm_uid=vm_uid, vm_gid=vm_gid,
                         proxy_uid=proxy_uid, proxy_gid=proxy_gid)
    if instance != provisional:
        # Account IDs are not part of any derived path/unit identity.
        for field in ("name", "owner_uid", "runtime_sha", "vm_user", "proxy_user", "vm_unit",
                      "proxy_unit", "workspace_unit", "metrics_unit", "disk", "seed", "base_path"):
            if getattr(instance, field) != getattr(provisional, field):
                raise Refused("profile account allocation changed its derived identity")

    _mkdir(instance.state_root, mode=0o711)
    # Keep both writable credential directories root-only while root creates
    # their initial files; hand each directory to its final owner only after
    # all root writes are complete.
    _mkdir(instance.vm_state, mode=0o700)
    _mkdir(instance.client_state, mode=0o700)
    config_profile = MAPPING_ROOT / name
    _mkdir(config_profile, mode=0o700)
    base_info = _ensure_base(instance, _provision())
    if (base_info.get("sha256") != _provision().IMAGE_SHA256
            or base_info.get("virtual_size") != _provision().PROFILE_BASE_VIRTUAL_SIZE):
        raise Refused("approved profile base does not match the fixed v2 image contract")

    transaction_id = str(uuid.uuid4())
    generation = str(uuid.uuid4())
    mapping = {
        "schema_version": 2, "name": name, "owner_uid": caller_uid,
        "state": "installing",
        "transaction": {"schema_version": 2, "operation": "install",
                        "transaction_id": transaction_id, "phase": "preparing",
                        "mode": "fresh", "generation": generation},
        "runtime": {"sha": runtime_sha, "manifest_sha256": runtime_manifest,
                    "helper_api": 1, "runtime_api": 1},
        "base": {"source_sha256": _provision().IMAGE_SHA256,
                 "manifest_sha256": PROFILE_BASE_MANIFEST},
        "resources": {"cpus": 2, "memory_mib": 4096, "ssh_port": instance.ssh_port},
        "accounts": {
            "vm": {"name": instance.vm_user, "uid": vm_uid, "gid": vm_gid},
            "proxy": {"name": instance.proxy_user, "uid": proxy_uid, "gid": proxy_gid},
        },
        "units": {"vm": instance.vm_unit, "proxy": instance.proxy_unit,
                  "workspace": instance.workspace_unit, "metrics": instance.metrics_unit},
        "workspace": None, "provisioning": None,
    }
    core = _core()
    mapping_path = MAPPING_ROOT / name / "profile.json"
    core.atomic_write_mapping(mapping_path, mapping)
    mapping_info = os.lstat(mapping_path)

    _generate_key(instance.host_key, uid=vm_uid, gid=vm_gid,
                  label=f"little-mester-profile-{name}-host")
    _generate_key(instance.control_key, uid=caller_uid,
                  gid=pwd.getpwuid(caller_uid).pw_gid,
                  label=f"little-mester-profile-{name}-control")
    _seed_profile(instance, owner_uid=caller_uid)
    known = instance.known_hosts
    host_public = pathlib.Path(str(instance.host_key) + ".pub").read_text(encoding="ascii").strip()
    known.write_text(f"[127.0.0.1]:{instance.ssh_port} {host_public}\n", encoding="ascii")
    os.chown(known, caller_uid, pwd.getpwuid(caller_uid).pw_gid, follow_symlinks=False)
    os.chmod(known, 0o600, follow_symlinks=False)

    os.chown(instance.vm_state, vm_uid, vm_gid, follow_symlinks=False)
    os.chmod(instance.vm_state, 0o700, follow_symlinks=False)
    os.chown(instance.client_state, caller_uid, pwd.getpwuid(caller_uid).pw_gid,
             follow_symlinks=False)
    os.chmod(instance.client_state, 0o700, follow_symlinks=False)

    directory_fd = os.open(instance.vm_state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        disk_info = _provision().create_profile_overlay(
            base_path=str(instance.base_path), base_sha256=_provision().IMAGE_SHA256,
            base_virtual_size=_provision().PROFILE_BASE_VIRTUAL_SIZE,
            directory_fd=directory_fd, directory_uid=vm_uid, directory_gid=vm_gid)
    finally:
        os.close(directory_fd)

    _write_units(instance)
    artifacts = {
        "disk": {"device": disk_info["device"], "inode": disk_info["inode"],
                 "virtual_size": disk_info["virtual_size"],
                 "allocated_bytes": disk_info["allocated_bytes"]},
        "seed": _file_record(instance.seed, uid=vm_uid, gid=vm_gid, mode=0o600, digest=True),
        "host_private": _file_record(instance.host_key, uid=vm_uid, gid=vm_gid,
                                      mode=0o600, digest=False),
        "host_public": _file_record(pathlib.Path(str(instance.host_key) + ".pub"),
                                    uid=vm_uid, gid=vm_gid, mode=0o644, digest=True),
        "client_private": _file_record(instance.control_key, uid=caller_uid,
                                        gid=pwd.getpwuid(caller_uid).pw_gid,
                                        mode=0o600, digest=False),
        "client_public": _file_record(pathlib.Path(str(instance.control_key) + ".pub"),
                                      uid=caller_uid, gid=pwd.getpwuid(caller_uid).pw_gid,
                                      mode=0o644, digest=True),
        "known_hosts": _file_record(known, uid=caller_uid,
                                    gid=pwd.getpwuid(caller_uid).pw_gid,
                                    mode=0o600, digest=True),
    }
    mapping["state"] = "ready"
    mapping["transaction"] = None
    mapping["provisioning"] = {"generation": generation, "artifacts": artifacts}
    core.atomic_write_mapping(mapping_path, mapping,
                              expected_existing=(mapping_info.st_dev, mapping_info.st_ino))
    _ensure_runtime_dirs(instance)
    print(f"Installed named VM profile {name} pinned to {runtime_sha}.")


def _load_profile(name: str, *, owner_uid: int | None = None):
    core = _core()
    mapping, instance = core.load_mapping(name, expected_owner_uid=owner_uid, require_ready=True)
    if (mapping["base"]["source_sha256"] != _provision().IMAGE_SHA256
            or mapping["base"]["manifest_sha256"] != PROFILE_BASE_MANIFEST):
        raise Refused("profile mapping does not identify the fixed approved-base manifest")
    manifest = _runtime_manifest(mapping["runtime"]["sha"])
    if manifest != mapping["runtime"]["manifest_sha256"]:
        raise Refused("pinned profile runtime no longer matches its installed manifest")
    return core, mapping, instance


def _verify_artifacts(core, mapping: dict, instance) -> tuple[int, int, int]:
    provision = _provision()
    artifacts = mapping["provisioning"]["artifacts"]
    vm = mapping["accounts"]["vm"]
    owner_uid = mapping["owner_uid"]
    owner = pwd.getpwuid(owner_uid)
    fds: list[int] = []
    try:
        disk_fd = os.open(instance.disk, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        fds.append(disk_fd)
        base_fd = os.open(instance.base_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        fds.append(base_fd)
        seed_fd = os.open(instance.seed, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        fds.append(seed_fd)
        disk = artifacts["disk"]
        overlay = provision.inspect_profile_overlay_fd(
            disk_fd, expected_base=str(instance.base_path),
            expected_identity=(disk["device"], disk["inode"]), expected_uid=vm["uid"])
        if (overlay["virtual_size"] != disk["virtual_size"]
                or not 0 < overlay["allocated_bytes"] < overlay["virtual_size"]):
            raise Refused("profile overlay geometry or allocation differs from its v2 contract")
        provision.verify_approved_profile_base_fd(
            base_fd, expected_path=str(instance.base_path))
        for fd, artifact, uid, gid, mode in (
                (disk_fd, disk, vm["uid"], vm["gid"], 0o600),
                (base_fd, None, 0, 0, 0o444),
                (seed_fd, artifacts["seed"], vm["uid"], vm["gid"], 0o600)):
            info = os.fstat(fd)
            access = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE
            expected_access = os.O_RDWR if fd == disk_fd else os.O_RDONLY
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_gid != gid
                    or stat.S_IMODE(info.st_mode) != mode or info.st_nlink != 1
                    or access != expected_access):
                raise Refused("profile QEMU artifact descriptor has unsafe identity or access")
            if artifact is not None and (info.st_dev, info.st_ino) != (artifact["device"], artifact["inode"]):
                raise Refused("profile QEMU artifact identity differs from protected metadata")
        if _sha256_fd(seed_fd) != artifacts["seed"]["sha256"]:
            raise Refused("profile cloud-init seed digest differs from protected metadata")
        # Revalidate all credential identities without returning their bytes.
        _require_regular(instance.host_key, uid=vm["uid"], gid=vm["gid"], mode=0o600,
                         identity=(artifacts["host_private"]["device"],
                                   artifacts["host_private"]["inode"]))
        _require_regular(pathlib.Path(str(instance.host_key) + ".pub"), uid=vm["uid"],
                         gid=vm["gid"], mode=0o644,
                         identity=(artifacts["host_public"]["device"],
                                   artifacts["host_public"]["inode"]),
                         digest=artifacts["host_public"]["sha256"])
        _require_regular(instance.control_key, uid=owner_uid, gid=owner.pw_gid, mode=0o600,
                         identity=(artifacts["client_private"]["device"],
                                   artifacts["client_private"]["inode"]))
        _require_regular(pathlib.Path(str(instance.control_key) + ".pub"), uid=owner_uid,
                         gid=owner.pw_gid, mode=0o644,
                         identity=(artifacts["client_public"]["device"],
                                   artifacts["client_public"]["inode"]),
                         digest=artifacts["client_public"]["sha256"])
        _require_regular(instance.known_hosts, uid=owner_uid, gid=owner.pw_gid, mode=0o600,
                         identity=(artifacts["known_hosts"]["device"],
                                   artifacts["known_hosts"]["inode"]),
                         digest=artifacts["known_hosts"]["sha256"])
        for fd in fds:
            os.set_inheritable(fd, True)
        return disk_fd, base_fd, seed_fd  # caller closes only if it does not exec
    except BaseException:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def _ensure_runtime_dirs(instance) -> None:
    _safe_root_dir(RUNTIME_ROOT, mode=0o755) if RUNTIME_ROOT.exists() else _mkdir(
        RUNTIME_ROOT, mode=0o755)
    profile_runtime = RUNTIME_ROOT / instance.name
    if profile_runtime.exists():
        _safe_root_dir(profile_runtime, mode=0o711)
    else:
        _mkdir(profile_runtime, mode=0o711)
    for path, username in ((instance.qmp_runtime, instance.vm_user),
                           (instance.proxy_runtime, instance.proxy_user)):
        user = pwd.getpwnam(username)
        if path.exists() or path.is_symlink():
            _safe_root_dir(path, uid=user.pw_uid, gid=user.pw_gid, mode=0o700)
        else:
            _mkdir(path, uid=user.pw_uid, gid=user.pw_gid, mode=0o700)
    owner = pwd.getpwuid(instance.owner_uid)
    readiness_runtime = _profile_readiness_runtime(instance)
    if readiness_runtime.exists() or readiness_runtime.is_symlink():
        _safe_root_dir(readiness_runtime, uid=owner.pw_uid, gid=owner.pw_gid, mode=0o700)
    else:
        _mkdir(readiness_runtime, uid=owner.pw_uid, gid=owner.pw_gid, mode=0o700)


def _profile_readiness_runtime(instance) -> pathlib.Path:
    """Profile-private /run directory for short-lived readiness credentials."""
    return instance.qmp_runtime.parent / "readiness"


def _read_profile_ssh_artifact(path: pathlib.Path, *, uid: int, gid: int,
                               expected_identity: tuple[int, int], limit: int,
                               expected_digest: str | None = None) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise Refused(f"profile SSH credential is unavailable: {path.name}") from error
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_gid != gid
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != expected_identity
                or info.st_size < 0 or info.st_size > limit):
            raise Refused(f"profile SSH credential identity or size is unsafe: {path.name}")
        data = bytearray()
        while block := os.read(fd, min(4096, limit + 1 - len(data))):
            data.extend(block)
            if len(data) > limit:
                raise Refused(f"profile SSH credential exceeds its fixed bound: {path.name}")
        if expected_digest is not None and hashlib.sha256(data).hexdigest() != expected_digest:
            raise Refused(f"profile SSH credential digest differs from its protected mapping: {path.name}")
        return bytes(data)
    finally:
        os.close(fd)


def _write_readiness_credential(path: pathlib.Path, payload: bytes, *, uid: int,
                                gid: int) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600)
    except OSError as error:
        raise Refused(f"preserving pre-existing readiness credential: {path.name}") from error
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise Refused("readiness credential write made no progress")
            view = view[written:]
        os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    finally:
        os.close(fd)


def _prepare_guest_readiness_credentials(instance, mapping: dict) -> None:
    owner_uid = mapping["owner_uid"]
    owner = pwd.getpwuid(owner_uid)
    runtime = _profile_readiness_runtime(instance)
    _safe_root_dir(runtime, uid=owner_uid, gid=owner.pw_gid, mode=0o700)
    if os.listdir(runtime):
        raise Refused("preserving pre-existing profile readiness credential evidence")
    artifacts = mapping["provisioning"]["artifacts"]
    client = artifacts["client_private"]
    known_hosts = artifacts["known_hosts"]
    identity_data = _read_profile_ssh_artifact(
        instance.control_key, uid=owner_uid, gid=owner.pw_gid,
        expected_identity=(client["device"], client["inode"]), limit=16 * 1024)
    known_data = _read_profile_ssh_artifact(
        instance.known_hosts, uid=owner_uid, gid=owner.pw_gid,
        expected_identity=(known_hosts["device"], known_hosts["inode"]),
        limit=4096, expected_digest=known_hosts["sha256"])
    try:
        _write_readiness_credential(runtime / "identity", identity_data,
                                    uid=owner_uid, gid=owner.pw_gid)
        _write_readiness_credential(runtime / "known_hosts", known_data,
                                    uid=owner_uid, gid=owner.pw_gid)
        directory_fd = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        _remove_readiness_credentials(runtime, expected_uid=owner_uid, expected_gid=owner.pw_gid)
        raise


def _remove_readiness_credentials(runtime: pathlib.Path, *, expected_uid: int,
                                  expected_gid: int) -> None:
    try:
        _safe_root_dir(runtime, uid=expected_uid, gid=expected_gid, mode=0o700)
        names = set(os.listdir(runtime))
    except FileNotFoundError:
        return
    if names - {"identity", "known_hosts"}:
        raise Refused("preserving unexpected profile readiness runtime evidence")
    for name in ("identity", "known_hosts"):
        path = runtime / name
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != expected_uid or info.st_gid != expected_gid
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise Refused("preserving changed profile readiness credential evidence")
        os.unlink(path)


def _proxy_port_ready(instance) -> None:
    path = instance.proxy_port_file
    try:
        raw = _read_bounded(path, uid=pwd.getpwnam(instance.proxy_user).pw_uid,
                            mode=0o600, limit=16)
        value = raw.decode("ascii")
    except (UnicodeError, OSError, Refused) as error:
        raise Refused("profile restricted egress proxy has not published its listener") from error
    if value != f"{instance.proxy_port}\n":
        raise Refused("profile restricted egress proxy listener differs from its derived port")
    try:
        with socket.create_connection(("127.0.0.1", instance.proxy_port), timeout=1):
            pass
    except OSError as error:
        raise Refused("profile restricted egress proxy is not accepting connections") from error


def _wait_proxy_ready(instance) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            _proxy_port_ready(instance)
            return
        except Refused:
            time.sleep(0.1)
    raise Refused("profile restricted egress proxy did not become ready within its fixed bound")


def _drop_privileges(user: pwd.struct_passwd, supplementary: list[int]) -> None:
    os.setgroups(sorted(set(supplementary)))
    os.setgid(user.pw_gid)
    os.setuid(user.pw_uid)
    libc = __import__("ctypes").CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise Refused("profile service could not set no_new_privs after identity drop")


def _service_qemu(name: str) -> None:
    if os.geteuid() != 0:
        raise Refused("profile QEMU service entrypoint must be invoked by its fixed systemd unit")
    # The singleton services retain shared locks for their whole lifetimes.
    # Holding the same lock exclusively makes profile/singleton admission
    # mutually exclusive and also pins the fixed helper/runtime while the
    # mapping, runtime manifest, units, and QEMU inputs are revalidated.
    lock_fd = _take_code_lock(exclusive=True)
    fds: tuple[int, ...] = ()
    try:
        core, mapping, instance = _load_profile(name)
        _verify_unit_bytes(instance.vm_unit, _expected_unit_text(instance, role="vm"))
        _verify_unit_bytes(instance.proxy_unit, _expected_unit_text(instance, role="proxy"))
        fds = _verify_artifacts(core, mapping, instance)
        disk_fd, base_fd, seed_fd = fds
        vm_user = pwd.getpwnam(instance.vm_user)
        _ensure_runtime_dirs(instance)
        if instance.qmp_socket.exists() or instance.qmp_socket.is_symlink():
            raise Refused("preserving pre-existing profile QMP socket evidence")
        _wait_proxy_ready(instance)
        builder = _qemu()
        argv = builder.build_system_qemu_argv(
            executable="/usr/bin/qemu-system-x86_64", disk_path=None, seed_iso=None,
            ssh_port=instance.ssh_port, proxy_port=instance.proxy_port,
            qmp_socket=str(instance.qmp_socket), vm_name=instance.name,
            disk_fd=disk_fd, base_fd=base_fd, seed_fd=seed_fd,
            cpus=instance.cpus, memory_mib=instance.memory_mib,
            metrics_port=instance.metrics_port)
        os.set_inheritable(lock_fd, True)
        _drop_privileges(vm_user, [grp.getgrnam("kvm").gr_gid])
        os.environ.clear()
        os.environ.update({"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/nonexistent",
                           "LANG": "C", "LC_ALL": "C"})
        os.execv(argv[0], argv)
    except BaseException:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.close(lock_fd)
        except OSError:
            pass
        raise


def _service_guest_ready(name: str, pid_text: str) -> None:
    """Run the singleton's bounded guest gate for this exact profile MainPID."""
    if os.geteuid() != 0 or not re.fullmatch(r"[1-9][0-9]{0,8}", pid_text):
        raise Refused("invalid fixed profile readiness request")
    _core, mapping, instance = _load_profile(name)
    _verify_unit_bytes(instance.vm_unit, _expected_unit_text(instance, role="vm"))
    owner_uid = mapping["owner_uid"]
    owner = pwd.getpwuid(owner_uid)
    lifecycle = _system_vm()
    readiness_runtime = _profile_readiness_runtime(instance)
    ssh_options = {
        "ssh_port": instance.ssh_port,
        "identity_file": readiness_runtime / "identity",
        "known_hosts": readiness_runtime / "known_hosts",
        "expected_identity_uid": owner_uid,
        "runtime_directory": readiness_runtime,
        "expected_runtime_uid": owner_uid,
    }
    pidfd = lifecycle._open_qemu_readiness_pidfd(
        int(pid_text), expected_uid=None)
    # $MAINPID is supplied by this fixed unit, whose ExecStart validated and
    # launched the QEMU binary. Bind liveness to that pidfd, then drop to the
    # enrolled owner before opening the already-staged SSH identity.
    os.environ.clear()
    os.environ.update({"PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                       "HOME": owner.pw_dir, "LANG": "C", "LC_ALL": "C"})
    try:
        _drop_privileges(owner, [])
        _safe_root_dir(readiness_runtime, uid=owner_uid, gid=owner.pw_gid, mode=0o700)
        _require_regular(ssh_options["identity_file"], uid=owner_uid,
                         gid=owner.pw_gid, mode=0o600)
        _require_regular(ssh_options["known_hosts"], uid=owner_uid,
                         gid=owner.pw_gid, mode=0o600)
        lifecycle._ensure_guest_ready(
            qemu_pidfd=pidfd, expected_qemu_uid=None,
            guest_ssh_options=ssh_options, workspace_path="/home/mester/workspace",
            verify_workspace_mount=False,
            timeout_seconds=lifecycle.SSH_READY_TIMEOUT)
    finally:
        try:
            _remove_readiness_credentials(
                readiness_runtime, expected_uid=owner_uid, expected_gid=owner.pw_gid)
        finally:
            os.close(pidfd)


def _service_qmp_stop(name: str, pid_text: str) -> None:
    if os.geteuid() != 0 or not re.fullmatch(r"[1-9][0-9]{0,8}", pid_text):
        raise Refused("invalid fixed profile QMP stop request")
    _core, mapping, instance = _load_profile(name)
    _verify_unit_bytes(instance.vm_unit, _expected_unit_text(instance, role="vm"))
    lifecycle = _system_vm()
    vm_user = pwd.getpwuid(mapping["accounts"]["vm"]["uid"])
    _drop_privileges(vm_user, [])
    lifecycle.qmp_powerdown_and_wait(
        int(pid_text), timeout_seconds=QMP_STOP_SECONDS,
        qmp_runtime=instance.qmp_runtime, qmp_socket_path=instance.qmp_socket,
        service_uid=mapping["accounts"]["vm"]["uid"])


def _service_qmp_status(name: str, pid_text: str) -> None:
    """Emit only the fixed, allowlisted QMP status fields for CI diagnostics."""
    if os.geteuid() != 0 or not re.fullmatch(r"[1-9][0-9]{0,8}", pid_text):
        raise Refused("invalid fixed profile QMP observation request")
    _core, mapping, instance = _load_profile(name)
    _verify_unit_bytes(instance.vm_unit, _expected_unit_text(instance, role="vm"))
    lifecycle = _system_vm()
    observation = lifecycle.qmp_query_status(
        int(pid_text), qmp_runtime=instance.qmp_runtime,
        qmp_socket_path=instance.qmp_socket,
        service_uid=mapping["accounts"]["vm"]["uid"], timeout_seconds=2.0)
    print("profile_qmp_status=" + json.dumps(observation, sort_keys=True,
                                             separators=(",", ":")), flush=True)


def _service_proxy(name: str) -> None:
    if os.geteuid() != 0:
        raise Refused("profile proxy service entrypoint must be invoked by its fixed systemd unit")
    _core_mapping, mapping, instance = _load_profile(name)
    _verify_unit_bytes(instance.proxy_unit, _expected_unit_text(instance, role="proxy"))
    if CODE_MARKER.exists() or CODE_MARKER.is_symlink():
        raise Refused("profile proxy start is blocked by an incomplete fixed-code update")
    _ensure_runtime_dirs(instance)
    port_file = instance.proxy_port_file
    if port_file.exists() or port_file.is_symlink():
        raise Refused("preserving pre-existing profile proxy port-file evidence")
    proxy_user = pwd.getpwnam(instance.proxy_user)
    proxy_path = _lib_path("proxy.py")
    _check_protected_ancestry(proxy_path)
    _drop_privileges(proxy_user, [])
    os.environ.clear()
    os.environ.update({"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/nonexistent",
                       "LANG": "C", "LC_ALL": "C"})
    os.execv("/usr/bin/python3", ["/usr/bin/python3", "-I", str(proxy_path),
                                   "--port-file", str(port_file),
                                   "--port", str(instance.proxy_port)])


def _systemd_state(unit: str) -> str:
    result = subprocess.run(["/usr/bin/systemctl", "show", "--property=ActiveState", "--value", unit],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, timeout=10, check=False)
    if result.returncode:
        raise Refused("could not verify named profile systemd state")
    state = result.stdout.strip()
    if state not in ("inactive", "failed", "active", "activating", "deactivating", "reloading"):
        raise Refused("named profile systemd state is ambiguous")
    return state


def _cleanup_proxy_port_file(instance) -> None:
    path = instance.proxy_port_file
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    proxy_uid = pwd.getpwnam(instance.proxy_user).pw_uid
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != proxy_uid or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1 or info.st_size > 16):
        raise Refused("preserving changed profile proxy port-file evidence")
    if _read_bounded(path, uid=proxy_uid, mode=0o600, limit=16) != f"{instance.proxy_port}\n".encode():
        raise Refused("preserving mismatched profile proxy port-file evidence")
    os.unlink(path)


def _write_staged_secret(directory_fd: int, name: str, payload: bytes, *,
                         owner_uid: int, owner_gid: int, mode: int) -> pathlib.Path:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=directory_fd)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise Refused("temporary profile SSH identity write made no progress")
            view = view[written:]
        os.fchown(fd, owner_uid, owner_gid)
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)
    return pathlib.Path(f"/proc/self/fd/{directory_fd}") / name


def _stage_owner_ssh(instance, caller_uid: int):
    owner = pwd.getpwuid(caller_uid)
    runtime = pathlib.Path(f"/run/user/{caller_uid}")
    try:
        info = os.lstat(runtime)
    except OSError as error:
        raise Refused("owner private runtime directory is unavailable for profile SSH") from error
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != caller_uid or stat.S_IMODE(info.st_mode) != 0o700):
        raise Refused("owner private runtime directory is unsafe for profile SSH")
    key_data = _read_bounded(instance.control_key, uid=caller_uid, mode=0o600, limit=16 * 1024)
    known_data = _read_bounded(instance.known_hosts, uid=caller_uid, mode=0o600, limit=4096)
    for _attempt in range(4):
        directory = runtime / f"little-mester-profile-ssh-{os.getpid()}-{os.urandom(6).hex()}"
        try:
            os.mkdir(directory, 0o700)
            break
        except FileExistsError:
            continue
    else:
        raise Refused("private profile SSH staging directory could not be created exclusively")
    try:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise Refused("private profile SSH staging directory changed while opening") from error
    try:
        opened = os.fstat(directory_fd)
        if (not stat.S_ISDIR(opened.st_mode) or opened.st_uid != 0
                or stat.S_IMODE(opened.st_mode) != 0o700):
            raise Refused("private profile SSH staging directory is not root-private")
        _write_staged_secret(directory_fd, "identity", key_data,
                             owner_uid=caller_uid, owner_gid=owner.pw_gid, mode=0o600)
        _write_staged_secret(directory_fd, "known_hosts", known_data,
                             owner_uid=caller_uid, owner_gid=owner.pw_gid, mode=0o600)
        os.fchown(directory_fd, caller_uid, owner.pw_gid)
        os.fchmod(directory_fd, 0o700)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    parent_fd = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return directory, directory / "identity", directory / "known_hosts", owner


def _remove_staged_ssh(directory: pathlib.Path) -> None:
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        return
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise Refused("preserving changed temporary profile SSH directory")
    for name in ("identity", "known_hosts"):
        path = directory / name
        try:
            file_info = os.lstat(path)
        except FileNotFoundError:
            continue
        if (not stat.S_ISREG(file_info.st_mode) or stat.S_ISLNK(file_info.st_mode)
                or file_info.st_uid != os.getuid() or stat.S_IMODE(file_info.st_mode) != 0o600
                or file_info.st_nlink != 1):
            raise Refused("preserving changed temporary profile SSH identity")
        os.unlink(path)
    directory.rmdir()


def _owner_operation(operation: str, name: str, runtime_sha: str | None, caller_uid: int) -> int:
    _require_owner(caller_uid)
    if operation == "install":
        if runtime_sha is None:
            raise Refused("named profile install requires a pinned runtime SHA")
        _install_profile(name, runtime_sha, caller_uid)
        return 0
    admission_fd = None
    if operation == "up":
        # Check admission before reading the mutable mapping/runtime. The
        # service launcher repeats this check and keeps the lock through exec.
        admission_fd = _take_code_lock(exclusive=True)
    try:
        core, mapping, instance = _load_profile(name, owner_uid=caller_uid)
        vm_state = _systemd_state(instance.vm_unit)
        proxy_state = _systemd_state(instance.proxy_unit)
    finally:
        if admission_fd is not None:
            os.close(admission_fd)
    if operation == "status":
        print(f"Profile: {name}\nRuntime: {mapping['runtime']['sha']}\n"
              f"VM: {vm_state}\nProxy: {proxy_state}")
        return 0
    if operation == "up":
        if vm_state in ("active", "activating", "deactivating"):
            raise Refused("named profile VM is already active or transitioning")
        _ensure_runtime_dirs(instance)
        _verify_unit_bytes(instance.vm_unit, _expected_unit_text(instance, role="vm"))
        _verify_unit_bytes(instance.proxy_unit, _expected_unit_text(instance, role="proxy"))
        _prepare_guest_readiness_credentials(instance, mapping)
        try:
            result = subprocess.run(["/usr/bin/systemctl", "start", instance.vm_unit],
                                    stdin=subprocess.DEVNULL, timeout=1000, check=False)
        finally:
            _remove_readiness_credentials(
                _profile_readiness_runtime(instance), expected_uid=mapping["owner_uid"],
                expected_gid=pwd.getpwuid(mapping["owner_uid"]).pw_gid)
        if result.returncode:
            raise Refused("named profile VM failed to start; profile evidence was retained")
        print(f"Named profile {name} is running and passed strict host-key-pinned SSH readiness.")
        return 0
    if operation == "down":
        result = subprocess.run(["/usr/bin/systemctl", "stop", instance.vm_unit],
                                stdin=subprocess.DEVNULL, timeout=60, check=False)
        if result.returncode:
            raise Refused("named profile graceful stop failed; evidence was retained")
        if _systemd_state(instance.vm_unit) not in ("inactive", "failed"):
            raise Refused("named profile VM remains active after stop")
        if _systemd_state(instance.proxy_unit) not in ("inactive", "failed"):
            raise Refused("named profile proxy remains active after VM stop")
        _cleanup_proxy_port_file(instance)
        print(f"Named profile {name} is stopped; persistent state was retained.")
        return 0
    if operation == "ssh":
        if vm_state != "active":
            raise Refused("named profile VM must be active before SSH")
        fds = _verify_artifacts(core, mapping, instance)
        for fd in fds:
            os.close(fd)
        staged_dir, staged_key, staged_known, owner = _stage_owner_ssh(instance, caller_uid)
        tty = os.open("/dev/tty", os.O_RDWR | os.O_CLOEXEC)
        try:
            for target in (0, 1, 2):
                os.dup2(tty, target, inheritable=True)
        finally:
            if tty > 2:
                os.close(tty)
        _drop_privileges(owner, [])
        try:
            lifecycle = _system_vm()
            ssh_argv = lifecycle.guest_ssh_argv(
                ssh_port=instance.ssh_port, identity_file=staged_key,
                known_hosts=staged_known, tty=True)
            result = subprocess.run([*ssh_argv, "/bin/bash -l"],
                                    stdin=None, check=False)
            return result.returncode
        finally:
            _remove_staged_ssh(staged_dir)
    raise Refused("unsupported named profile owner operation")


def _read_request() -> dict:
    data = bytearray()
    while block := sys.stdin.buffer.read(min(1024, MAX_REQUEST + 1 - len(data))):
        data.extend(block)
        if len(data) > MAX_REQUEST:
            raise Refused("profile control request exceeds its fixed bound")
    try:
        request = json.loads(data, object_pairs_hook=_reject_duplicate_pairs)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise Refused("profile control request is malformed JSON") from error
    if not isinstance(request, dict) or set(request) - {"schema_version", "operation", "name", "runtime_sha"}:
        raise Refused("profile control request has unknown fields")
    if type(request.get("schema_version")) is not int or request["schema_version"] != 1:
        raise Refused("profile control request protocol version is unsupported")
    if request.get("operation") not in ("install", "up", "down", "status", "ssh"):
        raise Refused("profile control operation is not allowlisted")
    name = request.get("name")
    policy = _core()._instance_policy()
    if (not isinstance(name, str) or not policy.PROFILE_NAME.fullmatch(name)
            or name in policy.RESERVED_NAMES):
        raise Refused("profile selector is outside its allowed grammar")
    if request["operation"] == "install":
        if set(request) != {"schema_version", "operation", "name", "runtime_sha"}:
            raise Refused("profile install request has an incomplete schema")
        runtime_sha = request["runtime_sha"]
        if not isinstance(runtime_sha, str) or re.fullmatch(r"[0-9a-f]{40}", runtime_sha) is None:
            raise Refused("profile runtime pin must be a complete lowercase commit SHA")
    else:
        if set(request) != {"schema_version", "operation", "name"}:
            raise Refused("profile lifecycle request has an incomplete schema")
        runtime_sha = None
    return {"operation": request["operation"], "name": name, "runtime_sha": runtime_sha}


def _reject_duplicate_pairs(pairs):
    record = {}
    for key, value in pairs:
        if key in record:
            raise Refused("profile control request contains a duplicate JSON key")
        record[key] = value
    return record


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        if os.geteuid() != 0:
            raise Refused("profile control requires its fixed root-owned sudo entrypoint")
        if argv:
            if (len(argv) == 3 and argv[0] == "service" and argv[1] in ("qemu", "proxy")
                    and re.fullmatch(r"[a-z][a-z0-9-]{0,31}", argv[2])):
                if argv[1] == "qemu":
                    _service_qemu(argv[2])
                _service_proxy(argv[2])
                return 2
            if (len(argv) == 4 and argv[0] == "service" and argv[1] == "qmp-stop"
                    and re.fullmatch(r"[a-z][a-z0-9-]{0,31}", argv[2])):
                _service_qmp_stop(argv[2], argv[3])
                return 0
            if (len(argv) == 4 and argv[0] == "service" and argv[1] == "guest-ready"
                    and re.fullmatch(r"[a-z][a-z0-9-]{0,31}", argv[2])):
                _service_guest_ready(argv[2], argv[3])
                return 0
            if (len(argv) == 4 and argv[0] == "service" and argv[1] == "qmp-status"
                    and re.fullmatch(r"[a-z][a-z0-9-]{0,31}", argv[2])):
                _service_qmp_status(argv[2], argv[3])
                return 0
            raise Refused("unsupported fixed profile service invocation")
        request = _read_request()
        raw_uid = os.environ.get("SUDO_UID", "")
        if not re.fullmatch(r"[1-9][0-9]{0,9}", raw_uid):
            raise Refused("profile control request has no valid sudo invoking UID")
        caller_uid = int(raw_uid)
        if caller_uid >= 2**32 - 1:
            raise Refused("profile control request has an out-of-range sudo invoking UID")
        return _owner_operation(request["operation"], request["name"],
                                request["runtime_sha"], caller_uid)
    except (OSError, KeyError, Refused, ValueError, subprocess.SubprocessError) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
