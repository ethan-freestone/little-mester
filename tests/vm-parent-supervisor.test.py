#!/usr/bin/env python3
"""REAL_PROCESS tests for terminal-independent supervisor lock handoff."""

import base64
import fcntl
import hashlib
import importlib.util
import json
import os
import pathlib
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
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
                self.assertTrue(key_arg.startswith("/proc/self/fd/"))
                self.assertTrue(known_hosts_arg.startswith("/proc/self/fd/"))
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
            PARENT_READY_COMMAND=parent_state.PARENT_READY_COMMAND,
            exec_transition_status=lambda _transition: "TARGET",
            parent_ssh_plan=ssh_plan,
        )

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return SimpleNamespace(returncode=0)

        with (mock.patch.object(parent_supervisor, "_protected_tool", return_value="/usr/bin/ssh"),
              mock.patch.object(parent_supervisor.subprocess, "run", side_effect=fake_run)):
            ready = parent_supervisor._wait_for_guest_readiness(
                fake_state, None, {"target_path": "child-qemu.target.json"},
                stop_requested=lambda: False, timeout_seconds=1)
        self.assertTrue(ready)
        self.assertEqual(len(calls), 1)
        argv, kwargs = calls[0]
        self.assertEqual(argv[-1], fake_state.PARENT_READY_COMMAND)
        self.assertEqual(len(kwargs["pass_fds"]), 2)
        self.assertTrue(all(fd >= 3 for fd in kwargs["pass_fds"]))
        self.assertTrue(kwargs["close_fds"])
        self.assertEqual(kwargs["timeout"], 8)

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
