# Ticket 007 — Migrate markdown tickets to JSON + generate formal schema file

## Objective

Migrate all existing `docs/tickets/ticket-NNN.md` files to JSON format (`ticket-NNN.json`) and generate a formal JSON Schema file (`ticket-v1.json`) from SCHEMA.md for runtime validation. This is the foundation that enables all ticket engine work.

## Constraints & Context (from DECISIONS.md)

- **All future tickets MUST be valid JSON verified against the formal schema.** There is no markdown path going forward.
- `_index.json` is part of the NEW system design — it has NO prior history to worry about. Ignore existing references as legacy.
- Existing `.md` files are source material for migration, not targets of the new engine. All new tickets go straight to JSON.
- **Git auth/SSH management is out of scope for this ticket.** The container doesn't manage SSH; workspace is volume-mounted.

## Scope boundary

What IS in scope:
1. Generate `ticket-engine-mcp/docs/ticket-v1.json` — a formal JSON Schema file from SCHEMA.md
2. Create a migration script that reads all existing `.md` tickets, extracts fields (number, area, type, status, title, objective, created_at, updated_at), and writes corresponding `.json` files to `docs/tickets/`
3. Run schema validation on every output file — if any can't be validated, report the issue

What is NOT in scope:
- Converting extended fields (labels, dependencies, epics, etc.) — those only exist on new tickets going forward
- Auto-migrating markdown-specific content (tables of phase tracking, code blocks) — we extract structured metadata only
- Runtime engine for validation (that goes into the engine as a separate task)

---

## Phase Tracking

| # | Task | Status | Notes |
|---|------|--------|-------|
| 7.1 | Write formal JSON Schema `ticket-v1.json` from SCHEMA.md | ❌ TODO | Must include all base fields + extended fields (labels, priority, etc.) as optional schema properties |
| 7.2 | Create migration script | ❌ TODO | Reads MD → extracts metadata → outputs JSON. Run on each existing ticket file. |
| 7.3 | Run migration on `docs/tickets/*.md` and verify output against schema | ❌ TODO | Log what was extracted vs skipped for each ticket |
| 7.4 | Update SCHEMA.md to reflect the formal schema (keep as human-readable spec, point to `.json` as source of truth) | ❌ TODO | Document that `ticket-v1.json` is the authoritative validation file |

---

## Phase 1: Formal JSON Schema

File: `ticket-engine-mcp/docs/ticket-v1.json`

Schema MUST cover:
- **Required fields:** `number`, `area`, `type`, `status`, `title`, `created_at`, `updated_at`
- **Optional fields:** `objective`, `labels`, `priority`, `dependencies`, `blocked_by`, `parent`, `estimate`, `assigned_to`, `comments`, `attachments`, `spans`
- All validations from SCHEMA.md: type enum values, status transitions, ISO-8601 format for dates, auto-increment number constraint

## Phase 2: Migration Script

Script: `scripts/migrate-tickets` (bash) or `ticket-engine-mcp/src/migrate.mjs` (Node.js — preferred since engine is TypeScript)

### Inputs:
- Source: `docs/tickets/ticket-*.md`
- Pattern to extract from each file:
  - Title (first `#` heading)
  - Number (in filename or `# Ticket NNN`)
  - Area (from metadata table at top of file, if present)
  - Type (if specified in objective/area field)
  - Status (if explicitly stated — most tickets don't have one; default to "draft")

### Outputs:
- One `.json` file per ticket → `ticket-NNN.json` in the same directory
- Console log of what was extracted and any fields that couldn't be auto-populated (will need manual input)

## Phase 3: Validation

Run each output JSON through a validator against `ticket-v1.json`:
```bash
# Node.js approach:
$ node -e 'const Ajv = require("ajv"); ...' ticket-NNN.json
# OR using jsonschema-cli / ajv-cli
npx ajv-cli validate -s ticket-v1.json -d ticket-001.json
```

All files must pass. Any failures → report and stop; do not auto-fix.

## Verification

```bash
# Run migration
node ticket-engine-mcp/src/migrate.mjs --input docs/tickets/ --output docs/tickets/

# Verify all output files pass schema validation
for f in docs/tickets/ticket-*.json; do
  npx ajv-cli validate -s ticket-v1.json -d "$f" || echo "FAILED: $f"
done

# Confirm new tickets are written as JSON, not markdown
ls docs/tickets/*.json | head -20

# Read one and confirm it matches expected schema shape
cat docs/tickets/ticket-001.json
```

## Files modified/created

| File | Action |
|---|---|
| `ticket-engine-mcp/docs/ticket-v1.json` | Create — formal JSON Schema |
| `scripts/migrate-tickets` or `ticket-engine-mcp/src/migrate.mjs` | Create — migration script |
| `docs/tickets/ticket-NNN.json` (multiple) | Create by running migration on existing .md files |
| `SCHEMA.md` | Update — reference ticket-v1.json as source of truth |
