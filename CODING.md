# CODING.md — Codex workspace instructions

Read this file at session start. It contains workspace-invariant rules that apply regardless
of Codex's native capabilities or permissions.

---

## TICKET EDIT RULE (MANDATORY)

**NEVER directly edit, create, write, or delete files under `docs/tickets/*.json`.**

All ticket operations — create, update, archive, validate, audit, status changes, criterion
edits, test additions, evidence updates — MUST go through:

- **CLI:** `./little-mester-mcp/scripts/ticket-engine <action> ...`
- **MCP tools:** `mester_tickets_*` tools (if available in session)

### Why

The ticket store (`docs/tickets/`) has derived files (`_index.json`, `_audit.json`) and a
PID lock (`.engine.lock`). Direct edits bypass the engine, causing silent drift in the index,
missing audit entries, and potential data corruption. This is a workspace invariant that
applies to every agent — not a preference.

### What's allowed

- **Reading** ticket files — `cat`, `grep`, editor views. Reading is always fine.
- **Editing files outside** `docs/tickets/` — product code, agent configs, docs, scripts.

### What requires the engine

- Creating, modifying, or deleting any `docs/tickets/ticket-NNN.json`
- Changing any field: status, acceptance criteria, tests, evidence, comments
- Archiving a ticket
- Modifying `_index.json` or `_audit.json`

### If already corrupted

1. Run `./little-mester-mcp/scripts/ticket-engine validate` to assess drift.
2. Delete `_index.json` and re-run `validate` to rebuild.
3. Do NOT repair by hand. Stop and ask a human if audit entries are missing.

---

## Development workflow

- Follow **doc → test → code → commit**. No stage skipped.
- Use conventional commits: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`.
- The workspace root is `/workspace`. The little-mester project lives here.
