import assert from "node:assert/strict"
import test from "node:test"

import { fetchMetricsJson, metricsBaseForMode } from "../agent/opencode/plugins/host-metrics-tui/metrics-endpoint.js"
import {
  applyMetricsPayload,
  createInitialMetricsState,
  displayMetric,
  formatTemperatureSensors,
  isValidDockerHistorySample,
  isValidDockerMetricsPayload,
  isValidHistorySample,
  isValidMetricsPayload,
  refreshMetricsFreshness,
  seedMetricsHistory,
  sparkline,
  sparkRange,
} from "../agent/opencode/plugins/host-metrics-tui/metrics-state.js"

test("only the fixed Docker and VM endpoints are selectable", () => {
  assert.equal(metricsBaseForMode("docker"), "http://metrics:19532")
  assert.equal(metricsBaseForMode("vm"), "http://10.0.2.100:19532")
  for (const mode of [undefined, null, "", "host", "constructor", "__proto__", "toString",
    "http://attacker.invalid"]) {
    assert.equal(metricsBaseForMode(mode), undefined)
  }
})

test("Docker and VM modes fetch both metrics resources from their fixed endpoint", async () => {
  for (const [mode, base] of [
    ["docker", "http://metrics:19532"],
    ["vm", "http://10.0.2.100:19532"],
  ]) {
    const urls = []
    const requestOptions = []
    const fetchImpl = async (url, options) => {
      urls.push(url)
      requestOptions.push(options)
      return { ok: true, status: 200, json: async () => ({ url }) }
    }

    assert.deepEqual(await fetchMetricsJson(mode, "/history?n=60", fetchImpl),
                     { url: `${base}/history?n=60` })
    assert.deepEqual(await fetchMetricsJson(mode, "/metrics", fetchImpl),
                     { url: `${base}/metrics` })
    assert.deepEqual(urls, [`${base}/history?n=60`, `${base}/metrics`])
    assert.deepEqual(requestOptions, [{ cache: "no-store" }, { cache: "no-store" }])
  }
})

test("missing mode, unsupported paths, HTTP errors, and fetch errors fail closed", async () => {
  let calls = 0
  const fetchImpl = async () => {
    calls += 1
    return { ok: false, status: 503 }
  }

  for (const mode of [undefined, "constructor", "__proto__", "toString"]) {
    await assert.rejects(fetchMetricsJson(mode, "/metrics", fetchImpl), /mode is not configured/)
  }
  await assert.rejects(fetchMetricsJson("docker", "http://attacker.invalid/metrics", fetchImpl),
                       /unsupported metrics request path/)
  assert.equal(calls, 0, "invalid configuration must not trigger a fetch")
  await assert.rejects(fetchMetricsJson("docker", "/metrics", fetchImpl), /HTTP 503/)
  await assert.rejects(fetchMetricsJson("vm", "/metrics", async () => {
    throw new Error("network unavailable")
  }), /network unavailable/)
  assert.equal(calls, 1)
})

const available = {
  cpu: "available",
  memory: "available",
  gpus: "available",
  temperatures: "no_sensors",
}

function metricsSample(overrides = {}) {
  return {
    source: "host",
    run_id: "a".repeat(32),
    sequence: 1,
    timestamp: "2026-11-20T00:00:00.000Z",
    cpu: {
      model: "Fixture CPU", load_avg_1m: 0, load_avg_5m: 0.1,
      load_avg_15m: 0.2, num_cpus: 4,
    },
    memory: { total_gb: 8, used_gb: 4, available_gb: 4, percent: 50 },
    gpu: [{
      index: 0, name: "Fixture GPU", temperature_c: 42,
      utilization_percent: 0, memory_total_mb: 8192,
      memory_used_mb: 0, memory_free_mb: 8192,
    }],
    availability: { ...available },
    temperatures: null,
    ...overrides,
  }
}

function historySample(overrides = {}) {
  return {
    run_id: "a".repeat(32),
    sequence: 1,
    timestamp: "2026-11-20T00:00:00.000Z",
    gpu_util: 0,
    vram_pct: 0,
    gpu_temp: 42,
    availability: { ...available },
    ...overrides,
  }
}

function vmHistoryRange(runId, first, last) {
  return Array.from({ length: last - first + 1 }, (_, index) => historySample({
    run_id: runId,
    sequence: first + index,
    gpu_util: Math.min(first + index, 100),
  }))
}

function dockerSample(overrides = {}) {
  return {
    timestamp: "2026-11-20T00:00:00Z",
    cpu: {
      model: "Fixture CPU", load_avg_1m: 0, load_avg_5m: 0,
      load_avg_15m: 0, num_cpus: 4,
    },
    memory: { total_gb: 8, used_gb: 0, available_gb: 8, percent: 0 },
    gpu: [{
      index: 0, name: "Fixture GPU", temperature_c: 0,
      utilization_percent: 0, memory_total_mb: 8192,
      memory_used_mb: 0, memory_free_mb: 8192,
    }],
    temperatures: [{ chip: "fixture", sensor: "Tctl", temperature_c: 0 }],
    ...overrides,
  }
}

function dockerHistorySample(overrides = {}) {
  return {
    t: "2026-11-20T00:00:00Z",
    ts: 10,
    gpu_util: 0,
    vram_pct: 0,
    gpu_temp: 0,
    cpu_load: 0,
    ...overrides,
  }
}

test("unavailable sensors render their reason and history keeps null gaps instead of zeroes", () => {
  const unavailable = metricsSample({
    gpu: [],
    availability: { ...available, gpus: "no_device" },
  })
  assert.equal(isValidMetricsPayload(unavailable), true)
  const state = applyMetricsPayload(createInitialMetricsState(), unavailable, 10, "vm")

  assert.equal(state.gpu, null)
  assert.equal(state.history[0].gpu_util, null)
  assert.equal(state.history[0].vram_pct, null)
  assert.equal(state.history[0].gpu_temp, null)
  assert.equal(displayMetric(state.gpu?.utilization_percent, state.availability.gpus), "— (no_device)")
  assert.equal(displayMetric(state.cpu?.load_avg_1m, state.availability.cpu, value => `${value}%`), "0%")
  assert.equal(displayMetric(null, "permission_denied"), "— (permission_denied)")
  assert.equal(displayMetric(null, null), "—")
})

test("identity advances once, cached samples do not append or refresh, and restarts reset sequence", () => {
  let state = applyMetricsPayload(createInitialMetricsState(), metricsSample(), 1000, "vm")
  assert.equal(state.sequence, 1)
  assert.equal(state.receivedAt, 1000)
  assert.equal(state.history.length, 1)

  state = applyMetricsPayload(state, metricsSample(), 2500, "vm")
  assert.equal(state.receivedAt, 1000, "same run/sequence must not refresh freshness")
  assert.equal(state.history.length, 1, "same run/sequence must not append a duplicate")
  assert.equal(state.stale, false)
  state = refreshMetricsFreshness(state, 3000)
  assert.equal(state.stale, true, "the stale threshold is inclusive at two seconds")

  state = applyMetricsPayload(state, metricsSample({ sequence: 2 }), 3001, "vm")
  assert.equal(state.sequence, 2)
  assert.equal(state.receivedAt, 3001)
  assert.equal(state.stale, false)
  assert.equal(state.history.length, 2)

  state = applyMetricsPayload(state, metricsSample({ sequence: 1 }), 3500, "vm")
  assert.equal(state.sequence, 2, "older sequence in one process is rejected")
  assert.equal(state.receivedAt, 3001)
  assert.equal(state.history.length, 2)

  state = applyMetricsPayload(state,
    metricsSample({ run_id: "b".repeat(32), sequence: 1 }), 3600, "vm")
  assert.equal(state.runId, "b".repeat(32))
  assert.equal(state.sequence, 1, "a new process run may restart its sequence")
  assert.equal(state.receivedAt, 3600)
  assert.equal(state.history.length, 3)
})

test("history seeding deduplicates samples and preserves zero readings and unavailable gaps", () => {
  const unavailable = historySample({
    sequence: 1, gpu_util: null, vram_pct: null, gpu_temp: null,
    availability: { ...available, gpus: "no_device" },
  })
  const zero = historySample({ sequence: 2 })
  let state = seedMetricsHistory(createInitialMetricsState(),
    [unavailable, unavailable, zero], "vm")

  assert.equal(state.history.length, 2)
  assert.deepEqual(state.history.map(sample => sample.gpu_util), [null, 0])
  assert.deepEqual(state.history.map(sample => sample.vram_pct), [null, 0])
  assert.equal(state.sequence, null, "history does not establish live identity")
  assert.equal(state.receivedAt, null, "history does not establish live freshness")
  assert.equal(state.sourceStatus, "Host metrics · waiting for sample")

  state = seedMetricsHistory(state, [unavailable, zero], "vm")
  assert.equal(state.history.length, 2)
  assert.equal(state.receivedAt, null, "cached history must not refresh freshness")
})

test("history arriving after a live poll fills older samples without duplicating the latest", () => {
  const second = historySample({ sequence: 2, gpu_util: 50 })
  const first = historySample({ sequence: 1, gpu_util: 25 })
  let state = applyMetricsPayload(createInitialMetricsState(),
    metricsSample({ sequence: 2 }), 100, "vm")
  state = seedMetricsHistory(state, [first, second], "vm")

  assert.deepEqual(state.history.map(sample => sample.sequence), [1, 2])
  assert.equal(state.receivedAt, 100, "history's cached latest sample is not a freshness advance")
})

test("older-than-history first live identity advances readings and freshness in chart order", () => {
  const runId = "a".repeat(32)
  let state = seedMetricsHistory(createInitialMetricsState(), vmHistoryRange(runId, 2, 61), "vm")
  const live = metricsSample({
    run_id: runId,
    sequence: 1,
    cpu: { ...metricsSample().cpu, model: "First live CPU" },
    gpu: [{ ...metricsSample().gpu[0], utilization_percent: 88 }],
  })
  state = applyMetricsPayload(state, live, 1234, "vm")

  assert.deepEqual(state.history.map(sample => sample.sequence),
    Array.from({ length: 61 }, (_, index) => index + 1))
  assert.equal(state.runId, runId)
  assert.equal(state.sequence, 1)
  assert.equal(state.cpu.model, "First live CPU")
  assert.equal(state.gpu.utilization_percent, 88)
  assert.equal(state.receivedAt, 1234)
  assert.equal(state.stale, false)
})

test("VM history seeded after live data is ordered without changing live freshness", () => {
  const runId = "a".repeat(32)
  let state = applyMetricsPayload(createInitialMetricsState(),
    metricsSample({ run_id: runId, sequence: 1 }), 200, "vm")
  state = refreshMetricsFreshness(state, 2200)
  state = seedMetricsHistory(state, vmHistoryRange(runId, 2, 61), "vm")

  assert.deepEqual(state.history.map(sample => sample.sequence),
    Array.from({ length: 61 }, (_, index) => index + 1))
  assert.equal(state.runId, runId)
  assert.equal(state.sequence, 1)
  assert.equal(state.receivedAt, 200)
  assert.equal(state.stale, true)
})

test("VM history keeps existing duplicate rows while an equal live identity updates readings", () => {
  const runId = "a".repeat(32)
  const seeded = historySample({ sequence: 2, gpu_util: 45 })
  let state = seedMetricsHistory(createInitialMetricsState(), [seeded], "vm")
  state = seedMetricsHistory(state, [historySample({ sequence: 2, gpu_util: 99 })], "vm")
  assert.equal(state.history[0].gpu_util, 45, "existing chart rows win duplicate history samples")

  const live = metricsSample({
    run_id: runId,
    sequence: 2,
    gpu: [{ ...metricsSample().gpu[0], utilization_percent: 70 }],
  })
  state = applyMetricsPayload(state, live, 420, "vm")
  assert.equal(state.sequence, 2)
  assert.equal(state.gpu.utilization_percent, 70, "accepted live data updates current readings")
  assert.equal(state.receivedAt, 420)
  assert.equal(state.history.length, 1)
  assert.equal(state.history[0].gpu_util, 45, "the existing seeded chart row wins")
})

test("same-run delayed live samples do not backfill or refresh and recompute staleness", () => {
  const runId = "a".repeat(32)
  const current = metricsSample({
    run_id: runId,
    sequence: 2,
    gpu: [{ ...metricsSample().gpu[0], utilization_percent: 62 }],
  })
  let state = applyMetricsPayload(createInitialMetricsState(), current, 1000, "vm")
  state = applyMetricsPayload(state, metricsSample({
    run_id: runId,
    sequence: 1,
    gpu: [{ ...metricsSample().gpu[0], utilization_percent: 12 }],
  }), 3000, "vm")

  assert.deepEqual(state.history.map(sample => sample.sequence), [2])
  assert.equal(state.runId, runId)
  assert.equal(state.sequence, 2)
  assert.equal(state.gpu.utilization_percent, 62)
  assert.equal(state.receivedAt, 1000)
  assert.equal(state.stale, true, "staleness is inclusive at the two-second boundary")
})

test("VM history keeps restart blocks ordered and ignores late history for another run", () => {
  const runA = "a".repeat(32)
  const runB = "b".repeat(32)
  let state = applyMetricsPayload(createInitialMetricsState(),
    metricsSample({ run_id: runA, sequence: 10 }), 1000, "vm")
  state = applyMetricsPayload(state,
    metricsSample({ run_id: runB, sequence: 1 }), 1100, "vm")
  state = seedMetricsHistory(state, vmHistoryRange(runB, 3, 4), "vm")
  state = seedMetricsHistory(state, vmHistoryRange(runB, 2, 2), "vm")

  assert.deepEqual(state.history.map(sample => [sample.run_id, sample.sequence]), [
    [runA, 10], [runB, 1], [runB, 2], [runB, 3], [runB, 4],
  ])
  const beforeLateHistory = state.history
  state = seedMetricsHistory(state, vmHistoryRange(runA, 9, 9), "vm")
  assert.deepEqual(state.history, beforeLateHistory, "other-run history cannot reorder or backfill")
  assert.equal(state.runId, runB)
  assert.equal(state.sequence, 1, "history cannot advance the live identity")
  assert.equal(state.receivedAt, 1100)
})

test("first live VM row obeys the history retention cap after ordered merging", () => {
  const runId = "a".repeat(32)
  let state = seedMetricsHistory(createInitialMetricsState(), vmHistoryRange(runId, 2, 241), "vm")
  state = applyMetricsPayload(state, metricsSample({ run_id: runId, sequence: 1 }), 500, "vm")

  assert.equal(state.history.length, 240)
  assert.deepEqual(state.history.map(sample => sample.sequence),
    Array.from({ length: 240 }, (_, index) => index + 2))
  assert.equal(state.runId, runId)
  assert.equal(state.sequence, 1)
  assert.equal(state.receivedAt, 500)
})

test("history is chart-only in either arrival order and cannot advance VM live state", () => {
  const first = historySample({ sequence: 1 })
  const second = historySample({ sequence: 2, gpu_util: 20 })
  const third = historySample({ sequence: 3, gpu_util: 30 })

  let historyFirst = seedMetricsHistory(createInitialMetricsState(), [first, second], "vm")
  assert.equal(historyFirst.runId, null)
  assert.equal(historyFirst.sequence, null)
  assert.equal(historyFirst.receivedAt, null)
  assert.equal(historyFirst.sourceStatus, "Host metrics · waiting for sample")
  historyFirst = applyMetricsPayload(historyFirst, metricsSample({ sequence: 2 }), 100, "vm")
  assert.equal(historyFirst.sequence, 2, "the live snapshot is accepted after history seeded it")
  assert.equal(historyFirst.receivedAt, 100)
  assert.equal(historyFirst.history.length, 2, "the live row is deduplicated against history")

  let liveFirst = applyMetricsPayload(createInitialMetricsState(),
    metricsSample({ sequence: 2 }), 200, "vm")
  liveFirst = refreshMetricsFreshness(liveFirst, 2200)
  const previousLive = {
    runId: liveFirst.runId,
    sequence: liveFirst.sequence,
    receivedAt: liveFirst.receivedAt,
    stale: liveFirst.stale,
    gpu: liveFirst.gpu,
    cpu: liveFirst.cpu,
    memory: liveFirst.memory,
    temperatures: liveFirst.temperatures,
    availability: liveFirst.availability,
    sourceStatus: liveFirst.sourceStatus,
  }
  liveFirst = seedMetricsHistory(liveFirst, [first, second, third], "vm")
  assert.deepEqual({
    runId: liveFirst.runId,
    sequence: liveFirst.sequence,
    receivedAt: liveFirst.receivedAt,
    stale: liveFirst.stale,
    gpu: liveFirst.gpu,
    cpu: liveFirst.cpu,
    memory: liveFirst.memory,
    temperatures: liveFirst.temperatures,
    availability: liveFirst.availability,
    sourceStatus: liveFirst.sourceStatus,
  }, previousLive)
  assert.deepEqual(liveFirst.history.map(sample => sample.sequence), [1, 2, 3])
  liveFirst = applyMetricsPayload(liveFirst, metricsSample({ sequence: 3 }), 2300, "vm")
  assert.equal(liveFirst.sequence, 3, "history did not consume the later live identity")
})

test("VM and Docker parsers are transport-specific with no fallback", () => {
  const docker = dockerSample()
  const dockerHistory = dockerHistorySample()
  assert.equal(isValidDockerMetricsPayload(docker), true)
  assert.equal(isValidMetricsPayload(docker), false)
  assert.equal(isValidDockerHistorySample(dockerHistory), true)
  assert.equal(isValidHistorySample(dockerHistory), false)
  assert.equal(isValidMetricsPayload(metricsSample()), true)
  assert.equal(isValidDockerMetricsPayload(metricsSample()), false)
  assert.equal(isValidHistorySample(historySample()), true)
  assert.equal(isValidDockerHistorySample(historySample()), false)

  const dockerInVmMode = applyMetricsPayload(createInitialMetricsState(), docker, 10, "vm")
  assert.equal(dockerInVmMode.runId, null)
  assert.equal(dockerInVmMode.receivedAt, null)
  assert.equal(dockerInVmMode.sourceStatus, "Host metrics · invalid response")
  const vmInDockerMode = applyMetricsPayload(createInitialMetricsState(), metricsSample(), 10, "docker")
  assert.equal(vmInDockerMode.runId, null)
  assert.equal(vmInDockerMode.receivedAt, null)
  assert.equal(vmInDockerMode.sourceStatus, "Host metrics · invalid response")
})

test("Docker snapshots preserve zeroes and only newer second-precision timestamps refresh", () => {
  let state = applyMetricsPayload(createInitialMetricsState(), dockerSample(), 100, "docker")
  assert.equal(state.transport, "docker")
  assert.equal(state.runId, null, "legacy Docker data has no VM run identity")
  assert.equal(state.sequence, null, "legacy Docker data has no VM sequence")
  assert.equal(state.legacyTimestamp, "2026-11-20T00:00:00Z")
  assert.equal(state.gpu.utilization_percent, 0)
  assert.equal(state.gpu.temperature_c, 0)
  assert.equal(state.memory.used_gb, 0)
  assert.equal(state.cpu.load_avg_1m, 0)
  assert.equal(state.temperatures[0].temperature_c, 0)
  assert.equal(state.availability.gpus, "available")

  state = applyMetricsPayload(state, dockerSample({
    gpu: [{ ...dockerSample().gpu[0], utilization_percent: 70 }],
  }), 500, "docker")
  assert.equal(state.receivedAt, 100, "same-second snapshots cannot prove a new sample")
  assert.equal(state.gpu.utilization_percent, 0, "ambiguous same-second values are ignored")
  state = refreshMetricsFreshness(state, 2100)
  assert.equal(state.stale, true)

  state = applyMetricsPayload(state, dockerSample({
    timestamp: "2026-11-20T00:00:01Z",
    gpu: [{ ...dockerSample().gpu[0], utilization_percent: 70 }],
  }), 2200, "docker")
  assert.equal(state.receivedAt, 2200)
  assert.equal(state.stale, false)
  assert.equal(state.gpu.utilization_percent, 70)

  state = applyMetricsPayload(state, dockerSample({
    timestamp: "2026-11-20T00:00:02Z", gpu: [], temperatures: [],
  }), 2300, "docker")
  assert.equal(state.gpu, null)
  assert.equal(state.availability.gpus, null, "Docker does not invent a VM unavailability reason")
  assert.equal(state.availability.temperatures, null)
  assert.equal(displayMetric(null, state.availability.gpus), "—")
  assert.equal(state.history.at(-1).gpu_util, null, "missing GPU data remains a chart gap")
  assert.equal(state.history.at(-1).vram_pct, null)
  assert.equal(state.history.at(-1).gpu_temp, null)
})

test("Docker history accepts bounded legacy rows but seeds chart data only", () => {
  const rows = Array.from({ length: 60 }, (_, index) => dockerHistorySample({
    ts: 10 + index * 0.5,
    gpu_util: index === 0 ? 0 : index,
  }))
  assert.equal(rows.every(isValidDockerHistorySample), true)
  let state = seedMetricsHistory(createInitialMetricsState(), rows, "docker")
  assert.equal(state.history.length, 60)
  assert.equal(state.history[0].gpu_util, 0)
  assert.equal(state.history[0].vram_pct, 0)
  assert.equal(state.history.at(-1).monotonic_timestamp, 39.5)
  assert.equal(state.runId, null)
  assert.equal(state.sequence, null)
  assert.equal(state.receivedAt, null)
  assert.equal(state.legacyTimestamp, null)
  assert.equal(state.cpu, null)
  assert.equal(state.sourceStatus, "Host metrics · waiting for sample")
  assert.equal(seedMetricsHistory(state, [...rows, dockerHistorySample({ ts: 50 })], "docker"), state,
    "more than the producer's requested 60 rows is rejected")

  const deduplicated = seedMetricsHistory(createInitialMetricsState(), [
    rows[0], { ...rows[0], gpu_util: 55 }, rows[1],
  ], "docker")
  assert.equal(deduplicated.history.length, 2, "same monotonic producer sample is deduplicated")
  assert.equal(deduplicated.history[0].gpu_util, 55)

  const liveTimestamp = "2026-11-20T00:00:00Z"
  const matchingHistory = [dockerHistorySample({ ts: 40, t: liveTimestamp })]
  let liveFirst = applyMetricsPayload(createInitialMetricsState(),
    dockerSample({ timestamp: liveTimestamp }), 100, "docker")
  liveFirst = refreshMetricsFreshness(liveFirst, 2100)
  assert.equal(liveFirst.stale, true)
  const liveReceivedAt = liveFirst.receivedAt
  liveFirst = seedMetricsHistory(liveFirst, matchingHistory, "docker")
  assert.equal(liveFirst.history.length, 1, "seeded producer history replaces the coarse live chart row")
  assert.equal(liveFirst.receivedAt, liveReceivedAt)
  assert.equal(liveFirst.stale, true, "history cannot make live data fresh")
  assert.equal(liveFirst.gpu.utilization_percent, 0)
  assert.equal(liveFirst.runId, null)

  let historyFirst = seedMetricsHistory(createInitialMetricsState(), [
    dockerHistorySample({ ts: 30, t: "2026-11-19T23:59:59Z" }),
    ...matchingHistory,
  ], "docker")
  historyFirst = applyMetricsPayload(historyFirst,
    dockerSample({ timestamp: liveTimestamp }), 300, "docker")
  assert.equal(historyFirst.history.length, 2, "history-first/live-second does not duplicate the latest point")
  assert.equal(historyFirst.receivedAt, 300, "the live snapshot establishes freshness")
  assert.equal(historyFirst.history[0].gpu_util, 0)
  assert.equal(historyFirst.history[1].gpu_util, 0)
})

test("Docker adapters enforce the producer's timestamp precision and bounded shapes", () => {
  assert.equal(isValidDockerMetricsPayload(dockerSample({
    timestamp: "2026-11-20T00:00:00.000Z",
  })), false, "Docker emits whole-second UTC timestamps")
  assert.equal(isValidDockerMetricsPayload(dockerSample({ gpu: new Array(9).fill(dockerSample().gpu[0]) })), false)
  assert.equal(isValidDockerMetricsPayload(dockerSample({ temperatures: new Array(65).fill({
    chip: "fixture", sensor: "Tctl", temperature_c: 0,
  }) })), false)
  assert.equal(isValidDockerHistorySample(dockerHistorySample({ t: "not-a-timestamp" })), false)
  assert.equal(isValidDockerHistorySample(dockerHistorySample({ ts: Number.POSITIVE_INFINITY })), false)
})

test("temperature rendering distinguishes available sensors from unavailable sensors and chart gaps", () => {
  const sample = metricsSample({
    availability: { ...available, temperatures: "available" },
    temperatures: [
      { chip: "k10temp", sensor: "Tctl", temperature_c: 54.6 },
      { chip: "nvme", sensor: "Composite", temperature_c: 38 },
    ],
  })
  assert.equal(isValidMetricsPayload(sample), true)
  const state = applyMetricsPayload(createInitialMetricsState(), sample, 200, "vm")
  assert.equal(formatTemperatureSensors(state.temperatures, state.availability.temperatures),
    "k10temp/Tctl 55°C, nvme/Composite 38°C")
  assert.equal(formatTemperatureSensors(null, "no_sensors"), "— (no_sensors)")
  assert.equal(sparkline([10, null, 30], 3).split("")[1], " ", "null readings leave a visible chart gap")
  assert.equal(sparkline([null, null], 2), "  ")
  assert.equal(sparkRange([null, 10, 30]), "10-30")
  assert.equal(sparkRange([null, null]), "")
})

test("malformed samples do not become fresh and stale status uses monotonic receipt time", () => {
  const malformed = metricsSample({ sequence: 0 })
  assert.equal(isValidMetricsPayload(malformed), false)
  let state = applyMetricsPayload(createInitialMetricsState(), malformed, 200, "vm")
  assert.equal(state.sequence, null)
  assert.equal(state.receivedAt, null)
  assert.equal(state.sourceStatus, "Host metrics · invalid response")

  state = applyMetricsPayload(state, metricsSample(), 500, "vm")
  state = applyMetricsPayload(state, metricsSample({ sequence: 1, cpu: null,
    availability: { ...available, cpu: "permission_denied" } }), 2500, "vm")
  assert.equal(state.receivedAt, 500)
  assert.equal(state.stale, true)
  assert.equal(state.cpu.model, "Fixture CPU", "invalid/duplicate data retains last-known display data")
})

test("unsafe host labels and inconsistent availability are rejected as malformed", () => {
  assert.equal(isValidMetricsPayload(metricsSample({
    gpu: [{ ...metricsSample().gpu[0], name: "GPU\u202e" }],
  })), false)
  assert.equal(isValidMetricsPayload(metricsSample({
    availability: { ...available, memory: "permission_denied" },
  })), false)
})
