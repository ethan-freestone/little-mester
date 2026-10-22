#!/bin/sh
set -eu

# Pull GGUF models listed under [llamacpp] in /models.txt.
# Line format:  local-name  ctx  out  hf-repo  hf-filename
# Each model is saved as /models/<local-name>.gguf, the path compose's MODEL_FILE uses.

# hf_transfer is deprecated; compose may still set this, so clear it.
unset HF_HUB_ENABLE_HF_TRANSFER || true
export HF_XET_HIGH_PERFORMANCE=1

pip install -q --no-cache-dir huggingface_hub

download_model() {
    name="$1"; repo="$2"; filename="$3"
    dest="/models/$name.gguf"

    if [ -s "$dest" ]; then
        echo ">> $name already present, skipping"
        return 0
    fi

    echo ">> Downloading $filename from $repo"
    HF_REPO="$repo" HF_FILE="$filename" python3 - <<'PY' || return 1
import os, sys
from huggingface_hub import hf_hub_download
try:
    path = hf_hub_download(
        repo_id=os.environ["HF_REPO"],
        filename=os.environ["HF_FILE"],
        local_dir="/models",
    )
    print(f"Downloaded to {path}")
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
PY

    mv "/models/$filename" "$dest" || return 1
    echo ">> Saved as $dest"
}

echo "=== llama.cpp GGUF Model Pull ==="
mkdir -p /models

failed=0
in_llamacpp=0
while IFS= read -r line; do
    line="$(echo "$line" | sed 's/#.*//;s/^[[:space:]]*//;s/[[:space:]]*$//')"
    [ -z "$line" ] && continue

    case "$line" in
        "[llamacpp]") in_llamacpp=1; continue ;;
        "["*)         in_llamacpp=0; continue ;;
    esac
    [ "$in_llamacpp" -eq 1 ] || continue

    # shellcheck disable=SC2086
    set -- $line
    if [ "$#" -lt 5 ]; then
        echo "WARNING: '$1' needs 5 columns (name ctx out repo filename), skipping" >&2
        failed=1
        continue
    fi

    download_model "$1" "$4" "$5" || { echo "WARNING: Failed to download $1" >&2; failed=1; }
done < /models.txt

echo "=== Done ==="
ls -lh /models/
exit "$failed"