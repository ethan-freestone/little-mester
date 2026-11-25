# OpenCode runtime compatibility and promotion contract

## Approved Phase 1 baseline

- **Version authority:** `lib/vm/opencode-version.py` declares the supported
  version (`2.2.5`), npm package name and exact executable output. Provisioning,
  compatibility inventory, protected readiness, CI and owner acceptance derive
  their expectations from this file. Historical reports and version-specific
  fixtures may retain the version they actually exercised.
- **Version evidence:** the expected supported version is policy, package JSON
  is package-reported evidence, and `opencode --version` is executable-reported
  evidence. A read-only guest inventory reports package metadata without
  executing guest code; the separate bounded runtime readiness/protection probe
  reports the executable result. Neither a matching version string nor package
  metadata claims byte identity.
- **Binary identity:** install the pinned npm package during provisioning;
  `/usr/local/bin/opencode` is root-owned and mode `0755`. The guest launch
  contract checks the exact version and uses that protected executable. The
  current contract does not persist a separate binary digest; a future version
  promotion must record and verify the package/artifact digest as part of its
  reviewed evidence rather than treating a version string as byte identity.
- **Package manager and development tools:** install Ubuntu's unversioned
  `nodejs`, `libnode-dev`, and `build-essential` packages without recommendations;
  leave the existing rootless-Docker prerequisite recommendations unchanged.
  Bootstrap npm from the moving registry `next-10` channel, accepting only a
  stable `10.x.y` release whose Node engine range matches the installed Node
  runtime. Require the canonical npm-registry tarball URL and verify its
  SHA-512 SRI before safe extraction; fail closed without a fallback if any
  check fails. The bundled `npm`, `npx`, and `node-gyp` commands retain global
  installation and native-addon development. This is a moving npm 10 channel,
  not a claim that npm 10 is upstream latest; reassess its major constraint
  when migrating the guest to a newer Ubuntu LTS. No exact npm patch is pinned.
- `agent/Dockerfile` retains a separate unpinned legacy Compose install; it is
  not the supported VM provisioning path and is deliberately unchanged here.
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

## Version-sensitive integration seams

These are the current reviewed boundaries, not a general compatibility
framework. They explain what a future version promotion must requalify:

- **Plugins and TUI:** exit-confirm uses OpenCode modal/keymap and dialog APIs;
  host metrics uses TUI slots, theme/OpenTUI behavior and a fixed metrics
  transport. Keep plugins under the protected global configuration and test
  them against the actual supported executable.
- **Authentication:** `browser-auth-session.py` validates the supported
  OpenCode authorization endpoint, query/client identity, PKCE and localhost
  callback variants. These are version-sensitive wire assumptions, not a
  promise of upstream stability; keep the fixtures and bridge changes together.
- **Settings and model selection:** Little Mester exposes only validated model
  and theme preferences. Theme is composed into the CLI config; the model is
  passed through a separate root-protected server config. Effective model
  listing and a real response are different evidence from accepting a stored
  preference.
- **Sessions and persistence:** tmux detach/reattach preserves a live process;
  after reboot the TUI is recreated, while auth, history and workspace
  persistence must be checked separately on the same disk.
- **Terminal:** TUI key handling, escape timing, resize and reconnect behavior
  cross OpenCode, tmux, PTY and SSH boundaries. Use the production-path terminal
  acceptance; local launcher fixtures alone do not qualify the guest path.

## Future compatibility and upgrade phases (not implemented here)

- **Phase 1 usability:** keep the single exact-version declaration, identify
  version-sensitive boundaries, provision reproducible supported guests, and
  report/validate expected, package-reported and executable-reported versions.
- **Phase 1 consolidation:** later remove unnecessary duplication and obsolete
  version-management mechanisms, and consolidate implementation/documentation
  where justified. This is a separate, owner-authorized workstream.
- **Phase 1.5 upgrades and rollback:** build one generic supported process for
  Little Mester runtimes and managed guest software; qualify an actual OpenCode
  upgrade from 2.2.5, including compatibility checks, config/persistent-data
  migrations, activation, failure recovery and rollback. Investigate whether a
  compatible OpenCode upgrade can be independent of an LM runtime upgrade. A
  reviewed LM SHA containing the supported-version declaration is an acceptable
  initial gate. Avoid a bespoke upgrade mechanism for every version pair.
  Illustrative commands such as `./lab vm versions`, `./lab vm upgrade --check`,
  `./lab vm upgrade --opencode`, `./lab vm upgrade --runtime` and
  `./lab vm rollback` are capabilities to explore, not settled CLI contracts.
- **Phase 2 disposable sibling VMs:** qualify new OpenCode versions in isolated
  sibling environments without replacing the known-good environment running
  the coordinating LM instance; capture real integration/acceptance evidence.
- **Phase 3 developer tooling and intelligence:** extend appropriate version
  management to MCP servers, developer tools and other managed guest software.
- **Phase 4 bounded autonomous correction:** let LM discover upstream changes,
  create a branch, update incompatible assumptions/tests and qualify a candidate
  in an isolated environment, then prepare reviewed publication/merge handoffs.
  Only after owner approval may the trusted host-side lifecycle activate the
  qualified change. Retain a recoverable known-good environment; an agent must
  not depend on remaining operational while replacing its own execution stack.

The long-term goal is routine, increasingly LM-assisted upgrades—not a moving
production `latest` install. OpenCode 2.2.5 remains the supported version for
this workstream; upgrades, rollback, sibling-VM orchestration and autonomous
correction are explicitly out of scope.
