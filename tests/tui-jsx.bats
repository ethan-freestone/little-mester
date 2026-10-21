#!/usr/bin/env bats

# Regression guard for OpenTUI JSX mistakes in TUI plugins.
#
# A <text> nested inside a <text> does not fail at plugin load time -- it fails
# while the slot renders, which shows up in the TUI as
#   "little-mester.host-metrics-tui crashed in slot sidebar.footer:
#    TextNodeRenderable only accepts strings, TextNodeRenderable instances,
#    or StyledText instances"
# and leaves the slot empty. scripts/check-tui-jsx.py catches that statically.

setup() {
  LAB_DIR="$(cd "$(dirname "${BATS_TEST_FILENAME}")/.." && pwd)"
  CHECKER="$LAB_DIR/scripts/check-tui-jsx.py"
  PLUGINS=("$LAB_DIR"/agent/opencode/plugins/*/tui.tsx)
}

@test "TUI plugins contain no nested <text> elements" {
  run python3 "$CHECKER" "${PLUGINS[@]}"
  [ "$status" -eq 0 ]
  [[ "$output" != *"R1"* ]]
}

@test "checker flags a <text> nested inside a <text>" {
  fixture="$(mktemp -t tui-jsx-XXXXXX)"
  cat > "$fixture" <<'EOF'
export function Bad(props: { state: () => string }) {
  return (
    <box>
      <text><text fg="#aaa">GPU </text>{props.state()}</text>
    </box>
  )
}
EOF
  run python3 "$CHECKER" "$fixture"
  [ "$status" -eq 1 ]
  [[ "$output" == *"R1"* ]]
  rm -f "$fixture"
}

@test "checker flags a bare fg prop on a span" {
  fixture="$(mktemp -t tui-jsx-XXXXXX)"
  cat > "$fixture" <<'EOF'
export function Bad(props: { state: () => string }) {
  return (
    <box>
      <text><span fg="#aaa">GPU </span>{props.state()}</text>
    </box>
  )
}
EOF
  run python3 "$CHECKER" "$fixture"
  [ "$status" -eq 1 ]
  [[ "$output" == *"R2"* ]]
  rm -f "$fixture"
}

@test "checker flags fontWeight in a style object" {
  fixture="$(mktemp -t tui-jsx-XXXXXX)"
  cat > "$fixture" <<'EOF'
export function Bad(props: { state: () => string }) {
  return (
    <box>
      <text><span style={{ fontWeight: "bold" }}>GPU </span>{props.state()}</text>
    </box>
  )
}
EOF
  run python3 "$CHECKER" "$fixture"
  [ "$status" -eq 1 ]
  [[ "$output" == *"R3"* ]]
  rm -f "$fixture"
}

@test "checker accepts a valid span-based text" {
  fixture="$(mktemp -t tui-jsx-XXXXXX)"
  cat > "$fixture" <<'EOF'
export function Good(props: { state: () => string }) {
  return (
    <box flexDirection="row" gap={1}>
      <text><span style={{ fg: "#aaa" }}>GPU </span>{props.state()}</text>
      <text fg="#34a853">{"spark"}</text>
    </box>
  )
}
EOF
  run python3 "$CHECKER" "$fixture"
  [ "$status" -eq 0 ]
  rm -f "$fixture"
}
