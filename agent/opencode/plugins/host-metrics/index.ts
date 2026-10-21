// host-metrics plugin (OpenCode v2): registers the `get_host_metrics` tool.
//
// v2 contract: default-export Plugin.define({ id, setup(context) }).
// Custom tools are registered through the plugin API -- see
// https://opencode.ai/v2/docs/build/plugins/ (ctx.tool.transform -> editor.add).
// A markdown file in a `tools/` directory is NOT a tool in v2; that was v1.
//
// The tool queries the metrics sidecar over the compose `internal` network
// (Docker DNS name `metrics`). No host port is published for that service, so
// the endpoint is only reachable from inside the compose network.

import { Plugin } from "@opencode/plugin"

const DEFAULT_TIMEOUT_MS = 5000

// Candidate sidecar endpoints, in priority order.
//  - `metrics`      -> the compose service (Docker DNS) on the shared internal network
//  - 127.0.0.1     -> sidecar running in the same network namespace (e.g. `docker exec`)
// Override with METRICS_URL when the sidecar lives somewhere else.
function endpoints() {
  const list: string[] = []
  if (process.env.METRICS_URL) list.push(process.env.METRICS_URL.replace(/\/+$/, ""))
  list.push("http://metrics:19532", "http://127.0.0.1:19532")
  return [...new Set(list)]
}

type Metrics = {
  timestamp?: string
  cpu?: {
    model?: string
    load_avg_1m?: number
    load_avg_5m?: number
    load_avg_15m?: number
    num_cpus?: number
  }
  memory?: { total_gb?: number; used_gb?: number; available_gb?: number; percent?: number }
  gpu?: Array<{
    index?: number
    name?: string
    temperature_c?: number
    utilization_percent?: number
    memory_total_mb?: number
    memory_used_mb?: number
    memory_free_mb?: number
  }>
  temperatures?: Array<{ chip?: string; sensor?: string; temperature_c?: number }>
}

async function fetchMetrics(signal?: AbortSignal) {
  const tried: string[] = []
  for (const base of endpoints()) {
    const url = `${base}/metrics`
    tried.push(url)
    try {
      const res = await fetch(url, {
        signal,
        headers: { accept: "application/json" },
      })
      if (!res.ok) continue
      return { base, data: (await res.json()) as Metrics }
    } catch {
      // try the next candidate
    }
  }
  throw new Error(`metrics sidecar unreachable (tried: ${tried.join(", ")})`)
}

function formatReport(data: Metrics, base: string): string {
  const lines: string[] = []
  const cpu = data.cpu ?? {}
  const mem = data.memory ?? {}

  lines.push(`Host metrics @ ${data.timestamp ?? "unknown time"}  (source: ${base})`)
  lines.push("")

  const model = cpu.model && cpu.model !== "unknown" ? cpu.model : "CPU"
  lines.push(
    `CPU   ${model} -- ${cpu.num_cpus ?? "?"} cores | load ${cpu.load_avg_1m ?? "?"} / ${cpu.load_avg_5m ?? "?"} / ${cpu.load_avg_15m ?? "?"} (1m/5m/15m)`,
  )
  lines.push(
    `RAM   ${mem.used_gb ?? "?"} / ${mem.total_gb ?? "?"} GB used (${mem.percent ?? "?"}%) -- ${mem.available_gb ?? "?"} GB available`,
  )

  const gpus = data.gpu ?? []
  if (gpus.length === 0) {
    lines.push("GPU   no GPU data reported")
  } else {
    for (const g of gpus) {
      const usedGb = ((g.memory_used_mb ?? 0) / 1024).toFixed(1)
      const totalGb = ((g.memory_total_mb ?? 0) / 1024).toFixed(1)
      lines.push(
        `GPU${g.index ?? 0}  ${g.name ?? "unknown"} -- util ${g.utilization_percent ?? "?"}% | VRAM ${usedGb}/${totalGb} GB | ${g.temperature_c ?? "?"} C`,
      )
    }
  }

  const temps = data.temperatures ?? []
  if (temps.length === 0) {
    lines.push("TEMP  no temperature sensors reported")
  } else {
    const rendered = temps
      .map((t) => `${t.chip ?? "?"}/${t.sensor ?? "?"} ${t.temperature_c ?? "?"}C`)
      .join(" | ")
    lines.push(`TEMP  ${rendered}`)
  }

  return lines.join("\n")
}

export default Plugin.define({
  id: "little-mester.host-metrics",

  async setup(ctx) {
    await ctx.tool.transform((editor) => {
      editor.add({
        name: "get_host_metrics",
        description:
          "Get live host hardware metrics from the metrics sidecar: CPU model/load averages/core count, RAM used/available, GPU utilisation/VRAM/temperature, and temperature sensors. Use before proposing large model operations that need VRAM, or when the user asks about host hardware status.",
        input: {
          type: "object",
          properties: {
            detail: {
              type: "string",
              enum: ["summary", "full"],
              description:
                "'summary' (default) returns a formatted report; 'full' returns the raw sidecar JSON.",
            },
          },
          additionalProperties: false,
        },
        execute: async (input: { detail?: string }, context: { signal?: AbortSignal }) => {
          try {
            const { base, data } = await fetchMetrics(context?.signal)
            if (input?.detail === "full") {
              return { content: JSON.stringify(data, null, 2) }
            }
            return { content: formatReport(data, base) }
          } catch (err) {
            const message = err instanceof Error ? err.message : String(err)
            return {
              content:
                `Could not read host metrics: ${message}\n` +
                `The metrics sidecar may be down. Check: \`docker compose ps metrics\` and \`docker compose logs metrics\`.`,
            }
          }
        },
      })
    })
  },
})
