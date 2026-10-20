// TUI plugin (OpenCode v2): renders the Little Mester ASCII logo on the home screen.
// v2 contract: default-export Plugin.define({ id, setup(context) }).
// The old { id, tui } / api.slots.register shape is v1 and does not run in v2.

import { Plugin } from "@opencode/plugin/tui"

const LOGO = ` ▄▄▄▄▄▄▄▄▄▄▄▄▄   ██        ██████  ██████████  ██████████  ██        ████████
 █████████████░  ██▒        ▒██▒▒▒  ▒▒▒██▒▒▒▒▒  ▒▒▒██▒▒▒▒▒ ██▒       ██▒▒▒▒▒▒▒
 █▒█▒█▒█▒█▒█▒█░  ██▒         ██▒       ██▒         ██▒     ██▒       ██████
 █▒█▒█▒█▒█▒█▒█░  ██▒         ██▒       ██▒         ██▒     ██▒       ██▒▒▒▒▒
 █████████████░  ████████  ██████      ██▒         ██▒     ████████  ████████
 █▒█▒█▒█▒█▒█▒█░   ▒▒▒▒▒▒▒▒  ▒▒▒▒▒▒      ▒▒          ▒▒      ▒▒▒▒▒▒▒▒  ▒▒▒▒▒▒▒▒
 █▒█▒█▒█▒█▒█▒█░
 █████████████░  ██      ██  ████████    ██████  ██████████  ████████  ██████
 █▒█▒█▒█▒█▒█▒█░  ████  ████▒ ██▒▒▒▒▒▒▒ ██ ▒▒▒▒▒▒  ▒▒▒██▒▒▒▒▒ ██▒▒▒▒▒▒▒ ██▒▒▒▒██
 █▒█▒█▒█▒█▒█▒█░  ██▒▒██ ▒██▒ ██████    ████          ██▒     ██████    ██████ ▒▒
 ▐▓▓▓▓▓▓▓▓▓▓▓▓▓▌  ██▒  ▒▒ ██▒ ██▒▒▒▒▒    ▒▒▒▒ ██      ██▒     ██▒▒▒▒▒   ██▒▒██▒
 █  █  ▐▌  █  █  ██▒     ██▒ ████████  ██████ ▒▒     ██▒     ████████  ██▒  ▒██
 █  █  ▐▌  █  █   ▒▒      ▒▒  ▒▒▒▒▒▒▒▒  ▒▒▒▒▒▒        ▒▒      ▒▒▒▒▒▒▒▒  ▒▒    ▒▒
 ▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔  Made in Sheffield`

const LOGO_LINES = LOGO.split("\n")

export default Plugin.define({
  id: "little-mester.logo",

  setup(context) {
    // slot() returns an unregister function; returning it from setup cleans up on unload.
    return context.ui.slot({
      // "home.footer" is a documented v2 slot, so this proves the plugin loads and renders.
      // "home.logo" does not exist as a real slot yet (the slot doesn't exist in OpenCode's
      // v2 slot registry). If upstream adds it later, swap the line above for:
      //   replace: "home.logo"
      prepend: "home.footer",
      render: () => (
        <box flexDirection="column">
          {LOGO_LINES.map((line, i) => (
            <text fg={i < 2 ? context.theme.text.base : context.theme.text.subdued}>
              {line}
            </text>
          ))}
        </box>
      ),
    })
  },
})