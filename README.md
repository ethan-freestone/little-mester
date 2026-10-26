# little-mester

A **locked-down, fully local sandbox for learning agentic coding.**

A local LLM (via **Ollama** and/or **llama.cpp**, on the GPU) drives a coding agent
(**opencode** by default, or **aider** via `--agent`) that can read and edit files in
**one folder you choose**, and nothing else. No internet by default. No access to your
home directory, keys, or other projects.

**IMPORTANT: You are always in a container.** The agent runs inside a Docker container.
The model server (Ollama or llama.cpp) runs in a separate container. Your workspace is
bind-mounted at `/workspace` inside the agent container. The host filesystem is NOT
accessible except for that one mount.

The goal of this repo is twofold:

1. Be a safe place to point an AI agent at code without worrying about your machine.
2. Be a **learning lab**: every piece is small enough to read, break, and rebuild.

> Status: personal learning project. Nothing here has been security-audited.
> Treat the sandbox as strong *guard rails*, not a guarantee. See
> [What this does NOT protect against](#what-this-does-not-protect-against).

---

## Read this first (humans and AI agents)

This repo has **two decision logs with different scopes**. Read both before making changes:

| File | Scope | Read before |
|---|---|---|
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | How to **use** little-mester: workflow, container rules, field semantics, testing constraints | Any session |
| [`docs/tickets/architecture/DECISIONS.md`](docs/tickets/architecture/DECISIONS.md) | **Engineering** designs for the ticket engine: schema, lifecycle, enforcement, routing | Any `little-mester-mcp/` change |

The default development cycle is **doc → test → code → commit**. See
[`docs/workflows.md`](docs/workflows.md). Do not skip stages or reorder them without a
ticket explaining why.

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

Agent internet is **enabled by default** (`[egress, internal]`). To disable (e.g. for
isolated sub-agents):

| Method | Compose override | Effect |
|---|---|---|
| Lab flag | `--isolated` | Sets agent networks to `[internal]` only — no outbound internet |
| Lab flag | `--egress-false` or `--egress false` | Same as `--isolated` |
| Direct compose | `-e NETWORKS_AGENT=internal` + omit egress from `networks.agent` | Same effect, more explicit but requires editing compose |

### Three ideas to hold onto

| Idea | How it's done here |
|---|---|
| The model can't touch files | Ollama/llama.cpp only turn text into text. They're in their own containers with no mounts except their model volumes. |
| The agent can only touch one folder | The agent container sees exactly one bind mount, `/workspace`, gated by `lab`'s path checks. |
| The agent can't phone home | By default the agent and model servers share an `internal: true` network. Egress is opt-in via `--egress`/`.mester-egress`. Only `ollama-pull` / `llamacpp-pull` have it during setup. |

---

## Dual backend: Ollama + llama.cpp

This system runs **both** backends simultaneously. You choose per model.

| Backend | Best for | Model format | How it works |
|---|---|---|---|
| **Ollama** | Small dense models that fit entirely in VRAM | Ollama format (auto-pulled) | Layer-wise GPU offload |
| **llama.cpp** | Large MoE models (35B-A3B, 30B-A3B, 20B) | GGUF (downloaded from HuggingFace) | **Expert-level offload** (`--n-cpu-moe`) |

### Why llama.cpp for MoE?

Your hardware: **Strix Point (HX 370)**, 12 cores, 16-CU Radeon 890M, **8 GB VRAM**,
~120 GB/s system RAM bandwidth.

- Ollama splits MoE by **layer** (not expert) → worst case for 35B-A3B on 8 GB
- llama.cpp supports `--n-cpu-moe` → **expert-level offload**:
  - **GPU**: attention, shared layers, KV cache, active experts (~3B params)
  - **CPU (RAM)**: remaining experts (~20 GB at Q4, paged from system RAM)
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

---

## Quick start (new machine)

**Requirements:** Arch/CachyOS (other distros: see `docs/01-host-setup.md`), working
NVIDIA driver, `sudo`.

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
./verify-lab.sh                 # re-run after any change to compose.yaml / Dockerfile
./lab pull                      # re-pull models after editing models.txt
./lab build                     # rebuild agent image after config changes
```

`<workspace>` can be a single repo, or a parent folder holding several repos. The
launcher refuses `/`, `$HOME`, anything you don't own, anything overlapping `~/.ssh`,
`~/.config`, and **by default** any path that would expose this lab repo itself (use
`--unsafe-recurse` to override).

### Workspace management

The `lab` script enforces strict path rules to ensure security.

- **Valid paths**: must be an absolute path (or relative to the current dir) pointing
  to a directory you own.
- **Invalid paths**: `/`, `$HOME`, anything overlapping `~/.ssh`, `~/.config`, or the
  `little-mester` repo itself.
- **Multi-repo workspaces**: you can point `./lab` at a parent folder containing
  multiple git repos. The agent will see all files in that tree.

**Tip**: create a dedicated folder for your experiments:

```bash
mkdir -p workspaces/my-experiment
cd workspaces/my-experiment
git init
```

### Self-modification (`--unsafe-recurse`)

When you want the agent to edit little-mester itself:

```bash
cd <path-to-little-mester-checkout>
./lab --unsafe-recurse    # allows opencode to read/write the current directory
```

You do not need to append `.` — it is implicit when using `--unsafe-recurse`.

This is off by design — it tells the agent "I know what I'm doing." Without
`--unsafe-recurse`, the guard always blocks self-mapping. This makes recursive
improvement possible **without making it the default**.

---

## Ticket engine (`little-mester-mcp`)

A lightweight, MCP-first ticketing system. Tickets are plain JSON files on disk
(`docs/tickets/ticket-NNN.json`) with a tiny rebuildable index (`_index.json`) — no
SQLite, no WASM, no migrations.

Two equivalent front-ends share one engine (`src/engine.ts`):

- **CLI** — `little-mester-mcp/scripts/ticket-engine` (source: `little-mester-mcp/src/cli.ts`)
- **MCP server** — tools named `mester_tickets_*` (source: `little-mester-mcp/src/mcp-server.ts`)

```bash
# Create, list, update, archive
./little-mester-mcp/scripts/ticket-engine create --area infra --type feature "Add CI pipeline" --priority major
./little-mester-mcp/scripts/ticket-engine list --area infra --status active
./little-mester-mcp/scripts/ticket-engine update 12 --status active --comment "Started work"
./little-mester-mcp/scripts/ticket-engine archive 12

# Inspect the audit trail and validate the store
./little-mester-mcp/scripts/ticket-engine audit
./little-mester-mcp/scripts/ticket-engine validate
```

**Structural enforcement (important):**

- All ticket writes **must** go through the engine (CLI or MCP tools). Never hand-edit
  `ticket-NNN.json`, `_index.json`, or `_audit.json`.
- A transient lock (`.engine.lock`) protects each write window; stale locks from dead
  processes are detected by PID and can be cleared safely.
- Every engine write appends one lean entry to `_audit.json` with a SHA-256 hash of the
  entry. Editing the audit log is detectable.
- Ticket files that exist on disk but have no audit entry are **orphans**. They are
  flagged for human review and **never auto-deleted** (fail-open). A human decides
  whether to adopt them (`audit --baseline`) or remove them.

The full rationale and invariants are in
[`docs/tickets/architecture/DECISIONS.md`](docs/tickets/architecture/DECISIONS.md) §I,
and usage is in [`little-mester-mcp/docs/README.md`](little-mester-mcp/docs/README.md)
and [`little-mester-mcp/docs/CLI.md`](little-mester-mcp/docs/CLI.md).

---

## Model management

### Adding / changing models

1. Edit `models.txt` (see format above)
2. Run `./lab pull` — this starts the appropriate pull service:
   - `ollama-pull` for `[ollama]` models (downloads from Ollama registry)
   - `llamacpp-pull` for `[llamacpp]` models (downloads GGUF from HuggingFace)

### Switching models at runtime

**In opencode:** Use `/model <provider>/<model>`, e.g.:

- `/model ollama/qwen3:8b`
- `/model llamacpp/qwen3-35b-a3b-q4_k_m`

**In Aider:** Pass `--model` when starting, or restart with a different model.

### VRAM monitoring

```bash
# Host-side
nvidia-smi -l 1

# Inside agent container (via metrics sidecar)
curl -s http://metrics:19532/summary
# Or use the get_host_metrics tool from opencode
```

If you see the model process using **system RAM instead of VRAM**, it has fallen back to
CPU. Reduce context, use a smaller quantization, or switch to a smaller model.

---

## Hardware & performance tuning

### Critical for sustained runs (laptop)

| Setting | Why |
|---|---|
| **Plugged in + performance profile** | Prevents throttling |
| **dGPU in discrete/MUX mode** | Prevents 5070 routing through iGPU |
| **Cooling stand / external fan** | **Throttling costs more than any flag** |
| **Close browsers, Electron apps** | Frees system RAM for CPU-side experts |
| **Don't set `mem_limit` on model container** | Agent's 8 GB limit is fine; model container needs flexibility |

### llama.cpp flags (in `compose.yaml`)

| Flag | Purpose | Tune? |
|---|---|---|
| `-ngl 99` | Offload all possible layers to GPU | No |
| `--n-cpu-moe 40` | Expert offload: start high, lower until VRAM ~7–7.4 GB | **YES** — primary tuning knob |
| `-c 65536` | Context window (64k) | Per-model in `models.txt` |
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

A lightweight sidecar exposes live GPU, CPU, RAM, and temperature data from inside the
container, so the agent has real hardware visibility without breaking the sandbox.

- **Detailed JSON**: `curl -s http://metrics:19532/metrics`
- **Quick summary**: `curl -s http://metrics:19532/summary`
  - Example: `[2026-10-21 14:32:11] CPU load 2.1/4.3/5.8 | RAM 12.3/31.7 GB (39%) | GPU0 RTX 5070 util=87% VRAM 7.2/8.0 GB 68°C`
- **History**: `curl -s 'http://metrics:19532/history?n=60'` → last N samples from a
  500 ms sampler ring buffer, used by the TUI sparkline.
- The service publishes **no host port** — `metrics` is a Docker DNS name on the
  `internal` network.

> **Agent query tool**: `get_host_metrics` is registered by the
> [`host-metrics` plugin](agent/opencode/plugins/host-metrics/). It returns a formatted
> report the agent can call before large model operations.
>
> **Live TUI view**: [`host-metrics-tui`](agent/opencode/plugins/host-metrics-tui/)
> renders a compact GPU/VRAM sparkline in the sidebar footer, expandable for detail.
>
> **Acceptance verification**: run `./scripts/verify-metrics.sh` on the host.

### Monitoring the agent

```bash
docker logs -f little-mester-agent-1     # agent output and errors
docker stats little-mester-agent-1       # CPU/RAM usage
./lab shell workspaces/my-experiment     # interactive shell inside the sandbox
```

---

## Repo map

| Path | What it is | Read this |
|---|---|---|
| `lab` | Launcher script — validates workspace path, runs compose | [docs/workflows.md](docs/workflows.md) |
| `compose.yaml` | Services, networks, volumes, hardening flags | — |
| `models.txt` | Model list with `[ollama]` / `[llamacpp]` sections | — |
| `scripts/regen-configs` | Generates `opencode.json` + updates compose from `models.txt` | — |
| `agent/` | Agent container Dockerfile + opencode config + plugins | [agent/README.md](agent/README.md) |
| `ollama/pull-models.sh` | Pulls Ollama models during setup | — |
| `llamacpp/pull-models.sh` | Pulls GGUF from HuggingFace during setup | — |
| `verify-lab.sh` | Regression tests for sandbox guarantees | [Testing](#testing) |
| `workspaces/` | Your projects live here (gitignored) | — |
| `docs/` | Journal, tickets, workflows, decisions | — |
| `docs/tickets/` | Ticket JSON files + index + audit log | [DECISIONS](docs/tickets/architecture/DECISIONS.md) |
| `little-mester-mcp/` | Ticket engine (CLI + MCP server) | [little-mester-mcp/docs/README.md](little-mester-mcp/docs/README.md) |
| `scripts/` | Automation tools — tickets, hooks, sync | [scripts/README.md](scripts/README.md) |
| `tests/` | Bats tests for `lab` and the TUI JSX guard | [Testing](#testing) |
| `SECURITY.md` | Full security model, network isolation, capabilities | [Security](#security-model-brief) |

---

## How a session works

1. You run `./lab workspaces/demo`.
2. `lab` resolves the path, runs safety checks, asks you to confirm the mount.
3. `lab` runs `docker compose` with `WORKSPACE`, `LAB_UID`, `LAB_GID`. Uses `sudo`
   (the Docker daemon is root-owned).
4. Compose starts `ollama` + `llamacpp` (health-checked), then the `agent` container:
   non-root, read-only fs, no caps, `/workspace` mounted rw, network gated by egress.
5. **opencode** sends prompts to `http://ollama:11434/v1` OR
   `http://llamacpp:11434/v1` (configurable per model).
6. The agent edits files in `/workspace`, runs commands in the container, and commits
   via git as `little-mester-agent`.
7. When you quit, the container is removed (`--rm`), and `lab` warns if it finds git hooks.
8. **You review on the host** with `git log` and `git diff`.

Understanding steps 5–7 *is* agentic coding: a loop where a model proposes actions, a
program carries them out, and the results go back to the model.

### Agentic workflow tips

- **Context window**: the model has a limited context window. If your workspace is huge,
  the agent may forget earlier instructions. Stage specific files with `git add` to focus it.
- **Git hooks**: the agent writes code that runs on your host if you execute hooks (like
  `Makefile` or `.envrc`). **Always review** these before running them.
- **Iterative prompts**: break complex tasks into small steps. "Refactor the login
  module" is better than "Fix the app".

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

- **The agent can delete or corrupt everything inside the mounted workspace, `.git`
  included.** Keep backups or push to a remote *before* sessions.
- **Traps that run on your host later.** Git hooks, `Makefile`, `package.json` scripts,
  `.envrc`, `.vscode/tasks.json`, shell scripts written by the agent run with *your*
  privileges if you execute them. **Read agent-written code before running it on the host.**
- **Container escape.** Containers share the host kernel. A kernel/runtime vuln could
  break out. Unlikely from a local model making mistakes, but real against deliberately
  hostile code. A VM is the next level up.
- **Docker group = root.** Anyone who can run `docker` can effectively become root on the
  host. `lab` uses `sudo` instead of adding you to the group.
- **Bad output.** A sandboxed agent can still write wrong or insecure code. Review it.

---

## Design decisions and trade-offs

| Decision | Why | Cost |
|---|---|---|
| Dual backend (Ollama + llama.cpp) | Learning, comparison, best tool per model | More complex compose, two pull services |
| llama.cpp with `--n-cpu-moe` for MoE | Only way to run 35B-A3B on 8 GB VRAM efficiently | Requires GGUF download from HF, not Ollama registry |
| Internet off at runtime | Closes "send data out" channel | Dependencies baked in image or in workspace |
| One-shot pull services on separate network | Keeps internet out of long-lived services | Extra services to understand |
| Docker over Podman | Most docs/tooling assume it | Daemon runs as root. Rootless Podman = later hardening |
| Agent commits as `little-mester-agent` | Easy to audit/revert agent changes | None |
| Q4_K_M quantization | Speed/quality sweet spot; Q6/Q8 slows decode ~2x | Slight quality loss vs Q8 |
| Context = 64k (not 196k) | 196k forces layers off GPU, kills prefill speed | Less context for huge files |
| `small_model = model` | Prevents eviction thrashing on title/summary calls | Slightly more VRAM for small tasks |
| JSON tickets + rebuildable index | No SQLite/migrations; files are the source of truth | Index must be kept in sync (validate catches drift) |
| Engine-only writes + audit trail | Makes bypasses visible and reviewable | Slightly more machinery per write |

---

## Testing

> **Testing context:** run tests from a working checkout, not the main repo.
>
> **Shell compatibility:** commands run identically in bash and fish. Use `;` as a
> separator (works in both); fish users should use `$status` not `$?`.

This project uses [Bats](https://bats-core.readthedocs.io/) for unit-testing the `lab`
launcher, and Jest for the ticket engine.

### Prerequisites

```bash
# Arch/CachyOS
sudo pacman -S bats

# macOS
brew install bats-core
```

### Running the sandbox tests (host)

```fish
bats tests/
```

Regression checks include: valid workspace path acceptance, rejection of sensitive paths
(`$HOME`, `$HOME/.ssh`, …), ownership verification, git-hook warning detection, and the
TUI JSX rules.

### Ticket engine tests

```fish
cd little-mester-mcp && npm test
```

### Agent tests (inside the sandbox)

1. Ensure your workspace is set up (e.g. `workspaces/demo`).
2. Start a session: `./lab workspaces/demo`
3. Ask the agent to run the test suite.
4. It executes `./run-tests.sh`, which safely invokes `bats` inside the container via
   `./lab exec`.

---

## Troubleshooting

| Symptom | Likely cause / Fix |
|---|---|
| `nvidia-smi` fails on host | Driver problem. RTX 50-series needs open kernel modules. Fix first. |
| `docker run --gpus all ... nvidia-smi` fails | NVIDIA container toolkit not configured. `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker` |
| `ollama ps` / llama.cpp shows CPU fallback | Model + context doesn't fit in VRAM. Lower context, smaller quantization, or smaller model. |
| Agent can't reach model server | Server not healthy yet, or services not on same network. Check `docker compose ps`. |
| Files created by agent owned by root | `LAB_UID`/`LAB_GID` not passed. Run via `./lab`, not `docker compose` directly. |
| Agent stalls trying to reach internet | Expected with no egress. It should fall back. Note latency in journal. |
| llama.cpp fails to start | Check the GGUF file exists in the `llamacpp-models` volume. Check `docker logs llamacpp`. |
| Out of memory (OOM) on host | Too many apps open. Close browsers. Check `docker stats`. |
| Throttling / low t/s | **Cooling!** Check temps in metrics. Fan/stand mandatory for sustained load. |
| `validate` reports orphan ticket files | A ticket was written outside the engine. Review it, then `audit --baseline` to adopt or remove it deliberately. Never delete blindly. |
| `validate` reports a stale lock | A previous engine process crashed. Remove `docs/tickets/.engine.lock` (check the PID first). |

---

## Learning path

Do these in order. Each has its own README with goals, exercises, and "you should be able
to explain" questions.

1. Get the sandbox running and make `./verify-lab.sh` pass. Read [SECURITY.md](SECURITY.md).
2. Use opencode (or `--agent aider`) on a small project. Review every diff. Journal what
   the model gets right and wrong.
3. Build your own agent loop: `my-agent/` (planned).
4. Measure it: `evals/` (planned).
5. Attack it: `red-team/` (planned).
6. Add controlled web access: `proxy/` (planned).
7. Add tool servers: `mcp/` (planned).

Keep `docs/journal.md` updated throughout. **What broke and why is the most valuable
thing you'll produce here.**

---

## Glossary

- **Agent**: Program that loops: ask the model what to do, do it, show the model the result.
- **Tool call**: Structured request from the model like "run `read_file` with path=x".
- **Context window**: How much text the model can consider at once. Costs VRAM.
- **Quantization**: Storing model weights at lower precision to fit in less memory.
- **Prompt injection**: Untrusted text (README, web page, issue) that tries to give the model instructions.
- **Bind mount**: Host folder made visible inside a container.
- **Capabilities**: Slices of root's power in Linux. Dropped to zero here.
- **MoE (Mixture of Experts)**: Architecture where only a subset of parameters ("experts") are active per token.
- **Expert offload (`--n-cpu-moe`)**: llama.cpp feature to keep inactive experts in system RAM, only active experts on GPU.
- **GGUF**: Model file format used by llama.cpp (single file, contains all weights + metadata).
- **KV cache**: Key-Value cache storing attention state for each token. Grows with context length.
- **Orphan ticket**: A `ticket-NNN.json` on disk with no entry in the engine's audit log. Flagged for human review; never auto-deleted.
- **Baseline**: A one-time audit entry that adopts pre-existing tickets into the audit trail when enforcement is first enabled.
