# Ticket 011 — Board views, dashboards, statistics + export formatting

## Objective

Add aggregate queries and visualization-ready outputs to the engine so you can inspect project health at a glance. MCP tools return structured JSON for opencode client rendering (future ticket will add human-facing UI).

## Context (from DECISIONS.md)

### C.4 — Board view / MCP output: JSON only
MCP engines must return structured data only. A future ticket adds visualizations for direct human interaction in opencode. The agent uses engine outputs programmatically within its sessions.

### C.5 — Migration timing of existing MD tickets
Existing `.md` tickets will eventually migrate but are kept as-is while building the system. This ticket's queries work on ANY `.json` files in the ticket directory regardless of migration status.

### C.7 — Model estimation included
`estimate` and `estimated_model` fields from the extended schema should be used in statistics (average estimate per area, tickets with/without estimates, etc.).

---

## Phase Tracking

| # | Task | Status | Notes |
|---|------|--------|-------|
| 11.1 | `getStatusCounts(area?)` — counts per status, optionally filtered by area | ❌ TODO | Returns `Record<string, number>` |
| 11.2 | `getTicketsByArea()` — grouping and counting all tickets by area | ❌ TODO | Aggregation of all tickets across statuses |
| 11.3 | `getResolutionRate(area?)` — {resolved, total, rate}% | ❌ TODO | Rate is resolved/total * 100, rounded to 2 decimals |
| 11.4 | `getAgingTickets(thresholdDays)` — tickets unchanged > threshold days | ❌ TODO | Sorts by last updated_at; useful for identifying stalled work |
| 11.5 | `getEpicSummary(epicNum?)` or `getEpicsSummary()` / all epics | ❌ TODO | Per-epic: count, resolved, percentage; if epicNum provided, single summary |
| 11.6 | Board view method — tickets grouped by status columns | ❌ TODO | See Phase D below |
| 11.7 | `exportTickets(filter?, format?)` — JSON/CSV/markdown-table export | ❌ TODO | Format enum: json, csv, markdown-table |
| 11.8 | MCP tools for all above dashboard/board/export endpoints | ❌ TODO | See Phase E |

---

### Phase A: Aggregate query methods

All methods operate on the engine's ticket directory/index — they're read-only queries that don't modify state:

```typescript
// Counts by status, optionally filtered to a single area
getStatusCounts(area?: string): Record<string, number>;
// Example return: { draft: 5, active: 3, resolved: 12, archived: 8 }

// Group all tickets by area (not just counts)
getTicketsByArea(): Record<string, { total: number; byStatus: Record<string, number> }>;
// Example return: 
// { auth: { total: 15, byStatus: { draft: 3, active: 2, resolved: 8, archived: 2 } }, ... }

// Resolution rate for an area (or all areas if no filter)
getResolutionRate(area?: string): Record<string, { resolved: number; total: number; rate: number }>;
// Example return: { auth: { resolved: 8, total: 15, rate: 53.33 } }

// Aging report — tickets unchanged beyond threshold days
getAgingTickets(thresholdDays: number): Array<{
    num: number;
    title: string;
    area: string;
    status: string;
    lastUpdated: string;
    ageDays: number;
}>.sort_by_last_update_desc();

// Estimates summary — useful for capacity planning
getEstimateSummary(area?: string): {
    totalEstimatedHours: number;
    ticketsWithEstimates: number;
    ticketsWithoutEstimates: number;
    averageEstimate: number;
}
```

### Phase B: Epic statistics (uses Phase C of ticket-010 epic methods)

```typescript
// Single epic summary
getEpicSummary(epicNum: number): {
    epicNum: number;
    title: string;
    total: number;
    resolved: number;
    active: number;
    draft: number;
    archived: number;
    percentageComplete: number;  // rounded to 2 decimals
    children: Array<{
        num: number;
        title: string;
        status: string;
        estimated_hours?: number;
    }>;
};

// All epics summary
getEpicsSummary(): Array<{epicNum, title, total, resolved, percentageComplete}>;

// Area-wide statistics (epics + tickets combined)
getAreaDashboard(area: string): {
    ticketCounts: Record<string, number>;      // status → count
    epicCounts: Record<string, number>;         // epic → ticket count under it
    resolutionRate: number;                     // overall area rate
    agingTickets: Array<{...}>;                 // stale work
    estimateSummary: { totalHours, withEst, withoutEst, avg };
};
```

### Phase C: Board view method (kanban columns)

The board groups tickets by status for display as columns:

```typescript
getBoardView(): Record<string, Ticket[]>;
// Key type is literal union of status values: 'draft' | 'active' | 'resolved' | 'archived'
// Each value is the list of full ticket objects in that column/state
// Optionally filters by area if specified in a wrapper method withAreaBoardView(area)
```

**Output format:** JSON object where keys are status labels and values are arrays of full Ticket objects. The client (opencode) renders this however it wants — for now, engine just returns the data structure. Future visualization ticket handles human-readable board rendering.

### Phase D: Export method

```typescript
exportTickets(filter?: { area?: string; status?: string }, format?: 'json' | 'csv' | 'markdown-table'): string | Buffer
```

**Format options:**
- `json` (default): Full structured JSON array of tickets with all fields
- `csv`: Tab-separated values suitable for spreadsheet import. Columns: number, area, type, status, title, objective, created_at, updated_at, severity labels
- `markdown-table`: Formatted markdown table for display in terminal/IDE context during sessions

**CSV header row:**
```
number,area,type,status,title,objective,created_at,updated_at
42,auth,feature,active,Implement OAuth2,"Users can register via Google","2026-10-15T09:00Z","2026-10-16T14:30Z"
```

**markdown-table output** example (truncated for display):
```
| # | Area   | Type     | Status    | Title                  | Created              | Updated              |
|---|--------|----------|-----------|------------------------|----------------------|----------------------|
| 42 | auth  | feature  | active    | Implement OAuth2       | 2026-10-15T09:00Z    | 2026-10-16T14:30Z    |
| 17 | engine | bug     | resolved  | Fix index corruption   | 2026-10-12T11:00Z    | 2026-10-14T16:00Z    |
```

### Phase E: MCP tools for all above

| Tool Name | Inputs | Output Description |
|---|---|---|
| `mesterTickets_statusCounts(area?)` | Optional `area` string | JSON `{draft: N, active: N, resolved: N, archived: N}` |
| `mesterTickets_ticketsByArea()` | none | JSON `{auth: {total: N, byStatus: {...}}, engine: {...}}` |
| `mesterTickets_resolutionRate(area?)` | Optional `area` string | JSON `{resolved: N, total: N, rate: N.N}` |
| `mesterTickets_aging(thresholdDays)` | Required thresholdDays | JSON array of aging tickets with title, area, status, ageDays |
| `mesterTickets_board(area?)` | Optional `area` string to filter by area only | JSON column structure (same as boardView) |
| `mesterTickets_epicSummary(epicNum?)` | Optional epic number or omit for all | Single summary object OR array of summaries |
| `mesterTickets_export(format?, area?, status?)` | format enum + optional filters | Exported data in requested format (string/Buffer) |

### Phase F: Dashboard aggregation tool (convenience single call)

Tool: `mesterTickets_dashboard(area?)` — runs all available stats for a single response:
```json
{
    "area": "all",
    "statusCounts": { "draft": 5, "active": 3, "resolved": 12, "archived": 8 },
    "resolutionRate": 60.0,
    "agingTickets": [...],
    "epicSummaries": [...],
    "estimateSummary": { "totalHours": 42, "ticketsWithEstimates": 15, ... }
}
```

## Files modified/created

| File | Action |
|---|---|
| `ticket-engine-mcp/src/engine.ts` | Add all aggregate query methods + Board view |
| `ticket-engine-mcp/src/exporters.ts` (new) | CSV and markdown-table export formatters |
| `ticket-engine-mcp/src/mcp-server.ts` | Register dashboard/board/export tools (when Phase 4 is implemented) |
| `ticket-engine-mcp/docs/ticket-v1.json` | Add `estimate` and `estimated_model` to schema if not already there (from ticket-010) |

## Verification

```bash
# Build and test
cd ticket-engine-mcp && npm run build && npm test

# Run MCP tools from agent session:
# 1. mesterTickets_dashboard → get full project health
# 2. mesterTickets_board → see column layout
# 3. mesterTickets_export('csv') → downloadable CSV of all tickets
```
