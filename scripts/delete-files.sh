#!/usr/bin/env bash
set -euo pipefail

if [[ $# -eq 0 ]]; then
    echo "Usage: ./scripts/delete-files.sh <file1> [file2] ..."
    exit 1
fi

echo "⚠️  The following files will be permanently deleted:"
for f in "$@"; do
    echo "  - $f"
done
echo ""

read -r -p "Are you absolutely sure you want to delete these files? [y/N] " confirm
if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

for f in "$@"; do
    if [[ -e "$f" ]]; then
        rm -f "$f"
        echo "✅ Deleted: $f"
    else
        echo "ℹ️  Not found (skipped): $f"
    fi
done

echo "Done."
