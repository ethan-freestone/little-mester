# VM PR reimplementation — historical kickoff baseline

**Historical kickoff record. Superseded by `PR_REIMPLEMENTATION_PLAN.md`.**

This preserves the initial kickoff observations only. The current authority is
`PR_REIMPLEMENTATION_PLAN.md`; the execution record is
`PR_REIMPLEMENTATION_PROGRESS.md`. All incomplete statuses below describe the
historical kickoff, not the completed Round 1 package.

## 1. Mission and authority

Reconstruct the intended capabilities and security properties of the original VM
PR, challenge its architecture, and plan an independently implemented replacement
on `feat/vm-approach-2`, based on `main`. Preserve necessary behavior, not historical
implementation structure. Round 1 permits investigation and planning artifacts
only. No application changes, implementation commits, pushes, replacement PR, VM
operations, or automatic transition to Round 2.

The original branch must remain untouched. The existing runtime, credentials,
guest overlays, host-Docker session, and owner-host evidence must be preserved.
All ticket writes require the little-mester MCP tools; this investigation has
performed no ticket writes.

## 2. Verified source baseline

Observed during kickoff, 2026-11-03 (harness date):

| Item | Verified value |
|---|---|
| Repository | `ethan-freestone/little-mester` |
| Origin | `git@github.com:ethan-freestone/little-mester.git` |
| Original PR | https://github.com/ethan-freestone/little-mester/pull/13 |
| Live title/status | `feat: vm approach`; open, draft |
| PR base | `main`, `efe77202594c79dc1a926fa84a7a5dbc8866c66a` |
| PR head | `feat/vm-approach`, `97af1d83a16fce68ddcc3390276c218a1b0cd518` |
| Local main / replacement HEAD | `efe77202594c79dc1a926fa84a7a5dbc8866c66a` |
| Merge base | `efe77202594c79dc1a926fa84a7a5dbc8866c66a` |
| Effective diff | 100 files; 29,219 insertions, 910 deletions |
| Original PR body | Empty |
| Review submissions / inline comments / issue comments | Each live API collection returned `[]` |
| Replacement checkout | `/workspace`, branch `feat/vm-approach-2` |
| Inspection checkout | `/tmp/vm-migration-source`, detached at original PR head |
| Pre-existing user changes | `AM vm-migration-plan.md`; preserve index and working file |

Live discovery used GitHub's public REST API because `gh` is unavailable:

- `/repos/ethan-freestone/little-mester/pulls?state=all&head=ethan-freestone%3Afeat%2Fvm-approach`
- `/repos/ethan-freestone/little-mester/pulls/13/reviews?per_page=100`
- `/repos/ethan-freestone/little-mester/pulls/13/comments?per_page=100`
- `/repos/ethan-freestone/little-mester/issues/13/comments?per_page=100`

No API failure was treated as an empty discussion. These three successful empty
collections require no pagination at this baseline. Recheck live revisions and
discussion before completing the plan and again at Round 2 preflight.

Planning files live in the replacement checkout, not on the original branch.
The detached inspection checkout is reproducible and is not the durable artifact
location. `/tmp/opencode` was not writable; source inspection therefore uses the
fallback `/tmp/vm-migration-source`. No permission changes were attempted.

## 3. Evidence hierarchy and investigation rules

1. User mission and explicit owner decisions establish authority.
2. Live PR, exact Git revisions, actual source/tests, and recorded owner evidence
   establish what exists and what has been observed.
3. Ticket acceptance criteria and the newest superseding handoffs establish
   unresolved scope and operational restrictions; old comments are not current
   procedures.
4. Architectural prose is a claim to verify, not proof of implemented behavior.
5. Historical summaries, collations, mocks, and health checks do not replace source
   review, VM acceptance, actual model completions, or human OAuth/TUI evidence.

Use MCP ticket reads with
`ticket_dir=/tmp/vm-migration-source/docs/tickets` when reconstructing this source
revision. The replacement branch's older ticket store is not equivalent. An
initial default-store read failed for #50; it was not interpreted as absence of
that requirement in the original PR. Do not copy the original audit history into
the replacement as an implementation shortcut.

## 4. Initial capability inventory — not yet a complete specification

These groups are confirmed investigation subjects. Individual requirement IDs,
acceptance criteria, confidence scores, and complete evidence links remain to be
reconstructed before architecture selection.

| Group | Evidence already located | Essential distinction |
|---|---|---|
| One-command launch and warm reuse | #35, #41, #52; `lab`, `lab-vm`, `lib/vm/lifecycle.sh` | Current `lab` still defaults to Docker; VM-first is gated, not completed |
| Native guest harness and workloads | #35 A2/A12/A17; VM architecture §§1,4,6 | Agent runs directly in guest; guest Docker serves project workloads, not agent isolation |
| Local/cloud providers | #45; `lib/vm/host-services.sh` | Host model API is optional; VM launch must not inherently require host Docker |
| Credentials and native OAuth | #45; lifecycle auth helpers; browser bridge | Native login, callback cleanup, persistence, escrow, and recursive secret forwarding are distinct requirements |
| Permission modes | #50; `tests/lab-permissions.bats` | Correct argv is not proof of actual UI/allow/deny behavior; `--yes` differs from agent auto mode |
| Workspace and private state | `lab-vm:109–149`; `lib/state.sh`; recursion state-boundary guards | Shared workspace is intentional authority; keys/disks/control state must remain outside it |
| Mediated public HTTP(S) | #43/#45; `lib/vm/proxy.py`; architecture §§2–3 | Public-only egress is not unrestricted networking; deny private, mixed DNS, host-local and directly connected destinations |
| Lifecycle and recovery | #41/#52; lifecycle suspend/resume/resize/down | Retain overlay on daily shutdown; destructive operations require explicit scope and secret/evidence protection |
| Recursive self-testing | #41/#43/#52; `lib/vm/recursion.sh` | Bounded child VM testing is separate from running several agents in one VM |
| Snapshots and sharing | #52/#55; `scripts/vm-recursion-workspace.py` | Safe source snapshots differ from the future structured sharing design; no child-request MCP sharing path |
| Resource limits | #41/#52; `scripts/vm_memory_budget.py`; runner memory functions | Preflight before side effects; account for cgroups, reserve, and reclaimable managed allocation |
| Metrics and terminal UI | #41 A25/A26; #42; metrics/plugin changes | Metrics can fail without blocking agent launch; dialogs and repeated streaming need actual runtime evidence |
| Tests and support contract | CI workflows; VM architecture §6; host acceptance runner | Fast tests do not establish supported host profiles or nested KVM behavior |
| Concurrent agents/worktrees | #56; architecture sessions matrix | Supervisor ownership/retries/integration are not implemented merely because concurrent TUIs can open |
| Adjacent proposals | #49/#53/#54/#55/#57 | Do not silently absorb documentation MCP, usage counters, readiness engine work, or future sharing research into VM scope |

## 5. Security invariants to reconstruct and verify

The following are initial invariants supported by the original architecture and
ticket criteria, not claims that the replacement or original implementation has
passed all relevant tests:

- Validate canonical workspace paths and ownership before export. Refuse root,
  home, sensitive directories, private VM state overlap, and unauthorized
  repository exposure.
- Guest agent has broad authority inside the guest and over the selected share;
  guest root/Docker is not another host security boundary.
- QEMU restricted user networking, loopback-only host control listeners, and
  explicitly configured narrow crossings. No host Docker socket, broad host
  credential share, raw host/LAN route, firewall/NAT/sysctl changes, or implicit
  privilege escalation to make tests pass.
- Validate all destination DNS answers and connect to the validated address.
  Preserve IPv4-embedded IPv6 handling, mixed-answer refusal, host-route denial,
  and fail-closed network introspection.
- Every child VM independently repeats isolation and policy; proxy chaining has
  no direct-origin fallback. No implicit ancestor model/auth/control endpoint.
- Credentials stay outside workspaces, source, images, seed ISOs, and logs.
  Browser opening is a constrained capability, not arbitrary host execution.
  Export failure must preserve the last usable guest/escrow state.
- Recursive testing requires explicit opt-in, non-increasing inherited depth,
  independent state, bounded snapshots, resource checks, and explicit cleanup.
- Ordinary parallel agents do not acquire additional VM authority implicitly.
- Failure preserves actionable diagnostics without exposing tokens or OAuth
  URLs; unknown process ownership must not lead to raw PID termination.

Each retained invariant must acquire both an automated negative test and the
appropriate real-host evidence requirement in the completed traceability matrix.

## 6. Initial history leads and adversarial questions

These are review leads, not final findings or an approved design:

- History moved from agent-in-guest-container provisioning to direct guest agent
  execution (`d7888b6`). Determine which image-build diagnostics are obsolete and
  which remain useful to the independent Docker fallback.
- `lab-vm` sources cloud-init/disk/SSH/runner directly and also sources lifecycle,
  which sources those same libraries plus recursion. Determine whether repeated
  sourcing is harmless, required, or obscures initialization ownership.
- `lib/vm/lifecycle.sh` spans credentials, bootstrap, profile listing, resize,
  browser auth, proxy reload, isolation inspection, metrics, and teardown.
  Evaluate cohesive boundaries against a simpler alternative; file size alone
  is not a valid finding.
- `scripts/vm-host-acceptance.py` is 4,219 lines, with 2,051 lines of associated
  tests. Investigate how much is product acceptance versus accumulated repair,
  source overlay, resume, and operator-handoff machinery. Preserve meaningful
  security checks even if runner orchestration changes.
- OAuth architecture describes source-inspected OpenCode 2.2.0 behavior, whereas
  lifecycle installation defaults to `latest` (`lifecycle.sh:247–253`) and ticket
  #52 records 2.2.5. Resolve pinning and compatibility verification explicitly;
  do not assume URL/callback internals remain stable.
- Root-owned recursion identity is not a tamper-proof boundary against an agent
  with guest admin-equivalent control. State precisely whether the depth cap is
  cooperative workflow enforcement, resource containment, or an adversarial
  guarantee; do not make a stronger claim than the design supports.
- Separate model API probing from starting/recovering host model services.
  Determine which behavior is essential versus convenience, and preserve owner
  authority over service changes.

## 7. Current host gate and evidence limitations

The newest #41 `tests.needed` entry supersedes the earlier fresh-outer request.
An attempted fresh outer unexpectedly reused `launcher-smoke` and began package
bootstrap; Ctrl-C/exit 130 left completion unknown and no child started. Commit
`ceb7a3a` targets exact outer-profile selection, but its correction is not live
host acceptance. Before another host action, private report/state review must
resolve interrupted apt activity. The stable snapshot is actively exported;
do not stop, clean, mutate, choose another snapshot, or retry without a new owner
handoff. No campaign/downstream release gate is authorized by this planning task.

The current shell has readable/writable `/dev/kvm` and QEMU tools, contrary to
older injected environment notes. `virtiofsd` was not found on PATH. Neither
observation proves full nested capability or grants authority over owner profiles.
No VM, Docker service, model, credential, or package operation was performed.

## 8. Remaining Round 1 deliverables

Complete these in dependency order; these are investigation work packages, NOT
the mandatory Round 2 implementation task specifications:

1. Finish reading #40/#42/#43/#44/#48/#49 and relevant decisions, source and tests;
   inventory every changed path and group effective capabilities.
2. Inspect meaningful historical diffs/reverts, distinguishing abandoned agent
   container/bootstrap/network work from current responsibilities.
3. Produce uniquely identified requirements with source, explicit/inferred status,
   class, priority, observable behavior, constraints, acceptance, verification,
   and confidence. Resolve ordinary agent spawning versus nested self-testing.
4. Produce evidence-backed adversarial findings and KEEP/SIMPLIFY/REPLACE/REMOVE/
   DEFER/CLARIFY dispositions, including risks and counterarguments.
5. Compare architecture alternatives against the verified `main` baseline and
   specify the smallest complete recommendation, interfaces, state machine,
   configuration, security model, errors, cleanup and recovery.
6. Produce self-contained dependency-ordered TASK documents with every mandatory
   field in mission §7.2. Confirm files/commands; mark unexecuted commands
   provisional. Map every requirement and invariant to tests and commits.
7. Specify coherent validated commit checkpoints, fast CI, real VM integration,
   recursive authorized-task tests, negative security tests, failure recovery,
   human OAuth/TUI evidence, budgets, timeouts, and scoped cleanup.
8. Add risk register, behavior-change approval list, complete equivalence and
   traceability matrices, confidence assessment and completion criteria.
9. Adversarially review the plan for completeness, hidden design decisions,
   unnecessary complexity, delegation readiness and justified scope.
10. Present the completed executive summary and explicit Round 2 approval question.

## 9. Verified tooling contracts, not executed validation

`main` CI and MCP `package.json` establish these command names:

- MCP directory: `npm ci`, `npm run build`, `npm run check:package`, `npm test`.
- Repository: `bats tests/`; `shellcheck lab scripts/*.sh`;
  `python3 -m py_compile scripts/*.py`.
- Original PR VM CI adds `bash scripts/check-vm-cloud-init-schema.sh`,
  `./lab-vm test`, `bats tests/vm.bats`, and
  `python3 -m unittest discover -s tests -p 'test_vm_*.py' -v`.

These commands were **located in source, not executed in this kickoff**. Audit
their side effects and required dependencies before running them in Round 1.
In particular, `run-tests.sh` invokes `./lab exec` and is not a read-only local
test entry point. Do not run it against the active runtime as an exploratory step.
Ticket validation must use MCP if needed; do not repair ticket files directly.

## 10. Approval and completion state

- Round 1 investigation: **IN PROGRESS**.
- Requirements inventory and security reconstruction: **INCOMPLETE**.
- Architecture selection: **NOT MADE**.
- Implementation task count / commit count: **NOT YET DETERMINED**.
- Plan traceability and adversarial self-review: **NOT COMPLETE**.
- Round 2 approval: **NOT REQUESTED / NOT GRANTED**.

Do not implement from this preliminary document. The approval checkpoint is only
appropriate after all required Round 1 deliverables above are complete.
