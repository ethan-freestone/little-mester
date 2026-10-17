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

### Phase 1: I make edits, you commit them
| Step | What I do (in this session) | What YOU do in your terminal | Status |
|------|----------------------------|------------------------------|--------|
| 1A ✅ | Write Part A changes to `./lab` (add --opencode / --aider flags done) | Nothing yet — wait for my edit confirmation | ✅ DONE |
| 1B ✅ | Write Part C changes to `./lab` (UNSAFE_RECURSE var, flag parsing, conditional guard, confirm() line, help text) | Same as above | ✅ DONE |
| 1C ✅ | Confirm edits are done — files committed | **Run:** `git add lab README.md && git commit -m "..."` | ✅ DONE (cf8077d) |
| 1D | Push to remote | **Run:** `git push origin recurse-work` | [ ] |

## ⚠️ IMPORTANT CONTEXTS

### Testing from `little-mester-working` not the repo itself
All Phase 2 testing below runs against **`~/Development/little-mester-working`** (the working checkout that has all changes). The agent will NOT merge this to MAIN yet — you'll test here first, then merge to little-mester and verify.

### ALL scripts MUST work in both fish AND bash
Every command shown uses only POSIX sh constructs (`;`, `echo`, standard pipes) that work identically in both fish and bash. **Never use `$?` (bash-only); always check if the user is on fish and adapt.** The testing section below notes shell-specific syntax where needed:

| Shell | Separator | Exit status variable |
|-------|-----------|---------------------|
| bash / sh | `;` or newline | `$?` |
| fish | `;` or Enter | `$status` |

Examples that work in BOTH:
```bash
# Both shells support these identically:
./lab . 2>&1
echo "exit=$?"          # or echo "exit=" $status in fish
./lab --help | grep unsafe
./lab /tmp               (pipe works identically in both)
```

---

### Phase 2: You test from your terminal
| Step | Command | Expected result | Status |
|------|---------|-----------------|--------|
| 2A | `cd ~/Development/little-mester-working && ./lab --help` (check help text) | `--unsafe-recurse` appears in flag list with DANGEROUS tag | [ ] |
| 2B | `./lab . 2>&1; echo "exit=" $status` (guard MUST fire — fish uses $status, bash uses `$?`) | `REFUSED: Workspace path would expose the little-mester repo itself. Use --unsafe-recurse to override` — non-zero exit (NOTE: uses `.` not `./lab`) | [ ] |
| 2C | `./lab --unsafe-recurse . 2>&1; echo "exit=" $status` (guard lifted) | Confirm shows `Unsaferecurse : 1`. With 'y': agent launches, no error from guard | [ ] |
| 2D | `./lab --opencode /tmp 2>&1 | grep Agent && ./lab --aider /tmp 2>&1 | grep Agent` (agent flags) | First: `Agent: opencode`, Second: `Agent: aider` | [ ] |
| 2E | `./lab /tmp 2>&1` (normal workspace, defaults) | EGRESS: true; normal confirmation flow, no guard error | [ ] |

### Phase 3: Close out
| Step | Action | Status |
|------|--------|--------|
| 3A | Run Bats tests: `cd /workspace && bats tests/` | All pass | [ ] |
| 3B | Commit & push (if not done in step 1D) | [ ] |
| 3C | Report back here that testing passed; mark ticket complete | [ ] |

---

## ═══════════════════════════════════════════════════
## Part A — Default agent + convenience flags
## ═══════════════════════════════════════════════════

### Current state: DONE ✅
All Part A changes implemented and committed:
- ✅ `AGENT="opencode"` already set (was done before this ticket)
- ✅ `--agent` help text updated earlier
- ✅ Added `--opencode)` / `--aider)` flags at ~line 189-190 in the while-case block

### What was done:
```bash
      --unsafe-recurse) UNSAFE_RECURSE=1; shift ;;
      --opencode) AGENT="opencode"; shift ;;
      --aider) AGENT="aider"; shift ;;
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

### Current state: DONE ✅ (committed at cf8077d)

All Part C changes have been implemented in `/workspace/lab` and committed.

The current `check_workspace` guard at lines 65-68 was:
```bash
if [[ "$ws" == "$LAB_DIR" || "$LAB_DIR" == "$ws"/* ]]; then
    die "Workspace path would expose the little-mester repo itself"
fi
```

**All edits completed and committed (cf8077d):**

| Edit | Location in lab | What changed |
|------|-----------------|--------------|
| C1 | ~line 18 | Added `UNSAFE_RECURSE=0` variable |
| C2 | ~line 189 (case block) | Added `--unsafe-recurse`, `--opencode`, `--aider` case lines |
| C3 | Lines 67-72 (`check_workspace`) | Guard now checks `${UNSAFE_RECURSE:-0} != "1"` before die() |
| C4 | ~line 154 (confirm()) | Added `Unsaferecurse: ${UNSAFE_RECURSE:-0}` output line |
| C5 | Help text block | Added `--unsafe-recurse` documentation via sed insert |

### Testing from host terminal after push:

```bash
cd /workspace && ./lab --help | grep unsafe
# Verify: --unsafe-recuse appears in help output

./lab . 2>&1; echo "exit=$?"
# Must exit=1 with guard message (UNSAFE_RECURSE not set)
# NOTE: uses `.` — realpath resolves to $LAB_DIR just like './.lab' would

./lab --unsafe-recurse . 2>&1 || true
# Confirm shows Unsaferecurse : 1, no REFUSED message
```


---

## ═══════════════════════════════════════════════════
## Files modified in this ticket (/workspace paths)
## ═══════════════════════════════════════════════════

| File | What changed | Committed |
|------|-------------|-----------|
| `/workspace/lab` | 5 edits (variable, 3 case lines, guard wrap, confirm line, help line) | ✅ cf8077d |
| `/workspace/README.md` | Self-modification section + `--unsafe-recurse` docs + example fix (`./lab` → `.` ) | ✅ cf8077d |
| `agent/opencode/opencode.json` | Reviewed; no changes needed (share disabled doesn't block internet) | — |

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
