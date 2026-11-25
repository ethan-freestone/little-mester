const ENDPOINTS = Object.freeze({
  docker: "http://metrics:19532",
  vm: "http://10.0.2.100:19532",
})

const ALLOWED_PATHS = new Set(["/metrics", "/history?n=60"])

export function metricsBaseForMode(mode) {
  return typeof mode === "string" && Object.hasOwn(ENDPOINTS, mode)
    ? ENDPOINTS[mode]
    : undefined
}

export async function fetchMetricsJson(mode, path, fetchImpl = globalThis.fetch) {
  const base = metricsBaseForMode(mode)
  if (!base) throw new Error("metrics mode is not configured")
  if (!ALLOWED_PATHS.has(path)) throw new Error("unsupported metrics request path")
  if (typeof fetchImpl !== "function") throw new Error("fetch is unavailable")

  const response = await fetchImpl(`${base}${path}`, { cache: "no-store" })
  if (!response.ok) throw new Error(`metrics endpoint returned HTTP ${response.status}`)
  return response.json()
}
