# VM harness architecture and trust boundary

> Decision: `docs/DECISIONS.md` §C.8. Tickets: epic #35, spike #40, provisioning #41,
> direct guest harness #42, guest workloads/egress #43, docs #44, model-agnostic harness #45.
> This page is the reviewer's one-page view; it is not a promise that every arrow is
> implemented yet. #41–#43 carry the acceptance evidence.

## 1. Shape

```
Host (your machine) — ./lab runs here as you, not as a service
┌──────────────────────────────────────────────────────────────────────────────┐
│  ./lab                                                                       │
│   ├─ check_workspace()            path guards run BEFORE anything is shared   │
│   ├─ VM host adapter              QEMU + supported host acceleration          │
│   ├─ virtiofsd                   exports ONLY the selected workspace          │
│   ├─ egress proxy (allowlist)    exposed at 10.0.2.100:3128 via guestfwd      │
│   └─ optional model API          user-configured loopback endpoint via guestfwd│
│                                                                              │
│   ┌────────────────────────── disposable VM ───────────────────────────────┐ │
│   │  systemd                                                                │ │
│   │  Little Mester harness runs directly in the guest (no agent container)  │ │
│   │    OpenCode / Aider / Claude Code / Codex adapter                       │ │
│   │    native guest auth + temporary loopback browser callback via SSH      │ │
│   │    /workspace (virtiofs, rw)                                            │ │
│   │                                                                         │ │
│   │  optional guest Docker + Compose (project workload capability)          │ │
│   │    postgres, redis, arbitrary project services                          │ │
│   │    optional nested Little Mester QEMU/KVM VMs                           │ │
│   └─────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────┘
```

Only explicitly selected crossings traverse the VM boundary: the workspace (virtiofs), SSH
control (loopback-only host forward), and named model, metrics, or egress endpoints. There is
no general host route. VM metrics transport is a separate integration from the existing
host-Docker metrics path; the latter remains in place during migration.

## 2. Crossings

| Crossing | Direction | What is exposed | What is *not* exposed |
|---|---|---|---|
| `virtiofsd` → guest `/workspace` | host → VM | Exactly the directory passed to `./lab`, read/write | `$HOME`, `~/.ssh`, `~/.config`, the lab repo without `--unsafe-recurse`, Docker sockets, other checkouts |
| SSH control | host → VM | `127.0.0.1:<ephemeral>` → guest `:22`, per-VM key, per-VM `known_hosts` | Host keys/credentials inside the VM; VM listening on the host LAN |
| Optional model API `guestfwd` | VM → one configured host endpoint | One explicitly selected local API port | General host loopback, host filesystem, host Docker |
| Optional metrics endpoint/stream | VM → one configured host metrics endpoint | Read-only metrics API/data needed by the guest plugin | Compose DNS assumptions, host `/proc` mounts, host filesystem, host Docker, general host loopback |
| Egress proxy `guestfwd` | VM → internet | HTTP(S) destinations allowed by `.mester-egress` | Unlisted domains, non-HTTP protocols (until added deliberately), raw host routing |
| Temporary browser OAuth callback | host browser → VM guest | One exact runtime-selected callback port, bound to host `127.0.0.1` and forwarded to guest `127.0.0.1` over the temporary SSH control master | Port ranges, LAN listeners, callback URL rewriting, general host route, callback/token capture |
| Guest runtime | agent in guest → VM | Guest Docker/Compose, guest processes, optional nested VMs | Host Docker daemon, host filesystem outside the selected workspace |

### Temporary browser OAuth callback: version-specific integration

For pinned OpenCode 2.2.0, source commit
`1097e2668b1e08ad63bdf0b1c4860b9bc01e420c` at
`packages/core/src/plugin/provider/chatgpt.ts` calls
`server.listen(0, "127.0.0.1", ...)` and places the chosen port in the OAuth
`redirect_uri` as `http://127.0.0.1:<port>/auth/callback`. This is confirmed by
source inspection, **not a documented/stable OpenCode CLI or API contract**.

`./lab-vm agent --browser-auth` starts the guest agent with an SSH control
master and host MCP browser bridge. When the guest opener requests the approved
authorization URL, the bridge extracts only that `redirect_uri` port, validates
the exact loopback host and callback
path, then requests one SSH `-O forward` from host `127.0.0.1:<port>` to guest
`127.0.0.1:<port>`. Only after the forward succeeds does it open the original
URL unchanged. The forward exists only while the auth command's SSH master is
alive. A new callback port replaces the previous forward before browser launch;
there is at most one callback listener. Auth forwarding commands ignore user SSH
config and disable SSH agent/X11 forwarding to prevent inherited forwards,
proxy commands, or agent sockets from expanding this capability. The bridge
accepts only the pinned authorization endpoint and callback shape. No port range
is opened, and
the browser URL, OAuth state, code and tokens are not logged or captured.

Authentication does not grant host execution, host filesystem/Docker access, or
a general network route. The existing public-destination proxy checks apply to
login traffic as well: loopback, private, link-local, multicast and mixed
public/non-public DNS answers are rejected before connection, and connections
use the validated IP rather than resolving the hostname again.

This is intentionally runtime-derived to avoid a hardcoded ephemeral port, but
it still depends on an internal OpenCode URL shape. Unexpected/missing redirect
URIs fail closed before browser launch. On every OpenCode version bump, inspect
the provider source and callback URL construction, update parser tests and this
document, and require owner-controlled live acceptance before claiming auth
works. See [the auth runbook](chatgpt-auth.md) and ticket #45 for operator steps
and test evidence.

## 3. Boundaries and what a compromise buys

| Compromised component | Can reach | Cannot reach (by design) |
|---|---|---|
| **Agent process in guest** | Guest OS with admin-equivalent control, guest Docker/Compose, nested VMs where supported, `/workspace`, allowlisted egress, and only configured guestfwd endpoints | Host filesystem outside the workspace, general host loopback/network routes, host Docker, host LAN |
| **A guest workload container** | Guest Docker networks/volumes and explicitly mounted guest paths | Host filesystem outside the selected workspace, host Docker, host LAN |
| **A nested VM** | Its explicitly shared guest paths and its own virtual devices | Host filesystem outside the outer workspace export, host Docker, host LAN |
| **The outer VM kernel** | Guest processes and the exported workspace | Host kernel, host files outside the export, host Docker, host credentials |
| **The host user** | Everything (they own the machine) | — the harness does not pretend to protect the host from its owner |

The selected workspace is the intentional host-data boundary and the outer VM is the
execution boundary. The agent is intentionally trusted with broad control inside that guest;
guest root or a Docker daemon is not treated as an additional host security boundary. A
compromised agent can corrupt the workspace it was pointed at; that is the authority granted
by choosing it. The design blocks ordinary guest routes to the host/LAN, but it is not a
mathematical guarantee against vulnerabilities in QEMU/KVM, virtiofs, the host kernel, or
other host components. Live checks are bounded evidence, not a proof against every breakout.

## 4. Target lifecycle

The VM primitives currently live in standalone `./lab-vm`; merging them into `./lab` and
launching the agent there remain ticket #41 A2. The intended integrated lifecycle is:

1. `./lab-vm up --workspace <dir>` validates the path on the host, resolves the pinned base image, and
   creates `.mester/vm/<name>/` (overlay, seed, keys, sockets; gitignored).
2. `qemu-system-x86_64` starts with KVM, the virtiofs device, SSH on a loopback ephemeral
   port, model/egress `guestfwd` entries, and `restrict=on`.

The concrete QEMU wiring is `vhost-user-fs-pci` with shareable memfd-backed guest RAM and
the mount tag `workspace`. `guestfwd` targets use QEMU's per-connection `cmd:` character
device form (`socat` to host loopback); a bare `-10.0.2.2:PORT` target is invalid QEMU syntax.
The egress target is a loopback-only launcher proxy that rejects private/reserved addresses
and applies `.mester-egress`; it is not a general route to host loopback.
3. cloud-init provisions the guest user, direct harness runtime, optional guest workload
   runtime (Docker/Compose), native
   auth prerequisites, proxy settings and the workspace mount.
4. `./lab-vm agent [name]` currently attaches the selected harness directly in the guest
   (not in a container) to the invoking terminal. The target everyday entry point is a
   single `./lab <workspace>` command that reuses/starts the prepared VM and enters the
   guest TUI directly; provisioning and diagnostic commands remain explicit satellites.
   The host-Docker workflow remains available as `./lab-docker`. This transition is gated by
   #35/#52 and has not happened yet.
   Guest-native provider login and browser callback forwarding are tracked in #45.
5. The agent controls guest Docker/Compose for project services and tests. It may launch
   nested Little Mester VMs when nested virtualization is available and verified.
6. The lifecycle can stop guest workloads and the outer VM. VM-state retention/deletion is
   an explicit lifecycle choice; recursive test sessions must not be silently destroyed.

The current `lab-vm stop` and `lab-vm down` are destructive to guest state; they are not the
target end-of-day operation. A resumable, non-destructive stop/restart path and command-by-
command retention guarantees are required before the VM workflow is recommended for daily
use (#41/#52).

The Little Mester checkout is not part of the default workspace export. Explicit recursion
may expose it; self-modification additionally requires `--unsafe-recurse`. Both recursive
modes must enforce a propagated maximum depth, defaulting to zero, and use a separate
disposable child workspace/state. These are target requirements tracked by #52, not claims
that recursive VM execution already works.

## 5. Deliberate non-goals

- Protecting the host from its owner, or from a malicious host kernel/driver.
- Running the agent on the host. The host runs QEMU/`virtiofsd`, not the agent.
- GPU passthrough into the VM. Models stay on the host behind a narrow API.
- Perfect workspace confidentiality from the VM: virtiofs is a shared filesystem by design.
- A remote shared Docker daemon as the default. Remote-target mode is #47 and is documented
  as weaker isolation.
- Alternative runtimes. They are evaluated in #46, not smuggled in here.

## 6. Use cases, support contract, and portability

### Use cases

| Use case | Required capability | Must not depend on |
|---|---|---|
| Run an agent with broad project access inside a host-isolated guest | Supported VM backend, one guarded workspace share, SSH/control, native guest agent | Host Docker, a host agent container, host credentials or general host routing |
| Test a project with services such as PostgreSQL/Redis | Optional workload runtime inside the guest (Docker/Compose is the first supported implementation) | The host's Docker daemon/socket or host-side Compose |
| Use a local model | Optional configured OpenAI-compatible host endpoint, reached through one explicit guestfwd mapping | A particular host Docker installation, published LAN port, or auto-managed host model container |
| Use hosted model APIs / approved package registries | Explicit provider credentials in the guest and mediated, configured egress | Unrestricted guest networking or implicit firewall edits |
| Work without a model service | Direct agent/runtime and project shell tools | A model server being installed or started by `lab-vm` |

Guest Docker/Compose is a **workload capability**, not the agent's wrapper or security
boundary. It may be installed in the guest for the project-testing use case, but the core
VM/agent launch path must not require the Docker CLI on the host. Host Docker may be one
user-chosen way to run a local model server, but it is not part of the VM contract.

### Host portability contract

“Unix” is not one virtualization ABI. A usable profile needs working implementations for
all of: hypervisor/acceleration, safe single-directory sharing, loopback-only control and
guestfwd networking, guest image architecture, and setup/package detection. The project
must therefore advertise explicit **tested host profiles**, not claim every Unix host works
because QEMU happens to build there. Unknown hosts must fail with missing capabilities and
supported alternatives; they must not silently fall back to weaker isolation or ask users
to change firewall/routing policy.

The current implementation is an **experimental Linux x86_64 profile**: QEMU, KVM when
available, a Linux guest, `virtiofsd`, cloud-init, and QEMU user networking. Linux distro
variation is handled by setup detection, but the VM acceptance gates remain incomplete.
macOS (Intel/Apple Silicon) and BSD are not currently supported claims: each needs its own
guest architecture/image, acceleration, workspace-sharing, and lifecycle acceptance tests.
TCG, where available, is a performance choice and is not evidence that the security or
workspace contract works on an otherwise unsupported host.

The intended setup flow is host-adapter based:

1. Detect OS, architecture, existing VM tools and available accelerators.
2. Explain the selected support profile and required userland packages; ask before any host
   package installation. Never install a daemon/service or alter firewall, NAT, routing,
   sysctl, or host Docker configuration as an implicit setup step.
3. Create a local config for the selected workspace, VM profile, optional guest workload
   runtime, and optional model endpoint. Defaults should work without a model endpoint.
4. Validate every host crossing before boot: exactly one selected workspace, loopback-only
   SSH, `restrict=on`, allowlisted egress, and—only when configured—one loopback model API
   port. Refuse startup if the host platform cannot enforce these invariants.

The configuration contract should separate independent choices rather than derive host
behavior from the development Compose stack:

```yaml
host_profile: auto                 # resolves to an installed, tested adapter
workload_runtime: docker-compose   # guest-only; also supports none for shell-only use
model:
  provider: none                   # none | local-openai-compatible | configured-remote
  local_port: 8080                 # optional host-loopback port, forwarded by QEMU
metrics: off                       # optional; must not be required to launch the agent
```

This is a design shape, not a committed config schema. The VM's guest address/port is a
stable logical endpoint; the host adapter maps it to the configured `127.0.0.1` service
using QEMU's supported `guestfwd` command target. It does not create a host listener,
publish a Docker port, or expose a host network. Remote providers are a separate explicit
egress mode: their domain must be approved by the egress policy and credentials remain in
the guest auth store. Metrics are optional and need a separately specified data source; the
agent must still start if metrics are disabled/unavailable.

Initial portability work should preserve the Linux profile while extracting interfaces for
VM lifecycle, workspace sharing, network forwarding, and model-provider configuration.
Additional Unix profiles are enabled only after CI/manual host evidence for each capability;
they do not get host-specific firewall workarounds.

## 6. Open items to prove in implementation

- `restrict=on` must still allow explicit `guestfwd` entries and SSH `hostfwd`. If a
  supported host cannot provide them without firewall/routing changes, that profile is
  unsupported; do not add a host firewall fallback.
- virtiofs mount/permission behaviour for the directly running guest agent and its
  guest-local workloads (#41–#43).
- Nested KVM availability and recursive VM lifecycle are unverified; host and guest CPU,
  kernel and QEMU configuration must prove the capability before it is advertised.
- Guest Docker/Compose behavior and the direct-agent execution model are implementation
  work; Docker is a guest workload tool, not the agent's security wrapper (#41–#43). Host
  Docker is not a VM prerequisite.
- DNS and non-HTTP egress: only the proxy path is allowed initially; anything else is a
  documented follow-up, not an accidental hole (#43).
- Remote host mode (#47) must repeat the same path guards, labels, teardown and egress
  guarantees, and state its weaker host-sharing model in `SECURITY.md`.
