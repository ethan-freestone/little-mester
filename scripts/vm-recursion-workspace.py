#!/usr/bin/env python3
"""Create/reuse a private workspace snapshot for a nested VM profile."""

from __future__ import annotations

import argparse
import fcntl
import fnmatch
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NoReturn

# Child snapshots contain selected source, not parent control state, credentials,
# model weights, or regenerated dependency/cache directories.
EXCLUDED_NAMES = {
    ".aws", ".bench-models", ".cache", ".config", ".git", ".gnupg",
    ".kube", ".little-mester.conf", ".local", ".mester", ".pytest_cache",
    ".ruff_cache", ".secrets", ".ssh", ".tox", ".venv", "__pycache__",
    "coverage", "credentials", "node_modules", "secrets",
}
EXCLUDED_FILE_NAMES = {".env", "auth.json", "credentials.json", "opencode.db", "secrets.env"}
EXCLUDED_GLOBS = {
    "*.db", "*.db-*", "*.key", "*.pem", "*.p12", "*.pfx", "*.secret",
    "*.sqlite", "*.sqlite3", "*.token", ".env.*", ".aider*cache*",
    ".*cache*", "id_ed25519*", "id_rsa*",
}
SAFE_ENV_EXAMPLES = {".env.example", ".env.sample", ".env.template"}
DEFAULT_MAX_SNAPSHOT_BYTES = 2 * 1024**3
MAX_CONFIGURED_SNAPSHOT_BYTES = 16 * 1024**3
STAGING_RESERVE_BYTES = 128 * 1024**2


def fail(message: str) -> NoReturn:
    print(f"REFUSED: {message}", file=sys.stderr)
    raise SystemExit(1)


def inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def excluded(name: str) -> bool:
    if name in EXCLUDED_NAMES:
        return True
    if name in SAFE_ENV_EXAMPLES:
        return False
    return name in EXCLUDED_FILE_NAMES or any(
        fnmatch.fnmatchcase(name, pattern) for pattern in EXCLUDED_GLOBS
    )


def git_commit(path: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def source_manifest(source: Path, max_bytes: int) -> dict[str, object]:
    """Hash included source files without following or copying excluded state."""
    entries: list[dict[str, object]] = []
    total_bytes = 0
    stack = [source]
    while stack:
        directory = stack.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            fail(f"cannot inventory recursive workspace {directory}: {exc}")
        for entry in children:
            if excluded(entry.name):
                continue
            candidate = Path(entry.path)
            relative = candidate.relative_to(source).as_posix()
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode):
                try:
                    resolved = candidate.resolve(strict=False)
                except (OSError, RuntimeError) as exc:
                    fail(f"cannot safely resolve workspace symlink {candidate}: {exc}")
                if not inside(resolved, source):
                    fail(f"symlink escapes selected workspace: {candidate}")
                entries.append({
                    "path": relative,
                    "type": "symlink",
                    "target": resolved.relative_to(source).as_posix(),
                })
            elif stat.S_ISDIR(info.st_mode):
                entries.append({"path": relative, "type": "directory", "mode": stat.S_IMODE(info.st_mode)})
                stack.append(candidate)
            elif stat.S_ISREG(info.st_mode):
                total_bytes += info.st_size
                if total_bytes > max_bytes:
                    fail(
                        f"recursive workspace exceeds the {max_bytes}-byte snapshot limit; "
                        "exclude generated data or raise LAB_VM_MAX_SNAPSHOT_BYTES deliberately"
                    )
                digest = hashlib.sha256()
                try:
                    with candidate.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                except OSError as exc:
                    fail(f"cannot hash recursive workspace file {candidate}: {exc}")
                entries.append({
                    "path": relative,
                    "type": "file",
                    "mode": stat.S_IMODE(info.st_mode),
                    "size": info.st_size,
                    "sha256": digest.hexdigest(),
                })
            else:
                fail(f"special files are not allowed in recursive workspace snapshots: {candidate}")

    entries.sort(key=lambda item: str(item["path"]))
    serialized = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return {
        "source_commit": git_commit(source),
        "tree_sha256": hashlib.sha256(serialized).hexdigest(),
        "total_bytes": total_bytes,
        "entry_count": len(entries),
        "entries": entries,
    }


def is_git_root(source: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--show-toplevel"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return result.returncode == 0 and Path(result.stdout.strip()).resolve() == source


def copy_contents(source: Path, target: Path, source_root: Path, target_root: Path) -> None:
    for entry in source.iterdir():
        if excluded(entry.name):
            continue
        destination = target / entry.name
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode):
            try:
                resolved = entry.resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                fail(f"cannot safely resolve workspace symlink {entry}: {exc}")
            if not inside(resolved, source_root):
                fail(f"symlink escapes selected workspace: {entry}")
            mapped_target = target_root / resolved.relative_to(source_root)
            link_target = os.path.relpath(mapped_target, destination.parent)
            destination.symlink_to(link_target)
        elif stat.S_ISDIR(info.st_mode):
            destination.mkdir(mode=stat.S_IMODE(info.st_mode))
            copy_contents(entry, destination, source_root, target_root)
            shutil.copystat(entry, destination, follow_symlinks=False)
        elif stat.S_ISREG(info.st_mode):
            shutil.copy2(entry, destination, follow_symlinks=False)
        else:
            fail(f"special files are not allowed in recursive workspace snapshots: {entry}")


def write_private_json(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, indent=2)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def max_snapshot_bytes_from_environment() -> int:
    raw = os.environ.get("LAB_VM_MAX_SNAPSHOT_BYTES")
    if raw is None:
        return DEFAULT_MAX_SNAPSHOT_BYTES
    if not raw.isdecimal():
        fail("LAB_VM_MAX_SNAPSHOT_BYTES must be a positive integer")
    value = int(raw)
    if not 1 <= value <= MAX_CONFIGURED_SNAPSHOT_BYTES:
        fail(f"LAB_VM_MAX_SNAPSHOT_BYTES must be from 1 to {MAX_CONFIGURED_SNAPSHOT_BYTES}")
    return value


def prepare(source_arg: str, session_arg: str, max_bytes: int | None = None) -> Path:
    source = Path(source_arg).resolve(strict=True)
    if not source.is_dir():
        fail(f"selected workspace is not a directory: {source}")
    max_bytes = max_snapshot_bytes_from_environment() if max_bytes is None else max_bytes
    if not 1 <= max_bytes <= MAX_CONFIGURED_SNAPSHOT_BYTES:
        fail(f"snapshot byte limit must be from 1 to {MAX_CONFIGURED_SNAPSHOT_BYTES}")
    manifest = source_manifest(source, max_bytes)

    session_arg_path = Path(session_arg).expanduser().absolute()
    current = session_arg_path
    while current != current.parent:
        if current.is_symlink():
            fail(f"recursive session path contains a symlink: {current}")
        current = current.parent
    session_parent = session_arg_path.parent.resolve(strict=True) if session_arg_path.parent.exists() else session_arg_path.parent
    session = session_parent / session_arg_path.name
    if inside(session, source) or inside(source, session):
        fail("recursive session directory must be separate from the selected workspace")

    required_free = int(manifest["total_bytes"]) * 2 + STAGING_RESERVE_BYTES
    disk_anchor = session.parent
    while not disk_anchor.exists() and disk_anchor != disk_anchor.parent:
        disk_anchor = disk_anchor.parent
    if not disk_anchor.is_dir():
        fail(f"cannot locate a filesystem for recursive snapshot preflight: {session}")
    try:
        available = shutil.disk_usage(disk_anchor).free
    except OSError as exc:
        fail(f"cannot inspect free space for recursive snapshot: {exc}")
    if available < required_free:
        fail(
            f"insufficient free space for recursive workspace snapshot: need {required_free} bytes "
            f"including staging reserve, have {available} bytes"
        )

    session.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if session.parent.is_symlink() or session.is_symlink():
        fail("recursive session path must not be a symlink")
    if session.parent.stat().st_uid != os.getuid() or not session.parent.is_dir():
        fail("recursive session root must be a directory owned by the current user")
    os.chmod(session.parent, 0o700)
    session.mkdir(mode=0o700, exist_ok=True)
    if session.stat().st_uid != os.getuid() or not session.is_dir():
        fail("recursive session directory must be owned by the current user")
    os.chmod(session, 0o700)

    workspace = session / "workspace"
    marker = session / "source-path"
    manifest_path = session / "source-manifest.json"
    lock_path = session / ".prepare.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if workspace.exists() or workspace.is_symlink() or marker.exists():
            if not workspace.is_dir() or workspace.is_symlink() or not marker.is_file():
                fail(f"incomplete recursive workspace state; inspect and clean explicitly: {session}")
            if marker.read_text().strip() != str(source):
                fail(f"recursive profile already contains a workspace from another source: {session}")
            if not manifest_path.is_file() or manifest_path.is_symlink():
                fail(f"recursive profile lacks revision-bound source metadata; use a fresh profile: {session}")
            try:
                previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                fail(f"recursive profile source metadata is invalid; inspect it explicitly: {exc}")
            if (
                previous_manifest.get("source_commit") != manifest.get("source_commit")
                or previous_manifest.get("tree_sha256") != manifest.get("tree_sha256")
            ):
                fail(f"recursive source changed since this profile was created; use a fresh profile: {session}")
            return workspace.resolve(strict=True)

        temp_workspace = Path(tempfile.mkdtemp(prefix=".workspace-", dir=session))
        try:
            copy_contents(source, temp_workspace, source, temp_workspace)
            source_revision = manifest.get("source_commit")
            if source_revision is not None and is_git_root(source):
                commands = [
                    ["git", "-C", str(temp_workspace), "init", "--quiet"],
                    ["git", "-C", str(temp_workspace), "config", "user.name", "Little Mester Snapshot"],
                    ["git", "-C", str(temp_workspace), "config", "user.email", "snapshot@localhost.invalid"],
                    ["git", "-C", str(temp_workspace), "add", "--force", "--all"],
                    ["git", "-C", str(temp_workspace), "commit", "--quiet", "-m", f"Snapshot source {source_revision[:12]}"],
                ]
                for command in commands:
                    result = subprocess.run(command, check=False, capture_output=True, text=True)
                    if result.returncode != 0:
                        fail(f"could not create an independent snapshot Git commit: {result.stderr.strip()}")
            copied_manifest = source_manifest(temp_workspace, max_bytes)
            if copied_manifest["tree_sha256"] != manifest["tree_sha256"]:
                fail("recursive source changed while being copied; no snapshot was installed")

            os.replace(temp_workspace, workspace)
            marker_tmp = session / ".source-path.tmp"
            marker_tmp.write_text(f"{source}\n")
            os.chmod(marker_tmp, 0o600)
            os.replace(marker_tmp, marker)
            write_private_json(manifest_path, manifest)
        except BaseException:
            if temp_workspace.exists():
                shutil.rmtree(temp_workspace)
            if workspace.exists() and not manifest_path.exists():
                shutil.rmtree(workspace)
            raise
    return workspace.resolve(strict=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--source", required=True)
    prepare_parser.add_argument("--session", required=True)
    prepare_parser.add_argument("--max-bytes", type=int, default=None)
    args = parser.parse_args()
    if args.command == "prepare":
        print(prepare(args.source, args.session, args.max_bytes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
