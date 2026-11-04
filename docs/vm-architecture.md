# VM harness architecture and trust boundary

## Replacement implementation status (2026-11-04)

The owner approved `PR_REIMPLEMENTATION_PLAN.md` for `feat/vm-approach-2`.
The contract below supersedes the historical proposal later on this page.
C01 CLI/configuration and private profile metadata; C02 fixture-tested
image/overlay/seed primitives; C03 fixture-tested public-only proxy/chaining; and
C04 fixture-tested process supervision/SSH primitives are implemented. C05 adds a
fixture-tested guest-native OpenCode bootstrap, private MCP/config payload, exact
version manifest, payload identity and readiness checks. C06 adds fixture-tested
internal profile start/resume/resize/delete/rebuild controllers; C07 adapters,
C08 metrics isolation and C09 scoped auth/browser primitives are also fixture
validated. C10 adds a revision-bound filtered source snapshot, persistent recursion
identity/depth guards, KVM/CPU preflight, isolated per-run state, candidate argv
binding, and exact recursive cleanup/report preservation. **No image has been
downloaded, no real QEMU/VM has been started, and no host/network or nested-VM
acceptance is claimed.** Only read-only `status`/`list`, graceful `suspend`, and the
fixture-tested exact-run `recursion-clean` operation are dispatched. Public `run`
and `up` remain fail-closed until TASK-16; ordinary `./lab` remains the existing
Docker launcher.

### C11 changed-VM self-test status

`scripts/vm-self-test.py` defines a bounded, ordered evidence state machine and
private 0600 no-clobber reports. Fixture coverage creates filtered source copies,
injects a guest bootstrap sentinel `A` plus exit 42 and one-vCPU argv/assertion,
then fixes only a second copy to sentinel `B` and snapshots it afresh. Tests run
the copied bootstrap prefix in an isolated temporary guest root, inspect the
copied QEMU argv and execute the copied process Bats suite; the selected source
checkout is hash-checked as unchanged. The report distinguishes fixture evidence
from live E2E acceptance, records exact hashes/deadlines/causes and marks live
acceptance `NOT_RUN`.

The live self-test adapter is deliberately unavailable pending the TASK-15 handoff.
The CLI therefore fails closed without starting a VM; fixture success is not
changed-VM, nested-VM, host, network, agent, or human acceptance. Public `run`/`up`
dispatch remains deferred to TASK-16.

### CLI and configuration contract

- `./lab-vm --help`: command/options and actual checkpoint availability.
- `./lab-vm context [--json] [--name NAME]`: read-only effective configuration,
  private profile paths and host capabilities; no directory creation or repairs.
- `./lab-vm setup`: read-only Linux x86_64/KVM/QEMU/virtiofs prerequisite report.
  `setup --install-prereqs` is a separate explicitly approved package action with
  default-no confirmation; normal launch/status/doctor never installs packages.
  Ambient `VM_YES`/`LAB_YES` cannot approve a host package install: skipping its
  confirmation requires `--yes` explicitly on that setup invocation.
- Future `./lab-vm <workspace>` / `run <workspace>` selects the exact named profile
  and enters a native guest agent. `up NAME --workspace PATH` is setup without
  entering the agent. Until implemented, these operations refuse with exit2,
  rather than returning fake success or starting a different profile.
  `--memory` applies only to fresh-profile `run`/`up`, not prerequisite setup.
- Global flags may precede or follow command/path. `--` ends launcher parsing and
  preserves subsequent agent arguments. `--yes` only confirms launcher actions;
  `--auto-accept`/`--permissions auto` requests agent auto mode, never overrides
  an explicit deny. Conflicting modes/duplicate value options fail before state.
- `--recurse` permits a filtered source snapshot; `--unsafe-recurse` separately
  permits writable self-development and requires a workspace containing the
  checkout. Both remain bounded by explicitly configured maximum depth (default0).

Configuration is optional **private JSON data**, never a sourced shell file:
`$MESTER_STATE_DIR/config.json`, default state root
`${XDG_DATA_HOME:-$HOME/.local/share}/little-mester`. Precedence: CLI, supported
environment values, private config, defaults. Schema version1 accepts only
`host_profile`, `workload_runtime`, `opencode_version`, `agent`, `model`, `metrics`,
`auth_forward`, `auth_escrow_dir`. It contains selectors, never credentials.
Unknown fields/schema/types/duplicate JSON keys or insecure config paths refuse.

Defaults: Linux x86_64, guest workload runtime Docker, OpenCode with latest resolved
once at fresh setup, no selected model, top-level metrics on, credential forwarding
off. Local Ollama/llama.cpp requires an explicit loopback API port; remote uses
mediated public egress. No host Docker/model service is implicitly started.

State is outside every workspace: `vm/profiles/NAME/profile.json` plus private
runtime/disks/keys, immutable image cache, services, run evidence and logs. Profile
names are validated, directories0700/files0600, metadata atomically replaced under
ordered locks. Unknown/legacy state is never silently migrated/discovered.
Read-only commands do not create/chmod state. Private control runtime captures
explicit reviewed files independently of the mutable source; re-executing edited
source on the host still requires review.

### Immutable image and per-profile disk contract (implemented C02)

`base` uses an explicit HTTPS URL/SHA-256 pair and stores the converted standalone
qcow2 at `vm/images/<source-sha256>/base.qcow2`. Download bytes are verified before
conversion; the standalone qcow2 format, absence of a backing file, and guest boot
signature are checked before publication. A private manifest records the source
pin and converted artifact hash. Population is serialized per source hash and
published atomically from private staging. An existing valid cache entry is reused;
an existing corrupt/partial entry is preserved and blocks use. A different pin
gets a different cache key—there is no in-place replacement or cache GC.
The initial pin is the URL/SHA pair in `lib/vm/disk.sh` (retained from the
reference implementation); C02 fixture tests validate the enforcement logic,
not the remote endpoint's current availability or bytes. `./lab-vm base` is an
explicit network-writing command and was not invoked for this checkpoint.

Each named profile owns one writable overlay whose absolute backing chain must
match its immutable cached base and profile metadata. Creating an existing overlay
is idempotent only after chain validation; it never silently repairs or resets it.
Later rebuilds may replace only the selected profile overlay, not a shared base.
Nested base downloads must use the parent's public-only proxy with no direct
fallback. Cloud-init user-data is a generated nonsecret value, atomically written
inside the selected private profile; private keys, provider tokens and broad host
data are not valid seed inputs. Fixture tests exercise these functions without network or
QEMU boot; this contract is not host-image acceptance evidence.

### Public-only proxy and nested chaining contract (implemented C03)

The proxy listens only on `127.0.0.1`; host forwarding and guest `guestfwd` are
separate explicit launcher responsibilities. It accepts HTTP only for port 80 and
CONNECT only for port 443. Permission denial and an optional exact-host/domain
allowlist are checked before DNS or socket contact. Direct egress resolves once,
rejects every mixed/private/reserved/multicast answer and host-local or directly
connected host network, then connects to one of those already-validated addresses.
IPv4-mapped, 6to4, Teredo and the well-known NAT64 IPv4 embedding cannot bypass
the address policy.

A nested proxy receives its immediate parent endpoint as an explicit startup
argument (`http://10.0.2.100:3128`, the restricted guestfwd channel); the parser
rejects any other parent endpoint. It applies its own hostname policy first, then
sends HTTP/CONNECT only to that parent; it does not resolve or connect to the
origin and has no direct fallback. The parent remains the final DNS, address and
host-route authority. Recursion configuration emits this argument only from a
validated inherited depth identity; it never infers nesting from a path name.
Proxy credentials and hop-by-hop request headers are not forwarded. Request and
response headers, concurrent handlers and idle time are bounded, ambiguous
request framing is refused, and request URLs/bodies/credentials are not logged.
Policy reload parses a complete candidate before replacing the snapshot; missing,
unreadable or malformed policy leaves the last-known-good policy in force.

C03 validation uses local fixture sockets and mocked DNS/address responses only.
It is not live LAN isolation, parent guestfwd wiring or nested-VM acceptance.

### Process supervision and SSH contract (implemented C04; fixtures only)

`lib/vm/runner.sh` builds QEMU argv without shell evaluation and requires Linux
x86_64 plus accessible `/dev/kvm` before startup state or processes are created.
There is no TCG fallback. The argv uses two host-CPU vCPUs, shareable memfd RAM,
the profile seed and overlay, `vhost-user-fs-pci`, a private short virtiofs socket,
loopback-only SSH forwarding, and `restrict=on`. Only the fixed proxy channel and
explicitly selected model/metrics channels are forwarded. Startup order is proxy,
virtiofsd, then QEMU. A bounded three-attempt SSH-forward bind retry starts a new
QEMU process each time and never signals the process already holding a port.

`lib/vm/process.py` keeps a private atomic process registry containing PID, start
ticks, executable and exact argv. A PID alone never authorizes signalling:
identity comparisons fail closed, and pidfds are used for verified signals. Startup
rollback TERM-sends only the run's recorded logger/supervisor processes, then
rechecks every recorded child; an unknown or still-running process preserves the
run state. A child that exits before its `/proc` argv can be captured is recorded
as already stopped with its original exit status, not as a signalable identity.
QEMU/virtiofs/proxy output is continuously drained into private 10-MiB segments
(current plus one rotated file); logging write failures do not stop pipe draining.
Failure metadata retains the stage, original exit status and bounded log paths.

`lib/vm/ssh.sh` disables user SSH config, agent/X11 forwarding and implicit
forwardings; it pins one per-run `known_hosts` key, requests no PTY for
noninteractive commands, uses an explicit PTY only for interactive commands, and
bounds the complete SSH process with a deadline. Initial trust is necessarily
trust-on-first-use: a complete Ed25519 scan is accepted only while the recorded
QEMU process owns the loopback listener, with ownership checked again after scan.
This ties acceptance to a listener owned by the recorded host process immediately
before and after scanning; it is not independent guest-image attestation. Existing
key mismatches and partial scans never replace trust.

C04 tests use fake executables, synthetic subprocesses, private temporary files
and loopback TCP/Unix sockets only. They do not exercise real QEMU, KVM, a guest
boot, image availability, SSH guest authentication, live host networking or
parent/child SEC-NET behavior. Lifecycle commands remain unavailable until later
tasks wire these primitives into the profile/guest lifecycle.

### Guest bootstrap and native runtime (implemented C05; fixtures only)

`lib/vm/cloud-init.sh` renders one schema-valid, secret-free NoCloud document with
the numeric guest UID/GID, public control key, recursion identity, explicit proxy
environment, and only the `workspace` virtiofs mount. Bootstrap source scripts are
base64-encoded in `write_files`; credentials and private keys are not seeded. The
generated data is checked with the installed `cloud-init schema` validator.

`lib/vm/guest-bootstrap.sh` is a first-boot, guest-root provisioner for the pinned
Ubuntu 24.04 guest. It verifies the workspace mount and ownership, requires the
explicit proxy for apt/curl/npm/pip and Docker, and installs supported Node.js plus
isolated Python tools. The optional rootful Docker/Compose runtime is installed
only when selected, with scoped daemon/client proxy settings; runtime `none` skips
Docker completely. Recursion-only QEMU, virtiofs, cloud-image and test tools are
installed only when the configured depth permits another child.

The agent executes natively in the guest. Bootstrap copies the selected OpenCode
sources/plugins and locked Little Mester MCP package into a guest-private runtime,
runs `npm ci` and its build there, renders native V2 OpenCode config plus separate
CLI-only TUI plugin config, and preserves explicit denies and unrelated provider
settings. `latest` is resolved once and saved as an exact install manifest; the
success marker is written only after version, MCP, config, tooling and mount checks
pass. Before package operations, the guest recomputes the host-selected payload
hash from the mounted workspace and refuses a mismatch. `lib/vm/guest.sh` hashes
relative input paths, file modes, sizes, contents and runtime selectors and
rejects cloud-init errors/recoverable errors during readiness validation.

C05 tests use local fixtures and fake package/runtime commands only. They cover
both Docker selections, private MCP build/retry behavior, exact-version reuse,
warm-bootstrap refusal, payload/hash/readiness checks, config migration and secret
handling. They do not install packages on this host or exercise a real guest. The
real image, KVM, SSH and nested-VM acceptance gates remain restricted and pending.

### Lifecycle and resource contract (C06 fixture-tested; commands undispatched)

The profile state machine is `allocated → starting → provisioning → ready`,
`ready → suspending → suspended → starting`. Failed/ambiguous states do not enter
the start path through the current lifecycle controller; unknown identity blocks
mutation and status is read-only. Repeated same-workspace ready operations verify
the exact owned runtime and bounded guest readiness without bootstrap, package, or
config transfer. A workspace change recomputes the frozen guest payload digest,
gracefully suspends through the same child-inventory guard as resize, stages the
NoCloud seed before metadata commit, then replaces the seed ISO and saved user-data
under the exact profile lock. It preserves the existing overlay and key; B→B is
warm-idempotent. Seed-build failure leaves the suspended metadata and boot assets
unchanged; uncertain publication never starts QEMU.

Default guest RAM is 8/4/2/1 GiB at recursion depths 0/1/2/3+, with a validated
1–128 GiB override only for a fresh profile. Resource checks use host
`MemAvailable`, every applicable cgroup v1/v2 ancestor, and a 512 MiB reserve.
Managed resize may project only the target QEMU's verified private resident pages
as releasable. A private root allocation lock and process-identity-backed
reservations serialize starts; stale reservations are reclaimed only after their
exact owner identity is confirmed stopped. Unreadable cgroup/identity state fails
closed.

Suspend preserves the overlay, guest data, and auth state. Unknown or unmanaged
QEMU children block shutdown; explicit recursive suspend calls a frozen controller
and verifies each child stopped before powering off its parent. Shutdown timeout
never force-kills QEMU and preserves the profile. Destructive down/stop/rebuild
requires exact effect confirmation and a separate sticky-descendant acknowledgement;
`--yes` alone is insufficient. Enabled auth forwarding fails closed until the
TASK-10 exporter exists. Reset/deletion scope is the selected profile only and
must preserve the immutable image cache, host workspace, and reports.

These are implementation contracts, not real-VM claims. The lifecycle, memory,
child-inventory and recursion suites use disposable state, synthetic procfs and
fake callbacks. Lifecycle implementations remain fixture-only and undispatched
beyond status/list/suspend/recursion-clean. Real image, KVM, guest,
live-network/SEC-NET, and nested-VM acceptance remains pending under ticket #41.
No command may report success for a stubbed VM.

Examples shared by Bash and Fish (diagnostics only):

```sh
./lab-vm --help
./lab-vm context --json --name candidate
./lab-vm list --json
./lab-vm setup
```

`./lab-vm base` is deliberately omitted from the diagnostic examples because it
downloads the pinned image into private state. It never boots a VM or replaces a
cache member.

### Approved target trust boundary (not yet runtime acceptance)

The agent runs **directly in a guest VM**, with guest administration authority.
Guest Docker is for project workloads, not an agent wrapper or host security
boundary. Only the chosen owned workspace is exported. Host state, credentials,
Docker socket and other paths are not shared. QEMU restricted user networking
and a public-only HTTP(S) proxy deny host/LAN/private destinations; there is no
host firewall/routing fallback. Optional model/metrics/auth crossings are narrow
and explicitly selected. Daily suspend/resume preserves state; deletion is scoped
and separately acknowledged.

Self-development must modify Little Mester including VM code, freeze a filtered
revision-bound candidate, provision fresh nested VMs **from that changed code**,
test failure→fix→fresh pass, and preserve parent usability. Additional agents in
one guest are not equivalent evidence. Recursion policy is enforced by supported
launchers, not a tamper-proof restriction on a guest administrator. Real KVM,
workload, network, credentials and human TUI/auth evidence remain required.

### C10 recursion and candidate-source boundary (fixture milestone)

The selected self-write workspace is copied into a private per-run candidate
snapshot. Its canonical manifest binds the source Git HEAD and current tracked,
dirty and untracked content while excluding credentials, state, databases, model
weights, caches and `.git` history. File modes and safe in-scope symlinks are
preserved; escaping/excluded/dangling links, special files, source races, changed
source for an existing run and modified retained snapshots refuse. Copy limits and
free-space reserve are checked before staging, and publication is no-clobber.

Nested launch identity is loaded from the guest's private persisted depth/maximum;
environment conflicts fail closed. Nested launches require `--recurse`, an
explicit non-increasing `--max-depth` (0–32), and usable supported-profile KVM plus
guest CPU virtualization flags before child state or snapshot creation. The private
candidate launcher and workspace are verified against the retained manifest, and
the prepared child argv points only to that snapshot with a clean, fixed environment
and a separate per-run state root. `recursion-clean RUN` requires an exact run-name
confirmation or explicit `--yes`, refuses live/unknown processes and retained
descendant runs, and preserves report archives. These are fixture-tested boundaries;
they do not claim a real child VM boot, changed seed/bootstrap execution, KVM
availability from `/dev/kvm` inspection, or root-proof recursion enforcement.

---

## Historical proposal (superseded; NOT current implementation instructions)

The remainder preserves the pre-replacement context until the final documentation
checkpoint. Its agent-container/rootless/default-launch/firewall-fallback claims
are superseded by the approved contract above and must not be used operationally.

> Decision: `docs/DECISIONS.md` §C.8. Tickets: epic #35, spike #40, provisioning #41,
> agent image #42, workspace Docker/egress #43, docs #44, model-agnostic harness #45.
> This page is the reviewer's one-page view; it is not a promise that every arrow is
> implemented yet. #41–#43 carry the acceptance evidence.

## 1. Shape

```
Host (your machine) — ./lab runs here as you, not as a service
┌──────────────────────────────────────────────────────────────────────────────┐
│  ./lab                                                                       │
│   ├─ check_workspace()            path guards run BEFORE anything is shared   │
│   ├─ qemu-system-x86_64          KVM, per-VM qcow2 overlay, cloud-init seed   │
│   ├─ virtiofsd                   exports ONLY the selected workspace          │
│   ├─ egress proxy (allowlist)    exposed at 10.0.2.100:3128 via guestfwd      │
│   └─ ollama / llamacpp           127.0.0.1:11434 / :8080 via guestfwd         │
│                                                                              │
│   ┌────────────────────────── disposable VM ───────────────────────────────┐ │
│   │  systemd                                                                │ │
│   │  rootless Docker daemon           user: mester                           │ │
│   │    unix:///run/user/<uid>/docker.sock                                   │ │
│   │                                                                         │ │
│   │  ┌──────────────── agent container ─────────────────┐                   │ │
│   │  │ opencode + docker CLI + compose v2               │                   │ │
│   │  │ /workspace  (virtiofs, rw)                       │                   │ │
│   │  │ docker.sock (only when Docker access is enabled) │                   │ │
│   │  └──────────────────────────────────────────────────┘                   │ │
│   │                                                                         │ │
│   │  ┌──────────── workspace containers (optional) ─────┐                   │ │
│   │  │ postgres, redis, ...                             │                   │ │
│   │  │ labelled, internal networks, no host ports       │                   │ │
│   │  └──────────────────────────────────────────────────┘                   │ │
│   └─────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────────┘
```

Only three kinds of traffic cross the VM boundary: the workspace (virtiofs), SSH control
(loopback-only host forward), and explicitly forwarded model/egress endpoints. There is
no general host route.

## 2. Crossings

| Crossing | Direction | What is exposed | What is *not* exposed |
|---|---|---|---|
| `virtiofsd` → guest `/workspace` | host → VM | Exactly the directory passed to `./lab`, read/write | `$HOME`, `~/.ssh`, `~/.config`, the lab repo without `--unsafe-recurse`, Docker sockets, other checkouts |
| SSH control | host → VM | `127.0.0.1:<ephemeral>` → guest `:22`, per-VM key, per-VM `known_hosts` | Host keys/credentials inside the VM; VM listening on the host LAN |
| Model API `guestfwd` | VM → host | ollama `:11434`, llamacpp `:8080` only | General host loopback, host filesystem, host Docker |
| Egress proxy `guestfwd` | VM → internet | HTTP(S) destinations allowed by `.mester-egress` | Unlisted domains, non-HTTP protocols (until added deliberately), raw host routing |
| Docker socket | agent container → VM daemon | Rootless daemon API, only when access is enabled | Host Docker daemon, rootful VM daemon by default |

## 3. Boundaries and what a compromise buys

| Compromised component | Can reach | Cannot reach (by design) |
|---|---|---|
| **Agent process / container** | `/workspace`, its own rootless containers, model APIs, allowlisted egress | Host filesystem outside the workspace, host loopback, host Docker, other VMs, VM root, host LAN |
| **A container started from the workspace** | `/workspace` as mounted, other containers on its Docker networks, allowlisted egress | Host, VM root, the agent container (unless explicitly networked), host ports |
| **The VM kernel / rootless daemon** | Other VM processes and the exported workspace | Host kernel, host files outside the export, host Docker, host credentials |
| **The host user** | Everything (they own the machine) | — the harness does not pretend to protect the host from its owner |

The workspace is the data boundary, the VM is the execution boundary, and rootless Docker
is the privilege boundary inside the VM. A compromised agent can corrupt the workspace it
was pointed at; that is the same authority the owner granted by choosing that directory.

## 4. Lifecycle

1. `./lab <dir>` validates the path on the host, resolves the pinned base image, and
   creates `.mester/vm/<name>/` (overlay, seed, keys, sockets; gitignored).
2. `qemu-system-x86_64` starts with KVM, the virtiofs device, SSH on a loopback ephemeral
   port, model/egress `guestfwd` entries, and `restrict=on`.
3. cloud-init creates `mester`, installs Docker/compose and configures rootless mode,
   proxy settings and the workspace mount.
4. `./lab` starts the agent container with the workspace mount and (optionally) the
   rootless Docker socket.
5. The agent runs `docker compose` for the workspace; containers are labelled with the
   workspace path and project name.
6. `./lab stop` runs the workspace/agent teardown, stops QEMU cleanly, and removes the
   overlay, seed and sockets. The base image stays cached; nothing workspace-specific does.

## 5. Deliberate non-goals

- Protecting the host from its owner, or from a malicious host kernel/driver.
- Running the agent on the host. The host runs QEMU/`virtiofsd`, not the agent.
- GPU passthrough into the VM. Models stay on the host behind a narrow API.
- Perfect workspace confidentiality from the VM: virtiofs is a shared filesystem by design.
- A remote shared Docker daemon as the default. Remote-target mode is #47 and is documented
  as weaker isolation.
- Alternative runtimes. They are evaluated in #46, not smuggled in here.

## 6. Open items to prove in implementation

- `restrict=on` must still allow the explicit `guestfwd` entries and SSH `hostfwd`; if it
  does not, the fallback is a host-side firewall + proxy with the same crossings, never a
  general host route.
- virtiofs mount/permission behaviour for UID-mapped rootless containers (#41/#43).
- Rootless Docker port and image limitations, with per-workspace fixes rather than a
  rootful default (#42/#43).
- DNS and non-HTTP egress: only the proxy path is allowed initially; anything else is a
  documented follow-up, not an accidental hole (#43).
- Remote host mode (#47) must repeat the same path guards, labels, teardown and egress
  guarantees, and state its weaker host-sharing model in `SECURITY.md`.
