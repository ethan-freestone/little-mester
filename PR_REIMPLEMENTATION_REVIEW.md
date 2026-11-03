# Original VM PR: evidence, adversarial review and dispositions

This companion is part of `PR_REIMPLEMENTATION_PLAN.md`, not a second design.
All original citations refer to PR #13 head
`97af1d83a16fce68ddcc3390276c218a1b0cd518`; main baseline is
`efe77202594c79dc1a926fa84a7a5dbc8866c66a`. Review is source/history-driven;
not a security certification or a claim every line was exhaustively audited.

## 1. Investigation and actual evolution

The 155-commit PR was grouped by effective capabilities, not its commit sequence.
`git diff --stat/--numstat main...feat/vm-approach`, ordered history, focused
historical diffs, current source/functions/test names, decisions and ticket
criteria were examined. Live PR has no review submissions, inline comments or
issue comments to incorporate at the checked baseline. There is no PR body
specification to privilege over source/owner requirements.

| Historical evidence | What changed / current interpretation |
|---|---|
| `8b2b182`, `7b54d26`, `a31e9af` | Standalone VM began with guest container-image build/rootless setup; boot/device fixes were essential, initial container topology was not final requirement |
| `04e76a4`, `671db3d`, `9baa4dd` inspected diff | Permission experiments included generated config edits/sed restore. `9baa4dd` explicitly removes that rejected approach for client `--auto`. Never resurrect sed/live blanket-permission changes |
| `45465de`, `8e9b433`, `838d44a`, `402826d` | Rootless Docker persistence/sub-ID/registry/proxy work fixed real guest workload problems; equivalent workload behavior matters, exact rootless stack no longer host boundary |
| `473d92d`, `6a70df7`, `dc5e896`, `2d390d1` | Image-era npm/platform/CPU diagnostics. Actual CPU features matter to native binary; extra agent-Docker install diagnostics are no longer needed on native guest path |
| `d7888b6` inspected code diff | Removes `_vm_build_agent_image` and BuildKit network probes, adds `_vm_install_guest_harness` and direct `vm_cmd_agent`. Building an agent image is abandoned as VM runtime prerequisite, not a capability to preserve |
| `fc15f9a`, `32bb8ea` inspected diff, `1117b74` | Fixed ports1455/1457 were replaced by exact runtime OAuth redirect port and unified agent entry. Dynamic callback and narrow bridge are justified; fixed port fallback is superseded |
| `8b970ef`, `491d0f6` current source/tests | IPv4-embedded IPv6 normalization and public-destination access with host-route blocking are necessary defenses, not optional complexity |
| `c27e987`, `6f9b76c` | Explicit guest update was added; apt candidate awk must consume stream to avoid SIGPIPE under pipefail. Keep behavior and regression, not separate conflicting payload hashes |
| `35ee0f6`, `6bc2482`, `4431779` | Bounded snapshots/recursion, child proxy chaining and nested image downloads through parent. Native VM self-testing is a real capability beyond ordinary subagents |
| `4925e55`, `dc1fb7a`, `9e4a16b` | Managed resize, depth-scaled RAM and setup-only memory override are owner-directed functionality, not experimental knobs to drop |
| `d46ab14`, `4839a41`, `f88029c` | Socket shortening fixes nested AF_UNIX limits; active-source overlays repair stable snapshot drift; bounded acceptance probes prevent unending SSH reads. Keep short sockets/deadlines and evidence, replace active-source repair with fresh immutable candidate snapshots |
| `7c1f555`, `44d11c6`, `7c37028`, `cc959c7` | Interrupted-run inspection was added, then corrected to avoid confusing active browser/TUI SSH with abandoned test clients. Keep bounded evidence/ownership guarantees; do not carry operator PID advice into product cleanup |
| `454d7ec`, `8a8e26b`, `a0a8809` | Campaign, non-destructive suspend/resume, private state and safe child suspension added late; this is real accepted intent, even though full host evidence remains incomplete |
| `41d69ab4`, `ceb7a3a` inspected diff, `97af1d8` | Current-source recursive proof and exact outer selection corrections. Latest ticket says interrupted attempt reused wrong live outer and package bootstrap status unknown. Exact-profile fix is code, not a passing retry |

No explicit `Revert ...` commit was found in the inspected filtered history;
semantic removals and superseding implementations above were identified from
diffs/decisions. Do not equate absence of a revert-named commit with absence of
abandoned work.

## 2. Findings

### F01 — Test orchestration became an environment-repair subsystem

- Requirements: R04,R21,R24,R28,R30. **REPLACE**, risk high, confidence10.
- Evidence: `scripts/vm-host-acceptance.py:39–64,290–308,598–831,938–1024`;
  hardcoded `DEFAULT_SNAPSHOT`, source-file allowlist, sync/rebuild/backups,
  active-source overlay payload, profile discovery. `ceb7a3a` changes discovery
  behavior after the latest #41 owner interruption.
- Problem/impact: acceptance may repair source, materialize selected-file overlays,
  install guest prerequisites, or choose a live profile. Which code/VM was tested
  requires reconstructing runner policy and source parity, complicating retries
  and risking the working environment. This is not merely a 4,219-line complaint.
- Alternative: fresh whole-tree revision-bound candidate + exact disposable run;
  separate read-only preflight, explicit setup and bounded verification.
- Tradeoff/counterargument: existing continuation/repair preserves expensive warm
  guests and valuable evidence. Do not remove it from original branch or delete
  old profiles. Replacement pays fresh-guest cost for unambiguous acceptance; keep
  diagnostic/evidence/timeout semantics, not repair orchestration.

### F02 — Shared-base rebuild has cross-profile destructive scope

- Requirements: R08,R16,R18. **REPLACE**, risk critical, confidence10.
- Evidence: `lib/vm/lifecycle.sh:1977–2034`: common `$VM_DIR_BASE/base.qcow2`,
  checks/stops only named vm_dir then deletes shared base and named overlay;
  `disk.sh:95–96` creates overlays referring to common base.
- Problem/impact: other profiles' backing chains can reference replaced/deleted
  base even if only one profile was selected. A printed warning is not dependency
  enforcement. Also lacks the down-path escrow call before target reset.
- Alternative: checksum-keyed immutable base cache, profile-only reset, no base
  GC/migration in this release, auth/descendant guard shared by all delete paths.
- Tradeoff: retains old bases/disk space; explicit later GC needs dependency audit.
  Disk savings cannot justify corrupting unrelated overlays.

### F03 — Low-level PID state is insufficient authority to signal

- Requirements: R04,R18,R28. **REPLACE**, risk critical, confidence10.
- Evidence: `runner.sh:607–672` trusts `qemu.pid`, `virtiofsd.pid`, `proxy.pid`
  and sends TERM/KILL; `_vm_status:680–693` treats `kill -0` as running. Some
  higher-level paths inspect argv, but the cleanup primitive itself does not.
- Problem/impact: stale/reused or tampered PID may point at an unrelated process;
  partial-start rollback can bypass higher-level verification.
- Alternative: centralized identity check PID+start ticks+executable+exact assets,
  profile UUID/locks; unknown means refuse, owned startup cleanup only.
- Tradeoff: procfs inspection and crash handling add complexity. This is justified
  safety work, not a removable abstraction or guest-root-proof mechanism.

### F04 — Model selection and host service management are coupled

- Requirements: R01,R11,R25. **SIMPLIFY**, risk high, confidence10.
- Evidence: `lifecycle.sh:1659–1687` only accepts local llamacpp/ollama, calls
  `vm_cmd_models ensure`; `host-services.sh:377–452` ensure→up→Compose startup/
  managed recovery. Contradicts C.9.15 optional model/host Docker independence.
- Problem/impact: cloud/model-none agent entry can be rejected; normal launch may
  change host services. Browser-only bypass is not model-agnostic configuration.
- Alternative: selected provider adapter and exact optional API capability;
  diagnostics read-only; host model lifecycle user-managed separately.
- Tradeoff: less automatic recovery. Approval explicitly required for this
  convenience behavior change; local no-subscription workflow remains supported.

### F05 — Warm reuse still transfers/configures guest runtime

- Requirements: R01,R16,R27. **SIMPLIFY**, risk medium, confidence10.
- Evidence: `lifecycle.sh:558–566` calls recursion installer/harness installer on
  same-workspace up; harness helper `:315–342` transfers plugins/config/MCP source
  before hash check; update `scripts/vm-update.sh:63–67` hashes different inputs.
- Problem/impact: setup/workspace reuse/update have competing notions of current
  payload. Source changes can mutate running guest when a user intended attach.
- Alternative: one relative-path payload hash, setup/update explicit; warm path
  bounded readiness only, no installation/transfer.
- Tradeoff: explicit update required to adopt source/plugin changes in an existing
  guest. Self-test fresh candidate is the correct path for VM changes.

### F06 — Shutdown safety is not uniform across transitions

- Requirements: R16–R19. **SIMPLIFY**, risk high, confidence9.
- Evidence: suspend invokes `_vm_require_no_live_nested_qemu` at `:1783–1849`;
  workspace-switch helper `:674–697` directly powers off without that check, and
  resize calls that generic helper. `stop:1951` runs workspace Compose down -v.
- Problem/impact: one non-destructive command guards children while another can
  stop their containing guest; generic VM stop assumes ownership of user Compose.
- Alternative: one descendant-safe graceful transition helper used by all poweroff
  operations; project workload teardown remains explicit project action.
- Tradeoff: busy/unknown guests refuse more often; actionable diagnostics and
  explicit recursive poweroff preserve usability without forced deletion.

### F07 — OAuth version and channel lifetime need explicit qualification

- Requirements: R14,R15,R27. **KEEP + SIMPLIFY**, risk high, confidence9.
- Evidence: architecture describes source-inspected 2.2.0; installer `:249,295–308`
  accepts latest; #52 records 2.2.5. Bridge `:187–197` retains callback until changed;
  architecture says tunnel closes with SSH master/TUI, not necessarily login end.
- Problem/impact: new upstream URL/listener behavior can break auth; a successful
  login need not immediately close long-lived session callback capability.
- Alternative: retain exact URL/port/token tests and MCP adapter; resolve/record
  version, compatibility gate, per-attempt lease/release and bounded expiry.
- Tradeoff: source-dependent browser integration is still necessary; don't replace
  it with device code or claim arbitrary latest supported. Unavailable native
  lifetime signal is an explicit compatibility blocker to investigate/test.

### F08 — Rootless guest complexity is no longer an outer boundary requirement

- Requirements: R10,R17. **SIMPLIFY**, risk medium, confidence9.
- Evidence: cloud-init sub-ID/netfilter/user services at `cloud-init.sh:6–43,109–182`,
  lifecycle rootless diagnostics/repair `:380–445`; C.8 explicitly grants guest admin
  and allows rootful Docker. #42 A3/A4 prioritizes ordinary Compose functionality.
- Problem/impact: repeated rootless setup/repair, lingering, module and UID mapping
  code adds failure modes to a guest the agent already administers.
- Alternative: rootful guest Docker or explicit none, proxy preserved.
- Tradeoff: loses guest-local defense in depth/rootless behavior; it does NOT grant
  host Docker or weaken outer VM by itself. Explicit owner approval, no host-Docker
  security changes, full Compose/negative boundary tests required.

### F09 — Module/global ownership is fragmented

- Requirements: R28,R30. **SIMPLIFY**, risk medium, confidence10.
- Evidence: `lab-vm:151–158` sources component modules then lifecycle re-sources
  them `:27–32`; lifecycle has fallback workspace guard `:487–513`; state-root
  resolution/log definitions repeated in scripts. Tactical decisions record
  caller variable clobbering C.9.4.
- Problem/impact: initialization and security guard behavior depend on entry path,
  separate fallback functions diverge, global flags become implicit interfaces.
- Alternative: one CLI parse/context, one guard helper, cohesive module owners;
  no duplicated sourcing or fallback implementations.
- Tradeoff: some explicit context argument plumbing is longer. Avoid converting
  this into a generic VM object/plugin framework merely for stylistic purity.

### F10 — Depth caps cannot constrain a malicious guest administrator

- Requirements: R19,R22,R25. **CLARIFY**, risk high if overclaimed, confidence10.
- Evidence: cloud-init grants `NOPASSWD:ALL`; persisted `/etc/lab-vm-recursion` is
  root-owned, and `recursion.sh:6–31` checks it against environment.
- Problem/impact: this reliably protects supported launcher workflows against
  accidental env reset, not a root process editing identity/running raw QEMU.
- Alternative: state cooperative policy accurately; outer VM allocation/network/
  filesystem remains adversarial boundary. Test normal-policy refusals and actual
  independent outer containment. No new privileged quota broker assumed.
- Tradeoff: not a stronger root-proof nesting guarantee. If owner wants that, new
  architecture/authority approval is required rather than a false claim.

### F11 — Reuse security primitives and behavioral tests, not incidental assertions

- Requirements: R03–R09,R15,R19,R22–R24,R30. **KEEP**, risk high if discarded,
  confidence10.
- Evidence: proxy 28 tests, browser13, memory7, cloud-init10; snapshot implementation
  detects copy races and excludes history; runner's short AF_UNIX socket and
  single-key complete scan address real bugs.
- Problem/impact: aggressive rewrite could remove defenses or replace tests with
  broad smoke/grep-only argv assertions. Smaller code is not proof of simplicity.
- Alternative: carry behavioral cases/negative fixtures forward; strengthen actual
  effects and provenance, use existing implementations when already simplest.
- Tradeoff: retained proxy/browser code may remain similar length. That's correct
  where security responsibilities justify it.

### F12 — Scope and evidence drift inflates apparent delivery

- Requirements: R29–R34. **DEFER + CLARIFY**, risk medium, confidence10.
- Evidence: changed benchmark/model alias; tickets #49/#51/#53–57; 2,604 added audit
  lines; campaign explicitly lists many `CAMPAIGN_NOT_CLAIMED` criteria.
- Problem/impact: importing full branch/history makes optional concepts and partial
  owner observations look like implemented/accepted release capabilities.
- Alternative: independent VM commits; no benchmark alias/nonnormative ticket data
  transplant; separate human, unit, host, recursive proof and release readiness.
- Tradeoff: original evidence must remain linked and discoverable, not discarded;
  new ticket work through MCP as execution proceeds. No audit fabrication.

## 3. Full effective changed-path disposition

This table covers all 100 changed paths. KEEP means preserve justified behavior/
tests, not blindly copy old source. REMOVE means exclude from independent
replacement, **not delete from original branch**. DEFER leaves separate ideas out
of scope. Replacement implementation is from main, so most REMOVE entries need
no deletion operation. Each category maps to tasks; historical ticket writes are
never copied/edited as files.

| Original changed path | Disposition / replacement responsibility |
|---|---|
| `.github/workflows/ci.yml` | SIMPLIFY; deterministic fast vs explicit live tests, TASK-16 |
| `.gitignore` | KEEP private runtime/generated exclusion intent, TASK-16 |
| `.mester-egress` | KEEP public-only policy contract, TASK-04/16; preserve explicit workspace deny |
| `CLAUDE.md` | SIMPLIFY invariant ticket instructions, TASK-16; no execution chronology |
| `CODING.md` | SIMPLIFY doc→test→code/engine rule, TASK-16 |
| `INSTRUCTIONS.md` | REPLACE stale environment/handoff prose with stable rules, TASK-16; no edits during Round1 |
| `README.md` | REPLACE concise actual workflow/support/status, TASK-16 |
| `SECURITY.md` | REPLACE explicit two modes/rootful/containment claims, TASK-16 |
| `agent/Dockerfile` | REMOVE VM-era diagnostic installation changes; retain main Docker behavior |
| `agent/diagnose-opencode-install.cjs` | REMOVE image-build experiment; native guest failure diagnostics TASK-06 |
| `agent/metrics/gather.py` | KEEP bounded concurrent collector/endpoints, TASK-09 |
| `agent/opencode/opencode.json` | SIMPLIFY VM renderer preserves denies, TASK-06/08; no blanket permission deletion |
| `agent/opencode/plugins/host-metrics-tui/tui.tsx` | KEEP transport error/retry parity, TASK-09 |
| `agent/opencode/plugins/host-metrics/index.ts` | KEEP configured endpoint behavior, TASK-09 |
| `agent/opencode/plugins/little-mester-exit-confirm/tui.tsx` | KEEP useful legibility only if actual renderer tested, TASK-09 |
| `bench/ai-analyse.sh` | REMOVE unrelated fixed model alias change |
| `compose.vm-model-api.yaml` | DEFER optional host model deployment recipe, no automatic core VM dependency; TASK-16 documents external API |
| `compose.yaml` | REMOVE unrelated alias change; reuse main hardened Docker configuration |
| `docs/01-host-setup.md` | REPLACE tested-profile/setup/guest instructions, TASK-06/16 |
| `docs/DECISIONS.md` | SIMPLIFY current durable decisions not repeated test transcripts, TASK-16 |
| `docs/chatgpt-auth.md` | SIMPLIFY native versioned/private lifecycle runbook, TASK-10 |
| `docs/opencode.md` | KEEP relevant V2 distinction; refresh installed version truth, TASK-16 |
| `docs/testing.md` | REPLACE fast/live/self-VM acceptance separation, TASK-16 |
| `docs/ticketing.md` | KEEP engine-only invariant if clarification needed, TASK-16; no ticket-engine feature work |
| `docs/tickets/README.md` | KEEP stable engine-only convention if needed, TASK-16 |
| `docs/tickets/_audit.json` | REMOVE transplant; original immutable evidence linked, future writes MCP only |
| `docs/tickets/_index.json` | REMOVE transplant; engine owns derived data |
| `docs/tickets/architecture/DECISIONS.md` | SIMPLIFY boundary rules if required via approved documentation work, not JSON writes, TASK-16 |
| `docs/tickets/ticket-006.json` | REMOVE transplant, docs objective evidence only |
| `docs/tickets/ticket-035.json` | KEEP evidence/requirements reference, no transplant |
| `docs/tickets/ticket-040.json` | KEEP architectural evidence reference, no transplant |
| `docs/tickets/ticket-041.json` | KEEP current restrictions/evidence reference, no transplant |
| `docs/tickets/ticket-042.json` | KEEP native runtime/workload/update requirements, no transplant |
| `docs/tickets/ticket-043.json` | KEEP boundaries/recursive/workload requirements, no transplant |
| `docs/tickets/ticket-044.json` | KEEP documentation truth requirements, no transplant |
| `docs/tickets/ticket-045.json` | KEEP auth/provider/credential requirements, no transplant |
| `docs/tickets/ticket-048.json` | KEEP logging/yes behavior requirements, no transplant |
| `docs/tickets/ticket-049.json` | DEFER independent engine persistence bug; no transplant |
| `docs/tickets/ticket-050.json` | KEEP permission behavior requirements, no transplant |
| `docs/tickets/ticket-051.json` | DEFER ticket test-plan schema; no transplant |
| `docs/tickets/ticket-052.json` | KEEP default/lifecycle/self-modification requirements, no transplant |
| `docs/tickets/ticket-053.json` | DEFER docs MCP module |
| `docs/tickets/ticket-054.json` | DEFER provider usage counter |
| `docs/tickets/ticket-055.json` | DEFER structured sharing research; KEEP no upward-request invariant |
| `docs/tickets/ticket-056.json` | KEEP session distinction; DEFER supervisor/merge ownership engine |
| `docs/tickets/ticket-057.json` | DEFER readiness/relationship schema |
| `docs/vm-architecture.md` | REPLACE concrete approved contracts, TASK-16 |
| `docs/vm-host-acceptance.md` | REPLACE scoped suites and evidence, TASK-15 |
| `lab` | REPLACE minimal staged/final dispatcher after gate, TASK-16 |
| `lab-docker` | KEEP main Docker route/semantics with necessary permission fixes, TASK-08/16 |
| `lab-vm` | REPLACE one parser, explicit context/delegation, TASK-01–16 |
| `lib/state.sh` | KEEP canonical private root intent, TASK-02 |
| `lib/vm/browser-bridge-server.py` | KEEP protocol/validation; SIMPLIFY lifetime ownership, TASK-10 |
| `lib/vm/browser-open.py` | KEEP narrow guest client, TASK-10 |
| `lib/vm/browser-open.sh` | KEEP guest opener adapter, TASK-10 |
| `lib/vm/cloud-init.sh` | REPLACE single optional workload/bootstrap source, TASK-06 |
| `lib/vm/disk.sh` | REPLACE immutable cache/backing safety, TASK-03 |
| `lib/vm/host-services.sh` | SIMPLIFY into read-only models/optional metrics services, TASK-08/09 |
| `lib/vm/lifecycle.sh` | REPLACE focused state transitions/delegated auth/bootstrap, TASK-07 |
| `lib/vm/prereqs.sh` | SIMPLIFY read-only capability discovery/explicit setup, TASK-01/06 |
| `lib/vm/proxy.py` | KEEP tested address/route/chaining core, TASK-04 |
| `lib/vm/recursion.sh` | SIMPLIFY explicit policy, candidate source/state, TASK-12 |
| `lib/vm/runner.sh` | REPLACE ownership/argv/deadlines; KEEP valid device/short socket behaviors, TASK-05 |
| `lib/vm/ssh.sh` | SIMPLIFY central isolated argv/host-key state, TASK-05 |
| `opencode.json` | REPLACE portable guest config without fixed `/workspace/...` host executable assumption, TASK-06/16 |
| `pyproject.toml` | KEEP necessary specific lint policy only, TASK-16; no broad exemption |
| `scripts/README.md` | KEEP/refresh durable command index, TASK-16 |
| `scripts/check-vm-cloud-init-schema.sh` | KEEP schema gate, TASK-06 |
| `scripts/regen-configs` | REMOVE blanket permission deletion; preserve explicit config/deny semantics, TASK-06/08 |
| `scripts/vm-context.py` | SIMPLIFY into canonical context helper, TASK-01/02/12 |
| `scripts/vm-extra.sh` | REMOVE explicitly temporary repair layer; read-only doctor/model diagnostics TASK-08 |
| `scripts/vm-guest-isolation-check.py` | KEEP representative bounded negatives, TASK-15 |
| `scripts/vm-host-acceptance.py` | REPLACE fresh scoped tests, no historical environment repair, TASK-15 |
| `scripts/vm-host-egress-check.py` | SIMPLIFY merge meaningful checks into boundary suite, TASK-15 |
| `scripts/vm-install-tools.sh` | KEEP guest-only explicit refresh behavior, TASK-14 |
| `scripts/vm-models-diagnose.sh` | SIMPLIFY read-only provider/transport doctor, TASK-08 |
| `scripts/vm-opencode-config.py` | KEEP preserve/unrelated/mcp validation intent; native V2 generated VM config, TASK-06 |
| `scripts/vm-opencode-tui-check.py` | KEEP actual TTY/argv detection, TASK-14 |
| `scripts/vm-python-tools.sh` | KEEP private venv/no unrelated replacement, TASK-06/14 |
| `scripts/vm-recursion-live-children.py` | KEEP unknown child fail-closed; replace source CLI search with frozen controls, TASK-07/12 |
| `scripts/vm-recursion-workspace.py` | KEEP filtered/race/manifest semantics; strengthen unchanged snapshot verification, TASK-11 |
| `scripts/vm-update.sh` | SIMPLIFY shared payload/hash/confirmation, TASK-14 |
| `scripts/vm_memory_budget.py` | KEEP host/cgroup/private RSS semantics; reservation safety, TASK-07 |
| `tests/exit-confirm.bats` | KEEP behavioral/static guards, TASK-09/16 |
| `tests/lab-permissions.bats` | KEEP mode/argv/deny regressions, TASK-08/16 |
| `tests/lab.bats` | KEEP main regressions; replace incidental VM routing assertions, TASK-16 |
| `tests/test_vm_browser_bridge.py` | KEEP13 behavioral families, extend expiry, TASK-10 |
| `tests/test_vm_cloud_init.py` | KEEP schema/guest/proxy/identity cases; remove rootless-specific internals after approved choice, TASK-06 |
| `tests/test_vm_context.py` | KEEP metadata/env/private-root cases, TASK-02/12 |
| `tests/test_vm_guest_isolation_check.py` | KEEP harmless bounded negative probes, TASK-15 |
| `tests/test_vm_host_acceptance.py` | KEEP ownership/deadline/report/scope negatives; REMOVE repair/overlay incidental expectations, TASK-13/15 |
| `tests/test_vm_install_tools.py` | KEEP guest-only/no stopped auto-start, TASK-14 |
| `tests/test_vm_memory_budget.py` | KEEP7 resource cases, TASK-07 |
| `tests/test_vm_metrics_service.py` | KEEP7 concurrency/bounds/interface cases, TASK-09 |
| `tests/test_vm_opencode_config.py` | KEEP4 preserve/validate/output behaviors; no new deprecated config, TASK-06 |
| `tests/test_vm_opencode_tui_check.py` | KEEP4 process identity/TTY cases, TASK-14 |
| `tests/test_vm_proxy.py` | KEEP28 security/protocol behaviors, TASK-04 |
| `tests/test_vm_recursion_live_children.py` | KEEP9 no-child/unknown/matched/child-failure/retention families, TASK-07 |
| `tests/test_vm_update.py` | KEEP13 update/guard/failure behaviors, TASK-14 |
| `tests/vm.bats` | SIMPLIFY into focused CLI/disk/process/lifecycle/agent/recursion suites; retain meaningful policy/failure cases, TASK-01–14 |

## 4. Test coverage preservation obligations

The plan does not claim coverage improvement from test count. Original tests are
a source of scenarios, not assertions that old internals must remain:

- Proxy28 and bridge13: carry all behavior families, strengthen bounds/lifetime.
- Memory7: retain MemAvailable, cgroups, reserve, private RSS, missing data behavior;
  add concurrency reservations. Snapshot behavior currently embedded in Bats
  receives actual file/Git/copy-race fixtures in its own focused suite.
- Cloud-init10: retain valid schema/embedded shell, UID/GID/KVM identity and package
  proxy; rootless sub-ID/module internals legitimately replaced only after approval.
- Metrics7, update13, tools2, TUI4, context4, config4, live-children9, guest-isolation2:
  preserve behavior independent of process/file organization.
- Host-acceptance70: retain sanitized report scope, timeouts/interruptions, exact
  profile/ownership, no-secret observations, state preservation and human-blocking
  checks; replace tests requiring snapshot repair/file allowlist overlays or stable
  profile discovery with tests proving they cannot occur.
- Main MCP/CLI/engine/Jest/build/package and existing Bash/Docker/TUI/license/docs
  checks remain. New VM code cannot justify dropping unrelated baseline tests.
- Actual changed-VM fail→fix→new-VM pass, G2 and G3 cap refusal, workloads, repeated
  streaming, secret refresh and human terminal/browser checks are ADDITIONAL
  obligations, not inferred from any original smoke or unit result.

## 5. OpenCode documentation consulted

Skill loaded; V2 docs fetched directly:

- https://opencode.ai/v2/docs/cli — native invocation, `run` automation,
  shared service versus `--standalone`.
- https://opencode.ai/v2/docs/config — system/global/project config and native
  `mcp.servers`/`providers`/`permissions`, separate CLI settings.
- https://opencode.ai/v2/docs/permissions — rule order, explicit denies, separate
  subagent permissions and shell authority; not a VM isolation mechanism.

Do not turn current web docs into a false pinned-version OAuth contract. The
specific installed version/CLI flags/export/import/browser source must be checked
in an isolated disposable guest during TASK-06/08/10. No active OpenCode service
restart/config mutation was performed for this investigation. No deprecated V1
fields recommended for new VM config; input legacy conversion can preserve
existing user behavior without forcing a global migration.

## 6. Adversarial self-review of the replacement

| Challenge | Resolution in plan | Remaining risk |
|---|---|---|
| Could more agents replace nested VMs? | No. R21/E2E-SELF must run changed launcher/seed/proxy/lifecycle in fresh G1/G2 | Hardware prerequisite, BLOCKED if absent |
| Does freezing source merely test old code? | Candidate whole-tree hash binds launcher and guest payload; changed bootstrap sentinel AND lifecycle option required | Effective QEMU/guest assertions must verify behavior, not hashes alone |
| Could running changed launcher in G0 damage G0? | Yes: agent already has G0 admin. Private candidate state/known-good control reduce accidental disruption; H boundary repeated | Not sandboxed against malicious guest root; honest trust model |
| Does a host user rerunning agent-edited `./lab-vm` remain safe? | No blanket promise. Use frozen trusted host control path for existing profile; modified source only runs in candidate guest until reviewed | Host execution of modified source always requires review |
| Are immutable base/private metadata gratuitous abstractions? | No: directly remove demonstrated shared-base/PID/ownership hazards | Additional validation must stay stdlib/simple, no framework |
| Is the new runner another repair framework? | Explicitly no discovery/reconciliation/active overlay, only unique run manifests/public CLI | Reject future convenience repairs unless separately approved |
| Does rootful Docker weaken a promised boundary? | Guest admin already granted, no new host crossing; change approved explicitly | Less guest-local defense; owner may reject design choice |
| Are credentials/model relay inherited silently to make child agent test pass? | No: explicit selected capability/provider, narrow immediate-parent relay/SSH push | Full child-agent gate blocked without approved provider, never shell-smoke pass |
| Can root defeat depth limit? | Yes, documented supported-launcher safeguard, outer VM resources bound host | Root-proof enforcement would need different approved architecture |
| Is latest OpenCode reproducible/auth-safe automatically? | Resolve once, record version, compatibility gate; version bump not assumed | Upstream undocumented OAuth may block acceptance |
| Are all shutdowns descendant-safe? | Shared helper for switch/resize/suspend/reset; unknown QEMU blocks | Live process disappearance races need negative tests |
| Does --yes authorize all destructive actions? | No: exact run scope plus separate descendant-discard/auth-export conditions | Document carefully; no host package installs from normal launch |
| Is success possible without real VM/agent/security evidence? | No, aggregate gates distinguish BLOCKED/NOT_RUN and human observations | Owner availability/host state can delay delivery |
| Are tasks independently executable without redesign? | Named files/interfaces, exact dependencies, cases/expected effects, commands/provisional discovery, commit and recovery for each | Pin-specific compatibility discovery is bounded and escalation explicit |
| Is there an unowned requirement/task/commit? | Traceability table R01–R31; R32/R33 exclusions, R34 host condition; 16 tasks/14 checkpoints | Mechanical validation and coordinator review required before implementation |

Round 1 conclusion: a simpler replacement is justified by ownership, provenance,
setup/runtime separation and scope—not simply fewer lines. Retain existing proxy,
browser and snapshot defenses where already the simplest robust implementation.
No implementation or live owner-host acceptance was performed in Round 1.
