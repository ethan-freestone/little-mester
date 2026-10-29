# Security Model & Architecture

> **Core principle:** The agent never gets a host shell or host Docker socket. Current
> `./lab` host-Docker mode runs in a hardened container; experimental `./lab-vm` runs
> OpenCode directly inside a local QEMU/KVM guest. The model's output is not trusted.
>
> **Two modes:** the current `./lab` runs an agent container on the host Docker daemon.
> The experimental `./lab-vm` runs the agent directly in a local QEMU/KVM guest; guest
> rootless Docker is for project workloads, not the agent wrapper or VM security boundary.
> Both are intended to expose only an explicitly selected workspace to host data. The
> full target design is in [`docs/vm-architecture.md`](docs/vm-architecture.md) and
> [`docs/DECISIONS.md` §C.8](docs/DECISIONS.md#c8-vm-harness-topology-transport-and-isolation-2026-10-26).

## 1. Execution boundaries

```
Current (host-Docker mode)
┌───────────────────────────── HOST ─────────────────────────────┐
│  ./lab ──► Docker daemon (root-owned)                          │
│              └─ agent container (non-root, read-only, no caps) │
│                   └─ /workspace (one bind mount)                │
└────────────────────────────────────────────────────────────────┘

Experimental VM mode (epic #35, in progress)
┌───────────────────────────── HOST ─────────────────────────────┐
│  ./lab-vm ──► QEMU/KVM + virtiofsd + model API + egress proxy  │
│              └─ disposable VM                                   │
│                   ├─ OpenCode runs directly as guest user mester│
│                   ├─ rootless Docker/Compose (project workloads) │
│                   └─ /workspace (one virtiofs export)           │
└────────────────────────────────────────────────────────────────┘
```

The VM is the execution boundary; the selected workspace is the intentional host-data
boundary. In VM mode the agent can control the guest OS and guest Docker, but should have no
host filesystem access outside that export, no host Docker socket, and no general host/LAN
route. A small number of explicit guestfwd channels are described below. These are required
capabilities, not proof against a QEMU/KVM or host-kernel vulnerability.

## 2. Network isolation and egress

- **Agent egress is enabled by default.** `compose.yaml` puts the agent on
  `[egress, internal]`; the `egress` network is not `internal`. This is the current
  behaviour, not an opt-in.
- **Disable it explicitly:** `./lab --isolated` (or `--egress false`) sets the agent to
  `[internal]` only. `.mester-egress` with `permission=false` is the file-based equivalent.
- **Host-Docker mode:** `.mester-egress` `allowed_domains` is not enforced for the
  container's network; treat it as documentation, not a control. `--isolated` disables
  container egress.
- **VM mode:** QEMU uses `restrict=on`. Direct guest Internet and general host-gateway
  access are blocked; approved HTTP(S) egress goes through the host loopback proxy, which
  enforces the selected workspace's `.mester-egress` domain list and rejects private,
  reserved, link-local, multicast, and mixed public/non-public DNS results. This control is
  separate from host-Docker egress and remains subject to the pending live acceptance gates
  in #41/#43.
- **Host-Docker setup pulls use egress too.** `ollama-pull` and `llamacpp-pull` run only
  during `./lab setup` and use the `egress` network; they are removed afterwards.
- **Host-Docker mode:** services do not publish ports to the host; agent/workspace
  services use Docker networks.
- **VM mode:** QEMU's SSH control port is bound to host loopback. Narrow QEMU `guestfwd`
  channels target only the explicitly configured egress proxy, local-model API and metrics
  API. Optional model API publishing must bind to `127.0.0.1`, never the host LAN. Native
  browser auth temporarily adds one host-loopback callback port forwarded to guest
  loopback; it is removed when the auth SSH control master exits. The full lifecycle and
  live boundary gates remain incomplete.

## 3. Filesystem restrictions

- **One workspace mount.** Host-Docker mounts only the directory chosen with
  `./lab <path>` at `/workspace`. VM mode exports only that selected directory with virtiofs;
  `virtiofsd` is started with that single shared directory. The agent can read/write this
  selected workspace, so use backups and do not select sensitive directories.
- **Path guards run on the host first.** `check_workspace()` refuses `/`, `$HOME`,
  `$HOME/.ssh`, `$HOME/.config`, credential directories, and the little-mester repo itself
  unless `--unsafe-recurse` is explicit. A refused path is never exported to the VM.
- **Host-Docker read-only root filesystem.** The agent container uses `read_only: true`;
  writes are limited to `/tmp`, `/home/agent` and `/aider-data` tmpfs mounts, which are
  ephemeral.
- **No host Docker socket.** Host-Docker mode currently has no Docker socket mounted.
  VM-mode OpenCode runs as `mester` and can control the **in-VM rootless** Docker daemon for
  workloads; this is guest authority, not an additional host boundary. The host Docker
  socket is never mounted. Extra host paths are not implicitly exposed.

## 4. Privilege and capability hardening

- **Host-Docker non-root agent.** The container runs as the invoking UID/GID
  (`LAB_UID`/`LAB_GID`), so workspace ownership matches the host user.
- **Host-Docker capabilities dropped.** `cap_drop: [ALL]` removes `NET_ADMIN`, `SYS_PTRACE`
  and the rest; `security_opt: ["no-new-privileges:true"]` blocks setuid/setgid escalation.
- **Rootless Docker in the VM.** The guest daemon runs as dedicated user `mester`
  (`subuid`/`subgid`, systemd lingering). Guest workloads are constrained by the guest
  kernel/daemon and outer VM boundary; rootless Docker is not treated as a host-isolation
  guarantee. Compose compatibility and nested-VM behavior remain under #42/#43.

## 5. Resource limits

- **Host-Docker mode:** `mem_limit: 8g` and `pids_limit: 512` apply to the agent
  container. They do not describe VM guest resource limits.
- Host-Docker model services publish no ports by default. Optional VM-only local model
  forwarders are separately selected and bind host ports to loopback only.

## 6. Security policy and immutability

- **The agent never gets host shell access.** Host-Docker interaction uses the mounted
  workspace and container shell (`./lab shell`). VM-mode OpenCode runs directly in the
  guest and can control its in-VM rootless Docker/Compose workload runtime; it does not
  receive the host Docker socket or a host command capability.
- **Security configuration is immutable without review.** Changes to `compose.yaml`,
  `agent/Dockerfile`, `lab`, the VM boundary, or the egress model require explicit human
  review and a ticket with evidence. The agent must not silently widen a boundary.
- **The host-Docker boundary is tested.** `./verify-lab.sh` covers container assertions, and
  `scripts/check-docs-consistency.py` fails when this document or `README.md` drifts from the
  compose defaults again.

## 7. What this does NOT protect against

- **Workspace destruction.** The agent can delete or corrupt everything in the selected
  directory, `.git` included. Keep backups or push before a session.
- **Host scripts you run later.** Git hooks, `Makefile`, `package.json` scripts, `.envrc`
  and shell scripts written by the agent run with *your* privileges if you execute them on
  the host. Review first.
- **Kernel/hypervisor escapes.** Host-Docker mode shares the host kernel. VM mode adds a
  QEMU/KVM boundary, but a QEMU, KVM, virtiofs, or host-kernel vulnerability could still
  permit escape. The VM harness is not a security-audited product; no finite smoke test
  proves that every escape is impossible. The implementation must keep the VM boundary,
  `restrict=on`, narrow guestfwd allowlist, loopback-only SSH/model/callback listeners, and
  single-workspace export intact, and report any live acceptance gate as pending until
  measured on the owner KVM host.
- **Remote-target mode (#47).** A remote Linux host shares that host's Docker daemon and is
  weaker isolation than the local VM. It is opt-in and must say so in its own docs.
- **Bad output.** A sandboxed agent can still write wrong or insecure code. Review it.

See [`README.md#what-this-does-not-protect-against`](README.md#what-this-does-not-protect-against)
and [`docs/vm-architecture.md`](docs/vm-architecture.md) for the boundary diagram and
crossing table.
