# Single session handoff — get implementation into the replacement VM

## 0. Start here; do not resume an old checklist

**Owner reset, 2026-11-06. This session is PLANNING ONLY.** No tests, implementation,
VM/service actions, auth attempts or provider calls are authorized now. This file
is the next session's entry point. On the owner's explicit implementation resume,
execute the packets below; do not ask the owner to reassemble topology, ticket
history or requirements already recorded here. No agent is spawned by this reset.

Priority and sequencing here supersede the prior INF-01-first/local-model-first
handoffs and historical STOP/retry instructions. Requirements not relevant to the
current packet remain tracked, not erased or silently waived. Release/C14 is
separate from moving development into a usable experimental VM.

**Short-term outcome:** Luna implements Little Mester from INSIDE a
`feat/vm-approach-2` VM, through native OpenCode using owner-controlled OpenAI auth.
Metrics and a real nested candidate VM must then work without sacrificing that
development VM or the old fallback. A green mock suite is not that outcome.

Read order: this file -> selected packet -> relevant source. Consult master
PLAN/TASKS only for the affected requirement; PROGRESS for facts. Do not reread
the entire historical handoff each iteration or call tickets to choose work.

### The actual work order (frozen; Luna does not need another plan)

| Packet | Implement | Prove using the runtime runner | When it fails |
|---|---|---|---|
| M0 | Add `--dev-step inspect` to the existing acceptance script; exact-profile transport, per-check errors and repair list. | One selected-profile inspection, including guest execution and proxy-layer diagnosis. | Fix/report that exact transport/diagnostic fault; M1 source implementation is still allowed. |
| M1 | Include known source fixes, exact-owned egress reload and configurable managed metrics host port; create one separate current-source candidate with max-depth2. Preserve cap0 profile. | `boot`: real candidate ready, mester executable, workspace usable; failed-stage evidence included. | Repair first failed stage and retry only its affected path; don't restart image download or invent another preflight. |
| M2a | Native browser/device auth path and exact mediated OpenAI egress. | `auth`: real private owner login + cleanup; transport/credential errors distinguished. | Repair proxy/config/bridge indicated by error; auth failure does not forbid auth implementation. |
| M2b | Native agent workspace/model invocation; synthetic coding-task verification. | `work`: actual OpenAI-backed guest agent edits a fixture and passes its test. | Repair model invocation/tool/cwd path; fixtures alone cannot pass live work. On PASS move session to N immediately. |
| M3 | Metrics service -> QEMU forward -> guest URL -> tool/TUI path. | `metrics`: actual /metrics JSON and /summary, tool values; separate UI observation. | Repair first failed link; M4 boot/recursion work continues independently. |
| M4a | Guest-local entry to existing self-test adapter; one credential-free C boot from N. | `nested`: real changed-source child ready + N continuity. | Fix adapter/KVM/bootstrap path; no dependency on C auth or local inference. |
| M4b | Extend same adapter/evidence with deliberate failed snapshot -> fixed snapshot and depth/boundary proof. | Real fail/fix and unchanged usable N. | Fix exact failed assertion; retain successful boot evidence scoped to its revision. |

**Do not stop after M0 to ask what to implement next.** M1–M4 are specified below.
Use the gate/repair matrix, implement its repair, run evidence, and continue. Sol
reviews a result and chooses a repair within this plan, not a new architecture.

## 1. Facts and assets to carry to the next session

| Item | Recorded fact / remaining uncertainty |
|---|---|
| Current implementation location | Owner explicitly says this session is inside the OLD `vm-approach` VM, not the replacement VM. Treat this as supplied context; absent recursion marker is not proof of physical host H. |
| Checkout | `/workspace` here; owner launch checkout `/home/ethan/Development/little-mester`; branch `feat/vm-approach-2`. Do not assume other terminals share paths or worktree contents. |
| Current Git baseline | Read-only Git inspection in reset: HEAD `e0ca2e3f9cc5350ef71084d5290fdd55edb59300`. Uncommitted INF-01 Python/tests and plan/docs changes already exist. Preserve/review them, do not reset or overwrite. |
| Existing replacement control root | `/home/ethan/.local/share/little-mester-branch-dev-vm-approach-2` in the owner launch environment. Never use the legacy default tree instead. |
| Existing replacement profile/run | `branch-dev-20261105`; run `71ff52d4-d3d5-4394-a43d-440b1929f07c`. Owner status: `ready`, `running`, depth 0, 8 GiB, workspace `/home/ethan/Development/little-mester`. Preserve it. |
| Existing guest | Owner applied targeted package/XDG fixes; OpenCode v2.2.5 runs as mester; TUI opens and Escape works. No successful OpenAI task, metrics display or recursive launch established. |
| Recursion limitation | Development launch used `--max-depth 0`; status shows `descendants_possible=false`. Confirm exact persisted cap read-only before acting. Never raise inherited identity/cap by editing markers or profile JSON. |
| Known service issues | Remote request got `ERR_PROXY_TUNNEL403`; exact denied hostname/layer not yet established. Browser was disabled on diagnostic attach; old frozen browser cleanup has a known argument-count defect, fixed in checkout source, not in that runtime. Metrics forward/service unverified. |
| Discovery result | Owner exact-profile report: provider `none`, no model ID, `NO_MODEL_FORWARD`, `NO_CAPABILITY`; no provider endpoint contacted. Execution identity unknown. This is EXPECTED for an unselected local model and says nothing about OpenAI login capability. Do not rerun discovery to progress OpenAI work. |
| Existing helpers | `scripts/vm-start-diagnostics.py --state-root ROOT --profile NAME [--logs]`; `vm-log-report.py` is a helper, not a standalone CLI. `scripts/vm-self-test.py` has a live adapter, but its control-plane context must be respected (see M4). |
| Earlier local validation | Focused 72; VM Python 398; Bats 149 passing as previously recorded; Ruff/AST/diff checks passed. Eight known README/DECISIONS docs failures remain. None rerun in this reset; none proves runtime features. |

Topology names for this migration: **O** = old working VM/launch environment;
**N** = replacement development VM; **C** = test VM launched from N. Record actual
ancestry/depth in evidence; do not relabel N as a physical-host-launched G0 merely
because the replacement profile schema says depth 0. Physical host stays protected.

## 2. Model roles and the implementation/evidence loop

Owner-selected roles, exact available references checked read-only in this reset:

- Implementer: **`openai/gpt-6-luna#max`** (Luna; do not guess another model from a
  spelling variation). Implement one packet and its evidence runner/probes.
- Planner/reviewer: **`openai/gpt-6.1-sol#medium`** (Sol). Review the resulting diff
  and runtime evidence, diagnose failures and freeze the next small fix packet.
- Owner controls the interactive login/browser and actual model/session switching.
  The next coordinator may delegate on implementation resume under this role split;
  do not automatically start paid nested agent sessions or use a different model.

Preferred next-session orchestration: Sol medium coordinates and delegates each
frozen implementation packet to Luna max, then runs/reviews the evidence and
assigns the next fix. If the owner starts directly in Luna, use the same packet
and return its diff/report to Sol; do not improvise an architectural choice.
Sol review is part of the loop, NOT another owner signoff after every packet.
When Luna is the active session, the frozen plan already authorizes its in-scope
edits/tests on resume. It does not need Sol to unlock implementation. Return a
concise diff/report for Sol at an evidence checkpoint; the owner controls a model
swap if delegation is unavailable. Do not spawn another implementation session
inside N merely to test the existence of one.

Each iteration MUST produce:

1. One named user-visible result, editable paths, exact execution location, assets
   and permitted effects. Do not replan the entire architecture.
2. Regression for the diagnosed fault + minimal production fix + a real evidence
   action using the same path the owner will use. Unit tests are supporting evidence.
3. Evidence runner attempt on the relevant available VM after implementation, not
   just a proposed command left to the owner. Owner executes only inaccessible
   control-plane actions and interactive auth/observations.
4. A sanitized report with what changed, what ran, what failed and the next fix.
5. Sol review: PASS advances; FAIL supplies a concrete next fix in the same scope;
   BLOCKED identifies the unavailable external capability and one bounded owner
   action while independent work continues. A guard/refusal is not the fix.

On implementation resume this loop proceeds through M0–M4 without another broad
approval per packet. Pause only for owner-only launch/login/UI interaction, an
actual unsafe boundary, unavailable hardware/budget or a new effect. Do not
request another permission merely to diagnose or fix an in-scope failure.
Add a new guard only for a demonstrated invariant violation with a regression
and an operational recovery path; more refusal conditions alone are not progress.

No identical rerun without a changed hypothesis/input or a diagnosed transient.
After two attempts with no new information, Sol must change the diagnostic or
implementation approach, not add another guard or demand another broad approval.
Do not retry indefinitely. Keep the existing 360-minute live campaign ceiling and
35-minute cleanup reserve; a planning iteration does not reset a live budget.
The final frozen acceptance runner's single-retry policy is NOT a global ban on
development iterations with changed fixes/revisions inside the approved envelope.
Do not weaken its release accounting to enable development.

### Gate semantics — completion tests are NOT implementation permissions

Maintain three DIFFERENT states:

- **Implementation:** TODO -> EDITING -> TESTABLE. A FAIL/NOT_RUN acceptance result
  never blocks this state. The repair code is explicitly allowed to be written.
- **Acceptance:** NOT_RUN -> PASS / FAIL / WAITING_OWNER / BLOCKED_EXTERNAL for
  each check. FAIL goes back to EDITING for that check, not to a global closed gate.
- **Release:** NOT_READY until the later full release criteria close. This has no
  authority to prevent experimental development or running a scoped repair probe.

Only genuine immediate safety/identity/resource constraints stop a LIVE operation;
they do not forbid implementing its fix or running independent safe evidence.
Missing auth can prevent a live model request, not boot/metrics/nested source work.
Missing nested KVM can prevent C launch, not finishing OpenAI development in N.

| Runtime evidence check | Prerequisite to ATTEMPT it | PASS condition | A failure opens this repair; allowed next work |
|---|---|---|---|
| DEV-BOOT | Exact new-profile ownership/absence, approved launch, sufficient actual resources; not metrics/auth/inference PASS. | Real ready QEMU/SSH and guest mester execution, /workspace usable. | Boot-stage repair (M1); M2/M3/M4 code/fixtures may proceed. |
| DEV-AUTH | Guest transport + owner login interaction + mediated public auth reachability. No local-model selection needed. | OpenAI stored/success enum from supported CLI, real login success, bridge cleanup checked. | Auth/egress/bridge repair (M2a); metrics and nested work independent. |
| DEV-WORK / MIGRATE | DEV-BOOT + usable OpenAI auth + owner-selected subscription/model route. | Actual N agent edits synthetic fixture, its test passes, tools execute in N. | Invocation/model/cwd repair (M2b); no full campaign prerequisite. |
| DEV-METRICS | Live guest transport + exact selected forward/service; no model request needed for HTTP probe. | Real CPU/RAM /metrics plus /summary, tool response; UI separate. | Metrics-link repair (M3); do M4 without waiting for UI. |
| DEV-CHILD-BOOT | N KVM/CPU/tools, inherited recursion budget, run-owned snapshot and RAM/disk, scoped launch authorization. | C actually boots changed source; N remains usable. | Recursion/adapter/bootstrap repair (M4a); no C auth/gateway/benchmark requirement. |
| DEV-SELF | Child-boot path and distinct failed/fixed snapshots. | Real intentional failure, fixed C passes candidate assertion, correct depth/boundaries/continuity. | Failing assertion repair (M4b); never retract unrelated working N features. |

### Deterministic failure -> fix table (do not return only "gate closed")

| Observed failure | Implementation/action that follows immediately | Evidence to rerun |
|---|---|---|
| Root/profile inaccessible in this agent shell | Finish controller probe implementation locally; owner runs ONE generated inspect command in the recorded O terminal. Do not guess another root or ask for it again. | Exact inspect report; no full campaign. |
| PID/start ticks/SSH identity mismatch | Reject that live transport; fix registry/listener association using selected-profile evidence. Never signal the unverified PID. | Identity regression + exact transport check. |
| Guest mester executable/PATH or XDG write failure | Verify existing source fixes and fixture; new candidate must freeze them. Repair only diagnosed guest package/parent-directory initialization. | Guest-user version and workspace write probe. |
| QEMU/bootstrap exit or timeout | Retain overlay/evidence, extract first stage/rc and bounded redacted excerpt; fix that stage, not diagnostic output alone. | Stage regression, then candidate boot. No cache reset. |
| Proxy CONNECT403 | Fixed synthetic HTTPS probe separates N policy denial from parent denial; repair selected `.mester-egress` rule/reload at owning layer. No completion retries as diagnostics. | Public auth HTTPS reaches expected HTTP response (401 may prove transport, NOT auth) + private destination still denied. |
| Browser callback/bridge cleanup failure | Fix existing bridge/cleanup and rerun cancellation/argument fixture; owner retries login once changed. Existing supported device flow is fallback if chosen. | Auth bridge lifecycle + real login, not VM reprovision. |
| Auth stored but real task fails | Classify provider/model lookup vs billing/auth vs tool/cwd/agent exit; fix native invocation/config, not VM boot. Owner handles account-only failures. | Fixed synthetic `work` task and external test verifier. |
| Metrics19532 foreign/unknown | Leave foreign process untouched; select/verify managed alternate port using M1 config support. No new global metrics gate. | Managed /summary + /metrics + selected guest forward. |
| Metrics host works, guest fails | Correct candidate forward or matched guest URL/ownership validation, not the HTTP collector. | Selected guest HTTP and negative foreign endpoint check. |
| No metrics forward in already-running candidate | QEMU cannot acquire it by changing guest config. Implement source fix now. Prepare one corrected fresh candidate through supported launch; keep authenticated N usable until owner privately logs into the corrected candidate and its work probe passes. No credential copying, unsupported runtime refreeze, in-place argv/metadata forgery or destructive rebuild. | Boot/forward and affected metrics checks; preserve working N/auth until replacement is independently usable. |
| Metrics guest HTTP works, UI/tool fails | Fix plugin renderer/transport/env. No VM recreation or inference deployment. | Tool result plus owner UI observation. |
| Self-test requires O parent profile while running in N | Implement guest-local adapter entry from M4a. Do not supply O control keys/root. | Guest-entry fixtures + real N->C boot. |
| Missing N KVM/CPU/tool/depth | Repair N launch/tool selection at its owning layer; do not edit recursion identity. Report exact missing hardware/config change to owner if inaccessible. Continue N coding/auth/metrics and child-runner fixtures. | Exact prerequisite and then child launch, not another whole-PR audit. |
| Child boot fails but N works | Repair C snapshot/bootstrap; retain N and its session, use a new failed/fixed snapshot as needed. | C regression/boot and N continuity. |

The sole reason to stop an entire session is no remaining safe in-scope work or
the owner's explicit pause. Otherwise return the failure, repair performed or
external action needed, affected rerun and independent next task. Do not end with
only FAIL/BLOCKED/"gate remains closed" or write another plan instead of a fix.

For live dependencies, recheck exact identity/liveness before use, not the entire
campaign. After a source change rerun its focused regression and affected runtime
checks; do not rerun all successful VM/auth steps for a metrics-renderer edit.
Do run the broad suite at a coherent checkpoint and before release. Preserve
evidence validity by recording which source/runtime/probe each result actually used.

## 3. Minimal evidence runner contract (implementation on resume, not now)

**Planned extension, NOT IMPLEMENTED:** a development-evidence mode in the EXISTING
`scripts/vm-host-acceptance.py`, not another driver file or campaign system. Reuse
its exact-profile transport and reporting plus existing redaction, metrics and
self-test helpers. No new dependency, daemon, validator framework or process manager.
Build only the current packet's probe; do not implement every mode before boot.

Freeze the following additive interface; it is **NOT IMPLEMENTED YET**:

```text
python3 scripts/vm-host-acceptance.py --dev-step STEP \
  --source SOURCE --state-root ROOT --profile PROFILE --run-id ID \
  [--dev-execute --confirm-run-id ID] [--dev-report ABSOLUTE_PRIVATE_FILE]
```

STEP is exactly `inspect|boot|auth|work|metrics|nested`. This is mutually exclusive
with campaign `--start/--resume/--execute/--cleanup/--suite/--stage`, discovery and
observation flags. Do not route it through `execute_stage`, `_campaign_result`,
campaign locks/attempt ledger or the old aggregate gate prerequisites. Preserve
those existing campaign interfaces unchanged. Source/IDs/name checks and exact
SSH identity checks ARE reused. `--dev-execute` is scoped action consent, not
permission to run any arbitrary guest command or to launch from the assistant.

- Without `--dev-execute`: observe only. May inspect selected profile/guest and
  make fixed bounded health/HTTP probes; cannot login, launch, change policy,
  start a service, edit a fixture or modify a VM. `boot` observes an owner-started
  candidate. Do not implement hidden auto-repair in an observation path.
- With it and matching ID: run only that step's documented synthetic exercise
  or run-owned guest worker. Explicitly selected owner-only actions remain owner-only.
  Missing consent reports `WAITING_OWNER`, not an implementation lock.
- Reports go to stdout by default. Optional private report creation requires a
  canonical existing owner0700 parent and a fresh owner0600 file; no source-tree
  outputs, credential files, or overwrite of earlier run evidence.
- Exit0: selected machine checks passed; exit1: observed feature failure; exit2:
  required external prerequisite/owner action unavailable. Always emit check
  results and repair task, including on failure. A human UI check can remain
  pending while machine checks pass; do not synthesize a human PASS.

Add a small `run_dev_step()` dispatcher and per-step functions in this script;
do not introduce another runner module/process framework. M0 adds inspect only;
later packets add their own step. Extend existing tests/test_vm_host_acceptance.py.
An unimplemented step returns a fixed `step_not_implemented` repair target, not
a successful placeholder. Luna verifies implemented flags/help on resume before
giving an executable command to the owner. Do not run the proposed CLI now.

Per-step report fields (fixed schema; identifiers, not secrets):
`step`, `run_id`, `location` (O/N/C), `source_head`, `source_tree_sha256`,
`profile`, `runtime_payload_sha256`, `guest_payload_sha256`, exact process
identity summary, `checks[]`, `result`, `reason_code`, `elapsed_ms`,
`next_action`, `protected_assets_unchanged`, and bounded artifact references.
Per check: `id`, `expected`, `observed`, `status`, `cause_exit_code`. Report source
and frozen runtime identities separately; one must not stand in for the other.
Missing capability -> report before dependent action, with the exact failed
predicate. Preserve the first operational cause instead of returning only rc2.
Continuity/preservation checks must identify what was measured; use UNKNOWN or
NOT_RUN for unobserved assets, never an unconditional `true` preservation claim.

Never collect tokens, auth DBs, browser callback URLs, environment dumps or owner
prompts/transcripts. Auth reports store presence/result enums only. Model proof
uses a fixed synthetic task and records verification/digest, not model content.
Reports are private, outside the shared checkout. Unit-test reports cannot set
real-runtime checks PASS. Independent checks continue after an unrelated failure.

Implementation MUST include fixture failures for unreachable transport, stale
identity, guest command failure, HTTP/proxy denial, missing tool and timeout;
assert they yield distinct next actions and do not mutate protected assets.
Default bounds to implement: read-only helper 20s/64KiB; synthetic work/model
task 120s; metrics 5s/64KiB; existing boot/bootstrap/child driver deadlines retained.
Interactive owner auth has an explicit cancel/deadline and is not polled forever.
These are development probe limits, not a new inference latency/throughput claim.

Use per-check statuses `PASS|FAIL|NOT_RUN|WAITING_OWNER|BLOCKED_EXTERNAL` in this
development-only report. Every non-PASS includes `repair_packet`, `next_action`,
and `blocks_live_checks[]`. This last list is limited to actual dependent runtime
checks, never an `implementation` or `all_work` pseudo-gate. Fixture coverage must
prove a failed metrics check does not suppress nested execution, and failed auth
does not suppress guest HTTP metrics evidence. The runner must not read its own
previous aggregate FAIL as a prerequisite to starting the repaired step.

## 4. Ordered implementation packets

### M0 — establish a usable evidence path, without another topology questionnaire

**Luna/O, first packet after resume.** Editable: existing acceptance runner's
development `inspect` mode + focused tests, diagnostic helper only if needed,
PROGRESS. No VM mutation. Do not build the other modes as an M0 prerequisite.

- Use the recorded root/profile, exact ready status and existing SSH registry.
  Inspect that profile's max_depth, frozen/source identities, guest mester PATH,
  workspace writability, KVM/CPU, memory headroom and required recursion tools.
  Record whether this is O controlling N, not guessed H. Do not inspect auth stores.
- Identify the exact failing proxy policy layer from a bounded synthetic request
  and redacted denied hostname/category; do not dump all policies/logs. Distinguish
  O's upstream proxy from N's own proxy. Never use a live completion retry as a
  network diagnostic.
- Output a one-screen summary plus report. Reuse information already supplied;
  do not ask owner for root/profile again or rerun local-provider discovery.
- **M0 implementation acceptance:** fixtures prove correct CLI routing, no mutation
  in observe mode, exact-profile-only transport, per-check failure continuation and
  repair target mapping. A real inspect attempt emits a report even if transport
  is unavailable. M0 does NOT require all inspected features to work.
- **Concrete first patch:** reuse `_profile_record(..., campaign_profile=False)`,
  `_validate_profile_runtime`, `_listener_owned_loopback` and `_guest_exec` with the
  exact-profile option. Add fixed guest probes, not caller-supplied shell/URL input.
  Keep existing discovery semantics unchanged; no INF-01 reimplementation.
- **Failure -> fix:** transport failure repairs the specific transport path;
  private state mismatch diagnoses only the selected path. No old-root migration.

M0 tests to add: CLI campaign/dev conflict; missing consent zero writes/launches;
stale identity zero SSH; SSH unavailable report retained; guest command failure
keeps cause rc; auth403 does not hide metrics checks; failure report explicitly
names the next M1/M2/M3 repair. Do not run the full suite repeatedly while writing
these fixtures. Focused test first, one coherent broad checkpoint afterward.

### M1 — launch one recursion-capable replacement development candidate

**Luna/O source work; owner performs launch under existing owner-launch policy.**
Editable: `lib/vm/{guest-bootstrap.sh,lifecycle.sh,services.sh,recursion.sh}` and
`runner.sh` only for a diagnosed defect; actual proxy config/renderer when needed;
focused tests and evidence driver. No unrelated cleanup/refactor.

Decision frozen for the known cap-0 case: **preserve the existing profile; create
one new dedicated candidate from reviewed current source with recursion enabled**.
Do not patch the old frozen runtime or raise its persisted cap. If M0 disproves
the cap-0 fact, Sol may select reuse only with recorded metadata evidence.

- Use the same known launch environment O and owner-approved checkout. New profile
  name is `branch-dev-<fresh-evidence-id-prefix>` under the recorded dedicated root;
  print/check exact absence and resource budget first. Do not select an old profile
  on collision. Keep the existing ready N and O sessions as rollback; if concurrent
  memory is insufficient, present exact deficit and a named non-destructive suspend
  choice to owner, rather than stopping/killing a VM automatically.
- Fresh source must include existing bootstrap/XDG/browser-cleanup fixes. Reuse
  latest-once/version policy; do not invent a version pin or silently reinstall O.
  Reuse the verified immutable base cache through supported lifecycle; do not
  redownload/rebuild a base merely because the new candidate has a new name.
- Implement the already-specified exact-owned egress-reload dispatch (M2a detail)
  and metrics-port selection below BEFORE this initial candidate runtime freezes.
  Do not knowingly launch another candidate missing a source fix already in this
  plan, then discover its immutable runtime cannot use that fix. M2a exercises the
  prepared path and repairs only genuinely new faults. Boot still need not wait
  for live auth, rendered metrics or full acceptance to pass.
- Set supported max depth 2 at candidate creation for changed-VM self-test; preserve
  actual ancestry. Ensure N receives usable nested KVM/CPU and recursion tools.
  Never forge a recursion marker or bypass inherited cap. If O imposes a lower
  cap, the owner changes only the authoritative launcher/config at the correct
  ancestor; report exact required change, not a generic capability refusal.
- OpenAI is via native remote auth/public egress, not a local-model guestfwd.
  `provider=none` is NOT a blocker to native login/configuration. No local server,
  GPU deployment, gateway or >=95% benchmark prerequisite for this candidate.
- Permit the exact required public OpenAI/auth/workflow destinations through the
  existing mediated proxy policy, at the diagnosed layer. Retain private/reserved/
  mixed-DNS and host/LAN denial, QEMU restriction and no direct fallback. No blank
  allowlist, unrestricted CONNECT, host networking or new authority over O.
- Include metrics forward/service using its existing ownership checks; if a foreign
  listener collides, use a fresh recorded managed port, never adopt/kill the foreign
  service. Do not make metrics success a prerequisite for boot/auth debugging.
- **Known source fix to include before candidate freeze:**
  `vm_lifecycle_metrics_select_port` currently asks for 19532 and discards any other
  port. Add an optional validated host config field `metrics_port` (default19532,
  integer1024..65535; no secret); pass it to existing metrics ensure/verify and
  return the actual selected host port. Guest metrics port remains19532. This is
  host-local selection, not inherited metrics in C. If default is occupied by an
  unverified listener, the development plan may choose one OS-allocated loopback
  port, record it in that candidate's config and use existing managed ensure;
  never scan ports, adopt the listener or run broad cleanup. Limit bind-race retry
  to three newly selected ports. Preserve unrelated config fields. No port picker
  framework or automatic edits to legacy state. Tests: foreign19532 survives;
  selected nondefault host port reaches QEMU argv/verification; C inherits none.
- Evidence runner `boot`: ready, mester execution, writable /workspace, actual
  identities, KVM/recursion prerequisite results and O/old-N continuity. On failure,
  automatically collect bounded failed-stage diagnostics in the same attempt.
- **Pass:** current-source N candidate ready and attachable; positive boot plus
  invalid-path/foreign-identity negative checks. **Next:** M2, not C14/campaign.

M1 tests/edits are not another lifecycle rewrite: retain current package/XDG fixes;
test new-profile cap2/tool selection and cache reuse in tests/vm-lifecycle.bats /
tests/test_vm_guest.py; metrics config cases in tests/test_vm_paths.py and
tests/test_vm_metrics_service.py. Preserve paused/ready overlays and frozen code.
Owner-started boot failure -> append its failed stage/rc/log excerpt to the SAME
attempt's report, implement the diagnosed fix and choose only the necessary new
snapshot/candidate. Do not restart from discovery or pre-download checks.

### M2 — native OpenAI auth and one actual coding task, then move the session

**Luna/O only for necessary bootstrap fixes; owner login/attach; then Luna/N.**
Editable: browser bridge/services/native config/proxy policy code only for observed
faults, focused regressions, evidence driver. Use the candidate's trusted controller.

- Run native browser auth via existing bridge, with fixed-source cleanup working;
  if browser is unavailable, use installed OpenCode's documented headless/device
  method as an explicit owner choice, not a new auth framework. Confirm supported
  CLI/version from installed help; don't copy an old login command blindly.
- Owner completes login privately in N. No copy of O/host credentials; no token/DB
  inspection or export. Report only login stored/success/failure and cleanup result.
- On proxy403, collect exact policy layer/denied public hostname and fix that scoped
  rule. On browser timeout/cleanup failure, fix bridge lifecycle. On unavailable
  OpenAI model selection, show safe supported choices in the guest rather than
  restarting provisioning. No raw callback URLs in evidence.
- **Source work, not an auth questionnaire:** proxy.py reads the selected workspace's
  `.mester-egress` and supports SIGHUP reload; launcher `egress-reload` currently
  refuses. Implement that already-planned exact-profile command using registered
  proxy identity, not `pgrep`/raw PID. Verify the proxy and profile ownership before
  signalling; invalid policy must retain the previous policy. Merge required public
  OpenAI destinations into the selected policy, preserving all unrelated rules and
  private/host denial. If denial is O's ancestor proxy, give the owner ONE exact
  policy/reload action at that layer; N must not control an ancestor. Do not add
  blind auto-allow of hostnames copied from errors.
- Wire `auth` evidence to existing bridge result/cleanup marker and supported
  installed CLI's credential-presence enum, never DB contents. Regression fixtures:
  old cleanup14-argument case, cancelled browser, failed reverse tunnel, new login
  with existing safe guest directories, proxy-layer403 and invalid-policy reload.
  Use tests/test_vm_browser_bridge.py, tests/test_vm_proxy.py, tests/vm-agent.bats.
- Owner selects OpenAI model/subscription route and approves one bounded synthetic
  completion/coding smoke; do not assume paid API charges are allowed. Required
  proof: native agent in N creates a temporary synthetic project/test, makes a small
  requested change, and its test passes. Test fixture results alone cannot pass it.
  Harmless test execution is part of the resumed packet, not this planning session.
- The owner selecting the ChatGPT subscription route/model for this resumed
  coding session supplies the synthetic-task consent; do not ask again for every
  task/retry. Only switching to separately billed API credentials/traffic needs a
  new billing decision. Never persist a token in a report or copy O's auth state.
- Check tool execution actually occurs in N (guest identity + /workspace), source
  edits are visible in the intended checkout and O remains responsive. Avoid
  concurrent real-source edits from O and N.
- Freeze the `work` exercise: create an initially failing standard-library Python
  unittest in a generated private temporary guest directory; ask the real native
  OpenAI agent to implement a tiny deterministic function (for example add(a,b)).
  The external runner, not the model, runs the test and verifies an expected file
  digest/semantic result. Record only success/cause/time; synthetic output only.
  Capture agent exit status and test status separately. Zero auth/model/tool calls
  in observe-only mode. No `echo`/shell-only substitute for an agent task.
- **Pass = MIGRATION:** owner opens the next implementation session inside N at
  /workspace; choose Luna max. Stop routine implementation in O. O becomes a
  launch/rollback terminal only. Sol medium reviews N's report in the new session.
- Do NOT wait for metrics, local models, gateway, full recursive acceptance, all
  historical security checks or C14 before this migration. Unpassed features stay
  explicitly unpassed, not prerequisites to using the experimental dev VM.

### M3 — metrics visible to the agent inside N

**Luna/N**, with only exact O-side managed service/forward actions handed to owner
when the guest cannot control them. Editable: metrics service, guest renderer/TUI,
existing forward validation/services, focused tests and evidence driver.

- Follow one request end to end: owned service -> selected QEMU guestfwd -> guest
  METRICS_URL -> tool response -> displayed metrics. Record which link fails.
- Reuse owned service or allocate a recorded managed instance; no inherited Docker
  DNS, assumed ancestor endpoint or foreign service takeover. No invented metrics.
- `metrics` evidence: actual bounded request parses CPU/RAM (GPU availability may be
  absent), native tool returns values, owner confirms UI where human-only; disabled/
  foreign endpoint negative check. Missing optional GPU != blanket metrics failure.
- Implement separate checks `metrics_service_owned`, `metrics_host_http`,
  `metrics_forward_matched`, `metrics_guest_http`, `metrics_tool`, `metrics_tui`.
  `/summary` is text/plain; `/metrics` is JSON. Don't parse summary as JSON or call
  an HTTP200 alone display evidence. Emit the first failed link's repair and keep
  later independently possible checks. service/port selection is M1 code, not a
  reason to postpone it until after a candidate boots without the needed forward.
  Focused coverage: tests/test_vm_metrics_service.py, tests/vm-metrics.bats and the
  existing host-metrics tool/TUI tests. Renderer/tool fix cannot require C readiness.
- Fix failed link, rerun that probe, then end-to-end. OpenAI development work in N
  continues independently if a metrics UI defect remains.

### M4 — from N, create/test a different VM using modified replacement code

**Luna/N**; O may perform only required exact outer control actions. Editable:
existing `vm-self-test.py`, recursion/workspace/lifecycle/proxy helpers, driver and
focused tests. Reuse the live adapter; no second recursive supervisor.

- Explicit adapter gap to resolve, NOT to defer: current `vm-self-test.py --live`
  takes a control-plane `--parent-profile` at ready depth 0. Do not pass O's private
  profile root into N or run the whole test in O and call that in-N implementation.
  Add a guest-local entry to the SAME existing adapter (proposed `--guest-live`,
  not yet implemented), validating N's persisted identity/cap and N-local private
  state instead of requiring O's `--parent-profile`. Keep the existing controller
  entry compatible; reuse snapshot/lifecycle/report code, no second supervisor.
  Candidate launcher/bootstrap must run in N. O may collect reports parent-initiated;
  no child-to-parent management MCP/HTTP endpoints. Positive test asserts worker
  location N; wrong-location test refuses without creating C.
- Freeze the guest entry inputs: `--guest-live --source /workspace --state-root
  <N-local-private-root> --run-id ID --confirm-run-id ID --yes --max-depth 2`.
  `--guest-live` is mutually exclusive with controller `--live`; it rejects
  `--parent-profile`. It reads the genuine persisted N identity, requires depth
  below inherited cap and usable local KVM, and creates children at parent+1.
  It never reads an O root, requires N's own profile on disk, or resets depth0.
  Keep controller `--live` behavior compatible. This is proposed syntax to implement,
  not an existing owner command. Add it to installed help before live handoff.
- N selects a filtered immutable snapshot of changed source, new C profile/overlay,
  run-owned ports/RAM/disk and inherited recursion depth. C initially uses model
  none and no browser/credential/metrics inheritance: real VM correctness does not
  need local inference or cloud credentials in C.
- Evidence `nested`: candidate source/payload hashes; deliberate bootstrap failure
  in disposable snapshot; diagnosed fix/new snapshot; actual C readiness/workspace
  smoke; candidate-specific behavior assertion; depth-limit refusal and isolated
  child-network/filesystem checks. N's authenticated session/source/workload survives.
- Start with one real N->C boot/smoke to debug lifecycle, then extend the SAME runner
  to changed-code failure->fix and full required depth proof. A smoke is a useful
  milestone, not full E2E-SELF PASS. No full campaign/UI/benchmark prerequisite to
  that first bounded boot. Report insufficient nested KVM/RAM at the exact layer.
- Preserve failed evidence; clean only exact run-owned stopped assets through
  supported lifecycle after owner-selected retention. Never delete N/O to prove
  cleanup. Once this works, resume broader acceptance from N.

M4a code acceptance in tests/test_vm_self_test.py: guest mode has no local
parent-profile dependency; real depth propagates; wrong identity/cap makes zero
snapshot/launch calls; child failure retains cause; metrics/auth failures do not
block a model-none C boot. Real evidence must show the candidate QEMU launch ran
inside N, not merely that O launched another VM. M4b then reuses runner snapshot
failure/fix hooks; do not build a second test supervisor or new protocol.

## 5. Work deferred without being erased

- INF-01 report remains useful; no more topology questioning for OpenAI work.
  INF-02–08 serving isolation/gateway/relay/local-model >=95% performance remain
  required for delivering THAT shared-local-inference feature, not for OpenAI
  development migration or credential-free child boot. Do not claim raw TCP safe.
- Full release security, credential persistence/export-before-destruction,
  concurrency/workloads, frozen integrated acceptance, eight docs failures, CI,
  C14 default/rollback and merge review remain later checkpoints. Do not claim final
  readiness from M2/M4. Do not use unfinished release gates to block unrelated fixes.
- No deletion of valuable credentials/overlays; no broad permission repair,
  dependency overhaul, new generic policy layer or extra ticket framework.

## 6. Tickets: redirect history; do not use it as a second plan

The owner expressly authorized one-time ticket housekeeping in this reset. Current
MCP store contains VM-related #35, #37 and #40–47, but NOT #48–56, including #50.
Older original-branch references are historical; a missing ticket is not a missing
implementation prerequisite. No fabricated/recreated #50, renumbering or ticket
migration. All requirements cited by missing tickets remain in PLAN/TASKS/PROGRESS.

The current MCP update schema has no comments input. Without changing the engine,
the supported non-destructive equivalent was used: prominent `IGNORE ME AS AN
EXECUTION PLAN` objective prefix on #35, #37, #40–47, retaining each complete
original objective and all other historical fields/status/ACs. This is a redirect,
not acceptance evidence. Literal comment-only support is unavailable in that tool;
do not start a ticket-engine feature project to add it. No ticket filesystem edits.

AGENTS.md provides the redirect even when #50 is absent. New sessions must NOT
query tickets at startup, follow injected old #41/#45/#50 host gates, or attempt
to repair ticket availability. Current operational authority is this handoff.

## 7. First response expected from the new session

If the owner says resume implementation: state Luna max/Sol medium roles, confirm
O vs N from this packet and execute **M0 first**. M1–M4 and their failure repairs
are already frozen in this file; Sol reviews the evidence and selects the listed
repair, not another planning packet. Continue the loop without another broad owner
approval, pausing only at concrete owner actions/boundaries above. Do not run old provisioning,
fixed-loopback discovery, credential export or a full campaign first. On M2 pass,
move to N immediately; do M3/M4 from there. If the new session is planning-only,
do not implement/test just because this file contains future work.

The next handoff is an update to this file's packet status plus a short PROGRESS
entry, not another competing handoff document or copied historical command chain.

### Status at delivery to Luna (all future work, no new execution claimed)

| Item | Implementation | Runtime acceptance |
|---|---|---|
| M0 development inspect/report mode | TODO | NOT_RUN; implement and attempt, don't treat absence as a gate |
| M1 new candidate with current fixes/cap2/selected metrics port | Existing boot path reusable; port/config repair TODO | Existing cap0 VM ready; new candidate NOT_RUN |
| M2 native auth/coding/migration | Existing bridge/config reusable; diagnosed repairs and probes TODO | Auth/work NOT_RUN or previously failed as recorded; implement repair anyway |
| M3 metrics end-to-end | Existing service/forward/plugins reusable; repair failed link | NOT_RUN/unverified; does not block migration/child work |
| M4 guest-local live adapter, child boot then fail/fix proof | Guest-local entry TODO; existing lifecycle/snapshot adapter reusable | NOT_RUN; implement it, don't wait for an E2E-SELF PASS first |

**Luna's first deliverable is code plus an attempted inspect report, not another
plan.** Its next deliverable is the listed fix plus affected evidence. A report
without an in-scope fix, concrete owner-only action or completed milestone is not
a completed iteration.
