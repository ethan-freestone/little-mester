import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECKER_PATH = ROOT / "scripts" / "vm-opencode-tui-check.py"
SPEC = importlib.util.spec_from_file_location("vm_opencode_tui_check", CHECKER_PATH)
CHECKER = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(CHECKER)


class OpenCodeTuiCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc_root = Path(self.temp.name)

    def add_process(self, pid, argv, *, tty_nr=34817, uid=None):
        uid = os.getuid() if uid is None else uid
        proc = self.proc_root / str(pid)
        proc.mkdir()
        (proc / "status").write_text(f"Name:\tnode\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        (proc / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")
        (proc / "stat").write_text(f"{pid} (node) S 1 {pid} {pid} {tty_nr} {pid} 0\n")
        return proc

    def find(self):
        return CHECKER.find_interactive_tui(self.proc_root, mester_uid=os.getuid())

    def test_attached_opencode_tui_is_detected(self):
        self.add_process(
            123,
            ["node", "/usr/local/lib/node_modules/@opencode/cli/bin/opencode"],
        )
        self.assertEqual(self.find(), ("123", "tty_nr=34817"))

    def test_headless_opencode_server_is_not_mistaken_for_tui(self):
        self.add_process(
            123,
            ["node", "/usr/local/lib/node_modules/@opencode/cli/bin/opencode", "serve"],
        )
        self.assertIsNone(self.find())

    def test_opencode_process_without_a_terminal_is_not_mistaken_for_tui(self):
        self.add_process(
            123,
            ["node", "/usr/local/lib/node_modules/@opencode/cli/bin/opencode"],
            tty_nr=0,
        )
        self.assertIsNone(self.find())

    def test_unrelated_process_arguments_do_not_look_like_opencode(self):
        self.add_process(123, ["grep", "opencode"], uid=os.getuid())
        self.assertIsNone(self.find())


if __name__ == "__main__":
    unittest.main()
