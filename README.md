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
   overlay/state. Local-model forwarding is currently failing on the tested host, so
   starting the VM is useful for debugging but does not make its agent ready for normal work.

   To inspect that known model-path failure without changing host networking, use a separate
   host terminal:

   ```bash
   ./lab-vm -d models status
   ```

### VM acceptance-test workflow (direct guest agent)

This is for acceptance testing only—not normal work—while browser auth, permission, workload,
and safe-lifecycle gates remain open. A one-off guest completion does not meet the still-open
repeated-stream acceptance; local-model failures have also been reported. Metrics TUI and dialog
rendering remain open. It runs OpenCode as a native guest process (not in an
agent container). **Current support is an experimental Linux x86_64/QEMU profile**, not a
claim of support for every Unix. Guest Docker/Compose is for project workloads; the VM does
not expose or share the host Docker socket. VM metrics are being developed separately from
the existing host-Docker metrics path; neither path should be removed until both modes have
verified coverage. Local models can use a user-managed API bound to host loopback; the optional
Compose forwarder remains an experimental, host-Docker-dependent convenience. No firewall or
host-network changes are permitted to make it work. Manage the current host-to-guest path through
`lab-vm`:

```bash
    ./lab-vm models up          # configured local model API forwarder
    ./lab-vm metrics up         # separate host-native VM metrics endpoint
    ./lab-vm models status      # checks the configured provider; exits nonzero if unavailable
    ./lab-vm -d models diagnose # detailed, read-only host/model-path evidence
    ./lab-vm -d models diagnose --probe-egress llamacpp # explicit TCP egress probe
    ./lab-vm verify-isolation   # inspect live QEMU config and probe from inside the guest
    ./lab-vm agent               # guest TUI; browser OAuth handoff is enabled by default
    ./lab-vm agent --no-browser-auth # skip the temporary host-browser bridge
    ./lab-vm -d install-tools --name default --restart-opencode # install guest Bats/ShellCheck, then relaunch TUI
    ./lab-vm update               # check managed tools/config, then confirm updates
    ./lab-vm update opencode      # check/update only OpenCode
    ./lab-vm update --all         # include guest OS packages and Docker Engine
    ./lab-vm auth-forward default <port> # exact-port manual diagnostic fallback
```

Exit the current TUI before `install-tools`. It updates packages in the selected running
guest without reprovisioning or rebooting the VM; `--restart-opencode` starts a new TUI
afterward. Use `--name <profile>` for a non-default guest, and `-v`/`-d` for verbose/debug
diagnostics.

Fresh guests install the latest OpenCode CLI by default; ordinary `up` retains an already
installed version. `update` checks first and asks before applying detected changes. Exit the
TUI and any guest workloads first. A semantic-version `VM_OPENCODE_VERSION` pin is supported;
`--all` also updates managed OS/Docker packages and refuses to proceed while guest containers
are running. Updates do not recreate the VM or replace guest credentials.

`verify-isolation` requires an already-running VM; it does not start or modify it. It verifies
the live QEMU process uses `restrict=on`, the SSH host forward is loopback-only, and the four
reviewed guest forwards target host loopback. It then runs bounded probes over SSH from inside
the guest: representative connections to the QEMU host gateway and direct Internet are expected
to fail; the egress proxy must refuse a private destination; the configured model API guestfwd
and the selected virtiofs workspace mount must work. This tests representative paths, not every
possible host service or exploit, and does not claim the guest has no host access: the selected
workspace, model/metrics forwarders and policy-controlled proxy are intentional channels. A failed
check exits nonzero with the individual results; it never repairs settings or changes firewall,
network, VM or container state.

Native provider sign-in happens in the guest TUI through `/connect`; `./lab-vm agent` uses
the one-command OpenAI browser flow by default. Add `--no-browser-auth` to skip the
temporary bridge. Nested launches inherit a recursion marker and default to no browser
bridge; `--browser-auth` explicitly opts a nested launch in. The MCP bridge opens only
approved provider authorization URLs in the host browser and derives one loopback SSH callback
forward from the runtime OAuth `redirect_uri`; the URL is not rewritten or logged. This relies on
the pinned OpenCode 2.2.0 implementation, not a stable CLI/API contract; see [decision C.9.13](docs/DECISIONS.md)
and [the auth runbook](docs/chatgpt-auth.md) for the source reference and version-update procedure.
`auth-forward` takes one exact callback port as a manual diagnostic fallback. The implementation
and live-acceptance ledger is ticket #45. The owner reports browser auth works; credential
persistence and remaining host-isolation acceptance are still open. Do not expect guest
credentials to survive destructive VM deletion.

VM agent work may use any public HTTP(S) destination through the existing host proxy. QEMU
still runs with `restrict=on`, so guests have no direct Internet or host route. The proxy
rejects private/reserved/multicast addresses, mixed public/private DNS answers, and the host's
local or directly connected networks, including IPv4-embedded IPv6 destinations. This broad
public-web policy does not expose host services or permit non-HTTP protocols. A running VM
proxy must be refreshed with `./lab-vm egress-reload` after the manifest change; live host
isolation and public-workflow acceptance remain separate gates.

`models up` selects the provider in `agent/opencode/opencode.json` by default; choose
`ollama`, `llamacpp`, or `all` explicitly when needed. It first uses an already-running,
healthy API bound only to host `127.0.0.1`; this path does not require Docker. Ollama uses
`127.0.0.1:11434`; a native llama.cpp server must use `--host 127.0.0.1 --port 8080` so it
matches the reviewed QEMU forward. If no host API is listening, `models up` can start the
optional Compose API forwarder, whose publication must remain loopback-only. `models status`
checks only the configured provider by default, or a selected provider with
`models status ollama|llamacpp|all`; it verifies the host listener is loopback-only and its
health endpoint responds. If no native endpoint is available, it also checks the Compose
forwarder and exits nonzero if it is missing, unhealthy, incorrectly bound, or unreachable.
Use `./lab-vm -d models diagnose` for read-only, Docker-specific layer-by-layer evidence.
That command records resolved Compose ports, Docker context/daemon mode, container bindings and
network attachments and internal-network settings, `docker port`, allowlisted Docker daemon
publication settings, relevant host listeners and NAT/firewall publication rules, loopback HTTP
probes, direct host-to-forwarder-container HTTP probes (to distinguish container reachability
from port publication), a host-originated probe to the non-loopback address selected by the
default IPv4 route, backend health, and available Docker journal diagnostics in the
normal `.mester/logs/` run log. It is read-only: it
does not start/recreate containers, alter daemon/firewall/network settings, or remove orphans.
If firewall-rule inspection is blocked by host permissions, rerun with
`./lab-vm -d models diagnose --sudo llamacpp`; `--sudo` uses `sudo -n` only for read-only
iptables/nftables and daemon-config reads and never prompts or escalates the other probes.
The forwarders remain attached to the internal model network and join a dedicated publication
bridge because Docker Engine can silently omit published-port mappings when a service is attached
only to an `internal: true` bridge. Masquerading is disabled on the publication bridge, but this
alone is not an egress firewall. The output identifies unavailable checks and exits nonzero if a
running forwarder is unhealthy or a non-loopback-address probe succeeds. These are host-side
Docker-forwarder checks, not tests of what the VM agent can access. The optional `--probe-egress`
attempts one TCP connection from each selected host forwarder to `1.1.1.1:443`; a successful
connection proves some egress exists, while a failed connection does not prove all destinations
or protocols are blocked. Neither probe changes host or container configuration. No separate
LAN device is needed for the guest isolation check; use `./lab-vm verify-isolation` for that.
VM metrics use one shared native host process at `127.0.0.1:19532`, independent of Docker;
it returns only curated metrics API data. Every outer VM profile has its own QEMU guestfwd
to that same host endpoint. `./lab-vm agent` starts the metrics service idempotently and checks
`/summary`, `/metrics`, and `/history` from inside the guest before opening OpenCode. If the
optional metrics process or forward is unavailable, the launcher warns but still starts
OpenCode; the TUI reports the failure and retries. Slow hardware sampling leaves the last
snapshot available instead of blocking concurrent session requests.
`./lab-vm metrics up/status/down` remain available for manual diagnostics. The host-Docker
`metrics` Compose service is unchanged. `models diagnose` is the standard first
step when a model API is unhealthy; `models up -d` also prints publication diagnostics if
startup fails. Neither path changes host firewall or networking. Stop the model forwarders
and metrics process separately when finished with VM testing:

```bash
./lab-vm models down
./lab-vm metrics down
```

`./lab-vm extra` delegates to `scripts/vm-extra.sh`, a **temporary** end-to-end guest
acceptance helper that must be removed after ticket #42's model, metrics and TUI gates pass.
Use the built-in `models diagnose` command for host Docker/model-forwarder troubleshooting;
it is read-only and does not attempt repairs.

On a fresh host, install the QEMU/KVM and `virtiofsd` prerequisites in
[`docs/01-host-setup.md`](docs/01-host-setup.md). The normal VM workflow is one
workspace-oriented command, matching the Docker interface:

```bash
./lab-vm /path/to/project
# or, explicitly:
./lab-vm run /path/to/project
# for an isolated acceptance run:
./lab-vm run /path/to/project --name launcher-smoke
```

The command starts the VM if needed, provisions the guest only on first use, then
attaches OpenCode directly to the selected workspace. `./lab-vm base` remains an
optional one-time prefetch; `./lab-vm up --workspace <path>` remains available for
setup and diagnostics without launching the TUI. `./lab-vm agent` attaches to the
already-running VM and its current workspace.

Each VM profile mounts exactly one host workspace at `/workspace`. Re-running with
that same path reuses the warm VM and enters the TUI without a reboot. `--name`
selects a separate profile and overlay, so host acceptance testing can avoid the
existing `default` profile. To switch to
another workspace, exit the current TUI and run the same command with the new path.
If the VM is still running on a different workspace, the CLI asks before requesting
a clean guest shutdown and restarting QEMU/virtiofsd with the new single-workspace
mount. The qcow2 overlay, installed guest tools, and control keys are preserved;
no base image or guest package provisioning is repeated, and neither host workspace
is deleted. Guest processes and active workload containers stop during the switch.
`--yes` skips the confirmation and therefore should only be used when that
interruption is intended. `stop`, `down`, `--clean`, and `rebuild` have separate,
more destructive effects described below.

### Choosing sessions, workspaces, and branches

| Goal | Use this | What to expect |
|---|---|---|
| One agent on one project | One VM profile and one workspace (`./lab-vm run /path/to/project`) | The guest and overlay are reused on later launches. |
| Several OpenCode sessions on the same checkout | Start `./lab-vm agent` in separate terminals against the same running profile | Each is a separate TUI process, but they edit the same files and share the guest. Coordinate edits; this is not an agent orchestrator. |
| Parallel branches with strong workspace separation | Create one Git worktree per branch and give each a separate named VM profile | Each profile mounts one worktree and has its own overlay. First use of a new profile provisions a guest; it is not a warm workspace switch. Concurrent VMs consume additional host resources. |
| One guest that can see several worktrees | Mount a common parent containing the worktrees | All descendants of that selected parent are intentionally exposed. The launcher currently starts sessions at `/workspace`; a polished per-session worktree/cwd selector is not implemented. |
| Have agents spawn/coordinate more agents | Not implemented | There is no supported agent-loop supervisor, work ownership protocol, or automatic worktree lifecycle yet; tracked in #56. |
| Test Little Mester inside Little Mester | Explicit nested VM design, after recursion gates pass | Nested KVM availability, depth enforcement, and child-state handling are not yet accepted. Do not treat the current recursion marker as a depth limit. |

Use a **same-profile workspace switch** only for sequential work: exit OpenCode, select the
new workspace, and let the guest shut down and restart while retaining that profile's overlay.
It does not keep two workspaces mounted concurrently. Use separate profiles for concurrent
VMs; `--name` is profile selection, not a switch shortcut. The temporary browser bridge uses
independent loopback ports per authorization session, while metrics intentionally use one
host-wide service per immediate VM host. A nested VM must enforce its own boundary and must
not inherit an ancestor's services or control channels by default.

For parallel work, separate Git worktrees avoid agents overwriting the same branch, but they do
not isolate uncommitted work if multiple sessions point at one worktree. In the interim, create
worktrees explicitly and either mount the intended worktree in its own profile or mount a
carefully chosen parent and manually select the correct project directory. Automated agent
spawning, session-to-worktree assignment, resource limits, cancellation, and safe cleanup are
future work—not implied by having multiple OpenCode processes.

For self-development, explicitly allow the repo workspace:

```bash
./lab-vm --unsafe-recurse "$PWD"
```

Use `--no-browser-auth` when the temporary browser bridge is not wanted. `ssh` is
for guest diagnostics, not a required daily step. The one-command launcher does not
change `./lab`'s default host-Docker behavior; VM-first routing remains gated on the
parent ticket's live usability, auth, permission, workload, isolation, and lifecycle
acceptance.

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
requirements, but are not implemented yet; the present recursion environment marker only
controls default browser-auth behavior and does **not** enforce depth. See #43/#52. When a guest
agent launches another `lab-vm`, an inherited recursion marker disables browser auth by
default; pass `--browser-auth` to explicitly enable the temporary bridge for that nested run.

Hardware configuration lives in [`machine.conf.example`](machine.conf.example). Copy it to `machine.conf` (gitignored) and fill in your specs. The benchmark tools use it to suggest optimal starting points.

---

## Everyday use

The commands below are for the established host-Docker workflow. For the VM acceptance
workflow, use the one-command `./lab-vm <workspace>` launcher as described above. After the
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
