export type MetricsMode = "docker" | "vm"

export function metricsBaseForMode(mode: unknown): string | undefined

export function fetchMetricsJson<T = unknown>(
  mode: unknown,
  path: "/metrics" | "/history?n=60",
  fetchImpl?: typeof fetch,
): Promise<T>
