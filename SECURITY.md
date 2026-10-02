# Security Model & Architecture

> **Core Principle:** The AI agent (`little-mester`) operates strictly inside a hardened Docker container. It **never** has shell access to the host machine. All security boundaries are enforced by Docker and Linux kernel features, not by trust in the AI's output.

## 1. Host vs. Container Boundary
```
┌─────────────────────────────────────────────────────────────────┐
│                        HOST (Your Machine)                     │
│                                                                 │
│  ┌──────────────┐    ┌──────────────────────────────────────┐  │
│  │   ./lab      │───►│  Docker Daemon (root-owned)          │  │
│  │  (Launcher)  │    │                                      │  │
│  └──────────────┘    │  ┌────────────────────────────────┐  │  │
│                      │  │  agent container               │  │  │
│                      │  │  - non-root user               │  │  │
│                      │  │  - read-only root fs           │  │  │
│                      │  │  - cap_drop: ALL               │  │  │
│                      │  │  - /workspace (bind mount)     │  │  │
│                      │  └────────────────────────────────┘  │  │
│                      └──────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## 2. Network Isolation
- **Internal Network:** The `agent` and `ollama` services share an `internal: true` Docker network. They can communicate via `http://ollama:11434`, but have **zero** route to the internet or host LAN.
- **Egress Only for Setup:** The `ollama-pull` service is the *only* container with internet access, and it runs exclusively during `./lab setup`. It is removed immediately after models are downloaded.
- **No Published Ports:** No ports are mapped to the host (`ports: []`). The AI cannot initiate outbound connections or listen for inbound ones.

## 3. Filesystem Restrictions
- **Single Bind Mount:** Only one directory from the host is mounted into the container: `/workspace`. This is explicitly chosen by the user via `./lab <path>`.
- **Read-Only Root FS:** The container's root filesystem is mounted read-only (`read_only: true`). It can only write to `/tmp` and `/home/agent` (via `tmpfs`), which are ephemeral and cleared on restart.
- **No Sensitive Host Paths:** `$HOME`, `$HOME/.ssh`, `$HOME/.config`, Docker socket, and the `little-mester` repo itself are explicitly blocked by `check_workspace` in the launcher script.

## 4. Privilege & Capability Hardening
- **Non-Root User:** The container runs as a dedicated non-root user (`agent`). UID/GID are passed from the host to ensure file ownership matches.
- **Capability Drop:** `cap_drop: [ALL]` removes all Linux capabilities (e.g., `NET_ADMIN`, `SYS_PTRACE`). The process cannot manipulate network stacks, trace other processes, or escalate privileges.
- **No New Privileges:** `security_opt: ["no-new-privileges:true"]` prevents any child process from gaining elevated permissions via setuid/setgid binaries.

## 5. Resource Limits
- **Memory:** `mem_limit: 4g` prevents the agent from exhausting host RAM.
- **PIDs:** `pids_limit: 512` prevents fork bombs or runaway processes inside the container.

## 6. Security Policy & Immutability
**This sandbox's security posture is intentionally rigid.** 
- The AI agent (`little-mester`) is **never** granted shell access to the host. Interaction occurs strictly via the mounted workspace directory or the container's internal shell (`./lab shell`).
- **Security configurations are immutable without explicit user review.** Any modification to `compose.yaml`, `agent/Dockerfile`, or `lab` launcher security flags requires manual inspection and confirmation by the human operator. The AI will never auto-modify security boundaries.
- If you need to change security settings, you must do so manually in the source files and re-run `./verify-sandbox.sh`.

## 7. What This Does NOT Protect Against
See [README.md#what-this-does-not-protect-against](README.md#what-this-does-not-protect-against) for limitations (e.g., workspace corruption, host-side script execution, container escape via kernel vulns).
