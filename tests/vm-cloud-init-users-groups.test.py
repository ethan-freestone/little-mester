"""Runtime-equivalent primary-group trace for Ubuntu's packaged cloud-init 26.1.

This optional test runs the installed cloud-init module and distro methods when
the exact Noble package is available. It skips elsewhere so the persistent
test suite remains usable on hosts without cloud-init. useradd -P confines all
account-file changes to disposable fixtures; the host account database is not
read or modified.
"""

from __future__ import annotations

import copy
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGE_VERSION = "26.1-0ubuntu1~24.04.1"
FIXTURE_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE7zJmK1W1h3V4eA7qQKX8U4n9x0z8m1c2b3d4e5f6g7 fixture"
)


def installed_package_version() -> str | None:
    binary = pathlib.Path("/usr/bin/cloud-init")
    if not binary.is_file():
        return None
    result = subprocess.run(
        [str(binary), "--version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return result.stdout.decode("utf-8", "replace").strip() if result.returncode == 0 else None


def generated_config():
    command = (
        f'source "{ROOT / "lib/vm/cloud-init.sh"}"; '
        f'VM_HOST_SCRIPT_ROOT="{ROOT}"; '
        f'vm_host_cloud_init smoke-v3 1234 "{FIXTURE_KEY}"'
    )
    output = subprocess.run(
        ["/usr/bin/bash", "-c", command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        text=True,
    ).stdout
    import yaml

    return yaml.safe_load(output)


def trace_cloud_init_users_groups(config):
    """Call installed cc_users_groups.handle and Distro methods with safe fakes."""
    from cloudinit.config import cc_users_groups
    from cloudinit.distros import Distro, subp, util

    groups_created: list[str] = []
    commands: list[list[str]] = []

    class DistroFixture:
        def get_default_user(self):
            return None

        def create_group(self, name, members=None):
            return Distro.create_group(self, name, members)

        def create_user(self, name, **kwargs):
            return Distro.add_user(self, name, **kwargs)

    def is_group(name):
        return name in groups_created

    def record_command(argv, **kwargs):
        argv = list(argv)
        commands.append(argv)
        if argv[0] == "groupadd":
            groups_created.append(argv[1])
        elif argv[0] == "useradd" and "--gid" in argv:
            primary_group = argv[argv.index("--gid") + 1]
            if primary_group not in groups_created:
                raise RuntimeError(f"useradd --gid {primary_group}: group does not exist")

    class CloudFixture:
        distro = DistroFixture()

        @staticmethod
        def get_public_ssh_keys():
            return []

    with (
        mock.patch.object(util, "is_user", return_value=False),
        mock.patch.object(util, "is_group", side_effect=is_group),
        mock.patch.object(util, "system_is_snappy", return_value=False),
        mock.patch.object(subp, "subp", side_effect=record_command),
    ):
        cc_users_groups.handle("users-groups", config, CloudFixture(), [])
    return groups_created, commands


def create_useradd_root(parent: pathlib.Path, group_present: bool) -> pathlib.Path:
    root = parent / ("with-group" if group_present else "without-group")
    etc = root / "etc"
    (etc / "default").mkdir(parents=True)
    (etc / "passwd").write_text("root:x:0:0:root:/root:/bin/sh\n", encoding="ascii")
    group = "root:x:0:\n"
    gshadow = "root:*::\n"
    if group_present:
        group += "mester:x:1234:\n"
        gshadow += "mester:!::\n"
    (etc / "group").write_text(group, encoding="ascii")
    (etc / "shadow").write_text("root:*:19000:0:99999:7:::\n", encoding="ascii")
    (etc / "gshadow").write_text(gshadow, encoding="ascii")
    (etc / "login.defs").write_text(
        "UID_MIN 1000\nUID_MAX 60000\nGID_MIN 1000\nGID_MAX 60000\nUSERGROUPS_ENAB yes\n",
        encoding="ascii",
    )
    (etc / "default/useradd").write_text("GROUP=100\nHOME=/home\nSHELL=/bin/sh\n", encoding="ascii")
    return root


@unittest.skipUnless(
    installed_package_version() == f"/usr/bin/cloud-init {PACKAGE_VERSION}",
    f"requires Ubuntu cloud-init {PACKAGE_VERSION}; no package is installed by this test",
)
class UbuntuCloudInitPrimaryGroupTests(unittest.TestCase):
    def test_production_generator_creates_group_before_primary_group_user(self):
        config = generated_config()
        self.assertEqual(config["groups"], ["mester"])
        self.assertEqual(config["users"][0]["name"], "mester")
        self.assertEqual(config["users"][0]["primary_group"], "mester")
        self.assertEqual(config["users"][0]["uid"], 1234)

        created, commands = trace_cloud_init_users_groups(copy.deepcopy(config))
        self.assertEqual(created, ["mester"])
        self.assertEqual(commands[0], ["groupadd", "mester"])
        useradd = next(command for command in commands if command[0] == "useradd")
        self.assertEqual(useradd[useradd.index("--gid") + 1], "mester")
        self.assertLess(commands.index(["groupadd", "mester"]), commands.index(useradd))

    def test_missing_top_level_group_reproduces_cloud_init_useradd_failure(self):
        broken = generated_config()
        broken.pop("groups", None)
        with self.assertRaisesRegex(RuntimeError, "group does not exist"):
            trace_cloud_init_users_groups(broken)

    def test_ubuntu_useradd_requires_named_gid_and_succeeds_after_group_creation(self):
        useradd = pathlib.Path("/usr/sbin/useradd")
        if not useradd.is_file():
            self.skipTest("Ubuntu useradd is unavailable")
        with tempfile.TemporaryDirectory(prefix="cloudinit-useradd-prefix-") as temporary:
            parent = pathlib.Path(temporary)
            missing_root = create_useradd_root(parent, group_present=False)
            before_passwd = (missing_root / "etc/passwd").read_text(encoding="ascii")
            before_group = (missing_root / "etc/group").read_text(encoding="ascii")
            missing = subprocess.run(
                [str(useradd), "-P", str(missing_root), "-M", "-u", "1234", "--gid", "mester", "vmfixture"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(missing.returncode, 6, missing.stderr.decode("utf-8", "replace"))
            self.assertEqual((missing_root / "etc/passwd").read_text(encoding="ascii"), before_passwd)
            self.assertEqual((missing_root / "etc/group").read_text(encoding="ascii"), before_group)

            present_root = create_useradd_root(parent, group_present=True)
            present = subprocess.run(
                [str(useradd), "-P", str(present_root), "-M", "-u", "1234", "--gid", "mester", "vmfixture"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(present.returncode, 0, present.stderr.decode("utf-8", "replace"))
            entry = next(
                line for line in (present_root / "etc/passwd").read_text(encoding="ascii").splitlines()
                if line.startswith("vmfixture:")
            )
            self.assertEqual(entry.split(":")[3], "1234")
            self.assertEqual(entry.split(":")[2], "1234")


if __name__ == "__main__":
    unittest.main()
