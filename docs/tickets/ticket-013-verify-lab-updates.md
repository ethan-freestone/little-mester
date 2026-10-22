# TICKET-013: Update verify-lab.sh for dual backend + metrics

**Status**: DONE
**Priority**: HIGH
**Completed**: 2026-10-21

## Changes

- Added check for `llamacpp reachable` (health endpoint)
- Now verifies both ollama and llamacpp model servers are accessible from agent container
- Added metrics sidecar verification:
  - `metrics sidecar reachable` — /summary endpoint
  - `metrics JSON valid` — /metrics returns valid JSON
  - `metrics has GPU data` — GPU metrics present
  - `metrics has CPU data` — CPU metrics present
  - `metrics has RAM data` — RAM metrics present
  - `metrics summary readable` — Human-readable summary contains VRAM
  - `get_host_metrics tool` — Plugin-style tool call works
- Renamed from verify-sandbox.sh to verify-lab.sh (broader scope)
