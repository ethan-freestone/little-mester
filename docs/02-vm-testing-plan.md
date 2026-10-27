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

**What:** `./lab-vm test` — workspace guards, disk functions, ISO generation tooling.  
**Where:** Runs on any host, including non-KVM CI runners.  
**Status:** Implemented in CI (`.github/workflows/ci.yml` → `vm-tests` job).

```bash
# Run locally (any host):
./lab-vm test

# CI runs this automatically on every push/PR.
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

**What:** One-time creation of the shared base qcow2 image (~20 GiB).  
**Where:** Your KVM-capable Linux host (not the development container).  
**How often:** Once per host. The base disk is cached and reused across VM sessions.

### Step-by-step on your KVM host:

```bash
# 1. Ensure prerequisites are installed (see docs/01-host-setup.md)
#    Arch:  sudo pacman -S qemu-system-x86 qemu-utils ovmf virtiofsd cloud-utils
#    Debian: sudo apt install qemu-system-x86 ovmf virtiofsd cloud-image-utils genisoimage

# 2. Clone and enter the repo (if not already done)
git clone <this-repo> little-mester
cd little-mester

# 3. Create the base disk (ONE TIME, ~20 GiB download + install)
./lab-vm base
```

**Cache guidance:**
- **Use cache (default):** The base disk image is downloaded and cached. Subsequent `./lab-vm base` runs on the same host are fast (the base already exists).
- **`--no-cache` equivalent:** Delete the base disk (`rm ~/.mester/vm/base.qcow2`) to force a fresh download and install. Use this when the base image is suspected to be corrupted.

**Expected output:**
```
>>> Downloading cloud image...
>>> Creating base qcow2 disk (20 GiB)...
>>> Installing cloud-init packages...
>>> Base disk created: ~/.mester/vm/base.qcow2
```

---

## Phase 3: VM lifecycle tests (KVM host)

**What:** End-to-end tests of VM provisioning, agent execution, and teardown.  
**Where:** Your KVM-capable Linux host.  
**How often:** Each time you iterate on VM code changes.

### 3a. Provision and start:

```bash
# Start a new VM (idempotent — safe to re-run)
./lab-vm up
```

**Cache guidance:**
- **Use cache (default):** Docker images inside the VM are cached across sessions.
- **No cache inside VM:** SSH in and manually rebuild: `./lab-vm ssh` then `docker build --no-cache -t mester-agent .`

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

# Base disk is preserved — free ~20 GiB only if you want to
# rm ~/.mester/vm/base.qcow2
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
| **Base disk creation** (recreate) | No cache | `rm ~/.mester/vm/base.qcow2 && ./lab-vm base` |
| **VM up** (normal day) | Cache (Docker images cached in VM) | `./lab-vm up` |
| **VM up** (after base disk rebuild) | Cache (Docker images rebuild from cached layers) | `./lab-vm up` |
| **Fresh Docker build** inside VM | No cache | `./lab-vm ssh` then `docker build --no-cache -t mester-agent .` |
| **CI pipeline** | Always no-cache for base image | Handled by CI config |

---

## Isolation Boundaries (Recap)

| Component | Resource | Impact on ./lab if broken |
|---|---|---|
| `lab-vm` + QEMU | `/dev/kvm`, QEMU binaries, `~/.mester/vm/` | **None** — completely separate |
| `./lab` + Docker Compose | Docker daemon, `compose.yaml`, `./` | **None** — VM crashes don't touch Docker |
| Workspace mount | Shared directory (virtiofs / bind mount) | Both see the same files, but `lab`'s guard runs first |
| `docs/tickets/` | Read by both | Neither writes directly (both use CLI/MCP) |

---

## Acceptance Criteria Status (Ticket #41)

| AC | Status | Evidence |
|---|---|---|
| A1: Documented runbook | Implemented | `docs/01-host-setup.md` |
| A2: VM lifecycle commands in ./lab | In-progress | `lab-vm` standalone, merge pending |
| A3: check_workspace() on host | Implemented | Self-tests pass (Test 1-6) |
| A4: No published host ports | Pending KVM | Requires live network audit on real host |
| A5: Model access via guestfwd | Pending KVM | Requires live test on real host |
| A6: Teardown leaves clean workspace | Pending KVM | Requires git status check on real host |
| A7: verify-lab.sh passes in VM | Pending KVM | Requires running inside a real VM |
| A8: Host no longer runs agent container | Pending KVM | Requires proof that Docker host is idle |

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
   ./lab-vm base      # One-time, ~20 GiB
   ./lab-vm up        # Provision and start
   ./lab-vm status    # Verify running
   ./lab-vm ssh       # Enter the VM
   # ... test little-mester behavior ...
   ./lab-vm stop      # Full teardown
   ./lab-vm down      # Remove artifacts
   ```

4. **If `./lab-vm up` fails:**
   - `./lab` is **NOT affected** — your Docker workflow continues normally
   - Check QEMU log: `cat ~/.mester/vm/default/qemu.log`
   - Kill orphaned processes: `pkill -u $(whoami) qemu-system-x86_64; pkill -u $(whoami) virtiofsd`
   - Fix code, commit, push, pull on host, retry
