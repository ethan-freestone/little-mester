#!/usr/bin/env python3
"""Authoritative supported OpenCode CLI version for managed VM integrations."""

import re

SUPPORTED_VERSION = "2.2.5"
if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
                    SUPPORTED_VERSION):
    raise RuntimeError("supported OpenCode version must be a stable exact semver")
PACKAGE_NAME = "@opencode/cli"
EXPECTED_EXECUTABLE_OUTPUT = f"opencode v{SUPPORTED_VERSION}"
PACKAGE_SPEC = f"{PACKAGE_NAME}@{SUPPORTED_VERSION}"
