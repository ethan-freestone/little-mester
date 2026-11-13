import argparse
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
                with self.assertRaisesRegex(acceptance.GateError, "owner does not match"):
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

                def unsafe_lstat(path):
                    path = pathlib.Path(path)
                    mode = stat.S_IFDIR | 0o755
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
                        mode = stat.S_IFDIR | 0o751
                    return SimpleNamespace(st_mode=mode, st_uid=uid)

                with mock.patch.object(acceptance.os, "lstat", side_effect=unsafe_lstat):
                    expected = {
                        "symlink": "contains a symlink",
                        "non-directory": "not a directory",
                        "group-writable": "group-writable",
                        "world-writable": "world-writable",
                        "wrong-owner": "owner does not match",
                        "no-traverse": "not traversable",
                        "final-mode": "normalized mode",
                    }[reason]
                    with self.assertRaisesRegex(acceptance.GateError, expected):
                        acceptance.check_directory_chain(final, 0, 0o755)

    def test_install_prefix_defaults_and_accepts_only_normalized_absolute_paths(self):
        sha = "a" * 40
        self.assertEqual(acceptance.install_path(sha), acceptance.INSTALL_PARENT / sha)
        self.assertEqual(acceptance.install_path(sha, pathlib.Path("/usr/local/little-mester")),
                         pathlib.Path("/usr/local/little-mester/v3") / sha)
        self.assertEqual(acceptance.install_prefix("/srv/little-mester"),
                         pathlib.Path("/srv/little-mester"))
        for invalid in ("relative/prefix", "/", "/usr/local/../tmp", "/usr/local/./tmp"):
            with self.subTest(prefix=invalid), self.assertRaises(argparse.ArgumentTypeError):
                acceptance.install_prefix(invalid)

    def test_cli_install_uses_requested_prefix(self):
        sha = "a" * 40
        prefix = pathlib.Path("/srv/little-mester")
        with tempfile.TemporaryDirectory() as temporary:
            report_dir = pathlib.Path(temporary) / "attempt"
            report_dir.mkdir(mode=0o700)
            with (
                mock.patch.object(acceptance, "new_report_dir", return_value=report_dir),
                mock.patch.object(acceptance, "candidate_commit"),
                mock.patch.object(acceptance, "install_candidate",
                                  return_value=prefix / "v3" / sha) as install,
                mock.patch.object(acceptance, "installed_preflight"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = acceptance.main([
                    "--install", "--sha", sha, "--prefix", str(prefix), "--repo", str(ROOT)])
            self.assertEqual(result, 0)
            install.assert_called_once_with(ROOT, sha, prefix)

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

    def test_initial_report_write_failure_is_controlled_and_stops_candidate_work(self):
        errors = io.StringIO()
        output = io.StringIO()
        with mock.patch.object(acceptance, "new_report_dir", return_value=pathlib.Path("/trusted/attempt")), \
                mock.patch.object(acceptance, "save_report", side_effect=OSError("disk unavailable")), \
                mock.patch.object(acceptance, "candidate_commit") as candidate, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = acceptance.main(["--preflight", "--sha", "a" * 40, "--repo", str(ROOT)])
        self.assertEqual(result, 1)
        self.assertIn("FAIL: runner initialization failed (OSError)", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())
        self.assertEqual(output.getvalue(), "")
        candidate.assert_not_called()

    def test_final_report_update_failure_does_not_print_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            install = pathlib.Path(temporary) / "install"
            install.mkdir()
            errors = io.StringIO()
            output = io.StringIO()
            with mock.patch.object(acceptance, "new_report_dir", return_value=pathlib.Path("/trusted/attempt")), \
                    mock.patch.object(acceptance, "save_report"), \
                    mock.patch.object(acceptance, "candidate_commit"), \
                    mock.patch.object(acceptance, "install_path", return_value=install), \
                    mock.patch.object(acceptance, "verify_install", return_value=install), \
                    mock.patch.object(acceptance, "installed_preflight") as preflight, \
                    mock.patch.object(acceptance.os, "open", side_effect=OSError("update unavailable")), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                result = acceptance.main(["--preflight", "--sha", "a" * 40, "--repo", str(ROOT)])
        self.assertEqual(result, 1)
        self.assertIn("FAIL: report update failed", errors.getvalue())
        self.assertIn("OVERALL: FAIL", output.getvalue())
        self.assertNotIn("OVERALL: PASS", output.getvalue())
        preflight.assert_called_once_with(install, None)

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
        debug = acceptance.parser().parse_args(["--run", "--sha", "a" * 40, "--debug"])
        self.assertTrue(debug.debug)
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
        self.assertTrue({
            "lib/vm/parent-state.py", "lib/vm/parent-launcher.py",
            "lib/vm/parent-gate.py", "lib/vm/parent-supervisor.py",
            "lib/vm/parent-qemu.py", "lib/vm/parent-provision.py",
            "scripts/parent-readonly-safety-gate.py",
            "agent/opencode/plugins/little-mester-exit-confirm/package.json",
            "agent/opencode/plugins/little-mester-exit-confirm/tui.tsx",
        }.issubset(manifest["files"]))
        self.assertEqual(set(acceptance.archive_arguments(ROOT, head)[6:]), set(manifest["files"]))
        self.assertNotIn(".github", names)
        self.assertIn('json.loads(sys.argv[2])', acceptance.INSTALL_WORKER)
        worker = acceptance.INSTALL_WORKER
        substitutions = (
            ('or os.geteuid() != 0', 'or False'),
            ('fd = os.open("/", flags)', 'fd = os.open(sys.argv[4], flags)'),
            ('if root.st_uid != 0 or root.st_mode & (stat.S_IWGRP | stat.S_IWOTH):', 'if False:'),
            ('or info.st_uid != 0', 'or False'),
            ('os.fchown(child, 0, 0)', 'pass'),
            ('os.fchown(target, 0, 0)', 'pass'),
            ('os.fchown(next_fd, 0, 0)', 'pass'),
            ('os.fchown(stream.fileno(), 0, 0)', 'pass'),
        )
        for original, replacement in substitutions:
            self.assertIn(original, worker)
            worker = worker.replace(original, replacement, 1)
        with tempfile.TemporaryDirectory() as temporary:
            for prefix in ("/prefix-one", "/prefix-two"):
                with self.subTest(prefix=prefix):
                    installed = subprocess.run(
                        [sys.executable, "-I", "-c", worker, head,
                         acceptance.installer_manifest(), prefix, temporary],
                        input=result.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                    )
                    self.assertEqual(installed.returncode, 0,
                                     installed.stderr.decode("utf-8", "replace"))
                    self.assertEqual(installed.stdout, b"PINNED_INSTALL_CREATED\n")
                    target = pathlib.Path(temporary) / prefix.lstrip("/") / "v3" / head
                    observed = {path.relative_to(target).as_posix() for path in target.rglob("*")}
                    self.assertEqual(observed, expected)
                    for name, mode in manifest["files"].items():
                        self.assertEqual(stat.S_IMODE(os.lstat(target / name).st_mode), mode)
                    if prefix == "/prefix-one":
                        helper = target / "scripts/parent-readonly-safety-gate.py"
                        self.assertTrue(helper.is_file())
                        installed_state_spec = importlib.util.spec_from_file_location(
                            "installed_parent_state", target / "lib/vm/parent-state.py")
                        installed_state = importlib.util.module_from_spec(installed_state_spec)
                        installed_state_spec.loader.exec_module(installed_state)
                        observed_paths = []
                        loaded_scanners = []
                        real_spec_from_file = importlib.util.spec_from_file_location

                        class ScannerLoader:
                            def __init__(self, wrapped):
                                self.wrapped = wrapped

                            def create_module(self, module_spec):
                                return self.wrapped.create_module(module_spec)

                            def exec_module(self, module):
                                self.wrapped.exec_module(module)
                                module._check_proc_visibility = mock.Mock()
                                module._check_pid_one_with_lsof = mock.Mock()
                                module._scan_with_lsof = mock.Mock(return_value=[os.getpid()])
                                loaded_scanners.append(module)

                        def installed_helper_spec(name, path):
                            observed_paths.append(pathlib.Path(path))
                            actual = real_spec_from_file(name, path)
                            actual.loader = ScannerLoader(actual.loader)
                            return actual

                        real_lstat = pathlib.Path.lstat

                        def protected_lstat(path):
                            path = pathlib.Path(path)
                            if path == helper:
                                return SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o644)
                            if path == target or target in path.parents:
                                return SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755)
                            return real_lstat(path)

                        with (tempfile.TemporaryFile() as disk,
                              mock.patch.object(pathlib.Path, "lstat", protected_lstat),
                              mock.patch.object(installed_state.importlib.util,
                                                "spec_from_file_location",
                                                side_effect=installed_helper_spec)):
                            identity = os.fstat(disk.fileno())
                            installed_state._scan_stale_disk_holders(
                                disk.fileno(), (identity.st_dev, identity.st_ino))
                        self.assertEqual(observed_paths, [helper])
                        loaded_scanners[0]._check_proc_visibility.assert_called_once()
                        loaded_scanners[0]._check_pid_one_with_lsof.assert_called_once()
                        self.assertEqual(loaded_scanners[0]._scan_with_lsof.call_count, 3)
                        for scan_call in loaded_scanners[0]._scan_with_lsof.call_args_list[-2:]:
                            self.assertEqual(scan_call.args[1:], (identity.st_dev, identity.st_ino))
                        helper.unlink()

                        def missing_helper_lstat(path):
                            path = pathlib.Path(path)
                            if path == helper:
                                return real_lstat(path)
                            if path == target or target in path.parents:
                                return SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755)
                            return real_lstat(path)

                        with mock.patch.object(pathlib.Path, "lstat", missing_helper_lstat):
                            with tempfile.TemporaryFile() as disk:
                                info = os.fstat(disk.fileno())
                                with self.assertRaisesRegex(
                                        installed_state.Refused, "privileged disk-holder verification failed"):
                                    installed_state._scan_stale_disk_holders(
                                        disk.fileno(), (info.st_dev, info.st_ino))

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

    def test_smoke_runs_without_tty_and_inherits_standard_streams(self):
        class NotTTY(io.StringIO):
            def isatty(self):
                return False

        sha = "b" * 40
        root = pathlib.Path("/protected/install")
        data = acceptance.report_template(sha, "run", pathlib.Path("/private/report"))
        state = pathlib.Path("/private/state")
        with (
            mock.patch.object(acceptance.sys, "stdin", NotTTY()),
            mock.patch.object(acceptance.sys, "stdout", NotTTY()),
            mock.patch.object(acceptance, "verify_workspace_mount", return_value=pathlib.Path("/workspace")),
            mock.patch.object(acceptance.pathlib.Path, "iterdir", return_value=iter(())),
            mock.patch.object(acceptance, "state_root", return_value=state),
            mock.patch.object(acceptance, "list_runs", return_value={"20261109T154005Z-" + "a" * 32}),
            mock.patch.object(acceptance, "inspect_cleanup", return_value={"status": "PASS"}),
            mock.patch.object(acceptance.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run,
        ):
            acceptance.run_smoke(root, "/workspace", "openai/model", set(), data, debug=True)
        args, kwargs = run.call_args
        self.assertEqual(args[0], [str(root / "lab-vm"), "smoke", "--debug", "--workspace", "/workspace", "--model", "openai/model"])
        self.assertIsNone(kwargs["stdin"])
        self.assertIsNone(kwargs["stdout"])
        self.assertIsNone(kwargs["stderr"])
        self.assertEqual(data["gates"]["run"], "PASS")
        self.assertEqual(data["gates"]["cleanup"], "PASS")

    def test_clean_env_preserves_only_validated_owner_session_bus(self):
        uid = 12345
        runtime = pathlib.Path(f"/run/user/{uid}")
        bus = runtime / "bus"

        def fake_stat(path, *args, **kwargs):
            if path == runtime:
                return SimpleNamespace(st_uid=uid, st_mode=stat.S_IFDIR | 0o700)
            if path == bus:
                return SimpleNamespace(st_uid=uid, st_mode=stat.S_IFSOCK | 0o600)
            raise FileNotFoundError

        with mock.patch.object(acceptance.os, "getuid", return_value=uid), \
                mock.patch.object(pathlib.Path, "stat", fake_stat):
            env = acceptance.clean_env(desktop_session=True)
            ordinary_env = acceptance.clean_env()
        self.assertEqual(env["XDG_RUNTIME_DIR"], str(runtime))
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], f"unix:path={bus}")
        self.assertNotIn("XDG_RUNTIME_DIR", ordinary_env)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", ordinary_env)
        self.assertNotIn("DISPLAY", env)
        self.assertNotIn("WAYLAND_DISPLAY", env)

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
            (run_dir / "virtiofs.socket-path").write_text("/run/user/1000/lmv3-test/virtiofs.sock\n", encoding="utf-8")
            os.chmod(run_dir / "virtiofs.socket-path", 0o600)
            child = subprocess.Popen(["/usr/bin/sleep", "30"])
            with mock.patch.object(acceptance, "state_root", return_value=pathlib.Path(temporary)), \
                     mock.patch.object(acceptance, "verify_user_directory_chain"), \
                     mock.patch.object(acceptance, "inspect_virtiofs_artifacts", return_value={
                         "socket_present": True, "pid_present": True, "runtime_directory": "present"
                     }):
                result = acceptance.inspect_cleanup("e" * 40, run_id, pathlib.Path("/protected/install"))
            self.assertEqual(result["status"], "PASS")
            self.assertTrue((run_dir / "virtiofs.socket-path").exists())
            self.assertTrue(any("PID artifact" in artifact for artifact in result["residual_artifacts"]))
            self.assertIsNone(child.poll())
            child.terminate()
            child.wait(timeout=5)

    def test_qemu_debug_stop_is_not_an_allowed_retained_acceptance_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_id = "20261109T154005Z-" + "f" * 32
            run_dir = pathlib.Path(temporary) / run_id
            run_dir.mkdir(mode=0o700)
            for name in ("qemu.log", "virtiofsd.log", "proxy.log", "virtiofs.socket-path"):
                (run_dir / name).touch(mode=0o600)
            (run_dir / "qemu.debug-stop").touch(mode=0o600)
            with mock.patch.object(acceptance, "state_root", return_value=pathlib.Path(temporary)), \
                    mock.patch.object(acceptance, "verify_user_directory_chain"):
                with self.assertRaisesRegex(acceptance.GateError, "unexpected retained artifacts"):
                    acceptance.inspect_cleanup("a" * 40, run_id, pathlib.Path("/protected/install"))

    def test_live_socket_listener_blocks_readonly_cleanup_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_id = "20261109T154005Z-" + "e" * 32
            run_dir = pathlib.Path(temporary) / run_id
            run_dir.mkdir(mode=0o700)
            for name in ("qemu.log", "virtiofsd.log", "proxy.log"):
                (run_dir / name).touch(mode=0o600)
            (run_dir / "virtiofs.socket-path").touch(mode=0o600)
            with mock.patch.object(acceptance, "state_root", return_value=pathlib.Path(temporary)), \
                    mock.patch.object(acceptance, "verify_user_directory_chain"), \
                    mock.patch.object(acceptance, "inspect_virtiofs_artifacts", side_effect=acceptance.GateError("BLOCKED", "listener remains")):
                with self.assertRaises(acceptance.GateError):
                    acceptance.inspect_cleanup("f" * 40, run_id, pathlib.Path("/protected/install"))

    def test_early_startup_without_qemu_log_is_not_cleanup_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_id = "20261109T154005Z-" + "1" * 32
            run_dir = pathlib.Path(temporary) / run_id
            run_dir.mkdir(mode=0o700)
            for name in ("virtiofsd.log", "proxy.log", "virtiofs.socket-path"):
                (run_dir / name).touch(mode=0o600)
            with mock.patch.object(acceptance, "state_root", return_value=pathlib.Path(temporary)), \
                    mock.patch.object(acceptance, "verify_user_directory_chain"):
                with self.assertRaisesRegex(acceptance.GateError, "early startup failure"):
                    acceptance.inspect_cleanup("1" * 40, run_id, pathlib.Path("/protected/install"))

    def test_cleanup_report_requires_completed_run_and_cleanup_gates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            report_dir = root / "report"
            report_dir.mkdir(mode=0o700)
            report = report_dir / "report.json"
            data = acceptance.report_template("2" * 40, "run", report_dir)
            data["evidence"]["vm_run_id"] = "20261109T154005Z-" + "2" * 32
            data["gates"]["run"] = "BLOCKED"
            data["gates"]["cleanup"] = "NOT_RUN"
            report.write_text(json.dumps(data), encoding="utf-8")
            os.chmod(report, 0o600)
            with mock.patch.object(acceptance, "runtime_dir", return_value=root):
                with self.assertRaisesRegex(acceptance.GateError, "early startup failure"):
                    acceptance.verify_cleanup_report("2" * 40, data["evidence"]["vm_run_id"])

    def test_successful_verify_cleanup_reports_overall_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            report_dir = root / "report"
            run_id = "20261109T154005Z-" + "3" * 32

            def allocate_report_dir():
                report_dir.mkdir(mode=0o700)
                return report_dir

            with (
                mock.patch.object(acceptance, "new_report_dir", side_effect=allocate_report_dir),
                mock.patch.object(acceptance, "candidate_commit"),
                mock.patch.object(acceptance, "verify_cleanup_report"),
                mock.patch.object(acceptance, "verify_install", return_value=root / "install"),
                mock.patch.object(acceptance, "inspect_cleanup", return_value={"status": "PASS"}),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                result = acceptance.main([
                    "--verify-cleanup", "--sha", "3" * 40,
                    "--repo", str(ROOT), "--run-id", run_id,
                ])
            self.assertEqual(result, 0)
            self.assertIn("OVERALL: PASS", output.getvalue())
            final_report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(final_report["status"], "PASS")
            self.assertEqual(final_report["gates"]["cleanup"], "PASS")

    def test_proc_unix_listener_path_preserves_repeated_spaces(self):
        import socket
        helper_loader = importlib.machinery.SourceFileLoader(
            "virtiofs_socket_helper", str(ROOT / "lib/vm/virtiofs-socket.py")
        )
        helper_spec = importlib.util.spec_from_loader(helper_loader.name, helper_loader)
        helper = importlib.util.module_from_spec(helper_spec)
        helper_loader.exec_module(helper)
        with tempfile.TemporaryDirectory(prefix="vm owner  ") as temporary:
            path = pathlib.Path(temporary) / "virtiofs  socket.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(path))
                self.assertTrue(helper._listener_present(str(path)))
            finally:
                listener.close()

    def test_cleanup_verification_blocks_unexpected_retained_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_id = "20261109T154005Z-" + "f" * 32
            run_dir = pathlib.Path(temporary) / run_id
            run_dir.mkdir(mode=0o700)
            for name in ("qemu.log", "virtiofsd.log", "proxy.log"):
                (run_dir / name).touch(mode=0o600)
            (run_dir / "virtiofs.socket-path").touch(mode=0o600)
            (run_dir / "oauth-url.txt").write_text("unexpected", encoding="utf-8")
            with mock.patch.object(acceptance, "state_root", return_value=pathlib.Path(temporary)), \
                    mock.patch.object(acceptance, "verify_user_directory_chain"):
                with self.assertRaises(acceptance.GateError):
                    acceptance.inspect_cleanup("a" * 40, run_id, pathlib.Path("/protected/install"))
            self.assertTrue((run_dir / "oauth-url.txt").exists())


if __name__ == "__main__":
    unittest.main()
