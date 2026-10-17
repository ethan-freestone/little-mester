# Ticket 009 — Session-based git auth with per-operation confirmation

## Objective

Every opencode session should automatically have SSH access configured for git operations, while requiring explicit user permission before any GitHub action is executed. The lab script must support an `--ssh` flag to enable host SSH key injection (optional, opt-in) versus relying on the container's default credentials.

---

## Problem

Currently:
- Git push fails because no SSH client exists in the Docker container (`cannot run ssh: No such file or directory`)
- There is no automatic SSH key loading per session — users must manually configure git credentials
- GitHub actions (push, PR create) happen without explicit user confirmation, which conflicts with the security-first design
- The host's SSH keys are not accessible to the container by default for security reasons

---

## Design: Session-based SSH + Per-operation Confirmation

### Mode 1: Container-native SSH (default, no host key exposure)

By default, the agent runs without access to host SSH keys. Git operations use:
- HTTPS with credential helper or token-based auth
- Environment variables like `GIT_SSH_COMMAND` configured at runtime
- No host filesystem mounting of private keys

This is the safe default — no risk of accidentally exposing SSH keys into containers or logs.

### Mode 2: Host SSH key injection (optional, `--ssh` flag)

When enabled via `./lab --ssh <workspace>`:
- Mounts `$HOME/.ssh/id_ed25519` (or configured key path) into the container
- Starts an ssh-agent inside the container and loads the key
- Only active for the duration of that session; keys are not persisted

Requires user confirmation because it exposes host credentials.

### Per-operation Confirmation Gate

Regardless of SSH mode, before ANY GitHub operation:

```
[GitHub Action Required] User must explicitly confirm each action:
  ┌──────────────────────────────────────┐
  │ Action: push                         │
  │ Remote: git@github.com:user/repo.git │
  │ Branch: opencode-v2                  │
  │ What will happen: Push 1 commit(s)   │
  └──────────────────────────────────────┘
  Proceed? [y/N]

  Action: create PR
  Remote: git@github.com:user/repo.git  
  Base: main → opencode-v2
  What will happen: Create pull request
  ──────────────────────────────────────
  Proceed? [y/N]
```

---

## Implementation Phases

### Phase 1: Default behavior — container-native git auth

Add a `.git-credentials` or `GIT_TOKEN` flow inside the agent config path for HTTPS-based git. The agent should be instructed to prompt for credentials when needed, and lab should accept `--token <file-or-env>` to inject GitHub tokens.

### Phase 2: Host SSH key injection (`--ssh` flag)

In `lab`:
- Add `--ssh` flag that sets `LITTLE_MESTER_SSH=1`
- Pass SSH key path as env var (e.g., `SSH_KEY_PATH="${SSH_KEY_PATH:-$HOME/.ssh/id_ed25519}"`)
- If `LITTLE_MESTER_SSH=1`, dynamically mount the host SSH private key volume into the container: `-v "${SSH_KEY_PATH}:/root/.ssh/deploy_key:ro"` (read-only for safety)
- Start ssh-agent + add-key in the compose env or Dockerfile RUN

### Phase 3: Per-operation confirmation before GitHub actions

In an opencode session, when the agent detects a remote GitHub URL (e.g., `git@github.com/...`), it should:
1. Print the pending action to stderr with full details
2. Wait for user response via stdin or prompt
3. Only proceed if explicitly confirmed
4. Log every action with timestamp in `journal.md` for audit trail

This is not a `lab` flag — it's runtime behavior of the agent itself, configured via `.mester-confirmation` manifest in workspace root:
```
github_confirmation=true    # show confirmation before every GH action (default)
github_auto_approve=false   # override all confirmations (DANGEROUS)
```

---

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
