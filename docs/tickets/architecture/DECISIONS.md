# Architecture Decisions Log — Building the Ticketing System (little-mester-mcp)

> **Last updated:** 2026-10-26  
> **Purpose:** Capture architectural decisions for building the ticketing engine itself. NOT about how to use little-mester the product — that's in [docs/DECISIONS.md](../../DECISIONS.md).
>
> **Read this before making any engineering change to `little-mester-mcp/`.** Read [docs/DECISIONS.md](../../DECISIONS.md) for usage/workflow constraints and field semantics (D.1-D.4, E).

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
| Status lifecycle enforcement | ✅ yes | P0 | Per DECISIONS.md D.3: draft→active→resolved→archived with bidirectional draft↔active; archiving from any status is always allowed |
| Routing modes | ✅ yes | P1 | Same repo / any directory / separate repo per engine-plan D.1 |
| Schema-enforced ticket JSON file | ✅ yes | P0 | Formal `.json` schema from SCHEMA.md + ticket-v1.json |
| Multi-field search & filtering | ✅ yes | P1 | area, status, type, priority, labels (scope/severity), q (full-text), date ranges, relationship filters, sorting by priority or date, pagination |
| Dependencies / blocking chains | ✅ yes | P1 | `dependencies[]`, `blocked_by[]`, transitive chain traversal — dependency check gates draft→active per DECISIONS D.3 |
| Epics as real tickets with lifecycle | ✅ yes | P2 | See C.1 below — full lifecycle (draft→active→resolved→archived); composite status auto-computed on READ (not persisted) |
| Board view (kanban) | ✅ yes (JSON output) | P2 | MCP server returns structured JSON only; ordering by priority then updated_at per engine-plan D.4 |
| Dashboards & statistics | ✅ yes | P2 | Counts by area/status/priority, resolution rate, aging reports, epic summaries; `priority` is the primary sort field per DECISIONS H decision |
| Labels — prefix/value expandable namespace | ✅ yes | P1 | Only `scope` and `severity` sets with validation + `user/custom-label` free-form. `type` is NOT a label set (DECISIONS D.2: it's the top-level enum field) |
| priority field (top-level, primary sort) | ✅ yes, as DECISIONS H decision | P1 | Values match scope/severity: trivial/major/minor/critical. Different from severity label — broader signal, label-based severity for multi-criteria filtering |
| estimate field with model-awareness | ✅ included in v1 schema | P2 | `suggested_model`, `estimated_models[]`, and auto-populated `models_used[]` per DECISIONS E |
| MCP tool interface | ✅ yes | P1 | All tools listed in Phase 4 of ticket-012. Git auth tools removed from scope (deferred to future-ticket system) |
| Opencode config integration | ✅ yes | P1 | `mcpServers` entry in opencode.json |
| External/deployment mode | ✅ yes | P2 | Both co-located and dedicated remote supported per DECISIONS C.2 — see ticket-013 |
| Offline cache + sync with conflict resolution | ✅ yes | P2 | `.mester-cache/` directory, `ours`/`theirs`/`minimal` strategies |
| Per-operation confirmation manifest | ✅ yes | P1 | `.mester-confirmation` YAML gating auto vs confirm |
| Markdown → JSON migration script | ❌ deferred until engine has validation tools ready (see C.5) | — | All tickets **will** migrate; just not now |

---

## C. Decisions on Specific Design Choices (Updated per latest DECISIONS)

### C.1 Epic type — REAL ticket with lifecycle

**Decision:** Epics are full-fledged tickets with their own lifecycle:
- Stored as `ticket-NNN.json` regular files in the directory
- Flagged with `is_epic: true` in addition to standard fields
- Own status transitions (draft → active → resolved → archived) per DECISIONS D.3
- **Composite status is auto-computed on read only, NOT persisted** to the file — gap G.8 resolution: this prevents sync conflicts in multi-engine mode and ensures the correct composite reflects real-time state | Override mechanism exists for cases where all children aren't done but epic must resolve

### C.2 External mode — BOTH patterns supported

**Decision:** Both co-located and dedicated remote repos:
1. **Co-located:** Point little-mester at a repo where tickets live WITH the code (versioned alongside). For personal/open-source projects.
2. **Dedicated separate repo:** One ticket repo serves multiple work repos — for company/private code without checking in your own metadata.

### C.3 Labels system — Prefix/Value namespace with predefined sets + user/free-form

**Decision:** Per DECISIONS D.3: labels use `prefix/value` format from two predefined sets plus free-form via `user/` prefix:

| Set | Values | Purpose (per DECISIONS D.1) |
|---|---|---|
| **scope** | `agent`, `engine`, `infra`, `docs`, `tests`, `security`, `performance` | What PART OF THE SYSTEM this touches — technical boundary, cross-cutting categorization. Use when a ticket spans multiple areas or needs model routing hints |
| **severity** | `trivial`, `minor`, `major`, `critical` | Urgency/impact level for triage. Can be combined with scope labels for multi-criteria filters that priority alone cannot express (e.g., "high impact but not urgent yet") |

**Custom label:** Any string after `user/` prefix — validated only for the prefix, stored verbatim. Useful for temporary workstreams or exploratory categorization without needing formal scope/severity values. **No separate tag system** (DECISIONS D.4: all tagging falls under labels).

### C.4 Board view / MCP output — JSON only + ordering
**Decision:** MCP server returns structured JSON data. Human rendering deferred to a future ticket. Ordering within board/dashboard views: **priority first** (critical → major → minor → trivial), then **updated_at descending** within same priority. Per DECISIONS I: ticket number is ID only, not an ordering index.

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

### C.7 Model estimation — Three distinct fields per DECISIONS E+H

Per DECISIONS D.E resolution, the engine tracks three model-related concepts that are NOT interchangeable:

| Field | What it means | When set |
|-------|---------------|----------|
| **`suggested_model`** (string) | Which model we RECOMMEND for this work type based on historical `models_used[]` data from other tickets in same area | Auto-computed from history; overridable at creation |
| **`estimated_models[]`** (array of `{model, subtask_hint}`) | Specific models projected to handle SUBTASKS of THIS ticket. Used when complexity suggests splitting across models. | Set during estimation phase after spike ticket completes |
| **`models_used[]`** (array of per-model usage records) | ACTUAL measured data from work done on this ticket: thinking_time_seconds, idle_time_seconds, cost_estimate_usd, context_window_used_pct | Automatically populated by opencode at session end — user does NOT fill in |

Additionally:
- **`estimated_model`** (string) — which local model was used when someone made an estimate. This is NOT the same as `suggested_model`. It records the estimator's context.
- **`estimate`** (number) — effort estimate (hours or story points). Separate from model tracking; it quantifies scope, not who does it.

### C.8 priority vs severity distinction (DECISIONS H resolution)

Per DECISIONS D.H resolution: `priority` is the PRIMARY top-level field for triage and sorting. The engine uses `priority` for board views, search defaults, and dashboard summaries.

**Why both `priority` (field) and `severity` (label set exist?** They serve different purposes in practice:
- `priority: critical` on a ticket means "this must be addressed now" — used by the engine for sorting and filtering
- `severity/critical` as a label can be combined with other labels in multi-criteria queries (`severity/critical scope/security`) to filter tickets that have BOTH properties

Most users will set both to the same value. That's intentional: `priority` is the machine-readable signal; `severity/critical` is the cross-referencing signal that composes with other classifications.

---

## C.9 Little Mester MCP — Single Server, Multiple Modules

**Decision:** The product is **Little Mester MCP** — a single MCP server (`@little-mester/mcp`) with the ticket engine as its **first implementation module** (`@little-mester/ticket-engine-mcp`). Future modules (file search, notes, etc.) will register their tools on the **same server process**.

| Aspect | Decision |
|---|---|
| **Package name** | `@little-mester/mcp` (the server) |
| **Module name** | `@little-mester/ticket-engine-mcp` (this package) |
| **Server name** | `little-mester` (as sent in MCP handshake) |
| **Tool prefix** | `mester_<module>_<action>` — e.g., `mester_tickets_create`, `mester_tickets_list` |
| **Module registration** | All modules import the same `McpServer` instance and call `server.registerTool()` |

**Why single server?**
- Running multiple MCP servers from one opencode session is wasteful (multiple stdio transports, multiple process overhead)
- Modules share the same transport and connection
- Tools are namespaced by prefix, so no collision
- opencode config (`mcpServers` in opencode.json) points to ONE server binary

**Implementation:** The `little-mester-mcp` package (module: `@little-mester/ticket-engine-mcp`) exports:
- `createTicketEngine()` — the core engine (usable standalone or via MCP)
- `registerTicketTools(server)` — registers all `mester_tickets_*` tools on a passed `McpServer` instance
- `main()` — standalone server entry point (for direct testing / dev)

Future modules follow the same pattern: import the server, register their `mester_<module>_*` tools.

---

## C.10 Auto-Discovery + Cross-Project Context (2026-10-25)

**Problem:** The MCP server must work when opencode points at ANY directory — the little-mester repo, a different project, or a worktree. It should IMMEDIATELY act as a fully qualified project manager without manual configuration.

**Decision:** The MCP server performs **auto-discovery on startup** by scanning the current working directory (and parents) for project markers.

**Discovery Algorithm:**

```typescript
function discoverProject(cwd: string): DiscoveryResult {
    // 1. Scan upward from cwd for markers (highest priority first)
    for (let dir = cwd; dir !== path.dirname(dir); dir = path.dirname(dir)) {
        if (fs.existsSync(path.join(dir, '.little-mester.conf'))) {
            return { ticketDir: path.join(dir, 'docs/tickets'), isLittleMesterProject: true, ... };
        }
        if (fs.existsSync(path.join(dir, 'docs/tickets'))) {
            return { ticketDir: path.join(dir, 'docs/tickets'), isLittleMesterProject: false, ... };
        }
        if (fs.existsSync(path.join(dir, 'DECISIONS.md'))) {
            // Found a project with decisions but no tickets dir yet
        }
    }
    // 2. Fallback: the little-mester repo's tickets
    return {
        ticketDir: '/workspace/docs/tickets',
        schemaPath: '/workspace/little-mester-mcp/docs/ticket-v1.json',
        decisionsPaths: ['/workspace/DECISIONS.md', '/workspace/docs/tickets/architecture/DECISIONS.md'],
        isLittleMesterProject: true,
    };
}
```

**DiscoveryResult** includes:
- `ticketDir`: where tickets live
- `schemaPath`: where `ticket-v1.json` lives  
- `decisionsPaths`: all `DECISIONS.md` files found
- `indexPath`: `_index.json` location
- `isLittleMesterProject`: boolean

**Context Loading:** On startup, server reads all discovered DECISIONS.md files and the schema. Key constraints are **injected into tool descriptions** so the agent has immediate context:

| Source | Injected into tool descriptions |
|--------|---------------------------------|
| `DECISIONS.md` | Status transitions (D.3), field semantics (D.2/E), priority order (H) |
| `ticket-v1.json` | Schema constraints for validation errors |
| `architecture/DECISIONS.md` | Engineering constraints (status transitions, field semantics) |

**Module Registration Pattern:** The package exports:
- `createTicketEngine(config)` — core engine
- `registerTicketTools(server, discovery)` — registers all `mester_tickets_*` tools
- `main()` — standalone entry: runs discovery, creates server, registers tools, starts stdio

**Enforcement of "No Manual Edits":**
- Schema validation in tools: every create/update calls `validateData()` — rejects if not following DECISIONS.md
- Index integrity: `validate` tool detects orphan files — manual edits show as mismatches
- Tool descriptions explicitly state: "All ticket operations must go through these MCP tools"

**Container → VM Future-Proofing:** Same discovery logic works in both environments. The MCP server doesn't care where it runs, only that files are on disk.

**Open Questions (Documented in Ticket-012):**
1. Fallback behavior: fail fast vs. fallback to `/workspace`?
2. Multiple projects: cwd wins, then parents, then fallback?
3. Enforcement scope: agent-only or also user?
4. opencode.json `cwd`: active worktree or always `/workspace`?
5. Context freshness: startup-only or per-call?

---

## D. Routing Modes (all three supported)

| Mode | Description | Config key | Override via lab |
|------|-------------|------------|------------------|
| **Same repo (default)** | `docs/tickets/` inside your workspace | `ticket_engine_dir=docs/tickets` | `--override ticket_engine_dir=<custom>` |
| **Any directory** | Anywhere on disk, even outside git, mounted at lab start | `ticket_engine_dir=/any/path/on/disk` | same override |
| **Separate repo** | Different git repo; syncable via flag | `ticket_engine_repo=/path/to/repo` | `--override ticket_engine_repo=<path>` + enable sync mode |

Default = mode 1 (same workspace). Tickets always go to the workspace's `docs/tickets/`, not the engine's own repo.
Mode 2 = private/untracked work; user manages where it lives and how it mounts.
Mode 3 = separate source-of-truth plus optional git sync.

---

## E. Ticket Extended Schema (v1 — updated per DECISIONS D.E+H+G)

Beyond base fields (`number`, `area`, `type`, `status`, `title`, `objective`, `created_at`, `updated_at`):

| Field | Type | Required | Purpose (per DECISIONS docs) |
|-------|------|----------|------------------------------|
| `priority` | enum: trivial / major / minor / critical | no | **Primary triage signal** — used for sorting, board views, default search ordering. See DECISIONS H decision. |
| `labels[]` | string array (prefix/value format) | no | Cross-cutting categorization tags. Predefined sets: scope (7 values), severity (4 values). Free-form via `user/` prefix — validated only for prefix match, stored verbatim. See DECISIONS D.3/D.1 |
| `dependencies[]` | number array | no | Other ticket numbers this one depends on |
| `blocked_by[]` | number array | no | Inverse of dependencies (auto-generated); used by transition guard — gates draft→active per DECISIONS D.3 |
| `parent` | number or null | no | Epic number this ticket belongs to |
| `is_epic` | boolean | — | Auto-set when created as epic; marks it as a grouping ticket, composite status auto-computed on read per C.1 |
| `estimate` | number | no | Estimated effort (hours/story points) |
| `estimated_model` | string | no | Which model was used by the person making the estimate — records estimator's context |
| `suggested_model` | string | no | Auto-computed recommendation for this work type based on historical models_used[] data. Overridable at creation. Per DECISIONS E |
| `estimated_models[]` | array of `{model: string, subtask_hint: string}` | no | Projected model split for complex tickets — each model handles one subtask with boundary hints. Set during estimation phase (post-spike). Per DECISIONS E+H |
| `models_used[]` (auto) | array of per-model usage records {model, thinking_time_seconds, idle_time_seconds, cost_estimate_usd, context_window_used_pct} | auto-generated | **Automatic** — populated by opencode at session end, not user-filled. See DECISIONS E |
| `comments[]` | object array | no | Timestamped notes |
| `attachments[]` | string array | no | File paths linked to ticket |
| `spans {start, end}` | object \| null | no | Date range tracking |
| `assigned_to` | string | no | Who is responsible (user or agent) |

---

## F. Complete Plan — All Tasks (Cross-Session To-Do List)

See the individual ticket files for full task breakdowns. This table is the cross-cutting summary:

| Priority | What | Ticket File | Notes (updated per DECISIONS D.E+H+G resolutions) |
|----------|------|-------------|----------------------------------------------------|
| P0 | Fix engine TypeScript syntax errors and update type enum to 6 values | `ticket-008-engine-fixes.md` + new ticket for enum expansion | Type: spike/feature/bug/improvement/doc/infra per DECISIONS D.2 |
| P0 | Generate formal JSON Schema + migration script (deferred) | `ticket-007-ticket-migration-and-schema.md` | Index format uses next_number (G.9 resolution), priority field (H decision) |
| P0 | Testing strategy — bats-core + Jest, decide what to test | `ticket-014-testing-strategy.md` | Phase 10 |
| P1 | Search, dependencies, epics as real tickets, label validation | `ticket-010-search-dependencies-epics-labels.md` | Also add models_used/suggested_model/estimated_models to extended schema per G.2. Label validation validates scope/severity prefix/value sets + user/* free-form per DECISIONS D.3; type is NOT a label set anymore (G.4 resolved) |
| P1 | MCP server tools + opencode integration + deprecation | `ticket-012-mcp-server-opencode-integration.md` | Removed git auth tools (deferred to future-ticket, per G.5/G.12). Board views sort by priority+updated_at per DECISIONS I |
| P2 | Board views, dashboards, statistics, export formats | `ticket-011-board-dashboard-export.md` | Uses priority as primary sort field; add aging reports for delayed work |
| P2 | External deployment — co-located + remote repo sync | `ticket-013-external-deployment-remote-sync.md` | SSH-based auth deferred per G.12/C.2. next_number format from G.9 resolution |
| P3 | Human-facing ticket visualization in opencode (stretch) | Future ticket-015 | Stretch goal; ordered by dependency chain not ticket number per DECISIONS I |

---

## G. Constraints & Boundaries

1. **JSON-first is a hard rule.** All tickets go to JSON verified against the formal schema. Markdown is only migratable source material.
2. **Everything runs via `--unsafe-recurse`.** No bridge scripts, no temp clone workflows. The ticketing system must be callable from within any agent session directly. See docs/DECISIONS.md A.1 and B for full context.
3. **Container deployment model.** Workspace is volume-mounted; SSH is not managed inside the container. External/git features work through whatever git has on the host (token_env, git credential-store). See docs/DECISIONS.md C.2 and G.12/C.2 |
4. **`_index.json` is fresh.** No legacy data. Part of this system design only; uses hybrid format with schemaVersion + next_number per DECISIONS G resolution — not blueprint's `version` nor SCHEMA.md's schemaVersion-only approach |
5. **Ticket number = ID, not ordering** (DECISIONS I). Monotonically increasing but may have gaps when tickets are deleted. Ordering comes from priority, updated_at, created_at, dependency chain order — NOT ticket number. Two routing modes sharing a number space each maintain their own index; collisions across modes are the user's responsibility to coordinate. |
6. **Epics: composite status is read-only** (G.8 resolution). The engine computes composite from children on every query but does NOT write it back to the epic's JSON file. This avoids sync conflicts in multi-engine mode. Manual `status` field on epic may differ from returned `composite_status`.

---

## H. Archiving Policy

All files in `docs/tickets/architecture/` — including this one — follow the same archive policy:

**Archive when any criterion is met:**
- Time-based: section written 90+ days ago without new context updates
- Completion-based: all related tickets marked DONE in their phase tables
- Superseded: a new decision explicitly overrides it (marked in the entry itself)

**Process:** Move to `docs/tickets/architecture/archive/DECISIONS-vN-archive.md` with timestamped filename. Leave a redirect link in place. Archives are append-only — never rewritten.

---

## I. Structural enforcement — lock, audit log, orphans (2026-10-26)

**Decision:** Ticket files are only written by the engine (`src/engine.ts`), reached through
the CLI (`src/cli.ts`) or the MCP tools (`src/mcp-server.ts`). The engine makes bypasses
*visible* with three cheap mechanisms: a transient lock, an append-only audit log with a
per-entry hash, and orphan detection. It does **not** try to make bypasses impossible.

> **TL;DR for a future agent:** Never hand-write `ticket-NNN.json`, `_index.json`, or
> `_audit.json`. Use `ticket-engine` or the `mester_tickets_*` tools. If `validate` reports
> an orphan or a stale lock, stop and ask a human — do not delete anything.

### I.1 Why enforce at all?

The index (`_index.json`) is derived data. If a ticket file is edited or added by hand,
the index silently drifts and every later query is wrong. The engine cannot prevent a
human or agent from using a text editor. What it *can* do is leave a trail that makes a
bypass obvious during `validate`, so drift is caught before it spreads.

This is **detection, not prevention**:
- We do not sign entries. Anyone with write access could forge the log.
- We do not chain hashes. A chain adds complexity without a meaningful threat-model gain
  for a single-user local tool.
- We do not fail closed. A suspicious store still works; the human is asked to look.

### I.2 The lock is transient

`.engine.lock` exists **only while a write is in progress**:

```
acquire → write ticket file → update index → append audit entry → release
```

- The file contains `{ "pid": <process id>, "acquired_at": "<ISO-8601>" }`.
- `acquireLock()` refuses to proceed if the lock belongs to a **live** process
  (`process.kill(pid, 0)` succeeds or returns `EPERM`).
- If the owning PID is **dead** (`ESRCH`), the lock is stale: the engine logs that it is
  clearing it and continues. This is the recovery path after a crash or `kill -9`.
- A corrupt/unparseable lock is treated as stale and cleared. It cannot protect anything.
- `releaseLock()` removes the lock only if it still belongs to this process.
- Release always happens in a `finally` block, so a thrown error does not leave a lock
  behind.

A lock present at `validate` time is therefore either a live write (informational) or a
stale lock (an error worth raising). A **missing** lock is normal.

### I.3 Audit log format

`_audit.json` is a JSON array, append-only by convention. Each entry is deliberately lean:

```json
{
  "timestamp": "2026-10-26T16:12:03.456Z",
  "operation": "create",
  "ticket_number": 34,
  "hash": "<64 lowercase hex chars>"
}
```

- `operation` is `create`, `update`, `archive`, or `baseline`.
- `hash = sha256(canonical_json({timestamp, operation, ticket_number}))`, where
  `canonical_json` is `JSON.stringify(value)` with no extra whitespace and keys in the
  order shown above.
- The hash covers only those three fields. It is **not** a chain and does not cover the
  ticket content. Its job is narrow: if someone edits an entry's timestamp, operation, or
  ticket number, the stored hash no longer matches and `validate` says so.
- Entries may legitimately refer to lower ticket numbers than earlier entries (for
  example: create #32, create #33, update #32). Ordering is checked by non-decreasing
  `timestamp`, never by ticket number.

The intent is written here and echoed in a comment block at the top of `engine.ts` so the
next reader does not mistake the hash for a blockchain.

### I.4 Orphans and baseline adoption

An **orphan** is a `ticket-NNN.json` file on disk with no audit entry. Orphans are:

- **flagged** by `validate` and `getEnforcement()`;
- **never auto-deleted or auto-adopted** by normal operations (fail-open);
- resolved only by a human choosing one of:
  - `ticket-engine audit --baseline` — adopt every currently un-audited ticket file as a
    `baseline` entry (one-time migration for stores created before enforcement); or
  - deliberate removal of the file (the human's call, outside the engine).

`baseline` entries use the same shape and hash as normal entries. They record that the
file existed before the audit log did, not who created it. Baseline adoption is
idempotent: tickets already present in the log are skipped.

### I.5 `validate` semantics

`validate()` reports two independent things:

1. **Index sync** — every file has an index entry and every index entry has a file
   (`synced`).
2. **Enforcement** — audit hashes are valid, the lock is not stale, and orphans are
   listed (`enforcement`).

The CLI exits `1` if any of these is true, so scripts and agents notice:

- index is out of sync,
- an audit hash is invalid or the log is corrupt,
- a stale lock from a dead PID exists,
- orphan files exist.

Engine writes themselves stay fail-open: enforcement problems do not block create/update/
archive. Only validation raises the alarm.

### I.6 CLI surface

```bash
ticket-engine audit                     # human-readable audit trail
ticket-engine audit --limit 20          # last N entries
ticket-engine audit --json              # raw JSON array
ticket-engine audit --baseline          # adopt un-audited files as baseline entries
ticket-engine validate                  # index + lock + audit + orphans; exit 1 on any problem
```

### I.7 Invariants a future change must preserve

1. Every create/update/archive path goes through `withLock()` and then `appendAuditEntry()`.
2. The lock is released even when the write throws.
3. A live lock is never stolen; a dead lock is always recoverable.
4. Audit entries are never rewritten in place by the engine (append only).
5. Orphans are reported, never silently deleted or adopted by a normal write.
6. The hash covers exactly `{timestamp, operation, ticket_number}` in that key order.
7. `validate` has no side effects; `audit --baseline` is the only adoption path.
