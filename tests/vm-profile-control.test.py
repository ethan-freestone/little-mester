#!/usr/bin/env python3
"""Focused protocol and signed-artifact tests for the named-profile control path."""

from __future__ import annotations

import importlib.util
import importlib.machinery
import hashlib
import io
import json
import os
import pathlib
import pwd
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(
        name, path, loader=importlib.machinery.SourceFileLoader(name, str(path)))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CONTROL = load("test_profile_control", ROOT / "lib/vm/profile-control.py")
INSTALLER = load("test_vm_system_install", ROOT / "tools/vm-system-install")


class RequestTests(unittest.TestCase):
    def read_request(self, payload: bytes) -> dict:
        stdin = io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8")
        with mock.patch.object(CONTROL.sys, "stdin", stdin):
            return CONTROL._read_request()

    def test_lifecycle_protocol_has_only_name_and_operation(self):
        request = self.read_request(b'{"schema_version":1,"operation":"status","name":"dev"}\n')
        self.assertEqual(request, {"operation": "status", "name": "dev", "runtime_sha": None})

    def test_install_protocol_requires_complete_sha(self):
        request = self.read_request((json.dumps({
            "schema_version": 1, "operation": "install", "name": "dev",
            "runtime_sha": "a" * 40,
        }) + "\n").encode())
        self.assertEqual(request["runtime_sha"], "a" * 40)
        for sha in ("a" * 39, "A" * 40, "../" + "a" * 37):
            with self.subTest(sha=sha), self.assertRaises(CONTROL.Refused):
                self.read_request((json.dumps({
                    "schema_version": 1, "operation": "install", "name": "dev",
                    "runtime_sha": sha,
                }) + "\n").encode())

    def test_request_rejects_duplicate_keys_unknown_fields_reserved_names_and_bounds(self):
        bad = (
            b'{"schema_version":1,"operation":"up","name":"dev","name":"other"}',
            b'{"schema_version":1,"operation":"up","name":"dev","path":"/tmp"}',
            b'{"schema_version":1,"operation":"up","name":"system"}',
            b'{"schema_version":true,"operation":"up","name":"dev"}',
            b'{"schema_version":1,"operation":"up","name":"../escape"}',
            b"{" + b" " * CONTROL.MAX_REQUEST,
        )
        for payload in bad:
            with self.subTest(payload=payload[:80]), self.assertRaises(CONTROL.Refused):
                self.read_request(payload)

    def test_ssh_is_fixed_to_loopback_and_ed25519_without_remote_options(self):
        instance = CONTROL._instance("dev", 1000, "a" * 40)
        lifecycle = CONTROL._system_vm()
        argv = lifecycle.guest_ssh_argv(
            ssh_port=instance.ssh_port, identity_file=instance.control_key,
            known_hosts=instance.known_hosts, tty=True)
        self.assertIn("-tt", argv)
        self.assertIn("mester@127.0.0.1", argv)
        self.assertIn("HostKeyAlgorithms=ssh-ed25519", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertEqual(argv[-1], "mester@127.0.0.1")
        self.assertNotIn("-o", argv[argv.index("mester@127.0.0.1") + 1:])

    def test_profile_vm_unit_uses_systemd_readiness_and_shared_shutdown(self):
        instance = CONTROL._instance("dev", 1000, "a" * 40)
        unit = CONTROL._expected_unit_text(instance, role="vm").decode("ascii")
        self.assertIn("User=1000\n", unit)
        self.assertIn("ExecStart=!/usr/bin/python3 -I /usr/libexec/little-mester-vm/profile-control service qemu dev\n",
                      unit)
        self.assertIn("ExecStartPost=!/usr/bin/python3 -I /usr/libexec/little-mester-vm/profile-control service guest-ready dev $MAINPID\n",
                      unit)
        self.assertIn("ExecStop=!/usr/bin/python3 -I /usr/libexec/little-mester-vm/profile-control service qmp-stop dev $MAINPID\n",
                      unit)
        readiness = CONTROL._profile_readiness_runtime(instance)
        self.assertIn(f"ReadWritePaths={instance.disk} {instance.qmp_runtime} {readiness} {CONTROL.CODE_LOCK}\n",
                      unit)
        self.assertIn("TimeoutStartSec=16min\nTimeoutStopSec=25s\n", unit)

    def test_readiness_credentials_are_verified_staged_private_and_removed(self):
        with tempfile.TemporaryDirectory(prefix="lm-profile-readiness-") as temporary:
            root = pathlib.Path(temporary)
            owner = pwd.getpwuid(os.getuid())
            profile_runtime = root / "runtime" / "dev"
            profile_runtime.mkdir(parents=True, mode=0o711)
            readiness = profile_runtime / "readiness"
            readiness.mkdir(mode=0o700)
            identity = root / "control_ed25519"
            known_hosts = root / "known_hosts"
            identity.write_bytes(b"private credential bytes\n")
            known_hosts.write_bytes(b"[127.0.0.1]:22223 ssh-ed25519 AQIDBA==\n")
            identity.chmod(0o600)
            known_hosts.chmod(0o600)
            identity_info = identity.stat()
            known_info = known_hosts.stat()
            instance = SimpleNamespace(
                qmp_runtime=profile_runtime / "vm",
                control_key=identity, known_hosts=known_hosts)
            mapping = {
                "owner_uid": owner.pw_uid,
                "provisioning": {"artifacts": {
                    "client_private": {"device": identity_info.st_dev,
                                       "inode": identity_info.st_ino},
                    "known_hosts": {"device": known_info.st_dev,
                                    "inode": known_info.st_ino,
                                    "sha256": hashlib.sha256(known_hosts.read_bytes()).hexdigest()},
                }},
            }
            CONTROL._prepare_guest_readiness_credentials(instance, mapping)
            for name, expected in (("identity", b"private credential bytes\n"),
                                   ("known_hosts", known_hosts.read_bytes())):
                staged = readiness / name
                self.assertEqual(staged.read_bytes(), expected)
                info = staged.stat()
                self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)),
                                 (owner.pw_uid, owner.pw_gid, 0o600))
            CONTROL._remove_readiness_credentials(
                readiness, expected_uid=owner.pw_uid, expected_gid=owner.pw_gid)
            self.assertEqual(list(readiness.iterdir()), [])

    def test_profile_seed_uses_shared_foundation_and_protected_session_contract(self):
        with tempfile.TemporaryDirectory(prefix="lm-profile-seed-") as temporary:
            root = pathlib.Path(temporary)
            vm_state = root / "vm"
            vm_state.mkdir()
            host_key = vm_state / "ssh_host_ed25519_key"
            control_key = root / "control_ed25519"
            host_private = "-----BEGIN OPENSSH PRIVATE KEY-----\nseed-test\n-----END OPENSSH PRIVATE KEY-----\n"
            host_public = "ssh-ed25519 AQIDBA== profile-host\n"
            client_public = "ssh-ed25519 BQYHCA== profile-client\n"
            host_key.write_text(host_private, encoding="ascii")
            pathlib.Path(str(host_key) + ".pub").write_text(host_public, encoding="ascii")
            pathlib.Path(str(control_key) + ".pub").write_text(client_public, encoding="ascii")
            instance = SimpleNamespace(name="dev", vm_user="profile-vm", vm_state=vm_state,
                                       host_key=host_key, control_key=control_key,
                                       seed=vm_state / "seed.iso")
            vm_user = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
            captured = {}

            def fake_genisoimage(argv, **_kwargs):
                captured["userdata"] = pathlib.Path(argv[-2]).read_bytes()
                captured["metadata"] = pathlib.Path(argv[-1]).read_bytes()
                output = pathlib.Path(argv[argv.index("-output") + 1])
                output.write_bytes(b"test-iso")
                return SimpleNamespace(returncode=0)

            with (mock.patch.object(CONTROL.pwd, "getpwnam", return_value=vm_user),
                  mock.patch.object(CONTROL.subprocess, "run", side_effect=fake_genisoimage)):
                CONTROL._seed_profile(instance, owner_uid=os.getuid())

            bootstrap = CONTROL._load_module(
                "profile_seed_test_bootstrap", CONTROL._lib_path("guest-bootstrap.py"))
            contract = CONTROL._load_module(
                "profile_seed_test_contract", CONTROL._lib_path("parent-session-contract.py"))
            expected = bootstrap.build_foundation(
                name="little-mester-profile-dev", uid=1000,
                ssh_key=client_public.strip(), workspace="disk",
                helper="/usr/local/libexec/lm-guest-user-dirs.py")
            contract.add_protected_session(
                expected, host_private=host_private, host_public=host_public.strip(),
                control_public=client_public.strip(),
                workspace_path=contract.DEFAULT_WORKSPACE)
            self.assertEqual(captured["userdata"], b"#cloud-config\n" + json.dumps(
                expected, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n")
            self.assertIn(b"local-hostname: little-mester-profile-dev\n", captured["metadata"])
            seed_info = instance.seed.stat()
            self.assertEqual(stat.S_IMODE(seed_info.st_mode), 0o600)
            self.assertEqual(instance.seed.read_bytes(), b"test-iso")

    def test_qemu_service_acquires_exclusive_lock_before_loading_mapping(self):
        order = []
        lock_fd = os.open(os.devnull, os.O_RDONLY)

        def take_lock(*, exclusive=False):
            order.append(("lock", exclusive))
            return lock_fd

        def load_profile(_name):
            order.append(("mapping", None))
            raise CONTROL.Refused("stop after verifying lock ordering")

        with (mock.patch.object(CONTROL.os, "geteuid", return_value=0),
              mock.patch.object(CONTROL, "_take_code_lock", side_effect=take_lock),
              mock.patch.object(CONTROL, "_load_profile", side_effect=load_profile),
              self.assertRaises(CONTROL.Refused)):
            CONTROL._service_qemu("dev")
        self.assertEqual(order, [("lock", True), ("mapping", None)])
        with self.assertRaises(OSError):
            os.fstat(lock_fd)


class RuntimeEnrollmentTests(unittest.TestCase):
    def test_owner_record_binds_runtime_root_to_normalized_v3_parent(self):
        record = {"schema_version": 2, "uid": 1000,
                  "runtime_root": "/srv/little-mester-ci/v3"}
        payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_gid=0,
                               st_nlink=1, st_size=len(payload))
        with (mock.patch.object(CONTROL.os, "lstat", return_value=info),
              mock.patch.object(CONTROL, "_read_bounded", return_value=payload),
              mock.patch.object(os.path, "realpath", return_value=record["runtime_root"])):
            self.assertEqual(CONTROL._read_owner_record(), record)

    def test_runtime_resolution_uses_enrolled_root_and_revalidates_ancestry(self):
        record = {"schema_version": 2, "uid": 1000,
                  "runtime_root": "/srv/little-mester-ci/v3"}
        with (mock.patch.object(CONTROL, "_read_owner_record", return_value=record),
              mock.patch.object(CONTROL, "_check_protected_ancestry") as check):
            self.assertEqual(CONTROL._runtime_root(), pathlib.Path(record["runtime_root"]))
        check.assert_called_once_with(pathlib.Path(record["runtime_root"]))


class SignedBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not pathlib.Path("/usr/bin/openssl").is_file():
            raise unittest.SkipTest("OpenSSL is unavailable")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lm-profile-control-")
        self.directory = pathlib.Path(self.temp.name)
        self.private_key = self.directory / "signing-key.pem"
        self.public_key = self.directory / "trust.pem"
        subprocess.run(["/usr/bin/openssl", "genpkey", "-algorithm", "ED25519",
                        "-out", str(self.private_key)], check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        subprocess.run(["/usr/bin/openssl", "pkey", "-in", str(self.private_key),
                        "-pubout", "-out", str(self.public_key)], check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)

        self.payloads = {
            "lib/vm/profile-control.py": (ROOT / "lib/vm/profile-control.py").read_bytes(),
            "sudoers/little-mester-vm-profile-control": (
                ROOT / "sudoers/little-mester-vm-profile-control").read_bytes(),
        }

    def tearDown(self):
        self.temp.cleanup()

    def bundle(self, *, extra: bool = False) -> bytes:
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            members = dict(self.payloads)
            if extra:
                members["unexpected.txt"] = b"not authorized"
            for name, payload in members.items():
                info = tarfile.TarInfo(name)
                info.mode = (0o755 if name == "lib/vm/profile-control.py" else 0o440)
                info.size = len(payload)
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                archive.addfile(info, io.BytesIO(payload))
        return stream.getvalue()

    def sign(self, bundle: bytes) -> bytes:
        bundle_path = self.directory / "bundle.tar"
        signature_path = self.directory / "bundle.sig"
        bundle_path.write_bytes(bundle)
        subprocess.run(["/usr/bin/openssl", "pkeyutl", "-sign", "-rawin",
                        "-inkey", str(self.private_key), "-in", str(bundle_path),
                        "-out", str(signature_path)], check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        return signature_path.read_bytes()

    def verify(self, bundle: bytes, signature: bytes):
        real_lstat = os.lstat

        def fake_lstat(path, *args, **kwargs):
            if pathlib.Path(path) == self.public_key:
                return os.stat_result((0o100644, 1, 1, 1, 0, 0, 0,
                                       0, 0, 0))
            return real_lstat(path, *args, **kwargs)

        with (mock.patch.object(INSTALLER, "PROFILE_CONTROL_TRUST_KEY", self.public_key),
              mock.patch.object(INSTALLER.os, "lstat", side_effect=fake_lstat)):
            return INSTALLER._verify_profile_control_bundle(bundle, signature)

    def test_exact_ed25519_bundle_verifies(self):
        bundle = self.bundle()
        signature = self.sign(bundle)
        payloads = self.verify(bundle, signature)
        self.assertEqual(payloads, self.payloads)
        self.assertEqual(payloads["sudoers/little-mester-vm-profile-control"],
                         INSTALLER.PROFILE_CONTROL_SUDOERS_BYTES)

    def test_tampered_signature_and_extra_tar_member_are_rejected(self):
        bundle = self.bundle()
        signature = self.sign(bundle)
        with self.assertRaises(RuntimeError):
            self.verify(bundle, bytes([signature[0] ^ 1]) + signature[1:])
        extra = self.bundle(extra=True)
        with self.assertRaises(RuntimeError):
            self.verify(extra, self.sign(extra))

    def test_sudoers_source_matches_exact_installed_policy(self):
        self.assertEqual(self.payloads["sudoers/little-mester-vm-profile-control"],
                         INSTALLER.PROFILE_CONTROL_SUDOERS_BYTES)

    def test_signed_control_install_enrolls_candidate_runtime_root(self):
        bundle = self.bundle()
        signature = self.sign(bundle)
        bundle_path = self.directory / "bundle.tar"
        signature_path = self.directory / "bundle.sig"
        runtime_root = self.directory / "v3"
        candidate = runtime_root / ("a" * 40)
        helper_source = candidate / "lib/vm/profile-control.py"
        helper_source.parent.mkdir(parents=True)
        helper_source.write_bytes(self.payloads["lib/vm/profile-control.py"])
        owner_record = self.directory / "profile-control-owner.json"
        marker = self.directory / "missing-code-update-marker"
        owner = SimpleNamespace(pw_uid=1001, pw_gid=1001)
        control_group = SimpleNamespace(gr_gid=991, gr_mem=["test-owner"])
        real_lstat = os.lstat
        real_run = subprocess.run

        def fake_lstat(path, *args, **kwargs):
            if pathlib.Path(path) == self.public_key:
                return os.stat_result((stat.S_IFREG | 0o644, 1, 1, 1, 0, 0, 0,
                                       0, 0, 0))
            return real_lstat(path, *args, **kwargs)

        def run_with_test_visudo(argv, *args, **kwargs):
            if argv[0] == "/usr/sbin/visudo":
                return subprocess.CompletedProcess(argv, 0)
            return real_run(argv, *args, **kwargs)

        with (mock.patch.object(INSTALLER.os, "geteuid", return_value=0),
              mock.patch.object(INSTALLER.grp, "getgrnam", return_value=control_group),
              mock.patch.object(INSTALLER.pwd, "getpwnam", return_value=owner),
              mock.patch.object(INSTALLER, "_ensure_code_update_lock", return_value=42),
              mock.patch.object(INSTALLER, "_release_code_update_lock"),
              mock.patch.object(INSTALLER, "CODE_UPDATE_MARKER", marker),
              mock.patch.object(INSTALLER, "PROFILE_CONTROL_TRUST_KEY", self.public_key),
              mock.patch.object(INSTALLER, "OWNER_RECORD", owner_record),
              mock.patch.object(INSTALLER.os, "lstat", side_effect=fake_lstat),
              mock.patch.object(INSTALLER, "ensure_dir"),
              mock.patch.object(INSTALLER, "install_file") as install_file,
              mock.patch.object(INSTALLER.subprocess, "run", side_effect=run_with_test_visudo)):
            INSTALLER.install_profile_control(
                candidate, "test-owner", str(bundle_path), str(signature_path))

        owner_record_calls = [call for call in install_file.call_args_list
                              if call.args[1] == owner_record]
        self.assertEqual(len(owner_record_calls), 1)
        self.assertEqual(owner_record_calls[0].args[2], 0o600)
        self.assertEqual(json.loads(owner_record_calls[0].kwargs["payload"]), {
            "schema_version": 2,
            "uid": owner.pw_uid,
            "runtime_root": str(runtime_root),
        })


if __name__ == "__main__":
    unittest.main()
