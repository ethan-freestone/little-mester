# Restricted metrics SSH lifecycle proposal (#56)

**Status: proposal only; not authorized or implemented.** This is the shortest design I
see that keeps the approved host-push/schema/ACK interface and does not put a forced
command in `mester`-writable policy. It adds a root-trusted guest authorization path and
needs explicit owner approval before implementation. No external review was requested or
performed.

## Proposed lifecycle

1. **Generate and retain the key on the host.** For each fresh parent run, the supervisor
   creates an Ed25519 keypair with the protected `ssh-keygen`. The private key is stored
   only in the owner-private parent state directory (`0700` directories; private file
   `0600`, owner UID, single link); the public key is the only half sent to the guest.
   Reuse is forbidden across run IDs. Remove the private file only after guest revocation
   is acknowledged and all relevant SSH children are stopped. If revocation is uncertain,
   retain the private file as ambiguous evidence and do not start a replacement run.
2. **Authorize it only in root-controlled guest policy.** Create a locked,
   non-interactive `mester-metrics` account with a usable fixed shell, no home writes,
   and a root-owned `AuthorizedKeysCommand`/policy helper. The helper accepts exactly the
   current run's Ed25519 public key from root-owned runtime state; it does not read
   `mester`'s home, `.ssh`, environment, or workspace. A root-owned forced receiver accepts
   exactly one bounded schema-v1 frame and emits one matching ACK. The receiver stores the
   latest snapshot atomically in guest `/run` (tmpfs), with run ID, increasing sequence,
   and an explicit freshness deadline.
3. **Restrict SSH at the server.** An `sshd Match User mester-metrics` block enforces
   `ForceCommand` to the fixed receiver, `PermitTTY no`, `DisableForwarding yes`,
   `AllowTcpForwarding no`, `AllowAgentForwarding no`, `X11Forwarding no`,
   `PermitTunnel no`, and no password authentication. The root authorization helper returns
   a key with `restrict` and `command="/usr/local/libexec/lm-metrics-receiver"`; the
   receiver rejects non-empty `SSH_ORIGINAL_COMMAND`, extra input, multiple frames, and
   malformed/oversized data. The locked account cannot obtain an interactive shell.
4. **Install, renew, and revoke narrowly.** A root-owned guest helper installs only a
   validated Ed25519 key bound to the current 32-hex run ID and a short lease; a second
   fixed helper revokes only that run ID. The host control key may invoke these exact
   helpers, not write `authorized_keys` or arbitrary root files. Accepted frames renew a
   small authorization lease (proposed maximum 2 seconds); absent renewal, the
   `AuthorizedKeysCommand` refuses authentication based on guest monotonic time, and a
   guest timer removes expired runtime authorization. A reboot loses the `/run` key and
   lease. This prevents a host supervisor crash from leaving an indefinitely accepted
   key while QEMU/guest might still be alive.
5. **Verify the guest host.** The sender uses the persistent host Ed25519 key already
   provisioned in the seed and the matching `known_hosts` pin, with
   `StrictHostKeyChecking=yes`, `HostKeyAlgorithms=ssh-ed25519`, no agent, and all
   forwarding disabled. Do not use `ssh-keyscan` to establish trust for this channel.
6. **Stop, crash, and recover.** Normal stop first stops the sender, asks the guest's
   fixed revocation helper to remove the matching run key and lease, requires a positive
   ACK, then stops QEMU and remaining helpers; any uncertain revocation fails closed and
   preserves the disk/key evidence. On supervisor crash, the short guest lease expires
   without renewal even if the VM survives. A new `up` refuses while existing process or
   run identity is ambiguous. Owner recovery may use the independently pinned control
   SSH identity to request the fixed revoke/status operation; it must not delete state or
   signal an unverified process. A guest reboot also clears authorization because it is
   runtime-only.

## New authority requiring owner decision

This introduces root-owned guest key-authorization, lease-renewal/revocation helpers and
the ability for the host control principal to request only those fixed operations. The
guest root account—not `mester`—owns the policy and receiver. It also adds a lease protocol
and a guest timer. These are new trusted guest capabilities beyond the existing metrics
frame/ACK contract. If the owner does not approve this authority and lifecycle, do not
implement a restricted SSH transport; continue only schema, scheduler, collector and
offline codec/display work that does not presume it.
