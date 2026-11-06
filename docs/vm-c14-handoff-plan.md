# Local-first VM acceptance → C14 handoff plan

> **Current control state — 2026-11-06: PAUSE after planning.** Owner approved
> shared-inference/security/performance direction, current-work checkpoint commits
> and detailed planning, then a pause to choose an implementation model. Start with
> [the controlling inference/performance amendment](vm-inference-performance-plan.md),
> INF-00 and its decision register. Implementation checkpoint: `ca6da6c`.
> **Do not execute older discovery, repair, retry or campaign commands below as
> the current next action.** They are retained chronological handoff evidence.
> Topology-aware discovery (INF-01) replaces the fixed-parent-loopback assumption.
> No new inference implementation/live operations/push/merge/default cutover now.

## Approved pre-C14 additions and current evidence

- Preserve ready development run `71ff52d4-d3d5-4394-a43d-440b1929f07c`, all
  archives/legacy state, parent session and unverified metrics listener. Owner
  confirmed mester --version after targeted corrections and partial TUI/Escape
  usability; model completion/metrics/permissions/isolation remain unaccepted.
- Ordinary persistent one-level VM is the performance priority. Default candidate
  is shared accelerator-backed inference behind an inference-only capability,
  explicit per-hop relay and no raw backend/host authority or recursive model copies.
- Required new gates: V-INFERENCE and E2E-MODEL-PERF, >=95% comparable direct
  warmed one-level generation throughput plus owner-frozen TTFT/concurrency/
  reliability/resource bounds. Security is independent and never waived for speed.
- VM-local verified cache/parent-push inference is a defined conditional alternative,
  not automatically another required backend before C14 or GPU passthrough approval.
- Execute INF-01–11 before INF-12/TASK-16. Each detailed task has substeps, inputs,
  files, tests, expected evidence, failure/stop behavior and decision points.
  R/I/H thinking labels recommend review; the owner always chooses the model.
- Remain within scoped owner-run effects and360min campaign ceiling, retain35min
  cleanup reserve and diagnosed retry policy. New deployments/downloads/hardware/
  credentials require concrete owner choice. No broad service repair or cleanup.
- Next recommendation after the owner's model swap/resume: INF-01.1–01.5 local
  topology/discovery implementation, then its reviewed read-only owner handoff.
  No restart or immediate provider/port questionnaire is the next step now.

## Authority and current state

Owner decisions recorded after C13b closeout:

- This document was originally a planning-only handoff. The owner subsequently
  explicitly switched back to **local §1 implementation**. That authorizes code,
  fixture tests, and documentation reconciliation only; it does **not** authorize
  provider discovery or VM/service startup by the assistant, push, merge, or C14
  activation. The later package-1 approval allows the owner—not this coding
  session—to start the development VM described below.
- Choose **1A**: implement a minimal repository-owned, redacting synthetic-canary
  checker, not an owner-supplied tool or a new testing framework.
- Choose **local models first**. Ollama and llama.cpp previously worked on the old
  branch/in Docker; availability on the actual acceptance host is unverified.
  Discover read-only, then ask the owner to select exact providers/model IDs.
- Live OpenAI login, paid streams, refresh, and provider-specific credential
  roundtrips are **deferred follow-up work, not merge or C14 blockers**. Preserve
  the ability to use native browser auth and its automated regression protection;
  do not remove the feature or report unperformed live tests as PASS.
- Disposable campaign credentials are acceptable in principle. This is not
  permission to inspect/copy existing credentials, log in now, export/delete a
  sole credential copy, or revoke unrelated sessions. Local-first acceptance
  needs no real OpenAI credentials.
- Keep VM as intended release default and Docker as optional explicit fallback.
  Ordinary `lab` remains on Docker until separately approved C14 activation.
- The owner approved a fixed four-package sequence: (1) an experimental explicit
  development entry and the first owner-started VM on this branch, (2) the bounded
  remaining security/evidence implementation, (3) integrated acceptance on a
  frozen candidate, then (4) C14 default/rollback and closeout. Keep the scope to
  this sequence; classify unrelated enhancements separately.
- The owner has approved the useful-evidence campaign within a 360-minute ceiling
  including cleanup and the one bounded diagnosed retry. Do not ask for campaign
  approval again. Pause only for genuine owner observations, provider/model choice,
  or effects outside the approved scope. The assistant must not start the VM.
- Manual OpenAI authentication experimentation by the owner is allowed in the
  disposable development VM. It is not a required acceptance result; no paid
  streams or credential export/import are included. Semi-automated auth testing is
  future scope. The default profile provider remains `none` until explicitly
  selected after read-only discovery.

Implementation baseline: `d3751e4fbda25349de5cbcb1f1e6400ae3e489c8`;
ledger closeout: `777f5a9f9716d83da254b01b96b93bb308021177`. No campaign ran.
This document supersedes older blanket requirements that every historical auth
subcheck must pass before merge/C14. It does not waive non-auth security gates.
The prior read-only host preflight passed at `422a523` (run ID
`2fb37d376f9040b3bbc6be4a5d216dd7`), but it predates the experimental CLI entry
and is not preflight evidence for a later frozen acceptance revision.

## 1. Bounded implementation handoff

### Implementation checkpoint

The bounded local implementation is complete and fixture-validated in
`scripts/vm-canary-check.py`, `scripts/vm-host-acceptance.py`, and tests. The
checker handles regular files, filtered snapshot trees, loose Git objects, raw
ISO9660 bytes, and run-owned logs/reports with fixed redacted results and bounded
work. It performs
a raw-byte pass over QCOW2 artifacts but does not inspect their virtual
filesystem contents; those checks remain BLOCKED. Guest filesystem isolation,
synthetic atomic export/import lifecycle checks, and provider-forwarding
acceptance also remain explicit blockers rather than implied PASS results.

No target-host provider discovery, VM boot, or acceptance has occurred. The
read-only preflight noted above does not prove boot, model availability,
filesystem isolation, guest registry access for the database image, nested KVM,
human UI behavior, or exact real cleanup.

The experimental `./lab vm run` / `up` entry is now wired to the existing lifecycle
and frozen-agent paths. It does not change ordinary `./lab` (still Docker), prove
acceptance, or activate the C14 default. The first owner-started development VM is
only a boot/workspace/TUI usability observation; the later integrated campaign
must use its separately frozen candidate and current-revision preflight.

For this package, run the commands from the already-running `feat/vm-approach`
parent VM, in its `feat/vm-approach-2` checkout. Its nested-KVM capability is the
relevant runtime; native-host or generic-container command results do not establish
that this child launch works. Keep the parent VM and its state/processes untouched.
The child development profile must use an isolated state root. The parent VM's default
`$XDG_DATA_HOME/little-mester` (or `$HOME/.local/share/little-mester`) currently
contains legacy VM/service state and fails the private-tree policy check. That is
an intentional refusal under the new schema, not a reason to chmod, delete, migrate,
or exempt the legacy tree. Keep it and its running processes untouched.

In the parent-VM Bash session, select the dedicated root and verify it read-only
before startup:

```bash
export MESTER_STATE_DIR="$HOME/.local/share/little-mester-branch-dev-vm-approach-2"
if [[ -e "$MESTER_STATE_DIR" || -L "$MESTER_STATE_DIR" ]]; then
  echo "STOP: dedicated development state root already exists" >&2
else
  ./lab vm context --json -d
fi
```

For a Fish session, use:

```fish
set -gx MESTER_STATE_DIR "$HOME/.local/share/little-mester-branch-dev-vm-approach-2"
if test -e "$MESTER_STATE_DIR"; or test -L "$MESTER_STATE_DIR"
  echo "STOP: dedicated development state root already exists" >&2
else
  ./lab vm context --json -d
end
```

Expected diagnostics include `state_root.root_metadata=ABSENT` and
`state_root.private_tree=PASS entries_checked=0`. Stop if the root is present or
any check fails; do not choose a different profile/root automatically. If debug
output reports `workspace.mester_marker=FAIL`, stop. That guard rejects
stale checkout-local `.mester` state even with `--unsafe-recurse`; identify the
marker and its owner/mode read-only. Do not remove or move it unless the owner has
verified that it contains no live state and explicitly chooses to remove it; never
exempt the guard. If it contains state, use a clean checkout/worktree outside that
marker tree, with the launcher run from that checkout so workspace and repository
identities match. In this run, the owner confirmed `$HOME/.mester` was empty and
removed it; the subsequent read-only workspace check passed.

If the policy checks pass, repeat the read-only RAM/disk check with the same
`MESTER_STATE_DIR` immediately before startup:

```sh
python3 "$PWD/scripts/vm_memory_budget.py" check --requested-mib 8192 --reserve-mib 512
df -h "$HOME/.local/share"
```

Once those checks pass, the owner—not the assistant—starts the development VM:

```bash
./lab vm run "$PWD" --name branch-dev-20261105 --unsafe-recurse --max-depth 0
```

This explicitly mounts the current checkout writable in the guest, disables child
VM depth, and starts or resumes only the `branch-dev-20261105` profile. It can download the
pinned base image and creates private profile, disk, key, seed and frozen-runtime
state outside the workspace and the legacy state root. Metrics remain **on by
default**, as requested. If the parent-VM metrics service is unavailable or unverified,
the launcher warns and continues without its forward; do not turn metrics off as a
precaution. It does not automatically install missing parent-VM packages; missing
prerequisites fail closed.
Ordinary `./lab` remains Docker. Keep `MESTER_STATE_DIR` set for every later
operation on this profile. The `run` command can resume it: stop and report if the
profile already exists but is not the dedicated development profile.

The first boot is a usability observation, not recursive acceptance. Manual
OpenAI login is optional owner experimentation inside the disposable guest; no
automated live-auth test, paid stream, credential export/import, or refresh test is
part of this step. Recursive G0→G1→G2 and changed-VM fail/fix proof remain in the
later integrated acceptance package.

### Package-1 interrupted prestart recovery

**Latest usability observation — TUI opens, features still blocked:** owner
confirms native TUI opens and Escape handling works. Metrics absent, no local
models, browser auth absent under the explicit --no-browser-auth attach, and
a remote model retries with ERR_PROXY_TUNNEL/403. Cancel retries; no paid requests
or authentication experimentation needed. This is partial usability, not full
package1 or acceptance PASS. Checkout egress still explicitly lists package
domains, so cloud destinations may be denied; exact denied hostname/ancestor
policy is not established. Do not clear the allowlist or bypass public-IP guards.

Current next step in a second terminal of the same parent VM/replacement checkout:

```sh
python3 -B scripts/vm-host-acceptance.py --discover-providers
```

This standalone read-only operation queries fixed parent-loopback model-list
endpoints with bounded responses/timeouts, and reports AVAILABLE/UNAVAILABLE plus
safe model IDs. It does not start services, select models, send completions,
copy credentials, change VM configuration or start an acceptance campaign.
Return the JSON and stop; owner then selects an exact discovered provider/model.
Keep the ready guest and parent unchanged. Metrics listener verification, model
forwarding/configuration, bounded remote egress diagnosis and browser cleanup/
optional manual auth remain separate open steps, not removed features or PASSes.

**Current owner step — attach the repaired ready VM:** owner --apply --repair-home
succeeded with both correction markers and `opencode v2.2.5` as mester. No restart
or reprovision was needed. From the same parent-VM replacement checkout:

```sh
MESTER_STATE_DIR="$HOME/.local/share/little-mester-branch-dev-vm-approach-2" \
./lab vm agent branch-dev-20261105 --permissions ask --no-browser-auth
```

This attaches through the existing frozen controller to the existing profile;
it does not install/update/rebuild. Browser bridge is explicitly disabled for
this usability check because the frozen controller retains the known cleanup
argument defect. This does not remove browser support or count as auth evidence.
Expect the native TUI at the intended workspace. Observe rendering, keyboard
response and any startup/plugin errors; no login/model selection/agent task is
needed yet. Report observations and pause before further acceptance. No launch
means no TUI PASS by assumption. Metrics forwarding remains unavailable/unverified,
and possible old browser helpers still require exact-identity review later.

**Current handoff — correct three confirmed root-owned XDG parents:** owner audit
shows home uid1000 mode0750, .local/.local/share/.config uid0 mode0755, cache
uid1000 mode0700, and OpenCode data directory absent. These parent owners block
guest-user application data creation. Source bootstrap now validates/prepares
these three parents before the guest-user version probe: create missing parents
privately, nonrecursively assign existing safe parents to the selected guest UID/
GID, preserve existing modes and contents. No recursive home/auth chown.

From the updated replacement checkout in the parent VM, Bash or Fish:

```sh
python3 -B scripts/vm-agent-permissions.py \
  --state-root "$HOME/.local/share/little-mester-branch-dev-vm-approach-2" \
  --profile branch-dev-20261105 \
  --run-id 71ff52d4-d3d5-4394-a43d-440b1929f07c --apply --repair-home
```

The explicit new --repair-home option requires --apply, validates guest mester
account/home ownership and all three canonical parent directory types/owners/
modes before mutation. It nonrecursively chowns only .local, .local/share and
.config to mester's UID/GID, preserving their modes and every child's contents/
ownership. The public package directory correction remains idempotent. No auth
storage inspection, reinstall, services, restart or frozen-runtime edits. Same
exact ready-run/listener/trust gates and20s guest/25s wrapper bounds. Expected:
home-parent ownership corrected, package modes corrected, and OpenCode version
as mester. No interactive input. Return short output and stop; refuse any changed
shape rather than bypass checks. Next after version success remains explicit
--no-browser-auth attachment for package1 usability. Metrics/old helper cleanup
and later integrated gates remain open. This supersedes the audit-only pause.

**Current result — public package modes corrected, guest home write blocked:**
owner --apply printed corrected, then OpenCode --version reached its own startup
and failed creating `/home/mester/.local/share/opencode`. The three public package
directories have now been changed; do not describe that operation as rolled back.
No version/TUI success yet. Current next step is the same exact helper/run ID
**without --apply**. Its audit now includes metadata only for home, .local,
.local/share, .local/share/opencode, .config and .cache (fixed paths; no directory
enumeration, auth/config data or file bodies). Return the short result and stop.
Do not recursively chown home or OpenCode storage, retry attach, reinstall or
restart the ready VM. Determine actual parent ownership before a narrow correction.

**Latest audit resolved — binary hardlinks do not block directory-only repair:**
owner audit reports executable-links, mode0755, uid0, links2. The single-link
predicate was unnecessarily restrictive: this operation never writes or chmods
the executable inode, only its three public parent directories. Removed only
that link-count restriction; canonical paths, exact symlink, type/root-owner and
mode checks remain. Real two-hardlink fixture now verifies both links retain
inode, mode, owner, link count, mtime and contents after directory correction.
Seven repair fixtures and changed-file Ruff/diff checks pass.
Next owner step: run the exact --apply command below for run71ff52d4, return
the correction/version result and stop. No reinstall/restart/frozen-runtime edit.
The earlier audit-only pause is superseded by this diagnosed correction.

**Latest repair result — audit only before any retry:** owner --apply refused
`guest installation differs from the diagnosed shape` before mode changes. The
helper now reports a fixed failed-check label plus numeric mode/UID/link count;
no predicate was relaxed. Next step: rerun the same vm-agent-permissions.py command
with the exact run71ff52d4 arguments **without --apply**, return its short result
and stop. This is read-only, bounded to the same20s guest/25s wrapper deadlines.
Do not repeat --apply or archive/restart the ready VM. A hardlink count, for
example, was not visible in namei output and is now reported if that check fails;
it is not assumed to be the owner-side cause without this audit.

**Current next step — preserve ready run, correct installed CLI modes:** owner
collector for run `71ff52d4-d3d5-4394-a43d-440b1929f07c` confirms ready/running,
clean cloud-init and private MCP build. Root resolves `/usr/bin/opencode`, mester
does not. The link points to `/usr/lib/node_modules/@opencode/cli/bin/opencode.exe`:
the scope/package/bin directories are root0700, executable root0755. This proves
global npm inherited bootstrap's077 umask, rather than a missing package/PATH-only
issue. Source now scopes022 to the global install subprocess only and verifies
the installed version as mester before readiness. Private runtime/config modes
remain0700/0600. The shared bootstrap install function also serves future updates.
Cleanup source now uses the session's fixed mester@127.0.0.1 endpoint instead of
reading missing argument15; actual callers pass14. Both regressions reproduced
before fixes. These changes do not modify the existing profile's frozen runtime.

From the same updated replacement checkout in the parent VM (Bash or Fish), run:

```sh
python3 -B scripts/vm-agent-permissions.py \
  --state-root "$HOME/.local/share/little-mester-branch-dev-vm-approach-2" \
  --profile branch-dev-20261105 \
  --run-id 71ff52d4-d3d5-4394-a43d-440b1929f07c --apply
```

This new narrow helper validates the exact ready profile/run/SSH listener and
existing host trust, then validates all guest paths/types/root ownership/modes and
the exact diagnosed symlink before any chmod. It sets only the scope/package/bin
directories to0755, without recursive chmod, file-content changes, reinstall,
credentials, VM restart or frozen-runtime edits. It then runs only --version as
mester. Guest command deadline20s, wrapper25s. Without --apply it only audits.
Expected output: `guest_opencode_directory_modes=corrected` and the installed
OpenCode version. No interactive input. Return that short result and stop; a
refusal must not be bypassed. Even on timeout/refusal, do not archive this ready VM.

After successful owner confirmation, next step is explicit frozen-controller
agent attach with --no-browser-auth to avoid the still-frozen cleanup defect for
this usability check; do not reprovision to test the host source patch. Optional
browser feature remains supported in source and separately unverified here.
Potential leftover browser helpers from the earlier failed cleanup remain an
exact-identity inspection/cleanup obligation; this permission repair does not
claim to stop or inventory them. Parent metrics listener remains untouched.

**Latest owner result — ready VM, attachment blocked:** fresh run
`71ff52d4-d3d5-4394-a43d-440b1929f07c` reached `Profile ... is ready` after
SSH/provisioning. The following native attach failed `exec: opencode: not found`,
then browser-session EXIT cleanup raised `15: unbound variable`. Keep this ready
VM and overlay; do not run any archive/reprovision command below for it. Source
inspection confirms cleanup expects argument15 while its callers pass14. Guest
executable visibility still needs diagnosis: root bootstrap's successful version
check does not prove mester can traverse/execute the installation.

Current next owner action is only the existing `vm-start-diagnostics.py --logs`
command below from this replacement checkout. Its updated current-source collector
adds user/root command lookup and bounded namei metadata for the two standard
OpenCode bin paths; it neither runs OpenCode nor reads auth/configuration. Existing
SSH ownership/trust gates and20s/64KiB bounds remain. Return the report, especially
AGENT_EXECUTABLE sections, and stop. Do not patch frozen runtime or remove browser
helpers manually. The failed cleanup may have left session-specific helpers;
no broad kill or assumption that those helpers were cleaned is justified.

**Current handoff — private MCP build ownership (2026-11-06):** run
`5d202308-f537-44ba-8487-4e68d8fa9515` passed SSH, then failed at
`private-payload-build`: npm ci reported EUSAGE/unusable lockfile. The post-failure
collector confirms profile `failed`, every recorded run process stopped, and no
runtime identity. Guest SSH collection is unavailable after orderly poweroff.
This supersedes the older unknown-profile/proxy recovery instructions below.

Source creates the private runtime parent as root/0700, but previously chowned
only its staging subtree before running npm as mester. A local offline real-npm
fixture with a valid dependency-free lockfile reproduces the exact EUSAGE when
the parent cannot be traversed; restoring access succeeds. Bootstrap now chowns
only that parent to the guest UID/GID before staging/build, retaining 0700 and
avoiding recursive ownership changes to an existing runtime. The production-path
ownership-order fixture fails before this fix and passes after it. Host bootstrap
success is still unverified; no lockfile regeneration or npm install fallback.

After local validation, the owner runs this from the same replacement checkout
in the parent VM, in Bash (the command also works in the owner's current Fish):

```bash
python3 -B scripts/vm-recover-prestart.py \
  --state-root "$HOME/.local/share/little-mester-branch-dev-vm-approach-2" \
  --profile branch-dev-20261105 --archive-stopped-failed --apply &&
MESTER_STATE_DIR="$HOME/.local/share/little-mester-branch-dev-vm-approach-2" \
./lab vm run "$PWD" --name branch-dev-20261105 \
  --unsafe-recurse --max-depth 0 --debug
```

The archiver rechecks stopped identities, reservations, locks and descendant
guards, and preserves the entire old profile in a private sibling archive before
a fresh launch captures fixed source. It does not patch the failed runtime.
Stop if archival refuses; do not change modes or kill processes. The launch can
download/install guest packages and uses the existing 300s SSH/1800s provisioning
bounds. Expected: pass private-payload-build, then reach ready/native TUI. No
input is needed during provisioning; do not log in or configure a provider for
this check. If it fails, return the existing --logs collector report and stop;
do not repeat recovery blindly. Keep the parent VM, all archives, legacy state
and unverified metrics listener untouched. Metrics remains enabled by default,
with unavailable forwarding nonfatal and not accepted as working.

**Inherited-lock correction:** if unknown cleanup previously refused
BlockingIOError, update source and reuse the same command below. Old proxy
supervisors could retain inherited lifecycle locks after the controller exited.
New supervisors close only their inherited FD copies (not LOCK_UN on the shared
controller lock). Recovery may retire the verified orphan proxy before acquiring
normal locks only when kernel lock owner/waiter and proxy FD checks exclude any
other live holder, alongside stopped VM/dead-controller/no-descendant guards.
It refuses a real active controller; no locks are forcibly unlocked.

OpenCode version policy is unchanged: latest resolves once per bootstrap attempt,
explicit pins remain possible, and the in-VM updater is retained. The observed
2.2.5 display is a test example, not a hard-coded installation version.

**Latest post-npm version fix and unknown/proxy cleanup:** read-only local CLI
verification prints `opencode v2.2.5`; the guest's old version regex rejected the
`v` prefix and could exit silently after successful npm installation. The source
now accepts the known plain/v-prefixed/labelled version formats, still requires
the exact saved version, and emits fixed bootstrap stages plus stage/exit ERR
diagnostics (never BASH_COMMAND or raw reported output). Updater parsing accepts
the same v prefix. This reproduces the observed silent post-npm failure pattern;
successful provisioning on the owner VM remains unverified.

Current profile is unknown after the earlier premature shutdown wait, with QEMU/
virtiofsd stopped and only proxy/logger running. Use the new explicit guarded mode:

```bash
python3 -B scripts/vm-recover-prestart.py \
  --state-root "$HOME/.local/share/little-mester-branch-dev-vm-approach-2" \
  --profile branch-dev-20261105 --archive-unknown-cleanup --apply &&
MESTER_STATE_DIR="$HOME/.local/share/little-mester-branch-dev-vm-approach-2" \
./lab vm run "$PWD" --name branch-dev-20261105 \
  --unsafe-recurse --max-depth 0 --debug
```

This requires unknown metadata, recorded stopped QEMU matching its run registry,
no descendants, free existing allocation/profile locks, dead/absent controller
reservation and stopped non-proxy processes. Only exact run-bound verified proxy/
logger identities may remain live. It stops their supervisor through process.py,
rechecks all stopped, then archives the entire profile with unknown metadata
unchanged. No unknown->failed transition, runtime patch, QEMU signal, broad kill,
parent listener operation or disk deletion. Without --apply it is read-only.
If refused, report output and do not loosen checks or rerun blindly.

**Current package-domain/fail-fast fix:** checkout policy explicitly adds only
`deb.nodesource.com`, `download.docker.com`, `registry.npmjs.org`, and
`files.pythonhosted.org` to existing permitted domains (Node22/Docker APT keys
and repositories, npm artifacts, and PyPI wheels). No allow-all, proxy bypass,
public-address filtering change or parent listener takeover. An independent
ancestor allowlist can still deny these; it must be diagnosed, not bypassed.
Future frozen source now recognizes terminal cloud-init error/degraded completion
even when `cloud-init status` exits2, preserving exit status without printing its
raw error data. Running/malformed status still uses the bounded readiness wait.

Owner interrupted the last launcher; list still says provisioning. That is not
process state. The existing `--archive-stopped-failed` command below now also
accepts interrupted starting/provisioning metadata **only if all existing stopped
identity/run/reservation/no-descendant checks pass**. It never stops a VM or edits
metadata to pretend it failed. If refused because a process is live/unknown, use
the collector below and stop; no force kill, metadata rewrite or another launch.

**One-command diagnostics (running or failed VM):** run from the replacement
checkout; no state-root export or per-run UUID needed:

```bash
python3 -B scripts/vm-start-diagnostics.py \
  --state-root "$HOME/.local/share/little-mester-branch-dev-vm-approach-2" \
  --profile branch-dev-20261105 --logs
```

This reports lifecycle/assets/process states plus bounded latest-run QEMU/proxy/
virtiofsd excerpts. When the exact QEMU-owned loopback SSH listener and existing
known_hosts/key are available, it also reads cloud-init status, output and service
logs through the isolated SSH transport. No host-key learning, installs, service
changes, VM signals, raw-log persistence or frozen-runtime edits. SSH is bounded
to20s/64KiB; unavailable guest collection is explicit. Redaction removes SSH keys,
fingerprints/key blocks, credential-like lines, URLs and long blobs. It is
best-effort, not a universal secret detector: review output before sharing. Never
collect seed/control-key/auth files. Default inventory without --logs is unchanged.

**Latest SSH retry:** run `43ec03cc-d7a5-4bbb-a34f-50dbfb33f937` timed out
at host-key validation. Owner's read-only scan exited0 with two stdout lines;
run directory was owned/0700 and known_hosts absent. The second line's contents
were not captured. Source now accepts ASCII `#` comment lines only on the scan
boundary, with a strict 64 KiB bound and exactly one complete expected-host key.
Stored known_hosts remains strict and extra keys/unknown lines still fail.
If this was scanner commentary, the latest fix should unblock SSH; that remains
unverified on the owner VM. Other two-line shapes now produce content-free
`scan-lines/comments/records` counts in the wait diagnostic. Use the stopped-failed
archive/retry command below once; it verifies rollback before touching the profile.

**Latest observation/retry:** the owner booted the guest; cloud-init reported
`Guest OS identity is unavailable` because Ubuntu's standard `/etc/os-release`
symlink was rejected. Host SSH trust readiness separately timed out after 300s;
rollback verified all recorded run processes stopped. Profile is failed, with
ISO/overlay/runs, so neither earlier prestart mode applies. Source now supports
only Ubuntu's standard OS-release link targets, confined to the guest root, and
adds bounded progress plus fixed SSH failure-check labels (no argv/key output).
The SSH root cause remains unproven; the next launch is diagnostic as well as a
boot retry, not evidence that SSH has been fixed.

After updating the replacement checkout, the owner can preserve this stopped
booted profile with the new explicit mode and capture fixed source in a fresh run:

```bash
python3 -B scripts/vm-recover-prestart.py \
  --state-root "$MESTER_STATE_DIR" --profile branch-dev-20261105 \
  --archive-stopped-failed --apply &&
./lab vm run "$PWD" --name branch-dev-20261105 \
  --unsafe-recurse --max-depth 0 --debug
```

Archive mode verifies every run stopped, no live profile process reference,
stopped/absent controller reservation, any recorded runtime identity stopped,
and no possible descendants. It acquires existing allocation/profile locks and
moves the entire disk/runtime/keys/runs/logs intact to a private sibling archive.
It never signals processes, patches frozen source, deletes disks or restarts a
running/unknown profile. Keep all prior archives and the working parent VM.
If SSH still waits, share its `check=` label and startup inventory immediately
from a second terminal. Do not interrupt the bounded run or repeat recovery.

**Earlier seed-preparation retry (historical; not for the current booted profile):** the owner already
archived the original prestart profile. A subsequent fresh launch created an
overlay, then failed at seed-write: the generic secret guard rejected the
renderer-owned `lock_passwd: true` and fixed `NOPASSWD` sudo entry. The exact
structured values are now allowed; arbitrary password/token material remains
refused. The current profile is `failed`, has no seed ISO/runs/runtime identity,
and must use the explicit overlay-preserving archive mode. Do not patch its frozen
runtime, delete its disk, or use ordinary resume on it.

```bash
python3 -B scripts/vm-recover-prestart.py \
  --state-root "$MESTER_STATE_DIR" --profile branch-dev-20261105 \
  --archive-failed-before-boot --apply &&
./lab vm run "$PWD" --name branch-dev-20261105 \
  --unsafe-recurse --max-depth 0 --debug
```

This archives the whole failed profile including its overlay, then allows a fresh
profile with fixed frozen source. It refuses a published ISO, any runs, recorded
runtime identity, active profile references, or live/unknown reservation ownership.
If refused, report its output; do not loosen its checks. The diagnostic script
below also reports whether the fixed metrics port has a listener. An existing
unverified parent/legacy listener must not be killed or adopted to make metrics
work; metrics forwarding remains nonfatal and its actual acceptance stays open.

The observed `starting` profile with no runtime identity/run registry and a
generated runtime `__pycache__` is handled by the bounded recovery script below.
The owner runs this from the parent VM's current replacement checkout. It archives
the entire never-booted profile outside the dedicated state root (prints its path),
preserves all evidence, and releases only a verified stopped-owner reservation.
It refuses guest assets, runs, live/unknown owners or active profile references.
It does not patch the frozen runtime; the next fresh launch captures fixed source.
No legacy/default-root cleanup or extra historical ticket gate is involved.

```bash
export MESTER_STATE_DIR="$HOME/.local/share/little-mester-branch-dev-vm-approach-2"
python3 -B scripts/vm-recover-prestart.py \
  --state-root "$MESTER_STATE_DIR" --profile branch-dev-20261105 --apply &&
./lab vm run "$PWD" --name branch-dev-20261105 \
  --unsafe-recurse --max-depth 0 --debug
```

If either command fails, do not repeat recovery blindly. Collect content-free
startup inventory, without depending on the full-tree status guard:

```bash
python3 -B scripts/vm-start-diagnostics.py \
  --state-root "$MESTER_STATE_DIR" --profile branch-dev-20261105
```

Metrics remain on. A successful development boot is package-1 usability evidence,
not completion of integrated acceptance or approval to activate C14.

### A. Minimal synthetic-canary checker

Reuse the acceptance runner/report and existing snapshot/guest mechanisms. Use
stdlib helpers and narrow fixtures; no daemon, generic scanner framework, new
provider support, automatic remediation, or unrelated refactoring.

The local implementation enumerates the filtered snapshot/tree and Git objects,
provisioning seed, base/overlay images, selected logs/diagnostics and reports.
It inspects regular bytes, loose Git objects and raw ISO9660 bytes; a raw QCOW2
byte pass is explicitly insufficient to inspect the image filesystem. Missing,
unsafe, incomplete or unsupported artifact checks stay BLOCKED. Do not mount or
modify arbitrary host images or install host tools implicitly.

- Generate an unguessable **fake** secret and keep it in a private campaign-owned
  host fixture outside exported source. Separately test known-positive artifacts
  in fixtures so an always-ABSENT checker cannot pass.
- Read the canary privately, not in argv, environment, chat, history, or reports.
  Check the relevant raw representation and any specified encodings actually
  produced by the tested pipeline; do not claim universal secret detection.
- Accept only explicitly manifested, canonical run-owned artifacts; reject
  escaping symlinks, foreign paths, unsafe files and ambiguous ownership.
- Bound bytes, file counts and runtime; inaccessible, oversized, unsupported,
  missing or incomplete inspections are BLOCKED, never absence/PASS.
- Persist only fixed check IDs, PASS/FAIL/BLOCKED, sanitized reason codes and
  artifact digests where safe. Never print a match, canary, secret, or file body.
- Synthetic material deliberately present in its source fixture is not a leak;
  define that exclusion explicitly and never exclude the artifact under test.
- Clean only exact generated fixtures. Keep reports outside exported snapshots.
- Add positive/negative/redaction/path/symlink/incomplete-coverage/timeout fixtures
  and integrate results into the existing security gate evidence.

### B. Reconcile acceptance scope without weakening auth safety

The runner distinguishes **required local-first acceptance** from **deferred
live-OpenAI evidence** in the existing report structure. It does not simply remove
`HUMAN-AUTH` from a list: the report records the reason, each related deferred
subcheck, automated auth regression obligations, and independent required
filesystem/private-state subchecks.

| Required before local-first C14 | Deferred live provider evidence |
|---|---|
| Existing automated browser/auth fixtures: URL/version validation, narrow callback forwarding, cancellation/timeout/port collision, cleanup, independent sessions and secret redaction | Real OpenAI browser success and live negative scenarios |
| Synthetic/private-state persistence and atomic export/import tests; failed export must block destructive deletion; forwarding default-off and explicit selection | Actual OpenAI refresh/expiry, real token export/delete/reimport and recursive provider credential import |
| Real host/guest filesystem and network isolation, canary absence, no host auth copy, exact cleanup | OpenAI paid/model streaming acceptance |
| Local-provider real streams, real agent work, workload/lifecycle/nesting and observed UI/permissions/rollback | Provider-specific claims not exercised locally |

Keep deferred checks visible as DEFERRED with an explicit owner-approved reason;
never set them PASS or claim full historical fifteen-gate
acceptance. Report both the required local-first outcome and deferred evidence.
Local-first readiness can pass only if every required subcheck and exact teardown
passes. Auth-dependent items in E2E-MODEL, E2E-LIFE, SEC-FS, SEC-SECRET and
HUMAN-AUTH must be classified individually. Non-auth items remain required.
Account-level expiry cannot be simulated into live-provider PASS.

Preserve current automated auth coverage and keep focused tests proving that
local-first scope defers only approved live-provider checks, cannot override
required failures, and keeps OpenAI functionality available. The bounded
follow-up specification for real OpenAI acceptance remains here; do not
implement live acceptance.

### C. Close executable prerequisites, not add features

- Audit existing procedures against the required local-first subchecks. Real
  agent failing-test→fix→pass, two authorized concurrent tasks, retention,
  selected config/tools apply, boundaries and actual nested fail/fix/depth proof
  need actual operations/evidence, not wholesale checklist attestation.
- The guest workload now checks for `postgres:16-alpine` inside the VM and makes
  one guest-only `docker pull` attempt, bounded to 180 seconds, when absent, using
  the guest-local socket and a fresh empty Docker config. It verifies the image
  afterward; failure remains BLOCKED. Compose is configured with
  `pull_policy: never` and `up --pull never` to prevent implicit pulls. Never use
  host Docker for this workload. Fixture tests cover both successful guest-side
  download and unavailable-image blocking; they do not prove target-host registry
  access or run a campaign.
- The runner must stop before expensive recursion until the exact-run
  `HUMAN-TUI` observation is recorded PASS; neither boot nor a stage result proves
  usability. The local runner now adds this fail-closed prerequisite; fixture
  validation is recorded in the progress ledger.
- Bind the selected suite/memory/update effects/provider scope and total budget
  consistently across start/resume/retry. Preserve private STATE_ROOT and attempt
  ledger for the one diagnosed retry; changed source/config needs fresh review.
- Reserve at least 35 minutes for exact teardown inside the approved total; do
  not consume the reserve on another stage. Ambiguous cleanup stays BLOCKED and
  preserved.
- Fix known docs-consistency failures within the already-planned C14 docs work,
  not as a new feature project.

Before the later frozen-candidate campaign handoff: review the exact diff; require
full Python/Bats, affected static/compile/shell, embedded guest-script, help and
Bash/Fish docs checks; record any missing required procedure as an explicit
blocker, never “ready” by assertion. Package 1 is the exception already approved:
the owner may start the development VM with the exact command above. That boot is
not the package-3 campaign, C14 permission, or authority for the assistant to start
a VM.

## 2. Execution responsibilities and mandatory pauses

All live operations run on the **actual target Linux host**, not an assumed
equivalent coding container. Where this session cannot reach that host, the owner
runs the supplied commands and returns sanitized output. No remote access is
assumed. The first package-1 development VM is owner-approved; P1–P8 below describe
the subsequent campaign and C14 checkpoints, within the current approval boundary.

| Checkpoint | Automatable work | Required owner input / explicit instruction |
|---|---|---|
| P0: implementation permission | Local §1 code/tests/docs and package-1 explicit development entry | Satisfied. Owner-started package-1 VM is separately approved; the fixed campaign and 360-minute ceiling are already approved, with no repeat approval request. |
| P1: read-only host/provider discovery | Exact revision/tool/KVM/RAM/disk checks and fixed loopback model-list queries; no VM/service/model mutation | Run the supplied read-only command on the target host, return sanitized result. Select discovered provider/model IDs; do not infer approval from availability. |
| P2: freeze the approved campaign effects | Produce one reviewed plan with hashes, exact paths, required/deferred subchecks and budgets, within the already approved 360-minute total | No second campaign approval. Pause only if a newly discovered effect exceeds the approved scope/ceiling or needs a real owner choice. OpenAI paid streams and credential export/import stay off. |
| P3: fresh usability | Start only the disposable campaign VM; boot/mount/tools checks, warm identity and local-model smoke | **STOP before recursion.** Open exact ready profile in the owner's terminal; perform TUI/metrics/cancel/permission actions using the supplied checklist. Explicitly continue only if usable. Diagnose failures first. |
| P4: work and lifecycle | Bounded local streams, agent test fail/fix/pass, guest Compose/query, retention, scan/cancel and approved config/tools apply, resize | Pause only for actual ask-mode/UI decisions or unexpected effects. Never accept permissions on the owner's behalf. Show exact test purpose and expected action. |
| P5: nesting/security/concurrency | Actual nested placement/fail/fix/depth, per-level budgets, isolation/canary checks, authorized concurrent harmless tasks | Confirm continuation into the costly stage as agreed at P3; pause on unavailable prerequisites, scope changes, uncertain ownership or resource exhaustion. |
| P6: evidence and cleanup | Summarize required failures/blocks and deferred auth; deepest-first exact teardown, residue/protected-asset checks; finalize report after cleanup | Complete outstanding genuine human observations before teardown. No premature PASS for teardown-dependent observations; finalize those from actual cleanup evidence. Stop for review if sole auth-copy or ownership is uncertain. |
| P7: C14 approval | Review required local-first evidence; prepare existing TASK-16 docs/CI/default-route changes | Separately approve C14 activation. No push/merge is implied by campaign approval or C14 implementation. |
| P8: default/rollback smoke | Test the new VM default on exact accepted scope and explicit Docker fallback without recreation | Review smoke result before requesting separate push/merge approval. A failed smoke blocks completion; preserve the working Docker session. |

### Required shape of every owner instruction

The implementation model must provide **one current step at a time**, not a wall
of executable campaign commands. Each handoff must say:

1. **Where:** target host vs coding checkout; Bash or Fish; exact directory/SHA.
2. **What:** copy/paste command using already-confirmed variables and exact run ID.
   No fake model names or illustrative budgets in a command presented to run.
3. **Effects:** read-only vs VM start/download/update/delete; protected assets.
4. **Expected result:** sanitized output/status, timeout and concrete stop condition.
5. **Owner action:** what to click/type/observe, or explicitly “no input needed”.
6. **Return:** exact sanitized summary/report fields to send back; no raw logs,
   prompts, model responses, OAuth URLs, credentials, or auth-store contents.
7. **Next pause:** state “stop here and wait” or the exact already-approved next
   automated action. EOF, timeout or silence is not consent.

An agent can automate already-approved noninteractive steps within one frozen
campaign budget without asking permission for each command. It must pause at
the named human checkpoints and for new effects, changed scope/revision, unsafe
state or exhausted budget. Browser callback clocks are never extended by pauses.
No silent retries. At most one diagnosed fresh attempt on the same source/config
revision, after exact cleanup, using the same STATE_ROOT and original total.

## 3. Decisions still needed, at the appropriate pause

Do not make the owner solve these before implementation has produced useful
read-only discovery and a concrete handoff:

- **P1:** The owner runs target-host commands. Which discovered Ollama/llama.cpp
  model(s) should be tested? Prefer sequential tests; a missing service gets
  reported, never started automatically. If neither provider is usable, present a
  bounded remedy only if it introduces effects outside the approved sequence.
- **P2:** The total campaign ceiling is already approved at 360 minutes, including
  teardown and the one diagnosed retry. Freeze measured resource requirements,
  guest-only PostgreSQL preparation and selected config/tools apply within that
  ceiling; pause only for a new effect outside the approved scope. Paid API ceiling
  is zero.
- **P3:** Confirm usable TUI and local agent behavior, then explicitly continue
  to the remaining costly acceptance stages or stop and diagnose.
- **P7:** Review the required local-first results and visible deferred auth list;
  authorize C14 only if required evidence and cleanup pass.

## 4. OpenAI follow-up boundary (specification only)

Keep native browser auth and automated fixture regression tests in the normal
local/CI suite. Later, separately scope an opt-in live OpenAI test: exact model,
cost/time limits, owner browser login using disposable campaign state, no host
credential copying, verified callback cleanup, a bounded real completion, and
explicitly approved retention/export/import tests. Interactive login remains an
owner action; refresh availability must not create an unbounded merge blocker.
Do not implement that live campaign during the local-first/C14 scope.

## 5. Completion rule and exclusions

A working VM milestone means real boot, usable native TUI and local agent,
guest-only workload, retained state, required isolation/nesting/lifecycle proof,
and exact cleanup on the actual host—not just green fixtures. C14 then delivers
the VM-default route with working explicit Docker fallback and required docs/CI.
Evidence must identify the tested runtime/source revision; any C14 changes to
runtime behavior require affected revalidation, not assumed coverage from an
earlier SHA.

No new supervisor, model manager, service startup automation, scanner framework,
automatic credential harvesting, broad host changes, unrelated cleanup, or
optional provider expansion. Preserve `vm-migration-plan.md` and working Docker
state. The owner—not the assistant—starts the approved development VM using the
exact reviewed command above. Provider discovery and later campaign stages remain
owner-operated as specified; the bounded 360-minute campaign is already approved,
so do not ask for approval again unless a new effect or scope change is found.
Manual OpenAI authentication is optional owner experimentation only; paid streams
and credential export/import remain excluded. Push, merge and C14 cutover are not
authorized by this plan.
