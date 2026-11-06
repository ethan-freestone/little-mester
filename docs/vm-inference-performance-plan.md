# Pre-C14 inference isolation, performance and execution handoff

**Status: owner-approved direction; detailed execution plan; implementation NOT started.**
**Approval: 2026-11-06. Implementation checkpoint: `ca6da6c`.**

This is the normative amendment to `PR_REIMPLEMENTATION_PLAN.md`, not a second
independent architecture. Read this document before executing any older model
discovery, configuration or campaign handoff. The owner approved the proposal in
the conversation, then requested: fix reported CI, commit existing work, write
very detailed steps, **pause**, and only later resume implementation/testing with
an owner-selected model. No new inference implementation or live operation is
authorized during this documentation phase. No push, merge or C14 activation now.

## 1. Executive decisions and approval boundary

1. Optimize the ordinary **one-level, persistent agent VM** first.
2. Default candidate: one accelerator-backed inference deployment outside agent
   VMs, exposed only through an explicitly selected inference capability.
3. Do not copy model servers/weights into every VM or nested VM by default.
4. Agent tools/MCP/shell/workload execution remain inside the agent VM. The model
   service returns data/tool-call descriptions; it must not execute agent tools.
5. A fixed TCP destination alone is not an inference-only security boundary.
   Replace unrestricted model-API exposure with an operation-constrained gateway.
6. Gate the new default candidate on measured performance **and** isolation.
   Never weaken isolation to reach a throughput target.
7. Preserve VM-local inference as a defined alternative. Its implementation is
   deferred unless measurements or a new owner decision bring it forward.
8. Approved initial one-level throughput target: at least **95%** of a comparable
   direct baseline. First-token latency needs a measured, explicitly frozen budget;
   it is not silently waived or assigned an invented number by an implementer.
9. Preserve the existing four-package sequence: development usability; remaining
   security/evidence implementation (now including these pre-C14 tasks); frozen
   integrated acceptance; separately reviewed C14 cutover/default/rollback.
10. C14, release and eventual merge still require real evidence and passing required
    checks. The owner's goal is merge, not permission to merge a failing candidate.

### What approval does NOT authorize

- No arbitrary host service/container start, GPU reassignment/passthrough, model
  download, paid API call, host package installation, firewall/NAT change or LAN
  listener. Concrete live effects require the scoped owner handoff first.
- No adopting/killing/restarting the unverified parent metrics listener.
- No patching the existing ready VM's frozen controller or historical snapshots.
- No blanket chmod/chown, credential copying, raw auth-store reads or deleting
  overlays/archives. No changes to the preserved `vm-migration-plan.md`.
- No ticket calls/writes for this work. Replacement plan/progress is the authority.
- No automatic switch to a larger model or spawning additional agents. The owner
  always chooses the implementation/review model and when to change it.

## 2. Known state versus unknowns

### Verified observations

- Replacement branch is `feat/vm-approach-2`. Checkpoint `ca6da6c` captures CI and
  accumulated bootstrap/diagnostic/recovery/ownership fixes.
- Ordinary `./lab` remains Docker; `./lab vm` is explicit and experimental.
- Development profile `branch-dev-20261105`, exact run
  `71ff52d4-d3d5-4394-a43d-440b1929f07c`, became ready in the existing parent VM.
  Dedicated root is `$HOME/.local/share/little-mester-branch-dev-vm-approach-2`.
  Do not substitute a similarly named profile or the legacy/default state root.
- Owner applied bounded public-package and XDG-parent ownership corrections;
  OpenCode `--version` as mester printed `opencode v2.2.5`. That is observed
  software, not an installation pin. Latest-once/pins/in-VM updates remain supported.
- Owner says TUI opens and Escape handling works. Metrics, local completions,
  actual permission behavior and full UI usability are not accepted yet.
- Browser bridge was explicitly disabled on the diagnostic attach. Its frozen
  controller retains a known cleanup-argument defect; source is fixed. Possible
  old session helpers still need exact-identity inspection, not a broad kill.
- Parent-VM loopback discovery at :11434 and :8080 returned no model IDs. This
  proves neither physical-host service absence nor an upstream relay's existence.
- Remote-model request hit proxy CONNECT403. Exact denied hostname/policy layer
  is unknown. The checkout currently permits package domains, not general cloud
  destinations. Do not empty the allowlist to make the error disappear.
- Checkpoint validation: Python380/380, Bats149/149, Node143/143 and package/build
  checks passed. Expanded CI ShellCheck command passed. Eight longstanding docs
  drift/link checks remain failures and must close before final delivery.

### Source-confirmed gaps

- `lib/vm/runner.sh` currently maps a selected guest model address with socat to
  the immediate launching machine's loopback port. It is raw TCP, not route/schema
  restriction, and must not be advertised as inference-only.
- `PR_REIMPLEMENTATION_PLAN.md` §6 and TASK-12 require an optional immediate-parent
  model relay. The reviewed replacement launch paths do not establish the complete
  relay from an existing upstream guestfwd into the next child's selected loopback
  endpoint. Passing provider flags is not that implementation.
- Provider discovery assumes local listeners rather than describing/revalidating
  the executing level and its already-authorized upstream capability.
- Ready-profile provider configuration is tied to its frozen runtime, readiness
  data and QEMU forward. Editing a guest config alone cannot safely change it.
- Real filesystem/canary, synthetic persistence/export safety and required
  integration procedures still have recorded gaps. This amendment does not erase
  them or convert mocked coverage into host evidence.

### Unknowns to resolve, not guess

Actual H/G0/G1 topology; serving backend/version; GPU availability and ownership;
selected model/quantization/context; existing upstream model forwards; exact
management endpoints; safe service isolation; protocol library; usable direct
baseline; TTFT budget; serving/concurrency/download/disk limits. An implementer
must collect safe evidence and present a bounded choice, not ask the owner to
reconstruct facts that the source or read-only topology report can establish.

## 3. Security and trust contract

### 3.1 Names and invariant

- **H:** physical host or actual serving-control machine, explicitly identified.
- **G0:** first agent VM launched by H in the release topology.
- **G1/G2:** nested agent/test VMs. Depth flags must match actual ancestry.
- **B:** inference backend, outside agent VMs, with selected model/accelerator.
- **C:** policy-enforcing inference capability gateway adjacent to B.
- **R0/R1:** optional parent-local relays for a selected capability, not new models.

```
H: isolated B + C, one loaded model on the selected accelerator
      C (fixed inference operations; loopback/private controlled transport)
        ^
        | selected QEMU guestfwd only, restrict=on
G0: agent + guest-local tools
      R0 -> only G0's already-selected C guest endpoint
        ^
        | selected G1 guestfwd only
G1: agent + guest-local tools
      R1 -> only G1's already-selected upstream model capability
        ^
G2: agent + guest-local tools; no implicit new capabilities
```

The same design applies when the current development checkout executes inside an
older parent VM. Do not relabel the parent as H merely because the command runs
there. Describe the outer launch and every effective forwarding hop.

**Invariant:** guest code, including guest root and arbitrary guest HTTP clients,
gets no general host/ancestor filesystem, process, credentials, Docker socket,
loopback or LAN authority. It gets only explicit constrained capabilities and the
owner-selected workspace. Tool execution is guest-local, not server-side execution
at B/C/H. UI permission rules are not the containment boundary.

### 3.2 Explicit exceptions; no false zero-access claim

The approved writable workspace is deliberately accessible across the VM boundary;
the agent can corrupt it. Nested tests instead receive filtered parent-selected
snapshots. Selected inference, curated metrics and optional native browser auth
are separate controlled crossings, never general host access. Public HTTP(S)
egress follows independent proxy policy. No claim of protection against all QEMU,
KVM, virtiofs, kernel or serving-library vulnerabilities. The backend must be
isolated too, so its compromise has bounded authority.

### 3.3 Serving isolation (a required decision, not a silent deployment)

INF-02 must select and document an isolation envelope appropriate to actual
hardware. An already-isolated service may be reused after verification; otherwise
present a proposed container or dedicated serving-VM recipe for owner approval.
No dependency on host Docker is introduced for ordinary agent launch; no agent
ever gets that daemon's socket. Guest Docker remains for project workloads.

Minimum properties to verify:

- Backend has selected weights and necessary accelerator devices only, not the
  working checkout, host home, SSH/auth directories, control state or Docker socket.
- No backend shell/tool executor, arbitrary MCP or agent control integration.
- Weights read-only during normal inference; administrative downloads/mutations are
  owner-side operations outside the guest capability.
- Unprivileged service where supported; no broad privileged/container/device mode,
  host networking or unrelated GPU/device access to save setup time.
- Endpoint not exposed on LAN/wildcard by default. Backend API unreachable from
  agent VM except through C; bypass endpoints are absent.
- Resource limits/fairness, bounded diagnostics, no request/prompt/token logging.
- Backend egress and any server-side URL/file features explicitly constrained;
  an inference request must not become an SSRF or host-file read capability.

If equivalent safety cannot be established, report BLOCKED. Do not deploy a
privileged service or raw backend forward as a temporary accepted solution.

### 3.4 Gateway and relay contract to freeze before coding

INF-03 produces the exact protocol table for installed versions. Preferred initial
surface is compatible chat streaming plus a sanitized selected-model list. This
document does NOT assert that every version uses the same endpoint/payload.
Implementers must verify actual SDK/provider traffic in synthetic fixtures and
versioned documentation. If an adapter needs additional operations, present why
and extend the reviewed table before implementation, not by allowing `/api/*`.

Required rules:

1. One immutable, verified upstream per selected capability. No request-supplied
   destination, port, Host override, CONNECT, Upgrade or proxy mode.
2. Exact allowed method/path combinations, strict decoding/normalization and body
   schema. Reject duplicate/ambiguous framing, traversal/encoded path bypasses,
   unsupported content encodings, unbounded headers/body and excessive nesting.
3. Selected model IDs only; model-list output is curated, not arbitrary backend
   metadata. Reject unknown fields by default unless explicitly classified safe.
4. No model pull/create/delete/load-management, filesystem paths, server-side tools,
   arbitrary remote image/file fetch, administration or credential forwarding.
   Tool-call definitions/results that the guest harness needs are data; they must
   never activate a backend executor. Review each backend's actual semantics.
5. Reject redirects rather than follow them to arbitrary destinations. Do not
   relay guest Authorization/proxy cookies/admin headers to B. Any needed backend
   credential stays private to C and never appears in guest/source/argv/reports.
6. Correct byte-stream/SSE forwarding with immediate incremental delivery, bounded
   queues/backpressure, request/idle deadlines and cancellation. Do not wait for
   the complete generation or buffer whole responses to inspect them.
7. Fixed concurrency/resource limits. Return actionable overload/unavailable/
   denied reasons without raw request data. Never transparently replay a request
   after any output was delivered; no hidden paid or duplicate generations.
8. R relays only an already-approved immediate-parent inference endpoint, selected
   by parent-owned metadata. Child arguments/markers cannot choose arbitrary
   loopback services. No relay auto-discovery, ancestor fallthrough or direct path.
9. Reuse keepalive/pools where protocol supports it; no process/reconnect per token.
   A per-connection socat hop is not automatically a per-token hop: measure its
   costs before replacing it. Prefer the smallest maintained secure implementation.
10. Transport choice and any new dependency must be reviewed in INF-03. Do not
    invent a new HTTP framework, duplicate public egress proxy logic or treat a
    hand-written parser as safe merely because it is stdlib. If a maintained
    dependency is necessary, provide version/license/support/package implications
    and obtain the owner's choice before adding it.

The public Internet egress proxy remains separate. Do not add private model
addresses to its public-destination policy or open host routing to support models.
Metrics/browser are not tunneled through the inference gateway. No SSH user
config/agent-forwarding/auth-master reuse for model transport.

## 4. Performance contract and measurement method

### 4.1 Approved goal and unresolved numeric choice

Normal path is one long-lived G0 using B's existing accelerator efficiently.
One-level generation throughput must reach >=0.95 of comparable direct baseline.
Security failure blocks regardless of speed. A low baseline is not evidence of
good absolute performance: report absolute rates and compare with the verified
device/model configuration, including CPU fallback or throttling if observed.

TTFT budget, representative context/output sizes, concurrency limits and acceptable
resource envelope are owner-frozen from measured baseline in INF-08. No implementer
may silently set a generous threshold after seeing candidate results. If owner has
not selected the budget, latency verdict is BLOCKED rather than implicitly PASS.

### 4.2 Required experiment controls

- Same B instance/version/model bytes or verified artifact identity, quantization,
  context capacity, device/offload, sampling/seed options and generation lengths.
- Predefined synthetic prompts with no project data/secrets. Reports retain only
  case IDs/hashes, counts and timing; no prompt/model-output transcripts.
- Preload once, distinguish cold-load from warmed inference. Record actual output
  token counts; do not compare tokens/second from different tokenizers or estimators.
- First characterize direct B from its authorized control context, then C from
  that context, G0 through C, then actual nested G1/G2. Guest never receives direct
  B access merely to build a baseline. No benchmark ports bypass the boundary.
- Warm up at least two requests per tested route, excluded from scoring; execute
  at least five measured repetitions per fixed case/route. Pair/interleave direct
  and candidate trials to reduce clock/thermal/cache drift. Invalidate mismatched
  output lengths/configuration rather than cherry-pick favorable runs.
- Test serial interactive case first; explicit concurrent case at two clients only
  when resource/serving scope permits. Larger concurrency is optional, owner-set,
  and not authority to consume all host resources or load extra models.
- Define small/typical and larger-context cases from verified model capacity;
  document sizes before running. Cover incremental tool-call/structured response
  compatibility needed by the actual agent, not only plain text output.
- Measure client TTFT with a monotonic clock to first actual content/token event,
  not socket connect/HTTP headers; document treatment of reasoning/tool events.
- Generation rate = emitted comparable tokens divided by generation duration;
  report duration convention and backend-provided prefill/decode timings separately.
- Aggregate throughput = total completed output tokens / common wall interval,
  including queue effects; report queue time, completion/error rate and fairness.
- Five observations give only a weak tail estimate. Label empirical p95/sample
  count honestly; increase sample count within frozen budget if a meaningful tail
  decision cannot be made. Do not claim statistical confidence without analysis.

### 4.3 Pass/fail and failure classification

Record per-case median and all sanitized samples, not only an overall average.
For each required warmed serial case compute:

`one_level_ratio = median(G0 decode tokens/s) / median(direct B decode tokens/s)`.

Every required serial case must meet0.95. Report gateway-only and networking delta
separately. Compare TTFT with the frozen absolute/relative budget and aggregate
throughput with the frozen concurrency target. Connection reliability requires
zero unexpected errors in the bounded normal-case sample; intentional disconnect/
overload/outage cases have explicit expected outcomes, not counted as normal PASS.

Nested ratios/TTFT are required evidence and correctness/reliability gates, but
not a made-up95% requirement for every depth. The owner decides any nested
performance threshold before its scored run. Slow nesting never excuses direct
ancestor access, model copying by default or dropping actual nested proof.

On a miss: preserve result -> classify GPU/model/queue/prefill/gateway/transport/
policy -> propose one bounded change -> local regression -> fresh comparable
measurement. Never loosen security, reduce output/context/concurrency only for the
candidate, change quantization or add unlimited retries to manufacture a pass.
If safe architecture cannot meet the ordinary-path gate, pause for owner choice
on optimization or bringing VM-local inference forward (§9).

## 5. Universal task packet for a less-capable implementation model

Every INF task below inherits this contract. Each substep is a handoff boundary;
do not hand a model only a title. Supply:

1. This document §§1–5, the whole selected INF section, relevant original R/D/task
   sections, prerequisite commit SHAs and exact current evidence statuses.
2. Current Git status/index, intended editable paths, protected assets and current
   execution context. Preserve any unfamiliar user changes; ask before overwriting.
3. Scope: LOCAL fixture/code/docs versus OWNER-LIVE. No VM/service/provider action
   follows merely from reading an implementation task. Commands marked PROPOSED
   are not implemented interfaces and must never be presented to run as-is.
4. One concrete expected output from the substep, a positive and negative test,
   exact success condition, error/timeout/stop behavior and next permitted action.
5. Required output from model: files/diff summary; exact test commands/results;
   unresolved issues; decision questions; no unsupported PASS statements.

### Thinking/review labels and owner choice

- **R (routine):** deterministic edit/test/docs work with a frozen interface.
- **I (integration):** multi-file identity/lifecycle/topology work; recommend an
  integration review before commit.
- **H (higher-thinking review recommended):** security boundary, protocol design,
  statistical/performance interpretation, feature/scope change or release decision.

These labels are recommendations, not automatic model-selection instructions.
**The owner always chooses the model**, including continuing with a less-capable
model or supplying a decision directly. On uncertainty the implementer must stop
the affected work, report evidence and a specific bounded question, and ask the
owner whether to resolve it or switch models. Do not silently improvise a security
contract, auto-spawn a stronger agent or weaken acceptance. An H decision needs
recorded owner disposition, regardless of which model helped evaluate it.

### Local validation and commit rules

- Start each packet by reading actual APIs and `--help`, not by assuming proposed
  filenames/flags exist. Follow docs -> failing tests -> implementation -> regression.
- Affected Python: `ruff check <exact files>`, AST/compile outside frozen trees,
  focused unittest modules; shell: `bash -n <exact files>` and full-severity
  ShellCheck. No broad lint ignores to accommodate new code.
- At coherent checkpoints: `python3 -B -m unittest discover -s tests`, `bats tests/`,
  CI command `shellcheck lab lab-docker lab-vm scripts/*.sh`, current CI Ruff,
  Node build/package/tests if relevant, docs/link checks and `git diff --check`.
- Compare known baseline lint/docs issues separately; introduce no new failures.
  All final required checks, including8 existing docs failures, must be green for
  delivery. A fixture code commit is never host evidence or C14 approval.
- Stage explicit paths, review cached diff, exclude `vm-migration-plan.md` and
  runtime/private/evidence artifacts. Local commits are allowed after owner resumes
  implementation; push/PR/merge follow the explicit delivery checkpoint.
- Record prerequisite and completed SHA/test/evidence handoff in progress. Keep
  FAIL/BLOCKED/NOT_RUN visible. Do not copy a failed observation into PASS prose.

## 6. Dependency graph and commit checkpoints

```
INF-00 current-state handoff
  -> INF-01 topology/discovery
  -> INF-02 serving-isolation decision
  -> INF-03 protocol/resource/dependency freeze
  -> INF-04 constrained gateway
  -> INF-05 recursive relay
  -> INF-06 provider/config/lifecycle integration
  -> INF-07 benchmark driver
  -> INF-08 one-level live security + performance decision
  -> INF-09 remaining usability/features + security procedure closure
  -> INF-10 nested correctness/security/performance
  -> INF-11 frozen integrated campaign and evidence review
  -> INF-12 existing TASK-16/C14 and delivery gates
```

Independent fixture work may proceed in this order; live stages must not skip
missing prerequisites. INF-07 depends on the frozen INF-03 contract and may be
implemented before INF-06 closes, but its real results wait for INF-06. INF-09
local UI/security procedure fixes may proceed independently; no expensive recursion
before the exact candidate's usable TUI and local agent path are accepted.

| Planned checkpoint | Scope | Prerequisite for commit |
|---|---|---|
| C13c-a | INF-01 topology/discovery code + recorded INF-02/03 decisions | Read-only/identity/secret-safe fixtures; no fabricated operational topology |
| C13c-b | INF-04 minimal capability gateway | Full framing/schema/stream/cancel/resource negative fixtures and H review disposition |
| C13c-c | INF-05/06 relay and frozen profile integration | Per-hop identity/no-fallback/config/lifecycle tests; no raw backend bypass |
| C13c-d | INF-07 benchmark/evidence driver | Fake timing/stream/count/failure tests; no performance claims from fixtures |
| C13c-e | INF-09 remaining usability and executable security gaps | Focused/full regressions and truthful readiness matrix |
| C13c-f | INF-08/10/11 sanitized evidence closeout | Real required outcomes and exact teardown; no secret artifacts in Git |
| C14 | INF-12 / existing TASK-16 | Owner-reviewed required evidence, separate cutover approval, final default/rollback checks |

Do not commit empty placeholders merely to match this table. Any checkpoint split
or combining must preserve a passing build and clear evidence boundary; record the
rationale. This supplements C01–C14, not a rewrite of historical commit identity.

## 7. Executable INF task specifications

### INF-00 — establish the next implementation handoff [R; planning complete]

**Purpose:** prevent loss of ready VM or restart of obsolete ticket/recovery work.
**Inputs:** checkpointca6da6c; planning commit carrying this document; progress;
current root/profile/run identifiers in §2. **Editable paths:** documentation only.

1. Read master plan's current amendment, this document and latest progress entry.
2. Verify branch/HEAD/status and preserved mission hash, without resetting anything.
3. Copy exact protected assets and pending evidence into the task handoff. State
   explicitly that the ready profile's controller predates source fixes.
4. List only the next INF-01 substep, its execution context and allowed effects.
5. Pause until owner resumes with their chosen implementation model. No test VM,
   server, gateway or new discovery is started by this planning checkpoint.

**Deliverable:** one handoff with exact revisions and pending decisions.
**Failure:** mismatched worktree/runtime or uncertain asset -> preserve and ask.
**Exit:** owner resumes; INF-01 begins. No operational acceptance from this packet.

### INF-01 — topology-aware, read-only capability discovery [I; H for ambiguity]

**Dependencies:** INF-00 owner resume. **Likely paths:** existing
`scripts/vm-host-acceptance.py`, `lib/vm/services.sh`, `lib/vm/recursion.sh`,
`tests/test_vm_host_acceptance.py`, `tests/vm-agent.bats`, topology runbook.
Do not change `runner.sh` forwarding yet.

1. Read actual selected-profile identity/forward parsing and persisted recursion
   marker APIs. Produce a source trace of H/G0/G1 meanings without scanning private
   legacy state or guessing from the directory name.
2. Define an explicit read-only report input: exact profile/root or exact authorized
   endpoint capability, not arbitrary host/port/URL. Validate owner/private metadata
   and running process/listener before any selected-endpoint probe.
3. Report current execution level, selected provider/model, known forward identity,
   immediate-parent destination class and missing relay. Never report host keys,
   full argv/env, source file bodies, auth data, prompts or wildcard process lists.
4. Distinguish `NO_CAPABILITY`, `WRONG_CONTEXT`, `ENDPOINT_UNAVAILABLE`,
   `MODEL_NOT_LISTED`, `IDENTITY_UNKNOWN` and `AVAILABLE`. A local :11434 miss is not
   a physical-host diagnosis. Do not probe all private addresses to find models.
5. Add fixtures for nested authorized upstream versus absent loopback, malformed
   marker, wrong-profile port, stale PID/forward, unavailable and bounded responses.
   Assert forbidden/unselected endpoints receive zero connections.
6. Only after fixture review provide one owner-run read-only command. The actual
   physical-host observation may need a separate owner terminal; no remote access
   is assumed. Capture safe endpoint/version/model IDs, not credentials.

**Deliverable:** topology/capability map with every unknown explicit, plus tested
discovery. **Bounds:** small fixed endpoint set, per-probe2s default/current bounded
response policy, no completion/service start. **Stop/H question:** uncertain ancestor
ownership, unknown forwarding origin or extra network access needed. Owner chooses
how to obtain missing evidence. **Exit:** exact serving topology available for INF-02.

### INF-02 — select/verify serving isolation and resources [H recommended]

**Dependencies:** INF-01 topology. **Editable paths:** architecture/host runbook/
progress only until owner approves concrete deployment effects; reuse existing
service definitions if safe, do not create a manager framework.

1. Inventory only authorized backend version/model/device and current isolation:
   process/container/serving VM, exposed listener, permitted mounts/devices, resource
   envelope. Avoid generic environment/config dumps or touching auth stores.
2. Check §3.3 properties against observed configuration. A service bound to loopback
   but able to read the entire host home is not the desired isolation envelope.
3. Compare reuse versus a dedicated constrained container/serving VM. Record GPU
   access, model residency/batching, startup effects, memory/disk and dependencies.
4. Present one recommendation with exact proposed effects, protected existing
   services and rollback. Owner chooses the envelope and any creation/reconfiguration;
   existing service availability is not authority to stop/redeploy it.
5. Freeze selected provider/model ID, artifact/version/config identity, device/offload
   and admitted concurrency. No hard-coded default model or automatic download.
6. If an isolated serving environment cannot use the required accelerator safely,
   report the tradeoff to the owner before proceeding; do not assume GPU passthrough
   into every VM is approved or technically available.

**Deliverable:** serving-envelope decision record and owner disposition.
**Tests:** dry-run/fixture assertions for any proposed deployment recipe; no live
deployment in this task without explicit exact-scope authorization.
**Exit:** safe B location/resource contract; otherwise BLOCKED.

### INF-03 — freeze minimal API, transport, limits and dependency choice [H]

**Dependencies:** INF-02. **Likely paths:** this document's implementation-contract
appendix or companion protocol table, provider renderer tests, existing proxy
security tests as behavioral reference. No gateway implementation before exit.

1. Inspect installed backend and supported client/SDK versioned docs/help/source.
   Use synthetic request fixtures, not recording the owner's prompt/auth traffic.
2. Write exact allowed method/path/query/body/header/response/error rules for each
   supported backend. Start with selected chat streaming/model list; explicitly
   classify tool-call JSON as guest-executed data. List every rejected operation.
3. Specify backend-specific unsafe fields (URL/file paths, model mutation, tool
   execution hooks, remote fetch). If unknown-field rejection breaks required
   inference, revise a narrowly enumerated schema rather than pass arbitrary JSON.
4. Choose maintained transport implementation and event loop/library after comparing
   current dependencies, licensing, cancellation/backpressure and robust framing.
   New dependency requires owner disposition; no assumed permission for a new stack.
5. Freeze numeric header/body/depth/queue/concurrency/connect/first-byte/idle/total
   limits with rationale for the selected model/workload. Legitimate long prefill
   must fit the agreed bound; no unbounded timeout. A needed increase is recorded.
6. Define capability identity/endpoint lifecycle, binding ownership, optional private
   authentication if required, fixed upstream and no guest-supplied authorization.
7. Define the sanitized report schema/check IDs and failure taxonomy. Ensure malformed
   input, backend outage, policy denial, overload and client cancel are distinct.
8. Present the table and unresolved questions. Owner decides or chooses review model.

**Deliverable:** frozen implementation contract, chosen dependency and test vectors.
**Exit:** all required protocol semantics/limits resolved; otherwise no INF-04 coding.

### INF-04 — implement the constrained streaming gateway [I; H review]

**Dependencies:** INF-03. **Proposed files, not existing interfaces:**
`lib/vm/inference_gateway.py`, `tests/test_vm_inference_gateway.py`; use a small
adjacent module only if the reviewed contract needs it. Existing public proxy
remains separate and retains its security tests.

1. Write failing allow/deny fixtures using only synthetic loopback backend sockets.
   Verify denied requests make zero upstream connections and no leaked credentials.
2. Implement frozen upstream/model binding, strict request validation and curated
   model list. No arbitrary forwarding, CONNECT/Upgrade/redirect fallback or admin
   passthrough. Upstream credentials, if any, are capability-private, not host auth.
3. Implement incremental response forwarding, bounded backpressure and keepalive/
   pooling. Test the first chunk arrives while the synthetic backend is still open.
4. Implement guest cancel/disconnect -> upstream cancellation, idle/connect/total
   deadlines, per-capability fairness/overload and clean process shutdown. Test a
   stalled reader cannot consume unbounded memory or block unrelated clients.
5. Add malformed/ambiguous HTTP framing, duplicate headers, encoded paths, hostile
   Host, oversized/chunked/compressed bodies, schema/type/model/URL/path/tool fields,
   redirects, truncated/error stream and backend-restart tests per frozen contract.
6. Add synthetic secret/redaction tests for logs, exception paths and report output.
   Never emit raw body/header/query or full backend exception text by default.
7. Run focused tests, impacted proxy/provider suites, Ruff/compile and full milestone.
   Record library and capability protocol versions. Obtain H review disposition;
   changing schema/limits to get a test green must be reviewed as a contract change.

**Deliverable:** minimal tested gateway, not a general reverse proxy/model manager.
**Live effects:** none here. **Exit:** negative fixtures and streaming/cancel bounds
pass; code checkpoint C13c-b eligible. No throughput or host-isolation PASS yet.

### INF-05 — selected per-hop relay and identity-bound lifecycle [I; H review]

**Dependencies:** INF-04. **Likely paths:** `lib/vm/runner.sh`, `process.py`,
`services.sh`, `recursion.sh`, `lifecycle.sh`, profile policy as needed;
`tests/vm-process.bats`, `tests/vm-recursion.bats`, process/recursion Python tests.

1. Trace existing QEMU selected-forward construction and process registry. Define
   one parent-owned inference capability identity rather than raw model-port trust.
2. Root-level forward points to C only, never B's unrestricted API. Validate listener
   binding/process/config identity, not just successful TCP connect or a port number.
3. For a selected child capability, allocate one private loopback R in the immediate
   parent and bind it only to that parent's authorized upstream inference endpoint.
   Do not start/reload a model or copy model files. Child selects no arbitrary target.
4. Track relay exact PID/start/executable/config identity in existing run lifecycle;
   close inherited controller locks, bound logs and avoid new generic supervisor.
5. Do not inherit models/metrics/browser/credentials by environment. Explicit selection
   propagates only the permitted model/capability; narrower policy may be applied,
   never broader access. Public proxy chaining remains independent.
6. Failure/occupied port/unknown identity leaves unrelated listeners untouched. A
   failed relay means model BLOCKED, not raw backend/ancestor/direct-network fallback.
7. Test G0/G1/G2 effective argv/identity, absent default, unauthorized target/model,
   upstream outage/no fallback, exact cleanup, two concurrent sessions, cold/warm
   path and inherited lock behavior. Simulated tests are not real nested proof.

**Deliverable:** fixed selected relay with exact ownership and no backend bypass.
**Exit:** code checkpoint component passing; live recreation/restart still needs
owner handoff and cannot silently replace the current development profile.

### INF-06 — provider, discovery and frozen-profile integration [I]

**Dependencies:** INF-05 and protocol freeze. **Likely paths:** `config.sh`,
`services.sh`, `guest.sh`, `guest-bootstrap.sh`, `lifecycle.sh`,
`scripts/vm-opencode-config.py`, updates/cloud-init if payload changes; existing
agent/config/lifecycle/update/recursion tests.

1. Preserve provider IDs/explicit selection and model-none entry. Extend validated
   configuration minimally for capability identity/placement only after schema
   review. Reject unsupported new fields; never store secrets in config/seed.
2. Render client endpoint/model/title-model consistently to the selected gateway.
   Preserve explicit permission denies and unrelated user settings. Protocol
   compatibility must use reviewed V2 docs and actual client adapter, not guesses.
3. Add topology-aware discovery from INF-01; list safe selected models through C.
   Distinguish no capability from backend unavailability or missing model.
4. Keep launcher flags and actual model availability consistent: changing a UI
   model cannot silently grant a new QEMU endpoint. Model metadata alone cannot
   authorize a host port or bypass a gateway.
5. Define existing-profile migration policy explicitly. Never patch a frozen runtime
   in place or pretend new controller code applies to an old profile. Prefer a
   fresh disposable accepted candidate; if preserving an existing profile requires
   a managed update/graceful restart, describe owner effects and test it first.
6. Verify warm attach performs no install/model load/service repair and retains
   overlay/keys/workspace. Selected gateway/relay status is read-only on attach.
7. Update shared payload hashing/setup/update together. Test missing gateway,
   mismatched identities, stale readiness, wrong model, title-model requests,
   config scan/cancel/apply and source/frozen separation.
8. Inspect actual client/SDK retry behavior with synthetic partial streams and
   initial denial/outage. Ensure neither client integration nor gateway silently
   presents a replayed partial generation as a single successful request; record
   request counts and give actionable cancellation/error behavior. Do not globally
   disable unrelated user settings without review or hide persistent403 behind
   indefinite retries.

**Deliverable:** supported explicit agent path through C with truthful diagnostics.
**Stop:** any need for automatic host model service changes or active-controller
replacement -> owner decision, not an expedient workaround. **Exit:** C13c-c eligible.

### INF-07 — reproducible streaming benchmark/evidence driver [I; H design review]

**Dependencies:** INF-03 contract; actual measurements depend on INF-06.
**Likely paths:** extend `scripts/vm-host-acceptance.py` and its report helpers;
one small `scripts/vm-inference-benchmark.py` only if separating measurement from
campaign orchestration is demonstrably clearer. Tests use fake clocks/streams.

1. Freeze a private run manifest identifying B/C/G0 routes, source/config/backend/
   model/device identities, case IDs, counts, limits and owner-selected budgets.
2. Implement monotonic connect/first-content/last-content timing, token-count
   collection and raw sanitized sample retention. Unknown counts/timing are BLOCKED,
   not guessed tokens from bytes or a constant tokenizer conversion.
3. Implement warmups, fixed measured repetitions, paired trial order, serial then
   approved concurrency. Do not load another model or mutate B to produce a baseline.
4. Implement incremental stream verification, cancellation/outage test cases and
   no-transparent-retry accounting. Missing/truncated samples cannot be ignored.
5. Compute per-case ratios/medians, absolute throughput and TTFT budgets, queue/error
   stats and route deltas. Missing latency decision or mismatched model/device
   identity prevents aggregate PASS even if tokens/s looks favorable.
6. Reports include fixed failures, tool/backend versions, exact source/runtime/run
   identity and scalar resource observations; no prompts, outputs, tokens or URLs
   with credentials. Keep private reports outside exported snapshots.
7. Test fabricated clock/count errors, incomplete streams, mixed configurations,
   zero/negative timings, all failures, outlier visibility, budget exhaustion,
   serialization/retry/resume and final teardown dependence.

**Deliverable:** deterministic measured-evidence driver with no live claims.
**Exit:** C13c-d eligible after review; operator commands must come from actual
implemented --help, not invented command examples in this planning document.

### INF-08 — one-level live isolation and performance gate [OWNER-LIVE; H analysis]

**Dependencies:** INF-02–07; local checks green; concrete owner-run handoff.

1. Prepare a source/config-frozen scope with exact B/C/G0 identities, protected
   development/legacy assets, allowed requests/resource budget, TTFT decision
   procedure and teardown reserve. No paid usage, host auth or unbounded downloads.
2. Owner confirms exact service/deployment/model effects not already authorized;
   retain overall approved360min campaign ceiling unless owner revises it. Preliminary
   characterization time is accounted for, not reset by changing task names.
3. Preflight actual device/backend/model residency and enclosing RAM/disk/cgroup;
   revalidate listener/capability and guest boundaries before inference.
4. Characterize direct baseline from authorized serving-control context and freeze
   the owner TTFT/concurrency budget before scored G0 measurements. If baseline is
   unstable, diagnose; do not compare incomparable trials.
5. Prove one-level negative API/network tests including arbitrary guest HTTP client,
   no raw backend reachability and no host files/ports/socket. Synthetic canaries
   stay private; denied operations do not reach B.
6. Execute §4 measurement routes/cases and selected-model real agent test. A plain
   completion is not proof of guest-local tool placement: write/read a harmless
   guest-only sentinel and verify serving-control machine did not execute the tool.
7. Report performance/security separately. Owner reviews required >=95% ratios,
   TTFT, error/cancel behavior, absolute speed and resource use. No security waiver.
8. On failure stop dependent stages, preserve sanitized evidence and propose one
   bounded diagnosed optimization. If architecture choice changes or budget is
   insufficient, ask owner; do not install per-VM models as a hidden fallback.

**Deliverable:** real one-level evidence and owner disposition. **Exit:** accepted
safe/fast path, or explicit BLOCKED and optimization/VM-local decision. No C14 yet.

### INF-09 — close existing usability and executable security gaps [I; human pauses]

**Dependencies:** INF-06 for local work; INF-08 for model-dependent live observations.
**Likely paths:** existing services/metrics/auth/update/acceptance/canary/isolation
code and tests; no new model manager or auth campaign.

1. Make a checklist from current blockers: metrics status/forward/UI, old browser
   helper ownership/cleanup, frozen controller mismatch, actual ask/auto/deny,
   public egress403 diagnostics, filesystem/QCOW2 inspection, synthetic credential
   lifecycle and any missing agent/workload/concurrency procedures.
2. For each, distinguish source implementation, fixture evidence, owner observation
   and required live evidence. Assign a concrete existing task/gate and next command
   only after reading implementation; no whole-checklist attestations.
3. Metrics: report actual enclosing level; verify exact service identity/curated
   endpoint. Never adopt/kill the unverified listener. Decide any child-local
   metrics capability explicitly rather than inheriting ancestor-host metrics.
4. Browser: inspect only exact run/session helper metadata and verified identities,
   preserve uncertain helpers, no broad pkill. Validate source cleanup on a fresh
   controlled runtime. Optional manual browser auth remains available, while live
   OpenAI paid/refresh/export acceptance stays deferred and not a C14 blocker.
5. Egress: identify denied hostname/policy layer with fixed redacted diagnostics.
   Public/provider policy changes are explicit; no allow-all/private route bypass.
   Local model success must not require cloud authentication or paid calls.
6. Security procedures: complete real out-of-scope filesystem checks and a safe
   QCOW2 filesystem inspection method, or leave required gate BLOCKED. Do not mount
   arbitrary images on host or install privileged inspection tools implicitly.
7. Synthetic auth/private-state checks use generated fake data, atomic import/export,
   failed-export deletion refusal and exact retention. Never inspect/copy real auth.
8. Owner observes TUI/metrics/dialog rendering, actual ask/auto/deny and cancellation
   on the exact candidate. Explicit denies must survive auto. Human absence/skip
   remains BLOCKED for required observations. Pause before expensive recursion.

**Deliverable:** actual procedures plus local tests and truthful owner observations.
**Exit:** C13c-e code checkpoint and prerequisites for INF-10/11; not blanket PASS.

### INF-10 — real nested relay/isolation and self-modification proof [OWNER-LIVE; H]

**Dependencies:** INF-08 ordinary path accepted; exact-run HUMAN-TUI/local agent
usable; INF-09 required procedures ready. Reuse existing TASK-13 live adapter.

1. Freeze source candidate, per-level budgets and explicit model capability; default
   children have no model/metrics/browser/credentials unless selected. Do not copy
   weights or model servers. Confirm actual G0->G1->G2 ancestry, not siblings.
2. Check CPU/KVM/RAM/disk at each enclosing level, including failed retained VMs.
   Confirm cap2 and G3 refusal before allocation/snapshot/QEMU.
3. Repeat changed-bootstrap AND changed-launcher failure42 -> fresh fixed candidate
   proof. Collect source/runtime/seed/effective argv and guest sentinel evidence;
   unchanged parent code or mere source hashes is not the required proof.
4. Exercise model stream and harmless actual agent tool task through selected R0/R1.
   Verify tool runs only in the intended guest; parent stays usable. Test unselected
   capability and parent relay outage cause refusal without direct fallback.
5. Repeat negative API/network/filesystem/canary checks at child levels. Child root
   cannot turn an R into a general parent proxy/port picker or model admin path.
6. Measure/report nested connection/TTFT/throughput separately under frozen bounds.
   Honor any owner-set nested performance gate without sacrificing one-level speed.
7. Deepest-first exact teardown retains reports and failed-run evidence, stops only
   verified run-owned processes/relays, and checks sockets/listeners/resources. An
   uncertain child prevents unsafe parent shutdown; never delete the working parent.

**Deliverable:** real E2E-SELF/DEPTH and nested inference/security evidence, not unit
inference. **Exit:** required results accepted or BLOCKED; no automatic rerun/merge.

### INF-11 — final frozen integrated campaign and acceptance review [I/H; OWNER-LIVE]

**Dependencies:** code from INF-01–10; review all changes/evidence and fresh preflight.

1. Freeze final candidate/source/config/runtime and selected-provider identities.
   Earlier development VM fixes/observations are not automatically campaign results.
2. Re-run only required affected earlier evidence on final candidate, using explicit
   compatibility mapping; changed runtime security/transport behavior invalidates
   affected prior PASSes. Do not assume hashes alone preserve behavioral evidence.
3. Execute required local-provider streams, real guest task fail->fix->pass,
   guest-only PostgreSQL workload/query/retention, warm/A->B->B, suspend/resume,
   selected actual config/tools update, resize and exact scoped destructive tests.
4. Execute two authorized guest-local tasks and required boundary/secret/private-state
   checks; include inference isolation/performance and nested proof from exact
   accepted source. No host Docker socket or project-service publication.
5. Keep owner UI/permission observations and deferred live OpenAI subchecks explicit.
   No paid work or credential export is added by this plan. Required synthetic
   credential safety remains mandatory.
6. Preserve>=35min cleanup reserve within the agreed360min ceiling and diagnosed
   retry policy. Time already consumed is not free; stop for owner if insufficient.
7. Complete exact manifest-owned teardown, old-helper disposition, residue checks,
   protected parent/workspace/base/auth preservation, and final sanitized report.
   Any uncertain cleanup -> BLOCKED, not a final green report.
8. Present gate-by-gate PASS/FAIL/BLOCKED/DEFERRED with exact evidence and limitations.
   Owner reviews whether required local-first acceptance actually passes.

**Deliverable:** C13c-f sanitized evidence closeout. **Exit:** owner accepts required
results and considers separate C14 approval, or lists blockers. No silent cutover.

### INF-12 — C14 default, docs/CI, delivery and eventual merge [H owner decision]

**Dependencies:** INF-11 required gates and exact cleanup accepted; explicit owner
instruction to begin cutover. This remains original TASK-16, not a new release route.

1. Reconcile README/SECURITY/architecture/setup/testing/decisions/command index with
   actual native VM behavior, inference boundary, optional backends, data retention,
   support limits and evidence. Remove misleading old agent-container/rootless/
   zero-access claims; label historical descriptions instead of treating them as
   the current release architecture. Close all8 existing docs-consistency failures.
2. Preserve Docker optional fallback/argv/permissions/metrics/security and existing
   working session. Stage/review default-route change; do not recreate Docker.
3. Run full Python/Bats/static/compile/schema/docs tests and MCP Node build/package/
   tests. Preserve engine-only ticket rules; any project-store validation must use
   approved engine/read-only procedure when the owner lifts the current ticket pause.
4. Owner approves actual VM-default activation on accepted scope; apply only the
   reviewed route/docs/CI change, then test default VM and explicit Docker rollback.
   Changed runtime behavior needs affected revalidation, not inherited acceptance.
5. Inspect exact Git diff/status, dependency/license/security reports, generated
   artifacts and secret-canary results. Keep mission/runtime/evidence secrets out
   of commit and snapshot. No sweeping npm audit fix or unrelated dependency update.
6. Prepare a replacement PR/evidence summary as authorized by owner, with current
   base/head, required gates, deferred evidence and rollback. Push/publication are
   separate from local commits; obtain explicit instruction at this delivery pause.
7. Check GitHub Actions on the exact proposed head: ShellCheck all launchers,
   Python/Ruff, Node/Bats/ticket-store and any added fast gateway tests. Do not
   claim green remote CI from local commands or waive failures. Resolve merge
   conflicts without overwriting user work; retest affected behavior afterward.
8. Present final diff/check/evidence to owner. Only on explicit final merge
   instruction, with required conditions met, merge the intended PR/revision.
   No force push, branch deletion, auto-merge or protected-state cleanup implied.
9. After merge, verify resulting main revision/checks and provide rollback/support
   notes. Do not delete legacy/valuable VM state as celebratory cleanup.

**Deliverable:** accepted VM-default release with working Docker fallback and honest
evidence. **Stop:** any required failed/unperformed check, uncertain cleanup,
unapproved effect or owner pause. The goal to merge never overrides these gates.
**Exit:** owner-authorized cutover/default/rollback evidence and exact-head CI
accepted; publication/final merge only on their explicit owner checkpoints, then
report the actual merged revision and remaining optional/deferred support work.

## 8. Decision register for implementers

| ID | Decision | Current status | Who resolves / thinking recommendation |
|---|---|---|---|
| D-INF-01 | Shared accelerator-backed inference default candidate; no recursive model copies | Owner approved | Implement only after resume; do not re-ask architectural preference |
| D-INF-02 | Inference-only capability, no raw backend administrative API | Owner approved | INF-03 freezes exact operations; H review recommended |
| D-INF-03 | >=95% ordinary one-level warmed generation ratio | Owner approved | INF-07 implements; INF-08 evidence review |
| D-INF-04 | Exact TTFT/concurrency/case/resource budgets | Pending measured baseline and owner selection | INF-08; H interpretation recommended, owner decides |
| D-INF-05 | Actual H/G0/B topology and current authorized model path | Unknown operational evidence | INF-01 read-only report; no speculative scans |
| D-INF-06 | Isolation envelope/deployment and accelerator access | Pending evidence and concrete effects | INF-02 owner choice; H recommended |
| D-INF-07 | Exact backend protocol/schema/transport dependency/limits | Pending version review | INF-03 owner disposition; H recommended |
| D-INF-08 | Existing ready-profile capability update vs fresh accepted candidate | Pending lifecycle review | INF-06; preserve current overlay/frozen runtime |
| D-INF-09 | VM-local alternative implementation priority | Deferred unless owner brings forward | §9 decision, not automatic performance fallback |
| D-INF-10 | C14 activation / publication / final merge | Not authorized in this phase | INF-12 separate explicit owner checkpoints |

If a decision is absent, create a short question record in progress: observed
evidence; exact uncertainty; two bounded options with effects; recommendation;
which dependent substep stops. Ask owner whether to decide or use another model.
Do not turn every routine deterministic test into a new architecture question.

## 9. Defined future option: VM-local inference with cached artifact push

**Not default and not currently required before C14.** Bring forward only on an
explicit owner choice after performance evidence, or a request for inference with
no external model capability. GPU passthrough is not silently approved by this
option; CPU/native acceleration/hardware support must be established separately.

### Conditional subplan (do not implement until brought forward)

F1. Discover selected backend/model/license/device support and actual RAM/VRAM/disk
requirements in the intended VM level; compare with shared inference on identical
model/settings. No promise of GPU parity or nested accelerator availability.

F2. Design an owner-controlled immutable content-addressed artifact cache outside
all exports, with trusted download source/digest/license, atomic staging, verified
publication, size/free-space bounds and no shared mutable replacement. Explicit
download consent/time/disk limits. Cached weights do not mean loaded VRAM is shared.

F3. Parent-push only selected verified artifacts into a VM-owned disk/data volume,
with a manifest and guest digest verification. Never export the host cache,
directory/credentials/socket or accept child-selected host paths/URLs. Existing
selected source snapshot exclusion of model caches/weights remains mandatory.

F4. Install/start backend **inside that VM**, behind guest-local loopback, without
host device/socket inheritance. Agent tools remain guest-local. Preserve weights
and service state across suspend/resume; warm attach never downloads or reloads
models implicitly. Separate model lifecycle from ordinary agent launch.

F5. For nested use require explicit per-child selection/copy/resource approval;
default no weights/backend duplication. Enclosing allocations must accommodate
loaded models and failed retained children, not just disk byte copies.

F6. Test atomic failed transfer/no partial use, wrong checksum/license/oversize,
concurrent cache publication, escaping links/special files, bounded source-only
push, persistent data, exact child cleanup and shared-cache preservation.

F7. Benchmark cold cache/copy/load separately from warm inference. Compare same
model/context/device capability; a CPU/GPU mismatch is not transport evidence.
Owner chooses whether the deployment is suitable; no universal fastest claim.

F8. Add explicit placement selection only after schema/UX review. Existing profiles
cannot silently switch placement or duplicate models. Keep shared capability mode
available; supported combinations/evidence must be documented honestly.

This option solves repeated downloading and optional external-inference avoidance.
It does not solve accelerator access, RAM/VRAM duplication or stronger isolation
against hypervisor vulnerabilities by itself.

## 10. Failure playbook and pause now

| Observation | Required response | Forbidden shortcut |
|---|---|---|
| Parent loopback no models | Resolve executing level/authorized capability in INF-01 | Start model containers or scan private networks |
| Proxy CONNECT403 | Identify exact policy layer/hostname safely | Blank allowlist, direct route or host-network mode |
| Guest forward reaches raw backend | Block capability acceptance; enforce C | Call a port restriction inference-only |
| Gateway drops/buffers streams | Reproduce synthetic split/slow/cancel case | Disable validation/bounds or retry partial output |
| Throughput <95% | Preserve comparable samples, classify and propose bounded optimization | Change model/device/output or lower gate silently |
| Need GPU passthrough/per-VM models | Present conditional §9/hardware decision | Add device privileges/copy models automatically |
| Old frozen runtime lacks fix | Preserve it; explicit lifecycle/candidate plan | Patch runtime files or destroy ready overlay |
| Existing listener/helper owner unknown | Report BLOCKED and exact-identity inspection | Adopt listener or broad process kill |
| Required procedure missing | Implement within approved task or report BLOCKED | Ask owner to attest a whole checklist PASS |
| Context/model too weak for uncertainty | Give owner bounded evidence/question/recommended review | Auto-switch/spawn agents or guess |

**Current stop:** CI/current implementation checkpoint is committed; this planning
amendment is to be committed separately, then report recommendations and pause.
No INF-01 implementation or live discovery starts until owner resumes. The next
recommended implementation task is **INF-01.1–01.5 local topology/discovery work**,
followed by its reviewed owner-run read-only report. Do not rerun old recovery or
provider commands merely because they appear earlier in the historical handoff.
