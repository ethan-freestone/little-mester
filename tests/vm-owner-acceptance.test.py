import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import stat
import subprocess
import sys
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
    @staticmethod
    def runtime_fixture(temporary, *, system_mode=0o755, system_uid=0):
        root = pathlib.Path(temporary)
        system_parent = root / "run" / "user"
        system_parent.mkdir(parents=True, mode=0o755)
        user_root = system_parent / str(os.getuid())
        user_root.mkdir(mode=0o700)
        os.chmod(user_root, 0o700)
        for directory in (root, root / "run", system_parent):
            os.chmod(directory, system_mode)
        runtime = user_root / "little-mester-vm-acceptance"

        real_lstat = os.lstat

        def fixture_lstat(path):
            path = pathlib.Path(path)
            info = real_lstat(path)
            if path == user_root or user_root in path.parents:
                return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid)
            return SimpleNamespace(
                st_mode=(info.st_mode & ~0o222) | stat.S_IXOTH,
                st_uid=system_uid,
            )

        return system_parent, user_root, runtime, fixture_lstat

    def test_directory_chain_accepts_owner_private_final_without_other_execute(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, user_root, _, fixture_lstat = self.runtime_fixture(temporary)
            with mock.patch.object(acceptance.os, "lstat", side_effect=fixture_lstat):
                acceptance.check_directory_chain(user_root, os.getuid(), 0o700)

    def test_previous_nested_validation_rejects_owner_owned_parent_as_root_ancestor(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, user_root, runtime, fixture_lstat = self.runtime_fixture(temporary)
            runtime.mkdir(mode=0o700)
            with mock.patch.object(acceptance.os, "lstat", side_effect=fixture_lstat):
                with self.assertRaisesRegex(acceptance.GateError, "ownership, type, or mode"):
                    acceptance.check_directory_chain(runtime, os.getuid(), 0o700)

    def test_protected_install_chain_accepts_root_owned_0755_final_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            final = root / "opt" / "little-mester" / "v3" / ("a" * 40)
            final.mkdir(parents=True, mode=0o755)
            for path in (root, root / "opt", root / "opt/little-mester", root / "opt/little-mester/v3", final):
                os.chmod(path, 0o755)
            real_lstat = os.lstat

            def root_lstat(path):
                info = real_lstat(path)
                mode = info.st_mode if pathlib.Path(path) == final else (info.st_mode & ~0o222) | stat.S_IXOTH
                return SimpleNamespace(
                    st_mode=mode,
                    st_uid=0,
                )

            with mock.patch.object(acceptance.os, "lstat", side_effect=root_lstat):
                acceptance.check_directory_chain(final, 0, 0o755)

    def test_protected_install_chain_rejects_unsafe_ancestry_and_final_mode(self):
        for reason in ("symlink", "non-directory", "group-writable", "world-writable", "wrong-owner", "no-traverse", "final-mode"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                root = pathlib.Path(temporary)
                final = root / "opt" / "little-mester" / "v3" / ("b" * 40)
                final.mkdir(parents=True, mode=0o755)
                paths = (root, root / "opt", root / "opt/little-mester", root / "opt/little-mester/v3", final)
                for path in paths:
                    os.chmod(path, 0o755)
                bad = paths[2]
                real_lstat = os.lstat

                def unsafe_lstat(path):
                    path = pathlib.Path(path)
                    info = real_lstat(path)
                    mode = info.st_mode
                    uid = 0
                    if reason == "symlink" and path == bad:
                        mode = stat.S_IFLNK | 0o777
                    elif reason == "non-directory" and path == bad:
                        mode = stat.S_IFREG | 0o644
                    elif reason == "group-writable" and path == bad:
                        mode |= stat.S_IWGRP
                    elif reason == "world-writable" and path == bad:
                        mode |= stat.S_IWOTH
                    elif reason == "wrong-owner" and path == bad:
                        uid = os.getuid() + 1
                    elif reason == "no-traverse" and path == bad:
                        mode &= ~stat.S_IXOTH
                    elif reason == "final-mode" and path == final:
                        mode = (mode & ~stat.S_IMODE(mode)) | stat.S_IFDIR | 0o750
                    return SimpleNamespace(st_mode=mode, st_uid=uid)

                with mock.patch.object(acceptance.os, "lstat", side_effect=unsafe_lstat):
                    with self.assertRaises(acceptance.GateError):
                        acceptance.check_directory_chain(final, 0, 0o755)

    def test_runtime_validator_accepts_protected_prefix_and_private_descendants(self):
        with tempfile.TemporaryDirectory() as temporary:
            system_parent, user_root, runtime, fixture_lstat = self.runtime_fixture(temporary)
            runtime.mkdir(mode=0o700)
            report_parent = runtime / "attempt-parent"
            report_parent.mkdir(mode=0o700)
            report_dir = report_parent / "attempt"
            report_dir.mkdir(mode=0o700)
            with mock.patch.object(acceptance, "RUNTIME_ROOT", runtime), \
                    mock.patch.object(acceptance.os, "lstat", side_effect=fixture_lstat):
                acceptance.check_private_runtime_chain(user_root, system_parent)
                acceptance.check_private_runtime_chain(runtime, system_parent)
                acceptance.check_private_runtime_chain(report_parent, system_parent)
                acceptance.check_private_runtime_chain(report_dir, system_parent)

    def test_runtime_validator_rejects_symlink_and_non_directory_system_components(self):
        for make_unsafe in ("symlink", "non-directory"):
            with self.subTest(make_unsafe=make_unsafe), tempfile.TemporaryDirectory() as temporary:
                system_parent, user_root, runtime, fixture_lstat = self.runtime_fixture(temporary)
                displaced = system_parent.with_name("user-original")
                system_parent.rename(displaced)
                if make_unsafe == "symlink":
                    system_parent.symlink_to(displaced)
                else:
                    system_parent.write_text("not a directory", encoding="ascii")
                with mock.patch.object(acceptance.os, "lstat", side_effect=fixture_lstat):
                    with self.assertRaises(acceptance.GateError):
                        acceptance.check_private_runtime_chain(user_root, system_parent)

    def test_runtime_validator_rejects_unsafe_system_ownership_and_permissions(self):
        for reason in ("group-writable", "world-writable", "wrong-owner", "no-traverse"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                system_parent, user_root, runtime, fixture_lstat = self.runtime_fixture(temporary)

                def unsafe_lstat(path):
                    info = fixture_lstat(path)
                    path = pathlib.Path(path)
                    if path == system_parent:
                        mode = info.st_mode
                        uid = info.st_uid
                        if reason == "group-writable":
                            mode |= stat.S_IWGRP
                        elif reason == "world-writable":
                            mode |= stat.S_IWOTH
                        elif reason == "wrong-owner":
                            uid = os.getuid()
                        else:
                            mode &= ~stat.S_IXOTH
                        return SimpleNamespace(st_mode=mode, st_uid=uid)
                    return info

                with mock.patch.object(acceptance.os, "lstat", side_effect=unsafe_lstat):
                    with self.assertRaises(acceptance.GateError):
                        acceptance.check_private_runtime_chain(user_root, system_parent)

    def test_runtime_validator_rejects_private_owner_mode_traversal_and_transition(self):
        for reason in (
            "wrong-owner", "group-writable", "world-writable", "wrong-mode", "no-traverse",
            "transition", "symlink", "child-group-writable", "child-world-writable",
            "child-wrong-mode", "child-no-traverse",
        ):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                system_parent, user_root, runtime, fixture_lstat = self.runtime_fixture(temporary)
                runtime.mkdir(mode=0o700)
                child = runtime / "child"
                child.mkdir(mode=0o700)
                if reason == "group-writable":
                    os.chmod(runtime, 0o720)
                elif reason == "world-writable":
                    os.chmod(runtime, 0o702)
                elif reason == "wrong-mode":
                    os.chmod(runtime, 0o750)
                elif reason == "no-traverse":
                    os.chmod(runtime, 0o600)
                elif reason == "symlink":
                    child.rmdir()
                    child.symlink_to(user_root)
                elif reason == "child-group-writable":
                    os.chmod(child, 0o720)
                elif reason == "child-world-writable":
                    os.chmod(child, 0o702)
                elif reason == "child-wrong-mode":
                    os.chmod(child, 0o750)
                elif reason == "child-no-traverse":
                    os.chmod(child, 0o600)

                def unsafe_lstat(path):
                    info = fixture_lstat(path)
                    path = pathlib.Path(path)
                    if reason == "wrong-owner" and path == user_root:
                        return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)
                    if reason == "transition" and path == child:
                        return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)
                    return info

                with mock.patch.object(acceptance.os, "lstat", side_effect=unsafe_lstat):
                    with self.assertRaises(acceptance.GateError):
                        acceptance.check_private_runtime_chain(child, system_parent)

    def test_report_creation_and_preflight_use_validated_private_runtime_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            system_parent, _, runtime, fixture_lstat = self.runtime_fixture(temporary)
            install = pathlib.Path(temporary) / "protected-install"
            install.mkdir(mode=0o755)
            with mock.patch.object(acceptance, "RUNTIME_ROOT", runtime), \
                    mock.patch.object(acceptance.os, "lstat", side_effect=fixture_lstat), \
                    mock.patch.object(acceptance, "candidate_commit"), \
                    mock.patch.object(acceptance, "install_path", return_value=install), \
                    mock.patch.object(acceptance, "verify_install", return_value=install), \
                    mock.patch.object(acceptance, "installed_preflight"), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = acceptance.main(["--preflight", "--sha", "a" * 40, "--repo", str(ROOT)])
            self.assertEqual(result, 0)
            attempts = list(runtime.iterdir())
            self.assertEqual(len(attempts), 1)
            report = attempts[0] / "report.json"
            self.assertEqual(stat.S_IMODE(os.lstat(report).st_mode), 0o600)
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "PASS")
            self.assertEqual(payload["gates"]["preflight"], "PASS")

    def test_early_runtime_gate_error_is_reported_without_traceback_or_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            system_parent, _, runtime, fixture_lstat = self.runtime_fixture(temporary)
            errors = io.StringIO()
            output = io.StringIO()

            def unsafe_system_lstat(path):
                info = fixture_lstat(path)
                if pathlib.Path(path) == system_parent:
                    return SimpleNamespace(st_mode=info.st_mode | stat.S_IWGRP, st_uid=info.st_uid)
                return info

            with mock.patch.object(acceptance, "RUNTIME_ROOT", runtime), \
                mock.patch.object(acceptance.os, "lstat", side_effect=unsafe_system_lstat), \
                mock.patch.object(acceptance, "candidate_commit") as candidate, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                result = acceptance.main(["--preflight", "--sha", "a" * 40, "--repo", str(ROOT)])
            self.assertEqual(result, 3)
            self.assertIn("BLOCKED: protected system directory", errors.getvalue())
            self.assertNotIn("Traceback", errors.getvalue())
            self.assertEqual(output.getvalue(), "")
            self.assertFalse(runtime.exists())
            candidate.assert_not_called()

    def test_report_directory_allocation_refusal_is_a_gate_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = pathlib.Path(temporary)
            os.chmod(parent, 0o700)
            with mock.patch.object(acceptance.os, "mkdir", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(acceptance.GateError, "trusted report directory"):
                    acceptance.new_report_dir(parent)

    def test_unsafe_report_parent_is_refused_by_production_private_directory_check(self):
        for reason in ("symlink", "wrong-mode", "wrong-owner"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                root = pathlib.Path(temporary)
                parent = root / "reports"
                if reason == "symlink":
                    parent.symlink_to(root)
                else:
                    parent.mkdir(mode=0o700)
                    if reason == "wrong-mode":
                        os.chmod(parent, 0o750)
                real_lstat = os.lstat

                def wrong_owner_lstat(path):
                    info = real_lstat(path)
                    if pathlib.Path(path) == parent and reason == "wrong-owner":
                        return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1)
                    return info

                with mock.patch.object(acceptance.os, "lstat", side_effect=wrong_owner_lstat):
                    with self.assertRaises(acceptance.GateError):
                        acceptance.new_report_dir(parent)

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

    def test_candidate_commit_ignores_repository_replace_refs(self):
        with tempfile.TemporaryDirectory(prefix="owner-acceptance-replace-ref-") as temporary:
            repo = pathlib.Path(temporary)
            env = {**os.environ, **acceptance.clean_env()}

            def git(*args):
                return subprocess.run(
                    [acceptance.GIT, "-C", str(repo), *args], env=env,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, check=True,
                ).stdout.decode("ascii").strip()

            git("init", "-q")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            for name in acceptance.INSTALL_FILES:
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(ENTRY.read_bytes() if name == "tools/vm-owner-acceptance" else f"pinned:{name}\n".encode())
            runner_path = repo / "tools/vm-owner-acceptance"
            runner_path.parent.mkdir(parents=True, exist_ok=True)
            runner_path.write_bytes(ENTRY.read_bytes())
            git("add", "--all")
            git("commit", "-qm", "pinned candidate")
            pinned_sha = git("rev-parse", "HEAD")
            pinned_launcher = (repo / "lab-vm").read_bytes()

            (repo / "tools/vm-owner-acceptance").write_bytes(b"untrusted runner bytes\n")
            (repo / "lab-vm").write_bytes(b"untrusted launcher bytes\n")
            git("commit", "-qam", "replacement tree")
            replacement_sha = git("rev-parse", "HEAD")
            git("replace", pinned_sha, replacement_sha)

            # Model an entrypoint loaded from the replacement tree. A replace
            # ref must not make its bytes appear to belong to the pinned SHA.
            with mock.patch.object(acceptance, "protected_executable", side_effect=lambda _repo, path: path), \
                    mock.patch.object(acceptance, "ENTRY", repo / "tools/vm-owner-acceptance"):
                with self.assertRaises(acceptance.GateError):
                    acceptance.candidate_commit(repo, pinned_sha)

            archive = subprocess.run(
                acceptance.archive_arguments(repo, pinned_sha), env={
                    **acceptance.clean_env(),
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                }, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=True,
            ).stdout
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as pinned_archive:
                self.assertEqual(pinned_archive.extractfile("lab-vm").read(), pinned_launcher)

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
        worker = acceptance.INSTALL_WORKER
        substitutions = (
            ('or os.geteuid() != 0', 'or False'),
            ('parts = ("opt", "little-mester", "v3")\nflags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC\nfd = os.open("/", flags)',
             'parts = ()\nflags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC\nfd = os.open(sys.argv[3], flags)'),
            ('if root.st_uid != 0 or root.st_mode & (stat.S_IWGRP | stat.S_IWOTH):', 'if False:'),
            ('os.fchown(target, 0, 0)', 'pass'),
            ('os.fchown(next_fd, 0, 0)', 'pass'),
            ('os.fchown(stream.fileno(), 0, 0)', 'pass'),
        )
        for original, replacement in substitutions:
            self.assertIn(original, worker)
            worker = worker.replace(original, replacement, 1)
        with tempfile.TemporaryDirectory() as temporary:
            installed = subprocess.run(
                [sys.executable, "-I", "-c", worker, head, acceptance.installer_manifest(), temporary],
                input=result.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr.decode("utf-8", "replace"))
            self.assertEqual(installed.stdout, b"PINNED_INSTALL_CREATED\n")
            target = pathlib.Path(temporary) / head
            observed = {path.relative_to(target).as_posix() for path in target.rglob("*")}
            self.assertEqual(observed, expected)
            for name, mode in manifest["files"].items():
                self.assertEqual(stat.S_IMODE(os.lstat(target / name).st_mode), mode)

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
