# Host Setup — VM Mode (Epic #35)

> **Last updated:** 2026-10-27  
> **Target:** A clean Linux host bootstrapped to run the disposable QEMU/KVM VM that hosts the agent sandbox.  
> **Epic:** [#35](docs/tickets/ticket-035.json) — VM-based Docker agent environment.  
> **Architectural reference:** [`docs/vm-architecture.md`](docs/vm-architecture.md), [`docs/DECISIONS.md` §C.8](docs/DECISIONS.md#c8-vm-harness-topology-transport-and-isolation-2026-10-26).

---

## 1. Prerequisites

### 1.1 Required packages (Arch/CachyOS)

```bash
sudo pacman -S --needed \
  qemu-system-x86 \
  qemu-utils \
  qemu-arch-extra \
  edk2-ovmf \
  libvirt \
  virtiofsd \
  cloud-utils \
  openssh \
  docker \
  docker-compose-v2
```

### 1.2 Required packages (Debian/Ubuntu)

```bash
sudo apt-get install -y \
  qemu-system-x86 \
  qemu-utils \
  ovmf \
  libvirt-clients \
  libvirt-daemon-system \
  cloud-image-utils \
  genisoimage \
  openssh-server \
  docker.io \
  docker-compose-v2
```

### 1.3 Kernel requirements

- `kvm-intel` or `kvm-amd` module loaded (check with `lsmod | grep kvm`)
- `/dev/kvm` exists and is readable by the current user
- If KVM is not available (CPU-only host), QEMU falls back to TCG emulation — slower but functional

```bash
ls -la /dev/kvm
# Should show: crw-rw---- 1 root:kvm ... /dev/kvm
```

Add your user to the `kvm` group if needed:

```bash
sudo usermod -aG kvm $(whoami)
# Log out and back in for group membership to take effect
```

### 1.4 Model servers (host-side)

Ollama and/or llama.cpp model servers should be running on the host before starting the VM. They are reachable from inside the VM through `guestfwd` ports — **not** through any published host port.

```bash
# Example: run ollama on host
ollama serve &

# Example: run llama.cpp on host
./llama-server -m models/your-model.gguf -p 8080
```

### 1.5 Verify QEMU works

```bash
# Basic check
qemu-system-x86_64 --version

# KVM check
qemu-system-x86_64 -enable-kvm -machine q35 -m 512 -drive file=/dev/null,format=raw \
  -nographic -display none 2>&1 | head -5
# Should start without errors (press Ctrl+A, X to exit)
```

---

## 2. One-command bootstrap

After installing prerequisites, run:

```bash
git clone <this-repo> little-mester
cd little-mester

# Create the base disk image (one-time, ~20 GiB)
./lab vm base

# Start your first VM session
./lab vm up
```

The `vm up` command:

1. Validates the workspace path (refuses `/`, `$HOME`, sensitive dirs, the lab repo without `--unsafe-recurse`)
2. Creates per-VM SSH keys under `~/.mester/vm/<name>/`
3. Creates a qcow2 overlay disk backed by the shared base image
4. Generates a cloud-init seed ISO with Docker installation and rootless configuration
5. Starts QEMU with KVM (or TCG fallback), virtiofs workspace export, and `guestfwd` for model APIs
6. Waits for SSH to become available
7. Builds the agent image from the workspace's `Dockerfile` (if present)
8. Confirms Docker is running inside the VM

**CPU-only hosts:** The same command works. You'll see:

```
WARNING: /dev/kvm not available — running in TCG (CPU emulation) mode
```

This is expected. The VM will be slower but fully functional.

---

## 3. Everyday use

### Start / stop / inspect

```bash
./lab vm up                  # Start the VM (idempotent)
./lab vm status              # Check if running, show containers
./lab vm ssh                 # SSH into the VM
./lab vm down                # Stop and remove the VM
./lab vm stop                # Teardown containers + stop VM
```

### Multi-VM support

Named VMs are isolated under `~/.mester/vm/<name>/`:

```bash
./lab vm up project-x        # Create a named VM
./lab vm status project-x    # Check state
./lab vm down project-x      # Remove it
```

### Workspace selection

Pass a workspace directory:

```bash
./lab vm up --workspace /path/to/your/project
```

The `check_workspace()` guard runs on the host **before** the workspace is exported to the VM. Refused paths are never mounted.

---

## 4. Isolation guarantees

| Guarantee | How it's enforced |
|---|---|
| **No published host ports** | QEMU uses `restrict=on`; only `guestfwd` and `hostfwd` for loopback SSH are allowed |
| **Workspace isolation** | Only the single directory passed to `./lab` is exported via `virtiofsd` |
| **No host filesystem access** | VM root filesystem is read-only; workspace is the only writable mount |
| **Model API via guestfwd** | Ollama (`:11434`) and llama.cpp (`:8080`) are reachable at `10.0.2.100` from inside the VM |
| **Egress proxy** | HTTP(S) outbound goes through `10.0.2.100:3128` (allowlist enforcement — #43) |
| **Rootless Docker** | Docker daemon runs as user `mester` inside the VM; containers cannot escape to VM root |
| **Per-VM SSH keys** | Each VM has its own key pair under `~/.mester/vm/<name>/` |

---

## 5. Teardown and cleanup

```bash
# Full teardown: containers + VM
./lab vm stop

# Remove VM artifacts (overlay disk, seed ISO, keys)
./lab vm down

# Delete the base image (free ~20 GiB, next vm up will recreate it)
rm ~/.mester/vm/base.qcow2
```

After teardown, `git status --short` in the real workspace checkout should be empty (A6).

---

## 6. Troubleshooting

### QEMU fails to start

```bash
# Check QEMU log
cat ~/.mester/vm/<name>/qemu.log

# Check if KVM is available
ls -la /dev/kvm
# If missing: sudo modprobe kvm-intel (or kvm-amd)
```

### SSH not ready

```bash
# Check QEMU log for errors
tail -50 ~/.mester/vm/<name>/qemu.log

# Wait longer and retry
./lab vm up   # Re-running is safe (idempotent)
```

### Docker not ready inside VM

```bash
# SSH in and check
./lab vm ssh

# Inside the VM:
docker info
# If socket doesn't exist, cloud-init may still be running
journalctl -u cloud-init --no-pager -n 50
```

### Port conflicts

If the SSH ephemeral port range (20000–30000) has conflicts, the script picks the lowest available port. You can check:

```bash
ss -tlnH | grep -E ':2[0-9]{4} '
```

---

## 7. Migration from host-Docker mode

If you've been using the current host-Docker mode:

1. **Stop the old setup:** `./lab stop`
2. **Install VM prerequisites** (Section 1)
3. **Create base disk:** `./lab vm base`
4. **Start VM:** `./lab vm up`
5. The host no longer runs the agent container (A8). All execution is inside the VM.
6. Existing `compose.yaml` and workspace files work unchanged — they're now managed by the in-VM rootless Docker daemon.

---

## 8. References

- **Architecture:** [`docs/vm-architecture.md`](docs/vm-architecture.md)
- **Decisions:** [`docs/DECISIONS.md` §C.8](docs/DECISIONS.md#c8-vm-harness-topology-transport-and-isolation-2026-10-26)
- **Security:** [`SECURITY.md`](SECURITY.md)
- **Epic:** [#35](docs/tickets/ticket-035.json)
- **Provisioning:** [#41](docs/tickets/ticket-041.json) (this document satisfies A1 and A8)
