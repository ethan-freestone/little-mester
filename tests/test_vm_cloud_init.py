"""Secret-free NoCloud generation tests; no VM, network or package operations."""

import base64
import json
import os
import struct
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLOUD_INIT = ROOT / "lib/vm/cloud-init.sh"


def public_key(comment="fixture@invalid"):
    blob = struct.pack(">I", len(b"ssh-ed25519")) + b"ssh-ed25519" + bytes(range(32))
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}"


def render(*args, env=None):
    return subprocess.run(
        [
            "bash", "-c", 'source "$1"; shift; vm_cloud_init_user_data "$@"',
            "_", str(CLOUD_INIT), *map(str, args),
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


class CloudInitTests(unittest.TestCase):
    def test_generated_schema_and_scripts_are_valid(self):
        env = os.environ.copy()
        env["CANARY_SEED_SECRET"] = "must-not-enter-seed"
        env["MCP_API_TOKEN"] = "must-not-enter-seed-either"
        payload_hash = "a" * 64
        result = render(
            "fixture-vm", 1000, 1001, public_key(), 1, 2, "none", "opencode",
            "latest", payload_hash, env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("must-not-enter-seed", result.stdout)
        self.assertTrue(result.stdout.startswith("#cloud-config\n"))
        config = json.loads(result.stdout.split("\n", 1)[1])

        self.assertEqual(config["hostname"], "fixture-vm")
        self.assertFalse(config["ssh_pwauth"])
        self.assertTrue(config["disable_root"])
        self.assertIn("groupadd --gid 1001 mester", config["bootcmd"][0][2])
        user = config["users"][0]
        self.assertEqual(user["uid"], 1000)
        self.assertEqual(user["primary_group"], "mester")
        self.assertEqual(user["ssh_authorized_keys"], [public_key()])
        self.assertIn("sudo", user["groups"])
        self.assertIn("kvm", user["groups"])
        self.assertIn("groupadd kvm", config["bootcmd"][1][2])
        self.assertEqual(config["mounts"], [["workspace", "/workspace", "virtiofs", "defaults,nofail", "0", "0"]])

        files = {item["path"]: item for item in config["write_files"]}
        apt_proxy = files["/etc/apt/apt.conf.d/80lab-vm-proxy"]["content"]
        profile = files["/etc/profile.d/lab-vm-proxy.sh"]["content"]
        self.assertIn("Acquire::http::Proxy", apt_proxy)
        self.assertIn("Acquire::https::Proxy", apt_proxy)
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertIn(f"{name}=http://10.0.2.100:3128", profile)
        self.assertIn("NO_PROXY=localhost,127.0.0.1,10.0.2.100", profile)
        self.assertEqual(files["/etc/lab-vm-recursion"]["content"], "1 2\n")
        for target in (
            "/usr/local/sbin/lab-vm-guest-bootstrap",
            "/usr/local/sbin/lab-vm-python-tools",
            "/usr/local/libexec/lab-vm-opencode-config.py",
            "/usr/local/libexec/lab-vm-guest-hash.sh",
        ):
            entry = files[target]
            self.assertEqual(entry["owner"], "root:root")
            self.assertEqual(entry["permissions"], "0755")
            self.assertEqual(entry["encoding"], "b64")
            self.assertTrue(base64.b64decode(entry["content"], validate=True).startswith(b"#!"))
        command = config["runcmd"][0]
        self.assertEqual(command[0], "/usr/local/sbin/lab-vm-guest-bootstrap")
        self.assertEqual(command[command.index("--uid") + 1], "1000")
        self.assertEqual(command[command.index("--gid") + 1], "1001")
        self.assertEqual(command[command.index("--runtime") + 1], "none")
        self.assertEqual(command[command.index("--payload-sha256") + 1], payload_hash)
        self.assertEqual(command[command.index("--proxy") + 1], "http://10.0.2.100:3128")

    def test_rejects_unsafe_identity_and_key_values(self):
        bad_args = (
            ("bad/name", 1000, 1001, public_key(), 0, 0, "none", "opencode", "latest", "a" * 64),
            ("vm", 0, 1001, public_key(), 0, 0, "none", "opencode", "latest", "a" * 64),
            ("vm", 1000, 1001, public_key().replace(" ", "\n", 1), 0, 0, "none", "opencode", "latest", "a" * 64),
            ("vm", 1000, 1001, public_key(), 2, 1, "none", "opencode", "latest", "a" * 64),
            ("vm", 1000, 1001, public_key(), 0, 0, "docker", "aider", "latest", "a" * 64),
        )
        for args in bad_args:
            with self.subTest(args=args):
                result = render(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("#cloud-config", result.stdout)
                self.assertNotIn("CANARY", result.stderr)

    def test_nonrecursive_guest_has_no_kvm_group_or_recursion_bootcmd(self):
        result = render("fixture-vm", 1000, 1001, public_key(), 0, 0, "none", "opencode", "latest", "a" * 64)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(result.stdout.split("\n", 1)[1])
        self.assertEqual(len(config["bootcmd"]), 1)
        self.assertEqual(config["users"][0]["groups"], ["sudo"])

    def test_renderer_source_is_not_seeded_as_plaintext(self):
        result = render("vm", 1000, 1000, public_key(), 0, 0, "docker", "opencode", "latest", "b" * 64)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("access[_-]?token", result.stdout)
        self.assertNotIn('"apiKey"', result.stdout)
        config = json.loads(result.stdout.split("\n", 1)[1])
        encoded = next(item["content"] for item in config["write_files"] if item["path"].endswith("vm-opencode-config.py"))
        self.assertIn(b"authorization", base64.b64decode(encoded))


if __name__ == "__main__":
    unittest.main()
