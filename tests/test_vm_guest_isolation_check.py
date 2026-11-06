"""Fixture-only tests for bounded VM guest network-isolation probes."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/vm-guest-isolation-check.py"
SPEC = importlib.util.spec_from_file_location("vm_guest_isolation_check", SCRIPT)
isolation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(isolation)

RUN_ID = "12345678123456781234567812345678"


class GuestIsolationTests(unittest.TestCase):
    def passing_probes(self, **changes):
        options = {
            "run_id": RUN_ID,
            "tcp_probe": lambda _host, _port: False,
            "dns_probe": lambda: False,
            "udp_probe": lambda: False,
            "private_proxy_probe": lambda _url: 403,
            "public_http_probe": lambda: 200,
            "public_proxy_probe": lambda: 200,
            "guest_identity_probe": lambda: True,
            "workspace_probe": lambda: True,
            "ancestor_probe": lambda: True,
        }
        options.update(changes)
        return isolation.run_probes(**options)

    def test_all_fixed_checks_pass_only_with_matching_observations(self):
        result = self.passing_probes(expected_depth=1, expected_max_depth=2)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual([item["id"] for item in result["checks"]], list(isolation.CHECKS))
        self.assertTrue(all(item["status"] == "PASS" for item in result["checks"]))
        self.assertEqual(set(result), {"schema_version", "run_id", "created_utc", "result", "elapsed_ms", "checks"})

    def test_direct_private_proxy_denial_and_public_http_are_independent(self):
        seen = []
        result = self.passing_probes(private_proxy_probe=lambda url: seen.append(url) or 403)
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(seen, list(isolation.PROXY_PRIVATE_TARGETS))
        self.assertTrue(all(url.endswith(":80/") for url in seen))

        denied_http = self.passing_probes(public_http_probe=lambda: None)
        checks = {item["id"]: item["status"] for item in denied_http["checks"]}
        self.assertEqual(denied_http["result"], "FAIL")
        self.assertEqual(checks["proxy_public_http_available"], "FAIL")
        self.assertEqual(checks["proxy_public_https_available"], "PASS")

    def test_direct_private_egress_or_wrong_guest_identity_fails_closed(self):
        private_reachable = self.passing_probes(
            tcp_probe=lambda host, port: (host, port) == ("10.0.0.1", 22),
        )
        checks = {item["id"]: item["status"] for item in private_reachable["checks"]}
        self.assertEqual(checks["direct_private_subnet_blocked"], "FAIL")
        self.assertEqual(private_reachable["result"], "FAIL")

        with mock.patch.object(isolation, "_guest_identity", return_value=False):
            wrong_identity = self.passing_probes(
                guest_identity_probe=None, expected_depth=1, expected_max_depth=2,
            )
        checks = {item["id"]: item["status"] for item in wrong_identity["checks"]}
        self.assertEqual(checks["guest_identity"], "FAIL")
        self.assertEqual(wrong_identity["result"], "FAIL")

    def test_any_correlated_dns_reply_proves_direct_egress_even_without_answers(self):
        class FakeDNS:
            def settimeout(self, _timeout):
                pass

            def connect(self, _destination):
                pass

            def send(self, _packet):
                pass

            def recv(self, _maximum):
                return b"LM\x81\x83\x00\x01\x00\x00\x00\x00\x00\x00"

            def close(self):
                pass

        with mock.patch.object(isolation.socket, "socket", return_value=FakeDNS()):
            self.assertTrue(isolation._direct_dns_responds())

    def test_default_cli_is_plan_only_and_exact_confirmation_is_required(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(isolation, "run_probes") as probes:
            self.assertEqual(isolation.main(["--run-id", RUN_ID]), 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["result"], "NOT_RUN")
        self.assertEqual(plan["planned_checks"], list(isolation.CHECKS))
        probes.assert_not_called()

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(isolation, "run_probes") as probes:
            self.assertEqual(isolation.main([
                "--execute", "--run-id", RUN_ID, "--confirm-run-id", "f" * 32,
            ]), 2)
        probes.assert_not_called()

    def test_private_report_is_no_clobber_and_sanitized(self):
        with tempfile.TemporaryDirectory(prefix="lm-isolation-report-") as temporary:
            report_root = Path(temporary)
            report_root.chmod(0o700)
            report_path = report_root / f"{RUN_ID}.json"
            report = self.passing_probes()
            isolation._write_report(report_path, report)
            self.assertEqual(report_path.stat().st_mode & 0o777, 0o600)
            stored = json.loads(report_path.read_text(encoding="ascii"))
            self.assertEqual(stored["run_id"], RUN_ID)
            self.assertNotIn("url", report_path.read_text(encoding="ascii").lower())
            with self.assertRaises(isolation.IsolationCheckError):
                isolation._report_path(str(report_path))


if __name__ == "__main__":
    unittest.main()
