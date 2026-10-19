# DECISIONS — How to use & improve little-mester (the product)

> **What goes in here:** Decisions about how you use, configure, test, and modify little-mester the product day-to-day. This covers constraints that affect any session — not specific engineering designs.
>
> **Not here:** Engineering designs for subsystems (those go in their own scope — see below). Per-ticket planning (goes in ticket docs themselves).

## ⚠️ Read before every session

Always read both this file and the per-subsystem decision logs before starting work:
1. This file — constraints about how you use little-mester, what's true NOW
2. Sub-system files (`docs/tickets/engine-plan.md`, `docs/tickets/architecture/DECISIONS.md`) — engineering designs scoped to each area

---

## A. How to use little-mester

### A.1 Default workflow: --unsafe-recurse direct editing
**You run:** `./lab <workspace> --unsafe-recurse` to launch an agent that directly edits files in the workspace. This includes editing little-mester itself when the workspace IS little-mester.

```bash
cd /path/to/little-mester
./lab --unsafe-recurse    # agent can read/write the current directory
```

- `--unsafe-recurse` lifts the guard that blocks self-mounting (`check_workspace`)
- Agent edits, runs tests (`bats tests/`, `npm test`), and commits — all within its session
- No temp clone bridge needed

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
No bats-core in the agent image, no host fallback installed, no Jest engine test suite written. This is a blocker for all validation of future work.

### C.2 SSH / git auth — OUT OF SCOPE (now)
Container-native SSH not managed here. Token-based or GitHub CLI auth is recommended path for any external git operations. Separate future ticket when needed.

---

## D. Sub-system decision logs

| Subsystem | Location | Scope | Per-ticket plans point to... |
|-----------|----------|-------|------------------------------|
| **How to use** (this file) | `docs/DECISIONS.md` | What's true NOW about little-mester as a product | All decisions in this file are facts — no designs |
| **ticket-engine subsystem** | `docs/tickets/engine-plan.md` + docs/tickets/architecture/DECISIONS.md | Engineering designs scoped to ticket-engine v1 build | The engine tickets (008, 007, 014, etc.) |
| **future-ticket system** | (not yet) | When we redesign the ticketing system entirely (rebuild from scratch) | Separate — not yet needed |

Per-ticket decisions go in each ticket file itself. When a decision spans multiple tickets or subsystem, write it in the appropriate sub-system file above.

---

## E. Models used per domain — pre-defined set for label tracking

You said you want to track **actual model usage** and use those stats for estimation. Here's what that means for labels.

### Label sets (predefined categories)
Use these when categorizing a ticket, NOT for the engine schema (those are separate from `labels[]`). They're here because they map directly to how you'll be tracking model usage across different domains of work:

| Predefined Set | Values | When to use it |
|---------------|--------|----------------|
| **type** | `spike`, `feature`, `bug`, `improvement`, `doc`, `infra` | What KIND of work this is. Use when creating a ticket. `spike` = research/planning only (no code output). All others = actual implementation work. |
| **severity** | `trivial`, `minor`, `major`, `critical` | How urgently does this need attention? `critical` = blocking, must do now. `trivial` = can wait. Use when triaging or prioritizing from a backlog. |
| **scope** | `agent`, `engine`, `infra`, `docs`, `tests`, `security`, `performance` | What PART OF THE SYSTEM this touches. Cross-cutting categorization. A ticket about engine performance might get BOTH `type/feature` AND `scope/engine` + `scope/performance`. |

### Field for model tracking

**`models_used`** (array) — records which models were used on this ticket, how long spent on each, and the actual measured result. This is **automatic data from opencode**, not something you manually fill in.

Per-model stats tracked automatically:
- `model` — e.g., `qwen3.6:35b-a3b`
- `thinking_time_seconds` — how long the model was THINKING (not idle time)
- `idle_time_seconds` — time waiting for input/prompts (tracked separately, not part of thinking time)
- `cost_estimate_usd` — estimated cost based on token usage
- `context_window_used_pct` — what percentage of the context window was consumed

### Derived stats from model tracking

The engine computes these automatically when you query:
- **Estimated remaining** — `estimated_total - actual_spent`, recalculated per-model using actual throughput data
- **Time by model** — how long each model spent on a ticket (for capacity planning)
- **Suggested model for future tickets in this area** — based on historical best performance metric

### Suggested model field

**`suggested_model`** (string) — optional hint about which model would be most appropriate for this task. Computed from the area's average `models_used` distribution: if 70% of past work in `engine` scope used `qwen3.6:35b-a3b`, that becomes the suggestion for new engine tickets. This can be overridden when creating a ticket.

### Complexity-based splitting hints

When a ticket's estimated model-usage exceeds a threshold (e.g., > 4 hours of combined thinking_time across any single model), it should be split into smaller tickets. The `suggested_models` field becomes an array listing 2+ models where each handles one subtask, with suggested boundaries:
```json
{
  "suggested_models": [
    { "model": "qwen3.6:35b-a3b", "subtask_hint": "implement auth logic" },
    { "model": "qwen3:8b", "subtask_hint": "write tests + docs" }
  ]
}
```

---

## F. Archive policy

Sections archived when: **(1)** >90 days old without new context, **(2)** all associated work done, **(3)** explicitly superseded by later decision. Archives are append-only — never rewritten. Redirect link left in place.
