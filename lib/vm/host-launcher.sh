#!/usr/bin/env bash
# Shared host-only policy and direct-guest smoke lifecycle.
# This file never reads workspace config or authority from environment vars.

set -euo pipefail

VM_HOST_CPU_COUNT=2
VM_HOST_MEMORY_MIB=4096
VM_HOST_DISK_GIB=24
VM_HOST_DOWNLOAD_TIMEOUT=300
VM_HOST_BOOT_TIMEOUT=300
VM_HOST_PROVIDER_TIMEOUT=300
VM_HOST_DOCKER_TIMEOUT=180
VM_HOST_CONTROL_TIMEOUT=30
VM_HOST_TOTAL_TIMEOUT=1200
VM_HOST_LOG_LIMIT_BYTES=1048576
VM_HOST_GUEST_OUTPUT_LIMIT_BYTES=65536
VM_HOST_SSH_KEY_LIMIT_BYTES=16384
VM_HOST_PROCESS_STOP_GRACE_MS=11000
VM_HOST_PROCESS_KILL_SETTLE_MS=2000
VM_HOST_MAX_RETAINED_RUNS=8
VM_HOST_WORKSPACE_MAX_BYTES=8589934592
VM_HOST_IMAGE_URL='https://cloud-images.ubuntu.com/releases/noble/release-20261017/ubuntu-24.04-server-cloudimg-amd64.img'
VM_HOST_IMAGE_SHA256='04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94'
VM_HOST_DOCKER_IMAGE='busybox:1.36.1'

vm_host_error() {
  printf 'REFUSED: %s\n' "$1" >&2
  return 1
}

vm_host_resource_limits() {
  printf '{"cpus":%s,"memory_mib":%s,"disk_gib":%s,"download_timeout_seconds":%s,"startup_timeout_seconds":%s,"provider_timeout_seconds":%s,"docker_timeout_seconds":%s,"ssh_control_timeout_seconds":%s,"total_timeout_seconds":%s}\n' \
    "$VM_HOST_CPU_COUNT" "$VM_HOST_MEMORY_MIB" "$VM_HOST_DISK_GIB" \
    "$VM_HOST_DOWNLOAD_TIMEOUT" "$VM_HOST_BOOT_TIMEOUT" "$VM_HOST_PROVIDER_TIMEOUT" \
    "$VM_HOST_DOCKER_TIMEOUT" "$VM_HOST_CONTROL_TIMEOUT" "$VM_HOST_TOTAL_TIMEOUT"
}

vm_host_qemu_network_args() {
  local ssh_port="${1:-}" proxy_port="${2:-}"
  [[ "$ssh_port" =~ ^[0-9]{1,5}$ && "$proxy_port" =~ ^[0-9]{1,5}$ ]] || return 2
  (( ssh_port >= 1024 && ssh_port <= 65535 && proxy_port >= 1024 && proxy_port <= 65535 )) || return 2
  printf 'user,id=net0,hostfwd=tcp:127.0.0.1:%s-:22,guestfwd=tcp:10.0.2.100:3128-cmd:socat - TCP:127.0.0.1:%s,restrict=on\n' "$ssh_port" "$proxy_port"
}

vm_host_require_kvm() {
  [[ -c /dev/kvm && -r /dev/kvm && -w /dev/kvm ]] || {
    vm_host_error 'host KVM is unavailable or not readable/writable at /dev/kvm; refusing TCG/nested fallback'
    return 1
  }
}

vm_host_validate_workspace() {
  local selected="${1:-}" install_root="${2:-}" state_root="${3:-}" credential_file="${4:-}"
  [[ -n "$selected" && -n "$install_root" && -n "$state_root" ]] || {
    vm_host_error 'workspace and trusted install/state roots are required'
    return 1
  }
  python3 -I - "$selected" "$install_root" "$state_root" "$credential_file" <<'PY'
import os
import pathlib
import pwd
import sys

raw, install_raw, state_raw, credentials_raw = sys.argv[1:]
uid = os.getuid()
def fail(message):
    print(f"REFUSED: {message}", file=sys.stderr)
    raise SystemExit(1)

try:
    path = pathlib.Path(raw)
    if not path.is_absolute():
        path = pathlib.Path.cwd() / path
    normalized = pathlib.Path(os.path.abspath(path))
    resolved = path.resolve(strict=True)
    if str(normalized) != str(resolved):
        fail("workspace path contains a symlink or non-canonical component")
    info = resolved.stat(follow_symlinks=False)
except (OSError, RuntimeError):
    fail("workspace must be an existing canonical directory")
if not resolved.is_dir():
    fail("workspace is not a directory")
if info.st_uid != uid:
    fail("workspace must be owned by the invoking host user")
if resolved == pathlib.Path("/"):
    fail("filesystem root cannot be exported")
try:
    home = pathlib.Path(pwd.getpwuid(uid).pw_dir).resolve(strict=True)
except (KeyError, OSError):
    fail("cannot resolve invoking host account home")
if resolved == home:
    fail("the complete host home directory cannot be exported")

def canon(raw_path):
    path = pathlib.Path(raw_path)
    if not path.is_absolute():
        path = pathlib.Path.cwd() / path
    return pathlib.Path(os.path.abspath(path))

install = canon(install_raw)
state = canon(state_raw)
for label, protected in (("trusted installation", install), ("private runtime state", state)):
    if resolved == protected or resolved in protected.parents or protected in resolved.parents:
        fail(f"workspace overlaps {label}")
if credentials_raw:
    credentials = canon(credentials_raw)
    if resolved == credentials or resolved in credentials.parents or credentials in resolved.parents:
        fail("workspace overlaps the private provider credential file")
print(resolved)
PY
}

vm_host_validate_credentials() {
  local file="${1:-}" workspace="${2:-}" install_root="${3:-}" state_root="${4:-}"
  [[ -n "$file" ]] || { vm_host_error 'a private provider credential file is required'; return 1; }
  python3 -I - "$file" "$workspace" "$install_root" "$state_root" <<'PY'
import json
import os
import pathlib
import re
import stat
import sys

raw, workspace_raw, install_raw, state_raw = sys.argv[1:]
def fail(message):
    print(f"REFUSED: {message}", file=sys.stderr)
    raise SystemExit(1)
path = pathlib.Path(raw)
try:
    if not path.is_absolute():
        path = pathlib.Path.cwd() / path
    lexical = pathlib.Path(os.path.abspath(path))
    resolved = path.resolve(strict=True)
    info = resolved.stat(follow_symlinks=False)
except (OSError, RuntimeError):
    fail("credential file must be an existing regular file")
if lexical != resolved or not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
    fail("credential file must be a canonical regular file, not a symlink")
if info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
    fail("credential file must be invoking-user-owned, single-link, and mode 0600")
try:
    text = resolved.read_text(encoding="utf-8")
    data = json.loads(text)
except (OSError, UnicodeError, json.JSONDecodeError):
    fail("credential file must contain valid UTF-8 JSON")
if not isinstance(data, dict) or set(data) != {"provider", "model", "api_key"}:
    fail("credential JSON must contain exactly provider, model, and api_key")
if data.get("provider") != "openai":
    fail("only the owner-selected OpenAI provider is supported in this smoke")
model = data.get("model")
key = data.get("api_key")
if not isinstance(model, str) or not re.fullmatch(r"openai/[A-Za-z0-9._-]{1,100}", model):
    fail("model must be an OpenAI model ID of the form openai/<model>")
if not isinstance(key, str) or not 20 <= len(key) <= 4096 or any(ord(c) < 0x21 or ord(c) > 0x7e for c in key):
    fail("api_key must be a non-empty printable ASCII credential")
for raw_protected in (workspace_raw, install_raw, state_raw):
    protected = pathlib.Path(os.path.abspath(raw_protected))
    if resolved == protected or resolved in protected.parents or protected in resolved.parents:
        fail("credential file overlaps workspace, installation, or runtime state")
PY
}

vm_host_credential_model() {
  python3 -I - "$1" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["model"])
PY
}

vm_host_credential_key_to_stdout() {
  # Intended only as a pipe into forced-TTY SSH; never capture or log this value.
  python3 -I - "$1" <<'PY'
import json, sys
sys.stdout.write(json.load(open(sys.argv[1], encoding="utf-8"))["api_key"] + "\n")
PY
}

vm_host_account_home() {
  python3 -I -c 'import os,pathlib,pwd; print(pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True))'
}

vm_host_prepare_state_root() {
  python3 -I - "$1" <<'PY'
import os, pathlib, stat, sys

home = pathlib.Path(sys.argv[1]).resolve(strict=True)
uid = os.getuid()
flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
fd = os.open(home, flags)
try:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise SystemExit("REFUSED: invoking user's home directory is not safely owned")
    for index, component in enumerate((".local", "state", "little-mester", "v3-smoke")):
        try:
            os.mkdir(component, 0o700, dir_fd=fd)
        except FileExistsError:
            pass
        try:
            next_fd = os.open(component, flags, dir_fd=fd)
        except OSError as error:
            raise SystemExit("REFUSED: private runtime state path contains a symlink or non-directory") from error
        child = os.fstat(next_fd)
        if not stat.S_ISDIR(child.st_mode) or child.st_uid != uid or child.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            os.close(next_fd)
            raise SystemExit("REFUSED: private runtime state path contains an unsafe directory")
        if index >= 1:
            os.fchmod(next_fd, 0o700)
        os.close(fd)
        fd = next_fd
finally:
    os.close(fd)
print(home / ".local/state/little-mester/v3-smoke")
PY
}

vm_host_check_run_capacity() {
  python3 -I - "$1" "$VM_HOST_MAX_RETAINED_RUNS" <<'PY'
import os, pathlib, stat, sys
root = pathlib.Path(sys.argv[1])
maximum = int(sys.argv[2])
entries = list(os.scandir(root))
if len(entries) >= maximum:
    print(f"REFUSED: {maximum} diagnostic runs are retained; owner review/cleanup is required", file=sys.stderr)
    raise SystemExit(1)
for item in entries:
    info = item.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_ISLNK(info.st_mode):
        print("REFUSED: unexpected entry in private run state; preserving it for owner review", file=sys.stderr)
        raise SystemExit(1)
    if (pathlib.Path(item.path) / "disk.qcow2").exists() or (pathlib.Path(item.path) / "ubuntu.img.part").exists():
        print("REFUSED: prior VM state was retained after ambiguous cleanup; owner review is required", file=sys.stderr)
        raise SystemExit(1)
PY
}

vm_host_workspace_capacity_allowed() {
  local mount_root="${1:-}" total_bytes="${2:-}"
  [[ "$mount_root" == yes && "$total_bytes" =~ ^[1-9][0-9]*$ ]] || {
    vm_host_error 'workspace must be the root of a dedicated owner-provisioned bounded filesystem'
    return 1
  }
  (( total_bytes <= VM_HOST_WORKSPACE_MAX_BYTES )) || {
    vm_host_error 'workspace filesystem exceeds the 8-GiB smoke limit'
    return 1
  }
}

vm_host_check_workspace_filesystem() {
  local workspace="$1" facts mount_root total_bytes
  facts="$(python3 -I - "$workspace" <<'PY'
import os, pathlib, sys
path = pathlib.Path(sys.argv[1]).resolve(strict=True)
if not path.is_dir():
    print("REFUSED: selected workspace is not a directory", file=sys.stderr)
    raise SystemExit(1)
usage = os.statvfs(path)
total_bytes = usage.f_blocks * usage.f_frsize
print(("yes" if os.path.ismount(path) else "no"), total_bytes)
PY
)" || return 1
  read -r mount_root total_bytes <<<"$facts"
  vm_host_workspace_capacity_allowed "$mount_root" "$total_bytes"
}

vm_host_require_commands() {
  local command
  for command in qemu-system-x86_64 qemu-img virtiofsd curl cat mv ssh ssh-keygen ssh-keyscan socat python3 timeout sha256sum date mktemp mkdir chmod stat id grep rm sleep readlink; do
    local executable
    executable="$(command -v "$command" 2>/dev/null || true)"
    [[ -n "$executable" ]] || {
      vm_host_error "required host command missing: $command (host installation is never performed automatically)"
      return 1
    }
    python3 -I "$VM_HOST_SCRIPT_ROOT/lib/vm/executable-policy.py" "$executable" >/dev/null || {
      vm_host_error "host command is not from a protected root-owned path: $command"
      return 1
    }
  done
  local iso_tool
  iso_tool="$(command -v genisoimage 2>/dev/null || command -v mkisofs 2>/dev/null || true)"
  [[ -n "$iso_tool" ]] || {
    vm_host_error 'required host command missing: genisoimage or mkisofs (host installation is never performed automatically)'
    return 1
  }
  python3 -I "$VM_HOST_SCRIPT_ROOT/lib/vm/executable-policy.py" "$iso_tool" >/dev/null || {
    vm_host_error 'ISO creation command is not from a protected root-owned path'
    return 1
  }
}

vm_host_is_under() {
  python3 -I - "$1" "$2" <<'PY'
import os, pathlib, sys
try:
    candidate = pathlib.Path(sys.argv[1]).resolve(strict=False)
    root = pathlib.Path(sys.argv[2]).resolve(strict=False)
    candidate.relative_to(root)
    raise SystemExit(0)
except ValueError:
    raise SystemExit(1)
except OSError as error:
    print("REFUSED: could not safely compare install and workspace paths", file=sys.stderr)
    raise SystemExit(2) from error
PY
}

vm_host_record_process_identity() {
  local identity_file="$1" pid="$2" executable="$3" expected_arg="$4"
  python3 -I - "$identity_file" "$pid" "$executable" "$expected_arg" <<'PY'
import json, os, pathlib, stat, sys

out, raw_pid, expected_exe, expected_arg = sys.argv[1:]
try:
    pid = int(raw_pid)
    if pid <= 1:
        raise ValueError()
    proc = pathlib.Path(f"/proc/{pid}")
    actual_exe = os.path.realpath(proc / "exe")
    argv = [os.fsdecode(arg) for arg in (proc / "cmdline").read_bytes().split(b"\0") if arg]
    stat_fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
    start_time = stat_fields[19]  # Linux /proc stat field 22 (starttime).
except (OSError, ValueError, IndexError):
    raise SystemExit("REFUSED: could not capture child process identity")
expected_exe = os.path.realpath(expected_exe)
if actual_exe != expected_exe or expected_arg not in argv:
    raise SystemExit("REFUSED: spawned process executable/argv did not match the requested run")
record = {"pid": pid, "start_time": start_time, "exe": actual_exe, "argv": argv}
fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    json.dump(record, stream, separators=(",", ":"))
    stream.write("\n")
PY
}

vm_host_process_identity_status() {
  local identity_file="$1"
  python3 -I - "$identity_file" <<'PY'
import json, os, pathlib, sys
try:
    record = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    pid = record["pid"]
    if not isinstance(pid, int) or pid <= 1:
        raise ValueError()
    proc = pathlib.Path(f"/proc/{pid}")
    actual_exe = os.path.realpath(proc / "exe")
    argv = [os.fsdecode(arg) for arg in (proc / "cmdline").read_bytes().split(b"\0") if arg]
    stat_fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
except FileNotFoundError:
    raise SystemExit(1)  # Process is gone; no signal may be sent.
except (OSError, ValueError, KeyError, TypeError, IndexError, json.JSONDecodeError):
    raise SystemExit(2)  # Malformed or unreadable identity is ambiguous.
if (str(record.get("start_time")) != stat_fields[19]
        or record.get("exe") != actual_exe
        or record.get("argv") != argv):
    raise SystemExit(2)
raise SystemExit(0)
PY
}

vm_host_stop_process_identity() {
  local identity_file="$1"
  python3 -I - "$identity_file" "$VM_HOST_PROCESS_STOP_GRACE_MS" "$VM_HOST_PROCESS_KILL_SETTLE_MS" <<'PY'
import json, os, pathlib, select, signal, sys

identity_path = pathlib.Path(sys.argv[1])
try:
    term_grace_ms = int(sys.argv[2])
    kill_settle_ms = int(sys.argv[3])
    if (term_grace_ms <= 0 or kill_settle_ms <= 0
            or term_grace_ms + kill_settle_ms > 15000):
        raise ValueError()
except ValueError:
    raise SystemExit("REFUSED: invalid bounded process cleanup deadline")
try:
    record = json.loads(identity_path.read_text(encoding="utf-8"))
    pid = record["pid"]
    if not isinstance(pid, int) or pid <= 1:
        raise ValueError()
    pidfd = os.pidfd_open(pid, 0)
except ProcessLookupError:
    raise SystemExit(0)
except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
    print("REFUSED: process identity is ambiguous; retaining run state", file=sys.stderr)
    raise SystemExit(2)

def matches():
    try:
        proc = pathlib.Path(f"/proc/{pid}")
        exe = os.path.realpath(proc / "exe")
        argv = [os.fsdecode(arg) for arg in (proc / "cmdline").read_bytes().split(b"\0") if arg]
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return False
    return (str(record.get("start_time")) == fields[19]
            and record.get("exe") == exe and record.get("argv") == argv)

try:
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    if poller.poll(0):
        raise SystemExit(0)
    if not matches():
        if poller.poll(0):
            raise SystemExit(0)
        print("REFUSED: process identity changed; retaining run state", file=sys.stderr)
        raise SystemExit(2)
    try:
        signal.pidfd_send_signal(pidfd, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if not poller.poll(term_grace_ms):
        if not matches():
            if poller.poll(0):
                raise SystemExit(0)
            print("REFUSED: process identity changed before escalation; retaining run state", file=sys.stderr)
            raise SystemExit(2)
        try:
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if not poller.poll(kill_settle_ms):
            print("REFUSED: owned process did not exit; retaining run state", file=sys.stderr)
            raise SystemExit(1)
finally:
    os.close(pidfd)
PY
}

vm_host_verify_logger_completion() {
  local identity_file="$1" completion_file="$2"
  python3 -I - "$identity_file" "$completion_file" "$VM_HOST_SCRIPT_ROOT/lib/vm/bounded-log.py" <<'PY'
import json, os, pathlib, stat, sys

identity_path, completion_path, expected_helper = map(pathlib.Path, sys.argv[1:])
try:
    record = json.loads(identity_path.read_text(encoding="utf-8"))
    completion_info = os.lstat(completion_path)
    if (not stat.S_ISREG(completion_info.st_mode) or completion_info.st_uid != os.getuid()
            or completion_info.st_mode & 0o077):
        raise ValueError()
    proof = json.loads(completion_path.read_text(encoding="utf-8"))
    argv = record["argv"]
    separator = argv.index("--")
    option = argv.index("--completion-file")
    if (option >= separator or option + 1 >= separator
            or pathlib.Path(argv[option + 1]) != completion_path
            or expected_helper not in [pathlib.Path(arg) for arg in argv[:separator]]):
        raise ValueError()
    child_pid = proof["child_pid"]
    if (proof.get("logger_pid") != record["pid"]
            or str(proof.get("logger_start_time")) != str(record["start_time"])
            or proof.get("process_group_clean") is not True
            or not isinstance(child_pid, int) or child_pid <= 1):
        raise ValueError()
except (OSError, ValueError, KeyError, TypeError, IndexError, json.JSONDecodeError):
    raise SystemExit("REFUSED: bounded logger cleanup proof is missing or invalid")

try:
    entries = os.scandir("/proc")
except OSError as error:
    raise SystemExit("REFUSED: could not verify bounded logger process-group cleanup") from error
with entries:
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            fields = pathlib.Path(entry.path, "stat").read_text().rsplit(")", 1)[1].split()
            process_group, session = int(fields[2]), int(fields[3])
        except FileNotFoundError:
            continue
        except (OSError, ValueError, IndexError) as error:
            raise SystemExit("REFUSED: could not verify bounded logger process-group cleanup") from error
        if process_group == child_pid and session == child_pid:
            raise SystemExit("REFUSED: bounded logger process group still has live members")
PY
}

vm_host_cleanup_one() {
  local identity_file="$1" completion_file="${2:-}"
  if [[ ! -e "$identity_file" && ! -L "$identity_file" ]]; then
    if [[ -n "$completion_file" && ( -e "$completion_file" || -L "$completion_file" ) ]]; then
      printf 'WARNING: bounded logger proof has no matching identity; preserving run state %s\n' "$VM_HOST_RUN_DIR" >&2
      return 1
    fi
    return 0
  fi
  [[ -f "$identity_file" && ! -L "$identity_file" ]] || {
    printf 'WARNING: invalid process identity record; preserving run state %s\n' "$VM_HOST_RUN_DIR" >&2
    return 1
  }
  vm_host_stop_process_identity "$identity_file" || {
    printf 'WARNING: process ownership is ambiguous; preserving run state %s\n' "$VM_HOST_RUN_DIR" >&2
    return 1
  }
  if [[ -n "$completion_file" ]]; then
    vm_host_verify_logger_completion "$identity_file" "$completion_file" || {
      printf 'WARNING: bounded logger completion is unverified; preserving run state %s\n' "$VM_HOST_RUN_DIR" >&2
      return 1
    }
  fi
  if [[ -n "$completion_file" ]]; then
    rm -f -- "$identity_file" "$completion_file"
  else
    rm -f -- "$identity_file"
  fi
}

vm_host_cleanup() {
  local cleanup_failed="${VM_HOST_UNTRACKED_CHILD:-0}"
  if [[ -z "${VM_HOST_RUN_DIR:-}" ]]; then
    if (( cleanup_failed == 0 )) && [[ "${VM_HOST_SMOKE_COMPLETE_PENDING:-0}" != 1 ]]; then
      return 0
    fi
    printf 'WARNING: cleanup incomplete; run state is unavailable, preserving possible resources\n' >&2
    return 1
  fi
  if ! python3 -I - "$VM_HOST_RUN_DIR" <<'PY'
import os, pathlib, stat, sys

raw = sys.argv[1]
try:
    path = pathlib.Path(raw)
    lexical = pathlib.Path(os.path.abspath(raw))
    resolved = path.resolve(strict=True)
    info = os.lstat(raw)
except (OSError, RuntimeError):
    raise SystemExit("REFUSED: private run-state directory cannot be verified")
if (lexical != resolved or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid() or info.st_mode & 0o077):
    raise SystemExit("REFUSED: private run-state directory cannot be verified")
PY
  then
    printf 'WARNING: cleanup incomplete; private run state is missing, substituted, or not owner-private; preserving it\n' >&2
    return 1
  fi
  if (( cleanup_failed == 0 )); then
    vm_host_cleanup_one "$VM_HOST_RUN_DIR/qemu.identity" "$VM_HOST_RUN_DIR/qemu.complete" || cleanup_failed=1
  fi
  if (( cleanup_failed == 0 )); then
    vm_host_cleanup_one "$VM_HOST_RUN_DIR/virtiofsd.identity" "$VM_HOST_RUN_DIR/virtiofsd.complete" || cleanup_failed=1
  fi
  if (( cleanup_failed == 0 )); then
    vm_host_cleanup_one "$VM_HOST_RUN_DIR/proxy.identity" "$VM_HOST_RUN_DIR/proxy.complete" || cleanup_failed=1
  fi
  if (( cleanup_failed == 0 )); then
    rm -f -- "$VM_HOST_RUN_DIR/disk.qcow2" "$VM_HOST_RUN_DIR/ubuntu.img.part" \
      "$VM_HOST_RUN_DIR/seed.iso" "$VM_HOST_RUN_DIR/user-data" "$VM_HOST_RUN_DIR/meta-data" \
      "$VM_HOST_RUN_DIR/id_vm_control" "$VM_HOST_RUN_DIR/id_vm_control.pub" \
      "$VM_HOST_RUN_DIR/known_hosts" "$VM_HOST_RUN_DIR/known_hosts.tmp" "$VM_HOST_RUN_DIR/known_hosts.valid" \
      "$VM_HOST_RUN_DIR/qemu.command" "$VM_HOST_RUN_DIR/proxy.port" "$VM_HOST_RUN_DIR/ssh_port" \
      "$VM_HOST_RUN_DIR/model-response.raw" "$VM_HOST_RUN_DIR/docker-container.raw" \
      "$VM_HOST_RUN_DIR/docker-inspect.raw" "$VM_HOST_RUN_DIR/docker-security.raw" || cleanup_failed=1
  fi
  if (( cleanup_failed == 0 )); then
    python3 -I - "$VM_HOST_RUN_DIR" "$VM_HOST_LOG_LIMIT_BYTES" <<'PY' || cleanup_failed=1
import os, pathlib, sys
root = pathlib.Path(sys.argv[1])
limit = int(sys.argv[2])
for name in ("qemu.log", "virtiofsd.log", "proxy.log"):
    path = root / name
    if not path.is_file() or path.is_symlink():
        continue
    data = path.read_bytes()
    if len(data) > limit:
        temporary = root / (name + ".bounded")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data[-limit:])
        os.replace(temporary, path)
    os.chmod(path, 0o600, follow_symlinks=False)
PY
  fi
  if (( cleanup_failed != 0 )); then
    printf 'WARNING: cleanup incomplete; private run state retained at %s\n' "$VM_HOST_RUN_DIR" >&2
    return 1
  fi
  return 0
}

vm_host_exit_cleanup() {
  local original_status=$?
  trap - EXIT
  if vm_host_cleanup; then
    if (( original_status == 0 )) && [[ "${VM_HOST_SMOKE_COMPLETE_PENDING:-0}" == 1 ]]; then
      printf 'Smoke acceptance stages completed.\n' || exit 1
    fi
    exit "$original_status"
  fi
  if (( original_status == 0 )); then
    exit 1
  fi
  exit "$original_status"
}

vm_host_ssh() {
  local run_dir="$1" command="$2" limit="${3:-$VM_HOST_CONTROL_TIMEOUT}"
  vm_host_ssh_bounded "$run_dir" - "$command" "$limit" 0 0
}

vm_host_ssh_capture() {
  local run_dir="$1" output="$2" command="$3" limit="${4:-$VM_HOST_CONTROL_TIMEOUT}"
  vm_host_ssh_bounded "$run_dir" "$output" "$command" "$limit" 0 0
}

vm_host_ssh_bounded() {
  local run_dir="$1" output="$2" command="$3" limit="$4"
  local inherit_stdin="${5:-0}" force_tty="${6:-0}"
  local bounded_logger="$VM_HOST_SCRIPT_ROOT/lib/vm/bounded-log.py"
  local -a logger_options=(--fail-on-overflow)
  local -a ssh_options=(-F /dev/null)
  local ssh_port
  [[ "$limit" =~ ^[1-9][0-9]*$ ]] || { vm_host_error 'SSH command deadline must be a positive integer'; return 2; }
  ssh_port="$(<"$run_dir/ssh_port")"
  [[ "$ssh_port" =~ ^[0-9]{1,5}$ ]] || { vm_host_error 'SSH control port is invalid'; return 2; }
  if (( inherit_stdin )); then logger_options+=(--stdin); fi
  if (( force_tty )); then ssh_options+=(-tt); else ssh_options+=(-T); fi
  ssh_options+=(
    -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=yes
    -o UserKnownHostsFile="$run_dir/known_hosts" -o GlobalKnownHostsFile=/dev/null
    -o IdentitiesOnly=yes -o IdentityAgent=none -o ProxyCommand=none -o ProxyJump=none
    -o CanonicalizeHostname=no -o ControlMaster=no -o ClearAllForwardings=yes
    -o PermitLocalCommand=no -o LogLevel=ERROR
    -i "$run_dir/id_vm_control" -p "$ssh_port" "mester@127.0.0.1" "$command"
  )
  python3 -I "$bounded_logger" "$output" "$VM_HOST_GUEST_OUTPUT_LIMIT_BYTES" \
    "${logger_options[@]}" -- timeout --signal=TERM --kill-after=5 "$limit" ssh "${ssh_options[@]}"
}

vm_host_write_cloud_seed() {
  local run_dir="$1" vm_name="$2" uid="$3" gid="$4" pubkey="$5" iso_tool
  if ! vm_host_cloud_init "$vm_name" "$uid" "$gid" "$pubkey" >"$run_dir/user-data"; then
    vm_host_error 'cloud-init seed generation failed'
    return 1
  fi
  if ! printf 'instance-id: %s\nlocal-hostname: %s\n' "$vm_name" "$vm_name" >"$run_dir/meta-data"; then
    vm_host_error 'cloud-init metadata creation failed'
    return 1
  fi
  chmod 0600 "$run_dir/user-data" "$run_dir/meta-data" || return 1
  if command -v genisoimage >/dev/null 2>&1; then iso_tool=genisoimage; else iso_tool=mkisofs; fi
  if ! "$iso_tool" -quiet -rock -joliet -output "$run_dir/seed.iso" -V cidata "$run_dir/user-data" "$run_dir/meta-data"; then
    vm_host_error 'cloud-init seed image creation failed'
    return 1
  fi
  chmod 0600 "$run_dir/seed.iso" || return 1
}

vm_host_prepare_disk() {
  local run_dir="$1" download="$1/ubuntu.img.part"
  printf 'stage: pinned Ubuntu base download\n'
  if ! curl -q --fail --location --silent --show-error --max-time "$VM_HOST_DOWNLOAD_TIMEOUT" \
    --retry 2 --output "$download" "$VM_HOST_IMAGE_URL"; then
    vm_host_error 'pinned Ubuntu image download failed'
    return 1
  fi
  printf '%s  %s\n' "$VM_HOST_IMAGE_SHA256" "$download" | sha256sum --check --status || {
    vm_host_error 'pinned Ubuntu image checksum mismatch'
    return 1
  }
  if ! qemu-img convert -f qcow2 -O qcow2 "$download" "$run_dir/disk.qcow2"; then
    vm_host_error 'pinned Ubuntu image conversion failed'
    return 1
  fi
  if ! qemu-img resize "$run_dir/disk.qcow2" "${VM_HOST_DISK_GIB}G"; then
    vm_host_error 'VM disk sizing failed'
    return 1
  fi
  rm -f -- "$download" || return 1
}

vm_host_start_proxy() {
  local run_dir="$1" proxy_script="$VM_HOST_SCRIPT_ROOT/lib/vm/proxy.py"
  local bounded_logger="$VM_HOST_SCRIPT_ROOT/lib/vm/bounded-log.py"
  local port_file="$1/proxy.port" completion_file="$1/proxy.complete"
  local pid python_exe status end
  rm -f -- "$port_file" "$completion_file"
  : >"$run_dir/proxy.log" || { vm_host_error 'could not create private proxy log'; return 1; }
  (umask 077; python3 -I "$bounded_logger" "$run_dir/proxy.log" "$VM_HOST_LOG_LIMIT_BYTES" \
    --completion-file "$completion_file" -- \
    python3 -I "$proxy_script" --port-file "$port_file") </dev/null &
  pid="$!"
  VM_HOST_UNTRACKED_CHILD=1
  python_exe="$(readlink -f -- "$(command -v python3)")" || return 1
  vm_host_record_process_identity "$run_dir/proxy.identity" "$pid" "$python_exe" "$bounded_logger" || {
    vm_host_error 'could not bind egress proxy to an exact run-owned process identity'
    return 1
  }
  VM_HOST_UNTRACKED_CHILD=0
  chmod 0600 "$run_dir/proxy.identity" "$run_dir/proxy.log" || return 1
  end=$((SECONDS + 10))
  while (( SECONDS < end )); do
    [[ -s "$port_file" ]] && break
    if vm_host_process_identity_status "$run_dir/proxy.identity"; then
      :
    else
      status=$?
      (( status == 1 )) && break
      vm_host_error 'egress proxy process identity became ambiguous'
      return 1
    fi
    sleep 0.1
  done
  [[ -s "$port_file" ]] || { vm_host_error 'public-only HTTP(S) egress proxy did not become ready'; return 1; }
  VM_HOST_PROXY_PORT="$(<"$port_file")"
  [[ "$VM_HOST_PROXY_PORT" =~ ^[0-9]{1,5}$ ]] && (( VM_HOST_PROXY_PORT >= 1024 && VM_HOST_PROXY_PORT <= 65535 )) || {
    vm_host_error 'egress proxy returned an invalid listener port'
    return 1
  }
}

vm_host_start_virtiofs() {
  local run_dir="$1" workspace="$2" socket="$1/virtiofs.sock"
  local completion_file="$1/virtiofsd.complete" pid executable status end
  local bounded_logger="$VM_HOST_SCRIPT_ROOT/lib/vm/bounded-log.py"
  [[ ! -e "$socket" && ! -L "$socket" ]] || { vm_host_error 'refusing pre-existing virtiofs socket path'; return 1; }
  rm -f -- "$completion_file"
  (umask 077; python3 -I "$bounded_logger" "$run_dir/virtiofsd.log" "$VM_HOST_LOG_LIMIT_BYTES" \
    --completion-file "$completion_file" -- \
    virtiofsd --shared-dir "$workspace" --socket-path "$socket" --rlimit-nofile=1048576) </dev/null &
  pid="$!"
  VM_HOST_UNTRACKED_CHILD=1
  executable="$(readlink -f -- "$(command -v python3)")" || return 1
  vm_host_record_process_identity "$run_dir/virtiofsd.identity" "$pid" "$executable" "$bounded_logger" || {
    vm_host_error 'could not bind virtiofsd to an exact run-owned process identity'
    return 1
  }
  VM_HOST_UNTRACKED_CHILD=0
  chmod 0600 "$run_dir/virtiofsd.identity" "$run_dir/virtiofsd.log" || return 1
  end=$((SECONDS + 15))
  while (( SECONDS < end )); do
    [[ -S "$socket" ]] && return 0
    if vm_host_process_identity_status "$run_dir/virtiofsd.identity"; then
      :
    else
      status=$?
      (( status == 1 )) && break
      vm_host_error 'virtiofsd process identity became ambiguous'
      return 1
    fi
    sleep 0.2
  done
  vm_host_error 'virtiofsd did not create its run-owned workspace socket'
  return 1
}

vm_host_free_port() {
  python3 -I -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
}

vm_host_start_qemu() {
  local run_dir="$1" vm_name="$2" workspace="$3" ssh_port="$4" netdev pid executable status end
  local bounded_logger="$VM_HOST_SCRIPT_ROOT/lib/vm/bounded-log.py"
  local completion_file="$1/qemu.complete"
  netdev="$(vm_host_qemu_network_args "$ssh_port" "$VM_HOST_PROXY_PORT")" || return 1
  local -a qemu=(qemu-system-x86_64 -enable-kvm -name "$vm_name" -machine q35,accel=kvm
    -cpu host -smp "$VM_HOST_CPU_COUNT" -m "$VM_HOST_MEMORY_MIB"
    -object "memory-backend-memfd,id=mem,size=${VM_HOST_MEMORY_MIB}M,share=on"
    -numa node,memdev=mem
    -drive "file=$run_dir/seed.iso,format=raw,if=none,id=cloud,readonly=on"
    -device virtio-scsi-pci,id=scsi0 -device scsi-cd,drive=cloud
    -drive "file=$run_dir/disk.qcow2,format=qcow2,if=virtio"
    -chardev "socket,id=charfs,path=$run_dir/virtiofs.sock"
    -device vhost-user-fs-pci,chardev=charfs,tag=workspace
    -display none -serial stdio -monitor none
    -netdev "$netdev" -device virtio-net-pci,netdev=net0 -no-reboot)
  printf '%q ' "${qemu[@]}" >"$run_dir/qemu.command" || return 1
  chmod 0600 "$run_dir/qemu.command" || return 1
  : >"$run_dir/qemu.log" || return 1
  chmod 0600 "$run_dir/qemu.log" || return 1
  rm -f -- "$completion_file"
  python3 -I "$bounded_logger" "$run_dir/qemu.log" "$VM_HOST_LOG_LIMIT_BYTES" \
    --completion-file "$completion_file" -- \
    "${qemu[@]}" </dev/null &
  pid="$!"
  VM_HOST_UNTRACKED_CHILD=1
  executable="$(readlink -f -- "$(command -v python3)")" || return 1
  vm_host_record_process_identity "$run_dir/qemu.identity" "$pid" "$executable" "$bounded_logger" || {
    vm_host_error 'could not bind QEMU to an exact run-owned process identity'
    return 1
  }
  VM_HOST_UNTRACKED_CHILD=0
  chmod 0600 "$run_dir/qemu.identity" || return 1
  printf '%s\n' "$ssh_port" >"$run_dir/ssh_port" || return 1
  chmod 0600 "$run_dir/ssh_port" || return 1
  end=$((SECONDS + 3))
  while (( SECONDS < end )); do
    if vm_host_process_identity_status "$run_dir/qemu.identity"; then return 0; else status=$?; fi
    (( status == 1 )) && break
    if (( status != 2 )); then break; fi
    sleep 0.1
  done
  vm_host_error 'QEMU exited or changed identity immediately after launch'
  return 1
}

vm_host_wait_ssh() {
  local run_dir="$1" ssh_port="$2" end
  end=$((SECONDS + VM_HOST_BOOT_TIMEOUT))
  while (( SECONDS < end )); do
    if ! vm_host_process_identity_status "$run_dir/qemu.identity"; then
      vm_host_error 'run-owned QEMU exited or its process identity became ambiguous before guest SSH readiness'
      return 1
    fi
    if python3 -I "$VM_HOST_SCRIPT_ROOT/lib/vm/bounded-log.py" \
      "$run_dir/known_hosts.tmp" "$VM_HOST_SSH_KEY_LIMIT_BYTES" --fail-on-overflow -- \
      timeout --signal=TERM --kill-after=2 3 ssh-keyscan -T 2 -t ed25519 -p "$ssh_port" -H 127.0.0.1; then
      if (umask 077; grep -E '^\|1\|[A-Za-z0-9+/=]+\|[A-Za-z0-9+/=]+[[:space:]]+ssh-ed25519[[:space:]]+[A-Za-z0-9+/=]+$' \
        "$run_dir/known_hosts.tmp" >"$run_dir/known_hosts.valid") && [[ -s "$run_dir/known_hosts.valid" ]]; then
        chmod 0600 "$run_dir/known_hosts.valid"
        mv -- "$run_dir/known_hosts.valid" "$run_dir/known_hosts"
        rm -f -- "$run_dir/known_hosts.tmp"
        return 0
      fi
    fi
    sleep 1 || return 1
  done
  vm_host_error "guest SSH did not become ready within ${VM_HOST_BOOT_TIMEOUT}s"
}

vm_host_provisioning_ready() {
  local run_dir="$1" security_output="$1/docker-security.raw" security
  printf 'stage: waiting for cloud-init and rootless guest readiness\n'
  if ! vm_host_ssh "$run_dir" \
    'cloud-init status --wait --long && test -f /var/lib/mester-v3-provisioned && mountpoint -q /workspace && test ! -S /var/run/docker.sock' "$VM_HOST_BOOT_TIMEOUT"; then
    vm_host_error 'cloud-init, workspace mount, or rootless bootstrap readiness failed'
    return 1
  fi
  if ! vm_host_ssh_capture "$run_dir" "$security_output" \
    'env -i HOME=/home/mester PATH=/usr/local/bin:/usr/bin:/bin XDG_RUNTIME_DIR=/run/user/'"$(id -u)"' DOCKER_HOST=unix:///run/user/'"$(id -u)"'/docker.sock /usr/bin/docker info --format "{{json .SecurityOptions}}"'; then
    rm -f -- "$security_output"
    vm_host_error 'guest rootless Docker info failed'
    return 1
  fi
  security="$(<"$security_output")"
  rm -f -- "$security_output"
  [[ "$security" == *rootless* ]] || { vm_host_error 'guest Docker did not report rootless; no rootful fallback'; return 1; }
  printf 'ROOTLESS_DOCKER_OK\n' || return 1
}

vm_host_import_credentials() {
  local run_dir="$1" credential_file="$2" timeout_seconds=60
  printf 'stage: importing owner credential into private guest OpenCode store\n'
  if ! vm_host_credential_key_to_stdout "$credential_file" | \
    vm_host_ssh_bounded "$run_dir" - \
      'env -i HOME=/home/mester PATH=/usr/local/bin:/usr/bin:/bin XDG_DATA_HOME=/home/mester/.local/share XDG_CONFIG_HOME=/home/mester/.config HTTP_PROXY=http://10.0.2.100:3128 HTTPS_PROXY=http://10.0.2.100:3128 opencode auth login openai --method key && chmod 0600 /home/mester/.local/share/opencode/opencode.db' \
      "$timeout_seconds" 1 1; then
    vm_host_error 'private guest OpenCode credential import failed (credential content suppressed)'
    return 1
  fi
  if ! vm_host_ssh "$run_dir" 'test -d /home/mester/.local/share/opencode && test "$(stat -c %a /home/mester/.local/share/opencode)" = 700 && test -f /home/mester/.local/share/opencode/opencode.db && test ! -L /home/mester/.local/share/opencode/opencode.db && test "$(stat -c %a /home/mester/.local/share/opencode/opencode.db)" = 600'; then
    vm_host_error 'guest OpenCode credential database is not private mode 0600'
    return 1
  fi
  VM_HOST_CREDENTIALS_IMPORTED=1
}

vm_host_model_smoke() {
  local run_dir="$1" model="$2" response="$1/model-response.raw" rc
  printf 'stage: requesting real OpenCode model response\n'
  if vm_host_ssh_capture "$run_dir" "$response" \
    "env -i HOME=/home/mester PATH=/usr/local/bin:/usr/bin:/bin XDG_DATA_HOME=/home/mester/.local/share XDG_CONFIG_HOME=/home/mester/.config HTTP_PROXY=http://10.0.2.100:3128 HTTPS_PROXY=http://10.0.2.100:3128 opencode run --model '$model' 'Reply with exactly: VM_RESPONSE_OK'" \
    "$VM_HOST_PROVIDER_TIMEOUT"; then
    rc=0
  else
    rc=$?
  fi
  if (( rc != 0 )) || ! grep -Fxq 'VM_RESPONSE_OK' "$response"; then
    rm -f -- "$response"
    vm_host_error 'OpenCode did not produce the required real response marker; details suppressed'
    return 1
  fi
  rm -f -- "$response"
  printf 'VM_RESPONSE_OK\n' || return 1
}

vm_host_docker_smoke() {
  local run_dir="$1" output="$1/docker-container.raw" inspect_output="$1/docker-inspect.raw" digest
  printf 'stage: executing rootless guest Docker container\n'
  if ! vm_host_ssh_capture "$run_dir" "$inspect_output" \
    "env -i HOME=/home/mester PATH=/usr/local/bin:/usr/bin:/bin XDG_RUNTIME_DIR=/run/user/$(id -u) DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock /usr/bin/docker image inspect --format '{{index .RepoDigests 0}}' '$VM_HOST_DOCKER_IMAGE' 2>/dev/null || true"; then
    rm -f -- "$inspect_output"
    vm_host_error 'could not inspect the rootless Docker image cache'
    return 1
  fi
  digest="$(<"$inspect_output")"
  rm -f -- "$inspect_output"
  if [[ -z "$digest" ]]; then
    vm_host_ssh "$run_dir" \
      "env -i HOME=/home/mester PATH=/usr/local/bin:/usr/bin:/bin XDG_RUNTIME_DIR=/run/user/$(id -u) DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock HTTP_PROXY=http://10.0.2.100:3128 HTTPS_PROXY=http://10.0.2.100:3128 /usr/bin/docker pull '$VM_HOST_DOCKER_IMAGE'" \
      "$VM_HOST_DOCKER_TIMEOUT" >/dev/null 2>&1 || {
      vm_host_error 'rootless Docker image pull failed; output suppressed'
      return 1
    }
    if ! vm_host_ssh_capture "$run_dir" "$inspect_output" \
      "env -i HOME=/home/mester PATH=/usr/local/bin:/usr/bin:/bin XDG_RUNTIME_DIR=/run/user/$(id -u) DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock /usr/bin/docker image inspect --format '{{index .RepoDigests 0}}' '$VM_HOST_DOCKER_IMAGE'"; then
      rm -f -- "$inspect_output"
      vm_host_error 'could not read the rootless Docker image digest'
      return 1
    fi
    digest="$(<"$inspect_output")"
    rm -f -- "$inspect_output"
  fi
  [[ "$digest" == *@sha256:* ]] || { vm_host_error 'Docker image has no immutable RepoDigest'; return 1; }
  printf 'Docker image identity: %s\n' "$digest"
  if ! vm_host_ssh_capture "$run_dir" "$output" \
    "env -i HOME=/home/mester PATH=/usr/local/bin:/usr/bin:/bin XDG_RUNTIME_DIR=/run/user/$(id -u) DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock /usr/bin/docker run --rm '$digest' sh -c 'printf DOCKER_CONTAINER_OK'" \
    "$VM_HOST_DOCKER_TIMEOUT"; then
    rm -f -- "$output"
    vm_host_error 'guest container execution failed; output suppressed'
    return 1
  fi
  if ! grep -Fxq 'DOCKER_CONTAINER_OK' "$output"; then
    rm -f -- "$output"
    vm_host_error 'container exited without the required output marker'
    return 1
  fi
  rm -f -- "$output"
  printf 'DOCKER_CONTAINER_OK\n' || return 1
}

vm_host_smoke() {
  local selected_workspace="" credential_file="" arg
  while (($#)); do
    arg="$1"; shift
    case "$arg" in
      --workspace) (($#)) || { vm_host_error '--workspace requires a value'; return 2; }; [[ -z "$selected_workspace" ]] || { vm_host_error '--workspace may appear only once'; return 2; }; selected_workspace="$1"; shift ;;
      --credentials-file) (($#)) || { vm_host_error '--credentials-file requires a value'; return 2; }; [[ -z "$credential_file" ]] || { vm_host_error '--credentials-file may appear only once'; return 2; }; credential_file="$1"; shift ;;
      *) vm_host_error "unsupported smoke argument: $arg"; return 2 ;;
    esac
  done
  [[ -n "$selected_workspace" && -n "$credential_file" ]] || {
    vm_host_error 'usage: lab-vm smoke --workspace <fresh-workspace> --credentials-file <private-provider-json>'
    return 2
  }
  vm_host_require_kvm || return 1
  vm_host_require_commands || return 1
  local home state_root workspace
  home="$(vm_host_account_home)" || { vm_host_error 'could not resolve invoking account home'; return 1; }
  state_root="$home/.local/state/little-mester/v3-smoke"
  workspace="$(vm_host_validate_workspace "$selected_workspace" "$VM_HOST_SCRIPT_ROOT" "$state_root" "$credential_file")" || return 1
  vm_host_validate_credentials "$credential_file" "$workspace" "$VM_HOST_SCRIPT_ROOT" "$state_root" || return 1
  vm_host_check_workspace_filesystem "$workspace" || return 1
  if vm_host_is_under "$VM_HOST_SCRIPT_ROOT" "$workspace"; then
    vm_host_error 'candidate launcher and transitive source must remain outside the exported workspace'
    return 1
  else
    local under_status=$?
    (( under_status == 1 )) || return 1
  fi
  local model start_time run_id vm_name ssh_port uid gid pubkey
  model="$(vm_host_credential_model "$credential_file")" || return 1
  start_time=$SECONDS
  state_root="$(vm_host_prepare_state_root "$home")" || return 1
  vm_host_check_run_capacity "$state_root" || return 1
  run_id="$(date -u +%Y%m%dT%H%M%SZ)-$(mktemp -u XXXXXXXX)" || { vm_host_error 'could not allocate run identifier'; return 1; }
  VM_HOST_RUN_DIR="$state_root/$run_id"
  (umask 077; mkdir -- "$VM_HOST_RUN_DIR") || return 1
  chmod 0700 "$VM_HOST_RUN_DIR" || return 1
  trap 'vm_host_exit_cleanup' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP
  printf 'Run state: %s\n' "$VM_HOST_RUN_DIR" || return 1
  uid="$(id -u)" || { vm_host_error 'could not resolve invoking UID'; return 1; }
  gid="$(id -g)" || { vm_host_error 'could not resolve invoking GID'; return 1; }
  vm_name="v3-smoke-${run_id//[^A-Za-z0-9-]/}"
  vm_name="${vm_name:0:31}"
  ssh-keygen -q -t ed25519 -N '' -C "little-mester-v3-$run_id" -f "$VM_HOST_RUN_DIR/id_vm_control" || {
    vm_host_error 'could not generate run-owned SSH control key'
    return 1
  }
  chmod 0600 "$VM_HOST_RUN_DIR/id_vm_control" || return 1
  pubkey="$(<"$VM_HOST_RUN_DIR/id_vm_control.pub")" || { vm_host_error 'could not read run-owned SSH public key'; return 1; }
  vm_host_write_cloud_seed "$VM_HOST_RUN_DIR" "$vm_name" "$uid" "$gid" "$pubkey" || return 1
  vm_host_prepare_disk "$VM_HOST_RUN_DIR" || return 1
  vm_host_start_proxy "$VM_HOST_RUN_DIR" || return 1
  vm_host_start_virtiofs "$VM_HOST_RUN_DIR" "$workspace" || return 1
  ssh_port="$(vm_host_free_port)" || { vm_host_error 'could not allocate loopback SSH port'; return 1; }
  vm_host_start_qemu "$VM_HOST_RUN_DIR" "$vm_name" "$workspace" "$ssh_port" || return 1
  vm_host_wait_ssh "$VM_HOST_RUN_DIR" "$ssh_port" || return 1
  vm_host_provisioning_ready "$VM_HOST_RUN_DIR" || return 1
  printf 'VM_UP\n' || return 1
  vm_host_import_credentials "$VM_HOST_RUN_DIR" "$credential_file" || return 1
  vm_host_model_smoke "$VM_HOST_RUN_DIR" "$model" || return 1
  vm_host_docker_smoke "$VM_HOST_RUN_DIR" || return 1
  if (( SECONDS - start_time > VM_HOST_TOTAL_TIMEOUT )); then
    vm_host_error "smoke exceeded ${VM_HOST_TOTAL_TIMEOUT}s total budget"
    return 1
  fi
  VM_HOST_SMOKE_COMPLETE_PENDING=1
}
