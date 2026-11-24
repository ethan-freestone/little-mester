#!/usr/bin/env python3
"""Standard-library checks for the declarative VM profile mapping v1 contract."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "docs/schemas/vm-profile-v1.schema.json"
RESOURCES_PATH = ROOT / "docs/schemas/vm-profile-resources-v1.json"
FIXTURE_DIR = ROOT / "tests/fixtures/vm-profile-schema-v1"
MAX_JSON_BYTES = 256 * 1024
MAX_JSON_DEPTH = 16
MAX_ID = 2147483647
RESERVED_NAMES = {
    "system",
    "profiles",
    "installations",
    "prune",
    "bootstrap",
    "control",
    "prepare-dependencies",
}


class ContractError(ValueError):
    pass


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate object key: {key}")
        result[key] = value
    return result


def parse_finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ContractError("non-finite JSON number")
    return value


def reject_nonfinite_constant(text: str) -> None:
    raise ContractError(f"non-finite JSON constant: {text}")


def _check_depth(value: Any, depth: int = 1) -> None:
    if isinstance(value, (dict, list)):
        if depth > MAX_JSON_DEPTH:
            raise ContractError("JSON nesting exceeds 16 containers")
        children = value.values() if isinstance(value, dict) else value
        for child in children:
            _check_depth(child, depth + 1)


def parse_bounded_json(raw: bytes) -> Any:
    if len(raw) > MAX_JSON_BYTES:
        raise ContractError("JSON exceeds 256 KiB")
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_nonfinite_constant,
            parse_float=parse_finite_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ContractError(f"malformed UTF-8 JSON: {exc}") from exc
    _check_depth(value)
    return value


def _schema_type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return type(value) is int
    if expected == "boolean":
        return type(value) is bool
    if expected == "null":
        return value is None
    if expected == "number":
        return type(value) in (int, float)
    raise ContractError(f"unsupported schema type in checked-in subset: {expected}")


def _strict_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _strict_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _strict_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def _resolve_ref(root_schema: dict[str, Any], ref: str) -> dict[str, Any]:
    if not ref.startswith("#/" ):
        raise ContractError(f"unsupported schema reference: {ref}")
    node: Any = root_schema
    for part in ref[2:].split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    if not isinstance(node, dict):
        raise ContractError(f"schema reference is not an object: {ref}")
    return node


def validate_declarative_subset(
    value: Any,
    rule: dict[str, Any],
    root_schema: dict[str, Any],
    path: str = "$",
) -> None:
    """Validate only the JSON Schema keywords used by the checked-in schema."""
    if "$ref" in rule:
        validate_declarative_subset(
            value, _resolve_ref(root_schema, rule["$ref"]), root_schema, path
        )
        return

    if "allOf" in rule:
        for branch in rule["allOf"]:
            validate_declarative_subset(value, branch, root_schema, path)
    if "oneOf" in rule:
        successes = 0
        for branch in rule["oneOf"]:
            try:
                validate_declarative_subset(value, branch, root_schema, path)
            except ContractError:
                continue
            successes += 1
        if successes != 1:
            raise ContractError(f"{path}: expected exactly one matching schema branch")

    expected_type = rule.get("type")
    if expected_type is not None and not _schema_type_matches(value, expected_type):
        raise ContractError(f"{path}: expected {expected_type}")
    if "const" in rule and not _strict_equal(value, rule["const"]):
        raise ContractError(f"{path}: expected constant {rule['const']!r}")
    if "enum" in rule and not any(
        _strict_equal(value, member) for member in rule["enum"]
    ):
        raise ContractError(f"{path}: value is not in enum")

    if isinstance(value, str):
        if len(value) < rule.get("minLength", 0):
            raise ContractError(f"{path}: string is too short")
        if len(value) > rule.get("maxLength", MAX_JSON_BYTES):
            raise ContractError(f"{path}: string is too long")
        if "pattern" in rule and re.search(rule["pattern"], value) is None:
            raise ContractError(f"{path}: string does not match pattern")
    if type(value) in (int, float):
        if "minimum" in rule and value < rule["minimum"]:
            raise ContractError(f"{path}: number below minimum")
        if "maximum" in rule and value > rule["maximum"]:
            raise ContractError(f"{path}: number above maximum")

    if isinstance(value, dict):
        required = rule.get("required", [])
        missing = set(required) - value.keys()
        if missing:
            raise ContractError(f"{path}: missing required properties {sorted(missing)}")
        properties = rule.get("properties", {})
        if rule.get("additionalProperties") is False:
            extra = value.keys() - properties.keys()
            if extra:
                raise ContractError(f"{path}: extra properties {sorted(extra)}")
        for key, child in value.items():
            if key in properties:
                validate_declarative_subset(
                    child, properties[key], root_schema, f"{path}.{key}"
                )
            elif isinstance(rule.get("additionalProperties"), dict):
                validate_declarative_subset(
                    child, rule["additionalProperties"], root_schema, f"{path}.{key}"
                )

    if isinstance(value, list):
        if len(value) < rule.get("minItems", 0):
            raise ContractError(f"{path}: too few array items")
        if len(value) > rule.get("maxItems", MAX_JSON_BYTES):
            raise ContractError(f"{path}: too many array items")
        if rule.get("uniqueItems"):
            keys = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in value]
            if len(set(keys)) != len(keys):
                raise ContractError(f"{path}: duplicate array items")
        if "items" in rule:
            for index, item in enumerate(value):
                validate_declarative_subset(
                    item, rule["items"], root_schema, f"{path}[{index}]"
                )


def digest_for(owner_uid: int, name: str) -> str:
    if type(owner_uid) is not int or owner_uid < 1:
        raise ContractError("owner UID must be a canonical positive integer")
    if re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name) is None:
        raise ContractError("profile name is outside v1 grammar")
    material = f"little-mester-profile-v1\0{owner_uid}\0{name}".encode("ascii")
    return hashlib.sha256(material).hexdigest()[:20]


def expected_account_marker(owner_uid: int, name: str, role: str) -> str:
    return f"little-mester-vm-profile-v1:{owner_uid}:{name}:{role}"


def expected_group_marker(owner_uid: int, name: str, role: str) -> str:
    return f"little-mester-vm-profile-v1:{owner_uid}:{name}:{role}:group"


def _is_canonical_absolute_path(path: str) -> bool:
    if not path.startswith("/") or "\x00" in path or any(
        ord(char) < 32 or ord(char) == 127 for char in path
    ):
        return False
    if path == "/":
        return True
    if path.startswith("//") or path.endswith("/"):
        return False
    return all(part not in ("", ".", "..") for part in path[1:].split("/"))


def _path_is_within(path: str, parent: str) -> bool:
    if parent == "/":
        return path.startswith("/")
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def decode_mountinfo_field(field: str) -> str:
    """Decode only the four Linux mountinfo octal escapes defined by procfs."""
    result: list[str] = []
    index = 0
    escapes = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}
    while index < len(field):
        if field[index] != "\\":
            result.append(field[index])
            index += 1
            continue
        code = field[index + 1 : index + 4]
        if code not in escapes:
            raise ContractError("invalid mountinfo escape")
        result.append(escapes[code])
        index += 4
    return "".join(result)


def _expected_step(operation: str, phase: str) -> int:
    steps = {
        "install": {"prepared": 0, "accounts": 1, "state": 2, "units": 3, "verified": 4},
        "reset": {"prepared": 0, "leaves": 1, "verified": 2},
        "remove": {"prepared": 0, "units": 1, "state": 2, "accounts": 3, "finalizing": 4},
    }
    try:
        return steps[operation][phase]
    except KeyError as exc:
        raise ContractError("unsupported transaction operation/phase pair") from exc


def _resource_maps(resources: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    by_selector = {item["selector"]: item for item in resources["resources"]}
    if len(by_selector) != len(resources["resources"]):
        raise ContractError("duplicate resource selector")
    return by_selector, {key: value for key, value in resources["transaction_allowlists"].items()}


def validate_profile_semantics(
    profile: Any, schema: dict[str, Any], resources: dict[str, Any]
) -> None:
    validate_declarative_subset(profile, schema, schema)
    if not isinstance(profile, dict):
        raise ContractError("profile mapping must be an object")

    owner_uid = profile["owner_uid"]
    name = profile["name"]
    if name in RESERVED_NAMES:
        raise ContractError("reserved profile name")
    if owner_uid > MAX_ID:
        raise ContractError("owner UID outside bound")
    digest = digest_for(owner_uid, name)

    vm = profile["accounts"]["vm"]
    proxy = profile["accounts"]["proxy"]
    if vm["name"] != f"lmv-{digest}" or proxy["name"] != f"lmp-{digest}":
        raise ContractError("account names do not match deterministic digest")
    if vm["uid"] == proxy["uid"] or vm["gid"] == proxy["gid"]:
        raise ContractError("VM and proxy numeric identities collide")
    if owner_uid in (vm["uid"], proxy["uid"]):
        raise ContractError("service UID collides with enrolled owner")
    expected_units = {
        "vm": f"little-mester-vm-p-{digest}.service",
        "proxy": f"little-mester-vm-p-{digest}-proxy.service",
        "workspace": f"little-mester-vm-p-{digest}-workspace.service",
        "metrics": f"little-mester-vm-p-{digest}-metrics.service",
    }
    if profile["units"] != expected_units:
        raise ContractError("unit names do not match deterministic digest and role")

    runtime = profile["runtime"]
    if runtime["control_helper_api_min"] > runtime["control_helper_api_max"]:
        raise ContractError("runtime helper API range is reversed")

    ports = [profile["ssh_port"], profile["ssh_port"] + 1, profile["ssh_port"] + 2]
    if any(type(port) is not int or not 1 <= port <= 65535 for port in ports):
        raise ContractError("derived port outside TCP port range")
    if len(set(ports)) != 3 or 22222 in ports:
        raise ContractError("derived ports collide with one another or singleton SSH")

    workspace = profile["workspace"]
    if workspace is not None:
        paths = [
            workspace["canonical_path"],
            workspace["mount"]["root"],
            workspace["mount"]["mount_point"],
        ]
        if not all(_is_canonical_absolute_path(path) for path in paths):
            raise ContractError("workspace contains a noncanonical absolute path")
        if not _path_is_within(workspace["canonical_path"], workspace["mount"]["mount_point"]):
            raise ContractError("workspace path is not beneath its recorded mount point")
        if os.major(workspace["device"]) != workspace["mount"]["major"] or os.minor(
            workspace["device"]
        ) != workspace["mount"]["minor"]:
            raise ContractError("workspace device and mount major/minor disagree")
        if type(workspace["unsafe_recurse"]) is not bool:
            raise ContractError("unsafe_recurse must be an exact JSON boolean")

    transaction = profile["transaction"]
    state = profile["state"]
    if state == "ready":
        if transaction is not None:
            raise ContractError("ready mapping must have null transaction")
        if vm["allocation"] != "committed" or proxy["allocation"] != "committed":
            raise ContractError("ready mapping requires committed VM and proxy accounts")
        return

    expected_operation = {
        "installing": "install",
        "resetting": "reset",
        "removing": "remove",
    }[state]
    if transaction is None or transaction["operation"] != expected_operation:
        raise ContractError("lifecycle state and transaction operation do not pair")
    operation = transaction["operation"]
    phase = transaction["phase"]
    if transaction["step"] != _expected_step(operation, phase):
        raise ContractError("transaction step does not match its phase")

    by_selector, allowlists = _resource_maps(resources)
    allowed = set(allowlists[operation])
    seen: set[str] = set()
    expected_by_selector: dict[str, dict[str, Any]] = {}
    for item in transaction["expected"]:
        selector = item["selector"]
        if selector in seen:
            raise ContractError("duplicate transaction selector")
        seen.add(selector)
        expected_by_selector[selector] = item
        if selector not in allowed:
            raise ContractError(f"selector {selector!r} is not allowlisted for {operation}")
        if selector not in by_selector:
            raise ContractError(f"unknown resource selector {selector!r}")
        if "identity" in item:
            identity = item["identity"]
            resource_kind = by_selector[selector]["kind"]
            evidence = item["evidence"]
            if resource_kind in {"regular-file", "directory", "socket"}:
                expected_kind = {
                    "regular-file": "regular",
                    "directory": "directory",
                    "socket": "socket",
                }[resource_kind]
                if identity["kind"] != expected_kind:
                    raise ContractError(f"{selector} identity kind does not match resource table")
            elif resource_kind == "host-account-identity":
                role = selector.removeprefix("account.")
                record = profile["accounts"][role]
                if (identity["uid"], identity["gid"]) != (record["uid"], record["gid"]):
                    raise ContractError(f"{selector} identity differs from mapping")
                if identity["marker"] != expected_account_marker(owner_uid, name, role):
                    raise ContractError(f"{selector} marker differs from recomputed marker")
                if evidence == "observed_present" and record["allocation"] != "committed":
                    raise ContractError(f"{selector} present while allocation is only planned")
                if evidence == "expected_absent":
                    raise ContractError("account absence is handled without an identity")
            elif resource_kind == "host-group-identity":
                role = selector.removeprefix("group.")
                record = profile["accounts"][role]
                if identity["gid"] != record["gid"]:
                    raise ContractError(f"{selector} identity differs from mapping")
                if identity["marker"] != expected_group_marker(owner_uid, name, role):
                    raise ContractError(f"{selector} marker differs from recomputed marker")
            elif resource_kind == "unit-manager-identity":
                role = selector.removeprefix("unit-manager.")
                fragment = expected_by_selector.get(f"unit-file.{role}")
                # A transaction may list manager before its corresponding file;
                # the complete pair is checked after the loop below.
                del fragment
        else:
            resource_kind = by_selector[selector]["kind"]
            if resource_kind in {"host-account-identity", "host-group-identity"}:
                role = selector.split(".", 1)[1]
                if profile["accounts"][role]["allocation"] != "planned":
                    raise ContractError(f"committed {selector} may not be recorded absent")

        evidence = item["evidence"]
        if evidence == "never_created":
            if operation != "remove" or not selector.startswith(("unit-file.", "unit-manager.")):
                raise ContractError("never_created is limited to remove unit selectors")
            if item["install_transaction_id"] == transaction["id"]:
                raise ContractError("never_created must reference a distinct install transaction")
        elif evidence == "expected_absent" and operation == "remove" and selector.startswith(
            ("unit-file.", "unit-manager.")
        ):
            raise ContractError("unit absence needs removal intent or never-created evidence")
        elif evidence == "removal_intent":
            if operation == "reset":
                if selector not in {
                    "state.vm.disk",
                    "state.vm.seed",
                    "state.vm.host-key-private",
                    "state.vm.host-key-public",
                    "state.client.control-key-private",
                    "state.client.control-key-public",
                    "state.client.known-hosts",
                } or phase not in {"leaves", "verified"}:
                    raise ContractError("reset removal intent is outside the leaf phase")
            elif operation == "remove":
                if selector.startswith(("unit-file.", "unit-manager.")):
                    valid_phases = {"units", "state", "accounts", "finalizing"}
                elif selector.startswith(("account.", "group.")):
                    valid_phases = {"accounts", "finalizing"}
                else:
                    valid_phases = {"state", "accounts", "finalizing"}
                if phase not in valid_phases or selector == "profile.config-dir":
                    raise ContractError("remove intent is outside its durable object phase")
            else:
                raise ContractError("install does not authorize removal intent")

    for selector, item in expected_by_selector.items():
        if not selector.startswith("unit-manager.") or "identity" not in item:
            continue
        role = selector.removeprefix("unit-manager.")
        fragment = expected_by_selector.get(f"unit-file.{role}")
        if fragment is None or "identity" not in fragment:
            raise ContractError("manager identity lacks its exact unit-fragment identity")
        manager_identity = item["identity"]
        file_identity = fragment["identity"]
        if (
            manager_identity["fragment_device"] != file_identity["device"]
            or manager_identity["fragment_inode"] != file_identity["inode"]
        ):
            raise ContractError("manager identity is not bound to the recorded fragment")


def validate_catalog_ports(profiles: list[dict[str, Any]]) -> None:
    used: dict[int, str] = {22222: "fixed singleton SSH"}
    names: set[tuple[int, str]] = set()
    for profile in profiles:
        validate_profile_semantics(profile, SCHEMA, RESOURCES)
        key = (profile["owner_uid"], profile["name"])
        if key in names:
            raise ContractError("duplicate owner/profile mapping in catalog")
        names.add(key)
        ports = [profile["ssh_port"], profile["ssh_port"] + 1, profile["ssh_port"] + 2]
        for port in ports:
            if port in used:
                raise ContractError(f"port {port} collides with {used[port]}")
            used[port] = f"{profile['name']}:{port}"
    if len(profiles) > 1000:
        raise ContractError("profile catalog exceeds 1000 records")


def _file_identity(kind: str, device: int = 8, inode: int = 42) -> dict[str, Any]:
    return {"device": device, "inode": inode, "kind": kind, "link_count": 1}


def _account_identity(owner_uid: int, name: str, role: str, uid: int, gid: int) -> dict[str, Any]:
    return {
        "uid": uid,
        "gid": gid,
        "marker": expected_account_marker(owner_uid, name, role),
    }


def _group_identity(owner_uid: int, name: str, role: str, gid: int) -> dict[str, Any]:
    return {"gid": gid, "marker": expected_group_marker(owner_uid, name, role)}


def _manager_identity(device: int, inode: int) -> dict[str, Any]:
    return {
        "fragment_device": device,
        "fragment_inode": inode,
        "load_state": "loaded",
        "active_state": "inactive",
        "job_state": "none",
    }


def make_profile(owner_uid: int = 1000, name: str = "default", ssh_port: int = 22300) -> dict[str, Any]:
    digest = digest_for(owner_uid, name)
    return {
        "schema_version": 1,
        "owner_uid": owner_uid,
        "name": name,
        "state": "ready",
        "runtime": {
            "commit_sha": "a" * 40,
            "profile_runtime_api": 1,
            "control_helper_api_min": 1,
            "control_helper_api_max": 3,
        },
        "accounts": {
            "vm": {"name": f"lmv-{digest}", "uid": 995, "gid": 995, "allocation": "committed"},
            "proxy": {"name": f"lmp-{digest}", "uid": 996, "gid": 996, "allocation": "committed"},
        },
        "units": {
            "vm": f"little-mester-vm-p-{digest}.service",
            "proxy": f"little-mester-vm-p-{digest}-proxy.service",
            "workspace": f"little-mester-vm-p-{digest}-workspace.service",
            "metrics": f"little-mester-vm-p-{digest}-metrics.service",
        },
        "workspace": {
            "canonical_path": "/workspaces/alpha",
            "device": os.makedev(8, 1),
            "inode": 12345,
            "mount_id": 7,
            "mount": {"major": 8, "minor": 1, "root": "/", "mount_point": "/"},
            "unsafe_recurse": False,
        },
        "ssh_port": ssh_port,
        "transaction": None,
    }


with SCHEMA_PATH.open("rb") as schema_file:
    SCHEMA = parse_bounded_json(schema_file.read())
with RESOURCES_PATH.open("rb") as resources_file:
    RESOURCES = parse_bounded_json(resources_file.read())


class VmProfileSchemaFixtures(unittest.TestCase):
    def read_fixture(self, name: str) -> Any:
        return parse_bounded_json((FIXTURE_DIR / name).read_bytes())

    def assert_profile_valid(self, profile: Any) -> None:
        validate_profile_semantics(profile, SCHEMA, RESOURCES)

    def test_valid_ready_installing_resetting_and_removing_fixtures(self) -> None:
        for name in (
            "valid-ready.json",
            "valid-installing.json",
            "valid-resetting.json",
            "valid-removing.json",
        ):
            with self.subTest(fixture=name):
                self.assert_profile_valid(self.read_fixture(name))
        sha64 = make_profile()
        sha64["runtime"]["commit_sha"] = "b" * 64
        self.assert_profile_valid(sha64)

    def test_invalid_fixtures_fail_schema_or_semantic_validation(self) -> None:
        for name in sorted(path.name for path in FIXTURE_DIR.glob("invalid-*.json")):
            with self.subTest(fixture=name), self.assertRaises(ContractError):
                self.assert_profile_valid(self.read_fixture(name))

    def test_profile_schema_is_closed_at_every_object_schema(self) -> None:
        def visit(node: Any) -> None:
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False)
                    self.assertEqual(set(node.get("required", [])), set(node.get("properties", {})))
                for child in node.values():
                    visit(child)
            elif isinstance(node, list):
                for child in node:
                    visit(child)

        visit(SCHEMA)
        self.assertEqual(SCHEMA["$schema"], "https://json-schema.org/draft/2020-12/schema")
        supported = {
            "$schema", "$id", "$defs", "title", "description", "type",
            "additionalProperties", "required", "properties", "$ref", "const",
            "enum", "pattern", "minLength", "maxLength", "minimum", "maximum",
            "oneOf", "allOf", "items", "maxItems", "uniqueItems",
        }

        def check_keywords(node: Any, schema_node: bool = True) -> None:
            if isinstance(node, dict):
                if schema_node:
                    self.assertLessEqual(set(node), supported)
                for key, child in node.items():
                    if key in {"$defs", "properties"}:
                        for schema_child in child.values():
                            check_keywords(schema_child)
                    elif key in {"oneOf", "allOf"}:
                        for schema_child in child:
                            check_keywords(schema_child)
                    elif key in {"items", "additionalProperties"} and isinstance(child, dict):
                        check_keywords(child)

        check_keywords(SCHEMA)

    def test_resource_selector_classes_and_transaction_allowlists_match_schema(self) -> None:
        resources = RESOURCES["resources"]
        by_selector, allowlists = _resource_maps(RESOURCES)
        self.assertEqual(len(by_selector), len(resources))
        self.assertEqual(
            set(RESOURCES),
            {
                "schema_version", "description", "profile_roots", "resources",
                "account_roles", "ports", "transaction_allowlists", "evidence",
                "fixed_locks", "catalog_snapshots", "runtime_installation",
                "runtime_roles", "refuse_not_selectors",
            },
        )
        for item in resources:
            self.assertEqual(
                set(item), {"selector", "kind", "path_template", "owner", "mode", "operations"}
            )
            self.assertEqual(set(item["operations"]), {"install", "reset", "remove"})
        defs = SCHEMA["$defs"]
        expected_file_selectors = {
            item["selector"]
            for item in resources
            if item["kind"] in {"regular-file", "directory", "socket"}
            and item["selector"] != "profile.mapping"
        }
        self.assertEqual(set(defs["fileSelector"]["enum"]), expected_file_selectors)
        self.assertEqual(
            set(defs["accountSelector"]["enum"]),
            {key for key, item in by_selector.items() if item["kind"] == "host-account-identity"},
        )
        self.assertEqual(
            set(defs["groupSelector"]["enum"]),
            {key for key, item in by_selector.items() if item["kind"] == "host-group-identity"},
        )
        self.assertEqual(
            set(defs["managerSelector"]["enum"]),
            {key for key, item in by_selector.items() if item["kind"] == "unit-manager-identity"},
        )
        self.assertEqual(set(defs["accountSelector"]["enum"]), {"account.vm", "account.proxy"})
        self.assertEqual(set(defs["groupSelector"]["enum"]), {"group.vm", "group.proxy"})
        self.assertEqual(
            set(defs["managerSelector"]["enum"]),
            {"unit-manager.vm", "unit-manager.proxy", "unit-manager.workspace", "unit-manager.metrics"},
        )
        for operation, selectors in allowlists.items():
            self.assertLessEqual(len(selectors), 64)
            self.assertEqual(len(selectors), len(set(selectors)))
            for selector in selectors:
                self.assertIn(selector, by_selector)
                self.assertTrue(by_selector[selector]["operations"][operation])
        self.assertEqual(
            set(allowlists["reset"]),
            {
                "state.vm.disk",
                "state.vm.seed",
                "state.vm.host-key-private",
                "state.vm.host-key-public",
                "state.client.control-key-private",
                "state.client.control-key-public",
                "state.client.known-hosts",
            },
        )

    def test_resource_table_freezes_lock_manifest_and_refusal_boundaries(self) -> None:
        lock_files = {item["basename"]: item for item in RESOURCES["fixed_locks"]["files"]}
        self.assertEqual(
            set(lock_files),
            {
                "maintenance.lock",
                "catalog.lock",
                "host-admission.lock",
                "profile-<digest>.lock",
                "sha-<full-lowercase-40-or-64-hex-sha>.lock",
            },
        )
        self.assertEqual(lock_files["sha-<full-lowercase-40-or-64-hex-sha>.lock"]["mode"], "0644")
        self.assertEqual(lock_files["catalog.lock"]["mode"], "0600")
        runtime = RESOURCES["runtime_installation"]
        self.assertEqual(runtime["manifest_basename"], "manifest.json")
        self.assertEqual(runtime["manifest_inventory_key"], "files")
        self.assertEqual(runtime["stage_basename"], ".staging-<full-SHA>-<16-lowercase-hex>")
        self.assertEqual(runtime["gc_basename"], ".gc-<full-SHA>-<16-lowercase-hex>")
        refusals = "\n".join(RESOURCES["refuse_not_selectors"])
        for boundary in ("browser-auth", "workspace probe", "provisioning scratch", "unknown children", "Common-control"):
            self.assertIn(boundary.lower(), refusals.lower())

    def test_deterministic_digest_and_generated_name_vectors(self) -> None:
        vectors = [
            (1000, "default", "8b95a12479c85e8b1338"),
            (1000, "work", "1c04efbef58307f16a27"),
            (65534, "a", "d2c5616a3545a5ad0e4b"),
        ]
        for owner_uid, name, expected in vectors:
            digest = hashlib.sha256(
                f"little-mester-profile-v1\0{owner_uid}\0{name}".encode("ascii")
            ).hexdigest()[:20]
            self.assertEqual(digest, expected)
            self.assertEqual(digest_for(owner_uid, name), expected)
            profile = make_profile(owner_uid, name)
            self.assertEqual(profile["accounts"]["vm"]["name"], f"lmv-{expected}")
            self.assertEqual(profile["accounts"]["proxy"]["name"], f"lmp-{expected}")
            self.assertEqual(profile["units"]["vm"], f"little-mester-vm-p-{expected}.service")

    def test_catalog_port_uniqueness_and_singleton_reservation(self) -> None:
        first = make_profile(1000, "default", 22300)
        second = make_profile(1001, "other", 22301)
        with self.assertRaisesRegex(ContractError, "collides"):
            validate_catalog_ports([first, second])
        validate_catalog_ports([make_profile(1000, "default", 22300), make_profile(1001, "other", 22304)])

    def test_transaction_state_phase_selector_and_evidence_rules(self) -> None:
        bad_pair = make_profile()
        bad_pair["state"] = "resetting"
        with self.assertRaisesRegex(ContractError, "state and transaction"):
            self.assert_profile_valid(bad_pair)

        wrong_step = self.read_fixture("valid-installing.json")
        wrong_step["transaction"]["step"] = 1
        wrong_step["transaction"]["phase"] = "units"
        with self.assertRaisesRegex(ContractError, "step does not match"):
            self.assert_profile_valid(wrong_step)

        wrong_selector = make_profile()
        wrong_selector["state"] = "resetting"
        wrong_selector["transaction"] = {
            "schema_version": 1,
            "id": "1" * 32,
            "operation": "reset",
            "phase": "leaves",
            "step": 1,
            "expected": [{"selector": "unit-file.vm", "evidence": "expected_absent"}],
        }
        with self.assertRaisesRegex(ContractError, "not allowlisted"):
            self.assert_profile_valid(wrong_selector)

        removal_absence = make_profile()
        removal_absence["state"] = "removing"
        removal_absence["transaction"] = {
            "schema_version": 1,
            "id": "2" * 32,
            "operation": "remove",
            "phase": "units",
            "step": 1,
            "expected": [{"selector": "unit-file.vm", "evidence": "expected_absent"}],
        }
        with self.assertRaisesRegex(ContractError, "unit absence"):
            self.assert_profile_valid(removal_absence)

        never_created = copy.deepcopy(removal_absence)
        never_created["transaction"]["expected"] = [
            {
                "selector": "unit-file.vm",
                "evidence": "never_created",
                "install_transaction_id": "3" * 32,
                "install_phase": "state",
            }
        ]
        self.assert_profile_valid(never_created)
        never_created["transaction"]["expected"][0]["install_phase"] = "units"
        with self.assertRaises(ContractError):
            self.assert_profile_valid(never_created)

        missing_intent_identity = copy.deepcopy(removal_absence)
        missing_intent_identity["transaction"]["expected"] = [
            {"selector": "state.vm.disk", "evidence": "removal_intent"}
        ]
        with self.assertRaises(ContractError):
            self.assert_profile_valid(missing_intent_identity)

    def test_duplicate_selector_and_identity_collisions_refuse(self) -> None:
        duplicate = self.read_fixture("valid-resetting.json")
        duplicate["transaction"]["expected"].append(
            {"selector": "state.vm.disk", "evidence": "expected_absent"}
        )
        with self.assertRaisesRegex(ContractError, "duplicate transaction selector"):
            self.assert_profile_valid(duplicate)

        collision = make_profile()
        collision["accounts"]["proxy"]["uid"] = collision["accounts"]["vm"]["uid"]
        with self.assertRaisesRegex(ContractError, "numeric identities collide"):
            self.assert_profile_valid(collision)

    def test_invalid_workspace_and_full_sha_are_rejected(self) -> None:
        bad_workspace = make_profile()
        bad_workspace["workspace"]["canonical_path"] = "/workspaces/../secret"
        with self.assertRaisesRegex(ContractError, "noncanonical"):
            self.assert_profile_valid(bad_workspace)
        bad_sha = make_profile()
        bad_sha["runtime"]["commit_sha"] = "a" * 39
        with self.assertRaises(ContractError):
            self.assert_profile_valid(bad_sha)
        bad_port = make_profile()
        bad_port["ssh_port"] = 23223
        with self.assertRaises(ContractError):
            self.assert_profile_valid(bad_port)
        bad_id = make_profile()
        bad_id["accounts"]["vm"]["uid"] = 2147483648
        with self.assertRaises(ContractError):
            self.assert_profile_valid(bad_id)
        float_id = make_profile()
        float_id["owner_uid"] = 1000.0
        with self.assertRaises(ContractError):
            self.assert_profile_valid(float_id)

    def test_mountinfo_escape_decoder_is_closed(self) -> None:
        self.assertEqual(decode_mountinfo_field("/work/with\\040space\\134name"), "/work/with space\\name")
        for malformed in ("bad\\041escape", "bad\\04", "bad\\777"):
            with self.subTest(field=malformed), self.assertRaises(ContractError):
                decode_mountinfo_field(malformed)

    def test_bounded_parser_rejects_duplicate_keys_nonfinite_utf8_trailing_size_and_depth(self) -> None:
        invalid_documents = [
            b'{"x":1,"x":2}',
            b'{"x":NaN}',
            b'{"x":Infinity}',
            b'{"x":-Infinity}',
            b'{"x":1e999}',
            b"\xff",
            b"{} {}",
        ]
        for raw in invalid_documents:
            with self.subTest(raw=raw), self.assertRaises(ContractError):
                parse_bounded_json(raw)
        with self.assertRaisesRegex(ContractError, "256 KiB"):
            parse_bounded_json(b" " * (MAX_JSON_BYTES + 1))
        too_deep = b"[" * MAX_JSON_DEPTH + b"0" + b"]" * MAX_JSON_DEPTH
        parse_bounded_json(too_deep)
        too_deep = b"[" * (MAX_JSON_DEPTH + 1) + b"0" + b"]" * (MAX_JSON_DEPTH + 1)
        with self.assertRaisesRegex(ContractError, "nesting"):
            parse_bounded_json(too_deep)


if __name__ == "__main__":
    unittest.main(verbosity=2)
