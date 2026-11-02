# DECISIONS — How to use & improve little-mester (the product)

> **What goes in here:** Durable, project-wide product/workflow decisions and constraints intended to apply across tickets and sessions. Keep entries concise, state the rationale, and link to the implementing ticket when applicable.
>
> **Not here:** Per-ticket plans, implementation details, test results, host-run transcripts, diagnostics, acceptance progress, retry commands, or handoffs. Those belong on the implementing ticket, maintained through the ticket engine. Cross-ticket engineering architecture belongs in `docs/tickets/architecture/DECISIONS.md`; ticket-specific engineering and delivery evidence belongs in that ticket. Do not duplicate a ticket's execution log here. Add a rule here only when it is a durable project-wide decision, not merely because implementation work changed code.

### Documentation boundaries (required)

- **Project decisions (`docs/DECISIONS.md`):** durable, cross-ticket policy, product workflow, and constraints. Record the decision and why; link to the ticket for implementation and evidence.
- **Subsystem architecture (`docs/tickets/architecture/DECISIONS.md`):** durable technical architecture/conventions that govern multiple tickets in that subsystem.
- **Implementing ticket:** scope, alternatives considered for that work, implementation plan, changes made, test evidence, host/CI attempts, failures/diagnosis, acceptance-criteria status, exact retry/handoff steps, and commit references. Use the ticket engine MCP/CLI; do not edit ticket JSON directly.
- **Schema changes:** use existing ticket fields (`objective`, `acceptance_criteria[].evidence`, and `tests.needed/created/cases`) when they fit. If an actual gap requires a new field, first plan and discuss the schema change as a separate ticketed change, update the ticket schema/engine/validation/CLI/MCP together, and migrate/test it before using the field. Do not invent ad-hoc fields or move evidence into this file as a workaround.

## ⚠️ Read before every session

Always read both this file and the per-subsystem decision logs before starting work:
1. This file — constraints about how you use little-mester, what's true NOW
2. Sub-system files (`docs/tickets/architecture/DECISIONS.md`) — engineering designs scoped to each area

### Meta rule: Document before acting
**Before doing ANY work** (code, config, tests, scripts, installs, or changes to agent behavior):
1. **Check for existing decisions** — Read `docs/DECISIONS.md` and relevant subsystem decision logs first.
2. **If it's architectural/structural** — Any decision about architecture, defaults, conventions, file layout, dependencies, or agent configuration MUST be discussed with the user first. Do NOT guess or make assumptions.
3. **Write it up** — After discussion/clarification, write the decision to the appropriate DECISIONS file (this one for usage/product workflow, or `docs/tickets/architecture/DECISIONS.md` for engineering/subsystem design) BEFORE implementing. Include rationale, trade-offs if relevant.
4. **Follow the doc→test→code→commit cycle** as stated in workflows — documentation comes first.

This rule applies to all future sessions. No implementation without prior written decision when the choice is non-obvious or architectural.

---

## A. How to use little-mester

### A.1 Default workflow — VM-native agent

The owner-directed release target is a native OpenCode process inside a local
QEMU/KVM guest. `./lab <workspace>` is the intended VM-first entry point; the former
container launcher is retained separately as `./lab-docker`. The launcher cutover and
VM acceptance are still in progress, so use the command behavior documented by the
current README and ticket #35 until that cutover is implemented. `./lab-vm` remains the
explicit lifecycle and diagnostic interface during this transition.

The guest receives one selected workspace. It has broad authority inside the guest,
but must not receive host credentials, host Docker, or files outside that workspace.
Use `--recurse --max-depth N` only for bounded nested testing. `--unsafe-recurse`
permits write access to a selected Little Mester checkout; it does not grant extra
network or host access. Do not use write-enabled recursion while VM control keys/state
are visible through the workspace share.

### A.2 Configuration defaults

| Setting | Default | Notes |
|---|---|---|
| Agent | OpenCode in the guest | Docker compatibility mode is explicit: `./lab-docker` |
| Workspace | One selected directory at `/workspace` | Guarded before the host shares it |
| Recursion | Disabled (`max-depth=0`) | Opt in explicitly; a child cannot raise the inherited maximum |
| Public HTTP(S) | Through the policy-checked VM proxy | Direct guest egress and non-HTTP protocols are blocked |
| Guest Docker | For project workloads | Not an agent wrapper or a host security boundary |
| Permission mode | OpenCode default/ask | `--auto-accept` requests OpenCode `--auto`; explicit denies remain configured |

Product direction does not imply acceptance. Check #35 and its linked ticket ledgers
before describing host lifecycle, auth, permission UI, workload, or isolation behavior
as accepted.

### A.3 Verify the active runtime

```bash
./lab-vm context --json
./lab-vm status default
./lab-vm verify-isolation default
```

These report runtime/context and representative boundary checks; they are not a
substitute for the full acceptance matrix or a proof against hypervisor vulnerabilities.

---

## B. Workflow & discipline

### B.1 Core development cycle (always follow)
Every change follows: **doc → test → code → commit**. No stage skipped, no ordering changes without a ticket explaining why.

### B.2 Conventional commits rule
All commits use the format: `type: concise description` where type is one of `feat`, `fix`, `docs`, `refactor`, `test`, `chore`. This applies to both product code AND ticket management changes.

### B.3 Session handoff — never skip this
When ending a session mid-work, include at the top of relevant docs:
```
## Current status
- What's done: ...
- What's in progress: ...
- Blocked on: ...
- Decisions made this session: [links to DECISIONS entries]
```

Next session reads that first — never re-implement already-completed work.

### B.4 Ticket edits — MCP/CLI only, enforced across ALL agent implementations

**Rule:** Ticket data (`docs/tickets/*.json`, `_index.json`, `_audit.json`, `.engine.lock`) MUST
ONLY be created, modified, or deleted through the ticket-engine CLI or the MCP server tools.
Direct file edits are forbidden for every agent — opencode, Claude Code, Codex, or any other
system that may be pointed at this workspace.

- **CLI:** `./little-mester-mcp/scripts/ticket-engine <action> ...`
- **MCP tools:** `mester_tickets_create`, `mester_tickets_update`, `mester_tickets_archive`,
  `mester_tickets_validate`, `mester_tickets_audit`, etc.

This is the same rule as C.3, but elevated to a **cross-agent architecture constraint**. The
ticket engine is the sole write authority. Every agent implementation that touches this workspace
must respect it, regardless of its native permissions or capabilities.

**Why (one architecture, multiple implementations):**

| Concern | What happens without the rule | What happens with it |
|---|---|---|
| **Index consistency** | `_index.json` drifts from ticket files — `next_number` resets, stale status/area snapshots | Engine recomputes atomically on every write |
| **Audit trail** | Silent edits — no one knows who changed what, when, or why | Every write appends to `_audit.json` with timestamp and actor |
| **Locking** | Two agents write the same ticket simultaneously, data loss | Engine holds a PID lock; conflicting writers get a clear error |
| **Cross-agent consistency** | Opencode denies via permissions, Claude Code ignores it, Codex overwrites — the workspace becomes unreliable | All agents share the same contract; the workspace stays correct regardless of which agent runs |

The workspace is the shared data store. The agent is just a transient reader/writer. The rule
exists because the workspace cannot tolerate inconsistent writers — and different agents have
different permission models that make it easy to bypass restrictions on one platform while
enforcing them on another.

**Enforcement per agent platform:**

| Platform | Mechanism | Location in repo |
|---|---|---|
| **OpenCode** | `permission` deny rules on `docs/tickets/**/*.json` + `instructions` array reminder | `agent/opencode/opencode.json` |
| **Claude Code** | `CLAUDE.md` system prompt (read at session start) | `CLAUDE.md` (in workspace root) |
| **Codex** | `CODING.md` or agent config directive | `CODING.md` (in workspace root) |
| **Any agent with file access** | Instruction file at workspace root; agents that read docs on session start will see it | `docs/tickets/README.md` or a top-level `AGENT-INSTRUCTIONS.md` |

Every new agent integration must add the same constraint to its platform-specific config. The
rule is not optional per-agent — it is a workspace invariant. If a platform does not support
permission denies, the instruction must be prominent in the system prompt and the rationale
(links to this decision) must be included so the agent understands it is an architecture
constraint, not a preference.

**What counts as a "ticket edit" (every action below requires the engine):**
- Creating a new ticket file (use `ticket-engine create` or `mester_tickets_create`)
- Changing any field in an existing ticket (use `ticket-engine update` or `mester_tickets_update`)
- Changing a ticket's status (use `mester_tickets_update` — **not** a JSON edit)
- Updating acceptance criteria, tests, evidence, or comments
- Archiving a ticket (use `ticket-engine archive` or `mester_tickets_archive`)
- Running `validate` or `audit` (safe — these are reads, not writes)

**What does NOT require the engine:**
- Reading ticket files (`cat`, `grep`, editor views) — reading is always allowed
- Editing files outside `docs/tickets/` — product code, agent configs, docs, scripts, etc.
- Deleting `_index.json` and letting the engine rebuild it (this is how you fix drift)

**If a ticket file is already corrupted by a direct edit:**
1. Do NOT attempt to repair it by hand.
2. Run `./little-mester-mcp/scripts/ticket-engine validate` to see the extent of drift.
3. If `_index.json` is out of sync, delete it and run `validate` to rebuild.
4. If `_audit.json` is missing entries, treat affected tickets as orphans — stop and ask a
   human before running `audit --baseline`.
5. After repair, commit the fix with a conventional commit noting the repair.

**Cross-agent note:** This is a deliberate architecture decision. The workspace is the contract;
agents are transient. Whether you run opencode, Claude Code, Codex, or a future agent platform,
the ticket store remains consistent because all paths converge on the same write authority.
Enforcement is per-platform, but the rule is universal.

---

## C. Constraints (what's true, not designs)

### C.1 Testing status (updated 2026-10-26)
The ticket engine now has a working Jest suite: **143 tests** (54 engine, 57 CLI, 32 MCP) under `little-mester-mcp/tests/`. Run it with `cd little-mester-mcp && npm test`. This resolves the earlier engine-testing blocker. Host-side Bats tests still require `bats-core` (see the root README's Testing section).

### C.2 SSH / git auth — OUT OF SCOPE (now)
Container-native SSH not managed here. Token-based or GitHub CLI auth is recommended path for any external git operations. Separate future ticket when needed.

### C.3 Ticket writes go through the engine (2026-10-26)
**Rule:** Never create, edit, move, or delete ticket data by hand. Use the engine:

- CLI: `./little-mester-mcp/scripts/ticket-engine ...` (`create`, `update`, `archive`, `audit`, `validate`)
- MCP: the `mester_tickets_*` tools

This covers `docs/tickets/ticket-NNN.json`, `_index.json`, and `_audit.json`. The engine
owns the write window, updates the index, and appends an audit entry.

**Why:** The index and audit log are derived data. A hand edit makes them silently wrong.
The engine cannot stop you, but `validate` will show the drift, a tampered audit entry, a
stale lock, or an orphan file.

**If `validate` complains:**
1. **Do not delete anything automatically.**
2. Read the report. Orphans are ticket files with no audit entry — they need a human
   decision.
3. To adopt a pre-existing batch once, run `./little-mester-mcp/scripts/ticket-engine audit --baseline`.
   This records them as `baseline` entries; it does not change the ticket files.
4. A stale lock is safe to clear only after checking the PID is gone. The engine clears
   dead-PID locks itself on the next write.

Engineering details and invariants: [docs/tickets/architecture/DECISIONS.md](tickets/architecture/DECISIONS.md) §I.

### C.4 Acceptance criteria and tests are first-class (2026-10-26)
**Rule:** Every ticket MUST have at least one **acceptance criterion** and a **test ledger**.
The engine enforces this at create/update time and `validate` fails when it is missing.

Acceptance criteria are an array of objects, not prose in `objective` or a comment:

```json
"acceptance_criteria": [
  {
    "id": "A1",
    "text": "A user with valid credentials receives a session token",
    "status": "passing",
    "evidence": "tests/auth.test.ts › valid credentials"
  },
  {
    "id": "A2",
    "text": "A wrong password is rejected without leaking whether the account exists",
    "status": "waived",
    "note": "Requires a browser session; manual QA only for now"
  }
]
```

The criterion **status is the single place to track testing**, because "test needed",
"test created", and "test passing" are states of the same criterion:

| Status | Meaning |
|---|---|
| `pending` | Not started; nothing verified yet |
| `in-progress` | Implementation underway |
| `test-needed` | Implemented; a test still needs to be written |
| `test-created` | A test exists but has not been confirmed passing |
| `passing` | Verified. `evidence` is **required** (test path/case, command + result, or artifact) |
| `waived` | No test is needed or possible. `note` is **required** explaining why |

The ticket-level test ledger records the paper trail:

```json
"tests": {
  "needed": ["test that an empty password is rejected"],
  "created": ["tests/auth.test.ts"],
  "cases": ["valid credentials", "wrong password", "empty password"]
}
```

**Write rules:**
- `create` requires at least one criterion. Bare strings are normalized to
  `{id: "A1"…, status: "pending"}`.
- `update` replaces the acceptance-criteria array (so send the full array when editing
  through the API). `tests.needed`, `tests.created`, and `tests.cases` are **merged**,
  never replaced: a later edit cannot erase the paper trail.
- Tests may drift from reality, but the trail must remain. A stale `passing` claim is
  corrected by changing the status and evidence — not by deleting the criterion.
- `validate` **fails** on missing criteria/ledger, invalid/duplicate ids, invalid status,
  `passing` without evidence, or `waived` without a note. It **warns** (does not fail) on
  drift: `test-needed` with an empty `needed` list, `test-created`/`passing` with an empty
  `created` list, or a `resolved` ticket with unverified criteria.

**Why:** "Done" becomes checkable by a human or an agent. Without a criterion there is no
definition of done; without a test ledger there is no way to tell whether the work was
verified or merely claimed.

Schema: [`little-mester-mcp/docs/SCHEMA.md`](../little-mester-mcp/docs/SCHEMA.md) and
[`ticket-v1.json`](../little-mester-mcp/docs/ticket-v1.json). Engineering invariants:
[`docs/tickets/architecture/DECISIONS.md`](tickets/architecture/DECISIONS.md) §J.

### C.5 Merging the ticket store (2026-10-26)
**Rule:** Ticket content merges like normal source. Derived files have their own rules.

| File | Merge rule |
|---|---|
| `ticket-NNN.json` | Normal three-way merge. Resolve content conflicts in the file, then run `validate`. Never resolve a conflict by deleting a ticket. |
| `_index.json` | **Never hand-merge.** It is derived. Delete it and run any engine command (for example `ticket-engine validate`) to reconstruct it from the ticket files, then commit the rebuilt index. |
| `_audit.json` | **Append-only.** On conflict, keep the entries from both sides with `ticket-engine audit --merge <their-log>`. The command verifies both logs, de-duplicates, orders by timestamp, and refuses to write an invalid log. Do not delete, edit, or renumber entries by hand. |
| `.engine.lock` | Never commit. It is a transient PID lock; `.gitignore` excludes it. A lock present after a crash is reported as stale by `validate`. |

**Resolving the two generated files:**

```bash
# Index: derived, so delete it and let the engine rebuild from ticket files.
rm docs/tickets/_index.json

# Audit: keep your side, then merge the other branch's entries through the engine.
git checkout --ours docs/tickets/_audit.json
git show :3:docs/tickets/_audit.json > /tmp/theirs-audit.json
./little-mester-mcp/scripts/ticket-engine audit --merge /tmp/theirs-audit.json
./little-mester-mcp/scripts/ticket-engine validate
```

**After a ticket-store merge:**
1. Run `./little-mester-mcp/scripts/ticket-engine validate`.
2. If an audit entry is missing, treat the ticket file as an **orphan**. Stop and ask a
   human. Do not run `audit --baseline` to hide a dropped entry; that command is only for
   adopting a store that predates enforcement.
3. Commit the rebuilt `_index.json` and the unioned `_audit.json` with the content merge.

**Why:** `_index.json` is a cache and must never be a source of truth. `_audit.json` is
evidence and must never lose a branch's writes silently. This keeps a merge auditable
instead of producing a plausible-looking but incomplete history.

### C.6 VM test environment

The current VM trust boundary and operating model are defined in [C.8](#c8-vm-harness-topology-transport-and-isolation).
Ticket-specific provisioning, test, and teardown procedures belong on #41–#45, not in
this decision log.

### C.7 Merge scope for `feat/ticket-context` (2026-10-26)
**Decision:** Merge the **whole `feat/ticket-context` branch** into `main` rather than
extracting an engine-only commit set. The branch is the reviewed unit: it carries the
engine, the ticket store, the AC policy, CI, and the config/docs work that depends on
them. Extracting a subset would split related history and make the ticket-store merge
harder to audit.

The module still has to build **independently** from its own package directory. A clean
clone of the branch passes `npm ci`, `npm run build`, `npm run check:package`, the full
Jest suite, and `ticket-engine validate`; those results are recorded on ticket #36.
Merge is blocked until #35's VM evidence and #36's remaining criteria are complete.

### C.9 VM harness implementation details (2026-10-26)

These decisions cover the `./lab-vm` scripts, `lib/vm/` modules, and the CachyOS testing
work. They are tactical choices that don't change the topology in §C.8 but affect how the
code is structured and what to watch for on other distros.

#### C.9.1 VM control state is private and outside workspaces

**Current decision (supersedes the original checkout-local-state rationale):** VM profiles,
base disks, seeds, keys, sockets, PIDs, and per-profile logs live outside selected workspaces
under `~/.local/share/little-mester/vm/`. Launcher and host-Docker logs use the same root's
`logs/` directory. `XDG_DATA_HOME` or `MESTER_STATE_DIR` may relocate the complete per-user
root. `./lab-vm context` reports the effective profile directory. Git-ignored state is not
private if the agent can read it through the workspace share.

The implementation does not fall back to checkout-local runtime state and does not move old
profiles. A stale checkout-local `.mester` directory is refused as a selected workspace until
reviewed; it is never used as a profile root. Inventory profiles and qcow2 backing chains before
any manual move/rebase. Nested profiles remain in a private recursion session inside the
containing guest.

#### C.9.2 Prereq check runs only for state-changing commands

**Decision:** The `_vm_prereqs_check()` function runs before `base`, `up`, `stop`, and
`down`. It is skipped for `status`, `ssh`, and `test`.

**Why:** `status` and `ssh` are read-only / interactive; running `pacman -S` or `apt install`
mid-session is disruptive and unnecessary. `test` is a convenience wrapper that calls `up`
internally, which will trigger prereqs.

#### C.9.3 Distro auto-detection for package names

**Decision:** `_vm_prereqs_resolve_packages()` maps each missing command to distro-specific
package names using `/etc/os-release` (ID + VERSION_ID):

| Distro | `virtiofsd` package | `qemu-img` package | `genisoimage` package |
|---|---|---|---|
| Debian/Ubuntu | `libvirt-daemon-system` | `qemu-utils` | `genisoimage` |
| Fedora | `libvirt-daemon-config-nwfilter` | `qemu-img` | `genisoimage` |
| RHEL/CentOS | `libvirt-daemon-config-nwfilter` | `qemu-img` | `genisoimage` |
| Arch/CachyOS | `virtiofsd` | `qemu-img` | `genisoimage` |

**Why:** The same command maps to different packages across distros. Hard-coding a single
package name would break on half the distros.

#### C.9.4 Loop variables prefixed with `_` to avoid caller clobbering

**Decision:** All loop variables inside `lib/vm/prereqs.sh` use the `_` prefix
(e.g. `_c`, `_c2`, `_miss`, `_clean`, `_pkg`, `_id`).

**Why:** `prereqs.sh` is sourced (not executed), so its variables share the caller's
namespace. Without the `_` prefix, a variable like `$cmd` inside the script would overwrite
the caller's `$cmd` — and the main dispatch loop in `lab-vm` uses `$cmd` for the command
argument. This caused silent command corruption in early iterations.

#### C.9.5 `command -v` for binary detection (with Arch caveat)

**Decision:** Use `command -v <name>` to check for required binaries. If a binary was
installed but `command -v` returns nothing, the binary may live at a non-standard path
(e.g. `/usr/lib/qemu/qemu-virtiofsd` on Arch). The fallback is to search with `find` or
`pacman -Ql <pkg> | grep bin` to locate it, then either add a PATH extension or use the
full path in the check.

**CachyOS / Arch caveat:** The `virtiofsd` package may provide the binary as
`/usr/lib/qemu/qemu-virtiofsd` rather than `/usr/bin/virtiofsd`. On Arch-derived distros,
`command -v virtiofsd` will fail even though the package is installed. Workaround: check
both `command -v virtiofsd` and `command -v qemu-virtiofsd`, or search with `find`.

#### C.9.6 ISO generator: report only if *neither* exists

**Decision:** `genisoimage` / `mkisofs` are reported as a pair — only if **both** are
missing is the pair reported. If either exists, neither is flagged.

**Why:** These two packages are interchangeable; the script tries `genisoimage` first and
falls back to `mkisofs`. Reporting both when only one is missing would be redundant.

#### C.9.7 Lifecycle acceptance is a set of independent gates

There is no single canonical command sequence that establishes VM acceptance. Record
base-image integrity, first provisioning, warm startup, guest readiness, agent attach,
workspace switch, safe shutdown/resume, destructive teardown, auth/permission behavior,
workload operation, and isolation as separate results on the relevant ticket. Preserve
the VM and its evidence until the owner approves destructive cleanup. In particular,
the current `stop`/`down` behavior deletes guest state and is not a routine daily stop;
see #41/#52 for the current lifecycle contract and evidence.

The local implementation now provides explicit `suspend`/`resume` commands that preserve the
overlay and workspace. Suspend checks for live nested QEMU processes in the guest and fails
closed on an untracked process or an unavailable probe; recursion capacity alone is not treated
as evidence that a child exists. `suspend --poweroff-recursive` explicitly requests graceful,
deepest-first child suspension before the parent. If any child cannot be stopped and verified,
the parent remains running; child profile state is preserved. These commands are not recommended
for valuable state until owner-host shutdown/resume and workload-persistence checks pass;
`stop`/`down` remain destructive.

#### C.9.8 User permission prompt before package installation

**Decision:** Before running `sudo pacman -S`, `sudo apt install`, etc., the script
prints the list of packages and prompts `[y/N]`. The default is `N` (no install).

**Why:** The agent must never unilaterally install packages on the host. The user must
explicitly confirm each installation. This is a security boundary: the VM harness is
powerful enough to modify the host, but it asks before doing so.

#### C.9.9 Ticket-level AC and procedures live on the ticket, not in separate docs

**Decision:** Acceptance criteria and testing procedures that are specific to a single
ticket belong in the ticket's `acceptance_criteria` and `tests` fields. Long-lived
architecture decisions, topology, trade-offs, and cross-ticket patterns belong in
`docs/DECISIONS.md` and `docs/vm-architecture.md`.

**Why:** A separate testing plan doc drifts from the ticket state. When the ticket is
closed, the doc becomes stale. Keeping procedures on the ticket means the AC status,
evidence, and test steps are always co-located and reviewed together. The separate doc
is for decisions that outlive any single ticket.

**What goes where:**

| Lives on the ticket | Lives in DECISIONS.md / vm-architecture.md |
|---|---|
| Per-ticket acceptance criteria with evidence | Cross-ticket architecture and topology |
| Step-by-step testing procedures for that ticket | Design trade-off rationale |
| Current status: pass / in-progress / pending | Rejected alternatives |
| Session-specific notes and outcomes | Conventions and non-goals |
| | Distro-specific implementation caveats (as reference) |

Ticket-specific test setup and procedures live on the relevant ticket; do not duplicate
them in separate test-plan documents. The ticket engine's current `tests` ledger records
needed/created/cases, while a first-class planned test-plan field is tracked by #51.

#### C.9.10 Log rotation — last 5 runs (original checkout-local path, 2026-10-27)

**Decision:** Both `./lab` and `./lab-vm` rotate their stdout/stderr logs, keeping the
last 5 timestamped runs. The original implementation stored logs under the checkout;
the current location is `$HOME/.local/share/little-mester/logs/` (or the configured
per-user state root), as recorded in C.9.25.

Implementation: each script defines `rotate_logs()` which creates the log directory,
generates a timestamped filename, and prunes to 5 files. The script then uses
`exec 1> >(tee -a "$LOG_FILE")` and `exec 2> >(tee -a "$LOG_FILE" >&2)` to tee all
output to both stdout and the log file.

**Why:** Without log rotation, repeated runs accumulate indefinitely in the repo or
get lost when the container is recreated. A 5-run window captures enough history for
debugging while keeping disk usage bounded. Runtime logs no longer live in or depend on
the source checkout.

#### C.9.12 Human gates and session handoff (2026-10-28)

**Project-wide workflow decision:** Before requesting an owner-operated test or a
potentially destructive/environment-changing action, put the exact procedure,
expected observations, stop conditions, evidence/secret safeguards, and current
revision on the implementing ticket. Record the returned result there before
planning a follow-up. Ticket fields, not this log, hold execution history and
handoffs. Keep an existing working session available until its replacement path
is verified; do not expose credentials in tickets or logs.

#### C.9.13 Native provider auth and credential boundaries (2026-10-29)

**General auth decision:** Authenticate in the OpenCode server/process that will
use the account, using its native provider dialog where available. A host
credential database or broad host credential directory must not be copied or
mounted into the VM. Any optional escrow/import/export is runtime-only, private,
auditable without secret output, provider-agnostic where supported, and must not
put credentials in the workspace, Git, image, base disk, or seed. Persistence
and safe export-before-delete are acceptance gates, not assumed behavior.

If a provider uses a host-browser OAuth callback, route only the callback to the
actual server namespace using a temporary, explicit host-loopback-to-control-SSH
forward. Keep it alive only during the login attempt and close it on success or
cancellation. Discover and pin the server namespace, auth method, and listener
for the installed OpenCode version; port numbers are not a cross-version
contract. Never publish a LAN callback, add a general host route, bypass the
egress proxy's private-address denial, or substitute an API key/device-only
flow for a required subscription browser flow. Route browser/model traffic only
through the public-only VM proxy, and require owner-controlled success, cancellation,
port-conflict, egress-denial, request, and persistence evidence before claiming
auth accepted. Ticket #45 owns the implementation chronology and test ledger;
the [auth runbook](chatgpt-auth.md) contains operator steps and secret handling.

**Public VM HTTP(S) egress approval (2026-10-29):** The owner approved broad
public-web access so the guest agent can do ordinary project work. Any public
HTTP(S) hostname may use the existing host proxy when `.mester-egress`
`allowed_domains` is blank. This does not create a direct guest route: QEMU stays
`restrict=on`, and the proxy rejects private/reserved/multicast addresses, mixed
public/non-public DNS answers, and the host's local/directly connected networks
(including IPv4-embedded IPv6 destinations). Non-HTTP protocols remain denied. No host
firewall, route, NAT, or LAN listener is added. Reload the running VM proxy after
the manifest change; live positive/negative and nested-VM checks remain required.

If the guest cannot launch the host browser, a host-resident MCP or equivalent
may open an ephemeral authorization URL only for approved provider origins.
Reach it only through a temporary per-VM SSH reverse forward bound to guest
loopback; its host listener remains bound to host loopback. Expose only the
provider-specific open-URL capability, with no general host access. Never log
or retain the URL/state, and never capture the callback/code; the callback
continues directly to the guest through the temporary loopback SSH forward.

**Recursive information sharing (strict push-only direction, 2026-10-29; design in #55):**
The initial sharing path must be parent-to-child only: the parent selects and validates a
schema-limited snapshot, and the child cannot request data or send control metadata through
that channel. Snapshot format, integrity, delivery, size limits, expiry, and revocation remain
design work. Read-only MCP resources are a future alternative to investigate for their common
resource shape, discovery/interoperability, and tooling; resource reads carry child request
metadata upward and are not the current path. Any reconsideration requires an explicit owner
decision. Neither design substitutes for `restrict=on` or proxy destination checks, and no
generic parent/host tools or paths may be exposed to descendants.

**OpenAI ChatGPT implementation for the pinned guest version:** OpenCode 2.2.0
source commit `1097e2668b1e08ad63bdf0b1c4860b9bc01e420c`,
`packages/core/src/plugin/provider/chatgpt.ts`, creates its OAuth callback
listener with `server.listen(0, "127.0.0.1", ...)`, then uses the selected port
in the OAuth `redirect_uri` (`/auth/callback`). This is confirmed by source
inspection, **not a stable/documented OpenCode CLI or API contract**. The
temporary `./lab-vm agent` host bridge (enabled by default; `--no-browser-auth`
opts out) extracts the port from the actual
validated authorize URL and asks its private OpenSSH control master to forward
only `127.0.0.1:<port>` on the host to `127.0.0.1:<port>` in the guest before
opening the unchanged URL. At most one callback forward is active; a new port
replaces the previous one. Auth forwarding commands ignore host SSH configuration
and explicitly disable SSH agent/X11 forwarding so inherited forwards, proxy
commands or agent sockets cannot expand this capability.
It fails closed if the authorization endpoint, callback URI, port,
or control forward is unexpected; it does not guess a fixed port, rewrite the
URL, open a range, or bind to a LAN interface. The independent diagnostic
`./lab-vm auth-forward [name] <callback-port>` also takes exactly one locally
observed port. This runtime URL parsing is a version-specific integration
assumption, not an OpenCode API; on every pinned-version change inspect the
provider source, update regression tests and this decision, and repeat the
owner-controlled browser acceptance. The tunnel and allowlist are implemented;
live browser login and persistence remain unverified under #45.

**Permission decision:** `--unsafe-recurse` authorizes exporting the repo; it
does not select an OpenCode permission mode. `--auto-accept` / `--permissions auto`
request the supported client `--auto` flag, preserving explicit denies. Consume
lab flags regardless of position and pass agent arguments unchanged. `ask` and
auto-accept conflict; unsupported modes must fail clearly, not silently do
nothing. Do not patch generated permissions into blanket allow rules.


---

### C.8 VM harness topology, transport and isolation (current)
**Product architecture decision:** Little Mester runs the harness directly in a local,
QEMU/KVM VM guest. The owner-directed default entry point is `./lab`; until that launcher
cutover lands, the implementation remains split between the Docker launcher and
`./lab-vm`. The guest VM, not a Docker container, is the intended host-isolation boundary.
The agent is intentionally given guest-level control: it can use Docker/Compose for
project services, install and configure guest software, and launch nested Little Mester
QEMU/KVM VMs when supported. Docker is a workload tool inside the guest, never the outer
agent sandbox. The selected workspace enters through virtiofs; model APIs and egress
remain on narrowly forwarded, policy-controlled channels. Native provider auth belongs
to the guest agent and its actual browser callback endpoint (#45).

**Why this shape:**

| Candidate | Isolation | GPU / models | Startup | Workspace I/O | Verdict |
|---|---|---|---|---|---|
| Local QEMU/KVM, `./lab`-managed | VM boundary; agent has broad guest control; no host ports; one exported path | Host-side model API, no VFIO | One command, image cached | virtiofs, live edits | **Chosen** |
| Local default + remote Linux host over SSH | Shares the remote host's VM/daemon/network rather than this local VM boundary | Depends on host | Fast after provisioning | sshfs/rsync, extra moving parts | Follow-up: #47 |
| Cloud VM | Strong, but provider credentials and cost | GPU SKUs available | Slowest | Network filesystem or copy | Not now |
| Docker-in-Docker on the host | Needs privileged containers; weak boundary | Host Docker | Fast | bind mount | Rejected (C.6) |
| Shared remote Docker daemon | Containers/networks/volumes shared with other work | Host Docker | Fast | bind mount | Rejected (C.6) |

**Concrete choices:**

- **Hypervisor:** `qemu-system-x86_64` driven directly by `./lab`, running as the invoking
  user with KVM (`/dev/kvm`). No libvirt daemon. This keeps the VM lifecycle scriptable,
  makes `virtiofsd` ownership match the workspace, and avoids a privileged host service.
  If KVM is unavailable, `./lab` refuses rather than silently falling back to unusably slow
  TCG emulation.
- **Guest:** a pinned cloud image with a per-VM qcow2 overlay and a cloud-init NoCloud
  seed (user, SSH key, packages). The base image is cached; the overlay is disposable.
  `qemu-guest-agent` provides clean shutdown.
- **Workspace transport:** `virtiofsd` exports exactly the directory selected on the host
  (`--shared-dir`, sandboxed with its namespace mode where the host permits it, otherwise
  the narrowest mode available) and QEMU exposes it as a `vhost-user-fs-pci` device.
  The guest mounts tag `workspace` at `/workspace`. The agent and workspace containers see
  that mount; nothing else from the host is exported. `check_workspace()` still runs on the
  host before export, so `/`, `$HOME`, the lab repo without `--unsafe-recurse`, and
  credential directories never reach the VM.
- **Guest authority:** the harness runs directly as a guest user with the ability to
  administer the guest and control its Docker daemon. Rootful-in-guest Docker or equivalent
  guest admin access is acceptable because the outer VM is the isolation boundary; guest
  root is not claimed as a security boundary from the agent. Host Docker and host privilege
  are never exposed.
- **Control channel:** SSH over a QEMU user-network forward bound to `127.0.0.1` on an
  ephemeral host port. The intended private per-user state directory holds per-VM keys
  and `known_hosts`; current profile paths are private to the host user's state root and
  are not exported through the selected workspace. Host keys,
  cloud credentials, and `~/.ssh` are never copied into the VM. `qemu-guest-agent` handles
  lifecycle; SSH handles commands and shell access.
- **Model APIs:** Model service is optional and configured independently of the VM runtime.
  When selected, the VM may reach exactly one configured host-loopback API endpoint through
  an explicit QEMU `guestfwd` mapping, not through a general host route. A native host
  service or user-managed container may provide it; host Docker is not a prerequisite and
  `lab-vm` does not implicitly install/manage a host model server. No model weights or GPUs
  enter the VM; a VM without a model endpoint remains usable for shell/workload tasks.
- **Egress:** QEMU user networking runs with `restrict=on`; the only Internet path is a
  host-side HTTP(S) proxy exposed through `guestfwd` (`10.0.2.100:3128`). Docker, apt, npm,
  and pip use it. A blank `.mester-egress` `allowed_domains` permits any public HTTP(S)
  hostname; a populated list narrows destinations. The proxy rejects non-public addresses,
  mixed DNS, and the host's local/directly connected networks before connecting. Non-HTTP
  protocols and DNS outside the proxy remain denied.
- **No host LAN ports:** the harness adds only loopback SSH control and any explicitly
  configured loopback model endpoint. That endpoint crosses to the guest only through its
  matching `guestfwd` entry. Guest services remain guest-local unless separately reviewed;
  nothing is published to the host LAN. Setup must never modify host firewall, NAT, routing,
  sysctl, or Docker daemon policy to make a crossing work; a profile that cannot satisfy
  the contract without such changes is unsupported.
- **Teardown and recursion:** guest workloads and nested VM state belong to the guest
  lifecycle. Cleanup must be explicit and must not silently destroy a live recursive test
  loop or its evidence. Separate/reused VM policy and snapshot/overlay retention require
  lifecycle acceptance tests; no automatic destructive teardown is assumed.

**Follow-ups (with defaults):**

- A remote Linux host target is **#47**: opt-in, off by default, and documented as weaker
  isolation because it shares that host's Docker daemon. Default remains local QEMU/KVM.
- Nested virtualization is a requirement to investigate and prove, not an assumed feature:
  test host nested-VMX/SVM exposure, guest `/dev/kvm`, and a nested boot/lifecycle before
  claiming recursive support. Provide a clear unsupported/fallback result when unavailable.
- Guest Docker/Compose must support normal project workflows (including Compose services,
  ports within the guest, volumes and test start/stop cycles); the guest Docker daemon may be
  rootful because it is contained by the outer VM. Never expose the host daemon.
- GPU passthrough into the VM is explicitly out of scope; model servers stay on the host.
- `guestfwd` + `restrict=on` behaviour and workspace-sharing semantics must be proven in
  #41. There is no firewall or general-route fallback; an unsupported host profile must
  fail clearly rather than weaken isolation.
- Alternative container runtimes remain future scope in #46.

#### C.9.15 Host portability and optional model services (2026-10-28)

**Decision:** The VM product must not be coupled to one host's Docker Engine, firewall,
networking setup, or model-serving container. Guest Docker/Compose remains an optional
workload capability for project integration tests; it is not the agent wrapper and the host
daemon/socket is never shared. Model inference is an optional provider configured through a
stable API endpoint contract; when local to the host, only that endpoint's loopback port may
be forwarded to the guest. Default setup/agent launch must not require any model server.

“Any Unix” is a portability goal, not a claim that all Unix variants share VM and filesystem
APIs. Publish explicit tested OS/architecture profiles. Each profile must pass hypervisor,
selected-workspace share, control/network isolation, and teardown gates. Unknown profiles
fail with actionable prerequisites; setup must not alter firewall/routing policy or silently
select weaker isolation. The current Linux x86_64/QEMU/virtiofs profile remains experimental
until its acceptance evidence is complete; macOS and BSD support must not be claimed before
their own profile tests.

**Reason:** A host Docker port-publication failure must be diagnosed as an implementation or
support-profile failure, not worked around with host-specific relays or firewall changes.
Keeping the host model provider and guest workload runtime behind optional interfaces allows
the same VM flow to work with native host services and across tested Unix profiles.

#### C.9.17 VM-first launcher, retained Docker mode, and bounded recursion (2026-11-01)

**Owner-directed product decision:** `./lab <workspace>` is the VM-first default entry
point and should start/reuse the guest and launch the interactive agent. Preserve the
existing containerized single-agent workflow separately as `./lab-docker`; it is not
Docker-in-Docker and must not acquire a host Docker socket or recursive agent capability.
This decision supersedes the earlier decision to postpone switching the default until all
acceptance gates passed. The staged dispatcher is implemented: `./lab vm <args>` reaches
`./lab-vm`, and the former host-Docker launcher is `./lab-docker`. Ordinary `./lab` still
delegates to Docker in this worktree, so the VM-default cutover is not complete. Do not
describe the branch as accepted or merge-ready until the independent VM, auth, permission,
workload, isolation, lifecycle, CI, and owner-review gates pass. Preserve a tested rollback
path.

**Setup versus daily use:** Host prerequisites, base-image download, and first guest
provisioning are setup-time operations. Daily startup should reuse the provisioned VM, perform
only bounded readiness/config checks, and land the user directly in the guest agent; `ssh`,
`status`, `base`, and other component commands remain available for diagnostics. The normal
daily path must not require a separate `up`, `ssh`, then `agent` sequence. VM commands must
document whether they preserve or delete the writable overlay, guest Docker volumes, auth
state, and logs. A non-destructive stop/suspend/restart policy must be implemented and
verified before documenting an end-of-day shutdown recommendation. In the current
experimental implementation, `up` provisions/starts but does not launch the TUI; `stop`
tears down guest Compose volumes and then calls `down`; `down` deletes the VM overlay and
control keys. These commands are destructive to guest state and are not a routine end-of-day
stop.

**Metrics compatibility:** Keep the existing host-Docker metrics integration working while
developing and validating a separate VM metrics transport. VM metrics must use an explicitly
configured, narrow endpoint/stream and preserve the host boundary; the guest must not depend
on Compose DNS names such as `metrics`, host Docker access, or a general host route. Only
retire or replace the Docker metrics path after both modes have their own verified acceptance
coverage and the owner approves the transition.

**Self-hosting and recursion:** Mount only the selected workspace by default; do not include
the Little Mester checkout in a VM workspace unless recursion is explicitly requested.
`--recurse` enables the bounded nested Little Mester test capability, while
`--unsafe-recurse` explicitly permits self-modification and implies the required repository
workspace. Both are governed by a maximum nesting depth, default `0`, configurable only via
an explicit maximum-depth option. Depth is propagated and enforced at every launcher boundary;
no child can reset/increase it implicitly, and exceeding the limit fails closed before VM or
agent startup. Recursive tests use a distinct disposable workspace/checkout so the active
outer implementation session remains intact. The outer VM remains the host boundary; nested
VMs do not gain additional host paths, host Docker, credentials, LAN access, or egress rights.

**Implementation note (2026-11-01):** the explicit `./lab vm <args>` dispatcher now reaches the
standalone VM CLI, and the former host-Docker implementation is split out as `./lab-docker`.
Ordinary `./lab` remains on the host-Docker compatibility path while the project release gates
are open; this is not the final VM-first default cutover or an acceptance claim.

**Implementation contract:** `lab-vm` accepts `--max-depth` from 0 through 32; the default is
0, and a nested invocation inherits the parent maximum unless explicitly lowered. A safe
repo-related workspace is copied to a stable private snapshot before it is shared; snapshots
exclude `.mester` and `.little-mester.conf`, use independent Git metadata, and reject escaping
symlinks and special files. Nested VM overlays/state are stored separately under the guest
user's `~/.cache/lab-vm-recursion/<profile>` and persist until the explicit
`recursion-clean <profile>` command is confirmed. Nested startup requires usable `/dev/kvm` and
guest CPU `vmx`/`svm` flags, and fails before snapshot/provisioning when either is unavailable;
it must not fall back to TCG for a recursive child. This implementation detail does not claim
live nested-KVM or isolation acceptance; see #43/#52. The cache is inside the containing guest
overlay: destructive outer `down`/`rebuild` can still remove it and must be treated as an
independent destructive action until #43 A6/#52 lifecycle acceptance closes.
When profile recursion metadata shows that descendants may exist, `down`, `stop`, `rebuild`,
and `up --clean` refuse before destructive side effects unless the caller supplies
`--discard-recursive`; `--yes` is not a substitute for this acknowledgement. The persisted
“descendants may exist” bit is sticky if a profile's configured maximum is later lowered, and
is cleared only when that profile's writable overlay is discarded.

**Reason:** Make the intended VM the ordinary route without losing the useful, non-recursive
Docker fallback; maintain metrics parity during migration; and support deliberate
self-improvement without accidental or infinite agent/VM recursion. Implementation and
acceptance evidence are tracked on #35, #41–#43, #50, and #52.

The trust-boundary diagram and crossing table live in `docs/vm-architecture.md`; the
engineering invariants are in `docs/tickets/architecture/DECISIONS.md` §K.

#### C.9.18 Harness-neutral, data-first tooling (2026-11-01)

**Decision:** Little Mester's operational workflows and project state should be available
through stable, machine-oriented interfaces—CLI and/or MCP—with one canonical data model.
Harness-specific integrations (OpenCode, Claude Code, Codex, or others) are adapters, not the
source of truth; they should invoke the same operations and expose the same validation/results.
Keep semantic decisions in versioned schemas, engine rules, and explicit data rather than
prompts, agent-specific memory, or undocumented conventions. Natural-language interaction may
help a user choose an operation, but must not be required to interpret or mutate canonical state.

For ticket work, lifecycle status remains distinct from delivery/merge readiness. When a broad
objective contains a separately integrable slice, split the remaining scope into separate tickets
rather than marking unfinished work resolved. The current ticket schema/CLI/MCP do not yet offer
a complete, equivalent machine-readable readiness-and-split workflow; do not invent an ad-hoc
field or treat prose as authoritative. Ticket #57 tracks that schema/engine/CLI/MCP improvement.

**Reason:** Reduce coupling to any one coding harness and minimize semantic state that must be
reconstructed from conversation. A canonical programmatic workflow makes work portable between
humans, agents, and harnesses and makes status auditable/queryable.

#### C.9.19 Shell portability in user instructions (2026-11-01)

**Decision:** User-facing command instructions must include both Bash and Fish forms. When a
sequence is identical in both shells, explicitly label it as shared; when syntax differs (for
example assignments, command substitution, loops, or environment-variable prefixes), show
separate complete blocks. Do not assume the user's interactive shell is Bash.

**Reason:** Bash-only snippets fail when copied into Fish, interrupting otherwise safe workflows.

#### C.9.20 Managed VM memory resize (2026-11-01)

`./lab-vm resize <profile> <GiB>` changes RAM for a VM profile controlled by that
invocation of `lab-vm`. QEMU RAM is not hot-resized: for a running profile, the command
must verify the requested budget first, request a clean guest shutdown, stop host-side
VM helpers, preserve the overlay/base image/seed/SSH identity/installed guest state, and
restart the same profile with the new RAM. If guest shutdown or resource preflight fails,
leave the current VM running and unchanged. Never implement resize by `down`, `rebuild`,
container-volume teardown, or overlay replacement.

This command cannot resize an ancestor VM that contains the current shell: `lab-vm`
controls only QEMU profiles it launches in the current host namespace. Assigning more RAM
to the coding shell itself requires its parent hypervisor/launch configuration and a
shutdown/restart there.

**Reason:** Permit deliberate RAM changes without losing useful guest state or confusing
a nested guest with the hypervisor that owns its parent.

#### C.9.21 VM memory defaults, per-profile persistence, and setup override (2026-11-01)

New depth-0 VM profiles default to **8 GiB**. New nested profiles default to half the
parent-level allocation (8/4/2/1 GiB by depth, with 1 GiB as the floor); `--memory <GiB>`
is an explicit setup-time override for a new profile. The selected allocation is persisted
with that VM profile and reused on warm starts. Existing profiles without metadata retain
the legacy 4-GiB allocation until explicitly resized. Changing a running VM's RAM requires
`resize`; an override must not silently affect a running or existing profile.

Before creating recursive snapshots, profile state, or QEMU processes—and before stopping
a running profile to resize—check the available parent memory, any enforced cgroup memory
limit, the requested guest RAM, and 512 MiB of safety headroom. Refuse before side effects
when the budget is insufficient; do not rely on host swap or QEMU overcommit. Resource
checks are safety gates, not proof that the guest workload fits its configured RAM.

**Reason:** Keep the ordinary new guest useful while scaling deliberate recursion to fit
inside its parent, preserve a per-profile operator override, and fail before disturbing
valuable VM/workspace state when the enclosing machine cannot satisfy the request.

#### C.9.22 Recursive source snapshots are filtered, bounded, and revision-bound (2026-11-01)

An explicitly requested recursive child receives a private copy of the selected source, not
the parent's state directory or a live shared checkout. Exclude `.mester`, `secrets.env`,
`.env` files (except checked-in examples), auth/database files, private-key files, model
weights, SQLite/database files, and regenerated dependency/runtime caches. Reject escaping
symlinks and special files. Rewrite in-workspace symlinks to relative links within the child
snapshot so they cannot point back to the parent checkout. Do not clone the source repository's
history into the child: historical committed
credentials must not be reachable through its Git object database. Preserve only a synthetic
Git commit of the filtered current tree, including intended tracked, dirty, and untracked
source files.

Record the source HEAD plus a content manifest/hash outside the child's workspace. Before
reusing a session, compare the current selected tree and refuse if its source revision changed;
use a fresh profile for a new test attempt. Preflight the included file bytes against a 2-GiB
default maximum and filesystem free space for the snapshot plus staging reserve before copying.
An explicit `LAB_VM_MAX_SNAPSHOT_BYTES` may raise the cap up to 16 GiB; it never bypasses the
free-space check. Snapshot preparation must fail closed without replacing an existing session.

**Reason:** The child should test the parent's selected revision and intended work without
inheriting host credentials, stale source, model caches, or unbounded disk consumption. This
filtered child copy does not make a parent workspace safe if old checkout-local runtime data is
still present; the launcher refuses that overlap until it is reviewed.

#### C.9.23 VM control state is private to the host user (2026-11-01)

VM profiles and base disks live under `~/.local/share/little-mester/vm/` outside every selected
workspace; launcher logs are under `~/.local/share/little-mester/logs/`. The root may be
relocated by `XDG_DATA_HOME` or `MESTER_STATE_DIR`. Restrict the state root and profile
directories to mode 0700; individual control keys remain mode 0600. Nested profiles remain
under the containing guest's private recursion session. Before sharing a workspace, reject any
overlap with selected VM state or an unreviewed checkout-local `.mester` directory.

There is no checkout-local fallback: old overlays and keys are neither discovered, moved, nor
deleted automatically. Before a manual migration, inventory all profiles and each qcow2 backing
chain. If an expected profile does not appear under the effective profile root printed by
`./lab-vm context`, stop before provisioning another profile.

#### C.9.24 Guest recursion identity and MCP runtime persist independently of the workspace (2026-11-01)

Provision a root-owned recursion identity in guest OS state and load it into ordinary login
shells and launcher processes. A caller-supplied recursion environment that disagrees with
the persisted depth/maximum is refused. `lab-vm context` reports the persisted identity even
when invoked outside the agent's original SSH command. It also reports the effective VM state
root and profile directory. The maximum remains bounded at 32 and a child cannot raise its
inherited maximum.

The direct guest MCP server uses a guest-private runtime copy, not workspace `node_modules`.
Provision Node.js 20 or newer, install dependencies from the checked-in lockfile, and build the
MCP server before launch. Do not copy guest dependencies/caches into recursive source
snapshots or mutate the selected workspace as a side effect of setup.

#### C.9.25 One per-user state root; checkout clones contain source only (2026-11-02)

The canonical host root defaults to `~/.local/share/little-mester`. VM profiles and base disks
are under `vm/`; VM per-profile logs are stored with those profiles, and both `lab-vm` and
`lab-docker` launcher logs are under `logs/`. Metrics service files use `vm-services/` under the
same root. `XDG_DATA_HOME` or `MESTER_STATE_DIR` relocates the whole root. Host acceptance
reports are private evidence under the XDG state directory and are not VM runtime logs.

`VM_STATE_DIR` and `VM_DIR_BASE` remain explicit internal/test/recursion overrides, not a
checkout-discovery mechanism. The default is never changed based on whether another directory
exists. `./lab-vm context` reports the effective state/profile paths, so an operator can verify
which location a command will use before provisioning or reopening.

Re-cloning the source on the same host preserves home-directory profiles. `resume` uses the
profile's recorded workspace; if that path moved, the operator selects the new workspace
explicitly with `up --workspace`. Do not automatically move old profile data: inventory the
overlay's qcow2 backing chain first. `.mester-egress` remains an independent workspace policy
file and is not part of this runtime root.

**Reason:** Keep private VM control state out of a writable self-development workspace while
making the location and re-clone behavior predictable. `--unsafe-recurse` authorizes source
workspace writes, not access to the parent VM's keys, disks, or control metadata.

---

### C.9.14 Diagnostics are part of the workflow (2026-10-28)

**Decision:** Software setup and lifecycle workflows must gather and present actionable
diagnostics as part of the normal workflow, especially on failure. A user must not need to
race a short-lived process with a separate SSH/debug command to collect evidence needed to
diagnose a failure. `--verbose` and `--debug` may add progressively more detail, but the
failure path must automatically capture the minimum useful context, preserve it in the
workflow's existing state/log directory, print its location, and retain the original failure
status. Diagnostics should be configurable when their volume or cost makes routine capture
excessive, with the default still sufficient to explain common setup failures.

**Safety:** Diagnostics must not print secrets (credentials, tokens, private keys), weaken
security boundaries, or run unrelated/destructive commands. Bound noisy command output where
appropriate, identify unavailable probes rather than silently omitting them, and keep raw
artifacts in gitignored state/log locations. Implementations and evidence belong on their
ticket; this is the cross-project product/workflow rule. See #41 for VM failure diagnostics.

When an investigation requires the same multi-step host checks more than once, add a reusable
diagnostic command to the relevant tool rather than asking the owner to compose bespoke shell
commands. Keep ordinary `status` output concise and actionable; put detailed, read-only evidence
behind an explicit `diagnose`/`doctor` command (or a documented verbosity flag). Agents should
run the existing diagnostic entry point first and request extra manual commands only when it
does not cover a specific evidence gap. Diagnostic helpers must not repair or restart services
unless the user explicitly invokes a separate repair action.

## Licensing — standard AGPL (2026-10-28)

**Decision:** Replace the root MIT licence and the MCP package's ISC declaration
with standard **GNU Affero General Public License version 3 only**
(`AGPL-3.0-only`). There are no additional restrictions, mandatory
payments, or separate commercial-licensing terms in this change.

- Preserve Ethan Freestone's 2026 copyright notice in the project and module docs.
- Use the complete, unmodified AGPL text in root `LICENSE`. Keep an identical copy
  in `little-mester-mcp/LICENSE` because npm packages must carry their own licence
  document when distributed independently of this repository.
- Align `package.json` and the root package entry in `package-lock.json` with the
  SPDX identifier. Leave third-party dependency licences unchanged.
- Explain commercial use/resale, distribution and modified-network-version source
  obligations, private local modifications, and independent project output in the
  README. Existing MIT/ISC grants for previously published versions remain valid.
- Extend `npm run check:package` to check metadata, identical licence copies, and
  inclusion of `LICENSE` in the npm tarball before considering the change verified.

---

## D. Fields & categories — what each means and when to use it

> This section resolves the "project manager's question": **what does each category field actually mean, what are they FOR, and when should you use which?**
> These decisions apply to human use of tickets AND to engine validation logic. Both must agree.

### D.1 The taxonomy (how the system organizes work)

Every ticket passes through TWO independent classification systems: **lifecycle fields** (status, type, area) that define WHAT the ticket IS and WHERE it is in its life, and **labels** (scope, severity) that are cross-cutting categorization tools you apply ON TOP.

#### Lifecycle fields — core identity of a ticket

| Field | What it answers | Format | When to set/change |
|-------|-----------------|--------|--------------------|
| **area** | "WHAT domain is this?" — human grouping. Like a folder or project area. | Free-text string (recommend lowercase, hyphens for multi-word) | Set once when creating the ticket. Do NOT change after — if the ticket moves domains, create a new ticket in the correct area rather than moving it and corrupting search/filter. |
| **type** | "WHAT KIND of work is this?" — affects how the engine gates progression. | Enum (see D.2 below) | Set on creation. May be changed between draft↔active only if re-gating is acceptable. Use `spike` when you need research BEFORE committing to a decision, NOT for implementation. |
| **status** | "WHERE in its life is this ticket?" — lifecycle progression marker. Not metadata, not classification. It's the ticket's state machine position. See D.3 for exact semantics. | Enum (see D.3 below) | Changed by you (or auto-computed for epics). Every transition follows `draft → active → resolved → archived` with specific gates. Status DOES determine what search results appear where. |

#### Labels — cross-cutting categorization applied ON TOP

| Field | What it answers | Format | When to use |
|-------|-----------------|--------|-------------|
| **scope** (label set) | "WHAT PART OF THE SYSTEM does this touch?" — technical boundary, not human organization. Helps route tickets to the right domain expert or model. | `scope/<value>` from predefined set: `agent`, `engine`, `infra`, `docs`, `tests`, `security`, `performance` OR free-form `user/custom-label`. Use when a ticket spans multiple areas (e.g., `type/feature scope/engine scope/performance`) — not for every ticket, only for cross-cutting ones. |
| **severity** (label set) | "HOW URGENT is this?" — triage signal independent of type or area. A `scope/engine` ticket could be severity/trivial or severity/critical depending on impact. | `severity/<value>` from predefined set: `trivial`, `minor`, `major`, `critical` OR free-form `user/custom-label`. Use when prioritizing from a backlog, during triage reviews, or when work needs attention that is out of normal flow. |

#### Why labels use the `prefix/value` format

Labels are scoped by namespace to avoid collisions between independent label sets. `type/feature` is different from `scope/engine` — even though both use `/` notation, they come from different predefined pools. The engine validates each prefix against its expected value set.

### D.2 Type enum — expanded to 6 values

**Decision (G.1 resolution):** `type` is a top-level required field with these 6 allowed values:

| Value | Meaning | How the engine treats it differently |
|-------|---------|--------------------------------------|
| **spike** | Research/exploration task. Output is findings, NOT implementation code. No code path. Use this WHENEVER you need to answer a "should we" or "how" question before committing to a `feature` or `bug` ticket that implements it. | Spike tickets do not gate downstream work. Other tickets should depend on the spike being resolved BEFORE they start. The engine may surface pending spikes differently in search views (e.g., "blocked research" vs active features). |
| **feature** | New functionality being built. Implementing something that didn't exist before. | Standard feature flow: draft → active → resolved → archived |
| **bug** | Defect fix or regression resolution. Something that was working and broke, or behavior not matching the spec. | Standard bug flow; engine may surface bugs in high-priority views faster than features |
| **improvement** | Non-functional enhancement — performance tuning, code quality, refactoring with no user-visible behavioral change (or one where the behavior is unchanged). | Same lifecycle but tracked separately from features for reporting: "how much of our work was pure improvement vs new features?" |
| **doc** | Documentation-only task with no code changes expected. | May be auto-resolved faster if no testing gates apply to documentation tickets. No engine-side dependency chains on purely doc tickets unless you add them manually. |
| **infra** | Infrastructure work — tooling, CI, deployment, setup, configuration changes. Affects the development environment rather than product features. | Same lifecycle as feature but grouped separately in area dashboard reports: "how much capacity is going to infra?" |

### D.3 Status field semantics — formal definition (G.3 resolution)

> This is THE authoritative definition of status meanings and transitions. Supersedes any informal references in other documents.

Each status has three things defined: its **meaning**, what **transition gates** apply, and **what it triggers**.

| Status | Meaning | Allowed Transitions (from here → to) | Transition Gate (when entering) | What It Triggers |
|--------|---------|--------------------------------------|----------------------------------|-------------------|
| **draft** | Ticket exists but work has NOT begun. Created state for every new ticket. Can be revisited if a ticket leaves `active`. | → active, → archived | None (create is always allowed). Entering from resolved means re-open — no dependency checks needed. | If this was previously `resolved`, the engine resets any auto-computed fields (spike research may need refreshing). If it has dependents that were "unblocked" by previous resolution, they are NOT automatically re-blocked (you must assess manually). |
| **active** | Work IS IN PROGRESS on this ticket. You (or an agent) have explicitly started implementing the objective. | → resolved, → draft | **Blocked if any dependency is NOT resolved/archived.** The engine checks `blocked_by` chain transitively. If blocked, no transition allowed and `reason` explains which tickets block progress. | Engine may auto-suggest related work (tickets depending on this one are "next in chain"). Area stats count toward "active capacity." |
| **resolved** | Work is COMPLETE but awaiting confirmation or closure. Not yet archived — still visible in reports, queryable for history. | → draft (reopen), → archived | None by default. Epics: status auto-computed from children — cannot be manually moved to resolved until all children are resolved OR overridden per C.3 of architecture DECISIONS. | Resolved tickets become "candidates for archival." If they stay unresolved past a threshold (configurable, default 30 days), they appear in aging reports for review. |
| **archived** | Terminal state. Ticket removed from active cycle. Read-only from this point — cannot be transitioned FROM archived to anything else. The ticket file still exists on disk but the engine treats it as immutable history. | NO transitions out. Only → archived is allowed (from any status). | None. Anyone can archive (no gate check). The gate check before moving to resolved handles dependency completeness instead. | Archived tickets drop out of default search results (include `include_archived=true` to find them). No capacity impact. Search still works on historical data if included explicitly. Epic archival cascades but does not affect children (children continue independently). |

#### Diagram of all valid transitions:
```
draft ──► active ──► resolved ──► archived
  ▲         ╲       ↗             ^
  └────────━━┘────/              │
                               any status ──/
```

**Key design decision:** Status is a simple state machine ONLY. No dependency chains or business logic in the transitions themselves (dependency checks happen BEFORE entering `active`, not as part of the transition). The lifecycle path is intentionally flat: forward, backward to draft once, and terminal at archived. Anything more complex belongs in epics + dependencies, not status itself.

### D.4 The tag problem — explicit decision

**Decision:** There is NO separate "tag" field. All tagging falls under `labels[]` with the namespace prefix system described in D.1. If you need ad-hoc classification that doesn't fit into scope/severity/any known set, use `user/custom-tag`. This prevents "tag proliferation" where every person invents their own classification without shared vocabulary.

Labels with a `user/` prefix have NO validation beyond the prefix check. They are stored as-is — useful for temporary workstreams, one-off tasks, or ideas you want to categorize but haven't settled into a formal label set yet.

---

## E. Models used per domain — pre-defined set for label tracking

You said you want to track **actual model usage** and use those stats for estimation. Here's what that means for labels.

### Label sets (predefined categories)

| Predefined Set | Values | When to use it |
|---------------|--------|----------------|
| **type** | `spike`, `feature`, `bug`, `improvement`, `doc`, `infra` (now expanded per D.2 above) | What KIND of work this is. Use when creating a ticket. `spike` = research/planning only (no code output). All others = actual implementation work. |
| **severity** | `trivial`, `minor`, `major`, `critical` | How urgently does this need attention? `critical` = blocking, must do now. `trivial` = can wait. Use when triaging or prioritizing from a backlog. |
| **scope** | `agent`, `engine`, `infra`, `docs`, `tests`, `security`, `performance` | What PART OF THE SYSTEM this touches. Cross-cutting categorization. A ticket about engine performance might get BOTH `type/feature` AND `scope/engine` + `scope/performance`. |

### Field for model tracking

**`models_used`** (array) — records which models were used on this ticket, how long spent on each, and the actual measured result. This is **automatic data from opencode**, not something you manually fill in.

Per-model stats tracked automatically:
- `model` — e.g., `qwen3.6:35b-a3b`
- `thinking_time_seconds` — how long the model was THINKING (not idle time)
- `idle_time_seconds` — time waiting for input/prompts (tracked separately, not part of thinking time)
- `cost_estimate_usd` — estimated cost based on token usage
- `context_window_used_pct` — what percentage of the context window was consumed

### Derived stats from model tracking

The engine computes these automatically when you query:
- **Estimated remaining** — `estimated_total - actual_spent`, recalculated per-model using actual throughput data
- **Time by model** — how long each model spent on a ticket (for capacity planning)
- **Suggested model for future tickets in this area** — based on historical best performance metric

### Suggested model field vs estimated model

These are related but NOT the same:

| Field | Meaning | Computed by/When set |
|-------|---------|----------------------|
| `suggested_model` (string) | Which model we **recommend** for this task based on historical data. "In this area, 70% of past work used qwen3.6:35b-a3b → that's the suggestion." | Auto-computed from `models_used[]` history when queried; can be overridden by human at creation time |
| `estimated_models` (array) | Which model(s) are projected for this specific ticket. For complexity-split tickets, lists 2+ models with subtask boundaries: `[{ model, subtask_hint }]` | Set during estimation phase (after spike); may be empty if no one has estimated yet |

**Why the distinction:** `suggested_model` is a general recommendation ("for this area, use X"). `estimated_models` is ticket-specific ("for THIS task, split: implement auth logic with X, write tests with Y"). A ticket about "migrate infra to containerize" might have `suggested_model: "qwen3.6:35b-a3b"` (area history) but `estimated_models: [{ model: "qwen3:8b", subtask_hint: "write config files and tests" }]` because that smaller model is adequate for the specific work.

### Complexity-based splitting hints

When a ticket's estimated model-usage exceeds a threshold (e.g., > 4 hours of combined thinking_time across any single model), it should be split into smaller tickets. The `estimated_models` field becomes an array listing 2+ models where each handles one subtask, with suggested boundaries:
```json
{
  "estimated_models": [
    { "model": "qwen3.6:35b-a3b", "subtask_hint": "implement auth logic" },
    { "model": "qwen3:8b", "subtask_hint": "write tests + docs" }
  ]
}
```

---

## F. Archive policy

Sections archived when: **(1)** >90 days old without new context, **(2)** all associated work done, **(3)** explicitly superseded by later decision. Archives are append-only — never rewritten. Redirect link left in place.

---

## G. Engineering Index Format Decision (G.9 resolution)

### `_index.json` canonical format

**Decision:** Adopt a hybrid approach that preserves SCHEMA.md's `schemaVersion` naming convention while including blueprint's `next_number` for deterministic ID generation (critical for parallel agents).

```json
{
  "schemaVersion": 1,
  "tickets": [
    {
      "number": 42,
      "file_path": "ticket-042.json",
      "status": "draft",
      "area": "auth"
    }
  ],
  "next_number": 43,
  "updated_at": "2026-10-15T09:30:00Z"
}
```

**Rationale for keeping `next_number`:** The engine must generate unique ticket numbers deterministically when multiple agents are creating tickets in parallel (or a human and an agent). Recomputing `next_number` from file scan is safe but not deterministic under concurrent modification. Storing it in the index allows optimistic concurrency: read → use → write with version check. If conflicting writers are possible (future-ticket system), this field becomes the concurrency token.

**What's NOT in `_index.json`:** Unlike blueprint.md's proposal, we do NOT duplicate `type` or `title` in every IndexEntry. Those live exclusively in the ticket JSON file. The index tracks only: number, file_path, status, area — sufficient for filtering and fast listing without reading every ticket file.

---

## H. Field precedence decision (G.10 resolution)

### priority vs severity — decided

**Decision:** `priority` is the primary top-level field with values matching scope's predefined set: `trivial`, `minor`, `major`, `critical`. The engine uses `priority` for sorting, filtering, and board views.

`severity` remains as a **label namespace only** (`severity/critical`, etc.) for cross-cutting classification that can be combined with other labels. In practice, most users will set both `priority` (top-level) and `severity/<value>` (label) to the same value — this is intentional and not redundant:
- `priority` on `feature/bug` controls how the ticket appears in board views and search results
- `severity/critical` on a label can be queried AND combined with other labels (`severity/critical scope/security`) for multi-criteria filters that `priority` alone cannot express

**Why keep both:** Not every classification question maps cleanly to "urgency." A ticket about engine performance might have priority=minor (not urgent) but severity/major (high impact if it breaks). The label-based severity is the broader signal; priority is the triage shorthand.

### Gaps documented elsewhere (from GAP_AUDIT.md)

| # | Reference | Status |
|---|-----------|--------|
| G.4 | Labels vs top-level fields — resolved by D.1 above (labels = cross-cutting, lifecycle fields = core identity) | RESOLVED |
| G.5 | Future-ticket automation rules | DEFERRED — explicitly out of current scope; will be documented in a future decision log |
| G.7 | Ticket numbering edge cases (deletion, multi-repo collision avoidance) | Documented: `next_number` is monotonically increasing and never decrements. Two routing modes sharing the same number space must coordinate through shared `_index.json`; each mode has its own index. When tickets are deleted, their numbers are NOT recycled (like GitHub issue numbers). If `_index.json` is corrupted/lost, `write_from_scratch()` recomputes from file scan and bumps next_number to max+1 — safe but irreversible for old gaps. |
| G.8 | Epic auto-computed status persist vs compute | See architecture/DECISIONS.md C.1: composite status is computed on read (not persisted). The epic's `status` field in its JSON reflects its last manual change. When querying, the engine returns composite_status separately from status. This prevents sync conflicts with multi-engine modes while preserving manual override capability. |
| G.11 | Status claims vs actual artifacts | Documented as informational note in GAP_AUDIT.md — not actionable without a full audit |
| G.12 | Ticket-009 cancelled, noting where references remain | ticket-012 tool list; resolved by removing git auth tools from 012 scope (deferred to future ticket) |

---

## I. Ordering vs ID decision (conversation: "I don't care about order")

**Decision:** Ticket numbers (`number` field, `next_number` in index) are **IDs only**, not ordering indices. There is no semantic meaning to their numeric value other than uniqueness and monotonic increase. Ordering of tickets within a view comes from:
- `updated_at` (last modified — default for activity-based views)
- `created_at` (creation time — chronological ordering)
- `priority` (triage-level ordering in search/boards)
- Dependency chains (epic children ordered by dependency, not by number)
- Manual sort parameters in search queries

The ticket tree visualization (future-ticket system, ticket-015 stretch goal) renders structure via dependencies and epic parent-child relationships, NOT via ticket numbers.

---

## I. Sub-system decision logs

| Subsystem | Location | Scope | Per-ticket plans point to... |
|-----------|----------|-------|------------------------------|
| **How to use** (this file) | `docs/DECISIONS.md` | What's true NOW about little-mester as a product | All decisions in this file are facts — no designs |
| **little-mester-mcp subsystem** | `docs/tickets/architecture/DECISIONS.md` | Engineering designs scoped to ticket-engine v1 build | The engine tickets (008, 007, 014, etc.) |
| **future-ticket system** | (not yet) | When we redesign the ticketing system entirely (rebuild from scratch) | Separate — not yet needed |

**Architecture decision (C.9 in architecture/DECISIONS.md):** Little Mester MCP is a **single MCP server** (`@little-mester/mcp`) with the ticket engine as its **first module** (`@little-mester/ticket-engine-mcp`). All tools use the `mester_tickets_<action>` prefix. Future modules (file search, notes, etc.) register on the same server — no multiple MCP servers per session.

Per-ticket decisions go in each ticket file itself. When a decision spans multiple tickets or subsystem, write it in the appropriate sub-system file above.

---

## J. OpenCode customisation (v2)

### Current status
- Done: logo plugin rewritten to the v2 plugin contract; config wiring moved to v2 mechanisms (J.2).
- Not confirmed: whether the logo actually renders on the home screen (see J.4).
- Rule: record test results in J.4. Do not add assumptions to this section.

### J.1 We run OpenCode v2. Do not use v1 docs or examples.

We install `@opencode/cli` (v2). The `opencode-ai` package is v1. Most search results, blog posts and plugin repos describe v1, and **v1 plugin code does not run in v2**.

| | v1 (do NOT use) | v2 (use this) |
|---|---|---|
| TUI/CLI config file | `tui.json` | `cli.json` (global only, no project-local file) |
| Plugin list key | `"plugin": [...]` | `"plugins": [...]` |
| Config location override | `OPENCODE_TUI_CONFIG` | `OPENCODE_CLI_CONFIG_CONTENT` (inline JSON) |
| Package import | `@opencode-ai/plugin/tui` | `@opencode/plugin/tui` |
| Entry point | `export default { id, tui(api) }` | `export default Plugin.define({ id, setup(context) })` |
| Slot registration | `api.slots.register({ slots: { home_logo } })` | `context.ui.slot({ <placement>: "<slot>", render })` |
| Slot names | `home_logo`, `sidebar_content` | dotted: `home.footer`, `sidebar.content`, ... |

A legacy `tui.json` is read **only once by the migration routine** to populate `cli.json`
(`packages/cli/src/config/migrate.ts`); it is not a live config file. Shipping one has no effect on
plugin loading and must not be used to register plugins or keybinds.

**Before changing any plugin or TUI config, read the v2 docs (not the v1 docs):**
- https://opencode.ai/v2/docs/build/plugins/cli (writing TUI plugins, slot list)
- https://opencode.ai/v2/docs/cli/plugins (loading plugins, discovery paths)
- https://opencode.ai/v2/docs/cli/config (every `cli.json` setting)
- https://opencode.ai/v2/docs/build/plugins/migrate-v1 (what changed from v1)

### J.2 Where config lives in the container

- **Server config** (`opencode.json`: providers, models, permissions): baked to `/etc/opencode/opencode/opencode.json`, selected by `OPENCODE_CONFIG`. This works. Local models appear.
- **CLI/TUI config** (`cli.json`, plugin list): supplied by the `OPENCODE_CLI_CONFIG_CONTENT` env var in the Dockerfile.
- **Plugin files**: baked to `/etc/opencode/plugins/<plugin-name>/`.
- **Never put OpenCode config or plugins under `/home/agent` or `~/.config/opencode`.** `/home/agent` is a tmpfs (see A.4), so anything copied there at build time is hidden at runtime. Observed when we tried it: local models disappeared. The tmpfs shadowing is the likely cause.
- Check that `compose.yaml` does not override or unset `OPENCODE_CONFIG` or `OPENCODE_CLI_CONFIG_CONTENT`.

Dockerfile:
```dockerfile
RUN npm install -g @opencode/cli && opencode --version

COPY opencode/opencode.json /etc/opencode/opencode/opencode.json
COPY opencode/plugins/little-mester-logo/ /etc/opencode/plugins/little-mester-logo/

ENV OPENCODE_CONFIG=/etc/opencode/opencode/opencode.json \
    OPENCODE_CLI_CONFIG_CONTENT='{"plugins":["/etc/opencode/plugins/little-mester-logo"]}'
```

### J.3 Logo plugin

Canonical source (single copy, no duplicates):
```
agent/opencode/plugins/little-mester-logo/
  package.json    { "name": "little-mester-logo", "type": "module", "exports": { "./tui": "./tui.tsx" } }
  tui.tsx         Plugin.define({ id: "little-mester.logo", setup(context) { return context.ui.slot({...}) } })
```

- `agent/opencode/tui.json` and the loose `.txt` copies under `plugins/` are obsolete and should be deleted.
- Colors come from `context.theme` tokens (the docs use `context.theme.text.base`), not string names like `"primary"`.
- `render` must return JSX, not a component reference.
- `setup` returns the unregister function from `context.ui.slot(...)`.

### J.4 Unverified. Test, then record the result here.

1. **Home-logo slot.** The documented v2 slots are `app`, `home.footer`, `home.footer.status`, `prompt.footer`, `prompt.footer.status`, `prompt.footer.file`, `session.composer.top`, `sidebar.content`, `sidebar.footer`. There is no documented logo slot. The plugin currently uses `prepend: "home.footer"`, which shows our logo but does **not** replace the built-in one. To test replacing it, try `replace: "home.logo"` (guessed name). If it fails, check `packages/plugin/src/tui/` in https://github.com/anomalyco/opencode for the real slot list, or ask upstream for one.
2. **`context.theme.text.subdued`.** Used for the lower logo lines. Not in the docs. Falls back to the default color if undefined. **Verified:** the resolved theme (`@opencode/theme` 2.1.2) exposes `text.base`, `text.muted` and `text.faint` — there is no `text.subdued`. The metrics plugin uses `text.muted` (with a literal fallback).
3. **JSX pragma.** `tui.tsx` has no `@jsxImportSource` pragma because the v2 docs' JSX examples do not use one. If JSX fails to resolve, restore `/** @jsxImportSource @opentui/solid */`.
4. **Directory-path plugin form** (`/etc/opencode/plugins/<name>` in `plugins`) with a `.tsx` entry. The docs show directory paths and `./tui` exports, but not `.tsx` entries specifically.

### J.5 Verifying and debugging

- TUI rendering is guarded statically by `scripts/check-tui-jsx.py` (see §J.9) — it catches the JSX
  mistakes that make a slot die at render time. It is run by `tests/tui-jsx.bats` in the normal
  `bats /workspace/tests/` suite. It cannot prove that a slot *looks* right; for that, drive the
  real TUI (see §J.9) or render the slot headlessly.
- To confirm the plugin loads at all (separately from whether the slot renders), add `context.ui.toast.show({ message: "little-mester logo plugin loaded" })` at the top of `setup`.
- The v2 docs say to confirm a plugin's ID and source appear in the active plugin list.
- `console.log` from a TUI plugin may not appear in server logs.
- For **server-side** plugins, the log line `loading plugin id=<path> entrypoint=file://<path>/index.ts` confirms the plugin was picked up. The server also hot-reloads: a new file under `<project>/.opencode/plugins/` or `<global-config>/plugins/` is picked up without a restart.

### J.6 Custom tools in v2 are plugins, not markdown files

- There is **no** `tools/` directory convention in v2. A markdown file in `tools/` (the v1 way) registers nothing — opencode v2 silently ignores it.
- Custom tools are registered from a plugin via `ctx.tool.transform((editor) => editor.add({ name, description, input, execute }))`. See <https://opencode.ai/v2/docs/build/plugins/> (section "Tools").
- Plugin discovery paths: `<global-config>/plugins/<name>/index.ts` and `<project>/.opencode/plugins/<name>/index.ts`. A plugin package keeps its server entry (`index.ts`) and optional TUI entry (`tui.ts`) together.
- **Server-side** plugins (tools, commands, transforms) belong in the **server** config's `plugins` array (`opencode.json`). **TUI-only** plugins belong in the CLI plugin list (`cli.json` / `OPENCODE_CLI_CONFIG_CONTENT`).
- `agent/opencode/opencode.json` is **generated** by `scripts/regen-configs` — anything added to it by hand is lost on the next regen. Add it to `gen_opencode_json()` instead.
- Our implementation: `agent/opencode/plugins/host-metrics/` → baked to `/etc/opencode/plugins/host-metrics/` → listed in the generated `opencode.json`. It registers the `get_host_metrics` tool.

### J.7 The metrics sidecar must bind 0.0.0.0 in its own netns

- A container binding `127.0.0.1` is reachable **only from inside that container**. Sibling containers reach it by container IP / Docker DNS, which requires a bind on `0.0.0.0` (or the container IP).
- `0.0.0.0` inside the container netns is **not** "exposed to the host": exposure to the host is controlled by `ports:` in compose, which we deliberately do not set. The `internal: true` network has no route off the Docker network.
- Consequence for verification: host-side scripts cannot `curl 127.0.0.1:19532`. They must run a client container attached to the compose network — see `scripts/verify-metrics.sh`.
- Host paths mounted into the sidecar must match the constants in `gather.py` exactly (`/host/proc`, `/host/hwmon`, `/host/thermal`). A mismatch fails silently and yields empty readings, so `scripts/verify-metrics.sh` audits the mounts (criterion A9).
### J.8 Metrics sidecar ring buffer + TUI sparkline (ticket-011)

- The old 2s TTL cache meant that polling at sub-TTL intervals always returned stale data.
  Replaced it with a **daemon sampler thread** that runs `_aggregate()` every 500ms and feeds both
  a `_latest` snapshot and a rolling-ring (max 240 entries) of historical samples.
- New `GET /history?n=N` endpoint returns the last N ring-buffer samples as JSON, used by the TUI
  plugin to seed charts immediately on mount instead of starting blank.
- The TUI plugin (`host-metrics-tui`) renders in `append: "sidebar.footer"` — a compact one-line
  sparkline (GPU util + VRAM bars) by default; clicking toggles an expanded detail view with GPU
  util / VRAM % time-series charts, temperature, and system overview.
- The plugin is baked to `/etc/opencode/plugins/host-metrics-tui/` via Dockerfile and listed in the
  `plugins` array of both `opencode.json` and `scripts/regen-configs`.
- V2 TUI reactivity (how data changes trigger render updates) is **confirmed** — see §J.9. The plugin
  keeps its data in a `createSignal<State>` and reads `props.state()` **inside the JSX expressions**
  (never hoisted into a local), so Solid re-renders the slot on every poll.
- The compact view is **two short lines** (`GPU 98% ▁▃██████████████` / `VRAM 7.2/8.0G 58°C`), sized
  for the narrow sidebar; the expanded view stacks one row per metric plus CPU/RAM overview.

### J.9 OpenTUI text rules (verified — a `<text>` inside a `<text>` kills the slot)

**The crash.** The v2 TUI renders plugin JSX through `@opentui/solid`. A `<text>` element is a
`TextRenderable`; a `<text>`'s children may only be **strings, `TextNodeRenderable` instances or
`StyledText`**. A `TextRenderable` is none of those, so nesting one inside another makes the
renderer call `TextNodeRenderable.add()` with an illegal value and throw:

```
TextNodeRenderable only accepts strings, TextNodeRenderable instances, or StyledText instances
    at add (TextNode.ts:126)
    at _insertNode (reconciler.ts:112)
```

OpenCode catches that and shows `little-mester.host-metrics-tui crashed in slot sidebar.footer: …`.
The slot then renders **nothing at all** — the plugin does not "look wrong", it is simply absent.

**The rules.**

| # | Rule | Consequence |
|---|------|-------------|
| R1 | Inline fragments inside a `<text>` must be `<span>` | `<span>` is a `TextNodeRenderable`; a nested `<text>` is not |
| R2 | A `<span>` takes its colour from `style={{ fg: "#rrggbb" }}` | a bare `fg=` prop on a span is **silently ignored** (only `<text>` understands it) |
| R3 | Bold is `<strong>` (or `<b>`) | `style={{ fontWeight: "bold" }}` is **silently ignored** — the style object understands `bold`/`italic`/`underline`/`dim`, not `fontWeight` |

The full inline catalogue is `box, text, input, select, textarea, ascii_font, tab_select, scrollbox,
code, diff, line_number, markdown, image, span, strong, b, em, i, u, br, a` — `span`/`strong`/`b`/`em`/`i`/`u`
are the inline ones. There is no `<bold>` element (the ticket-011 sketch used one).

**Why a static check.** These mistakes do **not** fail at plugin load time — they fail while the slot
renders, i.e. only in a running TUI. So `scripts/check-tui-jsx.py` checks them statically (R1/R2/R3)
and `tests/tui-jsx.bats` runs it over every `agent/opencode/plugins/*/tui.tsx`. On the pre-fix file it
reports all 10 nested-`<text>` sites and all 5 `fontWeight` sites.

**How it was verified** (OpenCode 2.1.2, `@opentui/core` 0.6.x):

1. *Headless* — render the slot in a real `@opentui/solid` renderer (`testRender`) with bun + FFI:
   the old file throws the exact error above, the new file renders both compact and expanded views.
2. *Live* — run the real TUI in a pty, type a prompt, press `<leader>b` (`ctrl+x`, then `b`) to toggle
   the sidebar, and read the screen:
   - old plugin → `crashed in slot … TextNodeRenderable only accepts …`, no metrics at all
   - new plugin → `GPU 98% ▁▃██████████████` / `VRAM 7.2/8.0G 58°C`, no crash

**Gotcha for live tests:** OpenCode auto-discovers every plugin directory under the global plugin
dir, so a "fixed" copy outside `/etc/opencode/plugins` does *not* replace the baked-in one — the old
plugin still loads and still crashes. Point `OPENCODE_CONFIG` at a private config directory whose
`plugins` array lists only the copy under test.

---

## K. Design decisions and trade-offs (from README)

These explain why little-mester is built the way it is. Refer to them when making
architectural choices — many have precedent here.

### D.1 Dual backend: Ollama + llama.cpp

| Backend | Best for | Model format | How it works |
|---|---|---|---|
| **Ollama** | Small dense models that fit entirely in VRAM | Ollama format (auto-pulled) | Layer-wise GPU offload |
| **llama.cpp** | Large MoE models (35B-A3B, 30B-A3B, 20B) | GGUF (downloaded from HuggingFace) | **Expert-level offload** (`--n-cpu-moe`) |

**Why llama.cpp for MoE:** llama.cpp supports `--n-cpu-moe` (expert-level offload) where
Ollama only does layer-level offload. For MoE models on limited VRAM, keeping inactive
experts in system RAM is the only way to run them efficiently.

**Why both simultaneously:** Learning and comparison. Some models are better suited to one
backend. Ollama has a richer registry; llama.cpp has more control over quantization and
offload strategy.

### D.2 Internet off at runtime

Egress is **on by default** (filtered by `.mester-egress`), but the default network is
`internal: true`. The model servers and agent share the internal network — no published
ports. Egress is opt-in per-session via `--egress-true` (unconditional) or
`.mester-egress` (hostname-filtered). Pull services (`ollama-pull`, `llamacpp-pull`) run
on a separate network for one-shot downloads only.

**Why:** Closes the "send data out" channel. Dependencies must be baked into the image or
present in the workspace.

### D.3 Docker over Podman

Docker is the default runtime. Most docs, tutorials, and tooling assume it. The daemon
runs as root (a known trade-off). Rootless Podman is a future hardening target.

### D.4 Context = 64k (not 196k)

196k forces layers off GPU, killing prefill speed. 64k is a practical ceiling for
most agentic tasks. Larger files are staged with `git add` to focus context.

### D.5 Q4_K_M quantization

Speed/quality sweet spot. Q6/Q8 slows decode ~2x. Slight quality loss vs Q8 is
acceptable for agentic coding where iteration speed matters more than perfect prose.

### D.6 `small_model = model`

Prevents eviction thrashing on title/summary calls. Slightly more VRAM used for small
tasks, but avoids the model being unloaded and reloaded between calls.

### D.7 JSON tickets + rebuildable index

No SQLite, no WASM, no migrations. Files are the source of truth. `_index.json` is a
tiny rebuildable index (validated by `ticket-engine validate`). Drift is caught.

### D.8 Engine-only writes + audit trail

All ticket writes go through the engine (CLI or MCP). Every write appends a hashed
entry to `_audit.json`. Editing the audit log is detectable. Orphan tickets (on disk,
no audit entry) are flagged for human review and never auto-deleted.

---

## L. Security caveats (what the sandbox does NOT protect against)

Full security model: [SECURITY.md](../SECURITY.md). This section lists the known blind spots
that the README originally included under "What this does NOT protect against."

**Read this twice. These are real risks.**

### E.1 Agent can destroy your workspace

The agent can delete or corrupt everything inside the mounted workspace, `.git` included.
**Keep backups or push to a remote *before* sessions.**

### E.2 Agent-written code is untrusted on the host

Traps that run on your host later: git hooks, `Makefile`, `package.json` scripts,
`.envrc`, `.vscode/tasks.json`, shell scripts written by the agent run with *your*
privileges if you execute them. **Read agent-written code before running it on the host.**

### E.3 Container escape is theoretically possible

Containers share the host kernel. A kernel or runtime vulnerability could break out.
Unlikely from a local model making mistakes, but real against deliberately hostile code.
A VM is the next level up.

### E.4 Docker group = root

Anyone who can run `docker` can effectively become root on the host. `lab` uses `sudo`
instead of adding you to the group.

### E.5 Bad output

A sandboxed agent can still write wrong or insecure code. Review it.

---

## M. Usage guide

### M.1 Model management

#### Adding / changing models

1. Edit `models.txt` (see format below)
2. Run `./lab pull` — starts the appropriate pull service:
   - `ollama-pull` for `[ollama]` models (downloads from Ollama registry)
   - `llamacpp-pull` for `[llamacpp]` models (downloads GGUF from HuggingFace)

#### Switching models at runtime

**In opencode:** Use `/model <provider>/<model>`, e.g.:

- `/model ollama/qwen3:8b`
- `/model llamacpp/qwen3-35b-a3b-q4_k_m`

**In Aider:** Pass `--model` when starting, or restart with a different model.

#### `models.txt` format

```
[ollama]
qwen3:8b              65536     0.25   # fits entirely in 8GB VRAM, very fast
qwen3:14b             65536     0.25   # may fit with reduced context

[llamacpp]
qwen3-35b-a3b-q4_k_m  65536     0.25   # main coder, expert offload
qwen3-coder-30b-a3b-q4_k_m  65536     0.25   # coding-tuned variant
gpt-oss-20b-q4_k_m    65536     0.25   # MXFP4, ~12GB, ~3.6B active, great tool calling
```

Columns: `model_name context_length temperature`. Lines starting with `#` are comments.

#### VRAM monitoring

```bash
# Host-side
nvidia-smi -l 1

# Inside agent container (via metrics sidecar)
curl -s http://metrics:19532/summary
# Or use the get_host_metrics tool from opencode
```

If you see the model process using **system RAM instead of VRAM**, it has fallen back to
CPU. Reduce context, use a smaller quantization, or switch to a smaller model.

### M.2 Hardware & performance tuning

#### Critical for sustained runs (laptop)

| Setting | Why |
|---|---|
| **Plugged in + performance profile** | Prevents throttling |
| **dGPU in discrete/MUX mode** | Prevents dGPU routing through iGPU |
| **Cooling stand / external fan** | **Throttling costs more than any flag** |
| **Close browsers, Electron apps** | Frees system RAM for CPU-side experts |
| **Don't set `mem_limit` on model container** | Agent's 8 GB limit is fine; model container needs flexibility |

#### llama.cpp flags (in `compose.yaml`)

| Flag | Purpose | Tune? |
|---|---|---|
| `-ngl 99` | Offload all possible layers to GPU | No |
| `--n-cpu-moe 40` | Expert offload: start high, lower until VRAM ~7–7.4 GB | **YES** — primary tuning knob |
| `-c 65536` | Context window (64k) | Per-model in `models.txt` |
| `--cache-type-k q8_0 --cache-type-v q8_0` | Quantized KV cache (saves VRAM) | No |
| `-t 8` | Threads (8–12, NOT 24 — decode is bandwidth-bound) | Benchmark with `llama-bench` |
| `-ub 2048` | Micro-batch for faster prefill | Try 1024/2048 |
| `--mlock` | Prevent CPU experts from paging to swap | Keep on |
| `--parallel 1` | Single parallel slot (multiplies KV memory) | Keep 1 |

#### Ollama env vars (if using Ollama)

```yaml
OLLAMA_FLASH_ATTENTION: "1"
OLLAMA_KV_CACHE_TYPE: q8_0
OLLAMA_NUM_PARALLEL: "1"
OLLAMA_CONTEXT_LENGTH: "65536"
OLLAMA_MAX_LOADED_MODELS: "1"
OLLAMA_KEEP_ALIVE: "-1"
```

### M.3 Monitoring (metrics sidecar)

A lightweight sidecar exposes live GPU, CPU, RAM, and temperature data from inside the
container, so the agent has real hardware visibility without breaking the sandbox.

- **Detailed JSON**: `curl -s http://metrics:19532/metrics`
- **Quick summary**: `curl -s http://metrics:19532/summary`
  - Example: `CPU load 2.1/4.3/5.8 | RAM 12.3/31.7 GB (39%) | GPU0 util=87% VRAM 7.2/8.0 GB 68°C`
- **History**: `curl -s 'http://metrics:19532/history?n=60'` → last N samples from a
  500 ms sampler ring buffer, used by the TUI sparkline.
- The service publishes **no host port** — `metrics` is a Docker DNS name on the
  `internal` network.

> **Agent query tool**: `get_host_metrics` is registered by the
> [`host-metrics` plugin](../agent/opencode/plugins/host-metrics/). It returns a formatted
> report the agent can call before large model operations.
>
> **Live TUI view**: [`host-metrics-tui`](../agent/opencode/plugins/host-metrics-tui/)
> renders a compact GPU/VRAM sparkline in the sidebar footer, expandable for detail.
>
> **Acceptance verification**: run `./scripts/verify-metrics.sh` on the host.

### M.4 Testing

This project uses [Bats](https://bats-core.readthedocs.io/) for unit-testing the `lab`
launcher, and Jest for the ticket engine.

#### Prerequisites

```bash
# Arch / CachyOS
sudo pacman -S bats

# macOS
brew install bats-core
```

#### Running the sandbox tests (host)

```bash
bats tests/
```

Regression checks include: valid workspace path acceptance, rejection of sensitive paths
(`$HOME`, `$HOME/.ssh`, …), ownership verification, git-hook warning detection, and the
TUI JSX rules.

#### Ticket engine tests

```bash
cd little-mester-mcp && npm test
```

#### Agent tests (inside the sandbox)

1. Ensure your workspace is set up (e.g. `workspaces/demo`).
2. Start a session: `./lab workspaces/demo`
3. Ask the agent to run the test suite.
4. It executes `./run-tests.sh`, which safely invokes `bats` inside the container via
   `./lab exec`.

### M.5 Troubleshooting

| Symptom | Likely cause / Fix |
|---|---|
| `nvidia-smi` fails on host | Driver problem. Fix first. |
| `docker run --gpus all ... nvidia-smi` fails | NVIDIA container toolkit not configured. `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker` |
| ollama / llama.cpp shows CPU fallback | Model + context doesn't fit in VRAM. Lower context, smaller quantization, or smaller model. |
| Agent can't reach model server | Server not healthy yet, or services not on same network. Check `docker compose ps`. |
| Files created by agent owned by root | `LAB_UID`/`LAB_GID` not passed. Run via `./lab`, not `docker compose` directly. |
| Agent stalls trying to reach internet | Expected with no egress. It should fall back. Note latency in journal. |
| llama.cpp fails to start | Check the GGUF file exists in the `llamacpp-models` volume. Check `docker logs llamacpp`. |
| Out of memory (OOM) on host | Too many apps open. Close browsers. Check `docker stats`. |
| Throttling / low t/s | **Cooling!** Check temps in metrics. Fan/stand mandatory for sustained load. |
| `validate` reports orphan ticket files | A ticket was written outside the engine. Review it, then `audit --baseline` to adopt or remove it deliberately. Never delete blindly. |
| `validate` reports a stale lock | A previous engine process crashed. Remove `docs/tickets/.engine.lock` (check the PID first). |

---

### M.0 Repo map

| Path | What it is | Read this |
|---|---|---|
| `lab` | Launcher script — validates workspace path, runs compose | [workflows.md](workflows.md) |
| `compose.yaml` | Services, networks, volumes, hardening flags | — |
| `models.txt` | Model list with `[ollama]` / `[llamacpp]` sections | — |
| `scripts/regen-configs` | Generates `opencode.json` + updates compose from `models.txt` | — |
| `agent/` | Agent container Dockerfile + opencode config + plugins | [agent/README.md](../agent/README.md) |
| `ollama/pull-models.sh` | Pulls Ollama models during setup | — |
| `llamacpp/pull-models.sh` | Pulls GGUF from HuggingFace during setup | — |
| `verify-lab.sh` | Regression tests for sandbox guarantees | [Testing](#m4-testing) |
| `workspaces/` | Your projects live here (gitignored) | — |
| `docs/` | Journal, tickets, workflows, decisions | — |
| `docs/tickets/` | Ticket JSON files + index + audit log | [DECISIONS](tickets/architecture/DECISIONS.md) |
| `little-mester-mcp/` | Ticket engine (CLI + MCP server) | [little-mester-mcp/docs/README.md](../little-mester-mcp/docs/README.md) |
| `scripts/` | Automation tools — tickets, hooks, sync | [scripts/README.md](../scripts/README.md) |
| `tests/` | Bats tests for `lab` and the TUI JSX guard | [Testing](#m4-testing) |
| `SECURITY.md` | Full security model, network isolation, capabilities | [SECURITY.md](../SECURITY.md) |

### M.6 How a session works

1. You run `./lab <workspace>`.
2. `lab` resolves the path, runs safety checks, asks you to confirm the mount.
3. `lab` runs `docker compose` with `WORKSPACE`, `LAB_UID`, `LAB_GID`. Uses `sudo`
   (the Docker daemon is root-owned).
4. Compose starts `ollama` + `llamacpp` (health-checked), then the `agent` container:
   non-root, read-only fs, no caps, `/workspace` mounted rw, network gated by egress.
5. **opencode** sends prompts to `http://ollama:11434/v1` OR
   `http://llamacpp:11434/v1` (configurable per model).
6. The agent edits files in `/workspace`, runs commands in the container, and commits
   via git as `little-mester-agent`.
7. When you quit, the container is removed (`--rm`), and `lab` warns if it finds git hooks.
8. **You review on the host** with `git log` and `git diff`.

Understanding steps 5–7 *is* agentic coding: a loop where a model proposes actions, a
program carries them out, and the results go back to the model.

### M.7 Agentic workflow tips

- **Context window**: the model has a limited context window. If your workspace is huge,
  the agent may forget earlier instructions. Stage specific files with `git add` to focus it.
- **Git hooks**: the agent writes code that runs on your host if you execute hooks (like
  `Makefile` or `.envrc`). **Always review** these before running them.
- **Iterative prompts**: break complex tasks into small steps. "Refactor the login
  module" is better than "Fix the app".

### M.8 Learning path

Do these in order. Each has its own README with goals, exercises, and "you should be able
to explain" questions.

1. Get the sandbox running and make `./verify-lab.sh` pass. Read [SECURITY.md](../SECURITY.md).
2. Use opencode (or `--agent aider`) on a small project. Review every diff. Journal what
   the model gets right and wrong.
3. Build your own agent loop: `my-agent/` (planned).
4. Measure it: `evals/` (planned).
5. Attack it: `red-team/` (planned).
6. Add controlled web access: `proxy/` (planned).
7. Add tool servers: `mcp/` (planned).

Keep `docs/journal.md` updated throughout. **What broke and why is the most valuable
thing you'll produce here.**

### J.10 Exit confirmation (ticket #37)

**Decision.** Ctrl+C is the only keyboard exit trigger, and it opens a visible confirmation dialog.
There is **no instant quit**: Ctrl+D, `<leader>q` and single-letter keys do not exit. While the
dialog is open, `Y`/`y` or `Q`/`q` confirms; `Esc`, `N`/`n` or Ctrl+C cancels; every other key is
swallowed so nothing leaks into the prompt. While the dialog is closed the plugin captures **only**
Ctrl+C — `y`, `Y`, `q`, `n` etc. type normally.

A literal "Ctrl+C+Q" chord is impossible: a terminal sends Ctrl+C as one control byte. The close
sequence is therefore **Ctrl+C, then Q** (Q presses the dialog's confirm action).

**Why the first implementation failed (verified against OpenCode 2.1.4 source).**

1. The overlay used slot `"root"`, which is not in the v2 `SlotMap` (`app`, `home.footer.*`,
   `prompt.footer.*`, `session.composer.top`, `session.panel`, `sidebar.content`, `sidebar.footer`).
   `"root"` is never mounted, so `setShow(true)` rendered nothing.
2. The plugin registered `y`, `Y`, `q` and `escape` as always-on global commands, so those letters
   were swallowed even with the dialog closed.
3. `agent/opencode/tui.json` is v1 config and is not loaded by v2, so its `app_exit: "q"` never
   applied — and OpenCode's default `app.exit` (`ctrl+c,ctrl+d,<leader>q`) stayed live, which is why
   Ctrl+D still quit instantly.

**Implementation contract.**

- Use `context.ui.dialog.show(...)` (a real modal dialog), not a slot overlay. `DialogProvider`
  pushes the `modal` input mode for the dialog's lifetime.
- Confirm/cancel keys live in a `mode: "modal"` keymap layer that is also `enabled: () => open()`.
  They are unreachable unless this specific dialog is open.
- The only global command is `ctrl+c` with `priority: 100`, which beats the prompt's own
  `prompt.clear` (`ctrl+c`) binding. That layer is disabled while the dialog is open so Ctrl+C
  cancels instead of reopening.
- `app.exit` is set to `"none"` and `prompt.clear` to `"none"` in
  `OPENCODE_CLI_CONFIG_CONTENT` (`agent/Dockerfile`). `app.exit` remains dispatchable **by command
  id**, so confirming calls `context.keymap.dispatch("app.exit")`; no keybinding is needed.
- Consequence: Ctrl+C no longer clears the prompt (owner accepted the workflow change). `/exit` and
  the other slash aliases still exist as deliberate, typed commands.
- `agent/opencode/tui.json` is deleted: dead v1 config that would have bound exit to a single `q`.

**Verification.** Static checks: `tests/tui-jsx.bats`, `tests/exit-confirm.bats` (26 tests total in
`bats tests/`, one unrelated skip). Live check: `scripts/verify-exit-confirm.py` drives a private
standalone TUI through a pty and asserts Ctrl+D is inert, `yq` types with the dialog closed, Ctrl+C
opens the dialog, Esc/N/Ctrl+C cancel without exiting, and q exits. It passed on OpenCode 2.1.4 in
the agent container; the run is recorded on ticket #37. The image must be rebuilt/restarted for the
baked `/etc/opencode` copy to replace any stale running copy.
### J.11 Auto-accept: remove hardcoded permission block, rely on `--auto` CLI flag (2026-10-27)

**Decision:** Remove the hardcoded `"permission": {"edit": "ask", "bash": "ask"}` block from
`agent/opencode/opencode.json` and from `scripts/regen-configs`. Use the `opencode --auto`
CLI flag at runtime to control permission mode, NOT config file modification.

**Why:** The `opencode.json` config file is baked into the Docker image and sets permissions
as a default. When the config file explicitly specifies `"ask"`, the `--auto` CLI flag does
not override it — the config takes precedence. This makes `--auto` unusable for per-run
permission control.

Three rejected alternatives:

| Approach | Why rejected |
|----------|-------------|
| `sed` live-patch of `opencode.json` before/after agent run | Modifies a tracked config file at runtime; leaves artifacts; race condition on restore |
| `OPENCODE_CLI_CONFIG_CONTENT` env var override in `--env` | Overwrites the entire CLI config (plugins, keybinds); fragile — any future config change breaks the override |
| Regenerate `opencode.json` with `regen-configs` per-run | Heavyweight; requires Python; breaks the "agent-only" loop since regen-configs touches config files outside the agent |

**Chosen approach:**
- Remove the `"permission"` block from the baked config. OpenCode's **default** (when no
  `permission` block is present) is `ask` for tools that don't explicitly allow themselves.
- In `./lab`, add `--auto-accept` / `-y` flag (alias for `--auto`). When set and
  `AGENT=opencode`, run `opencode --auto "$@"` instead of just `$AGENT "$@"`.
- Same pattern in `./lab-vm` for the `--auto-accept` alias on `lab-vm up`.
- No config files are modified at runtime. No env var overrides. Clean and reversible.

**Runtime behavior:**
| Invocation | Permission mode | How |
|---|---|---|
| `./lab <workspace>` (no flags) | `ask` (default) | No hardcoded block → OpenCode's default `ask` |
| `./lab --auto-accept <workspace>` | `auto` | `--auto` CLI flag overrides the default |
| `./lab-vm up --auto-accept` | `auto` | Same `--auto` flag passed inside the VM |

**Files changed:** `agent/opencode/opencode.json`, `scripts/regen-configs`, `./lab`,
`./lab-vm` (the `--auto-accept` flag alias wiring). The Dockerfile is **not** changed —
it already sets `OPENCODE_CLI_CONFIG_CONTENT` for plugins/keybinds, which is unaffected.
