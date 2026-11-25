/** @jsxImportSource @opentui/solid */
// host-metrics-tui plugin — always-visible GPU/CPU/RAM sparkline in the OpenCode v2 TUI.
//
// Compact mode: two short lines in sidebar.footer — GPU util % + sparkline, VRAM + temp.
// Expanded mode: one row per metric with sparkline, current value and min-max range,
//                 plus CPU/RAM system stats.
//
// Slot: append: "sidebar.footer"
// Polls the fixed loopback-forwarded host /metrics endpoint every 500ms.
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
import { fetchMetricsJson, metricsBaseForMode } from "./metrics-endpoint.js"
import {
  applyMetricsPollResult,
  createInitialMetricsState,
  displayMetric,
  formatTemperatureSensors,
  markMetricsPollUnavailable,
  refreshMetricsFreshness,
  seedMetricsHistory,
  sparkline,
  sparkRange,
} from "./metrics-state.js"
import type { HistorySample, MetricsState } from "./metrics-state.js"

/* ------------------------------------------------------------------ */
/* Display helpers — absent readings remain null until explicitly rendered. */
/* ------------------------------------------------------------------ */

function mbToGb(valMib: number | null | undefined): string {
  if (valMib == null || valMib < 0) return "?"
  return (valMib / 1024).toFixed(1)
}

function lastValue(values: Array<number | null>): number | null {
  for (let index = values.length - 1; index >= 0; index -= 1) {
    if (values[index] !== null) return values[index]
  }
  return null
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

type State = MetricsState

/* ------------------------------------------------------------------ */
/* Display helpers (all return strings for <text> children)             */
/* ------------------------------------------------------------------ */

function historySeries(s: State, field: keyof HistorySample): Array<number | null> {
  return s.history.map(sample => {
    const value = sample[field]
    return typeof value === "number" ? value : null
  })
}

function vramPercent(s: State): number | null {
  if (s.gpu && s.gpu.memory_total_mb > 0) {
    return Math.round(100 * s.gpu.memory_used_mb / s.gpu.memory_total_mb)
  }
  const historical = lastValue(historySeries(s, "vram_pct"))
  return historical === null ? null : Math.round(historical)
}

function tempText(s: State): string {
  return displayMetric(s.gpu?.temperature_c, s.availability.gpus,
    value => `${Math.round(Number(value))}°C`)
}

function gpuText(s: State): string {
  return displayMetric(s.gpu?.utilization_percent, s.availability.gpus,
    value => `${Math.round(Number(value))}%`)
}

/** "6.0/7.0G" — compact VRAM readout. */
function vramCompact(s: State): string {
  if (s.availability.gpus !== "available") return displayMetric(null, s.availability.gpus)
  if (!s.gpu || s.gpu.memory_total_mb <= 0) return "—"
  return `${mbToGb(s.gpu.memory_used_mb)}/${mbToGb(s.gpu.memory_total_mb)}G`
}

/** "6.0/7.0 GB (85%)" — expanded VRAM readout. */
function vramDetail(s: State): string {
  if (s.availability.gpus !== "available") return displayMetric(null, s.availability.gpus)
  if (!s.gpu || s.gpu.memory_total_mb <= 0) return "—"
  const percent = displayMetric(vramPercent(s), "available", value => `${Math.round(Number(value))}%`)
  return `${mbToGb(s.gpu.memory_used_mb)}/${mbToGb(s.gpu.memory_total_mb)} GB (${percent})`
}

function gpuLabel(s: State): string {
  if (!s.gpu?.name) return ""
  const short = s.gpu.name.length > 22 ? `${s.gpu.name.slice(0, 21)}…` : s.gpu.name
  return short
}

function cpuText(s: State): string {
  if (!s.cpu) return displayMetric(null, s.availability.cpu)
  const model = s.cpu.model && s.cpu.model !== "unknown" ? s.cpu.model : "CPU"
  return `${model} · ${s.cpu.num_cpus} cores · load ${s.cpu.load_avg_1m.toFixed(2)}`
    + `/${s.cpu.load_avg_5m.toFixed(2)}/${s.cpu.load_avg_15m.toFixed(2)}`
}

function ramText(s: State): string {
  if (!s.memory) return displayMetric(null, s.availability.memory)
  return `${s.memory.used_gb.toFixed(1)} / ${s.memory.total_gb.toFixed(1)} GB`
    + ` (${s.memory.percent.toFixed(0)}%)`
}

function temperaturesText(s: State): string {
  return formatTemperatureSensors(s.temperatures, s.availability.temperatures)
}

function sourceStatus(s: State): string {
  return s.stale ? "Host metrics · stale" : s.sourceStatus
}

/* ------------------------------------------------------------------ */
/* Render components                                                    */
/*                                                                     */
/* NOTE: props.state() is read inside the JSX expressions (never hoisted */
/* into a local) so that Solid tracks the signal and re-renders on poll. */
/* ------------------------------------------------------------------ */

const SPARK_WIDTH = 16

function MetricsView(props: { state: () => State; theme: Theme; onToggle: () => void }) {
  return (
    <box flexDirection="column" gap={0} onMouseUp={props.onToggle}>
      <Show when={props.state().mode === "expanded"} fallback={<CompactView state={props.state} theme={props.theme} />}>
        <ExpandedView state={props.state} theme={props.theme} />
      </Show>
    </box>
  )
}

function CompactView(props: { state: () => State; theme: Theme }) {
  return (
    <box flexDirection="column" gap={0}>
      <text fg={props.theme.muted}>{sourceStatus(props.state())}</text>
      <box flexDirection="row" gap={1}>
        <text>
          <span style={{ fg: props.theme.muted }}>GPU </span>
          {gpuText(props.state())}
        </text>
        <text fg={props.theme.gpu}>{sparkline(historySeries(props.state(), "gpu_util"), SPARK_WIDTH)}</text>
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
        <text fg={props.theme.muted}>{sourceStatus(props.state())}</text>
      </box>
      <text fg={props.theme.muted}>{gpuLabel(props.state())}</text>

      {/* GPU */}
      <box flexDirection="row" gap={1}>
        <text fg={props.theme.muted}>GPU</text>
        <text fg={props.theme.gpu}>{sparkline(historySeries(props.state(), "gpu_util"), SPARK_WIDTH)}</text>
        <text>{gpuText(props.state())}</text>
        <text fg={props.theme.gpu}>{sparkRange(historySeries(props.state(), "gpu_util"))}</text>
      </box>

      {/* VRAM */}
      <box flexDirection="row" gap={1}>
        <text fg={props.theme.muted}>VRM</text>
        <text fg={props.theme.vram}>{sparkline(historySeries(props.state(), "vram_pct"), SPARK_WIDTH)}</text>
        <text>{vramDetail(props.state())}</text>
        <text fg={props.theme.vram}>{sparkRange(historySeries(props.state(), "vram_pct"))}</text>
      </box>

      {/* Temperature */}
      <box flexDirection="row" gap={1}>
        <text fg={props.theme.muted}>Tmp</text>
        <text fg={props.theme.temp}>{sparkline(historySeries(props.state(), "gpu_temp"), SPARK_WIDTH)}</text>
        <text>{tempText(props.state())}</text>
      </box>

      {/* Optional host temperature sensors have their own availability state. */}
      <text fg={props.theme.muted}>Sensors: {temperaturesText(props.state())}</text>

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
    const [state, setState] = createSignal<State>(createInitialMetricsState())
    const theme = resolveTheme(context.theme)
    let pollingId: ReturnType<typeof setInterval> | null = null
    let isDisposed = false

    function toggle() {
      setState(prev => ({ ...prev, mode: prev.mode === "compact" ? "expanded" : "compact" }))
    }

    const configuredMode = process.env.METRICS_MODE
    const metricsMode = configuredMode === "docker" || configuredMode === "vm"
      ? configuredMode
      : null
    const metricsBase = metricsBaseForMode(configuredMode)

    /* Poll /history (first time) → seeds the data arrays. */
    function seedHistory() {
      if (!metricsBase || !metricsMode) return Promise.resolve()
      return fetchMetricsJson<unknown>(metricsMode, "/history?n=60")
        .then((data: unknown) => {
          if (isDisposed) return
          setState(prev => seedMetricsHistory(prev, data, metricsMode))
        })
        .catch(() => {})
    }

    /* Poll /metrics → push latest data point. */
    function pollMetrics() {
      const receivedAt = globalThis.performance.now()
      setState(prev => refreshMetricsFreshness(prev, receivedAt))
      if (!metricsBase || !metricsMode) {
        setState(prev => ({
          ...prev,
          sourceStatus: "Host metrics unavailable · mode not configured",
        }))
        return
      }
      const pollGeneration = state().pollGeneration
      fetchMetricsJson<unknown>(metricsMode, "/metrics")
        .then((data: unknown) => {
          if (isDisposed) return
          setState(prev => applyMetricsPollResult(
            prev, data, globalThis.performance.now(), metricsMode, pollGeneration,
          ))
        })
        .catch(() => {
          if (!isDisposed) {
            setState(prev => markMetricsPollUnavailable(
              prev, globalThis.performance.now(), metricsMode, pollGeneration,
            ))
          }
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
