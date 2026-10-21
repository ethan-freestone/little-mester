# tools/get_host_metrics — Host Hardware Metrics Tool

## Description

Returns host-level hardware statistics (CPU model/load, RAM usage, GPU utilisation
and VRAM, temperature sensors) collected by the metrics sidecar.

The sidecar runs as a separate compose container that mounts the host's `/proc` and
`/sys` trees read-only. The agent container has no direct access to those paths, so
the sidecar is the only way to read them — it publishes the result over the compose
`internal` network at `http://metrics:19532`, which is reachable from the agent
container via Docker DNS but not from the host (no port is published).

Use it to decide whether a large model will fit in VRAM, to diagnose memory
pressure, or to answer "what is the host doing right now?".

## Registration

This is an OpenCode **v2** plugin, not a markdown tool file. Custom tools in v2 are
registered through the plugin API:

```ts
// agent/opencode/plugins/host-metrics/index.ts
await ctx.tool.transform((editor) => {
  editor.add({
    name: "get_host_metrics",
    description: "...",
    input: { type: "object", properties: { detail: { type: "string", enum: ["summary", "full"] } } },
    execute: async (input, context) => ({ content: "..." }),
  })
})
```

The plugin is baked into the agent image at `/etc/opencode/plugins/host-metrics/` and
listed in the generated `opencode.json` (`plugins` array) by `scripts/regen-configs`.

> A markdown file in a `tools/` directory does **not** register a tool in v2 — that was
> the v1 mechanism. See [docs/DECISIONS.md](../../../docs/DECISIONS.md) section J.1.

## Arguments

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `detail` | `"summary" \| "full"` | `"summary"` | `"summary"` returns a formatted report; `"full"` returns the raw sidecar JSON. |

## Output

`detail: "summary"` returns a plain-text report:

```
Host metrics @ 2026-10-21T12:19:43Z  (source: http://metrics:19532)

CPU   AMD Ryzen AI 9 HX 370 w/ Radeon 890M -- 24 cores | load 0.42 / 0.51 / 1.49 (1m/5m/15m)
RAM   11.8 / 61.9 GB used (19.1%) -- 50.1 GB available
GPU   no GPU data reported
TEMP  acpitz/1 46C | nvme/1 25.9C | amdgpu/1 43C | k10temp/1 46.4C
```

`detail: "full"` returns the raw JSON documented in
[`agent/metrics/TOOLS.md`](../../metrics/TOOLS.md).

## When to Use

- Before proposing large model operations that depend on available VRAM
- When the user asks about host hardware status ("what's my GPU doing?")
- When diagnosing unexpected behaviour (e.g. the model fell back to CPU — is RAM
  pressure a cause?)

## Notes

- Available whenever the stack is started with the metrics sidecar (the default).
- The sidecar caches values for 2 seconds, so repeated calls are free.
- Endpoint resolution order: `METRICS_URL` env var → `http://metrics:19532` →
  `http://127.0.0.1:19532`. The last one covers the case where the sidecar shares a
  network namespace with the agent (e.g. `docker exec` into a single container).
- If no endpoint answers, the tool returns a diagnostic message naming every URL it
  tried instead of throwing, so the failure is visible in the transcript.
