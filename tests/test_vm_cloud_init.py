"""Exercise generated cloud-init scripts without provisioning the test machine."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CloudInitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.bin = self.path / "bin"
        self.bin.mkdir()
        # Capture the actual generator input, not grep matches in the template.
        self.command("genisoimage", 'cp "${@: -2:1}" "$CAPTURE"\n')
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        CAPTURE=str(self.path / "user-data"))
        subprocess.run(["bash", "-c", 'source "$1/lib/vm/cloud-init.sh"; '
                        'log() { :; }; vm_cloud_init_generate "$2/vm" demo 1234 2345 "ssh-ed25519 test"',
                        "_", str(ROOT), str(self.path)], env=self.env, check=True)
        self.data = (self.path / "user-data").read_text()

    def command(self, name, body):
        script = self.bin / name
        script.write_text("#!/bin/bash\n" + body)
        script.chmod(0o755)

    def content(self, name):
        match = re.search(r"  - path: " + re.escape(name) +
                          r"\n(?:    [^\n]*\n)*?    content: \|\n((?:      [^\n]*\n)+)", self.data)
        self.assertIsNotNone(match, name)
        return textwrap.dedent(match[1])

    def test_netfilter_loads_as_root_before_rootless_install_and_persists(self):
        provision = self.content("/usr/local/sbin/mester-provision")
        self.assertLess(provision.index("/usr/local/sbin/mester-load-netfilter"),
                        provision.index("dockerd-rootless-setuptool.sh install"))
        self.assertLess(provision.index("docker.service.d/proxy.conf"),
                        provision.index("dockerd-rootless-setuptool.sh install"))
        self.assertEqual(self.content("/etc/modules-load.d/mester-rootless.conf"), "nf_tables\n")
        self.assertIn("kmod iptables", provision)
        self.assertNotIn("--skip-iptables", provision)
        self.assertNotIn("--iptables=false", provision)
        self.assertIn("systemctl --user enable --now docker", provision)
        self.assertNotIn("systemctl --user restart docker", provision)

    def test_generated_helper_loads_nf_tables_and_propagates_failure(self):
        helper = self.content("/usr/local/sbin/mester-load-netfilter")
        self.assertIn("$(uname -r)", helper)  # Must expand in guest, not on host.
        self.command("modprobe", 'printf "%s\\n" "$*" > "$CAPTURE.module"\nexit "${MODULE_EXIT:-0}"\n')
        result = subprocess.run(["bash", "-c", helper], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.path / "user-data.module").read_text(), "nf_tables\n")
        self.command("uname", 'echo test-guest-kernel\n')
        result = subprocess.run(["bash", "-c", helper], env=dict(self.env, MODULE_EXIT="1"),
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("guest kernel test-guest-kernel cannot load nf_tables", result.stderr)

    def test_generated_shell_scripts_parse(self):
        for name in ["/usr/local/sbin/mester-load-netfilter", "/usr/local/sbin/mester-provision"]:
            subprocess.run(["bash", "-n"], input=self.content(name), text=True, check=True)

    def test_empty_group_uses_string_not_invalid_empty_membership_mapping(self):
        # Actual Ubuntu guest error: groups.0.mester: [] is too short.
        # A string creates the empty group; a mapping's member list cannot be [].
        groups = re.search(r"^groups:\n((?:  .+\n)+)", self.data, re.MULTILINE)
        self.assertIsNotNone(groups)
        self.assertEqual(groups[1], "  - mester\n")
        self.assertIn("    primary_group: mester\n", self.data)
        self.assertNotIn("mester: []", self.data)

    def test_schema_check_script_validates_rendered_config_and_propagates_failure(self):
        # The real CLI schema check runs separately in CI; here test its wiring.
        self.command("ssh-keygen", 'printf "ssh-ed25519 test-validation-key\\n" > "${@: -1}.pub"\n')
        self.command("cloud-init", 'test "$1" = schema && test "$2" = --config-file || exit 9\n'
                     'cp "$3" "$CAPTURE.validated"\nexit "${SCHEMA_EXIT:-0}"\n')
        command = ["bash", str(ROOT / "scripts/check-vm-cloud-init-schema.sh")]
        subprocess.run(command, env=self.env, check=True)
        rendered = (self.path / "user-data.validated").read_text()
        self.assertIn("hostname: schema-test\n", rendered)
        self.assertIn("groups:\n  - mester\n", rendered)
        result = subprocess.run(command, env=dict(self.env, SCHEMA_EXIT="7"))
        self.assertEqual(result.returncode, 7)


if __name__ == "__main__":
    unittest.main()
