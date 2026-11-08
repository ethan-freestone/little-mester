import importlib.util
import os
import pathlib
import stat
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parent.parent
POLICY_PATH = ROOT / "lib/vm/executable-policy.py"
SPEC = importlib.util.spec_from_file_location("executable_policy", POLICY_PATH)
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)


def resolved_only_accepts(path):
    """Reproduce the former target-only check to pin the reviewed counterexample."""
    resolved = pathlib.Path(path).resolve(strict=True)
    info = resolved.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
        return False
    for item in (resolved, *resolved.parents):
        info = item.stat(follow_symlinks=False)
        if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return False
    return True


class ExecutablePolicyTests(unittest.TestCase):
    def test_protected_python_system_symlink_is_accepted(self):
        POLICY.validate_executable("/usr/bin/python3")

    def test_protected_temporary_executable_and_lexical_symlink_are_accepted(self):
        uid = os.getuid()
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()) as temporary:
            root = pathlib.Path(temporary)
            bindir = root / "bin"
            bindir.mkdir(mode=0o755)
            executable = bindir / "tool"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            alias = bindir / "tool-alias"
            alias.symlink_to(executable.name)

            POLICY.validate_executable(
                alias,
                directory_owners={0, uid},
                executable_owners={0, uid},
                symlink_owners={0, uid},
            )

    def test_resolved_only_counterexample_rejects_unsafe_lexical_ancestry(self):
        uid = os.getuid()
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            unsafe = pathlib.Path(temporary) / "command"
            unsafe.symlink_to("/usr/bin/true")

            self.assertTrue(resolved_only_accepts(unsafe))
            with self.assertRaisesRegex(POLICY.UntrustedExecutablePath, "ancestor"):
                POLICY.validate_executable(
                    unsafe,
                    directory_owners={0, uid},
                    executable_owners={0},
                    symlink_owners={0, uid},
                )

    def test_safe_lexical_symlink_cannot_escape_to_unprotected_target_ancestry(self):
        uid = os.getuid()
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()) as protected_root:
            with tempfile.TemporaryDirectory(dir="/tmp") as unprotected_root:
                target = pathlib.Path(unprotected_root) / "tool"
                target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                target.chmod(0o755)
                alias = pathlib.Path(protected_root) / "tool"
                alias.symlink_to(target)

                with self.assertRaisesRegex(POLICY.UntrustedExecutablePath, "ancestor"):
                    POLICY.validate_executable(
                        alias,
                        directory_owners={0, uid},
                        executable_owners={0, uid},
                        symlink_owners={0, uid},
                    )

    def test_group_or_world_writable_temporary_executable_is_rejected(self):
        uid = os.getuid()
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()) as temporary:
            root = pathlib.Path(temporary)
            executable = root / "writable-tool"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o775)

            with self.assertRaisesRegex(POLICY.UntrustedExecutablePath, "regular file"):
                POLICY.validate_executable(
                    executable,
                    directory_owners={0, uid},
                    executable_owners={0, uid},
                    symlink_owners={0, uid},
                )


if __name__ == "__main__":
    unittest.main()
