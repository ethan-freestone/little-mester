#!/usr/bin/env bash
# Supported-launcher recursion policy. Sourcing is inert.

VM_RECURSION_MAX_SUPPORTED=32
VM_RECURSION_SOURCE_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
VM_RECURSION_ROOT="$(cd "$(dirname "$VM_RECURSION_SOURCE_FILE")/../.." && pwd)"
VM_RECURSION_PYTHON="${VM_RECURSION_PYTHON:-python3}"
VM_RECURSION_PATHS="${VM_RECURSION_PATHS:-$VM_RECURSION_ROOT/lib/vm/paths.py}"
VM_RECURSION_SNAPSHOT="${VM_RECURSION_SNAPSHOT:-$VM_RECURSION_ROOT/scripts/vm-recursion-workspace.py}"

vm_recursion_error() {
  printf 'REFUSED: %s\n' "$*" >&2
  return 2
}

vm_recursion_apply_guest_identity() {
  local identity_file="${1:-/etc/lab-vm-recursion}"
  local inherited_depth='-' inherited_max='-' identity
  [[ -v LAB_VM_RECURSION_DEPTH ]] && inherited_depth="$LAB_VM_RECURSION_DEPTH"
  [[ -v LAB_VM_MAX_DEPTH ]] && inherited_max="$LAB_VM_MAX_DEPTH"
  if ! identity="$("$VM_RECURSION_PYTHON" - "$identity_file" "$inherited_depth" "$inherited_max" <<'PY'
import os
import re
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
inherited_depth, inherited_max = sys.argv[2:]
try:
    info = path.lstat()
except FileNotFoundError:
    if inherited_depth != "-" or inherited_max != "-":
        raise SystemExit(2)
    print("-")
    raise SystemExit(0)
if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
        or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o022
        or info.st_size > 128):
    raise SystemExit(2)
try:
    text = path.read_bytes().decode("ascii")
except (OSError, UnicodeDecodeError):
    raise SystemExit(2) from None
match = re.fullmatch(r"(0|[1-9][0-9]?) (0|[1-9][0-9]?)\n?", text)
if match is None:
    raise SystemExit(2)
depth, maximum = map(int, match.groups())
if depth > 32 or maximum > 32 or depth > maximum:
    raise SystemExit(2)
if inherited_depth != "-" and inherited_depth != str(depth):
    raise SystemExit(2)
if inherited_max != "-" and inherited_max != str(maximum):
    raise SystemExit(2)
print(f"{depth} {maximum}")
PY
  )"; then
    vm_recursion_error 'Persisted recursion identity is missing, unsafe, invalid, or conflicts with the caller environment.'
    return 2
  fi
  [[ "$identity" == - ]] && return 0
  read -r LAB_VM_RECURSION_DEPTH LAB_VM_MAX_DEPTH <<<"$identity"
  export LAB_VM_RECURSION_DEPTH LAB_VM_MAX_DEPTH
}

vm_recursion_configure() {
  local command="${1:-}" parent_depth=0 inherited_max=0 inherited=0
  VM_NESTED_LAUNCH=0
  VM_GUEST_RECURSION_DEPTH=0
  if [[ -v LAB_VM_RECURSION_DEPTH || -v LAB_VM_MAX_DEPTH ]]; then
    [[ -v LAB_VM_RECURSION_DEPTH && -v LAB_VM_MAX_DEPTH ]] || {
      vm_recursion_error 'Inherited recursion identity is incomplete.'; return 2;
    }
    [[ "$LAB_VM_RECURSION_DEPTH" =~ ^(0|[1-9][0-9]?)$ &&
       "$LAB_VM_MAX_DEPTH" =~ ^(0|[1-9][0-9]?)$ ]] || {
      vm_recursion_error 'Inherited recursion identity is malformed.'; return 2;
    }
    parent_depth="$LAB_VM_RECURSION_DEPTH"
    inherited_max="$LAB_VM_MAX_DEPTH"
    (( 10#$parent_depth <= 10#$inherited_max && 10#$inherited_max <= VM_RECURSION_MAX_SUPPORTED )) || {
      vm_recursion_error 'Inherited recursion identity exceeds the supported limit.'; return 2;
    }
    inherited=1
  fi
  if [[ "$command" != run && "$command" != up ]]; then
    if (( VM_RECURSE )) || [[ -v 'VM_SEEN[max-depth]' ]]; then
      vm_recursion_error 'Recursion options apply only to run/up.'; return 2
    fi
    VM_EFFECTIVE_MAX_DEPTH="$inherited_max"
    VM_GUEST_RECURSION_DEPTH="$parent_depth"
    return 0
  fi
  if [[ -v 'VM_SEEN[max-depth]' ]]; then
    if (( inherited )) && (( 10#${VM_MAX_DEPTH:-0} > 10#$inherited_max ||
                             10#${VM_MAX_DEPTH:-0} < 10#$parent_depth )); then
      vm_recursion_error 'A child cannot raise its inherited maximum or lower it below the current depth.'; return 2
    fi
    VM_EFFECTIVE_MAX_DEPTH="${VM_MAX_DEPTH:-0}"
  elif (( inherited )); then
    VM_EFFECTIVE_MAX_DEPTH="$inherited_max"
  else
    VM_EFFECTIVE_MAX_DEPTH="${VM_MAX_DEPTH:-0}"
  fi
  if (( VM_RECURSE )) && [[ ! -v 'VM_SEEN[max-depth]' ]]; then
    vm_recursion_error 'Recursive run/up requires an explicit --max-depth (0..32).'
    return 2
  fi
  if [[ ! "$VM_EFFECTIVE_MAX_DEPTH" =~ ^(0|[1-9][0-9]?)$ ]] ||
    (( 10#$VM_EFFECTIVE_MAX_DEPTH > VM_RECURSION_MAX_SUPPORTED )); then
      vm_recursion_error 'Effective maximum depth must be from 0 to 32.'; return 2;
  fi
  if (( inherited )); then
    (( 10#$VM_EFFECTIVE_MAX_DEPTH >= 10#$parent_depth )) || {
      vm_recursion_error 'Effective maximum depth is below the persisted current depth.'; return 2;
    }
    (( VM_RECURSE == 1 )) || {
      vm_recursion_error 'Launching another VM from a guest requires explicit --recurse.'; return 2;
    }
    (( VM_UNSAFE_RECURSE == 0 )) || {
      vm_recursion_error 'Nested launches always use an isolated source snapshot; --unsafe-recurse is unavailable inside a guest.'; return 2;
    }
    (( 10#$parent_depth < 10#$VM_EFFECTIVE_MAX_DEPTH )) || {
      vm_recursion_error "Maximum nesting depth $VM_EFFECTIVE_MAX_DEPTH reached; no child state or snapshot was created."; return 2;
    }
    if [[ "${VM_SEEN[browser]+yes}" == yes && "${VM_BROWSER_AUTH:-auto}" == on ]]; then
      vm_recursion_error 'Browser forwarding is disabled for recursive child profiles.'; return 2
    fi
    if [[ "${VM_SEEN[auth-forward]+yes}" == yes && "${VM_OPTIONS[auth-forward]:-off}" != off ]]; then
      vm_recursion_error 'Credential forwarding is disabled for recursive child profiles.'; return 2
    fi
    VM_NESTED_LAUNCH=1
    # shellcheck disable=SC2034 # Public value consumed by lifecycle profile creation.
    VM_GUEST_RECURSION_DEPTH=$((10#$parent_depth + 1))
    VM_BROWSER_AUTH=off
  fi
}

vm_recursion_kvm_available() {
  local device="${1:-/dev/kvm}"
  [[ -c "$device" && -r "$device" && -w "$device" ]]
}

vm_recursion_cpu_virtualization_available() {
  local cpuinfo="${1:-/proc/cpuinfo}"
  grep -Eq '^flags[[:space:]]*:.*[[:space:]](vmx|svm)([[:space:]]|$)' "$cpuinfo" 2>/dev/null
}

vm_recursion_require_nested_kvm() {
  local device="${1:-/dev/kvm}" cpuinfo="${2:-/proc/cpuinfo}"
  [[ "$(uname -s 2>/dev/null)" == Linux && "$(uname -m 2>/dev/null)" == x86_64 ]] || {
    vm_recursion_error 'Nested VM launch requires the supported Linux x86_64 profile.'; return 2;
  }
  vm_recursion_kvm_available "$device" || {
    vm_recursion_error 'Nested VM launch requires usable KVM; no child snapshot or state was created.'; return 2;
  }
  vm_recursion_cpu_virtualization_available "$cpuinfo" || {
    vm_recursion_error 'Nested VM launch requires guest CPU vmx or svm exposure; no child snapshot or state was created.'; return 2;
  }
}

vm_recursion_validate_candidate_handoff() {
  local bundle="${LAB_VM_CANDIDATE_BUNDLE:-}" result workspace command_workspace
  [[ -n "$bundle" ]] || return 0
  [[ -v LAB_VM_RECURSION_DEPTH && -v LAB_VM_MAX_DEPTH ]] || {
    vm_recursion_error 'Candidate handoff requires a persisted parent recursion identity.'; return 2;
  }
  [[ "${VM_COMMAND:-}" == run || "${VM_COMMAND:-}" == up ]] || {
    vm_recursion_error 'Candidate handoff is valid only for run/up.'; return 2;
  }
  [[ -n "${VM_WORKSPACE:-}" ]] || {
    vm_recursion_error 'Candidate handoff requires its exact snapshot workspace.'; return 2;
  }
  result="$("$VM_RECURSION_PYTHON" "$VM_RECURSION_SNAPSHOT" verify --bundle "$bundle")" || {
    vm_recursion_error 'Candidate snapshot verification failed.'; return 2;
  }
  workspace="$(printf '%s' "$result" | "$VM_RECURSION_PYTHON" -c '
import json,sys
try:
    value=json.load(sys.stdin)["workspace"]
    assert isinstance(value,str) and value.startswith("/")
    print(value)
except Exception: raise SystemExit(2)
')" || return 2
  command_workspace="$(realpath -e -- "$VM_WORKSPACE")" || return 2
  [[ "$workspace" == "$VM_REPO" && "$command_workspace" == "$workspace" ]] || {
    vm_recursion_error 'Candidate launcher and exported workspace must be the exact frozen snapshot.'; return 2;
  }
  [[ -f "$workspace/lab-vm" && ! -L "$workspace/lab-vm" && -x "$workspace/lab-vm" ]] || {
    vm_recursion_error 'Frozen candidate lab-vm launcher is missing or unsafe.'; return 2;
  }
  # shellcheck disable=SC2034 # Read by the context report and later dispatcher.
  VM_RECURSION_CANDIDATE_HANDOFF=1
  unset LAB_VM_CANDIDATE_BUNDLE
}

vm_recursion_candidate_build() {
  local command="${1:?command required}" profile="${2:?profile required}"
  local workspace="${3:?workspace required}" bundle="${4:?bundle required}"
  local state_root="${5:?state root required}" candidate config_fields workload agent version
  [[ "$command" == run || "$command" == up ]] || return 2
  [[ "$profile" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || return 2
  candidate="$workspace/lab-vm"
  [[ -f "$candidate" && ! -L "$candidate" && -x "$candidate" ]] || {
    vm_recursion_error 'Frozen candidate launcher is missing or not executable.'; return 2;
  }
  local config_json="${VM_CONFIG_JSON:-}"
  [[ -n "$config_json" ]] || config_json='{}'
  config_fields="$(printf '%s' "$config_json" | "$VM_RECURSION_PYTHON" -c '
import json,sys
try:
    value=json.load(sys.stdin)
    print("\t".join((value["workload_runtime"],value["agent"],value["opencode_version"])))
except Exception: raise SystemExit(2)
')" || return 2
  IFS=$'\t' read -r workload agent version <<<"$config_fields"
  VM_RECURSION_CANDIDATE_ARGV=(
    "$candidate" "$command" --name "$profile" --workspace "$workspace"
    --recurse --max-depth "${VM_EFFECTIVE_MAX_DEPTH:?}" --metrics off
    --workload-runtime "$workload" --agent "$agent" --opencode-version "$version"
  )
  [[ -z "${VM_MEMORY:-}" ]] || VM_RECURSION_CANDIDATE_ARGV+=(--memory "$VM_MEMORY")
  [[ "${VM_SEEN[yes]+yes}" != yes ]] || VM_RECURSION_CANDIDATE_ARGV+=(--yes)
  if [[ "$command" == run ]]; then
    VM_RECURSION_CANDIDATE_ARGV+=(--no-browser-auth)
    [[ "${VM_PERMISSIONS:-ask}" != auto ]] || VM_RECURSION_CANDIDATE_ARGV+=(--permissions auto)
    [[ ! -v 'VM_SEEN[cwd]' ]] || VM_RECURSION_CANDIDATE_ARGV+=(--cwd "$VM_CWD")
    if [[ "${VM_SEEN[model-provider]+yes}" == yes ]]; then
      VM_RECURSION_CANDIDATE_ARGV+=(--model-provider "${VM_OPTIONS[model-provider]}")
    fi
    if [[ "${VM_SEEN[model-port]+yes}" == yes ]]; then
      VM_RECURSION_CANDIDATE_ARGV+=(--model-port "${VM_OPTIONS[model-port]}")
    fi
    if [[ "${VM_SEEN[model-id]+yes}" == yes ]]; then
      VM_RECURSION_CANDIDATE_ARGV+=(--model-id "${VM_OPTIONS[model-id]}")
    fi
    if ((${#VM_AGENT_ARGS[@]})); then
      VM_RECURSION_CANDIDATE_ARGV+=(-- "${VM_AGENT_ARGS[@]}")
    fi
  fi
  local parent_depth="${LAB_VM_RECURSION_DEPTH:-0}" maximum="${VM_EFFECTIVE_MAX_DEPTH:?}"
  VM_RECURSION_CANDIDATE_ENV=(
    'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
    "HOME=$state_root" 'TERM=dumb' 'LANG=C.UTF-8'
    "MESTER_STATE_DIR=$state_root"
    "LAB_VM_RECURSION_DEPTH=$parent_depth" "LAB_VM_MAX_DEPTH=$maximum"
    "LAB_VM_CANDIDATE_BUNDLE=$bundle"
  )
  # shellcheck disable=SC2034 # Public result consumed by candidate report/dispatcher callers.
  VM_RECURSION_CANDIDATE_WORKSPACE="$workspace"
  VM_RECURSION_STATE_ROOT="$state_root"
  VM_RECURSION_SOURCE_MANIFEST="$bundle/source-manifest.json"
  VM_RECURSION_SOURCE_TREE_SHA256="$("$VM_RECURSION_PYTHON" "$VM_RECURSION_SNAPSHOT" verify --bundle "$bundle" | \
    "$VM_RECURSION_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["tree_sha256"])')" || return 2
}

vm_recursion_prepare_candidate() {
  local state_root="${1:?state root required}" profile="${2:?profile required}"
  local source="${3:?source required}" command="${4:?command required}"
  local kvm_device="${5:-/dev/kvm}" cpuinfo="${6:-/proc/cpuinfo}"
  local run_info run_fields run_root child_state_root
  (( ${VM_NESTED_LAUNCH:-0} == 1 && ${VM_RECURSE:-0} == 1 )) || {
    vm_recursion_error 'Candidate snapshot preparation requires an authorized nested launch.'; return 2;
  }
  # Refuse unsupported nesting before creating private state or materializing a snapshot.
  vm_recursion_require_nested_kvm "$kvm_device" "$cpuinfo" || return $?
  run_info="$("$VM_RECURSION_PYTHON" "$VM_RECURSION_PATHS" recursion-run-create \
    --state-root "$state_root" --run-name "$profile")" || {
    vm_recursion_error 'Private candidate run state could not be allocated.'; return 2;
  }
  run_fields="$(printf '%s' "$run_info" | "$VM_RECURSION_PYTHON" -c '
import json,sys
try:
    data=json.load(sys.stdin)
    print("\t".join((data["run_root"],data["state_root"])))
except Exception: raise SystemExit(2)
')" || return 2
  IFS=$'\t' read -r run_root child_state_root <<<"$run_fields"
  local -a snapshot_args=(prepare --source "$source" --run-root "$run_root")
  if [[ -v LAB_VM_MAX_SNAPSHOT_BYTES ]]; then
    snapshot_args+=(--max-bytes "$LAB_VM_MAX_SNAPSHOT_BYTES")
  fi
  VM_RECURSION_SNAPSHOT_RESULT="$("$VM_RECURSION_PYTHON" "$VM_RECURSION_SNAPSHOT" "${snapshot_args[@]}")" || {
    vm_recursion_error 'Revision-bound candidate snapshot failed; existing run state was preserved.'; return 2;
  }
  run_fields="$(printf '%s' "$VM_RECURSION_SNAPSHOT_RESULT" | "$VM_RECURSION_PYTHON" -c '
import json,sys
try:
    data=json.load(sys.stdin)
    print("\t".join((data["workspace"],data["bundle"])))
except Exception: raise SystemExit(2)
')" || return 2
  local candidate_workspace bundle
  IFS=$'\t' read -r candidate_workspace bundle <<<"$run_fields"
  vm_recursion_candidate_build "$command" "$profile" "$candidate_workspace" "$bundle" "$child_state_root"
}

vm_recursion_exec_candidate() {
  ((${#VM_RECURSION_CANDIDATE_ARGV[@]})) || return 2
  exec env -i "${VM_RECURSION_CANDIDATE_ENV[@]}" "${VM_RECURSION_CANDIDATE_ARGV[@]}"
}

vm_recursion_context_json() {
  local payload_sha256="${1:-}" version="${2:-}" depth="${3:-0}" maximum="${4:-0}"
  "$VM_RECURSION_PYTHON" - "$VM_RECURSION_SOURCE_MANIFEST" \
    "$VM_RECURSION_SOURCE_TREE_SHA256" "$payload_sha256" "$version" \
    "$depth" "$maximum" "$VM_RECURSION_STATE_ROOT" <<'PY'
import json
import re
import sys
from pathlib import Path

manifest_path, tree, payload, version, depth, maximum, state_root = sys.argv[1:]
try:
    manifest = json.loads(Path(manifest_path).read_text(encoding="ascii"))
    source = manifest["source_manifest"]
    assert re.fullmatch(r"[a-f0-9]{64}", tree)
    assert not payload or re.fullmatch(r"[a-f0-9]{64}", payload)
    assert re.fullmatch(r"(?:0|[1-9][0-9]?)", depth)
    assert re.fullmatch(r"(?:0|[1-9][0-9]?)", maximum)
    assert int(depth) <= int(maximum) <= 32
    result = {
        "schema_version": 1, "source_head": source["source_head"],
        "source_tree_sha256": tree, "candidate_payload_sha256": payload or None,
        "opencode_version": version or None, "depth": int(depth),
        "max_depth": int(maximum), "nested_kvm_preflight": "passed",
        "effective_state_root": state_root,
    }
except Exception:
    raise SystemExit(2) from None
print(json.dumps(result, sort_keys=True, separators=(",", ":")))
PY
}

vm_recursion_clean_run() {
  local state_root="${1:?state root required}" run_name="${2:?run name required}"
  local confirmed="${3:-0}" answer=''
  [[ "$run_name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || return 2
  if (( confirmed != 1 )); then
    [[ -t 0 ]] || { vm_recursion_error 'Recursive cleanup requires an interactive confirmation or explicit --yes.'; return 2; }
    printf 'This removes only stopped recursive run %s and preserves its reports. Type the exact run name: ' "$run_name" >&2
    read -r answer || answer=''
    [[ "$answer" == "$run_name" ]] || { vm_recursion_error 'Recursive cleanup was not confirmed.'; return 2; }
  fi
  local -a args=(recursion-run-delete --state-root "$state_root" --run-name "$run_name" --confirm-run "$run_name")
  [[ ! -v VM_RECURSION_PROC_ROOT ]] || args+=(--proc-root "$VM_RECURSION_PROC_ROOT")
  "$VM_RECURSION_PYTHON" "$VM_RECURSION_PATHS" "${args[@]}"
}
