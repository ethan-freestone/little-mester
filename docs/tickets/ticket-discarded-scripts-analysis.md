# Ticket -discarded-scripts — Discarded Uncommitted Scripts Analysis

## Reason for discard

The following uncommitted files contained broken logic and partial implementations that are incompatible with the clean slate needed for ticket 000 implementation. Rather than salvage them, we discard and document them to preserve the reasoning trail without carrying forward bugs.

---

## File: `scripts/lab-dev-work` (13 KB)

### Status: Discarded — rewrite required

#### Bash syntax errors that prevent execution
- **Line 68**: Uses valid ternary (`> ? :`) operator — this is fine in bash, but the logic inside (`$(compute_next_number | tr -d '[:space:]') > 1 ? ...`) mixes shell redirection `>` with the ternary expression incorrectly. Should be `$(( max > 1 ? max : 1 ))`.
- **Line 123**: Variable assignment split across lines with semicolons `local sl; sl=$(wc -l < "$f")` — while this technically works on some bash versions, it is not portable to fish and breaks the "bash AND fish" convention. Should be on one line or use a subshell.
- **Line 179**: Malformed case/fi structure with mismatched brackets and missing pattern syntax. Will never parse correctly.
- **Line 192**: Unclosed `if [[ ... ]]` condition — missing closing `]]; then`. File is syntactically broken here forward.

#### Missing features vs ticket specs
- **commit/sync stubs** (lines 229-230): Both commands echo "stub" text — no actual git operations implemented.
- No implementation of subcommands matching `meta-ticket-schema.json` validation rules from ticket 000.
- No cross-reference against `.mester-egress` manifest.

#### Non-fish compatibility issues
- Uses `local -a` arrays extensively (bash-only, fish uses `local a=(...)`)
- `shift 2> /dev/null || true` patterns not portable to fish
- Bash-specific pattern matching (`[[ ... == *"..."* ]]`) needs fish equivalents

---

## File: `workspace/scripts/lab-dev-work` (13 KB)

### Status: Discarded — duplicate clone of `scripts/lab-dev-work`

An identical copy of the above file. No value as a separate artifact. Created by unknown prior process.

---

## Lessons learned for future scripts

1. **All scripts must pass in both bash AND fish** — test with both interpreters before committing.
2. **Conventional commit messages are mandatory** — every commit must follow the `type(scope): description` format (e.g., `feat(scripts): add lab-dev-work init command`).
3. **No stub commands should reach review** — verify complete implementation before any git add.
4. **Cross-reference against tickets before implementation** — ensure script logic matches documented requirements in `docs/tickets/`.
