#!/usr/bin/env python3
"""Owner-operated scratch-only qemu-img FD/QEMU fdset and writer-lock probe."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time


TIMEOUT = 15
RAW_BYTES = 16 * 1024 * 1024
TRUSTED_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"


def report(label: str, result: subprocess.CompletedProcess) -> None:
    print(f"=== {label}: exit={result.returncode} ===")
    if result.stdout:
        print("stdout:")
        print(result.stdout.decode("utf-8", errors="replace"), end="")
    if result.stderr:
        print("stderr:")
        print(result.stderr.decode("utf-8", errors="replace"), end="")


def run(label: str, argv: list[str], *, pass_fds: tuple[int, ...] = (), timeout: int = TIMEOUT):
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, close_fds=True, pass_fds=pass_fds,
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or b""
        stderr = error.stderr or b""
        report(label, subprocess.CompletedProcess(argv, 124, stdout, stderr))
        raise RuntimeError(f"{label} exceeded {timeout}s; scratch evidence retained") from error
    report(label, result)
    return result


def qemu_argv(executable: str, disk_fd: int) -> list[str]:
    return [executable, "-machine", "q35,accel=tcg", "-nodefaults", "-display", "none",
            "-S", "-monitor", "none", "-serial", "none", "-no-reboot",
            "-add-fd", f"fd={disk_fd},set=1,opaque=parent-disk",
            "-blockdev", "driver=file,node-name=parent-file,filename=/dev/fdset/1,locking=on,auto-read-only=off",
            "-blockdev", "driver=qcow2,node-name=parent-qcow2,file=parent-file,auto-read-only=off",
            "-device", "virtio-blk-pci,drive=parent-qcow2"]


def start_qemu(executable: str, disk_fd: int, log_path: pathlib.Path) -> subprocess.Popen:
    log = log_path.open("xb")
    try:
        return subprocess.Popen(qemu_argv(executable, disk_fd), stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, close_fds=True,
                                pass_fds=(disk_fd,),
                                start_new_session=True)
    finally:
        log.close()


def stop_owned_qemu(process: subprocess.Popen, log_path: pathlib.Path) -> bool:
    if process.poll() is not None:
        process.wait()
        return True
    try:
        pidfd = os.pidfd_open(process.pid, 0)
    except (OSError, AttributeError):
        print(f"REFUSED: cannot bind cleanup to scratch QEMU process {process.pid}", file=sys.stderr)
        return False
    try:
        signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        try:
            process.wait(timeout=5)
            return True
        except subprocess.TimeoutExpired:
            # Both guests are stopped with -S and use only this fresh scratch disk.
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
            try:
                process.wait(timeout=2)
                return True
            except subprocess.TimeoutExpired:
                print(f"REFUSED: scratch QEMU remains alive; preserving {log_path}", file=sys.stderr)
                return False
    except (OSError, AttributeError) as error:
        print(f"REFUSED: bounded scratch QEMU cleanup failed: {error}", file=sys.stderr)
        return False
    finally:
        os.close(pidfd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-approved", action="store_true",
                        help="confirm explicit owner authorization before running host QEMU/qemu-img")
    args = parser.parse_args()
    if not args.owner_approved:
        parser.error("this host-only gate runs QEMU/qemu-img; obtain owner approval, then pass --owner-approved")
    qemu_img = shutil.which("qemu-img", path=TRUSTED_PATH)
    qemu = shutil.which("qemu-system-x86_64", path=TRUSTED_PATH)
    if not qemu_img or not qemu:
        print("REFUSED: qemu-img and qemu-system-x86_64 must already be installed; no installation attempted",
              file=sys.stderr)
        return 2

    scratch = pathlib.Path(tempfile.mkdtemp(prefix="little-mester-parent-gate-"))
    os.chmod(scratch, 0o700)
    print(f"Scratch-only directory: {scratch}")
    first = second = None
    disk_fd = -1
    safe_to_remove = True
    try:
        for label, executable in (("qemu-img version", qemu_img), ("QEMU version", qemu)):
            version = run(label, [executable, "--version"])
            if version.returncode != 0:
                raise RuntimeError("cannot establish the selected host tool version; scratch retained")
        base = scratch / "base.raw"
        output = scratch / "disk.qcow2.part"
        run("create raw scratch source", [qemu_img, "create", "-f", "raw", str(base), str(RAW_BYTES)])
        fd = os.open(output, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            fd_path = f"/proc/self/fd/{fd}"
            converted = run("qemu-img inherited output-FD convert",
                            [qemu_img, "convert", "-f", "raw", "-O", "qcow2", str(base), fd_path],
                            pass_fds=(fd,), timeout=60)
            if converted.returncode != 0:
                raise RuntimeError("qemu-img inherited-output-FD compatibility failed; scratch retained")
            os.fsync(fd)
        finally:
            os.close(fd)

        info_result = run("explicit qcow2 image inspection",
                          [qemu_img, "info", "--output=json", "-f", "qcow2", str(output)])
        if info_result.returncode != 0:
            raise RuntimeError("explicit-format qemu-img info failed; scratch retained")
        try:
            info = json.loads(info_result.stdout)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeError("qemu-img info returned malformed JSON; scratch retained") from error
        format_specific = info.get("format-specific") if isinstance(info, dict) else None
        format_data = format_specific.get("data") if isinstance(format_specific, dict) else None
        if not isinstance(format_data, dict):
            format_data = {}
        if (not isinstance(info, dict) or info.get("format") != "qcow2"
                or info.get("virtual-size") != RAW_BYTES
                or info.get("backing-filename") not in (None, "")
                or info.get("data-file") not in (None, "")
                or info.get("data-file-raw") not in (None, False)
                or format_data.get("data-file") not in (None, "")
                or format_data.get("data-file-raw") not in (None, False)):
            raise RuntimeError("scratch image format/size/backing/data-file check failed; scratch retained")

        disk_fd = os.open(output, os.O_RDWR | os.O_NOFOLLOW)
        first = start_qemu(qemu, disk_fd, scratch / "qemu-first.log")
        time.sleep(2)
        if first.poll() is not None:
            print((scratch / "qemu-first.log").read_text(errors="replace"), end="")
            raise RuntimeError("first paused fdset QEMU did not remain running; scratch retained")

        second_fd = os.open(output, os.O_RDWR | os.O_NOFOLLOW)
        try:
            first_info = os.fstat(disk_fd)
            second_info = os.fstat(second_fd)
            if (second_fd == disk_fd or (first_info.st_dev, first_info.st_ino) !=
                    (second_info.st_dev, second_info.st_ino)):
                raise RuntimeError("separate writer descriptor does not match the scratch disk inode")
            second = start_qemu(qemu, second_fd, scratch / "qemu-second.log")
        finally:
            os.close(second_fd)
        try:
            second.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if not stop_owned_qemu(second, scratch / "qemu-second.log"):
                safe_to_remove = False
            print((scratch / "qemu-second.log").read_text(errors="replace"), end="")
            raise RuntimeError("second writer was not refused within 5s; scratch retained")
        if first.poll() is not None:
            raise RuntimeError("first QEMU exited during the second-writer test; scratch retained")
        second_output = (scratch / "qemu-second.log").read_text(errors="replace")
        print("=== second QEMU lock diagnostic ===")
        print(second_output, end="")
        lowered = second_output.lower()
        if second.returncode == 0 or 'failed to get "write" lock' not in lowered:
            raise RuntimeError("second writer did not fail with a lock diagnostic; scratch retained")
        print("PASS: inherited qemu-img output FD, explicit qcow2 format, QEMU fdset and writer lock.")
    except (OSError, RuntimeError) as error:
        print(f"NOT_PASS: {error}", file=sys.stderr)
        safe_to_remove = False
        return 1
    finally:
        if second is not None and not stop_owned_qemu(second, scratch / "qemu-second.log"):
            safe_to_remove = False
        if first is not None and not stop_owned_qemu(first, scratch / "qemu-first.log"):
            safe_to_remove = False
        if disk_fd >= 0:
            os.close(disk_fd)
        if safe_to_remove:
            shutil.rmtree(scratch)
            print("Scratch artifacts removed.")
        else:
            print(f"Scratch artifacts retained for owner inspection: {scratch}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
