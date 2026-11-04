# VM PR reimplementation progress

## Control state

- Phase: **Round 2 active; C01–C09 implementation committed; C10 TASK-11/12 fixture milestone complete; real acceptance remains pending**.
- Authority: user requested reading and kickoff of `vm-migration-plan.md`.
- Architecture/execution approval: **granted 2026-11-03**, “Yes, please begin round 2”.
- Authoritative plan: `PR_REIMPLEMENTATION_PLAN.md`.
- Implementing branch: `feat/vm-approach-2`; C05 implementation checkpoint is
  `cb7091a17720b59056bd2677f3af1aba5699f944`.
- C06 implementation commit: `de5150ed80789583db7dc483422ac918df4d60ec`, on
  `feat/vm-approach-2`, based at `5ecd200d32ba909e41d4a15885f57681a32f60d6`;
  the SHA ledger closeout is being committed separately.
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

The owner resumed Round 2 after the model swap. TASK-01/02 through TASK-05 are
committed at C01–C04. TASK-06/C05 is fixture-validated and committed. Later tasks
retain their ordered preconditions. No live
VM/network acceptance has occurred.

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
| TASK-11 | C10 | DONE (FIXTURE MILESTONE) | Coordinator/2026-11-04/2026-11-04 | Filtered Git-HEAD-bound snapshots; exclusions, race/capacity checks, synthetic Git and retained-snapshot verification; snapshot Python fixtures pass | Fixture-only; commit SHA closeout follows; no live VM or host acceptance |
| TASK-12 | C10 | DONE (FIXTURE MILESTONE) | Coordinator/2026-11-04/2026-11-04 | Persisted identity/depth guards, KVM/CPU preflight, isolated candidate state/argv, immediate-parent proxy depth and exact descendant-safe cleanup; 5 recursion Python cases, 6 recursion Bats cases; full 214 VM Python and 129 Bats pass | Public `run`/`up` remain undispatched until TASK-16; real nested KVM/VM and host/human acceptance remain pending |
| TASK-13 | C11 | NOT STARTED | Unassigned/—/— | — | Real nested hardware gate remains |
| TASK-14 | C12 | NOT STARTED | Unassigned/—/— | — | Depends on prior implementation |
| TASK-15 | C13 | NOT STARTED | Unassigned/—/— | — | Exact fresh-host handoff still required |
| TASK-16 | C14 | NOT STARTED | Unassigned/—/— | — | All live gates before cutover |

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

**State:** implementation and fixture validation complete on `feat/vm-approach-2`;
local C10 code checkpoint is being committed. Coordinator execution on 2026-11-04.
No push or PR update was performed.

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
