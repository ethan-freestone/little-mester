# Local-first VM acceptance → C14 handoff plan

## Authority and current state

Owner decisions recorded after C13b closeout:

- This document was originally a planning-only handoff. The owner subsequently
  explicitly switched back to **local §1 implementation**. That authorizes code,
  fixture tests, and documentation reconciliation only; it does **not** authorize
  provider discovery, VM/service startup, a campaign, credential activity, push,
  merge, or C14 activation.
- Choose **1A**: implement a minimal repository-owned, redacting synthetic-canary
  checker, not an owner-supplied tool or a new testing framework.
- Choose **local models first**. Ollama and llama.cpp previously worked on the old
  branch/in Docker; availability on the actual acceptance host is unverified.
  Discover read-only, then ask the owner to select exact providers/model IDs.
- Live OpenAI login, paid streams, refresh, and provider-specific credential
  roundtrips are **deferred follow-up work, not merge or C14 blockers**. Preserve
  the ability to use native browser auth and its automated regression protection;
  do not remove the feature or report unperformed live tests as PASS.
- Disposable campaign credentials are acceptable in principle. This is not
  permission to inspect/copy existing credentials, log in now, export/delete a
  sole credential copy, or revoke unrelated sessions. Local-first acceptance
  needs no real OpenAI credentials.
- Keep VM as intended release default and Docker as optional explicit fallback.
  Ordinary `lab` remains on Docker until separately approved C14 activation.

Implementation baseline: `d3751e4fbda25349de5cbcb1f1e6400ae3e489c8`;
ledger closeout: `777f5a9f9716d83da254b01b96b93bb308021177`. No campaign ran.
This document supersedes older blanket requirements that every historical auth
subcheck must pass before merge/C14. It does not waive non-auth security gates.

## 1. Bounded implementation handoff

### Implementation checkpoint

The bounded local implementation is complete and fixture-validated in
`scripts/vm-canary-check.py`, `scripts/vm-host-acceptance.py`, and tests. The
checker handles regular files, filtered snapshot trees, loose Git objects, raw
ISO9660 bytes, and run-owned logs/reports with fixed redacted results and bounded
work. It performs
a raw-byte pass over QCOW2 artifacts but does not inspect their virtual
filesystem contents; those checks remain BLOCKED. Guest filesystem isolation,
synthetic atomic export/import lifecycle checks, and provider-forwarding
acceptance also remain explicit blockers rather than implied PASS results.

No real target-host discovery or acceptance has occurred. Local fixtures do not
prove boot, model availability, filesystem isolation, guest registry access for
the database image, nested KVM, human UI behavior, or exact real cleanup.

### A. Minimal synthetic-canary checker

Reuse the acceptance runner/report and existing snapshot/guest mechanisms. Use
stdlib helpers and narrow fixtures; no daemon, generic scanner framework, new
provider support, automatic remediation, or unrelated refactoring.

The local implementation enumerates the filtered snapshot/tree and Git objects,
provisioning seed, base/overlay images, selected logs/diagnostics and reports.
It inspects regular bytes, loose Git objects and raw ISO9660 bytes; a raw QCOW2
byte pass is explicitly insufficient to inspect the image filesystem. Missing,
unsafe, incomplete or unsupported artifact checks stay BLOCKED. Do not mount or
modify arbitrary host images or install host tools implicitly.

- Generate an unguessable **fake** secret and keep it in a private campaign-owned
  host fixture outside exported source. Separately test known-positive artifacts
  in fixtures so an always-ABSENT checker cannot pass.
- Read the canary privately, not in argv, environment, chat, history, or reports.
  Check the relevant raw representation and any specified encodings actually
  produced by the tested pipeline; do not claim universal secret detection.
- Accept only explicitly manifested, canonical run-owned artifacts; reject
  escaping symlinks, foreign paths, unsafe files and ambiguous ownership.
- Bound bytes, file counts and runtime; inaccessible, oversized, unsupported,
  missing or incomplete inspections are BLOCKED, never absence/PASS.
- Persist only fixed check IDs, PASS/FAIL/BLOCKED, sanitized reason codes and
  artifact digests where safe. Never print a match, canary, secret, or file body.
- Synthetic material deliberately present in its source fixture is not a leak;
  define that exclusion explicitly and never exclude the artifact under test.
- Clean only exact generated fixtures. Keep reports outside exported snapshots.
- Add positive/negative/redaction/path/symlink/incomplete-coverage/timeout fixtures
  and integrate results into the existing security gate evidence.

### B. Reconcile acceptance scope without weakening auth safety

The runner distinguishes **required local-first acceptance** from **deferred
live-OpenAI evidence** in the existing report structure. It does not simply remove
`HUMAN-AUTH` from a list: the report records the reason, each related deferred
subcheck, automated auth regression obligations, and independent required
filesystem/private-state subchecks.

| Required before local-first C14 | Deferred live provider evidence |
|---|---|
| Existing automated browser/auth fixtures: URL/version validation, narrow callback forwarding, cancellation/timeout/port collision, cleanup, independent sessions and secret redaction | Real OpenAI browser success and live negative scenarios |
| Synthetic/private-state persistence and atomic export/import tests; failed export must block destructive deletion; forwarding default-off and explicit selection | Actual OpenAI refresh/expiry, real token export/delete/reimport and recursive provider credential import |
| Real host/guest filesystem and network isolation, canary absence, no host auth copy, exact cleanup | OpenAI paid/model streaming acceptance |
| Local-provider real streams, real agent work, workload/lifecycle/nesting and observed UI/permissions/rollback | Provider-specific claims not exercised locally |

Keep deferred checks visible as DEFERRED with an explicit owner-approved reason;
never set them PASS or claim full historical fifteen-gate
acceptance. Report both the required local-first outcome and deferred evidence.
Local-first readiness can pass only if every required subcheck and exact teardown
passes. Auth-dependent items in E2E-MODEL, E2E-LIFE, SEC-FS, SEC-SECRET and
HUMAN-AUTH must be classified individually. Non-auth items remain required.
Account-level expiry cannot be simulated into live-provider PASS.

Preserve current automated auth coverage and keep focused tests proving that
local-first scope defers only approved live-provider checks, cannot override
required failures, and keeps OpenAI functionality available. The bounded
follow-up specification for real OpenAI acceptance remains here; do not
implement live acceptance.

### C. Close executable prerequisites, not add features

- Audit existing procedures against the required local-first subchecks. Real
  agent failing-test→fix→pass, two authorized concurrent tasks, retention,
  selected config/tools apply, boundaries and actual nested fail/fix/depth proof
  need actual operations/evidence, not wholesale checklist attestation.
- The guest workload now checks for `postgres:16-alpine` inside the VM and makes
  one guest-only `docker pull` attempt, bounded to 180 seconds, when absent, using
  the guest-local socket and a fresh empty Docker config. It verifies the image
  afterward; failure remains BLOCKED. Compose is configured with
  `pull_policy: never` and `up --pull never` to prevent implicit pulls. Never use
  host Docker for this workload. Fixture tests cover both successful guest-side
  download and unavailable-image blocking; they do not prove target-host registry
  access or run a campaign.
- The runner must stop before expensive recursion until the exact-run
  `HUMAN-TUI` observation is recorded PASS; neither boot nor a stage result proves
  usability. The local runner now adds this fail-closed prerequisite; fixture
  validation is recorded in the progress ledger.
- Bind the selected suite/memory/update effects/provider scope and total budget
  consistently across start/resume/retry. Preserve private STATE_ROOT and attempt
  ledger for the one diagnosed retry; changed source/config needs fresh review.
- Reserve at least 35 minutes for exact teardown inside the approved total; do
  not consume the reserve on another stage. Ambiguous cleanup stays BLOCKED and
  preserved.
- Fix known docs-consistency failures within the already-planned C14 docs work,
  not as a new feature project.

Before a future live handoff: review the exact diff; require full Python/Bats,
affected static/compile/shell, embedded guest-script, help and Bash/Fish docs
checks; record any missing required procedure as an explicit blocker, never
“ready” by assertion. This local checkpoint remains uncommitted unless separately
requested; validation grants no push, live action or C14 permission.

## 2. Execution responsibilities and mandatory pauses

All live operations run on the **actual target Linux host**, not an assumed
equivalent coding container. Where this session cannot reach that host, the owner
runs the supplied commands and returns sanitized output. No remote access is
assumed. The following are future checkpoints, not authorization to run now.

| Checkpoint | Automatable work | Required owner input / explicit instruction |
|---|---|---|
| P0: implementation permission | Local §1 code/tests/docs | Satisfied by the subsequent explicit implementation request. No campaign permission included. |
| P1: read-only host/provider discovery | Exact revision/tool/KVM/RAM/disk checks and fixed loopback model-list queries; no VM/service/model mutation | Run the supplied read-only command on the target host, return sanitized result. Select discovered provider/model IDs; do not infer approval from availability. |
| P2: freeze campaign and approve effects | Produce one reviewed plan with hashes, exact paths, required/deferred subchecks and budgets | Approve run ID, revision, selected models, memory, total minutes, disk/download ceiling, guest PostgreSQL preparation if needed, selected config/tools updates and exact cleanup. OpenAI/paid API scope stays off. |
| P3: fresh usability | Start only the disposable campaign VM; boot/mount/tools checks, warm identity and local-model smoke | **STOP before recursion.** Open exact ready profile in the owner's terminal; perform TUI/metrics/cancel/permission actions using the supplied checklist. Explicitly continue only if usable. Diagnose failures first. |
| P4: work and lifecycle | Bounded local streams, agent test fail/fix/pass, guest Compose/query, retention, scan/cancel and approved config/tools apply, resize | Pause only for actual ask-mode/UI decisions or unexpected effects. Never accept permissions on the owner's behalf. Show exact test purpose and expected action. |
| P5: nesting/security/concurrency | Actual nested placement/fail/fix/depth, per-level budgets, isolation/canary checks, authorized concurrent harmless tasks | Confirm continuation into the costly stage as agreed at P3; pause on unavailable prerequisites, scope changes, uncertain ownership or resource exhaustion. |
| P6: evidence and cleanup | Summarize required failures/blocks and deferred auth; deepest-first exact teardown, residue/protected-asset checks; finalize report after cleanup | Complete outstanding genuine human observations before teardown. No premature PASS for teardown-dependent observations; finalize those from actual cleanup evidence. Stop for review if sole auth-copy or ownership is uncertain. |
| P7: C14 approval | Review required local-first evidence; prepare existing TASK-16 docs/CI/default-route changes | Separately approve C14 activation. No push/merge is implied by campaign approval or C14 implementation. |
| P8: default/rollback smoke | Test the new VM default on exact accepted scope and explicit Docker fallback without recreation | Review smoke result before requesting separate push/merge approval. A failed smoke blocks completion; preserve the working Docker session. |

### Required shape of every owner instruction

The implementation model must provide **one current step at a time**, not a wall
of executable campaign commands. Each handoff must say:

1. **Where:** target host vs coding checkout; Bash or Fish; exact directory/SHA.
2. **What:** copy/paste command using already-confirmed variables and exact run ID.
   No fake model names or illustrative budgets in a command presented to run.
3. **Effects:** read-only vs VM start/download/update/delete; protected assets.
4. **Expected result:** sanitized output/status, timeout and concrete stop condition.
5. **Owner action:** what to click/type/observe, or explicitly “no input needed”.
6. **Return:** exact sanitized summary/report fields to send back; no raw logs,
   prompts, model responses, OAuth URLs, credentials, or auth-store contents.
7. **Next pause:** state “stop here and wait” or the exact already-approved next
   automated action. EOF, timeout or silence is not consent.

An agent can automate already-approved noninteractive steps within one frozen
campaign budget without asking permission for each command. It must pause at
the named human checkpoints and for new effects, changed scope/revision, unsafe
state or exhausted budget. Browser callback clocks are never extended by pauses.
No silent retries. At most one diagnosed fresh attempt on the same source/config
revision, after exact cleanup, using the same STATE_ROOT and original total.

## 3. Decisions still needed, at the appropriate pause

Do not make the owner solve these before implementation has produced useful
read-only discovery and a concrete handoff:

- **P1:** Is the current session the acceptance host, or will the owner run host
  commands? Which discovered Ollama/llama.cpp model(s) should be tested? Prefer
  sequential tests; a missing service gets reported, never started automatically.
  If neither provider is usable, present a bounded remedy for separate approval.
- **P2:** Approve measured memory/storage/network requirements and a concrete
  total-time ceiling including teardown and any retry; do not silently inherit
  illustrative 360-minute examples. Confirm guest-only PostgreSQL preparation
  and selected config/tools apply. Paid API ceiling is zero for this campaign.
- **P3:** Confirm usable TUI and local agent behavior, then explicitly continue
  to the remaining costly acceptance stages or stop and diagnose.
- **P7:** Review the required local-first results and visible deferred auth list;
  authorize C14 only if required evidence and cleanup pass.

## 4. OpenAI follow-up boundary (specification only)

Keep native browser auth and automated fixture regression tests in the normal
local/CI suite. Later, separately scope an opt-in live OpenAI test: exact model,
cost/time limits, owner browser login using disposable campaign state, no host
credential copying, verified callback cleanup, a bounded real completion, and
explicitly approved retention/export/import tests. Interactive login remains an
owner action; refresh availability must not create an unbounded merge blocker.
Do not implement that live campaign during the local-first/C14 scope.

## 5. Completion rule and exclusions

A working VM milestone means real boot, usable native TUI and local agent,
guest-only workload, retained state, required isolation/nesting/lifecycle proof,
and exact cleanup on the actual host—not just green fixtures. C14 then delivers
the VM-default route with working explicit Docker fallback and required docs/CI.
Evidence must identify the tested runtime/source revision; any C14 changes to
runtime behavior require affected revalidation, not assumed coverage from an
earlier SHA.

No new supervisor, model manager, service startup automation, scanner framework,
automatic credential harvesting, broad host changes, unrelated cleanup, or
optional provider expansion. Preserve `vm-migration-plan.md` and working Docker
state. The subsequent instruction authorizes only local §1 implementation and
fixture validation. VM startup, provider discovery, credential activity, campaign
execution, push, merge and C14 cutover remain unauthorized.
