#!/usr/bin/env python3
"""Production API tests for the inert profile-mapping v1 parser."""

from __future__ import annotations

import copy
from contextlib import ExitStack
import importlib.util
import json
import os
import socket
import subprocess
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MAPPING_PATH = ROOT / "lib/vm/profile-control-mapping.py"
FIXTURE_DIR = ROOT / "tests/fixtures/vm-profile-schema-v1"
SCHEMA_PATH = ROOT / "docs/schemas/vm-profile-v1.schema.json"
RESOURCES_PATH = ROOT / "docs/schemas/vm-profile-resources-v1.json"


def load_python_file(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load test module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MAPPING = load_python_file("profile_control_mapping", MAPPING_PATH)
SCHEMA_FIXTURE_CHECKER = load_python_file(
    "vm_profile_schema_fixture_checker", ROOT / "tests/vm-profile-schema-fixtures.test.py"
)
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
RESOURCES = json.loads(RESOURCES_PATH.read_text(encoding="utf-8"))


def fixture_value(filename: str):
    return json.loads((FIXTURE_DIR / filename).read_text(encoding="utf-8"))


def encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def parse_value(value: dict):
    raw = encoded(value)
    return MAPPING.parse_profile_mapping(
        raw, expected_owner_uid=value["owner_uid"], expected_name=value["name"]
    )


def rekey_ready_profile(profile: dict, owner_uid: int) -> None:
    profile["owner_uid"] = owner_uid
    name = profile["name"]
    digest = MAPPING.profile_digest(owner_uid, name)
    for role in ("vm", "proxy"):
        account = profile["accounts"][role]
        account["name"] = f"{'lmv' if role == 'vm' else 'lmp'}-{digest}"
        group = profile["installation"]["groups"][role]
        group["name"] = account["name"]
        provenance = group["provenance"]
        provenance.update(
            {
                "owner_uid": owner_uid,
                "profile_name": name,
                "role": role,
                "group_name": account["name"],
                "gid": group["gid"],
            }
        )
    profile["units"] = {
        "vm": f"little-mester-vm-p-{digest}.service",
        "proxy": f"little-mester-vm-p-{digest}-proxy.service",
        "workspace": f"little-mester-vm-p-{digest}-workspace.service",
        "metrics": f"little-mester-vm-p-{digest}-metrics.service",
    }


def mutate_ready(**changes):
    profile = fixture_value("valid-ready.json")
    for dotted, value in changes.items():
        target = profile
        parts = dotted.split(".")
        for part in parts[:-1]:
            target = target[int(part)] if part.isdecimal() else target[part]
        target[parts[-1]] = value
    return profile


class ProfileMappingFixtureTests(unittest.TestCase):
    def test_every_checked_in_mapping_fixture_uses_the_production_api(self) -> None:
        fixtures = sorted(
            path
            for path in FIXTURE_DIR.glob("*.json")
            if path.name.startswith(("valid-", "invalid-"))
        )
        self.assertGreater(len(fixtures), 0)
        self.assertEqual(len(fixtures), len(list(FIXTURE_DIR.glob("valid-*.json"))) + len(list(FIXTURE_DIR.glob("invalid-*.json"))))
        for path in fixtures:
            raw = path.read_bytes()
            expected = json.loads(raw.decode("utf-8"))
            before = raw[:]
            with self.subTest(fixture=path.name):
                if path.name.startswith("valid-"):
                    result = MAPPING.parse_profile_mapping(
                        raw,
                        expected_owner_uid=expected["owner_uid"],
                        expected_name=expected["name"],
                    )
                    self.assertEqual(result, expected)
                    self.assertIsNot(result, expected)
                else:
                    with self.assertRaises(MAPPING.MappingRefused):
                        MAPPING.parse_profile_mapping(
                            raw,
                            expected_owner_uid=expected["owner_uid"],
                            expected_name=expected["name"],
                        )
                self.assertEqual(raw, before)

    def test_corrective_publication_negatives_are_schema_valid_but_refused(self) -> None:
        names = (
            "invalid-installing-partial-both-present.json",
            "invalid-installing-partial-missing-inode.json",
            "invalid-installing-partial-foreign-stage.json",
            "invalid-installing-partial-foreign-final.json",
            "invalid-removing-partial-final-foreign-stage.json",
            "invalid-removing-partial-both-intents.json",
            "invalid-removing-published-reload-without-fragment-intent.json",
            "invalid-removing-prepared-foreign-final-manager.json",
            "invalid-removing-prepared-published-stage.json",
            "invalid-removing-accounts-planned-intent.json",
            "invalid-installing-verified-missing-manager.json",
            "invalid-installing-verified-absent-manager.json",
            "invalid-resetting-incomplete-source.json",
        )
        for filename in names:
            value = fixture_value(filename)
            with self.subTest(fixture=filename):
                SCHEMA_FIXTURE_CHECKER.validate_profile_schema(value)
                with self.assertRaises(MAPPING.MappingRefused):
                    parse_value(value)

    def test_returned_record_is_complete_and_separate_from_each_parse(self) -> None:
        raw = (FIXTURE_DIR / "valid-ready.json").read_bytes()
        first = MAPPING.parse_profile_mapping(
            raw, expected_owner_uid=1000, expected_name="default"
        )
        second = MAPPING.parse_profile_mapping(
            raw, expected_owner_uid=1000, expected_name="default"
        )
        self.assertIsNot(first, second)
        self.assertIsNot(first["accounts"], second["accounts"])
        first["accounts"]["vm"]["uid"] = 7
        self.assertEqual(second["accounts"]["vm"]["uid"], 995)


class ProfileMappingIdentityTests(unittest.TestCase):
    def test_normative_profile_digest_vectors(self) -> None:
        vectors = (
            (1000, "default", "8b95a12479c85e8b1338"),
            (1000, "work", "1c04efbef58307f16a27"),
            (65534, "a", "d2c5616a3545a5ad0e4b"),
        )
        for owner_uid, name, digest in vectors:
            with self.subTest(owner_uid=owner_uid, name=name):
                self.assertEqual(MAPPING.profile_digest(owner_uid, name), digest)

    def test_expected_identity_arguments_are_exact_and_match_record(self) -> None:
        raw = (FIXTURE_DIR / "valid-ready.json").read_bytes()
        for owner_uid, name in ((1001, "default"), (1000, "work"), (True, "default"), (1000, "system")):
            with self.subTest(owner_uid=owner_uid, name=name):
                with self.assertRaises(MAPPING.MappingRefused):
                    MAPPING.parse_profile_mapping(
                        raw, expected_owner_uid=owner_uid, expected_name=name
                    )
        for owner_uid, name in ((True, "default"), (0, "default"), (2147483648, "default"), (1000, "system"), (1000, "x" * 33)):
            with self.subTest(owner_uid=owner_uid, name=name):
                with self.assertRaises(MAPPING.MappingRefused):
                    MAPPING.profile_digest(owner_uid, name)

    def test_reserved_names_and_invalid_profile_grammar_refuse(self) -> None:
        for name in sorted(MAPPING.RESERVED_NAMES):
            with self.subTest(name=name), self.assertRaises(MAPPING.MappingRefused):
                MAPPING.profile_digest(1000, name)
        for name in ("", "A", "0x", "a/../b", "_name", "a" * 33, "café"):
            with self.subTest(name=name), self.assertRaises(MAPPING.MappingRefused):
                MAPPING.profile_digest(1000, name)

    def test_full_lowercase_sha_lengths_only(self) -> None:
        base = fixture_value("valid-ready.json")
        for sha in ("a" * 40, "b" * 64):
            value = copy.deepcopy(base)
            value["runtime"]["commit_sha"] = sha
            self.assertEqual(parse_value(value)["runtime"]["commit_sha"], sha)
        for sha in ("a" * 39, "a" * 41, "a" * 63, "a" * 65, "A" * 40, "g" * 40):
            value = copy.deepcopy(base)
            value["runtime"]["commit_sha"] = sha
            with self.subTest(length=len(sha), prefix=sha[:1]), self.assertRaises(MAPPING.MappingRefused):
                parse_value(value)

    def test_uid_and_gid_are_distinct_namespaces_but_collisions_within_each_refuse(self) -> None:
        value = fixture_value("valid-ready.json")
        value["accounts"]["vm"]["uid"] = 997
        value["accounts"]["proxy"]["gid"] = 997
        value["installation"]["groups"]["proxy"]["gid"] = 997
        value["installation"]["groups"]["proxy"]["provenance"]["gid"] = 997
        self.assertEqual(parse_value(value)["accounts"]["vm"]["uid"], 997)

        colliding_uid = copy.deepcopy(value)
        colliding_uid["accounts"]["vm"]["uid"] = colliding_uid["accounts"]["proxy"]["uid"]
        with self.assertRaises(MAPPING.MappingRefused):
            parse_value(colliding_uid)
        colliding_gid = copy.deepcopy(value)
        colliding_gid["accounts"]["vm"]["gid"] = colliding_gid["accounts"]["proxy"]["gid"]
        colliding_gid["installation"]["groups"]["vm"]["gid"] = colliding_gid["accounts"]["vm"]["gid"]
        colliding_gid["installation"]["groups"]["vm"]["provenance"]["gid"] = colliding_gid["accounts"]["vm"]["gid"]
        with self.assertRaises(MAPPING.MappingRefused):
            parse_value(colliding_gid)

    def test_replan_chain_must_be_continuous_and_match_current_planned_ids(self) -> None:
        valid = fixture_value("valid-installing.json")
        valid["accounts"]["proxy"].update(uid=1200, gid=1201)
        proxy_group = valid["installation"]["groups"]["proxy"]
        proxy_group["gid"] = 1201
        proxy_group["provenance"]["gid"] = 1201
        valid["transaction"]["numeric_replans"] = [
            {"role": "proxy", "field": "uid", "previous": 996, "replacement": 1100, "reason": "numeric_collision"},
            {"role": "proxy", "field": "uid", "previous": 1100, "replacement": 1200, "reason": "numeric_collision"},
            {"role": "proxy", "field": "gid", "previous": 996, "replacement": 1201, "reason": "numeric_collision"},
        ]
        self.assertEqual(parse_value(valid)["accounts"]["proxy"]["uid"], 1200)

        discontinuous = copy.deepcopy(valid)
        discontinuous["transaction"]["numeric_replans"][1]["previous"] = 1099
        with self.assertRaises(MAPPING.MappingRefused):
            parse_value(discontinuous)
        stale_current = copy.deepcopy(valid)
        stale_current["accounts"]["proxy"]["uid"] = 1201
        with self.assertRaises(MAPPING.MappingRefused):
            parse_value(stale_current)

    def test_expected_object_and_replan_array_limits_refuse_overflow(self) -> None:
        expected_overflow = fixture_value("valid-installing.json")
        expected_overflow["transaction"]["expected"] *= 17
        self.assertGreater(len(expected_overflow["transaction"]["expected"]), MAPPING.MAX_EXPECTED_OBJECTS)
        with self.assertRaises(MAPPING.MappingRefused):
            parse_value(expected_overflow)

        replan_overflow = fixture_value("valid-installing.json")
        replan_overflow["transaction"]["numeric_replans"] = [
            {"role": "proxy", "field": "uid", "previous": 996, "replacement": 1100 + index, "reason": "numeric_collision"}
            for index in range(MAPPING.MAX_NUMERIC_REPLANS + 1)
        ]
        with self.assertRaises(MAPPING.MappingRefused):
            parse_value(replan_overflow)


class ProfileMappingRawParserTests(unittest.TestCase):
    def assert_refused(self, raw: object, owner_uid: int = 1000, name: str = "default") -> None:
        with self.assertRaises(MAPPING.MappingRefused) as caught:
            MAPPING.parse_profile_mapping(
                raw, expected_owner_uid=owner_uid, expected_name=name  # type: ignore[arg-type]
            )
        self.assertLessEqual(len(str(caught.exception)), 80)
        self.assertNotIn("little-mester-vm-p-", str(caught.exception))

    def test_bytes_type_size_boundary_and_input_immutability(self) -> None:
        raw = (FIXTURE_DIR / "valid-ready.json").read_bytes()
        self.assert_refused(b"")
        self.assert_refused(bytearray(raw))
        self.assert_refused(memoryview(raw))
        self.assert_refused(raw.decode("utf-8"))
        padded = raw + (b" " * (MAPPING.MAX_MAPPING_BYTES - len(raw)))
        self.assertEqual(len(padded), MAPPING.MAX_MAPPING_BYTES)
        before = padded[:]
        self.assertEqual(
            MAPPING.parse_profile_mapping(
                padded, expected_owner_uid=1000, expected_name="default"
            )["name"],
            "default",
        )
        self.assertEqual(padded, before)
        self.assert_refused(padded + b" ")

    def test_strict_utf8_single_value_and_malformed_json(self) -> None:
        for raw in (
            b"\xff",
            b"{",
            b"{}{}",
            b"{} trailing",
            b'{"x":"\\q"}',
        ):
            with self.subTest(raw=raw[:12]):
                self.assert_refused(raw)

    def test_recursive_duplicate_keys_and_nonfinite_or_overflowing_numbers(self) -> None:
        for raw in (
            b'{"outer":{"key":1,"key":2}}',
            b'{"workspace":NaN}',
            b'{"workspace":Infinity}',
            b'{"workspace":-Infinity}',
            b'{"workspace":1e999}',
        ):
            with self.subTest(raw=raw), self.assertRaises(MAPPING.MappingRefused):
                MAPPING.parse_profile_mapping(
                    raw, expected_owner_uid=1000, expected_name="default"
                )

    def test_container_depth_and_quoted_escaped_braces(self) -> None:
        MAPPING._scan_nesting("[" * 16 + "0" + "]" * 16)
        with self.assertRaises(MAPPING._InvalidMapping):
            MAPPING._scan_nesting("[" * 17 + "0" + "]" * 17)
        MAPPING._scan_nesting(json.dumps({"text": "braces { [ ] } and quote: \\\""}))

        value = fixture_value("valid-ready.json")
        value["workspace"]["canonical_path"] = '/workspace/quoted{"nested":[\\"literal"]}'
        parsed = parse_value(value)
        self.assertEqual(parsed["workspace"]["canonical_path"], value["workspace"]["canonical_path"])

    def test_invalid_unicode_scalars_and_controls_refuse_without_echo(self) -> None:
        raw = (FIXTURE_DIR / "valid-ready.json").read_bytes()
        surrogate = raw.replace(b"/workspaces/alpha", b"/workspaces/\\ud800")
        control = raw.replace(b"/workspaces/alpha", b"/workspaces/\\u0001")
        for candidate in (surrogate, control):
            with self.assertRaises(MAPPING.MappingRefused) as caught:
                MAPPING.parse_profile_mapping(
                    candidate, expected_owner_uid=1000, expected_name="default"
                )
            self.assertNotIn("workspaces", str(caught.exception))

    def test_exact_integer_types_and_schema_boundaries(self) -> None:
        base = fixture_value("valid-ready.json")
        bad_values = (
            ("schema_version", True),
            ("schema_version", 1.0),
            ("owner_uid", True),
            ("owner_uid", 1000.0),
            ("ssh_port", True),
            ("ssh_port", 22300.0),
            ("runtime.profile_runtime_api", True),
            ("runtime.profile_runtime_api", 1.0),
            ("workspace.device", True),
            ("workspace.device", 2049.0),
            ("workspace.inode", 12345.0),
            ("workspace.mount_id", True),
            ("workspace.mount.major", 8.0),
            ("workspace.mount.minor", False),
        )
        for dotted, invalid in bad_values:
            value = copy.deepcopy(base)
            target = value
            parts = dotted.split(".")
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = invalid
            with self.subTest(field=dotted, invalid=invalid), self.assertRaises(MAPPING.MappingRefused):
                parse_value(value)

        maximum = copy.deepcopy(base)
        rekey_ready_profile(maximum, MAPPING.MAX_IDENTIFIER - 1)
        maximum["accounts"]["vm"].update(uid=MAPPING.MAX_IDENTIFIER, gid=MAPPING.MAX_IDENTIFIER)
        maximum["accounts"]["proxy"].update(uid=1, gid=MAPPING.MAX_IDENTIFIER - 1)
        for role in ("vm", "proxy"):
            group = maximum["installation"]["groups"][role]
            group["gid"] = maximum["accounts"][role]["gid"]
            group["provenance"]["gid"] = group["gid"]
        maximum["workspace"]["device"] = MAPPING.MAX_DEVICE_OR_INODE
        maximum["workspace"]["inode"] = MAPPING.MAX_DEVICE_OR_INODE
        maximum["workspace"]["mount_id"] = MAPPING.MAX_IDENTIFIER
        maximum["workspace"]["mount"]["major"] = 2147483647
        maximum["workspace"]["mount"]["minor"] = MAPPING.MAX_MOUNT_COMPONENT
        maximum["runtime"].update(
            profile_runtime_api=65535,
            control_helper_api_min=65535,
            control_helper_api_max=65535,
        )
        maximum["ssh_port"] = 23222
        self.assertEqual(
            MAPPING.parse_profile_mapping(
                encoded(maximum),
                expected_owner_uid=maximum["owner_uid"],
                expected_name=maximum["name"],
            )["ssh_port"],
            23222,
        )

        overflow_cases = (
            ("owner_uid", MAPPING.MAX_IDENTIFIER + 1),
            ("runtime.profile_runtime_api", 65536),
            ("workspace.device", MAPPING.MAX_DEVICE_OR_INODE + 1),
            ("workspace.inode", MAPPING.MAX_DEVICE_OR_INODE + 1),
            ("workspace.mount_id", MAPPING.MAX_IDENTIFIER + 1),
            ("workspace.mount.major", MAPPING.MAX_MOUNT_COMPONENT + 1),
            ("workspace.mount.minor", MAPPING.MAX_MOUNT_COMPONENT + 1),
            ("ssh_port", 23223),
        )
        for dotted, invalid in overflow_cases:
            value = copy.deepcopy(base)
            target = value
            parts = dotted.split(".")
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = invalid
            with self.subTest(field=dotted, invalid=invalid), self.assertRaises(MAPPING.MappingRefused):
                parse_value(value)

    def test_transaction_and_identity_integer_bounds_are_exact(self) -> None:
        base = fixture_value("valid-installing.json")
        for field, invalid in (("schema_version", True), ("step", 5), ("step", 1.0)):
            value = copy.deepcopy(base)
            value["transaction"][field] = invalid
            with self.subTest(field=field, invalid=invalid), self.assertRaises(MAPPING.MappingRefused):
                parse_value(value)

        # Exact device/inode maxima are valid for an observed regular file and
        # its manager identity; the directory's link count remains unspecified.
        publication = fixture_value("valid-installing-partial-publication.json")
        for item in publication["transaction"]["expected"]:
            if item["selector"] == "unit-file.vm":
                item["identity"].update(
                    device=MAPPING.MAX_DEVICE_OR_INODE,
                    inode=MAPPING.MAX_DEVICE_OR_INODE,
                )
            elif item["selector"] == "unit-manager.vm":
                item["identity"].update(
                    fragment_device=MAPPING.MAX_DEVICE_OR_INODE,
                    fragment_inode=MAPPING.MAX_DEVICE_OR_INODE,
                )
        for identity_record in (
            publication["transaction"]["unit_publication"]["vm"]["fragment_identity"],
            publication["installation"]["unit_publication"]["vm"]["fragment_identity"],
        ):
            identity_record.update(
                device=MAPPING.MAX_DEVICE_OR_INODE,
                inode=MAPPING.MAX_DEVICE_OR_INODE,
            )
        self.assertEqual(parse_value(publication)["state"], "installing")

    def test_workspace_device_path_and_mount_spelling_are_lexical_only(self) -> None:
        base = fixture_value("valid-ready.json")
        bad_device = copy.deepcopy(base)
        bad_device["workspace"]["device"] += 1
        with self.assertRaises(MAPPING.MappingRefused):
            parse_value(bad_device)
        for path in (
            "relative/workspace",
            "/workspaces//alpha",
            "/workspaces/./alpha",
            "/workspaces/../etc",
            "/workspaces/alpha/",
        ):
            value = copy.deepcopy(base)
            value["workspace"]["canonical_path"] = path
            with self.subTest(path=path), self.assertRaises(MAPPING.MappingRefused):
                parse_value(value)
        wrong_spelling = copy.deepcopy(base)
        mount = wrong_spelling["workspace"]["mount"]
        mount["mountpoint"] = mount.pop("mount_point")
        with self.assertRaises(MAPPING.MappingRefused):
            parse_value(wrong_spelling)

    def test_remove_source_id_must_match_retained_install_record(self) -> None:
        value = fixture_value("valid-removing.json")
        value["transaction"]["source_install_id"] = "9" * 32
        with self.assertRaises(MAPPING.MappingRefused):
            parse_value(value)

    def test_install_published_manager_evidence_is_required_for_each_role_and_phase(self) -> None:
        for phase, step in (("units", 3), ("verified", 4)):
            for role in MAPPING.UNIT_ROLES:
                for mutation in ("missing", "expected_absent", "foreign"):
                    value = fixture_value("valid-installing-verified.json")
                    value["transaction"]["phase"] = phase
                    value["transaction"]["step"] = step
                    if phase == "units":
                        publication = value["installation"]["unit_publication"]
                        for index, published_role in enumerate(MAPPING.UNIT_ROLES):
                            if index > MAPPING.UNIT_ROLES.index(role):
                                publication[published_role] = {"state": "not_started"}
                        value["transaction"]["unit_publication"] = copy.deepcopy(publication)
                        value["transaction"]["expected"] = [
                            item for item in value["transaction"]["expected"]
                            if item["selector"].startswith(("unit-file.", "unit-manager."))
                            and item["selector"].rsplit(".", 1)[1]
                            in MAPPING.UNIT_ROLES[: MAPPING.UNIT_ROLES.index(role) + 1]
                        ]
                    expected = value["transaction"]["expected"]
                    manager = next(
                        item for item in expected
                        if item["selector"] == f"unit-manager.{role}"
                    )
                    if mutation == "missing":
                        expected.remove(manager)
                    elif mutation == "expected_absent":
                        manager.clear()
                        manager.update({"selector": f"unit-manager.{role}", "evidence": "expected_absent"})
                    else:
                        manager["identity"]["fragment_inode"] += 100
                    with self.subTest(phase=phase, role=role, mutation=mutation):
                        SCHEMA_FIXTURE_CHECKER.validate_profile_schema(value)
                        with self.assertRaises(MAPPING.MappingRefused):
                            parse_value(value)

    def test_reset_requires_complete_retained_source_in_every_reset_phase(self) -> None:
        phases = (("prepared", 0), ("leaves", 1), ("verified", 2))
        for phase, step in phases:
            for category, roles in (
                ("account", MAPPING.ACCOUNT_ROLES),
                ("group", MAPPING.ACCOUNT_ROLES),
                ("publication", MAPPING.UNIT_ROLES),
            ):
                for role in roles:
                    value = fixture_value("valid-resetting.json")
                    value["transaction"]["phase"] = phase
                    value["transaction"]["step"] = step
                    if phase == "prepared":
                        value["transaction"]["expected"] = []
                    if category == "account":
                        value["accounts"][role]["allocation"] = "planned"
                    elif category == "group":
                        value["installation"]["groups"][role]["state"] = "not_started"
                    else:
                        value["installation"]["unit_publication"][role] = {"state": "not_started"}
                    with self.subTest(phase=phase, category=category, role=role):
                        SCHEMA_FIXTURE_CHECKER.validate_profile_schema(value)
                        with self.assertRaises(MAPPING.MappingRefused):
                            parse_value(value)

    def test_account_removal_intent_binds_committed_account_and_owned_group_source(self) -> None:
        for phase, step in (("accounts", 3), ("finalizing", 4)):
            for role in MAPPING.ACCOUNT_ROLES:
                base = fixture_value("valid-removing.json")
                base["transaction"]["phase"] = phase
                base["transaction"]["step"] = step
                account = base["accounts"][role]
                intent = {
                    "selector": f"account.{role}",
                    "evidence": "removal_intent",
                    "identity": {
                        "uid": account["uid"],
                        "gid": account["gid"],
                        "marker": f"little-mester-vm-profile-v1/1000/default/{role}",
                    },
                }
                base["transaction"]["expected"].append(copy.deepcopy(intent))
                with self.subTest(phase=phase, role=role, case="owned-group-retained-without-group-intent"):
                    self.assertNotIn(
                        f"group.{role}",
                        {item["selector"] for item in base["transaction"]["expected"]},
                    )
                    SCHEMA_FIXTURE_CHECKER.validate_profile_schema(base)
                    self.assertEqual(parse_value(base)["state"], "removing")

                planned = copy.deepcopy(base)
                planned["accounts"][role]["allocation"] = "planned"
                with self.subTest(phase=phase, role=role, case="planned"):
                    SCHEMA_FIXTURE_CHECKER.validate_profile_schema(planned)
                    with self.assertRaises(MAPPING.MappingRefused):
                        parse_value(planned)

                planned_observation = copy.deepcopy(planned)
                next(
                    item for item in planned_observation["transaction"]["expected"]
                    if item["selector"] == f"account.{role}"
                )["evidence"] = "observed_present"
                with self.subTest(phase=phase, role=role, case="planned-observed-present"):
                    SCHEMA_FIXTURE_CHECKER.validate_profile_schema(planned_observation)
                    with self.assertRaises(MAPPING.MappingRefused):
                        parse_value(planned_observation)

                missing_group = copy.deepcopy(base)
                missing_group["installation"]["groups"][role]["state"] = "not_started"
                with self.subTest(phase=phase, role=role, case="missing-owned-group"):
                    SCHEMA_FIXTURE_CHECKER.validate_profile_schema(missing_group)
                    with self.assertRaises(MAPPING.MappingRefused):
                        parse_value(missing_group)

                mismatched_group = copy.deepcopy(base)
                group = mismatched_group["installation"]["groups"][role]
                wrong_identity = {
                    "name": group["name"],
                    "gid": group["gid"],
                    "provenance": copy.deepcopy(group["provenance"]),
                }
                wrong_identity["provenance"]["origin_install_transaction_id"] = "9" * 32
                mismatched_group["transaction"]["expected"].append({
                    "selector": f"group.{role}",
                    "evidence": "observed_present",
                    "identity": wrong_identity,
                })
                with self.subTest(phase=phase, role=role, case="mismatched-group-provenance"):
                    SCHEMA_FIXTURE_CHECKER.validate_profile_schema(mismatched_group)
                    with self.assertRaises(MAPPING.MappingRefused):
                        parse_value(mismatched_group)

    def test_remove_source_consistency_covers_each_phase_role_and_publication_state(self) -> None:
        phases = (("prepared", 0), ("units", 1), ("state", 2), ("accounts", 3), ("finalizing", 4))
        roles = MAPPING.UNIT_ROLES

        def file_identity(device: int, inode: int) -> dict:
            return {"device": device, "inode": inode, "kind": "regular", "link_count": 1}

        def manager_identity(device: int, inode: int) -> dict:
            return {
                "fragment_device": device,
                "fragment_inode": inode,
                "load_state": "loaded",
                "active_state": "inactive",
                "job_state": "none",
            }

        for phase, step in phases:
            for index, role in enumerate(roles):
                for state in ("not_started", "publication_intent", "published"):
                    profile = fixture_value("valid-removing.json")
                    tx = profile["transaction"]
                    tx["phase"], tx["step"] = phase, step
                    if phase == "prepared":
                        tx["expected"] = []
                    if state == "not_started":
                        record = {"state": "not_started"}
                        retained = file_identity(8, 900 + index)
                    elif state == "publication_intent":
                        retained = file_identity(8, 800 + index)
                        record = {"state": "publication_intent", "stage_identity": copy.deepcopy(retained)}
                    else:
                        record = copy.deepcopy(profile["installation"]["unit_publication"][role])
                        retained = record["fragment_identity"]
                    profile["installation"]["unit_publication"][role] = copy.deepcopy(record)
                    tx["unit_publication"][role] = copy.deepcopy(record)
                    target_selectors = {f"unit-file.{role}", f"unit-stage.{role}", f"unit-manager.{role}"}
                    tx["expected"][:] = [item for item in tx["expected"] if item["selector"] not in target_selectors]
                    foreign = file_identity(retained["device"], retained["inode"] + 101)
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
                             "identity": manager_identity(foreign["device"], foreign["inode"])},
                        ],
                        "paired-foreign-file-manager": [
                            {"selector": f"unit-file.{role}", "evidence": "observed_present", "identity": foreign},
                            {"selector": f"unit-manager.{role}", "evidence": "observed_present", "identity": manager_identity(foreign["device"], foreign["inode"])},
                        ],
                    }
                    for case, items in cases.items():
                        value = copy.deepcopy(profile)
                        value["transaction"]["expected"].extend(copy.deepcopy(items))
                        with self.subTest(phase=phase, role=role, state=state, case=case):
                            SCHEMA_FIXTURE_CHECKER.validate_profile_schema(value)
                            with self.assertRaises(MAPPING.MappingRefused):
                                parse_value(value)

                    if state == "publication_intent":
                        alternate = copy.deepcopy(profile)
                        alternate["transaction"]["expected"].extend([
                            {"selector": f"unit-stage.{role}", "evidence": "observed_present", "identity": copy.deepcopy(retained)},
                            {"selector": f"unit-file.{role}", "evidence": "observed_present", "identity": copy.deepcopy(retained)},
                        ])
                        with self.subTest(phase=phase, role=role, state=state, case="identity-bearing-alternates"):
                            SCHEMA_FIXTURE_CHECKER.validate_profile_schema(alternate)
                            with self.assertRaises(MAPPING.MappingRefused):
                                parse_value(alternate)

                    if state == "published":
                        staged = copy.deepcopy(profile)
                        staged["transaction"]["expected"].append({
                            "selector": f"unit-stage.{role}",
                            "evidence": "observed_present",
                            "identity": copy.deepcopy(retained),
                        })
                        with self.subTest(phase=phase, role=role, state=state, case="published-stage-identity"):
                            SCHEMA_FIXTURE_CHECKER.validate_profile_schema(staged)
                            with self.assertRaises(MAPPING.MappingRefused):
                                parse_value(staged)


class ProfileMappingNoEffectsTests(unittest.TestCase):
    def test_workspace_path_is_inert_even_when_missing_or_shell_looking(self) -> None:
        value = fixture_value("valid-ready.json")
        hostile = "/definitely-missing/hostile;$(touch-do-not-run)/child"
        value["workspace"].update(canonical_path=hostile, inode=999999999)
        value["workspace"]["mount"].update(root="/missing/root", mount_point="/")
        raw = encoded(value)

        fail = AssertionError("parser attempted an external side effect")
        forbidden = (
            "builtins.open",
            "os.stat",
            "os.lstat",
            "os.open",
            "os.mkdir",
            "os.makedirs",
            "os.remove",
            "os.unlink",
            "os.rename",
            "os.replace",
            "os.listdir",
            "os.scandir",
            "os.readlink",
            "os.access",
            "os.system",
            "os.path.realpath",
            "subprocess.run",
            "subprocess.Popen",
            "subprocess.check_call",
            "subprocess.check_output",
            "socket.socket",
            "socket.create_connection",
            "urllib.request.urlopen",
        )
        with ExitStack() as stack:
            for target in forbidden:
                stack.enter_context(mock.patch(target, side_effect=fail))
            for method in ("resolve", "stat", "exists", "open"):
                stack.enter_context(mock.patch.object(Path, method, side_effect=fail))
            parsed = MAPPING.parse_profile_mapping(
                raw, expected_owner_uid=1000, expected_name="default"
            )
        self.assertEqual(parsed["workspace"]["canonical_path"], hostile)

    def test_mapping_module_has_no_path_or_identity_authorization_api(self) -> None:
        self.assertEqual(
            set(MAPPING.parse_profile_mapping.__annotations__),
            {"raw", "expected_owner_uid", "expected_name", "return"},
        )
        self.assertEqual(
            set(MAPPING.profile_digest.__annotations__), {"owner_uid", "name", "return"}
        )
        self.assertNotIn("authorize_owner", vars(MAPPING))


class ProfileMappingContractCrossChecks(unittest.TestCase):
    @staticmethod
    def schema_node(path: str):
        node = SCHEMA
        for part in path.split("."):
            node = node[int(part)] if part.isdecimal() else node[part]
        return node

    def test_production_resource_kinds_and_allowlists_match_contract_artifact(self) -> None:
        artifact_kinds = {item["selector"]: item["kind"] for item in RESOURCES["resources"]}
        self.assertEqual(dict(MAPPING.RESOURCE_KINDS), artifact_kinds)
        for operation, selectors in RESOURCES["transaction_allowlists"].items():
            self.assertEqual(tuple(MAPPING.TRANSACTION_ALLOWLISTS[operation]), tuple(selectors))

        file_selectors = set(self.schema_node("$defs.fileSelector.enum"))
        expected_file_selectors = {
            selector
            for selector, kind in artifact_kinds.items()
            if kind in {"regular-file", "directory", "socket"} and selector != "profile.mapping"
        }
        self.assertEqual(file_selectors, expected_file_selectors)
        self.assertEqual(set(self.schema_node("$defs.accountSelector.enum")), {"account.vm", "account.proxy"})
        self.assertEqual(set(self.schema_node("$defs.groupSelector.enum")), {"group.vm", "group.proxy"})
        self.assertEqual(
            set(self.schema_node("$defs.managerSelector.enum")),
            {f"unit-manager.{role}" for role in MAPPING.UNIT_ROLES},
        )

    def test_production_numeric_bounds_match_schema_fields(self) -> None:
        fields = {
            "schema_version": (
                "properties.schema_version",
                "$defs.installTransaction.properties.schema_version",
                "$defs.resetTransaction.properties.schema_version",
                "$defs.removeTransaction.properties.schema_version",
                "$defs.groupProvenance.properties.schema_version",
            ),
            "owner_uid": ("properties.owner_uid", "$defs.groupProvenance.properties.owner_uid"),
            "uid_or_gid": (
                "$defs.account.properties.uid",
                "$defs.account.properties.gid",
                "$defs.accountIdentity.properties.uid",
                "$defs.accountIdentity.properties.gid",
                "$defs.groupIdentity.properties.gid",
                "$defs.groupRecord.properties.gid",
                "$defs.groupProvenance.properties.gid",
                "$defs.numericReplan.properties.previous",
                "$defs.numericReplan.properties.replacement",
            ),
            "runtime_api": (
                "$defs.runtime.properties.profile_runtime_api",
                "$defs.runtime.properties.control_helper_api_min",
                "$defs.runtime.properties.control_helper_api_max",
            ),
            "workspace_device": ("$defs.workspace.properties.device",),
            "workspace_inode": ("$defs.workspace.properties.inode",),
            "mount_id": ("$defs.workspace.properties.mount_id",),
            "mount_component": ("$defs.mount.properties.major", "$defs.mount.properties.minor"),
            "ssh_port": ("properties.ssh_port",),
            "transaction_step": (
                "$defs.installTransaction.properties.step",
                "$defs.resetTransaction.properties.step",
                "$defs.removeTransaction.properties.step",
            ),
            "object_device": (
                "$defs.fileIdentity.oneOf.0.properties.device",
                "$defs.fileIdentity.oneOf.1.properties.device",
                "$defs.managerIdentity.properties.fragment_device",
                "$defs.regularFileIdentity.properties.device",
            ),
            "object_inode": (
                "$defs.fileIdentity.oneOf.0.properties.inode",
                "$defs.fileIdentity.oneOf.1.properties.inode",
                "$defs.managerIdentity.properties.fragment_inode",
                "$defs.regularFileIdentity.properties.inode",
            ),
            "link_count": (
                "$defs.fileIdentity.oneOf.0.properties.link_count",
                "$defs.regularFileIdentity.properties.link_count",
            ),
        }
        for bound_name, paths in fields.items():
            expected = MAPPING.NUMERIC_BOUNDS[bound_name]
            for path in paths:
                with self.subTest(bound=bound_name, schema_path=path):
                    node = self.schema_node(path)
                    if "const" in node:
                        actual = (node["const"], node["const"])
                    else:
                        actual = (node["minimum"], node["maximum"])
                    self.assertEqual(actual, expected)

    def test_production_string_and_array_bounds_match_schema(self) -> None:
        fields = {
            "profile_name": "$defs.groupProvenance.properties.profile_name",
            "sha": "$defs.runtime.properties.commit_sha",
            "account_name": "$defs.account.properties.name",
            "unit_name": "$defs.unit",
            "absolute_path": "$defs.absolutePath",
            "transaction_id": "$defs.installTransaction.properties.id",
            "account_marker": "$defs.accountIdentity.properties.marker",
        }
        for bound_name, path in fields.items():
            node = self.schema_node(path)
            with self.subTest(bound=bound_name):
                self.assertEqual(
                    (node["minLength"], node["maxLength"]), MAPPING.STRING_BOUNDS[bound_name]
                )
        self.assertEqual(MAPPING.MAX_EXPECTED_OBJECTS, SCHEMA["$defs"]["installTransaction"]["properties"]["expected"]["maxItems"])
        self.assertEqual(MAPPING.MAX_EXPECTED_OBJECTS, SCHEMA["$defs"]["resetTransaction"]["properties"]["expected"]["maxItems"])
        self.assertEqual(MAPPING.MAX_EXPECTED_OBJECTS, SCHEMA["$defs"]["removeTransaction"]["properties"]["expected"]["maxItems"])
        self.assertEqual(MAPPING.MAX_NUMERIC_REPLANS, SCHEMA["$defs"]["installTransaction"]["properties"]["numeric_replans"]["maxItems"])
        self.assertEqual(MAPPING.MAX_MAPPING_BYTES, 256 * 1024)
        self.assertEqual(MAPPING.MAX_NESTING, 16)


if __name__ == "__main__":
    unittest.main(verbosity=2)
