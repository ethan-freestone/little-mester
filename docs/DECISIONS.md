# DECISIONS — How to use & improve little-mester (the product)

> **What goes in here:** Decisions about how you use, configure, test, and modify little-mester the product day-to-day. This covers constraints that affect any session — not specific engineering designs.
>
> **Not here:** Engineering designs for subsystems (those go in their own scope — see below). Per-ticket planning (goes in ticket docs themselves).

## ⚠️ Read before every session

Always read both this file and the per-subsystem decision logs before starting work:
1. This file — constraints about how you use little-mester, what's true NOW
2. Sub-system files (`docs/tickets/engine-plan.md`, `docs/tickets/architecture/DECISIONS.md`) — engineering designs scoped to each area

### Meta rule: Document before acting
**Before doing ANY work** (code, config, tests, scripts, installs, or changes to agent behavior):
1. **Check for existing decisions** — Read `docs/DECISIONS.md` and relevant subsystem decision logs first.
2. **If it's architectural/structural** — Any decision about architecture, defaults, conventions, file layout, dependencies, or agent configuration MUST be discussed with the user first. Do NOT guess or make assumptions.
3. **Write it up** — After discussion/clarification, write the decision to the appropriate DECISIONS file (this one for usage/product workflow, or `docs/tickets/architecture/DECISIONS.md` for engineering/subsystem design) BEFORE implementing. Include rationale, trade-offs if relevant.
4. **Follow the doc→test→code→commit cycle** as stated in workflows — documentation comes first.

This rule applies to all future sessions. No implementation without prior written decision when the choice is non-obvious or architectural.

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

### A.4 Container environment — everything must live in the repo
**Critical constraint:** The sandbox container is ephemeral and gets recreated from the built image. Any changes made inside the running container that aren't persisted will be lost on the next run.

- **`/home/agent` is a tmpfs mount** — writable at runtime but completely wiped when the container is recreated. Do NOT put persistent configs, plugins, or customizations there. They will not survive.
- **All persistent configuration must live in the little-mester repo** (or be baked into the agent Docker image). Files in the repo can be copied during image build or mounted in via compose.
- **The workspace mount** (`/workspace`) persists as long as it's the host directory you choose — that's the only host filesystem the agent sees by design. But configs for the agent/tui/opencode itself need to go in the repo, not in `/home/agent`.
- **When customizing the agent (logos, themes, plugins, TUI config),** put them in the repo (e.g., under `agent/` or a tracked location) and wire them up in `agent/Dockerfile` or compose so they persist across container rebuilds.
- **Avoid duplication** — when adding assets like logos, keep the canonical copy in a single tracked location (ideally under `agent/`) and symlink or copy from there as needed. Do not create multiple scattered copies without documenting why.

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

## D. Fields & categories — what each means and when to use it

> This section resolves the "project manager's question": **what does each category field actually mean, what are they FOR, and when should you use which?**
> These decisions apply to human use of tickets AND to engine validation logic. Both must agree.

### D.1 The taxonomy (how the system organizes work)

Every ticket passes through TWO independent classification systems: **lifecycle fields** (status, type, area) that define WHAT the ticket IS and WHERE it is in its life, and **labels** (scope, severity) that are cross-cutting categorization tools you apply ON TOP.

#### Lifecycle fields — core identity of a ticket

| Field | What it answers | Format | When to set/change |
|-------|-----------------|--------|--------------------|
| **area** | "WHAT domain is this?" — human grouping. Like a folder or project area. | Free-text string (recommend lowercase, hyphens for multi-word) | Set once when creating the ticket. Do NOT change after — if the ticket moves domains, create a new ticket in the correct area rather than moving it and corrupting search/filter. |
| **type** | "WHAT KIND of work is this?" — affects how the engine gates progression. | Enum (see D.2 below) | Set on creation. May be changed between draft↔active only if re-gating is acceptable. Use `spike` when you need research BEFORE committing to a decision, NOT for implementation. |
| **status** | "WHERE in its life is this ticket?" — lifecycle progression marker. Not metadata, not classification. It's the ticket's state machine position. See D.3 for exact semantics. | Enum (see D.3 below) | Changed by you (or auto-computed for epics). Every transition follows `draft → active → resolved → archived` with specific gates. Status DOES determine what search results appear where. |

#### Labels — cross-cutting categorization applied ON TOP

| Field | What it answers | Format | When to use |
|-------|-----------------|--------|-------------|
| **scope** (label set) | "WHAT PART OF THE SYSTEM does this touch?" — technical boundary, not human organization. Helps route tickets to the right domain expert or model. | `scope/<value>` from predefined set: `agent`, `engine`, `infra`, `docs`, `tests`, `security`, `performance` OR free-form `user/custom-label`. Use when a ticket spans multiple areas (e.g., `type/feature scope/engine scope/performance`) — not for every ticket, only for cross-cutting ones. |
| **severity** (label set) | "HOW URGENT is this?" — triage signal independent of type or area. A `scope/engine` ticket could be severity/trivial or severity/critical depending on impact. | `severity/<value>` from predefined set: `trivial`, `minor`, `major`, `critical` OR free-form `user/custom-label`. Use when prioritizing from a backlog, during triage reviews, or when work needs attention that is out of normal flow. |

#### Why labels use the `prefix/value` format

Labels are scoped by namespace to avoid collisions between independent label sets. `type/feature` is different from `scope/engine` — even though both use `/` notation, they come from different predefined pools. The engine validates each prefix against its expected value set.

### D.2 Type enum — expanded to 6 values

**Decision (G.1 resolution):** `type` is a top-level required field with these 6 allowed values:

| Value | Meaning | How the engine treats it differently |
|-------|---------|--------------------------------------|
| **spike** | Research/exploration task. Output is findings, NOT implementation code. No code path. Use this WHENEVER you need to answer a "should we" or "how" question before committing to a `feature` or `bug` ticket that implements it. | Spike tickets do not gate downstream work. Other tickets should depend on the spike being resolved BEFORE they start. The engine may surface pending spikes differently in search views (e.g., "blocked research" vs active features). |
| **feature** | New functionality being built. Implementing something that didn't exist before. | Standard feature flow: draft → active → resolved → archived |
| **bug** | Defect fix or regression resolution. Something that was working and broke, or behavior not matching the spec. | Standard bug flow; engine may surface bugs in high-priority views faster than features |
| **improvement** | Non-functional enhancement — performance tuning, code quality, refactoring with no user-visible behavioral change (or one where the behavior is unchanged). | Same lifecycle but tracked separately from features for reporting: "how much of our work was pure improvement vs new features?" |
| **doc** | Documentation-only task with no code changes expected. | May be auto-resolved faster if no testing gates apply to documentation tickets. No engine-side dependency chains on purely doc tickets unless you add them manually. |
| **infra** | Infrastructure work — tooling, CI, deployment, setup, configuration changes. Affects the development environment rather than product features. | Same lifecycle as feature but grouped separately in area dashboard reports: "how much capacity is going to infra?" |

### D.3 Status field semantics — formal definition (G.3 resolution)

> This is THE authoritative definition of status meanings and transitions. Supersedes any informal references in other documents.

Each status has three things defined: its **meaning**, what **transition gates** apply, and **what it triggers**.

| Status | Meaning | Allowed Transitions (from here → to) | Transition Gate (when entering) | What It Triggers |
|--------|---------|--------------------------------------|----------------------------------|-------------------|
| **draft** | Ticket exists but work has NOT begun. Created state for every new ticket. Can be revisited if a ticket leaves `active`. | → active, → archived | None (create is always allowed). Entering from resolved means re-open — no dependency checks needed. | If this was previously `resolved`, the engine resets any auto-computed fields (spike research may need refreshing). If it has dependents that were "unblocked" by previous resolution, they are NOT automatically re-blocked (you must assess manually). |
| **active** | Work IS IN PROGRESS on this ticket. You (or an agent) have explicitly started implementing the objective. | → resolved, → draft | **Blocked if any dependency is NOT resolved/archived.** The engine checks `blocked_by` chain transitively. If blocked, no transition allowed and `reason` explains which tickets block progress. | Engine may auto-suggest related work (tickets depending on this one are "next in chain"). Area stats count toward "active capacity." |
| **resolved** | Work is COMPLETE but awaiting confirmation or closure. Not yet archived — still visible in reports, queryable for history. | → draft (reopen), → archived | None by default. Epics: status auto-computed from children — cannot be manually moved to resolved until all children are resolved OR overridden per C.3 of architecture DECISIONS. | Resolved tickets become "candidates for archival." If they stay unresolved past a threshold (configurable, default 30 days), they appear in aging reports for review. |
| **archived** | Terminal state. Ticket removed from active cycle. Read-only from this point — cannot be transitioned FROM archived to anything else. The ticket file still exists on disk but the engine treats it as immutable history. | NO transitions out. Only → archived is allowed (from any status). | None. Anyone can archive (no gate check). The gate check before moving to resolved handles dependency completeness instead. | Archived tickets drop out of default search results (include `include_archived=true` to find them). No capacity impact. Search still works on historical data if included explicitly. Epic archival cascades but does not affect children (children continue independently). |

#### Diagram of all valid transitions:
```
draft ──► active ──► resolved ──► archived
  ▲         ╲       ↗             ^
  └────────━━┘────/              │
                               any status ──/
```

**Key design decision:** Status is a simple state machine ONLY. No dependency chains or business logic in the transitions themselves (dependency checks happen BEFORE entering `active`, not as part of the transition). The lifecycle path is intentionally flat: forward, backward to draft once, and terminal at archived. Anything more complex belongs in epics + dependencies, not status itself.

### D.4 The tag problem — explicit decision

**Decision:** There is NO separate "tag" field. All tagging falls under `labels[]` with the namespace prefix system described in D.1. If you need ad-hoc classification that doesn't fit into scope/severity/any known set, use `user/custom-tag`. This prevents "tag proliferation" where every person invents their own classification without shared vocabulary.

Labels with a `user/` prefix have NO validation beyond the prefix check. They are stored as-is — useful for temporary workstreams, one-off tasks, or ideas you want to categorize but haven't settled into a formal label set yet.

---

## E. Models used per domain — pre-defined set for label tracking

You said you want to track **actual model usage** and use those stats for estimation. Here's what that means for labels.

### Label sets (predefined categories)

| Predefined Set | Values | When to use it |
|---------------|--------|----------------|
| **type** | `spike`, `feature`, `bug`, `improvement`, `doc`, `infra` (now expanded per D.2 above) | What KIND of work this is. Use when creating a ticket. `spike` = research/planning only (no code output). All others = actual implementation work. |
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

### Suggested model field vs estimated model

These are related but NOT the same:

| Field | Meaning | Computed by/When set |
|-------|---------|----------------------|
| `suggested_model` (string) | Which model we **recommend** for this task based on historical data. "In this area, 70% of past work used qwen3.6:35b-a3b → that's the suggestion." | Auto-computed from `models_used[]` history when queried; can be overridden by human at creation time |
| `estimated_models` (array) | Which model(s) are projected for this specific ticket. For complexity-split tickets, lists 2+ models with subtask boundaries: `[{ model, subtask_hint }]` | Set during estimation phase (after spike); may be empty if no one has estimated yet |

**Why the distinction:** `suggested_model` is a general recommendation ("for this area, use X"). `estimated_models` is ticket-specific ("for THIS task, split: implement auth logic with X, write tests with Y"). A ticket about "migrate infra to containerize" might have `suggested_model: "qwen3.6:35b-a3b"` (area history) but `estimated_models: [{ model: "qwen3:8b", subtask_hint: "write config files and tests" }]` because that smaller model is adequate for the specific work.

### Complexity-based splitting hints

When a ticket's estimated model-usage exceeds a threshold (e.g., > 4 hours of combined thinking_time across any single model), it should be split into smaller tickets. The `estimated_models` field becomes an array listing 2+ models where each handles one subtask, with suggested boundaries:
```json
{
  "estimated_models": [
    { "model": "qwen3.6:35b-a3b", "subtask_hint": "implement auth logic" },
    { "model": "qwen3:8b", "subtask_hint": "write tests + docs" }
  ]
}
```

---

## F. Archive policy

Sections archived when: **(1)** >90 days old without new context, **(2)** all associated work done, **(3)** explicitly superseded by later decision. Archives are append-only — never rewritten. Redirect link left in place.

---

## G. Engineering Index Format Decision (G.9 resolution)

### `_index.json` canonical format

**Decision:** Adopt a hybrid approach that preserves SCHEMA.md's `schemaVersion` naming convention while including blueprint's `next_number` for deterministic ID generation (critical for parallel agents).

```json
{
  "schemaVersion": 1,
  "tickets": [
    {
      "number": 42,
      "file_path": "ticket-042.json",
      "status": "draft",
      "area": "auth"
    }
  ],
  "next_number": 43,
  "updated_at": "2026-10-15T09:30:00Z"
}
```

**Rationale for keeping `next_number`:** The engine must generate unique ticket numbers deterministically when multiple agents are creating tickets in parallel (or a human and an agent). Recomputing `next_number` from file scan is safe but not deterministic under concurrent modification. Storing it in the index allows optimistic concurrency: read → use → write with version check. If conflicting writers are possible (future-ticket system), this field becomes the concurrency token.

**What's NOT in `_index.json`:** Unlike blueprint.md's proposal, we do NOT duplicate `type` or `title` in every IndexEntry. Those live exclusively in the ticket JSON file. The index tracks only: number, file_path, status, area — sufficient for filtering and fast listing without reading every ticket file.

---

## H. Field precedence decision (G.10 resolution)

### priority vs severity — decided

**Decision:** `priority` is the primary top-level field with values matching scope's predefined set: `trivial`, `minor`, `major`, `critical`. The engine uses `priority` for sorting, filtering, and board views.

`severity` remains as a **label namespace only** (`severity/critical`, etc.) for cross-cutting classification that can be combined with other labels. In practice, most users will set both `priority` (top-level) and `severity/<value>` (label) to the same value — this is intentional and not redundant:
- `priority` on `feature/bug` controls how the ticket appears in board views and search results
- `severity/critical` on a label can be queried AND combined with other labels (`severity/critical scope/security`) for multi-criteria filters that `priority` alone cannot express

**Why keep both:** Not every classification question maps cleanly to "urgency." A ticket about engine performance might have priority=minor (not urgent) but severity/major (high impact if it breaks). The label-based severity is the broader signal; priority is the triage shorthand.

### Gaps documented elsewhere (from GAP_AUDIT.md)

| # | Reference | Status |
|---|-----------|--------|
| G.4 | Labels vs top-level fields — resolved by D.1 above (labels = cross-cutting, lifecycle fields = core identity) | RESOLVED |
| G.5 | Future-ticket automation rules | DEFERRED — explicitly out of current scope; will be documented in a future decision log |
| G.7 | Ticket numbering edge cases (deletion, multi-repo collision avoidance) | Documented: `next_number` is monotonically increasing and never decrements. Two routing modes sharing the same number space must coordinate through shared `_index.json`; each mode has its own index. When tickets are deleted, their numbers are NOT recycled (like GitHub issue numbers). If `_index.json` is corrupted/lost, `write_from_scratch()` recomputes from file scan and bumps next_number to max+1 — safe but irreversible for old gaps. |
| G.8 | Epic auto-computed status persist vs compute | See architecture/DECISIONS.md C.1: composite status is computed on read (not persisted). The epic's `status` field in its JSON reflects its last manual change. When querying, the engine returns composite_status separately from status. This prevents sync conflicts with multi-engine modes while preserving manual override capability. |
| G.11 | Status claims vs actual artifacts | Documented as informational note in GAP_AUDIT.md — not actionable without a full audit |
| G.12 | Ticket-009 cancelled, noting where references remain | ticket-012 tool list; resolved by removing git auth tools from 012 scope (deferred to future ticket) |

---

## I. Ordering vs ID decision (conversation: "I don't care about order")

**Decision:** Ticket numbers (`number` field, `next_number` in index) are **IDs only**, not ordering indices. There is no semantic meaning to their numeric value other than uniqueness and monotonic increase. Ordering of tickets within a view comes from:
- `updated_at` (last modified — default for activity-based views)
- `created_at` (creation time — chronological ordering)
- `priority` (triage-level ordering in search/boards)
- Dependency chains (epic children ordered by dependency, not by number)
- Manual sort parameters in search queries

The ticket tree visualization (future-ticket system, ticket-015 stretch goal) renders structure via dependencies and epic parent-child relationships, NOT via ticket numbers.

---

## I. Sub-system decision logs

| Subsystem | Location | Scope | Per-ticket plans point to... |
|-----------|----------|-------|------------------------------|
| **How to use** (this file) | `docs/DECISIONS.md` | What's true NOW about little-mester as a product | All decisions in this file are facts — no designs |
| **ticket-engine subsystem** | `docs/tickets/engine-plan.md` + docs/tickets/architecture/DECISIONS.md | Engineering designs scoped to ticket-engine v1 build | The engine tickets (008, 007, 014, etc.) |
| **future-ticket system** | (not yet) | When we redesign the ticketing system entirely (rebuild from scratch) | Separate — not yet needed |

Per-ticket decisions go in each ticket file itself. When a decision spans multiple tickets or subsystem, write it in the appropriate sub-system file above.
