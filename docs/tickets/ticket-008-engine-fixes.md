# Ticket 008 — Fix engine.ts syntax errors to make project compilable

## Objective

Fix all TypeScript syntax errors in `ticket-engine-mcp/src/engine.ts` so that `npm run build` (tsc) compiles successfully and all unit tests pass. This is a hard blocker for everything downstream.

## Context (from DECISIONS.md)

- The engine code (`engine.ts` and `index.ts`) implements core ticket operations (create/list/update/archive/validate) but has multiple syntax errors preventing compilation
- SQLite dependency (`sql.js`) in package.json is inconsistent with the JSON-first architecture decided in this session — remove it
- All new work depends on the engine compiling because tests cannot run on broken code

## Constraint

- **Do NOT change any method signatures or public API** unless a signature itself has a syntax error. This is a fix-only ticket, not a refactor.
- After fixes, also update `package.json` to remove SQLite dependency.

---

## Phase Tracking

| # | Task | Status | File | Line(s) | Details |
|---|------|--------|------|---------|---------|
| 8.1 | Fix line ~163: extra closing paren in status filter OR condition (`e.status === 'draft' \|\| e.status === status))`) | ❌ TODO | `engine.ts` | ~163 | Missing grouping parens around the OR; extra trailing ) |
| 8.2 | Fix line ~190: mismatched parens in update() key exclusion check (`(key !== 'file_path') &&)` | ❌ TODO | `engine.ts` | ~190-191 | Extra paren before `&&` and missing closing paren for outer if |
| 8.3 | Fix line ~207: missing type annotation— `const data:   = JSON.parse(raw)` | ❌ TODO | `engine.ts` | ~207 | Add `const data: Ticket = ...` |
| 8.4 | Fix line ~224: typographical error — `fileNuns` should be `fileNums` | ❌ TODO | `engine.ts` | ~224 | Simple variable name typo |
| 8.5 | Fix line ~233: unclosed construct in validate() return — missing closing paren and comma | ❌ TODO | `engine.ts` | ~233 | Missing `)` before `,` in the ternary/conditional expression; also missing final `}` for the array/map call |
| 8.6 | Remove SQLite dependency from package.json | ❌ TODO | `package.json` | All | Remove `sql.js` from dependencies and `@types/sql.js` from devDependencies |
| 8.7 | Run tests: verify build passes (`npm run build`) and all unit tests pass | ❌ TODO | — | — | Must pass clean |

---

## Acceptance Criteria

- [ ] `npm run build` completes without TypeScript errors (zero diagnostics)
- [ ] `npm test` runs with zero failures
- [ ] No new syntax or behavioral changes beyond the fixes listed above
- [ ] SQLite removed from package.json dependencies
- [ ] Engine can be imported as a module and all methods are callable

## Verification

```bash
cd ticket-engine-mcp

# Should complete with exit code 0 and no TS errors
npm run build -- 2>&1 | tee /tmp/build-output.txt
grep -i "error" /tmp/build-output.txt || echo "BUILD CLEAN: no errors found"

# All tests should pass
npm test
```

## Files

| File | Change |
|---|---|
| `ticket-engine-mcp/src/engine.ts` | Fix 5 syntax errors (tasks 8.1–8.5) |
| `ticket-engine-mcp/package.json` | Remove SQLite deps (task 8.6) |
