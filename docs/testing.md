# Testing & Ticket Workflow Decisions (C)

> **What goes in here:** Constraints about ticket writing, acceptance criteria, merging, testing policy, and test environments. These decisions affect how you work with the ticket engine — not engineering subsystem designs (those go in [docs/tickets/architecture/DECISIONS.md](tickets/architecture/DECISIONS.md)).
>
> **Read before every session:** [docs/DECISIONS.md](DECISIONS.md) §⚠️ Read before every session.

---

## C. Constraints (what's true, not designs)

### C.1 Testing status (updated 2026-10-26)
The ticket engine now has a working Jest suite: **143 tests** (54 engine, 57 CLI, 32 MCP) under `little-mester-mcp/tests/`. Run it with `cd little-mester-mcp && npm test`. This resolves the earlier engine-testing blocker. Host-side Bats tests still require `bats-core` (see the root README's Testing section).

### C.2 SSH / git auth — OUT OF SCOPE (now)
Container-native SSH not managed here. Token-based or GitHub CLI auth is recommended path for any external git operations. Separate future ticket when needed.

### C.3 Ticket writes go through the engine (2026-10-26)
**Rule:** Never create, edit, move, or delete ticket data by hand. Use the engine:

- CLI: `./little-mester-mcp/scripts/ticket-engine ...` (`create`, `update`, `archive`, `audit`, `validate`)
- MCP: the `mester_tickets_*` tools

This covers `docs/tickets/ticket-NNN.json`, `_index.json`, and `_audit.json`. The engine
owns the write window, updates the index, and appends an audit entry.

**Why:** The index and audit log are derived data. A hand edit makes them silently wrong.
The engine cannot stop you, but `validate` will show the drift, a tampered audit entry, a
stale lock, or an orphan file.

**If `validate` complains:**
1. **Do not delete anything automatically.**
2. Read the report. Orphans are ticket files with no audit entry — they need a human
   decision.
3. To adopt a pre-existing batch once, run `./little-mester-mcp/scripts/ticket-engine audit --baseline`.
   This records them as `baseline` entries; it does not change the ticket files.
4. A stale lock is safe to clear only after checking the PID is gone. The engine clears
   dead-PID locks itself on the next write.

Engineering details and invariants: [docs/tickets/architecture/DECISIONS.md](tickets/architecture/DECISIONS.md) §I.

### C.4 Acceptance criteria and tests are first-class (2026-10-26)
**Rule:** Every ticket MUST have at least one **acceptance criterion** and a **test ledger**.
The engine enforces this at create/update time and `validate` fails when it is missing.

Acceptance criteria are an array of objects, not prose in `objective` or a comment:

```json
"acceptance_criteria": [
  {
    "id": "A1",
    "text": "A user with valid credentials receives a session token",
    "status": "passing",
    "evidence": "tests/auth.test.ts › valid credentials"
  },
  {
    "id": "A2",
    "text": "A wrong password is rejected without leaking whether the account exists",
    "status": "waived",
    "note": "Requires a browser session; manual QA only for now"
  }
]
```

The criterion **status is the single place to track testing**, because "test needed",
"test created", and "test passing" are states of the same criterion:

| Status | Meaning |
|---|---|
| `pending` | Not started; nothing verified yet |
| `in-progress` | Implementation underway |
| `test-needed` | Implemented; a test still needs to be written |
| `test-created` | A test exists but has not been confirmed passing |
| `passing` | Verified. `evidence` is **required** (test path/case, command + result, or artifact) |
| `waived` | No test is needed or possible. `note` is **required** explaining why |

The ticket-level test ledger records the paper trail:

```json
"tests": {
  "needed": ["test that an empty password is rejected"],
  "created": ["tests/auth.test.ts"],
  "cases": ["valid credentials", "wrong password", "empty password"]
}
```

**Write rules:**
- `create` requires at least one criterion. Bare strings are normalized to
  `{id: "A1"…, status: "pending"}`.
- `update` replaces the acceptance-criteria array (so send the full array when editing
  through the API). `tests.needed`, `tests.created`, and `tests.cases` are **merged**,
  never replaced: a later edit cannot erase the paper trail.
- Tests may drift from reality, but the trail must remain. A stale `passing` claim is
  corrected by changing the status and evidence — not by deleting the criterion.
- `validate` **fails** on missing criteria/ledger, invalid/duplicate ids, invalid status,
  `passing` without evidence, or `waived` without a note. It **warns** (does not fail) on
  drift: `test-needed` with an empty `needed` list, `test-created`/`passing` with an empty
  `created` list, or a `resolved` ticket with unverified criteria.

**Why:** "Done" becomes checkable by a human or an agent. Without a criterion there is no
definition of done; without a test ledger there is no way to tell whether the work was
verified or merely claimed.

Schema: [`little-mester-mcp/docs/SCHEMA.md`](../little-mester-mcp/docs/SCHEMA.md) and
[`ticket-v1.json`](../little-mester-mcp/docs/ticket-v1.json). Engineering invariants:
[`docs/tickets/architecture/DECISIONS.md`](tickets/architecture/DECISIONS.md) §J.

### C.5 Merging the ticket store (2026-10-26)
**Rule:** Ticket content merges like normal source. Derived files have their own rules.

| File | Merge rule |
|---|---|
| `ticket-NNN.json` | Normal three-way merge. Resolve content conflicts in the file, then run `validate`. Never resolve a conflict by deleting a ticket. |
| `_index.json` | **Never hand-merge.** It is derived. Delete it and run any engine command (for example `ticket-engine validate`) to reconstruct it from the ticket files, then commit the rebuilt index. |
| `_audit.json` | **Append-only.** On conflict, keep the entries from both sides with `ticket-engine audit --merge <their-log>`. The command verifies both logs, de-duplicates, orders by timestamp, and refuses to write an invalid log. Do not delete, edit, or renumber entries by hand. |
| `.engine.lock` | Never commit. It is a transient PID lock; `.gitignore` excludes it. A lock present after a crash is reported as stale by `validate`. |

**Resolving the two generated files:**

```bash
# Index: derived, so delete it and let the engine rebuild from ticket files.
rm docs/tickets/_index.json

# Audit: keep your side, then merge the other branch's entries through the engine.
git checkout --ours docs/tickets/_audit.json
git show :3:docs/tickets/_audit.json > /tmp/theirs-audit.json
./little-mester-mcp/scripts/ticket-engine audit --merge /tmp/theirs-audit.json
./little-mester-mcp/scripts/ticket-engine validate
```

**After a ticket-store merge:**
1. Run `./little-mester-mcp/scripts/ticket-engine validate`.
2. If an audit entry is missing, treat the ticket file as an **orphan**. Stop and ask a
   human. Do not run `audit --baseline` to hide a dropped entry; that command is only for
   adopting a store that predates enforcement.
3. Commit the rebuilt `_index.json` and the unioned `_audit.json` with the content merge.

**Why:** `_index.json` is a cache and must never be a source of truth. `_audit.json` is
evidence and must never lose a branch's writes silently. This keeps a merge auditable
instead of producing a plausible-looking but incomplete history.

### C.6 VM/Docker test environment (2026-10-26)
**Decision:** The disposable test environment is a **full Linux VM** with Docker, the
repo checkout, and opencode installed inside it. Docker-in-Docker and a shared remote
daemon were rejected: DinD needs privileged mode, and a remote daemon gives weaker
isolation because its containers, networks, and volumes are shared with other work.

- The **repo checkout is never the test target**. Tests run in a disposable workspace
  (`workspaces/test-<id>` or a fresh clone) so `--unsafe-recurse`, compose, and
  destructive lab cases cannot touch the developer's working tree.
- Fresh provisioning is one documented runbook: install Docker (and the NVIDIA toolkit
  when GPU-enabled), clone the repo, build the agent image, then run `./verify-lab.sh`.
  A CPU-only host is supported; GPU tooling is optional.
- Teardown removes the workspace's containers, networks, and volumes, then deletes the
  disposable workspace. `git status --short` in the real checkout must be empty.
- The VM supplies the Docker daemon that this development container lacks. Provisioning,
  command-matrix, negative-case, and teardown evidence belongs on ticket #35.

### C.7 Merge scope for `feat/ticket-context` (2026-10-26)
**Decision:** Merge the **whole `feat/ticket-context` branch** into `main` rather than
extracting an engine-only commit set. The branch is the reviewed unit: it carries the
engine, the ticket store, the AC policy, CI, and the config/docs work that depends on
them. Extracting a subset would split related history and make the ticket-store merge
harder to audit.

The module still has to build **independently** from its own package directory. A clean
clone of the branch passes `npm ci`, `npm run build`, `npm run check:package`, the full
Jest suite, and `ticket-engine validate`; those results are recorded on ticket #36.
Merge is blocked until #35's VM evidence and #36's remaining criteria are complete.
