#!/usr/bin/python3
"""One-shot read-only locator for parent proc-FD scanner refusals.

Invoke under GNU timeout as root in the host procfs visibility domain. This is
diagnostic only and never reports disk clearance.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import pathlib
import re
import resource
import select
import signal
import stat
import sys
import time
import types

sys.stderr = open(os.devnull, "w")
sys.excepthook = lambda _type, _value, _traceback: None

# scripts/parent-readonly-safety-gate.py at bfff3c202794fcbfcfd135447de8a89c446a6a2e
EXPECTED_SHA256 = "8d585c91494aeb48b19108a960a614d54e2b1d7c56f8395082dd7cb3fd5bb1f7"
EXPECTED_COMMIT = "bfff3c202794fcbfcfd135447de8a89c446a6a2e"
EXPECTED_HELPER = pathlib.Path(
    "/opt/little-mester/v3/bfff3c202794fcbfcfd135447de8a89c446a6a2e/"
    "scripts/parent-readonly-safety-gate.py")
FD_PATH = re.compile(r"/proc/(\d+)/task/(\d+)/fd/(\d+)\Z")
OUTPUT_LIMIT = 4096


def _emit(value: dict) -> None:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if len(raw) > OUTPUT_LIMIT:
        raw = '{"result":"DIAGNOSTIC_OUTPUT_LIMIT","clearance":false}'
    print(raw, flush=True)


def _protected_source(path: pathlib.Path) -> bytes:
    path = pathlib.Path(os.path.abspath(path))
    if path != EXPECTED_HELPER:
        raise ValueError("installed helper path mismatch")
    parts = path.parts
    if len(parts) < 2:
        raise ValueError("helper path must name a file")
    directory_fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in parts[1:-1]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC |
                               os.O_NOFOLLOW, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
            parent_info = os.fstat(directory_fd)
            if (not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != 0
                    or parent_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
                raise ValueError("helper parent path is not protected")
        fd = os.open(parts[-1], os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                     dir_fd=directory_fd)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                    or stat.S_IMODE(info.st_mode) != 0o644 or info.st_size > 2 * 1024 * 1024):
                raise ValueError("helper is not a protected bounded regular file")
            chunks = []
            remaining = 2 * 1024 * 1024 + 1
            while remaining:
                part = os.read(fd, min(65536, remaining))
                if not part:
                    break
                chunks.append(part)
                remaining -= len(part)
            source = b"".join(chunks)
            if len(source) > 2 * 1024 * 1024:
                raise ValueError("helper exceeds size bound")
            if hashlib.sha256(source).hexdigest() != EXPECTED_SHA256:
                raise ValueError("installed helper SHA-256 mismatch")
            return source
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)


def _load_exact_helper():
    source = _protected_source(EXPECTED_HELPER)
    namespace = {"__name__": "_installed_parent_readonly_safety_gate",
                 "__file__": str(EXPECTED_HELPER), "__package__": None, "__cached__": None}
    exec(compile(source, str(EXPECTED_HELPER), "exec", dont_inherit=True), namespace)
    return namespace


def _apply_limits() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (25, 26))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))


def _assert_protected_self() -> None:
    path = pathlib.Path(os.path.abspath(__file__))
    directory_fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:-1]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC |
                              os.O_NOFOLLOW, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
            info = os.fstat(directory_fd)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                    or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
                raise ValueError("diagnostic path is not root-protected")
        fd = os.open(path.parts[-1], os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                     dir_fd=directory_fd)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                    or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
                raise ValueError("diagnostic file is not root-protected")
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)


def _instrument(module):
    """Wrap only production helper calls; invoke each wrapped syscall once."""
    real_os = module["os"]
    original_helper = module["_stat_fd_target"]
    real_stat, real_open = real_os.stat, real_os.open
    real_fstat, real_close = real_os.fstat, real_os.close
    active = None
    ordinal = 0
    fallback_successes = 0
    first_failure = None
    seen_paths = {}

    def target_path(value):
        try:
            text = real_os.fspath(value)
            return text if isinstance(text, str) and FD_PATH.fullmatch(text) else None
        except BaseException:
            return None

    def note_errno(record, operation, error):
        try:
            record["errors"].append({"operation": operation, "errno": error.errno})
        except BaseException:
            pass

    def observed_stat(path, *args, **kwargs):
        record = active
        is_target = record is not None and target_path(path) == record["path"]
        try:
            return real_stat(path, *args, **kwargs)
        except OSError as error:
            if is_target:
                note_errno(record, "INITIAL_STAT", error)
            raise

    def observed_open(path, flags, *args, **kwargs):
        record = active
        is_target = record is not None and target_path(path) == record["path"]
        try:
            result = real_open(path, flags, *args, **kwargs)
        except OSError as error:
            if is_target:
                note_errno(record, "FALLBACK_OPEN", error)
            raise
        if is_target:
            record["fallback_fd"] = result
        return result

    def observed_fstat(fd):
        record = active
        is_target = record is not None and record.get("fallback_fd") == fd
        try:
            return real_fstat(fd)
        except OSError as error:
            if is_target:
                note_errno(record, "FALLBACK_FSTAT", error)
            raise

    def observed_close(fd):
        record = active
        is_target = record is not None and record.get("fallback_fd") == fd
        try:
            return real_close(fd)
        except OSError as error:
            if is_target:
                note_errno(record, "FALLBACK_CLOSE", error)
            raise

    proxy = types.SimpleNamespace(**vars(real_os))
    proxy.stat = observed_stat
    proxy.open = observed_open
    proxy.fstat = observed_fstat
    proxy.close = observed_close
    module["os"] = proxy

    def observed_helper(path):
        nonlocal active, ordinal, fallback_successes, first_failure
        raw_path = target_path(path)
        if raw_path is None:
            return original_helper(path)
        ordinal += 1
        previous_observation = seen_paths.get(raw_path)
        repeated = previous_observation is not None
        record = {"ordinal": ordinal, "path": raw_path, "repeat_observation": repeated,
                  "prior_slot_observation_ordinal": (previous_observation[0]
                                                     if previous_observation else None),
                  "prior_slot_observation_result": (previous_observation[1]
                                                    if previous_observation else "NONE"),
                  "fallback_fd": None, "errors": []}
        previous = active
        active = record
        try:
            result = original_helper(path)
        except BaseException as error:
            if first_failure is None and not isinstance(error, FileNotFoundError):
                errors = record["errors"]
                initial_errors = [item for item in errors
                                  if item["operation"] == "INITIAL_STAT"]
                fallback_errors = [item for item in errors
                                   if item["operation"].startswith("FALLBACK_")]
                if fallback_errors:
                    first_stage, final_stage = fallback_errors[0], fallback_errors[-1]
                    operation, err = first_stage["operation"], first_stage["errno"]
                    later_failures = fallback_errors[1:]
                    final_operation, final_errno = final_stage["operation"], final_stage["errno"]
                elif initial_errors and isinstance(error, module["GateRefused"]):
                    operation, err = "O_PATH_UNAVAILABLE", initial_errors[0]["errno"]
                    later_failures = []
                    final_operation, final_errno = operation, err
                elif initial_errors:
                    operation, err = initial_errors[0]["operation"], initial_errors[0]["errno"]
                    later_failures = []
                    final_operation, final_errno = operation, err
                else:
                    operation, err = "UNCLASSIFIED_HELPER_FAILURE", None
                    later_failures = []
                    final_operation, final_errno = operation, err
                first_failure = {
                    "result": "FD_OBSERVATION_REFUSED",
                    "observation_ordinal": ordinal,
                    "repeat_observation": repeated,
                    "prior_slot_observation_ordinal": record["prior_slot_observation_ordinal"],
                    "prior_slot_observation_result": record["prior_slot_observation_result"],
                    "operation": operation,
                    "errno": err,
                    "errno_name": errno.errorcode.get(err) if isinstance(err, int) else None,
                    "later_fallback_failures": later_failures,
                    "production_refusal_operation": final_operation,
                    "production_refusal_errno": final_errno,
                    "initial_stat_errno": (initial_errors[0]["errno"] if initial_errors else None),
                    "initial_stat_errno_name": (errno.errorcode.get(initial_errors[0]["errno"])
                                                if initial_errors and isinstance(
                                                    initial_errors[0]["errno"], int) else None),
                    "fallback_successes_before_failure": fallback_successes,
                    "process_fd_stability": "NOT_SAMPLED",
                    "target_identity": "UNRESOLVED",
                    "clearance": False,
                }
                seen_paths[raw_path] = (ordinal, "REFUSED")
            elif isinstance(error, FileNotFoundError):
                seen_paths[raw_path] = (ordinal, "ENOENT")
            raise
        else:
            if any(item["operation"] == "INITIAL_STAT" for item in record["errors"]):
                fallback_successes += 1
            seen_paths[raw_path] = (ordinal, "OBSERVED")
            return result
        finally:
            active = previous

    module["_stat_fd_target"] = observed_helper
    return lambda: {"first_failure": first_failure,
                    "fallback_successes": fallback_successes,
                    "observations": ordinal}


def main() -> int:
    class SanitizedParser(argparse.ArgumentParser):
        def error(self, _message):
            _emit({"result": "USAGE_ERROR", "clearance": False})
            raise SystemExit(2)

    parser = SanitizedParser(description="Bounded read-only FD-stage diagnostic; never clearance")
    parser.add_argument("device", type=int)
    parser.add_argument("inode", type=int)
    args = parser.parse_args()

    if os.geteuid() != 0:
        _emit({"result": "REFUSED_NOT_ROOT", "clearance": False})
        return 2
    if args.device < 0 or args.inode <= 0:
        _emit({"result": "REFUSED_INVALID_IDENTITY", "clearance": False})
        return 2
    try:
        _assert_protected_self()
    except BaseException:
        _emit({"result": "DIAGNOSTIC_PATH_UNTRUSTED", "clearance": False})
        return 2
    return _run_bounded_scan(args.device, args.inode)


def _scan_once(device: int, inode: int) -> int:
    try:
        _apply_limits()
        module = _load_exact_helper()
        telemetry = _instrument(module)
        try:
            module["_scan_proc_inode"](device, inode, 0, -1)
        except BaseException as error:
            snapshot = telemetry()
            failure = snapshot["first_failure"]
            if failure is not None:
                failure["helper_sha256"] = EXPECTED_SHA256
                failure["helper_commit"] = EXPECTED_COMMIT
                _emit(failure)
            else:
                _emit({"result": "SCAN_REFUSED_OUTSIDE_FD_TARGET_STAGE",
                       "exception_class": type(error).__name__,
                       "fd_observations": snapshot["observations"],
                       "fallback_successes": snapshot["fallback_successes"],
                       "helper_sha256": EXPECTED_SHA256,
                       "helper_commit": EXPECTED_COMMIT,
                       "clearance": False})
            return 0
        snapshot = telemetry()
        _emit({"result": "SCAN_COMPLETED_DIAGNOSTIC_ONLY",
               "fd_observations": snapshot["observations"],
               "fallback_successes": snapshot["fallback_successes"],
               "helper_sha256": EXPECTED_SHA256,
               "helper_commit": EXPECTED_COMMIT,
               "clearance": False})
        return 0
    except BaseException as error:
        _emit({"result": "DIAGNOSTIC_SETUP_FAILED",
               "exception_class": type(error).__name__, "clearance": False})
        return 2


def _run_bounded_scan(device: int, inode: int) -> int:
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
    ready_read, ready_write = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        child = os.fork()
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        os.close(ready_read)
        os.close(ready_write)
        _emit({"result": "WORKER_START_FAILED", "clearance": False})
        return 2
    if child == 0:
        group_ready = False
        output_ready = False
        try:
            os.close(read_fd)
            os.close(ready_read)
            os.setpgid(0, 0)
            group_ready = True
            os.write(ready_write, b"G")
            os.close(ready_write)
            ready_write = -1
            os.dup2(write_fd, 1)
            output_ready = True
            if write_fd != 1:
                os.close(write_fd)
            code = _scan_once(device, inode)
            sys.stdout.flush()
            os.killpg(0, signal.SIGKILL)
            os._exit(code)
        except BaseException:
            if ready_write >= 0:
                try:
                    os.write(ready_write, b"N")
                    os.close(ready_write)
                except BaseException:
                    pass
            if output_ready:
                try:
                    _emit({"result": "WORKER_FAILED", "clearance": False})
                    sys.stdout.flush()
                except BaseException:
                    pass
            if group_ready:
                try:
                    os.killpg(0, signal.SIGKILL)
                except BaseException:
                    pass
            os._exit(2)
    os.close(write_fd)
    os.close(ready_write)
    output = bytearray()
    deadline = time.monotonic() + 30
    reaped = False
    private_group = False
    pipe_eof = False
    wait_status = None
    try:
        ready, _, _ = select.select([ready_read], [], [], max(0.0, deadline - time.monotonic()))
        if ready:
            private_group = os.read(ready_read, 1) == b"G"
        os.close(ready_read)
        ready_read = -1
        if not private_group:
            _emit({"result": "WORKER_START_FAILED", "clearance": False})
            return 2
        while time.monotonic() < deadline:
            if pipe_eof:
                select.select([], [], [], min(0.1, deadline - time.monotonic()))
                ready = []
            else:
                ready, _, _ = select.select([read_fd], [], [],
                                            min(0.1, deadline - time.monotonic()))
            if ready and not pipe_eof:
                part = os.read(read_fd, OUTPUT_LIMIT + 1 - len(output))
                if part:
                    output.extend(part)
                    if len(output) > OUTPUT_LIMIT:
                        raise ValueError("worker output exceeded bound")
                else:
                    pipe_eof = True
            if not reaped:
                waited, status = os.waitpid(child, os.WNOHANG)
                if waited == child:
                    reaped, wait_status = True, status
            if reaped and pipe_eof:
                break
        if time.monotonic() >= deadline:
            if private_group:
                try:
                    os.killpg(child, signal.SIGKILL)
                except OSError:
                    pass
            else:
                try:
                    os.kill(child, signal.SIGKILL)
                except OSError:
                    pass
            if not reaped:
                try:
                    waited, status = os.waitpid(child, os.WNOHANG)
                    if waited == child:
                        reaped, wait_status = True, status
                except ChildProcessError:
                    reaped = True
            _emit({"result": "HARD_TIMEOUT", "clearance": False})
            return 124
        if not reaped:
            waited, status = os.waitpid(child, os.WNOHANG)
            if waited == child:
                reaped, wait_status = True, status
        normal_worker_exit = (reaped and os.WIFSIGNALED(wait_status)
                              and os.WTERMSIG(wait_status) == signal.SIGKILL)
        if not normal_worker_exit or not output or len(output) > OUTPUT_LIMIT:
            _emit({"result": "WORKER_FAILED_OR_OUTPUT_INVALID", "clearance": False})
            return 2
        value = json.loads(output.decode("ascii"))
        if not isinstance(value, dict) or value.get("clearance") is not False:
            _emit({"result": "WORKER_OUTPUT_INVALID", "clearance": False})
            return 2
        _emit(value)
        return 0
    except BaseException:
        if private_group:
            try:
                os.killpg(child, signal.SIGKILL)
            except OSError:
                pass
        else:
            try:
                os.kill(child, signal.SIGKILL)
            except OSError:
                pass
        if not reaped:
            try:
                os.waitpid(child, os.WNOHANG)
            except BaseException:
                pass
        _emit({"result": "WORKER_FAILED_OR_OUTPUT_INVALID", "clearance": False})
        return 2
    finally:
        if not reaped:
            if private_group:
                try:
                    os.killpg(child, signal.SIGKILL)
                except OSError:
                    pass
            else:
                try:
                    os.kill(child, signal.SIGKILL)
                except OSError:
                    pass
            reap_deadline = min(deadline + 0.25, time.monotonic() + 0.25)
            while time.monotonic() < reap_deadline:
                try:
                    waited, status = os.waitpid(child, os.WNOHANG)
                    if waited == child:
                        wait_status, reaped = status, True
                        break
                    if waited < 0:
                        break
                except ChildProcessError:
                    reaped = True
                    break
                select.select([], [], [], min(0.01, reap_deadline - time.monotonic()))
        os.close(read_fd)
        if ready_read >= 0:
            os.close(ready_read)


if __name__ == "__main__":
    sys.exit(main())
