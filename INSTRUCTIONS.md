# Project Instructions

## Ticket Engine — Engine-Only Writes

**ALL ticket writes must go through the `little-mester` MCP server tools.** Never use the `edit`, `write`, or any file-system tool to directly modify files in `docs/tickets/`.

### Why?
The ticket engine maintains an audit log, lock enforcement, index sync, and orphan detection. Direct file edits bypass all of this, creating drift and un-audited changes.

### Available MCP Tools (server: `little-mester`)

| Tool | Use |
|---|---|
| `tools["little-mester"].mester_tickets_create` | Create a new ticket |
| `tools["little-mester"].mester_tickets_list` | List tickets (filterable) |
| `tools["little-mester"].mester_tickets_get` | Get a single ticket by number |
| `tools["little-mester"].mester_tickets_update` | Update ticket fields |
| `tools["little-mester"].mester_tickets_archive` | Archive a ticket |
| `tools["little-mester"].mester_tickets_search` | Multi-criteria search |
| `tools["little-mester"].mester_tickets_board` | Kanban board view |
| `tools["little-mester"].mester_tickets_validate` | Validate the ticket store |

### Rules

1. **CREATE** a new ticket idea → use `mester_tickets_create`
2. **UPDATE** an existing ticket → use `mester_tickets_update`
3. **ARCHIVE** a completed ticket → use `mester_tickets_archive`
4. **VIEW** tickets → use `mester_tickets_list`, `mester_tickets_search`, or `mester_tickets_get`
5. **NEVER** call `edit`, `write`, or any file tool on `docs/tickets/*.json`

If a tool fails or is unavailable, report the error — do not fall back to direct file edits.

---

## Current State — VM Approach (feat/vm-approach branch)

### What's done
- **`lab-vm`**: Standalone QEMU/KVM VM manager with full lifecycle (base/up/ssh/status/stop/down)
- **Verbose/debug flags**: `--verbose`/`-v` and `--debug`/`-d` added to `lab-vm` with `log()`, `log_verbose()`, `log_debug()` helpers and env var propagation (`VM_VERBOSE`, `VM_DEBUG`)
- **Auto-accept flag**: `-y`/`--yes` added to both `lab-vm` and `./lab`, sets `VM_YES` and `LAB_YES`
- **ChatGPT Plus login**: `opencode auth login openai --method chatgpt-headless` was verified inside the current agent container; `opencode auth list` reported OpenAI as `stored`
- **Auth runbook**: `docs/chatgpt-auth.md` records the verified device flow, secure export/import procedure, and required VM persistence design
- **2026-10-28 host proof (partial)**: KVM boot, SSH, and proxied package downloads succeeded at `7b54d26`; cloud-init then failed because rootless Docker required `nf_tables`. The fix explicitly installs kmod/iptables, loads/persists guest nf_tables, and installs Docker proxy/CIDR settings before first service start. Clean host retest remains required.
- **Second host attempt (12:05–12:06 UTC)**: The patched script successfully installed/started rootless Docker 29.9.2 and touched the completion marker. Launcher still rejected cloud-init's nonzero result despite `status: done`; QEMU log shows an early schema-validation warning. Exact failing field/exit code remain unknown. Updated diagnostics capture numeric status, long/JSON status and `cloud-init schema --system`; next retry must preserve/reuse overlay, **without --clean**, to obtain those details. See #41 and docs/DECISIONS.md §C.9.14–15.
- **Third host attempt (12:55 UTC; supersedes diagnostic-only handoff above)**: Confirmed exit 2 and schema error `groups.0.mester: [] is too short`. Generator now uses string group `mester`; regression coverage added and CI validates production-rendered YAML using cloud-init CLI. Old overlay caches invalid first-boot data, so next acceptance run requires **--clean**, after evidence archival. Warm guest cloud-init finished at 7.62s but SSH discovery took ~2m40s; single-type bounded keyscan replaces multi-key/pipeline probe and retains diagnostics, with actual speed improvement still pending host proof.
- **Fourth host attempt (13:08 UTC; current gate)**: SSH ready in ~10s, cloud-init clean with no recoverable errors, rootless/workspace checks passed. Agent image build failed resolving auth.docker.io during anonymous token retrieval. Build now explicitly sources guest proxy profile for the non-login Docker CLI/auth helper and forwards all six upper/lower-case build proxy args; build output/status retained in build.log. Owner approved adding npmjs.org to the project allowlist. Next retry is **without --clean** because installed guest state is good. See #41 and docs/DECISIONS.md §C.9.17; full image/lifecycle/auth/permissions remain pending.
- **Ticket #48**: AC updated to include `-y`/`--yes` (A7-A8)
- **Ticket #41**: Active VM provisioning/lifecycle; partial host evidence is not a full pass. Exact retry and handoff steps live in its test ledger. Decisions/backtracks: docs/DECISIONS.md §C.9.11–13.

### What's not done
- `./lab vm up` doesn't exist — VM commands only live in `./lab-vm`
- Logging coverage is incremental; #48 already marks A1–A8 passing. Runtime permission behavior is a separate open bug, #50.
- ChatGPT credentials are still container-local under `/home/agent/.local/share/opencode/opencode.db`; automatic host escrow, VM import, guest data persistence, and export-before-VM-deletion are documented but not implemented
- Browser/non-device-code OpenCode ChatGPT login is required by #45 A12/A13; forwarding and persistence are not implemented. Do not depend on legacy Codex device code remaining available.
- #50: launcher argument/mode fixes have mock regression coverage, but this running v2.2.0 container already has `opencode --auto` and the reported “prompt” UI is unresolved. Do not claim the UI bug fixed from argv tests.
- This container has no /dev/kvm or Docker daemon. Host testing must be coordinated, never simulated as a passing lifecycle result.

### Immediate next step: Retry the proxied build without --clean

Read #41/#45/#50 through MCP before asking the owner to exit/rebuild/retry.
Export the current container login privately using docs/chatgpt-auth.md **before
container recreation**. Do not stop the working host-Docker session merely to
test the standalone VM. Preserve failed-run logs outside `.mester/vm/default`
before `--clean`/`stop`/`down`; failed startup cleanup may already have stopped
the guest. Fourth-attempt logs were archived under
`.mester/logs/vm-host-attempt4-20261028_131237/` (attempts 2/3 also retained).
Full command procedures and expected observations belong on #41. Cloud-init is
now clean; the new change is host-side build invocation, so do not clean/reinstall
the guest for this retry. Build errors are saved in build.log; old schema/status
files may be historical and must not be mistaken for this run's build result.

```bash
# 1. On the host, inspect the shared checkout/revision
git status --short
git log -1 --oneline

# 2. Reuse installed overlay and retry image build with explicit CLI proxy env
./lab-vm up --debug --unsafe-recurse --auto-accept
# Stop on any new failure; return the error and saved diagnostics before retry.

# 4. Verify
./lab-vm status
./lab-vm ssh    # drops you inside the VM

# 5. Inside: verify provisioning only first
findmnt /workspace
docker info --format '{{json .SecurityOptions}}' # must include rootless
docker compose version
# Return output to #41 before agent/integration tests or teardown.
```

**Gate: `./lab-vm up` must complete provisioning and build the image.** SSH alone is not success. Do not retire host-Docker mode until the full lifecycle, auth and permission gates pass.

### Test the new flags (once VM boots)
```bash
./lab-vm --help        # verify -v/-d/-y in usage text
./lab-vm --verbose up  # should show SSH commands, virtiofsd output
./lab-vm --debug up    # should show raw output, timing, variable values
./lab-vm -y base       # should auto-accept package install prompt
./lab-vm -y --verbose up  # verbose + auto-accept combined
```

### After Phase 1 passes
1. Test verbose/debug/yes flags on each `lab-vm` subcommand
2. Update library files to use `log()`/`log_verbose()`/`log_debug()` helpers
3. Commit all changes
4. Update ticket #48 AC to passing with evidence
5. Then implement/test #42/#43, #45 browser auth/persistence and #50 permissions; integrate `./lab` only after the staged human gates. Do not remove host-Docker mode prematurely.
