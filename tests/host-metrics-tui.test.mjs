import assert from "node:assert/strict"
import test from "node:test"

import { fetchMetricsJson, metricsBaseForMode } from "../agent/opencode/plugins/host-metrics-tui/metrics-endpoint.js"
import {
  applyMetricsPayload,
  createInitialMetricsState,
  displayMetric,
  formatTemperatureSensors,
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

test("unavailable sensors render their reason and history keeps null gaps instead of zeroes", () => {
  const unavailable = metricsSample({
    gpu: [],
    availability: { ...available, gpus: "no_device" },
  })
  assert.equal(isValidMetricsPayload(unavailable), true)
  const state = applyMetricsPayload(createInitialMetricsState(), unavailable, 10)

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
  let state = applyMetricsPayload(createInitialMetricsState(), metricsSample(), 1000)
  assert.equal(state.sequence, 1)
  assert.equal(state.receivedAt, 1000)
  assert.equal(state.history.length, 1)

  state = applyMetricsPayload(state, metricsSample(), 2500)
  assert.equal(state.receivedAt, 1000, "same run/sequence must not refresh freshness")
  assert.equal(state.history.length, 1, "same run/sequence must not append a duplicate")
  assert.equal(state.stale, false)
  state = refreshMetricsFreshness(state, 3000)
  assert.equal(state.stale, true, "the stale threshold is inclusive at two seconds")

  state = applyMetricsPayload(state, metricsSample({ sequence: 2 }), 3001)
  assert.equal(state.sequence, 2)
  assert.equal(state.receivedAt, 3001)
  assert.equal(state.stale, false)
  assert.equal(state.history.length, 2)

  state = applyMetricsPayload(state, metricsSample({ sequence: 1 }), 3500)
  assert.equal(state.sequence, 2, "older sequence in one process is rejected")
  assert.equal(state.receivedAt, 3001)
  assert.equal(state.history.length, 2)

  state = applyMetricsPayload(state, metricsSample({ run_id: "b".repeat(32), sequence: 1 }), 3600)
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
    [unavailable, unavailable, zero], 100)

  assert.equal(state.history.length, 2)
  assert.deepEqual(state.history.map(sample => sample.gpu_util), [null, 0])
  assert.deepEqual(state.history.map(sample => sample.vram_pct), [null, 0])
  assert.equal(state.sequence, 2)
  assert.equal(state.receivedAt, 100)

  state = seedMetricsHistory(state, [unavailable, zero], 1500)
  assert.equal(state.history.length, 2)
  assert.equal(state.receivedAt, 100, "cached history must not refresh freshness")
})

test("history arriving after a live poll fills older samples without duplicating the latest", () => {
  const second = historySample({ sequence: 2, gpu_util: 50 })
  const first = historySample({ sequence: 1, gpu_util: 25 })
  let state = applyMetricsPayload(createInitialMetricsState(), metricsSample({ sequence: 2 }), 100)
  state = seedMetricsHistory(state, [first, second], 150)

  assert.deepEqual(state.history.map(sample => sample.sequence), [1, 2])
  assert.equal(state.receivedAt, 100, "history's cached latest sample is not a freshness advance")
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
  const state = applyMetricsPayload(createInitialMetricsState(), sample, 200)
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
  let state = applyMetricsPayload(createInitialMetricsState(), malformed, 200)
  assert.equal(state.sequence, null)
  assert.equal(state.receivedAt, null)
  assert.equal(state.sourceStatus, "Host metrics · invalid response")

  state = applyMetricsPayload(state, metricsSample(), 500)
  state = applyMetricsPayload(state, metricsSample({ sequence: 1, cpu: null,
    availability: { ...available, cpu: "permission_denied" } }), 2500)
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
