#!/bin/sh
# Per-auth-session xdg-open shim. Every request goes through the host MCP tool;
# unsupported URLs are refused rather than falling back to arbitrary browsing.

name=${0##*/}
if [ "$name" = "gio" ]; then
  if [ "$#" -ne 2 ] || [ "$1" != "open" ]; then
    echo 'Host browser bridge unavailable or authorization URL rejected.' >&2
    exit 2
  fi
  shift
else
  [ "$#" -eq 1 ] || {
    echo 'Host browser bridge unavailable or authorization URL rejected.' >&2
    exit 2
  }
fi

bridge_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$bridge_dir/browser-open.py" "$1"
