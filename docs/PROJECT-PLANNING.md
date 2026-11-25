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

## Little Mester VM and development roadmap

This owner-refined roadmap records the intended order after the current VM
foundation work. Future-phase scopes and tool names are direction, not detailed
implementation requirements; refine acceptance when a phase approaches. Do not
create speculative tickets for every candidate capability.

### Phase 1 — Secure, usable VM foundation

The current usability integration is PR #21, targeting `feat/vm-approach-3`.
Finish the agreed implementation, independently review and test each child PR,
get the child PRs through CI, and present each exact head for owner review.
Merge a child into #21 only after explicit owner approval; then pass combined #21
CI and automated acceptance. Perform real owner-host testing on Ariadne with
actual OpenCode use, and merge #21 into `feat/vm-approach-3` only after acceptance
and explicit owner approval. Its scope
includes:

- Independent persistent VM profiles and environments.
- Shared immutable SHA-addressed runtime installations, inventory and pruning.
- A simpler CLI and developer experience, host-metrics improvements, terminal
  interaction and settings fixes, and cross-distribution host support.
- A maintainable OpenCode compatibility foundation: one supported-version
  authority, identifiable version-sensitive integration seams, reproducible
  supported guests, and explicit expected/package-reported/executable-reported
  version validation. OpenCode 2.2.5 remains the supported version; no upgrade
  or rollback mechanism is part of #21.

After #21 is accepted and merged into `feat/vm-approach-3`, the owner will create
a separate comprehensive cleanup PR from that branch only after separately
authorizing that workstream, and do the work inside the new VM. This remains
Phase 1's in-VM development acid test; it is not part of #21 or this
compatibility child. Retain the cleanup acceptance criteria already recorded on ticket #57.
The cleanup should materially reduce unnecessary code and complexity, retire
obsolete VM implementations and redundant infrastructure, consolidate logic,
simplify tests without sacrificing meaningful coverage, and reconcile tickets.
It also includes proportionate MCP comments/audit-validation maintenance (with
an owner-side update/restart if evidence shows it is needed), consolidation of
documentation into a small set of authoritative human-readable documents, and
an approachable README with clear prose, diagrams, quick-start instructions and
practical examples. Report before/after line
counts for production code, tests and documentation, plus the agreed category
breakdown, and name obsolete implementation paths removed. Do not set arbitrary
line-count targets or trade away readability, security or correctness.

The authorized work in this sequence stops after #21's combined acceptance and
owner-approved merge into `feat/vm-approach-3`. Do not begin the separate cleanup
PR or merge PR #17 (`feat/vm-approach-3`) into `main` without their separately
authorized follow-on work and acceptance.

### Phase 1.5 — Managed upgrades, rollback and everyday VM maintenance

Profiles, multiple persistent environments, runtime pinning and the improved
CLI have been brought forward into #21; do not duplicate them. The remaining
work includes a generic supported upgrade process for Little Mester runtimes and
managed guest software. Qualify an actual OpenCode upgrade from the currently
supported 2.2.5 release, covering compatibility validation, configuration and
persistent-data migrations, activation, failure recovery and rollback. Avoid a
bespoke mechanism for each old/new version pair; investigate whether compatible
OpenCode upgrades can be independent of Little Mester runtime upgrades. Initially
it is acceptable to require a reviewed LM SHA containing the supported-version
declaration. Example commands such as `./lab vm versions`, `./lab vm upgrade
--check`, `./lab vm upgrade --opencode`, `./lab vm upgrade --runtime` and
`./lab vm rollback` describe desired capabilities only; they are not settled CLI
contracts.

This phase also retains the planned local-inference work (suitable backends such
as llama.cpp or Ollama, model compatibility/configuration/selection, performance,
GPU sharing across profiles, and sensible resource allocation) and a refined
everyday maintenance experience. Develop the work inside the new VM.

### Phase 2 — Disposable sibling VM orchestration

Build controlled host-side provisioning, acceptance and cleanup for disposable
candidate VMs, so compatibility qualification cannot modify or endanger the
known-good development environment in which the coordinating LM instance runs.
Provision from exact identifiable candidate revisions in independent temporary
environments, with bounded resource allocation and concurrency, automated
acceptance, structured results/logs/diagnostics, reliable cleanup, and isolation
between candidates, persistent VMs and the host. In particular, test new
OpenCode releases in disposable siblings rather than replacing the coordinator's
own execution dependency. Test VMs run beside the development VM on the physical
host, not through nested virtualization. Reuse Phase 1 profile/runtime
infrastructure where appropriate. Phase 2 is host orchestration; agent-facing
MCP access is Phase 3 work.

### Phase 3 — MCP bookshelf, project intelligence and developer workflows

Make Little Mester a productive day-to-day engineering environment with
curated tools, token-efficient context, persistent project knowledge and
refined workflows. Prefer local-first, open-source and Git-friendly approaches;
selectively enable capabilities; make network, filesystem and credential
permissions explicit; and reuse OpenCode or existing tools instead of
unnecessarily reinventing them. These are independently deliverable candidate
workstreams, not a commitment to every named tool:

- **3A — MCP and tooling bookshelf:** lightweight configuration, pinning,
  enabling and disabling of selected tools/servers, including project-specific
  capabilities and simple version management for appropriate managed MCP
  servers, developer tools and other guest software; do not build a bespoke
  framework where existing configuration suffices.
- **3B — Test orchestration MCP:** narrowly expose approved Phase 2 requests,
  acceptance operations, results and cleanup. Do not expose unrestricted host
  shell, sudo, hypervisor access, or authority to replace the persistent runtime.
- **3C — Documentation quality:** evaluate Textlint MCP, Vale, Markdownlint and
  Lychee; establish reusable human-facing review workflows without proliferating
  temporary Markdown documents.
- **3D — Codebase intelligence and context efficiency:** benchmark candidates
  such as Serena, Aider-style maps, GitNexus, CodeGraphContext, SCIP and
  Repomix. Measure token use, latency, navigation accuracy and task success for
  real work across Python, Java, Groovy, JavaScript and TypeScript. Retain
  overlapping tools only when their value is complementary.
- **3E — Persistent project knowledge:** keep authored knowledge, instructions,
  decisions and observations reviewable and Git-backed. Derived indexes/caches
  must be disposable and reconstructable from clean checkouts; do not make an
  opaque database the sole home of irreplaceable knowledge.
- **3F — Application testing and debugging:** investigate Playwright MCP,
  Chrome DevTools MCP, Semgrep and MCP Inspector for meaningful real-application
  evidence and diagnosis.
- **3G — Connected research and repository integrations:** consider Fetch,
  Context7 and GitHub integrations. Read-only GitHub access may reduce manual
  reporting; authenticated writes, pushes and merges remain owner-controlled
  unless a later decision explicitly changes that policy. Control network
  access and credentials explicitly.
- **3H — Everyday workflow refinement:** improve planning/review proportionality,
  task-specific workflows, model selection/delegation, agent/worktree and
  stacked-PR coordination, owner handoffs, context/session continuity, ticket
  lifecycle, docs, and proportionate testing/acceptance. Measure real task time,
  token use, model effectiveness, owner interventions, rework and reliability.
  Refine workflows throughout Phase 3, not only in its final PR; optimize
  engineering outcomes rather than tooling or process complexity.

### Phase 4 — Bounded autonomous development and self-correction

Build on Phases 2 and 3 so Little Mester can take a task such as “create a branch
and get the latest OpenCode release working”: discover upstream changes, identify
incompatible assumptions, update implementation/tests, qualify the version in an
isolated sibling environment, and prepare reviewed publication/merge handoffs.
Bound the work with explicit time, resource and retry budgets. Following owner
approval, use the trusted host-side lifecycle mechanism to activate the qualified
upgrade, potentially including Little Mester itself. Preserve a recoverable
known-good environment throughout; do not require an agent to remain operational
while replacing the software on which its current execution depends. Production
runtime updates, destructive operations and final merges remain owner-controlled.
Phase 3 supplies tools and workflows; Phase 4 coordinates them into bounded
engineering cycles.

### Phase 5 — Security, architecture and complexity reconciliation

Review capabilities added in Phases 1.5–4 for security boundaries and
permissions, host orchestration/MCP exposure, credentials, failure/recovery,
unnecessary complexity, redundant tools, test quality and documentation
accuracy. Do not repeat foundational cleanup already completed in the Phase 1
cleanup PR.

### Phase 6 — Final retirement and housekeeping

Retire any remaining legacy implementations, obsolete resources, branches and
tickets; reconcile repository policies and complete final cutover. Do useful
housekeeping earlier when appropriate rather than retaining obsolete
infrastructure solely for this phase.

After Phase 1, prefer small, independently reviewable and mergeable PRs directly
to `main` wherever practical. Use phases, milestones and tickets to group work;
do not require every capability in a phase to land through one enormous branch.
Use Little Mester daily and improve it through actual experience.

## Future architectural intent — not implemented

Eventually, Little Mester should manage plans as structured, validated,
programmatically accessible records through modular MCP capabilities, rather
than treating Markdown semantics as the machine-readable source of truth. Plans
should have stable identities and explicit, potentially many-to-many links to
existing tickets: plans may span multiple tickets, and tickets may belong to
multiple plans. This hierarchy is the default planning strategy, not the only
strategy the future architecture must support. Markdown remains suitable for
documentation today. **This is future intent, not an implemented feature.**
