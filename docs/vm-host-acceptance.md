# VM host acceptance runbook

This is the owner-run procedure for TASK-15/C13. The runner is opt-in and
fail-closed: planning is read-only, live work needs staged `--start`/`--resume`
plus the exact `--confirm-run-id` and approved total-time budget, and cleanup is
a separate exact-run operation. A green
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

## 3. Provider choice and read-only plan

Provider discovery is optional and read-only. It queries only the fixed loopback
Ollama and llama.cpp model-list endpoints; it does not start a service, download
a model, read host auth, or retain responses:

```bash
python3 scripts/vm-host-acceptance.py --discover-providers
```

Only after reviewing that output, the owner may write an explicit credential-free
provider file outside both the checkout and state root. Include only providers
and exact model IDs the owner chose. The example below is a template; replace the
IDs with discovered/approved values. OpenAI uses browser login and has no endpoint
or credential field:

```bash
PROVIDER_CONFIG="$DATA_ROOT/little-mester-acceptance-$RUN_ID.providers.json"
( umask 077
  cat > "$PROVIDER_CONFIG" <<'JSON'
{"schema_version":1,"providers":[
  {"provider":"ollama","endpoint":"http://127.0.0.1:11434","model_id":"qwen3:8b"},
  {"provider":"llamacpp","endpoint":"http://127.0.0.1:8080","model_id":"coder-q4"},
  {"provider":"openai","model_id":"openai/gpt-4.1"}
]}
JSON
)
chmod 600 -- "$PROVIDER_CONFIG"
```

Never put credentials, API keys, prompts, or transcripts in this file. Keep its
exact bytes unchanged between stages; a changed config requires a fresh run. If
OpenAI or a local provider is not approved, omit it. Skipping OpenAI login blocks
only dependent OpenAI checks; available independent local-provider and
non-provider checks still run.

Run the plan again with the final suite/provider choice. A passing read-only plan
is not authorization. Record its checkout/config hashes, estimated disk budget,
requested effects, selected gates and the owner's explicit total-minute ceiling.
The filtered snapshot is capped at 2 GiB; preflight also calculates required
free disk from that cap and source size. Keep the disk budget it reports as the
campaign ceiling, and stop if the pinned image download or guest updates would
exceed separately approved storage/network limits. Provider probes are exactly
five short streams per selected provider; paid-provider use requires separate
owner approval. No unbounded prompts or automatic retry are permitted.

```bash
python3 scripts/vm-host-acceptance.py \
  --source "$SOURCE" --state-root "$STATE_ROOT" --run-id "$RUN_ID" \
  --memory-gib 8 --suite all --provider-config "$PROVIDER_CONFIG"
```

Use the same `--suite` and exact provider file for every stage. If no provider
file is selected, omit `--provider-config` consistently. Do not alter scope,
revision, memory, update authorization, or total budget after start.

## 4. Staged, exact-run campaign

Live execution is only after separate approval of this exact checkout, run ID,
provider choices, update effects, disk/download and paid-provider ceilings, and
total time. Set an explicit total budget in minutes (1–720); each automated stage
is independently capped at 60 minutes and consumes from the same total, including
the one allowed diagnosed fresh retry. The owner must keep every stage command's
budget identical. Interactive pauses do not restart a stage or replenish its
budget.

Start at `clean-usability` from the owner terminal. This creates the immutable
snapshot and run-owned private namespace and starts only the fresh campaign guest:

```bash
TOTAL_MINUTES=360  # replace with the exact owner-approved ceiling
python3 scripts/vm-host-acceptance.py \
  --source "$SOURCE" --state-root "$STATE_ROOT" --run-id "$RUN_ID" \
  --memory-gib 8 --suite all --provider-config "$PROVIDER_CONFIG" \
  --start --stage clean-usability --confirm-run-id "$RUN_ID" \
  --approved-total-minutes "$TOTAL_MINUTES"
```

Resume one stage at a time in this exact order. Do not skip a stage merely because
it is BLOCKED; later independent stages may still run. Never repeat a completed
stage with the same run ID:

```bash
python3 scripts/vm-host-acceptance.py --source "$SOURCE" --state-root "$STATE_ROOT" \
  --run-id "$RUN_ID" --memory-gib 8 --suite all --provider-config "$PROVIDER_CONFIG" \
  --resume --stage early-auth --confirm-run-id "$RUN_ID" \
  --approved-total-minutes "$TOTAL_MINUTES"

python3 scripts/vm-host-acceptance.py --source "$SOURCE" --state-root "$STATE_ROOT" \
  --run-id "$RUN_ID" --memory-gib 8 --suite all --provider-config "$PROVIDER_CONFIG" \
  --resume --stage providers-daily-permissions --confirm-run-id "$RUN_ID" \
  --approved-total-minutes "$TOTAL_MINUTES"

python3 scripts/vm-host-acceptance.py --source "$SOURCE" --state-root "$STATE_ROOT" \
  --run-id "$RUN_ID" --memory-gib 8 --suite all --provider-config "$PROVIDER_CONFIG" \
  --resume --stage workload-retention-lifecycle --confirm-run-id "$RUN_ID" \
  --approved-total-minutes "$TOTAL_MINUTES"

python3 scripts/vm-host-acceptance.py --source "$SOURCE" --state-root "$STATE_ROOT" \
  --run-id "$RUN_ID" --memory-gib 8 --suite all --provider-config "$PROVIDER_CONFIG" \
  --resume --stage nested-boundaries-agents --confirm-run-id "$RUN_ID" \
  --approved-total-minutes "$TOTAL_MINUTES"

python3 scripts/vm-host-acceptance.py --source "$SOURCE" --state-root "$STATE_ROOT" \
  --run-id "$RUN_ID" --memory-gib 8 --suite all --provider-config "$PROVIDER_CONFIG" \
  --resume --stage final-auth --confirm-run-id "$RUN_ID" \
  --approved-total-minutes "$TOTAL_MINUTES"
```

Do **not** clean up yet. Record the selected owner-observed gates in §5, then run
the exact `--cleanup` teardown in §6. Teardown uses the same remaining total
budget and 60-minute stage cap, preserves the sanitized report outside the
deleted run namespace, and marks teardown PASS only after residue checks succeed.

`early-auth` asks whether to open the optional OpenAI browser bridge. Answer `n`
or press Enter to skip; this does not block local-provider checks. A `y` opens the
campaign-owned guest agent so the owner signs in directly in the host browser.
Never paste credentials into chat or the terminal. The bridge has a bounded wait
and records only completion booleans/exit codes.

At a pause, use status with the exact state root and ID. It prints sanitized
stage/gate states, elapsed/remaining approved minutes and report path:

```bash
python3 scripts/vm-host-acceptance.py \
  --state-root "$STATE_ROOT" --run-id "$RUN_ID" --status
```

If a command is interrupted, preserve the report and state. Resume only if status
shows the prior stage completed (PASS, BLOCKED or FAIL), then run the next stage.
A still-RUNNING/ambiguous stage is not safely resumable; stop for owner review.
After that review, use only exact-run teardown; it independently checks every
manifested profile and preserves any unknown, provisioning, or unresponsive
state rather than guessing.
Resume rejects changed checkout/config, profile/listener identity, suite, update
effects or budget, and serializes stage/cleanup mutations. A second exact-revision
attempt is allowed only after the first attempt's exact teardown succeeded. Keep
the **same private `STATE_ROOT` and exact provider-config file/bytes** so the
attempt ledger and budget accounting persist; do not create a new state root based
on the retry ID. Use the same checkout, suites, memory, update authorization and
total-minute ceiling, with a new run ID:

```bash
PREVIOUS_RUN_ID="$RUN_ID"
RETRY_RUN_ID="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
RUN_ID="$RETRY_RUN_ID"  # keep STATE_ROOT and PROVIDER_CONFIG unchanged
# Set this to a diagnosed reason_code present in the prior sanitized report.
RETRY_REASON="provider_stream_incomplete"
python3 scripts/vm-host-acceptance.py \
  --source "$SOURCE" --state-root "$STATE_ROOT" --run-id "$RETRY_RUN_ID" \
  --memory-gib 8 --suite all --provider-config "$PROVIDER_CONFIG" \
  --start --stage clean-usability --confirm-run-id "$RETRY_RUN_ID" \
  --approved-total-minutes "$TOTAL_MINUTES" \
  --retry-of "$PREVIOUS_RUN_ID" --retry-reason "$RETRY_REASON"
```

The example reason is illustrative only: use it only if that exact reason code
appears in the prior report and the cause was diagnosed. Do not change provider
configuration bytes (or switch between configured and omitted config) for a
retry. A changed checkout or provider config is a different revision and is not a
retry. The second attempt's elapsed stages are added to the first attempt's
elapsed stages; the originally approved total is not replenished. A third
attempt for the same source/config revision is refused. If the first attempt
omitted `--provider-config`, omit that flag on the retry and every stage command;
do not pass an empty/unset config path. Add `--apply-selected-updates` only if the
same effect was included in the original plan and every stage.

Fish uses `set` and command substitution instead of Bash assignments. Example
initialization and start (use the same stage names/options above for each resume):

```fish
set SOURCE (pwd -P)
set RUN_ID (python3 -c 'import uuid; print(uuid.uuid4().hex)')
if set -q XDG_DATA_HOME
  set DATA_ROOT (realpath -e "$XDG_DATA_HOME")
else
  set DATA_ROOT (realpath -e "$HOME/.local/share")
end
set STATE_ROOT "$DATA_ROOT/little-mester-acceptance-$RUN_ID"
set TOTAL_MINUTES 360
install -d -m 700 -- "$STATE_ROOT"
chmod 700 -- "$STATE_ROOT"
python3 scripts/vm-host-acceptance.py --discover-providers
set PROVIDER_CONFIG "$DATA_ROOT/little-mester-acceptance-$RUN_ID.providers.json"
umask 077
printf '%s\n' '{"schema_version":1,"providers":[{"provider":"ollama","endpoint":"http://127.0.0.1:11434","model_id":"OWNER_SELECTED_MODEL"}]}' > "$PROVIDER_CONFIG"
chmod 600 -- "$PROVIDER_CONFIG"
python3 scripts/vm-host-acceptance.py --source "$SOURCE" --state-root "$STATE_ROOT" \
  --run-id "$RUN_ID" --memory-gib 8 --suite all --provider-config "$PROVIDER_CONFIG"
python3 scripts/vm-host-acceptance.py --source "$SOURCE" --state-root "$STATE_ROOT" \
  --run-id "$RUN_ID" --memory-gib 8 --suite all --provider-config "$PROVIDER_CONFIG" \
  --start --stage clean-usability --confirm-run-id "$RUN_ID" \
  --approved-total-minutes "$TOTAL_MINUTES"

python3 scripts/vm-host-acceptance.py --source "$SOURCE" --state-root "$STATE_ROOT" \
  --run-id "$RUN_ID" --memory-gib 8 --suite all --provider-config "$PROVIDER_CONFIG" \
  --resume --stage early-auth --confirm-run-id "$RUN_ID" \
  --approved-total-minutes "$TOTAL_MINUTES"
python3 scripts/vm-host-acceptance.py --source "$SOURCE" --state-root "$STATE_ROOT" \
  --run-id "$RUN_ID" --memory-gib 8 --suite all --provider-config "$PROVIDER_CONFIG" \
  --resume --stage providers-daily-permissions --confirm-run-id "$RUN_ID" \
  --approved-total-minutes "$TOTAL_MINUTES"
python3 scripts/vm-host-acceptance.py --source "$SOURCE" --state-root "$STATE_ROOT" \
  --run-id "$RUN_ID" --memory-gib 8 --suite all --provider-config "$PROVIDER_CONFIG" \
  --resume --stage workload-retention-lifecycle --confirm-run-id "$RUN_ID" \
  --approved-total-minutes "$TOTAL_MINUTES"
python3 scripts/vm-host-acceptance.py --source "$SOURCE" --state-root "$STATE_ROOT" \
  --run-id "$RUN_ID" --memory-gib 8 --suite all --provider-config "$PROVIDER_CONFIG" \
  --resume --stage nested-boundaries-agents --confirm-run-id "$RUN_ID" \
  --approved-total-minutes "$TOTAL_MINUTES"
python3 scripts/vm-host-acceptance.py --source "$SOURCE" --state-root "$STATE_ROOT" \
  --run-id "$RUN_ID" --memory-gib 8 --suite all --provider-config "$PROVIDER_CONFIG" \
  --resume --stage final-auth --confirm-run-id "$RUN_ID" \
  --approved-total-minutes "$TOTAL_MINUTES"
```

Use only owner-selected providers/models in the Fish JSON. If no provider is
approved, omit `--provider-config` from the Fish plan and every stage command.
Do not use `--execute` for the owner campaign; staged start/resume is the
reviewed path.

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

## 5. Complete owner-observed gates

The report is stored in a private sibling `reports/` directory; use the exact
`report_path` printed by the runner. The current verified guest OpenCode version
is in `verified_guest_opencode_version` (initially also recorded in
`HOST-CLEAN.guest_opencode_version`). Use that exact version below, especially
if the approved lifecycle update changed it. A PASS
requires every listed checklist ID for that gate. Record only IDs you actually
completed; never put prompts, response text, URLs, tokens, screenshots, or
free-form command output into acceptance evidence.

The sanitized status command is:

```bash
python3 scripts/vm-host-acceptance.py \
  --state-root "$STATE_ROOT" --run-id "$RUN_ID" --status
```

For recording, use `--record-observation`, then add:
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
the report. Complete every selected owner checklist **after the staged checks and
before exact teardown**. In particular, `HUMAN-AUTH` must be recorded before
teardown; recording it after `final-auth` updates that stage from BLOCKED to PASS
when the complete auth checklist passes. Once cleanup is PASS, the report is
final and owner observations are refused.

### Gate → procedure → evidence → cleanup matrix

The report is the machine-evidence ledger; the owner records only fixed checklist
IDs. The stage runner never promotes an owner action or a health check into a
behavioral PASS.

| Gate | Procedure and pass condition | Safe evidence | Cleanup / stop condition |
|---|---|---|---|
| `HOST-PREFLIGHT` | Read-only Linux/KVM, memory, disk, tool, source/config hash, ownership and collision checks. | Fixed check IDs/statuses and resource counts only. | No VM is created. Any blocked required check stops fresh start. |
| `HOST-CLEAN` | Start the exact fresh profile; verify pinned image integrity, clean cloud-init, virtiofs workspace UID/GID, guest Docker, Node, MCP files and exact OpenCode version. | Profile/runtime/image/payload hashes and boolean check map. | Keep only this run's profile; use exact cleanup on any failed start. |
| `E2E-DAILY` | Runner performs repeated start/warm identity and A→B→B same-overlay/workspace switch. Owner enters native TUI and tests `lab-docker` rollback without recreating the host Docker session. | Runtime identity, overlay/key digests, workspace tree hashes, checklist IDs. | Stop if `lab-docker` is unavailable or rollback would recreate/alter the working Docker container. |
| `E2E-MODEL` | Five real bounded streams per explicitly selected available provider; separately observe model-none shell/auth and provider-denial/missing-credential errors. | Provider enum, attempted/passed counts, schema boolean; no prompt or response text. | Provider profiles are run-owned and suspended after checks. Missing login/service/model blocks only dependent checks. |
| `E2E-WORK` | Runner uses only preloaded `postgres:16-alpine` inside guest Compose, checks health/query, bind canary, restart persistence, no published container service, guest-loopback binding, VM suspend/resume DB canary, then removes exact volume/path. Owner performs failing-test→fix→pass and verifies no host socket/service. | Boolean Compose/query/bind/retention/cleanup checks and the fixed owner checklist. | The runner refuses image pulls; exact Compose project and generated workspace path only. Stop if cleanup cannot confirm volume/path removal. |
| `E2E-LIFE` | Runner checks suspend/resume identity, update scan/cancel, separately authorized exact `config`/`tools` apply, resize and memory identity. Owner verifies auth/DB/tools/key/workspace canaries, child-blocks-parent, deepest-first stop, teardown and preserved base/reports. | Exit/status enums, version, RAM readings, profile/overlay/key/workspace digests. | Do not apply updates without exact plan+execution approval. Leave ambiguous children or auth state intact for owner review. |
| `E2E-AGENTS` | Two owner-authorized same-guest harmless tasks run concurrently in separate terminals; verify distinct session/auth ports, clean cancellation, and discuss shared-workspace collision risk. | Four checklist IDs and observed booleans only; never save agent transcripts. | Stop both exact tasks normally; do not claim file isolation, a supervisor or safe shared-workspace concurrency. |
| `SEC-FS` | Use a synthetic host canary outside the shared workspace. From the guest, test only readability of that path, exact profile control-key/socket paths, a nested child's out-of-scope path, and a workspace symlink aimed at the synthetic canary. Confirm source/state overlap is rejected by preflight. Verify the single export path only under separate disposable-auth approval. | PASS/FAIL booleans/checklist IDs; never the canary value, private key, socket data or exported credential. | Remove only the generated symlink/canary after the guest probes. Never use real host files/credentials; leave the gate BLOCKED if a safe child/export probe is unavailable. |
| `SEC-NET` | Runner performs actual boundary probes from campaign depth 0, self-test G1 and its actual nested G2 child; review fixed 12-probe reports and exact 0→1→2 topology. | Probe IDs/statuses and report digests only. | The self-test deletes only manifested children deepest-first. Missing G2 report, ancestry, or cleanup is BLOCKED/FAIL—not inferred from a depth label. |
| `SEC-SECRET` | With disposable synthetic data only, confirm a canary is absent from filtered snapshot/history, seed/base/log/report, verify provider forwarding is opt-in, and—only after separate approval—exercise atomic export/import, failure-before-delete, and refreshed-state persistence. | Booleans and digests; no token, auth-store dump, transcript or canary text. | Never delete the only credential copy. Without explicit disposable-credential/export approval, keep the credential-dependent checklist blocked. |
| `HUMAN-TUI` | One terminal: native entry, warm start without reboot/install, metrics sidebar/tool, Ctrl-C cancel/confirm, retry rendering, exact version. | Complete fixed checklist IDs and observed version. | Exit normally; no screenshots containing secrets. |
| `HUMAN-PERM` | Exercise actual auto-allowed, auto-denied, ask prompt, shared/standalone parity and argv/UI action record in the disposable guest. | Complete fixed checklist IDs and observed version; no free-form output. | Stop if a deny is weakened or an action exceeds the requested scope. |
| `HUMAN-AUTH` | Separate consent required. Owner completes browser success and cancel/interruption/timeout/occupied-port/egress-denied negatives, verifies auth-list completion and temporary callback removal, then refresh persistence and disposable delete/reimport/recursive import only where safe. | Completion booleans/checklist IDs only; never URLs, callback content, auth output or tokens. | Preserve the sole auth copy. Any ambiguous export/import or callback cleanup stops destructive actions. |
| `E2E-SELF` | TASK-13 runs the changed candidate lifecycle/bootstrap, expected failing A then fresh fixed B, harmless child task, source/runtime parity and parent continuity. | Archived sanitized self-test report/digest, stage enums, cleanup status. | Delete exact child profiles deepest-first. Failed cleanup keeps the gate blocked and preserves state. |
| `E2E-DEPTH` | Review actual G1→G2 nesting and G3 refusal before state/QEMU, plus exact persisted depth and parent chain. | Nested profile names/depths, refusal booleans, lifecycle/runtime hashes and G2 boundary digest. | Never substitute sibling profiles or labels. Cleanup only through the exact ownership manifest. |

For a safe guest-only workload agent check, use the already-ready campaign profile
and an explicit disposable task. Do not point Compose at the host Docker socket:

```bash
MESTER_STATE_DIR="$CAMPAIGN_STATE" ./lab-vm agent \
  --name "$PROFILE" --permissions ask -- \
  'In the disposable workspace only, create a tiny test that fails, observe the failure, make the smallest fix, rerun it, and report only PASS/FAIL. Do not read credentials, inspect files outside /workspace, or publish a service.'
```

For `E2E-AGENTS`, start two separately authorized commands like the following in
two terminals at the same time, changing each task to a different harmless file.
If the implementation does not expose a safe way to verify distinct session/auth
ports, record that subcheck BLOCKED rather than guessing:

```bash
MESTER_STATE_DIR="$CAMPAIGN_STATE" ./lab-vm agent \
  --name "$PROFILE" --permissions ask -- \
  'Create and run one harmless test in agent-a/ only. Do not read secrets or edit agent-b/.'
```

For `SEC-FS`/`SEC-SECRET`, use an unguessable synthetic canary created outside the
checkout/workspace under the private campaign state, mode `0600`. Ask the guest
only whether the exact path is readable; require a boolean answer and do not ask
it to print file contents. Place an exact canary symlink inside the disposable
workspace only for the escaping-link negative, then remove that link after the
check. **This repository does not currently ship a canary-scanning/redaction
checker.** That checker is owner-supplied and must be reviewed before it reads the
canary privately and scans only the approved snapshot/history/seed/base/log/report
paths while emitting only `ABSENT/PRESENT`. Until a reviewed checker is available,
keep the canary-absence subchecks and `SEC-SECRET` BLOCKED; do not substitute
generic `grep` output, which could disclose the canary. The automated report's
`state_root_disjoint_source` and `snapshot_disk_budget` checks are the evidence
for overlap and disk guards. A real credential export/delete/reimport is not
part of this safe synthetic procedure; it requires separate owner authorization
and a disposable credential profile. If that checker or a safe provider
operation is unavailable, the affected gate remains BLOCKED.

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

The generated campaign state uses the validated model-none default and never
imports provider settings or credentials from the host's ordinary Little Mester
state. The optional private provider file explicitly configures only owner-selected
providers/models; it contains no auth. Do not repoint the campaign at ordinary
state or copy auth data to force a model test. With no configured provider, actual
configured-provider streams and model-dependent agent work remain BLOCKED; report
only model-none behavior that was actually observed.

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

## 6. Review and exact cleanup

Review every selected gate and the sanitized report before cleanup. Keep a copy
of the report path and final status in the approved evidence location. If any
profile is ambiguous, unresponsive, or not proven to belong to this run, stop;
do not signal PIDs or remove state manually.

Clean only the exact run:

```bash
python3 scripts/vm-host-acceptance.py \
  --state-root "$STATE_ROOT" --run-id "$RUN_ID" \
  --cleanup --confirm-run-id "$RUN_ID" \
  --approved-total-minutes "$TOTAL_MINUTES"
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
