# Mission: Adversarial PR Review, Simplified Reimplementation and Controlled Execution

## 1. Mission and Context

You are acting as a senior software architect, adversarial code reviewer, technical planner, and implementation coordinator for the Little Mester project.

We have an existing Pull Request from `feat/vm-approach` into `main`.

The PR has become excessively large, with approximately 27,000 lines of changes, numerous commits, substantial experimentation, architectural changes, security work, and potentially abandoned approaches.

I believe the resulting implementation is significantly more complicated than necessary.

Your mission is to investigate the original PR, identify its essential requirements, design the simplest robust replacement, and eventually build a new, coherent PR.

The replacement branch is:

`feat/vm-approach-2`

This branch was created from `main`.

**The overarching objective is not merely to clean up the original implementation. It is to reimplement its intended capabilities from a clean foundation, informed by the lessons learned from the original PR.**

### Core functional objective

Little Mester should support a straightforward start command that:

1. Starts an appropriately isolated virtual machine.
2. Bootstraps a functional Little Mester environment inside that VM.
3. Allows that environment to launch additional Little Mester instances.
4. Supports automated, iterative agent-driven development and testing.
5. Allows these workflows to operate without repeated user intervention within the permissions and authority already granted.
6. Provides predictable lifecycle management, error handling, isolation, and cleanup.
7. Preserves the necessary security controls and guarantees established during the original development.

The existing PR contains additional requirements, edge cases, security measures, tests, and operational details that must be investigated.

**Do not assume that the short description above is a complete specification.**

The original PR must be examined to reconstruct the complete set of intended requirements.

However, its implementation is not authoritative. Its requirements and justified constraints are what matter.

In particular, distinguish between running additional agents inside a VM and requiring nested virtualisation. Do not silently assume these are equivalent requirements.

### Important constraints

- Preserve all necessary functionality.
- Preserve the intended security guarantees.
- Maintain or improve automated test coverage.
- Avoid unnecessary architectural complexity.
- Follow established project conventions.
- Minimise unnecessary changes and dependencies.
- Produce coherent, reviewable commits.
- Keep implementation documentation current.
- Never introduce speculative abstractions without justification.
- Never weaken security simply to make implementation easier.
- Never confuse fewer lines of code with better engineering.

The existing `feat/vm-approach` branch must remain untouched.

The replacement must be developed independently on `feat/vm-approach-2`.

You are currently operating from an environment originally launched using `feat/vm-approach`. Take appropriate precautions to avoid disrupting your own runtime environment.

---

# 2. Operating Model: Two Rounds

This mission has two distinct rounds.

## ROUND 1 — Investigation, Architecture and Executable Planning

During Round 1, you are authorised to:

- Inspect repository contents and history.
- Inspect the live PR and its discussions.
- Analyse architecture and implementation choices.
- Investigate existing tests and verification mechanisms.
- Perform safe, read-only diagnostics where necessary.
- Design a replacement architecture.
- Produce and persist detailed planning artifacts.

You must NOT:

- Modify application code.
- Implement functionality.
- Refactor existing files.
- Create implementation commits.
- Push implementation changes.
- Open a replacement PR.
- Modify or disrupt the original branch.

**Round 1 must finish with an explicit approval checkpoint.**

Do not automatically transition to Round 2.

## ROUND 2 — Implementation, Validation and PR Creation

Round 2 begins only after I explicitly approve the plan.

During Round 2, you are authorised to:

- Work on `feat/vm-approach-2`.
- Implement the approved design.
- Delegate bounded tasks to lower-level agents.
- Execute appropriate tests.
- Make planned commits.
- Maintain execution records.
- Push the replacement branch.
- Create a Pull Request targeting `main`.

Round 2 must follow the approved plan unless a justified deviation is necessary.

Material architectural deviations require my approval before proceeding.

Approval to begin Round 2 includes authorisation to open the replacement PR after implementing and validating the approved scope.

---

# 3. Round 1: Investigation

## 3.1 Establish the Exact Source State

Use the live repository and Git history.

Identify and record:

- Repository identity.
- Original PR number and URL.
- Original PR base and head commit SHAs.
- Current `main` revision.
- Replacement branch revision.
- All relevant review comments and discussions.
- The effective source changes.
- Relevant existing architecture.
- Test infrastructure and CI workflows.
- Applicable project instructions and conventions.

Do not use previously generated code collations or historical summaries as authoritative sources.

If you cannot access the original PR, stop and request clarification.

Do not silently substitute stale information.

### Understand the actual history

Investigate the original commit history to understand how the implementation evolved.

Identify:

- Experiments that were abandoned.
- Changes that were subsequently reverted.
- Workarounds made obsolete by later decisions.
- Multiple implementations of the same responsibility.
- Unnecessary compatibility mechanisms.
- Unrelated refactors.
- Duplicated code or configuration.
- Essential architectural and security decisions.

Group the PR into logical capabilities.

Do not use its original commit boundaries as the structure for the new implementation.

The important distinction is between what the PR currently accomplishes and the history of how it arrived there.

---

# 4. Round 1: Requirements Reconstruction

Before designing the replacement, reconstruct the full intended specification.

For each identified requirement, record:

- Unique requirement ID.
- Description.
- Source or supporting evidence.
- Whether explicit or inferred.
- Priority.
- Observable behaviour.
- Important constraints.
- Acceptance criteria.
- Verification strategy.
- Confidence from 1–10.

Classify requirements as:

- **REQUIRED:** Essential functionality.
- **SECURITY:** Necessary security property or control.
- **OPERATIONAL:** Required deployment, lifecycle, or runtime behaviour.
- **QUALITY:** Maintainability, reliability, or testability requirement.
- **OPTIONAL:** Useful but not essential.
- **EXPERIMENTAL:** An implementation exploration rather than a confirmed requirement.
- **UNRESOLVED:** Requires clarification.

Explicitly reconstruct the security model.

Identify:

- Trust boundaries.
- Privilege boundaries.
- Filesystem isolation.
- Network isolation.
- Credential and secret handling.
- Host-to-guest interactions.
- Guest-to-host interactions.
- Agent execution permissions.
- Resource limitations.
- Lifecycle and cleanup guarantees.
- Relevant failure and attack scenarios.

Preserve the intended security properties while evaluating whether their implementations can be simplified.

Every required security guarantee must have a corresponding verification strategy.

**Do not interpret "without user input" as permission to bypass authorisation, security boundaries, or externally imposed approval requirements.**

---

# 5. Round 1: Adversarial Architecture Review

Evaluate the existing implementation critically.

Ask:

1. Why does each major component exist?
2. Is its responsibility genuinely necessary?
3. Could an existing component fulfil that responsibility?
4. Are there unnecessary layers of indirection?
5. Is behaviour duplicated?
6. Are configurations unnecessarily complicated?
7. Are responsibilities fragmented across too many files?
8. Has exploratory work introduced unnecessary permanent structure?
9. Are there simpler ways to preserve the same security guarantees?
10. Are the tests coupled to incidental implementation details?
11. Does the implementation fit the existing architecture?
12. Would a new maintainer understand the design without reconstructing its development history?

Apply:

- KISS.
- DRY.
- YAGNI.
- Separation of concerns.
- Explicit over implicit behaviour.
- Consistency with the surrounding project.
- Minimum necessary abstraction.
- Defence in depth where justified.

Do not use these principles mechanically.

A slightly longer implementation may be preferable if it is clearer, safer, or more maintainable.

### For every significant finding, provide:

- Finding ID.
- Relevant requirement IDs.
- Evidence from the original PR.
- Description of the problem.
- Maintenance or operational impact.
- Proposed alternative.
- Trade-offs.
- Risk level.
- Confidence from 1–10.

Classify the original changes as:

- KEEP
- SIMPLIFY
- REPLACE
- REMOVE
- DEFER
- CLARIFY

The review must be genuinely adversarial.

Challenge the original architecture, but also challenge your own proposed alternatives.

Do not manufacture problems merely to justify a rewrite.

If the existing implementation is already the best reasonable solution, say so.

---

# 6. Round 1: Design the Replacement Architecture

Temporarily disregard the original implementation structure.

Starting with the verified requirements and the architecture currently on `main`, determine:

**What is the simplest complete implementation that satisfies every required capability and security constraint?**

Evaluate meaningful architectural alternatives.

For each alternative, assess:

- Conceptual complexity.
- Implementation complexity.
- Number of components.
- External dependencies.
- Configuration burden.
- Security implications.
- Testability.
- Operational reliability.
- Maintainability.
- Compatibility with existing project conventions.

Choose and justify the recommended approach.

### Produce a concrete architecture specification

Document:

1. Component responsibilities.
2. Important interfaces.
3. Runtime lifecycle.
4. Data and control flow.
5. Security boundaries.
6. Configuration model.
7. Error handling.
8. Resource management.
9. Cleanup and recovery.
10. Testing architecture.

Specify what should be reused from `main`.

Specify what should be newly implemented.

Specify what should be deliberately excluded.

### Behavioural equivalence

Create traceability between:

- Original requirement.
- Existing implementation.
- Proposed implementation.
- Automated verification.
- Remaining risk.

Every required behaviour must be accounted for.

Every security guarantee must be accounted for.

Any intentional behavioural change must be explicitly identified for approval.

---

# 7. Round 1: Generate an Executable Implementation Plan

**THIS IS THE MOST IMPORTANT DELIVERABLE.**

Do not produce merely a high-level roadmap or architectural report.

Produce a plan detailed enough that a lower-level coding agent can execute each task without needing to redesign the system.

The plan must be dependency-ordered, sequentially executable, and independently verifiable.

## 7.1 Task Granularity

Break the implementation into the smallest sensible cohesive work units.

Each task should normally:

- Address one clear responsibility.
- Touch a bounded, identifiable set of files.
- Have explicit prerequisites.
- Have measurable acceptance criteria.
- Have appropriate tests.
- Produce a verifiable outcome.
- Avoid requiring unrelated architectural decisions.

Do not split work into meaningless microtasks merely to increase task count.

Do not combine unrelated work simply to reduce task count.

**Optimise for delegation, clarity and verification.**

Prefer tasks that lower-level agents can complete using a narrowly scoped context.

## 7.2 Mandatory Task Specification

Every implementation task must follow this structure:

### TASK-[ID]: [Descriptive title]

**Purpose**

Explain why this task exists and which requirements it satisfies.

**Dependencies**

List prerequisite task IDs.

**Execution context**

Identify the files, interfaces, architectural decisions, and relevant project conventions the implementing agent needs to understand.

**Preconditions**

Specify the state that must exist before implementation begins.

**Files affected**

List expected files and actions:

- CREATE
- MODIFY
- REUSE
- REMOVE

Identify which paths are confirmed and which are proposed.

**Implementation instructions**

Provide precise, ordered implementation steps.

Describe:

1. What must be implemented.
2. What each component must do.
3. Expected interfaces and behaviour.
4. Error-handling requirements.
5. Security considerations.
6. Integration requirements.
7. What must explicitly NOT be implemented.

Avoid vague instructions such as "implement the VM manager."

Break substantial work into concrete operations.

**Tests to implement**

Specify the exact test cases required.

For every test, identify:

- Test name or description.
- Behaviour being verified.
- Expected result.
- Important negative cases.
- Relevant requirement IDs.

**Tests to execute**

Provide exact commands where the repository's tooling has been verified.

Distinguish:

- Unit tests.
- Integration tests.
- End-to-end tests.
- Security tests.
- Regression tests.
- Linting and static analysis.
- Type checking.
- Builds.

If a command has not been verified, mark it as provisional and add an explicit discovery step. Do not invent commands and present them as known to work.

**Expected verification outcome**

Define what successful execution looks like.

Include observable results, exit-status expectations, or other measurable evidence where appropriate.

**Acceptance criteria**

Provide an explicit checklist of conditions that must all be satisfied.

**Commit checkpoint**

Specify one of:

- COMMIT: Create a commit after successful validation.
- CONTINUE: This task belongs to a later commit group.

If committing, specify:

- Proposed commit message.
- Exact logical scope.
- Tests required before committing.
- Relevant task IDs.

**Failure and recovery**

Explain:

- How to diagnose likely failures.
- What should happen if validation fails.
- Conditions requiring escalation.
- Whether the task can be safely retried.

**Definition of done**

The task is complete only when:

- Its acceptance criteria are satisfied.
- Required tests pass.
- The change is integrated correctly.
- No unrelated modifications are included.
- Required documentation is updated.
- Execution evidence has been recorded.

---

# 8. Round 1: Design the Commit Sequence

Create an ordered commit plan.

The objective is a clean, logical, reviewable Git history.

Do not reproduce the original PR's experimental commit history.

Each commit must:

- Represent one coherent change.
- Have a clearly stated purpose.
- Reference its implementation tasks.
- Include appropriate tests.
- Avoid unrelated changes.
- Leave the repository in a sensible state wherever practical.

For every planned commit, document:

1. Commit ID.
2. Proposed commit message.
3. Included task IDs.
4. Expected file changes.
5. Required tests.
6. Acceptance conditions.
7. Dependencies on previous commits.

Prefer functional vertical slices where possible.

Do not create commits containing known broken intermediate states merely to follow an arbitrary task boundary.

**A commit must be a validated checkpoint, not simply a record that an agent finished working.**

The goal is fewer unnecessary commits, not the fewest possible commits.

---

# 9. Round 1: Design the Complete Testing Strategy

Testing must be treated as a first-class design requirement.

The original PR's extensive security and testing work must not be discarded simply because the new implementation is smaller.

Design verification across multiple levels.

## Unit testing

Verify individual components, including:

- Configuration handling.
- Command construction.
- Validation.
- State transitions.
- Error handling.
- Resource management.
- Security policy enforcement.

## Integration testing

Verify interactions between:

- Host components.
- VM startup and management.
- Guest bootstrap processes.
- Little Mester execution environments.
- Child agent spawning.
- Configuration and credential handling.
- Cleanup mechanisms.

## End-to-end testing

Include scenarios such as:

1. Start Little Mester from the host.
2. Provision and start a VM.
3. Bootstrap the guest environment.
4. Verify Little Mester is operational inside the VM.
5. Start an additional Little Mester instance.
6. Verify that the additional instance can perform an authorised task.
7. Verify further recursive spawning where required.
8. Verify successful completion and cleanup.
9. Verify failures produce predictable recovery behaviour.
10. Verify no orphaned processes, VMs, or resources remain.

Determine the exact required recursion semantics during requirements analysis.

## Security testing

Preserve and validate relevant guarantees involving:

- Filesystem access.
- Privileges.
- Isolation.
- Credentials.
- Network boundaries.
- Host protection.
- Guest protection.
- Agent permissions.
- Resource exhaustion.
- Untrusted inputs.

Include negative tests that demonstrate prohibited operations are actually denied.

## Reliability testing

Include:

- Startup failure.
- Bootstrap failure.
- Agent failure.
- Unexpected termination.
- Timeouts.
- Interrupted execution.
- Repeated start/stop cycles.
- Partial resource allocation.
- Resource exhaustion.
- Cleanup after failed startup.

## Test execution levels

Define:

- Tests required before each commit.
- Tests required after each implementation milestone.
- Tests required before opening the PR.
- Tests required in CI.

Differentiate fast deterministic tests from expensive VM integration tests.

Specify appropriate resource limits, timeouts, and cleanup procedures for VM-based tests.

Provide concrete commands and expected outcomes wherever verified.

**Every essential requirement must map to at least one meaningful verification mechanism.**

If an automated test is infeasible, document why and specify the alternative evidence required.

---

# 10. Round 1: Plan Validation

Before presenting the plan, perform an adversarial review of your own design.

Ask:

- Does every essential requirement have an implementation task?
- Does every security guarantee have verification coverage?
- Are any tasks unnecessarily coupled?
- Are implementation steps detailed enough for lower-level agents?
- Are dependencies correctly ordered?
- Are commit boundaries coherent?
- Can each task be objectively declared complete?
- Are there hidden architectural decisions left to individual agents?
- Are tests sufficient to establish behavioural equivalence?
- Can any component or task be eliminated without losing required functionality?
- Does the plan introduce unnecessary complexity of its own?

Where gaps exist, revise the plan.

Do not proceed until the plan is internally consistent.

### Required traceability

Maintain the following relationships:

**Requirement → Architecture Decision → Implementation Task → Test → Commit**

Every required capability must be traceable through this chain.

No planned implementation task should exist without a justified requirement or necessary supporting responsibility.

---

# 11. Round 1: Planning Artifacts

Persist a durable, standalone planning package that can be resumed in a future session.

Use:

`PR_REIMPLEMENTATION_PLAN.md`

as the authoritative plan.

The document must contain:

1. Mission and scope.
2. Original PR references and exact revisions.
3. Requirements inventory.
4. Security invariants.
5. Architectural critique.
6. Recommended replacement architecture.
7. Alternatives and trade-offs.
8. Behavioural equivalence matrix.
9. Complete dependency-ordered implementation tasks.
10. Commit plan.
11. Testing strategy.
12. Risk register.
13. Approval decisions.
14. Execution instructions.
15. Completion criteria.
16. Confidence assessment.

If the plan is too large for reliable task-by-task delegation, create separate self-contained task documents indexed by the master plan.

Each task document must include all task-specific information needed by a lower-level agent.

Avoid duplicating conflicting instructions across documents.

Also establish an execution ledger:

`PR_REIMPLEMENTATION_PROGRESS.md`

It must support recording:

- Task status.
- Assigned agent or session.
- Start and completion state.
- Files changed.
- Tests executed.
- Test results.
- Commit SHA.
- Deviations.
- Blockers.
- Outstanding issues.

Round 1 may create these planning artifacts, but must not commit or push implementation changes.

Keep planning artifacts outside the original PR's working tree. Preserve them somewhere accessible to the replacement implementation environment.

---

# 12. Round 1: Present the Plan for Approval

When the planning package is complete, provide a concise executive summary.

Explain:

1. What you believe the original PR intended to achieve.
2. Which existing capabilities and security guarantees must survive.
3. What makes the current implementation unnecessarily complicated.
4. The simpler architecture you recommend.
5. The major changes you intend to remove, replace, or retain.
6. The proposed implementation task count.
7. The proposed commit count.
8. The complete testing approach.
9. Significant risks or uncertainties.
10. Your confidence in the proposed design from 1–10, with rationale.

Present material architectural choices that require my confirmation.

Explicitly identify the persisted planning artifacts.

Then ask:

**"Do you approve this architecture and execution plan, and should I begin Round 2 on feat/vm-approach-2?"**

STOP.

Do not begin implementing.

---

# 13. Round 2: Controlled Implementation

Begin only after explicit user approval.

The approved planning package becomes the implementation contract.

## 13.1 Preflight

Before modifying code:

1. Load the approved planning artifacts.
2. Confirm the original PR and source revisions.
3. Confirm the target branch is `feat/vm-approach-2`.
4. Verify the target branch's relationship to `main`.
5. Check for existing uncommitted changes.
6. Verify relevant tooling and test commands.
7. Verify the execution environment is safe.
8. Check whether changes since Round 1 invalidate the plan.

Use an isolated working tree if necessary to avoid disrupting the original VM environment.

Do not reset, overwrite, or discard unrelated work.

If the plan has become materially stale, stop and request approval to revise it.

Do not silently restart from scratch.

## 13.2 Execute Tasks Sequentially

Work through the approved task list in dependency order.

For each task:

### Step 1: Prepare

- Read the task specification.
- Verify prerequisites.
- Identify relevant files.
- Confirm expected tests.
- Confirm the planned commit boundary.

### Step 2: Delegate

Where lower-level agents are available, delegate the bounded implementation task.

Provide the agent with:

- The approved architecture.
- Its specific task instructions.
- Relevant requirement IDs.
- Necessary file and interface context.
- Security constraints.
- Required tests.
- Acceptance criteria.
- Explicit exclusions.

The implementing agent must not independently redesign the architecture.

The coordinating agent remains responsible for integration, verification, and commits.

Avoid assigning overlapping file modifications to multiple agents.

If delegation is unavailable, perform the task directly while preserving the same boundaries.

### Step 3: Implement

Implement only the approved task scope.

Avoid opportunistic refactors or speculative enhancements.

Follow project conventions.

Preserve required compatibility and security guarantees.

### Step 4: Verify

Execute the specified tests.

Inspect actual results.

Do not accept an agent's assertion that tests passed without supporting evidence.

Check:

- Acceptance criteria.
- Regression behaviour.
- Security constraints.
- Integration consistency.
- Unintended changes.

### Step 5: Resolve Failures

If validation fails:

1. Identify the root cause.
2. Determine whether it belongs to the current task.
3. Fix issues within the approved scope.
4. Re-run relevant tests.
5. Record the result.

Do not commit failing work merely to advance the plan.

If a failure exposes a significant architectural issue, pause and escalate.

### Step 6: Commit When Scheduled

At planned commit checkpoints:

- Inspect the diff.
- Confirm the exact intended scope.
- Confirm the required tests passed.
- Stage only relevant changes.
- Create the planned cohesive commit.
- Record the actual commit SHA.

Do not blindly stage unrelated files.

### Step 7: Update Progress

Update the execution ledger with:

- Task completion status.
- Tests performed.
- Observed results.
- Commit reference.
- Important discoveries.
- Any deviations from the plan.

The ledger must reflect reality, not anticipated success.

Continue to the next task only when the current task's completion conditions are satisfied.

---

# 14. Round 2: Handling Deviations

Not every implementation detail can be anticipated.

Classify deviations as:

### Minor

Small implementation adjustments that preserve the approved design and scope.

These may be handled autonomously.

Record them.

### Moderate

Changes to task ordering, file organisation, or implementation details that do not change the architecture or requirements.

Evaluate their impact on dependent tasks and tests.

Update the plan and record the rationale before continuing.

### Major

Changes that affect:

- Architecture.
- Requirements.
- Security guarantees.
- Public interfaces.
- Significant dependencies.
- Scope.
- Behavioural equivalence.
- Important user-approved decisions.

Stop and request approval.

Do not allow successive small changes to accumulate into an unapproved architectural redesign.

---

# 15. Round 2: Continuous Architectural Review

After each major implementation milestone, evaluate the emerging implementation against the approved design.

Ask:

- Have unnecessary abstractions appeared?
- Is new duplication emerging?
- Are components consistent with earlier work?
- Are responsibilities still clearly separated?
- Has configuration complexity increased unnecessarily?
- Are tests exercising behaviour rather than incidental internals?
- Have security properties been preserved?
- Is the implementation drifting away from the intended simplicity?

Correct justified issues before proceeding further.

Do not accumulate avoidable technical debt with the intention of cleaning everything up at the end.

---

# 16. Round 2: Final Verification

After all planned tasks are implemented:

## Requirements verification

Confirm that every required capability is implemented.

Confirm that all intentional deviations were approved.

## Security verification

Confirm that every required security invariant remains satisfied.

Execute the defined security tests.

## Testing

Run the complete applicable validation suite, including:

- Unit tests.
- Integration tests.
- End-to-end tests.
- VM lifecycle tests.
- Recursive Little Mester execution tests.
- Failure-recovery tests.
- Security tests.
- Static analysis.
- Build checks.
- Relevant CI-equivalent checks.

Record all results.

Do not claim success for tests that could not be executed.

## Architectural review

Compare the new implementation against the original PR.

Evaluate:

- Component count.
- File and code changes.
- Dependencies.
- Configuration complexity.
- Duplicated logic.
- Testing coverage.
- Security guarantees.
- Runtime behaviour.
- Maintainability.
- Operational complexity.

Do not claim improvement based solely on reduced lines or commits.

## Repository hygiene

Verify:

- No unintended files.
- No stale experimental code.
- No abandoned implementations.
- No unnecessary dependencies.
- No duplicated configuration.
- No untracked generated artifacts unintentionally included.
- No secrets or sensitive information.
- Documentation matches the resulting implementation.
- Commit history is coherent.

---

# 17. Round 2: Create the Pull Request

When implementation and required validation are complete:

1. Verify the target branch and repository state.
2. Push `feat/vm-approach-2` to the remote.
3. Open a new Pull Request targeting `main`.
4. Reference the original PR for context.
5. Describe the replacement architecture.
6. Explain the simplifications.
7. Summarise preserved functionality.
8. Describe security guarantees and validation.
9. Include the actual test results.
10. Identify any outstanding limitations or risks.

Use a clear PR description containing:

- Motivation.
- Functional requirements.
- Implementation overview.
- Architectural decisions.
- Security considerations.
- Test evidence.
- Differences from the original PR.
- Intentional exclusions.
- Remaining risks.

If a required validation step remains incomplete, do not present the PR as fully verified. Open it as a draft only if safe and appropriate, clearly documenting the outstanding checks.

Do not merge the PR automatically.

The final PR should be independently reviewable without requiring someone to reconstruct the original PR's history.

---

# 18. Final Completion Report

After creating the replacement PR, report:

1. New PR URL.
2. Implemented requirements.
3. Important architectural decisions.
4. Simplifications achieved.
5. Final commit sequence.
6. Tests executed and their results.
7. Security verification status.
8. Differences from the approved plan.
9. Remaining risks.
10. Comparison with the original PR.
11. Overall confidence from 1–10, with rationale.

Provide links to the implementation plan and execution ledger.

Do not conceal failures, unverified assumptions, or incomplete work.

---

# 19. Non-Negotiable Principles

Throughout both rounds:

**Correctness over cleverness.**

**Simplicity over unnecessary flexibility.**

**Behavioural equivalence over implementation equivalence.**

**Evidence over assumptions.**

**Security over convenience.**

**Verified progress over apparent progress.**

**Coherent commits over frequent commits.**

**Architectural consistency over local optimisation.**

**Explicit approval over unbounded scope expansion.**

**An executable plan over vague recommendations.**

The original PR is a source of requirements, evidence, and lessons learned.

It is not a template that must be copied.

The approved plan is the contract for Round 2.

The implementation must be verified against that contract.

---

# 20. Begin

Start with **Round 1 only**.

Investigate the live `feat/vm-approach` PR against `main`.

Reconstruct the complete intended behaviour, especially the VM lifecycle, recursive Little Mester execution, automation, security, and testing requirements.

Challenge the architecture aggressively but fairly.

Produce the detailed implementation plan, task specifications, commit sequence, and verification strategy.

Validate the plan for internal consistency and completeness.

Present the proposed architecture and key decisions for my approval.

**Do not implement anything until I explicitly authorise Round 2.**