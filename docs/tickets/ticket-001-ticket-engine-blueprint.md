# Ticket 001 — lightweight JSON-first ticket engine (MCP) [UPDATED]

> LAST UPDATED: 2026-10-19
> All changes below preserve prior work while removing obsolete references.

## Phase tracking

| # | Task | Status | Notes |
|---|------|--------|-------|
| **Phase A: Foundation** | | | |
| 0.1 | Archive old tickets, remove deprecated schema files | ✅ DONE | Archived to `docs/tickets/archive/` (2 files) |
| 0.2 | Create ticket-001 (engine = priority 1) | ✅ DONE | This file |
| 1   | Rewrite docs/workflows.md → JSON-first format + routing modes + conventional commits rule | ✅ DONE | Updated to match SCHEMA.md and blueprint routing modes |
| **Phase B: Engine** | | | |
| 2   | Add phase tracking table here to prevent skipping steps (this table) | ✅ DONE | |
| 3   | Write `ticket-engine-mcp/docs/blueprint.md` — correct routing, remove SQL-related references | ❌ PARTIAL | Blueprint fixed in doc; code changes still needed |
| 4   | Generate proper JSON Schema from SCHEMA.md spec → `_resources/ticket-v1.json` | ❌ TODO next | Drafted in SCHEMA.md; formal .json file pending — see ticket-007 |
| **Phase C: Implementation (UPDATED)** | | | |
| 5   | Write engine.ts core (create/list/update/archive/validate + _index.json routing) | ❌ TODO | See DECISIONS.md Phase 0 tasks. **NOT blocked by lab-dev-work** — agent uses --unsafe-recurse directly now. Syntax errors prevent compiling. |
| 6   | MCP server tools (mesterTickets_{create, list, update, archive}) via `@modelcontextprotocol/sdk` | ❌ TODO | Blocks on Phase C tasks first. See DECISIONS.md ticket-012 |
| 7   | Opencode integration — mcpServers config in opencode.json | ❌ TODO | See DECISIONS.md Phase 5. **NOT blocked by lab-dev-work anymore** |

---

## Architecture (UPDATED — no lab-dev-work)

See `docs/tickets/architecture/DECISIONS.md` for the full updated architecture diagram and workflow documentation. The self-editting flow is now:

```
./lab <workspace> --unsafe-recurse → agent edits workspace directly → bats tests from within session → git commit
```

No temp clone bridge. See DECISIONS.md section A.3.

### Routing Modes

| Mode | Path | Lab override | Notes |
|---|---|---|---|
| Same repo (default) | workspace's `docs/tickets/` | `--override ticket_engine_dir=<custom>` | Tickets go to the workspace, not this repo |
| Any directory | `/any/path/on/disk` | same override | Outside git; user mounts at lab start |
| Separate repo | `/path/to/other-repo/docs/tickets/` | `--override ticket_engine_repo=/path --sync` | Git pull/push/sync ops available if synced |

### JSON Schema (draft v1 — formal .json file pending)

Full spec: `ticket-engine-mcp/docs/SCHEMA.md`. Fields:
- `number` (int, auto-incremented)
- `area` (string) — domain grouping  
- `type` (enum: feature\|bug\|infra\|pause)
- `status` (enum: draft\|active\|resolved\|archived) with transitions: `draft → active → resolved → archived` (+ `resolved → draft`, any → `archived`, `draft ↔ active`)
- `title` (string, yes/required)
- `objective` (string, optional)
- `created_at` (ISO-8601, immutable)
- `updated_at` (ISO-8601, last modification)

## Superseded items

- **lab-dev-work bridge script**: No longer needed. User runs `--unsafe-recurse` to edit workspace directly. See DECISIONS.md section A.3.
- **SQLite**: Removed from architecture per DECISIONS.md section F constraint 1 (JSON-first is a hard rule). Remove SQLite dependency from package.json when fixing build.
