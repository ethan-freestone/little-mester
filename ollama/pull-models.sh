#!/bin/sh
set -eu
ollama serve >/tmp/serve.log 2>&1 &
pid=$!
until ollama list >/dev/null 2>&1; do sleep 1; done
while read -r m; do
  case "$m" in ''|'#'*) continue;; esac
  echo ">> pulling $m"
  ollama pull "$m"
done < /models.txt
kill "$pid"