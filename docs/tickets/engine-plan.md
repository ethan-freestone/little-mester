# engine-plan.md — Present (current v1) ticket-engine subsystem design decisions

> **Scope:** All designs and decisions for building the current ticketing engine (`ticket-engine-mcp/`). This is what we're building NOW.
>
> **Not here:** Future-ticket-system redesign — that's a separate future effort when we rebuild the system entirely (see E).
>
> **Per-ticket plans** go in each `docs/tickets/ticket-NNN.md` file. When a plan spans multiple tickets or subsystem, reference it here.

## How this relates to other decision files

| File | What goes here | Who reads this? |
|------|---------------|------------------|
| `docs/DECISIONS.md` | Product usage constraints (A.1-A.5 in that file). These are facts about how you use little-mester. Read first, always. | Everyone |
| **This file** | Engineering designs scoped to the CURRENT ticket-engine build — schema decisions, routing modes, search design, label system, epic design, MCP tools, external deployment. | Anyone working on `ticket-engine-mcp/` or related engine tickets |
| Per-ticket files | Work items (`ticket-008.md`, `ticket-010.md`, etc.) — step-by-step plan for that specific task. Reference this file when they depend on a shared decision. | The agent doing that ticket's work |

## All tickets referencing this engine-plan

| # | Title | Scope |
|---|-------|-------|
| **008** | Fix engine TypeScript compile errors | P0 block — 5 syntax fixes in engine.ts, remove stale SQLite dep |
| **007** | Generate formal JSON Schema + migration script | P1 — `ticket-v1.json` schema file (migration deferred per docs/DECISIONS.md C.2) |
| **014** | Testing strategy resolution — get testing running | P0 block — bats-core in container, Jest engine tests, run-tests.sh; NO testing happening currently (docs/DECISIONS.md C.1) |
| **010** | Multi-field search, dependencies, epics as real tickets, enum labels | P2 — dependency graph engine, epic lifecycle, label validation, `search()` method |
| **011** | Board views, dashboards, statistics + export formatting | P2 — status counts, resolution rates, aging reports, JSON/CSV/markdown-table export |
| **012** | MCP server tools + opencode integration + deprecation of legacy scripts | P3 — all `mesterTickets_*` endpoints, mcpServers config in opencode.json |
| **013** | External deployment mode (co-located + dedicated remote) | P4 — git sync with conflict strategies, offline cache |

## Design decisions for present-engine work

### D.1 Routing modes (all three supported NOW)
All three modes apply to the current engine: same-repo, any-directory, separate-repo. This is not deferred — all three are in scope for v1.

### D.2 Epics = real tickets with lifecycle
Epics are full-fledged tickets (draft → active → resolved → archived), stored in directory as regular ticket-NNN.json files alongside normal tickets. They have their own status, are independently queryable, and can be depended upon. Composite status from children (auto-computed on read). Override mechanism available for cases where all children aren't done but epic must resolve.

### D.3 Labels = expandable enum + user/ namespace
Predefined categories with `user/something` free-form suffix to avoid collisions:

| Set | Values | Purpose |
|-----|--------|---------|
| type | spike, feature, bug, improvement, doc, infra | Kind of work (spike = research/planning before active) |
| severity | trivial, minor, major, critical | Urgency/blocking level for triage |
| scope | agent, engine, infra, docs, tests, security, performance | What part of the system it touches |

### D.4 Board view / MCP output = JSON only — NOW
MCP server returns structured JSON data only. Human visualization is deferred to a separate future ticket (015). Agent uses engine outputs programmatically within sessions.

### D.5 Execution model: SEQUENTIAL chains AND parallel branches
Both supported in present-engine: linear dependency chains (A→B→C) and parallel groups under same epic without direct dependencies. Cross-references via conventional commit tags (#N, refs #N) are also valid but tracked differently than formal dependency links.

### D.6 External deployment: BOTH co-located + dedicated remote — NOW
Both patterns in scope for v1:
1. **Co-located:** tickets lived within same repo as code (personal projects). Default mode.
2. **Dedicated separate repo:** one ticket repo serves multiple work repos (company/private code where you can't check in your own metadata).

### D.7 Estimate field + estimated_model included in v1 schema
`estimate` stores effort; `estimated_model` tracks which local model projected most efficient for the task. Every ticket should be preceded by a SPIKE sub-ticket (`type: spike`) for documentation before active work begins.

### D.8 Git auth = OUT OF SCOPE NOW (but tracked in future)
Not implementing git SSH/key management in present-engine work. It will have its own separate ticket when needed. Token-based or GitHub CLI auth is the recommended path for any external git operations.

---

## E. Future-ticket-system design — NOT in this file

When we REDesign the ticketing system (not building on top of current engine, but rebuilding entirely), that gets its OWN decision log file. Not here. This file covers what we're building NOW with present-engine constraints only.

## F. Archive policy

Sections archived (moved to `docs/archives/`) when: **(1)** >90 days old without new context, **(2)** all associated tickets done, **(3)** superseded by later decision. Archives are append-only — never rewritten. Redirect link left in place.
