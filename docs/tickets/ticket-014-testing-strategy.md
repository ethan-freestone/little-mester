# Ticket 014 — Testing strategy: decide and implement (resolving "testing is not happening")

## Objective

Define and implement an actual testing pipeline for little-mester. No more writing tests in documentation without ever running them. This ticket answers: what gets tested, how it runs, and how the agent executes it during sessions.

## Context (from DECISIONS.md, section A.5)

- **Current state:** Testing has NOT been happening at all. Tests have been planned and documented everywhere but never actually executed.
- The `tests/lab.bats` file has 13 assertions but bats-core is not installed in the agent image or on the host (ticket-002 was supposed to fix this but wasn't completed).
- Engine unit tests exist as planned items in `package.json` but no actual test files have been written.
- **This is a blocker for everything else** — without tests, there's no quality gate for engine changes, migration work, or any feature development.

## What needs decision (before implementation)

| Question | Options | Status |
|---|---|---|
| **Primary test framework** | Jest (for TypeScript/engine code) + bats-core (for bash scripts/infrastructure) | ✅ Both needed — different domains |
| **Where do tests live?** | `tests/lab.bats` (bats, for shell infrastructure), `ticket-engine-mcp/tests/` (Jest, for engine) | ✅ Already structured this way |
| **How do we run tests from within agent sessions?** | Through MCP tool OR through a simple `run-tests.sh` script the agent can invoke with bash | Need to decide what's simpler and more reliable — see Phase 3 below |
| **Do we need CI/CD or just local testing?** | Start with local only; CI/CD later when needed | ✅ Local only for now |

---

## Phase Tracking

| # | Task | Status | Notes |
|---|------|--------|-------|
| 14.1 | **Decide**: Write down the testing strategy document (framework, what to test, how tests run) | ❌ TODO | This section is the decision — finalize with your confirmation or changes |
| **Phase A: Get bats-core working** | | | |
| 14.2 | Add bats to agent Dockerfile (`agent/Dockerfile`) — first try `apt-get install bats`, then fall back to GitHub clone if broken on bookworm | ❌ TODO | See ticket-002 Part A for details; still valid |
| 14.3 | Create host fallback script: `scripts/install-bats.sh` (OS detect, native pkg or clone-and-install) | ❌ TODO | See ticket-002 Part B; still valid |
| 14.4 | Verify bats works inside container AND on host: `bats tests/lab.bats` must pass all 13 assertions | ❌ TODO | Both environments must work |
| **Phase B: Engine unit test suite** | | | |
| 14.5 | Write Jest test files for engine.ts — ALL public methods get tests | ❌ TODO | Tests for: create (happy path + validation), list (all variants), update (happy path + transition guards), archive, validate, search (if implemented) |
| 14.6 | Add `./run-tests.sh` script that orchestrates both bats-core and Jest runs | ❌ TODO | Single entry point for the entire test suite |
| **Phase C: Agent integration** | | | |
| 14.7 | Document in README/docs how to run tests from within an agent session | ❌ TODO | Agent should be able to invoke `./run-tests.sh` without leaving its session |
| 14.8 | Add TDD gate enforcement to `lab-dev-work sync` (once implemented) or to `check_workspace` guard | ⏸️ BLOCKED | See ticket-004 — need the bridge script first (but that's obsolete now; reconsider as a hook in lab itself) |

---

## Decision: What gets tested, by framework

### Tests for agent/bash infrastructure → bats-core
**What:** The `lab` script and all shell utilities (`check_workspace`, `check_egress_permission`, `confirm()`, etc.)

**Test file:** `tests/lab.bats` (already exists with 13 assertions)

**Additional tests needed:**
- Egress permission combinations (no manifest + egress true/false, manifest with various values)
- Workspace path validation edge cases (nested subdirs, symlinks, relative paths)
- Help text verification that expected flags appear

### Tests for engine TypeScript code → Jest
**What:** All public methods on `TicketEngine` interface in `engine.ts`

**Test files to create:**
- `ticket-engine-mcp/tests/engine/create.test.ts` — happy path, validation, type checking
- `ticket-engine-mcp/tests/engine/list.test.ts` — all variants: no filter, area filter, status filter, combined
- `ticket-engine-mcp/tests/engine/update.test.ts` — happy path, transition guards (valid and invalid transitions), field protection (immutable fields)
- `ticket-engine-mcp/tests/engine/archive.test.ts` — archive existing + non-existing ticket
- `ticket-engine-mcp/tests/engine/validate.test.ts` — validation between files and index
- `ticket-engine-mcp/tests/engine/dependencies.test.ts` — add/list/check dependency chains
- `ticket-engine-mcp/tests/engine/search.test.ts` — multi-criteria filtering (if Phase 2 complete)

### Test runner: `run-tests.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "=== Running bats-core tests ==="
bats "${ROOT}/tests/lab.bats"

echo ""
echo "=== Running Jest engine tests ==="
cd "${ROOT}/ticket-engine-mcp"
npm test -- --verbose --detectOpenHandles --forceExit

echo ""
echo "All tests passed."
```

## Verification

After completion of this ticket:
```bash
# Single command runs everything
./run-tests.sh

# Or just one subsystem
bats tests/lab.bats          # Infrastructure (bats required)
cd ticket-engine-mcp && npm test   # Engine unit tests (Jest required)
```

## Files created/modified

| File | Action |
|---|---|
| `run-tests.sh` (update) | Ensure it orchestrates both bats and Jest |
| `agent/Dockerfile` (modify) | Add bats-core to apt-get install block |
| `scripts/install-bats.sh` (create) | Host fallback installer |
| `tests/lab.bats` (may expand) | Additional test cases as needed |
| `ticket-engine-mcp/tests/**/*.test.ts` (create multiple) | Full unit test suite for all engine methods |

## Notes on Phase D (TDD gate in lab flow)

The TDD gate (ticket-004) requires tests to pass before sync/merge. Since:
- The agent can now run `./run-tests.sh` from within its session when using `--unsafe-recurse`
- We may not need the bridge script at all

Consider making the gate a simple check **in `lab` itself** that runs tests when the workspace is the little-mester repo. This avoids needing any external script.
