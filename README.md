# little-mester

A **locked-down, fully local sandbox for learning agentic coding.**

<<<<<<< HEAD
A local LLM (via **Ollama** and/or **llama.cpp**, on the GPU) drives a coding agent (**opencode** by default, or **aider** via `--agent`) that can read and edit files in **one folder you choose**, and nothing else. No internet by default. No access to your home directory, keys, or other projects.

**IMPORTANT: You are always in a container.** The agent runs inside a Docker container. The model server (Ollama or llama.cpp) runs in a separate container. Your workspace is bind-mounted at `/workspace` inside the agent container. The host filesystem is NOT accessible except for that one mount.
=======
A local LLM (via **Ollama** and/or **llama.cpp**, on the GPU) drives a coding agent (**opencode** by default, or **aider** via `--agent`) that can read and edit files in **one folder you choose**, and nothing else. No internet by default. No access to your home directory, keys, or other projects.

**IMPORTANT: You are always in a container.** The agent runs inside a Docker container. The model server (Ollama or llama.cpp) runs in a separate container. Your workspace is bind-mounted at `/workspace` inside the agent container. The host filesystem is NOT accessible except for that one mount.
>>>>>>> main

The goal: two things in one repo.

1. A safe place to point an AI agent at code without worrying about your machine.
2. A **learning lab**: every piece is small enough to read, break, and rebuild.

> Status: personal learning project. Nothing here has been security-audited.
> Treat the sandbox as strong *guard rails*, not a guarantee. See [What this does NOT protect against](#what-this-does-not-protect-against).

---

## Build status

- ✅ 0. Host NVIDIA driver works (`nvidia-smi`)
- ✅ 1. Repo skeleton, `.gitignore`, `models.txt`
- ✅ 2. `compose.yaml` (dual backend: ollama + llama.cpp)
- ✅ 3. `agent/Dockerfile`, model pull scripts
- ✅ 4. `lab` launcher with path guard rails & egress gate (includes `.mester-egress`)
- ✅ 5. `./lab setup` completes (packages, image build, models pulled)
- ✅ 6. `./verify-lab.sh` all green
- ✅ 7. First opencode session on a throwaway repo
- [ ] 8. Multi-repo workspace tested
- [ ] 9. Own agent loop (`my-agent/`)
- [ ] 10. Evals (`evals/`)
- [ ] 11. Benchmarking (`bench/`)
- [ ] 11. Red-team the sandbox (`red-team/`)
- [ ] 12. Opt-in web access via proxy (`proxy/`)
- [ ] 13. MCP (`mcp/`)

---

## Architecture

Default networking (no overrides):

```
 HOST (your machine — CachyOS/Arch recommended)
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
 │   │  └──────┬──────┘                                   │ named volume  │  │
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

Agent internet is **enabled by default** (`[egress, internal]`). To disable (e.g. for isolated sub-agents):

| Method | Compose override | Effect |
|---|---|---|
| Lab flag | `--isolated` | Sets agent networks to `[internal]` only — no outbound internet |
| Lab flag | `--egress-false` or `--egress false` | Same as `--isolated` |
| Direct compose | `-e NETWORKS_AGENT=internal` + omit egress from `networks.agent` override | Same effect, more explicit but requires editing compose |

### Three ideas to hold onto

| Idea | How it's done here |
|---|---|
<<<<<<< HEAD
| The model can't touch files | Ollama only turns text into text. It's in its own container with no mounts except its model volume. |
| The agent can only touch one folder | One bind mount (`/workspace`), gated by `lab`'s path checks. |
| The agent can't phone home | Agent + Ollama share an `internal: true` network. Egress is opt-in via `--egress`/`.mester-egress`, and only `ollama-pull` (setup profile) has it during setup. |

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
./lab workspaces/demo             # start opencode on that folder (use --aider for Aider)
./lab shell workspaces/demo      # plain bash inside the sandbox
./verify-sandbox.sh              # verify security guarantees hold
```

Full usage is in [docs/workflows.md](docs/workflows.md).

### Host metrics sidecar

A lightweight metrics sidecar runs alongside `ollama` and the agent, exposing live GPU, CPU, RAM, and
temperature data from inside the container — so the agent has *real* hardware visibility without breaking
the sandbox or requiring the user to leave their terminal.

*   **Detailed JSON**: `curl -s http://metrics:19532/metrics` → structured data with all four metric domains (GPU, CPU, RAM, temperatures).
*   **Quick summary**: `curl -s http://metrics:19532/summary` → one human-readable line (`[timestamp] CPU load ... | RAM X/Y GB (%) | GPU0 RTXxxx util=ZZ% VRAM A/BGB C°C`).
*   **History**: `curl -s 'http://metrics:19532/history?n=60'` → the last N samples from a 500 ms sampler ring buffer, used by the TUI sparkline to seed charts immediately.
*   The service publishes **no host port** — `metrics` is a Docker DNS name on the compose `internal` network, so it resolves only from inside the stack.

> **Agent query tool**: `get_host_metrics` is registered by the [`host-metrics` plugin](agent/opencode/plugins/host-metrics/) and documented in [`agent/README.md`](agent/README.md). The agent calls it whenever it needs to know VRAM availability before proposing large model operations, or to diagnose unexpected behavior (e.g., CPU fallback). One call returns a formatted report.
>
> **Live TUI view**: the [`host-metrics-tui` plugin](agent/opencode/plugins/host-metrics-tui/) renders a compact GPU/VRAM sparkline in the sidebar footer, expandable for full time-series detail.
>
> **Acceptance verification**: Run `./scripts/verify-metrics.sh` on the host after starting up.

### Monitoring the agent

The agent (opencode by default) runs inside a container. Monitor it from the host:

*   **Container logs**: view the agent's output and errors:
    ```bash
    docker logs -f little-mester-agent-1
    ```
*   **Resource usage**: monitor CPU/RAM of the agent container:
    ```bash
    docker stats little-mester-agent-1
    ```
*   **Interactive shell**: if the agent hangs or you need to debug file permissions:
    ```bash
    ./lab shell workspaces/my-experiment
    # You are now inside the container as a non-root user
    ls -la /workspace
    exit
    ```

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
- **Bad output.** A sandboxed agent can still write wrong or insecure code. Review it.

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
| `docs/tickets/` | Tickets by area/engine (JSON-first) | See DECISIONS.md for decision log |
| `ticket-engine-mcp/` | Lightweight MCP-first ticketing system | `ticket-engine-mcp/docs/blueprint.md` |
| `scripts/` | Automation tools — tickets, hooks, sync | [Scripts](#scripts-and-automation) above |
| `tests/lab.bats` | Bats tests for the `lab` launcher | [Testing](#testing) below |
| `skills/` | *Planned.* Reusable workflow skills (not yet created) | — |
| `SECURITY.md` | **Security model, network isolation** | [Security above](#security--ai-interaction-boundaries) |

---

## Testing

> **Testing context:** run tests from the working checkout (e.g. `little-mester-working` on `recurse-work`), NOT the main repo. You test changes there before merging to `main`.
>
> **Shell compatibility:** all commands below run identically in both bash and fish. Use `;` as separator (works in both); fish users should use `$status` not `$?` for exit status.

This project uses [Bats](https://bats-core.readthedocs.io/) for unit testing the `lab` launcher script. Tests validate path guards, security checks, and helper functions without requiring Docker or GPU access.

### Prerequisites

Install Bats on your host:
```bash
# Arch/CachyOS
sudo pacman -S bats

# macOS
brew install bats-core
```

### Running tests (host)

```fish
bats tests/
```

The test suite includes regression checks for:
- Valid workspace path acceptance
- Rejection of sensitive paths (`$HOME`, `$HOME/.ssh`, etc.)
- Ownership verification logic
- Git hook warning detection

#### Switching Models
By default, Ollama serves whichever is first in `models.txt` (or last ran). To switch:
1.  Ensure the target model is downloaded (see above).
2.  If your agent supports explicit model selection (opencode: via config; Aider: via command-line flag), pass it. Otherwise rely on Ollama's default behavior.
3.  **VRAM Check**: If the agent stalls or falls back to CPU, check `nvidia-smi`. If VRAM is full, switch to a smaller quantization (e.g., `q4_K_M`) or a smaller model (e.g., `phi3` instead of `llama3`).

To **add** a model: open `models.txt`, add the model name (e.g., `llama3.2:latest`, `mistral`), then run `./lab setup`. This triggers the `ollama-pull` container to download the new model over the internet.

To **monitor** loaded models:
*   **List models**: `docker exec ollama ollama list` inside the container.
*   **Check VRAM**: on your host, run `nvidia-smi` and look for the `ollama` process. If it is using CPU (high memory usage in RAM instead of VRAM), your model is too large for your GPU.

### Agent tests (inside sandbox)

You can instruct the agent to run the test suite inside the sandbox itself. This is useful for verifying that code changes don't break the launcher's security guards from within the container environment.

1. Ensure your workspace is set up (e.g., `workspaces/demo`).
2. Start a session: `./lab workspaces/demo`
3. Prompt the agent: *"Run the unit tests for the lab script using the provided helper script."*
4. The agent will execute `./run-tests.sh`, which safely invokes `bats` inside the container via `./lab exec`.

Alternatively, run it manually from your host:
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
- How architecture decisions are recorded — see below
- Ticket format and status transitions
- MCP development workflow
- Session handoff template

> ⚠️ **Important: Decision logs have different scopes — read BOTH before working:**
> - **[docs/DECISIONS.md](docs/DECISIONS.md)** → How to **use** little-mester (workflow, --unsafe-recurse, security, testing). Read before ANY session on this repo.
> - **(engine decisions)** [docs/tickets/architecture/DECISIONS.md](tickets/architecture/DECISIONS.md) → engineering designs for building the ticket engine (schema, epics, search, labels, routing). Read before any `ticket-engine-mcp/` change.
=======
| The model can't touch files | Ollama/llama.cpp only turn text into text. They're in their own containers with no mounts except their model volumes. |
| The agent can only touch one folder | The agent container sees exactly one bind mount, `/workspace`, gated by `lab`'s path checks. |
| The agent can't phone home | By default the agent and model servers share an `internal: true` network. Egress is opt-in via `--egress`/`.mester-egress`. Only `ollama-pull` / `llamacpp-pull` have it during setup. |

---

## Dual Backend: Ollama + llama.cpp

This system runs **both** backends simultaneously. You choose per model.

| Backend | Best for | Model format | How it works |
|---|---|---|---|
| **Ollama** | Small dense models that fit entirely in VRAM | Ollama format (auto-pulled) | Layer-wise GPU offload |
| **llama.cpp** | Large MoE models (35B-A3B, 30B-A3B, 20B) | GGUF (downloaded from HuggingFace) | **Expert-level offload** (`--n-cpu-moe`) |

### Why llama.cpp for MoE?

Your hardware: **Strix Point (HX 370)**, 12 cores, 16-CU Radeon 890M, **8 GB VRAM**, ~120 GB/s system RAM bandwidth.

- Ollama splits MoE by **layer** (not expert) → worst case for 35B-A3B on 8GB
- llama.cpp supports `--n-cpu-moe` → **expert-level offload**:
  - **GPU**: attention, shared layers, KV cache, active experts (~3B params)
  - **CPU (RAM)**: remaining experts (~20GB at Q4, paged from system RAM)
- Theoretical: 3B active @ Q4 = ~1.8 GB read/token ÷ 120 GB/s = **~60 t/s ceiling**
- Expected: **20–35 t/s** in practice

### Model selection in `models.txt`

```
[ollama]
qwen3:8b              65536     0.25   # fits entirely in 8GB VRAM, very fast
qwen3:14b             65536     0.25   # may fit with reduced context

[llamacpp]
qwen3-35b-a3b-q4_k_m  65536     0.25   # main coder, expert offload
qwen3-coder-30b-a3b-q4_k_m  65536     0.25   # coding-tuned variant
gpt-oss-20b-q4_k_m    65536     0.25   # MXFP4, ~12GB, ~3.6B active, great tool calling
```

Edit `models.txt`, then run `./lab setup` (or `./lab pull`).
>>>>>>> main

---

## Quick start (new machine)

<<<<<<< HEAD
Requirements: Arch/CachyOS, working NVIDIA driver, `sudo`.
=======
**Requirements:** Arch/CachyOS (other distros: see `docs/01-host-setup.md`), working NVIDIA driver, `sudo`.
>>>>>>> main

```bash
git clone <this-repo> little-mester
cd little-mester

# One-time setup: installs Docker + GPU toolkit, builds images, pulls models
./lab setup

# Verify sandbox properties (network isolation, caps, etc.)
./verify-lab.sh

# Create a project workspace
mkdir -p workspaces/demo
cd workspaces/demo
git init

# Start the agent (opencode by default)
./lab workspaces/demo
```

---

## Everyday use

```bash
./lab <workspace>               # start opencode (default agent)
./lab --aider    <workspace>    # use Aider for this session
./lab --agent opencode <path>   # explicitly pick opencode
./lab <workspace> --help        # all flags: --unsafe-recurse, --egress, --isolated…
./lab shell <workspace>         # plain bash inside the sandbox (non-root)
./lab stop                      # stop Docker compose
./verify-lab.sh             # re-run after any change to compose.yaml / Dockerfile
./lab pull                      # re-pull models after editing models.txt
./lab build                     # rebuild agent image after config changes
```

`<workspace>` can be a single repo, or a parent folder holding several repos. The launcher refuses `/`, `$HOME`, anything you don't own, anything overlapping `~/.ssh`, `~/.config`, and **by default** any path that would expose this lab repo itself (use `--unsafe-recurse` to override).

<<<<<<< HEAD
### Workspace management

The `lab` script enforces strict path rules to ensure security.

*   **Valid paths**: must be an absolute path (or relative to the current dir) pointing to a directory you own.
*   **Invalid paths**: `/`, `$HOME`, anything overlapping `~/.ssh`, `~/.config`, or the `little-mester` repo itself.
*   **Multi-repo workspaces**: you can point `./lab` at a parent folder containing multiple git repos. The agent will see all files in that tree.

**Tip**: create a dedicated folder for your experiments:
```bash
mkdir -p workspaces/my-experiment
cd workspaces/my-experiment
git init # Initialize your project here
```

### Self-modification (`--unsafe-recurse`)
=======
### Self-modification (recursive improvement)
>>>>>>> main

When you want the agent to edit little-mester itself:

```bash
cd <path-to-little-mester-checkout>
./lab --unsafe-recurse    # allows opencode to read/write the current directory
```

You do not need to append `.` — it is implicit when using `--unsafe-recurse`.

This is off by design — it tells the agent "I know what I'm doing." Without `--unsafe-recurse`, the guard always blocks self-mapping. This makes recursive improvement possible **without making it the default**.

---

<<<<<<< HEAD
## How a session works
=======
## Model Management

### Adding / Changing Models

1. Edit `models.txt` (see format above)
2. Run `./lab pull` — this starts the appropriate pull service:
   - `ollama-pull` for `[ollama]` models (downloads from Ollama registry)
   - `llamacpp-pull` for `[llamacpp]` models (downloads GGUF from HuggingFace)

### Switching Models at Runtime

**In opencode:** Use `/model <provider>/<model>` command, e.g.:
- `/model ollama/qwen3:8b`
- `/model llamacpp/qwen3-35b-a3b-q4_k_m`

**In Aider:** Pass `--model` flag when starting, or restart with different model.

### VRAM Monitoring

```bash
# Host-side
nvidia-smi -l 1

# Inside agent container (via metrics sidecar)
curl -s http://metrics:19532/summary
# Or use the get_host_metrics tool from opencode
```

If you see the model process using **system RAM instead of VRAM**, it's fallen back to CPU. Reduce context, use smaller quantization, or switch to a smaller model.

---

## Hardware & Performance Tuning
>>>>>>> main

### Critical for sustained runs (laptop)

<<<<<<< HEAD
Understanding steps 4 to 6 *is* agentic coding: a loop where a model proposes actions,
a program carries them out, and the results go back to the model. [my-agent/](#learning-path) is
where you build it yourself from scratch.

### Agentic workflow tips

*   **Context window**: the model has a limited context window. If your workspace is huge, the agent might forget earlier instructions. Use `git add` to stage specific files you want it to focus on.
*   **Git hooks**: the agent writes code that runs on your host if you execute hooks (like `Makefile` or `.envrc`). **Always review** these before running them.
*   **Iterative prompts**: break complex tasks into small steps. "Refactor the login module" is better than "Fix the app".
=======
| Setting | Why |
|---|---|
| **Plugged in + performance profile** | Prevents throttling |
| **dGPU in discrete/MUX mode** | Prevents 5070 routing through iGPU |
| **Cooling stand / external fan** | **Throttling costs more than any flag** |
| **Close browsers, Electron apps** | Frees system RAM for CPU-side experts |
| **Don't set `mem_limit` on model container** | Agent's 8GB limit is fine; model container needs flexibility |

### llama.cpp flags (in compose.yaml)

| Flag | Purpose | Tune? |
|---|---|---|
| `-ngl 99` | Offload all possible layers to GPU | No |
| `--n-cpu-moe 40` | Expert offload: start high, lower until VRAM ~7–7.4GB | **YES** — primary tuning knob |
| `-c 65536` | Context window (64k) | Per-model in models.txt |
| `--cache-type-k q8_0 --cache-type-v q8_0` | Quantized KV cache (saves VRAM) | No |
| `-t 8` | Threads (8–12, NOT 24 — decode is bandwidth-bound) | Benchmark with `llama-bench` |
| `-ub 2048` | Micro-batch for faster prefill | Try 1024/2048 |
| `--mlock` | Prevent CPU experts from paging to swap | Keep on |
| `--parallel 1` | Single parallel slot (multiplies KV memory) | Keep 1 |

### Ollama env vars (if using Ollama)

```yaml
OLLAMA_FLASH_ATTENTION: "1"
OLLAMA_KV_CACHE_TYPE: q8_0
OLLAMA_NUM_PARALLEL: "1"
OLLAMA_CONTEXT_LENGTH: "65536"
OLLAMA_MAX_LOADED_MODELS: "1"
OLLAMA_KEEP_ALIVE: "-1"
```

---

## Monitoring

### Metrics sidecar (runs automatically)

- **Detailed JSON:** `curl -s http://metrics:19532/metrics`
- **Quick summary:** `curl -s http://metrics:19532/summary`
  - Example: `[2026-10-21 14:32:11] CPU load 2.1/4.3/5.8 | RAM 12.3/31.7 GB (39%) | GPU0 RTX 5070 util=87% VRAM 7.2/8.0 GB 68°C`

### Agent has hardware visibility

The `host-metrics` plugin registers `get_host_metrics` tool. The agent calls it to check VRAM before proposing large model ops, or to diagnose CPU fallback.

```bash
# Test from host (after ./lab <workspace> is running)
./scripts/verify-metrics.sh
```

---

## Repo map

| Path | What it is |
|---|---|
| `lab` | Launcher script. Validates paths, egress, runs compose. |
| `compose.yaml` | Services, networks, volumes, hardening flags. |
| `models.txt` | Model list with backend sections `[ollama]` / `[llamacpp]`. |
| `scripts/regen-configs` | Generates `opencode.json` + updates compose from `models.txt`. |
| `verify-lab.sh` | Regression tests for sandbox guarantees. |
| `agent/` | Agent container Dockerfile + opencode config. |
| `ollama/pull-models.sh` | Pulls Ollama models during setup. |
| `llamacpp/pull-models.sh` | Pulls GGUF from HuggingFace during setup. |
| `workspaces/` | Your projects live here (gitignored). |
| `docs/` | Journal, tickets, host setup, threat model. |
| `tests/` | Unit tests for `lab` (Bats). |
| `SECURITY.md` | Full security model, network isolation, capabilities. |

---

## How a session works

1. You run `./lab workspaces/demo`.
2. `lab` resolves path, runs safety checks, asks you to confirm the mount.
3. `lab` runs `docker compose` with `WORKSPACE`, `LAB_UID`, `LAB_GID`. Uses `sudo` (Docker daemon is root-owned).
4. Compose starts `ollama` + `llamacpp` (health-checked), then `agent` container: non-root, read-only fs, no caps, `/workspace` mounted rw, network gated by egress.
5. **opencode** sends prompts to `http://ollama:11434/v1` OR `http://llamacpp:11434/v1` (configurable per model).
6. Agent edits files in `/workspace`, runs commands in container, commits via git as `little-mester-agent`.
7. When you quit, container is removed (`--rm`), `lab` warns if it finds git hooks.
8. **You review on the host** with `git log` and `git diff`.

Understanding steps 5–7 *is* agentic coding: a loop where a model proposes actions, a program carries them out, and results go back to the model.

---

## Security model (brief)

Full version: [SECURITY.md](SECURITY.md).

| Channel | Control |
|---|---|
| Your files | Only one bind mount. Path guards in `lab`. No home dir, no Docker socket. |
| Credentials | None mounted or passed in. |
| Network | `internal: true` network. No proxy. |
| Privileges | Non-root, `cap_drop: ALL`, `no-new-privileges`, read-only root fs. |
| Resources | `mem_limit: 8g`, `pids_limit: 512` on agent. |
| Model server exposure | No published ports. |

### What this does NOT protect against

**Read this twice.**

- **The agent can delete or corrupt everything inside the mounted workspace, `.git` included.** Keep backups or push to a remote *before* sessions.
- **Traps that run on your host later.** Git hooks, `Makefile`, `package.json` scripts, `.envrc`, `.vscode/tasks.json`, shell scripts written by the agent run with *your* privileges if you execute them. **Read agent-written code before running it on the host.**
- **Container escape.** Containers share the host kernel. A kernel/runtime vuln could break out. Unlikely from a local model making mistakes, but real against deliberately hostile code. A VM is the next level up.
- **Docker group = root.** Anyone who can run `docker` can effectively become root on the host. `lab` uses `sudo` instead of adding you to the group.
- **Bad output.** A sandboxed agent can still write wrong or insecure code. Review it.
>>>>>>> main

---

## Design decisions and trade-offs

| Decision | Why | Cost |
|---|---|---|
| Dual backend (Ollama + llama.cpp) | Learning, comparison, best tool per model | More complex compose, two pull services |
| llama.cpp with `--n-cpu-moe` for MoE | Only way to run 35B-A3B on 8GB VRAM efficiently | Requires GGUF download from HF, not Ollama registry |
| Internet off at runtime | Closes "send data out" channel | Dependencies baked in image or in workspace |
| One-shot pull services on separate network | Keeps internet out of long-lived services | Extra services to understand |
| Docker over Podman | Most docs/tooling assume it | Daemon runs as root. Rootless Podman = later hardening |
| Agent commits as `little-mester-agent` | Easy to audit/revert agent changes | None |
| Q4_K_M quantization | Speed/quality sweet spot; Q6/Q8 slows decode ~2x | Slight quality loss vs Q8 |
| Context = 64k (not 196k) | 196k forces layers off GPU, kills prefill speed | Less context for huge files |
| `small_model = model` | Prevents eviction thrashing on title/summary calls | Slightly more VRAM for small tasks |

---

## Troubleshooting

| Symptom | Likely cause / Fix |
|---|---|
<<<<<<< HEAD
| `nvidia-smi` fails on host | Driver problem. RTX 50-series needs open kernel modules. Fix before anything else. |
| `docker run --gpus all ... nvidia-smi` fails | NVIDIA container toolkit not configured. Re-run `sudo nvidia-ctk runtime configure --runtime=docker` and restart docker. |
| `ollama ps` shows part CPU | Model plus context doesn't fit in VRAM. Lower `OLLAMA_CONTEXT_LENGTH` or use a smaller model/quantization. |
| Agent can't reach `ollama` | Ollama not healthy yet, or the services aren't on the same network. |
| Files created by the agent are owned by root | `LAB_UID`/`LAB_GID` weren't passed through. Run via `./lab`, not `docker compose` directly. |
| Agent (or Aider) tries to reach the internet and stalls | Expected with no network. It should fall back. Note how long it takes in your journal. |
=======
| `nvidia-smi` fails on host | Driver problem. RTX 50-series needs open kernel modules. Fix first. |
| `docker run --gpus all ... nvidia-smi` fails | NVIDIA container toolkit not configured. `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker` |
| `ollama ps` / llama.cpp shows CPU fallback | Model + context doesn't fit in VRAM. Lower context, smaller quantization, or smaller model. |
| Agent can't reach model server | Server not healthy yet, or services not on same network. Check `docker compose ps`. |
| Files created by agent owned by root | `LAB_UID`/`LAB_GID` not passed. Run via `./lab`, not `docker compose` directly. |
| Agent stalls trying to reach internet | Expected with no egress. It should fall back. Note latency in journal. |
| llama.cpp fails to start | Check GGUF file exists in `llamacpp-models` volume. Check `docker logs llamacpp`. |
| Out of memory (OOM) on host | Too many apps open. Close browsers. Check `docker stats`. |
| Throttling / low t/s | **Cooling!** Check temps in metrics. Fan/stand mandatory for sustained load. |
>>>>>>> main

---

## Learning path

Do these in order. Each has its own README with goals, exercises, and "you should be able to explain" questions.

<<<<<<< HEAD
1. Get the sandbox running and make `verify-sandbox.sh` pass. Read [SECURITY.md](SECURITY.md).
2. Use opencode (or `--agent aider`) on a small project. Review every diff. Journal what the model gets right and wrong.
3. Build your own agent loop: [my-agent/](my-agent/README.md) — *not yet created*.
4. Measure it: [evals/](evals/README.md) — *not yet created*.
5. Attack it: [red-team/](red-team/README.md) — *not yet created*.
6. Add controlled web access: [proxy/](proxy/README.md) — *not yet created*.
7. Add tool servers: [mcp/](mcp/README.md) — *not yet created*.
=======
1. Get sandbox running, make `verify-lab.sh` pass. Read [SECURITY.md](SECURITY.md).
2. Use opencode (or `--agent aider`) on a small project. Review every diff. Journal what the model gets right/wrong.
3. Build your own agent loop: `my-agent/` (planned).
4. Measure it: `evals/` (planned).
5. Attack it: `red-team/` (planned).
6. Add controlled web access: `proxy/` (planned).
7. Add tool servers: `mcp/` (planned).
>>>>>>> main

Keep `docs/journal.md` updated throughout. **What broke and why is the most valuable thing you'll produce here.**

---

## Glossary

- **Agent**: Program that loops: ask model what to do, do it, show model the result.
- **Tool call**: Structured request from model like "run `read_file` with path=x".
- **Context window**: How much text the model can consider at once. Costs VRAM.
- **Quantization**: Storing model weights at lower precision to fit in less memory.
- **Prompt injection**: Untrusted text (README, web page, issue) that tries to give the model instructions.
- **Bind mount**: Host folder made visible inside a container.
- **Capabilities**: Slices of root's power in Linux. Dropped to zero here.
- **MoE (Mixture of Experts)**: Model architecture where only a subset of parameters ("experts") are active per token.
- **Expert offload (`--n-cpu-moe`)**: llama.cpp feature to keep inactive experts in system RAM, only active experts on GPU.
- **GGUF**: Model file format used by llama.cpp (single file, contains all weights + metadata).
- **KV cache**: Key-Value cache storing attention state for each token. Grows with context length.