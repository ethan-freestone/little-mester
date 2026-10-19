# Architecture Decisions Log — Building the Ticketing System (ticket-engine-mcp)

> **Last updated:** 2026-10-19  
> **Purpose:** Capture architectural decisions for building the ticketing engine itself. NOT about how to use little-mester the product — that's in [docs/DECISIONS.md](../DECISIONS.md).
>
> **Read this before making any engineering change to `ticket-engine-mcp/`.** Read [docs/DECISIONS.md](../DECISIONS.md) for usage/workflow constraints.

---

## A. Session Context & Answers (Never Repeated — Product Usage Constraints)

These are user decisions about how they work that constrain what the ticketing system must support. They live here because they directly affect engineering requirements, not just product usage.

### A.1 Self-edit: --unsafe-recurse (from docs/DECISIONS.md section A.1)
You already use `./lab <workspace> --unsafe-recurse`. The **ticketing system must work in this context** — it cannot require any external bridge script or separate worktree to function. All ticket management tools must be callable from within the agent's normal session.

### A.2 Container + workspace mount (from docs/DECISIONS.md section E)
Little-mester runs in a container with workspace volume-mounted. SSH not managed inside container. Any external/git-based ticketing feature must work within this constraint (no direct key management — use token_env or `git credential-store`).

---

## B. JIRA-equivalent Capability Map

What "JIRA-like" means for this system:

| Capability | Required? | Priority | Notes |
|---|---|---|---|
| Ticket CRUD (create/list/update/archive) | ✅ yes | P0 — Foundation | Engine code exists, needs syntax fixes + compile |
| JSON Schema validation per ticket | ✅ yes | P0 — Foundation | All tickets must pass schema validation on write |
| Status lifecycle enforcement | ✅ yes | P0 | draft → active → resolved → archived (bidirectional draft↔active) |
| Routing modes | ✅ yes | P1 | Same repo / any directory / separate repo |
| Schema-enforced ticket JSON file | ✅ yes | P0 | Formal `.json` schema from SCHEMA.md |
| Multi-field search & filtering | ✅ yes | P1 | area, status, type, priority, labels, q (full-text), date ranges, relationship filters, sorting, pagination |
| Dependencies / blocking chains | ✅ yes | P1 | `dependencies[]`, `blocked_by[]`, transitive chain traversal |
| Epics as real tickets with lifecycle | ✅ yes | P2 | See C.1 below — full lifecycle (draft→active→resolved→archived) |
| Board view (kanban) | ✅ yes (JSON output) | P2 | MCP server returns structured JSON only. Human visualization is a future ticket. |
| Dashboards & statistics | ✅ yes | P2 | Counts by area/status, resolution rate, aging reports, epic summaries |
| Labels — expandable enum + user/ namespace | ✅ yes | P1 | Predefined sets (type/severity/scope) + `user/custom` for free-form. See C.3 |
| estimate field with model-awareness | ✅ included in v1 schema | P2 | See C.7 / C.9b |
| MCP tool interface | ✅ yes | P1 | All tools listed in Phase 4 of ticket-012 |
| Opencode config integration | ✅ yes | P1 | `mcpServers` entry in opencode.json |
| External/deployment mode | ✅ yes | P2 | See C.2 — both co-located and dedicated remote supported |
| Offline cache + sync with conflict resolution | ✅ yes | P2 | `.mester-cache/` directory, `ours`/`theirs`/`minimal` strategies |
| Per-operation confirmation manifest | ✅ yes | P1 | `.mester-confirmation` YAML gating auto vs confirm |
| Markdown → JSON migration script | ❌ deferred until engine has validation tools ready (see C.5) | — | All tickets **will** migrate; just not now |

---

## C. Decisions on Specific Design Choices

### C.1 Epic type — REAL ticket with lifecycle

**Decision:** Epics are full-fledged tickets with their own lifecycle:
- Stored as `ticket-NNN.json` regular files in the directory
- Flagged with `is_epic: true` in addition to standard fields
- Own status transitions (draft → active → resolved → archived)
- Inherit composite status from children (auto-computed on read)
- Queryable independently, depend-on by other tickets
- Override mechanism exists for cases where all children aren't done but epic must resolve

### C.2 External mode — BOTH patterns supported

**Decision:** Both co-located and dedicated remote repos:
1. **Co-located:** Point little-mester at a repo where tickets live WITH the code (versioned alongside). For personal/open-source projects.
2. **Dedicated separate repo:** One ticket repo serves multiple work repos — for company/private code without checking in your own metadata.

### C.3 Labels system — Enum with user/ prefix namespace

**Decision:** Expandable predefined categorization sets + free-form via `user/` prefix:

| Set | Values | Purpose |
|---|---|---|
| **type** | `spike`, `feature`, `bug`, `improvement`, `doc`, `infra` | Kind of work (replaces/extends ticket.type) |
| **severity** | `trivial`, `minor`, `major`, `critical` | Urgency/blocking level for triage |
| **scope** | `agent`, `engine`, `infra`, `docs`, `tests`, `security`, `performance` | What part of the system this touches |

Justification: Pure free-form has no filtering. An expandable enum with namespaces gives you structured searches while staying simple — pick from sets you need, add custom `user/something` when nothing fits.

### C.4 Board view / MCP output — JSON only for now

**Decision:** MCP server returns structured JSON data. Human rendering (kanban visualization) is a future ticket (`015`). Agent uses engine outputs programmatically within sessions.

### C.5 Migration timing of existing tickets — Deferred, not skipped

**Decision:** Keep existing `.md` tickets as-is while building the system. A note in every ticket says they **will** migrate to JSON when:
1. The engine compiles (ticket-008)
2. Schema validation is working (part of ticket-007/010)
3. A migration script exists that can be tested with its own tests (before migration, so it's reliable)

Migration is a future task — NOT an immediate blocker on building the engine. All new tickets go straight to JSON.

### C.6 Execution model — SEQUENTIAL chains AND parallel branches

**Decision:** Tickets support both:
- **Sequential chains:** A → B → C where each depends on prior resolution (blocking chain)
- **Parallel:** Multiple tickets in same epic with no direct dependency, or cross-references via conventional tags (`#N`, `refs #N`) without formal links
- Future stretch goal (ticket-015): visualized "ticket tree" graph showing all relationships

### C.7 Model estimation (stretch included in v1)

**Decision:** Include `estimate` field with optional `estimated_model`. Every ticket should be preceded by a SPIKE sub-ticket (`type: spike`) for documentation/research before active work begins. The SPIKE tracks what research was done and what the estimate is based on.

---

## D. Routing Modes (all three supported)

| Mode | Description | Config key | Override via lab |
|------|-------------|------------|------------------|
| **Same repo (default)** | `docs/tickets/` inside your workspace | `ticket_engine_dir=docs/tickets` | `--override ticket_engine_dir=<custom>` |
| **Any directory** | Anywhere on disk, even outside git, mounted at lab start | `ticket_engine_dir=/any/path/on/disk` | same override |
| **Separate repo** | Different git repo; syncable via flag | `ticket_engine_repo=/path/to/repo` | `--override ticket_engine_repo=<path> --sync` |

Default = mode 1 (same workspace). Tickets always go to the workspace's `docs/tickets/`, not the engine's own repo.
Mode 2 = private/untracked work; user manages where it lives and how it mounts.
Mode 3 = separate source-of-truth plus optional git sync.

---

## E. Ticket Extended Schema (v1 additions)

Beyond base fields (`number`, `area`, `type`, `status`, `title`, `objective`, `created_at`, `updated_at`):

| Field | Type | Required | Purpose |
|-------|------|----------|---------|
| `labels[]` | string array | no | Expandable enum + user/custom tags (see C.3) |
| `priority` | enum: trivial/major/minor/critical | no | Urgency for triage |
| `dependencies[]` | number array | no | Other ticket numbers this one depends on |
| `blocked_by[]` | number array | no | Inverse of dependencies (auto-generated) |
| `parent` | number or null | no | Epic number this ticket belongs to |
| `is_epic` | boolean | — | Auto-set when created as epic; marks it as a grouping ticket |
| `estimate` | number | no | Estimated effort (hours/story points) |
| `estimated_model` | string | no | Which local model projected most efficient for this task |
| `comments[]` | object array | no | Timestamped notes |
| `attachments[]` | string array | no | File paths linked to ticket |
| `spans {start, end}` | object \| null | no | Date range tracking |
| `assigned_to` | string | no | Who is responsible (user or agent) |

---

## F. Complete Plan — All Tasks (Cross-Session To-Do List)

See the individual ticket files for full task breakdowns. This table is the cross-cutting summary:

| Priority | What | Ticket File | Phase(s) |
|----------|------|-------------|----------|
| P0 | Fix engine TypeScript to compile | `ticket-008-engine-fixes.md` | Phase 0 |
| P0 | Generate formal JSON Schema + migration script (deferred) | `ticket-007-ticket-migration-and-schema.md` | Phase 1 |
| P0 | Testing strategy — bats-core + Jest, decide what to test | `ticket-014-testing-strategy.md` | Phase 10 |
| P1 | Search, dependencies, epics as real tickets, enum labels | `ticket-010-search-dependencies-epics-labels.md` | Phase 1.5 + Phase 2 |
| P1 | MCP server tools + opencode integration + deprecation | `ticket-012-mcp-server-opencode-integration.md` | Phase 4 + 5 |
| P2 | Board views, dashboards, statistics, export formats | `ticket-011-board-dashboard-export.md` | Phase 3 |
| P2 | External deployment — co-located + remote repo sync | `ticket-013-external-deployment-remote-sync.md` | Phase 6 |
| P3 | Human-facing ticket visualization in opencode (stretch) | Future ticket-015 | Stretch goal |

---

## G. Constraints & Boundaries

1. **JSON-first is a hard rule.** All tickets go to JSON verified against the formal schema. Markdown is only migratable source material.
2. **Everything runs via `--unsafe-recurse`.** No bridge scripts, no temp clone workflows. The ticketing system must be callable from within any agent session directly. See docs/DECISIONS.md A.1 and B for full context.
3. **Container deployment model.** Workspace is volume-mounted; SSH is not managed inside the container. External/git features work through whatever git has on the host (token_env, git credential-store). See docs/DECISIONS.md E.
4. **`_index.json` is fresh.** No legacy data. Part of this system design only.

---

## H. Archiving Policy

All files in `docs/tickets/architecture/` — including this one — follow the same archive policy:

**Archive when any criterion is met:**
- Time-based: section written 90+ days ago without new context updates
- Completion-based: all related tickets marked DONE in their phase tables
- Superseded: a new decision explicitly overrides it (marked in the entry itself)

**Process:** Move to `docs/tickets/architecture/archive/DECISIONS-vN-archive.md` with timestamped filename. Leave a redirect link in place. Archives are append-only — never rewritten.
