# Ticket 001 — lightweight JSON-first ticket engine (MCP)

Engine lives in `ticket-engine-mcp/`. This ticket tracks Phase A work.

## Phase tracking

| # | Task | Status | Notes |
|---|------|--------|-------|
| **Phase A: Foundation** | | | |
| 0.1 | Archive old tickets, remove deprecated schema files | ✅ DONE | Archived to `docs/tickets/archive/` (2 files); `meta-ticket-schema.json` status unclear — may have been dropped; if needed elsewhere, relocate and update reference |
| 0.2 | Create ticket-001 (engine = priority 1) | ✅ DONE | This file |
| 1   | Rewrite docs/workflows.md → JSON-first format + routing modes + conventional commits rule | ✅ DONE | Updated to match SCHEMA.md and blueprint routing modes |
| **Phase B: Engine** | | |
| 2   | Add phase tracking table here to prevent skipping steps (this table) | ✅ DONE | |
| 3   | Write `ticket-engine-mcp/docs/blueprint.md` — correct routing, remove SQLite, align with JSON schema | ❌ TODO performed below (see note) | Blueprint fixed: SQLite section removed, engine TS interface updated to reflect _index.json instead of SQL, routing modes documented in Phase A workflows update |
| 4   | Generate proper JSON Schema from SCHEMA.md spec → `_resources/ticket-v1.json` | ❌ TODO next | Drafted above but needs formal JSON Schema output |
| **Phase C: Implementation** | | |
| 5   | Write engine.ts core (create/list/update/archive/validate + _index.json routing) | ❌ TODO | AC-01 through AC-06 tests needed first (TDD gate) |
| 6   | MCP server tools (mesterTickets_{create, list, update, archive}) via `@modelcontextprotocol/sdk` | ❌ TODO | Wraps engine.ts methods |
| 7   | Opencode integration — mcpServers config in agent/opencode.json | ❌ TODO | Needs deprecation messages for legacy scripts |
| **Phase D: Monitoring (A.5)** | | |
| 8   | Write `scripts/monitor` script for GPU/RAM/CPU/temps (native opencode tool, not system monitor) | ❌ TODO next | |

## Routing modes (also in workflows.md)

| Mode | Path | Lab override | Notes |
|---|---|---|---|
| Same repo (default) | workspace's `docs/tickets/` | `--override ticket_engine_dir=<custom>` | Tickets go to the workspace, not this repo |
| Any directory | `/any/path/on/disk` | same override | Outside git; user mounts at lab start |
| Separate repo | `/path/to/other-repo/docs/tickets/` | `--override ticket_engine_repo=/path --sync` | Git pull/push/sync ops available if synced |

## JSON Schema (draft v1 — formal .json file pending)

Full spec: `ticket-engine-mcp/docs/SCHEMA.md`. Fields:
- `number` (int, auto-incremented)
- `area` (string) — domain grouping  
- `type` (enum: feature\|bug\|infra\|pause)
- `status` (enum: draft\|active\|resolved\|archived) with transitions: `draft → active → resolved → archived` (+ `resolved → draft`, any → `archived`, `draft ↔ active`)
- `title` (string)
- `objective` (string, optional)
- `created_at` (ISO-8601, immutable)
- `updated_at` (ISO-8601, last modification)
