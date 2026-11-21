#!/usr/bin/env python3
"""Strict single-directory assignment policy for the fixed system VM."""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import pathlib
import secrets
import stat
import subprocess
import sys
import pwd
import grp
import time
from dataclasses import dataclass


class Refused(RuntimeError):
    """Workspace state is unsafe, stale, or outside the fixed policy."""


@dataclass(frozen=True)
class Workspace:
    path: pathlib.Path
    identity: tuple[int, int]
    uid: int
    unsafe_recurse: bool = False


ASSIGNMENT = pathlib.Path("/etc/little-mester-vm/workspace.json")
DROPIN_DIR = pathlib.Path("/etc/systemd/system/little-mester-vm-workspace.service.d")
DROPIN = DROPIN_DIR / "20-workspace.conf"
SOCKET_DIR = pathlib.Path("/run/little-mester-vm-workspace")
SOCKET = SOCKET_DIR / "virtiofs.sock"
SHARE_DIR = pathlib.Path("/run/little-mester-vm-workspace-share")
VM_UNIT = "little-mester-vm.service"
VIRTIOFSD_CANDIDATES = (
    "/usr/bin/virtiofsd",
    "/usr/libexec/virtiofsd",
    "/usr/lib/qemu/virtiofsd",
    "/usr/lib/virtiofsd",
)


def _inside(path: pathlib.Path, parent: pathlib.Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_workspace(raw_path: str, *, uid: int, home: pathlib.Path,
                       repository: pathlib.Path, protected_roots: tuple[pathlib.Path, ...],
                       mountpoints=(), unsafe_recurse: bool = False) -> Workspace:
    if type(uid) is not int or uid <= 0 or not isinstance(raw_path, str) or not raw_path:
        raise Refused("workspace path or owner identity is invalid")
    if type(unsafe_recurse) is not bool:
        raise Refused("unsafe-recurse authorization must be explicit")
    raw = pathlib.Path(raw_path).expanduser()
    if not raw.is_absolute():
        raw = pathlib.Path.cwd() / raw
    try:
        path = raw.resolve(strict=True)
        home_path = pathlib.Path(home).resolve(strict=True)
        repo_path = pathlib.Path(repository).resolve(strict=True)
        protected = tuple(pathlib.Path(item).resolve(strict=False) for item in protected_roots)
        nested_mounts = tuple(pathlib.Path(item).resolve(strict=False) for item in mountpoints)
    except OSError as error:
        raise Refused("workspace path or protected boundary cannot be resolved") from error
    if path == pathlib.Path("/") or path == home_path:
        raise Refused("filesystem root and the complete home directory cannot be shared")
    try:
        info = os.lstat(path)
    except OSError as error:
        raise Refused("workspace directory is unavailable") from error
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != uid or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise Refused("workspace must be an owner-controlled directory without group/world write")
    if any(_inside(path, boundary) or _inside(boundary, path) for boundary in protected):
        raise Refused("workspace overlaps protected installation or VM state")
    if any(_inside(mount, path) and mount != path for mount in nested_mounts):
        raise Refused("workspace contains a nested mount boundary")
    if _inside(path, repo_path):
        if not unsafe_recurse:
            raise Refused("sharing the Little Mester repository requires --unsafe-recurse")
    elif _inside(repo_path, path):
        raise Refused("workspace parent contains the Little Mester repository; choose a narrower directory")
    return Workspace(path, (info.st_dev, info.st_ino), uid, unsafe_recurse)


def verify_identity(workspace: Workspace) -> None:
    try:
        info = os.lstat(workspace.path)
    except OSError as error:
        raise Refused("workspace directory changed or disappeared after validation") from error
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != workspace.uid
            or (info.st_dev, info.st_ino) != workspace.identity):
        raise Refused("workspace directory changed after validation")


def _payload(workspace: Workspace) -> dict:
    return {"version": 1, "path": str(workspace.path), "uid": workspace.uid,
            "device": workspace.identity[0], "inode": workspace.identity[1],
            "unsafe_recurse": workspace.unsafe_recurse}


def write_assignment(path: pathlib.Path, workspace: Workspace, *, uid: int,
                     file_uid: int | None = None, file_gid: int = -1,
                     mode: int = 0o600, replace: bool = False) -> None:
    """Atomically persist private owner assignment; never follow a target symlink."""
    path = pathlib.Path(path)
    verify_identity(workspace)
    try:
        parent = path.parent
        parent_info = os.lstat(parent)
        if (not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
                or parent_info.st_uid not in (0, uid)
                or parent_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
            raise Refused("workspace assignment parent is not protected")
        exists = False
        try:
            old = os.lstat(path)
            exists = True
        except FileNotFoundError:
            old = None
        if exists and not replace:
            raise Refused("preserving pre-existing workspace assignment evidence")
        if exists and (not stat.S_ISREG(old.st_mode) or stat.S_ISLNK(old.st_mode)
                       or old.st_uid != 0 or stat.S_IMODE(old.st_mode) != mode
                       or old.st_nlink != 1):
            raise Refused("preserving unsafe pre-existing workspace assignment")
        temp = parent / f".{path.name}.{secrets.token_hex(12)}.tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        try:
            payload = (json.dumps(_payload(workspace), sort_keys=True, separators=(",", ":")) + "\n").encode()
            view = memoryview(payload)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise Refused("workspace assignment write made no progress")
                view = view[count:]
            os.fchmod(fd, mode)
            os.fchown(fd, uid if file_uid is None else file_uid, file_gid)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            if exists:
                os.replace(temp, path)
            else:
                os.link(temp, path, follow_symlinks=False)
                os.unlink(temp)
            dirfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
    except OSError as error:
        raise Refused("could not safely persist workspace assignment") from error


def read_assignment(path: pathlib.Path, *, uid: int,
                    verify_path: bool = True) -> Workspace:
    path = pathlib.Path(path)
    try:
        info = os.lstat(path)
        root_owned_assignment = info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o640
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or (info.st_uid, stat.S_IMODE(info.st_mode)) not in ((uid, 0o600), (0, 0o640))
                or info.st_nlink != 1 or info.st_size > 4096):
            raise Refused("workspace assignment has unsafe owner, type, mode, or size")
        if root_owned_assignment:
            try:
                control_gid = grp.getgrnam("lm-vm-control").gr_gid
            except KeyError as error:
                raise Refused("workspace control group is unavailable") from error
            if info.st_gid != control_gid:
                raise Refused("root workspace assignment is not readable through the fixed control group")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise Refused("workspace assignment changed while opening")
            raw = os.read(fd, 4097)
        finally:
            os.close(fd)
        value = json.loads(raw)
    except Refused:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise Refused("workspace assignment is missing or malformed") from error
    if (not isinstance(value, dict)
            or set(value) != {"version", "path", "uid", "device", "inode", "unsafe_recurse"}
            or value["version"] != 1 or type(value["uid"]) is not int or value["uid"] != uid
            or type(value["device"]) is not int or type(value["inode"]) is not int
            or type(value["unsafe_recurse"]) is not bool
            or not isinstance(value["path"], str) or not pathlib.Path(value["path"]).is_absolute()):
        raise Refused("workspace assignment does not match the fixed schema")
    workspace = Workspace(pathlib.Path(value["path"]), (value["device"], value["inode"]),
                          uid, value["unsafe_recurse"])
    if verify_path:
        verify_identity(workspace)
    return workspace


def assignment_owner_uid(path: pathlib.Path = ASSIGNMENT) -> int:
    """Read only the owner field after validating the protected root file envelope."""
    path = pathlib.Path(path)
    try:
        info = os.lstat(path)
        control_gid = grp.getgrnam("lm-vm-control").gr_gid
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != 0 or info.st_gid != control_gid
                or stat.S_IMODE(info.st_mode) != 0o640 or info.st_nlink != 1
                or info.st_size > 4096):
            raise Refused("root workspace assignment has unsafe metadata")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise Refused("root workspace assignment changed while opening")
            value = json.loads(os.read(fd, 4097))
        finally:
            os.close(fd)
    except Refused:
        raise
    except (OSError, KeyError, UnicodeError, json.JSONDecodeError) as error:
        raise Refused("root workspace assignment is unavailable or malformed") from error
    if (not isinstance(value, dict)
            or set(value) != {"version", "path", "uid", "device", "inode", "unsafe_recurse"}
            or type(value.get("uid")) is not int or value.get("uid") <= 0):
        raise Refused("root workspace assignment has an invalid owner identity")
    return value["uid"]


def _mountpoints() -> tuple[pathlib.Path, ...]:
    points = []
    try:
        lines = pathlib.Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise Refused("host mount table is unavailable") from error
    for line in lines:
        fields = line.split()
        if len(fields) < 5:
            raise Refused("host mount table is malformed")
        encoded = fields[4]
        for sequence, character in (("\\040", " "), ("\\011", "\t"),
                                    ("\\012", "\n"), ("\\134", "\\")):
            encoded = encoded.replace(sequence, character)
        points.append(pathlib.Path(encoded))
    return tuple(points)


def reject_nested_mounts(mountpoints: tuple[pathlib.Path, ...], *,
                         share_root: pathlib.Path = SHARE_DIR) -> None:
    for point in mountpoints:
        if point != share_root and share_root in point.parents:
            raise Refused("workspace contains a nested mount boundary")


def _mount_context(path: pathlib.Path) -> dict:
    """Return bounded mount metadata for path without disclosing host paths."""
    try:
        lines = pathlib.Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError as error:
        return {"mountinfo": "unavailable", "errno": error.errno}
    best = None
    for line in lines:
        fields = line.split()
        if len(fields) < 10 or "-" not in fields:
            continue
        separator = fields.index("-")
        if separator + 2 >= len(fields):
            continue
        mountpoint = fields[4]
        for sequence, character in (("\\040", " "), ("\\011", "\t"),
                                    ("\\012", "\n"), ("\\134", "\\")):
            mountpoint = mountpoint.replace(sequence, character)
        point = pathlib.Path(mountpoint)
        if path == point or point in path.parents:
            if best is None or len(point.parts) > best[0]:
                best = (len(point.parts), {
                    "mount_id": int(fields[0]),
                    "is_exact_mountpoint": path == point,
                    "filesystem": fields[separator + 1],
                })
    return best[1] if best is not None else {"mountinfo": "no-covering-entry"}


def _path_identity(path: pathlib.Path) -> dict:
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as error:
        return {"state": "unavailable", "errno": error.errno,
                "error": errno.errorcode.get(error.errno, "UNKNOWN")}
    return {
        "state": "present",
        "kind": "directory" if stat.S_ISDIR(info.st_mode) else "other",
        "device": info.st_dev,
        "inode": info.st_ino,
        "uid": info.st_uid,
        "gid": info.st_gid,
        "mode": stat.S_IMODE(info.st_mode),
    }


def _source_visibility(path: pathlib.Path, source: dict, mount: dict) -> str:
    if source.get("state") == "present":
        return "visible"
    if source.get("error") in ("EACCES", "EPERM"):
        return "inaccessible"
    if (source.get("error") == "ENOENT" and str(path).startswith("/home/")
            and mount.get("filesystem") == "tmpfs"):
        return "hidden-by-ProtectHome-tmpfs"
    if source.get("error") == "ENOENT":
        try:
            os.stat(path.parent, follow_symlinks=False)
            return "missing-source"
        except PermissionError:
            return "inaccessible-parent"
        except FileNotFoundError:
            return "missing-parent"
        except OSError:
            return "inaccessible-parent"
    return "unavailable"


def _workspace_bind_diagnostic(workspace: Workspace, target: pathlib.Path,
                               dropin_matches: bool) -> dict:
    source = _path_identity(workspace.path)
    destination = _path_identity(target)
    source_mount = _mount_context(workspace.path)
    target_mount = _mount_context(target)
    expected = {"device": workspace.identity[0], "inode": workspace.identity[1]}
    source_matches = (source.get("state") == "present"
                      and (source.get("device"), source.get("inode")) == workspace.identity)
    target_matches = (destination.get("state") == "present"
                      and (destination.get("device"), destination.get("inode")) == workspace.identity)
    source_visibility = _source_visibility(workspace.path, source, source_mount)
    if not dropin_matches:
        diagnosis = "incorrect-bind-configuration"
    elif source["state"] == "present" and not source_matches:
        diagnosis = "approved-source-replaced"
    elif destination["state"] == "unavailable":
        diagnosis = "destination-inaccessible" if destination.get("error") in (
            "EACCES", "EPERM") else "destination-missing-or-bind-absent"
    elif not target_mount.get("is_exact_mountpoint"):
        diagnosis = "bind-absent-or-wrong-destination"
    elif not target_matches:
        diagnosis = "bind-target-identity-mismatch"
    else:
        diagnosis = "approved-bind-present"
    return {
        "event": "workspace_bind_identity",
        "diagnosis": diagnosis,
        "expected": expected,
        "source": source,
        "source_visibility": source_visibility,
        "source_matches_assignment": source_matches if source["state"] == "present" else None,
        "source_mount": source_mount,
        "destination": destination,
        "destination_matches_assignment": target_matches,
        "destination_mount": target_mount,
        "dropin_matches_assignment": dropin_matches,
    }


def _log_workspace_bind_diagnostic(workspace: Workspace, target: pathlib.Path,
                                   dropin_matches: bool) -> None:
    try:
        detail = _workspace_bind_diagnostic(workspace, target, dropin_matches)
        print("WORKSPACE_BIND_DIAGNOSTIC " + json.dumps(detail, sort_keys=True,
                                                        separators=(",", ":")),
              file=sys.stderr, flush=True)
    except (OSError, ValueError, TypeError):
        # Diagnostics must never change fail-closed startup behavior.
        print("WORKSPACE_BIND_DIAGNOSTIC {\"event\":\"unavailable\"}",
              file=sys.stderr, flush=True)


def _verify_bind_identity(workspace: Workspace, target: pathlib.Path,
                          dropin_matches: bool) -> None:
    source = _path_identity(workspace.path)
    destination = _path_identity(target)
    source_mount = _mount_context(workspace.path)
    target_mount = _mount_context(target)
    if source["state"] == "present" and (
            source.get("device"), source.get("inode")) != workspace.identity:
        _log_workspace_bind_diagnostic(workspace, target, dropin_matches)
        raise Refused("approved host workspace source changed after assignment")
    if (source["state"] != "present"
            and _source_visibility(workspace.path, source, source_mount)
            != "hidden-by-ProtectHome-tmpfs"):
        _log_workspace_bind_diagnostic(workspace, target, dropin_matches)
        raise Refused("approved host workspace source is missing or inaccessible at service startup")
    if (destination["state"] != "present" or destination.get("kind") != "directory"
            or (destination.get("device"), destination.get("inode")) != workspace.identity
            or not target_mount.get("is_exact_mountpoint") or not dropin_matches):
        _log_workspace_bind_diagnostic(workspace, target, dropin_matches)
        raise Refused("host workspace bind target differs from the approved source identity")
def _systemd_bind_path(path: pathlib.Path) -> str:
    raw = str(path)
    if not raw.startswith("/"):
        raise Refused("workspace path must be absolute before generating systemd BindPaths")
    # BindPaths has its own colon/list parser after unit-file parsing. In the
    # supported systemd versions, backslash and quote escaping is not preserved
    # as a literal path in this field (and whole-item quotes duplicate fields).
    # Emit only a conservative alphabet that needs no parser escaping; reject
    # everything else before configure() publishes either file.
    safe = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/._-")
    if any(character not in safe for character in raw):
        raise Refused(
            "workspace path contains characters unsupported by systemd BindPaths; "
            "use only ASCII letters, digits, '/', '.', '_' or '-'")
    return f"[Service]\nBindPaths={raw}:{SHARE_DIR}\n"


def effective_bind_paths_match(value: str, source: pathlib.Path | str,
                               destination: pathlib.Path | str) -> bool:
    """Accept exactly one effective recursive bind with the requested endpoints."""
    return value.split(":") == [str(source), str(destination), "rbind"]


def verify_host_assignment(uid: int) -> Workspace:
    """Validate assignment and generated systemd source before lifecycle actions."""
    workspace = read_assignment(ASSIGNMENT, uid=uid)
    try:
        actual = DROPIN.read_bytes()
    except OSError as error:
        raise Refused("workspace systemd bind configuration is unavailable") from error
    if actual != _systemd_bind_path(workspace.path).encode("utf-8"):
        raise Refused("workspace assignment and systemd bind configuration disagree")
    return workspace


def _atomic_root_file(path: pathlib.Path, payload: bytes, *, mode: int, group: int,
                      replace: bool) -> None:
    if os.geteuid() != 0:
        raise Refused("workspace configuration is root-only")
    try:
        parent = path.parent
        parent_info = os.lstat(parent)
        if (not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
                or parent_info.st_uid != 0 or parent_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
            raise Refused("workspace configuration parent is not root-protected")
        try:
            old = os.lstat(path)
        except FileNotFoundError:
            old = None
        if old is not None:
            if (not replace or not stat.S_ISREG(old.st_mode) or stat.S_ISLNK(old.st_mode)
                    or old.st_uid != 0 or old.st_gid != group
                    or stat.S_IMODE(old.st_mode) != mode or old.st_nlink != 1):
                raise Refused("preserving pre-existing workspace configuration evidence")
        temp = parent / f".{path.name}.{secrets.token_hex(12)}.tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        try:
            view = memoryview(payload)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise Refused("workspace configuration write made no progress")
                view = view[count:]
            os.fchmod(fd, mode)
            os.fchown(fd, 0, group)
            os.fsync(fd)
        finally:
            os.close(fd)
        if old is None:
            os.link(temp, path, follow_symlinks=False)
            os.unlink(temp)
        else:
            os.replace(temp, path)
        dirfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except OSError as error:
        raise Refused("could not safely write root workspace configuration") from error


def _remove_root_file_if_unchanged(path: pathlib.Path, payload: bytes, *, mode: int,
                                   group: int) -> None:
    """Remove a newly published config file only if it is still our exact file."""
    dirfd = -1
    fd = -1
    try:
        dirfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.stat(path.name, dir_fd=dirfd, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != 0 or info.st_gid != group
                or stat.S_IMODE(info.st_mode) != mode or info.st_nlink != 1):
            raise Refused("preserving changed workspace configuration during rollback")
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dirfd)
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise Refused("workspace configuration changed while rolling back")
        contents = bytearray()
        while len(contents) <= len(payload):
            chunk = os.read(fd, min(4096, len(payload) + 1 - len(contents)))
            if not chunk:
                break
            contents.extend(chunk)
        if bytes(contents) != payload:
            raise Refused("preserving changed workspace configuration during rollback")
        current = os.stat(path.name, dir_fd=dirfd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise Refused("workspace configuration changed before rollback removal")
        os.unlink(path.name, dir_fd=dirfd)
        os.fsync(dirfd)
    except FileNotFoundError:
        return
    except OSError as error:
        raise Refused("could not safely roll back workspace configuration") from error
    finally:
        if fd >= 0:
            os.close(fd)
        if dirfd >= 0:
            os.close(dirfd)


def _restore_workspace_pair(assignment_path: pathlib.Path, dropin_path: pathlib.Path, *,
                            assignment: bytes, previous_assignment: bytes | None,
                            dropin: bytes, previous_dropin: bytes | None,
                            control_gid: int) -> None:
    """Restore only our published files, and only if both match known states."""
    def current_bytes(path: pathlib.Path) -> bytes | None:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise Refused("workspace configuration cannot be inspected during rollback") from error

    current_dropin = current_bytes(dropin_path)
    current_assignment = current_bytes(assignment_path)
    if (current_dropin not in (dropin, previous_dropin)
            or current_assignment not in (assignment, previous_assignment)):
        raise Refused(
            "workspace configuration changed during rollback; preserving both files for recovery")

    if current_dropin == dropin:
        if previous_dropin is None:
            _remove_root_file_if_unchanged(dropin_path, dropin, mode=0o644, group=0)
        else:
            _atomic_root_file(dropin_path, previous_dropin,
                              mode=0o644, group=0, replace=True)
    if current_assignment == assignment:
        if previous_assignment is None:
            _remove_root_file_if_unchanged(
                assignment_path, assignment, mode=0o640, group=control_gid)
        else:
            _atomic_root_file(assignment_path, previous_assignment,
                              mode=0o640, group=control_gid, replace=True)


def configure(raw_path: str, repository: pathlib.Path, *, owner_uid: int,
              unsafe_recurse: bool, reassign: bool) -> Workspace:
    if os.geteuid() != 0:
        raise Refused("workspace assignment requires the fixed root configurator")
    try:
        owner = pwd.getpwuid(owner_uid)
        control_gid = grp.getgrnam("lm-vm-control").gr_gid
    except KeyError as error:
        raise Refused("workspace owner or control group is unavailable") from error
    if owner_uid == 0:
        raise Refused("root cannot be the workspace owner")
    lock_path = pathlib.Path("/run/little-mester-vm-workspace-config.lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        lock_info = os.fstat(lock_fd)
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != 0
                or stat.S_IMODE(lock_info.st_mode) != 0o600 or lock_info.st_nlink != 1):
            raise Refused("workspace configuration lock has unsafe metadata")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        for unit in (VM_UNIT, "little-mester-vm-proxy.service",
                     "little-mester-vm-workspace.service", "little-mester-vm-metrics.service"):
            state = subprocess.run(["/usr/bin/systemctl", "show", "--property=ActiveState",
                                    "--value", unit], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, check=False)
            if state.returncode or state.stdout.strip() not in ("inactive", "failed"):
                raise Refused("workspace cannot be assigned or changed unless both fixed services are stopped")
        home = pathlib.Path(owner.pw_dir)
        workspace = validate_workspace(
            raw_path, uid=owner_uid, home=home, repository=repository,
            protected_roots=(pathlib.Path("/etc/little-mester-vm"),
                             pathlib.Path("/var/lib/little-mester-vm"),
                             pathlib.Path("/opt/little-mester")),
            mountpoints=_mountpoints(), unsafe_recurse=unsafe_recurse)
        try:
            previous = read_assignment(ASSIGNMENT, uid=owner_uid,
                                       verify_path=not reassign)
        except Refused as error:
            if "missing or malformed" not in str(error):
                raise
            previous = None
        if previous is not None and previous != workspace and not reassign:
            raise Refused("workspace is already assigned; use explicit --reassign while the VM is stopped")
        if previous == workspace:
            try:
                if DROPIN.read_bytes() != _systemd_bind_path(workspace.path).encode("utf-8"):
                    raise Refused("workspace assignment and systemd bind configuration disagree")
            except OSError as error:
                raise Refused("workspace systemd bind configuration is unavailable") from error
            return workspace
        rendered = _systemd_bind_path(workspace.path).encode("utf-8")
        assignment = (json.dumps(_payload(workspace), sort_keys=True, separators=(",", ":")) + "\n").encode()
        previous_assignment_bytes = ASSIGNMENT.read_bytes() if previous is not None else None
        if previous is None:
            try:
                os.lstat(ASSIGNMENT)
            except FileNotFoundError:
                pass
            else:
                raise Refused("preserving pre-existing workspace assignment evidence")
        try:
            dropin_info = os.lstat(DROPIN)
        except FileNotFoundError:
            previous_dropin_bytes = None
        else:
            if (not stat.S_ISREG(dropin_info.st_mode) or stat.S_ISLNK(dropin_info.st_mode)
                    or dropin_info.st_uid != 0 or dropin_info.st_gid != 0
                    or stat.S_IMODE(dropin_info.st_mode) != 0o644 or dropin_info.st_nlink != 1):
                raise Refused("preserving pre-existing workspace drop-in with unsafe metadata")
            previous_dropin_bytes = DROPIN.read_bytes()
        reload_attempted = False
        try:
            _atomic_root_file(ASSIGNMENT, assignment, mode=0o640, group=control_gid,
                              replace=previous is not None)
            _atomic_root_file(DROPIN, rendered, mode=0o644, group=0, replace=reassign)
            reload_attempted = True
            subprocess.run(["/usr/bin/systemctl", "daemon-reload"], check=True,
                           stdin=subprocess.DEVNULL)
        except BaseException as primary_error:
            # The two files cannot be renamed as one filesystem transaction.
            # Inspect both before mutating either: a concurrent root writer
            # must not have its assignment paired with our rollback drop-in.
            try:
                _restore_workspace_pair(
                    ASSIGNMENT, DROPIN, assignment=assignment,
                    previous_assignment=previous_assignment_bytes,
                    dropin=rendered, previous_dropin=previous_dropin_bytes,
                    control_gid=control_gid)
                if reload_attempted:
                    subprocess.run(["/usr/bin/systemctl", "daemon-reload"], check=True,
                                   stdin=subprocess.DEVNULL)
            except BaseException as rollback_error:
                raise Refused(
                    "workspace configuration failed and rollback/reload also failed; "
                    "preserving both files for recovery"
                ) from rollback_error
            raise primary_error
        return workspace
    finally:
        os.close(lock_fd)


def fix_socket_permissions() -> None:
    """Wait for and verify the owner-created socket; never mutate it as root."""
    deadline = time.monotonic() + 8
    while True:
        try:
            info = os.lstat(SOCKET)
            break
        except FileNotFoundError:
            if time.monotonic() >= deadline:
                raise Refused("virtiofsd did not create its socket before the startup deadline")
            time.sleep(0.05)
    try:
        assignment_uid = assignment_owner_uid()
        if os.geteuid() != assignment_uid:
            raise Refused("virtiofs socket readiness must run as the assigned workspace owner")
        read_assignment(ASSIGNMENT, uid=assignment_uid, verify_path=False)
        owner = pwd.getpwuid(assignment_uid)
        control_gid = grp.getgrnam("lm-vm-control").gr_gid
        directory = os.lstat(SOCKET_DIR)
    except (KeyError, OSError) as error:
        raise Refused("workspace runtime directory or socket is unavailable") from error
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != owner.pw_uid
            or directory.st_gid not in (owner.pw_gid, control_gid)
            or stat.S_IMODE(directory.st_mode) != 0o750
            or not stat.S_ISSOCK(info.st_mode) or info.st_uid != owner.pw_uid
            or info.st_gid != control_gid or stat.S_IMODE(info.st_mode) != 0o770
            or info.st_nlink != 1):
        raise Refused("virtiofs socket has unsafe type, owner, or runtime directory")
    # virtiofsd's --socket-group intentionally yields 0770 (its fixed umask
    # protects all other users). The assigned owner is also a member of the
    # fixed control group, so it can safely assign that group to its private
    # runtime directory and remove the semantically unused socket execute bits.
    os.chown(SOCKET_DIR, owner.pw_uid, control_gid, follow_symlinks=False)
    os.chmod(SOCKET_DIR, 0o750, follow_symlinks=False)
    os.chmod(SOCKET, 0o660, follow_symlinks=False)
    directory = os.lstat(SOCKET_DIR)
    info = os.lstat(SOCKET)
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != owner.pw_uid
            or directory.st_gid != control_gid or stat.S_IMODE(directory.st_mode) != 0o750
            or not stat.S_ISSOCK(info.st_mode) or info.st_uid != owner.pw_uid
            or info.st_gid != control_gid or stat.S_IMODE(info.st_mode) != 0o660
            or info.st_nlink != 1):
        raise Refused("virtiofs socket permissions did not normalize safely")


def verify_service_socket() -> None:
    try:
        info = os.lstat(SOCKET)
        expected_gid = grp.getgrnam("lm-vm-control").gr_gid
        directory = os.lstat(SOCKET_DIR)
        owner_uid = assignment_owner_uid()
    except (OSError, KeyError) as error:
        raise Refused("workspace virtiofs socket is unavailable") from error
    read_assignment(ASSIGNMENT, uid=owner_uid, verify_path=False)
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != owner_uid
            or directory.st_gid != expected_gid or stat.S_IMODE(directory.st_mode) != 0o750
            or not stat.S_ISSOCK(info.st_mode) or info.st_uid != owner_uid or info.st_gid != expected_gid
            or stat.S_IMODE(info.st_mode) != 0o660 or info.st_nlink != 1):
        raise Refused("workspace virtiofs socket has unsafe access metadata")


def verify_service_workspace() -> None:
    assignment_uid = assignment_owner_uid()
    workspace = read_assignment(ASSIGNMENT, uid=assignment_uid, verify_path=False)
    try:
        actual = DROPIN.read_bytes()
    except OSError as error:
        raise Refused("validated host workspace bind mount is unavailable") from error
    dropin_matches = actual == _systemd_bind_path(workspace.path).encode("utf-8")
    _verify_bind_identity(workspace, SHARE_DIR, dropin_matches)


def build_virtiofsd_argv(executable: str, *, uid: int, gid: int) -> list[str]:
    if (executable not in VIRTIOFSD_CANDIDATES or type(uid) is not int or uid <= 0
            or type(gid) is not int or gid < 0):
        raise Refused("virtiofsd executable or fixed identity mapping is invalid")
    return [executable, "--socket-path", str(SOCKET), "--socket-group", "lm-vm-control",
            "--shared-dir", str(SHARE_DIR),
            "--sandbox", "namespace", "--no-announce-submounts",
            f"--uid-map=:1000:{uid}:1:", f"--gid-map=:1000:{gid}:1:"]


def _serve() -> None:
    assignment_uid = assignment_owner_uid()
    workspace = read_assignment(ASSIGNMENT, uid=assignment_uid, verify_path=False)
    mount_text = _systemd_bind_path(workspace.path).encode()
    try:
        actual = DROPIN.read_bytes()
    except OSError as error:
        raise Refused("systemd workspace bind configuration is unavailable") from error
    dropin_matches = actual == mount_text
    _verify_bind_identity(workspace, SHARE_DIR, dropin_matches)
    reject_nested_mounts(_mountpoints())
    executable = None
    for candidate in VIRTIOFSD_CANDIDATES:
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            continue
        if (stat.S_ISREG(info.st_mode) and info.st_uid == 0
                and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) == 0
                and os.access(candidate, os.X_OK)):
            executable = candidate
            break
    if executable is None:
        raise Refused("no protected virtiofsd executable exists at a supported fixed path")
    owner = pwd.getpwuid(assignment_uid)
    os.execv(executable, build_virtiofsd_argv(executable, uid=owner.pw_uid, gid=owner.pw_gid))


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="action", required=True)
    check = commands.add_parser("check")
    check.add_argument("--dir", required=True)
    check.add_argument("--repository", required=True)
    check.add_argument("--home", default=str(pathlib.Path.home()))
    check.add_argument("--unsafe-recurse", action="store_true")
    commands.add_parser("configure")
    commands.add_parser("serve")
    commands.add_parser("serve-check")
    commands.add_parser("socket-permissions")
    commands.add_parser("verify")
    args = parser.parse_args()
    try:
        if args.action == "check":
            workspace = validate_workspace(args.dir, uid=os.getuid(),
                home=pathlib.Path(args.home), repository=pathlib.Path(args.repository),
                protected_roots=(pathlib.Path("/etc/little-mester-vm"),
                                 pathlib.Path("/var/lib/little-mester-vm")),
                mountpoints=(), unsafe_recurse=args.unsafe_recurse)
            print(json.dumps(_payload(workspace), sort_keys=True))
            return 0
        if args.action == "configure":
            request = json.load(sys.stdin)
            if (not isinstance(request, dict)
                    or set(request) != {"dir", "repository", "owner_uid", "unsafe_recurse", "reassign"}
                    or not isinstance(request["dir"], str)
                    or not isinstance(request["repository"], str)
                    or type(request["owner_uid"]) is not int
                    or type(request["unsafe_recurse"]) is not bool
                    or type(request["reassign"]) is not bool):
                raise Refused("workspace configuration request does not match the fixed schema")
            if os.environ.get("SUDO_UID") != str(request["owner_uid"]):
                raise Refused("workspace configurator caller identity does not match sudo owner")
            workspace = configure(request["dir"], pathlib.Path(request["repository"]),
                owner_uid=request["owner_uid"], unsafe_recurse=request["unsafe_recurse"],
                reassign=request["reassign"])
            print(str(workspace.path))
            return 0
        if args.action == "serve":
            _serve()
        if args.action == "serve-check":
            verify_service_workspace()
            return 0
        if args.action == "socket-permissions":
            fix_socket_permissions()
            return 0
        if args.action == "verify":
            verify_service_socket()
            return 0
    except (OSError, Refused, ValueError) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
