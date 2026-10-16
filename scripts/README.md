# scripts — little-mester automation tools

All scripts use `#!/usr/bin/env sh` (POSIX sh). Sourced files in `lib/` are never executed directly.

## Core scripts

| Script | Purpose | Usage |
|---|---|---|
| `validate-tickets` | Validate all tickets against schema rules (9 checks) | `./scripts/validate-tickets [--strict] [--fix] [-h|--help]` |
| `ticket-lifecycle` | List/restore/cleanup/move tickets by lifecycle state | `./scripts/ticket-lifecycle [list\|restore\|cleanup\|move] [--state <state>]` |
| `ticket-new` | Scaffold a new ticket file formatted to pass validation | `./scripts/ticket-new [--number NNN] <description>` |
| `sync-helper` | Wrap PR open + validate-tickets pre-merge guard | `./scripts/sync-helper [--target main] [--dry-run] [-h|--help]` |
| `commit-helper` | Git commit with validation gate + ticket update | `./scripts/commit-helper <message> [--add-ticket-ref]` |

## Helper scripts

| Script | Purpose | Usage |
|---|---|---|
| `lib/check-syntax.sh` | Syntax check all or specific scripts via `sh -n` | `./scripts/lib/check-syntax.sh [--all] [script …]` |
| `lib/common.sh` | Shared logging, colors, die helpers (sourced only) | `. "$(dirname "$0")/lib/common.sh"` |

## Ticket directory

- Active tickets: `docs/tickets/ticket-[NNN]-*.md`
- Archived: `docs/tickets/*[Aa]rchive/*/`
- Discarded analysis: `docs/tickets/ticket-discarded-scripts-analysis.md`
- Schema: `docs/tickets/meta-ticket-schema.json`

## Workflow for agents (little-mester)

1. **Before editing tickets**: run `./scripts/validate-tickets --strict` and fix all failures first.
2. **When creating a new ticket**: use `ticket-new` — it auto-numbers and generates scaffolding that passes validation.
3. **Before syncing to target branch**: run `sync-helper` — blocks if any ticket fails strict validation.
4. **Before committing code**: run `commit-helper` with a message; it validates tickets and optionally references the ticket number.
5. **After any script change**: run `./scripts/lib/check-syntax.sh --all`.
