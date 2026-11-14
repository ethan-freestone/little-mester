#!/usr/bin/env python3
"""Credential-free OpenCode OAuth bridge test against the disposable QEMU guest.

The real OpenCode CLI generates the authorization URL, the host bridge creates
the pinned SSH forward, and these local fixtures act only as the browser/IdP
callback. The restricted second-boot QEMU network prevents a token exchange
from reaching any upstream provider.
"""

from __future__ import annotations

import argparse
import errno
import importlib.util
import json
import os
import pathlib
import pty
import select
import shutil
import signal
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "lib/vm/browser-auth-session.py"
PROCESS_SIGNAL_PATH = pathlib.Path(__file__).with_name("ci-process-signal.py")
PROCESS_SIGNAL_SPEC = importlib.util.spec_from_file_location("ci_process_signal", PROCESS_SIGNAL_PATH)
PROCESS_SIGNAL = importlib.util.module_from_spec(PROCESS_SIGNAL_SPEC)
PROCESS_SIGNAL_SPEC.loader.exec_module(PROCESS_SIGNAL)
SPEC = importlib.util.spec_from_file_location("browser_auth_contract", BRIDGE)
AUTH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTH)


class HarnessCancelled(Exception):
    def __init__(self, signum: int, cleanup_detail: str | None = None):
        super().__init__(f"OAuth harness cancelled by signal {signum}")
        self.signum = signum
        self.cleanup_detail = cleanup_detail


def _raise_on_signal(signum, _frame):
    raise HarnessCancelled(signum)


def install_signal_handlers() -> dict[int, object]:
    """Install handlers while the caller has all handled signals blocked."""
    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
        previous[signum] = signal.signal(signum, _raise_on_signal)
    return previous


def restore_signal_handlers(previous: dict[int, object]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def handled_signals() -> set[signal.Signals]:
    return {signal.SIGINT, signal.SIGTERM, signal.SIGALRM}


def process_group_has_live_members(pgid: int) -> bool:
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="ascii")
            rest = stat[stat.rfind(") ") + 2 :].split()
            if len(rest) > 3 and int(rest[2]) == pgid and rest[0] != "Z":
                return True
        except FileNotFoundError:
            # The process exited between enumerating /proc and reading stat.
            continue
        except (OSError, ValueError, IndexError):
            # An entry that remains present but cannot be parsed is not proof
            # that the owned process group is empty. Fail cleanup closed.
            try:
                entry.stat()
            except FileNotFoundError:
                continue
            except OSError:
                return True
            return True
    return False


def write_bridge_identity(path: pathlib.Path, pid: int) -> dict[str, int]:
    stat = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    fields = stat[stat.rfind(") ") + 2 :].split()
    if len(fields) <= 19 or int(fields[2]) != pid or int(fields[3]) != pid:
        raise RuntimeError("PTY bridge did not create its own process group")
    status = pathlib.Path(f"/proc/{pid}/status").read_text(encoding="ascii")
    uid = int(next(line for line in status.splitlines() if line.startswith("Uid:")).split()[1])
    record = {"pid": pid, "starttime": int(fields[19]), "uid": uid,
              "process_group": int(fields[2]), "session_id": int(fields[3])}
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as output:
        json.dump(record, output, separators=(",", ":"))
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    return record


def parse_live_url(data: bytes) -> str | None:
    prefix = AUTH.AUTHORIZATION_URL_PREFIX
    start = data.find(prefix)
    if start < 0:
        return None
    end = start + len(prefix)
    while end < len(data) and data[end] in AUTH.URL_CHARACTERS:
        end += 1
    if end == len(data):
        return None
    url = data[start:end].decode("ascii")
    AUTH.validate_authorization_url(url)
    return url


def request_callback(port: int, query: dict[str, str]) -> tuple[int, bytes]:
    url = f"http://127.0.0.1:{port}/auth/callback?{urllib.parse.urlencode(query)}"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read(4096)
    except urllib.error.HTTPError as error:
        return error.code, error.read(4096)


class Session:
    def __init__(self, port: int, key: pathlib.Path, known_hosts: pathlib.Path,
                 case: str, evidence_root: pathlib.Path):
        self.case = case
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="oauth-case-", dir=evidence_root))
        os.chmod(self.root, 0o700)
        self.run_dir = self.root / f"{case}-{os.getpid()}"
        self.run_dir.mkdir(mode=0o700)
        self.control_dir = pathlib.Path(tempfile.gettempdir()) / f"lmv3-{os.getpid()}-{case}"
        marker = self.root / "control-directory"
        marker.write_text(str(self.control_dir), encoding="utf-8")
        os.chmod(marker, 0o600)
        self.control_dir.mkdir(mode=0o700)
        self.control_socket = self.control_dir / "auth-control.sock"
        socket_path_bytes = len(os.fsencode(self.control_socket))
        if socket_path_bytes >= AUTH.CONTROL_SOCKET_LIMIT:
            raise RuntimeError(
                "hosted CI private SSH control socket path exceeds the production limit "
                f"({socket_path_bytes} bytes); evidence retained at {self.root}"
            )
        self.proof = self.run_dir / "proof.json"
        self.bridge_identity = self.root / "bridge-process.json"
        identity = self.run_dir / "identity_file"
        pinned_hosts = self.run_dir / "known_hosts"
        shutil.copyfile(key, identity)
        shutil.copyfile(known_hosts, pinned_hosts)
        os.chmod(identity, 0o600)
        os.chmod(pinned_hosts, 0o600)
        self.command = [
            "/usr/bin/python3", "-I", str(BRIDGE), "--ssh", "/usr/bin/ssh",
            "--ssh-port", str(port), "--identity-file", str(identity),
            "--known-hosts", str(pinned_hosts),
            "--control-socket", str(self.control_socket),
            "--ssh-identity", str(self.run_dir / "ssh.identity"),
            "--callback-port-file", str(self.run_dir / "callback.port"),
            "--proof-file", str(self.proof), "--run-dir", str(self.run_dir),
            "--guest-auth-dir", "/tmp/mester-v3-auth-ci", "--timeout-seconds", "90",
            "--output-limit", "65536", "--persistent-parent",
        ]
        self.pid = -1
        self.bridge_pgid = -1
        self.bridge_pidfd: int | None = None
        self.bridge_anchor: tuple[int, int] | None = None
        self.master_fd = -1
        self.output = bytearray()
        self.exit_status: int | None = None
        self.forced_kill = False
        self.closed = False

    def start(self) -> str:
        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals())
        try:
            child_pid, master_fd = pty.fork()
            if child_pid > 0:
                # Publish both owned handles before a pending cancellation can
                # run the handler and enter Session.close().
                self.pid, self.master_fd = child_pid, master_fd
                self.bridge_pgid = child_pid
                try:
                    self.bridge_pidfd = os.pidfd_open(child_pid, 0)
                    identity = write_bridge_identity(self.bridge_identity, child_pid)
                    self.bridge_anchor = (child_pid, identity["starttime"])
                except BaseException:
                    try:
                        if self.bridge_pidfd is not None:
                            signal.pidfd_send_signal(self.bridge_pidfd, signal.SIGKILL)
                        else:
                            # This is the unreaped direct child returned by
                            # pty.fork(); its PID cannot be reused before waitpid.
                            os.kill(child_pid, signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        pass
                    try:
                        os.waitpid(child_pid, 0)
                    except ChildProcessError:
                        pass
                    self.pid = -1
                    os.close(master_fd)
                    self.master_fd = -1
                    if self.bridge_pidfd is not None:
                        os.close(self.bridge_pidfd)
                        self.bridge_pidfd = None
                    raise
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        if child_pid == 0:
            for name in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "DISPLAY", "WAYLAND_DISPLAY"):
                os.environ.pop(name, None)
            os.execv(self.command[0], self.command)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.master_fd], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(self.master_fd, 8192)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                self.output.extend(chunk)
                if len(self.output) > 65536:
                    raise RuntimeError("auth bridge exceeded the bounded CI capture")
                url = parse_live_url(bytes(self.output))
                if url is not None:
                    return url
            waited, status = os.waitpid(self.pid, os.WNOHANG)
            if waited:
                self.exit_status = os.waitstatus_to_exitcode(status)
                self.pid = -1
                raise RuntimeError(f"OpenCode auth exited before producing its URL (status={self.exit_status})")
        raise RuntimeError("OpenCode auth URL did not reach the local simulator before the deadline")

    def wait(self, seconds: float = 30) -> None:
        deadline = time.monotonic() + seconds
        while self.pid > 0 and time.monotonic() < deadline:
            ready, _, _ = select.select([self.master_fd], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(self.master_fd, 8192)
                except OSError as error:
                    if error.errno == errno.EIO:
                        chunk = b""
                    else:
                        raise
                self.output.extend(chunk)
                if len(self.output) > 65536:
                    raise RuntimeError("auth bridge exceeded the bounded CI capture")
            waited, status = os.waitpid(self.pid, os.WNOHANG)
            if waited:
                self.exit_status = os.waitstatus_to_exitcode(status)
                self.pid = -1
                break

    def interrupt(self) -> None:
        if self.pid > 0:
            os.kill(self.pid, signal.SIGINT)
            wait_error = None
            try:
                self.wait(10)
            except BaseException as error:
                # A PTY read/capture failure must not bypass the bounded
                # terminate-and-reap fallback for the owned bridge child.
                wait_error = error
            if self.pid > 0:
                owned_pgid = self.pid
                self.forced_kill = True
                try:
                    # pty.fork() makes the bridge a session/process-group
                    # leader; include its SSH forward if graceful cleanup failed.
                    os.killpg(owned_pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    try:
                        waited, status = os.waitpid(self.pid, os.WNOHANG)
                    except ChildProcessError:
                        self.pid = -1
                        break
                    if waited:
                        self.exit_status = os.waitstatus_to_exitcode(status)
                        self.pid = -1
                        break
                    time.sleep(0.05)
                if self.pid > 0:
                    raise RuntimeError(f"{self.case}: auth bridge was not reaped after bounded SIGKILL")
                group_deadline = time.monotonic() + 3
                while process_group_has_live_members(owned_pgid) and time.monotonic() < group_deadline:
                    time.sleep(0.05)
                if process_group_has_live_members(owned_pgid):
                    raise RuntimeError(f"{self.case}: PTY process group survived bounded SIGKILL")
            if wait_error is not None:
                raise wait_error

    def close(self) -> None:
        if self.closed:
            return
        previous = {}
        deferred_signals = []
        signal_mask = signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals())
        if __import__("threading").current_thread() is __import__("threading").main_thread():
            try:
                for signum in handled_signals():
                    previous[signum] = signal.signal(
                        signum, lambda received, _frame: deferred_signals.append(received)
                    )
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, signal_mask)
        else:
            signal.pthread_sigmask(signal.SIG_SETMASK, signal_mask)
        failures = []
        proof_complete = False
        try:
            try:
                self.interrupt()
            except Exception as error:
                failures.append(f"bounded auth-process stop failed ({error})")
            if self.bridge_pgid > 0 and process_group_has_live_members(self.bridge_pgid):
                try:
                    group_stopped = PROCESS_SIGNAL.terminate_marked_groups(
                        self.root, anchor=getattr(self, "bridge_anchor", None)
                    )
                except Exception as error:
                    group_stopped = False
                    failures.append(f"PTY bridge descendant cleanup failed ({error})")
                if not group_stopped or process_group_has_live_members(self.bridge_pgid):
                    failures.append(
                        f"PTY bridge process group {self.bridge_pgid} was not proved empty; "
                        f"artifacts retained at {self.root}"
                    )
                else:
                    # A clean-looking bridge proof is insufficient if a
                    # descendant survived it; contain the process but keep
                    # the case failed and retain its evidence.
                    failures.append(
                        f"PTY bridge process group {self.bridge_pgid} had surviving descendants; "
                        f"they were stopped and evidence is retained at {self.root}"
                    )
            if self.master_fd >= 0:
                try:
                    os.close(self.master_fd)
                except OSError as error:
                    failures.append(f"PTY close failed ({error})")
                self.master_fd = -1
            try:
                if not self.proof.is_file():
                    raise RuntimeError(
                        f"auth bridge did not publish cleanup proof; artifacts retained at {self.root}"
                    )
                proof = __import__("json").loads(self.proof.read_text(encoding="utf-8"))
                if (not proof.get("ssh_stopped") or not proof.get("control_socket_absent")
                        or not proof.get("listener_verified_closed") or not proof.get("cleanup_complete")):
                    raise RuntimeError(
                        f"SSH/listener/control-socket cleanup was not proved; artifacts retained at {self.root}"
                    )
                if self.exit_status is None or self.forced_kill:
                    raise RuntimeError(
                        f"auth process did not exit cleanly after bounded interruption; "
                        f"artifacts retained at {self.root}"
                    )
                proof_complete = True
            except Exception as error:
                failures.append(f"cleanup proof failed ({error})")
            if proof_complete and not failures:
                # Keep the evidence root as a shell-visible cleanup marker
                # until the separately located short SSH control directory is gone.
                shutil.rmtree(self.control_dir)
                shutil.rmtree(self.root)
                self.closed = True
        finally:
            bridge_pidfd = getattr(self, "bridge_pidfd", None)
            if bridge_pidfd is not None:
                os.close(bridge_pidfd)
                self.bridge_pidfd = None
            restore_mask = signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals())
            try:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, restore_mask)
        if deferred_signals:
            detail = "; ".join(failures) if failures else None
            raise HarnessCancelled(deferred_signals[0], cleanup_detail=detail)
        if failures:
            raise RuntimeError(f"{self.case}: " + "; ".join(failures))


def run_case(args, case: str) -> None:
    session = None
    try:
        handled = {signal.SIGINT, signal.SIGTERM, signal.SIGALRM}
        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, handled)
        try:
            session = Session(args.port, pathlib.Path(args.identity),
                              pathlib.Path(args.known_hosts), case,
                              pathlib.Path(args.evidence_root))
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        url = session.start()
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        callback = urllib.parse.urlsplit(query["redirect_uri"][0])
        state = query["state"][0]
        if case == "success":
            status, _ = request_callback(callback.port, {"code": "synthetic-ci-code", "state": state})
            if status != 200:
                raise RuntimeError(f"synthetic valid callback returned unexpected HTTP status {status}")
        elif case == "mismatched-state":
            status, _ = request_callback(callback.port, {"code": "synthetic-ci-code", "state": "X" * 43})
            if status != 400:
                raise RuntimeError(f"callback listener did not reject mismatched OAuth state (HTTP {status})")
        elif case == "denial":
            status, _ = request_callback(callback.port, {"error": "access_denied", "state": state})
            if status != 400:
                raise RuntimeError(f"pinned OpenCode callback did not reject provider denial (HTTP {status})")
        elif case == "expiration":
            status, _ = request_callback(callback.port, {"error": "expired", "state": state})
            if status != 400:
                raise RuntimeError(f"pinned OpenCode callback did not reject expiration response (HTTP {status})")
        elif case == "malformed":
            status, _ = request_callback(callback.port, {"code": "", "state": state})
            if status != 400:
                raise RuntimeError(f"callback listener did not reject malformed authorization response (HTTP {status})")
        elif case == "interruption":
            session.interrupt()
        if case not in ("mismatched-state", "interruption"):
            session.wait(20)
            if session.pid > 0:
                raise RuntimeError(f"{case}: OpenCode did not finish the synthetic callback path before deadline")
            if session.exit_status != 78:
                raise RuntimeError(
                    f"{case}: expected sanitized auth/token-exchange failure status 78, got {session.exit_status}"
                )
        elif case == "mismatched-state":
            session.interrupt()
        if case in ("mismatched-state", "interruption") and session.exit_status != 130:
            raise RuntimeError(f"{case}: expected bounded SIGINT exit 130, got {session.exit_status}")
    except BaseException as original:
        if session is None:
            raise
        try:
            session.close()
        except Exception as cleanup_error:
            raise RuntimeError(
                f"{case}: test failed ({original}); cleanup proof also failed ({cleanup_error})"
            ) from original
        raise
    else:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--known-hosts", required=True)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--max-runtime-seconds", type=int, default=900)
    args = parser.parse_args()
    if not 1 <= args.max_runtime_seconds <= 2400:
        parser.error("--max-runtime-seconds must be between 1 and 2400")
    # The pinned 2.2.5 callback handler does not guarantee duplicate-callback
    # rejection; replay protection is exercised by the deterministic bridge
    # simulator in vm-browser-auth.test.py instead.
    main_mask = signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals())
    previous = install_signal_handlers()
    signal.alarm(args.max_runtime_seconds)
    try:
        # Pending cancellation is delivered only after the handler is active
        # and execution is protected by the enclosing try/finally.
        signal.pthread_sigmask(signal.SIG_SETMASK, main_mask)
        for case in ("success", "mismatched-state", "denial", "expiration", "malformed", "interruption"):
            run_case(args, case)
            print(f"QEMU_OAUTH_SIMULATOR_PASS: {case} (synthetic; no token/provider/model access)")
    except HarnessCancelled as cancelled:
        suffix = f"; {cancelled.cleanup_detail}" if cancelled.cleanup_detail else ""
        print(f"QEMU_OAUTH_SIMULATOR_CANCELLED: cleanup attempted after signal {cancelled.signum}{suffix}",
              file=__import__("sys").stderr)
        return 128 + cancelled.signum
    finally:
        restore_mask = signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals())
        signal.alarm(0)
        restore_signal_handlers(previous)
        signal.pthread_sigmask(signal.SIG_SETMASK, restore_mask)
    print("QEMU_OAUTH_UPSTREAM_BOUNDARY: token exchange, real identity provider and model response NOT_RUN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
