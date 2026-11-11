# Phase 1 continuous execution workflow

**Scope:** tickets #53–#57 and their approved Phase 1 work. This is the operating
workflow for those tickets; it preserves approved functional objectives, security
architecture, acceptance criteria and owner authority. The owner-approved trajectory
correction below prioritizes working integration and review efficiency, not weaker controls.

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
review receives an appropriately scoped delta review. Sol reviews assess whether code is
necessary and reachable, not only locally correct: report the acceptance behavior or verified
risk advanced, CLI/smoke/milestone reachability, duplication/complexity, security impact, code
growth by production/tests/docs, and one verdict: PASS, FIX, SIMPLIFY, DEFER, or BLOCKED.
SIMPLIFY requires actual justified consolidation while preserving trust-boundary checks; do
not answer it with more defensive scaffolding. Review integrated boundaries rather than every
small commit. Keep independent combined quality/security review for changed trust boundaries
and the final holistic review on #57.

## Phase 1 progression and stopping conditions

Progress toward these three demonstrations in order:

1. **M1 persistent parent:** protected `up` → validated `status` → interactive/reconnectable
   `ssh` → identity-bound `stop` → `up` reuses the same disk and workspace; ordinary smoke
   still deletes its ephemeral disk. `recover` initially remains a dry-run report. Keep real
   parent launch disabled until the owner validates qemu-img inherited-output-FD compatibility,
   QEMU fdset opening, single-writer locking, and explicit image-format handling on scratch
   artifacts. Default workspace is guest-disk-backed, with no implicit virtiofs export.
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
