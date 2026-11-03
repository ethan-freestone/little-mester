#!/usr/bin/env bash
# Render a single secret-free NoCloud cloud-config document for the VM guest.
# Dynamic values are encoded as JSON scalars, which are also valid YAML scalars.

VM_CLOUD_INIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VM_CLOUD_INIT_ROOT="$(cd "$VM_CLOUD_INIT_DIR/../.." && pwd)"
VM_CLOUD_INIT_PYTHON="${VM_CLOUD_INIT_PYTHON:-python3}"

vm_cloud_init_error() {
  printf 'REFUSED: %s\n' "$*" >&2
  return 2
}

vm_cloud_init_user_data() {
  local name="${1:-}" uid="${2:-}" gid="${3:-}" public_key="${4:-}"
  local depth="${5:-0}" max_depth="${6:-0}" runtime="${7:-docker}"
  local agent="${8:-opencode}" opencode_version="${9:-latest}"
  local payload_sha256="${10:-}" proxy="${11:-http://10.0.2.100:3128}"
  local model_provider="${12:-none}" model_port="${13:-}" model_id="${14:-}"
  local bootstrap="$VM_CLOUD_INIT_ROOT/lib/vm/guest-bootstrap.sh"
  local python_tools="$VM_CLOUD_INIT_ROOT/scripts/vm-python-tools.sh"
  local config_renderer="$VM_CLOUD_INIT_ROOT/scripts/vm-opencode-config.py"
  local hash_helper="$VM_CLOUD_INIT_ROOT/lib/vm/guest.sh"

  [[ -f "$bootstrap" && ! -L "$bootstrap" && -f "$python_tools" && ! -L "$python_tools" &&
    -f "$config_renderer" && ! -L "$config_renderer" && -f "$hash_helper" && ! -L "$hash_helper" ]] || {
    vm_cloud_init_error 'Guest bootstrap inputs are unavailable.'
    return 2
  }

  "$VM_CLOUD_INIT_PYTHON" - \
    "$name" "$uid" "$gid" "$public_key" "$depth" "$max_depth" "$runtime" \
    "$agent" "$opencode_version" "$payload_sha256" "$proxy" \
    "$model_provider" "$model_port" "$model_id" \
    "$bootstrap" "$python_tools" "$config_renderer" "$hash_helper" <<'PY'
import base64
import json
import re
import struct
import sys
from pathlib import Path

(name, uid, gid, public_key, depth, max_depth, runtime, agent,
 version, payload_sha256, proxy, model_provider, model_port, model_id,
 bootstrap_path, python_tools_path,
 renderer_path, hash_helper_path) = sys.argv[1:]

def fail():
    raise SystemExit("REFUSED: Invalid cloud-init input.")

def decimal(value, maximum):
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,9})", value):
        fail()
    number = int(value)
    if number > maximum:
        fail()
    return number

if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}", name):
    fail()
uid_num = decimal(uid, 2**31 - 1)
gid_num = decimal(gid, 2**31 - 1)
depth_num = decimal(depth, 32)
max_depth_num = decimal(max_depth, 32)
if uid_num == 0 or gid_num == 0 or depth_num > max_depth_num:
    fail()
if runtime not in {"docker", "none"} or agent not in {"opencode", "aider"}:
    fail()
if model_provider not in {"none", "ollama", "llamacpp", "remote"}:
    fail()
if len(model_id) > 512 or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in model_id):
    fail()
if model_provider in {"ollama", "llamacpp"}:
    model_port_num = decimal(model_port, 65535)
    if model_port_num == 0:
        fail()
else:
    if model_port:
        fail()
    model_port_num = None
if version != "latest" and not re.fullmatch(
    r"v?2\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", version
):
    fail()
if not re.fullmatch(r"[a-f0-9]{64}", payload_sha256):
    fail()
if proxy != "http://10.0.2.100:3128":
    fail()

parts = public_key.split()
if len(parts) < 2 or parts[0] not in {
    "ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
}:
    fail()
try:
    blob = base64.b64decode(parts[1], validate=True)
    key_type_length = struct.unpack(">I", blob[:4])[0]
    key_type = blob[4:4 + key_type_length].decode("ascii")
except (ValueError, struct.error, UnicodeDecodeError):
    fail()
if key_type != parts[0] or any(ord(char) < 32 or ord(char) == 127 for char in public_key):
    fail()

bootstrap = Path(bootstrap_path).read_text(encoding="utf-8")
python_tools = Path(python_tools_path).read_text(encoding="utf-8")
renderer = Path(renderer_path).read_text(encoding="utf-8")
hash_helper = Path(hash_helper_path).read_text(encoding="utf-8")
proxy_profile = "\n".join((
    "export HTTP_PROXY=http://10.0.2.100:3128",
    "export HTTPS_PROXY=http://10.0.2.100:3128",
    "export http_proxy=http://10.0.2.100:3128",
    "export https_proxy=http://10.0.2.100:3128",
    "export NO_PROXY=localhost,127.0.0.1,10.0.2.100",
    "export no_proxy=localhost,127.0.0.1,10.0.2.100",
    "export PATH=/opt/little-mester/python-tools/bin:$PATH",
    "",
))
recursion_profile = """if [ -r /etc/lab-vm-recursion ]; then
  IFS=' ' read -r LAB_VM_RECURSION_DEPTH LAB_VM_MAX_DEPTH < /etc/lab-vm-recursion
  export LAB_VM_RECURSION_DEPTH LAB_VM_MAX_DEPTH
fi
"""
bootcmd = [
    [
        "bash", "-c",
        "if getent group mester >/dev/null; then "
        "test \"$(getent group mester | cut -d: -f3)\" = " + str(gid_num) + "; "
        "else groupadd --gid " + str(gid_num) + " mester; fi",
    ]
]
guest_groups = ["sudo"]
if depth_num < max_depth_num:
    bootcmd.append(["bash", "-c", "getent group kvm >/dev/null || groupadd kvm"])
    guest_groups.append("kvm")

def write_file(path, content, permissions="0644", encoded=False):
    item = {
        "path": path,
        "owner": "root:root",
        "permissions": permissions,
    }
    if encoded:
        item["encoding"] = "b64"
        item["content"] = base64.b64encode(content.encode("utf-8")).decode("ascii")
    else:
        item["content"] = content
    return item

config = {
    "hostname": name,
    "ssh_pwauth": False,
    "disable_root": True,
    "bootcmd": bootcmd,
    "users": [{
        "name": "mester",
        "uid": uid_num,
        "primary_group": "mester",
        "groups": guest_groups,
        "shell": "/bin/bash",
        "sudo": ["ALL=(ALL) NOPASSWD:ALL"],
        "lock_passwd": True,
        "ssh_authorized_keys": [public_key],
    }],
    "mounts": [["workspace", "/workspace", "virtiofs", "defaults,nofail", "0", "0"]],
    "write_files": [
        write_file(
            "/etc/apt/apt.conf.d/80lab-vm-proxy",
            'Acquire::http::Proxy "http://10.0.2.100:3128";\n'
            'Acquire::https::Proxy "http://10.0.2.100:3128";\n',
        ),
        write_file("/etc/profile.d/lab-vm-proxy.sh", proxy_profile),
        write_file("/etc/lab-vm-recursion", f"{depth_num} {max_depth_num}\n"),
        write_file("/etc/profile.d/lab-vm-recursion.sh", recursion_profile),
        write_file("/usr/local/sbin/lab-vm-guest-bootstrap", bootstrap, "0755", True),
        write_file("/usr/local/sbin/lab-vm-python-tools", python_tools, "0755", True),
        write_file("/usr/local/libexec/lab-vm-opencode-config.py", renderer, "0755", True),
        write_file("/usr/local/libexec/lab-vm-guest-hash.sh", hash_helper, "0755", True),
    ],
    "runcmd": [[
        "/usr/local/sbin/lab-vm-guest-bootstrap",
        "--runtime", runtime,
        "--agent", agent,
        "--depth", str(depth_num),
        "--max-depth", str(max_depth_num),
        "--opencode-version", version,
        "--payload-sha256", payload_sha256,
        "--uid", str(uid_num),
        "--gid", str(gid_num),
        "--workspace", "/workspace",
        "--source-root", "/workspace",
        "--proxy", proxy,
        "--model-provider", model_provider,
        "--model-port", "" if model_port_num is None else str(model_port_num),
        "--model-id", model_id,
    ]],
}
print("#cloud-config")
print(json.dumps(config, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
PY
}
