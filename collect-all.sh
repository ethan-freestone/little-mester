#!/usr/bin/env bash
# collect-all.sh – Combine every relevant file in this workspace into a single document.
# Designed to be passed directly to an external AI agent as context.
# Usage: ./collect-all.sh [output-file]

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$ROOT/WORKSPACE-ALL.md}"


{
  echo "# Workspace Context – Complete File Collection"
  echo ""
  echo "Generated: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
  echo ""
  echo "---"
  echo ""

  find "$ROOT" \
    -type f \
    -not -path '*/\.git/*' \
    -not -path '*/node_modules/*' \
    -not -path '*/__pycache__/*' \
    -not -path '*/.idea/shelf/*' \
    -not -path '*/.aider.tags.cache/*' \
    -not -path '*/.opencode/skills/*' \
    -not -name '*.db' \
    -not -name '*.binstub' \
    -not -empty \
  | sed "s|^$ROOT/||" | sort | while IFS= read -r rel; do

    # Skip binary files
    if file -b --mime-type "$ROOT/$rel" | grep -q '^text/'; then
      sep="------------------------------------------------------------"
      ext="${rel##*.}"
      lang=""
      case "${ext,,}" in
        sh|bash)   lang=bash ;;
        py)        lang=python ;;
        md)        lang=markdown ;;
        yaml|yml)  lang=yaml ;;
        json)      lang=json ;;
        tsx|jsx)   lang=tsx ;;
        ts|mjs)    lang=typescript ;;
        rs)        lang=rust ;;
        go)        lang=go ;;
        toml)      lang=toml ;;
        sql)       lang=sql ;;
      esac
      # Handle name-based extensions (Dockerfile, Makefile, etc.)
      if [ -z "$lang" ]; then
        bname="$(basename "$rel")"
        case "$bname" in
          Dockerfile*)  lang=dockerfile ;;
          Makefile*)    lang=makefile ;;
          *.toml)       lang=toml ;;
          compose.yaml|compose.yml) lang=yaml ;;
        esac
      fi

      echo ""
      echo "$sep"
      echo "## File: $rel"
      echo "$sep"
      if [ -n "$lang" ]; then
        echo "\`\`\`$lang"
        cat "$ROOT/$rel"
        printf '\n'  # trailing newline
        echo "\`\`\`"
      else
        echo "_[plain text]_"
        echo ""
        cat "$ROOT/$rel"
      fi
      echo ""

    else
      bytes=$(wc -c < "$ROOT/$rel")
      echo "$sep"
      echo "## File: $rel ($bytes bytes) – binary, skipped"
      echo "_Binary file — not included in context._"
      echo ""
    fi

  done

} > "$OUT"

echo "Done → $OUT ($(wc -l < "$OUT") lines)"
