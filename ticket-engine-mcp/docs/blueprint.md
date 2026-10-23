# ticket-engine-mcp — Implementation Plan & Blueprint

## Architecture overview

### Core design principle: JSON-first, _index.json as indexer only
- Tickets = `.json` files on disk (always the source of truth)
- `_index.json` stores metadata for fast cross-workspace queries by area/status/type
- No SQLite; index is tiny + rebuildable from ticket JSON files

### 3 Storage Routing Modes

| Mode | Description | Config key | Override via lab |
|------|-------------|------------|------------------|
| **1. Same repo (default)** | `docs/tickets/` inside the workspace you're pointing at | `ticket_engine_dir=docs/tickets` | `--override ticket_engine_dir=<custom>` |
| **2. Any directory** | Anywhere on disk (`.` is fine), even if outside the git repo or in a separate mounted path — needs to be brought into the container at `./lab` time like any other workspace | `ticket_engine_dir=/any/path/on/disk` | same override |
| **3. Separate git repo** | Different git repo on disk; must be brought into the container as a separate directory mount, plus can auto-sync via `./lab sync-tickets --repo <path>` if user opts in | `ticket_engine_repo=/path/to/repo` | `--override ticket_engine_repo=<path>` + enable sync mode |

- "Default" = mode 1, but tickets always go to the workspace's `docs/tickets/`, not ours.
- Mode 2 is for private/untracked work; user manages where it lives and how it mounts.
- Mode 3 adds git ops (pull/push/sync) — optional flag-gated.

---

## TDD Step-by-Step Plan (one step at a time, verify → commit per step)

Each AC maps to exactly one test. We proceed in strict order: test FAIL → implement → test PASS → commit.

### Phase 1: Scaffolding
| # | TODO | Description | Status |
|---|------|-------------|--------|
| 0  | Setup package.json | Scope `@little-mester/ticket-engine-mcp`, add deps (`zod` for validation, `_index.json` as metadata store) | DONE |
| 0.1 | tsconfig + jest setup | Target ES2021, commonjs, strict; jest w/ ts-jest | DONE |
| 0.2 | node_modules.gitignore | Workspace `.gitignore` covers `node_modules/`; ticket-engine-mcp has its own if needed | DONE |

### Phase 2: Unit Tests + Engine (AC-01 through AC-06)
| # | TODO | Test Name (exact) | Acceptance Cr. | Status |
|---|------|-------------------|---------------|--------|
| 1  | AC-01 test | `'create returns ticket with auto-incremented number'` | AC-01: create returns a Ticket with type `Ticket`, number is next available, file written to disk | ❌ |
| 2  | AC-01 impl | _(implement create())_ | _(pass AC-01 test)_ | ❌ |
| 3  | AC-02 test | `'create rejects invalid type value'` | AC-02: throw on bad type (only feature/bug/infra/pause valid) | ❌ |
| 4  | AC-02 impl | _(implement validation)_ | _(pass AC-02 test)_ | ❌ |
| 5  | AC-03 test | `'list() returns all tickets from _index.json'` | AC-03: list with no args = read _index.json, return array of Ticket | ❌ |
| 6  | AC-03 impl | _(implement list())_ | _(pass AC-03 test)_ | ❌ |
| 7  | AC-04 test | `'update(number, patch) modifies ticket fields correctly'` | AC-04: update specific JSON fields; updates updated_at | ❌ |
| 8  | AC-05 test | `'archive(number) sets status = "archived"'` | AC-05: archive calls update with status=archived + archived_at | ❌ |
| 9  | AC-06 test | `'validate() checks _index.json against filesystem'` | AC-06: validate returns {synced, missing?, extra?} | ❌ |

### Phase 3: CLI (MC-01 through MC-02)
| # | TODO | Description | Status |
|---|------|-------------|--------|
| 10 | MC-01 CLI create/list | `./scripts/ticket-engine create ...` + `./scripts/ticket-engine list ...` wired to engine.ts | ❌ |
| 11 | MC-02 CLI update/archive | `./scripts/ticket-engine update X --status resolved` etc. | ❌ |

### Phase 4: MCP Server (MC-03 through MC-06)
| # | TODO | Description | Status |
|---|------|-------------|--------|
| 12 | MC-03 stdio server skeleton | `src/mcp.ts` creates McpServer, registers tools, writes to stdout/stderr | ❌ |
| 13 | MC-04 tool: mesterTickets_create | Registers create handler (wraps engine.create) | ❌ |
| 14 | MC-05 tool: mesterTickets_list | Registers list handler with area/status filters | ❌ |
| 15 | MC-06 tools: update + archive | Register both handlers wired to engine methods | ❌ |

### Phase 5: Opencode Integration (SC-01 through SC-03)
| # | TODO | Description | Status |
|---|------|-------------|--------|
| 16 | SC-01 Config opencode mcp.json | `mcpServers.little-mester-ticket-engine` pointing to `npm run server` | ❌ |
| 17 | SC-02 Deprecation messages | Update `ticket-new`, `validate-tickets`, `ticket-lifecycle` → emit deprecation hints + call ticket-engine | ❌ |
| 18 | SC-03 Migration script | Parse existing `docs/tickets/*.md`, seed _index.json | ❌ |

---

## Metadata schema (`_index.json`)

```json
{
  "version": 1,
  "tickets": [
    {
      "number": 42,
      "file": "ticket-042.json",
      "area": "auth",
      "type": "feature",
      "status": "draft",
      "title": "Implement OAuth2 flow for user registration"
    }
  ],
  "next_number": 43,
  "updated_at": "2026-10-15T09:00:00Z"
}
```

Notes:
- `number == file number`. No separate PK.
- `file` = canonical ref to `.json` on disk (relative to ticket dir).
- Every field is re-extractable from the JSON source → index is rebuildable.
- Index lives wherever routing config specifies (per-mode).

---

## engine.ts — TypeScript interface

```typescript
export interface Ticket {
    number: number;
    area: string;
    type: 'feature' | 'bug' | 'infra' | 'pause';
    status: 'draft' | 'active' | 'resolved' | 'archived';
    title: string;
    objective?: string;
    created_at: string;
    updated_at: string;
}

export interface TicketEngine {
    ticketDir: string;          // where .json files live
    
    create(params: { area?: string; type?: string; title: string; objective?: string }): Ticket;
    list(area?: string, status?: string): Ticket[];
    update(number: number, patch: Partial<Omit<Ticket, 'id' | 'file_path' | 'number'>>): Ticket | undefined;
    archive(number: number): Ticket | undefined;
    validate(): { synced: boolean; missing_files?: string[]; extra_files?: string[] };
}

export function createTicketEngine({ ticketDir }: { ticketDir: string }): TicketEngine;
```

---

## CLI commands (exact shape)

```bash
# Create
./scripts/ticket-engine create --area X --type feature "Title" [--objective "..."]

# List
./scripts/ticket-engine list [--area X] [--status active|archived] [-o stdout | -o ./output.json]

# Update
./scripts/ticket-engine update 42 --status resolved
./scripts/ticket-engine update 42 --title "New title"

# Archive
./scripts/ticket-engine archive 42
```

---

## Session handoff (2026-10-17)

### Completed Phases A–B
1. Phase A: Foundation complete
   - Archived old tickets, removed deprecated schema files (`docs/tickets/archive/`)
   - Renumbered active tickets by priority order and fixed internal markdown headings
   - Rewrote `docs/workflows.md` → JSON-first format with routing modes and status transitions
   - Added conventional commits rule to workflows.md (all commits must follow `type: description` format)
   - Fixed README.md — now explains pointing `./lab <workspace-path>` at one folder, default egress=true, how to disable
   - Updated this blueprint to JSON-first architecture (removed SQLite section, updated `_index.json` metadata schema)
2. Phase B: Engine spec complete
   - JSON Schema draft documented in ticket-001 and `SCHEMA.md`
   - TypeScript interface frozen in engine.ts
3. Session handoff doc added to this file — user is done for the day

### Remaining work
4. Phase C: Implementation (next session) — write engine.ts core with _index.json routing, TDD tests
5. Phase D: Monitor script (`scripts/monitor`) for GPU/RAM/CPU/temps — need to add before Phase C per A.5 ticket
6. Formal JSON Schema file generation from SCHEMA.md

### Blocked on nothing — all files committed to `feat/ticket-context` branch
