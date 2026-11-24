#!/usr/bin/env python3
"""Side-effect-free parser and semantic validator for protected profile v1 bytes.

This module deliberately does not open a mapping, authorize an owner, inspect a
host identity, or grant permission to act on any returned value.  A future
protected reader must establish those properties before calling this parser.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any


class MappingRefused(RuntimeError):
    """A bounded, metadata-free refusal to parse or validate mapping bytes."""


MAX_MAPPING_BYTES = 262144
MAX_NESTING = 16
MAX_IDENTIFIER = 2147483647
MAX_EXPECTED_OBJECTS = 64
MAX_NUMERIC_REPLANS = 8
MAX_DEVICE_OR_INODE = 9223372036854775807
MAX_MOUNT_COMPONENT = 4294967295

RESERVED_NAMES = frozenset(
    {
        "system",
        "profiles",
        "installations",
        "prune",
        "bootstrap",
        "control",
        "prepare-dependencies",
    }
)

UNIT_ROLES = ("vm", "proxy", "workspace", "metrics")
ACCOUNT_ROLES = ("vm", "proxy")

# Numeric limits are hard-coded contract data, not loaded from a schema file.
# The test suite cross-checks each entry against the checked-in JSON Schema.
NUMERIC_BOUNDS = MappingProxyType(
    {
        "schema_version": (1, 1),
        "owner_uid": (1, MAX_IDENTIFIER),
        "uid_or_gid": (1, MAX_IDENTIFIER),
        "runtime_api": (1, 65535),
        "workspace_device": (0, MAX_DEVICE_OR_INODE),
        "workspace_inode": (1, MAX_DEVICE_OR_INODE),
        "mount_id": (1, MAX_IDENTIFIER),
        "mount_component": (0, MAX_MOUNT_COMPONENT),
        "ssh_port": (22223, 23222),
        "transaction_step": (0, 4),
        "object_device": (0, MAX_DEVICE_OR_INODE),
        "object_inode": (1, MAX_DEVICE_OR_INODE),
        "link_count": (1, 1),
    }
)

STRING_BOUNDS = MappingProxyType(
    {
        "profile_name": (1, 32),
        "sha": (40, 64),
        "account_name": (24, 24),
        "unit_name": (1, 128),
        "absolute_path": (1, 4096),
        "transaction_id": (32, 32),
        "account_marker": (1, 128),
    }
)

RESOURCE_KINDS = MappingProxyType(
    {
        "profile.config-dir": "directory",
        "profile.mapping": "regular-file",
        "state.root": "directory",
        "state.vm-dir": "directory",
        "state.client-dir": "directory",
        "state.vm.disk": "regular-file",
        "state.vm.seed": "regular-file",
        "state.vm.host-key-private": "regular-file",
        "state.vm.host-key-public": "regular-file",
        "state.client.control-key-private": "regular-file",
        "state.client.control-key-public": "regular-file",
        "state.client.known-hosts": "regular-file",
        "unit-file.vm": "regular-file",
        "unit-file.proxy": "regular-file",
        "unit-file.workspace": "regular-file",
        "unit-file.metrics": "regular-file",
        "unit-stage.vm": "regular-file",
        "unit-stage.proxy": "regular-file",
        "unit-stage.workspace": "regular-file",
        "unit-stage.metrics": "regular-file",
        "unit-manager.vm": "unit-manager-identity",
        "unit-manager.proxy": "unit-manager-identity",
        "unit-manager.workspace": "unit-manager-identity",
        "unit-manager.metrics": "unit-manager-identity",
        "account.vm": "host-account-identity",
        "group.vm": "host-group-identity",
        "account.proxy": "host-account-identity",
        "group.proxy": "host-group-identity",
        "runtime.root": "directory",
        "runtime.vm-dir": "directory",
        "runtime.vm.qmp-socket": "socket",
        "runtime.proxy-dir": "directory",
        "runtime.proxy.port": "regular-file",
        "runtime.metrics-dir": "directory",
        "runtime.metrics.port": "regular-file",
        "runtime.workspace-dir": "directory",
        "runtime.workspace.virtiofs-socket": "socket",
    }
)

TRANSACTION_ALLOWLISTS = MappingProxyType(
    {
        "install": (
            "profile.config-dir",
            "state.root",
            "state.vm-dir",
            "state.client-dir",
            "account.vm",
            "group.vm",
            "account.proxy",
            "group.proxy",
            "unit-stage.vm",
            "unit-stage.proxy",
            "unit-stage.workspace",
            "unit-stage.metrics",
            "unit-file.vm",
            "unit-file.proxy",
            "unit-file.workspace",
            "unit-file.metrics",
            "unit-manager.vm",
            "unit-manager.proxy",
            "unit-manager.workspace",
            "unit-manager.metrics",
        ),
        "reset": (
            "state.vm.disk",
            "state.vm.seed",
            "state.vm.host-key-private",
            "state.vm.host-key-public",
            "state.client.control-key-private",
            "state.client.control-key-public",
            "state.client.known-hosts",
        ),
        "remove": (
            "profile.config-dir",
            "state.root",
            "state.vm-dir",
            "state.client-dir",
            "state.vm.disk",
            "state.vm.seed",
            "state.vm.host-key-private",
            "state.vm.host-key-public",
            "state.client.control-key-private",
            "state.client.control-key-public",
            "state.client.known-hosts",
            "unit-file.vm",
            "unit-file.proxy",
            "unit-file.workspace",
            "unit-file.metrics",
            "unit-stage.vm",
            "unit-stage.proxy",
            "unit-stage.workspace",
            "unit-stage.metrics",
            "unit-manager.vm",
            "unit-manager.proxy",
            "unit-manager.workspace",
            "unit-manager.metrics",
            "account.vm",
            "group.vm",
            "account.proxy",
            "group.proxy",
            "runtime.root",
            "runtime.vm-dir",
            "runtime.vm.qmp-socket",
            "runtime.proxy-dir",
            "runtime.proxy.port",
            "runtime.metrics-dir",
            "runtime.metrics.port",
            "runtime.workspace-dir",
            "runtime.workspace.virtiofs-socket",
        ),
    }
)

_PROFILE_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_LOWER_HEX_32 = re.compile(r"[0-9a-f]{32}\Z")
_ACCOUNT_NAME = re.compile(r"lm[vp]-[0-9a-f]{20}\Z")
_UNIT_NAME = re.compile(
    r"little-mester-vm-p-[0-9a-f]{20}(?:-proxy|-workspace|-metrics)?\.service\Z"
)
_ACCOUNT_MARKER = re.compile(
    r"little-mester-vm-profile-v1/[1-9][0-9]*/[a-z][a-z0-9-]{0,31}/(vm|proxy)\Z"
)

_PHASE_STEPS = MappingProxyType(
    {
        "install": MappingProxyType(
            {"prepared": 0, "accounts": 1, "state": 2, "units": 3, "verified": 4}
        ),
        "reset": MappingProxyType({"prepared": 0, "leaves": 1, "verified": 2}),
        "remove": MappingProxyType(
            {"prepared": 0, "units": 1, "state": 2, "accounts": 3, "finalizing": 4}
        ),
    }
)

_FILE_KINDS = frozenset({"regular-file", "directory", "socket"})
_UNIT_SELECTOR_PREFIXES = ("unit-file.", "unit-stage.", "unit-manager.")
_RESET_LEAVES = frozenset(TRANSACTION_ALLOWLISTS["reset"])


class _InvalidMapping(Exception):
    pass


def _refuse() -> None:
    raise _InvalidMapping


def _validate_identity(owner_uid: Any, name: Any) -> None:
    low, high = NUMERIC_BOUNDS["owner_uid"]
    if type(owner_uid) is not int or not low <= owner_uid <= high:
        _refuse()
    low, high = STRING_BOUNDS["profile_name"]
    if (
        type(name) is not str
        or not low <= len(name) <= high
        or _PROFILE_NAME.fullmatch(name) is None
        or name in RESERVED_NAMES
    ):
        _refuse()


def profile_digest(owner_uid: int, name: str) -> str:
    """Return the normative 20-hex identity digest for one valid profile key."""
    try:
        _validate_identity(owner_uid, name)
        material = f"little-mester-profile-v1\0{owner_uid}\0{name}".encode("ascii")
        return hashlib.sha256(material).hexdigest()[:20]
    except MappingRefused:
        raise
    except Exception:
        raise MappingRefused("profile mapping refused") from None


def _expect_object(value: Any, fields: tuple[str, ...] | frozenset[str]) -> dict[str, Any]:
    if type(value) is not dict or value.keys() != set(fields):
        _refuse()
    return value


def _integer(value: Any, bounds: tuple[int, int]) -> int:
    if type(value) is not int or not bounds[0] <= value <= bounds[1]:
        _refuse()
    return value


def _string(
    value: Any,
    bounds: tuple[int, int],
    pattern: re.Pattern[str] | None = None,
) -> str:
    if type(value) is not str or not bounds[0] <= len(value) <= bounds[1]:
        _refuse()
    if pattern is not None and pattern.fullmatch(value) is None:
        _refuse()
    return value


def _one_of(value: Any, values: tuple[str, ...] | frozenset[str]) -> str:
    if type(value) is not str or value not in values:
        _refuse()
    return value


def _valid_json_string(value: str) -> bool:
    for character in value:
        point = ord(character)
        # JSON escape sequences can encode non-scalar surrogate code points even
        # though the input byte stream itself was valid UTF-8.
        if 0xD800 <= point <= 0xDFFF or point < 0x20 or 0x7F <= point <= 0x9F:
            return False
    return True


def _validate_all_strings(value: Any) -> None:
    if type(value) is str:
        if not _valid_json_string(value):
            _refuse()
        return
    if type(value) is list:
        for item in value:
            _validate_all_strings(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or not _valid_json_string(key):
                _refuse()
            _validate_all_strings(item)


def _scan_nesting(text: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{" or character == "[":
            depth += 1
            if depth > MAX_NESTING:
                _refuse()
        elif character == "}" or character == "]":
            depth -= 1


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _refuse()
        result[key] = value
    return result


def _parse_finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        _refuse()
    return value


def _reject_constant(_: str) -> None:
    _refuse()


def _absolute_path(value: Any) -> str:
    path = _string(value, STRING_BOUNDS["absolute_path"])
    if not path.startswith("/"):
        _refuse()
    if path == "/":
        return path
    if path.startswith("//") or path.endswith("/"):
        _refuse()
    if any(part in ("", ".", "..") for part in path[1:].split("/")):
        _refuse()
    return path


def _is_within(path: str, parent: str) -> bool:
    if parent == "/":
        return path.startswith("/")
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def _linux_device_major_minor(device: int) -> tuple[int, int]:
    """Decode Linux dev_t without passing attacker-controlled integers to C."""
    major = ((device >> 8) & 0xFFF) | ((device >> 32) & 0xFFFFF000)
    minor = (device & 0xFF) | ((device >> 12) & 0xFFFFFF00)
    return major, minor


def _expected_account_name(role: str, digest: str) -> str:
    return f"{'lmv' if role == 'vm' else 'lmp'}-{digest}"


def _expected_units(digest: str) -> dict[str, str]:
    return {
        "vm": f"little-mester-vm-p-{digest}.service",
        "proxy": f"little-mester-vm-p-{digest}-proxy.service",
        "workspace": f"little-mester-vm-p-{digest}-workspace.service",
        "metrics": f"little-mester-vm-p-{digest}-metrics.service",
    }


def _account_marker(owner_uid: int, name: str, role: str) -> str:
    return f"little-mester-vm-profile-v1/{owner_uid}/{name}/{role}"


def _group_provenance(
    owner_uid: int, name: str, role: str, gid: int, install_id: str
) -> dict[str, Any]:
    group_name = _expected_account_name(role, profile_digest(owner_uid, name))
    return {
        "schema_version": 1,
        "owner_uid": owner_uid,
        "profile_name": name,
        "role": role,
        "group_name": group_name,
        "gid": gid,
        "origin_install_transaction_id": install_id,
    }


def _transaction_id(value: Any) -> str:
    return _string(value, STRING_BOUNDS["transaction_id"], _LOWER_HEX_32)


def _validate_file_identity(value: Any, resource_kind: str) -> dict[str, Any]:
    if resource_kind == "directory":
        identity = _expect_object(value, ("device", "inode", "kind"))
        if identity["kind"] != "directory":
            _refuse()
    else:
        identity = _expect_object(value, ("device", "inode", "kind", "link_count"))
        expected_kind = "socket" if resource_kind == "socket" else "regular"
        if identity["kind"] != expected_kind:
            _refuse()
        _integer(identity["link_count"], NUMERIC_BOUNDS["link_count"])
    _integer(identity["device"], NUMERIC_BOUNDS["object_device"])
    _integer(identity["inode"], NUMERIC_BOUNDS["object_inode"])
    return identity


def _validate_account_identity(value: Any) -> dict[str, Any]:
    identity = _expect_object(value, ("uid", "gid", "marker"))
    _integer(identity["uid"], NUMERIC_BOUNDS["uid_or_gid"])
    _integer(identity["gid"], NUMERIC_BOUNDS["uid_or_gid"])
    _string(identity["marker"], STRING_BOUNDS["account_marker"], _ACCOUNT_MARKER)
    return identity


def _validate_group_provenance(value: Any) -> dict[str, Any]:
    provenance = _expect_object(
        value,
        (
            "schema_version",
            "owner_uid",
            "profile_name",
            "role",
            "group_name",
            "gid",
            "origin_install_transaction_id",
        ),
    )
    _integer(provenance["schema_version"], NUMERIC_BOUNDS["schema_version"])
    _integer(provenance["owner_uid"], NUMERIC_BOUNDS["owner_uid"])
    _string(provenance["profile_name"], STRING_BOUNDS["profile_name"], _PROFILE_NAME)
    _one_of(provenance["role"], ACCOUNT_ROLES)
    _string(provenance["group_name"], STRING_BOUNDS["account_name"], _ACCOUNT_NAME)
    _integer(provenance["gid"], NUMERIC_BOUNDS["uid_or_gid"])
    _transaction_id(provenance["origin_install_transaction_id"])
    return provenance


def _validate_manager_identity(value: Any) -> dict[str, Any]:
    identity = _expect_object(
        value,
        ("fragment_device", "fragment_inode", "load_state", "active_state", "job_state"),
    )
    _integer(identity["fragment_device"], NUMERIC_BOUNDS["object_device"])
    _integer(identity["fragment_inode"], NUMERIC_BOUNDS["object_inode"])
    if (
        identity["load_state"] != "loaded"
        or identity["active_state"] != "inactive"
        or identity["job_state"] != "none"
    ):
        _refuse()
    return identity


def _validate_manager_absence_identity(value: Any) -> dict[str, Any]:
    identity = _expect_object(
        value, ("load_state", "active_state", "job_state", "daemon_reload")
    )
    if (
        identity["load_state"] != "not-found"
        or identity["active_state"] != "inactive"
        or identity["job_state"] != "none"
        or identity["daemon_reload"] != "completed"
    ):
        _refuse()
    return identity


def _validate_expected_object(value: Any) -> dict[str, Any]:
    if type(value) is not dict or "selector" not in value or "evidence" not in value:
        _refuse()
    selector = value["selector"]
    evidence = value["evidence"]
    if type(selector) is not str or selector not in RESOURCE_KINDS:
        _refuse()
    if type(evidence) is not str:
        _refuse()
    kind = RESOURCE_KINDS[selector]

    if kind in _FILE_KINDS:
        if evidence in ("observed_present", "removal_intent"):
            item = _expect_object(value, ("selector", "evidence", "identity"))
            _validate_file_identity(item["identity"], kind)
        elif evidence == "expected_absent":
            _expect_object(value, ("selector", "evidence"))
        elif evidence == "never_created":
            if not selector.startswith(("unit-file.", "unit-stage.")):
                _refuse()
            item = _expect_object(
                value, ("selector", "evidence", "install_transaction_id")
            )
            _transaction_id(item["install_transaction_id"])
        elif evidence == "verified_absent":
            if not selector.startswith("unit-file."):
                _refuse()
            item = _expect_object(
                value,
                ("selector", "evidence", "install_transaction_id", "publication_state"),
            )
            _transaction_id(item["install_transaction_id"])
            if item["publication_state"] != "publication_intent":
                _refuse()
        else:
            _refuse()
    elif kind == "host-account-identity":
        if evidence in ("observed_present", "removal_intent"):
            item = _expect_object(value, ("selector", "evidence", "identity"))
            _validate_account_identity(item["identity"])
        elif evidence == "expected_absent":
            _expect_object(value, ("selector", "evidence"))
        else:
            _refuse()
    elif kind == "host-group-identity":
        if evidence in ("observed_present", "removal_intent", "creation_intent"):
            item = _expect_object(value, ("selector", "evidence", "identity"))
            identity = _expect_object(
                item["identity"], ("name", "gid", "provenance")
            )
            _string(identity["name"], STRING_BOUNDS["account_name"], _ACCOUNT_NAME)
            _integer(identity["gid"], NUMERIC_BOUNDS["uid_or_gid"])
            _validate_group_provenance(identity["provenance"])
        elif evidence == "expected_absent":
            _expect_object(value, ("selector", "evidence"))
        else:
            _refuse()
    elif kind == "unit-manager-identity":
        if evidence in ("observed_present", "removal_intent"):
            item = _expect_object(value, ("selector", "evidence", "identity"))
            _validate_manager_identity(item["identity"])
        elif evidence == "expected_absent":
            _expect_object(value, ("selector", "evidence"))
        elif evidence == "never_created":
            item = _expect_object(
                value, ("selector", "evidence", "install_transaction_id")
            )
            _transaction_id(item["install_transaction_id"])
        elif evidence == "verified_absent":
            item = _expect_object(
                value,
                (
                    "selector",
                    "evidence",
                    "install_transaction_id",
                    "publication_state",
                    "manager_identity",
                ),
            )
            _transaction_id(item["install_transaction_id"])
            _one_of(item["publication_state"], ("publication_intent", "published"))
            _validate_manager_absence_identity(item["manager_identity"])
        else:
            _refuse()
    else:
        _refuse()
    return value


def _publication_rank(record: dict[str, Any]) -> int:
    return {"not_started": 0, "publication_intent": 1, "published": 2}[record["state"]]


def _publication_identity(record: dict[str, Any]) -> dict[str, Any] | None:
    if record["state"] == "publication_intent":
        return record["stage_identity"]
    if record["state"] == "published":
        return record["fragment_identity"]
    return None


def _validate_unit_publication(value: Any) -> dict[str, Any]:
    publication = _expect_object(value, UNIT_ROLES)
    for role in UNIT_ROLES:
        record = publication[role]
        if type(record) is not dict or "state" not in record:
            _refuse()
        state = _one_of(record["state"], ("not_started", "publication_intent", "published"))
        if state == "not_started":
            _expect_object(record, ("state",))
        elif state == "publication_intent":
            _expect_object(record, ("state", "stage_identity"))
            _validate_file_identity(record["stage_identity"], "regular-file")
        else:
            _expect_object(record, ("state", "fragment_identity"))
            _validate_file_identity(record["fragment_identity"], "regular-file")
    ranks = [_publication_rank(publication[role]) for role in UNIT_ROLES]
    if any(left < right for left, right in zip(ranks, ranks[1:])):
        _refuse()
    return publication


def _validate_publication_phase(publication: dict[str, Any], phase: str) -> None:
    ranks = [_publication_rank(publication[role]) for role in UNIT_ROLES]
    if phase in ("prepared", "accounts", "state") and any(ranks):
        _refuse()
    if phase == "verified" and ranks != [2, 2, 2, 2]:
        _refuse()


def _validate_numeric_replans(
    value: Any, accounts: dict[str, Any]
) -> list[dict[str, Any]]:
    if type(value) is not list or len(value) > MAX_NUMERIC_REPLANS:
        _refuse()
    latest: dict[tuple[str, str], int] = {}
    for item in value:
        replan = _expect_object(
            item, ("role", "field", "previous", "replacement", "reason")
        )
        role = _one_of(replan["role"], ACCOUNT_ROLES)
        field = _one_of(replan["field"], ("uid", "gid"))
        previous = _integer(replan["previous"], NUMERIC_BOUNDS["uid_or_gid"])
        replacement = _integer(replan["replacement"], NUMERIC_BOUNDS["uid_or_gid"])
        if replacement == previous or replan["reason"] != "numeric_collision":
            _refuse()
        key = (role, field)
        if key in latest and previous != latest[key]:
            _refuse()
        latest[key] = replacement
    for (role, field), replacement in latest.items():
        if accounts[role][field] != replacement:
            _refuse()
    return value


def _validate_transaction(
    transaction: Any,
    state: str,
    profile: dict[str, Any],
    installation: dict[str, Any],
) -> None:
    if type(transaction) is not dict:
        _refuse()
    operation = {"installing": "install", "resetting": "reset", "removing": "remove"}[state]
    common = {"schema_version", "id", "operation", "phase", "step", "expected"}
    if operation == "install":
        fields = common | {"unit_publication", "numeric_replans"}
    elif operation == "reset":
        fields = common
    else:
        fields = common | {"source_install_id", "unit_publication"}
    tx = _expect_object(transaction, fields)
    _integer(tx["schema_version"], NUMERIC_BOUNDS["schema_version"])
    tx_id = _transaction_id(tx["id"])
    if tx["operation"] != operation:
        _refuse()
    phase = tx["phase"]
    if type(phase) is not str or phase not in _PHASE_STEPS[operation]:
        _refuse()
    _integer(tx["step"], NUMERIC_BOUNDS["transaction_step"])
    if tx["step"] != _PHASE_STEPS[operation][phase]:
        _refuse()
    expected = tx["expected"]
    if type(expected) is not list or len(expected) > MAX_EXPECTED_OBJECTS:
        _refuse()
    selectors: set[str] = set()
    expected_by_selector: dict[str, dict[str, Any]] = {}
    allowed = frozenset(TRANSACTION_ALLOWLISTS[operation])
    for raw_item in expected:
        item = _validate_expected_object(raw_item)
        selector = item["selector"]
        if selector in selectors or selector not in allowed:
            _refuse()
        selectors.add(selector)
        expected_by_selector[selector] = item

    if operation == "install":
        _validate_numeric_replans(tx["numeric_replans"], profile["accounts"])
        if tx_id != installation["id"]:
            _refuse()
        publication = _validate_unit_publication(tx["unit_publication"])
        if publication != installation["unit_publication"]:
            _refuse()
        _validate_publication_phase(publication, phase)
    elif operation == "remove":
        if _transaction_id(tx["source_install_id"]) != installation["id"]:
            _refuse()
        publication = _validate_unit_publication(tx["unit_publication"])
        if publication != installation["unit_publication"]:
            _refuse()

    _validate_expected_semantics(profile, tx, expected_by_selector)


def _validate_expected_semantics(
    profile: dict[str, Any],
    transaction: dict[str, Any],
    expected: dict[str, dict[str, Any]],
) -> None:
    owner_uid = profile["owner_uid"]
    name = profile["name"]
    operation = transaction["operation"]
    phase = transaction["phase"]
    installation = profile["installation"]
    source_publication = transaction.get("unit_publication")

    if operation == "remove":
        _validate_remove_publication_source_consistency(transaction, expected)

    for selector, item in expected.items():
        evidence = item["evidence"]
        kind = RESOURCE_KINDS[selector]
        identity = item.get("identity")
        if identity is not None:
            if kind == "host-account-identity":
                role = selector.removeprefix("account.")
                account = profile["accounts"][role]
                if (identity["uid"], identity["gid"]) != (account["uid"], account["gid"]):
                    _refuse()
                if identity["marker"] != _account_marker(owner_uid, name, role):
                    _refuse()
                if evidence in ("observed_present", "removal_intent") and account["allocation"] != "committed":
                    _refuse()
                if evidence == "removal_intent":
                    group = installation["groups"][role]
                    if (
                        group["state"] != "owned"
                        or group["name"] != account["name"]
                        or group["gid"] != account["gid"]
                        or group["provenance"]
                        != _group_provenance(
                            owner_uid, name, role, account["gid"], installation["id"]
                        )
                    ):
                        _refuse()
            elif kind == "host-group-identity":
                role = selector.removeprefix("group.")
                group = installation["groups"][role]
                protected_identity = {
                    "name": group["name"],
                    "gid": group["gid"],
                    "provenance": group["provenance"],
                }
                if identity != protected_identity:
                    _refuse()
                if evidence == "creation_intent" and group["state"] != "creation_intent":
                    _refuse()
                if evidence in ("observed_present", "removal_intent") and group["state"] != "owned":
                    _refuse()

        elif kind == "host-account-identity":
            role = selector.removeprefix("account.")
            if evidence == "expected_absent" and profile["accounts"][role]["allocation"] != "planned":
                _refuse()
        elif kind == "host-group-identity":
            role = selector.removeprefix("group.")
            if evidence == "expected_absent" and installation["groups"][role]["state"] != "not_started":
                _refuse()

        if evidence == "never_created":
            if operation != "remove" or not selector.startswith(_UNIT_SELECTOR_PREFIXES):
                _refuse()
            if item["install_transaction_id"] != transaction["source_install_id"]:
                _refuse()
            role = selector.rsplit(".", 1)[1]
            if source_publication[role]["state"] != "not_started":
                _refuse()
        elif evidence == "verified_absent":
            if operation != "remove" or item["install_transaction_id"] != transaction["source_install_id"]:
                _refuse()
            role = selector.rsplit(".", 1)[1]
            if item["publication_state"] != source_publication[role]["state"]:
                _refuse()
        elif evidence == "expected_absent" and operation == "remove" and selector.startswith(
            _UNIT_SELECTOR_PREFIXES
        ):
            _refuse()
        elif evidence == "removal_intent":
            if operation == "reset":
                if selector not in _RESET_LEAVES or phase not in ("leaves", "verified"):
                    _refuse()
            elif operation == "remove":
                if selector.startswith(_UNIT_SELECTOR_PREFIXES):
                    valid_phases = {"units", "state", "accounts", "finalizing"}
                elif selector.startswith(("account.", "group.")):
                    valid_phases = {"accounts", "finalizing"}
                else:
                    valid_phases = {"state", "accounts", "finalizing"}
                if phase not in valid_phases or selector == "profile.config-dir":
                    _refuse()
            else:
                _refuse()

    for selector, item in expected.items():
        if not selector.startswith("unit-manager.") or "identity" not in item:
            continue
        role = selector.rsplit(".", 1)[1]
        fragment = expected.get(f"unit-file.{role}")
        if fragment is None or "identity" not in fragment:
            _refuse()
        manager_identity = item["identity"]
        file_identity = fragment["identity"]
        if (
            manager_identity["fragment_device"] != file_identity["device"]
            or manager_identity["fragment_inode"] != file_identity["inode"]
        ):
            _refuse()

    if operation == "install":
        _validate_install_publication_evidence(transaction, expected)
    elif operation == "remove":
        if phase in ("units", "state", "accounts", "finalizing"):
            _validate_remove_publication_evidence(transaction, expected)


def _validate_install_publication_evidence(
    transaction: dict[str, Any], expected: dict[str, dict[str, Any]]
) -> None:
    publication = transaction["unit_publication"]
    for role in UNIT_ROLES:
        record = publication[role]
        file_item = expected.get(f"unit-file.{role}")
        stage_item = expected.get(f"unit-stage.{role}")
        manager_item = expected.get(f"unit-manager.{role}")
        if record["state"] == "not_started":
            for item in (file_item, stage_item):
                if item and item["evidence"] in ("observed_present", "removal_intent"):
                    _refuse()
        elif record["state"] == "publication_intent":
            identity = record["stage_identity"]
            path_items = (stage_item, file_item)
            for item in path_items:
                if item is not None and "identity" in item and item["identity"] != identity:
                    _refuse()
            matches = [
                item
                for item in path_items
                if item is not None
                and item.get("identity") == identity
                and item["evidence"] == "observed_present"
            ]
            if len(matches) != 1:
                _refuse()
        else:
            identity = record["fragment_identity"]
            if (
                file_item is None
                or file_item["evidence"] != "observed_present"
                or file_item.get("identity") != identity
            ):
                _refuse()
            if stage_item is not None and "identity" in stage_item:
                _refuse()
            if (
                manager_item is None
                or manager_item["evidence"] != "observed_present"
                or manager_item["identity"]["fragment_device"] != identity["device"]
                or manager_item["identity"]["fragment_inode"] != identity["inode"]
            ):
                _refuse()


def _validate_remove_publication_source_consistency(
    transaction: dict[str, Any], expected: dict[str, dict[str, Any]]
) -> None:
    """Bind supplied remove identities to the retained install snapshot.

    Source consistency applies in ``prepared`` too. Omitted sparse selectors
    are not observations of absence; later phases separately require complete
    durable evidence.
    """
    for role in UNIT_ROLES:
        record = transaction["unit_publication"][role]
        file_item = expected.get(f"unit-file.{role}")
        stage_item = expected.get(f"unit-stage.{role}")
        manager_item = expected.get(f"unit-manager.{role}")

        if record["state"] == "not_started":
            if any(
                item is not None and "identity" in item
                for item in (file_item, stage_item, manager_item)
            ):
                _refuse()
        elif record["state"] == "publication_intent":
            identity = record["stage_identity"]
            path_items = (file_item, stage_item)
            if sum(item is not None and "identity" in item for item in path_items) > 1:
                _refuse()
            for item in path_items:
                if item is not None and "identity" in item and item["identity"] != identity:
                    _refuse()
            if manager_item is not None and "identity" in manager_item:
                manager_identity = manager_item["identity"]
                if (
                    (manager_identity["fragment_device"], manager_identity["fragment_inode"])
                    != (identity["device"], identity["inode"])
                    or file_item is None
                    or file_item.get("identity") != identity
                ):
                    _refuse()
        else:
            identity = record["fragment_identity"]
            if stage_item is not None and "identity" in stage_item:
                _refuse()
            if file_item is not None and "identity" in file_item and file_item["identity"] != identity:
                _refuse()
            if manager_item is not None and "identity" in manager_item:
                manager_identity = manager_item["identity"]
                if (
                    manager_identity["fragment_device"] != identity["device"]
                    or manager_identity["fragment_inode"] != identity["inode"]
                ):
                    _refuse()


def _validate_remove_publication_evidence(
    transaction: dict[str, Any], expected: dict[str, dict[str, Any]]
) -> None:
    source_id = transaction["source_install_id"]
    publication = transaction["unit_publication"]
    for role in UNIT_ROLES:
        record = publication[role]
        file_item = expected.get(f"unit-file.{role}")
        stage_item = expected.get(f"unit-stage.{role}")
        manager_item = expected.get(f"unit-manager.{role}")
        if record["state"] == "not_started":
            for item in (file_item, manager_item):
                if (
                    item is None
                    or item["evidence"] != "never_created"
                    or item["install_transaction_id"] != source_id
                ):
                    _refuse()
            if stage_item is not None and stage_item["evidence"] != "never_created":
                _refuse()
        elif record["state"] == "published":
            identity = record["fragment_identity"]
            if stage_item is not None and "identity" in stage_item:
                _refuse()
            if (
                file_item is None
                or file_item["evidence"] not in ("observed_present", "removal_intent")
                or file_item.get("identity") != identity
            ):
                _refuse()
            if manager_item is None:
                _refuse()
            if manager_item["evidence"] in ("observed_present", "removal_intent"):
                manager_identity = manager_item.get("identity", {})
                if (
                    manager_identity.get("fragment_device"),
                    manager_identity.get("fragment_inode"),
                ) != (identity["device"], identity["inode"]):
                    _refuse()
            elif manager_item["evidence"] == "verified_absent":
                if file_item["evidence"] != "removal_intent":
                    _refuse()
            else:
                _refuse()
        else:
            identity = record["stage_identity"]
            stage_matches = (
                stage_item is not None
                and stage_item["evidence"] == "removal_intent"
                and stage_item.get("identity") == identity
            )
            final_matches = (
                file_item is not None
                and file_item["evidence"] == "removal_intent"
                and file_item.get("identity") == identity
            )
            if stage_matches and file_item is not None and "identity" in file_item:
                _refuse()
            if final_matches and stage_item is not None and "identity" in stage_item:
                _refuse()
            if stage_matches == final_matches:
                _refuse()
            if stage_matches:
                if (
                    file_item is None
                    or file_item["evidence"] != "verified_absent"
                    or file_item["install_transaction_id"] != source_id
                ):
                    _refuse()
                if manager_item is None or manager_item["evidence"] != "verified_absent":
                    _refuse()
            elif manager_item is None or manager_item["evidence"] not in (
                "removal_intent",
                "verified_absent",
            ):
                _refuse()


def _validate_retained_reset_source(profile: dict[str, Any]) -> None:
    """Require reset's retained account, group, and unit source records."""
    owner_uid = profile["owner_uid"]
    name = profile["name"]
    installation = profile["installation"]
    for role in ACCOUNT_ROLES:
        account = profile["accounts"][role]
        group = installation["groups"][role]
        if (
            account["allocation"] != "committed"
            or group["state"] != "owned"
            or group["name"] != account["name"]
            or group["gid"] != account["gid"]
            or group["provenance"]
            != _group_provenance(owner_uid, name, role, account["gid"], installation["id"])
        ):
            _refuse()
    if any(
        installation["unit_publication"][role]["state"] != "published"
        for role in UNIT_ROLES
    ):
        _refuse()


def _validate_profile(profile: Any) -> dict[str, Any]:
    root = _expect_object(
        profile,
        (
            "schema_version",
            "owner_uid",
            "name",
            "state",
            "runtime",
            "accounts",
            "installation",
            "units",
            "workspace",
            "ssh_port",
            "transaction",
        ),
    )
    _integer(root["schema_version"], NUMERIC_BOUNDS["schema_version"])
    owner_uid = _integer(root["owner_uid"], NUMERIC_BOUNDS["owner_uid"])
    name = _string(root["name"], STRING_BOUNDS["profile_name"], _PROFILE_NAME)
    if name in RESERVED_NAMES:
        _refuse()
    state = _one_of(root["state"], ("installing", "ready", "resetting", "removing"))

    runtime = _expect_object(
        root["runtime"],
        ("commit_sha", "profile_runtime_api", "control_helper_api_min", "control_helper_api_max"),
    )
    _string(runtime["commit_sha"], STRING_BOUNDS["sha"], _SHA)
    _integer(runtime["profile_runtime_api"], NUMERIC_BOUNDS["runtime_api"])
    api_min = _integer(runtime["control_helper_api_min"], NUMERIC_BOUNDS["runtime_api"])
    api_max = _integer(runtime["control_helper_api_max"], NUMERIC_BOUNDS["runtime_api"])
    if api_min > api_max:
        _refuse()

    accounts = _expect_object(root["accounts"], ACCOUNT_ROLES)
    for role in ACCOUNT_ROLES:
        account = _expect_object(accounts[role], ("name", "uid", "gid", "allocation"))
        _string(account["name"], STRING_BOUNDS["account_name"], _ACCOUNT_NAME)
        _integer(account["uid"], NUMERIC_BOUNDS["uid_or_gid"])
        _integer(account["gid"], NUMERIC_BOUNDS["uid_or_gid"])
        _one_of(account["allocation"], ("planned", "committed"))
    digest = profile_digest(owner_uid, name)
    for role in ACCOUNT_ROLES:
        if accounts[role]["name"] != _expected_account_name(role, digest):
            _refuse()
    if accounts["vm"]["uid"] == accounts["proxy"]["uid"]:
        _refuse()
    if accounts["vm"]["gid"] == accounts["proxy"]["gid"]:
        _refuse()
    if owner_uid in (accounts["vm"]["uid"], accounts["proxy"]["uid"]):
        _refuse()

    installation = _expect_object(root["installation"], ("id", "unit_publication", "groups"))
    install_id = _transaction_id(installation["id"])
    _validate_unit_publication(installation["unit_publication"])
    groups = _expect_object(installation["groups"], ACCOUNT_ROLES)
    for role in ACCOUNT_ROLES:
        group = _expect_object(groups[role], ("name", "gid", "state", "provenance"))
        _string(group["name"], STRING_BOUNDS["account_name"], _ACCOUNT_NAME)
        _integer(group["gid"], NUMERIC_BOUNDS["uid_or_gid"])
        _one_of(group["state"], ("not_started", "creation_intent", "owned"))
        provenance = _validate_group_provenance(group["provenance"])
        expected_group_name = _expected_account_name(role, digest)
        if (
            group["name"] != expected_group_name
            or group["name"] != accounts[role]["name"]
            or group["gid"] != accounts[role]["gid"]
            or provenance
            != _group_provenance(owner_uid, name, role, group["gid"], install_id)
        ):
            _refuse()
        if group["state"] == "creation_intent":
            # A snapshot cannot establish that an interrupted creation was
            # completed; never infer adoption from a matching name or GID.
            _refuse()

    units = _expect_object(root["units"], UNIT_ROLES)
    for role in UNIT_ROLES:
        _string(units[role], STRING_BOUNDS["unit_name"], _UNIT_NAME)
    if units != _expected_units(digest):
        _refuse()

    workspace = root["workspace"]
    if workspace is not None:
        workspace = _expect_object(
            workspace,
            ("canonical_path", "device", "inode", "mount_id", "mount", "unsafe_recurse"),
        )
        canonical_path = _absolute_path(workspace["canonical_path"])
        device = _integer(workspace["device"], NUMERIC_BOUNDS["workspace_device"])
        _integer(workspace["inode"], NUMERIC_BOUNDS["workspace_inode"])
        _integer(workspace["mount_id"], NUMERIC_BOUNDS["mount_id"])
        mount = _expect_object(workspace["mount"], ("major", "minor", "root", "mount_point"))
        major = _integer(mount["major"], NUMERIC_BOUNDS["mount_component"])
        minor = _integer(mount["minor"], NUMERIC_BOUNDS["mount_component"])
        _absolute_path(mount["root"])
        mount_point = _absolute_path(mount["mount_point"])
        if not _is_within(canonical_path, mount_point):
            _refuse()
        if _linux_device_major_minor(device) != (major, minor):
            _refuse()
        if type(workspace["unsafe_recurse"]) is not bool:
            _refuse()

    _integer(root["ssh_port"], NUMERIC_BOUNDS["ssh_port"])
    ports = (root["ssh_port"], root["ssh_port"] + 1, root["ssh_port"] + 2)
    if any(port < 1 or port > 65535 for port in ports) or 22222 in ports:
        _refuse()

    transaction = root["transaction"]
    if state == "ready":
        if transaction is not None:
            _refuse()
        _validate_retained_reset_source(root)
    else:
        if transaction is None:
            _refuse()
        if state == "resetting":
            _validate_retained_reset_source(root)
        _validate_transaction(transaction, state, root, installation)

    return root


def _container_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    return _reject_duplicate_keys(pairs)


def parse_profile_mapping(
    raw: bytes, *, expected_owner_uid: int, expected_name: str
) -> dict[str, Any]:
    """Parse and validate one complete profile-mapping v1 byte string.

    The returned record is newly decoded validated data, not a capability,
    authorization result, or proof of any on-disk identity.  This function has
    no filesystem, subprocess, network, owner-resolution, or path interface.
    """
    try:
        _validate_identity(expected_owner_uid, expected_name)
        if type(raw) is not bytes or not raw or len(raw) > MAX_MAPPING_BYTES:
            _refuse()
        text = raw.decode("utf-8", errors="strict")
        _scan_nesting(text)
        parsed = json.loads(
            text,
            object_pairs_hook=_container_pairs,
            parse_constant=_reject_constant,
            parse_float=_parse_finite_float,
        )
        _validate_all_strings(parsed)
        record = _validate_profile(parsed)
        if record["owner_uid"] != expected_owner_uid or record["name"] != expected_name:
            _refuse()
        return record
    except MappingRefused:
        raise
    except Exception:
        # Never echo input bytes, JSON exception messages, keys, or host metadata.
        raise MappingRefused("profile mapping refused") from None
    except BaseException:
        # Retain normal process-control exceptions; convert only parser/runtime
        # failures covered by Exception above.
        raise
