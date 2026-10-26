# Ticket Schema Specification

Human-readable reference for the data the ticket engine reads and writes. The machine
definition is [`ticket-v1.json`](ticket-v1.json) (JSON Schema draft-07). If they disagree,
`ticket-v1.json` wins for validation; this file explains intent. Field semantics come from
[`docs/DECISIONS.md`](../../docs/DECISIONS.md) §D–E and
[`architecture/DECISIONS.md`](../../docs/tickets/architecture/DECISIONS.md) §E.

## Version management

The schema version is stored in `_index.json` as `schemaVersion` (currently `1`). When a
new version is introduced:

1. Read every `ticket-NNN.json`.
2. Apply the transform for the new version.
3. Write the transformed files back **through the engine**.
4. Regenerate the index.
5. Update `schemaVersion`.

Transforms must be idempotent: running twice produces no further changes.

---

## Ticket file (`ticket-NNN.json`)

One ticket per file. Example:

```json
{
  "$schema": "https://raw.githubusercontent.com/little-mester/ticket-engine/v1.0/docs/SCHEMA.md",
  "id": 42,
  "file_path": "/workspace/docs/tickets/ticket-042.json",
  "number": 42,
  "area": "auth",
  "type": "feature",
  "status": "draft",
  "title": "Implement OAuth2 flow for user registration",
  "objective": "Users can register and log in via Google OAuth without manual credential entry",
  "priority": "major",
  "labels": ["scope/security", "severity/major"],
  "acceptance_criteria": [
    {
      "id": "A1",
      "text": "A user can register and log in via Google OAuth",
      "status": "test-needed"
    },
    {
      "id": "A2",
      "text": "The flow does not duplicate an existing account",
      "status": "pending"
    }
  ],
  "tests": {
    "needed": ["integration test for the OAuth callback"],
    "created": [],
    "cases": []
  },
  "created_at": "2026-10-15T09:00:00.000Z",
  "updated_at": "2026-10-15T09:00:00.000Z",
  "models_used": []
}
```

Older tickets may omit `$schema`, `id`, `file_path`, or `models_used`; the engine treats
`number` as the identity and `file_path` as derived. New engine-written tickets include
them. `acceptance_criteria` and `tests` are required for every ticket — older tickets were
backfilled through the engine (see the migration note below).

### Required fields

| Field | Type | Notes |
|---|---|---|
| `number` | integer ≥ 1 | Unique ID. Never recycled; gaps are allowed. |
| `area` | string | Human grouping. Set at creation; prefer creating a new ticket over moving areas. |
| `type` | enum | `spike`, `feature`, `bug`, `improvement`, `doc`, `infra` |
| `status` | enum | `draft`, `active`, `resolved`, `archived` |
| `title` | string | One-line summary |
| `acceptance_criteria` | criterion[] | At least one; see [Acceptance criteria](#acceptance-criteria) |
| `tests` | object | `{needed[], created[], cases[]}`; arrays may be empty |
| `created_at` | ISO-8601 | Immutable creation time |
| `updated_at` | ISO-8601 | Last modification time |

### Optional fields

| Field | Type | Purpose |
|---|---|---|
| `objective` | string | Detailed objective |
| `priority` | enum | `trivial`, `minor`, `major`, `critical` — primary sort signal |
| `labels` | string[] | `prefix/value` tags. Predefined `scope/*`, `severity/*`; otherwise `user/*` |
| `dependencies` / `blocked_by` | number[] | Dependency graph |
| `parent` / `is_epic` | number / boolean | Epic grouping |
| `estimate` | number | Effort (hours or points) |
| `estimated_model` | string | Model used when the estimate was made |
| `suggested_model` | string | Recommended model for this work type |
| `estimated_models` | `{model, subtask_hint}[]` | Projected model split |
| `models_used` | usage records[] | Actual model usage, populated automatically |
| `comments` | `{timestamp, author, body}[]` | Timestamped notes (CLI appends here) |
| `attachments` | string[] | Linked file paths |
| `spans` | `{start, end}` | Date range |
| `assigned_to` | string | Responsible user/agent |
| `links` | `{target, type}[]` | Typed relationships |
| `subtickets` | `{id, title, status}[]` | Lightweight child entries |
| `notes` | string | Free-form notes |
| `id` / `file_path` | number / string | Convenience copies written by the engine |

`additionalProperties` is allowed so the schema can grow without invalidating old
tickets. The engine's `validateData()` enforces `number`, `area`, `type`, `status`, and
`title` on read, and additionally checks the shape of `acceptance_criteria` and `tests`
when present. Full policy checks run in `validate` (see below).

---

## Acceptance criteria

Every ticket has at least one criterion. Criteria are objects, not prose:

```json
"acceptance_criteria": [
  {
    "id": "A1",
    "text": "A valid login returns a session token",
    "status": "passing",
    "evidence": "tests/auth.test.ts › valid credentials"
  },
  {
    "id": "A2",
    "text": "An invalid password does not reveal account existence",
    "status": "waived",
    "note": "Requires browser-level timing checks; manual QA for now"
  }
]
```

| Field | Required | Notes |
|---|---|---|
| `id` | yes | `^[A-Za-z][A-Za-z0-9_-]{0,15}$`; unique per ticket. Auto-assigned `A1`, `A2`, … when omitted on input |
| `text` | yes | What must be true for the criterion to be met |
| `status` | yes | One of the six statuses below; defaults to `pending` when omitted on input |
| `evidence` | when `passing` | Test path/case, command + result, or observed artifact |
| `note` | when `waived` | Why no test is needed or possible |

| Status | Meaning |
|---|---|
| `pending` | Not started; nothing verified yet |
| `in-progress` | Implementation underway |
| `test-needed` | Implemented; a test still needs to be written |
| `test-created` | A test exists but has not been confirmed passing |
| `passing` | Verified; `evidence` required |
| `waived` | No test needed/possible; `note` required |

## Test ledger

```json
"tests": {
  "needed": ["test that an empty password is rejected"],
  "created": ["tests/auth.test.ts"],
  "cases": ["valid credentials", "wrong password", "empty password"]
}
```

All three arrays are required (they may be empty) and contain non-empty strings.
`needed` is the to-write list, `created` lists existing test files/cases, and `cases`
names the concrete scenarios covered, including manual observations.

**Write semantics:**

- `create` requires `acceptance_criteria`; `tests` defaults to `{needed: [], created: [], cases: []}`.
- `update` replaces the acceptance-criteria array. The CLI patches criteria by id
  (`--ac-status`, `--ac-evidence`) instead of resending the whole array.
- `update` merges the three test arrays with the stored values, so the paper trail is
  append-only from the API's point of view.

### Migration note (2026-10-26)

Acceptance criteria and the test ledger became required after the store already held
tickets. Every existing ticket was backfilled **through the engine** (`update` calls, so
each change is audited), not by hand-editing files. Historical/archived tickets whose work
predates the policy use `status: "waived"` with a note explaining the backfill; tickets
with active work got criteria derived from their objectives and a `needed`/`created` ledger
matching the real tests.

---

## Lifecycle

```
draft ──► active ──► resolved ──► archived
  ▲         │            │            │
  └─────────┘            └────────────┘
       (active → draft)     (resolved → draft, archived → draft)
```

| From | Allowed next states |
|---|---|
| `draft` | `active`, `archived` |
| `active` | `resolved`, `draft` |
| `resolved` | `draft`, `archived` |
| `archived` | `draft` |

`archive` (the command/tool) sets `archived` directly and bypasses the table.
`update --status` enforces the table.

---

## Index file (`_index.json`)

Derived metadata; safe to rebuild. It is **not** the source of truth for ticket content.

```json
{
  "schemaVersion": 1,
  "tickets": [
    { "number": 42, "file_path": "ticket-042.json", "status": "draft", "area": "auth" }
  ],
  "updated_at": "2026-10-15T09:30:00.000Z"
}
```

| Field | Description |
|---|---|
| `schemaVersion` | Schema generation (currently `1`) |
| `tickets[]` | One entry per ticket: `number`, `file_path` (relative), `status`, `area` |
| `updated_at` | Last index write |

If the index is missing or has no `schemaVersion`, the engine reconstructs it from the
ticket files on startup. `validate` reports drift between index and disk.

---

## Audit file (`_audit.json`)

Append-only by convention, written only by the engine. One lean entry per write:

```json
[
  {
    "timestamp": "2026-10-26T16:12:03.456Z",
    "operation": "create",
    "ticket_number": 34,
    "hash": "0f1e2d3c4b5a69788796a5b4c3d2e1f0a1b2c3d4e5f60718293a4b5c6d7e8f90"
  }
]
```

| Field | Description |
|---|---|
| `timestamp` | ISO-8601, when the write happened |
| `operation` | `create`, `update`, `archive`, or `baseline` |
| `ticket_number` | Ticket affected |
| `hash` | `sha256(canonical_json({timestamp, operation, ticket_number}))` as 64 lowercase hex chars |

Rules:

- The hash covers **only** those three fields, in that key order. It is not a chain and
  does not cover ticket content.
- Editing `timestamp`, `operation`, or `ticket_number` breaks the hash. `validate` reports
  `Audit hash mismatch`.
- Ticket numbers may go backwards (for example `create #32`, `create #33`,
  `update #32`). Ordering is checked by non-decreasing `timestamp`, not ticket number.
- `baseline` entries record pre-existing files at enforcement startup. They are created
  only by `ticket-engine audit --baseline`.
- A ticket file with no entry here is an **orphan**: reported by `validate`, never
  auto-deleted or auto-adopted.

Rationale and invariants: [`architecture/DECISIONS.md` §I](../../docs/tickets/architecture/DECISIONS.md).

### Merging ticket stores

| File | Merge rule |
|---|---|
| `ticket-NNN.json` | Normal three-way merge. Never delete a ticket to resolve a conflict; rerun `validate` after resolving content. |
| `_index.json` | Derived cache. **Do not hand-merge.** Delete it and run any engine command (e.g. `validate`) to rebuild it from the ticket files. |
| `_audit.json` | Append-only evidence. On conflict, keep **both** sides' entries ordered by non-decreasing timestamp. Never delete or rewrite an entry. |
| `.engine.lock` | Transient. Never commit; it is gitignored. `validate` reports a crash-left lock as stale. |

After resolving a merge, run `ticket-engine validate`. A ticket file without an audit
entry is an orphan, not something to paper over with `audit --baseline`. Baseline adoption
is only for a store that predates enforcement (see `docs/DECISIONS.md` §C.5).

**Resolving an audit conflict through the engine:**

```bash
# Keep your side of the conflicted log, then merge the other branch's entries.
git checkout --ours docs/tickets/_audit.json
git show :3:docs/tickets/_audit.json > /tmp/theirs-audit.json
./little-mester-mcp/scripts/ticket-engine audit --merge /tmp/theirs-audit.json

# _index.json is derived: delete it and let the engine rebuild it.
rm docs/tickets/_index.json
./little-mester-mcp/scripts/ticket-engine validate
```

`audit --merge` verifies both logs, adds only entries that are not already present, sorts
by timestamp, and refuses to write if either side has an invalid hash. It never drops an
entry. If a ticket is still an orphan after the merge, stop and ask a human.

---

## Lock file (`.engine.lock`)

Transient. Exists only while a write is running:

```json
{
  "pid": 12345,
  "acquired_at": "2026-10-26T16:12:03.400Z"
}
```

| State | Meaning |
|---|---|
| Absent | Normal — no write in progress |
| Present, live PID | A write is in progress; other writers refuse to start |
| Present, dead PID | Stale lock from a crash; cleared automatically on the next write |
| Corrupt/unparseable | Treated as stale; cleared |

`validate` reports a stale lock as an error (exit `1`) but does not remove it. The next
write does.

---

## File naming

- Ticket files: `ticket-NNN.json`, zero-padded to at least 3 digits (`ticket-042.json`).
- `_index.json`, `_audit.json`, and `.engine.lock` are reserved names.
- Other files (for example `architecture/DECISIONS.md`, `archive/*.md`) are ignored by the
  engine's ticket scan.

---

## Validation summary

| Check | Where | Fails when |
|---|---|---|
| Required ticket fields | engine read/write | `number`, `area`, `type`, `status`, or `title` missing/invalid |
| Type enum | `create` / `update` | type not in the 6 allowed values |
| Status transition | `update` | transition not in the lifecycle table |
| Acceptance criteria required | `create` / `validate` | no criteria on create, or an empty/missing array in a stored ticket |
| Criterion shape | engine read/write + `validate` | missing/invalid/duplicate id, missing text, invalid status |
| Evidence / note | normalizer + `validate` | `passing` without `evidence`, or `waived` without `note` |
| Test ledger | `validate` | missing `tests` object, or a `needed`/`created`/`cases` value that is not a string[] |
| Policy drift (warning) | `validate` | `test-needed` with empty `needed`; `test-created`/`passing` with empty `created`; resolved with unverified criteria |
| Index sync | `validate` | file without index entry, or index entry without file |
| Audit hashes/shape | `validate` | tampered entry, malformed entry, corrupt JSON, out-of-order timestamps |
| Lock | `validate` | stale lock (dead PID) or corrupt lock |
| Orphans | `validate` | ticket file has no audit entry |

Policy errors make `validate` exit `1`. Drift warnings are printed but do not fail.
Details: [`docs/DECISIONS.md`](../../docs/DECISIONS.md) §C.4 and
[`architecture/DECISIONS.md`](../../docs/tickets/architecture/DECISIONS.md) §J.
