# Little Mester MCP — Server Architecture

> **DECISION LOG:** All engineering decisions spanning all engine tickets are in [docs/tickets/architecture/DECISIONS.md](../../../docs/tickets/architecture/DECISIONS.md). Product usage constraints are in [docs/DECISIONS.md](../../docs/DECISIONS.md). Read both — new sessions MUST read the decision files first.

## What is Little Mester MCP?

`@little-mester/ticket-engine-mcp` is the first module of **Little Mester MCP** — a generic MCP server designed to be extensible. The ticketing engine is the first implementation module. Future modules (file search, notes, etc.) will live alongside in the same server.

### Tool naming convention

All MCP tools follow `mester_<module>_<action>`:

| Module | Tool prefix | Examples |
|--------|------------|----------|
| `tickets` (current) | `mester_tickets_*` | `mester_tickets_create`, `mester_tickets_search`, `mester_tickets_board` |
| `files` (future) | `mester_files_*` | `mester_files_search`, `mester_files_read` |
| `notes` (future) | `mester_notes_*` | `mester_notes_create`, `mester_notes_list` |

---

## What to implement (ordered by priority)

### Phase 0: Make the engine compile
1. Fix 5 syntax errors in `engine.ts`
2. Remove stale SQLite dep from `package.json`
3. Get `npm run build` + `npm test` green

### Phase 1: Ticket migration + schema enforcement
4. Generate formal `ticket-v1.json` (JSON Schema) from SCHEMA.md
5. Write migration script — convert ALL `.md` tickets to JSON
6. Add schema validation to engine on every write

### Phase 2: Search, dependencies, epics, labels
7. Dependency graph (transitive blocking chains)
8. Epics as REAL tickets with lifecycle
9. Enum label system (predefined sets + user/custom namespace)
10. Full multi-field search API

### Phase 3: Visualization-ready outputs
11. Board view, dashboard stats, export (JSON/CSV/markdown-table)

### Phase 4: MCP server + opencode integration
12. Server skeleton — all tools registered with descriptions
13. `opencode.json` mcpServers config
14. Deprecation warnings in legacy scripts

### Phase 5: External deployment
15. Remote routing protocol (co-located + separate repo modes)
16. Git sync engine with 3 conflict strategies
17. MCP tools for sync operations

### Phase 6: Documentation cleanup
18. README, workflows.md, SECURITY.md — all updated per session answers

---

## Routing Modes (all three supported)

| Mode | Config key | Override via lab |
|------|------------|------------------|
| Same repo (default) | `ticket_engine_dir=docs/tickets` | `--override ticket_engine_dir=<custom>` |
| Any directory | `ticket_engine_dir=/any/path/on/disk` | same override |
| Separate repo | `ticket_engine_repo=<path>` | `--override ticket_engine_repo=<path> --sync` |

---

## MCP Tools (ticketing module)

All tools are registered under the `mester_tickets_` prefix:

| Tool | Description |
|------|-------------|
| `mester_tickets_create` | Create a new ticket with area, type, title, objective |
| `mester_tickets_list` | List tickets with optional area/status/priority filters |
| `mester_tickets_get` | Get a single ticket by number |
| `mester_tickets_update` | Update ticket fields (status transitions enforced) |
| `mester_tickets_archive` | Archive a ticket (removes from active cycle) |
| `mester_tickets_search` | Multi-field search across tickets |
| `mester_tickets_board` | Board view — sorted by priority then updated_at |
| `mester_tickets_validate` | Check index/files sync state |

---

## Ticket JSON Schema

Full field spec in [docs/tickets/architecture/DECISIONS.md](../../../docs/tickets/architecture/DECISIONS.md) — see label enum sets, estimate fields, epic design.

Schema version file: `ticket-v1.json` (generated from this plan).
