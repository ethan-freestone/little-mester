#!/usr/bin/env python3
"""Credential-free hosted-KVM regression through the singleton public CLI.

This is CI orchestration, not an installed/runtime asset. Input is sent only
through the controlling host PTY of ``<checkout>/lab vm system tui``. A second
SSH channel uses the installed candidate's bounded guest-SSH helper strictly
for read-only tmux observations; it never sends terminal input or repairs
guest state.
"""

from __future__ import annotations

import grp
import json
import os
import pathlib
import platform
import pwd
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_INSTALL_PREFIX = pathlib.Path("/srv/little-mester-ci")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
SESSION_NAME = "little-mester-opencode"
FAST_ESCAPE_TRIALS = 10
FAST_ESCAPE_GAP_SECONDS = 0.030
FAST_ESCAPE_MAX_NS = 35_000_000
REATTACH_ATTEMPTS = 3
ATTACH_TIMEOUT_SECONDS = 90
OBSERVATION_TIMEOUT_SECONDS = 15
CANCEL_OBSERVATION_TIMEOUT_SECONDS = 0.75
EVIDENCE_MAX_BYTES = 64 * 1024
EVIDENCE_MAX_RECORDS = 256
EVIDENCE_MAX_LINE = 4096
DIALOG_TEXT = "Close Little Mester?"
PROMPT_TEXT = "Ask anything"


class TerminalGateError(RuntimeError):
    """A required production-terminal assertion failed or was unavailable."""


_CREDENTIAL_CHECKS = frozenset({
    "control_group_lookup", "control_group_id", "effective_control_group",
    "effective_runner_uid",
    "runtime_directory_lstat", "runtime_directory_metadata",
    "control_key_lstat", "control_key_metadata",
    "known_hosts_lstat", "known_hosts_metadata",
    "host_tool_git", "host_tool_sg", "host_tool_ssh", "host_tool_sudo",
    "host_tool_systemctl", "host_tool_infocmp", "host_tool_python3",
    "xterm_terminfo_probe", "xterm_terminfo_entry", "python_pexpect",
})


class CredentialPreflightError(TerminalGateError):
    """A fixed, secret-free credential preflight subcheck failed."""

    def __init__(self, check: str):
        if not isinstance(check, str) or check not in _CREDENTIAL_CHECKS:
            raise TerminalGateError("invalid credential diagnostic code")
        self.check = check
        super().__init__("credential preflight failed")


class GateStop(Exception):
    """Stop unsafe dependent actions while retaining required skipped checks."""


def _check_ids() -> tuple[str, ...]:
    ids = [
        "hosted_prerequisites", "candidate_sha", "installed_candidate", "credentials",
        "system_vm_active", "guest_prelaunch_state", "public_cli_attach",
        "baseline_state_identity", "tmux_escape_time", "tmux_status",
        "host_pty_resize_1", "host_pty_resize_2", "idle_ctrl_c_opens_modal",
        "modal_ctrl_c_cancels", "reattach_recovery", "post_reattach_ctrl_c_opens_modal",
        "post_reattach_ctrl_c_cancels", "q_confirm_opens_modal", "q_clean_cli_exit",
        "session_cleanup",
    ]
    for trial in range(1, FAST_ESCAPE_TRIALS + 1):
        ids.extend((f"fast_escape_timing_{trial:02d}",
                    f"fast_escape_cancel_{trial:02d}"))
    ids.append("reattach_initial_detach")
    for attempt in range(1, REATTACH_ATTEMPTS + 1):
        ids.extend((f"reattach_same_pane_{attempt:02d}",
                    f"reattach_public_cli_{attempt:02d}"))
        if attempt < REATTACH_ATTEMPTS:
            ids.append(f"reattach_batch_detach_{attempt:02d}")
    return tuple(ids)


REQUIRED_CHECKS = _check_ids()


class EvidenceLedger:
    """Write a bounded, private record containing only sanitized observations."""

    def __init__(self, directory: pathlib.Path, candidate_sha: str):
        self.directory = directory
        self.path = directory / "production-terminal-evidence.jsonl"
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.stream = os.fdopen(fd, "w", encoding="utf-8")
        self.count = 0
        self.bytes_written = 0
        self.completed: set[str] = set()
        self.first_failure: str | None = None
        self.record("run", "start", candidate_sha=candidate_sha)

    def record(self, kind: str, name: str, **details) -> None:
        entry = {"sequence": self.count + 1, "kind": kind, "name": name, **details}
        line = json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n"
        size = len(line.encode("utf-8"))
        if (self.count >= EVIDENCE_MAX_RECORDS or size > EVIDENCE_MAX_LINE
                or self.bytes_written + size > EVIDENCE_MAX_BYTES):
            raise TerminalGateError("private terminal evidence limit exceeded")
        self.stream.write(line)
        self.stream.flush()
        self.count += 1
        self.bytes_written += size

    def check(self, name: str, passed: bool, **details) -> bool:
        if name not in REQUIRED_CHECKS or name in self.completed:
            raise TerminalGateError("invalid or repeated required terminal check")
        self.completed.add(name)
        passed = bool(passed)
        self.record("check", name, status="PASS" if passed else "FAIL", **details)
        if not passed and self.first_failure is None:
            self.first_failure = name
        print(f"production_terminal_check={'PASS' if passed else 'FAIL'} check={name}",
              flush=True)
        return passed

    def skip_unattempted(self) -> None:
        reason = self.first_failure or "required prerequisite unavailable"
        for name in REQUIRED_CHECKS:
            if name in self.completed:
                continue
            self.completed.add(name)
            self.record("check", name, status="SKIP", required=True,
                        reason=f"unattempted after {reason}")
            print(f"production_terminal_check=SKIP check={name} required=true "
                  f"after={reason}", flush=True)

    def close(self) -> None:
        if not self.stream.closed:
            self.stream.close()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise TerminalGateError(message)


def _run(argv: list[str], *, timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=timeout, check=False)


def verify_host_terminal_tools() -> dict:
    """Check predictable terminal-gate host requirements without VM credentials."""
    tools = (
        ("host_tool_git", "/usr/bin/git"), ("host_tool_sg", "/usr/bin/sg"),
        ("host_tool_ssh", "/usr/bin/ssh"), ("host_tool_sudo", "/usr/bin/sudo"),
        ("host_tool_systemctl", "/usr/bin/systemctl"),
        ("host_tool_infocmp", "/usr/bin/infocmp"),
        ("host_tool_python3", "/usr/bin/python3"),
    )
    for check, tool in tools:
        if not (os.path.isfile(tool) and os.access(tool, os.X_OK)):
            raise CredentialPreflightError(check)
    try:
        terminfo = _run(["/usr/bin/infocmp", "-1", "xterm-256color"])
    except (OSError, subprocess.SubprocessError):
        raise CredentialPreflightError("xterm_terminfo_probe") from None
    if terminfo.returncode != 0:
        raise CredentialPreflightError("xterm_terminfo_entry")
    try:
        import pexpect  # noqa: F401
    except ImportError:
        raise CredentialPreflightError("python_pexpect") from None
    return {"host_tools": "available", "host_terminal": "xterm-256color"}


def verify_hosted_prerequisites(repo: pathlib.Path, env: dict[str, str] | None = None) -> dict:
    """Reject local runs, non-hosted runners, and missing disposable KVM."""
    env = os.environ if env is None else env
    _require(env.get("GITHUB_ACTIONS") == "true", "GitHub Actions is required")
    _require(env.get("RUNNER_ENVIRONMENT") == "github-hosted",
             "a GitHub-hosted runner is required")
    _require(env.get("RUNNER_OS") == "Linux" and env.get("RUNNER_ARCH") == "X64"
             and platform.machine() == "x86_64", "ubuntu-24.04 Linux x64 is required")
    workspace = env.get("GITHUB_WORKSPACE", "")
    _require(bool(workspace) and pathlib.Path(workspace).resolve() == repo.resolve(),
             "the candidate checkout must be the GitHub workspace")
    _require(pathlib.Path("/dev/kvm").is_char_device()
             and os.access("/dev/kvm", os.R_OK | os.W_OK),
             "read/write hosted KVM is required")
    return {"runner": "github-hosted-ubuntu-24.04-x64", "kvm": "read-write"}


def verify_candidate(repo: pathlib.Path, candidate_sha: str,
                     env: dict[str, str] | None = None) -> dict:
    """Independently bind the checked-out CLI bytes to the exact workflow SHA."""
    env = os.environ if env is None else env
    _require(SHA_RE.fullmatch(candidate_sha) is not None,
             "candidate SHA must be a full commit SHA")
    _require(env.get("GITHUB_SHA") == candidate_sha,
             "candidate SHA differs from workflow GITHUB_SHA")
    head = _run(["/usr/bin/git", "-C", str(repo), "rev-parse", "--verify", "HEAD"])
    _require(head.returncode == 0 and head.stdout.decode("ascii", "ignore").strip()
             == candidate_sha, "checkout HEAD does not match candidate SHA")
    status = _run(["/usr/bin/git", "-C", str(repo), "status", "--porcelain",
                   "--untracked-files=all"])
    _require(status.returncode == 0 and not status.stdout,
             "candidate checkout is not clean")
    for relative in ("lab", "tools/vm-owner-acceptance"):
        source = repo / relative
        _require(source.is_file() and os.access(source, os.X_OK),
                 "candidate public CLI or owner acceptance runner is unavailable")
        object_bytes = _run(["/usr/bin/git", "-C", str(repo), "show",
                             f"{candidate_sha}:{relative}"])
        _require(object_bytes.returncode == 0 and object_bytes.stdout == source.read_bytes(),
                 "candidate public CLI bytes differ from the verified Git object")
    return {"candidate_sha": candidate_sha, "checkout_head": candidate_sha,
            "checkout_clean": True}


def _verify_root_owned_path(path: pathlib.Path, *, final_mode: int | None = None,
                            final_directory: bool = False) -> None:
    current = pathlib.Path(path.anchor)
    for index, component in enumerate(path.parts[1:]):
        current = current / component
        info = os.lstat(current)
        final = index == len(path.parts[1:]) - 1
        _require(not stat.S_ISLNK(info.st_mode) and info.st_uid == 0
                 and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH),
                 "installed candidate path contains an unsafe component")
        if final:
            expected_type = stat.S_ISDIR(info.st_mode) if final_directory else stat.S_ISREG(info.st_mode)
            _require(expected_type and (final_mode is None
                                        or stat.S_IMODE(info.st_mode) == final_mode),
                     "installed candidate path has an unexpected final type or mode")
        else:
            _require(stat.S_ISDIR(info.st_mode),
                     "installed candidate path contains a non-directory component")


def _protected_candidate_file(path: pathlib.Path, expected_mode: int,
                              relative: str, repo: pathlib.Path, sha: str) -> None:
    _verify_root_owned_path(path, final_mode=expected_mode)
    info = os.lstat(path)
    _require(info.st_nlink == 1, "installed candidate file has an unsafe link count")
    expected = _run(["/usr/bin/git", "-C", str(repo), "show", f"{sha}:{relative}"])
    _require(expected.returncode == 0 and expected.stdout == path.read_bytes(),
             "installed candidate file differs from its verified Git object")


def verify_installed_candidate(repo: pathlib.Path, prefix: pathlib.Path,
                               candidate_sha: str) -> pathlib.Path:
    """Check the exact installed TUI dispatch chain before using it to observe."""
    _require(prefix.is_absolute() and prefix != pathlib.Path("/")
             and ".." not in prefix.parts and str(prefix) == os.path.normpath(str(prefix)),
             "install prefix is not an absolute normalized path")
    root = prefix / "v3" / candidate_sha
    for path in (prefix, prefix / "v3", root):
        _verify_root_owned_path(path, final_directory=True)
    for relative, mode in (("tools/vm-owner-acceptance", 0o755),
                           ("lab-vm", 0o755), ("lib/vm/system-vm.py", 0o644)):
        _protected_candidate_file(root / relative, mode, relative, repo, candidate_sha)
    return root


def verify_credentials_and_host_tools() -> dict:
    """Validate protected SSH access in the authorized group context without reading keys."""
    uid = os.getuid()
    try:
        group = grp.getgrnam("lm-vm-control")
    except (KeyError, OSError):
        raise CredentialPreflightError("control_group_lookup") from None
    if group.gr_gid <= 0:
        raise CredentialPreflightError("control_group_id")
    if os.geteuid() != uid:
        raise CredentialPreflightError("effective_runner_uid")
    try:
        effective_groups = os.getgroups()
        effective_gid = os.getegid()
    except OSError:
        raise CredentialPreflightError("effective_control_group") from None
    if group.gr_gid not in effective_groups and group.gr_gid != effective_gid:
        raise CredentialPreflightError("effective_control_group")
    runtime = pathlib.Path(f"/run/user/{uid}")
    try:
        info = os.lstat(runtime)
    except OSError:
        raise CredentialPreflightError("runtime_directory_lstat") from None
    if not (stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
            and info.st_uid == uid and stat.S_IMODE(info.st_mode) == 0o700):
        raise CredentialPreflightError("runtime_directory_metadata")
    assets = (
        ("control_key", pathlib.Path("/etc/little-mester-vm/control_ed25519"),
         0, group.gr_gid, 0o640),
        ("known_hosts", pathlib.Path("/etc/little-mester-vm/known_hosts"),
         0, 0, 0o644),
    )
    for name, path, owner, gid, mode in assets:
        try:
            info = os.lstat(path)
        except OSError:
            raise CredentialPreflightError(f"{name}_lstat") from None
        if not (stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
                and info.st_uid == owner and info.st_gid == gid
                and stat.S_IMODE(info.st_mode) == mode and info.st_nlink == 1):
            raise CredentialPreflightError(f"{name}_metadata")
    host = verify_host_terminal_tools()
    return {"credential_files": "metadata-verified-not-read", **host}


def _clean_tui_environment() -> dict[str, str]:
    """Do not forward CI tokens, OpenCode credentials, or caller shell state."""
    home = pwd.getpwuid(os.getuid()).pw_dir
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": home,
            "TERM": "xterm-256color", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}


def public_cli_command(repo: pathlib.Path, prefix: pathlib.Path) -> list[str]:
    return [str(repo / "lab"), "vm", "system", "tui", "--prefix", str(prefix)]


class HostPtyClient:
    """One real public-CLI invocation attached to a controlling host PTY."""

    def __init__(self, repo: pathlib.Path, prefix: pathlib.Path):
        import pexpect

        self.pexpect = pexpect
        self.command = public_cli_command(repo, prefix)
        shell_command = "exec " + shlex.join(self.command)
        self.child = pexpect.spawn(
            "/usr/bin/sg", ["lm-vm-control", "-c", shell_command],
            cwd=str(repo), env=_clean_tui_environment(), encoding=None,
            dimensions=(32, 100), timeout=30, maxread=4096,
        )

    def alive(self) -> bool:
        return self.child is not None and self.child.isalive()

    def drain(self) -> None:
        if self.child is None:
            return
        while True:
            try:
                self.child.read_nonblocking(size=8192, timeout=0)
            except self.pexpect.TIMEOUT:
                return
            except self.pexpect.EOF:
                return

    def send_control(self, key: str) -> None:
        _require(self.alive(), "public TUI host PTY is not live")
        self.child.sendcontrol(key)

    def send_bytes(self, value: bytes) -> None:
        _require(self.alive(), "public TUI host PTY is not live")
        self.child.send(value)

    def send_fast_escape_n(self) -> int:
        """Measure only host-PTY writes and the requested 30 ms gap."""
        _require(self.alive(), "public TUI host PTY is not live")
        previous_delay = self.child.delaybeforesend
        try:
            self.child.delaybeforesend = None
            started = time.monotonic_ns()
            self.child.send(b"\x1b")
            time.sleep(FAST_ESCAPE_GAP_SECONDS)
            self.child.send(b"n")
            elapsed = time.monotonic_ns() - started
        finally:
            self.child.delaybeforesend = previous_delay
        return elapsed

    def resize(self, rows: int, columns: int) -> None:
        _require(self.alive(), "public TUI host PTY is not live")
        self.child.setwinsize(rows, columns)

    def wait_exit(self, timeout: float) -> tuple[int | None, int | None]:
        if self.child is None:
            return None, None
        try:
            self.child.expect(self.pexpect.EOF, timeout=timeout)
        except self.pexpect.TIMEOUT as error:
            raise TerminalGateError("public TUI command did not exit within its bound") from error
        self.child.close()
        return self.child.exitstatus, self.child.signalstatus

    def close(self) -> None:
        if self.child is not None:
            if self.child.isalive():
                self.child.close(force=True)
            else:
                self.child.close()
            self.child = None


REMOTE_OBSERVER = r'''import json, os, re, subprocess

env = {"PATH": "/usr/bin:/bin", "HOME": "/home/mester",
       "TERM": "xterm-256color",
       "TMUX_TMPDIR": "/home/mester/.local/share/opencode/tmux"}
tmux = ["/usr/bin/tmux", "-f", "/etc/little-mester-tmux.conf"]

def run(args, allowed=(0,)):
    result = subprocess.run(tmux + args, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, timeout=4, check=False)
    if result.returncode not in allowed:
        raise RuntimeError("read-only tmux observation failed")
    return result.stdout.strip()

def records(output, fields, casts=()):
    result = []
    for line in output.splitlines():
        values = line.split("\t")
        if len(values) != len(fields):
            raise RuntimeError("malformed tmux observation")
        item = dict(zip(fields, values))
        for name, cast in casts:
            item[name] = cast(item[name])
        result.append(item)
    return result

try:
    sessions_output = run(["list-sessions", "-F",
                           "#{session_id}\t#{session_name}\t#{session_windows}"],
                          allowed=(0, 1))
    if not sessions_output:
        print(json.dumps({"server_available": False, "sessions": [], "panes": [],
                          "clients": [], "escape_time": None, "global_status": None,
                          "session_status": None, "dialog_visible": False,
                          "idle_prompt_visible": False}, sort_keys=True))
        raise SystemExit(0)
    sessions = records(sessions_output,
                       ("session_id", "session_name", "windows"),
                       (("windows", int),))
    pane_output = run(["list-panes", "-a", "-F",
                       "#{session_id}\t#{session_name}\t#{window_id}\t#{pane_id}\t"
                       "#{pane_pid}\t#{pane_dead}\t#{pane_width}\t#{pane_height}"],
                      allowed=(0, 1))
    panes = records(pane_output,
                    ("session_id", "session_name", "window_id", "pane_id", "pid",
                     "dead", "width", "height"),
                    (("pid", int), ("width", int), ("height", int)))
    target = [item for item in sessions if item["session_name"] == "little-mester-opencode"]
    target_panes = [item for item in panes if item["session_name"] == "little-mester-opencode"]
    clients = []
    if target:
        client_output = run(["list-clients", "-t", "little-mester-opencode", "-F",
                             "#{client_session}\t#{client_width}\t#{client_height}"],
                            allowed=(0, 1))
        clients = records(client_output, ("session_name", "width", "height"),
                          (("width", int), ("height", int)))
    escape_time = run(["show-options", "-s", "escape-time"])
    global_status = run(["show-options", "-g", "-v", "status"])
    session_status = (run(["show-options", "-A", "-v", "-t",
                           "little-mester-opencode", "status"])
                      if target else None)
    dialog_visible = False
    idle_prompt_visible = False
    if len(target_panes) == 1:
        pane_id = target_panes[0]["pane_id"]
        if not re.fullmatch(r"%[0-9]+", pane_id):
            raise RuntimeError("invalid pane identity")
        screen = run(["capture-pane", "-p", "-t", pane_id])
        dialog_visible = "Close Little Mester?" in screen
        idle_prompt_visible = "Ask anything" in screen
    print(json.dumps({"server_available": True, "sessions": sessions, "panes": panes,
                      "clients": clients, "escape_time": escape_time,
                      "global_status": global_status, "session_status": session_status,
                      "dialog_visible": dialog_visible,
                      "idle_prompt_visible": idle_prompt_visible}, sort_keys=True))
except (OSError, subprocess.SubprocessError, ValueError, RuntimeError):
    raise SystemExit(42)
'''


OBSERVATION_WRAPPER = r'''import importlib.util, json, pathlib, shlex, sys

root = pathlib.Path(sys.argv[1])
path = root / "lib/vm/system-vm.py"
spec = importlib.util.spec_from_file_location("installed_terminal_observer_vm", path)
if spec is None or spec.loader is None:
    raise SystemExit(42)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
remote = "python3 -I -c " + shlex.quote(sys.argv[2])
command = "/bin/bash -lc " + shlex.quote(remote)
status, output = module._guest_ssh(command, timeout=15, capture_limit=8192)
if status != 0:
    raise SystemExit(43)
sys.stdout.buffer.write(output)
'''


class ReadOnlyGuestObserver:
    """Use only the installed fixed SSH helper and non-mutating tmux queries."""

    def __init__(self, installed_root: pathlib.Path):
        self.installed_root = installed_root
        self.env = _clean_tui_environment()

    def observe(self) -> dict:
        argv = ["/usr/bin/python3", "-I", "-c", OBSERVATION_WRAPPER,
                str(self.installed_root), REMOTE_OBSERVER]
        command = "exec " + shlex.join(argv)
        result = subprocess.run(
            ["/usr/bin/sg", "lm-vm-control", "-c", command],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=self.env, timeout=OBSERVATION_TIMEOUT_SECONDS + 10, check=False,
        )
        _require(result.returncode == 0 and len(result.stdout) <= 8192,
                 "bounded read-only guest observation failed")
        try:
            observation = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as error:
            raise TerminalGateError("guest observer returned malformed evidence") from error
        _validate_observation(observation)
        return observation


def _validate_observation(value: object) -> None:
    _require(isinstance(value, dict)
             and set(value) == {"server_available", "sessions", "panes", "clients",
                                "escape_time", "global_status", "session_status",
                                "dialog_visible", "idle_prompt_visible"},
             "guest observer returned an unexpected evidence shape")
    _require(type(value["server_available"]) is bool
             and isinstance(value["sessions"], list) and isinstance(value["panes"], list)
             and isinstance(value["clients"], list)
             and type(value["dialog_visible"]) is bool
             and type(value["idle_prompt_visible"]) is bool,
             "guest observer returned invalid state types")
    for session in value["sessions"]:
        _require(set(session) == {"session_id", "session_name", "windows"}
                 and re.fullmatch(r"\$[0-9]+", str(session["session_id"])) is not None
                 and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(session["session_name"])) is not None
                 and type(session["windows"]) is int and session["windows"] >= 0,
                 "guest observer returned invalid session identity")
    for pane in value["panes"]:
        _require(set(pane) == {"session_id", "session_name", "window_id", "pane_id",
                               "pid", "dead", "width", "height"}
                 and re.fullmatch(r"\$[0-9]+", str(pane["session_id"])) is not None
                 and re.fullmatch(r"@[0-9]+", str(pane["window_id"])) is not None
                 and re.fullmatch(r"%[0-9]+", str(pane["pane_id"])) is not None
                 and type(pane["pid"]) is int and pane["pid"] > 1
                 and pane["dead"] in ("0", "1")
                 and type(pane["width"]) is int and type(pane["height"]) is int,
                 "guest observer returned invalid pane identity")
    for client in value["clients"]:
        _require(set(client) == {"session_name", "width", "height"}
                 and client["session_name"] == SESSION_NAME
                 and type(client["width"]) is int and type(client["height"]) is int,
                 "guest observer returned invalid attached-client state")


def _target_session(observation: dict) -> dict | None:
    matches = [item for item in observation["sessions"]
               if item["session_name"] == SESSION_NAME]
    return matches[0] if len(matches) == 1 else None


def _target_pane(observation: dict) -> dict | None:
    matches = [item for item in observation["panes"]
               if item["session_name"] == SESSION_NAME]
    return matches[0] if len(matches) == 1 else None


def _identity(observation: dict) -> dict | None:
    session, pane = _target_session(observation), _target_pane(observation)
    if session is None or pane is None:
        return None
    if pane["session_id"] != session["session_id"]:
        return None
    return {"session_id": session["session_id"], "session_name": session["session_name"],
            "pane_id": pane["pane_id"], "pane_pid": pane["pid"]}


def _same_live_identity(observation: dict, expected: dict) -> bool:
    pane = _target_pane(observation)
    return (len(observation["sessions"]) == 1 and len(observation["panes"]) == 1
            and _identity(observation) == expected and pane is not None and pane["dead"] == "0")


class ProductionTerminalGate:
    def __init__(self, repo: pathlib.Path, prefix: pathlib.Path, candidate_sha: str,
                 ledger: EvidenceLedger, *, env: dict[str, str] | None = None,
                 observer_factory=ReadOnlyGuestObserver, client_factory=HostPtyClient):
        self.repo = repo
        self.prefix = prefix
        self.candidate_sha = candidate_sha
        self.ledger = ledger
        self.env = os.environ if env is None else env
        self.observer_factory = observer_factory
        self.client_factory = client_factory
        self.observer: ReadOnlyGuestObserver | None = None
        self.client: HostPtyClient | None = None
        self.identity: dict | None = None

    def _must(self, name: str, operation):
        try:
            details = operation()
            if details is None:
                details = {}
            _require(isinstance(details, dict), "invalid terminal check result")
        except CredentialPreflightError as error:
            self.ledger.check(name, False, reason=error.check)
            print(f"production_terminal_credential=FAIL check={error.check}", flush=True)
            raise GateStop(name) from None
        except Exception as error:  # noqa: BLE001 - only exception class enters private evidence
            self.ledger.check(name, False, reason=type(error).__name__)
            raise GateStop(name) from None
        self.ledger.check(name, True, **details)
        return details

    def _check(self, name: str, passed: bool, **details) -> bool:
        return self.ledger.check(name, passed, **details)

    def _observe(self) -> dict:
        _require(self.observer is not None, "read-only guest observer is unavailable")
        if self.client is not None:
            self.client.drain()
        return self.observer.observe()

    def _wait_for(self, predicate, timeout: float = 8.0) -> dict:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self._observe()
            if predicate(last):
                return last
            if self.client is not None and not self.client.alive():
                break
            time.sleep(0.1)
        raise TerminalGateError("guest TUI state did not reach its bounded expectation")

    def _attached(self, observation: dict, expected: dict | None = None) -> bool:
        identity = _identity(observation)
        pane = _target_pane(observation)
        return (identity is not None and (expected is None or identity == expected)
                and _same_live_identity(observation, identity)
                and len(observation["clients"]) == 1 and pane is not None
                and observation["clients"][0]["session_name"] == SESSION_NAME)

    def _attach_client(self) -> HostPtyClient:
        client = self.client_factory(self.repo, self.prefix)
        self.client = client
        return client

    def _wait_attached(self, expected: dict | None = None,
                       timeout: float = ATTACH_TIMEOUT_SECONDS) -> dict:
        observation = self._wait_for(
            lambda item: self._attached(item, expected), timeout=timeout)
        _require(self.client is not None and self.client.alive(),
                 "public CLI exited before the guest PTY attached")
        return observation

    def _send_ctrl_c_and_wait(self, open_dialog: bool) -> dict:
        _require(self.client is not None and self.client.alive(),
                 "public CLI host PTY is not live")
        self.client.send_control("c")
        return self._wait_for(
            lambda item: self._attached(item, self.identity)
            and item["dialog_visible"] is open_dialog,
            timeout=8,
        )

    def _ensure_dialog_open(self) -> dict:
        observation = self._observe()
        _require(self._attached(observation, self.identity),
                 "guest TUI identity is unavailable before Escape+n trial")
        if not observation["dialog_visible"]:
            observation = self._send_ctrl_c_and_wait(open_dialog=True)
        _require(observation["dialog_visible"], "modal precondition is unavailable")
        return observation

    def _skip_trials_from(self, trial: int, reason: str) -> None:
        for skipped in range(trial, FAST_ESCAPE_TRIALS + 1):
            for check_name in (f"fast_escape_timing_{skipped:02d}",
                               f"fast_escape_cancel_{skipped:02d}"):
                if check_name not in self.ledger.completed:
                    self._check(check_name, False, required_unattempted=True,
                                reason=reason)

    def _run_fast_escape_trials(self) -> None:
        _require(self.client is not None, "public CLI host PTY is unavailable")
        attempted = 0
        cancelled = 0
        for trial in range(1, FAST_ESCAPE_TRIALS + 1):
            try:
                self._ensure_dialog_open()
            except Exception as error:  # noqa: BLE001 - remaining checks are required failures
                reason = type(error).__name__
                self._skip_trials_from(trial, reason)
                break
            pair_written = False
            elapsed_ns = None
            try:
                elapsed_ns = self.client.send_fast_escape_n()
                attempted += 1
                pair_written = True
                timing_valid = elapsed_ns <= FAST_ESCAPE_MAX_NS
                self._check(f"fast_escape_timing_{trial:02d}", timing_valid,
                            requested_gap_ns=30_000_000, measured_ns=elapsed_ns,
                            maximum_ns=FAST_ESCAPE_MAX_NS)
            except Exception as error:  # noqa: BLE001 - still observe cancellation independently
                self._check(f"fast_escape_timing_{trial:02d}", False,
                            requested_gap_ns=30_000_000, maximum_ns=FAST_ESCAPE_MAX_NS,
                            reason=type(error).__name__)
            try:
                observation = self._observe()
                if pair_written and observation["dialog_visible"]:
                    try:
                        observation = self._wait_for(
                            lambda item: self._attached(item, self.identity)
                            and not item["dialog_visible"],
                            timeout=CANCEL_OBSERVATION_TIMEOUT_SECONDS,
                        )
                    except TerminalGateError:
                        observation = self._observe()
                live = self._attached(observation, self.identity)
                trial_cancelled = (pair_written and live
                                   and not observation["dialog_visible"])
                cancelled += int(trial_cancelled)
                self._check(f"fast_escape_cancel_{trial:02d}", trial_cancelled,
                            driver_writes_completed=pair_written,
                            modal_cancelled=trial_cancelled, pane_live=live)
                if not live:
                    self._skip_trials_from(trial + 1, "guest TUI identity unavailable")
                    break
            except Exception as error:  # noqa: BLE001 - cancellation is independent of timing
                self._check(f"fast_escape_cancel_{trial:02d}", False,
                            driver_writes_completed=pair_written, modal_cancelled=False,
                            reason=type(error).__name__)
                self._skip_trials_from(trial + 1, "guest TUI state unavailable")
                break
            print(f"production_terminal_fast_escape_trial={trial}/{FAST_ESCAPE_TRIALS} "
                  f"requested_gap_ns=30000000 measured_ns={elapsed_ns} "
                  f"timing={'PASS' if elapsed_ns is not None and elapsed_ns <= FAST_ESCAPE_MAX_NS else 'FAIL'} "
                  f"cancellation={'PASS' if trial_cancelled else 'FAIL'}", flush=True)
        self.ledger.record("batch", "fast_escape", attempted=attempted,
                           cancelled=cancelled, requested=FAST_ESCAPE_TRIALS,
                           requested_gap_ns=30_000_000, maximum_ns=FAST_ESCAPE_MAX_NS)
        print(f"production_terminal_fast_escape={cancelled}/{attempted} cancelled; "
              f"{attempted}/{FAST_ESCAPE_TRIALS} attempted", flush=True)

    def _detach(self, check_name: str) -> bool:
        if self.client is None or not self.client.alive():
            return self._check(check_name, False, reason="public CLI PTY is not live")
        try:
            self.client.send_control("b")
            self.client.send_bytes(b"d")
            exit_status, signal_status = self.client.wait_exit(timeout=15)
            self.client = None
            passed = exit_status == 0 and signal_status is None
            return self._check(check_name, passed, exit_status=exit_status,
                               signal_status=signal_status)
        except Exception as error:  # noqa: BLE001 - preserve bounded failure evidence
            if self.client is not None:
                self.client.close()
                self.client = None
            return self._check(check_name, False, reason=type(error).__name__)

    def _detached_identity(self) -> dict | None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                observation = self.observer.observe()
            except Exception:  # noqa: BLE001 - retry only this read-only observation
                observation = None
            if (observation is not None
                    and _same_live_identity(observation, self.identity)
                    and not observation["clients"]):
                return observation
            time.sleep(0.1)
        return None

    def _run_reattach_batch(self) -> None:
        _require(self.identity is not None, "baseline pane identity is unavailable")
        if not self._detach("reattach_initial_detach"):
            raise GateStop("reattach_initial_detach")
        if self._detached_identity() is None:
            self._check("reattach_same_pane_01", False,
                        reason="detached pane identity or zero-client state was not verified")
            self._skip_reattach_from(1)
            raise GateStop("reattach_same_pane_01")

        attached = False
        for attempt in range(1, REATTACH_ATTEMPTS + 1):
            same_pane = self._detached_identity()
            same_check = f"reattach_same_pane_{attempt:02d}"
            if same_pane is None:
                self._check(same_check, False,
                            reason="retry precondition did not preserve the detached pane")
                self._skip_reattach_from(attempt)
                raise GateStop(same_check)
            self._check(same_check, True, session_id=self.identity["session_id"],
                        pane_id=self.identity["pane_id"], pane_pid=self.identity["pane_pid"])
            try:
                self._attach_client()
                observation = self._wait_attached(self.identity, timeout=ATTACH_TIMEOUT_SECONDS)
                passed = _same_live_identity(observation, self.identity)
            except Exception as error:  # noqa: BLE001 - retry only after read-only safe-state proof
                passed = False
                reason = type(error).__name__
                if self.client is not None:
                    self.client.close()
                    self.client = None
            else:
                reason = ""
            attach_check = f"reattach_public_cli_{attempt:02d}"
            self._check(attach_check, passed, attempt=attempt,
                        retry_batch_size=REATTACH_ATTEMPTS,
                        session_id=self.identity["session_id"],
                        pane_id=self.identity["pane_id"],
                        pane_pid=self.identity["pane_pid"],
                        reason=reason)
            if not passed:
                safe = self._detached_identity() is not None
                if not safe:
                    self._skip_reattach_from(attempt + 1)
                    raise GateStop(attach_check)
                if attempt == REATTACH_ATTEMPTS:
                    raise GateStop(attach_check)
                continue

            attached = True
            if attempt < REATTACH_ATTEMPTS:
                detach_check = f"reattach_batch_detach_{attempt:02d}"
                if not self._detach(detach_check):
                    self._skip_reattach_from(attempt + 1)
                    raise GateStop(detach_check)
                if self._detached_identity() is None:
                    next_same = f"reattach_same_pane_{attempt + 1:02d}"
                    self._check(next_same, False,
                                reason="batch detach changed or lost the guest pane")
                    self._skip_reattach_from(attempt + 1)
                    raise GateStop(next_same)
                attached = False
        _require(attached and self.client is not None and self.client.alive(),
                 "reattach retry batch did not leave the same pane attached")

    def _skip_reattach_from(self, attempt: int) -> None:
        for index in range(attempt, REATTACH_ATTEMPTS + 1):
            for check_name in (f"reattach_same_pane_{index:02d}",
                               f"reattach_public_cli_{index:02d}"):
                if check_name not in self.ledger.completed:
                    self._check(check_name, False, required_unattempted=True,
                                reason="reattach batch stopped after an unsafe state")
            detach_name = f"reattach_batch_detach_{index:02d}"
            if (index < REATTACH_ATTEMPTS and detach_name in REQUIRED_CHECKS
                    and detach_name not in self.ledger.completed):
                self._check(detach_name, False, required_unattempted=True,
                            reason="reattach batch stopped after an unsafe state")

    def run(self) -> None:
        self._must("hosted_prerequisites",
                   lambda: verify_hosted_prerequisites(self.repo, self.env))
        self._must("candidate_sha",
                   lambda: verify_candidate(self.repo, self.candidate_sha, self.env))
        install_root = self._must(
            "installed_candidate",
            lambda: {"installed_root": str(verify_installed_candidate(
                self.repo, self.prefix, self.candidate_sha))},
        )["installed_root"]
        self._must("credentials", verify_credentials_and_host_tools)
        self._must("system_vm_active", _verify_system_vm_active)
        self.observer = self.observer_factory(pathlib.Path(install_root))

        def clean_guest_baseline():
            observation = self.observer.observe()
            _require(not observation["sessions"] and not observation["panes"],
                     "a pre-existing guest tmux session must be preserved")
            self.ledger.record("observation", "guest_prelaunch",
                               server_available=observation["server_available"],
                               session_count=0, pane_count=0)
            return {"session_count": 0, "pane_count": 0}

        self._must("guest_prelaunch_state", clean_guest_baseline)
        try:
            self._attach_client()
            first = self._wait_attached(timeout=ATTACH_TIMEOUT_SECONDS)
        except Exception as error:  # noqa: BLE001 - no alternate/local input path
            self._check("public_cli_attach", False, reason=type(error).__name__)
            raise GateStop("public_cli_attach") from None
        self._check("public_cli_attach", True,
                    command="<candidate checkout>/lab vm system tui --prefix <disposable prefix>")
        self.identity = _identity(first)
        baseline_ok = (self.identity is not None and _same_live_identity(first, self.identity)
                       and len(first["sessions"]) == 1 and len(first["panes"]) == 1
                       and len(first["clients"]) == 1 and not first["dialog_visible"]
                       and first["idle_prompt_visible"])
        self._must("baseline_state_identity", lambda: _condition_details(
            baseline_ok, "fresh idle singleton TUI and one live guest PTY",
            identity=self.identity or {}))
        self._must("tmux_escape_time", lambda: _condition_details(
            first["escape_time"] == "escape-time 10", "effective escape-time is 10 ms"))
        self._must("tmux_status", lambda: _condition_details(
            first["global_status"] == "off" and first["session_status"] == "off",
            "global and effective session status are off"))
        self._resize_check(1, 37, 101)
        self._resize_check(2, 41, 109)

        try:
            modal = self._send_ctrl_c_and_wait(open_dialog=True)
            self._check("idle_ctrl_c_opens_modal", modal["dialog_visible"],
                        idle_prompt_visible=modal["idle_prompt_visible"])
        except Exception as error:  # noqa: BLE001
            self._check("idle_ctrl_c_opens_modal", False, reason=type(error).__name__)
            raise GateStop("idle_ctrl_c_opens_modal") from None
        try:
            idle = self._send_ctrl_c_and_wait(open_dialog=False)
            self._check("modal_ctrl_c_cancels", not idle["dialog_visible"],
                        idle_prompt_visible=idle["idle_prompt_visible"])
        except Exception as error:  # noqa: BLE001
            self._check("modal_ctrl_c_cancels", False, reason=type(error).__name__)
            raise GateStop("modal_ctrl_c_cancels") from None

        self._run_fast_escape_trials()
        try:
            observation = self._observe()
            _require(self._attached(observation, self.identity),
                     "TUI identity unavailable before reattach recovery")
            if observation["dialog_visible"]:
                observation = self._send_ctrl_c_and_wait(open_dialog=False)
            self._must("reattach_recovery", lambda: _condition_details(
                self._attached(observation, self.identity) and not observation["dialog_visible"],
                "same live pane in verified idle state"))
        except GateStop:
            raise
        except Exception as error:  # noqa: BLE001
            self._check("reattach_recovery", False, reason=type(error).__name__)
            raise GateStop("reattach_recovery") from None
        try:
            self._run_reattach_batch()
        except GateStop:
            raise
        except Exception as error:  # noqa: BLE001
            self._skip_reattach_from(1)
            if "reattach_initial_detach" not in self.ledger.completed:
                self._check("reattach_initial_detach", False, reason=type(error).__name__)
            raise GateStop("reattach_initial_detach") from None

        try:
            modal = self._send_ctrl_c_and_wait(open_dialog=True)
            self._check("post_reattach_ctrl_c_opens_modal", modal["dialog_visible"],
                        same_pane=True)
        except Exception as error:  # noqa: BLE001
            self._check("post_reattach_ctrl_c_opens_modal", False,
                        reason=type(error).__name__)
            raise GateStop("post_reattach_ctrl_c_opens_modal") from None
        try:
            idle = self._send_ctrl_c_and_wait(open_dialog=False)
            self._check("post_reattach_ctrl_c_cancels", not idle["dialog_visible"],
                        same_pane=True)
        except Exception as error:  # noqa: BLE001
            self._check("post_reattach_ctrl_c_cancels", False,
                        reason=type(error).__name__)
            raise GateStop("post_reattach_ctrl_c_cancels") from None
        try:
            modal = self._send_ctrl_c_and_wait(open_dialog=True)
            self._check("q_confirm_opens_modal", modal["dialog_visible"],
                        same_pane=True)
        except Exception as error:  # noqa: BLE001
            self._check("q_confirm_opens_modal", False, reason=type(error).__name__)
            raise GateStop("q_confirm_opens_modal") from None
        try:
            _require(self.client is not None and self.client.alive(),
                     "public TUI host PTY is not live before q confirmation")
            self.client.send_bytes(b"q")
            exit_status, signal_status = self.client.wait_exit(timeout=20)
            self.client = None
            passed = exit_status == 0 and signal_status is None
            self._check("q_clean_cli_exit", passed, key="q", exit_status=exit_status,
                        signal_status=signal_status)
        except Exception as error:  # noqa: BLE001
            self._check("q_clean_cli_exit", False, key="q", reason=type(error).__name__)
            raise GateStop("q_clean_cli_exit") from None
        try:
            final = self.observer.observe()
            clean = not final["sessions"] and not final["panes"]
            self._check("session_cleanup", clean, session_count=len(final["sessions"]),
                        pane_count=len(final["panes"]), session_absent=True)
        except Exception as error:  # noqa: BLE001
            self._check("session_cleanup", False, reason=type(error).__name__)
            raise GateStop("session_cleanup") from None

    def _resize_check(self, index: int, rows: int, columns: int) -> None:
        name = f"host_pty_resize_{index}"
        try:
            _require(self.client is not None and self.client.alive(),
                     "public TUI host PTY is unavailable for resize")
            self.client.resize(rows, columns)
            observation = self._wait_for(
                lambda item: self._attached(item, self.identity)
                and item["clients"][0]["width"] == columns
                and item["clients"][0]["height"] == rows,
                timeout=8,
            )
            pane = _target_pane(observation)
            passed = (pane is not None and pane["width"] == columns
                      and pane["height"] == rows)
            self._check(name, passed, host_pty_rows=rows, host_pty_columns=columns,
                        guest_client_width=observation["clients"][0]["width"],
                        guest_client_height=observation["clients"][0]["height"],
                        guest_pane_width=pane["width"] if pane else None,
                        guest_pane_height=pane["height"] if pane else None)
        except Exception as error:  # noqa: BLE001
            self._check(name, False, host_pty_rows=rows, host_pty_columns=columns,
                        reason=type(error).__name__)
            raise GateStop(name) from None


def _condition_details(condition: bool, description: str, **details) -> dict:
    _require(condition, description)
    return details or {"assertion": description}


def _verify_system_vm_active() -> dict:
    result = _run(["/usr/bin/systemctl", "show", "--no-pager",
                   "--property=ActiveState", "--property=MainPID",
                   "little-mester-vm.service"])
    _require(result.returncode == 0, "fixed system VM state is unavailable")
    state = dict(line.split("=", 1) for line in result.stdout.decode("ascii", "ignore").splitlines()
                 if "=" in line)
    _require(state.get("ActiveState") == "active" and state.get("MainPID", "0").isdigit()
             and int(state["MainPID"]) > 1,
             "fixed system VM must be running before terminal acceptance")
    return {"unit_active": True, "main_pid_valid": True}


def _evidence_directory(env: dict[str, str]) -> pathlib.Path:
    raw = env.get("RUNNER_TEMP", "")
    _require(bool(raw), "hosted runner private temporary directory is unavailable")
    parent = pathlib.Path(raw)
    info = os.lstat(parent)
    _require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
             and info.st_uid == os.getuid(),
             "hosted runner temporary directory is not private to this runner")
    directory = pathlib.Path(tempfile.mkdtemp(prefix="lm-production-terminal-", dir=parent))
    os.chmod(directory, 0o700)
    return directory


def _remove_success_evidence(directory: pathlib.Path, ledger: EvidenceLedger) -> None:
    ledger.close()
    info = os.lstat(ledger.path)
    _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
             and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
             and info.st_size <= EVIDENCE_MAX_BYTES,
             "private terminal evidence changed before successful cleanup")
    os.unlink(ledger.path)
    info = os.lstat(directory)
    _require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
             and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700
             and not any(directory.iterdir()),
             "private terminal evidence directory changed before cleanup")
    os.rmdir(directory)


def run_production_terminal_acceptance(repo: pathlib.Path, prefix: pathlib.Path,
                                       candidate_sha: str,
                                       env: dict[str, str] | None = None) -> None:
    """Run the mandatory live gate; never downgrade a missing prerequisite to SKIP."""
    env = os.environ if env is None else env
    directory = _evidence_directory(env)
    ledger = EvidenceLedger(directory, candidate_sha)
    gate = ProductionTerminalGate(repo, prefix, candidate_sha, ledger, env=env)
    succeeded = False
    primary: BaseException | None = None
    try:
        gate.run()
        if ledger.first_failure is not None:
            raise GateStop(ledger.first_failure)
        if len(ledger.completed) != len(REQUIRED_CHECKS):
            raise GateStop("required_checks_incomplete")
        ledger.record("run", "result", status="PASS", checks=len(REQUIRED_CHECKS))
        succeeded = True
    except BaseException as error:  # noqa: BLE001 - preserve first failure over teardown
        primary = error
        if ledger.first_failure is None:
            active = next((name for name in REQUIRED_CHECKS if name not in ledger.completed), None)
            if active is not None:
                ledger.check(active, False, reason=type(error).__name__)
        ledger.skip_unattempted()
        ledger.record("run", "result", status="FAIL",
                      first_failure=ledger.first_failure or "unclassified")
    finally:
        cleanup_error = None
        try:
            if gate.client is not None:
                gate.client.close()
        except BaseException as error:  # noqa: BLE001
            cleanup_error = error
        try:
            ledger.close()
            if succeeded and cleanup_error is None:
                _remove_success_evidence(directory, ledger)
        except BaseException as error:  # noqa: BLE001 - retain the primary gate failure
            cleanup_error = cleanup_error or error
        if not succeeded or cleanup_error is not None:
            print(f"production_terminal_private_evidence={directory}", flush=True)
    if cleanup_error is not None and primary is None:
        primary = cleanup_error
        succeeded = False
    if not succeeded:
        first = (ledger.first_failure or type(primary).__name__) if primary else "unknown"
        print(f"production_terminal_gate=FAIL first_failure={first}", flush=True)
        raise TerminalGateError(f"production terminal regression failed at {first}") from None
    print("production_terminal_gate=PASS", flush=True)


def main() -> int:
    if sys.argv[1:] == ["--host-preflight"]:
        try:
            verify_host_terminal_tools()
        except CredentialPreflightError as error:
            print(f"production_terminal_preflight=FAIL check={error.check}", flush=True)
            return 1
        print("production_terminal_preflight=PASS checks=host_tools", flush=True)
        return 0
    _require(not sys.argv[1:], "unsupported argument")
    sha = os.environ.get("GITHUB_SHA", "")
    try:
        run_production_terminal_acceptance(REPO, DEFAULT_INSTALL_PREFIX, sha)
    except TerminalGateError as error:
        print(f"production_terminal_gate=FAIL reason={type(error).__name__}",
              file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
