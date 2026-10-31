import json
import os
import pty
import select
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts" / "vm-update.sh"


class VmUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.vm_dir = self.root / "vm" / "test-profile"
        self.vm_dir.mkdir(parents=True)
        (self.vm_dir / "qemu.pid").write_text(f"{os.getpid()}\n")
        (self.vm_dir / "ssh_port").write_text("22422\n")
        (self.vm_dir / "known_hosts").write_text("test-host-key\n")
        (self.vm_dir / "id_vm_control").write_text("test-control-key\n")
        self.calls_file = self.root / "ssh-calls.jsonl"
        mock_ssh = self.bin / "ssh"
        mock_ssh.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "path = Path(os.environ['SSH_CALLS_FILE'])\n"
            "calls = path.read_text().splitlines() if path.exists() else []\n"
            "calls.append(json.dumps({'args': sys.argv[1:], 'stdin': sys.stdin.buffer.read().decode()}))\n"
            "path.write_text('\\n'.join(calls) + '\\n')\n"
            "latest = json.loads(calls[-1])\n"
            "if 'No interactive OpenCode TUI is attached' in latest['stdin']:\n"
            "    print('No interactive OpenCode TUI is attached to a guest terminal.')\n"
            "    helper_calls = sum('No interactive OpenCode TUI is attached' in json.loads(call)['stdin'] for call in calls)\n"
            "    if helper_calls == int(os.environ.get('SSH_TUI_FAIL_ON_CALL', '0')):\n"
            "        raise SystemExit(int(os.environ.get('SSH_TUI_STATUS', '3')))\n"
            "elif 'requested_opencode_version' in latest['stdin']:\n"
            "    print(os.environ.get('SSH_CHECK_OUTPUT', 'CURRENT OpenCode CLI 2.2.0'))\n"
            "else:\n"
            "    print('mock guest update applied')\n"
        )
        mock_ssh.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update(
            PATH=f"{self.bin}:{self.env.get('PATH', '')}",
            VM_DIR_BASE=str(self.root / "vm"),
            SSH_CALLS_FILE=str(self.calls_file),
            VM_YES="0",
        )

    def run_updater(self, *args, yes=None, check_output=None):
        env = self.env.copy()
        if yes is not None:
            env["VM_YES"] = str(yes)
        if check_output is not None:
            env["SSH_CHECK_OUTPUT"] = check_output
        return subprocess.run(
            ["bash", str(UPDATER), "test-profile", *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def ssh_calls(self):
        if not self.calls_file.exists():
            return []
        return [json.loads(line) for line in self.calls_file.read_text().splitlines()]

    def test_default_update_checks_without_mutating_when_current(self):
        result = self.run_updater(check_output="CURRENT OpenCode CLI 2.2.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No updates available", result.stdout)
        calls = self.ssh_calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("find_interactive_tui", calls[0]["stdin"])
        check_script = calls[1]["stdin"]
        self.assertIn("targets=(\"$@\")", check_script)
        self.assertIn("requested_opencode_version=\"$2\"", check_script)
        self.assertNotIn("pgrep", check_script)
        self.assertIn("apt-get update -qq", check_script)

    def test_detected_update_requires_confirmation_before_apply(self):
        result = self.run_updater("opencode", check_output="UPDATE  OpenCode CLI  2.2.0 -> 2.3.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("confirmation requires a terminal", result.stderr)
        self.assertEqual(len(self.ssh_calls()), 2)

    def test_interactive_cancel_does_not_apply_any_updates(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        env = self.env.copy()
        env["SSH_CHECK_OUTPUT"] = "UPDATE  OpenCode CLI  2.2.0 -> 2.3.0"
        process = subprocess.Popen(
            ["bash", str(UPDATER), "test-profile", "opencode"],
            env=env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
        )
        output = bytearray()
        while b"Apply the listed updates" not in output:
            ready, _, _ = select.select([master], [], [], 5)
            self.assertTrue(ready, "update confirmation prompt did not appear")
            output.extend(os.read(master, 4096))
        os.write(master, b"n\n")
        self.assertEqual(process.wait(timeout=5), 0)
        output.extend(os.read(master, 4096))
        self.assertIn(b"Update cancelled", output)
        self.assertEqual(len(self.ssh_calls()), 2)

    def test_yes_applies_only_the_component_with_an_update(self):
        result = self.run_updater(
            "opencode", yes="1", check_output="UPDATE  OpenCode CLI  2.2.0 -> 2.3.0"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.ssh_calls()
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[3]["args"][-1], "sudo bash -s -- 2.3.0 opencode")
        self.assertIn("npm install --global", calls[3]["stdin"])
        self.assertIn("has_target opencode", calls[3]["stdin"])
        self.assertIn("apply_targets=()", UPDATER.read_text())
        self.assertIn("grep -q '^UPDATE  OpenCode CLI '", UPDATER.read_text())

    def test_all_checks_guest_os_and_only_applies_reported_updates(self):
        result = self.run_updater(
            "--all", yes="1", check_output="UPDATE  Guest OS packages  2 package(s)\n  - docker-ce\n  - curl"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.ssh_calls()
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[3]["args"][-1], "sudo bash -s -- latest system")
        self.assertIn("apt-get -s upgrade", calls[1]["stdin"])
        self.assertIn("apt-get upgrade -y", calls[3]["stdin"])
        self.assertIn("systemctl --user restart docker", calls[3]["stdin"])

    def test_invalid_target_and_all_combination_refuse_before_ssh(self):
        result = self.run_updater("banana")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown update target", result.stderr)
        self.assertEqual(self.ssh_calls(), [])

        result = self.run_updater("--all", "opencode")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Do not combine --all", result.stderr)
        self.assertEqual(self.ssh_calls(), [])

    def test_stopped_vm_refuses_without_ssh(self):
        (self.vm_dir / "qemu.pid").write_text("2147483647\n")
        result = self.run_updater("opencode")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not running", result.stderr)
        self.assertEqual(self.ssh_calls(), [])

    def test_update_stops_only_when_guest_reports_an_attached_tui(self):
        env = self.env.copy()
        env["SSH_TUI_FAIL_ON_CALL"] = "1"
        env["SSH_TUI_STATUS"] = "3"
        result = subprocess.run(
            ["bash", str(UPDATER), "test-profile", "opencode"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(len(self.ssh_calls()), 1)
        self.assertIn("find_interactive_tui", self.ssh_calls()[0]["stdin"])

    def test_update_rechecks_for_a_tui_immediately_before_applying(self):
        env = self.env.copy()
        env.update(
            SSH_CHECK_OUTPUT="UPDATE  OpenCode CLI  2.2.0 -> 2.3.0",
            SSH_TUI_FAIL_ON_CALL="2",
            SSH_TUI_STATUS="3",
            VM_YES="1",
        )
        result = subprocess.run(
            ["bash", str(UPDATER), "test-profile", "opencode"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 3)
        calls = self.ssh_calls()
        self.assertEqual(len(calls), 3)
        self.assertIn("find_interactive_tui", calls[2]["stdin"])
        self.assertNotIn("npm install --global", "\n".join(call["stdin"] for call in calls))


if __name__ == "__main__":
    unittest.main()
