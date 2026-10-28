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

A local LLM (Ollama or llama.cpp) drives a coding agent (**opencode** by default, **aider** via `--agent`) that edits files in **one folder you choose**. Agent internet is enabled by default; use `--isolated` to request offline mode. No host shell access. No access to your keys, home directory, or other projects unless explicitly supplied.

**The core ethos:** little-mester is a single tool built from MCP modules to allow multiple interfaces (opencode today, potentially codex or claude-code later) to act in a single well-defined, sandboxed way. Security is first. Consistency second. Human readability third.

> Status: personal learning project. Not security-audited. Treat the sandbox as strong guard rails, not a guarantee. See [SECURITY.md](SECURITY.md).

### Architecture

**Migration status (2026-10-28):** Use `./lab` (host Docker) for actual work; the VM is **not usable as a replacement yet**. `./lab-vm` installs OpenCode 2.2.0 and opens its TUI, but the owner reports `Malformed_HTTP_Response` from the forwarded llama.cpp endpoint, missing metrics, and a missing Ctrl-C catch. The VM remains an acceptance-test path, not the daily workflow. Keep the working Docker session/container intact; do not run both agents against the same checkout concurrently. #42 tracks fixing and verifying the guest experience; PostgreSQL Compose is a later test after basic model/TUI behavior works. Browser auth (#45), permission behavior (#50), nested virtualization/isolation (#43), and full lifecycle gates (#41) also remain open. See [VM architecture](docs/vm-architecture.md).

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

Agent internet is **enabled by default** (`[egress, internal]`). Disable with `--isolated` for an offline session. `.mester-egress` uses `key=value` fields; `permission=false` refuses egress-enabled launches. Host-Docker `allowed_domains` parsing is advisory; enforced proxy policy for the VM is tracked in #43. Agent limits are `mem_limit: 8g` and `pids_limit: 512`.

### Three ideas to hold onto

| Idea | How it's done here |
|---|---|
| The model can't touch files | Ollama/llama.cpp only produce text. No filesystem mounts except their own model volumes. |
| The agent touches only one folder | One bind mount (`/workspace`), gated by `lab`'s path checks. |
| Internet access is explicit | Egress is enabled by default; `--isolated` requests offline mode. Do not treat the host-Docker domain list as an enforced firewall. |

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

### Established host-Docker workflow (fallback)

**Use this workflow for development now.** VM mode has unresolved model connectivity and
TUI integration problems; do not switch your active work to it yet.

> **ChatGPT authentication:** VM browser login and credential persistence are still being tested; see [ChatGPT authentication](docs/chatgpt-auth.md) and ticket #45 before replacing a working authenticated session.

```bash
git clone <this-repo> little-mester
cd little-mester

# One-time host-Docker setup: install Docker + GPU toolkit, build images, pull models
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

### Current operating steps after a full shutdown

Use the established host-Docker agent for work, and start the VM separately when you need
to inspect/debug it. The VM is **not** ready to replace the working agent yet.

1. **Start the computer and Docker Engine.** Open a terminal in this repository and check:

   ```bash
   docker info
   docker compose version
   ```

   Run `./lab setup` only if this machine has not been set up yet or the images/models need
   installation; it is not a per-boot command. Do not run `docker compose down`, `docker
   system prune`, `./lab stop`, or VM teardown as part of a normal restart.

2. **Resume work in the host-Docker workflow** in terminal A. From the project you want to
   edit, launch the repository's `lab` script (adjust the path if the checkout is elsewhere):

   ```bash
   cd /path/to/little-mester
   ./lab /path/to/project
   ```

   When working on little-mester itself, from the checkout root use `./lab --unsafe-recurse .`.
   This is a new agent invocation after a full shutdown. OpenCode's `/home/agent` is currently tmpfs, so
   do not assume its login survived container removal/power-off. If it asks you to sign in,
   follow [`docs/chatgpt-auth.md`](docs/chatgpt-auth.md). Credential export/import across
   recreated sessions is not automated yet.

3. **Boot the VM for debugging** from terminal B, in the little-mester checkout. Use the
   same selected project path; add `--unsafe-recurse` only when deliberately using the
   little-mester repository itself as the guest workspace:

   ```bash
   cd /path/to/little-mester
   ./lab-vm status
   ./lab-vm up --workspace /path/to/project
   # For self-development instead:
   # ./lab-vm up --unsafe-recurse --workspace "$PWD"
   ./lab-vm status
   ./lab-vm ssh
   ```

   `up` leaves QEMU running after it returns. Use `ssh` for guest diagnostics; exit that SSH
   shell to return to the host without stopping the VM. Do **not** run `./lab-vm agent` on a
   workspace the host-Docker agent is actively editing: both would write the same files.
   Also avoid `--clean`, `./lab-vm stop`, `down`, or `rebuild`; those can destroy the VM's
   overlay/state. The VM's model forwarding is currently failing on the tested host, so
   starting the VM is useful for debugging but does not make its agent ready for normal work.

   To inspect that known model-path failure without changing host networking, use a separate
   host terminal:

   ```bash
   ./lab-vm -d models status
   ```

### VM acceptance-test workflow (direct guest agent)

This is for acceptance testing only—not normal work—while guest model connectivity, metrics,
and TUI key handling are unresolved. It runs OpenCode as a native guest process (not in an
agent container). **Current support is an experimental Linux x86_64/QEMU profile**, not a
claim of support for every Unix. Guest Docker/Compose is for project workloads; the VM does
not expose or share the host Docker socket. The optional model path is not yet portable:
`models up` currently uses the host Compose installation, which is explicitly not the target
architecture. No firewall or host-network changes are permitted to make it work. Manage the
current experimental host-to-guest path through `lab-vm`:

```bash
./lab-vm models up          # configured local model + metrics
./lab-vm -d models status        # bindings and live loopback probes
./lab-vm -d extra repair-model-api  # temporary diagnostics for ticket #42
```

`models up` selects the provider in `agent/opencode/opencode.json` by default; choose
`ollama`, `llamacpp`, or `all` explicitly when needed. It starts the selected Compose API
forwarders, whose ports are published by Docker on `127.0.0.1` only. It verifies actual host
HTTP reachability instead of trusting Compose/inspect port metadata. On failure, `-d`
reports Docker context/daemon/network and port-publication evidence without changing host
firewall or networking. The default `./lab` workflow does not start these services. Stop
only the API forwarders when finished with VM testing:

```bash
./lab-vm models down
```

`./lab-vm extra` delegates to `scripts/vm-extra.sh`, a **temporary** repair/diagnostic
script that must be removed after ticket #42's model, metrics and TUI acceptance gates pass.

On a fresh host, install the QEMU/KVM and `virtiofsd` prerequisites in
[`docs/01-host-setup.md`](docs/01-host-setup.md), then from the little-mester checkout:

```bash
./lab-vm base                                      # one-time base image
./lab-vm up --workspace /path/to/project           # provision VM and install OpenCode
./lab-vm agent                                     # attach to the guest OpenCode TUI
```

For an already-provisioned default VM, the normal start is simply:

```bash
./lab-vm up
./lab-vm agent
```

For self-development, run these from the little-mester repo root and explicitly allow the
repo workspace:

```bash
./lab-vm up --unsafe-recurse --workspace "$PWD"
./lab-vm agent
```

`up` installs/repairs the pinned default OpenCode version; `agent` only checks readiness
and launches it. The agent has broad authority inside the guest, including guest Docker;
the outer VM is the host-isolation boundary. VM mode is not yet a replacement for `./lab`:
keep the existing Docker workflow available and follow the current gates on tickets #41–#45
and #50. Do not concurrently edit the same workspace from both agents.

Hardware configuration lives in [`machine.conf.example`](machine.conf.example). Copy it to `machine.conf` (gitignored) and fill in your specs. The benchmark tools use it to suggest optimal starting points.

---

## Everyday use

The commands below are for the established host-Docker workflow. For the VM acceptance
workflow, use `./lab-vm up` followed by `./lab-vm agent` as described above.

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

## License

Copyright (c) 2026 Ethan Freestone.

Unless otherwise noted, little-mester, including `little-mester-mcp`, is licensed
under the **GNU Affero General Public License, version 3 only**
(`AGPL-3.0-only`). See [LICENSE](LICENSE) for the complete terms.

- Personal and commercial use, including selling copies, is permitted without
  licence fees, revenue limits, or team-size limits, subject to the AGPL.
- Distributing covered versions requires providing corresponding source under the
  AGPL. Modified versions supporting remote network interaction must prominently
  offer their corresponding source to users interacting with them remotely.
- Purely private, local modifications do not have to be published.
- Merely using this tool to develop an independent project does not place that
  project under the AGPL; incorporating covered code may create obligations.

The software comes **without warranty**, as described in the licence. Third-party
software and model weights retain their respective licences. This change does not
withdraw permissions already granted for versions published under MIT or ISC.

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
| ChatGPT login in containers/VMs | [docs/chatgpt-auth.md](docs/chatgpt-auth.md) |
| GitHub Actions / CI decisions | [docs/github-actions.md](docs/github-actions.md) |
| Ticket engine internals | [docs/tickets/architecture/DECISIONS.md](docs/tickets/architecture/DECISIONS.md) |
| Scripts reference | [scripts/README.md](scripts/README.md) |
| Agent container details | [agent/README.md](agent/README.md) |
| MCP server docs | [little-mester-mcp/docs/README.md](little-mester-mcp/docs/README.md) |

---

*In the established `./lab` workflow, the agent runs inside a Docker container and the workspace is bind-mounted at `/workspace`. VM mode instead runs OpenCode directly in the guest OS; it remains under acceptance testing and is not yet a replacement for host-Docker mode.*
