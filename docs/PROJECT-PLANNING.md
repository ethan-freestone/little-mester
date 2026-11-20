# Project planning hierarchy

Little Mester organizes work around outcomes, using the shallowest useful form:

**Idea → Phase → Milestone → Submilestone → Tasks / Acceptance Tests**

Levels are optional; this is guidance for planning, not a required ticket schema.

## Levels

- **Idea** — the overall objective or problem. An idea may fit in one PR or span
  multiple phases.
- **Phase** — one coherent, independently reviewable and mergeable increment.
  **One phase maps to one PR.** Small ideas may omit phases and deliver their
  milestones in one PR. Larger ideas use Phase 1, Phase 2, and so on; milestone
  numbering resets within each phase. A phase is complete when its required
  milestones are accepted and its PR is merged.
- **Milestone** — a requirement or demonstrable outcome needed by its parent
  phase/PR (M1, M2, ...). A milestone is not a PR. Give it clear acceptance
  conditions and evidence appropriate to the outcome. It can be accepted before
  its phase is complete.
- **Submilestone (optional)** — an independently verifiable part of a larger
  milestone (M1a, M1b, M2a, ...). Submilestones belong to their parent and do not
  create PRs by themselves; use meaningful outcomes, not arbitrary work steps.
- **Tasks and acceptance tests** — implementation, investigation, testing, and
  review work that supplies evidence for a milestone. A task is not automatically
  a milestone, and a passing unit test does not automatically accept its parent.

## Scale the structure to the work

Start shallow. A small idea can have M1 and M2 directly under one PR. If scope
grows, introduce phases and distribute existing milestones without losing their
history. Split a complex milestone into submilestones only when that makes
acceptance clearer. Split an oversized phase into separate phases/PRs when one
PR is no longer a sensible reviewable increment. Omit any level that adds no
clarity; do not require elaborate upfront decomposition for small ideas.

## Evidence and status

Report progress and acceptance precisely. These are evidence distinctions, not
new ticket lifecycle states:

1. Planned
2. Implementation in progress
3. Implemented but not verified
4. Verified by local tests
5. Verified by hosted or integration tests
6. Accepted
7. Merged (for a PR/phase)

Record the evidence that supports each claim. CI proves only what its checks
exercise; it cannot establish owner-only behavior it does not test. Accept each
milestone against its own relevant criteria and evidence. The parent phase stays
incomplete until every mandatory milestone is accepted **and** its PR is merged.

## Example: secure persistent Little Mester VM

**Idea:** Secure persistent Little Mester VM

**Phase 1 — PR #17**

- **M1: Persistent VM lifecycle**
  - M1a: Normal lifecycle
  - M1b: Safe recovery/reconciliation
- **M2: Protected OpenCode experience**
  - A1: Real-KVM acceptance
  - B0: Shared reusable primitives
  - B1: Composable CI stages
  - Owner functional acceptance
- **M3: Live host metrics**
- **M4: Final integration and acceptance**

Here A1, B0, and B1 are work packages beneath M2, not separate PRs. This
illustrates the hierarchy; it does not require changing historical tickets or
forcing existing project work into a new schema.

## Future architectural intent — not implemented

Eventually, Little Mester should manage plans as structured, validated,
programmatically accessible records through modular MCP capabilities, rather
than treating Markdown semantics as the machine-readable source of truth. Plans
should have stable identities and explicit, potentially many-to-many links to
existing tickets: plans may span multiple tickets, and tickets may belong to
multiple plans. This hierarchy is the default planning strategy, not the only
strategy the future architecture must support. Markdown remains suitable for
documentation today. **This is future intent, not an implemented feature.**
