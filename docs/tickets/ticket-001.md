# Ticket T-001 — little-mester-ticket-engine (SQLite + MCP server)

## Objective
Build an embedded, local-first ticketing engine inside little-mester that stores tickets in SQLite AND surfaces an MCP server for opencode to use natively. Each workspace points the engine at a database path (local directory or separate repo), so personal workflow tickets stay private from company repos.

---

## Status: ACTIVE

## Type: infra

## Scope & Boundaries

Do not modify files outside this project directory.
Do not introduce dependencies beyond what is documented.

### Files in scope

- `little-mester-ticket-engine/` — the engine library (SQLite backend)
- `little-mester-ticket-engine/mcp-server.ts` — MCP server wrapper
- `scripts/ticket-engine` — CLI to run against a workspace
- `docs/` — any future doc files

### Steps

1. Design SQLite schema: `tickets` table (id, number, area, type, status, title, objective, created/updated_at), `comments` table for notes
2. Implement engine as TypeScript library in `little-mester-ticket-engine/` with functions: create_ticket(), update_ticket(), list_tickets(), archive_ticket()
3. Create MCP server entry point in `mcp-server.ts` exposing list/create/update/archive tools
4. Add CLI wrapper in `scripts/ticket-engine` that resolves DB path from workspace config (--from-workspace-dir or --from-repo)
5. Default: store against working directory (e.g., `<workspace>/ticket-engine.db`)
6. Override: point to separate repo (env/little-mester.conf: `TICKET_ENGINE_DB=/path/to/separate/db.sqlite`)

### Verification

Verify the engine works by running:
```bash
cd little-mester-ticket-engine && npx tsc --noEmit
```

Expected: no TypeScript errors, tests pass.

## Files

| File | Role |
|------|------|
| `little-mester-ticket-engine/schema.sql` | Database schema |
| `little-mester-ticket-engine/engine.ts` | Core ticket engine (SQLite CRUD) |
| `little-mester-ticket-engine/mcp-server.ts` | MCP server tools for opencode |
| `scripts/ticket-engine` | CLI wrapper to run engine against workspace |

## Dependencies

(none yet)
