#!/usr/bin/env python3
"""Closed v2 profile mapping schema, parser, and protected instance loader."""

from __future__ import annotations

import grp
import importlib.util
import json
import os
import pathlib
import re
import stat
import sys
from types import ModuleType
import uuid


MAPPING_SCHEMA_VERSION = 2
RUNTIME_API_VERSION = 1
HELPER_API_VERSION = 1
MAPPING_PATH_ROOT = pathlib.Path("/etc/little-mester-vm/profiles")
MAX_MAPPING_BYTES = 64 * 1024
SHA40 = r"^[0-9a-f]{40}$"
SHA256 = r"^[0-9a-f]{64}$"
UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
PROFILE_NAME = r"^[a-z][a-z0-9-]{0,31}$"


class Refused(RuntimeError):
    """A profile mapping or protected profile object failed closed validation."""


def _object(properties: dict, required: tuple[str, ...]) -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "properties": properties, "required": list(required),
    }


def mapping_schema() -> dict:
    """Return the machine-readable closed mapping schema used by the parser."""
    artifact_identity = _object({
        "device": {"type": "integer", "minimum": 1},
        "inode": {"type": "integer", "minimum": 1},
    }, ("device", "inode"))
    hashed_artifact = _object({
        "device": {"type": "integer", "minimum": 1},
        "inode": {"type": "integer", "minimum": 1},
        "sha256": {"type": "string", "pattern": SHA256},
    }, ("device", "inode", "sha256"))
    provision = _object({
        "generation": {"type": "string", "pattern": UUID},
        "artifacts": _object({
            "disk": _object({
                "device": {"type": "integer", "minimum": 1},
                "inode": {"type": "integer", "minimum": 1},
                "virtual_size": {"const": 68719476736},
                "allocated_bytes": {"type": "integer", "minimum": 1,
                                    "maximum": 68719476735},
            }, ("device", "inode", "virtual_size", "allocated_bytes")),
            "seed": hashed_artifact,
            "host_private": artifact_identity,
            "host_public": hashed_artifact,
            "client_private": artifact_identity,
            "client_public": hashed_artifact,
            "known_hosts": hashed_artifact,
        }, ("disk", "seed", "host_private", "host_public", "client_private",
            "client_public", "known_hosts")),
    }, ("generation", "artifacts"))
    transaction_common = {
        "schema_version": {"const": 2},
        "transaction_id": {"type": "string", "pattern": UUID},
        "phase": {"type": "string", "minLength": 1, "maxLength": 48,
                   "pattern": r"^[a-z][a-z0-9-]*$"},
    }
    transaction = {"oneOf": [
        _object({**transaction_common,
                 "operation": {"const": "install"},
                 "mode": {"enum": ["fresh", "reprovision"]},
                 "generation": {"type": "string", "pattern": UUID}},
                ("schema_version", "operation", "transaction_id", "phase", "mode", "generation")),
        _object({**transaction_common,
                 "operation": {"const": "reset"},
                 "generation": {"type": "string", "pattern": UUID}},
                ("schema_version", "operation", "transaction_id", "phase", "generation")),
        _object({**transaction_common,
                 "operation": {"const": "remove"},
                 "generation": {"type": ["string", "null"], "pattern": UUID}},
                ("schema_version", "operation", "transaction_id", "phase", "generation")),
    ]}
    identity = _object({
        "name": {"type": "string", "pattern": r"^[a-z][a-z0-9-]{0,31}$"},
        "uid": {"type": "integer", "minimum": 1, "maximum": 999},
        "gid": {"type": "integer", "minimum": 1, "maximum": 999},
    }, ("name", "uid", "gid"))
    workspace = {
        "oneOf": [
            {"type": "null"},
            _object({
                "path": {"type": "string", "minLength": 1, "maxLength": 4096,
                         "pattern": r"^/[^\x00\r\n]*$"},
                "device": {"type": "integer", "minimum": 1},
                "inode": {"type": "integer", "minimum": 1},
                "mount_id": {"type": "integer", "minimum": 1},
            }, ("path", "device", "inode", "mount_id")),
        ],
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://little-mester.invalid/vm/profile-mapping-v2.schema.json",
        "title": "Little Mester protected VM profile mapping v2",
        "type": "object", "additionalProperties": False,
        "required": ["schema_version", "name", "owner_uid", "state", "transaction",
                     "runtime", "base", "resources", "accounts", "units", "workspace",
                     "provisioning"],
        "properties": {
            "schema_version": {"const": MAPPING_SCHEMA_VERSION},
            "name": {"type": "string", "pattern": PROFILE_NAME},
            "owner_uid": {"type": "integer", "minimum": 1, "maximum": 4294967294},
            "state": {"enum": ["installing", "ready", "unprovisioned", "resetting", "removing"]},
            "transaction": {"oneOf": [{"type": "null"}, transaction]},
            "runtime": _object({
                "sha": {"type": "string", "pattern": SHA40},
                "manifest_sha256": {"type": "string", "pattern": SHA256},
                "helper_api": {"const": HELPER_API_VERSION},
                "runtime_api": {"const": RUNTIME_API_VERSION},
            }, ("sha", "manifest_sha256", "helper_api", "runtime_api")),
            "base": _object({
                "source_sha256": {"const": _approved_base_digest()},
                "manifest_sha256": {"type": "string", "pattern": SHA256},
            }, ("source_sha256", "manifest_sha256")),
            "resources": _object({
                "cpus": {"type": "integer", "minimum": 1, "maximum": 16},
                "memory_mib": {"type": "integer", "minimum": 256, "maximum": 65536},
                "ssh_port": {"type": "integer", "minimum": 22223, "maximum": 23220},
            }, ("cpus", "memory_mib", "ssh_port")),
            "accounts": _object({"vm": identity, "proxy": identity}, ("vm", "proxy")),
            "units": _object({
                "vm": {"type": "string", "pattern": r"^[A-Za-z0-9_.@:-]+\.service$"},
                "proxy": {"type": "string", "pattern": r"^[A-Za-z0-9_.@:-]+\.service$"},
                "workspace": {"type": "string", "pattern": r"^[A-Za-z0-9_.@:-]+\.service$"},
                "metrics": {"type": "string", "pattern": r"^[A-Za-z0-9_.@:-]+\.service$"},
            }, ("vm", "proxy", "workspace", "metrics")),
            "workspace": workspace,
            "provisioning": {"oneOf": [{"type": "null"}, provision]},
        },
    }


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refused("profile mapping contains a duplicate JSON key")
        result[key] = value
    return result


def _exact_object(value: object, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise Refused(f"profile mapping {label} has an unknown or missing field")
    return value


def _integer(value: object, low: int, high: int, label: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise Refused(f"profile mapping {label} is outside its validated range")
    return value


def _sha(value: object, length: int, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-f]{{{length}}}", value):
        raise Refused(f"profile mapping {label} is malformed")
    return value


def _uuid(value: object, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(UUID, value):
        raise Refused(f"profile mapping {label} is malformed")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as error:
        raise Refused(f"profile mapping {label} is malformed") from error
    return value


def _approved_base_digest() -> str:
    path = pathlib.Path(__file__).with_name("parent-provision.py")
    spec = importlib.util.spec_from_file_location("profile_control_parent_provision", path)
    if spec is None or spec.loader is None:
        raise Refused("fixed approved-base policy is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.IMAGE_SHA256


def _validate_artifact_identity(value: object, label: str, *, digest: bool) -> None:
    keys = {"device", "inode", "sha256"} if digest else {"device", "inode"}
    record = _exact_object(value, keys, label)
    _integer(record["device"], 1, 2**63 - 1, f"{label} device")
    _integer(record["inode"], 1, 2**63 - 1, f"{label} inode")
    if digest:
        _sha(record["sha256"], 64, f"{label} digest")


def validate_mapping(value: object, *, expected_name: str | None = None,
                     expected_owner_uid: int | None = None,
                     require_ready: bool = False) -> dict:
    """Validate closed v2 shape plus parser-only cross-field invariants."""
    record = _exact_object(value, {
        "schema_version", "name", "owner_uid", "state", "transaction", "runtime",
        "base", "resources", "accounts", "units", "workspace", "provisioning",
    }, "root")
    if type(record["schema_version"]) is not int or record["schema_version"] != MAPPING_SCHEMA_VERSION:
        raise Refused("profile mapping schema version is unsupported; no migration is attempted")
    name = record["name"]
    if not isinstance(name, str) or not re.fullmatch(PROFILE_NAME, name):
        raise Refused("profile mapping name is invalid")
    if expected_name is not None and name != expected_name:
        raise Refused("profile mapping name does not match its protected directory")
    owner_uid = _integer(record["owner_uid"], 1, 2**32 - 2, "owner UID")
    if expected_owner_uid is not None and owner_uid != expected_owner_uid:
        raise Refused("profile mapping belongs to a different enrolled owner")
    if record["state"] not in ("installing", "ready", "unprovisioned", "resetting", "removing"):
        raise Refused("profile mapping lifecycle state is unsupported")

    runtime = _exact_object(record["runtime"],
                            {"sha", "manifest_sha256", "helper_api", "runtime_api"}, "runtime")
    _sha(runtime["sha"], 40, "runtime SHA")
    _sha(runtime["manifest_sha256"], 64, "runtime manifest digest")
    if (type(runtime["helper_api"]) is not int or runtime["helper_api"] != HELPER_API_VERSION
            or type(runtime["runtime_api"]) is not int
            or runtime["runtime_api"] != RUNTIME_API_VERSION):
        raise Refused("profile runtime/helper API is incompatible")

    base = _exact_object(record["base"], {"source_sha256", "manifest_sha256"}, "base")
    _sha(base["source_sha256"], 64, "base source SHA-256")
    _sha(base["manifest_sha256"], 64, "base manifest digest")
    if base["source_sha256"] != _approved_base_digest():
        raise Refused("profile mapping selects an unapproved base image")
    resources = _exact_object(record["resources"], {"cpus", "memory_mib", "ssh_port"}, "resources")
    _integer(resources["cpus"], 1, 16, "CPU allocation")
    _integer(resources["memory_mib"], 256, 65536, "memory allocation")
    ssh_port = _integer(resources["ssh_port"], 22223, 23220, "SSH port")
    if (ssh_port - 22223) % 3:
        raise Refused("profile SSH port is not the start of a reserved three-port block")

    accounts = _exact_object(record["accounts"], {"vm", "proxy"}, "accounts")
    for role in ("vm", "proxy"):
        identity = _exact_object(accounts[role], {"name", "uid", "gid"}, f"{role} account")
        if not isinstance(identity["name"], str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}",
                                                                       identity["name"]):
            raise Refused(f"profile {role} account name is invalid")
        _integer(identity["uid"], 1, 999, f"{role} UID")
        _integer(identity["gid"], 1, 999, f"{role} GID")
    if accounts["vm"]["uid"] == accounts["proxy"]["uid"] or accounts["vm"]["gid"] == accounts["proxy"]["gid"]:
        raise Refused("profile VM and proxy OS identities must be distinct")

    units = _exact_object(record["units"], {"vm", "proxy", "workspace", "metrics"}, "units")
    if any(not isinstance(unit, str) or not re.fullmatch(r"[A-Za-z0-9_.@:-]+\.service", unit)
           for unit in units.values()):
        raise Refused("profile unit name is invalid")
    if len(set(units.values())) != 4:
        raise Refused("profile unit names must be distinct")

    workspace = record["workspace"]
    if workspace is not None:
        workspace = _exact_object(workspace, {"path", "device", "inode", "mount_id"}, "workspace")
        path = workspace["path"]
        if (not isinstance(path, str) or not path.startswith("/")
                or path.startswith("//")
                or any(character in path for character in "\0\r\n")
                or str(pathlib.PurePosixPath(path)) != path
                or ".." in pathlib.PurePosixPath(path).parts):
            raise Refused("profile workspace observation is not an absolute safe path")
        for key in ("device", "inode", "mount_id"):
            _integer(workspace[key], 1, 2**63 - 1, f"workspace {key}")

    transaction = record["transaction"]
    if transaction is not None:
        if not isinstance(transaction, dict):
            raise Refused("profile transaction is malformed")
        operation = transaction.get("operation")
        if not isinstance(operation, str):
            raise Refused("profile transaction operation is malformed")
        transaction_keys = {
            "install": {"schema_version", "operation", "transaction_id", "phase", "mode", "generation"},
            "reset": {"schema_version", "operation", "transaction_id", "phase", "generation"},
            "remove": {"schema_version", "operation", "transaction_id", "phase", "generation"},
        }.get(operation)
        if transaction_keys is None:
            raise Refused("profile transaction operation is unsupported")
        transaction = _exact_object(transaction, transaction_keys, "transaction")
        if type(transaction["schema_version"]) is not int or transaction["schema_version"] != 2:
            raise Refused("profile transaction schema version is unsupported")
        _uuid(transaction["transaction_id"], "transaction ID")
        phase = transaction["phase"]
        if (not isinstance(phase, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", phase)):
            raise Refused("profile transaction phase is malformed")
        if operation == "install" and transaction["mode"] not in ("fresh", "reprovision"):
            raise Refused("profile transaction install mode is unsupported")
        if transaction["generation"] is not None:
            _uuid(transaction["generation"], "transaction generation")

    state = record["state"]
    provisioning = record["provisioning"]
    if provisioning is not None:
        provisioning = _exact_object(provisioning, {"generation", "artifacts"}, "provisioning")
        _uuid(provisioning["generation"], "provisioning generation")
        artifacts = _exact_object(provisioning["artifacts"], {
            "disk", "seed", "host_private", "host_public", "client_private",
            "client_public", "known_hosts",
        }, "provisioning artifacts")
        disk = _exact_object(artifacts["disk"], {
            "device", "inode", "virtual_size", "allocated_bytes",
        }, "disk evidence")
        _integer(disk["device"], 1, 2**63 - 1, "disk device")
        _integer(disk["inode"], 1, 2**63 - 1, "disk inode")
        if type(disk["virtual_size"]) is not int or disk["virtual_size"] != 64 * 1024**3:
            raise Refused("profile disk virtual capacity is invalid")
        _integer(disk["allocated_bytes"], 1, 64 * 1024**3 - 1, "disk allocated size")
        _validate_artifact_identity(artifacts["seed"], "seed evidence", digest=True)
        _validate_artifact_identity(artifacts["host_private"], "host private key evidence", digest=False)
        _validate_artifact_identity(artifacts["host_public"], "host public key evidence", digest=True)
        _validate_artifact_identity(artifacts["client_private"], "client private key evidence", digest=False)
        _validate_artifact_identity(artifacts["client_public"], "client public key evidence", digest=True)
        _validate_artifact_identity(artifacts["known_hosts"], "known-host evidence", digest=True)

    if (state == "ready" and (transaction is not None or provisioning is None)
            or state == "unprovisioned" and (transaction is not None or provisioning is not None)
            or state == "installing" and (transaction is None or transaction["operation"] != "install")
            or state == "resetting" and (transaction is None or transaction["operation"] != "reset"
                                          or provisioning is None)
            or state == "removing" and (transaction is None or transaction["operation"] != "remove")):
        raise Refused("profile mapping state, transaction, and provisioning evidence disagree")
    if state == "installing" and transaction["generation"] is None:
        raise Refused("install transaction has no durable provisioning-generation ID")
    if state == "installing" and provisioning is not None:
        raise Refused("installing mapping cannot publish current provisioning evidence")
    if state == "resetting" and transaction["generation"] != provisioning["generation"]:
        raise Refused("reset transaction does not identify the retained provisioning generation")
    if state == "removing":
        current_generation = None if provisioning is None else provisioning["generation"]
        if transaction["generation"] != current_generation:
            raise Refused("remove transaction does not identify the retained provisioning generation")
    if require_ready and (state != "ready" or transaction is not None or provisioning is None):
        raise Refused("profile is not ready for VM execution")
    return record


def _instance_policy() -> ModuleType:
    path = pathlib.Path(__file__).with_name("vm-instance.py")
    spec = importlib.util.spec_from_file_location("profile_control_vm_instance", path)
    if spec is None or spec.loader is None:
        raise Refused("fixed VM instance descriptor is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def instance_from_mapping(value: object, *, expected_name: str | None = None,
                          expected_owner_uid: int | None = None,
                          require_ready: bool = False) -> object:
    record = validate_mapping(value, expected_name=expected_name,
                              expected_owner_uid=expected_owner_uid,
                              require_ready=require_ready)
    policy = _instance_policy()
    accounts = record["accounts"]
    units = record["units"]
    workspace = record["workspace"]
    try:
        instance = policy.Instance.profile(
            name=record["name"], owner_uid=record["owner_uid"],
            runtime_sha=record["runtime"]["sha"],
            vm_uid=accounts["vm"]["uid"], vm_gid=accounts["vm"]["gid"],
            proxy_uid=accounts["proxy"]["uid"], proxy_gid=accounts["proxy"]["gid"],
            base_source_sha256=record["base"]["source_sha256"],
            base_manifest_sha256=record["base"]["manifest_sha256"],
            ssh_port=record["resources"]["ssh_port"],
            cpus=record["resources"]["cpus"], memory_mib=record["resources"]["memory_mib"],
            workspace_path=None if workspace is None else workspace["path"])
    except policy.InvalidInstance as error:
        raise Refused("profile mapping cannot derive a safe instance identity") from error
    expected = {
        "vm_user": accounts["vm"]["name"], "proxy_user": accounts["proxy"]["name"],
        "vm_unit": units["vm"], "proxy_unit": units["proxy"],
        "workspace_unit": units["workspace"], "metrics_unit": units["metrics"],
    }
    for field, value in expected.items():
        if getattr(instance, field) != value:
            raise Refused(f"profile mapping {field.replace('_', ' ')} is not its derived identity")
    return instance


def _check_protected_directory(path: pathlib.Path, *, mode: int,
                               gid: int = 0) -> os.stat_result:
    try:
        info = os.lstat(path)
    except OSError as error:
        raise Refused("protected profile mapping directory is unavailable") from error
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != 0 or info.st_gid != gid or stat.S_IMODE(info.st_mode) != mode):
        raise Refused("protected profile mapping directory has unsafe identity or mode")
    return info


def load_mapping(name: str, *, expected_owner_uid: int | None = None,
                 require_ready: bool = False) -> tuple[dict, object]:
    """Read one root-protected mapping without following its final path."""
    policy = _instance_policy()
    if not isinstance(name, str) or not re.fullmatch(policy.PROFILE_NAME, name) or name in policy.RESERVED_NAMES:
        raise Refused("profile selector is outside its allowed grammar")
    root = MAPPING_PATH_ROOT
    try:
        control_gid = grp.getgrnam("lm-vm-control").gr_gid
    except KeyError as error:
        raise Refused("fixed profile control group is unavailable") from error
    _check_protected_directory(root.parent, mode=0o750, gid=control_gid)
    _check_protected_directory(root, mode=0o700)
    profile_dir = root / name
    _check_protected_directory(profile_dir, mode=0o700)
    path = profile_dir / "profile.json"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise Refused("protected profile mapping file is unavailable") from error
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or info.st_size <= 0 or info.st_size > MAX_MAPPING_BYTES):
            raise Refused("protected profile mapping file has unsafe identity, mode, or size")
        payload = bytearray()
        while block := os.read(fd, min(8192, MAX_MAPPING_BYTES + 1 - len(payload))):
            payload.extend(block)
            if len(payload) > MAX_MAPPING_BYTES:
                raise Refused("protected profile mapping exceeds its size limit")
    finally:
        os.close(fd)
    try:
        record = json.loads(payload, object_pairs_hook=_reject_duplicate_pairs)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise Refused("protected profile mapping is malformed JSON") from error
    record = validate_mapping(record, expected_name=name,
                              expected_owner_uid=expected_owner_uid,
                              require_ready=require_ready)
    return record, instance_from_mapping(record, expected_name=name,
                                         expected_owner_uid=expected_owner_uid,
                                         require_ready=require_ready)


def atomic_write_mapping(path: pathlib.Path, value: dict, *,
                         expected_existing: tuple[int, int] | None = None) -> None:
    """Publish validated secret-free mapping bytes by same-directory replace."""
    record = validate_mapping(value)
    instance_from_mapping(record)
    if path != MAPPING_PATH_ROOT / record["name"] / "profile.json":
        raise Refused("profile mapping publication path is not derived from its identity")
    if os.geteuid() != 0:
        raise Refused("profile mapping publication is root-only")
    directory = path.parent
    _check_protected_directory(directory, mode=0o700)
    payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(payload) > MAX_MAPPING_BYTES:
        raise Refused("profile mapping exceeds its size limit")
    temp_name = f".profile-{uuid.uuid4().hex}.tmp"
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        try:
            current = os.stat("profile.json", dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if expected_existing is None:
            if current is not None:
                raise Refused("preserving pre-existing profile mapping")
        elif (current is None or not stat.S_ISREG(current.st_mode)
              or stat.S_ISLNK(current.st_mode) or current.st_uid != 0 or current.st_gid != 0
              or stat.S_IMODE(current.st_mode) != 0o600 or current.st_nlink != 1
              or (current.st_dev, current.st_ino) != expected_existing):
            raise Refused("profile mapping identity changed before atomic update")
        fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=directory_fd)
        try:
            view = memoryview(payload)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise Refused("profile mapping write made no progress")
                view = view[count:]
            os.fchown(fd, 0, 0)
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        if expected_existing is None:
            os.link(temp_name, "profile.json", src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd, follow_symlinks=False)
            os.unlink(temp_name, dir_fd=directory_fd)
        else:
            os.replace(temp_name, "profile.json", src_dir_fd=directory_fd,
                       dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


if __name__ == "__main__" and sys.argv[1:] == ["--schema-json"]:
    print(json.dumps(mapping_schema(), sort_keys=True, indent=2))
