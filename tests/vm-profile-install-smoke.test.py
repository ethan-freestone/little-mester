#!/usr/bin/env python3
"""Exercise the pinned profile-helper payload in an isolated install layout."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import os
import pathlib
import pwd
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_extensionless(name: str, path: pathlib.Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load extensionless entrypoint: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


OWNER = load_extensionless("profile_smoke_owner_acceptance", ROOT / "tools/vm-owner-acceptance")


class InstalledProfilePayloadSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lm-profile-payload-smoke-")
        self.root = pathlib.Path(self.temp.name)
        self.candidate_root = self.root / "candidate"
        self.system_root = self.root / "system"
        self.archive_payloads = self._build_candidate_archive()
        self.system_installer = load_extensionless(
            f"profile_smoke_system_installer_{id(self)}",
            self.candidate_root / "tools/vm-system-install")
        self.candidate_helper = load_extensionless(
            f"profile_smoke_runtime_manifest_{id(self)}",
            self.candidate_root / "lib/vm/profile-control.py")
        self._materialize_production_layout()

    def tearDown(self):
        for name in (
            "profile_control_core_runtime", "profile_control_vm_instance",
            "profile_control_parent_provision", "profile_control_provision_runtime",
            "profile_control_qemu_runtime", "parent_executable_policy",
            f"profile_smoke_runtime_manifest_{id(self)}",
        ):
            sys.modules.pop(name, None)
        self.temp.cleanup()

    def _build_candidate_archive(self) -> set[str]:
        sha = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD^{commit}"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ).stdout.strip()
        result = subprocess.run(
            OWNER.archive_arguments(ROOT, sha), check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        observed = set()
        self.candidate_root.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
            for member in archive:
                if member.isdir():
                    continue
                relative = pathlib.PurePosixPath(member.name)
                if (relative.is_absolute() or ".." in relative.parts
                        or member.name not in OWNER.INSTALL_FILES or not member.isfile()):
                    raise AssertionError(f"production artifact has an unexpected member: {member.name}")
                source = archive.extractfile(member)
                if source is None:
                    raise AssertionError(f"production artifact member could not be read: {member.name}")
                destination = self.candidate_root.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read())
                destination.chmod(OWNER.INSTALL_FILES[member.name])
                observed.add(member.name)
        if observed != set(OWNER.INSTALL_FILES):
            raise AssertionError(
                "production candidate archive does not match its exact inventory: "
                f"missing={sorted(set(OWNER.INSTALL_FILES) - observed)} "
                f"unexpected={sorted(observed - set(OWNER.INSTALL_FILES))}")
        return observed

    def _materialize_production_layout(self) -> None:
        installer = self.system_installer
        expected_closure = {
            "lib/vm/profile-control-core.py",
            "lib/vm/vm-instance.py",
            "lib/vm/parent-provision.py",
            "lib/vm/parent-qemu.py",
            "lib/vm/proxy.py",
            "lib/vm/executable-policy.py",
        }
        self.assertTrue(expected_closure.issubset(installer.FILES))
        self.assertTrue(expected_closure.issubset(OWNER.INSTALL_FILES))
        self.assertTrue(set(self.candidate_helper.PROFILE_RUNTIME_FILES).issubset(
            self.archive_payloads))
        self.assertIn("lib/vm/executable-policy.py", installer.UPDATE_ADDITIONALS)
        self.assertIn("lib/vm/executable-policy.py", installer.PERMANENT_INTERLOCK_RELATIVES)

        owner = pwd.getpwuid(os.getuid()).pw_name
        for relative, (destination, mode) in installer.FILES.items():
            payload = installer.source_payload(self.candidate_root, relative, owner)
            path = self.system_root / pathlib.Path(destination).relative_to("/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            path.chmod(mode)

        # The production installer receives this extensionless helper through
        # its exact signed-bundle manifest, separate from the fixed library.
        for relative, (destination, mode) in installer.PROFILE_CONTROL_BUNDLE_FILES.items():
            path = self.system_root / pathlib.Path(destination).relative_to("/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((self.candidate_root / relative).read_bytes())
            path.chmod(mode)

        self.helper_path = (self.system_root /
                            pathlib.Path(installer.PROFILE_CONTROL_HELPER).relative_to("/"))
        self.fixed_lib = self.system_root / "usr/lib/little-mester-vm"
        self.assertTrue(self.helper_path.is_file())
        self.assertFalse(self.helper_path.name.endswith(".py"))
        self.assertTrue((self.candidate_root / "lab-vm").is_file())

    def _load_installed_helper(self):
        helper = load_extensionless(f"profile_smoke_control_{id(self)}", self.helper_path)
        # The production constant is absolute. Redirect only that fixed root to
        # this isolated system layout; the loader itself remains unchanged.
        helper.FIXED_LIB = self.fixed_lib
        return helper

    def test_installed_extensionless_helper_loads_its_complete_fixed_dependency_closure(self):
        helper = self._load_installed_helper()
        loaded_paths = []
        original_spec_from_file_location = importlib.util.spec_from_file_location

        def record_module_path(name, location, *args, **kwargs):
            loaded_paths.append(pathlib.Path(location).resolve())
            return original_spec_from_file_location(name, location, *args, **kwargs)

        with mock.patch.object(importlib.util, "spec_from_file_location",
                               side_effect=record_module_path):
            core = helper._core()
            instance_policy = core._instance_policy()
            self.assertTrue(instance_policy.Instance)
            approved_base_sha = core._approved_base_digest()
            provision = helper._provision()
            self.assertEqual(approved_base_sha, provision.IMAGE_SHA256)
            provision._validate_host_tool("/usr/bin/true")
            qemu = helper._qemu()

        self.assertEqual(pathlib.Path(helper.__file__).resolve(), self.helper_path.resolve())
        self.assertEqual(helper._lib_path("proxy.py"), self.fixed_lib / "proxy.py")
        self.assertTrue(helper._lib_path("proxy.py").is_file())
        expected_dynamic_files = {
            "profile-control-core.py", "vm-instance.py", "parent-provision.py",
            "parent-qemu.py", "executable-policy.py",
        }
        self.assertTrue(callable(qemu.build_system_qemu_argv))
        self.assertEqual({path.name for path in loaded_paths}, expected_dynamic_files)
        self.assertTrue(all(path.is_relative_to(self.fixed_lib.resolve()) for path in loaded_paths))
        self.assertFalse(any(path.is_relative_to(ROOT / "lib/vm") for path in loaded_paths))

    def test_missing_fixed_dependency_fails_without_source_checkout_fallback(self):
        policy_path = self.fixed_lib / "executable-policy.py"
        policy_path.unlink()
        helper = self._load_installed_helper()
        provision = helper._provision()
        with self.assertRaises(FileNotFoundError):
            provision._validate_host_tool("/usr/bin/true")
        self.assertFalse(policy_path.exists())
        self.assertTrue((ROOT / "lib/vm/executable-policy.py").is_file())


if __name__ == "__main__":
    unittest.main()
