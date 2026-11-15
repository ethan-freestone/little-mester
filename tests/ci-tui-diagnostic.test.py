#!/usr/bin/env python3
"""Tests for bounded, allowlisted CI TUI diagnostics."""

import importlib.util
import pathlib
import subprocess
import sys
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).with_name("ci-tui-diagnostic.py")
SPEC = importlib.util.spec_from_file_location("ci_tui_diagnostic", SCRIPT)
DIAGNOSTIC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DIAGNOSTIC)


class TuiDiagnosticTests(unittest.TestCase):
    def test_classifies_causal_error_without_returning_terminal_text(self):
        secret = b"\x1b[31mEACCES /etc/opencode/service.json token=private-secret\x1b[0m"
        cause = DIAGNOSTIC.classify(secret)
        self.assertEqual(cause, "protected-service-config-write")
        self.assertNotIn("private-secret", cause)
        self.assertNotIn("/etc/opencode", cause)

    def test_capture_is_private_and_bounded_while_draining_input(self):
        with tempfile.TemporaryDirectory(prefix="ci-tui-capture-") as directory:
            output = pathlib.Path(directory) / "terminal.raw"
            source = b"x" * (DIAGNOSTIC.MAX_CAPTURE_BYTES + 4096)
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "capture", str(output)],
                input=source, capture_output=True, check=False, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.stat().st_size, DIAGNOSTIC.MAX_CAPTURE_BYTES)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_failure_report_retains_private_raw_and_emits_only_fixed_classification(self):
        with tempfile.TemporaryDirectory(prefix="ci-tui-report-") as directory:
            root = pathlib.Path(directory)
            source = root / "terminal.raw"
            raw = b"EACCES /etc/opencode/service.json token=private-secret\n"
            source.write_bytes(raw)
            source.chmod(0o600)
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "report", "--input", str(source),
                 "--phase", "startup", "--actual-exit", "1", "--expected-exit", "124",
                 "--retain-directory", str(root)],
                capture_output=True, text=True, check=False, timeout=10,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("cause=protected-service-config-write", result.stdout)
            self.assertIn("private_raw=", result.stdout)
            self.assertNotIn("private-secret", result.stdout)
            self.assertNotIn("/etc/opencode/service.json", result.stdout)
            retained_path = pathlib.Path(result.stdout.strip().split("private_raw=", 1)[1])
            self.assertEqual(retained_path.read_bytes(), raw)
            self.assertEqual(retained_path.stat().st_mode & 0o777, 0o600)

    def test_success_requires_expected_exit_and_prompt(self):
        with tempfile.TemporaryDirectory(prefix="ci-tui-success-") as directory:
            root = pathlib.Path(directory)
            source = root / "terminal.raw"
            source.write_bytes(b"Ask anything")
            source.chmod(0o600)
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "report", "--input", str(source),
                 "--phase", "startup", "--actual-exit", "124", "--expected-exit", "124",
                 "--retain-directory", str(root)],
                capture_output=True, text=True, check=False, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(
                result.stdout.strip(),
                "KVM_TUI_DIAGNOSTIC phase=startup result=PASS "
                "cause=tui-prompt-reached captured_bytes=12",
            )
            self.assertNotIn("private_raw=", result.stdout)

    def test_malformed_osc_sequences_are_stripped_in_bounded_time(self):
        code = (
            "import importlib.util,sys; "
            f"s=importlib.util.spec_from_file_location('diag',{str(SCRIPT)!r}); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            "print(m.classify(b'\\x1b]' * 65536))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            check=False, timeout=2,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "tui-prompt-not-observed")

    def test_expected_timeout_pipeline_does_not_run_inherited_err_trap(self):
        with tempfile.TemporaryDirectory(prefix="ci-tui-pipeline-") as directory:
            output = pathlib.Path(directory) / "terminal.raw"
            command = (
                "set -Eeuo pipefail; trap 'printf ERR_TRAP_FIRED' ERR; "
                f"if bash -c 'exit 124' | {sys.executable} {SCRIPT} capture {output}; then "
                "statuses=(\"${PIPESTATUS[@]}\"); "
                "else statuses=(\"${PIPESTATUS[@]}\"); fi; "
                "printf 'statuses=%s,%s\\n' \"${statuses[0]}\" \"${statuses[1]}\""
            )
            result = subprocess.run(
                ["bash", "-c", command], capture_output=True, text=True,
                check=False, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("statuses=124,0", result.stdout)
            self.assertNotIn("ERR_TRAP_FIRED", result.stdout)
            self.assertEqual(output.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
