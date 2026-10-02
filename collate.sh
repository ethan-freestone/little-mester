#!/usr/bin/env bash
set -euo pipefail

OUTPUT="codebase_context.md"

# Empty the file if it exists so we don't append to an older run
> "$OUTPUT"

echo "Collating codebase into $OUTPUT..."

# git ls-files ensures we only get tracked files, ignoring junk
git ls-files | while read -r file; do
  # Skip the output file itself just in case it gets tracked
  if [[ "$file" == "$OUTPUT" ]]; then continue; fi

  # Append filename and location as a markdown header
  echo "## File: \`$file\`" >> "$OUTPUT"

  # Append file content wrapped in markdown code blocks
  # You can dynamically grab the extension for syntax highlighting if needed,
  # but standard backticks work perfectly for LLM context.
  echo '```' >> "$OUTPUT"
  cat "$file" >> "$OUTPUT"
  echo '```' >> "$OUTPUT"
  echo "" >> "$OUTPUT"
done

echo "Done! Context saved to $OUTPUT"