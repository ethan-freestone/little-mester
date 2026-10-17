# OpenCode v2 Migration Plan

This is a standalone tracking document for the Opencode v2 migration. It may be deleted once all phases are complete and committed.

---

## Architectural Decisions

| Decision | Choice | Rationale |
|----------|---------|-----------|
| **lab default agent** | `AGENT="opencode"` (change from `"aider"`) | Primary mode is opencode; aider = backup via `--agent` |
| **Config location** | `/workspace/agent/opencode/opencode.json` → `OPENCODE_CONFIG=/etc/opencode/opencode/opencode.json` in container | Each agent has its own config file under `agents/<name>/`. No workspace root pollution. Explicit path > root discovery for determinism. |
| **Dockerfile** | One at `/workspace/agent/Dockerfile` with a companion README explaining the choice | Both agents need same base deps; split only when divergence warrants it (documented in separate README) |
| **Permissions model** | `"ask"` default, configurable per-run via lab flag or config | Conservative by default; flexible for future use cases |
| **Skills location** | `.opencode/skills/testing_workflow.md` | v2 standard, no backwards compat needed |

---

## Phases

Each phase follows the pattern: **Architecture → Test → Commit**. Every commit should include a journal entry documenting what changed and why.

### Phase 1: Config Structure + Dockerfile README [Arch → Test → Commit]

**Architecture:**
- Create `/workspace/agent/opencode/opencode.json` — copy current config from `agent/opencode.json`, adapt to v2 format if needed
- Update `agent/Dockerfile`:
  - COPY step: `COPY opencode/opencode.json /etc/opencode/opencode/opencode.json`
  - Update env var: `ENV OPENCODE_CONFIG=/etc/opencode/opencode/opencode.json`
  - CMD stays (doesn't change — lab invokes the binary directly, not via CMD)
- Create `/workspace/agent/README.md` documenting why unified Dockerfile, how splitting works, and what would trigger a split

**Test:** `./lab --agent opencode <ws>` starts opencode with config resolving from `$OPENCODE_CONFIG`

**Journal entry:** Document the decision to keep one Dockerfile, list current agents served, note conditions for eventual split.

---

### Phase 2: Lab Script Default → Opencode [Arch → Test → Commit]

**Architecture:**
- Change `AGENT="aider"` → `AGENT="opencode"` in lab (line 11)
- Update help text / validation to reflect opencode as primary option
- Keep `--agent` flag accepting any binary name for flexibility (no hardcoding)

**Test:**
- `./lab <ws>` → starts opencode
- `./lab --agent aider <ws>` → still works (aider installed and runnable)
- Unknown `--agent badname` → fails cleanly with validation error

**Journal entry:** Note the rationale for flipping default + how to still use fallback agents.

---

### Phase 3: Skills Directory Restructure [Arch → Test → Commit]

**Architecture:** Move skills per v2 convention:
- Create `.opencode/skills/` directory
- Copy `skills/testing_workflow.md` to `.opencode/skills/testing_workflow.md`  
- Remove old `/workspace/skills/` dir (no backwards compat needed)

**Test:** Start opencode in a session — verify skill triggers on code-modification task. Verify via `opencode debug config` that skills are in the loaded scan paths.

**Journal entry:** Document new skills location and v2 convention followed.

---

### Phase 4: Provider & Model Connectivity Validation [Arch → Test → Commit]

**Architecture:** v2 uses the same config schema for custom npm providers (`"@ai-sdk/openai-compatible"`). No syntax changes expected — purely validation phase.

**Test:**
- `opencode debug config` in-session shows merged config correct
- Both models selectable with context/output limits working
- Ollama connection confirmed
- Context length limits honored (65536 for default, 32768 for small model)

**Journal entry:** Document connectivity results — any issues, workarounds or future notes.

---

### Phase 5: Permissions & Safety [Arch → Test → Commit]

**Architecture:** Keep `"ask"` default. Consider adding a lab flag like `--permissions deny|ask|auto` for per-run control, similar to how `.mester-egress` works. Document in config schema.

**Test:** Start session — verify edit + bash both prompt for consent (not auto-approved). Test that permissions can be overridden if needed.

**Journal entry:** Note any permission edge cases discovered during testing.

---

### Phase 6: Full Session Integration [Arch → Test → Commit]

**Test:** End-to-end validation:
- Build new image
- Start opencode session
- Verify config/models/permissions all correct
- Trigger and verify skill loading
- Verify aider fallback still works via `--agent aider`
- Confirm Docker security constraints not broken (read_only, cap_drop, tmpfs)

**Journal entry:** Migration sign-off. List anything that changed from original plan, final state summary, open follow-ups.

---

## Open Items (Decisions Needed During Execution)

1. **Option A vs B for Dockerfile runner split** (Phase 1):
   - **A)** One Dockerfile with configurable entrypoint (`docker-compose.yaml profiles: [agent-opencode]`, `[agent-aider]`)
   - **B)** Two completely separate docker dirs/images at `/agent/opencode/Dockerfile` and `/agent/aider/Dockerfile`
   - Currently leaning toward keeping ONE until divergence justifies two

2. **Permissions flag name** (Phase 5): `--permissions` vs `--confirm` vs `--guard` for the per-run overwrite flag?

3. **Dockerfile CMD**: Currently defaults to `aider`. After Phase 2, this should be changed to `opencode` as well since lab invokes the binary directly anyway.

---

## Checklist

- [ ] Phase 1: Config structure + Dockerfile README created
- [ ] Phase 1: Test — opencode starts with correct config
- [ ] Phase 2: Lab default agent flipped to opencode
- [ ] Phase 2: Test — both agents work via `./lab` and `./lab --agent aider`
- [ ] Phase 3: Skills restructured to `.opencode/skills/`
- [ ] Phase 3: Test — skill triggers correctly in session
- [ ] Phase 4: Provider & model connectivity validated
- [ ] Phase 4: Test — both models selectable, Ollama connected
- [ ] Phase 5: Permissions set to "ask" default, configurable per-run
- [ ] Phase 5: Test — edit/bash prompt for consent
- [ ] Phase 6: Full end-to-end integration test
- [ ] Final journal entry with sign-off and open follow-ups
