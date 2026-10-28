# Little Mester MCP — Ticket Engine Module

The ticketing module of **Little Mester MCP**. Tickets are plain JSON files on disk, with a
small rebuildable index and an append-only audit log. One engine (`src/engine.ts`) serves
two front-ends: a CLI and an MCP server.

> **Read the decision logs first.**
> - Usage/workflow constraints: [`docs/DECISIONS.md`](../../docs/DECISIONS.md)
> - Engineering design + invariants: [`docs/tickets/architecture/DECISIONS.md`](../../docs/tickets/architecture/DECISIONS.md) — especially §I (enforcement)
>
> New sessions and AI agents: read both before changing anything here.

---

## At a glance

| | |
|---|---|
| Package | `@little-mester/ticket-engine-mcp` |
| License | GNU Affero General Public License, version 3 only (`AGPL-3.0-only`) — see [`LICENSE`](../LICENSE) |
| Language | TypeScript (CommonJS) |
| Tests | Jest (`npm test`) — engine, CLI, MCP |
| Store | `docs/tickets/` (JSON files, `_index.json`, `_audit.json`) |
| Writes | **Engine only** — CLI or `mester_tickets_*` MCP tools |
| Enforcement | Transient PID lock + per-entry SHA-256 audit + orphan detection |
| Quality gate | Every ticket has acceptance criteria + a test ledger (`validate` fails without them) |

---

## For AI agents (hard rules)

1. **Do not hand-write ticket data.** Never create, edit, move, or delete
   `ticket-NNN.json`, `_index.json`, or `_audit.json` directly.
2. Use `little-mester-mcp/scripts/ticket-engine ...` or the `mester_tickets_*` tools.
3. Every ticket needs **at least one acceptance criterion** and a test ledger. Put the
   criterion in `acceptance_criteria`, never in `objective` or a comment.
4. If `validate` reports an **orphan** or a **stale lock**, stop and ask a human. Do not
   delete or "fix" the files yourself.
5. `audit --baseline` is the only adoption path, and only when a human asks for it.
6. Follow `doc → test → code → commit` (see [`docs/workflows.md`](../../docs/workflows.md)).

Why: the index and audit log are derived data. A hand edit silently drifts them. The
engine cannot prevent it, but `validate` makes it visible.

---

## Install, build, test

```bash
cd little-mester-mcp
npm ci
npm run build          # tsc → dist/
npm run check:package  # entry points, built CLI, tarball contents
npm test               # Jest: engine + CLI + MCP
```

The published entry points are `dist/engine.js` (`main`), `dist/engine.d.ts` (`types`),
and `dist/cli.js` (`ticket-engine` bin). `files` whitelists `dist/` and `docs/`, so the
tarball never falls back to shipping TypeScript sources or tests.

The package includes the complete AGPL text in `LICENSE` (identical to the repo's
root licence). Copyright (c) 2026 Ethan Freestone. Unless otherwise noted, this
licence applies to the module's code and documentation. Third-party dependencies
retain their own licences. The software is provided without warranty, as described
in the licence. Using the module as a tool for an independent project does not by
itself change that project's licence. Commercial use and compliant resale are
permitted without licence fees or company-size limits.

Run the CLI during development without building:

```bash
# From the repo root (targets docs/tickets automatically)
./little-mester-mcp/scripts/ticket-engine list

# Or inside the module (the wrapper still targets the repo's docs/tickets)
cd little-mester-mcp
./scripts/ticket-engine list
```

The wrapper pins `TICKET_DIR` to the repo's `docs/tickets` unless you override it. That
prevents the classic mistake of writing a second store under `little-mester-mcp/docs/tickets`.

---

## Layout

```
little-mester-mcp/
├── src/
│   ├── engine.ts       core CRUD, lifecycle, index, enforcement, validate
│   ├── index.ts        index read/write/scan/reconstruct
│   ├── cli.ts          command-line front-end
│   └── mcp-server.ts   MCP tool registrations (mester_tickets_*)
├── scripts/
│   ├── ticket-engine   shell wrapper → src/cli.ts
│   └── check-package.js  post-build package/tarball smoke test
├── docs/
│   ├── README.md       this file
│   ├── CLI.md          command reference
│   ├── SCHEMA.md       human-readable schema
│   ├── ticket-v1.json  machine-readable JSON Schema
│   └── blueprint.md    historical implementation plan
└── tests/              engine / cli / mcp-server
```

---

## Data model

A ticket directory contains:

| File | What it is | Written by |
|---|---|---|
| `ticket-NNN.json` | One ticket. Source of truth for ticket content. | engine |
| `_index.json` | Rebuildable metadata index (`schemaVersion`, `tickets[]`, `updated_at`). | engine |
| `_audit.json` | Append-only array of lean write records with per-entry hashes. | engine |
| `.engine.lock` | Transient PID lock while a write is in progress. Normally absent. | engine |

`_index.json` is derived. If it is missing or corrupt, the engine reconstructs it from
the ticket files. The ticket files themselves are never regenerated from the index.

Full field list: [`SCHEMA.md`](SCHEMA.md) and [`ticket-v1.json`](ticket-v1.json).

### Acceptance criteria & tests

Two required fields define "done":

```json
"acceptance_criteria": [
  { "id": "A1", "text": "Login returns a session token", "status": "passing",
    "evidence": "tests/auth.test.ts › valid credentials" },
  { "id": "A2", "text": "Wrong password leaks nothing", "status": "test-needed" }
],
"tests": {
  "needed": ["test wrong-password timing"],
  "created": ["tests/auth.test.ts"],
  "cases": ["valid credentials"]
}
```

`status` is the test-tracking state: `pending`, `in-progress`, `test-needed`,
`test-created`, `passing` (needs `evidence`), or `waived` (needs `note`). `create`
requires ≥1 criterion; `update` replaces criteria but **merges** the three test arrays so
the paper trail cannot be erased by an edit. `validate` fails on policy errors and warns
on drift. Details: [`docs/DECISIONS.md` §C.4](../../docs/DECISIONS.md) and
[`architecture/DECISIONS.md` §J](../../docs/tickets/architecture/DECISIONS.md).

---

## Enforcement model (summary)

The engine makes bypasses **visible**, it does not try to make them impossible. Full
rationale and invariants: [`architecture/DECISIONS.md` §I](../../docs/tickets/architecture/DECISIONS.md).

- **Transient lock.** A write acquires `.engine.lock` (PID + timestamp), does its work,
  and releases it in `finally`. A live PID blocks other writers. A dead PID is stale and
  is cleared automatically on the next write.
- **Audit log.** Every create/update/archive appends one entry:
  `{ timestamp, operation, ticket_number, hash }`. `hash` is
  `sha256(canonical_json({timestamp, operation, ticket_number}))`. Editing those fields
  breaks the hash. It is not a chain.
- **Orphans.** A `ticket-NNN.json` with no audit entry is an orphan. It is flagged for
  human review and never auto-deleted or auto-adopted. `audit --baseline` is the explicit
  one-time adoption path for stores created before enforcement.
- **Merging branches.** `_index.json` is deleted and rebuilt; `_audit.json` conflicts are
  resolved with `audit --merge <their-log>`, which verifies both logs and never drops an
  entry. Runbook: [`SCHEMA.md`](SCHEMA.md#merging-ticket-stores) and
  [`docs/DECISIONS.md` §C.5](../../docs/DECISIONS.md).
- **Fail-open writes, loud validation.** Writes still work if the store is suspicious.
  `validate` exits `1` for out-of-sync index, tampered audit, stale lock, or orphans.

---

## Two front-ends, one engine

### CLI

```bash
./little-mester-mcp/scripts/ticket-engine create --area infra --type feature \
  --ac "CI runs on every push" --ac "Failures block merge" \
  --test-needed "workflow smoke test" --priority major "Add CI"
./little-mester-mcp/scripts/ticket-engine list --area infra --status active
./little-mester-mcp/scripts/ticket-engine update 12 --status active --comment "Started"
./little-mester-mcp/scripts/ticket-engine update 12 --ac-status A1=passing \
  --ac-evidence "A1=.github/workflows/ci.yml run 123"
./little-mester-mcp/scripts/ticket-engine archive 12
./little-mester-mcp/scripts/ticket-engine audit --limit 20
./little-mester-mcp/scripts/ticket-engine validate
```

Details, flags, exit codes: [`CLI.md`](CLI.md).

### MCP server

All tools use the `mester_<module>_<action>` naming convention:

| Tool | Purpose |
|---|---|
| `mester_tickets_create` | Create a draft ticket (requires ≥1 acceptance criterion) |
| `mester_tickets_list` | List tickets (area/status filters) |
| `mester_tickets_get` | Read one ticket by number |
| `mester_tickets_update` | Update fields, criteria, and test ledger (status transitions enforced) |
| `mester_tickets_archive` | Archive a ticket |
| `mester_tickets_search` | Multi-field search (includes criterion text) |
| `mester_tickets_board` | Board view (priority, then `updated_at`) |
| `mester_tickets_validate` | Index + enforcement + acceptance-criteria/test policy |

The MCP server calls the same `createTicketEngine()`; there is no duplicated business
logic. Start it with `npm run server` (stdio transport).

---

## Lifecycle

```
draft ──► active ──► resolved ──► archived
  ▲         │            │            │
  └─────────┘            └────────────┘
       (active → draft)     (resolved → draft, archived → draft)
```

Allowed transitions in the engine:

| From | To |
|---|---|
| `draft` | `active`, `archived` |
| `active` | `resolved`, `draft` |
| `resolved` | `draft`, `archived` |
| `archived` | `draft` |

The `archive` command always sets `archived` directly. `update --status` follows the table
above, so `active → archived` via update is rejected; use `archive` for that.

---

## Routing modes

| Mode | Where tickets live | Config |
|---|---|---|
| Same repo (default) | `<workspace>/docs/tickets` | default / `TICKET_DIR` |
| Any directory | anywhere on disk | `--ticket-dir <path>` or `TICKET_DIR` |
| Separate repo | another checkout | point `TICKET_DIR` at it, sync with git |

The engine never assumes the module directory is the store. The wrapper's default is the
repo root's `docs/tickets`, not the module's.

---

## Testing

```bash
npm test                      # all suites
npm test -- engine.test.ts    # engine only
npm test -- cli.test.ts       # CLI only (slower: spawns ts-node per command)
npm test -- mcp-server.test.ts
```

The suites cover:

- CRUD, numbering, index reconstruction, status transitions;
- transient lock release, live-PID refusal, stale-PID recovery;
- per-entry audit hashes, tamper detection, ordering with revisits;
- orphan detection and baseline adoption;
- CLI flags (`--priority`, `--comment`, `--limit`, `--json`, `--baseline`) and exit codes;
- `validate` failing on out-of-sync, tampered audit, stale lock, and orphans.

---

## Troubleshooting

| Symptom | Meaning | What to do |
|---|---|---|
| `validate` → `Orphans: N ticket file(s)` | Files exist with no audit entry (likely hand-written). | Review them. Ask a human. Adopt with `audit --baseline` or remove deliberately. Never blind-delete. |
| `validate` → `❌ Lock: Stale lock from dead PID` | A writer crashed. | Safe to clear; the next write clears it automatically. |
| `validate` → `❌ Audit: Audit hash mismatch` | An audit entry was edited by hand. | Stop. Investigate. Do not append. |
| `validate` → `OUT OF SYNC` | Index and files disagree. | Run `list` and inspect; the engine will reconcile on the next write, but find out why first. |
| Ticket written to `little-mester-mcp/docs/tickets/` | Ran the CLI from the module dir with an old wrapper. | Use the wrapper (it pins `TICKET_DIR`) or pass `--ticket-dir`. |

---

## Related docs

- [`CLI.md`](CLI.md) — every command and flag
- [`SCHEMA.md`](SCHEMA.md) — ticket, index, and audit formats
- [`blueprint.md`](blueprint.md) — historical plan (not current behaviour)
- [`docs/DECISIONS.md`](../../docs/DECISIONS.md) — product usage rules
- [`docs/tickets/architecture/DECISIONS.md`](../../docs/tickets/architecture/DECISIONS.md) — engineering decisions (§I = enforcement)
- [`docs/workflows.md`](../../docs/workflows.md) — development cycle and conventions
