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

    def test_termination_kills_owned_descendant_after_child_leader_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            log = pathlib.Path(directory) / "bounded.log"
            child_pid_file = pathlib.Path(directory) / "child.pid"
            descendant_pid_file = pathlib.Path(directory) / "descendant.pid"
            child_code = (
                "import os,signal,time,pathlib; "
                "pid=os.fork(); "
                "pathlib.Path(os.environ['CHILD_PID_FILE']).write_text(str(os.getpid())) if pid else None; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN) if pid == 0 else None; "
                "pathlib.Path(os.environ['DESCENDANT_PID_FILE']).write_text(str(os.getpid())) if pid == 0 else None; "
                "print('CHILD_READY', flush=True); time.sleep(30)"
            )
            wrapper = subprocess.Popen(
                [PYTHON, "-I", str(LOGGER), str(log), "256", "--", PYTHON, "-c", child_code],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={**os.environ, "CHILD_PID_FILE": str(child_pid_file),
                     "DESCENDANT_PID_FILE": str(descendant_pid_file)},
            )
            child_pid = None
            descendant_pid = None
            try:
                for _ in range(100):
                    if child_pid_file.exists() and descendant_pid_file.exists():
                        child_pid = int(child_pid_file.read_text())
                        descendant_pid = int(descendant_pid_file.read_text())
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(child_pid)
                self.assertIsNotNone(descendant_pid)
                wrapper.send_signal(signal.SIGTERM)
                self.assertEqual(wrapper.wait(timeout=12), 143)
                state = None
                for _ in range(100):
                    try:
                        fields = pathlib.Path(f"/proc/{descendant_pid}/stat").read_text().rsplit(")", 1)[1].split()
                        state = fields[0]
                    except FileNotFoundError:
                        break
                    if state == "Z":
                        break
                    time.sleep(0.02)
                self.assertIn(state, (None, "Z"), "owned descendant survived wrapper termination")
            finally:
                if wrapper.poll() is None:
                    wrapper.kill()
                    wrapper.wait()
                if child_pid is not None and descendant_pid is not None:
                    try:
                        fields = pathlib.Path(f"/proc/{descendant_pid}/stat").read_text().rsplit(")", 1)[1].split()
                        if fields[2] == str(child_pid) and fields[3] == str(child_pid):
                            os.killpg(child_pid, signal.SIGKILL)
                    except (FileNotFoundError, ProcessLookupError):
                        pass

    def test_hangup_is_forwarded_and_logged_child_is_reaped(self):
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
            self.assertTrue(child_pid_file.exists())
            child_pid = int(child_pid_file.read_text())
            wrapper.send_signal(signal.SIGHUP)
            self.assertEqual(wrapper.wait(timeout=3), 129)
            for _ in range(100):
                if not pathlib.Path(f"/proc/{child_pid}").exists():
                    break
                time.sleep(0.02)
            self.assertFalse(pathlib.Path(f"/proc/{child_pid}").exists(), "logged child survived wrapper hangup")

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
