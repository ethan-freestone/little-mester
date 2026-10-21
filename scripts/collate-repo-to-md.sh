#!/usr/bin/env bash
# Collates every tracked repo file into a single Markdown document.
# Usage: ./scripts/collate-repo-to-md.sh [--overwrite]
set -euo pipefail

OUTPUT=".REPO_COLLATION.MD"
BASELINE=$(git rev-parse --show-toplevel)

if [[ -f "$OUTPUT" && "$1" != "--overwrite" ]]; then
  echo "Output file '$OUTPUT' already exists. Re-run with --overwrite to recreate." >&2
  exit 1
fi

# Map file extensions to syntax-highlight language names.
lang_for_ext() {
  case "$1" in
    sh)       echo bash ;;
    js)       echo javascript ;;
    ts)       echo typescript ;;
    tsx)      echo tsx ;;
    py)       echo python ;;
    md)       echo markdown ;;
    json)     echo json ;;
    xml|iml)  echo xml ;;
    yaml|yml) echo yaml ;;
    Dockerfile*) echo dockerfile ;;
    txt|log)  echo "" ;;
    *)        echo "$1" ;;   # bare extension as fallback
  esac
}

{
cat <<HEADER
# Repository Collatation

Auto-generated on $(date -Iseconds) from commit $(git rev-parse --short HEAD).
$(git ls-files | wc -l) files.
HEADER

while IFS= read -r filepath; do
  fullpath="$BASELINE/$filepath"

  # Skip the collation file itself if it happens to be tracked
  [[ "$filepath" == "$OUTPUT" ]] && continue

  # Binary detection via null-byte search
  if grep -Pqc '\x00' "$fullpath" 2>/dev/null; then
    size=$(wc -c < "$fullpath")
    echo "---"
    echo "> **\`$filepath\`** ($size bytes, binary content omitted)"
    echo ""
    continue
  fi

  # Determine highlight language
  ext="${filepath##*.}"
  lang=$(lang_for_ext "$ext")
  separator="${lang:-text}"

  echo "---"
  echo "> **\`$filepath\`**"
  echo ""
  echo ">\`\`\`$separator"
  
  # Write file content verbatim (cat avoids any shell interpretation)
  cat "$fullpath"
  echo ""
  echo "\`\`\`"
  echo ""

done < <(git ls-files | sort)

} > "$OUTPUT"

echo "Wrote $OUTPUT ($(wc -l < "$OUTPUT") lines, $(stat --printf='%s' "$OUTPUT") bytes)."
