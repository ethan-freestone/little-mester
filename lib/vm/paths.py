#!/usr/bin/env python3
"""VM path/config policy and private metadata, using only the standard library.

Profile v1 requires exactly PROFILE_FIELDS plus optional runtime_identity. Image
path/hash, resolved OpenCode version and payload hashes may be null while
allocated/starting/provisioning/failed/unknown/deleting. Every field remains
required; image path/hash must be null together, and supplied hashes/versions
must always be valid. Ready/suspending/suspended require all final metadata.
Optional runtime_identity has exactly pid, start_ticks, executable, disk_path,
socket_path, ssh_port, argv; it records identity, never authorizes signalling.

Runtime installs accept an explicit list of regular files (no directory walking
or symlinks). runtime/manifest.json records paths, source modes, sizes and hashes;
runtime_payload_sha256 hashes its canonical JSON. An existing runtime must match
both the requested source and its manifest exactly; installs never update it.
Root allocation lock is always acquired before a profile lock. Readers never
create locks or directories; atomic replacement makes unlocked reads coherent.
"""

import argparse
import ctypes
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit


class PolicyError(ValueError):
    """Untrusted input or unsafe filesystem state; never include input values."""


def _policy_debug_enabled():
    return os.environ.get("VM_POLICY_DEBUG") == "1"


def _policy_debug(message):
    if _policy_debug_enabled():
        print(f"[debug] {message}", file=sys.stderr)


LOG_SEGMENT_BYTES = 10 * 1024 * 1024


class BoundedLogWriter:
    """Write a continuously drained stream to one bounded current + prior file."""

    def __init__(self, path, max_bytes=LOG_SEGMENT_BYTES):
        require(type(max_bytes) is int and 1 <= max_bytes <= LOG_SEGMENT_BYTES)
        self.path = canonical(str(path))
        self.parent = self.path.parent
        require(self.parent.is_dir() and not self.parent.is_symlink())
        private_entry(self.parent)
        self.rotated = self.path.with_name(self.path.name + ".1")
        require(not self.path.exists() and not self.path.is_symlink())
        require(not self.rotated.exists() and not self.rotated.is_symlink())
        self.max_bytes = max_bytes
        self.current_size = 0
        self.fd = None

    def _open_current(self):
        self.fd = os.open(
            self.path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )

    def _rotate(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.rotated.exists() or self.rotated.is_symlink():
            private_entry(self.rotated)
            require(self.rotated.stat().st_size <= self.max_bytes)
            self.rotated.unlink()
        if self.path.exists():
            private_entry(self.path)
            require(self.path.stat().st_size <= self.max_bytes)
            self.path.replace(self.rotated)
        self.current_size = 0
        self._open_current()

    def write(self, data):
        view = memoryview(data)
        while view:
            if self.fd is None:
                self._open_current()
            remaining = self.max_bytes - self.current_size
            if remaining <= 0:
                self._rotate()
                remaining = self.max_bytes
            chunk = view[:remaining]
            offset = 0
            while offset < len(chunk):
                offset += os.write(self.fd, chunk[offset:])
            self.current_size += len(chunk)
            view = view[len(chunk):]

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


def require(condition):
    if not condition:
        raise PolicyError("VM policy validation failed")


def text(value):
    require(isinstance(value, str) and bool(value))
    require(not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value))
    return value


def integer(value, low, high):
    require(type(value) is int and low <= value <= high)


def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def decode(raw):
    require(len(raw) <= 1024 * 1024)
    return json.loads(raw, object_pairs_hook=no_duplicates,
                      parse_constant=lambda _: require(False))


def encoded(data):
    return (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()


def overlaps(a, b):
    return a == b or a in b.parents or b in a.parents


def canonical(value, *, exists=False):
    path = Path(text(str(value)))
    require(path.is_absolute())
    require(str(path) == str(value) and ".." not in path.parts)
    for part in [*reversed(path.parents), path]:
        require(not part.is_symlink())
    require(path.resolve(strict=exists) == path)
    return path


def private_entry(path):
    info = path.lstat()
    require(info.st_uid == os.getuid() and not stat.S_ISLNK(info.st_mode))
    if stat.S_ISDIR(info.st_mode):
        require(stat.S_IMODE(info.st_mode) == 0o700)
    else:
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600)
        require(info.st_nlink == 1)


def _private_entry_issue(path, *, runtime=False):
    """Return a safe reason code and metadata for a private-entry violation."""
    try:
        info = path.lstat()
    except OSError:
        return "entry_unreadable", None
    if info.st_uid != os.getuid():
        return "owner_mismatch", info
    if stat.S_ISLNK(info.st_mode):
        return "symlink", info
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISDIR(info.st_mode):
        return ("directory_mode", info) if mode != 0o700 else (None, info)
    if not stat.S_ISREG(info.st_mode):
        return "not_regular_file", info
    if info.st_nlink != 1:
        return "hard_link_count", info
    allowed_modes = (0o600, 0o700) if runtime else (0o600,)
    if mode not in allowed_modes:
        return "file_mode", info
    return None, info


def walk_error(error):
    raise error


def _debug_state_root_scan(root):
    """Scan all descendants and report bounded, content-free policy diagnostics."""
    checked = 0
    issue_count = 0
    walk_error_count = 0
    issues = []
    walk_errors = []

    def relative_path(path):
        try:
            relative = Path(path).relative_to(root).as_posix()
        except (TypeError, ValueError):
            relative = "<unavailable>"
        return relative[:512]

    def onerror(error):
        nonlocal walk_error_count
        walk_error_count += 1
        if len(walk_errors) < 25:
            walk_errors.append((relative_path(getattr(error, "filename", None)), type(error).__name__))

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
        for name in dirs + files:
            child = Path(directory) / name
            checked += 1
            parts = child.relative_to(root).parts
            runtime = len(parts) >= 4 and parts[:2] == ("vm", "profiles") and parts[3] == "runtime"
            reason, info = _private_entry_issue(child, runtime=runtime)
            if reason:
                issue_count += 1
                if len(issues) < 25:
                    issues.append((relative_path(child), reason, info))

    if not issue_count and not walk_error_count:
        _policy_debug(f"state_root.private_tree=PASS entries_checked={checked}")
        return

    _policy_debug(
        "state_root.private_tree=FAIL "
        f"entries_checked={checked} violations={issue_count} walk_errors={walk_error_count}"
    )
    displayed = 0
    for relative, reason, info in issues[:25]:
        metadata = ""
        if info is not None:
            metadata = (
                f" uid={info.st_uid} mode={stat.S_IMODE(info.st_mode):04o}"
                f" links={info.st_nlink}"
            )
        _policy_debug(
            f"state_root.entry=FAIL path={json.dumps(relative, ensure_ascii=True)} "
            f"reason={reason}{metadata}"
        )
        displayed += 1
    for relative, error_type in walk_errors[: max(0, 25 - displayed)]:
        _policy_debug(
            f"state_root.walk=FAIL path={json.dumps(relative, ensure_ascii=True)} "
            f"reason={error_type}"
        )
        displayed += 1
    omitted = issue_count + walk_error_count - displayed
    if omitted:
        _policy_debug(f"state_root.additional_violations_omitted={omitted}")
    raise PolicyError("VM policy validation failed")


def state_root(value, *, must_exist=False, scan=True):
    try:
        root = canonical(value, exists=must_exist)
    except (OSError, PolicyError):
        _policy_debug("state_root.canonical=FAIL")
        raise
    _policy_debug("state_root.canonical=PASS")
    try:
        require(root != Path("/") and root != Path.home().resolve())
        for ancestor in root.parents:
            if ancestor.exists():
                info = ancestor.stat()
                require(stat.S_ISDIR(info.st_mode))
                require(info.st_uid in (0, os.getuid()))
                # Root-owned sticky temp directories are safe namespace ancestors,
                # not private state. Other writable ancestry is not trusted.
                writable = info.st_mode & 0o022
                require(not writable or (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))
    except (OSError, PolicyError):
        _policy_debug("state_root.ancestors=FAIL")
        raise
    _policy_debug("state_root.ancestors=PASS")
    if root.exists():
        try:
            require(root.is_dir())
            private_entry(root)
        except (OSError, PolicyError):
            info = None
            try:
                info = root.lstat()
            except OSError:
                pass
            if info is None:
                _policy_debug("state_root.root_metadata=FAIL reason=unreadable")
            else:
                reason = "not_directory"
                if stat.S_ISDIR(info.st_mode):
                    reason, _ = _private_entry_issue(root)
                _policy_debug(
                    "state_root.root_metadata=FAIL "
                    f"reason={reason or 'policy_violation'} uid={info.st_uid} "
                    f"mode={stat.S_IMODE(info.st_mode):04o} links={info.st_nlink}"
                )
            raise
        _policy_debug("state_root.root_metadata=PASS")
        if not scan:
            _policy_debug("state_root.private_tree=SKIPPED")
            return root
        if _policy_debug_enabled():
            _debug_state_root_scan(root)
        else:
            for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
                for name in dirs + files:
                    child = Path(directory) / name
                    # Runtime executable files are private 0700, not metadata 0600.
                    parts = child.relative_to(root).parts
                    if len(parts) >= 4 and parts[:2] == ("vm", "profiles") and parts[3] == "runtime":
                        info = child.lstat()
                        require(info.st_uid == os.getuid())
                        require(not stat.S_ISLNK(info.st_mode))
                        require(stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                        if stat.S_ISDIR(info.st_mode):
                            require(stat.S_IMODE(info.st_mode) == 0o700)
                        else:
                            require(info.st_nlink == 1 and stat.S_IMODE(info.st_mode) in (0o600, 0o700))
                    else:
                        private_entry(child)
    else:
        _policy_debug("state_root.root_metadata=ABSENT")
        if scan:
            _policy_debug("state_root.private_tree=PASS entries_checked=0")
    return root


def profile_name(name):
    require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name) is not None)
    return name


def profile_path(root, name):
    name = profile_name(name)
    vm = root / "vm"
    namespaces = {"profiles", "images", "services", "runs", "logs", "reports", ".locks"}
    require(not (vm / "base.qcow2").exists())
    if name not in namespaces:
        require(not (vm / name).exists())
    if vm.exists():
        require(all(p.name in namespaces for p in vm.iterdir()))
    return vm / "profiles" / name


def workspace_path(value, root, repository, recurse=False, unsafe=False):
    workspace = Path(text(str(value))).resolve(strict=True)
    repo = Path(text(str(repository))).resolve(strict=True)
    require(workspace.is_dir() and workspace.stat().st_uid == os.getuid())
    require(repo.is_dir())
    home = Path.home().resolve()
    require(workspace not in (Path("/"), home))
    sensitive = [home / name for name in (
        ".ssh", ".aws", ".azure", ".gnupg", ".kube", ".docker", ".config", ".local",
        ".cache", ".netrc", ".npmrc", ".pypirc", ".git-credentials", ".bashrc", ".zshrc",
    )]
    sensitive += [Path(p) for p in ("/etc", "/proc", "/sys", "/dev", "/run", "/boot", "/var/lib")]
    require(not any(overlaps(workspace, item.resolve()) for item in sensitive + [root]))
    for parent in (workspace, *workspace.parents):
        require(not (parent / ".mester").exists() and not (parent / ".mester").is_symlink())
    if unsafe:
        require(workspace == repo or workspace in repo.parents)
    if overlaps(workspace, repo):
        require(recurse or unsafe)
    return workspace


def config_read(root):
    path = root / "config.json"
    data = decode(path.read_bytes()) if path.exists() else {}
    return config_validate(root, data)


def config_validate(root, data):
    result = {"schema_version": 1, "host_profile": "linux-x86_64", "workload_runtime": "docker",
              "opencode_version": "latest", "agent": "opencode", "model": {"provider": "none"},
              "metrics": "on", "auth_forward": "off", "auth_escrow_dir": str(root / "secrets")}
    require(isinstance(data, dict) and data.keys() <= result.keys())
    result.update(data)
    require(type(result["schema_version"]) is int and result["schema_version"] == 1)
    require(result["host_profile"] == "linux-x86_64")
    require(result["workload_runtime"] in ("docker", "none"))
    require(result["agent"] in ("opencode", "aider"))
    opencode_version(result["opencode_version"], latest=True)
    require(result["metrics"] in ("on", "off"))
    model = result["model"]
    require(isinstance(model, dict) and model.keys() <= {"provider", "port", "id"})
    require(model.get("provider") in ("none", "ollama", "llamacpp", "remote"))
    if model["provider"] in ("ollama", "llamacpp"):
        integer(model.get("port"), 1, 65535)
    elif "port" in model:
        raise PolicyError("VM policy validation failed")
    if "id" in model:
        require(len(text(model["id"])) <= 512)
    auth = result["auth_forward"]
    if isinstance(auth, list):
        require(bool(auth) and all(isinstance(p, str) and p in ("openai", "anthropic", "kimi") for p in auth))
        require(len(auth) == len(set(auth)))
    else:
        require(auth in ("off", "auto"))
    escrow = canonical(text(result["auth_escrow_dir"]))
    require(escrow not in (Path("/"), Path.home().resolve()))
    # Validate ancestry without requiring the selector to exist. The default is
    # beneath the already validated private (or not-yet-created) state root; its
    # nearest existing ancestor may legitimately be a 0755 home directory.
    state_root(str(escrow))
    if escrow != root / "secrets" and not escrow.exists():
        parent = escrow.parent
        while not parent.exists():
            parent = parent.parent
        private_entry(parent)
    return result


def version(value, *, latest=False):
    text(value)
    require((latest and value == "latest") or re.fullmatch(
        r"v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", value) is not None)


def opencode_version(value, *, latest=False):
    version(value, latest=latest)
    require(value == "latest" or re.fullmatch(
        r"v?2\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", value) is not None)


PROFILE_FIELDS = {
    "schema_version", "profile_id", "name", "workspace", "image_sha256", "image_path", "memory_gib",
    "depth", "max_depth", "descendants_possible", "opencode_version", "guest_payload_sha256",
    "runtime_payload_sha256", "status",
}
STATUSES = {"allocated", "starting", "provisioning", "ready", "suspending", "suspended", "failed", "deleting", "unknown"}
PROFILE_TRANSITIONS = {
    "allocated": {"starting", "failed", "deleting"},
    "starting": {"provisioning", "ready", "failed", "unknown"},
    "provisioning": {"ready", "failed", "unknown"},
    "ready": {"suspending", "unknown"},
    "suspending": {"suspended", "ready", "unknown"},
    "suspended": {"starting", "deleting", "unknown"},
    "failed": {"starting", "deleting", "unknown"},
    "deleting": set(),
    "unknown": set(),
}
TRANSITION_PATCH_FIELDS = {
    ("allocated", "starting"): {"runtime_payload_sha256"},
    ("starting", "provisioning"): {"runtime_identity"},
    ("provisioning", "ready"): {"opencode_version"},
    ("provisioning", "failed"): {"runtime_identity"},
    ("suspending", "suspended"): {"memory_gib", "runtime_identity"},
    ("suspended", "starting"): {"workspace", "memory_gib", "max_depth", "guest_payload_sha256", "runtime_identity"},
    ("suspended", "deleting"): {"runtime_identity"},
    ("failed", "starting"): {"runtime_identity"},
    ("failed", "deleting"): {"runtime_identity"},
}


def profile_validate(data, root, name):
    require(isinstance(data, dict) and PROFILE_FIELDS <= data.keys())
    require(data.keys() <= PROFILE_FIELDS | {"runtime_identity"})
    require(type(data["schema_version"]) is int and data["schema_version"] == 1)
    require(str(uuid.UUID(text(data["profile_id"]))) == data["profile_id"])
    require(data["name"] == profile_name(name))
    workspace = canonical(text(data["workspace"]))
    require(not overlaps(workspace, root) and workspace not in (Path("/"), Path.home().resolve()))
    # Metadata must preserve the same credential guards, without imposing repo policy.
    workspace_path(str(workspace), root, str(workspace), recurse=True)
    integer(data["memory_gib"], 1, 128)
    integer(data["depth"], 0, 32)
    integer(data["max_depth"], data["depth"], 32)
    require(type(data["descendants_possible"]) is bool)
    require(data["max_depth"] == data["depth"] or data["descendants_possible"])
    require(isinstance(data["status"], str) and data["status"] in STATUSES)
    pending = data["status"] in {"allocated", "starting", "provisioning", "failed", "unknown", "deleting"}
    for field in ("image_sha256", "guest_payload_sha256", "runtime_payload_sha256"):
        value = data[field]
        require((value is None and pending) or
                (isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None))
    require((data["image_path"] is None) == (data["image_sha256"] is None))
    if data["image_path"] is not None:
        require(canonical(text(data["image_path"])) == root / "vm/images" / data["image_sha256"] / "base.qcow2")
    if data["opencode_version"] is None:
        require(pending)
    else:
        opencode_version(data["opencode_version"])
    if "runtime_identity" in data:
        identity = data["runtime_identity"]
        require(isinstance(identity, dict) and identity.keys() == {
            "pid", "start_ticks", "executable", "disk_path", "socket_path", "ssh_port", "argv"})
        integer(identity["pid"], 1, 2**31 - 1)
        integer(identity["start_ticks"], 1, 2**63 - 1)
        integer(identity["ssh_port"], 1, 65535)
        canonical(text(identity["executable"]))
        require(canonical(text(identity["disk_path"])) == profile_path(root, name) / "disk.qcow2")
        canonical(text(identity["socket_path"]))
        require(isinstance(identity["argv"], list) and bool(identity["argv"]))
        for arg in identity["argv"]:
            text(arg)
    return data


def profile_read(root, name):
    path = profile_path(root, name) / "profile.json"
    require(path.is_file())
    return profile_validate(decode(path.read_bytes()), root, name)


def profile_list(root):
    """Read every exact private profile without creating or repairing state."""
    root = state_root(str(root), must_exist=False, scan=True)
    if not root.exists():
        return []
    profiles = root / "vm/profiles"
    if not profiles.exists() and not profiles.is_symlink():
        return []
    require(profiles.is_dir() and not profiles.is_symlink())
    private_entry(profiles)
    result = []
    for path in sorted(profiles.iterdir(), key=lambda item: item.name):
        require(path.is_dir() and not path.is_symlink())
        profile_name(path.name)
        private_entry(path)
        result.append(profile_read(root, path.name))
    return result


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_bytes(path, payload):
    if path.exists() or path.is_symlink():
        private_entry(path)
    fd, temp = tempfile.mkstemp(prefix=".data-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        fsync_dir(path.parent)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def atomic_json(path, data):
    fd, temp = tempfile.mkstemp(prefix=".metadata-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded(data))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        fsync_dir(path.parent)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def private_mkdir(path):
    try:
        path.mkdir(mode=0o700)
        fsync_dir(path.parent)
    except FileExistsError:
        pass
    private_entry(path)


def ensure_private_root(root):
    root = state_root(str(root), scan=False)
    if not root.exists():
        missing = []
        current = root
        while not current.exists():
            missing.append(current)
            current = current.parent
        require(current.is_dir())
        for directory in reversed(missing):
            private_mkdir(directory)
    return root


def _recursion_run_base(root, *, create):
    root = state_root(str(root), scan=False)
    suffix = hashlib.sha256(os.fsencode(root)).hexdigest()[:24]
    base = root.parent / f".little-mester-recursion-{suffix}"
    if create:
        base = ensure_private_root(base)
    else:
        canonical(str(base), exists=True)
        private_entry(base)
    return root, base


def recursion_run_create(root, run_name):
    """Return one exact private recursive snapshot/run namespace and state root."""
    profile_name(run_name)
    root = ensure_private_root(root)
    state_root(str(root), must_exist=True, scan=True)
    with lock_file(root / ".allocation.lock"):
        state_root(str(root), must_exist=True, scan=True)
        _root, base = _recursion_run_base(root, create=True)
        with lock_file(base / ".runs.lock"):
            run_root = base / run_name
            if run_root.exists() or run_root.is_symlink():
                private_entry(run_root)
            else:
                private_mkdir(run_root)
            state = ensure_private_root(run_root / "state")
    return {"run_root": str(run_root), "state_root": str(state)}


def _nested_recursion_report_roots(run_root):
    """Find private nested-run report namespaces; any retained run blocks cleanup."""
    report_roots = []
    for directory, dirs, files in os.walk(run_root, followlinks=False, onerror=walk_error):
        current = Path(directory)
        require(not any(name.startswith(".little-mester-recursion-") for name in files))
        for name in list(dirs):
            if not name.startswith(".little-mester-recursion-"):
                continue
            require(re.fullmatch(r"\.little-mester-recursion-[a-f0-9]{24}", name) is not None)
            nested_base = current / name
            private_entry(nested_base)
            dirs.remove(name)
            entries = {item.name for item in nested_base.iterdir()}
            require(".runs.lock" in entries and entries <= {".runs.lock", "reports"})
            private_entry(nested_base / ".runs.lock")
            if "reports" in entries:
                report_path = nested_base / "reports"
                private_tree(report_path)
                report_roots.append(report_path)
            run_entries = entries - {".runs.lock", "reports"}
            require(not run_entries)
    return report_roots


def recursion_run_delete(root, run_name, confirmed_name, proc_root):
    """Delete only one confirmed run after proving all exact child QEMU stopped."""
    profile_name(run_name)
    require(confirmed_name == run_name)
    _root, base = _recursion_run_base(root, create=False)
    run_root = base / run_name
    private_entry(run_root)
    state = run_root / "state"
    state_root(str(state), must_exist=True, scan=True)

    with lock_file(state / ".allocation.lock"), lock_file(base / ".runs.lock"):
        state_root(str(state), must_exist=True, scan=True)
        profiles = profile_list(state)
        require(all(item["status"] in {"allocated", "failed", "suspended"} for item in profiles))

        child_script = Path(__file__).resolve().parents[2] / "scripts/vm-recursion-live-children.py"
        spec = importlib.util.spec_from_file_location("vm_recursion_cleanup_children", child_script)
        require(spec is not None and spec.loader is not None)
        children = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(children)
        live_profiles, unknown_pids = children.live_nested_qemus(state, proc_root=Path(proc_root))
        require(not live_profiles and not unknown_pids)

        report_path = state / "vm/reports"
        report_roots = _nested_recursion_report_roots(run_root)
        if report_path.exists() or report_path.is_symlink():
            private_tree(report_path)
            report_roots.insert(0, report_path)
        archived = []
        if report_roots:
            archive_root = base / "reports"
            if not archive_root.exists():
                private_mkdir(archive_root)
            else:
                private_entry(archive_root)
            for index, source_report in enumerate(report_roots):
                destination = archive_root / f"{run_name}-{uuid.uuid4().hex}-{index}"
                rename_noreplace(source_report, destination)
                archived.append(str(destination))
                fsync_dir(archive_root)
                fsync_dir(source_report.parent)

        # Recheck the exact private run directory immediately before removing
        # only this caller-confirmed child namespace.
        private_entry(run_root)
        shutil.rmtree(run_root)
        fsync_dir(base)
    return {
        "deleted_run": run_name,
        "reports_path": archived[0] if archived else None,
        "archived_reports": archived,
    }


@contextmanager
def lock_file(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        private_entry(path)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        os.close(fd)


def _inherited_lock_fd(path, variable):
    raw = os.environ.get(variable)
    require(raw is not None and re.fullmatch(r"[0-9]+", raw) is not None)
    fd = int(raw)
    info = os.fstat(fd)
    target = os.stat(path, follow_symlinks=False)
    require(stat.S_ISREG(info.st_mode) and info.st_dev == target.st_dev and info.st_ino == target.st_ino)
    private_entry(Path(path))
    probe = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            require(error.errno in (errno.EAGAIN, errno.EACCES))
        else:
            fcntl.flock(probe, fcntl.LOCK_UN)
            require(False)
    finally:
        os.close(probe)
    return fd


def _inherited_profile_locks(root, name, path):
    allocation_root = os.environ.get("VM_HELD_ALLOCATION_ROOT")
    allocation_fd_value = os.environ.get("VM_HELD_ALLOCATION_FD")
    profile_variables = ("VM_HELD_PROFILE_ROOT", "VM_HELD_PROFILE_NAME", "VM_HELD_PROFILE_PATH", "VM_HELD_PROFILE_FD")
    profile_present = [variable in os.environ for variable in profile_variables]
    if allocation_root is None and allocation_fd_value is None and not any(profile_present):
        return None
    require(allocation_root == str(root) and allocation_fd_value is not None)
    allocation_fd = _inherited_lock_fd(root / ".allocation.lock", "VM_HELD_ALLOCATION_FD")
    if not any(profile_present):
        return allocation_fd, None
    require(all(profile_present))
    inherited_name = profile_name(os.environ["VM_HELD_PROFILE_NAME"])
    inherited_path = profile_path(root, inherited_name)
    require(os.environ["VM_HELD_PROFILE_ROOT"] == str(root))
    require(os.environ["VM_HELD_PROFILE_PATH"] == str(inherited_path))
    profile_fd = _inherited_lock_fd(inherited_path / ".profile.lock", "VM_HELD_PROFILE_FD")
    if inherited_name != name:
        return allocation_fd, None
    require(inherited_path == path)
    return allocation_fd, profile_fd


@contextmanager
def profile_lock_details(root, name):
    """Lock root then exact profile; nested managed workers reuse inherited fds."""
    profile_name(name)
    root = ensure_private_root(root)
    path = profile_path(root, name)
    inherited = _inherited_profile_locks(root, name, path)
    if inherited is not None:
        allocation_fd, profile_fd = inherited
        state_root(str(root), must_exist=True)
        private_entry(path)
        require((path / "profile.json").exists())
        if profile_fd is not None:
            yield path, allocation_fd, profile_fd
        else:
            with lock_file(path / ".profile.lock") as acquired_profile_fd:
                yield path, allocation_fd, acquired_profile_fd
        return
    with lock_file(root / ".allocation.lock") as allocation_fd:
        state_root(str(root), must_exist=True)
        for directory in (root / "vm", root / "vm/profiles", path):
            private_mkdir(directory)
        with lock_file(path / ".profile.lock") as profile_fd:
            if not (path / "profile.json").exists():
                require(all(p.name in {".profile.lock", "runtime"} for p in path.iterdir()))
            yield path, allocation_fd, profile_fd


@contextmanager
def profile_lock(root, name):
    """Mutating API: validate/create private state; lock allocation then profile."""
    with profile_lock_details(root, name) as (path, _allocation_fd, _profile_fd):
        yield path


@contextmanager
def image_cache_lock(root, digest):
    require(isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest) is not None)
    root = ensure_private_root(root)
    lock_path = root / "vm/.locks" / f"{digest}.lock"
    with lock_file(root / ".allocation.lock"):
        state_root(str(root), must_exist=True)
        vm = root / "vm"
        private_mkdir(vm)
        private_mkdir(vm / ".locks")
        private_mkdir(vm / "images")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            private_entry(lock_path)
        except BaseException:
            os.close(fd)
            raise
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield root / "vm/images"
    finally:
        os.close(fd)


def private_tree(path):
    require(path.is_dir() and not path.is_symlink())
    private_entry(path)
    for directory, dirs, files in os.walk(path, followlinks=False, onerror=walk_error):
        current = Path(directory)
        private_entry(current)
        for name in dirs:
            child = current / name
            require(not child.is_symlink())
            private_entry(child)
        for name in files:
            child = current / name
            private_entry(child)
            with child.open("rb") as stream:
                os.fsync(stream.fileno())
        fsync_dir(current)


def hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def rename_noreplace(source, destination):
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    require(renameat2 is not None)
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def image_publish(root, digest, staging):
    require(isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest) is not None)
    root = state_root(str(root), must_exist=True)
    images = root / "vm/images"
    private_entry(images)
    stage = canonical(staging, exists=True)
    require(stage.parent == images and stage.name.startswith(f".staging.{digest}."))
    target = images / digest
    require(not target.exists() and not target.is_symlink())
    private_tree(stage)
    names = {item.name for item in stage.iterdir()}
    require(names == {"base.qcow2", "manifest.json"})
    manifest_path = stage / "manifest.json"
    require(manifest_path.stat().st_size <= 4096)
    manifest = decode(manifest_path.read_bytes())
    require(isinstance(manifest, dict) and manifest.keys() == {
        "schema_version", "source_url", "source_sha256", "artifact_sha256", "virtual_size_bytes", "format"})
    require(type(manifest["schema_version"]) is int and manifest["schema_version"] == 1
            and manifest["format"] == "qcow2")
    require(manifest["source_sha256"] == digest)
    url = text(manifest["source_url"])
    parsed_url = urlsplit(url)
    require(parsed_url.scheme == "https" and bool(parsed_url.hostname)
            and parsed_url.username is None and parsed_url.password is None
            and parsed_url.port in (None, 443) and not parsed_url.query and not parsed_url.fragment)
    require(isinstance(manifest["artifact_sha256"], str) and
            re.fullmatch(r"[a-f0-9]{64}", manifest["artifact_sha256"]) is not None)
    integer(manifest["virtual_size_bytes"], 1024**3, 1024**4)
    artifact = stage / "base.qcow2"
    require(artifact.stat().st_size > 0)
    digest_file = hash_file(artifact)
    require(digest_file == manifest["artifact_sha256"])
    rename_noreplace(stage, target)
    fsync_dir(images)
    return target


def profile_overlay_publish(root, name, staging):
    root = state_root(str(root), must_exist=True)
    data = profile_read(root, name)
    profile = profile_path(root, name)
    stage = canonical(staging, exists=True)
    require(stage.parent.parent == profile and stage.parent.name.startswith(".overlay."))
    require(stage.name == "disk.qcow2")
    private_entry(stage)
    require(stage.stat().st_size > 0)
    target = profile / "disk.qcow2"
    require(not target.exists() and not target.is_symlink())
    require(data["image_path"] is not None)
    with stage.open("rb") as stream:
        os.fsync(stream.fileno())
    rename_noreplace(stage, target)
    fsync_dir(profile)
    return target


def profile_asset_publish(root, name, asset, staging):
    """Publish one create-only private key or seed ISO in the exact profile."""
    require(asset in {"control-key", "seed.iso"})
    root = state_root(str(root), must_exist=True)
    data = profile_read(root, name)
    require(data["status"] == "starting")
    profile = profile_path(root, name)
    stage = canonical(staging, exists=True)
    stage_dir = stage.parent
    require(stage_dir.parent == profile and re.fullmatch(r"\.asset\.[A-Za-z0-9_-]{1,64}", stage_dir.name))
    private_entry(stage_dir)
    private_entry(stage)
    require(stage.is_file() and not stage.is_symlink() and 0 < stage.stat().st_size <= 64 * 1024 * 1024)
    payload = stage.read_bytes()
    if asset == "control-key":
        require(payload.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----\n")
                and payload.rstrip().endswith(b"-----END OPENSSH PRIVATE KEY-----"))
    else:
        offset = 16 * 2048
        require(len(payload) >= offset + 2048 and payload[offset] == 1
                and payload[offset + 1:offset + 6] == b"CD001" and payload[offset + 6] == 1)
    target = profile / asset
    require(not target.exists() and not target.is_symlink())
    with profile_lock(root, name):
        current = profile_read(root, name)
        require(current["status"] == "starting")
        require(not target.exists() and not target.is_symlink())
        rename_noreplace(stage, target)
        fsync_dir(profile)
    return target


def profile_asset_replace(root, name, asset, staging, user_data_staging):
    """Replace a verified seed ISO and its user-data copy during a locked restart."""
    require(asset == "seed.iso")
    root = state_root(str(root), must_exist=True)
    data = profile_read(root, name)
    require(data["status"] == "starting")
    profile = profile_path(root, name)
    stage = canonical(staging, exists=True)
    stage_dir = stage.parent
    require(stage_dir.parent == profile and re.fullmatch(r"\.asset\.[A-Za-z0-9_-]{1,64}", stage_dir.name))
    private_entry(stage_dir)
    private_entry(stage)
    require(stage.is_file() and not stage.is_symlink() and 0 < stage.stat().st_size <= 64 * 1024 * 1024)
    offset = 16 * 2048
    staged = stage.read_bytes()
    require(len(staged) >= offset + 2048 and staged[offset] == 1
            and staged[offset + 1:offset + 6] == b"CD001" and staged[offset + 6] == 1)
    target = profile / asset
    private_entry(target)
    old = target.read_bytes()
    require(len(old) >= offset + 2048 and old[offset] == 1
            and old[offset + 1:offset + 6] == b"CD001" and old[offset + 6] == 1)
    user_data_stage = canonical(user_data_staging, exists=True)
    require(user_data_stage.parent == stage_dir and user_data_stage.name == "user-data")
    private_entry(user_data_stage)
    require(user_data_stage.is_file() and not user_data_stage.is_symlink()
            and user_data_stage.stat().st_size <= 1024 * 1024)
    new_user_data = user_data_stage.read_bytes()
    seed_validate(new_user_data)
    user_data_target = profile / "seed.user-data"
    private_entry(user_data_target)
    require(user_data_target.stat().st_size <= 1024 * 1024)
    old_user_data = user_data_target.read_bytes()
    for staged_file in (stage, user_data_stage):
        with staged_file.open("rb") as stream:
            os.fsync(stream.fileno())
    with profile_lock(root, name):
        current = profile_read(root, name)
        require(current["status"] == "starting")
        private_entry(target)
        require(target.read_bytes() == old)
        private_entry(user_data_target)
        require(user_data_target.read_bytes() == old_user_data)
        replaced_user_data = False
        replaced_iso = False
        try:
            os.replace(user_data_stage, user_data_target)
            replaced_user_data = True
            os.replace(stage, target)
            replaced_iso = True
            fsync_dir(profile)
        except BaseException:
            # A normal publication error rolls back the duplicate user-data copy.
            # A process crash leaves status=starting, which blocks lifecycle use.
            rollback_errors = []
            if replaced_iso:
                try:
                    atomic_bytes(target, old)
                except (OSError, RuntimeError) as rollback_error:
                    rollback_errors.append(rollback_error)
            if replaced_user_data:
                try:
                    atomic_bytes(user_data_target, old_user_data)
                except (OSError, RuntimeError) as rollback_error:
                    rollback_errors.append(rollback_error)
            if rollback_errors:
                raise RuntimeError("seed asset replacement rollback failed") from rollback_errors[0]
            raise
    return target


def seed_write(root, name, raw):
    seed_validate(raw)
    profile_read(root, name)
    root = state_root(str(root), must_exist=True)
    with profile_lock(root, name) as profile:
        profile_read(root, name)
        target = profile / "seed.user-data"
        atomic_bytes(target, raw)
    return target


def seed_validate(raw):
    require(isinstance(raw, bytes) and len(raw) <= 1024 * 1024)
    require(raw.startswith(b"#cloud-config\n") or raw == b"#cloud-config")
    require(b"\x00" not in raw and all(byte in (9, 10, 13) or byte >= 32 for byte in raw))
    sensitive = re.compile(
        rb"(?i)(?:password|passwd|token|api[_-]?key|access[_-]?token|refresh[_-]?token|"
        rb"client[_-]?secret|private[_-]?key|credential)"
    )
    require(sensitive.search(raw) is None)
    return raw


def profile_run(root, name, command):
    require(command)
    if command and command[0] == "--":
        command = command[1:]
    require(command)
    root = state_root(str(root), must_exist=True)
    profile_read(root, name)
    with profile_lock_details(root, name) as (profile, allocation_fd, profile_fd):
        data = profile_read(root, name)
        env = os.environ.copy()
        env["VM_PROFILE_PATH"] = str(profile)
        env["VM_PROFILE_IMAGE_PATH"] = data["image_path"] or ""
        env["VM_PROFILE_IMAGE_SHA256"] = data["image_sha256"] or ""
        env["VM_HELD_ALLOCATION_ROOT"] = str(root)
        env["VM_HELD_PROFILE_ROOT"] = str(root)
        env["VM_HELD_PROFILE_NAME"] = name
        env["VM_HELD_PROFILE_PATH"] = str(profile)
        env["VM_HELD_ALLOCATION_FD"] = str(allocation_fd)
        env["VM_HELD_PROFILE_FD"] = str(profile_fd)
        return subprocess.run(command, env=env, pass_fds=(allocation_fd, profile_fd), check=False).returncode


def image_run(root, digest, command):
    require(command)
    if command and command[0] == "--":
        command = command[1:]
    require(command)
    with image_cache_lock(root, digest) as images:
        env = os.environ.copy()
        env["VM_IMAGE_IMAGES_DIR"] = str(images)
        env["VM_IMAGE_CACHE_DIR"] = str(images / digest)
        env["VM_IMAGE_STATE_ROOT"] = str(root)
        env["VM_IMAGE_SHA256"] = digest
        return subprocess.run(command, env=env, check=False).returncode


def profile_write(root, name, data):
    profile_validate(data, root, name)
    with profile_lock(root, name) as path:
        if (path / "profile.json").exists():
            old = profile_read(root, name)
            require(all(data[key] == old[key] for key in ("profile_id", "name", "depth")))
            require(data["max_depth"] <= old["max_depth"])
            require(not old["descendants_possible"] or data["descendants_possible"])
            require(data["status"] == old["status"])
            for key in PROFILE_FIELDS - {"max_depth", "descendants_possible"}:
                require(data[key] == old[key])
        runtime = path / "runtime/manifest.json"
        if runtime.exists():
            require(data["runtime_payload_sha256"] == hashlib.sha256(runtime.read_bytes()).hexdigest())
        atomic_json(path / "profile.json", data)
    return data


def profile_version_update(root, name, expected, candidate):
    """Commit one exact guest OpenCode version after a verified in-place update."""
    opencode_version(expected)
    opencode_version(candidate)
    require(expected != candidate)
    root = state_root(str(root), must_exist=True)
    with profile_lock(root, name) as path:
        data = profile_read(root, name)
        require(data["status"] == "ready" and data["opencode_version"] == expected)
        data["opencode_version"] = candidate
        profile_validate(data, root, name)
        atomic_json(path / "profile.json", data)
    return data


def profile_create(root, name, data):
    """Create one new allocated profile; never treat an existing profile as an update."""
    profile_validate(data, root, name)
    require(data["status"] == "allocated")
    root = state_root(str(root), must_exist=True)
    with profile_lock(root, name) as path:
        target = path / "profile.json"
        require(not target.exists() and not target.is_symlink())
        require({entry.name for entry in path.iterdir()} == {".profile.lock"})
        atomic_json(target, data)
    return data


def profile_transition(root, name, expected_status, target_status, patch=None):
    """Atomically apply one allowed state transition under root/profile locks."""
    require(expected_status in STATUSES and target_status in STATUSES)
    patch = {} if patch is None else patch
    require(isinstance(patch, dict)
            and patch.keys() <= TRANSITION_PATCH_FIELDS.get((expected_status, target_status), set()))
    root = state_root(str(root), must_exist=True)
    with profile_lock(root, name) as path:
        require((path / "profile.json").is_file())
        old = profile_read(root, name)
        require(old["status"] == expected_status)
        require(target_status in PROFILE_TRANSITIONS[expected_status])
        updated = dict(old)
        for key, value in patch.items():
            if key == "runtime_identity" and value is None:
                updated.pop(key, None)
            else:
                updated[key] = value
        updated["status"] = target_status
        profile_validate(updated, root, name)
        require(updated["profile_id"] == old["profile_id"])
        require(updated["depth"] == old["depth"])
        require(updated["max_depth"] <= old["max_depth"])
        require(not old["descendants_possible"] or updated["descendants_possible"])
        runtime = path / "runtime/manifest.json"
        if runtime.exists():
            require(updated["runtime_payload_sha256"] == hashlib.sha256(runtime.read_bytes()).hexdigest())
        atomic_json(path / "profile.json", updated)
    return updated


def _profile_runs_stopped(profile):
    runs = profile / "runs"
    if not runs.exists() and not runs.is_symlink():
        return
    require(runs.is_dir() and not runs.is_symlink())
    private_entry(runs)
    process_path = Path(__file__).resolve().with_name("process.py")
    import importlib.util
    spec = importlib.util.spec_from_file_location("vm_delete_process", process_path)
    require(spec is not None and spec.loader is not None)
    process = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(process)
    for run in sorted(runs.iterdir(), key=lambda item: item.name):
        require(run.is_dir() and not run.is_symlink())
        private_entry(run)
        registry = run / "processes.json"
        private_entry(registry)
        status = process.registry_status(registry, run.name)
        require(status["state"] == "stopped")
    private_tree(runs)


def profile_delete(root, name):
    """Remove only a deleting exact profile after preserving its private run reports."""
    root = state_root(str(root), must_exist=True)
    with profile_lock(root, name) as profile:
        data = profile_read(root, name)
        require(data["status"] == "deleting" and "runtime_identity" not in data)
        _profile_runs_stopped(profile)
        runtime = profile / "runtime"
        if runtime.exists() or runtime.is_symlink():
            runtime_validate(root, name)
        allowed = {".profile.lock", "profile.json", "runtime", "disk.qcow2", "seed.iso",
                   "seed.user-data", "control-key", "runs"}
        require({item.name for item in profile.iterdir()} <= allowed)
        for item in profile.iterdir():
            if item.name in {"runtime", "runs"}:
                continue
            private_entry(item)
            if item.name not in {".profile.lock", "profile.json"}:
                require(item.is_file())

        reports = root / "vm/reports"
        report_root = reports / data["profile_id"]
        archived_runs = report_root / "runs"
        runs = profile / "runs"
        if reports.exists() or reports.is_symlink():
            require(reports.is_dir() and not reports.is_symlink())
            private_entry(reports)
        elif runs.exists():
            private_mkdir(reports)
        if report_root.exists() or report_root.is_symlink():
            require(report_root.is_dir() and not report_root.is_symlink())
            private_entry(report_root)
            require({item.name for item in report_root.iterdir()} <= {"runs"})
            if archived_runs.exists() or archived_runs.is_symlink():
                require(archived_runs.is_dir() and not archived_runs.is_symlink())
                _profile_runs_stopped(report_root)
                private_tree(archived_runs)
        elif runs.exists():
            private_mkdir(report_root)
        if runs.exists():
            require(runs.is_dir() and not runs.is_symlink())
            require(not archived_runs.exists() and not archived_runs.is_symlink())
            rename_noreplace(runs, archived_runs)
            fsync_dir(profile)
            fsync_dir(report_root)

        # Recheck exact directory membership after moving reports, then unlink only
        # this validated profile path while allocation/profile locks remain held.
        require({item.name for item in profile.iterdir()} <= allowed - {"runs"})
        shutil.rmtree(profile)
        fsync_dir(profile.parent)
        if reports.exists():
            fsync_dir(reports)
        return {"deleted": name, "profile_id": data["profile_id"],
                "reports_path": str(report_root) if report_root.exists() else None}


def file_capture(source, rel):
    text(rel)
    relative = Path(rel)
    require(not relative.is_absolute() and str(relative) == rel and ".." not in relative.parts)
    require(rel != "manifest.json" and relative.parts[0] != "manifest.json")
    path = canonical(str(source / relative), exists=True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid())
        require(info.st_size <= 64 * 1024 * 1024)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            payload = stream.read(64 * 1024 * 1024 + 1)
        after = os.fstat(fd)
        require(len(payload) == info.st_size and info.st_mtime_ns == after.st_mtime_ns
                and info.st_ctime_ns == after.st_ctime_ns)
        mode = 0o700 if info.st_mode & 0o111 else 0o600
        entry = {"path": rel, "mode": stat.S_IMODE(info.st_mode), "installed_mode": mode,
                 "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        return payload, entry
    finally:
        os.close(fd)


def runtime_install(root, name, source_value, files):
    source = canonical(source_value, exists=True)
    require(source.is_dir() and not overlaps(source, root))
    require(files and len(files) == len(set(files)) and len(files) <= 4096)
    captured = []
    total = 0
    for rel in sorted(files):
        payload, entry = file_capture(source, rel)
        total += len(payload)
        require(total <= 256 * 1024 * 1024)
        captured.append((rel, payload, entry))
    manifest = {"schema_version": 1, "files": [item[2] for item in captured]}
    digest = hashlib.sha256(encoded(manifest)).hexdigest()
    with profile_lock(root, name) as path:
        target = path / "runtime"
        if (path / "profile.json").exists():
            old = profile_read(root, name)
            require(old["runtime_payload_sha256"] in (None, digest))
        if target.exists():
            require((target / "manifest.json").read_bytes() == encoded(manifest))
            expected = {"manifest.json"} | set(files)
            actual = {str(p.relative_to(target)) for p in target.rglob("*") if not p.is_dir()}
            require(actual == expected)
            for rel, payload, entry in captured:
                installed = target / rel
                require(installed.read_bytes() == payload and
                        stat.S_IMODE(installed.stat().st_mode) == entry["installed_mode"])
        else:
            staging = Path(tempfile.mkdtemp(prefix=".runtime-", dir=path))
            try:
                for rel, payload, entry in captured:
                    destination = staging / rel
                    current = staging
                    for component in destination.parent.relative_to(staging).parts:
                        current = current / component
                        private_mkdir(current)
                    with destination.open("xb") as stream:
                        os.fchmod(stream.fileno(), entry["installed_mode"])
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                atomic_json(staging / "manifest.json", manifest)
                for directory, _, _ in os.walk(staging, topdown=False):
                    fsync_dir(Path(directory))
                os.rename(staging, target)
                fsync_dir(path)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
    return {"schema_version": 1, "runtime_path": str(target), "runtime_payload_sha256": digest}


def runtime_validate(root, name):
    """Validate an installed immutable runtime without sourcing or mutating it."""
    root = state_root(str(root), must_exist=True)
    data = profile_read(root, name)
    runtime = profile_path(root, name) / "runtime"
    require(data["runtime_payload_sha256"] is not None)
    require(runtime.is_dir() and not runtime.is_symlink())
    private_entry(runtime)
    manifest_path = runtime / "manifest.json"
    private_entry(manifest_path)
    require(manifest_path.stat().st_size <= 1024 * 1024)
    manifest = decode(manifest_path.read_bytes())
    require(isinstance(manifest, dict) and manifest.keys() == {"schema_version", "files"})
    require(type(manifest["schema_version"]) is int and manifest["schema_version"] == 1)
    require(isinstance(manifest["files"], list) and 1 <= len(manifest["files"]) <= 4096)
    require(hashlib.sha256(encoded(manifest)).hexdigest() == data["runtime_payload_sha256"])
    expected = {"manifest.json"}
    total = 0
    for item in manifest["files"]:
        require(isinstance(item, dict) and item.keys() == {
            "path", "mode", "installed_mode", "size", "sha256"})
        relative_text = text(item["path"])
        relative = Path(relative_text)
        require(not relative.is_absolute() and str(relative) == relative_text
                and ".." not in relative.parts and relative_text != "manifest.json")
        require(type(item["mode"]) is int and 0 <= item["mode"] <= 0o7777)
        require(item["installed_mode"] in (0o600, 0o700))
        integer(item["size"], 0, 64 * 1024 * 1024)
        require(isinstance(item["sha256"], str)
                and re.fullmatch(r"[a-f0-9]{64}", item["sha256"]) is not None)
        require(relative_text not in expected)
        expected.add(relative_text)
        target = canonical(str(runtime / relative), exists=True)
        require(target.is_file())
        info = target.lstat()
        require(info.st_uid == os.getuid() and stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == item["installed_mode"])
        require(info.st_size == item["size"] and hash_file(target) == item["sha256"])
        total += info.st_size
        require(total <= 256 * 1024 * 1024)
    for directory, directories, files in os.walk(runtime, followlinks=False, onerror=walk_error):
        current = Path(directory)
        private_entry(current)
        for name_part in directories:
            child = current / name_part
            require(not child.is_symlink())
            private_entry(child)
        for name_part in files:
            child = current / name_part
            require(not child.is_symlink())
            require(str(child.relative_to(runtime)) in expected)
    actual = {str(path.relative_to(runtime)) for path in runtime.rglob("*") if path.is_file()}
    require(actual == expected)
    return {"runtime_path": str(runtime), "runtime_payload_sha256": data["runtime_payload_sha256"]}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise PolicyError("Invalid VM policy command")


def main():
    parser = Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    for command in ("config", "validate-config", "context", "validate-workspace",
                    "profile-read", "profile-list", "profile-write", "profile-create", "profile-transition", "profile-version-update", "runtime-install", "runtime-validate", "image-run",
                    "image-publish", "profile-run", "profile-delete", "overlay-publish", "asset-publish", "asset-replace", "seed-write",
                    "recursion-run-create", "recursion-run-delete"):
        sub = commands.add_parser(command)
        sub.add_argument("--state-root", required=True)
        if command in ("context", "profile-read", "profile-write", "profile-create", "profile-transition", "profile-version-update", "runtime-install", "runtime-validate",
                       "profile-run", "profile-delete", "overlay-publish", "asset-publish", "asset-replace", "seed-write"):
            sub.add_argument("--profile", required=True)
        if command == "profile-transition":
            sub.add_argument("--expected-status", required=True, choices=sorted(STATUSES))
            sub.add_argument("--status", required=True, choices=sorted(STATUSES))
        if command == "profile-version-update":
            sub.add_argument("--expected-version", required=True)
            sub.add_argument("--version", required=True)
        if command in ("context", "validate-workspace"):
            sub.add_argument("--repository", required=True)
            sub.add_argument("--workspace", required=command == "validate-workspace")
            sub.add_argument("--recurse", action="store_true")
            sub.add_argument("--unsafe-recurse", action="store_true")
        if command == "runtime-install":
            sub.add_argument("--source", required=True)
            sub.add_argument("--files", nargs="+", required=True)
        if command in ("image-run", "image-publish"):
            sub.add_argument("--sha256", required=True)
        if command == "image-run" or command == "profile-run":
            sub.add_argument("command_argv", nargs=argparse.REMAINDER)
        if command in ("image-publish", "overlay-publish", "asset-publish", "asset-replace"):
            sub.add_argument("--staging", required=True)
        if command == "asset-replace":
            sub.add_argument("--user-data-staging", required=True)
        if command in ("asset-publish", "asset-replace"):
            sub.add_argument("--asset", required=True,
                             choices=("seed.iso",) if command == "asset-replace" else ("control-key", "seed.iso"))
        if command in ("recursion-run-create", "recursion-run-delete"):
            sub.add_argument("--run-name", required=True)
        if command == "recursion-run-delete":
            sub.add_argument("--confirm-run", required=True)
            sub.add_argument("--proc-root", type=Path, default=Path("/proc"))
    args = parser.parse_args()
    root = state_root(args.state_root, scan=args.command not in ("profile-write", "runtime-install"))
    if hasattr(args, "profile"):
        path = profile_path(root, args.profile)
    if args.command == "config":
        result = config_read(root)
    elif args.command == "validate-config":
        result = config_validate(root, decode(sys.stdin.buffer.read(1024 * 1024 + 1)))
    elif args.command in ("context", "validate-workspace"):
        result = {"schema_version": 1, "state_root": str(root)}
        if args.command == "context":
            if path.exists():
                profile_read(root, args.profile)
            result.update(profile_root=str(root / "vm/profiles"), profile_path=str(path), profile_name=args.profile)
        if args.workspace:
            result["workspace"] = str(workspace_path(args.workspace, root, args.repository,
                                                      args.recurse, args.unsafe_recurse))
    elif args.command == "profile-read":
        result = profile_read(root, args.profile)
    elif args.command == "profile-list":
        result = profile_list(root)
    elif args.command == "profile-write":
        result = profile_write(root, args.profile, decode(sys.stdin.buffer.read(1024 * 1024 + 1)))
    elif args.command == "profile-create":
        result = profile_create(root, args.profile, decode(sys.stdin.buffer.read(1024 * 1024 + 1)))
    elif args.command == "profile-transition":
        result = profile_transition(
            root, args.profile, args.expected_status, args.status,
            decode(sys.stdin.buffer.read(1024 * 1024 + 1)),
        )
    elif args.command == "profile-version-update":
        result = profile_version_update(root, args.profile, args.expected_version, args.version)
    elif args.command == "runtime-validate":
        result = runtime_validate(root, args.profile)
    elif args.command == "image-run":
        return image_run(root, args.sha256, args.command_argv)
    elif args.command == "image-publish":
        result = {"published": str(image_publish(root, args.sha256, args.staging))}
    elif args.command == "profile-run":
        return profile_run(root, args.profile, args.command_argv)
    elif args.command == "profile-delete":
        result = profile_delete(root, args.profile)
    elif args.command == "overlay-publish":
        result = {"published": str(profile_overlay_publish(root, args.profile, args.staging))}
    elif args.command == "asset-publish":
        result = {"published": str(profile_asset_publish(root, args.profile, args.asset, args.staging))}
    elif args.command == "asset-replace":
        result = {"published": str(profile_asset_replace(
            root, args.profile, args.asset, args.staging, args.user_data_staging))}
    elif args.command == "seed-write":
        result = {"seed_path": str(seed_write(root, args.profile, sys.stdin.buffer.read(1024 * 1024 + 1)))}
    elif args.command == "recursion-run-create":
        result = recursion_run_create(root, args.run_name)
    elif args.command == "recursion-run-delete":
        result = recursion_run_delete(root, args.run_name, args.confirm_run, args.proc_root)
    else:
        result = runtime_install(root, args.profile, args.source, args.files)
    sys.stdout.buffer.write(encoded(result))
    return 0


if __name__ == "__main__":
    try:
        status = main()
        if status is not None:
            sys.exit(status)
    except (ValueError, TypeError, KeyError, OSError, RuntimeError):
        print("VM policy validation failed; check command, schema and private paths", file=sys.stderr)
        sys.exit(2)
