"""Disposable filesystem tests for the VM policy boundary (no VM operations)."""

import importlib.util
import json
import multiprocessing
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "lib/vm/paths.py"


def load_module():
    spec = importlib.util.spec_from_file_location("vm_paths", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def locked_increment(root, count):
    module = load_module()
    for _ in range(count):
        with module.profile_lock(Path(root), "test"):
            counter = Path(root) / "counter"
            value = int(counter.read_text()) if counter.exists() else 0
            module.atomic_json(counter, value + 1)


class PathsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "state"
        self.root.mkdir(mode=0o700)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.workspace = self.base / "work"
        self.workspace.mkdir()

    def cli(self, command, *args, data=None, ok=True, debug=False):
        env = os.environ.copy()
        env.pop("VM_POLICY_DEBUG", None)
        if debug:
            env["VM_POLICY_DEBUG"] = "1"
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), command, "--state-root", str(self.root), *map(str, args)],
            input=json.dumps(data) if data is not None else None, text=True, capture_output=True,
            check=False, env=env,
        )
        self.assertEqual(result.returncode, 0 if ok else 2, result.stderr)
        return json.loads(result.stdout) if ok else result

    def profile(self):
        return {"schema_version": 1, "profile_id": str(uuid.uuid4()), "name": "test", "workspace": str(self.workspace),
                "image_sha256": None, "image_path": None, "memory_gib": 8, "depth": 0, "max_depth": 0,
                "descendants_possible": False, "opencode_version": None, "guest_payload_sha256": None,
                "runtime_payload_sha256": None, "status": "allocated"}

    def test_config_defaults_readonly(self):
        before = list(self.root.iterdir())
        data = self.cli("config")
        self.assertEqual(data["model"], {"provider": "none"})
        self.assertEqual(data["auth_escrow_dir"], str(self.root / "secrets"))
        self.assertEqual(list(self.root.iterdir()), before)

    def test_context_accepts_private_metrics_namespace_before_profile_creation(self):
        metrics = self.root / "vm/metrics"
        metrics.mkdir(parents=True, mode=0o700)
        metrics.parent.chmod(0o700)
        data = self.cli("context", "--profile", "test", "--repository", self.repo)
        self.assertEqual(data["profile_name"], "test")
        self.assertFalse((self.root / "vm/profiles/test").exists())
        foreign = self.root / "vm/unrecognized"
        foreign.mkdir(mode=0o700)
        self.cli("context", "--profile", "test", "--repository", self.repo, ok=False)

    def test_bounded_log_writer_rotates_only_current_and_one_prior_segment(self):
        paths = load_module()
        log = self.root / "serial.log"
        writer = paths.BoundedLogWriter(log, max_bytes=16)
        writer.write(b"a" * 10)
        writer.write(b"b" * 10)
        writer.write(b"c" * 10)
        writer.close()

        rotated = self.root / "serial.log.1"
        self.assertEqual(rotated.read_bytes() + log.read_bytes(), b"a" * 10 + b"b" * 10 + b"c" * 10)
        self.assertEqual(rotated.stat().st_mode & 0o777, 0o600)
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertLessEqual(rotated.stat().st_size, 16)
        self.assertLessEqual(log.stat().st_size, 16)
        self.assertEqual(set(self.root.glob("serial.log*")), {log, rotated})

    def test_bounded_log_writer_refuses_existing_files_symlinks_and_oversize(self):
        paths = load_module()
        with self.assertRaises(paths.PolicyError):
            paths.BoundedLogWriter(self.root / "serial.log", max_bytes=paths.LOG_SEGMENT_BYTES + 1)

        existing = self.root / "existing.log"
        existing.write_text("preserve")
        existing.chmod(0o600)
        with self.assertRaises(paths.PolicyError):
            paths.BoundedLogWriter(existing)
        self.assertEqual(existing.read_text(), "preserve")

        target = self.root / "target.log"
        target.write_text("target")
        target.chmod(0o600)
        link = self.root / "linked.log"
        link.symlink_to(target)
        with self.assertRaises(paths.PolicyError):
            paths.BoundedLogWriter(link)
        self.assertEqual(target.read_text(), "target")

    def test_validate_config_stdin_defaults_and_no_write(self):
        data = self.cli("validate-config", data={"model": {"provider": "llamacpp", "port": 1234}})
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["model"], {"provider": "llamacpp", "port": 1234})
        self.cli("validate-config", data={"model": {"provider": "ollama"}}, ok=False)
        self.cli("validate-config", data={"model": {"provider": "remote", "port": 1234}}, ok=False)
        self.cli("validate-config", data={"opencode_version": "1.2.3"}, ok=False)
        self.cli("validate-config", data={"model": {"provider": "remote", "id": "x" * 513}}, ok=False)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_absent_config_under_nonprivate_home_readonly(self):
        home = self.base / "home"
        home.mkdir(mode=0o755)
        self.root = home / ".local/share/little-mester"
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            result = self.cli("config")
            self.assertEqual(result["auth_escrow_dir"], str(self.root / "secrets"))
            self.cli("validate-config", data=result)
            self.cli("context", "--profile", "test.v2", "--repository", self.repo)
            self.cli("validate-workspace", "--repository", self.repo, "--workspace", self.workspace)
            self.assertFalse(self.root.exists())
            self.assertEqual(list(home.iterdir()), [])
            self.root = home / "private-state"
            self.root.mkdir(mode=0o700)
            self.cli("config")
            self.cli("validate-config", data={"auth_escrow_dir": str(home / "custom-escrow")}, ok=False)
            self.assertEqual(list(self.root.iterdir()), [])

    def test_recursive_run_roots_are_private_separate_and_idempotent(self):
        helper = load_module()
        first = helper.recursion_run_create(self.root, "child-one")
        second = helper.recursion_run_create(self.root, "child-one")
        run_root = Path(first["run_root"])
        child_state = Path(first["state_root"])
        self.assertEqual(first, second)
        self.assertEqual(run_root.parent.parent, self.root.parent)
        self.assertNotEqual(child_state, self.root)
        self.assertEqual(stat.S_IMODE(run_root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(child_state.stat().st_mode), 0o700)
        self.assertFalse(helper.overlaps(run_root, self.workspace))
        allocation_lock = self.root / ".allocation.lock"
        self.assertTrue(allocation_lock.is_file())
        self.assertEqual(stat.S_IMODE(allocation_lock.stat().st_mode), 0o600)

    def test_recursive_run_cleanup_requires_exact_confirmation_and_stopped_inventory(self):
        helper = load_module()
        created = helper.recursion_run_create(self.root, "child-clean")
        run_root = Path(created["run_root"])
        child_state = Path(created["state_root"])
        proc_root = self.base / "proc-fixture"
        proc_root.mkdir(mode=0o700)

        with self.assertRaises(helper.PolicyError):
            helper.recursion_run_delete(self.root, "child-clean", "other", proc_root)

        qemu = proc_root / "8841"
        qemu.mkdir()
        (qemu / "comm").write_text("qemu-system-x86_64\n", encoding="ascii")
        with self.assertRaises(helper.PolicyError):
            helper.recursion_run_delete(self.root, "child-clean", "child-clean", proc_root)
        self.assertTrue(run_root.is_dir())

        shutil.rmtree(qemu)
        vm_dir = child_state / "vm"
        vm_dir.mkdir(mode=0o700)
        reports = vm_dir / "reports"
        reports.mkdir(mode=0o700)
        report = reports / "result.json"
        report.write_text('{"result":"fixture"}\n', encoding="ascii")
        report.chmod(0o600)
        result = helper.recursion_run_delete(
            self.root, "child-clean", "child-clean", proc_root,
        )
        self.assertFalse(run_root.exists())
        self.assertEqual(result["deleted_run"], "child-clean")
        archive = Path(result["reports_path"])
        self.assertEqual((archive / "result.json").read_text(), '{"result":"fixture"}\n')
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o700)

    def test_recursive_cleanup_requires_descendant_run_first_and_preserves_its_reports(self):
        helper = load_module()
        outer = helper.recursion_run_create(self.root, "outer")
        outer_state = Path(outer["state_root"])
        inner = helper.recursion_run_create(outer_state, "inner")
        inner_run_root = Path(inner["run_root"])
        proc_root = self.base / "empty-proc"
        proc_root.mkdir(mode=0o700)

        with self.assertRaises(helper.PolicyError):
            helper.recursion_run_delete(self.root, "outer", "outer", proc_root)
        self.assertTrue(Path(outer["run_root"]).is_dir())

        vm_dir = Path(inner["state_root"]) / "vm"
        vm_dir.mkdir(mode=0o700)
        reports = vm_dir / "reports"
        reports.mkdir(mode=0o700)
        result = reports / "nested-report.json"
        result.write_text('{"descendant":true}\n', encoding="ascii")
        result.chmod(0o600)

        cleaned_inner = helper.recursion_run_delete(
            outer_state, "inner", "inner", proc_root,
        )
        self.assertFalse(inner_run_root.exists())
        first_archive = Path(cleaned_inner["reports_path"])
        self.assertEqual((first_archive / "nested-report.json").read_text(), '{"descendant":true}\n')

        cleaned_outer = helper.recursion_run_delete(self.root, "outer", "outer", proc_root)
        self.assertFalse(Path(outer["run_root"]).exists())
        self.assertTrue(cleaned_outer["archived_reports"])
        final_archive = Path(cleaned_outer["archived_reports"][0])
        archived_result = next(final_archive.rglob("nested-report.json"))
        self.assertEqual(archived_result.read_text(), '{"descendant":true}\n')

    def test_profile_dotted_names(self):
        dotted = dict(self.profile(), name="test.v2")
        self.cli("profile-write", "--profile", "test.v2", data=dotted)
        self.assertEqual(self.cli("profile-read", "--profile", "test.v2"), dotted)

    def test_profile_create_is_allocated_only_and_never_overwrites(self):
        data = self.profile()
        self.assertEqual(self.cli("profile-create", "--profile", "test", data=data), data)
        self.assertEqual(self.cli("profile-read", "--profile", "test"), data)
        before = (self.root / "vm/profiles/test/profile.json").read_bytes()
        self.cli("profile-create", "--profile", "test", data=data, ok=False)
        self.cli("profile-create", "--profile", "other",
                 data=dict(data, name="other", status="provisioning"), ok=False)
        self.assertEqual((self.root / "vm/profiles/test/profile.json").read_bytes(), before)
        self.assertFalse((self.root / "vm/profiles/other/profile.json").exists())

    def test_profile_list_is_readonly_and_rejects_ambiguous_entries(self):
        self.cli("profile-write", "--profile", "test", data=self.profile())
        before = set(self.root.rglob("*"))
        listed = self.cli("profile-list")
        self.assertEqual([item["name"] for item in listed], ["test"])
        self.assertEqual(set(self.root.rglob("*")), before)
        bad = self.root / "vm/profiles/unknown"
        bad.mkdir(mode=0o700)
        self.cli("profile-list", ok=False)

    def test_profile_memory_bounds(self):
        for memory in (0, 129, 1048576, True):
            self.cli("profile-write", "--profile", "test", data=dict(self.profile(), memory_gib=memory), ok=False)
        self.cli("profile-write", "--profile", "test", data=dict(self.profile(), memory_gib=128))

    def test_profile_version_update_is_exact_and_keeps_ready_state(self):
        image = "a" * 64
        profile = self.profile()
        profile.update({
            "image_sha256": image,
            "image_path": str(self.root / "vm/images" / image / "base.qcow2"),
            "opencode_version": "2.2.5",
            "guest_payload_sha256": "b" * 64,
            "runtime_payload_sha256": "c" * 64,
            "status": "ready",
        })
        self.cli("profile-write", "--profile", "test", data=profile)

        updated = self.cli(
            "profile-version-update", "--profile", "test",
            "--expected-version", "2.2.5", "--version", "2.2.6",
        )
        self.assertEqual(updated["status"], "ready")
        self.assertEqual(updated["opencode_version"], "2.2.6")
        self.assertEqual(updated["guest_payload_sha256"], "b" * 64)

        stale = self.cli(
            "profile-version-update", "--profile", "test",
            "--expected-version", "2.2.5", "--version", "2.2.7", ok=False,
        )
        self.assertEqual(stale.returncode, 2)
        self.assertEqual(self.cli("profile-read", "--profile", "test")["opencode_version"], "2.2.6")

    def test_profile_recursive_allocation_requires_sticky_bit(self):
        recursive = dict(self.profile(), name="recursive", max_depth=1)
        self.cli("profile-write", "--profile", "recursive", data=recursive, ok=False)
        recursive["descendants_possible"] = True
        self.cli("profile-write", "--profile", "recursive", data=recursive)

    def test_profile_provisioning_allows_pending_metadata(self):
        provisioning = dict(self.profile(), name="pending", status="provisioning")
        self.cli("profile-write", "--profile", "pending", data=provisioning)
        self.assertEqual(self.cli("profile-read", "--profile", "pending"), provisioning)
        for status in ("ready", "suspending", "suspended"):
            self.cli("profile-write", "--profile", "pending", data=dict(provisioning, status=status), ok=False)

    def test_profile_state_machine_requires_expected_status_and_verified_transitions(self):
        image_hash = "a" * 64
        image_dir = self.root / "vm/images"
        (self.root / "vm").mkdir(mode=0o700)
        image_dir.mkdir(mode=0o700)
        image_dir.chmod(0o700)
        image_cache = image_dir / image_hash
        image_cache.mkdir(mode=0o700)
        base = image_cache / "base.qcow2"
        base.write_text("base\n", encoding="ascii")
        base.chmod(0o600)
        data = dict(
            self.profile(), image_sha256=image_hash, image_path=str(base),
            opencode_version="2.0.0", guest_payload_sha256="b" * 64,
            runtime_payload_sha256="c" * 64,
        )
        self.cli("profile-write", "--profile", "test", data=data)

        def transition(expected, target, **patch):
            result = subprocess.run(
                [sys.executable, "-B", str(SCRIPT), "profile-transition",
                 "--state-root", str(self.root), "--profile", "test",
                 "--expected-status", expected, "--status", target],
                input=json.dumps(patch), text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        disk = self.root / "vm/profiles/test/disk.qcow2"
        socket = "/tmp/test-vm-profile.sock"
        runtime_identity = {
            "pid": 4321, "start_ticks": 12345, "executable": "/usr/bin/qemu-system-x86_64",
            "disk_path": str(disk), "socket_path": socket, "ssh_port": 40222,
            "argv": ["/usr/bin/qemu-system-x86_64", "-enable-kvm", "-m", "8192",
                     "-drive", f"file={disk},format=qcow2,if=virtio", "-chardev",
                     f"socket,id=charfs,path={socket}", "-netdev",
                     "user,id=net0,hostfwd=tcp:127.0.0.1:40222-:22,restrict=on"],
        }
        starting = transition("allocated", "starting")
        self.assertEqual(starting["status"], "starting")
        provisioning = transition("starting", "provisioning", runtime_identity=runtime_identity)
        self.assertEqual(provisioning["status"], "provisioning")
        ready = transition("provisioning", "ready")
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(transition("ready", "suspending")["status"], "suspending")
        suspended = transition("suspending", "suspended", runtime_identity=None)
        self.assertEqual(suspended["status"], "suspended")
        self.assertNotIn("runtime_identity", suspended)
        before = self.cli("profile-read", "--profile", "test")
        for expected, target in (("ready", "suspending"), ("suspended", "ready"), ("suspended", "provisioning")):
            result = subprocess.run(
                [sys.executable, "-B", str(SCRIPT), "profile-transition",
                 "--state-root", str(self.root), "--profile", "test",
                 "--expected-status", expected, "--status", target],
                input="{}", text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 2)
        self.assertEqual(self.cli("profile-read", "--profile", "test"), before)
        resumed = transition("suspended", "starting", runtime_identity=None)
        self.assertEqual(resumed["status"], "starting")
        self.assertNotIn("runtime_identity", resumed)

    def test_unknown_profile_is_terminal_and_does_not_accept_metadata_repair(self):
        data = self.profile()
        self.cli("profile-write", "--profile", "test", data=data)
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "profile-transition",
             "--state-root", str(self.root), "--profile", "test",
             "--expected-status", "allocated", "--status", "starting"],
            input="{}", text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "profile-transition",
             "--state-root", str(self.root), "--profile", "test",
             "--expected-status", "starting", "--status", "unknown"],
            input="{}", text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        unknown = self.cli("profile-read", "--profile", "test")
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "profile-transition",
             "--state-root", str(self.root), "--profile", "test",
             "--expected-status", "unknown", "--status", "failed"],
            input=json.dumps({"memory_gib": 4}), text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.cli("profile-read", "--profile", "test"), unknown)

    def test_home_local_shell_credentials_and_aliases(self):
        home = self.base / "home"
        home.mkdir(mode=0o755)
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            for name in (".local/bin", ".bashrc", ".zshrc"):
                sensitive = home / name
                sensitive.mkdir(parents=True)
                self.cli("validate-workspace", "--repository", self.repo, "--workspace", sensitive, ok=False)
                alias = self.base / "alias"
                alias.symlink_to(sensitive, target_is_directory=True)
                self.cli("validate-workspace", "--repository", self.repo, "--workspace", alias, ok=False)
                alias.unlink()

    def test_malformed_types_and_unicode_are_sanitized(self):
        for data in ({"host_profile": []}, {"metrics": {}}, {"auth_forward": {}},
                     {"model": {"provider": []}}, {"schema_version": True}):
            result = self.cli("validate-config", data=data, ok=False)
            self.assertNotIn("Traceback", result.stderr)
        for change in ({"profile_id": []}, {"profile_id": "CANARY_NOT_UUID"},
                       {"runtime_identity": []}, {"runtime_identity": {"pid": {}}}, {"status": {}}):
            result = self.cli("profile-write", "--profile", "test", data=dict(self.profile(), **change), ok=False)
            self.assertNotIn("Traceback", result.stderr)
            self.assertNotIn("CANARY_NOT_UUID", result.stderr)
        config = self.root / "config.json"
        config.write_bytes(b'{"agent":"\xffCANARY_SECRET"}')
        config.chmod(0o600)
        result = self.cli("config", ok=False)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("CANARY_SECRET", result.stderr)

    def test_config_strict_and_secret_safe(self):
        config = self.root / "config.json"
        for content in ['{"schema_version":2}', '{"metrics":"off","metrics":"on"}',
                        '{"token":"CANARY_SECRET"}', '{"model":{"provider":"ollama"}}',
                        '{"auth_forward":["openai","openai"]}', '{"agent":true}',
                        '{"opencode_version":"bad\\nversion"}']:
            config.write_text(content)
            config.chmod(0o600)
            result = self.cli("config", ok=False)
            self.assertNotIn("CANARY_SECRET", result.stderr)
        config.write_text('{"schema_version":1,"model":{"provider":"ollama","port":11434}}')
        self.assertEqual(self.cli("config")["model"]["port"], 11434)
        config.chmod(0o644)
        self.cli("config", ok=False)

    def test_private_ancestry_and_descendants(self):
        self.root.chmod(0o755)
        self.cli("config", ok=False)
        self.root.chmod(0o700)
        alias = self.base / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        old = self.root
        self.root = alias
        self.cli("config", ok=False)
        self.root = old
        (self.root / "leak").symlink_to(self.workspace)
        self.cli("config", ok=False)

    def test_private_tree_debug_is_bounded_and_never_prints_contents(self):
        for index in range(30):
            unsafe = self.root / f"unsafe-{index:02d}"
            unsafe.write_text("CANARY_SECRET_CONTENT")
            unsafe.chmod(0o644)

        plain = self.cli("config", ok=False)
        self.assertNotIn("state_root.entry=FAIL", plain.stderr)

        result = self.cli("config", ok=False, debug=True)
        self.assertIn("[debug] state_root.canonical=PASS", result.stderr)
        self.assertIn("[debug] state_root.ancestors=PASS", result.stderr)
        self.assertIn("[debug] state_root.root_metadata=PASS", result.stderr)
        self.assertIn(
            "[debug] state_root.private_tree=FAIL entries_checked=30 violations=30 walk_errors=0",
            result.stderr,
        )
        self.assertIn('path="unsafe-', result.stderr)
        self.assertIn("reason=file_mode uid=", result.stderr)
        self.assertIn("mode=0644 links=1", result.stderr)
        self.assertIn("[debug] state_root.additional_violations_omitted=5", result.stderr)
        self.assertEqual(result.stderr.count("[debug] state_root.entry=FAIL"), 25)
        self.assertNotIn("CANARY_SECRET_CONTENT", result.stderr)

    def test_insecure_ancestor_and_foreign_owner(self):
        self.base.chmod(0o777)
        self.cli("config", ok=False)
        self.base.chmod(0o700)
        module = load_module()
        with (mock.patch.object(module.os, "getuid", return_value=self.root.stat().st_uid + 1),
              self.assertRaises(module.PolicyError)):
            module.state_root(str(self.root))

    def test_absent_context_does_not_allocate(self):
        self.root = self.base / "absent"
        data = self.cli("context", "--profile", "test", "--repository", self.repo)
        self.assertEqual(data["profile_path"], str(self.root / "vm/profiles/test"))
        self.assertFalse(self.root.exists())

    def test_context_no_creation_exact_name_legacy(self):
        self.cli("context", "--profile", "test", "--repository", self.repo)
        self.assertEqual(list(self.root.iterdir()), [])
        for name in ["../test", ".", "a/b", "", "-bad"]:
            self.cli("context", "--profile", name, "--repository", self.repo, ok=False)
        (self.root / "vm").mkdir(mode=0o700)
        (self.root / "vm/base.qcow2").touch(mode=0o600)
        self.cli("context", "--profile", "test", "--repository", self.repo, ok=False)

    def test_workspace_overlap_alias_and_recurse(self):
        def validate(path, *flags, ok=True):
            return self.cli("validate-workspace", "--repository", self.repo, "--workspace", path, *flags, ok=ok)
        self.assertEqual(validate(self.workspace)["workspace"], str(self.workspace))
        for path in ["/", Path.home(), self.root, self.base]:
            validate(path, ok=False)
        alias = self.base / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        validate(alias, ok=False)
        validate(self.repo, ok=False)
        validate(self.repo, "--recurse")
        validate(self.repo, "--unsafe-recurse")
        validate(self.workspace, "--unsafe-recurse", ok=False)
        (self.workspace / ".git").write_text("gitdir: elsewhere")
        validate(self.workspace)
        (self.workspace / ".mester").mkdir()
        validate(self.workspace, ok=False)

    def test_workspace_debug_reports_failed_check_without_paths(self):
        (self.workspace / ".mester").mkdir()
        result = self.cli(
            "validate-workspace", "--repository", self.repo, "--workspace", self.workspace,
            ok=False, debug=True,
        )
        self.assertIn("[debug] workspace.canonical=PASS", result.stderr)
        self.assertIn("[debug] workspace.directory_owner=PASS", result.stderr)
        self.assertIn("[debug] workspace.mester_marker=FAIL", result.stderr)
        self.assertNotIn(str(self.workspace), result.stderr)

    def test_profile_schema_sticky_identity_and_readonly(self):
        self.cli("profile-read", "--profile", "test", ok=False)
        self.assertEqual(list(self.root.iterdir()), [])
        data = self.profile()
        self.cli("profile-write", "--profile", "test", data=data)
        self.assertEqual(self.cli("profile-read", "--profile", "test"), data)
        path = self.root / "vm/profiles/test/profile.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        changed = dict(data, profile_id=str(uuid.uuid4()))
        self.cli("profile-write", "--profile", "test", data=changed, ok=False)
        self.cli("profile-write", "--profile", "test", data=dict(data, memory_gib=4), ok=False)
        data["descendants_possible"] = True
        self.cli("profile-write", "--profile", "test", data=data)
        self.cli("profile-write", "--profile", "test", data=dict(data, descendants_possible=False), ok=False)
        self.cli("profile-write", "--profile", "test", data=dict(data, unknown=1), ok=False)
        self.cli("profile-write", "--profile", "test", data=dict(data, schema_version=2), ok=False)
        self.cli("profile-write", "--profile", "test", data=dict(data, status="ready"), ok=False)
        self.cli("profile-write", "--profile", "test", data=dict(data, workspace=str(self.root)), ok=False)
        self.assertEqual(self.cli("profile-read", "--profile", "test"), data)
        path.chmod(0o644)
        self.cli("profile-read", "--profile", "test", ok=False)

    def test_atomic_failure_preserves_metadata(self):
        module = load_module()
        target = self.root / "metadata.json"
        module.atomic_json(target, {"old": True})
        with mock.patch.object(module.os, "replace", side_effect=OSError("injected")), self.assertRaises(OSError):
            module.atomic_json(target, {"old": False})
        self.assertEqual(json.loads(target.read_text()), {"old": True})
        self.assertEqual(list(self.root.iterdir()), [target])
        with mock.patch.object(module.os, "fsync", side_effect=OSError("injected")), self.assertRaises(OSError):
            module.atomic_json(target, {"old": False})
        self.assertEqual(json.loads(target.read_text()), {"old": True})

    def test_multiprocess_root_profile_locks(self):
        workers = [multiprocessing.Process(target=locked_increment, args=(str(self.root), 15)) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(json.loads((self.root / "counter").read_text()), 60)

    def test_profile_run_holds_ordered_locks_for_nested_state_transition(self):
        paths = load_module()
        self.cli("profile-write", "--profile", "test", data=self.profile())
        other = dict(self.profile(), name="child")
        self.cli("profile-write", "--profile", "child", data=other)
        nested = (
            "import importlib.util,sys; from pathlib import Path; "
            f"spec=importlib.util.spec_from_file_location('nested_paths',{str(SCRIPT)!r}); "
            "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
            "module.profile_transition(Path(sys.argv[1]),sys.argv[2],'allocated','starting',{})"
        )
        command = [sys.executable, "-c", nested, str(self.root), "test"]
        self.assertEqual(paths.profile_run(self.root, "test", command), 0)
        self.assertEqual(self.cli("profile-read", "--profile", "test")["status"], "starting")

        command = [sys.executable, "-c", nested, str(self.root), "child"]
        self.assertEqual(paths.profile_run(self.root, "test", command), 0)
        self.assertEqual(self.cli("profile-read", "--profile", "child")["status"], "starting")

    def test_frozen_runtime_manifest_immutable(self):
        source = self.repo
        (source / "launcher").write_text("trusted")
        (source / "launcher").chmod(0o755)
        args = ("--profile", "test", "--source", source, "--files", "launcher")
        installed = self.cli("runtime-install", *args)
        frozen = Path(installed["runtime_path"]) / "launcher"
        self.assertEqual(frozen.read_text(), "trusted")
        data = dict(self.profile(), runtime_payload_sha256=installed["runtime_payload_sha256"])
        self.cli("profile-write", "--profile", "test", data=data)
        self.assertEqual(
            self.cli("runtime-validate", "--profile", "test"),
            {"runtime_path": str(frozen.parent),
             "runtime_payload_sha256": installed["runtime_payload_sha256"]},
        )
        (source / "launcher").write_text("changed")
        self.cli("runtime-install", *args, ok=False)
        self.assertEqual(frozen.read_text(), "trusted")
        self.assertEqual(self.cli("runtime-validate", "--profile", "test")["runtime_path"], str(frozen.parent))
        (source / "launcher").write_text("trusted")
        self.assertEqual(self.cli("runtime-install", *args), installed)
        self.cli("runtime-install", "--profile", "test", "--source", source,
                 "--files", "../work", ok=False)

    def test_nested_runtime_profile_binding_and_tamper(self):
        (self.repo / "lib/nested").mkdir(parents=True)
        (self.repo / "lib/nested/helper").write_text("frozen")
        args = ("--profile", "test", "--source", self.repo, "--files", "lib/nested/helper")
        result = self.cli("runtime-install", *args)
        data = dict(self.profile(), runtime_payload_sha256=result["runtime_payload_sha256"])
        self.cli("profile-write", "--profile", "test", data=data)
        self.cli("config")  # Nested payload directories must all be private.
        self.assertEqual(self.cli("runtime-validate", "--profile", "test")["runtime_path"], result["runtime_path"])
        self.cli("profile-write", "--profile", "test", data=dict(data, runtime_payload_sha256=None), ok=False)
        installed = Path(result["runtime_path"]) / "lib/nested/helper"
        installed.write_text("tampered")
        self.cli("runtime-install", *args, ok=False)
        self.cli("runtime-validate", "--profile", "test", ok=False)

    def test_runtime_symlink_source_refused_without_allocation(self):
        (self.repo / "link").symlink_to(self.workspace)
        self.cli("runtime-install", "--profile", "test", "--source", self.repo,
                 "--files", "link", ok=False)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_workspace_symlink_loop_is_sanitized(self):
        loop = self.base / "loop"
        loop.symlink_to(loop)
        result = self.cli("validate-workspace", "--repository", self.repo, "--workspace", loop, ok=False)
        self.assertNotIn("Traceback", result.stderr)

    def test_namespace_words_are_valid_exact_profile_names(self):
        self.cli("profile-write", "--profile", "test", data=self.profile())
        for name in ("profiles", "images", "services", "runs", "logs"):
            result = self.cli("context", "--profile", name, "--repository", self.repo)
            self.assertEqual(result["profile_path"], str(self.root / "vm/profiles" / name))

    def test_new_nested_state_ancestors_created_privately_without_chmod(self):
        self.root = self.base / "new-home/data/little-mester"
        home = self.base / "new-home"
        home.mkdir(mode=0o755)
        self.cli("profile-write", "--profile", "test", data=self.profile())
        self.assertEqual(home.stat().st_mode & 0o777, 0o755)
        self.assertEqual((home / "data").stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)

    def test_state_ancestor_must_be_a_directory(self):
        leaf = self.base / "not-a-directory"
        leaf.write_text("unrelated")
        leaf.chmod(0o600)
        self.root = leaf / "state"
        self.cli("config", ok=False)
        self.cli("context", "--profile", "test", "--repository", self.repo, ok=False)
        self.assertEqual(leaf.read_text(), "unrelated")

    def test_image_run_creates_private_hash_lock_and_passes_paths(self):
        digest = "a" * 64
        script = "import os,stat; p=os.environ['VM_IMAGE_IMAGES_DIR']; assert os.environ['VM_IMAGE_SHA256']=='" + digest + "'; assert stat.S_IMODE(os.stat(p).st_mode)==0o700; assert os.environ['VM_IMAGE_CACHE_DIR']==p+'/'+'" + digest + "'"
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "image-run", "--state-root", str(self.root),
             "--sha256", digest, "--", sys.executable, "-c", script],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lock = self.root / "vm/.locks" / f"{digest}.lock"
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    def test_image_publish_is_atomic_and_never_replaces_existing_hash(self):
        digest = "a" * 64
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "image-run", "--state-root", str(self.root),
             "--sha256", digest, "--", sys.executable, "-c", "pass"],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        images = self.root / "vm/images"
        stage = images / f".staging.{digest}.first"
        stage.mkdir(mode=0o700)
        artifact = b"standalone qcow fixture"
        (stage / "base.qcow2").write_bytes(artifact)
        (stage / "base.qcow2").chmod(0o600)
        manifest = {"schema_version": 1, "source_url": "https://example.invalid/image.img",
                    "source_sha256": digest, "artifact_sha256": __import__("hashlib").sha256(artifact).hexdigest(),
                    "virtual_size_bytes": 20 * 1024**3, "format": "qcow2"}
        (stage / "manifest.json").write_text(json.dumps(manifest))
        (stage / "manifest.json").chmod(0o600)
        self.cli("image-publish", "--sha256", digest, "--staging", stage)
        target = images / digest
        old = (target / "base.qcow2").read_bytes()
        second = images / f".staging.{digest}.second"
        second.mkdir(mode=0o700)
        (second / "marker").write_text("must not replace")
        (second / "marker").chmod(0o600)
        self.cli("image-publish", "--sha256", digest, "--staging", second, ok=False)
        self.assertEqual((target / "base.qcow2").read_bytes(), old)
        self.assertTrue(second.exists())

    def test_overlay_publish_is_profile_bound_and_no_replace(self):
        digest = "b" * 64
        data = dict(self.profile(), image_sha256=digest,
                    image_path=str(self.root / "vm/images" / digest / "base.qcow2"))
        self.cli("profile-write", "--profile", "test", data=data)
        profile = self.root / "vm/profiles/test"
        stage_dir = profile / ".overlay.first"
        stage_dir.mkdir(mode=0o700)
        staged = stage_dir / "disk.qcow2"
        staged.write_bytes(b"qcow overlay fixture")
        staged.chmod(0o600)
        self.cli("overlay-publish", "--profile", "test", "--staging", staged)
        target = profile / "disk.qcow2"
        old = target.read_bytes()
        second_dir = profile / ".overlay.second"
        second_dir.mkdir(mode=0o700)
        second = second_dir / "disk.qcow2"
        second.write_bytes(b"replacement forbidden")
        second.chmod(0o600)
        self.cli("overlay-publish", "--profile", "test", "--staging", second, ok=False)
        self.assertEqual(target.read_bytes(), old)
        self.assertTrue(second.exists())

    def test_profile_asset_publish_is_create_only_and_restricted_to_key_or_seed_iso(self):
        self.cli("profile-write", "--profile", "test", data=self.profile())
        self.cli("profile-transition", "--profile", "test", "--expected-status", "allocated",
                 "--status", "starting", data={})
        profile = self.root / "vm/profiles/test"
        stage_dir = profile / ".asset.first"
        stage_dir.mkdir(mode=0o700)
        key = stage_dir / "control-key"
        key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nfixture\n-----END OPENSSH PRIVATE KEY-----\n")
        key.chmod(0o600)
        key_payload = key.read_text()
        result = self.cli("asset-publish", "--profile", "test", "--asset", "control-key", "--staging", key)
        self.assertEqual(result["published"], str(profile / "control-key"))
        self.assertEqual((profile / "control-key").read_text(), key_payload)

        other_dir = profile / ".asset.second"
        other_dir.mkdir(mode=0o700)
        other = other_dir / "control-key"
        other.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nreplacement\n-----END OPENSSH PRIVATE KEY-----\n")
        other.chmod(0o600)
        self.cli("asset-publish", "--profile", "test", "--asset", "control-key", "--staging", other, ok=False)
        self.assertIn("replacement", other.read_text())
        self.assertNotIn("replacement", (profile / "control-key").read_text())

        malformed_dir = profile / ".asset.third"
        malformed_dir.mkdir(mode=0o700)
        malformed = malformed_dir / "seed.iso"
        malformed.write_bytes(b"not an ISO")
        malformed.chmod(0o600)
        self.cli("asset-publish", "--profile", "test", "--asset", "seed.iso", "--staging", malformed, ok=False)
        self.assertFalse((profile / "seed.iso").exists())

    def test_seed_iso_replace_is_atomic_profile_bound_and_starting_only(self):
        self.cli("profile-write", "--profile", "test", data=self.profile())
        self.cli("profile-transition", "--profile", "test", "--expected-status", "allocated",
                 "--status", "starting", data={})
        profile = self.root / "vm/profiles/test"

        def make_iso(stage_dir, name, marker):
            stage_dir.mkdir(mode=0o700)
            stage = stage_dir / name
            payload = bytearray(17 * 2048)
            payload[16 * 2048] = 1
            payload[16 * 2048 + 1:16 * 2048 + 6] = b"CD001"
            payload[16 * 2048 + 6] = 1
            payload[17 * 2048 - 1] = marker
            stage.write_bytes(payload)
            stage.chmod(0o600)
            return stage

        original = make_iso(profile / ".asset.original", "seed.iso", 1)
        self.cli("asset-publish", "--profile", "test", "--asset", "seed.iso", "--staging", original)
        user_data_target = profile / "seed.user-data"
        user_data_target.write_bytes(b"#cloud-config\nold: true\n")
        user_data_target.chmod(0o600)
        before = (profile / "seed.iso").read_bytes()
        replacement = make_iso(profile / ".asset.replacement", "seed.iso", 2)
        replacement_user_data = replacement.parent / "user-data"
        replacement_user_data.write_bytes(b"#cloud-config\nnew: true\n")
        replacement_user_data.chmod(0o600)
        result = self.cli("asset-replace", "--profile", "test", "--asset", "seed.iso",
                          "--staging", replacement, "--user-data-staging", replacement_user_data)
        self.assertEqual(result["published"], str(profile / "seed.iso"))
        self.assertNotEqual((profile / "seed.iso").read_bytes(), before)
        self.assertEqual(user_data_target.read_bytes(), b"#cloud-config\nnew: true\n")
        published = (profile / "seed.iso").read_bytes()
        published_user_data = user_data_target.read_bytes()

        rollback_iso = make_iso(profile / ".asset.rollback", "seed.iso", 5)
        rollback_user_data = rollback_iso.parent / "user-data"
        rollback_user_data.write_bytes(b"#cloud-config\nrollback: true\n")
        rollback_user_data.chmod(0o600)
        paths = load_module()
        real_replace = os.replace
        replace_count = 0

        def fail_iso_replace(source, destination):
            nonlocal replace_count
            replace_count += 1
            if replace_count == 2:
                raise OSError("injected ISO rename failure")
            return real_replace(source, destination)

        with (mock.patch.object(paths.os, "replace", side_effect=fail_iso_replace),
              self.assertRaises(OSError)):
            paths.profile_asset_replace(self.root, "test", "seed.iso", rollback_iso, rollback_user_data)
        self.assertEqual((profile / "seed.iso").read_bytes(), published)
        self.assertEqual(user_data_target.read_bytes(), published_user_data)
        self.assertTrue(rollback_iso.exists())
        self.assertFalse(rollback_user_data.exists())

        invalid = make_iso(profile / ".asset.invalid", "seed.iso", 4)
        invalid_user_data = invalid.parent / "user-data"
        invalid_user_data.write_bytes(b"#cloud-config\napi_token: do-not-store\n")
        invalid_user_data.chmod(0o600)
        self.cli("asset-replace", "--profile", "test", "--asset", "seed.iso",
                 "--staging", invalid, "--user-data-staging", invalid_user_data, ok=False)
        self.assertEqual((profile / "seed.iso").read_bytes(), published)
        self.assertEqual(user_data_target.read_bytes(), published_user_data)

        self.cli("profile-transition", "--profile", "test", "--expected-status", "starting",
                 "--status", "failed", data={})
        rejected = make_iso(profile / ".asset.failed", "seed.iso", 3)
        rejected_user_data = rejected.parent / "user-data"
        rejected_user_data.write_bytes(b"#cloud-config\nother: true\n")
        rejected_user_data.chmod(0o600)
        self.cli("asset-replace", "--profile", "test", "--asset", "seed.iso",
                 "--staging", rejected, "--user-data-staging", rejected_user_data, ok=False)
        self.assertEqual((profile / "seed.iso").read_bytes(), published)
        self.assertEqual(user_data_target.read_bytes(), published_user_data)

    def test_exact_profile_delete_archives_stopped_runs_and_preserves_shared_state(self):
        digest = "d" * 64
        image = self.root / "vm/images" / digest
        image.mkdir(parents=True, mode=0o700)
        (self.root / "vm").chmod(0o700)
        (self.root / "vm/images").chmod(0o700)
        image.chmod(0o700)
        (image / "base.qcow2").write_bytes(b"immutable base")
        (image / "base.qcow2").chmod(0o600)
        data = dict(self.profile(), image_sha256=digest,
                    image_path=str(image / "base.qcow2"), status="allocated")
        self.cli("profile-create", "--profile", "test", data=data)
        other = dict(self.profile(), name="other")
        self.cli("profile-create", "--profile", "other", data=other)
        profile = self.root / "vm/profiles/test"
        runs = profile / "runs"
        run = runs / "run-one"
        run.mkdir(parents=True, mode=0o700)
        runs.chmod(0o700)
        run.chmod(0o700)
        spec = importlib.util.spec_from_file_location("vm_paths_process_empty", SCRIPT.with_name("process.py"))
        process = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(process)
        process.registry_initialize(run / "processes.json", "run-one")
        log = run / "qemu.log"
        log.write_text("bounded fixture diagnostics\n")
        log.chmod(0o600)
        self.cli("profile-transition", "--profile", "test", "--expected-status", "allocated",
                 "--status", "deleting", data={})

        result = self.cli("profile-delete", "--profile", "test")
        self.assertEqual(result["deleted"], "test")
        self.assertFalse(profile.exists())
        reports = Path(result["reports_path"])
        self.assertEqual((reports / "runs/run-one/qemu.log").read_text(), "bounded fixture diagnostics\n")
        self.assertTrue((self.root / "vm/profiles/other/profile.json").is_file())
        self.assertEqual((image / "base.qcow2").read_bytes(), b"immutable base")
        self.assertTrue(self.workspace.is_dir())

    def test_exact_profile_delete_refuses_running_or_unknown_run_identity(self):
        data = self.profile()
        self.cli("profile-create", "--profile", "test", data=data)
        profile = self.root / "vm/profiles/test"
        run = profile / "runs/run-one"
        run.mkdir(parents=True, mode=0o700)
        (profile / "runs").chmod(0o700)
        run.chmod(0o700)
        helper_path = SCRIPT.with_name("process.py")
        spec = importlib.util.spec_from_file_location("vm_paths_process_fixture", helper_path)
        process = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(process)
        identity = process.capture_identity(os.getpid())
        process.registry_initialize(run / "processes.json", "run-one")
        process.registry_add(run / "processes.json", "run-one", "qemu_1", identity)
        self.cli("profile-transition", "--profile", "test", "--expected-status", "allocated",
                 "--status", "deleting", data={})
        self.cli("profile-delete", "--profile", "test", ok=False)
        self.assertTrue((profile / "profile.json").is_file())
        self.assertTrue((run / "processes.json").is_file())

    def test_seed_write_secret_refusal_and_atomic_failure_preserve_previous(self):
        self.cli("profile-write", "--profile", "test", data=self.profile())
        safe = b"#cloud-config\npackages:\n - qemu-guest-agent\n"
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "seed-write", "--state-root", str(self.root), "--profile", "test"],
            input=safe, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.root / "vm/profiles/test/seed.user-data"
        self.assertEqual(target.read_bytes(), safe)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        public_key_seed = b"#cloud-config\nssh_authorized_keys:\n - ssh-ed25519 AAAATEST public-only\n"
        public = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "seed-write", "--state-root", str(self.root), "--profile", "test"],
            input=public_key_seed, capture_output=True, check=False,
        )
        self.assertEqual(public.returncode, 0, public.stderr)
        self.assertEqual(target.read_bytes(), public_key_seed)
        before = target.read_bytes()
        bad = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "seed-write", "--state-root", str(self.root), "--profile", "test"],
            input=b"#cloud-config\napi_token: SECRET_CANARY\n", capture_output=True, check=False,
        )
        self.assertEqual(bad.returncode, 2)
        self.assertNotIn(b"SECRET_CANARY", bad.stderr)
        self.assertEqual(target.read_bytes(), before)

    @unittest.skipUnless(shutil.which("genisoimage") and shutil.which("ssh-keygen"),
                         "local ISO and key tools required; no VM/network operations")
    def test_real_seed_preparation_reaches_key_and_iso_publication(self):
        data = self.profile()
        data["status"] = "starting"
        self.cli("profile-write", "--profile", "test", data=data)
        result = subprocess.run(
            ["bash", "-c", '''
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
source "$1/lib/vm/lifecycle.sh"
source "$1/lib/vm/disk.sh"
vm_disk_create_overlay() {
  printf fixture-disk > "$1/vm/profiles/$2/disk.qcow2"
  chmod 600 "$1/vm/profiles/$2/disk.qcow2"
}
vm_guest_payload_hash() { printf '%064d\\n' 0; }
payload="$(vm_guest_payload_hash)"
vm_lifecycle_prepare_profile_assets_locked "$2" test "$3" 0 0 docker opencode latest "$payload" 1000 1000
''', "_", str(SCRIPT.parents[2]), str(self.root), str(self.workspace)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        profile = self.root / "vm/profiles/test"
        for name in ("disk.qcow2", "seed.user-data", "control-key", "seed.iso"):
            self.assertTrue((profile / name).is_file(), name)
            self.assertEqual((profile / name).stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(profile.glob(".asset.*")))
        self.assertFalse((profile / "runs").exists())

    def test_seed_atomic_write_failure_preserves_existing_file(self):
        module = load_module()
        self.cli("profile-write", "--profile", "test", data=self.profile())
        target = self.root / "vm/profiles/test/seed.user-data"
        target.write_bytes(b"#cloud-config\nold: value\n")
        target.chmod(0o600)
        with (mock.patch.object(module.os, "replace", side_effect=OSError("fixture failure")),
              self.assertRaises(OSError)):
            module.seed_write(self.root, "test", b"#cloud-config\nnew: value\n")
        self.assertEqual(target.read_bytes(), b"#cloud-config\nold: value\n")
        self.assertEqual(list(target.parent.glob(".data-*")), [])


if __name__ == "__main__":
    unittest.main()
