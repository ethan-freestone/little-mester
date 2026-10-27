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
- **VM boot fixes (pending real-host proof)**: QEMU now uses valid `guestfwd` command targets, `vhost-user-fs-pci`, shareable RAM, a pinned/checksummed Ubuntu cloud image, rootless-Docker cloud-init, fail-fast QEMU diagnostics, and a loopback egress proxy
- **Ticket #48**: AC updated to include `-y`/`--yes` (A7-A8)
- **Ticket #41**: VM provisioning/lifecycle — A1/A3 passing; A2/A5/A8/A18 in-progress; full lifecycle remains pending KVM evidence

### What's not done
- `./lab vm up` doesn't exist — VM commands only live in `./lab-vm`
- Library files (`cloud-init.sh`, `lifecycle.sh`, `prereqs.sh`, `disk.sh`, `ssh.sh`, `runner.sh`) not yet updated to use `log()`/`log_verbose()`/`log_debug()`
- Ticket #48 AC A1-A6 status not yet marked passing after testing
- ChatGPT credentials are still container-local under `/home/agent/.local/share/opencode/opencode.db`; automatic host escrow, VM import, guest data persistence, and export-before-VM-deletion are documented but not implemented
- This container cannot provide KVM evidence. Re-run `./lab-vm up --clean --debug --unsafe-recurse --auto-accept` on the CachyOS host and record the first real-host result on ticket #41

### Immediate next step: Prove the VM works on your CachyOS host

```bash
# 1. Pull the branch
git fetch && git checkout feat/vm-approach

# 2. Start the VM (auto-installs missing prereqs if needed)
./lab-vm base   # downloads ~20 GiB base disk image

# 3. Boot the VM
./lab-vm up     # creates overlay disk, seed ISO, starts virtiofsd + QEMU, waits for SSH

# 4. Verify
./lab-vm status
./lab-vm ssh    # drops you inside the VM

# 5. Test inside the VM
docker info
cd /workspace && ./lab --unsafe-recurse

# 6. Tear down
./lab-vm stop
./lab-vm down
```

**The single most important gate: `./lab-vm up` must boot successfully.** Everything else depends on it.

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
5. Then: merge `lab-vm` commands into `./lab`, update docs, kill host-Docker mode
