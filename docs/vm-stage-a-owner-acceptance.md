# Stage A parent VM owner acceptance

## Current status

- **Scope:** make the fixed systemd-managed v3 parent usable for normal work on one
  explicitly selected host directory. This is not child-VM/self-testing work.
- **Baseline:** `497a70ca131cc622f661c52d08f10baf6ec0e4d2` on
  `fix/vm-stage-a-owner-acceptance`.
- **Protected state:** no host installation, service, VM, disk, key, credential, or
  authentication state is changed by this source branch. Deployment remains a separate
  owner action.
- **Lifecycle scope:** this branch intentionally retains one named `system` VM with the
  existing disk, keys, fixed unit, and ports. It does not add profile selection or
  multiplex multiple identities; commands must continue to target that one fixed system
  instance. Supporting multiple profiles would require a separate identity/storage design
  and explicit owner approval.
- **Still unresolved:** real host virtiofs/metrics runtime proof in hosted KVM and owner
  interactive acceptance. Local Ollama/llama.cpp forwarding, old-VM retirement, and Stage B
  cleanup remain out of scope.
- **Decision:** keep the fixed `little-mester-vm.service` and its current disk identity;
  add one owner-scoped workspace service. Do not introduce profile flags or infer an
  assignment from the current directory.

## Read-only audit: observed facts and unresolved reports

| Area | Verified from the baseline source | Not established by source inspection |
|---|---|---|
| Workspace | The fixed system VM requires an explicit owner-selected directory, served by a sandboxed `virtiofsd` unit, then mounted/probed at guest `/workspace`. | Real virtiofs confinement and QEMU connection require hosted KVM; this development host lacks `virtiofsd`. |
| Lifecycle | `lab vm` / `lab-vm system` operations are scoped to the one fixed v3 system instance; workspace assignment is independent of its identity. | Multiple named profiles are deliberately unsupported; no current-directory or workspace-derived profile is permitted. |
| OAuth | The bounded browser bridge validates the OpenAI URL and exact localhost callback, uses pinned SSH, and attempts the desktop portal before manual URL display. | The owner-reported portal failure stage; there is no live browser/ChatGPT test in this development session. |
| Clipboard | A fixed `wl-copy`/`xclip`/`xsel` fallback is attempted only after authorization URL validation and portal fallback. | Whether Wayland/X11 clipboard tools are installed on the owner session. |
| Settings | The protected launcher composes the theme into the pinned 2.2.5 CLI schema and passes the allowlisted model only through a separate root-protected server config; other config remains unavailable. | Which individual OpenCode settings UI writes were attempted by the owner. |
| Metrics | The v3 guest loads a protected display plugin and reads the bounded host snapshot from a loopback-only sidecar over a fixed QEMU guest-forward; the legacy parent keeps its old plugin set. | Real GPU/sensor values and the pinned guest plugin behavior still need hosted-KVM/owner acceptance. |
| Exit dialog | The plugin declares Esc/N cancellation and Y/Q confirmation in modal mode; pinned-binary CI now drives Esc, N, and Y in a real PTY. | The test has not run locally because pinned OpenCode 2.2.5 is not installed here. |
| Version | Provisioning and readiness intentionally require `opencode v2.2.5`. | Whether a later pinned version is necessary; no version change is assumed. |
| Install/update | Fresh install remains explicit and refuses existing state. Explicit `--update-code-only` refuses running services and verifies disk, seed, keys, assignment, and drop-in state fingerprints. | The updater has not been exercised on an installed owner VM; guest-state migration remains separate. |

## Workspace architecture decision

### CLI contract

```text
./lab vm up --dir PATH [--unsafe-recurse] [--reassign]
./lab vm tui
./lab vm ssh
./lab vm status
./lab vm down
```

`up` requires one explicit directory unless the fixed VM already has an assignment.
`tui`, `ssh`, and `status` use that persisted assignment. `--reassign` is required to
change an existing assignment. Reassignment is refused while the VM is active; the old
workspace share must be stopped and verified before the new path is installed. `down`
stops only the fixed v3 system VM and its workspace service and preserves the disk,
credentials, OpenCode history, host keys, and assignment. It does not destroy anything.

The selected path is resolved before any privileged operation. The owner-facing
validator rejects filesystem root, the complete home directory, protected installation
and state roots, non-directories, unsafe ownership, and nested mountpoints. Symlinks in
the *argument* are resolved to a canonical directory and the resolved target is checked;
the canonical device/inode is checked again immediately before sharing. The repository
root (or a directory containing it) is refused by default. `--unsafe-recurse` permits
the repository itself or a directory within it, but never permits exporting a parent
directory that also contains unrelated host paths.

The guest sees the one selected directory read/write at `/workspace`. There is no
fallback to `/`, `$HOME`, the current directory, the project repository, or the guest's
internal `/home/mester/workspace`. Guest rootless Docker may bind paths inside the guest,
but the host has exported no other host path or Docker socket.
The fixed guest UID/GID 1000 is translated bidirectionally to the invoking host owner's
UID and primary GID by `virtiofsd`; host file modes remain unchanged, and the guest does
not receive access by broadening host permissions.

### Service and socket boundary

Keep QEMU under the dedicated `lm-vm` system service identity. Add a separate systemd
workspace service running `virtiofsd` as the invoking owner, not as root and not as
`lm-vm`. The service receives the selected source only through one systemd bind mount at
`/workspace`, with `ProtectHome=yes`, `ProtectSystem=strict`, `ReadWritePaths=/workspace`,
private temp, and a `NoExecPaths=/` execution boundary. `NoNewPrivileges` is disabled
only because virtiofsd 1.10 creates its user namespace through the setuid
`newuidmap`/`newgidmap` helpers. Sandboxing options that systemd uses to force
`NoNewPrivileges` (including `PrivateDevices`, `RestrictSUIDSGID`, and
`RestrictAddressFamilies`) are intentionally omitted; the daemon's own namespace
sandbox and the unit's filesystem namespace remain in force. `ExecPaths=` permits only
the system executable/library trees needed by Python, virtiofsd, its loader, and mapping
helpers; these trees must remain root-controlled.
Pass the explicit virtiofsd namespace sandbox option; do not enable submount announcing.

The root installer creates a dedicated runtime socket directory owned by the workspace
owner and group `lm-vm-control`, with mode `0750`; the workspace service starts with the
owner's primary group so virtiofsd's supported user-namespace ID maps work, then its root
`ExecStartPost` normalizes the socket directory and socket to `lm-vm-control`. The service creates the
socket there with group `lm-vm-control` and mode `0660`. The directory is not writable by
the QEMU group, so QEMU can connect but cannot replace the socket. The installed account
policy must limit `lm-vm-control` to the owner and `lm-vm`. The socket path and every
ancestor are checked for type, owner/group, mode, and symlinks before QEMU starts. No
socket is placed under the owner's `0700` `/run/user/<uid>` directory, and no service
opens a TCP listener for filesystem access.

The VM unit requires and follows the workspace unit; the workspace unit is part of the
VM unit and stops after QEMU. This gives startup order `validated assignment → share
socket ready → QEMU → guest cloud-init/mount/readiness`, and stop order `QMP guest
shutdown → QEMU exit → virtiofsd stop/socket removal`. If QEMU shutdown fails, the share
is retained until QEMU exit is proven. `up` confirms `/workspace` is a virtiofs mount and
performs a nonce-based host→guest read and guest→host write probe, removing only its own
temporary marker by identity. `status` reports the canonical host path and
`/workspace`; it never reports credentials.

Systemd's dynamic `BindPaths=` value must be generated only by the fixed root-owned
workspace configurator after validating the invoking UID, canonical path, repository
opt-in, and mount table. It must be escaped as a systemd unit value, never interpolated
into a shell command. The serving process checks the assigned source inode through the
visible bind mount and rejects nested mounts in that same sandbox before executing
virtiofsd. Tests must cover spaces and systemd-special characters in paths,
symlink argument resolution, nested bind-mount rejection, source replacement between
validation/start, and escape-symlink reads from the guest. If any of these cannot be
proved on the supported virtiofsd/systemd versions, sharing fails closed.

### Repository self-development

`--unsafe-recurse` is a specific authorization to share this repository directory; it
does not disable canonicalization, ownership, mount-boundary, socket, proxy, or guest
security checks. The candidate launcher and protected implementation remain installed
outside `/workspace`. Editing source in the selected repository does not deploy it to or
restart the currently running parent VM.

## Other Stage A implementation decisions

- **Preferences:** retain immutable root-owned security configuration. Store a
  mode-0600 user preference document with a strict allowlist (model and theme only). The protected launcher validates and composes it with
  security policy; user config cannot add plugins, alter permissions, change networking,
  enable project config, or replace protected settings.
- **OAuth:** preserve the bounded SSH/callback design and exact localhost callback
  allowlist. Add ordered host browser/portal handling, manual full-URL presentation, and
  Wayland/X11 clipboard fallback only after URL validation. Never place URLs, codes, or
  tokens in CI logs. Report sanitized stage identifiers for URL generation, presentation,
  browser handoff, callback, provider exchange, and credential persistence.
- **Metrics:** reuse the repository's bounded schema/collector. The guest gets one fixed
  read-only host metrics endpoint over an explicit QEMU `guestfwd` mapping to a
  host-loopback listener; only fixed GET paths and bounded JSON responses are accepted.
  This is a new host→guest data channel and must be documented, loopback-only, absent
  from generic proxy policy, and tested for rejection of arbitrary paths/methods/hosts.
  Every display labels source as host; missing sensors/channel render an explicit
  unavailable state, never guest metrics labelled as host.
- **TUI exit:** preserve the custom confirmation policy. Exercise Ctrl+C, Esc, N, Y, Q,
  prompt typing, repeated keys, tmux reconnect, and cancellation using the installed
  pinned binary and a PTY; static key-binding checks are not acceptance.
- **Version:** retain OpenCode 2.2.5. Test the actual installed guest binary against
  plugin APIs, settings behavior, auth workflow, and model selection before considering
  a separately reviewed pin change.
- **Update/migration:** do not reimage or clone the existing disk. A code-only candidate
  update must refuse a running VM, stage and replace protected host code/unit assets from
  the versioned candidate, reload systemd without starting/restarting services, and prove disk,
  keys, credentials, preferences, history, and workspace assignment identities unchanged.
  Guest-state migration is explicit and owner-approved; this branch never performs it.

## Acceptance levels

1. **Infrastructure automated acceptance:** existing pinned install, host policy,
   network, guest readiness, SSH, QMP, and cleanup gates.
2. **Non-destructive install acceptance:** verifies candidate files, permissions,
   service/account/group configuration, and stopped/running state without starting,
   stopping, replacing, or reimaging an owner VM.
3. **Disposable hosted-KVM integration:** creates a uniquely named CI-owned workspace and
   VM state, proves the exact host directory is mounted read/write, proves a sibling
   sentinel and an escape symlink are unreadable, exercises OAuth mocks, preferences,
   metrics transport, PTY keys, same-disk persistence and shutdown, then verifies all
   test-owned services/processes/sockets are stopped and cleans only test-owned files.
4. **Owner interactive acceptance:** real browser/ChatGPT Plus auth and response, copied
   URL fallback, visible host metrics, settings changes, Esc cancellation, daily workspace
   edits, `down`, reconnect, and restart. These remain owner-only and must not be reported
   as CI passes.

The development host has no `virtiofsd` executable, KVM device, or installed v3 service
account. No host packages will be installed to work around that. Pure/unit tests can run
locally; actual socket ownership, QEMU connection, guest mount, and out-of-scope access
must be demonstrated by the disposable hosted-KVM integration before owner deployment.
