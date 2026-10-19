# DECISIONS — How to use & improve little-mester (the product)

> **What goes in here:** Decisions about how you use, configure, test, and modify little-mester the product day-to-day. This covers constraints that affect any session — not specific engineering designs.
>
> **Not here:** Engineering designs for subsystems (those go in their own scope — see below). Per-ticket planning (goes in ticket docs themselves).

## ⚠️ Read before every session

Always read both this file and the per-subsystem decision logs before starting work:
1. This file — constraints about how you use little-mester, what's true NOW
2. Sub-system files (ticket-engine, future-ticket, ...) — engineering designs scoped to each area

---

## A. How to use little-mester

### A.1 Default workflow: --unsafe-recurse direct editing
**You run:** `./lab <workspace> --unsafe-recurse` to launch an agent that directly edits files in the workspace — including the little-mester repo itself when the workspace IS little-mester.

```bash
# From inside little-mester (when workspace = this repo)
cd /path/to/little-mester
./lab --unsafe-recurse    # agent can read/write the current directory
```

- `--unsafe-recurse` lifts the default guard that blocks self-mounting (`check_workspace`)
- The agent edits, runs tests (`bats tests/` for bash infrastructure, `npm test` for engine), and commits — all within its session
- No temp clone bridge needed (lab-dev-work is obsolete)

### A.2 Default configuration values
| Setting | Value | How to change |
|---------|-------|---------------|
| Default agent | `opencode` | `./lab --aider <workspace>` for aider |
| Default egress | `true` (internet on) | `--isolated` or `--egress-false` |
| Self-edit guard | denies by default | `--unsafe-recurse` sets `UNSAFE_RECURSE=1` |
| Network when egress=true | `[egress, internal]` | — |
| Network when egress=false | `[internal]` only | — |

### A.3 How to verify the sandbox
```bash
./verify-sandbox.sh    # proves security guarantees hold
./lab shell <workspace>  # plain bash inside the sandbox for debugging
```

---

## B. Workflow & discipline

### B.1 Core development cycle (always follow)
Every change follows: **doc → test → code → commit**. No stage skipped, no ordering changes without a ticket explaining why.

### B.2 Conventional commits rule
All commits use the format: `type: concise description` where type is one of `feat`, `fix`, `docs`, `refactor`, `test`, `chore`. This applies to both product code AND ticket management changes.

### B.3 Session handoff — never skip this
When ending a session mid-work, include at the top of relevant docs:
```
## Current status
- What's done: ...
- What's in progress: ...
- Blocked on: ...
- Decisions made this session: [links to DECISIONS entries]
```

Next session reads that first — never re-implement already-completed work.

---

## C. Constraints (what's true, not designs)

### C.1 Testing is NOT happening right now (2026-10-19)
No bats-core in the agent image, no host fallback installed, no Jest engine test suite written. This is a blocker for all validation of future work. See ticket-014 for the plan to fix it.

### C.2 SSH / git auth — OUT OF SCOPE (now)
Container-native SSH not managed here. Token-based or GitHub CLI auth is the recommended path for any external git operations. A separate future ticket will address this when needed.

---

## D. Sub-system decision logs

| Subsystem | Location | Scope | Per-ticket plans point to... |
|-----------|----------|-------|------------------------------|
| **How to use** (this file) | `docs/DECISIONS.md` | What's true NOW about little-mester as a product | — |
| **ticket-engine subsystem** | `docs/tickets/engine-plan.md` | Engine architecture, design decisions that span all engine tickets | All `docs/tickets/ticket-NNN*.md` under 008–014 |
| **future-ticket system** | (not yet created) | Future ticketing system design — when we redesign the system entirely | Separate from engine-plan.md; it's future work |

Per-ticket decisions go in each ticket file itself. When a decision spans multiple tickets or subsystem, write it in the appropriate sub-system file above.

---

## E. Archive policy

Sections archived (moved to `docs/archives/`) when: **(1)** >90 days old without new context, **(2)** all associated work done, **(3)** explicitly superseded by a later decision. Archives are append-only historical records — never rewritten. A redirect link is left in place on the active file.
