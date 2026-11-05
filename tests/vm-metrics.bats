#!/usr/bin/env bats

setup() {
  ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
  TOOL_PLUGIN="$ROOT/agent/opencode/plugins/host-metrics/index.ts"
  TUI_PLUGIN="$ROOT/agent/opencode/plugins/host-metrics-tui/tui.tsx"
}

@test "host metrics plugin honors one explicit URL and a disabled transport" {
  run python3 - "$TOOL_PLUGIN" <<'PY'
import sys

src = open(sys.argv[1], encoding="utf-8").read()
for required in (
    'configured.toLowerCase() === "disabled"',
    "return [configured.replace(/\\/+$/, \"\")]",
    'return ["http://metrics:19532", "http://127.0.0.1:19532"]',
    'if (!tried.length) throw new Error("metrics are disabled for this VM profile")',
    "Metrics are optional and do not block the agent.",
):
    if required not in src:
        raise SystemExit(f"missing metrics endpoint/error behavior: {required}")
PY
  [ "$status" -eq 0 ]
}

@test "host metrics TUI loads its slot and displays retry or disabled state" {
  run python3 - "$TUI_PLUGIN" <<'PY'
import sys

src = open(sys.argv[1], encoding="utf-8").read()
for required in (
    'id: "little-mester.host-metrics-tui"',
    'append: "sidebar.footer"',
    '"Metrics unavailable · retrying"',
    '"Metrics disabled for this profile"',
    "new AbortController()",
    'configuredEndpoint.toLowerCase() !== "disabled"',
):
    if required not in src:
        raise SystemExit(f"missing TUI metrics behavior: {required}")
if 'fetch("http://metrics:19532/metrics")' in src:
    raise SystemExit("TUI must route through the explicit endpoint selector")
PY
  [ "$status" -eq 0 ]

  run python3 "$ROOT/scripts/check-tui-jsx.py" "$TUI_PLUGIN"
  [ "$status" -eq 0 ]
}

@test "native agent accepts only the exact selected metrics guestfwd" {
  local data
  data='{"runtime_identity":{"argv":["qemu-system-x86_64","-netdev","user,id=net0,guestfwd=tcp:10.0.2.100:19532-cmd:/usr/bin/socat - TCP:127.0.0.1:19532,restrict=on"]}}'
  run bash -c 'source "$1/lib/vm/services.sh"; vm_services_profile_metrics_forward "$2"; printf "%s\n" "$VM_SERVICES_METRICS_FORWARD_PORT"' \
    _ "$ROOT" "$data"
  [ "$status" -eq 0 ]
  [ "$output" = 19532 ]

  data='{"runtime_identity":{"argv":["qemu-system-x86_64","-netdev","user,id=net0,restrict=on"]}}'
  run bash -c 'source "$1/lib/vm/services.sh"; vm_services_profile_metrics_forward "$2"; printf "<%s>\n" "$VM_SERVICES_METRICS_FORWARD_PORT"' \
    _ "$ROOT" "$data"
  [ "$status" -eq 0 ]
  [ "$output" = "<>" ]
}

@test "lifecycle disables nested metrics and tolerates missing optional service" {
  local result runtime
  run bash -c '
    source "$1/lib/vm/lifecycle.sh"
    vm_lifecycle_metrics_select_port /unused "$1" '\''{"metrics":"off"}'\'' 0 ""
  ' _ "$ROOT"
  [ "$status" -eq 0 ]
  [ -z "$output" ]

  run bash -c '
    source "$1/lib/vm/lifecycle.sh"
    vm_lifecycle_metrics_select_port /unused "$1" '\''{}'\'' 0 19532
  ' _ "$ROOT"
  [ "$status" -eq 0 ]
  [ -z "$output" ]

  run bash -c '
    source "$1/lib/vm/lifecycle.sh"
    vm_lifecycle_metrics_select_port /unused "$1" '\''{"metrics":"on"}'\'' 1 19532
  ' _ "$ROOT"
  [ "$status" -eq 0 ]
  [ -z "$output" ]

  runtime="$(mktemp -d /tmp/vm-metrics-runtime.XXXXXX)"
  run bash -c '
    source "$1/lib/vm/lifecycle.sh"
    port="$(vm_lifecycle_metrics_select_port /unused "$2" '\''{"metrics":"on"}'\'' 0 "" 2>/dev/null)"
    printf "<%s>" "$port"
  ' _ "$ROOT" "$runtime"
  rmdir "$runtime"
  [ "$status" -eq 0 ]
  [ "$output" = "<>" ]
}

@test "VM metrics management remains separate from experimental run/up" {
  run bash "$ROOT/lab-vm" --help
  [ "$status" -eq 0 ]
  [[ "$output" == *"metrics up|status|down"* ]]
  [[ "$output" == *"run [WORKSPACE] [--name NAME]"* ]]
  [[ "$output" == *"up [PROFILE] --workspace PATH"* ]]
  [[ "$output" == *"Not yet implemented"* ]]
}
