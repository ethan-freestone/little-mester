# Host Setup — VM Mode (Epic #35)

> **Last updated:** 2026-10-28
> **Target:** A clean Linux host bootstrapped to run the disposable QEMU/KVM VM that hosts the agent sandbox.  
> **Epic:** [#35](tickets/ticket-035.json) — VM-based Docker agent environment.
> **Architectural reference:** [`vm-architecture.md`](vm-architecture.md), [`DECISIONS.md` §C.8](DECISIONS.md#c8-vm-harness-topology-transport-and-isolation-2026-10-26).

---

## 1. Prerequisites

### 1.1 Required packages (Arch/CachyOS)

```bash
sudo pacman -S --needed \
  qemu-system-x86 \
  qemu-img \
  virtiofsd \
  cdrtools \
  openssh \
  socat \
  curl \
  python
```

### 1.2 Required packages (Debian/Ubuntu)

```bash
sudo apt-get install -y \
  qemu-system-x86 \
  qemu-utils \
  virtiofsd \
  genisoimage \
  openssh-client \
  socat \
  curl \
  python3
```

Host Docker, libvirt, and OVMF are not required. `lab-vm` launches QEMU directly with its default firmware, and cloud-init installs Docker plus the Compose plugin inside the guest.

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

Ollama and/or llama.cpp may run on the host. They are reachable from inside the VM through narrow `guestfwd` ports — **not** through published agent ports. The model servers do not need to be running when QEMU starts.

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

If the agent will use a ChatGPT Plus or Pro subscription, preserve existing auth using the host credential escrow procedure in [the auth runbook](chatgpt-auth.md). Browser/non-device-code login is a required #45 acceptance gate; only legacy device login has been verified so far. Automatic import into a disposable VM is not implemented yet; do not assume a login stored in the current container will follow the VM.

**VM acceptance status:** The host lifecycle is still under test; the latest
failure and current retry procedure are tracked on ticket #41. Follow that
ticket's current handoff before using `--clean`, `stop`, or `down` (these can
delete guest state). The VM is not integrated into `./lab`, and a successful
`lab-vm up` alone is not an integrated-agent acceptance pass.

After installing prerequisites, run:

```bash
git clone <this-repo> little-mester
cd little-mester

# Download and verify the base image (roughly 600 MiB; 20 GiB virtual size)
./lab-vm base

# Start your first VM session
./lab-vm up --workspace /path/to/project
```

The `vm up` command:

1. Validates the workspace path (refuses `/`, `$HOME`, sensitive dirs, the lab repo without `--unsafe-recurse`)
2. Creates per-VM SSH keys under this checkout's `.mester/vm/<name>/`
3. Downloads the pinned Ubuntu 24.04 cloud image, verifies its SHA-256, and creates a qcow2 overlay
4. Generates a secret-free cloud-init seed ISO with rootless Docker provisioning
5. Starts the loopback egress proxy, virtiofsd, and QEMU with KVM (or TCG fallback)
6. Waits for SSH to become available
7. Confirms the guest Docker daemon is running rootless
8. Installs the pinned OpenCode CLI and syncs its config/plugins into the guest OS (no agent container)

**CPU-only hosts:** The same command works. You'll see:

```
WARNING: /dev/kvm not available — running in TCG (CPU emulation) mode
```

This is expected. The VM will be slower but fully functional.

---

## 3. Everyday use

### Start / stop / inspect

```bash
./lab-vm up                  # Start the VM (idempotent)
./lab-vm status              # Check if running, show containers
./lab-vm agent               # Run OpenCode directly in the guest TUI
./lab-vm ssh                 # SSH into the VM
./lab-vm down                # Stop and remove the VM
./lab-vm stop                # Teardown containers + stop VM
```

### Multi-VM support

Named VMs are isolated under this repository's `.mester/vm/<name>/`:

```bash
./lab-vm up project-x        # Create a named VM
./lab-vm status project-x    # Check state
./lab-vm down project-x      # Remove it
```

### Workspace selection

Pass a workspace directory:

```bash
./lab-vm up --workspace /path/to/your/project
```

The `check_workspace()` guard runs on the host **before** the workspace is exported to the VM. Refused paths are never mounted.

---

## 4. Isolation guarantees

| Guarantee | How it's enforced |
|---|---|
| **No published host ports** | QEMU uses `restrict=on`; only `guestfwd` and `hostfwd` for loopback SSH are allowed |
| **Workspace isolation** | Only the single directory passed to `./lab-vm` is exported via `virtiofsd` |
| **No host filesystem access** | The guest has an isolated writable qcow2 overlay; the selected workspace is its only host filesystem export |
| **Model API via guestfwd** | Ollama (`:11434`) and llama.cpp (`:8080`) are reachable at `10.0.2.100` from inside the VM |
| **Egress proxy** | HTTP(S) outbound goes through `10.0.2.100:3128` (allowlist enforcement — #43) |
| **Guest authority** | OpenCode runs directly as `mester`; it can use guest Docker/Compose and administer the guest. The outer VM is the host boundary. |
| **Per-VM SSH keys** | Each VM has its own key pair under `.mester/vm/<name>/` |

---

## 5. Teardown and cleanup

```bash
# Destructive: stop guest workloads and remove the VM overlay/state
./lab-vm stop

# Remove VM artifacts (overlay disk, seed ISO, keys)
./lab-vm down

# Rebuild the verified base image when needed
./lab-vm rebuild --yes
```

After teardown, `git status --short` in the real workspace checkout should be empty (A6).

---

## 6. Troubleshooting

### Existing base disk is empty or invalid

Early VM code created a blank 20 GiB qcow2 instead of downloading an OS. Replace it once:

```bash
./lab-vm rebuild --yes
```

`up --yes` also replaces a detected legacy blank base automatically. The download is roughly 600 MiB; 20 GiB is its virtual expanded size.

### QEMU fails to start

```bash
# Check QEMU log
cat .mester/vm/<name>/qemu.log

# Check if KVM is available
ls -la /dev/kvm
# If missing: sudo modprobe kvm-intel (or kvm-amd)
```

### SSH not ready

```bash
# Check QEMU log for errors
tail -50 .mester/vm/<name>/qemu.log

# Wait longer and retry
./lab-vm up   # Re-running is safe (idempotent)
```

### Docker not ready inside VM

```bash
# SSH in and check
./lab-vm ssh

# Inside the VM:
docker info
# If socket doesn't exist, cloud-init may still be running
journalctl -u cloud-init --no-pager -n 50
```

To start the direct guest agent rather than a shell, use `./lab-vm agent`. This requires
the VM to be running and attaches the OpenCode TUI to the current terminal.

### `Could not open guest forwarding device`

This was caused by the old invalid destination `guestfwd=...-10.0.2.2:PORT`. The runner now uses QEMU's `cmd:` character-device form with `socat`, so optional host model services need not be listening when QEMU starts. Confirm the current command includes `guestfwd=...-cmd:socat` with `--debug`.

### SSH port diagnostics

The kernel assigns an available loopback port for SSH. The selected port is printed during startup and stored in the VM profile:

```bash
cat .mester/vm/<name>/ssh_port
ss -tlnp | grep "$(cat .mester/vm/<name>/ssh_port)"
```

---

## 7. Migration status

VM mode is still under acceptance testing. **Do not stop or replace the working
host-Docker session yet.** The VM provisions guest Docker and `./lab-vm up` installs
OpenCode directly in the guest. Run `./lab-vm agent` to open its TUI; this command checks
the install and launches OpenCode without reinstalling it. TUI operation, provider auth,
Compose feedback, permissions, nested virtualization and full lifecycle gates still need
live verification.
The current sequence and stop conditions are tracked on tickets #41–#45. Integration into
`./lab` and retiring host-Docker mode happen only after those gates pass.

---

## 8. References

- **Architecture:** [`vm-architecture.md`](vm-architecture.md)
- **ChatGPT authentication:** [`docs/chatgpt-auth.md`](chatgpt-auth.md)
- **Decisions:** [`DECISIONS.md` §C.8](DECISIONS.md#c8-vm-harness-topology-transport-and-isolation-current)
- **Security:** [`../SECURITY.md`](../SECURITY.md)
- **Epic:** [#35](tickets/ticket-035.json)
- **Provisioning:** [#41](tickets/ticket-041.json) (this document satisfies A1 and A8)
