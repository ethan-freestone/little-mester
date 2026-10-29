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

**Migration status (2026-10-29):** `./lab` is currently the established host-Docker workflow; `./lab-vm` is the experimental direct-guest VM workflow and is **not ready to replace it**. The VM TUI has unresolved model connectivity, metrics, and Ctrl-C behavior. Keep the working Docker session/container intact and do not run two agents against the same checkout concurrently. The VM will become the default `./lab` route only after the model/TUI/metrics/exit, browser auth, permission, guest workload, isolation, and lifecycle gates pass. The existing Docker workflow will be retained as an explicit `./lab-docker` option; it is not Docker-in-Docker and does not expose the host Docker socket. During migration, use `./lab` for established Docker work and `./lab-vm` only for acceptance testing. See [VM architecture](docs/vm-architecture.md) and decision §C.9.17.

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

### Current host-Docker workflow

Use this established workflow for actual work until the VM acceptance gates pass. The future
VM-first launcher will make `./lab <workspace>` enter the VM agent directly; that cutover is
not implemented yet.

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
not expose or share the host Docker socket. VM metrics are being developed separately from
the existing host-Docker metrics path; neither path should be removed until both modes have
verified coverage. The optional model path is not yet portable:
`models up` currently uses the host Compose installation, which is explicitly not the target
architecture. No firewall or host-network changes are permitted to make it work. Manage the
current experimental host-to-guest path through `lab-vm`:

```bash
    ./lab-vm models up          # configured local model API forwarder
    ./lab-vm metrics up         # separate host-native VM metrics endpoint
    ./lab-vm models status      # concise state and host reachability summary
    ./lab-vm -d models diagnose # detailed, read-only host/model-path evidence
```

`models up` selects the provider in `agent/opencode/opencode.json` by default; choose
`ollama`, `llamacpp`, or `all` explicitly when needed. It starts only the selected Compose
model API forwarders, whose ports are published by Docker on `127.0.0.1` only. It verifies
actual host HTTP reachability instead of trusting Compose/inspect port metadata. `models status`
summarizes whether each running forwarder has an active host port mapping and responds on
loopback. If it does not, run `./lab-vm -d models diagnose` before collecting ad-hoc evidence.
That command records resolved Compose ports, Docker context/daemon mode, container bindings and
network attachments, `docker port`, relevant host listeners, loopback HTTP probes, backend health,
and available Docker journal diagnostics in the normal `.mester/logs/` run log. It is read-only: it
does not start/recreate containers, alter daemon/firewall/network settings, or remove orphans.
The output identifies unavailable checks and exits nonzero if a running forwarder is unhealthy.
VM metrics
use a separate native host process at `127.0.0.1:19532`, independent of Docker; it returns
only curated metrics API data and is reachable from the guest through the existing QEMU
guestfwd. The host-Docker `metrics` Compose service is unchanged. `models diagnose` is the
standard first step when API status is unhealthy; `models up -d` also prints publication
diagnostics if startup fails. Neither changes host firewall or networking. The default `./lab`
workflow does not start these VM services. Stop the API forwarders and metrics process
separately when finished with VM testing:

```bash
./lab-vm models down
./lab-vm metrics down
```

`./lab-vm extra` delegates to `scripts/vm-extra.sh`, a **temporary** end-to-end guest
acceptance helper that must be removed after ticket #42's model, metrics and TUI gates pass.
Use the built-in `models diagnose` command for host Docker/model-forwarder troubleshooting;
it is read-only and does not attempt repairs.

On a fresh host, install the QEMU/KVM and `virtiofsd` prerequisites in
[`docs/01-host-setup.md`](docs/01-host-setup.md), then from the little-mester checkout:

```bash
./lab-vm base                                      # one-time base image
./lab-vm up --workspace /path/to/project           # provision VM and install OpenCode
./lab-vm agent                                     # attach to the guest OpenCode TUI
```

For an already-provisioned default VM, acceptance testing currently requires two commands:

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

`base` downloads/prepares the reusable base image once. `up` starts/provisions the VM and
installs or repairs the pinned OpenCode version; it currently returns to the host rather
than launching the TUI. `agent` checks readiness and attaches the interactive TUI. `ssh` is
for guest diagnostics, not a required daily step. The target daily path is one command that
starts/reuses the VM and launches OpenCode; that behavior is tracked for future implementation
and is not yet available.

**Shutdown and data retention:** Current `./lab-vm stop` is not a gentle pause: it runs
`docker compose down -v --remove-orphans` in the guest, then calls `down`. `./lab-vm down`
stops QEMU and deletes the writable VM overlay, seed ISO, and VM control keys (the cached base
image remains). Either command can discard guest state; `stop` also removes guest Compose
volumes. There is not yet a supported non-destructive end-of-day suspend/stop command. Do not
use `stop`, `down`, `--clean`, or `rebuild` on a VM whose state you need to preserve. This
behavior must be improved and documented before VM mode is recommended for daily use.

The agent has broad authority inside the guest, including guest Docker; the outer VM is the
host-isolation boundary. VM mode is not yet a replacement for `./lab`: keep the existing
Docker workflow available and follow the current gates on tickets #41–#45 and #50. Do not
concurrently edit the same workspace from both agents. Do not mount the Little Mester
repository unless recursion is explicitly enabled. Bounded `--recurse` and `--unsafe-recurse`
(self-modification) with a maximum nesting depth defaulting to zero are approved design
requirements, but are not implemented yet; see the linked recursion ticket.

Hardware configuration lives in [`machine.conf.example`](machine.conf.example). Copy it to `machine.conf` (gitignored) and fill in your specs. The benchmark tools use it to suggest optimal starting points.

---

## Everyday use

The commands below are for the established host-Docker workflow. For the VM acceptance
workflow, use `./lab-vm up` followed by `./lab-vm agent` as described above. After the
acceptance gates pass, `./lab` is intended to use the VM by default and the current Docker
workflow will remain available as `./lab-docker`.

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
