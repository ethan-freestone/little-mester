import ast
import contextlib
import io
import os
import pathlib
import re
import stat
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
LAB_VM = ROOT / "lab-vm"


def load_bootstrap_validator():
    source = LAB_VM.read_text(encoding="utf-8")
    match = re.search(r"<<'PY'\n(.*?)\nPY\n", source, flags=re.DOTALL)
    if match is None:
        raise AssertionError("lab-vm bootstrap Python block was not found")

    parsed = ast.parse(match.group(1), filename=str(LAB_VM))
    functions = {
        node.name: node
        for node in parsed.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"refuse", "validate_bootstrap_executable"}
    }
    if set(functions) != {"refuse", "validate_bootstrap_executable"}:
        raise AssertionError("lab-vm bootstrap validator helper was not found")

    module = ast.Module(body=list(functions.values()), type_ignores=[])
    namespace = {"os": os, "pathlib": pathlib, "stat": stat, "sys": __import__("sys")}
    exec(compile(module, str(LAB_VM), "exec"), namespace)
    return namespace["validate_bootstrap_executable"]


VALIDATE_BOOTSTRAP_EXECUTABLE = load_bootstrap_validator()


class BootstrapPolicyTests(unittest.TestCase):
    def _validate(self, path, *, uid_overrides=None, mode_overrides=None):
        uid_overrides = uid_overrides or {}
        mode_overrides = mode_overrides or {}
        real_lstat = os.lstat
        error_output = io.StringIO()

        def synthetic_lstat(candidate):
            candidate = pathlib.Path(candidate)
            info = real_lstat(candidate)
            mode = mode_overrides.get(candidate, info.st_mode)
            if candidate == pathlib.Path("/tmp"):
                # Model the protected system directory rather than /tmp's real
                # host-specific mode; all files and links remain real fixtures.
                mode &= ~(stat.S_IWGRP | stat.S_IWOTH)
            return SimpleNamespace(
                st_mode=mode,
                st_uid=uid_overrides.get(candidate, 0),
            )

        with mock.patch.object(os, "lstat", side_effect=synthetic_lstat):
            with contextlib.redirect_stderr(error_output):
                try:
                    VALIDATE_BOOTSTRAP_EXECUTABLE(str(path))
                except SystemExit as error:
                    self.assertEqual(error.code, 1)
                    return error_output.getvalue()
        return None

    @staticmethod
    def _executable(path, mode=0o755):
        path.write_text("synthetic executable\n", encoding="utf-8")
        path.chmod(mode)
        return path

    @staticmethod
    def _directory(path, mode=0o755):
        path.mkdir()
        path.chmod(mode)
        return path

    def test_accepts_root_owned_0777_symlink_to_protected_executable(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="vm-bootstrap-") as temporary:
            root = pathlib.Path(temporary)
            target_dir = root / "protected"
            self._directory(target_dir)
            target = self._executable(target_dir / "python3.14")
            alias = root / "python3"
            alias.symlink_to(target)

            self.assertEqual(os.lstat(alias).st_mode & 0o777, 0o777)
            self.assertIsNone(self._validate(alias))

    def test_rejects_symlink_with_unsafe_ownership(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="vm-bootstrap-") as temporary:
            root = pathlib.Path(temporary)
            target = self._executable(root / "python3.14")
            alias = root / "python3"
            alias.symlink_to(target)

            message = self._validate(alias, uid_overrides={alias: 1000})
            self.assertIn("symlink must be root-owned", message)

    def test_rejects_target_under_writable_ancestry(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="vm-bootstrap-") as temporary:
            root = pathlib.Path(temporary)
            writable = root / "writable"
            self._directory(writable)
            target = self._executable(writable / "python3.14")
            alias = root / "python3"
            alias.symlink_to(target)
            writable_mode = os.lstat(writable).st_mode | stat.S_IWGRP

            message = self._validate(alias, mode_overrides={writable: writable_mode})
            self.assertIn("every ancestor", message)

    def test_rejects_unowned_intermediate_symlink_in_target_path(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="vm-bootstrap-") as temporary:
            root = pathlib.Path(temporary)
            target_dir = root / "protected"
            self._directory(target_dir)
            self._executable(target_dir / "python3.14")
            intermediate = root / "target-dir"
            intermediate.symlink_to(target_dir, target_is_directory=True)
            alias = root / "python3"
            alias.symlink_to("target-dir/python3.14")

            message = self._validate(alias, uid_overrides={intermediate: 1000})
            self.assertIn("symlink must be root-owned", message)

    def test_rejects_writable_directory_reached_through_intermediate_symlink(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="vm-bootstrap-") as temporary:
            root = pathlib.Path(temporary)
            writable = root / "writable-target"
            self._directory(writable)
            self._executable(writable / "python3.14")
            intermediate = root / "target-dir"
            intermediate.symlink_to(writable, target_is_directory=True)
            alias = root / "python3"
            alias.symlink_to("target-dir/python3.14")
            writable_mode = os.lstat(writable).st_mode | stat.S_IWOTH

            message = self._validate(alias, mode_overrides={writable: writable_mode})
            self.assertIn("every ancestor", message)

    def test_rejects_unowned_non_executable_and_nonregular_final_targets(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="vm-bootstrap-") as temporary:
            root = pathlib.Path(temporary)

            unowned_target = self._executable(root / "unowned", mode=0o755)
            unowned_alias = root / "unowned-link"
            unowned_alias.symlink_to(unowned_target)
            message = self._validate(unowned_alias, uid_overrides={unowned_target: 1000})
            self.assertIn("not executable", message)

            non_executable = self._executable(root / "not-executable", mode=0o644)
            non_executable_alias = root / "non-executable-link"
            non_executable_alias.symlink_to(non_executable)
            message = self._validate(non_executable_alias)
            self.assertIn("not executable", message)

            directory = root / "directory-target"
            self._directory(directory)
            directory_alias = root / "directory-link"
            directory_alias.symlink_to(directory, target_is_directory=True)
            message = self._validate(directory_alias)
            self.assertIn("not executable", message)


if __name__ == "__main__":
    unittest.main()
