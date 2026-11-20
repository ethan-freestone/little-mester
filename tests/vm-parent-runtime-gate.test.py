#!/usr/bin/env python3
"""Pure argv regressions for the owner-only runtime gate; no QEMU is run."""

import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("parent_runtime_gate", ROOT / "tools/vm-parent-runtime-gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class RuntimeGateArgvTests(unittest.TestCase):
    def test_qemu_fdset_uses_actual_passed_descriptor(self):
        for descriptor in (3, 4, 19):
            argv = gate.qemu_argv("/usr/bin/qemu-system-x86_64", descriptor)
            add_fd = argv[argv.index("-add-fd") + 1]
            self.assertEqual(add_fd, f"fd={descriptor},set=1,opaque=parent-disk")
            self.assertIn("filename=/dev/fdset/1", argv[argv.index("-blockdev") + 1])
            self.assertIn("locking=on", argv[argv.index("-blockdev") + 1])
            self.assertIn("-S", argv)
            self.assertNotIn("-netdev", argv)


if __name__ == "__main__":
    unittest.main()
