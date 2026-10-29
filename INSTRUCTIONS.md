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

## Current State — VM Approach (feat/vm-approach branch, 2026-10-29)

### What's done
- **`lab-vm`**: Standalone QEMU/KVM lifecycle primitives and direct-in-guest OpenCode launch. Guest Docker is for project workloads, not an agent wrapper.
- **Verbose/debug flags**: `--verbose`/`-v` and `--debug`/`-d` added to `lab-vm` with `log()`, `log_verbose()`, `log_debug()` helpers and env var propagation (`VM_VERBOSE`, `VM_DEBUG`)
- **Auto-accept flag**: `-y`/`--yes` added to both `lab-vm` and `./lab`, sets `VM_YES` and `LAB_YES`
- **ChatGPT Plus login**: `opencode auth login openai --method chatgpt-headless` was verified inside the current agent container; `opencode auth list` reported OpenAI as `stored`
- **Auth runbook**: `docs/chatgpt-auth.md` records the verified device flow, secure export/import procedure, and required VM persistence design
- **VM host-test status**: Owner has reported direct-guest local-model completion, metrics, exit handling, proxy reload and a bounded isolation smoke check. Full acceptance remains open. Ticket #41 is the source of truth for exact evidence and the current handoff; old image-build retries are superseded.
- **Ticket #48**: AC updated to include `-y`/`--yes` (A7-A8)
- **Ticket #41**: Active VM provisioning/lifecycle; partial host evidence is not a full pass. Exact retry and handoff steps live in its test ledger. Project-wide workflow/auth constraints: docs/DECISIONS.md §C.9.12–13.

### What's not done
- `./lab vm up` doesn't exist — VM commands only live in `./lab-vm`
- Logging coverage is incremental; #48 already marks A1–A8 passing. Runtime permission behavior is a separate open bug, #50.
- ChatGPT credentials are still container-local under `/home/agent/.local/share/opencode/opencode.db`; automatic host escrow, VM import, guest data persistence, and export-before-VM-deletion are documented but not implemented
- Browser/non-device-code OpenCode ChatGPT login is required by #45. Runtime-derived callback forwarding for pinned OpenCode 2.2.0 is implemented locally; the real browser callback and persistence still need acceptance. Do not depend on legacy Codex device code remaining available.
- The current egress manifest allows login through auth.openai.com but not model requests through api.openai.com. The latter requires separate explicit owner approval. Authentication must preserve the VM boundary: no host/LAN route, host Docker socket, broad credential mount or arbitrary host command capability.
- Global VM TUI dialog rendering and repeated socket-disconnect reports remain open on #41. Do not infer either is fixed by the earlier smoke result or exit-confirm styling.
- Current stop/down delete guest state. Non-destructive daily shutdown/resume and launcher integration are #52; preserve the valuable existing overlay.
- #50: launcher argument/mode fixes have mock regression coverage, but this running v2.2.0 container already has `opencode --auto` and the reported “prompt” UI is unresolved. Do not claim the UI bug fixed from argv tests.
- This container has no /dev/kvm or Docker daemon. Host testing must be coordinated, never simulated as a passing lifecycle result.

### Immediate next step: Follow the current #41 host gate

Before requesting another host action, read #41/#45/#50 through MCP. Preserve the
working host-Docker session and export credentials privately before any container
recreation. Exact commands, expected results, diagnostics to preserve and stop
conditions belong to ticket #41 `tests.needed`; do not duplicate or follow a
superseded procedure from this file.

The active gate is native guest browser authentication with the current running
VM, followed by the remaining ordered gates on parent #35. Follow the latest
superseding handoff on #41/#45; do not restart provisioning to test a host-side
auth patch. Keep host-Docker mode until lifecycle, auth, permissions, workload
and host-isolation acceptance pass. Record implementation, local tests and owner
observations through MCP as work proceeds; local mocks are not host acceptance.
