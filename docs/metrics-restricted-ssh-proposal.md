# Restricted metrics SSH lifecycle proposal (#56)

**Status: proposal only; not authorized or implemented.** This keeps the approved
host-push/schema/ACK interface. Sol Medium's focused delta review returned FIX; crash-safe
renewal/child containment, durable anti-replay across reboot, complete SSH authorization and
environment policy, and disk-control-key retention still need resolution. No transport
implementation is justified by this review. Explicit owner approval of the privileged guest
authority is also required before implementation.

## Proposed lifecycle

1. **Separate disk control from run data identity.** During first protected provisioning of
   a persistent parent disk, create one Ed25519 control keypair. The private key remains in
   owner-private parent state (`0700` directories, `0600` file, owner UID, single link); its
   public key is installed only in root-owned guest policy by the protected seed. Retain
   this disk-scoped control key across parent runs so it can revoke an expired generation
   and install the next one; it is never accepted for telemetry or an interactive shell.
   For each run/generation, create a distinct Ed25519 data keypair, store its private key in
   the same owner-private state, send only its public key, and never reuse it across
   generations. Preserve data keys until revocation/expiry is proven and exact SSH children
   stop. Theft of the disk control key or compromise by a process with the same host UID is
   outside this design's containment guarantee; lease expiry cannot contain those attackers.
2. **Keep privileged authority separate from guest `mester`.** Guest `mester` must not be
   able to install, renew, or revoke keys, choose a key, or write receiver state. The
   persistent disk control key and current run's data key are distinct root SSH identities,
   accepted only with `PermitRootLogin forced-commands-only`. A root-owned key policy maps
   the disk control key to exactly the fixed lifecycle helper and the current data key to
   exactly the receiver; expired/revoked data keys are not returned. The lifecycle helper
   accepts only the current disk/run/generation tuple and fixed operations. Neither private
   key is installed in guest `mester`'s account or workspace. The root-owned helper/receiver run
   with required root privileges and cannot be invoked as arbitrary commands by guest
   `mester`. This persistent, narrowly forced root authority is new and requires explicit
   owner approval.
3. **Restrict and verify effective SSH policy.** Set `AuthorizedKeysFile none` for root and
   use only a root-owned `AuthorizedKeysCommand`/policy helper that returns current,
   generation-matched keys with per-key `restrict` and fixed-command options. Require
   `AuthenticationMethods publickey`, `PubkeyAuthentication yes`, and
   `PermitRootLogin forced-commands-only`; disable password and keyboard-interactive auth,
   forwarding, PTY, `PermitUserEnvironment`, and `PermitUserRC`. Use a fixed root-owned
   non-interactive shell, clear the helper/receiver environment to an explicit allowlist,
   and reject unexpected `SSH_ORIGINAL_COMMAND`. Restrict process/session counts. Before
   acceptance, inspect effective `sshd -T -C user=root` and verify the exact forced-command
   behavior on the target guest; do not infer this boundary from config text alone.
4. **Use a generation state machine and independent leases.** Root-owned `/run` state binds
   `(parent run ID, generation, public-key fingerprint, next sequence, authorization expiry)`.
   Install atomically creates the current generation with next sequence one and a bootstrap
   expiry of `guest_monotonic_now + 2s`; its ACK follows durable state publication. It is
   idempotent only for that exact current generation/key; stale or revoked generations
   cannot be reinstalled. Renewal is accepted only for the active unexpired generation;
   revoke is idempotent for that exact generation and cannot affect a newer one. Each frame
   must match the run/generation and exact next sequence;
   sequence check and snapshot publication are atomic across concurrent receiver invocations.
   The supervisor—not accepted telemetry frames
   or a surviving sender child—renews the short authorization lease through the separate
   control channel (maximum 2 seconds, measured by the guest monotonic clock). The control
   renewer lives with the supervisor: no detached renewer, persistent SSH master, or control
   key in the sender; at most one bounded renewal request is in flight. If the supervisor
   exits, its exact helper is terminated and any final in-flight renewal can extend expiry
   only once; then the lease expires without further renewal. Every data request rechecks
   generation and lease inside the root receiver, so an already-authenticated SSH session
   cannot cause acceptance after revocation or expiry. Revoke serializes with
   acceptance/publication/renewal and ACKs only after that
   generation can no longer accept or renew. Frame freshness is distinct: each published
   snapshot expires after 1.5 seconds of guest-monotonic time. A transient data-channel
   outage does not expire authorization while the live supervisor renews it. If the lease
   expires, a live supervisor automatically bootstraps a fresh generation after connectivity
   returns; it never silently revives a revoked one. A guest reboot clears all runtime keys,
   leases, and snapshots.
5. **Bound the receiver and exchange.** Preserve the schema-v1 16-KiB frame and 256-byte
   ACK limits. Accept exactly one newline-terminated strict UTF-8 JSON frame per connection;
   reject duplicate keys, extra bytes/frames, malformed input, and commands. The sender
   half-closes after the one frame; the receiver requires EOF within the same absolute
   deadline before accepting it. Bound read, write, stderr, process/session counts, and the
   absolute collect-through-ACK deadline;
   synchronously permit at most one in-flight frame and clean up only exact verified
   children. A control renewal is committed before its ACK; telemetry publication and
   freshness update are committed before the frame ACK. Telemetry frames do not extend the
   authorization lease. Verify `/run` is the expected tmpfs, then use directory-relative,
   no-follow, exclusive file operations for root-owned snapshot and authorization state.
   Never use workspace or home paths.
6. **Verify the guest host.** The sender uses the persistent host Ed25519 key already
   provisioned in the seed and matching `known_hosts` pin, with
   `StrictHostKeyChecking=yes`, `HostKeyAlgorithms=ssh-ed25519`, no agent, and all
   forwarding disabled. Do not use `ssh-keyscan` to establish trust.
7. **Stop, crash, and recover without false success.** On normal stop, stop the exact sender
   first, request fixed revocation, and require its positive generation-bound ACK before
   claiming revocation. If SSH/revocation is unavailable, stop only verified owned sender
   processes and request bounded shutdown of the verified QEMU under the parent lifecycle;
   preserve disk/key/run evidence, report `STOPPING`/`AMBIGUOUS` rather than clean `STOPPED`
   or revoked, and block replacement `up` while state is ambiguous. The guest lease prevents
   further acceptance after expiry even if an
   authenticated connection survives. Keep the host private key until revoke/expiry and
   child termination are proven. Startup rollback and owner recovery use the same state
   machine; recovery never signals an unverified process or reinstalls a revoked generation.

## New authority requiring owner decision

This introduces root-owned guest key authorization, lease renewal/revocation, a dedicated
forced-command control principal, and privileged receiver state. These capabilities exceed
the existing metrics frame/ACK contract. Do not implement restricted SSH transport unless
the focused delta review passes and the owner explicitly approves this exact authority and
lifecycle. Until then, continue only already-approved schema, scheduler, collector, codec,
and display work that does not presume this transport.
