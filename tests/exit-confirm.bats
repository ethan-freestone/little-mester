#!/usr/bin/env bats

# Regression guard for ticket #37: Ctrl+C opens a close-confirmation dialog,
# Ctrl+D does nothing, and letter keys are never captured outside the dialog.
#
# These are static checks. Live behaviour is recorded on the ticket once
# verified in a running TUI (see docs/DECISIONS.md J.10).

setup() {
  LAB_DIR="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
  PLUGIN="$LAB_DIR/agent/opencode/plugins/little-mester-exit-confirm/tui.tsx"
  DOCKERFILE="$LAB_DIR/agent/Dockerfile"
}

@test "exit-confirm uses a real dialog, not the undocumented root slot" {
  run grep -n 'append: "root"' "$PLUGIN"
  [ "$status" -eq 1 ]

  run grep -n 'dialog\.show' "$PLUGIN"
  [ "$status" -eq 0 ]

  run grep -n 'append: "app"' "$PLUGIN"
  [ "$status" -eq 1 ]
}

@test "exit-confirm keeps letter keys inside a dialog-gated modal layer" {
  run python3 - "$PLUGIN" <<'PY'
import re
import sys

src = open(sys.argv[1], encoding="utf-8").read()
try:
    global_start = src.index('mode: "global"')
    modal_start = src.index('mode: "modal"')
except ValueError as exc:
    raise SystemExit(f"missing keymap layer mode: {exc}")
if global_start > modal_start:
    raise SystemExit("global layer must be declared before modal layer")

bind = re.compile(r'bind:\s*"([^"]+)"')
global_binds = bind.findall(src[global_start:modal_start])
modal_binds = bind.findall(src[modal_start:])

if global_binds != ["ctrl+c"]:
    raise SystemExit(f"global layer must bind only ctrl+c, got {global_binds}")

modal_letters = []
for spec in modal_binds:
    for key in spec.split(","):
        if not key.startswith("ctrl+") and key not in {"escape"}:
            modal_letters.append(key)
if not modal_letters:
    raise SystemExit("modal layer must bind y/q/n keys")
if any(not re.fullmatch(r"(shift\+)?[yqn]", key) for key in modal_letters):
    raise SystemExit(f"unexpected modal letter binds: {modal_letters}")

modal_block = src[modal_start:]
if "enabled: () => open()" not in modal_block:
    raise SystemExit("modal layer must be gated on the dialog being open")
PY
  [ "$status" -eq 0 ]
}

@test "exit-confirm dispatches app.exit by command id after confirming" {
  run grep -n 'context\.keymap\.dispatch("app\.exit")' "$PLUGIN"
  [ "$status" -eq 0 ]

  run grep -n 'context\.ui\.dialog\.clear()' "$PLUGIN"
  [ "$status" -eq 0 ]
}

@test "Dockerfile disables ctrl+c/ctrl+d exit and prompt.clear ctrl+c" {
  run grep -Fq '"keybinds":{"app.exit":"none","prompt.clear":"none"}' "$DOCKERFILE"
  [ "$status" -eq 0 ]

  # No stale alias, and no single-key exit left in the app.exit binding.
  run grep -Eq '"app[._]exit"[[:space:]]*:[[:space:]]*"(ctrl|q|d|c)' "$DOCKERFILE"
  [ "$status" -eq 1 ]
}

@test "obsolete v1 tui.json is gone" {
  [ ! -e "$LAB_DIR/agent/opencode/tui.json" ]
}

@test "exit-confirm plugin is baked into the image" {
  run grep -Fq 'COPY opencode/plugins/little-mester-exit-confirm/ /etc/opencode/plugins/little-mester-exit-confirm/' "$DOCKERFILE"
  [ "$status" -eq 0 ]

  run grep -Fq '/etc/opencode/plugins/little-mester-exit-confirm' "$DOCKERFILE"
  [ "$status" -eq 0 ]
}
