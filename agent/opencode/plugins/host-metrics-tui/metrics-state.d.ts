export type SensorAvailability = "available" | "collector_failed" | "malformed" | "no_device"
  | "no_sensors" | "permission_denied" | "timeout" | "unsupported" | null
export type SensorCode = Exclude<SensorAvailability, null>
export type MetricsMode = "docker" | "vm"

export interface Availability {
  cpu: SensorAvailability
  memory: SensorAvailability
  gpus: SensorAvailability
  temperatures: SensorAvailability
}

export interface VmHistorySample {
  run_id: string
  sequence: number
  timestamp: string
  gpu_util: number | null
  vram_pct: number | null
  gpu_temp: number | null
  availability: Record<"cpu" | "memory" | "gpus" | "temperatures", SensorCode>
}

export interface DockerHistorySample {
  run_id: null
  sequence: null
  timestamp: string
  monotonic_timestamp: number | null
  gpu_util: number | null
  vram_pct: number | null
  gpu_temp: number | null
}

export type HistorySample = VmHistorySample | DockerHistorySample

export interface CpuMetrics {
  model: string
  load_avg_1m: number
  load_avg_5m: number
  load_avg_15m: number
  num_cpus: number
}

export interface MemoryMetrics {
  total_gb: number
  used_gb: number
  available_gb: number
  percent: number
}

export interface GpuMetrics {
  index: number
  name: string
  temperature_c: number
  utilization_percent: number
  memory_total_mb: number
  memory_used_mb: number
  memory_free_mb: number
}

export interface TemperatureMetrics {
  chip: string
  sensor: string
  temperature_c: number
}

export interface MetricsState {
  mode: "compact" | "expanded"
  history: HistorySample[]
  transport: MetricsMode | null
  runId: string | null
  pollGeneration: number
  sequence: number | null
  /** Docker reports whole-second UTC timestamps; same-second updates are indistinguishable. */
  legacyTimestamp: string | null
  receivedAt: number | null
  stale: boolean
  gpu: GpuMetrics | null
  cpu: CpuMetrics | null
  memory: MemoryMetrics | null
  temperatures: TemperatureMetrics[] | null
  availability: Availability
  sourceStatus: string
}

export interface MetricsPayload {
  source: "host"
  run_id: string
  sequence: number
  timestamp: string
  cpu: CpuMetrics | null
  memory: MemoryMetrics | null
  gpu: GpuMetrics[]
  availability: Record<"cpu" | "memory" | "gpus" | "temperatures", SensorCode>
  temperatures: TemperatureMetrics[] | null
}

export interface DockerMetricsPayload {
  timestamp: string
  cpu: CpuMetrics
  memory: MemoryMetrics
  gpu: GpuMetrics[]
  temperatures: TemperatureMetrics[]
}

export interface DockerHistoryPayloadSample {
  t: string
  ts: number
  gpu_util: number
  vram_pct: number
  gpu_temp: number
  cpu_load: number
}

export function isValidMetricsPayload(value: unknown): value is MetricsPayload
export function isValidDockerMetricsPayload(value: unknown): value is DockerMetricsPayload
export function isValidHistorySample(value: unknown): value is VmHistorySample
export function isValidDockerHistorySample(value: unknown): value is DockerHistoryPayloadSample
export function createInitialMetricsState(): MetricsState
export function isMetricsSampleNewer(state: MetricsState, sample: unknown): boolean
export function applyMetricsPayload(
  state: MetricsState, sample: unknown, receivedAt: number, mode: MetricsMode,
): MetricsState
export function applyMetricsPollResult(
  state: MetricsState, sample: unknown, receivedAt: number, mode: MetricsMode, generation: number,
): MetricsState
export function seedMetricsHistory(
  state: MetricsState, samples: unknown, mode: MetricsMode,
): MetricsState
export function isMetricsStale(receivedAt: number | null, now: number): boolean
export function refreshMetricsFreshness(state: MetricsState, now: number): MetricsState
export function markMetricsUnavailable(state: MetricsState, now: number): MetricsState
export function markMetricsPollUnavailable(
  state: MetricsState, now: number, mode: MetricsMode, generation: number,
): MetricsState
export function displayMetric(
  value: number | string | null | undefined,
  availability: SensorAvailability,
  format?: (value: number | string) => string,
): string
export function formatTemperatureSensors(
  temperatures: TemperatureMetrics[] | null,
  availability: SensorAvailability,
): string
export function sparkline(values: Array<number | null>, width: number): string
export function sparkRange(values: Array<number | null>): string
