# Ticket 009 — Session-based git auth with per-operation confirmation

## Objective

Every opencode session should automatically have SSH access configured for git operations, while requiring explicit user permission before any GitHub action is executed. The lab script must support an `--ssh` flag to enable host SSH key injection (optional, opt-in) versus relying on the container's default credentials.

## Phase tracking

| # | Task | Status | Notes |
|---|------|--------|-------|
| **Phase 1: Container-native git auth (default)** | | | |
| 9.1 | Verify HTTPS/git credential flow works in agent image | ❌ TODO | Agent has `git` installed; needs token/env var injection path |
| 9.2 | Accept `--token <file-or-env>` flag in lab to inject GitHub tokens | ❌ TODO | New flag needed in lab's argument parsing |
| **Phase 2: Host SSH key injection (`--ssh` flag)** | | | |
| 9.3 | Add `--ssh` flag → sets `LITTLE_MESTER_SSH=1` env var | ❌ TODO | New flag in lab; compose supports dynamic mounts |
| 9.4 | Dynamic volume mount of host SSH key into container (read-only) | ❌ TODO | Requires compose template/container config work |
| 9.5 | Start ssh-agent + load key in container entrypoint | ❌ TODO | Dockerfile RUN or compose environment setup |
| **Phase 3: Per-operation confirmation gate** | | | |
| 9.6 | Implement runtime confirmation prompt before GitHub actions (agent-side) | ❌ TODO | Agent-side logic; not lab feature |
| 9.7 | Create workspace `.mester-confirmation` manifest schema | ❌ TODO | New manifest format for per-workspace policy |
## Verification

```bash
# Default mode (no SSH) — git must use token or prompt
./lab ./workspace -- git push origin main  # → should fail without auth, then allow user to provide token

# With host SSH key injection
./lab --ssh ./workspace -- git push origin main  # → should succeed if key is valid

# Per-operation confirmation test:
# In an opencode session requesting a PR create:
# Agent prints action details and waits for [y/N] before executing
```

---

## Files
| File | Role |
|------|------|
| `lab` (modify) | Add `--ssh` flag, handle SSH key injection env propagation |
| `compose.yaml` (modify) | Support conditional SSH volume mount based on env var |
| `agent/Dockerfile` (modify) | Install ssh-agent, add entrypoint to start agent + load key if available |
| Workspace `.mester-confirmation` (new) | Per-workspace confirmation policy manifest |
| README.md (update) | Document new `--ssh` flag and how host keys work |
