# host-metrics-tui — Live GPU / RAM / Temperature Sparkline

## Description

A lightweight OpenCode v2 TUI plugin that renders a constantly-refreshing hardware status
indicator in the sidebar footer. It polls the metrics sidecar at 2 Hz and displays:

- **GPU utilization** as an ASCII sparkline (`▁▂▃▄▅▆█`) with current percentage
- **VRAM usage** as another sparkline with GB values
- **GPU temperature** as a plain number

Clicking the sidebar footer toggles an expanded detail view showing full time-series
charts for GPU util and VRAM %, plus system overview (CPU cores, RAM).

## How it works

### Data flow
1. On setup: fetches `/history?n=60` from the metrics sidecar to seed historical data.
2. Every 500 ms: polls `/metrics` → extracts the latest GPU sample → pushes it into local arrays (max 240 entries each).
3. The slot re-renders with fresh sparklines every poll (the `render` callback is called
   each time new data arrives).

### Endpoints used
| Endpoint | Purpose |
|----------|---------|
| `/history?n=60` | Seed initial history on first poll (ring buffer from Phase 1) |
| `/metrics` | Push latest GPU frame into the live chart array |

### Error handling
- If `fetch` throws for any reason: the sparkline retains its last known data and keeps
  retrying every 500 ms. No hard failure — the indicator simply doesn't update until
  connectivity is restored.

## Registration

Baked into the agent image at `/etc/opencode/plugins/host-metrics-tui/` via Dockerfile
(`COPY opencode/plugins/host-metrics-tui/`). Listed in `opencode.json` under the
`plugins` array — see `scripts/regen-configs` for the generator.

## Slot placement

Registered as `append: "sidebar.footer"` so it appears below existing sidebar footer
content (session info, navigation hints). Clicking/toggling switches between compact
and expanded view.

### Layout

Compact (default) — two short lines, sized for the narrow sidebar:

```
GPU 98% ▁▃██████████████
VRAM 7.2/8.0G 58°C
```

Expanded — one row per metric plus a system overview:

```
▸ System Monitor 60 samples
NVIDIA GeForce RTX 5070 Laptop GPU
GPU ▁▃██████████████ 98% 0-100
VRM ▁▃██████████████ 7.2/8.0 GB (90%) 88-91
Tmp ▁▁▁▁▁▁████████ 58°C
CPU: AMD Ryzen AI 9 HX 370 w/ Radeon 890M · 24 cores · load 0.32/0.32/0.40
RAM: 11.4 / 61.9 GB (18%)
```

## ⚠️ OpenTUI text rules (do not break these)

The TUI renders this file's JSX through `@opentui/solid`. Three rules are not obvious
from JSX and fail **at render time** (not at load time) — a violation kills the whole
slot and the plugin disappears from the sidebar:

1. **Never nest a `<text>` inside a `<text>`.** A `<text>` is a `TextRenderable`; a
   `<text>`'s children may only be strings, `TextNodeRenderable`s or `StyledText`.
   Nesting throws `TextNodeRenderable only accepts strings, TextNodeRenderable
   instances, or StyledText instances` and OpenCode reports
   `crashed in slot sidebar.footer`. Use `<span>` for inline fragments — `<span>` *is* a
   `TextNodeRenderable`.
2. **Colour on a `<span>` goes through `style={{ fg: "#rrggbb" }}`.** A bare `fg=` prop on
   a span is silently ignored (only `<text>` elements understand it).
3. **Bold is `<strong>` (or `<b>`).** `style={{ fontWeight: "bold" }}` is silently ignored —
   the style object understands `bold`/`italic`/`underline`/`dim`, not `fontWeight`.

Also: read `props.state()` **inside the JSX expressions**, never hoist it into a local —
Solid only tracks the signal where it is read inside a reactive expression.

`scripts/check-tui-jsx.py` (run by `tests/tui-jsx.bats`) enforces rules 1–3 statically.
See `docs/DECISIONS.md` §J.9.
