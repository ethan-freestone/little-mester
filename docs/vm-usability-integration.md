# VM usability integration: shared profile and CLI contract

**Status:** design baseline for tickets #66–#70; implementation is in progress.
Protocol/runtime validation and a standalone committed-tree installer primitive
exist, but the fixed helper, signed bootstrap/update path, profile lifecycle and
CLI integration are not implemented. The profile and CLI streams must use this
contract or obtain an explicit reviewed change to it before implementing
divergent behavior. The integration PR remains based on
`e10ebd76e1e362c7b6e95bdb631d12c22b2e2d7f` on
`feat/vm-useability-tweaks`, targeting `feat/vm-approach-3`.

## Three distinct identities and a narrow host control plane

1. **Repository checkout** is development source and the public `./lab` CLI.
   For named profiles, only an explicit `install` reads its committed full
   `HEAD` SHA as a candidate.
2. **Installed runtime** is protected, immutable code at
   `/opt/little-mester/v3/<full-commit-sha>`. An install is verified against
   that commit and may be shared by profiles that pin the same SHA.
3. **Profile** is an independently persistent VM identity and state set. Its
   protected metadata pins one installed runtime SHA and owns its disk, seed,
   keys/known-host identity, workspace assignment, network resources, units,
   metrics state, service identities and configuration.

Changing branches or files in a checkout must not rebuild, invalidate, or
retarget any installed profile. Commands that execute or start profile runtime
code resolve the profile's recorded SHA, validate that exact protected
installation, and dispatch through it; they must not derive the runtime from the
caller's current `HEAD`. Fixed-helper safety operations (`status`, `down`,
`reset`, `remove`, and profile display) may operate from valid protected profile
metadata when the runtime is missing, but never execute candidate code and
report the invalid pin. Invalid/unsafe profile metadata still fails closed. Profiles can pin
different SHAs. Identical immutable runtime bytes may be shared, but mutable VM
state and host resources may not. Metadata is protected from the guest and
workspace, validated fail-closed, and changed atomically.

An explicit future `update` may change a profile's SHA reference after separate
compatibility/preservation design. **No profile update, upgrade, or migration
command is part of this integration PR.** Re-installing an existing profile at a
different SHA must refuse without changing its state; it must not act as an
implicit update. Missing, unsafe, or incomplete runtime references fail closed
for operations that execute runtime code. Fixed-helper safety operations
(`status`, `down`, `reset`, `remove`, and profile display) may still operate from
valid protected profile metadata when the pinned runtime is damaged or missing.

The fixed host control plane is one small, root-owned helper at
`/usr/libexec/little-mester-vm/profile-control`, with an explicit protocol
version. It owns protected profile metadata, per-profile unit generation,
operation/runtime locks, the fixed bounded profile QMP stop path, and
installation inventory/pruning. Its request is a
bounded versioned record containing only an allowlisted operation, profile name
and operation-specific validated data; it accepts no arbitrary executable,
unit, QEMU option or privileged path. It never executes code from the mutable
checkout or guest workspace. It resolves and verifies the profile's manifest,
then launches the profile runtime from that exact immutable `/opt/.../<SHA>`
tree as the fixed owner; generated profile systemd units likewise execute the
exact SHA-addressed runtime. Profile installation cannot replace this helper,
upgrade another profile, or alter the existing `/usr/lib/little-mester-vm`
singleton runtime/units. A new runtime must declare a compatible helper API;
mismatch refuses with a safe diagnostic. Updating the helper itself is a
separate explicit owner operation, not an effect of `install`.

The host control plane uses one distribution-independent signed control
artifact and installer, not a native OS package. Native package managers are
used only by the small platform adapter to install required system dependencies.
The common artifact contains the fixed helper, minimal virtiofs root, exact
systemd guard files, the `systemd-tmpfiles` rule for its root-owned runtime-lock
directory, API/schema versions and a root-owned manifest. A host
administrator explicitly enrolls the project's release-signing key in a
root-owned trust store. For first bootstrap, `./lab vm bootstrap` places the
portable bootstrap executable and detached signature in a root-owned mode-`0700`
staging directory using fixed system utilities, verifies those staged bytes
with the host's `gpgv` and enrolled key, then requires explicit administrator
authorization before executing the signed bootstrap. The same flow works on
every supported host. It does not execute repository checkout code or trust a
key/executable supplied by the checkout.
It installs the fixed helper, exact sudoers rule, root-owned enrollment record,
and singleton guard files. Later helper updates use the same signed common
artifact and fixed-helper transaction; they are explicit and separate from
profile/runtime `install`. The owner-facing commands and artifact format are
identical on Ubuntu, Debian, Arch and CachyOS; the prerequisite adapter maps
only those four validated distribution IDs. Missing trust enrollment or a
missing/incompatible helper is reported as a bootstrap prerequisite, not a
checkout-based `sudo` fallback.

`./lab vm bootstrap [--recover]` is the explicit administrator-authorized
first-time/recovery setup; `./lab vm control update [--recover]` is the
owner-authorized update/recovery entrypoint. Neither
uses apt, pacman, ALPM hooks or distribution-specific package scriptlets to
install Little Mester. The platform adapter may use apt or pacman for declared
host dependencies only, with the existing preview/approval guarantees.
That adapter is the only distro-specific branch: it requires supported Linux,
a responsive systemd manager, accessible `/dev/kvm`, and the fixed host tools
needed by QEMU, virtiofsd, networking and the signed bootstrap verifier (including
`gpgv`). It previews and interactively installs only declared missing system
dependencies; it performs no system upgrade and makes no lifecycle or control-
artifact decisions. After this prerequisite gate, helper, runtime, profile and
systemd behavior is distribution-independent.
The common artifact installs a `systemd-tmpfiles` rule for the root-owned,
non-writable mode-`0755` `/run/little-mester-vm-control` directory. Before that
rule exists, the signed bootstrap or root fixed-system installer establishes
the directory with no-follow, descriptor-relative operations under `/run` and
validates its root ownership/mode; it creates the root-owned, single-link
mode-`0600` maintenance lock with `O_CREAT|O_NOFOLLOW` and verifies the opened
inode before locking. Any pre-existing symlink, unsafe parent or unexpected
file identity refuses. Maintenance, catalog, profile and host-admission locks
are root-owned, single-link regular files mode `0600`; immutable-SHA lock files
are root-owned mode `0644` so profile services can open them read-only for
shared locks, while only root can take an exclusive prune lock. The helper and
service wrapper use no-follow opens and validate identities on every
acquisition; no service account can replace a lock file.
Enrollment is one owner UID per host, recorded in root-owned
`/etc/little-mester-vm/control-owner.json` and added to
`lm-vm-profile-control`; the helper verifies both that record and effective
group membership. A new login is required after group membership changes.

Every ordinary owner helper request acquires a shared
`/run/little-mester-vm-control/maintenance.lock` lease before reading profile
state and refuses if a root-owned control-transaction
marker exists. It holds the lease through helper-controlled host effects and
releases it before execing a client-only operation from the already-verified
immutable runtime. Common bootstrap/update/recovery use the lease exclusively;
only the matching signed recovery operation may inspect its own existing marker.
The control-artifact updater refuses while any managed profile unit/job is
active or pending.

The fixed singleton's current `system-acceptance.py` verifier requires exact
ordered `ExecStartPre` commands, but it only requires the expected code-update
drop-in to be present for the VM, proxy and metrics units; it does not reject all
extra drop-ins there. The protected updater/rollback verifier has the same
presence-only gap. This integration must tighten both verifiers, not weaken
them: with no common control manifest or profile guards, accept only the exact
baseline drop-in sets `{90-code-update-guard.conf}` for the VM, proxy and
metrics units. The workspace unit has two exact valid states: with a validated
workspace assignment, its set is `{20-workspace.conf, 90-code-update-guard.conf}`
and the `20` bytes must match that assignment; with no assignment, it must be
stopped and its set is `{90-code-update-guard.conf}` with no `20` drop-in or
workspace bind. With the common artifact installed, add only the exact
artifact-owned `95-profile-exclusion.conf` to the corresponding sets, plus
exactly one appended, ordered
`singleton-start-guard` command. The root-owned control manifest at
`/usr/libexec/little-mester-vm/control-manifest.json` binds artifact identity,
helper API/version and the exact bytes/digests of all four guards. Both
verifiers validate the no-follow root-owned, non-writable path ancestry, file
identity/mode, manifest schema and guard digests before
allowing the expanded sets. A missing/invalid manifest with any profile guard,
or a present manifest with missing/modified guards, fails closed; it never falls
back to the baseline. Any other drop-in, command, ordering, fragment or base-
unit difference fails.
Both verifiers explicitly recognize the no-assignment state only as a stopped
static installation layout; the full live owner-acceptance/TUI path still
requires an assigned workspace. If an assignment exists without the exact `20`
drop-in, or the drop-in exists without a valid assignment, verification fails.
The current `system-acceptance.py` reads an assignment unconditionally, so its
static installation-layout verification must gain this distinct no-assignment
case without weakening the live acceptance gate.
This verifier/owner-runner compatibility is owned by #70 and coordinated with
#67; it does not change the base units or `/usr/lib` singleton runtime. Tests
must show the fixed singleton passes its existing owner-acceptance path both
before and after common bootstrap, and rejects a modified/unowned guard.
The separate protected updater/rollback verifier in
`tools/vm-system-install._verify_effective_unit_guard` must accept the same
exact baseline or manifest-bound guard set and command order. Its code-only
update and rollback paths remain supported; they may not overwrite profile
guards or accept extra drop-ins. This compatibility is also owned by #70.

The common control-artifact bootstrap/update is one journaled transaction on all
supported distributions. The one-time signed bootstrap artifact is explicitly
authorized by the host administrator and verified against the administrator-
enrolled release key before it publishes the helper. It does not use apt/pacman
hooks or execute checkout code. Subsequent `./lab vm control update` requests
pass the candidate signed control bundle as bounded data on stdin to the
installed helper; no owner-controlled path or environment selects the root
artifact. The helper verifies the artifact signature and descriptor against the
enrolled key before mutation.

Control bootstrap/update acquires the exclusive
`/run/little-mester-vm-control/maintenance.lock` lease and
the fixed-system code-update lock, both nonblocking and in that order, then
takes the catalog and host-admission locks exclusively. When all four fixed
unit names are absent, initial signed bootstrap may establish a narrowly
allowlisted substrate while holding the maintenance lease: create/validate only
the exact `lm-vm-control` group, empty root-owned
`/etc/little-mester-vm` directory (mode `0750`, group `lm-vm-control`), and
empty root-owned `code-update.lock` (mode `0660`, same group, single link). Any
unexpected entry, membership, owner, mode, symlink or nonempty lock refuses.
This substrate step is idempotent and safe to retry if interrupted before the
transaction journal/marker, because the all-absent/no-state precondition proves
no VM unit or profile can be started; it never removes ambiguous objects. With
the exact lock established, bootstrap acquires it before publishing its
`profile-control` journal/marker or installing helper/guard assets. If fixed
units are installed, the config directory and existing code-update lock must
already validate; bootstrap does not reconstruct a missing legacy interlock.
Before first
bootstrap, the fixed singleton must be in one of two provable states: all four
component units are installed with exact `90-code-update-guard` drop-ins and
effective marker/lock guards (and the workspace drop-in matches its assignment,
or is absent together with the assignment), or all four fixed unit names are absent (not
masked) with no pre-existing unit drop-ins, singleton state, or queued jobs. In
the all-absent case bootstrap installs the manifest-bound `95` guards in
advance; a later fixed-system installer must preserve and verify them before
loading any singleton units. A partial, masked, malformed or otherwise
unverifiable component set refuses without mutation.
A normal bootstrap/update refuses
if either transaction marker or any incomplete fixed-system code-update/rollback
journal already exists. `--recover` is the sole exception: it accepts only a
well-formed `profile-control` marker with the matching durable journal and signed
artifact, and refuses a fixed-system marker or any unknown owner/schema. It
validates every profile mapping/schema, runtime manifest/API, the
candidate helper's declared supported API/schema ranges, and systemd state. It
refuses before publishing files if any record is malformed, any profile unit/job
is active or pending, any fixed-singleton component is active/activating/
deactivating or has a queued job, or the candidate would strand a profile.
Initial bootstrap additionally requires an empty profile catalog and no
existing profile units.

The legacy fixed-system updater and rollback must also acquire the exclusive
`/run/little-mester-vm-control/maintenance.lock` lease before the existing
fixed-system code-update lock, nonblocking,
so ordinary profile helper operations and singleton guards cannot overlap the
transaction. Both implementations use one bounded, root-owned mode-`0600`
versioned marker schema at `/etc/little-mester-vm/code-update.in-progress`:
`schema_version`, owner (`protected-fixed-system` or `profile-control`),
operation, unique transaction ID, candidate SHA/artifact SHA and the durable
journal identity. Its durable journal starts at a `prepared` phase and is
atomically published/fsynced before the marker; no protected asset changes
before both are durable. The systemd guards depend only on marker-path existence,
not its contents. The legacy updater/rollback writes its typed marker and durable
snapshot/journal before its first protected-file or unit-guard mutation; the
present `tools/vm-system-install` ordering writes/changes permanent guards
before the marker, so changing both update and rollback ordering is an
implementation requirement, not already-satisfied behavior.

The existing exact `protected-code-update-in-progress` bytes are recognized only
as a legacy v0 `protected-fixed-system` marker by the fixed-system rollback path.
Recovery requires explicit candidate SHA and one unambiguous durable snapshot
that proves the matching incomplete transaction; if multiple snapshots or
phases could match, refuse and preserve evidence. The common control installer
always treats a v0 marker as foreign and refuses it. New transactions never
write v0 markers. If a crash leaves a `prepared` journal without its marker, the
matching `--recover` operation may proceed only after proving every target still
matches the journal's pre-transaction identity; otherwise it refuses without
mutation. A marker without its matching journal, or any owner/transaction/SHA
mismatch, is never cleared or adopted. The common installer refuses any
pre-existing foreign marker and holds the exclusive code-update lock from
preflight through staging, atomic publication, `daemon-reload`, effective-unit
verification and commit.
The existing `90-code-update-guard` blocks singleton starts by both shared-lock
contention and marker presence. Ordinary profile helper requests refuse while
the marker exists.

The fixed-system update/rollback path recognizes a `profile-control` marker as
foreign and refuses before mutation; it never validates, overwrites or clears it.
Conversely, the common installer refuses a `protected-fixed-system` marker and
any incomplete fixed-system journal. These marker ownership and shared-lock
rules ensure neither recovery path can consume the other's transaction. On
success, only the owning transaction removes its marker after verifying the
exact marker owner/transaction ID, journal identity and effective systemd
configuration; the common control installer also verifies the installed signed
manifest. A committed journal with a leftover marker is reverified before
clearing. A crash releases the lock but leaves the marker and journal; no timeout or ordinary owner request
clears them. `./lab vm control update --recover` may resume or roll back only the
matching signed transaction after revalidating every journaled object. If that
proof fails, retain the marker for administrator inspection; there is no
unverified recovery bypass. If the installed helper itself cannot execute,
`./lab vm bootstrap --recover` uses the administrator-authorized signed
bootstrap artifact to recover only a matching `profile-control` bootstrap/update
journal; it cannot clear or adopt a fixed-system code-update marker.

Fresh fixed-system installation is also part of the protected transaction
contract. `tools/vm-system-install` acquires the exclusive maintenance lease
first. If `/etc/little-mester-vm` or `lm-vm-control` does not yet exist, it
safely creates only that exact root-owned config directory/group substrate while
holding the lease, validates the identities, then creates/acquires the existing
code-update lock (root:`lm-vm-control`, mode `0660`). When the common control
artifact is already installed, its exact manifest-owned files, enrollment and
profile directories/data are expected and preserved; unexpected contents,
memberships, ownership or modes outside that allowlist refuse. With the lock
held, it rechecks that all
four fixed unit names are absent (not masked), with no state, jobs or existing
unit drop-ins; if the common artifact is installed, its four manifest-bound
`95` guards must already be present and valid. The workspace unit is stopped and
unassigned, so the accepted set contains `90` and optional manifest-owned `95`,
but no `20` workspace drop-in. The fresh install publishes its
`protected-fixed-system` `install` journal and typed marker before creating VM
accounts, state directories, unit files or other protected assets. It installs
the `90-code-update-guard` drop-ins before publishing base unit fragments,
retains both exclusive locks through provisioning, `daemon-reload` and exact
effective-unit verification (baseline or manifest-bound guards, including the
no-assignment/no-`20` state), and removes
the marker only after commit. It never starts/enables the VM. A failed install
leaves its marker/journal; recovery may resume or roll back only exact
transaction-owned objects and never deletes pre-existing/unknown VM state.
The current fresh-install path creates accounts/assets before acquiring the
code-update lock and releases that lock before installation; moving lock
acquisition and adding this journal/marker/final-verification boundary are
implementation requirements.

Removing the common helper/guards is a separate explicit comprehensive-cleanup
operation owned by #57, executed by the signed bootstrap runner rather than by
the helper that is being removed. It takes the exclusive maintenance lease,
code-update lock, catalog lock and host-admission lock in that order, all
nonblocking. It refuses while any profile mapping (including an incomplete
transaction) remains, any profile unit/job is active or pending, any
fixed-singleton component is not inactive/job-free, any lock is busy, or
explicit owner cleanup authorization is absent. It writes/fsyncs a
`profile-control`/`remove` journal and typed marker before mutation, removes
only the exact manifest-owned `95` guards, helper, control manifest, minimal
virtiofs root, sudoers rule, owner-enrollment record, tmpfiles rule and
dedicated owner-control group (the group is removed only if it has the recorded
identity and no unexpected members). It daemon-reloads, then verifies either
the exact baseline `90` state on all four installed singleton units (workspace
`20` exactly matches an assignment, or is absent together with the assignment),
or the
provably absent/no-state/no-drop-in singleton state accepted by bootstrap. The
transaction marker is removed only after the final verification and commit. It
preserves the fixed singleton's `90` code-update guards, base units,
`/usr/lib` runtime, disk and credentials; the original development VM, all
workspaces and all installed SHA runtimes are outside the remover's allowlist.
It also preserves the administrator-enrolled release key. The signed bootstrap
artifact remains available for recovery if the helper is gone: recovery verifies
the same transaction and may resume removal or restore the exact signed control
artifact. No timeout or ordinary helper request clears an interrupted marker.
After commit, it leaves the root-owned lock files/directory under `/run` until
reboot rather than unlinking a lock inode while held; it removes the tmpfiles
rule so the transient lock directory is not recreated. The marker is transaction
state, not a persistent profile-reference database.
Unknown helper API or metadata schemas refuse before profile metadata, accounts,
units, or VM state are mutated. The legacy `/usr/lib/little-mester-vm` singleton
is not a bootstrap source and is neither copied nor updated.

For profile `install`, the checkout-side CLI selects only its full committed
`HEAD` and supplies the repository path as data to the authorized helper. The
helper validates the request and owner, then obtains the commit/tree/blob bytes
using fixed `/usr/bin/git` operations run with the invoking owner's UID and a
sanitized environment (`GIT_NO_REPLACE_OBJECTS=1`, `GIT_NO_LAZY_FETCH=1`,
`GIT_CONFIG_NOSYSTEM=1`, isolated empty global config/home, and system/global
config, hooks, filters, pagers and fsmonitor disabled). It verifies `HEAD`
equals the requested full SHA, reads the
committed `profile-runtime.json` as data, and validates its exact schema:
`schema_version`, `profile_runtime_api`, `control_helper_api_min`,
`control_helper_api_max`, the fixed `entrypoint` `lib/vm/profile-runtime.py`,
and a unique list of `{path, git_mode}` runtime files. Every listed path must
exist in the committed tree as a regular blob with mode `100644` or `100755`;
links/gitlinks and paths outside the exact ASCII relative-path grammar are
rejected: a path must begin `lib/vm/` or `agent/opencode/plugins/`, contain only
`[A-Za-z0-9._/-]`, have no empty, `.` or `..` component, no backslash, and no
leading/trailing slash. Only this declared runtime inventory plus the descriptor
is installed;
other checkout files are ignored. The helper reads each named blob directly; it
does not use checkout/archive hooks, filters, or execute candidate code. The
root-owned extractor rejects traversal, duplicates, undeclared archive entries
and unsafe modes, and writes a temporary root-owned tree using dirfd/no-follow
operations. The fixed bounds are 4,096 entries, 256 MiB total, 16 MiB per blob,
and 240 bytes per path; exceeding any bound refuses before publication. It
compares every blob digest and inventory entry with the Git tree, fsyncs, then
atomically publishes `/opt/little-mester/v3/<SHA>` and a root-owned manifest.
Directories are root-owned mode `0555`; Git `100644` files become `0444`, Git
`100755` files become `0555`; the manifest is root-owned mode `0444`. No hard
links, writable bits, special files or extra paths are accepted. The descriptor
`profile-runtime.json` is one additional manifest inventory entry with its own
digest; it does not need to self-list, avoiding recursive hashing.
Candidate code remains data throughout installation;
later lifecycle execution is only from the verified pinned tree under its
profile service identity.

The owner-facing helper is invoked only as the literal sudoers command
`/usr/libexec/little-mester-vm/profile-control` with **no arguments** by members
of `lm-vm-profile-control`; the installed sudoers rule grants no shell, wildcard, `SETENV`
or environment preservation. The helper also has a separate fixed internal
systemd-only `singleton-start-guard` mode named literally in the artifact-owned
drop-in, plus a fixed `profile-unit-stop` mode named by generated profile units;
neither is available through the sudoers rule or owner request schema.
Both systemd-only modes take a shared maintenance lease and fail closed on the
maintenance marker. `profile-unit-stop` does not reacquire a catalog/profile
lock already held by the initiating `down` operation.
The owner-facing helper reads one size-bounded JSON request from stdin
and validates an explicit `protocol: 1`, exact keys, enum operations and
profile-name grammar. It derives the authorized owner UID only from sudo's
reset `SUDO_UID`/`SUDO_USER` invocation context, requires that UID to equal the
root-owned enrolled owner and be in the dedicated control group, and rejects
direct/root or mismatched invocations; a request cannot supply or override
caller identity. Owner-facing control operations reject a direct/root invocation.
The only privileged operations are the explicit signed common bootstrap, the
fixed owner-request protocol, and literal systemd singleton/profile-stop modes;
each accepts only its exact typed operation and is not a general root shell or
package-manager hook. The VM, proxy and
workspace service identities are never members of the owner group. Protocol v1, profile metadata
schema v1, install manifest schema v1 and profile-runtime API v1 are separate,
explicit version fields. Unknown or incompatible versions fail before effects;
ordinary operations never migrate metadata or update the helper.

Protocol v1 has operation-specific exact schemas: `install` accepts only the
profile name, repository path and candidate full SHA; `control.update` accepts
only a transaction operation and a bounded signed artifact payload framed after
the JSON header (the declared length and digest must match); recovery accepts
only the matching transaction ID and signed artifact. Ordinary lifecycle
operations accept a profile name plus only their documented typed fields;
`settings.set` accepts only model ID/theme; inventory/prune accept only
`dry_run`; reset/remove accept only the profile name. No operation accepts
arbitrary argv, executable, unit name, environment, `cwd`, SSH command, or
privileged path. The helper canonicalizes workspace/repository paths as data and
validates ownership, mount identity and overlap before use. Destructive
confirmation happens inside the fixed helper through `/dev/tty`; it prints the
effect summary and requires the exact operation/profile or candidate-SHA
confirmation. `--yes` and noninteractive stdin cannot bypass it. Prune's
confirmation string names the complete sorted set; any change on locked rescan
aborts and requires a fresh preview.
No ordinary owner operation is accepted while a control-transaction marker
exists. The fixed `control.update --recover` path is the sole helper exception:
it requires the exact journaled transaction ID and signed artifact, verifies the
marker owner/inode and every old/new object identity, then resumes or rolls back
only that transaction. It cannot clear a fixed-system code-update marker. If
the helper cannot run, the administrator repeats the signed common bootstrap in
recovery mode; unverified state is retained for inspection. No marker is
auto-cleared by timeout.

The owner request arrives on stdin, then interactive operations reopen the
controlling terminal through `/dev/tty` for confirmation or terminal I/O; they
never treat piped request bytes as user input. For `ssh`/`tui`, the helper
requires a controlling TTY, replaces fds 0/1/2 with that terminal, and execs the
pinned runtime in the caller's foreground process group. It emits no JSON on the
terminal stream; sanitized refusal text goes to stderr and the SSH exit status
is returned unchanged.

The helper is the only root control-plane implementation: it owns profile CRUD,
metadata, authorization, accounts, unit generation, operation locks, systemd
actions and GC. It has no general privileged RPC channel. It performs each root
effect itself from fixed OS operations and validated metadata; runtime code
cannot request new root actions after dispatch. `up` commits any requested
workspace assignment, performs admission, starts the generated units, then launches the pinned client
entrypoint for guest readiness. `down` calls systemd's fixed profile units,
whose helper-owned `ExecStop` invokes only the bounded QMP `quit` for
that exact profile unit/MainPID/socket and waits for the verified QEMU process
to exit; this stop path remains usable if the pinned runtime tree is damaged.
`status`, install/create/reset/remove, profile display, inventory and prune are
handled by the helper. For `ssh`,
`auth`, `tui`, guest-level `verify`/`accept`, and supported guest settings, it
resolves and verifies the profile manifest, then executes one fixed entrypoint
from that exact `/opt/little-mester/v3/<SHA>` tree with an allowlisted operation
enum, sanitized environment and bounded typed context passed over a private
file descriptor. Python entrypoints use fixed `/usr/bin/python3 -B -I` (`-B`
prevents bytecode from modifying the immutable installation). Client operations
run as the enrolled owner; VM/proxy service operations run as their profile
service account. Service processes receive only fixed generated unit arguments
and profile-scoped writable roots. The helper never loads checkout Python/shell
modules as lifecycle code. Profile CRUD, systemd lifecycle, inventory and pruning
are control-plane operations on the fixed helper, not separate copies of the VM
lifecycle engine. The old global
`prepare-dependencies` command remains its documented compatibility exception
and cannot select or mutate a profile.

The fixed SSH/TUI operation has no caller-supplied argv, remote command, port,
key path or SSH option. It uses the profile's allocated loopback port and
canonical control-key/known-host files, validates `TERM` against a bounded
terminal-name grammar, and execs fixed `/usr/bin/ssh -F /dev/null -tt` options:
strict pinned-host verification, `IdentitiesOnly=yes`, `IdentityAgent=none`, no
password/keyboard-interactive authentication, no agent/X11/local/remote/dynamic
forwarding, no proxy command/jump, `EscapeChar=none`, and the single fixed guest
command `/usr/local/bin/lm-opencode`. The guest command attaches to its existing
tmux session; disconnect does not stop the guest. The host SSH control private
key is generated once per profile into its canonical owner-owned `0600` file
through a unique same-directory temporary name, verified and atomically renamed; partial
files are cleaned only if their inode matches the operation journal. It is
never copied to `/tmp`, inherited from an agent, put in argv/environment/logs,
or staged in a workspace. The guest SSH host key is generated as protected
profile state, and its public half is pinned into `known_hosts` before the first
connection through the fixed seed/readiness path (not `ssh-keyscan` TOFU). Reset
removes both identities so the next guest is a new identity.

Profile metadata is the authoritative profile-to-SHA mapping, stored as one
root-owned atomic JSON record at
`/etc/little-mester-vm/profiles/<name>/profile.json`. Schema v1 has exactly the
owner UID, validated name, lifecycle state, nullable versioned transaction
record (operation, phase, durable step and expected manager-owned object
identities), runtime SHA and API versions, generated service account names and
planned/committed UID/GID values, deterministic unit names, workspace canonical
path plus filesystem identity/mount identity, and allocated SSH port; it
contains no credentials. Unknown fields/schema versions fail closed.
State, secrets/SSH identities and runtime sockets are under profile-scoped fixed
roots, never under `/opt` or the exported workspace. No separate
installation-reference database is introduced.

The design baseline prefers dedicated **static non-login system accounts per
profile** (separate VM/metrics and proxy roles), with generated collision-checked
names, no home directory, and exact profile-scoped filesystem permissions. This
is a design choice, not an owner-mandated systemd mechanism; a reviewed
`DynamicUser` alternative is acceptable only if it proves the same persistent
cross-unit disk/seed/socket ownership, profile isolation and cleanup contract.
The static baseline is chosen because the disk, seed, guest identity and
profile-owned files retain stable ownership across restarts and are shared by
the VM and metrics units; proxy state has a second stable identity. A
`DynamicUser` plus `StateDirectory=` could persist a single unit's state, but
cross-unit disk/seed ownership, the virtiofs host-owner service and cleanup need
additional systemd coordination. Static accounts are ordinary OS users with no home or
login, and are created/removed by the CLI helper; this is not a general identity
framework. Removal verifies the
generated account identity and absence of running processes before deleting
only those accounts; it verifies journaled UID/GID, system-range IDs,
groups, nologin shell, empty home and manager marker, and never runs a recursive
home deletion. A service for profile A must fail DAC access to profile B's
private state, while both may read the same root-owned immutable runtime. The
helper derives a bounded account/unit suffix from the host owner UID
and profile name (for example, separate `lmv-<digest>` and `lmp-<digest>`
accounts); the exact generated names are stored in that profile's protected
mapping and checked for collisions. The VM account alone receives supplementary
`kvm`; neither account is a member of the owner-control group. No arbitrary
profile name is interpolated into an account, unit, or filesystem path.

Each profile has isolated roots under
`/var/lib/little-mester-vm/profiles/<name>`,
`/etc/little-mester-vm/profiles/<name>`, and a private profile runtime directory
under `/run`. The fixed mutable layout is
`/var/lib/little-mester-vm/profiles/<name>/vm/{disk.qcow2,seed.iso,ssh_host_ed25519_key,ssh_host_ed25519_key.pub}`,
`/var/lib/little-mester-vm/profiles/<name>/client/{control_ed25519,control_ed25519.pub,known_hosts}`,
the authoritative mapping `/etc/little-mester-vm/profiles/<name>/profile.json`,
and runtime sockets/port files under `/run`; no other path is implicitly part
of a profile. The per-profile state parent is root-owned mode `0711`; `vm/` is
VM-service-owned mode `0700`, while `client/` is enrolled-owner-owned mode
`0700`. The `/etc` profile directory and mapping are root-owned mode `0700` and
`0600` respectively. Per-profile systemd units use distinct
deterministic names and write allowlists; their `ExecStart` references that
profile's exact `/opt/little-mester/v3/<SHA>` code. The profile VM/metrics
identity alone can access its private VM state; the proxy identity alone can
access proxy state. Disks/seeds and private service files are mode `0600`, and root-owned profile
JSON/config is mode `0600`. The client-key directory is enrolled-owner-owned
mode `0700`; the SSH control private key is owner-owned mode `0600` (so OpenSSH
accepts it), its public key is mode `0644`, and `known_hosts` is owner-owned
mode `0600`. The guest host private key is VM-service-owned mode `0600`; neither
private identity is exposed through virtiofs. The virtiofs socket is mode
`0660` in a per-profile mode-`2710` runtime directory owned by the enrolled
owner UID and grouped to the profile VM account's private primary
group; the setgid bit gives the socket that group.

Workspace sharing is the deliberate host-owner identity exception: it reads the
owner's assigned workspace without recursively changing its ACLs, but it must
not see any other host path or same-owner process. The unit runs `virtiofsd`
inside an artifact-provided, root-owned, non-writable minimal `RootDirectory` that
contains only the fixed daemon, its required libraries, `/dev/null`, and the
minimal runtime files. Systemd bind-mounts exactly the canonical assigned
workspace at `/workspace` and that profile's socket directory at `/run/profile`;
no other host directory is visible. The workspace and private socket-directory
binds are the only writable host binds; the minimal root image and every other
path remain read-only or inaccessible. The socket directory is owned by the
enrolled owner UID, grouped to the profile's private VM socket group, and
setgid so the socket is created with the expected group. The service can create
the socket there; the VM account can traverse/connect but cannot replace it. The
workspace service inherits no host-owner supplementary groups; it needs none
because it owns its socket directory.
The unit uses the enrolled owner UID with the profile's private socket group as
its primary GID (not the owner's login group or supplementary groups), only
inside that minimal root;
it uses `PrivatePIDs=yes`, `ProtectProc=invisible`,
`ProcSubset=pid`, `PrivateTmp=yes`, `ProtectHome=yes`, `ProtectSystem=strict`,
`NoNewPrivileges=yes`, an empty capability set, and `RestrictAddressFamilies=AF_UNIX`.
The fixed `virtiofsd` invocation
uses `--sandbox=namespace --no-announce-submounts`; guest symlinks/submounts
cannot expose paths outside the assigned workspace. There is no supplementary
owner group containing access to other host locations and no fallback to the
host-root filesystem. Missing namespace/root-image prerequisites refuse start.
Tests run attempts from inside the actual service and guest against another
workspace, private disk/keys/config/socket/history, `/home`, `/etc`, `/var/lib`,
and same-owner host processes; every attempt outside the assigned share must
fail.

The fixed system VM remains available as the explicit transitional singleton
route (`./lab vm system ...`); installing or selecting a profile never adopts,
replaces, or retargets its `/var/lib/little-mester-vm` disk, `/etc` identity, or
`/usr/lib` runtime. Its component units are mutually exclusive with named
profile component units: either start path refuses while any unit on the other
side is active, activating, deactivating, or has a pending start job. Named profiles remain mutually
concurrent subject to resource admission. The original `feat/vm-approach`
development VM is a third, distinct implementation and is explicitly excluded
from this singleton gate; it must remain usable concurrently with profiles. The
guard does not alter that VM's code, unit, runtime or state and does not add a
resource cap to it.

The signed common control artifact installs the same minimal, manifest-owned guard in
drop-in directories for the fixed singleton's VM, proxy, workspace and metrics
units. Guarding each is necessary because systemd starts the main unit's required
dependencies before its `ExecStartPre`. Each drop-in adds only a fixed
`ExecStartPre=+/usr/libexec/little-mester-vm/profile-control
singleton-start-guard` call to the helper's internal
`singleton-start-guard`; none replaces a base unit, `ExecStart`, `/usr/lib`
runtime, disk, credentials or other configuration. Each invocation takes a
shared maintenance lease, a shared catalog/read lock, and a nonblocking shared
host-admission lock; it snapshots
profile unit/job states and reports exact blocking profile name(s). The shared
locks let all four component guards execute concurrently without false
contention. Profile start takes a shared catalog/read lock and the same
host-admission lock exclusively; profile assignment/catalog mutations take the
catalog lock exclusively. It checks all fixed-singleton unit/job states before
its own host resource admission. The guard does not inspect or gate
the original development VM, add a memory threshold/cgroup cap, or change the
singleton's existing resource behavior. Because singleton and profile units
cannot be active together, workspace assignments need not be compared for
concurrent access.

Systemd marks a guarded target unit `activating` before its guard runs. Profile
starts issue a nonblocking systemd start request while holding the exclusive
admission lock and release it as soon as their VM unit is observed activating or
failed, not after guest readiness. The singleton's component guards take the
shared lock and fail closed if a profile start owns it; if they acquire it first,
the profile start fails closed on the queued/activating singleton unit state.
Thus simultaneous requests either observe the other's pending/activating state
or one fails closed; neither relies on a race-prone preflight alone. The guard
checks the fixed singleton only, never the separate development VM. It is owned
and removed by the common installer only after singleton retirement is explicitly
authorized, the singleton is stopped, and the installer can prove the drop-in
bytes match its installed manifest. The comprehensive cleanup ticket owns that
retirement; profile install/remove never edits the drop-in.

The explicit `system` route forwards the existing singleton operations through
their current protected owner-acceptance/candidate-matching procedure; that
legacy checkout-derived `verify`/`accept` path is a scoped compatibility
exception, not a profile dispatcher or profile migration path. A bare profile lifecycle command such as `./lab vm up` means the
named `default` profile and never auto-detects or adopts singleton state. The
existing top-level
`./lab vm prepare-dependencies` remains the global host-prerequisite command,
separate from profile and singleton lifecycle dispatch.

### Runtime installation inventory and pruning

The root helper reads direct children of `/opt/little-mester/v3` without
following links. The reserved `.staging/`, `.staging-<SHA>-<nonce>` and
`.gc-<SHA>-<nonce>` children are reported as `unknown/incomplete`, never accepted
as runtimes or automatically deleted. Runtime publication stages into a unique
same-parent `.staging-<SHA>-<nonce>` sibling and uses a no-replace atomic rename;
interrupted stages remain unknown for administrator inspection. Each new managed installation contains a root-owned,
read-only manifest with exactly `schema_version`, full `commit_sha`,
`profile_runtime_api`, `control_helper_api_min`, `control_helper_api_max`, and a
sorted array of `{path, git_mode, sha256}` entries. The root helper generates
the manifest from the verified Git tree; it is not copied as authority from the
candidate. It is created only after every tree entry passes safe-path, type/mode
and blob-integrity checks, and is validated before dispatch or pruning. A complete
managed installation for the same SHA is verified and reused; an incomplete,
modified or symlinked target is preserved and refused, never overwritten.
Validation walks the whole runtime with `lstat`/dirfd no-follow operations,
requires the exact inventory and required parent directories, root ownership,
no group/world write bits, regular single-link files, expected read/execute
modes and matching SHA-256 digests. The reserved manifest is the only
non-inventory file; any other node makes the install unknown/incomplete.
Pre-manager installations without a valid manifest (including installations
whose ownership or legacy use is unknown) are listed as `unknown/legacy` and
are never automatically classified as unused or pruned. A same-SHA unknown tree
is not silently adopted or overwritten.

```text
./lab vm installations
./lab vm prune --dry-run
./lab vm prune
```

`installations` reports each recognized SHA, profile references (including
stopped, `installing`, `resetting` and `removing` profiles), verified allocated
size, reference state, integrity state, liveness state and a safe reason.
Reference state is `referenced`, `unused`, `unknown/legacy`, or
`unknown/incomplete`; integrity is `verified` or `unknown/incomplete`; liveness
is `idle`, `busy`, or `unknown`. Unsafe
names/types/links are reported as unknown and retained. It never displays
credentials. `prune --dry-run` is read-only and reports candidates and total
reclaimable bytes. `prune` requires an interactive TTY and exact confirmation of
the displayed candidate set; it re-scans and revalidates after confirmation.
There is no noninteractive `--yes` bypass.

Pruning is allowed only for an exact SHA-addressed tree with a valid manager
manifest, zero references in every authoritative profile mapping, no matching
or transitional systemd units, and no active per-install lock. Running services
hold a shared lock for their lifetime; profile/runtime operations hold locks
while resolving, creating, starting, removing or dispatching. The pruner takes
the catalog lock and a nonblocking exclusive per-SHA lock, then rechecks the
mapping and systemd state before removing the candidate from the installable
namespace. If the profile catalog cannot be
fully and safely scanned, pruning refuses rather than guessing. Unexpected
directories, malformed mappings, unknown legacy state, or lock/active-service
evidence are retained and reported. A changing/busy tree remains marked busy;
if integrity cannot be verified it is `unknown/incomplete`, not a prune
candidate. The helper may delete only the validated
`/opt/little-mester/v3/<full-SHA>` runtime tree; pruning can never delete a VM
disk, credentials, workspace, profile configuration, system account, or unit.
Removing a profile does not trigger pruning; its now-unreferenced managed
runtime remains available until a separate explicit prune.

The profile mapping is the reference source; there is no persistent reference
counter. Root-owned lock files and catalog/profile metadata live outside `/opt`,
workspaces and profile service write roots. Lock order is **maintenance lease →
catalog (shared for read, exclusive for mutation) → profile → host-admission
(exclusive) → SHA** for profile starts, **maintenance lease (shared) → catalog
(shared) → host-admission (shared)** for fixed-singleton component start guards,
**maintenance lease (exclusive) → fixed-system code-update lock (exclusive) →
catalog (exclusive) → host-admission (exclusive)** for common control
bootstrap/update, **maintenance lease (exclusive) → fixed-system code-update
lock (exclusive)** for legacy fixed-system update/rollback,
**maintenance lease → catalog → profile → SHA** for other profile dispatch, and
**maintenance lease → catalog → SHA** for prune. Every lock acquisition is
nonblocking; on contention
the helper releases locks already held and returns a bounded `BUSY` diagnostic
or retries from the beginning. It never waits for a lower-ranked lock while
holding a higher-ranked one. Catalog locks cover only atomic mapping/assignment
publication, final mapping removal, a bounded metadata snapshot, or the short
admission/start handoff through systemd's `activating` state. They are never held
for guest readiness. Inventory copies mapping/manifest metadata under the
catalog lock, releases it, then verifies runtime trees and allocated size under
per-SHA shared locks; a busy or changing tree is reported with liveness `busy`
and integrity `unknown/incomplete`, never waited on. Profile locks
and shared SHA locks may remain held for a TUI/lifecycle operation; the catalog
lock is released first. State-only journal updates are atomic under the profile
lock and do not change SHA references. Any operation that must reacquire the
catalog lock first releases the remaining locks, reacquires in order, and
revalidates its durable journal before continuing. A profile `up` that includes
workspace assignment/reassignment first takes the catalog lock exclusively,
then its profile lock, validates disjointness and atomically commits the
assignment. It releases the catalog lock before acquiring the exclusive
host-admission lock; while retaining the profile lock it revalidates the
committed profile journal, checks singleton unit/job state and resource
admission, then requests systemd start and releases admission once the VM unit
is observed activating or failed. Another profile's assignment sees the
committed mapping and cannot claim an overlapping workspace. A start without
assignment takes the catalog lock shared. No lock upgrade is attempted; if a
later step needs catalog access it releases lower-ranked locks and reacquires
in order. Thus slow Git reads, VM startup, stop, TUI sessions and tree deletion
do not hold the catalog lock.

Profile services and public dispatch hold a shared SHA lock using fixed
`/usr/bin/flock` wrappers. A generated service unit runs only
`/usr/bin/flock --no-fork --shared <root-owned-lock> /usr/bin/python3 -B -I
/opt/little-mester/v3/<SHA>/lib/vm/profile-runtime.py ...`; `--no-fork` keeps
QEMU as systemd's MainPID, and the lock descriptor is non-CLOEXEC through the
runtime's final `exec` into QEMU. Lock files are root-owned and non-replaceable
under `/run/little-mester-vm-control`, and their stable parent is created by the
common bootstrap and recreated by `systemd-tmpfiles`. The runtime is opened and
manifest-verified before dispatch. The pruner takes a nonblocking exclusive SHA
lock; it cannot unlink a runtime while a service or dispatch holds a shared
lock. The pinned service entrypoint must `exec` QEMU without `-daemonize`.
Before `profile-unit-stop` sends its fixed QMP `quit`, it verifies the
exact generated unit, QEMU executable identity, MainPID/cgroup membership and
per-profile socket ownership; it never trusts a PID file or accepts a PID/QMP
command from the caller.

Prune first records and displays one sorted candidate set and total size. After
the owner confirms that exact snapshot, the helper reacquires the catalog lock
and rescans. If membership or verified size changed, it aborts and asks the owner
to preview/confirm again; it never adds a newly eligible SHA to the already
confirmed set. For each confirmed candidate it rechecks all mappings and unit
states, takes the exclusive SHA lock nonblocking, and atomically renames only
that runtime directory to a root-owned `.gc-<SHA>-<nonce>` quarantine name on
the same filesystem. It retains the SHA lock, releases the catalog lock, and
then removes the quarantine tree descriptor-relatively without following links
or crossing mount points. This keeps long deletion out of the global catalog
critical section while preventing install/dispatch for that SHA. A crash or
unlink failure leaves the quarantine reported as `unknown/incomplete`; it is
never resumed as an install or automatically deleted again. A new install for
that SHA refuses while a matching quarantine remains. Active, activating,
deactivating, failed, masked/unknown, or otherwise transitional profile units
are not treated as stopped.

Install first resolves and validates the committed `HEAD` and descriptor as the
invoking owner. It briefly snapshots the profile mapping under ordered locks: a
`ready` mapping at that same SHA returns success after verifying the existing
runtime manifest; a different SHA or unsafe state refuses without mutation.
It releases the catalog lock before manifest/tree verification while retaining
the profile and shared-SHA locks. Missing/corrupt pinned runtime bytes are not
silently rebuilt by this fast path.
For a new or recoverable `installing` profile, it releases catalog/profile locks,
then reads committed Git blobs and extracts the bounded candidate into a unique
root-owned `/opt/little-mester/v3/.staging-<SHA>-<nonce>` sibling. Same-parent
atomic publication needs no temporary permission change on the immutable runtime
root. No catalog lock is held during Git
reads, blob copying, digesting or extraction. For an existing SHA target, it
briefly acquires catalog/profile/shared-SHA locks to snapshot the runtime-root
and manifest identities, releases the catalog lock, and verifies the complete
tree while holding the profile/shared-SHA locks. It then releases those locks and
reacquires in order; final publication revalidates the profile snapshot and
runtime-root/manifest identities without rehashing the tree under the catalog
lock. If any identity changed, it releases locks and retries validation. No
full-tree hash runs while the catalog lock is held; unrelated profile metadata
operations continue during same-SHA verification. For a missing target, the
already-verified stage is atomically renamed while holding
the catalog and exclusive SHA locks. It then atomically publishes an
`installing` profile mapping under the catalog lock. If
an exact managed runtime already exists, the helper may remove only this
operation's stage after checking its recorded inode and full inventory; orphaned
stages from interrupted operations remain unknown and untouched. A
crash before runtime publication
leaves only a stage, which is never treated as an install or pruned; a crash
after runtime publication but before mapping publication leaves a valid
unreferenced managed runtime. A crash after mapping publication leaves the SHA
referenced and the profile name reserved. Repeating an `installing` profile/SHA resumes only
after exact artifact/account reconciliation; it refuses unknown or conflicting
artifacts and never cleans them automatically. Account names and planned
system UID/GID pairs are journaled
before account creation. Each generated account carries an exact manager marker;
recovery adopts only an exact name/UID/GID/shell/group/marker match and never
deletes or adopts an unmarked OS account. ID collisions cause a new planned ID
to be journaled before retry. Units are generated only after state ownership and
accounts verify. The profile becomes `ready` only after all artifacts and
effective units pass validation. All mapping/journal writes use root-owned temp
files, `fsync`, atomic rename and parent-directory `fsync`.

`down` stops every profile unit (VM, proxy, metrics, and workspace sharing),
waits for systemd to report each fully inactive and for all unit-cgroup
processes to exit, then preserves persistent state. Reset/remove do not
themselves stop or kill anything. A fresh reset, or a fresh remove of a ready
profile, requires every expected unit to be known, unmasked and fully inactive,
with no queued job, listener, socket owner or surviving process; `failed`,
`activating`, `deactivating`, unknown or masked states refuse before mutation.
Removal of an `installing` profile uses its journal to identify which generated
units were never created. During `removing` recovery, an absent/not-found unit
is accepted only if the durable removal intent records the exact unit-file
identity and phase; recovery daemon-reloads and verifies that it is absent with
no queued job or surviving cgroup process. All remaining units must still be
known, unmasked and inactive. An unjournaled missing/foreign unit, or any
failed/unknown/masked state, refuses. Reset never removes unit files, so its
recovery always requires all expected units to remain known and inactive. The
readiness/status output identifies the exact unit/process preventing the
operation.

After a fresh exact TTY confirmation, reset atomically writes a versioned
`resetting` journal and deletes only these fixed profile-owned leaves using
descriptor-relative no-follow checks: the leaves `vm/disk.qcow2`, `vm/seed.iso`,
`vm/ssh_host_ed25519_key` and its `.pub`, plus `client/control_ed25519`,
`client/control_ed25519.pub`, and
`client/known_hosts` under the profile's fixed `/var/lib/little-mester-vm/profiles/<name>/`
root. V1 stores no guest credentials, preferences or session snapshots outside
the guest disk; they are removed with that disk. The implementation never
walks or recursively deletes a workspace or caller-supplied path. Before each
unlink it records the expected device/inode/type in the journal; missing leaves
are an idempotent success, but a replaced inode, symlink, mount point, unknown
child, or path escape stops recovery and retains the journal for inspection.
Reset preserves the ready profile mapping/runtime pin, service accounts/units,
and workspace assignment. A retry requires the same
interactive confirmation, revalidates the full stopped predicate, then resumes
from the last verified step. Other operations refuse while `resetting`.

Remove similarly journals `removing`, retains the SHA reference and profile
name reservation, and confirms the stopped predicate before mutation. It may
remove a ready profile or recover an `installing` profile only when every
present artifact exactly matches its journaled manager-owned identity and each
absent artifact is expected absent at the durable install phase; unknown
artifacts refuse and remain for inspection. Its
durable phases are: (1) verify and unlink only the manager-generated unit files,
then daemon-reload and verify the exact units are absent; (2) remove the fixed
profile state/config/runtime directory entries with descriptor-relative
no-follow traversal that rejects unknown entries and mount boundaries; (3)
verify recorded system UID/GID, system-range IDs, primary/supplementary groups,
nologin shell, empty home and account marker, then remove the generated user
accounts and groups without deleting a home; (4) reacquire catalog then
profile lock, revalidate the `removing` journal and absence of units/state/
accounts, and unlink the mapping as the final commit. A crash after any phase
restarts at that phase: already-absent manager-owned units/files/accounts are
accepted only when the durable intent and recorded identity prove their removal
or expected absence, while name reuse is
blocked until the final mapping unlink. Any replacement/unmarked OS object stops
recovery; it is never deleted or adopted. Retries require TTY reconfirmation and
revalidate the journal-aware stopped predicate, including journal-proven absent
units. Fault injection interrupts before/after every
journal fsync, rename/unlink, daemon-reload, account/group removal and final
commit; assertions prove no path under a workspace, another profile, or `/opt`
can be reached by reset/remove.

## Public command grammar

The profile option appears before the operation and defaults predictably to
`default`; profile names match `[a-z][a-z0-9-]{0,31}` and the reserved names
`system`, `profiles`, `installations`, `prune`, `bootstrap`, `control`, and
`prepare-dependencies` are
rejected; there is no hidden current-profile selection. The shared baseline is:

```text
./lab vm [--profile NAME] install
./lab vm [--profile NAME] up [WORKSPACE] [--dir WORKSPACE] [--unsafe-recurse] [--reassign]
./lab vm [--profile NAME] status|ssh|auth|tui|down|verify|accept
./lab vm [--profile NAME] settings show
./lab vm [--profile NAME] settings set [--model openai/<id>] [--theme <name>]
./lab vm profiles list
./lab vm profiles show NAME
./lab vm [--profile NAME] reset       # explicit confirmation; destructive
./lab vm [--profile NAME] remove      # explicit confirmation; destructive
./lab vm installations
./lab vm prune --dry-run
./lab vm prune
./lab vm bootstrap [--recover]             # common signed bootstrap/recovery
./lab vm control update [--recover]        # common signed helper update/recovery
./lab vm system up|down|status|ssh|auth|tui|verify|accept  # existing singleton
./lab vm prepare-dependencies [production|acceptance|system-install] # existing global host prerequisite
```

The fixed `system` route, `profiles`, `installations`, `prune`, `bootstrap`,
`control` and
`prepare-dependencies` are separate top-level routes and reject `--profile`;
only named-profile commands accept that selector.

- `install` is the only profile operation dependent on checkout `HEAD`: it uses
  only the committed tree at the full `HEAD` SHA (uncommitted working-tree
  changes are not installed), creates or verifies/reuses the immutable install,
  then atomically creates the selected profile's mapping. It never overwrites an
   existing runtime. Repeating for the same managed profile/SHA is safe; a
   different already-pinned SHA refuses. No `--sha` or `/opt` path is required
   in ordinary use. It requires the common compatible helper installed by
   `./lab vm bootstrap`; missing helper/enrollment names that prerequisite, not
   a checkout sudo fallback. Legacy/unknown trees are preserved and not
   auto-adopted.
- Runtime-executing profile operations use that profile's validated recorded
  SHA. Fixed-helper safety operations (`status`, `down`, `reset`, `remove`, and
  profile display) may operate from valid protected metadata if the runtime is
  missing or damaged, but do not execute it. `status` and `profiles show` expose
  the profile name, lifecycle and pinned
  revision, but not protected paths, credentials or secret material.
- `down` stops the selected profile and preserves all its persistent state.
  `reset` requires a stopped profile and exact interactive confirmation; it
  deletes that profile's guest disk/seed and host/guest SSH identities. Guest
  credentials, preferences and session state inside that disk are removed with
  it. Reset preserves the profile mapping/runtime pin, service accounts/units
  and workspace assignment. It leaves the profile stopped;
  the next `up` provisions a fresh guest. `remove` also requires a stopped
  profile and exact interactive confirmation; it removes only that profile's
  mapping, units, accounts, identities, assignment metadata, configuration and
  persistent disk/state. Neither command deletes or modifies workspace contents
  or immutable runtime installations. Neither performs installation GC.
  Noninteractive destructive calls refuse; interrupted operations are visible
  and resumable only through the same operation or owner inspection.
- `--unsafe-recurse` without `--dir` means the current directory (`--dir .`);
  an explicit `--dir` takes precedence over a positional workspace. A positional
  path takes precedence over implicit `.`. `--unsafe-recurse` counts as selecting
  `.` when no explicit path is supplied; if `.` differs from an existing
  assignment, `--reassign` is also required. Without either a path or that flag,
  `up` reuses the profile's prior assignment; a first `up` without a workspace
  fails. A missing/disappeared assignment fails closed rather than selecting
  another workspace. The flag authorizes that workspace assignment only, not
  access to protected runtime or another profile. A different clone is an
  ordinary workspace unless existing containment checks reject it. Reassignment
  requires explicit `--reassign`, a new explicit workspace selection, a stopped
  profile, and successful checks that reject overlap with every other profile's
  workspace and protected state. `--reassign` without a new workspace selection
  refuses. A validated first assignment or reassignment is committed atomically
  under catalog/profile locks before startup; if startup then fails, that
  assignment remains selected and no existing profile state is deleted.
- `installations` and `prune` are global operations; they reject `--profile`.
  Profile listing/show operations likewise reject a profile selector. `prune`
  never reclaims accounts, units, mutable state or workspaces.
- `prepare-dependencies` remains the existing global host-prerequisite command;
  it does not create, select, or mutate a profile and remains on the current
  owner-acceptance path. Native package changes are limited to host dependencies
  and retain their separate interactive approval/no-upgrade guarantees. The
  fixed-system `system` route has no profile selector.
- Every command on an existing profile reads the authoritative mapping;
  operations that execute or start runtime code verify the recorded SHA and
  immutable manifest first. Fixed-helper safety operations (`status`, `down`,
  `reset`, `remove`, and profile display) do not need a valid runtime tree and
  never execute candidate code; this permits safe stop/removal of a damaged
  install. `profiles list`, global inventory and prune are helper-only queries.
  Guest/user actions are delegated only to the exact
  `/opt/little-mester/v3/<SHA>/lib/vm/profile-runtime.py` entrypoint. No public
  profile operation imports executable modules from the checkout.
- Profile SSH host ports are allocated from the managed profile range
  `22223–23222`, with the existing singleton port `22222` reserved. Allocation
  is serialized, recorded in the profile mapping and checked against existing
  listeners; a bind conflict refuses startup without silently reassigning.
  Proxy and metrics endpoints use per-profile services and runtime port files;
  QMP and virtiofs sockets use per-profile runtime directories. Profile unit
  names and system-account suffixes are manager-generated from the owner/profile
  identity, never accepted from CLI paths.
- Profile account ownership is stable. VM/metrics and proxy processes use distinct
  non-login accounts for each profile; `kvm` is supplementary only to the VM
   account. Workspace sharing runs inside the artifact-provided minimal root and
  namespace described above, with exactly one workspace bind and one profile
  socket bind. Cross-profile tests include actual in-unit read/write attempts
  against another profile's disk, seed, keys, configuration, sockets and metrics
   history, plus guest symlink/submount attempts. First assignment and
   reassignment reject overlaps against all profile mappings. The fixed
   singleton is handled only by mutual exclusion on systemd unit/job state, so
   no singleton workspace metadata or assignment is read or changed by profile
   operations. Canonical path, directory inode and mount
  identity are checked; same-path, ancestor/descendant, bind aliases and
  unprovable filesystem relationships refuse. Resolve the selected workspace
  from a no-follow directory fd, record device/inode plus `statx` mount ID and
  `/proc/self/mountinfo` mount-root identity, and compare each opened ancestor
  chain. If a path component changes during validation or disjointness cannot be
  proven, refuse. String-prefix checks alone are never accepted. The separate
  `feat/vm-approach` development VM is not gated or reconfigured by this rule.
- Per profile, QEMU remains bounded to 2 vCPUs and 4096 MiB guest RAM;
  `MemoryMax=5120M` and `MemorySwapMax=0` bound each profile VM unit to guest RAM
  plus a fixed 1024-MiB host-process overhead allowance. A nonblocking
  host-admission lock serializes the resource check and start handoff, not VM
  lifetimes. For every active/activating/deactivating profile VM or nonempty
  surviving VM cgroup, read exact cgroup-v2 `memory.max` and `memory.current`;
  require the expected 5120-MiB limit and compute unused profile headroom as
  `max(0, memory.max - memory.current)`. Include the requested profile's full
  5120-MiB cap. Require both `MemTotal - (existing profile caps + requested
  cap) >= 2048 MiB` and current host `/proc/meminfo` `MemAvailable >= existing
  profile headroom + requested 5120 MiB + 2048-MiB host reserve`. Thus existing
  profiles can grow to their configured limits without spending the new
  profile's budget or the host reserve. An unknown unit/cgroup, limit mismatch,
  impossible current value or unclassifiable process makes admission refuse.
  It parses exact byte and `/proc/meminfo` units; missing, conflicting or ambiguous data refuses before
  any start and never stops another VM. `MemAvailable` reflects memory currently
  used by the separate `feat/vm-approach` development VM and other host
  processes; no cap or lifecycle change is imposed on that VM. This is a
  start-time admission gate, not a hard reservation against an uncapped legacy
  VM growing later. The fixed singleton is refused by mutual exclusion and is
  not folded into profile reservations. Each profile receives 2 vCPUs; this
  contract makes no CPU throughput/latency guarantee. Named profiles may run
  concurrently when the memory gate passes; cgroup caps remain enforced.
- `verify` is read-only. `accept` is an explicitly authorized owner operation;
  neither fabricates provider authentication nor treats local mocks as host
  acceptance. Both resolve the selected profile's pinned runtime.
- Profile preference UX may change only supported ordinary preferences. It may
  not override root-enforced permissions, plugins, providers/network policy,
  sharing, or other protected configuration.

This is an additive profile-aware façade. Keep the legacy `feat/vm-approach`
VM and its existing commands/state as a separate fallback; do not silently
retarget or adopt that VM as a profile.

## Initial file ownership and integration seams

These boundaries avoid concurrent edits to dispatch and lifecycle files. A
stream should coordinate any needed change outside its ownership before editing
it; ownership can be adjusted by the design review, but not independently by
parallel streams.

| Ticket | Primary ownership | Shared seam / restriction |
|---|---|---|
| #67 profiles | Fixed root-owned control helper and protocol v1; common signed bootstrap/update transaction; authoritative mappings; manifest/inventory/prune; profile accounts, units, locks, paths and lifecycle; profile-aware runtime support and tests. Owns the four manifest-owned guard drop-ins on fixed-singleton component units and profile/singleton unit-state admission. | Runtime operations execute only from a profile-pinned `/opt/<SHA>` install. The singleton base units, `/usr/lib` runtime, disk and credentials are not replaced. The guards affect only the fixed singleton; they must not gate or alter `feat/vm-approach`. Do not edit public `lab` grammar; provide the fixed helper protocol to #70. Coordinate PTY and metrics seams with #69/#68. |
| #70 CLI/settings | `lab` public grammar and request construction for the fixed helper; common cross-distro bootstrap/update entrypoints and safe preference UX; explicit preservation of `./lab vm prepare-dependencies`; legacy `lab-vm`/`tools/vm-owner-acceptance` compatibility dispatch; exact artifact allowlists in both `lib/vm/system-acceptance.py` and `tools/vm-system-install` update/rollback verification; generated-bytecode root fix plus install/provision/verify regressions. | No profile metadata/lifecycle/GC policy here. Profile commands never load checkout lifecycle code. Preserve singleton as explicit `./lab vm system ...`; the existing global host-dependency command remains separate. Native package managers install dependencies only; the signed common artifact bootstraps/updates the helper and guards, while the fixed helper installs immutable SHA runtimes from committed Git trees. |
| #69 terminal | Shared fixed-system/profile TUI SSH/PTy helper and actual interactive regressions; document observed key semantics. | Request JSON is consumed before interactive I/O; helper then reopens `/dev/tty`, restores it to fd 0/1/2, and `exec`s the pinned client in the existing foreground process group. Require TTY on all three streams. The typed context contains only a resolved fixed loopback port, canonical profile key/known-host paths, and validated TERM; no caller argv or remote command. Use fixed SSH options, `-tt`, `EscapeChar=none`, no forwarding, strict pinned host key, and fixed `/usr/local/bin/lm-opencode`. Preserve stdin bytes, output, resize/SIGWINCH, Ctrl+C, and exact SSH exit status; no JSON is mixed into TUI output. Disconnect leaves the guest tmux session. SSH private key is generated atomically at its canonical protected path and never staged to `/tmp` or workspace. Do not edit `system-vm.py`; #67 owns the profile call site and #70 public routing. |
| #68 metrics | Host collector availability projection and TUI sensor-specific rendering; fixture, history, stale/error and renderer tests. | Keep the canonical collector contract in `parent-metrics.py`. `/metrics` returns exact `{source,run_id,sequence,timestamp,cpu,memory,gpu,availability,temperatures}`; `run_id` is new per service process and `sequence` increases once per completed collection attempt. Identical cached payloads keep the same sequence. `/history?n=60` returns completed samples `{run_id,sequence,timestamp,gpu_util,vram_pct,gpu_temp,availability}`; unavailable readings and VRAM with no valid denominator are `null`, never zero, and a repeated request does not append a duplicate sample. `availability` has exactly `cpu`, `memory`, `gpus`, `temperatures`, each `available` or a collector code (`no_device`, `no_sensors`, `unsupported`, `permission_denied`, `timeout`, `collector_failed`, `malformed`). Unavailable CPU/memory/temperatures are `null`; unavailable GPU is `[]` with its reason. Server sets `Cache-Control: no-store`; client requests with `cache:"no-store"`. #67 owns per-profile service/history state, unit, runtime port file and QEMU guestfwd binding. TUI polls every 500 ms, preserves null chart gaps and renders unavailable values as `—`. A valid response refreshes freshness only when `(run_id,sequence)` advances; same-sequence cached responses, HTTP errors and malformed payloads do not. Use guest-monotonic `performance.now()` receipt time and mark retained last-known values stale after 2 seconds without a newer valid sample. No new host listener or transport. |
| #66 integration | Contract, evidence ledger, common-base integration and final cross-stream verification. | No stream changes `feat/vm-approach-3`, pushes, merges, performs Ariadne operations, or reports owner acceptance without supplied evidence. |

Known legacy shared-routing facts: `./lab vm tui` currently flows through `lab` →
`tools/vm-owner-acceptance --system-vm tui` → `lab-vm system tui` →
`lib/vm/system-vm.py`, which opens SSH with a forced PTY to the fixed guest
launcher; the guest launch attaches through tmux. The profile implementation
does not use that checkout-SHA dispatch path: the fixed control helper resolves
the profile, then starts its pinned `/opt/<SHA>` profile entrypoint. For legacy
`system verify|accept`, keep the existing owner-acceptance candidate/runner
match gate and its explicit authorization; those commands do not determine or
change a profile's runtime pin.

## Required cross-stream checks

- Candidate SHA A is installed and pinned; checkout moves to B; all operations
  still dispatch to A. A second profile pinned to B dispatches to B. Same-SHA
  profiles share immutable code only.
- Invalid names, symlinked/malformed profile metadata, traversal, absent or
  incomplete runtime SHA for runtime-executing operations, and cross-profile
  resource collisions fail closed; fixed-helper safety operations remain
  available from valid protected metadata.
- Profiles A/B have distinct service UIDs and paths: attempts by A's VM, metrics,
  proxy and minimal-root workspace service to read/write B's private state/socket,
  any host path outside A's workspace, or same-owner host processes fail; both
  profiles can read but not modify the shared immutable runtime. Removing/resetting
  A leaves B and every host workspace byte unchanged. Start the actual
  `virtiofsd` unit, verify it creates its socket in the narrowly writable
  `/run/profile` bind with the enrolled-owner UID/profile-VM GID and expected
  mode, and verify the VM can connect but cannot replace the socket; workspace
  contents remain writable only within the assigned share.
- Race-test fixed singleton vs profile starts from both entrypoints, including
  queued/activating/deactivating systemd states and simultaneous requests. Exactly
  one side of each singleton/profile race may proceed; independent profile starts
  may both proceed if admitted. Exercise guard drop-ins on all four singleton
  component units (VM, proxy, workspace, metrics), and verify refusals name
  blocking profiles. Verify the original development VM is not included in this
  gate and byte-exact removal of the manifest-owned singleton guard drop-ins
  occurs only after explicit retirement authorization.
- Exercise all four singleton component guards concurrently: their shared
  catalog/read and host-admission locks must not cause false contention. Race
  them against profile starts holding the exclusive host-admission lock and
  verify systemd queued/activating jobs close the check-to-start window.
- Run both fixed-system verifiers (`system-acceptance.py` and
  `tools/vm-system-install` update/rollback checks) before and after common
  bootstrap. They must accept the exact baseline/manifest-owned guard states and
  reject a missing, modified, reordered, extra or unowned guard file or command
  on each of the four units. The root-owned control manifest must bind exact
  guard bytes and helper version; the fixed singleton's base units,
  runtime, disk and credentials remain unchanged.
- Bootstrap-state tests cover both a fully installed fixed singleton with all
  four exact effective `90-code-update-guard` interlocks and a host with all
  four fixed unit names absent, no pre-existing drop-ins, state or jobs. For an
  installed singleton, the workspace verifier requires exact `20` bytes when
  assignment exists and no `20` drop-in when the stopped unit is unassigned. The
  absent case installs the four manifest-bound `95` files first; a later
  fixed-system install must preserve and verify them. A partial, masked or
  malformed unit set refuses.
- Prove the original `feat/vm-approach` development VM can remain running while
  multiple named profiles start; verify no unit, runtime, cgroup limit, lock or
  lifecycle route for that VM is changed. Admission fixtures use real
  `/proc/meminfo`/systemd-cgroup-shaped data and show host `MemAvailable` impacts
  the result, including memory used by the development VM.
- Memory admission fixtures include active profiles whose `memory.current` is
  well below `memory.max`; a new profile must reserve the existing profiles'
  unused cgroup headroom plus its own full cap and host reserve in `MemAvailable`,
  as well as the total-cap/`MemTotal` check. Boundary, malformed and impossible
  cgroup readings refuse without stopping any VM.
- `down` preserves a stopped profile; reset/remove effects, TTY confirmation,
  crash recovery, no-workspace-deletion and no-runtime-deletion are tested.
- Candidate SHA A is installed and used by profile A; a checkout at B still
  dispatches A. Profile B can pin B. The checkout's lifecycle modules are never
  loaded for profile operations.
- Inventory reports SHA, all profile references, size and status. Prune dry-run
  changes nothing; normal prune retains stopped references, live units, locks,
  malformed/unknown installations, and deletes only confirmed managed SHA trees.
  Candidate-set changes between preview and confirmation abort. It never removes
  VM state, accounts, units, credentials or workspaces.
- Common bootstrap artifact signature/key enrollment, exact sudoers authorization,
  owner enrollment, protocol/API mismatch-before-effects, unprivileged Git blob
  reads, exact extraction and no-checkout-code-execution are verified with
  adversarial repository and malformed request fixtures. Control-update tests
  cover shared/exclusive maintenance leases, compatibility preflight, typed
  marker ownership, atomic publication, interrupted recovery and refusal to
  remove the helper with profiles.
- Across reboot, `systemd-tmpfiles` recreates only the root-owned mode-`0755`
  lock directory. Symlinked/replaced lock paths, wrong owners/modes/link counts,
  and malformed lock files refuse; only the non-writable root-owned SHA lock
  permits profile services to take shared locks, and only root can take an
  exclusive prune lock.
- Run the same `./lab vm bootstrap`, `control update`, profile `install`, and
  lifecycle request schemas on supported Ubuntu, Debian, Arch and CachyOS hosts.
  Verify the signed control artifact, helper/runtime manifests, generated units and
  lifecycle results are distribution-independent; only the explicit
  `prepare-dependencies` adapter may differ (apt versus pacman). Neither native
  package manager installs, updates or removes the control helper, guards or
  SHA-addressed runtime.
- Bootstrap-race tests hold the existing fixed-system code-update lock while
  concurrent legacy singleton starts enter each component guard; assert either
  the start is observed queued/activating before bootstrap proceeds or the
  nonblocking start guard refuses. Common control-update tests race ordinary
  helper operations, fixed-system code update/rollback and systemd starts against
  the shared lock and owner-tagged marker. Prove each path refuses a foreign
  marker, only its owner can clear its exact transaction, and crashes preserve
  the marker until signed-journal recovery. Native package-manager hook failure
  is not part of the control-helper gate; apt/pacman are exercised only by the
  dependency adapter.
- Initial control bootstrap is fault-injected before and after creation of its
  missing config/group/code-lock substrate. It accepts only exact idempotent
  substrate on retry, acquires the code-update lock before journaling or
  installing helper/guards, and refuses unsafe existing config, lock, group or
  memberships. Installed-unit bootstrap refuses a missing legacy code lock.
- Fresh fixed-system installation is fault-injected from absent config/group
  through final verification: acquire the maintenance lease first, safely create
  only the lock substrate, journal/mark before VM accounts/assets, install `90`
  guards before base fragments, preserve/verify any manifest-owned `95` guards,
  and hold both locks through provision, daemon-reload and exact unassigned
  workspace-layout verification. Recovery touches only transaction-owned
  objects; it never starts the VM or deletes pre-existing state.
- Start a second profile pinned to an already-installed SHA while verification
  hashes the complete runtime tree. Assert the catalog lock is released during
  hashing, unrelated profile metadata operations complete, and final ordered-lock
  revalidation rejects changed mapping/runtime identities.
- `up --dir` assignment/reassignment takes catalog-exclusive then profile lock,
  commits a validated mapping, releases catalog before exclusive host admission,
  and starts only after revalidating the profile journal and singleton unit/job
  state. A competing assignment observes the committed mapping and rejects
  overlap; a start without assignment uses the shared catalog lock. Failed
  admission does not deadlock, upgrade a lock, or undo the explicitly committed
  assignment.
- Marker/journal fault injection covers the prepared-journal/marker gap,
  marker-without-journal, every asset mutation, commit and marker unlink for
  common bootstrap/update/removal and legacy fixed-system install/update/rollback.
  Only an unchanged
  prepared journal can recover without a marker; committed journals with a
  leftover marker are reverified before clearing. Foreign owner, transaction ID,
  candidate/artifact SHA, schema or object identity always refuses. The legacy
  fixed-system v0 marker is recoverable only by explicit fixed-system rollback
  with one matching snapshot; common control recovery refuses v0.
- Common-control removal fault injection exercises the signed bootstrap runner
  after the helper has been deleted: only exact artifact-owned files/guards are
  removed, the fixed `90` interlock and baseline singleton assets remain, and
  signed `bootstrap --recover` either resumes the matching removal or restores
  the exact signed control artifact. It verifies baseline/absent singleton
  layout (including assignment-matched or absent workspace `20`) before marker
  removal and never unlinks the active `/run` lock inode.
- Reset/remove fault injection covers every durable journal boundary, account
  removal, unit reload and final mapping commit. In `removing` recovery, absent
  units are accepted only when the durable intent/phase proves that exact
  manager-owned unit was removed (or never created by the recorded install
  phase); unjournaled absence, failed/unknown/masked units, jobs and surviving
  processes refuse. Recovery is idempotent, requires TTY reconfirmation, and
  never crosses into any workspace or `/opt` tree.
- PTY tests use the production helper/SSH path to verify request framing is not
  consumed as terminal input, real stdin bytes, resize/SIGWINCH, Ctrl+C, foreground
  process group, disconnect/reattach and exact exit-status handling.
- Metrics tests verify unique `(run_id,sequence)` progression, history nulls and
  no duplicate/cache-freshness extension; UI tests cover no-zero initial state,
  null chart gaps, error handling and the two-second monotonic stale threshold.
- CLI grammar/help/status matches this document and exposes active SHA without
  requiring hashes or internal paths from ordinary users.
- Exact-candidate tests/reviews and hosted CI are recorded separately from
  owner-run Ariadne results. No KVM lifecycle or interactive acceptance is
  inferred from mocks.
