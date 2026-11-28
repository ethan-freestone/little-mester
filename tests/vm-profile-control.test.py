#!/usr/bin/env python3
"""Focused protocol and signed-artifact tests for the named-profile control path."""

from __future__ import annotations

import importlib.util
import importlib.machinery
import io
import json
import os
import pathlib
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace


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
        argv = CONTROL._ssh_argv(instance, tty=True)
        self.assertIn("-tt", argv)
        self.assertIn("mester@127.0.0.1", argv)
        self.assertIn("HostKeyAlgorithms=ssh-ed25519", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertEqual(argv[-1], "mester@127.0.0.1")
        self.assertNotIn("-o", argv[argv.index("mester@127.0.0.1") + 1:])

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


if __name__ == "__main__":
    unittest.main()
