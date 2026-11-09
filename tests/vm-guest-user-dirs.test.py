"""Filesystem regressions for the exact guest rootless-directory helper."""

from __future__ import annotations

import base64
import importlib.util
import os
import pathlib
import pwd
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parent.parent
HELPER_PATH = ROOT / "lib/vm/guest-user-dirs.py"
SPEC = importlib.util.spec_from_file_location("guest_user_dirs", HELPER_PATH)
guest_dirs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guest_dirs)


class GuestUserDirectoryTests(unittest.TestCase):
    def setUp(self):
        if os.geteuid() == 0:
            self.account = pwd.getpwnam("nobody")
        else:
            self.account = pwd.getpwuid(os.getuid())
        self.temporary = tempfile.TemporaryDirectory(prefix="guest-user-dirs-")
        self.root = pathlib.Path(self.temporary.name)
        os.chmod(self.root, 0o755)
        home_parent = self.root / "home"
        home_parent.mkdir(mode=0o755)
        self.home = home_parent / "mester"
        self.home.mkdir(mode=0o700)
        if os.geteuid() == 0:
            os.chown(self.home, self.account.pw_uid, self.account.pw_gid)
        else:
            self.assertEqual(self.account.pw_uid, os.getuid())
            self.assertEqual(self.account.pw_gid, os.getgid())
        self.home_fd = os.open(self.home, guest_dirs.DIR_FLAGS)

    def tearDown(self):
        os.close(self.home_fd)
        self.temporary.cleanup()

    def test_generated_cloud_init_embeds_exact_helper_and_user_bus_gate(self):
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE7zJmK1W1h3V4eA7qQKX8U4n9x0z8m1c2b3d4e5f6g7 fixture"
        command = (
            f'source "{ROOT / "lib/vm/cloud-init.sh"}"; '
            f'VM_HOST_SCRIPT_ROOT="{ROOT}"; '
            f'vm_host_cloud_init smoke-v3 {self.account.pw_uid} "{key}"'
        )
        generated = subprocess.run(
            ["/usr/bin/bash", "-c", command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            text=True,
        ).stdout
        match = re.search(
            r"(?ms)^  - path: /usr/local/sbin/mester-v3-user-dirs\.py\n"
            r"(?:^    .*\n)*?^    content: ([A-Za-z0-9+/=]+)$",
            generated,
        )
        self.assertIsNotNone(match, "cloud-init must install the guest helper from its protected source")
        self.assertEqual(base64.b64decode(match.group(1)), HELPER_PATH.read_bytes())
        self.assertIn("encoding: b64", generated)
        self.assertIn("permissions: '0644'", generated)
        self.assertIn("mester-v3-user-dirs.py prepare", generated)
        self.assertIn("mester-v3-user-dirs.py verify-write", generated)
        self.assertIn("user systemd bus did not become ready", generated)
        self.assertIn("systemctl --user show-environment", generated)
        self.assertIn("groups:\n  - mester\nusers:", generated)
        self.assertIn("dockerd-rootless-setuptool.sh install", generated)
        self.assertNotIn("systemctl enable --now docker.service", generated)
        try:
            import yaml
        except ImportError:
            yaml = None
        if yaml is not None:
            config = yaml.safe_load(generated)
            self.assertEqual(config["groups"], ["mester"])
            self.assertEqual(config["users"][0]["uid"], self.account.pw_uid)
            files = {entry["path"]: entry for entry in config["write_files"]}
            provision_script = files["/usr/local/sbin/mester-v3-provision"]["content"]
            syntax = subprocess.run(
                ["bash", "-n"], input=provision_script.encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(syntax.returncode, 0, syntax.stderr.decode("utf-8", "replace"))
            embedded = files["/usr/local/sbin/mester-v3-user-dirs.py"]
            self.assertEqual(embedded["encoding"], "b64")
            self.assertEqual(base64.b64decode(embedded["content"]), HELPER_PATH.read_bytes())

    def test_all_user_config_parents_are_private_and_nonroot_can_create_file(self):
        guest_dirs.prepare_config_dirs(self.home_fd, self.account.pw_uid, self.account.pw_gid)
        for components in guest_dirs.USER_DIRS:
            path = self.home.joinpath(*components)
            info = path.lstat()
            self.assertTrue(path.is_dir())
            self.assertEqual((info.st_uid, info.st_gid), (self.account.pw_uid, self.account.pw_gid))
            self.assertEqual(info.st_mode & 0o777, 0o700)

        child = os.fork()
        if child == 0:
            try:
                if os.geteuid() == 0:
                    os.setgroups([])
                    os.setgid(self.account.pw_gid)
                    os.setuid(self.account.pw_uid)
                guest_dirs.verify_user_write_at(
                    self.home_fd, self.account.pw_uid, self.account.pw_gid
                )
            except BaseException as error:
                print(f"non-root write failed: {error}", file=sys.stderr)
                os._exit(1)
            os._exit(0)
        _, status = os.waitpid(child, 0)
        self.assertTrue(os.WIFEXITED(status), f"write-check child status={status}")
        self.assertEqual(os.WEXITSTATUS(status), 0)
        self.assertEqual(list(self.home.glob(".mester-write-check-*")), [])
        self.assertEqual(list((self.home / ".config/systemd/user").glob(".mester-write-check-*")), [])

    def test_existing_root_owned_intermediate_dirs_are_fixed_without_recursive_chown(self):
        if os.geteuid() != 0 or self.account.pw_uid == 0:
            self.skipTest("root-owned directory transition requires root and a distinct unprivileged identity")
        user_dir = self.home / ".config/systemd/user"
        user_dir.mkdir(parents=True, mode=0o755)
        sentinel = user_dir / "root-owned-sentinel"
        sentinel.write_text("leave file ownership alone", encoding="utf-8")
        os.chown(sentinel, 0, 0)
        for path in (self.home / ".config", self.home / ".config/systemd", user_dir):
            os.chown(path, 0, 0)
            os.chmod(path, 0o755)

        guest_dirs.prepare_config_dirs(self.home_fd, self.account.pw_uid, self.account.pw_gid)

        for path in (self.home / ".config", self.home / ".config/systemd", user_dir):
            info = path.stat()
            self.assertEqual((info.st_uid, info.st_gid), (self.account.pw_uid, self.account.pw_gid))
            self.assertEqual(info.st_mode & 0o777, 0o700)
        sentinel_info = sentinel.stat()
        self.assertEqual((sentinel_info.st_uid, sentinel_info.st_gid), (0, 0))
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "leave file ownership alone")

    def test_symlinked_config_parent_is_refused_without_touching_target(self):
        target = self.root / "outside"
        target.mkdir(mode=0o700)
        marker = target / "preserve"
        marker.write_text("untouched", encoding="utf-8")
        (self.home / ".config").symlink_to(target, target_is_directory=True)

        with self.assertRaises(guest_dirs.Refusal):
            guest_dirs.prepare_config_dirs(self.home_fd, self.account.pw_uid, self.account.pw_gid)

        self.assertTrue((self.home / ".config").is_symlink())
        self.assertEqual(marker.read_text(encoding="utf-8"), "untouched")
        self.assertEqual(marker.stat().st_uid, os.getuid())

    def test_symlinked_systemd_user_leaf_is_refused_without_following_it(self):
        systemd = self.home / ".config/systemd"
        systemd.mkdir(parents=True, mode=0o700)
        target = self.root / "outside-user-dir"
        target.mkdir(mode=0o700)
        marker = target / "preserve"
        marker.write_text("untouched", encoding="utf-8")
        (systemd / "user").symlink_to(target, target_is_directory=True)

        with self.assertRaises(guest_dirs.Refusal):
            guest_dirs.prepare_config_dirs(self.home_fd, self.account.pw_uid, self.account.pw_gid)

        self.assertTrue((systemd / "user").is_symlink())
        self.assertEqual(marker.read_text(encoding="utf-8"), "untouched")
        self.assertEqual(marker.stat().st_uid, os.getuid())

    def test_group_world_writable_existing_config_parent_is_refused_unchanged(self):
        config = self.home / ".config"
        config.mkdir(mode=0o777)
        config.chmod(0o777)

        with self.assertRaisesRegex(guest_dirs.Refusal, "group/world-writable"):
            guest_dirs.prepare_config_dirs(self.home_fd, self.account.pw_uid, self.account.pw_gid)

        self.assertEqual(config.stat().st_mode & 0o777, 0o777)
        self.assertEqual((config.stat().st_uid, config.stat().st_gid), (self.account.pw_uid, self.account.pw_gid))

    def test_wrong_home_owner_fails_without_chowning_home(self):
        if os.geteuid() != 0 or self.account.pw_uid == 0:
            self.skipTest("wrong-owner fixture requires root and a distinct unprivileged identity")
        os.chown(self.home, 0, 0)
        with self.assertRaises(guest_dirs.Refusal):
            guest_dirs.prepare_config_dirs(self.home_fd, self.account.pw_uid, self.account.pw_gid)
        info = self.home.stat()
        self.assertEqual((info.st_uid, info.st_gid), (0, 0))
        self.assertFalse((self.home / ".config").exists())


if __name__ == "__main__":
    unittest.main()
