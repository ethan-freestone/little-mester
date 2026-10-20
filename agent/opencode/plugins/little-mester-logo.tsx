/** @jsxImportSource @opentui/solid */

// TUI slot plugin — replaces OpenCode's default home-screen logo with Little Mester ASCII art.
// Pattern: same TuiPluginModule / slot-registration model as oc-plugin-vault-tec (the working reference).
// The open-code source uses "replace" mode for the home_logo slot so one registered implementation
// fully replaces the default logo — no merge, no overlap.

import type { TuiPluginApi } from "@opencode-ai/plugin/tui"

const LOGO = ` ▄▄▄▄▄▄▄▄▄▄▄▄▄   ██        ██████  ██████████  ██████████  ██        ████████
 █████████████░  ██▒        ▒██▒▒▒  ▒▒▒██▒▒▒▒▒  ▒▒▒██▒▒▒▒▒ ██▒       ██▒"▒▒▒▒▒▒▒
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

// Build the JSX tree once (static, no per-render overhead).
// First two lines use "primary" theme token to make the logo pop; rest uses textMuted.
const LOGO_LINES = LOGO.split("\n")
const LogoBody: JSX.Element[] = LOGO_LINES.map((line, i) => {
  const fg = i < 2 ? "primary" : "textMuted"
  return (
    <text key={i} fg={fg}>
      {line}
    </text>
  )
})

// The home_logo slot — replaces the default logo entirely (slot mode: replace).
const LogoSlot = () => (
  <box flexDirection="column" gap={0}>
    {LogoBody}
  </box>
)

// TuiPluginModule — must export { id, tui } exactly.
export default {
  id: "little-mester-logo",

  // The tui function runs when the plugin loads; register our slot into the host registry.
  tui: async (api: TuiPluginApi) => {
    api.slots.register({
      slots: {
        home_logo(): JSX.Element | undefined {
          return LogoSlot as unknown as JSX.Element
        },
      },
    })
  },
}
