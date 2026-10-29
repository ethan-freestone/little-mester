/** @jsxImportSource @opentui/solid */
// little-mester.exit-confirm — Ctrl+C opens a confirmation dialog. There is no
// instant single-key quit.
//
// Contract notes (verified against OpenCode 2.1.4 source):
// - `app.exit` is rebound to "none" in OPENCODE_CLI_CONFIG_CONTENT
//   (agent/Dockerfile), so the defaults ctrl+c / ctrl+d / <leader>q never exit.
// - While the dialog is closed this plugin binds ctrl+c only. No letter key is
//   captured from the prompt.
// - While a dialog is open OpenCode pushes the "modal" input mode. The
//   confirmation keys live in a modal layer that is also gated on our own
//   dialog being open, so y/Y/q/Q/n/N are captured only for this dialog.
// - Confirmation dispatches the `app.exit` command by ID. Dispatch works even
//   when the command has no key binding.
// - The prompt is a real ui.dialog, not a slot overlay: slot "root" does not
//   exist in v2 (see docs/DECISIONS.md J.10).
// - JSX follows the OpenTUI text rules in docs/DECISIONS.md J.9.

import { Plugin } from "@opencode/plugin/tui"
import { createSignal } from "solid-js"

export default Plugin.define({
  id: "little-mester.exit-confirm",

  setup(context) {
    const [open, setOpen] = createSignal(false)
    let active = false

    const finish = (confirmed: boolean) => {
      if (!active) return
      active = false
      setOpen(false)
      context.ui.dialog.clear()
      if (confirmed) context.keymap.dispatch("app.exit")
    }

    // The only binding available from the prompt. Priority 100 beats the
    // prompt's own ctrl+c (prompt.clear) binding; the layer is disabled while
    // our dialog is open so the modal ctrl+c handler (cancel) wins.
    context.keymap.layer(() => ({
      mode: "global",
      priority: 100,
      enabled: () => !open(),
      commands: [
        {
          id: "little-mester.exit-confirm.open",
          title: "Close Little Mester",
          group: "Little Mester",
          bind: "ctrl+c",
          run: () => {
            if (open()) return
            active = true
            setOpen(true)
            context.ui.dialog.show(
              () => (
                <box
                  flexDirection="column"
                  paddingX={2}
                  paddingY={1}
                  gap={1}
                  backgroundColor="#00000030"
                >
                  <text fg="#e94560">
                    <strong>Close Little Mester?</strong>
                  </text>
                  <text>Are you sure you want to close Little Mester?</text>
                  <box flexDirection="row" gap={2}>
                    <text fg="#8a8a8a">[Y] or [Q] close</text>
                    <text fg="#8a8a8a">[Esc] or [N] cancel</text>
                  </box>
                  <text fg="#8a8a8a">Press Ctrl+C again to cancel.</text>
                </box>
              ),
              () => {
                // Backdrop dismissal or any external close cancels.
                if (!active) return
                active = false
                setOpen(false)
              },
            )
            // show() resets dialog layout state, so apply centering after it.
            context.ui.dialog.set({ centered: true, size: "medium" })
          },
        },
      ],
    }))

    // Only reachable while our dialog is open (both the keymap "modal" mode
    // and `open()` must be true). This is what keeps y/q/n out of the prompt.
    context.keymap.layer(() => ({
      mode: "modal",
      enabled: () => open(),
      commands: [
        {
          id: "little-mester.exit-confirm.yes",
          title: "Close Little Mester",
          group: "Little Mester",
          bind: "y,shift+y",
          run: () => finish(true),
        },
        {
          id: "little-mester.exit-confirm.yes-q",
          title: "Close Little Mester",
          group: "Little Mester",
          bind: "q,shift+q",
          run: () => finish(true),
        },
        {
          id: "little-mester.exit-confirm.no",
          title: "Cancel closing Little Mester",
          group: "Little Mester",
          bind: "escape,n,shift+n,ctrl+c",
          run: () => finish(false),
        },
      ],
    }))

    return () => {
      active = false
      context.ui.dialog.clear()
    }
  },
})
