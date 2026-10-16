# Ticket Schema Specification v1.0

## Version Management

The schema version is stored in `_index.json` under `schemaVersion`. When upgrading to a new schema version:
1. The engine reads all existing ticket `.json` files
2. For each file, apply the necessary transforms for the current schema version
3. Write back the transformed file
4. Regenerate the index
5. Update `_index.json.schemaVersion`

All transforms are idempotent — running them twice produces no additional changes.

## Ticket File Format (`ticket-NNN.json`)

Each ticket is a single JSON file:

```json
{
  "$schema": "https://raw.githubusercontent.com/anonymous/little-mester/ticket-engine/v1.0/docs/SCHEMA.md",
  "number": 42,
  "area": "auth",
  "type": "feature",
  "status": "draft",
  "title": "Implement OAuth2 flow for user registration",
  "objective": "Users can register and login via Google OAuth during their first visit without manual credential entry",
  "created_at": "2026-10-15T09:00:00Z",
  "updated_at": "2026-10-15T09:00:00Z"
}
```

### Field Definitions

| Field          | Type   | Required | Allowed Values                          | Description                                    |
|----------------|--------|----------|-----------------------------------------|------------------------------------------------|
| `number`       | number | yes      | unique integer                          | Auto-incremented ticket identifier             |
| `area`         | string | yes      | any                                     | Domain area the ticket belongs to              |
| `type`         | string | yes      | `feature`, `bug`, `infra`, `pause`      | Categorization of work type                    |
| `status`       | string | yes      | `draft`, `active`, `resolved`, `archived` | Current lifecycle state                       |
| `title`        | string | yes      | any                                     | One-line human-readable summary                |
| `objective`    | string | no       | any                                     | Detailed objective in present/future tense     |
| `created_at`   | string | yes      | ISO-8601 datetime                       | Immutable creation timestamp                   |
| `updated_at`   | string | yes      | ISO-8601 datetime                       | Last modification timestamp                    |

### Lifecycle Transitions

```
draft → active → resolved → archived
    ↘          ↗
     ────────/
(draft ↔ active allowed, resolved → active NOT allowed)
```

Valid transitions:
- `draft → active` (start work)
- `active → resolved` (complete work)  
- `resolved → draft` (reopen if needed)
- any → `archived` (remove from active cycle)
- `draft ↔ active` (bidirectional toggle)

## Index File (`_index.json`)

A single index file per ticket directory:

```json
{
  "schemaVersion": 1,
  "tickets": [
    { "number": 42, "file_path": "ticket-042.json", "status": "draft", "area": "auth" }
  ],
  "updated_at": "2026-10-15T09:30:00Z"
}
```

The index file is generated automatically by the engine — no manual editing needed.

## File Naming Convention

Ticket files: `ticket-NNN.json` where NNN is zero-padded to 3 digits (e.g. `ticket-042.json`).
