#!/usr/bin/env python3
"""Wait for durable parent-supervisor authorization, then exec one child."""

import json
import importlib.util
import os
import pathlib
import stat
import sys


def refuse(message: str) -> int:
    print(f"REFUSED: {message}", file=sys.stderr)
    return 2


def main() -> int:
    if (len(sys.argv) != 11 or sys.argv[1] != "--release-fd" or sys.argv[3] != "--token"
            or sys.argv[5] != "--target" or sys.argv[7] != "--identity"
            or sys.argv[9] != "--state-dir-fd"):
        return refuse("invalid parent gate arguments")
    try:
        release_fd = int(sys.argv[2])
    except ValueError:
        return refuse("invalid parent gate descriptor")
    token = sys.argv[4].encode("ascii", "strict")
    if len(token) != 32 or any(byte not in b"0123456789abcdef" for byte in token):
        return refuse("invalid parent gate token")
    target_name = sys.argv[6]
    identity_name = sys.argv[8]
    try:
        state_dir_fd = int(sys.argv[10])
    except ValueError:
        return refuse("invalid parent state directory descriptor")
    if (not target_name.startswith("child-") or not target_name.endswith(".target.json")
            or not identity_name.startswith("child-") or not identity_name.endswith(".identity.json")):
        return refuse("invalid private gate filenames")
    try:
        received = b""
        while len(received) <= len(token):
            chunk = os.read(release_fd, len(token) + 1 - len(received))
            if not chunk:
                return 0  # Supervisor died or aborted before commit: no work is launched.
            received += chunk
            if len(received) >= len(token):
                break
        if received != token:
            return refuse("parent gate authorization mismatch")
        if os.read(release_fd, 1):
            return refuse("parent gate authorization has trailing data")
    except OSError:
        return 0
    finally:
        try:
            os.close(release_fd)
        except OSError:
            pass

    try:
        state = _load_module("parent_state", pathlib.Path(__file__).with_name("parent-state.py"))
        directory_info = os.fstat(state_dir_fd)
        if (not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.getuid()
                or stat.S_IMODE(directory_info.st_mode) != 0o700):
            return refuse("parent state directory descriptor is unsafe")
        target = state._read_json(state_dir_fd, target_name)
        identity = state._read_json(state_dir_fd, identity_name)
        state._validate_exec_transition(identity)
        current = state.observe_process(os.getpid())
        if (identity["pid"] != os.getpid() or identity["uid"] != current["uid"]
                or identity["host_boot_id"] != current["host_boot_id"]
                or identity["start_time"] != current["start_time"]
                or identity["release_token"] != token.decode("ascii")
                or identity["target_path"] != target_name
                or identity["identity_path"] != identity_name
                or identity["gate"] != {"exe": current["exe"], "argv": current["argv"]}):
            return refuse("durable child identity does not authorize this gate")
        if (set(target) != {"executable", "argv"}
                or target.get("executable") != identity["target"]["exe"]
                or target.get("argv") != identity["target"]["argv"]):
            return refuse("durable child identity does not authorize this target")
        executable = target["executable"]
        argv = target["argv"]
        if os.path.realpath(executable) != executable or argv[0] != executable:
            return refuse("parent gate target executable is not canonical")
        policy = _load_module("executable_policy", pathlib.Path(__file__).with_name("executable-policy.py"))
        policy.validate_executable(executable)
        os.close(state_dir_fd)
        _close_other_fds({0, 1, 2})
        os.execve(executable, argv, {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/nonexistent",
                                     "LANG": "C", "LC_ALL": "C"})
    except (OSError, RuntimeError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        return refuse(f"could not safely execute authorized parent child: {error}")
    return 127


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise OSError("protected helper module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _close_other_fds(keep):
    with os.scandir("/proc/self/fd") as entries:
        descriptors = [int(entry.name) for entry in entries if entry.name.isdigit()]
    for descriptor in descriptors:
        if descriptor not in keep:
            try:
                os.close(descriptor)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
