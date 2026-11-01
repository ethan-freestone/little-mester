"""Persistent guest recursion identity reporting tests."""

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("vm_context", ROOT / "scripts/vm-context.py")
vm_context = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(vm_context)


class RecursionContextTests(unittest.TestCase):
    def test_persisted_identity_marks_an_ordinary_guest_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = Path(directory) / "identity"
            identity.write_text("1 2\n")
            with patch.dict(os.environ, {}, clear=True):
                result = vm_context.recursion_context(identity)
        self.assertEqual(result["depth"], 1)
        self.assertEqual(result["max_depth"], 2)
        self.assertEqual(result["metadata"], "configured")
        self.assertTrue(result["child_depth_available"])

    def test_environment_must_match_persisted_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = Path(directory) / "identity"
            identity.write_text("1 2\n")
            with patch.dict(os.environ, {"LAB_VM_RECURSION_DEPTH": "0", "LAB_VM_MAX_DEPTH": "2"}, clear=True):
                result = vm_context.recursion_context(identity)
        self.assertEqual(result["metadata"], "invalid-or-incomplete")
        self.assertFalse(result["child_depth_available"])

    def test_invalid_or_symlinked_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = root / "identity"
            identity.write_text("1 2 extra\n")
            with patch.dict(os.environ, {}, clear=True):
                malformed = vm_context.recursion_context(identity)
            identity.unlink()
            target = root / "target"
            target.write_text("1 2\n")
            identity.symlink_to(target)
            with patch.dict(os.environ, {}, clear=True):
                symlinked = vm_context.recursion_context(identity)
        self.assertEqual(malformed["metadata"], "invalid-or-incomplete")
        self.assertEqual(symlinked["metadata"], "invalid-or-incomplete")


if __name__ == "__main__":
    unittest.main()
