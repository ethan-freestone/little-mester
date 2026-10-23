# little-mester — Ticketing System Engineering Plan

> **DECISION LOG:** All engineering decisions spanning all engine tickets are in [docs/tickets/engine-plan.md](../../../docs/tickets/engine-plan.md). Product usage constraints are in [docs/DECISIONS.md](../../docs/DECISIONS.md). Read both — new sessions MUST read the decision files first.

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

## Ticket JSON Schema

Full field spec in [docs/tickets/engine-plan.md](../../../docs/tickets/engine-plan.md) — see label enum sets, estimate fields, epic design.

Schema version file: `ticket-v1.json` (generated from this plan).
