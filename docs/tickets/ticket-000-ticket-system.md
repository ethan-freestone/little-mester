# Ticket 000 — enforce consistent, verifiable ticket creation & style

## Objective
Document the little-mester internal ticket system and ensure all future tickets follow a strict format that can be auto-validated.

---

## Why this exists

Without a ticketing standard, ad-hoc improvements produce undocumented work that is:
- Hard to track or revert
- Missing testing requirements
- Drifts from the intended workflow (init → edit → verify → sync)
- Impossible for the TDD gate or audit trail to function

## How it works

1. Every self-improvement ticket lives under `docs/tickets/`.
2. Each ticket MUST pass schema validation via `scripts/lab-dev-work validate-tickets` before the commit that added it is accepted.
3. The validation script blocks sync if any ticket in the system fails its checks.
4. Future automated work by little-mester must create new tickets through the same validated path.

---

## Schema definition

All tickets MUST conform to `docs/tickets/meta-ticket-schema.json`.

### Required structure

| Field | Position | Constraint |
|-------|----------|-----------|
| Heading | Line 1 | `# Ticket NNN — description` (3-digit zero-padded number, en-dash with space) |
| `## Objective` | Next heading | ≤ 1 short sentence. Present or future tense only. No bullets. |
| `## Scope & Boundaries` | After Objective | Must include "Do not" line(s). At least one boundary constraint. |
| `### File` (to create/edit) | Within sections | Relative path from repo root. New files marked `(new)` |
| `### Steps` | Ordered | Each step must be a concrete action with a target file or command. |
| `### Verification` | Required section | At least one test, command, or inspection that proves success/failure. |
| `### Files` (summary table) | Last section | Every changed/created file gets an entry with role. No omissions. |
| `## Dependencies` (optional) | Before last section | Ticket numbers this ticket relies on (e.g., "Depends on: 001"). |

### Naming rules

- File name: `ticket-NNN-short-description.md`
- `-NN-`: three-digit zero-padded number starting from 000
- `short-description`: kebab-case, max 6 words, must start with action verb where possible (add, create, update, enforce, fix)

### Content rules

- No paragraphs longer than 5 sentences in any section
- Every "must/should/may" maps to a verifiable condition somewhere in the ticket
- Sections appear strictly in this order, case-sensitive:
  1. `# Ticket NNN — description` (heading)
  2. `## Objective`
  3. `## Scope & Boundaries`
  4. Step sections (`### Part X` or named subsections)
  5. `## Files` (summary table)
  6. `## Dependencies` (if any, must come after Files)

---

## Validation rules (`validate-tickets`)

The following checks run on every invocation of `scripts/lab-dev-work validate-tickets`:

1. **Heading format** — line 1 matches `^# Ticket (0[0-9]|[1-9][0-9]+) — .+$`
2. **Number continuity** — no gaps in numbering across all `docs/tickets/ticket-*.md` files; sequence starts at 000 or 001
3. **File name matches number** — `ticket-NNN-*` must have NNN matching the heading number
4. **"Do not" lines** — if section "Scope & Boundaries" exists, must contain ≥1 line with `Do not` or `Do NOT`
5. **Sections ordered** — sections appear in required order; if out of order → error with fix suggestion
6. **Verification present** — ticket must contain a section (heading) ending in or called "Verification" or include at least one command-style block (`bash` code fence or indented code block with a command prefix like `$ `)
7. **Files table completeness** — every file referenced in steps appears in the Files table
8. **Length limit** — no section block (between headings) exceeds 100 lines
9. **No unresolved TODOs** — ticket must not contain uncompleted TODO or FIXME markers unless explicitly marked as known issues with a reference to another ticket

### Auto-fix capability

`scripts/lab-dev-work validate-tickets --fix` attempts:
- Re-number sections with correct zero-padded number (changes heading and filename)
- Fix heading separator (`— ` → en-dash)
- Move common sections into required order
- Add missing "Do not" lines where Scope & Boundaries exists but is empty

Auto-fix rewrites files in-place after printing what changed. User confirmation required before write.

---

## Integration with lab-dev-work

### Pre-commit enforcement

`scripts/lab-dev-work commit` (or the internal `lab-dev-work` wrapper for automated commits) runs:
```bash
if ! scripts/lab-dev-work validate-tickets --strict; then
  die "Cannot commit: ticket validation failed. Run './scripts/lab-dev-work validate-tickets' for details."
fi
```

### Sync enforcement

`scripts/lab-dev-work sync` also validates all tickets in the workspace at merge time. If any ticket fails, it refuses to merge until fixed. The user is told which specific tickets are broken and the exact errors.

### Automated ticket creation by little-mester itself

When little-mester decides on its own improvement work, it MUST:
1. Create a new `docs/tickets/ticket-NNN-description.md` following all rules above.
2. Write the Objective and Scope & Boundaries before any steps.
3. Include Verification with at least one runnable command or test expectation.
4. List every changed file in the Files table.
5. Pass `validate-tickets --strict` (no auto-fix allowed for automated creation — must be manually reviewed).

Failure to create a valid ticket is treated as a refusal condition: little-mester does NOT proceed with the work until the ticket exists and validates cleanly.

---

## Enforcing enforcement of ticket standards

There are TWO layers that validate each other:

1. **`validate-tickets`** → checks individual ticket conformance against schema rules.
2. **`scripts/lab-dev-work sync`** → calls `validate-tickets` before allowing any merge, ensuring every ticket in the directory passes.

If a developer (or AI agent) tries to bypass validation:
- The TDD gate (ticket-004) will catch skipped tests and block at a later stage.
- But without validated tickets, the audit trail is broken and work becomes untrackable — which is its own failure mode. Both guards are independent and neither replaces the other.

---

## Files

| File | Role |
|------|------|
| `docs/tickets/ticket-000-ticket-system.md` (new) | This meta-ticket — defines the system and all rules |
| `docs/tickets/meta-ticket-schema.json` (new) | Machine-readable schema for validation script to consume |
