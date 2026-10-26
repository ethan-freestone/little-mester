/** @jsxImportSource @opentui/solid */
// little-mester.exit-confirm — intercepts Ctrl+C in the opencode TUI and asks
// for confirmation before closing Little Mester.
//
// v2 contract: default-export Plugin.define({ id, setup(context) }).
//
// The default `app_exit` keybind includes `ctrl+c`, which would exit
// immediately. This plugin rebinds `ctrl+c` to a confirmation overlay rendered
// via a slot; on confirm it dispatches `app_exit` to close; on cancel or
// Escape it returns to the session. The `app_exit` keybind in tui.json is
// narrowed to drop `ctrl+c` so the two bindings do not clash.

import { Plugin } from "@opencode/plugin/tui"
import { createSignal } from "solid-js"

export default Plugin.define({
  id: "little-mester.exit-confirm",

  setup(context) {
    const [show, setShow] = createSignal(false)

    context.keymap.layer(() => ({
      mode: "global",
      commands: [{
        id: "little-mester.exit-confirm",
        title: "Confirm exit",
        bind: "ctrl+c",
        run: () => setShow(true),
      }],
    }))

    // Render a full-screen confirmation overlay via slot.
    const unregister = context.ui.slot({
      append: "root",
      render: () => {
        if (!show()) return null
        return (
          <box
            flexDirection="column"
            width="100%"
            height="100%"
            justifyContent="center"
            alignItems="center"
            bg="#1a1a2e"
          >
            <box
              flexDirection="column"
              paddingX={2}
              paddingY={1}
              bg="#16213e"
              border={{ style: "round", color: "#e94560" }}
              width={54}
            >
              <text fg="#e94560">
                <strong>Close Little Mester?</strong>
              </text>
              <text>Are you sure you want to close Little Mester?</text>
              <box flexDirection="row" gap={2} marginTop={1}>
                <text fg="#8a8a8a">[Y]es  </text>
                <text fg="#8a8a8a">[Esc] Cancel</text>
              </box>
            </box>
          </box>
        )
      },
    })

    // Overlay key handlers: Y to confirm, Esc/q to cancel.
    context.keymap.layer(() => ({
      mode: "global",
      commands: [
        {
          id: "little-mester.exit-confirm.yes",
          title: "Confirm exit",
          bind: "y",
          run: () => {
            context.keymap.dispatch("app_exit")
            setShow(false)
          },
        },
        {
          id: "little-mester.exit-confirm.cancel",
          title: "Cancel exit",
          bind: "escape",
          run: () => setShow(false),
        },
        {
          id: "little-mester.exit-cancel-q",
          title: "Cancel exit",
          bind: "q",
          run: () => setShow(false),
        },
      ],
    }))

    return unregister
  },
})
