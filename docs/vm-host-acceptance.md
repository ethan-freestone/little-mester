# VM host acceptance runbook

This is the owner-run procedure for TASK-15/C13. The runner is opt-in and
fail-closed: planning is read-only, live work needs `--execute` plus the exact
`--confirm-run-id`, and cleanup is a separate exact-run operation. A green
preflight is **not** acceptance. Do not use this runbook against a default or
existing VM, active snapshot, working host-Docker session, or real credentials
unless the exact campaign and any credential actions have been separately
reviewed and approved.

The C13 code checkpoint does not mean live acceptance passed. Automated reports
leave owner-observed gates blocked until their complete fixed checklist is
recorded. Never record a PASS for a test you did not personally complete and
verify.

## 1. Prepare a disposable scope

Use the reviewed replacement checkout and a private state root outside that
checkout. Do not choose an existing VM state directory or the historical
campaign's state. The state root must be a real, canonical directory with mode
`0700`.

```bash
cd /absolute/path/to/reviewed/feat-vm-approach-2
SOURCE="$(pwd -P)"
RUN_ID="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
DATA_ROOT="$(realpath -e "${XDG_DATA_HOME:-$HOME/.local/share}")"
STATE_ROOT="$DATA_ROOT/little-mester-acceptance-$RUN_ID"
install -d -m 700 -- "$STATE_ROOT"
chmod 700 -- "$STATE_ROOT"
```

Check that the checkout, branch, revision, and state-root location are the ones
approved for the campaign. Keep the generated `RUN_ID` for every later command.
If the directory already exists, stop and generate a new run ID; do not reuse or
delete it.

## 2. Read-only plan

Run the plan for the exact selected stages. `all` includes preflight, fresh
guest, daily lifecycle, model/workload/agent checkpoints, network/filesystem/
secret boundaries, lifecycle, human observations, and changed-VM recursion.
The preflight checks this execution context's Linux/KVM/tools/RAM/disk and exact
name collisions; it does not install packages or start a VM.

```bash
python3 scripts/vm-host-acceptance.py \
  --source "$SOURCE" --state-root "$STATE_ROOT" --run-id "$RUN_ID" \
  --memory-gib 8 --suite all
```

Review the full output. `BLOCKED` or `FAIL` means stop before execution. Confirm
the selected scope, exact checkout revision, budgets, protected assets, and
expected side effects with the campaign owner. A passing plan alone does not
authorize live mutation.

## 3. Explicit live campaign

Only after approval of this exact run ID and scope, run the campaign from a
terminal in which the owner can complete the interactive agent/human steps:

```bash
python3 scripts/vm-host-acceptance.py \
  --source "$SOURCE" --state-root "$STATE_ROOT" --run-id "$RUN_ID" \
  --memory-gib 8 --suite all \
  --execute --confirm-run-id "$RUN_ID"
```

The campaign creates one immutable filtered source snapshot, a private
run-owned state namespace, a fresh outer profile, and (for boundary/self tests)
bounded nested profiles. It never auto-selects an existing profile, installs
host packages, changes host networking, invokes host Docker, or prints raw
agent/model/auth output into its report. Guest boot may download the pinned
image and guest provisioning may use the approved network path. Resource or
capability failures remain BLOCKED; do not switch to TCG or broaden scope to
make them pass.

For owner-run checks while the campaign is still active, take the exact
`report_path` from the command's JSON summary. If the process was interrupted
before printing its summary, derive that one path from the state root and run
ID—do not search for another campaign:

```bash
RUN_NAME="accept-${RUN_ID:0:12}"
STATE_DIGEST="$(printf '%s' "$STATE_ROOT" | sha256sum | cut -c1-24)"
CAMPAIGN_BASE="$(dirname "$STATE_ROOT")/.little-mester-recursion-$STATE_DIGEST"
REPORT_PATH="$CAMPAIGN_BASE/reports/$RUN_NAME.json"
test -f "$REPORT_PATH" && test ! -L "$REPORT_PATH"
CAMPAIGN_STATE="$CAMPAIGN_BASE/$RUN_NAME/state"
PROFILE="${RUN_NAME}-outer"
MESTER_STATE_DIR="$CAMPAIGN_STATE" ./lab-vm status --name "$PROFILE"
```

The status command must show the exact generated profile ready. To perform an
interactive native-agent/TUI observation, run this in the terminal and exit
normally when finished:

```bash
MESTER_STATE_DIR="$CAMPAIGN_STATE" ./lab-vm agent --name "$PROFILE" --permissions ask
```

For a bounded harmless project task, add one explicit prompt after `--`, for example:

```bash
MESTER_STATE_DIR="$CAMPAIGN_STATE" ./lab-vm agent \
  --name "$PROFILE" --permissions ask -- \
  'In this disposable workspace, create and run one harmless test file, then report only whether it passed. Do not read credentials or edit files outside the workspace.'
```

Use only disposable/synthetic data. The agent's actual work and the required
human observation must be reviewed in the terminal; its response is not
automatically accepted as evidence. Do not use `run` or `up`, do not point the
state root at the normal Little Mester profile store, and do not infer that an
agent command starts or resumes a guest—it attaches only to the ready campaign
profile.

The default lifecycle suite scans and safely cancels an update when it is not a
no-op; it does not apply updates. If applying only the fixed guest `config` and
`tools` targets is separately approved, add `--apply-selected-updates` to both
the read-only plan and the execution command above, using the same fresh run ID.
The plan will list that effect; execution is still gated by the exact confirmation.
Do not run a second campaign with the same run ID. Include this optional side
effect in the one reviewed campaign scope. It must not be combined with a
different update target or used to update OpenCode, system packages, Docker, or
auth data.

## 4. Complete owner-observed gates

The report is stored in a private sibling `reports/` directory; use the exact
`report_path` printed by the runner. The current verified guest OpenCode version
is in `verified_guest_opencode_version` (initially also recorded in
`HOST-CLEAN.guest_opencode_version`). Use that exact version below, especially
if the approved lifecycle update changed it. A PASS
requires every listed checklist ID for that gate. Record only IDs you actually
completed; never put prompts, response text, URLs, tokens, screenshots, or
free-form command output into acceptance evidence.

The common command shape is:

```bash
python3 scripts/vm-host-acceptance.py \
  --state-root "$STATE_ROOT" --run-id "$RUN_ID" \
  --cleanup --confirm-run-id "$RUN_ID"
```

For recording, replace `--cleanup` with `--record-observation`, then add:
`--observation-gate GATE --observation-result PASS
--observed-version VERIFIED_VERSION` and repeat `--observation-check ID` for
each completed checklist item. For example:

```bash
python3 scripts/vm-host-acceptance.py \
  --state-root "$STATE_ROOT" --run-id "$RUN_ID" \
  --record-observation --confirm-run-id "$RUN_ID" \
  --observation-gate HUMAN-TUI --observation-result PASS \
  --observed-version 2.2.5 \
  --observation-check single-command-entry \
  --observation-check warm-start-no-reboot \
  --observation-check metrics-sidebar-and-tool \
  --observation-check ctrl-c-cancel-and-confirm \
  --observation-check retry-rendering \
  --observation-check version-matched
```

Replace `2.2.5` with the exact version verified for this run. The CLI rejects a
version mismatch, an unselected gate, an incomplete PASS checklist, a duplicate
observation, or an attempt to override an automated failure/prerequisite block.
For an unavailable or failed check, record `BLOCKED` or `FAIL` with only the
checklist IDs that were actually observed. Independent passing gates remain in
the report.

### Human gates

- **HUMAN-TUI:** `single-command-entry`, `warm-start-no-reboot`,
  `metrics-sidebar-and-tool`, `ctrl-c-cancel-and-confirm`, `retry-rendering`,
  `version-matched`. Use the same terminal for supported dialogs, metrics,
  cancel/confirm and retry. Do not save screenshots containing secrets.
- **HUMAN-PERM:** `auto-allowed-tool`, `auto-deny-preserved`, `ask-mode-prompt`,
  `shared-standalone-match`, `argv-ui-action-recorded`. Verify actual tool and
  deny behavior, not a mock or default answer.
- **HUMAN-AUTH:** `browser-success`,
  `cancel-interrupt-timeout-occupied-port-egress-denied`,
  `auth-list-and-completion`, `temporary-callback-removed`, `refresh-persisted`,
  `disposable-delete-reimport`, `recursive-provider-import`. This requires
  separate approval and disposable provider credentials. Never record OAuth
  URLs, tokens, callback contents, or raw auth-store output.

### Other owner-run behavioral gates

These are intentionally not inferred from guest health or a successful boot.
Perform the real behavior in the disposable campaign guest, then record its
complete checklist:

The generated campaign state currently uses the validated model-none default
and does not import provider settings or credentials from the host's ordinary
Little Mester state. Do not repoint it at that existing state or copy auth data
to force a model test. Until a reviewed provider-specific campaign configuration
is available, actual configured-provider streams and model-dependent agent work
must remain BLOCKED; report only model-none behavior that was actually observed.

- **E2E-DAILY:** `native-tui-entry`,
  `two-identical-starts-no-reboot-or-install`, `A-B-B-same-overlay-and-keys`,
  `warm-start-same-process-identity`, `docker-rollback-without-recreation`.
  The runner verifies warm identity and the A→B→B lifecycle path. Verify actual
  TUI entry and the supported Docker rollback path without recreating the host
  Docker container. If the separately staged `lab-docker` route is unavailable,
  leave this gate BLOCKED; an owner observation must not turn a missing rollback
  route into PASS.
- **E2E-MODEL:** `model-none-shell-and-auth`, `each-configured-provider-selected`,
  `five-real-streams-per-provider`, `provider-denial-and-missing-credential-errors`,
  `no-model-response-or-credential-recorded`. Run five actual streaming requests
  per configured provider, not health checks. If no provider is configured,
  verify model-none shell/auth only and do not claim configured-model coverage.
- **E2E-WORK:** `guest-only-postgres-compose`, `health-and-query`,
  `failing-test-fix-pass`, `volume-survives-stop-restart`, `compose-down`,
  `bind-and-guest-loopback-port`, `no-host-published-service-or-socket`. Keep
  Compose services and ports inside the guest; do not use host Docker or publish
  guest services on the host/LAN.
- **E2E-LIFE:** `suspend-resume-identity`,
  `auth-db-tools-keys-workspace-retained`, `update-scan-cancel-noop`,
  `selected-apply-exact-version`, `live-child-blocks-parent`,
  `deepest-first-shutdown`, `resize-memtotal-and-identity`,
  `exact-teardown-no-residue`, `base-workspace-reports-preserved`. The runner
  records suspend/resume, update scan/cancel, exact optional `config`/`tools`
  update, resize, and identity evidence. Verify the remaining real workload,
  auth, child-blocking, and teardown observations before recording PASS.
- **E2E-AGENTS:** `two-authorized-same-guest-tasks`, `separate-auth-ports`,
  `clean-agent-cancellation`, `shared-workspace-risk-explained-no-supervisor-claim`.
  Use two distinct authorized tasks; describe concurrent shared-workspace risk
  and do not claim a supervisor or file-isolation guarantee.
- **SEC-FS:** `host-file-unreadable`, `control-key-and-socket-unreadable`,
  `child-out-of-scope-files-unreadable`, `escaping-symlinks-refused`,
  `state-root-overlap-blocked`, `one-export-verified`. Use only a disposable
  canary and exact run-owned export; never test against real host credentials.
- **SEC-SECRET:** `canary-absent-from-snapshot-and-history`,
  `canary-absent-from-seed-base-log-report`, `provider-forwarding-opt-in-only`,
  `private-atomic-export-import`, `export-failure-prevents-delete`,
  `refreshed-state-survives-restart-and-import`. Use synthetic canaries and an
  explicitly approved disposable auth profile. If real disposable credentials
  or provider access are not authorized, leave this gate BLOCKED.

**E2E-SELF/E2E-DEPTH** run through the live TASK-13 adapter. The adapter needs a
ready exact parent profile, nested KVM, sufficient resource budget, and an
interactive harmless child-agent task. It verifies candidate lifecycle and
bootstrap mutations, failed-A/fixed-B boots, exact source/runtime hashes,
G1/G2/G3, refusal before state/QEMU, exact child cleanup, and parent continuity.
Unavailable model credentials or missing nested capabilities are BLOCKED, not
waived.

## 5. Review and exact cleanup

Review every selected gate and the sanitized report before cleanup. Keep a copy
of the report path and final status in the approved evidence location. If any
profile is ambiguous, unresponsive, or not proven to belong to this run, stop;
do not signal PIDs or remove state manually.

Clean only the exact run:

```bash
python3 scripts/vm-host-acceptance.py \
  --state-root "$STATE_ROOT" --run-id "$RUN_ID" \
  --cleanup --confirm-run-id "$RUN_ID"
```

Cleanup uses the ownership manifest and supported lifecycle operations,
gracefully suspending ready profiles deepest-first before exact deletion. It
preserves the report and rejects foreign/duplicate profile entries. Unknown,
unresponsive, or otherwise ambiguous ownership/state is preserved for owner
review. If cleanup fails, preserve the namespace and diagnostics; do not use
wildcards, raw PID signaling, `rm -rf`, or a different run ID.

Allow at most one fresh run ID retry for a diagnosed failure on the same
revision, within the approved time/resource budget. Record failures honestly.
Do not proceed to TASK-16, enable the VM default, or claim release acceptance
until all required live gates pass and are reviewed.
