# Ticket 000 — Area-based ticket system with types, areas, directories, and lightweight catalog

## Status: ACTIVE

## Type: infra

## Objective

Replace the flat `docs/tickets/` directory structure with an area-based system that uses typed tickets organized in per-area directories, supported by a lightweight global catalog that only tracks area names (not individual ticket data).

---

## Problem (why this exists)

The current flat `docs/tickets/` structure has three failures:
- **Context bloat**: A growing number of `.md` files with prefix-based naming (`[MERGED]`, `[SUPERS]`) clutters the directory and makes grep/vim navigation worse over time, not better
- **No area isolation**: All tickets compete in one namespace. Different feature areas produce naming collisions and force everyone to read unrelated files
- **Schema drift**: `meta-ticket-schema.json` defines format rules that no existing script validates reliably — the validation script at `scripts/validate-tickets` has its own copy of the rules, not a reference to this schema

---

## Structure (what gets created)

```
docs/tickets/
├── meta-ticket-schema.json        ← single source of truth for format rules
├── catalog.json                   ← lightweight: only areas, no ticket data
│                                │   { "areas": ["infra/tools", "feat/wibble"] }
├── _README.md                     ← conventions doc (not a ticket — 1 page)
│
├── active/                        ← current work
│   └── infra/tools/               ← one area, independent numbering
│       ├── _index.json            ← per-area index: [ { number, title, status } ]
│       ├── ticket-0.md
│       └── ticket-X.md
│
├── merged/                        ← hold before archival (30-day rule)
│   └── <area>/
│
└── archive/                       ← historical reference
    └── <area>/
```

---

## Lightweight catalog (catalog.json design)

**Rule**: `catalog.json` only lists area names. Nothing else.

```json
{ "areas": ["infra/tools"] }
```

A user adds an area to this file when creating a new area for the first time. That is the only content ever written there until a new area exists. No ticket data, no numbers, no titles, no statuses.

**Why**: The catalog is consulted before creating a new ticket — it answers "does this area exist yet?" Not "what tickets exist?" Ticket lookup comes from `active/<area>/_index.json` which is local to the work you're doing in that area.

The `_index.json` per area contains:
```json
[
  { "number": 0, "title": "area-based-tickets", "status": "ACTIVE" }
]
```

The index only grows within its own directory tree — it never affects other areas. It is also always tiny (dozens of entries max per area in practice), not thousands.

---

## Area naming convention

Areas are named like git branch scopes:

| Prefix | When to use | Example |
|--------|------------|---------|
| `feat/` | New functionality | `feat/wibble`, `feat/auth-gate` |
| `fix/` or `bug/` | Bug fixes | `fix/egress`, `bug/login-crash` |
| `infra/` | Internal tooling, infra changes | `infra/tickets`, `infra/ci-setup` |
| (no prefix) | Standalone/workstream | `tools/deploy-pipeline` |

Subareas are allowed for granularity: `infra/tools`, `feat/auth/oauth`. Depth is not limited but 1-2 levels is the practical max before it gets confusing.

---

## Ticket numbering rules

### Global numbering
All tickets get a unique globally sequential number starting at 0 (zero, padded to 3 digits when used in filenames: `ticket-0.md`, `ticket-00.md` — but actual files use 3-digit padding: `ticket-000.md`).

Global numbers fill gaps. If the highest global number present is 7 and we create a new ticket, it gets 8 regardless of how many areas exist.

### Per-area numbering
Within each area (`active/infra/tools`), tickets are numbered independently starting at 0 (`_index.json` tracks this). Area numbering resets and is scoped to the area name. This lets parallel workstreams operate without coordination.

### Cross-referencing
- `[T-NNN]` resolves globally (e.g., "[T-3]", "[T-47]") by walking `active/merged/archive/*/<area>/ticket-NNN.md`
- `[area:sub/area, NNN]` for explicit cross-area reference

This means a central catalog isn't needed for lookup. The filesystem IS the database. We just need to know which directories to walk — and `catalog.json` tells us which areas to search.

---

## Migration of existing tickets

The 5 existing active tickets (001-005) go under `active/infra/tools/` since they're infrastructure/process work, not user-facing features. They get global numbers T-1 through T-5 and area-local numbers 1 through 5 in the new `_index.json`.

Migration steps:
1. Move each existing ticket file into `active/infra/tools/ticket-{padded-number}.md`
2. Migrate their content format (heading, sections) to match this schema
3. Create `active/infra/tools/_index.json` with those entries
4. Reorder to reflect dependency chain (001 first, then 002 based on... etc.)

---

## Files modified / created by THIS ticket

| File | Action | Path relative to repo root |
|------|--------|----------------------------|
| `docs/tickets/meta-ticket-schema.json` | Create/replace | schema definition for both era-1 (old flat) and new area-based format |
| `docs/tickets/catalog.json` (new file) | Create | lightweight area registry, only listing areas |
| `docs/tickets/_README.md` (new file) | Create | single-page conventions doc |
| `active/infra/tools/` (directory) | Create | new area directory |
| `active/infra/tools/_index.json` (new file) | Create | per-area index with migrated T-1 through T-5 entries |
| `scripts/ticket-new` | Modify | create tickets in correct area path, update _index.json locally only |
| `scripts/validate-tickets` | Modify | validate against meta-schema for the active schema version |
| `ticket-discarded-scripts-analysis.md` | Move into archive directory structure | move as-is (no rewrite needed) |

The old flat `docs/tickets/*.md` files are NOT deleted — they're migrated into the new directory structure. The migration is: read → transform to new format → write to `active/<area>/ticket-NNN.md`. The migration script in this ticket does that work.

---

## Validation rules (what validate-tickets must support after merge)

1. **Schema version detection**: Check if `docs/tickets/meta-ticket-schema.json` defines era 1 (flat, original format) or era 2 (area-based). Validate tickets accordingly.
2. **Active schema**: Only era 2 is valid going forward. Era 1 format is accepted on existing files during a transition window (until the user explicitly migrates to it), but new tickets must use era 2.
3. **Area index integrity**: `_index.json` must list all tickets present in the directory, and vice versa. Gaps or duplicates are errors.
4. **Catalog consistency**: `catalog.json` areas list must have entries if corresponding directories exist under `active/merged/archive/`, and no missing entries (area exists on disk but not in catalog = error).

---

## Files section (summary)

| File | Role |
|------|------|
| `docs/tickets/meta-ticket-schema.json` | Defines both eras' format rules for validation |
| `docs/tickets/catalog.json` | Lightweight area registry only, no ticket data |
| `docs/tickets/_README.md` | One-page conventions doc |
| `active/infra/tools/` (dir) | First working area directory |
| `active/infra/tools/_index.json` | Per-area track: numbers, titles, statuses |
