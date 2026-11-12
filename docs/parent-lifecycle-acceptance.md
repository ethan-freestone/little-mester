# Owner-run persistent-parent lifecycle acceptance

**Owner action only.** The agent must not run this procedure. It starts a real VM and
may download the pinned guest image. The scratch QEMU/qemu-img compatibility gate has
owner-reported PASS ×2 for source revision `37f5fb622135831a5195615e175086c8053b0992`;
this procedure is the next, separate acceptance step and does not imply the VM has
already been accepted. The NVIDIA probe remains unrelated and pending.

## Before starting

- Use the candidate checkout containing the integrated `parent up` path and review its
  change list locally. Do not proceed if this is not the intended source.
- Do not start if `lab-vm parent status --json` reports `STARTING`, `RUNNING`, or
  `AMBIGUOUS`. For an existing RUNNING parent, use its normal `ssh`/`tui` commands; do
  not start a second writer.
- Choose the initial virtual capacity deliberately. This procedure does not resize an
  existing disk; reusing a parent with a different requested capacity is refused.

## Bounded first run and persistence check

```sh
lab-vm parent up --capacity-gib 64
lab-vm parent status --json
lab-vm parent ssh
```

After SSH is ready, create a non-sensitive guest-disk canary and inspect it:

```sh
mkdir -p /home/mester/workspace
printf 'parent-persistence-canary\n' > /home/mester/workspace/.phase1-parent-canary
cat /home/mester/workspace/.phase1-parent-canary
```

Exit the SSH shell, then use `lab-vm parent tui` to confirm that the protected OpenCode
launcher reaches the persistent tmux session. Before attaching, follow the owner-terminal
OSC 52 mitigation and harmless canary in [docs/opencode.md §J.12](opencode.md#j12-remote-terminal-clipboard-write-mitigation-55).
The first boot must complete cloud-init,
install the pinned OpenCode 2.2.5 CLI, and report RUNNING only after pinned-host-key SSH
readiness and `/usr/local/bin/lm-opencode` availability. Startup readiness is capped at
900 seconds after bounded image/seed provisioning steps; the `up` operation refuses on
failure and performs identity-bound child rollback without deleting the disk.

Stop and repeat the same initial capacity:

```sh
lab-vm parent stop
lab-vm parent status --json
lab-vm parent up --capacity-gib 64
lab-vm parent status --json
lab-vm parent ssh
cat /home/mester/workspace/.phase1-parent-canary
```

Expected: stop reaches STOPPED, the second up reaches RUNNING, and the canary remains
unchanged. Also verify the guest's OpenCode local history/config remains present after
re-up by opening the TUI; do not perform browser authentication or add credentials during
this acceptance. A tmux process is scoped to one guest boot; reattachment is tested by
disconnecting/reconnecting with `parent tui`, while cross-stop persistence applies to
the guest disk data, not a live tmux process.

## Refusal, timeout, and evidence

- The first `up` can spend bounded time downloading/verifying the pinned base image,
  converting/sizing the disk, creating the seed, and waiting up to 900 seconds for guest
  readiness. Do not start another `up` concurrently.
- `parent stop` waits 15 seconds by default. A timeout does not cancel graceful QMP
  shutdown (which can continue up to 60 seconds); inspect status before retrying.
- If any command returns REFUSED, status is AMBIGUOUS, QEMU identity changes, or rollback
  cannot verify a child is stopped, stop the acceptance. Preserve the disk and all state;
  do not manually kill QEMU, delete files, or repeat `up` until the owner has inspected
  the exact evidence.
- Record the exact source revision, host QEMU/qemu-img versions, command output, both
  status JSON results, stop/re-up result, canary equality, and observed OpenCode history
  persistence. A successful first boot alone is not lifecycle acceptance.
