#!/usr/bin/env bash
# Apply only exact Python-tool versions from one successful guest update scan.
set -euo pipefail

plan="${1:-}"
tools_dir="${VM_PYTHON_TOOLS_DIR:-/opt/little-mester/python-tools}"
proxy="${LAB_VM_PROXY:-http://10.0.2.100:3128}"
python="${VM_PYTHON_TOOLS_PYTHON:-python3}"
test_mode="${VM_PYTHON_TOOLS_TEST_MODE:-0}"

[[ "$test_mode" == 0 || "$test_mode" == 1 ]] || {
  printf 'REFUSED: invalid guest tools test mode.\n' >&2
  exit 2
}
if [[ "$test_mode" != 1 ]]; then
  (( EUID == 0 )) || {
    printf 'REFUSED: selected guest Python tools must be updated as root.\n' >&2
    exit 2
  }
fi

[[ "$tools_dir" == /* && "$tools_dir" != *'/../'* && "$tools_dir" != */.. ]] || {
  printf 'REFUSED: Python tools directory must be an absolute safe path.\n' >&2
  exit 2
}
[[ "$proxy" == http://10.0.2.100:3128 ]] || {
  printf 'REFUSED: Python tooling requires the explicit guest proxy.\n' >&2
  exit 2
}
[[ -f "$plan" && ! -L "$plan" ]] || {
  printf 'REFUSED: a regular scanned update plan is required.\n' >&2
  exit 2
}
read -r plan_owner plan_mode plan_links < <(stat -c '%u %a %h' -- "$plan") || exit 2
[[ "$plan_mode" == 600 && "$plan_links" == 1 ]] || {
  printf 'REFUSED: scanned update plan permissions are invalid.\n' >&2
  exit 2
}
if [[ "$test_mode" != 1 ]]; then
  [[ "$plan_owner" == 0 ]] || {
    printf 'REFUSED: scanned update plan is not root-owned.\n' >&2
    exit 2
  }
fi
[[ -d "$tools_dir" && ! -L "$tools_dir" && -x "$tools_dir/bin/pip" && ! -L "$tools_dir/bin/pip" ]] || {
  printf 'ERROR: the selected guest Python tools environment is unavailable.\n' >&2
  exit 1
}
command -v "$python" >/dev/null 2>&1 || {
  printf 'ERROR: guest Python is unavailable.\n' >&2
  exit 1
}

planned_text="$("$python" - "$plan" <<'PY'
import json
import re
import sys
import time
from pathlib import Path

allowed = {"pip", "ruff", "uv", "aider-chat"}
version_re = re.compile(r"[0-9][0-9A-Za-z]*(?:[.+_!-][0-9A-Za-z]+)*\Z")
try:
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert type(data["created"]) is int and 0 <= time.time() - data["created"] <= 3600
    assert "tools" in data["targets_selected"]
    tool_target = next(item for item in data["targets"] if item["name"] == "tools")
    assert isinstance(tool_target["packages"], list)
    seen = set()
    for item in tool_target["packages"]:
        name, current, candidate = item["name"], item["current"], item["candidate"]
        assert name in allowed and name not in seen
        seen.add(name)
        assert isinstance(current, str) and isinstance(candidate, str)
        assert version_re.fullmatch(current)
        assert version_re.fullmatch(candidate)
        print(f"{name}\t{current}\t{candidate}")
except Exception:
    raise SystemExit(2)
PY
)" || {
  printf 'REFUSED: scanned Python tool versions are invalid.\n' >&2
  exit 2
}
planned=()
if [[ -n "$planned_text" ]]; then mapfile -t planned <<<"$planned_text"; fi

((${#planned[@]})) || {
  printf 'CURRENT  selected Python tools already match the scan.\n'
  exit 0
}

installed_before="$("$tools_dir/bin/pip" list --format=json)" || {
  printf 'ERROR: installed guest Python tool versions could not be rechecked.\n' >&2
  exit 1
}
requirements=()
for entry in "${planned[@]}"; do
  IFS=$'\t' read -r name expected candidate <<<"$entry"
  actual="$(printf '%s' "$installed_before" | "$python" -c '
import json,sys
try:
 data=json.load(sys.stdin); key=sys.argv[1].lower().replace("_","-")
 values=[item["version"] for item in data if item["name"].lower().replace("_","-")==key]
 assert len(values)==1 and isinstance(values[0],str); print(values[0])
except Exception: raise SystemExit(2)
' "$name")" || {
    printf 'ERROR: selected guest Python tool is no longer installed: %s\n' "$name" >&2
    exit 1
  }
  [[ "$actual" == "$expected" || "$actual" == "$candidate" ]] || {
    printf 'REFUSED: installed Python tool version changed after scan: %s\n' "$name" >&2
    exit 2
  }
  [[ "$actual" == "$candidate" || "$expected" == "$candidate" ]] && continue
  requirements+=("$name==$candidate")
done

((${#requirements[@]})) || {
  printf 'CURRENT  selected Python tools already match the scan.\n'
  exit 0
}

for requirement in "${requirements[@]}"; do
  "$tools_dir/bin/pip" install --disable-pip-version-check --proxy "$proxy" \
    --no-deps --upgrade "$requirement" || {
    printf 'PARTIAL  Python tool update failed; the existing guest environment was retained where the installer allowed.\n' >&2
    exit 1
  }
done

installed_after="$("$tools_dir/bin/pip" list --format=json)" || {
  printf 'PARTIAL  Python tools changed but their exact installed versions could not be verified.\n' >&2
  exit 1
}
for entry in "${planned[@]}"; do
  IFS=$'\t' read -r name expected candidate <<<"$entry"
  actual="$(printf '%s' "$installed_after" | "$python" -c '
import json,sys
try:
 data=json.load(sys.stdin); key=sys.argv[1].lower().replace("_","-")
 values=[item["version"] for item in data if item["name"].lower().replace("_","-")==key]
 assert len(values)==1 and isinstance(values[0],str); print(values[0])
except Exception: raise SystemExit(2)
' "$name")" || {
    printf 'PARTIAL  selected Python tool version could not be verified: %s\n' "$name" >&2
    exit 1
  }
  [[ "$actual" == "$candidate" ]] || {
    printf 'PARTIAL  selected Python tool did not reach its scanned exact version: %s\n' "$name" >&2
    exit 1
  }
done

for tool in pip pip3 ruff uv aider; do
  [[ -x "$tools_dir/bin/$tool" ]] || {
    printf 'PARTIAL  selected tool install completed but required executable is missing: %s\n' "$tool" >&2
    exit 1
  }
done
printf 'UPDATED  selected Python tools: %s\n' "${requirements[*]}"
