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
- **VM host-test status**: Latest CachyOS run reaches rootless Docker and starts the image build, but BuildKit fails resolving `registry-1.docker.io`; full image and lifecycle remain unverified. Ticket #41 is the source of truth for attempt history, logs, implementation evidence and the current retry handoff.
- **Ticket #48**: AC updated to include `-y`/`--yes` (A7-A8)
- **Ticket #41**: Active VM provisioning/lifecycle; partial host evidence is not a full pass. Exact retry and handoff steps live in its test ledger. Project-wide workflow/auth constraints: docs/DECISIONS.md §C.9.12–13.

### What's not done
- `./lab vm up` doesn't exist — VM commands only live in `./lab-vm`
- Logging coverage is incremental; #48 already marks A1–A8 passing. Runtime permission behavior is a separate open bug, #50.
- ChatGPT credentials are still container-local under `/home/agent/.local/share/opencode/opencode.db`; automatic host escrow, VM import, guest data persistence, and export-before-VM-deletion are documented but not implemented
- Browser/non-device-code OpenCode ChatGPT login is required by #45 A12/A13; forwarding and persistence are not implemented. Do not depend on legacy Codex device code remaining available.
- #50: launcher argument/mode fixes have mock regression coverage, but this running v2.2.0 container already has `opencode --auto` and the reported “prompt” UI is unresolved. Do not claim the UI bug fixed from argv tests.
- This container has no /dev/kvm or Docker daemon. Host testing must be coordinated, never simulated as a passing lifecycle result.

### Immediate next step: Follow the current #41 host gate

Before requesting another host action, read #41/#45/#50 through MCP. Preserve the
working host-Docker session and export credentials privately before any container
recreation. Exact commands, expected results, diagnostics to preserve and stop
conditions belong to ticket #41 `tests.needed`; do not duplicate or follow a
superseded procedure from this file.

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
