#!/usr/bin/env python3
"""Focused tests for the read-only parent safety-gate process scanner."""

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import signal
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "parent_readonly_safety_gate", ROOT / "scripts/parent-readonly-safety-gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
DIAG_SPEC = importlib.util.spec_from_file_location(
    "parent_ssh_endpoint_diagnostic", ROOT / "scripts/parent-ssh-endpoint-diagnostic.py")
diagnostic = importlib.util.module_from_spec(DIAG_SPEC)
DIAG_SPEC.loader.exec_module(diagnostic)


class ProcessScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sudo = "/usr/bin/sudo"
        created = subprocess.run([sudo, "-n", "/usr/bin/mktemp", "-d",
                                  "/opt/lm-procfs-test.XXXXXX"],
                                 capture_output=True, text=True, timeout=10, check=False)
        if created.returncode:
            raise AssertionError(f"CI cannot create protected scanner fixture: {created.stderr}")
        cls.protected_root = pathlib.Path(created.stdout.strip())
        subprocess.run([sudo, "-n", "/usr/bin/chmod", "0755", str(cls.protected_root)],
                       check=True, timeout=10)
        subprocess.run([sudo, "-n", "/usr/bin/install", "-d", "-o", "0", "-g", "0",
                        "-m", "0755", str(cls.protected_root / "scripts"),
                        str(cls.protected_root / "lib" / "vm")], check=True, timeout=10)
        cls.protected_helper = cls.protected_root / "scripts/parent-readonly-safety-gate.py"
        subprocess.run([sudo, "-n", "/usr/bin/install", "-o", "0", "-g", "0", "-m", "0644",
                        str(ROOT / "scripts/parent-readonly-safety-gate.py"),
                        str(cls.protected_helper)], check=True, timeout=10)
        cls.protected_state = cls.protected_root / "lib/vm/parent-state.py"
        subprocess.run([sudo, "-n", "/usr/bin/install", "-o", "0", "-g", "0", "-m", "0644",
                        str(ROOT / "lib/vm/parent-state.py"), str(cls.protected_state)],
                       check=True, timeout=10)

    @classmethod
    def tearDownClass(cls):
        sudo = "/usr/bin/sudo"
        subprocess.run([sudo, "-n", "/usr/bin/rm", "--", str(cls.protected_helper)],
                       check=True, timeout=10)
        subprocess.run([sudo, "-n", "/usr/bin/rm", "--", str(cls.protected_state)],
                       check=True, timeout=10)
        subprocess.run([sudo, "-n", "/usr/bin/rmdir", "--", str(cls.protected_root / "scripts"),
                        str(cls.protected_root / "lib/vm"), str(cls.protected_root / "lib")],
                       check=True, timeout=10)
        subprocess.run([sudo, "-n", "/usr/bin/rmdir", "--", str(cls.protected_root)],
                       check=True, timeout=10)

    def _privileged_scan(self, info, allowed_pid=0, allowed_fd=-1):
        if not pathlib.Path("/usr/bin/sudo").exists():
            self.fail("CI requires sudo for the real protected scanner integration")
        result = subprocess.run(
            ["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-B",
             str(self.protected_helper), "--scan-inode",
             str(info.st_dev), str(info.st_ino), str(allowed_pid), str(allowed_fd)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=40, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        value = json.loads(result.stdout)
        self.assertIs(value["complete"], True)
        self.assertGreater(value["processes_scanned"], 0)
        self.assertEqual(value["coverage"], {"procfs": True,
                                             "pid_namespace": "init-shared",
                                             "user_namespace": "init-shared",
                                             "descriptors": True, "file_mappings": True})
        return value["holders"]

    def test_real_privileged_scanner_finds_invoking_users_scratch_fd(self):
        with tempfile.NamedTemporaryFile() as held:
            info = os.fstat(held.fileno())
            holders = self._privileged_scan(info)
            self.assertIn([os.getpid(), "fd", held.fileno()], holders)

    def test_protected_installed_parent_state_invokes_protected_scanner(self):
        spec = importlib.util.spec_from_file_location("installed_parent_state_scanner_test",
                                                      self.protected_state)
        state = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(state)
        with tempfile.NamedTemporaryFile() as disk:
            info = os.fstat(disk.fileno())
            real_run = subprocess.run

            def run(argv, **kwargs):
                if argv == ["/usr/bin/sudo", "-v"]:
                    return subprocess.CompletedProcess(argv, 0)
                return real_run(argv, **kwargs)

            with mock.patch.object(state.subprocess, "run", side_effect=run):
                result = state._scan_stale_disk_holders(
                    disk.fileno(), (info.st_dev, info.st_ino),
                    scratch_dir=pathlib.Path(tempfile.gettempdir()))
            self.assertIsNone(result)

    def test_real_privileged_scanner_refuses_forged_caller_fd_exemption(self):
        with tempfile.NamedTemporaryFile() as held:
            info = os.fstat(held.fileno())
            result = subprocess.run(
                ["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-B",
                 str(self.protected_helper), "--scan-inode", str(info.st_dev),
                 str(info.st_ino), str(os.getpid()), "0"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=10, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn(b"invalid_caller_fd_exemption", result.stderr)

    def test_real_privileged_scanner_finds_cross_uid_holder(self):
        with tempfile.NamedTemporaryFile() as held:
            info = os.fstat(held.fileno())
            child = subprocess.Popen(
                ["/usr/bin/sudo", "-n", "/usr/bin/python3", "-c",
                 "import os,sys,time; f=open(sys.argv[1],'rb'); print('READY',os.getpid(),flush=True); time.sleep(20)",
                 held.name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True)
            try:
                ready = child.stdout.readline().split()
                self.assertEqual(ready[0], "READY")
                holder_pid = int(ready[1])
                holders = self._privileged_scan(info, os.getpid(), held.fileno())
                self.assertTrue(any(item[0] == holder_pid and item[1] == "fd" for item in holders),
                                holders)
            finally:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=5)
                child.stdout.close()
                child.stderr.close()

    def test_real_privileged_scanner_finds_mapping_after_fd_close(self):
        with tempfile.NamedTemporaryFile() as held:
            held.write(b"x" * 4096)
            held.flush()
            info = os.fstat(held.fileno())
            child = subprocess.Popen(
                [sys.executable, "-c",
                 "import mmap,os,sys,time; f=os.open(sys.argv[1],os.O_RDONLY); m=mmap.mmap(f,0,access=mmap.ACCESS_READ); os.close(f); print('READY',flush=True); time.sleep(20)",
                 held.name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "READY")
                holders = self._privileged_scan(info, os.getpid(), held.fileno())
                self.assertTrue(any(item[0] == child.pid and item[1] == "map" for item in holders),
                                holders)
            finally:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=5)
                child.stdout.close()
                child.stderr.close()

class SafetyGateCliIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.install = self.root / "install"
        (self.install / "lib/vm").mkdir(parents=True)
        self.state = self.root / "state"
        self.state.mkdir()
        self.disk = self.state / "disk.qcow2"
        self.disk.write_bytes(b"isolated fixture disk")
        disk_info = os.stat(self.disk)
        self.lab_vm = self.install / "lab-vm"
        self.lab_vm.write_text(
            "#!" + sys.executable + "\nimport json,sys\n"
            f"disk={{'format':'qcow2','device':{disk_info.st_dev},'inode':{disk_info.st_ino}}}\n"
            "if sys.argv[1:] == ['parent','status','--json']:\n"
            " print(json.dumps({'status':'STOPPED','disk':disk}))\n"
            "elif sys.argv[1:] == ['parent','recover','--dry-run','--json']:\n"
            " print(json.dumps({'dry_run':True,'action':'none','status':'STOPPED'}))\n"
            "else: raise SystemExit(2)\n")
        self.lab_vm.chmod(0o700)
        (self.install / "lib/vm/parent-state.py").write_text(
            "def exec_transition_status(identity):\n"
            "    return 'STOPPED' if identity.get('identity_path') in "
            "('child-proxy.identity.json','child-qemu.identity.json') else 'AMBIGUOUS'\n")
        for name in ("child-proxy.identity.json", "child-qemu.identity.json"):
            (self.state / name).write_text(json.dumps({"identity_path": name}))

    def tearDown(self):
        self.temp.cleanup()

    def _invoke(self):
        output, errors = io.StringIO(), io.StringIO()
        with (mock.patch.object(sys, "argv", ["parent-readonly-safety-gate.py",
                                               "--install-root", str(self.install),
                                               "--state-dir", str(self.state)]),
              mock.patch.object(gate.os, "geteuid", return_value=1000),
              mock.patch.object(gate, "_scan_with_privileged_procfs", side_effect=[
                  {"complete": True, "processes_scanned": 2,
                   "coverage": {"procfs": True, "pid_namespace": "init-shared",
                                "user_namespace": "init-shared", "descriptors": True,
                                "file_mappings": True},
                   "holders": [[os.getpid(), "fd", 9]]},
                  {"complete": True, "processes_scanned": 2,
                   "coverage": {"procfs": True, "pid_namespace": "init-shared",
                                "user_namespace": "init-shared", "descriptors": True,
                                "file_mappings": True}, "holders": []}]),
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors)):
            code = gate.main()
        return code, output.getvalue(), errors.getvalue()

    def test_actual_cli_loads_installed_module_and_validates_both_children(self):
        real_load = gate._load_state_module
        real_check = gate._check_children
        with (mock.patch.object(gate, "_load_state_module", wraps=real_load) as load,
              mock.patch.object(gate, "_check_children", wraps=real_check) as check):
            code, output, errors = self._invoke()
        self.assertEqual(code, 0, errors)
        self.assertIn("SAFETY GATE: PASS", output)
        self.assertEqual(load.call_count, 1)
        self.assertEqual(check.call_count, 1)

    def test_actual_cli_converts_unexpected_failure_to_refused_nonzero(self):
        with mock.patch.object(gate, "_check_children", side_effect=NameError("fixture")):
            code, output, errors = self._invoke()
        self.assertEqual(code, 2)
        self.assertNotIn("SAFETY GATE: PASS", output)
        self.assertIn("REFUSED: unexpected gate failure (NameError)", errors)
        self.assertIn("SAFETY GATE: FAIL", errors)


class OpenSSHHostLookupTests(unittest.TestCase):
    def test_parent_bracketed_loopback_port_matches_only_that_port(self):
        keygen = shutil.which("ssh-keygen")
        ssh = shutil.which("ssh")
        if not keygen or not ssh:
            self.skipTest("OpenSSH tools unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            private = root / "host"
            subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(private)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10)
            key_type, public_key, *_ = private.with_suffix(".pub").read_text().split()
            known_hosts = root / "known_hosts"
            known_hosts.write_text(f"[127.0.0.1]:46617 {key_type} {public_key}\n")
            exact = subprocess.run([keygen, "-F", "[127.0.0.1]:46617", "-f", str(known_hosts)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=5, check=False)
            other_port = subprocess.run(
                [keygen, "-F", "[127.0.0.1]:46618", "-f", str(known_hosts)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
            self.assertEqual(exact.returncode, 0)
            self.assertNotEqual(other_port.returncode, 0)
            config = subprocess.run([ssh, "-G", "-F", "/dev/null", "-p", "46617",
                                     "mester@127.0.0.1"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, timeout=5, check=True).stdout.splitlines()
            values = {line.split(None, 1)[0]: line.split(None, 1)[1]
                      for line in config if line.startswith(("hostname ", "port ", "hostkeyalias "))}
            self.assertEqual(values.get("hostname"), "127.0.0.1")
            self.assertEqual(values.get("port"), "46617")
            self.assertNotIn("hostkeyalias", values)

    def test_diagnostic_fingerprints_only_keys_and_sanitizes_rejection(self):
        keygen = shutil.which("ssh-keygen")
        if not keygen:
            self.skipTest("ssh-keygen unavailable")
        with tempfile.TemporaryDirectory() as directory:
            private = pathlib.Path(directory) / "host"
            subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(private)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10)
            public_line = private.with_suffix(".pub").read_bytes()
            expected = diagnostic._fingerprint(b"", public_file=private.with_suffix(".pub"))
            self.assertEqual(diagnostic._fingerprint(public_line), expected)
            self.assertEqual(
                diagnostic._rejection_label(
                    b"WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! secret-looking text"),
                "CHANGED_KEY",
            )
            self.assertEqual(
                diagnostic._server_fingerprint(
                    b"debug1: Server host key: ssh-ed25519 SHA256:"
                    b"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
                "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            )


if __name__ == "__main__":
    unittest.main()
