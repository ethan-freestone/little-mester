"""Protocol and immutable-runtime tests for the named-profile control core."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE_PATH = ROOT / "lib/vm/profile-control-core.py"
SPEC = importlib.util.spec_from_file_location("vm_profile_control_core", CORE_PATH)
core = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = core
SPEC.loader.exec_module(core)


SHA_A = "a" * 40
SHA_B = "b" * 40
RUNTIME_FILES = {
    "lib/vm/profile-runtime.py": ("runtime entrypoint\n", "100644"),
    "profile-runtime.json": ("{\"schema_version\":1}\n", "100644"),
}


def _manifest(sha: str, files: dict[str, tuple[bytes, str]]) -> dict:
    inventory = [
        {"path": path, "git_mode": mode, "sha256": hashlib.sha256(content).hexdigest()}
        for path, (content, mode) in sorted(files.items())
    ]
    return {
        "schema_version": 1,
        "commit_sha": sha,
        "profile_runtime_api": 1,
        "control_helper_api_min": 1,
        "control_helper_api_max": 1,
        "files": inventory,
    }


class RuntimeFixture:
    def __init__(self, root: pathlib.Path, sha: str, files=None):
        self.root = root
        self.sha = sha
        self.store = root / "runtime-store"
        self.directory = self.store / sha
        self.files = dict(RUNTIME_FILES if files is None else files)
        self.store.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.store, 0o755)
        for relative, (content, _mode) in self.files.items():
            path = self.directory / relative
            path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            path.write_bytes(content.encode() if isinstance(content, str) else content)
        payload = json.dumps(_manifest(sha, {
            path: ((content.encode() if isinstance(content, str) else content), mode)
            for path, (content, mode) in self.files.items()
        }), sort_keys=True, separators=(",", ":")).encode()
        manifest_path = self.directory / core.MANIFEST_FILENAME
        manifest_path.write_bytes(payload)
        os.chmod(manifest_path, 0o444)
        for relative, (_content, mode) in self.files.items():
            os.chmod(self.directory / relative, 0o444 if mode == "100644" else 0o555)
        for directory in sorted((item for item in self.directory.rglob("*") if item.is_dir()), reverse=True):
            os.chmod(directory, 0o555)
        os.chmod(self.directory, 0o555)
        os.chmod(self.store, 0o555)

    def make_directories_writable(self):
        for directory in [self.store, *[p for p in self.directory.rglob("*") if p.is_dir()], self.directory]:
            if directory.exists():
                os.chmod(directory, 0o755)

    def rewrite_manifest(self, manifest: dict):
        path = self.directory / core.MANIFEST_FILENAME
        os.chmod(path, 0o644)
        path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
        os.chmod(path, 0o444)


class ProfileControlProtocolTests(unittest.TestCase):
    def test_accepts_exact_status_and_install_schemas(self):
        self.assertEqual(
            core.parse_request(b'{"protocol":1,"operation":"status","profile":"default"}'),
            {"protocol": 1, "operation": "status", "profile": "default"},
        )
        request = {
            "protocol": 1,
            "operation": "install",
            "profile": "secondary-1",
            "repository": "/workspace/project",
            "commit_sha": SHA_A,
        }
        self.assertEqual(core.validate_request(request), request)

    def test_accepts_only_documented_typed_optional_fields(self):
        self.assertEqual(core.validate_request({
            "protocol": 1, "operation": "up", "profile": "default",
            "workspace": "/workspace/project", "unsafe_recurse": True, "reassign": False,
        })["workspace"], "/workspace/project")
        self.assertEqual(core.validate_request({
            "protocol": 1, "operation": "settings.set", "profile": "default",
            "model": "openai/example", "theme": "dark",
        })["theme"], "dark")
        self.assertEqual(core.validate_request({
            "protocol": 1, "operation": "prune", "dry_run": True,
        })["dry_run"], True)

    def test_accepts_the_profile_and_inventory_read_operations(self):
        requests = [
            {"protocol": 1, "operation": operation, "profile": "default"}
            for operation in ("status", "down", "ssh", "auth", "tui", "verify", "accept",
                              "settings.show", "profiles.show", "reset", "remove")
        ]
        requests.extend({"protocol": 1, "operation": operation}
                        for operation in ("profiles.list", "installations"))
        for request in requests:
            with self.subTest(operation=request["operation"]):
                self.assertEqual(core.validate_request(request), request)

    def test_rejects_non_object_bad_version_and_boolean_protocol(self):
        for request in ([], {"protocol": 2, "operation": "status", "profile": "default"},
                        {"protocol": True, "operation": "status", "profile": "default"}):
            with self.subTest(request=request), self.assertRaises(core.Refused):
                core.validate_request(request)

    def test_rejects_unknown_and_missing_request_fields(self):
        bad = (
            {"protocol": 1, "operation": "status", "profile": "default", "argv": ["sh"]},
            {"protocol": 1, "operation": "remove"},
            {"protocol": 1, "operation": "settings.set", "profile": "default"},
            {"protocol": 1, "operation": "prune", "dry_run": 1},
            {"protocol": 1, "operation": "up", "profile": "default", "workspace": "relative"},
        )
        for request in bad:
            with self.subTest(request=request), self.assertRaises(core.Refused):
                core.validate_request(request)

    def test_rejects_reserved_invalid_and_non_ascii_profile_names(self):
        for name in ("system", "profiles", "Upper", "1bad", "../escape", "bad_name", "équipe"):
            with self.subTest(name=name), self.assertRaises(core.Refused):
                core.validate_profile_name(name)

    def test_rejects_relative_install_path_and_malformed_sha(self):
        for repository, sha in (("project", SHA_A), ("/workspace/project", "a" * 39),
                                ("/workspace/project", "A" * 40)):
            request = {"protocol": 1, "operation": "install", "profile": "default",
                       "repository": repository, "commit_sha": sha}
            with self.subTest(repository=repository, sha=sha), self.assertRaises(core.Refused):
                core.validate_request(request)

    def test_parse_rejects_duplicate_keys_invalid_utf8_trailing_data_and_oversize(self):
        bad_inputs = (
            b'{"protocol":1,"operation":"status","profile":"default","profile":"other"}',
            b"\xff",
            b'{"protocol":1,"operation":"status","profile":"default"} false',
            b" " * (core.MAX_REQUEST_BYTES + 1),
        )
        for raw in bad_inputs:
            with self.subTest(raw=raw[:40]), self.assertRaises(core.Refused):
                core.parse_request(raw)

    def test_parse_rejects_nonstandard_json_numbers_and_unsupported_operations(self):
        with self.assertRaises(core.Refused):
            core.parse_request(b'{"protocol":1,"operation":"status","profile":"default","x":NaN}')
        with self.assertRaises(core.Refused):
            core.validate_request({"protocol": 1, "operation": "control.update"})

    def test_read_request_bounds_streamed_input(self):
        request = b'{"protocol":1,"operation":"status","profile":"default"}'
        self.assertEqual(core.read_request(io.BytesIO(request))["profile"], "default")
        with self.assertRaises(core.Refused):
            core.read_request(io.BytesIO(b"x" * (core.MAX_REQUEST_BYTES + 1)))


class RuntimeManifestTests(unittest.TestCase):
    def valid_manifest(self):
        files = {path: (content.encode(), mode) for path, (content, mode) in RUNTIME_FILES.items()}
        return _manifest(SHA_A, files)

    def test_accepts_exact_manifest_and_api_range(self):
        manifest = self.valid_manifest()
        manifest["control_helper_api_min"] = 1
        manifest["control_helper_api_max"] = 3
        self.assertEqual(core.validate_runtime_manifest(manifest, pinned_sha=SHA_A)["commit_sha"], SHA_A)

    def test_manifest_read_bound_covers_the_maximum_valid_inventory(self):
        paths = [
            f"lib/vm/inventory-{index:04d}-" + "a" * 218
            for index in range(core.MAX_INVENTORY_ENTRIES - 2)
        ]
        paths.extend(("lib/vm/profile-runtime.py", "profile-runtime.json"))
        manifest = {
            "schema_version": 1,
            "commit_sha": SHA_A,
            "profile_runtime_api": 1,
            "control_helper_api_min": 1,
            "control_helper_api_max": 1,
            "files": [
                {"path": path, "git_mode": "100644", "sha256": "0" * 64}
                for path in sorted(paths)
            ],
        }
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        self.assertGreater(len(encoded), 1024 * 1024)
        self.assertLessEqual(len(encoded), core.MAX_MANIFEST_BYTES)
        self.assertEqual(
            len(core.validate_runtime_manifest(manifest, pinned_sha=SHA_A)["files"]),
            core.MAX_INVENTORY_ENTRIES,
        )

    def test_validates_the_committed_runtime_descriptor_schema(self):
        descriptor = {
            "schema_version": 1,
            "profile_runtime_api": 1,
            "control_helper_api_min": 1,
            "control_helper_api_max": 2,
            "entrypoint": "lib/vm/profile-runtime.py",
            "files": [{"path": "lib/vm/profile-runtime.py", "git_mode": "100755"}],
        }
        self.assertEqual(
            core.validate_runtime_descriptor(descriptor, helper_api=2)["entrypoint"],
            "lib/vm/profile-runtime.py",
        )
        bad_descriptors = []
        duplicate = dict(descriptor)
        duplicate["files"] = [*descriptor["files"], dict(descriptor["files"][0])]
        bad_descriptors.append(duplicate)
        wrong_entrypoint = dict(descriptor)
        wrong_entrypoint["entrypoint"] = "lib/vm/arbitrary.py"
        bad_descriptors.append(wrong_entrypoint)
        descriptor_self_lists = dict(descriptor)
        descriptor_self_lists["files"] = [
            *descriptor["files"], {"path": "profile-runtime.json", "git_mode": "100644"},
        ]
        bad_descriptors.append(descriptor_self_lists)
        incompatible = dict(descriptor)
        incompatible["control_helper_api_min"] = 3
        bad_descriptors.append(incompatible)
        for bad in bad_descriptors:
            with self.subTest(descriptor=bad), self.assertRaises(core.Refused):
                core.validate_runtime_descriptor(bad, helper_api=2)

    def test_rejects_unknown_schema_keys_wrong_pin_and_incompatible_apis(self):
        mutations = []
        unknown = self.valid_manifest()
        unknown["extra"] = "ignored?"
        mutations.append((unknown, SHA_A))
        wrong_sha = self.valid_manifest()
        wrong_sha["commit_sha"] = SHA_B
        mutations.append((wrong_sha, SHA_A))
        wrong_api = self.valid_manifest()
        wrong_api["control_helper_api_min"] = 2
        mutations.append((wrong_api, SHA_A))
        wrong_runtime = self.valid_manifest()
        wrong_runtime["profile_runtime_api"] = 2
        mutations.append((wrong_runtime, SHA_A))
        for manifest, pin in mutations:
            with self.subTest(manifest=manifest), self.assertRaises(core.Refused):
                core.validate_runtime_manifest(manifest, pinned_sha=pin)

    def test_rejects_unsorted_duplicate_missing_and_unsafe_inventory(self):
        valid = self.valid_manifest()
        cases = []
        unsorted = dict(valid)
        unsorted["files"] = list(reversed(valid["files"]))
        cases.append(unsorted)
        duplicate = dict(valid)
        duplicate["files"] = [*valid["files"], valid["files"][0]]
        cases.append(duplicate)
        missing_entrypoint = dict(valid)
        missing_entrypoint["files"] = [entry for entry in valid["files"]
                                        if entry["path"] != "lib/vm/profile-runtime.py"]
        cases.append(missing_entrypoint)
        for unsafe_path in ("lib/vm/../escape", "lib/vm//bad", "/lib/vm/absolute",
                            "lib/vm/a\\b", "docs/readme.md", "lib/vm/é.py"):
            bad = dict(valid)
            bad["files"] = [dict(entry) for entry in valid["files"]]
            bad["files"][0]["path"] = unsafe_path
            bad["files"].sort(key=lambda item: item["path"])
            cases.append(bad)
        for manifest in cases:
            with self.subTest(manifest=manifest), self.assertRaises(core.Refused):
                core.validate_runtime_manifest(manifest, pinned_sha=SHA_A)

    def test_rejects_bad_digest_and_git_mode(self):
        for field, value in (("sha256", "z" * 64), ("git_mode", "120000")):
            manifest = self.valid_manifest()
            manifest["files"][0][field] = value
            with self.subTest(field=field), self.assertRaises(core.Refused):
                core.validate_runtime_manifest(manifest, pinned_sha=SHA_A)


class PinnedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="profile-runtime-")
        self.root = pathlib.Path(self.temp.name)
        self.uid = os.getuid()
        self.runtime_a = RuntimeFixture(self.root, SHA_A)

    def tearDown(self):
        for path in sorted(self.root.rglob("*"), reverse=True):
            if path.is_dir() and not path.is_symlink():
                os.chmod(path, 0o755)
        self.temp.cleanup()

    def verify(self, fixture=None, *, pin=SHA_A, expected_uid=None, **kwargs):
        fixture = fixture or self.runtime_a
        return core.verify_runtime_installation(
            fixture.directory, pinned_sha=pin,
            expected_uid=self.uid if expected_uid is None else expected_uid, **kwargs,
        )

    def test_verifies_complete_sha_addressed_installation(self):
        manifest = self.verify()
        self.assertEqual(manifest["commit_sha"], SHA_A)
        self.assertEqual(
            core.resolve_pinned_runtime(SHA_A, runtime_store=self.runtime_a.store, expected_uid=self.uid),
            self.runtime_a.directory,
        )

    def test_staging_verification_requires_the_exact_reserved_name_and_opt_in(self):
        stage_name = f".staging-{SHA_A}-0123456789abcdef"
        stage = self.runtime_a.store / stage_name
        self.runtime_a.make_directories_writable()
        shutil.copytree(self.runtime_a.directory, stage, copy_function=shutil.copy2)
        for directory in sorted((item for item in stage.rglob("*") if item.is_dir()), reverse=True):
            os.chmod(directory, 0o555)
        os.chmod(stage, 0o555)
        os.chmod(self.runtime_a.store, 0o555)

        with self.assertRaises(core.Refused):
            core.verify_runtime_installation(stage, pinned_sha=SHA_A, expected_uid=self.uid)
        self.assertEqual(
            core.verify_runtime_installation(
                stage, pinned_sha=SHA_A, expected_uid=self.uid, allow_staging=True,
            )["commit_sha"],
            SHA_A,
        )
        malformed = self.runtime_a.store / f".staging-{SHA_A}-not-a-nonce"
        with self.assertRaises(core.Refused):
            core.verify_runtime_installation(
                malformed, pinned_sha=SHA_A, expected_uid=self.uid, allow_staging=True,
            )

    def test_profiles_select_independent_sha_roots_without_checkout_resolution(self):
        runtime_b = RuntimeFixture(self.root, SHA_B, {
            **RUNTIME_FILES,
            "lib/vm/another-module.py": ("other runtime\n", "100644"),
        })
        self.assertEqual(
            core.resolve_pinned_runtime(SHA_A, runtime_store=self.runtime_a.store, expected_uid=self.uid),
            self.runtime_a.directory,
        )
        self.assertEqual(
            core.resolve_pinned_runtime(SHA_B, runtime_store=runtime_b.store, expected_uid=self.uid),
            runtime_b.directory,
        )

    def test_rejects_manifest_sha_that_does_not_match_selected_directory(self):
        with self.assertRaises(core.Refused):
            self.verify(pin=SHA_B)

    def test_rejects_modified_runtime_digest_and_extra_inventory_path(self):
        self.runtime_a.make_directories_writable()
        entrypoint = self.runtime_a.directory / "lib/vm/profile-runtime.py"
        os.chmod(entrypoint, 0o644)
        entrypoint.write_text("modified runtime\n")
        os.chmod(entrypoint, 0o444)
        with self.assertRaises(core.Refused):
            self.verify()

        self.runtime_a.make_directories_writable()
        os.chmod(entrypoint, 0o644)
        entrypoint.write_text("runtime entrypoint\n")
        os.chmod(entrypoint, 0o444)
        extra = self.runtime_a.directory / "lib/vm/extra.py"
        extra.write_text("not declared\n")
        os.chmod(extra, 0o444)
        for directory in (self.runtime_a.directory / "lib/vm", self.runtime_a.directory / "lib",
                          self.runtime_a.directory, self.runtime_a.store):
            os.chmod(directory, 0o555)
        with self.assertRaises(core.Refused):
            self.verify()

    def test_rejects_missing_files_symlinks_and_wrong_modes(self):
        self.runtime_a.make_directories_writable()
        target = self.runtime_a.directory / "lib/vm/profile-runtime.py"
        original = target.read_bytes()
        target.unlink()
        for directory in (self.runtime_a.directory / "lib/vm", self.runtime_a.directory / "lib",
                          self.runtime_a.directory, self.runtime_a.store):
            os.chmod(directory, 0o555)
        with self.assertRaises(core.Refused):
            self.verify()

        self.runtime_a.make_directories_writable()
        target.write_bytes(original)
        os.chmod(target, 0o444)
        target.unlink()
        target.symlink_to(self.runtime_a.directory / "profile-runtime.json")
        for directory in (self.runtime_a.directory / "lib/vm", self.runtime_a.directory / "lib",
                          self.runtime_a.directory, self.runtime_a.store):
            os.chmod(directory, 0o555)
        with self.assertRaises(core.Refused):
            self.verify()

        self.runtime_a.make_directories_writable()
        target.unlink()
        target.write_text("runtime entrypoint\n")
        os.chmod(target, 0o644)
        for directory in (self.runtime_a.directory / "lib/vm", self.runtime_a.directory / "lib",
                          self.runtime_a.directory, self.runtime_a.store):
            os.chmod(directory, 0o555)
        with self.assertRaises(core.Refused):
            self.verify()

    def test_rejects_writable_store_target_and_symlinked_store_ancestry(self):
        os.chmod(self.runtime_a.store, 0o775)
        with self.assertRaises(core.Refused):
            self.verify()
        os.chmod(self.runtime_a.store, 0o555)

        alias = self.root / "runtime-alias"
        alias.symlink_to(self.runtime_a.store, target_is_directory=True)
        with self.assertRaises(core.Refused):
            core.resolve_pinned_runtime(SHA_A, runtime_store=alias, expected_uid=self.uid)

    def test_rejects_wrong_owner_and_incompatible_runtime_api(self):
        with self.assertRaises(core.Refused):
            self.verify(expected_uid=self.uid + 1)
        with self.assertRaises(core.Refused):
            self.verify(helper_api=2)

    def test_rejects_manifest_duplicates_and_unknown_tree_nodes(self):
        self.runtime_a.make_directories_writable()
        manifest_path = self.runtime_a.directory / core.MANIFEST_FILENAME
        os.chmod(manifest_path, 0o644)
        manifest_path.write_bytes(b'{"schema_version":1,"schema_version":1}')
        os.chmod(manifest_path, 0o444)
        for directory in (self.runtime_a.directory / "lib/vm", self.runtime_a.directory / "lib",
                          self.runtime_a.directory, self.runtime_a.store):
            os.chmod(directory, 0o555)
        with self.assertRaises(core.Refused):
            self.verify()

        self.runtime_a.make_directories_writable()
        self.runtime_a.rewrite_manifest(_manifest(SHA_A, {
            path: (content.encode() if isinstance(content, str) else content, mode)
            for path, (content, mode) in self.runtime_a.files.items()
        }))
        extra = self.runtime_a.directory / "lib/vm/unknown.py"
        extra.write_text("unknown node\n")
        os.chmod(extra, 0o444)
        for directory in (self.runtime_a.directory / "lib/vm", self.runtime_a.directory / "lib",
                          self.runtime_a.directory, self.runtime_a.store):
            os.chmod(directory, 0o555)
        with self.assertRaises(core.Refused):
            self.verify()


if __name__ == "__main__":
    unittest.main(verbosity=2)
