# Little Mester VM reimplementation: architecture and execution contract

**Approved by the owner on 2026-11-03. Round 2 in progress on `feat/vm-approach-2`.**

**Current controlling amendment — owner approved 2026-11-06:**
[Pre-C14 inference isolation/performance execution plan](docs/vm-inference-performance-plan.md).
Prioritize the persistent one-level VM; shared accelerator-backed inference via
an operation-constrained capability is the default candidate, not raw backend TCP
access or recursive model copies. Require >=95% comparable direct-baseline warmed
generation throughput plus owner-frozen latency/reliability/security gates.
VM-local cached artifact push is a defined optional alternative, not automatically
implemented before C14. The companion specifies INF-00–12, detailed substeps,
tests/evidence/stop conditions, owner decisions and review/model-choice rules.
It supersedes conflicting older model-discovery/relay/acceptance instructions.
Current phase: commit CI/existing work, detailed planning, then **pause** for the
owner to choose an implementation model. No new inference implementation, live
operations, push, merge or C14 activation during this phase. Checkpoint `ca6da6c`.

**2026-11-05 clarification:** this replacement plan controls execution. Original
branch tickets provide requirements, failure history and observations, not an
additional approval workflow. The TASK-15 campaign policy in §9 supersedes legacy
handoff ordering and retry restrictions for new replacement-owned test assets.

## 1. Mission, scope and decisive requirement

Replace PR [#13](https://github.com/ethan-freestone/little-mester/pull/13) independently
on `feat/vm-approach-2` from `main`. Preserve intended behavior and justified security
properties, not the original file structure or experimental history.

**Little Mester must modify LITTLE MESTER itself, including its VM implementation,
then provision and test a different VM using those changes, collect results, fix
failures, and repeat without sacrificing its working parent environment.** Opening
another agent, checking `/dev/kvm`, mocking QEMU, booting an unchanged image, or
testing only application code does not meet this requirement. This is R20–R24 and
the release-blocking E2E-SELF test below. Ordinary additional agents may share a VM;
recursive VM implementation testing actually requires nested virtualization.

The original package specified 16 implementation tasks and 14 commit checkpoints;
later C13a/C13b and the approved INF-00–12/C13c amendment supplement that baseline.
All paths named as proposed are design choices, not files already implemented.

### Package index

- This document: authority, requirements, architecture, traceability, decisions,
  commits, tests, risks, execution and completion contract.
- `PR_REIMPLEMENTATION_TASKS.md`: mandatory, dependency-ordered TASK-01–16 specs.
- `PR_REIMPLEMENTATION_REVIEW.md`: source/history evidence, findings, full changed
  path disposition, retained test obligations and adversarial self-review.
- `PR_REIMPLEMENTATION_PROGRESS.md`: actual observations and execution ledger.
- `PR_REIMPLEMENTATION_BASELINE.md`: historical kickoff; not a competing plan.
- [Pre-C14 inference/performance amendment](docs/vm-inference-performance-plan.md):
  approved2026-11-06 R35–R38/D11–D13, INF-00–12 and conditional VM-local F1–F8;
  controlling next-step/owner-model/pause and measurable acceptance contract.

No application code or original-branch work is authorized during Round 1. Planning
artifacts are in `/workspace` on the replacement branch, outside the detached
original-source worktree. Do not overwrite the user's staged/unstaged mission file.

## 2. Exact source and evidence baseline

Live API rechecked during Round 1 completion, 2026-11-03:

| Item | Revision/value |
|---|---|
| Repository / origin | `ethan-freestone/little-mester`; `git@github.com:ethan-freestone/little-mester.git` |
| PR | #13, `feat: vm approach`, open draft, empty body, 155 commits |
| PR head / original local branch | `97af1d83a16fce68ddcc3390276c218a1b0cd518` |
| PR base / live main / local main | `efe77202594c79dc1a926fa84a7a5dbc8866c66a` |
| Replacement HEAD / merge base | `efe77202594c79dc1a926fa84a7a5dbc8866c66a` |
| Effective diff | 100 files, +29,219 / −910 |
| Reviews / inline comments / issue comments | All three live REST collections empty; no pagination required |
| Replacement checkout | `/workspace`, `feat/vm-approach-2` |
| Original inspection worktree | `/tmp/vm-migration-source`, detached at PR head |
| Pre-existing user modification | `AM vm-migration-plan.md` — preserve both index and working version |

REST endpoints are recorded in the historical baseline. `gh` is unavailable;
public API access succeeded, including `commits/main`. No stale collation was used
as authority. Source citations in this package refer to the exact PR head unless
explicitly marked main. Ticket evidence was read through MCP with
`ticket_dir=/tmp/vm-migration-source/docs/tickets`, not the older replacement store.
#35, #40–45, #48–50, #52–57 were inspected. Historical criteria are interpreted
using superseding decisions, not treated as simultaneous contradictory contracts.

### Main architecture to reuse

Main uses a Bash `lab` launcher and libraries/scripts, Python stdlib diagnostics,
OpenCode plugins, a curated Python metrics service, and a TypeScript MCP package.
The MCP package has Node >=20, a lockfile, build/package/Jest commands. Existing
Bats, ShellCheck, Python compile/Ruff, docs-consistency and ticket-validation CI
must survive. No Python framework, daemon manager, libvirt, database, queue,
generic plugin system, or new package-management layer is justified for VM control.

## 3. Requirement inventory

Classification: REQUIRED, SECURITY, OPERATIONAL, QUALITY, OPTIONAL, EXPERIMENTAL,
UNRESOLVED. `E` = explicit; `I` = inferred supporting responsibility. P0 blocks
release, P1 is required product parity, P2 is optional/deferred. Confidence measures
the interpretation, not whether implementation has passed. Verification IDs in
§9 and tasks are normative. Each row includes observable acceptance and constraints.

| ID / class / priority / source type / confidence | Requirement, evidence, observable acceptance and constraints | Verification |
|---|---|---|
| R01 REQUIRED P0 E 10 | One command starts/reuses isolated guest and enters native agent at selected workspace; no agent container. Mission §1; #35 A2/A16/A17, #42 A1, #52 A1. Warm launch performs no install/download. | V-LAUNCH, E2E-DAILY |
| R02 OPERATIONAL P0 E 10 | Linux x86_64 QEMU/KVM/virtiofs/cloud-init support profile; no TCG fallback; capability absence fails before setup with actionable diagnostics. #40, #41 A11/A15, C.8/C.9.15. Explicit setup lists distro-mapped userland packages, defaults to no installation and asks before host changes. CPU-only host works; GPU not required. | V-CONFIG, V-PROCESS, HOST-CLEAN |
| R03 SECURITY P0 E 10 | Host validates canonical owned workspace; refuse root/home, credential-path overlap, private state and unauthorized checkout exposure before export. #41 A3, #52 A4. Unsafe recursion is not permission to mount host home. | V-PATH, SEC-FS |
| R04 SECURITY P0 E 10 | Private per-user state outside all shares, 0700 directories/0600 keys; no checkout fallback or automatic migration of old profiles. C.9.23–25. Resolve exact requested profile, never substitute another. | V-PATH, V-LIFE, SEC-FS |
| R05 SECURITY P0 E 10 | QEMU `restrict=on`; SSH loopback only; exact selected model/metrics/proxy/auth channels; no host Docker socket/general route/LAN listener/firewall/NAT/sysctl changes. #41 A4/A18, #43 A3/A4. | V-PROCESS, SEC-NET |
| R06 SECURITY P0 E 10 | HTTP 80/CONNECT 443 proxy enforces deny/allowlist/public default; reject private/reserved/multicast, mixed DNS, embedded IPv4 bypass and local/direct networks; use validated IP. Fail closed on introspection failure. #43 A4, C.9.13. | V-PROXY, SEC-NET |
| R07 SECURITY P0 E 10 | Nested proxy applies child hostname policy then chains through immediate parent public-only proxy, no direct fallback; parent route/IP policy remains final authority. #43, commit 6bc2482. Child/parent denials propagate. | V-PROXY, SEC-NET, E2E-SELF |
| R08 OPERATIONAL P0 E 10 | Cached checksum-pinned bootable base, per-profile overlay and seed; atomic download, stable backing chain, failed checksum installs nothing. #41 A1/A18/A21, disk.sh. A profile reset never replaces a shared base. | V-IMAGE, HOST-CLEAN |
| R09 OPERATIONAL P0 E 10 | Bounded bootstrap readiness requires SSH, clean cloud-init status (including no recoverable errors), workspace mount and actual harness readiness. Preserve original stage/status on failure. #41 A21/A22. | V-GUEST, V-PROCESS, HOST-CLEAN |
| R10 REQUIRED P0 E 10 | Guest Docker/Compose supports PostgreSQL, guest-local ports/binds/volumes and repeated test feedback. Rootful/rootless is guest implementation choice, not host boundary. #42 A2–4, #43 A1/A2. Optional runtime `none` still supports shell/agent. | V-GUEST, E2E-WORK |
| R11 REQUIRED P0 E 10 | Model-agnostic selection: no-subscription local path, cloud API-key/native OAuth paths, and no configured model path usable for setup/shell/auth. #45 A1–7, C.9.15. No implicit host model/container changes. | V-AGENT, E2E-MODEL |
| R12 REQUIRED P1 E 9 | Preserve metrics tool/UI and exit-confirm plugin, report missing metrics without blocking agent; terminal dialogs legible. Host-Docker metrics stays independent. #42 A6/A9–11; #41 A25. | V-METRICS, HUMAN-TUI |
| R13 REQUIRED P0 E 10 | `--permissions auto`/`--auto-accept` map once to supported OpenCode auto mode; ask omits it; conflicts/unsupported modes refuse. `--yes` controls launcher confirmation only. Explicit denies remain effective in real tool execution. #50; C.9.13. | V-AGENT, HUMAN-PERM |
| R14 SECURITY P0 E 10 | Native guest ChatGPT browser flow: unchanged validated authorize URL, runtime-selected single loopback callback forward, no manual second terminal, no token capture. Reject unknown URL/version shapes, port collision and unauthorized callers. #45 A12–17. | V-AUTH, HUMAN-AUTH |
| R15 SECURITY P0 E 10 | Guest auth persists across suspend/resume; optional runtime forwarding off/auto/provider selectors, limited supported credential capabilities, no broad DB copy. Escrow atomic private import/export; failed export blocks deletion. #45 A3/A8–11/A18. No secrets in image/seed/source/log. | V-AUTH, SEC-SECRET, HUMAN-AUTH |
| R16 OPERATIONAL P0 E 10 | Idempotent up/status/list/SSH/attach; exact same profile A→B→B mounts only B after graceful switch and preserves disk/tools/identity. Status/context/doctor never install/repair. #41 A2/A12/A13, #52 A8. | V-LIFE, E2E-DAILY |
| R17 OPERATIONAL P0 E 10 | Non-destructive suspend/resume preserves overlay/auth/workload volumes. Live or unrecognized nested QEMU blocks parent shutdown; explicit recursive suspend works deepest-first and leaves parent on failure. #41 A6/A24; C.9.7. | V-LIFE, E2E-LIFE |
| R18 SECURITY P0 E 10 | Scoped destructive down/stop/rebuild explicitly acknowledged, secrets exported if enabled, sticky descendant-data guard, no implicit workspace/base/evidence deletion. #43 A6, #52 A3. `--yes` is not descendant discard authority. | V-LIFE, SEC-SECRET, E2E-LIFE |
| R19 OPERATIONAL P0 E 10 | Persist 8/4/2/1 GiB depth defaults, setup override, managed graceful resize; preflight MemAvailable/cgroup plus 512 MiB before state/snapshot/start/stop. Never resize ancestor or silently overcommit. C.9.20–21. | V-RESOURCE, E2E-LIFE |
| R20 REQUIRED P0 E 10 | Parent agent can deliberately edit Little Mester checkout, including VM source, under explicit self-write opt-in while host control state stays private. User clarification; #41 A9, #52 A5/A7. | V-SNAPSHOT, E2E-SELF |
| R21 REQUIRED P0 E 10 | Run CHANGED launcher, QEMU construction, proxy, seed/bootstrap, guest harness/config from revision-bound candidate, provisioning fresh nested VM to test VM changes. User clarification; #52 A7; 41d69ab4. Unchanged parent launcher/old overlay does not count. | V-RECURSE, V-SELF, E2E-SELF |
| R22 SECURITY P0 E 10 | Recursive VM opt-in, default maximum 0, range 0–32; child depth increments, max cannot rise, persisted guest identity survives login. Missing KVM/flags or depth refusal occurs before snapshot/profile/QEMU. #43 A8, #52 A6. Limits are supported-launcher policy, not guest-root-proof isolation. | V-RECURSE, E2E-DEPTH |
| R23 SECURITY P0 E 10 | Filtered parent-selected snapshots include intended tracked/dirty/untracked source, no historical Git objects, keys/auth/DB/state/model caches; no escaping symlinks/special files. Manifest source SHA/tree/modes; 2 GiB default/16 GiB max, disk reserve; changed source needs fresh run. C.9.22. | V-SNAPSHOT, SEC-SECRET, E2E-SELF |
| R24 REQUIRED P0 E 10 | Automated bounded fail→diagnose→fix→fresh candidate pass; structured evidence, deadlines, cancellation, explicit cleanup and parent continuity. No stale-profile discovery, active source patching or recurring prompts within preapproved scope. Mission §§1/9, user clarification. | V-SELF, E2E-SELF |
| R25 SECURITY P0 E 10 | Child gets no implicit ancestor keys/workspace/model/metrics/browser endpoints; explicitly selected narrow model/credential relay only if approved. Parent-pushed source has no child request/control channel. #45 A18, #43 A8, #55 A1/A5. | V-RECURSE, SEC-NET, SEC-SECRET |
| R26 REQUIRED P1 E 9 | Additional native agent sessions perform authorized tasks in same guest or distinct profiles/worktrees without requiring nested VM; independent auth sessions, explicit same-workspace conflict warning. #56 A1/A2/A5; mission §1. No claim of full supervisor. | V-AGENT, E2E-AGENTS |
| R27 OPERATIONAL P1 E 10 | Explicit in-place update scan/confirmation for harness/config/tools and selected OS/Docker; preserve overlay/auth/workspace; warm launch does not update; pin supported, resolve latest once. #42 A7/A12–14. No update of active interactive session. | V-UPDATE, E2E-LIFE |
| R28 QUALITY P0 E 10 | Stage-specific bounded secret-safe diagnostics, -v/-d, rotation five routine logs but failed acceptance evidence retained; machine-readable context/results, original exit cause, unknown identity never signaled. #48; C.9.14; interrupted #41 history. | V-CONFIG, V-PROCESS, V-SELF |
| R29 OPERATIONAL P1 E 10 | Keep verified host-Docker fallback as `lab-docker`, no host socket/DinD, preserve existing runtime/auth; VM default only with explicit release acceptance gate. #35/#52 current ledger. | V-DISPATCH, E2E-DAILY |
| R30 QUALITY P0 E 10 | Meaningful fast, integration, negative-security, recursive and failure tests; no mock→host inference; docs label actual support/evidence. Mission §§9–10; CI. All required behaviors have traceability. | All V-/E2E-/SEC-/HUMAN- groups |
| R31 QUALITY P1 E 10 | Bash/Fish documented commands, one decision log, correct README/SECURITY/setup/testing, engine-only ticket writes; Node/MCP private lockfile build. #44; C.9.18–19/24. | V-DOCS, V-GUEST, HOST-CLEAN |
| R32 OPTIONAL P2 E 10 | Remote/cloud VM hosts, BSD/macOS adapters, alternate runtimes, GPU passthrough excluded from this release; no weakened fallback advertised. C.8/C.9.15, #46/#47. | V-CONFIG refusal; docs exclusion |
| R33 EXPERIMENTAL P2 E 10 | #53 docs MCP, #54 usage counters, #55 structured sharing/MCP research, #57 readiness schema and #51 ticket test-plan field are separate work. Do not import ticket-store churn or implement upward sharing. | V-DOCS scope check |
| R34 OPERATIONAL P0 E 9 | Preserve original/valuable sessions, profiles, credentials and exports. Legacy interrupted apt/report state is relevant only if that asset is deliberately reused; it is not a prerequisite to a fresh isolated replacement campaign. Approve the replacement run scope once, preflight actual resources and collisions, and never substitute an existing profile/export. | HOST-PREFLIGHT scope/ownership/evidence |
| R35 SECURITY P0 E 10 | External local-model access grants only a reviewed inference capability, not raw model administration, server-side tools, host files/URLs, unrelated services or general ancestor routing. Backend itself has a verified isolation envelope; arbitrary guest HTTP clients/root cannot bypass the gateway. Owner amendment2026-11-06. | INF-02–06, INF-08/10; SEC-NET, SEC-FS, SEC-SECRET |
| R36 QUALITY P0 E 10 | Prioritize persistent one-level VM performance. Same-model/device/settings benchmark requires >=95% comparable direct-baseline warmed generation throughput and explicitly frozen TTFT/concurrency/reliability/resource criteria. No security relaxation, cherry-picked samples or changing model/device to pass. Owner amendment2026-11-06. | INF-07/08; E2E-MODEL-PERF |
| R37 OPERATIONAL P0 E 10 | Topology-aware discovery and explicitly selected immediate-parent inference relays work without recursive weights/server deployment. Absent parent loopback is not proof of physical-host absence. Missing capability fails closed with no scan/service-start/direct fallback; tools execute only in the selected guest. | INF-01/05/06/10; V-RECURSE, E2E-SELF |
| R38 OPTIONAL P2 E 10 | Define VM-local inference as alternative placement with owner-controlled verified artifact cache and parent-pushed VM-owned copies, persistence and explicit per-level resources. Implementation deferred unless owner brings it forward; no cache export, implicit downloads/copies or GPU passthrough authority. | Companion §9; conditional future scope, not current C14 gate |

## 4. Architecture decisions and alternatives

### Alternatives evaluated

| Alternative | Complexity/dependencies/config | Security/reliability/testability | Decision |
|---|---|---|---|
| Trim original in place | Least immediate code movement, retains dual setup paths and historical repair runner | Existing tests valuable; source/profile discovery ambiguity and shared state remain costly | Reject as replacement strategy; selectively reuse proven primitives/tests |
| Bash orchestration + Python policy helpers, direct QEMU/KVM | Matches main; no new language/runtime dependencies; explicit commands/config | Small ownership state machine; deterministic argv/policy tests; real nested tests mandatory | **Recommend** |
| Rewrite manager entirely in Python | Better structured subprocess/state APIs but another CLI/config convention and broad rewrite | Viable, but not demonstrably simpler than existing Bash + policy helpers at this scope | Do not choose without new evidence |
| libvirt/Vagrant/Terraform/daemon supervisor | Adds services/providers/state/config and host privileges | Mature lifecycle but different host trust/setup footprint; nested portability still needs proof | Reject for now |
| Agents/containers only inside one VM | Simplest parallel coding | Cannot exercise changed VM provisioning/lifecycle in a new VM; fails R21 | Reject as substitute; retain for ordinary sessions |
| Ask physical host to test every candidate | No nested KVM needed | Breaks self-contained iterative VM development and repetitive-intervention objective; broad host control risky | Manual fallback on unsupported hardware only, not acceptance of recursive support |

### Normative decisions

- **D01 — small native-host adapter.** Bash `lab-vm` owns argument parsing once;
  six responsibility modules own config, images, processes/SSH, guest setup,
  lifecycle and auth. Python stdlib helpers implement paths/snapshots, proxy,
  recursion inventory/resource checks and evidence. No generic host-adapter
  framework until a second tested platform exists. Reuse main MCP and plugins.
- **D02 — explicit owned state.** Schema-versioned profile metadata plus one
  runtime identity file; lock per state root for allocation and per profile for
  mutation. No implicit profile discovery, override-from-workspace shell config,
  automatic legacy migration, or shared mutable base replacement. Private runtime
  payload per profile freezes the launcher/control helpers used for that profile.
- **D03 — repeated restricted boundary.** Retain QEMU user networking, virtiofs
  and public-only proxy with child chaining. Generate forward list from selected
  capabilities; do not always map both model APIs/metrics. No direct-origin nested
  fallback and no host network repair.
- **D04 — guest setup is one operation, warm launch another.** One cloud-init
  template + guest bootstrap script; one managed guest-runtime payload and content
  hash. Guest Docker is **rootful inside the VM by default** to avoid redundant
  rootless/sub-ID/netfilter repair machinery; this changes no host boundary and
  requires explicit approval. Workload runtime `none` supported. Preserve main
  Python tools and Node/MCP capabilities; validation tools included when recursion
  is enabled.
- **D05 — independent provider and harness adapters.** Native OpenCode initial
  supported VM harness; keep existing Aider Docker fallback. VM Aider/other
  harness selection uses a fixed supported command adapter, not arbitrary host
  command templates. VM-native secondary harness support is a parity extension
  of TASK-08, with Aider smoke required before claiming it supported. Local model
  APIs are selected external endpoints, never auto-repaired host services. A
  missing model may warn but cannot prevent shell/auth/configuration entry.
- **D06 — retain proven narrow auth transport.** Keep the tested MCP browser
  capability/client, not a new hand-written protocol. Centralize validation and
  SSH construction; lifetime-bound per-attempt callback lease, explicit release,
  plus bounded expiry for abrupt loss. Escrow adapters use supported export/import,
  not raw DB copies. Forwarding off by default; `auto` imports only implemented
  supported providers and reports unsupported selectors, never silently drops them.
- **D07 — candidate VM test, not agent nesting.** Every attempt has a fresh run
  ID, source snapshot, candidate runtime, child profile, overlay and manifest.
  Execute changed VM launcher in parent G0 against a candidate snapshot to create
  G1; G1 installs/tests the same source. Depth-2 proves recursion and depth-3
  refusal at cap2. Known-good parent control payload remains available. No repairs
  of active exports or per-file overlay allowlists.
- **D08 — safe daily lifecycle.** `suspend`/`resume` retain disks; `down` and
  compatibility `stop` remain explicitly destructive, with printed effects and
  separate acknowledgement. `rebuild` resets ONLY named profile, not shared base;
  `base` populates checksum-keyed immutable cache. Graceful shutdown timeout
  preserves state; no automatic force-kill as a daily recovery policy.
- **D09 — scoped tests and evidence.** Separate fast checks, live VM tests,
  recursive self-test, and human OAuth/TUI observations. Test runner receives exact
  names/run IDs, never discovers or repairs the developer environment. FAIL,
  BLOCKED, NOT_RUN and PASS are distinct. Report original stage/cause, not raw
   guest output or arbitrary host commands.
  One reviewed campaign scope covers its ordinary stages, bounded retries and
  run-owned cleanup; do not demand a new ticket authorization for each command.
  Stop the affected stage on failure, preserve diagnostics and continue only after
  diagnosis/revalidation. Widening scope or touching protected state needs owner
  approval; fixing a candidate inside the approved scope does not.
- **D10 — compatibility and delivery discipline.** Preserve main behavior in
  `lab-docker`; expose VM mode explicitly until replacement acceptance, then
  VM-default dispatcher. Original ticket/audit history stays on its original
  branch; any new execution-ticket records go through MCP, not copied files.
- **D11 — inference capability, not model-server authority (2026-11-06).**
  Shared serving retains accelerator efficiency but must enforce exact allowed
  inference/model-list operations and safe backend isolation, selected upstream/
  models, bounded streaming/cancellation and no administrative/host-tool capability.
  Existing raw socat model forwarding is not sufficient acceptance. Optional
  relays repeat only the selected immediate-parent capability; no model copies.
- **D12 — benchmark before changing placement (2026-11-06).** Optimize one-level
  persistent VM first against identical warmed model/device/settings. Require
  >=95% baseline generation ratio, owner-frozen first-token/concurrency/resource
  budgets, reliability and negative isolation evidence. Missing measurements are
  BLOCKED, not an assertion that tunneling is slow or per-VM inference is faster.
- **D13 — optional VM-local artifact push (2026-11-06).** Preserve a defined
  alternative using a verified owner-side cache and explicit parent push into
  VM-owned storage; no host-cache mounts or automatic nested deployment. Bring
  implementation forward only by owner decision. GPU passthrough remains excluded
  without a separate hardware/authority decision.

## 5. Concrete interfaces and data/control flow

### Files and responsibilities (all VM paths proposed on replacement)

| Component | Files / responsibility |
|---|---|
| Entry/routing | `lab-vm`, `lab`, `lab-docker`; parse, select exact mode/profile, preserve agent args after `--` |
| Configuration/state | `lib/state.sh`, `lib/vm/config.sh`, `lib/vm/paths.py`; validated values, private paths, JSON metadata and locks |
| Image | `lib/vm/disk.sh`; checksum cache, overlay/seed, no deleting another profile's backing image |
| Process/control | `lib/vm/runner.sh`, `lib/vm/ssh.sh`; pure argv builders, owned process identities, sockets, isolated SSH, deadlines |
| Network | `lib/vm/proxy.py`; retain tested public/IP/route/chaining core, bounded listener/policy reload |
| Guest | `lib/vm/guest.sh`, `lib/vm/cloud-init.sh`, `lib/vm/guest-bootstrap.sh`, `scripts/vm-opencode-config.py`; setup/payload readiness/config |
| Lifecycle | `lib/vm/lifecycle.sh`; profile transitions only, delegates auth/guest/process operations |
| Auth | `lib/vm/auth.sh`, existing-named `browser-bridge-server.py`, `browser-open.py`, `browser-open.sh`; per-session transport/escrow |
| Resources/recursion | `lib/vm/recursion.sh`, `scripts/vm_memory_budget.py`, `scripts/vm-recursion-workspace.py`, `scripts/vm-recursion-live-children.py` |
| Optional services | `lib/vm/services.sh`; model diagnostics and immediate-host curated metrics using existing `agent/metrics/gather.py` |
| Verification | `scripts/vm-self-test.py`, `scripts/vm-host-acceptance.py`, `scripts/vm-guest-isolation-check.py`; separate deterministic tests under `tests/` |
| Updates | `scripts/vm-update.sh`, `scripts/vm-install-tools.sh`, `scripts/vm-python-tools.sh`; same guest payload/hash as setup |

Do not create another module for each function. The split above isolates security
policy from orchestration and guest setup from warm runtime; it is not a target
line-count or mandatory one-file-per-responsibility framework.

### Public CLI contract

`lab-vm <workspace>` = `lab-vm run <workspace> --name default`.
`setup [--install-prereqs]` checks the supported host and explains missing userland
packages; installation is opt-in, default-no confirmation (or explicit setup
`--yes`), never an implicit launch/diagnostic action. It must not install/start
libvirt or alter host daemon/network policy. Initial distro maps cover Debian/
Ubuntu and Arch/CachyOS only after package-name validation; other Linux distros
may use a manual prerequisite path and must pass the same capability gates.
`up [profile] --workspace <path>` starts/provisions but does not enter agent.
`agent [profile] [-- <agent-args>]`, `ssh [profile] [-- <command...>]`,
`status [profile] [--json]`, `list [--json]`, `context [--json]`, `doctor [profile]`
are explicit operations. `suspend [profile] [--poweroff-recursive]`, `resume`,
`resize <profile> <GiB>`, `down`, `stop`, `rebuild`, `base`, `update`,
`install-tools`, `egress-reload`, `models status|diagnose`, `metrics up|status|down`,
`auth-forward`, and `recursion-clean` retain meaningful capabilities. Temporary
`extra repair-model-api` and automatic `models ensure/up/down` are not core VM
operations; model lifecycle stays with the selected external provider/runbook.

Global `-v/-d`, `-y/--yes`, `--auto-accept`, `--permissions ask|auto`,
`--browser-auth|--no-browser-auth`, `--recurse|--unsafe-recurse`, `--max-depth`,
`--memory`, `--name` have one parser; conflicting duplicates fail before state.
`--` ends launcher parsing. Only fresh-profile setup through `run`/`up` accepts
the memory override (not the host-prerequisite `setup` command); only explicit
`--discard-recursive` permits deleting possible descendant data. Host package
installation is a separate approved setup action, never a diagnostic side effect.

### Configuration (no new YAML dependency)

Use optional host-private JSON `$MESTER_STATE_DIR/config.json`, parsed without
shell evaluation; CLI overrides allowed nonsecret values. Schema v1 fields:
`host_profile=linux-x86_64`, `workload_runtime=docker|none`,
`opencode_version=latest|semver`, `agent=opencode|aider`,
`model={provider:none|ollama|llamacpp|remote, port?:1..65535, id?:string}`,
`metrics=on|off`, `auth_forward=off|auto|[provider IDs]`,
`auth_escrow_dir=<absolute private path>`. Defaults: Docker guest workload,
OpenCode/latest-resolved-once, model none, metrics on for top-level agent,
auth forwarding off, depth0/max0. Local provider requires explicit loopback port;
remote uses normal mediated public egress. No token/key values in this config.

Selected environment compatibility inputs are MESTER_STATE_DIR, XDG_DATA_HOME,
VM_OPENCODE_VERSION, VM_YES/LAB_YES, VM_VERBOSE/VM_DEBUG and explicit recursion
identity. VM_DIR_BASE/VM_STATE_DIR remain validated internal test/recursion
overrides, never discovered from selected workspace. Ignore host proxy/SSH agent
environment for controlled tunnels and child direct egress.

State layout: `$MESTER_STATE_DIR/vm/images/<sha256>/base.qcow2`,
`profiles/<name>/{profile.json,runtime/,disk.qcow2,seed.iso,control-key,known_hosts}`,
`services/`, `runs/<id>/` and `logs/`; secrets under a private `secrets/` directory.
This is a new layout, no implicit compatibility with old overlays. Print effective
root, and refuse to overwrite existing unknown schema/legacy paths.

Profile schema v1: profile UUID/name, canonical workspace, image hash/path,
memory GiB, depth/max, sticky descendants_possible, resolved software version,
guest payload hash, trusted runtime payload hash, lifecycle status. Runtime
identity: PID, `/proc/<pid>/stat` start ticks, executable, exact disk/socket/port
arguments. Metadata writes temp+fsync+atomic replace. Runtime processes verified
before signals; PID existence alone never establishes ownership. Use a short
private XDG runtime socket directory (checked user ownership/mode), fallback
short private temp directory, with <108-byte AF_UNIX paths.

### State machine and failure behavior

`absent → allocated → starting → provisioning → ready → suspended → starting`;
startup failures become `failed` retaining overlay and diagnostics, not ready.
`ready → suspending → suspended`; timeout returns failure with parent still
running/unknown, not falsely suspended. `ready|suspended|failed → deleting →
absent` only after destructive/descendant/auth checks. Stale/ambiguous identities
are `unknown`, block mutation and point to read-only doctor. No repair during
status. Base cache lock and profile lock serialize state mutation; warm read of
running same-workspace profile checks identity/readiness and returns without
package/config work. A workspace switch and resize share the same graceful
shutdown helper including live-descendant checks; neither replaces overlay.

Bound stages: proxy ready 5s, virtiofs ready 15s, SSH connect 10s/probe with 300s
overall boot deadline, cloud-init/setup 1800s, graceful shutdown 60s. Child remote
commands have stage deadlines inside the SSH overall deadline; timeout kills only
owned test transport/processes, preserves candidate guest/profile for inspection.
Exit statuses: 0 success; 2 usage/preflight/BLOCKED; 1 observed operational/test
failure; 124 deadline; 130 interrupt; 143 termination. Preserve raw underlying rc
as structured `cause_exit_code` when translating at CLI boundary.

### Self-development workflow (normative)

1. Host H starts parent G0 with only selected Little Mester checkout S shared,
   `--unsafe-recurse --max-depth 2`, private host control state not exported.
   Parent launcher/SSH/QEMU processes are frozen known-good runtime, not read from
   source files on every lifecycle call. Setup prints the exact private trusted
   controller path to use for this profile while self-editing. A host user must
   not execute the agent-edited `./lab-vm` on H until reviewed; freezing a runtime
   does not make an independently re-executed untrusted source launcher safe.
2. Agent in G0 edits S (including any `lab-vm`, runner, proxy, seed/bootstrap,
   guest-runtime or lifecycle code). No restart of G0 to exercise those edits.
3. From G0, `scripts/vm-self-test.py --source /workspace --max-depth 2` (proposed)
   checks persisted authorization, KVM/CPU, RAM/disk and inherited policy, then
   parent-pushes a filtered frozen candidate S1 with whole-tree manifest M1.
4. Candidate `S1/lab-vm` executes in G0 with an exact new private state root and
   explicit G1 name. Its changed runner/proxy/cloud-init/guest installer actually
   create G1 with fresh overlay/seed. The test runner is known-good orchestration;
   it does not substitute parent VM code or patch active source. G1 gets S1 only,
   installs private MCP/plugins from S1, and reports hashes/version/readiness.
5. Runner checks candidate-generated effective QEMU/network/mount configuration,
   guest boot, candidate-specific test assertion (a changed bootstrap sentinel
   AND changed lifecycle behavior), actual workload, isolation and nested policy.
   G1 can execute candidate launcher again to create G2; cap2 refuses G3 before
   state. A child agent performs a harmless authorized file/test task; a shell
   smoke cannot be reported as the full agent-driven acceptance.
6. Inject a deliberate candidate bootstrap failure in a disposable COPY, observe
   nonzero staged report and preserved evidence, fix it, take S2/M2, and launch
   fresh G1/G2 profiles. Verify output proves M2 rather than M1 or known-good S0.
7. G0 checks its own TUI/session, mounted source and ongoing workload still work;
   failed candidate reports remain. Structured reports are collected via
   parent-initiated SSH, no child-request host control service. Explicit per-run
   cleanup shuts descendants deepest-first and deletes only runner-owned state
   after preserving reports. Never delete S, H profiles, base cache or G0.

The runner is a bounded test driver, not an autonomous merge/retry supervisor.
Repeated agent-directed attempts within the selected depth/resource/workspace
authority need no new interactive prompt (`--yes --permissions auto`), but new
provider credentials, host changes and destructive cleanup outside the exact run
remain separately authorized. A candidate bug may damage G0 guest state (the
agent already has guest admin); it must not acquire H authority. Retaining the
host-Docker session and known-good host control path remains essential rollback.

## 6. Trust model and security verification

| Boundary/invariant | Enforcement | Required negative evidence |
|---|---|---|
| H files → G0 | Canonical single virtiofs export, no control-state overlap | SEC-FS attempts external file/symlink, home/state/key/socket access denied |
| G0 files → G1 | Fresh filtered parent-selected snapshot, private independent state | SEC-SECRET excludes history/env/auth/keys/caches; traversal/special files refused |
| Guest → H network | restrict=on, explicit guestfwd only, public proxy validates DNS/IP/routes | SEC-NET public positive plus private/LAN/metadata/host local/direct/raw DNS/non-HTTP negatives |
| G1 → G0/ancestor | Same boundary, child policy plus chained parent proxy | SEC-NET parent denial/no-fallback; no ancestor control, metrics/model/browser by default |
| SSH/control → host | Exact profile state, identity checks, `-F /dev/null`, no agent/X11/implicit forwarding | V-PROCESS stale/reused PID, hostile ssh config and profile traversal never execute/signal |
| OAuth → host browser | One validated capability, per-session token, one callback lease | V-AUTH invalid origin/duplicate redirect/port conflict/no token/oversize all fail before open |
| Secrets at rest | Native guest store, 0600/0700 escrow, atomic export before deletion | SEC-SECRET export failure leaves guest/old escrow; scan seed/image/source/report for canaries |
| Resource/cleanup | Per-root allocation lock, RAM/disk checks, sticky descendant bit, known owned process identities | V-RESOURCE concurrent allocation, limit/headroom failure before state; V-LIFE unknown children block |

Compromised agent has guest admin and may corrupt the explicitly selected writable
workspace. A compromised child may corrupt only its deliberately exported snapshot
through filesystem sharing. Guest roots can edit their own depth marker or run
raw QEMU: depth enforcement is a supported-launcher safeguard, NOT an adversarial
guest-root security boundary. Aggregate host containment is the outer VM's fixed
allocation and host isolation. A stronger root-proof quota service would change
scope/privilege architecture and is not smuggled into this plan. No claim of
protection against QEMU/KVM/virtiofs/kernel vulnerabilities or malicious host owner.

Optional child model relay: only explicit local-provider selection creates an
immediate-parent loopback relay to the selected existing guestfwd API. The child
gets only that API, not the ancestor host loopback. No arbitrary relay targets.
Optional credentials are parent-pushed over SSH using supported provider adapters;
do not inherit ancestor escrow paths. Metrics/browser are off in child by default;
browser flow only on explicitly approved immediate-parent capability.

**2026-11-06 refinement:** the selected model API must be operation-constrained,
not merely port-constrained. Backend isolation, gateway protocol/resource rules,
per-hop identity and no raw-backend bypass are specified in the controlling
[inference amendment](docs/vm-inference-performance-plan.md). Existing launcher
TCP forwards do not establish these properties. An external model returns data;
guest harness executes tools in the guest, never on the serving host. The selected
workspace, curated metrics and optional browser action remain explicit exceptions
to a no-general-host-access boundary; do not advertise literal zero crossings.

## 7. Intentional behavior changes requiring approval

Approval of Round 2 must cover these explicit design choices:

1. New state namespace/schema with no automatic legacy-profile migration. Existing
   original overlays/session remain untouched; migration is separate inventory work.
2. Default guest Docker is rootful (or `none`), because guest admin is already
   granted. Remove rootless repair complexity, not outer isolation.
3. VM launch never auto-starts/repairs host model containers. Explicit local model
   endpoint or remote provider; model-none entry remains usable. Preserve Docker
   fallback/model stack separately. With no provider configured the initial VM
   enters model-none configuration rather than assuming a running llama.cpp API;
   the documented no-subscription configuration selects an existing local API.
4. `rebuild` means reset named profile using immutable checksum cache, NOT deleting
   shared base. `stop/down` stay destructive and documented; suspend is daily stop.
5. Latest OpenCode is resolved once for a fresh setup, exact version recorded;
   unsupported auth integration version fails closed until compatibility checked.
   This retains #42's latest default but does not pretend every latest OAuth works.
6. Full VM self-modification proof—including candidate VM boot and fail→fix→pass—
   is release-blocking. Mere nested agents or an infrastructure-only smoke is not
   an equivalent substitute. Cap2 proof required on supported nested-KVM host.

Unknown hosts may still use explicit Docker fallback, but cannot be represented as
passing recursive VM support. Optional research R32/R33 is deliberately deferred.

## 8. Traceability / behavioral equivalence / commits

Each essential requirement has an original evidence path in §3, a replacement
decision and task below, meaningful verification in §9, and commit checkpoint.
No untracked required behavior is intentionally dropped. R32/R33 are explicit
exclusions; R34 is a preservation/scope condition, not a requirement to repair the
old branch's interrupted host campaign before testing the replacement.

| Requirements | Decision → task | Test groups | Commit |
|---|---|---|---|
| R02,R28 | D01,D02 → TASK-01 | V-CONFIG | C01 |
| R03,R04 | D02 → TASK-02 | V-PATH, SEC-FS | C01 |
| R08 | D02,D08 → TASK-03 | V-IMAGE | C02 |
| R05,R06,R07 | D03 → TASK-04 | V-PROXY, SEC-NET | C03 |
| R02,R05,R09,R28 | D01,D02,D03 → TASK-05 | V-PROCESS | C04 |
| R09,R10,R31 | D04 → TASK-06 | V-GUEST, HOST-CLEAN | C05 |
| R04,R16,R17,R18,R19 | D02,D08 → TASK-07 | V-LIFE,V-RESOURCE,E2E-LIFE | C06 |
| R01,R11,R13,R26 | D05 → TASK-08 | V-AGENT,E2E-MODEL,E2E-AGENTS,HUMAN-PERM | C07 |
| R12 | D03,D05 → TASK-09 | V-METRICS,HUMAN-TUI | C08 |
| R14,R15 | D06 → TASK-10 | V-AUTH,SEC-SECRET,HUMAN-AUTH | C09 |
| R20,R23 | D07 → TASK-11 | V-SNAPSHOT,SEC-SECRET | C10 |
| R07,R21,R22,R25 | D03,D07 → TASK-12 | V-RECURSE,E2E-DEPTH,SEC-NET | C10 |
| R20,R21,R24 | D07,D09 → TASK-13 | V-SELF,E2E-SELF | C11 |
| R27 | D04,D06 → TASK-14 | V-UPDATE,E2E-LIFE | C12 |
| R05–R25,R30,R34 | D09 → TASK-15 | HOST-/E2E-/SEC-/HUMAN- suites | C13 |
| R01,R29,R30,R31,R32,R33 | D10 → TASK-16 | V-DISPATCH,V-DOCS,E2E-DAILY | C14 |
| R35,R37 | D11 → INF-01–06/10 | V-INFERENCE, SEC-NET/FS/SECRET, E2E-SELF | C13c-a/b/c/f |
| R36 | D12 → INF-07/08/11 | E2E-MODEL-PERF, E2E-MODEL | C13c-d/f |
| R38 | D13 → companion §9 F1–F8 | Conditional VM-local artifact/placement tests | Deferred; owner must bring forward |

Commit scopes/files/tests are exactly the associated task specs. Dependency order
is C01→C02→…→C14 as the reviewed execution order; do not commit known broken
intermediate entry points. TASK-01/02 and TASK-11/12 are CONTINUE+COMMIT groups.

| Checkpoint | Proposed message | Included tasks / acceptance before commit |
|---|---|---|
| C01 | `feat(vm): define validated profiles and workspace boundaries` | 01–02; config/path/security tests, original main regressions; no operational route before ready |
| C02 | `feat(vm): add immutable base cache and profile disks` | 03; checksum/backing/locking/error tests |
| C03 | `feat(vm): enforce public-only chained proxy egress` | 04; all retained proxy behavior and negative tests |
| C04 | `feat(vm): launch owned restricted QEMU profiles` | 05; process/SSH/argv/socket/failure tests |
| C05 | `feat(vm): provision native guest harness and workloads` | 06; schema/bootstrap/private payload tests |
| C06 | `feat(vm): add safe profile lifecycle and resource checks` | 07; lifecycle/resize/children/refusal tests |
| C07 | `feat(vm): launch native agents with explicit providers and permissions` | 08; argv/model-none/session/harness tests |
| C08 | `feat(vm): preserve metrics and terminal integrations` | 09; endpoint/plugin/static failure tests |
| C09 | `feat(vm): add scoped native auth and credential persistence` | 10; bridge/escrow/lifetime/redaction tests |
| C10 | `feat(vm): run revision-bound recursive candidate VMs` | 11–12; full snapshot/depth/crossing tests |
| C11 | `test(vm): add changed-VM fail-fix self-test workflow` | 13; deterministic runner fault/candidate tests |
| C12 | `feat(vm): add explicit state-preserving guest updates` | 14; update/no-warm-install/config-hash tests |
| C13 | `test(vm): add scoped host and recursive acceptance suites` | 15; local test-driver tests; live evidence linked separately, never fabricated |
| C13a | `feat(lab): stage explicit VM and Docker rollback routes` | TASK-15A / TASK-16 preparation slice; routing/permission parity fixtures; temporary transition dispatcher remains Docker and public VM startup stays gated; intended released default remains VM |
| C13b | `test(vm): complete staged provider and owner acceptance workflow` | TASK-15B; executable gate coverage, explicit provider configuration, resumable stages, semi-automatic browser auth, failure/cleanup fixtures and friendly host handoff; no live result implied |
| C14 | `feat(lab): enable accepted VM workflow with Docker rollback` | 16; docs/CI/build/all local + required real-host gates; otherwise keep dispatcher staged and checkpoint BLOCKED |

A code checkpoint can be locally validated without claiming live acceptance;
C14/default cutover and final release require the live gates. If host testing is
unavailable, keep staged route and any replacement PR draft, explicitly blocked.
Do not mark the final approved scope complete on that basis.

## 9. Complete verification strategy

Commands executed in Round 1 on original detached source, not replacement code:

- `python3 -B -m unittest discover -s tests -p test_vm_proxy.py -v`: 28 PASS.
- Same command for `test_vm_memory_budget.py`: 7 PASS.
- Same command for `test_vm_opencode_config.py`: 4 PASS.
- `bash scripts/check-vm-cloud-init-schema.sh`: exit 0, schema valid; cloud-init
  emitted datasource/pickle permission warnings while discovering local context.
  This is schema validation only, not guest provisioning or a clean host result.

All other command forms below were confirmed in source/CI but not executed here,
or are explicitly proposed. Task specs must first verify dependencies and inspect
side effects in disposable fixtures. Never use `run-tests.sh` to investigate the
live session: it delegates to Docker execution.

### Fast tests: required at each affected checkpoint, full suite at milestones

| ID | Required coverage / expected result |
|---|---|
| V-CONFIG | Parser flags anywhere, `--`, validation/conflicts/config precedence, no shell eval, unsupported host/KVM, debug redaction; rc2 and zero mutations on refusal |
| V-PATH | Canonical/symlink/ancestor overlaps, uid/modes, root/home/secrets/state, exact profile names, metadata schema/atomic failure, competing locks; denied before export |
| V-IMAGE | Correct pin/hash, corrupt download, invalid base, interrupted staging, concurrent cache access, overlays/backing references; no shared-base replacement |
| V-PROXY | Retain all 28 source test scenarios; add malformed/oversized request, disconnect/timeouts/concurrency, policy deny/reload, child narrower policy, upstream unavailable/no fallback |
| V-PROCESS | Exact QEMU devices/shareable RAM/restrict/selected forwards; no host agent/X11 config; short socket/long profile paths; reused PID/identity mismatch never signaled; partial startup rollback only owned helpers; bounded SSH/cloud diagnostics |
| V-GUEST | Schema, bootstrap shell syntax, optional runtime, private Node>=20/MCP lockfile build, fresh install version manifest, UID/GID/mount, proxy for package pulls, marker committed only at successful end |
| V-LIFE | Repeated up/status/suspend/resume/down, A→B→B, resize, timeout, unknown/stale processes, nested guard, sticky discard, auth-export refusal and exact reset; workspace/base/evidence always preserved |
| V-RESOURCE | 8/4/2/1 defaults, explicit setup override, persisted reuse, cgroup v1/v2 ancestry, no missing-limit assume-unlimited, 512 MiB reserve, managed release-only projection, concurrent reservation/refusal before state |
| V-AGENT | Native cwd, exact args once/ask/auto conflict/deny intact, model-none/remote/local, no model recovery, Aider adapter whitelist, same-guest concurrent sessions, terminal exit statuses |
| V-METRICS | Reuse curated endpoints/tool/UI; asynchronous slow GPU, concurrent sessions, unavailable endpoint nonblocking, child no ancestor metrics, no exposed arbitrary paths |
| V-AUTH | Retain 13 source browser scenarios; token/auth/body bounds, exact endpoint+one redirect, failed forwarding/no open, concurrent ports, cancellation/expiry cleanup, private atomic escrow, off/auto/provider selectors and unsupported provider errors |
| V-SNAPSHOT | Dirty/untracked/tracked, modes/links/content manifest, history exclusion, secret-name canaries, symlink/special/race refusal, byte/free-space cap, changed source/reuse refusal, fresh-run isolation |
| V-RECURSE | Depth0 default/max32 validation, persisted identity/env reset conflict, cap-lowering only, missing KVM before snapshot, no implicit endpoints, explicit model/secret relay only, exact candidate launcher/payload and run ownership |
| V-SELF | Fault-inject snapshot/start/cloud-init/SSH/workload/report/timeouts/cancel; candidate-vs-parent source proof; fresh fail/fix, only run-owned cleanup, NOT_RUN/BLOCKED never PASS; no discovery/repair |
| V-UPDATE | Default scan/current no mutation; explicit confirmation/cancel/yes; selected-only apply; TUI/workload guard; failed scan/SSH no install; same payload hash as setup, no overlay/auth/workspace deletion |
| V-DISPATCH | Explicit VM route + preserved Docker args/helpers; final default route tests, no leaked flags and unchanged Docker metrics/security |
| V-DOCS | Correct support/lifecycle/secret/default/recursion claims, local links, Bash/Fish, one decisions source, no unrelated feature/ticket churn |

Fast CI commands (provisional on replacement until files/tasks exist):
`bats tests/`; `python3 -B -m unittest discover -s tests -p 'test_vm_*.py' -v`;
`shellcheck lab lab-docker lab-vm scripts/*.sh lib/vm/*.sh` (full severity, no
blanket error-only escape); Python compile plus `ruff check` on touched Python;
`bash scripts/check-vm-cloud-init-schema.sh`; `python3 scripts/check-docs-consistency.py`.
Inside `little-mester-mcp`: `npm ci`, `npm run build`, `npm run check:package`,
`npm test` (build performs TypeScript checking). MCP ticket validation through
the available engine tool; no fallback filesystem edits. VM plugin checks use
existing Bats/static guards plus a supported-runtime load test, not invented npm
scripts in plugins without a package test command.

### Real integration and end-to-end gates

Implemented entry points are `scripts/vm-self-test.py` and
`scripts/vm-host-acceptance.py`; their actual `--help` and owner runbook, not the
historical proposed CLI, control available commands. C13b extends these existing
tools rather than creating a parallel campaign controller. Use unique generated
disposable profiles/state roots. No defaults to a developer profile or stable
exported snapshot. Consult the actual `--help` and current runbook for available
stage/provider/resume options; their availability does not authorize execution.

### Local-first acceptance scope — owner revision after C13b

**2026-11-06 approved extension:** execute the controlling inference amendment's
INF-00–12 in addition to the required local-first gates. Its C13c checkpoints
close inference isolation, topology/relay, measured one-level performance and
remaining usability/security procedures before final frozen acceptance/C14.
No command that assumes parent-loopback model servers may substitute for
topology-aware discovery. Approval of planning/checkpoint commits is not permission
to start that implementation or live campaign before the owner's requested pause.

**Current next-work authority:** [local-first C14 handoff plan](docs/vm-c14-handoff-plan.md).
The owner selected a minimal repository-owned canary checker (1A), existing local
models first, and disposable campaign credentials where separately approved.
Live OpenAI/provider credential acceptance is deferred follow-up, not a merge or
C14 blocker. Preserve native browser auth and required automated regression and
credential-safety tests; never mark deferred live evidence PASS. Required
non-auth security/workload/lifecycle/nesting evidence is unchanged.

The owner subsequently authorized **local §1 implementation** under the handoff
plan. The canary checker, local-first runner reconciliation and fixture tests are
implemented and locally validated; no live action is authorized. This bounded
scope supersedes conflicting older all-auth-before-C14 language below and in
TASK-15/TASK-16. C13b remains committed at `d3751e4`, ledger `777f5a9`; at that
checkpoint the uncommitted changes were not live campaign evidence.

#### Current execution amendment — 2026-11-05

The owner subsequently approved this fixed order: (1) implement an experimental
explicit development entry and hand off the first VM for the owner to start on
this branch, (2) implement the remaining bounded security/evidence work, (3) run
integrated acceptance on a frozen candidate, then (4) perform C14 default/rollback
and closeout. The owner-approved useful-evidence campaign has a 360-minute ceiling
including cleanup and the one diagnosed retry; do not ask for campaign approval
again. Pause for real owner observations or effects outside scope. Manual owner
OpenAI authentication experimentation is allowed in the disposable VM but is not
acceptance; paid streams and credential export/import are excluded. The assistant
must not start a VM. Ordinary `lab` remains on Docker until C14; no push, merge or
C14 activation is authorized. This amendment supersedes the earlier local-only
execution boundary below while retaining the security exclusions and required
campaign evidence.

### Acceptance-readiness revision — 2026-11-05 (historical; amended above)

This section supersedes stale C12-baseline/finish-C13-first/"TASK-16 not started"
sequencing below, but does not waive any gate. C13 code is committed at `1e0b0ba`;
the cgroup v2 fix is `232180b`, recorded at `336cf69`. The owner ran only a
read-only preflight on `336cf69c8b287f3c7f6e3539efdae7c1d5bf9503`: every check
passed, including 36,201 MiB available for 8,192 MiB + 512 MiB reserve. This is
host-readiness evidence, not boot, isolation, workload, auth, or release evidence.

The planning revision initially requested documentation only. The owner later
authorized **C13a implementation only**; its routes and local fixtures are now
committed locally at `599e9ef` (see the progress closeout), but this is not live
rollback evidence. C13b, live campaign/credential
actions, push and VM-default cutover remain unauthorized. The intended released
default is VM with Docker optional; the transition dispatcher remains on Docker
until required acceptance and the separately authorized C14 cutover. The owner
selected Ollama, llama.cpp and OpenAI browser login for future acceptance;
endpoint/model selection occurs through bounded read-only discovery followed by
explicit owner choice before execution.

#### Ordered prerequisite checkpoints

1. **C13a / TASK-15A: stage routes without cutover — implemented locally.**
   Extract the supported Docker path into `lab-docker`, preserving helper sourcing, flags/denies, metrics and
   existing-session reuse. Add explicit `lab vm` routing without enabling public
   VM startup; during the transition ordinary `lab` remains routed to Docker.
   This staging does not change the intended VM released default. It is the previously permitted
   non-default TASK-16 preparation slice, now named so rollback preparation does
   not depend on the acceptance that tests it. Local route/parity tests pass; do
   not recreate or exercise the working host Docker session here. Existence of an
   executable is not rollback proof; actual session reuse remains to be checked
    on the host. The local code checkpoint is `599e9ef`; no push was made.
2. **C13b / TASK-15B: finish acceptance readiness.** Audit C13 against every gate,
   implement missing live probe/workload/provider drivers and human procedures,
   staged resume and safe auth/configuration handling using frozen APIs. A gate
   that merely accepts a checklist has reporting support, not complete executable
   coverage. Fix topology, resource, source/runtime, permission and evidence
   defects found by that audit before presenting another full host campaign.
3. **Owner-reviewed staged host acceptance.** Present exact final revision, source
   hashes, private state/evidence paths, selected endpoints/models, resources,
   optional human/auth checkpoints, permitted effects and cleanup/retry budget.
   Rerun preflight on that revision. A prior green preflight cannot authorize a
   different build, provider scope or credential operation.
4. **TASK-16 / C14: final cutover.** Review all required gate evidence, complete
   remaining docs/CI/integration, then separately authorize VM-default activation
   and final default/rollback smoke. Keep ordinary Docker behavior until then.

#### Campaign configuration, browser login and pauses

- Use an explicit validated campaign configuration separate from ordinary host
  state: provider/model identifiers and selected loopback services only. Read-only
  discovery must not dump environment variables, auth stores, prompts, arbitrary
  service configuration or credentials; never start/reconfigure local model
  services automatically. Test Ollama and llama.cpp sequentially, including five
  actual streams each, to avoid needless simultaneous model RAM pressure.
- OpenAI sign-in is **semi-automatic**, not credential collection: the campaign
  offers an early optional checkpoint, invokes the existing native browser bridge,
  opens the host browser and waits within the callback deadline while the owner
  signs in directly with the provider. Record sanitized status, verified version
  and callback cleanup, not URLs, tokens, browser contents or auth-store output.
  A login-success signal alone is insufficient: verify a real completion too.
- Make auth/human checkpoints explicitly opt-out at each applicable stage; missing
  terminal, EOF, skip, cancellation or timeout means BLOCKED, never implied consent
  or PASS. Skipping auth does not cancel boot, local-provider, network or other
  independent checks. Keep selected OpenAI-dependent gates blocked; a local model
  may satisfy provider-independent agent work but cannot stand in for OpenAI proof.
- Reuse only the exact campaign guest's owner-authorized login for intermediate
  OpenAI tests, with no routine re-login. Private auth stays inside the owned guest
  unless the owner separately enables provider-specific run-owned escrow/import.
  Nested forwarding is default-off and separately selected. Never read/copy the
  working host/Docker auth store or ask the owner to paste credentials here.
- Put refresh persistence, destructive export-failure/delete/reimport, recursive
  provider-import and final browser regression checks at the end, on separately
  confirmed disposable assets. An interactive login still creates sensitive
  credentials: approval must cover their temporary storage, any private export,
  deletion and retention. Do not revoke the account's unrelated sessions. If the
  provider cannot produce refresh evidence during the budget, leave that subcheck
  BLOCKED; do not manufacture token expiry or weaken the refresh requirement.
- Resume only an explicit exact manifest and unchanged tested revision, frozen
  runtime, configuration and profile identities. Never discover/adopt an unrelated
  ready VM. Stages must revalidate prerequisites, serialize writes and prevent
  duplicate mutation; changed code/config requires a fresh approved attempt.
  A pause is not a failed run and needs no new login if the same session is valid.

#### Required stage order and operational coverage

| Stage | Real procedure/evidence required before gate PASS |
|---|---|
| Plan/preflight | Exact checkout/config scope, tools/KVM, each enclosing RAM/cgroup budget, disk/paths/listeners; explicit owner approval before mutation |
| Fresh guest / usability | HOST-CLEAN boot/image/cloud-init/mount/MCP/Docker checks; warm/TUI checkpoint before expensive recursion; baseline identity evidence |
| Early auth / providers | Optional owner browser login; model-none behavior, five real streams per selected Ollama/llama.cpp/OpenAI model, actionable negative provider cases, fixed sanitized results |
| Daily / permissions | Same-profile warm and A→B→B identity checks, actual staged Docker rollback without recreation; real ask/auto/deny and shared/standalone behavior plus human TUI/metrics/dialog observations |
| Work / state | Agent creates guest-only PostgreSQL Compose project, query and failing-test→fix→pass, persistent volume across restart; use retained DB/tools/auth canaries for suspend/resume/update/resize tests; selected config/tools apply must actually occur once, not only scan/cancel/no-op |
| Recursion / boundaries / agents | Actual G0→G1→G2 ancestry, G3 refusal before mutation, changed bootstrap AND lifecycle fail/fix proof, real child-agent task; outer/child filesystem/network/secret checks and two same-guest tasks with independent auth ports |
| End auth / teardown | Optional but required-for-full-PASS refresh/reimport/negative browser tests on disposable assets; deepest-first exact cleanup, helper/socket/port residue verification, protected base/workspace/report and Docker-session preservation |

C13b must produce a gate→subcheck→command/fixture→expected result→evidence→cleanup
matrix. Implement automatable operations, and give concrete terminal instructions
for genuinely human-only actions. Audit that a depth flag or sibling VM is not
reported as actual nested ancestry; check resource availability at each enclosing
guest. Create workload/auth retention evidence **before** lifecycle mutation.
Record teardown results **after** cleanup without promoting untested behavioral
subchecks. No complete checklist flag bundle may be offered as a substitute for
performing the tests. Pin and verify the same supported OpenCode version for any
shared/standalone UI or permission comparison.

#### Budgets, evidence and readiness exit

Default automated stage/attempt wall-clock budget is at most 60 minutes, retaining
the tighter operation deadlines below; human pauses stop the automated-stage
clock, but live browser callbacks still expire and must clean up on deadline.
Use 8→4→2 GiB nested allocations subject to actual per-level checks. Record disk,
download, local-model availability, total campaign time and any paid OpenAI usage
ceiling in the live handoff; no uncapped API spending or automatic service/model
download. One fresh retry per diagnosed failure per revision within the approved
total budget; persistent failure stops dependent stages and preserves evidence.

The host handoff must be copy/paste friendly (Bash/Fish explicitly distinguished):
how to update a clean worktree to the exact commit, create private state outside
the snapshot, plan, approve/start, pause/resume, skip a human/auth checkpoint,
view a concise sanitized report, send it back, and clean the exact run. Each step
must say where it runs, expected output, elapsed bound and stopping conditions.
No raw auth/model transcript capture. Keep reports outside snapshots and terminal
recordings; never include `vm-migration-plan.md` in a clean acceptance snapshot.

Readiness exit: C13a/C13b independently pass their local suites and full regressions,
help/docs match the implemented commands, every required gate has a real procedure,
and no selected prerequisite is knowingly absent. Human skip/provider outage may
still yield partial evidence, but all required gates remain visible and must pass
before cutover. C13b implementation and live campaign execution require later
authorization; these plan edits authorize neither.

### TASK-15 execution policy and order (2026-11-05 reconciliation)

The owner requested a planning-only reconciliation on 2026-11-05, not live-test
execution. Historical source was reread through MCP at original branch revision
`97af1d83a16fce68ddcc3390276c218a1b0cd518`; the active replacement baseline is C12
`65a798d`. The default replacement ticket store does not contain #50 or the
original #41 handoff. This is expected branch-local history, not a missing
permission or reason to recreate old tickets. For historical inspection use an
explicit MCP `ticket_dir` in an original-source checkout. Never copy that store
into the replacement, run its old commands here, or wait for an old ticket to
change before advancing this plan.

1. **Finish the C13 implementation locally first.** Implement acceptance suites,
   guest-isolation probes, reports/docs and a real adapter for TASK-13's currently
   unavailable live backend. Wire the adapter to the existing frozen lifecycle,
   agent and recursion APIs; do not write a second lifecycle controller. Test the
   complete dry-run/failure/interrupt/cleanup contracts and verify actual `--help`.
   A report aggregator or fixture adapter alone is not sufficient. Public `run`
   and `up` remain gated; the explicit acceptance path can exercise those existing
   APIs without enabling the production default or waiting for TASK-16.
2. **Prepare one replacement campaign handoff.** Record exact tested revision,
   source/snapshot and private run-state/evidence roots, protected assets, resource
   limits, selected providers, expected side effects and cleanup/retry bounds.
   Present the generated dry-run to the owner. Authorize the scoped campaign once
   before live mutation; record the scope/results through MCP when applicable,
   using the plan/progress as the canonical cross-stage checklist. No new schema,
   ticket migration or chain of per-command approvals is a prerequisite.
3. **Read-only preflight, then explicit clean setup.** Check the actual executing
   host or supported enclosing VM: KVM/nested CPU access, tools, disk/RAM/cgroups,
   mount/listener collisions and exact identities. Do not infer capability from
   the words “host”, “container” or “CachyOS”; do not impose a physical-host-only
   check when a qualified nested environment is the intended execution context.
   Report that context accurately. Missing capabilities block before mutation;
   prerequisite installation remains a separately confirmed setup operation.
   Start only fresh run-owned profiles against unique immutable snapshots. Do
   not reuse the old stable snapshot or auto-select its live exporter. An unrelated
   exporter or interrupted apt on an untouched legacy profile does not block a
   disjoint run; real resource/path collisions do.
4. **Cheap usability checks before expensive recursion.** Establish HOST-CLEAN,
   basic warm launch and model-none behavior, then actual repeated selected-model
   streaming. Perform human TUI/metrics/exit, permission and browser-auth checks
   once this disposable guest is usable. Automation must offer a human checkpoint,
   not mark a default/Enter as an observation. Skip unavailable human observations
   as BLOCKED while retaining independent automated results; do not require all
   recursion to pass before a person may inspect a working TUI.
5. **Exercise retained state and real work.** Run guest-only PostgreSQL Compose
   and failing-test→fix→pass, same-profile A→B→B and warm PID/identity checks,
   suspend/resume/resize and update scan/cancel/selected apply with auth, keys,
   tools and workload-volume retention. Use separate disposable assets for
   destructive auth export/reimport/failure tests. Actual update acceptance is
   required; the old branch's successful scan does not establish it here.
6. **Prove recursive self-development and boundaries.** Run the TASK-13 adapter:
   changed VM bootstrap and launcher, intentional failure→fixed fresh candidate,
   G1/G2 at max2, G3 refused before state/QEMU, an actual child-agent task and
   parent continuity. Check filesystem/secret/network boundaries at outer and
   child, plus concurrent same-guest agents and auth-port isolation. Resource
   preflight applies at each enclosing level, including retained failed children.
   Independent boundary checks may run earlier; successful boot/apt/health does
   not stand in for them.
7. **Close out safely, then TASK-16.** Persist private reports and explicitly clean
   only manifest-owned disposable assets using verified lifecycle operations;
   never raw PID signaling or broad discovery/deletion. Verify no residue or
   protected-state change. Review every gate below, including any BLOCKED items.
   TASK-16 stages explicit routing/Docker rollback and validates its route-specific
   smoke; only then, with the other required acceptance passed, enable the VM
   default and run the final default/rollback smoke. Final delivery/merge remains
   separately authorized; a C13 code commit does not imply release acceptance.

**Recovery without deadlock:** each live command has a deadline, original exit
cause and structural stage evidence; SSH completion must not wait indefinitely
on inherited stdout. Failure stops dependent stages, not unrelated local work.
Inspect/fix/test locally, then resume or use a fresh generated attempt within the
approved run/resource budget. Default retry limit is one fresh retry per diagnosed
failure per revision; persistent failure or a depleted budget is BLOCKED pending
review, not an endless automatic loop. Cleanup of verified stopped run-owned
failed candidates after evidence retention is included in the campaign scope;
unknown/unresponsive ownership is preserved for review. Normal explicit human
OAuth approval and destructive-action confirmations still apply. No standing
approval authorizes changes to legacy/default VMs, active snapshots, host Docker
sessions, host services, firewall policy or credentials outside the chosen narrow
auth workflow. Keep the working Docker session and private credentials intact;
privately export/verify before any separately approved container recreation.

| Gate | Observable pass and important failures |
|---|---|
| HOST-PREFLIGHT | One owner-approved replacement campaign scope/revision; KVM/CPU/tools/virtiofs sandbox, RAM/cgroup and disk budget in actual execution context; unique exact-owned profiles/snapshots; preserve protected sessions/auth; failed check BLOCKED before mutation |
| HOST-CLEAN | Fresh cached-image integrity, fresh boot/schema/cloud-init clean state, virtiofs UID/GID, Node/MCP, guest Docker and no host Docker requirement; CPU-only evidence separately labeled |
| E2E-DAILY | One command enters native TUI; two identical starts no reboot/install; A→B→B same overlay/keys; normal warm same PID; Docker rollback opens existing supported path without recreation |
| E2E-MODEL | Each configured Ollama/llama.cpp plus selected remote provider completes actual repeated streaming requests (5 per configured provider); health only insufficient; denial/missing credentials/outage actionable; model-none shell/auth works |
| V-INFERENCE | Synthetic exact method/path/schema/model/upstream validation, hostile framing/URL/file/admin/tool fields denied before backend contact; incremental bounded streaming/cancel/backpressure, exact relay identity/no fallback; actual backend protocol contract reviewed |
| E2E-MODEL-PERF | Owner-frozen same-model/device/config direct B vs gateway/G0 measurements; >=95% warmed one-level generation ratio per required serial case, agreed TTFT/concurrency/resource/reliability gates; absolute samples and nested deltas reported without transcript/secret capture; isolation independently required |
| E2E-WORK | Agent creates guest-only PostgreSQL Compose stack, health/query, failing project test→fix→pass, stop/restart/query persisted volume, down; bind/guest-loopback port works; H has no published guest service/socket |
| E2E-SELF | Full changed-VM workflow §5: edit candidate VM bootstrap AND lifecycle/command construction, fresh G1 then G2, runtime/source hashes match, intentional failure→fixed fresh pass, child agent task, parent remains usable, scoped cleanup; unchanged boot/mock is FAIL |
| E2E-DEPTH | Actual depth0→1→2 boots, persisted max2 after independent login; depth3 refused before state/QEMU; unauthorized reset/increase/missing KVM similarly denied |
| E2E-LIFE | Repeated suspend/resume preserves auth/DB volume/installed tools/keys/workspace; update scan/cancel/no-op and selected apply preserve that state with exact installed-version evidence; live child blocks parent; explicit deepest-first shutdown; graceful resize verifies guest MemTotal/preserved identity; destructive exact profile teardown leaves no helper/socket residue and base/workspace/reports intact |
| E2E-AGENTS | Two same-guest agents complete separate authorized file/test tasks, independent auth ports and clean cancellation; document shared-workspace risk, no supervisor claim |
| SEC-FS | Guest/child can't read out-of-scope host files/control keys/socket; escaping symlinks refused; state-root overlap blocked, one export verified |
| SEC-NET | At outer and child: public HTTP/HTTPS via policy works; direct Internet/raw DNS/UDP/non-HTTP, loopback host, local/direct public subnet, private/LAN/metadata/ancestor targets denied; configured denied hostname and mixed DNS mocked deterministically plus bounded live probes; inspect actual host/outer listener bind addresses and owning identities as well as QEMU argv; no wildcard/LAN binds or host-published Compose ports |
| SEC-SECRET | Canary credential absent in snapshot/Git history/seed/base/log/report; per-provider opt-in only; import/export private and atomic; induced export failure prevents deletion; refreshed guest state survives restart/import |
| HUMAN-TUI | Same terminal compare supported guest dialogs, metrics sidebar/tool, Ctrl-C cancel/confirm and retry rendering; record version, no secret screenshots |
| HUMAN-PERM | Actual guest harmless allowed tool without prompt under requested auto, explicit deny stays denied; compare ask and shared/standalone same version; argv/UI/action all recorded |
| HUMAN-AUTH | Native browser success, cancel/interrupt/timeout/occupied port/egress-denied; auth-list + one completion, temporary callback gone after attempt, refreshed-token persistence, disposable delete/reimport and explicit recursive supported-provider import; no raw OAuth URL/token records |

### Limits, evidence and CI levels

Fast tests <=10 minutes/job; no KVM required. Run impacted suites before each
commit, full fast suite after C06, C10, C12, C14. Real-host tests manual/opt-in
KVM runner, not pretended on GitHub's unqualified runner. C05/C06 establish
disposable boot/lifecycle milestone; C10/C11 establish recursion/self-development;
C09/C12 establish auth/update; C14 requires all applicable gate evidence and CI.

Nested default allocations 8→4→2 GiB; outer overhead/reserve means nominal RAM
alone does not assure fit. Preflight EACH enclosing level, not sum all nested
allocations against H. Root allocation 2 vCPUs and 20 GiB virtual disk; bounded
snapshot 2 GiB (16 max override) plus twice copied bytes +128 MiB free staging.
Overlay growth checked against free space; no full-disk quota guarantee is
claimed. Bound noninteractive serial/provision logs to 10 MiB per file with one
rotated segment, retain only five routine launcher runs, and preserve failed-run
structured reports until explicit cleanup. A bounded log sink must continue
draining output after rotation so it cannot hang QEMU. Parent hardware limits
bound actual guest CPU/RAM. Proxy 64 concurrent
connections, bounded headers/bodies/idle time; no secrets in diagnostics.

Self-test deadline 60 minutes/attempt, 1800s boot/provision stage, 120s per normal
guest test/probe, 60s graceful shutdown; explicit configurable larger budget
requires recorded rationale, never unbounded SSH reads. Acceptance artifacts
0600 with schema version, run/profile IDs, source HEAD/tree/payload hashes,
software/image versions, stages, elapsed/deadline, result/cause rc, scoped
observations and preserved diagnostic paths. No model prompts/auth URLs/tokens.
Reports survive failure/interruption and are outside exported workspaces. Failed
run cleanup is an explicit run-ID operation; no wildcard/default profile sweep.

Human OAuth/terminal rendering/actual permissions require explicit observations;
mark unavailable required evidence BLOCKED, not waived. For inbound listener
exposure, adopt the original owner's #43 clarification: actual OS socket/address
and process-ownership checks at host and outer replace a mandatory second LAN
device. Pair them with guest/child negative egress probes; QEMU argv alone is
insufficient. A second-device probe is useful supplementary evidence, not a new
blocking prerequisite; absent such a probe, do not claim measured physical-LAN
reachability. Fresh OpenCode compatibility is tested in an isolated guest, never
by modifying the active harness session. Legacy 2.2.0 auth observations do not
replace tests at the replacement's reviewed 2.2.5 compatibility version.

## 10. Risk register and confidence

| Risk | Severity / mitigation / gate |
|---|---|
| Nested KVM/virtiofs not usable on chosen hardware | High: explicit supported profile, fail closed, actual G1/G2 boot; no TCG/agents-only substitution |
| Candidate VM code destabilizes parent guest | High: frozen known-good control payload, candidate/private state, resource preflight, G0 continuity and host-Docker rollback; not a guest-root-proof sandbox |
| Bootstrap/live model/version behavior differs from mocks | High: exact source/version hashes and fresh VM/live completion gates; no mock acceptance |
| Native OAuth depends on internal provider URL/lifetime behavior | High: retain tested bridge, version compatibility gate, bounded per-attempt lease, native owner acceptance; unsupported latest may block release |
| Credentials leak through source/history/diagnostics or export fails | Critical: filtered snapshot/history, canaries, private runtime transfer, atomic export and preservation; no broad DB copies |
| Shared base/identity cleanup damages other profile | Critical: immutable hash-keyed base, UUID+PID-start identity, exact root/profile locks, no discovery/rebuild cache deletion |
| Route/DNS/proxy parser corner cases | Critical: retain proven address/route tests, reject mixed answers, connect pinned IP, independent nested policy, bounded live negatives |
| Resource estimation races/overcommit | High: allocation lock/reservations plus cgroup/host preflight; recheck before start; preserve on failure; not arbitrary guest-workload quota enforcement |
| Rewrite loses tested behavior while shrinking runner | High: full path dispositions/test obligation map, negative scenarios preserved, complete traceability; smaller line count not success criterion |
| Rootful guest choice/state-layout change rejected | Medium: approval decisions explicit; no implementation before approval |
| Original host valuable/interrupted state | High: preserve those assets; use a separate campaign scope and unique snapshots/profiles, not legacy retry locks or an active stable exporter |
| Task/commit integration drift | Medium: bounded contexts, one coordinator, test-first commits, revalidate revisions, stop for material redesign |
| Port-limited model access still exposes backend administration/SSRF/tools | Critical: operation/schema-constrained gateway, isolated serving envelope, no raw bypass; INF-02–06/08/10 |
| Shared inference misses performance/reliability expectations | High: matched direct baseline, explicit >=95% one-level gate and owner TTFT budget; diagnose transport/queue/device separately, never weaken isolation; INF-07/08 |
| New gateway parsing/dependency adds attack surface | High: freeze protocol/library/limits before coding, maintained streaming implementation, hostile framing/schema/cancel/backpressure fixtures and owner H-review disposition; INF-03/04 |
| Weak-model handoff invents topology/security decisions | High: full task packets/substeps, explicit unknowns and R/I/H labels; owner chooses model and resolves questions, no auto-switch or speculative host effects |

Confidence **8/10** in requirements reconstruction and the architecture/plan.
The user's VM-self-modification goal is now explicit and traceable. Source proves
where complexity and risks arose, and retained proxy/snapshot/auth tests provide
useful reusable behavior. Confidence is not 10: no replacement exists yet,
nested-host/OAuth/TUI acceptance cannot be inferred from investigation, and exact
latest OpenCode compatibility is intentionally a runtime qualification gate.

## 11. Approval, execution and completion contract

After approval: read all task specs, verify live PR/main/replacement revisions and
dirty index, preserve mission/planning/user files, choose isolated worktree if
needed, record approved decisions and exact SHA baseline. Execute TASK-01→16 in
order; delegate only bounded approved implementation, never overlapping writes;
coordinator reviews diffs/tests and owns commits. Every task requires docs→tests→
code→validation. Record task/session/status/files/commands/results/SHA/deviation
in progress and any project-ticket evidence through MCP. Never directly edit
`docs/tickets/` or transplant the historical audit log. No auto-merge.

Minor detail adjustments may proceed with recorded rationale. Changes to security,
public interface, required behavior, model/credential authority, dependencies or
VM recursion semantics require approval. Do not convert successive small changes
into an unapproved redesign. If revisions materially change, stop to rebase the
plan with approval; do not silently restart or discard existing work.

Done means: all P0/P1 requirements trace to implemented tested behavior; approved
intentional changes only; E2E-SELF really exercises changed VM implementation;
all security negatives and human gates evidenced; no orphan owned VM/helpers;
original runtime/branch/user changes preserved; documentation truthful; coherent
14-checkpoint history (justified adjustments recorded); clean relevant diff,
license/build/package/CI/ticket checks pass; replacement PR targets main and links
original PR, plan, actual evidence and limitations. If any host gate unavailable,
release remains blocked; draft PR only if explicitly truthful and safe. Opening
the replacement PR is authorized by Round 2 approval after approved-scope
implementation/validation; merging is never automatic.

**Approval recorded:** the owner answered “Yes, please begin round 2” after the
architecture/execution approval question. This approves decisions §7 and this
package; it does not turn pending host tests into passes or authorize changes to
the preserved original host environment. Actual execution is in the progress ledger.
