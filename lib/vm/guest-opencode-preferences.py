#!/usr/bin/env python3
"""Validated, non-security OpenCode preference storage for the protected guest."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import secrets
import stat
import sys


class Refused(RuntimeError):
    """Preference input or persisted state is outside the allowlist."""


MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
THEME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}\Z")
PREFERENCE_KEYS = frozenset({"model", "theme"})
PREFERENCE_PATH = pathlib.Path("/home/mester/.local/state/opencode/preferences.json")
PROTECTED_PLUGIN = "/etc/opencode/plugins/little-mester-exit-confirm"
PROTECTED_METRICS_PLUGIN = "/etc/opencode/plugins/little-mester-host-metrics-tui"


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise Refused("preference JSON contains a duplicate key")
        value[key] = item
    return value


def parse_preferences(raw: str | bytes) -> dict[str, str]:
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _value: (_ for _ in ()).throw(
                               Refused("non-finite JSON values are forbidden")))
    except Refused:
        raise
    except (UnicodeError, json.JSONDecodeError, TypeError) as error:
        raise Refused("preference document is malformed") from error
    if not isinstance(value, dict) or set(value) - PREFERENCE_KEYS:
        raise Refused("only the model and theme preferences are supported")
    result: dict[str, str] = {}
    for key, pattern in (("model", MODEL_RE), ("theme", THEME_RE)):
        if key not in value:
            continue
        item = value[key]
        if not isinstance(item, str) or not pattern.fullmatch(item):
            raise Refused(f"{key} preference is outside its fixed safe grammar")
        result[key] = item
    return result


def build_cli_config(preferences: dict[str, str], *, host_metrics: bool = False) -> dict:
    if type(host_metrics) is not bool:
        raise Refused("host metrics plugin selector must be boolean")
    safe = parse_preferences(json.dumps(preferences, separators=(",", ":")))
    config = {
        "plugins": [PROTECTED_PLUGIN, *([PROTECTED_METRICS_PLUGIN] if host_metrics else [])],
        "keybinds": {"app.exit": "none", "prompt.clear": "none"},
    }
    if "theme" in safe:
        # OpenCode 2.2.5 CLI config accepts TUI theme as an object; `model` is
        # a server config property and is applied separately by the launcher.
        config["theme"] = {"name": safe["theme"]}
    return config


def _read_file(path: pathlib.Path, *, uid: int) -> bytes:
    try:
        info = os.lstat(path)
        parent = os.lstat(path.parent)
        if (not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode)
                or parent.st_uid != uid or parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1 or info.st_size > 4096):
            raise Refused("preference file or parent has unsafe owner, type, mode, or size")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise Refused("preference file changed while opening")
            return os.read(fd, 4097)
        finally:
            os.close(fd)
    except Refused:
        raise
    except FileNotFoundError:
        return b"{}"
    except OSError as error:
        raise Refused("preference file is unavailable") from error


def read_preferences(path: pathlib.Path = PREFERENCE_PATH, *, uid: int = 1000) -> dict[str, str]:
    return parse_preferences(_read_file(pathlib.Path(path), uid=uid))


def write_preferences(path: pathlib.Path, preferences: dict[str, str], *, uid: int) -> None:
    path = pathlib.Path(path)
    safe = parse_preferences(json.dumps(preferences, separators=(",", ":")))
    try:
        parent = path.parent
        parent_info = os.lstat(parent)
        if (not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
                or parent_info.st_uid != uid or parent_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
            raise Refused("preference directory is not private to the guest owner")
        try:
            existing = os.lstat(path)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if (not stat.S_ISREG(existing.st_mode) or stat.S_ISLNK(existing.st_mode)
                    or existing.st_uid != uid or stat.S_IMODE(existing.st_mode) != 0o600
                    or existing.st_nlink != 1 or existing.st_size > 4096):
                raise Refused("preserving unsafe pre-existing preference file")
            parse_preferences(_read_file(path, uid=uid))
        temp = parent / f".{path.name}.{secrets.token_hex(12)}.tmp"
        payload = (json.dumps(safe, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            view = memoryview(payload)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise Refused("preference write made no progress")
                view = view[count:]
            os.fchown(fd, uid, -1)
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        if existing is None:
            os.link(temp, path, follow_symlinks=False)
            os.unlink(temp)
        else:
            os.replace(temp, path)
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Refused:
        raise
    except OSError as error:
        raise Refused("could not safely persist OpenCode preferences") from error


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    config_parser = sub.add_parser("config", help="print protected CLI config with safe preferences")
    config_parser.add_argument("--host-metrics", action="store_true",
                               help="load the fixed v3 host metrics display plugin")
    sub.add_parser("model", help="print the validated model preference, if one is set")
    set_parser = sub.add_parser("set", help="set only model and/or theme preferences")
    set_parser.add_argument("--model")
    set_parser.add_argument("--theme")
    set_parser.add_argument("--clear-model", action="store_true")
    set_parser.add_argument("--clear-theme", action="store_true")
    args = parser.parse_args()
    try:
        current = read_preferences(PREFERENCE_PATH, uid=os.getuid())
        if args.action == "config":
            print(json.dumps(build_cli_config(current, host_metrics=args.host_metrics), separators=(",", ":")))
            return 0
        if args.action == "model":
            print(current.get("model", ""))
            return 0
        if not any((args.model is not None, args.theme is not None,
                    args.clear_model, args.clear_theme)):
            parser.error("set requires --model, --theme, or a clear option")
        if args.model is not None:
            current["model"] = args.model
        if args.theme is not None:
            current["theme"] = args.theme
        if args.clear_model:
            current.pop("model", None)
        if args.clear_theme:
            current.pop("theme", None)
        write_preferences(PREFERENCE_PATH, current, uid=os.getuid())
        print("OpenCode preferences saved. Restart the TUI to apply them.")
        return 0
    except (OSError, Refused, ValueError) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
