#!/usr/bin/env python3
"""Test the exact hosted-KVM command fragments and SSH readiness semantics."""

from __future__ import annotations

import os
import hashlib
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
                   key: pathlib.Path | None = None,
                   path_prefix: pathlib.Path | None = None) -> subprocess.CompletedProcess[bytes]:
        use_known = known_hosts or self.known_hosts
        use_key = key or self.client_key
        built = subprocess.run(
            ["bash", "-c",
             f"source {CONTRACT!s}; args=(); ci_kvm_ssh_args \"$1\" \"$2\" \"$3\" \"$4\" batch args; printf '%s\\0' \"${{args[@]}}\"",
             "ci-kvm-ssh-test", f"{self.username}@127.0.0.1", str(use_key),
             str(self.port), str(use_known)],
            cwd=ROOT, capture_output=True, timeout=5, check=True,
        )
        if path_prefix is not None:
            command = f"PATH={shlex.quote(str(path_prefix))}:$PATH; {command}"
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
        remote_timeout = subprocess.CompletedProcess(
            [self.ssh], 124, b"KVM_PERSISTENCE_REMOTE_AUTHENTICATED\n", b""
        )
        self.assertEqual(self.classify(remote_timeout), "persistence-check-timeout")

    @staticmethod
    def generated_readiness(root: pathlib.Path, previous_boot_id: str,
                            binary: pathlib.Path,
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
             f"{shlex.quote(previous_boot_id)} {shlex.quote(str(binary))} "
             f"{shlex.quote(str(boot_finished))} "
             f"{shlex.quote(str(boot_id_file or pathlib.Path('/proc/sys/kernel/random/boot_id')))}"],
            cwd=ROOT, capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        return f"PATH={shlex.quote(str(systemd_bin))}:$PATH; export PATH; {generated}", boot_finished

    def test_authenticated_ssh_readiness_is_independent_of_persistence(self):
        with tempfile.TemporaryDirectory(prefix="ci-kvm-remote-ready-") as directory:
            root = pathlib.Path(directory)
            binary = root / "opencode"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            # Socket-activated SSH can answer authenticated requests while the
            # aggregate ssh.service unit reports inactive after reboot. The
            # workspace fixture is intentionally absent: SSH/boot readiness
            # must still succeed and persistence is checked separately.
            remote_command, _ = self.generated_readiness(
                root, "00000000-0000-0000-0000-000000000000", binary
            )
            remote = self.run_remote(remote_command)
        self.assertEqual(remote.returncode, 0, remote.stderr.decode(errors="replace"))
        self.assertEqual(self.classify(remote), "ready")
        self.assertIn(b"KVM_RESTART_REMOTE_AUTHENTICATED", remote.stdout)
        self.assertIn(b"KVM_RESTART_REMOTE_READY", remote.stdout)

        self.assertIn(b"KVM_RESTART_REMOTE_NOTE=ssh-service-inactive", remote.stdout)

        with tempfile.TemporaryDirectory(prefix="ci-kvm-remote-boot-id-") as directory:
            root = pathlib.Path(directory)
            binary = root / "opencode"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            current_boot_id = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            remote_command, _ = self.generated_readiness(root, current_boot_id, binary)
            unchanged = self.run_remote(remote_command)
        self.assertEqual(unchanged.returncode, 44)
        self.assertEqual(self.classify(unchanged), "remote-guest-boot-id-unchanged")

        with tempfile.TemporaryDirectory(prefix="ci-kvm-remote-missing-boot-id-") as directory:
            root = pathlib.Path(directory)
            binary = root / "opencode"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            remote_command, _ = self.generated_readiness(
                root, "00000000-0000-0000-0000-000000000000", binary,
                boot_id_file=root / "missing-boot-id",
            )
            missing_id = self.run_remote(remote_command)
        self.assertEqual(missing_id.returncode, 47)
        self.assertEqual(self.classify(missing_id), "remote-guest-boot-id-unavailable")

        with tempfile.TemporaryDirectory(prefix="ci-kvm-remote-cloud-init-degraded-") as directory:
            root = pathlib.Path(directory)
            binary = root / "opencode"
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            remote_command, _ = self.generated_readiness(
                root, "00000000-0000-0000-0000-000000000000", binary,
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

    @staticmethod
    def generated_persistence_check(path: pathlib.Path, content_sha256: str,
                                    *, uid: int | None = None, gid: int | None = None,
                                    home: pathlib.Path | None = None,
                                    mount_sha256: str = "") -> str:
        uid = os.getuid() if uid is None else uid
        gid = os.getgid() if gid is None else gid
        home = pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir) if home is None else home
        generated = subprocess.run(
            ["bash", "-c",
             f"source {CONTRACT!s}; ci_kvm_persistence_check_command \"$1\" \"$2\" \"$3\" \"$4\" \"$5\" \"$6\"",
             "persistence-contract", str(path), str(uid), str(gid), str(home),
             content_sha256, mount_sha256],
            cwd=ROOT, capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        return generated

    @staticmethod
    def generated_persistence_create(path: pathlib.Path, content: str) -> str:
        uid = os.getuid()
        gid = os.getgid()
        home = pwd.getpwuid(uid).pw_dir
        return subprocess.run(
            ["bash", "-c",
             f"source {CONTRACT!s}; ci_kvm_persistence_create_command \"$1\" \"$2\" \"$3\" \"$4\" \"$5\"",
             "persistence-create-contract", str(path), content, str(uid), str(gid), home],
            cwd=ROOT, capture_output=True, text=True, timeout=5, check=True,
        ).stdout

    @staticmethod
    def generated_persistence_flush(path: pathlib.Path) -> str:
        uid = os.getuid()
        gid = os.getgid()
        home = pwd.getpwuid(uid).pw_dir
        return subprocess.run(
            ["bash", "-c",
             f"source {CONTRACT!s}; ci_kvm_persistence_flush_command \"$1\" \"$2\" \"$3\" \"$4\"",
             "persistence-flush-contract", str(path), str(uid), str(gid), home],
            cwd=ROOT, capture_output=True, text=True, timeout=5, check=True,
        ).stdout

    def test_persistence_flush_is_bounded_contract_and_failure_is_classified(self):
        with tempfile.TemporaryDirectory(prefix="ci-kvm-persistence-flush-") as directory:
            root = pathlib.Path(directory)
            fixture = root / "flush-proof"
            fixture.write_text("synthetic flush fixture")
            fixture.chmod(0o600)
            command = self.generated_persistence_flush(fixture)
            success = self.run_remote(command)
            self.assertEqual(success.returncode, 0, success.stderr.decode(errors="replace"))
            self.assertEqual(self.classify(success), "persistence-flush-pass")
            self.assertIn(b"KVM_PERSISTENCE_FLUSH=PASS", success.stdout)

            missing = self.run_remote(
                self.generated_persistence_flush(root / "missing-fixture")
            )
            self.assertEqual(missing.returncode, 50)
            self.assertEqual(self.classify(missing), "persistence-fixture-not-regular")

            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_sync = fake_bin / "sync"
            fake_sync.write_text("#!/bin/sh\nexit 1\n")
            fake_sync.chmod(0o755)
            failed_flush = self.run_remote(command, path_prefix=fake_bin)
            self.assertEqual(failed_flush.returncode, 50)
            self.assertEqual(
                self.classify(failed_flush), "persistence-filesystem-flush-failed"
            )

    def test_persistence_creator_is_private_and_refuses_preexisting_symlinks(self):
        content = "synthetic create contract content"
        with tempfile.TemporaryDirectory(prefix="ci-kvm-persistence-create-") as directory:
            root = pathlib.Path(directory)
            fixture = root / "workspace with spaces" / "persisted fixture.bin"
            fixture.parent.mkdir(mode=0o700)

            created = self.run_remote(self.generated_persistence_create(fixture, content))
            self.assertEqual(created.returncode, 0, created.stderr.decode(errors="replace"))
            self.assertEqual(self.classify(created), "persistence-create-pass")
            self.assertEqual(fixture.read_text(), content)
            self.assertEqual(fixture.stat().st_uid, os.getuid())
            self.assertEqual(fixture.stat().st_gid, os.getgid())
            self.assertEqual(fixture.stat().st_mode & 0o777, 0o600)

            fixture.write_text("existing bytes must remain")
            fixture.chmod(0o600)
            refused = self.run_remote(self.generated_persistence_create(fixture, content))
            self.assertEqual(refused.returncode, 50)
            self.assertEqual(self.classify(refused), "persistence-fixture-path-preoccupied")
            self.assertEqual(fixture.read_text(), "existing bytes must remain")

            fixture.unlink()
            target = fixture.parent / "target"
            target.write_text("symlink target must remain")
            fixture.symlink_to(target.name)
            refused = self.run_remote(self.generated_persistence_create(fixture, content))
            self.assertEqual(refused.returncode, 50)
            self.assertEqual(self.classify(refused), "persistence-fixture-path-preoccupied")
            self.assertEqual(target.read_text(), "symlink target must remain")

    def test_persistence_probe_proves_path_owner_content_and_same_root_mount(self):
        content = b"synthetic persistence proof only"
        content_sha256 = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory(prefix="ci-kvm-persistence-") as directory:
            root = pathlib.Path(directory)
            fixture = root / "workspace with spaces" / "persisted fixture.bin"
            fixture.parent.mkdir(mode=0o700)
            fixture.write_bytes(content)
            fixture.chmod(0o600)

            pre_command = self.generated_persistence_check(fixture, content_sha256)
            pre = self.run_remote(pre_command)
            self.assertEqual(pre.returncode, 0, pre.stderr.decode(errors="replace"))
            self.assertEqual(self.classify(pre), "persistence-pass")
            self.assertIn(
                f"KVM_PERSISTENCE_CONTENT_SHA256={content_sha256}".encode(), pre.stdout
            )
            mount_line = next(
                line for line in pre.stdout.decode().splitlines()
                if line.startswith("KVM_PERSISTENCE_MOUNT_SHA256=")
            )
            mount_sha256 = mount_line.split("=", 1)[1]
            self.assertRegex(mount_sha256, r"^[0-9a-f]{64}$")

            post = self.run_remote(
                self.generated_persistence_check(
                    fixture, content_sha256, mount_sha256=mount_sha256
                )
            )
            self.assertEqual(post.returncode, 0, post.stderr.decode(errors="replace"))
            self.assertEqual(self.classify(post), "persistence-pass")

            missing = fixture.parent / "missing.bin"
            result = self.run_remote(
                self.generated_persistence_check(missing, content_sha256)
            )
            self.assertEqual(result.returncode, 50)
            self.assertEqual(self.classify(result), "persistence-fixture-missing")

            wrong_home = self.run_remote(
                self.generated_persistence_check(
                    fixture, content_sha256, home=pathlib.Path("/not/the/ssh/home")
                )
            )
            self.assertEqual(self.classify(wrong_home), "persistence-home-mismatch")

            wrong_uid = self.run_remote(
                self.generated_persistence_check(
                    fixture, content_sha256, uid=os.getuid() + 1
                )
            )
            self.assertEqual(self.classify(wrong_uid), "persistence-identity-mismatch")

            fixture.chmod(0o644)
            result = self.run_remote(
                self.generated_persistence_check(fixture, content_sha256)
            )
            self.assertEqual(self.classify(result), "persistence-fixture-mode-mismatch")
            fixture.chmod(0o600)

            fixture.write_bytes(b"different content")
            fixture.chmod(0o600)
            result = self.run_remote(
                self.generated_persistence_check(fixture, content_sha256)
            )
            self.assertEqual(self.classify(result), "persistence-fixture-content-mismatch")

            fixture.chmod(0o600)
            fixture.parent.chmod(0o755)
            bad_parent = self.run_remote(
                self.generated_persistence_check(fixture, content_sha256)
            )
            self.assertEqual(self.classify(bad_parent), "persistence-parent-owner-or-mode")
            fixture.parent.chmod(0o700)

            fixture.unlink()
            target = fixture.parent / "outside-target"
            target.write_bytes(content)
            target.chmod(0o600)
            fixture.symlink_to(target.name)
            result = self.run_remote(
                self.generated_persistence_check(fixture, content_sha256)
            )
            self.assertEqual(self.classify(result), "persistence-fixture-symlink")

            fixture.unlink()
            fixture.write_bytes(content)
            fixture.chmod(0o600)
            result = self.run_remote(
                self.generated_persistence_check(
                    fixture, content_sha256,
                    mount_sha256="0" * 64,
                )
            )
            self.assertEqual(self.classify(result), "persistence-mount-identity-changed")

            fake_bin = root / "fake-findmnt"
            fake_bin.mkdir()
            fake_findmnt = fake_bin / "findmnt"
            fake_findmnt.write_text("#!/bin/sh\nprintf '%s\\n' 'workspace 0:99 tmpfs'\n")
            fake_findmnt.chmod(0o755)
            nonroot = self.run_remote(
                self.generated_persistence_check(fixture, content_sha256),
                path_prefix=fake_bin,
            )
            self.assertEqual(
                self.classify(nonroot), "persistence-fixture-not-on-root-filesystem"
            )

            fake_findmnt.write_text("#!/bin/sh\nexit 1\n")
            unavailable = self.run_remote(
                self.generated_persistence_check(fixture, content_sha256),
                path_prefix=fake_bin,
            )
            self.assertEqual(
                self.classify(unavailable), "persistence-mount-identity-unavailable"
            )


if __name__ == "__main__":
    unittest.main()
