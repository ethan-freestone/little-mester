# VM owner-host acceptance runner

`scripts/vm-host-acceptance.py` is a host-side acceptance tool for the physical
KVM owner host. It is not CI. Keep the runner, its tests, and this document
after merge so the bounded nested-KVM gate can be repeated after
launcher/provisioning changes. It does not update tickets or claim the broader
VM release is accepted.

## Before using it

The latest #35/#41/#45/#50 tickets remain the source of truth for gate order.
The `preflight` and `nested` commands automate safe-snapshot preparation,
outer-guest readiness, and the bounded nested-VM gate. `campaign` is the ordered
owner-host continuation: it requires the existing named outer/child profiles,
does not start or clean either one, and uses a newly generated disposable
profile/workspace for downstream human and destructive tests.

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

If the guest RAM check blocks, the private report records the measured
`MemAvailable`, remaining cgroup budget, reclaimable QEMU RSS, projected
available memory, requested RAM/reserve, and a bounded failure category. It
does not start the child or adjust memory automatically. Use the read-only
report inspector to display only these diagnostics and named check results:

```bash
./scripts/vm-host-acceptance.py inspect-report \
  /path/printed/by/the/acceptance/runner.json
```

The inspector does not change the report or VM state. Keep the JSON private;
share only the reported RAM components and check names/results.

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
usable. Before changing or probing the child, it also runs the same read-only
QEMU/listener and guest-network checks against the outer profile; a failure
stops before the child phase. It additionally validates the running child's
QEMU `restrict=on`, the
loopback-only SSH and reviewed guestfwd set, and bounded guest probes for blocked
host-gateway/direct TCP and raw DNS, successful public HTTPS through the nested
proxy, private-destination denial, and the virtiofs workspace. The isolation
probe explicitly skips model availability, so this is not model acceptance or
a complete host-isolation proof. On the physical host it also checks the outer
VM's live QEMU arguments and confirms its SSH and egress-proxy listening sockets
are loopback-only. The child performs the same socket check in the outer guest.
This replaces a separate LAN-client probe for inbound QEMU forwarding: no
wildcard or LAN-bound hostfwd/listener is accepted. Guest-originated private/LAN
egress denial remains a separate check. It does not run an agent, auth flow,
workload, or teardown. **The child profile is retained on both success and
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

If child boot and readiness have already passed but a later probe failed or was
blocked by the child RAM budget, continue against that exact still-running child
without allocating another VM:

```bash
./scripts/vm-host-acceptance.py nested \
  --outer-profile <running-outer-profile> \
  --reuse-child <existing-child-profile>
```

This mode revalidates the existing child's private state, SSH reachability,
readiness and workspace; runs the live nested QEMU/network-isolation probes
described above; then checks depth-cap refusal and parent continuity. Model
availability is explicitly skipped. It does not start, stop, or clean a VM, and
does not require a passing new-child memory budget because no child is launched.
Use it only when earlier evidence already records a successful child boot; the
continuation report complements rather than replaces that evidence. The
depth-cap probe uses a new unique profile name.

For a retry, wait until the targeted revision is present in the CachyOS checkout
and the current #35/#41 handoff permits it, then run this once; it generates a
fresh child profile:

```bash
./scripts/vm-host-acceptance.py nested
```

Do not rerun a failed profile with the fresh-child mode or clean it, edit the
active snapshot manually, restart the outer VM, bypass the parent proxy, or widen
egress. Use `--reuse-child` only for the already-running child after its boot and
readiness passed. If startup still fails, stop and preserve the child/profile
and private report. Use the report to
locate `virtiofsd.log` and `virtiofsd-startup.log`, then return the first failure
and a redacted tail of those files; do not retry before the result is reviewed.

### Guided human observations

After the current ticket order permits a human gate, run one of:

```bash
./scripts/vm-host-acceptance.py observe --gate tui
./scripts/vm-host-acceptance.py observe --gate auth
./scripts/vm-host-acceptance.py observe --gate permissions
./scripts/vm-host-acceptance.py observe --gate workload
```

These optional commands are the only interactive part: visual TUI behavior and
browser/permission interactions cannot be inferred from shell checks. They
record fixed yes/no/unknown answers (and, where relevant, OpenCode version and
permission mode), never prompts, URLs, OAuth state/codes/tokens, screenshots, or
raw logs. They are not prerequisites for or evidence of the automated nested
gate, and they do not update tickets.

### Ordered owner-host campaign

After the current #35/#41/#45/#50 handoff explicitly permits the remaining
gates, run this once from the physical CachyOS checkout root:

```bash
./scripts/vm-host-acceptance.py campaign \
  --outer-profile nested-8g-20261102094820 \
  --reuse-child nested-accept-20261102124501-ee957d5c
```

The first gate is the existing-child continuation from `nested --reuse-child`.
The campaign requires that the exact named outer already be live on the safe
snapshot; it will not provision a substitute profile. This gate is strictly
read-only: it refuses a missing/stale snapshot rather than repairing it, refuses
missing outer-guest QEMU tools rather than installing them, and refuses to
create/restart an outer profile. It starts, stops, and cleans no VM. If it fails
or blocks, the campaign stops before any agent or disposable-profile work. A
pass there is only nested readiness,
QEMU/listener/network-boundary, depth-refusal, and parent-continuity evidence.

Next, it reloads the current approved outer guest egress policy, performs
read-only `lab-vm models status`, opens the direct guest TUI for the
model-stream/metrics/dialog/exit observations, then provisions
one uniquely named 2-GiB disposable profile and a private scratch workspace
outside the checkout. OAuth is not started until a separate interactive
confirmation that the current #45 prerequisites authorize the test, followed
by a second independent approval to start the real browser OAuth flow. Never
copy or record OAuth URLs, state, codes, or tokens. The agent
permission/workload gate uses only that scratch project's `opencode.json` and
`compose.yaml`: an `ask` rule, one exact harmless `deny`, and a PostgreSQL
Compose project with no host-published ports. Fixed human answers and sanitized
gate results are saved; raw TUI, auth-list, Docker, and OAuth output is not put
in the report.

The final stages verify guest Compose health/query/restart/down, then put a
known row in the guest workload, exercise non-destructive `suspend`/`resume`,
and verify the same overlay/workspace and row survive. Before destructive
`stop`/`down`, the runner asks for explicit confirmation naming only its
generated disposable profile/workspace. If OpenAI credentials are stored, the
campaign opts that profile into private host escrow, checks a fresh-overlay
import, and leaves the mode-0600 escrow in the mode-0700 state directory; it
does not touch host-Docker credentials or recreate its container. It removes
only the generated scratch workspace after the disposable VM residue check.
Declined approval, failed export/import, uncertain state, or failed gates leave
VM/workspace state in place for review; never clean it manually as part of a
retry. The campaign resets inherited `VM_YES`/`LAB_YES` so its explicit
confirmation is not bypassed. Guest agent launches explicitly disable any
ambient credential-forwarding environment; escrow is enabled only for the
campaign's own generated profile during the authorized teardown/import gate.
Any earlier failed or blocked gate prevents destructive cleanup and leaves the
disposable guest and workspace available for review.

Use `observe --gate workload` only when doing that human slice separately. The
campaign's report records the unique disposable profile and fixed observations,
not secret material or raw command output. A campaign result covers only these
gates and does not imply overall VM/release acceptance.

## Post-merge disposition

Keep this runner, `tests/test_vm_host_acceptance.py`, and this document. No CI
job is needed. The safe-snapshot/profile reconciliation, nested gate, and
explicitly authorized disposable campaign are repeatable acceptance support,
not one-time merge scaffolding. Do not add ticket writes or implicit
model-service changes here.

## Evidence and exit status

Each command prints `PASS`/`FAIL`/`BLOCKED` checks and writes a minimal JSON
summary under
`${XDG_STATE_HOME:-$HOME/.local/state}/little-mester/host-acceptance/` with
directory mode `0700` and file mode `0600`. Raw command output is not copied
into the report. The nested command streams provisioning diagnostics to the
terminal and leaves native VM failure logs in the child profile; preserve and
redact those separately before sharing.

Once the outer guest starts the remote phase, it prints timestamped progress
markers for runtime overlay setup, child `lab-vm up`, SSH control-state checks,
each child readiness probe, depth-cap refusal, and outer-session continuity.
Markers are streamed to the terminal and persisted incrementally in the private
report. If no remote output arrives for 30 seconds, the host runner records and
prints a silence heartbeat with the last-known stage and elapsed time. Ordinary
provisioning output continues to stream and is not copied into the JSON report.

After child startup, readiness uses separate bounded SSH commands: 30 seconds
for `cloud-init status`, 30 seconds for `lab-vm context --json`, 10 seconds for
`findmnt /workspace`, and 10 seconds for the workspace-state boundary probe.
Each stage emits start/completion or timeout progress; the structured summary
records return codes, stderr-presence booleans, and named readiness checks, not
raw command output. The depth-cap probe has a 120-second deadline, and outer
continuity has a 30-second deadline. Timeouts fail the gate and preserve the
child profile. The child `lab-vm up` stage is visible through streamed output
and heartbeats; these do not imply that stage passed until its completion event
and return code are recorded.
The full remote child phase also has a 30-minute deadline. A structured terminal
summary ends the runner's read immediately; it closes only its dedicated local
SSH client instead of waiting indefinitely for EOF from inherited output
descriptors. The summary is authoritative for the remote checks. A deadline or
interrupt likewise closes only that local SSH client and preserves the outer
and child VM state; neither action runs guest teardown.
Ctrl-C exits the acceptance runner (typically status 130) and closes its local
SSH client; it does not stop or clean the nested VM. An interrupted run is not
a pass and must not be retried with the same child profile.

For a run interrupted after child startup, inspect its private summary and
matching local runner/SSH processes without changing state:

```sh
./scripts/vm-host-acceptance.py inspect-interrupted \
  nested-accept-20261102005812-6494102d
```

Replace the profile with the exact child name printed by `lab-vm up`. This
command prints check names/results only (not raw report details or command
arguments); it recognizes only this runner and its generated depth-probe SSH
command, not browser-auth tunnels or ordinary `lab-vm run`/TUI SSH sessions. It
also prints allowlisted child readiness failure stages, SSH return status, and
boolean subcheck results when present. It does not print raw stderr, signal
processes, connect to a VM, or delete evidence. Keep the child profile and
report. Do not treat an unfinished report as a pass.

Exit status is zero only for a passed preflight/nested gate or a fully
positive human observation; blocked or failed gates return nonzero. A runner
PASS covers only the stated gate and must not be generalized to full VM,
model, TUI, auth, permission, workload, isolation, or merge acceptance.
