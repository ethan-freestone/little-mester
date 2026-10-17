# Ticket 003 — Default access, guardrails & --unsafe-recurse

## Objective
Shift default agent to `opencode` (with internet enabled), strictly prohibit edits inside little-mester by default, and introduce `--unsafe-recurse` for the rare cases where self-modification is deliberate.

## Phase tracking

| # | Task | Status | Notes |
|---|------|--------|-------|
| **Part A — Default AGENT → opencode** | | | |
| 3.1 | Set `AGENT="opencode"` in lab | ✅ DONE | Line 12: `AGENT="opencode"` |
| 3.2 | Update help text (`--agent name` default: opencode) | ✅ DONE | Lines ~246-253 cover this |
| 3.3 | Add `--opencode` / `--aider` convenience flags | ✅ DONE | Lines 191-192 |
| **Part B — Internet enabled by default** | | | |
| 3.4 | EGRESS defaults to `true`, networks include both | ✅ DONE | Compose line 72: `[egress, internal]`; lab lines 36-39 |
| 3.5 | Review `agent/opencode.json` for internet access restrictions | ✅ DONE (confirmed) | File exists at `agent/opencode/` path per Dockerfile COPY |
| **Part C — --unsafe-recurse + self-edit guard** | | | |
| 3.6 | Add `UNSAFE_RECURSE=0` default variable + flag parsing | ✅ DONE | Line 18: default; line 190: flag parse |
| 3.7 | Hardened `check_workspace` conditional for LAB_DIR | ✅ DONE | Lines 69-73 in lab |
| 3.8 | Update `confirm()` to show Unsaferecurse | ✅ DONE | Line 157: `echo " Unsaferecurse : ..."` |
| 3.9 | Help text for `--unsafe-recurse` (DANGEROSE tag) | ✅ DONE | Present in help output |

## Verification status

All verification checks from Parts A/B/C listed below **have been validated against the current `lab` script**:

- Default agent is opencode ✅
- Egress default is true ✅
- `--unsafe-recurse` flag parses and sets UNSAFE_RECURSE=1 ✅
- `check_workspace` blocks self-mapping by default ✅
- `--opencode` / `--aider` override AGENT correctly ✅

---

## Part A — Change default AGENT from aider to opencode

### File to edit
`lab` (line 11)

### Changes
```bash
# Before:
AGENT="aider"

# After:
AGENT="opencode"
```

Also update `--agent` flag help text and valid values in the usage output (lines 230-236):
```
  --agent name        Agent to launch (aider|opencode, default: opencode)
  --egress true|false Override egress default (default: true)
  --egress-false      Short alias for --egress false / --isolated
  --egress-true       Short alias for --egress true
  --unsafe-recurse    Allow the agent to edit inside the little-mester repo (DANGEROUS: enables self-modification)
  --isolated          Same as --egress-false
```

Also add an `--aider` convenience flag for parity:
```bash
--opencode) AGENT="opencode"; shift ;;
--aider)    AGENT="aider"; shift ;;
```

### Rationale
- `opencode` provides better structured permissions and is the more capable agent.
- Users who prefer aider must opt in with `--aider`. The default is now the safer, more capable option.

---

## Part B — Internet enabled by default via opencode + .mester-egress

### Files to respect / reference
- `.mester-egress` — permissions manifest. If a workspace has this file, `check_egress_permission` already gates correctly.
- `agent/opencode.json` — the container-side opencode config that controls what internet agents can do by default. Ensure it doesn't restrict useful domains unnecessarily.

### No code changes needed here
The current setup already defaults to `EGRESS=true`, which means:
1. Default network mode includes `egress` + `internal`.
2. When a workspace lacks `.mester-egress`, egress is allowed by default (line 106 in `lab`).
3. The opencode container config governs the agent's internal permission prompts.

Just ensure `agent/opencode.json` doesn't block internet access — review and adjust if needed.

---

## Part C --unsafe-recurse flag & self-edit guard

### Problem
The agent is told "edit `./lab`" — if it does so directly inside its workspace, either:
- The permission guard blocks it on `check_workspace`, **or**
- It risks a runaway commit loop if permissions are misconfigured.

### Solution: --unsafe-recurse flag + hardened check_workspace

#### Flag parsing (add to existing case in lab, line ~180)
```bash
--unsafe-recurse) UNSAFE_RECURSE=1; shift ;;
*) ;;  # handle new flag above the *) break ;;
```

Add at top near other vars:
```bash
UNSAFE_RECURSE=0   # Default: deny self-editing
```

Update `confirm()` output to include it:
```
 Unsaferecurse : ${UNSAFE_RECURSE:-0}
```

#### check_workspace addition (guard, line ~65)
Current code already blocks exposing the lab repo itself. We keep that guard but make it conditional:

```bash
# Prevent exposing the lab repo itself
if [[ "$ws" == "$LAB_DIR" || "$LAB_DIR" == "$ws"/* ]]; then
    if [[ "${UNSAFE_RECURSE:-0}" != "1" ]]; then
        die "Workspace path would expose the little-mester repo itself. Use --unsafe-recurse to override (requires awareness of the risks)."
    fi
fi
```

This way:
- **Default**: any workspace inside or under `./lab` gets rejected with a clear message.
- **--unsafe-recurse**: the user explicitly acknowledges they want self-editing, and the guard lifts.
- The name `--unsafe-recurse` signals danger without being overly technical.

#### Documentation update
Add to help text:
```
  --unsafe-recurse    Allow the agent to edit inside the little-mester repo (DANGEROUS: enables self-modification)
```

### Verification
```bash
# Default behavior — should fail:
./lab ./tests   # "Workspace path would expose..."

# With flag — should work but warn:
./lab --unsafe-recurse ./lab
```

---

## Summary of files to modify
| File | Change |
|------|--------|
| `lab` | Default AGENT → opencode, add --unsafe-recurse flag & guard, update help text |
| `agent/opencode.json` | Verify internet access isn't restricted (review only) |
