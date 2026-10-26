/** @jsxImportSource @opentui/solid */
// little-mester.exit-confirm — intercepts Ctrl+C in the opencode TUI and asks
// for confirmation before closing Little Mester.
//
// v2 contract: default-export Plugin.define({ id, setup(context) }).
//
// The default `app_exit` keybind includes `ctrl+c`, which would exit
// immediately. This plugin rebinds `ctrl+c` to a confirmation dialog: on
// confirm it dispatches `app_exit` to close; on cancel it returns to the
// session. The `app_exit` keybind in tui.json is narrowed to drop `ctrl+c`
// so the two bindings do not clash.

import { Plugin } from "@opencode/plugin/tui"

export default Plugin.define({
  id: "little-mester.exit-confirm",

  setup(context) {
    const { DialogConfirm, dialog } = context.ui
    // The dialog stack exposes `show` in current OpenCode versions and `replace`
    // in older type declarations; support either.
    const openDialog: (render: () => unknown, onClose?: () => void) => void =
      typeof dialog.show === "function" ? dialog.show.bind(dialog) : dialog.replace.bind(dialog)

    context.keymap.layer(() => ({
      mode: "global",
      commands: [{
        id: "little-mester.exit-confirm",
        title: "Confirm exit",
        bind: "ctrl+c",
        run: () => {
          openDialog(
            () => (
              <DialogConfirm
                title="Close Little Mester?"
                message="Are you sure you want to close Little Mester?"
                onConfirm={() => {
                  dialog.clear()
                  const result = context.keymap.dispatch("app_exit")
                  if (!result.ok) {
                    // Fallback if the built-in exit command is unavailable.
                    process.exit(0)
                  }
                }}
                onCancel={() => {
                  dialog.clear()
                }}
              />
            ),
            () => { dialog.clear() }
          )
        },
      }],
    }))
  },
})
