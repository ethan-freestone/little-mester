# VM Mode Testing Plan (Epic #35 / Ticket #41)

> **Last updated:** 2026-10-27  
> **Status:** Development — self-tests pass, KVM evidence pending  
> **Reference:** [`docs/DECISIONS.md` §C.8](DECISIONS.md#c8-vm-harness-topology-transport-and-isolation-2026-10-26)

---

## Design Philosophy: Isolate, Prove, Merge

```
lab-vm (standalone)  ──proven──►  CI validates  ──proven──►  Merge into ./lab
  (zero dep on lab)              self-tests pass            remove lab-vm
```

**Why standalone first?** A broken VM build must NEVER kill `./lab`. By keeping `lab-vm`
completely independent during development, you can iterate on VM code without risking the
host-Docker workflow that's already working.

---

## Phase 1: Self-tests (CI — done ✓)

**What:** `./lab-vm test`, `bats tests`, and `python3 tests/test_vm_proxy.py` — workspace guards, CLI parsing, QEMU arguments, disk/ISO generation, and proxy policy.
**Where:** Runs on any host, including non-KVM CI runners.  
**Status:** Implemented in CI (`.github/workflows/ci.yml` → `vm-tests` job).

```bash
# Run locally (any host):
./lab-vm test

bats tests
python3 tests/test_vm_proxy.py
# CI runs these automatically on every push/PR.
```

**Expected output:**
```
=== lab-vm self-tests ===
Test 1: check_workspace rejects / ... PASS
Test 2: check_workspace rejects $HOME ... PASS
Test 3: check_workspace rejects $HOME/.ssh ... PASS
Test 4: check_workspace accepts a safe directory ... PASS
Test 5: check_workspace accepts a sibling directory ... PASS
Test 6: check_workspace with --unsafe-recurse accepts repo ... PASS
Test 7: qemu-img create/delete works ... [PASS|SKIP]
Test 8: cloud-init ISO generation ... [PASS|SKIP]

---- 6+ passed, 0 failed ----
```

---

## Phase 2: Base disk creation (KVM host)

**What:** One-time download and verification of the shared Ubuntu qcow2 image (roughly 600 MiB download, resized to 20 GiB virtual capacity).
**Where:** Your KVM-capable Linux host (not the development container).  
**How often:** Once per host. The base disk is cached and reused across VM sessions.

### Step-by-step on your KVM host:

```bash
# 1. Ensure prerequisites are installed (see docs/01-host-setup.md)
#    Arch/CachyOS: ./lab-vm -y base can install detected missing packages
#    Debian/Ubuntu: ./lab-vm -y base can install detected missing packages

# 2. Clone and enter the repo (if not already done)
git clone <this-repo> little-mester
cd little-mester

# 3. Download, checksum, and convert the base disk (ONE TIME)
./lab-vm base
```

**Cache guidance:**
- **Use cache (default):** The base disk image is downloaded and cached. Subsequent `./lab-vm base` runs on the same host are fast (the base already exists).
- **Fresh base:** Run `./lab-vm rebuild --yes`. State is repository-local under `.mester/vm/`.

**Expected output:**
```
Downloading pinned Ubuntu 24.04 cloud image...
Verified bootable base disk: .../.mester/vm/base.qcow2 (20 GiB virtual)
```

---

## Phase 3: VM lifecycle tests (KVM host)

**What:** End-to-end tests of VM provisioning, agent execution, and teardown.  
**Where:** Your KVM-capable Linux host.  
**How often:** Each time you iterate on VM code changes.

### 3a. Provision and start:

```bash
# Start a new VM (idempotent — safe to re-run)
./lab-vm up --clean --debug --unsafe-recurse --auto-accept
```

**Cache guidance:**
- Re-running `up` while the VM is running is idempotent.
- `stop`/`down` deletes the writable overlay, so a later fresh VM rebuilds the agent image; only the verified Ubuntu base is retained.

### 3b. Verify state:

```bash
./lab-vm status
# Should show: running (PID: XXXX, SSH: 2XXXXX)
```

### 3c. SSH into the VM:

```bash
./lab-vm ssh
# You're now inside the VM as the 'mester' user
docker info          # Should work (rootless Docker)
pwd                  # Should show /workspace
```

### 3d. Test isolation — the critical test:

```bash
# Inside the VM, test that little-mester behavior works WITH --unsafe-recurse:
cd /workspace
./lab --unsafe-recurse "Create a test file called /tmp/proof.txt with the content 'VM-isolation-works'"
```

**This is the acceptance criterion for testing little-mester within little-mester VM world.**
The agent runs inside the VM, operates on the workspace, and should create the file.
With `--unsafe-recurse`, the agent can also modify the little-mester repo itself — which
is the whole point: testing the self-editing capability.

### 3e. Verify the agent's work:

```bash
# Inside the VM (or from host via ./lab-vm ssh):
cat /tmp/proof.txt
# Should show: VM-isolation-works

# Check the workspace is clean after teardown:
git status --short
# Should be empty (or show only expected changes from the test)
```

### 3f. Teardown:

```bash
# Full teardown: containers + VM
./lab-vm stop

# Remove all VM artifacts (overlay, seed ISO, keys)
./lab-vm down

# Base disk is preserved; use ./lab-vm rebuild --yes only when replacing it
```

---

## Phase 4: Integration testing with multiple agents (KVM host)

**What:** Test little-mester behavior with different agent configurations inside the VM.

```bash
./lab-vm up
./lab-vm ssh
# Inside VM:
./lab --agent opencode "Write a test suite for auth"
./lab --agent aider "Refactor the config parsing"
# Verify each agent's output is correct
./lab-vm stop
./lab-vm down
```

**What to verify:**
- Both opencode and aider can be launched inside the VM
- Agent commits have correct git identity
- `--workflow tdd` and `--workflow regression` profiles work
- `--isolated` flag properly restricts egress
- `--ro` / `--rw` mount mode works

---

## Phase 5: Merge into `./lab` (only after Phase 4 passes)

**Prerequisites:**
- [ ] Phase 1: CI self-tests pass on every push
- [ ] Phase 2: Base disk creates successfully on your KVM host
- [ ] Phase 3: Full lifecycle (up → ssh → test → stop → down) works end-to-end
- [ ] Phase 4: Multiple agents work inside the VM
- [ ] Acceptance evidence collected and attached to ticket #41

**Merge steps:**
1. Add `vm` subcommand to `./lab` (forward to `lab-vm` initially)
2. Add `--vm-mode` flag to `./lab` for the default workflow switch
3. Update `./lab --help` to include `vm up/status/ssh/down/stop/base`
4. Remove standalone `lab-vm` (its functionality is now in `./lab`)
5. Update `docs/01-host-setup.md` to use `./lab vm` instead of `./lab-vm`
6. Update `docs/DECISIONS.md` §C.6 to reference the merged path
7. Update ticket #41 status to `completed`

---

## Cache vs No-Cache: Quick Reference

| Scenario | Cache? | Command |
|---|---|---|
| **Base disk creation** (first time) | N/A (nothing to cache yet) | `./lab-vm base` |
| **Base disk creation** (recreate) | No cache | `./lab-vm rebuild --yes` |
| **VM up** (already running) | Existing guest | `./lab-vm up` |
| **VM up** (after `stop`/`down`) | Fresh overlay | `./lab-vm up` |
| **Fresh agent image** | Automatic in fresh overlay | `./lab-vm up --clean` |
| **CI pipeline** | Always no-cache for base image | Handled by CI config |

---

## Isolation Boundaries (Recap)

| Component | Resource | Impact on ./lab if broken |
|---|---|---|
| `lab-vm` + QEMU | `/dev/kvm`, QEMU binaries, repository-local `.mester/vm/` | **None** — completely separate |
| `./lab` + Docker Compose | Docker daemon, `compose.yaml`, `./` | **None** — VM crashes don't touch Docker |
| Workspace mount | Shared directory (virtiofs / bind mount) | Both see the same files, but `lab`'s guard runs first |
| `docs/tickets/` | Read by both | Neither writes directly (both use CLI/MCP) |

---

## Acceptance Criteria Status (Ticket #41)

| AC | Status | Evidence |
|---|---|---|
| A1: Documented runbook | **passing** | `docs/01-host-setup.md` |
| A2: VM lifecycle commands in ./lab | **in-progress** | `lab-vm` standalone, merge pending |
| A3: check_workspace() on host | **passing** | Self-tests 1-6 pass |
| A4: No published host ports | pending | Requires live network audit on real host |
| A5: Model access via guestfwd | **in-progress** | QEMU 10.0.13 accepts the corrected `cmd:socat` arguments; live host connectivity pending |
| A6: Teardown leaves clean workspace | pending | Requires git status check on real host |
| A7: verify-lab.sh passes in VM | pending | Requires running inside a real VM |
| A8: Host no longer runs agent container | **in-progress** | VM mode stops host container by design |
| A9: VM-world self-testing with --unsafe-recurse | pending | Requires KVM host test (Phase 3d) |

---

## Notes for the Developer Running in This Container

This container **does not have KVM**. You cannot run `./lab-vm up` here. Here's how to work:

1. **While in this container:**
   - Run self-tests: `./lab-vm test` (pass ✓)
   - Run CI: `git push` → CI will run `./lab-vm test` with full prereqs
   - Edit `lab-vm` and `lib/vm/*` code — changes are committed here
   - Review code, run ShellCheck: `shellcheck lab-vm lib/vm/*.sh`

2. **When you push to the repo:**
   - CI runs `./lab-vm test` on ubuntu-latest (with qemu-utils, genisoimage, virtiofsd)
   - If CI passes, the code is safe for KVM host testing

3. **On your KVM-capable host (when ready):**
   ```bash
   git pull
   ./lab-vm base      # One-time, roughly 600 MiB download / 20 GiB virtual
   ./lab-vm up --clean --debug --unsafe-recurse --auto-accept
   ./lab-vm status    # Verify running
   ./lab-vm ssh       # Enter the VM
   # ... test little-mester behavior ...
   ./lab-vm stop      # Full teardown
   ./lab-vm down      # Remove artifacts
   ```

4. **If `./lab-vm up` fails:**
   - `./lab` is **NOT affected** — your Docker workflow continues normally
   - Check QEMU log: `cat .mester/vm/default/qemu.log`
   - Run `./lab-vm down` to clean managed QEMU, virtiofsd, proxy, PID, and socket state
   - Fix code, commit, push, pull on host, retry

---

## Troubleshooting

### VM won't start — virtiofsd socket not ready
```bash
# Check if virtiofsd is running
ps aux | grep virtiofsd
# If stale, kill and retry
pkill -u $(whoami) virtiofsd
./lab-vm down && ./lab-vm up
```

### SSH into VM fails with "connection refused"
The VM may still be booting. Cloud-init can take 30-60 seconds to configure SSH.
```bash
# Wait and retry
sleep 30 && ./lab-vm ssh
```

### Docker socket path mismatch inside VM
If you see errors like `/run/user//docker.sock` (empty UID), this is the `host_uid` expansion bug.
It was fixed in this commit. On older versions, manually fix:
```bash
# Inside the VM, verify the UID is correct
id -u mester  # should match your host UID
```

### Empty workspace causes VM startup failure
If no `--workspace` is specified, the VM now defaults to `$(pwd)`.
If you need a specific workspace:
```bash
./lab-vm up --workspace /path/to/your/project
```

### Base disk corrupted
```bash
./lab-vm rebuild --yes
```

### Orphaned QEMU processes after crash
The cleanup trap handles EXIT/INT/TERM automatically. For SIGKILL:
```bash
pkill -9 -u $(whoami) qemu-system-x86_64
pkill -9 -u $(whoami) virtiofsd
```
