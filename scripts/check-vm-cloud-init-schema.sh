#!/usr/bin/env bash
# Offline contract check plus cloud-init's own schema validator when available.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMP_ROOT="${TMPDIR:-/tmp}"
TEMP_DIR="$(mktemp -d "$TEMP_ROOT/vm-cloud-init.XXXXXX")"
trap 'rm -rf -- "$TEMP_DIR"' EXIT
USER_DATA="$TEMP_DIR/user-data"

key="$(python3 - <<'PY'
import base64
import struct
blob = struct.pack(">I", len(b"ssh-ed25519")) + b"ssh-ed25519" + bytes(range(32))
print("ssh-ed25519 " + base64.b64encode(blob).decode("ascii") + " fixture@invalid")
PY
)"

# shellcheck disable=SC1091 # ROOT is the repository root; this runtime source is validated by the schema test.
source "$ROOT/lib/vm/cloud-init.sh"
vm_cloud_init_user_data schema-check 1000 1001 "$key" 0 2 none opencode latest \
  "$(printf '%064d' 0)" > "$USER_DATA"

python3 - "$USER_DATA" <<'PY'
import base64
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
raw = path.read_text(encoding="utf-8")
assert raw.startswith("#cloud-config\n")
config = json.loads(raw.split("\n", 1)[1])
assert config["hostname"] == "schema-check"
assert config["ssh_pwauth"] is False and config["disable_root"] is True
assert config["bootcmd"][0][0:2] == ["bash", "-c"]
assert "groupadd --gid 1001 mester" in config["bootcmd"][0][2]
assert "groupadd kvm" in config["bootcmd"][1][2]
user = config["users"][0]
assert user["uid"] == 1000 and user["primary_group"] == "mester"
assert "sudo" in user["groups"] and "kvm" in user["groups"]
assert user["ssh_authorized_keys"] == ["ssh-ed25519 " + base64.b64encode(
    len(b"ssh-ed25519").to_bytes(4, "big") + b"ssh-ed25519" + bytes(range(32))
).decode("ascii") + " fixture@invalid"]
assert config["mounts"] == [["workspace", "/workspace", "virtiofs", "defaults,nofail", "0", "0"]]
files = {item["path"]: item for item in config["write_files"]}
proxy = files["/etc/profile.d/lab-vm-proxy.sh"]["content"]
assert "HTTP_PROXY=http://10.0.2.100:3128" in proxy
assert "HTTPS_PROXY=http://10.0.2.100:3128" in proxy
assert "NO_PROXY=localhost,127.0.0.1,10.0.2.100" in proxy
assert files["/etc/lab-vm-recursion"]["content"] == "0 2\n"
for target in (
    "/usr/local/sbin/lab-vm-guest-bootstrap",
    "/usr/local/sbin/lab-vm-python-tools",
    "/usr/local/libexec/lab-vm-opencode-config.py",
    "/usr/local/libexec/lab-vm-guest-hash.sh",
):
    item = files[target]
    assert item["owner"] == "root:root" and item["permissions"] == "0755"
    assert item["encoding"] == "b64"
    decoded = base64.b64decode(item["content"], validate=True)
    assert decoded.startswith(b"#!")
command = config["runcmd"][0]
assert command[0] == "/usr/local/sbin/lab-vm-guest-bootstrap"
assert command[command.index("--runtime") + 1] == "none"
assert command[command.index("--uid") + 1] == "1000"
assert command[command.index("--gid") + 1] == "1001"
assert command[command.index("--proxy") + 1] == "http://10.0.2.100:3128"
assert not re.search(r"CANARY_[A-Z0-9_]+", raw)
PY

if command -v cloud-init >/dev/null 2>&1; then
  cloud-init schema --config-file "$USER_DATA"
  printf 'PASS cloud-init schema: cloud-init schema accepted generated NoCloud data\n'
else
  printf 'PASS cloud-init structure: generated JSON/YAML subset is valid; cloud-init binary is unavailable\n'
fi
