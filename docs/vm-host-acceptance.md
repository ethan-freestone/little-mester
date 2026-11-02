# VM owner-host acceptance runner

`scripts/vm-host-acceptance.py` is a host-side acceptance tool for the physical
KVM owner host. It is not CI. Keep the runner, its tests, and this document
after merge so the bounded nested-KVM gate can be repeated after
launcher/provisioning changes. It does not update tickets or claim the broader
VM release is accepted.

## Before using it

The latest #35/#41/#45/#50 tickets remain the source of truth for gate order.
This runner automates only safe-snapshot preparation, outer-guest readiness,
and one bounded nested-VM gate. It does not perform model, TUI, browser-auth,
permission, workload, full-isolation, or teardown acceptance.

Run commands from the CachyOS checkout root. The stable snapshot path is
`$HOME/.cache/lab-vm-recursion/initial-launcher-smoke/workspace`; the default
outer profile is `launcher-smoke`.

`preflight` and `nested` repair the known stale-snapshot conditions without a
manual review/confirmation step:

- Stale allowlisted runtime files are replaced atomically; previous bytes are
  retained under the private acceptance evidence directory. Both source repair
  and full rebuild refuse to mutate a snapshot while an active export is
  detected. For the nested gate only, if the existing export is otherwise safe
  and only those allowlisted runtime files differ, the runner leaves the active
  snapshot untouched and stages the verified current files in a private `/tmp`
  source overlay inside the outer guest. Other stale/unsafe snapshot conditions
  still block; `preflight` does not use this fallback.
- If `.mester` control state is visible, the snapshot is rebuilt using
  `scripts/vm-recursion-workspace.py` exclusions. The old tree is retained as a
  private sibling backup. Rebuild is refused while a QEMU/virtiofsd process is
  exporting the snapshot; no live mount is changed.
- The VM-state path is resolved with the same environment-variable semantics as
  `lib/vm/lifecycle.sh`. A single matching live outer profile is reused. If it
  is missing, the runner checks an 8-GiB-plus-512-MiB RAM budget and provisions
  it with recursion depth 0/max 1. Unusable existing profile state is left
  untouched and a fresh unique profile is used instead.
- Profile creation does not pass `--yes`, auto-install host packages, replace a
  bad base image, stop/down/clean a VM, or delete acceptance evidence. It has
  stdin closed so an unexpected prompt fails instead of waiting for manual
  input. A failed start leaves its profile and diagnostics in place.
- If the outer guest's nested-VM tools are missing, the runner bootstraps only
  those prerequisites inside that guest, and only after every other guest
  preflight check passes. This is limited to Ubuntu/Debian guests with
  non-interactive `sudo`; it refreshes guest apt metadata and installs mapped
  missing packages with `--no-install-recommends --no-remove --no-upgrade`.
  Unsupported guests or install failures block the child launch. This can
  change the outer guest's package state, but never installs or upgrades
  packages on the physical host.

This automation is intentionally owner-host-only. On a VM/container or without
usable KVM/tools it makes no snapshot or VM changes.

## Commands

### Automated preflight

```bash
./scripts/vm-host-acceptance.py preflight
```

Checks host KVM/tools, reconciles the stable snapshot and outer profile as
described above, then verifies the outer guest's context, nested KVM, virtiofs,
4-GiB child RAM budget plus reserve, tools, and host-state exclusion. It does
not query model services or launch an agent/TUI. If only nested-VM tools are
missing and all other guest checks pass, it performs the bounded guest-only
bootstrap above and repeats preflight before proceeding. It writes a private
summary report and may start a missing outer profile.

### Bounded nested-VM gate

Run the full automated gate in one command. The child profile name is generated
uniquely and recorded in private evidence; `--profile` is optional if a stable
name is needed for a controlled retry.

```bash
./scripts/vm-host-acceptance.py nested
```

The runner starts one child from `/workspace` using `--recurse` (never
`--unsafe-recurse`), verifies cloud-init completion, child depth 1/max 1,
virtiofs and excluded host state, tries one depth-2 launch and confirms it is
refused before profile state is created, then checks the outer session remains
usable. It does not run an agent, model request, auth flow, workload, isolation
matrix, or teardown. **The child profile is retained on both success and
failure.** On failure, the runner stops and preserves the profile and
diagnostics. It never retries a failed child under the same name or cleans it
up. The child uses its private recursion-session directory as `VM_STATE_DIR`
and explicitly inherits the existing parent egress proxy for provisioning, so
startup does not chmod the outer guest's unrelated default XDG state directory
or try direct DNS/network access. This keeps the gate compatible with an
already-active snapshot whose `lab-vm` or disk helper source has not yet been
refreshed. The runner starts the child with `lab-vm -d up`, so virtiofsd debug
output is captured in the private child profile's `virtiofsd.log`; startup
failure details are also retained in `virtiofsd-startup.log`. Virtiofs sockets
use a private per-user short path under `/tmp/little-mester-vfs-<uid>/` rather
than the deeply nested profile path, which can exceed Linux's AF_UNIX pathname
limit. Existing live VMs using the legacy per-profile socket remain recognized.
If the stable snapshot is still actively exported and the only mismatch is an
allowlisted runtime source, the nested runner uses current host source files in
a private `/tmp` overlay in the outer guest; it does not replace files under
the mounted `/workspace` or restart the outer VM. This path transfers only the
hash-verified allowlisted runtime files, and leaves the child workspace as the
existing `/workspace` snapshot.
For a retry, wait until the targeted revision is present in the CachyOS checkout
and the current #35/#41 handoff permits it, then run this once; it generates a
fresh child profile:

```bash
./scripts/vm-host-acceptance.py nested
```

Do not reuse or clean a failed profile, edit the active snapshot manually,
restart the outer VM, bypass the parent proxy, or widen egress. If startup still
fails, stop and preserve the child/profile and private report. Use the report to
locate `virtiofsd.log` and `virtiofsd-startup.log`, then return the first failure
and a redacted tail of those files; do not retry before the result is reviewed.

### Guided human observations

After the current ticket order permits a human gate, run one of:

```bash
./scripts/vm-host-acceptance.py observe --gate tui
./scripts/vm-host-acceptance.py observe --gate auth
./scripts/vm-host-acceptance.py observe --gate permissions
```

These optional commands are the only interactive part: visual TUI behavior and
browser/permission interactions cannot be inferred from shell checks. They
record fixed yes/no/unknown answers (and, where relevant, OpenCode version and
permission mode), never prompts, URLs, OAuth state/codes/tokens, screenshots, or
raw logs. They are not prerequisites for or evidence of the automated nested
gate, and they do not update tickets.

## Post-merge disposition

Keep this runner, `tests/test_vm_host_acceptance.py`, and this document. No CI
job is needed. The safe-snapshot/profile reconciliation and nested gate are
repeatable acceptance support, not one-time merge scaffolding. Do not add
automatic VM teardown, ticket writes, model calls, or agent launches here.

## Evidence and exit status

Each command prints `PASS`/`FAIL`/`BLOCKED` checks and writes a minimal JSON
summary under
`${XDG_STATE_HOME:-$HOME/.local/state}/little-mester/host-acceptance/` with
directory mode `0700` and file mode `0600`. Raw command output is not copied
into the report. The nested command streams provisioning diagnostics to the
terminal and leaves native VM failure logs in the child profile; preserve and
redact those separately before sharing.

Exit status is zero only for a passed preflight/nested gate or a fully
positive human observation; blocked or failed gates return nonzero. A runner
PASS covers only the stated gate and must not be generalized to full VM,
model, TUI, auth, permission, workload, isolation, or merge acceptance.
