# Ticket 013 — External deployment mode: co-located tickets, remote repo sync, offline cache

## Objective

Support the two patterns you work in:
1. **Co-located:** Point little-mester at a repo where tickets are checked in WITH the code (ideal for personal projects)
2. **Dedicated remote:** One ticket repo serves multiple work repos — useful for company/private code where you can't check in your own metadata

This enables cross-repo ticket sharing with git-based sync and offline-first operation with conflict resolution.

## Context (from DECISIONS.md)

### C.2 — Both use cases supported
- Co-located: tickets live in the same repo as code, versioned alongside it
- Dedicated separate: one ticket repo for multiple work repos (e.g., personal/open-source + company/private code both share the same tickets)

### Git auth out of scope
SSH is not managed inside the container. Workspace is volume-mounted. The token-based and SSH key injection approach is deferred to a future ticket (use the existing placeholder concept from ticket-009).

---

## Phase Tracking

| # | Task | Status | Notes |
|---|------|--------|-------|
| 13.1 | Remote routing protocol — config schema for external sources | ❌ TODO | Define the `routing` type and supported transports |
| 13.2 | Git sync engine (`sync --from`, `--to`, `--both`) with conflict strategies | ❌ TODO | See Phase A |
| 13.3 | Offline cache management (`.mester-cache/` directory pattern) | ❌ TODO | Track last synced index; log local modifications for resolution |
| 13.4 | MCP tool: `mesterTickets_sync` — trigger sync from any session | ❌ TODO | See Phase C |
| 13.5 | Opencode routing config example documentation | ❌ TODO | Show how to configure all three modes (default, remote, separate repo) |

---

## Architecture: The three patterns

### Pattern 1: Co-located tickets (per-workspace)
```
repo/
├── ticket-engine.json   # routing config
├── docs/
│   └── tickets/         # ← where .json files live
│       ├── _index.json  # local index
│       └── ticket-042.json
├── src/
└── README.md
```

Tickets live in the same repo as code. Any sync target points at a DIFFERENT repo (the "source of truth"). This is the default mode — no config needed, just `./lab <workspace>`.

### Pattern 2: Dedicated remote repo  
```
ticket-repo/                    # ← SINGLE source of truth
├── docs/tickets/
│   ├── _index.json
│   └── ticket-*.json           # all tickets across ALL work repos

company-workdir/                # second work repo - references same tickets
├── src/
└── .mester-routing.json        # points to the shared ticket repo
```

Company/private code references tickets from the shared repo. No ticket files in the private repo — just a routing config that tells the engine where to look.

### Pattern 3: Cross-repo (two-way sync)
Both repos maintain their own copy of tickets and reconcile them on demand or on schedule.

```
repo-A/                          # work repo A
├── .mester-routing.json → refs ticket-repo/docs/tickets

ticket-repo/                     # shared / central
├── docs/tickets/
│   ├── _index.json  (authoritative)
│   └── ticket-*.json
```

---

## Phase A: Remote routing protocol

### Routing config schema (YAML/JSON file at repo root)

```yaml
# .mester-routing.yaml or .mester-routing.json
version: 1
routing: co-located | remote | separate-repo

# For 'remote' mode:
remote:
  type: git                      # git transport (HTTP deferred to later)
  url: https://github.com/user/shared-ticket-repo.git
  ticket_dir: docs/tickets       # relative path within the repo
  auth:
    type: token_env              # token from environment variable
    env_var: MESTER_TICKET_TOKEN

# OR ssh-based (future):
#   auth:
#     type: ssh                  # use host's SSH keys, not managed by us
#     key_path: ~/.ssh/id_ed25519  # if user specifies

# For 'separate-repo' mode:
separate_repo:
  path: /path/to/other/repo/docs/tickets
  auto_sync: true                # sync on every engine operation?
  sync_interval_minutes: 30      # periodic background sync
```

### Index propagation in remote mode:
When the engine reads/writes tickets in a different location than the local index, it must:
1. Resolve `_index.json` from the configured source (local or remote)
2. If remote: fetch via `git pull` (or HTTP GET if supported later)
3. Write any changes to the _resolved_ ticket directory
4. Reconcile index on every read/write so it's always accurate

---

## Phase B: Git sync engine

```bash
# CLI commands (will be exposed as MCP tools in 13.4):

# Sync from remote source (pull latest tickets)
mesterTickets_sync --from <url> --strategy ours|theirs|minimal

# Sync to remote target (push local changes)  
mesterTickets_sync --to <url> --strategy ours|theirs|minimal

# Full bidirectional sync with conflict resolution
mesterTickets_sync --both <remote_url> --strategy minimal

# Check for uncommitted local changes in the ticket directory
mesterTickets_sync_status  # returns: clean|conflicted|stale

# List tickets that were modified locally vs remote
mesterTickets_diff HEAD~1  # or a specific range
```

### Conflict strategies:
1. **`ours`:** Local (current repo) wins on ANY conflict. Remote updates are discarded for overlapping changes. Simple but data-loss risk.
2. **`theirs`:** Remote wins on ANY conflict. The shared source of truth always prevails. Safe for dedicated remote mode.
3. **`minimal`:** Only non-overlapping ticket field changes are merged (diff-based rebase). Overlapping fields on the same ticket require MANUAL merge — engine pauses sync and returns the conflict list to you. 

**Minimal strategy algorithm:**
```
For each ticket changed in both directions:
  if only one side changed → accept that change
  if both sides changed:
    extract diff between base→ours and base→theirs
    if diffs are on different fields → auto-merge (combine)
    for overlapping field changes → flag for manual resolution
```

### Conflict output format:
When minimal strategy finds conflicts, the engine returns structured data:

```json
{
  "sync_status": "conflict",
  "conflicts": [
    {
      "ticket_number": 42,
      "overlapping_fields": ["status", "title"],
      "base_values": { "status": "draft", "title": "Old title" },
      "local_values": { "status": "active", "title": "New title v1" },
      "remote_values": { "status": "active", "title": "Title updated for feature X" }
    }
  ],
  "auto_merged": [
    { "ticket_number": 43, "fields": ["objective"] }
  ],
  "blocked_by_conflicts": true
}
```

---

## Phase C: MCP tools for sync

| Tool Name | Input Schema | Description |
|---|---|---|
| `mesterTickets_sync` | `{ direction?: enum[from\|to\|both], remote_url?: string, strategy?: enum[ours\|theirs\|minimal] }` | Sync tickets between local engine directory and a remote git repo. Direction defaults to 'sync' (bidirectional). Strategy defaults to 'minimal'. Pass direction + remote_url as needed. |
| `mesterTickets_sync_status` | `{}` | Check status: clean, conflicted, or stale. Returns sync metadata without performing any operations. Use before/after sync calls. |

---

## Files modified/created

| File | Action |
|---|---|
| `ticket-engine-mcp/src/sync.ts` (new) | Git sync engine with ours/theirs/minimal strategies |
| `ticket-engine-mcp/src/cache.ts` (new) | Offline cache management: .mester-cache/, diff tracking, last-synced index |
| `ticket-engine-mcp/src/remote.ts` (new) | Remote routing protocol implementation + config parsing |
| `.mester-routing.json` spec | Document in this ticket's Phase A (user creates per-repo) |
| `mcp-server.ts` (updated) | Add sync tools on top of existing engine registration |

## Verification

```bash
# Test co-located mode (no config needed, works by default)
./lab <workspace>                          # tickets go to workspace/docs/tickets/

# Set up remote in a second repo
echo 'routing: remote' > .mester-routing.yaml
echo 'remote:' >> .mester-routing.yaml  
echo '  type: git' >> .mester-routing.yaml
echo '  url: https://github.com/user/shared-ticket-repo.git' >> .mester-routing.yaml

# Sync from the shared repo
npx ts-node src/sync.ts --from <shared-repo-url>
```

## Notes on SSH (out of scope)
SSH-based auth for `remote.type: git` will be implemented when ticket-009's SSH injection concept is addressed. Until then, token auth via `git credential-store` or GitHub CLI (`gh auth login`) is the recommended path. The engine just passes through whatever git has access to on the host — it doesn't directly manage keys.
