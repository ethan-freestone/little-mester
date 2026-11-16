# OpenCode runtime compatibility and promotion contract

## Approved Phase 1 baseline

- **Version:** `@opencode/cli@2.2.5`; acceptance requires exact output
  `opencode v2.2.5` from the protected guest runtime.
- **Binary identity:** install the pinned npm package during provisioning;
  `/usr/local/bin/opencode` is root-owned and mode `0755`. The guest launch
  contract checks the exact version and uses that protected executable. The
  current contract does not persist a separate binary digest; a future version
  promotion must record and verify the package/artifact digest as part of its
  reviewed evidence rather than treating a version string as byte identity.
- **Protected launch:** use the canonical launch assets in
  `lib/vm/parent-session-contract.py`. The TUI is started through the protected
  tmux launcher with a clean environment, fixed `HOME`, `PATH`, `TERM`, global
  `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `OPENCODE_CONFIG`, and
  `OPENCODE_DISABLE_PROJECT_CONFIG=1`. Keep the protected exit-confirm plugin
  and keybind policy intact; do not discover project plugins or config.
- **Terminal:** require the documented terminal capability and tmux runtime;
  TUI probes must use the actual pinned binary and a bounded PTY capture. SSH
  disconnection must not be treated as an OpenCode/VM shutdown request.
- **State:** keep user data under `/home/mester/.local/share/opencode` and the
  project/workspace under `/home/mester/workspace`; preserve the tmux socket
  under the protected OpenCode state directory. Auth, database/history,
  workspace, and persistent disk behavior must be verified on the same disk.
- **Updates:** no unattended rolling update. The owner controls the installed
  version; promotion requires a reviewed source change, exact artifact/package
  identity, protected-config/plugin and terminal tests, migration and
  persistence checks, credential-free hosted KVM regression, and explicit
  owner acceptance on an approved disk. Existing-disk upgrades require a
  separately approved, non-destructive migration/rollback procedure and must
  preserve disk identity and data.

## Forward compatibility roadmap (not implemented by Phase 1 M2)

Add a non-blocking latest-upstream compatibility lane that reports drift but
does not change production installs. Promote a new version only after a
deliberate review of CLI/auth/config/plugin/terminal changes, artifact identity,
state migration and rollback behavior, the complete local matrix, and hosted
KVM regression. New installations should default to the latest **approved and
tested** version, never an unreviewed rolling upstream version. Keep existing
disks on their current approved runtime until an owner-approved non-destructive
upgrade is explicitly performed.
