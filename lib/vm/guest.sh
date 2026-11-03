#!/usr/bin/env bash
# Host-side guest setup helpers. Sourcing this file performs no guest or host
# package operations; setup callers opt in by invoking these functions.

VM_GUEST_SOURCE_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
VM_GUEST_ROOT_DIR="$(cd "$(dirname "$VM_GUEST_SOURCE_FILE")/../.." && pwd)"
VM_GUEST_PYTHON="${VM_GUEST_PYTHON:-python3}"

vm_guest_error() {
  printf 'REFUSED: %s\n' "$*" >&2
  return 2
}

# C05/C09 shared algorithm: canonical sorted JSON over relative paths, source
# file modes, sizes and SHA-256 digests. Generated dist/node_modules and tests
# are deliberately excluded; update must call this same function.
vm_guest_payload_hash() {
  local source_root="${1:-$VM_GUEST_ROOT_DIR}"
  local workload_runtime="${2:-docker}" agent="${3:-opencode}"
  local version="${4:-latest}" depth="${5:-0}" max_depth="${6:-0}"

  "$VM_GUEST_PYTHON" - "$source_root" "$workload_runtime" "$agent" \
    "$version" "$depth" "$max_depth" <<'PY'
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
runtime, agent, version, depth, max_depth = sys.argv[2:]
if runtime not in {"docker", "none"} or agent != "opencode":
    raise SystemExit(2)
if version != "latest" and not re.fullmatch(
    r"v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", version
):
    raise SystemExit(2)
if not re.fullmatch(r"(?:0|[1-9][0-9]?)", depth) or not re.fullmatch(
    r"(?:0|[1-9][0-9]?)", max_depth
) or int(depth) > int(max_depth) or int(max_depth) > 32:
    raise SystemExit(2)

excluded = {".git", "node_modules", "dist", "__pycache__", "tests", "coverage"}
paths = [
    "lib/vm/cloud-init.sh",
    "lib/vm/guest.sh",
    "lib/vm/guest-bootstrap.sh",
    "scripts/vm-opencode-config.py",
    "scripts/vm-python-tools.sh",
]

def collect_tree(relative):
    base = root / relative
    if not base.is_dir() or base.is_symlink():
        raise SystemExit(2)
    base = base.resolve(strict=True)
    for current, directories, files in os.walk(base, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in excluded)
        for name in sorted(files):
            full = Path(current) / name
            if full.is_symlink():
                try:
                    target = full.resolve(strict=True)
                except (OSError, RuntimeError):
                    raise SystemExit(2) from None
                if not target.is_file() or not target.is_relative_to(base):
                    raise SystemExit(2)
            paths.append(full.relative_to(root).as_posix())
        for name in list(directories):
            if (Path(current) / name).is_symlink():
                raise SystemExit(2)

for directory in ("agent/opencode", "little-mester-mcp"):
    collect_tree(directory)

entries = []
total = 0
for relative in sorted(set(paths)):
    if relative.startswith("../") or Path(relative).is_absolute():
        raise SystemExit(2)
    path = root / relative
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        target_text = os.readlink(path)
        if Path(target_text).is_absolute():
            raise SystemExit(2)
        allowed_relative = (
            "agent/opencode" if relative.startswith("agent/opencode/")
            else "little-mester-mcp" if relative.startswith("little-mester-mcp/")
            else None
        )
        if allowed_relative is None:
            raise SystemExit(2)
        try:
            resolved = path.resolve(strict=True)
            allowed_root = (root / allowed_relative).resolve(strict=True)
        except (OSError, RuntimeError):
            raise SystemExit(2) from None
        if not resolved.is_file() or not resolved.is_relative_to(allowed_root):
            raise SystemExit(2)
        payload = os.fsencode(target_text)
    elif stat.S_ISREG(info.st_mode):
        if info.st_size > 64 * 1024 * 1024:
            raise SystemExit(2)
        payload = path.read_bytes()
        if len(payload) != info.st_size:
            raise SystemExit(2)
    else:
        raise SystemExit(2)
    if len(payload) > 64 * 1024 * 1024:
        raise SystemExit(2)
    total += len(payload)
    if total > 256 * 1024 * 1024:
        raise SystemExit(2)
    entries.append({
        "path": relative,
        "mode": stat.S_IMODE(info.st_mode),
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    })

manifest = {
    "schema_version": 1,
    "selectors": {
        "workload_runtime": runtime,
        "agent": agent,
        "opencode_version": version,
        "depth": int(depth),
        "max_depth": int(max_depth),
    },
    "files": entries,
}
encoded = (json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
print(hashlib.sha256(encoded).hexdigest())
PY
}

vm_guest_cloud_init_status_clean() {
  local status_json="${1:-}"
  [[ -n "$status_json" ]] || { vm_guest_error 'Cloud-init status output is missing.'; return 1; }
  if ! printf '%s' "$status_json" | "$VM_GUEST_PYTHON" -c '
import json,sys
try:
    data=json.load(sys.stdin)
    assert isinstance(data,dict)
    assert data.get("status")=="done"
    assert data.get("errors",[])==[]
    assert data.get("recoverable_errors",{}) in ({}, [])
    assert data.get("extended_status","done")=="done"
except Exception:
    raise SystemExit(1)
' >/dev/null 2>&1; then
    vm_guest_error 'Cloud-init did not finish cleanly; recoverable errors block readiness.'
    return 1
  fi
}

# Validate both independent gates: the SSH/cloud-init command itself must have
# exit status 0 and its JSON must be clean; then the root-owned guest marker must
# agree with the exact payload requested by the host.
vm_guest_readiness_validate() {
  local cloud_init_rc="${1:-}" status_json="${2:-}" marker_json="${3:-}"
  local expected_sha256="${4:-}"
  [[ "$cloud_init_rc" == 0 ]] || {
    vm_guest_error 'Cloud-init status command failed; guest is not ready.'
    return 1
  }
  vm_guest_cloud_init_status_clean "$status_json" || return 1
  if ! printf '%s' "$marker_json" | "$VM_GUEST_PYTHON" -c '
import json
import re
import sys
try:
    marker = json.load(sys.stdin)
    expected = sys.argv[1]
    assert re.fullmatch(r"[a-f0-9]{64}", expected)
    assert marker.get("status") == "ready"
    assert marker.get("payload_sha256") == expected
    assert marker.get("workspace_mount") == "virtiofs"
    assert marker.get("agent") == "opencode"
    assert marker.get("mcp_build") is True
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", marker.get("opencode_version", ""))
except Exception:
    raise SystemExit(1)
' "$expected_sha256" >/dev/null 2>&1; then
    vm_guest_error 'Guest bootstrap marker does not prove the requested runtime is ready.'
    return 1
  fi
}
