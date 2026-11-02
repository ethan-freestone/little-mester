/** @jsxImportSource @opentui/solid */
// host-metrics-tui plugin — always-visible GPU/CPU/RAM sparkline in the OpenCode v2 TUI.
//
// Compact mode: two short lines in sidebar.footer — GPU util % + sparkline, VRAM + temp.
// Expanded mode: one row per metric with sparkline, current value and min-max range,
//                 plus CPU/RAM system stats.
//
// Slot: append: "sidebar.footer"
// Polls /metrics every 500ms; uses /history on first poll to seed history.
//
// ── OpenTUI text rules (verified against @opentui/core 0.6.x, see docs/DECISIONS.md §J.9) ──
// 1. A <text> element is a TextRenderable. Its children may only be strings,
//    TextNodeRenderables or StyledText. Nesting a <text> inside a <text> is NOT
//    allowed: the slot dies with
//      "TextNodeRenderable only accepts strings, TextNodeRenderable instances,
//       or StyledText instances"
//    Inline fragments inside a <text> must use <span>, which IS a TextNodeRenderable.
// 2. A <span> takes its colour from style={{ fg: "..." }}; a bare fg prop on a span
//    is ignored (only <text> elements understand a bare fg prop).
// 3. Bold is <strong> (or <b>). style={{ fontWeight: "bold" }} is silently ignored —
//    the style object understands bold/italic/underline/dim, not fontWeight.

import { Plugin } from "@opencode/plugin/tui"
import { createSignal, Show } from "solid-js"

/* ------------------------------------------------------------------ */
/* Sparkline helpers — ASCII chars ▁▂▃▄▅▆█, scaled to data range       */
/* ------------------------------------------------------------------ */

const CHARS = "▁▂▃▄▅▆█".split("") as const  // 0→7
type CharIdx = (typeof CHARS)[number]
const N_CHARS = CHARS.length

function scaleToChar(v: number, min: number, max: number): CharIdx {
  if (min === max) return CHARS[4]  // flat data → middle char
  const idx = Math.max(0, Math.min(N_CHARS - 1, Math.round((v - min) / (max - min) * (N_CHARS - 1))))
  return CHARS[idx] as CharIdx
}

/** Sparkline of the last `width` samples, scaled to the visible range. */
function sparkline(values: number[], width: number): string {
  if (!values.length) return ""
  const src = values.slice(-width)
  const mn = Math.min(...src), mx = Math.max(...src)
  return src.map(v => scaleToChar(v, mn, mx)).join("")
}

/** "min-max" of the samples — shown next to the sparkline in expanded mode. */
function sparkRange(values: number[]): string {
  if (!values.length) return ""
  const mn = Math.min(...values), mx = Math.max(...values)
  return `${mn}-${mx}`
}

function mbToGb(valMib: number | undefined): string {
  if (valMib == null || valMib < 0) return "?"
  return (valMib / 1024).toFixed(1)
}

function lastValue(values: number[]): number {
  return values.length ? values[values.length - 1] : 0
}

/* ------------------------------------------------------------------ */
/* Theme                                                                */
/* ------------------------------------------------------------------ */

interface Theme {
  muted: string
  gpu: string
  vram: string
  temp: string
}

function resolveTheme(theme: any): Theme {
  return {
    muted: theme?.text?.muted ?? "#8a8a8a",
    gpu: "#34a853",
    vram: "#5b93d0",
    temp: "#ea4c73",
  }
}

/* ------------------------------------------------------------------ */
/* Shared reactive state                                                */
/* ------------------------------------------------------------------ */

interface State {
  mode: "compact" | "expanded"
  metricsError: string
  history: number[]    // GPU util % values
  vramHistory: number[] // VRAM % values
  tempVals: number[]   // GPU temps (only positive readings)
  gpuUtil: number      // latest snapshot
  gpuName: string
  vramUsedMb: number
  vramTotalMb: number
  cpuModel: string
  cpuCores: number
  load1: number
  load5: number
  load15: number
  ramUsedGb: number
  ramTotalGb: number
  ramPct: number
}

function initialState(): State {
  return {
    mode: "expanded",
    metricsError: "",
    history: [],
    vramHistory: [],
    tempVals: [],
    gpuUtil: 0,
    gpuName: "?",
    vramUsedMb: 0,
    vramTotalMb: 0,
    cpuModel: "CPU",
    cpuCores: 0,
    load1: 0,
    load5: 0,
    load15: 0,
    ramUsedGb: 0,
    ramTotalGb: 0,
    ramPct: 0,
  }
}

interface MetricsPayload {
  cpu?: {
    model?: string
    load_avg_1m?: number
    load_avg_5m?: number
    load_avg_15m?: number
    num_cpus?: number
  }
  memory?: {
    total_gb?: number
    used_gb?: number
    percent?: number
  }
  gpu?: Array<{
    name?: string
    utilization_percent?: number
    memory_used_mb?: number
    memory_total_mb?: number
    temperature_c?: number
  }>
}

/* ------------------------------------------------------------------ */
/* Display helpers (all return strings for <text> children)             */
/* ------------------------------------------------------------------ */

function gpuPercent(s: State): number {
  return s.gpuUtil || lastValue(s.history)
}

function vramPercent(s: State): number {
  if (s.vramTotalMb > 0) return Math.round(100 * s.vramUsedMb / s.vramTotalMb)
  return Math.round(lastValue(s.vramHistory))
}

function tempText(s: State): string {
  const t = lastValue(s.tempVals)
  return `${t > 0 ? Math.round(t) : "?"}°C`
}

function gpuText(s: State): string {
  return `${Math.round(gpuPercent(s))}%`
}

/** "6.0/7.0G" — compact VRAM readout. */
function vramCompact(s: State): string {
  if (s.vramTotalMb <= 0) return "n/a"
  return `${mbToGb(s.vramUsedMb)}/${mbToGb(s.vramTotalMb)}G`
}

/** "6.0/7.0 GB (85%)" — expanded VRAM readout. */
function vramDetail(s: State): string {
  if (s.vramTotalMb <= 0) return "n/a"
  return `${mbToGb(s.vramUsedMb)}/${mbToGb(s.vramTotalMb)} GB (${vramPercent(s)}%)`
}

function gpuLabel(s: State): string {
  if (!s.gpuName || s.gpuName === "?") return ""
  const short = s.gpuName.length > 22 ? `${s.gpuName.slice(0, 21)}…` : s.gpuName
  return short
}

function cpuText(s: State): string {
  const model = s.cpuModel && s.cpuModel !== "unknown" ? s.cpuModel : "CPU"
  const cores = s.cpuCores > 0 ? ` · ${s.cpuCores} cores` : ""
  return `${model}${cores} · load ${s.load1.toFixed(2)}/${s.load5.toFixed(2)}/${s.load15.toFixed(2)}`
}

function ramText(s: State): string {
  if (s.ramTotalGb <= 0) return "? / ? GB"
  return `${s.ramUsedGb.toFixed(1)} / ${s.ramTotalGb.toFixed(1)} GB (${s.ramPct.toFixed(0)}%)`
}

/* ------------------------------------------------------------------ */
/* Render components                                                    */
/*                                                                     */
/* NOTE: props.state() is read inside the JSX expressions (never hoisted */
/* into a local) so that Solid tracks the signal and re-renders on poll. */
/* ------------------------------------------------------------------ */

const SPARK_WIDTH = 16

function metricsBase(): string {
  return (process.env.METRICS_URL || "http://metrics:19532").replace(/\/+$/, "")
}

const METRICS_REQUEST_TIMEOUT_MS = 3000

function fetchMetricsJson<T>(path: string): Promise<T> {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), METRICS_REQUEST_TIMEOUT_MS)
  return fetch(`${metricsBase()}${path}`, { signal: controller.signal })
    .then(response => {
      if (!response.ok) throw new Error(`metrics endpoint returned HTTP ${response.status}`)
      return response.json() as Promise<T>
    })
    .finally(() => clearTimeout(timeout))
}

function MetricsView(props: { state: () => State; theme: Theme; onToggle: () => void }) {
  return (
    <box flexDirection="column" gap={0} onMouseUp={props.onToggle}>
      <Show when={props.state().metricsError}>
        <text fg="#ea4c73">{props.state().metricsError}</text>
      </Show>
      <Show when={props.state().mode === "expanded"} fallback={<CompactView state={props.state} theme={props.theme} />}>
        <ExpandedView state={props.state} theme={props.theme} />
      </Show>
    </box>
  )
}

function CompactView(props: { state: () => State; theme: Theme }) {
  return (
    <box flexDirection="column" gap={0}>
      <box flexDirection="row" gap={1}>
        <text>
          <span style={{ fg: props.theme.muted }}>GPU </span>
          {gpuText(props.state())}
        </text>
        <text fg={props.theme.gpu}>{sparkline(props.state().history, SPARK_WIDTH)}</text>
      </box>
      <box flexDirection="row" gap={1}>
        <text>
          <span style={{ fg: props.theme.muted }}>VRAM </span>
          {vramCompact(props.state())}
        </text>
        <text fg={props.theme.temp}>{tempText(props.state())}</text>
      </box>
    </box>
  )
}

function ExpandedView(props: { state: () => State; theme: Theme }) {
  return (
    <box flexDirection="column" gap={0}>
      {/* Header */}
      <box flexDirection="row" gap={1}>
        <text fg={props.theme.muted}>
          <strong>▸ System Monitor</strong>
        </text>
        <text fg={props.theme.muted}>{`${props.state().history.length} samples`}</text>
      </box>
      <text fg={props.theme.muted}>{gpuLabel(props.state())}</text>

      {/* GPU */}
      <box flexDirection="row" gap={1}>
        <text fg={props.theme.muted}>GPU</text>
        <text fg={props.theme.gpu}>{sparkline(props.state().history, SPARK_WIDTH)}</text>
        <text>{gpuText(props.state())}</text>
        <text fg={props.theme.gpu}>{sparkRange(props.state().history)}</text>
      </box>

      {/* VRAM */}
      <box flexDirection="row" gap={1}>
        <text fg={props.theme.muted}>VRM</text>
        <text fg={props.theme.vram}>{sparkline(props.state().vramHistory, SPARK_WIDTH)}</text>
        <text>{vramDetail(props.state())}</text>
        <text fg={props.theme.vram}>{sparkRange(props.state().vramHistory)}</text>
      </box>

      {/* Temperature */}
      <box flexDirection="row" gap={1}>
        <text fg={props.theme.muted}>Tmp</text>
        <text fg={props.theme.temp}>{sparkline(props.state().tempVals, SPARK_WIDTH)}</text>
        <text>{tempText(props.state())}</text>
      </box>

      {/* System overview */}
      <text fg={props.theme.muted}>
        <strong>CPU:</strong> {cpuText(props.state())}
      </text>
      <text fg={props.theme.muted}>
        RAM: <strong>{ramText(props.state())}</strong>
      </text>
    </box>
  )
}

/* ------------------------------------------------------------------ */
/* TUI Plugin                                                           */
/* ------------------------------------------------------------------ */

export default Plugin.define({
  id: "little-mester.host-metrics-tui",

  async setup(context) {
    const [state, setState] = createSignal<State>(initialState())
    const theme = resolveTheme(context.theme)
    let pollingId: ReturnType<typeof setInterval> | null = null
    let isDisposed = false
    let metricsRequestInFlight = false

    function toggle() {
      setState(prev => ({ ...prev, mode: prev.mode === "compact" ? "expanded" : "compact" }))
    }

    /* Poll /history (first time) → seeds the data arrays. */
    function seedHistory() {
      return fetchMetricsJson<Array<{ gpu_util?: number; vram_pct?: number; gpu_temp?: number }>>("/history?n=60")
        .then((data: Array<{ gpu_util?: number; vram_pct?: number; gpu_temp?: number }>) => {
          if (isDisposed || !Array.isArray(data) || !data.length) return
          setState(prev => ({
            ...prev,
            history: data.map(s => s.gpu_util ?? 0).slice(-240),
            vramHistory: data.map(s => Math.round(s.vram_pct ?? 0)).slice(-240),
            tempVals: data
              .filter(s => (s.gpu_temp ?? 0) > 0)
              .map(s => Math.round(s.gpu_temp as number))
              .slice(-20),
          }))
        })
      .catch(() => {
        if (!isDisposed) setState(prev => ({ ...prev, metricsError: "host metrics unavailable" }))
      })
    }

    /* Poll /metrics → push latest data point. */
    function pollMetrics() {
      if (metricsRequestInFlight) return
      metricsRequestInFlight = true
      fetchMetricsJson<MetricsPayload>("/metrics")
        .then((d: MetricsPayload) => {
          if (isDisposed) return
          setState(prev => ({ ...prev, metricsError: "" }))
          const g = d.gpu?.[0]
          setState(prev => {
            const history = [...prev.history]
            const vramHistory = [...prev.vramHistory]
            const tempVals = [...prev.tempVals]

            const util = g?.utilization_percent
            if (util != null) history.push(util)

            const usedMb = g?.memory_used_mb ?? prev.vramUsedMb
            const totalMb = g?.memory_total_mb ?? prev.vramTotalMb
            if (totalMb > 0) vramHistory.push(Math.round(100 * usedMb / totalMb))

            const temp = g?.temperature_c
            if (temp != null && temp > 0) {
              const rounded = Math.round(temp)
              if (!tempVals.length || tempVals[tempVals.length - 1] !== rounded) tempVals.push(rounded)
            }

            return {
              ...prev,
              gpuName: g?.name || prev.gpuName,
              gpuUtil: util ?? prev.gpuUtil,
              vramUsedMb: usedMb,
              vramTotalMb: totalMb,
              history: history.slice(-240),
              vramHistory: vramHistory.slice(-240),
              tempVals: tempVals.slice(-20),
              cpuModel: d.cpu?.model?.trim() || prev.cpuModel,
              cpuCores: d.cpu?.num_cpus ?? prev.cpuCores,
              load1: d.cpu?.load_avg_1m ?? prev.load1,
              load5: d.cpu?.load_avg_5m ?? prev.load5,
              load15: d.cpu?.load_avg_15m ?? prev.load15,
              ramUsedGb: d.memory?.used_gb ?? prev.ramUsedGb,
              ramTotalGb: d.memory?.total_gb ?? prev.ramTotalGb,
              ramPct: d.memory?.percent ?? prev.ramPct,
            }
          })
        })
        .catch(() => {
          if (!isDisposed) setState(prev => ({ ...prev, metricsError: "host metrics unavailable" }))
        })
        .finally(() => {
          metricsRequestInFlight = false
        })
    }

    // Start polling (500ms cadence)
    seedHistory()
    pollMetrics()            // immediate first read
    pollingId = setInterval(() => pollMetrics(), 500)

    // Palette command provides a keyboard route to the same toggle.
    context.keymap.layer(() => ({
      mode: "global",
      commands: [{
        id: "little-mester.host-metrics.toggle",
        title: "Toggle host metrics detail",
        group: "Little Mester",
        palette: true,
        run: toggle,
      }],
    }))

    const unregister = context.ui.slot({
      append: "sidebar.footer",
      render: () => <MetricsView state={state} theme={theme} onToggle={toggle} />,
    })

    return () => {
      isDisposed = true
      if (pollingId) clearInterval(pollingId)
      unregister()
    }
  },
})
