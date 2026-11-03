#!/usr/bin/env bash
# Install Python CLI tooling in one isolated guest venv; never alters host tools.
set -euo pipefail

action="${1:-install}"
tools_dir="${VM_PYTHON_TOOLS_DIR:-/opt/little-mester/python-tools}"
tools_bin="$tools_dir/bin"
proxy="${LAB_VM_PROXY:-http://10.0.2.100:3128}"
python="${VM_PYTHON_TOOLS_PYTHON:-python3}"

[[ "$tools_dir" == /* && "$tools_dir" != *'/../'* && "$tools_dir" != */.. ]] || {
  printf 'REFUSED: Python tools directory must be an absolute safe path.\n' >&2
  exit 2
}
[[ "$proxy" == http://10.0.2.100:3128 ]] || {
  printf 'REFUSED: Python tooling requires the explicit guest proxy.\n' >&2
  exit 2
}

case "$action" in
  install)
    command -v "$python" >/dev/null 2>&1 || {
      printf 'ERROR: Python 3 is missing from the guest.\n' >&2
      exit 1
    }
    "$python" -m venv --help >/dev/null 2>&1 || {
      printf 'ERROR: the guest Python venv module is unavailable.\n' >&2
      exit 1
    }
    install -d -m 0755 "$(dirname "$tools_dir")"
    if [[ ! -d "$tools_dir" ]]; then
      "$python" -m venv "$tools_dir"
    fi
    [[ -x "$tools_bin/python" ]] || {
      printf 'ERROR: isolated Python venv is incomplete.\n' >&2
      exit 1
    }
    "$tools_bin/python" -m pip install --disable-pip-version-check --proxy "$proxy" --upgrade pip ruff uv aider-chat
    for tool in pip pip3 ruff uv aider; do
      [[ -x "$tools_bin/$tool" ]] || {
        printf 'ERROR: isolated Python tool is missing: %s\n' "$tool" >&2
        exit 1
      }
    done
    ;;
  check)
    for tool in pip pip3 ruff uv aider; do
      [[ -x "$tools_bin/$tool" ]] || {
        printf 'MISSING  Python tool %s\n' "$tool"
        exit 1
      }
    done
    printf 'CURRENT  isolated Python tools: pip, ruff, uv, aider\n'
    ;;
  *)
    printf 'Usage: %s [install|check]\n' "$0" >&2
    exit 2
    ;;
esac
