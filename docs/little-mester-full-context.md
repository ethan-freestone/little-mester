# Little Mester: Complete Project Context for Self-Modification
> **Instructions for External LLM (Claude/Gemini)**: 
> This document contains the complete source code, configuration, and operational workflows of the "Little Mester" agent. Use this context to analyze, suggest improvements, or generate code changes for Little Mester itself. When proposing changes, always consider the existing skills, testing workflows, and architecture defined below.

## 1. Operational Workflows & Skills
### testing_workflow.md
# Skill: Testing & Regression Prevention Workflow

## Objective
Ensure code quality by enforcing a strict testing lifecycle (TDD) and regression prevention before any code changes are committed. This skill ensures Little Mester analyzes existing tests, writes new ones up-front, and validates the entire suite.

## Trigger
This skill is active during all development tasks involving code modification, feature addition, or refactoring.

## Workflow Steps

### 1. Pre-Change Analysis
- **Identify Scope**: Determine which files/modules are affected by the requested change.
- **Audit Existing Tests**: Check for existing unit and integration tests in the target area.
- **Gap Analysis**: Identify missing coverage for the proposed changes. Explicitly state what is currently tested vs. what needs testing.

### 2. Test Strategy Selection (Workflow Choice)
Before writing code, Little Mester must select the appropriate testing strategy based on the task context:
- **Strategy A: Strict TDD (Recommended)**
  - Write failing unit tests first.
  - Write failing integration tests if the feature touches external systems or APIs.
  - Run tests to confirm failure.
- **Strategy B: Regression Focus**
  - Identify high-risk areas for regression in existing code.
  - Write specific regression tests for those areas.
  - Proceed with implementation.

### 3. Implementation (TDD Cycle)
1. **Write Tests**: Create unit and integration tests that define the expected behavior.
   - *Constraint*: Must include both Unit (logic) and Integration (system/API) tests where applicable.
2. **Run & Fail**: Execute the test suite to ensure new tests fail as expected.
3. **Implement Code**: Write the minimum code required to pass the tests.
4. **Run & Pass**: Execute the full test suite (unit + integration) to ensure success.

### 4. Regression Check
- Run the *entire* relevant test suite, not just new tests.
- Verify no existing functionality is broken.
- If regressions are found, fix them immediately before committing.

### 5. Commitment
- Commit the code and the associated tests together.
- Ensure commit message references the tests added/modified.

## Decision Matrix: Unit vs Integration
| Feature Type | Required Tests |
| :--- | :--- |
| Pure Logic / Algorithms | Unit Tests |
| API Endpoints | Integration Tests + Unit Tests (for logic) |
| Database Interactions | Integration Tests (with mock or test DB) |
| UI Components | Integration Tests (E2E) + Unit Tests (logic) |

## Execution Rules
- **NO** code changes are committed without passing tests.
- **NO** feature is considered "done" until regression checks pass.
- Always prioritize **Integration Tests** for system stability and **Unit Tests** for logic correctness.
- If a test cannot be written, explicitly state why and propose a workaround or manual verification step.

## 2. Project Structure & Configuration
## File: `README.md`
```
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
```

## File: `codebase_context.md`
```
## File: `.gitignore`
```
workspaces/*
!workspaces/.gitkeep```

## File: `.idea/.gitignore`
```
# Default ignored files
/shelf/
/workspace.xml
# Editor-based HTTP Client requests
/httpRequests/
# Ignored default folder with query files
/queries/
# Datasource local storage ignored files
/dataSources/
/dataSources.local.xml
```

## File: `.idea/little-mester.iml`
```
<?xml version="1.0" encoding="UTF-8"?>
<module type="JAVA_MODULE" version="4">
  <component name="NewModuleRootManager" inherit-compiler-output="true">
    <exclude-output />
    <content url="file://$MODULE_DIR$" />
    <orderEntry type="inheritedJdk" />
    <orderEntry type="sourceFolder" forTests="false" />
  </component>
</module>```

## File: `.idea/misc.xml`
```
<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="ProjectRootManager" version="2" languageLevel="JDK_21" default="true" project-jdk-name="21" project-jdk-type="JavaSDK">
    <output url="file://$PROJECT_DIR$/out" />
  </component>
</project>```

## File: `.idea/modules.xml`
```
<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="ProjectModuleManager">
    <modules>
      <module fileurl="file://$PROJECT_DIR$/.idea/little-mester.iml" filepath="$PROJECT_DIR$/.idea/little-mester.iml" />
    </modules>
  </component>
</project>```

## File: `.idea/runConfigurations/Collate.xml`
```
<component name="ProjectRunConfigurationManager">
  <configuration default="false" name="Collate" type="ShConfigurationType">
    <option name="SCRIPT_TEXT" value="" />
    <option name="INDEPENDENT_SCRIPT_PATH" value="true" />
    <option name="SCRIPT_PATH" value="$PROJECT_DIR$/collate.sh" />
    <option name="SCRIPT_OPTIONS" value="" />
    <option name="INDEPENDENT_SCRIPT_WORKING_DIRECTORY" value="true" />
    <option name="SCRIPT_WORKING_DIRECTORY" value="$PROJECT_DIR$" />
    <option name="INDEPENDENT_INTERPRETER_PATH" value="true" />
    <option name="INTERPRETER_PATH" value="/usr/bin/bash" />
    <option name="INTERPRETER_OPTIONS" value="" />
    <option name="EXECUTE_IN_TERMINAL" value="true" />
    <option name="EXECUTE_SCRIPT_FILE" value="true" />
    <envs />
    <method v="2" />
  </configuration>
</component>```

## File: `.idea/vcs.xml`
```
<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="VcsDirectoryMappings">
    <mapping directory="" vcs="Git" />
    <mapping directory="$PROJECT_DIR$" vcs="Git" />
  </component>
</project>```

## File: `LICENSE`
```
MIT License

Copyright (c) 2026 Ethan Freestone

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## File: `README.md`
```
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
| `lab` | Launcher script. Validates the workspace path, then runs compose. | this file |
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
- **Capabilities**: slices of root's power in Linux. Dropped to zero here.```

## File: `agent/Dockerfile`
```
FROM python:3.12-slim

ARG LAB_UID=1000
ARG LAB_GID=1000

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ripgrep ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir aider-chat pytest

RUN groupadd -g ${LAB_GID} agent && useradd -m -u ${LAB_UID} -g ${LAB_GID} agent

ENV PATH="/opt/venv/bin:$PATH" HOME=/home/agent
WORKDIR /workspace
CMD ["aider"]```

## File: `collate.sh`
```
#!/usr/bin/env bash
set -euo pipefail

OUTPUT="codebase_context.md"

# Empty the file if it exists so we don't append to an older run
> "$OUTPUT"

echo "Collating codebase into $OUTPUT..."

# git ls-files ensures we only get tracked files, ignoring junk
git ls-files | while read -r file; do
  # Skip the output file itself just in case it gets tracked
  if [[ "$file" == "$OUTPUT" ]]; then continue; fi

  # Append filename and location as a markdown header
  echo "## File: \`$file\`" >> "$OUTPUT"

  # Append file content wrapped in markdown code blocks
  # You can dynamically grab the extension for syntax highlighting if needed,
  # but standard backticks work perfectly for LLM context.
  echo '```' >> "$OUTPUT"
  cat "$file" >> "$OUTPUT"
  echo '```' >> "$OUTPUT"
  echo "" >> "$OUTPUT"
done

echo "Done! Context saved to $OUTPUT"```

## File: `compose.yaml`
```
name: little-mester

services:
  ollama:
    image: ollama/ollama:latest
    volumes:
      - ollama-models:/root/.ollama
    environment:
      OLLAMA_FLASH_ATTENTION: "1"
      OLLAMA_KV_CACHE_TYPE: q8_0
      OLLAMA_CONTEXT_LENGTH: "16384"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
    security_opt: ["no-new-privileges:true"]
    networks: [internal]            # no internet, no published ports
    healthcheck:
      test: ["CMD", "ollama", "list"]
      interval: 5s
      timeout: 5s
      retries: 30

  # One-shot: the ONLY service with internet. Used by `./lab setup`.
  ollama-pull:
    image: ollama/ollama:latest
    profiles: [setup]
    entrypoint: ["/bin/sh", "/pull-models.sh"]
    volumes:
      - ollama-models:/root/.ollama
      - ./ollama/pull-models.sh:/pull-models.sh:ro
      - ./models.txt:/models.txt:ro
    networks: [egress]

  agent:
    build:
      context: ./agent
      args:
        LAB_UID: "${LAB_UID:-1000}"
        LAB_GID: "${LAB_GID:-1000}"
    profiles: [agent]
    depends_on:
      ollama:
        condition: service_healthy
    user: "${LAB_UID:-1000}:${LAB_GID:-1000}"
    working_dir: /workspace
    volumes:
      - "${WORKSPACE:?run via ./lab}:/workspace:${MOUNT_MODE:-rw}"
    read_only: true
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    tmpfs:
      - /tmp
      - /home/agent:uid=${LAB_UID:-1000},gid=${LAB_GID:-1000},mode=0700
    mem_limit: 4g
    pids_limit: 512
    environment:
      HOME: /home/agent
      OLLAMA_API_BASE: http://ollama:11434
      # Swap out AIDER models
      AIDER_MODEL: ollama_chat/fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:Q4
      #AIDER_MODEL: ollama_chat/qwen2.5-coder:7b
      AIDER_CHECK_UPDATE: "false"
      AIDER_ANALYTICS_DISABLE: "true"
      GIT_AUTHOR_NAME: little-mester-agent
      GIT_AUTHOR_EMAIL: agent@localhost
      GIT_COMMITTER_NAME: little-mester-agent
      GIT_COMMITTER_EMAIL: agent@localhost
      GIT_CONFIG_COUNT: "1"
      GIT_CONFIG_KEY_0: safe.directory
      GIT_CONFIG_VALUE_0: "*"
    networks: [internal]            # no internet
    stdin_open: true
    tty: true

networks:
  internal:
    internal: true
  egress: {}

volumes:
  ollama-models:```

## File: `docs/journal.md`
```
```

## File: `lab`
```
#!/usr/bin/env bash
set -euo pipefail

LAB_DIR="$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")" && pwd)"
LAB_UID="$(id -u)"
LAB_GID="$(id -g)"
WORKSPACE=""
MOUNT_MODE="rw"  # Default mount mode

die() { echo "REFUSED: $*" >&2; exit 1; }

dc() {
  sudo env "WORKSPACE=${WORKSPACE:-$LAB_DIR}" "LAB_UID=$LAB_UID" "LAB_GID=$LAB_GID" "MOUNT_MODE=$MOUNT_MODE" \
    docker compose -f "$LAB_DIR/compose.yaml" --project-directory "$LAB_DIR" "$@"
}

check_workspace() {
  local ws="$1"
  [[ -d "$ws" ]] || die "Not a directory: $ws"
  ws="$(realpath "$ws")"

  # System & Root folder protections
  [[ "$ws" != "/" ]] || die "Cannot mount filesystem root (/)"
  [[ "$ws" != "$HOME" ]] || die "Cannot mount entire home directory ($HOME)"

  # Ensure target is owned by current user
  [[ -O "$ws" ]] || die "Workspace directory must be owned by you (UID $LAB_UID)"

  # Prevent exposing the lab repo itself
  if [[ "$ws" == "$LAB_DIR" || "$LAB_DIR" == "$ws"/* ]]; then
    die "Workspace path would expose the little-mester repo itself"
  fi

  # Blacklist sensitive credential/config paths
  local sensitive_paths=(
    "$HOME/.ssh"
    "$HOME/.gnupg"
    "$HOME/.config"
    "$HOME/.aws"
    "$HOME/.kube"
    "$HOME/.local"
    "$HOME/.bashrc"
    "$HOME/.zshrc"
  )

  for s in "${sensitive_paths[@]}"; do
    if [[ "$ws" == "$s" || "$ws" == "$s"/* || "$s" == "$ws"/* ]]; then
      die "Path overlaps with sensitive system/credential directory: $s"
    fi
  done

  WORKSPACE="$ws"
}

confirm() {
  [[ "${LAB_YES:-}" == "1" ]] && return 0
  echo "--------------------------------------------------------"
  echo " Mounting path : $WORKSPACE"
  echo " Mount mode    : READ-${MOUNT_MODE^^}"
  echo "--------------------------------------------------------"
  read -r -p "Continue? [y/N] " a
  [[ "$a" == "y" || "$a" == "Y" ]] || exit 1
}

warn_hooks() {
  local h
  h="$(find "$WORKSPACE" -path '*/.git/hooks/*' -type f ! -name '*.sample' 2>/dev/null || true)"
  if [[ -n "$h" ]]; then
    echo "WARNING: Non-sample git hooks found inside workspace:"
    echo "$h"
    echo "Note: Git hooks created or modified by the agent will execute on YOUR HOST if triggered!"
  fi
}

# Parse mode flags (--ro / --rw) if provided first
if [[ "${1:-}" == "--ro" ]]; then
  MOUNT_MODE="ro"
  shift
elif [[ "${1:-}" == "--rw" ]]; then
  MOUNT_MODE="rw"
  shift
fi

case "${1:-}" in
  setup)
    command -v pacman >/dev/null || die "automated setup is Arch/CachyOS only; see docs/01-host-setup.md"
    nvidia-smi -L || die "NVIDIA driver not working on host (fix this first)"
    sudo pacman -S --needed docker docker-compose docker-buildx nvidia-container-toolkit
    sudo systemctl enable --now docker
    sudo nvidia-ctk runtime configure --runtime=docker
    sudo systemctl restart docker
    sudo docker run --rm --gpus all ubuntu:24.04 nvidia-smi -L
    dc build agent
    dc --profile setup run --rm ollama-pull
    echo "Setup complete. Next: ./verify-sandbox.sh, then ./lab <workspace>"
    ;;
  stop)
    dc down
    ;;
  exec)
    check_workspace "${2:?Usage: ./lab exec <path> '<command>'}"
    confirm
    dc run --rm -T agent sh -c "${3:?Missing command}"
    ;;
  shell)
    check_workspace "${2:?Usage: ./lab shell [--ro|--rw] <path>}"
    confirm
    dc run --rm agent bash
    warn_hooks
    ;;
  ""|-h|--help)
    echo "Usage:"
    echo "  ./lab [--ro|--rw] <path-to-workspace> [aider args]"
    echo "  ./lab [--ro|--rw] shell <path-to-workspace>"
    echo "  ./lab exec <path-to-workspace> '<command>'"
    echo "  ./lab setup"
    echo "  ./lab stop"
    ;;
  *)
    check_workspace "$1"
    confirm
    shift
    dc run --rm agent aider "$@"
    warn_hooks
    ;;
esac```

## File: `models.txt`
```
qwen2.5-coder:7b
fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:Q4```

## File: `ollama/pull-models.sh`
```
#!/bin/sh
set -eu
ollama serve >/tmp/serve.log 2>&1 &
pid=$!
until ollama list >/dev/null 2>&1; do sleep 1; done
while read -r m; do
  case "$m" in ''|'#'*) continue;; esac
  echo ">> pulling $m"
  ollama pull "$m"
done < /models.txt
kill "$pid"```

## File: `verify-sandbox.sh`
```
#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$(realpath "$0")")"
export LAB_YES=1
WS="$PWD/workspaces/.verify"; mkdir -p "$WS"
pass=0; fail=0

# check <name> <0=should succeed | 1=should fail> <shell command run inside container>
check() {
  local got=1
  ./lab exec "$WS" "$3" >/dev/null 2>&1 && got=0
  if [[ $got -eq $2 ]]; then echo "PASS  $1"; ((pass++)); else echo "FAIL  $1"; ((fail++)); fi
}

check "runs as non-root"           0 '[ "$(id -u)" != 0 ]'
check "root fs is read-only"       1 'touch /usr/x'
check "workspace is writable"      0 'touch /workspace/.t && rm /workspace/.t'
check "no internet (https)"        1 'curl -sS -m 4 https://example.com'
check "no internet (raw IP)"       1 'curl -sS -m 4 http://1.1.1.1'
check "ollama reachable"           0 'curl -sf -m 5 http://ollama:11434/api/tags'
check "zero capabilities"          0 "grep -q '^CapEff:[[:space:]]*0000000000000000' /proc/self/status"
check "no-new-privileges set"      0 "grep -q '^NoNewPrivs:[[:space:]]*1' /proc/self/status"
check "no docker socket"           0 '[ ! -e /var/run/docker.sock ]'
check "no host ssh keys visible"   0 "[ ! -e /home/$USER/.ssh ] && [ ! -e /root/.ssh ]"
check "no other home dirs"         0 '[ -z "$(ls -A /home | grep -v "^agent$")" ]'

if ss -tln | grep -q ':11434\b'; then
  echo "FAIL  something on the host is listening on 11434"; ((fail++))
else
  echo "PASS  nothing on host listens on 11434"; ((pass++))
fi

echo "---- $pass passed, $fail failed"
[[ $fail -eq 0 ]]```

## File: `workspaces/.gitkeep`
```
```

```

## File: `collate.sh`
```
#!/usr/bin/env bash
set -euo pipefail

OUTPUT="docs/little-mester-full-context.md"
mkdir -p "$(dirname "$OUTPUT")"

echo "📦 Collating ALL Little Mester files for external LLM assistance..."

cat > "$OUTPUT" << 'HEADER'
# Little Mester: Complete Project Context for Self-Modification
> **Instructions for External LLM (Claude/Gemini)**: 
> This document contains the complete source code, configuration, and operational workflows of the "Little Mester" agent. Use this context to analyze, suggest improvements, or generate code changes for Little Mester itself. When proposing changes, always consider the existing skills, testing workflows, and architecture defined below.

## 1. Operational Workflows & Skills
HEADER

# Add skills
if [ -d "skills" ]; then
    for skill in skills/*.md; do
        echo "### $(basename "$skill")" >> "$OUTPUT"
        cat "$skill" >> "$OUTPUT"
        echo "" >> "$OUTPUT"
    done
fi

cat >> "$OUTPUT" << 'SECTION'
## 2. Project Structure & Configuration
SECTION

# Add config files, scripts, etc.
git ls-files | grep -E '\.(sh|yaml|yml|toml|json|md|txt)$' | while read -r file; do
    echo "## File: \`$file\`" >> "$OUTPUT"
    echo '```' >> "$OUTPUT"
    cat "$file" >> "$OUTPUT"
    echo '```' >> "$OUTPUT"
    echo "" >> "$OUTPUT"
done

cat >> "$OUTPUT" << 'SECTION'
## 3. Core Agent Source Code
SECTION

# Add main source files (excluding scripts, docs, and the output file itself)
git ls-files | grep -vE '(scripts/|docs/|\.git/|node_modules/|'"$OUTPUT"')' | while read -r file; do
    echo "## File: \`$file\`" >> "$OUTPUT"
    echo '```' >> "$OUTPUT"
    cat "$file" >> "$OUTPUT"
    echo '```' >> "$OUTPUT"
    echo "" >> "$OUTPUT"
done || true

cat >> "$OUTPUT" << 'SECTION'
## 4. Instructions for Modification
- Always preserve the TDD and regression prevention workflows.
- When adding new skills, follow the format in `skills/`.
- Ensure all changes are compatible with the existing Docker/compose setup.
- Update this context file after making changes to keep it accurate.

---
*Generated by `collate.sh` on $(date)*
SECTION

echo "✅ Full Little Mester context generated at $OUTPUT"
```

## File: `compose.yaml`
```
name: little-mester

services:
  ollama:
    image: ollama/ollama:latest
    volumes:
      - ollama-models:/root/.ollama
    environment:
      OLLAMA_FLASH_ATTENTION: "1"
      OLLAMA_KV_CACHE_TYPE: q8_0
      OLLAMA_CONTEXT_LENGTH: "16384"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
    security_opt: ["no-new-privileges:true"]
    networks: [internal]            # no internet, no published ports
    healthcheck:
      test: ["CMD", "ollama", "list"]
      interval: 5s
      timeout: 5s
      retries: 30

  # One-shot: the ONLY service with internet. Used by `./lab setup`.
  ollama-pull:
    image: ollama/ollama:latest
    profiles: [setup]
    entrypoint: ["/bin/sh", "/pull-models.sh"]
    volumes:
      - ollama-models:/root/.ollama
      - ./ollama/pull-models.sh:/pull-models.sh:ro
      - ./models.txt:/models.txt:ro
    networks: [egress]

  agent:
    build:
      context: ./agent
      args:
        LAB_UID: "${LAB_UID:-1000}"
        LAB_GID: "${LAB_GID:-1000}"
    profiles: [agent]
    depends_on:
      ollama:
        condition: service_healthy
    user: "${LAB_UID:-1000}:${LAB_GID:-1000}"
    working_dir: /workspace
    volumes:
      - "${WORKSPACE:?run via ./lab}:/workspace:${MOUNT_MODE:-rw}"
    read_only: true
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    tmpfs:
      - /tmp
      - /home/agent:uid=${LAB_UID:-1000},gid=${LAB_GID:-1000},mode=0700
    mem_limit: 4g
    pids_limit: 512
    environment:
      HOME: /home/agent
      OLLAMA_API_BASE: http://ollama:11434
      # Swap out AIDER models
      #AIDER_MODEL: ollama_chat/qwen2.5-coder:7b
      AIDER_MODEL: ollama_chat/qwen3.6:35b-a3b
      AIDER_CHECK_UPDATE: "false"
      AIDER_ANALYTICS_DISABLE: "true"
      GIT_AUTHOR_NAME: little-mester-agent
      GIT_AUTHOR_EMAIL: agent@localhost
      GIT_COMMITTER_NAME: little-mester-agent
      GIT_COMMITTER_EMAIL: agent@localhost
      GIT_CONFIG_COUNT: "1"
      GIT_CONFIG_KEY_0: safe.directory
      GIT_CONFIG_VALUE_0: "*"
    networks: [internal]            # no internet
    stdin_open: true
    tty: true

networks:
  internal:
    internal: true
  egress: {}

volumes:
  ollama-models:```

## File: `docs/journal.md`
```
```

## File: `models.txt`
```
qwen2.5-coder:7b
qwen3.6:35b-a3b
codellama:70b-code-q2_K```

## File: `ollama/pull-models.sh`
```
#!/bin/sh
set -eu
ollama serve >/tmp/serve.log 2>&1 &
pid=$!
until ollama list >/dev/null 2>&1; do sleep 1; done
while read -r m || [ -n "$m" ]; do
  case "$m" in ''|'#'*) continue;; esac
  echo ">> pulling $m"
  ollama pull "$m" || echo "WARNING: Failed to pull $m"
done < /models.txt
kill "$pid"```

## File: `scripts/collate-web-context.sh`
```
#!/usr/bin/env bash
set -euo pipefail

# Configuration
OUTPUT_FILE="docs/little-mester-web-context.md"
SKILLS_DIR="skills"
CODEBASE_CONTEXT="codebase_context.md"
mkdir -p "$(dirname "$OUTPUT_FILE")"

echo "📦 Collating Little Mester Web Context..."

# Start the document with optimized LLM structure
cat > "$OUTPUT_FILE" << 'HEADER'
# Little Mester: Complete Context for Web LLMs (Claude/Gemini)
> **Instructions**: Copy and paste this entire document into your LLM web interface. It contains all necessary context, skills, and constraints to operate Little Mester effectively in a stateless or semi-stateless environment.

## 1. Role & Objective
Little Mester is an autonomous software engineering agent focused on high-quality, test-driven development with strict regression prevention. It operates under a TDD-first philosophy, ensuring all changes are covered by unit and integration tests before commitment.

## 2. Core Skills & Workflows
HEADER

# Dynamically inject all skills from the skills directory
if [ -d "$SKILLS_DIR" ]; then
    for skill_file in "$SKILLS_DIR"/*.md; do
        if [ -f "$skill_file" ]; then
            echo "### $(basename "$skill_file" .md)" >> "$OUTPUT_FILE"
            cat "$skill_file" >> "$OUTPUT_FILE"
            echo "" >> "$OUTPUT_FILE"
        fi
    done
else
    echo "*⚠️ Skills directory not found. Create 'skills/' and add .md files to enable dynamic injection.*" >> "$OUTPUT_FILE"
fi

# Append architecture & codebase context
cat >> "$OUTPUT_FILE" << 'SECTION'
## 3. Codebase Architecture & Structure
SECTION

if [ -f "$CODEBASE_CONTEXT" ]; then
    cat "$CODEBASE_CONTEXT" >> "$OUTPUT_FILE"
else
    echo "*ℹ️ Run `./collate.sh` to generate the latest codebase context first, or manually paste your architecture notes here.*" >> "$OUTPUT_FILE"
fi

# Append active task state (requires manual update or CI/CD hook)
cat >> "$OUTPUT_FILE" << 'SECTION'
## 4. Active Tasks & Recent Changes
> *Note: Update this section with current PRs, TODOs, or active feature branches before handing off to an external LLM.*

- **Current Focus**: [Describe current task or feature branch]
- **Recent Commits**: [List relevant hashes/messages]
- **Known Issues/Blockers**: [List any]

## 5. Execution Constraints & Guardrails
- Always follow the Testing & Regression Prevention Workflow.
- Never commit without passing both unit and integration tests.
- Prioritize integration tests for system stability, unit tests for logic correctness.
- If a test cannot be written, explicitly state why and propose a workaround.
- Ask for clarification if requirements are ambiguous or conflicting.

---
*Generated by `scripts/collate-web-context.sh` on $(date)*
SECTION

echo "✅ Web context generated at $OUTPUT_FILE"
echo "📋 Ready to paste into Claude/Gemini web UI."
```

## File: `skills/testing_workflow.md`
```
# Skill: Testing & Regression Prevention Workflow

## Objective
Ensure code quality by enforcing a strict testing lifecycle (TDD) and regression prevention before any code changes are committed. This skill ensures Little Mester analyzes existing tests, writes new ones up-front, and validates the entire suite.

## Trigger
This skill is active during all development tasks involving code modification, feature addition, or refactoring.

## Workflow Steps

### 1. Pre-Change Analysis
- **Identify Scope**: Determine which files/modules are affected by the requested change.
- **Audit Existing Tests**: Check for existing unit and integration tests in the target area.
- **Gap Analysis**: Identify missing coverage for the proposed changes. Explicitly state what is currently tested vs. what needs testing.

### 2. Test Strategy Selection (Workflow Choice)
Before writing code, Little Mester must select the appropriate testing strategy based on the task context:
- **Strategy A: Strict TDD (Recommended)**
  - Write failing unit tests first.
  - Write failing integration tests if the feature touches external systems or APIs.
  - Run tests to confirm failure.
- **Strategy B: Regression Focus**
  - Identify high-risk areas for regression in existing code.
  - Write specific regression tests for those areas.
  - Proceed with implementation.

### 3. Implementation (TDD Cycle)
1. **Write Tests**: Create unit and integration tests that define the expected behavior.
   - *Constraint*: Must include both Unit (logic) and Integration (system/API) tests where applicable.
2. **Run & Fail**: Execute the test suite to ensure new tests fail as expected.
3. **Implement Code**: Write the minimum code required to pass the tests.
4. **Run & Pass**: Execute the full test suite (unit + integration) to ensure success.

### 4. Regression Check
- Run the *entire* relevant test suite, not just new tests.
- Verify no existing functionality is broken.
- If regressions are found, fix them immediately before committing.

### 5. Commitment
- Commit the code and the associated tests together.
- Ensure commit message references the tests added/modified.

## Decision Matrix: Unit vs Integration
| Feature Type | Required Tests |
| :--- | :--- |
| Pure Logic / Algorithms | Unit Tests |
| API Endpoints | Integration Tests + Unit Tests (for logic) |
| Database Interactions | Integration Tests (with mock or test DB) |
| UI Components | Integration Tests (E2E) + Unit Tests (logic) |

## Execution Rules
- **NO** code changes are committed without passing tests.
- **NO** feature is considered "done" until regression checks pass.
- Always prioritize **Integration Tests** for system stability and **Unit Tests** for logic correctness.
- If a test cannot be written, explicitly state why and propose a workaround or manual verification step.
```

## File: `verify-sandbox.sh`
```
#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$(realpath "$0")")"
export LAB_YES=1
WS="$PWD/workspaces/.verify"; mkdir -p "$WS"
pass=0; fail=0

# check <name> <0=should succeed | 1=should fail> <shell command run inside container>
check() {
  local got=1
  ./lab exec "$WS" "$3" >/dev/null 2>&1 && got=0
  if [[ $got -eq $2 ]]; then echo "PASS  $1"; ((pass++)); else echo "FAIL  $1"; ((fail++)); fi
}

check "runs as non-root"           0 '[ "$(id -u)" != 0 ]'
check "root fs is read-only"       1 'touch /usr/x'
check "workspace is writable"      0 'touch /workspace/.t && rm /workspace/.t'
check "no internet (https)"        1 'curl -sS -m 4 https://example.com'
check "no internet (raw IP)"       1 'curl -sS -m 4 http://1.1.1.1'
check "ollama reachable"           0 'curl -sf -m 5 http://ollama:11434/api/tags'
check "zero capabilities"          0 "grep -q '^CapEff:[[:space:]]*0000000000000000' /proc/self/status"
check "no-new-privileges set"      0 "grep -q '^NoNewPrivs:[[:space:]]*1' /proc/self/status"
check "no docker socket"           0 '[ ! -e /var/run/docker.sock ]'
check "no host ssh keys visible"   0 "[ ! -e /home/$USER/.ssh ] && [ ! -e /root/.ssh ]"
check "no other home dirs"         0 '[ -z "$(ls -A /home | grep -v "^agent$")" ]'

if ss -tln | grep -q ':11434\b'; then
  echo "FAIL  something on the host is listening on 11434"; ((fail++))
else
  echo "PASS  nothing on host listens on 11434"; ((pass++))
fi

echo "---- $pass passed, $fail failed"
[[ $fail -eq 0 ]]```

## 3. Core Agent Source Code
## File: `.gitignore`
```
workspaces/*
!workspaces/.gitkeep
.aider*
```

## File: `.idea/.gitignore`
```
# Default ignored files
/shelf/
/workspace.xml
# Editor-based HTTP Client requests
/httpRequests/
# Ignored default folder with query files
/queries/
# Datasource local storage ignored files
/dataSources/
/dataSources.local.xml
```

## File: `.idea/little-mester.iml`
```
<?xml version="1.0" encoding="UTF-8"?>
<module type="JAVA_MODULE" version="4">
  <component name="NewModuleRootManager" inherit-compiler-output="true">
    <exclude-output />
    <content url="file://$MODULE_DIR$" />
    <orderEntry type="inheritedJdk" />
    <orderEntry type="sourceFolder" forTests="false" />
  </component>
</module>```

## File: `.idea/misc.xml`
```
<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="ProjectRootManager" version="2" languageLevel="JDK_21" default="true" project-jdk-name="21" project-jdk-type="JavaSDK">
    <output url="file://$PROJECT_DIR$/out" />
  </component>
</project>```

## File: `.idea/modules.xml`
```
<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="ProjectModuleManager">
    <modules>
      <module fileurl="file://$PROJECT_DIR$/.idea/little-mester.iml" filepath="$PROJECT_DIR$/.idea/little-mester.iml" />
    </modules>
  </component>
</project>```

## File: `.idea/runConfigurations/Collate.xml`
```
<component name="ProjectRunConfigurationManager">
  <configuration default="false" name="Collate" type="ShConfigurationType">
    <option name="SCRIPT_TEXT" value="" />
    <option name="INDEPENDENT_SCRIPT_PATH" value="true" />
    <option name="SCRIPT_PATH" value="$PROJECT_DIR$/collate.sh" />
    <option name="SCRIPT_OPTIONS" value="" />
    <option name="INDEPENDENT_SCRIPT_WORKING_DIRECTORY" value="true" />
    <option name="SCRIPT_WORKING_DIRECTORY" value="$PROJECT_DIR$" />
    <option name="INDEPENDENT_INTERPRETER_PATH" value="true" />
    <option name="INTERPRETER_PATH" value="/usr/bin/bash" />
    <option name="INTERPRETER_OPTIONS" value="" />
    <option name="EXECUTE_IN_TERMINAL" value="true" />
    <option name="EXECUTE_SCRIPT_FILE" value="true" />
    <envs />
    <method v="2" />
  </configuration>
</component>```

## File: `.idea/vcs.xml`
```
<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="VcsDirectoryMappings">
    <mapping directory="" vcs="Git" />
    <mapping directory="$PROJECT_DIR$" vcs="Git" />
  </component>
</project>```

## File: `LICENSE`
```
MIT License

Copyright (c) 2026 Ethan Freestone

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## File: `README.md`
```
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
```

## File: `agent/Dockerfile`
```
FROM python:3.12-slim

ARG LAB_UID=1000
ARG LAB_GID=1000

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ripgrep ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir aider-chat pytest

RUN groupadd -g ${LAB_GID} agent && useradd -m -u ${LAB_UID} -g ${LAB_GID} agent

ENV PATH="/opt/venv/bin:$PATH" HOME=/home/agent
WORKDIR /workspace
CMD ["aider"]```

## File: `codebase_context.md`
```
## File: `.gitignore`
```
workspaces/*
!workspaces/.gitkeep```

## File: `.idea/.gitignore`
```
# Default ignored files
/shelf/
/workspace.xml
# Editor-based HTTP Client requests
/httpRequests/
# Ignored default folder with query files
/queries/
# Datasource local storage ignored files
/dataSources/
/dataSources.local.xml
```

## File: `.idea/little-mester.iml`
```
<?xml version="1.0" encoding="UTF-8"?>
<module type="JAVA_MODULE" version="4">
  <component name="NewModuleRootManager" inherit-compiler-output="true">
    <exclude-output />
    <content url="file://$MODULE_DIR$" />
    <orderEntry type="inheritedJdk" />
    <orderEntry type="sourceFolder" forTests="false" />
  </component>
</module>```

## File: `.idea/misc.xml`
```
<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="ProjectRootManager" version="2" languageLevel="JDK_21" default="true" project-jdk-name="21" project-jdk-type="JavaSDK">
    <output url="file://$PROJECT_DIR$/out" />
  </component>
</project>```

## File: `.idea/modules.xml`
```
<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="ProjectModuleManager">
    <modules>
      <module fileurl="file://$PROJECT_DIR$/.idea/little-mester.iml" filepath="$PROJECT_DIR$/.idea/little-mester.iml" />
    </modules>
  </component>
</project>```

## File: `.idea/runConfigurations/Collate.xml`
```
<component name="ProjectRunConfigurationManager">
  <configuration default="false" name="Collate" type="ShConfigurationType">
    <option name="SCRIPT_TEXT" value="" />
    <option name="INDEPENDENT_SCRIPT_PATH" value="true" />
    <option name="SCRIPT_PATH" value="$PROJECT_DIR$/collate.sh" />
    <option name="SCRIPT_OPTIONS" value="" />
    <option name="INDEPENDENT_SCRIPT_WORKING_DIRECTORY" value="true" />
    <option name="SCRIPT_WORKING_DIRECTORY" value="$PROJECT_DIR$" />
    <option name="INDEPENDENT_INTERPRETER_PATH" value="true" />
    <option name="INTERPRETER_PATH" value="/usr/bin/bash" />
    <option name="INTERPRETER_OPTIONS" value="" />
    <option name="EXECUTE_IN_TERMINAL" value="true" />
    <option name="EXECUTE_SCRIPT_FILE" value="true" />
    <envs />
    <method v="2" />
  </configuration>
</component>```

## File: `.idea/vcs.xml`
```
<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="VcsDirectoryMappings">
    <mapping directory="" vcs="Git" />
    <mapping directory="$PROJECT_DIR$" vcs="Git" />
  </component>
</project>```

## File: `LICENSE`
```
MIT License

Copyright (c) 2026 Ethan Freestone

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## File: `README.md`
```
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
| `lab` | Launcher script. Validates the workspace path, then runs compose. | this file |
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
- **Capabilities**: slices of root's power in Linux. Dropped to zero here.```

## File: `agent/Dockerfile`
```
FROM python:3.12-slim

ARG LAB_UID=1000
ARG LAB_GID=1000

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ripgrep ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir aider-chat pytest

RUN groupadd -g ${LAB_GID} agent && useradd -m -u ${LAB_UID} -g ${LAB_GID} agent

ENV PATH="/opt/venv/bin:$PATH" HOME=/home/agent
WORKDIR /workspace
CMD ["aider"]```

## File: `collate.sh`
```
#!/usr/bin/env bash
set -euo pipefail

OUTPUT="codebase_context.md"

# Empty the file if it exists so we don't append to an older run
> "$OUTPUT"

echo "Collating codebase into $OUTPUT..."

# git ls-files ensures we only get tracked files, ignoring junk
git ls-files | while read -r file; do
  # Skip the output file itself just in case it gets tracked
  if [[ "$file" == "$OUTPUT" ]]; then continue; fi

  # Append filename and location as a markdown header
  echo "## File: \`$file\`" >> "$OUTPUT"

  # Append file content wrapped in markdown code blocks
  # You can dynamically grab the extension for syntax highlighting if needed,
  # but standard backticks work perfectly for LLM context.
  echo '```' >> "$OUTPUT"
  cat "$file" >> "$OUTPUT"
  echo '```' >> "$OUTPUT"
  echo "" >> "$OUTPUT"
done

echo "Done! Context saved to $OUTPUT"```

## File: `compose.yaml`
```
name: little-mester

services:
  ollama:
    image: ollama/ollama:latest
    volumes:
      - ollama-models:/root/.ollama
    environment:
      OLLAMA_FLASH_ATTENTION: "1"
      OLLAMA_KV_CACHE_TYPE: q8_0
      OLLAMA_CONTEXT_LENGTH: "16384"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
    security_opt: ["no-new-privileges:true"]
    networks: [internal]            # no internet, no published ports
    healthcheck:
      test: ["CMD", "ollama", "list"]
      interval: 5s
      timeout: 5s
      retries: 30

  # One-shot: the ONLY service with internet. Used by `./lab setup`.
  ollama-pull:
    image: ollama/ollama:latest
    profiles: [setup]
    entrypoint: ["/bin/sh", "/pull-models.sh"]
    volumes:
      - ollama-models:/root/.ollama
      - ./ollama/pull-models.sh:/pull-models.sh:ro
      - ./models.txt:/models.txt:ro
    networks: [egress]

  agent:
    build:
      context: ./agent
      args:
        LAB_UID: "${LAB_UID:-1000}"
        LAB_GID: "${LAB_GID:-1000}"
    profiles: [agent]
    depends_on:
      ollama:
        condition: service_healthy
    user: "${LAB_UID:-1000}:${LAB_GID:-1000}"
    working_dir: /workspace
    volumes:
      - "${WORKSPACE:?run via ./lab}:/workspace:${MOUNT_MODE:-rw}"
    read_only: true
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    tmpfs:
      - /tmp
      - /home/agent:uid=${LAB_UID:-1000},gid=${LAB_GID:-1000},mode=0700
    mem_limit: 4g
    pids_limit: 512
    environment:
      HOME: /home/agent
      OLLAMA_API_BASE: http://ollama:11434
      # Swap out AIDER models
      AIDER_MODEL: ollama_chat/fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:Q4
      #AIDER_MODEL: ollama_chat/qwen2.5-coder:7b
      AIDER_CHECK_UPDATE: "false"
      AIDER_ANALYTICS_DISABLE: "true"
      GIT_AUTHOR_NAME: little-mester-agent
      GIT_AUTHOR_EMAIL: agent@localhost
      GIT_COMMITTER_NAME: little-mester-agent
      GIT_COMMITTER_EMAIL: agent@localhost
      GIT_CONFIG_COUNT: "1"
      GIT_CONFIG_KEY_0: safe.directory
      GIT_CONFIG_VALUE_0: "*"
    networks: [internal]            # no internet
    stdin_open: true
    tty: true

networks:
  internal:
    internal: true
  egress: {}

volumes:
  ollama-models:```

## File: `docs/journal.md`
```
```

## File: `lab`
```
#!/usr/bin/env bash
set -euo pipefail

LAB_DIR="$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")" && pwd)"
LAB_UID="$(id -u)"
LAB_GID="$(id -g)"
WORKSPACE=""
MOUNT_MODE="rw"  # Default mount mode

die() { echo "REFUSED: $*" >&2; exit 1; }

dc() {
  sudo env "WORKSPACE=${WORKSPACE:-$LAB_DIR}" "LAB_UID=$LAB_UID" "LAB_GID=$LAB_GID" "MOUNT_MODE=$MOUNT_MODE" \
    docker compose -f "$LAB_DIR/compose.yaml" --project-directory "$LAB_DIR" "$@"
}

check_workspace() {
  local ws="$1"
  [[ -d "$ws" ]] || die "Not a directory: $ws"
  ws="$(realpath "$ws")"

  # System & Root folder protections
  [[ "$ws" != "/" ]] || die "Cannot mount filesystem root (/)"
  [[ "$ws" != "$HOME" ]] || die "Cannot mount entire home directory ($HOME)"

  # Ensure target is owned by current user
  [[ -O "$ws" ]] || die "Workspace directory must be owned by you (UID $LAB_UID)"

  # Prevent exposing the lab repo itself
  if [[ "$ws" == "$LAB_DIR" || "$LAB_DIR" == "$ws"/* ]]; then
    die "Workspace path would expose the little-mester repo itself"
  fi

  # Blacklist sensitive credential/config paths
  local sensitive_paths=(
    "$HOME/.ssh"
    "$HOME/.gnupg"
    "$HOME/.config"
    "$HOME/.aws"
    "$HOME/.kube"
    "$HOME/.local"
    "$HOME/.bashrc"
    "$HOME/.zshrc"
  )

  for s in "${sensitive_paths[@]}"; do
    if [[ "$ws" == "$s" || "$ws" == "$s"/* || "$s" == "$ws"/* ]]; then
      die "Path overlaps with sensitive system/credential directory: $s"
    fi
  done

  WORKSPACE="$ws"
}

confirm() {
  [[ "${LAB_YES:-}" == "1" ]] && return 0
  echo "--------------------------------------------------------"
  echo " Mounting path : $WORKSPACE"
  echo " Mount mode    : READ-${MOUNT_MODE^^}"
  echo "--------------------------------------------------------"
  read -r -p "Continue? [y/N] " a
  [[ "$a" == "y" || "$a" == "Y" ]] || exit 1
}

warn_hooks() {
  local h
  h="$(find "$WORKSPACE" -path '*/.git/hooks/*' -type f ! -name '*.sample' 2>/dev/null || true)"
  if [[ -n "$h" ]]; then
    echo "WARNING: Non-sample git hooks found inside workspace:"
    echo "$h"
    echo "Note: Git hooks created or modified by the agent will execute on YOUR HOST if triggered!"
  fi
}

# Parse mode flags (--ro / --rw) if provided first
if [[ "${1:-}" == "--ro" ]]; then
  MOUNT_MODE="ro"
  shift
elif [[ "${1:-}" == "--rw" ]]; then
  MOUNT_MODE="rw"
  shift
fi

case "${1:-}" in
  setup)
    command -v pacman >/dev/null || die "automated setup is Arch/CachyOS only; see docs/01-host-setup.md"
    nvidia-smi -L || die "NVIDIA driver not working on host (fix this first)"
    sudo pacman -S --needed docker docker-compose docker-buildx nvidia-container-toolkit
    sudo systemctl enable --now docker
    sudo nvidia-ctk runtime configure --runtime=docker
    sudo systemctl restart docker
    sudo docker run --rm --gpus all ubuntu:24.04 nvidia-smi -L
    dc build agent
    dc --profile setup run --rm ollama-pull
    echo "Setup complete. Next: ./verify-sandbox.sh, then ./lab <workspace>"
    ;;
  stop)
    dc down
    ;;
  exec)
    check_workspace "${2:?Usage: ./lab exec <path> '<command>'}"
    confirm
    dc run --rm -T agent sh -c "${3:?Missing command}"
    ;;
  shell)
    check_workspace "${2:?Usage: ./lab shell [--ro|--rw] <path>}"
    confirm
    dc run --rm agent bash
    warn_hooks
    ;;
  ""|-h|--help)
    echo "Usage:"
    echo "  ./lab [--ro|--rw] <path-to-workspace> [aider args]"
    echo "  ./lab [--ro|--rw] shell <path-to-workspace>"
    echo "  ./lab exec <path-to-workspace> '<command>'"
    echo "  ./lab setup"
    echo "  ./lab stop"
    ;;
  *)
    check_workspace "$1"
    confirm
    shift
    dc run --rm agent aider "$@"
    warn_hooks
    ;;
esac```

## File: `models.txt`
```
qwen2.5-coder:7b
fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:Q4```

## File: `ollama/pull-models.sh`
```
#!/bin/sh
set -eu
ollama serve >/tmp/serve.log 2>&1 &
pid=$!
until ollama list >/dev/null 2>&1; do sleep 1; done
while read -r m; do
  case "$m" in ''|'#'*) continue;; esac
  echo ">> pulling $m"
  ollama pull "$m"
done < /models.txt
kill "$pid"```

## File: `verify-sandbox.sh`
```
#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$(realpath "$0")")"
export LAB_YES=1
WS="$PWD/workspaces/.verify"; mkdir -p "$WS"
pass=0; fail=0

# check <name> <0=should succeed | 1=should fail> <shell command run inside container>
check() {
  local got=1
  ./lab exec "$WS" "$3" >/dev/null 2>&1 && got=0
  if [[ $got -eq $2 ]]; then echo "PASS  $1"; ((pass++)); else echo "FAIL  $1"; ((fail++)); fi
}

check "runs as non-root"           0 '[ "$(id -u)" != 0 ]'
check "root fs is read-only"       1 'touch /usr/x'
check "workspace is writable"      0 'touch /workspace/.t && rm /workspace/.t'
check "no internet (https)"        1 'curl -sS -m 4 https://example.com'
check "no internet (raw IP)"       1 'curl -sS -m 4 http://1.1.1.1'
check "ollama reachable"           0 'curl -sf -m 5 http://ollama:11434/api/tags'
check "zero capabilities"          0 "grep -q '^CapEff:[[:space:]]*0000000000000000' /proc/self/status"
check "no-new-privileges set"      0 "grep -q '^NoNewPrivs:[[:space:]]*1' /proc/self/status"
check "no docker socket"           0 '[ ! -e /var/run/docker.sock ]'
check "no host ssh keys visible"   0 "[ ! -e /home/$USER/.ssh ] && [ ! -e /root/.ssh ]"
check "no other home dirs"         0 '[ -z "$(ls -A /home | grep -v "^agent$")" ]'

if ss -tln | grep -q ':11434\b'; then
  echo "FAIL  something on the host is listening on 11434"; ((fail++))
else
  echo "PASS  nothing on host listens on 11434"; ((pass++))
fi

echo "---- $pass passed, $fail failed"
[[ $fail -eq 0 ]]```

## File: `workspaces/.gitkeep`
```
```

```

## File: `collate.sh`
```
#!/usr/bin/env bash
set -euo pipefail

OUTPUT="docs/little-mester-full-context.md"
mkdir -p "$(dirname "$OUTPUT")"

echo "📦 Collating ALL Little Mester files for external LLM assistance..."

cat > "$OUTPUT" << 'HEADER'
# Little Mester: Complete Project Context for Self-Modification
> **Instructions for External LLM (Claude/Gemini)**: 
> This document contains the complete source code, configuration, and operational workflows of the "Little Mester" agent. Use this context to analyze, suggest improvements, or generate code changes for Little Mester itself. When proposing changes, always consider the existing skills, testing workflows, and architecture defined below.

## 1. Operational Workflows & Skills
HEADER

# Add skills
if [ -d "skills" ]; then
    for skill in skills/*.md; do
        echo "### $(basename "$skill")" >> "$OUTPUT"
        cat "$skill" >> "$OUTPUT"
        echo "" >> "$OUTPUT"
    done
fi

cat >> "$OUTPUT" << 'SECTION'
## 2. Project Structure & Configuration
SECTION

# Add config files, scripts, etc.
git ls-files | grep -E '\.(sh|yaml|yml|toml|json|md|txt)$' | while read -r file; do
    echo "## File: \`$file\`" >> "$OUTPUT"
    echo '```' >> "$OUTPUT"
    cat "$file" >> "$OUTPUT"
    echo '```' >> "$OUTPUT"
    echo "" >> "$OUTPUT"
done

cat >> "$OUTPUT" << 'SECTION'
## 3. Core Agent Source Code
SECTION

# Add main source files (excluding scripts, docs, and the output file itself)
git ls-files | grep -vE '(scripts/|docs/|\.git/|node_modules/|'"$OUTPUT"')' | while read -r file; do
    echo "## File: \`$file\`" >> "$OUTPUT"
    echo '```' >> "$OUTPUT"
    cat "$file" >> "$OUTPUT"
    echo '```' >> "$OUTPUT"
    echo "" >> "$OUTPUT"
done || true

cat >> "$OUTPUT" << 'SECTION'
## 4. Instructions for Modification
- Always preserve the TDD and regression prevention workflows.
- When adding new skills, follow the format in `skills/`.
- Ensure all changes are compatible with the existing Docker/compose setup.
- Update this context file after making changes to keep it accurate.

---
*Generated by `collate.sh` on $(date)*
SECTION

echo "✅ Full Little Mester context generated at $OUTPUT"
```

## File: `compose.yaml`
```
name: little-mester

services:
  ollama:
    image: ollama/ollama:latest
    volumes:
      - ollama-models:/root/.ollama
    environment:
      OLLAMA_FLASH_ATTENTION: "1"
      OLLAMA_KV_CACHE_TYPE: q8_0
      OLLAMA_CONTEXT_LENGTH: "16384"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
    security_opt: ["no-new-privileges:true"]
    networks: [internal]            # no internet, no published ports
    healthcheck:
      test: ["CMD", "ollama", "list"]
      interval: 5s
      timeout: 5s
      retries: 30

  # One-shot: the ONLY service with internet. Used by `./lab setup`.
  ollama-pull:
    image: ollama/ollama:latest
    profiles: [setup]
    entrypoint: ["/bin/sh", "/pull-models.sh"]
    volumes:
      - ollama-models:/root/.ollama
      - ./ollama/pull-models.sh:/pull-models.sh:ro
      - ./models.txt:/models.txt:ro
    networks: [egress]

  agent:
    build:
      context: ./agent
      args:
        LAB_UID: "${LAB_UID:-1000}"
        LAB_GID: "${LAB_GID:-1000}"
    profiles: [agent]
    depends_on:
      ollama:
        condition: service_healthy
    user: "${LAB_UID:-1000}:${LAB_GID:-1000}"
    working_dir: /workspace
    volumes:
      - "${WORKSPACE:?run via ./lab}:/workspace:${MOUNT_MODE:-rw}"
    read_only: true
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    tmpfs:
      - /tmp
      - /home/agent:uid=${LAB_UID:-1000},gid=${LAB_GID:-1000},mode=0700
    mem_limit: 4g
    pids_limit: 512
    environment:
      HOME: /home/agent
      OLLAMA_API_BASE: http://ollama:11434
      # Swap out AIDER models
      #AIDER_MODEL: ollama_chat/qwen2.5-coder:7b
      AIDER_MODEL: ollama_chat/qwen3.6:35b-a3b
      AIDER_CHECK_UPDATE: "false"
      AIDER_ANALYTICS_DISABLE: "true"
      GIT_AUTHOR_NAME: little-mester-agent
      GIT_AUTHOR_EMAIL: agent@localhost
      GIT_COMMITTER_NAME: little-mester-agent
      GIT_COMMITTER_EMAIL: agent@localhost
      GIT_CONFIG_COUNT: "1"
      GIT_CONFIG_KEY_0: safe.directory
      GIT_CONFIG_VALUE_0: "*"
    networks: [internal]            # no internet
    stdin_open: true
    tty: true

networks:
  internal:
    internal: true
  egress: {}

volumes:
  ollama-models:```

## File: `lab`
```
#!/usr/bin/env bash
set -euo pipefail

LAB_DIR="$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")" && pwd)"
LAB_UID="$(id -u)"
LAB_GID="$(id -g)"
WORKSPACE=""
MOUNT_MODE="rw"  # Default mount mode

die() { echo "REFUSED: $*" >&2; exit 1; }

dc() {
  sudo env "WORKSPACE=${WORKSPACE:-$LAB_DIR}" "LAB_UID=$LAB_UID" "LAB_GID=$LAB_GID" "MOUNT_MODE=$MOUNT_MODE" \
    docker compose -f "$LAB_DIR/compose.yaml" --project-directory "$LAB_DIR" "$@"
}

check_workspace() {
  local ws="$1"
  [[ -d "$ws" ]] || die "Not a directory: $ws"
  ws="$(realpath "$ws")"

  # System & Root folder protections
  [[ "$ws" != "/" ]] || die "Cannot mount filesystem root (/)"
  [[ "$ws" != "$HOME" ]] || die "Cannot mount entire home directory ($HOME)"

  # Ensure target is owned by current user
  [[ -O "$ws" ]] || die "Workspace directory must be owned by you (UID $LAB_UID)"

  # Prevent exposing the lab repo itself
  if [[ "$ws" == "$LAB_DIR" || "$LAB_DIR" == "$ws"/* ]]; then
    die "Workspace path would expose the little-mester repo itself"
  fi

  # Blacklist sensitive credential/config paths
  local sensitive_paths=(
    "$HOME/.ssh"
    "$HOME/.gnupg"
    "$HOME/.config"
    "$HOME/.aws"
    "$HOME/.kube"
    "$HOME/.local"
    "$HOME/.bashrc"
    "$HOME/.zshrc"
  )

  for s in "${sensitive_paths[@]}"; do
    if [[ "$ws" == "$s" || "$ws" == "$s"/* || "$s" == "$ws"/* ]]; then
      die "Path overlaps with sensitive system/credential directory: $s"
    fi
  done

  WORKSPACE="$ws"
}

confirm() {
  [[ "${LAB_YES:-}" == "1" ]] && return 0
  echo "--------------------------------------------------------"
  echo " Mounting path : $WORKSPACE"
  echo " Mount mode    : READ-${MOUNT_MODE^^}"
  echo "--------------------------------------------------------"
  read -r -p "Continue? [y/N] " a
  [[ "$a" == "y" || "$a" == "Y" ]] || exit 1
}

warn_hooks() {
  local h
  h="$(find "$WORKSPACE" -path '*/.git/hooks/*' -type f ! -name '*.sample' 2>/dev/null || true)"
  if [[ -n "$h" ]]; then
    echo "WARNING: Non-sample git hooks found inside workspace:"
    echo "$h"
    echo "Note: Git hooks created or modified by the agent will execute on YOUR HOST if triggered!"
  fi
}

# Parse mode flags (--ro / --rw) if provided first
if [[ "${1:-}" == "--ro" ]]; then
  MOUNT_MODE="ro"
  shift
elif [[ "${1:-}" == "--rw" ]]; then
  MOUNT_MODE="rw"
  shift
fi

case "${1:-}" in
  setup)
    command -v pacman >/dev/null || die "automated setup is Arch/CachyOS only; see docs/01-host-setup.md"
    nvidia-smi -L || die "NVIDIA driver not working on host (fix this first)"
    sudo pacman -S --needed docker docker-compose docker-buildx nvidia-container-toolkit
    sudo systemctl enable --now docker
    sudo nvidia-ctk runtime configure --runtime=docker
    sudo systemctl restart docker
    sudo docker run --rm --gpus all ubuntu:24.04 nvidia-smi -L
    dc build agent
    dc --profile setup run --rm ollama-pull
    echo "Setup complete. Next: ./verify-sandbox.sh, then ./lab <workspace>"
    ;;
  stop)
    dc down
    ;;
  exec)
    check_workspace "${2:?Usage: ./lab exec <path> '<command>'}"
    confirm
    dc run --rm -T agent sh -c "${3:?Missing command}"
    ;;
  shell)
    check_workspace "${2:?Usage: ./lab shell [--ro|--rw] <path>}"
    confirm
    dc run --rm agent bash
    warn_hooks
    ;;
  ""|-h|--help)
    echo "Usage:"
    echo "  ./lab [--ro|--rw] <path-to-workspace> [aider args]"
    echo "  ./lab [--ro|--rw] shell <path-to-workspace>"
    echo "  ./lab exec <path-to-workspace> '<command>'"
    echo "  ./lab setup"
    echo "  ./lab stop"
    ;;
  *)
    check_workspace "$1"
    confirm
    shift
    dc run --rm agent aider "$@"
    warn_hooks
    ;;
esac```

## File: `models.txt`
```
qwen2.5-coder:7b
qwen3.6:35b-a3b
codellama:70b-code-q2_K```

## File: `ollama/pull-models.sh`
```
#!/bin/sh
set -eu
ollama serve >/tmp/serve.log 2>&1 &
pid=$!
until ollama list >/dev/null 2>&1; do sleep 1; done
while read -r m || [ -n "$m" ]; do
  case "$m" in ''|'#'*) continue;; esac
  echo ">> pulling $m"
  ollama pull "$m" || echo "WARNING: Failed to pull $m"
done < /models.txt
kill "$pid"```

## File: `skills/testing_workflow.md`
```
# Skill: Testing & Regression Prevention Workflow

## Objective
Ensure code quality by enforcing a strict testing lifecycle (TDD) and regression prevention before any code changes are committed. This skill ensures Little Mester analyzes existing tests, writes new ones up-front, and validates the entire suite.

## Trigger
This skill is active during all development tasks involving code modification, feature addition, or refactoring.

## Workflow Steps

### 1. Pre-Change Analysis
- **Identify Scope**: Determine which files/modules are affected by the requested change.
- **Audit Existing Tests**: Check for existing unit and integration tests in the target area.
- **Gap Analysis**: Identify missing coverage for the proposed changes. Explicitly state what is currently tested vs. what needs testing.

### 2. Test Strategy Selection (Workflow Choice)
Before writing code, Little Mester must select the appropriate testing strategy based on the task context:
- **Strategy A: Strict TDD (Recommended)**
  - Write failing unit tests first.
  - Write failing integration tests if the feature touches external systems or APIs.
  - Run tests to confirm failure.
- **Strategy B: Regression Focus**
  - Identify high-risk areas for regression in existing code.
  - Write specific regression tests for those areas.
  - Proceed with implementation.

### 3. Implementation (TDD Cycle)
1. **Write Tests**: Create unit and integration tests that define the expected behavior.
   - *Constraint*: Must include both Unit (logic) and Integration (system/API) tests where applicable.
2. **Run & Fail**: Execute the test suite to ensure new tests fail as expected.
3. **Implement Code**: Write the minimum code required to pass the tests.
4. **Run & Pass**: Execute the full test suite (unit + integration) to ensure success.

### 4. Regression Check
- Run the *entire* relevant test suite, not just new tests.
- Verify no existing functionality is broken.
- If regressions are found, fix them immediately before committing.

### 5. Commitment
- Commit the code and the associated tests together.
- Ensure commit message references the tests added/modified.

## Decision Matrix: Unit vs Integration
| Feature Type | Required Tests |
| :--- | :--- |
| Pure Logic / Algorithms | Unit Tests |
| API Endpoints | Integration Tests + Unit Tests (for logic) |
| Database Interactions | Integration Tests (with mock or test DB) |
| UI Components | Integration Tests (E2E) + Unit Tests (logic) |

## Execution Rules
- **NO** code changes are committed without passing tests.
- **NO** feature is considered "done" until regression checks pass.
- Always prioritize **Integration Tests** for system stability and **Unit Tests** for logic correctness.
- If a test cannot be written, explicitly state why and propose a workaround or manual verification step.
```

## File: `verify-sandbox.sh`
```
#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$(realpath "$0")")"
export LAB_YES=1
WS="$PWD/workspaces/.verify"; mkdir -p "$WS"
pass=0; fail=0

# check <name> <0=should succeed | 1=should fail> <shell command run inside container>
check() {
  local got=1
  ./lab exec "$WS" "$3" >/dev/null 2>&1 && got=0
  if [[ $got -eq $2 ]]; then echo "PASS  $1"; ((pass++)); else echo "FAIL  $1"; ((fail++)); fi
}

check "runs as non-root"           0 '[ "$(id -u)" != 0 ]'
check "root fs is read-only"       1 'touch /usr/x'
check "workspace is writable"      0 'touch /workspace/.t && rm /workspace/.t'
check "no internet (https)"        1 'curl -sS -m 4 https://example.com'
check "no internet (raw IP)"       1 'curl -sS -m 4 http://1.1.1.1'
check "ollama reachable"           0 'curl -sf -m 5 http://ollama:11434/api/tags'
check "zero capabilities"          0 "grep -q '^CapEff:[[:space:]]*0000000000000000' /proc/self/status"
check "no-new-privileges set"      0 "grep -q '^NoNewPrivs:[[:space:]]*1' /proc/self/status"
check "no docker socket"           0 '[ ! -e /var/run/docker.sock ]'
check "no host ssh keys visible"   0 "[ ! -e /home/$USER/.ssh ] && [ ! -e /root/.ssh ]"
check "no other home dirs"         0 '[ -z "$(ls -A /home | grep -v "^agent$")" ]'

if ss -tln | grep -q ':11434\b'; then
  echo "FAIL  something on the host is listening on 11434"; ((fail++))
else
  echo "PASS  nothing on host listens on 11434"; ((pass++))
fi

echo "---- $pass passed, $fail failed"
[[ $fail -eq 0 ]]```

## File: `workspaces/.gitkeep`
```
```

## 4. Instructions for Modification
- Always preserve the TDD and regression prevention workflows.
- When adding new skills, follow the format in `skills/`.
- Ensure all changes are compatible with the existing Docker/compose setup.
- Update this context file after making changes to keep it accurate.

---
*Generated by `collate.sh` on $(date)*
