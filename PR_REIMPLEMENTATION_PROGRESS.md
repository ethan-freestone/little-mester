# VM PR reimplementation progress

## Control state

- Phase: **CI/current implementation checkpoint committed at `ca6da6c`; owner-confirmed boot/version and partial TUI usability; inference/security/performance amendment approved; detailed planning followed by mandatory pause for owner model selection. C14/merge not started.**
- Authority: user requested reading and kickoff of `vm-migration-plan.md`.
- Architecture/execution approval: **granted 2026-11-03**, “Yes, please begin round 2”.
- Current authorization: the owner approved the fixed four-package sequence in
  [the local-first C14 handoff plan](docs/vm-c14-handoff-plan.md): (1) experimental
  explicit development entry and first owner-started VM on this branch, (2) the
  remaining bounded security/evidence implementation, (3) integrated acceptance
  on a frozen candidate, and (4) C14 default/rollback and closeout. The owner starts
  the VM; the assistant must not. The useful-evidence campaign is approved within
  a 360-minute ceiling including cleanup and one diagnosed retry; do not request
  campaign approval again. Pause for genuine owner observations or new effects.
  Manual owner OpenAI authentication experimentation is allowed in the disposable
  VM; no paid streams or credential export/import are included. No push, merge or
  C14 activation is authorized.
- Latest2026-11-06 authority supersedes older next commands: checkpoint existing
  work and reported CI fix, document all approved inference/performance changes,
  commit planning and **pause**. No INF implementation, live discovery/VM/service/
  provider operation, push, merge or default cutover during this phase.
- Controlling new task source:
  [inference/performance execution plan](docs/vm-inference-performance-plan.md),
  INF-00–12 plus conditional VM-local F1–F8. Owner always chooses the model;
  higher-thinking review labels are recommendations, never automatic switches.
- Authoritative plan: `PR_REIMPLEMENTATION_PLAN.md`.
- Implementing branch: `feat/vm-approach-2`; C05 implementation checkpoint is
  `cb7091a17720b59056bd2677f3af1aba5699f944`.
- C06 implementation commit: `de5150ed80789583db7dc483422ac918df4d60ec`, on
  `feat/vm-approach-2`, based at `5ecd200d32ba909e41d4a15885f57681a32f60d6`;
  its SHA ledger closeout is recorded separately.
- C10 implementation commit: `272140ec0fb7798f96d759623bcf6699e13f16a6`, on
   `feat/vm-approach-2`, based on C09 `13a2292`; no push was performed.
- C11 fixture closeout: `668147b`; C12: `65a798d080f9470dfc43ccd907f448c9022fc77f`.
- C13 implementation commit: `1e0b0bacfd99234b12b69cc4a53fa7f1e4eff53c` on
  `feat/vm-approach-2`; no push. This ledger-only SHA closeout follows separately.
- C13b readiness implementation commit:
  `d3751e4fbda25349de5cbcb1f1e6400ae3e489c8` on `feat/vm-approach-2`; local only,
  no push. This progress-ledger SHA closeout follows separately.
- Historical owner direction (2026-11-05, planning reconciliation): read original
  ticket history through MCP, reconcile this branch's plan/tasks/progress, commit
  locally and stop; no C13 or host action in that session. This was superseded by
  the later request to finish C13 locally only.
- Current scope: preserve the already-ready run71ff52d4 and parent; do not issue
  a new start/recovery command. Shared inference capability/isolation, actual
  recursive relay and >=95% one-level benchmark extend package2/3 via approved
  R35–R38/D11–D13. Ordinary lab stays Docker. Old preflight/partial owner boot
  observations are not final-candidate acceptance. VM-local artifact push is a
  conditional alternative; no automatic model copies or GPU passthrough scope.
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

The table above is the historical kickoff ledger. The completed Round 2
investigation and execution records below supersede its STARTED/PENDING statuses;
those rows describe the initial kickoff state only.

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

At kickoff, application files changed: **none**. Ticket writes: **none**.
Implementation commits/pushes/PRs: **none**. VM/service operations: **none**.

Tests executed at kickoff: **none**. Later implementation and validation results
are recorded in the completion ledger and C13 entry below.

## Historical next resumption action (superseded)

The next local C13 implementation action was completed in the resumed session.
The next operational milestone is an owner-reviewed, exact-scope campaign
handoff; it remains unapproved and unrun here. Preserve `vm-migration-plan.md`
and the working Docker session. Do not run old-branch commands or reuse its
stable snapshot/profile discovery.

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

The owner resumed Round 2 after the model swap. TASK-01/02 through TASK-05 are
committed at C01–C04. TASK-06/C05 is fixture-validated and committed. Later tasks
retain their ordered preconditions. Rows below record each checkpoint's state at
that time; later approvals and amendments supersede stale routing restrictions.
No live VM/network acceptance has occurred.

| Task | Planned checkpoint | Status | Assigned/start/completion | Actual files/tests/results/SHA | Deviations/blockers |
|---|---|---|---|---|---|
| TASK-01 | C01 | DONE | Coordinator/2026-11-03/2026-11-03 | CLI/config/docs;50 Bats total pass; f8aa799 | None; no VM operations |
| TASK-02 | C01 | DONE | Delegated+coordinator/2026-11-03/2026-11-03 | Paths/tests;25 Python pass; f8aa799 | None; no VM operations |
| TASK-03 | C02 | DONE | Coordinator/2026-11-03/2026-11-03 | disk/cache/overlay/seed;65 Bats,30 Python;68861dc | Fixture-only; no real download/QEMU |
| TASK-04 | C03 | DONE | Coordinator/2026-11-03/2026-11-03 | proxy policy/chaining;36 proxy Python,66 Bats,66 VM Python;8dc9816 | Loopback/mock fixtures only; no live SEC-NET |
| TASK-05 | C04 | DONE | Coordinator/2026-11-03/2026-11-03 | runner/SSH/process supervision;77 Bats,76 VM Python;b530193 | Fake executable/temp/loopback fixtures only; no real KVM/QEMU/guest; lifecycle CLI wiring remains later work |
| TASK-06 | C05 | DONE | Coordinator/2026-11-03/2026-11-03 | Guest bootstrap/config/hash/readiness;98 Python,77 Bats; cloud-init schema; static checks PASS; cb7091a | Fixture-only; no host install, real VM, image/network, SEC-NET or nested acceptance |
| TASK-07 | C06 | DONE (FIXTURE MILESTONE) | Coordinator/2026-11-03/2026-11-03 | Internal fresh-up/resume/A→B→B, exact-target resize, reservation recovery, exact deletion/report archive, and rebuild failure guard; 106 Bats,136 VM Python; `de5150e4` | Fixture-only; lifecycle commands beyond status/list/suspend remain undispatched; #41 real-host/KVM/network/nested acceptance stays pending |
| TASK-08 | C07 | DONE (FIXTURE MILESTONE) | Coordinator/2026-11-03/2026-11-03 | Native OpenCode V2/Aider adapters, explicit provider config and permission handling, ready-profile-bound diagnostics, constrained cwd; 148 VM Python, 116 Bats, Bash syntax, ShellCheck, and diff checks PASS | Fixture-only; no live Aider/provider/UI or human-permission acceptance, VM, image, or network operation; live gates remain separately approved |
| TASK-09 | C08 | DONE (FIXTURE MILESTONE) | Coordinator/2026-11-04/2026-11-04 | Verified loopback metrics service and selected QEMU guestfwd; collector concurrency/history, optional guest plugin retry/disabled state, child metrics isolation, and distinct exit-confirm callbacks; 160 VM Python tests, 122 Bats tests, Bash syntax, ShellCheck, Python compilation, JSX, and diff checks PASS | Fixture-only; no live VM, GPU, Docker, or TUI renderer actions; HUMAN-TUI and host/VM acceptance remain separate approved gates |
| TASK-10 | C09 | DONE (FIXTURE MILESTONE) | Coordinator/2026-11-04/2026-11-04 | Scoped native auth/browser bridge, private escrow, runtime key adapters, lifecycle export guard; 36 auth/browser Python tests, 196 full Python tests, 123 Bats; Ruff, ShellCheck, Bash syntax, Python compilation, diff checks PASS | Local fixture checkpoint only; no live login, auth-store inspection, credential export/import, guest, or host browser acceptance; TASK-15 HUMAN-AUTH and refreshed-token owner proof remain pending |
| TASK-11 | C10 | DONE (FIXTURE MILESTONE) | Coordinator/2026-11-04/2026-11-04 | Filtered Git-HEAD-bound snapshots; exclusions, race/capacity checks, synthetic Git and retained-snapshot verification; snapshot Python fixtures pass; `272140e` | Fixture-only; no live VM or host acceptance |
| TASK-12 | C10 | DONE (FIXTURE MILESTONE) | Coordinator/2026-11-04/2026-11-04 | Persisted identity/depth guards, KVM/CPU preflight, isolated candidate state/argv, immediate-parent proxy depth and exact descendant-safe cleanup; 5 recursion Python cases, 6 recursion Bats cases; full 214 VM Python and 129 Bats pass; `272140e` | Public `run`/`up` remain undispatched until TASK-16; real nested KVM/VM and host/human acceptance remain pending |
| TASK-13 | C11+C13 | PARTIAL (LOCAL CODE + FIXTURE MILESTONE) | Coordinator/2026-11-04/2026-11-05 | C11 evidence-gated state machine plus C13 opt-in nested-VM adapter, candidate lifecycle/bootstrap mutations, exact G1/G2/G3 evidence and cleanup; local tests recorded in C13 closeout below | No live VM/agent/nested-KVM/host/network acceptance; E2E-SELF remains pending owner campaign |
| TASK-14 | C12 | DONE (FIXTURE MILESTONE) | Coordinator/2026-11-05/2026-11-05 | Guest update scan/apply, exact-version tools installer, `/proc` TUI guard, CLI/cloud-init/profile integration; 249 Python and 131 Bats tests PASS; changed-file Ruff/ShellCheck, Bash syntax, Python compilation, cloud-init schema and diff checks PASS | Fixture-only; no live VM, package/network, credential, host or lifecycle acceptance; TASK-15 remains the live gate |
| TASK-15 | C13 | CODE COMMITTED; READINESS INCOMPLETE; LIVE ACCEPTANCE PENDING | Coordinator/2026-11-05/2026-11-05 | C13 runner/probes/adapter/reporting/fixtures; cgroup fix `232180b`; owner read-only preflight PASS on `336cf69` | Reporting/checklists alone are not complete procedures; C13a/C13b readiness work and real host evidence required |
| TASK-15A | C13a | COMMITTED LOCALLY; CHECKS PASS | Coordinator/2026-11-05/2026-11-05 | `599e9ef` `lab` dispatcher, preserved `lab-docker`, explicit `lab vm`; local route/permission fixtures | Transition dispatcher stays Docker until acceptance; released default remains VM, with Docker optional; host rollback smoke pending |
| TASK-15B | C13b | COMMITTED; LOCAL-FIRST §1 IMPLEMENTED/VALIDATED LOCALLY | Coordinator/2026-11-05/2026-11-05 | `d3751e4`, ledger `777f5a9`; see appended local-first implementation checkpoint | 323 Python/141 Bats pass; QCOW2 filesystem inspection, filesystem isolation and synthetic private-state campaign evidence remain blockers; no live evidence |
| TASK-16 | C14 | CUTOVER NOT STARTED | Unassigned/—/— | Non-default preparation delegated to TASK-15A/C13a | All required live gates reviewed before default cutover; final default/rollback smoke after activation |

**Round 1 stop condition (satisfied):** explicit Round 2 approval was granted on
2026-11-03. Continue only in task order and preserve each named checkpoint. Do not
push or create a replacement PR absent explicit authorization; live host/network
gates remain separately restricted by #41.

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

At C01 handoff, process ownership/signalling, image-cache code, boot, native guest
installation, agent permission behavior, OAuth and lifecycle were not implemented.
TASK-03 now provides fixture-tested image-cache code, but real image download/boot
remains NOT_RUN. Profile metadata stores runtime identity but does not authorize signalling.
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

**C02 committed:** `68861dcccfee3d3d0f7a21383ad46fe02bd0cc21`
(`feat(vm): add immutable base cache and profile disks`). Exact12 planned paths
committed with `git commit --only`; the user's staged-empty mission file was
deliberately excluded. This post-commit SHA/status ledger update is uncommitted
and is included with C03 below; no push or PR created.

## TASK-04 / C03 validation — 2026-11-03

Implemented the reviewed public-only HTTP(S) proxy and explicit immediate-parent
chaining contract. `lib/vm/proxy.py` retains the original normalization, host-route,
all-answer DNS, validated-IP connect, domain-policy and protocol-forwarding core,
with stricter parsing, bounded handling and private readiness records. `lib/vm/config.sh`
now emits the fixed `--upstream-proxy http://10.0.2.100:3128` argv only for a
validated inherited recursion identity; the proxy independently rejects every
other configured parent endpoint. No parent path/profile-name inference is used.

Key safeguards in the replacement:

- Only HTTP port80 and CONNECT443; permission/domain policy runs before DNS or
  parent contact. Direct mode rejects all non-public/mixed/reserved/multicast
  answers and host-local/directly connected host networks, and connects only to
  a previously validated IP. IPv4-mapped, 6to4, Teredo and well-known NAT64
  IPv4 embedding remain covered.
- Nested mode uses only the explicit private numeric parent guestfwd endpoint,
  does not resolve/connect to the origin and has no direct fallback. Parent status
  denials are preserved; parent outage is 502 and `connect_public` is not called.
- Strict HTTP authority/Host checks; duplicate/conflicting Content-Length,
  Transfer-Encoding, Expect, malformed connection tokens, oversized request line
  or headers are rejected before upstream contact. Proxy credentials and
  hop-by-hop/nominated headers are removed. Request logging is disabled; URLs,
  query values and bodies are not emitted.
- Concurrent handler slots, request/header bytes and idle/connect time are bounded.
  Timeout/slot release is tested. Policy candidates are strict-parsed before the
  lock-protected snapshot swap; malformed/unreadable/missing reload keeps last-good.
- Loopback bind only. Port and JSON readiness identity are private atomic
  no-replace files; marker contains PID/start ticks/executable/port and is published
  last. Startup refuses stale records without overwriting/deleting them; shutdown
  removes only exact matching records. Exact identity signaling remains TASK-05.

Test-first evidence and exact validation:

| Check | Result |
|---|---|
| New suite against original proxy in disposable temp tree |Expected red: ambiguous framing, header bound, idle bound, malformed-policy reload, private parent endpoint and readiness mode/identity failed; slot 503/recovery already passed |
| Proxy test suite |36/36 PASS (28 original scenarios retained in meaning plus 8 hardening/fixture cases); all bound sockets are loopback; mixed DNS/route safety decisions are synthetic/mocked |
| Idle-timeout release |3 consecutive targeted repetitions PASS |
| `bats tests/` |66/66 PASS |
| `python3 -B -m unittest discover -s tests -p 'test_vm_*.py' -v` |66/66 PASS |
| `shellcheck -x lib/vm/config.sh tests/vm-cli.bats`; `bash -n lib/vm/config.sh` |PASS, full severity |
| `ruff check lib/vm/proxy.py tests/test_vm_proxy.py`; AST parse |PASS |
| `python3 lib/vm/proxy.py --help` |PASS; no server started |
| `git diff --check` on C03 paths |PASS |
| Docs consistency checker |8 failures; exact `main` Git-object baseline confirms same8 pre-existing README/DECISIONS failures |

No external DNS lookup, public-origin connection, host route mutation, network
configuration change, QEMU launch, VM, package/service action or credentials were
used. C03 is code/fixture evidence only—not live SEC-NET, parent guestfwd wiring,
host egress or nested-VM acceptance. Current workspace `.mester-egress` has a
restricted allowlist unlike the detached PR fixture, so the original empty-list
public-default scenario is retained as a separate explicit fixture test.

No architecture deviation. The fixed upstream is the documented guestfwd parent
address `10.0.2.100:3128`; integration into process/QEMU startup is TASK-05, and
live parent/child denial verification remains a later authorized SEC-NET gate.
Ticket #41 now records the C03 fixture/test evidence and the remaining TASK-05,
live SEC-NET and fresh-nested-VM gates; ticket-store validation passed with no
orphan, audit, index-sync or quality issues. No acceptance criterion was marked
passing from fixture-only evidence.

## TASK-05 / C04 validation — 2026-11-03

Implemented the low-level owned-process startup and SSH primitives in `lib/vm/runner.sh`,
`lib/vm/ssh.sh` and `lib/vm/process.py`. `runner.sh` builds a KVM-only argv array
with two host-CPU vCPUs, shareable memfd RAM, virtiofs, per-profile seed/overlay,
loopback SSH, `restrict=on`, and only the selected proxy/model/metrics channels.
Startup orders proxy → virtiofsd → QEMU. The bounded three-attempt host-forward
collision retry starts only a new QEMU process and never signals the port owner.
KVM/device/tool and input checks precede profile-run allocation; no TCG fallback
exists.

Process records are private and atomically replaced. Read-only status does not
create the registry lock (a missing lock fails closed as unknown). Identity
comparisons include PID, `/proc` start ticks, executable and complete argv; pidfd
signaling is used only after revalidation. Rollback signals only recorded
logger/supervisor identities and then rechecks every recorded child. An
early-exiting child records its original exit status without a signalable identity.
Bounded logs use the new
`paths.py` `BoundedLogWriter`: 10-MiB current plus one rotated segment, while
continuing to drain child output if log writes fail. SSH ignores user config,
agents and implicit forwards, verifies QEMU ownership of the loopback listener
before and after a complete Ed25519 first-use scan, never replaces a pinned key,
and applies a deadline to the complete remote command. C04 docs describe the
trust-on-first-use assumption and fixture-only boundary. `lab-vm` lifecycle
dispatch, guest readiness and profile/doctor integration remain later tasks; the
new startup primitives are not yet the user-facing lifecycle.

Actual validation:

| Command/check | Result |
|---|---|
| `bats tests/` |77/77 PASS, including11 C04 fake-process cases |
| `python3 -B -m unittest discover -s tests -p 'test_vm_*.py' -v` |76/76 PASS (paths32, process8, proxy36) |
| `shellcheck -x lib/vm/runner.sh lib/vm/ssh.sh tests/vm-process.bats` |PASS, full severity |
| `bash -n lib/vm/runner.sh lib/vm/ssh.sh` |PASS |
| `ruff check lib/vm/paths.py lib/vm/process.py tests/test_vm_paths.py tests/test_vm_process.py` and Python AST parse |PASS |
| `python3 lib/vm/process.py --help` |PASS; helper CLI only, no process operation |
| `git diff --check` |PASS |
| `python3 scripts/check-docs-consistency.py` |8 failures; same pre-existing README/DECISIONS assertions recorded against main Git objects; C04 adds no links |

The C04 fixtures cover strict argv/path validation, unavailable-KVM refusal before
run-state allocation, process absent/stopped/running/unknown states, PID/start-tick/
argv mismatch never signaling a live process, owned-listener detection, complete
host-key validation and mismatch preservation, bounded remote timeout, flood
rotation and injected ENOSPC drain, launch failure status127, early exit status37,
port collision without owner signaling, startup ordering and partial rollback with
foreign-process continuity. Only fake executables, synthetic subprocesses, private temporary files,
and loopback TCP/Unix sockets were used. No real KVM/QEMU/guest, host network
configuration, package/service, image download, credential, live SEC-NET or
nested-VM operation occurred. This is fixture evidence only; ticket #41 acceptance
criteria remain pending and no host/VM acceptance is claimed.

Ticket #41 now contains C04 test-file/case evidence and the remaining lifecycle and
live acceptance gaps. Ticket-store validation passed: synced index, intact 134-entry
audit, no lock/orphans, and no ticket-quality issues.
The original detached PR worktree remains outside this checkpoint; preserve it.
The pre-existing staged-empty/worktree-populated `vm-migration-plan.md` remains
excluded from all C04 commit paths.

**C04 committed:** `b530193` (`feat(vm): launch owned restricted QEMU profiles`).
Exact12 planned C04 paths were committed with `git commit --only`; the staged-empty
mission file was excluded. No push or PR was created. The next ordered task is
TASK-06/C05; real VM, host-network, image-download and fresh nested-VM gates remain
pending and restricted as previously recorded.

## TASK-06 / C05 — guest bootstrap and native payload

**State:** implementation, fixture validation and C05 code checkpoint complete.
Coordinator execution on `feat/vm-approach-2`, 2026-11-03.

Created `lib/vm/cloud-init.sh`, `lib/vm/guest.sh`,
`lib/vm/guest-bootstrap.sh`, `scripts/vm-opencode-config.py`,
`scripts/check-vm-cloud-init-schema.sh`, `scripts/vm-python-tools.sh`, and the
cloud-init, guest, and config fixture suites. Updated `docs/vm-architecture.md` and
ticket #41 evidence through the ticket MCP API. `vm-migration-plan.md` was not
edited; its worktree SHA-256 remains
`3d6ee2115423272b88a8efd8d4d33462318d5ae4c537841091e513ee5e2d1374` and its
index entry remains empty. The detached source worktree remains clean at
`97af1d83a16fce68ddcc3390276c218a1b0cd518`.

Implemented the schema-valid secret-free NoCloud template, numeric UID/GID and
public-key setup, recursion identity, explicit proxy, workspace-tag mount and
guest-side mount/ownership checks. Bootstrap is one-shot, checks the mounted
workspace payload hash before package operations, resolves `latest` once into an
exact install manifest, installs a native guest OpenCode harness, privately stages
and builds locked MCP sources, emits V2 server plus separate CLI plugin config,
and writes readiness evidence only after all gates. Docker/Compose remains an
optional rootful workload runtime; `none` does not invoke Docker. Recursion tools
and KVM group membership are conditional on a child-depth allowance. Internal
regular-file symlinks are preserved only when contained in the source subtree;
external or escaping targets fail closed.

Actual validation:

| Command/check | Result |
|---|---|
| `python3 -B -m unittest discover -s tests -p 'test_vm_*.py' -v` |98/98 PASS; all new tests use local/fake fixtures |
| `bats tests/` |77/77 PASS |
| `shellcheck -x --severity=style` on five changed Bash files |PASS |
| `ruff check` on the renderer and three changed Python test files |PASS |
| `bash -n` on five changed Bash files; `git diff --check` |PASS |
| Python `ast.parse` on four changed Python files |PASS |
| `bash scripts/check-vm-cloud-init-schema.sh` |PASS; installed `cloud-init schema` accepted generated NoCloud data (validator emitted expected non-root instance-state warnings) |
| `vm_guest_payload_hash /workspace docker opencode latest 0 2` |PASS; `5a017ba024394b5e2ab78e4b19ed4389afa5b55ac40685d72e785eae5c0c7068` |
| `python3 scripts/check-docs-consistency.py` |8 failures, unchanged pre-existing README/DECISIONS assertions; no new architecture-doc link failures |

Ticket #41 received the three C05 test files and fixture evidence through MCP;
acceptance criteria A1–A8 remain **pending**. Ticket-store validation passed:
index synchronized, audit intact (135 entries), no lock/orphans, and no quality
issues. No MCP install/build command was run on the active host. No real VM,
QEMU/KVM guest boot, image download, host-network change, live
SEC-NET, credentials, or nested-VM operation was performed. C05 is local fixture
evidence only, not host/VM acceptance.

**C05 committed:** `cb7091a17720b59056bd2677f3af1aba5699f944`
(`feat(vm): provision native guest harness and workloads`), 14 exact C05 paths.
No push or PR was created. This execution-ledger SHA closeout is a separate
documentation-only commit; the staged-empty/worktree-populated
`vm-migration-plan.md` remains excluded.

## TASK-07 / C06 — safe lifecycle and resource integration

**State:** C06 implementation committed locally on `feat/vm-approach-2` as
`de5150ed80789583db7dc483422ac918df4d60ec` (based at `5ecd200d32ba909e41d4a15885f57681a32f60d6`);
the SHA ledger closeout is a separate documentation-only commit.

The CLI still dispatches only read-only exact-profile `status`/`list` and graceful,
descendant-safe `suspend`; startup, resume, resize and destructive operations
continue to refuse. Internal fixture-tested orchestration covers fresh profile
allocation/start through a frozen runtime, same-workspace warm resume, A→B→B
workspace switching with staged seed/hash update, exact-target RAM reservation and
graceful resize, identity-based reservation recovery, exact profile deletion with
stopped run-report archival, and rebuild stopping if deletion fails. Status checks
recorded process identity even for suspended/unknown profiles. Auth-enabled deletion
and uncertain process/reservation states fail closed. No lifecycle implementation
beyond status/list/suspend is user-facing.

Current validation:

| Command/check | Result |
|---|---|
| `bats --print-output-on-failure tests/*.bats` |106/106 PASS; fake executables and temporary state only |
| `python3 -B -m unittest discover -s tests -p 'test_vm*.py' -v` |136/136 PASS |
| Ruff on changed VM Python modules/tests |PASS |
| `bash -n lab-vm lib/vm/lifecycle.sh lib/vm/config.sh lib/vm/runner.sh`; ShellCheck on production Bash modules |PASS |
| ShellCheck Bats files at error severity; `git diff --check` |PASS |
| `mester_tickets_validate` after appending C06 fixture evidence to #41 |PASS: index synced, audit intact (137 entries), no lock/orphans/quality issues; A1–A8 remain pending |

The TASK-07 implementation and fixture milestone are committed locally; the
remaining closeout is this documentation-only SHA record. No lifecycle
implementation was exposed through the CLI. No VM/image/network operation was
run; real acceptance gates remain restricted and pending under ticket #41. The
pre-existing staged-empty/worktree-populated `vm-migration-plan.md` remains
untouched and excluded from C06.

## TASK-10 / C09 — scoped native OAuth and provider credential persistence

**State:** fixture implementation and C09 verification complete; committed locally
on `feat/vm-approach-2` as C09. Coordinator execution on 2026-11-04. No push or
PR update was performed.

The compatibility boundary is the reviewed OpenCode v2.2.5 release only. The
owner-provided guest version and `opencode auth` help output were used to confirm
the scoped `auth export openai` and stdin-based `auth import` CLI forms. Tagged
upstream sources were reviewed for the exact loopback callback, authorize URL,
credential schema, and Anthropic/Moonshot provider paths. No login, account listing,
credential-store read, export, import, or active credential use was performed.

Implemented:

- `lib/vm/auth.sh`: version-gated provider resolution, scoped OpenAI export/import,
  fixture-safe validation, private escrow/path checks, staged export, fsync and
  atomic replace, once-per-profile import markers, and runtime Anthropic/Kimi API-key
  installation without raw auth-database access.
- `lib/vm/browser-bridge-server.py`, `browser-open.py`, and `browser-open.sh`:
  per-session authenticated single-tool MCP, exact authorize/redirect validation,
  host-loopback SSH callback forwarding and readiness probe, bounded callback
  leases, and private guest shim/token handoff. Callback completion cannot be
  reliably observed through the supported v2.2.5 interface, so the documented
  120-second default (configurable through 600 seconds) remains the cleanup bound;
  session close and process exit also release the forward.
- `lib/vm/services.sh`, `lib/vm/ssh.sh`, `lab-vm`, and `lib/vm/lifecycle.sh`:
  native-agent import/runtime injection, browser bridge startup/cleanup, validated
  SSH control-socket reuse, frozen-runtime inclusion, and fail-closed export checks
  before the internal destructive lifecycle delete hook. User-facing down/rebuild
  dispatch remains unavailable at this checkpoint.
- `docs/chatgpt-auth.md`, `tests/test_vm_auth.py`, `tests/test_vm_browser_bridge.py`,
  and `tests/vm-process.bats`: exact compatibility/runbook, synthetic-only auth and
  callback fixtures, secret-output canaries, and SSH control-socket policy coverage.

Final validation:

| Command/check | Result |
|---|---|
| `python3 -B -m unittest discover -s tests -p 'test_*.py'` |196/196 PASS; fixtures only |
| `python3 -B -m unittest tests.test_vm_auth tests.test_vm_browser_bridge` |36/36 PASS |
| `bats tests/` |123/123 PASS |
| Ruff on changed auth/browser Python modules and suites |PASS |
| ShellCheck warnings on changed Bash modules/launcher |PASS |
| `bash -n` on changed Bash modules/launcher |PASS |
| `python3 -m py_compile` on bridge/client/path helpers |PASS |
| `git diff --check` |PASS |
| `sha256sum vm-migration-plan.md` |`3d6ee2115423272b88a8efd8d4d33462318d5ae4c537841091e513ee5e2d1374` unchanged |

All tests used synthetic credentials and local temporary files/sockets. No guest
auth data was inspected, no active credentials were used, and no live login,
credential export/import, VM, or host browser was performed. Human callback,
credential persistence/refresh and owner proof remain separate TASK-15 acceptance
gates; this fixture milestone does not claim those live outcomes.

## TASK-11/12 / C10 — revision-bound snapshots and bounded recursion

**State:** implementation and fixture validation committed locally on
`feat/vm-approach-2` as `272140ec0fb7798f96d759623bcf6699e13f16a6` (based on C09
`13a2292`). Coordinator execution on 2026-11-04. No push or PR update was performed.

Implemented `scripts/vm-recursion-workspace.py` for private, filtered snapshots of
the selected self-write checkout. The source must have a canonical Git HEAD;
tracked, dirty and untracked intended files are manifested with path/type/mode/size
and content hashes, while credentials, state, databases, model weights, caches and
`.git` history are excluded. Safe in-tree symlinks and modes are retained. Source
replacement/races, special or escaping/excluded targets, capacity/free-space
violations, changed source for an existing run, modified retained snapshots and
bundle aliases fail closed. Snapshot publication is private, locked and no-clobber.

Implemented `lib/vm/recursion.sh` and path/lifecycle integration for persisted
guest depth/maximum identity, caller-environment conflict detection, explicit
recursive opt-in, non-increasing depth limits (0–32), supported-profile KVM/CPU
preflight before candidate allocation, and a fresh per-run private state root.
Candidate argv points to the verified frozen `lab-vm` and workspace, with a fixed
clean environment and optional capabilities disabled by default. Nested proxy
selection is bound to the immediate parent depth. Exact recursive cleanup refuses
live/unknown descendants or retained child runs and archives reports before removing
only the confirmed run. Fixture tests inspect the candidate argv and changed source
snapshot; they do not start that candidate as a VM.

Validation:

| Command/check | Result |
|---|---|
| `python3 -B -m unittest discover -s tests -p 'test_vm_*.py' -v` |214/214 PASS; local fixtures only |
| `bats tests/` |129/129 PASS |
| Ruff on changed Python implementation/tests |PASS |
| ShellCheck on changed Bash modules/fixtures; `bash -n` |PASS |
| Python `ast.parse` on changed Python files; `git diff --check` |PASS |
| `python3 scripts/check-docs-consistency.py` |8 existing README/DECISIONS drift/link failures; same baseline failures |
| `sha256sum vm-migration-plan.md` |`3d6ee2115423272b88a8efd8d4d33462318d5ae4c537841091e513ee5e2d1374` unchanged |

No VM was launched; no live KVM/nested-guest, credentials, network, or host
acceptance activity occurred. `run`/`up` remain unavailable until TASK-16; only
the exact-run cleanup command is dispatched. Real nested-VM and TASK-15 host/human
acceptance remain pending. The staged-empty/worktree-populated
`vm-migration-plan.md` is excluded from the C10 commit.

## TASK-14 / C12 — explicit guest updates and validation tools

**State:** implemented and fixture-validated locally on `feat/vm-approach-2` on
2026-11-05. The C12 checkpoint is local only; no push or PR update was performed.

Implemented guest-side `scripts/vm-update.sh` scan/apply plans with a private,
expiring plan, exact selected targets/versions, read-only scans, stale-plan and
state rechecks, explicit cancel/confirmation, auth compatibility blocking, and
partial-result reporting. Apply rechecks the active OpenCode process using full
`/proc` argv and controlling-TTY evidence, and applies the guest-workload guard to
disruptive OS/Docker updates. Host lifecycle integration only targets an already
ready and verified-running managed guest; it does not provision or resume one.
Warm agent/setup paths remain separate from update.

Added the exact-version selected Python tools installer and the reusable
`scripts/vm-opencode-tui-check.py` process guard. Config/runtime refresh reuses the
setup payload hash and staged guest-private replacement; it preserves the original
ready-marker payload identity and tracks update hashes separately. Profile
OpenCode version metadata is committed only after guest success. Failure handling
preserves workspace, overlay, auth and existing runtime/config state where
possible, without claiming rollback for package-manager partial operations.
CLI parsing, cloud-init provisioning, guest source identity and profile-lock
helpers were integrated; C12 fixture acceptance is marked in
`PR_REIMPLEMENTATION_TASKS.md`, and `docs/vm-architecture.md` records the live
acceptance boundary.

Validation:

| Command/check | Result |
|---|---|
| `python3 -B -m unittest discover -s tests -p 'test_*.py' -q` |249/249 PASS; fixture-only |
| `bats tests/*.bats` |131/131 PASS |
| Ruff on changed Python implementation/tests |PASS |
| ShellCheck on changed Bash implementation/launcher |PASS |
| `bash -n lab-vm lib/vm/*.sh scripts/*.sh` |PASS |
| Python compilation for changed helpers |PASS |
| `bash scripts/check-vm-cloud-init-schema.sh` |PASS |
| `git diff --check` |PASS |
| `python3 -B scripts/check-docs-consistency.py` |8 existing README/`docs/DECISIONS.md` drift/link failures |
| Repository-wide `ruff check .` |Existing unrelated findings: `metrics_service.py` F841/BLE001 and `test_vm_agent.py` ISC004 |
| Repository-wide default ShellCheck |Existing informational findings in unchanged `lib/vm/auth.sh` and `lib/vm/services.sh`; changed-file checks pass |
| `sha256sum vm-migration-plan.md` |`3d6ee2115423272b88a8efd8d4d33462318d5ae4c537841091e513ee5e2d1374` unchanged; untracked and excluded |

No live guest, VM, host package, network, credential, or host acceptance was run.
Fixture results do not satisfy E2E-LIFE or other live acceptance. Update/host
gates remain deferred to TASK-15 and its current owner-reviewed handoff. The
untracked `vm-migration-plan.md` was not modified or included.

## TASK-15 planning reconciliation / model handoff — 2026-11-05

**Owner request:** inspect the old branch and its MCP tickets, treat the approved
replacement plan as authority, rectify the plan/tasks/progress here, commit and
stop for a model swap. Do not implement C13 or perform host acceptance in this
session. This record supersedes the preceding response's suggestion that missing
old tickets or unresolved old handoffs must be repaired before work can proceed.

### Historical inspection and findings

Temporarily switched the clean tracked checkout from `feat/vm-approach-2` at
`65a798d` to `feat/vm-approach` at
`97af1d83a16fce68ddcc3390276c218a1b0cd518`, read original tickets #35/#41–43/#45/#48–57
through MCP with explicit `ticket_dir=/workspace/docs/tickets`, then returned to
`feat/vm-approach-2` before making edits. No old-branch file/ticket was changed.
The default replacement store's missing #50 is expected historical divergence;
it was never a permission blocker. No ticket copying/migration is needed.

| Historical evidence | Replacement disposition |
|---|---|
| #41 runner selected `launcher-smoke` despite a fresh outer name, interrupted apt, then refused a fresh outer because the stable snapshot was actively exported | Keep exact-name, ownership, immutable source and deadline protections; remove stable-snapshot discovery/reuse. Use unique campaign state/snapshots, not a repair/restart of the old exporter |
| #35/#41 accumulated one-attempt approvals, preserve-all locks and conflicting superseding order instructions | One owner-approved replacement scope covers ordinary stages, bounded diagnosed retries and exact-run cleanup; escalate actual scope/protected-state changes, not every routine command |
| #41 hung SSH stream, mistaken process candidates and memory blocks (roughly 3.2 GiB available vs 4 GiB + reserve) | Require bounded completion, component resource evidence and verified ownership. Never signal raw PIDs or ignore RAM/cgroup limits; do not require a literal physical CachyOS host when a qualified nested test context is intended |
| #41/#52 old A→B→B and limited nested continuation had owner evidence | Historical learning only: repeat replacement gates at its revision; old partial passes do not establish changed-VM G1/G2/fail→fix→fresh proof |
| #42 default update scan passed without status 141, but apply/retention remained open | Keep draining parser coverage and explicitly include live scan/cancel/no-op/selected exact-version apply and state retention in E2E-LIFE |
| #45 superseded provider-only egress with public HTTP(S) through the proxy; browser sign-in was not persistence/refresh proof | Retain replacement public-only proxy boundary and independent auth lifecycle/secret gates; no obsolete domain-approval hurdle or raw auth-store copy |
| #50 correct argv did not settle actual prompt/deny UI; #56 live concurrency remained unproven | Require real harmless allow/deny, UI and concurrent-session observations; use the replacement's reviewed version, not old 2.2.0 behavior as acceptance |
| #43 owner replaced mandatory second-device inbound test with actual host/outer loopback listener checks | Amend SEC-NET to inspect real OS binds/process ownership plus QEMU argv and guest/child probes; second LAN device optional. Do not claim measured LAN reachability without that extra measurement |
| #55 push-only sharing; #53/#54/#57 future documentation/usage/readiness work | Keep the existing push-only boundary and exclusions; no future MCP/schema feature becomes a TASK-15 prerequisite |

### Changes to the replacement plan

- R34 now preserves valuable original state without importing its campaign's
  no-retry lock. Master §9 defines one bounded campaign scope, explicit recovery,
  cleanup, gate ownership and usable-guest-first/human checkpoints.
- TASK-15 explicitly owns the real adapter missing from C11, not only a report
  wrapper. It reuses frozen lifecycle/agent/recursion APIs while public `run/up`
  stay gated. This avoids a TASK-15↔TASK-16 dependency deadlock.
- TASK-16 may stage explicit routing/Docker rollback before live completion; the
  final route/default/rollback smoke stays its responsibility. VM-default cutover
  still requires the other live gates and final smoke, never just C13 fixtures.
- Human observations can start once the clean disposable guest/model path works;
  they need not wait for expensive recursive proof. All required gates still
  block release if incomplete. Missing human input never becomes PASS by default.
- Updated architecture status to name the pending adapter implementation and
  canonical campaign policy rather than imply that a historical handoff unlocks
  an already-complete live backend.

### Remaining work / resumption order

1. **Next implementation session:** build C13 acceptance/isolation tools, the real
   self-test adapter and docs, with fixture/mock failure/timeout/resource/ownership
   tests. Run impacted and full fast checks; commit locally. Do not push or run
   live tests just because these planning amendments exist.
2. **Before live testing:** generate/verify `--help` and dry-run, then present one
   revision-bound campaign handoff with unique state/snapshots/profiles, protected
   assets, resource/retry/cleanup limits, selected model/auth options and private
   evidence paths. Owner approves that scope; normal OAuth/destructive prompts
   remain explicit. No old ticket signoff chain.
3. **Actual supported test environment:** read-only preflight → separately approved
   setup/clean guest → basic warm/model streaming + human TUI/permission/auth
   checkpoint → Compose/project feedback and A→B→B/update/lifecycle retention →
   changed-VM fail/fix G1/G2/depth refusal and outer/child boundaries/concurrency →
   exact-run cleanup/evidence review. Independent gates can retain results while
   another is BLOCKED. Stop dependent stages on failure; diagnose and use only
   bounded, campaign-scoped retries.
4. **TASK-16:** staged explicit route/Docker rollback/docs/CI, acceptance review,
   then accepted VM-default switch and final default/rollback smoke. Keep Docker
   working/auth intact until acceptance. Publishing/merge remains owner-controlled.

### Verification and scope

This is documentation-only; no application/test code, VM, host setup/network,
model service, credential workflow or ticket data changed. The C12 results
(249 Python/131 Bats and changed-file checks) are prior evidence, not rerun by
this planning session. Documentation validation commands/results are recorded
below after execution. Pre-existing eight README/DECISIONS consistency findings
and unrelated repository-wide lint findings remain deferred as recorded at C12.
`vm-migration-plan.md` remains untracked and byte-identical at SHA-256
`3d6ee2115423272b88a8efd8d4d33462318d5ae4c537841091e513ee5e2d1374`.

Planning-session validation:

| Check | Result |
|---|---|
| `git diff --check` |PASS |
| Python stdlib Markdown/plan integrity check over the four changed docs |PASS: local Markdown links, balanced fenced blocks, no conflict markers; 34 requirements, 16 task sections and all 15 independent live gates retained; only four documentation paths changed |
| `python3 -B scripts/check-docs-consistency.py` |Exit 1: same eight pre-existing README/`docs/DECISIONS.md` drift/link failures across 106 checks; no new failure |
| Branch/status and mission hash |Returned to `feat/vm-approach-2`; original branch untouched; mission file hash unchanged and excluded |

No application suites were rerun for these documentation-only changes. No push,
ticket write, host acceptance or C13 implementation was performed. Stop after the
local planning commit for the owner's model swap.

## C13 local implementation and validation — 2026-11-05

**Scope resumed:** complete the C13 code checkpoint only. The earlier planning-only
direction above is historical and superseded for this resumed session. No live
acceptance, host mutation, network probe, credential action, human test, TASK-16
work, or push was authorized or performed at that checkpoint. `run`/`up` were
fail-closed at that checkpoint; the later package-1 approval supersedes that
restriction for the experimental route only. Ordinary `lab` remains on Docker.

**Implementation:** added `scripts/vm-host-acceptance.py` with read-only scoped
preflight, explicit confirmed execution, generated private run/snapshot ownership,
fresh clean guest checks, exact listener/process verification, daily warm/A→B→B,
outer/child network probes, bounded lifecycle/update/resize checks, live self-test
adapter integration, sanitized fixed-enum gate reports, owner checklist recording,
and manifest-only graceful deepest-first cleanup. Added
`scripts/vm-guest-isolation-check.py` with fixed bounded guest probes and private
sanitized reports. Completed the `--live` nested-VM adapter in
`scripts/vm-self-test.py`, including candidate bootstrap/lifecycle mutations,
G1/G2/G3 evidence, refusal-before-state/QEMU checks, parent continuity, exact
cleanup, and the existing frozen lifecycle interfaces. Added fixture coverage in
`tests/test_vm_host_acceptance.py`, `tests/test_vm_guest_isolation_check.py`, and
`tests/test_vm_self_test.py`, plus the owner handoff in
`docs/vm-host-acceptance.md`.

Review corrections included enforcing the SSH deadline at both host and guest,
selecting only the registry for the exact live process identity, treating any
correlated direct DNS reply (including empty-answer/NXDOMAIN) as egress, blocking
lifecycle resize campaigns below the required 8 GiB, graceful exact cleanup of
ready profiles, and preventing an owner attestation from overriding an unstaged
Docker rollback prerequisite. Some behavior gates remain deliberately BLOCKED
until real owner evidence is recorded; no fixture or health result is described
as live PASS. The isolated campaign does not import host provider configuration
or credentials, so configured-model streams and model-dependent agent work remain
BLOCKED until a reviewed provider-specific campaign configuration is available.

| Exact local command | Observed result |
|---|---|
| `python3 -m unittest discover -s tests -p 'test_*.py'` |279 tests, OK, exit 0; 67.910s |
| `bats tests/` |131/131 pass, exit 0 |
| `python3 -m unittest tests.test_vm_self_test tests.test_vm_host_acceptance tests.test_vm_guest_isolation_check` |41 tests, OK, exit 0; 13.689s |
| `ruff check scripts/vm-self-test.py scripts/vm-host-acceptance.py scripts/vm-guest-isolation-check.py tests/test_vm_self_test.py tests/test_vm_host_acceptance.py tests/test_vm_guest_isolation_check.py` |PASS |
| `python3 -m py_compile scripts/vm-self-test.py scripts/vm-host-acceptance.py scripts/vm-guest-isolation-check.py tests/test_vm_self_test.py tests/test_vm_host_acceptance.py tests/test_vm_guest_isolation_check.py` |PASS |
| `bash -n lab-vm` |PASS |
| `shellcheck -x lab-vm` |PASS |
| `python3 scripts/vm-host-acceptance.py --help`; `python3 scripts/vm-guest-isolation-check.py --help`; `python3 scripts/vm-self-test.py --help` |PASS; explicit confirmation/cleanup/observation options displayed |
| `git diff --check` |PASS |

Live gates remain **PENDING/BLOCKED** until a later owner-reviewed exact campaign
is authorized and performed. Actual model streams, guest Compose/project fix,
multi-agent work, filesystem/secret canaries, UI/permission/auth observations,
changed-VM nested proof, and exact live teardown have not been run. C13 is
committed locally as `1e0b0bacfd99234b12b69cc4a53fa7f1e4eff53c`; this separate
ledger-only commit records that SHA. No push is planned. Preserve the untracked
`vm-migration-plan.md` unchanged and exclude it from C13.

## Cgroup v2 memory-preflight compatibility fix — 2026-11-05

The owner ran the read-only host preflight from a clean C13 worktree. Linux and
KVM, CPU virtualization, required tools, disk budget, and path/listener checks all
passed, but memory accounting was BLOCKED. The follow-up diagnostic showed about
36 GiB `MemAvailable`; cgroup v2 memory files existed in the owner's session
cgroup but not at `/sys/fs/cgroup`. The previous checker incorrectly required
`memory.max` and `memory.current` at the v2 hierarchy root, even though the root
cgroup is exempt from controller resource interface files.

Updated `scripts/vm_memory_budget.py` to accept both files absent only at a
verified cgroup v2 hierarchy root, while still requiring files at every visible
non-root ancestor. It verifies that exactly one cgroup2 mount exposes the real
hierarchy root, that the mapped `cgroup.procs` contains the target process, and
that the process membership remains stable during the read. Missing controller
files below root, hidden ancestor hierarchies, and ambiguous mappings still fail
closed. Added fixture tests for the root-file exception and these safety guards
in `tests/test_vm_memory_budget.py`.

| Exact local command | Observed result |
|---|---|
| `python3 -m unittest tests.test_vm_memory_budget` |21 tests, OK |
| `python3 -m unittest discover -s tests -p 'test_*.py'` |281 tests, OK; 60.386s |
| `ruff check scripts/vm_memory_budget.py tests/test_vm_memory_budget.py` |PASS |
| `python3 -m py_compile scripts/vm_memory_budget.py tests/test_vm_memory_budget.py` |PASS |
| `git diff --check` |PASS |

This was fixture/local validation only; the owner's host preflight has not yet
been rerun with the fix, and no VM or live acceptance campaign was started. The
follow-up fix is committed locally as
`232180b0a1c9440d40c29adbf256f0bd995dc3e7`; this ledger-only commit records its
SHA. No push was performed.

## Acceptance-readiness planning revision — 2026-11-05

**Authority:** owner requested a plan only, no implementation, to resolve the
known blockers before host acceptance and the next phase. Updated canonical
plan/tasks/progress; application code, working runtime and credentials untouched.

**Owner-reported host evidence:** clean detached worktree on
`336cf69c8b287f3c7f6e3539efdae7c1d5bf9503`; run ID
`ec97a5616e63467b961ccb8d1649a6bb`. Memory diagnostic returned `ok: true`,
36,224 MiB available and no finite cgroup cap. Full read-only preflight returned
PASS on all checks with 36,201 MiB available, 8,192 MiB requested + 512 MiB reserve,
no missing tools and no run/evidence collision. Source tree hash:
`d610355c86bdd89ce2d6de4670e6d57fb2cc717bb0fbfbddaeafd9dfd8e8a39c`.
These are the owner's supplied results, not tests run by this planning session.
No VM boot, live acceptance campaign or credential operation was reported.

**Decisions confirmed with owner:**
- Named separate prerequisites: C13a routes/rollback, then C13b campaign completion.
  The transition dispatcher stays routed to Docker pending acceptance; the
  intended released default remains VM, with Docker optional. C14 is the later
  cutover, not a blocker to preparing the route E2E-DAILY tests.
- Test Ollama and llama.cpp using existing services; perform read-only discovery
  later and explicitly select endpoints/model IDs before live execution.
- Remote provider is OpenAI through human browser sign-in. The runner must open
  the browser and wait; credentials are never supplied to the agent/chat. Offer
  opt-out early and at final auth checks; reuse the campaign's authorized login
  during intermediate stages, with no unnecessary repeated prompts. Skip blocks
  dependent OpenAI/auth gates, not independent local/boot/security work.
- Staged bounded sessions: up to 60 minutes per automated stage/attempt, tighter
  operation/callback deadlines retained, human pauses distinct from automation;
  one diagnosed fresh retry per revision inside the approved total budget.

**Remaining pre-execution choices:** exact local/remote model IDs/endpoints,
supported pinned OpenCode comparison version, paid OpenAI usage allowance, total
campaign/download/disk limits, and explicit consent for sensitive guest storage,
run-owned private auth export/reimport/recursive forwarding and destructive test
profiles. Browser interaction is not blanket permission for those operations.
Refresh unavailable in the budget stays BLOCKED rather than being fabricated.

**Next work after C13a:**
1. TASK-15B/C13b only after separate authorization: first audit all gate/subcheck
   procedures and actual nested topology;
   implement missing drivers, private selected-provider config, exact-run staged
   resume, optional human browser checkpoints, workload/state-retention and final
   cleanup evidence; publish a friendly stepwise host handoff and pass local tests.
2. Review exact final revision/plan and obtain scoped campaign approval. Fresh
   preflight → clean/usability → optional early login → providers/daily/permissions
   → real work/retention/lifecycle → recursion/boundaries/agents → final optional
   auth → exact teardown/evidence review. Preserve independent partial results.
3. Only once required gates pass and are reviewed: remaining TASK-16/C14 docs/CI,
   separately authorized default activation, final default/rollback smoke. No
   final delivery/merge authority implied.

The planning session did not run commands against the owner's host. This C13a
implementation ran only local fixture/regression checks; it did not exercise the
working Docker session, create a VM, run acceptance, or touch credentials.

Planning validation: `git diff --check` PASS; Python stdlib integrity check of
the three changed Markdown files PASS (local links, balanced fences, no conflict
markers, all fifteen gates and eighteen task sections retained). The repository
docs-consistency command (`python3 -B scripts/check-docs-consistency.py`) exits 1
with the same eight pre-existing README/decision-log drift/link findings across
106 checks (same eight README/decision-log findings reported before C13a; no new
failure category). `vm-migration-plan.md` remains untracked, excluded and unchanged at SHA-256
`3d6ee2115423272b88a8efd8d4d33462318d5ae4c537841091e513ee5e2d1374`.

## C13a implementation closeout — 2026-11-05

**Authorization:** user authorized implementation mode for C13a only, reaffirming
the final product default is VM and Docker is an optional fallback. This is a
transition-stage route: until C14 acceptance/cutover, `lab` continues to delegate
to Docker; `lab-docker` makes Docker explicit, and `lab vm` invokes the existing
fail-closed VM CLI. VM `run`/`up` gates are unchanged.

**Changes:** moved the existing Docker launcher intact to executable `lab-docker`
and made `lab` a thin dispatcher that preserves argv and legacy source-helper
behavior. Added Docker-side permission parsing because the old `--permissions`
flag was silently ignored: ask omits auto, auto/`--auto-accept` emits one OpenCode
`--auto`, conflicts/unsupported modes fail, Aider rejects auto, and permission
override flags after `--` are refused. No permission config is rewritten, so
native OpenCode explicit deny rules remain in force. Updated route help/README for
the temporary transition and intended VM default. Acceptance fixture now expects
the staged rollback executable while keeping live no-recreation smoke BLOCKED.

**Local validation:** `bats tests/` 141 PASS; Python unittest discovery for
`test_vm_*.py` 281 PASS; `bash -n lab lab-docker lab-vm`, ShellCheck on those three
launchers and `git diff --check` PASS. `scripts/check-docs-consistency.py` remains
exit 1 with eight previously recorded README/decision-log drift/link findings
across 106 checks. No live acceptance, actual Docker invocation, host session
reuse test, VM boot or credential activity. C13a is committed locally at
`599e9ef`; C13b and C14 remain pending later authorization. No push.

## C13b local readiness closeout — 2026-11-05

**Authorization and boundary:** the user requested completion of C13b acceptance
readiness locally and then confirmed the local commit closeout. This does not
authorize an owner-host campaign, VM/service startup, credentials/browser sign-in,
Docker-session changes, C14 cutover, or push. Keep the transition routing from
C13a unchanged: ordinary `lab` uses Docker, `lab-docker` remains explicit, and
`lab vm` remains gated. The old read-only host preflight PASS on `336cf69` is
historical and does not validate C13b commit `d3751e4`.

**Implemented locally:**

- Replaced the live one-shot `--execute` path with exact-run `--start`/`--resume`,
  sanitized `--status`, serialized mutations, revision/config/profile/listener
  resume checks, and a final budgeted exact `--cleanup`/teardown stage. A report
  cannot become PASS before successful teardown.
- Added explicit 1–720 minute campaign ceilings and 60-minute stage caps. The
  private state-root attempt ledger binds source/config revision, same approved
  total, prior elapsed stage time, and at most one diagnosed fresh run-ID retry.
  Resume/retry must preserve the same private `STATE_ROOT`; retry also preserves
  the exact provider-config bytes and source revision.
- Added bounded read-only loopback Ollama/llama.cpp discovery and explicit
  credential-free provider/model configuration. OpenAI browser sign-in remains
  optional/owner-controlled; skipping blocks dependent checks only. No host auth
  is imported and no model transcript is retained.
- Connected guest-only Compose/PostgreSQL health/query/bind and
  container-restart checks to a DB-canary VM suspend/resume probe and exact
  generated-project/volume/workspace cleanup. Compose health now requires a
  successful `up --wait`; unavailable cleanup evidence stays BLOCKED.
- SEC-NET requires actual depth-0/G1/G2 evidence, including G2's own isolation
  report. Browser-bridge cleanup emits only a fixed success marker after exact
  temporary-resource cleanup.
- Added a per-gate procedure/evidence/cleanup matrix, staged Bash/Fish commands,
  ordering for owner observations before teardown, and explicit retry instructions.
  **Known owner-side gap:** this repository does not ship a redacting canary
  scanner. It is marked owner-supplied/unavailable; canary-absence/SEC-SECRET
  subchecks must remain BLOCKED until a reviewed checker exists. Credential
  export/delete/reimport also needs separate consent and disposable credentials.

**Files changed:** `scripts/vm-host-acceptance.py`, `scripts/vm-self-test.py`,
`lib/vm/services.sh`, `tests/test_vm_host_acceptance.py`,
`tests/test_vm_self_test.py`, `docs/vm-host-acceptance.md`, and this progress
ledger. No changes were made to the pre-existing untracked `vm-migration-plan.md`;
its SHA-256 remains
`3d6ee2115423272b88a8efd8d4d33462318d5ae4c537841091e513ee5e2d1374`.

**Local validation:**

| Command/check | Result |
|---|---|
| `python3 -B -m unittest discover -s tests` | 305 tests PASS |
| `bats tests/` | 141 tests PASS |
| Changed-file Ruff | PASS |
| Python compilation for acceptance/self-test/config/isolation scripts | PASS |
| `bash -n lib/vm/services.sh` | PASS |
| Compile embedded workload setup/retention/cleanup guest scripts | PASS |
| Runbook Markdown fences, local links, retry guidance and stage/evidence order | PASS |
| `git diff --check` | PASS |
| `scripts/check-docs-consistency.py` | 8 pre-existing README/DECISIONS.md drift/link failures across 106 checks; unchanged by C13b |

No campaign, VM, provider service, browser, credential, Docker session, or live
acceptance command was run. C13b implementation is committed locally at
`d3751e4fbda25349de5cbcb1f1e6400ae3e489c8`; no push was made. This progress-ledger
SHA closeout is recorded in a separate local commit. At that C13b checkpoint, live
acceptance still required exact-revision review, fresh preflight, separate campaign
approval and the then-current canary-checker/credential decisions. The later
owner-approved local-first scope and this implementation checkpoint supersede that
checker disposition; live acceptance, C14 and release remain unproven and unauthorized.

## Historical owner local-first/C14 planning amendment — superseded

**Owner instruction:** remain in the higher-thinking model for planning and
documentation; do not begin implementation. Document the path and return for
the owner to switch to an implementation model.

This planning-only boundary was superseded by the subsequent explicit
authorization for local §1 implementation, recorded in the current control state
and the implementation checkpoint below. It remains as historical decision
context only.

**Decisions recorded:**

- 1A: minimal repository-owned synthetic-canary checker. The earlier
  owner-supplied/unavailable-tool disposition is superseded as the future plan,
  not as an implemented capability.
- Local models first. Prior Ollama/llama.cpp success on the old branch/Docker is
  a discovery lead, not proof of availability here. Read-only discovery must be
  followed by exact owner provider/model selection on the target host.
- Live OpenAI automation/credential acceptance is deferred and must not block
  merge/C14. Preserve browser auth capability and its automated regressions;
  never convert deferred live results into PASS. Mixed security/lifecycle gates
  must retain their required non-auth and synthetic credential-safety checks.
- Disposable campaign credentials are acceptable in principle; actual login,
  export/import/destructive actions still need separate exact consent. Do not
  copy working host/Docker auth.
- Follow the staged plan with clear automatic steps and explicit owner-input
  pauses. Provide one exact current command at a time, location/shell/revision,
  effects, expected result/time limit, stop conditions and sanitized return data.

**Documented handoff:** `docs/vm-c14-handoff-plan.md`; authority links/amendments
in master plan, task contract and runbook. Contains bounded implementation scope,
scanner coverage/failure rules, required-vs-deferred evidence, guest PostgreSQL
preparation prerequisite, P0–P8 automation/owner responsibilities, teardown budget,
C14/default+Docker smoke and exclusions. At that planning checkpoint the runner
scope had not changed; the following implementation checkpoint records its later
local reconciliation.

**Action boundary at that checkpoint:** documentation only. No application
code/tests were changed, no provider discovery/preflight/campaign/VM/credential/
Docker action was run, and no C14/push/merge action was authorized. Prior 305
Python/141 Bats results describe the committed C13b implementation, not execution
of this new plan.

## Local-first §1 implementation checkpoint — 2026-11-05

**Authority:** the owner subsequently switched back to local §1 implementation.
This supersedes the preceding planning-only boundary. The scope is limited to
repository code, fixtures and documentation; no live campaign, VM/service start,
provider discovery, credential activity, push, merge or C14 activation.

### Implemented locally

- Added `scripts/vm-canary-check.py`: private synthetic-canary loading and bounded
  scans for manifested regular files/trees, loose Git objects, raw ISO9660 seed
  bytes, selected campaign logs/reports and raw QCOW2 bytes. Results contain only
  fixed statuses/reason codes, safe digests and bounded counts. Positive raw and
  encoded matches fail without disclosure; unsafe, incomplete, timed-out or
  unsupported inspection stays BLOCKED. Selected non-log metadata files in log
  trees are scanned too.
- Reconciled `scripts/vm-host-acceptance.py` to workflow v2: local Ollama/llama.cpp
  selections only are streamed; live OpenAI auth/paid streams/refresh/credential
  roundtrips and auth workflow stages are explicitly DEFERRED. The report keeps
  auth regressions required outside campaign scope. Deferred `HUMAN-AUTH` is not
  a required gate and cannot be attested into PASS; required non-auth failures
  still block. Exact run scope binds suites, memory, provider config, update
  effects and budget across start/resume/retry. Nested suites require owner
  `HUMAN-TUI` PASS before recursion and non-teardown work preserves a 35-minute
  teardown reserve. The exact run canary is removed at cleanup and the final
  report/self-test reports are rechecked before campaign PASS is possible.
- Reconciled `docs/vm-host-acceptance.md`, `docs/vm-c14-handoff-plan.md`, the master
  plan/task scope and this ledger. The runbook identifies local-only authority,
  deferred OpenAI work, failure-closed evidence requirements and current
  readiness blockers. The historical `vm-migration-plan.md` worktree file was
  preserved untouched.

### Validation

| Command/check | Result |
|---|---|
| `python3 -B -m unittest discover -s tests` | **323 tests PASS**, including existing auth regression coverage; fixture-only |
| `bats --print-output-on-failure tests/*.bats` | **141/141 PASS** |
| Ruff on the four changed Python modules/tests | PASS |
| `python3 -m py_compile` on the changed Python modules/tests | PASS |
| `bash -n lab-vm lib/vm/lifecycle.sh lib/vm/config.sh lib/vm/runner.sh` | PASS |
| ShellCheck on those production Bash files | PASS |
| `python3 scripts/vm-host-acceptance.py --help` | PASS; read-only help only |
| Markdown fences/local links in the updated runbook and handoff plan | PASS |
| `git diff --check` | PASS |
| `python3 -B scripts/check-docs-consistency.py` | **8 existing README/DECISIONS drift/link failures across 106 checks**; unrelated to changed handoff/runbook files and retained for the planned C14 docs work |

### Still blocked / not established

- `SEC-SECRET` cannot PASS: raw QCOW2 bytes are scanned, but virtual filesystem
  contents are not inspected (`qcow2_content_not_inspected`). Required local
  provider-forwarding and synthetic private-state persistence/atomic
  export-import/failure-before-delete checks are also not yet campaign evidence.
  Canary absence alone is never a security PASS.
- `SEC-FS` remains BLOCKED pending real filesystem-isolation campaign evidence.
- Target-guest registry access for `postgres:16-alpine` remains unverified. The
  follow-up implementation now makes one bounded guest-only pull if missing and
  verifies local availability; failure stays BLOCKED. Fixture coverage does not
  prove target-host registry access, and host Docker is not used.
- No target-host model availability, boot, TUI, workload, network/nesting,
  isolation or exact real teardown was tested. Local fixtures do not replace
  those gates. Ordinary `lab` remains on Docker; C14 remains pending.

The local-first §1 checkpoint is committed locally as `8171011`; the guest-pull
follow-up has now been validated for a separate local-only commit. No campaign,
VM/service, provider discovery, credential, push, merge or C14 operation was
performed.

## Guest-only PostgreSQL download follow-up — 2026-11-05

**Owner decision:** permit downloading `postgres:16-alpine` inside the disposable
guest when it is absent. This does not authorize host Docker, a campaign, or any
live VM/service operation.

The workload now inspects the image in the guest, attempts one `docker pull`
bounded to 180 seconds when absent, using the guest-local Unix socket and a fresh
empty Docker config, then inspects again before creating the Compose workspace.
The fixed report exposes only `postgres_image_available`. Compose has
`pull_policy: never` and is always invoked with `--pull never`; unavailable image
or failed verification stays BLOCKED. The guest command deadline is 480 seconds
to leave room for the bounded download and workload checks.

Added fixture coverage for both the mocked successful pull path and unavailable-
image BLOCKED path, including pull order/deadline, no Compose startup on failure,
and the no-implicit-pull Compose settings. Updated the owner runbook and handoff
plan. Tests do not contact a registry or invoke host/guest Docker.

### Follow-up validation

| Check | Result |
|---|---|
| `python3 -B -m unittest discover -s tests` | **324 tests PASS**, fixture-only |
| `bats --print-output-on-failure tests/*.bats` | **141/141 PASS** |
| Ruff on acceptance runner and tests | PASS |
| Python compilation, including embedded guest workload code | PASS |
| Bash syntax and ShellCheck on production lifecycle scripts | PASS |
| Acceptance CLI help; updated Markdown fences/local links | PASS |
| `git diff --check` | PASS |
| `python3 -B scripts/check-docs-consistency.py` | 8 existing README/DECISIONS drift/link failures across 106 checks; unchanged |

No registry was contacted and no host Docker, guest Docker, VM/service, provider,
credential, campaign, push, merge or C14 operation was used. The follow-up commit
is local only and remains unpushed.

**Follow-up implementation commit:** `50c671b6b04903eb491a6ce40436be9cc8b69dcd`
(`Allow bounded guest-only PostgreSQL pull`); no push was made. This SHA closeout
is recorded in a separate local progress-ledger commit.

## Experimental explicit development VM entry — 2026-11-05

**Authority and boundary:** the owner approved the fixed four-package sequence
recorded above and in `docs/vm-c14-handoff-plan.md`. Package 1 is the experimental
explicit run/up entry and the first VM started by the owner on this branch. The
assistant did not and must not start a VM. Ordinary `./lab` remains on Docker.
The 360-minute campaign ceiling (including cleanup and one diagnosed retry) is
already approved; do not request campaign approval again. Pause for genuine owner
observations or new effects. Manual owner OpenAI authentication experimentation
is optional in the disposable VM; paid streams and credential export/import are
excluded. No provider/model has been selected; the default remains `none`.

**Implemented in `6cec2ae18fa1d23178ce3276d14a75009f0a814f`:**

- Wired `lab-vm run/up` to the existing lifecycle controller. `up` returns after
  successful readiness without attaching an agent; `run` attaches through the
  profile's frozen runtime only after lifecycle success and preserves argument
  boundaries/exit status.
- Kept unsupported cleanup acknowledgements (`--clean` and
  `--discard-recursive`) fail-closed for this experimental entry. No default
  `lab` route change, host package install, VM startup or provider discovery.
- Reconciled CLI/Docker help, README, architecture, acceptance handoff/runbook,
  task/plan authority and route fixtures. Handoff names the writable checkout
  flag and depth-0 cap; it warns that the named profile persists and can resume.
- Preserved the pre-existing untracked `vm-migration-plan.md`; SHA-256 remains
  `3d6ee2115423272b88a8efd8d4d33462318d5ae4c537841091e513ee5e2d1374`.

**Validation:**

| Command/check | Result |
|---|---|
| `python3 -B -m unittest discover -s tests` | **324 tests PASS** |
| `bats --print-output-on-failure tests/*.bats` | **145/145 PASS** |
| `bash -n lab lab-docker lab-vm lib/vm/lifecycle.sh` + ShellCheck on those paths | PASS |
| `lab --help`, `lab-vm --help`, acceptance-runner `--help` | PASS; read-only help only |
| Markdown fences and local links in seven touched plan/runbook/readme files | PASS |
| `git diff --check` | PASS |
| `python3 -B scripts/check-docs-consistency.py` | **8 existing README/DECISIONS drift/link findings across 106 checks**; no new category |

No image was downloaded; no QEMU/VM/service, provider discovery, live
authentication, campaign, host Docker, push or merge operation occurred. The old read-only
preflight passed at `422a523` (run ID `2fb37d376f9040b3bbc6be4a5d216dd7`), but
predates this entry and is not acceptance-candidate preflight evidence. Commit
`6cec2ae` is local and unpushed; the progress-ledger SHA closeout follows
separately. Owner may start the dedicated development profile only from a parent-
VM checkout containing this commit; if that checkout does not contain this
exact revision, stop rather than use a stale tree or assume a push.

## Owner-reported legacy state rejection and isolated development root — 2026-11-06

The owner ran `./lab vm list -d` on the target host. Canonical path, ancestors and
root metadata passed; the private-tree scan checked 126 entries and found 112
violations with zero walk errors. The diagnostic displayed 25 entries and omitted
87. Reported examples include `vm-services` mode 0755 and legacy log/VM files mode
0644. Exit status was 2. This is the default legacy state tree, not a false positive
or a repair request. Do not chmod, delete, migrate, or exempt it; preserve the
legacy VM/processes and ordinary Docker route.

**Package-1 way forward:** run from the already-running `feat/vm-approach` parent
VM, using its nested-KVM environment and the already-supported `MESTER_STATE_DIR`
override for a fresh, dedicated `little-mester-branch-dev-vm-approach-2` root. Do
not treat native-host or generic-container runs as evidence for this child launch.
The owner must recheck that exact path is absent and rerun read-only resource capacity checks
immediately before startup; the earlier absence/capacity observation can go stale.
The updated `docs/vm-c14-handoff-plan.md` gives a read-only `context --json -d`
check followed by the owner-only start command with metrics on by default.
The assistant does not start a VM. The first boot is only the package-1 usability
observation; recursive G0→G1→G2 changed-VM fail/fix proof remains package 3.
OpenAI login stays optional and manual in the disposable guest; no automated live
OpenAI auth test, paid stream, credential export/import or refresh test is added.

The owner then attempted that `run` command and received the generic VM policy
validation refusal after the state-root diagnostics passed. A read-only rerun in the
parent VM confirmed the dedicated root and profile were absent, and isolated the
failure to `workspace.mester_marker=FAIL`; all preceding workspace checks passed.
The marker was `/home/ethan/.mester` (directory, mode 0755, uid 1000). The owner
confirmed it was empty and removed it. A subsequent read-only check reports the
dedicated state root/profile absent and all workspace-policy checks passing for
`/home/ethan/Development/little-mester`. The subsequent start attempt exited 2
without allocating a profile. Source tracing found the silent pre-allocation cause:
default `model.provider=none` omits `model.port`, but lifecycle treated that
optional JSON key as required. The lifecycle now treats an absent port as empty;
local-model providers still require a valid explicit port. The fresh-up fixture now
uses the actual default shape, and its targeted Bats test passes. Owner rerun remains
pending.

TASK-02 continues to refuse checkout-local `.mester` state even with
`--unsafe-recurse`; do not weaken the guard. Inspect any such marker read-only and
only remove it if the owner confirms it is empty and explicitly chooses removal;
otherwise use a clean checkout outside that marker tree. The CLI's debug flag
previously did not reach workspace-policy validation, so the follow-up adds bounded,
content-free workspace check labels and propagates `-d` to that path. Metrics stay
enabled by default; the handoff command no longer overrides them off.

### Package-1 prestart cache failure and bounded recovery

Owner's next start passed workspace/resource prerequisites, then warned that
metrics forwarding was unavailable and exited 2. Inventory identified a 0755
`runtime/lib/vm/__pycache__`; direct read-only metadata inspection confirmed
`starting`, no runtime identity, and no printed run/metrics registries. Source
ordering identifies dynamic sibling imports before the frozen worker scan as the
likely cause. Suppress bytecode in the metrics/process/path helpers and launch
environment; propagate debug policy labels to the frozen worker.

`scripts/vm-recover-prestart.py` defaults to read-only. Explicit `--apply` accepts
only a starting profile without runtime identity, runs or guest assets, checks
live process references and stopped reservation ownership, acquires existing
nonblocking allocation/profile locks, and atomically archives the entire profile
outside the state root. It preserves cache evidence/runtime exactly, then requires
normal full-tree validation before releasing the exact dead-owner reservation.
No chmod, disk deletion, legacy cleanup, runtime patch or metadata transition.
Fresh owner launch recaptures the fixed source under the original profile name.
`scripts/vm-start-diagnostics.py` reports selected metadata/assets and verified
run/metrics process states without argv or full file contents, even when an
unrelated tree-mode violation prevents normal status. PR_REIMPLEMENTATION controls
this work; superseded original-branch handoffs are not an additional gate.
Default-root UX remains deferred until acceptance. Ordinary lab stays Docker.
Local validation: recovery/diagnostic fixtures 7/7, metrics Python fixtures 13/13,
lifecycle/metrics Bats 32/32, CLI Bats 35/35 PASS. New scripts/tests Ruff,
launcher/lifecycle ShellCheck, Bash syntax and diff checks PASS. Launcher exports
bytecode suppression for all helper imports, including inline identity checks.
No owner VM lifecycle operation or host acceptance was performed by the assistant.

Owner ran recovery successfully: exact prestart profile preserved at the printed
private sibling archive, dead-owner reservation released. The next launch failed
before workspace validation/profile allocation. Source review found that the
metrics helper creates `vm/metrics`, but `profile_path` omitted `metrics` from
its allowed namespaces. Added that existing service namespace without allowing
arbitrary entries. Paths fixtures pass 48/48, including metrics present before
profile creation and continued refusal of unknown namespaces. Startup inventory
now reports absent profiles instead of FileNotFoundError. No second recovery is
needed: retry the fresh launch from updated source, retaining metrics and archive.

### Immediate execution plan to C14

1. Owner retries the same dedicated-profile launch from fixed source. Preserve
   both parent VM and prestart archive. On failure collect debug output plus
   startup inventory; diagnose that stage, never restart a live/unknown profile
   or apply prestart recovery blindly.
2. Once ready, owner verifies native TUI, writable intended workspace, metrics,
   and requested permission behavior. This closes package-1 usability only.
3. Finish package-2 remaining bounded security/evidence implementation listed in
   docs/vm-c14-handoff-plan.md. Reuse existing fixtures; no new auth campaign,
   default-root migration, historical ticket gating or unrelated enhancements.
4. Freeze the candidate and run the already-approved package-3 integrated campaign:
   actual host preflight, owner-selected local model streams/agent work, Docker
   workload, lifecycle persistence, host/guest isolation, and changed-VM recursive
   fail/fix proof with parent continuity. Retain structured evidence; mocks do not
   replace real observations. Manual OpenAI auth remains optional and deferred.
5. Required evidence passes -> request C14 activation approval once, implement
   TASK-16 default/rollback/docs/CI closeout and rerun affected checks. Merge/push
   only with explicit authorization. Do not bypass functionality/security gates.

### Seed-write root cause and next implementation-model handoff

Owner's fresh retry cleared the namespace/cache refusals. Inventory: `failed`,
overlay present, seed ISO absent, no runs or runtime identity; metrics registry
absent. Reproduced failure locally by passing actual renderer output into
`seed_validate`: the broad secret regex rejected `lock_passwd: true` and the
fixed `ALL=(ALL) NOPASSWD:ALL` sudo value. JSON cloud-config now exempts only
those exact structured values before scanning; arbitrary password/token fields,
lock_passwd=false, and altered sudo strings remain refused. YAML input keeps
the existing conservative scan. This heuristic is not a universal secret scanner.

Added stage-specific seed-render/write/key-publish/ISO-publish errors. A local
integration fixture uses real ssh-keygen and genisoimage, actual renderer and
publication policy, with only overlay creation and payload hashing stubbed; all
four assets publish privately and staging cleans up. No QEMU/network/guest run.
Recovery now has explicit `--archive-failed-before-boot` mode: only failed
profiles without ISO/runs/runtime identity, with stopped ownership and no live
profile references. Archives the entire overlay/runtime intact outside state,
then releases a verified dead-owner reservation. No in-place runtime updates.

Next owner action is the current retry command in docs/vm-c14-handoff-plan.md.
Next implementation model must read this checkpoint plus the immediate execution
plan above, not restart historical ticket handoffs. Await the owner-run result;
do not launch VMs from this assistant. If another failure occurs, collect debug
output and startup inventory before choosing recovery. Frozen active profiles
must not be patched. Metrics warning is not a proven code defect: absent registry
can mean a parent/legacy port conflict; startup diagnostics now report listener
presence. Never kill or adopt an unverified listener. Resolve from actual output.
Once boot works, close package-1 usability, then package-2 gaps and the already
approved frozen integrated campaign before C14. Boot alone is not release acceptance.
Validation for this checkpoint: paths 49/49 (including real local seed publication),
recovery/diagnostics 9/9, cloud-init 6/6, metrics Python 13/13, lifecycle/disk/metrics
Bats 46/46 PASS; changed Python Ruff, launcher/lifecycle ShellCheck and diff checks
PASS. No host VM boot, metrics-forwarding success, or C14 acceptance is claimed.

### Boot observed; Ubuntu identity fix and SSH diagnostic handoff

Owner's next launch booted Ubuntu 24.04.5; serial log shows ssh.service started
and cloud-init final failed with `Guest OS identity is unavailable`. The guest
preflight rejected Ubuntu's standard os-release symlink. Fixed exact relative
`../usr/lib/os-release` and absolute `/usr/lib/os-release` targets, resolved under
the selected guest root; arbitrary links, symlinked final targets, escapes,
oversize identity files and non-pinned OS identities remain refused. No shell
sourcing of OS data. Fixture tests cover normal, relative/absolute and escape.

Host runner independently timed out at 300s waiting for SSH key trust. Owner
inventory after exit124 verifies proxy/virtiofsd/QEMU and all logger identities
stopped; profile failed, no recorded runtime identity. The QEMU exit0 and
virtiofsd exit1 after rollback are not proof of original failure causes. Source
review has not established why SSH key trust failed; do not claim it fixed.
Added named proxy/filesystem/QEMU/SSH/cloud-init stages, 15-second wait progress,
elapsed/limit and console paths. SSH exposes fixed check labels for port validation,
listener ownership before/after scan, scan unavailable/timeout, host-key
validation/persistence and verified. No argv, key payloads or arbitrary stderr.

New explicit `--archive-stopped-failed` recovery supports this booted failed
profile only after verifying all run registries stopped, no live references,
stopped recorded identity/reservation, and no possible descendants. Whole overlay,
ISO, keys, frozen runtime and run evidence are archived intact, never patched or
deleted. Existing prestart modes remain narrow and refuse booted profiles. The
latest owner command is in docs/vm-c14-handoff-plan.md.

**Implementation-model continuation (only if SSH still fails):**
1. Collect next runner `check=` label, startup inventory and bounded console tail;
   do not infer SSH success from ssh.service startup or root cause from rollback.
2. If ownership fails, inspect the exact registry QEMU identity, selected loopback
   hostfwd port and `/proc/net/tcp` socket inode ownership using a content-free
   report. Check FD enumeration races/permissions and IPv4 parsing; never bypass
   ownership or trust a foreign/legacy listener.
3. If keyscan fails, use the verified run's exact loopback port for a bounded
   scan, report exit/result shape only (not raw argv/keys). Verify guest network
   and restrict=on hostfwd wiring; no broad network relaxation.
4. If key validation/persistence fails, fixture-reproduce the actual scan shape
   (nondefault-port host syntax, complete Ed25519 wire blob, single-key rules)
   and private known_hosts publication/permissions. No StrictHostKeyChecking
   relaxation, host-file trust fallback or blind retry.
5. Fix only the reproduced defect and retain regression coverage. Do not patch
   active frozen runtime. Any new retry must preserve the stopped profile/run
   and recapture source; no cache cleanup, old handoff gate or legacy changes.
6. Once SSH succeeds, cloud-init/harness has its own bounded readiness gate;
   fail early on terminal cloud-init failure with fixed reason codes in a future
   scoped change rather than letting known failures consume the full deadline.
   Remaining security/integrated campaign and C14 ordering above still apply.
Local validation: guest fixtures 15/15, recovery fixtures 12/12, process/lifecycle
Bats 40/40 PASS; changed Python Ruff, production Bash ShellCheck and diff checks
PASS. These are fixture results, not a successful owner boot or SSH fix claim.

### SSH two-line scan boundary fix / immediate next owner run

Run43ec03cc-d7a5-4bbb-a34f-50dbfb33f937 exited124 after300s at
host-key-validation-or-persistence. During the run, owner scan exited0 with two
stdout lines; strict `_host_key_line` failed, run directory owned0700, known_hosts
absent. This narrows failure to scan parsing, but does not identify the second
line as commentary. No raw keys/stdout were collected. OS-release bootstrap fix
still lacks successful owner evidence; do not imply guest readiness.

Implemented scan-boundary-only ASCII comment/blank filtering with 64KiB bound;
still require exactly one complete Ed25519 key for exact host/port. Stored
known_hosts parser remains strict, no comment filtering on stored files; extra
keys, unknown lines, controls, wrong host/port, partial blobs, and changed keys
remain refused. Failed trust CLI emits only line/comment/record counts; SSH
propagates only an allowlisted counts format into progress labels. No key/raw
stderr output or host-check relaxation. Fixture first reproduced comment+key
rejection before fix. This handles the likely commentary case, not arbitrary
two-line scanner output, and is not yet an owner-verified SSH fix.

Next owner step: run documented `--archive-stopped-failed --apply && ./lab vm run`
from updated checkout. Archiver verifies stopped ownership before archiving the
entire disk/ISO/runtime/run evidence and recapturing source. Do not edit frozen
profiles, destroy prior evidence, touch parent listeners or rerun if active.
If it still waits, counts identify whether commentary or extra/unknown records
remain; use the scoped SSH continuation above rather than restarting the workflow.
Once SSH passes, inspect new cloud-init/harness milestones and actual console
failures. Any substantial remaining provisioning/metrics work belongs to the
implementation-model handoff; C14 still follows the established acceptance gates.
Local validation: process Python9/9 and process Bats13/13 PASS; changed Python
Ruff, SSH/runner ShellCheck and diff checks PASS. No VM operation performed here.

### Owner-confirmed SSH success; provisioning egress failure; reusable log report

Run19609672-f460-4ba6-8869-ab7683b6e10f verified guest SSH and entered
cloud-init readiness. Serial log showed successful base package installation,
then curl403 and terminal scripts_user failure around79s. This clears the earlier
OS-identity blocker but not bootstrap acceptance. Next source download is the
NodeSource signing key; owner confirmed checkout egress allowlist contains only
pypi.org,github.com,npmjs.com,ubuntu.com,deb.debian.org, excluding NodeSource.
NodeSource egress denial is the likely403 source, not a proven origin response.
No policy bypass or listener takeover is authorized/implemented by diagnostics.

Owner requested a reusable collating/redacting script instead of custom commands.
Extended vm-start-diagnostics.py with --logs, using vm-log-report.py: selected
latest-run host logs and fixed cloud-init status/output/log commands over existing
isolated SSH. Exact registry identity, profile disk argv binding and loopback
listener ownership gate guest collection; no host-key learning, arbitrary command
input or file exports. Current source collector can inspect the running frozen
profile without patching it. Stdout is bounded64KiB/deadline20s; cleanup signals
only the collector's private client process group, never VM/run services. Host
tails are private regular/nonlinked files, capped64KiB each and80 report lines.
No raw log files are saved. Public/private key lines/blocks/fingerprints,
credential-like lines, URLs and long identifiers are redacted; explicitly
best-effort and owner review remains required before sharing.

Use the one-command --logs invocation at the top of the recovery section in
docs/vm-c14-handoff-plan.md, even while launch is active. Do not recover/restart
the active VM to run diagnostics. Remaining implementation-model scope: reconcile
required bootstrap destinations with explicit egress policy without weakening
public/private boundaries; detect terminal cloud-init failure promptly; preserve
active/failed disks and frozen run evidence before any fresh retry. Do not assume
the current run stopped until inventory confirms it. Metrics remains separately
blocked by a present unverified listener and must not touch parent service state.

### Required package domains, cloud-init fail-fast, interrupted-state guard

Owner requested implementation and reports killing the launcher; subsequent list
still says provisioning. This is not stopped-process evidence. Added explicit
checkout-policy entries deb.nodesource.com/download.docker.com (source key/APT
URLs), registry.npmjs.org (npm and committed MCP lockfile artifacts), and
files.pythonhosted.org (pip wheel CDN). Existing allowed domains retained; no
blank allowlist, bypass, public/private-network relaxation or unrelated cloud auth
domains. This is checkout policy, not an implicit exception for arbitrary projects.
Ancestor policy can still independently deny; do not bypass it.

Cloud-init readiness now captures JSON even on nonzero status, detects terminal
error/disabled/degraded-done or done-with-errors, and exits immediately with a fixed
stage diagnostic and original nonzero code (1 if command itself exited0). Running,
unknown and malformed JSON do not trigger terminal classification. Marker is never
read after terminal failure. Added an actual lifecycle/SSH fixture returning exit2
and terminal JSON: only one SSH call, immediate exit2, no secret-error output.

Archive-stopped-failed now accepts interrupted starting/provisioning metadata under
the same all-stopped/run/identity/dead-owner/no-descendant checks. It does not signal
or reclassify an active guest. If guarded archive refuses, collect inventory/logs;
do not force kill/rewrite status to unblock it. Controlled interrupted-provisioning
shutdown is the next implementation-model task ONLY if current processes remain
live: reuse exact registry/SSH ownership, terminal-cloud-init confirmation and
child guard; require controller lock free/dead; send graceful guest poweroff only,
wait bounded for exact QEMU stopped, then stop owned sidecars, preserve disk/logs,
and refuse unknown identity or timeout without escalation. No broad pkill/down.
This has not been implemented or host-tested; do not substitute a custom force
command. The owner can first try the documented guarded archive/fresh launch;
it proceeds only if the interrupted launch truly left all processes stopped.
Validation: guest16/16, proxy37/37, recovery15/15 and lifecycle Bats28/28 PASS;
changed Python Ruff, guest/lifecycle ShellCheck and diff checks PASS. No owner VM
stop/restart, live package download or provisioning success is claimed here.

### Run4b967dd8 terminal failure and shutdown-wait defect

Owner's next run verified SSH then failed terminally after about173s, exited2
with cleanup-unverified. Follow-up inventory: profile unknown, exact QEMU and
virtiofsd/loggers stopped with exit0, proxy and proxy_logger still running.
Serial tail shows clean guest poweroff at188s, hiding the earlier bootstrap
failure. Guest SSH log collection is correctly unavailable after poweroff.

Found concrete cleanup defect: vm_lifecycle_wait_qemu_stopped called wait-role,
which waits for role registration and immediately reports an already-registered
running process. It did not wait for exit, so graceful poweroff was prematurely
classified as timeout and sidecar cleanup was skipped. Corrected future source
to poll exact identity until stopped, unknown, or bounded deadline; never signals
QEMU or treats missing/unknown as stopped. Regression invokes the real waiter
with a helper reporting running then stopped. Frozen failed profile is unchanged.
Collector now adds redacted error_excerpt across the full bounded64KiB tail,
so the final80 shutdown lines no longer hide earlier errors still in that window.
It cannot recover errors outside that bound or rotated files; do not infer their
absence. No retry or process operation executed on the owner's VM.

Immediate owner action: rerun the same --logs collector and inspect error_excerpt.
Current unknown profile with live proxy does not qualify for archival. After the
actual bootstrap cause is known, next implementation model must provide a narrow
explicit recovery for this known interrupted cleanup: verify exact stopped QEMU,
no descendants, dead controller/free locks; stop only exact run-owned proxy
supervisor through process.py identity checks; reverify every run stopped;
preserve unknown metadata/evidence and require explicit stopped-unknown archival
authorization rather than inventing an unknown->failed schema transition. Never
touch parent metrics listener or broad process groups. No blind relaunch.
Local checks: lifecycle Bats29/29 and log fixtures6/6 PASS; ShellCheck/Ruff PASS.

### Silent post-npm failure reproduced; guarded unknown-proxy recovery

Latest owner context shows npm added5packages immediately before scripts_user
failed, without a diagnostic. Read-only local `opencode --version` prints
`opencode v2.2.5`; the bootstrap regex required a non-alphanumeric immediately
before the numeric version and therefore rejected the CLI's v prefix. Corrected
known plain/v-prefixed/labelled formats while retaining exact saved-version
matching; malformed/mismatched output gets a fixed explicit error, never raw
version output. Updated in-place updater parsing for v prefix too. Fixture default
now matches actual CLI output; mismatch/no-secret and stage failure tests added.
Bootstrap prints fixed named stages across preflight/packages/Node/Python/Docker/
OpenCode/version/adapter/private build/readiness and ERR trap reports only fixed
stage plus exit status. No BASH_COMMAND/argv tracing. Actual guest success is still
unverified; this fixes a concrete reproducer consistent with the host symptom.

New explicit --archive-unknown-cleanup addresses current run4b967dd8 leftovers:
unknown metadata only, stopped recorded QEMU matched to run registry, no possible
descendants, all nonproxy roles stopped, only exact run-bound proxy/logger may
remain live. Requires dead/absent reservation owner and free allocation/profile
locks before signalling. Stops only run supervisors via process.py exact identity
checks, rechecks stopped roles and live references, archives whole disk/ISO/keys/
runtime/logs with unknown metadata unchanged, then releases dead reservation.
Read-only mode does not signal. No QEMU/parent/metrics signals, unknown->failed
schema changes, disk deletion or frozen-runtime edits. Synthetic real supervisor
fixture verifies proxy stops and overlay/evidence preserved; live nonproxy refuses.

Owner retry is now the top command in docs/vm-c14-handoff-plan.md. If it refuses,
stop and report; do not fall back to force-kill or a less restrictive archive mode.
If fresh bootstrap fails, use the existing --logs collector; named stages and
stage/exit trap should avoid further blind failure-context iterations. Remaining
substantial work belongs to implementation-model handoff under the existing
package-1 -> package-2 -> integrated acceptance -> C14 plan, not new old-ticket gates.
Validation: guest18/18, update15/15, recovery17/17 PASS; changed production Bash
ShellCheck, changed Python Ruff and diff checks PASS. Local CLI version/help-only
observation; no owner VM operation, provider call or provisioning success claimed.

### Orphan supervisor lock inheritance and unchanged version policy

Owner's unknown-cleanup attempt passed stopped-QEMU/proxy identity/dead-controller
preflight, then nonblocking lifecycle flock refused BlockingIOError before any
archive. Source analysis and real-process fixture reproduce long-lived run-logged
supervisors retaining the controller's inherited allocation/profile descriptors.
Child close_fds did not close the supervisor's own references; these outlived the
controller and kept lifecycle locks held.

New supervisor source verifies and closes only its inherited lifecycle FD copies,
clears stale VM_HELD metadata, and never LOCK_UNs the controller's shared flock.
Fixture proves controller remains locked while alive and both locks become
acquirable after it closes, even while supervisor/child continue running.
For the already-frozen orphan logger, unknown-cleanup now has a guarded fallback:
same stopped-QEMU/dead-controller/run-binding checks, examine kernel flock owners/
waiters plus verified proxy/supervisor FD holders, refuse any other live holder,
then retire only exact orphan proxy supervisors and acquire normal locks before
whole-profile archival. A live-controller-lock fixture still refuses. No lock
stealing, broad kill, QEMU signal, metadata rewrite or parent listener operation.
Unknown/missing or inaccessible relevant lock ownership fails closed. Unrelated
applications' private FD tables are not inspected.

Owner clarified that latest/in-VM upgrades are required, not a one-off pinned
release. No OpenCode release was hard-coded: default latest resolves once per
bootstrap transaction; explicit owner pins and in-VM updater remain supported.
The version-format fix accepts CLI display labels/v prefix while validating the
resolved transaction version. Warm starts do not update implicitly. Reuse the
same guarded unknown-cleanup/retry command; successful owner provisioning remains
unverified. Local checks: process Python10/10, recovery18/18, process Bats13/13 PASS.

### Run5d202308 private MCP build failure — parent ownership correction

Owner's guarded archive succeeded and preserved the prior profile at the printed
private sibling archive. Fresh run5d202308-f537-44ba-8487-4e68d8fa9515 passed SSH,
then failed terminally at about174s readiness elapsed. Updated collector identifies
`stage=private-payload-build exit=1` and npm ci EUSAGE/unusable lockfile. Post-run
profile failed, no runtime identity, all six recorded roles stopped with exit0;
guest collection unavailable after clean poweroff. These exit0s are cleanup
results, not successful provisioning. Metrics registry absent with a present
unverified listener remains separate and untouched.

Owner authorized planning then implementation, after a read-only diagnosis pause.
PR_REIMPLEMENTATION and the current C14 handoff control this work; no further
ticket calls or writes. Inspection found root's install -d creates the private
share_root as0700, but only the stage subtree was chowned to mester. The parent
therefore blocks the runuser npm process from reading its valid copied lockfile.
Real npm, in a dependency-free disposable fixture with offline/no-script/no-audit
options, reproduced the exact EUSAGE with an inaccessible parent (exit1) and
succeeded with parent access restored (exit0). No registry or VM operation.

Fix: nonrecursive chown of the one private parent to the selected guest UID/GID,
before staging/npm, keeping0700. Existing runtime subtrees are not recursively
reowned by this new operation. Latest-once/pins/in-VM updates, npm ci/lockfile,
egress and host boundaries remain unchanged. New production-path ownership-order
test audits chown and refuses the synthetic user switch unless that exact parent
was assigned first; it fails before the fix and passes after. Existing fixture
runuser never switched users, which had hidden this traversal defect.

Initial focused validation: guest/update/recovery/log Python59/59 PASS; changed
test Ruff, production bootstrap ShellCheck and Bash syntax PASS. Final milestone
checks recorded below. No assistant VM start/stop, host acceptance, source-version
pin, credential action, push/merge or C14 activation. Preserve all uncommitted work
and vm-migration-plan.md. Next owner handoff uses guarded archive-stopped-failed
then fresh launch, not the superseded archive-unknown-cleanup mode. Any refusal
stops; any new provisioning failure requires collector evidence before retry.

Final local milestone: `python3 -B -m unittest discover -s tests`365/365 PASS;
`bats tests/`149/149 PASS; `git diff --check` PASS. These include the inherited
uncommitted fixes and remain local fixture evidence, not owner VM acceptance.

### Run71ff52d4 ready; native attach and browser-cleanup failures

Owner archived the failed profile successfully at the printed private sibling
archive, then fresh run71ff52d4-d3d5-4394-a43d-440b1929f07c passed SSH and
provisioning, printing `Profile branch-dev-20261105 is ready`. This is owner
bootstrap-readiness evidence, not TUI/usability/metrics or integrated acceptance.
Native attach then failed `exec: opencode: not found`, and browser EXIT cleanup
raised `15: unbound variable`. Source confirms cleanup reads argument15 while
trap/callers pass14; that failure may leave session helpers and must not be
reported as successful cleanup. Root bootstrap's version check does not establish
guest-user executable visibility. Preserve ready profile and parent; no archive,
restart, credential operation or frozen-runtime edit is justified.

Extended the existing current-source read-only collector with fixed user/root
command lookup and bounded standard-bin namei metadata, placed after cloud-init
logs so last80 lines retain it. No OpenCode execution, config/auth read, unbounded
filesystem search or VM signals; existing verified SSH and20s/64KiB bounds remain.
Local log tests7/7, changed-file Ruff and git diff --check PASS. Next owner step is
only the same --logs collector; diagnose visibility before selecting a source fix
and a non-reprovisioning way to validate it. Current handoff supersedes older
archive-and-retry instructions. Tickets remain untouched.

### Confirmed global npm directory-mode defect; ready-VM repair handoff

Owner's collector verifies run71ff52d4 remains ready/running and cloud-init finished
with0failures; private MCP ci/build succeeded. Root resolves /usr/bin/opencode,
mester does not. namei proves its exact symlink reaches root0755 opencode.exe
through root0700 @opencode/cli/bin directories. Bootstrap-wide umask077 was
inherited by the global npm install. Source now scopes022 only inside that install
subprocess, retaining private077 afterward, and checks version through runuser
mester with the actual agent PATH before readiness. Shared install code also
serves update. No hard-coded OpenCode version, unlocked install or permission/
egress boundary changes. Existing frozen runtime/guest install are not patched by
these local edits.

Browser cleanup source fixes the separate missing argument15: its14-argument
callers carry bridge ports, and the session always uses mester@127.0.0.1; cleanup
now uses that same fixed endpoint. Regression under nounset reproduces old failure
and passes after fix. Global-install umask regression also went red before fix;
guest-user execution denial now blocks readiness, even when root could execute.

Added scripts/vm-agent-permissions.py as a bounded owner-operated correction for
the diagnosed ready guest, default audit/explicit --apply. Uses existing profile,
exact registry identity and QEMU-owned/pinned SSH transport checks; requires exact
run ID, ready/running state, diagnosed canonical guest paths/root ownership/modes
and symlink. Validates all first, then chmods only three public package directories
to0755 and checks --version as mester. No recursive chmod, package/file-content,
credential, frozen-runtime, metadata, VM or parent-service change. Six fixtures
cover audit, exact/idempotent apply/private-file preservation, unsafe-mode and
symlink refusals, nonready/unknown/stopped/wrong-run/foreign-listener no-SSH cases.
No owner repair or host acceptance has yet occurred.

Validation: full Python374/374 and Bats149/149 PASS; changed Python Ruff PASS;
bootstrap ShellCheck and Bash syntax PASS; repair --help and git diff --check PASS.
Full ShellCheck services.sh retains the same existing SC2016/SC2015/SC2034 findings,
confirmed by checking its HEAD Git-object contents; no new finding from cleanup
change. Next owner step is only the exact --apply handoff in the C14 plan. After
successful version confirmation, attach existing VM with --no-browser-auth for
usability, avoiding its frozen cleanup bug without replacing runtime. Prior auth
helpers may remain: no cleanup PASS is claimed, and exact-identity inspection is
still needed. Keep all profiles/archives, parent and metrics listener intact;
ordinary lab remains Docker, package1 usability and later gates still open.

### Ready-guest repair refusal — expose the failed predicate, do not relax it

Owner --apply returned the generic diagnosed-shape refusal, exit2. This comes
from pre-chmod validation, so no directory modes were changed. The exact
predicate is unknown; namei did not capture all checked metadata (including
executable link count). Replaced generic guest validation refusal with fixed
check labels and numeric mode/UID/link counts, never arbitrary paths/file bodies.
Existing conditions retained; explicit require checks also remain enforced with
Python optimization. New real hardlink fixture reproduces the refusal and
verifies fixed executable-links/links2 output with every directory unchanged;
unsafe-mode and symlink fixtures verify distinct labels too. Seven repair tests,
changed-file Ruff and git diff --check PASS. No VM/guest mutation performed here.
Next owner action is the exact same helper WITHOUT --apply (audit-only), then
return result and stop. Preserve ready VM; no blind apply/restart or weakened
checks. Current C14 handoff updated; no ticket calls.

### Ready-guest hardlink audit resolved

Owner audit reports executable-links mode0755 uid0 links2. Single-link refusal
was overconservative for this operation: it does not change binary contents or
inode permissions, only three known public ancestor directories. Removed only
the executable-link-count condition; all canonical path, exact symlink, regular
file/root-owner/mode and directory checks remain. Revised real-hardlink fixture
applies directory-only repair and verifies both links retain inode/mode/owner/
link count/mtime/content; private file remains untouched in existing coverage.
Seven repair tests, changed-file Ruff and git diff --check PASS. No owner apply
or VM mutation performed by this session; next owner action is exact --apply
then stop with correction/version output. Ready VM, overlay, frozen runtime,
parent state and metrics listener preserved; prior helper cleanup still not
claimed. Current handoff supersedes audit-only pause. Tickets untouched.

### Public package correction applied; guest XDG-parent write refusal

Owner --apply printed guest_opencode_directory_modes=corrected; the subsequent
mester --version invocation reached OpenCode startup, then failed mkdir for
/home/mester/.local/share/opencode/log (EACCES creating its opencode parent).
The three public directory-mode changes succeeded and are not rolled back;
guest version/TUI success remains unverified. Do not conflate new home-write
failure with executable visibility or rebuild the ready VM.

Expanded helper audit-only output with fixed home/.local/share/opencode/config/
cache path metadata (type, mode, UID, links, or absent). No enumeration/file
contents/auth inspection, mutation or recursive ownership operations. Eight
repair tests, Ruff and git diff --check PASS, including synthetic unread secret
and unchanged-parent-mode coverage. Next owner step: same exact helper/run ID
without --apply; return result and stop. Diagnose actual ownership before fixing
selected parents; preserve all runtime/credential data. No ticket calls or VM
operations by the assistant. Current handoff updated.

### Confirmed root-owned XDG parents — targeted owner correction

Owner audit identifies home uid1000 mode0750; .local, .local/share and .config
uid0 mode0755; cache uid1000 mode0700; .local/share/opencode absent. This is a
parent ownership defect, not existing auth-store permissions or another package
visibility failure. Bootstrap now validates all three XDG parents before the
guest-user OpenCode version check, creates missing directories0700, assigns only
the parents nonrecursively to selected guest UID/GID, and preserves existing
safe directory modes/contents. Symlink/unexpected-type/foreign-owner/mode refuses;
fixture mode keeps synthetic ownership handling without privileged operations.

Added explicit --repair-home (requires --apply) to the existing exact-run helper.
Validates account/home/three canonical parent directory owners and modes before
any package chmod or parent chown, then changes only those parent owners to the
guest mester account, leaving descendants and home/cache unchanged. Public
package directory-mode correction is idempotent. No recursive chown, auth data
read, reinstall, VM restart, parent process/service operation or frozen-runtime
edit. Owner command in C14 handoff returns only correction status plus --version;
stop on refusal or new error before attach. Current ready profile preserved.

Focused guest/update/repair48/48 PASS; full Python380/380 and Bats149/149 PASS.
Changed-file Ruff, bootstrap ShellCheck/Bash syntax and git diff --check PASS.
Coverage includes safe parent preparation, unsafe symlink no readiness/no target
writes, parent-mode preservation and unchanged synthetic private-data inode/stat/
contents, plus validation before the first mutation. No owner correction or live
version/TUI success claimed by these fixtures. Next: exact --apply --repair-home,
return result and stop, then --no-browser-auth attach only after version success.
All prior archives, ready VM, parent, metrics listener and mission file preserved;
tickets untouched. Package1 usability and later acceptance/C14 still pending.

### Owner-confirmed repaired guest executable; next native attach

Owner ran exact run71ff52d4 helper --apply --repair-home successfully: both
guest_home_parent_ownership=corrected and guest_opencode_directory_modes=corrected,
then opencode v2.2.5 as mester. This is live guest executable evidence; no restart,
reinstall or frozen-runtime patch. TUI/metrics/permission/model/workload/recursive
acceptance are not established by --version.
Next owner action is existing-profile `lab vm agent branch-dev-20261105` with
explicit ask permissions and --no-browser-auth, under the same dedicated state
root. Reuses frozen controller/profile and avoids its old browser-cleanup defect
for the usability observation. Owner observes rendering/input/errors, no login,
model selection or agent task yet. Metrics and potential prior auth-helper cleanup
remain open. Current C14 handoff updated; no assistant VM operation or ticket call.

### Owner reports native TUI opens; Escape works; feature gates remain open

Owner attached and reports TUI opens and Escape handling works. No metrics or
local models visible; no browser auth under the explicit --no-browser-auth
usability attach; remote-model request retries with ERR_PROXY_TUNNEL403. Record
only partial usability: no model completion, metrics, browser, actual permissions
or integrated acceptance PASS. Cancel retries rather than generate paid traffic.
Metrics forward was already unavailable/unverified and model provider remained
none pending discovery/owner selection. Checkout allowlist currently has package
domains, consistent with remote denial, but exact denied hostname/policy layer
still unknown. No allow-all, proxy bypass or parent service takeover.

Next owner action is existing standalone --discover-providers on the parent VM,
read-only fixed-loopback model-list queries, then return safe IDs/status and
select an exact available provider/model. No service startup, configuration or
VM/campaign operation authorized by discovery itself. Updated current C14 handoff;
all required feature/security/usability gates remain, browser capability not
removed, prior helper cleanup and metrics verification still open. No source
implementation change, ticket call or VM operation in this observation step.

## Owner-authorized implementation checkpoint and CI repair — 2026-11-06

Owner approved checkpointing existing work, fixing the reported GitHub Actions
failure, then detailed planning only and a pause before further implementation.
No push/merge/default cutover or live operational action is part of this phase.
Preserve/exclude vm-migration-plan.md, all existing profiles/archives, credentials,
the ready development VM, parent session and unverified metrics listener.

Reproduced `shellcheck lab scripts/*.sh`: SC1091 at dispatcher's explicit source
of lab-docker. CI now lists all three launchers: `shellcheck lab lab-docker lab-vm
scripts/*.sh`. Exact expanded command PASS without disabling SC1091, changing
severity or removing scripts. The workflow continues to run Node/Bats/hygiene/
ticket-store jobs unchanged otherwise; no ticket-store operation was run here.

Reviewed existing production and regression diffs plus new bounded recovery,
redacted diagnostics and guest-only permission-repair helpers. This checkpoint
captures earlier boot/ownership/seed/SSH/lock/readiness fixes and evidence, not
the approved but not-yet-implemented inference architecture.

Revalidated locally for this checkpoint:

| Command/check | Observed result |
|---|---|
| python3 -B -m unittest discover -s tests |380/380 PASS|
| bats tests/ |149/149 PASS|
| shellcheck lab lab-docker lab-vm scripts/*.sh |PASS, full default severity|
| ruff check scripts/*.py agent/metrics/gather.py bench/bench.py |PASS|
| scripts Python py_compile with external temporary cache; VM/test Python AST |PASS|
| npm run build in little-mester-mcp |PASS, existing local dependencies|
| npm run check:package |PASS|
| npm test -- --runInBand |3 suites/143 tests PASS, temporary ticket fixtures only|
| git diff --check |PASS|
| python3 -B scripts/check-docs-consistency.py |Same8 previously recorded README/DECISIONS drift/link failures; still required C14 closeout work, not a newly green docs gate|

No npm ci/download, registry/model request, real ticket read/write, VM operation,
provider/service mutation, credential action or active harness restart. The
untracked mission file remains SHA256
3d6ee2115423272b88a8efd8d4d33462318d5ae4c537841091e513ee5e2d1374 and is excluded
from staging. SHA of this implementation checkpoint is recorded by the subsequent
planning-only commit, which will define executable pre-C14 inference/performance
steps and an explicit pause for the owner to choose the implementation model.

## Approved inference/performance amendment and implementation-model pause

**Authority2026-11-06:** owner accepted the benchmark-first shared-inference
proposal and optional VM-local alternative, then requested CI repair/current-work
commit, very detailed planning capable of substep handoff to a less-capable model,
commit planning, pause/recommendations, later implementation/testing and eventual
merge only after required gates. Current-work checkpoint is
`ca6da6cd7c4889fb190d809086ca62cfb9488fd6`; nothing was pushed or merged.

**Planning deliverable:** docs/vm-inference-performance-plan.md (863 lines at
initial validation), normative companion to master plan. Thirteen INF-00–12 tasks
with dependency graph/C13c checkpoints, complete numbered substeps, scope/files,
deliverables/exit conditions, positive/negative test obligations, owner-live effects,
failure/preservation rules, review recommendations and explicit owner decision
register. Universal task-packet instructions prevent handing a model only a title.
Owner always chooses the model; R/I/H labels do not auto-switch or delegate.

**Approved engineering deltas captured:**
- R35–R38 and D11–D13 supplement original requirements/decisions: inference-only
  capability + backend isolation; comparable >=95% warmed one-level generation
  target; topology-aware discovery/per-hop relays; conditional VM-local cache/push.
- Shared model deployment uses selected accelerator outside agent VMs; no recursive
  weights/server copies, no host tools/MCP/shell execution by the model service.
  Raw TCP destination restriction is not sufficient inference isolation.
- Freeze actual protocol/schema/unsafe fields, transport library/dependency and
  numerical resource/timeout bounds before coding; explicit backend-isolation/
  deployment and hardware/service effects need owner disposition, not guesses.
- Measurement controls direct B vs gateway/G0 vs nesting; identical model/device/
  settings, warmups/paired samples, per-case ratios, actual token/TTFT/error/queue/
  cancel data and no transcripts. TTFT/concurrency budgets selected by owner from
  measured baseline before scoring; missing values are BLOCKED. No security waiver.
- Conditional VM-local F1–F8 defines checksum/license cache, parent-push VM-owned
  storage/persistence and explicit per-level resources; no host-cache mount, auto
  download/copy, automatic implementation or implicit GPU passthrough authority.
- Keep metrics/browser cleanup/permissions/public-egress403 and QCOW2/filesystem/
  synthetic credential procedure gaps explicit. Live OpenAI acceptance remains
  deferred; current optional features/regressions are not removed.
- INF-11 freezes/revalidates integrated evidence and exact cleanup; INF-12 preserves
  existing TASK-16/C14, requires8 docs failures closed, default/rollback proof,
  exact-head CI and explicit cutover/publication/final merge checkpoints.

Master plan/tasks/current progress, C14 handoff, host acceptance and architecture
pages now point at the controlling amendment and current PAUSE; earlier operational
commands are historical, not the next action. Product decisions contains a workflow
pointer rather than a competing engineering specification. Original plans/history
retained; no ticket calls/writes or source/runtime mutation during planning.

Validation: eight-document Markdown fences/local-link/conflict integrity PASS;
38 unique requirements;13 ordered INF task sections each with deliverable/exit;
8 conditional VM-local steps; preserved mission hash unchanged. git diff --check
PASS. Docs consistency retains exactly the8 known failures across109 checks after
new links, with no new failed check. Existing full380 Python/149 Bats/143 Node and
CI static/build results belong to checkpointca6da6c; not rerun or relabeled as new
host evidence for documentation-only changes. All new files remain proposed APIs
until implemented; no fabricated gateway/relay/benchmark result.

**STOP after planning commit.** Recommend owner select implementation model and
resume INF-01.1–01.5 local topology/discovery tests/code first. Recommend deeper
reasoning review at INF-02 serving isolation, INF-03 protocol/dependency/limits and
INF-08 performance interpretation; owner may instead choose any model or decide
directly. Do not request arbitrary service location/port choices before source/
read-only topology explains what is missing. Preserve the ready development VM,
parent/legacy state, all archives/auth and unverified metrics listener. No more
implementation, discovery, VM/service/model operation, push/merge or C14 now.
