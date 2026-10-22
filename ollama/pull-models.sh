#!/bin/sh
set -eu

# Pull models listed in /models.txt under [ollama] section into the local Ollama install.
# Format:
#   [ollama]
#   model_name    context_size    [output_ratio_or_value]

resolve() {
  full="$1"
  case "$full" in *:*) echo "$full" ;; *) echo "${full}:latest" ;; esac
}

ollama serve >/tmp/serve.log 2>&1 &
pid=$!
trap 'kill $pid' EXIT

until ollama list >/dev/null 2>&1; do sleep 1; done

in_ollama=0
while IFS= read -r line; do
  # Strip comments and whitespace
  line="$(echo "$line" | sed 's/#.*//' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  [ -z "$line" ] && continue

  # Check for section header
  if echo "$line" | grep -q '^\[.*\]$'; then
    section="$(echo "$line" | sed 's/\[//;s/\]//')"
    if [ "$section" = "ollama" ]; then
      in_ollama=1
    else
      in_ollama=0
    fi
    continue
  fi

  if [ "$in_ollama" -eq 1 ]; then
    name="$(echo "$line" | awk '{print $1}')"
    [ -z "$name" ] && continue
    case "$name" in ''|'#'*) continue ;; esac
    echo ">> pulling $(resolve "$name")"
    ollama pull "$(resolve "$name")" || echo "WARNING: Failed to pull $(resolve "$name")"
  fi
done < /models.txt

echo "Done."