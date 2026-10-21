# Ticket 011 — Real-time GPU/CPU/RAM sparkline in the OpenCode TUI

## Objective

Replace the ad-hoc metrics sidecar verification workflow with an always-visible, live-updating hardware metrics chart in the OpenCode v2 TUI — compact by default, expandable for detail.

The agent container already has a working metrics sidecar (ticket-010 legacy) that gathers GPU, CPU, RAM, and temperature data from the host via `/proc` and NVML. This ticket makes that data continuously visible in the TUI at 2 Hz so the user can watch GPU util, VRAM, and temperature in real time while running `./lab`.

---

## Scope & Boundaries

### In scope
1. **Sidecar ring buffer + `/history` endpoint** — Replace the 2s TTL cache with a background sampler (500ms intervals) + ring buffer so charts can render immediately on TUI mount without waiting for data to accumulate.
2. **Compact sparkline plugin** — Always-visible in `sidebar.footer`: GPU util bar, VRAM usage, temperature as plain numbers; poll `/metrics` every 500 ms internally via a v2 TUI plugin.
3. **Expandable detail view** — Clicking/toggling the compact line reveals: time-series chart (last 60 s), full GPU snapshot, RAM/CPU status, and all temps.

### Out of scope
- Pushing metrics to Grafana / Prometheus.
- Temperature sensor fixes (hwmon symlink issue noted in DECISIONS.md §J.7).
- Per-process / per-container stats beyond the host summary already collected.

---

## Implementation

### Phase 1 — Sidecar: ring buffer + `/history`

**File to edit:** `agent/metrics/gather.py`

Replace the current `_TTLCache` class with a background sampler thread + ring buffer:

```python
import threading, collections, time, sys
from datetime import datetime, timezone

# ── Ring buffer ───────────────────────────────────────
_lock = threading.Lock()
_latest: dict | None = None  # current snapshot (served by GET /metrics)
_history: collections.deque[collections.OrderedDict] = collections.deque(
    maxlen=120  # 60 s at 2 Hz
)

# Sampler runs a background thread; no longer uses TTL cache.
def _sampler():
    global _latest
    while True:
        snap = _aggregate()          # re-use the existing function
        g = (snap["gpu"] or [{}])[0]  # first GPU, or empty dict if no data
        history_entry = collections.OrderedDict([
            ("t", snap["timestamp"]),
            ("ts", time.monotonic()),              # monotonic for interval logic
            ("gpu_util", g.get("utilization_percent", 0)),
            ("vram_pct", round(
                100 * g.get("memory_used_mb", 0)
                / max(g.get("memory_total_mb", 1), 1)), 4),
            ("gpu_temp", g.get("temperature_c", 0)),
            ("cpu_load", snap["cpu"]["load_avg_1m"]),
        ])
        with _lock:
            _latest = snap
            _history.append(history_entry)
        time.sleep(0.5)

# Start the sampler once at module import
_threading.Thread(target=_sampler, daemon=True).start()
```

**New endpoint — `GET /history?n=N`:** returns a JSON array of the last N ring-buffer samples (default 120).

```python
class _MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/history":
            q = int(urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query).get("n", ["120"])[0])
            with _lock:
                recent = list(_history)[-q:]   # last N entries
            payload = json.dumps(recent, indent=2)

        elif self.path == "/metrics":
            with _lock:
                data = dict(_latest) if _latest else _aggregate()
            payload = json.dumps(data, indent=2)
```

Also update `/summary` to read from `_latest` under the lock.

**What this solves:**
- The old 2s TTL meant the TUI would get stale data on fast polls (0.5 s) and see four identical values in a row.
- A freshly opened chart now renders a full window immediately from `/history`.

### Phase 2 — Sidecar: verify GPU is populated

**No changes needed.** NVML + `utility` capability are already correct in `compose.yaml` and `Dockerfile`. GPU data is being collected via NVML. The temperature array stays empty (hwmon symlink problem), but GPUs report temp via NVML which works fine.

### Phase 3 — TUI plugin: compact sparkline

**New directory:** `agent/opencode/plugins/host-metrics-tui/`
- `package.json` — standard v2 plugin entry
- `tui.tsx` — the chart implementation, following the logo plugin's `Plugin.define` pattern

#### Rendering (unexpanded / default)

Renders into the `sidebar.footer` slot (documented v2 slot): a single line that polls `/history` on mount and then pushes fresh data from `/metrics` every 500 ms:

```
GPU  ▁▃▅█▇▇▆▂▃▅▄    67%     VRAM 12.1/8.0 GB   58°C
```

The bar is the last ~30 data points of GPU util, mapped to `▁▂▃▄▅▆█` (scale 0–100 → 0–7). VRAM and temp are plain numbers that never vary enough at this scale for sparklines.

#### Polling strategy
1. On component mount: fetch `/history?n=30`, store in local array.
2. `setInterval(500)`: fetch `/metrics` → push into the array, keep it trimmed to 120 entries max.
3. Update the rendered sparkline on every poll via solid-like signal update (`createSignal`).
4. If any `fetch` throws: show `<text fg=yellow>GPU n/a</text>`, keep the interval alive (no dead state).

#### Implementation sketch (TypeScript, Solid JSX)

```tsx
import { Plugin } from "@opencode/plugin/tui"
import { createSignal, onCleanup } from "solid-js"   // or @opentui/solid
// (check docs; DECISIONS.md §J.4 flags the pragma is unconfirmed.)

interface Snap {
  gpu_util: number; vram_pct: number; gpu_temp: number
  cpu_load: number; t: string; ts: number
}

// Sparkline char map: index → ▁▂▃▄▅▆█ (scaled 0..7)
const CHARS = "▁▂▃▄▅▆█"

function sparkline(values: number[]): string {
  const n = Math.max(1, values.length)
  return values.slice(-30).map(v =>
    CHARS[Math.round(Math.min(7, v * 7 / 100))]
  ).join("")
}

export default Plugin.define({
  id: "little-mester.gpu-chart",

  setup(context) {
    const [history, setHistory] = createSignal<Snap[]>([])
    const [error, setError] = createSignal<string | null>(null)
    let intervalId: ReturnType<typeof setInterval>

    async function poll() {
      try {
        // On first poll get history so the chart isn't blank. After that,
        // just push new data from /metrics into the tail of history and keep it trimmed to 120.
        const isInitial = history().length === 0
        if (isInitial) {
          const res = await fetch("http://metrics:19532/history?n=120")
          const data: Snap[] = await res.json()
          setHistory(data)
        } else {
          const res = await fetch("http://metrics:19532/metrics")
          const data = await res.json()
          const g = (data.gpu || [{}])[0]
          const vramPct = Math.round(
            100 * (g.memory_used_mb ?? 0) / max(g.memory_total_mb ?? 1, 1)
          )
          push(history(), {
            t: data.timestamp ?? new Date().toISOString(),
            ts: Date.now() / 1000,
            gpu_util: g.utilization_percent ?? 0,
            vram_pct: vramPct,
            gpu_temp: g.temperature_c ?? 0,
            cpu_load: data.cpu?.load_avg_1m ?? 0,
          })
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err))
      }
    }

    // First poll immediately; then interval.
    poll()
    if (error()) { setError(null); setTimeout(poll, 5000) }
      else { intervalId = setInterval(poll, 500) }

    return () => clearInterval(intervalId)   // cleanup on slot unload
  },

  render(context) {
    const s = history()
    const err = error()
    if (err) return <text fg="yellow">GPU n/a: {err}</text>
    if (!s.length) return <text>Loading…</text>

    const last = s[s.length - 1]
    const gpuBar = sparkline(s.map(x => x.gpu_util))
    return (
      <box flexDirection="row" gap="2">
        {/* NOTE: this sketch is illustrative only — see "Bug found during
            implementation" below. Inline fragments in a <text> must be
            <span>/<strong>, never a nested <text>, and there is no <bold>
            element (inline catalogue: span, strong, b, em, i, u, a, br). */}
        <text><span style={{ fg: "#aaa" }}>GPU </span>{gpuBar} <strong>{last.gpu_util}%</strong></text>
        <text>VRAM {(last.vram_pct * 80 / 100 | 0)}/8.0 GB</text> {/* simplified */}
        <text>{last.gpu_temp}°C</text>
      </box>
    )
  },
})
```

### Phase 4 — TUI plugin: expandable detail view

Same plugin, toggle mode when the user clicks/hovers on the compact line.

Expanded view on mount:
```
GPU: ████████░░   67% ── VRAM: █████░░░░░   12.1 / 8.0 GB (151%)
Temp: 58°C   Load: 2.8/2.6/2.6

    GPU     ▁▃▅█▇▅▆▂▃▅▄▇▆▃▁▃▅▅▆▇    67
    VRAM   ░▁▃▃▄▄▄▄▅▅▆░▃▃▃▃▃▃▂▁      151%

RAM: 11.0/61.9 GB (17.7%)   CPU: AMD Ryzen AI 9 HX 370, 24 cores
```

Implementation detail — the toggle is simply a local state (boolean signal). The expanded view fetches `/history?n=120` on first open and then continues polling at 500 ms via the same interval. It renders two sparklines: one for GPU util per sample point, one for VRAM %. Uses `▁▂▃▄▅▆█` for rising bars (GPU util %) and `░░░░░░░░` as inactive fill (VRAM % — to contrast visually).

### Phase 5 — Wire in OpenCode config

**File: `agent/opencode/opencode.json`** — add `host-metrics-tui` to the `plugins` array.
If using `scripts/regen-configs`, update `_gen_opencode_json()` / equivalent generator there.

### Bug found during implementation — the plugin crashed the slot

The first implementation nested `<text>` elements inside `<text>` (e.g.
`<text>GPU {bar} <bold>{pct}%</bold></text>`). That does **not** fail at load time — it
fails while the slot renders:

```
little-mester.host-metrics-tui crashed in slot sidebar.footer:
TextNodeRenderable only accepts strings, TextNodeRenderable instances, or StyledText instances
```

The sidebar footer was simply empty; no metrics at all. Fix and the rules that caused it
are documented in `docs/DECISIONS.md` §J.9 and in the plugin README:

- inline fragments inside a `<text>` must be `<span>` / `<strong>` (never a nested `<text>`),
- a span's colour goes through `style={{ fg: "…" }}` (a bare `fg=` is ignored),
- there is no `<bold>` element — bold is `<strong>`.

To keep this from coming back, `scripts/check-tui-jsx.py` checks the JSX statically and
`tests/tui-jsx.bats` runs it over every TUI plugin.

**Verified:** headless render in a real `@opentui/solid` renderer (old file → exact error,
new file → both views render) and live in the real TUI in a pty (`<leader>b` toggles the
sidebar): the old plugin crashes the slot, the new plugin shows
`GPU 98% ▁▃██████████████` / `VRAM 7.2/8.0G 58°C`.

---

## Acceptance Criteria

### A1: Sparkline visible and non-blank after startup
Start `./lab <workspace>`, wait 5 s. The `sidebar.footer` (or whichever slot) shows a non-empty line containing "GPU" followed by at least three sparkline bars with a percentage.

### A1b: The slot does not crash
No `crashed in slot` message in the TUI, and `scripts/check-tui-jsx.py` reports no problems
for `agent/opencode/plugins/*/tui.tsx`.

### A2: Updates live at ~2 Hz
After confirming the initial value, wait 2 seconds from the host sidecar. Toggle between two load states (`stress` vs `idle`) while watching — the sparkline shape changes within 1 second of actual hardware change (the 0.5 s poll cadence).

### A3: Expand → show full detail
Clicking/toggling renders an expanded view with: a visible time-series chart, current VRAM in GB, GPU temperature, CPU load averages, and RAM usage. All values come from the sidecar (no hard-coded numbers).

### A4: Fails gracefully if sidecar is down
Stop `docker compose stop metrics`. The TUI shows "GPU n/a" or equivalent — no crash, no infinite error loop. Resuming the sidecar restores data without restarting opencode.

### A5: No new host-exposed ports
`docker port` on any service still returns nothing (no hosts ports). The `/history` endpoint stays behind Docker DNS only.

---

## Dependencies

| Dependency | Status | Notes |
|------------|--------|-------|
| NVML + `utility` GPU capability on metrics container | Done (ticket-010 legacy) | Already in compose.yaml |
| OpenCode v2 TUI plugin API | Confirmed working | Logo plugin loads and renders; DECISIONS.md §J.4 has unverified checklist |
| Solid / `@opentui/solid` reactivity signals | **Confirmed** | `createSignal` + reading `props.state()` inside the JSX expressions re-renders the slot on every poll — verified live (§J.9) |

---

## Files

| File | Role | New / Modified |
|------|------|----------------|
| `agent/metrics/gather.py` | Ring buffer + `/history` endpoint | **Modify** |
| `agent/opencode/plugins/host-metrics-tui/package.json` | Plugin package manifest | **Create** |
| `agent/opencode/plugins/host-metrics-tui/tui.tsx` | Compact sparkline + expandable detail TUI | **Create** |
| `agent/opencode/plugins/host-metrics-tui/README.md` | Plugin docs incl. the OpenTUI text rules | **Create** |
| `agent/opencode/opencode.json` | Register plugin | **Modify** |
| `scripts/regen-configs` | Emit new plugin path in generated config | **Modify** |
| `scripts/check-tui-jsx.py` | Static guard against the JSX mistakes that crash a slot | **Create** |
| `tests/tui-jsx.bats` | Regression tests for that guard | **Create** |

---

## Open Questions / Future Work

1. **Temperature sparklines** — Could chart CPU temps if we fix the hwmon symlink issue (DECISIONS.md §J.7). GPU temp stays constant-ish on a gaming/workstation so it's low priority for a sparkline.
2. **Theme-aware colors** — Use `context.theme` tokens for bar/fill foregrounds instead of hardcoded chars or text color — matches the logo plugin approach (§J.3 in DECISIONS.md).
3. **Performance impact** — 500 ms polling every cycle while opencode is alive is roughly 2 HTTP round-trips/sec to a local Unix-sidecar. Negligible for both agent and sidecar.
