#!/usr/bin/env bash
# collate-to-md.sh – Collate every relevant file in this workspace into a single Markdown document.
# Designed to be passed directly to an external AI agent as context (raw Claude window, etc.).
#
# Usage:
#   ./scripts/collate-to-md.sh [output.md] [--all]
#     --all       Include untracked files too (uses `find` instead of `git ls-files`)
#     If no output path given, writes to the repo root as `_COLLATED_OUTPUT.MD` (gitignored).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")"/.. && pwd)"
INCLUDE_UNTRACKED=false
OUTPUT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --all)    INCLUDE_UNTRACKED=true; shift ;;
    --output) OUTPUT="$2";     shift 2 ;;
    *)        if [[ -z "$OUTPUT" ]]; then OUTPUT="$1"; fi; shift ;;
  esac
done

# Default output at repo root (conveniently gitignored).
if [[ -z "$OUTPUT" ]]; then
  OUTPUT="$ROOT/_COLLATED_OUTPUT.MD"
fi

OUT_DIR="$(dirname "$OUTPUT")"
[[ ! -d "$OUT_DIR" ]] && mkdir -p "$OUT_DIR"

# Language detection: extension first, then basename fallback.
lang_for_ext() {
  case "${1,,}" in
    sh|bash)   echo bash ;;
    js)        echo javascript ;;
    ts)        echo typescript ;;
    tsx)       echo tsx ;;
    jsx)       echo jsx ;;
    py)        echo python ;;
    md)        echo markdown ;;
    yaml|yml)  echo yaml ;;
    json)      echo json ;;
    toml)      echo toml ;;
    sql)       echo sql ;;
    rs)        echo rust ;;
    go)        echo go ;;
    rb)        echo ruby ;;
    kt|java)   echo java ;;
    html)      echo html ;;
    css)       echo css ;;
    sh*)       echo bash ;;
    *)         echo "$1" ;;
  esac
}

lang_for_bname() {
  case "${1,,}" in
    dockerfile*) echo dockerfile ;;
    makefile*)   echo makefile ;;
    compose*|docker-compose*) echo yaml ;;
    gemfile*)    echo ruby ;;
    *.gemspec)   echo ruby ;;
    rbs*)          echo ruby ;;
  esac
}

# Collect the full list of paths.
if [[ "$INCLUDE_UNTRACKED" == true ]]; then
  # Use find to grab everything, excluding known noise dirs and patterns.
  # Exclude gitignored-style directories and hidden tooling artifacts.
  mapfile -t FILES < <(
    find "$ROOT" \
      -type f \
      -not -path '*/\.git/*' \
      -not -path '*/node_modules/*' \
      -not -path '*/__pycache__/*' \
      -not -path '*/.idea/shelf/*' \
      -not -path '*/.aider.tags.cache/*' \
      -not -path '*/.opencode/skills/*' \
      -not -path '*/workspaces/node_modules/*' \
      -not -name '*.db' \
      -not -name '*.binstub' \
      -not -name '*.pyc' \
      -not -name '*.pdb' \
      -not -empty 2>/dev/null | sort
  )
else
  # Use git ls-files for committed-only files (safer, predictable).
  mapfile -t FILES < <(git -C "$ROOT" ls-files)
fi

# Strip the ROOT prefix from each path.
REL_PATHS=()
for p in "${FILES[@]}"; do
  REL_PATHS+=( "${p#"$ROOT"/}" )
done

{
cat <<'HEADER'
# Workspace Collation – Complete File Collection

HEADER

echo "_Generated: $(date -u '+%Y-%m-%d %T UTC')_"
echo ""

if [[ "$INCLUDE_UNTRACKED" == true ]]; then
  echo "**Mode:** include-untracked (all files in workspace, not just committed)"
else
  commit_msg="$(git -C "$ROOT" log --format='%h %s' -1 2>/dev/null || echo 'N/A')"
  branch="$(git -C "$ROOT" branch --show-current 2>/dev/null || echo '')"
  echo "**Commit:** \`$commit_msg\`"
  [[ -n "$branch" ]] && echo "**Branch:** \`$branch\`"
fi

echo ""
echo "$((_COLL_FILE_COUNT=${#REL_PATHS[@]})) files."
echo ""
echo "---"
echo ""

SEP="---"

for rel in "${REL_PATHS[@]}"; do
  full="$ROOT/$rel"

  # Skip the output file itself if it happens to be inside ROOT.
  [[ "$(realpath "$full" 2>/dev/null)" == "$(realpath "$OUTPUT" 2>/dev/null)" ]] && continue

  # Binary detection: check for null bytes (works reliably).
  if grep -Pq '\x00' "$full" 2>/dev/null; then
    size=$(wc -c < "$full")
    echo ""
    echo "$SEP"
    echo "> **\`$rel\`** ($size bytes – binary, skipped)"
    echo "$SEP"
    echo ""
    continue
  fi

  content="$(cat "$full" 2>/dev/null)" || continue

  ext="${rel##*.}"
  lang=$(lang_for_ext "$ext")

  # If extension gave us nothing useful, try basename-based detection.
  if [[ "$lang" == "$ext" ]]; then
    bname="$(basename "$rel")"
    named_lang=$(lang_for_bname "$bname")
    [[ -n "$named_lang" ]] && lang="$named_lang"
  fi

  # Last-resort: use "plaintext" for unrecognized.
  [[ "$lang" == "$ext" ]] && lang="text"
  separator="$lang"

  echo ""
  echo "$SEP"
  echo "> **\`$rel\`**"
  echo "$SEP"
  if [[ -n "$content" ]]; then
    echo ""
    echo "\`\`\`$separator"
    cat "$full"
    printf '\n'           # ensure trailing newline after file content
    echo "\`\`\`"
  fi

done
# shellcheck disable=SC2094
} > "$OUTPUT"

echo "Wrote $OUTPUT ($(wc -l < "$OUTPUT") lines, $(stat --printf='%s' "$OUTPUT") bytes)."
