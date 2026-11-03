"""Disposable filesystem tests for the VM policy boundary (no VM operations)."""

import importlib.util
import json
import multiprocessing
import os
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

    def cli(self, command, *args, data=None, ok=True):
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), command, "--state-root", str(self.root), *map(str, args)],
            input=json.dumps(data) if data is not None else None, text=True, capture_output=True, check=False,
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

    def test_validate_config_stdin_defaults_and_no_write(self):
        data = self.cli("validate-config", data={"model": {"provider": "llamacpp", "port": 1234}})
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["model"], {"provider": "llamacpp", "port": 1234})
        self.cli("validate-config", data={"model": {"provider": "ollama"}}, ok=False)
        self.cli("validate-config", data={"model": {"provider": "remote", "port": 1234}}, ok=False)
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

    def test_profile_dotted_names(self):
        dotted = dict(self.profile(), name="test.v2")
        self.cli("profile-write", "--profile", "test.v2", data=dotted)
        self.assertEqual(self.cli("profile-read", "--profile", "test.v2"), dotted)

    def test_profile_memory_bounds(self):
        for memory in (0, 129, 1048576, True):
            self.cli("profile-write", "--profile", "test", data=dict(self.profile(), memory_gib=memory), ok=False)
        self.cli("profile-write", "--profile", "test", data=dict(self.profile(), memory_gib=128))

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

    def test_frozen_runtime_manifest_immutable(self):
        source = self.repo
        (source / "launcher").write_text("trusted")
        (source / "launcher").chmod(0o755)
        args = ("--profile", "test", "--source", source, "--files", "launcher")
        installed = self.cli("runtime-install", *args)
        frozen = Path(installed["runtime_path"]) / "launcher"
        self.assertEqual(frozen.read_text(), "trusted")
        (source / "launcher").write_text("changed")
        self.cli("runtime-install", *args, ok=False)
        self.assertEqual(frozen.read_text(), "trusted")
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
        self.cli("profile-write", "--profile", "test", data=dict(data, runtime_payload_sha256=None), ok=False)
        installed = Path(result["runtime_path"]) / "lib/nested/helper"
        installed.write_text("tampered")
        self.cli("runtime-install", *args, ok=False)

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
