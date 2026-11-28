# Protected VM profile mapping and resource contract v2

**Status:** owner-approved contract. The contract-document review chain is
recorded in ticket #67 and ended in Sol APPROVE at `8bf560a4616505660e5acf2a68275a3799ec2f02`;
that review covered the contract document, not implementation or acceptance.
This is the normative target for the named-profile helper, not evidence that
production provisioning has been implemented or accepted. The preserved
checkpoint implementation declared `schema_version: 2`, but its record shape
did not match this contract: it stored a fixed `base_name` rather than the closed
`base` reference with source SHA and published-manifest digest, and it omitted
the required provisioning-generation/evidence mapping. It also lacked the v2
machine-readable mapping and resource schemas. That checkpoint is reference
material, not the implementation foundation, and does not satisfy A12.

The owner authorizes disposable hosted execution as pre-closure evidence. It is
not final acceptance, does not satisfy A12, and does not authorize physical-host
operations or production provisioning.

V2 is a deliberate compatibility break from
the former `vm-profile-schema-v1.md` contract: a v2 helper rejects v1
mappings and resource contracts before effects, with no migration,
reinterpretation, or adoption. The fixed `system` singleton and the legacy
`feat/vm-approach` development VM are outside this change and remain unchanged.
The profile mapping/resource schema and profile install/reset transaction
schemas are version 2; the common request protocol, runtime manifest, and
profile-runtime API retain their separate existing versions.

## 1. Owner decisions and preserved invariants

Owner decisions accepted on 2026-11-25:

- A new profile is fully provisioned and start-capable before mapping state is
  `ready`. `ready` does not mean running, guest-booted, cloud-init-complete, or
  runtime-readiness-demonstrated.
- `reset` ends in `unprovisioned` after verified removal of the seven
  bootable-artifact leaves. `up` never provisions implicitly. The existing
  explicit `install` command reprovisions an existing profile while retaining
  its identity, runtime pin, allocations, base selection, units, and workspace;
  the owner need not remove and recreate it.
- Profiles use independent sparse qcow2 writable overlays of exactly 64 GiB,
  sharing only a verified immutable approved base. Disk writes, seed images,
  and SSH identities remain profile-private.
- Published base images are retained indefinitely for this increment: no
  automatic deletion, overwrite, repair-in-place, or base-image prune command.
  This does not waive or alter installed-runtime inventory/prune requirements.
- UID/GID selection uses the lowest unused IDs in the host's system ranges,
  checking local account/group databases and NSS. Planned values are journaled;
  only the existing durable pre-creation numeric-collision replan is allowed.
- SSH selection reserves the lowest available, unclaimed contiguous 3-port
  block. If its first port is `s`, SSH/proxy/metrics use `s`, `s+1`, and `s+2`.
  The start range is `22223..23220`, so the full block stays within
  `22223..23222`. Selection is serialized with catalog reservation; a committed
  block is never silently changed.

Existing security invariants remain: the protected profile mapping is the sole
authoritative profile-to-runtime reference; no refcount database is added;
selectors and resource actions are closed; journals are durable and
identity-bound; unknown or foreign objects are preserved and cause refusal;
the profile runtime is immutable; workspaces are never mutated or deleted; and
fixed-helper trust does not permit executing checkout/runtime code as root.

## 2. Mapping shape, states, and explicit reprovision

V2 retains the validated profile identity, pinned runtime, committed account and
group identities, four unit identities, workspace observation, and fixed SSH
port field. It adds:

- `base`: a closed reference to the approved base source SHA and its verified
  published-manifest digest. The image path is derived from the fixed base-store
  root and digest; a request or mapping cannot supply an arbitrary host path.
- `provisioning`: `null` when there is no committed guest generation (including
  `unprovisioned` and an in-progress fresh/reprovision install). `ready` has a
  closed current-generation record; `resetting` retains its source generation
  until reset commits. The record has a generation ID, the exact seven
  provisioning-leaf identities, and non-secret evidence to validate disk
  geometry/backing, seed contents, key-pair coherence, and the known-host pin.
  It never stores private key bytes.
- `state`: the existing states plus `unprovisioned`.

The v2 state/transaction pairing is exact:

| State | Transaction | Provisioning evidence | Meaning |
|---|---|---|---|
| `installing` | matching `install` with immutable `mode=fresh` or `mode=reprovision` | In progress only in its journal | Resume only the same transaction and mode. |
| `ready` | `null` | Complete current generation | All provisioning leaves, accounts, units, and base relationship verify; start is permitted. |
| `resetting` | matching `reset` | The source generation remains retained in the transaction/mapping until commit | Resume only exact journaled deletions. |
| `unprovisioned` | `null` | `null`; all seven leaves must verify absent | Profile identity and infrastructure remain, but no guest can start. |
| `removing` | matching `remove` | Source evidence retained for exact recovery | Existing confirmed removal lifecycle. |

Allowed successful transitions:

```text
absent        -> installing(mode=fresh)       -> ready
ready         -> resetting                    -> unprovisioned
unprovisioned -> installing(mode=reprovision) -> ready
ready or unprovisioned -> removing            -> absent
```

Interrupted operations stay in their transaction state. A partial reprovision
is not rolled back to `unprovisioned` merely because an attempt failed; recovery
resumes only exact generation-bound intents/identities, or the owner may use the
existing confirmed remove path. `reset` is valid only from `ready`; an already
`unprovisioned` profile is reported as such and is not implicitly reset again.

The command behavior is intentionally small and reuses `install`:

| Existing mapping | Explicit `install` behavior |
|---|---|
| Absent | Fresh install selects the committed checkout SHA. |
| `installing` | Resume only the recorded mode, generation, and transaction. |
| `ready` | Verify and return; never silently replace disk, seed, or keys. |
| `unprovisioned` | Explicitly reprovision with the retained SHA, base, identity, allocations, units, and workspace. Do not resolve checkout `HEAD` or accept a different pin. |
| `resetting` or `removing` | Refuse; require the matching explicit recovery operation. |

For reprovision, retain the original infrastructure-install provenance. Do not
rewrite or republish service accounts, groups, directories, or units. Keep their
committed numeric IDs, selected port block, workspace association, profile
identity, and runtime pin unchanged. Allocate a new provisioning-generation ID
and create a new overlay, seed, host key pair, client key pair, and matching
known-hosts pin. A `ready` profile with missing, changed, or foreign artifacts
fails verification; it does not turn itself into a reprovision. Only the
explicit confirmed `reset` followed by explicit `install` is the supported
replacement route.

`up`, `ssh`, `tui`, authentication, and guest-readiness/acceptance operations
refuse on `unprovisioned` without mutation. Status/display and fixed-helper
recovery/removal operations may still describe the retained mapping without
exposing paths or secrets. Runtime/CLI guest readiness after `up` remains
distinct from mapping state `ready`.

## 3. Shared immutable approved base

There is one approved source for this increment; do not substitute `latest`,
another revision, digest, architecture, or image:

```text
Ubuntu 24.04 Noble amd64
https://cloud-images.ubuntu.com/releases/noble/release-20261017/ubuntu-24.04-server-cloudimg-amd64.img
SHA-256 04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94
```

This reuses the singleton's pinned-byte digest policy. It does not claim
upstream-signature verification. The base is the verified upstream qcow2 stored
unchanged; no flattening, conversion, or per-profile copy is performed.

The minimal store is:

| Object | Path/layout | Required protection |
|---|---|---|
| Store root | `/var/lib/little-mester-vm/bases` | root-owned, `0755`; every ancestor root-controlled. |
| Base directory | `<store>/<source-sha256>/` | root-owned, `0755`. |
| Published base | `<base-dir>/base.qcow2` | root-owned, `0444`, regular single-link file; never writable by a profile service account. |
| Manifest | `<base-dir>/manifest.json` | root-owned, `0600`, closed bounded schema. Binds source/revision/URL, source and stored digests, exact derived path, file identity, qcow2 geometry/features, manifest version, and creation provenance. |
| Lock | `/run/little-mester-vm-control/base-<source-sha256>.lock` | Root-only single-link regular lock; acquired after shared maintenance lock. |

The profile mapping records its base reference before any overlay is created.
The manifest and image are verified on reuse. A missing, malformed, replaced,
or digest-mismatched published base causes refusal; it is never overwritten,
repaired, or deleted in place. Base preparation is narrowly scoped to this
approved image and is not a general image manager. Published bases, including
currently unreferenced ones, remain indefinitely. There is no base refcount or
base GC operation. Runtime inventory/prune remains separate and unchanged.

Base preparation/reuse is serialized by the per-digest lock and does not hold
catalog, profile, runtime-SHA, or host-admission locks during download, hashing,
copying, or image inspection. A base transaction records exact stage identities
before acting on them. Retry may reconcile only the matching journal and exact
objects; unknown stages are retained and reported, not removed by prefix. The
base reference is durably in the profile mapping before overlay creation, so
reset/reprovision and transitional mappings continue to identify the same
immutable image.

## 4. Image validation and QEMU trust boundary

The isolated local-tool evidence is recorded in
[`vm-profile-image-probe.md`](vm-profile-image-probe.md). It established for the
approved image under QEMU 8.2.2:

- The exact SHA-256 matches the approved digest. `qemu-img info` reports
  `format=qcow2`, `virtual-size=3758096384`, compat `1.1`, 64-KiB clusters,
  refcount bits 16, zlib compression, lazy refcounts off, `corrupt=false`, and
  extended-L2 off. There is no backing image or external data file; the backing
  chain contains exactly one image. `qemu-img check -f qcow2` reports no errors.
- An overlay created with explicit qcow2 backing format, absolute approved base
  path, `preallocation=off`, and `64G` has virtual size exactly
  `68719476736` bytes and remains sparse. `umask 077` is required for
  `qemu-img create` to produce mode `0600` (the default test umask produced
  `0644`).
- Two overlays point to one unchanged base and have independent writes. The base
  digest is unchanged after writes.
- QEMU 8.2.2 accepts an explicit read-only base block node connected as the
  overlay's backing node, even when the mutable overlay header names a different
  nonexistent backing path. Without the explicit node, QEMU follows the embedded
  path and refuses. Profile start must therefore use the explicit approved
  read-only base-node graph; it must not allow mutable overlay metadata to select
  an arbitrary host file.

Required production checks are fail-closed:

1. Verify the protected base bytes against the pinned digest and inspect them
   explicitly as qcow2. Require exact approved virtual size/features, no
   backing filename, no external data file, no encryption, no corruption flag,
   and no unsupported metadata. Do not change the flattened-image validator used
   by the singleton or parent VM.
2. Create one profile-owned sparse qcow2 overlay of exactly 64 GiB with an
   explicit qcow2 backing format and exact absolute published-base path. Verify
   file mode/owner/link count, virtual size, sparse allocation, qcow2 features,
   exact backing path/format, and absence of external data, encryption,
   corruption, unsupported features, or additional backing levels.
3. At start, the fixed helper validates the mapping and exact artifact identity,
   requires the profile's units/jobs and VM-account processes stopped, and keeps
   the profile operation serialized through QEMU launch. The pinned runtime
   opens the exact overlay with no-follow semantics and passes its still-open
   file descriptor to QEMU. A bounded unprivileged header inspector reads only
   that descriptor; it never opens or resolves a path found in the qcow2 header.
   It accepts only the exact supported header/features, rejects every external
   data-file feature/extension, encryption and unknown feature, and requires the
   encoded backing reference to match the selected base. QEMU receives that same
   overlay descriptor plus a read-only descriptor for the verified base, and its
   block graph explicitly attaches the overlay to the approved base node. It
   must not reopen the overlay or derive the base node from a mutable header.
4. The VM unit keeps the strict systemd sandbox (`ProtectSystem=strict`,
   `ProtectHome=yes`, private temporary storage, `NoNewPrivileges`, and a closed
   device allowlist) and its exact writable-path allowlist: the selected overlay
   and fixed private runtime only. The root-owned mode-`0644` SHA lock is opened
   read-only for shared `flock` and is not granted writable-path access. The seed
   and base are read-only, and other profile state is inaccessible by DAC. This
   is a second boundary, not a substitute for header validation. A writable qcow2's
   external data file is opened read/write by QEMU; external-data attempts to
   access a read-only host path must fail before guest execution. Tests must
   cover a changed header, a path to a world-readable host file, a path to another
   profile, and a path to an allowed profile file. No untrusted process may
   modify the overlay between fd inspection and QEMU opening that same fd.
5. Fetch through the fixed trusted downloader as an unprivileged worker. Run
   image parsing/probing and overlay creation as an unprivileged, bounded,
   filesystem-confined worker. Downloaded bytes are untrusted until root has
   copied them into a protected, exclusively created stage and verified the
   pinned digest. Root performs descriptor-relative identity checks, journal
   writes, ownership changes, and atomic publication; root does not parse images
   or execute checkout/pinned-runtime code.

The qemu-img JSON validator must reject external-data fields and unexpected
format/features, not just compare the top-level format. `qemu-img info` output
for an external-data fixture exposed `format-specific.data.data-file` and
`data-file-raw`; a nested backing fixture produced a three-image chain. Do not
run any qemu-img inspection on an existing untrusted overlay at startup; startup
uses the fd-only header inspector and passes the same descriptor into QEMU. The
owner VM account can modify the overlay while its service is running. Exclusive
profile serialization, a stopped/job-free/process-free precondition, descriptor
passing, and the unit's
filesystem sandbox prevent an attacker-controlled path substitution between
validation and QEMU opening the image. The QEMU fdset probe on the local 8.2.2
toolchain is recorded in the image-probe report; supported-host/KVM validation
remains outstanding.

Local qemu-img/QEMU probe results are fixture evidence only. They do not prove
hosted-KVM boot, systemd confinement, cross-distro QEMU behavior, release
provenance, or physical-host acceptance. Those remain separate gates.

## 5. Journal, resource, and lifecycle requirements

The v2 closed resource table extends install to the exact seven existing
per-profile leaves: disk, seed, VM host-key pair, client/control-key pair, and
known_hosts. These are produced with the base reference and a new generation ID;
the existing reset/remove operations remain exact-selector-only. Base-store
objects have their separate exact per-base publication transaction and permanent
retention policy. There is no recursive cleanup or general scratch selector.

Fresh install preserves existing account/group creation and monotonic four-unit
publication rules. Provisioning happens only after a protected mapping/journal
has durably recorded the runtime/base pins, planned IDs/ports, install mode,
transaction ID, provisioning-generation ID, and exact fixed selectors. Reprovision
uses the same install operation schema with `mode=reprovision`, the retained
infrastructure-install ID and complete source unit/account evidence. Its account,
group, and unit records are verification-only; no creation intent, reallocation,
or republishing is allowed.

Each disk/seed/key/known_hosts stage follows durable intent, exclusive
transaction-derived creation, `fsync`, exact identity/content/coherence
verification, no-replace publication, directory `fsync`, and durable published
evidence. Key-pair recovery is coherent: never regenerate one half after another
published artifact embeds its counterpart. Journal records contain no private
key bytes. Unknown, foreign, mismatched, or ambiguous artifacts are preserved
and block progress. `ready` is published atomically only after all seven leaves,
the base relationship, accounts/groups, and four inactive/job-free unit
fragments/manager identities verify.

Reset first verifies stopped, job-free, and process-free units. It snapshots the
current provisioning generation and deletes only its seven exact leaves using
durable removal intents and descriptor-relative no-follow operations. Base,
mapping identity/runtime pin, resource allocations, service accounts/units, and
workspace assignment remain. Only after all seven leaves verify absent does one
atomic mapping commit set `state=unprovisioned`, clear current provisioning
evidence, and clear the reset transaction. Reprovision uses a fresh generation;
it does not reuse old disk or credential identities. Remove remains explicit,
keeps its mapping until the final commit, and never deletes the shared base.

## 6. Implementation acceptance sequence

1. **Reviewed contract/schema:** add the machine-readable v2 mapping schema and
   closed resource table; change the production parser and semantic fixtures
   together. V1/unknown versions fail before effects. Add compatibility tests
   proving the singleton and legacy development VM paths are unchanged.
2. **Image primitives:** build a separate strict overlay validator; preserve the
   existing flattened-image validator. Test exact base reuse, qemu-img JSON,
   backing/feature negatives, sparse capacity, umask/mode, base immutability,
   an fd-only header inspector that rejects data-file/backing-path fixtures
   without opening their named targets, exact-FD handoff across the check/start
   boundary, poisoned-header protection, and the explicit QEMU block graph.
3. **Journaled fresh install:** one workspace-null profile reaches `ready` with
   exact accounts, ports, independent overlay/seed/keys, and inactive units.
   Fault-inject every journal/fsync/rename boundary and prove exact-only resume.
4. **Public CLI vertical slice:** fixture-enrolled development host runs
   `install -> status -> up -> ssh/tui -> down`; `install` leaves units inactive,
   and guest readiness is demonstrated separately after `up`.
5. **Reset/reprovision and independence:** prove
   `reset -> unprovisioned -> explicit install` preserves profile identity, pin,
   allocations, units, workspace and base while generating fresh disk/keys; then
   prove two profiles share only the immutable base and cannot observe each
   other's writes or credentials.
6. **Broader gates:** finish runtime inventory/prune, admission, removal/recovery,
   namespace/confinement and concurrency acceptance. Hosted-KVM qualification,
   signed release bootstrap/enrollment, supported-host release trust, and
   physical-owner acceptance remain distinct and must not be inferred from
   fixtures or local image probes.
