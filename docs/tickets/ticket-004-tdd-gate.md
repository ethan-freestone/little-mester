# Ticket 004 — TDD gate: mandatory testing before merge

## Objective
Enforce the rule that "any change to little-mester MUST pass all existing tests AND create new ones for any new functionality" — programmatically, not as a suggestion.

---

## Problem
The `testing_workflow.md` skill describes the process but relies on the agent reading it and cooperating. We need actual gate logic in `lab-dev-work` that blocks merges when tests fail.

---

## Design: TDD gate inside lab-dev-work

Add a `verify` subcommand to `scripts/lab-dev-work`:

### verify
1. Reads ticket metadata from `$TMPDIR/.lab-dev-workspace`.
2. Runs bats tests in the agent container against the feature branch's changes.
3. If all pass → allows sync/merge to proceed.
4. If any fail → blocks sync, prints which tests failed, requires human intervention.

```bash
verify)
    # Read workspace path from metadata
    local ws="$(grep workspace= "$TMPDIR/.lab-dev-workspace" | cut -d= -f2)"
    
    echo "Running test suite against feature branch..." >&2
    
    # Run bats inside agent container mounted at temp clone
    LITTLE_MESTER_EGRESS=false \
    docker compose -f "$LAB_DIR/compose.yaml" --project-directory "$LAB_DIR" \
        run --rm -T agent bats /workspace/tests/
    
    local rc=$?
    if [[ $rc -ne 0 ]]; then
        die "Test failed ($rc). Merge blocked. Fix failures before running ./lab-dev-work sync."
    fi
    
    # Check for new test files (ensure new functionality has tests)
    local new_tests="$(git diff --name-only origin/main...HEAD | grep '^tests/' || true)"
    if [[ -n "$new_tests" ]]; then
        echo "New test files detected — good:" >&2
        echo "$new_tests" >&2
    fi
    
    # Check for production code changes without matching test coverage
    local new_code="$(git diff --name-only origin/main...HEAD | grep -v '^tests/' | grep -v 'README\|SECURITY\|.md' || true)"
    if [[ -n "$new_code" ]]; then
        echo "Production code changed but no corresponding tests — manual review required:" >&2
        echo "$new_code" >&2
        # Don't die here — warn and require explicit acknowledgment
        read -r -p "Acknowledge untested changes? [y/N] " ack
        [[ "$ack" == "y" || "$ack" == "Y" ]] || die "Aborted: acknowledge untested changes before proceeding."
    fi
    
    echo "All tests passed. Safe to sync." >&2
    ;;
```

---

## Gate enforcement flow

The merge/sync subcommand calls `verify` automatically before merging:

```bash
sync)
    if [[ "${TDD_GATE:-1}" == "1" ]]; then
        ./scripts/lab-dev-work verify   # or call the function directly
    fi
    # ... proceed with merge logic ...
    ;;
```

Set `TDD_GATE=0` to bypass (only for human emergency override, logged in output).

---

## Integration with existing testing_workflow skill

Extend `skills/testing_workflow.md` to document both workflows:

**For humans:** The skill describes the TDD process conceptually.
**For agents via lab-dev-work:** The gate is automatic — the agent cannot proceed past the verification step without human intervention if tests fail.

Update `testing_workflow.md` to add a new section at the end:

```markdown
## Automated Enforcement (via lab-dev-work)

When modifying little-mester through ./lab-dev-work, testing is enforced automatically:
1. `./lab-dev-work verify` runs the full test suite before merge.
2. Merges are blocked if any tests fail.
3. New production code without corresponding tests generates a warning requiring human acknowledgment.
4. The gate can be bypassed with `--no-tdd-gate`, but this requires explicit conscious approval.

This ensures TDD is not optional when improving little-mester itself.
```

---

## Verification
```bash
# After ticket-001 and ticket-003:
./scripts/lab-dev-work init
git checkout $(grep branch= "$TMPDIR/.lab-dev-workspace" | cut -d= -f2)

# Make a change that breaks a test:
echo '# broken' >> /workspace/tests/lab.bats

# Verify blocks merge:
./scripts/lab-dev-work verify  # → exits with error
./scripts/lab-dev-work sync    # → also blocked by verify gate

# Fix the tests, then succeed:
git add -A && git commit -m "fix: broken test"
./scripts/lab-dev-work verify  # passes
./scripts/lab-dev-work sync    # proceeds
```

---

## Files
| File | Role |
|------|------|
| `scripts/lab-dev-work` (modify) | Add verify subcommand + auto-gate in sync |
| `skills/testing_workflow.md` (modify) | Document automated enforcement section |
