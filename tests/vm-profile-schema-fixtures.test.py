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
    return f"little-mester-vm-profile-v1/{owner_uid}/{name}/{role}"


def expected_group_provenance(
    owner_uid: int, name: str, role: str, gid: int, install_id: str
) -> dict[str, Any]:
    digest = digest_for(owner_uid, name)
    group_name = f"{'lmv' if role == 'vm' else 'lmp'}-{digest}"
    return {
        "schema_version": 1,
        "owner_uid": owner_uid,
        "profile_name": name,
        "role": role,
        "group_name": group_name,
        "gid": gid,
        "origin_install_transaction_id": install_id,
    }


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


def validate_profile_schema(profile: Any, schema: dict[str, Any] | None = None) -> None:
    """Apply only the checked-in JSON Schema subset, without model assumptions."""
    schema = SCHEMA if schema is None else schema
    validate_declarative_subset(profile, schema, schema)


def _publication_rank(record: dict[str, Any]) -> int:
    return {"not_started": 0, "publication_intent": 1, "published": 2}[record["state"]]


def _publication_identity(record: dict[str, Any]) -> dict[str, Any] | None:
    if record["state"] == "publication_intent":
        return record["stage_identity"]
    if record["state"] == "published":
        return record["fragment_identity"]
    return None


def _expected_by_selector(transaction: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["selector"]: item for item in transaction["expected"]}


def _unit_role(selector: str) -> str:
    return selector.rsplit(".", 1)[1]


def _validate_numeric_replan_history(profile: dict[str, Any], transaction: dict[str, Any]) -> None:
    latest: dict[tuple[str, str], int] = {}
    for item in transaction["numeric_replans"]:
        key = (item["role"], item["field"])
        if item["previous"] == item["replacement"]:
            raise ContractError("numeric collision replan must select a different planned ID")
        if key in latest and item["previous"] != latest[key]:
            raise ContractError("numeric replan journal chain is discontinuous")
        latest[key] = item["replacement"]
    for (role, field), replacement in latest.items():
        if profile["accounts"][role][field] != replacement:
            raise ContractError("numeric replan journal does not match the current planned identity")


def _validate_publication_record(publication: dict[str, Any], phase: str | None = None) -> None:
    roles = ("vm", "proxy", "workspace", "metrics")
    ranks = [_publication_rank(publication[role]) for role in roles]
    if any(left < right for left, right in zip(ranks, ranks[1:])):
        raise ContractError("unit publication roles are not a monotonic four-role prefix")
    if phase in {"prepared", "accounts", "state"} and any(ranks):
        raise ContractError("unit publication must remain not_started before units phase")
    if phase == "verified" and ranks != [2, 2, 2, 2]:
        raise ContractError("verified install requires all four unit fragments published")


def _validate_reset_retained_source(profile: dict[str, Any]) -> None:
    """Independently validate reset's retained account/group/unit source."""
    owner_uid = profile["owner_uid"]
    name = profile["name"]
    installation = profile["installation"]
    for role in ("vm", "proxy"):
        account = profile["accounts"][role]
        group = installation["groups"][role]
        expected_provenance = expected_group_provenance(
            owner_uid, name, role, account["gid"], installation["id"]
        )
        if (
            account["allocation"] != "committed"
            or group["state"] != "owned"
            or group["name"] != account["name"]
            or group["gid"] != account["gid"]
            or group["provenance"] != expected_provenance
        ):
            raise ContractError("reset requires retained committed accounts and owned group provenance")
    publication = installation["unit_publication"]
    if any(
        publication[role]["state"] != "published"
        for role in ("vm", "proxy", "workspace", "metrics")
    ):
        raise ContractError("reset requires all retained unit publications to be published")


def validate_profile_semantics(
    profile: Any, schema: dict[str, Any], resources: dict[str, Any]
) -> None:
    validate_profile_schema(profile, schema)
    if not isinstance(profile, dict):
        raise ContractError("profile mapping must be an object")

    owner_uid = profile["owner_uid"]
    name = profile["name"]
    if name in RESERVED_NAMES:
        raise ContractError("reserved profile name")
    if owner_uid > MAX_ID:
        raise ContractError("owner UID outside bound")
    digest = digest_for(owner_uid, name)
    installation = profile["installation"]
    install_id = installation["id"]

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

    for role in ("vm", "proxy"):
        account = profile["accounts"][role]
        group = installation["groups"][role]
        if (group["name"], group["gid"]) != (account["name"], account["gid"]):
            raise ContractError(f"{role} group provenance differs from mapped name/GID")
        expected_provenance = expected_group_provenance(
            owner_uid, name, role, group["gid"], install_id
        )
        if group["provenance"] != expected_provenance:
            raise ContractError(f"{role} group provenance does not match install identity")
        if group["state"] == "creation_intent":
            raise ContractError(
                f"ambiguous interrupted {role} group creation; refuse adoption and preserve the journal"
            )

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
        _validate_reset_retained_source(profile)
        _validate_publication_record(installation["unit_publication"])
        return

    expected_operation = {"installing": "install", "resetting": "reset", "removing": "remove"}[state]
    if transaction is None or transaction["operation"] != expected_operation:
        raise ContractError("lifecycle state and transaction operation do not pair")
    operation = transaction["operation"]
    phase = transaction["phase"]
    if transaction["step"] != _expected_step(operation, phase):
        raise ContractError("transaction step does not match its phase")

    if operation == "reset":
        _validate_reset_retained_source(profile)

    if operation == "install":
        _validate_numeric_replan_history(profile, transaction)
        if transaction["id"] != install_id:
            raise ContractError("install transaction differs from retained installation source ID")
        if transaction["unit_publication"] != installation["unit_publication"]:
            raise ContractError("install transaction differs from retained four-role publication record")
        _validate_publication_record(transaction["unit_publication"], phase)
    elif operation == "remove":
        if transaction["source_install_id"] != install_id:
            raise ContractError("remove source install ID differs from retained installation record")
        if transaction["unit_publication"] != installation["unit_publication"]:
            raise ContractError("remove transaction did not preserve the complete source publication snapshot")

    by_selector, allowlists = _resource_maps(resources)
    allowed = set(allowlists[operation])
    expected_by_selector: dict[str, dict[str, Any]] = {}
    if operation == "remove":
        _validate_remove_publication_source_consistency(
            transaction, _expected_by_selector(transaction)
        )
    for item in transaction["expected"]:
        selector = item["selector"]
        if selector in expected_by_selector:
            raise ContractError("duplicate transaction selector")
        expected_by_selector[selector] = item
        if selector not in allowed:
            raise ContractError(f"selector {selector!r} is not allowlisted for {operation}")
        if selector not in by_selector:
            raise ContractError(f"unknown resource selector {selector!r}")
        evidence = item["evidence"]
        if "identity" in item:
            identity = item["identity"]
            resource_kind = by_selector[selector]["kind"]
            if resource_kind in {"regular-file", "directory", "socket"}:
                expected_kind = {"regular-file": "regular", "directory": "directory", "socket": "socket"}[resource_kind]
                if identity["kind"] != expected_kind:
                    raise ContractError(f"{selector} identity kind does not match resource table")
            elif resource_kind == "host-account-identity":
                role = selector.removeprefix("account.")
                record = profile["accounts"][role]
                if (identity["uid"], identity["gid"]) != (record["uid"], record["gid"]):
                    raise ContractError(f"{selector} identity differs from mapping UID/GID")
                if identity["marker"] != expected_account_marker(owner_uid, name, role):
                    raise ContractError(f"{selector} GECOS marker differs from canonical identification marker")
                if evidence in {"observed_present", "removal_intent"} and record["allocation"] != "committed":
                    raise ContractError(f"{selector} present while allocation is only planned")
                if evidence == "removal_intent":
                    group = installation["groups"][role]
                    expected_provenance = expected_group_provenance(
                        owner_uid, name, role, record["gid"], install_id
                    )
                    if (
                        group["state"] != "owned"
                        or group["name"] != record["name"]
                        or group["gid"] != record["gid"]
                        or group["provenance"] != expected_provenance
                    ):
                        raise ContractError(
                            f"{selector} removal intent lacks its exact owned primary-group provenance"
                        )
            elif resource_kind == "host-group-identity":
                role = selector.removeprefix("group.")
                record = installation["groups"][role]
                expected_identity = {
                    "name": record["name"],
                    "gid": record["gid"],
                    "provenance": record["provenance"],
                }
                if identity != expected_identity:
                    raise ContractError(f"{selector} identity/provenance differs from protected mapping")
                if evidence == "creation_intent" and record["state"] != "creation_intent":
                    raise ContractError(f"{selector} creation intent is not journaled")
                if evidence in {"observed_present", "removal_intent"} and record["state"] != "owned":
                    raise ContractError(f"{selector} group lacks committed protected provenance")
            elif resource_kind == "unit-manager-identity":
                pass
        else:
            resource_kind = by_selector[selector]["kind"]
            if resource_kind == "host-account-identity":
                role = selector.split(".", 1)[1]
                if profile["accounts"][role]["allocation"] != "planned":
                    raise ContractError(f"committed {selector} may not be recorded absent")
            elif resource_kind == "host-group-identity":
                role = selector.split(".", 1)[1]
                if installation["groups"][role]["state"] != "not_started":
                    raise ContractError(f"{selector} absence conflicts with group provenance state")

        if evidence == "never_created":
            if operation != "remove" or not selector.startswith(("unit-file.", "unit-stage.", "unit-manager.")):
                raise ContractError("never_created is limited to remove unit selectors")
            if item["install_transaction_id"] != transaction["source_install_id"]:
                raise ContractError("never_created install ID differs from retained source record")
            role = _unit_role(selector)
            if transaction["unit_publication"][role]["state"] != "not_started":
                raise ContractError("never_created requires matching role state not_started")
        elif evidence == "verified_absent":
            if operation != "remove" or item["install_transaction_id"] != transaction["source_install_id"]:
                raise ContractError("verified unit absence is not bound to the source install")
            role = _unit_role(selector)
            source_state = transaction["unit_publication"][role]["state"]
            if item["publication_state"] != source_state:
                raise ContractError("verified absence publication state differs from retained source")
        elif evidence == "expected_absent" and operation == "remove" and selector.startswith(
            ("unit-file.", "unit-stage.", "unit-manager.")
        ):
            raise ContractError("unit absence needs exact never-created or verified/removal evidence")
        elif evidence == "removal_intent":
            if operation == "reset":
                if selector not in {
                    "state.vm.disk", "state.vm.seed", "state.vm.host-key-private",
                    "state.vm.host-key-public", "state.client.control-key-private",
                    "state.client.control-key-public", "state.client.known-hosts",
                } or phase not in {"leaves", "verified"}:
                    raise ContractError("reset removal intent is outside the leaf phase")
            elif operation == "remove":
                if selector.startswith(("unit-file.", "unit-stage.", "unit-manager.")):
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
        role = _unit_role(selector)
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

    if operation == "install":
        _validate_install_publication_evidence(transaction, expected_by_selector)
    elif operation == "remove":
        if phase in {"units", "state", "accounts", "finalizing"}:
            _validate_remove_publication_evidence(transaction, expected_by_selector)


def _validate_install_publication_evidence(
    transaction: dict[str, Any], expected: dict[str, dict[str, Any]]
) -> None:
    publication = transaction["unit_publication"]
    for role in ("vm", "proxy", "workspace", "metrics"):
        record = publication[role]
        file_item = expected.get(f"unit-file.{role}")
        stage_item = expected.get(f"unit-stage.{role}")
        manager_item = expected.get(f"unit-manager.{role}")
        if record["state"] == "not_started":
            for item in (file_item, stage_item):
                if item and item["evidence"] in {"observed_present", "removal_intent"}:
                    raise ContractError(f"{role} unit object exists while publication is not_started")
        elif record["state"] == "publication_intent":
            identity = record["stage_identity"]
            path_items = (
                (f"unit-stage.{role}", stage_item),
                (f"unit-file.{role}", file_item),
            )
            for selector, item in path_items:
                if item is not None and "identity" in item and item["identity"] != identity:
                    raise ContractError(f"{role} publication intent has a foreign identity at {selector}")
            matches = []
            for _, item in path_items:
                if item and item.get("identity") == identity and item["evidence"] == "observed_present":
                    matches.append(item)
            if len(matches) != 1:
                raise ContractError(f"{role} publication intent must reconcile exactly one observed inode")
        elif record["state"] == "published":
            identity = record["fragment_identity"]
            if file_item is None or file_item["evidence"] != "observed_present" or file_item.get("identity") != identity:
                raise ContractError(f"published {role} fragment lacks its exact final identity")
            if stage_item is not None and "identity" in stage_item:
                raise ContractError(f"published {role} fragment has a staging-object identity")
            if (
                manager_item is None
                or manager_item["evidence"] != "observed_present"
                or manager_item["identity"]["fragment_device"] != identity["device"]
                or manager_item["identity"]["fragment_inode"] != identity["inode"]
            ):
                raise ContractError(f"published {role} fragment lacks exact present manager evidence")


def _validate_remove_publication_source_consistency(
    transaction: dict[str, Any], expected: dict[str, dict[str, Any]]
) -> None:
    """Independently bind supplied remove identities to the copied source."""
    for role in ("vm", "proxy", "workspace", "metrics"):
        record = transaction["unit_publication"][role]
        file_item = expected.get(f"unit-file.{role}")
        stage_item = expected.get(f"unit-stage.{role}")
        manager_item = expected.get(f"unit-manager.{role}")
        if record["state"] == "not_started":
            if any(
                item is not None and "identity" in item
                for item in (file_item, stage_item, manager_item)
            ):
                raise ContractError(f"not_started {role} source has an identity-bearing unit object")
        elif record["state"] == "publication_intent":
            identity = record["stage_identity"]
            path_items = (file_item, stage_item)
            if sum(item is not None and "identity" in item for item in path_items) > 1:
                raise ContractError(f"{role} source has identity-bearing alternate pathnames")
            for item in path_items:
                if item is not None and "identity" in item and item["identity"] != identity:
                    raise ContractError(f"{role} supplied path identity differs from retained stage inode")
            if manager_item is not None and "identity" in manager_item:
                manager_identity = manager_item["identity"]
                if (
                    (manager_identity["fragment_device"], manager_identity["fragment_inode"])
                    != (identity["device"], identity["inode"])
                    or file_item is None
                    or file_item.get("identity") != identity
                ):
                    raise ContractError(f"{role} manager identity lacks paired exact final-file evidence")
        else:
            identity = record["fragment_identity"]
            if stage_item is not None and "identity" in stage_item:
                raise ContractError(f"published {role} source has a staging-object identity")
            if file_item is not None and "identity" in file_item and file_item["identity"] != identity:
                raise ContractError(f"published {role} final identity differs from retained fragment")
            if manager_item is not None and "identity" in manager_item:
                manager_identity = manager_item["identity"]
                if (
                    manager_identity["fragment_device"] != identity["device"]
                    or manager_identity["fragment_inode"] != identity["inode"]
                ):
                    raise ContractError(f"published {role} manager differs from retained fragment")


def _validate_remove_publication_evidence(
    transaction: dict[str, Any], expected: dict[str, dict[str, Any]]
) -> None:
    source_id = transaction["source_install_id"]
    for role in ("vm", "proxy", "workspace", "metrics"):
        record = transaction["unit_publication"][role]
        file_item = expected.get(f"unit-file.{role}")
        stage_item = expected.get(f"unit-stage.{role}")
        manager_item = expected.get(f"unit-manager.{role}")
        if record["state"] == "not_started":
            for selector, item in (
                (f"unit-file.{role}", file_item),
                (f"unit-manager.{role}", manager_item),
            ):
                if item is None or item["evidence"] != "never_created" or item["install_transaction_id"] != source_id:
                    raise ContractError(f"{selector} never-created proof lacks matching not_started source record")
            if stage_item is not None and stage_item["evidence"] != "never_created":
                raise ContractError(f"unit-stage.{role} is not authorized by a not_started source record")
        elif record["state"] == "published":
            identity = record["fragment_identity"]
            if stage_item is not None and "identity" in stage_item:
                raise ContractError(f"published {role} fragment has a staging-object identity")
            if file_item is None or file_item["evidence"] not in {"observed_present", "removal_intent"} or file_item.get("identity") != identity:
                raise ContractError(f"published {role} fragment lacks its exact final identity")
            if manager_item is None:
                raise ContractError(f"published {role} fragment lacks manager identity/reload evidence")
            if manager_item["evidence"] in {"observed_present", "removal_intent"}:
                manager_identity = manager_item.get("identity", {})
                if (manager_identity.get("fragment_device"), manager_identity.get("fragment_inode")) != (identity["device"], identity["inode"]):
                    raise ContractError(f"published {role} manager record differs from exact fragment")
            elif manager_item["evidence"] == "verified_absent":
                if file_item["evidence"] != "removal_intent":
                    raise ContractError(
                        f"published {role} manager absence requires retained durable fragment removal intent"
                    )
            else:
                raise ContractError(f"published {role} fragment lacks manager removal/reload evidence")
        else:
            identity = record["stage_identity"]
            stage_matches = stage_item is not None and stage_item["evidence"] == "removal_intent" and stage_item.get("identity") == identity
            final_matches = file_item is not None and file_item["evidence"] == "removal_intent" and file_item.get("identity") == identity
            if stage_matches and file_item is not None and "identity" in file_item:
                raise ContractError(
                    f"{role} publication_intent has an identity-bearing alternate pathname at unit-file.{role}"
                )
            if final_matches and stage_item is not None and "identity" in stage_item:
                raise ContractError(
                    f"{role} publication_intent has an identity-bearing alternate pathname at unit-stage.{role}"
                )
            if stage_matches == final_matches:
                raise ContractError(f"{role} publication_intent must reconcile exactly one stage or final inode")
            if stage_matches:
                if file_item is None or file_item["evidence"] != "verified_absent" or file_item["install_transaction_id"] != source_id:
                    raise ContractError(f"{role} staged publication lacks final-path absence proof")
                if manager_item is None or manager_item["evidence"] != "verified_absent":
                    raise ContractError(f"{role} staged publication lacks post-reload manager absence")
            elif manager_item is None or manager_item["evidence"] not in {"removal_intent", "verified_absent"}:
                raise ContractError(f"{role} final publication intent lacks manager identity/reload evidence")


def validate_install_transition(previous: dict[str, Any], current: dict[str, Any]) -> None:
    """Model one durable install update; this is not a filesystem/crash test."""
    validate_profile_semantics(previous, SCHEMA, RESOURCES)
    validate_profile_semantics(current, SCHEMA, RESOURCES)
    if previous["state"] != "installing" or current["state"] != "installing":
        raise ContractError("install transition model requires installing mappings")
    old_tx, new_tx = previous["transaction"], current["transaction"]
    if old_tx["operation"] != "install" or new_tx["operation"] != "install":
        raise ContractError("install transaction substitution")
    stable_keys = ("owner_uid", "name", "runtime", "units", "workspace", "ssh_port")
    if any(previous[key] != current[key] for key in stable_keys):
        raise ContractError("install transition changed owner/profile/name/pin or fixed mapping")
    if old_tx["id"] != new_tx["id"] or previous["installation"]["id"] != current["installation"]["id"]:
        raise ContractError("install transaction substitution")
    if new_tx["numeric_replans"][:len(old_tx["numeric_replans"])] != old_tx["numeric_replans"]:
        raise ContractError("numeric replan journal is not append-only")
    appended_replans = new_tx["numeric_replans"][len(old_tx["numeric_replans"]):]
    if new_tx["step"] < old_tx["step"]:
        raise ContractError("install phase cannot roll back")
    if new_tx["step"] > old_tx["step"] + 1:
        raise ContractError("install phase order cannot be skipped")
    old_publication = previous["installation"]["unit_publication"]
    new_publication = current["installation"]["unit_publication"]
    for role in ("vm", "proxy", "workspace", "metrics"):
        old, new = old_publication[role], new_publication[role]
        if _publication_rank(new) < _publication_rank(old):
            raise ContractError("unit publication state cannot roll back")
        if old["state"] == "publication_intent":
            old_identity = old["stage_identity"]
            new_identity = _publication_identity(new)
            if new_identity != old_identity:
                raise ContractError("publication intent must reconcile the exact staged inode")
        elif old["state"] == "published" and new != old:
            raise ContractError("published unit fragment identity is immutable")

    changed_numeric_fields: set[tuple[str, str]] = set()
    for role in ("vm", "proxy"):
        old_account = previous["accounts"][role]
        new_account = current["accounts"][role]
        if old_account["name"] != new_account["name"]:
            raise ContractError("account name is immutable during install")
        if old_account["allocation"] == "committed" and new_account != old_account:
            raise ContractError("committed account identity cannot be replaced")
        for field in ("uid", "gid"):
            if old_account[field] != new_account[field]:
                changed_numeric_fields.add((role, field))
                old_group = previous["installation"]["groups"][role]
                new_group = current["installation"]["groups"][role]
                if old_account["allocation"] != "planned":
                    raise ContractError("only unallocated planned numeric identities may be replanned")
                if old_group["state"] != "not_started" or new_group["state"] != "not_started":
                    raise ContractError("created or ambiguous group must be exactly reconciled before any replan")
                if old_account["allocation"] != new_account["allocation"]:
                    raise ContractError("numeric replan cannot commit an account")
        if previous["installation"]["groups"][role]["state"] == "owned" and current["installation"]["groups"][role]["state"] != "owned":
            raise ContractError("group provenance cannot roll back")
    if len(appended_replans) != len(changed_numeric_fields):
        raise ContractError("each planned numeric replacement needs a durable collision journal entry")
    appended_keys = [(item["role"], item["field"]) for item in appended_replans]
    if len(set(appended_keys)) != len(appended_keys) or set(appended_keys) != changed_numeric_fields:
        raise ContractError("numeric replan journal must cover each changed planned field exactly once")
    for item in appended_replans:
        key = (item["role"], item["field"])
        if key not in changed_numeric_fields:
            raise ContractError("numeric replan journal records an unchanged or unrelated identity")
        if item["previous"] != previous["accounts"][item["role"]][item["field"]]:
            raise ContractError("numeric replan journal does not match the prior planned ID")
        if item["replacement"] != current["accounts"][item["role"]][item["field"]]:
            raise ContractError("numeric replan journal does not match the replacement planned ID")
        if item["reason"] != "numeric_collision":
            raise ContractError("planned identity replacement lacks a numeric-collision reason")

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
    identity = {"device": device, "inode": inode, "kind": kind}
    if kind != "directory":
        identity["link_count"] = 1
    return identity


def _account_identity(owner_uid: int, name: str, role: str, uid: int, gid: int) -> dict[str, Any]:
    return {
        "uid": uid,
        "gid": gid,
        "marker": expected_account_marker(owner_uid, name, role),
    }


def _group_identity(
    owner_uid: int, name: str, role: str, gid: int, install_id: str
) -> dict[str, Any]:
    group_name = f"{'lmv' if role == 'vm' else 'lmp'}-{digest_for(owner_uid, name)}"
    return {
        "name": group_name,
        "gid": gid,
        "provenance": expected_group_provenance(owner_uid, name, role, gid, install_id),
    }


def _group_record(
    owner_uid: int, name: str, role: str, gid: int, install_id: str, state: str
) -> dict[str, Any]:
    identity = _group_identity(owner_uid, name, role, gid, install_id)
    return {"name": identity["name"], "gid": gid, "state": state, "provenance": identity["provenance"]}


def _published_units() -> dict[str, Any]:
    return {
        role: {"state": "published", "fragment_identity": _file_identity("regular", 8, 701 + index)}
        for index, role in enumerate(("vm", "proxy", "workspace", "metrics"))
    }


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
    install_id = "0" * 32
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
        "installation": {
            "id": install_id,
            "unit_publication": _published_units(),
            "groups": {
                "vm": _group_record(owner_uid, name, "vm", 995, install_id, "owned"),
                "proxy": _group_record(owner_uid, name, "proxy", 996, install_id, "owned"),
            },
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
            "valid-installing-directory-nlink-2.json",
            "valid-installing-partial-publication.json",
            "valid-installing-verified.json",
            "valid-removing-partial-not-started.json",
            "valid-removing-partial-stage.json",
            "valid-removing-partial-final.json",
            "valid-removing-published-reload.json",
            "valid-removing-prepared-published.json",
            "valid-removing-prepared-partial-stage.json",
            "valid-removing-accounts-committed-intent.json",
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

    def test_raw_schema_checks_are_separate_from_semantic_model_checks(self) -> None:
        for path in sorted(FIXTURE_DIR.glob("valid-*.json")):
            with self.subTest(fixture=path.name):
                validate_profile_schema(self.read_fixture(path.name), SCHEMA)
        # This is well-typed JSON but fails a semantic link between identity and provenance.
        typed_but_mismatched = self.read_fixture("invalid-group-provenance-gid.json")
        validate_profile_schema(typed_but_mismatched, SCHEMA)
        with self.assertRaisesRegex(ContractError, "provenance"):
            validate_profile_semantics(typed_but_mismatched, SCHEMA, RESOURCES)

        semantic_negatives = {
            "invalid-removing-prepared-foreign-final-manager.json": "differs from retained fragment",
            "invalid-removing-prepared-published-stage.json": "staging-object identity",
            "invalid-removing-accounts-planned-intent.json": "only planned",
            "invalid-installing-verified-missing-manager.json": "present manager evidence",
            "invalid-installing-verified-absent-manager.json": "present manager evidence",
            "invalid-resetting-incomplete-source.json": "retained committed accounts",
        }
        for filename, message in semantic_negatives.items():
            with self.subTest(fixture=filename):
                invalid = self.read_fixture(filename)
                validate_profile_schema(invalid, SCHEMA)
                with self.assertRaisesRegex(ContractError, message):
                    validate_profile_semantics(invalid, SCHEMA, RESOURCES)

    def test_directory_identity_is_disjoint_from_single_link_file_and_socket_identity(self) -> None:
        directory_case = self.read_fixture("directory-nlink-2.json")
        self.assertGreaterEqual(directory_case["observed_st_nlink"], 2)
        validate_declarative_subset(directory_case["identity"], SCHEMA["$defs"]["fileIdentity"], SCHEMA)
        realistic = self.read_fixture("valid-installing-directory-nlink-2.json")
        directory_identity = next(
            item["identity"] for item in realistic["transaction"]["expected"]
            if item["selector"] == "state.root"
        )
        # The fixture represents a directory with a child (Linux st_nlink >= 2); nlink is not stored.
        self.assertEqual(directory_identity, {"device": 8, "inode": 1110, "kind": "directory"})
        self.assert_profile_valid(realistic)
        with self.assertRaises(ContractError):
            validate_profile_schema(self.read_fixture("invalid-directory-link-count.json"), SCHEMA)
        for kind in ("regular", "socket"):
            validate_declarative_subset(_file_identity(kind), SCHEMA["$defs"]["fileIdentity"], SCHEMA)
        invalid_file = _file_identity("regular")
        invalid_file["link_count"] = 2
        with self.assertRaises(ContractError):
            validate_declarative_subset(invalid_file, SCHEMA["$defs"]["fileIdentity"], SCHEMA)

    def test_account_marker_and_group_provenance_are_exact_but_not_standalone_proof(self) -> None:
        profile = self.read_fixture("valid-installing.json")
        account = next(item for item in profile["transaction"]["expected"] if item["selector"] == "account.vm")
        self.assertEqual(account["identity"]["marker"], "little-mester-vm-profile-v1/1000/default/vm")
        with self.assertRaises(ContractError):
            validate_profile_schema(self.read_fixture("invalid-account-marker-colon.json"), SCHEMA)
        for filename in ("invalid-group-provenance-gid.json", "invalid-group-provenance-origin.json"):
            with self.subTest(fixture=filename), self.assertRaises(ContractError):
                self.assert_profile_valid(self.read_fixture(filename))
        fake_marker = copy.deepcopy(profile)
        group = next(item for item in fake_marker["transaction"]["expected"] if item["selector"] == "group.vm")
        group["identity"]["marker"] = "little-mester-vm-profile-v1/1000/default/vm/group"
        with self.assertRaises(ContractError):
            validate_profile_schema(fake_marker, SCHEMA)

    def test_install_transition_model_allows_only_unallocated_planned_id_replan(self) -> None:
        previous = self.read_fixture("valid-installing.json")
        replanned = copy.deepcopy(previous)
        account = replanned["accounts"]["proxy"]
        account["uid"], account["gid"] = 1200, 1201
        replanned["transaction"]["numeric_replans"] = [
            {"role": "proxy", "field": "uid", "previous": 996, "replacement": 1200, "reason": "numeric_collision"},
            {"role": "proxy", "field": "gid", "previous": 996, "replacement": 1201, "reason": "numeric_collision"},
        ]
        group = replanned["installation"]["groups"]["proxy"]
        group["gid"] = 1201
        group["provenance"] = expected_group_provenance(1000, "default", "proxy", 1201, replanned["installation"]["id"])
        validate_install_transition(previous, replanned)
        unjournaled = copy.deepcopy(replanned)
        unjournaled["transaction"]["numeric_replans"] = []
        with self.assertRaisesRegex(ContractError, "durable collision journal"):
            validate_install_transition(previous, unjournaled)
        self.assertEqual(replanned["transaction"]["id"], previous["transaction"]["id"])
        self.assertEqual(replanned["runtime"], previous["runtime"])
        self.assertEqual(replanned["accounts"]["proxy"]["name"], previous["accounts"]["proxy"]["name"])

        committed_replacement = copy.deepcopy(previous)
        committed_replacement["accounts"]["vm"]["uid"] = 1202
        committed_replacement["accounts"]["vm"]["gid"] = 1203
        committed_replacement["transaction"]["numeric_replans"] = [
            {"role": "vm", "field": "uid", "previous": 995, "replacement": 1202, "reason": "numeric_collision"},
            {"role": "vm", "field": "gid", "previous": 995, "replacement": 1203, "reason": "numeric_collision"},
        ]
        committed_replacement["installation"]["groups"]["vm"]["gid"] = 1203
        committed_replacement["installation"]["groups"]["vm"]["provenance"] = expected_group_provenance(1000, "default", "vm", 1203, previous["installation"]["id"])
        vm_group = next(item for item in committed_replacement["transaction"]["expected"] if item["selector"] == "group.vm")
        vm_group["identity"] = _group_identity(1000, "default", "vm", 1203, previous["installation"]["id"])
        vm_account = next(item for item in committed_replacement["transaction"]["expected"] if item["selector"] == "account.vm")
        vm_account["identity"]["uid"], vm_account["identity"]["gid"] = 1202, 1203
        with self.assertRaisesRegex(ContractError, "committed"):
            validate_install_transition(previous, committed_replacement)

        created_group = copy.deepcopy(previous)
        created_group["installation"]["groups"]["proxy"]["state"] = "owned"
        created_group["transaction"]["expected"][3] = {
            "selector": "group.proxy", "evidence": "observed_present",
            "identity": _group_identity(1000, "default", "proxy", 996, previous["installation"]["id"]),
        }
        group_replacement = copy.deepcopy(created_group)
        group_replacement["accounts"]["proxy"]["gid"] = 1201
        group_replacement["transaction"]["numeric_replans"] = [
            {"role": "proxy", "field": "gid", "previous": 996, "replacement": 1201, "reason": "numeric_collision"},
        ]
        group_replacement["installation"]["groups"]["proxy"]["gid"] = 1201
        group_replacement["installation"]["groups"]["proxy"]["provenance"] = expected_group_provenance(1000, "default", "proxy", 1201, previous["installation"]["id"])
        group_replacement["transaction"]["expected"][3]["identity"] = _group_identity(1000, "default", "proxy", 1201, previous["installation"]["id"])
        with self.assertRaisesRegex(ContractError, "group"):
            validate_install_transition(created_group, group_replacement)

        substituted = copy.deepcopy(replanned)
        substituted["transaction"]["id"] = "9" * 32
        substituted["installation"]["id"] = "9" * 32
        for role in ("vm", "proxy"):
            group = substituted["installation"]["groups"][role]
            group["provenance"]["origin_install_transaction_id"] = "9" * 32
        vm_group = next(item for item in substituted["transaction"]["expected"] if item["selector"] == "group.vm")
        vm_group["identity"]["provenance"]["origin_install_transaction_id"] = "9" * 32
        with self.assertRaisesRegex(ContractError, "transaction substitution"):
            validate_install_transition(previous, substituted)

        ambiguous = copy.deepcopy(previous)
        ambiguous["installation"]["groups"]["proxy"]["state"] = "creation_intent"
        ambiguous["transaction"]["expected"][3] = {
            "selector": "group.proxy", "evidence": "creation_intent",
            "identity": _group_identity(1000, "default", "proxy", 996, previous["installation"]["id"]),
        }
        with self.assertRaisesRegex(ContractError, "ambiguous interrupted"):
            validate_install_transition(ambiguous, replanned)

    def test_unit_publication_states_never_roll_back_and_reconcile_exact_inodes(self) -> None:
        before = self.read_fixture("valid-installing-partial-publication.json")
        after = copy.deepcopy(before)
        identity = copy.deepcopy(after["installation"]["unit_publication"]["proxy"]["stage_identity"])
        published = {"state": "published", "fragment_identity": identity}
        after["installation"]["unit_publication"]["proxy"] = copy.deepcopy(published)
        after["transaction"]["unit_publication"]["proxy"] = copy.deepcopy(published)
        after["transaction"]["expected"] = [item for item in after["transaction"]["expected"] if item["selector"] != "unit-stage.proxy"]
        after["transaction"]["expected"].append({"selector": "unit-file.proxy", "evidence": "observed_present", "identity": identity})
        after["transaction"]["expected"].append({
            "selector": "unit-manager.proxy", "evidence": "observed_present",
            "identity": _manager_identity(identity["device"], identity["inode"]),
        })
        validate_install_transition(before, after)

        skipped_phase = copy.deepcopy(after)
        skipped_phase["transaction"]["phase"] = "verified"
        skipped_phase["transaction"]["step"] = 4
        skipped_phase["installation"]["unit_publication"] = {
            role: {"state": "published", "fragment_identity": _file_identity("regular", 8, 701 + index)}
            for index, role in enumerate(("vm", "proxy", "workspace", "metrics"))
        }
        skipped_phase["transaction"]["unit_publication"] = copy.deepcopy(skipped_phase["installation"]["unit_publication"])
        skipped_phase["transaction"]["expected"] = []
        for index, role in enumerate(("vm", "proxy", "workspace", "metrics")):
            inode = 701 + index
            skipped_phase["transaction"]["expected"].extend([
                {"selector": f"unit-file.{role}", "evidence": "observed_present", "identity": _file_identity("regular", 8, inode)},
                {"selector": f"unit-manager.{role}", "evidence": "observed_present", "identity": _manager_identity(8, inode)},
            ])
        previous_accounts = self.read_fixture("valid-installing.json")
        with self.assertRaisesRegex(ContractError, "phase order"):
            validate_install_transition(previous_accounts, skipped_phase)

        rolled_back = copy.deepcopy(before)
        rolled_back["installation"]["unit_publication"]["proxy"] = {"state": "not_started"}
        rolled_back["transaction"]["unit_publication"]["proxy"] = {"state": "not_started"}
        rolled_back["transaction"]["expected"] = [item for item in rolled_back["transaction"]["expected"] if item["selector"] != "unit-stage.proxy"]
        with self.assertRaisesRegex(ContractError, "roll back"):
            validate_install_transition(before, rolled_back)

        wrong_inode = self.read_fixture("valid-removing-partial-stage.json")
        stage = next(item for item in wrong_inode["transaction"]["expected"] if item["selector"] == "unit-stage.proxy")
        stage["identity"]["inode"] += 1
        with self.assertRaisesRegex(ContractError, "differs from retained stage inode"):
            self.assert_profile_valid(wrong_inode)

        wrong_source = self.read_fixture("valid-removing-partial-not-started.json")
        wrong_source["transaction"]["expected"][0]["install_transaction_id"] = "9" * 32
        with self.assertRaisesRegex(ContractError, "source record"):
            self.assert_profile_valid(wrong_source)

    def test_publication_path_evidence_rejects_conflicts_and_requires_install_inode(self) -> None:
        fixture_errors = {
            "invalid-installing-partial-foreign-final.json": "foreign identity",
            "invalid-installing-partial-foreign-stage.json": "foreign identity",
            "invalid-installing-partial-both-present.json": "exactly one observed inode",
            "invalid-installing-partial-missing-inode.json": "exactly one observed inode",
            "invalid-removing-partial-final-foreign-stage.json": "identity-bearing alternate pathname",
            "invalid-removing-partial-both-intents.json": "identity-bearing alternate pathname",
            "invalid-removing-published-reload-without-fragment-intent.json": "retained durable fragment removal intent",
        }
        for filename, message in fixture_errors.items():
            with self.subTest(fixture=filename):
                fixture = self.read_fixture(filename)
                validate_profile_schema(fixture, SCHEMA)
                with self.assertRaisesRegex(ContractError, message):
                    validate_profile_semantics(fixture, SCHEMA, RESOURCES)

        # An installing intent may reconcile at either exact pathname, but an
        # omitted alternate selector is not itself evidence of filesystem absence.
        valid_stage = self.read_fixture("valid-installing-partial-publication.json")
        validate_profile_semantics(valid_stage, SCHEMA, RESOURCES)
        valid_final = copy.deepcopy(valid_stage)
        stage_entry = next(
            item for item in valid_final["transaction"]["expected"]
            if item["selector"] == "unit-stage.proxy"
        )
        stage_entry["selector"] = "unit-file.proxy"
        validate_profile_schema(valid_final, SCHEMA)
        validate_profile_semantics(valid_final, SCHEMA, RESOURCES)

        # Check both removal pathname directions with both identity-bearing
        # alternate evidence forms: foreign observed identity and same-inode
        # removal intent. Neither may be hidden by one matching primary path.
        alternate_cases = []
        stage_primary = self.read_fixture("valid-removing-partial-stage.json")
        stage_identity = copy.deepcopy(
            next(item for item in stage_primary["transaction"]["expected"]
                 if item["selector"] == "unit-stage.proxy")["identity"]
        )
        foreign_file = copy.deepcopy(stage_primary)
        foreign_file_entry = next(
            item for item in foreign_file["transaction"]["expected"]
            if item["selector"] == "unit-file.proxy"
        )
        foreign_file_entry.clear()
        foreign_file_entry.update({
            "selector": "unit-file.proxy",
            "evidence": "observed_present",
            "identity": _file_identity("regular", 8, stage_identity["inode"] + 10),
        })
        alternate_cases.append(("stage-to-final-observed", foreign_file))

        same_inode_file = copy.deepcopy(stage_primary)
        same_inode_entry = next(
            item for item in same_inode_file["transaction"]["expected"]
            if item["selector"] == "unit-file.proxy"
        )
        same_inode_entry.clear()
        same_inode_entry.update({
            "selector": "unit-file.proxy",
            "evidence": "removal_intent",
            "identity": stage_identity,
        })
        alternate_cases.append(("stage-to-final-removal-intent", same_inode_file))

        final_primary = self.read_fixture("valid-removing-partial-final.json")
        final_identity = copy.deepcopy(
            next(item for item in final_primary["transaction"]["expected"]
                 if item["selector"] == "unit-file.proxy")["identity"]
        )
        foreign_stage = copy.deepcopy(final_primary)
        foreign_stage["transaction"]["expected"].append({
            "selector": "unit-stage.proxy",
            "evidence": "observed_present",
            "identity": _file_identity("regular", 8, final_identity["inode"] + 10),
        })
        alternate_cases.append(("final-to-stage-observed", foreign_stage))

        same_inode_stage = copy.deepcopy(final_primary)
        same_inode_stage["transaction"]["expected"].append({
            "selector": "unit-stage.proxy",
            "evidence": "removal_intent",
            "identity": final_identity,
        })
        alternate_cases.append(("final-to-stage-removal-intent", same_inode_stage))

        for name, profile in alternate_cases:
            with self.subTest(mutation=name):
                validate_profile_schema(profile, SCHEMA)
                with self.assertRaisesRegex(ContractError, "identity-bearing alternate pathname"):
                    validate_profile_semantics(profile, SCHEMA, RESOURCES)

        published_stage_identity = copy.deepcopy(
            self.read_fixture("valid-removing-published-reload.json")
        )
        published_stage_identity["transaction"]["expected"].append({
            "selector": "unit-stage.vm",
            "evidence": "removal_intent",
            "identity": copy.deepcopy(
                next(item for item in published_stage_identity["transaction"]["expected"]
                     if item["selector"] == "unit-file.vm")["identity"]
            ),
        })
        validate_profile_schema(published_stage_identity, SCHEMA)
        with self.assertRaisesRegex(ContractError, "staging-object identity"):
            validate_profile_semantics(published_stage_identity, SCHEMA, RESOURCES)

    def test_install_published_roles_require_present_exact_manager_evidence(self) -> None:
        roles = ("vm", "proxy", "workspace", "metrics")
        for phase, step in (("units", 3), ("verified", 4)):
            for role in roles:
                for mutation in ("missing", "absent", "foreign"):
                    profile = self.read_fixture("valid-installing-verified.json")
                    profile["transaction"]["phase"] = phase
                    profile["transaction"]["step"] = step
                    if phase == "units":
                        publication = profile["installation"]["unit_publication"]
                        for index, published_role in enumerate(roles):
                            if index > roles.index(role):
                                publication[published_role] = {"state": "not_started"}
                        profile["transaction"]["unit_publication"] = copy.deepcopy(publication)
                        profile["transaction"]["expected"] = [
                            item for item in profile["transaction"]["expected"]
                            if item["selector"].startswith((
                                "unit-file.", "unit-manager."
                            ))
                            and item["selector"].rsplit(".", 1)[1]
                            in roles[: roles.index(role) + 1]
                        ]
                    manager = next(
                        item for item in profile["transaction"]["expected"]
                        if item["selector"] == f"unit-manager.{role}"
                    )
                    if mutation == "missing":
                        profile["transaction"]["expected"].remove(manager)
                    elif mutation == "absent":
                        manager.clear()
                        manager.update({"selector": f"unit-manager.{role}", "evidence": "expected_absent"})
                    else:
                        manager["identity"]["fragment_inode"] += 100
                    with self.subTest(phase=phase, role=role, mutation=mutation):
                        validate_profile_schema(profile, SCHEMA)
                        with self.assertRaises(ContractError):
                            validate_profile_semantics(profile, SCHEMA, RESOURCES)

    def test_reset_source_is_complete_in_each_reset_phase(self) -> None:
        for phase, step in (("prepared", 0), ("leaves", 1), ("verified", 2)):
            for category, roles in (
                ("account", ("vm", "proxy")),
                ("group", ("vm", "proxy")),
                ("publication", ("vm", "proxy", "workspace", "metrics")),
            ):
                for role in roles:
                    profile = self.read_fixture("valid-resetting.json")
                    transaction = profile["transaction"]
                    transaction["phase"] = phase
                    transaction["step"] = step
                    if phase == "prepared":
                        transaction["expected"] = []
                    if category == "account":
                        profile["accounts"][role]["allocation"] = "planned"
                    elif category == "group":
                        profile["installation"]["groups"][role]["state"] = "not_started"
                    else:
                        profile["installation"]["unit_publication"][role] = {"state": "not_started"}
                    with self.subTest(phase=phase, category=category, role=role):
                        validate_profile_schema(profile, SCHEMA)
                        with self.assertRaises(ContractError):
                            validate_profile_semantics(profile, SCHEMA, RESOURCES)

    def test_account_removal_intents_need_committed_account_and_owned_group_source(self) -> None:
        def put_expected(profile: dict[str, Any], selector: str, item: dict[str, Any] | None) -> None:
            expected = profile["transaction"]["expected"]
            expected[:] = [entry for entry in expected if entry["selector"] != selector]
            if item is not None:
                expected.append(item)

        for phase, step in (("accounts", 3), ("finalizing", 4)):
            for role in ("vm", "proxy"):
                base = self.read_fixture("valid-removing.json")
                base["transaction"]["phase"] = phase
                base["transaction"]["step"] = step
                account = base["accounts"][role]
                intent = {
                    "selector": f"account.{role}",
                    "evidence": "removal_intent",
                    "identity": _account_identity(1000, "default", role, account["uid"], account["gid"]),
                }

                # No group expected-object is required: the owned, exact
                # protected primary-group record is retained in the mapping.
                put_expected(base, f"account.{role}", copy.deepcopy(intent))
                with self.subTest(phase=phase, role=role, case="no-group-evidence"):
                    self.assertNotIn(
                        f"group.{role}",
                        {item["selector"] for item in base["transaction"]["expected"]},
                    )
                    validate_profile_schema(base, SCHEMA)
                    validate_profile_semantics(base, SCHEMA, RESOURCES)

                matching_group = _group_identity(
                    1000, "default", role, account["gid"], base["installation"]["id"]
                )
                with_group = copy.deepcopy(base)
                put_expected(with_group, f"group.{role}", {
                    "selector": f"group.{role}", "evidence": "observed_present",
                    "identity": copy.deepcopy(matching_group),
                })
                with self.subTest(phase=phase, role=role, case="matching-group-evidence"):
                    validate_profile_schema(with_group, SCHEMA)
                    validate_profile_semantics(with_group, SCHEMA, RESOURCES)

                planned = copy.deepcopy(base)
                planned["accounts"][role]["allocation"] = "planned"
                with self.subTest(phase=phase, role=role, case="planned-account"):
                    validate_profile_schema(planned, SCHEMA)
                    with self.assertRaisesRegex(ContractError, "only planned"):
                        validate_profile_semantics(planned, SCHEMA, RESOURCES)

                planned_observation = copy.deepcopy(planned)
                next(
                    item for item in planned_observation["transaction"]["expected"]
                    if item["selector"] == f"account.{role}"
                )["evidence"] = "observed_present"
                with self.subTest(phase=phase, role=role, case="planned-observed-present"):
                    validate_profile_schema(planned_observation, SCHEMA)
                    with self.assertRaisesRegex(ContractError, "only planned"):
                        validate_profile_semantics(planned_observation, SCHEMA, RESOURCES)

                unowned_group = copy.deepcopy(base)
                unowned_group["installation"]["groups"][role]["state"] = "not_started"
                with self.subTest(phase=phase, role=role, case="missing-owned-group-source"):
                    validate_profile_schema(unowned_group, SCHEMA)
                    with self.assertRaisesRegex(ContractError, "owned primary-group provenance"):
                        validate_profile_semantics(unowned_group, SCHEMA, RESOURCES)

                mismatched_group = copy.deepcopy(base)
                wrong_identity = copy.deepcopy(matching_group)
                wrong_identity["provenance"]["origin_install_transaction_id"] = "9" * 32
                put_expected(mismatched_group, f"group.{role}", {
                    "selector": f"group.{role}", "evidence": "observed_present",
                    "identity": wrong_identity,
                })
                with self.subTest(phase=phase, role=role, case="mismatched-group-provenance"):
                    validate_profile_schema(mismatched_group, SCHEMA)
                    with self.assertRaisesRegex(ContractError, "differs from protected mapping"):
                        validate_profile_semantics(mismatched_group, SCHEMA, RESOURCES)

    def test_remove_source_identity_consistency_applies_to_every_phase_and_state(self) -> None:
        roles = ("vm", "proxy", "workspace", "metrics")
        phases = (("prepared", 0), ("units", 1), ("state", 2), ("accounts", 3), ("finalizing", 4))

        def set_expected(profile: dict[str, Any], role: str, items: list[dict[str, Any]]) -> None:
            expected = profile["transaction"]["expected"]
            selectors = {f"unit-file.{role}", f"unit-stage.{role}", f"unit-manager.{role}"}
            expected[:] = [item for item in expected if item["selector"] not in selectors]
            expected.extend(copy.deepcopy(items))

        def make_case(phase: str, step: int, role: str, state: str) -> tuple[dict[str, Any], dict[str, Any]]:
            profile = self.read_fixture("valid-removing.json")
            transaction = profile["transaction"]
            transaction["phase"] = phase
            transaction["step"] = step
            if phase == "prepared":
                transaction["expected"] = []
            index = roles.index(role)
            if state == "not_started":
                record = {"state": "not_started"}
            elif state == "publication_intent":
                record = {"state": "publication_intent", "stage_identity": _file_identity("regular", 9, 850 + index)}
            else:
                record = copy.deepcopy(profile["installation"]["unit_publication"][role])
            profile["installation"]["unit_publication"][role] = copy.deepcopy(record)
            transaction["unit_publication"][role] = copy.deepcopy(record)
            identity = record.get("stage_identity", record.get("fragment_identity", _file_identity("regular", 9, 950 + index)))
            return profile, identity

        for phase, step in phases:
            for role in roles:
                for state in ("not_started", "publication_intent", "published"):
                    profile, retained = make_case(phase, step, role, state)
                    foreign = _file_identity("regular", retained["device"], retained["inode"] + 100)
                    cases = {
                        "foreign-file": [{
                            "selector": f"unit-file.{role}", "evidence": "observed_present", "identity": foreign,
                        }],
                        "foreign-stage": [{
                            "selector": f"unit-stage.{role}", "evidence": "observed_present", "identity": foreign,
                        }],
                        "foreign-manager": [
                            {"selector": f"unit-file.{role}", "evidence": "observed_present",
                             "identity": foreign if state == "not_started" else copy.deepcopy(retained)},
                            {"selector": f"unit-manager.{role}", "evidence": "observed_present",
                             "identity": _manager_identity(foreign["device"], foreign["inode"])},
                        ],
                        "paired-foreign-final-manager": [
                            {"selector": f"unit-file.{role}", "evidence": "observed_present", "identity": foreign},
                            {"selector": f"unit-manager.{role}", "evidence": "observed_present", "identity": _manager_identity(foreign["device"], foreign["inode"])},
                        ],
                    }
                    for case, supplied in cases.items():
                        candidate = copy.deepcopy(profile)
                        set_expected(candidate, role, supplied)
                        with self.subTest(phase=phase, role=role, state=state, case=case):
                            validate_profile_schema(candidate, SCHEMA)
                            with self.assertRaises(ContractError):
                                validate_profile_semantics(candidate, SCHEMA, RESOURCES)

                    if state == "publication_intent":
                        alternate = copy.deepcopy(profile)
                        set_expected(alternate, role, [
                            {"selector": f"unit-stage.{role}", "evidence": "observed_present", "identity": copy.deepcopy(retained)},
                            {"selector": f"unit-file.{role}", "evidence": "observed_present", "identity": copy.deepcopy(retained)},
                        ])
                        with self.subTest(phase=phase, role=role, state=state, case="both-identity-bearing-alternates"):
                            validate_profile_schema(alternate, SCHEMA)
                            with self.assertRaises(ContractError):
                                validate_profile_semantics(alternate, SCHEMA, RESOURCES)

                    if state == "published":
                        stage = copy.deepcopy(profile)
                        set_expected(stage, role, [{
                            "selector": f"unit-stage.{role}", "evidence": "observed_present", "identity": copy.deepcopy(retained),
                        }])
                        with self.subTest(phase=phase, role=role, state=state, case="published-stage-identity"):
                            validate_profile_schema(stage, SCHEMA)
                            with self.assertRaises(ContractError):
                                validate_profile_semantics(stage, SCHEMA, RESOURCES)

        # A source-consistent sparse prepared record is valid without implying
        # that omitted stage/final/manager selectors were observed absent.
        for fixture in (
            "valid-removing-prepared-published.json",
            "valid-removing-prepared-partial-stage.json",
            "valid-removing-partial-not-started.json",
            "valid-removing-partial-stage.json",
            "valid-removing-partial-final.json",
            "valid-removing-published-reload.json",
        ):
            with self.subTest(valid_fixture=fixture):
                self.assert_profile_valid(self.read_fixture(fixture))

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
                "runtime_roles", "refuse_not_selectors", "unit_publication",
                "planned_id_replans",
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
        self.assertNotIn("group_marker", RESOURCES["account_roles"])
        self.assertIn("GECOS account comment; identification only", RESOURCES["account_roles"]["vm"]["marker"])
        self.assertIn("/etc/group has no marker/comment field", RESOURCES["account_roles"]["group_provenance"])
        self.assertEqual(
            set(defs["managerSelector"]["enum"]),
            {"unit-manager.vm", "unit-manager.proxy", "unit-manager.workspace", "unit-manager.metrics"},
        )
        publication = RESOURCES["unit_publication"]
        self.assertEqual(publication["roles"], ["vm", "proxy", "workspace", "metrics"])
        self.assertEqual(publication["states"], ["not_started", "publication_intent", "published"])
        self.assertIn("before any unit staging/writes", publication["initialization"])
        self.assertIn("States never roll back", publication["recovery"])
        replans = RESOURCES["planned_id_replans"]
        self.assertEqual(replans["maximum_entries"], 8)
        self.assertEqual(replans["entry_fields"], ["role", "field", "previous", "replacement", "reason"])
        self.assertEqual(replans["reason"], "numeric_collision")
        self.assertIn("Committed IDs never change", replans["policy"])
        self.assertIn("verified_absent", RESOURCES["evidence"])
        self.assertIn("daemon_reload=completed", RESOURCES["evidence"]["verified_absent"])
        for role in publication["roles"]:
            stage = by_selector[f"unit-stage.{role}"]
            final = by_selector[f"unit-file.{role}"]
            self.assertEqual(stage["kind"], "regular-file")
            self.assertEqual(Path(stage["path_template"]).parent, Path(final["path_template"]).parent)
            self.assertIn(role, stage["path_template"])
            self.assertIn("<digest>", stage["path_template"])
            self.assertIn("<install-transaction-id>", stage["path_template"])
            self.assertNotIn("*", stage["path_template"])
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
            "source_install_id": removal_absence["installation"]["id"],
            "unit_publication": copy.deepcopy(removal_absence["installation"]["unit_publication"]),
            "expected": [{"selector": "unit-file.vm", "evidence": "expected_absent"}],
        }
        with self.assertRaisesRegex(ContractError, "unit absence"):
            self.assert_profile_valid(removal_absence)

        never_created = self.read_fixture("valid-removing-partial-not-started.json")
        self.assert_profile_valid(never_created)
        ambiguous_never_created = copy.deepcopy(never_created)
        ambiguous_never_created["installation"]["unit_publication"]["vm"] = {
            "state": "publication_intent",
            "stage_identity": _file_identity("regular", 8, 801),
        }
        ambiguous_never_created["transaction"]["unit_publication"]["vm"] = copy.deepcopy(
            ambiguous_never_created["installation"]["unit_publication"]["vm"]
        )
        with self.assertRaisesRegex(ContractError, "not_started"):
            self.assert_profile_valid(ambiguous_never_created)

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
