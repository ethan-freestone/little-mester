# Workflows — how to work through this repo

## Core discipline: doc → test → code → commit

Every change follows four stages. No stage is skipped or reordered.

1. **Doc** — Write or update the spec before any implementation. The spec must be precise enough that a second session could implement it from scratch without asking questions.
2. **Test** — Write failing tests that express what "done" looks like. Tests run *before* code exists and must fail (or assert `expect(1).toBe(0)`-level falseness until the feature passes).
3. **Code** — Implement only what is needed to pass the failing tests. No extra features. No refactoring for its own sake.
4. **Commit** — One clear commit per logical change. Message follows: `type: concise description` (e.g., `fix`, `feat`, `docs`, `chore`, `refactor`).

If a stage blocks you, you do not skip ahead. You write down *why* in the doc and move to the next blocker. Blocks get their own tickets.

## Documenting architecture decisions

Every major architectural direction gets one entry in **one of two files** depending on scope:

| Scope | File | When to write here |
|-------|------|-------------------|
| **How to USE little-mester** (workflow, defaults, guard rails, testing strategy, session handoff) | [docs/DECISIONS.md](../DECISIONS.md) | Whenever a user decision affects how the product is used or improved |
| **How to BUILD** everything in little-mester | [docs/tickets/architecture/DECISIONS.md](tickets/architecture/DECISIONS.md) | Engineering designs for `little-mester-mcp/` (the server). For other subsystems: separate scope files when needed. Per-ticket plans go in ticket docs themselves. |

**If you're unsure which one:** If the answer affects `./lab`, `--unsafe-recurse`, security, testing, or daily workflow → `docs/DECISIONS.md`. If it affects `engine.ts`, `_index.json`, JSON schema, MCP tools, or ticket data model → `docs/tickets/architecture/DECISIONS.md`.

Each entry includes a timestamp so future sessions can distinguish current from historical decisions. If you disagree with a recorded entry, add your objection as a new section with justification — do NOT silently overwrite it.

**Read the correct decision log before ANY session.** For product work: both files (see README working conventions).

## Ticket format and workflow (JSON-first — the engine's truth)

Tickets are stored as JSON in one of these locations depending on routing mode:

| Mode | Path example | Managed via lab |
|---|---|---|
| Same repo (default) | `docs/tickets/` inside the workspace | `little-mester-mcp` local engine |
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
  "acceptance_criteria": [
    {
      "id": "A1",
      "text": "A first-time visitor can register with Google in one flow",
      "status": "test-needed"
    },
    {
      "id": "A2",
      "text": "An existing account is linked instead of duplicated",
      "status": "pending"
    }
  ],
  "tests": {
    "needed": ["integration test for the Google callback"],
    "created": [],
    "cases": []
  },
  "created_at": "2026-10-15T09:00:00Z",
  "updated_at": "2026-10-15T09:00:00Z"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `number` | number | yes | Auto-incremented, unique across all routing modes |
| `area` | string | yes | Domain area grouping (e.g. `auth`, `infra`, `engine`) |
| `type` | string | yes | `feature` \| `bug` \| `improvement` \| `spike` \| `doc` \| `infra` |
| `status` | string | yes | Lifecycle state — see transitions below |
| `title` | string | yes | One-line human-readable summary |
| `objective` | string | no | Detailed objective in present/future tense |
| `acceptance_criteria` | object[] | yes | At least one criterion; ids unique; status is the test-tracking state |
| `tests` | object | yes | `{needed[], created[], cases[]}` paper trail |
| `created_at` | ISO-8601 | yes | Immutable creation timestamp |
| `updated_at` | ISO-8601 | yes | Last modification timestamp |

### Acceptance criteria and tests (required)

Every ticket carries its own definition of done and its test paper trail. **Do not put
acceptance criteria in `objective`, `notes`, or a comment** — they are structured fields
so `validate`, the board, and agents can read them.

Each criterion has `{id, text, status, evidence?, note?}`. The status doubles as the
testing state:

| Status | Meaning | Required |
|---|---|---|
| `pending` | Not started / nothing verified | — |
| `in-progress` | Implementation underway | — |
| `test-needed` | Implemented, test still to write | — |
| `test-created` | Test exists, not confirmed passing | — |
| `passing` | Verified | `evidence` |
| `waived` | No test needed or possible | `note` |

The ticket-level ledger is append-only in spirit: `needed` lists tests to write, `created`
lists test files/cases that exist, and `cases` names the concrete cases (automated or
manually observed). Tests may drift, but never delete the trail — change the criterion
status and evidence instead.

```bash
# Create with criteria and a test ledger
./little-mester-mcp/scripts/ticket-engine create --area auth --type feature \
  --ac "A first-time visitor can register with Google" \
  --ac "An existing account is linked, not duplicated" \
  --test-needed "integration test for the Google callback" \
  "Implement OAuth2 registration"

# Record what changed during implementation
./little-mester-mcp/scripts/ticket-engine update 42 \
  --ac-status A1=passing \
  --ac-evidence "A1=tests/auth.test.ts › google callback" \
  --test-created "tests/auth.test.ts" \
  --test-case "first-time registration"
```

`create` fails without at least one `--ac`. `update` replaces the criteria array through
the CLI patch flags and merges the three test-ledger arrays. `validate` fails on policy
errors and warns on drift (see `docs/DECISIONS.md` §C.4).

### Status transitions

```
draft ──► active ──► resolved ──► archived
  ▲         │            │            │
  └─────────┘            └────────────┘
```

Valid transitions:
| From → To | Valid? | Notes |
|---|---|---|
| `draft → active` | ✅ | Start work |
| `draft → archived` | ✅ | Drop unstarted work |
| `active → resolved` | ✅ | Complete work |
| `active → draft` | ✅ | Return to backlog |
| `resolved → draft` | ✅ | Reopen if needed |
| `resolved → archived` | ✅ | Close for good |
| `archived → draft` | ✅ | Reopen an archived ticket |
| everything else | ❌ | `archive` command force-sets `archived` |

### Ticket numbering

Tickets are numbered **globally** across all routing modes. Number collision is a hard error — always claim the next available number before creating a ticket file. The engine (`_index.json`) tracks what's been issued.

### Legacy markdown tickets (archived)

Old markdown-format tickets remain in `docs/tickets/archive/` for reference only. They are NOT processed by the engine. New work gets JSON format.

## VM/Docker test environment (deferred)

The disposable test environment will be a **full Linux VM with Docker and opencode**
(decision: `docs/DECISIONS.md` §C.6). It is not provisioned yet: the current development
container has no Docker CLI, no daemon socket, and no podman. Ticket #35 owns the
provisioning runbook, the command matrix, the negative cases, and teardown evidence.

Rules for that future work:

- Run everything against a disposable workspace (`workspaces/test-<id>` or a fresh
  clone), never the real checkout.
- Exercise `./lab`, compose, `--unsafe-recurse`, the ticket-engine CLI, and the MCP tools
  end to end; record the observed effect on #35.
- Tear down containers, networks, volumes, and the workspace, then confirm
  `git status --short` is empty in the real checkout.

Do not start the VM work until a Docker-capable host is available and #35 is picked up
as its own session.

## MCP development workflow (for building modules like @little-mester/ticket-engine-mcp)

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
