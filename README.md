# little-mester

```
 ▄▄▄▄▄▄▄▄▄▄▄▄▄   ██        ██████  ██████████  ██████████  ██        ████████
 █████████████░  ██▒        ▒██▒▒▒  ▒▒▒██▒▒▒▒▒  ▒▒▒██▒▒▒▒▒ ██▒       ██▒▒▒▒▒▒▒
 █▒█▒█▒█▒█▒█▒█░  ██▒         ██▒       ██▒         ██▒     ██▒       ██████
 █▒█▒█▒█▒█▒█▒█░  ██▒         ██▒       ██▒         ██▒     ██▒       ██▒▒▒▒▒
 █████████████░  ████████  ██████      ██▒         ██▒     ████████  ████████
 █▒█▒█▒█▒█▒█▒█░   ▒▒▒▒▒▒▒▒  ▒▒▒▒▒▒      ▒▒          ▒▒      ▒▒▒▒▒▒▒▒  ▒▒▒▒▒▒▒▒
 █▒█▒█▒█▒█▒█▒█░
 █████████████░  ██      ██  ████████    ██████  ██████████  ████████  ██████
 █▒█▒█▒█▒█▒█▒█░  ████  ████▒ ██▒▒▒▒▒▒▒ ██ ▒▒▒▒▒▒  ▒▒▒██▒▒▒▒▒ ██▒▒▒▒▒▒▒ ██▒▒▒▒██
 █▒█▒█▒█▒█▒█▒█░  ██▒▒██ ▒██▒ ██████    ████          ██▒     ██████    ██████ ▒▒
 ▐▓▓▓▓▓▓▓▓▓▓▓▓▓▌  ██▒  ▒▒ ██▒ ██▒▒▒▒▒    ▒▒▒▒ ██      ██▒     ██▒▒▒▒▒   ██▒▒██▒
 █  █  ▐▌  █  █  ██▒     ██▒ ████████  ██████ ▒▒     ██▒     ████████  ██▒  ▒██
 █  █  ▐▌  █  █   ▒▒      ▒▒  ▒▒▒▒▒▒▒▒  ▒▒▒▒▒▒        ▒▒      ▒▒▒▒▒▒▒▒  ▒▒    ▒▒
 ▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔  Made in Sheffield
```

A locked-down, fully local sandbox for learning agentic coding.

A local LLM (Ollama or llama.cpp) drives a coding agent (**opencode** by default, **aider** via `--agent`) that edits files in **one folder you choose**, nothing else. No internet by default. No host shell access. No access to your keys, home directory, or other projects.

**The core ethos:** little-mester is a single tool built from MCP modules to allow multiple interfaces (opencode today, potentially codex or claude-code later) to act in a single well-defined, sandboxed way. Security is first. Consistency second. Human readability third.

> Status: personal learning project. Not security-audited. Treat the sandbox as strong guard rails, not a guarantee. See [SECURITY.md](SECURITY.md).

### Architecture

```
 HOST (any Linux / macOS with Docker)
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │                                                                              │
 │  ./lab ──► docker compose                                                    │
 │                                                                              │
 │   ┌──────────── network: internal [+ optional egress] ───────────────────┐  │
 │   │                                                                       │  │
 │   │  ┌─────────────┐                                                      │  │
 │   │  │ agent       │  http://ollama:11434/v1   ┌─────────────────────┐   │  │
 │   │  │ (opencode/  │ ──────────────────────► │ ollama              │   │  │
 │   │  │  aider,     │                         │ (model server,      │   │  │
 │   │  │  git, py)   │  http://llamacpp:11434/v1 │ uses GPU)          │   │  │
 │   │  │ non-root    │ ──────────────────────► │ no published ports  │   │  │
 │   │  │ read-only   │                         └─────────┬───────────┘   │  │
 │   │  │ no caps     │                                   │               │  │
 │   │  └──────┬──────┘                                   │               │  │
 │   │         │ bind mount (rw)                          ▼               │  │
 │   │         ▼                              ┌───────────────────┐       │  │
 │   │  /workspace (YOUR PROJECT)             │ ollama-models     │       │  │
 │   │  (ONLY host folder agent sees)         │ (model weights)   │       │  │
 │   │                                        └───────────────────┘       │  │
 │   │                                                                    │  │
 │   │  ┌─────────────┐                         ┌───────────────────┐   │  │
 │   │  │ metrics     │ ◄─── /host/proc etc.    │ llamacpp-models   │   │  │
 │   │  │ (sidecar)   │   read-only host hw     │ (GGUF files)      │   │  │
 │   │  └─────────────┘                         └───────────────────┘   │  │
 │   │                                                                    │  │
 │   │  --egress / .mester-egress ──► optional egress network ──► internet│  │
 │   └────────────────────────────────────────────────────────────────────┘
 │                                                                              │
 └──────────────────────────────────────────────────────────────────────────────┘
```

Agent internet is **enabled by default** (`[egress, internal]`). Disable with `--isolated` for an offline session. When egress is on, outbound traffic is filtered against `.mester-egress` (one hostname per line) or allowed unconditionally with `--egress-true`.

### Three ideas to hold onto

| Idea | How it's done here |
|---|---|
| The model can't touch files | Ollama/llama.cpp only produce text. No filesystem mounts except their own model volumes. |
| The agent touches only one folder | One bind mount (`/workspace`), gated by `lab`'s path checks. |
| The agent can't phone home | Model + agent share `internal: true`. Egress is opt-in. Only pull services have internet, briefly, during setup. |

---

## Read this first (humans and AI agents)

This repo has **two decision logs**. Read both before making changes:

| File | Scope | Read before |
|---|---|---|
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | How to use, configure, test, and improve little-mester | Any session |
| [`docs/tickets/architecture/DECISIONS.md`](docs/tickets/architecture/DECISIONS.md) | Engineering designs for the ticket engine | Any `little-mester-mcp/` change |

`docs/DECISIONS.md` is split into focused sub-logs:
- **§A** — How to use · **§K** — Design decisions · **§L** — Security caveats
- [`docs/testing.md`](docs/testing.md) — Testing policy
- [`docs/ticketing.md`](docs/ticketing.md) — Ticket schema & workflow
- [`docs/opencode.md`](docs/opencode.md) — OpenCode customisation
- [`docs/github-actions.md`](docs/github-actions.md) — CI/CD decisions

The default development cycle is **doc → test → code → commit**. See [`docs/workflows.md`](docs/workflows.md). Do not skip stages or reorder them without a ticket explaining why.

---

## Setup

```bash
git clone <this-repo> little-mester
cd little-mester

# One-time setup: install Docker + GPU toolkit, build images, pull models
./lab setup

# Verify sandbox properties (network isolation, capabilities, etc.)
./verify-lab.sh

# Create a project workspace
mkdir -p workspaces/my-project
cd workspaces/my-project
git init

# Start the agent
./lab .
```

**Requirements:** Docker with GPU support (`nvidia-smi` works on host), `sudo`, and a working GPU driver. Other distros and GPU vendors may need adjustments to `./lab setup` — see [`docs/`](docs/).

Hardware configuration lives in [`machine.conf.example`](machine.conf.example). Copy it to `machine.conf` (gitignored) and fill in your specs. The benchmark tools use it to suggest optimal starting points.

---

## Everyday use

```bash
./lab <workspace>                   # start opencode (default agent)
./lab --aider    <workspace>        # use Aider for this session
./lab shell <workspace>             # plain bash inside the sandbox
./lab stop                          # stop all containers
./lab pull                          # re-pull models after editing models.txt
./lab build                         # rebuild agent image
./verify-lab.sh                     # re-verify sandbox guarantees
```

`<workspace>` can be a single repo or a parent folder holding several repos. The launcher refuses `/`, `$HOME`, sensitive paths (`~/.ssh`, `~/.config`, etc.), and by default any path that would expose the little-mester repo itself.

### Key flags

| Flag | Effect |
|---|---|
| `--agent aider\|opencode` | Pick agent for this session |
| `--isolated` or `--egress-false` | No outbound internet |
| `--egress-true` | Allow internet (checked against `.mester-egress`) |
| `--unsafe-recurse` | Allow the agent to edit little-mester itself |
| `--ro` / `--rw` | Read-only or read-write mount |
| `--workflow <name>` | Select a workflow profile (see [`docs/workflows.md`](docs/workflows.md)) |

### Self-editing

To let the agent modify the little-mester repo:

```bash
cd /path/to/little-mester
./lab --unsafe-recurse    # allows opencode to read/write this directory
```

This is off by design — it signals "I know what I'm doing." Without `--unsafe-recurse`, the guard always blocks self-mapping.

---

## What the sandbox protects

| Channel | Control |
|---|---|
| Your files | Only one bind mount. Path guards in `lab`. |
| Network | `internal: true` network. No published ports. Egress defaults to on; filtered by `.mester-egress` or `--egress-true`. |
| Privileges | Non-root, `cap_drop: ALL`, `no-new-privileges`, read-only root fs. |
| Resources | Memory and PID limits on the agent container. |

Full details: [SECURITY.md](SECURITY.md).

---

## Workflows

The [`docs/workflows.md`](docs/workflows.md) file describes these in detail:

| Workflow | When to use |
|---|---|
| **Pair programming** (default) | Agent writes code you review. Most common mode. |
| **Chat** | Ask questions, explore code, get explanations. |
| **Teaching/Learning** | Agent explains concepts, walks through patterns. |
| **Testing** | Agent writes and runs tests against your codebase. |
| **Ticket-driven** | Create tickets first, implement incrementally. |

Select a workflow with `--workflow <name>`.

---

## Where to go from here

| What you need | Go to |
|---|---|
| **Getting started** | [docs/DECISIONS.md](docs/DECISIONS.md) §A · [Workflows](docs/workflows.md) |
| Design decisions & trade-offs | [docs/DECISIONS.md](docs/DECISIONS.md) §K |
| Security caveats (what's NOT protected) | [docs/DECISIONS.md](docs/DECISIONS.md) §L · [SECURITY.md](SECURITY.md) |
| Testing policy & ticket workflow | [docs/testing.md](docs/testing.md) |
| Ticket schema & workflow decisions | [docs/ticketing.md](docs/ticketing.md) |
| OpenCode customisation | [docs/opencode.md](docs/opencode.md) |
| GitHub Actions / CI decisions | [docs/github-actions.md](docs/github-actions.md) |
| Ticket engine internals | [docs/tickets/architecture/DECISIONS.md](docs/tickets/architecture/DECISIONS.md) |
| Scripts reference | [scripts/README.md](scripts/README.md) |
| Agent container details | [agent/README.md](agent/README.md) |
| MCP server docs | [little-mester-mcp/docs/README.md](little-mester-mcp/docs/README.md) |

---

*The agent runs inside a Docker container. Your workspace is bind-mounted at `/workspace`. The model servers (Ollama + llama.cpp) run in separate containers on an internal network. Egress is on by default (filtered by `.mester-egress`); use `--isolated` to disable or `--egress-true` for unconditional allow.*
