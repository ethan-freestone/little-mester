# VM harness architecture and trust boundary

## Replacement implementation status (2026-11-03)

The owner approved `PR_REIMPLEMENTATION_PLAN.md` for `feat/vm-approach-2`.
The contract below supersedes the historical proposal later on this page.
At checkpoint C01 only CLI/configuration, path validation and private profile
metadata are being implemented. **No VM boot/lifecycle support or real-host
acceptance is claimed yet.** Ordinary `./lab` remains the existing Docker launcher.

### CLI and configuration contract

- `./lab-vm --help`: command/options and actual checkpoint availability.
- `./lab-vm context [--json] [--name NAME]`: read-only effective configuration,
  private profile paths and host capabilities; no directory creation or repairs.
- `./lab-vm setup`: read-only Linux x86_64/KVM/QEMU/virtiofs prerequisite report.
  `setup --install-prereqs` is a separate explicitly approved package action with
  default-no confirmation; normal launch/status/doctor never installs packages.
  Ambient `VM_YES`/`LAB_YES` cannot approve a host package install: skipping its
  confirmation requires `--yes` explicitly on that setup invocation.
- Future `./lab-vm <workspace>` / `run <workspace>` selects the exact named profile
  and enters a native guest agent. `up NAME --workspace PATH` is setup without
  entering the agent. Until implemented, these operations refuse with exit2,
  rather than returning fake success or starting a different profile.
  `--memory` applies only to fresh-profile `run`/`up`, not prerequisite setup.
- Global flags may precede or follow command/path. `--` ends launcher parsing and
  preserves subsequent agent arguments. `--yes` only confirms launcher actions;
  `--auto-accept`/`--permissions auto` requests agent auto mode, never overrides
  an explicit deny. Conflicting modes/duplicate value options fail before state.
- `--recurse` permits a filtered source snapshot; `--unsafe-recurse` separately
  permits writable self-development and requires a workspace containing the
  checkout. Both remain bounded by explicitly configured maximum depth (default0).

Configuration is optional **private JSON data**, never a sourced shell file:
`$MESTER_STATE_DIR/config.json`, default state root
`${XDG_DATA_HOME:-$HOME/.local/share}/little-mester`. Precedence: CLI, supported
environment values, private config, defaults. Schema version1 accepts only
`host_profile`, `workload_runtime`, `opencode_version`, `agent`, `model`, `metrics`,
`auth_forward`, `auth_escrow_dir`. It contains selectors, never credentials.
Unknown fields/schema/types/duplicate JSON keys or insecure config paths refuse.

Defaults: Linux x86_64, guest workload runtime Docker, OpenCode with latest resolved
once at fresh setup, no selected model, top-level metrics on, credential forwarding
off. Local Ollama/llama.cpp requires an explicit loopback API port; remote uses
mediated public egress. No host Docker/model service is implicitly started.

State is outside every workspace: `vm/profiles/NAME/profile.json` plus private
runtime/disks/keys, immutable image cache, services, run evidence and logs. Profile
names are validated, directories0700/files0600, metadata atomically replaced under
ordered locks. Unknown/legacy state is never silently migrated/discovered.
Read-only commands do not create/chmod state. Private control runtime captures
explicit reviewed files independently of the mutable source; re-executing edited
source on the host still requires review.

Examples shared by Bash and Fish (diagnostics only):

```sh
./lab-vm --help
./lab-vm context --json --name candidate
./lab-vm setup
```

### Approved target trust boundary (not yet runtime acceptance)

The agent runs **directly in a guest VM**, with guest administration authority.
Guest Docker is for project workloads, not an agent wrapper or host security
boundary. Only the chosen owned workspace is exported. Host state, credentials,
Docker socket and other paths are not shared. QEMU restricted user networking
and a public-only HTTP(S) proxy deny host/LAN/private destinations; there is no
host firewall/routing fallback. Optional model/metrics/auth crossings are narrow
and explicitly selected. Daily suspend/resume preserves state; deletion is scoped
and separately acknowledged.

Self-development must modify Little Mester including VM code, freeze a filtered
revision-bound candidate, provision fresh nested VMs **from that changed code**,
test failure→fix→fresh pass, and preserve parent usability. Additional agents in
one guest are not equivalent evidence. Recursion policy is enforced by supported
launchers, not a tamper-proof restriction on a guest administrator. Real KVM,
workload, network, credentials and human TUI/auth evidence remain required.

---

## Historical proposal (superseded; NOT current implementation instructions)

The remainder preserves the pre-replacement context until the final documentation
checkpoint. Its agent-container/rootless/default-launch/firewall-fallback claims
are superseded by the approved contract above and must not be used operationally.

> Decision: `docs/DECISIONS.md` §C.8. Tickets: epic #35, spike #40, provisioning #41,
> agent image #42, workspace Docker/egress #43, docs #44, model-agnostic harness #45.
> This page is the reviewer's one-page view; it is not a promise that every arrow is
> implemented yet. #41–#43 carry the acceptance evidence.

## 1. Shape

```
Host (your machine) — ./lab runs here as you, not as a service
┌──────────────────────────────────────────────────────────────────────────────┐
│  ./lab                                                                       │
│   ├─ check_workspace()            path guards run BEFORE anything is shared   │
│   ├─ qemu-system-x86_64          KVM, per-VM qcow2 overlay, cloud-init seed   │
│   ├─ virtiofsd                   exports ONLY the selected workspace          │
│   ├─ egress proxy (allowlist)    exposed at 10.0.2.100:3128 via guestfwd      │
│   └─ ollama / llamacpp           127.0.0.1:11434 / :8080 via guestfwd         │
│                                                                              │
│   ┌────────────────────────── disposable VM ───────────────────────────────┐ │
│   │  systemd                                                                │ │
│   │  rootless Docker daemon           user: mester                           │ │
│   │    unix:///run/user/<uid>/docker.sock                                   │ │
│   │                                                                         │ │
│   │  ┌──────────────── agent container ─────────────────┐                   │ │
│   │  │ opencode + docker CLI + compose v2               │                   │ │
│   │  │ /workspace  (virtiofs, rw)                       │                   │ │
│   │  │ docker.sock (only when Docker access is enabled) │                   │ │
│   │  └──────────────────────────────────────────────────┘                   │ │
│   │                                                                         │ │
│   │  ┌──────────── workspace containers (optional) ─────┐                   │ │
│   │  │ postgres, redis, ...                             │                   │ │
│   │  │ labelled, internal networks, no host ports       │                   │ │
│   │  └──────────────────────────────────────────────────┘                   │ │
│   └─────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────┘
```

Only three kinds of traffic cross the VM boundary: the workspace (virtiofs), SSH control
(loopback-only host forward), and explicitly forwarded model/egress endpoints. There is
no general host route.

## 2. Crossings

| Crossing | Direction | What is exposed | What is *not* exposed |
|---|---|---|---|
| `virtiofsd` → guest `/workspace` | host → VM | Exactly the directory passed to `./lab`, read/write | `$HOME`, `~/.ssh`, `~/.config`, the lab repo without `--unsafe-recurse`, Docker sockets, other checkouts |
| SSH control | host → VM | `127.0.0.1:<ephemeral>` → guest `:22`, per-VM key, per-VM `known_hosts` | Host keys/credentials inside the VM; VM listening on the host LAN |
| Model API `guestfwd` | VM → host | ollama `:11434`, llamacpp `:8080` only | General host loopback, host filesystem, host Docker |
| Egress proxy `guestfwd` | VM → internet | HTTP(S) destinations allowed by `.mester-egress` | Unlisted domains, non-HTTP protocols (until added deliberately), raw host routing |
| Docker socket | agent container → VM daemon | Rootless daemon API, only when access is enabled | Host Docker daemon, rootful VM daemon by default |

## 3. Boundaries and what a compromise buys

| Compromised component | Can reach | Cannot reach (by design) |
|---|---|---|
| **Agent process / container** | `/workspace`, its own rootless containers, model APIs, allowlisted egress | Host filesystem outside the workspace, host loopback, host Docker, other VMs, VM root, host LAN |
| **A container started from the workspace** | `/workspace` as mounted, other containers on its Docker networks, allowlisted egress | Host, VM root, the agent container (unless explicitly networked), host ports |
| **The VM kernel / rootless daemon** | Other VM processes and the exported workspace | Host kernel, host files outside the export, host Docker, host credentials |
| **The host user** | Everything (they own the machine) | — the harness does not pretend to protect the host from its owner |

The workspace is the data boundary, the VM is the execution boundary, and rootless Docker
is the privilege boundary inside the VM. A compromised agent can corrupt the workspace it
was pointed at; that is the same authority the owner granted by choosing that directory.

## 4. Lifecycle

1. `./lab <dir>` validates the path on the host, resolves the pinned base image, and
   creates `.mester/vm/<name>/` (overlay, seed, keys, sockets; gitignored).
2. `qemu-system-x86_64` starts with KVM, the virtiofs device, SSH on a loopback ephemeral
   port, model/egress `guestfwd` entries, and `restrict=on`.
3. cloud-init creates `mester`, installs Docker/compose and configures rootless mode,
   proxy settings and the workspace mount.
4. `./lab` starts the agent container with the workspace mount and (optionally) the
   rootless Docker socket.
5. The agent runs `docker compose` for the workspace; containers are labelled with the
   workspace path and project name.
6. `./lab stop` runs the workspace/agent teardown, stops QEMU cleanly, and removes the
   overlay, seed and sockets. The base image stays cached; nothing workspace-specific does.

## 5. Deliberate non-goals

- Protecting the host from its owner, or from a malicious host kernel/driver.
- Running the agent on the host. The host runs QEMU/`virtiofsd`, not the agent.
- GPU passthrough into the VM. Models stay on the host behind a narrow API.
- Perfect workspace confidentiality from the VM: virtiofs is a shared filesystem by design.
- A remote shared Docker daemon as the default. Remote-target mode is #47 and is documented
  as weaker isolation.
- Alternative runtimes. They are evaluated in #46, not smuggled in here.

## 6. Open items to prove in implementation

- `restrict=on` must still allow the explicit `guestfwd` entries and SSH `hostfwd`; if it
  does not, the fallback is a host-side firewall + proxy with the same crossings, never a
  general host route.
- virtiofs mount/permission behaviour for UID-mapped rootless containers (#41/#43).
- Rootless Docker port and image limitations, with per-workspace fixes rather than a
  rootful default (#42/#43).
- DNS and non-HTTP egress: only the proxy path is allowed initially; anything else is a
  documented follow-up, not an accidental hole (#43).
- Remote host mode (#47) must repeat the same path guards, labels, teardown and egress
  guarantees, and state its weaker host-sharing model in `SECURITY.md`.
