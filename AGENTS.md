# Replacement VM work: read this first

For `feat/vm-approach-2`, start with **PR_REIMPLEMENTATION_HANDOFF.md**. It is the
single current-session handoff and overrides older next-step, ticket, local-first,
and planning-pause prose in this repository. PR_REIMPLEMENTATION_PLAN.md and
PR_REIMPLEMENTATION_TASKS.md retain requirements; PR_REIMPLEMENTATION_PROGRESS.md
records evidence. Historical documents are not extra approval workflows.

- The 2026-11-06 reset is **planning only in that session**: no tests, code changes,
  launches or provider calls. Implementation starts only when the owner resumes it.
- Immediate goal on resume: move implementation into the replacement VM using
  OpenAI auth; then make metrics and recursive VM creation work there. Local-model
  discovery/gateway/performance work is not a prerequisite for this migration.
- Owner-selected roles: **Luna max** implements; **Sol medium** plans/reviews the
  evidence and assigns the next bounded fix. Every implementation includes a
  runnable evidence path. No PASS from fixtures alone; no refusal-only handoffs.
- A failed acceptance check OPENS its repair task; it never blocks implementing
  that repair. M0–M4 and the failure-to-fix matrix are already specified in the
  handoff. Do not stop after inspection to write another plan or wait for an
  aggregate gate to pass. Only actual unsafe live actions/dependencies pause.
- Do not read/search tickets to determine current work, approvals, or host commands.
  Ticket #50 is absent from this checkout, not a blocker or a ticket to recreate.
  The ticket redirects were explicitly authorized as one-time housekeeping.
- Ticket history is retained. If a future ticket write is explicitly requested,
  use the little-mester MCP engine only; never edit docs/tickets/ files directly.
- Preserve the old VM/session, existing ready profile/overlay, credentials, archives,
  frozen runtimes and user changes including vm-migration-plan.md. No push, merge,
  C14 cutover, broad cleanup, token copying or security-boundary weakening implied.
