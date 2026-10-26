# Ticket Engine CLI

The **Ticket Engine CLI** (`ticket-engine`) provides a command-line interface for managing Jira-like tickets stored as JSON files. It is the programmatic front-end for the ticketing engine (`src/engine.ts`) and is also exposed via the MCP server (`src/mcp-server.ts`).

## Installation

```bash
cd little-mester-mcp
npm install        # install deps
npm run build      # compile TypeScript
```

## Quick start

```bash
# Create a ticket
./scripts/ticket-engine create --area infra --type feature "Add CI pipeline" --objective "Automated builds on push"

# List all tickets
./scripts/ticket-engine list

# List tickets in a specific area
./scripts/ticket-engine list --area auth

# List only active (non-draft) tickets
./scripts/ticket-engine list --status active

# Update a ticket
./scripts/ticket-engine update 1 --status active --priority critical

# Archive a ticket
./scripts/ticket-engine archive 1

# Validate index/files sync
./scripts/ticket-engine validate
```

## Commands

### `create`

Create a new ticket (starts in `draft` status).

```
ticket-engine create --area <area> --type <type> "Title" [--objective "..."]
```

| Flag | Required? | Description |
|------|-----------|-------------|
| `--area` | Yes | Domain area (e.g. `auth`, `infra`, `billing`) |
| `--type` | No | Type: `spike`, `feature`, `bug`, `improvement`, `doc`, `infra`. Defaults to `feature` |
| `--objective` | No | Detailed objective in present/future tense |

**Exit codes:**
- `0` — ticket created successfully
- `1` — error (missing title, invalid type, etc.)

**Example:**
```bash
$ ticket-engine create --area auth --type bug "Login fails on empty password" --objective "Reject empty passwords at the auth layer"
✅ Created ticket #42
  #42 [DRAFT] bug — auth — Login fails on empty password
   Objective: Reject empty passwords at the auth layer
```

### `list`

List tickets with optional filters.

```
ticket-engine list [--area <area>] [--status <status>]
```

| Flag | Required? | Description |
|------|-----------|-------------|
| `--area` | No | Filter by area key |
| `--status` | No | Filter by status: `draft`, `active`, `resolved`, `archived` |

**Example:**
```bash
$ ticket-engine list --area auth --status active
Found 2 ticket(s):
  #42 [ACTIVE] bug — auth — Login fails on empty password
  #43 [ACTIVE] feature — auth — Add OAuth2 login
```

### `update`

Update fields on an existing ticket. Status transitions are enforced per [DECISIONS.md D.3](../../../docs/DECISIONS.md).

```
ticket-engine update <number> [--status <status>] [--title <title>] [--objective <obj>] [--area <area>] [--type <type>] [--priority <priority>]
```

| Flag | Required? | Description |
|------|-----------|-------------|
| `<number>` | Yes | Ticket number (integer) |
| `--status` | No | New status (validated by lifecycle rules) |
| `--title` | No | New title |
| `--objective` | No | New objective |
| `--area` | No | New area |
| `--type` | No | New type |
| `--priority` | No | New priority: `trivial`, `minor`, `major`, `critical` |

**Status transition rules:**
- `draft → active` (start work)
- `draft ↔ active` (bidirectional toggle)
- `active → resolved` (complete work)
- `resolved → draft` (reopen)
- any → `archived` (remove from active cycle)

**Example:**
```bash
$ ticket-engine update 42 --status active --priority critical
✏️ Ticket #42 updated:
  #42 [ACTIVE] bug — auth — Login fails on empty password
```

### `archive`

Archive a ticket (remove from active cycle). Archiving is always allowed from any status.

```
ticket-engine archive <number>
```

**Example:**
```bash
$ ticket-engine archive 42
📦 Ticket #42 archived:
  #42 [ARCHIVED] bug — auth — Login fails on empty password
```

### `validate`

Check that `_index.json` is in sync with the actual ticket files on disk.

```
ticket-engine validate
```

**Exit codes:**
- `0` — index and files are in sync
- `1` — index is out of sync (reports `Missing files` or `Extra index entries`)

## Options (global)

| Flag | Description |
|------|-------------|
| `--ticket-dir <dir>` | Override the ticket directory. Defaults to `./docs/tickets` or the `TICKET_DIR` environment variable |
| `-h`, `--help` | Show help and exit (exit code 0) |

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TICKET_DIR` | `./docs/tickets` | Directory containing ticket JSON files and `_index.json` |

## File format

Each ticket is stored as `docs/tickets/ticket-NNN.json` (zero-padded to 3 digits). The index file `docs/tickets/_index.json` stores metadata for fast queries. See [docs/SCHEMA.md](../docs/SCHEMA.md) for the full JSON schema.

## Architecture

```
ticket-engine CLI
├── parseArgs()       — argv → CliArgs (no library needed)
├── getTicketDir()    — --ticket-dir > TICKET_DIR > default
├── main()            — command router → engine call
│   ├── create → engine.create()
│   ├── list   → engine.list(area?, status?)
│   ├── update → engine.update(number, patch)
│   ├── archive→ engine.archive(number)
│   └── validate→ engine.validate()
└── formatTicket()    — human-readable one-line summary
```

The CLI delegates all business logic to `src/engine.ts` — there is no duplicate logic. The MCP server (`src/mcp-server.ts`) uses the same engine.

## Testing

Run CLI tests with:

```bash
npm test -- cli.test.ts
```

## Migration from legacy scripts

The following legacy shell scripts are deprecated and point to the CLI:

| Legacy script | CLI equivalent |
|---------------|---------------|
| `scripts/ticket-new` | `ticket-engine create` |
| `scripts/validate-tickets` | `ticket-engine validate` |
| `scripts/ticket-lifecycle list` | `ticket-engine list --status <status>` |
| `scripts/ticket-lifecycle mark-archived` | `ticket-engine archive` |
