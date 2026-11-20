#!/usr/bin/env bash
# Validate every repository workflow with the pinned, checksum-verified actionlint.
set -Eeuo pipefail

readonly actionlint_version='1.7.12'
readonly actionlint_sha256='8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8'
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
temp_root="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
[[ -d "$temp_root" && -w "$temp_root" ]] || {
  printf 'check-workflows: temporary directory is unavailable: %s\n' "$temp_root" >&2
  exit 1
}
work="$(mktemp -d "$temp_root/actionlint.XXXXXXXX")"
trap 'rm -rf -- "$work"' EXIT

archive="actionlint_${actionlint_version}_linux_amd64.tar.gz"
url="https://github.com/rhysd/actionlint/releases/download/v${actionlint_version}/${archive}"
curl --fail --location --silent --show-error --connect-timeout 20 --max-time 120 \
  "$url" --output "$work/$archive"
printf '%s  %s\n' "$actionlint_sha256" "$work/$archive" | sha256sum --check --status \
  || { printf 'check-workflows: pinned actionlint archive checksum mismatch\n' >&2; exit 1; }
tar -xzf "$work/$archive" --no-same-owner --no-same-permissions -C "$work" actionlint
"$work/actionlint" -color "$repo_root"/.github/workflows/*.yml
