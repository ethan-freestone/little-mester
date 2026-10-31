import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "vm-install-tools.sh"


class VmInstallToolsTests(unittest.TestCase):
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
        self.args_capture = self.root / "ssh-args.json"
        self.stdin_capture = self.root / "ssh-stdin"
        mock_ssh = self.bin / "ssh"
        mock_ssh.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['SSH_ARGS_CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n"
            "Path(os.environ['SSH_STDIN_CAPTURE']).write_bytes(sys.stdin.buffer.read())\n"
            "print('mock guest accepted installer payload')\n"
        )
        mock_ssh.chmod(0o755)
        self.env = os.environ.copy()
        self.env.update(
            PATH=f"{self.bin}:{self.env.get('PATH', '')}",
            VM_DIR_BASE=str(self.root / "vm"),
            VM_VERBOSE="1",
            VM_DEBUG="1",
            SSH_ARGS_CAPTURE=str(self.args_capture),
            SSH_STDIN_CAPTURE=str(self.stdin_capture),
        )

    def run_installer(self, profile="test-profile"):
        return subprocess.run(
            ["bash", str(INSTALLER), profile],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_installs_tools_only_in_the_selected_running_guest(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Installing Bats and ShellCheck into the running VM 'test-profile'", result.stdout)
        self.assertIn("[verbose] Guest package manager: apt", result.stdout)
        self.assertIn("[debug]", result.stdout)

        ssh_args = json.loads(self.args_capture.read_text())
        self.assertIn("22422", ssh_args)
        self.assertIn("mester@127.0.0.1", ssh_args)
        self.assertIn("sudo bash -s -- '1'", ssh_args)

        payload = self.stdin_capture.read_text()
        self.assertIn("apt-get update", payload)
        self.assertIn("apt-get install -y bats shellcheck", payload)
        self.assertIn("bats --version", payload)
        self.assertIn("shellcheck --version", payload)
        self.assertIn("if (( debug_level >= 1 )); then", payload)

    def test_refuses_a_stopped_vm_without_ssh(self):
        (self.vm_dir / "qemu.pid").write_text("2147483647\n")
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("VM 'test-profile' is not running", result.stderr)
        self.assertFalse(self.args_capture.exists())


if __name__ == "__main__":
    unittest.main()
