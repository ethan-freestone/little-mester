"""Exercise generated cloud-init scripts without provisioning the test machine."""
import os
import json
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
                        CAPTURE=str(self.path / "user-data"),
                        CAPTURE_SUBID=str(self.path / "subids.sh"))
        subprocess.run(["bash", "-c", 'source "$1/lib/vm/cloud-init.sh"; '
                        'log() { :; }; vm_cloud_init_generate "$2/vm" demo 1234 2345 "ssh-ed25519 test"',
                        "_", str(ROOT), str(self.path)], env=self.env, check=True)
        subprocess.run(["bash", "-c", 'source "$1/lib/vm/cloud-init.sh"; '
                        'vm_cloud_init_subid_script > "$CAPTURE_SUBID"',
                        "_", str(ROOT)], env=self.env, check=True)
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
        self.assertLess(provision.index("/home/mester/.config/docker/daemon.json"),
                        provision.index("dockerd-rootless-setuptool.sh install"))
        self.assertEqual(self.content("/etc/modules-load.d/mester-rootless.conf"), "nf_tables\n")
        self.assertIn("kmod iptables", provision)
        self.assertNotIn("--skip-iptables", provision)
        self.assertNotIn("--iptables=false", provision)
        self.assertIn("systemctl --user enable docker", provision)
        self.assertIn("systemctl --user restart docker", provision)
        self.assertLess(provision.index("mester-configure-subids mester"),
                        provision.index("dockerd-rootless-setuptool.sh install"))
        helper = self.content("/usr/local/sbin/mester-configure-subids")
        self.assertIn("usermod \"$option\"", helper)
        self.assertIn("overlaps another account", helper)
        subprocess.run(["bash", "-n"], input=helper, text=True, check=True)
        proxy_match = re.search(
            r"cat > /home/mester/\.config/docker/daemon\.json <<'DOCKER_PROXY'\n(.*?)\nDOCKER_PROXY\n",
            provision, re.DOTALL)
        self.assertIsNotNone(proxy_match, "rootless Docker daemon proxy config")
        proxy_config = json.loads(proxy_match[1])
        self.assertEqual(proxy_config["proxies"]["http-proxy"], "http://10.0.2.100:3128")
        self.assertEqual(proxy_config["proxies"]["https-proxy"], "http://10.0.2.100:3128")

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
        for name in ["/usr/local/sbin/mester-load-netfilter", "/usr/local/sbin/mester-configure-subids",
                     "/usr/local/sbin/mester-provision"]:
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

    def test_subid_helper_adds_exact_uid_and_gid_ranges_idempotently(self):
        uid_file = self.path / "subuid"
        gid_file = self.path / "subgid"
        calls = self.path / "usermod.calls"
        uid_file.touch(); gid_file.touch()
        self.command("usermod", 'printf "%s %s %s\\n" "$1" "$2" "$3" >> "$CALLS"\n'
                     'case "$1" in\n'
                     '  --add-subuids) printf "mester:100000:65536\\n" >> "$MESTER_SUBUID_FILE" ;;\n'
                     '  --add-subgids) printf "mester:100000:65536\\n" >> "$MESTER_SUBGID_FILE" ;;\n'
                     '  *) exit 9 ;;\n'
                     'esac\n')
        env = dict(self.env, MESTER_SUBUID_FILE=str(uid_file), MESTER_SUBGID_FILE=str(gid_file),
                   CALLS=str(calls))
        helper = str(self.path / "subids.sh")
        subprocess.run(["bash", helper, "mester"], env=env, check=True)
        subprocess.run(["bash", helper, "mester"], env=env, check=True)
        self.assertEqual(uid_file.read_text(), "mester:100000:65536\n")
        self.assertEqual(gid_file.read_text(), "mester:100000:65536\n")
        self.assertEqual(len(calls.read_text().splitlines()), 2)

    def test_subid_helper_refuses_overlapping_ranges_for_another_user(self):
        uid_file = self.path / "subuid"
        gid_file = self.path / "subgid"
        uid_file.write_text("ubuntu:100000:65536\n")
        gid_file.touch()
        calls = self.path / "usermod.calls"
        self.command("usermod", 'echo "$*" >> "$CALLS"\n')
        env = dict(self.env, MESTER_SUBUID_FILE=str(uid_file), MESTER_SUBGID_FILE=str(gid_file),
                   CALLS=str(calls))
        result = subprocess.run(["bash", str(self.path / "subids.sh"), "mester"], env=env,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("overlaps another account", result.stderr)
        self.assertFalse(calls.exists())


if __name__ == "__main__":
    unittest.main()
