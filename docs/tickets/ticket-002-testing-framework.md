# Ticket 002 — bats-core inside agent + host fallback

## Objective
Enable test execution both inside and outside the agent container so that little-mester can validate itself ("little-mester against little-mester").

## Scope & Boundaries
- **Do not** mount `.$LAB_DIR` as workspace (already blocked in `check_workspace`).
- All manifests use `.mester-egress` key-value format: `permission=true|false`, `allowed_domains=`, `intent=`.

## Phase tracking

| # | Task | Status | Notes |
|---|------|--------|-------|
| **Phase A — Bake bats into agent Dockerfile** | | | |
| 2.1 | Add `bats` to `agent/Dockerfile` apt-get install block | ❌ TODO | Not yet implemented; current Dockerfile has git curl ripgrep ca-certificates nodejs npm only |
| 2.2 | Verify bats present inside container image | ❌ TODO | `docker run --rm workspace_agent bats --version` |
| **Phase B — Host fallback installer** | | | |
| 2.3 | Create `scripts/install-bats.sh` (OS detect + native pkg or clone) | ❌ TODO | Script doesn't exist yet; referenced in ticket but never written |
| 2.4 | Validate bats available on host (`bats tests/lab.bats → all pass`) | ❌ TODO | Depends on 2.3 |

---
## Part A — Bake bats-core into agent Dockerfile

### File to edit
`agent/Dockerfile`

### Steps
1. Add `bats` to the existing `apt-get install` block.
2. If `apt-get install bats` fails on bookworm (python:3.12-slim → Debian), fall back to GitHub clone:
   ```dockerfile
   RUN git clone --depth 1 https://github.com/bats-core/bats-core.git /tmp/bats \
       && bash /tmp/bats/install.sh /usr/local \
       && rm -rf /tmp/bats
   ```
3. Build locally:
   ```bash
   sudo docker compose --project-directory . build agent
   ```
4. Confirm bats is present:
   ```bash
   docker run --rm workspace_agent bats --version
   ```

### Verification inside container
```bash
docker run -it --rm workspace_agent bash -c 'bats --version && bats /workspace/tests/'
```

---

## Part B — Standalone install-bats.sh fallback

### File to create
`scripts/install-bats.sh`

### Purpose
- Host-side fallback when user doesn't have pacman or native package manager.
- Safe idempotent: skips if bats already present.
- No root required for clone-and-install path (installs to `~/.bats`), falls back to `/usr/local/bin` with sudo when writable.

### Steps
1. Detect OS (`pacman`, `apt`, `yum`/`dnf`, `brew`).
2. Try native package first; if absent, clone bats-core from GitHub and run its install script.
3. Follow repo conventions: `set -euo pipefail`, `die()` helper, verbose flag support.

### Verification
```bash
bash scripts/install-bats.sh
bats --version
bats tests/lab.bats   # all 13 assertions pass
```

---

## Notes — Docker-in-Docker (DinD)

**DinD is out of scope for ticket-002.** The current `tests/lab.bats` runs host-side by sourcing `lab` directly — no Docker needed. Dind capability (spinning up new little-mester instances from within the sandbox) may require:
- Mounting the host Docker socket into a future container run with appropriate flag
- Running a `docker:dind` sidecar service
- Either approach is documented as future work in ticket-004 Phase C.

