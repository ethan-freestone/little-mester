# Ticket 004 — TDD gate: mandatory testing before merge

## Objective
Enforce the rule that "any change to little-mester MUST pass all existing tests AND create new ones for any new functionality" — programmatically, not as a suggestion.

## Phase tracking

| # | Task | Status | Blocking on |
|---|------|--------|-------------|
| **Phase A: Verify dependent criteria (from ticket-003)** | | | |
| 4.1 | A1: `--unsafe-recurse` guard fires by default | ✅ Satisfied (ticket-003) | — |
| 4.2 | A2: `--unsafe-recurse` lifts guard | ✅ Satisfied (ticket-003) | — |
| 4.3 | A3: `--opencode`/`--aider` flags route correctly | ✅ Satisfied (ticket-003) | — |
| 4.4 | A4: Normal workspace works with defaults | ✅ Satisfied (code present) | — |
| 4.5 | A5: Help text includes `--unsafe-recurse` | ✅ Satisfied (ticket-003) | — |
| **Phase B: TDD gate script** | | | |
| 4.6 | Add verify step to lab flow or agent workflow | ❌ TODO | Since --unsafe-recurse is used directly, the gate should be a simple check in `lab` itself that runs tests when the workspace is the little-mester repo (not a separate script) |
| 4.8 | Auto-gate in workflow via `run-tests.sh` verification step (agent runs tests from within session) | ⏸️ BLOCKED | Depends on ticket-014 (testing strategy); not through bridge script since --unsafe-recurse is used directly |
| **Phase C: Testing verification** | | Dependent on ticket-014 (testing strategy) |
| 4.9 | A6: Bats test suite passes (`bats tests/`) | ⏸️ BLOCKED | ticket-002 Phase A not done (bats not in agent image) + no bats installed on host to verify |
| 4.10 | A7: Scripts work in both bash and fish | ⏸️ BLOCKED | Requires real execution to validate |
| **Phase D: Skill documentation** | | | |
| 4.11 | Create `skills/testing_workflow.md` (docs only) | ❌ TODO | `skills/` directory doesn't exist yet; needs creation before this doc can live there |

## Superseded items — ticket-005 (lab-dev-work)

**Ticket 005 has been archived as superseded.** The `scripts/lab-dev-work` bridge script is no longer needed because you already use `--unsafe-recurse` to edit little-mester directly from agent sessions. See your session decision log at `docs/tickets/architecture/DECISIONS.md` section A.3.

The TDD gate will now be enforced by:
1. Running `./run-tests.sh` (bats-core + Jest) from within the agent's workspace when detecting edits to little-mester files
2. Manual verification by you before approving merges
3. As a Phase B improvement: adding a test run step directly into the `lab` script for the local-when-workspace-is-little-mester case

### Acceptance Criteria — Docker-in-Docker Testing Capability

This ticket's work is **not complete** until bats-core can actually execute tests in both environments (see ticket-014). These tests were originally documented in **ticket-002 Phase 2** but belong here because they require Docker-in-Docker (running agent containers from within the OpenCode session) — capability not available during ticket-002's implementation.

### A1–A5: Already verified via ticket-003 code

Verified against current `lab` script (lines confirmed):
- **A1** ✅ guard fires at line 69-73 of `lab`; die message matches
- **A2** ✅ flag parsing at line 190; UNSAFE_RECURSE=1 allows path through check_workspace
- **A3** ✅ `--opencode`/`--aider` set AGENT at lines 191-192; `confirm()` reflects AGENT
- **A4** ✅ no guard error for `/tmp/test-ws`; confirm() output includes EGRESS: true
- **A5** ✅ `./lab --help` (line 237) prints `--unsafe-recurse` with DANGEROUS tag

### A6–A7: Pending verification

- **A6**: Cannot verify until bats is in the agent image (ticket-002). Currently only `tests/lab.bats` exists — file presence verified, execution depends on ticket-002.
- **A7**: Requires actual bash/fish cross-shell testing. Not validated yet.

## Verification (TDD Gate)

```bash
# After ticket-005 creates scripts/lab-dev-work:
./scripts/lab-dev-work init
git checkout $(grep branch= "$TMPDIR/.lab-dev-workspace" | cut -d= -f2)

# Make a change that breaks a test:
echo '# broken' >> /workspace/tests/lab.bats

# Verify blocks merge:
./scripts/lab-dev-work verify  # → exits with error
./scripts/lab-dev-work sync    # → also blocked by verify gate

# Fix the tests, then succeed:
git add -A && git commit -m "fix: broken test"
./scripts/lab-dev-work verify  # passes
./scripts/lab-dev-work sync    # proceeds
```

## Files (pending creation)
| File | Role |
|------|------|
| `scripts/lab-dev-work` (create) | TDD gate script with init/commit/sync + verify subcommand (spec in ticket-005) |
| `skills/testing_workflow.md` (create) | Document automated enforcement section — needs `skills/` dir first |

---
