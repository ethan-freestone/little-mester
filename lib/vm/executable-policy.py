#!/usr/bin/env python3
"""Validate that an executable and its full symlink path stay protected."""

import os
import pathlib
import stat
import sys


class UntrustedExecutablePath(ValueError):
    pass


def validate_executable(
    raw_path,
    *,
    directory_owners=frozenset({0}),
    executable_owners=frozenset({0}),
    symlink_owners=frozenset({0}),
):
    """Walk lexical and symlink-target components without resolve() shortcuts."""
    path = pathlib.Path(raw_path)
    if not path.is_absolute():
        path = pathlib.Path.cwd() / path
    components = list(path.parts[1:])
    current = pathlib.Path("/")
    symlink_count = 0

    while components:
        component = components.pop(0)
        if component in ("", "."):
            continue
        if component == "..":
            current = current.parent
            continue

        candidate = current / component
        try:
            info = os.lstat(candidate)
        except OSError as error:
            raise UntrustedExecutablePath("executable path component is missing or unreadable") from error

        if stat.S_ISLNK(info.st_mode):
            if info.st_uid not in symlink_owners:
                raise UntrustedExecutablePath("executable symlink is not root-owned")
            symlink_count += 1
            if symlink_count > 40:
                raise UntrustedExecutablePath("executable symlink chain is too deep")
            target = pathlib.Path(os.readlink(candidate))
            if target.is_absolute():
                current = pathlib.Path("/")
                components = list(target.parts[1:]) + components
            else:
                components = list(target.parts) + components
            continue

        final_component = not components
        if final_component:
            if (info.st_uid not in executable_owners
                    or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                    or not stat.S_ISREG(info.st_mode)
                    or not os.access(candidate, os.X_OK)):
                raise UntrustedExecutablePath("executable is not a protected root-owned regular file")
            return candidate
        elif (info.st_uid not in directory_owners
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or not stat.S_ISDIR(info.st_mode)):
            raise UntrustedExecutablePath("executable path ancestor is not a protected directory")
        current = candidate

    raise UntrustedExecutablePath("executable path does not name a regular executable")


def main(arguments):
    if len(arguments) != 1:
        print("usage: executable-policy.py PATH", file=sys.stderr)
        return 2
    try:
        validate_executable(arguments[0])
    except (OSError, ValueError) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
