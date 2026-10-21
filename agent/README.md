# Dockerfile — why one for both agents

## Decision

Keep a **single** Dockerfile at `agent/Dockerfile` instead of splitting into
`agent/opencode/Dockerfile` and `agent/aider/Dockerfile`.

### Why one file

1. Both agents share the same base dependencies: git, curl, ripgrep, nodejs/npm,
   NVIDIA container toolkit compatibility (via host config).
2. The lab script already selects which binary to invoke per-run via `--agent`.
3. One image to build and maintain; fewer Docker contexts to understand.

### How splitting works today

The agents diverge only in their binary dependencies:

| Dep | opencode | aider |
|-----|----------|-------|
| Binary | `opencode` (npm) | `aider-chat` (pip) |
| Config | `opencode/opencode.json` → `/etc/opencode/opencode/opencode.json` | (none mounted yet) |
| Runtime choice | CMD `"opencode"` / env override | `CMD ["aider"]` |

lab picks the binary with this section:

```bash
case "$AGENT" in
  aider|opencode) ;;
  *) die "unknown agent: $AGENT (expected aider or opencode)" ;;
esac
...
dc run --rm agent "$AGENT" "$@"
```

### What would trigger a split

- When one agent starts needing different apt/npm/python system packages consistently.
- When per-agent image size becomes meaningful (e.g., >2x difference).
- When build times diverge enough that hot-swapping agents requires separate contexts.
- When security profiles diverge (different cap sets, tmpfs mounts, etc.).

### Config locations for v2 migration

| Agent | Old path | New path |
|-------|----------|----------|
| opencode | `agent/opencode.json` | `agent/opencode/opencode.json` |

The Dockerfile COPY and OPENCODE_CONFIG env var are updated to reflect this change.

---

## Metrics Sidecar (`agent/metrics/`)

A lightweight monitoring service runs as a compose container alongside `ollama` and the `agent`. It exposes live host-level GPU, CPU, RAM, and temperature data via an HTTP server on the compose `internal` network (`http://metrics:19532`) — so the agent can query hardware stats **from inside its own sandboxed container** without breaking any security boundaries.

### How it works

| Layer | Detail |
|-------|--------|
| **Collection** | Reads host `/proc` mounts (`loadavg`, `meminfo`, `cpuinfo`) and parses `nvidia-smi` CSV output. Temperatures come from `/sys/class/hwmon` (CPU/motherboard sensors) and `/sys/class/thermal` (GPU die junction). |
| **Serving** | Tiny Python HTTP server. Two endpoints: `/metrics` (machine-parsable JSON), `/summary` (one-line string). |
| **Caching** | 2-second TTL on all metric values so repeated polls from the agent/TUI are free (<1 ms response). |
| **Security** | Container drops ALL Linux capabilities, runs read-only filesystem, mounts only `/proc`, `/sys/class/hwmon`, `/sys/class/thermal` as `:ro`. No ports published to the host — reachable only via Docker DNS from inside compose. |

### Accessing metrics from the agent

The agent has a dedicated tool, `get_host_metrics`, registered by the
[`host-metrics` plugin](opencode/plugins/host-metrics/). One call returns a
formatted report:

```
Host metrics @ 2026-10-21T12:19:43Z  (source: http://metrics:19532)

CPU   AMD Ryzen AI 9 HX 370 w/ Radeon 890M -- 24 cores | load 0.42 / 0.51 / 1.49 (1m/5m/15m)
RAM   11.8 / 61.9 GB used (19.1%) -- 50.1 GB available
GPU   no GPU data reported
TEMP  acpitz/1 46C | nvme/1 25.9C | amdgpu/1 43C | k10temp/1 46.4C
```

Pass `detail: "full"` to get the raw sidecar JSON instead.

The same data is available over raw HTTP from inside the container:

```bash
curl -s http://metrics:19532/metrics
curl -s http://metrics:19532/summary
```

Full schema documentation: [`agent/metrics/TOOLS.md`](agent/metrics/TOOLS.md).

### Acceptance testing

Run on the host after starting up:

```bash
./scripts/verify-metrics.sh
```

This validates all 10 acceptance criteria from ticket-legacy-10 (service startup, JSON schema, accuracy vs. `nvidia-smi` / `free(1)`, no port exposure, etc.).

### TUI dashboard (optional future extension)

An OpenCode plugin that polls `/summary` every 3 seconds and renders a live hardware-gauge panel in the sidebar/window footer — keeping all host metrics visible without leaving the agent session. This is documented as an optional enhancement; current default behavior already surfaces key values in the bash tool output.
