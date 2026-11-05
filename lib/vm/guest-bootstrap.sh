#!/usr/bin/env bash
# Root-only first-boot provisioner for the native guest runtime.
# All downloads use the explicit public-only guest proxy. There is no agent
# container, host install, broad Docker daemon rewrite or retry-time latest lookup.
set -Eeuo pipefail
umask 077

VM_GUEST_ROOT="${VM_GUEST_ROOT:-/}"
VM_GUEST_TEST_MODE="${VM_GUEST_TEST_MODE:-0}"
VM_GUEST_APT_GET_BIN="${VM_GUEST_APT_GET_BIN:-apt-get}"
VM_GUEST_CURL_BIN="${VM_GUEST_CURL_BIN:-curl}"
VM_GUEST_NPM_BIN="${VM_GUEST_NPM_BIN:-npm}"
VM_GUEST_NODE_BIN="${VM_GUEST_NODE_BIN:-node}"
VM_GUEST_OPENCODE_BIN="${VM_GUEST_OPENCODE_BIN:-opencode}"
VM_GUEST_AIDER_BIN="${VM_GUEST_AIDER_BIN:-/opt/little-mester/python-tools/bin/aider}"
VM_GUEST_SYSTEMCTL_BIN="${VM_GUEST_SYSTEMCTL_BIN:-systemctl}"
VM_GUEST_DOCKER_BIN="${VM_GUEST_DOCKER_BIN:-docker}"
VM_GUEST_RUNUSER_BIN="${VM_GUEST_RUNUSER_BIN:-runuser}"
VM_GUEST_USERMOD_BIN="${VM_GUEST_USERMOD_BIN:-usermod}"
VM_GUEST_STAT_BIN="${VM_GUEST_STAT_BIN:-stat}"
VM_GUEST_MOUNTPOINT_BIN="${VM_GUEST_MOUNTPOINT_BIN:-mountpoint}"
VM_GUEST_FINDMNT_BIN="${VM_GUEST_FINDMNT_BIN:-findmnt}"
VM_GUEST_PYTHON="${VM_GUEST_PYTHON:-python3}"
VM_GUEST_PYTHON_TOOLS="${VM_GUEST_PYTHON_TOOLS:-/usr/local/sbin/lab-vm-python-tools}"
VM_GUEST_CONFIG_RENDERER="${VM_GUEST_CONFIG_RENDERER:-/usr/local/libexec/lab-vm-opencode-config.py}"
VM_GUEST_HASH_HELPER="${VM_GUEST_HASH_HELPER:-/usr/local/libexec/lab-vm-guest-hash.sh}"
VM_GUEST_PROXY="http://10.0.2.100:3128"
VM_GUEST_VERSION_RE='^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$'
VM_GUEST_STAGE=''

vm_guest_bootstrap_error() {
  printf 'GUEST BOOTSTRAP FAILED: %s\n' "$*" >&2
  return 1
}

vm_guest_rooted() {
  local path="$1"
  if [[ "$VM_GUEST_ROOT" == / ]]; then
    printf '%s\n' "$path"
  else
    printf '%s%s\n' "${VM_GUEST_ROOT%/}" "$path"
  fi
}

vm_guest_chown_mester() {
  local uid="$1" gid="$2"
  shift 2
  if [[ "$VM_GUEST_TEST_MODE" == 1 ]]; then
    return 0
  fi
  chown -R "$uid:$gid" -- "$@"
}

vm_guest_require_root() {
  if [[ "$VM_GUEST_TEST_MODE" == 1 ]]; then
    [[ "$VM_GUEST_ROOT" != / && "$VM_GUEST_ROOT" == /* && -d "$VM_GUEST_ROOT" && ! -L "$VM_GUEST_ROOT" ]] || {
      vm_guest_bootstrap_error 'Fixture mode requires a separate existing temporary guest root.'
      return 1
    }
    return 0
  fi
  (( EUID == 0 )) || {
    vm_guest_bootstrap_error 'First-boot guest provisioning must run as root.'
    return 1
  }
  [[ "$VM_GUEST_ROOT" == / ]] || {
    vm_guest_bootstrap_error 'Non-system guest roots are fixture-only.'
    return 1
  }
}

vm_guest_parse_args() {
  VM_GUEST_RUNTIME='' VM_GUEST_AGENT='' VM_GUEST_DEPTH='' VM_GUEST_MAX_DEPTH=''
  VM_GUEST_VERSION_SELECTOR='' VM_GUEST_PAYLOAD_SHA256='' VM_GUEST_UID='' VM_GUEST_GID=''
  VM_GUEST_WORKSPACE='' VM_GUEST_SOURCE_ROOT='' VM_GUEST_PROXY_ARG=''
  VM_GUEST_MODEL_PROVIDER='' VM_GUEST_MODEL_PORT='' VM_GUEST_MODEL_ID=''
  while (($#)); do
    case "$1" in
      --runtime|--agent|--depth|--max-depth|--opencode-version|--payload-sha256|--uid|--gid|--workspace|--source-root|--proxy|--model-provider|--model-port|--model-id)
        (($# >= 2)) || { vm_guest_bootstrap_error 'A bootstrap option is missing its value.'; return 2; }
        case "$1" in
          --runtime) VM_GUEST_RUNTIME="$2" ;;
          --agent) VM_GUEST_AGENT="$2" ;;
          --depth) VM_GUEST_DEPTH="$2" ;;
          --max-depth) VM_GUEST_MAX_DEPTH="$2" ;;
          --opencode-version) VM_GUEST_VERSION_SELECTOR="$2" ;;
          --payload-sha256) VM_GUEST_PAYLOAD_SHA256="$2" ;;
          --uid) VM_GUEST_UID="$2" ;;
          --gid) VM_GUEST_GID="$2" ;;
          --workspace) VM_GUEST_WORKSPACE="$2" ;;
          --source-root) VM_GUEST_SOURCE_ROOT="$2" ;;
          --proxy) VM_GUEST_PROXY_ARG="$2" ;;
          --model-provider) VM_GUEST_MODEL_PROVIDER="$2" ;;
          --model-port) VM_GUEST_MODEL_PORT="$2" ;;
          --model-id) VM_GUEST_MODEL_ID="$2" ;;
        esac
        shift 2 ;;
      *) vm_guest_bootstrap_error 'Unknown guest bootstrap option.'; return 2 ;;
    esac
  done
  [[ "$VM_GUEST_RUNTIME" == docker || "$VM_GUEST_RUNTIME" == none ]] || { vm_guest_bootstrap_error 'Unsupported guest workload runtime.'; return 2; }
  [[ "$VM_GUEST_AGENT" == opencode || "$VM_GUEST_AGENT" == aider ]] || { vm_guest_bootstrap_error 'Unsupported native guest harness.'; return 2; }
  [[ "$VM_GUEST_DEPTH" =~ ^(0|[1-9][0-9]?)$ && "$VM_GUEST_MAX_DEPTH" =~ ^(0|[1-9][0-9]?)$ ]] || { vm_guest_bootstrap_error 'Invalid recursion identity.'; return 2; }
  (( 10#$VM_GUEST_DEPTH <= 10#$VM_GUEST_MAX_DEPTH && 10#$VM_GUEST_MAX_DEPTH <= 32 )) || { vm_guest_bootstrap_error 'Recursion identity is outside supported bounds.'; return 2; }
  [[ "$VM_GUEST_VERSION_SELECTOR" == latest || "$VM_GUEST_VERSION_SELECTOR" =~ ^v?2\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$ ]] || { vm_guest_bootstrap_error 'OpenCode must use the supported V2 version selector.'; return 2; }
  [[ "$VM_GUEST_PAYLOAD_SHA256" =~ ^[a-f0-9]{64}$ ]] || { vm_guest_bootstrap_error 'Invalid guest payload identity.'; return 2; }
  [[ "$VM_GUEST_UID" =~ ^[1-9][0-9]{0,9}$ && "$VM_GUEST_GID" =~ ^[1-9][0-9]{0,9}$ ]] || { vm_guest_bootstrap_error 'Invalid guest workspace ownership identity.'; return 2; }
  (( 10#$VM_GUEST_UID <= 2147483647 && 10#$VM_GUEST_GID <= 2147483647 )) || { vm_guest_bootstrap_error 'Guest ownership identity is outside supported bounds.'; return 2; }
  [[ "$VM_GUEST_WORKSPACE" == /workspace && "$VM_GUEST_SOURCE_ROOT" == /workspace ]] || { vm_guest_bootstrap_error 'Only the workspace-tag mount may be used as guest source.'; return 2; }
  [[ "$VM_GUEST_PROXY_ARG" == "$VM_GUEST_PROXY" ]] || { vm_guest_bootstrap_error 'The explicit guest proxy is required.'; return 2; }
  [[ "$VM_GUEST_MODEL_PROVIDER" == none || "$VM_GUEST_MODEL_PROVIDER" == ollama ||
     "$VM_GUEST_MODEL_PROVIDER" == llamacpp || "$VM_GUEST_MODEL_PROVIDER" == remote ]] || {
    vm_guest_bootstrap_error 'Unsupported selected model provider.'
    return 2
  }
  [[ "$VM_GUEST_MODEL_ID" != *$'\n'* && "$VM_GUEST_MODEL_ID" != *$'\r'* &&
     ${#VM_GUEST_MODEL_ID} -le 512 ]] || { vm_guest_bootstrap_error 'Selected model id is invalid.'; return 2; }
  case "$VM_GUEST_MODEL_PROVIDER" in
    ollama|llamacpp)
      if [[ ! "$VM_GUEST_MODEL_PORT" =~ ^[1-9][0-9]{0,4}$ ]] ||
        (( 10#$VM_GUEST_MODEL_PORT > 65535 )); then
        vm_guest_bootstrap_error 'Selected local model provider requires a valid host port.'
        return 2
      fi ;;
    none|remote)
      [[ -z "$VM_GUEST_MODEL_PORT" ]] || { vm_guest_bootstrap_error 'Model port is invalid for a nonlocal provider.'; return 2; } ;;
  esac
}

vm_guest_set_proxy_environment() {
  export HTTP_PROXY="$VM_GUEST_PROXY" HTTPS_PROXY="$VM_GUEST_PROXY"
  export http_proxy="$VM_GUEST_PROXY" https_proxy="$VM_GUEST_PROXY"
  export NO_PROXY='localhost,127.0.0.1,10.0.2.100' no_proxy='localhost,127.0.0.1,10.0.2.100'
  export npm_config_proxy="$VM_GUEST_PROXY" npm_config_https_proxy="$VM_GUEST_PROXY"
  export npm_config_noproxy='localhost,127.0.0.1,10.0.2.100'
  export PIP_PROXY="$VM_GUEST_PROXY"
}

vm_guest_system_preflight() {
  local os_release
  os_release="$(vm_guest_rooted /etc/os-release)"
  [[ -f "$os_release" && ! -L "$os_release" ]] || { vm_guest_bootstrap_error 'Guest OS identity is unavailable.'; return 1; }
  "$VM_GUEST_PYTHON" - "$os_release" <<'PY'
import sys
values = {}
for line in open(sys.argv[1], encoding="utf-8"):
    if "=" in line:
        key, value = line.rstrip("\n").split("=", 1)
        values[key] = value.strip('"')
raise SystemExit(0 if values.get("ID") == "ubuntu" and values.get("VERSION_ID") == "24.04" else 1)
PY
  local rc=$?
  (( rc == 0 )) || { vm_guest_bootstrap_error 'Guest bootstrap requires the pinned Ubuntu 24.04 image.'; return 1; }
}

vm_guest_install_base_packages() {
  export DEBIAN_FRONTEND=noninteractive
  vm_guest_apt_get update
  vm_guest_apt_get install -y --no-install-recommends \
    ca-certificates curl git python3 python3-pip python3-venv build-essential
}

vm_guest_apt_get() {
  "$VM_GUEST_APT_GET_BIN" \
    -o "Acquire::http::Proxy=$VM_GUEST_PROXY" \
    -o "Acquire::https::Proxy=$VM_GUEST_PROXY" "$@"
}

vm_guest_node_major() {
  local raw major
  command -v "$VM_GUEST_NODE_BIN" >/dev/null 2>&1 || return 1
  raw="$("$VM_GUEST_NODE_BIN" --version 2>/dev/null)" || return 1
  raw="${raw#v}"
  [[ "$raw" =~ ^([0-9]+)\.[0-9]+\.[0-9]+ ]] || return 1
  major="${BASH_REMATCH[1]}"
  printf '%s\n' "$major"
}

vm_guest_install_node() {
  local major keyring source_list
  major="$(vm_guest_node_major || true)"
  if [[ "$major" =~ ^[0-9]+$ ]] && (( major >= 20 )); then
    command -v "$VM_GUEST_NPM_BIN" >/dev/null 2>&1 || { vm_guest_bootstrap_error 'Node is present without npm.'; return 1; }
    return 0
  fi
  keyring="$(vm_guest_rooted /etc/apt/keyrings/nodesource.asc)"
  source_list="$(vm_guest_rooted /etc/apt/sources.list.d/nodesource.list)"
  install -d -m 0755 "$(dirname "$keyring")" "$(dirname "$source_list")"
  "$VM_GUEST_CURL_BIN" --fail --silent --show-error --location \
    --proxy "$VM_GUEST_PROXY" --noproxy '' --output "$keyring" \
    https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key
  [[ -s "$keyring" ]] || { vm_guest_bootstrap_error 'Node repository key download was empty.'; return 1; }
  chmod 0644 "$keyring"
  printf 'deb [arch=amd64 signed-by=%s] https://deb.nodesource.com/node_22.x nodistro main\n' "$keyring" > "$source_list"
  chmod 0644 "$source_list"
  vm_guest_apt_get update
  vm_guest_apt_get install -y --no-install-recommends nodejs
  major="$(vm_guest_node_major)" || { vm_guest_bootstrap_error 'Supported Node.js is unavailable after package setup.'; return 1; }
  (( major >= 20 )) || { vm_guest_bootstrap_error 'Node.js 20 or newer is required.'; return 1; }
  command -v "$VM_GUEST_NPM_BIN" >/dev/null 2>&1 || { vm_guest_bootstrap_error 'npm is unavailable after package setup.'; return 1; }
}

vm_guest_install_docker() {
  local keyring source_list dropin dropin_tmp docker_config
  keyring="$(vm_guest_rooted /etc/apt/keyrings/docker.asc)"
  source_list="$(vm_guest_rooted /etc/apt/sources.list.d/docker.list)"
  install -d -m 0755 "$(dirname "$keyring")" "$(dirname "$source_list")"
  "$VM_GUEST_CURL_BIN" --fail --silent --show-error --location \
    --proxy "$VM_GUEST_PROXY" --noproxy '' --output "$keyring" \
    https://download.docker.com/linux/ubuntu/gpg
  [[ -s "$keyring" ]] || { vm_guest_bootstrap_error 'Docker repository key download was empty.'; return 1; }
  chmod 0644 "$keyring"
  printf 'deb [arch=amd64 signed-by=%s] https://download.docker.com/linux/ubuntu noble stable\n' "$keyring" > "$source_list"
  chmod 0644 "$source_list"
  vm_guest_apt_get update
  vm_guest_apt_get install -y --no-install-recommends \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

  dropin="$(vm_guest_rooted /etc/systemd/system/docker.service.d/80-lab-vm-proxy.conf)"
  install -d -m 0755 "$(dirname "$dropin")"
  dropin_tmp="$(mktemp "$(dirname "$dropin")/.80-lab-vm-proxy.XXXXXX")"
  cat > "$dropin_tmp" <<'PROXY'
[Service]
Environment="HTTP_PROXY=http://10.0.2.100:3128"
Environment="HTTPS_PROXY=http://10.0.2.100:3128"
Environment="http_proxy=http://10.0.2.100:3128"
Environment="https_proxy=http://10.0.2.100:3128"
Environment="NO_PROXY=localhost,127.0.0.1,10.0.2.100"
Environment="no_proxy=localhost,127.0.0.1,10.0.2.100"
PROXY
  chmod 0644 "$dropin_tmp"
  if [[ -e "$dropin" || -L "$dropin" ]]; then
    if [[ ! -f "$dropin" || -L "$dropin" ]]; then
      rm -f -- "$dropin_tmp"
      vm_guest_bootstrap_error 'An unrelated Docker service proxy configuration exists; it was preserved.'
      return 1
    fi
    if ! cmp -s -- "$dropin_tmp" "$dropin"; then
      rm -f -- "$dropin_tmp"
      vm_guest_bootstrap_error 'An unrelated Docker service proxy configuration exists; it was preserved.'
      return 1
    fi
    rm -f -- "$dropin_tmp"
  else
    mv -- "$dropin_tmp" "$dropin"
  fi
  "$VM_GUEST_SYSTEMCTL_BIN" daemon-reload
  "$VM_GUEST_SYSTEMCTL_BIN" enable --now docker.service
  "$VM_GUEST_USERMOD_BIN" -aG docker mester
  "$VM_GUEST_DOCKER_BIN" compose version >/dev/null

  docker_config="$(vm_guest_rooted /home/mester/.docker/config.json)"
  "$VM_GUEST_PYTHON" - "$docker_config" "$VM_GUEST_PROXY" <<'PY'
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
proxy = sys.argv[2]
path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
if path.exists() or path.is_symlink():
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SystemExit(1)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise SystemExit(1) from None
    if not isinstance(data, dict):
        raise SystemExit(1)
else:
    data = {}
proxies = data.setdefault("proxies", {})
if not isinstance(proxies, dict):
    raise SystemExit(1)
default = proxies.setdefault("default", {})
if not isinstance(default, dict):
    raise SystemExit(1)
default.update({"httpProxy": proxy, "httpsProxy": proxy, "noProxy": "localhost,127.0.0.1,10.0.2.100"})
payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
fd, temporary = tempfile.mkstemp(prefix=".docker-config-", dir=path.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
  vm_guest_chown_mester "$VM_GUEST_UID" "$VM_GUEST_GID" "$(dirname "$docker_config")" "$docker_config"
}

vm_guest_install_recursion_tools() {
  vm_guest_apt_get install -y --no-install-recommends \
    qemu-system-x86 qemu-utils virtiofsd cloud-image-utils cloud-init genisoimage \
    socat bats shellcheck
  local tool
  for tool in qemu-system-x86_64 qemu-img virtiofsd cloud-localds cloud-init genisoimage socat bats shellcheck; do
    command -v "$tool" >/dev/null 2>&1 || {
      vm_guest_bootstrap_error "A recursion validation tool is missing: $tool"
      return 1
    }
  done
}

vm_guest_validate_workspace_mount() {
  local fstype mounts owner rc
  "$VM_GUEST_MOUNTPOINT_BIN" -q "$VM_GUEST_WORKSPACE" || {
    vm_guest_bootstrap_error 'The workspace tag is not mounted.'
    return 1
  }
  fstype="$("$VM_GUEST_FINDMNT_BIN" --noheadings --output FSTYPE --target "$VM_GUEST_WORKSPACE" 2>/dev/null | tr -d '[:space:]')" || return 1
  [[ "$fstype" == virtiofs ]] || { vm_guest_bootstrap_error 'The workspace mount is not virtiofs.'; return 1; }
  mounts="$("$VM_GUEST_FINDMNT_BIN" --noheadings --output TARGET,FSTYPE --types virtiofs 2>/dev/null)" || return 1
  "$VM_GUEST_PYTHON" - "$mounts" <<'PY'
import sys
rows = [line.split() for line in sys.argv[1].splitlines() if line.split()]
raise SystemExit(0 if rows == [["/workspace", "virtiofs"]] else 1)
PY
  rc=$?
  (( rc == 0 )) || { vm_guest_bootstrap_error 'Only the selected workspace may be mounted with virtiofs.'; return 1; }
  owner="$("$VM_GUEST_STAT_BIN" -c '%u:%g' -- "$VM_GUEST_WORKSPACE")" || return 1
  [[ "$owner" == "$VM_GUEST_UID:$VM_GUEST_GID" ]] || {
    vm_guest_bootstrap_error 'Workspace ownership does not match the guest control identity.'
    return 1
  }
}

vm_guest_verify_payload_hash() {
  local helper workspace actual
  helper="$(vm_guest_rooted "$VM_GUEST_HASH_HELPER")"
  workspace="$(vm_guest_rooted "$VM_GUEST_SOURCE_ROOT")"
  [[ -f "$helper" && ! -L "$helper" ]] || {
    vm_guest_bootstrap_error 'Guest payload hash helper is missing.'
    return 1
  }
  actual="$(bash -c 'source "$1"; shift; vm_guest_payload_hash "$@"' _ \
    "$helper" "$workspace" "$VM_GUEST_RUNTIME" "$VM_GUEST_AGENT" \
    "$VM_GUEST_VERSION_SELECTOR" "$VM_GUEST_DEPTH" "$VM_GUEST_MAX_DEPTH" \
    "$VM_GUEST_MODEL_PROVIDER" "$VM_GUEST_MODEL_PORT" "$VM_GUEST_MODEL_ID")" || {
    vm_guest_bootstrap_error 'Guest workspace payload could not be hashed safely.'
    return 1
  }
  [[ "$actual" == "$VM_GUEST_PAYLOAD_SHA256" ]] || {
    vm_guest_bootstrap_error 'Guest workspace payload differs from the host-selected payload hash.'
    return 1
  }
}

vm_guest_copy_tree() {
  local source="$1" destination="$2" mode="$3"
  "$VM_GUEST_PYTHON" - "$source" "$destination" "$mode" <<'PY'
import os
import shutil
import stat
import sys
from pathlib import Path

source, destination, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
if source.is_symlink() or not source.is_dir() or destination.exists() or destination.is_symlink():
    raise SystemExit(1)
source_root = source.resolve(strict=True)
excluded = {".git", "node_modules", "dist", "__pycache__"}
if mode == "mcp":
    excluded.update({"tests", "coverage"})

def copy_directory(src, dst):
    info = src.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise SystemExit(1)
    dst.mkdir(mode=0o700)
    with os.scandir(src) as entries:
        for entry in sorted(entries, key=lambda item: item.name):
            if entry.name in excluded:
                continue
            item = Path(entry.path)
            target = dst / entry.name
            item_info = item.lstat()
            if stat.S_ISLNK(item_info.st_mode):
                link_text = os.readlink(item)
                if Path(link_text).is_absolute():
                    raise SystemExit(1)
                try:
                    resolved = item.resolve(strict=True)
                except (OSError, RuntimeError):
                    raise SystemExit(1) from None
                if not resolved.is_file() or not resolved.is_relative_to(source_root):
                    raise SystemExit(1)
                target.symlink_to(link_text)
            if stat.S_ISDIR(item_info.st_mode):
                copy_directory(item, target)
            elif stat.S_ISREG(item_info.st_mode) and item_info.st_size <= 64 * 1024 * 1024:
                shutil.copyfile(item, target, follow_symlinks=False)
                os.chmod(target, stat.S_IMODE(item_info.st_mode))
            elif not stat.S_ISLNK(item_info.st_mode):
                raise SystemExit(1)

copy_directory(source, destination)
PY
}

vm_guest_normalize_version() {
  local version="$1"
  version="${version#v}"
  [[ "$version" =~ $VM_GUEST_VERSION_RE ]] || return 1
  printf '%s\n' "$version"
}

vm_guest_read_install_manifest() {
  local path="$1"
  "$VM_GUEST_PYTHON" - "$path" "$VM_GUEST_PAYLOAD_SHA256" "$VM_GUEST_RUNTIME" \
    "$VM_GUEST_AGENT" "$VM_GUEST_DEPTH" "$VM_GUEST_MAX_DEPTH" \
    "$VM_GUEST_VERSION_SELECTOR" "$VM_GUEST_TEST_MODE" <<'PY'
import json
import re
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload, runtime, agent, depth, maximum, selector, test_mode = sys.argv[2:]
try:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError
    if test_mode != "1" and info.st_uid != 0:
        raise ValueError
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["payload_sha256"] == payload
    assert data["runtime"] == runtime
    assert data["agent"] == agent
    assert data["depth"] == int(depth) and data["max_depth"] == int(maximum)
    assert data["version_selector"] == selector
    assert data["opencode_package"] == "@opencode/cli"
    version = data["opencode_version"]
    assert isinstance(version, str) and re.fullmatch(r"2\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version)
except Exception:
    raise SystemExit(1)
print(version)
PY
}

vm_guest_write_install_manifest() {
  local path="$1" version="$2"
  "$VM_GUEST_PYTHON" - "$path" "$VM_GUEST_PAYLOAD_SHA256" "$VM_GUEST_RUNTIME" \
    "$VM_GUEST_AGENT" "$VM_GUEST_DEPTH" "$VM_GUEST_MAX_DEPTH" \
    "$VM_GUEST_VERSION_SELECTOR" "$version" <<'PY'
import json
import os
import re
import tempfile
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload, runtime, agent, depth, maximum, selector, version = sys.argv[2:]
if not re.fullmatch(r"[a-f0-9]{64}", payload) or not re.fullmatch(
    r"2\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version
):
    raise SystemExit(1)
path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
data = {
    "schema_version": 1,
    "payload_sha256": payload,
    "runtime": runtime,
    "agent": agent,
    "depth": int(depth),
    "max_depth": int(maximum),
    "version_selector": selector,
    "opencode_package": "@opencode/cli",
    "opencode_version": version,
}
encoded = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()
fd, temporary = tempfile.mkstemp(prefix=".install-manifest-", dir=path.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    if path.exists() or path.is_symlink():
        raise SystemExit(1)
    os.link(temporary, path, follow_symlinks=False)
    os.unlink(temporary)
    temporary = ""
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    if temporary and os.path.exists(temporary):
        os.unlink(temporary)
PY
}

vm_guest_resolve_opencode_version() {
  local manifest="$1" version raw
  if [[ -e "$manifest" || -L "$manifest" ]]; then
    version="$(vm_guest_read_install_manifest "$manifest")" || {
      vm_guest_bootstrap_error 'Saved install manifest does not match this fresh setup; it was preserved.'
      return 1
    }
    printf '%s\n' "$version"
    return 0
  fi
  if [[ "$VM_GUEST_VERSION_SELECTOR" == latest ]]; then
    raw="$("$VM_GUEST_NPM_BIN" view @opencode/cli@latest version --json)" || {
      vm_guest_bootstrap_error 'OpenCode latest version could not be resolved through the guest proxy.'
      return 1
    }
    version="$("$VM_GUEST_PYTHON" -c '
import json,re,sys
try:
    value=json.loads(sys.stdin.read())
    assert isinstance(value,str)
    value=value.removeprefix("v")
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?",value)
except Exception:
    raise SystemExit(1)
print(value)
' <<<"$raw")" || { vm_guest_bootstrap_error 'OpenCode registry returned an invalid version.'; return 1; }
  else
    version="$(vm_guest_normalize_version "$VM_GUEST_VERSION_SELECTOR")" || { vm_guest_bootstrap_error 'OpenCode version is invalid.'; return 1; }
  fi
  vm_guest_write_install_manifest "$manifest" "$version" || {
    vm_guest_bootstrap_error 'Exact OpenCode install manifest could not be saved safely.'
    return 1
  }
  printf '%s\n' "$version"
}

vm_guest_install_opencode() {
  local manifest="$1" version reported rc adapter_help
  version="$(vm_guest_resolve_opencode_version "$manifest")" || return 1
  [[ "$version" =~ ^2\. ]] || {
    vm_guest_bootstrap_error 'This OpenCode version is outside the verified V2 adapter range.'
    return 1
  }
  "$VM_GUEST_NPM_BIN" install --global --no-audit --no-fund "@opencode/cli@$version"
  reported="$("$VM_GUEST_OPENCODE_BIN" --version 2>/dev/null)" || { vm_guest_bootstrap_error 'Installed OpenCode executable failed its version check.'; return 1; }
  "$VM_GUEST_PYTHON" - "$reported" "$version" <<'PY'
import re
import sys
reported, expected = sys.argv[1:]
raise SystemExit(0 if re.search(r"(?<![0-9A-Za-z.])" + re.escape(expected) + r"(?![0-9A-Za-z.])", reported) else 1)
PY
  rc=$?
  (( rc == 0 )) || { vm_guest_bootstrap_error 'OpenCode executable version differs from the saved exact version.'; return 1; }
  adapter_help="$("$VM_GUEST_OPENCODE_BIN" run --help 2>&1)" || {
    vm_guest_bootstrap_error 'Installed OpenCode does not expose the V2 run adapter help.'
    return 1
  }
  [[ "$adapter_help" == *--auto* ]] || {
    vm_guest_bootstrap_error 'Installed OpenCode does not advertise the verified --auto permission option.'
    return 1
  }
  VM_GUEST_RESOLVED_VERSION="$version"
}

vm_guest_check_aider_adapter() {
  local version help
  [[ -x "$VM_GUEST_AIDER_BIN" ]] || {
    vm_guest_bootstrap_error 'Aider is missing from the isolated guest Python tools environment.'
    return 1
  }
  version="$("$VM_GUEST_AIDER_BIN" --version 2>/dev/null)" || {
    vm_guest_bootstrap_error 'The installed Aider executable failed its version check.'
    return 1
  }
  [[ "$version" =~ [Aa]ider[[:space:]]+[0-9]+\.[0-9]+ ]] || {
    vm_guest_bootstrap_error 'The installed Aider version output is not recognized.'
    return 1
  }
  help="$("$VM_GUEST_AIDER_BIN" --help 2>&1)" || {
    vm_guest_bootstrap_error 'The installed Aider command failed its adapter help check.'
    return 1
  }
  [[ "$help" == *--model* ]] || {
    vm_guest_bootstrap_error 'The installed Aider command does not advertise the supported --model adapter.'
    return 1
  }
  VM_GUEST_AIDER_VERSION="$version"
}

vm_guest_run_python_tools() {
  local script
  script="$(vm_guest_rooted "$VM_GUEST_PYTHON_TOOLS")"
  [[ -f "$script" && ! -L "$script" ]] || { vm_guest_bootstrap_error 'Guest Python tooling installer is missing.'; return 1; }
  VM_PYTHON_TOOLS_DIR="$(vm_guest_rooted /opt/little-mester/python-tools)" \
    LAB_VM_PROXY="$VM_GUEST_PROXY" bash "$script" install
}

vm_guest_build_private_payload() {
  local share_root final stage workspace_source mcp_source config_dir renderer
  local update_mode="${VM_GUEST_UPDATE_MODE:-0}" backup file destination staged_config
  local -a config_stages=()
  share_root="$(vm_guest_rooted /home/mester/.local/share/little-mester)"
  final="$share_root/guest-runtime"
  workspace_source="$(vm_guest_rooted /workspace)"
  mcp_source="$workspace_source/little-mester-mcp"
  config_dir="$(vm_guest_rooted /home/mester/.config/opencode)"
  renderer="$(vm_guest_rooted "$VM_GUEST_CONFIG_RENDERER")"
  install -d -m 0700 "$share_root" || return 1
  if [[ "$update_mode" == 1 ]]; then
    [[ -d "$final" && ! -L "$final" ]] || {
      vm_guest_bootstrap_error 'Existing private guest runtime is missing or unsafe; it was preserved.'
      return 1
    }
    [[ -d "$config_dir" && ! -L "$config_dir" ]] || {
      vm_guest_bootstrap_error 'Existing generated config directory is missing or unsafe; it was preserved.'
      return 1
    }
    for file in opencode.json cli.json; do
      destination="$config_dir/$file"
      [[ -f "$destination" && ! -L "$destination" ]] || {
        vm_guest_bootstrap_error 'Existing generated config is missing or unsafe; no source was overwritten.'
        return 1
      }
    done
  else
    [[ ! -e "$final" && ! -L "$final" ]] || { vm_guest_bootstrap_error 'A guest runtime payload already exists; it was not replaced.'; return 1; }
  fi
  stage="$(mktemp -d "$share_root/.guest-runtime.XXXXXX")" || return 1
  VM_GUEST_STAGE="$stage"
  trap 'if [[ -n "${VM_GUEST_STAGE:-}" && -d "$VM_GUEST_STAGE" ]]; then rm -rf -- "$VM_GUEST_STAGE"; fi' EXIT

  vm_guest_copy_tree "$workspace_source/agent/opencode" "$stage/opencode" opencode || return 1
  vm_guest_copy_tree "$mcp_source" "$stage/mcp" mcp || return 1
  vm_guest_chown_mester "$VM_GUEST_UID" "$VM_GUEST_GID" "$stage" || return 1

  LAB_VM_PROXY="$VM_GUEST_PROXY" "$VM_GUEST_RUNUSER_BIN" -u mester -- env \
    HOME="$(vm_guest_rooted /home/mester)" \
    HTTP_PROXY="$VM_GUEST_PROXY" HTTPS_PROXY="$VM_GUEST_PROXY" \
    http_proxy="$VM_GUEST_PROXY" https_proxy="$VM_GUEST_PROXY" \
    NO_PROXY='localhost,127.0.0.1,10.0.2.100' no_proxy='localhost,127.0.0.1,10.0.2.100' \
    npm_config_proxy="$VM_GUEST_PROXY" npm_config_https_proxy="$VM_GUEST_PROXY" \
    npm_config_noproxy='localhost,127.0.0.1,10.0.2.100' \
    npm_config_cache="$(vm_guest_rooted /home/mester/.cache/lab-vm-npm)" \
    "$VM_GUEST_NPM_BIN" ci --prefix "$stage/mcp" || return 1
  LAB_VM_PROXY="$VM_GUEST_PROXY" "$VM_GUEST_RUNUSER_BIN" -u mester -- env \
    HOME="$(vm_guest_rooted /home/mester)" \
    HTTP_PROXY="$VM_GUEST_PROXY" HTTPS_PROXY="$VM_GUEST_PROXY" \
    http_proxy="$VM_GUEST_PROXY" https_proxy="$VM_GUEST_PROXY" \
    NO_PROXY='localhost,127.0.0.1,10.0.2.100' no_proxy='localhost,127.0.0.1,10.0.2.100' \
    npm_config_proxy="$VM_GUEST_PROXY" npm_config_https_proxy="$VM_GUEST_PROXY" \
    npm_config_noproxy='localhost,127.0.0.1,10.0.2.100' \
    npm_config_cache="$(vm_guest_rooted /home/mester/.cache/lab-vm-npm)" \
    "$VM_GUEST_NPM_BIN" run build --prefix "$stage/mcp" || return 1
  [[ -s "$stage/mcp/dist/mcp-server.js" ]] || { vm_guest_bootstrap_error 'Private MCP build output is missing.'; return 1; }

  install -d -m 0700 "$stage/generated" || return 1
  "$VM_GUEST_PYTHON" "$renderer" \
    --input "$stage/opencode/opencode.json" \
    --output "$stage/generated/opencode.json" \
    --cli-output "$stage/generated/cli.json" \
    --mcp-cwd "$final/mcp" \
    --plugin-root "$final/opencode/plugins" \
    --plugin-source "$stage/opencode/plugins" \
    --model-provider "$VM_GUEST_MODEL_PROVIDER" \
    --model-id "$VM_GUEST_MODEL_ID" || return 1
  if ! "$VM_GUEST_PYTHON" - "$stage/generated/opencode.json" "$stage/generated/cli.json" "$stage/mcp" <<'PY'
import json
import stat
import sys
from pathlib import Path

config_path, cli_path, mcp_dir = map(Path, sys.argv[1:])
config = json.loads(config_path.read_text(encoding="utf-8"))
cli = json.loads(cli_path.read_text(encoding="utf-8"))
server = config["mcp"]["servers"]["little-mester"]
assert server["type"] == "local"
assert server["command"] == ["node", "dist/mcp-server.js"]
assert server["cwd"].endswith("/guest-runtime/mcp")
assert (mcp_dir / "package-lock.json").is_file()
assert (mcp_dir / "dist/mcp-server.js").is_file()
assert isinstance(config.get("permissions", []), list)
assert isinstance(config.get("providers", {}), dict)
assert "mcpServers" not in config and "provider" not in config and "permission" not in config
assert all(isinstance(item, str) for item in cli.get("plugins", []))
for path in (config_path, cli_path):
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
PY
  then
    return 1
  fi

  vm_guest_chown_mester "$VM_GUEST_UID" "$VM_GUEST_GID" "$stage" || return 1
  if [[ "$update_mode" == 1 ]]; then
    backup="$(mktemp -d "$share_root/.guest-runtime-backup.XXXXXX")" || return 1
    chmod 0700 "$backup" || { rmdir -- "$backup" 2>/dev/null || true; return 1; }
    install -d -m 0700 "$backup/config" || { rmdir -- "$backup" 2>/dev/null || true; return 1; }
    for file in opencode.json cli.json; do
      install -m 0600 "$config_dir/$file" "$backup/config/$file" || {
        for staged_config in "${config_stages[@]}"; do rm -f -- "$staged_config"; done
        rm -rf -- "$backup"
        return 1
      }
      staged_config="$config_dir/.$file.update.$$.${RANDOM}"
      install -m 0600 "$stage/generated/$file" "$staged_config" || {
        rm -f -- "$staged_config"
        for staged_config in "${config_stages[@]}"; do rm -f -- "$staged_config"; done
        rm -rf -- "$backup"
        return 1
      }
      vm_guest_chown_mester "$VM_GUEST_UID" "$VM_GUEST_GID" "$staged_config" || {
        rm -f -- "$staged_config"
        for staged_config in "${config_stages[@]}"; do rm -f -- "$staged_config"; done
        rm -rf -- "$backup"
        return 1
      }
      config_stages+=("$staged_config")
    done
    mv -- "$final" "$backup/guest-runtime" || {
      for staged_config in "${config_stages[@]}"; do rm -f -- "$staged_config"; done
      rm -rf -- "$backup"
      return 1
    }
    if ! mv -- "$stage" "$final"; then
      mv -- "$backup/guest-runtime" "$final" || {
        for staged_config in "${config_stages[@]}"; do rm -f -- "$staged_config"; done
        vm_guest_bootstrap_error 'Runtime swap failed and the original remains in its private backup.'
        return 1
      }
      for staged_config in "${config_stages[@]}"; do rm -f -- "$staged_config"; done
      rm -rf -- "$backup"
      return 1
    fi
    VM_GUEST_STAGE=''
    local config_swap_failed=0
    local index=0
    for file in opencode.json cli.json; do
      staged_config="${config_stages[$index]}"
      index=$((index + 1))
      if [[ -z "$staged_config" || -L "$staged_config" ]] || ! mv -f -- "$staged_config" "$config_dir/$file"; then
        config_swap_failed=1
        break
      fi
    done
    if (( config_swap_failed )); then
      for staged_config in "${config_stages[@]}"; do rm -f -- "$staged_config"; done
      for file in opencode.json cli.json; do
        staged_config="$config_dir/.$file.restore.$$.${RANDOM}"
        if install -m 0600 "$backup/config/$file" "$staged_config" &&
          mv -f -- "$staged_config" "$config_dir/$file"; then
          :
        else
          rm -f -- "$staged_config"
          vm_guest_bootstrap_error 'Config rollback failed; original runtime and config copies remain in the private backup.'
          return 1
        fi
      done
      rm -rf -- "$final"
      mv -- "$backup/guest-runtime" "$final" || {
        vm_guest_bootstrap_error 'Runtime rollback failed; the original runtime remains in its private backup.'
        return 1
      }
      rm -rf -- "$backup"
      vm_guest_bootstrap_error 'Generated config replacement failed; prior runtime and config were restored.'
      return 1
    fi
    vm_guest_chown_mester "$VM_GUEST_UID" "$VM_GUEST_GID" "$config_dir" "$final" || {
      vm_guest_bootstrap_error 'Updated runtime ownership check failed; the prior runtime backup was retained.'
      return 1
    }
    rm -rf -- "$backup"
    trap - EXIT
  else
    mv -- "$stage" "$final" || return 1
    VM_GUEST_STAGE=''
    trap - EXIT
    install -d -m 0700 "$config_dir" || return 1
    for file in opencode.json cli.json; do
      destination="$config_dir/$file"
      [[ ! -e "$destination" && ! -L "$destination" ]] || {
        vm_guest_bootstrap_error 'Existing guest OpenCode config was preserved; a fresh profile is required.'
        return 1
      }
      install -m 0600 "$final/generated/$file" "$destination" || return 1
    done
    vm_guest_chown_mester "$VM_GUEST_UID" "$VM_GUEST_GID" "$config_dir" "$final" || return 1
  fi
}

vm_guest_write_success_marker() {
  local marker="$1" version="$2" recursion_tools="$3" aider_version="$4"
  "$VM_GUEST_PYTHON" - "$marker" "$VM_GUEST_PAYLOAD_SHA256" "$VM_GUEST_RUNTIME" \
    "$VM_GUEST_AGENT" "$VM_GUEST_DEPTH" "$VM_GUEST_MAX_DEPTH" "$version" \
    "$recursion_tools" "$aider_version" "$VM_GUEST_VERSION_SELECTOR" "$VM_GUEST_MODEL_PROVIDER" \
    "$VM_GUEST_MODEL_PORT" "$VM_GUEST_MODEL_ID" "$VM_GUEST_TEST_MODE" <<'PY'
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
payload, runtime, agent, depth, maximum, version, recursion_tools, aider_version, \
    version_selector, provider, model_port, model_id, test_mode = sys.argv[2:]
if test_mode != "1" and os.geteuid() != 0:
    raise SystemExit(1)
path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
if path.exists() or path.is_symlink():
    raise SystemExit(1)
data = {
    "schema_version": 1,
    "status": "ready",
    "agent": agent,
    "workload_runtime": runtime,
    "payload_sha256": payload,
    "opencode_version": version,
    "opencode_version_selector": version_selector,
    "aider_version": aider_version,
    "model_provider": provider,
    "model_port": int(model_port) if model_port else None,
    "model_id": model_id,
    "workspace_mount": "virtiofs",
    "mcp_build": True,
    "recursion_tools": recursion_tools == "true",
    "depth": int(depth),
    "max_depth": int(maximum),
}
encoded = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()
fd, temporary = tempfile.mkstemp(prefix=".guest-ready-", dir=path.parent)
try:
    os.fchmod(fd, 0o644)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    if path.exists() or path.is_symlink():
        raise SystemExit(1)
    os.link(temporary, path, follow_symlinks=False)
    os.unlink(temporary)
    temporary = ""
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o644:
        raise SystemExit(1)
    if test_mode != "1" and info.st_uid != 0:
        raise SystemExit(1)
finally:
    if temporary and os.path.exists(temporary):
        os.unlink(temporary)
PY
}

vm_guest_main() {
  vm_guest_parse_args "$@" || return $?
  vm_guest_require_root || return 1
  vm_guest_set_proxy_environment

  local marker manifest version major aider_version recursion_tools=false
  marker="$(vm_guest_rooted /var/lib/lab-vm/guest-ready.json)"
  manifest="$(vm_guest_rooted /var/lib/lab-vm/install-manifest.json)"
  [[ ! -e "$marker" && ! -L "$marker" ]] || { vm_guest_bootstrap_error 'A successful bootstrap marker already exists; no warm provisioning is allowed.'; return 1; }

  vm_guest_system_preflight
  vm_guest_validate_workspace_mount
  vm_guest_verify_payload_hash
  [[ -f "$(vm_guest_rooted /etc/apt/apt.conf.d/80lab-vm-proxy)" ]] || { vm_guest_bootstrap_error 'Explicit apt proxy configuration is missing.'; return 1; }
  vm_guest_install_base_packages
  vm_guest_install_node
  vm_guest_run_python_tools

  if [[ "$VM_GUEST_RUNTIME" == docker ]]; then
    vm_guest_install_docker
  fi
  if (( 10#$VM_GUEST_MAX_DEPTH > 10#$VM_GUEST_DEPTH )); then
    vm_guest_install_recursion_tools
    recursion_tools=true
  fi

  vm_guest_install_opencode "$manifest"
  version="$VM_GUEST_RESOLVED_VERSION"
  vm_guest_check_aider_adapter
  aider_version="$VM_GUEST_AIDER_VERSION"
  vm_guest_build_private_payload

  major="$(vm_guest_node_major)"
  if [[ ! "$major" =~ ^[0-9]+$ ]] || (( major < 20 )); then
    vm_guest_bootstrap_error 'Node.js readiness check failed.'
    return 1
  fi
  if [[ "$VM_GUEST_RUNTIME" == docker ]]; then
    "$VM_GUEST_DOCKER_BIN" compose version >/dev/null
  fi
  if [[ "$recursion_tools" == true ]]; then
    for tool in qemu-system-x86_64 qemu-img virtiofsd cloud-localds cloud-init genisoimage socat bats shellcheck; do
      command -v "$tool" >/dev/null 2>&1 || { vm_guest_bootstrap_error "A recursion validation tool is unavailable: $tool"; return 1; }
    done
  fi
  [[ -x "$(vm_guest_rooted /opt/little-mester/python-tools/bin/ruff)" &&
    -x "$(vm_guest_rooted /opt/little-mester/python-tools/bin/uv)" ]] || {
    vm_guest_bootstrap_error 'Isolated Python tools failed their readiness check.'
    return 1
  }
  [[ -s "$(vm_guest_rooted /home/mester/.config/opencode/opencode.json)" &&
    -s "$(vm_guest_rooted /home/mester/.config/opencode/cli.json)" ]] || {
    vm_guest_bootstrap_error 'Native OpenCode config files are missing.'
    return 1
  }
  vm_guest_write_success_marker "$marker" "$version" "$recursion_tools" "$aider_version" || {
    vm_guest_bootstrap_error 'Root-owned success marker could not be committed.'
    return 1
  }
  printf 'Guest bootstrap complete: OpenCode %s, Aider %s; payload %s\n' \
    "$version" "$aider_version" "$VM_GUEST_PAYLOAD_SHA256"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  vm_guest_main "$@"
fi
