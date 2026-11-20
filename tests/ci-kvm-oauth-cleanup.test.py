#!/usr/bin/env python3
"""Exercise the stage EXIT cleanup against a live OAuth harness and child group."""

from __future__ import annotations

import json
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
STAGE = ROOT / "tests/ci-kvm-stage.sh"
HARNESS = ROOT / "tests/ci-qemu-oauth-bridge.py"
SIGNAL_HELPER = ROOT / "tests/ci-process-signal.py"
FUNCTION_BEGIN = "# BEGIN: live OAuth harness cleanup primitives; exercised by ci-kvm-oauth-cleanup.test.py."
FUNCTION_END = "# END: live OAuth harness cleanup primitives."


def cleanup_functions() -> str:
    source = STAGE.read_text(encoding="utf-8")
    start = source.index(FUNCTION_BEGIN)
    end = source.index(FUNCTION_END, start) + len(FUNCTION_END)
    return source[start:end]


def driver_source() -> str:
    return f'''#!/usr/bin/python3
import importlib.util
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

root = pathlib.Path({str(ROOT)!r})
spec = importlib.util.spec_from_file_location("oauth_harness", root / "tests/ci-qemu-oauth-bridge.py")
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
process_spec = importlib.util.spec_from_file_location("process_signal", root / "tests/ci-process-signal.py")
process = importlib.util.module_from_spec(process_spec)
process_spec.loader.exec_module(process)
ready = pathlib.Path(sys.argv[1])
proof = pathlib.Path(sys.argv[2])

class LiveControlledSession(harness.Session):
    def start(self):
        self.descendant = subprocess.Popen(
            [sys.executable, "-c", "import signal; signal.pause()"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
        )
        self.bridge_record = harness.write_bridge_identity(self.bridge_identity, self.descendant.pid)
        ready.write_text("ready\\n", encoding="ascii")
        return ("https://auth.openai.com/oauth/authorize?"
                "redirect_uri=http%3A%2F%2F127.0.0.1%3A43191%2Fcallback&state=controlled-cancel")

    def close(self):
        if self.closed:
            return
        result = process.main(["--terminate-marked-groups", str(self.root)])
        if result != 0:
            raise RuntimeError("controlled OAuth bridge descendant cleanup was not proved")
        try:
            self.descendant.wait(timeout=3)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("controlled OAuth bridge descendant did not reap") from error
        if not process.process_session_empty(self.bridge_record["session_id"]):
            raise RuntimeError("controlled OAuth bridge session remains live")
        self.control_dir and __import__("shutil").rmtree(self.control_dir, ignore_errors=True)
        __import__("shutil").rmtree(self.root, ignore_errors=False)
        proof.write_text(json.dumps({{
            "harness_cancelled": True,
            "bridge_pid": self.descendant.pid,
            "bridge_session_id": self.bridge_record["session_id"],
            "bridge_reaped": self.descendant.poll() is not None,
            "bridge_session_empty": process.process_session_empty(self.bridge_record["session_id"]),
        }}, sort_keys=True), encoding="utf-8")
        self.closed = True

harness.Session = LiveControlledSession
def wait_for_controlled_cancellation(_port, _parameters):
    signal.pause()

harness.request_callback = wait_for_controlled_cancellation
sys.argv = [str(root / "tests/ci-qemu-oauth-bridge.py"),
            "--port", "43191", "--identity", sys.argv[3],
            "--known-hosts", sys.argv[4], "--evidence-root", sys.argv[5],
            "--max-runtime-seconds", "30"]
raise SystemExit(harness.main())
'''


class OAuthCleanupCancellationTests(unittest.TestCase):
    def test_live_harness_term_is_pidfd_signalled_reaped_and_cleans_bridge_descendant(self):
        with tempfile.TemporaryDirectory(prefix="ci-kvm-oauth-cancel-") as directory:
            root = pathlib.Path(directory)
            work = root / "work"
            work.mkdir(mode=0o700)
            evidence = work / "oauth-evidence"
            evidence.mkdir(mode=0o700)
            identity = work / "identity"
            known_hosts = work / "known_hosts"
            identity.write_text("synthetic key\n", encoding="ascii")
            known_hosts.write_text("synthetic host\n", encoding="ascii")
            identity.chmod(0o600)
            known_hosts.chmod(0o600)
            ready = work / "live-harness.ready"
            proof = work / "cleanup-proof.json"
            driver = work / "controlled-oauth-harness.py"
            driver.write_text(driver_source(), encoding="utf-8")
            driver.chmod(0o700)
            functions = work / "stage-oauth-cleanup-functions.sh"
            functions.write_text(cleanup_functions(), encoding="utf-8")
            shell = work / "exercise-stage-cleanup.sh"
            shell.write_text(
                "set -Eeuo pipefail\n"
                f"process_signal_helper={str(SIGNAL_HELPER)!r}\n"
                f"source {str(functions)!r}\n"
                f"oauth_evidence_root={str(evidence)!r}\n"
                "oauth_cleanup_unproved=0\n"
                "oauth_pid='' oauth_starttime='' oauth_uid='' oauth_parent=''\n"
                f"python3 -I {str(driver)!r} {str(ready)!r} {str(proof)!r} "
                f"{str(identity)!r} {str(known_hosts)!r} {str(evidence)!r} &\n"
                "oauth_pid=$!\n"
                "oauth_starttime=$(process_starttime \"$oauth_pid\")\n"
                "oauth_uid=$(process_uid \"$oauth_pid\")\n"
                "oauth_parent=$BASHPID\n"
                "harness_pid=$oauth_pid\n"
                "fallback_cleanup() {\n"
                "  if [[ -n \"${oauth_pid:-}\" ]]; then\n"
                "    signal_owned_process \"$oauth_pid\" \"$oauth_starttime\" \"$oauth_uid\" \"$oauth_parent\" '*' TERM >/dev/null 2>&1 || true\n"
                "    for _ in {1..30}; do\n"
                "      state=$(process_state \"$oauth_pid\")\n"
                "      [[ -z \"$state\" || \"$state\" == Z ]] && break\n"
                "      sleep 0.1\n"
                "    done\n"
                "    wait \"$oauth_pid\" 2>/dev/null || true\n"
                "  fi\n"
                "}\n"
                "trap fallback_cleanup EXIT\n"
                "for _ in {1..100}; do [[ -s " + repr(str(ready)) + " ]] && break; sleep 0.05; done\n"
                f"[[ -s {str(ready)!r} ]] || {{ printf 'harness did not become live\\n' >&2; exit 1; }}\n"
                "stop_oauth_harness\n"
                "[[ -z \"$oauth_pid\" && $oauth_cleanup_unproved == 0 ]]\n"
                "[[ -z $(process_state \"$harness_pid\") ]]\n"
                f"[[ -d {str(evidence)!r} && -z $(find {str(evidence)!r} -mindepth 1 -maxdepth 1 -print -quit) ]]\n"
                "trap - EXIT\n",
                encoding="utf-8",
            )
            shell.chmod(0o700)
            result = subprocess.run(
                ["bash", str(shell)], capture_output=True, text=True, timeout=15, check=False,
            )
            self.assertEqual(result.returncode, 0, f"stdout={result.stdout}\nstderr={result.stderr}")
            self.assertIn("QEMU_OAUTH_SIMULATOR_CANCELLED: cleanup attempted after signal 15", result.stderr)
            self.assertTrue(proof.is_file(), "live OAuth harness did not publish cleanup proof")
            cleanup = json.loads(proof.read_text(encoding="utf-8"))
            self.assertTrue(cleanup["harness_cancelled"])
            self.assertTrue(cleanup["bridge_reaped"])
            self.assertTrue(cleanup["bridge_session_empty"])
            self.assertFalse(pathlib.Path(f"/proc/{cleanup['bridge_pid']}").exists())


if __name__ == "__main__":
    unittest.main()
