# Ticket T-001 — ticket-engine-mcp

## Objective
Build a **markdown-first** ticketing engine with SQLite as an **indexer/query layer** on top of flat `.md` files, plus an embedded MCP server so opencode can create/list/update/archive tickets natively over stdio. 

Tickets are always `docs/tickets/<file>.md`. SQLite only indexes their frontmatter + status for fast querying. The engine never commits ticket data to any repo.

---

## Status: ACTIVE

## Type: infra — **TODAY/PRIORITY 1**

---

## Architecture decision — Markdown files + SQLite indexer (NOT SQLite storage)

### Why markdown-first
- Tickets ARE markdown files in `docs/tickets/`. Always were, always will be.
- SQLite is the **indexer only** — it reads frontmatter from markdown files and indexes them for fast queries by status, area, type.
- No bloated `.db` file to commit. Each workspace has its own small SQLite file alongside tickets (or in a private location).
- Privacy: engine config routes the indexer DB per-workspace via `defaults.conf`. Personal workflow stays out of company repos.

### Storage routing — 3 modes per workspace
Configured in `workspaces/<repo>/defaults.conf` or overridden at runtime via `./lab`:

| Mode | Description | Config key | Override |
|------|-------------|------------|----------|
| Default | Same repo: `docs/tickets/` | `ticket_engine_dir=docs/tickets` | `--override ticket_engine_dir=<custom>` |
| Private subdirectory | Inside workspace, not committed | `ticket_engine_dir=.tickets` + `.gitignore` the dir | same override |
| Separate git repo | Point to a private repo entirely | `ticket_engine_repo=/path/to/private/repo` | `--override ticket_engine_repo=<path>` — and `./lab sync-tickets --repo <path>` keeps it in sync |

Default routing: engine always defaults to `docs/tickets/`. Override only when needed. The SQLite indexer DB goes wherever the workspace's `.mester-config` or `defaults.conf` says.

### File layout after completion
```
workspace (any — company, personal, etc.)
├── docs/tickets/                    ← ticket markdown files (always here by default)
│   ├── ticket-001-something.md
│   ├── ticket-002-another.md
│   └── _index.json                  ← optional: for backwards compat
├── .mester-config                   ← workspace override file
│   ticket_engine_dir=.tickets       ← or defaults.conf instead
└── tickets.db                       ← SQLite index (auto-created, never manual)
```

---

## Acceptance Criteria (tests verify each)

### Functional ACs
- [ ] **AC-01 Create ticket**
  - `engine.create(area="", type="feature", title="...")` → writes `docs/tickets/ticket-NNN-title.md`
  - SQLite index is updated automatically after file creation
  - Returns `{ id, number, area, type, status: "draft", title, objective }`
  
- [ ] **AC-02 List tickets**
  - `engine.list(area?, status?)` → queries SQLite index (NOT filesystem scan)
  - Results match actual markdown files on disk — validation always passes
  
- [ ] **AC-03 Update ticket**
  - `engine.update(number, { status: "resolved" })` → edits frontmatter in markdown file + updates index
  - Returns updated record
  - Unknown number → returns undefined (checked by caller)

- [ ] **AC-04 Archive ticket**
  - `engine.archive(number)` → sets `status: archived`, writes `_archived_at` to frontmatter
  
- [ ] **AC-05 Storage mode: default (docs/tickets/)**
  - When no override, stores/indexes in workspace's `docs/tickets/`

- [ ] **AC-06 Storage mode: override**
  - When config overrides (e.g., `.tickets/` or separate repo path), all CRUD targets that location
  
### MCP Server ACs
- [ ] **MC-01** MCP server starts on stdio transport (not HTTP — opencode uses stdio)
- [ ] **MC-02** exposes tool `mesterTickets_create(area, type, title, objective?)` → creates ticket markdown file
- [ ] **MC-03** exposes tool `mesterTickets_list(area?, status?)` → lists from SQLite index with optional filters
- [ ] **MC-04** exposes tool `mesterTickets_update(number, field, value)` → updates frontmatter in markdown + re-indexes
- [ ] **MC-05** exposes tool `mesterTickets_archive(number)` → archives a ticket
- [ ] **MC-06** opencode can call all 4 tools and see correct results

### Testing ACs (TDD)
- [ ] **TC-01** Engine tests — unit tests covering AC-01 through AC-06
- [ ] **TC-02** MCP tool tests — mock transport for all 4 tools  
- [ ] **TC-03** Integration test — end-to-end: create → list → update → archive (all operations)

### Script ACs  
- [ ] **SC-01** `scripts/ticket-engine` CLI replaces/supersedes flat-file `ticket-new`/`validate-tickets`
- [ ] **SC-02** Resolves storage mode from workspace config / env override
- [ ] **SC-03** Backwards compat: `ticket-new` and `validate-tickets` still work during migration

---

## File-by-file blueprint (what to implement)

### 1. Directory structure
```
/workspace/ticket-engine-mcp/              ← new (already created by rename)
├── src/
│   ├── engine.ts                           ← core library: markdown CRUD + SQLite indexer
│   ├── cli.ts                              ← CLI entry point (replaces ./scripts/ticket-new, etc.)
│   └── mcp-server.ts                       ← MCP server via stdio transport
├── tests/
│   ├── engine.test.ts                      ← AC-01 through AC-06 (TDD)
│   └── mcp.test.ts                         ← MC-01 through MC-06 (mock transport)
└── package.json
```

### 2. engine.ts — exact interface & implementation notes
```typescript
// Engine always reads/write markdown files. SQLite is ONLY the query index layer.
import * as fs from 'fs';
import * as path from 'path';
import Database from 'better-sqlite3';

export interface TicketDoc {
    number: number;          // extracted or assigned (auto-increment)
    area: string;            // e.g., "infra/tools" — empty for top-level tickets
    type: string;            // feature|bug|infra|pause|docs
    status: string;          // draft|active|resolved|archived|pending-review
    title: string;           // from frontmatter or markdown heading
    objective?: string;      // one sentence summary
    created_at: string;
    updated_at: string;
}

export interface TicketEngine {
    /** Where ticket markdown files live */
    ticketDir: string;
    
    /** Path to the SQLite index DB for this workspace */
    indexPath: string;
    /** The underlying db connection (use for tests, close on dispose) */
    db: Database.Database;

    /** Create a new ticket. Returns doc + writes markdown file + indexes it in SQLite */
    create({ area='', type='feature', title }: { area?: string; type?: string; title: string }): TicketDoc;

    /** List tickets from the SQLite index (fast) — supports optional filters */
    list(area?: string, status?: string): TicketDoc[];

    /** Update a ticket's frontmatter + re-index in SQLite */
    update(number: number, patch: Partial<Omit<TicketDoc, 'number'>>): TicketDoc | undefined;

    /** Archive a ticket (set status=archived) */
    archive(number: number): TicketDoc | undefined;

    /** Validate index matches filesystem — re-sync if drifted */
    validate(): { synced: boolean; missing?: string[]; extra?: string[] };
}

// Key implementation details for tomorrow:

// 1. Markdown file format — frontmatter YAML:
// ---
// number: 42
// area: "infra/tools" 
// type: feature
// status: active
// objective: "This sentence describes what the ticket should achieve."
// created_at: "2026-10-15T..."
// updated_at: "2026-10-16T..."
// ---
// # Ticket 42 — Title Here
// ...rest of body...

// 2. Indexing approach (engine.ts init()):
//    - Ensure SQLite DB at indexPath for this workspace
//    - Scan ticketDir/*.md files, extract frontmatter via yaml.parse()  
//    - INSERT or UPDATE rows in tickets table
//    - On every modify/create/archive operation: update that row

// 3. auto-increment logic per area:
//    SELECT MAX(CAST(SUBSTR(filename, 8) AS INTEGER)) FROM ... 
//    OR query SQLite index: SELECT MAX(number) where area=?.next = max + 1

// 4. Index validate/repair:
//    - List files on disk → list rows in SQLite
//    - Delete orphaned rows (row exists, file gone)
//    - Insert missing rows (file exists, no row)
//    - Return { synced: true/false } — used by `validate-tickets` replacement
```

### 3. cli.ts — command dispatching skeleton
```typescript
export function resolveTicketDir(): string { ... }   // check config → default docs/tickets/
export function resolveIndexPath(ticketDir: string): string { ... }  // <ticketDir>/.. index db path
export function resolveStorageMode(): { mode: 'default' | 'override' | 'repo'; path: string; repo?: string } { ... }

// CLI subcommands (same API shape as today but delegates to engine):
// ticket-engine create --area X --type feature "Title"
//   [--objective "..."]  
// ticket-engine list [--area X] [--status active|archived]  
// ticket-engine update <number> --field status --value resolved
// ticket-engine archive <number>
```

### 4. mcp-server.ts — MCP tool registration skeleton
```typescript
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio';
import { createEngine, type TicketEngine } from './engine';

const server = new McpServer({ name: 'ticket-engine-mcp', version: '0.1.0' });

// 4 tools — each registers with parameters + description in MCP spec format
server.registerTool('mesterTickets_list', { 
    description: 'List tickets, optionally filtered by area and/or status.', 
    inputSchema: { type: 'object', properties: { area: { ... }, status: { ... } } } 
}, handler);

server.registerTool('mesterTickets_create', { ... }, createHandler);
server.registerTool('mesterTickets_update', { ... }, updateHandler);
server.registerTool('mesterTickets_archive', { ... }, archiveHandler);


```
