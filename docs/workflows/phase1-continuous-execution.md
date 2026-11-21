# Phase 1 continuous execution workflow

**Scope:** tickets #53–#57 and their approved Phase 1 work. For PR #20 and subsequent
development, the owner-mandated process below also applies alongside the ticket-specific
workflow; it preserves approved functional objectives, security architecture, acceptance
criteria and owner authority. The owner-approved trajectory correction below prioritizes
working integration and review efficiency, not weaker controls.

## Owner-mandated PR #20 and follow-on process

- **Luna High is the orchestrator.** Luna coordinates, makes architectural decisions,
  integrates and reports. Delegate mechanical implementation, investigation, evidence
  gathering and test execution to appropriate subagents; preserve Sol's context and compute
  for independent review and genuinely difficult reasoning. Do not have Luna perform every
  task directly.
- **Sol is the independent design and code reviewer, not an investigator or worker.** Do not
  use Sol for repository searches, routine source inspection, CI/log/Git retrieval, test or
  build execution, edits, or ticket administration. Delegate those evidence-gathering and
  mechanical tasks to cheaper agents. Luna prepares concise, evidence-backed review packets
  with paths/lines and relevant excerpts, problem facts, proposed approach and alternatives,
  affected production behavior/security invariants, regression risks, and test strategy. Sol
  may challenge assumptions and request additional evidence; delegate that evidence gathering
  and return it to Sol rather than spending Sol's compute on retrieval.
- Track every sub-phase through the ticket engine, using an existing ticket or a suitable
  subticket/comment. Use MCP for all ticket fields, criteria, test-ledger changes and other
  mutations. While the known MCP comment-write gap remains, the ticket CLI may append comments
  only under the temporary owner authorization below. Never edit ticket JSON directly. Keep
  the ticket, documentation, implementation and evidence synchronized. Ticket #63 owns future
  programmatic enforcement; this manual process does not expand PR #20 into that work.
- Follow **documentation → tests → implementation → commit**. Make coherent, appropriately
  scoped commits throughout development; do not accumulate a whole implementation before
  committing. Run the relevant local checks and ticket validation, and do not hand off with
  unexplained uncommitted changes.
- **Mandatory pre-implementation design review:** track the sub-phase; delegate investigation
  and evidence gathering; Luna proposes the technical approach and regression strategy; Sol
  independently reviews that proposal before implementation. Resolve blocking design findings
  first. A material deviation from an approved design requires renewed Sol review before the
  deviating implementation. Do not request redundant design reviews for mechanical steps
  already covered by one approved approach; group related decisions into a coherent packet.
  Then follow documentation → tests → code → verification → commit. Design verdicts are
  APPROVE, FIX, BLOCK, or NEEDS EVIDENCE; if evidence is requested, gather it through a
  delegated investigator before asking Sol to reconsider.
- **Every commit gets an independent Sol review of that exact commit SHA**, considering its
  own diff and cumulative implications for PR #20, architecture, security and the complete
  acceptance scope. A review of an uncommitted diff is not commit review. Address blocking
  findings in a corrective commit and independently review that exact corrective SHA. Do not
  create review-only commits merely to record approval; the final handoff may record approval
  of the final evidence commit.
- Local tests, mocks, transient guests and code review are not production integration or owner
  acceptance. Do not push, merge, deploy, operate an owner VM, or expand security policy
  without owner authorization. Record remaining gates and exact authorization needed at each
  handoff. Programmatic enforcement remains separate under #63.

## Roles and execution loop

- **Luna High** owns orchestration, implementation, integration, source commits, ticket
  updates, and truthful reporting. It continues across approved tickets and implementation
  slices instead of stopping after a successful test, commit, review, or routine ticket
  update.
- **Luna Low/Explore and other delegated agents** gather targeted source, test, CI and log
  evidence and execute mechanical checks. They do not make implementation or security
  decisions.
- **Sol** reviews the pre-implementation design and independently reviews every exact commit
  under the owner-mandated process above, including correctness, maintainability, reuse,
  complexity, full PR scope and security boundaries. Sol is not assigned implementation,
  investigation, evidence retrieval, test execution, or ticket work. Actionable findings are
  resolved in scope and corrective commits receive a fresh review.

For each coherent block: record the ticket/sub-phase; delegate investigation and evidence
gathering; prepare the proposal and regression plan; obtain Sol's pre-implementation design
verdict; resolve blockers; then update docs/ticket context, add regression tests, implement,
and run focused REAL_PROCESS/mock/regression checks. Commit tested source and obtain exact-SHA
Sol review; correct blockers in a newly reviewed commit; then synchronize factual ticket
evidence through MCP. Commit ticket evidence with the relevant sub-phase; avoid review-only
churn. Keep test ledgers concise and preserve pending criteria until their stated evidence
exists.

**Temporary owner authorization (2026-11-15):** until the MCP comment-write gap is fixed,
the repository ticket CLI's documented `update <number> --comment <text>` operation may
append comments only. Inspect the current ticket first and never replace historical comments.
This exception does not authorize CLI edits to fields, criteria, test ledgers, audit history or
other ticket data; those mutations must use MCP.

Sol reviews assess whether code is necessary and reachable, not only locally correct: report
the acceptance behavior or verified risk advanced, CLI/smoke/milestone reachability,
duplication/complexity, security impact, code growth by production/tests/docs, cumulative PR
acceptance/security implications, and one verdict: PASS, FIX, SIMPLIFY, DEFER, or BLOCKED.
SIMPLIFY requires actual justified consolidation while preserving trust-boundary checks; do
not answer it with more defensive scaffolding. Every commit is independently reviewed; the
final holistic review on #57 remains an additional gate, not a substitute.

## Phase 1 progression and stopping conditions

**Continuous-execution correction (owner-approved 2026-11-11):** M1→M2→M3 is a
priority order, not a prohibition on independent later-milestone work. When an owner-only
runtime gate blocks an assumption, name the exact dependent operations and pause only those.
Continue useful authorized implementation elsewhere in #54–#56; do not stop at a gate,
review, commit, or ticket update. Do not invent a new architecture to route around a gate.
For M1, unverified qemu-img inherited-FD/QEMU fdset/locking behavior blocks only enabling or
claiming real parent disk launch and owner VM acceptance; it does not block offline lifecycle,
status/readiness, shutdown, SSH, guest config, or tests that preserve the launch-disabled gate.
For #55, reuse settled 2.2.5 probes and continue protected configuration/tmux wiring and
offline tests without claiming live guest behavior. For #56, continue the already approved
schema, scheduler, collector, restricted receiver/ACK, stale snapshot and display work with
bounded offline tests; hardware timing/accuracy remains pending the owner NVIDIA probe.
Return to the owner only after independent safe work is exhausted, an actual owner decision is
needed, or a batched host acceptance action is ready. Every handoff must say what the gate
blocks, what independent work was completed or remains, and the exact action that unlocks it.

Progress toward these three demonstrations in order:

1. **M1 persistent parent:** protected `up` → validated `status` → interactive/reconnectable
   `ssh` → identity-bound `stop` → `up` reuses the same disk and workspace; ordinary smoke
    still deletes its ephemeral disk. `recover` initially remains a dry-run report. The owner
    reported all four qemu-img/QEMU scratch compatibility checks PASS ×2 for source `37f5fb6`;
    see the [runtime-gate record](../parent-runtime-gates.md). This unblocks implementing the
    launch path, not lifecycle acceptance: only the separate owner-run
    [parent acceptance procedure](../parent-lifecycle-acceptance.md) can establish the first
    real up/status/ssh/stop/re-up demonstration.
   Default workspace is guest-disk-backed, with no implicit virtiofs export.
   Confirm whether virtiofsd serves any other parent-launch requirement before omitting it.
2. **M2 real OpenCode:** after M1 integration, provision pinned 2.2.5 and protected assets,
   support SSH/TUI and tmux reattachment, and prove configuration, credentials and session
   history survive ordinary stop/up. Provisioning must not erase credentials. Reuse existing
   compatibility probes; do not repeat settled investigations without contradictory live
   guest evidence. New egress requires owner approval.
3. **M3 live metrics:** integrate the approved schema/collector through restricted per-boot
   SSH push/ACK, receiver, tmpfs snapshot and 1.5-second freshness behavior. Request an
   owner-operated NVIDIA collection/timing probe before selecting the GPU method; supported
   NVIDIA utilization and VRAM remain required. Sampler failure must not stop the VM. Reuse
   existing collectors/schema; add no compatibility layer without demonstrated need.

### M2 CI delivery gates: A1 → B0 → B1

Keep these gates ordered; do not begin B0/B1 implementation while A1 is failing.

- **A1:** the complete hosted real-KVM integration, including protected OpenCode TUI,
  restricted reboot/egress, persistence and safe cleanup, is green on the exact candidate SHA.
  Local tests and source review do not substitute for this evidence.
- **B0 (mandatory after A1):** compare smoke, hosted CI and persistent-parent implementations;
  consolidate each safe overlap into independently executable, offline-testable primitives for
  provisioning/directory validation, process identity/signalling/shutdown, PTY/tmux, bounded
  diagnostics and result reporting. For every identified overlap, record that it is consolidated,
  intentionally separate with its security rationale, or deferred with its dependency. Preserve
  smoke and the full hosted real-KVM baseline. Do not wrap duplicate implementations, create a
  general framework, or split files while retaining hidden shared state. Sol Medium reviews the
  overlap inventory and closure evidence before B0 is complete.
- **B1:** expose genuinely separate GitHub Actions steps while retaining one safely owned
  disposable guest, identity-verified state transitions and bounded cleanup. Reuse B0 operations;
  do not add a competing supervisor or weaken process/disk ownership across steps.

The hosted-KVM workflow invokes each substantive stage in the foreground and keeps only the
minimal QEMU lifecycle owner detached between steps. The owner closes inherited step pipes and
preserves `RUNNER_TRACKING_ID`; the unconditional cleanup step requests identity-checked stop,
reap and private-state removal. Stage failure or ordinary cancellation also attempts bounded
cleanup. This cannot guarantee cleanup after runner/VM hard termination, host power loss, or
forced process kill that prevents both the stage trap and the Actions `always()` step from
running. In that case the runner itself is expected to be discarded; no cross-run recovery or
claim of host-level orphan prevention is made.

Before more lifecycle helpers, connect existing provisioning, process identity, supervisor,
gate, SSH and smoke cleanup primitives to supported subcommands. At M1 identify every
remaining unreachable product function and wire it into the immediate path, explicitly defer
it, or remove it if unjustified. Prefer real unprivileged process/lock/signal/identity tests
for lifecycle behavior; mocks do not establish VM behavior. Never run a real VM, SSH listener,
or owner-host QEMU/qemu-img gate without explicit owner authority.

Stop only for a genuine owner decision/authorization, an unresolved security or architecture
boundary outside the approved scope, an unresolvable blocking review finding, required
owner-host acceptance, or final merge readiness. Batch related owner-host checks into one
coherent request. Do not autonomously perform owner-host QEMU/VM actions, privileged host
operations, owner authentication, legacy VM changes, network-authority expansion, push,
merge, or cutover.

The owner-only runtime and security gates are defined by the relevant ticket criteria and
test ledgers, [the Phase 1 smoke/owner procedure](../vm-v3-phase1-smoke.md), and the
authoritative subsystem decisions in
[`docs/tickets/architecture/DECISIONS.md`](../tickets/architecture/DECISIONS.md).
Follow those sources rather than copying or weakening their procedures here. No parent
launch is enabled before its required owner-host QEMU fdset and single-writer validation.

## Reviews, commits, and ticket closure

- Keep Git provenance accurate: implementation and milestone evidence commits should be
  scoped and describe incomplete work honestly. Avoid review-only commits and repeated prose;
  consolidate ticket evidence at meaningful milestones. Inspect the full diff and working
  tree before each commit; leave clean worktrees at handoff boundaries where possible.
- Commit meaningful tested checkpoints; do not wait for a whole ticket to finish.
- Record concise tests, source SHAs, Sol verdicts and acceptance progress on the relevant
  ticket. Test ledgers contain actual tests and actionable test gaps; discussion belongs in
  append-only comments. Do not mark an end-to-end criterion passing without its demonstration.
  Split a criterion once through MCP only if it truly bundles independently verifiable
  behaviors; never split merely to manufacture progress. Validate the ticket store after
  ticket operations. Normal mutations use MCP; the temporary CLI exception is only for
  appending handoff comments.
- When a ticket is complete, reconcile every criterion with evidence, record legitimate
  deferrals in the appropriate existing successor/backlog ticket, resolve/supersede
  obsolete requirements, and transition the ticket through MCP. Then continue to the next
  approved dependent ticket. Do not close a ticket whose owner-host acceptance is pending.
- #57 requires a final holistic Sol review and owner-host acceptance preparation. Only the
  owner authorizes the Git merge.

## After Phase 1

After Phase 1 merges, begin Phase 1.5 with a bounded ticket/backlog/roadmap review—not
immediate local-model implementation. Reconcile completed and deferred work, identify
duplicates/obsolete/oversized tickets, and propose the Phase 1.5 scope for owner agreement
where material roadmap changes are needed. Only then implement and validate the agreed
local-model development cutover. Keep later multi-VM/orchestration scope in its existing
roadmap until that phase approaches. This roadmap does not authorize future-phase work.

This workflow is currently maintained as a repository document referenced by tickets.
Future work should migrate reusable workflow definitions and ticket associations into the
MCP workflow engine.
