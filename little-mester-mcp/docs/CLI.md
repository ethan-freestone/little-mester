# Ticket Engine CLI

`ticket-engine` is the command-line front-end for the ticketing engine
(`src/engine.ts`). It delegates every operation to the engine — there is no duplicate
business logic — and the MCP server (`src/mcp-server.ts`) uses the same engine.

**Never edit ticket JSON, the index, or the audit log by hand.** Use this CLI or the
`mester_tickets_*` MCP tools. See [`docs/DECISIONS.md` §C.3](../../docs/DECISIONS.md) and
[`architecture/DECISIONS.md` §I](../../docs/tickets/architecture/DECISIONS.md).

## Where it lives

```
little-mester-mcp/scripts/ticket-engine   # wrapper → src/cli.ts (ts-node)
little-mester-mcp/src/cli.ts              # implementation
```

Run it from the repo root:

```bash
./little-mester-mcp/scripts/ticket-engine <command> ...
```

Or from the module directory:

```bash
cd little-mester-mcp
./scripts/ticket-engine <command> ...
```

The wrapper sets `TICKET_DIR` to the repo's `docs/tickets` unless it is already set. This
means the location you run it from does **not** change which store is written. Override
deliberately with `TICKET_DIR=...` or `--ticket-dir <dir>`.

## Install / dev

```bash
cd little-mester-mcp
npm install
npm run build     # compile to dist/
npm run dev -- list
```

---

## Quick start

```bash
T=./little-mester-mcp/scripts/ticket-engine

$T create --area infra --type feature "Add CI pipeline" \
  --objective "Automated builds on push" --priority major

$T list
$T list --area infra --status active

$T update 12 --status active --comment "Started work"
$T update 12 --priority critical

$T archive 12

$T audit                 # recent write trail
$T audit --json          # raw JSON
$T audit --baseline      # adopt pre-engine ticket files (one-time, human-approved)

$T validate              # index + lock + audit + orphans
```

---

## Commands

### `create`

Create a ticket. New tickets start in `draft` status.

```
ticket-engine create --area <area> --type <type> --ac "<criterion>" \
  [--ac "<criterion>" ...] [--objective "..."] [--priority <priority>] \
  [--test-needed "..."] [--test-created "..."] [--test-case "..."] [--comment "..."] \
  "Title"
```

| Flag | Required | Description |
|---|---|---|
| `--area` | recommended | Domain area (`auth`, `infra`, `billing`, …) |
| `--type` | no | `spike`, `feature`, `bug`, `improvement`, `doc`, `infra`. Default `feature` |
| `--ac` | **yes** | Acceptance criterion. Repeat for more than one. Bare text gets an auto id and `pending` |
| `--priority` | no | `trivial`, `minor`, `major`, `critical` |
| `--objective` | no | Detailed objective (present/future tense) |
| `--test-needed` | no | Test still to write (repeatable) |
| `--test-created` | no | Test file/case that exists (repeatable) |
| `--test-case` | no | Named case covered, including manual checks (repeatable) |
| `--comment` | no | Add an initial comment (author `cli`) |

At least one `--ac` is required. Invalid `--type` or `--priority` values exit `1` with a
clear error.

```bash
$ $T create --area auth --type bug "Login fails on empty password" \
    --ac "An empty password is rejected at the auth layer" \
    --ac "The error does not reveal whether the account exists" \
    --objective "Reject empty passwords at the auth layer" --priority critical \
    --test-needed "test empty password rejection"
✅ Created ticket #42
  #42 [DRAFT] bug — auth — Login fails on empty password
   Objective: Reject empty passwords at the auth layer
   Acceptance criteria: 2
   Tests: 1 needed, 0 created, 0 cases
```

Exit codes: `0` created, `1` error (missing title, missing `--ac`, invalid type/priority,
invalid criterion).

---

### `list`

```
ticket-engine list [--area <area>] [--status <status>]
```

| Flag | Description |
|---|---|
| `--area` | Filter by area |
| `--status` | `draft`, `active`, `resolved`, `archived` |

```bash
$ $T list --area auth --status active
Found 2 ticket(s):
  #42 [ACTIVE] bug — auth — Login fails on empty password
  #43 [ACTIVE] feature — auth — Add OAuth2 login
```

---

### `update`

Update fields on an existing ticket. Status changes follow the lifecycle table below.

```
ticket-engine update <number> [--status <status>] [--title <title>] \
  [--objective <obj>] [--area <area>] [--type <type>] \
  [--priority <priority>] [--ac "<criterion>"] \
  [--ac-status <id>=<status>] [--ac-evidence <id>=<text>] \
  [--test-needed "..."] [--test-created "..."] [--test-case "..."] \
  [--comment "..."]
```

| Flag | Description |
|---|---|
| `<number>` | Ticket number (required) |
| `--status` | New status (transition validated) |
| `--title` / `--objective` / `--area` / `--type` | Update those fields |
| `--priority` | `trivial`, `minor`, `major`, `critical` |
| `--ac` | Append a new criterion (auto id continues the sequence) |
| `--ac-status <id>=<status>` | Set one criterion's status: `pending`, `in-progress`, `test-needed`, `test-created`, `passing`, `waived` |
| `--ac-evidence <id>=<text>` | Set evidence for one criterion (required when status is `passing`) |
| `--test-needed` / `--test-created` / `--test-case` | Append to the test ledger (repeatable) |
| `--comment` | Append a timestamped comment (author `cli`) |

`--comment` is appended, not replaced; existing comments are preserved. Acceptance
criteria are patched by id, so existing criteria are kept unless you append a new one.
The three test-ledger arrays are merged with the stored values, never replaced.

```bash
$ $T update 42 --status active --priority critical
✏️ Ticket #42 updated:
  #42 [ACTIVE] bug — auth — Login fails on empty password
   Acceptance criteria: 2 (pending=2)
   Comment: …            # only if --comment was passed

$ $T update 42 --ac-status A1=passing \
    --ac-evidence "A1=tests/auth.test.ts › empty password" \
    --test-created "tests/auth.test.ts"
✏️ Ticket #42 updated:
   Acceptance criteria: 2 (passing=1, pending=1)
```

Exit codes: `0` updated, `1` not found / invalid transition / invalid type or priority /
invalid criterion (unknown id, invalid status, `passing` without evidence).

---

### `archive`

Force a ticket to `archived`, regardless of its current status.

```
ticket-engine archive <number>
```

```bash
$ $T archive 42
📦 Ticket #42 archived:
  #42 [ARCHIVED] bug — auth — Login fails on empty password
```

Exit codes: `0` archived, `1` not found.

> `archive` sets the status directly. `update --status archived` follows the transition
> table and therefore rejects `active → archived`; use `archive` for that case.

---

### `audit`

Read the append-only audit log (`_audit.json`), or adopt pre-existing tickets.

```
ticket-engine audit [--limit N] [--json] [--baseline]
```

| Flag | Description |
|---|---|
| `--limit N` | Show only the last N entries (positive integer) |
| `--json` | Print the raw JSON array instead of a table |
| `--baseline` | Adopt every ticket file with no audit entry as a `baseline` entry |

Each entry is lean:

```json
{
  "timestamp": "2026-10-26T16:12:03.456Z",
  "operation": "create",
  "ticket_number": 34,
  "hash": "<64 hex chars>"
}
```

`operation` is `create`, `update`, `archive`, or `baseline`.

```bash
$ $T audit --limit 3
Audit log (last 3 of 57 entries):
  2026-10-26T16:12:03.456Z  create   #034  a1b2c3d4e5f6…
  2026-10-26T16:13:10.001Z  update   #034  9f8e7d6c5b4a…
  2026-10-26T16:14:22.900Z  archive  #012  0123456789ab…
```

**`--baseline` is the only adoption path** for tickets created before enforcement
existed. It:

- adds `baseline` entries for un-audited ticket files;
- does **not** modify the ticket files;
- is idempotent (already-audited numbers are skipped);
- refuses to run if the existing audit log is already invalid.

Exit codes: `0` success, `1` unreadable/corrupt audit log or invalid `--limit`.

---

### `validate`

Check the store end-to-end. This is the command to run before committing or after an
agent session.

```
ticket-engine validate
```

It reports five independent things:

1. **Index sync** — every ticket file has an index entry and vice versa.
2. **Lock** — absent (normal), held by a live writer, or stale (dead PID).
3. **Audit** — hashes valid, entries well-formed, timestamps non-decreasing.
4. **Orphans** — ticket files with no audit entry (reported, never deleted).
5. **Acceptance criteria + tests** — every ticket has ≥1 criterion and a complete
   `{needed, created, cases}` ledger; statuses are valid; `passing` has evidence;
   `waived` has a note. Drift warnings are reported without failing.

Example (clean):

```
✅ Index is in sync with ticket files on disk.
✅ Lock: No lock held (normal — the lock only exists during writes)
✅ Audit: Audit log intact (57 entries)
✅ Orphans: none.
✅ Acceptance criteria: 57 ticket(s) checked; all have criteria + test ledgers.
```

Example (needs attention):

```
❌ Index is OUT OF SYNC with ticket files on disk.
   Missing files: /workspace/docs/tickets/ticket-099.json
❌ Lock: Stale lock from dead PID 12345 (acquired …) — safe to clear
❌ Audit: Audit hash mismatch at entry #3 — entry was manually edited
❌ Orphans: 1 ticket file(s) on disk with no audit entry — human review required.
   ↳ ticket-099.json
❌ Acceptance criteria: 1 error(s) across 57 ticket(s).
   ❌ #042 [acceptance_criteria] Criterion A1 is passing but has no evidence
   ⚠️ #043 [tests] Criterion A2 is test-needed but tests.needed is empty
```

Errors fail validation; warnings (⚠️) do not.

Exit codes:

- `0` — index synced, audit valid, no stale lock, no orphans, no policy errors
- `1` — any of: out-of-sync index, tampered/corrupt audit, stale lock, orphans,
  acceptance-criteria/test policy errors

Validation has **no side effects**. It does not clear locks, adopt orphans, or rewrite the
index. `audit --baseline` is the only adoption command.

---

## Global options

| Option | Description |
|---|---|
| `--ticket-dir <dir>` | Override the store directory |
| `TICKET_DIR` (env) | Same, but via environment. The wrapper sets it to `<repo>/docs/tickets` |
| `-h`, `--help` | Show help; exit `0` |

Precedence: `--ticket-dir` > `TICKET_DIR` > `./docs/tickets`.

## Architecture

```
ticket-engine (sh wrapper)
└── src/cli.ts
    ├── parseArgs()      argv → CliArgs
    ├── getTicketDir()   --ticket-dir > TICKET_DIR > ./docs/tickets
    ├── main()           command router
    │   ├── create   → engine.create()
    │   ├── list     → engine.list(area?, status?)
    │   ├── update   → engine.update(number, patch)
    │   ├── archive  → engine.archive(number)
    │   ├── audit    → engine.getAuditLog() / engine.baseline()
    │   └── validate → engine.validate()
    └── formatTicket()   one-line human summary
```

All writes go through the engine's transient lock and append an audit entry. See
[`README.md`](README.md) and [`architecture/DECISIONS.md` §I](../../docs/tickets/architecture/DECISIONS.md).

## File format

Tickets are `ticket-NNN.json` (zero-padded to 3 digits). The index is `_index.json`; the
audit log is `_audit.json`. Full formats: [`SCHEMA.md`](SCHEMA.md).

## Testing

```bash
cd little-mester-mcp
npm test -- cli.test.ts
```

CLI tests spawn `ts-node` per invocation, so they are slower than the engine suite. They
cover create/list/update/archive, `--priority`, `--comment`, `audit` (including
`--baseline`, `--json`, `--limit`), and `validate` exit codes.

## Legacy scripts

These remain for historical reference but are deprecated in favour of the CLI:

| Legacy script | CLI equivalent |
|---|---|
| `scripts/ticket-new` | `ticket-engine create` |
| `scripts/validate-tickets` | `ticket-engine validate` |
| `scripts/ticket-lifecycle list` | `ticket-engine list --status <status>` |
| `scripts/ticket-lifecycle mark-archived` | `ticket-engine archive` |
