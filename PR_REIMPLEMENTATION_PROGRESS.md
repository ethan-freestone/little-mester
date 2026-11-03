# VM PR reimplementation progress

## Control state

- Phase: **Round 2 resumed; TASK-03/C02 validated, preparing checkpoint commit**.
- Authority: user requested reading and kickoff of `vm-migration-plan.md`.
- Architecture/execution approval: **granted 2026-11-03**, “Yes, please begin round 2”.
- Authoritative plan: `PR_REIMPLEMENTATION_PLAN.md`.
- Implementing branch: `feat/vm-approach-2` at
  `f8aa799b165795f4235f70b42474ea7a02a0b122` before C02 commit.
- Inspected source: PR #13 at
  `97af1d83a16fce68ddcc3390276c218a1b0cd518`.
- Assigned session: current coordinating OpenCode session; no subagents assigned.

## Investigation ledger

| Work package | Status | Evidence / result |
|---|---|---|
| Read mission and phase restrictions | DONE | Entire `vm-migration-plan.md` read; Round 1 only |
| Preserve initial repository state | DONE | Initial `git status --short --branch`: replacement branch, only `AM vm-migration-plan.md`; user changes not edited/staged |
| Identify live PR and exact revisions | DONE | Public GitHub REST discovery; PR #13 draft/open; head/base match local source/main refs |
| Read live reviews and comments | DONE at baseline | Review submissions, inline comments and issue comments each returned empty arrays |
| Establish effective diff / history | STARTED | `git diff --stat main...feat/vm-approach`: 100 files, +29219/-910; ordered commit subjects read, detailed historical diffs pending |
| Isolate source inspection | DONE | Detached worktree `/tmp/vm-migration-source`; original branch not checked out or modified |
| Read current operational gates | STARTED | MCP reads of source-store #35/#41/#45/#50/#52/#53–#57; newest #41 gate blocks host retry pending interrupted apt/report review |
| Inspect source and existing conventions | STARTED | Original architecture, launcher, state/recursion/proxy/lifecycle portions, main launcher, both CI workflows, MCP package metadata |
| Check tooling without operational changes | DONE | PATH-only discovery and device access checks; no tool version/runtime acceptance inferred |
| Reconstruct complete requirements | PENDING | Initial capability inventory in plan, not a finished specification |
| Complete adversarial review and alternatives | PENDING | Review leads recorded, no final disposition/architecture selection |
| Define executable tasks, tests and commit plan | PENDING | No implementation task or commit is authorized |
| Validate traceability / present approval checkpoint | PENDING | Do not request approval before complete planning package |

The table above is the historical kickoff ledger. The completion ledger below
supersedes its STARTED/PENDING investigation statuses; implementation remains unstarted.

## Kickoff environment observations

Available on PATH: git, python3, node, npm, bats, shellcheck, ruff,
qemu-system-x86_64, qemu-img, cloud-init, docker. `gh` and `virtiofsd` were not
found on PATH. `/dev/kvm` exists and is readable/writable by this process.
These are bounded diagnostics, not a VM boot or host acceptance result.

The instructed preferred temporary root `/tmp/opencode` is root-owned and not
writable by this session. Creating the detached worktree there failed before
checkout. Fallback `/tmp/vm-migration-source` succeeded. No chmod, privilege
escalation, package installation, or broad cleanup was attempted.

The first source-ticket attempt used the replacement/default store and could not
find #50. Subsequent reads explicitly used the detached source ticket directory.
That distinction must be retained when resuming requirements reconstruction.

## Changes and verification

Files created during kickoff:

- `PR_REIMPLEMENTATION_PLAN.md` — preliminary authoritative plan and evidence map.
- `PR_REIMPLEMENTATION_PROGRESS.md` — this ledger.

Application files changed: **none**. Ticket writes: **none**. Implementation
commits/pushes/PRs: **none**. VM/service operations: **none**.

Tests executed at kickoff: **none**. Completion-session diagnostic tests and
actual outcomes are recorded below; no replacement implementation exists yet.

## Next resumption action

Read the completed plan/review/task package and wait for explicit Round 2 approval.
Do not resume the superseded kickoff investigation queue as if no plan existed.
Before implementation, verify Git/API revisions and preserve existing user changes.
If `/tmp/vm-migration-source` no longer exists, recreate a detached inspection
worktree at the recorded source SHA in a writable external location and update
paths in this ledger. Do not switch/reset the replacement or original branch.

The latest #41 restriction remains a **host-execution blocker**, not a planning
blocker. Do not request original-host commands merely to advance this review.

## Round 2 execution ledger contract

When and only when explicitly approved, append one record per implementation
task containing: task ID; assigned agent/session; start/completion state; files
changed; exact tests and observed results; commit SHA; deviations; blockers; and
outstanding issues. Keep failed/blocked tasks visible. No task may become DONE
solely on an agent assertion or a mock standing in for required real-host evidence.

## Round 1 completion ledger — 2026-11-03

**Outcome: planning complete, awaiting explicit Round 2 approval.**

The user clarified that self-hosting means Little Mester modifying LITTLE MESTER,
including VM implementation, then testing those changes in real fresh VMs. The
completed contract makes this release-blocking (R20–R24, E2E-SELF), distinct from
ordinary same-guest additional agents. No scope has been reduced to agent nesting.

| Investigation deliverable | Final status | Evidence |
|---|---|---|
| Live source/review recheck | DONE | REST PR13: same exact base/head, open draft,155 commits/100 changed files; live main same SHA; three comment/review collections empty |
| Requirements reconstruction | DONE | Master §3:34 unique requirements, class/priority/source/explicitness/confidence/observable acceptance/verification; superseding decisions accounted for |
| Trust/security model | DONE | Master §§5–6: host/guest/child/workspace/network/credential/resource/control boundaries and negative evidence; guest-admin/depth limitations explicit |
| Source/history adversarial review | DONE | Review findings F01–F12, focused historical diffs, original operational gate, all100 changed-path dispositions |
| Alternatives and recommendation | DONE | Master §4: alternative comparison; decisions D01–D10 and concrete CLI/config/state/lifecycle/data flow |
| Self-modification/VM proof design | DONE | Changed candidate launcher/bootstrap/proxy/harness; fresh G1/G2, max2/G3 refusal, failureA→fixedB, parent continuity and exact cleanup |
| Executable implementation tasks | DONE | TASK-01–16; every mandatory task field, ordered dependencies, file scope, test cases/results/commands, recovery and commit checkpoint |
| Commit sequence | DONE | C01–C14 mapped to exact tasks/scopes/validation; two cohesive CONTINUE groups |
| Verification strategy/equivalence | DONE | Master §§8–9; requirements→decision→task→test→commit; unit/integration/E2E/security/reliability/CI/human and resource/deadline/cleanup levels |
| Risks, approval changes and confidence | DONE | Master §§7,10–11; explicit rootful guest/state-layout/model-start/reset/version/self-VM choices; confidence8/10 with limitations |
| Adversarial self-review | DONE | Review §6, including self-test cannot substitute old code, unsafe host reexecution, depth policy not root-proof, gate honesty and ownership |
| Mechanical package checks | DONE |100/100 paths,16 ordered tasks/all13 mandatory fields,34 unique requirement IDs,14 commit IDs; no missing/extra disposition or dependency cycle |
| Executive summary/approval checkpoint | READY | Present package and exact approval question; stop before implementation |

### Actual diagnostic tests executed on original detached source

Working directory `/tmp/vm-migration-source` at original PR head, NOT replacement:

| Exact command | Observed result |
|---|---|
| `python3 -B -m unittest discover -s tests -p test_vm_proxy.py -v` |28 tests, OK, exit0;7.188s |
| `python3 -B -m unittest discover -s tests -p test_vm_memory_budget.py -v` |7 tests, OK, exit0;0.015s |
| `python3 -B -m unittest discover -s tests -p test_vm_opencode_config.py -v` |4 tests, OK, exit0;0.059s |
| `bash scripts/check-vm-cloud-init-schema.sh` |Valid schema, exit0; datasource/pickle permission warnings while cloud-init discovered local context. No cloud-init provisioning run |

Total39 selected unit tests passed. Local sockets/temp fixtures/schema generation
are bounded diagnostics, not owner-host/KVM boot/OAuth/TUI/model/Compose acceptance.
Other source tests were inventoried by AST/names and selected implementation
inspection, not claimed executed. No replacement test suite exists yet.

### Package validation and actual files

Inline read-only Python validation compared the review inventory with
`git diff --name-only main...feat/vm-approach`:100 source paths/100 dispositions,
no missing/extra paths. TASK-01–16 each have all13 required fields and only earlier
prerequisites. Master has34 unique requirement IDs and C01–C14. Text whitespace
and source citations/revision checks performed; final verification recorded at
handoff. These checks validate plan structure, not implementation correctness.

Planning files now:

- `PR_REIMPLEMENTATION_PLAN.md` — completed authoritative contract.
- `PR_REIMPLEMENTATION_TASKS.md` — complete executable task specifications.
- `PR_REIMPLEMENTATION_REVIEW.md` — evidence/findings/inventory/self-review.
- `PR_REIMPLEMENTATION_PROGRESS.md` — this ledger.
- `PR_REIMPLEMENTATION_BASELINE.md` — renamed historical kickoff, clearly superseded.

Application code changed: NONE. Ticket writes: NONE. Original branch commits or
tracked worktree changes: NONE. Implementation commits/pushes/new PR: NONE.
VM/model/Docker-service/credential/package operations: NONE. The schema validator
attempted local datasource pickle access and was denied, as warned above; this
was not a guest bootstrap or a passing host-provisioning result.

### Plan validation corrections made before approval

- Made host prerequisite setup explicit and default-no, separate from read-only
  doctor/warm launch; do not lose the original prompted distro-map requirement.
- Applied descendant guard to workspace switch/resize as well as suspend.
- Clarified frozen trusted host controller vs host reexecution of agent-edited
  source; snapshotting alone does not protect a host user who runs that source.
- Kept rootful guest and no implicit model-service recovery as approval choices,
  not silent simplifications. Model-none initial setup vs configured local default
  is explicitly identified for approval.
- Added shared setup/update payload hash, current-tree snapshot reuse validation,
  concurrent allocation reservations and bounded draining serial logs.
- Clarified native OAuth per-attempt lifetime/compatibility blocker rather than
  claiming TUI-exit-only cleanup satisfies login completion.
- Required changed actual VM launch parameter plus guest bootstrap sentinel and
  failure42→fresh fixed candidate, not just copied source hashes or agent nesting.

## Round 2 task execution table

The owner resumed Round 2 after the model swap. TASK-01/02 are complete at C01;
TASK-03 has completed fixture validation and is awaiting its C02 commit. Later
tasks remain unstarted and retain their ordered preconditions. No live VM acceptance
has occurred.

| Task | Planned checkpoint | Status | Assigned/start/completion | Actual files/tests/results/SHA | Deviations/blockers |
|---|---|---|---|---|---|
| TASK-01 | C01 | DONE | Coordinator/2026-11-03/2026-11-03 | CLI/config/docs;50 Bats total pass; f8aa799 | None; no VM operations |
| TASK-02 | C01 | DONE | Delegated+coordinator/2026-11-03/2026-11-03 | Paths/tests;25 Python pass; f8aa799 | None; no VM operations |
| TASK-03 | C02 | VALIDATED | Coordinator/2026-11-03/2026-11-03 | disk/cache/overlay/seed;65 Bats,30 Python;pending C02 commit | Fixture-only; no real download/QEMU |
| TASK-04 | C03 | NOT STARTED | Unassigned/—/— | — | Depends on TASK-02; network policy tests next |
| TASK-05 | C04 | NOT STARTED | Unassigned/—/— | — | Depends on TASK-02; process/SSH implementation |
| TASK-06 | C05 | NOT STARTED | Unassigned/—/— | — | Guest setup implementation; no host operations |
| TASK-07 | C06 | NOT STARTED | Unassigned/—/— | — | Depends on TASK-05/06 |
| TASK-08 | C07 | NOT STARTED | Unassigned/—/— | — | Guest harness/provider parity |
| TASK-09 | C08 | NOT STARTED | Unassigned/—/— | — | Depends on guest setup/runtime |
| TASK-10 | C09 | NOT STARTED | Unassigned/—/— | — | Version qualification |
| TASK-11 | C10 | NOT STARTED | Unassigned/—/— | — | Control key and secure path |
| TASK-12 | C10 | NOT STARTED | Unassigned/—/— | — | Depends on TASK-11 |
| TASK-13 | C11 | NOT STARTED | Unassigned/—/— | — | Real nested hardware gate remains |
| TASK-14 | C12 | NOT STARTED | Unassigned/—/— | — | Depends on prior implementation |
| TASK-15 | C13 | NOT STARTED | Unassigned/—/— | — | Exact fresh-host handoff still required |
| TASK-16 | C14 | NOT STARTED | Unassigned/—/— | — | All live gates before cutover |

**Handoff stop condition:** ask the mission's explicit architecture/execution
approval question. Do not start TASK-01, commit, push, run owner-host tests or
open the replacement PR merely because planning is complete.

The preceding stop condition was satisfied by the user's explicit approval.
It remains a historical Round 1 instruction, not a new approval barrier.

Final read-only validation: all31 P0/P1 required/security/operational/quality
requirements mapped through the traceability table;100/100 changed paths covered;
all16 tasks have13 mandatory fields and only earlier dependencies;14 checkpoints;
all short historical commit citations resolve; all planning text has no trailing
whitespace/unbalanced fences/conflict markers. `git diff --check` exited0.
Final Git refs unchanged; original detached worktree tracked/untracked status
clean; replacement contains only the five untracked planning files plus the
unchanged pre-existing `AM vm-migration-plan.md`. No staging/commit performed.

## Round 2 preflight / C01 start — 2026-11-03

- Owner approved all §7 choices and authorized implementation.
- Live PR13/main API revisions match approved baseline. Replacement branch still
  `feat/vm-approach-2` at efe7720; original detached source preserved.
- Existing user mission worktree SHA256:
  `3d6ee2115423272b88a8efd8d4d33462318d5ae4c537841091e513ee5e2d1374`.
  Staged mission file is empty (SHA256 e3b0c442…b855); leave both untouched.
- Tool versions: Bash5.2.21, Python3.12.3, Bats1.10.0, Ruff0.17.3, Node22.23.4;
  ShellCheck available. No dependency installation required for C01.
- Baseline `bats tests/`:26/26 passed, exit0, before code changes.
- Main already contains `docs/vm-architecture.md`; TASK-01 documentation action
  is MODIFY, not CREATE. Its old design is superseded by the approved native guest
  contract. This is a file-existence correction, not architecture deviation.
- TASK-01 step5 clarified to prohibit *implicit* installation: step3 explicitly
  authorizes opt-in setup. No host installation will be performed during C01 tests.
- Coordinator owns CLI/config/docs and integration. Bounded delegated TASK-02
  session `ses_e5f93e600ffeozrl1MnGR23DCj` owns only paths helper/tests; no commits.
- C01 work is all local fixtures/state policy; no VM, Docker/model service,
  credential, owner-host or package operations. Execution start recorded on #41
  through MCP; existing historical criteria remain pending.

## C01 validation and model-swap handoff — 2026-11-03

**Historical handoff:** the owner requested a stop for model swap after C01.
That pause ended with the user's subsequent “Continue please”; TASK-03 is now
validated below. No background agent remains running.

Implemented:

- `lab-vm`: executable replacement entry point with one parser, honest command
  availability, read-only context and explicit prerequisite setup. Unsupported
  VM operations refuse rc2, never start another VM or claim success.
- `lib/state.sh`, `lib/vm/config.sh`: per-user root selection, non-executable JSON
  config, validated flags/environment and precedence, no model-service mutation,
  separate `--yes` vs agent auto permissions. Host installation requires explicit
  setup opt-in and actual confirmation or CLI `--yes`; ambient yes is insufficient.
- `lib/vm/paths.py`: strict config/profile schemas, exact canonical workspace/
  private-state guards, modes/ownership/no legacy discovery, atomic fsync/rename,
  ordered root/profile locks, sticky descendant metadata, bounded frozen control
  payload capture without overwriting an existing payload.
- `tests/vm-cli.bats`, `tests/test_vm_paths.py`; current contract documented at
  top of `docs/vm-architecture.md`, old proposal explicitly historical/superseded.
- Approved planning package persisted; ticket execution evidence written via MCP
  only on the replacement store (#41), no original-branch ticket changes.

Actual validation:

| Command | Result |
|---|---|
| Baseline `bats tests/` before code |26/26 PASS |
| Initial new CLI tests before implementation |21 tests FAILED as expected (missing implementation) |
| Delegated path-helper test-first runs |Initial missing implementation red; validate-config and six later policy cases red before fixes |
| Coordinator regression red runs |Symlink loop, namespace names, nested root creation, non-directory ancestor, provider transition, inherited setup approval and unavailable-package refusal demonstrated failures before fixes |
| Final `bats tests/` |50/50 PASS (26 original +24 CLI cases), exit0 |
| Final `python3 -B -m unittest discover -s tests -p 'test_vm_*.py' -v` |25/25 PASS, exit0 |
| `shellcheck lab-vm lib/state.sh lib/vm/config.sh` |PASS, full severity; only targeted public-result SC2034 annotations |
| `ruff check lib/vm/paths.py tests/test_vm_paths.py` |PASS |
| `bash -n lab-vm lib/state.sh lib/vm/config.sh` |PASS |
| Python AST parsing of helper/tests |PASS |
| `git diff --check` scoped to changed tracked docs/ticket files |PASS |
| MCP ticket validate |PASS: synced, audit intact, no lock/orphans/quality issues |
| `python3 scripts/check-docs-consistency.py` |FAIL:8 pre-existing README/DECISIONS drift/link assertions; not caused by this checkpoint |
| Docs check with `read()` sourcing exact main Git-object text |Same8 failures; baseline proved, no new failure. TASK-16 owns correction; no unrelated doc repairs folded into C01 |

Review/integration corrections:

- Host package install cannot inherit `LAB_YES` approval; explicit CLI yes only.
- Package availability is queried read-only before any privileged command; no
  automatic repository refresh/addition. Ubuntu noble/Arch package pages checked;
  Debian page challenged automated access, so runtime local metadata verification
  remains authoritative. No actual package action was executed.
- Explicit provider change clears incompatible old local port/model fields before
  applying new selectors, avoiding stale inherited local configuration.
- Clarified plan's “setup-only memory”: fresh-profile `run/up`, not host-prereq setup.
- Dotted/exact namespace-like profile names, memory1–128, sticky descendants and
  partially provisioned null metadata tested. Runtime copying refuses source or
  installed-payload drift; update lifecycle is deferred as planned.
- Sensitive guard includes all ~/.local and shell credential locations, generic
  rc2 on invalid input/symlink loops; context doesn't allocate state.
- Tests use genuinely private temporary ancestry instead of weakening production
  ownership guards to accommodate group-writable Bats fixture directories.

Resume procedure for the next model:

1. Read plan, TASK-03 and this handoff; verify final C01 SHA/status below.
2. Preserve the pre-existing staged-empty/unstaged-populated mission file; never
   stage or commit `vm-migration-plan.md`. Preserve original branch/source worktree.
3. Do not request new architecture approval; Round 2 is approved. The pause is for
   model swap only. Continue TASK-03 after the owner resumes work.
4. Implement image-cache primitives/test-first as C02, then continue task order.
   Do not run original-host lifecycle/auth/test handoffs or start VM tests now.
5. C01 has no real VM acceptance; full changed-VM self-test remains TASK-13/15.

Known limits: process ownership/signalling, image downloads/boot, native guest
installation, actual agent permission behavior, OAuth and lifecycle are later
tasks. Profile metadata stores runtime identity but does not authorize signalling.
An fsync failure after rename can report failure after the atomic replacement
has committed; no false rollback guarantee is made.

**C01 committed:** `f8aa799b165795f4235f70b42474ea7a02a0b122`
(`feat(vm): define validated profiles and workspace boundaries`). Exact15 planned
files staged/committed using `git commit --only`; user's staged-empty mission file
was deliberately excluded. This post-commit SHA/DONE update is an uncommitted
ledger-only change to include with the next checkpoint. No push or PR created.
At the time, the next task was TASK-03/C02; execution resumed at the owner's
request. C01 has no real VM acceptance; full changed-VM self-test remains
TASK-13/15.

## TASK-03 / C02 validation — 2026-11-03

Implemented the checksum-keyed immutable base cache and selected-profile disk/seed
primitives. Changes:

- Created `lib/vm/disk.sh`: fixed initial URL/SHA pin from the detached PR13 disk
  module; size-bounded HTTPS curl, mandatory hash check before qemu-img use,
  standalone qcow2/boot-sector validation, bounded free-space preflight, private
  staging and atomic no-replace cache publication. Existing corrupt/partial cache
  is diagnosed and never removed/replaced, including with ambient yes. New source
  digest gets a separate cache key. Nested mode forces the parent proxy and an
  empty `--noproxy` exclusion; there is no direct fallback.
- Added private per-hash cache locking and safe `image-run`/`image-publish`,
  profile-locked execution and no-replace overlay publication, and atomic bounded
  seed writes to `lib/vm/paths.py`. Profile overlays bind to exact image hash/path,
  preserve the shared base and other profiles, and existing chains must match.
- Updated `lab-vm`/`lib/vm/config.sh` to expose explicit `base` preparation only;
  unsupported lifecycle commands continue to refuse. Updated CLI/architecture
  docs. Added `tests/vm-disk.bats` and helper cases in `tests/test_vm_paths.py`.
- The source pin was inspected/reused offline from
  `/tmp/vm-migration-source/lib/vm/disk.sh` at PR13 head. Remote URL availability
  and bytes are **not** claimed verified; the explicit download path verifies the
  configured SHA before any qemu-img operation. No `base` command was invoked.

Test-first evidence and exact validation:

| Check | Result |
|---|---|
| Initial `bats tests/vm-disk.bats` before disk module |10/10 expected FAIL: module absent |
| C01 baseline (`git show HEAD:lib/vm/paths.py`) with five targeted C02 helper tests |Expected red: 4 failures +1 error (image-run, image-publish, overlay-publish, seed-write absent) |
| Final `bats tests/` |65/65 PASS; disk suite includes all fixtures; no real network/QEMU |
| Final `python3 -B -m unittest discover -s tests -p 'test_vm_*.py' -v` |30/30 PASS |
| `shellcheck -x lab-vm lib/state.sh lib/vm/config.sh lib/vm/disk.sh tests/vm-cli.bats tests/vm-disk.bats` |PASS, full severity |
| `bash -n lab-vm lib/state.sh lib/vm/config.sh lib/vm/disk.sh` |PASS |
| `ruff check lib/vm/paths.py tests/test_vm_paths.py` and AST parse |PASS |
| `python3 scripts/check-docs-consistency.py` |8 existing README/DECISIONS drift/link failures |
| Docs checker reading exact main Git-object versions |Same8 failures, no new failure from C02 |
| MCP `mester_tickets_validate` after recording C02 evidence on replacement #41 |PASS: synced, audit intact (130 entries), no lock/orphans/quality issues; criteria remain pending |

Covered failures preserve prior cache/profile bytes and private fixture state. No
real curl download, actual host `qemu-img`, QEMU launch, VM, package, service or
credential action occurred. These fixtures are code-level evidence only, not
V-IMAGE host acceptance, guest bootability or nested-self-test evidence.

No architecture deviations. Implementation relies on Linux `renameat2(RENAME_NOREPLACE)`
for atomic no-clobber publication; absence of that primitive fails closed. Seed
content rejects common secret-bearing field/value keywords but remains dependent
on TASK-06's trusted generator and later secret-canary scan for broader detection.

**C02 commit and SHA are recorded immediately after the commit below.**
