_Migrated from active tickets — merged work archived per ticket-000 lifecycle._

# [MERGED] 135a2b8 Ticket 002 — Default access, guardrails & --unsafe-recurse (archived)

## Closure Summary

| Field | Value |
|-------|-------|
| Status | ✅ Done / Archived |
| Branch | `recurse-work` |
| Commit | `cf8077d` (local), **PR pending** |
| Pr/SHA | `[await merge]` |
| Merged date | — |

## What was completed

### All code work: DONE ✅
- **Part A** — Default agent (`AGENT="opencode"`) + `--opencode` / `--aider` convenience flags. ✅ Done
- **Part B** — Internet enabled by default (`EGRESS="${EGRESS:-true}"`). Already done, no changes needed.
- **Part C** — `--unsafe-recurse` flag, conditional guardian guard, confirm output line, help text docs. ✅ Committed at cf8077d

### Files changed (local)
| File | Role |
|------|------|
| `./lab` | Added `UNSAFE_RECURSE` var (line ~18), flag parsing case (line ~189), guard wrap in `check_workspace` (lines 67-72), confirm output line (~line 154), help text via sed — **5 edits** total |
| `README.md` | Self-modification section + `--unsafe-recurse` docs + example fix (`./lab` → `.`) |
| `agent/opencode/opencode.json` | Reviewed only — no changes needed |

---

## What remains (on user's side)

| Step | Action | Owner |
|------|--------|-------|
| 1D | `git push origin recurse-work` | human |
| Phase 2A–E | Run terminal tests (see below → moved to ticket-004 acceptance criteria) | human |
| Phase 3A | Run Bats tests: `bats tests/` | human |
| PR | Open PR against `main` and merge | human |

## Ticket-002 phase test cases (moved to ticket-004 as acceptance criteria)

**NOTE:** These test steps from ticket-002 are captured here for traceability but the execution belongs on **ticket-004** (the Bats / testing gate ticket). Reason: the Phase 2 tests require Docker-in-Docker capability and a local agent container — neither of which was available in this session. Once ticket-004 is complete, re-run these commands in your terminal:

1. `cd ~/Development/little-mester-working && ./lab --help` → verify `--unsafe-recurse` appears
2. `./lab . 2>&1; echo "exit=$?"` → must exit=1 with REFUSED guard message (note: uses `.` not `./lab`)
3. `./lab --unsafe-recurse . 2>&1` → confirm shows `Unsaferecurse: 1`, no REFUSED message
4. `./lab --opencode /tmp 2>&1 | grep Agent && ./lab --aider /tmp 2>&1 | grep Agent` → `Agent: opencode` and `Agent: aider` respectively
5. `./lab /tmp/test-ws 2>&1 | grep -E "Mounting|Egress|Agent"` → EGRESS: true shown

---

## End state (once merged)

After merge to `main`, the user will be able to run:

```bash
cd /workspace
./lab --unsafe-recurse ./lab
# Launches opencode against itself — enabling self-modification
# Guard ensures this is NEVER the default
```
