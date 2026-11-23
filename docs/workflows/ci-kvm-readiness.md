# Transient KVM CI readiness

The CI `boot` gate waits for a fully provisioned transient guest, not merely
for transport. Cloud-init `runcmd` runs apt index update, installs tmux,
Node.js, npm and CA certificates, installs exactly `@opencode/cli@2.2.5`,
prepares workspace/user directories and SSH socket service, then publishes
`/var/tmp/lm-kvm-trial-ready`. Serial output exposes START/END/FAIL substage
markers with integer `SECONDS` elapsed time; apt update is deliberately
non-quiet. A failed substage emits FAIL, never END.

Boot success additionally requires cloud-init's finished message, an Ed25519
host key pinned from that owned serial log, authenticated SSH, active
`ssh.service`, non-error `cloud-init status --long`, the boot-finished file,
and the exact readiness marker. These checks and identity-safe final cleanup
remain strict. Progress is reported every 15 seconds with serial tails every
30 seconds.

The readiness limit is 900 seconds inside a 960-second boot-stage deadline;
the workflow boot step has an 18-minute timeout. One persisted 2400-second
monotonic whole-run cap starts during state initialization and is not reset or
renewed by later stages. These are shared finite bounds, not stage allowances;
the outer cap bounds each nested deadline. Cleanup remains allowed after its
expiry so the lifecycle owner can prove shutdown.

This is not production readiness acceptance: `lib/vm/system-vm.py` separately
uses a 900-second full-provisioning bound and protected-session readiness.
Transient initial QEMU uses direct user-mode egress, not the production proxy;
it proves neither proxy reachability nor proxy performance.
