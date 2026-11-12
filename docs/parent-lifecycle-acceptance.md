# Owner-run persistent-parent M1a acceptance

**Owner action only.** This procedure installs trusted code and starts a real VM. The
agent must not run it. The owner-reported QEMU/qemu-img scratch PASS ×2 is compatibility
evidence only. NVIDIA timing remains separately pending.

## Owner evidence log

- Candidate `24d307089c3931687ea3c07bcc6fa51a303e3be1` started the persistent parent in
  approximately 9 seconds (`run_id=1e65b748de8d76ca27daa92eac1b7cbe`). The owner reports
  that it remains RUNNING across multiple SSH connections and SSH authentication succeeds.
- **M1a remains pending:** after the Ubuntu shell prompt appears, typed commands are echoed
  but produce no visible execution/output (including `pwd` and `echo hello`). Diagnose the
  interactive SSH I/O path before continuing lifecycle/persistence acceptance. No agent-side
  VM interaction or acceptance is implied by this report.
- Offline production-path probe: the pinned `parent_ssh_plan()` and
  `_run_interactive_ssh()` successfully execute a harmless shell arithmetic command over a
  real local `sshd` and controlling PTY (repeated three times). This verifies host-side PTY
  allocation, inherited terminal descriptors, and shell input/output in the fixture; it
  does not reproduce the owner guest or identify its runtime cause. No SSH policy change is
  justified by this evidence. The next distinction requires owner-approved guest-side
  observation of shell execution and terminal state.

### Next owner diagnostic: capture one harmless PTY round-trip

Using the **same protected installed candidate that started the still-running parent**, open
a fresh local terminal and record one SSH session to a mode-0600 file. This reconnects only;
do not run `up`, `stop`, or `recover` for this check.

```bash
umask 077
LOG="$HOME/.local/state/little-mester/parent-ssh-pty.typescript"
install -d -m 0700 "$HOME/.local/state/little-mester"
script --quiet --flush --command "$INSTALL_ROOT/lab-vm parent ssh" "$LOG"
```

At the guest prompt, type `echo $((19+23))` and press Enter, then type `exit`. A shell that
executes the command should display a separate `42` output line (the echoed command itself
does not contain `42`). Report whether that line appeared. If not, report the terminal
emulator and local `TERM`, plus a sanitized excerpt showing the echoed line and any following
output; do not share credentials or unredacted private logs. Keep the transcript private.

## 1. Install the reviewed candidate

Use the existing protected deployment mechanism documented in
[`docs/vm-v3-phase1-smoke.md`](vm-v3-phase1-smoke.md#owner-only-execution-procedure):
`tools/vm-owner-acceptance --install`. Do not execute `lab-vm` from a mutable checkout,
use `sudo lab-vm`, manually copy files, or add a development-mode bypass.

From a clean owner clone containing the final reviewed commit:

```sh
set -euo pipefail
REPO="$HOME/Development/little-mester-m1a"
CANDIDATE_SHA='24d307089c3931687ea3c07bcc6fa51a303e3be1'
cd "$REPO"
test "$(git -C "$REPO" rev-parse "$CANDIDATE_SHA^{commit}")" = "$CANDIDATE_SHA"
test -z "$(git -C "$REPO" status --porcelain)"
tools/vm-owner-acceptance --install --sha "$CANDIDATE_SHA" --repo "$REPO"
INSTALL_ROOT="/opt/little-mester/v3/$CANDIDATE_SHA"
namei -l "$INSTALL_ROOT/lab-vm" \
  "$INSTALL_ROOT/lib/vm/parent-launcher.py" \
  "$INSTALL_ROOT/lib/vm/parent-supervisor.py" \
  "$INSTALL_ROOT/agent/opencode/plugins/little-mester-exit-confirm/tui.tsx"
"$INSTALL_ROOT/lab-vm" preflight
```

The existing installer streams an exact allowlisted Git archive to its protected worker,
installs under a new SHA-specific `/opt/little-mester/v3/` path, rejects symlinks and
unexpected entries, enforces root-owned non-writable ancestry and file modes, verifies
every installed byte against the pinned Git commit, then runs installed read-only
preflight. Its inventory includes the parent modules and protected plugin assets. It
does not run smoke or start a VM. It writes a private acceptance report outside the
checkout and changes neither the development checkout, legacy VM, nor parent VM data.

`--install` refuses to overwrite an existing SHA path. If it already exists, use
`tools/vm-owner-acceptance --preflight --sha "$CANDIDATE_SHA" --repo "$REPO"` and
proceed only on PASS. Preserve a partial or failed installation for owner inspection;
never chmod, overwrite, delete, or create a parallel launcher. A different reviewed SHA
uses a new path and leaves prior installations untouched. The persistent parent data is
separate user-owned state at `~/.local/state/little-mester/parent-v3/default`.

## 2. Preflight and competing-writer checks

```sh
"$INSTALL_ROOT/lab-vm" parent status --json
```

An existing provisioned parent must report validated **STOPPED**. Fresh state may report
**NOT_CREATED** or **UNPROVISIONED**. Stop on **STARTING**, **RUNNING**, or **AMBIGUOUS**.
Do not change existing state.

If the disk exists, this read-only device/inode scan must print no open-writer candidate:

```sh
DISK="$HOME/.local/state/little-mester/parent-v3/default/disk.qcow2"
if [[ -e "$DISK" ]]; then
  python3 - "$DISK" <<'PY'
import glob, os, sys
disk = os.stat(sys.argv[1])
unreadable = []
for proc in glob.glob('/proc/[0-9]*'):
    pid = proc.rsplit('/', 1)[-1]
    try:
        owner = os.stat(proc).st_uid
    except (FileNotFoundError, ProcessLookupError):
        continue
    if owner not in (0, os.getuid()):
        continue
    try:
        descriptors = list(os.scandir(proc + '/fd'))
    except (FileNotFoundError, ProcessLookupError):
        continue
    except PermissionError:
        unreadable.append(pid)
        continue
    for descriptor in descriptors:
        fd = descriptor.path
        try:
            opened = os.stat(fd)
            if (opened.st_dev, opened.st_ino) == (disk.st_dev, disk.st_ino):
                try:
                    exe = os.path.basename(os.readlink(proc + '/exe'))
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    exe = 'unavailable'
                print(f'open disk writer candidate: pid={pid} exe={exe}')
        except (FileNotFoundError, ProcessLookupError):
            pass
        except PermissionError:
            unreadable.append(pid)
if unreadable:
    print('cannot verify open descriptors for PIDs: ' + ','.join(sorted(set(unreadable))))
    raise SystemExit(2)
PY
fi
```

Stop for inspection if a writer is reported. Refuse stale QMP/proxy artifacts without
removing them:

```sh
STATE_DIR="$HOME/.local/state/little-mester/parent-v3/default"
for artifact in qmp.sock proxy.port; do
  if [[ -e "$STATE_DIR/$artifact" || -L "$STATE_DIR/$artifact" ]]; then
    echo "REFUSED: inspect existing parent artifact $STATE_DIR/$artifact" >&2
    exit 2
  fi
done
```

Check a prior SSH-port listener with:

```sh
if [[ -e "$STATE_DIR/ssh_port" || -L "$STATE_DIR/ssh_port" ]]; then
  if [[ ! -f "$STATE_DIR/ssh_port" || -L "$STATE_DIR/ssh_port" ]]; then
    echo "REFUSED: ssh_port is not a regular non-symlink file" >&2
    exit 2
  fi
  PORT="$(cat "$STATE_DIR/ssh_port")"
  if [[ ! "$PORT" =~ ^[0-9]{1,5}$ ]]; then
    echo "REFUSED: ssh_port is not a decimal TCP port" >&2
    exit 2
  fi
  PORT=$((10#$PORT))
  if (( PORT < 1024 || PORT > 65535 )); then
    echo "REFUSED: ssh_port is outside the valid unprivileged TCP port range" >&2
    exit 2
  fi
  command -v ss >/dev/null
  LISTENERS="$(ss -H -ltn)"
  if awk -v p="$PORT" '$4 ~ (":" p "$") { print; found=1 } END { exit found ? 0 : 1 }' <<<"$LISTENERS"; then
    echo "REFUSED: listener remains on prior parent SSH port $PORT" >&2
    exit 2
  else
    AWK_STATUS=$?
    if (( AWK_STATUS != 1 )); then
      echo "REFUSED: could not reliably inspect prior parent SSH port $PORT" >&2
      exit 2
    fi
  fi
fi
```

Stop if any check fails. Use capacity 64 for a fresh parent; for an existing STOPPED disk
use its current `disk.virtual_capacity_gib` (up does not resize).

## 3. Up, SSH disconnect/reconnect, duplicate-up

```sh
CAPACITY=64
"$INSTALL_ROOT/lab-vm" parent up --capacity-gib "$CAPACITY"
"$INSTALL_ROOT/lab-vm" parent status --json
"$INSTALL_ROOT/lab-vm" parent ssh
```

Expect RUNNING with a validated run ID. In the guest shell:

```sh
cd /home/mester/workspace
printf 'm1a-persistence-canary\n' > .phase1-parent-canary
cat .phase1-parent-canary
exit
```

Confirm the same run remains RUNNING after disconnect, then reconnect and read the canary:

```sh
"$INSTALL_ROOT/lab-vm" parent status --json
"$INSTALL_ROOT/lab-vm" parent ssh
cd /home/mester/workspace
cat .phase1-parent-canary
exit
```

While RUNNING, invoke up again with the same capacity and inspect status:

```sh
"$INSTALL_ROOT/lab-vm" parent up --capacity-gib "$CAPACITY"
"$INSTALL_ROOT/lab-vm" parent status --json
```

Expect the same run ID and disk identity; no second writer. Stop if another run appears
or identity differs.

## 4. Stop, re-up, verify persistence, stop again

```sh
"$INSTALL_ROOT/lab-vm" parent stop
"$INSTALL_ROOT/lab-vm" parent status --json
"$INSTALL_ROOT/lab-vm" parent up --capacity-gib "$CAPACITY"
"$INSTALL_ROOT/lab-vm" parent status --json
"$INSTALL_ROOT/lab-vm" parent ssh
cd /home/mester/workspace
cat .phase1-parent-canary
exit
"$INSTALL_ROOT/lab-vm" parent stop
"$INSTALL_ROOT/lab-vm" parent status --json
```

Expect STOPPED after each stop, then RUNNING after re-up with the same disk device/inode,
a fresh run ID, and unchanged canary. This M1a test is plain SSH; TUI/auth/history are M2.

Provisioning stages have individual bounds (download up to 310 seconds, image conversion
up to 600 seconds, readiness up to 900 seconds); there is no VM lifetime/idle timeout.
Stop on refusal, AMBIGUOUS status, unexpected listener/writer, identity mismatch, or
unverified cleanup. Preserve all state; do not manually signal QEMU, delete files or
repeat up. Stop waits 15 seconds by default; graceful QMP shutdown may continue up to 60
seconds. On timeout, inspect status before retrying.

Return candidate SHA/branch, clean-tree and install/preflight results, host QEMU versions,
status JSONs, disk/run identity comparisons, disconnect/reconnect, duplicate-up, canary,
stop/re-up results and any refusal output. M1a is not full M1 lifecycle acceptance.

## Separate bounded NVIDIA timing probe

This read-only probe does not start a VM. Return its output and all ten timings; it informs
M3 collector choice only.

```sh
set -eu
command -v nvidia-smi
timeout --signal=TERM --kill-after=1 5 \
  nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu,memory.total,memory.used,memory.free \
  --format=csv,noheader,nounits
for n in 1 2 3 4 5 6 7 8 9 10; do
  /usr/bin/time -f 'sample_wall_seconds=%e' timeout --signal=TERM --kill-after=1 5 \
    nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu,memory.total,memory.used,memory.free \
    --format=csv,noheader,nounits >/dev/null
done
timeout --signal=TERM --kill-after=1 5 nvidia-smi --version
```
