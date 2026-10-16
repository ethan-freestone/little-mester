# little-mester

A **locked-down, fully local sandbox for learning agentic coding.**

A local LLM (via Ollama) drives a coding agent that reads and edits files in **one folder you choose**, and nothing else. No access to your home directory, keys, or other projects. Agent has internet by default — see [How to disable](#how-to-disable-internet).

The goal: two things in one repo.

1. A safe place to point an AI agent at code without worrying about your machine.
2. A **learning lab**: every piece is small enough to read, break, and rebuild.

> Status: personal learning project. Nothing here has been security-audited.
> Treat the sandbox as strong *guard rails*, not a guarantee. See [What this does NOT protect against](#what-this-does-not-protect-against).

---

## Architecture

Default networking (no overrides):

```
 HOST (CachyOS)
 ┌──────────────────────────────────────────────────────────────────────┐
 │                                                                       │
 │  ./lab ──► docker compose                                             │
 │                                                                       │
 │   ┌──── networks: egress + internal (internet ON by default) ──────┐  │
 │   │                                                                │  │
 │   │  ┌───────────────┐   http://ollama:11434   ┌────────────────┐  │  │
 │   │  │ agent         │ ──────────────────────► │ ollama         │  │  │
 │   │  │ (Aider, git,  │                         │ (model server, │  │  │
 │   │  │  python)      │                         │  uses GPU)     │  │  │
 │   │  │ non-root      │                         │ no ports       │  │  │
 │   │  │ read-only fs  │                         │ published      │  │  │
 │   │  └──────┬────────┘                         └───────┬────────┘  │  │
 │   └─────────┼──────────────────────────────────────────┼───────────┘  │
 │             │ bind mount (read-write)                  │ named volume │
 │             ▼                                          ▼              │
 │   ~/…/little-mester/workspaces/<project>        ollama-models         │
 │   (the ONLY host folder the agent sees)                               │
 │                                                                       │
 │   setup only:  ollama-pull ──► network: egress ──► internet           │
 └──────────────────────────────────────────────────────────────────────┘
```

Agent internet is **enabled by default** (`[egress, internal]`). To disable (e.g. for isolated sub-agents):

| Method | Compose override | Effect |
|---|---|---|
| Lab flag | `--isolated` | Sets agent networks to `[internal]` only — no outbound internet |
| Lab flag | `--egress-false` or `--egress false` | Same as `--isolated` |
| Direct compose | `-e NETWORKS_AGENT=internal` + omit egress from `networks.agent` override | Same effect, more explicit but requires editing compose |

### Three ideas to hold onto

| Idea | How it's done here |
|---|---|
| The model can't touch files | Ollama only turns text into text. It's in its own container with no mounts except its model volume. |
| The agent can only touch one folder | The agent container sees exactly one bind mount, `/workspace`. |
| Internet can be turned on or off | Agent defaults to `[egress, internal]` (internet ON). Use `--isolated` in lab to remove egress entirely. Only `ollama-pull` has internet during setup. |

---

## What exists now

The repo is split into two conceptual layers:

| Layer | Where | Purpose |
|---|---|---|
| **Sandbox** | root (`lab`, `compose.yaml`, `agent/`, `ollama/`) | Safe execution environment for the agent |
| **Ticket engine** | `ticket-engine-mcp/` | Lightweight MCP-first ticketing system using JSON files + in-memory index (no SQLite) |

### Sandbox (root)

Run `./lab setup` to configure docker, build the agent image, and pull models. Then:

```fish
./lab workspaces/demo             # start Aider on that folder
./lab shell workspaces/demo      # plain bash inside the sandbox
./verify-sandbox.sh              # verify security guarantees hold
```

Full usage is in [docs/workflows.md](docs/workflows.md).

### Ticket engine (`ticket-engine-mcp`)

A lightweight MCP module for ticket lifecycle management. It stores tickets as `.json` files on disk backed by a tiny `_index.json` — no SQLite, no WASM, no schema migrations. Designed to eventually be a standalone hosted service, but currently managed alongside the repo.

See `ticket-engine-mcp/docs/blueprint.md` for implementation plan.

### Scripts and automation

| Path | What it does |
|---|---|
| `scripts/ticket-new` | Create new tickets |
| `scripts/validate-tickets` | Validate ticket format and index sync |
| `scripts/ticket-lifecycle` | Manage ticket status transitions |
| `scripts/commit-helper` | Assist with commit message formatting |
| `scripts/sync-helper` | Sync helpers for workspace management |

---

## Security & AI Interaction Boundaries

### 🔒 How to safely interact with the agent
1. **Workspace mount:** You explicitly mount a single directory into the container at `/workspace`. The AI reads/writes only here.
2. **Container shell:** Use `./lab shell <path>` for interactive debugging inside the sandbox.
3. **No host commands:** The AI cannot execute commands on your OS, access SSH keys, or modify system files.

### 🛡️ Security model overview
Full details in [SECURITY.md](SECURITY.md). Key guarantees:

- ✅ Zero host shell access — AI operates strictly inside the container
- ✅ Immutable security configs — `compose.yaml` and `lab` cannot be altered by the AI
- ✅ Network air-gap — no outbound internet during runtime
- ✅ Privilege dropping — non-root, all Linux capabilities dropped, read-only root fs

### What this does NOT protect against

Read this twice.

- **The agent can delete or corrupt everything inside the mounted workspace, `.git` included.** Keep backups or push to a remote *before* sessions.
- **Traps that run on your host later.** Git hooks, `Makefile`, `package.json` scripts, shell scripts written by the agent run with *your* privileges if you run them on the host. Review before executing.
- **Container escape.** Containers share the host kernel. Unlikely from a small local model making mistakes, but real against deliberately hostile code. A VM is the next level up.
- **Docker group = root.** Anyone who can run `docker` can effectively become root.

---

## Repo map

| Path | What it is | Read this |
|---|---|---|
| `lab` | Launcher script — validates workspace path, runs compose | [docs/workflows.md](docs/workflows.md) |
| `compose.yaml` | Defines services, networks, volumes, hardening flags | — |
| `agent/Dockerfile` | Container image for the agent | — |
| `ollama/pull-models.sh` | Model-pulling script | — |
| `models.txt` | Models to download during setup | — |
| `verify-sandbox.sh` | Regression tests for sandbox properties | [Testing](#testing) |
| `workspaces/` | Where projects the agent works on live (gitignored) | — |
| `docs/workflows.md` | **Development process and discipline** | — |
| `docs/journal.md` | Development journal | — |
| `docs/tickets/` | Tickets by mode (area, lab, engine) | [Working conventions](#working-conventions) |
| `ticket-engine-mcp/` | Lightweight MCP-first ticketing system | `ticket-engine-mcp/docs/blueprint.md` |
| `scripts/` | Automation tools — tickets, hooks, sync | [Scripts](#scripts-and-automation) above |
| `tests/lab.bats` | Bats tests for the `lab` launcher | [Testing](#testing) below |
| `skills/` | Reusable workflow skills | [Working conventions](#working-conventions) |
| `SECURITY.md` | **Security model, network isolation** | [Security above](#security--ai-interaction-boundaries) |

---

## Testing

### Host tests (Bats)

```fish
bats tests/
```

Runs path guard validation, security checks, and launcher helpers without Docker or GPU.

### Agent tests (inside sandbox)

```fish
./run-tests.sh workspaces/demo    # runs bats inside the container via ./lab exec
```

### Ticket engine tests

```fish
cd ticket-engine-mcp && npm test  # Jest suite for the JSON ticket engine
```

---

## Working conventions

Follow [docs/workflows.md](docs/workflows.md) for:
- The **doc → test → code → commit** cycle (no skipping stages)
- How architecture decisions are recorded (`arch-*.md` files)
- Ticket format and status transitions
- MCP development workflow
- Session handoff template

---

## Quick start (new machine)

Requirements: Arch/CachyOS, working NVIDIA driver, `sudo`.

```fish
git clone <this-repo> little-mester
cd little-mester
./lab setup                       # installs docker + GPU toolkit, builds image, pulls models
./verify-sandbox.sh               # proves the sandbox properties hold
mkdir -p workspaces/demo          # make or clone a project here
./lab workspaces/demo             # start the agent on that folder
```

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `nvidia-smi` fails on host | Driver problem. RTX 50-series needs open kernel modules. Fix before anything else. |
| `docker run --gpus all ... nvidia-smi` fails | NVIDIA container toolkit not configured. Re-run `sudo nvidia-ctk runtime configure --runtime=docker` and restart docker. |
| `ollama ps` shows part CPU | Model plus context doesn't fit in VRAM. Lower `OLLAMA_CONTEXT_LENGTH` or use a smaller model. |
| Agent can't reach `ollama` | Ollama not healthy yet, or services aren't on the same network. |
| Agent files owned by root | `LAB_UID`/`LAB_GID` weren't passed through. Run via `./lab`, not `docker compose` directly. |
