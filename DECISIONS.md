# Little-Mester Architecture Decisions (moved)

This file is intentionally a pointer. The live decision logs are:

| File | Scope |
|---|---|
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Product usage, workflow, security model, and the VM harness decision (§C.8) |
| [`docs/tickets/architecture/DECISIONS.md`](docs/tickets/architecture/DECISIONS.md) | Ticket-engine engineering decisions |

The former root log (DECISION-001…012, 2026-10-21/22) is archived at
[`docs/tickets/archive/root-decisions-v1-2026-10-21.md`](docs/tickets/archive/root-decisions-v1-2026-10-21.md).
It is frozen history and must not be used as current guidance.

In particular, DECISION-011 (Docker-in-Docker rejection) predates the VM harness decision
in `docs/DECISIONS.md` §C.8: the harness now uses a disposable VM with **rootless Docker**,
not DinD. The trust boundary is documented in
[`docs/vm-architecture.md`](docs/vm-architecture.md).
