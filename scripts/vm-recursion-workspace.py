#!/usr/bin/env python3
"""Create/reuse a private workspace snapshot for a nested VM profile."""

from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NoReturn


# Keep host-only state and large ignored model weights out of disposable child
# snapshots. The selected source tree remains read/write within its own copy.
EXCLUDED_NAMES = {".bench-models", ".git", ".little-mester.conf", ".mester"}


def fail(message: str) -> NoReturn:
    print(f"REFUSED: {message}", file=sys.stderr)
    raise SystemExit(1)


def inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_source_tree(source: Path) -> None:
    for current, dirs, files in os.walk(source, followlinks=False):
        base = Path(current)
        for name in dirs + files:
            candidate = base / name
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode):
                resolved = candidate.resolve(strict=False)
                if not inside(resolved, source):
                    fail(f"symlink escapes selected workspace: {candidate}")
            elif not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                fail(f"special files are not allowed in recursive workspace snapshots: {candidate}")
        dirs[:] = [name for name in dirs if name not in EXCLUDED_NAMES]


def is_git_root(source: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--show-toplevel"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return result.returncode == 0 and Path(result.stdout.strip()).resolve() == source


def copy_contents(source: Path, target: Path) -> None:
    for entry in source.iterdir():
        if entry.name in EXCLUDED_NAMES:
            continue
        destination = target / entry.name
        if entry.is_symlink():
            destination.symlink_to(os.readlink(entry), target_is_directory=entry.is_dir())
        elif entry.is_dir():
            shutil.copytree(entry, destination, symlinks=True, ignore=shutil.ignore_patterns(*EXCLUDED_NAMES))
        else:
            shutil.copy2(entry, destination, follow_symlinks=False)


def prepare(source_arg: str, session_arg: str) -> Path:
    source = Path(source_arg).resolve(strict=True)
    session_arg_path = Path(session_arg).expanduser().absolute()
    current = session_arg_path
    while current != current.parent:
        if current.is_symlink():
            fail(f"recursive session path contains a symlink: {current}")
        current = current.parent
    session = session_arg_path
    if not source.is_dir():
        fail(f"selected workspace is not a directory: {source}")
    session_parent = session.parent.resolve(strict=True) if session.parent.exists() else session.parent
    session = session_parent / session.name
    if inside(session, source) or inside(source, session):
        fail("recursive session directory must be separate from the selected workspace")

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
    lock_path = session / ".prepare.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if workspace.exists() or workspace.is_symlink() or marker.exists():
            if not workspace.is_dir() or workspace.is_symlink() or not marker.is_file():
                fail(f"incomplete recursive workspace state; inspect and clean explicitly: {session}")
            if marker.read_text().strip() != str(source):
                fail(f"recursive profile already contains a workspace from another source: {session}")
            return workspace.resolve(strict=True)

        validate_source_tree(source)
        temp_workspace = Path(tempfile.mkdtemp(prefix=".workspace-", dir=session))
        try:
            if is_git_root(source):
                result = subprocess.run(
                    ["git", "clone", "--no-hardlinks", "--no-checkout", str(source), str(temp_workspace)],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                if result.returncode != 0:
                    fail(f"could not create an independent Git checkout: {result.stderr.strip()}")
                for entry in list(temp_workspace.iterdir()):
                    if entry.name != ".git":
                        if entry.is_dir() and not entry.is_symlink():
                            shutil.rmtree(entry)
                        else:
                            entry.unlink()
            copy_contents(source, temp_workspace)
            final_workspace = session / "workspace"
            os.replace(temp_workspace, final_workspace)
            marker_tmp = session / ".source-path.tmp"
            marker_tmp.write_text(f"{source}\n")
            os.chmod(marker_tmp, 0o600)
            os.replace(marker_tmp, marker)
        except BaseException:
            if temp_workspace.exists():
                shutil.rmtree(temp_workspace)
            raise
    return workspace.resolve(strict=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--source", required=True)
    prepare_parser.add_argument("--session", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        print(prepare(args.source, args.session))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
