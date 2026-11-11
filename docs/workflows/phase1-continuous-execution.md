# Phase 1 continuous execution workflow

**Scope:** tickets #53–#57 and their approved Phase 1 work. This is the operating
workflow for those tickets; it does not change their functional objectives, acceptance
criteria, architecture, or owner authority.

## Roles and execution loop

- **Luna High** owns orchestration, implementation, integration, source commits, ticket
  updates, and truthful reporting. It continues across approved tickets and implementation
  slices instead of stopping after a successful test, commit, review, or routine ticket
  update.
- **Luna Low/Explore** gathers targeted, read-only source and test evidence when discovery
  is useful. It does not make implementation or security decisions.
- **Sol Medium** supplies a focused technical plan when a material design choice warrants
  one and independently reviews meaningful correctness, maintainability, reuse,
  complexity, and security boundaries. Sol's PASS/JUSTIFIED verdicts mean continue;
  actionable SIMPLIFY findings are resolved within scope. A BLOCKED finding is assessed
  for a safe in-scope correction before escalation.

For each coherent block: identify its ticket requirements and existing reuse points, make
the smallest implementation, run focused REAL_PROCESS/mock/regression tests appropriate to
the claims, commit tested source, obtain any required independent review, resolve actionable
findings, and update the ticket through MCP. Commit ticket evidence separately when useful.
The ticket CLI is reserved for appending comments/handoff discussion; use MCP for ordinary
ticket fields and criteria. Keep test ledgers factual and concise; record review discussion
as ticket comments. Preserve pending criteria until their stated evidence exists.

At a security-sensitive boundary, review the exact source SHA. A material change after
review receives an appropriately scoped delta review. Meaningful Sol reviews report code
growth by production/tests/docs, reuse versus duplication, specific simplification targets,
complexity verdict, and security impact. Do not reduce security checks or tests solely to
reduce line count.

## Phase 1 progression and stopping conditions

Progress in dependency order: #53 evidence/supersession, #54 persistent parent lifecycle,
#55 pinned OpenCode 2.2.5 TUI/session behavior, #56 approved host-push metrics, then #57
integrated acceptance/merge readiness. Continue safe independent implementation when an
owner-only runtime gate blocks one operation; do not infer runtime success from local
tests, mocks, argv inspection, or source review.

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

- Keep Git provenance accurate: implementation, consolidation, and ticket-evidence commits
  should be scoped and describe incomplete work honestly. Inspect the full diff and working
  tree before each commit; leave clean worktrees at handoff boundaries where possible.
- Commit meaningful tested checkpoints; do not wait for a whole ticket to finish.
- Record tests, source SHAs, and review results on the relevant ticket. Do not mark an
  acceptance criterion passing without its required evidence. Validate the ticket store
  after ticket operations.
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
