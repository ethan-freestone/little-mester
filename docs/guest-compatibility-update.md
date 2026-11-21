# Fixed system VM: compatibility-gated host update

## Status

This is the reviewed implementation contract for the compatibility/update
work; the commands marked **planned** are not supported until the implementation
is complete and reviewed on its exact commit. It does not authorize an owner
operation. Do not use either existing owner VM for development or tests.

The source comparison is between base
`497a70ca131cc622f661c52d08f10baf6ec0e4d2` and candidate
`90953b22cfe984e2f3d2ed44e5c5c5e64559ed81`. The shared bootstrap and rootless
Docker provisioning are unchanged. The candidate changes the fixed system guest
contract: it uses virtiofs at `/workspace`, adds protected session/config,
preferences-helper and metrics-plugin assets, and checks those assets at
readiness. A guest provisioned exactly from the base recipe would not match this
candidate contract. This does **not** establish the state of either installed
owner guest; it may already have been separately updated.

The protected `--update-code-only` path changes host assets only. It does not
change the guest disk, cloud-init seed, SSH keys, workspace assignment, or guest
authentication/preferences/history, and it must not start the VM. Guest
compatibility is checked only on a later, separately owner-authorized start.

## Update and rollback contract

Before update, the owner records the previous installed SHA and candidate SHA,
checks the current host state with the previous pinned runtime, and separately
authorizes the host-code update while guest compatibility is still unknown. The
update refuses active or ambiguous services; it never stops them. One shared
exclusive update lock serializes update/rollback with the supported lifecycle
and interactive routes. Fixed systemd start guards reject starts during the
update. The one-time legacy transition first blocks all four fixed units (VM,
proxy, workspace and metrics), verifies they are stopped, then publishes and
verifies durable maintenance guards before changing protected assets. If any
guard or state check is ambiguous, it refuses before mutation and leaves
recovery evidence intact.

**Planned update command (not yet supported):**

```sh
sudo /opt/little-mester/v3/$CANDIDATE_SHA/tools/vm-system-install \
  --update-code-only --previous-sha "$PREVIOUS_SHA"
```

Before the first protected target is replaced, the updater durably snapshots the
complete prior inventory of the fixed protected targets, including exact bytes,
owner/group/mode, rendered owner-specific units, and targets/directories that
were absent. The snapshot is root-controlled, bounded, tied to the exact
candidate SHA and retained for recovery. The old immutable `/opt/.../$PREVIOUS_SHA`
installation is also retained. The update is **not** a multi-file atomic
transaction. If interrupted, the durable guard blocks supported VM use and the
snapshot remains available; do not start the VM or manually edit installed
files.

**Planned rollback command (not yet supported):**

```sh
sudo /opt/little-mester/v3/$CANDIDATE_SHA/tools/vm-system-install \
  --rollback-code-only --candidate-sha "$CANDIDATE_SHA"
```

Rollback requires all fixed services inactive/failed and the exclusive update
lock. It validates the protected snapshot and target inventory, accepts only
known pre-update or candidate bytes (including a safe interrupted-update state),
and refuses unknown drift without deleting evidence. It restores the prior
inventory and metadata, removes only recorded candidate-added regular files
with expected bytes, and removes a candidate-created directory only when it is
still safely owned and empty. It verifies the restored inventory and unchanged
disk/seed/key/assignment/workspace fingerprints, reloads systemd, never starts
services, and retains the snapshot. It does not roll back or mutate guest state.

## Guest compatibility gate

After a successful host update, the VM remains stopped. A separate owner
authorization is required to start it. During systemd `ExecStartPost`, before
normal readiness succeeds, a fixed read-only checker uses the pinned system-VM
SSH identity and host key to inspect an allowlisted inventory of immutable guest
assets and the `/workspace` virtiofs mount. It compares a complete bounded
response with the expected inventory derived from the pinned candidate.

The checker must not accept a caller-provided command, destination, or path; it
must not follow symlinked path components, execute guest launchers/plugins or
OpenCode, write guest/workspace files, or inspect preference, authentication,
conversation-history, or workspace contents. It excludes mutable user state
from the compatibility digest. It emits only bounded fixed diagnostics and one
of:

- `MATCH`: every required immutable asset and fixed workspace-mount observation
  is complete and matches.
- `MISMATCH`: a complete, well-formed observation proves a required item is
  absent or differs.
- `INCONCLUSIVE`: transport, identity, timeout, schema, type, size, or response
  validation is incomplete or ambiguous.

Only `MATCH` permits startup to proceed. Both other results fail the systemd
startup gate; the unit does not become active and normal `ssh`, `auth`, and `tui`
routes must refuse. The checker does not start/stop the VM, retry into normal
use, repair the guest, or trigger a guest update. A separately authorized boot
can run existing cloud-init/guest boot activities before the checker; those
activities are not claimed to be read-only. The checker itself is read-only.
This check establishes the listed contract in the trusted guest execution
environment; it is not attestation against a compromised guest OS and does not
prove full Phase 1 acceptance or persistence semantics.

If the checker reports mismatch or inconclusive, preserve the VM and evidence;
do not use normal agent routes. Guest asset repair/update requires a separate
design review and owner authorization. No cloud-init reseed, disk replacement,
fresh guest, or automatic migration is part of this path.

## Planned owner sequence

The following command shapes become actionable only after implementation,
focused tests, exact-SHA Sol review, and hosted production systemd acceptance:

1. Pin full previous/candidate SHAs and record the selected VM, disk, and
   workspace. Run host-only `verify`/`status`; these do not establish guest
   compatibility.
2. Obtain explicit approval for protected host-code update before compatibility
   is known. Confirm all four fixed services are already inactive/failed; stop
   on active/ambiguous state. Run the planned update command above. Verify it
   returns without starting the VM and that disk, seed, keys, workspace
   assignment, and workspace drop-in fingerprints are unchanged.
3. If update fails or compatibility later fails, keep the VM stopped and use
   only the planned rollback command after separate owner approval. Do not
   improvise `systemctl`, SSH, or file edits.
4. Obtain a separate, explicit approval to start the selected VM for
   compatibility-gated acceptance. Use the pinned owner `up` command. `MATCH`
   permits normal acceptance; `MISMATCH` or `INCONCLUSIVE` means stop and
   preserve state. The update command itself never starts the VM.
5. After `MATCH`, proceed with owner UX, workspace, networking, provider,
   persistence, and CachyOS checks in the Phase 1 owner checklist. These remain
   separate acceptance gates.

No implementation, hosted run, deployment, or operation of an owner VM is
authorized by this design document.
