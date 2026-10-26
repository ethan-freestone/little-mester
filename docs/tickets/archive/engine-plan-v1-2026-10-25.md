# engine-plan.md — Present (current v1) ticket-engine subsystem design decisions

> **Scope:** All designs and decisions for building the current ticketing engine (`ticket-engine-mcp/`). This is what we're building NOW.
>
> **Not here:** Future-ticket-system redesign — that's a separate future effort when we rebuild the system entirely (see H below).
>
> **Per-ticket plans** go in each `docs/tickets/ticket-NNN.md` file. When a plan spans multiple tickets or subsystem, reference it here.

## How this relates to other decision files

| File | What goes in this file vs | Read by |
|------|---------------------------|---------|
| `docs/DECISIONS.md` | Product usage constraints (A.1-A.5), field semantics (D.1-D.4), model tracking (E), index format (G), priority decision (H) | Everyone — this is the PRIMARY "what do each of these fields mean" doc |
| **This file** | Engineering designs scoped to the CURRENT ticket-engine build — schema decisions, routing modes, label system per DECISIONS D.2/D.3, board views, search design, MCP tools, external deployment | Anyone working on `ticket-engine-mcp/` or related engine tickets |
| `architecture/DECISIONS.md` | Engine-specific architectural decisions (C sections) plus cross-cutting constraints (G sections mapping to DECISIONS) | The agent/engineer implementing engine code |
| Per-ticket files | Work items (`ticket-008.md`, `ticket-010.md`, etc.) — step-by-step plan for that specific task. Reference this file when they depend on a shared decision. | The agent doing that ticket's work |

## All tickets referencing this engine-plan

| # | Title | Scope (updated per latest DECISIONS) | Status (2026-10-25) |
|---|-------|------|---------------------|
| **008** | Fix engine TypeScript compile errors | P0 blocker — 5 syntax fixes in engine.ts, remove stale SQLite dep; also update type enum to 6 values per DECISIONS D.2 | ✅ DONE |
| **007** | Generate formal JSON Schema + migration script | P1 — `ticket-v1.json` schema file (uses next_number from index format decision G) | ✅ DONE |
| **014** | Testing strategy resolution — get testing running | P0 blocker — bats-core in container, Jest engine tests, run-tests.sh; NO testing happening currently (docs/DECISIONS.md C.1) | 🔴 NEEDS: MCP server tool tests + migration tests |
| **015** | Model tracking fields + type enum expansion (6 values) + field semantics lock | P1 — Add `suggested_model`, `estimated_models[]`, `models_used[]` to engine + schema; ensure type enum = 6 values; lock field semantics per DECISIONS D.2/E | 🔴 DRAFT — schema has fields, engine doesn't implement them |
| **010** | Multi-field search, dependencies, epics as real tickets, enum labels | P2 — dependency graph engine, epic lifecycle, label validation via scope/severity prefixes per DECISIONS D.3, `search()` method; also add models_used tracking + suggested_model + estimated_models fields to extended schema (gap G.2) | 🔴 DRAFT — basic search only; no deps, no label validation |
| **011** | Board views, dashboards, statistics + export formatting | P2 — status counts, resolution rates, aging reports, rank/summary by priority per DECISIONS H decision; use priority as primary sort | 🟡 PARTIAL — board exists but sorts by number not priority+updated_at |
| **012** | MCP server tools + opencode integration + deprecation of legacy scripts | P3 — all `mesterTickets_*` endpoints (removed git auth tools from scope since deferred to future-ticket system); mcpServers config in opencode.json | 🔴 DRAFT — server works, no opencode config, no deprecation warnings |
| **013** | External deployment mode (co-located + dedicated remote) | P4 — git sync with conflict strategies, offline cache; SSH-based auth deferred per DECISIONS C.2 / G.12 | 🔴 DRAFT |
| **016** | Subticket tree system + cross-area typed links | P2 — First-class subticket hierarchy + typed links (blocks/depends_on/triggers/references/duplicates/spans/supersedes), cycle detection, `_links.json` edge table | 🔴 DRAFT — **TYPO: ~number** |
| **017** | Typed subticket system — link types, cycle detection, index format, schema changes | P2 — Design all 8 link types with reverse-edge semantics, cycle detection, schema additions, `_index.json` vs `_links.json` decision | 🔴 DRAFT |
| **018** | Implement subticket system — schema, engine code, migration pass | P2 — Implementation of 017 design; ordered phases with explicit execution dependencies | 🔴 DRAFT |

## Design decisions for present-engine work

### D.1 Routing modes (all three supported NOW)
All three modes apply to the current engine: same-repo, any-directory, separate-repo. This is not deferred — all three are in scope for v1.

### D.2 Epics = real tickets with lifecycle
Epics are full-fledged tickets (`type` may be spike/feature/bug/improvement/doc/infra per DECISIONS D.2), stored in directory as regular ticket-NNN.json files alongside normal tickets. They have their own status, are independently queryable, and can be depended upon. Composite status from children (auto-computed on read; **not persisted** to the file — see architecture/DECISIONS C.1). Override mechanism available for cases where all children aren't done but epic must resolve.

### D.3 Labels = expandable prefix + value namespace
Per DECISIONS D.3: labels use `prefix/value` format from predefined pools with free-form `user/custom-label` fallback:

| Prefix Set | Allowed Values | What it's for | Validation |
|------------|---------------|---------------|------------|
| **scope** | `agent`, `engine`, `infra`, `docs`, `tests`, `security`, `performance` | Technical boundary of what system area a ticket touches | Engine validates prefix matches a known set, then checks value against that set's allowlist; accepts `user/*` with no further validation |
| **severity** | `trivial`, `minor`, `major`, `critical` | Urgency/impact level for triage — independent from anything else on the ticket | Same validation pattern as scope |
| custom | Any string after `user/` prefix | One-off, temporary, or exploratory categorization that doesn't fit predefined sets | Only checks prefix starts with `user/`; stores rest verbatim |

Per DECISIONS D.2 resolution: **`type` is NOT a label namespace.** It's the top-level required field on every ticket (see DECISIONS D.2 for its 6 allowed values: spike, feature, bug, improvement, doc, infra). Labels add cross-cutting tags around that core identity. There is no `type/*` label prefix — confusion between the two was gap G.4, now resolved.

### D.4 Board view / MCP output = JSON only — NOW
MCP server returns structured JSON data only. Human visualization (board rendering, ticket trees) deferred to a separate future ticket (015). Agent uses engine outputs programmatically within sessions. **Ordering:** Board views sort by `priority` first (critical → major → minor → trivial), then by `updated_at` descending within same priority. Number is ID only, not ordering — see DECISIONS I.

### D.5 Execution model: SEQUENTIAL chains AND parallel branches
Both supported in present-engine: linear dependency chains (A→B→C) and parallel groups under same epic without direct dependencies. Cross-references via conventional commit tags (#N, refs #N) are also valid but tracked differently than formal dependency links.

### D.6 External deployment: BOTH co-located + dedicated remote — NOW
Both patterns in scope for v1:
1. **Co-located:** tickets lived within same repo as code (personal projects). Default mode.
2. **Dedicated separate repo:** one ticket repo serves multiple work repos (company/private code where you can't check in your own metadata).

### D.7 Estimate field + model tracking — updated per DECISIONS E+H

The engine now tracks two model-related fields:

**`suggested_model`** (string, optional): Which model we **recommend** for this task based on historical data of `models_used` across all tickets in the same area. Computed automatically from past work distribution. Can be overridden by the user at creation time.

**`estimated_models`** (array, optional): For ticket-specific estimation — lists 2+ models where each handles one subtask with a suggested boundary:
```json
{ "estimated_models": [
    { "model": "qwen3.6:35b-a3b", "subtask_hint": "implement auth logic" },
    { "model": "qwen3:8b",      "subtask_hint": "write tests + docs" }
  ]
}
```

**`models_used`** (array, auto-populated): Per-model stats from actual work done on this ticket. Engine adds entries automatically when you complete work on the ticket and opencode session ends — user doesn't fill this in.

| Field | Purpose | Who/what sets it |
|-------|---------|------------------|
| `models_used[]` | Actual measured usage per model on this ticket (thinking_time_seconds, idle_time_seconds) | Auto from opencode at session end |
| `suggested_model` | Recommendation for **this type** of work based on area history | Auto-computed, overridable |
| `estimated_models[]` | Projected models for future work split by subtask | Set during estimation (after spike ticket) |

### D.8 Git auth = OUT OF SCOPE NOW (but tracked as deferred in G.12)
Not implementing git SSH/key management in present-engine work. It was part of a removed ticket-009 — no further action needed beyond noting removal in the GAP_AUDIT log. Token-based or GitHub CLI auth recommended for any external git operations.

### D.9 Ticket number = ID (ordering comes from other fields)
Per DECISIONS I: `number` is purely an identifier, not a position index. Its only semantic property is **monotonic increase** — it never decreases but may have gaps when tickets are deleted. Ordering within search/board/dashboards results comes from:
- `priority` (primary for triage views)
- `updated_at` (for activity-based views)
- `created_at` (chronological ordering)
- Dependency chain order (epic children ordered by dependency resolution)

This means ticket numbers don't change when priorities or statuses change — they're stable identifiers like GitHub issue numbers.

### D.10 Index format decision (G.9 resolution)
The `_index.json` uses a **hybrid format** combining the best of both prior proposals:

```json
{
  "schemaVersion": 1,        // from SCHEMA.md naming convention
  "tickets": [               // minimal index entries — file_path is canonical ref
    { "number": 42, "file_path": "ticket-042.json", "status": "draft", "area": "auth" }
  ],
  "next_number": 43,         // from blueprint.md — critical for parallel agents
  "updated_at": "2026-10-15T09:30:00Z"
}
```

Rationale: keeping `next_number` enables deterministic ID generation under concurrent writes (parallel agents). Recomputing it from file scan is safe but not lock-free-safe. SchemaVersion naming from SCHEMA.md rather than blueprint's `version`. Type/title NOT duplicated in index entries — only status/area needed for fast filtering; deeper queries read the ticket JSON files.

---

## E. Future-ticket-system design — NOT in this file (deferred)

When we REDesign the ticketing system (not building on top of current engine, but rebuilding entirely), that gets its OWN decision log file. Not here. This file covers what we're building NOW with present-engine constraints only.

The future-ticket system may introduce:
- Auto-triggers for status transitions based on dependency resolution
- Automated assignment rules (route to right model or engineer)
- Ticket tree visualizations from dependency structures
- Migration path from present-engine format

None of this is coded or documented yet. See GAP_AUDIT.md entry G.5 for current knowledge state.

---

## F. Archive policy for subsystem documents

Sections archived when: **(1)** >90 days old without new context, **(2)** all associated tickets done, **(3)** superseded by later decision. Archives are append-only — never rewritten. Redirect link left in place.

---

## G. Immediate Blockers & This Session's Plan (2026-10-25)

### G.1 Critical fixes (Tier 1 — must fix before anything else)
| # | Issue | File | Fix |
|---|-------|------|-----|
| 1 | Ticket-016 typo: `~number` instead of `number` | `docs/tickets/ticket-016.json` | Change `"~number": 16` → `"number": 16` |
| 2 | Missing `src/cli.ts` — `npm run dev` fails | `package.json` references it | Create stub `src/cli.ts` |
| 3 | Board view sorts by number, not priority+updated_at | `src/mcp-server.ts` line 405 | Fix sort to: priority (critical→major→minor→trivial) then updated_at desc |
| 4 | Archived→draft transition not allowed | `engine.ts` STATUS_TRANSITIONS | Add `archived: ['draft']` per DECISIONS D.3 "archiving from any status is always allowed" implies archived→draft should work |

### G.2 Core engine tickets to close (Tier 2 — this session if time)
| Ticket | Work | Files |
|--------|------|-------|
| **014** | Add MCP server tests (8 tools) | `tests/mcp-server.test.ts` |
| **012** subtasks 1–3 | Wire opencode.json mcpServers config; add deprecation warnings to `ticket-new`, `ticket-lifecycle`, `validate-tickets` | `scripts/regen-configs`, legacy scripts |
| **015** | Implement model tracking fields in engine (`suggested_model`, `estimated_models[]`, `models_used[]`) + extend schema | `src/engine.ts`, `docs/ticket-v1.json` |

### G.3 Subticket/Dependency system (Tier 3 — next session)
| Ticket | Work |
|--------|------|
| **017** | Finalize link type enum (8 types), reverse-edge semantics, cycle detection algorithm, `_links.json` format |
| **018** | Implement Phase 0 (schema), Phase 1 (engine), Phase 2 (link API), Phase 3 (migration) |
| **010** | Full multi-field search (priority, labels, date ranges), dependency filters, label validation |

### G.4 Schema mismatch to resolve
- `ticket-v1.json` has `additionalProperties: false` but 8/18 existing tickets use fields it rejects: `links`, `subtickets`, `notes`, `priority`, `labels`, `estimate`, `suggested_model`, `estimated_models`, `models_used`, `comments`, `attachments`, `spans`, `assigned_to`, `parent`, `is_epic`, `blocked_by`, `dependencies`
- Fix: update schema to allow all documented extended fields (see engine-plan.md E)
