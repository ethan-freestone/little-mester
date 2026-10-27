# VM harness architecture and trust boundary

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
3. cloud-init creates `mester`, installs Docker/compose and configures rootless mode,
   proxy settings and the workspace mount.
4. `lab-vm` builds the agent image in the guest. Starting that agent through the integrated
   `./lab` VM command is still pending A2.
5. The agent runs `docker compose` for the workspace; containers are labelled with the
   workspace path and project name.
6. `./lab-vm stop` runs the workspace/agent teardown, stops QEMU cleanly, and removes the
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
