#!/usr/bin/env python3
"""Exercise bounded apt setup and the two hosted KVM workflow call sites."""

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests/ci-kvm-host-dependencies.sh"


class HostAptSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ci-kvm-host-apt-")
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.args_file = self.root / "apt-args"
        self._executable("sudo", """#!/usr/bin/env bash
set -euo pipefail
[[ "${1:-}" == -n ]]
shift
exec "$@"
""")
        self._executable("apt-get", """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$@" > "$FAKE_APT_ARGS_FILE"
printf '%s\\n' "${FAKE_APT_OUTPUT:-apt mock succeeded}"
exit "${FAKE_APT_EXIT_STATUS:-0}"
""")

    def _executable(self, name: str, content: str) -> None:
        path = self.bin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def _run(self, phase: str, *, status: int = 0, output: str = ""):
        environment = os.environ.copy()
        environment.update({
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "RUNNER_TEMP": str(self.root),
            "FAKE_APT_ARGS_FILE": str(self.args_file),
            "FAKE_APT_EXIT_STATUS": str(status),
            "FAKE_APT_OUTPUT": output or "apt mock succeeded",
        })
        return subprocess.run(
            ["bash", str(SCRIPT), phase], env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=15, check=False,
        )

    def test_update_uses_bounded_acquisition_and_lock_options(self):
        result = self._run("update")
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        args = self.args_file.read_text(encoding="utf-8").splitlines()
        self.assertIn("Acquire::Retries=1", args)
        self.assertIn("Acquire::http::Timeout=30", args)
        self.assertIn("Acquire::https::Timeout=30", args)
        self.assertIn("DPkg::Lock::Timeout=30", args)
        self.assertEqual(args[-1], "update")
        self.assertIn(b"HOST_APT_SETUP_PASS phase=update", result.stdout)
        self.assertIn(b"timeout_seconds=180", result.stdout)

    def test_install_failure_reports_phase_exit_and_sanitized_bounded_tail(self):
        lines = "".join(f"repository diagnostic line {i:04d} padded text\n" for i in range(500))
        lines += ("Failed to fetch https://apt-user:private-password@mirror.example/pkg.deb "
                  "token=private-token\n")
        result = self._run("install", status=100, output=lines)
        self.assertEqual(result.returncode, 100)
        diagnostic = result.stderr.split(b"HOST_APT_SETUP_DIAGNOSTICS_BEGIN\n", 1)[1]
        diagnostic = b"HOST_APT_SETUP_DIAGNOSTICS_BEGIN\n" + diagnostic.split(
            b"HOST_APT_SETUP_DIAGNOSTICS_END\n", 1)[0] + b"HOST_APT_SETUP_DIAGNOSTICS_END\n"
        self.assertLessEqual(len(diagnostic), 8192)
        text = result.stderr.decode("utf-8", "replace")
        self.assertIn("phase=install", text)
        self.assertIn("exit_status=100", text)
        self.assertIn("classification=network-acquisition", text)
        self.assertIn("duration_seconds=", text)
        self.assertIn("[REDACTED]@mirror.example", text)
        self.assertIn("token=[REDACTED]", text)
        self.assertNotIn("private-password", text)
        self.assertNotIn("private-token", text)
        args = self.args_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(args[-4:], ["cloud-image-utils", "openssh-server", "virtiofsd", "ncurses-bin"])
        self.assertIn("python3-pexpect", args)
        self.assertIn("qemu-system-x86", args)
        self.assertIn("--no-install-recommends", args)
        self.assertIn("--no-upgrade", args)
        self.assertIn("--no-remove", args)

    def test_sanitizes_bearer_and_url_credentials_before_truncating_records(self):
        prefix = "x" * 8170
        output = (prefix + " https://apt-user:boundary-secret@mirror.example/pkg.deb\n"
                  "Authorization: Bearer private-bearer-secret\n")
        result = self._run("install", status=100, output=output)
        text = result.stderr.decode("utf-8", "replace")
        self.assertNotIn("boundary-secret", text)
        self.assertNotIn("private-bearer-secret", text)
        self.assertIn("Authorization: [REDACTED]", text)
        self.assertIn("[REDACTED]@mirror.example", text)
        diagnostic = result.stderr.split(b"HOST_APT_SETUP_DIAGNOSTICS_BEGIN\n", 1)[1]
        diagnostic = b"HOST_APT_SETUP_DIAGNOSTICS_BEGIN\n" + diagnostic.split(
            b"HOST_APT_SETUP_DIAGNOSTICS_END\n", 1)[0] + b"HOST_APT_SETUP_DIAGNOSTICS_END\n"
        self.assertLessEqual(len(diagnostic), 8192)

    def test_applies_diagnostic_cap_after_expanding_redactions(self):
        output = "token=x " * 6000
        result = self._run("install", status=100, output=output)
        diagnostic = result.stderr.split(b"HOST_APT_SETUP_DIAGNOSTICS_BEGIN\n", 1)[1]
        diagnostic = b"HOST_APT_SETUP_DIAGNOSTICS_BEGIN\n" + diagnostic.split(
            b"HOST_APT_SETUP_DIAGNOSTICS_END\n", 1)[0] + b"HOST_APT_SETUP_DIAGNOSTICS_END\n"
        self.assertLessEqual(len(diagnostic), 8192)
        self.assertNotIn(b"token=x", diagnostic)
        self.assertIn(b"token=[REDACTED]", diagnostic)

    def test_lock_and_timeout_failures_are_classified_without_retrying_whole_phase(self):
        locked = self._run("update", status=100,
                           output="Could not get lock /var/lib/apt/lists/lock")
        self.assertEqual(locked.returncode, 100)
        self.assertIn(b"classification=package-manager-lock", locked.stderr)

        timed_out = self._run("install", status=124, output="APT command interrupted")
        self.assertEqual(timed_out.returncode, 124)
        self.assertIn(b"classification=timeout", timed_out.stderr)

    def test_both_workflow_jobs_have_separately_bounded_update_and_install_steps(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        transient = workflow.split("  transient-qemu-integration:", 1)[1].split(
            "  # ── 5. Exact installed production", 1)[0]
        production = workflow.split("  production-systemd-vm-acceptance:", 1)[1].split(
            "  # ── 6.", 1)[0]
        for job in (transient, production):
            self.assertIn("run: bash tests/ci-kvm-host-dependencies.sh update", job)
            self.assertIn("bash tests/ci-kvm-host-dependencies.sh install", job)
            self.assertIn("timeout-minutes: 4", job)
            self.assertIn("timeout-minutes: 10", job)
            self.assertLess(job.index("ci-kvm-host-dependencies.sh update"),
                            job.index("ci-kvm-host-dependencies.sh install"))
        self.assertIn("id: host_dependencies", production)
        self.assertIn("steps.host_dependencies.outcome == 'success'", production)
        self.assertNotIn("needs:", transient + production)


if __name__ == "__main__":
    unittest.main()
