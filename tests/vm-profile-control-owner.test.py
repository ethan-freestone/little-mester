#!/usr/bin/env python3
"""Read-only owner enrollment and authorization gate regressions."""

from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import json
import os
import pathlib
import pwd
import signal
import stat
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "lib/vm/profile-control-owner.py"
SPEC = importlib.util.spec_from_file_location("vm_profile_control_owner", MODULE_PATH)
owner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = owner
SPEC.loader.exec_module(owner)


CALLER_UID = 24680
ACCOUNT_NAME = "fixture-owner"
PRIMARY_GID = 24681


class OwnerFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vm-owner-gate-")
        self.root = pathlib.Path(self.temp.name)
        self.enrollment_owner_uid = os.geteuid()
        self.directory_gid = 46241 if self.enrollment_owner_uid == 0 else os.getegid()
        self.control_gid = 46242 if self.directory_gid != 46242 else 46243
        self.file_gid = 0 if self.enrollment_owner_uid == 0 else os.getegid()
        self.directory = self.root / "etc" / "little-mester-vm"
        self.directory.mkdir(mode=0o755, parents=True)
        os.chmod(self.root / "etc", 0o755)
        os.chown(self.directory, self.enrollment_owner_uid, self.directory_gid)
        os.chmod(self.directory, owner.ENROLLMENT_DIRECTORY_MODE)
        self.path = self.directory / "control-owner.json"
        self.account_gid = PRIMARY_GID
        self._supplementary = True
        self._account_by_name_error = None
        self._account_by_uid_error = None
        self._group_by_name_error = None
        self._directory_group_by_name_error = None
        self._groups_error = None
        self._group_gid_override = None
        self._group_lookup_gid_override = None
        self._directory_group_gid_override = None
        self._directory_group_lookup_gid_override = None
        self._omit_directory_group = False
        self._account_uid_by_name = CALLER_UID
        self._account_name_by_uid = ACCOUNT_NAME
        self._group_member_names = [ACCOUNT_NAME]
        self._directory_group_member_names = []
        self._write_enrollment(CALLER_UID)

    def tearDown(self):
        for item in sorted(self.root.rglob("*"), key=lambda path: len(path.parts), reverse=True):
            try:
                if item.is_dir() and not item.is_symlink():
                    os.chmod(item, 0o755)
            except FileNotFoundError:
                pass
        self.temp.cleanup()

    def _write_enrollment(self, uid: int, *, raw: bytes | None = None):
        if self.path.exists() or self.path.is_symlink():
            self.path.unlink()
        if raw is None:
            raw = json.dumps({"schema_version": 1, "owner_uid": uid}).encode("utf-8")
        self.path.write_bytes(raw)
        os.chown(self.path, self.enrollment_owner_uid, self.file_gid)
        os.chmod(self.path, owner.ENROLLMENT_FILE_MODE)

    def _account(self, name: str, uid: int, gid: int):
        return SimpleNamespace(pw_name=name, pw_uid=uid, pw_gid=gid)

    def _group(self):
        gid = self.control_gid if self._group_gid_override is None else self._group_gid_override
        return SimpleNamespace(gr_name=owner.CONTROL_GROUP, gr_gid=gid, gr_mem=list(self._group_member_names))

    def _directory_group(self):
        gid = (self.directory_gid if self._directory_group_gid_override is None
               else self._directory_group_gid_override)
        return SimpleNamespace(gr_name=owner.ENROLLMENT_DIRECTORY_GROUP, gr_gid=gid,
                               gr_mem=list(self._directory_group_member_names))

    def _account_by_name(self, name: str):
        if self._account_by_name_error is not None:
            raise self._account_by_name_error
        return self._account(name, self._account_uid_by_name, self.account_gid)

    def _group_by_name(self, name: str):
        if name == owner.CONTROL_GROUP:
            if self._group_by_name_error is not None:
                raise self._group_by_name_error
            group = self._group()
            if self._group_lookup_gid_override is not None:
                group.gr_gid = self._group_lookup_gid_override
            return group
        if name == owner.ENROLLMENT_DIRECTORY_GROUP:
            if self._directory_group_by_name_error is not None:
                raise self._directory_group_by_name_error
            group = self._directory_group()
            if self._directory_group_lookup_gid_override is not None:
                group.gr_gid = self._directory_group_lookup_gid_override
            return group
        raise KeyError(name)

    def _account_by_uid(self, uid: int):
        if self._account_by_uid_error is not None:
            raise self._account_by_uid_error
        return self._account(self._account_name_by_uid, uid, self.account_gid)

    def _all_groups(self):
        if self._groups_error is not None:
            raise self._groups_error
        groups = [self._group()]
        if not self._supplementary:
            groups[0].gr_mem = []
        if not self._omit_directory_group:
            groups.append(self._directory_group())
        return groups

    def authorize(self, *, invocation=None, all_groups=None):
        return owner._test_authorize_owner(
            control_path=str(self.path),
            enrollment_owner_uid=self.enrollment_owner_uid,
            enrollment_file_gid=self.file_gid,
            directory_mode=owner.ENROLLMENT_DIRECTORY_MODE,
            invocation=(0, 0, str(CALLER_UID), ACCOUNT_NAME)
            if invocation is None else invocation,
            account_by_name=self._account_by_name,
            account_by_uid=self._account_by_uid,
            group_by_name=self._group_by_name,
            all_groups=self._all_groups if all_groups is None else all_groups,
        )

    def _snapshot(self, path=None):
        path = self.path if path is None else pathlib.Path(path)
        info = os.stat(path, follow_symlinks=False)
        return (
            info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            path.read_bytes() if stat.S_ISREG(info.st_mode) else None,
        )

    def assert_refused_without_file_repair(self, action):
        before = self._snapshot()
        with self.assertRaises(owner.OwnerRefused):
            action()
        self.assertEqual(self._snapshot(), before)

    def test_valid_identity_uses_resolved_primary_gid_and_profile_control_gid(self):
        identity = self.authorize()
        self.assertEqual(identity, owner.OwnerIdentity(
            uid=CALLER_UID,
            primary_gid=PRIMARY_GID,
            account_name=ACCOUNT_NAME,
            control_group_gid=self.control_gid,
        ))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            identity.uid = 123

    def test_shared_config_directory_uses_lm_vm_control_not_owner_group(self):
        directory_info = os.stat(self.directory, follow_symlinks=False)
        self.assertEqual(directory_info.st_gid, self.directory_gid)
        self.assertNotEqual(self.directory_gid, self.control_gid)
        self.assertEqual(self.authorize().control_group_gid, self.control_gid)

    def test_owner_need_not_belong_to_shared_config_directory_group(self):
        self._directory_group_member_names = []
        self.assertEqual(self.authorize().uid, CALLER_UID)

    def test_shared_group_membership_does_not_replace_profile_control_membership(self):
        self._supplementary = False
        self._directory_group_member_names = [ACCOUNT_NAME]
        with self.assertRaises(owner.OwnerRefused):
            self.authorize()

    def test_primary_group_membership_is_accepted_without_os_getgroups(self):
        self.account_gid = self.control_gid
        self._supplementary = False
        with mock.patch.object(owner.os, "getgroups", side_effect=AssertionError("must not inspect elevated groups")):
            self.assertEqual(self.authorize().primary_gid, self.control_gid)

    def test_supplementary_account_database_membership_is_accepted(self):
        self.account_gid = PRIMARY_GID
        self._supplementary = True
        self.assertEqual(self.authorize().uid, CALLER_UID)

    def test_missing_supplementary_group_membership_is_refused(self):
        self._supplementary = False
        self._group_member_names = []
        with self.assertRaises(owner.OwnerRefused):
            self.authorize()

    def test_nonroot_missing_malformed_and_conflicting_sudo_context_refuse(self):
        cases = (
            (CALLER_UID, 1000, str(CALLER_UID), ACCOUNT_NAME),
            (CALLER_UID, 0, str(CALLER_UID), ACCOUNT_NAME),
            (1000, 0, str(CALLER_UID), ACCOUNT_NAME),
            (0, 0, None, ACCOUNT_NAME),
            (0, 0, "", ACCOUNT_NAME),
            (0, 0, "0", "root"),
            (0, 0, "024680", ACCOUNT_NAME),
            (0, 0, "+24680", ACCOUNT_NAME),
            (0, 0, "24680x", ACCOUNT_NAME),
            (0, 0, "24681", ACCOUNT_NAME),
            (0, 0, str(CALLER_UID), None),
            (0, 0, str(CALLER_UID), ""),
            (0, 0, str(CALLER_UID), " fixture-owner"),
            (0, 0, str(CALLER_UID), "fixture/owner"),
            (0, 0, str(CALLER_UID), "Fixture-Owner"),
            (0, 0, str(CALLER_UID), "a" * 33),
        )
        for invocation in cases:
            with self.subTest(invocation=invocation), self.assertRaises(owner.OwnerRefused):
                self.authorize(invocation=invocation)

    def test_uid_name_and_account_database_disagreement_refuse(self):
        self._account_uid_by_name = CALLER_UID + 1
        with self.assertRaises(owner.OwnerRefused):
            self.authorize()

        self._account_uid_by_name = CALLER_UID
        self._account_name_by_uid = "another-account"
        with self.assertRaises(owner.OwnerRefused):
            self.authorize()

        self._account_name_by_uid = ACCOUNT_NAME
        with self.assertRaises(owner.OwnerRefused):
            self.authorize(invocation=(0, 0, str(CALLER_UID + 1), ACCOUNT_NAME))

    def test_account_lookup_errors_fail_closed(self):
        self._account_by_name_error = OSError("NSS unavailable")
        with self.assertRaises(owner.OwnerRefused):
            self.authorize()

        self._account_by_name_error = None
        self._account_by_uid_error = KeyError("unknown UID")
        with self.assertRaises(owner.OwnerRefused):
            self.authorize()

    def test_enrolled_uid_must_match_resolved_invoker(self):
        self._write_enrollment(CALLER_UID + 1)
        self.assert_refused_without_file_repair(self.authorize)

    def test_missing_enrollment_refuses_without_creating_it(self):
        self.path.unlink()
        with self.assertRaises(owner.OwnerRefused):
            self.authorize()
        self.assertFalse(self.path.exists())

    def test_oversized_enrollment_is_refused_without_repair(self):
        self._write_enrollment(CALLER_UID, raw=b"{" + b" " * owner.MAX_ENROLLMENT_BYTES)
        self.assert_refused_without_file_repair(self.authorize)

    def test_duplicate_extra_and_non_object_json_are_refused(self):
        invalid = (
            b'{"schema_version":1,"owner_uid":24680,"owner_uid":24680}',
            b'{"schema_version":1,"owner_uid":24680,"extra":true}',
            b'[1,2]',
        )
        for raw in invalid:
            with self.subTest(raw=raw):
                self._write_enrollment(CALLER_UID, raw=raw)
                self.assert_refused_without_file_repair(self.authorize)

    def test_schema_version_must_be_exact_integer_one(self):
        invalid = (
            b'{"owner_uid":24680}',
            b'{"schema_version":true,"owner_uid":24680}',
            b'{"schema_version":2,"owner_uid":24680}',
            b'{"schema_version":1.0,"owner_uid":24680}',
        )
        for raw in invalid:
            with self.subTest(raw=raw):
                self._write_enrollment(CALLER_UID, raw=raw)
                with self.assertRaises(owner.OwnerRefused):
                    self.authorize()

    def test_owner_uid_must_be_exact_positive_integer(self):
        invalid = (
            b'{"schema_version":1,"owner_uid":true}',
            b'{"schema_version":1,"owner_uid":0}',
            b'{"schema_version":1,"owner_uid":-1}',
            b'{"schema_version":1,"owner_uid":24680.0}',
            b'{"schema_version":1,"owner_uid":"24680"}',
        )
        for raw in invalid:
            with self.subTest(raw=raw):
                self._write_enrollment(CALLER_UID, raw=raw)
                with self.assertRaises(owner.OwnerRefused):
                    self.authorize()

    def test_malformed_and_non_utf8_json_refuse(self):
        for raw in (b'{', b'{"schema_version":1,"owner_uid":24680} trailing', b"\xff"):
            with self.subTest(raw=raw):
                self._write_enrollment(CALLER_UID, raw=raw)
                with self.assertRaises(owner.OwnerRefused):
                    self.authorize()

    def test_group_lookup_and_nss_failures_refuse(self):
        self._group_by_name_error = KeyError(owner.CONTROL_GROUP)
        with self.assertRaises(owner.OwnerRefused):
            self.authorize()

        self._group_by_name_error = None
        self._groups_error = OSError("NSS group enumeration failed")
        with self.assertRaises(owner.OwnerRefused):
            self.authorize()

    def test_shared_directory_group_lookup_and_nss_failures_refuse(self):
        self._directory_group_by_name_error = KeyError(owner.ENROLLMENT_DIRECTORY_GROUP)
        with self.assertRaises(owner.OwnerRefused):
            self.authorize()

        self._directory_group_by_name_error = None
        self._omit_directory_group = True
        with self.assertRaises(owner.OwnerRefused):
            self.authorize()

        self._omit_directory_group = False
        self._directory_group_lookup_gid_override = self.directory_gid + 1
        with self.assertRaises(owner.OwnerRefused):
            self.authorize()

        self._directory_group_lookup_gid_override = None
        real_all_groups = self._all_groups
        calls = 0

        def fail_shared_group_enumeration():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("shared group enumeration failed")
            return real_all_groups()

        with self.assertRaises(owner.OwnerRefused):
            self.authorize(all_groups=fail_shared_group_enumeration)
        self.assertEqual(calls, 2)

    def test_authorization_and_config_directory_groups_cannot_alias_one_gid(self):
        self._directory_group_gid_override = self.control_gid
        with self.assertRaises(owner.OwnerRefused):
            self.authorize()

    def test_group_database_absence_or_inconsistency_refuses(self):
        self._groups_error = None
        with mock.patch.object(self, "_all_groups", return_value=[]):
            with self.assertRaises(owner.OwnerRefused):
                self.authorize()

        self._group_lookup_gid_override = self.control_gid + 1
        with self.assertRaises(owner.OwnerRefused):
            self.authorize()

    def test_directory_owner_group_and_exact_mode_are_protected(self):
        self._directory_group_gid_override = self.directory_gid + 1000
        with self.assertRaises(owner.OwnerRefused):
            self.authorize()
        self._directory_group_gid_override = None

        os.chmod(self.directory, 0o770)
        with self.assertRaises(owner.OwnerRefused):
            self.authorize()

    def test_unsafe_writable_ancestry_refuses_without_mutation(self):
        etc = self.root / "etc"
        original = stat.S_IMODE(os.stat(etc).st_mode)
        os.chmod(etc, 0o777)
        try:
            before = self._snapshot()
            with self.assertRaises(owner.OwnerRefused):
                self.authorize()
            self.assertEqual(self._snapshot(), before)
        finally:
            os.chmod(etc, original)

    def test_symlinked_ancestry_is_refused_and_preserved(self):
        actual = self.root / "real-etc"
        actual.mkdir()
        (actual / "little-mester-vm").mkdir(mode=owner.ENROLLMENT_DIRECTORY_MODE)
        link = self.root / "etc-link"
        link.symlink_to(actual, target_is_directory=True)
        linked_path = link / "little-mester-vm" / "control-owner.json"
        before = self._snapshot(self.path)
        with self.assertRaises(owner.OwnerRefused):
            owner._test_authorize_owner(
                control_path=str(linked_path),
                enrollment_owner_uid=self.enrollment_owner_uid,
                enrollment_file_gid=self.file_gid,
                directory_mode=owner.ENROLLMENT_DIRECTORY_MODE,
                invocation=(0, 0, str(CALLER_UID), ACCOUNT_NAME),
                account_by_name=self._account_by_name,
                account_by_uid=self._account_by_uid,
                group_by_name=self._group_by_name,
                all_groups=self._all_groups,
            )
        self.assertTrue(link.is_symlink())
        self.assertEqual(self._snapshot(self.path), before)

    def test_symlink_enrollment_file_is_refused_without_touching_target(self):
        target = self.root / "sentinel"
        target.write_bytes(b"preserve this target")
        self.path.unlink()
        self.path.symlink_to(target)
        with self.assertRaises(owner.OwnerRefused):
            self.authorize()
        self.assertEqual(target.read_bytes(), b"preserve this target")
        self.assertTrue(self.path.is_symlink())

    def test_wrong_file_mode_and_hardlink_are_refused_without_repair(self):
        os.chmod(self.path, 0o640)
        before = self._snapshot()
        with self.assertRaises(owner.OwnerRefused):
            self.authorize()
        self.assertEqual(self._snapshot(), before)

        os.chmod(self.path, owner.ENROLLMENT_FILE_MODE)
        alias = self.root / "enrollment-alias"
        os.link(self.path, alias)
        before = self._snapshot()
        with self.assertRaises(owner.OwnerRefused):
            self.authorize()
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(os.stat(alias).st_nlink, 2)

    def test_directory_and_fifo_enrollment_files_are_refused(self):
        self.path.unlink()
        self.path.mkdir(mode=0o700)
        with self.assertRaises(owner.OwnerRefused):
            self.authorize()
        self.path.rmdir()

        os.mkfifo(self.path, mode=owner.ENROLLMENT_FILE_MODE)
        alarm_fired = False

        def timeout(_signum, _frame):
            nonlocal alarm_fired
            alarm_fired = True
            raise TimeoutError("FIFO open blocked")

        prior_handler = signal.signal(signal.SIGALRM, timeout)
        signal.setitimer(signal.ITIMER_REAL, 2.0)
        try:
            with self.assertRaises(owner.OwnerRefused):
                self.authorize()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, prior_handler)
        self.assertFalse(alarm_fired, "FIFO validation must be nonblocking")
        self.assertTrue(stat.S_ISFIFO(os.stat(self.path, follow_symlinks=False).st_mode))

    def test_fifo_replacement_between_stat_and_open_is_nonblocking(self):
        real_open = owner.os.open
        replaced = False
        saved = self.root / "saved-enrollment"
        observed_flags = []

        def replace_before_open(name, flags, mode=0o777, *, dir_fd=None):
            nonlocal replaced
            if name == self.path.name and dir_fd is not None and not replaced:
                replaced = True
                self.path.rename(saved)
                os.mkfifo(self.path, mode=owner.ENROLLMENT_FILE_MODE)
                observed_flags.append(flags)
            return real_open(name, flags, mode, dir_fd=dir_fd)

        prior_handler = signal.signal(signal.SIGALRM, lambda *_args: (_ for _ in ()).throw(
            TimeoutError("opening a replaced FIFO blocked")))
        signal.setitimer(signal.ITIMER_REAL, 2.0)
        try:
            with mock.patch.object(owner.os, "open", side_effect=replace_before_open):
                with self.assertRaises(owner.OwnerRefused):
                    self.authorize()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, prior_handler)
        self.assertTrue(replaced)
        self.assertTrue(observed_flags[0] & os.O_NONBLOCK)
        self.assertTrue(saved.exists())
        self.assertTrue(stat.S_ISFIFO(os.stat(self.path, follow_symlinks=False).st_mode))

    def test_replacement_during_read_is_detected(self):
        real_read = owner.os.read
        replaced = False
        saved = self.root / "original-enrollment"

        def replace_after_read(fd, count):
            nonlocal replaced
            chunk = real_read(fd, count)
            if not replaced:
                replaced = True
                self.path.rename(saved)
                self._write_enrollment(CALLER_UID)
            return chunk

        with mock.patch.object(owner.os, "read", side_effect=replace_after_read):
            with self.assertRaises(owner.OwnerRefused):
                self.authorize()
        self.assertTrue(replaced)
        self.assertTrue(saved.exists())
        self.assertTrue(self.path.exists())

    def test_content_change_during_read_is_detected(self):
        real_read = owner.os.read
        changed = False

        def change_after_read(fd, count):
            nonlocal changed
            chunk = real_read(fd, count)
            if not changed:
                changed = True
                with self.path.open("r+b") as stream:
                    stream.seek(0)
                    stream.write(b'{"schema_version":1,"owner_uid":24680}')
                    stream.truncate()
            return chunk

        with mock.patch.object(owner.os, "read", side_effect=change_after_read):
            with self.assertRaises(owner.OwnerRefused):
                self.authorize()
        self.assertTrue(changed)

    @unittest.skipUnless(os.geteuid() == 0, "root-only: construct a genuinely root-owned fixture")
    def test_genuine_root_owned_fixture_matches_fixed_enrollment_contract(self):
        directory_info = os.stat(self.directory, follow_symlinks=False)
        file_info = os.stat(self.path, follow_symlinks=False)
        self.assertEqual((directory_info.st_uid, directory_info.st_gid,
                          stat.S_IMODE(directory_info.st_mode)),
                         (0, self.directory_gid, owner.ENROLLMENT_DIRECTORY_MODE))
        self.assertEqual((file_info.st_uid, file_info.st_gid,
                          stat.S_IMODE(file_info.st_mode), file_info.st_nlink),
                         (0, 0, owner.ENROLLMENT_FILE_MODE, 1))
        self.assertEqual(self.authorize().uid, CALLER_UID)

    @unittest.skipUnless(os.geteuid() == 0, "root-only: simulate foreign enrollment owner")
    def test_foreign_file_owner_refuses_and_preserves_root_fixture(self):
        os.chown(self.path, 65534, 0)
        before = self._snapshot()
        with self.assertRaises(owner.OwnerRefused):
            self.authorize()
        self.assertEqual(self._snapshot(), before)

    def test_actual_sudo_context_and_passwd_records_authorize_through_fixture_seam(self):
        invocation = (
            os.getuid(),
            os.geteuid(),
            os.environ.get("SUDO_UID"),
            os.environ.get("SUDO_USER"),
        )
        required = os.environ.get("LM_VM_REQUIRE_ACTUAL_SUDO_OWNER_REGRESSION") == "1"
        sudo_uid = invocation[2]
        is_sudo_context = (
            invocation[0] == 0 and invocation[1] == 0
            and isinstance(sudo_uid, str)
            and owner._CANONICAL_UID.fullmatch(sudo_uid) is not None
            and isinstance(invocation[3], str)
        )
        if not is_sudo_context:
            if required:
                self.fail("CI required the actual nonroot-origin sudo owner regression, but SUDO_* context is absent")
            self.skipTest("requires a real/effective-root process with nonroot-origin SUDO_* context")

        account_name = invocation[3]
        self._group_member_names = [account_name]
        self._write_enrollment(int(sudo_uid))
        identity = owner._test_authorize_owner(
            control_path=str(self.path),
            enrollment_owner_uid=self.enrollment_owner_uid,
            enrollment_file_gid=self.file_gid,
            directory_mode=owner.ENROLLMENT_DIRECTORY_MODE,
            invocation=invocation,
            account_by_name=pwd.getpwnam,
            account_by_uid=pwd.getpwuid,
            group_by_name=self._group_by_name,
            all_groups=self._all_groups,
        )
        self.assertEqual(identity.uid, int(sudo_uid))
        self.assertEqual(identity.account_name, account_name)
        self.assertEqual(identity.control_group_gid, self.control_gid)

    def test_public_api_has_no_path_identity_or_request_parameters(self):
        self.assertEqual(str(inspect.signature(owner.authorize_owner)), "() -> 'OwnerIdentity'")
        captured = {}

        def capture(**kwargs):
            captured.update(kwargs)
            return owner.OwnerIdentity(1, 2, "fixed", 3)

        with mock.patch.object(owner, "_authorize_owner", side_effect=capture), \
                mock.patch.object(owner.os, "getuid", return_value=0), \
                mock.patch.object(owner.os, "geteuid", return_value=0), \
                mock.patch.dict(os.environ, {
                    "SUDO_UID": str(CALLER_UID),
                    "SUDO_USER": ACCOUNT_NAME,
                    "LM_VM_CONTROL_OWNER_FILE": "/tmp/attacker.json",
                    "LM_VM_PROFILE_OWNER_UID": "1",
                    "LM_VM_PROFILE_REQUEST_UID": "1",
                }):
            self.assertEqual(owner.authorize_owner().account_name, "fixed")
        self.assertEqual(captured["control_path"], owner.ENROLLMENT_FILE)
        self.assertEqual(captured["enrollment_owner_uid"], 0)
        self.assertEqual(captured["enrollment_file_gid"], 0)
        self.assertEqual(captured["real_uid"], 0)
        self.assertEqual(captured["effective_uid"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
