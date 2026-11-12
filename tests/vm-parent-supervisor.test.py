#!/usr/bin/env python3
"""REAL_PROCESS tests for terminal-independent supervisor lock handoff."""

import base64
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import pty
import pwd
import select
import signal
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
PROTECTED_FIXTURE_ROOT = pathlib.Path(
    os.environ.get("LM_PROTECTED_FIXTURE_ROOT", "/usr/local"))
SPEC = importlib.util.spec_from_file_location("parent_state", ROOT / "lib/vm/parent-state.py")
parent_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(parent_state)
SUPERVISOR = ROOT / "lib/vm/parent-supervisor.py"
SUPERVISOR_SPEC = importlib.util.spec_from_file_location("parent_supervisor", SUPERVISOR)
parent_supervisor = importlib.util.module_from_spec(SUPERVISOR_SPEC)
SUPERVISOR_SPEC.loader.exec_module(parent_supervisor)


class SupervisorHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.temp.name) / "home"
        self.home.mkdir(mode=0o700)
        os.chmod(self.home, 0o700)
        self.parent = parent_state.initialize(home=self.home)
        self.state_dir = self.home / ".local/state/little-mester/parent-v3/default"
        disk = self.state_dir / "disk.qcow2"
        disk.write_bytes(b"qcow2-test-placeholder")
        os.chmod(disk, 0o600)
        info = disk.stat()
        self.parent["disk"].update({"provisioned": True, "device": info.st_dev, "inode": info.st_ino})
        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            fd = parent_state._open_directory_chain(create=False, home_override=self.home)
            try:
                parent_state._atomic_json(fd, "parent.json", self.parent)
            finally:
                os.close(fd)

    def tearDown(self):
        self.temp.cleanup()

    def cleanup_gated_child(self, role, transition=None):
        owned = parent_supervisor._CHILD_PROCESSES.get(role)
        if transition is None and owned is not None:
            transition = owned[1]
        if transition is not None:
            self.assertEqual(parent_supervisor._stop_gated_target(parent_state, role, transition), "STOPPED")
        elif owned is not None:
            owned[0].wait(timeout=3)
            parent_supervisor._CHILD_PROCESSES.pop(role, None)

    @staticmethod
    def cleanup_test_supervisor(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)

    def test_bounded_qemu_output_capture_is_private_and_caps_guest_serial(self):
        log_path = pathlib.Path(self.temp.name) / "qemu-test.log"
        log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        capture = parent_supervisor._BoundedOutputCapture(read_fd, log_fd, limit=256)
        capture.start()
        os.write(write_fd, b"guest-controlled-serial\n" * 1000)
        os.close(write_fd)
        self.assertTrue(capture.finish())
        data = log_path.read_bytes()
        self.assertLessEqual(len(data), 256)
        self.assertTrue(data.endswith(parent_supervisor._BoundedOutputCapture._MARKER))
        self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)

    def test_bounded_ssh_probe_classifies_only_fixed_labels(self):
        self.assertEqual(parent_supervisor._ssh_failure_label(
            b"Host key verification failed.\n", 255, timed_out=False,
            forward_connected=True), "SSH_HOST_KEY_REJECTED")
        self.assertEqual(parent_supervisor._ssh_failure_label(
            b"Permission denied (publickey).\n", 255, timed_out=False,
            forward_connected=True), "SSH_AUTH_REJECTED")
        self.assertEqual(parent_supervisor._ssh_failure_label(
            b"Connection refused\n", 255, timed_out=False,
            forward_connected=True), "GUEST_SSH_SERVICE_UNAVAILABLE")
        self.assertEqual(parent_supervisor._ssh_failure_label(
            b"guest-provided text", 255, timed_out=False,
            forward_connected=False), "SSH_TRANSPORT_OR_HANDSHAKE_FAILED")

    def test_parent_pinned_known_hosts_accepts_exact_server_key_and_rejects_substitute(self):
        ssh = shutil.which("ssh")
        sshd = shutil.which("sshd")
        keygen = shutil.which("ssh-keygen")
        if not all((ssh, sshd, keygen)):
            self.skipTest("local OpenSSH tools unavailable")
        root = pathlib.Path(self.temp.name) / "parent-ssh-pin-integration"
        root.mkdir(mode=0o700)
        client_key, host_key, substitute_key = (root / "client", root / "host", root / "substitute")
        for key in (client_key, host_key, substitute_key):
            subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        authorized_keys = root / "authorized_keys"
        authorized_keys.write_text(client_key.with_suffix(".pub").read_text())
        authorized_keys.chmod(0o600)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()
        config = root / "sshd_config"
        config.write_text(
            f"Port {port}\nListenAddress 127.0.0.1\nHostKey {host_key}\n"
            f"PidFile {root / 'sshd.pid'}\nAuthorizedKeysFile {authorized_keys}\n"
            f"StrictModes no\nUsePAM no\nPasswordAuthentication no\n"
            f"KbdInteractiveAuthentication no\nPubkeyAuthentication yes\n"
            f"PermitRootLogin {'yes' if os.getuid() == 0 else 'no'}\n"
            f"AllowUsers {pwd.getpwuid(os.getuid()).pw_name}\nUseDNS no\nLogLevel QUIET\n")
        config.chmod(0o600)
        checked = subprocess.run([sshd, "-t", "-f", str(config)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 timeout=5, check=False)
        if checked.returncode:
            self.skipTest("local sshd cannot validate isolated test configuration")
        server = subprocess.Popen([sshd, "-D", "-e", "-f", str(config)],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, close_fds=True,
                                  start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    self.skipTest("isolated test sshd exited during startup")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.02)
            else:
                self.skipTest("isolated test sshd did not become ready")

            directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                parent_supervisor._write_ssh_connection(
                    parent_state, directory_fd, port, host_key.with_suffix(".pub").read_bytes())
            finally:
                os.close(directory_fd)
            known_hosts = root / "known_hosts"

            def connect():
                return subprocess.run(
                    [ssh, "-F", "/dev/null", "-T", "-p", str(port), "-i", str(client_key),
                     "-o", f"UserKnownHostsFile={known_hosts}", "-o", "GlobalKnownHostsFile=/dev/null",
                     "-o", "StrictHostKeyChecking=yes", "-o", "HostKeyAlgorithms=ssh-ed25519",
                     "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                     "-o", "BatchMode=yes", "-o", "ConnectTimeout=2",
                     f"{pwd.getpwuid(os.getuid()).pw_name}@127.0.0.1", "true"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    close_fds=True, timeout=5, check=False)

            accepted = connect()
            self.assertEqual(accepted.returncode, 0, accepted.stderr.decode("utf-8", "replace"))

            directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                parent_supervisor._write_ssh_connection(
                    parent_state, directory_fd, port,
                    substitute_key.with_suffix(".pub").read_bytes())
            finally:
                os.close(directory_fd)
            rejected = connect()
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn(b"Host key verification failed", rejected.stderr)
        finally:
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=2)

    def test_production_ssh_plan_works_with_openssh_descriptor_closing(self):
        sshd = shutil.which("sshd")
        keygen = shutil.which("ssh-keygen")
        if not sshd or not keygen:
            self.skipTest("local OpenSSH tools unavailable")
        root = pathlib.Path(self.temp.name) / "production-ssh-fd"
        root.mkdir(mode=0o700)
        control_key, host_key, substitute_key = (root / "control", root / "host", root / "substitute")
        for key in (control_key, host_key, substitute_key):
            subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)

        # Populate the same protected parent files validated by parent_ssh_plan().
        assets = {
            "id_vm_control": control_key.read_bytes(),
            "id_vm_control.pub": control_key.with_suffix(".pub").read_bytes(),
            "ssh_host_ed25519_key": host_key.read_bytes(),
            "ssh_host_ed25519_key.pub": host_key.with_suffix(".pub").read_bytes(),
            "user-data": b"#cloud-config\n{}\n",
            "meta-data": b"instance-id: production-ssh-test\n",
            "seed.iso": b"production-ssh-test-seed",
        }
        for name, data in assets.items():
            path = self.state_dir / name
            path.write_bytes(data)
            path.chmod(0o600)
        fd = parent_state._open_directory_chain(create=False, home_override=self.home)
        try:
            parent = parent_state._read_json(fd, "parent.json")
            parent["ssh_identity"] = {
                "seed_sha256": hashlib.sha256(assets["seed.iso"]).hexdigest(),
                "control_private_sha256": hashlib.sha256(assets["id_vm_control"]).hexdigest(),
                "control_public_sha256": hashlib.sha256(assets["id_vm_control.pub"]).hexdigest(),
                "host_private_sha256": hashlib.sha256(assets["ssh_host_ed25519_key"]).hexdigest(),
                "host_public_sha256": hashlib.sha256(assets["ssh_host_ed25519_key.pub"]).hexdigest(),
            }
            parent_state._atomic_json(fd, "parent.json", parent)
        finally:
            os.close(fd)

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()
        authorized_keys = root / "authorized_keys"
        authorized_keys.write_bytes(assets["id_vm_control.pub"])
        authorized_keys.chmod(0o600)
        config = root / "sshd_config"
        config.write_text(
            f"Port {port}\nListenAddress 127.0.0.1\nHostKey {host_key}\n"
            f"PidFile {root / 'sshd.pid'}\nAuthorizedKeysFile {authorized_keys}\n"
            "StrictModes no\nUsePAM no\nPasswordAuthentication no\n"
            "KbdInteractiveAuthentication no\nPubkeyAuthentication yes\n"
            f"PermitRootLogin {'yes' if os.getuid() == 0 else 'no'}\n"
            f"AllowUsers {pwd.getpwuid(os.getuid()).pw_name}\nUseDNS no\nLogLevel QUIET\n")
        config.chmod(0o600)
        if subprocess.run([sshd, "-t", "-f", str(config)], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=5, check=False).returncode:
            self.skipTest("local sshd cannot validate isolated test configuration")
        server = subprocess.Popen([sshd, "-D", "-e", "-f", str(config)],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        supervisor = None
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and server.poll() is None:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.02)
            else:
                self.skipTest("isolated test sshd did not become ready")
            fd = parent_state._open_directory_chain(create=False, home_override=self.home)
            try:
                parent_supervisor._write_ssh_connection(
                    parent_state, fd, port, assets["ssh_host_ed25519_key.pub"])
            finally:
                os.close(fd)
            supervisor, _run_id = self.spawn_supervisor()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and parent_state.read_status(home=self.home)["status"] != "STARTING":
                time.sleep(0.01)
            fd = parent_state._open_directory_chain(create=False, home_override=self.home)
            try:
                run = parent_state._read_json(fd, "active-run.json")
                run.update({"phase": "RUNNING", "qemu": {
                    "schema": 1, "pid": os.getpid(), "uid": os.getuid(),
                    "host_boot_id": pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                    "start_time": "1", "exe": "/usr/bin/qemu-system-x86_64",
                    "argv": ["/usr/bin/qemu-system-x86_64"],
                }})
                parent_state._atomic_json(fd, "active-run.json", run)
            finally:
                os.close(fd)

            with mock.patch.object(parent_state, "_recorded_process_status", return_value="MATCH"):
                argv, descriptors = parent_state.parent_ssh_plan(
                    home=self.home, allow_starting=True,
                    remote_command=parent_state.PARENT_READY_COMMAND, tty=False)
            try:
                # The local sshd account is the test runner; production remains mester.
                argv[argv.index("mester@127.0.0.1")] = (
                    f"{pwd.getpwuid(os.getuid()).pw_name}@127.0.0.1")
                # Use the production argv and retained descriptor lifetime, not normal paths.
                result = parent_supervisor._run_bounded_ssh_attempt(argv, descriptors, timeout_seconds=5)
                # 43 is the fixture's expected guest-side missing-launcher check;
                # reaching it proves the pinned SSH handshake and authentication passed.
                self.assertEqual(result[0], 43, result[1].decode("utf-8", "replace"))
                self.assertFalse(result[2])

                # Keep the trusted parent pin unchanged and substitute the endpoint key.
                server.terminate()
                server.wait(timeout=2)
                config.write_text(config.read_text().replace(f"HostKey {host_key}\n",
                                                              f"HostKey {substitute_key}\n"))
                server = subprocess.Popen([sshd, "-D", "-e", "-f", str(config)],
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, close_fds=True,
                                          start_new_session=True)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and server.poll() is None:
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                            break
                    except OSError:
                        time.sleep(0.02)
                else:
                    self.fail("substituted test sshd did not become ready")
                rejected = parent_supervisor._run_bounded_ssh_attempt(argv, descriptors, timeout_seconds=5)
                self.assertEqual(rejected[0], 255)
                self.assertIn(b"Host key verification failed", rejected[1])
            finally:
                for descriptor in descriptors:
                    os.close(descriptor)

            server.terminate()
            server.wait(timeout=2)
            config.write_text(config.read_text().replace(f"HostKey {substitute_key}\n",
                                                          f"HostKey {host_key}\n"))
            server = subprocess.Popen([sshd, "-D", "-e", "-f", str(config)],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, close_fds=True,
                                      start_new_session=True)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and server.poll() is None:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.02)
            else:
                self.fail("restored test sshd did not become ready")

            # Exercise a complete shell command round-trip over the exact production
            # pinned SSH plan/runner under a real controlling PTY.  The command text
            # does not contain its expected output, so PTY echo alone cannot pass.
            with mock.patch.object(parent_state, "_recorded_process_status", return_value="MATCH"):
                shell_argv, shell_fds = parent_state.parent_ssh_plan(home=self.home)
            shell_argv[shell_argv.index("mester@127.0.0.1")] = (
                f"{pwd.getpwuid(os.getuid()).pw_name}@127.0.0.1")
            master_fd, slave_fd = pty.openpty()
            shell_runner = r"""
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location('parent_state', sys.argv[1])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
raise SystemExit(module._run_interactive_ssh(json.loads(sys.argv[2])))
"""
            shell_proc = subprocess.Popen(
                [sys.executable, "-I", "-c", shell_runner,
                 str(ROOT / "lib/vm/parent-state.py"), json.dumps(shell_argv)],
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, close_fds=True)
            os.close(slave_fd)
            shell_output = bytearray()
            try:
                os.write(master_fd, b"echo $((19+23))\n")
                deadline = time.monotonic() + 8
                while (time.monotonic() < deadline
                       and b"42" not in shell_output.splitlines()):
                    ready, _, _ = select.select([master_fd], [], [], 0.1)
                    if ready:
                        try:
                            chunk = os.read(master_fd, 4096)
                        except OSError:
                            break
                        if not chunk:
                            break
                        shell_output.extend(chunk)
                    if shell_proc.poll() is not None:
                        break
                self.assertIn(b"42", shell_output.splitlines(),
                              f"interactive shell did not execute input; PTY output={bytes(shell_output)!r}")
                os.write(master_fd, b"exit\n")
                self.assertEqual(shell_proc.wait(timeout=5), 0,
                                 bytes(shell_output).decode("utf-8", "replace"))
            finally:
                if shell_proc.poll() is None:
                    shell_proc.terminate()
                    try:
                        shell_proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        shell_proc.kill()
                        shell_proc.wait(timeout=2)
                os.close(master_fd)
                for descriptor in shell_fds:
                    os.close(descriptor)

            with mock.patch.object(parent_state, "_recorded_process_status", return_value="MATCH"):
                interactive, interactive_fds = parent_state.parent_ssh_plan(
                    home=self.home, attach_opencode=True)
            try:
                interactive[interactive.index("mester@127.0.0.1")] = (
                    f"{pwd.getpwuid(os.getuid()).pw_name}@127.0.0.1")
                result = parent_state._run_interactive_ssh(interactive)
                self.assertNotEqual(result, 255)
            finally:
                for descriptor in interactive_fds:
                    os.close(descriptor)

            # Exercise the production interactive runner's signal forwarding with a
            # long-lived real OpenSSH child, then verify it was reaped before return.
            known_hosts = root / "signal-known-hosts"
            host_fields = assets["ssh_host_ed25519_key.pub"].decode().split()
            known_hosts.write_text(f"[127.0.0.1]:{port} {host_fields[0]} {host_fields[1]}\n")
            known_hosts.chmod(0o600)
            signal_argv = ["/usr/bin/ssh", "-F", "/dev/null", "-N", "-T", "-p", str(port),
                           "-i", str(control_key), "-o", f"UserKnownHostsFile={known_hosts}",
                           "-o", "GlobalKnownHostsFile=/dev/null", "-o", "StrictHostKeyChecking=yes",
                           "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                           "-o", "PasswordAuthentication=no", "-o", "LogLevel=QUIET",
                           f"{pwd.getpwuid(os.getuid()).pw_name}@127.0.0.1"]
            ssh_pid_file = root / "interactive-ssh.pid"
            runner = r"""
import importlib.util, json, pathlib, sys
spec = importlib.util.spec_from_file_location('parent_state', sys.argv[1])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
original = module.subprocess.Popen
def tracked(*args, **kwargs):
    child = original(*args, **kwargs)
    pathlib.Path(sys.argv[3]).write_text(str(child.pid))
    return child
module.subprocess.Popen = tracked
raise SystemExit(module._run_interactive_ssh(json.loads(sys.argv[2])))
"""
            runner_proc = subprocess.Popen(
                [sys.executable, "-I", "-c", runner, str(ROOT / "lib/vm/parent-state.py"),
                 json.dumps(signal_argv), str(ssh_pid_file)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not ssh_pid_file.exists() and runner_proc.poll() is None:
                    time.sleep(0.01)
                self.assertTrue(ssh_pid_file.exists(), "interactive SSH child did not start")
                ssh_pid = int(ssh_pid_file.read_text())
                runner_proc.send_signal(signal.SIGTERM)
                self.assertEqual(runner_proc.wait(timeout=5), 128 + signal.SIGTERM)
            finally:
                if runner_proc.poll() is None:
                    runner_proc.terminate()
                    try:
                        runner_proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        runner_proc.kill()
                        runner_proc.wait(timeout=2)
            with self.assertRaises(ProcessLookupError):
                os.kill(ssh_pid, 0)

            server.terminate()
            server.wait(timeout=2)
            config.write_text(config.read_text().replace(f"HostKey {host_key}\n",
                                                          f"HostKey {substitute_key}\n"))
            server = subprocess.Popen([sshd, "-D", "-e", "-f", str(config)],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, close_fds=True,
                                      start_new_session=True)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and server.poll() is None:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.02)
            else:
                self.fail("substituted interactive test sshd did not become ready")
            with mock.patch.object(parent_state, "_recorded_process_status", return_value="MATCH"):
                untrusted_interactive, untrusted_fds = parent_state.parent_ssh_plan(
                    home=self.home, attach_opencode=True)
            try:
                untrusted_interactive[untrusted_interactive.index("mester@127.0.0.1")] = (
                    f"{pwd.getpwuid(os.getuid()).pw_name}@127.0.0.1")
                self.assertEqual(parent_state._run_interactive_ssh(untrusted_interactive), 255)
            finally:
                for descriptor in untrusted_fds:
                    os.close(descriptor)
        finally:
            if supervisor is not None:
                self.cleanup_test_supervisor(supervisor)
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=2)

    def test_lab_vm_worker_uses_separate_script_fd_and_preserves_terminal_stdin(self):
        sudo = shutil.which("sudo")
        ci = os.environ.get("GITHUB_ACTIONS") == "true"

        def unavailable(message):
            if ci:
                self.fail(f"CI protected launcher fixture prerequisite unavailable: {message}")
            self.skipTest(f"local protected launcher fixture prerequisite unavailable: {message}")

        if not sudo or subprocess.run([sudo, "-n", "true"], check=False).returncode:
            unavailable("non-interactive sudo is required")
        if (not PROTECTED_FIXTURE_ROOT.is_absolute()
                or str(PROTECTED_FIXTURE_ROOT) != os.path.normpath(str(PROTECTED_FIXTURE_ROOT))):
            self.fail("protected fixture root must be an absolute normalized path")
        for path in reversed((PROTECTED_FIXTURE_ROOT, *PROTECTED_FIXTURE_ROOT.parents)):
            try:
                info = path.lstat()
            except OSError as error:
                unavailable(f"protected ancestor is unavailable: {path} ({error})")
            if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                    or not info.st_mode & stat.S_IXOTH):
                unavailable(f"ancestor is not a traversable protected root directory: {path}")

        with tempfile.TemporaryDirectory() as source_temp:
            source_root = pathlib.Path(source_temp)
            fixture_files = [
                "lab-vm",
                "lib/vm/host-launcher.sh", "lib/vm/cloud-init.sh", "lib/vm/proxy.py",
                "lib/vm/bounded-log.py", "lib/vm/executable-policy.py",
                "lib/vm/browser-auth-session.py", "lib/vm/virtiofs-socket.py",
                "lib/vm/guest-user-dirs.py", "lib/vm/parent-state.py",
                "lib/vm/parent-launcher.py", "lib/vm/parent-gate.py",
                "lib/vm/parent-supervisor.py", "lib/vm/parent-qemu.py",
                "lib/vm/parent-provision.py",
                "agent/opencode/plugins/little-mester-exit-confirm/package.json",
                "agent/opencode/plugins/little-mester-exit-confirm/tui.tsx",
            ]
            for relative in fixture_files:
                destination = source_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if relative == "lab-vm":
                    shutil.copyfile(ROOT / relative, destination)
                    destination.chmod(0o755)
                elif relative == "lib/vm/parent-state.py":
                    destination.write_text(
                        "import os, subprocess, sys\n"
                        "fds = ','.join('1' if os.isatty(fd) else '0' for fd in (0, 1, 2))\n"
                        "term = os.environ.get('TERM', '<unset>')\n"
                        "try: os.fstat(3); script_fd = 'open'\n"
                        "except OSError: script_fd = 'closed'\n"
                        "print(f'LAUNCHER-FDS:{fds}:{script_fd}:TERM={term}', flush=True)\n"
                        "child = subprocess.Popen([sys.executable, '-c', "
                        "\"import sys; line=sys.stdin.readline(); "
                        "print('42' if line.strip() == 'echo $((19+23))' else 'BAD-INPUT', flush=True)\"])\n"
                        "raise SystemExit(child.wait())\n")
                    destination.chmod(0o644)
                else:
                    destination.write_text("fixture dependency\n")
                    destination.chmod(0o644)

            template = str(PROTECTED_FIXTURE_ROOT / "lm-vm-ssh-pty.XXXXXX")
            try:
                install_result = subprocess.run(
                    [sudo, "-n", "/usr/bin/mktemp", "-d", template],
                    check=True, capture_output=True, text=True, timeout=5)
            except (OSError, subprocess.SubprocessError) as error:
                self.fail(f"could not create protected launcher fixture under {PROTECTED_FIXTURE_ROOT}: {error}")
            install_root = pathlib.Path(install_result.stdout.strip())
            try:
                self.assertEqual(install_root.parent, PROTECTED_FIXTURE_ROOT)
                subprocess.run([sudo, "-n", "/usr/bin/chmod", "0755", str(install_root)],
                               check=True, timeout=5)
                archive = io.BytesIO()
                with tarfile.open(fileobj=archive, mode="w") as bundle:
                    for relative in fixture_files:
                        bundle.add(source_root / relative, arcname=relative)
                subprocess.run(
                    [sudo, "-n", "/usr/bin/tar", "--no-same-owner", "--no-same-permissions",
                     "-xf", "-", "-C", str(install_root)],
                    input=archive.getvalue(), check=True, timeout=5)
                subprocess.run([sudo, "-n", "/usr/bin/chown", "-R", "0:0", str(install_root)],
                               check=True, timeout=5)
                protected_paths = list(install_root.parents) + [install_root]
                for directory, child_dirs, child_files in os.walk(install_root):
                    protected_paths.extend(pathlib.Path(directory) / name
                                           for name in child_dirs + child_files)
                for path in protected_paths:
                    info = path.lstat()
                    self.assertFalse(stat.S_ISLNK(info.st_mode), f"fixture path is a symlink: {path}")
                    self.assertEqual(info.st_uid, 0, f"fixture path is not root-owned: {path}")
                    self.assertFalse(info.st_mode & (stat.S_IWGRP | stat.S_IWOTH),
                                     f"fixture path is group/world writable: {path}")
                    if path in install_root.parents or path.is_dir():
                        self.assertTrue(stat.S_ISDIR(info.st_mode), f"fixture directory type is invalid: {path}")
                        self.assertTrue(info.st_mode & stat.S_IXOTH,
                                        f"fixture directory is not traversable: {path}")
                    else:
                        self.assertTrue(stat.S_ISREG(info.st_mode), f"fixture file type is invalid: {path}")

                master_fd, slave_fd = pty.openpty()
                child = subprocess.Popen(
                    [str(install_root / "lab-vm"), "parent", "ssh"],
                    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, close_fds=True,
                    env={**os.environ, "TERM": "xterm-256color"})
                os.close(slave_fd)
                output = bytearray()
                try:
                    os.write(master_fd, b"echo $((19+23))\n")
                    deadline = time.monotonic() + 8
                    while (time.monotonic() < deadline
                           and b"42" not in output.splitlines()):
                        ready, _, _ = select.select([master_fd], [], [], 0.1)
                        if ready:
                            try:
                                output.extend(os.read(master_fd, 4096))
                            except OSError:
                                break
                        if child.poll() is not None:
                            break
                    self.assertIn(b"LAUNCHER-FDS:1,1,1:closed:TERM=xterm-256color\r\n", output,
                                  f"launcher descriptors were not preserved/closed correctly: {bytes(output)!r}")
                    self.assertIn(b"42", output.splitlines(),
                                  f"interactive output did not reach the PTY: {bytes(output)!r}")
                    self.assertEqual(child.wait(timeout=3), 0)
                finally:
                    if child.poll() is None:
                        child.terminate()
                        try:
                            child.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(timeout=2)
                    os.close(master_fd)

                refused = subprocess.run(
                    [str(install_root / "lab-vm"), "parent", "ssh"],
                    env={**os.environ, "TERM": "dumb"},
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, timeout=5, check=False)
                self.assertEqual(refused.returncode, 2, refused.stdout)
                self.assertIn("requires a host-supported TERM with clear capability", refused.stdout)
                self.assertNotIn("LAUNCHER-FDS", refused.stdout)
            finally:
                if (install_root.parent == PROTECTED_FIXTURE_ROOT
                        and install_root.name.startswith("lm-vm-ssh-pty.")):
                    subprocess.run([sudo, "-n", "/usr/bin/rm", "-rf", "--", str(install_root)],
                                   check=True, timeout=5)
                else:
                    self.fail(f"refusing to clean unexpected protected fixture path: {install_root}")

    def test_bounded_ssh_probe_caps_stderr_and_reaps_timeout_child(self):
        argv = [sys.executable, "-c", "import sys; sys.stderr.write('x' * 10000); sys.exit(255)"]
        returncode, stderr, timed_out = parent_supervisor._run_bounded_ssh_attempt(
            argv, [], timeout_seconds=2, output_limit=128)
        self.assertEqual(returncode, 255)
        self.assertEqual(len(stderr), 128)
        self.assertFalse(timed_out)

        slow = [sys.executable, "-c", "import time; time.sleep(5)"]
        returncode, stderr, timed_out = parent_supervisor._run_bounded_ssh_attempt(
            slow, [], timeout_seconds=0.05, output_limit=128)
        self.assertEqual(returncode, -9)
        self.assertEqual(stderr, b"")
        self.assertTrue(timed_out)

    def test_bounded_ssh_probe_deadline_covers_closed_stderr_and_inherited_writer(self):
        closed = [sys.executable, "-c", "import os,time; os.close(2); time.sleep(0.2)"]
        started = time.monotonic()
        cpu_started = time.process_time()
        _returncode, _stderr, timed_out = parent_supervisor._run_bounded_ssh_attempt(
            closed, [], timeout_seconds=1, output_limit=128)
        self.assertFalse(timed_out)
        self.assertGreaterEqual(time.monotonic() - started, 0.15)
        self.assertLess(time.process_time() - cpu_started, 0.1)

        inherited = [sys.executable, "-c",
                     "import os,time; pid=os.fork(); os._exit(0) if pid else None; time.sleep(0.3)"]
        started = time.monotonic()
        _returncode, _stderr, timed_out = parent_supervisor._run_bounded_ssh_attempt(
            inherited, [], timeout_seconds=0.05, output_limit=128)
        self.assertTrue(timed_out)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_completed_qemu_log_capture_error_does_not_block_verified_stop(self):
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        capture = parent_supervisor._BoundedOutputCapture(read_fd, os.open("/dev/full", os.O_WRONLY))
        capture.start()
        os.write(write_fd, b"bounded private log test\n")
        os.close(write_fd)
        self.assertTrue(capture.finish())
        self.assertIsNotNone(capture.error)
        parent_supervisor._CHILD_LOG_CAPTURES["fixture"] = capture
        try:
            with mock.patch("builtins.print"):
                self.assertTrue(parent_supervisor._finish_child_log_capture("fixture"))
            self.assertNotIn("fixture", parent_supervisor._CHILD_LOG_CAPTURES)
        finally:
            parent_supervisor._CHILD_LOG_CAPTURES.pop("fixture", None)

    def spawn_supervisor(self, *, send_commit=True):
        run_id = "0123456789abcdef0123456789abcdef"
        handoff_read, handoff_write = os.pipe2(os.O_CLOEXEC)
        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            args = [sys.executable, "-I", str(SUPERVISOR), "--parent-id", "default", "--run-id", run_id,
                    "--lock-fd", str(lock_fd), "--handoff-fd", str(handoff_read),
                    "--state-home", str(self.home)]
            proc = subprocess.Popen(args, pass_fds=(lock_fd, handoff_read), close_fds=True,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
            os.close(lock_fd)  # Drop this open-file-description reference; do not LOCK_UN.
            if send_commit:
                os.write(handoff_write, b"COMMIT\n")
            os.close(handoff_write)
        os.close(handoff_read)
        return proc, run_id

    def spawn_recovery_target(self, directory_fd, role):
        target = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, close_fds=True)
        try:
            deadline = time.monotonic() + 2
            while True:
                try:
                    target_identity = parent_state.observe_process(target.pid)
                    if (target_identity["exe"] == os.path.realpath(sys.executable)
                            and target_identity["argv"] == [sys.executable, "-c", "import time; time.sleep(30)"]):
                        break
                except parent_state.Refused:
                    pass
                if time.monotonic() >= deadline:
                    raise AssertionError("recovery target identity did not stabilize")
                time.sleep(0.01)
        except BaseException:
            if target.poll() is None:
                target.kill()
                target.wait(timeout=3)
            raise
        target_name = f"child-{role}.target.json"
        identity_name = f"child-{role}.identity.json"
        token = {"qemu": "1", "proxy": "2", "metrics": "3"}[role] * 32
        gate_argv = [os.path.realpath(sys.executable), "-I",
                     str((ROOT / "lib/vm/parent-gate.py").resolve()),
                     "--release-fd", "10", "--token", token,
                     "--target", target_name, "--identity", identity_name,
                     "--state-dir-fd", "20", "--disk-fd", "-1"]
        gate_identity = {**target_identity, "exe": os.path.realpath(sys.executable),
                         "argv": gate_argv}
        transition = parent_state.create_exec_transition(
            gate_identity, target_identity["exe"], target_identity["argv"],
            release_token=token, target_path=target_name,
            identity_path=identity_name)
        parent_state._atomic_json(directory_fd, target_name,
                                  {"executable": target_identity["exe"],
                                   "argv": target_identity["argv"],
                                   "fd_contracts": []})
        parent_state._atomic_json(directory_fd, identity_name, transition)
        return target, transition

    def test_handoff_records_exact_supervisor_and_retains_lock_until_owner_stop(self):
        proc, run_id = self.spawn_supervisor()
        reaper = threading.Thread(target=proc.wait)
        reaper.start()
        try:
            deadline = time.monotonic() + 3
            status = parent_state.read_status(home=self.home)
            while time.monotonic() < deadline and status["status"] == "AMBIGUOUS" and proc.poll() is None:
                time.sleep(0.01)
                status = parent_state.read_status(home=self.home)
            self.assertEqual(status["status"], "STARTING")
            self.assertEqual(status["run_id"], run_id)
            with self.assertRaises(parent_state.Refused):
                with parent_state.lifecycle_lock(home=self.home):
                    self.fail("supervisor did not retain exclusive lifecycle lock")
            self.assertFalse((self.state_dir / "supervisor.sock").exists())
            result = parent_state.request_supervisor_stop(home=self.home)
            self.assertTrue(result["disk_retained"])
            reaper.join(timeout=3)
            self.assertFalse(reaper.is_alive())
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "STOPPED")
            self.assertTrue((self.state_dir / "disk.qcow2").exists())
            with parent_state.lifecycle_lock(home=self.home):
                pass
        finally:
            self.cleanup_test_supervisor(proc)

    def test_parent_stop_cli_helper_requests_only_verified_starting_supervisor(self):
        proc, run_id = self.spawn_supervisor()
        reaper = threading.Thread(target=proc.wait)
        reaper.start()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and parent_state.read_status(home=self.home)["status"] != "STARTING":
                time.sleep(0.01)
            result = parent_state.request_supervisor_stop(home=self.home)
            self.assertEqual(result["status"], "STOPPED")
            self.assertEqual(result["run_id"], run_id)
            self.assertTrue(result["disk_retained"])
            reaper.join(timeout=3)
            self.assertFalse(reaper.is_alive())
            self.assertEqual(proc.returncode, 0)
            self.assertTrue((self.state_dir / "disk.qcow2").exists())
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "STOPPED")
        finally:
            self.cleanup_test_supervisor(proc)

    def test_owner_up_stop_reaps_its_supervisor_before_accepting_procfs_stop_state(self):
        proc, _run_id = self.spawn_supervisor()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and parent_state.read_status(home=self.home)["status"] != "STARTING":
                time.sleep(0.01)
            result = parent_state.request_supervisor_stop(
                home=self.home, timeout_seconds=3, reap_supervisor=proc.poll)
            self.assertEqual(result["status"], "STOPPED")
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "STOPPED")
            self.assertTrue((self.state_dir / "disk.qcow2").exists())
        finally:
            self.cleanup_test_supervisor(proc)

    def test_owner_up_stop_reaps_preexisting_supervisor_zombie_before_status_validation(self):
        proc, _run_id = self.spawn_supervisor()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if parent_state.read_status(home=self.home)["status"] == "STARTING":
                    break
                time.sleep(0.01)
            else:
                self.fail("supervisor did not publish STARTING")

            proc.send_signal(signal.SIGTERM)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    fields = pathlib.Path(f"/proc/{proc.pid}/stat").read_text().rsplit(")", 1)[1].split()
                except FileNotFoundError:
                    self.fail("supervisor was reaped before the zombie race was observed")
                if fields[0] == "Z":
                    break
                time.sleep(0.01)
            else:
                self.fail("supervisor did not reach the expected unreaped-child state")

            self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")
            result = parent_state.request_supervisor_stop(
                home=self.home, timeout_seconds=3, reap_supervisor=proc.poll)
            self.assertEqual(result["status"], "STOPPED")
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "STOPPED")
            self.assertTrue((self.state_dir / "disk.qcow2").exists())
        finally:
            self.cleanup_test_supervisor(proc)

    def test_parent_stop_waits_out_one_transient_ambiguous_procfs_exit_observation(self):
        proc, _run_id = self.spawn_supervisor()
        reaper = threading.Thread(target=proc.wait)
        reaper.start()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and parent_state.read_status(home=self.home)["status"] != "STARTING":
                time.sleep(0.01)
            actual_read_status = parent_state.read_status
            calls = 0

            def transient_status(*, home=None):
                nonlocal calls
                calls += 1
                if calls == 2:
                    return {"status": "AMBIGUOUS", "parent_id": "default"}
                return actual_read_status(home=home)

            with mock.patch.object(parent_state, "read_status", side_effect=transient_status):
                result = parent_state.request_supervisor_stop(home=self.home)
            self.assertEqual(result["status"], "STOPPED")
            self.assertGreaterEqual(calls, 3)
            self.assertTrue((self.state_dir / "disk.qcow2").exists())
            reaper.join(timeout=3)
            self.assertFalse(reaper.is_alive())
        finally:
            self.cleanup_test_supervisor(proc)

    def test_parent_stop_refuses_unverified_supervisor_identity_without_signal(self):
        proc, _run_id = self.spawn_supervisor()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and parent_state.read_status(home=self.home)["status"] != "STARTING":
                time.sleep(0.01)
            fd = parent_state._open_directory_chain(create=False, home_override=self.home)
            try:
                run = parent_state._read_json(fd, "active-run.json")
                run["supervisor"]["start_time"] = str(int(run["supervisor"]["start_time"]) + 1)
                parent_state._atomic_json(fd, "active-run.json", run)
            finally:
                os.close(fd)
            with self.assertRaises(parent_state.Refused):
                parent_state.request_supervisor_stop(home=self.home)
            self.assertIsNone(proc.poll())
            self.assertTrue((self.state_dir / "disk.qcow2").exists())
        finally:
            self.cleanup_test_supervisor(proc)

    def test_parent_stop_refuses_when_pidfd_api_is_unavailable(self):
        proc, _run_id = self.spawn_supervisor()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and parent_state.read_status(home=self.home)["status"] != "STARTING":
                time.sleep(0.01)
            with mock.patch.object(parent_state.os, "pidfd_open", side_effect=AttributeError):
                with self.assertRaisesRegex(parent_state.Refused, "pidfd"):
                    parent_state.request_supervisor_stop(home=self.home)
            self.assertIsNone(proc.poll())
            self.assertTrue((self.state_dir / "disk.qcow2").exists())
        finally:
            self.cleanup_test_supervisor(proc)

    def test_sigkill_supervisor_releases_kernel_lock_but_reports_ambiguous_without_recovery(self):
        proc, _run_id = self.spawn_supervisor()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and parent_state.read_status(home=self.home)["status"] != "STARTING":
                time.sleep(0.01)
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "STARTING")
            proc.kill()
            self.assertNotEqual(proc.wait(timeout=3), 0)
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")
            preview = parent_state.recovery_preview(home=self.home)
            self.assertEqual(preview["status"], "AMBIGUOUS")
            self.assertEqual(preview["action"], "none")
            self.assertTrue((self.state_dir / "disk.qcow2").exists())
            with parent_state.lifecycle_lock(home=self.home):
                pass
        finally:
            self.cleanup_test_supervisor(proc)

    def test_supervisor_signal_does_not_mark_ambiguous_running_record_stopped(self):
        proc, _run_id = self.spawn_supervisor()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and parent_state.read_status(home=self.home)["status"] != "STARTING":
                time.sleep(0.01)
            fd = parent_state._open_directory_chain(create=False, home_override=self.home)
            try:
                run = parent_state._read_json(fd, "active-run.json")
                run.update({"phase": "RUNNING", "qemu": {"untrusted": "record"}})
                parent_state._atomic_json(fd, "active-run.json", run)
            finally:
                os.close(fd)
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")
            proc.terminate()
            self.assertEqual(proc.wait(timeout=3), 2)
            result = parent_state.read_status(home=self.home)
            self.assertEqual(result["status"], "AMBIGUOUS")
            fd = parent_state._open_directory_chain(create=False, home_override=self.home)
            try:
                self.assertEqual(parent_state._read_json(fd, "active-run.json")["phase"], "RUNNING")
            finally:
                os.close(fd)
            self.assertTrue((self.state_dir / "disk.qcow2").exists())
        finally:
            self.cleanup_test_supervisor(proc)

    def test_missing_commit_does_not_record_supervisor_and_releases_lock(self):
        proc, _run_id = self.spawn_supervisor(send_commit=False)
        try:
            self.assertEqual(proc.wait(timeout=3), 2)
            self.assertFalse((self.state_dir / "active-run.json").exists())
            with parent_state.lifecycle_lock(home=self.home):
                pass
        finally:
            self.cleanup_test_supervisor(proc)

    def test_supervisor_gated_spawn_records_exact_same_pid_target_before_release(self):
        marker = pathlib.Path(self.temp.name) / "gated-target.started"
        replacement_marker = pathlib.Path(self.temp.name) / "must-not-replace"
        executable = os.path.realpath(sys.executable)
        code = f"import pathlib,time; pathlib.Path({str(marker)!r}).write_text('ready'); time.sleep(30)"
        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            transition = None
            try:
                transition = parent_supervisor._spawn_gated_target(
                    parent_state, lock_fd=lock_fd, role="qemu", executable=executable,
                    argv=[executable, "-c", code], home=str(self.home))
                self.assertEqual(parent_state.exec_transition_status(transition), "TARGET")
                self.assertEqual(transition["gate"]["exe"], executable)
                self.assertEqual(transition["target"]["exe"], executable)
                self.assertEqual(transition["gate"]["argv"][0], executable)
                self.assertEqual(transition["pid"], parent_state.observe_process(transition["pid"])["pid"])
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not marker.exists():
                    time.sleep(0.01)
                self.assertTrue(marker.exists())
                target_record = (self.state_dir / "child-qemu.target.json").read_bytes()
                identity_record = (self.state_dir / "child-qemu.identity.json").read_bytes()
                with self.assertRaises(parent_state.Refused):
                    parent_supervisor._spawn_gated_target(
                        parent_state, lock_fd=lock_fd, role="qemu", executable=executable,
                        argv=[executable, "-c", f"import pathlib; pathlib.Path({str(replacement_marker)!r}).touch()"],
                        home=str(self.home))
                self.assertEqual((self.state_dir / "child-qemu.target.json").read_bytes(), target_record)
                self.assertEqual((self.state_dir / "child-qemu.identity.json").read_bytes(), identity_record)
                self.assertEqual(parent_state.exec_transition_status(transition), "TARGET")
                self.assertFalse(replacement_marker.exists())
                with self.assertRaises(parent_state.Refused):
                    with parent_state.lifecycle_lock(home=self.home):
                        self.fail("gated target caused supervisor lock release")
            finally:
                self.cleanup_gated_child("qemu", transition)
            self.assertEqual(parent_state.exec_transition_status(transition), "STOPPED")

    def test_gated_spawn_refuses_lock_fd_alias_without_closing_callers_fd(self):
        executable = os.path.realpath(sys.executable)
        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            with self.assertRaises(parent_state.Refused):
                parent_supervisor._spawn_gated_target(
                    parent_state, lock_fd=lock_fd, role="qemu", executable=executable,
                    argv=[executable, "-c", "pass"], home=str(self.home), disk_fd=lock_fd)
            self.assertEqual(os.fstat(lock_fd).st_ino, (self.state_dir / "lifecycle.lock").stat().st_ino)

    def test_parent_seed_persists_control_and_host_identity_without_host_exports(self):
        calls = []
        public_by_private = {}
        real_run = subprocess.run

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if "-t" in argv:
                key_path = pathlib.Path(argv[-1])
                private = f"private:{key_path.name}".encode()
                public = f"ssh-ed25519 {('A' * 40)} {key_path.name}\n".encode()
                key_path.write_bytes(private)
                pathlib.Path(f"{key_path}.pub").write_bytes(public)
                public_by_private[private] = public
                return subprocess.CompletedProcess(argv, 0)
            if "-y" in argv:
                private = pathlib.Path(argv[-1]).read_bytes()
                return subprocess.CompletedProcess(argv, 0, stdout=public_by_private[private])
            if "-output" in argv:
                pathlib.Path(argv[argv.index("-output") + 1]).write_bytes(b"seed-iso-fixture")
                return subprocess.CompletedProcess(argv, 0)
            raise AssertionError(argv)

        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            with (mock.patch.object(parent_supervisor, "_protected_tool",
                                    side_effect=lambda name, _paths: "/usr/bin/ssh-keygen" if name == "ssh-keygen" else "/usr/bin/genisoimage"),
                  mock.patch.object(parent_supervisor.subprocess, "run", side_effect=fake_run)):
                host_public = parent_supervisor._prepare_parent_seed(
                    parent_state, lock_fd, str(self.home))
                seed = (self.state_dir / "seed.iso").read_bytes()
                self.assertEqual(seed, b"seed-iso-fixture")
                self.assertEqual(stat.S_IMODE((self.state_dir / "seed.iso").stat().st_mode), 0o600)
                user_data = (self.state_dir / "user-data").read_text()
                config = json.loads(user_data.split("\n", 1)[1])
                self.assertFalse(any("virtiofs" in json.dumps(entry) for entry in config.get("runcmd", [])))
                self.assertIn("ssh-ed25519", config["users"][0]["ssh_authorized_keys"][0])
                self.assertEqual(config["ssh_keys"]["ed25519_private"],
                                 (self.state_dir / "ssh_host_ed25519_key").read_text())
                sshd_config = next(item for item in config["write_files"]
                                   if item["path"].endswith("99-little-mester-parent.conf"))
                self.assertEqual(sshd_config["content"].count("\n"), 1)
                protected = {item["path"]: item for item in config["write_files"]}
                apt_proxy = protected["/etc/apt/apt.conf.d/90little-mester-parent-proxy"]["content"]
                self.assertIn('Acquire::http::Proxy "http://10.0.2.100:3128";', apt_proxy)
                self.assertIn('Acquire::https::Proxy "http://10.0.2.100:3128";', apt_proxy)
                opencode_config = json.loads(protected["/etc/opencode/opencode.json"]["content"])
                self.assertFalse(opencode_config["autoupdate"])
                self.assertEqual(opencode_config["share"], "disabled")
                self.assertEqual(protected["/etc/opencode/opencode.json"]["owner"], "root:root")
                plugin_path = "/etc/opencode/plugins/little-mester-exit-confirm/tui.tsx"
                self.assertEqual(protected[plugin_path]["owner"], "root:root")
                self.assertEqual(protected[plugin_path]["permissions"], "0644")
                expected_plugin = (ROOT / "agent/opencode/plugins/little-mester-exit-confirm/tui.tsx").read_bytes()
                self.assertEqual(base64.b64decode(protected[plugin_path]["content"], validate=True), expected_plugin)
                profile = protected["/etc/profile.d/little-mester-opencode.sh"]["content"]
                launcher = protected["/usr/local/bin/lm-opencode"]["content"]
                runner = protected["/usr/local/libexec/lm-opencode"]["content"]
                self.assertIn("OPENCODE_DISABLE_PROJECT_CONFIG=1", profile)
                self.assertIn("little-mester-exit-confirm", profile)
                self.assertIn("tmux new-session -A -s little-mester-opencode", launcher)
                self.assertIn("XDG_CONFIG_HOME=/etc", runner)
                self.assertIn("OPENCODE_CONFIG=/etc/opencode/opencode.json", runner)
                self.assertIn("HTTP_PROXY=http://10.0.2.100:3128", runner)
                self.assertIn("HTTPS_PROXY=http://10.0.2.100:3128", runner)
                self.assertIn("OPENCODE_DISABLE_PROJECT_CONFIG=1", runner)
                self.assertIn("exec /usr/local/bin/opencode", runner)
                cli_export = next(line for line in profile.splitlines()
                                  if line.startswith("export OPENCODE_CLI_CONFIG_CONTENT="))
                cli_content = json.loads(cli_export.split("=", 1)[1].strip("'"))
                self.assertEqual(cli_content["keybinds"], {"app.exit": "none", "prompt.clear": "none"})
                self.assertIn("test \"$(opencode --version)\" = 'opencode v2.2.5'",
                              config["runcmd"][0][-1])
                for script in (launcher, runner):
                    self.assertEqual(real_run(["/bin/sh", "-n"], input=script.encode(),
                                              check=False).returncode, 0)
                self.assertEqual(config["packages"], ["tmux", "nodejs", "npm"])
                self.assertIn("@opencode/cli@2.2.5", config["runcmd"][0][-1])
                port = 22345
                directory_fd = parent_state._open_directory_chain(create=False, home_override=self.home)
                try:
                    parent_supervisor._write_ssh_connection(parent_state, directory_fd, port, host_public)
                finally:
                    os.close(directory_fd)
                self.assertIn(f"[127.0.0.1]:{port} ssh-ed25519".encode(),
                              (self.state_dir / "known_hosts").read_bytes())
                before_calls = len(calls)
                same_public = parent_supervisor._prepare_parent_seed(
                    parent_state, lock_fd, str(self.home))
                self.assertEqual(same_public, host_public)
                self.assertEqual((self.state_dir / "seed.iso").read_bytes(), seed)
                self.assertEqual(len(calls[before_calls:]), 2)
                self.assertTrue(all("-y" in command for command in calls[before_calls:]))
                directory_fd = parent_state._open_directory_chain(create=False, home_override=self.home)
                try:
                    recorded_parent = parent_state._read_json(directory_fd, "parent.json")
                    parent_state._validate_seed_identity(directory_fd, recorded_parent)
                finally:
                    os.close(directory_fd)
                private_path = self.state_dir / "id_vm_control"
                original_private = private_path.read_bytes()
                private_path.unlink()
                directory_fd = parent_state._open_directory_chain(create=False, home_override=self.home)
                try:
                    with self.assertRaises(parent_state.Refused):
                        parent_state._validate_seed_identity(
                            directory_fd, parent_state._read_json(directory_fd, "parent.json"))
                finally:
                    os.close(directory_fd)
                private_path.write_bytes(original_private)
                os.chmod(private_path, 0o600)
                private_path.write_bytes(b"replacement private key")
                directory_fd = parent_state._open_directory_chain(create=False, home_override=self.home)
                try:
                    with self.assertRaises(parent_state.Refused):
                        parent_state._validate_seed_identity(
                            directory_fd, parent_state._read_json(directory_fd, "parent.json"))
                finally:
                    os.close(directory_fd)
                private_path.unlink()
                os.mkfifo(private_path, 0o600)
                directory_fd = parent_state._open_directory_chain(create=False, home_override=self.home)
                try:
                    with self.assertRaises(parent_state.Refused):
                        parent_state._validate_seed_identity(
                            directory_fd, parent_state._read_json(directory_fd, "parent.json"))
                finally:
                    os.close(directory_fd)
                (self.state_dir / "seed.iso").write_bytes(b"substituted seed")
                directory_fd = parent_state._open_directory_chain(create=False, home_override=self.home)
                try:
                    with self.assertRaises(parent_state.Refused):
                        parent_state._validate_seed_identity(
                            directory_fd, parent_state._read_json(directory_fd, "parent.json"))
                finally:
                    os.close(directory_fd)
                (self.state_dir / "seed.iso").write_bytes(seed)

    def test_parent_ssh_requires_running_identity_and_builds_pinned_nonforwarding_tty(self):
        files = {
            "id_vm_control": b"control-private",
            "id_vm_control.pub": b"ssh-ed25519 " + b"A" * 40 + b" control\n",
            "ssh_host_ed25519_key": b"host-private",
            "ssh_host_ed25519_key.pub": b"ssh-ed25519 " + b"B" * 40 + b" host\n",
            "user-data": b"#cloud-config\n{}\n", "meta-data": b"instance-id: test\n",
            "seed.iso": b"seed-fixture",
        }
        for name, data in files.items():
            (self.state_dir / name).write_bytes(data)
            os.chmod(self.state_dir / name, 0o600)
        fd = parent_state._open_directory_chain(create=False, home_override=self.home)
        try:
            parent = parent_state._read_json(fd, "parent.json")
        finally:
            os.close(fd)
        parent["ssh_identity"] = {
            "seed_sha256": hashlib.sha256(files["seed.iso"]).hexdigest(),
            "control_private_sha256": hashlib.sha256(files["id_vm_control"]).hexdigest(),
            "control_public_sha256": hashlib.sha256(files["id_vm_control.pub"]).hexdigest(),
            "host_private_sha256": hashlib.sha256(files["ssh_host_ed25519_key"]).hexdigest(),
            "host_public_sha256": hashlib.sha256(files["ssh_host_ed25519_key.pub"]).hexdigest(),
        }
        fd = parent_state._open_directory_chain(create=False, home_override=self.home)
        try:
            parent_state._atomic_json(fd, "parent.json", parent)
            parent_supervisor._write_ssh_connection(parent_state, fd, 23456, files["ssh_host_ed25519_key.pub"])
        finally:
            os.close(fd)

        proc, _run_id = self.spawn_supervisor()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and parent_state.read_status(home=self.home)["status"] != "STARTING":
                time.sleep(0.01)
            fd = parent_state._open_directory_chain(create=False, home_override=self.home)
            try:
                run = parent_state._read_json(fd, "active-run.json")
                run.update({"phase": "RUNNING", "qemu": {
                    "schema": 1, "pid": os.getpid(), "uid": os.getuid(),
                    "host_boot_id": pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                    "start_time": "1", "exe": "/usr/bin/qemu-system-x86_64",
                    "argv": ["/usr/bin/qemu-system-x86_64"],
                }})
                parent_state._atomic_json(fd, "active-run.json", run)
            finally:
                os.close(fd)
            with mock.patch.object(parent_state, "_recorded_process_status", return_value="MATCH"):
                argv, descriptors = parent_state.parent_ssh_plan(home=self.home)
            try:
                self.assertEqual(argv[argv.index("-p") + 1], "23456")
                self.assertEqual(argv[argv.index("-tt")], "-tt")
                self.assertIn("EscapeChar=none", argv)
                self.assertIn("StrictHostKeyChecking=yes", argv)
                self.assertIn("ClearAllForwardings=yes", argv)
                self.assertIn("ForwardAgent=no", argv)
                self.assertEqual(argv[-1], "mester@127.0.0.1")
                with mock.patch.object(parent_state, "_recorded_process_status", return_value="MATCH"):
                    tui_argv, tui_descriptors = parent_state.parent_ssh_plan(
                        home=self.home, attach_opencode=True)
                self.assertEqual(tui_argv[-2:], ["mester@127.0.0.1",
                                                 "exec /usr/local/bin/lm-opencode"])
                self.assertEqual(len(tui_descriptors), len(descriptors))
                for descriptor in tui_descriptors:
                    os.close(descriptor)
                key_arg = argv[argv.index("-i") + 1]
                known_hosts_arg = next(item.split("=", 1)[1] for item in argv
                                        if item.startswith("UserKnownHostsFile="))
                expected_fd_root = f"/proc/{os.getpid()}/fd/"
                self.assertTrue(key_arg.startswith(expected_fd_root))
                self.assertTrue(known_hosts_arg.startswith(expected_fd_root))
                self.assertIn(int(key_arg.rsplit("/", 1)[1]), descriptors)
                self.assertIn(int(known_hosts_arg.rsplit("/", 1)[1]), descriptors)
                self.assertNotEqual(key_arg, known_hosts_arg)
                required_seals = (fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK |
                                  fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE)
                for descriptor in descriptors:
                    self.assertEqual(fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) & required_seals,
                                     required_seals)
                with self.assertRaises(OSError):
                    os.write(int(key_arg.rsplit("/", 1)[1]), b"substitution")
            finally:
                for descriptor in descriptors:
                    os.close(descriptor)
        finally:
            self.cleanup_test_supervisor(proc)

    def test_explicit_recovery_stops_only_fully_recorded_orphans_and_retains_disk(self):
        supervisor, run_id = self.spawn_supervisor()
        children = []
        disk_before = (self.state_dir / "disk.qcow2").stat()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and parent_state.read_status(home=self.home)["status"] != "STARTING":
                time.sleep(0.01)
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "STARTING")

            directory_fd = parent_state._open_directory_chain(
                create=False, home_override=self.home)
            try:
                for role in ("qemu", "proxy"):
                    target, transition = self.spawn_recovery_target(directory_fd, role)
                    children.append(target)
                    self.assertEqual(parent_state.exec_transition_status(transition), "TARGET")
            finally:
                os.close(directory_fd)

            supervisor.kill()
            supervisor.wait(timeout=3)
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")
            preview = parent_state.recovery_preview(home=self.home)
            self.assertEqual(preview["action"], "recover")
            self.assertEqual(preview["status"], "RECOVERABLE")
            self.assertTrue(preview["dry_run"])
            self.assertTrue(all(child.poll() is None for child in children))

            recovered = parent_state.recover_parent(home=self.home, execute=True)
            self.assertEqual(recovered["status"], "STOPPED")
            self.assertTrue(recovered["disk_retained"])
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "STOPPED")
            disk_after = (self.state_dir / "disk.qcow2").stat()
            self.assertEqual((disk_after.st_dev, disk_after.st_ino),
                             (disk_before.st_dev, disk_before.st_ino))
            self.assertTrue(all(child.wait(timeout=2) == -signal.SIGTERM for child in children))
            self.assertFalse((self.state_dir / "supervisor.sock").exists())
            for role in ("qemu", "proxy"):
                self.assertTrue((self.state_dir / f"child-{role}.identity.json").exists())
                self.assertTrue((self.state_dir / f"child-{role}.target.json").exists())
        finally:
            self.cleanup_test_supervisor(supervisor)
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=3)

    def test_partial_startup_recovery_refuses_missing_child_identity_without_signalling(self):
        supervisor, _run_id = self.spawn_supervisor()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True)
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and parent_state.read_status(home=self.home)["status"] != "STARTING":
                time.sleep(0.01)
            supervisor.kill()
            supervisor.wait(timeout=3)
            with self.assertRaisesRegex(parent_state.Refused, "durable qemu child identity"):
                parent_state.recover_parent(home=self.home, execute=True)
            self.assertIsNone(child.poll())
        finally:
            self.cleanup_test_supervisor(supervisor)
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)

    def test_recovery_cleanup_resumes_after_interruption_and_preview_reports_artifacts(self):
        supervisor, _run_id = self.spawn_supervisor()
        children = []
        qmp = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and parent_state.read_status(home=self.home)["status"] != "STARTING":
                time.sleep(0.01)
            directory_fd = parent_state._open_directory_chain(
                create=False, home_override=self.home)
            try:
                for role in ("qemu", "proxy"):
                    child, _transition = self.spawn_recovery_target(directory_fd, role)
                    children.append(child)
            finally:
                os.close(directory_fd)
            qmp.bind(str(self.state_dir / "qmp.sock"))
            os.chmod(self.state_dir / "qmp.sock", 0o600)
            (self.state_dir / "proxy.port").write_text("12345\n")
            (self.state_dir / "proxy.port").chmod(0o600)
            supervisor.kill()
            supervisor.wait(timeout=3)

            remove_artifact = parent_state._recovery_remove_artifact

            def interrupt_before_proxy_port(directory_fd, name, kind):
                if name == "proxy.port":
                    raise parent_state.Refused("injected interruption before proxy cleanup")
                return remove_artifact(directory_fd, name, kind)

            with mock.patch.object(parent_state, "_recovery_remove_artifact",
                                   side_effect=interrupt_before_proxy_port):
                with self.assertRaisesRegex(parent_state.Refused, "injected interruption"):
                    parent_state.recover_parent(home=self.home, execute=True)
            for child in children:
                child.wait(timeout=2)
            qmp.close()

            preview = parent_state.recovery_preview(home=self.home)
            self.assertEqual(preview["status"], "STOPPED")
            self.assertEqual(preview["action"], "cleanup")
            self.assertEqual(preview["artifacts"], ["proxy.port"])
            resumed = parent_state.recover_parent(home=self.home, execute=True)
            self.assertEqual(resumed["status"], "STOPPED")
            self.assertFalse((self.state_dir / "proxy.port").exists())
            self.assertEqual(parent_state.recovery_preview(home=self.home)["action"], "none")
        finally:
            qmp.close()
            self.cleanup_test_supervisor(supervisor)
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=3)

    def test_recovery_revalidates_lock_after_child_stop_before_state_mutation(self):
        supervisor, _run_id = self.spawn_supervisor()
        children = []
        original_lock = self.state_dir / "lifecycle.lock"
        displaced_lock = self.state_dir / "lifecycle.lock.displaced"
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and parent_state.read_status(home=self.home)["status"] != "STARTING":
                time.sleep(0.01)
            directory_fd = parent_state._open_directory_chain(
                create=False, home_override=self.home)
            try:
                for role in ("qemu", "proxy"):
                    child, _transition = self.spawn_recovery_target(directory_fd, role)
                    children.append(child)
            finally:
                os.close(directory_fd)
            supervisor.kill()
            supervisor.wait(timeout=3)

            def replace_lock(_transition, **_kwargs):
                os.replace(original_lock, displaced_lock)
                replacement = os.open(original_lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(replacement)
                return "STOPPED"

            with mock.patch.object(parent_state, "stop_exec_transition", side_effect=replace_lock):
                with self.assertRaisesRegex(parent_state.Refused, "lifecycle transaction"):
                    parent_state.recover_parent(home=self.home, execute=True)
            directory_fd = parent_state._open_directory_chain(
                create=False, home_override=self.home)
            try:
                self.assertEqual(parent_state._read_json(directory_fd, "active-run.json")["phase"],
                                 "STARTING")
            finally:
                os.close(directory_fd)
            self.assertTrue(all(child.poll() is None for child in children))
            self.assertTrue(displaced_lock.exists())
        finally:
            self.cleanup_test_supervisor(supervisor)
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=3)

    def test_recovery_refuses_unsafe_cleanup_artifact_before_signalling_children(self):
        supervisor, _run_id = self.spawn_supervisor()
        children = []
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and parent_state.read_status(home=self.home)["status"] != "STARTING":
                time.sleep(0.01)
            directory_fd = parent_state._open_directory_chain(
                create=False, home_override=self.home)
            try:
                for role in ("qemu", "proxy"):
                    child, _transition = self.spawn_recovery_target(directory_fd, role)
                    children.append(child)
            finally:
                os.close(directory_fd)
            unsafe = self.state_dir / "qmp.sock"
            unsafe.write_text("not a socket")
            unsafe.chmod(0o600)
            supervisor.kill()
            supervisor.wait(timeout=3)

            preview = parent_state.recovery_preview(home=self.home)
            self.assertEqual(preview["action"], "none")
            self.assertEqual(preview["status"], "AMBIGUOUS")
            with self.assertRaisesRegex(parent_state.Refused, "unsafe qmp.sock"):
                parent_state.recover_parent(home=self.home, execute=True)
            self.assertTrue(all(child.poll() is None for child in children))
            self.assertEqual(unsafe.read_text(), "not a socket")
        finally:
            self.cleanup_test_supervisor(supervisor)
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=3)

    def test_partial_seed_iso_is_refused_and_reported_ambiguous(self):
        part = self.state_dir / "seed.iso.part"
        part.write_bytes(b"partial evidence")
        os.chmod(part, 0o600)
        self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")
        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            with self.assertRaises(parent_state.Refused):
                parent_supervisor._prepare_parent_seed(parent_state, lock_fd, str(self.home))
        self.assertEqual(part.read_bytes(), b"partial evidence")

    def test_existing_seed_refuses_missing_persistent_key_material(self):
        (self.state_dir / "seed.iso").write_bytes(b"seed")
        os.chmod(self.state_dir / "seed.iso", 0o600)
        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            with mock.patch.object(parent_supervisor, "_protected_tool", return_value="/usr/bin/ssh-keygen"):
                with self.assertRaises(RuntimeError):
                    parent_supervisor._prepare_parent_seed(parent_state, lock_fd, str(self.home))

    def test_supervisor_gated_spawn_eof_fails_closed_on_transition_persistence_error(self):
        marker = pathlib.Path(self.temp.name) / "must-not-start"
        executable = os.path.realpath(sys.executable)
        code = f"import pathlib; pathlib.Path({str(marker)!r}).write_text('started')"
        captured = {}

        def fail_commit(_directory_fd, _name, identity, **_kwargs):
            captured["identity"] = identity
            raise parent_state.Refused("injected authorization persistence failure")

        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            try:
                with mock.patch.object(parent_state, "persist_exec_transition", side_effect=fail_commit):
                    with self.assertRaises(parent_state.Refused):
                        parent_supervisor._spawn_gated_target(
                            parent_state, lock_fd=lock_fd, role="qemu", executable=executable,
                            argv=[executable, "-c", code], home=str(self.home))
                transition = captured["identity"]
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and parent_state.exec_transition_status(transition) != "STOPPED":
                    time.sleep(0.01)
                self.assertEqual(parent_state.exec_transition_status(transition), "STOPPED")
                self.assertFalse(marker.exists())
            finally:
                self.cleanup_gated_child("qemu", captured.get("identity"))

    def test_starting_child_rollback_uses_durable_identity_and_retains_disk(self):
        executable = os.path.realpath(sys.executable)
        transition = None
        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            try:
                transition = parent_supervisor._spawn_gated_target(
                    parent_state, lock_fd=lock_fd, role="qemu", executable=executable,
                    argv=[executable, "-c", "import time; time.sleep(30)"], home=str(self.home))
                self.assertEqual(parent_state.exec_transition_status(transition), "TARGET")
                parent_supervisor._stop_starting_children(parent_state, str(self.home))
                self.assertEqual(parent_state.exec_transition_status(transition), "STOPPED")
                self.assertNotIn("qemu", parent_supervisor._CHILD_PROCESSES)
                self.assertTrue((self.state_dir / "disk.qcow2").exists())
            finally:
                if transition is not None:
                    self.cleanup_gated_child("qemu", transition)

    def test_running_is_committed_only_after_readiness_and_exact_qemu_identity(self):
        executable = os.path.realpath(sys.executable)
        run_id = "0123456789abcdef0123456789abcdef"
        transition = None
        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            fd = parent_state._open_directory_chain(create=False, home_override=self.home)
            try:
                parent_supervisor._record_supervisor(
                    parent_state, fd, "default", run_id, parent_state.observe_process(os.getpid()))
            finally:
                os.close(fd)
            try:
                transition = parent_supervisor._spawn_gated_target(
                    parent_state, lock_fd=lock_fd, role="qemu", executable=executable,
                    argv=[executable, "-c", "import time; time.sleep(30)"], home=str(self.home))
                with self.assertRaisesRegex(parent_state.Refused, "SSH readiness"):
                    parent_supervisor._commit_running_after_readiness(
                        parent_state, run_id=run_id, home=str(self.home), lock_fd=lock_fd,
                        qemu_transition=transition, readiness=lambda: False)
                fd = parent_state._open_directory_chain(create=False, home_override=self.home)
                try:
                    self.assertEqual(parent_state._read_json(fd, "active-run.json")["phase"], "STARTING")
                finally:
                    os.close(fd)
                qemu = parent_supervisor._commit_running_after_readiness(
                    parent_state, run_id=run_id, home=str(self.home), lock_fd=lock_fd,
                    qemu_transition=transition, readiness=lambda: True)
                self.assertEqual(qemu["pid"], parent_supervisor._CHILD_PROCESSES["qemu"][0].pid)
                fd = parent_state._open_directory_chain(create=False, home_override=self.home)
                try:
                    record = parent_state._read_json(fd, "active-run.json")
                finally:
                    os.close(fd)
                self.assertEqual(record["phase"], "RUNNING")
                self.assertEqual(record["qemu"], qemu)
            finally:
                if transition is not None:
                    parent_supervisor._stop_gated_target(parent_state, "qemu", transition)

    def test_start_parent_children_reaches_local_ssh_metadata_writer(self):
        from types import SimpleNamespace

        (self.state_dir / "ssh_host_ed25519_key.pub").write_text(
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey parent\n")
        (self.state_dir / "disk.qcow2").write_bytes(b"disk")
        directory_fd = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
        parent = {"disk": {"provisioned": True}, "ssh_identity": {"seed": "present"}}
        fake_state = SimpleNamespace(
            Refused=parent_state.Refused,
            _open_directory_chain=lambda **_kwargs: os.dup(directory_fd),
            _read_json=lambda _fd, _name: parent,
            validate_parent=lambda _record: None,
            _validate_seed_identity=lambda *_args: None,
            PARENT_ID="default",
            STATE_COMPONENTS=parent_state.STATE_COMPONENTS,
            pwd=parent_state.pwd,
        )
        transitions = iter(({"target_path": "proxy"}, {"target_path": "qemu"}))
        args = SimpleNamespace(ssh_port=23456, lock_fd=99, run_id="0123456789abcdef0123456789abcdef")
        try:
            with (mock.patch.object(parent_supervisor, "_protected_tool", side_effect=lambda name, _paths: f"/usr/bin/{name}"),
                  mock.patch.object(parent_supervisor, "_load_protected_module",
                                    return_value=SimpleNamespace(build_qemu_argv=lambda **_kwargs: ["/usr/bin/qemu"])),
                  mock.patch.object(parent_supervisor, "_write_ssh_connection") as write_ssh,
                  mock.patch.object(parent_supervisor, "_spawn_gated_target", side_effect=lambda *_a, **_kw: next(transitions)),
                  mock.patch.object(parent_supervisor, "_wait_for_proxy", return_value=34567),
                  mock.patch.object(parent_supervisor, "_wait_for_guest_readiness", return_value=True),
                  mock.patch.object(parent_supervisor, "_commit_running_after_readiness") as commit):
                parent_supervisor._start_parent_children(fake_state, args, str(self.home),
                                                         stop_requested=lambda: False)
            write_ssh.assert_called_once()
            self.assertEqual(write_ssh.call_args.args[2], 23456)
            commit.assert_called_once()
        finally:
            os.close(directory_fd)

    def test_finalize_writes_valid_stopping_then_stopped_record_and_keeps_disk(self):
        proc, run_id = self.spawn_supervisor()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and parent_state.read_status(home=self.home)["status"] != "STARTING":
                time.sleep(0.01)
            fd = parent_state._open_directory_chain(create=False, home_override=self.home)
            try:
                run = parent_state._read_json(fd, "active-run.json")
                run["phase"] = "RUNNING"
                run["qemu"] = {"schema": 1, "pid": 999999, "uid": os.getuid(),
                               "host_boot_id": "test", "start_time": "1",
                               "exe": "/usr/bin/qemu-system-x86_64", "argv": ["qemu"]}
                parent_state._atomic_json(fd, "active-run.json", run)
            finally:
                os.close(fd)
            lock_fd = os.open(self.state_dir / "lifecycle.lock", os.O_RDWR)
            with (mock.patch.object(parent_supervisor.os, "getpid", return_value=proc.pid),
                  mock.patch.object(parent_state, "_recorded_process_status", return_value="GONE")):
                parent_supervisor._finalize(
                    parent_state, SimpleNamespace(run_id=run_id, lock_fd=lock_fd), str(self.home))
            status = parent_state.read_status(home=self.home)
            self.assertEqual(status["status"], "STOPPING")
            fd = parent_state._open_directory_chain(create=False, home_override=self.home)
            try:
                run = parent_state._read_json(fd, "active-run.json")
            finally:
                os.close(fd)
            self.assertEqual(run["phase"], "STOPPING")
            self.assertNotIn("qemu", run)
            proc.terminate()
            self.assertEqual(proc.wait(timeout=3), 2)
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "STOPPED")
            self.assertTrue((self.state_dir / "disk.qcow2").exists())
            with parent_state.lifecycle_lock(home=self.home):
                pass
        finally:
            self.cleanup_test_supervisor(proc)

    def test_qmp_socket_is_removed_only_after_exact_qemu_stop_and_only_if_owned_socket(self):
        qmp_path = self.state_dir / "qmp.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(qmp_path))
            os.chmod(qmp_path, 0o600)
            fake_state = SimpleNamespace(
                Refused=parent_state.Refused,
                _open_directory_chain=lambda **_kwargs: parent_state._open_directory_chain(
                    create=False, home_override=self.home),
                exec_transition_status=lambda _transition: "TARGET",
            )
            with self.assertRaisesRegex(parent_state.Refused, "QMP socket"):
                parent_supervisor._remove_qmp_socket(fake_state, {"pid": os.getpid()}, str(self.home))
            self.assertTrue(qmp_path.exists())
            fake_state.exec_transition_status = lambda _transition: "STOPPED"
            parent_supervisor._remove_qmp_socket(fake_state, {"pid": os.getpid()}, str(self.home))
            self.assertFalse(qmp_path.exists())
            qmp_path.write_text("not a socket")
            with self.assertRaisesRegex(parent_state.Refused, "unowned QMP socket"):
                parent_supervisor._remove_qmp_socket(fake_state, {"pid": os.getpid()}, str(self.home))
            self.assertTrue(qmp_path.exists())
        finally:
            listener.close()

    def test_guest_readiness_uses_fixed_pinned_ssh_command_and_checks_qemu(self):
        calls = []

        def ssh_plan(**kwargs):
            return (["/usr/bin/ssh", "-F", "/dev/null", "-T", "mester@127.0.0.1",
                     kwargs["remote_command"]], [os.open("/dev/null", os.O_RDONLY),
                                                  os.open("/dev/null", os.O_RDONLY)])

        fake_state = SimpleNamespace(
            Refused=parent_state.Refused,
            PARENT_READY_COMMAND=parent_state.PARENT_READY_COMMAND,
            exec_transition_status=lambda _transition: "TARGET",
            parent_ssh_plan=ssh_plan,
            read_status=lambda **_kwargs: {"status": "STARTING",
                                           "run_id": "0123456789abcdef0123456789abcdef"},
        )

        def fake_run(argv, descriptors, **kwargs):
            calls.append((argv, descriptors, kwargs))
            return 0, b"", False

        with (mock.patch.object(parent_supervisor, "_protected_tool", return_value="/usr/bin/ssh"),
              mock.patch.object(parent_supervisor, "_read_private_port", return_value=23456),
              mock.patch.object(parent_supervisor, "_loopback_forward_accepts", return_value=True),
              mock.patch.object(parent_supervisor, "_run_bounded_ssh_attempt", side_effect=fake_run)):
            ready = parent_supervisor._wait_for_guest_readiness(
                fake_state, None, {"target_path": "child-qemu.target.json"},
                stop_requested=lambda: False, timeout_seconds=1)
        self.assertTrue(ready)
        self.assertEqual(len(calls), 1)
        argv, descriptors, kwargs = calls[0]
        self.assertEqual(argv[-1], fake_state.PARENT_READY_COMMAND)
        self.assertEqual(len(descriptors), 2)
        self.assertTrue(all(fd >= 3 for fd in descriptors))

    def test_guest_readiness_reaps_exited_qemu_before_transition_classification(self):
        child = subprocess.Popen([sys.executable, "-c", "raise SystemExit(17)"])
        deadline = time.monotonic() + 3
        observed_zombie = False
        while time.monotonic() < deadline:
            # Wait until the kernel has exited the child without calling
            # Popen.poll()/wait(), leaving it as a zombie for the first probe.
            try:
                fields = pathlib.Path(f"/proc/{child.pid}/stat").read_text().rsplit(")", 1)[1].split()
            except FileNotFoundError:
                break
            if fields[0] == "Z":
                observed_zombie = True
                break
            time.sleep(0.01)
        self.assertTrue(observed_zombie)
        self.assertIsNone(child.returncode)
        observations = []

        def transition_status(_transition):
            reaped = child.returncode is not None
            observations.append(reaped)
            return "STOPPED" if reaped else "AMBIGUOUS"

        fake_state = SimpleNamespace(
            Refused=parent_state.Refused,
            PARENT_READY_COMMAND=parent_state.PARENT_READY_COMMAND,
            read_status=lambda **_kwargs: {"run_id": "0123456789abcdef0123456789abcdef"},
            exec_transition_status=transition_status,
        )
        parent_supervisor._CHILD_PROCESSES["qemu"] = (child, None)
        try:
            with (mock.patch.object(parent_supervisor, "_protected_tool", return_value="/usr/bin/ssh"),
                  mock.patch.object(parent_supervisor, "_read_private_port", return_value=23456)):
                with self.assertRaisesRegex(parent_state.Refused,
                                            r"QEMU_PROCESS_EXITED_BEFORE_READINESS exit=17"):
                    parent_supervisor._wait_for_guest_readiness(
                        fake_state, None, {"target_path": "child-qemu.target.json"},
                        stop_requested=lambda: False, timeout_seconds=1)
            self.assertEqual(observations, [True])
            self.assertEqual(child.returncode, 17)
        finally:
            parent_supervisor._CHILD_PROCESSES.pop("qemu", None)

    def test_post_bind_record_failure_removes_only_new_socket(self):
        active_run = self.state_dir / "active-run.json"
        active_run.mkdir(mode=0o700)
        proc, _run_id = self.spawn_supervisor()
        try:
            self.assertEqual(proc.wait(timeout=3), 2)
            self.assertTrue(active_run.is_dir())
            self.assertEqual(parent_state.read_status(home=self.home)["status"], "AMBIGUOUS")
        finally:
            self.cleanup_test_supervisor(proc)

    def test_commit_eof_barrier_is_bounded_when_writer_stays_open(self):
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        try:
            os.write(write_fd, b"COMMIT\n")
            started = time.monotonic()
            self.assertFalse(parent_supervisor._read_commit(read_fd, timeout_seconds=0.05))
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_atomic_stop_record_failure_still_closes_supervisor_lock(self):
        directory_fd = parent_state._open_directory_chain(create=False, home_override=self.home)
        try:
            parent_state._atomic_json(directory_fd, "active-run.json",
                                      {"run_id": "test-run", "supervisor": {"pid": os.getpid()}})
        finally:
            os.close(directory_fd)
        with parent_state.lifecycle_lock(home=self.home) as lock_fd:
            with mock.patch.object(parent_state, "_atomic_json", side_effect=OSError("injected final write failure")):
                with self.assertRaises(OSError):
                    parent_supervisor._finalize(
                        parent_state, SimpleNamespace(run_id="test-run", lock_fd=lock_fd),
                        str(self.home))
            with self.assertRaises(OSError):
                os.fstat(lock_fd)


if __name__ == "__main__":
    unittest.main()
