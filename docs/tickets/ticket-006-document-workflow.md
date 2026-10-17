# Ticket 006 — Document workflow & update checklists

## Objective
Update all relevant documentation so users understand the new defaults, guardrails, and how to safely improve little-mester itself.

## Phase tracking

| # | Task | Status | Blocking on |
|---|------|--------|-------------|
| 6.1 | Update README.md default agent references → opencode | ❌ TODO | Depends on ticket-003 being fully accurate — this was partially addressed in the README cleanup (self-referencing) |
| 6.2 | Add self-edit section to README.md (`lab-dev-work` workflow docs) | ❌ TODO | Blocked: depends on `scripts/lab-dev-work` being created by ticket-005 |
| 6.3 | Add Self-Editing Safety section to SECURITY.md | ❌ TODO | Depends on 6.2 (needs lab-dev-work reference) |
| 6.4 | Create `skills/testing_workflow.md` + doc update in ticket-004 Phase D | ❌ TODO | Requires creating `skills/` directory first |
| 6.5 | Update journal.md with restructuring entry | ✅ DONE | Journal entry for "Restructuring to tickets" already exists per ticket-006's own design |

## Notes on prior work

The README cleanup (done in this session) addressed several items that were originally scoped here:
- Removed 2 of 3 duplicate "Repo map" sections
- Fixed `skills/` path reference to note it as "planned, not yet created"
- Marked learning path items (my-agent/, evals/, red-team/, proxy/, mcp/) as *not yet created*
- Clarified `arch-*.md` file convention as advisory rather than existing files
## README.md updates

### Update default agent mention
Change any reference that says "default agent is aider" to "default agent is opencode (with internet)".

### Add self-edit section
Add a new section after the "Verify Sandbox" or "Configuration" section:

```markdown
## Self-Improvement (Editing Little Mester)

To safely modify little-mester itself:

1. **Start a session:** `./lab-dev-work init`
   - Clones repo to a temp directory, creates feature branch.
   
2. **Make changes in the temp workspace.** Tests can be run with `bats tests/`.

3. **Verify your changes:** `./scripts/lab-dev-work verify`
   - Runs all existing tests plus checks for test coverage of new code.
   - Must pass before sync proceeds.

4. **Commit & push:** `./scripts/lab-dev-work commit && ./scripts/lab-dev-work push`

5. **Sync to main:** `./scripts/lab-dev-work sync`
   - Merges feature branch into main with TDD gate enforced.
   - Uses `--pr` flag: `./lab-dev-work --pr sync` creates a PR instead of auto-merge.

### Safeguards
- **TDD Gate:** Tests must pass before any merge proceeds (auto-enforced).
- **--unsafe-recurse:** Use only when deliberately editing little-mester; the guard is lifted by name with explicit acknowledgment.
- **Human confirmation:** Every sync requires human review before changes land in main.

### Default agent is opencode
From now on, `./lab` defaults to launching `opencode` (with internet enabled). To use aider: `./lab --aider ./workspace`.
```

---

## SECURITY.md updates

Add a "Self-Editing Safety" section:

```markdown
## Self-Editing Safety

When modifying the little-mester codebase itself, all changes must pass through the TDD gate (ticket-004) which enforces:
1. All existing tests must pass.
2. New functionality must include new tests.
3. Sync requires human confirmation.

Direct edits or bypass attempts are blocked by `check_workspace` and the `--unsafe-recurse` flag requirement.
```

---

## journal.md updates

Add an entry summarizing this restructuring:

```markdown
## 2026-10-15 — Restructuring to tickets

Pivoted from a fixed linear plan into independent ticketed work items under `docs/tickets/`.
Each ticket is standalone, references others where applicable.

### New conventions:
- Default agent changed to opencode (with internet enabled by default).
- Added `--unsafe-recurse` flag for deliberate self-editing.
- Created lab-dev-work helper for safe self-modification cycle.
- Added TDD gate as a hard requirement before any merge.

### Tickets created:
1. `ticket-001-bats-inside-agent.md` — bats-core packaging
2. `ticket-002-default-access-and-guardrails.md` — defaults and --unsafe-recurse
3. `ticket-003-lab-dev-work.md` — safe self-edit helper
4. `ticket-004-tdd-gate.md` — mandatory testing before merge
5. `ticket-005-document-workflow.md` — docs & checklists (this file)
```

---

## Files modified
| File | Change |
|------|--------|
| `README.md` | Add self-edit workflow section, update default agent mention |
| `SECURITY.md` | Add Self-Editing Safety section |
| `docs/journal.md` | Record restructuring and new conventions |
