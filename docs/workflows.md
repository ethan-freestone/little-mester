# Workflows — how to work through this repo

## Core discipline: doc → test → code → commit

Every change follows four stages. No stage is skipped or reordered.

1. **Doc** — Write or update the spec before any implementation. The spec must be precise enough that a second session could implement it from scratch without asking questions.
2. **Test** — Write failing tests that express what "done" looks like. Tests run *before* code exists and must fail (or assert `expect(1).toBe(0)`-level falseness until the feature passes).
3. **Code** — Implement only what is needed to pass the failing tests. No extra features. No refactoring for its own sake.
4. **Commit** — One clear commit per logical change. Message follows: `type: concise description` (e.g., `fix`, `feat`, `docs`, `chore`, `refactor`).

If a stage blocks you, you do not skip ahead. You write down *why* in the doc and move to the next blocker. Blocks get their own tickets.

## Documenting architecture decisions

Every major architectural direction gets one file inside `docs/` named `arch-<name>.md`.

Each includes:
- Context — what problem we're solving
- Decision — what was chosen and why
- Consequences — trade-offs, what it blocks or enables
- Related docs — links to any tickets, blueprints, or specs that inform this

Any time a new arch decision is made, the following files are **always** updated as part of the same commit:
- The `arch-*.md` for the new decision itself
- `README.md` (top-level architecture section)
- Any `blueprint.md` or schema doc that references the old approach

The rule: **grep first.** Before committing any arch change, search (`grep`) every markdown file in the repo for references to outdated concepts. Update all of them. Failure to update a stale reference is itself a bug.

## Ticket format and workflow (JSON-first — the engine's truth)

Tickets are stored as JSON in one of these locations depending on routing mode:

| Mode | Path example | Managed via lab |
|---|---|---|
| Same repo (default) | `docs/tickets/` inside the workspace | `ticket-engine-mcp` local engine |
| Any directory | `/any/path/on/disk` — external to git repo, mounted at lab start | `lab --override ticket_engine_dir=/path` |
| Separate repo | `/path/to/other-repo/docs/tickets/` — different git repo, synced via lab flag | `lab --override ticket_engine_repo=/path --sync` |

### Ticket JSON schema (`ticket-NNN.json`)

```json
{
  "number": 42,
  "area": "auth",
  "type": "feature",
  "status": "draft",
  "title": "Implement OAuth2 flow for user registration",
  "objective": "Users can register via Google OAuth on first visit without manual credentials",
  "created_at": "2026-10-15T09:00:00Z",
  "updated_at": "2026-10-15T09:00:00Z"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `number` | number | yes | Auto-incremented, unique across all routing modes |
| `area` | string | yes | Domain area grouping (e.g. `auth`, `infra`, `engine`) |
| `type` | string | yes | `feature` \| `bug` \| `infra` \| `pause` |
| `status` | string | yes | Lifecycle state — see transitions below |
| `title` | string | yes | One-line human-readable summary |
| `objective` | string | no | Detailed objective in present/future tense |
| `created_at` | ISO-8601 | yes | Immutable creation timestamp |
| `updated_at` | ISO-8601 | yes | Last modification timestamp |

### Status transitions

```
draft ──► active ──► resolved ──► archived
  ▲         ╲       ↗
   └─────────┘────/
```

Valid transitions:
| From → To | Valid? | Notes |
|---|---|---|
| `draft → active` | ✅ | Start work |
| `active → resolved` | ✅ | Complete work |
| `resolved → draft` | ✅ | Reopen if needed |
| any → `archived` | ✅ | Terminal — read-only from this point |
| `draft ↔ active` | ✅ | Bidirectional toggle for unstarted work |

### Ticket numbering

Tickets are numbered **globally** across all routing modes. Number collision is a hard error — always claim the next available number before creating a ticket file. The engine (`_index.json`) tracks what's been issued.

### Legacy markdown tickets (archived)

Old markdown-format tickets remain in `docs/tickets/archive/` for reference only. They are NOT processed by the engine. New work gets JSON format.

## MCP development workflow (for building modules like ticket-engine-mcp)

When building or updating an MCP module:

1. Write `docs/blueprint.md` describing the intent, schema, and routing mode.
2. Implement engine logic **first** (all business rules as unit tests).
3. Register MCP tools **second** (thin wrappers over engine methods).
4. Add TDD gate ticket — no code ships without passing tests first.

> Note: A dedicated workflow MCP extension is a future concept for automating the doc→test→code→commit cycle itself within opencode. Not implemented yet.

## Session handoff template

When a session ends mid-task, include this at the top of any tracker or doc:

```
## Current status
- What's done: ...
- What's in progress: ...
- Blocked on: ...
```

Next session reads that first and does not re-implement already-done work.
