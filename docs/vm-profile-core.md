# Named VM profile control-core implementation slice

This note records the first executable slice of the approved contract in
[`vm-usability-integration.md`](vm-usability-integration.md). It does not mark
the profile system or ticket #67 complete.

## Current code seam

`lib/vm/profile-control-core.py` is a side-effect-free core for the future fixed
`/usr/libexec/little-mester-vm/profile-control` helper:

- `read_request`, `parse_request` and `validate_request` enforce protocol v1, bounded JSON,
  duplicate-key rejection, exact request fields, profile-name grammar and typed
  values. The request schemas cover the ordinary profile commands listed in the
  integration baseline; protocol v1's framed `control.update` operation is not
  implemented in this slice and must integrate with the signed common
  bootstrap/update path.
- `validate_runtime_manifest` checks the exact v1 install-manifest fields,
  sorted unique runtime inventory, approved file modes/paths, SHA pin, and helper
  and runtime API compatibility.
- `validate_runtime_descriptor` checks the candidate's exact committed
  `profile-runtime.json` schema, fixed entrypoint, unique bounded file inventory,
  approved Git modes/paths, and helper/runtime API range before a future installer
  reads any declared blobs.
- `verify_runtime_installation` opens the selected SHA tree without following
  links and checks the complete inventory, ownership, immutability modes, file
  link counts, size bounds and digests. `resolve_pinned_runtime` takes the SHA
  supplied by the caller from protected profile metadata and resolves only
  `/opt/little-mester/v3/<SHA>`; it does not inspect checkout `HEAD`.
- `lib/vm/profile-runtime-install.py` is a standalone extraction primitive for
  the future helper. It asks fixed Git plumbing to read only the invoking
  owner's exact committed `HEAD`, verifies commit/tree/blob object IDs and the
  descriptor, extracts bounded bytes without checkout filters or execution,
  verifies a generated manifest, and publishes by a same-parent no-replace
  rename. Interrupted `.staging-<SHA>-<nonce>` siblings are preserved for
  inspection. It assumes the future helper already authorized the owner and
  holds the maintenance/profile/SHA locks; it is not installed or dispatched.

The current manifest filename convention in this code is `manifest.json`; its
schema-v1 inventory key is `files`. The
profile-runtime install path must generate that exact name and schema, or a
reviewed compatibility change is required before the two sides are integrated.
The bootstrap owns and validates the protected `/opt/little-mester/v3` ancestry;
the runtime verifier then opens each store component with `O_NOFOLLOW` and
validates the selected immutable SHA directory.

The request field spellings are `profile`, `repository`, and `commit_sha` for
`install`; `profile` plus optional `workspace`, `unsafe_recurse`, and `reassign`
for `up`; `profile` plus optional `model`/`theme` for `settings.set`; and
`dry_run` for `prune`. Other ordinary operations take only their profile name,
or no profile field for list/inventory. The public CLI remains #70-owned; its
request builder must use these exact names or coordinate a reviewed protocol
change before dispatching to this core.

## Maintenance substrate interface and gap

`lib/vm/control-maintenance.py` defines the fixed root-owned
`/run/little-mester-vm-control` mode-`0755` directory and its single-link,
root-owned mode-`0600` `maintenance.lock`. The explicitly authorized future
bootstrap/fixed-system installer may call `ensure_maintenance_substrate()` to
create missing objects exclusively; it validates but never repairs existing
objects. Ordinary dispatch uses `maintenance_lease(*, exclusive=...)`, which
requires the substrate to exist, takes a nonblocking shared or exclusive
`flock`, and yields the close-on-exec lock descriptor. `Busy` means lock
contention; `Refused` means missing, unsafe, or changed substrate. Descriptor-
relative no-follow opens and path/descriptor identity checks guard acquisition.
The module does not authorize callers, install itself, create transaction
markers/journals, or acquire the separate code-update lock. These are future
bootstrap/helper responsibilities; ticket #67 acceptance criterion A7 remains
in progress.

## Explicitly not implemented by this slice

These modules are not the installed/root-owned helper and are not dispatched by
the public `lab` CLI. They do not yet implement owner enrollment/authentication,
profile metadata read/write or atomic journaling, catalog/profile/SHA locks, signed bootstrap,
systemd units, account/path allocation, start admission, lifecycle,
inventory/pruning, or TTY confirmation. The installer primitive is not
authorization: callers must not use it directly or execute a runtime until the
fixed helper has validated protected profile metadata and holds the required
locks. The maintenance lease is only a fixed nonblocking substrate primitive;
it is not caller authorization or a transaction protocol. The fixed #20
singleton and the `feat/vm-approach` development VM remain untouched.

The intended integration direction is: #70 constructs the bounded request and
routes it to the fixed helper; the helper authorizes the enrolled sudo caller,
loads the root-owned profile mapping, passes only its stored SHA to
`resolve_pinned_runtime`, and then dispatches fixed operations. The core accepts
no executable, unit name, QEMU option, environment, cwd or runtime path from a
request.
