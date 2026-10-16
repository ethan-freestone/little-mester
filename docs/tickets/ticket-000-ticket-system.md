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

## Ticket lifecycle

Tickets move through a defined set of states — they never just "hang around" clogging context forever. Completed work gets documented properly elsewhere, then the ticket migrates up the chain into archival.

### States

| State | Marker | Validation | Visibility |
|-------|--------|-----------|------------|
| `active` | `NNN-name.md` (default) | Checked by validate-tickets | Daily context |
| `merged` | `[MERGED] NNN-name.md` | Skipped validation | Visible, linked to merge commit |
| `superseded` | `[SUPERS] NNN-name.md` | Skipped + notes why | Archived, one-line reference only |

### Naming convention for non-active tickets

Files in either `merged` or `supreserved` state get a bracket prefix:
- Merged work: `[MERGED] NNN-description.md`
- Superseded: `[SUPERS] NNN-description.md`

The number still gets zero-padded. The merge commit hash (or PR URL) MUST appear as the first line of the `[MERGED]` ticket so it's always traceable.

### Lifecycle transitions

```
active ──(work merged to main)──→ [MERGED]
[Mergered] ──(older than 30 days)──→ move to `docs/tickets/archive/area/`
[Superseded by NNN for reason.] ──→ [SUPERS] (one line, then archive after 7 days)
```

- **Merged**: When a ticket's work is merged to `main`, add `[MERGED] <commit-or-pr-hash>` prefix and mark it in-place. The validation script will ignore it going forward.
- **Superseded**: If another ticket replaces this one entirely (e.g., changed scope, new approach), the original ticket gets marked `[SUPERS] NNN — reason` and validation stops immediately.

### Archive structure

Archived tickets are grouped under `docs/tickets/archive/area/<feature-area>/` so that dormant features can be easily reconstituted:
```
docs/tickets/
├── 006-new-feature.md           ← active ticket in main directory
├── [MERGED] 001-bats-inside-agent.md   ← merged, moved to archive after 30 days
├── [SUPERS] 002-default-access.md      ← superseded, moved to archive after 7 days
└── archive/
    └── egress-area/             ← "area" group for a feature or work stream
        ├── 001-bats-inside-agent.md   ← [MERGED] prefix was dropped in archive
        ├── 002-default-access.md      ← [SUPERS] prefix also stripped there
        └── 015-egress-refactor.md     ← archived while dormant, easily reactivated
```

#### Archive rules

1. **When archiving**: Move `[MERGED]` tickets to archive after >30 days since merge date. Move `[SUPERS]` tickets after >7 days.
2. **Area naming**: Each area is a feature- or work-stream-level grouping, named by the primary focus (e.g., `egress-area`, `testing-area`, `access-control`). The name must be chosen when the archive directory is first created and can be reused across different work streams even if they were initially planned differently.
3. **Reactivation**: If a dormant feature in an area's backlog is reopened, all its archived tickets can be restored to the active directory:
   - `scripts/lab-dev-work tickets restore --area egress-area` restores all from that area.
   - Active ticket numbers get re-established (renumbered to fill gaps).
4. **Historical queries**: `scripts/lab-dev-work tickets list --archived --area egress-area` lists what's archived under a specific area without cluttering active context.
5. **Count continuity**: Only files matching `ticket-NNN-*` are counted for number-gaps validation. `[MERGED]`, `[SUPERS]`, and files in `archive/` are excluded from gap checks — the next active ticket gets assigned the natural continuation of that numbering (e.g., if last active is 014, next creates 015).

### Merging a completed ticket

When a feature's tickets are merged:
1. Update each `[MERGED] <commit/PR>` line with merge reference.
2. The `sync` command auto-tracks merge date for 30-day archival scheduling.
3. Run `scripts/lab-dev-work tickets cleanup` to move eligible merged/superseded tickets into archive (respects area grouping).

Run `cleanup` every sprint or before major releases to keep active context lean.

---

## Script architecture pattern

All little-mester tooling follows a modular, single-responsibility design. Each script is callable independently as `scripts/<name> --flag value` regardless of context. No script reads another via subprocess — each is self-contained with its own arg parsing, validation, and output format.

### Rules

1. **One purpose per script.** Validation lives in `validate-tickets`. Lifecycle management (`list`, `restore`, `cleanup`) lives in `ticket-lifecycle`. Each tool does one thing well and can be called standalone by opencode or CI.
2. **CLI convention:** Every script parses `--flag value` args (short `-f val` supported), outputs structured results to stdout, errors to stderr, and exits non-zero on failure. Exit codes: 0=success, 1=failure/validation error, 2=bad input, 3=config not found.
3. **No shared state between scripts.** Each discovers its own working directory from `SCRIPT_DIR`. Scripts discover the repo root by walking up to find `.git/`, not by assuming `$REPO_ROOT=../..` (which breaks when called from different CWDs).
4. **Common utilities** (shared logging/colors/die) live in `scripts/lib/common.sh` and are sourced via `source "$(dirname "$0")/lib/common.sh"` — but this is an *optional* optimization, not required. Simple scripts can just define their own helpers inline.
5. **No orchestration shell.** There is no single "master" script that calls all others. Each tool is called directly by its user (opencode session, human operator, CI job). If composition is needed, the *caller* orchestrates, not a middle-layer script.

### Script inventory (planned)

| Script | Purpose | Called from |
|--------|---------|-------------|
| `validate-tickets` | Validate ticket conformance against schema rules | Opencode pre-commit, CI gate |
| `ticket-lifecycle` | list/restore/cleanup tickets per lifecycle states | Manual cleanup, opencode workflow |
| `commit-helper` | Wrap git commit with validation gate + ticket update | Opencode post-work flow |
| `sync-helper` | Wrap PR open + validate-tickets pre-merge guard | Opencode sync phase |
| `ticket-new` | Scaffold a new valid ticket file (number, header, sections) | Opencode self-ticket creation |

### Draft / out-of-scope items

These are planned but not numbered — they track ideas without polluting the active ticket sequence:

- **CI workflow integration** — auto-detect `.github/workflows/` existence and scaffold a `validate-tickets.yml` gate. Out of scope for today, implement when someone explicitly requests CI setup.
- **Ticket search** (`ticket-search`) — fuzzy find archived tickets by keyword. Useful but low priority until archival has accumulated enough entries to matter.

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
