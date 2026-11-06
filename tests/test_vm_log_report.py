"""Synthetic log redaction/collection fixtures; no VM, credentials or SSH server."""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("log_report", ROOT / "scripts/vm-log-report.py")
logs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(logs)
spec = importlib.util.spec_from_file_location("log_paths", ROOT / "lib/vm/paths.py")
paths = importlib.util.module_from_spec(spec)
spec.loader.exec_module(paths)


class LogTests(unittest.TestCase):
    def test_public_private_keys_fingerprints_tokens_urls_and_ansi_are_redacted(self):
        raw = (b"package installed\n\x1b[31mGUEST BOOTSTRAP FAILED: download HTTP 403\x1b[0m\n"
               b"ssh-ed25519 " + b"A" * 60 + b" comment\n"
               b"SHA256:fixture-fingerprint\n"
               b"| ssh-ed25519 | 01:3b:86:83:51:2d:b0:94:8e:0e:95:39 |\n"
               b"-----BEGIN OPENSSH PRIVATE KEY-----\nshort-private-fragment\n"
               b"-----END OPENSSH PRIVATE KEY-----\n"
               b"Authorization: Bearer SECRET_CANARY\n"
               b"GET https://user:SECRET_CANARY@example.invalid/path?x=SECRET_CANARY\n")
        result = logs.redact(raw)
        self.assertIn("package installed", result)
        self.assertIn("download HTTP 403", result)
        for forbidden in ("A" * 60, "fixture-fingerprint", "01:3b", "short-private-fragment",
                          "SECRET_CANARY", "user:", "\x1b"):
            self.assertNotIn(forbidden, result)

    def test_partial_key_block_tail_is_redacted(self):
        self.assertNotIn("short-fragment", logs.redact(
            b"short-fragment\n-----END OPENSSH PRIVATE KEY-----\npackage ok\n"))

    def test_earlier_error_survives_shutdown_noise(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qemu_1.log"
            path.write_bytes(b"curl: HTTP error 403\n" + b"stopping service\n" * 150)
            path.chmod(0o600)
            report = logs.local_tail(path, paths)
            self.assertNotIn("403", report["excerpt"])
            self.assertIn("403", report["error_excerpt"])

    def test_failure_context_keeps_preceding_command_details_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qemu_1.log"
            path.write_bytes(b"installer returned 17\napi_token=SECRET_CANARY\n"
                             b"Failed to run module scripts_user\n" + b"stopping service\n" * 150)
            path.chmod(0o600)
            report = logs.local_tail(path, paths)
            self.assertIn("installer returned 17", report["failure_context"])
            self.assertNotIn("SECRET_CANARY", report["failure_context"])

    def test_local_reads_are_bounded_and_refuse_symlinks_hardlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qemu_1.log"
            path.write_bytes(b"X" * (logs.LIMIT * 2) + b"\nfinal failure\n")
            path.chmod(0o600)
            report = logs.local_tail(path, paths)
            self.assertEqual(report["state"], "collected")
            self.assertIn("final failure", report["excerpt"])
            self.assertLess(len(report["excerpt"]), 2000)
            link = path.with_name("linked.log")
            link.symlink_to(path)
            self.assertNotEqual(logs.local_tail(link, paths)["state"], "collected")
            link.unlink()
            os.link(path, link)
            self.assertEqual(logs.local_tail(path, paths)["state"], "unsafe")

    def test_guest_collection_refuses_unverified_listener_without_starting_ssh(self):
        process = mock.Mock()
        process.registry_read.return_value = {"processes": {}}
        with mock.patch.object(logs.subprocess, "Popen") as start:
            report = logs.guest_logs(Path("/profile"), Path("/run/run-one"), process, ROOT)
        self.assertEqual(report["state"], "unavailable-no-unique-verified-listener")
        start.assert_not_called()

    def test_guest_capture_redacts_output_and_bounds_a_flood(self):
        process = mock.Mock()
        process.registry_read.return_value = {"processes": {"qemu_1": {"identity": {
            "pid": 123, "executable": "/usr/bin/qemu-system-x86_64",
            "argv": ["file=/profile/disk.qcow2,format=qcow2,if=virtio",
                     "hostfwd=tcp:127.0.0.1:40222-:22"],
        }}}}
        process.identity_state.return_value = "running"
        process.listener_owned_by.return_value = True
        real_popen = subprocess.Popen
        for code, expected in (("print('api_token: SECRET_CANARY')", "collected"),
                               ("import os; os.write(1,b'x'*1000000)", "output-limit")):
            def start(_command, code=code, **kwargs):
                self.assertIn("AGENT_EXECUTABLE_USER", _command[2])
                self.assertIn("AGENT_EXECUTABLE_ROOT", _command[2])
                self.assertIn("namei -l /usr/local/bin/opencode /usr/bin/opencode", _command[2])
                return real_popen([sys.executable, "-c", code], **kwargs)
            with mock.patch.object(logs.subprocess, "Popen", side_effect=start):
                report = logs.guest_logs(Path("/profile"), Path("/run/run-one"), process, ROOT)
            self.assertEqual(report["state"], expected)
            self.assertNotIn("SECRET_CANARY", report["excerpt"])


if __name__ == "__main__":
    unittest.main()
