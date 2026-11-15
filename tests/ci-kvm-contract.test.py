#!/usr/bin/env python3
"""Test the exact hosted-KVM command fragments and SSH readiness semantics."""

from __future__ import annotations

import os
import pathlib
import pwd
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "tests/ci-kvm-contract.sh"
CLASSIFIER = ROOT / "tests/ci-kvm-readiness.py"


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class HostedKvmCommandTests(unittest.TestCase):
    def test_boot_modes_share_loopback_forward_and_ssh_trust_options(self):
        command = f'''source {CONTRACT!s}
printf 'INITIAL=%s\\n' "$(ci_kvm_netdev 43210 0)"
printf 'RESTRICTED=%s\\n' "$(ci_kvm_netdev 43210 1)"
qemu_initial=(); ci_kvm_qemu_args initial /tmp/guest.qcow2 /tmp/seed.iso /tmp/serial.log 43210 qemu_initial
qemu_restart=(); ci_kvm_qemu_args restricted-restart /tmp/guest.qcow2 /tmp/seed.iso /tmp/serial.log 43210 qemu_restart
printf 'INITIAL_QEMU='; printf '%s\\0' "${{qemu_initial[@]}}"
printf 'RESTART_QEMU='; printf '%s\\0' "${{qemu_restart[@]}}"
batch=(); ci_kvm_ssh_args mester@127.0.0.1 /tmp/key 43210 /tmp/known batch batch
pty=(); ci_kvm_ssh_args mester@127.0.0.1 /tmp/key 43210 /tmp/known tty pty
printf 'BATCH='; printf '%s\\0' "${{batch[@]}}"
printf 'PTY='; printf '%s\\0' "${{pty[@]}}"
'''
        result = subprocess.run(
            ["bash", "-c", command], cwd=ROOT, capture_output=True,
            timeout=5, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        text, nul_data = result.stdout.split(b"INITIAL_QEMU=", 1)
        self.assertIn(
            b"INITIAL=user,id=net0,hostfwd=tcp:127.0.0.1:43210-:22",
            text,
        )
        self.assertIn(
            b"RESTRICTED=user,id=net0,restrict=on,hostfwd=tcp:127.0.0.1:43210-:22",
            text,
        )
        self.assertNotIn(b"hostfwd=tcp:0.0.0.0", text)
        initial_qemu, nul_data = nul_data.split(b"RESTART_QEMU=", 1)
        restart_qemu, nul_data = nul_data.split(b"BATCH=", 1)
        initial_qemu_args = initial_qemu.rstrip(b"\0").split(b"\0")
        restart_qemu_args = restart_qemu.rstrip(b"\0").split(b"\0")
        initial_netdev = initial_qemu_args[initial_qemu_args.index(b"-netdev") + 1]
        restart_netdev = restart_qemu_args[restart_qemu_args.index(b"-netdev") + 1]
        self.assertIn(b"restrict=on", restart_netdev)
        self.assertNotIn(b"restrict=on", initial_netdev)
        self.assertIn(b"hostfwd=tcp:127.0.0.1:43210-:22", initial_netdev)
        self.assertIn(b"hostfwd=tcp:127.0.0.1:43210-:22", restart_netdev)
        restart_qemu_args[restart_qemu_args.index(b"-netdev") + 1] = initial_netdev
        self.assertEqual(initial_qemu_args, restart_qemu_args)
        self.assertTrue(any(arg.startswith(b"file=/tmp/guest.qcow2,") for arg in initial_qemu_args))
        self.assertTrue(any(arg.startswith(b"file=/tmp/seed.iso,") for arg in initial_qemu_args))
        self.assertIn(b"file:/tmp/serial.log", initial_qemu_args)
        batch_raw, pty_raw = nul_data.split(b"PTY=", 1)
        batch = batch_raw.rstrip(b"\0").split(b"\0")
        pty = pty_raw.rstrip(b"\0").split(b"\0")
        for args in (batch, pty):
            self.assertIn(b"StrictHostKeyChecking=yes", args)
            self.assertIn(b"UserKnownHostsFile=/tmp/known", args)
            self.assertIn(b"GlobalKnownHostsFile=/dev/null", args)
            self.assertIn(b"ProxyCommand=none", args)
            self.assertIn(b"ProxyJump=none", args)
            self.assertIn(b"IdentitiesOnly=yes", args)
            self.assertIn(b"IdentityAgent=none", args)
            self.assertIn(b"-p", args)
            self.assertEqual(args[args.index(b"-p") + 1], b"43210")
        self.assertIn(b"-T", batch)
        self.assertIn(b"-tt", pty)


class LocalSshReadinessTests(unittest.TestCase):
    """Use a temporary real sshd to prove auth-success and remote-status behavior."""

    @classmethod
    def setUpClass(cls):
        cls.ssh = shutil.which("ssh")
        cls.sshd = shutil.which("sshd")
        cls.keygen = shutil.which("ssh-keygen")
        cls.keyscan = shutil.which("ssh-keyscan")
        if not all((cls.ssh, cls.sshd, cls.keygen, cls.keyscan)):
            raise unittest.SkipTest("local OpenSSH server/client tools are unavailable")
        cls.temp = tempfile.TemporaryDirectory(prefix="ci-kvm-readiness-sshd-")
        cls.root = pathlib.Path(cls.temp.name)
        cls.sshd_process = None
        cls.addClassCleanup(cls.cleanup_resources)
        cls.username = pwd.getpwuid(os.getuid()).pw_name
        cls.port = free_port()
        cls.client_key = cls.root / "client-key"
        cls.host_key = cls.root / "host-key"
        cls.authorized_keys = cls.root / "authorized_keys"
        cls.known_hosts = cls.root / "known_hosts"
        cls.config = cls.root / "sshd_config"
        cls.pid_file = cls.root / "sshd.pid"
        for key in (cls.client_key, cls.host_key):
            subprocess.run(
                [cls.keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10,
            )
        cls.authorized_keys.write_text(cls.client_key.with_suffix(".pub").read_text())
        cls.authorized_keys.chmod(0o600)
        permit_root = "yes" if os.getuid() == 0 else "no"
        cls.config.write_text(
            f"Port {cls.port}\nListenAddress 127.0.0.1\nHostKey {cls.host_key}\n"
            f"PidFile {cls.pid_file}\nAuthorizedKeysFile {cls.authorized_keys}\n"
            f"StrictModes no\nUsePAM no\nPasswordAuthentication no\n"
            f"KbdInteractiveAuthentication no\nPubkeyAuthentication yes\n"
            f"PermitRootLogin {permit_root}\nAllowUsers {cls.username}\n"
            "AllowTcpForwarding no\nX11Forwarding no\nPermitTunnel no\n"
            "UseDNS no\nLogLevel QUIET\n"
        )
        cls.config.chmod(0o600)
        check = subprocess.run(
            [cls.sshd, "-t", "-f", str(cls.config)], capture_output=True,
            timeout=5, check=False,
        )
        if check.returncode:
            raise RuntimeError(
                "temporary sshd config validation failed: "
                + check.stderr.decode("utf-8", errors="replace")[:300]
            )
        cls.sshd_process = subprocess.Popen(
            [cls.sshd, "-D", "-e", "-f", str(cls.config)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
        )
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if cls.sshd_process.poll() is not None:
                raise RuntimeError("temporary sshd exited during startup")
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.02)
        else:
            raise RuntimeError("temporary sshd did not become ready within 5s")
        scan = subprocess.run(
            [cls.keyscan, "-T", "2", "-t", "ed25519", "-p", str(cls.port), "127.0.0.1"],
            capture_output=True, timeout=5, check=False,
        )
        if scan.returncode or not scan.stdout:
            raise RuntimeError("temporary sshd host key scan failed")
        cls.known_hosts.write_bytes(scan.stdout)
        cls.known_hosts.chmod(0o600)

    @classmethod
    def cleanup_resources(cls):
        process = getattr(cls, "sshd_process", None)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        temp = getattr(cls, "temp", None)
        if temp is not None:
            temp.cleanup()

    def run_remote(self, command: str, *, known_hosts: pathlib.Path | None = None,
                   key: pathlib.Path | None = None) -> subprocess.CompletedProcess[bytes]:
        use_known = known_hosts or self.known_hosts
        use_key = key or self.client_key
        built = subprocess.run(
            ["bash", "-c",
             f"source {CONTRACT!s}; args=(); ci_kvm_ssh_args \"$1\" \"$2\" \"$3\" \"$4\" batch args; printf '%s\\0' \"${{args[@]}}\"",
             "ci-kvm-ssh-test", f"{self.username}@127.0.0.1", str(use_key),
             str(self.port), str(use_known)],
            cwd=ROOT, capture_output=True, timeout=5, check=True,
        )
        args = [self.ssh, *built.stdout.rstrip(b"\0").decode().split("\0"), command]
        return subprocess.run(args, capture_output=True, timeout=8, check=False)

    def classify(self, result: subprocess.CompletedProcess[bytes]) -> str:
        with tempfile.NamedTemporaryFile() as output:
            output.write(result.stdout + result.stderr)
            output.flush()
            classified = subprocess.run(
                ["python3", str(CLASSIFIER), "--exit-status", str(result.returncode),
                 "--input", output.name], capture_output=True, text=True,
                timeout=5, check=True,
            )
        return classified.stdout.strip()

    def test_transport_and_timeout_exit_classes_are_separate(self):
        transport = subprocess.CompletedProcess(
            [self.ssh], 255, b"", b"connect to host 127.0.0.1 port 1: Connection refused\n"
        )
        self.assertEqual(self.classify(transport), "transport-or-forwarding")
        timeout = subprocess.CompletedProcess([self.ssh], 124, b"", b"")
        self.assertEqual(self.classify(timeout), "attempt-timeout")

    @staticmethod
    def generated_readiness(root: pathlib.Path, previous_boot_id: str,
                            state: pathlib.Path, binary: pathlib.Path,
                            *, boot_id_file: pathlib.Path | None = None,
                            cloud_init_output: str = "status: done\nextended_status: done\n",
                            cloud_init_exit: int = 0) -> tuple[str, pathlib.Path]:
        systemd_bin = root / "bin"
        systemd_bin.mkdir()
        (systemd_bin / "systemctl").write_text("#!/bin/sh\nexit 1\n")
        (systemd_bin / "systemctl").chmod(0o755)
        (systemd_bin / "cloud-init").write_text(
            "#!/bin/sh\nprintf '%s' " + shlex.quote(cloud_init_output)
            + f"\nexit {cloud_init_exit}\n"
        )
        (systemd_bin / "cloud-init").chmod(0o755)
        boot_finished = root / "boot-finished"
        boot_finished.touch()
        generated = subprocess.run(
            ["bash", "-c",
             f"source {CONTRACT!s}; ci_kvm_restart_readiness_command "
             f"{shlex.quote(previous_boot_id)} {shlex.quote(str(state))} "
             f"{shlex.quote(str(binary))} {shlex.quote(str(boot_finished))} "
             f"{shlex.quote(str(boot_id_file or pathlib.Path('/proc/sys/kernel/random/boot_id')))}"],
            cwd=ROOT, capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        return f"PATH={shlex.quote(str(systemd_bin))}:$PATH; export PATH; {generated}", boot_finished

    def test_successful_authentication_remote_failure_and_strict_host_identity(self):
        with tempfile.TemporaryDirectory(prefix="ci-kvm-remote-ready-") as directory:
            root = pathlib.Path(directory)
            state = root / "persisted.txt"
            binary = root / "opencode"
            # Socket-activated SSH can answer authenticated requests while the
            # aggregate ssh.service unit reports inactive after reboot.
            remote_command, _ = self.generated_readiness(
                root, "00000000-0000-0000-0000-000000000000", state, binary
            )
            remote = self.run_remote(remote_command)
        self.assertEqual(remote.returncode, 42, remote.stderr.decode(errors="replace"))
        self.assertEqual(self.classify(remote), "remote-persistence-file-missing")

        with tempfile.TemporaryDirectory(prefix="ci-kvm-remote-ready-pass-") as directory:
            root = pathlib.Path(directory)
            state = root / "persisted.txt"
            state.write_text("persistent-fixture")
            binary = root / "opencode"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            remote_command, _ = self.generated_readiness(
                root, "00000000-0000-0000-0000-000000000000", state, binary
            )
            remote = self.run_remote(remote_command)
        self.assertEqual(remote.returncode, 0, remote.stderr.decode(errors="replace"))
        self.assertEqual(self.classify(remote), "ready")
        self.assertIn(b"KVM_RESTART_REMOTE_NOTE=ssh-service-inactive", remote.stdout)

        with tempfile.TemporaryDirectory(prefix="ci-kvm-remote-boot-id-") as directory:
            root = pathlib.Path(directory)
            state = root / "persisted.txt"
            state.write_text("persistent-fixture")
            binary = root / "opencode"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            current_boot_id = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            remote_command, _ = self.generated_readiness(root, current_boot_id, state, binary)
            unchanged = self.run_remote(remote_command)
        self.assertEqual(unchanged.returncode, 44)
        self.assertEqual(self.classify(unchanged), "remote-guest-boot-id-unchanged")

        with tempfile.TemporaryDirectory(prefix="ci-kvm-remote-missing-boot-id-") as directory:
            root = pathlib.Path(directory)
            state = root / "persisted.txt"
            state.write_text("persistent-fixture")
            binary = root / "opencode"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            remote_command, _ = self.generated_readiness(
                root, "00000000-0000-0000-0000-000000000000", state, binary,
                boot_id_file=root / "missing-boot-id",
            )
            missing_id = self.run_remote(remote_command)
        self.assertEqual(missing_id.returncode, 47)
        self.assertEqual(self.classify(missing_id), "remote-guest-boot-id-unavailable")

        with tempfile.TemporaryDirectory(prefix="ci-kvm-remote-cloud-init-degraded-") as directory:
            root = pathlib.Path(directory)
            state = root / "persisted.txt"
            state.write_text("persistent-fixture")
            binary = root / "opencode"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            remote_command, _ = self.generated_readiness(
                root, "00000000-0000-0000-0000-000000000000", state, binary,
                cloud_init_output="status: done\nextended_status: degraded done\n",
                cloud_init_exit=2,
            )
            degraded = self.run_remote(remote_command)
        self.assertEqual(degraded.returncode, 46)
        self.assertEqual(self.classify(degraded), "remote-cloud-init-error")

        mismatch = self.known_hosts.parent / "wrong_known_hosts"
        wrong_type, wrong_key, *_ = self.client_key.with_suffix(".pub").read_text().split()
        mismatch.write_text(f"[127.0.0.1]:{self.port} {wrong_type} {wrong_key}\n")
        failed = self.run_remote("true", known_hosts=mismatch)
        self.assertEqual(failed.returncode, 255)
        self.assertEqual(self.classify(failed), "host-key-verification")

        wrong_key = self.known_hosts.parent / "wrong-client-key"
        subprocess.run(
            [self.keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(wrong_key)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10,
        )
        failed = self.run_remote("true", key=wrong_key)
        self.assertEqual(failed.returncode, 255)
        self.assertEqual(self.classify(failed), "authentication-or-ssh-negotiation")


if __name__ == "__main__":
    unittest.main()
