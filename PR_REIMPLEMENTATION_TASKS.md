# Executable VM replacement tasks

> **Current owner reset, 2026-11-06:** start at
> [PR_REIMPLEMENTATION_HANDOFF.md](PR_REIMPLEMENTATION_HANDOFF.md), packets M0–M4.
> Planning only now; no tests/implementation. On resume use Luna max -> real
> evidence runner -> Sol medium review -> diagnosed fix/advance. Get development
> into the replacement VM with OpenAI auth before local-inference architecture or
> full release acceptance. The task specifications below retain requirements, not
> a competing sequence. Missing historical tickets, especially #50, are not gates;
> do not query/reconstruct them. Keep evidence in PROGRESS, not tickets/MCP.
> The handoff's frozen implementation order and failure-to-fix matrix control the
> next edits. A failed gate requires its repair plus affected rerun, NOT waiting
> for that same gate to pass before implementing its fix. No new planning round.

**Approved 2026-11-03; execute in order, with actual evidence in the progress ledger.**

**Current amendment, approved2026-11-06:**
[Detailed pre-C14 inference/performance tasks](docs/vm-inference-performance-plan.md)
are normative. INF-00–12 supplement the existing tasks and precede final C14:
topology-aware discovery; serving isolation; exact protocol/limits/dependency
decision; inference-only gateway; selected per-hop relays; frozen profile/client
integration; benchmark driver; real one-level >=95% performance plus security;
remaining metrics/browser/permission/security procedure closure; real nested
proof; frozen integrated acceptance; existing TASK-16 release/delivery.
No recursive model copies by default. VM-local cache/push placement is defined
future/conditional scope only. R35–R38 and D11–D13 extend the master contract.

The companion contains complete substeps, likely files, test/expected outcomes,
dependencies/checkpoint boundaries, operational authority and stop/escalation
rules for handing work to a less-capable model. Read its universal task packet
and whole selected INF section before coding. Higher-thinking reviews are
recommended at marked H decisions; **the owner always chooses the model**.
Do not auto-switch/spawn reviewers or silently invent a security/performance
decision. Current execution is the planning-only reset described in the single
handoff; M0–M4 begin on implementation resume. The older INF-first instruction is
superseded for development migration; C14 still needs separate approval.

Read `PR_REIMPLEMENTATION_PLAN.md` (requirements R01–R38, decisions D01–D13,
interfaces, gates) first. A task agent receives that contract plus its complete
task section and prerequisite commit evidence. It must not redesign interfaces.
All VM paths here are proposed CREATE paths on `main` unless labeled confirmed;
original-PR versions are reference inputs, not wholesale cherry-pick instructions.
Never edit ticket JSON. No task may modify the user's `vm-migration-plan.md`.

**Current authorization amendment — 2026-11-05:** the owner approved the fixed
four-package sequence recorded in `PR_REIMPLEMENTATION_PLAN.md` and
`docs/vm-c14-handoff-plan.md`: explicit experimental development entry + first
owner-started VM; bounded remaining security/evidence work; frozen-candidate
integrated acceptance; then C14 default/rollback closeout. Old task text saying
public `run`/`up` must remain gated is historical and superseded for the explicit
experimental route only; ordinary `lab` stays on Docker. The campaign is already
approved within 360 minutes including cleanup and one diagnosed retry; do not seek
campaign approval again. The assistant must not start a VM. Manual owner OpenAI
auth experimentation is optional; paid streams and credential export/import are
excluded. Pause for genuine owner observations or new effects. Push, merge and
C14 activation remain unauthorized.

## Common execution contract (part of EVERY task)

- Prerequisites must be integrated and their required tests passing. Inspect Git
  status/index before edits; unrelated user changes are not part of a task.
- Follow docs→tests→code. Start by checking the exact available tools, versions
  and file contracts, then write the specified failing tests. No failing commit.
- Commands marked **P** are provisional until first executed on the replacement;
  first inspect their implementation, confirm `--help`/dependencies where relevant,
  use disposable state, then record exact command/output/rc. Commands marked
  **S** have been located in source/CI, not run during Round 1. **E** commands
  actually ran on original source only, as recorded in the plan. None proves the
  replacement works before execution. Do not invent success or install host tools
  without authorization.
- Every changed Bash file: `bash -n <exact paths>` and `shellcheck <exact paths>`
  (**S**, full severity). Every changed Python file: compile using an external
  pycache prefix or AST parse and `ruff check <exact paths>` (**S**). Use existing
  project style and no broad lint ignores. Node build/tests only from confirmed
  `little-mester-mcp/package.json`, not guessed root commands.
- Fast VM suite (**P** until created):
  `python3 -B -m unittest discover -s tests -p 'test_vm_*.py' -v`;
  Bash regressions (**S**): `bats tests/`.
- Every test case below supplies expected positive/negative outcomes and relevant
  requirement IDs. A named test family can use table-driven cases, not one test
  asserting only that a command string contains a word.
- Shared Definition of done **DD**: all task checkboxes satisfied; specified tests
  actually pass; integrated contracts reviewed; no unrelated changes; docs/help
  updated; actual commands/results recorded in progress and owning ticket via MCP
  when applicable; checkpoint scope reviewed; host gates labeled accurately.
- On validation failure do not commit. Diagnose the first failing stage, preserve
  private evidence, fix only in-scope defects, rerun failed and related regression
  tests. Retry deterministic tests in disposable fixtures safely. Stop/escalate
  if a required behavior/security/interface must change or valuable host state is
  implicated. No rollback/reset of user work. Live tests require the scoped
  replacement campaign approval in master §9, not authority inferred from this
  task list. Historical ticket handoffs are evidence, not additional vetoes or
  per-command reapproval requirements for disjoint replacement-owned test assets.

## TASK-01: Define CLI/configuration and support-profile contracts

**Purpose:** R02,R28,R29; D01,D02,D10. Establish one parser and diagnostic contract
without touching active launcher behavior.

**Dependencies:** none.

**Execution context:** main `lab` parsing and `lib/` conventions; original
`lab-vm` usage/parser; main CI; plan §5 public CLI/config schema.

**Preconditions:** Round 2 approval recorded; replacement SHA/main relationship
and dirty state verified. No VM operations permitted for this task.

**Files affected:** CREATE proposed `lab-vm`, `lib/vm/config.sh`,
`tests/vm-cli.bats`; REUSE confirmed main `lab` unchanged. CREATE VM CLI contract
section in proposed `docs/vm-architecture.md`. Do not yet route ordinary `lab`.

**Implementation instructions:**
1. Parse documented commands/options once, preserving arguments after `--` in a
   Bash array. Set typed nonsecret values; validate profile regex, memory 1–128,
   depth 0–32, mutually exclusive modes/duplicates before opening state.
2. Parse private JSON config without `source`, command evaluation or token fields;
   document precedence CLI > selected compatibility env > private config > defaults.
   Unknown schema/options/malformed values fail rc2. Defaults exactly plan §5.
3. Implement help/context and `setup` capability discovery; VM state-changing
   dispatches fail explicitly as unavailable until their later task is integrated.
   No silent no-op success. Detect Linux x86_64/KVM/virtiofs capabilities without
   installs; search validated known virtiofsd locations without symlink creation.
   Explicit `setup --install-prereqs` may list distro-mapped userland packages and
   request default-no confirmation, then install only after approval (or --yes
   explicitly on setup). Validate Debian/Ubuntu and Arch/CachyOS package maps;
   use dry-run/mock package managers in tests. Never install/start libvirt/host
   daemons, change groups/kernel settings or network policy implicitly. Other
   distros get manual missing-capability guidance, not untested support claims.
4. Normal/verbose/debug log API writes redacted stage/status to stderr; structured
   query output stdout remains parseable. `--yes` and auto permission flags are
   distinct. No global `set -x` or TUI tee logging.
5. Do not add implicit package installation outside explicit approved setup,
   cloud adapters, a framework, or model repair.

**Tests to implement:** V-CONFIG `flags_before_after_workspace` and
`double_dash_preserves_agent_args` => same parsed values/argv; `conflicts_invalid`
=> rc2 before subprocess/state for bad/missing/duplicate mode/profile/depth/memory;
`config_not_executable` => shell payload remains data, malformed config refused;
`unsupported_host` => actionable missing capability, no TCG. `yes_not_auto` and
`structured_output_redacted` => no permission broadening or secret/log pollution.
`setup_install_default_no` => no package call on absent/negative confirmation;
`setup_explicit_approved_install` => only mapped userland packages, never from
status/run/doctor; `virtiofsd_nonstandard_path` => direct executable path/no host link.
Requirements R02,R28,R29 in every parser/diagnostic family.

**Tests to execute:** **P** `bats tests/vm-cli.bats`; **S** common Bash/static checks
and existing main Bats. Read tests to ensure no active Docker invocation.

**Expected verification outcome:** all parser cases pass; help/context rc0,
invalid/preflight rc2; no state/VM/service mutation.

**Acceptance criteria:** [ ] one parser; [ ] exact help/interfaces; [ ] fail-before-
side-effects tests; [ ] config is data; [ ] existing `lab` unchanged.

**Commit checkpoint:** CONTINUE to C01 with TASK-02; do not commit a functioning
claim for unimplemented commands.

**Failure and recovery:** inspect parsed fixture output and dependency discovery;
fix parser only. Unsupported local tooling is BLOCKED, not permission to install.
Safe deterministic retry. Escalate any public-contract revision.

**Definition of done:** DD plus TASK-02 integration ready; no boot performed.

## TASK-02: Canonical workspace boundaries and private profile store

**Purpose:** R03,R04,R28; D02. One authoritative guard/state resolver, no fallback
definitions or checkout-visible VM control data.

**Dependencies:** TASK-01.

**Execution context:** main `check_workspace`; original recursion overlap guards;
plan profile schema/path layout, sticky descendant field, lock discipline.

**Preconditions:** validated CLI configuration; exact fresh test state root.

**Files affected:** CREATE proposed `lib/state.sh`, `lib/vm/paths.py`,
`tests/test_vm_paths.py`; MODIFY proposed `lib/vm/config.sh`, `lab-vm`;
REUSE main path-policy behavior, strengthen canonical overlap cases.

**Implementation instructions:**
1. Implement path-policy helper CLI `validate-workspace`, `profile-read`,
   `profile-write`, `context` with JSON stdin/stdout for structured data; no eval.
   All paths canonical; verify every existing ancestor is non-symlinked where
   private state requires it. Refuse sensitive ancestors/descendants and stale
   checkout-local `.mester`; explicit unsafe self-write cannot override secrets.
2. Resolve only `$MESTER_STATE_DIR/vm/profiles/<validated-name>`. Validate owner,
   0700/0600 modes, UUID/schema/required fields and no state/share overlap.
   Unknown/legacy store yields diagnostic/refusal, never discovery or migration.
3. Atomic metadata writes using exclusive temp, fsync, rename and directory fsync.
   Use `flock` root allocation lock plus profile lock with consistent root→profile
   order. A failed write retains prior metadata. Reject malformed/foreign ownership.
4. Private runtime payload directory freezes trusted control code/hash per profile;
   only explicit setup/update can install a new payload. No workspace-sourced
   config/functions at shutdown. Remove duplicated guard implementations.
5. Read-only status/context do not create/chmod/repair directories.

**Tests to implement:** V-PATH `canonical_overlap_matrix` => root/home/sensitive/
state/symlink ancestors denied, safe owned dir accepted (R03); `exact_profile_only`
=> traversal/foreign/legacy/other-live-profile refused, never substituted (R04);
`atomic_failure_and_locks` => old state intact on failure, concurrent mutation
serialized; `readonly_no_mkdir` and `runtime_payload_not_live_source` => no query
mutation or accidental control updates (R04,R28). SEC-FS temp fixtures only.

**Tests to execute:** **P** `python3 -B -m unittest discover -s tests -p test_vm_paths.py -v`;
TASK-01 suite and common static checks; existing main Bats.

**Expected verification outcome:** path/metadata refusals rc2 and no export/write;
safe atomic operations rc0; state root never inside selected workspace.

**Acceptance criteria:** [ ] canonical single guard; [ ] private schema/locks;
[ ] no migration/discovery; [ ] read-only queries; [ ] runtime source frozen.

**Commit checkpoint:** COMMIT C01 `feat(vm): define validated profiles and workspace boundaries`;
scope TASK-01/02 files/docs/tests only; all both-task validations first.

**Failure and recovery:** retain previous metadata and temp diagnostics; no repair
of existing profiles. Retry only isolated fixtures; malformed real state escalates.

**Definition of done:** DD; C01 SHA and evidence recorded.

## TASK-03: Checksum-keyed base cache and profile disk/seed primitives

**Purpose:** R08; D02,D08. Remove shared mutable base rebuild hazard.

**Dependencies:** TASK-02.

**Execution context:** original `lib/vm/disk.sh`, pinned URL/SHA and boot-sector
check; plan cache/backing-chain rules.

**Preconditions:** private validated store and locks, disposable image fixtures.

**Files affected:** CREATE proposed `lib/vm/disk.sh`, `tests/vm-disk.bats`;
MODIFY proposed CLI base dispatch; REUSE source pin as initial candidate pin,
subject to download verification (not a promise remote availability persists).

**Implementation instructions:**
1. Use URL+SHA explicit pin; cache path keyed by SHA, locked during population.
   Download to private staging, verify SHA before conversion/use, validate qcow2
   standalone/bootable, atomic install, record resulting artifact hash.
2. Never replace an existing cache member in place, even `--yes`. Corrupt existing
   cache blocks with diagnosis. New pin creates new path; overlays retain absolute
   backing references. No base garbage collection in this scope.
3. Create named overlay only when absent; verify existing backing chain matches
   profile metadata. `rebuild` later resets target overlay only. Disk capacity
   validation checks free space before staging; nested downloads force proxy path.
4. Seed write primitive accepts generated nonsecret cloud config from TASK-06,
   atomic output; no seed generation with provider secrets or broad host data.

**Tests to implement:** V-IMAGE `verified_download_atomic` => correct pin installs;
bad hash/format/boot marker/interruption leaves cache absent and prior valid data
untouched; `parallel_cache` => one installed artifact; `two_overlay_backings` =>
reset A never alters B/base; `nested_download_proxy_only` => no direct fallback;
`insufficient_disk` => no staging. All R08.

**Tests to execute:** **P** `bats tests/vm-disk.bats`; common static checks,
TASK-01/02 regression. Use local fake download/qcow fixtures; no network VM boot.

**Expected verification outcome:** exact fixture backing/hash confirmed, failures
nonzero and no shared data changes; report original download/hash failure stage.

**Acceptance criteria:** [ ] immutable cache; [ ] atomic checksum validation;
[ ] backing-chain validation; [ ] no shared rebuild deletion; [ ] nested proxy.

**Commit checkpoint:** COMMIT C02 `feat(vm): add immutable base cache and profile disks`;
TASK-03 files/tests, all affected fast checks.

**Failure and recovery:** preserve corrupt real cache for inspection, never remove
it automatically. Retry new staging after transient fixture/download failure only
within authorized setup. Escalate pin change or backing-chain inconsistency.

**Definition of done:** DD; C02 recorded; no live image reset.

## TASK-04: Public-only HTTP(S) policy and nested proxy chaining

**Purpose:** R05–R07; D03. Preserve proven network controls rather than rewriting
them for line-count reduction.

**Dependencies:** TASK-02.

**Execution context:** original `lib/vm/proxy.py`, all 28 proxy tests; source
address normalization/route parsing/connect-public functions; plan §6 boundary.

**Preconditions:** private per-profile ports/files, no host route changes.

**Files affected:** CREATE proposed `lib/vm/proxy.py`, `tests/test_vm_proxy.py`;
REUSE reviewed original core/tests with targeted API simplification; MODIFY
proposed config to supply child upstream explicitly, not infer from path shape.

**Implementation instructions:**
1. Retain permission=false, domain/subdomain/exact-host policy, public-default,
   IPv4/mapped/NAT64/transition handling, all-answer DNS validation and validated-IP
   connection. Parent must reject host-local/directly connected public networks.
2. Restrict plain HTTP to 80/CONNECT to 443; strip proxy credentials/hop-by-hop
   headers, bound headers/connections/idle time and reject ambiguous framing.
   Do not log target URLs, bodies, credentials or OAuth query data.
3. Child uses explicit immediate-parent upstream, applies child domain policy
   before contact, preserves parent denial and never falls back direct. Top-level
   direct-public path remains separate. No second DNS lookup after validation.
4. Bind 127.0.0.1 dynamic port, atomic readiness identity; authenticated ownership
   via private state, not exposed host control API. Reload policy atomically with
   last-known-good on malformed/read failure, exact PID identity before signal.

**Tests to implement:** V-PROXY all 28 original scenarios unchanged in meaning
(R05–R07), plus `ambiguous_request_rejected`, `header_limit`,
`slot_exhaustion_recovers`, `idle_timeout_releases`, `denied_reload_keeps_policy`.
SEC-NET tests prove no private/mixed/local/metadata endpoint connect and no
upstream contact on child deny, no origin contact on parent outage.

**Tests to execute:** **E on original/P on replacement**
`python3 -B -m unittest discover -s tests -p test_vm_proxy.py -v`; common Python
checks. Bind only local fixture sockets. Live SEC-NET deferred to TASK-15.

**Expected verification outcome:** public fixtures succeed; forbidden destinations
403 without socket connection; upstream failure 502/no fallback; no request logs.

**Acceptance criteria:** [ ] retained negative cases; [ ] parent chain explicit;
[ ] fail-closed route/DNS; [ ] resource bounds; [ ] secret-safe output/reload.

**Commit checkpoint:** COMMIT C03 `feat(vm): enforce public-only chained proxy egress`;
TASK-04 only, all proxy/static/path regressions first.

**Failure and recovery:** preserve last valid policy; do not broaden allowlists or
disable route guards to pass tests. Unsupported host route discovery escalates.

**Definition of done:** DD; no claim of live LAN/nested isolation.

## TASK-05: Owned QEMU/virtiofs processes and hardened SSH control

**Purpose:** R02,R05,R09,R28; D01–D03. Valid argv, verifiable ownership, bounded
failure capture without stale-PID damage.

**Dependencies:** TASK-03,TASK-04.

**Execution context:** original runner argv/short sockets/SSH key scan and known
raw-PID cleanup flaw; plan stage deadlines and process-identity schema.

**Preconditions:** exact profile, checked KVM before any start, verified image.

**Files affected:** CREATE proposed `lib/vm/runner.sh`, `lib/vm/ssh.sh`,
`tests/vm-process.bats`, `tests/test_vm_process.py`; MODIFY proposed context/doctor
identity reporting. No guest install or application launch yet.

**Implementation instructions:**
1. Pure Bash array builder: KVM only, host CPU/2 vCPUs, shareable memfd RAM,
   virtiofs `vhost-user-fs-pci`, seed/overlay, serial log, restricted user network,
   loopback SSH and selected forward list. Whitelist port/path grammar, reject
   commas/newlines in QEMU-sensitive inputs or encode safely before argv use.
2. Start proxy then virtiofs then QEMU; short private sockets <108 bytes and no
   legacy-path guessing. Record PID/start ticks/exe/exact assets atomically.
   Bounded bind-conflict retry (3 attempts) only for this new run, never kill owner
   of occupied port. No TCG. Startup rollback only processes created/verified here.
3. Central SSH argv `-F /dev/null`, BatchMode, ConnectTimeout, pinned known_hosts,
   identities-only, no agent/X11/implicit forwards; explicit PTY only interactive.
   First complete Ed25519 scan must be from verified owned loopback listener;
   never replace established key on mismatch/partial scan. Document first-boot
   trust assumption. Bound complete remote commands, not just connection setup.
4. Read-only identity validator distinguishes absent/stopped/running/unknown.
   PID reuse, foreign exe/assets or unreadable proc blocks signals/deletion.
   No unconditional TERM/KILL of a file's numeric PID. Graceful daily shutdown is
   later lifecycle's guest poweroff; startup-owned helper cleanup may TERM only
   verified own new processes, no broad process-group/PID sweep.
5. Failure records stage/original rc and bounded QEMU/virtiofs/SSH diagnostics;
   retain disk/logs. A small bounded-log operation in the existing paths Python
   helper drains noninteractive output into 10-MiB segments, at most current+one
   rotated file per stream; track its owned process if separate. Never let full
   log storage block QEMU or rotate away structured failed-run evidence.
   Interactive agent streams are never tee-captured.

**Tests to implement:** V-PROCESS `argv_device_matrix`/`selected_crossings_only`
(R02,R05), `long_socket_and_hostile_path`, `pid_reuse_never_signaled`,
`partial_allocation_rollback`, `port_collision_no_kill`, `ssh_config_ignored`,
`partial_scan_no_known_hosts_write`, `remote_deadline`, `early_qemu_exit_cause`,
`serial_flood_bounded_and_drained`
(R09,R28). Assert actual fake-process signals/absence, not only string presence.

**Tests to execute:** **P** `bats tests/vm-process.bats` and
`python3 -B -m unittest discover -s tests -p test_vm_process.py -v`; common static
checks plus proxy/disk suites. Fake executables/temp state, no real QEMU start.

**Expected verification outcome:** forbidden/unknown state never signaled; owned
startup failures bounded with cause; valid argv matches every selected crossing.

**Acceptance criteria:** [ ] pure argv; [ ] strict owned process identity;
[ ] isolated SSH; [ ] bounded probes; [ ] diagnostic retention; [ ] no TCG.

**Commit checkpoint:** COMMIT C04 `feat(vm): launch owned restricted QEMU profiles`;
TASK-05 files/docs/tests, all affected fast suites.

**Failure and recovery:** unknown identity preserves state and reports doctor;
never kill unrelated port/process to make retry possible. Escalate sandbox/KVM
limitations, retain failed profile and no implicit boot retry after timeout.

**Definition of done:** DD; implementation tests only, real boot still pending.

## TASK-06: Single guest bootstrap and native runtime payload

**Purpose:** R09,R10,R31; D04,D05. Fresh guest ready for project work and Little
Mester development without an agent container or duplicate repair installer.

**Dependencies:** TASK-05.

**Execution context:** original cloud-init/guest installer/config generator,
main MCP package/lockfile/plugins, plan guest Docker choice and version resolution.

**Preconditions:** rootful guest choice approved; image/profile/SSH contracts ready.

**Files affected:** CREATE proposed `lib/vm/cloud-init.sh`, `lib/vm/guest.sh`,
`lib/vm/guest-bootstrap.sh`, `scripts/vm-opencode-config.py`,
`scripts/check-vm-cloud-init-schema.sh`, `scripts/vm-python-tools.sh`,
`tests/test_vm_cloud_init.py`, `tests/test_vm_guest.py`,
`tests/test_vm_opencode_config.py`; REUSE confirmed `little-mester-mcp/` manifests,
`agent/opencode/` plugin sources (not workspace node_modules).

**Implementation instructions:**
1. Render one schema-valid NoCloud template with UID/GID, authorized PUBLIC
   control key, recursion identity and proxy env. No provider keys/tokens in seed.
   Mount only `workspace` tag and verify guest ownership/mount type.
2. Bootstrap as guest root: supported Node >=20, Python tooling isolated venv,
   selected native harness, optional rootful guest Docker/Compose. Recursion mode
   adds QEMU/virtiofs/KVM group/Bats/ShellCheck/cloud tools inside guest only.
   All package/npm/pip/Docker pulls through proxy, no host installs.
3. Resolve latest OpenCode once at fresh setup; save exact version and install
   manifest. Existing up/warm paths cannot call npm view/install implicitly.
   Copy MCP source/lockfile to guest-private dir, `npm ci && npm run build` there.
   Render config from source preserving unrelated provider/permission settings;
   use native V2 MCP shape and separate CLI plugin config; avoid deprecated fields
   for newly generated VM data. Do not rewrite unrelated Docker configs globally.
4. One payload hash includes all effective bootstrap/harness/config/plugin/MCP
   inputs using relative paths and file modes, same hash algorithm later update.
   Root-owned success marker written only after all checks. Capture clean
   cloud-init JSON/schema/readiness; exit2/recoverable errors never treated ready.
5. Remove rootless installer/sub-ID/netfilter-repair paths from new VM bootstrap;
   no Docker wrapping the agent. Guest-runtime none must skip Docker completely.

**Tests to implement:** V-GUEST `generated_schema_and_scripts` valid,
`runtime_none_no_docker`, `native_harness_private_mcp`, `latest_resolved_once`,
`payload_hash_relative_and_complete`, `bootstrap_failure_no_success_marker`,
`cloud_init_recoverable_blocks`, `recursion_tools_present` (R09,R10,R31).
Config tests preserve explicit denies/non-VM provider settings; SEC-SECRET seed
canary absent (R15 supporting). UID/GID and proxy env assertions mandatory.

**Tests to execute:** **P** `python3 -B -m unittest discover -s tests -p test_vm_guest.py -v`;
cloud-init/config unit suites; **E original/P replacement**
`bash scripts/check-vm-cloud-init-schema.sh`; common static. **S** MCP commands
from package directory in disposable guest runtime at HOST-CLEAN milestone,
not install/build in active runtime without review.

**Expected verification outcome:** schema valid and fake bootstrap exact packages/
commands; failed stages never mark ready; config valid/private/deny-preserving.

**Acceptance criteria:** [ ] one bootstrap; [ ] native harness; [ ] optional guest
Docker; [ ] recursion tooling; [ ] exact version/hash; [ ] no warm install;
[ ] secret-free seed; [ ] private lockfile MCP build.

**Commit checkpoint:** COMMIT C05 `feat(vm): provision native guest harness and workloads`;
TASK-06 scope and fast checks. Schedule scoped HOST-CLEAN evidence separately.

**Failure and recovery:** keep overlay/serial/provision diagnostics; no active
parent reprovision to test changes. Pin/package incompatibility escalates; fresh
disposable attempt only after diagnosing original failure.

**Definition of done:** DD; no full host acceptance claim without HOST-CLEAN.

## TASK-07: Idempotent safe lifecycle, resources and descendant handling

**Purpose:** R04,R16–R19,R28; D02,D08. One lifecycle state machine preserves
valuable overlays/workspaces and refuses ambiguous cleanup.

**Dependencies:** TASK-06.

**Execution context:** original suspend/resume/resize/memory/live-child code;
plan state transitions, deadlines, sticky descendants and auth hooks.

**Preconditions:** image/process/guest contracts verified; use auth export hook
that refuses auth-enabled deletion until TASK-10 exists, never a success stub.

**Files affected:** CREATE proposed `lib/vm/lifecycle.sh`,
`scripts/vm_memory_budget.py`, `scripts/vm-recursion-live-children.py`,
`tests/vm-lifecycle.bats`, `tests/test_vm_memory_budget.py`,
`tests/test_vm_recursion_live_children.py`; MODIFY proposed CLI and metadata.

**Implementation instructions:**
1. Implement absent/starting/provisioning/ready/suspended/failed/unknown transitions
   under locks. Same-workspace ready up verifies identity and bounded readiness,
   returns without bootstrap/config transfer. A→B graceful restart uses same
   overlay/key; B→B no restart. Never discover a different profile.
2. Resource preflight before snapshots/profile/image/process writes and before
   resize/switch shutdown: depth defaults 8/4/2/1, validated explicit setup RAM,
   stored allocation; MemAvailable plus cgroup ancestry and 512 MiB reserve.
   Recheck under root allocation lock; reserve concurrent starting allocations
   to avoid two not-yet-resident QEMUs each passing. Reclaim only proven target
   private RSS for managed resize, never ancestor/other VM allocation.
3. Suspend checks guest process inventory vs exact managed disks. Unknown/unreadable
   child blocks; explicit recursive suspend calls frozen child control runtime
   deepest-first. Fail child→leave parent running. No source-tree CLI search.
4. Reuse the same descendant-safe graceful helper for suspend, resize and workspace
   switch. On timeout preserve state, no forced QEMU kill. Resume validates
   recorded path/state; missing moved workspace requires explicit selection.
5. Destructive down/stop/rebuild prints exact effect, asks unless preapproved yes,
   always requires explicit sticky descendant discard acknowledgement. No Compose
   `down -v` against arbitrary user's project. Auth export guard runs first;
   stopped guest requiring export blocks with explicit resume/export guidance.
   Delete only exact owned profile assets after verified processes gone; preserve
   host workspace, base cache, reports. Rebuild allocates fresh target only.

**Tests to implement:** V-LIFE repeated commands/A→B→B/no package calls; failed
shutdown leaves overlay/profile; unknown child/foreign PID no signal;
recursive child failure leaves parent; switch and resize cannot skip child guard;
sticky discard remains after lowering max; auth pending blocks destructive path;
rebuild A keeps B/base/workspace/report (R04,R16–18). V-RESOURCE all source seven
scenarios plus defaults/override/metadata/cgroup unreadable/concurrent reservations
and before-side-effect ordering (R19). Verify actual mocked call order/counts.

**Tests to execute:** **P** `bats tests/vm-lifecycle.bats`, memory/live-children Python
suites, full VM Python + Bats milestone, common static checks. Live E2E-LIFE only
with TASK-15 scoped host approval; never use valuable original overlays.

**Expected verification outcome:** rc0 only verified transition, preflight rc2 no
mutation, shutdown124 preserves; zero unintended profile/workspace/base changes.

**Acceptance criteria:** [ ] idempotent exact profile; [ ] non-destructive daily
lifecycle; [ ] all shutdown paths child-safe; [ ] locks/budget; [ ] exact delete;
[ ] no shared base reset; [ ] auth fail-closed integration hook.

**Commit checkpoint:** COMMIT C06 `feat(vm): add safe profile lifecycle and resource checks`;
TASK-07 plus full fast milestone. Record live tests separately.

**Failure and recovery:** unknown/stale identity preserved for doctor, no raw PIDs
or force deletion. Stop after host shutdown/resize failure; retry only after exact
state review. Allocation reservation crash recovery checks identity, not age alone.

**Definition of done:** DD, full fast milestone results recorded.

## TASK-08: Native agents, explicit providers and real permission modes

**Purpose:** R01,R11,R13,R26; D05. An agent launch is not model-service management
and parallel agents do not imply nested virtualization.

**Dependencies:** TASK-07.

**Execution context:** main launcher agent argument handling; #50 contract;
original native command paths and model ensure/cloud rejection flaw; V2 CLI/config
guides linked in review. Pin-specific APIs require compatibility verification.

**Preconditions:** ready profile with native runtime; no auth bridge needed for
local/model-none initial cases. Unsupported harness not silently substituted.

**Files affected:** MODIFY proposed `lab-vm`, `lib/vm/guest.sh`,
`scripts/vm-opencode-config.py`; CREATE proposed `lib/vm/services.sh`,
`tests/vm-agent.bats`, `tests/test_vm_agent.py`; REUSE confirmed Aider command
knowledge from main and fixed supported native adapter installation in TASK-06.

**Implementation instructions:**
1. Native agent at `/workspace`, one argv builder for plain and browser paths;
   `--permissions auto`/`--auto-accept` request exactly one supported `--auto`;
   ask omits it, invalid/conflicting modes refused. Preserve explicit denies and
   user agent args after `--`. No config blanket allow or live permission changes.
2. Provider config local Ollama/llama.cpp maps only chosen API port, remote uses
   public proxy/native credentials; none opens agent configuration/shell without
   health gate. Local failure reports diagnostic warning/error according to
   requested completion operation, never starts/repairs Docker/model server.
3. Fixed VM harness adapters OpenCode and Aider, each with valid installation,
   invocation and provider environment; OpenCode-specific auto flags never reach
   Aider. No generic host-command strings or claimed Claude/Codex support.
4. Repeated native agent attaches share one profile, independent SSH/terminal
   processes. Allow explicit relative `--cwd` only within canonical selected
   mount for worktree selection; reject escape. Warn about concurrent writers;
   do not implement scheduling/merging/ownership supervisor.
5. Interactive stdout remains terminal, exit status propagated; no transcript
   capture. Model status/diagnose bounded read-only; CLI startup never repairs.

**Tests to implement:** V-AGENT exact cwd/args/mode once/ask/conflicts/Aider no auto
(R01,R13); local/remote/none provider matrix/no Docker commands/no hardcoded
llamacpp-only rejection (R11); concurrent independent session/cwd escape/child
not implicitly requested, nonzero agent return (R26,R28). HUMAN-PERM separate.

**Tests to execute:** **P** `bats tests/vm-agent.bats` and
`python3 -B -m unittest discover -s tests -p test_vm_agent.py -v`; config/lifecycle
regressions, common static; **P** E2E-MODEL/E2E-AGENTS via later host suite.

**Expected verification outcome:** no host model mutation, exact native invocation,
no agent container; unused/missing provider doesn't prevent setup/auth access.

**Acceptance criteria:** [ ] native OpenCode/Aider adapters; [ ] all provider modes;
[ ] explicit denies untouched; [ ] one argv path; [ ] constrained session cwd;
[ ] model diagnostics read-only; [ ] actual permissions still human-gated.

**Commit checkpoint:** COMMIT C07 `feat(vm): launch native agents with explicit providers and permissions`;
TASK-08 scope and affected fast suites.

**Failure and recovery:** diagnose provider/config vs transport separately; no
restart model service or blanket allow workaround. Unsupported CLI version blocks
adapter capability, do not guess options. Live model/UI failures escalated.

**Definition of done:** DD; E2E/HUMAN results not inferred from argv mocks.

## TASK-09: VM metrics and terminal feature parity without Docker regression

**Purpose:** R12; D03,D05. Retain existing useful integrations, not unrelated UI
redesign or a host-data filesystem escape.

**Dependencies:** TASK-08.

**Execution context:** main `agent/metrics/gather.py`, metrics tool/TUI and exit
plugins; original concurrency/nonblocking/error UI changes; plan child defaults.

**Preconditions:** explicit optional metrics capability; Docker mode untouched.

**Files affected:** MODIFY confirmed `agent/metrics/gather.py`,
`agent/opencode/plugins/host-metrics/index.ts`,
`agent/opencode/plugins/host-metrics-tui/tui.tsx`,
`agent/opencode/plugins/little-mester-exit-confirm/tui.tsx` only where necessary;
MODIFY proposed services/guest config; CREATE proposed
`tests/test_vm_metrics_service.py`; MODIFY confirmed `tests/exit-confirm.bats`.

**Implementation instructions:**
1. Reuse curated summary/metrics/history endpoints, bounded sample history and
   asynchronous GPU collection. VM host listener explicit 127.0.0.1, existing
   Docker private-container bind behavior unchanged. No Docker daemon dependency.
2. Bind one verified per-user immediate-host metrics service idempotently, selected
   guestfwd only. Unknown listener no hijack/kill. Child metrics off unless explicit
   reviewed child-local endpoint; never ancestor service by inheritance.
3. Guest failure warns/displays retry/error state without blocking agent. Preserve
   current TUI plugin interface, logo and exit-confirm behavior; CLI config separate
   from server config. Do not claim global dialog styling fixed from exit patch.

**Tests to implement:** V-METRICS seven original service cases including slow GPU/
parallel clients/unknown endpoint plus disabled/missing transport and child default
off; exit-confirm existing static guards plus distinct cancel/confirm callbacks.
All R12; source fixtures avoid real GPU assumptions.

**Tests to execute:** **P** metrics Python suite; **S** `bats tests/exit-confirm.bats`
and existing TUI guards (discover exact test filenames); common Python/static
checks. HUMAN-TUI records real renderer behavior later, not simulated pass.

**Expected verification outcome:** metrics errors never block launch; bounded
collector correct; host-Docker tests pass unchanged; plugin load test defined.

**Acceptance criteria:** [ ] endpoint boundary; [ ] optional/nonblocking;
[ ] no Docker regression; [ ] no child ancestor default; [ ] existing TUI parity.

**Commit checkpoint:** COMMIT C08 `feat(vm): preserve metrics and terminal integrations`;
TASK-09 files only with all affected static/Python/Bats checks.

**Failure and recovery:** preserve optional failure indicator and start agent;
no widening bind/host mounts. Incompatible plugin API requires isolated supported-
version test and escalation rather than active OpenCode restart.

**Definition of done:** DD; HUMAN-TUI still explicit live gate until observed.

## TASK-10: Scoped native OAuth and provider credential persistence

**Purpose:** R14,R15; D06. Native login and secret lifecycle, not copied auth DBs.

**Dependencies:** TASK-08,TASK-09.

**Execution context:** original browser bridge/server/client 13 tests, lifecycle
auth escrow guards, native V2 docs, #45 full criteria; plan §6/§7 version decision.

**Preconditions:** selected installed version has a reviewed compatibility entry;
no use of active user's auth in tests. Live login separately owner-controlled.

**Files affected:** CREATE proposed `lib/vm/auth.sh`,
`lib/vm/browser-bridge-server.py`, `lib/vm/browser-open.py`,
`lib/vm/browser-open.sh`, `tests/test_vm_browser_bridge.py`,
`tests/test_vm_auth.py`; MODIFY proposed lifecycle/agent hooks;
CREATE proposed `docs/chatgpt-auth.md`.

**Implementation instructions:**
1. Retain existing narrow MCP protocol/client instead of inventing another host
   API. Centralize authorize URL validation, per-session random token/body limits,
   one callback port from exact redirect_uri. Forward before unchanged browser
   open; no callback/code capture. Ignore user SSH config/agent/X11.
2. Give callback a per-attempt lease: authenticated release on detected native
   login success/cancel, session-close release, expiry timeout (120s default,
   configurable <=600s), plus interrupt/process-exit cleanup. Use supported native
   auth-status only, no auth DB reads/polling secret exports. When success/cancel
   cannot be observed reliably for a new version, close via bounded lease and
   report that limitation; do not claim immediate completion cleanup accepted
   until a compatible integration is verified. Persistent TUI master alone is not
   sufficient lifetime control. Port conflict fails without browser opening.
3. Native auth store persists in overlay. Escrow off imports nothing; auto/provider
   selectors use capability adapter table (OpenAI export/import and supported
   runtime Anthropic/Kimi API-key mechanisms). Unsupported explicit selectors
   fail clearly. No raw DB or broad directory copy, no credentials in CLI argv.
4. Runtime SSH stdin transfer, private escrow owner/modes/no symlink/no overlap;
   import once per intended provider/profile, never overwrite fresh guest refresh
   state on every launch. Atomic export validated structurally without printing,
   fsync/replace. Export failure blocks down/rebuild/reset before deletion.
5. Do not wake stopped guest implicitly to export. Preserve overlay and give
   explicit resume/export instructions. Never log TUI/auth URL/body/token/debug
   env. Add canary-safe tests; require owner proof for refreshed tokens.

**Tests to implement:** V-AUTH 13 retained source cases plus invalid/absent token,
oversize, expiry/release success/cancel/interrupt, occupied callback/no open,
concurrent sessions, unknown-version fail-closed (R14); escrow off/auto/selectors,
unsupported adapter, path/modes/symlink, malformed export/atomic failure/old escrow
retained, stopped guest preserves disk (R15). SEC-SECRET no seed/log/source canary.

**Tests to execute:** **P** browser/auth Python suites, agent/lifecycle regressions,
common static. HUMAN-AUTH only under TASK-15 exact private handoff; no real tokens
in fixtures/output. `opencode auth` commands must be confirmed via pinned guest
`--help` before adapter implementation; do not guess version-specific flags.

**Expected verification outcome:** one exact temporary channel, no host generic
capability; failure leaves guest/current escrow intact. No local test auth claim.

**Acceptance criteria:** [ ] native flow/version gate; [ ] one lease cleanup;
[ ] no arbitrary host open/command; [ ] all credential selector semantics;
[ ] secret-safe atomic export-before-delete; [ ] independent sessions.

**Commit checkpoint:** COMMIT C09 `feat(vm): add scoped native auth and credential persistence`;
TASK-10 scope, all auth/agent/lifecycle negative tests first.

**Failure and recovery:** preserve existing guest and escrow, no delete/recreate,
no API-key/device substitution for required browser flow. An unavailable supported
completion signal is a compatibility blocker, not a reason to assert cleanup pass.

**Definition of done:** DD; human callback/persistence/refresh evidence tracked.

## TASK-11: Filtered revision-bound candidate source snapshots

**Purpose:** R20,R23; D07. A child must test current intended Little Mester changes,
not stale clean HEAD or historical credentials.

**Dependencies:** TASK-07,TASK-10.

**Execution context:** original `vm-recursion-workspace.py` exclusion/copy/manifest
and synthetic Git behavior; plan fresh-run/no-overlay model.

**Preconditions:** explicit selected self-write workspace and private run root;
no ongoing writes during snapshot materialization (race detected/refused).

**Files affected:** CREATE proposed `scripts/vm-recursion-workspace.py`,
`tests/test_vm_snapshot.py`; MODIFY proposed path/config integration and docs.

**Implementation instructions:**
1. Inventory intended current files including dirty/untracked, exclude original
   secret/state/DB/model/cache classes, no `.git` history. Preserve modes/content,
   relative in-scope symlinks; reject escaping/dangling-to-excluded/special files.
2. Manifest canonical source HEAD, relative path/type/mode/bytes/hash, tree digest;
   do not publish source absolute private paths to child unnecessarily. Bounded
   2 GiB default/16 GiB maximum, free space >=2*included bytes+128 MiB before copy.
3. Stage privately with lock; verify copied manifest and re-inventory source to
   reject concurrent edits. Synthetic Git commit contains filtered current tree
   only. Atomic snapshot install; failed snapshot never replaces existing run.
4. Every changed tree gets fresh run/profile. Reuse only identical source manifest
   AND untouched retained snapshot. No active-source file overlay, partial allowlist
   parity, auto-repair of source snapshots, or copying host control state.
5. Parent chooses/pushes source; no child discovery/request transport. Generic
   #55 structured data sharing/MCP stays out of scope.

**Tests to implement:** V-SNAPSHOT tracked+dirty+untracked actual content, source
HEAD unchanged but dirty tree changes digest, historical secret inaccessible via
Git, excluded env/auth/keys/weights/SQLite/caches, modes/internal symlink rewrite,
excluded target/escape/FIFO refused, file-replacement race rejected, cap/disk check
before copy, reuse modified snapshot rejected (R20,R23). Real temp Git fixtures,
not only mocked lists.

**Tests to execute:** **P** `python3 -B -m unittest discover -s tests -p test_vm_snapshot.py -v`;
path/secret regressions and common Python checks.

**Expected verification outcome:** independent source snapshot contains exact
intended VM changes and no forbidden content/history; failure no installed run.

**Acceptance criteria:** [ ] complete source provenance; [ ] secret exclusions;
[ ] bounded/race-safe copy; [ ] synthetic Git; [ ] no source overlay/discovery.

**Commit checkpoint:** CONTINUE to C10 with TASK-12.

**Failure and recovery:** preserve source and prior snapshots; ask agent to stop
concurrent writes and create a fresh run after diagnosis. Never mutate active export.

**Definition of done:** DD; validated TASK-12 integration input.

## TASK-12: Bounded candidate VM launch and repeated boundary

**Approved refinement2026-11-06:** INF-01/03/05/06/10 supply the missing complete
model-capability relay and security/performance proof. A raw loopback TCP forward
or passing model flags is not sufficient: the upstream is an inference-only
gateway, identity-bound at each hop, with no raw backend/ancestor fallback.
Default children still inherit no capability and no weights/model-server copies.

**Purpose:** R07,R21,R22,R25; D03,D07. Actually run changed VM implementation and
propagate bounded recursive authority, not simply start another agent.

**Dependencies:** TASK-11.

**Execution context:** source recursion policy and child proxy chain, plan G0/G1/G2
workflow, immediate-parent capability mapping, persisted identity limitations.

**Preconditions:** supported nested KVM/CPU flags checked before snapshot/state;
max explicit at outer, inherited identity consistent. Guest tooling from TASK-06.

**Files affected:** CREATE proposed `lib/vm/recursion.sh`,
`tests/vm-recursion.bats`, `tests/test_vm_recursion.py`; MODIFY proposed CLI,
config/runner/guest/services/auth/snapshot interfaces only within these contracts.

**Implementation instructions:**
1. Load persistent guest depth/max; conflict with caller environment fails. Opt-in
   required, max default0, range0–32, child depth+1 and max not increased. Apply
   guard before capabilities/snapshots/state; supported-launcher policy only.
2. Candidate launcher path is exactly frozen candidate `lab-vm`, with candidate
   runner/proxy/cloud-init/bootstrap/config/MCP payload from whole manifest; start
   fresh named G1 overlay/seed in private parent-guest state. Record candidate
   payload hashes and effective launch parameters, never select old profile.
3. Repeat restricted networking and independent child proxy; explicit immediate-
   parent upstream only. Child gets snapshot/state independent of parent source.
   Default no browser/metrics/model/credentials/control endpoints. Selected local
   model may opt into one fixed immediate-parent relay, credential selectors
   parent-push supported providers only; no ancestor directory/path inheritance.
4. Sticky descendant possible flag stays until overlay discarded. Children use
   private per-run profiles and stored frozen controls for later lifecycle, not
   searching workspace for a matching executable. Recursion-clean refuses any
   unknown/live process and deletes only exact confirmed stopped run state.
5. Report context JSON with source/tree/payload/version/depth/KVM and effective
   root, no tokens or fabricated host identity. Do not add host-control MCP service.

**Tests to implement:** V-RECURSE default/limits/max reset/env conflict/refusal
before snapshot, missing KVM/CPU (R22); modified candidate launcher/seed/proxy
hashes actually used rather than parent input (R21); separate state/depth, no
implicit ancestor endpoint, explicit approved relay only, parent chain outage no
direct fallback (R07,R25); sticky discard and exact cleanup ownership (R18).

**Tests to execute:** **P** `bats tests/vm-recursion.bats`, recursion/snapshot Python
suites; full VM Python+Bats milestone and common static. E2E-DEPTH/E2E-SELF still
real-host gates, not mocks or `/dev/kvm` checks.

**Expected verification outcome:** guards refuse without state; candidate launch
uses only fresh selected source/state; no parent capability inherited silently.

**Acceptance criteria:** [ ] actual candidate VM source binding; [ ] depth guard;
[ ] repeated boundary; [ ] private lifecycle controls; [ ] endpoint defaults;
[ ] cleanup ownership; [ ] root-proof depth NOT claimed.

**Commit checkpoint:** COMMIT C10 `feat(vm): run revision-bound recursive candidate VMs`;
TASK-11/12 cohesive snapshot+child capability, full fast milestone first.

**Failure and recovery:** unsupported nested profile BLOCKED before state; preserve
failed candidate/report. Do not weaken guard/TCG/network or change parent defaults
to get a pass. New source requires new run, never repair existing candidate.

**Definition of done:** DD; C10 recorded, live recursive proof explicitly pending.

## TASK-13: Changed-VM fail→fix→fresh-pass test driver

**Purpose:** R20,R21,R24,R28; D07,D09. Deliver automated repeatable testing of
Little Mester changes, including the VM it provisions.

**Dependencies:** TASK-12.

**Execution context:** master §5 normative workflow and E2E-SELF; original acceptance
runner's source-overlay/profile-discovery failure lessons, not its repair machinery.

**Preconditions:** known-good coordinator runtime frozen, new exact run ID/source;
test scope authorized by outer recursion policy. No model creds for shell subgate;
full child-agent gate requires explicitly configured provider capability.

**Files affected:** CREATE proposed `scripts/vm-self-test.py`,
`tests/test_vm_self_test.py`, `tests/fixtures/vm-selftest/` fixture definitions;
REUSE proposed public lifecycle/snapshot/recursion interfaces, do not expose internals
or duplicate provisioning/SSH code. MODIFY self-development docs.

**Implementation instructions:**
1. Implement proposed CLI from master §9, stage machine preflight→snapshot→candidate
   boot→source parity→tests/workload/boundary/depth→parent continuity→report.
   Require fresh run-specific state; optional `--yes` authorizes only selected run.
   Structured reports atomic 0600 with all required provenance/stage/cause fields.
2. Parent G0 runs candidate launcher from frozen snapshot to create G1 fresh;
   verify complete source manifest, changed effective QEMU options, seed/bootstrap
   sentinel and installed guest payload hashes. Run tests from that source in G1,
   then G1 candidate launches G2 and checks G3 refusal at cap2. Actual agent task
   required for full E2E result; shell-only result separately labeled incomplete.
3. Provide disposable fixture transformation in a full source COPY: insert a
   bootstrap prefix after its shebang that writes `A` to guest-only
   `/var/lib/little-mester/selftest-candidate` then exits42 before normal success
   marker. In the copied QEMU array builder change the vCPU count from2 to1 and
   update the copied behavioral test's expected vCPU count to1. Verify boot fails
   at guest provisioning, not preflight, with actual `-smp 1` in effective argv.
   Fix only that copy: remove exit42 and change sentinel to `B`. Create a fresh
   snapshot/profile/overlay/seed; require sentinelB, actual guest `nproc`/QEMU vCPU1,
   expected candidate tests passing and new manifest. Preserve failedA evidence.
   This proves both VM bootstrap and VM launch changes were exercised; a sentinel
   in workspace alone is insufficient. Never edit the user's source for injection.
4. G0 continuity checks source mount/ongoing workload and agent/session heartbeat.
   Use parent-initiated SSH for results, no upward control. No active snapshot
   patching, stable hardcoded paths, profile discovery or automatic package repair.
5. Bound remote stages and total60m; timeout/interrupt records partial report,
   closes only owned test SSH transport, preserves candidate VM/logs. Explicit
   cleanup requires run manifest, retains evidence, deepest-first graceful shutdown;
   unknown PID/profile blocks cleanup. Repeated attempts fit approved scope without
   repetitive questions, but no automatic model/host changes or arbitrary retry.

**Tests to implement:** V-SELF `changed_vm_source_required` fails parent/stale hashes;
`bootstrap_and_lifecycle_mutations_observed`; `intentional_failure_then_new_fixed_run`
validates different tree/profile/seed; `depth_and_parent_continuity`; stage fault
matrix (copy/boot/cloud-init/SSH/test/report/timeout/SIGINT); `only_owned_cleanup`;
`missing_agent_not_full_pass`; invalid human/evidence results cannot become PASS
(R20,R21,R24,R28). Fake driver tests plus real E2E acceptance specification.

**Tests to execute:** **P** `python3 -B -m unittest discover -s tests -p test_vm_self_test.py -v`;
snapshot/recursion/process suites, common static. Proposed live command only under
TASK-15 preflight/handoff; never run against original #41 stable snapshot.

**Expected verification outcome:** deterministic runner distinguishes observed
failure, blocked environment, unrun stages and success; full success impossible
without actual candidate VM/agent/continuity evidence.

**Acceptance criteria:** [ ] changed VM implementation really exercised;
[ ] fresh fail/fix provenance; [ ] G1/G2/cap; [ ] child agent task; [ ] deadlines;
[ ] no active repair/discovery; [ ] retained reports/scoped explicit cleanup.

**Commit checkpoint:** COMMIT C11 `test(vm): add changed-VM fail-fix self-test workflow`;
TASK-13 driver/fixtures/docs/test scope, all runner regressions first.

**Failure and recovery:** preserve report/current parent, diagnose first stage;
never reuse failed overlay for fresh-provisioning claim. Unsafe candidate state
stops loop pending inspection. Escalate need for additional host authority.

**Definition of done:** DD; E2E-SELF real pass remains release-blocking if unavailable.

## TASK-14: Explicit state-preserving guest updates and validation tools

**Purpose:** R27; D04,D06. One payload/version model for setup and update, never
reprovision merely to sync source changes or warm-start agent.

**Dependencies:** TASK-10,TASK-13.

**Execution context:** original vm-update/install-tools/python-tools tests and
SIGPIPE history; proposed guest payload hash/version manifest from TASK-06.

**Preconditions:** safe managed ready guest; no active TUI or guest workload for
disruptive OS/Docker operation, exact installed version known.

**Files affected:** CREATE proposed `scripts/vm-update.sh`,
`scripts/vm-install-tools.sh`, `scripts/vm-opencode-tui-check.py`,
`tests/test_vm_update.py`, `tests/test_vm_install_tools.py`,
`tests/test_vm_opencode_tui_check.py`; MODIFY proposed guest/auth/CLI helpers;
REUSE proposed Python-tools and same guest payload installer.

**Implementation instructions:**
1. Preserve default scan targets opencode/config/tools; --all additionally guest
   OS/Docker; named targets supported; conflicting targets fail before SSH.
   Scan may refresh guest package metadata but must not apply package changes.
2. Report exact current/candidate versions/hash then require confirmation (yes
   only explicit approved override). Cancellation/current returns0 no install;
   failed scan nonzero no apply. Recheck active TUI/workloads immediately before
   apply; compare full process argv/TTY, not pgrep string.
3. Apply only reported selected changes via shared guest payload/tool functions,
   same content hash as setup. Preserve disk/base/keys/workspace/auth; export
   supported refresh state before risky credential-affecting operation, no source
   overwrite. New unsupported OAuth version blocks enabling auth until checked.
4. Warm run/up/agent never call updater/network installer. Tools install operates
   only selected guest; no host package install, no replacement unrelated symlinks.

**Tests to implement:** V-UPDATE retain 13 update/4 TUI/2 tool source scenarios;
add same hash from setup/update, no-op warm paths, unsupported auth upgrade gated,
state/auth retention on partial failure. Include apt parser consumes full pipe
without status141. R27; R15 supporting credential-preservation cases.

**Tests to execute:** **P** update/install-tools/TUI Python suites; common static,
full fast milestone. Live update scan/cancel/state-retention in E2E-LIFE under
TASK-15, no original guest update authorized by task implementation.

**Expected verification outcome:** clear CURRENT/UPDATE/BLOCKED, only approved
selected changes applied; no overlay/credential/workspace deletion or warm install.

**Acceptance criteria (fixture milestone):** [x] explicit scan/apply split;
[x] precise guards; [x] one payload hash; [x] in-place retention;
[x] no host installs. Live update acceptance remains TASK-15.

**Commit checkpoint:** COMMIT C12 `feat(vm): add explicit state-preserving guest updates`;
TASK-14 scope, full fast milestone and static checks.

**Failure and recovery:** retain original guest/manifest on failure; report partial
package state truthfully, no rollback claims without actual rollback mechanism.
Owner handoff for interrupted apt, never automatic clean rebuild.

**Definition of done:** DD; precise update/retention live evidence tracked.

## TASK-15: Scoped real-host, security and human acceptance suites

**Current extension2026-11-06:** INF-01–11 from the controlling inference amendment
are prerequisites to final acceptance: operation-level model isolation, real
per-hop relays, measured one-level performance and existing procedure/usability
gaps. V-INFERENCE and E2E-MODEL-PERF are required alongside existing local-first
gates. Older fixed-loopback discovery/raw-forward assumptions are not sufficient.
Live OpenAI-specific acceptance remains deferred; required synthetic credential
safety and non-auth security are not deferred. Current phase ends in a pause.

**Historical TASK-15 sequencing checkpoint (2026-11-05; superseded by the current
authorization above):** C13's local code checkpoint is
committed; it is not complete live acceptance. The owner's preflight on `336cf69`
passed after the cgroup fix, but no guest has been started. Follow master §9's
"Acceptance-readiness revision" and TASK-15A/15B below before the next full host
campaign. At that checkpoint, C13a was user-authorized and implemented locally;
C13b, live campaign, credential action and VM-default cutover remained
unauthorized.
These task sections specify execution; they do not themselves authorize live work.

**Purpose:** R05–R25,R30,R34; D09. Replace historical campaign/repair machinery
with bounded reproducible evidence for all required behaviors.

**Dependencies:** TASK-14.

**Execution context:** plan §9 complete gate definitions; original representative
isolation/Compose/provenance tests and campaign exclusions. Original #35/#41–43/#45/
#48/#50/#52/#55/#56 were reread through MCP on 2026-11-05 from `feat/vm-approach`
at `97af1d83a16fce68ddcc3390276c218a1b0cd518`; reconciliation is recorded in master
§9 and progress. Those tickets are branch-local historical inputs. Their absence
or older versions in the replacement store are not blockers; no ticket migration,
reconstruction of old handoffs, or old-state repair is required for C13.

**Preconditions:** local full fast suite passes for the later integrated campaign;
owner-reviewed exact fresh replacement run/state/profile scope documented in
PR_REIMPLEMENTATION_PROGRESS.md, credential safeguards
and preservation plan before live mutation (not before local implementation).
Original active snapshots/VMs and the working Docker/auth session must not be
touched. This 2026-11-05 reconciliation authorizes planning edits only, not host
execution. One later campaign approval covers its recorded stages/retry/cleanup
bounds; no fresh authorization per routine command inside that scope.

**Files affected:** CREATE proposed `scripts/vm-host-acceptance.py`,
`scripts/vm-guest-isolation-check.py`, `tests/test_vm_host_acceptance.py`,
`tests/test_vm_guest_isolation_check.py`, `docs/vm-host-acceptance.md`;
MODIFY `scripts/vm-self-test.py` and `tests/test_vm_self_test.py` to implement and
test the unavailable live adapter; REUSE existing frozen lifecycle/agent/recursion
APIs and available CLI; fixtures for PostgreSQL and
approved harmless agent tasks. No historical repair/source overlays or arbitrary
report-process PID signaling.

**Implementation instructions:**
1. Implement suites clean/daily/workload/boundary/lifecycle/agents/human with exact
   generated run ownership. Preflight read-only; setup/create is separate explicit
   stage, no implicit package/VM/profile repair. Dry-run lists planned assets/effects.
   Allocate a unique immutable source snapshot and private state root per campaign;
   refuse a name/path collision without selecting another existing profile. No
   stable-snapshot requirement, active-export repair or auto-discovered outer VM.
2. Run all HOST/E2E/SEC/HUMAN groups in master §9; call E2E-SELF driver, don't
   duplicate VM lifecycle code. Implement its real stage adapter, using the frozen
   lifecycle APIs; do not leave a stub or substitute fixtures/manual assertions for
   live VM proof. Keep normal public `run`/`up` gated until TASK-16; the explicit
   acceptance entry can invoke those APIs without changing the production default.
   Record each independent gate + NOT_RUN/BLOCKED;
   aggregate PASS only if every required selected gate actually passes. Never
   infer user observations from an Enter/default answer or basic health response.
3. Preserve meaningful original negative tests: guest/child/direct/mixed/private/
   ancestor/LAN listeners, files/credentials, guest Compose and repeated streams,
   real permissions and auth lifetime/refresh, resize/suspend/update/cleanup.
   Verify actual listener bind addresses/ownership at host and outer, alongside
   QEMU argv and guest/child probes. Per master §9/#43's historical owner decision,
   a second LAN device is supplementary, not required. Human observations remain
   required for actual TUI/permissions/browser behavior, never inferred from mocks.
4. Evidence scoped to exact revision/runtime/versions and stage; report structural
   sanitized fields and fixed observation enums, no raw prompt/token/URL dumps.
   Run ownership manifest drives explicit cleanup, failures preserve reports.
5. Prepare one concise campaign handoff with revision, dry-run/commands, expected
   effects, protected assets, evidence paths, selected providers, budgets and
   retry/cleanup bounds; record in PR_REIMPLEMENTATION_PROGRESS.md. Obtain approval before
   live mutation, not a chain of legacy-ticket signoffs. Execute master §9's order:
   preflight→clean→usable guest/model/human checks→work/state retention→recursive
   proof/boundaries/concurrency→scoped cleanup/evidence review. Allow independent
   checks to retain results if another gate is blocked. Do not claim this coding
   shell is the owner's physical host; check capabilities and label the actual
   execution context. A qualified nested VM is not categorically forbidden.
6. Failure stops dependent stages; bounded diagnosis/fix/revalidation and fresh
   attempts within the approved campaign do not need new per-command permission.
   Default limit: one fresh retry per diagnosed failure per revision, within
   resource/time budgets. Retain evidence, clean only verified run-owned assets
   via supported lifecycle operations, and preserve unknown/unresponsive state.
   Scope expansion/protected-state changes require owner review; persistent
   failure is BLOCKED, not an automatic indefinite retry or a passing result.

**Tests to implement:** V-SELF/V-LIFE driver mock tests for every suite failure/
timeout/interrupt/no-default/no-profile-discovery; `report_scope_complete` maps all
required gates, human missing=BLOCKED; `no_secret_raw_output`; `cleanup_exact_run`;
`active_original_untouched`; immutable unique snapshots despite unrelated live
exporters; exact requested outer or refusal; component RAM-budget evidence;
bounded inherited-stdout/SSH completion; retry/resource limits; actual socket
bind/ownership verification; live-adapter integration contract. SEC/E2E/HUMAN
actual results recorded separately. E2E-LIFE includes live C12 scan/cancel/no-op,
selected exact-version apply and state/auth retention, not only suspend/resize.

**Tests to execute:** **P** host-acceptance/guest-isolation Python suites; full fast
suite/static; planned host commands from §9 only after --help verification and
specific handoff. Record each run ID, command, rc, source hashes and actual outcome.

**Expected verification outcome:** real evidence covers all required plan gates;
unsupported/unavailable/budget failures are BLOCKED before mutation, not false PASS.

**Acceptance criteria:** [ ] complete independent gate coverage; [ ] actual changed-
VM E2E; [ ] live boundary/workload/lifecycle; [ ] human auth/UI/permissions;
[ ] exact scoped evidence/cleanup; [ ] original runtime preserved. Code suite may
commit before owner results, but release remains BLOCKED until all live boxes pass.

**Commit checkpoint:** COMMIT C13 `test(vm): add scoped host and recursive acceptance suites`;
TASK-15 test driver/docs/fixtures and passing local checks. Record live outcomes
as evidence, not fabricated commit prerequisites; C14 consumes required live results.

**Failure and recovery:** stop dependent execution at the first unsafe/failed
stage, retain evidence and record through MCP when applicable; no broad cleanup
or implicit model repair. A diagnosed retry uses a fresh attempt ID inside the
approved campaign limits. Do not make unrelated legacy-run repair or a fresh
ticket authorization a prerequisite for ordinary local fixes or scoped retries.

**Definition of done:** DD for code checkpoint; live acceptance checklist remains
visible and must pass before default cutover/final completion. TASK-15 remains
IN PROGRESS or BLOCKED after its local code commit until every required live
acceptance checkbox passes; C13 alone does not satisfy TASK-16's prerequisite.

## TASK-15A: Prepare explicit routes and Docker rollback (C13a)

**Purpose/dependency:** R01,R29–R33; TASK-15's E2E-DAILY requires a supported
rollback entry point that does not yet exist. This is TASK-16's non-default
preparation slice, not default cutover and not a new architecture. Depends on
C13 plus the cgroup fix at `232180b` / ledger `336cf69`.

**Files:** confirmed `lab`, CLI/helpers under `lib/`, relevant Docker and VM
parser/permission/metrics fixtures; proposed `lab-docker`, route tests, and
truthful route documentation. Inspect contracts before selecting exact paths.
Do not modify auth stores, active container state or public VM startup gates.

**Implementation, only after later authorization:**
1. Extract the supported Docker launcher preserving helper sourcing, argument
   forwarding, current-container identity/reuse, metrics and safety behavior.
   Correct required permission-parser defects with explicit tests, not silent
   compatibility with ignored flags. Preserve denies and Aider restrictions.
2. Route ordinary `lab` to Docker and `lab vm` explicitly to the gated VM entry;
   `lab-docker` exposes the supported fallback. `lab-vm run/up` and their aliases
   remain fail-closed; only the scoped acceptance path invokes lifecycle APIs.
3. Document that staging is not release acceptance. Specify a host smoke that
   records the existing Docker container's identity before/after invocation and
   refuses creation/rebuild/recreation; actual UI/session observation remains
   required. No need to exercise that live session in this implementation shell.

**Tests/outcome:** fixture dispatch/source-helper/flags-before-after-workspace,
exact-once OpenCode auto, ask/auto conflicts, denies, Aider, metrics and exit-code
parity; no accidental VM startup, host Docker socket sharing or DinD. Bats/full
Python regression, changed-shell syntax and full-severity ShellCheck, docs/diff
checks. Existing unrelated failures must be labeled, not suppressed or called PASS.

**Commit:** C13a `feat(lab): stage explicit VM and Docker rollback routes`.
**Done:** executable rollback/explicit routes locally tested; the transition-stage
dispatcher remains routed to Docker until acceptance, while VM remains the intended
released default and Docker becomes its explicit optional fallback. Live rollback
smoke pending; TASK-16/C14 default cutover remains unstarted.

## TASK-15B: Complete staged provider and owner acceptance workflow (C13b)

**Purpose/dependency:** R05–R25,R30,R34; depends on TASK-15A. Turn C13 gate reporting
and partial automation into an executable owner campaign. Master §9's current
readiness revision is authoritative. No live mutation or credential permission is
inferred from this specification.

**Files:** reuse `scripts/vm-host-acceptance.py`, `scripts/vm-self-test.py`,
`scripts/vm-guest-isolation-check.py`, existing frozen lifecycle/agent/auth/recursion
APIs, tests and `docs/vm-host-acceptance.md`. Add small gate-specific fixtures or
helpers only where necessary; no second controller or generic campaign framework.
The C13a route slice is reused rather than redone.

**Implementation units (docs→tests→code after authorization):**
1. **Coverage audit first.** Build the complete gate/subcheck/procedure/evidence/
   cleanup matrix, distinguishing automatic checks, real human observations and
   current gaps. Audit actual nested process ancestry versus depth labels or
   sibling profiles, per-enclosing-level RAM, frozen runtime/config propagation,
   working-directory changes, listener ownership and exact cleanup. Fix any
   false PASS paths. Keep all fifteen gates and their original obligations.
2. **Explicit provider configuration.** Plan bounded read-only discovery for
   existing Ollama and llama.cpp services; owner selects endpoint/model explicitly.
   Validate private campaign config and per-profile selected forwards/model IDs;
   no host auth/config copying or automatic service start/model downloads. Allow
   separate run-owned provider profiles/config slices where the existing one-
   provider profile contract requires them, preserving the intended same-profile
   state tests. Never silently switch a live profile's immutable configuration.
   Execute five real streams per selected local provider and OpenAI; probe health
   alone is insufficient. Record result enums/counts only, never prompts/responses.
3. **Semi-automatic OpenAI login.** Early optional human checkpoint calls the
   existing browser bridge, opens the host browser and waits for the owner's
   direct sign-in with a bounded callback. The owner supplies no credentials to
   the agent/chat. Verify provider completion and removal of temporary callbacks.
   Reuse only this campaign's authorized guest session between stages; do not
   repeatedly ask for login or use ordinary host/Docker auth. Missing terminal,
   EOF/skip/timeout/cancel keeps affected subchecks BLOCKED and continues independent
   tests. Human interaction cannot be inferred from a default/Enter response.
4. **Staged operation and budget.** Add a documented exact-run stage/resume/status
   interface using the existing private manifest. Stages: clean/usability → early
   human/auth → providers/daily/permissions → workload/retention/lifecycle → nested
   proof/boundaries/concurrent agents → final auth → teardown. Pause before costly
   recursion so a usable TUI can be reviewed first. Revalidate revision/config/
   runtime/process ownership on resume; reject duplicate/concurrent unsafe work.
   Preserve independent results. New code/config requires a fresh scoped attempt;
   no auto-adoption of ready profiles. At most 60 minutes per automated stage/
   attempt with tighter operation deadlines; interactive pauses do not extend
   callback expiry. The live handoff must bound total time, download/disk and
   paid-provider use. One diagnosed fresh retry per revision within total budget.
5. **Execute real work and negatives.** Automate guest PostgreSQL Compose health/
   query/persistence/down plus real agent failing-test→fix→pass and two concurrent
   authorized tasks with separate auth ports. Capture DB/tools/auth/key/workspace
   canaries before suspend/resume, selected config/tools apply and resize. Both
   selected apply and scan/cancel/no-op need real evidence; selecting no updates
   cannot satisfy the apply subcheck. Implement outer/child filesystem/network/
   secret boundary operations and actual self-development fail/fix/depth proof.
   Use local models for provider-independent agent tests if explicitly selected;
   never substitute them for missing OpenAI acceptance.
6. **Final credential/cleanup checks.** Optional end checkpoint handles real
   refresh persistence and browser negatives. Provider-specific escrow/export,
   destructive failure/delete/reimport and recursive import require separate
   confirmation and disposable run-owned profiles; never destroy the sole auth
   copy or read/copy unrelated credentials. Skipping keeps those required gates
   BLOCKED. Expiry/refresh unavailable within budget is BLOCKED, not simulated
   success. Verify deepest-first exact teardown and residue/protected assets;
   finalize report after cleanup so teardown evidence is not required before it
   can occur. No premature PASS checklist recording or wholesale attestation.
7. **Approachable owner handoff.** Update the runbook with implemented commands,
   Bash/Fish forms, expected output/time/stop conditions and sanitized status/report
   viewing. Include clean worktree revision update, private config/state creation,
   dry-run review, start, skip, pause/resume and exact cleanup. Do not ask the owner
   to diagnose controller files or invent test commands. Human-only tests need
   actual UI actions and outcomes, not just abstract checklist labels. Keep evidence
   outside exported snapshots; protect `vm-migration-plan.md` and existing runtime.

**Tests to implement:** config/endpoint/model validation and provider-none/selected/
missing-service cases; positive/negative streaming schemas and redaction; native
browser open/wait/success/cancel/EOF/timeout and callbacks removed; no secret state
in logs/argv/report; owner skip blocks only dependencies; authorized session reuse
without repeat login; manifest-version/revision/config/process checks, serialized
stage resume, duplicate mutation refusal, wall-clock and retry bounds; actual
parent/child placement and per-level budgets; fixture Compose/project/volume and
concurrent-agent failures; pre-mutation retention baselines; selected apply and
negative export/delete refusal; final post-cleanup evidence. No mock is live proof.

**Tests to execute:** all affected Python/Bats fixtures, full Python/Bats suite,
changed-file Ruff/compile/shell checks, real `--help`, docs links/fences/consistency,
exact staged diff/scope review. Do not execute the campaign while implementing.

**Commit:** C13b `test(vm): complete staged provider and owner acceptance workflow`.
Record gate matrix and remaining operational choices in progress. If necessary
split this checkpoint into documented tested commits without claiming readiness
early. **Done:** no known absent prerequisite for the selected full campaign;
every gate has a real test procedure; local tests pass; a dry-run-ready owner
handoff exists. Actual host acceptance is still pending.

### Owner scope amendment after C13b — local implementation authorized

The [local-first C14 handoff plan](docs/vm-c14-handoff-plan.md) is the current
contract for TASK-15B, live TASK-15 acceptance, and TASK-16. The owner has
authorized local §1 implementation: minimal repository-owned synthetic-canary
checker, required/deferred subcheck separation, focused regression tests, and
runbook reconciliation. No new framework. Local Ollama/llama.cpp discovery and
exact owner selection are future, separately authorized live-work steps.

Live OpenAI login/streams/refresh and real provider credential roundtrips are
deferred, visible evidence, **not merge/C14 blockers**. Native browser auth and
automated auth/security regressions remain required; deferred checks must not be
reported PASS. Non-auth filesystem/network/secret isolation and real local
workload/lifecycle/nesting/cleanup gates remain required. Split mixed checklist
obligations explicitly rather than waiving entire security gates. The local
runner reconciliation is implemented and fixture-validated; real campaign
evidence remains pending or BLOCKED as listed in the handoff plan.

This amendment supersedes contrary live-auth prerequisites below and TASK-15B's
owner-supplied-checker assumption. Only local code/tests/docs are authorized; no
campaign, service/model startup, credentials, push, merge or C14 activation.

### TASK-15 live execution after C13a/C13b (separate approval; scope amended above)

Record selected local provider/model IDs (read-only discovery only after separate
authorization), exact
revision/config hashes, total time/disk/download/API ceiling, optional checkpoint
choices and auth-storage/export consent. Rerun preflight on that revision; approve
the exact scope once. Execute stages in master §9 order and preserve partial
results without waivers. The local-first campaign does not perform live OpenAI
login or credential forwarding. Review all required gates and cleanup evidence
before C14. Deferred live auth remains outside the local-first result; it does
not satisfy the separate historical live-auth follow-up.

## TASK-16: Documentation, CI, accepted VM default and preserved Docker rollback

**Current prerequisite2026-11-06:** complete INF-11 owner acceptance review and
INF-12 delivery/cutover checklist. C13c-a–f supplement earlier code milestones;
none is automatic C14 approval. Close eight known docs consistency findings,
keep the mission file and private runtime/evidence out of commits, run remote CI
on the actual PR head, and obtain explicit cutover/publication/final merge
instructions at the named checkpoints. The owner's goal to merge does not waive
failed or unperformed tests, uncertain cleanup or preservation requirements.

**Purpose:** R01,R29–R33; D10. Deliver coherent reviewable product, not only new
primitives or a misleading default switch.

**Dependencies:** TASK-15, with all required real-host gates passing for cutover.
TASK-16's non-default explicit route, Docker rollback and documentation/CI staging
may be prepared while TASK-15 is open; final default/rollback route smoke belongs
to TASK-16 and must not circularly block preparation of the route it tests.
The explicit route/rollback preparation is now named TASK-15A/C13a and must occur
before C13b and live E2E-DAILY. Reuse that slice; C14 does not postpone creation of
the rollback route until after a gate requiring it. Planning this order does not
authorize either slice's implementation.

**Execution context:** main `lab`, CI, verify-lab/docs consistency, existing
Docker metrics/config; master approval choices, review path dispositions.

**Preconditions:** for cutover, required C13 evidence reviewed; dirty/source revisions
still match approved contract. If host gates incomplete, stop at staged explicit
VM route; do NOT commit final default switch or claim scope finished.

**Files affected:** CREATE proposed `lab-docker` from confirmed main `lab`;
MODIFY confirmed `lab`, `.github/workflows/ci.yml`, `.gitignore`, README.md,
SECURITY.md, `docs/DECISIONS.md`, `docs/vm-architecture.md`, `docs/testing.md`,
`scripts/check-docs-consistency.py`, `verify-lab.sh`, `tests/lab.bats`,
`tests/exit-confirm.bats`, `scripts/README.md`; CREATE proposed setup/auth/acceptance
docs as needed from earlier tasks. Keep root DECISIONS pointer, licensing and MCP
package unchanged except justified integration. No copy of original ticket JSON.

**Implementation instructions:**
1. Preserve existing Docker helper sourcing/commands in `lab-docker`; test actual
   argument/mode/metrics security parity. Implement explicit `lab vm` dispatcher
   while gated; after acceptance make `lab <workspace>` VM default with Docker
   fallback clearly named. No in-place recreation of active host-Docker container.
   Carry #50's necessary Docker parser/mode fixes: flags before/after workspace,
   exactly one OpenCode --auto, ask/conflict/invalid-mode handling, Aider no auto,
   and explicit configured denies preserved. Do not preserve main's ignored
   permission flags as if that were the intended behavior or copy blanket rules.
2. Update concise README quickstart/setup/daily suspend/resume, actual stop/down/
   rebuild semantics, one-workspace switch, self-edit + changed-VM test example,
   recursion hardware/resource requirements, authority boundaries and failures.
   Bash/Fish complete forms; relative paths and generated config status accurate.
3. SECURITY documents guest admin/rootful choice, explicit writable share, public-
   only proxy, optional endpoints/secrets, cooperative depth cap, no host breakout
   proof. Decision log records intentional changes, no session transcript paste.
4. CI adds deterministic VM suites/schema/full-severity ShellCheck/Python/Ruff,
   keeps MCP build/package/Jest/ticket/license guards and Docker/TUI regressions.
   Optional labeled KVM runner only for live tests; unknown CI host cannot claim VM
   support. Docs consistency distinguishes Docker defaults from VM schema.
5. Review diff against all path dispositions; exclude temporary diagnostics,
   benchmark/model-alias changes and unrelated future ticket features. Publish
   replacement branch/PR only after approved-scope validation; draft if safe but
   missing live gates, explicit scope limitations. No automatic merge.

**Tests to implement:** V-DISPATCH vm-default/explicit-Docker/source-helper/flag
parity, no host Docker requirement for VM, rollback no socket/DinD; V-DOCS complete
links/defaults/semantics/recursion/secret checks (R29–R33). E2E-DAILY actual final
default and rollback smoke; regression full original main suites (R01,R30).

**Tests to execute:** **S/P** every fast CI command in master §9 after inspecting
final file paths; MCP npm ci/build/check:package/test from its package directory,
MCP validate through engine; all HOST/E2E/SEC/HUMAN evidence reviewed, final clean-
workspace default/rollback smoke. `git diff --check`; inspect staged exact scope.

**Expected verification outcome:** all applicable fast checks exit0, complete
live evidence, true default VM and verified rollback; no secrets/unrelated changes.

**Acceptance criteria:** [ ] required gates pass; [ ] correct default/rollback;
[ ] docs/security truthful; [ ] CI/package/license/ticket regressions intact;
[ ] no source/audit/secret churn; [ ] self-VM test documented; [ ] independent PR.

**Commit checkpoint:** COMMIT C14 `feat(lab): enable accepted VM workflow with Docker rollback`;
TASK-16 exact docs/CI/dispatcher/tests, full final validations required. If blocked,
keep C14 pending; TASK-15A/C13a is the planned pre-acceptance route slice, not C14
completion or permission to switch the default. Any further split is recorded.

**Failure and recovery:** preserve Docker mode/auth and named original profiles;
no cutover on regression. Fix bounded documentation/dispatch failures, rerun all
affected tests. Material requirement/security/default change escalates to owner.

**Definition of done:** DD plus master completion contract and final PR URL,
implemented requirements/commits/evidence/deviations/risks/confidence reported.
