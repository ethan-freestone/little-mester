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
- `verify_runtime_installation` opens the selected SHA tree without following
  links and checks the complete inventory, ownership, immutability modes, file
  link counts, size bounds and digests. `resolve_pinned_runtime` takes the SHA
  supplied by the caller from protected profile metadata and resolves only
  `/opt/little-mester/v3/<SHA>`; it does not inspect checkout `HEAD`.

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

## Explicitly not implemented by this slice

This module is not the installed/root-owned helper and is not dispatched by the
public `lab` CLI. It does not yet implement owner enrollment/authentication,
profile metadata read/write or atomic journaling, locks, signed bootstrap,
runtime extraction/install, systemd units, account/path allocation, start
admission, lifecycle, inventory/pruning, or TTY confirmation. Callers must not
use the validator as authorization and must not execute a runtime until the
fixed helper has validated protected profile metadata and holds the required
runtime lock. The fixed #20 singleton and the `feat/vm-approach` development VM
remain untouched.

The intended integration direction is: #70 constructs the bounded request and
routes it to the fixed helper; the helper authorizes the enrolled sudo caller,
loads the root-owned profile mapping, passes only its stored SHA to
`resolve_pinned_runtime`, and then dispatches fixed operations. The core accepts
no executable, unit name, QEMU option, environment, cwd or runtime path from a
request.
