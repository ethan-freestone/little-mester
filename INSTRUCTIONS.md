# Project Instructions

## Ticket Engine — Engine-Only Writes

**ALL ticket writes must go through the `little-mester` MCP server tools.** Never use the `edit`, `write`, or any file-system tool to directly modify files in `docs/tickets/`.

### Why?
The ticket engine maintains an audit log, lock enforcement, index sync, and orphan detection. Direct file edits bypass all of this, creating drift and un-audited changes.

### Available MCP Tools (server: `little-mester`)

| Tool | Use |
|---|---|
| `tools["little-mester"].mester_tickets_create` | Create a new ticket |
| `tools["little-mester"].mester_tickets_list` | List tickets (filterable) |
| `tools["little-mester"].mester_tickets_get` | Get a single ticket by number |
| `tools["little-mester"].mester_tickets_update` | Update ticket fields |
| `tools["little-mester"].mester_tickets_archive` | Archive a ticket |
| `tools["little-mester"].mester_tickets_search` | Multi-criteria search |
| `tools["little-mester"].mester_tickets_board` | Kanban board view |
| `tools["little-mester"].mester_tickets_validate` | Validate the ticket store |

### Rules

1. **CREATE** a new ticket idea → use `mester_tickets_create`
2. **UPDATE** an existing ticket → use `mester_tickets_update`
3. **ARCHIVE** a completed ticket → use `mester_tickets_archive`
4. **VIEW** tickets → use `mester_tickets_list`, `mester_tickets_search`, or `mester_tickets_get`
5. **NEVER** call `edit`, `write`, or any file tool on `docs/tickets/*.json`

If a tool fails or is unavailable, report the error — do not fall back to direct file edits.
