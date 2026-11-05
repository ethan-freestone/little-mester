# VM host acceptance runbook

> **Local-first implementation checkpoint:** see the
> [local-first C14 handoff plan](vm-c14-handoff-plan.md). Local §1 code and
> fixture tests have been reconciled locally. No live campaign, VM, service,
> provider or credential operation has been authorized or run. Live OpenAI auth/paid
> use is deferred from merge/C14; automated auth regressions remain required.

This is the owner-run procedure for TASK-15/C13. The runner is opt-in and
fail-closed: planning is read-only, live work needs staged `--start`/`--resume`
plus the exact `--confirm-run-id` and approved total-time budget, and cleanup is
a separate exact-run operation. A green
preflight is **not** acceptance. Do not use this runbook against a default or
existing VM, active snapshot, working host-Docker session, or real credentials
unless the exact campaign and any credential actions have been separately
reviewed and approved.

The C13 code checkpoint does not mean live acceptance passed. Automated reports
leave required owner-observed gates BLOCKED until their complete fixed checklist
is recorded; deferred auth is recorded separately as DEFERRED. Never record a
PASS for a test you did not personally complete and verify.

## Local-first scope and explicit blockers

Workflow version 2 records a `local-first-c14-v1` scope in the private report.
`HUMAN-AUTH` and its live browser/refresh/delete/reimport checks are `DEFERRED`
with an explicit owner-approved reason; they are not required for the local-first
campaign result and are never promoted to PASS. The early/final live-auth stages
are likewise recorded as deferred. The normal `tests/test_vm_auth.py` and related
automated auth regressions remain required outside the campaign.

Only owner-selected Ollama/llama.cpp models are eligible for local real-stream
checks. OpenAI selections do not trigger campaign login, paid streams, refresh, or
credential roundtrips. A missing selected local model blocks `E2E-MODEL` only;
it does not change the deferred OpenAI result or turn a required non-auth gate
into PASS.

The repository-owned `scripts/vm-canary-check.py` reads a private per-run
synthetic canary and emits only fixed IDs/statuses/reason codes, bounded counts,
and artifact SHA-256 digests. It scans manifested regular files and trees,
decompresses loose Git objects, checks raw ISO9660 seed bytes, and scans selected
campaign logs/reports. Raw canary matches fail; missing, unsafe, oversized,
time-limited, or unsupported artifacts block. It does not emit canary values or
artifact contents. Exact generated canary fixtures are removed during exact-run
cleanup, and the preserved report is checked again after finalization.

**Known readiness blockers:** the checker does not inspect the virtual filesystem
inside QCOW2 base/overlay images; it checks raw bytes and reports
`qcow2_content_not_inspected`, so `SEC-SECRET` cannot pass on that evidence.
Guest/host filesystem isolation probes and the required synthetic private-state
atomic export/import, failure-before-delete, refreshed-state retention, and
provider-forwarding checks are also not yet implemented as campaign evidence and
remain BLOCKED. The canary scan alone is never sufficient for `SEC-SECRET` PASS.
No arbitrary host image is mounted or modified, and no host tools are installed
implicitly. These blockers need a reviewed minimal remedy and fixture/real evidence
before anyone can claim local-first readiness.

For nested suites, `HUMAN-TUI` is automatically a required gate; the exact-run
runner refuses the costly nested stage until the owner records a complete PASS
observation. Non-teardown stages leave a 35-minute portion of the approved total
budget reserved for exact teardown. The guest workload runner still requires
`postgres:16-alpine` preloaded and refuses pulls; if absent, stop BLOCKED. Any
guest-only image preparation needs separate approval for the exact OCI digest,
archive source, size limit, and disk reserve (see the handoff plan).

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

Only at separately approved target-host P1, provider discovery is optional and
read-only. It queries only the fixed loopback Ollama and llama.cpp model-list
endpoints; it does not start a service, download a model, read host auth, or
retain responses. Do not run it as part of this local implementation task:

```bash
python3 scripts/vm-host-acceptance.py --discover-providers
```

Only after reviewing separately authorized discovery output, the owner may write
an explicit credential-free provider file outside both the checkout and state
root. Include only locally available providers and exact model IDs the owner
chose. The values below are placeholders and must be replaced before use. Do not
select OpenAI for this local-first campaign:

```bash
PROVIDER_CONFIG="$DATA_ROOT/little-mester-acceptance-$RUN_ID.providers.json"
( umask 077
  cat > "$PROVIDER_CONFIG" <<'JSON'
{"schema_version":1,"providers":[
  {"provider":"ollama","endpoint":"http://127.0.0.1:11434","model_id":"OWNER_SELECTED_OLLAMA_MODEL"},
  {"provider":"llamacpp","endpoint":"http://127.0.0.1:8080","model_id":"OWNER_SELECTED_LLAMA_CPP_MODEL"}
]}
JSON
)
chmod 600 -- "$PROVIDER_CONFIG"
```

Never put credentials, API keys, prompts, or transcripts in this file. Keep its
exact bytes unchanged between stages; a changed config requires a fresh run. If
no local provider is approved, omit it. Any OpenAI checks remain visibly DEFERRED
and are not merge/C14 blockers; the runner will not initiate live OpenAI login,
paid streams, refresh, or credential roundtrips.

Run the plan again with the final suite/provider choice. A passing read-only plan
is not authorization. Record its checkout/config hashes, estimated disk budget,
requested effects, selected gates and the owner's explicit total-minute ceiling.
The filtered snapshot is capped at 2 GiB; preflight also calculates required
free disk from that cap and source size. Keep the disk budget it reports as the
campaign ceiling, and stop if the pinned image download or guest updates would
exceed separately approved storage/network limits. Local-provider acceptance is
five bounded streams per explicitly selected local provider. No paid-provider
requests, unbounded prompts, or automatic retry are permitted.

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
provider choices, update effects, disk/download limits, and total time. Set an
explicit total budget in minutes (1–720); each automated stage is independently
capped at 60 minutes and consumes from the same total, including the one allowed
diagnosed fresh retry. A 35-minute reserve is withheld from non-teardown stages
for exact cleanup. Keep every stage command's budget identical. Interactive
pauses do not restart a stage or replenish its budget.

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

Do **not** clean up yet. Record the selected owner-observed required gates in §5,
then run the exact `--cleanup` teardown in §6. Teardown uses the same remaining
total budget and 60-minute stage cap, preserves the sanitized report outside the
deleted run namespace, and marks teardown PASS only after residue checks succeed.

`early-auth` and `final-auth` are compatibility stages that record the approved
live-OpenAI work as DEFERRED and perform no browser login, paid request, refresh,
or credential action. Advance through them in the documented order; do not run
the historical browser bridge procedure for this local-first scope.

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
Resume rejects changed checkout/config, profile/listener identity, suite, memory,
provider selection, update effects or budget, and serializes stage/cleanup
mutations. A second exact-revision
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
version mismatch, a non-required gate, an incomplete PASS checklist, a duplicate
observation, or an attempt to override an automated failure/prerequisite block.
For an unavailable or failed check, record `BLOCKED` or `FAIL` with only the
checklist IDs that were actually observed. Independent passing gates remain in
the report. Record `HUMAN-TUI` after fresh usability and **before**
`nested-boundaries-agents`; when a nested suite is selected, the runner adds it as
a required gate and refuses recursion until that exact observation is PASS.
Record other selected owner checklists before exact teardown. `HUMAN-AUTH` must
not be recorded: its live checks are DEFERRED, are not a required local-first gate,
and are not accepted by the observation command. Once cleanup is PASS, the report
is final and owner observations are refused.

### Gate → procedure → evidence → cleanup matrix

The report is the machine-evidence ledger; the owner records only fixed checklist
IDs. The stage runner never promotes an owner action or a health check into a
behavioral PASS.

| Gate | Procedure and pass condition | Safe evidence | Cleanup / stop condition |
|---|---|---|---|
| `HOST-PREFLIGHT` | Read-only Linux/KVM, memory, disk, tool, source/config hash, ownership and collision checks. | Fixed check IDs/statuses and resource counts only. | No VM is created. Any blocked required check stops fresh start. |
| `HOST-CLEAN` | Start the exact fresh profile; verify pinned image integrity, clean cloud-init, virtiofs workspace UID/GID, guest Docker, Node, MCP files and exact OpenCode version. | Profile/runtime/image/payload hashes and boolean check map. | Keep only this run's profile; use exact cleanup on any failed start. |
| `E2E-DAILY` | Runner performs repeated start/warm identity and A→B→B same-overlay/workspace switch. Owner enters native TUI and tests `lab-docker` rollback without recreating the host Docker session. | Runtime identity, overlay/key digests, workspace tree hashes, checklist IDs. | Stop if `lab-docker` is unavailable or rollback would recreate/alter the working Docker container. |
| `E2E-MODEL` | Five bounded streams per explicitly selected available local provider; separately observe model-none shell/auth and local-provider denial/missing-model errors. | Provider enum, attempted/passed counts, schema boolean; no prompt or response text. | Provider profiles are run-owned and suspended after checks. Missing selected local service/model blocks the local gate. OpenAI live streams/auth remain DEFERRED and are not attempted. |
| `E2E-WORK` | Runner uses only preloaded `postgres:16-alpine` inside guest Compose, checks health/query, bind canary, restart persistence, no published container service, guest-loopback binding, VM suspend/resume DB canary, then removes exact volume/path. Owner performs failing-test→fix→pass and verifies no host socket/service. | Boolean Compose/query/bind/retention/cleanup checks and the fixed owner checklist. | The runner refuses image pulls; exact Compose project and generated workspace path only. Stop if cleanup cannot confirm volume/path removal. |
| `E2E-LIFE` | Runner checks suspend/resume identity, local DB/tools/keys/workspace retention, update scan/cancel, separately authorized exact `config`/`tools` apply, resize and memory identity. Live OpenAI auth-state retention is DEFERRED. | Exit/status enums, version, RAM readings, profile/overlay/key/workspace digests. | Do not apply updates without exact plan+execution approval. Leave ambiguous children or auth state intact for owner review. |
| `E2E-AGENTS` | Two owner-authorized same-guest harmless tasks run concurrently in separate terminals; verify distinct session/auth ports, clean cancellation, and discuss shared-workspace collision risk. | Four checklist IDs and observed booleans only; never save agent transcripts. | Stop both exact tasks normally; do not claim file isolation, a supervisor or safe shared-workspace concurrency. |
| `SEC-FS` | Required host/guest file-read, control-key/socket, child out-of-scope, symlink and source/state-overlap probes. The live disposable-credential export item is individually DEFERRED. | Fixed subcheck statuses only; never the canary value, private key or socket data. | Current campaign implementation lacks the real filesystem probes, so keep the gate BLOCKED. No live credential export is attempted. |
| `SEC-NET` | Runner performs actual boundary probes from campaign depth 0, self-test G1 and its actual nested G2 child; review fixed 12-probe reports and exact 0→1→2 topology. | Probe IDs/statuses and report digests only. | The self-test deletes only manifested children deepest-first. Missing G2 report, ancestry, or cleanup is BLOCKED/FAIL—not inferred from a depth label. |
| `SEC-SECRET` | Run the repository canary checker on exact snapshot/tree, loose Git objects, seed, logs, reports, and image artifacts. Local provider-forwarding and synthetic/private-state persistence/export-failure checks remain required. Live OpenAI credential roundtrips are individually DEFERRED. | Fixed IDs/statuses/reason codes, bounded counts and safe artifact digests only; no token, auth-store dump, transcript or canary text. | QCOW2 virtual filesystem inspection and the missing required subchecks keep the gate BLOCKED. Never delete the only credential copy or infer PASS from canary absence alone. |
| `HUMAN-TUI` | One terminal: native entry, warm start without reboot/install, metrics sidebar/tool, Ctrl-C cancel/confirm, retry rendering, exact version. | Complete fixed checklist IDs and observed version. | Exit normally; no screenshots containing secrets. |
| `HUMAN-PERM` | Exercise actual auto-allowed, auto-denied, ask prompt, shared/standalone parity and argv/UI action record in the disposable guest. | Complete fixed checklist IDs and observed version; no free-form output. | Stop if a deny is weakened or an action exceeds the requested scope. |
| `HUMAN-AUTH` | Live OpenAI browser/login, negative scenarios, refresh, export/delete/reimport and recursive credential import are deferred follow-up work. | Report `DEFERRED` with `owner_deferred_live_openai`; never record these as PASS. | No campaign auth action occurs. Preserve existing auth state; a later separate live-test approval is required. |
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

The runner now creates one unguessable synthetic canary under the private
campaign state, outside the exported source, with mode `0600`. The checker reads
it privately and scans only the exact artifact manifest. It emits fixed statuses,
sanitized reason codes, counts and safe digests; it never prints a match, canary,
or file contents. Positive/negative, encoded/raw, redaction, path, symlink,
incomplete-coverage and timeout behavior is fixture-tested. Do not substitute
generic `grep` output or ask a guest/model to print contents. The checker scans
raw QCOW2 bytes but cannot inspect QCOW2 virtual filesystems; those artifacts
remain BLOCKED as `qcow2_content_not_inspected`. Therefore a clean canary result
does not make `SEC-SECRET` pass. The current gate separately remains BLOCKED on
required filesystem-boundary, provider-forwarding and synthetic private-state
export/import prerequisites. The report's source/state collision and disk-budget
checks are separate evidence. Live credential export/delete/reimport is deferred
and is not part of the local-first campaign.

### Human gates

- **HUMAN-TUI:** `single-command-entry`, `warm-start-no-reboot`,
  `metrics-sidebar-and-tool`, `ctrl-c-cancel-and-confirm`, `retry-rendering`,
  `version-matched`. Use the same terminal for supported dialogs, metrics,
  cancel/confirm and retry. Do not save screenshots containing secrets.
- **HUMAN-PERM:** `auto-allowed-tool`, `auto-deny-preserved`, `ask-mode-prompt`,
  `shared-standalone-match`, `argv-ui-action-recorded`. Verify actual tool and
  deny behavior, not a mock or default answer.
- **HUMAN-AUTH:** `DEFERRED` (not an observation request): `browser-success`,
  `cancel-interrupt-timeout-occupied-port-egress-denied`,
  `auth-list-and-completion`, `temporary-callback-removed`, `refresh-persisted`,
  `disposable-delete-reimport`, `recursive-provider-import`. The report records
  every item as DEFERRED with `owner_deferred_live_openai`; do not try to record
  this gate. A later live follow-up needs separate approval and disposable
  provider credentials.

### Other owner-run behavioral gates

These are intentionally not inferred from guest health or a successful boot.
Perform the real behavior in the disposable campaign guest, then record its
complete checklist:

The generated campaign state uses the validated model-none default and never
imports provider settings or credentials from the host's ordinary Little Mester
state. The optional private provider file explicitly configures only owner-selected
providers/models; it contains no auth. Do not repoint the campaign at ordinary
state or copy auth data to force a model test. With no selected local provider,
local real-stream coverage remains BLOCKED; OpenAI login/streams are DEFERRED, not
treated as a substitute or a PASS.

- **E2E-DAILY:** `native-tui-entry`,
  `two-identical-starts-no-reboot-or-install`, `A-B-B-same-overlay-and-keys`,
  `warm-start-same-process-identity`, `docker-rollback-without-recreation`.
  The runner verifies warm identity and the A→B→B lifecycle path. Verify actual
  TUI entry and the supported Docker rollback path without recreating the host
  Docker container. If the separately staged `lab-docker` route is unavailable,
  leave this gate BLOCKED; an owner observation must not turn a missing rollback
  route into PASS.
- **E2E-MODEL:** `model-none-shell-and-auth`, `each-selected-local-provider-selected`,
  `five-real-streams-per-selected-local-provider`,
  `local-provider-denial-and-missing-model-errors`,
  `no-model-response-or-credential-recorded`. Run five actual streaming requests
  per selected local provider, not health checks. OpenAI-specific checks are
  separately listed DEFERRED.
- **E2E-WORK:** `guest-only-postgres-compose`, `health-and-query`,
  `failing-test-fix-pass`, `volume-survives-stop-restart`, `compose-down`,
  `bind-and-guest-loopback-port`, `no-host-published-service-or-socket`. Keep
  Compose services and ports inside the guest; do not use host Docker or publish
  guest services on the host/LAN.
- **E2E-LIFE:** `suspend-resume-identity`,
  `local-db-tools-keys-workspace-retained`, `update-scan-cancel-noop`,
  `selected-apply-exact-version`, `live-child-blocks-parent`,
  `deepest-first-shutdown`, `resize-memtotal-and-identity`,
  `exact-teardown-no-residue`, `base-workspace-reports-preserved`. The runner
  records suspend/resume, update scan/cancel, exact optional `config`/`tools`
  update, resize, and identity evidence. Verify the remaining real workload,
  local-state, child-blocking, and teardown observations before recording PASS;
  live OpenAI auth-state retention is DEFERRED.
- **E2E-AGENTS:** `two-authorized-same-guest-tasks`, `separate-auth-ports`,
  `clean-agent-cancellation`, `shared-workspace-risk-explained-no-supervisor-claim`.
  Use two distinct authorized tasks; describe concurrent shared-workspace risk
  and do not claim a supervisor or file-isolation guarantee.
- **SEC-FS:** `host-file-unreadable`, `control-key-and-socket-unreadable`,
  `child-out-of-scope-files-unreadable`, `escaping-symlinks-refused`,
  `state-root-overlap-blocked`, `one-export-verified`. The last live
  credential-export item is individually DEFERRED; the required filesystem
  probes are not yet implemented as campaign evidence and remain BLOCKED.
- **SEC-SECRET:** `canary-absent-from-snapshot-and-history`,
  `canary-absent-from-seed-base-and-logs`,
  `canary-absent-from-campaign-and-self-test-reports`, `provider-forwarding-opt-in-only`,
  `synthetic-private-state-persistence-and-atomic-export-import`,
  `synthetic-export-failure-prevents-delete`,
  `synthetic-refreshed-state-survives-restart-and-import`. The latter required
  local synthetic checks are not yet wired into campaign evidence, so the gate
  remains BLOCKED even if canary scans are clean. Live credential roundtrips are
  separate DEFERRED checks.

**E2E-SELF/E2E-DEPTH** run through the live TASK-13 adapter. The adapter needs a
ready exact parent profile, nested KVM, sufficient resource budget, and an
interactive harmless child-agent task. It verifies candidate lifecycle and
bootstrap mutations, failed-A/fixed-B boots, exact source/runtime hashes,
G1/G2/G3, refusal before state/QEMU, exact child cleanup, and parent continuity.
Missing selected local-model capabilities or nested prerequisites are BLOCKED,
not waived. Live OpenAI credentials are neither needed nor used here.

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
