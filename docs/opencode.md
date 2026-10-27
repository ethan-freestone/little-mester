# OpenCode Customisation Decisions (J)

> **What goes in here:** Decisions about how OpenCode v2 is configured, customised, and extended in little-mester. Covers plugins, TUI slots, config locations, the metrics sidecar, and exit-confirmation.
>
> **Read before every session:** [docs/DECISIONS.md](DECISIONS.md) §⚠️ Read before every session.

---

## J. OpenCode customisation (v2)

### Current status
- Done: logo plugin rewritten to the v2 plugin contract; config wiring moved to v2 mechanisms (J.2).
- Verified: ChatGPT Plus device-code authentication works from the agent container with `chatgpt-headless` (J.2.1).
- Not implemented: secure export/import and persistence of that credential across disposable VM recreation (J.2.1).
- Not confirmed: whether the logo actually renders on the home screen (see J.4).
- Rule: record test results in J.4. Do not add assumptions to this section.

### J.1 We run OpenCode v2. Do not use v1 docs or examples.

We install `@opencode/cli` (v2). The `opencode-ai` package is v1. Most search results, blog posts and plugin repos describe v1, and **v1 plugin code does not run in v2**.

| | v1 (do NOT use) | v2 (use this) |
|---|---|---|
| TUI/CLI config file | `tui.json` | `cli.json` (global only, no project-local file) |
| Plugin list key | `"plugin": [...]` | `"plugins": [...]` |
| Config location override | `OPENCODE_TUI_CONFIG` | `OPENCODE_CLI_CONFIG_CONTENT` (inline JSON) |
| Package import | `@opencode-ai/plugin/tui` | `@opencode/plugin/tui` |
| Entry point | `export default { id, tui(api) }` | `export default Plugin.define({ id, setup(context) })` |
| Slot registration | `api.slots.register({ slots: { home_logo } })` | `context.ui.slot({ <placement>: "<slot>", render })` |
| Slot names | `home_logo`, `sidebar_content` | dotted: `home.footer`, `sidebar.content`, ... |

A legacy `tui.json` is read **only once by the migration routine** to populate `cli.json`
(`packages/cli/src/config/migrate.ts`); it is not a live config file. Shipping one has no effect on
plugin loading and must not be used to register plugins or keybinds.

**Before changing any plugin or TUI config, read the v2 docs (not the v1 docs):**
- https://opencode.ai/v2/docs/build/plugins/cli (writing TUI plugins, slot list)
- https://opencode.ai/v2/docs/cli/plugins (loading plugins, discovery paths)
- https://opencode.ai/v2/docs/cli/config (every `cli.json` setting)
- https://opencode.ai/v2/docs/build/plugins/migrate-v1 (what changed from v1)

### J.2 Where config lives in the container

- **Server config** (`opencode.json`: providers, models, permissions): baked to `/etc/opencode/opencode/opencode.json`, selected by `OPENCODE_CONFIG`. This works. Local models appear.
- **CLI/TUI config** (`cli.json`, plugin list): supplied by the `OPENCODE_CLI_CONFIG_CONTENT` env var in the Dockerfile.
- **Plugin files**: baked to `/etc/opencode/plugins/<plugin-name>/`.
- **Never put OpenCode config or plugins under `/home/agent` or `~/.config/opencode`.** `/home/agent` is a tmpfs (see [DECISIONS.md](DECISIONS.md) §A.4), so anything copied there at build time is hidden at runtime. Observed when we tried it: local models disappeared. The tmpfs shadowing is the likely cause.
- Check that `compose.yaml` does not override or unset `OPENCODE_CONFIG` or `OPENCODE_CLI_CONFIG_CONTENT`.

Saved provider credentials are different from configuration. OpenCode stores them in its data database, which currently resolves to `/home/agent/.local/share/opencode/opencode.db` and is erased when the `/home/agent` tmpfs is recreated. See J.2.1 before changing this behavior.

Dockerfile:
```dockerfile
RUN npm install -g @opencode/cli && opencode --version

COPY opencode/opencode.json /etc/opencode/opencode/opencode.json
COPY opencode/plugins/little-mester-logo/ /etc/opencode/plugins/little-mester-logo/

ENV OPENCODE_CONFIG=/etc/opencode/opencode/opencode.json \
    OPENCODE_CLI_CONFIG_CONTENT='{"plugins":["/etc/opencode/plugins/little-mester-logo"]}'
```

### J.2.1 ChatGPT Plus authentication in containers and VMs

The verified callback-free login command is:

```bash
opencode auth login openai --method chatgpt-headless
```

It displays a device code for `https://auth.openai.com/codex/device`. The user completes authorization in a host browser while the container polls OpenAI, so no localhost callback or API key is required.

This login is currently container-local because provider credentials live in the OpenCode database under `/home/agent`, which is tmpfs. The VM implementation must use OpenCode's supported `auth export`/`auth import` commands, persist the narrow OpenCode data directory outside the agent tmpfs, and save the latest refreshed credential before deleting the VM overlay. Credentials must never be placed in the workspace, Git, cloud-init ISO, shared base image, or logs.

The complete operator procedure, verified observations, security requirements, and pending VM lifecycle design are in [`docs/chatgpt-auth.md`](chatgpt-auth.md). Treat that document as the runbook for the next implementation session.

### J.3 Logo plugin

Canonical source (single copy, no duplicates):
```
agent/opencode/plugins/little-mester-logo/
  package.json    { "name": "little-mester-logo", "type": "module", "exports": { "./tui": "./tui.tsx" } }
  tui.tsx         Plugin.define({ id: "little-mester.logo", setup(context) { return context.ui.slot({...}) } })
```

- `agent/opencode/tui.json` and the loose `.txt` copies under `plugins/` are obsolete and should be deleted.
- Colors come from `context.theme` tokens (the docs use `context.theme.text.base`), not string names like `"primary"`.
- `render` must return JSX, not a component reference.
- `setup` returns the unregister function from `context.ui.slot(...)`.

### J.4 Unverified. Test, then record the result here.

1. **Home-logo slot.** The documented v2 slots are `app`, `home.footer`, `home.footer.status`, `prompt.footer`, `prompt.footer.status`, `prompt.footer.file`, `session.composer.top`, `sidebar.content`, `sidebar.footer`. There is no documented logo slot. The plugin currently uses `prepend: "home.footer"`, which shows our logo but does **not** replace the built-in one. To test replacing it, try `replace: "home.logo"` (guessed name). If it fails, check `packages/plugin/src/tui/` in https://github.com/anomalyco/opencode for the real slot list, or ask upstream for one.
2. **`context.theme.text.subdued`.** Used for the lower logo lines. Not in the docs. Falls back to the default color if undefined. **Verified:** the resolved theme (`@opencode/theme` 2.1.2) exposes `text.base`, `text.muted` and `text.faint` — there is no `text.subdued`. The metrics plugin uses `text.muted` (with a literal fallback).
3. **JSX pragma.** `tui.tsx` has no `@jsxImportSource` pragma because the v2 docs' JSX examples do not use one. If JSX fails to resolve, restore `/** @jsxImportSource @opentui/solid */`.
4. **Directory-path plugin form** (`/etc/opencode/plugins/<name>` in `plugins`) with a `.tsx` entry. The docs show directory paths and `./tui` exports, but not `.tsx` entries specifically.

### J.5 Verifying and debugging

- TUI rendering is guarded statically by `scripts/check-tui-jsx.py` (see §J.9) — it catches the JSX
  mistakes that make a slot die at render time. It is run by `tests/tui-jsx.bats` in the normal
  `bats /workspace/tests/` suite. It cannot prove that a slot *looks* right; for that, drive the
  real TUI (see §J.9) or render the slot headlessly.
- To confirm the plugin loads at all (separately from whether the slot renders), add `context.ui.toast.show({ message: "little-mester logo plugin loaded" })` at the top of `setup`.
- The v2 docs say to confirm a plugin's ID and source appear in the active plugin list.
- `console.log` from a TUI plugin may not appear in server logs.
- For **server-side** plugins, the log line `loading plugin id=<path> entrypoint=file://<path>/index.ts` confirms the plugin was picked up. The server also hot-reloads: a new file under `<project>/.opencode/plugins/` or `<global-config>/plugins/` is picked up without a restart.

### J.6 Custom tools in v2 are plugins, not markdown files

- There is **no** `tools/` directory convention in v2. A markdown file in `tools/` (the v1 way) registers nothing — opencode v2 silently ignores it.
- Custom tools are registered from a plugin via `ctx.tool.transform((editor) => editor.add({ name, description, input, execute }))`. See https://opencode.ai/v2/docs/build/plugins/ (section "Tools").
- Plugin discovery paths: `<global-config>/plugins/<name>/index.ts` and `<project>/.opencode/plugins/<name>/index.ts`. A plugin package keeps its server entry (`index.ts`) and optional TUI entry (`tui.ts`) together.
- **Server-side** plugins (tools, commands, transforms) belong in the **server** config's `plugins` array (`opencode.json`). **TUI-only** plugins belong in the CLI plugin list (`cli.json` / `OPENCODE_CLI_CONFIG_CONTENT`).
- `agent/opencode/opencode.json` is **generated** by `scripts/regen-configs` — anything added to it by hand is lost on the next regen. Add it to `gen_opencode_json()` instead.
- Our implementation: `agent/opencode/plugins/host-metrics/` → baked to `/etc/opencode/plugins/host-metrics/` → listed in the generated `opencode.json`. It registers the `get_host_metrics` tool.

### J.7 The metrics sidecar must bind 0.0.0.0 in its own netns

- A container binding `127.0.0.1` is reachable **only from inside that container**. Sibling containers reach it by container IP / Docker DNS, which requires a bind on `0.0.0.0` (or the container IP).
- `0.0.0.0` inside the container netns is **not** "exposed to the host": exposure to the host is controlled by `ports:` in compose, which we deliberately do not set. The `internal: true` network has no route off the Docker network.
- Consequence for verification: host-side scripts cannot `curl 127.0.0.1:19532`. They must run a client container attached to the compose network — see `scripts/verify-metrics.sh`.
- Host paths mounted into the sidecar must match the constants in `gather.py` exactly (`/host/proc`, `/host/hwmon`, `/host/thermal`). A mismatch fails silently and yields empty readings, so `scripts/verify-metrics.sh` audits the mounts (criterion A9).
### J.8 Metrics sidecar ring buffer + TUI sparkline (ticket-011)

- The old 2s TTL cache meant that polling at sub-TTL intervals always returned stale data.
  Replaced it with a **daemon sampler thread** that runs `_aggregate()` every 500ms and feeds both
  a `_latest` snapshot and a rolling-ring (max 240 entries) of historical samples.
- New `GET /history?n=N` endpoint returns the last N ring-buffer samples as JSON, used by the TUI
  plugin to seed charts immediately on mount instead of starting blank.
- The TUI plugin (`host-metrics-tui`) renders in `append: "sidebar.footer"` — a compact one-line
  sparkline (GPU util + VRAM bars) by default; clicking toggles an expanded detail view with GPU
  util / VRAM % time-series charts, temperature, and system overview.
- The plugin is baked to `/etc/opencode/plugins/host-metrics-tui/` via Dockerfile and listed in the
  `plugins` array of both `opencode.json` and `scripts/regen-configs`.
- V2 TUI reactivity (how data changes trigger render updates) is **confirmed** — see §J.9. The plugin
  keeps its data in a `createSignal<State>` and reads `props.state()` **inside the JSX expressions**
  (never hoisted into a local), so Solid re-renders the slot on every poll.
- The compact view is **two short lines** (`GPU 98% ▁▃██████████████` / `VRAM 7.2/8.0G 58°C`), sized
  for the narrow sidebar; the expanded view stacks one row per metric plus CPU/RAM overview.

### J.9 OpenTUI text rules (verified — a `<text>` inside a `<text>` kills the slot)

**The crash.** The v2 TUI renders plugin JSX through `@opentui/solid`. A `<text>` element is a
`TextRenderable`; a `<text>`'s children may only be **strings, `TextNodeRenderable` instances or
`StyledText`**. A `TextRenderable` is none of those, so nesting one inside another makes the
renderer call `TextNodeRenderable.add()` with an illegal value and throw:

```
TextNodeRenderable only accepts strings, TextNodeRenderable instances, or StyledText instances
    at add (TextNode.ts:126)
    at _insertNode (reconciler.ts:112)
```

OpenCode catches that and shows `little-mester.host-metrics-tui crashed in slot sidebar.footer: …`.
The slot then renders **nothing at all** — the plugin does not "look wrong", it is simply absent.

**The rules.**

| # | Rule | Consequence |
|---|------|-------------|
| R1 | Inline fragments inside a `<text>` must be `<span>` | `<span>` is a `TextNodeRenderable`; a nested `<text>` is not |
| R2 | A `<span>` takes its colour from `style={{ fg: "#rrggbb" }}` | a bare `fg=` prop on a span is **silently ignored** (only `<text>` understands it) |
| R3 | Bold is `<strong>` (or `<b>`) | `style={{ fontWeight: "bold" }}` is **silently ignored** — the style object understands `bold`/`italic`/`underline`/`dim`, not `fontWeight` |

The full inline catalogue is `box, text, input, select, textarea, ascii_font, tab_select, scrollbox,
code, diff, line_number, markdown, image, span, strong, b, em, i, u, br, a` — `span`/`strong`/`b`/`em`/`i`/`u`
are the inline ones. There is no `<bold>` element (the ticket-011 sketch used one).

**Why a static check.** These mistakes do **not** fail at plugin load time — they fail while the slot
renders, i.e. only in a running TUI. So `scripts/check-tui-jsx.py` checks them statically (R1/R2/R3)
and `tests/tui-jsx.bats` runs it over every `agent/opencode/plugins/*/tui.tsx`. On the pre-fix file it
reports all 10 nested-`<text>` sites and all 5 `fontWeight` sites.

**How it was verified** (OpenCode 2.1.2, `@opentui/core` 0.6.x):

1. *Headless* — render the slot in a real `@opentui/solid` renderer (`testRender`) with bun + FFI:
   the old file throws the exact error above, the new file renders both compact and expanded views.
2. *Live* — run the real TUI in a pty, type a prompt, press `<leader>b` (`ctrl+x`, then `b`) to toggle
   the sidebar, and read the screen:
   - old plugin → `crashed in slot … TextNodeRenderable only accepts …`, no metrics at all
   - new plugin → `GPU 98% ▁▃██████████████` / `VRAM 7.2/8.0G 58°C`, no crash

**Gotcha for live tests:** OpenCode auto-discovers every plugin directory under the global plugin
dir, so a "fixed" copy outside `/etc/opencode/plugins` does *not* replace the baked-in one — the old
plugin still loads and still crashes. Point `OPENCODE_CONFIG` at a private config directory whose
`plugins` array lists only the copy under test.

### J.10 Exit confirmation (ticket #37)

**Decision.** Ctrl+C is the only keyboard exit trigger, and it opens a visible confirmation dialog.
There is **no instant quit**: Ctrl+D, `<leader>q` and single-letter keys do not exit. While the
dialog is open, `Y`/`y` or `Q`/`q` confirms; `Esc`, `N`/`n` or Ctrl+C cancels; every other key is
swallowed so nothing leaks into the prompt. While the dialog is closed the plugin captures **only**
Ctrl+C — `y`, `Y`, `q`, `n` etc. type normally.

A literal "Ctrl+C+Q" chord is impossible: a terminal sends Ctrl+C as one control byte. The close
sequence is therefore **Ctrl+C, then Q** (Q presses the dialog's confirm action).

**Why the first implementation failed (verified against OpenCode 2.1.4 source).**

1. The overlay used slot `"root"`, which is not in the v2 `SlotMap` (`app`, `home.footer.*`,
   `prompt.footer.*`, `session.composer.top`, `session.panel`, `sidebar.content`, `sidebar.footer`).
   `"root"` is never mounted, so `setShow(true)` rendered nothing.
2. The plugin registered `y`, `Y`, `q` and `escape` as always-on global commands, so those letters
   were swallowed even with the dialog closed.
3. `agent/opencode/tui.json` is v1 config and is not loaded by v2, so its `app_exit: "q"` never
   applied — and OpenCode's default `app.exit` (`ctrl+c,ctrl+d,<leader>q`) stayed live, which is why
   Ctrl+D still quit instantly.

**Implementation contract.**

- Use `context.ui.dialog.show(...)` (a real modal dialog), not a slot overlay. `DialogProvider`
  pushes the `modal` input mode for the dialog's lifetime.
- Confirm/cancel keys live in a `mode: "modal"` keymap layer that is also `enabled: () => open()`.
  They are unreachable unless this specific dialog is open.
- The only global command is `ctrl+c` with `priority: 100`, which beats the prompt's own
  `prompt.clear` (`ctrl+c`) binding. That layer is disabled while the dialog is open so Ctrl+C
  cancels instead of reopening.
- `app.exit` is set to `"none"` and `prompt.clear` to `"none"` in
  `OPENCODE_CLI_CONFIG_CONTENT` (`agent/Dockerfile`). `app.exit` remains dispatchable **by command
  id**, so confirming calls `context.keymap.dispatch("app.exit")`; no keybinding is needed.
- Consequence: Ctrl+C no longer clears the prompt (owner accepted the workflow change). `/exit` and
  the other slash aliases still exist as deliberate, typed commands.
- `agent/opencode/tui.json` is deleted: dead v1 config that would have bound exit to a single `q`.

**Verification.** Static checks: `tests/tui-jsx.bats`, `tests/exit-confirm.bats` (26 tests total in
`bats tests/`, one unrelated skip). Live check: `scripts/verify-exit-confirm.py` drives a private
standalone TUI through a pty and asserts Ctrl+D is inert, `yq` types with the dialog closed, Ctrl+C
opens the dialog, Esc/N/Ctrl+C cancel without exiting, and q exits. It passed on OpenCode 2.1.4 in
the agent container; the run is recorded on ticket #37. The image must be rebuilt/restarted for the
baked `/etc/opencode` copy to replace any stale running copy.
