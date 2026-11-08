#!/usr/bin/env python3
"""Plan and run one bounded, run-owned Little Mester VM acceptance campaign.

Planning is read-only. Live work uses exact-run ``--start``/``--resume`` stages,
an explicit total-time ceiling, and ``--confirm-run-id``. The one-shot ``--execute``
path is disabled. The runner never selects a pre-existing VM, installs host
packages, changes host networking, or cleans assets outside its private run
manifest. Required human observations remain BLOCKED until recorded by a person;
the local-first scope records live OpenAI auth gates as DEFERRED without action.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import functools
import hashlib
import http.client
import importlib.util
import json
import os
import platform
import re
import secrets
import select
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PATHS_PATH = ROOT / "lib/vm/paths.py"
SNAPSHOT_PATH = ROOT / "scripts/vm-recursion-workspace.py"
MEMORY_PATH = ROOT / "scripts/vm_memory_budget.py"
ISOLATION_PATH = ROOT / "scripts/vm-guest-isolation-check.py"
CANARY_CHECK_PATH = ROOT / "scripts/vm-canary-check.py"
RUN_NAME_RE = re.compile(r"accept-[a-f0-9]{12}")
PROFILE_RE = re.compile(
    r"accept-[a-f0-9]{12}-(?:outer|agent|workload|provider-(?:ollama|llamacpp|openai))"
)
PROFILE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
RUN_ID_RE = re.compile(r"[a-f0-9]{32}")
MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_PROVIDER_CONFIG_BYTES = 16 * 1024
MAX_PROVIDER_RESPONSE_BYTES = 256 * 1024
MAX_GUEST_MARKER_BYTES = 16 * 1024
WORKFLOW_VERSION = 2
DEFAULT_RESERVE_MIB = 512
PROVIDER_DISCOVERY_ROUTES = {
    "ollama": "/api/tags",
    "llamacpp": "/v1/models",
}
PROVIDER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}")
OWNER_CHECKLISTS = {
    "E2E-DAILY": (
        "native-tui-entry", "two-identical-starts-no-reboot-or-install", "A-B-B-same-overlay-and-keys",
        "warm-start-same-process-identity", "docker-rollback-without-recreation",
    ),
    "E2E-MODEL": (
        "model-none-shell-and-auth", "each-selected-local-provider-selected",
        "five-real-streams-per-selected-local-provider",
        "local-provider-denial-and-missing-model-errors", "no-model-response-or-credential-recorded",
    ),
    "E2E-WORK": (
        "guest-only-postgres-compose", "health-and-query", "failing-test-fix-pass",
        "volume-survives-stop-restart", "compose-down", "bind-and-guest-loopback-port",
        "no-host-published-service-or-socket",
    ),
    "E2E-LIFE": (
        "suspend-resume-identity", "local-db-tools-keys-workspace-retained", "update-scan-cancel-noop",
        "selected-apply-exact-version", "live-child-blocks-parent", "deepest-first-shutdown",
        "resize-memtotal-and-identity", "exact-teardown-no-residue", "base-workspace-reports-preserved",
    ),
    "E2E-AGENTS": (
        "two-authorized-same-guest-tasks", "separate-auth-ports", "clean-agent-cancellation",
        "shared-workspace-risk-explained-no-supervisor-claim",
    ),
    "SEC-FS": (
        "host-file-unreadable", "control-key-and-socket-unreadable", "child-out-of-scope-files-unreadable",
        "escaping-symlinks-refused", "state-root-overlap-blocked", "one-export-verified",
    ),
    "SEC-SECRET": (
        "canary-absent-from-snapshot-and-history", "canary-absent-from-seed-base-and-logs",
        "canary-absent-from-campaign-and-self-test-reports",
        "provider-forwarding-opt-in-only", "synthetic-private-state-persistence-and-atomic-export-import",
        "synthetic-export-failure-prevents-delete", "synthetic-refreshed-state-survives-restart-and-import",
    ),
    "HUMAN-TUI": (
        "single-command-entry", "warm-start-no-reboot", "metrics-sidebar-and-tool",
        "ctrl-c-cancel-and-confirm", "retry-rendering", "version-matched",
    ),
    "HUMAN-PERM": (
        "auto-allowed-tool", "auto-deny-preserved", "ask-mode-prompt",
        "shared-standalone-match", "argv-ui-action-recorded",
    ),
    "HUMAN-AUTH": (
        "browser-success", "cancel-interrupt-timeout-occupied-port-egress-denied",
        "auth-list-and-completion", "temporary-callback-removed", "refresh-persisted",
        "disposable-delete-reimport", "recursive-provider-import",
    ),
}
HUMAN_CHECKLISTS = {key: value for key, value in OWNER_CHECKLISTS.items() if key.startswith("HUMAN-")}
OWNER_OBSERVATION_GATES = frozenset(OWNER_CHECKLISTS)
LOCAL_FIRST_SCOPE_ID = "local-first-c14-v1"
DEFERRED_GATE_REASONS = {"HUMAN-AUTH": "owner_deferred_live_openai"}
DEFERRED_CHECKS = {
    "E2E-MODEL": ("live-openai-browser-login", "live-openai-streaming-and-paid-use",
                  "live-openai-refresh-and-expiry"),
    "E2E-LIFE": ("live-openai-auth-state-retained",),
    "SEC-FS": ("one-export-verified",),
    "SEC-SECRET": ("live-disposable-credential-atomic-export-import",
                    "live-export-failure-before-delete", "live-refreshed-credential-state-import"),
    "HUMAN-AUTH": OWNER_CHECKLISTS["HUMAN-AUTH"],
}

GATE_IDS = (
    "HOST-PREFLIGHT", "HOST-CLEAN", "E2E-DAILY", "E2E-MODEL", "E2E-WORK",
    "E2E-SELF", "E2E-DEPTH", "E2E-LIFE", "E2E-AGENTS", "SEC-FS", "SEC-NET",
    "SEC-SECRET", "HUMAN-TUI", "HUMAN-PERM", "HUMAN-AUTH",
)
SUITE_GATES = {
    "preflight": ("HOST-PREFLIGHT",),
    "clean": ("HOST-PREFLIGHT", "HOST-CLEAN"),
    "daily": ("HOST-PREFLIGHT", "HOST-CLEAN", "E2E-DAILY"),
    "model": ("HOST-PREFLIGHT", "HOST-CLEAN", "E2E-MODEL"),
    "workload": ("HOST-PREFLIGHT", "HOST-CLEAN", "E2E-WORK"),
    "boundary": ("HOST-PREFLIGHT", "HOST-CLEAN", "SEC-FS", "SEC-NET", "SEC-SECRET"),
    "lifecycle": ("HOST-PREFLIGHT", "HOST-CLEAN", "E2E-LIFE"),
    "agents": ("HOST-PREFLIGHT", "HOST-CLEAN", "E2E-AGENTS"),
    "human": ("HOST-PREFLIGHT", "HOST-CLEAN", "HUMAN-TUI", "HUMAN-PERM", "HUMAN-AUTH"),
    "self": ("HOST-PREFLIGHT", "HOST-CLEAN", "E2E-SELF", "E2E-DEPTH"),
}
ALL_SUITES = tuple(SUITE_GATES)
STAGE_ORDER = (
    "clean-usability", "early-auth", "providers-daily-permissions",
    "workload-retention-lifecycle", "nested-boundaries-agents", "final-auth", "teardown",
)
STAGE_SUITES = {
    "clean-usability": ("clean",),
    "early-auth": (),
    "providers-daily-permissions": ("model", "daily", "human"),
    "workload-retention-lifecycle": ("workload", "lifecycle"),
    "nested-boundaries-agents": ("self", "boundary", "agents"),
    "final-auth": (),
    "teardown": (),
}
STAGE_BUDGET_SECONDS = 60 * 60
TEARDOWN_RESERVE_SECONDS = 35 * 60
MAX_APPROVED_TOTAL_MINUTES = 720
_STAGE_DEADLINE: float | None = None
_STAGE_STARTED_AT: float | None = None


class AcceptanceError(ValueError):
    """Unsafe scope, unavailable precondition, or invalid evidence."""


@contextlib.contextmanager
def _stage_budget():
    global _STAGE_DEADLINE, _STAGE_STARTED_AT
    previous = _STAGE_DEADLINE
    previous_started = _STAGE_STARTED_AT
    stage_started = time.monotonic()
    requested_deadline = time.monotonic() + STAGE_BUDGET_SECONDS
    _STAGE_DEADLINE = min(previous, requested_deadline) if previous is not None else requested_deadline
    _STAGE_STARTED_AT = previous_started if previous_started is not None else stage_started
    try:
        yield
    finally:
        _STAGE_DEADLINE = previous
        _STAGE_STARTED_AT = previous_started


def _bounded_timeout(requested: int) -> int:
    if type(requested) is not int or requested < 1:
        raise AcceptanceError("operation deadline is invalid")
    if _STAGE_DEADLINE is None:
        return requested
    remaining = int(_STAGE_DEADLINE - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("automated stage exceeded its 60-minute budget")
    return min(requested, remaining)


def _tighten_stage_budget(total_remaining_seconds: int) -> None:
    global _STAGE_DEADLINE
    if type(total_remaining_seconds) is not int or total_remaining_seconds < 1:
        raise AcceptanceError("approved campaign time budget is exhausted or invalid")
    if _STAGE_DEADLINE is None or _STAGE_STARTED_AT is None:
        raise AcceptanceError("stage deadline was not initialized")
    candidate = _STAGE_STARTED_AT + total_remaining_seconds
    _STAGE_DEADLINE = min(_STAGE_DEADLINE, candidate)


def _remaining_campaign_seconds(approved_seconds: int, prior_elapsed_ms: int,
                               campaign_elapsed_ms: int, *, stage: str | None) -> int:
    if (type(approved_seconds) is not int or type(prior_elapsed_ms) is not int
            or type(campaign_elapsed_ms) is not int or prior_elapsed_ms < 0
            or campaign_elapsed_ms < 0):
        raise AcceptanceError("campaign time accounting is invalid")
    reserve = TEARDOWN_RESERVE_SECONDS if stage != "teardown" else 0
    elapsed_seconds = (prior_elapsed_ms + campaign_elapsed_ms + _stage_elapsed_ms()) // 1000
    remaining = approved_seconds - elapsed_seconds - reserve
    if remaining < 1:
        raise AcceptanceError("approved campaign budget cannot preserve the 35-minute teardown reserve")
    return remaining


def _stage_elapsed_ms() -> int:
    if _STAGE_STARTED_AT is None:
        return 0
    return max(0, int((time.monotonic() - _STAGE_STARTED_AT) * 1000))


def _bounded_stage(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        with _stage_budget():
            return function(*args, **kwargs)
    return wrapped


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AcceptanceError("acceptance dependency is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _private_directory(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700 or path.is_symlink()
            or path.resolve(strict=True) != path):
        raise AcceptanceError("state and evidence directories must be canonical, owned, private 0700 directories")


def _canonical_directory(value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise AcceptanceError("source and state-root paths must be absolute and canonical")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise AcceptanceError("source or state root is unavailable") from error
    if resolved != path or not path.is_dir() or path.is_symlink():
        raise AcceptanceError("source and state-root paths must be canonical directories")
    return path


def _run_id(value: str | None) -> str:
    selected = value or uuid.uuid4().hex
    if RUN_ID_RE.fullmatch(selected) is None:
        raise AcceptanceError("run ID must be 32 lowercase hexadecimal characters")
    return selected


def _run_name(run_id: str) -> str:
    return f"accept-{run_id[:12]}"


def _canary_path(state_root: Path, run_id: str) -> Path:
    if RUN_ID_RE.fullmatch(run_id) is None:
        raise AcceptanceError("campaign canary identity is invalid")
    return state_root / f".vm-host-acceptance-canary-{run_id}.bin"


def _create_canary_fixture(state_root: Path, run_id: str) -> None:
    path = _canary_path(state_root, run_id)
    token = secrets.token_urlsafe(48).encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as error:
        raise AcceptanceError("private synthetic canary fixture could not be created") from error
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)


def _remove_canary_fixture(state_root: Path, run_id: str, expected: bytes | None) -> bool:
    path = _canary_path(state_root, run_id)
    parent_fd = -1
    fd = -1
    try:
        parent_fd = os.open(state_root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        parent_info = os.fstat(parent_fd)
        if (parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) != 0o700):
            return False
        if expected is None:
            try:
                os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                return False
            except FileNotFoundError:
                return True
        fd = os.open(path.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=parent_fd)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size != len(expected)):
            return False
        content = bytearray()
        while len(content) <= len(expected):
            chunk = os.read(fd, len(expected) + 1 - len(content))
            if not chunk:
                break
            content.extend(chunk)
        if bytes(content) != expected or _canary_file_signature(info) != _canary_file_signature(os.fstat(fd)):
            return False
        named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if _canary_file_signature(named) != _canary_file_signature(info):
            return False
        os.unlink(path.name, dir_fd=parent_fd)
        return True
    except OSError:
        return False
    finally:
        if fd >= 0:
            os.close(fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _canary_file_signature(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _required_gates(suites: list[str]) -> list[str]:
    selected = {gate for suite in suites for gate in SUITE_GATES[suite]
                if gate not in DEFERRED_GATE_REASONS}
    if selected.intersection({"E2E-SELF", "E2E-DEPTH", "SEC-NET", "E2E-AGENTS"}):
        selected.add("HUMAN-TUI")
    return sorted(selected)


def _acceptance_scope(suites: list[str]) -> dict[str, Any]:
    selected = {gate for suite in suites for gate in SUITE_GATES[suite]}
    deferred_gates = [
        {"id": gate, "status": "DEFERRED", "reason_code": reason}
        for gate, reason in DEFERRED_GATE_REASONS.items() if gate in selected
    ]
    deferred_checks = {
        gate: [{"id": item, "status": "DEFERRED", "reason_code": "owner_deferred_live_openai"}
               for item in checks]
        for gate, checks in DEFERRED_CHECKS.items() if gate in selected
    }
    return {
        "id": LOCAL_FIRST_SCOPE_ID,
        "required_gates": _required_gates(suites),
        "deferred_gates": deferred_gates,
        "deferred_stages": [
            {"id": stage, "status": "DEFERRED", "reason_code": "owner_deferred_live_openai"}
            for stage in ("early-auth", "final-auth")
        ],
        "deferred_checks": deferred_checks,
        "automated_auth_regressions": {
            "status": "REQUIRED_OUTSIDE_CAMPAIGN",
            "suite": "tests/test_vm_auth.py and related local auth regression tests",
        },
    }


def _campaign_scope(suites: list[str], memory_gib: int, provider_config_sha256: str | None,
                    apply_selected_updates: bool, approved_total_budget_seconds: int) -> dict[str, Any]:
    return {
        "scope_id": LOCAL_FIRST_SCOPE_ID,
        "suites": list(suites),
        "memory_gib": memory_gib,
        "provider_config_sha256": provider_config_sha256,
        "apply_selected_updates": apply_selected_updates,
        "approved_total_budget_seconds": approved_total_budget_seconds,
    }


def _profile_name(run_id: str, role: str = "outer") -> str:
    if role not in {"outer", "agent", "workload", "provider-ollama", "provider-llamacpp", "provider-openai"}:
        raise AcceptanceError("unknown acceptance profile role")
    return f"{_run_name(run_id)}-{role}"


def _namespace_path(state_root: Path, run_name: str) -> Path:
    digest = hashlib.sha256(os.fsencode(state_root)).hexdigest()[:24]
    return state_root.parent / f".little-mester-recursion-{digest}" / run_name


def _source_identity(source: Path) -> dict[str, Any]:
    snapshot = _load_module("vm_host_acceptance_snapshot", SNAPSHOT_PATH)
    manifest, _fingerprints = snapshot.inventory(source, 2 * 1024**3)
    return {
        "source_head": manifest["source_head"],
        "tree_sha256": manifest["tree_sha256"],
        "entry_count": manifest["entry_count"],
        "total_bytes": manifest["total_bytes"],
    }


def _provider_endpoint(value: object, provider: str) -> tuple[str, int]:
    if provider not in PROVIDER_DISCOVERY_ROUTES or not isinstance(value, str):
        raise AcceptanceError("provider endpoint is unsupported")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise AcceptanceError("provider endpoint is invalid") from error
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or port is None
            or not 1024 <= port <= 65535 or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or value != f"http://127.0.0.1:{port}"):
        raise AcceptanceError("provider endpoints must be canonical loopback HTTP origins")
    return parsed.hostname, port


def _decode_json_no_duplicates(raw: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise AcceptanceError("provider configuration is invalid JSON") from error


def load_provider_config(path_value: str | os.PathLike[str] | None) -> tuple[dict[str, Any], str]:
    """Read a private, credential-free explicit set of provider/model selections."""
    if path_value is None:
        return {"schema_version": 1, "providers": []}, hashlib.sha256(b'{"schema_version":1,"providers":[]}').hexdigest()
    path = Path(path_value)
    try:
        lexical_info = path.lstat()
    except OSError as error:
        raise AcceptanceError("provider configuration is unavailable") from error
    if (not stat.S_ISREG(lexical_info.st_mode) or path.is_symlink()
            or path.resolve(strict=True) != path):
        raise AcceptanceError("provider configuration must be a canonical private 0600 regular file")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise AcceptanceError("provider configuration changed during validation") from error
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_PROVIDER_CONFIG_BYTES):
            raise AcceptanceError("provider configuration must be a canonical private 0600 regular file")
        chunks = bytearray()
        while len(chunks) <= MAX_PROVIDER_CONFIG_BYTES:
            chunk = os.read(fd, min(4096, MAX_PROVIDER_CONFIG_BYTES + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        raw = bytes(chunks)
        if len(raw) != info.st_size or len(raw) > MAX_PROVIDER_CONFIG_BYTES:
            raise AcceptanceError("provider configuration changed or exceeds its size limit")
    finally:
        os.close(fd)
    config = _decode_json_no_duplicates(raw)
    if (not isinstance(config, dict) or set(config) != {"schema_version", "providers"}
            or type(config.get("schema_version")) is not int or config["schema_version"] != 1
            or not isinstance(config.get("providers"), list) or len(config["providers"]) > 3):
        raise AcceptanceError("provider configuration schema is invalid")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for item in config["providers"]:
        if not isinstance(item, dict) or set(item) - {"provider", "endpoint", "model_id"}:
            raise AcceptanceError("provider configuration entry is invalid")
        provider = item.get("provider")
        model_id = item.get("model_id")
        if (not isinstance(provider, str) or provider not in {"ollama", "llamacpp", "openai"}
                or provider in seen
                or not isinstance(model_id, str) or not PROVIDER_ID_RE.fullmatch(model_id)):
            raise AcceptanceError("provider/model selection is invalid or duplicated")
        if provider == "openai":
            if "endpoint" in item or not model_id.startswith("openai/"):
                raise AcceptanceError("OpenAI selection must name an OpenAI model and use the browser bridge")
            normalized.append({"provider": provider, "model_id": model_id})
        else:
            host, port = _provider_endpoint(item.get("endpoint"), provider)
            normalized.append({"provider": provider, "endpoint": f"http://{host}:{port}",
                               "port": port, "model_id": model_id})
        seen.add(provider)
    return {"schema_version": 1, "providers": normalized}, hashlib.sha256(raw).hexdigest()


def _probe_provider_models(provider: str, host: str, port: int, *, timeout: float = 2.0) -> list[str] | None:
    """Read model identifiers from one already-selected loopback endpoint.

    ``None`` means the selected endpoint did not produce a bounded, valid model
    response. An empty list is a valid response with no models.
    """
    if (provider not in PROVIDER_DISCOVERY_ROUTES or host != "127.0.0.1"
            or type(port) is not int or not 1 <= port <= 65535
            or not isinstance(timeout, (int, float)) or not 0 < timeout <= 2.0):
        raise AcceptanceError("provider discovery target is outside the selected loopback policy")
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", PROVIDER_DISCOVERY_ROUTES[provider],
                           headers={"Accept": "application/json", "Connection": "close"})
        response = connection.getresponse()
        if response.status != 200:
            return None
        raw = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
        if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
            return None
        value = _decode_json_no_duplicates(raw)
    except (OSError, http.client.HTTPException, TimeoutError, AcceptanceError):
        return None
    finally:
        connection.close()
    if provider == "ollama":
        items = value.get("models") if isinstance(value, dict) else None
        if not isinstance(items, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("name"), str)
                for item in items):
            return None
        names = [item["name"] for item in items]
    else:
        items = value.get("data") if isinstance(value, dict) else None
        if not isinstance(items, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("id"), str)
                for item in items):
            return None
        names = [item["id"] for item in items]
    return sorted({name for name in names if isinstance(name, str) and PROVIDER_ID_RE.fullmatch(name)})


def summarize_provider_selections(config: dict[str, Any]) -> dict[str, Any]:
    """Summarize configured selections without contacting unbound endpoints."""
    selections: list[dict[str, Any]] = []
    for item in config["providers"]:
        if item["provider"] == "openai":
            selections.append({"provider": "openai", "model_id": item["model_id"],
                               "status": "OWNER_AUTH_REQUIRED"})
            continue
        selections.append({"provider": item["provider"], "model_id": item["model_id"],
                           "status": "PROFILE_BOUND_DISCOVERY_REQUIRED",
                           "reason_code": "no_endpoint_contact_before_exact_profile"})
    return {"providers": selections, "result": "NOT_PROBED"}


def _read_current_recursion_identity(path: Path = Path("/etc/lab-vm-recursion"),
                                    environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Read the same bounded persisted depth identity enforced by recursion.sh."""
    environment = os.environ if environ is None else environ
    depth_env = environment.get("LAB_VM_RECURSION_DEPTH")
    maximum_env = environment.get("LAB_VM_MAX_DEPTH")
    has_depth = "LAB_VM_RECURSION_DEPTH" in environment
    has_maximum = "LAB_VM_MAX_DEPTH" in environment
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        if has_depth or has_maximum:
            return {"state": "INVALID", "depth": None, "max_depth": None}
        return {"state": "ABSENT", "depth": None, "max_depth": None}
    except OSError:
        return {"state": "INVALID", "depth": None, "max_depth": None}
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o022
                or info.st_size > 128):
            return {"state": "INVALID", "depth": None, "max_depth": None}
        raw = os.read(fd, 129)
        if len(raw) != info.st_size or len(raw) > 128:
            return {"state": "INVALID", "depth": None, "max_depth": None}
    except OSError:
        return {"state": "INVALID", "depth": None, "max_depth": None}
    finally:
        os.close(fd)
    match = re.fullmatch(rb"(0|[1-9][0-9]?) (0|[1-9][0-9]?)\n?", raw)
    if match is None:
        return {"state": "INVALID", "depth": None, "max_depth": None}
    depth, maximum = (int(value) for value in match.groups())
    if (depth > 32 or maximum > 32 or depth > maximum or has_depth != has_maximum
            or (has_depth and (depth_env != str(depth) or maximum_env != str(maximum)))):
        return {"state": "INVALID", "depth": None, "max_depth": None}
    return {"state": "PERSISTED", "depth": depth, "max_depth": maximum}


def _loopback_listener_state(port: int, proc_net: Path = Path("/proc/net")) -> str:
    """Classify only the exact selected port; never enumerate or contact networks."""
    if type(port) is not int or not 1 <= port <= 65535:
        return "UNKNOWN"
    matches: list[bool] = []
    for table_name, loopback_hex in (
        ("tcp", "0100007F"),
        ("tcp6", "00000000000000000000000001000000"),
    ):
        try:
            rows = (proc_net / table_name).read_text(encoding="ascii").splitlines()[1:]
        except (OSError, UnicodeError):
            return "UNKNOWN"
        for row in rows:
            fields = row.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            try:
                local_address, local_port = fields[1].split(":", 1)
                if int(local_port, 16) == port:
                    matches.append(local_address.upper() == loopback_hex)
            except (ValueError, TypeError):
                return "UNKNOWN"
    if not matches:
        return "ABSENT"
    if len(matches) != 1 or not matches[0]:
        return "UNKNOWN"
    return "PRESENT"


def _profile_model_forward_identity(profile: dict[str, Any], state_root: Path,
                                    name: str) -> dict[str, Any] | None:
    """Validate the exact profile's restricted QEMU argv without exposing it."""
    try:
        identity = profile["runtime_identity"]
        argv = identity["argv"]
        if (not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv)
                or Path(identity["executable"]).name != "qemu-system-x86_64"
                or Path(argv[0]).name != "qemu-system-x86_64" or "-enable-kvm" not in argv):
            return None
        netdevs = [argv[index + 1] for index, item in enumerate(argv[:-1]) if item == "-netdev"]
        drives = [argv[index + 1] for index, item in enumerate(argv[:-1]) if item == "-drive"]
        chardevs = [item for item in argv if item.startswith("socket,id=charfs,path=")]
        expected_disk = state_root / "vm/profiles" / name / "disk.qcow2"
        expected_drive = f"file={expected_disk},format=qcow2,if=virtio"
        if (len(netdevs) != 1 or len(chardevs) != 1 or len(drives) != 2 or drives.count(expected_drive) != 1
                or chardevs[0] != f"socket,id=charfs,path={identity['socket_path']}"):
            return None
        segments = netdevs[0].split(",")
        if segments.count("restrict=on") != 1:
            return None
        if any(segment not in {"user", "id=net0", "restrict=on"}
               and not segment.startswith(("hostfwd", "guestfwd")) for segment in segments):
            return None
        hostfwds = [segment for segment in segments if segment.startswith("hostfwd")]
        expected_ssh = f"hostfwd=tcp:127.0.0.1:{identity['ssh_port']}-:22"
        if hostfwds != [expected_ssh]:
            return None

        forwards: dict[str, tuple[int, str]] = {}
        forward_pattern = re.compile(
            r"guestfwd=tcp:10\.0\.2\.100:(3128|11434|8080|19532)-cmd:([A-Za-z0-9_./+-]+) "
            r"- TCP:127\.0\.0\.1:([1-9][0-9]{0,4})"
        )
        guestfwds = [segment for segment in segments if segment.startswith("guestfwd")]
        for segment in guestfwds:
            match = forward_pattern.fullmatch(segment)
            if match is None:
                return None
            guest_port, command, host_port_text = match.groups()
            host_port = int(host_port_text)
            if host_port > 65535 or Path(command).name != "socat" or guest_port in forwards:
                return None
            forwards[guest_port] = (host_port, command)
        if "3128" not in forwards or len({port for port, _command in forwards.values()}) != len(forwards):
            return None
        if len({command for _port, command in forwards.values()}) != 1:
            return None
        model_forwards = {key: value for key, value in forwards.items() if key in {"11434", "8080"}}
        if len(model_forwards) > 1:
            return None
        if model_forwards:
            guest_port, (host_port, _command) = next(iter(model_forwards.items()))
            return {"provider": "ollama" if guest_port == "11434" else "llamacpp",
                    "guest_port": int(guest_port), "destination_port": host_port,
                    "transport": "socat-tcp"}
        return {"provider": None, "guest_port": None, "destination_port": None,
                "transport": None}
    except (KeyError, OSError, TypeError, ValueError):
        return None


def _read_discovery_guest_marker(state_root: Path, profile: str) -> dict[str, Any]:
    # The remote reader accepts only the fixed root-owned readiness file and caps
    # output before it crosses SSH. No guest command or endpoint is caller supplied.
    code = (
        "import os,stat; p='/var/lib/lab-vm/guest-ready.json'; "
        "fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK); "
        "s=os.fstat(fd); "
        "ok=stat.S_ISREG(s.st_mode) and s.st_uid==0 and s.st_nlink==1 "
        "and stat.S_IMODE(s.st_mode)==0o644 and s.st_size<=16384; "
        "d=os.read(fd,16385); os.close(fd); "
        "(ok and len(d)==s.st_size) or (_ for _ in ()).throw(SystemExit(2)); "
        "os.write(1,d)"
    )
    command = "sudo python3 -c " + shlex.quote(code)
    completed = _guest_exec(state_root, profile, command, timeout=15, campaign_profile=False)
    if completed.returncode != 0 or len(completed.stdout) > MAX_GUEST_MARKER_BYTES:
        raise AcceptanceError("exact profile readiness identity is unavailable")
    value = _decode_json_no_duplicates(completed.stdout)
    if not isinstance(value, dict):
        raise AcceptanceError("exact profile readiness identity is malformed")
    return value


def _validate_discovery_guest_marker(marker: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "schema_version", "status", "agent", "workload_runtime", "payload_sha256",
        "opencode_version", "opencode_version_selector", "aider_version", "model_provider",
        "model_port", "model_id", "workspace_mount", "mcp_build", "recursion_tools", "depth", "max_depth",
    }
    if marker.keys() != expected:
        raise AcceptanceError("exact profile readiness identity is malformed")
    provider = marker["model_provider"]
    port = marker["model_port"]
    model_id = marker["model_id"]
    depth = marker["depth"]
    maximum = marker["max_depth"]
    version = marker["opencode_version"]
    version_selector = marker["opencode_version_selector"]
    aider_version = marker["aider_version"]
    if (type(marker["schema_version"]) is not int or marker["schema_version"] != 1
            or marker["status"] != "ready" or marker["agent"] not in {"opencode", "aider"}
            or marker["workload_runtime"] not in {"none", "docker"}
            or marker["payload_sha256"] != profile.get("guest_payload_sha256")
            or marker["workspace_mount"] != "virtiofs" or marker["mcp_build"] is not True
            or type(marker["recursion_tools"]) is not bool
            or type(depth) is not int or type(maximum) is not int
            or not 0 <= depth <= maximum <= 32
            or depth != profile.get("depth") or maximum != profile.get("max_depth")
            or not isinstance(version_selector, str)
            or (version_selector != "latest" and re.fullmatch(
                r"v?2\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", version_selector
            ) is None)
            or not isinstance(aider_version, str)
            or not isinstance(model_id, str) or len(model_id) > 512
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in model_id)
            or provider not in {"none", "ollama", "llamacpp", "remote"}):
        raise AcceptanceError("exact profile readiness identity is malformed")
    if marker["agent"] == "opencode":
        if not isinstance(version, str) or re.fullmatch(r"2\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version) is None:
            raise AcceptanceError("exact profile readiness identity is malformed")
    elif re.fullmatch(r"[Aa]ider\s+\d+\.\d+(?:\.\d+)?", aider_version) is None:
        raise AcceptanceError("exact profile readiness identity is malformed")
    if provider in {"ollama", "llamacpp"}:
        if type(port) is not int or not 1 <= port <= 65535:
            raise AcceptanceError("exact profile readiness identity is malformed")
    elif port is not None:
        raise AcceptanceError("exact profile readiness identity is malformed")
    return {"provider": provider, "model_id": model_id, "model_port": port,
            "depth": depth, "max_depth": maximum}


def _empty_topology_report(profile: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "execution_context": {"identity": "UNKNOWN", "level": "UNKNOWN", "depth": None},
        "profile": {"name": profile, "level": "UNKNOWN", "depth": None},
        "selection": {"provider": None, "model_id": None},
        "forward": {"status": "UNKNOWN", "destination_class": "UNKNOWN", "destination_port": None},
        "relay": {"status": "UNKNOWN", "kind": "immediate-parent-relay"},
        "capability_boundary": {"status": "UNKNOWN", "kind": "operation-constrained-gateway"},
        "status": "IDENTITY_UNKNOWN",
        "reason_code": "profile_identity_unverified",
        "note": "Read-only selected-profile inventory; availability is not a security or acceptance result.",
    }


def discover_profile_topology(state_root: Path, profile_name: str, *,
                              recursion_marker: Path = Path("/etc/lab-vm-recursion"),
                              environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Inventory exactly one ready profile and probe only its verified model forward."""
    report = _empty_topology_report(profile_name if isinstance(profile_name, str) else "invalid")
    context = _read_current_recursion_identity(recursion_marker, environ)
    if context["state"] == "INVALID":
        report["status"] = "WRONG_CONTEXT"
        report["reason_code"] = "recursion_identity_invalid"
        return report
    if context["state"] == "PERSISTED":
        report["execution_context"] = {"identity": "PERSISTED", "level": f"G{context['depth']}",
                                        "depth": context["depth"]}
    else:
        report["execution_context"] = {"identity": "ABSENT", "level": "UNKNOWN", "depth": None}

    try:
        root = _canonical_directory(state_root)
        _private_directory(root)
        if not isinstance(profile_name, str) or PROFILE_NAME_RE.fullmatch(profile_name) is None:
            raise AcceptanceError("profile name is outside the permitted exact-profile syntax")
        profile = _profile_record(root, profile_name, campaign_profile=False)
        if profile.get("status") != "ready" or not isinstance(profile.get("runtime_identity"), dict):
            raise AcceptanceError("exact profile is not ready")
        if not _validate_profile_runtime(root, profile_name):
            raise AcceptanceError("exact profile runtime identity is unavailable")
        if not _listener_owned_loopback(profile):
            raise AcceptanceError("exact profile process or SSH listener identity is unavailable")
        marker = _validate_discovery_guest_marker(_read_discovery_guest_marker(root, profile_name), profile)
        forward = _profile_model_forward_identity(profile, root, profile_name)
        if forward is None:
            raise AcceptanceError("exact profile QEMU forward identity is malformed")
    except (AcceptanceError, OSError, RuntimeError, TimeoutError, ValueError, KeyError, TypeError):
        report["reason_code"] = "profile_or_forward_identity_unverified"
        return report

    depth = profile["depth"]
    maximum = profile["max_depth"]
    report["profile"] = {"name": profile_name, "level": f"G{depth}", "depth": depth}
    if context["state"] == "PERSISTED":
        if depth != context["depth"] + 1 or maximum > context["max_depth"]:
            report["status"] = "WRONG_CONTEXT"
            report["reason_code"] = "profile_not_immediate_child"
            return report
    elif depth != 0:
        report["status"] = "WRONG_CONTEXT"
        report["reason_code"] = "nested_parent_identity_absent"
        return report

    provider = marker["provider"]
    model_id = marker["model_id"]
    report["selection"] = {"provider": provider, "model_id": model_id or None}
    destination_class = "outer_launcher_loopback" if depth == 0 else "immediate_parent_loopback"
    if provider in {"none", "remote"}:
        if forward["provider"] is not None:
            report["reason_code"] = "profile_selection_forward_mismatch"
            return report
        report["relay"] = {"status": "NOT_APPLICABLE", "kind": "immediate-parent-relay"}
        report["capability_boundary"] = {
            "status": "NOT_APPLICABLE", "kind": "operation-constrained-gateway",
        }
        report["forward"] = {"status": "NO_MODEL_FORWARD", "destination_class": "NONE",
                              "destination_port": None}
        report["status"] = "NO_CAPABILITY"
        report["reason_code"] = "no_locally_discoverable_selected_model"
        return report

    expected_guest_port = 11434 if provider == "ollama" else 8080
    if (forward["provider"] != provider or forward["guest_port"] != expected_guest_port
            or forward["destination_port"] != marker["model_port"]):
        report["reason_code"] = "profile_selection_forward_mismatch"
        return report
    destination_port = forward["destination_port"]
    report["forward"] = {
        "status": "MATCHED", "provider": provider, "guest_port": expected_guest_port,
        "destination_class": destination_class, "destination_port": destination_port,
        "transport": forward["transport"],
    }
    report["relay"] = {
        "status": "MISSING" if depth > 0 else "NOT_REQUIRED",
        "kind": "immediate-parent-relay",
    }
    report["capability_boundary"] = {
        "status": "MISSING", "kind": "operation-constrained-gateway",
    }
    if not model_id:
        report["status"] = "NO_CAPABILITY"
        report["reason_code"] = "selected_model_id_absent"
        return report
    listener_state = _loopback_listener_state(destination_port)
    if listener_state == "UNKNOWN":
        report["reason_code"] = "selected_listener_identity_unknown"
        return report
    if listener_state == "ABSENT":
        report["status"] = "ENDPOINT_UNAVAILABLE"
        report["reason_code"] = "selected_loopback_listener_absent"
        return report
    models = _probe_provider_models(provider, "127.0.0.1", destination_port, timeout=2.0)
    if models is None:
        report["status"] = "ENDPOINT_UNAVAILABLE"
        report["reason_code"] = "selected_endpoint_unavailable"
    elif model_id not in models:
        report["status"] = "MODEL_NOT_LISTED"
        report["reason_code"] = "selected_model_not_listed"
    else:
        report["status"] = "AVAILABLE"
        report["reason_code"] = "selected_model_listed"
    return report


def _read_mem_budget(requested_mib: int, reserve_mib: int) -> dict[str, Any]:
    budget = _load_module("vm_host_acceptance_memory", MEMORY_PATH)
    passed, details = budget.check_budget(requested_mib, reserve_mib)
    return {"passed": passed, **details}


def _socket_listener_summary() -> dict[str, int | bool]:
    """Read listener counts without retaining addresses or arbitrary process text."""
    listeners = 0
    loopback = 0
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(name).read_text(encoding="ascii").splitlines()[1:]
        except (OSError, UnicodeError):
            return {"readable": False, "listeners": 0, "loopback": 0}
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            listeners += 1
            local = fields[1].split(":", 1)[0]
            if ((name.endswith("tcp") and local == "0100007F")
                    or (name.endswith("tcp6") and local == "00000000000000000000000001000000")):
                loopback += 1
    return {"readable": True, "listeners": listeners, "loopback": loopback}


def preflight(source: Path, state_root: Path, run_id: str, memory_gib: int = 8,
              suites: list[str] | None = None,
              provider_config_path: str | os.PathLike[str] | None = None,
              *, resume: bool = False) -> dict[str, Any]:
    """Read-only capability, ownership, resource, revision and collision check."""
    _private_directory(state_root)
    selected_suites = set(suites or ())
    source_identity = _source_identity(source)
    provider_config, provider_config_sha256 = load_provider_config(provider_config_path)
    provider_config_file = Path(provider_config_path) if provider_config_path is not None else None
    if provider_config_file is not None and (provider_config_file == source or source in provider_config_file.parents
                                             or provider_config_file == state_root
                                             or state_root in provider_config_file.parents):
        raise AcceptanceError("provider configuration must remain outside the checkout and state root")
    if source == state_root or source in state_root.parents or state_root in source.parents:
        raise AcceptanceError("source checkout and private VM state root must be disjoint")
    if not 1 <= memory_gib <= 128:
        raise AcceptanceError("requested memory must be between 1 and 128 GiB")

    checks: list[dict[str, Any]] = []

    def record(name: str, passed: bool, detail: dict[str, Any] | None = None, *, blocked: bool = False) -> None:
        checks.append({"id": name, "status": "PASS" if passed else "BLOCKED" if blocked else "FAIL",
                       "detail": detail or {}})

    supported = platform.system() == "Linux" and platform.machine() == "x86_64"
    record("supported_linux_x86_64", supported, {"system": platform.system(), "machine": platform.machine()}, blocked=True)
    kvm = Path("/dev/kvm")
    try:
        kvm_info = kvm.stat()
        kvm_ok = stat.S_ISCHR(kvm_info.st_mode) and os.access(kvm, os.R_OK | os.W_OK)
    except OSError:
        kvm_ok = False
    record("kvm_device_read_write", kvm_ok, blocked=True)
    try:
        flags = Path("/proc/cpuinfo").read_text(encoding="ascii", errors="ignore")
        cpu_ok = any(re.search(r"\b(?:vmx|svm)\b", line) for line in flags.splitlines() if line.startswith("flags"))
    except OSError:
        cpu_ok = False
    record("cpu_virtualization_exposed", cpu_ok, blocked=True)

    required_tools = (
        "qemu-system-x86_64", "qemu-img", "virtiofsd", "ssh", "ssh-keyscan",
        "ssh-keygen", "curl", "python3", "socat", "flock", "timeout", "git",
    )
    missing = [name for name in required_tools if shutil.which(name) is None]
    if not shutil.which("genisoimage") and not shutil.which("mkisofs"):
        missing.append("genisoimage-or-mkisofs")
    record("required_tools", not missing, {"missing_count": len(missing)}, blocked=bool(missing))

    try:
        memory = _read_mem_budget(memory_gib * 1024, DEFAULT_RESERVE_MIB)
        record("memory_budget", memory["passed"], {
            key: memory.get(key) for key in (
                "memavailable_mib", "cgroup_remaining_mib", "projected_available_mib",
                "requested_mib", "reserve_mib",
            )
        }, blocked=not memory["passed"])
    except (OSError, RuntimeError, ValueError):
        memory = {"passed": False}
        record("memory_budget", False, {"readable": False}, blocked=True)

    if "lifecycle" in selected_suites:
        resize_budget_ok = memory_gib >= 8
        record("lifecycle_resize_memory", resize_budget_ok,
               {"minimum_gib": 8, "requested_gib": memory_gib}, blocked=not resize_budget_ok)

    disk_parent = state_root.parent
    disk = shutil.disk_usage(disk_parent)
    copy_factor = 5 if "self" in selected_suites else 2 if "daily" in selected_suites else 1
    estimated = max(
        2 * 1024**3,
        2 * 1024**3 + source_identity["total_bytes"] * copy_factor + 128 * 1024**2,
    )
    disk_ok = disk.free >= estimated
    record("snapshot_disk_budget", disk_ok, {
        "free_bytes": disk.free, "required_bytes": estimated,
    }, blocked=not disk_ok)
    listener_summary = _socket_listener_summary()
    record("listener_inventory_readable", listener_summary["readable"], {
        "listener_count": listener_summary["listeners"],
        "loopback_count": listener_summary["loopback"],
    }, blocked=not listener_summary["readable"])

    # Configured endpoints are not contacted during preflight. Availability is
    # checked only after an exact ready profile and its selected forward are bound.
    provider_selection_check = summarize_provider_selections(provider_config)

    namespace = _namespace_path(state_root, _run_name(run_id))
    collision = namespace.exists() or namespace.is_symlink()
    existing_owned = False
    report_path = namespace.parent / "reports" / f"{_run_name(run_id)}.json"
    if resume:
        try:
            _private_directory(namespace)
            _private_directory(report_path.parent)
            manifest = _read_private_json(report_path)
            existing_owned = (manifest.get("run_id") == run_id
                              and manifest.get("run_name") == _run_name(run_id))
        except (AcceptanceError, OSError):
            existing_owned = False
        record("unique_run_namespace", existing_owned,
               {"existing_exact_run": existing_owned}, blocked=not existing_owned)
    else:
        record("unique_run_namespace", not collision, {"collision": collision}, blocked=collision)
    namespace_parent = namespace.parent
    parent_safe = True
    if namespace_parent.exists() or namespace_parent.is_symlink():
        try:
            _private_directory(namespace_parent)
        except AcceptanceError:
            parent_safe = False
    record("private_campaign_parent", parent_safe, blocked=not parent_safe)
    report_collision = report_path.exists() or report_path.is_symlink()
    record("unique_run_evidence", existing_owned if resume else not report_collision,
           {"existing_exact_run": existing_owned} if resume else {"collision": report_collision},
           blocked=resume and not existing_owned or not resume and report_collision)
    canary_path = _canary_path(state_root, run_id)
    canary_collision = canary_path.exists() or canary_path.is_symlink()
    canary_safe = False
    if resume:
        try:
            canary_info = canary_path.lstat()
            canary_safe = (stat.S_ISREG(canary_info.st_mode) and canary_info.st_uid == os.getuid()
                           and canary_info.st_nlink == 1 and stat.S_IMODE(canary_info.st_mode) == 0o600
                           and not canary_path.is_symlink())
        except OSError:
            canary_safe = False
    record("unique_run_canary", canary_safe if resume else not canary_collision,
           {"collision": canary_collision}, blocked=resume and not canary_safe or not resume and canary_collision)
    report_root_safe = True
    if report_path.parent.exists() or report_path.parent.is_symlink():
        try:
            _private_directory(report_path.parent)
        except AcceptanceError:
            report_root_safe = False
    record("private_evidence_directory", report_root_safe, blocked=not report_root_safe)
    return {
        "schema_version": 1,
        "run_id": run_id,
        "created_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "deadlines_seconds": {
            "host_preflight": 30, "guest_probe": 120, "guest_boot": 1900,
            "graceful_suspend": 90, "update_scan": 300, "update_apply": 900,
        },
        "execution_context": {"system": platform.system(), "machine": platform.machine()},
        "source": source_identity,
        "provider_config": {
            "sha256": provider_config_sha256,
            "providers": [{key: item[key] for key in ("provider", "endpoint", "port", "model_id") if key in item}
                          for item in provider_config["providers"]],
            "selections": provider_selection_check["providers"],
        },
        "memory": {key: memory.get(key) for key in (
            "memavailable_mib", "cgroup_remaining_mib", "projected_available_mib",
            "requested_mib", "reserve_mib", "passed",
        )},
        "checks": checks,
        "result": "PASS" if all(item["status"] == "PASS" for item in checks) else "BLOCKED",
    }


LIFECYCLE_SCRIPT = r'''set -euo pipefail
repo=$1 state_root=$2 profile=$3 workspace=$4 operation=$5 memory=$6 confirm=$7 depth=$8 maximum=$9
model_provider=${10} model_port=${11} model_id=${12}
source "$repo/lib/vm/config.sh"
vm_cli_defaults
VM_STATE_ROOT="$state_root"
VM_PATHS_HELPER="$repo/lib/vm/paths.py"
source "$repo/lib/vm/disk.sh"
source "$repo/lib/vm/recursion.sh"
source "$repo/lib/vm/lifecycle.sh"
if [[ "$model_provider" != none ]]; then
  VM_SEEN[model-provider]=1 VM_OPTIONS[model-provider]="$model_provider"
  if [[ -n "$model_port" ]]; then
    VM_SEEN[model-port]=1 VM_OPTIONS[model-port]="$model_port"
  fi
  if [[ -n "$model_id" ]]; then
    VM_SEEN[model-id]=1 VM_OPTIONS[model-id]="$model_id"
  fi
fi
vm_config_overrides
vm_config_load
case "$operation" in
  up)
    VM_COMMAND=up VM_MAX_DEPTH="$maximum" VM_SEEN[max-depth]=1
    if (( depth > 0 )); then
      LAB_VM_RECURSION_DEPTH=$((depth - 1)) LAB_VM_MAX_DEPTH="$maximum"
      VM_RECURSE=1 VM_UNSAFE_RECURSE=0
    else
      unset LAB_VM_RECURSION_DEPTH LAB_VM_MAX_DEPTH
      VM_RECURSE=0 VM_UNSAFE_RECURSE=0
    fi
    vm_recursion_configure up
    [[ "$VM_GUEST_RECURSION_DEPTH" == "$depth" ]]
    if (( VM_NESTED_LAUNCH )); then vm_recursion_require_nested_kvm /dev/kvm /proc/cpuinfo; fi
    VM_LIFECYCLE_ROOT="$workspace"
    vm_lifecycle_up "$state_root" "$profile" "$workspace" "$workspace" \
      "$VM_GUEST_RECURSION_DEPTH" "$VM_EFFECTIVE_MAX_DEPTH" "$memory" \
      "$VM_RECURSE" "$VM_UNSAFE_RECURSE" 0 60 1800
    ;;
  status)
    vm_lifecycle_status "$state_root" "$profile"
    ;;
  suspend)
    runtime="$state_root/vm/profiles/$profile/runtime/lab-vm"
    vm_lifecycle_suspend "$state_root" "$profile" 0 60 "$runtime"
    ;;
  resume)
    vm_lifecycle_resume "$state_root" "$profile" "$workspace" "$workspace" 1 0 0 60 none "" "" 1800
    ;;
  resize)
    vm_lifecycle_resize "$state_root" "$profile" "$memory" 0 60
    ;;
  update-scan)
    vm_lifecycle_update "$state_root" "$profile" 0 0 0 config
    ;;
  update-apply)
    vm_lifecycle_update "$state_root" "$profile" 1 0 0 config tools
    ;;
  delete)
    [[ "$confirm" == "$profile" ]] || exit 2
    vm_lifecycle_delete "$state_root" "$profile" 0 1 0 60
    ;;
  *) exit 2 ;;
esac
'''


def _lifecycle_call(state_root: Path, profile: str, workspace: Path, operation: str,
                    memory_gib: int = 8, *, depth: int = 0, maximum: int = 2,
                    confirm: str = "", timeout: int = 1900,
                    provider_selection: dict[str, Any] | None = None) -> subprocess.CompletedProcess:
    if PROFILE_RE.fullmatch(profile) is None:
        raise AcceptanceError("profile is outside the generated campaign namespace")
    if operation == "delete" and confirm != profile:
        raise AcceptanceError("exact profile confirmation is required for cleanup")
    timeout = _bounded_timeout(timeout)
    provider, port, model_id = "none", "", ""
    if provider_selection is not None:
        provider = provider_selection.get("provider", "")
        model_id = provider_selection.get("model_id", "")
        if provider == "openai":
            provider = "remote"
        elif provider in {"ollama", "llamacpp"}:
            port = str(provider_selection.get("port", ""))
        if provider not in {"ollama", "llamacpp", "remote"}:
            raise AcceptanceError("provider profile selection is invalid")
    args = ["bash", "-c", LIFECYCLE_SCRIPT, "vm-acceptance", str(ROOT), str(state_root),
            profile, str(workspace), operation, str(memory_gib), confirm, str(depth), str(maximum),
            provider, port, model_id]
    try:
        return subprocess.run(
            args, cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True,
            check=False, timeout=timeout,
            env={**os.environ, "MESTER_STATE_DIR": str(state_root), "VM_INSTALL_PREREQS": "0"},
        )
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("lifecycle operation exceeded its bounded deadline") from error


def _profile_record(state_root: Path, profile: str, *, campaign_profile: bool = True) -> dict[str, Any]:
    allowed_name = PROFILE_RE if campaign_profile else PROFILE_NAME_RE
    if not isinstance(profile, str) or allowed_name.fullmatch(profile) is None:
        raise AcceptanceError("profile name is outside the permitted exact-profile syntax")
    try:
        result = subprocess.run(
            [sys.executable, str(PATHS_PATH), "profile-read", "--state-root", str(state_root), "--profile", profile],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            check=False, timeout=_bounded_timeout(10),
        )
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("profile identity read exceeded its bounded deadline") from error
    if result.returncode != 0:
        raise AcceptanceError("generated profile metadata is unavailable")
    try:
        value = _decode_json_no_duplicates(result.stdout)
    except (AcceptanceError, UnicodeError, json.JSONDecodeError) as error:
        raise AcceptanceError("generated profile metadata is invalid") from error
    if not isinstance(value, dict) or value.get("name") != profile:
        raise AcceptanceError("profile name did not match the exact campaign target")
    return value


def _validate_profile_runtime(state_root: Path, profile: str) -> bool:
    """Verify the exact profile's frozen runtime before using its identity."""
    try:
        result = subprocess.run(
            [sys.executable, str(PATHS_PATH), "runtime-validate", "--state-root", str(state_root),
             "--profile", profile],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=_bounded_timeout(15),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _profile_run_directory(state_root: Path, profile: str, data: dict[str, Any]) -> Path:
    """Resolve the one registry that records this exact live QEMU identity."""
    runs = state_root / "vm/profiles" / profile / "runs"
    _private_directory(runs)
    identity = data.get("runtime_identity")
    if not isinstance(identity, dict):
        raise AcceptanceError("exact live process identity is unavailable")
    try:
        expected = {key: identity[key] for key in ("pid", "start_ticks", "executable", "argv")}
        process = _load_module("vm_host_acceptance_registry_process", ROOT / "lib/vm/process.py")
        matches: list[Path] = []
        for run_dir in runs.iterdir():
            _private_directory(run_dir)
            registry = run_dir / "processes.json"
            record = process.registry_read(registry, run_dir.name)
            for role, item in record["processes"].items():
                if role.startswith("qemu_") and item.get("identity") == expected:
                    matches.append(run_dir)
        if len(matches) != 1:
            raise AcceptanceError("exact profile process registry is unavailable or ambiguous")
        return matches[0]
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
        raise AcceptanceError("exact profile process registry is unsafe") from error


def _hash_file(path: Path) -> str | None:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _read_private_json(path: Path, maximum: int = MAX_REPORT_BYTES) -> dict[str, Any]:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise AcceptanceError("private campaign evidence is unavailable") from error
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > maximum):
            raise AcceptanceError("private campaign evidence is unsafe")
        chunks = bytearray()
        while len(chunks) <= maximum:
            block = os.read(fd, min(65536, maximum + 1 - len(chunks)))
            if not block:
                break
            chunks.extend(block)
        if len(chunks) > maximum or len(chunks) != info.st_size:
            raise AcceptanceError("private campaign evidence is oversized or changed")
        value = json.loads(chunks.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AcceptanceError("private campaign evidence is invalid") from error
    finally:
        os.close(fd)
    if not isinstance(value, dict):
        raise AcceptanceError("private campaign evidence has an invalid schema")
    return value


def _listener_owned_loopback(profile: dict[str, Any]) -> bool:
    identity = profile.get("runtime_identity")
    if not isinstance(identity, dict):
        return False
    argv = identity.get("argv")
    port = identity.get("ssh_port")
    pid = identity.get("pid")
    if not isinstance(argv, list) or type(port) is not int or type(pid) is not int:
        return False
    try:
        process = _load_module("vm_host_acceptance_process", ROOT / "lib/vm/process.py")
        process_identity = {key: identity[key] for key in ("pid", "start_ticks", "executable", "argv")}
        if process.identity_state(process_identity) != "running":
            return False
        owned_inodes = {
            os.readlink(descriptor)[8:-1]
            for descriptor in Path(f"/proc/{pid}/fd").iterdir()
            if os.readlink(descriptor).startswith("socket:[") and os.readlink(descriptor).endswith("]")
        }
    except (KeyError, OSError, RuntimeError, ValueError):
        return False
    if "-enable-kvm" not in argv or not any("restrict=on" in item for item in argv):
        return False
    expected = f"hostfwd=tcp:127.0.0.1:{port}-:22"
    if not any(expected in item for item in argv):
        return False
    found: set[str] = set()
    ssh_listener_found = False
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            rows = table.read_text(encoding="ascii").splitlines()[1:]
        except (OSError, UnicodeError):
            return False
        for row in rows:
            columns = row.split()
            if len(columns) < 10 or columns[3] != "0A":
                continue
            inode = columns[9]
            if inode not in owned_inodes:
                continue
            local_hex, _remote = columns[1].split(":", 1)
            actual_port = int(columns[1].rsplit(":", 1)[1], 16)
            if table.name == "tcp":
                loopback = local_hex == "0100007F"
            else:
                loopback = local_hex == "00000000000000000000000001000000"
            if not loopback:
                return False
            found.add(inode)
            if actual_port == port:
                ssh_listener_found = True
    return bool(found) and ssh_listener_found


def _guest_exec(state_root: Path, profile: str, command: str, *, timeout: int = 120,
                campaign_profile: bool = True, allow_failure: bool = False) -> subprocess.CompletedProcess:
    timeout = _bounded_timeout(timeout)
    allowed_name = PROFILE_RE if campaign_profile else PROFILE_NAME_RE
    if (not isinstance(profile, str) or allowed_name.fullmatch(profile) is None
            or "\n" in command or len(command) > 60_000
            or type(timeout) is not int or not 1 <= timeout <= 1800):
        raise AcceptanceError("guest command is outside the fixed campaign contract")
    data = _profile_record(state_root, profile, campaign_profile=campaign_profile)
    if data.get("status") != "ready" or not _listener_owned_loopback(data):
        raise AcceptanceError("profile or owned loopback listener is not verified")
    run_dir = _profile_run_directory(state_root, profile, data)
    key = state_root / "vm/profiles" / profile / "control-key"
    known_hosts = run_dir / "known_hosts"
    remote_script = r'''set -euo pipefail
repo=$1 state_root=$2 profile=$3 key=$4 known_hosts=$5 command=$6 deadline=$7
source "$repo/lib/vm/config.sh"
source "$repo/lib/vm/disk.sh"
source "$repo/lib/vm/lifecycle.sh"
VM_LIFECYCLE_ROOT="$repo"
data="$(vm_lifecycle_profile_read "$state_root" "$profile")"
info="$(vm_lifecycle_registry_for_profile "$state_root" "$profile" "$data")"
registry="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["registry"])' <<<"$info")"
run_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])' <<<"$info")"
role="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["role"])' <<<"$info")"
port="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["runtime_identity"]["ssh_port"])' <<<"$data")"
source "$repo/lib/vm/ssh.sh"
vm_ssh_listener_verified "$registry" "$run_id" "$role" "$port"
vm_ssh_noninteractive "$key" "$known_hosts" "$port" "$((deadline + 5))" mester@127.0.0.1 \
  "timeout --foreground --signal=TERM --kill-after=2s ${deadline}s bash -lc $(printf '%q' "$command")"
'''
    try:
        completed = subprocess.run(
            ["bash", "-c", remote_script, "vm-acceptance-isolation", str(ROOT), str(state_root),
             profile, str(key), str(known_hosts), command, str(timeout)],
            stdin=subprocess.DEVNULL, capture_output=True,
            check=False, timeout=timeout + 30, cwd=ROOT,
        )
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("bounded guest command timed out") from error
    if completed.returncode != 0 and not allow_failure:
        raise AcceptanceError("bounded guest command failed")
    if allow_failure:
        # Development probes consume only fixed, size-bounded JSON on stdout.
        # Never forward guest stderr, which may contain arbitrary guest output.
        return subprocess.CompletedProcess(
            completed.args, completed.returncode, completed.stdout[:64 * 1024], b"",
        )
    return completed


def _guest_clean_checks(state_root: Path, profile: str, workspace: Path,
                        host_uid: int, host_gid: int) -> dict[str, Any]:
    code = r'''import json,os,stat,subprocess
from pathlib import Path
checks={"cloud_init_clean":False,"workspace_mount":False,"workspace_uid_gid_match":False,"node_available":False,
"mcp_config_valid":False,"mcp_build_present":False,"guest_docker_available":False,
}
opencode_version=None
try:
    cloud=subprocess.run(["cloud-init","status","--wait"],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=45)
    checks["cloud_init_clean"]=(cloud.returncode==0 and len(cloud.stdout)<4096)
    for line in Path("/proc/mounts").read_text().splitlines():
        fields=line.split()
        if len(fields)>=3 and fields[1]=="/workspace": checks["workspace_mount"]=(fields[2]=="virtiofs")
    info=Path("/workspace").stat(); checks["workspace_uid_gid_match"]=(info.st_uid==int(os.environ["LM_EXPECT_UID"]) and info.st_gid==int(os.environ["LM_EXPECT_GID"]))
    node=subprocess.run(["node","--version"],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=5)
    checks["node_available"]=(node.returncode==0 and len(node.stdout)<64)
    opencode=subprocess.run(["opencode","--version"],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=5)
    if opencode.returncode==0 and len(opencode.stdout)<128:
        candidate=opencode.stdout.decode("ascii","ignore").strip()
        if __import__("re").fullmatch(r"2\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?",candidate): opencode_version=candidate
    cfg=Path("/home/mester/.config/opencode/opencode.json"); runtime=Path("/home/mester/.local/share/little-mester/guest-runtime/mcp/dist/mcp-server.js")
    data=json.loads(cfg.read_text()); server=data["mcp"]["servers"]["little-mester"]
    checks["mcp_config_valid"]=(server.get("type")=="local" and server.get("command")==["node","dist/mcp-server.js"] and server.get("cwd")=="/home/mester/.local/share/little-mester/guest-runtime/mcp")
    checks["mcp_build_present"]=(runtime.is_file() and not runtime.is_symlink() and runtime.stat().st_size>0)
    docker=subprocess.run(["docker","info","--format","{{.ServerVersion}}"],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=10)
    checks["guest_docker_available"]=(docker.returncode==0 and len(docker.stdout)<128)
except Exception:
    pass
print(json.dumps({"checks":checks,"opencode_version":opencode_version},sort_keys=True,separators=(",",":")))'''
    command = "env LM_EXPECT_UID=" + str(host_uid) + " LM_EXPECT_GID=" + str(host_gid) + " python3 -c " + shlex.quote(code)
    result = _guest_exec(state_root, profile, command, timeout=75)
    try:
        value = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AcceptanceError("guest clean-check report is invalid") from error
    checks = value.get("checks")
    required = {"cloud_init_clean", "workspace_mount", "workspace_uid_gid_match", "node_available",
                "mcp_config_valid", "mcp_build_present", "guest_docker_available"}
    version = value.get("opencode_version")
    if (value.keys() != {"checks", "opencode_version"} or not isinstance(checks, dict) or checks.keys() != required
            or any(type(item) is not bool for item in checks.values())):
        raise AcceptanceError("guest clean-check report does not match the fixed schema")
    version_ok = isinstance(version, str) and re.fullmatch(r"2\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", version)
    passed = version_ok and all(value is True for value in checks.values())
    return {"checks": checks, "opencode_version": version if version_ok else None,
            "host_docker_dependency": "NOT_REQUIRED", "result": "PASS" if passed else "FAIL"}


GUEST_WORKLOAD_CODE = r'''import json,os,re,socket,subprocess,tempfile
from pathlib import Path

run_id=os.environ["LM_WORKLOAD_ID"]
relative=os.environ["LM_WORKLOAD_RELATIVE"]
project=os.environ["LM_WORKLOAD_PROJECT"]
checks={"postgres_image_available":False,"compose_health":False,"query_ok":False,
 "bind_mount_readable":False,"volume_survives_container_restart":False,
 "no_published_service":False,"guest_loopback_only":False,"compose_down":False,
 "volume_retained_for_vm_suspend":False}
status="BLOCKED"
root=Path("/workspace")/relative
compose_path=root/"compose.yaml"
started=False

def call(argv,timeout=30,env=None):
 if argv and argv[0]=="docker":
  env=dict(os.environ if env is None else env)
  env["DOCKER_HOST"]="unix:///var/run/docker.sock"
  env.pop("DOCKER_CONTEXT",None)
 return subprocess.run(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
  stderr=subprocess.DEVNULL,timeout=timeout,check=False,env=env)

try:
 if (not re.fullmatch(r"\.lm-acceptance-work-[a-f0-9]{12}",relative)
     or not re.fullmatch(r"[a-f0-9]{32}",run_id)):
  raise ValueError
 image=call(["docker","image","inspect","postgres:16-alpine"],10)
 if image.returncode!=0:
  with tempfile.TemporaryDirectory(prefix="lm-postgres-docker-config-") as docker_config:
   pull_env=os.environ.copy()
   pull_env["DOCKER_CONFIG"]=docker_config
   call(["docker","pull","postgres:16-alpine"],180,env=pull_env)
  image=call(["docker","image","inspect","postgres:16-alpine"],10)
 checks["postgres_image_available"]=image.returncode==0
 if checks["postgres_image_available"]:
  if root.exists() or root.is_symlink(): raise FileExistsError
  root.mkdir(mode=0o700)
  bind=root/"bind"
  bind.mkdir(mode=0o700)
  (bind/"canary").write_text("BIND-"+run_id,encoding="ascii")
  compose_path.write_text("""services:
  db:
    image: postgres:16-alpine
    pull_policy: never
    environment:
      POSTGRES_DB: lm_acceptance
      POSTGRES_USER: lm_acceptance
      POSTGRES_HOST_AUTH_METHOD: trust
    volumes:
      - dbdata:/var/lib/postgresql/data
      - ./bind:/acceptance-bind:ro
    healthcheck:
      test: [CMD-SHELL, pg_isready -U lm_acceptance -d lm_acceptance]
      interval: 2s
      timeout: 2s
      retries: 30
volumes:
  dbdata:
""",encoding="ascii")

  def compose(*args,timeout=150):
   return call(["docker","compose","--project-directory",str(root),"-p",project,
    "-f",str(compose_path),*args],timeout)

  started=True
  up=compose("up","-d","--pull","never","--wait","--wait-timeout","120")
  checks["compose_health"]=up.returncode==0
  if up.returncode!=0:
   status="FAIL"
  else:
   container=compose("ps","-q","db")
   container_id=container.stdout.decode("ascii","ignore").strip()
   inspect=(call(["docker","inspect","--format","{{json .NetworkSettings.Ports}}",container_id],10)
            if container_id else None)
   try:
    ports=json.loads(inspect.stdout) if inspect and inspect.returncode==0 else None
    checks["no_published_service"]=(isinstance(ports,dict)
                                     and all(not value for value in ports.values()))
   except Exception:
    pass
   insert=compose("exec","-T","db","psql","-U","lm_acceptance","-d","lm_acceptance",
    "-v","ON_ERROR_STOP=1","-Atc",
    "CREATE TABLE IF NOT EXISTS acceptance_probe(marker text PRIMARY KEY); "
    f"INSERT INTO acceptance_probe VALUES ('{run_id}') ON CONFLICT DO NOTHING")
   query=compose("exec","-T","db","psql","-U","lm_acceptance","-d","lm_acceptance",
    "-Atc",f"SELECT marker FROM acceptance_probe WHERE marker='{run_id}'")
   checks["query_ok"]=(insert.returncode==0 and query.returncode==0
                       and query.stdout.strip()==run_id.encode())
   bind_check=compose("exec","-T","db","sh","-c",
    "test \"$(cat /acceptance-bind/canary)\" = \"$1\"","sh","BIND-"+run_id)
   checks["bind_mount_readable"]=bind_check.returncode==0
   stopped=compose("stop","db")
   restarted=(compose("up","-d","--pull","never","--wait","--wait-timeout","120")
              if stopped.returncode==0 else None)
   query_after=(compose("exec","-T","db","psql","-U","lm_acceptance","-d","lm_acceptance",
    "-Atc",f"SELECT marker FROM acceptance_probe WHERE marker='{run_id}'")
    if restarted and restarted.returncode==0 else None)
   checks["volume_survives_container_restart"]=(
    bool(query_after and query_after.returncode==0 and query_after.stdout.strip()==run_id.encode()))
   try:
    with socket.socket() as listener:
     listener.bind(("127.0.0.1",0))
     listener.listen(1)
     address=listener.getsockname()
     with socket.create_connection(address,timeout=2) as client:
      accepted,_peer=listener.accept()
      with accepted:
       checks["guest_loopback_only"]=address[0]=="127.0.0.1"
   except Exception:
    pass
   down=compose("down",timeout=60)
   empty=compose("ps","-q","db",timeout=10)
   checks["compose_down"]=(down.returncode==0 and empty.returncode==0
                           and not empty.stdout.strip())
   volume=call(["docker","volume","inspect",project+"_dbdata"],10)
   checks["volume_retained_for_vm_suspend"]=volume.returncode==0
   status="PASS" if all(checks.values()) else "FAIL"
except (OSError,subprocess.SubprocessError,ValueError,FileExistsError):
 status="FAIL" if checks["postgres_image_available"] else "BLOCKED"
finally:
 if started and root.exists():
  try:
   call(["docker","compose","--project-directory",str(root),"-p",project,
    "-f",str(root/"compose.yaml"),"down"],60)
  except Exception:
   pass
print(json.dumps({"schema_version":1,"result":status,"checks":checks},
 sort_keys=True,separators=(",",":")))'''


def _workload_relative(run_id: str) -> str:
    if RUN_ID_RE.fullmatch(run_id) is None:
        raise AcceptanceError("workload run identity is invalid")
    return f".lm-acceptance-work-{run_id[:12]}"


def _run_guest_workload_code(state_root: Path, profile: str, run_id: str,
                             code: str, timeout: int = 480) -> dict[str, Any]:
    relative = _workload_relative(run_id)
    project = f"lmacc-{run_id[:12]}"
    environment = " ".join((
        "LM_WORKLOAD_ID=" + shlex.quote(run_id),
        "LM_WORKLOAD_RELATIVE=" + shlex.quote(relative),
        "LM_WORKLOAD_PROJECT=" + shlex.quote(project),
    ))
    command = f"env {environment} python3 -c {shlex.quote(code)}"
    result = _guest_exec(state_root, profile, command, timeout=timeout)
    try:
        value = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AcceptanceError("guest workload report is invalid") from error
    if (not isinstance(value, dict) or set(value) != {"schema_version", "result", "checks"}
            or value.get("schema_version") != 1 or value.get("result") not in {"PASS", "FAIL", "BLOCKED"}
            or not isinstance(value.get("checks"), dict)
            or set(value["checks"]) != {
                "postgres_image_available", "compose_health", "query_ok", "bind_mount_readable",
                "volume_survives_container_restart", "no_published_service", "guest_loopback_only",
                "compose_down", "volume_retained_for_vm_suspend",
            }
            or any(type(check) is not bool for check in value["checks"].values())):
        raise AcceptanceError("guest workload report does not match the fixed schema")
    checks_pass = all(value["checks"].values())
    if ((value["result"] == "PASS") != checks_pass
            or (value["result"] == "BLOCKED" and value["checks"]["postgres_image_available"])):
        raise AcceptanceError("guest workload result does not match its check outcomes")
    return value


def _guest_workload_checks(state_root: Path, profile: str, run_id: str) -> dict[str, Any]:
    try:
        value = _run_guest_workload_code(state_root, profile, run_id, GUEST_WORKLOAD_CODE)
    except TimeoutError:
        return {"status": "BLOCKED", "reason_code": "guest_workload_deadline_exceeded"}
    except AcceptanceError:
        return {"status": "BLOCKED", "reason_code": "guest_workload_unavailable"}
    reason = ("guest_postgres_image_unavailable" if value["result"] == "BLOCKED"
              else "guest_workload_check_failed" if value["result"] == "FAIL" else None)
    return {"status": value["result"], "reason_code": reason, "checks": value["checks"],
            "project": f"lmacc-{run_id[:12]}", "workload_path": _workload_relative(run_id)}


GUEST_WORKLOAD_RETENTION_CODE = r'''import json,os,subprocess
from pathlib import Path
run_id=os.environ["LM_WORKLOAD_ID"]; relative=os.environ["LM_WORKLOAD_RELATIVE"]
project=os.environ["LM_WORKLOAD_PROJECT"]; root=Path("/workspace")/relative
result=False; stopped=False
args=["docker","compose","--project-directory",str(root),"-p",project,"-f",str(root/"compose.yaml")]
try:
 up=subprocess.run([*args,"up","-d","--pull","never","--wait","--wait-timeout","120"],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=150,check=False)
 if up.returncode==0:
  query=subprocess.run([*args,"exec","-T","db","psql","-U","lm_acceptance","-d","lm_acceptance","-Atc",f"SELECT marker FROM acceptance_probe WHERE marker='{run_id}'"],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=20,check=False)
  result=query.returncode==0 and query.stdout.strip()==run_id.encode()
except Exception: pass
try:
 down=subprocess.run([*args,"down"],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=60,check=False)
 ps=subprocess.run([*args,"ps","-q","db"],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=10,check=False)
 stopped=down.returncode==0 and ps.returncode==0 and not ps.stdout.strip()
except Exception: stopped=False
print(json.dumps({"database_canary_retained":result,"compose_stopped":stopped},sort_keys=True,separators=(",",":")))'''


def _guest_workload_retention(state_root: Path, profile: str, run_id: str) -> dict[str, bool]:
    # The retention probe has a deliberately smaller result schema than setup.
    environment = " ".join((
        "LM_WORKLOAD_ID=" + shlex.quote(run_id),
        "LM_WORKLOAD_RELATIVE=" + shlex.quote(_workload_relative(run_id)),
        "LM_WORKLOAD_PROJECT=" + shlex.quote(f"lmacc-{run_id[:12]}"),
    ))
    command = f"env {environment} python3 -c {shlex.quote(GUEST_WORKLOAD_RETENTION_CODE)}"
    completed = _guest_exec(state_root, profile, command, timeout=240)
    try:
        result = json.loads(completed.stdout)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AcceptanceError("workload retention report is invalid") from error
    if (not isinstance(result, dict) or set(result) != {"database_canary_retained", "compose_stopped"}
            or any(type(value) is not bool for value in result.values())):
        raise AcceptanceError("workload retention report does not match its fixed schema")
    return result


GUEST_WORKLOAD_CLEANUP_CODE = r'''import json,os,re,shutil,subprocess
from pathlib import Path
run_id=os.environ["LM_WORKLOAD_ID"]; relative=os.environ["LM_WORKLOAD_RELATIVE"]
project=os.environ["LM_WORKLOAD_PROJECT"]; root=Path("/workspace")/relative
removed=False; artifacts=False
def volume_present(name):
 result=subprocess.run(["docker","volume","ls","--filter","name=^"+name+"$","--format","{{.Name}}"],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=10,check=False)
 if result.returncode!=0: raise RuntimeError
 names=result.stdout.decode("ascii","strict").splitlines()
 if any(item!=name for item in names): raise RuntimeError
 return bool(names)
def remove_volume(name):
 if volume_present(name):
  drop=subprocess.run(["docker","volume","rm",name],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15,check=False)
  if drop.returncode!=0: return False
 return not volume_present(name)
try:
 if (not re.fullmatch(r"[a-f0-9]{32}",run_id)
     or relative!=".lm-acceptance-work-"+run_id[:12]
     or project!="lmacc-"+run_id[:12]
     or root.is_symlink() or root.parent.resolve(strict=True)!=Path("/workspace").resolve(strict=True)): raise ValueError
 info=subprocess.run(["docker","info"],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10,check=False)
 if info.returncode!=0: raise RuntimeError
 compose=root/"compose.yaml"
 if root.exists():
  if compose.is_file() and not compose.is_symlink():
   args=["docker","compose","--project-directory",str(root),"-p",project,"-f",str(compose)]
   down=subprocess.run([*args,"down","--volumes"],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=60,check=False)
   removed=down.returncode==0 and not volume_present(project+"_dbdata")
   if not removed: raise RuntimeError
  else:
   removed=remove_volume(project+"_dbdata")
   if not removed: raise RuntimeError
  shutil.rmtree(root)
  artifacts=not root.exists() and not root.is_symlink()
 else:
  removed=remove_volume(project+"_dbdata")
  artifacts=True
except Exception: pass
print(json.dumps({"volume_removed":removed,"workspace_artifacts_removed":artifacts},sort_keys=True,separators=(",",":")))'''


def _guest_workload_cleanup(state_root: Path, profile: str, run_id: str) -> dict[str, bool]:
    environment = " ".join((
        "LM_WORKLOAD_ID=" + shlex.quote(run_id),
        "LM_WORKLOAD_RELATIVE=" + shlex.quote(_workload_relative(run_id)),
        "LM_WORKLOAD_PROJECT=" + shlex.quote(f"lmacc-{run_id[:12]}"),
    ))
    command = f"env {environment} python3 -c {shlex.quote(GUEST_WORKLOAD_CLEANUP_CODE)}"
    result = _guest_exec(state_root, profile, command, timeout=120)
    try:
        value = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AcceptanceError("workload cleanup report is invalid") from error
    if (not isinstance(value, dict) or set(value) != {"volume_removed", "workspace_artifacts_removed"}
            or any(type(item) is not bool for item in value.values())):
        raise AcceptanceError("workload cleanup report does not match its fixed schema")
    return value


def _workload_suspend_resume_retention(state_root: Path, profile: str,
                                       workspace: Path, memory_gib: int,
                                       run_id: str) -> dict[str, Any]:
    try:
        before = _profile_record(state_root, profile)
        key_path = state_root / "vm/profiles" / profile / "control-key"
        key_hash = _hash_file(key_path)
        overlay = _overlay_identity(state_root, profile)
        if (before.get("status") != "ready" or key_hash is None or overlay is None
                or not _listener_owned_loopback(before)):
            return {"status": "BLOCKED", "reason_code": "outer_profile_not_verified_ready"}
        identity = (before.get("profile_id"), before.get("image_sha256"),
                    before.get("runtime_payload_sha256"), before.get("workspace"))
        suspended = _lifecycle_call(state_root, profile, workspace, "suspend", memory_gib, timeout=90)
        if suspended.returncode != 0:
            return {"status": "BLOCKED" if suspended.returncode == 2 else "FAIL",
                    "reason_code": "workload_suspend_failed"}
        state = _profile_record(state_root, profile)
        if (state.get("status") != "suspended" or state.get("profile_id") != identity[0]
                or _overlay_identity(state_root, profile) != overlay or _hash_file(key_path) != key_hash):
            return {"status": "FAIL", "reason_code": "workload_suspend_identity_changed"}
        resumed = _lifecycle_call(state_root, profile, workspace, "resume", memory_gib, timeout=1900)
        if resumed.returncode != 0:
            return {"status": "BLOCKED" if resumed.returncode == 2 else "FAIL",
                    "reason_code": "workload_resume_failed"}
        after = _profile_record(state_root, profile)
        if (after.get("status") != "ready"
                or (after.get("profile_id"), after.get("image_sha256"),
                    after.get("runtime_payload_sha256"), after.get("workspace")) != identity
                or _overlay_identity(state_root, profile) != overlay
                or _hash_file(key_path) != key_hash or not _listener_owned_loopback(after)):
            return {"status": "FAIL", "reason_code": "workload_resume_identity_changed"}
        retained = _guest_workload_retention(state_root, profile, run_id)
    except TimeoutError:
        return {"status": "BLOCKED", "reason_code": "workload_retention_deadline_exceeded"}
    except (AcceptanceError, OSError, RuntimeError, ValueError, KeyError):
        return {"status": "BLOCKED", "reason_code": "workload_retention_unavailable"}
    passed = retained["database_canary_retained"] and retained["compose_stopped"]
    return {"status": "PASS" if passed else "FAIL",
            "reason_code": None if passed else "workload_data_not_retained",
            **retained, "profile_identity_retained": True}


def _workload_gate_result(setup: dict[str, Any] | None,
                          retention: dict[str, Any] | None,
                          cleanup: dict[str, bool] | None) -> dict[str, Any]:
    if not isinstance(setup, dict):
        return {"status": "BLOCKED", "reason_code": "guest_workload_not_run"}
    setup_status = setup.get("status")
    if setup_status == "FAIL":
        return {"status": "FAIL", "reason_code": setup.get("reason_code", "guest_workload_failed"),
                "automated_status": "FAIL", "setup_checks": setup.get("checks")}
    if setup_status != "PASS":
        return {"status": "BLOCKED", "reason_code": setup.get("reason_code", "guest_workload_unavailable"),
                "automated_status": "BLOCKED", "setup_checks": setup.get("checks")}
    if not isinstance(retention, dict) or retention.get("status") != "PASS":
        status = "FAIL" if isinstance(retention, dict) and retention.get("status") == "FAIL" else "BLOCKED"
        return {"status": status,
                "reason_code": (retention.get("reason_code") if isinstance(retention, dict)
                                else "workload_vm_retention_not_verified"),
                "automated_status": status, "setup_checks": setup.get("checks"),
                "retention": retention}
    if not isinstance(cleanup, dict) or not cleanup.get("volume_removed") \
            or not cleanup.get("workspace_artifacts_removed"):
        return {"status": "BLOCKED", "reason_code": "exact_workload_cleanup_unconfirmed",
                "automated_status": "BLOCKED", "setup_checks": setup.get("checks"),
                "retention": retention, "cleanup": cleanup}
    return {"status": "BLOCKED", "reason_code": "owner_live_evidence_required",
            "automated_status": "PASS", "setup_checks": setup.get("checks"),
            "retention": retention, "cleanup": cleanup,
            "pending_owner_checklist": ["failing-test-fix-pass", "bind-and-guest-loopback-port",
                                        "no-host-published-service-or-socket"]}


def _cached_image_integrity(state_root: Path, profile: str) -> dict[str, Any]:
    data = _profile_record(state_root, profile)
    digest = data.get("image_sha256")
    image_path = data.get("image_path")
    if (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or image_path != str(state_root / "vm/images" / digest / "base.qcow2")):
        return {"status": "FAIL", "artifact_sha256": None}
    command = r'''set -euo pipefail
repo=$1 cache=$2
source "$repo/lib/vm/disk.sh"
[[ "$VM_DISK_PIN_SHA256" == "$(basename "$(dirname "$cache")")" ]]
vm_disk_manifest_valid "$cache" "$VM_DISK_PIN_URL" "$VM_DISK_PIN_SHA256" \
  "$((VM_BASE_SIZE * 1024 * 1024 * 1024))"'''
    try:
        result = subprocess.run(
            ["bash", "-c", command, "vm-acceptance-image", str(ROOT),
             str(Path(image_path).parent)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=_bounded_timeout(30),
        )
    except subprocess.TimeoutExpired:
        return {"status": "BLOCKED", "artifact_sha256": None, "reason_code": "image_check_timeout"}
    artifact_hash = None
    if result.returncode == 0:
        try:
            manifest = _read_private_json(Path(image_path).parent / "manifest.json", maximum=4096)
            candidate_hash = manifest.get("artifact_sha256")
            if isinstance(candidate_hash, str) and re.fullmatch(r"[a-f0-9]{64}", candidate_hash):
                artifact_hash = candidate_hash
        except AcceptanceError:
            pass
    return {
        "status": "PASS" if artifact_hash is not None else "FAIL",
        "artifact_sha256": artifact_hash,
    }


def _guest_isolation(state_root: Path, profile: str, workspace: Path, run_id: str) -> dict[str, Any]:
    data = _profile_record(state_root, profile)
    depth = data.get("depth")
    maximum = data.get("max_depth")
    if (type(depth) is not int or depth not in {0, 1}
            or type(maximum) is not int or maximum != 2):
        return {"status": "BLOCKED", "reason_code": "profile_recursion_identity_unverified"}
    guest_command = (
        "python3 /workspace/scripts/vm-guest-isolation-check.py --execute "
        f"--run-id {run_id} --confirm-run-id {run_id} "
        f"--expected-depth {depth} --expected-max-depth {maximum}"
    )
    try:
        completed = _guest_exec(state_root, profile, guest_command, timeout=120)
    except TimeoutError:
        return {"status": "FAIL", "reason_code": "probe_timeout"}
    except AcceptanceError:
        return {"status": "BLOCKED", "reason_code": "profile_or_listener_unverified"}
    try:
        report = json.loads(completed.stdout)
    except (UnicodeError, json.JSONDecodeError):
        return {"status": "FAIL", "reason_code": "invalid_guest_report"}
    safe_checks = report.get("checks") if isinstance(report, dict) else None
    if (not isinstance(report, dict) or report.get("schema_version") != 1 or report.get("run_id") != run_id
            or not isinstance(safe_checks, list) or len(safe_checks) != 12
            or {item.get("id") for item in safe_checks if isinstance(item, dict)} != {
                "guest_identity", "workspace_mount", "ancestor_capabilities_absent",
                "qemu_host_gateway_blocked", "metadata_endpoint_blocked",
                "direct_private_subnet_blocked", "direct_public_tcp_blocked", "direct_dns_blocked",
                "direct_udp_blocked", "proxy_private_destination_denied", "proxy_public_http_available",
                "proxy_public_https_available",
            }
            or any(not isinstance(item, dict) or set(item) != {"id", "status"}
                   or item["id"] not in {"guest_identity", "workspace_mount", "ancestor_capabilities_absent",
                                         "qemu_host_gateway_blocked",
                                         "metadata_endpoint_blocked", "direct_private_subnet_blocked",
                                         "direct_public_tcp_blocked", "direct_dns_blocked", "direct_udp_blocked",
                                         "proxy_private_destination_denied", "proxy_public_http_available",
                                         "proxy_public_https_available"}
                   or item["status"] not in {"PASS", "FAIL"} for item in safe_checks)):
        return {"status": "FAIL", "reason_code": "invalid_guest_report"}
    checks_pass = all(item["status"] == "PASS" for item in safe_checks)
    if (type(report.get("elapsed_ms")) is not int or report["elapsed_ms"] < 0
            or not isinstance(report.get("created_utc"), str)
            or report.get("result") != ("PASS" if checks_pass else "FAIL")):
        return {"status": "FAIL", "reason_code": "invalid_guest_report"}
    return {
        "status": "PASS" if checks_pass and _listener_owned_loopback(data) else "FAIL",
        "guest_checks": safe_checks,
        "host_listener_check": "PASS" if _listener_owned_loopback(data) else "FAIL",
        "source_tree_sha256": _source_identity(workspace)["tree_sha256"],
    }


def _runtime_identity_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    keys = ("pid", "start_ticks", "executable", "argv")
    return all(left.get(key) == right.get(key) for key in keys)


def _overlay_identity(state_root: Path, profile: str) -> tuple[int, int] | None:
    path = state_root / "vm/profiles" / profile / "disk.qcow2"
    try:
        info = path.lstat()
    except OSError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
        return None
    return info.st_dev, info.st_ino


def _daily_lifecycle_checks(state_root: Path, profile: str, source: Path, run_root: Path,
                            memory_gib: int) -> dict[str, Any]:
    """Exercise warm identity and A→B→B via the frozen lifecycle API only."""
    initial = _profile_record(state_root, profile)
    if initial.get("status") != "ready" or not _listener_owned_loopback(initial):
        return {"status": "BLOCKED", "reason_code": "outer_profile_not_verified_ready"}
    overlay_before = _overlay_identity(state_root, profile)
    key_path = state_root / "vm/profiles" / profile / "control-key"
    key_before = _hash_file(key_path)
    if overlay_before is None or key_before is None:
        return {"status": "BLOCKED", "reason_code": "profile_state_identity_unavailable"}

    warm_results: list[subprocess.CompletedProcess] = []
    current_identity = initial.get("runtime_identity")
    for _ in range(2):
        result = _lifecycle_call(state_root, profile, Path(initial["workspace"]), "resume",
                                 memory_gib, timeout=1900)
        warm_results.append(result)
        if result.returncode != 0:
            return {
                "status": "BLOCKED" if result.returncode == 2 else "FAIL",
                "reason_code": "warm_start_failed", "cause_exit_code": result.returncode,
            }
        current = _profile_record(state_root, profile)
        identity = current.get("runtime_identity")
        if (current.get("status") != "ready" or not isinstance(identity, dict)
                or not _runtime_identity_equal(current_identity, identity)
                or not _listener_owned_loopback(current)):
            return {"status": "FAIL", "reason_code": "warm_process_identity_changed"}
        current_identity = identity

    snapshot_root = run_root / "workspace-b-snapshot"
    if snapshot_root.exists() or snapshot_root.is_symlink():
        return {"status": "FAIL", "reason_code": "workspace_b_collision"}
    snapshot_root.mkdir(mode=0o700)
    snapshot = _load_module("vm_host_acceptance_daily_snapshot", SNAPSHOT_PATH)
    try:
        prepared = snapshot.prepare(source, snapshot_root, 2 * 1024**3)
    except (OSError, RuntimeError, ValueError):
        return {"status": "BLOCKED", "reason_code": "workspace_b_snapshot_unavailable"}
    workspace_b = Path(prepared["workspace"])
    switch = _lifecycle_call(state_root, profile, workspace_b, "resume", memory_gib, timeout=1900)
    if switch.returncode != 0:
        return {"status": "BLOCKED" if switch.returncode == 2 else "FAIL",
                "reason_code": "workspace_switch_failed", "cause_exit_code": switch.returncode}
    switched = _profile_record(state_root, profile)
    switched_identity = switched.get("runtime_identity")
    retained = (
        switched.get("workspace") == str(workspace_b)
        and switched.get("profile_id") == initial.get("profile_id")
        and switched.get("image_sha256") == initial.get("image_sha256")
        and switched.get("guest_payload_sha256") == initial.get("guest_payload_sha256")
        and _overlay_identity(state_root, profile) == overlay_before
        and _hash_file(key_path) == key_before
        and isinstance(switched_identity, dict)
        and _listener_owned_loopback(switched)
    )
    if not retained:
        return {"status": "FAIL", "reason_code": "workspace_switch_state_not_retained"}

    warm_after_switch: list[subprocess.CompletedProcess] = []
    for _ in range(2):
        result = _lifecycle_call(state_root, profile, workspace_b, "resume", memory_gib, timeout=1900)
        warm_after_switch.append(result)
        if result.returncode != 0:
            return {"status": "BLOCKED" if result.returncode == 2 else "FAIL",
                    "reason_code": "workspace_b_warm_start_failed", "cause_exit_code": result.returncode}
        current = _profile_record(state_root, profile)
        identity = current.get("runtime_identity")
        if (current.get("status") != "ready" or current.get("workspace") != str(workspace_b)
                or not isinstance(identity, dict)
                or not _runtime_identity_equal(switched_identity, identity)
                or _overlay_identity(state_root, profile) != overlay_before
                or _hash_file(key_path) != key_before or not _listener_owned_loopback(current)):
            return {"status": "FAIL", "reason_code": "workspace_b_warm_identity_changed"}
        switched_identity = identity

    return {
        "status": "PASS",
        "profile_name": profile,
        "workspace_a_tree_sha256": _source_identity(Path(initial["workspace"]))["tree_sha256"],
        "workspace_b_tree_sha256": prepared["tree_sha256"],
        "workspace_b_distinct": Path(initial["workspace"]) != workspace_b,
        "same_overlay_and_key": True,
        "warm_process_identity_stable": True,
        "warm_resume_count": len(warm_results) + len(warm_after_switch),
        "docker_rollback_route_staged": (
            (ROOT / "lab-docker").is_file() and not (ROOT / "lab-docker").is_symlink()
            and os.access(ROOT / "lab-docker", os.X_OK)
        ),
    }


def _guest_memory_mib(state_root: Path, profile: str) -> int | None:
    command = (
        "python3 -c 'import json; from pathlib import Path; "
        "value=next(int(line.split()[1]) for line in Path(\"/proc/meminfo\").read_text().splitlines() "
        "if line.startswith(\"MemTotal:\")); print(json.dumps({\"memtotal_kib\":value}))'"
    )
    try:
        result = _guest_exec(state_root, profile, command, timeout=30)
        value = json.loads(result.stdout)
    except (AcceptanceError, TimeoutError, UnicodeError, json.JSONDecodeError, StopIteration):
        return None
    kib = value.get("memtotal_kib") if isinstance(value, dict) else None
    return kib // 1024 if type(kib) is int and kib > 0 else None


def _provider_config_entry(report: dict[str, Any], provider: str) -> dict[str, Any] | None:
    config = report.get("provider_config")
    entries = config.get("providers") if isinstance(config, dict) else None
    if not isinstance(entries, list):
        return None
    return next((item for item in entries if isinstance(item, dict)
                 and item.get("provider") == provider), None)


def _suspend_profile(state_root: Path, profile: str, memory_gib: int) -> bool:
    data = _profile_record(state_root, profile)
    if data.get("status") in {"allocated", "failed", "suspended"}:
        return True
    if data.get("status") != "ready" or not _listener_owned_loopback(data):
        return False
    result = _lifecycle_call(state_root, profile, Path(data["workspace"]), "suspend",
                             int(data["memory_gib"]), timeout=90)
    if result.returncode != 0:
        return False
    after = _profile_record(state_root, profile)
    return after.get("status") == "suspended" and after.get("profile_id") == data.get("profile_id")


def _suspend_other_campaign_profiles(state_root: Path, run_id: str, report: dict[str, Any],
                                     keep_profile: str, memory_gib: int) -> bool:
    for item in report.get("owned_profiles", []):
        if not isinstance(item, dict) or item.get("run_id") != run_id:
            return False
        name = item.get("profile_name")
        if not isinstance(name, str) or PROFILE_RE.fullmatch(name) is None:
            return False
        if name == keep_profile:
            continue
        profile_path = state_root / "vm/profiles" / name
        if not profile_path.exists():
            continue
        try:
            if not _suspend_profile(state_root, name, memory_gib):
                return False
        except (AcceptanceError, OSError, RuntimeError, ValueError):
            return False
    return True


def _resume_campaign_profile(state_root: Path, run_id: str, report: dict[str, Any],
                             profile: str, memory_gib: int) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = _profile_record(state_root, profile)
        if data.get("status") == "suspended":
            if not _suspend_other_campaign_profiles(state_root, run_id, report, profile, memory_gib):
                return None, "other_campaign_profile_not_safely_suspended"
            budget = _read_mem_budget(int(data["memory_gib"]) * 1024, DEFAULT_RESERVE_MIB)
            if not budget.get("passed"):
                return None, "campaign_profile_resume_memory_budget_blocked"
            resumed = _lifecycle_call(state_root, profile, Path(data["workspace"]), "resume",
                                      int(data["memory_gib"]), timeout=1900)
            data = _profile_record(state_root, profile)
            if resumed.returncode != 0 or data.get("status") != "ready":
                return None, "campaign_profile_resume_failed"
        if data.get("status") != "ready" or not _listener_owned_loopback(data):
            return None, "campaign_profile_identity_unverified"
        return data, None
    except (AcceptanceError, OSError, RuntimeError, ValueError, KeyError):
        return None, "campaign_profile_identity_unavailable"


def _model_selection_matches(state_root: Path, profile: str, selection: dict[str, Any]) -> bool:
    provider = "openai" if selection["provider"] == "openai" else selection["provider"]
    expected = selection["model_id"] if provider == "openai" else f"{provider}/{selection['model_id']}"
    code = r'''import json
from pathlib import Path
try:
 data=json.loads(Path("/home/mester/.config/opencode/opencode.json").read_text())
 print(json.dumps({"model_matches":data.get("model")==__import__("os").environ["LM_EXPECTED_MODEL"]}))
except Exception:
 print(json.dumps({"model_matches":False}))'''
    command = "env LM_EXPECTED_MODEL=" + shlex.quote(expected) + " python3 -c " + shlex.quote(code)
    try:
        result = _guest_exec(state_root, profile, command, timeout=30)
        value = json.loads(result.stdout)
    except (AcceptanceError, TimeoutError, UnicodeError, json.JSONDecodeError):
        return False
    return value == {"model_matches": True}


def _ensure_provider_profile(state_root: Path, run_id: str, workspace: Path,
                             report: dict[str, Any], selection: dict[str, Any],
                             memory_gib: int) -> tuple[str, dict[str, Any] | None, str | None]:
    provider = selection.get("provider")
    profile = _profile_name(run_id, f"provider-{provider}")
    if not _suspend_other_campaign_profiles(state_root, run_id, report, profile, memory_gib):
        return profile, None, "another_campaign_profile_not_safely_suspended"
    budget = _read_mem_budget(memory_gib * 1024, DEFAULT_RESERVE_MIB)
    if not budget.get("passed"):
        return profile, None, "provider_profile_memory_budget_blocked"
    path = state_root / "vm/profiles" / profile
    owned = report.setdefault("owned_profiles", [])
    if not any(isinstance(item, dict) and item.get("profile_name") == profile for item in owned):
        owned.append({"profile_name": profile, "depth": 0, "run_id": run_id})
        _atomic_replace_json(_campaign_report_path(state_root, run_id), report)

    def remember(data: dict[str, Any]) -> None:
        report.setdefault("provider_profiles", {})[provider] = {
            "profile_name": profile, "profile_id": data.get("profile_id"),
            "image_sha256": data.get("image_sha256"),
            "guest_payload_sha256": data.get("guest_payload_sha256"),
            "runtime_payload_sha256": data.get("runtime_payload_sha256"),
            "model_id": selection["model_id"],
        }
        _atomic_replace_json(_campaign_report_path(state_root, run_id), report)

    if path.exists() or path.is_symlink():
        try:
            data = _profile_record(state_root, profile)
            if data.get("status") == "suspended":
                resumed = _lifecycle_call(state_root, profile, Path(data["workspace"]), "resume",
                                          memory_gib, timeout=1900, provider_selection=selection)
                if resumed.returncode != 0:
                    return profile, None, "provider_profile_resume_failed"
                data = _profile_record(state_root, profile)
            if (data.get("status") != "ready" or data.get("depth") != 0
                    or not _listener_owned_loopback(data)
                    or not _model_selection_matches(state_root, profile, selection)):
                return profile, None, "provider_profile_identity_or_model_mismatch"
            remember(data)
            return profile, data, None
        except (AcceptanceError, OSError, RuntimeError, ValueError):
            return profile, None, "provider_profile_identity_unavailable"
    result = _lifecycle_call(state_root, profile, workspace, "up", memory_gib,
                             timeout=1900, provider_selection=selection)
    if result.returncode != 0:
        return profile, None, "provider_profile_start_failed"
    try:
        data = _profile_record(state_root, profile)
    except (AcceptanceError, OSError, RuntimeError, ValueError):
        return profile, None, "provider_profile_identity_unavailable"
    if (data.get("status") != "ready" or data.get("depth") != 0
            or not _listener_owned_loopback(data)
            or not _model_selection_matches(state_root, profile, selection)):
        return profile, None, "provider_profile_identity_or_model_mismatch"
    remember(data)
    return profile, data, None


def _validate_provider_stream_report(raw: bytes, provider: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        return None
    if (not isinstance(value, dict)
            or set(value) != {"provider", "streams_attempted", "streams_passed", "schema_valid"}
            or value.get("provider") != provider
            or type(value.get("streams_attempted")) is not int or value["streams_attempted"] != 5
            or type(value.get("streams_passed")) is not int
            or not 0 <= value["streams_passed"] <= 5
            or type(value.get("schema_valid")) is not bool):
        return None
    return value


def _provider_stream_check(state_root: Path, profile: str,
                           selection: dict[str, Any]) -> dict[str, Any]:
    script = r'''import http.client,json,os,subprocess
provider=os.environ["LM_PROVIDER"]; model=os.environ["LM_MODEL"]
passed=0; schema=True
for _ in range(5):
 try:
  if provider=="ollama":
   conn=http.client.HTTPConnection("10.0.2.100",11434,timeout=60)
   payload=json.dumps({"model":model,"prompt":"Reply with OK only.","stream":True}).encode()
   conn.request("POST","/api/generate",payload,{"Content-Type":"application/json","Connection":"close"})
   response=conn.getresponse(); done=False; chunks=0; valid=response.status==200
   for line in response:
    if len(line)>65536: valid=False; break
    if not line.strip(): continue
    item=json.loads(line); valid=valid and isinstance(item,dict) and isinstance(item.get("response"),str) and type(item.get("done")) is bool
    chunks+=bool(item.get("response")); done=done or item.get("done") is True
   conn.close(); passed+=bool(valid and done and chunks)
  elif provider=="llamacpp":
   conn=http.client.HTTPConnection("10.0.2.100",8080,timeout=60)
   payload=json.dumps({"model":model,"messages":[{"role":"user","content":"Reply with OK only."}],"stream":True}).encode()
   conn.request("POST","/v1/chat/completions",payload,{"Content-Type":"application/json","Connection":"close"})
   response=conn.getresponse(); chunks=0; done=False; valid=response.status==200
   for line in response:
    if len(line)>65536: valid=False; break
    line=line.strip()
    if not line: continue
    if line==b"data: [DONE]": done=True; continue
    if not line.startswith(b"data: "): continue
    item=json.loads(line[6:]); choices=item.get("choices") if isinstance(item,dict) else None
    valid=valid and isinstance(choices,list) and bool(choices) and isinstance(choices[0],dict) and isinstance(choices[0].get("delta"),dict)
    chunks+=1
   conn.close(); passed+=bool(valid and done and chunks)
  elif provider=="openai":
   proc=subprocess.Popen(["opencode","run","--model",model,"--format","json","Reply with OK only."],
                         stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
   events=0; response_events=0; bytes_seen=0
   for line in proc.stdout:
    bytes_seen+=len(line)
    if len(line)>16384 or bytes_seen>262144: raise ValueError
    event=json.loads(line); schema_ok=isinstance(event,dict) and isinstance(event.get("type"),str)
    if not schema_ok: raise ValueError
    events+=1; response_events+=event.get("type") in {"text","step-finish","message"}
   code=proc.wait(timeout=60)
   passed+=bool(code==0 and events>0 and response_events>0)
  else: schema=False; break
 except Exception:
  schema=False
print(json.dumps({"provider":provider,"streams_attempted":5,"streams_passed":passed,"schema_valid":schema}))'''
    env_values = {"ollama": {"LM_PROVIDER": "ollama"},
                  "llamacpp": {"LM_PROVIDER": "llamacpp"},
                  "openai": {"LM_PROVIDER": "openai"}}.get(selection["provider"], {})
    command = "env " + " ".join(f"{key}={shlex.quote(value)}" for key, value in
                                 {**env_values, "LM_MODEL": selection["model_id"]}.items())
    command += " python3 -c " + shlex.quote(script)
    try:
        completed = _guest_exec(state_root, profile, command, timeout=420)
    except TimeoutError:
        return {"status": "BLOCKED", "reason_code": "provider_stream_deadline_exceeded",
                "streams_attempted": 0, "streams_passed": 0}
    except AcceptanceError:
        return {"status": "BLOCKED", "reason_code": "provider_guest_unavailable",
                "streams_attempted": 0, "streams_passed": 0}
    parsed = _validate_provider_stream_report(completed.stdout, selection["provider"])
    if parsed is None:
        return {"status": "FAIL", "reason_code": "provider_stream_schema_invalid",
                "streams_attempted": 0, "streams_passed": 0}
    passed = parsed["schema_valid"] and parsed["streams_passed"] == 5
    return {"status": "PASS" if passed else "BLOCKED",
            "reason_code": None if passed else "provider_stream_incomplete",
            "streams_attempted": parsed["streams_attempted"],
            "streams_passed": parsed["streams_passed"]}


def _provider_stream_checks(state_root: Path, run_id: str, workspace: Path,
                            report: dict[str, Any], memory_gib: int) -> dict[str, Any]:
    entries = report.get("provider_config", {}).get("providers", [])
    if not isinstance(entries, list):
        return {"status": "BLOCKED", "reason_code": "provider_manifest_invalid",
                "providers": [], "automated_status": "BLOCKED"}
    provider_results: list[dict[str, Any]] = [
        {"provider": "openai", "model_id": item.get("model_id"), "status": "DEFERRED",
         "reason_code": "owner_deferred_live_openai"}
        for item in entries if isinstance(item, dict) and item.get("provider") == "openai"
    ]
    local_entries = [item for item in entries if isinstance(item, dict)
                     and item.get("provider") in {"ollama", "llamacpp"}]
    if not local_entries:
        return {"status": "BLOCKED", "reason_code": "local_provider_not_selected",
                "providers": provider_results, "automated_status": "BLOCKED"}
    selections = {item.get("provider"): item for item in entries if isinstance(item, dict)}
    discovery = {item.get("provider"): item for item in
                 report.get("provider_config", {}).get("selections", []) if isinstance(item, dict)}
    for provider in ("ollama", "llamacpp"):
        selection = selections.get(provider)
        if selection is None:
            continue
        validation = discovery.get(provider, {})
        if provider != "openai" and validation.get("status") != "AVAILABLE":
            provider_results.append({"provider": provider, "status": "BLOCKED",
                                     "reason_code": "selected_provider_unavailable",
                                     "streams_attempted": 0, "streams_passed": 0})
            continue
        profile, data, reason = _ensure_provider_profile(
            state_root, run_id, workspace, report, selection, memory_gib,
        )
        if data is None:
            provider_results.append({"provider": provider, "status": "BLOCKED",
                                     "reason_code": reason,
                                     "streams_attempted": 0, "streams_passed": 0})
            continue
        result = _provider_stream_check(state_root, profile, selection)
        result["provider"] = provider
        result["profile_name"] = profile
        result["profile_id"] = data.get("profile_id")
        result["guest_payload_sha256"] = data.get("guest_payload_sha256")
        result["runtime_payload_sha256"] = data.get("runtime_payload_sha256")
        report.setdefault("provider_profiles", {})[provider] = {
            "profile_name": profile, "profile_id": data.get("profile_id"),
            "image_sha256": data.get("image_sha256"),
            "guest_payload_sha256": data.get("guest_payload_sha256"),
            "runtime_payload_sha256": data.get("runtime_payload_sha256"),
            "model_id": selection["model_id"],
        }
        if not _suspend_profile(state_root, profile, memory_gib):
            result["status"] = "BLOCKED"
            result["reason_code"] = "provider_profile_suspend_unconfirmed"
        provider_results.append(result)
        _atomic_replace_json(_campaign_report_path(state_root, run_id), report)
    local_results = [item for item in provider_results if item.get("provider") in {"ollama", "llamacpp"}]
    all_passed = bool(local_results) and all(item.get("status") == "PASS" for item in local_results)
    primary_profile = _profile_name(run_id)
    _primary, restore_reason = _resume_campaign_profile(
        state_root, run_id, report, primary_profile, memory_gib,
    )
    all_passed = all_passed and restore_reason is None
    return {"status": "PASS" if all_passed else "BLOCKED",
            "automated_status": "PASS" if all_passed else "BLOCKED",
            "reason_code": None if all_passed else restore_reason or "provider_or_owner_evidence_incomplete",
            "providers": provider_results}


def _lifecycle_retention_checks(state_root: Path, profile: str, workspace: Path,
                                memory_gib: int, apply_selected_updates: bool,
                                workload_run_id: str | None = None,
                                workload_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    if memory_gib < 8:
        return {"status": "BLOCKED", "reason_code": "lifecycle_resize_requires_8_gib_outer_guest"}
    before = _profile_record(state_root, profile)
    key_path = state_root / "vm/profiles" / profile / "control-key"
    key_hash = _hash_file(key_path)
    overlay_identity = _overlay_identity(state_root, profile)
    if (before.get("status") != "ready" or key_hash is None or overlay_identity is None
            or not _listener_owned_loopback(before)):
        return {"status": "BLOCKED", "reason_code": "outer_profile_not_verified_ready"}
    profile_identity = before.get("profile_id")
    image_hash = before.get("image_sha256")
    workspace_path = before.get("workspace")
    runtime_payload_hash = before.get("runtime_payload_sha256")
    suspend = _lifecycle_call(state_root, profile, workspace, "suspend", memory_gib, timeout=90)
    if suspend.returncode != 0:
        return {"status": "BLOCKED" if suspend.returncode == 2 else "FAIL",
                "reason_code": "graceful_suspend_failed", "suspend_exit_code": suspend.returncode}
    suspended = _profile_record(state_root, profile)
    if (suspended.get("status") != "suspended" or suspended.get("profile_id") != profile_identity
            or _overlay_identity(state_root, profile) != overlay_identity
            or _hash_file(key_path) != key_hash):
        return {"status": "FAIL", "reason_code": "suspend_state_not_retained"}
    resume = _lifecycle_call(state_root, profile, workspace, "resume", memory_gib, timeout=1900)
    if resume.returncode != 0:
        return {"status": "BLOCKED" if resume.returncode == 2 else "FAIL",
                "reason_code": "graceful_resume_failed", "resume_exit_code": resume.returncode}
    after = _profile_record(state_root, profile)
    if (after.get("status") != "ready" or after.get("profile_id") != profile_identity
            or after.get("image_sha256") != image_hash or after.get("workspace") != workspace_path
            or after.get("runtime_payload_sha256") != runtime_payload_hash
            or _overlay_identity(state_root, profile) != overlay_identity
            or _hash_file(key_path) != key_hash or not _listener_owned_loopback(after)):
        return {"status": "FAIL", "reason_code": "resume_state_not_retained"}

    workload_retention = None
    if workload_run_id is not None:
        try:
            workload_retention = _guest_workload_retention(state_root, profile, workload_run_id)
        except (AcceptanceError, OSError, RuntimeError, TimeoutError, ValueError):
            if workload_evidence is not None:
                workload_evidence.update({"status": "BLOCKED",
                                          "reason_code": "workload_retention_probe_unavailable"})
            return {"status": "BLOCKED", "reason_code": "workload_retention_probe_unavailable"}
        workload_status = ("PASS" if workload_retention["database_canary_retained"]
                           and workload_retention["compose_stopped"] else "FAIL")
        if workload_evidence is not None:
            workload_evidence.update({
                "status": workload_status,
                "reason_code": None if workload_status == "PASS" else "workload_state_not_retained_across_vm_suspend",
                **workload_retention,
            })
        if not workload_retention["database_canary_retained"] or not workload_retention["compose_stopped"]:
            return {"status": "FAIL", "reason_code": "workload_state_not_retained_across_vm_suspend",
                    "workload_retention": workload_retention}

    scan = _lifecycle_call(state_root, profile, workspace, "update-scan", memory_gib, timeout=360)
    scan_text = scan.stdout.decode("ascii", "ignore")
    if scan.returncode != 0 or not any(marker in scan_text for marker in (
            "CURRENT  selected guest targets already match",
            "CANCELLED  guest and profile state were not changed",
    )):
        return {"status": "BLOCKED" if scan.returncode == 2 else "FAIL",
                "reason_code": "update_scan_or_cancel_failed", "update_scan_exit_code": scan.returncode}
    apply_result = "NOT_AUTHORIZED"
    if apply_selected_updates:
        applied = _lifecycle_call(state_root, profile, workspace, "update-apply", memory_gib, timeout=960)
        apply_text = applied.stdout.decode("ascii", "ignore")
        if applied.returncode != 0:
            return {"status": "BLOCKED" if applied.returncode == 2 else "FAIL",
                    "reason_code": "selected_update_apply_failed", "update_apply_exit_code": applied.returncode}
        if re.search(r"(?m)^UPDATED\s+selected guest updates completed;", apply_text) is None:
            return {"status": "BLOCKED", "reason_code": "selected_update_apply_not_performed"}
        after_update = _profile_record(state_root, profile)
        if (after_update.get("profile_id") != profile_identity
                or after_update.get("workspace") != workspace_path
                or _overlay_identity(state_root, profile) != overlay_identity
                or _hash_file(key_path) != key_hash or not _listener_owned_loopback(after_update)):
            return {"status": "FAIL", "reason_code": "selected_update_state_not_retained"}
        after = after_update
        apply_result = "PASS"
    memory_before = _guest_memory_mib(state_root, profile)
    if memory_gib >= 8:
        down = _lifecycle_call(state_root, profile, workspace, "resize", 4, timeout=300)
        if down.returncode != 0:
            return {"status": "BLOCKED" if down.returncode == 2 else "FAIL",
                    "reason_code": "graceful_resize_down_failed", "resize_down_exit_code": down.returncode}
        low_data = _profile_record(state_root, profile)
        memory_low = _guest_memory_mib(state_root, profile)
        up = _lifecycle_call(state_root, profile, workspace, "resize", memory_gib, timeout=300)
        if up.returncode != 0:
            return {"status": "BLOCKED" if up.returncode == 2 else "FAIL",
                    "reason_code": "graceful_resize_restore_failed", "resize_restore_exit_code": up.returncode}
        restored = _profile_record(state_root, profile)
        memory_restored = _guest_memory_mib(state_root, profile)
        low_ok = memory_low is not None and 3 * 1024 <= memory_low <= 4 * 1024 + 256
        restored_ok = memory_restored is not None and (memory_gib * 1024 - 256) <= memory_restored <= memory_gib * 1024 + 256
        if (not low_ok or not restored_ok or low_data.get("status") != "ready"
                or low_data.get("memory_gib") != 4 or low_data.get("profile_id") != profile_identity
                or restored.get("profile_id") != profile_identity
                or restored.get("memory_gib") != memory_gib
                or restored.get("workspace") != workspace_path
                or _overlay_identity(state_root, profile) != overlay_identity
                or _hash_file(key_path) != key_hash or not _listener_owned_loopback(restored)):
            return {"status": "FAIL", "reason_code": "resize_guest_memory_or_identity_invalid"}
        after = restored
    return {
        "status": "PASS",
        "profile_name": profile,
        "profile_identity_retained": True,
        "workspace_retained": True,
        "base_image_retained": True,
        "overlay_and_key_retained": True,
        "suspend_resume": "PASS",
        "update_scan_cancel": "PASS",
        "selected_update_apply": apply_result,
        "opencode_version": after.get("opencode_version"),
        "resize_memory_before_mib": memory_before,
        "resize_restore_verified": memory_gib >= 8,
        "workload_retention": workload_retention,
    }


def _self_test_gate(state_root: Path, parent_profile: str, workspace: Path,
                    evidence_root: Path) -> dict[str, Any]:
    self_test = _load_module("vm_host_acceptance_self_test", ROOT / "scripts/vm-self-test.py")
    run_id = uuid.uuid4().hex
    report_root = self_test._report_root_from_state(str(state_root))
    adapter = self_test.LiveSelfTestAdapter(
        workspace, state_root, report_root, run_id, parent_profile=parent_profile,
    )
    runner = self_test.SelfTestRunner(
        workspace, report_root, adapter, max_depth=2, yes=True, run_id=run_id,
        total_deadline_seconds=_bounded_timeout(STAGE_BUDGET_SECONDS),
    )
    report, exit_code = runner.run()
    report_path = report_root / f"{run_id}.json"
    if not report_path.is_file() or report_path.is_symlink():
        return {"status": "FAIL", "reason_code": "self_test_report_missing"}

    cleanup_status = "BLOCKED"
    cleanup_reason = None
    try:
        self_test.cleanup_live_self_test(str(state_root), run_id, run_id)
        cleanup_status = "PASS"
        report = _read_private_json(report_path)
    except (OSError, RuntimeError, ValueError, KeyError):
        cleanup_reason = "exact_self_test_cleanup_unconfirmed"
        try:
            report = _read_private_json(report_path)
        except (OSError, UnicodeError, json.JSONDecodeError):
            cleanup_reason = "self_test_report_unreadable"

    archive_path = evidence_root / f"selftest-{run_id}.json"
    if archive_path.exists() or archive_path.is_symlink():
        return {"status": "FAIL", "reason_code": "self_test_evidence_collision"}
    archived_report = json.loads(json.dumps(report))
    diagnostic_refs: list[str] = []
    failure_evidence = report.get("stages", {}).get("expected_boot_failure", {}).get("evidence", {})
    for reference in failure_evidence.get("diagnostic_refs", []):
        if not isinstance(reference, str) or reference != f"{run_id}-failure.json":
            return {"status": "FAIL", "reason_code": "self_test_diagnostic_manifest_invalid"}
        diagnostic_path = report_root / reference
        info = diagnostic_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > 16 * 1024):
            return {"status": "FAIL", "reason_code": "self_test_diagnostic_unsafe"}
        diagnostic = _read_private_json(diagnostic_path, maximum=16 * 1024)
        if diagnostic.get("run_id") != run_id or diagnostic.get("schema_version") != 1:
            return {"status": "FAIL", "reason_code": "self_test_diagnostic_invalid"}
        diagnostic_archive = evidence_root / f"selftest-{run_id}-failure.json"
        _atomic_json(diagnostic_archive, diagnostic)
        diagnostic_refs.append(diagnostic_archive.name)
    if diagnostic_refs:
        archived_report["preserved_diagnostic_refs"] = [
            {"stage": "expected_boot_failure", "path": name} for name in diagnostic_refs
        ]
        archived_report["stages"]["expected_boot_failure"]["evidence"]["diagnostic_refs"] = diagnostic_refs
    _atomic_json(archive_path, archived_report)
    digest = _hash_file(archive_path)
    stages = report.get("stages", {})
    result = report.get("result")
    depth_status = stages.get("depth", {}).get("status")
    depth_evidence = stages.get("depth", {}).get("evidence", {})
    passed = exit_code == 0 and result == "PASS" and cleanup_status == "PASS"
    status = "PASS" if passed else "FAIL" if result == "FAIL" else "BLOCKED"
    return {
        "status": status,
        "reason_code": cleanup_reason,
        "run_id": run_id,
        "self_test_result": result,
        "self_test_exit_code": exit_code,
        "depth_gate_status": depth_status,
        "cleanup_status": cleanup_status,
        "report_ref": archive_path.name,
        "report_sha256": digest,
        "g1_boundary_status": stages.get("boundary", {}).get("status"),
        "g2_boundary_verified": depth_evidence.get("g2_boundary_verified") is True,
        "g2_boundary_report_sha256": depth_evidence.get("g2_boundary_report_sha256"),
        "stage_statuses": {
            name: item.get("status") for name, item in stages.items()
            if name in self_test.STAGE_ORDER
        },
    }


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    encoded = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    if len(encoded) > MAX_REPORT_BYTES:
        raise AcceptanceError("acceptance report exceeds the bounded size")
    fd, temporary_name = tempfile.mkstemp(prefix=".acceptance-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _new_campaign(state_root: Path, run_id: str, source: Path, suites: list[str],
                  preflight_report: dict[str, Any], campaign_scope: dict[str, Any]) -> tuple[Path, Path, Path]:
    paths = _load_module("vm_host_acceptance_paths", PATHS_PATH)
    created = paths.recursion_run_create(state_root, _run_name(run_id))
    run_root = Path(created["run_root"])
    child_state = Path(created["state_root"])
    _private_directory(run_root)
    _private_directory(child_state)
    report_root = run_root.parent / "reports"
    if report_root.exists() or report_root.is_symlink():
        _private_directory(report_root)
    else:
        report_root.mkdir(mode=0o700)
        os.chmod(report_root, 0o700)
        _private_directory(report_root)
    snapshot = _load_module("vm_host_acceptance_snapshot", SNAPSHOT_PATH)
    report = {
        "schema_version": 1,
        "workflow_version": WORKFLOW_VERSION,
        "run_id": run_id,
        "run_name": _run_name(run_id),
        "created_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "source": preflight_report["source"],
        "snapshot": {"tree_sha256": None, "entry_count": None, "total_bytes": None},
        "suites": suites,
        "required_gates": _required_gates(suites),
        "acceptance_scope": _acceptance_scope(suites),
        "campaign_scope": campaign_scope,
        "synthetic_canary": {"status": "PENDING", "encoding_checks": ["raw", "hex", "base64"]},
        "stages": {stage: "NOT_RUN" for stage in STAGE_ORDER},
        "stage_elapsed_ms": {stage: 0 for stage in STAGE_ORDER},
        "legacy_elapsed_ms": 0,
        "approved_total_budget_seconds": preflight_report.get("approved_total_budget_seconds"),
        "provider_config_sha256": preflight_report.get("provider_config", {}).get("sha256"),
        "protected_assets": ["default profiles", "shared state outside this run", "host Docker session", "credentials"],
        "owned_profiles": [],
        "gates": {
            gate: ({"status": "DEFERRED", "reason_code": DEFERRED_GATE_REASONS[gate]}
                   if gate in DEFERRED_GATE_REASONS else {"status": "NOT_RUN"})
            for gate in GATE_IDS
        },
        "result": "BLOCKED",
    }
    report_path = report_root / f"{_run_name(run_id)}.json"
    _atomic_json(report_path, report)
    report["synthetic_canary"]["status"] = "GENERATING"
    _atomic_replace_json(report_path, report)
    _create_canary_fixture(state_root, run_id)
    report["synthetic_canary"]["status"] = "GENERATED"
    _atomic_replace_json(report_path, report)
    prepared = snapshot.prepare(source, run_root, 2 * 1024**3)
    workspace = Path(prepared["workspace"])
    report["snapshot"] = {
        "tree_sha256": prepared["tree_sha256"],
        "entry_count": prepared["entry_count"],
        "total_bytes": prepared["total_bytes"],
    }
    _atomic_replace_json(report_path, report)
    return run_root, child_state, workspace


def _campaign_report_path(state_root: Path, run_id: str) -> Path:
    run_root = _namespace_path(state_root, _run_name(run_id))
    return run_root.parent / "reports" / f"{_run_name(run_id)}.json"


def _approved_budget_seconds(minutes: int | None) -> int:
    if type(minutes) is not int or not 1 <= minutes <= MAX_APPROVED_TOTAL_MINUTES:
        raise AcceptanceError(
            f"staged execution requires an explicit total budget from 1 to {MAX_APPROVED_TOTAL_MINUTES} minutes"
        )
    return minutes * 60


def _campaign_elapsed_ms(report: dict[str, Any]) -> int:
    values = report.get("stage_elapsed_ms")
    if not isinstance(values, dict) or set(values) != set(STAGE_ORDER):
        raise AcceptanceError("campaign stage-time ledger is invalid")
    elapsed = 0
    for value in values.values():
        if type(value) is not int or not 0 <= value <= STAGE_BUDGET_SECONDS * 1000:
            raise AcceptanceError("campaign stage-time entry is outside the fixed stage budget")
        elapsed += value
    return elapsed


def _campaign_result(report: dict[str, Any]) -> str:
    required = report.get("required_gates", [])
    gates = report.get("gates", {})
    if not isinstance(required, list) or not required or not isinstance(gates, dict):
        return "BLOCKED"
    suites = report.get("suites")
    if (not isinstance(suites, list) or any(suite not in ALL_SUITES for suite in suites)
            or report.get("required_gates") != _required_gates(suites)
            or report.get("acceptance_scope") != _acceptance_scope(suites)):
        return "BLOCKED"
    statuses = [gates.get(gate, {}).get("status", "NOT_RUN") for gate in required]
    if "FAIL" in statuses:
        return "FAIL"
    cleanup = report.get("cleanup", {})
    stages = report.get("stages", {})
    canary = report.get("synthetic_canary", {})
    security = gates.get("SEC-SECRET", {})
    if (not all(status == "PASS" for status in statuses)
            or not isinstance(cleanup, dict) or cleanup.get("status") != "PASS"
            or cleanup.get("synthetic_canary_removed") is not True
            or not isinstance(canary, dict) or canary.get("status") != "REMOVED"
            or not isinstance(security, dict)
            or not isinstance(security.get("final_report_check"), dict)
            or security["final_report_check"].get("status") != "PASS"
            or not isinstance(stages, dict) or stages.get("teardown") != "PASS"):
        return "BLOCKED"
    return "PASS"


def _attempt_revision(source_identity: dict[str, Any], provider_config_sha256: str) -> str:
    payload = json.dumps([
        source_identity.get("source_head"), source_identity.get("tree_sha256"),
        provider_config_sha256,
    ], separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _diagnostic_reason_codes(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        reason = value.get("reason_code")
        if isinstance(reason, str) and re.fullmatch(r"[a-z][a-z0-9_]{1,95}", reason):
            found.add(reason)
        for child in value.values():
            found.update(_diagnostic_reason_codes(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_diagnostic_reason_codes(child))
    return found


def _load_attempt_ledger(state_root: Path) -> tuple[dict[str, Any], Path]:
    path = state_root / "acceptance-attempts.json"
    if path.exists() or path.is_symlink():
        if path.is_symlink():
            raise AcceptanceError("acceptance attempt ledger must not be a symlink")
        ledger = _read_private_json(path, maximum=256 * 1024)
        if set(ledger) != {"schema_version", "attempts"} or ledger.get("schema_version") != 1 \
                or not isinstance(ledger.get("attempts"), list):
            raise AcceptanceError("acceptance attempt ledger schema is invalid")
        for item in ledger["attempts"]:
            required = {"revision_sha256", "run_id", "source_head", "source_tree_sha256",
                        "provider_config_sha256", "approved_total_budget_seconds",
                        "scope_sha256", "prior_elapsed_ms", "started_utc", "retry_of", "retry_reason"}
            if (not isinstance(item, dict) or set(item) != required
                    or not isinstance(item.get("revision_sha256"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", item["revision_sha256"])
                    or not isinstance(item.get("run_id"), str) or RUN_ID_RE.fullmatch(item["run_id"]) is None
                    or not isinstance(item.get("source_head"), str)
                    or not isinstance(item.get("source_tree_sha256"), str)
                    or re.fullmatch(r"[a-f0-9]{64}", item["source_tree_sha256"]) is None
                    or not isinstance(item.get("provider_config_sha256"), str)
                    or re.fullmatch(r"[a-f0-9]{64}", item["provider_config_sha256"]) is None
                    or not isinstance(item.get("scope_sha256"), str)
                    or re.fullmatch(r"[a-f0-9]{64}", item["scope_sha256"]) is None
                    or type(item.get("approved_total_budget_seconds")) is not int
                    or not 60 <= item["approved_total_budget_seconds"] <= MAX_APPROVED_TOTAL_MINUTES * 60
                    or type(item.get("prior_elapsed_ms")) is not int or item["prior_elapsed_ms"] < 0
                    or not isinstance(item.get("started_utc"), str)
                    or (item.get("retry_of") is not None
                        and (not isinstance(item.get("retry_of"), str)
                             or RUN_ID_RE.fullmatch(item["retry_of"]) is None))
                    or (item.get("retry_reason") is not None
                        and (not isinstance(item.get("retry_reason"), str)
                             or re.fullmatch(r"[a-z][a-z0-9_]{1,95}", item["retry_reason"]) is None))
                    or ((item.get("retry_of") is None) != (item.get("retry_reason") is None))):
                raise AcceptanceError("acceptance attempt ledger contains an invalid entry")
        if len(ledger["attempts"]) > 512:
            raise AcceptanceError("acceptance attempt ledger exceeds its bounded history")
        return ledger, path
    return {"schema_version": 1, "attempts": []}, path


def _register_campaign_attempt(state_root: Path, run_id: str, source_identity: dict[str, Any],
                               provider_config_sha256: str, *, retry_of: str | None,
                               retry_reason: str | None,
                               approved_total_budget_seconds: int,
                               campaign_scope: dict[str, Any] | None = None) -> dict[str, Any]:
    if (retry_of is None) != (retry_reason is None):
        raise AcceptanceError("a fresh retry requires both --retry-of and --retry-reason")
    revision = _attempt_revision(source_identity, provider_config_sha256)
    scope = campaign_scope or {
        "scope_id": LOCAL_FIRST_SCOPE_ID, "suites": [], "memory_gib": 8,
        "provider_config_sha256": provider_config_sha256, "apply_selected_updates": False,
        "approved_total_budget_seconds": approved_total_budget_seconds,
    }
    scope_raw = json.dumps(scope, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    scope_sha256 = hashlib.sha256(scope_raw).hexdigest()
    ledger, path = _load_attempt_ledger(state_root)
    prior = [item for item in ledger["attempts"] if item["revision_sha256"] == revision]
    if not 60 <= approved_total_budget_seconds <= MAX_APPROVED_TOTAL_MINUTES * 60:
        raise AcceptanceError("approved campaign budget is outside the bounded time range")
    if len(prior) >= 2:
        raise AcceptanceError("the one fresh retry limit for this source/config revision is exhausted")
    prior_elapsed_ms = 0
    if not prior:
        if retry_of is not None or retry_reason is not None:
            raise AcceptanceError("--retry-of is valid only after one prior exact-revision attempt")
    else:
        previous = prior[0]
        if any(item.get("scope_sha256") != scope_sha256 for item in prior):
            raise AcceptanceError("suite, memory, provider, update effects, or total budget changed; retry refused")
        if any(item["approved_total_budget_seconds"] != approved_total_budget_seconds for item in prior):
            raise AcceptanceError("approved total campaign budget cannot increase or change on retry")
        if (retry_of != previous["run_id"] or retry_reason is None
                or not re.fullmatch(r"[a-z][a-z0-9_]{1,95}", retry_reason)
                or retry_reason in {"not_run", "owner_observation_required", "owner_live_evidence_required"}):
            raise AcceptanceError("fresh retry must identify the prior run and a diagnosed operational reason")
        previous_path = _campaign_report_path(state_root, retry_of)
        previous_report = _read_private_json(previous_path)
        if (previous_report.get("run_id") != retry_of
                or previous_report.get("result") not in {"FAIL", "BLOCKED"}
                or not isinstance(previous_report.get("cleanup"), dict)
                or previous_report["cleanup"].get("status") != "PASS"
                or previous_report.get("stages", {}).get("teardown") != "PASS"
                or previous_report.get("source", {}).get("source_head") != source_identity.get("source_head")
                or previous_report.get("source", {}).get("tree_sha256") != source_identity.get("tree_sha256")
                or previous_report.get("provider_config_sha256") != provider_config_sha256
                or previous_report.get("campaign_scope") != scope
                or retry_reason not in _diagnostic_reason_codes(previous_report)):
            raise AcceptanceError("retry reason is not diagnosed in the prior exact-revision campaign report")
        for item in prior:
            if item["approved_total_budget_seconds"] != approved_total_budget_seconds:
                raise AcceptanceError("prior attempt budget does not match this retry")
            report_path = _campaign_report_path(state_root, item["run_id"])
            report = _read_private_json(report_path)
            if (report.get("run_id") != item["run_id"]
                    or report.get("approved_total_budget_seconds") != approved_total_budget_seconds
                    or report.get("stages", {}).get("teardown") != "PASS"
                    or not isinstance(report.get("cleanup"), dict)
                    or report["cleanup"].get("status") != "PASS"):
                raise AcceptanceError("prior attempt time accounting is incomplete")
            prior_elapsed_ms += _campaign_elapsed_ms(report)
        if (prior_elapsed_ms + TEARDOWN_RESERVE_SECONDS * 1000
                >= approved_total_budget_seconds * 1000):
            raise AcceptanceError("approved budget cannot preserve the teardown reserve; retry is refused")
    if any(item["run_id"] == run_id for item in ledger["attempts"]):
        raise AcceptanceError("run ID already exists in the acceptance attempt ledger")
    entry = {
        "revision_sha256": revision,
        "run_id": run_id,
        "source_head": source_identity.get("source_head"),
        "source_tree_sha256": source_identity.get("tree_sha256"),
        "provider_config_sha256": provider_config_sha256,
        "approved_total_budget_seconds": approved_total_budget_seconds,
        "scope_sha256": scope_sha256,
        "prior_elapsed_ms": prior_elapsed_ms,
        "started_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "retry_of": retry_of,
        "retry_reason": retry_reason,
    }
    ledger["attempts"].append(entry)
    if path.exists():
        _atomic_replace_json(path, ledger)
    else:
        _atomic_json(path, ledger)
    return {"ordinal": len(prior) + 1, "retry_of": retry_of, "retry_reason": retry_reason,
            "prior_elapsed_ms": prior_elapsed_ms}


def _canary_artifact_manifest(state_root: Path, run_id: str, report: dict[str, Any],
                              report_path: Path) -> tuple[dict[str, Any], list[Path]]:
    run_root = _namespace_path(state_root, _run_name(run_id))
    child_state = run_root / "state"
    workspace = run_root / "candidate-snapshot" / "workspace"
    artifacts: list[dict[str, str]] = []
    roots = [run_root, report_path.parent]

    def add(artifact_id: str, path: Path, kind: str, reason_code: str | None = None) -> None:
        entry = {"id": artifact_id, "path": str(path), "kind": kind}
        if reason_code is not None:
            entry["reason_code"] = reason_code
        artifacts.append(entry)

    add("snapshot_workspace", workspace, "tree")
    add("snapshot_git_history", workspace / ".git", "git-loose")

    profiles = report.get("owned_profiles")
    if not isinstance(profiles, list):
        raise AcceptanceError("campaign profile manifest is invalid for canary inspection")
    role_depth = {"outer": 0, "agent": 1, "workload": 1,
                  "provider-ollama": 0, "provider-llamacpp": 0, "provider-openai": 0}
    seen: set[str] = set()
    image_paths: dict[str, Path] = {}
    image_unavailable = False
    for item in profiles:
        if not isinstance(item, dict) or set(item) != {"profile_name", "depth", "run_id"}:
            raise AcceptanceError("campaign profile manifest is invalid for canary inspection")
        name = item["profile_name"]
        role = next((key for key in role_depth if name == _profile_name(run_id, key)), None) \
            if isinstance(name, str) else None
        if (role is None or item["run_id"] != run_id or item["depth"] != role_depth[role]
                or name in seen):
            raise AcceptanceError("campaign profile ownership is invalid for canary inspection")
        seen.add(name)
        profile_dir = child_state / "vm/profiles" / name
        suffix = role.replace("-", "_")
        add(f"seed_user_data_{suffix}", profile_dir / "seed.user-data", "file")
        add(f"seed_iso_{suffix}", profile_dir / "seed.iso", "iso9660")
        add(f"campaign_logs_{suffix}", profile_dir / "runs", "logs")
        add(f"profile_overlay_{suffix}", profile_dir / "disk.qcow2", "opaque-qcow2")
        try:
            data = _profile_record(child_state, name)
            image_path = Path(data["image_path"])
            image_digest = data.get("image_sha256")
            if (not isinstance(image_digest, str) or re.fullmatch(r"[a-f0-9]{64}", image_digest) is None
                    or image_path != child_state / "vm/images" / image_digest / "base.qcow2"):
                raise ValueError
            image_paths[image_digest] = image_path
        except (AcceptanceError, OSError, RuntimeError, ValueError, KeyError, TypeError):
            image_unavailable = True

    for digest, image_path in sorted(image_paths.items()):
        image_id = f"base_image_{digest[:16]}"
        add(image_id, image_path, "opaque-qcow2")
        add(f"base_manifest_{digest[:16]}", image_path.parent / "manifest.json", "file")
    if image_unavailable or not profiles:
        missing_image = child_state / "vm/images/unverified/base.qcow2"
        add("base_image_unverified", missing_image, "opaque-qcow2")

    self_gate = report.get("gates", {}).get("E2E-SELF", {})
    self_report_ref = self_gate.get("report_ref") if isinstance(self_gate, dict) else None
    if self_report_ref is not None:
        self_run_id = self_gate.get("run_id")
        if (not isinstance(self_run_id, str) or RUN_ID_RE.fullmatch(self_run_id) is None
                or self_report_ref != f"selftest-{self_run_id}.json"):
            raise AcceptanceError("self-test report reference is invalid for canary inspection")
        add("nested_self_test_report", report_path.parent / self_report_ref, "file")
        diagnostics = self_gate.get("preserved_diagnostic_refs", [])
        if not isinstance(diagnostics, list) or len(diagnostics) > 32:
            raise AcceptanceError("self-test diagnostic manifest is invalid for canary inspection")
        for index, reference in enumerate(diagnostics):
            if (not isinstance(reference, dict) or reference.get("stage") != "expected_boot_failure"
                    or not isinstance(reference.get("path"), str)
                    or reference["path"] != f"selftest-{self_run_id}-failure.json"):
                raise AcceptanceError("self-test diagnostic reference is invalid for canary inspection")
            add(f"nested_self_test_diagnostic_{index}", report_path.parent / reference["path"], "file")
        # The live self-test removes nested seed/images before returning its
        # archive. Do not imply those transient artifacts were inspected.
        add("nested_transient_images", run_root, "unsupported",
            "nested_transient_images_not_available_for_post_run_inspection")

    manifest = {
        "schema_version": 1,
        "required_ids": [item["id"] for item in artifacts],
        "artifacts": artifacts,
    }
    return manifest, roots


def _run_canary_scan(state_root: Path, run_id: str, report: dict[str, Any],
                     report_path: Path) -> dict[str, Any]:
    checker = _load_module("vm_host_acceptance_canary", CANARY_CHECK_PATH)
    try:
        canary = checker.read_canary(_canary_path(state_root, run_id))
        manifest, roots = _canary_artifact_manifest(state_root, run_id, report, report_path)
        return checker.scan_manifest(
            canary, manifest, roots, timeout_seconds=_bounded_timeout(60),
        )
    except (AcceptanceError, OSError, RuntimeError, ValueError, KeyError, TypeError):
        return {"schema_version": 1, "result": "BLOCKED",
                "checks": [{"id": "campaign_manifest", "status": "BLOCKED",
                            "reason_code": "canary_inspection_unavailable"}],
                "limits": {"files_scanned": 0, "bytes_scanned": 0,
                           "timeout_seconds": 60}}


def _canary_result_reason(scan: dict[str, Any]) -> str | None:
    checks = scan.get("checks")
    if not isinstance(checks, list):
        return "canary_inspection_unavailable"
    for item in checks:
        if isinstance(item, dict) and item.get("status") in {"FAIL", "BLOCKED"}:
            reason = item.get("reason_code")
            if isinstance(reason, str) and re.fullmatch(r"[a-z][a-z0-9_]{1,63}", reason):
                return reason
            return "canary_inspection_incomplete"
    return None


def _canary_check_status(scan: dict[str, Any], *, exact_ids: set[str] | None = None,
                         prefixes: tuple[str, ...] = ()) -> str:
    checks = scan.get("checks")
    if not isinstance(checks, list):
        return "BLOCKED"
    selected = [item for item in checks if isinstance(item, dict)
                and ((exact_ids is not None and item.get("id") in exact_ids)
                     or any(isinstance(item.get("id"), str)
                            and item["id"].startswith(prefix) for prefix in prefixes))]
    if not selected or (exact_ids is not None
                        and {item.get("id") for item in selected} != exact_ids):
        return "BLOCKED"
    statuses = [item.get("status") for item in selected]
    if "FAIL" in statuses:
        return "FAIL"
    if any(status != "PASS" for status in statuses):
        return "BLOCKED"
    return "PASS"


def _finalize_canary_report_check(report_path: Path, report: dict[str, Any],
                                  canary: bytes | None) -> dict[str, Any]:
    checker = _load_module("vm_host_acceptance_canary_finalize", CANARY_CHECK_PATH)
    security = report.setdefault("gates", {}).setdefault("SEC-SECRET", {"status": "BLOCKED"})
    if canary is None:
        if security.get("status") != "FAIL":
            security["status"] = "BLOCKED"
            security["reason_code"] = "canary_fixture_unavailable"
        security["final_report_check"] = {
            "status": "BLOCKED", "reason_code": "canary_fixture_unavailable",
        }
        security.setdefault("required_subchecks", {})[
            "canary-absent-from-campaign-and-self-test-reports"
        ] = "BLOCKED"
        report["result"] = _campaign_result(report)
        _atomic_replace_json(report_path, report)
        return report
    final_artifacts = [{"id": "campaign_report", "path": str(report_path), "kind": "file"}]
    self_gate = report.get("gates", {}).get("E2E-SELF", {})
    self_report_ref = self_gate.get("report_ref") if isinstance(self_gate, dict) else None
    if self_report_ref is not None:
        self_run_id = self_gate.get("run_id")
        if (not isinstance(self_run_id, str) or RUN_ID_RE.fullmatch(self_run_id) is None
                or self_report_ref != f"selftest-{self_run_id}.json"):
            security["status"] = "BLOCKED"
            security["reason_code"] = "self_test_report_reference_invalid"
            security["final_report_check"] = {
                "status": "BLOCKED", "reason_code": "self_test_report_reference_invalid",
            }
            security.setdefault("required_subchecks", {})[
                "canary-absent-from-campaign-and-self-test-reports"
            ] = "BLOCKED"
            report["result"] = _campaign_result(report)
            _atomic_replace_json(report_path, report)
            return report
        final_artifacts.append({"id": "nested_self_test_report",
                                "path": str(report_path.parent / self_report_ref), "kind": "file"})
        references = self_gate.get("preserved_diagnostic_refs", [])
        if not isinstance(references, list) or len(references) > 32:
            references = None
        if references is None:
            security["status"] = "BLOCKED"
            security["reason_code"] = "self_test_diagnostic_manifest_invalid"
            security["final_report_check"] = {
                "status": "BLOCKED", "reason_code": "self_test_diagnostic_manifest_invalid",
            }
            security.setdefault("required_subchecks", {})[
                "canary-absent-from-campaign-and-self-test-reports"
            ] = "BLOCKED"
            report["result"] = _campaign_result(report)
            _atomic_replace_json(report_path, report)
            return report
        for index, reference in enumerate(references):
            if (not isinstance(reference, dict) or reference.get("stage") != "expected_boot_failure"
                    or not isinstance(reference.get("path"), str)
                    or reference["path"] != f"selftest-{self_run_id}-failure.json"):
                security["status"] = "BLOCKED"
                security["reason_code"] = "self_test_diagnostic_reference_invalid"
                security["final_report_check"] = {
                    "status": "BLOCKED", "reason_code": "self_test_diagnostic_reference_invalid",
                }
                security.setdefault("required_subchecks", {})[
                    "canary-absent-from-campaign-and-self-test-reports"
                ] = "BLOCKED"
                report["result"] = _campaign_result(report)
                _atomic_replace_json(report_path, report)
                return report
            final_artifacts.append({"id": f"nested_self_test_diagnostic_{index}",
                                    "path": str(report_path.parent / reference["path"]), "kind": "file"})
    manifest = {"schema_version": 1,
                "required_ids": [item["id"] for item in final_artifacts],
                "artifacts": final_artifacts}
    result = checker.scan_manifest(
        canary, manifest, [report_path.parent], timeout_seconds=_bounded_timeout(60),
    )
    scan_status = result.get("result", "BLOCKED")
    reason = _canary_result_reason(result)
    for _attempt in range(3):
        security["final_report_check"] = {"status": scan_status, "reason_code": reason}
        security.setdefault("required_subchecks", {})[
            "canary-absent-from-campaign-and-self-test-reports"
        ] = scan_status
        if scan_status == "FAIL":
            security["status"] = "FAIL"
            security["reason_code"] = reason or "canary_present"
        elif scan_status == "BLOCKED" and security.get("status") != "FAIL":
            security["status"] = "BLOCKED"
            security["reason_code"] = reason or "final_report_not_inspected"
        report["result"] = _campaign_result(report)
        encoded = (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
        current = report_path.read_bytes()
        if current == encoded:
            return report
        _atomic_replace_json(report_path, report)
        result = checker.scan_manifest(
            canary, manifest, [report_path.parent], timeout_seconds=_bounded_timeout(60),
        )
        scan_status = result.get("result", "BLOCKED")
        reason = _canary_result_reason(result)
    # If report content cannot reach a stable redacted result, fail closed.
    security["status"] = "BLOCKED"
    security["reason_code"] = "final_report_check_did_not_stabilize"
    security["final_report_check"] = {"status": "BLOCKED",
                                      "reason_code": "final_report_check_did_not_stabilize"}
    report["result"] = _campaign_result(report)
    _atomic_replace_json(report_path, report)
    return report


def _mark_stage_running(report: dict[str, Any], stage: str) -> None:
    states = report.get("stages")
    if stage not in STAGE_ORDER or not isinstance(states, dict) or set(states) != set(STAGE_ORDER):
        raise AcceptanceError("workflow stage manifest is invalid")
    index = STAGE_ORDER.index(stage)
    prior_states = [states.get(prior) for prior in STAGE_ORDER[:index]]
    out_of_order = (False if stage == "teardown"
                    else any(value in {"NOT_RUN", "RUNNING"} for value in prior_states))
    if states.get(stage) != "NOT_RUN" or out_of_order:
        raise AcceptanceError("workflow stages must be attempted once and in documented order")
    states[stage] = "RUNNING"


@contextlib.contextmanager
def _campaign_lock(state_root: Path, run_id: str):
    _private_directory(state_root)
    _run_name(run_id)
    lock_path = state_root / ".vm-host-acceptance.lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise AcceptanceError("campaign stage lock is unavailable") from error
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise AcceptanceError("campaign stage lock is unsafe")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise AcceptanceError("another exact-run stage or cleanup is active") from error
        yield lock_path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _resume_campaign(state_root: Path, source: Path, run_id: str,
                     suites: list[str], provider_config_sha256: str,
                     source_identity: dict[str, Any],
                     campaign_scope: dict[str, Any] | None = None) -> tuple[dict[str, Any], Path, Path, Path]:
    run_root = _namespace_path(state_root, _run_name(run_id))
    report_path = _campaign_report_path(state_root, run_id)
    _private_directory(run_root)
    _private_directory(report_path.parent)
    _private_directory(run_root / "state")
    report = _read_private_json(report_path)
    if (report.get("schema_version") != 1 or report.get("workflow_version") != WORKFLOW_VERSION
            or report.get("run_id") != run_id or report.get("run_name") != _run_name(run_id)
            or report.get("suites") != suites
            or report.get("required_gates") != _required_gates(suites)
            or report.get("acceptance_scope") != _acceptance_scope(suites)
            or (campaign_scope is not None and report.get("campaign_scope") != campaign_scope)
            or report.get("provider_config_sha256") != provider_config_sha256):
        raise AcceptanceError("resume manifest revision, suite, provider configuration, or schema changed")
    stored_source = report.get("source")
    if (not isinstance(stored_source, dict)
            or stored_source.get("source_head") != source_identity.get("source_head")
            or stored_source.get("tree_sha256") != source_identity.get("tree_sha256")):
        raise AcceptanceError("source revision changed; resume requires a fresh scoped attempt")
    workspace = run_root / "candidate-snapshot" / "workspace"
    if (not workspace.is_dir() or workspace.is_symlink()
            or _source_identity(workspace).get("tree_sha256") != report.get("snapshot", {}).get("tree_sha256")):
        raise AcceptanceError("immutable campaign snapshot changed; resume was refused")
    child_state = run_root / "state"
    ready_profiles: list[str] = []
    clean = report.get("gates", {}).get("HOST-CLEAN", {})
    if isinstance(clean, dict) and clean.get("status") == "PASS":
        profile = _profile_name(run_id)
        current = _profile_record(child_state, profile)
        if (current.get("name") != profile or current.get("depth") != 0
                or current.get("status") not in {"ready", "suspended"}
                or current.get("profile_id") != clean.get("profile_id")
                or current.get("image_sha256") != clean.get("image_sha256")
                or current.get("guest_payload_sha256") != clean.get("guest_payload_sha256")
                or current.get("runtime_payload_sha256") != clean.get("runtime_payload_sha256")):
            raise AcceptanceError("campaign profile/runtime identity changed; resume was refused")
        if current.get("status") == "ready" and not _listener_owned_loopback(current):
            raise AcceptanceError("ready campaign process/listener ownership changed; resume was refused")
        if current.get("status") == "ready":
            ready_profiles.append(profile)
    provider_profiles = report.get("provider_profiles", {})
    if not isinstance(provider_profiles, dict):
        raise AcceptanceError("provider profile identity manifest is invalid")
    for provider, expected in provider_profiles.items():
        selection = _provider_config_entry(report, provider)
        if (provider not in {"ollama", "llamacpp", "openai"} or selection is None
                or not isinstance(expected, dict)):
            raise AcceptanceError("provider profile selection is outside the exact campaign configuration")
        name = _profile_name(run_id, f"provider-{provider}")
        if expected.get("profile_name") != name or expected.get("model_id") != selection.get("model_id"):
            raise AcceptanceError("provider profile model identity changed; resume was refused")
        current = _profile_record(run_root / "state", name)
        if (current.get("name") != name or current.get("depth") != 0
                or current.get("status") not in {"ready", "suspended"}
                or current.get("profile_id") != expected.get("profile_id")
                or current.get("image_sha256") != expected.get("image_sha256")
                or current.get("guest_payload_sha256") != expected.get("guest_payload_sha256")
                or current.get("runtime_payload_sha256") != expected.get("runtime_payload_sha256")):
            raise AcceptanceError("provider profile/runtime identity changed; resume was refused")
        if current.get("status") == "ready":
            if (not _listener_owned_loopback(current)
                    or not _model_selection_matches(run_root / "state", name, selection)):
                raise AcceptanceError("provider process or selected model ownership changed; resume was refused")
            ready_profiles.append(name)
    if len(ready_profiles) > 1:
        raise AcceptanceError("multiple campaign guests are active; exact-run resume was refused")
    return report, run_root, child_state, workspace


def _safe_status(result: subprocess.CompletedProcess) -> str:
    if result.returncode == 0:
        return "PASS"
    if result.returncode == 2:
        return "BLOCKED"
    return "FAIL"


@_bounded_stage
def execute_campaign(source: Path, state_root: Path, run_id: str, suites: list[str],
                     memory_gib: int, confirm_run_id: str, *,
                     apply_selected_updates: bool = False,
                     provider_config_path: str | os.PathLike[str] | None = None,
                     campaign_suites: list[str] | None = None,
                     workflow_stage: str | None = None,
                     resume: bool = False,
                     approved_total_minutes: int | None = None,
                     retry_of: str | None = None,
                     retry_reason: str | None = None) -> tuple[dict[str, Any], Path]:
    if confirm_run_id != run_id:
        raise AcceptanceError("execution requires the exact plan run ID")
    selected_suites = list(suites)
    full_suites = list(campaign_suites if campaign_suites is not None else suites)
    if any(suite not in ALL_SUITES for suite in selected_suites + full_suites):
        raise AcceptanceError("execution suite is outside the fixed campaign contract")
    if workflow_stage is not None and workflow_stage not in STAGE_ORDER:
        raise AcceptanceError("workflow stage is unsupported")
    if workflow_stage == STAGE_ORDER[0] and resume:
        raise AcceptanceError("the clean-usability stage cannot resume an existing campaign")
    if workflow_stage not in {None, STAGE_ORDER[0]} and not resume:
        raise AcceptanceError("later campaign stages require an exact-run resume")
    approved_seconds = (_approved_budget_seconds(approved_total_minutes)
                        if workflow_stage is not None or approved_total_minutes is not None else None)
    if workflow_stage is None and approved_seconds is None:
        raise AcceptanceError("live campaign execution requires an explicit total time budget")
    if workflow_stage is None and (retry_of is not None or retry_reason is not None):
        raise AcceptanceError("fresh retry authorization is valid only for a staged campaign start")
    if resume and (retry_of is not None or retry_reason is not None):
        raise AcceptanceError("retry authorization cannot be changed while resuming an existing run")
    if apply_selected_updates and "lifecycle" not in full_suites:
        raise AcceptanceError("selected update apply is valid only with the lifecycle suite")
    initial = preflight(source, state_root, run_id, memory_gib, full_suites,
                        provider_config_path, resume=resume)
    if initial["result"] != "PASS":
        return {"run_id": run_id, "result": "BLOCKED", "preflight": initial, "gates": {}}, Path()
    if not any(suite != "preflight" for suite in full_suites):
        return {"run_id": run_id, "result": "PASS", "preflight": initial,
                "gates": {"HOST-PREFLIGHT": {"status": "PASS", "checks": initial["checks"]}}}, Path()
    provider_config_sha256 = initial.get("provider_config", {}).get("sha256", "")
    if approved_seconds is None:
        raise AcceptanceError("campaign scope requires its approved total time budget")
    campaign_scope = _campaign_scope(
        full_suites, memory_gib, provider_config_sha256, apply_selected_updates, approved_seconds,
    )
    attempt_info: dict[str, Any] | None = None
    if resume:
        report, run_root, child_state, workspace = _resume_campaign(
            state_root, source, run_id, full_suites, provider_config_sha256, initial["source"],
            campaign_scope,
        )
        if report.get("approved_total_budget_seconds") != approved_seconds:
            raise AcceptanceError("approved total campaign budget changed; resume was refused")
        elapsed_ms = _campaign_elapsed_ms(report)
        attempt = report.get("attempt", {})
        prior_elapsed_ms = attempt.get("prior_elapsed_ms", 0) if isinstance(attempt, dict) else None
        if type(prior_elapsed_ms) is not int or prior_elapsed_ms < 0:
            raise AcceptanceError("prior-attempt time accounting is invalid")
        remaining_seconds = _remaining_campaign_seconds(
            approved_seconds, prior_elapsed_ms, elapsed_ms, stage=workflow_stage,
        )
        _tighten_stage_budget(remaining_seconds)
        report["resume_preflight"] = initial
        if workflow_stage is not None:
            if (workflow_stage == "nested-boundaries-agents"
                    and report.get("gates", {}).get("HUMAN-TUI", {}).get("status") != "PASS"):
                raise AcceptanceError("owner must verify and record a usable TUI before recursion")
            _mark_stage_running(report, workflow_stage)
    else:
        if approved_seconds is not None:
            initial["approved_total_budget_seconds"] = approved_seconds
            _remaining_campaign_seconds(approved_seconds, 0, 0, stage=workflow_stage)
        if workflow_stage is not None:
            attempt_info = _register_campaign_attempt(
                state_root, run_id, initial["source"], provider_config_sha256,
                retry_of=retry_of, retry_reason=retry_reason,
                approved_total_budget_seconds=approved_seconds,
                campaign_scope=campaign_scope,
            )
        if approved_seconds is not None:
            prior_elapsed_ms = attempt_info.get("prior_elapsed_ms", 0) if attempt_info else 0
            remaining_seconds = _remaining_campaign_seconds(
                approved_seconds, prior_elapsed_ms, 0, stage=workflow_stage,
            )
            _tighten_stage_budget(remaining_seconds)
        run_root, child_state, workspace = _new_campaign(
            state_root, run_id, source, full_suites, initial, campaign_scope,
        )
        report = _read_private_json(_campaign_report_path(state_root, run_id))
        if attempt_info is not None:
            report["attempt"] = attempt_info
            _atomic_replace_json(_campaign_report_path(state_root, run_id), report)
        if workflow_stage is not None:
            if workflow_stage != STAGE_ORDER[0]:
                raise AcceptanceError("a new campaign must begin with clean-usability")
            _mark_stage_running(report, workflow_stage)
        report["preflight"] = initial
    report_path = _campaign_report_path(state_root, run_id)
    if "provider_config" in initial:
        report["provider_config"] = initial["provider_config"]
    requested_effects = {
        "selected_guest_update_targets": ["config", "tools"] if apply_selected_updates else [],
        "selected_guest_updates_authorized": apply_selected_updates,
    }
    if resume and report.get("requested_effects") != requested_effects:
        raise AcceptanceError("selected campaign effects changed; resume requires a fresh attempt")
    report["requested_effects"] = requested_effects
    report["execution_context"] = initial["execution_context"]
    if not resume:
        report["source"]["workspace_tree_sha256"] = _source_identity(workspace)["tree_sha256"]
    stage_suites = selected_suites
    selected_gates = {gate for suite in stage_suites for gate in SUITE_GATES[suite]}
    for gate in selected_gates:
        if report["gates"].get(gate, {}).get("status") == "NOT_RUN":
            report["gates"][gate] = {"status": "BLOCKED", "reason_code": "not_run"}

    active_gate: str | None = None
    active_gate_started_at: float | None = None

    def begin_gate(gate: str) -> None:
        nonlocal active_gate, active_gate_started_at
        if gate in selected_gates:
            active_gate = gate
            active_gate_started_at = time.monotonic()
            report["active_gate"] = gate
            _atomic_replace_json(report_path, report)

    def set_gate(gate: str, status: str, **evidence: Any) -> None:
        nonlocal active_gate, active_gate_started_at
        if gate not in report.get("required_gates", []):
            return
        if status not in {"PASS", "FAIL", "BLOCKED", "NOT_RUN"}:
            raise AcceptanceError("gate result is outside the fixed status enum")
        if active_gate == gate and active_gate_started_at is not None:
            evidence.setdefault("elapsed_ms", max(0, int((time.monotonic() - active_gate_started_at) * 1000)))
        if active_gate == gate:
            active_gate = None
            active_gate_started_at = None
            report.pop("active_gate", None)
        report["gates"][gate] = {"status": status, **evidence}
        _atomic_replace_json(report_path, report)

    if report["gates"].get("HOST-PREFLIGHT", {}).get("status") in {"NOT_RUN", "BLOCKED"}:
        set_gate("HOST-PREFLIGHT", "PASS", checks=initial["checks"], memory=initial["memory"])
    profile = _profile_name(run_id)
    guest_ready = False
    outer_boundary_status: str | None = None
    outer_boundary_evidence: dict[str, Any] | None = None
    nested_boundary_evidence: dict[str, Any] = {}
    workload_setup: dict[str, Any] | None = None
    workload_retention: dict[str, Any] | None = None
    workload_cleanup: dict[str, bool] | None = None

    try:
        profile_needed = any(suite != "preflight" for suite in stage_suites)
        if profile_needed:
            if not any(item.get("profile_name") == profile for item in report.get("owned_profiles", [])
                       if isinstance(item, dict)):
                report.setdefault("owned_profiles", []).append(
                    {"profile_name": profile, "depth": 0, "run_id": run_id},
                )
                _atomic_replace_json(report_path, report)
            clean_gate = report["gates"].get("HOST-CLEAN", {})
            if clean_gate.get("status") == "PASS":
                data = _profile_record(child_state, profile)
                if data.get("status") == "suspended":
                    if not _suspend_other_campaign_profiles(child_state, run_id, report,
                                                            profile, memory_gib):
                        raise AcceptanceError("other exact campaign profiles could not be suspended")
                    resumed = _lifecycle_call(child_state, profile, Path(data["workspace"]),
                                              "resume", memory_gib, timeout=1900)
                    data = _profile_record(child_state, profile)
                    if resumed.returncode != 0 or data.get("status") != "ready":
                        raise AcceptanceError("exact campaign profile could not be resumed safely")
                guest_ready = data.get("status") == "ready" and _listener_owned_loopback(data)
                if not guest_ready:
                    raise AcceptanceError("exact campaign guest process ownership is not verified")
            elif "clean" in stage_suites:
                begin_gate("HOST-CLEAN")
                completed = _lifecycle_call(child_state, profile, workspace, "up", memory_gib, timeout=1900)
                if completed.returncode == 0:
                    data = _profile_record(child_state, profile)
                    lifecycle_ready = data.get("status") == "ready" and data.get("runtime_identity") is not None
                    clean = None
                    clean_error = None
                    image_integrity = _cached_image_integrity(child_state, profile)
                    if lifecycle_ready:
                        try:
                            clean = _guest_clean_checks(child_state, profile, workspace, os.getuid(), os.getgid())
                        except TimeoutError:
                            clean_error = "guest_clean_probe_timeout"
                        except AcceptanceError:
                            clean_error = "guest_clean_probe_failed"
                    clean_status = "PASS" if (
                        lifecycle_ready and clean is not None and clean.get("result") == "PASS"
                        and image_integrity["status"] == "PASS"
                    ) else "FAIL" if (
                        (clean is not None and clean.get("result") == "FAIL")
                        or image_integrity["status"] == "FAIL"
                    ) else "BLOCKED"
                    guest_ready = clean_status == "PASS"
                    set_gate("HOST-CLEAN", clean_status,
                             reason_code=clean_error or image_integrity.get("reason_code"),
                             profile_name=profile, readiness_verified=lifecycle_ready,
                             profile_id=data.get("profile_id"), depth=data.get("depth"),
                             checks=clean.get("checks") if clean else None,
                             host_docker_dependency=clean.get("host_docker_dependency") if clean else "NOT_REQUIRED",
                             image_integrity=image_integrity["status"],
                             image_artifact_sha256=image_integrity.get("artifact_sha256"),
                             image_sha256=data.get("image_sha256"),
                             guest_payload_sha256=data.get("guest_payload_sha256"),
                             runtime_payload_sha256=data.get("runtime_payload_sha256"),
                             opencode_version=data.get("opencode_version"),
                             guest_opencode_version=clean.get("opencode_version") if clean else None)
                    if clean_status == "PASS":
                        report["verified_guest_opencode_version"] = clean.get("opencode_version")
                        _atomic_replace_json(report_path, report)
                else:
                    set_gate("HOST-CLEAN", _safe_status(completed), reason_code="fresh_guest_start_failed",
                             cause_exit_code=completed.returncode)

        if "daily" in suites:
            if guest_ready:
                begin_gate("E2E-DAILY")
                try:
                    daily = _daily_lifecycle_checks(child_state, profile, workspace, run_root, memory_gib)
                except TimeoutError:
                    daily = {"status": "FAIL", "reason_code": "daily_deadline_exceeded"}
                except (AcceptanceError, OSError, RuntimeError, ValueError):
                    daily = {"status": "BLOCKED", "reason_code": "daily_environment_unavailable"}
                daily_status = daily.pop("status")
                if daily_status == "FAIL":
                    set_gate("E2E-DAILY", "FAIL", **daily)
                elif daily_status == "BLOCKED":
                    set_gate("E2E-DAILY", "BLOCKED", **daily)
                else:
                    set_gate(
                        "E2E-DAILY", "BLOCKED",
                        reason_code=("docker_rollback_smoke_required" if daily.get("docker_rollback_route_staged")
                                     else "rollback_route_not_staged"),
                        automated_status="PASS", **daily,
                    )
            else:
                set_gate("E2E-DAILY", "BLOCKED", reason_code="outer_guest_not_verified_ready")

        if "boundary" in suites:
            if guest_ready:
                begin_gate("SEC-NET")
                outer_boundary = _guest_isolation(child_state, profile, workspace, run_id)
                network_status = outer_boundary.pop("status")
                outer_boundary_status = network_status
                outer_boundary_evidence = outer_boundary
                # A depth-1 profile started by the host is a sibling, not an
                # enclosing guest's child. Keep the measured outer evidence but
                # refuse SEC-NET PASS until the live self-test proves actual
                # G0→G1→G2 placement and obtains a G2 isolation report.
                set_gate("SEC-NET", "FAIL" if network_status == "FAIL" else "BLOCKED",
                         reason_code="actual_nested_child_boundary_evidence_required",
                         outer=outer_boundary, nested_child_status="NOT_RUN")
            else:
                set_gate("SEC-NET", "BLOCKED", reason_code="outer_guest_not_verified_ready")
            set_gate(
                "SEC-FS", "BLOCKED", reason_code="required_filesystem_evidence_unavailable",
                required_subchecks={
                    "host_file_unreadable": "BLOCKED", "control_key_and_socket_unreadable": "BLOCKED",
                    "child_out_of_scope_files_unreadable": "BLOCKED", "escaping_symlinks_refused": "BLOCKED",
                    "state_root_overlap_blocked": "BLOCKED",
                },
                deferred_checks=[
                    {"id": item, "status": "DEFERRED", "reason_code": "owner_deferred_live_openai"}
                    for item in DEFERRED_CHECKS["SEC-FS"]
                ],
            )

        if "workload" in suites:
            if guest_ready:
                begin_gate("E2E-WORK")
                workload_setup = _guest_workload_checks(child_state, profile, run_id)
            else:
                workload_setup = {"status": "BLOCKED", "reason_code": "outer_guest_not_verified_ready"}

        if guest_ready and "lifecycle" in suites:
            begin_gate("E2E-LIFE")
            life_workspace = Path(_profile_record(child_state, profile)["workspace"])
            lifecycle_workload_evidence: dict[str, Any] = {}
            try:
                life = _lifecycle_retention_checks(
                    child_state, profile, life_workspace, memory_gib, apply_selected_updates,
                    run_id if workload_setup and workload_setup.get("status") == "PASS" else None,
                    lifecycle_workload_evidence,
                )
            except TimeoutError:
                life = {"status": "FAIL", "reason_code": "lifecycle_deadline_exceeded"}
            except (AcceptanceError, OSError, RuntimeError, ValueError):
                life = {"status": "BLOCKED", "reason_code": "lifecycle_environment_unavailable"}
            workload_retention = lifecycle_workload_evidence or life.get("workload_retention")
            life_status = life.pop("status")
            if life_status == "FAIL":
                set_gate("E2E-LIFE", "FAIL", **life)
            elif life_status == "BLOCKED":
                set_gate("E2E-LIFE", "BLOCKED", **life)
            else:
                set_gate("E2E-LIFE", "BLOCKED", reason_code="full_lifecycle_suite_incomplete",
                         automated_status="PASS", **life)
            lifecycle_version = report["gates"].get("E2E-LIFE", {}).get("opencode_version")
            if isinstance(lifecycle_version, str):
                report["verified_guest_opencode_version"] = lifecycle_version
                _atomic_replace_json(report_path, report)
        elif "lifecycle" in suites:
            set_gate("E2E-LIFE", "BLOCKED", reason_code="outer_guest_not_verified_ready")

        if "workload" in suites:
            if (workload_setup and workload_setup.get("status") == "PASS"
                    and "lifecycle" not in suites and guest_ready):
                workload_retention = _workload_suspend_resume_retention(
                    child_state, profile, Path(_profile_record(child_state, profile)["workspace"]),
                    memory_gib, run_id,
                )
            if guest_ready:
                try:
                    workload_cleanup = _guest_workload_cleanup(child_state, profile, run_id)
                except (AcceptanceError, OSError, RuntimeError, TimeoutError, ValueError):
                    workload_cleanup = None
            result = _workload_gate_result(workload_setup, workload_retention, workload_cleanup)
            gate_status = result.pop("status")
            set_gate("E2E-WORK", gate_status, **result)

        if "self" in suites:
            if guest_ready:
                try:
                    begin_gate("E2E-SELF")
                    self_evidence = _self_test_gate(child_state, profile, workspace, report_path.parent)
                    nested_boundary_evidence = {
                        key: self_evidence.get(key) for key in (
                            "g1_boundary_status", "g2_boundary_verified", "g2_boundary_report_sha256",
                        )
                    }
                    self_status = self_evidence.pop("status")
                    depth_status = self_evidence.pop("depth_gate_status", "NOT_RUN")
                    set_gate("E2E-SELF", self_status, **self_evidence)
                    set_gate("E2E-DEPTH", depth_status, source="live self-test depth stage")
                except (AcceptanceError, OSError, RuntimeError, TimeoutError, ValueError):
                    set_gate("E2E-SELF", "BLOCKED", reason_code="live_self_test_unavailable")
                    set_gate("E2E-DEPTH", "BLOCKED", reason_code="live_self_test_unavailable")
            else:
                set_gate("E2E-SELF", "BLOCKED", reason_code="outer_guest_not_verified_ready")
                set_gate("E2E-DEPTH", "BLOCKED", reason_code="outer_guest_not_verified_ready")

        if "boundary" in suites and "self" in suites:
            g1_status = nested_boundary_evidence.get("g1_boundary_status")
            g2_verified = nested_boundary_evidence.get("g2_boundary_verified") is True
            g2_digest = nested_boundary_evidence.get("g2_boundary_report_sha256")
            complete = (
                outer_boundary_status == "PASS" and g1_status == "PASS" and g2_verified
                and isinstance(g2_digest, str) and re.fullmatch(r"[a-f0-9]{64}", g2_digest)
            )
            set_gate("SEC-NET", "PASS" if complete else "FAIL" if "FAIL" in {
                outer_boundary_status, g1_status,
            } else "BLOCKED",
                reason_code=None if complete else "actual_nested_child_boundary_evidence_incomplete",
                outer=outer_boundary_evidence, g1_status=g1_status,
                g2_verified=g2_verified, g2_boundary_report_sha256=g2_digest,
                topology="campaign-depth-0→self-test-depth-1→self-test-depth-2")

        if "model" in suites:
            if guest_ready:
                begin_gate("E2E-MODEL")
                try:
                    model_evidence = _provider_stream_checks(
                        child_state, run_id, workspace, report, memory_gib,
                    )
                except TimeoutError:
                    model_evidence = {"status": "BLOCKED", "automated_status": "BLOCKED",
                                      "reason_code": "provider_stage_deadline_exceeded", "providers": []}
                except (AcceptanceError, OSError, RuntimeError, ValueError):
                    model_evidence = {"status": "BLOCKED", "automated_status": "BLOCKED",
                                      "reason_code": "provider_environment_unavailable", "providers": []}
                provider_statuses = {item.get("status") for item in model_evidence.get("providers", [])}
                if "FAIL" in provider_statuses:
                    set_gate("E2E-MODEL", "FAIL", reason_code="provider_stream_schema_invalid",
                             automated_status="FAIL", providers=model_evidence.get("providers", []))
                elif model_evidence.get("reason_code") not in {
                        None, "provider_or_owner_evidence_incomplete"}:
                    set_gate("E2E-MODEL", "BLOCKED", reason_code=model_evidence["reason_code"],
                             automated_status=model_evidence.get("automated_status"),
                             providers=model_evidence.get("providers", []))
                elif any(item.get("status") == "BLOCKED" for item in model_evidence.get("providers", [])):
                    blocked_reason = next(item.get("reason_code") for item in model_evidence["providers"]
                                          if item.get("status") == "BLOCKED")
                    set_gate("E2E-MODEL", "BLOCKED", reason_code=blocked_reason or "provider_stream_incomplete",
                             automated_status="BLOCKED", providers=model_evidence["providers"])
                else:
                    set_gate("E2E-MODEL", "BLOCKED", reason_code="owner_local_model_observation_required",
                             automated_status=model_evidence.get("automated_status", "BLOCKED"),
                             automated_reason_code=model_evidence.get("reason_code"),
                             providers=model_evidence.get("providers", []))
            else:
                set_gate("E2E-MODEL", "BLOCKED", reason_code="outer_guest_not_verified_ready")

        # Owner checkpoints are explicit, version-bound attestations. No health
        # check or default/Enter response is promoted into a behavioral PASS.
        for suite, gates in {
            "agents": ("E2E-AGENTS",),
            "human": ("HUMAN-TUI", "HUMAN-PERM", "HUMAN-AUTH"),
        }.items():
            if suite in suites:
                for gate in gates:
                    reason = "owner_observation_required" if gate.startswith("HUMAN-") else "owner_live_evidence_required"
                    set_gate(gate, "BLOCKED", reason_code=reason)

        if "boundary" in suites:
            canary_scan = _run_canary_scan(state_root, run_id, report, report_path)
            canary_status = canary_scan.get("result")
            if canary_status not in {"PASS", "FAIL", "BLOCKED"}:
                canary_status = "BLOCKED"
            snapshot_status = _canary_check_status(
                canary_scan, exact_ids={"snapshot_workspace", "snapshot_git_history"},
            )
            seed_image_log_status = _canary_check_status(
                canary_scan, prefixes=(
                    "seed_user_data_", "seed_iso_", "campaign_logs_", "profile_overlay_",
                    "base_image_", "base_manifest_", "nested_transient_images",
                ),
            )
            set_gate(
                "SEC-SECRET", ("FAIL" if canary_status == "FAIL" else "BLOCKED"),
                reason_code=(_canary_result_reason(canary_scan)
                             or "required_private_state_subchecks_not_run"),
                canary_check=canary_scan,
                required_subchecks={
                    "canary-absent-from-snapshot-and-history": snapshot_status,
                    "canary-absent-from-seed-base-and-logs": seed_image_log_status,
                    "canary-absent-from-campaign-and-self-test-reports": "BLOCKED",
                    "provider-forwarding-opt-in-only": "BLOCKED",
                    "synthetic-private-state-persistence-and-atomic-export-import": "BLOCKED",
                    "synthetic-export-failure-prevents-delete": "BLOCKED",
                    "synthetic-refreshed-state-survives-restart-and-import": "BLOCKED",
                },
                deferred_checks=[
                    {"id": item, "status": "DEFERRED", "reason_code": "owner_deferred_live_openai"}
                    for item in DEFERRED_CHECKS["SEC-SECRET"]
                ],
            )

        if ("HUMAN-TUI" in report.get("required_gates", [])
                and report.get("gates", {}).get("HUMAN-TUI", {}).get("status") == "NOT_RUN"):
            set_gate("HUMAN-TUI", "BLOCKED", reason_code="owner_observation_required")

        report["result"] = _campaign_result(report)
        if workflow_stage is not None:
            required_for_stage = selected_gates.intersection(report.get("required_gates", []))
            stage_results = [report["gates"].get(gate, {}).get("status", "NOT_RUN")
                             for gate in required_for_stage]
            report["stages"][workflow_stage] = (
                "FAIL" if "FAIL" in stage_results else
                "PASS" if stage_results and all(value == "PASS" for value in stage_results) else
                "BLOCKED"
            )
            report["stage_elapsed_ms"][workflow_stage] = _stage_elapsed_ms()
        else:
            report["legacy_elapsed_ms"] = _stage_elapsed_ms()
        report["finished_utc"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        _atomic_replace_json(report_path, report)
    except KeyboardInterrupt:
        if active_gate is not None:
            elapsed = (max(0, int((time.monotonic() - active_gate_started_at) * 1000))
                       if active_gate_started_at is not None else 0)
            report["gates"][active_gate] = {
                "status": "BLOCKED", "reason_code": "interrupted", "elapsed_ms": elapsed,
            }
            report.pop("active_gate", None)
        report["result"] = "BLOCKED"
        if workflow_stage is not None and report.get("stages", {}).get(workflow_stage) == "RUNNING":
            report["stages"][workflow_stage] = "BLOCKED"
        if workflow_stage is not None:
            report["stage_elapsed_ms"][workflow_stage] = _stage_elapsed_ms()
        else:
            report["legacy_elapsed_ms"] = _stage_elapsed_ms()
        report["interrupted"] = True
        report["finished_utc"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        _atomic_replace_json(report_path, report)
        raise
    except TimeoutError:
        if active_gate is not None:
            elapsed = (max(0, int((time.monotonic() - active_gate_started_at) * 1000))
                       if active_gate_started_at is not None else 0)
            report["gates"][active_gate] = {
                "status": "FAIL", "reason_code": "deadline_exceeded", "elapsed_ms": elapsed,
            }
            report.pop("active_gate", None)
        report["result"] = "FAIL"
        if workflow_stage is not None and report.get("stages", {}).get(workflow_stage) == "RUNNING":
            report["stages"][workflow_stage] = "FAIL"
        if workflow_stage is not None:
            report["stage_elapsed_ms"][workflow_stage] = min(
                STAGE_BUDGET_SECONDS * 1000, _stage_elapsed_ms(),
            )
        else:
            report["legacy_elapsed_ms"] = min(STAGE_BUDGET_SECONDS * 1000, _stage_elapsed_ms())
        report["reason_code"] = "deadline_exceeded"
        report["finished_utc"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        _atomic_replace_json(report_path, report)
    except (AcceptanceError, OSError, RuntimeError, ValueError):
        if active_gate is not None:
            elapsed = (max(0, int((time.monotonic() - active_gate_started_at) * 1000))
                       if active_gate_started_at is not None else 0)
            report["gates"][active_gate] = {
                "status": "BLOCKED", "reason_code": "environment_unavailable", "elapsed_ms": elapsed,
            }
            report.pop("active_gate", None)
        report["result"] = "BLOCKED"
        if workflow_stage is not None and report.get("stages", {}).get(workflow_stage) == "RUNNING":
            report["stages"][workflow_stage] = "BLOCKED"
        if workflow_stage is not None:
            report["stage_elapsed_ms"][workflow_stage] = min(
                STAGE_BUDGET_SECONDS * 1000, _stage_elapsed_ms(),
            )
        else:
            report["legacy_elapsed_ms"] = min(STAGE_BUDGET_SECONDS * 1000, _stage_elapsed_ms())
        report["reason_code"] = "environment_unavailable"
        report["finished_utc"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        _atomic_replace_json(report_path, report)
    return report, report_path


def _atomic_replace_json(path: Path, data: dict[str, Any]) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
        raise AcceptanceError("acceptance report is not a private owned regular file")
    encoded = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    if len(encoded) > MAX_REPORT_BYTES:
        raise AcceptanceError("acceptance report exceeds the bounded size")
    fd, temporary_name = tempfile.mkstemp(prefix=".acceptance-update-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def cleanup_campaign(state_root: Path, run_id: str, confirm_run_id: str, *,
                     staged_teardown: bool = False) -> dict[str, Any]:
    if run_id != confirm_run_id:
        raise AcceptanceError("cleanup requires the exact campaign run ID")
    run_root = _namespace_path(state_root, _run_name(run_id))
    _private_directory(run_root)
    report_path = run_root.parent / "reports" / f"{_run_name(run_id)}.json"
    _private_directory(report_path.parent)
    report = _read_private_json(report_path)
    if report.get("run_id") != run_id or report.get("run_name") != _run_name(run_id):
        raise AcceptanceError("campaign manifest ownership does not match the requested run")
    if (report.get("workflow_version") != WORKFLOW_VERSION
            or not isinstance(report.get("suites"), list)
            or report.get("required_gates") != _required_gates(report["suites"])
            or report.get("acceptance_scope") != _acceptance_scope(report["suites"])):
        raise AcceptanceError("campaign workflow scope is invalid for exact cleanup")
    canary_expected: bytes | None = None
    canary_marker = report.get("synthetic_canary")
    if (not isinstance(canary_marker, dict)
            or canary_marker.get("status") not in {"PENDING", "GENERATING", "GENERATED", "REMOVED"}):
        raise AcceptanceError("campaign canary ownership marker is missing or invalid")
    canary_state = canary_marker["status"]
    canary_cleanup_required = canary_state in {"PENDING", "GENERATING", "GENERATED", "REMOVED"}
    if canary_cleanup_required:
        try:
            if canary_state in {"PENDING", "GENERATING"}:
                canary_expected = None
            elif canary_state == "GENERATED":
                checker = _load_module("vm_host_acceptance_canary_cleanup", CANARY_CHECK_PATH)
                canary_expected = checker.read_canary(_canary_path(state_root, run_id))
        except (AcceptanceError, OSError, RuntimeError, ValueError):
            canary_expected = None
    suites = report.get("suites")
    gates = report.get("gates")
    if (not isinstance(suites, list) or not suites or any(item not in ALL_SUITES for item in suites)
            or len(set(suites)) != len(suites) or not isinstance(gates, dict)
            or any(not isinstance(item, dict) or item.get("status") not in
                   {"PASS", "FAIL", "BLOCKED", "NOT_RUN", "DEFERRED"} for item in gates.values())):
        raise AcceptanceError("campaign suite or gate manifest is invalid")
    owned_profiles = report.get("owned_profiles", [])
    if not isinstance(owned_profiles, list):
        raise AcceptanceError("owned profile manifest is invalid")
    checked_profiles: list[dict[str, Any]] = []
    seen_profiles: set[str] = set()
    role_depth = {"outer": 0, "agent": 1, "workload": 1,
                  "provider-ollama": 0, "provider-llamacpp": 0, "provider-openai": 0}
    for item in owned_profiles:
        if not isinstance(item, dict) or set(item) != {"profile_name", "depth", "run_id"}:
            raise AcceptanceError("owned profile manifest is invalid")
        name, depth, owner = item["profile_name"], item["depth"], item["run_id"]
        suffix = next((role for role in role_depth if name == _profile_name(run_id, role)), None) \
            if isinstance(name, str) else None
        if (owner != run_id or suffix is None or type(depth) is not int
                or role_depth[suffix] != depth or name in seen_profiles):
            raise AcceptanceError("cleanup target is outside this exact campaign run")
        seen_profiles.add(name)
        checked_profiles.append(item)
    prior_cleanup = report.get("cleanup", {})
    prior_deleted = prior_cleanup.get("deleted_profiles", []) if isinstance(prior_cleanup, dict) else []
    if (not isinstance(prior_deleted, list) or len(prior_deleted) != len(set(prior_deleted))
            or any(name not in seen_profiles for name in prior_deleted)):
        raise AcceptanceError("partial cleanup manifest is invalid")
    deleted: list[str] = list(prior_deleted)
    child_state = run_root / "state"
    profile_root = child_state / "vm/profiles"
    if profile_root.exists():
        _private_directory(profile_root)
        observed_profiles = {item.name for item in profile_root.iterdir()}
        if not observed_profiles <= seen_profiles or not (seen_profiles - observed_profiles) <= set(deleted):
            raise AcceptanceError("campaign profile directory contains unmanifested or missing ownership")
    self_gate = gates.get("E2E-SELF", {})
    self_run_id = self_gate.get("run_id") if isinstance(self_gate, dict) else None
    if self_run_id is not None and ("self" not in suites or not isinstance(self_run_id, str)
                                    or not RUN_ID_RE.fullmatch(self_run_id)):
        raise AcceptanceError("self-test cleanup reference is outside the selected campaign scope")
    if isinstance(self_run_id, str) and RUN_ID_RE.fullmatch(self_run_id):
        self_test = _load_module("vm_host_acceptance_self_test_cleanup", ROOT / "scripts/vm-self-test.py")
        self_test.cleanup_live_self_test(str(child_state), self_run_id, self_run_id)
        self_report_path = child_state / "vm/self-test-reports" / f"{self_run_id}.json"
        self_archive_ref = self_gate.get("report_ref")
        if isinstance(self_archive_ref, str) and re.fullmatch(r"selftest-[a-f0-9]{32}\.json", self_archive_ref):
            self_archive = report_path.parent / self_archive_ref
            if self_archive.exists() and self_report_path.is_file():
                nested_report = _read_private_json(self_report_path)
                _atomic_replace_json(self_archive, nested_report)
        self_gate["cleanup_status"] = "PASS"
        _atomic_replace_json(report_path, report)
    for item in sorted(checked_profiles, key=lambda value: (-value["depth"], value["profile_name"])):
        name = item["profile_name"]
        profile_path = child_state / "vm/profiles" / name
        if not profile_path.exists():
            if name in deleted:
                continue
            raise AcceptanceError("manifested campaign profile is missing without prior cleanup evidence")
        data = _profile_record(child_state, name)
        if data.get("status") in {"starting", "provisioning", "unknown", "deleting"}:
            raise AcceptanceError("ambiguous profile state is preserved for owner review")
        if data.get("status") == "ready":
            suspended = _lifecycle_call(
                child_state, name, Path(data["workspace"]), "suspend",
                int(data["memory_gib"]), timeout=90,
            )
            if suspended.returncode != 0:
                raise AcceptanceError("graceful exact-profile shutdown did not confirm; state was preserved")
            data = _profile_record(child_state, name)
        if data.get("status") not in {"allocated", "failed", "suspended"}:
            raise AcceptanceError("profile did not reach an exact deletable stopped state")
        result = _lifecycle_call(child_state, name, Path(data["workspace"]), "delete",
                                 int(data["memory_gib"]), confirm=name, timeout=180)
        if result.returncode != 0:
            raise AcceptanceError("supported lifecycle cleanup did not confirm exact profile deletion")
        if profile_path.exists() or profile_path.is_symlink():
            raise AcceptanceError("exact profile residue remained after lifecycle cleanup")
        deleted.append(name)
        report["cleanup"] = {"status": "BLOCKED", "deleted_profiles": deleted}
        _atomic_replace_json(report_path, report)
    report["cleanup"] = {"status": "RUNNING", "deleted_profiles": deleted}
    report["result"] = "BLOCKED"
    report["finished_utc"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    _atomic_replace_json(report_path, report)
    paths = _load_module("vm_host_acceptance_paths_cleanup", PATHS_PATH)
    paths.recursion_run_delete(state_root, _run_name(run_id), _run_name(run_id), Path("/proc"))
    if run_root.exists() or run_root.is_symlink() or not report_path.is_file() or report_path.is_symlink():
        raise AcceptanceError("campaign teardown residue or report preservation check failed")
    report = _read_private_json(report_path)
    report["cleanup"] = {
        "status": "PASS", "deleted_profiles": deleted,
        "profile_residue": "NONE", "protected_run_report_preserved": True,
        "host_docker_session_touched": False,
    }
    if staged_teardown:
        report["stages"]["teardown"] = "PASS"
        report["stage_elapsed_ms"]["teardown"] = min(
            STAGE_BUDGET_SECONDS * 1000, _stage_elapsed_ms(),
        )
    report["result"] = _campaign_result(report)
    report["finished_utc"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    _atomic_replace_json(report_path, report)
    if canary_cleanup_required:
        if not _remove_canary_fixture(state_root, run_id, canary_expected):
            report["cleanup"]["status"] = "BLOCKED"
            report["cleanup"]["reason_code"] = "canary_fixture_cleanup_unconfirmed"
            if staged_teardown:
                report["stages"]["teardown"] = "BLOCKED"
            report["result"] = _campaign_result(report)
            _atomic_replace_json(report_path, report)
            raise AcceptanceError("private synthetic canary fixture cleanup was not confirmed")
        report["synthetic_canary"]["status"] = "REMOVED"
        report["cleanup"]["synthetic_canary_removed"] = True
        report["result"] = _campaign_result(report)
        _atomic_replace_json(report_path, report)
        report = _finalize_canary_report_check(report_path, report, canary_expected)
    if run_root.exists() or run_root.is_symlink() or not report_path.is_file() or report_path.is_symlink():
        raise AcceptanceError("campaign teardown residue or report preservation check failed")
    return {"run_id": run_id, "cleanup": "PASS", "deleted_profiles": deleted,
            "report_path": str(report_path)}


@_bounded_stage
def execute_teardown_stage(state_root: Path, run_id: str, confirm_run_id: str,
                           approved_total_minutes: int) -> tuple[dict[str, Any], Path]:
    if run_id != confirm_run_id:
        raise AcceptanceError("teardown requires the exact campaign run ID")
    approved_seconds = _approved_budget_seconds(approved_total_minutes)
    report_path = _campaign_report_path(state_root, run_id)
    _private_directory(report_path.parent)
    report = _read_private_json(report_path)
    if (report.get("workflow_version") != WORKFLOW_VERSION or report.get("run_id") != run_id
            or report.get("approved_total_budget_seconds") != approved_seconds):
        raise AcceptanceError("teardown revision or approved total budget does not match the campaign")
    attempt = report.get("attempt", {})
    prior_elapsed = attempt.get("prior_elapsed_ms", 0) if isinstance(attempt, dict) else None
    if type(prior_elapsed) is not int or prior_elapsed < 0:
        raise AcceptanceError("prior-attempt time accounting is invalid")
    campaign_elapsed = _campaign_elapsed_ms(report)
    _tighten_stage_budget(_remaining_campaign_seconds(
        approved_seconds, prior_elapsed, campaign_elapsed, stage="teardown",
    ))
    _mark_stage_running(report, "teardown")
    _atomic_replace_json(report_path, report)
    try:
        cleanup_campaign(state_root, run_id, confirm_run_id, staged_teardown=True)
    except BaseException:
        try:
            report = _read_private_json(report_path)
            report.setdefault("stages", {})["teardown"] = "BLOCKED"
            report.setdefault("stage_elapsed_ms", {})["teardown"] = min(
                STAGE_BUDGET_SECONDS * 1000, _stage_elapsed_ms(),
            )
            cleanup = report.get("cleanup")
            if not isinstance(cleanup, dict):
                cleanup = {}
                report["cleanup"] = cleanup
            cleanup["status"] = "BLOCKED"
            cleanup.setdefault("reason_code", "exact_teardown_unconfirmed")
            report["result"] = _campaign_result(report)
            _atomic_replace_json(report_path, report)
        except (OSError, AcceptanceError, UnicodeError, json.JSONDecodeError):
            pass
        raise
    report = _read_private_json(report_path)
    return report, report_path


def record_human_observation(state_root: Path, run_id: str, confirm_run_id: str,
                             gate: str, outcome: str, version: str,
                             checklist: list[str]) -> dict[str, Any]:
    """Record a version-bound owner checklist; never infer a result from automation."""
    if run_id != confirm_run_id or RUN_ID_RE.fullmatch(run_id) is None:
        raise AcceptanceError("human observation requires the exact campaign run ID")
    if gate not in OWNER_CHECKLISTS:
        raise AcceptanceError("owner observation gate is not supported")
    if gate in DEFERRED_GATE_REASONS:
        raise AcceptanceError("this live provider gate is deferred from the local-first campaign")
    if outcome not in {"PASS", "FAIL", "BLOCKED"}:
        raise AcceptanceError("human observation outcome must be PASS, FAIL, or BLOCKED")
    if not isinstance(checklist, list) or not checklist or len(checklist) != len(set(checklist)):
        raise AcceptanceError("human observation requires unique fixed checklist IDs")
    required = OWNER_CHECKLISTS[gate]
    if any(item not in required for item in checklist):
        raise AcceptanceError("human observation contains an unknown checklist ID")
    if outcome == "PASS" and set(checklist) != set(required):
        raise AcceptanceError("a human PASS requires every gate-specific checklist item")
    if not re.fullmatch(r"2\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", version):
        raise AcceptanceError("observed OpenCode version is invalid")
    _private_directory(state_root)
    run_root = _namespace_path(state_root, _run_name(run_id))
    report_path = run_root.parent / "reports" / f"{_run_name(run_id)}.json"
    _private_directory(report_path.parent)
    report = _read_private_json(report_path)
    if (report.get("schema_version") != 1 or report.get("run_id") != run_id
            or report.get("run_name") != _run_name(run_id)):
        raise AcceptanceError("campaign report does not belong to this exact run")
    selected_suites = report.get("suites")
    report_gates = report.get("gates")
    if (not isinstance(selected_suites, list) or any(item not in ALL_SUITES for item in selected_suites)
            or not isinstance(report_gates, dict)
            or report.get("required_gates") != _required_gates(selected_suites)
            or report.get("acceptance_scope") != _acceptance_scope(selected_suites)):
        raise AcceptanceError("campaign suite or gate manifest is invalid")
    selected_gates = set(report.get("required_gates", []))
    if gate not in selected_gates:
        raise AcceptanceError("this gate was not selected for the exact campaign")
    clean_gate = report_gates.get("HOST-CLEAN", {})
    verified_version = report.get("verified_guest_opencode_version")
    if (not isinstance(clean_gate, dict) or clean_gate.get("status") != "PASS"
            or not isinstance(verified_version, str) or verified_version != version):
        raise AcceptanceError("owner observation version does not match the verified fresh guest")
    if isinstance(report.get("cleanup"), dict) and report["cleanup"].get("status") == "PASS":
        raise AcceptanceError("the exact campaign is finalized; owner observations cannot change its report")
    existing = report_gates.get(gate)
    if not isinstance(existing, dict) or existing.get("status") not in {"BLOCKED", "NOT_RUN"}:
        raise AcceptanceError("gate is not awaiting an owner observation")
    allowed_reasons = {
        "not_run", "owner_observation_required", "owner_live_evidence_required",
        "owner_local_model_observation_required",
        "docker_rollback_smoke_required",
        "full_lifecycle_suite_incomplete",
    }
    if existing.get("reason_code") not in allowed_reasons:
        raise AcceptanceError("automated failure or prerequisite block cannot be overridden by an owner attestation")
    if existing.get("automated_status") in {"FAIL", "BLOCKED"}:
        raise AcceptanceError("owner observation cannot override a failed or blocked automated subcheck")
    prior = dict(existing)
    prior.pop("status")
    prior_reason = prior.pop("reason_code", None)
    if prior_reason is not None:
        prior["pending_reason_code"] = prior_reason
    report_gates[gate] = {
        **prior,
        "status": outcome,
        "observation": "OWNER_RECORDED",
        "recorded_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "opencode_version": version,
        "checklist_ids": list(checklist),
    }
    for stage in STAGE_ORDER:
        if report.get("stages", {}).get(stage) not in {"PASS", "FAIL", "BLOCKED"}:
            continue
        if stage == "early-auth":
            continue
        if stage == "final-auth":
            report["stages"][stage] = (
                "PASS" if report_gates.get("HUMAN-AUTH", {}).get("status") == "PASS" else "BLOCKED"
            )
            continue
        stage_suites = [suite for suite in STAGE_SUITES[stage] if suite in selected_suites]
        stage_gates = {item for suite in stage_suites for item in SUITE_GATES[suite]}
        stage_gates.intersection_update(report["required_gates"])
        statuses_for_stage = [report_gates.get(item, {}).get("status", "NOT_RUN") for item in stage_gates]
        report["stages"][stage] = (
            "FAIL" if "FAIL" in statuses_for_stage else
            "PASS" if statuses_for_stage and all(item == "PASS" for item in statuses_for_stage) else
            "BLOCKED"
        )
    report["result"] = _campaign_result(report)
    report["last_observation_utc"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    _atomic_replace_json(report_path, report)
    return {"run_id": run_id, "gate": gate, "status": outcome,
            "campaign_result": report["result"], "report_path": str(report_path)}


def campaign_status(state_root: Path, run_id: str) -> dict[str, Any]:
    report_path = _campaign_report_path(state_root, run_id)
    _private_directory(report_path.parent)
    report = _read_private_json(report_path)
    suites = report.get("suites")
    if (report.get("run_id") != run_id or report.get("run_name") != _run_name(run_id)
            or report.get("workflow_version") != WORKFLOW_VERSION
            or not isinstance(suites, list)
            or report.get("required_gates") != _required_gates(suites)
            or report.get("acceptance_scope") != _acceptance_scope(suites)):
        raise AcceptanceError("campaign status is not bound to the exact staged run")
    gates = report.get("gates")
    stages = report.get("stages")
    if not isinstance(gates, dict) or not isinstance(stages, dict):
        raise AcceptanceError("campaign status manifest is invalid")
    elapsed_ms = _campaign_elapsed_ms(report)
    legacy_elapsed = report.get("legacy_elapsed_ms", 0)
    attempt = report.get("attempt", {})
    prior_elapsed = attempt.get("prior_elapsed_ms", 0) if isinstance(attempt, dict) else None
    if (type(legacy_elapsed) is not int or not 0 <= legacy_elapsed <= STAGE_BUDGET_SECONDS * 1000
            or type(prior_elapsed) is not int or prior_elapsed < 0):
        raise AcceptanceError("campaign status time accounting is invalid")
    elapsed_ms += legacy_elapsed + prior_elapsed
    total_seconds = report.get("approved_total_budget_seconds")
    if type(total_seconds) is not int or total_seconds < 1:
        raise AcceptanceError("campaign status has no valid approved total budget")
    return {
        "run_id": run_id,
        "result": report.get("result", "BLOCKED"),
        "stages": {stage: stages.get(stage, "NOT_RUN") for stage in STAGE_ORDER},
        "gates": {gate: gates.get(gate, {}).get("status", "NOT_RUN")
                  for gate in report.get("required_gates", [])},
        "acceptance_scope": report["acceptance_scope"],
        "deferred_gates": report["acceptance_scope"]["deferred_gates"],
        "deferred_checks": report["acceptance_scope"]["deferred_checks"],
        "report_path": str(report_path),
        "cleanup": report.get("cleanup", {}).get("status", "NOT_RUN"),
        "approved_total_minutes": total_seconds // 60,
        "elapsed_minutes": elapsed_ms // 60_000,
        "remaining_minutes": max(0, total_seconds - elapsed_ms // 1000) // 60,
    }


def _openai_auth_checkpoint(state_root: Path, run_id: str, workspace: Path,
                            memory_gib: int, report: dict[str, Any],
                            report_path: Path) -> dict[str, Any]:
    selected = report.get("provider_config", {}).get("providers", [])
    if not any(isinstance(item, dict) and item.get("provider") == "openai" for item in selected):
        return {"status": "BLOCKED", "reason_code": "openai_model_not_selected"}
    clean = report.get("gates", {}).get("HOST-CLEAN", {})
    if not isinstance(clean, dict) or clean.get("status") != "PASS":
        return {"status": "BLOCKED", "reason_code": "fresh_guest_not_usable"}
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return {"status": "BLOCKED", "reason_code": "interactive_terminal_unavailable"}
    selection = _provider_config_entry(report, "openai")
    if selection is None:
        return {"status": "BLOCKED", "reason_code": "openai_model_not_selected"}
    print("Optional OpenAI browser sign-in: credentials stay in the host browser. "
          "Choose y to open the campaign guest's native bridge; press Enter to skip.")
    try:
        print("Open browser sign-in now? [y/N] ", end="", flush=True)
        ready, _, _ = select.select([sys.stdin], [], [], min(120, _bounded_timeout(120)))
        if not ready:
            return {"status": "BLOCKED", "reason_code": "owner_checkpoint_timeout"}
        answer = sys.stdin.readline().strip().lower()
    except (EOFError, KeyboardInterrupt, OSError, ValueError):
        return {"status": "BLOCKED", "reason_code": "owner_checkpoint_skipped"}
    if answer not in {"y", "yes"}:
        return {"status": "BLOCKED", "reason_code": "owner_checkpoint_skipped"}
    marker = report_path.parent / f"auth-callback-{run_id}.json"
    if marker.exists() or marker.is_symlink():
        return {"status": "BLOCKED", "reason_code": "callback_evidence_collision"}
    profile, data, reason = _ensure_provider_profile(
        state_root, run_id, workspace, report, selection, memory_gib,
    )
    if data is None:
        return {"status": "BLOCKED", "reason_code": reason or "openai_profile_unavailable"}
    command = [str(ROOT / "lab-vm"), "agent", "--name", profile,
               "--permissions", "ask", "--browser-auth"]
    bridge_exit_code: int | None = None
    bridge_failure = "browser_bridge_unavailable"
    try:
        completed = subprocess.run(
            command, cwd=ROOT, env={**os.environ, "MESTER_STATE_DIR": str(state_root),
                                    "VM_AUTH_ACCEPTANCE_MARKER": str(marker)},
            stdin=None, stdout=None, stderr=None, check=False, timeout=_bounded_timeout(1800),
        )
        bridge_exit_code = completed.returncode
    except subprocess.TimeoutExpired:
        bridge_failure = "browser_checkpoint_timeout"
    except OSError:
        pass
    except KeyboardInterrupt:
        bridge_failure = "browser_checkpoint_interrupted"

    callback_cleanup = False
    try:
        marker_report = _read_private_json(marker, maximum=4096)
        callback_cleanup = marker_report == {"callback_cleanup": "PASS"}
    except AcceptanceError:
        pass
    finally:
        if marker.exists() and not marker.is_symlink():
            marker.unlink()

    auth_code = r'''import json,subprocess
try:
 p=subprocess.run(["opencode","auth","list","--format","json"],stdin=subprocess.DEVNULL,
                  stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=15,check=False)
 data=json.loads(p.stdout) if p.returncode==0 and len(p.stdout)<=65536 else None
 def has_openai(value):
  if isinstance(value,dict):
   if value.get("provider")=="openai" or value.get("name")=="openai": return True
   if "openai" in value: return True
   return any(has_openai(item) for item in value.values())
  if isinstance(value,list): return any(has_openai(item) for item in value)
  return False
 print(json.dumps({"auth_list_completed":p.returncode==0,"openai_present":has_openai(data)}))
except Exception:
 print(json.dumps({"auth_list_completed":False,"openai_present":False}))'''
    guest_command = "python3 -c " + shlex.quote(auth_code)
    try:
        if bridge_exit_code != 0:
            raise AcceptanceError("browser bridge did not complete successfully")
        auth_result = _guest_exec(state_root, profile, guest_command, timeout=30)
        auth_value = json.loads(auth_result.stdout)
        auth_verified = (auth_value == {"auth_list_completed": True, "openai_present": True})
    except (AcceptanceError, TimeoutError, UnicodeError, json.JSONDecodeError):
        auth_verified = False
    try:
        profile_suspended = _suspend_profile(state_root, profile, memory_gib)
    except (AcceptanceError, OSError, RuntimeError, ValueError):
        profile_suspended = False
    passed = bridge_exit_code == 0 and callback_cleanup and auth_verified and profile_suspended
    return {
        "status": "PASS" if passed else "BLOCKED",
        "reason_code": None if passed else (
            bridge_failure if bridge_exit_code is None else "browser_login_or_callback_verification_incomplete"
        ),
        "bridge_command_exit_code": bridge_exit_code,
        "auth_list_verified": auth_verified,
        "temporary_callbacks_removed": callback_cleanup,
        "provider_profile_suspended": profile_suspended,
    }


@_bounded_stage
def execute_stage(source: Path, state_root: Path, run_id: str, suites: list[str],
                  memory_gib: int, confirm_run_id: str, stage: str, *,
                  start: bool = False, apply_selected_updates: bool = False,
                  provider_config_path: str | os.PathLike[str] | None = None,
                  approved_total_minutes: int | None = None,
                  retry_of: str | None = None,
                  retry_reason: str | None = None) -> tuple[dict[str, Any], Path]:
    if stage not in STAGE_ORDER or confirm_run_id != run_id:
        raise AcceptanceError("staged execution requires an exact run ID and supported stage")
    if stage == "teardown":
        if start:
            raise AcceptanceError("teardown can only resume an existing exact campaign")
        if approved_total_minutes is None:
            raise AcceptanceError("teardown requires the original explicit total campaign budget")
        return execute_teardown_stage(
            state_root, run_id, confirm_run_id, approved_total_minutes,
        )
    if start and stage != STAGE_ORDER[0]:
        raise AcceptanceError("a new staged campaign must start at clean-usability")
    if not start and stage == STAGE_ORDER[0]:
        raise AcceptanceError("clean-usability is available only to a new campaign")
    if not any(suite != "preflight" for suite in suites):
        raise AcceptanceError("staged campaign requires at least one acceptance suite")
    approved_seconds = _approved_budget_seconds(approved_total_minutes)
    if start and stage != STAGE_ORDER[0]:
        raise AcceptanceError("fresh attempts must restart at clean-usability")
    if not start and (retry_of is not None or retry_reason is not None):
        raise AcceptanceError("retry authorization can be supplied only with --start")
    stage_suites = [suite for suite in STAGE_SUITES[stage] if suite in suites]
    if stage == "clean-usability":
        stage_suites = ["clean"]

    if stage in {"early-auth", "final-auth"}:
        initial = preflight(source, state_root, run_id, memory_gib, suites,
                            provider_config_path, resume=True)
        if initial["result"] != "PASS":
            return {"run_id": run_id, "result": "BLOCKED", "preflight": initial}, Path()
        config_sha = initial.get("provider_config", {}).get("sha256", "")
        scope = _campaign_scope(
            suites, memory_gib, config_sha, apply_selected_updates, approved_seconds,
        )
        report, _run_root, _child_state, _workspace = _resume_campaign(
            state_root, source, run_id, suites,
            config_sha, initial["source"], scope,
        )
        if report.get("approved_total_budget_seconds") != approved_seconds:
            raise AcceptanceError("approved total campaign budget changed; resume was refused")
        elapsed_ms = _campaign_elapsed_ms(report)
        attempt = report.get("attempt", {})
        prior_elapsed_ms = attempt.get("prior_elapsed_ms", 0) if isinstance(attempt, dict) else None
        if type(prior_elapsed_ms) is not int or prior_elapsed_ms < 0:
            raise AcceptanceError("prior-attempt time accounting is invalid")
        _tighten_stage_budget(_remaining_campaign_seconds(
            approved_seconds, prior_elapsed_ms, elapsed_ms, stage=stage,
        ))
        report_path = _campaign_report_path(state_root, run_id)
        expected_effects = {
            "selected_guest_update_targets": ["config", "tools"] if apply_selected_updates else [],
            "selected_guest_updates_authorized": apply_selected_updates,
        }
        if report.get("requested_effects") != expected_effects:
            raise AcceptanceError("selected campaign effects changed; resume requires a fresh attempt")
        _mark_stage_running(report, stage)
        states = report["stages"]
        _atomic_replace_json(report_path, report)
        if stage == "early-auth":
            report["early_auth"] = {
                "status": "DEFERRED", "reason_code": "owner_deferred_live_openai",
                "campaign_effect": "none",
            }
            states[stage] = "DEFERRED"
        else:
            states[stage] = "DEFERRED"
            report["final_auth"] = {
                "status": "DEFERRED", "reason_code": "owner_deferred_live_openai",
                "campaign_effect": "none",
            }
        report["stage_elapsed_ms"][stage] = _stage_elapsed_ms()
        report["result"] = _campaign_result(report)
        report["finished_utc"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        _atomic_replace_json(report_path, report)
        return report, report_path

    return execute_campaign(
        source, state_root, run_id, stage_suites, memory_gib, confirm_run_id,
        apply_selected_updates=apply_selected_updates,
        provider_config_path=provider_config_path,
        campaign_suites=suites, workflow_stage=stage, resume=not start,
        approved_total_minutes=approved_total_minutes,
        retry_of=retry_of, retry_reason=retry_reason,
    )


DEV_STEPS = ("inspect", "boot", "auth", "work", "metrics", "nested")
DEV_STATUSES = {"PASS", "FAIL", "NOT_RUN", "WAITING_OWNER", "BLOCKED_EXTERNAL"}
DEV_MAX_OUTPUT = 64 * 1024
DEV_PROXY_HOST = "auth.openai.com"


def _dev_check(check_id: str, expected: str, observed: Any, status: str,
               cause_exit_code: int | None = None, *, repair: str = "M0-exact-transport",
               action: str = "Repair this exact check and rerun its evidence step.",
               blocks: list[str] | None = None) -> dict[str, Any]:
    if status not in DEV_STATUSES:
        raise AcceptanceError("development evidence status is invalid")
    check = {"id": check_id, "expected": expected, "observed": observed,
             "status": status, "cause_exit_code": cause_exit_code}
    if status != "PASS":
        check.update({"repair_packet": repair, "next_action": action,
                      "blocks_live_checks": list(blocks or [])})
    return check


def _dev_not_run(checks: list[dict[str, Any]], check_id: str, expected: str,
                 repair: str, action: str, blocks: list[str]) -> None:
    checks.append(_dev_check(check_id, expected, "not_attempted_due_to_prerequisite", "NOT_RUN",
                             repair=repair, action=action, blocks=blocks))


def _dev_guest_json(state_root: Path, profile: str, code: str) -> tuple[dict[str, Any] | None, int | None, str]:
    try:
        completed = _guest_exec(
            state_root, profile, "python3 -c " + shlex.quote(code), timeout=20,
            campaign_profile=False, allow_failure=True,
        )
    except TimeoutError:
        return None, 124, "guest_command_timeout"
    except (AcceptanceError, OSError, RuntimeError):
        return None, None, "exact_guest_transport_unavailable"
    if completed.returncode != 0:
        return None, completed.returncode, "guest_command_failed"
    if len(completed.stdout) > DEV_MAX_OUTPUT:
        return None, completed.returncode, "guest_output_limit_exceeded"
    try:
        value = _decode_json_no_duplicates(completed.stdout)
    except (AcceptanceError, UnicodeError, json.JSONDecodeError, ValueError):
        return None, completed.returncode, "guest_probe_output_invalid"
    return (value, completed.returncode, "guest_probe_completed") if isinstance(value, dict) else \
        (None, completed.returncode, "guest_probe_output_invalid")


DEV_GUEST_FACTS = r'''import json,os,pwd,re,shutil
from pathlib import Path
def available():
 for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
  if line.startswith("MemAvailable:"): return int(line.split()[1])//1024
 raise ValueError
def virt():
 text=Path("/proc/cpuinfo").read_text(encoding="ascii",errors="ignore")[:65536]
 return any(re.search(r"\b(vmx|svm)\b",x) for x in text.splitlines() if x.startswith("flags"))
mount=any(len(x:=line.split())>=3 and x[1]=="/workspace" and x[2]=="virtiofs" for line in Path("/proc/mounts").read_text().splitlines())
try: user=pwd.getpwuid(os.geteuid()).pw_name
except KeyError: user="unknown"
tools={n:shutil.which(n) is not None for n in ("qemu-system-x86_64","qemu-img","virtiofsd","socat","bats","shellcheck","cloud-init")}
print(json.dumps({"mester":user=="mester","opencode":shutil.which("opencode") is not None,
 "workspace_writable":os.access("/workspace",os.W_OK|os.X_OK),"workspace_virtiofs":mount,
 "kvm":os.access("/dev/kvm",os.R_OK|os.W_OK),"cpu_virtualization":virt(),
 "memavailable_mib":available(),"tools":tools},sort_keys=True,separators=(",",":")))
'''
DEV_GUEST_MARKER = r'''import json,os,stat
fd=os.open("/var/lib/lab-vm/guest-ready.json",os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK)
try:
 s=os.fstat(fd)
 if not(stat.S_ISREG(s.st_mode) and s.st_uid==0 and s.st_nlink==1 and stat.S_IMODE(s.st_mode)==0o644 and s.st_size<=16384): raise SystemExit(2)
 raw=os.read(fd,16385)
finally: os.close(fd)
if len(raw)!=s.st_size: raise SystemExit(2)
d=json.loads(raw)
print(json.dumps({k:d.get(k) for k in ("status","payload_sha256","opencode_version","workspace_mount","depth","max_depth","recursion_tools")},sort_keys=True,separators=(",",":")))
'''
DEV_GUEST_PROXY = r'''import json,socket
status=None
try:
 with socket.create_connection(("10.0.2.100",3128),timeout=5) as s:
  s.settimeout(5); s.sendall(b"CONNECT auth.openai.com:443 HTTP/1.1\r\nHost: auth.openai.com:443\r\nConnection: close\r\n\r\n")
  raw=bytearray()
  while b"\r\n" not in raw and len(raw)<1024:
   part=s.recv(min(256,1024-len(raw)))
   if not part: break
   raw.extend(part)
 fields=bytes(raw).split(b"\r\n",1)[0].decode("ascii","strict").split()
 if len(fields)>=2 and fields[0] in ("HTTP/1.0","HTTP/1.1") and fields[1].isdigit(): status=int(fields[1])
except (OSError,UnicodeError,ValueError,TimeoutError): pass
print(json.dumps({"connect_status":status},sort_keys=True,separators=(",",":")))
'''
DEV_GUEST_METRICS = r'''import json
from urllib.request import build_opener,ProxyHandler,Request
op=build_opener(ProxyHandler({})); out={"summary":False,"json":False,"cpu":False,"memory":False}
try:
 with op.open(Request("http://10.0.2.100:19532/summary",headers={"Accept":"text/plain"}),timeout=5) as r:
  b=r.read(65537); out["summary"]=r.status==200 and r.headers.get_content_type()=="text/plain" and 0<len(b)<=65536
except Exception: pass
try:
 with op.open(Request("http://10.0.2.100:19532/metrics",headers={"Accept":"application/json"}),timeout=5) as r:
  b=r.read(65537); d=json.loads(b) if r.status==200 and len(b)<=65536 else {}
  out["json"]=isinstance(d,dict) and isinstance(d.get("cpu"),dict) and isinstance(d.get("memory"),dict)
  out["cpu"]="load_avg_1m" in d.get("cpu",{}); out["memory"]="total_gb" in d.get("memory",{})
except Exception: pass
print(json.dumps(out,sort_keys=True,separators=(",",":")))
'''


def _dev_proxy_details(state_root: Path, profile: str, data: dict[str, Any]) -> tuple[Path, bool, bool]:
    run_dir = _profile_run_directory(state_root, profile, data)
    process = _load_module("vm_dev_inspect_process", ROOT / "lib/vm/process.py")
    registry = process.registry_read(run_dir / "processes.json", run_dir.name)
    item = registry.get("processes", {}).get("proxy")
    identity = item.get("identity") if isinstance(item, dict) else None
    if not isinstance(identity, dict) or process.identity_state(identity) != "running":
        raise AcceptanceError("exact profile proxy identity is unavailable")
    argv = identity.get("argv")
    runtime = state_root / "vm/profiles" / profile / "runtime"
    if not isinstance(argv, list) or len(argv) < 2 or argv[1] != str(runtime / "lib/vm/proxy.py"):
        raise AcceptanceError("proxy does not match the exact frozen profile runtime")

    def value(option: str) -> str | None:
        positions = [i for i, item in enumerate(argv[:-1]) if item == option]
        return argv[positions[0] + 1] if len(positions) == 1 else None

    if (value("--workspace") != data.get("workspace")
            or value("--port-file") != str(run_dir / "proxy.port")
            or value("--reload-marker") != str(run_dir / "proxy.ready")):
        raise AcceptanceError("proxy arguments do not match the selected profile")
    upstream = value("--upstream-proxy")
    if upstream not in (None, "http://10.0.2.100:3128") or (data.get("depth") == 0 and upstream):
        raise AcceptanceError("proxy parent does not match the fixed immediate-parent boundary")
    workspace = Path(data["workspace"])
    if workspace.resolve(strict=True) != workspace or not workspace.is_dir():
        raise AcceptanceError("selected workspace is not canonical")
    proxy = _load_module("vm_dev_inspect_proxy", runtime / "lib/vm/proxy.py")
    enabled, domains = proxy.policy(workspace / ".mester-egress")
    return runtime, bool(proxy.allowed(DEV_PROXY_HOST, enabled, domains)), upstream is not None


def _dev_metrics_port(profile: dict[str, Any], state_root: Path, name: str) -> int | None:
    if _profile_model_forward_identity(profile, state_root, name) is None:
        return None
    argv = profile["runtime_identity"]["argv"]
    netdevs = [argv[i + 1] for i, item in enumerate(argv[:-1]) if item == "-netdev"]
    if len(netdevs) != 1:
        return None
    pattern = re.compile(r"guestfwd=tcp:10\.0\.2\.100:19532-cmd:([A-Za-z0-9_./+-]+) - TCP:127\.0\.0\.1:([1-9][0-9]{0,4})")
    matches = [pattern.fullmatch(x) for x in netdevs[0].split(",") if x.startswith("guestfwd=tcp:10.0.2.100:19532-")]
    matches = [x for x in matches if x and Path(x.group(1)).name == "socat"]
    if len(matches) != 1:
        return None
    port = int(matches[0].group(2))
    return port if port <= 65535 else None


def _dev_host_metrics(state_root: Path, runtime: Path, port: int) -> tuple[bool, dict[str, Any]]:
    service = runtime / "lib/vm/metrics_service.py"
    empty = {"summary": False, "json": False, "cpu": False, "memory": False}
    if not service.is_file() or service.is_symlink():
        return False, empty
    try:
        verified = subprocess.run([sys.executable, str(service), "verify", "--state-root", str(state_root),
                                   "--port", str(port), "--quiet"], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=5, check=False, cwd=ROOT)
    except (OSError, subprocess.TimeoutExpired):
        return False, empty
    if verified.returncode != 0:
        return False, empty
    result = {"summary": False, "json": False, "cpu": False, "memory": False}
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", "/summary", headers={"Accept": "text/plain", "Connection": "close"})
        response = connection.getresponse(); body = response.read(DEV_MAX_OUTPUT + 1)
        result["summary"] = response.status == 200 and response.headers.get_content_type() == "text/plain" and 0 < len(body) <= DEV_MAX_OUTPUT
        connection.close(); connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/metrics", headers={"Accept": "application/json", "Connection": "close"})
        response = connection.getresponse(); body = response.read(DEV_MAX_OUTPUT + 1)
        data = _decode_json_no_duplicates(body) if response.status == 200 and len(body) <= DEV_MAX_OUTPUT else {}
        if isinstance(data, dict):
            result["json"] = isinstance(data.get("cpu"), dict) and isinstance(data.get("memory"), dict)
            result["cpu"] = isinstance(data.get("cpu"), dict) and "load_avg_1m" in data["cpu"]
            result["memory"] = isinstance(data.get("memory"), dict) and "total_gb" in data["memory"]
    except (OSError, http.client.HTTPException, TimeoutError, AcceptanceError, UnicodeError, json.JSONDecodeError):
        pass
    finally:
        connection.close()
    return True, result


def _dev_report_path(value: str, source: Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.name in ("", ".", ".."):
        raise AcceptanceError("development report must be an absolute fresh file path")
    _private_directory(path.parent)
    if (path.parent / path.name).resolve(strict=False) != path or path.is_relative_to(source):
        raise AcceptanceError("development report must be outside the shared checkout")
    if path.exists() or path.is_symlink():
        raise AcceptanceError("development report will not overwrite existing evidence")
    return path


def run_dev_step(source: Path, state_root: Path, profile: str, run_id: str, step: str,
                 *, dev_execute: bool = False, dev_report: str | None = None) -> dict[str, Any]:
    if step not in DEV_STEPS or PROFILE_NAME_RE.fullmatch(profile) is None:
        raise AcceptanceError("development step or exact profile name is invalid")
    if step != "inspect":
        packet = {"boot": "M1-boot", "auth": "M2a-auth-egress", "work": "M2b-agent-work",
                  "metrics": "M3-metrics", "nested": "M4-nested"}[step]
        check = _dev_check("step_available", "implemented development evidence step", "not_implemented",
                           "NOT_RUN", repair=packet, action=f"Implement {step} in its frozen packet.")
        report = {"step": step, "run_id": run_id, "location": "O", "source_head": None,
                  "source_tree_sha256": None, "profile": profile, "runtime_payload_sha256": None,
                  "guest_payload_sha256": None, "process_identity_summary": {"state": "NOT_RUN"},
                  "checks": [check], "result": "BLOCKED_EXTERNAL", "reason_code": "step_not_implemented",
                  "elapsed_ms": 0, "next_action": check["next_action"],
                  "protected_assets_unchanged": {"measured_assets": [], "unmeasured": "UNKNOWN"}, "artifacts": []}
        if dev_report:
            destination = _dev_report_path(dev_report, source)
            report["artifacts"] = [destination.name]
            _atomic_json(destination, report)
        return report
    if dev_execute:
        raise AcceptanceError("inspect is observe-only; --dev-execute is not valid for this step")

    started = time.monotonic(); checks: list[dict[str, Any]] = []
    source_head = source_tree = runtime_hash = guest_hash = None
    profile_data: dict[str, Any] | None = None; initial_profile: dict[str, Any] | None = None
    process_summary: dict[str, Any] = {"state": "UNKNOWN"}; transport = False
    try:
        source_info = _source_identity(source)
        source_head, source_tree = source_info["source_head"], source_info["tree_sha256"]
        checks.append(_dev_check("source_identity", "current source HEAD and tree digest",
                                 {"head_present": bool(source_head), "tree_digest_present": bool(source_tree)}, "PASS"))
    except (AcceptanceError, OSError, RuntimeError, ValueError, KeyError):
        checks.append(_dev_check("source_identity", "current source HEAD and tree digest", "unavailable", "FAIL",
                                 repair="M0-source-provenance", action="Repair source inventory before freezing an M1 candidate.",
                                 blocks=["boot", "work", "nested"]))

    root_ok = False
    try:
        if state_root == source or state_root in source.parents or source in state_root.parents:
            raise AcceptanceError("source checkout and selected profile state root overlap")
        _private_directory(state_root); root_ok = True
        checks.append(_dev_check("exact_state_root", "recorded canonical owner-private root", "available_0700", "PASS"))
    except (OSError, AcceptanceError, RuntimeError):
        checks.append(_dev_check("exact_state_root", "recorded canonical owner-private root", "unavailable_or_not_private",
                                 "BLOCKED_EXTERNAL", repair="M0-exact-transport",
                                 action="Run this exact inspect command in the recorded O terminal; do not substitute another root.",
                                 blocks=["boot", "auth", "work", "metrics", "nested"]))
    if root_ok:
        try:
            profile_data = _profile_record(state_root, profile, campaign_profile=False)
            initial_profile = dict(profile_data)
            runtime_hash = profile_data.get("runtime_payload_sha256")
            guest_hash = profile_data.get("guest_payload_sha256")
            checks.append(_dev_check("exact_profile", "only selected profile metadata", {
                "status": profile_data.get("status"), "depth": profile_data.get("depth"),
                "max_depth": profile_data.get("max_depth"), "memory_gib": profile_data.get("memory_gib")}, "PASS"))
            ready = profile_data.get("status") == "ready"
            checks.append(_dev_check("profile_ready", "selected profile is ready", profile_data.get("status"),
                                     "PASS" if ready else "FAIL", repair="M1-boot",
                                     action="Repair the first failed boot stage; preserve the existing overlay.",
                                     blocks=["auth", "work", "metrics", "nested"]))
            depth, maximum = profile_data.get("depth"), profile_data.get("max_depth")
            cap_ok = type(depth) is int and type(maximum) is int and depth <= maximum and maximum >= 2
            checks.append(_dev_check("recursion_cap", "separate candidate can inherit max_depth 2", {
                "depth": depth, "max_depth": maximum}, "PASS" if cap_ok else "FAIL",
                repair="M1-boot", action="Preserve this profile; create a separate current-source cap-2 candidate.",
                blocks=["nested"]))
        except (AcceptanceError, OSError, RuntimeError, ValueError, KeyError):
            checks.append(_dev_check("exact_profile", "only selected profile metadata", "unavailable", "BLOCKED_EXTERNAL",
                                     repair="M0-exact-transport",
                                     action="Run this exact inspect command in the recorded O terminal; do not select another profile.",
                                     blocks=["boot", "auth", "work", "metrics", "nested"]))

    # Read-only controller prerequisites; no profile is started, stopped or signalled.
    try:
        info = Path("/dev/kvm").stat(); kvm_ok = stat.S_ISCHR(info.st_mode) and os.access("/dev/kvm", os.R_OK | os.W_OK)
    except OSError: kvm_ok = False
    checks.append(_dev_check("controller_kvm", "controller exposes read/write KVM", kvm_ok,
                             "PASS" if kvm_ok else "BLOCKED_EXTERNAL", repair="M1-boot",
                             action="Use the authoritative launcher with nested KVM; do not use TCG.", blocks=["boot", "nested"]))
    try:
        cpu = Path("/proc/cpuinfo").read_text(encoding="ascii", errors="ignore")[:1024*1024]
        cpu_ok = any(re.search(r"\b(?:vmx|svm)\b", x) for x in cpu.splitlines() if x.startswith("flags"))
    except OSError: cpu_ok = False
    checks.append(_dev_check("controller_cpu", "controller exposes VMX/SVM", cpu_ok,
                             "PASS" if cpu_ok else "BLOCKED_EXTERNAL", repair="M1-boot",
                             action="Expose virtualization at the authoritative outer launcher; never edit identity markers.",
                             blocks=["boot", "nested"]))
    tools = ("qemu-system-x86_64", "qemu-img", "virtiofsd", "socat", "bats", "shellcheck", "cloud-init")
    missing = [name for name in tools if shutil.which(name) is None]
    checks.append(_dev_check("controller_recursion_tools", "candidate boot/test tools are installed", {"missing": missing},
                             "PASS" if not missing else "BLOCKED_EXTERNAL", repair="M1-boot",
                             action="Repair only the listed missing tools through the supported environment.", blocks=["boot", "nested"]))
    requested_gib = profile_data.get("memory_gib", 8) if profile_data else 8
    if type(requested_gib) is not int or not 1 <= requested_gib <= 128: requested_gib = 8
    try:
        budget = _read_mem_budget(requested_gib*1024, DEFAULT_RESERVE_MIB); budget_ok = budget.get("passed") is True
        budget_view = {k: budget.get(k) for k in ("memavailable_mib", "cgroup_remaining_mib", "projected_available_mib", "requested_mib", "reserve_mib")}
    except (OSError, RuntimeError, ValueError, KeyError): budget_ok, budget_view = False, {"readable": False}
    checks.append(_dev_check("controller_memory_headroom", "enough memory for candidate plus 512 MiB reserve", budget_view,
                             "PASS" if budget_ok else "BLOCKED_EXTERNAL", repair="M1-boot",
                             action="Report the exact deficit; do not stop or suspend an existing VM automatically.", blocks=["boot"]))

    runtime_ok = False
    if profile_data is not None:
        try:
            runtime_ok = _validate_profile_runtime(state_root, profile)
        except (OSError, RuntimeError, AcceptanceError, ValueError):
            runtime_ok = False
        checks.append(_dev_check("frozen_runtime", "selected frozen runtime validates", {
            "validated": runtime_ok, "runtime_payload_sha256_present": bool(runtime_hash),
            "guest_payload_sha256_present": bool(guest_hash)}, "PASS" if runtime_ok else "FAIL",
            repair="M1-boot", action="Repair frozen source/runtime identity; never modify the existing ready profile in place.",
            blocks=["boot", "auth", "work", "metrics", "nested"]))
        try:
            identity = profile_data.get("runtime_identity")
            process = _load_module("vm_dev_inspect_identity", ROOT / "lib/vm/process.py")
            state = process.identity_state({k: identity[k] for k in ("pid", "start_ticks", "executable", "argv")})
            process_summary = {"state": state, "pid": identity["pid"], "start_ticks": identity["start_ticks"],
                               "executable": Path(identity["executable"]).name}
            transport = (profile_data.get("status") == "ready" and runtime_ok and state == "running"
                         and _listener_owned_loopback(profile_data))
        except (OSError, RuntimeError, AcceptanceError, KeyError, TypeError, ValueError): transport = False
        checks.append(_dev_check("exact_qemu_ssh_identity", "selected QEMU process and loopback SSH identity match",
                                 process_summary, "PASS" if transport else "BLOCKED_EXTERNAL",
                                 repair="M0-exact-transport", action="Repair exact profile/listener association; never signal an unverified PID.",
                                 blocks=["auth", "work", "metrics", "nested"]))
    else:
        _dev_not_run(checks, "frozen_runtime", "selected frozen runtime validates", "M1-boot",
                     "Inspect the exact selected profile before runtime validation.", ["boot", "auth", "work", "metrics", "nested"])
        _dev_not_run(checks, "exact_qemu_ssh_identity", "selected QEMU and loopback SSH identity match", "M0-exact-transport",
                     "Inspect the exact selected profile; do not discover a substitute.", ["auth", "work", "metrics", "nested"])

    if transport:
        facts, cause, detail = _dev_guest_json(state_root, profile, DEV_GUEST_FACTS)
        if facts is None:
            checks.append(_dev_check("guest_facts", "fixed guest command returns bounded facts", detail, "FAIL", cause,
                                     repair="M1-boot", action="Repair mester PATH/guest execution or exact SSH transport; preserve the command rc.",
                                     blocks=["auth", "work", "metrics", "nested"]))
        else:
            checks.append(_dev_check("guest_mester_opencode_path", "guest execution is mester with OpenCode on PATH",
                                     {"mester": facts.get("mester"), "opencode": facts.get("opencode")},
                                     "PASS" if facts.get("mester") and facts.get("opencode") else "FAIL",
                                     repair="M1-boot", action="Repair only the diagnosed guest user/PATH bootstrap fault.", blocks=["auth", "work"]))
            checks.append(_dev_check("guest_workspace", "writable virtiofs /workspace",
                                     {"writable": facts.get("workspace_writable"), "virtiofs": facts.get("workspace_virtiofs")},
                                     "PASS" if facts.get("workspace_writable") and facts.get("workspace_virtiofs") else "FAIL",
                                     repair="M1-boot", action="Repair guest workspace ownership/mount in a fresh candidate.", blocks=["work", "nested"]))
            checks.append(_dev_check("guest_kvm_cpu", "guest has KVM and VMX/SVM",
                                     {"kvm": facts.get("kvm"), "cpu_virtualization": facts.get("cpu_virtualization")},
                                     "PASS" if facts.get("kvm") and facts.get("cpu_virtualization") else "BLOCKED_EXTERNAL",
                                     repair="M1-boot", action="Repair nested KVM/CPU exposure at its owning launcher.", blocks=["nested"]))
            guest_tools = facts.get("tools") if isinstance(facts.get("tools"), dict) else {}
            guest_missing = [name for name in tools if guest_tools.get(name) is not True]
            checks.append(_dev_check("guest_recursion_tools", "guest recursion tools are available", {"missing": guest_missing},
                                     "PASS" if not guest_missing else "FAIL", repair="M1-boot",
                                     action="Repair only missing guest tools and freeze the fix into a new candidate.", blocks=["nested"]))
            available = facts.get("memavailable_mib")
            checks.append(_dev_check("guest_memory_headroom", "guest has at least 512 MiB available", available,
                                     "PASS" if type(available) is int and available >= DEFAULT_RESERVE_MIB else "BLOCKED_EXTERNAL",
                                     repair="M1-boot", action="Report exact memory headroom; do not resize or stop a VM automatically.", blocks=["nested"]))
        marker, marker_rc, marker_reason = _dev_guest_json(state_root, profile, DEV_GUEST_MARKER)
        marker_ok = bool(marker and marker.get("status") == "ready" and marker.get("payload_sha256") == guest_hash
                         and marker.get("workspace_mount") == "virtiofs")
        checks.append(_dev_check("guest_payload_identity", "guest marker matches source profile payload",
                                 {"available": marker is not None, "payload_matches": bool(marker and marker.get("payload_sha256") == guest_hash),
                                  "workspace_mount": marker.get("workspace_mount") if marker else marker_reason},
                                 "PASS" if marker_ok else "FAIL", marker_rc if marker is None else None,
                                 repair="M1-boot", action="Repair guest-ready identity in a fresh candidate; do not inspect auth stores.",
                                 blocks=["work", "nested"]))
        try:
            runtime, policy_allows, upstream_present = _dev_proxy_details(state_root, profile, profile_data)
            proxy, cause, detail = _dev_guest_json(state_root, profile, DEV_GUEST_PROXY)
            code = proxy.get("connect_status") if proxy else None
            if policy_allows and code == 200:
                status, layer, action = "PASS", "selected_policy_and_connect_pass", ""
            elif not policy_allows:
                status, layer, action = "FAIL", "selected_workspace_egress_policy_denied", \
                    "Repair the selected .mester-egress allow rule and exact-owned reload path (M2a)."
            elif code == 403 and upstream_present:
                status, layer = "FAIL", "immediate_parent_proxy_denied_after_child_policy_allow"
                action = "Repair the immediate parent's mediated egress policy; the selected guest must not control an ancestor."
            elif code == 403:
                status, layer = "FAIL", "denied_beyond_selected_workspace_policy"
                action = "Repair the exact mediated upstream/outer egress policy; the selected .mester-egress allows this public host."
            else:
                status, layer, action = "FAIL", "selected_proxy_transport_unavailable", \
                    "Repair the exact proxy/QEMU path; do not retry a model completion as a network diagnostic."
            checks.append(_dev_check("openai_auth_https_connect", "no-credential CONNECT to auth.openai.com returns 200",
                                     {"selected_policy_allows": policy_allows, "connect_status": code,
                                      "diagnosed_layer": layer, "probe": detail}, status, cause,
                                     repair="M2a-auth-egress", action=action or "No egress repair required.", blocks=["auth", "work"] if status != "PASS" else []))
        except (AcceptanceError, OSError, RuntimeError, ValueError, KeyError):
            checks.append(_dev_check("openai_auth_https_connect", "no-credential CONNECT to auth.openai.com returns 200",
                                     "exact_proxy_identity_or_policy_unavailable", "BLOCKED_EXTERNAL",
                                     repair="M0-proxy-diagnosis", action="Verify exact proxy identity and policy without issuing a completion request.",
                                     blocks=["auth", "work"]))
    else:
        for args in (
            ("guest_facts", "fixed guest command returns bounded facts", "M0-exact-transport", "Repair selected transport before guest inspection.", ["auth", "work", "metrics", "nested"]),
            ("guest_mester_opencode_path", "guest mester has OpenCode on PATH", "M1-boot", "Verify mester/PATH after transport repair.", ["auth", "work"]),
            ("guest_workspace", "writable virtiofs /workspace", "M1-boot", "Verify workspace after transport repair.", ["work", "nested"]),
            ("guest_kvm_cpu", "guest KVM and VMX/SVM", "M1-boot", "Verify nested capability after transport repair.", ["nested"]),
            ("guest_recursion_tools", "guest recursion tools", "M1-boot", "Verify tools after transport repair.", ["nested"]),
            ("guest_memory_headroom", "guest has 512 MiB headroom", "M1-boot", "Verify memory after transport repair.", ["nested"]),
            ("guest_payload_identity", "guest marker matches selected payload", "M1-boot", "Verify guest identity after transport repair.", ["work", "nested"]),
            ("openai_auth_https_connect", "no-credential CONNECT to auth.openai.com returns 200", "M2a-auth-egress", "Verify exact proxy after transport repair.", ["auth", "work"]),
        ):
            _dev_not_run(checks, *args)

    # Metrics is deliberately independent from the auth HTTPS check.
    try:
        port = _dev_metrics_port(profile_data, state_root, profile) if profile_data and transport else None
    except (AcceptanceError, OSError, RuntimeError, ValueError, KeyError, TypeError):
        port = None
    if profile_data is None or not transport:
        _dev_not_run(checks, "metrics_forward", "exact profile guestfwd targets selected metrics host port",
                     "M0-exact-transport", "Repair/verify exact profile identity before inspecting its guest forward.", ["metrics"])
        _dev_not_run(checks, "metrics_host_http", "owned /summary and /metrics endpoints respond", "M3-metrics",
                     "Verify host endpoints after exact profile transport is available.", ["metrics"])
        _dev_not_run(checks, "metrics_guest_http", "guest /summary text and CPU/RAM JSON respond", "M3-metrics",
                     "Verify the guest URL after exact profile transport is available.", ["metrics"])
    elif port is None:
        checks.append(_dev_check("metrics_forward", "exact profile guestfwd targets selected metrics host port",
                                 "no_verified_metrics_forward", "FAIL", repair="M1-metrics-port",
                                 action="Implement configurable managed host port selection before freezing a new candidate.",
                                 blocks=["metrics"]))
        _dev_not_run(checks, "metrics_host_http", "owned /summary and /metrics endpoints respond", "M3-metrics",
                     "Verify host endpoints after M1 managed port selection.", ["metrics"])
        _dev_not_run(checks, "metrics_guest_http", "guest /summary text and CPU/RAM JSON respond", "M3-metrics",
                     "Verify the guest URL after an exact forward is present.", ["metrics"])
    else:
        runtime = state_root / "vm/profiles" / profile / "runtime"
        try:
            owned, details = _dev_host_metrics(state_root, runtime, port)
            checks.append(_dev_check("metrics_host_service", "managed service owns selected host port",
                                     {"owned": owned, "port": port}, "PASS" if owned else "FAIL",
                                     repair="M3-metrics", action="Select a verified managed alternate; leave foreign listeners untouched.", blocks=["metrics"]))
            host_ok = owned and details["summary"] and details["json"] and details["cpu"] and details["memory"]
            checks.append(_dev_check("metrics_host_http", "owned /summary text and /metrics CPU/RAM JSON respond", details,
                                     "PASS" if host_ok else "FAIL", repair="M3-metrics",
                                     action="Repair the first failed owned host HTTP link; do not adopt a foreign service.", blocks=["metrics"]))
            if owned:
                guest, cause, reason = _dev_guest_json(state_root, profile, DEV_GUEST_METRICS)
                guest_ok = bool(guest and guest.get("summary") and guest.get("json") and guest.get("cpu") and guest.get("memory"))
                checks.append(_dev_check("metrics_guest_http", "guest /summary text and CPU/RAM JSON respond",
                                         guest if guest else reason, "PASS" if guest_ok else "FAIL", cause,
                                         repair="M3-metrics", action="Repair the matched QEMU forward or guest URL; retain the host HTTP result.", blocks=["metrics"]))
            else:
                _dev_not_run(checks, "metrics_guest_http", "guest /summary and CPU/RAM JSON respond", "M3-metrics",
                             "Do not contact the guest forward until its host listener is verified as managed.", ["metrics"])
        except (AcceptanceError, OSError, RuntimeError, ValueError, KeyError):
            checks.append(_dev_check("metrics_host_service", "managed service owns selected host port", "verification_failed",
                                     "FAIL", repair="M3-metrics", action="Repair the exact managed port/service; leave foreign listeners untouched.", blocks=["metrics"]))
            _dev_not_run(checks, "metrics_guest_http", "guest /summary and CPU/RAM JSON respond", "M3-metrics",
                         "Verify managed host service before guest HTTP probe.", ["metrics"])

    protected = {"selected_profile_metadata": "UNKNOWN", "other_profiles": "NOT_MEASURED",
                 "credentials": "NOT_INSPECTED", "old_vm_session": "NOT_MEASURED",
                 "source_user_changes": "NOT_WRITTEN_BY_INSPECT"}
    if initial_profile is not None:
        try:
            protected["selected_profile_metadata"] = "UNCHANGED_DURING_INSPECT" if \
                _profile_record(state_root, profile, campaign_profile=False) == initial_profile else "CHANGED"
        except (AcceptanceError, OSError, RuntimeError, ValueError):
            protected["selected_profile_metadata"] = "UNKNOWN"
    failures = [item for item in checks if item["status"] != "PASS"]
    if any(item["status"] == "FAIL" for item in failures): result = "FAIL"
    elif any(item["status"] == "BLOCKED_EXTERNAL" for item in failures): result = "BLOCKED_EXTERNAL"
    elif failures: result = "FAIL"
    else: result = "PASS"
    first = failures[0] if failures else None
    report = {"step": "inspect", "run_id": run_id, "location": "O", "source_head": source_head,
              "source_tree_sha256": source_tree, "profile": profile, "runtime_payload_sha256": runtime_hash,
              "guest_payload_sha256": guest_hash, "process_identity_summary": process_summary,
              "checks": checks, "result": result, "reason_code": first["id"] if first else "inspect_checks_passed",
              "elapsed_ms": max(0, int((time.monotonic()-started)*1000)),
              "next_action": first.get("next_action") if first else "Continue to M1 candidate preparation after Sol review.",
              "protected_assets_unchanged": protected, "artifacts": []}
    if dev_report:
        destination = _dev_report_path(dev_report, source)
        report["artifacts"] = [destination.name]
        _atomic_json(destination, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", help="canonical checkout to snapshot and test")
    parser.add_argument("--state-root", help="existing private 0700 state directory")
    parser.add_argument("--suite", action="append", choices=(*ALL_SUITES, "all"),
                        help="select one or more independent acceptance suites")
    parser.add_argument("--run-id", help="32 lowercase hexadecimal ID; plan output supplies one if omitted")
    parser.add_argument("--memory-gib", type=int, help="requested outer guest memory (default: 8 GiB)")
    parser.add_argument("--apply-selected-updates", action="store_true",
                        help="plan and explicitly apply only the lifecycle suite's config/tools update targets")
    parser.add_argument("--provider-config", help="private credential-free explicit provider/model selections (0600 JSON)")
    parser.add_argument("--discover-providers", action="store_true",
                        help="read only the model selected by one exact ready profile and verified forward")
    parser.add_argument("--profile", help="exact ready profile name for --discover-providers")
    parser.add_argument("--dev-step", choices=DEV_STEPS,
                        help="run one independent development evidence probe (not a campaign stage)")
    parser.add_argument("--dev-execute", action="store_true",
                        help="consent to this step's documented synthetic guest action")
    parser.add_argument("--dev-report", help="write a fresh private JSON report beneath an existing owner0700 parent")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--execute", dest="operation", action="store_const", const="execute",
                        help="disabled one-shot path; use the exact staged workflow")
    action.add_argument("--start", dest="operation", action="store_const", const="start",
                        help="start a new staged campaign at clean-usability")
    action.add_argument("--resume", dest="operation", action="store_const", const="resume",
                        help="run the next named stage for the exact unchanged campaign")
    action.add_argument("--status", dest="operation", action="store_const", const="status",
                        help="show a concise sanitized summary for one exact run")
    action.add_argument("--cleanup", dest="operation", action="store_const", const="cleanup",
                        help="clean only assets in this run's ownership manifest")
    action.add_argument("--record-observation", dest="operation", action="store_const", const="observation",
                        help="record one explicit owner observation against a fixed gate checklist")
    parser.add_argument("--stage", choices=STAGE_ORDER, help="exact staged campaign step")
    parser.add_argument("--approved-total-minutes", type=int,
                        help=f"owner-reviewed campaign time ceiling (1–{MAX_APPROVED_TOTAL_MINUTES} minutes)")
    parser.add_argument("--retry-of", help="exact prior run ID for the single diagnosed fresh retry")
    parser.add_argument("--retry-reason", help="exact operational reason_code in the prior report")
    parser.add_argument("--confirm-run-id", help="repeat the exact planned ID to authorize execution/cleanup")
    parser.add_argument("--observation-gate", choices=tuple(OWNER_CHECKLISTS))
    parser.add_argument("--observation-result", choices=("PASS", "FAIL", "BLOCKED"))
    parser.add_argument("--observed-version", help="OpenCode version actually observed by the owner")
    parser.add_argument("--observation-check", action="append", default=[],
                        help="completed fixed checklist ID (repeat for each observed item)")
    args = parser.parse_args(argv)

    try:
        if args.dev_step is not None:
            if (not args.source or not args.state_root or not args.profile or not args.run_id
                    or args.operation is not None or args.suite or args.stage or args.discover_providers
                    or args.provider_config or args.apply_selected_updates or args.memory_gib is not None
                    or args.approved_total_minutes is not None or args.retry_of or args.retry_reason
                    or args.confirm_run_id or args.observation_gate or args.observation_result
                    or args.observed_version or args.observation_check):
                parser.error("--dev-step requires --source, --state-root, --profile and --run-id; campaign/discovery flags are mutually exclusive")
            source = _canonical_directory(args.source)
            raw_state_root = Path(args.state_root)
            if not raw_state_root.is_absolute() or ".." in raw_state_root.parts:
                parser.error("--state-root must be an absolute exact path without traversal")
            if PROFILE_NAME_RE.fullmatch(args.profile) is None:
                parser.error("--profile must be one exact valid profile name")
            if RUN_ID_RE.fullmatch(args.run_id) is None:
                parser.error("--run-id must be 32 lowercase hexadecimal characters")
            report = run_dev_step(source, raw_state_root, args.profile, args.run_id, args.dev_step,
                                  dev_execute=args.dev_execute, dev_report=args.dev_report)
            print(json.dumps(report, sort_keys=True))
            return 0 if report["result"] == "PASS" else 1 if report["result"] == "FAIL" else 2
        if args.dev_execute or args.dev_report:
            parser.error("--dev-execute and --dev-report require --dev-step")
        if args.discover_providers:
            if (args.source or args.provider_config or args.operation is not None or args.suite
                    or args.run_id or args.stage or args.retry_of or args.retry_reason
                    or args.confirm_run_id or args.approved_total_minutes is not None
                    or args.memory_gib is not None
                    or args.apply_selected_updates or args.observation_gate or args.observation_result
                    or args.observed_version or args.observation_check
                    or args.profile is None or args.state_root is None):
                parser.error("--discover-providers requires only --state-root and exact --profile")
            state_root = _canonical_directory(args.state_root)
            print(json.dumps(discover_profile_topology(state_root, args.profile), sort_keys=True))
            return 0
        if args.profile is not None:
            parser.error("--profile is used only with --discover-providers")
        memory_gib = 8 if args.memory_gib is None else args.memory_gib
        if not args.state_root:
            parser.error("--state-root is required")
        state_root = _canonical_directory(args.state_root)
        run_id = _run_id(args.run_id)
        if args.operation == "status":
            result = campaign_status(state_root, run_id)
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.operation == "cleanup":
            if args.approved_total_minutes is None:
                parser.error("--cleanup requires the original --approved-total-minutes ceiling")
            if args.stage:
                parser.error("--cleanup is the teardown stage; do not also supply --stage")
            with _campaign_lock(state_root, run_id):
                report, report_path = execute_teardown_stage(
                    state_root, run_id, args.confirm_run_id or "", args.approved_total_minutes,
                )
            result = {"run_id": run_id, "cleanup": report.get("cleanup", {}).get("status", "BLOCKED"),
                      "stage_status": report.get("stages", {}).get("teardown", "BLOCKED"),
                      "report_path": str(report_path)}
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.operation == "observation":
            if not args.observation_gate or not args.observation_result or not args.observed_version \
                    or not args.observation_check:
                parser.error("--record-observation requires a gate, result, version and one or more --observation-check IDs")
            with _campaign_lock(state_root, run_id):
                result = record_human_observation(
                    state_root, run_id, args.confirm_run_id or "", args.observation_gate,
                    args.observation_result, args.observed_version, args.observation_check,
                )
            print(json.dumps(result, sort_keys=True))
            return 0 if result["campaign_result"] == "PASS" else 1 if result["campaign_result"] == "FAIL" else 2
        if not args.source:
            parser.error("--source is required for a plan or campaign")
        if not args.suite:
            parser.error("at least one --suite is required for a plan or campaign")
        source = _canonical_directory(args.source)
        suites = list(ALL_SUITES) if "all" in args.suite else list(dict.fromkeys(args.suite))
        if args.apply_selected_updates and "lifecycle" not in suites:
            parser.error("--apply-selected-updates requires --suite lifecycle")
        if args.operation == "execute":
            parser.error("one-shot execution is disabled; use --start/--resume staged campaign commands")
        if args.operation in {"start", "resume"}:
            if not args.stage:
                parser.error("--start/--resume requires --stage")
            if not args.confirm_run_id:
                parser.error("--start/--resume requires --confirm-run-id with the exact run ID")
            if args.approved_total_minutes is None:
                parser.error("--start/--resume requires the explicitly approved --approved-total-minutes ceiling")
            if args.operation == "resume" and (args.retry_of or args.retry_reason):
                parser.error("--retry-of/--retry-reason are valid only with --start")
            if args.operation == "start":
                start = True
            else:
                start = False
            with _campaign_lock(state_root, run_id):
                report, report_path = execute_stage(
                    source, state_root, run_id, suites, memory_gib, args.confirm_run_id,
                    args.stage, start=start, apply_selected_updates=args.apply_selected_updates,
                    provider_config_path=args.provider_config,
                    approved_total_minutes=args.approved_total_minutes,
                    retry_of=args.retry_of, retry_reason=args.retry_reason,
                )
            summary = {"run_id": run_id, "result": report.get("result", "BLOCKED"),
                       "stage": args.stage,
                       "stage_status": report.get("stages", {}).get(args.stage, "BLOCKED"),
                       "report_path": str(report_path) if report_path != Path() else None,
                       "gates": {name: item.get("status") for name, item in report.get("gates", {}).items()
                                 if name in report.get("required_gates", [])}}
            print(json.dumps(summary, sort_keys=True))
            return 0 if summary["stage_status"] == "PASS" else 1 if summary["stage_status"] == "FAIL" else 2
        if args.stage:
            parser.error("--stage is used only with --start or --resume")
        if args.retry_of or args.retry_reason:
            parser.error("retry options are valid only with --start")
        if args.approved_total_minutes is not None:
            parser.error("--approved-total-minutes is used only with live execution")
        plan = preflight(source, state_root, run_id, memory_gib, suites, args.provider_config)
        plan["suites"] = suites
        plan["requested_effects"] = {
            "selected_guest_update_targets": ["config", "tools"] if args.apply_selected_updates else [],
            "selected_guest_updates_authorized": args.apply_selected_updates,
        }
        plan["assets"] = {
            "run_name": _run_name(run_id),
            "profiles": [_profile_name(run_id)] if any(s != "preflight" for s in suites) else [],
            "snapshot": "candidate-snapshot/workspace",
            "private_state": "state/",
            "report": f"reports/{_run_name(run_id)}.json",
        }
        plan["protected"] = ["existing/default profiles", "shared VM bases", "host Docker/auth session", "credentials"]
        print(json.dumps(plan, sort_keys=True))
        return 0 if plan["result"] == "PASS" else 2
    except KeyboardInterrupt:
        print("BLOCKED: campaign interrupted; run-owned report and uncertain assets are preserved.", file=sys.stderr)
        return 130
    except (AcceptanceError, OSError, RuntimeError, TimeoutError, ValueError, KeyError) as error:
        print(f"BLOCKED: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
