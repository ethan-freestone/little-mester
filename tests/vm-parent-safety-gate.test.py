#!/usr/bin/env python3
"""Focused tests for the read-only parent safety-gate process scanner."""

import contextlib
import importlib.util
import io
import json
import os
import pathlib
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
    def test_process_fixture_reports_all_matching_pids(self):
        with tempfile.NamedTemporaryFile() as held:
            info = os.fstat(held.fileno())
            fixture = (f"p1\ncinit\nf3\nD{info.st_dev:#x}\ni{info.st_ino}\n"
                       f"p42\ncrunner\nf8\nD{info.st_dev:#x}\ni{info.st_ino}\n")
            self.assertEqual(gate._parse_lsof(fixture, "", 0, info.st_dev, info.st_ino), [1, 42])

    def test_empty_or_incomplete_process_fixture_is_refused(self):
        with self.assertRaisesRegex(gate.GateRefused, "no matching open descriptor"):
            gate._parse_lsof("", "", 0, 1, 2)
        # An lsof warning about an unreadable live FD table is a hard failure,
        # even if its partial stdout contains the exact target holder.
        fixture = "p42\ncrunner\nf8\nD0xfd01\ni77\n"
        with self.assertRaisesRegex(gate.GateRefused, "incomplete or failed"):
            gate._parse_lsof(fixture, "lsof: WARNING: cannot inspect PID 43", 0,
                             64769, 77)
        with self.assertRaisesRegex(gate.GateRefused, "incomplete or failed"):
            gate._parse_lsof(fixture, "", 1, 64769, 77)

    def test_missing_device_inode_fields_are_refused(self):
        with self.assertRaisesRegex(gate.GateRefused, "omitted device/inode"):
            gate._parse_lsof("p42\ncworker\nf8\n", "", 0, 1, 2)

    def test_real_local_temp_fd_and_ordinary_process_churn(self):
        lsof = pathlib.Path("/usr/bin/lsof")
        self.assertTrue(lsof.is_file())
        with tempfile.NamedTemporaryFile() as held:
            info = os.fstat(held.fileno())
            target = f"/proc/{os.getpid()}/fd/{held.fileno()}"
            churn = subprocess.Popen(
                [sys.executable, "-c",
                 "import subprocess; print('ready', flush=True); "
                 "[subprocess.run(['/bin/true'], check=True) for _ in range(120)]"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                self.assertEqual(churn.stdout.readline().strip(), b"ready")
                result = subprocess.run([str(lsof), "-FpcfDi", "--", target],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, timeout=10, check=False)
                self.assertEqual(
                    gate._parse_lsof(result.stdout, result.stderr, result.returncode,
                                     info.st_dev, info.st_ino), [os.getpid()])
            finally:
                churn.wait(timeout=10)
                churn.stdout.close()

    def test_gate_process_and_pid_one_are_visible_without_static_tree_requirement(self):
        gate._check_proc_visibility()

    def test_privileged_pid_one_check_uses_only_system_lsof(self):
        completed = subprocess.CompletedProcess(
            ["sudo", "lsof"], 0, stdout="p1\ncsystemd\n", stderr="")
        with (mock.patch.object(gate, "_system_executable",
                                side_effect=lambda name: pathlib.Path("/usr/bin") / name) as trusted,
              mock.patch.object(gate.subprocess, "run", return_value=completed) as run):
            gate._check_pid_one_with_lsof()
        self.assertEqual(trusted.call_args_list, [mock.call("sudo"), mock.call("lsof")])
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["/usr/bin/sudo", "/usr/bin/lsof"])
        self.assertIn("-p", argv)
        self.assertNotIn(str(ROOT), " ".join(argv))

    def test_privileged_scan_executes_only_protected_system_utilities(self):
        with tempfile.NamedTemporaryFile() as held:
            info = os.fstat(held.fileno())
            stdout = f"p{os.getpid()}\nctest\nf3\nD{info.st_dev:#x}\ni{info.st_ino}\n"
            completed = subprocess.CompletedProcess(
                ["sudo", "lsof"], 0, stdout=stdout, stderr="")
            target = pathlib.Path(f"/proc/{os.getpid()}/fd/{held.fileno()}")
            with (mock.patch.object(gate, "_system_executable",
                                    side_effect=lambda name: pathlib.Path("/usr/bin") / name) as trusted,
                  mock.patch.object(gate.subprocess, "run", return_value=completed) as run):
                self.assertEqual(
                    gate._scan_with_lsof(target, info.st_dev, info.st_ino), [os.getpid()])
            self.assertEqual(trusted.call_args_list, [mock.call("sudo"), mock.call("lsof")])
            argv = run.call_args.args[0]
            self.assertEqual(argv[:2], ["/usr/bin/sudo", "/usr/bin/lsof"])
            self.assertNotIn(str(ROOT), " ".join(argv))


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
              mock.patch.object(gate, "_check_proc_visibility"),
              mock.patch.object(gate, "_check_pid_one_with_lsof"),
              mock.patch.object(gate, "_scan_with_lsof", return_value=[os.getpid()]),
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
