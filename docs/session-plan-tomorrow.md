# little-mester Session Plan — 2026-10-02 (Next)

## Objective
- Add bats-core to the agent Dockerfile build so tests run **inside** the container ("little-mester against little-mester").
- Create a standalone `install-bats.sh` fallback for any host-side/CI scenario.
- Design and implement an automated safe self-edit pattern (temp-clone → commit → pull → restore) for the agent to modify `./lab` without permission bypass or runaway risk.
- End-to-end verify: `./lab setup → bats run → all tests pass`.

## Scope & Boundaries
- **Do not** mount or expose `.$LAB_DIR` itself as workspace (already blocked in `check_workspace`).
- All manifests use `.mester-egress` key-value format: `permission=true|false`, `allowed_domains=`, `intent=`.
- Docker Compose lacks runtime array merging; we rely on `${NETWORKS}` env var injection from `dc()`.

---

## Part 1 — bats-core baked into agent Dockerfile

### File to edit
`/workspace/agent/Dockerfile`

### Steps (in session)
1. In the existing `apt-get install` block, add `bats` (Debian/Ubuntu package name is `bats`).
2. If package isn't available on `python:3.12-slim` (Debian bookworm), we must install from source:
   ```dockerfile
   RUN git clone --depth 1 https://github.com/bats-core/bats-core.git /tmp/bats \
       && sudo /tmp/bats/install.sh /usr/local \
       && rm -rf /tmp/bats
   ```
   (The base image doesn't ship `sudo` yet; either `apt-get install -y sudo` first, or switch to `apk`/direct clone.)

3. Build locally:  
   `$ sudo docker compose --project-directory /workspace build agent`
4. Confirm bats is present inside the built image:  
   `$ docker run --rm workspace_agent bats --version`

### Verification command (inside container)
```bash
docker run -it --rm workspace_agent bash -c 'bats --version && bats /workspace/tests/'
```

---

## Part 2 — Standalone install-bats.sh fallback

### File to create
`/workspace/scripts/install-bats.sh`

### Purpose
- Host-side fallback when host OS / user doesn't run Arch with pacman.
- Safe idempotent: runs once, skips if bats already present.
- No root required for the "clone-and-install" path (installs to `~/.bats`), falls back to `/usr/local/bin` with sudo when writable.

### Steps (in session)
1. Detect OS (`pacman`, `apt`, `yum`/`dnf`, `brew`).
2. Try native package first; if absent, clone bats-core from Github and run its install script.
3. Write the script following current repo conventions: set -euo pipefail, die() helper, verbose flag support.

### Verification
```bash
bash scripts/install-bats.sh
bats --version   # should print a version string
bats tests/lab.bats  # all assertions pass
```

---

## Part 3 — Automated safe self-edit pattern for ./lab

### File to create
`/workspace/lab-dev-work` (helper script, invoked by the agent)

### Problem this solves
The agent is told "edit `./lab`" — if it does so directly inside its workspace, either:
- The permission guard blocks it on `check_workspace`, **or**
- It risks a runaway commit loop if permissions are misconfigured.

### Pattern (temp-clone → work → sync)
```
1. Clone the little-mester repo to a temporary directory outside check_workspace.
2. Create a dedicated branch:  lab-dev/<timestamp>.
3. Agent operates on that temp clone in /tmp.
4. When agent is done, invoke `./lab-dev-work pull` which:
   - Commits staged changes on the feature branch (respecting conventional commit format).
   - Pushes to origin.
   - Opens a PR or creates one (if --pr flag given).
   - Merges into main via `git pull --rebase` + fast-forward inside the main WORKSPACE.
5. Finally, restart/re-run the agent session in the original workspace so it picks up changes.
```

### Safety checks embedded in `/workspace/lab-dev-work`
- Abort early if any untracked / unstaged changes exist in the real repo (prevents accidental data loss).
- Use `--dry-run` mode first: `./lab-dev-work --dry-run pull` echoes what would happen.
- Only allow merge into main from branches that follow convention `feat/… | fix/… | chore/…`.
- No permission bypass: everything happens through git remote ops; the real repo never gets mounted inside the container.

### Key workflow subcommands (in session)
| Subcommand    | What it does |
|---------------|-------------|
| `init`        | Clone to $TMPDIR, create branch, echo next steps. |
| `commit`      | Stage + conventional-commit on feature branch. |
| `push`        | Push branch to origin (requires SSH key available). |
| `pull`        | PR → merge → rebase-sync into original workspace. |
| `--dry-run`   | Print what pull would do. |
| `--pr`        | Open a PR rather than auto-merge. |

### Design notes for the session
1. Add `init | commit | push | pull |--dry-run|--pr` cases to a new helper script.
2. For PR creation, prefer `gh pr create` (requires gh CLI and auth token in CI).
3. For local-only testing: use `git worktree add` instead of cloning so sync is fast with `.git` pointers.

---

## Part 4 — End-to-end verification run

### Once Parts 1–3 are implemented, run:

```bash
# 1. Install bats on host (if install-bats.sh exists)
bash scripts/install-bats.sh

# 2. Run bats natively on the host (sanity)
bats tests/lab.bats   # should pass all 13 assertions

# 3. Build agent image with baked bats
sudo docker compose --project-directory /workspace build agent

# 4. Run bats inside the container
docker run --rm workspace_agent bats /workspace/tests/

# 5. If everything passes, do a test self-edit cycle:
   sudo git config global user.name "little-mester-agent" \
                     user.email "agent@localhost"

   ./lab-dev-work init      # creates temp clone on branch
   # Agent edits the file in the temp workspace
   ./lab-dev-work commit    # conventional-commit
   ./lab-dev-work pull      # syncs back (or gh pr create)
```

---

## Files referenced / to be modified

| Path                               | Role |
|------------------------------------|------|
| `/workspace/agent/Dockerfile`      | Add bats-core installation |
| `/workspace/scripts/install-bats.sh` | Host fallback installer (new) |
| `/workspace/lab-dev-work`          | Safe self-edit workflow helper (new) |
| `/workspace/compose.yaml`          | Read-only reference for networks |
| `/workspace/lab`                   | `check_workspace`, `dc()`, egress gate (already wired) |
| `.mester-egress`                   | Manifest format spec |
| `/workspace/tests/lab.bats`        | 13 tests that validate all security checks |

## Open decisions for the session
1. **bats package availability** — If `apt-get install bats` fails on bookworm, fall back to GitHub clone. Decide at runtime which approach we use.
2. **PR workflow preference** — Auto-merge to main or always create PR? Default to `--pr` mode for safety; allow unmerged sync via `git worktree`.
