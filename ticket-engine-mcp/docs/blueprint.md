# ticket-engine-mcp — Implementation Plan & Blueprint

## Architecture overview

### Core design principle: Markdown-first, SQLite as indexer only
- Tickets = `.md` files on disk (always the source of truth)
- SQLite indexes frontmatter for fast queries across area/status/type
- No bloated DB to commit; index is tiny + rebuildable from markdown

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
| 0  | Setup package.json | Scope `@little-mester/ticket-engine-mcp`, add deps (sql.js for pure-JS SQLite, @modelcontextprotocol/sdk) | DONE |
| 0.1 | tsconfig + jest setup | Target ES2021, commonjs, strict; jest w/ ts-jest | DONE |
| 0.2 | node_modules.gitignore | Workspace `.gitignore` covers `node_modules/`; ticket-engine-mcp has its own if needed | DONE |

### Phase 2: Unit Tests + Engine (AC-01 through AC-06)
| # | TODO | Test Name (exact) | Acceptance Cr. | Status |
|---|------|-------------------|---------------|--------|
| 1  | AC-01 test | `'create returns ticket with auto-incremented number'` | AC-01: create returns a Ticket with type `Ticket`, number is next available, file written to disk | ❌ |
| 2  | AC-01 impl | _(implement create())_ | _(pass AC-01 test)_ | ❌ |
| 3  | AC-02 test | `'create rejects invalid type value'` | AC-02: throw on bad type (only feature/bug/infra/pause valid) | ❌ |
| 4  | AC-02 impl | _(implement validation)_ | _(pass AC-02 test)_ | ❌ |
| 5  | AC-03 test | `'list() returns all tickets from index'` | AC-03: list with no args = SELECT * FROM index, return array of Ticket | ❌ |
| 6  | AC-03 impl | _(implement list())_ | _(pass AC-03 test)_ | ❌ |
| 7  | AC-04 test | `'update(number, patch) modifies ticket fields correctly'` | AC-04: update specific frontmatter fields; updates updated_at | ❌ |
| 8  | AC-05 test | `'archive(number) sets status = "archived"'` | AC-05: archive calls update with status=archived + archived_at | ❌ |
| 9  | AC-06 test | `'validate() checks index against filesystem'` | AC-06: validate returns {synced, missing?, extra?} | ❌ |

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
| 18 | SC-03 Migration script | Parse existing `docs/tickets/*.md`, seed SQLite index | ❌ |

---

## Schema (SQLite indexer — pure frontmatter pointers)

```sql
CREATE TABLE IF NOT EXISTS index_tickets (
    id INTEGER PRIMARY KEY,                    -- matches file's number field
    file_path TEXT NOT NULL UNIQUE,            -- path to .md file
    area TEXT DEFAULT '',                      -- e.g., "infra/tools"
    type TEXT DEFAULT 'feature',               -- feature|bug|infra|pause
    status TEXT DEFAULT 'draft',               -- draft|active|resolved|archived
    title TEXT,                                -- extracted from frontmatter or heading
    objective TEXT,                            -- optional
    created_at DATETIME,
    updated_at DATETIME,
    archived_at DATETIME
);

CREATE INDEX idx_status ON index_tickets(status);
```

Schema notes:
- `id == file number`. No separate PK.
- `file_path` = canonical ref to `.md` on disk.
- Every field is re-extractable from the markdown source → index is rebuildable.
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
    ticketDir: string;          // where .md files live
    indexPath: string;           // path to SQLite index DB
    
    create(params: { area?: string; type?: string; title: string; objective?: string }): Ticket;
    list(area?: string, status?: string): Ticket[];
    update(number: number, patch: Partial<Omit<Ticket, 'id' | 'file_path' | 'number'>>): Ticket | undefined;
    archive(number: number): Ticket | undefined;
    validate(): { synced: boolean; missing_files?: string[]; extra_files?: string[] };
}

export function createTicketEngine({ ticketDir, indexPath }: { ticketDir: string; indexPath: string }): TicketEngine;
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

## Session handoff (2026-10-16)

### Completed today
1. Renamed `little-mester-ticket-engine/` → `/workspace/ticket-engine-mcp/`
2. Created package.json scoped `@little-mester/ticket-engine-mcp`
3. Installed deps: sql.js (pure JS, no native build), @modelcontextprotocol/sdk
4. Set up tsconfig (ES2021, commo
