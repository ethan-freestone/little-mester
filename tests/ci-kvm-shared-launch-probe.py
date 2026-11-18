#!/usr/bin/env python3
"""Bounded disposable QEMU launch through the shared hosted-KVM argv builder."""
from __future__ import annotations

import argparse
import grp
import importlib.util
import os
import pathlib
import pwd
import secrets
import shutil
import socket
import stat
import subprocess
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "lib/vm/parent-qemu.py"
SPEC = importlib.util.spec_from_file_location("shared_qemu_builder", BUILDER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("shared QEMU builder is unavailable")
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


def send_qmp(sock: socket.socket, command: str) -> None:
    import json
    sock.sendall((json.dumps({"execute": command}, separators=(",", ":")) + "\r\n").encode())
    stream = sock.makefile("rb")
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        sock.settimeout(max(0.01, deadline - time.monotonic()))
        line = stream.readline(65537)
        if not line:
            raise RuntimeError("QMP closed before replying")
        message = json.loads(line)
        if "event" in message:
            continue
        if "error" in message or not isinstance(message.get("return"), dict):
            raise RuntimeError("QMP rejected the fixed probe command")
        return
    raise RuntimeError("QMP response exceeded its fixed deadline")


def qmp_quit(path: pathlib.Path, process: subprocess.Popen, deadline: float) -> None:
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"shared-builder QEMU exited before QMP readiness ({process.returncode})")
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(1)
            client.connect(str(path))
            with client:
                greeting = client.makefile("rb").readline(65537)
                if b'"QMP"' not in greeting:
                    raise RuntimeError("QEMU did not provide its QMP greeting")
                send_qmp(client, "qmp_capabilities")
                send_qmp(client, "quit")
            return
        except (OSError, TimeoutError) as error:
            last_error = error
            time.sleep(0.05)
    raise RuntimeError(f"shared-builder QEMU did not publish its QMP socket: {last_error}")


def systemd_preflight() -> tuple[bool, list[str]]:
    missing = [name for name in ("systemd-run", "systemctl", "sudo") if not shutil.which(name)]
    if missing:
        return False, [f"missing-tools={','.join(missing)}"]

    reasons: list[str] = []
    try:
        status = subprocess.run(["systemctl", "is-system-running"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=2, check=False)
    except subprocess.TimeoutExpired:
        reasons.append("system-manager(scope=system,reason=timeout)")
    except OSError as error:
        reasons.append(f"system-manager(scope=system,reason={type(error).__name__})")
    else:
        state = status.stdout.decode("ascii", "replace").strip() or "unknown"
        if state not in ("running", "degraded"):
            reasons.append(f"system-manager(scope=system,state={state},exit={status.returncode})")

    try:
        sudo = subprocess.run(["sudo", "-n", "true"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=2, check=False)
    except subprocess.TimeoutExpired:
        reasons.append("sudo(mode=noninteractive,reason=timeout)")
    except OSError as error:
        reasons.append(f"sudo(mode=noninteractive,reason={type(error).__name__})")
    else:
        if sudo.returncode != 0:
            reasons.append(f"sudo(mode=noninteractive,exit={sudo.returncode})")
    return not reasons, reasons


def launch(command: list[str], qmp_path: pathlib.Path, *, unit: str | None) -> None:
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=subprocess.STDOUT, close_fds=True)
        try:
            qmp_quit(qmp_path, process, time.monotonic() + 6)
            try:
                code = process.wait(timeout=5)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError("shared-builder QEMU did not exit after QMP quit") from error
            if code:
                output.seek(0)
                detail = output.read(4096).decode("utf-8", "replace")
                raise RuntimeError(f"bounded shared-builder launch exited {code}: {detail}")
        except BaseException:
            if unit:
                subprocess.run(["sudo", "-n", "systemctl", "stop", unit],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=4, check=False)
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=pathlib.Path)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve(strict=True)
    work = run_dir / "work"
    if not work.is_dir() or work.is_symlink():
        raise SystemExit("REFUSED: hosted KVM work directory is unavailable")
    if not pathlib.Path("/dev/kvm").is_char_device() or not os.access("/dev/kvm", os.R_OK | os.W_OK):
        print("SHARED_KVM_BUILDER_LAUNCH=NOT_RUN reason=kvm-unavailable")
        print("PRODUCTION_SYSTEMD_VM_UNIT=NOT_RUN reason=production-unit-not-installed")
        return 0
    for path in (work / "guest.qcow2", work / "seed.iso"):
        if not path.is_file() or path.is_symlink():
            raise SystemExit(f"REFUSED: disposable KVM input is unavailable: {path.name}")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
    with tempfile.TemporaryDirectory(prefix="shared-qemu-builder-", dir=work) as temporary, \
         tempfile.TemporaryDirectory(prefix="lmq-") as socket_temporary:
        probe_dir = pathlib.Path(temporary)
        qmp = pathlib.Path(socket_temporary) / "q.sock"
        argv = BUILDER.build_ci_qemu_argv(
            mode="initial", disk_path=str(work / "guest.qcow2"),
            seed_iso=str(work / "seed.iso"), serial_log=str(probe_dir / "serial.log"),
            ssh_port=port)
        qemu = ["/usr/bin/qemu-system-x86_64", *argv, "-S",
                "-qmp", f"unix:{qmp},server=on,wait=off"]
        transient_pass = False
        managed, manager_reasons = systemd_preflight()
        if managed:
            unit = f"lm-ci-shared-qemu-{os.getpid()}-{secrets.token_hex(4)}.service"
            command = ["sudo", "-n", "systemd-run", "--unit", unit, "--wait", "--collect",
                       "--service-type=exec", f"--uid={pwd.getpwuid(os.getuid()).pw_name}",
                       f"--gid={grp.getgrgid(os.getgid()).gr_name}",
                       "--property=RuntimeMaxSec=12s", "--property=TimeoutStopSec=2s",
                       "--property=KillMode=control-group", "--property=SendSIGKILL=yes",
                       "--", *qemu]
            try:
                launch(command, qmp, unit=unit)
                transient_pass = True
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                print(f"HOSTED_SYSTEMD_TRANSIENT_QEMU=NOT_RUN reason={type(error).__name__}")
                try:
                    info = qmp.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                        raise RuntimeError("transient probe left an unsafe QMP artifact")
                    qmp.unlink()
                command = ["/usr/bin/timeout", "--foreground", "--signal=TERM",
                           "--kill-after=2s", "10s", *qemu]
                launch(command, qmp, unit=None)
        else:
            command = ["/usr/bin/timeout", "--foreground", "--signal=TERM",
                       "--kill-after=2s", "10s", *qemu]
            launch(command, qmp, unit=None)
            print("HOSTED_SYSTEMD_TRANSIENT_QEMU=NOT_RUN "
                  f"reason={' ; '.join(manager_reasons)}")
        if transient_pass:
            print("HOSTED_SYSTEMD_TRANSIENT_QEMU=PASS")
        print("SHARED_KVM_BUILDER_LAUNCH=PASS")
        print("PRODUCTION_SYSTEMD_VM_UNIT=NOT_RUN reason=production-unit-not-installed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
