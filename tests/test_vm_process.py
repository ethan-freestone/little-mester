import base64
import contextlib
import fcntl
import importlib.util
import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROCESS_PATH = Path(__file__).parents[1] / "lib" / "vm" / "process.py"
SPEC = importlib.util.spec_from_file_location("vm_process", PROCESS_PATH)
process = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(process)


class ProcessTests(unittest.TestCase):
    @contextlib.contextmanager
    def proxy_reload_fixture(self, *, invalid_policy=False, qemu_match=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_dir = root / "vm/profiles/candidate"
            run_dir = profile_dir / "runs/run-123"
            workspace = root / "workspace"
            run_dir.mkdir(parents=True, mode=0o700)
            workspace.mkdir(mode=0o700)
            runtime = profile_dir / "runtime"
            runtime.mkdir(mode=0o700)
            qemu = {"pid": 12345, "start_ticks": 21, "executable": "/usr/bin/qemu-system-x86_64",
                    "argv": ["qemu-system-x86_64", "-enable-kvm"]}
            profile_qemu = dict(qemu)
            if not qemu_match:
                profile_qemu["start_ticks"] += 1
            proxy_identity = {"pid": 12346, "start_ticks": 22, "executable": "/usr/bin/python3",
                              "argv": ["/usr/bin/python3", str(runtime / "lib/vm/proxy.py"),
                                       "--workspace", str(workspace), "--port-file", str(run_dir / "proxy.port"),
                                       "--reload-marker", str(run_dir / "proxy.ready")]}
            profile = {"name": "candidate", "status": "ready", "depth": 0,
                       "workspace": str(workspace), "runtime_payload_sha256": "a" * 64,
                       "runtime_identity": profile_qemu}
            registry = {"processes": {"qemu_0": {"identity": qemu},
                                      "proxy": {"identity": proxy_identity}}}
            paths = process._PATHS
            proxy_module = SimpleNamespace(
                policy=mock.Mock(side_effect=ValueError("invalid policy") if invalid_policy else None,
                                 return_value=(True, set())),
                allowed=mock.Mock(return_value=True),
            )
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(paths, "state_root", return_value=root))
                stack.enter_context(mock.patch.object(paths, "profile_read", return_value=profile))
                stack.enter_context(mock.patch.object(paths, "runtime_validate", return_value={
                    "runtime_payload_sha256": "a" * 64,
                }))
                stack.enter_context(mock.patch.object(paths, "profile_path", return_value=profile_dir))
                stack.enter_context(mock.patch.object(paths, "private_entry"))
                stack.enter_context(mock.patch.object(process, "registry_read", return_value=registry))
                stack.enter_context(mock.patch.object(process, "identity_state", return_value="running"))
                signal_proxy = stack.enter_context(mock.patch.object(process, "registry_signal", return_value="running"))
                stack.enter_context(mock.patch.object(process, "_load_reload_proxy", return_value=proxy_module))
                yield root, profile_dir, run_dir, signal_proxy

    def test_proxy_reload_signals_only_exact_registered_proxy_after_policy_validation(self):
        with self.proxy_reload_fixture() as (root, _profile_dir, run_dir, signal_proxy):
            result = process.proxy_reload_profile(root, "candidate")
        self.assertEqual(result["status"], "reload_signal_sent")
        self.assertTrue(result["policy_allows_auth_host"])
        signal_proxy.assert_called_once_with(run_dir / "processes.json", "run-123", "proxy", signal.SIGHUP, 0)

    def test_proxy_reload_invalid_policy_and_qemu_mismatch_never_signal(self):
        with self.proxy_reload_fixture(invalid_policy=True) as (root, _profile, _run, signal_proxy):
            with self.assertRaises(process.ProcessPolicyError):
                process.proxy_reload_profile(root, "candidate")
            signal_proxy.assert_not_called()
        with self.proxy_reload_fixture(qemu_match=False) as (root, _profile, _run, signal_proxy):
            with self.assertRaises(process.ProcessPolicyError):
                process.proxy_reload_profile(root, "candidate")
            signal_proxy.assert_not_called()

    def test_supervisor_drops_inherited_locks_without_unlocking_controller(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            descriptors = []
            for name in (".allocation.lock", ".profile.lock"):
                fd = os.open(root / name, os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                descriptors.append(fd)
            env = dict(os.environ, VM_HELD_ALLOCATION_ROOT=directory, VM_HELD_PROFILE_PATH=directory,
                       VM_HELD_ALLOCATION_FD=str(descriptors[0]), VM_HELD_PROFILE_FD=str(descriptors[1]))
            registry = root / "processes.json"
            process.registry_initialize(registry, "lock-test")
            supervisor = subprocess.Popen(
                [sys.executable, "-B", str(PROCESS_PATH), "run-logged", "--registry", str(registry),
                 "--run-id", "lock-test", "--role", "proxy", "--log", str(root / "proxy.log"),
                 "--", sys.executable, "-c", "import time; time.sleep(60)"],
                env=env, pass_fds=tuple(descriptors), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    state = process.registry_status(registry, "lock-test")
                    if state["processes"].get("proxy", {}).get("state") == "running":
                        break
                    time.sleep(0.02)
                self.assertEqual(state["processes"].get("proxy", {}).get("state"), "running")
                with (root / ".allocation.lock").open("rb") as probe, self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                for fd in descriptors:
                    os.close(fd)
                descriptors.clear()
                for name in (".allocation.lock", ".profile.lock"):
                    with (root / name).open("rb") as probe:
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                for fd in descriptors:
                    os.close(fd)
                process.registry_stop(registry, "lock-test", 2)
                supervisor.wait(timeout=5)

    def run_logged(self, registry, run_id, role, log, argv, max_bytes=None):
        command = [
            sys.executable, str(PROCESS_PATH), "run-logged",
            "--registry", str(registry), "--run-id", run_id,
            "--role", role, "--log", str(log),
        ]
        if max_bytes is not None:
            command.extend(("--max-bytes", str(max_bytes)))
        command.extend(("--", *argv))
        return subprocess.run(command, check=False, timeout=15).returncode

    def test_pid_reuse_or_argv_change_is_unknown_and_never_signaled(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            identity = process.capture_identity(child.pid)
            self.assertEqual(process.identity_state(identity), "running")

            reused = dict(identity)
            reused["start_ticks"] += 1
            self.assertEqual(process.identity_state(reused), "unknown")
            self.assertEqual(process.signal_identity(reused, signal.SIGTERM, 0.1), "unknown")
            self.assertIsNone(child.poll(), "PID-reuse mismatch must not signal the live process")

            changed_argv = dict(identity)
            changed_argv["argv"] = [*identity["argv"], "not-the-recorded-argv"]
            self.assertEqual(process.identity_state(changed_argv), "unknown")
            self.assertEqual(process.signal_identity(changed_argv, signal.SIGTERM, 0.1), "unknown")
            self.assertIsNone(child.poll(), "argv mismatch must not signal the live process")

            self.assertEqual(process.signal_identity(identity, signal.SIGTERM, 2), "stopped")
            self.assertIsNotNone(child.poll())
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=3)

    def test_registry_is_private_atomic_and_reports_absent_running_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = root / "runner-state.json"
            lock = root / f".{registry.name}.lock"
            self.assertEqual(process.registry_status(registry, "run-a")["state"], "absent")
            self.assertFalse(lock.exists())
            process.registry_initialize(registry, "run-a")
            self.assertEqual(registry.stat().st_mode & 0o777, 0o600)
            self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
            self.assertEqual(process.registry_status(registry, "run-a")["state"], "stopped")

            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                process.registry_add(registry, "run-a", "qemu", process.capture_identity(child.pid))
                self.assertEqual(process.registry_status(registry, "run-a", "qemu")["state"], "running")
                self.assertEqual(process.registry_status(registry, "run-b")["state"], "unknown")
            finally:
                child.terminate()
                child.wait(timeout=3)
            self.assertEqual(process.registry_status(registry, "run-a", "qemu")["state"], "stopped")

            with self.assertRaises((FileExistsError, process.ProcessPolicyError)):
                process.registry_initialize(registry, "run-b")
            self.assertEqual(process.registry_status(registry, "run-a")["state"], "stopped")
            lock.unlink()
            self.assertEqual(process.registry_status(registry, "run-a")["state"], "unknown")
            self.assertFalse(lock.exists(), "read-only status must not recreate a missing lock")

    def test_run_logged_drains_flood_and_keeps_only_two_bounded_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = root / "runner-state.json"
            log = root / "serial.log"
            process.registry_initialize(registry, "run-flood")
            argv = [
                sys.executable,
                "-c",
                "import os; block=b'x'*65536; [os.write(1, block) for _ in range(40)]",
            ]
            self.assertEqual(self.run_logged(registry, "run-flood", "qemu", log, argv, 65536), 0)
            current = log.read_bytes()
            rotated = Path(f"{log}.1").read_bytes()
            self.assertEqual(len(current), 65536)
            self.assertEqual(len(rotated), 65536)
            self.assertEqual(current + rotated, b"x" * 131072)
            self.assertFalse(Path(f"{log}.2").exists())
            state = process.registry_status(registry, "run-flood", "qemu")
            self.assertEqual(state["state"], "stopped")
            self.assertEqual(state["processes"]["qemu"]["exit_code"], 0)

    def test_run_logged_preserves_child_exit_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = root / "runner-state.json"
            process.registry_initialize(registry, "run-fail")
            argv = [sys.executable, "-c", "raise SystemExit(37)"]
            self.assertEqual(self.run_logged(registry, "run-fail", "qemu", root / "qemu.log", argv), 37)
            state = process.registry_status(registry, "run-fail", "qemu")
            self.assertEqual(state["state"], "stopped")
            self.assertEqual(state["processes"]["qemu"]["exit_code"], 37)

    def test_run_logged_records_exec_failure_without_orphaning_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = root / "runner-state.json"
            log = root / "qemu.log"
            process.registry_initialize(registry, "run-exec-fail")
            result = self.run_logged(registry, "run-exec-fail", "qemu", log, [str(root / "missing-qemu")])
            self.assertEqual(result, 127)
            state = process.registry_status(registry, "run-exec-fail")
            self.assertEqual(state["processes"]["qemu"]["state"], "stopped")
            self.assertEqual(state["processes"]["qemu"]["exit_code"], 127)
            self.assertEqual(state["processes"]["qemu_logger"]["state"], "stopped")
            self.assertIn(b"launch failed", log.read_bytes())

    def test_run_logged_keeps_draining_when_log_storage_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = root / "runner-state.json"
            process.registry_initialize(registry, "run-full")
            argv = [
                sys.executable,
                "-c",
                "import os; block=b'x'*65536; [os.write(1, block) for _ in range(40)]",
            ]
            with mock.patch.object(process._PATHS.BoundedLogWriter, "write", side_effect=OSError(28, "fixture ENOSPC")):
                result = process.run_logged(registry, "run-full", "qemu", root / "serial.log", argv)
            self.assertEqual(result, 0)
            state = process.registry_status(registry, "run-full", "qemu")
            self.assertEqual(state["processes"]["qemu"]["state"], "stopped")
            self.assertEqual(state["processes"]["qemu"]["exit_code"], 0)

    def test_loopback_listener_must_belong_to_recorded_process(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            port = listener.getsockname()[1]
            self.assertTrue(process.listener_owned_by(os.getpid(), port))
            self.assertFalse(process.listener_owned_by(os.getpid(), port + 1))
        finally:
            listener.close()

    def test_known_host_scan_requires_complete_ed25519_and_never_replaces_key(self):
        with tempfile.TemporaryDirectory() as directory:
            known_hosts = Path(directory) / "known_hosts"
            blob = struct.pack("!I", len(b"ssh-ed25519")) + b"ssh-ed25519"
            blob += struct.pack("!I", 32) + b"k" * 32
            key = base64.b64encode(blob).decode("ascii")
            valid = f"127.0.0.1 ssh-ed25519 {key}\n".encode("ascii")
            with self.assertRaises(process.ProcessPolicyError):
                process.trust_host_key(known_hosts, b"127.0.0.1 ssh-ed25519 partial\n")
            short_blob = struct.pack("!I", len(b"ssh-ed25519")) + b"ssh-ed25519"
            short_blob += struct.pack("!I", 1) + b"k"
            short_key = base64.b64encode(short_blob).decode("ascii")
            with self.assertRaises(process.ProcessPolicyError):
                process.trust_host_key(known_hosts, f"127.0.0.1 ssh-ed25519 {short_key}\n".encode())
            self.assertFalse(known_hosts.exists())
            self.assertEqual(process.trust_host_key(known_hosts, valid), "created")
            self.assertEqual(known_hosts.stat().st_mode & 0o777, 0o600)
            self.assertEqual(process.trust_host_key(known_hosts, valid), "matched")

            changed = bytearray(blob)
            changed[-1] ^= 1
            mismatch = f"127.0.0.1 ssh-ed25519 {base64.b64encode(changed).decode()}\n".encode()
            with self.assertRaises(process.ProcessPolicyError):
                process.trust_host_key(known_hosts, mismatch)
            self.assertEqual(known_hosts.read_bytes(), valid)

    def test_keyscan_comments_are_scan_only_and_extra_keys_remain_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            known_hosts = Path(directory) / "known_hosts"
            blob = struct.pack("!I", 11) + b"ssh-ed25519" + struct.pack("!I", 32) + b"k" * 32
            key = base64.b64encode(blob).decode()
            scanned = f"[127.0.0.1]:40222 ssh-ed25519 {key}\n".encode()
            comment = b"# 127.0.0.1:40222 SSH-2.0-OpenSSH_9.6p1 Ubuntu\n"
            self.assertEqual(process.trust_host_key(known_hosts, comment + scanned, port=40222), "created")
            stored = known_hosts.read_bytes()
            self.assertFalse(stored.startswith(b"#"))
            self.assertEqual(process.trust_host_key(known_hosts, scanned + comment, port=40222), "matched")
            for bad in (scanned + scanned, comment, b"unknown-output\n" + scanned,
                        b"# bad\x00comment\n" + scanned,
                        scanned.replace(b"40222", b"40223"), b"#" * 65537 + scanned):
                with self.subTest(shape=len(bad)), self.assertRaises(process.ProcessPolicyError):
                    process.trust_host_key(known_hosts, bad, port=40222)
                self.assertEqual(known_hosts.read_bytes(), stored)
            result = subprocess.run(
                [sys.executable, "-B", str(PROCESS_PATH), "trust-key", "--known-hosts", str(known_hosts),
                 "--port", "40222"], input=scanned + scanned, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, b"scan-lines=2,comments=0,records=2\n")
            self.assertNotIn(key.encode(), result.stdout + result.stderr)
            known_hosts.write_bytes(comment + stored)
            with self.assertRaises(process.ProcessPolicyError):
                process.trust_host_key(known_hosts, scanned, port=40222)


if __name__ == "__main__":
    unittest.main()
