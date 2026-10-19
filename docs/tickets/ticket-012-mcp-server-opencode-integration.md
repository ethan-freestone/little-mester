# Ticket 012 — MCP Server tools + Opencode config integration + deprecation of legacy scripts

## Objective

Wire all engine methods into a runnable MCP server with proper tool descriptions, then configure opencode to load it. After the engine is complete (ticket-010 and ticket-011), this ticket registers everything as MCP endpoints and hooks into opencode so you can manage tickets from within agent sessions.

## Context (from DECISIONS.md)

### C.4 — JSON-only output
MCP server outputs structured JSON. A future visualization ticket handles human-readable rendering in opencode. No markdown tables in MCP responses.

### Routing modes (all three as specified in blueprint)
| Mode | Description | Config key | Override via lab |
|---|---|---|---|
| **Same repo (default)** | `docs/tickets/` inside workspace you're pointing at | `ticket_engine_dir=docs/tickets` | `--override ticket_engine_dir=<custom>` |
| **Any directory** | Anywhere on disk, even outside git repo or in a mounted path | `ticket_engine_dir=/any/path/on/disk` | same override |
| **Separate repo** | Different git repo; syncable via lab flag | `ticket_engine_repo=/path/to/repo` | `--override ticket_engine_repo=<path>` + enable sync mode |

"Default" = mode 1, but tickets always go to the workspace's `docs/tickets/`, not our repo.
- Mode 2 is for private/untracked work; user manages where it lives and how it mounts.
- Mode 3 adds git ops (pull/push/sync) — optional flag-gated.

---

## Phase Tracking

| # | Task | Status | Notes |
|---|------|--------|-------|
| 12.1 | Create MCP server skeleton (`mcp-server.ts`) with stdio transport | ❌ TODO | Registers all tool handlers, writes to stdout/stderr for MCP stdio protocol |
| 12.2 | Register ALL mesterTickets_* endpoints (25+ tools) | ❌ TODO | See Phase A below |
| 12.3 | Write clear natural-language descriptions for each MCP tool (visible in opencode) | ❌ TODO | Agent must understand what each tool does to invoke it correctly |
| 12.4 | Add `mcpServers` config to `agent/opencode/opencode.json` | ❌ TODO | See Phase D |
| 12.5 | Update legacy scripts (`ticket-new`, `ticket-lifecycle`, `validate-tickets`) with deprecation hints | ❌ TODO | Scripts remain functional but warn users about MCP engine as recommended path forward |

---

## All MCP Tools

Based on all phases: create (008), dependencies/epics/search (010), board/dashboard/export (011).

### Ticket CRUD

| Tool Name | Input Schema | Description |
|---|---|---|
| `mesterTickets_create` | `{ area: string, title: string, type?: enum[feature\|bug\|infra\|pause\|spike], objective?: string, labels?: string[], priority?: enum[trivial\|minor\|major\|critical], estimate?: number, expected_model?: string, parent_epic?: number }` | Create a new ticket with auto-incremented number in the `draft` status. Returns full ticket JSON. Use when you want to start working on something — specify area for domain grouping and any labels from the predefined sets (type/*/severity/*/scope/*) |
| `mesterTickets_update` | `{ number: integer, patch: object }` | Partial update of an existing ticket. Pass only the fields you want to change in `patch`. Status transitions are enforced — you can't jump directly to a status that isn't valid from the current one (e.g., resolved → active NOT allowed). Returns updated ticket JSON. |
| `mesterTickets_archive` | `{ number: integer }` | Archive a ticket (terminal state). The ticket becomes read-only from this point. Only callable if status != 'archived' already. Returns archived ticket with updated_at timestamp. |
| `mesterTickets_get` | `{ number: integer }` | Get the raw full ticket JSON by number. Use when you need to inspect all fields of a specific ticket including optional ones (labels, estimate, dependencies, etc.) |

### Search & Filtering

| Tool Name | Input Schema | Description |
|---|---|---|
| `mesterTickets_list` | `{ area?: string, status?: string, limit?: integer }` | List tickets with simple area/status filters. Faster than search for basic queries. |
| `mesterTickets_search` | `{ q?: string, area: string, type?: enum, status?: enum, labels?: string[], severity?: enum[trivial\|minor\|major\|critical], scope?: enum, parent_epic?: integer, has_dependencies?: boolean, sort_by?: string, sort_order?: 'asc'\|'desc', limit?: integer, offset?: integer }` | Full multi-field search across all ticket attributes. Combine any filters freely (AND within category, OR between). Use `q` for free-text keyword search in title/objective. Use `has_dependencies: false` with status=active to find ready-to-start work. Returns array of matching tickets. |
| `mesterTickets_extractTicketRefs` | `{ text: string }` | Extract all ticket number references from a text string (commit messages, comments). Matches #N, #042, refs #N patterns. Useful for auto-linking or cross-referencing. Returns array of numbers found. |

### Dependencies + Epics

| Tool Name | Input Schema | Description |
|---|---|---|
| `mesterTickets_linkDependency` | `{ parent_ticket: integer, child_ticket: integer }` | Make the child ticket depend on the parent. Parent must be resolved before child can transition to active. Returns both tickets with updated dependency fields. |
| `mesterTickets_unlinkDependency` | `{ parent_ticket: integer, child_ticket: integer }` | Remove a dependency link between two tickets. |
| `mesterTickets_getBlockedByChain` | `{ ticket_number: integer }` | Get the full transitive blocking chain for a ticket — what's transitively preventing it from moving forward. Returns array of blocking ticket numbers and their statuses. |
| `mesterTickets_canTransition` | `{ ticket_number: integer, new_status: enum }` | Check if transitioning this ticket to the given status is valid, considering dependency guards. Returns `{ allowed: boolean, reason?: string, blocking_tickets?: number[] }`. Use before attempting a transition to avoid errors. |
| `mesterTickets_createEpic` | `{ title: string, area?: string, objective?: string }` | Create an epic ticket as a real ticket with its own lifecycle. Returns the new epic's tickket number for linking subsequent child tickets. |
| `mesterTickets_addChildToEpic` | `{ epic_number: integer, child_ticket: integer }` | Link a regular ticket as a child of an epic. The child appears in all epic visualizations and statistics. |
| `mesterTickets_getEpicsSummary` | `{ epic_number?: integer }` | Get summary stats for all epics (or one specific). Returns array with count, resolved, percentage complete per epic. Pass no arg to get all; pass a number for single epic detail. |

### Board + Dashboard

| Tool Name | Input Schema | Description |
|---|---|---|
| `mesterTickets_board` | `{ area: string }` | Get tickets grouped by status (kanban columns). Returns JSON object where keys are statuses and values are arrays of full ticket objects. For structured display in opencode clients. |
| `mesterTickets_dashboard` | `{ area?: string }` | Aggregated project health summary. Single call returns: statusCounts, resolutionRate, aging tickets (>5 days), epic summaries, estimate stats. Pass `area` to filter to a single domain. |
| `mesterTickets_agingReport` | `{ threshold_days: integer }` | Get tickets unchanged beyond N days (stalled work). Useful for reviewing and re-activating or archiving orphaned work. |

### Export

| Tool Name | Input Schema | Description |
|---|---|---|
| `mesterTickets_export` | `{ format?: enum[json\|csv\|markdown-table], area?: string, status?: string }` | Export tickets in requested format. Json: full structured data. Csv: tab-separated for spreadsheets. Markdown-table: display-friendly table for terminal/IDE context. Default format is json. |

## Phase D: Opencode config integration

Add to `agent/opencode/opencode.json`:

```json
{
  "mcpServers": {
    "ticket-engine": {
      "command": "npx",
      "args": ["ts-node", "src/mcp-server.ts"],
      "cwd": "/workspace/ticket-engine-mcp"
    }
  }
}
```

Alternatively with npm run server:
```json
"mcpServers": {
  "little-mester-ticket-engine": {
    "command": "npm",
    "args": ["run", "server"],
    "cwd": "/workspace/ticket-engine-mcp"
  }
}
```

## Phase E: Deprecation of legacy scripts

For each legacy script in `scripts/`, add a deprecation notice at the top:

```bash
# NOTE: This script is deprecated. Use the ticket-engine MCP tools instead.
#   npx ts-node src/mcp-server.ts --tool mesterTickets_create ...
# This script remains functional for backward compatibility only.
# Remove in v2.0 release.
```

Scripts to update:
- `ticket-new` → replaced by `mesterTickets_create`
- `ticket-lifecycle` → partially replaced by `mesterTickets_update` + `mesterTickets_archive`  
- `validate-tickets` → replaced by engine's built-in validate + MCP tool integration

## Files modified/created

| File | Action |
|---|---|
| `ticket-engine-mcp/src/mcp-server.ts` | Created: MCP server skeleton with all tools registered |
| `agent/opencode/opencode.json` | Modified: add mcpServers section pointing to engine |
| `scripts/ticket-new` | Modified: deprecation warning |
| `scripts/ticket-lifecycle` | Modified: deprecation warning |
| `scripts/validate-tickets` | Modified: deprecation warning |

## Verification

```bash
# Start the MCP server manually to verify tools register correctly
cd ticket-engine-mcp && npx ts-node src/mcp-server.ts

# In a separate session, connect opencode — it should auto-load the ticket-engine MCP server
# Once loaded, you can query tickets from within any opencode session without leaving the IDE
```

## Dependencies

- Requires: 008 (engine compiles), 010 (search/dependencies/epics complete), 011 (board/dashboard/export complete)
