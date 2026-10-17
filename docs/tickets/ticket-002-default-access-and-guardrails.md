# Ticket 002 — Default access, guardrails & --unsafe-recurse

## Objective
Shift default agent to `opencode` (with internet enabled), strictly prohibit edits inside little-mester by default, and introduce `--unsafe-recurse` for the rare cases where self-modification is deliberate.

---

## ═══════════════════════════════════════════════════
## OPERATIONAL CONTEXT — READ FIRST
## ═══════════════════════════════════════════════════

### Where we are RIGHT NOW:
- There is a **single working directory**: `/workspace` which IS the `little-mester` repo.
- Branch: **`recurse-work`** (NOT detached, not main — this branch was created for this ticket).
- GitHub origin: `git@github.com:ethan-freestone/little-mester.git`.

### Working model for this ticket:
1. **AI agent (this session):** Can read/write files in `/workspace` by editing them directly.
2. **Human user:** Must run all `git`, SSH, and Docker commands manually from their terminal.
3. **Testing:** Must happen from a terminal outside the OpenCode container (Docker-in-Docker is not available).

### Critical constraint:
- The AI cannot commit or push. Every time I make edits, **you must commit them** before they exist as actual files on disk in your git working tree.
- Until you run `git add` + `git commit`, the changed files exist only inside the OpenCode session's memory layer / overlay — **not yet in your real filesystem**.

### Testing constraint:
- `./lab --unsafe-recurse ./lab` would attempt to mount `/workspace` as a workspace. If you run this from inside the OpenCode Docker container, it could cause conflicts. Test from the host shell instead.

---

## ═══════════════════════════════════════════════════
## STATUS TRACKER  (mark [x] as you go)
## ═══════════════════════════════════════════════════

### Phase 1: I make edits, you commit them
| Step | What I do (in this session) | What YOU do in your terminal | Status |
|------|----------------------------|------------------------------|--------|
| 1A | Write Part A changes to `./lab` (add --opencode / --aider flags if not done) | Nothing yet — wait for my edit confirmation | [ ] |
| 1B | Write Part C changes to `./lab` (UNSAFE_RECURSE var, flag parsing, conditional guard, confirm() line, help text) | Same as above | [ ] |
| 1C | Confirm I'm done editing — files exist in session memory only | **Run:** `git add lab agent/ && git commit -m "..."` — verify with `git diff HEAD` that changes appear | [ ] |
| 1D | — | **Push:** `git push origin recurse-work` (or wait for PR review) | [ ] |

### Phase 2: You test from your terminal
| Step | Command | Expected result | Status |
|------|---------|-----------------|--------|
| 2A | `cd /workspace/little-mester && ./lab ./lab 2>&1; echo "exit=$?"` | Output contains `die("Workspace path would expose the little-mester repo itself...)`, **exit=1** (guard fires — expected!) | [ ] |
| 2B | `cd /workspace/little-mester && ./lab --unsafe-recurse ./lab 2>&1; echo "exit=$?"` | Confirm output shows `Unsaferecurse : 1`. With `y`: agent launches into itself. Without confirmation: still exits normally — no error | [ ] |
| 2C | `./lab --opencode /tmp` and `./lab --aider /tmp` (brief test) | Confirm output shows `Agent: opencode` vs `Agent: aider` appropriately | [ ] |
| 2D | `./lab /tmp 2>&1` (normal workspace, no flags) | Works normally; EGRESS shown as true in confirm output | [ ] |

### Phase 3: Close out
| Step | Action | Status |
|------|--------|--------|
| 3A | Run tests if any exist: `./run-tests.sh` or `bats tests/` | [ ] |
| 3B | Commit & push (if not done in step 1D) | [ ] |
| 3C | Report back here that testing passed; mark ticket complete | [ ] |

---

## ═══════════════════════════════════════════════════
## Part A — Default agent + convenience flags
## ═══════════════════════════════════════════════════

### Current state: ALREADY DONE ✅
- `lab` line 12: `AGENT="opencode"` ← correct.
- Help text already says `(default: opencode)`.

### Remaining work (I will make these edits here):

Add to the while-loop case block (~line 183, before `*) \`):
```bash
      --opencode) AGENT="opencode"; shift ;;
      --aider)    AGENT="aider"; shift ;;
```

---

## ═══════════════════════════════════════════════════
## Part B — Internet enabled by default
## ═══════════════════════════════════════════════════

### Current state: ALREADY DONE ✅
- `EGRESS="${EGRESS:-true}"` (line 15).
- `.mester-egress` manifest check (lines 99-141).
- `agent/opencode/opencode.json` has no network-blocking config. **No files to modify.**

---

## ═══════════════════════════════════════════════════
## Part C --unsafe-recurse flag & self-edit guard
## ═══════════════════════════════════════════════════

### Current state: NOT IMPLEMENTED ❌

The current `check_workspace` guard at lines 65-68 is a hard-coded die():
```bash
if [[ "$ws" == "$LAB_DIR" || "$LAB_DIR" == "$ws"/* ]]; then
    die "Workspace path would expose the little-mester repo itself"
fi
```

This ALWAYS blocks. Must be made conditional on `UNSAFE_RECURSE`.

### What I need to do in this session (edit ./lab):

#### C1-A: Add UNSAFE_RECURSE=0 variable at top of script
**Where:** After line 15 (`EGRESS="${EGRESS:-true}"`)
```bash
UNSAFE_RECURSE=0   # Default: deny self-editing
```

#### C1-B: Add --unsafe-recurse flag parsing
**Where:** In the while-case block (~line 183), before `*) \` case line:
```bash
      --unsafe-recurse) UNSAFE_RECURSE=1; shift ;;
```

#### C1-C: Harden check_workspace — conditional guard
**Replace** lines 65-68:
```bash
# Before:
if [[ "$ws" == "$LAB_DIR" || "$LAB_DIR" == "$ws"/* ]]; then
    die "Workspace path would expose the little-mester repo itself"
fi

# After:
if [[ "$ws" == "$LAB_DIR" || "$LAB_DIR" == "$ws"/* ]]; then
    if [[ "${UNSAFE_RECURSE:-0}" != "1" ]]; then
        die "Workspace path would expose the little-mester repo itself. Use --unsafe-recurse to override (requires awareness of the risks)."
    fi
fi
```

#### C1-D: Update confirm() output — add unsaferecurse line
**Insert into confirm()** (before the dashed-line footer):
```bash
    echo " Unsaferecurse : ${UNSAFE_RECURSE:-0}"
```

#### C1-E: Add --unsafe-recurse to help text
**In the `-h|--help)` block** (~line 227+), add within the flags section:
```bash
  --unsafe-recurse    Allow the agent to edit inside the little-mester repo (DANGEROUS: enables self-modification)
```

---

## ═══════════════════════════════════════════════════
## Files I will modify in this session (/workspace paths)
## ═══════════════════════════════════════════════════

| File | What changes | Reason it's critical |
|------|-------------|---------------------|
| `/workspace/lab` | ~5 edits (variable, 3 case lines, 1 guard wrap, 1 confirm line, 1 help line) | ALL logic changes for this ticket live here |
| *(review only)* `/workspace/agent/opencode/opencode.json` | No changes needed | Share disabled doesn't block internet; otel: {} is empty |

---

## ═══════════════════════════════════════════════════
## Git & push commands (RUN THESE IN YOUR TERMINAL)
## ═══════════════════════════════════════════════════

After I confirm all edits are done, you will run:

```bash
# 1. Verify what changed from my edits:
git diff

# 2. Commit (if happy):
git add lab agent/opencode/opencode.json   # json is just reviewed, not modified
git commit -m "feat: add --unsafe-recurse flag, --opencode/--aider flags (ticket 002)

Per ticket-002 plan:
- Added UNSAFE_RECURSE=0 default variable at top of lab script
- Added --unsafe-recurse flag parsing in the CLI case block
- Made check_workspace self-mount guard conditional on UNSAFE_RECURSE
- Added UNSAFE_RECURSE status display in confirm() output
- Added --opencode / --aider convenience flags for agent selection toggle
- Added --unsafe-recurse documentation to help text
- Verified egress defaults to true, no code changes needed for Part B"

# 3. Push:
git push origin recurse-work

# 4. Optional — open a PR:
gh pr create --base MAIN --head recurse-work --title "feat(002): unsafe-recurse & agent selection flags"
```

---

## ═══════════════════════════════════════════════════
## Testing commands (RUN THESE IN YOUR HOST TERMINAL)
## ═══════════════════════════════════════════════════

### Test 1: Guard MUST fire by default
```bash
cd /workspace && ./lab ./lab 2>&1 || true
# EXPECTED: "REFUSED: Workspace path would expose the little-mester repo itself. Use --unsafe-recurse to override (requires awareness of the risks)."
# EXPECTED exit code: 1
```

### Test 2: --unsafe-recurse MUST lift the guard
```bash
cd /workspace && ./lab --unsafe-recurse ./lab 2>&1 || true
# EXPECTED confirm output includes: "Unsaferecurse : 1"
# If you type 'y': agent launches into self (works!)
# If you type 'n': exits cleanly with exit 0
```

### Test 3: --opencode and --aider flags work
```bash
cd /workspace && ./lab --opencode /tmp 2>&1 | grep "Agent" || true
cd /workspace && ./lab --aider /tmp 2>&1 | grep "Agent" || true
# EXPECTED: Agent shows opencode for first, aider for second
```

### Test 4: Normal workspace still works with default settings
```bash
mkdir -p /tmp/test-ws
cd /workspace && ./lab /tmp/test-ws 2>&1 | grep -E "Mounting|Egress|Agent" || true
# EXPECTED: EGRESS: true shown in confirm
```

---

## ═══════════════════════════════════════════════════
## End state goal
## ═══════════════════════════════════════════════════

After this ticket is complete, the user (on branch MAIN) will be able to run:

```bash
cd /workspace
./lab --unsafe-recurse ./lab
# This launches opencode against itself — enabling self-modification
# The agent can edit lab, commit changes, etc. — recursively improving itself
```

The guard ensures this is NEVER the default — only with explicit `--unsafe-recurse` acknowledgment.
