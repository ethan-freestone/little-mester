# Security Model & Architecture

> **Core principle:** The agent (`little-mester`) runs in a hardened Docker container and
> **never** gets shell access to the host. Docker and Linux kernel features enforce the
> boundary; the model's output is not trusted.
>
> **Two modes, one boundary:** the current implementation runs the agent container on the
> host Docker daemon. The target harness (epic #35) runs it inside a disposable local
> QEMU/KVM VM with **rootless** Docker. Both keep the same workspace-wide boundary. The
> full target design is in [`docs/vm-architecture.md`](docs/vm-architecture.md) and
> [`docs/DECISIONS.md` §C.8](docs/DECISIONS.md#c8-vm-harness-topology-transport-and-isolation-2026-10-26).

## 1. Host vs. container boundary

```
Current (host-Docker mode)
┌───────────────────────────── HOST ─────────────────────────────┐
│  ./lab ──► Docker daemon (root-owned)                          │
│              └─ agent container (non-root, read-only, no caps) │
│                   └─ /workspace (one bind mount)                │
└────────────────────────────────────────────────────────────────┘

Target (epic #35, in progress)
┌───────────────────────────── HOST ─────────────────────────────┐
│  ./lab ──► QEMU/KVM + virtiofsd + model API + egress proxy     │
│              └─ disposable VM                                   │
│                   └─ rootless Docker (user mester)              │
│                        ├─ agent container (non-root, no caps)   │
│                        └─ workspace containers (optional)       │
│                             └─ /workspace (virtiofs export)     │
└────────────────────────────────────────────────────────────────┘
```

The VM is the execution boundary; the workspace is the data boundary. In both modes the
agent sees exactly one selected directory, not the host filesystem.

## 2. Network isolation and egress

- **Agent egress is enabled by default.** `compose.yaml` puts the agent on
  `[egress, internal]`; the `egress` network is not `internal`. This is the current
  behaviour, not an opt-in.
- **Disable it explicitly:** `./lab --isolated` (or `--egress false`) sets the agent to
  `[internal]` only. `.mester-egress` with `permission=false` is the file-based equivalent.
- **`.mester-egress` `allowed_domains` is not enforced yet.** It is parsed and recorded,
  but a non-empty list does not currently restrict traffic. Enforcement is the job of
  #43 via the host-side allowlisting proxy described in §C.8; until then, treat the list
  as documentation, not a control.
- **Setup pulls use egress too.** `ollama-pull` and `llamacpp-pull` run only during
  `./lab setup` and use the `egress` network; they are removed afterwards.
- **No published ports.** No service maps a port to the host. Agent and workspace services
  are reachable only on Docker's internal networks. The target VM adds one loopback-only
  SSH control forward (`127.0.0.1:<ephemeral>`) and nothing else.

## 3. Filesystem restrictions

- **One workspace mount.** Only the directory chosen with `./lab <path>` is mounted, at
  `/workspace`. The target VM exports that same directory with virtiofs; `virtiofsd` is
  started with that single shared directory.
- **Path guards run on the host first.** `check_workspace()` refuses `/`, `$HOME`,
  `$HOME/.ssh`, `$HOME/.config`, credential directories, and the little-mester repo itself
  unless `--unsafe-recurse` is explicit. A refused path is never exported to the VM.
- **Read-only root filesystem.** The agent container uses `read_only: true`; writes are
  limited to `/tmp`, `/home/agent` and `/aider-data` tmpfs mounts, which are ephemeral.
- **No host Docker socket today.** The current agent has no Docker access. The target
  gives it the **in-VM rootless** socket only when Docker access is explicitly enabled
  (#42); the host Docker socket is never mounted. Extra host paths require the recorded
  approval flow in #43.

## 4. Privilege and capability hardening

- **Non-root agent.** The container runs as the invoking UID/GID (`LAB_UID`/`LAB_GID`), so
  workspace ownership matches the host user.
- **Capabilities dropped.** `cap_drop: [ALL]` removes `NET_ADMIN`, `SYS_PTRACE` and the rest;
  `security_opt: ["no-new-privileges:true"]` blocks setuid/setgid escalation.
- **Rootless Docker in the VM.** The target daemon runs as the dedicated `mester` user
  (`subuid`/`subgid`, systemd lingering). A container escape lands as an unprivileged VM
  user, not VM root. Rootful Docker is an explicit escape hatch, never the default.
- **No privileged containers in the target harness.** Workspace compose files will run
  through a generated override that forbids `privileged`, host network/PID/IPC, and bind
  mounts outside the workspace (#43). Today the agent has no Docker access at all.

## 5. Resource limits

- **Memory:** `mem_limit: 8g` on the agent container (older docs said 4 GiB; that was
  wrong).
- **PIDs:** `pids_limit: 512` prevents fork bombs and runaway process trees.
- The model servers have their own GPU reservations and named model volumes; they publish
  no ports.

## 6. Security policy and immutability

- **The agent never gets host shell access.** Interaction happens through the mounted
  workspace, the container's internal shell (`./lab shell`), and — in the target mode — the
  in-VM rootless Docker socket when explicitly enabled.
- **Security configuration is immutable without review.** Changes to `compose.yaml`,
  `agent/Dockerfile`, `lab`, the VM boundary, or the egress model require explicit human
  review and a ticket with evidence. The agent must not silently widen a boundary.
- **The boundary is tested.** `./verify-lab.sh` covers sandbox assertions, and
  `scripts/check-docs-consistency.py` fails when this document or `README.md` drifts from
  the compose defaults again.

## 7. What this does NOT protect against

- **Workspace destruction.** The agent can delete or corrupt everything in the selected
  directory, `.git` included. Keep backups or push before a session.
- **Host scripts you run later.** Git hooks, `Makefile`, `package.json` scripts, `.envrc`
  and shell scripts written by the agent run with *your* privileges if you execute them on
  the host. Review first.
- **Kernel/runtime escapes.** The current mode shares the host kernel; a kernel or runtime
  vulnerability could break out. The VM harness (#35) raises this bar but is not a
  security-audited product.
- **Remote-target mode (#47).** A remote Linux host shares that host's Docker daemon and is
  weaker isolation than the local VM. It is opt-in and must say so in its own docs.
- **Bad output.** A sandboxed agent can still write wrong or insecure code. Review it.

See [`README.md#what-this-does-not-protect-against`](README.md#what-this-does-not-protect-against)
and [`docs/vm-architecture.md`](docs/vm-architecture.md) for the boundary diagram and
crossing table.
