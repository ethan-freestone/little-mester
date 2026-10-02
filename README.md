# little-mester

A **locked-down, fully local sandbox for learning agentic coding.**

A local LLM (via Ollama, on the GPU) drives a coding agent (Aider to start with) that can
read and edit files in **one folder you choose**, and nothing else. No internet by default.
No access to your home directory, keys, or other projects.

The goal of this repo is twofold:

1. Be a safe place to point an AI agent at code without worrying about your machine.
2. Be a **learning lab**: every piece is small enough to read, break, and rebuild.

> Status: personal learning project. Nothing here has been security-audited.
> Treat the sandbox as strong *guard rails*, not a guarantee. See [What this does NOT protect against](#what-this-does-not-protect-against).

---

## Build status (tick these off as you go)

- [ ] 0. Host NVIDIA driver works (`nvidia-smi`)
- [ ] 1. Repo skeleton, `.gitignore`, `models.txt`
- [ ] 2. `compose.yaml`
- [ ] 3. `agent/Dockerfile`, `ollama/pull-models.sh`
- [ ] 4. `lab` launcher with path guard rails
- [ ] 5. `./lab setup` completes (packages, image build, models pulled)
- [ ] 6. `./verify-sandbox.sh` all green
- [ ] 7. First Aider session on a throwaway repo
- [ ] 8. Multi-repo workspace tested
- [ ] 9. Own agent loop (`my-agent/`)
- [ ] 10. Evals (`evals/`)
- [ ] 11. Red-team the sandbox (`red-team/`)
- [ ] 12. Opt-in web access via proxy (`proxy/`)
- [ ] 13. MCP (`mcp/`)

---

## Architecture

```
 HOST (CachyOS)
 ┌──────────────────────────────────────────────────────────────────────┐
 │                                                                      │
 │  ./lab ──► docker compose                                            │
 │                                                                      │
 │   ┌──────────── network: internal (no route to internet) ──────────┐ │
 │   │                                                                │ │
 │   │  ┌───────────────┐   http://ollama:11434   ┌────────────────┐  │ │
 │   │  │ agent         │ ──────────────────────► │ ollama         │  │ │
 │   │  │ (Aider, git,  │                         │ (model server, │  │ │
 │   │  │  python)      │                         │  uses the GPU) │  │ │
 │   │  │ non-root      │                         │ no ports       │  │ │
 │   │  │ read-only fs  │                         │ published      │  │ │
 │   │  │ no caps       │                         └───────┬────────┘  │ │
 │   │  └──────┬────────┘                                 │           │ │
 │   └─────────┼──────────────────────────────────────────┼───────────┘ │
 │             │ bind mount (read-write)                  │ named volume│
 │             ▼                                          ▼             │
 │   ~/…/little-mester/workspaces/<project>        ollama-models        │
 │   (the ONLY host folder the agent sees)         (model weights)      │
 │                                                                      │
 │   setup only:  ollama-pull ──► network: egress ──► internet          │
 └──────────────────────────────────────────────────────────────────────┘
```

Three ideas to hold onto:

| Idea | How it's done here |
|---|---|
| The model can't touch files | Ollama only turns text into text. It's in its own container with no mounts except its model volume. |
| The agent can only touch one folder | The agent container sees exactly one bind mount, `/workspace`. |
| The agent can't phone home | The agent and Ollama share an `internal: true` network. Only the one-shot `ollama-pull` container ever has internet, and only during setup. |

---

## Comprehensive Usage Guide

This section provides a deep dive into managing your sandbox, models, and the agent workflow.

### 1. Workspace Management
The `lab` script enforces strict path rules to ensure security.

*   **Valid Paths**: Must be an absolute path (or relative to current dir) pointing to a directory you own.
*   **Invalid Paths**: `/`, `$HOME`, anything overlapping `~/.ssh`, `~/.config`, or the `little-mester` repo itself.
*   **Multi-Repo Workspaces**: You can point `./lab` at a parent folder containing multiple git repos. The agent will see all files in that tree.

**Tip**: Create a dedicated folder for your experiments:
```bash
mkdir -p workspaces/my-experiment
cd workspaces/my-experiment
git init # Initialize your project here
```

### 2. Model Management (Adding & Switching)
Models are managed via `models.txt` and the `ollama` service.

#### Adding a Model
1.  Open `models.txt`.
2.  Add the model name (e.g., `llama3.2:latest`, `mistral`).
3.  Run `./lab setup`. This triggers the `ollama-pull` container to download the new model over the internet.

#### Switching Models
By default, Ollama uses the first model listed in `models.txt` or the one you last ran. To switch:
1.  Ensure the target model is downloaded (see above).
2.  When starting a session, you can specify the model if your version of Aider supports it, or simply rely on Ollama's default behavior.
3.  **VRAM Check**: If the agent stalls or falls back to CPU, check `nvidia-smi`. If VRAM is full, switch to a smaller quantization (e.g., `q4_K_M`) or a smaller model (e.g., `phi3` instead of `llama3`).

#### Monitoring Models
*   **List Models**: Run `docker exec ollama ollama list` inside the container.
*   **Check VRAM**: On your host, run `nvidia-smi`. Look for the `ollama` process. If it's using CPU (high memory usage in RAM instead of VRAM), your model is too large for your GPU.

### 3. Monitoring the Agent (`little-mester`)
Since the agent runs inside a container, you need to monitor it from the host.

*   **Container Logs**: View the agent's output and errors:
    ```bash
    docker logs -f little-mester-agent-1
    ```
*   **Resource Usage**: Monitor CPU/RAM of the agent container:
    ```bash
    docker stats little-mester-agent-1
    ```
*   **Interactive Shell**: If the agent hangs or you need to debug file permissions:
    ```bash
    ./lab shell workspaces/my-experiment
    # You are now inside the container as a non-root user
    ls -la /workspace
    exit
    ```

### 4. Agentic Workflow Tips
*   **Context Window**: The model has a limited context window. If your workspace is huge, the agent might forget earlier instructions. Use `git add` to stage specific files you want it to focus on.
*   **Git Hooks**: The agent writes code that runs on your host if you execute hooks (like `Makefile` or `.envrc`). **Always review** these before running them.
*   **Iterative Prompts**: Break complex tasks into small steps. "Refactor the login module" is better than "Fix the app".

---

## Quick start (new machine)

Requirements: Arch/CachyOS (other distros: see [docs/01-host-setup.md](docs/01-host-setup.md)),
a working NVIDIA driver, `sudo`.

```fish
git clone <this-repo> little-mester
cd little-mester
./lab setup                       # installs docker + GPU toolkit, builds image, pulls models
./verify-sandbox.sh               # proves the sandbox properties hold
mkdir -p workspaces/demo          # make or clone a project here
./lab workspaces/demo             # start the agent on that folder
```

Models to download are listed in [`models.txt`](models.txt). Edit it, then re-run
`./lab setup`.

## Daily use

```fish
./lab <path-to-workspace>           # start Aider on that folder
./lab <path-to-workspace> --help    # extra args are passed through to aider
./lab shell <path-to-workspace>     # plain bash inside the sandbox (to poke around)
./lab stop                          # stop the Ollama container
./verify-sandbox.sh                 # re-run after ANY change to compose.yaml or the Dockerfile
```

`<path-to-workspace>` can be a single repo, or a parent folder holding several repos.
The launcher refuses `/`, `$HOME`, anything you don't own, anything overlapping
`~/.ssh`, `~/.config` and similar, and anything that would expose this lab repo itself.

---

## Repo map

| Path | What it is | Read this |
|---|---|---|
| `lab` | Launcher script. Validates the workspace path, then runs compose. | [docs/usage-guide.md](docs/usage-guide.md) |
| `compose.yaml` | Defines the services, networks, volumes and hardening flags. | [agent/README.md](agent/README.md) |
| `models.txt` | List of models to download during setup. | [ollama/README.md](ollama/README.md) |
| `verify-sandbox.sh` | Regression tests for the sandbox properties. | [docs/threat-model.md](docs/threat-model.md) |
| `agent/` | Dockerfile for the container the agent runs in. | [agent/README.md](agent/README.md) |
| `ollama/` | Model-pulling script and notes on running models on 8 GB VRAM. | [ollama/README.md](ollama/README.md) |
| `workspaces/` | Where the projects the agent works on live (gitignored). | [workspaces/README.md](workspaces/README.md) |
| `docs/` | Journal, host setup notes, threat model. | [docs/README.md](docs/README.md) |
| `my-agent/` | *Planned.* Build your own agent loop from scratch. | [my-agent/README.md](my-agent/README.md) |
| `evals/` | *Planned.* Pass/fail tasks to compare models and prompts. | [evals/README.md](evals/README.md) |
| `red-team/` | *Planned.* Attacks against your own sandbox. | [red-team/README.md](red-team/README.md) |
| `proxy/` | *Planned.* Opt-in, allowlisted web access. | [proxy/README.md](proxy/README.md) |
| `mcp/` | *Planned.* Tool servers for your agent. | [mcp/README.md](mcp/README.md) |

---

## How a session works

1. You run `./lab workspaces/demo`.
2. `lab` resolves the path, runs the safety checks, and asks you to confirm the mount.
3. `lab` runs `docker compose` with `WORKSPACE`, `LAB_UID` and `LAB_GID` set. It uses `sudo`
   because the docker daemon is root-owned.
4. Compose starts `ollama` if needed and waits for its healthcheck.
5. Compose starts the `agent` container: non-root, read-only filesystem, zero Linux
   capabilities, `/workspace` mounted read-write.
6. Aider sends your request and the files you've added to `http://ollama:11434`.
7. Ollama runs the model on the GPU and returns text.
8. Aider edits files in `/workspace` and commits to git, as the author `little-mester-agent`.
9. When you quit, the container is removed (`--rm`) and `lab` warns if it finds git hooks.
10. **You review on the host** with `git log` and `git diff`.

Understanding steps 6 to 8 *is* agentic coding: a loop where a model proposes actions,
a program carries them out, and the results go back to the model. Aider hides the loop;
[my-agent/](my-agent/README.md) is where you build it yourself.

---

## Security model, briefly

Full version: [docs/threat-model.md](docs/threat-model.md).

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
| Small 7B model | Fits in 8 GB VRAM | Weaker tool calling. You will see it fail, which is educational |

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `nvidia-smi` fails on host | Driver problem. RTX 50-series needs the open kernel modules. Fix before anything else. |
| `docker run --gpus all ... nvidia-smi` fails | NVIDIA container toolkit not configured. Re-run `sudo nvidia-ctk runtime configure --runtime=docker` and restart docker. |
| `ollama ps` shows part CPU | Model plus context doesn't fit in VRAM. Lower `OLLAMA_CONTEXT_LENGTH` or use a smaller model/quantization. |
| Agent can't reach `ollama` | Ollama not healthy yet, or the services aren't on the same network. |
| Files created by the agent are owned by root | `LAB_UID`/`LAB_GID` weren't passed through. Run via `./lab`, not `docker compose` directly. |
| Aider tries to reach the internet and stalls | Expected with no network. It should fall back. Note how long it takes in your journal. |

---

## Learning path

Do these in order. Each has its own README with goals, exercises and "you should be able to explain" questions.

1. Get the sandbox running and make `verify-sandbox.sh` pass. Read [docs/threat-model.md](docs/threat-model.md).
2. Use Aider on a small project. Review every diff. Journal what the model gets right and wrong.
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
