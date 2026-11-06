#!/usr/bin/env bash
# Select a private per-user root; never discover state inside a checkout.
# Selection itself is read-only. lib/vm/paths.py validates it before use.
vm_state_root() {
  printf '%s\n' "${MESTER_STATE_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/little-mester}"
}
