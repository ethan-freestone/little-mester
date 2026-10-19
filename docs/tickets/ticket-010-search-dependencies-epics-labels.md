# Ticket 010 — Multi-field search, ticket dependencies, epics (real tickets), and label system

## Objective

Implement the engine's query layer, relationship graph, and epic tracking. This turns the engine from a simple CRUD store into a JIRA-equivalent that supports sequential/parallel ticket execution chains, cross-ticket referencing, and structured organization through real epics + enum labels.

## Context (from DECISIONS.md)

### C.6 — Execution model: SEQUENTIAL and PARALLEL
- **Sequential chains:** A depends on B which depends on C → linear blocking. Modeled via `dependencies[]` + `blocked_by[]` with transitive chain traversal. An earlier ticket must be resolved before later ones can proceed (status transition guard).
- **Parallel branches:** Group of tickets in the same epic that share no direct dependencies OR cross-references where tickets mention each other without a formal dependency link. Both valid; engine tracks them differently.
- **Cross-ticket referencing:** Agents use conventional commit-style tags (`#N`, `refs #N`) in commit messages. Engine recognizes these patterns for future auto-linking or graph display.

### C.1 — Epics are REAL tickets
- An epic is a ticket with its own lifecycle (draft → active → resolved + archived)
- Child tickets have `parent: <epic_number>` pointing to it
- Epics inherit status from children (resolved only when all children resolved, or explicit override)
- Must be queryable independently and dependable by other tickets

### C.3 — Labels are enum arrays
Predefined sets with user/ namespace for custom:
| Set | Values | Purpose |
|---|---|---|
| **type** | `spike`, `feature`, `bug`, `improvement`, `doc`, `infra` | Classifies kind of work (replaces/extends ticket.type) |
| **severity** | `trivial`, `minor`, `major`, `critical` | Urgency/blocking level for triage |
| **scope** | `agent`, `engine`, `infra`, `docs`, `tests`, `security`, `performance` | What part of the system this touches |

Custom labels use `user/` prefix to avoid collisions.

### C.7 — Model estimation (stretch included)
- `estimate` field stores effort (hours or story points)
- Optional `estimated_model` field for model-awareness: which local model is projected most efficient for the task
- Every ticket should have a SPIKE sub-ticket for thorough documentation/research before active work

---

## Phase Tracking

| # | Task | Status | Notes |
|---|------|--------|-------|
| 10.1 | Add extended schema fields to `Ticket` interface + JSON Schema: labels, severity, scope, estimate, estimated_model, epics (real ticket type) | ❌ TODO | Update engine.ts, SCHEMA.md, ticket-v1.json |
| 10.2 | Implement label validation (enum prefix sets + user/ custom) | ❌ TODO | On create/update: validate each label against predefined + prefix rules |
| 10.3 | Implement dependency graph operations | ❌ TODO | See Phase A |
| 10.4 | Implement epic as real ticket with lifecycle | ❌ TODO | See Phase B |
| 10.5 | Add transition guards for dependencies (can't advance if blockers unresolved) | ❌ TODO | See Phase C |
| 10.6 | Implement `search()` method with all filter options | ❌ TODO | See Phase D |

---

### Phase A: Dependency graph engine

Add these methods to `TicketEngine`:

```typescript
// Direct dependencies → dependents (what this ticket depends on)
addDependency(ticketNum: number, dependsOn: number): void;
removeDependency(ticketNum: number, dependsOn: number): void;
getDependencies(ticketNum: number): number[];  // direct only

// Dependents → what depends on this ticket (blocking chain)
addDependent(ticketNum: number, dependedOn: number): void;
removeDependent(ticketNum: number, dependedOn: number): void;
getDependents(ticketNum: number): number[];   // direct only

// Transitive blocking chain
getBlockedByChain(ticketNum: number): number[];  // follows dependency graph transitively (DFS)
getBlocksChain(ticketNum: number): number[];     // what tickets are transitively blocked by this

// Dependency-aware transition guard
canTransition(ticketNum: number, newStatus: 'active' | 'resolved' | 'archived'): {
    allowed: boolean;
    reason?: string;  // e.g., "Cannot activate: blocking tickets [42, 43] are not resolved"
};
```

**Blocking rules:**
- A ticket in `draft` status cannot transition to `active` if any dependency is NOT `resolved` (or `archived`)
- This prevents starting work on a feature that depends on infrastructure changes not deployed
- Epics provide an additional constraint: epics cannot resolve until all children are resolved or explicitly overridden

### Phase B: Epic as real ticket type

```typescript
// Create epic — stored like any ticket but with isEpic = true + children tracked in index
createEpic(params: { area?: string; title: string; objective?: string }): number;

// Link/unlink child tickets to/from an epic
addChildToEpic(epicNum: number, childNum: number): void;
removeChildFromEpic(epicNum: number, childNum: number): void;

// Query epics
getEpics(): Array<{num: number; title: string; area?: string; children: number[]}>;
getEpicsWithSummary(): Array<{num: number; title: string; resolved: number; total: number; pct: number; children: {num: number; title: string; status: string}[]>}>;

// Get child tickets of an epic
getEpicChildren(epicNum: number): number[];

// Epic override — allow resolving even with unresolved children
overrideEpicResolution(epicNum: number, reason: string): void;  // logs the override
```

**How epics work internally:**
- Epics are stored as `ticket-NNN.json` regular ticket files (same schema)
- They have a special `is_epic: true` field in addition to standard fields
- The `_index.json` tracks parent→child relationships for efficient epic children lookup
- Epic status = composite of children's statuses (auto-computed on read):
  - "all resolved" → ready to resolve the epic itself
  - "some active, some draft" → auto-set to "active" (at least one child is in progress)
  - No direct transition from draft/resolved without going through intermediate states unless overridden

### Phase C: Transition guards for dependencies

Extend `canTransition()` on the TicketEngine to check dependency status:

```typescript
// When transitioning a ticket's status, check if it has unresolved blockers
function canTransitionWithDeps(ticketNum: number, newStatus: string): {
    allowed: boolean;
    reason?: string;
    blockingTickets?: number[];  // tickets that would block this change
}
```

**Guard rules:**
- `draft → active` is blocked if any dependency is NOT in at least `resolved` (archived also counts as "done") status
- `active → resolved` is blocked if any dependent has NOT transitioned to `resolved` (i.e., the dependency is still needed somewhere) — this prevents deleting work that's required by active children
- No guard on -> archived since that's terminal and explicit

### Phase D: Search method with all filter options

```typescript
search(params: {
  // Exact match filters
  area?: string;
  type?: 'feature' | 'bug' | 'infra' | 'pause' | 'spike';
  status?: 'draft' | 'active' | 'resolved' | 'archived';
  priority?: 'trivial' | 'minor' | 'major' | 'critical';
  
  // Labels — any of these match (OR logic within a single set)
  labels?: string[];           // e.g., ['type/feature', 'severity/high']
  labelType?: 'spike' | 'bug' | 'improvement';  // shorthand for type/* labels
  labelSeverity?: 'critical' | 'major' | 'minor' | 'trivial';
  labelScope?: 'agent' | 'engine' | 'infra' | 'docs' | 'tests' | 'security' | 'performance';
  
  // Relationship filters
  parent_epic?: number;        // tickets under an epic
  depended_on_by?: number;     // tickets that this ticket blocks
  exclude_dependencies?: boolean;  // exclude tickets that have dependencies (find ready-to-start work)
  
  // Text search (full-text on title + objective)
  q?: string;                  // free-text keyword search
  
  // Date range filters
  created_after?: string;      // ISO-8601
  created_before?:string;      // ISO-8601
  updated_after?: string;      // ISO-8601  
  updated_before?: string;     // ISO-8601
  
  // Priority / effort filters
  has_estimate?: boolean;      // tickets with estimated vs unestimated work
  estimate_min?: number;       // minimum estimate value
  estimate_max?: number;       // maximum estimate value
  
  // Sorting
  sort_by?: 'number' | 'updated_at' | 'created_at' | 'estimate_desc';
  sort_order?: 'asc' | 'desc';
  
  // Pagination
  limit?: number;              // defaults to 50, max 200
  offset?: number;             // default 0
}): Ticket[]
```

**Search query interpretation:**
- Multiple filters within the same category are AND (all must match)
- Multiple categories can be combined freely
- `q` text search OR-combines with all other exact-match filters
- `exclude_dependencies: true` is an additional filter that returns only tickets ready to work on (nothing depends on them being done first)

### Phase E: Conventional commit reference recognition

Implement pattern recognition for cross-ticket references in text fields:

```typescript
// Extract ticket numbers from a string (commit message, comment body, etc.)
extractTicketRefs(text: string): number[];  // matches #42, #042, refs #42, closes #42 patterns

// Auto-link detected ticket refs in comments/fields
autoLinkRefs(ticketNum: number, text: string): { linked: number[], message: string };
```

## Files modified/created

| File | Action |
|---|---|
| `ticket-engine-mcp/src/engine.ts` | Add dependency methods, epic methods, search method, extended Ticket type |
| `ticket-engine-mcp/src/index.ts` | Update IndexEntry to track relationships; add parents tracking for epics |
| `ticket-engine-mcp/docs/SCHEMA.md` | Document new fields (labels enum, estimate, estimated_model, is_epic) and dependency graph semantics |
| `ticket-engine-mcp/docs/ticket-v1.json` | Add new schema properties and label validation constraints |
| `docs/workflows.md` | Update ticket lifecycle to mention sequential/parallel execution model + epic usage

## Verification

```bash
# After implementation:
cd ticket-engine-mcp
npm run build    # must compile
npm test         # all tests pass

# Manual verification of search:
npx ts-node src/mcp-server.ts  # start server, then in another terminal:
# call mesterTickets_search with area=auth, status=active, labelSeverity=critical
```

## Notes

- This ticket builds on engine.ts being compilable first (008) and the schema being defined (007 pending migration or deferred — see C.5)
- SPIKE sub-tickets are handled naturally: create a spike ticket with `parent` pointing to the feature/bug epic, treat it like any other dependency
