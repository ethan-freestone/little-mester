import assert from "node:assert/strict"
import test from "node:test"

import { fetchMetricsJson, metricsBaseForMode } from "../agent/opencode/plugins/host-metrics-tui/metrics-endpoint.js"

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
    const fetchImpl = async (url) => {
      urls.push(url)
      return { ok: true, status: 200, json: async () => ({ url }) }
    }

    assert.deepEqual(await fetchMetricsJson(mode, "/history?n=60", fetchImpl),
                     { url: `${base}/history?n=60` })
    assert.deepEqual(await fetchMetricsJson(mode, "/metrics", fetchImpl),
                     { url: `${base}/metrics` })
    assert.deepEqual(urls, [`${base}/history?n=60`, `${base}/metrics`])
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
