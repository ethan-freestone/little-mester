#!/bin/sh
# Per-agent xdg-open shim. Unsupported URLs never fall back to general browsing.

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

bridge_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd) || exit 2
exec python3 "$bridge_dir/browser-open.py" "$1"
