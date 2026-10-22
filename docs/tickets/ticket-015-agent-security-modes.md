# TICKET-015: Agent security modes — Container, Sysbox, and Incus

**Status**: OPEN
**Priority**: HIGH
**Date**: 2026-10-22
**Related**: TICKET-014 (Docker-in-Docker failure), DECISION-011 (Docker-in-Docker rejected)

---

## Context

The agent needs to run nested containers for its work (testing, building, isolated toolchains). Three security modes are possible. The current setup uses mode 1 (Container) which has no Docker-in-Docker.

## Mode 1: Container Mode (current)

### What it is
The agent runs in a standard Docker container with the existing compose setup. No Docker-in-Docker. The agent can run any binary in its image but **cannot launch new containers**.

### Security boundary
```
┌──────────────────────────────────────────────────┐
│                    HOST                           │
│                                                   │
│  ┌─────────────────────────────────────────────┐  │
│  │  Docker container (agent)                    │  │
│  │  - cap_drop: ALL                             │  │
│  │  - no-new-privileges:true                    │  │
│  │  - read_only: true                           │  │
│  │  - /workspace bind mount only                │  │
│  │  - tmpfs: /tmp, /home/agent                  │  │
│  │  - no Docker daemon inside                   │  │
│  └─────────────────────────────────────────────┘  │
│                                                   │
│  ┌─────────────┐  ┌──────────────┐               │
│  │ ollama      │  │ llamacpp     │               │
│  │ (internal)  │  │ (internal)   │               │
│  └─────────────┘  └──────────────┘               │
└──────────────────────────────────────────────────┘
```

### Setup
- Already working. `./lab <workspace>` launches the agent in its container.
- No host-level changes needed.

### Pros
- ✅ Already working
- ✅ Zero host dependency
- ✅ Simplest setup
- ✅ No workflow change
- ✅ Strong security boundary for what the agent can do

### Cons
- ❌ Agent cannot run Docker containers
- ❌ Agent is limited to binaries compiled into its image
- ❌ Cannot run arbitrary test harnesses that need container isolation

### Security rating
| Aspect | Score | Notes |
|--------|-------|-------|
| Host isolation | ⭐⭐⭐⭐⭐ | Agent can only access `/workspace` |
| Privilege escalation | ⭐⭐⭐⭐⭐ | No caps, no-new-privs, read-only |
| Container escape risk | ⭐⭐⭐⭐⭐ | Minimal attack surface, no Docker daemon |
| Agent flexibility | ⭐⭐ | Limited to pre-installed tools |

---

## Mode 2: Sysbox Mode

### What it is
Replace `runc` with `sysbox-runc` as the container runtime. Sysbox is a user-space container runtime that creates a proper user namespace boundary, allowing containers to run Docker-in-Docker safely — **without** privileged mode.

### Security boundary
```
┌──────────────────────────────────────────────────┐
│                    HOST                           │
│                                                   │
│  ┌─────────────────────────────────────────────┐  │
│  │  Docker container (agent, sysbox-runc)       │  │
│  │  - sysbox creates isolated user ns           │  │
│  │  - cap_drop: ALL (still applies)             │  │
│  │  - /workspace bind mount only                │  │
│  │  - dockerd runs inside, fully nested         │  │
│  │  - nested containers get their own user ns   │  │
│  └─────────────────────────────────────────────┘  │
│                                                   │
│  ┌─────────────┐  ┌──────────────┐               │
│  │ ollama      │  │ llamacpp     │               │
│  │ (internal)  │  │ (internal)   │               │
│  └─────────────┘  └──────────────┘               │
└──────────────────────────────────────────────────┘
```

### Setup (~15 minutes)

1. Install sysbox on the host:
```bash
# Arch/CachyOS — check if package exists, otherwise use repo:
sudo pacman -S sysbox     # or follow https://nxospace.com/docs/installation
# Alternative: add nxis repository and install sysbox-runc
```

2. Configure Docker to use sysbox-runc as a runtime:
```bash
sudo tee /etc/docker/daemon.json <<'EOF'
{
  "runtimes": {
    "sysbox-runc": {
      "path": "runc"
    },
    "sysbox": {
      "runtime": "sysbox-runc"
    }
  }
}
EOF
sudo systemctl restart docker
```

3. Update `compose.yaml` agent service:
```yaml
agent:
  # ... existing config ...
  x-runc-runtime: sysbox-runc   # or use runtime: sysbox if supported
```

4. Update `agent/Dockerfile` to include Docker CLI:
```dockerfile
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates curl && \
    install -m 0755 /tmp/docker.tgz /usr/local/bin/docker && \
    rm -tmp/docker.tgz
```

5. Remove `cap_add: SYS_ADMIN` and `seccomp=unconfined` from any leftover config.

### Pros
- ✅ Agent can run Docker containers
- ✅ Nested containers are user-namespaced (can't touch host)
- ✅ Same compose workflow — no workflow change
- ✅ Host Docker is NOT reachable from nested containers
- ✅ Model servers (ollama, llamacpp) still work
- ✅ ~15 min setup, one host package

### Cons
- ❌ Requires host-level package installation
- ❌ Sysbox is not an Arch package by default — may need to add repo or build
- ❌ Sysbox has known edge cases with certain kernel features
- ❌ User-namespace boundary is strong but not as strong as a VM

### Security rating
| Aspect | Score | Notes |
|--------|-------|-------|
| Host isolation | ⭐⭐⭐⭐ | User-namespace boundary; nested containers can't reach host Docker |
| Privilege escalation | ⭐⭐⭐⭐ | sysbox-runc restricts what's visible to the inner container |
| Container escape risk | ⭐⭐⭐ | Sysbox has had CVEs in the past; actively maintained though |
| Agent flexibility | ⭐⭐⭐⭐⭐ | Full Docker-in-Docker, run any container |

### Docker-in-Docker inside Sysbox
Sysbox provides a Docker-compatible environment inside the container. The agent runs `dockerd` as PID 1 inside its container, and nested containers get their own user namespace. The host Docker daemon is **not** shared. Nested containers see a fake `/proc/1` that doesn't expose host processes.

---

## Mode 3: Incus VM Mode

### What it is
Move the agent out of Docker entirely and into an Incus VM. Model servers stay in Docker compose on the host. The agent VM runs nested Docker inside the VM for its container needs. Network is restricted to the Incus bridge.

### Security boundary
```
┌──────────────────────────────────────────────────────────────────┐
│                        HOST                                     │
│                                                                   │
│  ┌────────────────────────┐  ┌──────────────┐                   │
│  │  Docker compose        │  │  ollama      │                   │
│  │  llamacpp + metrics    │  │  (internal)  │                   │
│  └────────────────────────┘  └──────────────┘                   │
│                                                                   │
│  incusbr0 (10.x.x.1) — Incus bridge                                │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │  VM "agent-vm" (separate kernel)                            │  │
│  │  - 6 CPU, 12GB RAM, 60GB disk                               │  │
│  │  - Docker daemon inside VM                                  │  │
│  │  - git, opencode, all tools in VM                           │  │
│  │  - talks to host only via: http://10.x.x.1:11434 (llamacpp)│  │
│  │  - NO host filesystem access                                │  │
│  └─────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

### Setup (~45-60 minutes)

1. Install Incus on host:
```bash
sudo pacman -S incus
sudo systemctl enable --now incus.socket
sudo usermod -aG incus-admin $USER && newgrp incus-admin
incus admin init --minimal
```

2. Launch the agent VM:
```bash
incus launch images:ubuntu/24.04 agent-vm \
  -c limits.cpu=6 \
  -c limits.memory=12GiB \
  -d root,size=60GiB
```

3. Install inside the VM:
```bash
incus exec agent-vm -- bash
# Inside VM:
apt-get update && apt-get install -y docker.io git nodejs npm curl
npm i -g @opencode/cli
```

4. Configure host firewall so the VM can only reach llamacpp and metrics:
```bash
# Allow only llamacpp API and metrics from the VM
sudo ufw allow from 10.x.x.0/24 to any port 11434 proto tcp
sudo ufw allow from 10.x.x.0/24 to any port 19532 proto tcp
sudo ufw enable
```

5. Republish llamacpp on the bridge IP:
```yaml
# compose.yaml llamacpp service — change network binding
ports:
  - "10.114.15.1:11434:11434"   # bridge IP, not localhost
```

6. Update `./lab` with VM wrappers:
```bash
# lab vm start|stop|shell|export-patch
```

### How the agent works
- Agent clones the repo inside the VM (never mounts host filesystem).
- Works on a branch, produces patches.
- Pull results out with `incus exec agent-vm -- git -C /home/agent/repo format-patch main --stdout > review.patch`.
- Review and apply on host.
- Snapshots before risky runs: `incus snapshot create agent-vm clean`.

### Pros
- ✅ Strongest security boundary — separate kernel
- ✅ Agent can run Docker containers (nested in VM)
- ✅ Host Docker is completely unreachable (no shared daemon, no shared namespaces)
- ✅ No host filesystem exposure — git patch workflow enforces boundary
- ✅ Can snapshot/rollback
- ✅ True isolation: a kernel exploit in the VM can't affect the host

### Cons
- ❌ ~45-60 min setup
- ❌ Larger disk usage (60GB VM + Docker images)
- ❌ Workflow change: no bind mounts, use patch workflow
- ❌ Agent leaves compose ecosystem
- ❌ Network configuration more complex (bridge IP, firewall rules)
- ❌ GPU access for agent (if ever needed) requires GPU passthrough, which is complex

### Security rating
| Aspect | Score | Notes |
|--------|-------|-------|
| Host isolation | ⭐⭐⭐⭐⭐ | Separate kernel, no shared namespaces, no host mounts |
| Privilege escalation | ⭐⭐⭐⭐⭐ | VM-level boundary; even root in VM can't touch host |
| Container escape risk | ⭐⭐⭐⭐ | Kernel boundary is strong; VM escape requires hypervisor vuln |
| Agent flexibility | ⭐⭐⭐⭐⭐ | Full Docker-in-VM, run any container |

---

## Comparison summary

| Aspect | Container (current) | Sysbox | Incus VM |
|--------|--------------------|--------|----------|
| **Setup time** | None (already running) | ~15 min | ~45-60 min |
| **Host package needed** | None | sysbox-runc | incus |
| **Agent can run Docker** | ❌ No | ✅ Yes | ✅ Yes |
| **Nested container isolation** | N/A | User-namespace | Separate kernel |
| **Host Docker reachable** | N/A | ❌ No | ❌ No |
| **Host filesystem reachable** | Only /workspace | Only /workspace | ❌ No (patch workflow) |
| **GPU for agent** | ❌ No | ❌ No (indirect) | ❌ No (no passthrough) |
| **Snapshot/rollback** | ❌ No | ❌ No | ✅ Yes |
| **Workflow change** | None | Minimal | Patch-based |
| **Security level** | Strong | Stronger | Strongest |
| **CVE history** | Minimal (just Docker) | Several (sysbox-runc) | Minimal (kernel + Incus) |
| **Maintenance burden** | Low | Medium | Medium-High |

## Recommendation path

1. **Container mode** (current) — use today, close your laptop, keep shipping.
2. **Sysbox** — try next if you need Docker-in-Docker. ~15 min, same workflow.
3. **Incus VM** — build only if Sysbox won't install or you need the strongest boundary.

## Open questions

- Does sysbox support the NVIDIA container toolkit for GPU passthrough into nested containers? (Not needed today — models stay on host.)
- What's the current Incus package name on CachyOS? (`incus` vs `incus-edge`?)
- Should the patch workflow include a `./lab vm import-patch` helper?
- How do we handle secrets/credentials in the VM mode? (SSH keys, API tokens, etc.)
