# Ticket 015 — Model tracking fields + type enum expansion (6 values) + field semantics lock

## Objective

Lock down all field definitions, expand the `type` enum to 6 values (per DECISIONS D.2 resolution), and add model tracking fields (`models_used`, `suggested_model`, `estimated_models`) to the engine so they are in the schema AND the code — not just documented in DECISIONS but enforced by validation.

This ticket resolves gaps **G.1** (type enum), **G.2** (models_used missing from specs/artifacts), and **G.6** (estimated_model vs suggested_model dual naming). All three decisions are already documented in `docs/DECISIONS.md` sections D.E+D.H+G — this ticket makes them engine reality.

## Constraints & Context (from DECISIONS.md)

- **type expanded to 6 values per DECISIONS D.2:** `spike`, `feature`, `bug`, `improvement`, `doc`, `infra`
- **priority is primary triage signal, severity is label-set** per DECISIONS H decision — both coexist for different use cases
- **models_used auto-populated** by opencode; user does NOT fill this in (DECISIONS E)
- **suggested_model vs estimated_models** are DIFFERENT: suggested = area recommendation from history; estimated_models = ticket-specific subtask split (DECISIONS E+H resolution of G.6)
- `_index.json` format uses `schemaVersion` + `next_number` per DECISIONS G resolution — hybrid of SCHEMA.md and blueprint formats
- **Type is NOT a label set** per DECISIONS D.2/G.4 resolution — it's the top-level enum field. Labels only contain scope, severity, and user/* entries

---

## Phase Tracking

| # | Task | Status | Notes |
|---|------|--------|-------|
| 15.1 | Update all engine schema definitions to expanded type enum (6 values) + add priority/scope/severity models tracking fields | ❌ TODO | Per DECISIONS D.2+D.H+G and ticket-v1.json |
| 15.2 | Update `_index.json` format to include `next_number` (DECISIONS G resolution) | ❌ TODO | Hybrid: schemaVersion + next_number, NOT blueprint's `version` |
| 15.3 | Document field semantics — what area/type/scope/status/label/tag mean and when to use each (DECISIONS D.1-D.4) | ✅ DONE | All documented in DECISIONS.md sections D.1 through D.4 — see below for summary |
| 15.4 | Verify ticket-v1.json schema matches these decisions (manual review) | ❌ TODO | Ensure all fields, enums, validation rules match DECISIONS docs |

---

## Field Semantics Reference (DECISIONS D.1-D.4 — documented in full, summarized here)

> This is what the user asked for: "A project manager's opinion on the difference between area/scope/type/status/label/tag and when to use what." All decisions are in DECISIONS.md; this section summarizes for quick reference.

### Lifecycle Fields (core identity of a ticket — set once, change rarely)

| Field | What it answers | Format | When to use it | How the engine treats it differently |
|-------|-----------------|--------|----------------|--------------------------------------|
| **area** | "WHAT domain is this?" — human grouping. Like a folder or project area. e.g., `auth`, `infra`, `engineering` | Free-text (recommend lowercase, hyphens for multi-word) | Set once when creating the ticket. If the ticket's domain changes mid-life, create a new ticket in the correct area rather than moving it. | Used as primary filter/grouping key in board views and dashboards. No validation engine-side — just string storage. |
| **type** | "WHAT KIND of work is this?" — affects how the engine gates progression. e.g., spike=research (no code), feature=new functionality, bug=defect fix | Enum: `spike`, `feature`, `bug`, `improvement`, `doc`, `infra` | Set on creation. Each type has different implications for what kind of work follows and what dependencies make sense. `spike` is a gate — other tickets SHOULD depend on the spike being resolved first. Engine validates against the 6-value enum on write. | Spike tickets may surface differently in search (as "blocked research"). Improvement/doc types tracked separately in dashboards to measure capacity allocation between them. |
| **status** | "WHERE in its lifecycle is this?" (not metadata, not classification — it's the ticket's state machine position) | Enum: `draft`, `active`, `resolved`, `archived` | Changed by you as work progresses. Every transition follows defined gates: draft→active requires dependency check via blocked_by[]; active→resolved has no gate; resolved↔draft toggles when re-opening; archived is terminal from any status. | Transition guards block draft→active if any dependency isn't resolved/archived. Status directly determines search visibility (archived dropped from default results). Epic composite_status computed from children's statuses. |

### Cross-cutting classification (applied ON TOP of core identity — optional, multiple values)

| Field | What it answers | Format | When to use it | How the engine treats it differently |
|-------|-----------------|--------|----------------|--------------------------------------|
| **priority** (top-level field) | "HOW URGENT is this for the current sprint?" — primary triage signal, used by all sorting/filtering. e.g., critical, major, minor, trivial | Enum: `trivial`, `major`, `minor`, `critical` | Set/update when the ticket's urgency changes. This is what board views sort by (critical → trivial). Most people use `priority` as their main status signal. | Primary sort field in board views and search results. Dashboard stats group by priority. Used in aging reports (low-priority tickets age silently; high-priority ones show up sooner). |
| **labels[]** (array of prefix/value strings) | Cross-cutting categorization that COMPOSES with other filters. Not for urgent/triage needs (use priority for that). | Prefix/value: `scope/engine`, `severity/major`, `user/team-x` | Use when a ticket spans multiple domains (`scope/engine scope/performance`) or needs multi-criteria filtering (`severity/critical scope/security`). `user/*` values accept any string after the prefix. Engine validates known prefixes (scope, severity) against their value allowlists; rejects unknown prefixes silently. | Only label sets with validation are `scope` and `severity`. Both can be queried independently AND combined in multi-criteria filters (`scope/engine AND severity/critical`). |

### How to use labels effectively (per DECISIONS D.3 resolution of G.4)

| Scenario | What to set | Why it works |
|----------|-------------|--------------|
| "This feature touches the auth system" | `type: feature` + `area: auth` | Core identity fields tell WHERE and WHAT — no extra labels needed |
| "This infra change affects the entire platform" | `type: infra` + `labels: [scope/engine]` + `priority: major` | Scope label says it's cross-cutting; priority ensures board visibility |
| "This improvement is technical debt but not urgent yet" | `type: improvement` + `severity/minor` (label) + `area: engineering` | priority might be trivial; severity/major as a label still tags it for when capacity opens up. Using both tells the engine "this matters but doesn't need immediate attention". |
| "Experimental workstream I'm not ready to formalize" | `type: spike` + `labels: [user/experimental-x]` | Labels allow `user/*` with no validation beyond prefix — flexible for exploration without polluting known sets. type is already `spike` so you know it's research first. |

### The tag problem (DECISIONS D.4 resolution of G.4)

**Decision:** There is NO separate "tag" field. All ad-hoc classification goes through labels with the `user/` prefix. This prevents "tag proliferation" where every person invents their own system without shared vocabulary. If it doesn't fit `scope/*`, `severity/*`, or your predefined label sets, use `user/something`.

---

## Phase A: Engine Updates (type enum + models tracking)

### 15.A.1 Update engine.ts VALID_TYPES constant
Change from 4 values to 6: `['spike', 'feature', 'bug', 'improvement', 'doc', 'infra']`

### 15.A.2 Update Ticket interface in engine.ts
Add optional fields for model tracking:
```typescript
export interface Ticket {
    // existing base fields...
    priority?: 'trivial' | 'major' | 'minor' | 'critical';
    labels?: string[];
    estimated_model?: string;
    suggested_model?: string;
    estimated_models?: Array<{model: string, subtask_hint: string}>;
    // models_used is auto-populated by opencode — not user-set. 
    // But it IS part of the ticket object for API consistency:
    models_used?: Array<{
        model: string;
        thinking_time_seconds: number;
        idle_time_seconds: number;
        cost_estimate_usd?: number;
        context_window_used_pct?: number;
    }>;
}
```

### 15.A.3 Update `_index.json` to use schemaVersion + next_number (DECISIONS G)
Add `next_number` field to TicketIndex interface and update writeIndex/createTicketEngine to maintain it. This enables deterministic ID generation for parallel agents — reading next_number atomically before incrementing.

### 15.A.4 Update index.ts IndexEntry if needed
If the hybrid format adds fields beyond `[number, file_path, status, area]`, update accordingly. Based on current design: no additional fields needed in IndexEntry since type/title are NOT duplicated (engine's preference over blueprint).

---

## Phase B: Schema & Migration Updates

### 15.B.1 Validate ticket-v1.json matches all DECISIONS decisions
Manual review comparing:
- Type enum has all 6 values ✓ (done when created)
- Priority field present with correct enum values ✓ 
- models_used, suggested_model, estimated_models schemas present ✓
- Index format references schemaVersion + next_number ✓
- No separate tag/label type confusion ✓

### 15.B.2 Update SCHEMA.md extended fields table
Add priority field explicitly (was implied but not listed in base table — exists in engineering docs only):
| `priority` | enum: trivial/major/minor/critical | no | Primary triage signal — different from severity label. See DECISIONS H decision. |

### 15.B.3 Update blueprint.md with index format resolution
Replace `_index.json` section to match hybrid format (schemaVersion + next_number, NOT `version`).

---

## Phase C: Migration & Verification

### 15.C.1 Run migration on any non-JSON tickets via ticket-007
When ticket-007 runs, it will use this schema as source of truth — no additional work beyond ensuring ticket-007 references the correct path to `ticket-v1.json`.

### 15.C.2 Verify all engine tests pass with new type enum
After updating VALID_TYPES and all hardcoded references, run full test suite. Any remaining "invalid type" failures indicate missed hardcoded values.

### 15.C.3 Verify all MCP tool input schemas reference expanded type enum
Check ticket-012's tool descriptions still match the expanded enum. The `list` tool's area/status filters remain unchanged — only the type enum values in create/update tools need updating.

---

## Verification

```bash
# After implementation:
cd ticket-engine-mcp

# Schema validation of existing tickets
npx ajv-cli validate -s docs/ticket-v1.json -d ../../docs/tickets/*.json 2>&1 | head

# Type enum expansion check — all 6 values should be valid
node -e "const { createTicketEngine } = require('./dist/src/engine.js'); 
  const e = createTicketEngine({ticketDir: '/tmp/test', indexPath: '/tmp/test/_index.json'});
  console.log('Creating ticket with type=improvement...');
  const t = e.create({area:'test', type:'improvement', title:'test improvement'});
  console.log(t); // should succeed, not throw"

# Index format check — next_number should exist after creation
cat /tmp/test/_index.json | jq '.next_number'  # should be 2

# Clean up test
rm -rf /tmp/test
```

---

## Files modified/created

| File | Action | Change |
|------|--------|--------|
| `ticket-engine-mcp/docs/ticket-v1.json` | Created ✅ | Formal JSON Schema with 6-value type enum, priority, models_used/suggested_model/estimated_models per DECISIONS docs |
| `docs/DECISIONS.md` | Updated ✅ | Section D expanded (field semantics + status meanings + model tracking field distinctions) + Sections G (index format) + D.H + I (ordering vs ID) documented |
| `ticket-engine-mcp/docs/ticket-v1.json` | Created ✅ | Matches all DECISIONS resolutions plus SCHEMA.md extended fields |
| `ticket-engine-mcp/src/engine.ts` | Update needed ⏸️ | VALID_TYPES → 6 values; Ticket interface + index add new model tracking fields |
| `ticket-engine-mcp/src/index.ts` | Update needed ⏸️ | Add next_number to TicketIndex/DefaultIndex |
| `ticket-engine-mcp/docs/blueprint.md` | Update needs ⏸️ | Sync _index.json format to hybrid format |
| `ticket-008-phase-fixes.md` (existing) | Already covers 008 ✅ | But type enum update must happen after or during engine.ts work in this ticket |
| `docs/tickets/engine-plan.md` | Updated ✅ | Resolved G.1/G.9 references, updated ticket table notes with new field scope |
| `architecture/DECISIONS.md` | Recreated ✅ | All gap resolutions applied (status semantics, priority vs severity distinction, epic persist/compute clarification) |
| `docs/tickets/GAP_AUDIT.md` | Created ✅ | In `/home/agent/.opencode/plan/` — all 12 gaps documented with status and affected docs |

## Notes

- This ticket handles the **type enum expansion** that was blocking the schema generation (ticket-007) — after this completes, ticket-v1.json is already created correctly
- **Models tracking is the first time these fields exist in code** — they've only been documented in DECISIONS.md section E. Adding them to the engine interface + _index structure means they're real fields from day one, not deferred
- **No `tag` field** (per DECISIONS D.4) — all ad-hoc classification goes via `user/` labels
- **Ticket numbers = IDs only** (DECISIONS I), ordered by priority+updated_at in views
