# Ticket 005 — lab-dev-work safe self-edit helper

## Objective
Create a script that lets little-mester modify itself safely via git ops (no workspace mounting inside the container), then sync changes back through the standard `./lab` flow.

---

## Problem this solves
The agent cannot edit `./lab` directly because `check_workspace` blocks it — which is correct and desired for safety. But how does little-mester improve itself? This script provides the bridge.

---

## Design: temp-clone → work → sync

```
1. ./lab-dev-work init    — Clone to $TMPDIR, create branch lab-dev/YYYYMMDD-HHmmss, echo next steps
2. Agent operates on that temp clone in /tmp
3. ./lab-dev-work commit  — Stage + conventional-commit on the feature branch
4. ./lab-dev-work sync    — Push branch → PR/worktree merge → rebase into original workspace
```

---

## Script spec

### File to create
`scripts/lab-dev-work`

### Shebang & conventions
```bash
#!/usr/bin/env bash
set -euo pipefail
die() { echo "REFUSED: $*" >&2; exit 1; }
```

### Variables
```bash
LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP_CLONE_DIR=""
BRANCH_NAME=""
DRY_RUN=0

# Git identity from host env or defaults
GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-little-mester-agent}"
GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-agent@localhost}"
GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME:-little-mester-agent}"
GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL:-agent@localhost}"
```

---

## Subcommands

### init
- Creates `$TMPDIR/little-mester-dev-<timestamp>`.
- Clones the repo there (or uses existing if present).
- Creates branch: `lab-dev/$(date +%Y%m%d-%H%M%S)`.
- Checks out that branch.
- Prints the path and next instructions to the user.
- Saves metadata (`$TMPDIR/.lab-dev-workspace`) so other subcommands know where the clone is and what branch was used.

**Safety checks in init:**
- Abort if real repo has uncommitted/unstaged changes (prevents data loss).
- Abort if already in a lab-dev session (idempotent check).

### commit
- Reads metadata from `$TMPDIR/.lab-dev-workspace`.
- Stages all changes: `git add -A`.
- Prompts user for commit message OR reads from stdin/stdin template.
- Uses conventional commit format (`feat:`, `fix:`, `chore:`, `test:`, etc.).
- Commits on the feature branch.
- If user provides a ticket number reference (e.g., `--ticket 004`), append reference footer.

### push
- Reads metadata from `$TMPDIR/.lab-dev-workspace`.
- Pushes the feature branch to origin.
- Requires git remote to be configured (assumes host environment has SSH keys).
- If --pr flag given, also runs `gh pr create`:
  ```bash
  gh pr create \
    --base main \
    --title "auto: $COMMIT_MSG" \
    --body "Auto-synced from lab-dev-work.

  Branch: $(cat "$TMPDIR/.lab-dev-workspace" | grep branch=)

  Ticket: $(cat "$TMPDIR/.lab-dev-workspace" | grep ticket || echo N/A)"
  ```

### pull / sync
Reads metadata, then does one of:

**Normal mode (sync):**
1. Verify branch exists on remote (`git fetch origin`).
2. Save current original workspace state to backup ref.
3. On the real repo (original WORKSPACE): `git checkout main && git pull --rebase`.
4. `git merge --ff-only origin/<feature-branch>` or `git cherry-pick` if conflict.
5. Re-run `./lab exec` on original workspace to pick up changes.

**PR mode (`--pr`):**
1. Push branch.
2. `gh pr create --auto-merge` or manual PR creation.
3. Echo PR URL for human review.

**Dry run (`--dry-run`) echoes what sync would do.**

### Safety checks embedded
1. Abort if real repo has uncommitted changes (prevent data loss in original workspace).
2. Only allow merge from branches matching `lab-dev/*` convention.
3. No permission bypass — everything happens through git remote ops.
4. `--dry-run` mode prints actions before executing.

### Usage
```bash
./scripts/lab-dev-work init      # Clone to temp, create branch
# Agent edits files in the cloned workspace
./scripts/lab-dev-work commit    # Stage + conventional-commit
./scripts/lab-dev-work push     # Push to origin
./scripts/lab-dev-work sync     # Merge into original workspace
./scripts/lab-dev-work --pr sync  # Create PR instead of auto-sync
./scripts/lab-dev-work --dry-run sync  # Preview what sync would do
```

---

## Files
| File | Role |
|------|------|
| `scripts/lab-dev-work` (new) | Safe self-edit workflow helper |
| `$TMPDIR/.lab-dev-workspace` (generated) | Metadata storage for branch, path, ticket ref |
