# TICKET-014: Docker-in-Docker inside agent container — failure analysis

**Status**: CLOSED (failed approach, documented for posterity)
**Priority**: HIGH
**Date**: 2026-10-22
**Related**: TICKET-015 (three security modes), DECISION-011 (Docker-in-Docker rejected)

---

## Problem

The goal is to allow the AI agent (running inside the `agent` container) to run Docker containers for its own work — e.g. running tests, building, or executing isolated toolchains — **without** accessing the host's Docker daemon or any host filesystem beyond `/workspace`.

## Attempted approaches

### Attempt 1: Docker-in-Docker (DinD) with privileged container

**Setup**: Agent container runs with `privileged: true` so `dockerd` inside can create nested containers.

**Result**: ❌ Failed. Privileged mode gives the agent full host access (block devices, network namespaces, cgroups), which defeats the security boundary. **Rejected outright.**

### Attempt 2: DinD with rootless Docker + user namespaces

**Setup**: Agent container runs with `userns_mode: host`, `cap_add: SYS_ADMIN`, and a rootless `dockerd` that uses subuid/subgid ranges. The idea: nested containers are user-namespaced and can't touch the host.

**What happened**:
1. `dockerd` starts but fails to create user namespace mappings because the agent container itself is already user-namespaced by Docker.
2. `setup-rootless.sh` (the container entrypoint) exits non-zero, killing the agent immediately.
3. The agent never starts — it's dead on arrival.

**Diagnostic findings**:
- The Docker image itself is fine: setuid bits are present, subuid ranges exist.
- `NoNewPrivs: 0` in the bypass `docker run` only means no flags were passed; the compose security settings were the real blocker.
- The subuid ranges are duplicated (`useradd -m` adds them, then `printf` adds them again) — harmless but messy.
- The failure comes from the **compose security settings**, not the image.

### Attempt 3: `./lab --none --unsafe-recurse` (Docker-in-Docker with no entrypoint)

**Setup**: Skips the entrypoint entirely and runs the agent directly, bypassing `setup-rootless.sh`.

**Result**: ❌ Still failed. Without the entrypoint script, `dockerd` never starts at all, so the agent has no Docker to talk to. Without DinD, the agent is just a normal container — no nested containers.

### Attempt 4: `docker compose --profile agent run --rm --entrypoint "" agent opencode`

**Setup**: Direct compose run with empty entrypoint. Same result as Attempt 3 — no Docker daemon inside the container.

**Result**: ❌ No Docker daemon → no nested containers.

## Root cause summary

Docker-in-Docker inside a Docker container (DiD-in-D) requires either:

| Approach | What it needs | Why it fails here |
|----------|--------------|-------------------|
| **Privileged DinD** | `privileged: true` | Gives agent full host access — violates security boundary |
| **Rootless DinD** | `userns_mode: host`, `SYS_ADMIN`, working subuid in the nested namespace | Docker's user namespace stack can't stack properly; the entrypoint script fails and kills the agent |

The fundamental issue: **Docker was never designed to run inside an already-namespaced container.** The namespace nesting creates conflicts that are difficult or impossible to resolve without weakening the outer sandbox.

## What works today (the working mode)

The agent runs inside a hardened container with:
- `cap_drop: [ALL]` — no Linux capabilities
- `security_opt: ["no-new-privileges:true"]` — no setuid escalation
- `read_only: true` — immutable root filesystem
- `security_opt` + `no-new-privileges` — prevents the agent from gaining any privileges
- Only `/workspace` is mounted from the host

This is secure but **the agent cannot run Docker containers**. It can run anything that's compiled into its image (Python, Node.js, bats, git, ripgrep) but not launch new containers.

## Recommendation

Proceed with one of the three security modes documented in TICKET-015:
1. **Container mode** — current approach, no Docker-in-Docker, agent runs compiled tools only
2. **Sysbox mode** — sysbox-runc runtime enables safe nested containers
3. **Incus VM mode** — full VM boundary, agent runs in a separate VM with nested Docker

See TICKET-015 for detailed comparison and setup instructions.
