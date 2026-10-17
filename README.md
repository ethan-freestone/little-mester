# little-mester

A **locked-down, fully local sandbox for learning agentic coding.**

A local LLM (via Ollama, on the GPU) drives a coding agent (**opencode by default, or aider** via `--agent`) that can
read and edit files in **one folder you choose**, and nothing else. No internet by default.
No access to your home directory, keys, or other projects.
Run `./lab <workspace-path>` to point the agent at one folder on your machine. The agent (Aider or opencode) inside the container reads and edits only that folder — nothing else. No access to your home directory, keys, or other projects. Agent has internet by default — see [How to disable](#how-to-disable-internet).

The goal: two things in one repo.

1. A safe place to point an AI agent at code without worrying about your machine.
2. A **learning lab**: every piece is small enough to read, break, and rebuild.

> Status: personal learning project. Nothing here has been security-audited.
> Treat the sandbox as strong *guard rails*, not a guarantee. See [What this does NOT protect against](#what-this-does-not-protect-against).

---

## Build status (tick these off as you go)

- ~~[ ] 0. Host NVIDIA driver works (`nvidia-smi`)~~ — done
- ~~[ ] 1. Repo skeleton, `.gitignore`, `models.txt`~~ — done
- ~~[ ] 2. `compose.yaml`~~ — done
- ~~[ ] 3. `agent/Dockerfile`, `ollama/pull-models.sh`~~ — done
- ~~[ ] 4. `lab` launcher with path guard rails & egress gate~~ — done (includes `.mester-egress`)
- ~~[ ] 5. `./lab setup` completes (packages, image build, models pulled)~~ — done
- ~~[ ] 6. `./verify-sandbox.sh` all green~~ — done
- ~~[ ] 7. First opencode session on a throwaway repo~~ — done
- [ ] 8. Multi-repo workspace tested
- [ ] 9. Own agent loop (`my-agent/`)
- [ ] 10. Evals (`evals/`)
- [ ] 11. Red-team the sandbox (`red-team/`)
- [ ] 12. Opt-in web access via proxy (`proxy/`)
- [ ] 13. MCP (`mcp/`)

---

## Architecture

Default networking (no overrides):

```
 HOST (CachyOS)
 ┌──────────────────────────────────────────────────────────────────────┐
 │                                                                      │
 │  ./lab ──► docker compose                                            │
 │                                                                      │
 │   ┌──────────── network: [internal] [+ optional egress] ──────────┐ │
 │   │                                                                │ │
 │   │  ┌───────────────┐   http://ollama:11434   ┌────────────────┐  │ │
 │   │  │ agent         │ ──────────────────────► │ ollama         │  │ │
 │   │  │ (opencode/    │                         │ (model server, │  │ │
 │   │  │  Aider, git,  │                         │  uses the GPU) │  │ │
 │   │  │  python)      │                         │ no ports       │  │ │
 │   │  │ non-root      │                         │ published      │  │ │
 │   │  │ read-only fs  │                         └───────┬────────┘  │ │
 │   │  │ no caps)      │                                 │           │ │
 │   └─────────┼────────┘                                 │           │ │
 │             │ bind mount (read-write)                  │ named volume│
 │             ▼                                          ▼             │
 │   ~/…/little-mester/workspaces/<project>        ollama-models        │
 │   (the ONLY host folder the agent sees)         (model weights)      │
 │                                                                      │
 │   --egress / .mester-egress ──► optional egress network ──► internet │
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
| The agent can only touch one folder | The agent container sees exactly one bind mount, `/workspace`, gated by `lab`'s path checks. |
| The agent can't phone home | By default the agent and Ollama share an `internal: true` network. Egress is opt-in via `--egress`/`.mester-egress`. Only `ollama-pull` has it during setup. |
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

**Key Guarantees:**
- ✅ **Zero Host Shell Access:** The AI operates strictly inside the container.
- ✅ **Immutable Security Configs:** Security settings in `compose.yaml` and `lab` cannot be altered by the AI. Any changes require manual review and confirmation.
- ✅ **Network Air-Gap by default:** No outbound internet during runtime. Optional egress via `--egress` or `.mester-egress` manifest per workspace. Only `ollama-pull` has temporary egress during setup.
- ✅ **Privilege Dropping:** Runs as non-root, drops all Linux capabilities, enforces `no-new-privileges`, and limits memory/PIDs.

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

#### Switching Models
By default, Ollama serves whichever is first in `models.txt` (or last ran). To switch:
1.  Ensure the target model is downloaded (see above).
2.  If your agent supports explicit model selection (opencode: via config; Aider: via command-line flag), pass it. Otherwise rely on Ollama's default behavior.
3.  **VRAM Check**: If the agent stalls or falls back to CPU, check `nvidia-smi`. If VRAM is full, switch to a smaller quantization (e.g., `q4_K_M`) or a smaller model (e.g., `phi3` instead of `llama3`).

```fish
bats tests/
```

### 3. Monitoring the Agent

The agent (opencode by default) runs inside a container. Monitor it from the host:

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

```bash
git clone <this-repo> little-mester
cd little-mester
./lab setup                       # installs docker + GPU toolkit, builds image, pulls models
./verify-sandbox.sh               # proves the sandbox properties hold
mkdir -p workspaces/demo          # make or clone a project here
./lab workspaces/demo             # start the agent on that folder
```

Models to download are listed in [`models.txt`](models.txt). Edit it, then re-run
`./lab setup`.

## Everyday use (works in bash or fish — tested in both)

```bash
./lab <workspace>               # start opencode (default agent)
./lab --aider    <workspace>    # switch to Aider for this session
./lab --agent opencode <path>   # explicitly pick opencode
./lab <workspace> --help        # all flags: .lab --unsafe-recurse, --egress, --isolated…
./lab shell <workspace>         # plain bash inside the sandbox
./lab stop                      # stop Docker compose
./verify-sandbox.sh             # re-run after any change to compose.yaml / Dockerfile
```

`<workspace>` can be a single repo, or a parent folder holding several repos.
The launcher refuses `/`, `$HOME`, anything you don't own, anything overlapping
`~/.ssh`, `~/.config` and similar, and **by default** any path that would expose this
lab repo itself (use `--unsafe-recurse` to override — see below).

### Self-modification (ticket 002)

When you want the agent to edit little-mester itself, use `--unsafe-recurse`:

```bash
# From any little-mester checkout (e.g. ~/Development/little-mester-working on recurse-work):
cd <path-to-little-mester-checkout>
./lab --unsafe-recurse    # allows opencode to read/write the current directory
```

You do not need to append `.` — it is implicit when using `--unsafe-recurse`.

This is off by design — it tells the agent "I know what I'm doing." Without `--unsafe-recurse`, the guard always blocks self-mapping. This makes recursive improvement possible **without making it the default**.

---

## Repo map

| Path | What it is | Read this |
|---|---|---|
| `lab` | Launcher script. Validates paths, egress, then runs compose. | [Security & AI Interaction Boundaries](#security--ai-interaction-boundaries)|
| `compose.yaml` | Defines services, networks, volumes and hardening flags. | [agent/README.md](agent/README.md) |
| `models.txt` | List of models to download during setup. | — |
| `verify-sandbox.sh` | Regression tests for sandbox network/host guarantees. | — |
| `./agent/` | Dockerfile + opencode config for the agent container. | [agent/README.md](agent/README.md) |
| `./ollama/` | Model-pulling script and VRAM notes. | — |
| `./workspaces/` | Where projects the agent works on live (gitignored). | — |
| `docs/` | Journal, tickets, host setup notes, threat model. | [docs/journal.md](docs/journal.md) |
| `docs/tickets/` | Tickets with goals, steps, and progress tracking. | [ticket-001](docs/tickets/ticket-001-bats-inside-agent.md) |
| `tests/` | Unit tests for the `lab` launcher script (Bats). | — |
| `run-tests.sh` | Runs Bats inside the agent container via `lab exec`. | — |
| `SECURITY.md` | Full security model, network isolation, capabilities. | [Security & AI Interaction Boundaries](#security--ai-interaction-boundaries) |
| `scripts/` | Automation (regen-configs, check-syntax, ticket helpers). | [scripts/README.md](scripts/README.md) |

---

## How a session works

1. You run `./lab workspaces/demo`.
2. `lab` resolves the path, runs safety checks, and asks you to confirm the mount.
3. `lab` (via compose) waits for ollama then starts the `agent` container:
   non-root, read-only filesystem, zero Linux capabilities, `/workspace` mounted read-write.
4. **By default, opencode** sends your prompt to `http://ollama:11434/v1`.
5. The agent edits files in `/workspace`, runs commands in the container, and commits via git.
6. When you quit, the container is removed (`--rm`) and `lab` warns if it finds git hooks.
7. **You review on the host** with `git log` and `git diff`.

Understanding steps 4 to 6 *is* agentic coding: a loop where a model proposes actions,
a program carries them out, and the results go back to the model. [my-agent/](#learning-path) is
where you build it yourself from scratch.

---

## Testing

> **Testing context:** Run tests from `little-mester-working` (the working
> checkout on recurse-work), NOT the main repo. You test changes here before merging
to MAIN.
>
> **Shell compatibility:** All commands below run identically in both bash and fish.
> Use `;` as separator (works in both); fish users should use `$status` not `$?` for
> exit status.

This project uses [Bats](https://bats-core.readthedocs.io/) for unit testing the `lab` launcher script. Tests validate path guards, security checks, and helper functions without requiring Docker or GPU access.

### Prerequisites
Install Bats on your host:
```bash
# Arch/CachyOS
sudo pacman -S bats

# macOS
brew install bats-core
```

### Running Tests (Host)
```bash
bats tests/
```

The test suite includes regression checks for:
- Valid workspace path acceptance
- Rejection of sensitive paths (`$HOME`, `$HOME/.ssh`, etc.)
- Ownership verification logic
- Git hook warning detection

### Running Tests via the Agent (`little-mester`)
You can instruct the agent to run the test suite inside the sandbox itself. This is useful for verifying that code changes don't break the launcher's security guards from within the container environment.

1. Ensure your workspace is set up (e.g., `workspaces/demo`).
2. Start a session: `./lab workspaces/demo`
3. Prompt the agent: *"Run the unit tests for the lab script using the provided helper script."*
4. The agent will execute `./run-tests.sh`, which safely invokes `bats` inside the container via `./lab exec`.

Alternatively, run it manually from your host:
```bash
./run-tests.sh workspaces/demo
```

---

## Repo map

| Path | What it is | Read this |
|---|---|---|
| `lab` | Launcher script. Validates paths, egress permissions, then runs compose. | [Security & AI Interaction Boundaries](#security--ai-interaction-boundaries) |
| `compose.yaml` | Defines services, networks, volumes and hardening flags. | [agent/README.md](agent/README.md) |
| `agent/opencode/opencode.json` | OpenCode agent config — model routing, permission defaults (ask/edit, ask/bash). | — |
| `models.txt` | List of models to download during setup. | — |
| `verify-sandbox.sh` | Regression tests for sandbox network/host guarantees. | — |
| `agent/` | Dockerfile for the container the agent runs in. | [agent/README.md](agent/README.md) |
| `ollama/` | Model-pulling script + notes on running models on 8 GB VRAM. | — |
| `workspaces/` | Where the projects the agent works on live (gitignored). | — |
| `docs/` | Journal, host setup notes, tickets. | [docs/journal.md](docs/journal.md) |
| `docs/tickets/` | Tickets with goals, steps, and progress tracking. | See individual ticket files |
| `tests/` | Unit tests for the `lab` launcher script (Bats). | — |
| `run-tests.sh` | Runs Bats inside the agent container via `./lab exec`. | — |
| `SECURITY.md` | **Security model, network isolation, capabilities.** | [Security & AI Interaction Boundaries](#security--ai-interaction-boundaries) |
| `scripts/` | Automation (regen-configs, check-syntax, ticket helpers). | [scripts/README.md](scripts/README.md) |

| Path | Status |
|---|---|
| `my-agent/` | *Planned.* Build your own agent loop from scratch. |
| `evals/` | *Planned.* Pass/fail tasks to compare models and prompts. |
| `red-team/` | *Planned.* Attacks against your own sandbox. |
| `proxy/` | *Planned.* Opt-in allowlisted web access. |
| `mcp/` | *Planned.* Tool servers for your agent. |

---

## How a session works

1. You run `./lab workspaces/demo`.
2. `lab` resolves the path, runs safety and egress checks, and asks you to confirm the mount.
3. `lab` runs `docker compose` with `WORKSPACE`, `LAB_UID` and `LAB_GID` set. It uses `sudo` because the docker daemon is root-owned.
4. Compose starts `ollama` if needed (health-checked) then starts the `agent` container: non-root, read-only fs, no Linux capabilities, `/workspace` mounted read-write, network gated by egress settings.
5. **By default opencode** sends your prompt to ollama (`http://ollama:11434/v1`). With `--aider`, Aider sends its request instead.
6. The agent edits files in `/workspace`, runs commands in the container, and commits as `little-mester-agent` via git.
7. When you quit, the container is removed (`--rm`) and `lab` warns if it finds git hooks.
8. **You review on the host** with `git log` and `git diff`.

Understanding steps 5 to 7 *is* agentic coding: a loop where a model proposes actions,
a program carries them out, and the results go back to the model. [my-agent/](#learning-path) is
where you build it yourself from scratch.

---

## Security model, briefly

Full version: [SECURITY.md](SECURITY.md).

An agent can only cause harm through what it can reach. This repo removes each channel:

| Channel | Control |
|---|---|
| Your files | Only one bind mount. Path guards in `lab`. No home dir, no Docker socket. |
| Credentials | None are mounted or passed in. |
| Network | `internal: true` network. No proxy running. |
| Privileges | Non-root user, `cap_drop: ALL`, `no-new-privileges`, read-only root fs. |
| Resources | `mem_limit`, `pids_limit`. |
| Exposure of the model server | No published ports. |

### What this does NOT protect against

Read this twice.

- **The agent can delete or corrupt everything inside the mounted workspace, `.git` included.**
  Keep backups or push to a remote *before* sessions.
- **Traps that run on your host later.** Git hooks, `Makefile`, `package.json` scripts,
  `.envrc`, `.vscode/tasks.json` and plain shell scripts written by the agent run with
  *your* privileges if you run them. Read agent-written code before executing it on the host.
- **Container escape.** Containers share the host kernel. A kernel or runtime vulnerability
  could break out. Unlikely from a small local model making mistakes, but real against
  deliberately hostile code. A VM is the next level up.
- **Docker group = root.** Anyone who can run `docker` can effectively become root on the host.
  `lab` uses `sudo` instead of adding you to the group.
- **Bad output.** A sandboxed agent can still write wrong or insecure code. Review it.

---

## Design decisions and trade-offs

| Decision | Why | Cost |
|---|---|---|
| Ollama in a container, not on the host | Self-contained, reproducible, no host port | Models live in a Docker volume; first setup re-downloads them |
| Internet off at runtime | Closes the "send data out" channel | Dependencies must be baked into the image or placed in the workspace |
| One-shot `ollama-pull` on a separate network | Keeps internet access out of the long-lived services | Extra service to understand |
| Docker over Podman | Most documentation and tooling assume it | Daemon runs as root. Rootless Podman is a good later hardening exercise |
| Agent commits as `little-mester-agent` | Easy to audit and revert agent changes | None really |
| Large local model (qwen3:8b / qwen3.6:35b-a3b) | Fits in available VRAM with proper quantization | Models need Docker volume; first setup re-downloads them |

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `nvidia-smi` fails on host | Driver problem. RTX 50-series needs open kernel modules. Fix before anything else. |
| `docker run --gpus all ... nvidia-smi` fails | NVIDIA container toolkit not configured. Re-run `sudo nvidia-ctk runtime configure --runtime=docker` and restart docker. |
| `ollama ps` shows part CPU | Model plus context doesn't fit in VRAM. Lower `OLLAMA_CONTEXT_LENGTH` or use a smaller model/quantization. |
| Agent can't reach `ollama` | Ollama not healthy yet, or the services aren't on the same network. |
| Files created by the agent are owned by root | `LAB_UID`/`LAB_GID` weren't passed through. Run via `./lab`, not `docker compose` directly. |
| Agent (or Aider) tries to reach the internet and stalls | Expected with no network. It should fall back. Note how long it takes in your journal. |

---

## Learning path

Do these in order. Each has its own README with goals, exercises and "you should be able to explain" questions.

1. Get the sandbox running and make `verify-sandbox.sh` pass. Read [SECURITY.md](SECURITY.md).
2. Use opencode (or `--agent aider`) on a small project. Review every diff. Journal what the model gets right and wrong.
3. Build your own agent loop: [my-agent/](my-agent/README.md).
4. Measure it: [evals/](evals/README.md).
5. Attack it: [red-team/](red-team/README.md).
6. Add controlled web access: [proxy/](proxy/README.md).
7. Add tool servers: [mcp/](mcp/README.md).

Keep [docs/journal.md](docs/journal.md) updated throughout. What broke and why is the most
valuable thing you'll produce here.

## Glossary

- **Agent**: a program that loops: ask the model what to do, do it, show the model the result.
- **Tool call**: a structured request from the model like "run `read_file` with path=x".
- **Context window**: how much text the model can consider at once. Costs VRAM.
- **Quantization**: storing model weights at lower precision so they fit in less memory.
- **Prompt injection**: untrusted text (a README, web page, issue) that tries to give the model instructions.
- **Bind mount**: a host folder made visible inside a container.
- **Capabilities**: slices of root's power in Linux. Dropped to zero here.
