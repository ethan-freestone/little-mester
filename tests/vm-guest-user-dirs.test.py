"""Filesystem regressions for the exact guest rootless-directory helper."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import pathlib
import pwd
import re
import stat
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
        for path in (getattr(self, "protected_opencode", None),
                     getattr(self, "protected_xdg", None)):
            if path is not None:
                path.chmod(0o700)
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
        self.assertIn("timeout --signal=TERM --kill-after=1s 2s runuser", generated)
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
        for components in (*guest_dirs.USER_DIRS, *guest_dirs.OPENCODE_RUNTIME_DIRS[3:]):
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

    def test_opencode_runtime_preparation_is_credential_independent(self):
        guest_dirs.prepare_opencode_dirs(
            self.home_fd, self.account.pw_uid, self.account.pw_gid
        )
        for components in guest_dirs.OPENCODE_RUNTIME_DIRS:
            path = self.home.joinpath(*components)
            info = path.lstat()
            self.assertTrue(stat.S_ISDIR(info.st_mode))
            self.assertEqual((info.st_uid, info.st_gid), (self.account.pw_uid, self.account.pw_gid))
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)
        self.assertFalse((self.home / ".local/share/opencode/auth.json").exists())

    @unittest.skipUnless(pathlib.Path("/usr/local/bin/opencode").is_file(),
                         "pinned OpenCode executable is not installed")
    def test_pinned_opencode_version_needs_prepared_default_state_chain(self):
        executable = pathlib.Path("/usr/local/bin/opencode")
        version = subprocess.run(
            [str(executable), "--version"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, check=False, timeout=8,
        )
        if version.returncode != 0 or version.stdout.strip() != "opencode v2.2.5":
            self.skipTest("installed OpenCode is not the pinned 2.2.5 runtime")

        (self.home / ".local/share/opencode").mkdir(parents=True, mode=0o700)
        (self.home / ".local").chmod(0o700)
        (self.home / ".local/share").chmod(0o700)
        (self.home / ".local/share/opencode").chmod(0o700)
        (self.home / ".cache").mkdir(mode=0o700)
        protected_xdg = self.root / "protected-xdg"
        protected_opencode = protected_xdg / "opencode"
        protected_opencode.mkdir(parents=True, mode=0o700)
        protected_config = protected_opencode / "opencode.json"
        protected_config.write_text(
            json.dumps({"autoupdate": False, "share": "disabled"}),
            encoding="utf-8",
        )
        protected_config.chmod(0o444)
        protected_opencode.chmod(0o555)
        protected_xdg.chmod(0o555)
        self.protected_xdg = protected_xdg
        self.protected_opencode = protected_opencode
        work = self.root / "workspace"
        work.mkdir(mode=0o700)
        if os.geteuid() == 0:
            for path in (work, self.home / ".local/share", self.home / ".local/share/opencode",
                         self.home / ".cache"):
                os.chown(path, self.account.pw_uid, self.account.pw_gid)
            (self.home / ".local").chmod(0o755)
        else:
            (self.home / ".local").chmod(0o500)
        env = {
            "HOME": str(self.home),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "XDG_DATA_HOME": str(self.home / ".local/share"),
            # Mirror protected `/etc/opencode` without depending on the runner's
            # host filesystem or granting the unprivileged process write access.
            "XDG_CONFIG_HOME": str(protected_xdg),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "OPENCODE_CONFIG": str(protected_config),
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        }

        def drop_privileges():
            if os.geteuid() == 0:
                os.setgroups([])
                os.setgid(self.account.pw_gid)
                os.setuid(self.account.pw_uid)

        def run_version():
            return subprocess.run(
                [str(executable), "--version"], cwd=work, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                check=False, timeout=8,
                preexec_fn=drop_privileges if os.geteuid() == 0 else None,
            )

        denied = run_version()
        self.assertEqual(denied.returncode, 1)
        self.assertIn("EACCES", denied.stderr)
        self.assertIn(".local/state", denied.stderr)

        guest_dirs.prepare_opencode_dirs(
            self.home_fd, self.account.pw_uid, self.account.pw_gid
        )
        self.assertTrue((self.home / ".local/state/opencode").is_dir())
        corrected = run_version()
        self.assertEqual(corrected.returncode, 0, corrected.stderr[:300])
        self.assertEqual(corrected.stdout.strip(), "opencode v2.2.5")

    def test_proxy_configs_are_created_privately_without_following_existing_symlink(self):
        guest_dirs.prepare_rootless_config(self.home_fd, self.account.pw_uid, self.account.pw_gid)
        unit_file = self.home / ".config/systemd/user/docker.service.d/80-mester-proxy.conf"
        daemon_file = self.home / ".config/docker/daemon.json"
        for path, contents in (
            (unit_file, guest_dirs.PROXY_UNIT_CONFIG),
            (daemon_file, guest_dirs.DOCKER_DAEMON_CONFIG),
        ):
            info = path.stat()
            self.assertTrue(path.is_file())
            self.assertEqual((info.st_uid, info.st_gid), (self.account.pw_uid, self.account.pw_gid))
            self.assertEqual(info.st_mode & 0o777, 0o600)
            self.assertEqual(path.read_bytes(), contents)
        self.assertIn(b"Environment=DOCKERD_ROOTLESS_ROOTLESSKIT_FLAGS=--cidr=10.0.3.0/24", unit_file.read_bytes())
        self.assertEqual(json.loads(daemon_file.read_text(encoding="utf-8"))["proxies"]["http-proxy"], "http://10.0.2.100:3128")

        separate = self.root / "separate-home"
        separate.mkdir(mode=0o700)
        os.chown(separate, self.account.pw_uid, self.account.pw_gid)
        separate_fd = os.open(separate, guest_dirs.DIR_FLAGS)
        try:
            guest_dirs.prepare_config_dirs(separate_fd, self.account.pw_uid, self.account.pw_gid)
            external = self.root / "external-config"
            external.write_text("preserve", encoding="utf-8")
            target = separate / ".config/systemd/user/docker.service.d/80-mester-proxy.conf"
            target.symlink_to(external)
            with self.assertRaisesRegex(guest_dirs.Refusal, "refusing to replace"):
                guest_dirs.prepare_rootless_config(separate_fd, self.account.pw_uid, self.account.pw_gid)
            self.assertTrue(target.is_symlink())
            self.assertEqual(external.read_text(encoding="utf-8"), "preserve")
        finally:
            os.close(separate_fd)

    def _run_as_guest(self, operation):
        if os.geteuid() == 0:
            os.setgroups([])
            os.setgid(self.account.pw_gid)
            os.setuid(self.account.pw_uid)
        operation()

    def test_auth_store_preparation_needs_no_credentials(self):
        child = os.fork()
        if child == 0:
            try:
                self._run_as_guest(lambda: guest_dirs.prepare_auth_store_at(
                    self.home_fd, self.account.pw_uid, self.account.pw_gid))
            except BaseException as error:
                print(f"empty credential-store preparation failed: {error}", file=sys.stderr)
                os._exit(1)
            os._exit(0)
        _, status = os.waitpid(child, 0)
        self.assertTrue(os.WIFEXITED(status))
        self.assertEqual(os.WEXITSTATUS(status), 0)
        store = self.home / ".local/share/opencode"
        self.assertTrue(store.is_dir())
        self.assertEqual({path.name for path in store.iterdir()}, {"tmux"},
                         "prepare-auth must not require or create credentials")
        self.assertEqual(store.stat().st_mode & 0o777, 0o700)
        tmux_dir = store / "tmux"
        self.assertTrue(tmux_dir.is_dir())
        self.assertEqual(tmux_dir.stat().st_mode & 0o777, 0o700)

    def test_auth_store_repair_preserves_existing_credentials_and_tightens_modes(self):
        store = self.home / ".local/share/opencode"
        store.mkdir(parents=True, mode=0o775)
        for path in (self.home / ".local", self.home / ".local/share", store):
            path.chmod(0o775)
            if os.geteuid() == 0:
                os.chown(path, self.account.pw_uid, self.account.pw_gid)
        credential = store / "auth.json"
        payload = b'{"openai":{"refresh":"preserve-this-byte-for-byte"}}\n'
        credential.write_bytes(payload)
        credential.chmod(0o664)
        database = store / "opencode.db"
        database_payload = b"sqlite-database-fixture-preserved-byte-for-byte"
        database.write_bytes(database_payload)
        database.chmod(0o664)
        if os.geteuid() == 0:
            os.chown(credential, self.account.pw_uid, self.account.pw_gid)
            os.chown(database, self.account.pw_uid, self.account.pw_gid)

        child = os.fork()
        if child == 0:
            try:
                self._run_as_guest(lambda: guest_dirs.prepare_auth_store_at(
                    self.home_fd, self.account.pw_uid, self.account.pw_gid))
            except BaseException as error:
                print(f"credential privacy repair failed: {error}", file=sys.stderr)
                os._exit(1)
            os._exit(0)
        _, status = os.waitpid(child, 0)
        self.assertTrue(os.WIFEXITED(status))
        self.assertEqual(os.WEXITSTATUS(status), 0)
        self.assertEqual(credential.read_bytes(), payload)
        self.assertEqual(credential.stat().st_mode & 0o777, 0o600)
        self.assertEqual(database.read_bytes(), database_payload)
        self.assertEqual(database.stat().st_mode & 0o777, 0o600)
        for path in (self.home / ".local", self.home / ".local/share", store):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_auth_store_repair_refuses_symlink_and_hardlinked_credentials(self):
        store = self.home / ".local/share/opencode"
        store.mkdir(parents=True, mode=0o700)
        if os.geteuid() == 0:
            for path in (self.home / ".local", self.home / ".local/share", store):
                os.chown(path, self.account.pw_uid, self.account.pw_gid)

        def attempt():
            guest_dirs.prepare_auth_store_at(self.home_fd, self.account.pw_uid, self.account.pw_gid)

        def refuse_as_guest():
            child = os.fork()
            if child == 0:
                try:
                    self._run_as_guest(lambda: self.assertRaises(guest_dirs.Refusal, attempt))
                except BaseException:
                    os._exit(1)
                os._exit(0)
            _, status = os.waitpid(child, 0)
            self.assertTrue(os.WIFEXITED(status))
            self.assertEqual(os.WEXITSTATUS(status), 0)

        guarded_names = (
            "opencode.db", "opencode.db-wal", "opencode.db-shm",
            "opencode.db-journal", "auth.json",
        )
        for index, name in enumerate(guarded_names):
            target = self.root / f"outside-{index}"
            payload = f"preserve-{name}".encode()
            target.write_bytes(payload)
            target.chmod(0o644)
            if os.geteuid() == 0:
                os.chown(target, self.account.pw_uid, self.account.pw_gid)
            (store / name).symlink_to(target)
            refuse_as_guest()
            self.assertTrue((store / name).is_symlink())
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)
            (store / name).unlink()

            target.write_bytes(payload)
            target.chmod(0o644)
            alias = store / name
            os.link(target, alias)
            refuse_as_guest()
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)
            self.assertEqual(target.stat().st_nlink, 2)
            alias.unlink()

            if os.geteuid() == 0 and self.account.pw_uid != 0:
                owned_by_root = store / name
                owned_by_root.write_bytes(payload)
                owned_by_root.chmod(0o644)
                os.chown(owned_by_root, 0, 0)
                refuse_as_guest()
                info = owned_by_root.stat()
                self.assertEqual((info.st_uid, info.st_mode & 0o777), (0, 0o644))
                self.assertEqual(owned_by_root.read_bytes(), payload)
                owned_by_root.unlink()

            fifo = store / name
            os.mkfifo(fifo, 0o640)
            if os.geteuid() == 0:
                os.chown(fifo, self.account.pw_uid, self.account.pw_gid)
            refuse_as_guest()
            info = fifo.lstat()
            self.assertTrue(stat.S_ISFIFO(info.st_mode))
            self.assertEqual(info.st_mode & 0o777, 0o640)
            fifo.unlink()

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

        state_dir = self.home / ".local/state"
        state_dir.mkdir(parents=True, mode=0o755)
        state_sentinel = state_dir / "root-owned-sentinel"
        state_sentinel.write_text("leave state contents alone", encoding="utf-8")
        os.chown(state_sentinel, 0, 0)
        for path in (self.home / ".local", state_dir):
            os.chown(path, 0, 0)
            os.chmod(path, 0o755)

        guest_dirs.prepare_config_dirs(self.home_fd, self.account.pw_uid, self.account.pw_gid)

        for path in (self.home / ".config", self.home / ".config/systemd", user_dir):
            info = path.stat()
            self.assertEqual((info.st_uid, info.st_gid), (self.account.pw_uid, self.account.pw_gid))
            self.assertEqual(info.st_mode & 0o777, 0o700)
        for path in (self.home / ".local", state_dir, state_dir / "opencode"):
            info = path.stat()
            self.assertEqual((info.st_uid, info.st_gid), (self.account.pw_uid, self.account.pw_gid))
            self.assertEqual(info.st_mode & 0o777, 0o700)
        sentinel_info = sentinel.stat()
        self.assertEqual((sentinel_info.st_uid, sentinel_info.st_gid), (0, 0))
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "leave file ownership alone")
        state_info = state_sentinel.stat()
        self.assertEqual((state_info.st_uid, state_info.st_mode & 0o777), (0, 0o644))
        self.assertEqual(state_sentinel.read_text(encoding="utf-8"), "leave state contents alone")

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
        if os.geteuid() == 0:
            os.chown(config, self.account.pw_uid, self.account.pw_gid)
        config.chmod(0o777)
        original = config.stat()

        with self.assertRaisesRegex(guest_dirs.Refusal, "group/world-writable"):
            guest_dirs.prepare_config_dirs(self.home_fd, self.account.pw_uid, self.account.pw_gid)

        self.assertEqual(config.stat().st_mode & 0o777, 0o777)
        self.assertEqual((config.stat().st_uid, config.stat().st_gid), (original.st_uid, original.st_gid))

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
