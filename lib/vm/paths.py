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
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path


class PolicyError(ValueError):
    """Untrusted input or unsafe filesystem state; never include input values."""


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


def walk_error(error):
    raise error


def state_root(value, *, must_exist=False, scan=True):
    root = canonical(value, exists=must_exist)
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
    if root.exists():
        require(root.is_dir())
        private_entry(root)
        if not scan:
            return root
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
    return root


def profile_name(name):
    require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name) is not None)
    return name


def profile_path(root, name):
    name = profile_name(name)
    vm = root / "vm"
    namespaces = {"profiles", "images", "services", "runs", "logs"}
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
    version(result["opencode_version"], latest=True)
    require(result["metrics"] in ("on", "off"))
    model = result["model"]
    require(isinstance(model, dict) and model.keys() <= {"provider", "port", "id"})
    require(model.get("provider") in ("none", "ollama", "llamacpp", "remote"))
    if model["provider"] in ("ollama", "llamacpp"):
        integer(model.get("port"), 1, 65535)
    elif "port" in model:
        raise PolicyError("VM policy validation failed")
    if "id" in model:
        text(model["id"])
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


PROFILE_FIELDS = {
    "schema_version", "profile_id", "name", "workspace", "image_sha256", "image_path", "memory_gib",
    "depth", "max_depth", "descendants_possible", "opencode_version", "guest_payload_sha256",
    "runtime_payload_sha256", "status",
}
STATUSES = {"allocated", "starting", "provisioning", "ready", "suspending", "suspended", "failed", "deleting", "unknown"}


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
        version(data["opencode_version"])
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


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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


@contextmanager
def lock_file(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        private_entry(path)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextmanager
def profile_lock(root, name):
    """Mutating API: validate/create private state; lock allocation then profile."""
    profile_name(name)
    root = state_root(str(root), scan=False)
    if not root.exists():
        # Create only missing ancestors after validating the entire existing
        # ancestry. Existing HOME/XDG parents are never chmodded or replaced.
        missing = []
        current = root
        while not current.exists():
            missing.append(current)
            current = current.parent
        require(current.is_dir())
        for directory in reversed(missing):
            private_mkdir(directory)
    with lock_file(root / ".allocation.lock"):
        state_root(str(root), must_exist=True)
        path = profile_path(root, name)
        for directory in (root / "vm", root / "vm/profiles", path):
            private_mkdir(directory)
        with lock_file(path / ".profile.lock"):
            if not (path / "profile.json").exists():
                require(all(p.name in {".profile.lock", "runtime"} for p in path.iterdir()))
            yield path


def profile_write(root, name, data):
    profile_validate(data, root, name)
    with profile_lock(root, name) as path:
        if (path / "profile.json").exists():
            old = profile_read(root, name)
            require(all(data[key] == old[key] for key in ("profile_id", "name", "depth")))
            require(data["max_depth"] <= old["max_depth"])
            require(not old["descendants_possible"] or data["descendants_possible"])
        runtime = path / "runtime/manifest.json"
        if runtime.exists():
            require(data["runtime_payload_sha256"] == hashlib.sha256(runtime.read_bytes()).hexdigest())
        atomic_json(path / "profile.json", data)
    return data


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


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise PolicyError("Invalid VM policy command")


def main():
    parser = Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    for command in ("config", "validate-config", "context", "validate-workspace",
                    "profile-read", "profile-write", "runtime-install"):
        sub = commands.add_parser(command)
        sub.add_argument("--state-root", required=True)
        if command in ("context", "profile-read", "profile-write", "runtime-install"):
            sub.add_argument("--profile", required=True)
        if command in ("context", "validate-workspace"):
            sub.add_argument("--repository", required=True)
            sub.add_argument("--workspace", required=command == "validate-workspace")
            sub.add_argument("--recurse", action="store_true")
            sub.add_argument("--unsafe-recurse", action="store_true")
        if command == "runtime-install":
            sub.add_argument("--source", required=True)
            sub.add_argument("--files", nargs="+", required=True)
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
    elif args.command == "profile-write":
        result = profile_write(root, args.profile, decode(sys.stdin.buffer.read(1024 * 1024 + 1)))
    else:
        result = runtime_install(root, args.profile, args.source, args.files)
    sys.stdout.buffer.write(encoded(result))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, TypeError, KeyError, OSError, RuntimeError):
        print("VM policy validation failed; check command, schema and private paths", file=sys.stderr)
        sys.exit(2)
