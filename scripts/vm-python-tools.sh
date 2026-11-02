#!/usr/bin/env bash
# Manage the VM's Python CLI toolchain in an isolated, root-owned venv.
set -euo pipefail

action="${1:-install}"
tools_dir=/opt/little-mester/python-tools
tools_bin="$tools_dir/bin"

source_proxy_environment() {
  if [[ -r /etc/profile.d/mester.sh ]]; then
    # The guest's policy-controlled egress proxy is required for PyPI access.
    # shellcheck disable=SC1091
    source /etc/profile.d/mester.sh
  fi
}

check_links() {
  local tool path expected
  for tool in pip pip3 ruff uv; do
    path="$(readlink -f "/usr/local/bin/$tool" 2>/dev/null || true)"
    expected="$(readlink -f "$tools_bin/$tool" 2>/dev/null || true)"
    [[ -x "$tools_bin/$tool" && -n "$expected" && "$path" == "$expected" ]] || return 1
  done
}

install_tools() {
  source_proxy_environment
  export DEBIAN_FRONTEND=noninteractive
  apt-get install -y python3-pip python3-venv
  command -v python3 >/dev/null 2>&1 || {
    echo 'ERROR: python3 is missing; install the Ubuntu python3 package first.' >&2
    return 1
  }
  python3 -m venv --help >/dev/null 2>&1 || {
    echo 'ERROR: python3-venv is missing; install it before Python CLI tools.' >&2
    return 1
  }

  install -d -m 0755 /opt/little-mester
  python3 -m venv "$tools_dir"
  "$tools_bin/python" -m pip install --disable-pip-version-check --upgrade pip ruff uv

  install -d -m 0755 /usr/local/bin
  local tool link current expected
  for tool in pip pip3 ruff uv; do
    link="/usr/local/bin/$tool"
    current="$(readlink -f "$link" 2>/dev/null || true)"
    expected="$(readlink -f "$tools_bin/$tool" 2>/dev/null || true)"
    [[ -x "$tools_bin/$tool" && -n "$expected" ]] || {
      echo "ERROR: expected Python tool executable is missing: $tools_bin/$tool" >&2
      return 1
    }
    if [[ -e "$link" || -L "$link" ]]; then
      if [[ "$current" != "$expected" ]]; then
        echo "ERROR: refusing to replace unrelated tool at $link" >&2
        return 1
      fi
    fi
    ln -sfn "$tools_bin/$tool" "$link"
  done

  pip --version
  ruff --version
  uv --version
}

check_tools() {
  source_proxy_environment
  if [[ ! -x "$tools_bin/python" ]] || ! check_links; then
    echo 'UPDATE  Python toolchain  pip/ruff/uv missing or not managed by this VM'
    return 0
  fi

  local outdated
  outdated="$("$tools_bin/python" -m pip list --outdated --format=json --disable-pip-version-check)"
  python3 - "$outdated" <<'PY'
import json
import sys

wanted = {"pip", "ruff", "uv"}
rows = json.loads(sys.argv[1])
updates = [row for row in rows if row.get("name", "").lower() in wanted]
if updates:
    for row in sorted(updates, key=lambda item: item["name"].lower()):
        print(f"UPDATE  Python tool {row['name']}  {row['version']} -> {row['latest_version']}")
else:
    print("CURRENT Python tools: pip, ruff, uv")
PY
}

case "$action" in
  install) install_tools ;;
  check) check_tools ;;
  *)
    echo "Usage: $0 [install|check]" >&2
    exit 2
    ;;
esac
