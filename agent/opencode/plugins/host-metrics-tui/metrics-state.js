const AVAILABILITY_KEYS = ["cpu", "memory", "gpus", "temperatures"]
const AVAILABILITY_CODES = new Set([
  "available", "collector_failed", "malformed", "no_device", "no_sensors",
  "permission_denied", "timeout", "unsupported",
])
const METRICS_KEYS = [
  "source", "run_id", "sequence", "timestamp", "cpu", "memory", "gpu",
  "availability", "temperatures",
]
const HISTORY_KEYS = [
  "run_id", "sequence", "timestamp", "gpu_util", "vram_pct", "gpu_temp",
  "availability",
]
const DOCKER_METRICS_KEYS = ["timestamp", "cpu", "memory", "gpu", "temperatures"]
const DOCKER_HISTORY_KEYS = ["t", "ts", "gpu_util", "vram_pct", "gpu_temp", "cpu_load"]
const HISTORY_LIMIT = 240
const DOCKER_HISTORY_LIMIT = 60
const SPARK_CHARS = "▁▂▃▄▅▆█"

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value)
}

function hasKeys(value, keys) {
  return isObject(value)
    && Object.keys(value).length === keys.length
    && keys.every(key => Object.hasOwn(value, key))
}

function isNumber(value, minimum, maximum) {
  return typeof value === "number" && Number.isFinite(value)
    && value >= minimum && value <= maximum
}

function isNullableNumber(value, minimum, maximum) {
  return value === null || isNumber(value, minimum, maximum)
}

function isSafeText(value, maximumLength) {
  return typeof value === "string"
    && new TextEncoder().encode(value).length <= maximumLength
    && !/[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/u.test(value)
}

function isTimestamp(value) {
  if (typeof value !== "string"
      || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/.test(value)) return false
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) && new Date(parsed).toISOString() === value
}

// agent/metrics/gather.py uses UTC timestamps with second precision. Updates
// that happen during the same second cannot be distinguished by the client.
function isDockerTimestamp(value) {
  if (typeof value !== "string"
      || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/.test(value)) return false
  const parsed = Date.parse(value)
  return Number.isFinite(parsed)
    && new Date(parsed).toISOString().slice(0, 19) + "Z" === value
}

function isIdentity(value) {
  return typeof value.run_id === "string" && /^[0-9a-f]{32}$/.test(value.run_id)
    && Number.isSafeInteger(value.sequence) && value.sequence > 0
    && isTimestamp(value.timestamp)
}

function isAvailability(value) {
  return hasKeys(value, AVAILABILITY_KEYS)
    && AVAILABILITY_KEYS.every(key => typeof value[key] === "string"
      && AVAILABILITY_CODES.has(value[key]))
}

function isCpu(value) {
  return hasKeys(value, ["model", "load_avg_1m", "load_avg_5m", "load_avg_15m", "num_cpus"])
    && isSafeText(value.model, 128)
    && ["load_avg_1m", "load_avg_5m", "load_avg_15m"]
      .every(key => isNumber(value[key], 0, 1_000_000))
    && Number.isSafeInteger(value.num_cpus) && value.num_cpus >= 1 && value.num_cpus <= 4096
}

function isMemory(value) {
  if (!hasKeys(value, ["total_gb", "used_gb", "available_gb", "percent"])
      || !isNumber(value.total_gb, Number.MIN_VALUE, 1_000_000)
      || !isNumber(value.used_gb, 0, value.total_gb)
      || !isNumber(value.available_gb, 0, value.total_gb)
      || !isNumber(value.percent, 0, 100)) return false
  return Math.abs(value.used_gb + value.available_gb - value.total_gb) <= 0.16
}

function isDockerMemory(value) {
  if (!hasKeys(value, ["total_gb", "used_gb", "available_gb", "percent"])
      || !isNumber(value.total_gb, 0, 1_000_000)
      || !isNumber(value.used_gb, 0, value.total_gb)
      || !isNumber(value.available_gb, 0, value.total_gb)
      || !isNumber(value.percent, 0, 100)) return false
  return Math.abs(value.used_gb + value.available_gb - value.total_gb) <= 0.16
}

function isGpu(value) {
  return hasKeys(value, ["index", "name", "temperature_c", "utilization_percent",
    "memory_total_mb", "memory_used_mb", "memory_free_mb"])
    && Number.isSafeInteger(value.index) && value.index >= 0 && value.index <= 7
    && isSafeText(value.name, 128)
    && isNumber(value.temperature_c, -100, 250)
    && isNumber(value.utilization_percent, 0, 100)
    && Number.isSafeInteger(value.memory_total_mb) && value.memory_total_mb >= 0
    && value.memory_total_mb <= 2 ** 31 - 1
    && Number.isSafeInteger(value.memory_used_mb) && value.memory_used_mb >= 0
    && value.memory_used_mb <= 2 ** 31 - 1
    && value.memory_used_mb <= value.memory_total_mb
    && Number.isSafeInteger(value.memory_free_mb) && value.memory_free_mb >= 0
    && value.memory_free_mb <= 2 ** 31 - 1
    && value.memory_free_mb <= value.memory_total_mb
}

function isTemperature(value) {
  return hasKeys(value, ["chip", "sensor", "temperature_c"])
    && isSafeText(value.chip, 64)
    && isSafeText(value.sensor, 64)
    && isNumber(value.temperature_c, -100, 250)
}

export function isValidMetricsPayload(value) {
  if (!hasKeys(value, METRICS_KEYS) || value.source !== "host" || !isIdentity(value)
      || !isAvailability(value.availability) || !Array.isArray(value.gpu)
      || value.gpu.length > 8 || !value.gpu.every(isGpu)
      || new Set(value.gpu.map(gpu => gpu.index)).size !== value.gpu.length) return false

  if ((value.availability.cpu === "available") !== (value.cpu !== null)
      || (value.cpu !== null && !isCpu(value.cpu))) return false
  if ((value.availability.memory === "available") !== (value.memory !== null)
      || (value.memory !== null && !isMemory(value.memory))) return false
  if ((value.availability.gpus === "available") !== (value.gpu.length > 0)) return false
  if (value.availability.temperatures === "available") {
    if (!Array.isArray(value.temperatures) || value.temperatures.length < 1
        || value.temperatures.length > 64 || !value.temperatures.every(isTemperature)) return false
  } else if (value.temperatures !== null) {
    return false
  }
  return true
}

function isDockerCpu(value) {
  return hasKeys(value, ["model", "load_avg_1m", "load_avg_5m", "load_avg_15m", "num_cpus"])
    && isSafeText(value.model, 128)
    && ["load_avg_1m", "load_avg_5m", "load_avg_15m"]
      .every(key => isNumber(value[key], 0, 1_000_000))
    && Number.isSafeInteger(value.num_cpus) && value.num_cpus >= 1 && value.num_cpus <= 4096
}

/** Validate only the legacy Docker producer's /metrics shape. */
export function isValidDockerMetricsPayload(value) {
  if (!hasKeys(value, DOCKER_METRICS_KEYS) || !isDockerTimestamp(value.timestamp)
      || !isDockerCpu(value.cpu) || !isDockerMemory(value.memory)
      || !Array.isArray(value.gpu) || value.gpu.length > 8
      || !value.gpu.every(isGpu)
      || new Set(value.gpu.map(gpu => gpu.index)).size !== value.gpu.length
      || !Array.isArray(value.temperatures) || value.temperatures.length > 64
      || !value.temperatures.every(isTemperature)) return false
  return true
}

export function isValidHistorySample(value) {
  if (!hasKeys(value, HISTORY_KEYS) || !isIdentity(value)
      || !isAvailability(value.availability)
      || !isNullableNumber(value.gpu_util, 0, 100)
      || !isNullableNumber(value.vram_pct, 0, 100)
      || !isNullableNumber(value.gpu_temp, -100, 250)) return false
  return value.availability.gpus === "available"
    || (value.gpu_util === null && value.vram_pct === null && value.gpu_temp === null)
}

/** Validate only the legacy Docker producer's /history row shape. */
export function isValidDockerHistorySample(value) {
  return hasKeys(value, DOCKER_HISTORY_KEYS)
    && isDockerTimestamp(value.t)
    && isNumber(value.ts, 0, Number.MAX_SAFE_INTEGER)
    && isNumber(value.gpu_util, 0, 100)
    && isNumber(value.vram_pct, 0, 100)
    && isNumber(value.gpu_temp, -100, 250)
    && isNumber(value.cpu_load, 0, 1_000_000)
}

export function createInitialMetricsState() {
  return {
    mode: "expanded",
    history: [],
    transport: null,
    runId: null,
    pollGeneration: 0,
    sequence: null,
    legacyTimestamp: null,
    receivedAt: null,
    stale: false,
    gpu: null,
    cpu: null,
    memory: null,
    temperatures: null,
    availability: { cpu: null, memory: null, gpus: null, temperatures: null },
    sourceStatus: "Host metrics · waiting for sample",
  }
}

export function isMetricsSampleNewer(state, sample) {
  if (!isValidMetricsPayload(sample)) return false
  if (state.runId === null) return true
  if (sample.run_id !== state.runId) return true
  return sample.sequence > state.sequence
}

function snapshotToHistory(sample) {
  const gpu = sample.gpu[0]
  return {
    run_id: sample.run_id,
    sequence: sample.sequence,
    timestamp: sample.timestamp,
    gpu_util: gpu?.utilization_percent ?? null,
    vram_pct: gpu && gpu.memory_total_mb > 0
      ? Math.round(gpu.memory_used_mb * 1000 / gpu.memory_total_mb) / 10
      : null,
    gpu_temp: gpu?.temperature_c ?? null,
    availability: sample.availability,
  }
}

function historyKey(sample) {
  if (typeof sample.run_id === "string") return `${sample.run_id}:${sample.sequence}`
  if (typeof sample.monotonic_timestamp === "number") {
    return `docker-history:${sample.monotonic_timestamp}`
  }
  return `docker-live:${sample.timestamp}`
}

function mergeVmHistory(history, incoming, targetRunId) {
  const retainedRuns = history.filter(sample => sample.run_id !== targetRunId)
  const targetSamples = new Map()
  for (const sample of history) {
    if (sample.run_id !== targetRunId) continue
    const key = historyKey(sample)
    if (!targetSamples.has(key)) targetSamples.set(key, sample)
  }
  for (const sample of incoming) {
    if (sample.run_id !== targetRunId) continue
    const key = historyKey(sample)
    if (!targetSamples.has(key)) targetSamples.set(key, sample)
  }
  const orderedTargetRun = [...targetSamples.values()]
    .sort((left, right) => left.sequence - right.sequence)
  return [...retainedRuns, ...orderedTargetRun].slice(-HISTORY_LIMIT)
}

export function applyMetricsPayload(state, sample, receivedAt, mode) {
  if (mode === "vm") return applyVmMetricsPayload(state, sample, receivedAt)
  if (mode === "docker") return applyDockerMetricsPayload(state, sample, receivedAt)
  return { ...refreshMetricsFreshness(state, receivedAt), sourceStatus: "Host metrics · invalid response" }
}

export function applyMetricsPollResult(state, sample, receivedAt, mode, generation) {
  if (mode === "vm" && generation !== state.pollGeneration) {
    return refreshMetricsFreshness(state, receivedAt)
  }
  return applyMetricsPayload(state, sample, receivedAt, mode)
}

function applyVmMetricsPayload(state, sample, receivedAt) {
  if (state.transport !== null && state.transport !== "vm") {
    return { ...refreshMetricsFreshness(state, receivedAt), sourceStatus: "Host metrics · invalid response" }
  }
  if (!isValidMetricsPayload(sample)) {
    return { ...refreshMetricsFreshness(state, receivedAt), sourceStatus: "Host metrics · invalid response" }
  }
  if (!isMetricsSampleNewer(state, sample)) {
    return refreshMetricsFreshness(state, receivedAt)
  }

  return {
    ...state,
    history: mergeVmHistory(state.history, [snapshotToHistory(sample)], sample.run_id),
    transport: "vm",
    runId: sample.run_id,
    pollGeneration: state.pollGeneration + (sample.run_id === state.runId ? 0 : 1),
    sequence: sample.sequence,
    receivedAt,
    stale: false,
    gpu: sample.gpu[0] ?? null,
    cpu: sample.cpu,
    memory: sample.memory,
    temperatures: sample.temperatures,
    availability: sample.availability,
    sourceStatus: "Host metrics",
  }
}

function dockerSnapshotToHistory(sample) {
  const gpu = sample.gpu[0]
  return {
    run_id: null,
    sequence: null,
    timestamp: sample.timestamp,
    monotonic_timestamp: null,
    gpu_util: gpu?.utilization_percent ?? null,
    vram_pct: gpu && gpu.memory_total_mb > 0
      ? Math.round(gpu.memory_used_mb * 1000 / gpu.memory_total_mb) / 10
      : null,
    gpu_temp: gpu?.temperature_c ?? null,
  }
}

function dockerHistoryToChartSample(sample) {
  return {
    run_id: null,
    sequence: null,
    timestamp: sample.t,
    monotonic_timestamp: sample.ts,
    gpu_util: sample.gpu_util,
    vram_pct: sample.vram_pct,
    gpu_temp: sample.gpu_temp,
  }
}

function compareDockerHistory(left, right) {
  const byTimestamp = Date.parse(left.timestamp) - Date.parse(right.timestamp)
  if (byTimestamp !== 0) return byTimestamp
  return (left.monotonic_timestamp ?? Number.POSITIVE_INFINITY)
    - (right.monotonic_timestamp ?? Number.POSITIVE_INFINITY)
}

function mergeDockerHistory(history, incoming) {
  const incomingTimestamps = new Set(incoming.map(sample => sample.timestamp))
  const mergedByKey = new Map()
  for (const sample of history) {
    if (typeof sample.run_id === "string") continue
    // A legacy live row has no sub-second identity. Replace it with the
    // producer's precise monotonic history row when that timestamp is seeded.
    if (sample.monotonic_timestamp === null && incomingTimestamps.has(sample.timestamp)) continue
    mergedByKey.set(historyKey(sample), sample)
  }
  for (const sample of incoming) mergedByKey.set(historyKey(sample), sample)
  return [...mergedByKey.values()].sort(compareDockerHistory).slice(-HISTORY_LIMIT)
}

function applyDockerMetricsPayload(state, sample, receivedAt) {
  if (state.transport !== null && state.transport !== "docker") {
    return { ...refreshMetricsFreshness(state, receivedAt), sourceStatus: "Host metrics · invalid response" }
  }
  if (!isValidDockerMetricsPayload(sample)) {
    return { ...refreshMetricsFreshness(state, receivedAt), sourceStatus: "Host metrics · invalid response" }
  }

  // Same-second Docker snapshots may be distinct producer updates, but the
  // legacy timestamp cannot prove that. Only a strictly newer timestamp can
  // advance freshness; sub-second samples may therefore be skipped.
  if (state.legacyTimestamp !== null
      && Date.parse(sample.timestamp) <= Date.parse(state.legacyTimestamp)) {
    return refreshMetricsFreshness(state, receivedAt)
  }

  const gpu = sample.gpu[0] ?? null
  const historySample = dockerSnapshotToHistory(sample)
  const history = state.history.some(existing => existing.run_id === null
      && existing.timestamp === sample.timestamp)
    ? state.history
    : mergeDockerHistory(state.history, [historySample])
  return {
    ...state,
    history,
    transport: "docker",
    runId: null,
    sequence: null,
    legacyTimestamp: sample.timestamp,
    receivedAt,
    stale: false,
    gpu,
    cpu: sample.cpu,
    memory: sample.memory,
    temperatures: sample.temperatures,
    availability: {
      cpu: "available",
      memory: "available",
      gpus: gpu === null ? null : "available",
      temperatures: sample.temperatures.length === 0 ? null : "available",
    },
    sourceStatus: "Host metrics",
  }
}

export function seedMetricsHistory(state, samples, mode) {
  if (mode === "vm") return seedVmMetricsHistory(state, samples)
  if (mode === "docker") return seedDockerMetricsHistory(state, samples)
  return state
}

function seedVmMetricsHistory(state, samples) {
  if (state.transport !== null && state.transport !== "vm") return state
  if (!Array.isArray(samples) || !samples.every(isValidHistorySample)
      || new Set(samples.map(sample => sample.run_id)).size > 1) return state
  const targetRunId = state.runId ?? samples.at(-1)?.run_id
  if (targetRunId === undefined) return state

  return {
    ...state,
    history: mergeVmHistory(state.history, samples, targetRunId),
  }
}

function seedDockerMetricsHistory(state, samples) {
  if (state.transport !== null && state.transport !== "docker") return state
  if (!Array.isArray(samples) || samples.length > DOCKER_HISTORY_LIMIT
      || !samples.every(isValidDockerHistorySample)) return state
  const incoming = samples.map(dockerHistoryToChartSample)
  return { ...state, history: mergeDockerHistory(state.history, incoming) }
}

export function isMetricsStale(receivedAt, now) {
  return typeof receivedAt === "number" && Number.isFinite(receivedAt)
    && typeof now === "number" && Number.isFinite(now)
    && now - receivedAt >= 2000
}

export function refreshMetricsFreshness(state, now) {
  return { ...state, stale: isMetricsStale(state.receivedAt, now) }
}

export function markMetricsUnavailable(state, now) {
  return {
    ...refreshMetricsFreshness(state, now),
    sourceStatus: "Host metrics · retrying",
  }
}

export function markMetricsPollUnavailable(state, now, mode, generation) {
  if (mode === "vm" && generation !== state.pollGeneration) {
    return refreshMetricsFreshness(state, now)
  }
  return markMetricsUnavailable(state, now)
}

export function displayMetric(value, availability, format = String) {
  if (availability !== null && availability !== "available") {
    return `— (${availability})`
  }
  if (value === null || value === undefined) return "—"
  return format(value)
}

export function formatTemperatureSensors(temperatures, availability) {
  if (availability !== "available") return displayMetric(null, availability)
  if (!Array.isArray(temperatures) || temperatures.length === 0) return "—"
  const entries = temperatures.slice(0, 3).map(item =>
    `${item.chip}/${item.sensor} ${Math.round(item.temperature_c)}°C`)
  if (temperatures.length > entries.length) entries.push(`+${temperatures.length - entries.length}`)
  return entries.join(", ")
}

export function sparkline(values, width) {
  if (!Array.isArray(values) || !Number.isSafeInteger(width) || width < 1 || !values.length) return ""
  const visible = values.slice(-width)
  const present = visible.filter(value => typeof value === "number" && Number.isFinite(value))
  if (!present.length) return " ".repeat(visible.length)
  const minimum = Math.min(...present)
  const maximum = Math.max(...present)
  return visible.map(value => {
    if (value === null || typeof value !== "number" || !Number.isFinite(value)) return " "
    if (minimum === maximum) return SPARK_CHARS[4]
    const index = Math.max(0, Math.min(SPARK_CHARS.length - 1,
      Math.round((value - minimum) / (maximum - minimum) * (SPARK_CHARS.length - 1))))
    return SPARK_CHARS[index]
  }).join("")
}

export function sparkRange(values) {
  if (!Array.isArray(values)) return ""
  const present = values.filter(value => typeof value === "number" && Number.isFinite(value))
  if (!present.length) return ""
  return `${Math.min(...present)}-${Math.max(...present)}`
}
