#!/bin/sh
set -eu

# Pull models listed in /models.txt into the local Ollama install.
# New format (since this config change):
#   <model_name>    <context_size>    [output_ratio_or_value]
# We only need the first field (the model name).

resolve() {
  full="$1"
  case "$full" in *:*) echo "$full" ;; *) echo "${full}:latest" ;; esac
}

ollama serve >/tmp/serve.log 2>&1 &
pid=$!
trap 'kill $pid' EXIT

until ollama list >/dev/null 2>&1; do sleep 1; done

while read -r name _ctx _ratio; do
  case "$name" in ''|'#'*) continue ;; esac
  echo ">> pulling $(resolve "$name")"
  ollama pull "$(resolve "$name")" || echo "WARNING: Failed to pull $(resolve "$name")"
done < /models.txt

echo "Done."
