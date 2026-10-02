#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Default workspace if not provided
WS="${1:-workspaces/demo}"

echo "🧪 Running unit tests inside the agent container..."
./lab exec "$WS" 'bats /workspace/tests/'
