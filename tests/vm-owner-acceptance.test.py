import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import stat
import subprocess
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
ENTRY = ROOT / "tools/vm-owner-acceptance"
loader = importlib.machinery.SourceFileLoader("vm_owner_acceptance", str(ENTRY))
spec = importlib.util.spec_from_loader(loader.name, loader)
acceptance = importlib.util.module_from_spec(spec)
loader.exec_module(acceptance)


class OwnerAcceptanceTests(unittest.TestCase):
    def test_cli_requires_full_sha_and_rejects_multiple_operations(self):
        with self.assertRaises(SystemExit):
            acceptance.parser().parse_args(["--install", "--sha", "abc"])
        with self.assertRaises(SystemExit):
            acceptance.parser().parse_args(["--install", "--run", "--sha", "a" * 40])
        parsed = acceptance.parser().parse_args(["--sha", "a" * 40])
        self.assertFalse(any(getattr(parsed, name) for name in (
            "preflight", "install", "storage_proof", "run", "verify_cleanup", "all"
        )))

    def test_candidate_requires_exact_commit_and_matching_runner_bytes(self):
        with mock.patch.object(acceptance, "git") as git:
            with self.assertRaises(acceptance.GateError):
                acceptance.candidate_commit(ROOT, "not-a-full-sha")
            git.assert_not_called()

            sha = "a" * 40
            git.side_effect = [
                subprocess.CompletedProcess([], 0, (sha + "\n").encode(), b""),
                subprocess.CompletedProcess([], 0, ENTRY.read_bytes(), b""),
            ]
            acceptance.candidate_commit(ROOT, sha)
            self.assertEqual(git.call_count, 2)

    def test_existing_installation_is_never_overwritten(self):
        sha = "a" * 40
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary) / sha
            target.mkdir()
            marker = target / "preserve"
            marker.write_text("historical", encoding="utf-8")
            with mock.patch.object(acceptance, "INSTALL_PARENT", pathlib.Path(temporary)):
                with self.assertRaises(acceptance.GateError):
                    acceptance.install_candidate(ROOT, sha)
            self.assertEqual(marker.read_text(encoding="utf-8"), "historical")

    def test_install_archive_contains_exactly_the_worker_allowlisted_runtime(self):
        head = subprocess.run(
            [acceptance.GIT, "-C", str(ROOT), "rev-parse", "HEAD"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=acceptance.clean_env(),
        ).stdout.decode("ascii").strip()
        result = subprocess.run(
            acceptance.archive_arguments(ROOT, head), check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**acceptance.clean_env(), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
        )
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
            names = {member.name.rstrip("/") for member in archive.getmembers()}
        manifest = json.loads(acceptance.installer_manifest())
        expected = set(manifest["files"]) | set(manifest["directories"])
        self.assertEqual(names, expected)
        self.assertEqual(set(acceptance.archive_arguments(ROOT, head)[6:]), set(manifest["files"]))
        self.assertNotIn(".github", names)
        self.assertIn('json.loads(sys.argv[2])', acceptance.INSTALL_WORKER)

    def test_default_operation_is_read_only_preflight_and_reports_blocked_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = pathlib.Path(temporary)
            report_dir = parent / "attempt"
            report_dir.mkdir(mode=0o700)
            with (
                mock.patch.object(acceptance.pathlib.Path, "resolve", autospec=True,
                                  side_effect=lambda path, *a, **kw: pathlib.Path(path)),
                mock.patch.object(acceptance, "new_report_dir", return_value=report_dir),
                mock.patch.object(acceptance, "candidate_commit"),
                mock.patch.object(acceptance, "install_path", return_value=parent / "missing-install"),
                mock.patch.object(acceptance, "install_candidate") as install,
                mock.patch.object(acceptance, "installed_preflight") as preflight,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = acceptance.main(["--sha", "a" * 40])
            install.assert_not_called()
            preflight.assert_not_called()
            self.assertEqual(result, 3)
            report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "BLOCKED")
            self.assertEqual(report["gates"]["candidate"], "PASS")
            self.assertEqual(report["gates"]["preflight"], "BLOCKED")
            self.assertEqual(report["gates"]["install"], "NOT_RUN")
            self.assertEqual(report["gates"]["run"], "NOT_RUN")

    def test_each_report_directory_and_report_file_is_exclusive(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = pathlib.Path(temporary)
            first = acceptance.new_report_dir(parent)
            second = acceptance.new_report_dir(parent)
            self.assertNotEqual(first, second)
            self.assertEqual(stat.S_IMODE(os.lstat(first).st_mode), 0o700)
            data = acceptance.report_template("a" * 40, "preflight", first)
            acceptance.save_report(first, data)
            report = first / "report.json"
            self.assertEqual(stat.S_IMODE(os.lstat(report).st_mode), 0o600)
            self.assertEqual(json.loads(report.read_text())["status"], "IN_PROGRESS")
            with self.assertRaises(FileExistsError):
                acceptance.save_report(first, data)
            self.assertNotIn("authorization_url", report.read_text())

    def test_storage_proof_bounds_attempt_to_capacity_plus_one_and_cleans_only_own_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = pathlib.Path(temporary)
            os.chmod(workspace, 0o700)
            real_fdopen = os.fdopen

            class FullFilesystem:
                def __init__(self, stream):
                    self.stream = stream
                    self.written = 0
                    self.attempts = []

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    self.stream.close()

                def write(self, data):
                    self.attempts.append(len(data))
                    if self.written + len(data) > 8:
                        raise OSError(28, "No space left on device")
                    count = self.stream.write(data)
                    self.written += count
                    return count

            writers = []
            anonymous_files = []

            def fdopen(*args, **kwargs):
                writer = FullFilesystem(real_fdopen(*args, **kwargs))
                writers.append(writer)
                return writer

            usage = SimpleNamespace(f_blocks=8, f_frsize=1)
            with (
                mock.patch.object(acceptance, "WORKSPACE_PROOF_BYTES", 8),
                mock.patch.object(acceptance, "open_anonymous_probe", side_effect=lambda directory: (
                    anonymous_files.append(tempfile.TemporaryFile(dir=directory))
                    or os.dup(anonymous_files[-1].fileno())
                )),
                mock.patch.object(acceptance, "mount_facts", return_value=(
                    "tmpfs", {"nosuid", "nodev", "noexec"},
                    {"mode=700", f"uid={os.getuid()}", f"gid={os.getgid()}", "size=8k"},
                )),
                mock.patch.object(acceptance.os, "statvfs", return_value=usage),
                mock.patch.object(acceptance.os, "fdopen", side_effect=fdopen),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                acceptance.storage_proof(str(workspace))

            self.assertEqual(output.getvalue(), "ENOSPC_CONFIRMED after 67108864 bytes; 67108865 bytes attempted at most\n")
            self.assertEqual(writers[0].written, 8)
            self.assertEqual(writers[0].attempts, [8, 1])
            self.assertEqual(list(workspace.iterdir()), [])
            for anonymous in anonymous_files:
                anonymous.close()

    def test_storage_proof_refuses_non_tmpfs_and_never_fills_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = pathlib.Path(temporary)
            os.chmod(workspace, 0o700)
            usage = SimpleNamespace(f_blocks=64 * 1024 * 1024, f_frsize=1)
            with (
                mock.patch.object(acceptance, "mount_facts", return_value=("ext4", set(), set())),
                mock.patch.object(acceptance.os, "statvfs", return_value=usage),
            ):
                with self.assertRaises(acceptance.GateError):
                    acceptance.storage_proof(str(workspace))
            self.assertEqual(list(workspace.iterdir()), [])

    def test_smoke_requires_tty_then_inherits_terminal_without_capturing_oauth_output(self):
        class NotTTY(io.StringIO):
            def isatty(self):
                return False

        sha = "b" * 40
        root = pathlib.Path("/protected/install")
        data = acceptance.report_template(sha, "run", pathlib.Path("/private/report"))
        with mock.patch.object(acceptance.sys, "stdin", NotTTY()), \
                mock.patch.object(acceptance.subprocess, "run") as run:
            with self.assertRaises(acceptance.GateError):
                acceptance.run_smoke(root, "/workspace", "openai/model", set(), data)
            run.assert_not_called()

        class TTY(io.StringIO):
            def isatty(self):
                return True

        state = pathlib.Path("/private/state")
        with (
            mock.patch.object(acceptance.sys, "stdin", TTY()),
            mock.patch.object(acceptance.sys, "stdout", TTY()),
            mock.patch.object(acceptance, "verify_workspace_mount", return_value=pathlib.Path("/workspace")),
            mock.patch.object(acceptance.pathlib.Path, "iterdir", return_value=iter(())),
            mock.patch.object(acceptance, "state_root", return_value=state),
            mock.patch.object(acceptance, "list_runs", return_value={"20261109T154005Z-" + "a" * 32}),
            mock.patch.object(acceptance, "inspect_cleanup", return_value={"status": "PASS"}),
            mock.patch.object(acceptance.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run,
        ):
            acceptance.run_smoke(root, "/workspace", "openai/model", set(), data)
        args, kwargs = run.call_args
        self.assertEqual(args[0], [str(root / "lab-vm"), "smoke", "--workspace", "/workspace", "--model", "openai/model"])
        self.assertIsNone(kwargs["stdin"])
        self.assertIsNone(kwargs["stdout"])
        self.assertIsNone(kwargs["stderr"])
        self.assertEqual(data["gates"]["run"], "PASS")
        self.assertEqual(data["gates"]["cleanup"], "PASS")

    def test_nonzero_smoke_is_blocked_and_never_claims_cleanup(self):
        class TTY(io.StringIO):
            def isatty(self):
                return True

        run_id = "20261109T154005Z-" + "c" * 32
        data = acceptance.report_template("c" * 40, "run", pathlib.Path("/private/report"))
        with (
            mock.patch.object(acceptance.sys, "stdin", TTY()),
            mock.patch.object(acceptance.sys, "stdout", TTY()),
            mock.patch.object(acceptance, "verify_workspace_mount", return_value=pathlib.Path("/workspace")),
            mock.patch.object(acceptance.pathlib.Path, "iterdir", return_value=iter(())),
            mock.patch.object(acceptance, "state_root", return_value=pathlib.Path("/private/state")),
            mock.patch.object(acceptance, "list_runs", return_value={run_id}),
            mock.patch.object(acceptance.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)),
        ):
            with self.assertRaises(acceptance.GateError):
                acceptance.run_smoke(pathlib.Path("/protected/install"), "/workspace", "openai/model", set(), data)
        self.assertEqual(data["gates"]["run"], "BLOCKED")
        self.assertEqual(data["gates"]["cleanup"], "NOT_RUN")
        self.assertEqual(data["evidence"]["smoke_exit_code"], 1)

    def test_retained_stale_pid_is_never_authority_and_historical_files_are_not_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_id = "20261109T154005Z-" + "d" * 32
            run_dir = pathlib.Path(temporary) / run_id
            run_dir.mkdir(mode=0o700)
            for name in ("qemu.log", "virtiofsd.log", "proxy.log"):
                (run_dir / name).touch(mode=0o600)
            sock = run_dir / "virtiofs.sock"
            import socket
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(sock))
            listener.close()
            child = subprocess.Popen(["/usr/bin/sleep", "30"])
            pid_file = run_dir / "virtiofs.sock.pid"
            pid_file.write_text(f"{child.pid}\n", encoding="ascii")
            os.chmod(pid_file, 0o600)
            with mock.patch.object(acceptance, "state_root", return_value=pathlib.Path(temporary)), \
                    mock.patch.object(acceptance, "verify_user_directory_chain"), \
                    mock.patch.object(acceptance, "socket_listener_present", return_value=False):
                result = acceptance.inspect_cleanup("e" * 40, run_id)
            self.assertEqual(result["status"], "PASS")
            self.assertTrue(sock.exists())
            self.assertEqual(pid_file.read_text(encoding="ascii"), f"{child.pid}\n")
            self.assertIsNone(child.poll())
            child.terminate()
            child.wait(timeout=5)

    def test_live_socket_listener_blocks_readonly_cleanup_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_id = "20261109T154005Z-" + "e" * 32
            run_dir = pathlib.Path(temporary) / run_id
            run_dir.mkdir(mode=0o700)
            for name in ("qemu.log", "virtiofsd.log", "proxy.log"):
                (run_dir / name).touch(mode=0o600)
            sock = run_dir / "virtiofs.sock"
            import socket
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(sock))
            try:
                with mock.patch.object(acceptance, "state_root", return_value=pathlib.Path(temporary)), \
                        mock.patch.object(acceptance, "verify_user_directory_chain"), \
                        mock.patch.object(acceptance, "socket_listener_present", return_value=True):
                    with self.assertRaises(acceptance.GateError):
                        acceptance.inspect_cleanup("f" * 40, run_id)
                self.assertTrue(sock.exists())
            finally:
                listener.close()

    def test_proc_unix_listener_path_preserves_repeated_spaces(self):
        import socket
        with tempfile.TemporaryDirectory(prefix="vm owner  ") as temporary:
            path = pathlib.Path(temporary) / "virtiofs  socket.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(path))
                self.assertTrue(acceptance.socket_listener_present(str(path)))
            finally:
                listener.close()

    def test_cleanup_verification_blocks_unexpected_retained_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_id = "20261109T154005Z-" + "f" * 32
            run_dir = pathlib.Path(temporary) / run_id
            run_dir.mkdir(mode=0o700)
            for name in ("qemu.log", "virtiofsd.log", "proxy.log"):
                (run_dir / name).touch(mode=0o600)
            (run_dir / "oauth-url.txt").write_text("unexpected", encoding="utf-8")
            with mock.patch.object(acceptance, "state_root", return_value=pathlib.Path(temporary)), \
                    mock.patch.object(acceptance, "verify_user_directory_chain"):
                with self.assertRaises(acceptance.GateError):
                    acceptance.inspect_cleanup("a" * 40, run_id)
            self.assertTrue((run_dir / "oauth-url.txt").exists())


if __name__ == "__main__":
    unittest.main()
