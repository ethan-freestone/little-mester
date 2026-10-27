# Merge Strategy: When to Merge, When to Rebase

**Created**: 2026-10-27

---

## Decision: When to merge, when to rebase

### Rule of thumb

| Scenario | Approach | Why |
|---|---|---|
| Your branch is a feature being developed, and you want a clean linear history | **Rebase** | Keeps `git log` tidy; every feature appears as a straight line |
| You are integrating `main` into your branch after it has diverged, and the branch will **never** be rewritten | **Merge** | Preserves both histories as facts; no rewriting of shared history |
| The branch is shared / has already been pushed and others may have based work on it | **Merge** (never rebase) | Rebasing rewrites public history and will cause chaos for collaborators |
| You are the only author, the branch is local-only, and you prefer a linear log | **Rebase** | You control the history; a clean log aids review and bisect |
| The divergent changes are **orthogonal** (touch different files) | Either works; **merge** is simpler | No conflict resolution needed — `git merge` is fast and honest |
| The divergent changes touch the **same files** with different approaches | **Merge** + resolve conflicts consciously | Rebasing would force you to resolve the same conflicts, one commit at a time, with no benefit |

### Applied to this repo (2026-10-27)

`main` and `feat/vm-docker-agent-env` diverged at commit `a385c5f` (feat: add Ctrl+C exit-confirm TUI plugin).

**`main` brought:**
- CI restructure (monolithic → 4 parallel jobs)
- Code hygiene (ruff lint config, shellcheck fixes, re.S → re.DOTALL)
- README major rewrite (ASCII logo, simplified, removed VM status section)
- DECISIONS.md split into focused sub-logs (testing.md, ticketing.md, opencode.md, github-actions.md)
- `little-mester-mcp` build/test changes
- Removal of `scripts/check-docs-consistency.py` (replaced by structured sub-logs)
- Deletion of tickets #40–#47 and `docs/vm-architecture.md` (content considered premature)

**Branch brought:**
- Ticket #35 expansion into VM harness epic with children #40–#47
- `docs/vm-architecture.md` — VM trust boundary design
- `scripts/check-docs-consistency.py` — docs drift checker
- Root DECISIONS.md → pointer + archive
- SECURITY.md rewrite aligning with compose defaults
- verify-lab.sh wired to run consistency script

**Resolution: Merge (not rebase)**

Reasons:
1. The branch is already pushed (`origin/feat/vm-docker-agent-env`). Rebasing would rewrite shared history.
2. The changes are heavily overlapping (same files, different approaches). Rebasing would force conflict resolution commit-by-commit with no advantage.
3. A merge commit preserves both decisions as explicit, auditable history.

### How the conflicts resolve (semantic reasoning)

| File | Branch intent | Main intent | Resolution |
|---|---|---|---|
| `DECISIONS.md` | Replace old log with pointer + archive | Replace old log with 12 structured decisions | **Keep main's version** — structured decisions are more useful long-term. The archive and pointer are implied by the split into sub-logs. |
| `README.md` | Keep most original content | Major rewrite (simplified, ASCII logo) | **Keep main's version** — the rewrite is a documented improvement; VM harness details belong in `docs/` not README. |
| `SECURITY.md` | Align with compose + VM target | Align with compose + simplified | **Keep main's version** — it was rewritten to match the same compose defaults but in a more concise form. |
| `docs/tickets/_index.json` | Add #40–#47 | Remove #40–#47 | **Keep branch's version** — these tickets are valid work items; they should not be silently deleted. |
| `docs/tickets/_audit.json` | Add audit entries for #40–#47 | Remove those entries | **Keep branch's version** — audit trail for the tickets must be preserved. |
| `docs/tickets/ticket-039.json` | Expanded to epic lifecycle feature | Rolled back to simple bug fix | **Keep branch's version** — the expanded scope is the intended work item. |
| `docs/tickets/ticket-040..047.json` | Created | Deleted | **Keep all** — these are valid tickets tracking VM harness work. |
| `docs/vm-architecture.md` | Created | Deleted | **Keep** — valid architecture design document. |
| `scripts/check-docs-consistency.py` | Created | Deleted | **Keep** — useful drift checker; main replaced it with sub-logs but this script still has value. |
| `verify-lab.sh` | Added consistency check call | Removed that call | **Keep branch's call** — the script exists, so the check should run. |
| `lab` | No changes | Shellcheck/ruff fixes | **Keep main's changes** — hygiene fixes. |
| `little-mester-mcp/` | No changes | Build/test improvements | **Keep main's changes** — test reliability. |
| `pyproject.toml` | Doesn't exist | Ruff config | **Keep** — needed for ruff lint in CI. |
| `.github/workflows/` | No changes | CI restructure | **Keep main's changes** — CI work. |

---

## Quick reference

### Merge
```bash
git checkout <branch>
git merge main          # or: git merge <source-branch>
# resolve conflicts if any
git commit              # auto-message: "Merge branch 'main' into ..."
```

### Rebase
```bash
git checkout <branch>
git rebase main         # replays branch commits on top of main
# resolve conflicts one commit at a time
git rebase --continue
```

### Key difference
- **Merge** = two-parent commit, both histories preserved, honest record
- **Rebase** = linear history, rewrites commits, cleaner log but destroys original history
