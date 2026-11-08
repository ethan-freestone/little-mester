import os
import pathlib
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = pathlib.Path(__file__).parent.parent
LOGGER = ROOT / "lib/vm/bounded-log.py"
PYTHON = sys.executable


class BoundedLogTests(unittest.TestCase):
    def test_stream_is_capped_and_child_exit_code_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            log = pathlib.Path(directory) / "bounded.log"
            result = subprocess.run(
                [PYTHON, "-I", str(LOGGER), str(log), "256", "--", PYTHON, "-c",
                 "import sys; sys.stdout.write('x' * 10000)"]
            )
            self.assertEqual(result.returncode, 0)
            self.assertLessEqual(log.stat().st_size, 256)
            self.assertIn(b"[bounded-log: output truncated]", log.read_bytes())
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)

            failed = subprocess.run(
                [PYTHON, "-I", str(LOGGER), str(log), "256", "--", PYTHON, "-c", "raise SystemExit(23)"]
            )
            self.assertEqual(failed.returncode, 23)

    def test_termination_is_forwarded_and_logged_child_is_reaped(self):
        with tempfile.TemporaryDirectory() as directory:
            log = pathlib.Path(directory) / "bounded.log"
            child_pid_file = pathlib.Path(directory) / "child.pid"
            wrapper = subprocess.Popen(
                [PYTHON, "-I", str(LOGGER), str(log), "256", "--", PYTHON, "-c",
                 "import os,time,pathlib; pathlib.Path(os.environ['CHILD_PID_FILE']).write_text(str(os.getpid())); print('CHILD_READY', flush=True); time.sleep(30)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={**os.environ, "CHILD_PID_FILE": str(child_pid_file)},
            )
            for _ in range(100):
                if log.exists() and b"CHILD_READY" in log.read_bytes():
                    break
                time.sleep(0.02)
            self.assertTrue(log.exists())
            self.assertIn(b"CHILD_READY", log.read_bytes())
            self.assertTrue(child_pid_file.exists())
            child_pid = int(child_pid_file.read_text())
            wrapper.send_signal(signal.SIGTERM)
            self.assertEqual(wrapper.wait(timeout=3), 143)
            for _ in range(100):
                if not pathlib.Path(f"/proc/{child_pid}").exists():
                    break
                time.sleep(0.02)
            self.assertFalse(pathlib.Path(f"/proc/{child_pid}").exists(), "logged child survived wrapper termination")

    def test_external_deadline_terminates_wrapper_and_real_child(self):
        with tempfile.TemporaryDirectory() as directory:
            log = pathlib.Path(directory) / "bounded.log"
            child_pid_file = pathlib.Path(directory) / "child.pid"
            result = subprocess.run(
                ["timeout", "--signal=TERM", "--kill-after=2", "0.3", PYTHON, "-I", str(LOGGER),
                 str(log), "256", "--", PYTHON, "-c",
                 "import os,time,pathlib; pathlib.Path(os.environ['CHILD_PID_FILE']).write_text(str(os.getpid())); print('CHILD_READY', flush=True); time.sleep(30)"],
                env={**os.environ, "CHILD_PID_FILE": str(child_pid_file)},
                timeout=5,
            )
            self.assertEqual(result.returncode, 124)
            self.assertTrue(child_pid_file.exists())
            child_pid = int(child_pid_file.read_text())
            for _ in range(100):
                if not pathlib.Path(f"/proc/{child_pid}").exists():
                    break
                time.sleep(0.02)
            self.assertFalse(pathlib.Path(f"/proc/{child_pid}").exists(), "child survived external timeout")


if __name__ == "__main__":
    unittest.main()
