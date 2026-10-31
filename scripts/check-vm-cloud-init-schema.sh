#!/usr/bin/env bash
# Read-only validation: never run cloud-init provisioning on the test machine.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/vm/cloud-init.sh
source "$ROOT/lib/vm/cloud-init.sh"
command -v cloud-init >/dev/null || { echo "cloud-init is required for schema validation" >&2; exit 1; }
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
ssh-keygen -q -t ed25519 -N '' -f "$tmp_dir/test-key"
vm_cloud_init_user_data schema-test 1000 1000 "$(cat "$tmp_dir/test-key.pub")" > "$tmp_dir/user-data"
cloud-init schema --config-file "$tmp_dir/user-data"
