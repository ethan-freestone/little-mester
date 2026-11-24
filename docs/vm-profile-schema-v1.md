# Protected VM profile mapping schema v1

**Purpose and status.** This is the normative encoding clarification for the
approved profile architecture in
[`vm-usability-integration.md`](vm-usability-integration.md), not an architecture
revision. It freezes the JSON representation and closed host-resource selectors
that a future fixed helper must implement. The JSON Schema is
[`schemas/vm-profile-v1.schema.json`](schemas/vm-profile-v1.schema.json); the
selector/layout/action inventory is
[`schemas/vm-profile-resources-v1.json`](schemas/vm-profile-resources-v1.json).

This document distinguishes three things:

1. **Existing approved normative behavior:** one authoritative mapping and no
   reference database; immutable per-profile runtime pins; lifecycle states and
   reset/remove ordering; protected atomic journals; fixed unit/account roles;
   lock ordering and nonblocking contention; fixed-singleton-only exclusion;
   workspace non-destruction; and explicit, separately authorized runtime GC.
2. **Newly frozen encoding details:** exact mapping keys/types, deterministic
   identity derivation, resource selector names and layouts, transaction target
   records, and the bounded JSON parsing rules in this specification.
3. **Implementation status:** the request/runtime-validation core, committed-tree
   runtime installer primitive, owner authorization gate, and maintenance-lock
   substrate exist as described in
   [`vm-profile-core.md`](vm-profile-core.md). No profile-mapping reader/writer,
   metadata schema consumer, transaction journal implementation, fixed installed
   dispatcher, lifecycle, account/unit manager, inventory/pruner, signed
   bootstrap, or owner/live acceptance is implemented by this contract commit.
   The fixtures test declarative encoding and selected semantic rules only; they
   do not prove filesystem protection, crash recovery, systemd behavior, or host
   safety. Ticket #67 acceptance criteria remain unchanged and incomplete.

## 1. Approved boundaries retained

The mapping at `/etc/little-mester-vm/profiles/<name>/profile.json` is the sole
authoritative profile-to-runtime reference. There is no reference counter or
second profile database. The mapping contains no credentials. Root-owned
temporary files, file `fsync`, atomic rename, and parent-directory `fsync` are
required for every mapping/journal publication. Unknown versions, fields,
selectors, actions, or unsafe objects fail closed; there is no implicit schema
migration.

The lifecycle state enum is exactly `installing`, `ready`, `resetting`, and
`removing`. `ready` has no transaction. `installing`, `resetting`, and `removing`
have only their matching `install`, `reset`, and `remove` transaction. Successful
install reaches `ready` only after all exact artifacts and effective units verify;
successful reset returns to `ready`; successful remove unlinks the mapping as its
final profile commit. A profile remains pinned to its recorded full runtime SHA;
reinstall at a different SHA refuses. There is no implicit migration, workspace
deletion, or install GC. Runtime pruning is a separate explicit operation.

The permitted mapping-state transitions are:

| From | To | Condition |
|---|---|---|
| No mapping | `installing` | New explicit install publishes its prepared journal/mapping. |
| `installing` | `installing` | Resume only the same durable install transaction. |
| `installing` | `ready` | Exact install artifacts and units verify; clear transaction atomically. |
| `installing` | `removing` | Explicit confirmed removal of only journal-proven install artifacts. |
| `ready` | `resetting` | Explicit confirmed reset after the stopped/job-free predicate. |
| `resetting` | `resetting` | Resume only the same reset transaction and exact leaf intents. |
| `resetting` | `ready` | All seven reset leaves verify absent; clear transaction atomically. |
| `ready` | `removing` | Explicit confirmed removal after the stopped/job-free predicate. |
| `removing` | `removing` | Resume only the same remove transaction and exact object evidence. |
| `removing` | No mapping | Final mapping unlink, after units, state, and accounts are verified/removed. |

No other transition or transaction substitution is valid. `down` does not change
the mapping state. Runtime update/migration does not exist in this state machine.

Reset deletes only the seven listed reset leaves and preserves the mapping/runtime
pin, accounts, unit fragments, and workspace assignment. Remove preserves the
approved order: exact unit removal/reload/absence verification; exact known
state/config/runtime child removal and empty-directory checks; recorded account
and group verification/removal without deleting a home; then final mapping
unlink. The config directory may be removed only after that unlink and only if
empty. Missing units during remove recovery are accepted only with durable
matching removal intent or recorded install evidence that proves those exact
units were never created. Unknown children are never recursively removed.

The fixed `#20` singleton is excluded only by its four component unit/job states
(VM, proxy, workspace, metrics). The distinct `feat/vm-approach` development VM
is outside that exclusion. No singleton workspace comparison or new singleton
resource cap is introduced. Existing nonblocking lock order, fixed-helper
authorization, runtime pinning, service isolation, workspace checks, and immutable
runtime inventory/prune boundaries remain as specified by the integration
baseline.

The common-control transaction marker at
`/etc/little-mester-vm/code-update.in-progress` is a **distinct schema** with its
own owner, operation, transaction, artifact/SHA, and journal identity. It is not
the profile transaction below and must never be parsed or recovered as one.

## 2. Mapping v1 encoding

The root object has exactly these required keys; there are no optional or
extension keys:

| Key | Encoding |
|---|---|
| `schema_version` | Exact JSON integer `1`. |
| `owner_uid` | Positive integer, at most `2147483647`; it is the enrolled owner identity, not request-supplied identity. |
| `name` | Existing profile grammar `[a-z][a-z0-9-]{0,31}`. Also reject the reserved command names `system`, `profiles`, `installations`, `prune`, `bootstrap`, `control`, and `prepare-dependencies`. |
| `state` | `installing`, `ready`, `resetting`, or `removing`. |
| `runtime` | Exact object below, including the full 40- or 64-character lowercase SHA. |
| `accounts` | Exact `vm` and `proxy` role objects below. |
| `units` | Exact `vm`, `proxy`, `workspace`, and `metrics` unit names below. |
| `workspace` | JSON `null` or the exact observation object below. It is never authority by itself. |
| `ssh_port` | Integer from `22223` through `23222`, inclusive. |
| `transaction` | JSON `null` or the exact operation-specific v1 record below. |

Every nested object is closed (`additionalProperties: false`). Exact JSON integer
checks must reject booleans (`true`/`false`) even in languages where booleans are
integer subtypes. All numeric identities are bounded positive integers except
device numbers and mount major/minor, which may be zero. VM/proxy UIDs must be
distinct, VM/proxy GIDs must be distinct, and neither service UID may equal the
owner UID. UIDs and GIDs are separate numeric namespaces. Identity, allocation,
NSS, and host system-range checks are additional semantic/host validation, not
properties inferred from JSON types alone.

### 2.1 Runtime, account, and unit objects

`runtime` has exactly `commit_sha`, `profile_runtime_api`,
`control_helper_api_min`, and `control_helper_api_max`. The SHA is a full lowercase
40- or 64-hex Git object ID; abbreviations are forbidden. API fields are exact
integers in `1..65535`; minimum must not exceed maximum. Dispatch compatibility
is checked against the installed helper and verified manifest before execution.

Each account object has exactly `name`, `uid`, `gid`, and `allocation`, where
allocation is `planned` or `committed`. The names and groups are recomputed, never
trusted from stored strings:

```text
digest = first 20 lowercase hex characters of
         SHA256(ASCII("little-mester-profile-v1\0" +
                      canonical_decimal(owner_uid) + "\0" + profile_name))
VM user/group    = lmv-<digest>
Proxy user/group = lmp-<digest>
VM unit          = little-mester-vm-p-<digest>.service
Proxy unit       = little-mester-vm-p-<digest>-proxy.service
Workspace unit   = little-mester-vm-p-<digest>-workspace.service
Metrics unit     = little-mester-vm-p-<digest>-metrics.service
```

`units` stores exactly those four unit names under their corresponding role
keys. The fixed vectors below are normative and must be tested byte-for-byte:

| owner UID | profile | digest | VM account | proxy account |
|---:|---|---|---|---|
| `1000` | `default` | `8b95a12479c85e8b1338` | `lmv-8b95a12479c85e8b1338` | `lmp-8b95a12479c85e8b1338` |
| `1000` | `work` | `1c04efbef58307f16a27` | `lmv-1c04efbef58307f16a27` | `lmp-1c04efbef58307f16a27` |
| `65534` | `a` | `d2c5616a3545a5ad0e4b` | `lmv-d2c5616a3545a5ad0e4b` | `lmp-d2c5616a3545a5ad0e4b` |

Every recomputed name must match exactly. Any generated-name or numeric-ID
collision refuses before effects; the helper must not select a replacement
identity or adopt an unmarked account.

The approved baseline is two static non-login accounts and matching private
primary groups per profile. The VM account alone receives supplementary `kvm`;
the proxy account receives no supplementary groups; each generated primary group
has no unexpected members. The frozen marker is the exact ASCII account comment
`little-mester-vm-profile-v1:<canonical-owner-uid>:<profile-name>:<vm|proxy>`;
the group marker appends `:<role>:group` after the profile name. The passwd home
field is the empty string and no home directory is created or recursively
removed. The shell must be the host's canonical root-owned, non-writable nologin
executable. If the exact host path/identity, system UID/GID ranges, marker,
bidirectional NSS identity, group membership, or no-process condition cannot be
proven, refuse. These are safe fail-closed host checks; no account creation,
marker verification, or removal implementation is claimed here.

### 2.2 Workspace observation and port derivation

Non-null `workspace` has exactly `canonical_path`, `device`, `inode`, `mount_id`,
`mount`, and `unsafe_recurse`. `mount` has exactly `major`, `minor`, `root`, and
`mount_point` (one spelling everywhere). Paths are absolute normalized paths;
reject NUL/control bytes, repeated separators, `.`/`..` components, and
noncanonical spellings. `device`, `inode`, mount ID, major/minor, path, and
mount-root values are observations only. Before every use, re-open through
no-follow descriptor-relative checks, revalidate `statx` mount ID, decode and
validate `/proc/self/mountinfo` escaped fields (`\\040`, `\\011`, `\\012`,
`\\134`), and prove the selected mount/path relationship and aliases. If statx,
mountinfo, ancestor identity, or alias proof is absent or ambiguous, refuse.
Revalidate after reboot; these IDs are not durable authority and workspace
contents are never a destructive target.

The stored SSH port `p` is in `22223..23222`. Derive the proxy port as `p+1` and
metrics port as `p+2`; do not store independent proxy/metrics port keys. All
three values must be valid TCP ports, pairwise distinct, outside the fixed
singleton SSH reservation (`22222`), unique across the catalog and available on
the intended loopback listeners. Conflicts or overflow refuse; a committed
profile is never silently reallocated. The proxy and metrics port files contain
canonical decimal ASCII plus exactly one LF.

### 2.3 Bounded JSON parser requirements

Before schema validation, accept at most 256 KiB of UTF-8 JSON with at most 16
nested arrays/objects. Decode UTF-8 strictly. Reject duplicate object keys,
non-finite numbers (`NaN`, `Infinity`, `-Infinity`), trailing second values,
malformed JSON, excessive size/depth, and integer values outside their field
bounds. Do not normalize duplicate keys or coerce floats, strings, or booleans to
integers. Serialize mapping/journal updates as UTF-8 JSON with no duplicate keys;
atomic publication and protected ancestry checks remain mandatory.

## 3. Transaction v1

A non-null transaction has exactly `schema_version`, `id`, `operation`, `phase`,
`step`, and `expected`. `schema_version` is integer `1`; `id` is exactly 32
lowercase hexadecimal characters generated randomly for that transaction;
`step` is an integer in `0..4`; `expected` is a bounded array (at most 64) of
closed, selector-discriminated objects. The transaction never accepts a path,
unit name, account name, arbitrary dictionary, replacement identity, or
caller-chosen selector. Selector resolution comes only from the closed resource
table. A selector may occur at most once in an `expected` array.

| Operation | Exact phases and matching step | Allowed expected selectors |
|---|---|---|
| `install` | `prepared=0`, `accounts=1`, `state=2`, `units=3`, `verified=4` | See `transaction_allowlists.install` in the resource table. |
| `reset` | `prepared=0`, `leaves=1`, `verified=2` | Exactly the seven reset-leaf selectors. |
| `remove` | `prepared=0`, `units=1`, `state=2`, `accounts=3`, `finalizing=4` | See `transaction_allowlists.remove` in the resource table. |

The mapping-state/operation pairing is exact: `installing/install`,
`resetting/reset`, and `removing/remove`. `ready` requires a null transaction and
both account allocations `committed`. Reset changes only the seven fixed leaves
below; retry keeps the same transaction and advances only after its durable
intent/identity checks. Remove keeps the mapping/name reservation until final
commit. No phase permits an unlisted selector or generic recursive walk.

Each expected-object branch has exactly one selector class and an explicit
evidence shape:

* `observed_present`: exact no-follow observed identity. A file-system identity
  records bounded device, inode, kind (`regular`, `directory`, or `socket`) and
  single-link count. Account/group identities record the numeric UID/GID and
  exact role marker. Unit-manager identity binds the loaded unit to the exact
  fragment device/inode and records only `inactive`, `job_state=none`.
* `expected_absent`: records only that the object was absent at that preflight.
  This is **not** permission to accept later absence after the operation could
  have created it.
* `never_created`: allowed only for a unit-file or unit-manager selector in a
  remove transaction. It carries a different `install_transaction_id` and an
  install phase of `prepared`, `accounts`, or `state`, all before unit
  publication. It proves non-creation, not removal. A later install phase cannot
  be used to infer that a particular unit was never created.
* `removal_intent`: stores the exact previously observed identity and is durably
  published/fsynced before unlink or account/unit removal. Later absence is
  accepted only for that same selector/identity, operation, and durable phase.
  It cannot authorize replacement-identity adoption.

File/account/group/manager object schemas are separately discriminated in the
JSON Schema. The resource table is the operation allowlist; the fixture checker
cross-checks schema selector enums against that table and enforces per-operation
selector/evidence policy. Unsupported lifecycle actions fail closed; this
encoding does not claim that a fixed helper currently implements them.

## 4. Closed resource layout and operations

The JSON resource table is normative and intentionally finite. `<name>` is the
validated mapping name; `<digest>` is the recomputed 20-hex digest. No caller
data is interpolated into other path components.

### 4.1 Persistent and protected profile resources

* `S=/var/lib/little-mester-vm/profiles/<name>` is root:root `0711`.
  `S/vm` is VM-account:VM-group `0700`; `S/client` is enrolled-owner:owner
  primary-group `0700`.
* `C=/etc/little-mester-vm/profiles/<name>` is root:root `0700`; its only v1
  child is authoritative `profile.json`, root:root `0600`.
* The seven and only reset leaves are `S/vm/disk.qcow2`, `S/vm/seed.iso`,
  `S/vm/ssh_host_ed25519_key`, `S/vm/ssh_host_ed25519_key.pub`,
  `S/client/control_ed25519`, `S/client/control_ed25519.pub`, and
  `S/client/known_hosts`. Private keys, disk, seed, and known_hosts are `0600`;
  public keys are `0644`. Ownership is exact as listed in the resource table.
* Reset records each exact leaf identity before unlink, uses no-follow
  descriptor-relative operations, and treats an initially absent leaf as
  idempotent only when the journal records that absence. Replaced identity,
  symlink, mount boundary, escape, or unknown child stops and retains the
  transaction. Reset never traverses a workspace.
* Remove accepts only the table's fixed leaves and directories; it checks each
  directory is empty before `rmdir`. It removes the four generated unit
  fragments and reloads/verifies systemd before state/config cleanup; verifies
  and removes generated accounts/groups without deleting homes; and unlinks the
  profile mapping last. It removes `C` only after mapping unlink and only when
  empty. Any unexpected child remains untouched and blocks completion.

### 4.2 Units, runtime roots, sockets, and ports

The four unit-manager identities and exact unit fragments are `vm`, `proxy`,
`workspace`, and `metrics`, with names derived above. The table fixes fragment
paths under `/etc/systemd/system`, root ownership and `0644` mode. A unit
manager record is accepted only for the exact generated fragment, known
inactive state, and no queued job; unknown, failed, masked, active, transitional,
or unexpected drop-in state refuses. The workspace service uses the enrolled
owner UID and profile VM primary GID only inside the approved minimal root; it
does not receive owner supplementary groups.

Reset performs the same inactive/job-free manager checks as a preflight, but its
transaction selector list remains exactly the seven reset leaves; it does not
journal or mutate unit-manager identities.

The private runtime tree is `/run/little-mester-vm-p-<digest>`, root:root
`0711`, with only these role children:

| Path | Owner:group | Mode | Meaning |
|---|---|---:|---|
| `vm/` | VM account:VM group | `0700` | `qmp.sock` is a VM-owned `0600` Unix socket. |
| `proxy/` | proxy account:proxy group | `0700` | `port` is a proxy-owned `0600` regular file containing `p+1`. |
| `metrics/` | VM account:VM group | `0700` | `port` is a VM-owned `0600` regular file containing `p+2`; history stays in memory and has no file. |
| `workspace/` | enrolled owner:VM group | `2710` | `virtiofs.sock` is enrolled-owner:VM-group `0660`; this is the narrow writable socket bind. |

No other runtime child is part of v1. Runtime cleanup checks every unit stopped,
job-free and process-free, then removes only recorded, exact known sockets/files
and empty role directories. Do not create or infer persistent proxy/metrics
files.

### 4.3 Fixed locks, snapshots, and catalog boundaries

The protected root:root `0755` lock directory is
`/run/little-mester-vm-control`. Fixed lock basenames are `maintenance.lock`,
`catalog.lock`, `host-admission.lock`, `profile-<digest>.lock`, and
`sha-<full-lowercase-SHA>.lock`. All are root-owned, single-link regular files;
all are `0600` except SHA locks, which are `0644` so services can open them
read-only for shared locks. The existing code-update lock remains separate at
`/etc/little-mester-vm/code-update.lock`, root:`lm-vm-control` `0660`.

All locks are acquired no-follow and nonblocking with path/opened-inode identity
checks. Preserve the approved ordering in the integration baseline; on any
contention release held locks and return bounded `BUSY` or restart from the
beginning, never wait for a lower-ranked lock while holding a higher-ranked one.
Catalog protects only membership/pin/owner/name/reservation/workspace projection,
bounded coherent snapshots, atomic mapping/assignment publication or unlink,
and the short admission/start handoff through systemd `activating`. A coherent
full-record snapshot takes all corresponding shared profile locks in sorted
profile-name order while catalog is shared; contention is `BUSY`, records are
not omitted, and the catalog is capped at 1000 profiles. Release catalog before
full runtime hashing; revalidate the snapshot under ordered locks before
mutation/prune. No catalog lock is held for Git, guest readiness, full-tree hash,
or runtime deletion. These snapshot details constrain future implementation;
they are not a claim of current lock/catalog code.

### 4.4 Immutable runtime inventory names

Keep the existing immutable store `/opt/little-mester/v3/<full-SHA>`, manifest
basename `manifest.json`, and manifest inventory key `files`. A same-parent stage
name is exactly `.staging-<full-SHA>-<16 lowercase hex>`; a GC quarantine is
exactly `.gc-<full-SHA>-<16 lowercase hex>`. A stage/quarantine may be acted on
only when its exact name and observed identity are recorded by the operation's
durable journal. Unknown/orphan stages and quarantines are retained as
`unknown/incomplete`; there is no prefix cleanup, adoption, or automatic retry.
Runtime installs remain immutable and profile removal never triggers GC.
This mapping clarification does not redefine the committed runtime descriptor
`profile-runtime.json`, its fixed entrypoint `lib/vm/profile-runtime.py`, or the
existing manifest schema/inventory and API compatibility checks.

## 5. Explicitly unsupported targets and implementation boundary

The selector table excludes arbitrary and unknown children, generic recursive
deletion, proxy/metrics persistent files, browser-auth temporary directories,
workspace probes, provisioning scratch, unexpected drop-ins, orphan staging or
`.gc` trees, and all workspace contents. It refuses any resource whose exact
identity cannot be established without following links or crossing an
unverified mount boundary. Common-control transaction state is a different
schema. Because the helper lifecycle action set is not implemented, unlisted or
unsupported selectors/actions must refuse rather than be guessed.

This schema/fixture commit does not implement or establish the profile metadata
reader/writer, atomic protected filesystem operations, transaction recovery,
account marker inspection, systemd verification, fixed helper dispatch, signed
bootstrap, workspace mount proof, inventory/pruning, or owner/live acceptance.
Passing these fixtures is not filesystem-protection or crash-recovery evidence;
the #67 implementation and acceptance ledger remains in progress.
