#!/usr/bin/env bash
# Safe profile lifecycle orchestration. Sourcing this module is inert.

VM_LIFECYCLE_SOURCE_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
VM_LIFECYCLE_ROOT="$(cd "$(dirname "$VM_LIFECYCLE_SOURCE_FILE")/../.." && pwd)"
VM_LIFECYCLE_PYTHON="${VM_LIFECYCLE_PYTHON:-python3}"
VM_LIFECYCLE_PATHS="${VM_LIFECYCLE_PATHS:-$VM_LIFECYCLE_ROOT/lib/vm/paths.py}"
VM_LIFECYCLE_PROCESS="${VM_LIFECYCLE_PROCESS:-$VM_LIFECYCLE_ROOT/lib/vm/process.py}"
VM_LIFECYCLE_CHILDREN="${VM_LIFECYCLE_CHILDREN:-$VM_LIFECYCLE_ROOT/scripts/vm-recursion-live-children.py}"
VM_LIFECYCLE_DEFAULT_TIMEOUT=60

vm_lifecycle_error() {
  printf 'REFUSED: %s\n' "$*" >&2
  return 2
}

vm_lifecycle_profile_read() {
  local root="$1" name="$2"
  "$VM_LIFECYCLE_PYTHON" "$VM_LIFECYCLE_PATHS" profile-read \
    --state-root "$root" --profile "$name"
}

vm_lifecycle_profile_list() {
  local root="$1"
  "$VM_LIFECYCLE_PYTHON" "$VM_LIFECYCLE_PATHS" profile-list --state-root "$root"
}

vm_lifecycle_transition() {
  local root="$1" name="$2" expected="$3" target="$4" patch="${5:-}"
  [[ -n "$patch" ]] || patch='{}'
  printf '%s' "$patch" | "$VM_LIFECYCLE_PYTHON" "$VM_LIFECYCLE_PATHS" profile-transition \
    --state-root "$root" --profile "$name" \
    --expected-status "$expected" --status "$target"
}

vm_lifecycle_field() {
  local data="$1" field="$2"
  "$VM_LIFECYCLE_PYTHON" -c '
import json,sys
try:
    value=json.loads(sys.argv[1])[sys.argv[2]]
    if isinstance(value,bool): print("true" if value else "false")
    elif value is None: print("")
    elif isinstance(value,(str,int)): print(value)
    else: raise ValueError
except Exception: raise SystemExit(2)
' "$data" "$field"
}

vm_lifecycle_workspace_valid() {
  local root="$1" name="$2" workspace="$3" repository="$4" recurse="${5:-0}" unsafe="${6:-0}"
  local -a args=(validate-workspace --state-root "$root" --profile "$name" \
    --repository "$repository" --workspace "$workspace")
  (( recurse == 0 )) || args+=(--recurse)
  (( unsafe == 0 )) || args+=(--unsafe-recurse)
  "$VM_LIFECYCLE_PYTHON" "$VM_LIFECYCLE_PATHS" "${args[@]}" >/dev/null
}

vm_lifecycle_identity_state() {
  local data="$1" root="$2" name="$3"
  "$VM_LIFECYCLE_PYTHON" - "$VM_LIFECYCLE_PROCESS" "$data" "$root" "$name" \
    "${VM_LIFECYCLE_PROC_ROOT:-/proc}" <<'PY'
import importlib.util, json, sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("vm_lifecycle_process",sys.argv[1])
if spec is None or spec.loader is None: raise SystemExit(2)
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
try:
    data=json.loads(sys.argv[2]); item=data.get("runtime_identity")
    if not isinstance(item,dict): print("unknown"); raise SystemExit(0)
    expected={"pid":item["pid"],"start_ticks":item["start_ticks"],
              "executable":item["executable"],"argv":item["argv"]}
    profile=Path(sys.argv[3])/"vm/profiles"/sys.argv[4]
    disk=profile/"disk.qcow2"
    executable=Path(expected["executable"]).name
    drive=f"file={disk},format=qcow2,if=virtio"
    if (executable not in {"qemu-system-x86_64","qemu-system-x86"}
            or expected["argv"][0] != expected["executable"]
            or item.get("disk_path") != str(disk)
            or drive not in expected["argv"]
            or not disk.is_file() or disk.is_symlink()):
        print("unknown"); raise SystemExit(0)
    print(module.identity_state(expected,proc_root=Path(sys.argv[5])))
except (KeyError, TypeError, ValueError, OSError, module.ProcessPolicyError):
    print("unknown")
PY
}

vm_lifecycle_registry_for_profile() {
  local root="$1" name="$2" data="$3"
  "$VM_LIFECYCLE_PYTHON" - "$VM_LIFECYCLE_PATHS" "$VM_LIFECYCLE_PROCESS" \
    "$root" "$name" "$data" <<'PY'
import importlib.util, json, sys
from pathlib import Path
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    if spec is None or spec.loader is None: raise RuntimeError
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module
try:
    paths=load("vm_lifecycle_paths",sys.argv[1]); process=load("vm_lifecycle_process",sys.argv[2])
    root=Path(sys.argv[3]); profile=root/"vm/profiles"/sys.argv[4]
    data=json.loads(sys.argv[5]); runtime=data["runtime_identity"]
    expected={"pid":runtime["pid"],"start_ticks":runtime["start_ticks"],
              "executable":runtime["executable"],"argv":runtime["argv"]}
    runs=profile/"runs"
    if runs.is_symlink() or not runs.is_dir(): raise RuntimeError
    paths.private_entry(runs)
    matches=[]
    for run in runs.iterdir():
        if run.is_symlink() or not run.is_dir(): raise RuntimeError
        paths.private_entry(run)
        registry=run/"processes.json"
        record=process.registry_read(registry,run.name)
        for role,item in record["processes"].items():
            if role.startswith("qemu_") and item["identity"] == expected:
                matches.append((registry,run.name,role))
    if len(matches)!=1: raise RuntimeError
    print(json.dumps({"registry":str(matches[0][0]),"run_id":matches[0][1],"role":matches[0][2]},sort_keys=True))
except Exception:
    raise SystemExit(2)
PY
}

vm_lifecycle_children_guard() {
  local root="$1" recursive="$2" controller="$3" rc
  if "$VM_LIFECYCLE_PYTHON" "$VM_LIFECYCLE_CHILDREN" --state-root "$root" >/dev/null; then
    return 0
  else
    rc=$?
  fi
  case "$rc" in
    3)
      (( recursive == 1 )) || { vm_lifecycle_error 'Live nested QEMU children block shutdown; use --poweroff-recursive after review.'; return 2; }
      [[ -n "$controller" ]] || { vm_lifecycle_error 'A frozen child controller is required for recursive suspend.'; return 2; }
      "$VM_LIFECYCLE_PYTHON" "$VM_LIFECYCLE_CHILDREN" --state-root "$root" \
        --poweroff-recursive --controller "$controller"
      ;;
    *) vm_lifecycle_error 'Nested QEMU inventory is unknown or unsafe; profile was preserved.'; return 2 ;;
  esac
}

vm_lifecycle_guest_shutdown() {
  local root="$1" name="$2" data="$3" key registry_info registry run_id role known_hosts port
  registry_info="$(vm_lifecycle_registry_for_profile "$root" "$name" "$data")" || {
    vm_lifecycle_error 'The exact QEMU process registry could not be established.'; return 2;
  }
  read -r registry run_id role < <("$VM_LIFECYCLE_PYTHON" -c '
import json,sys
d=json.loads(sys.argv[1]); print(d["registry"],d["run_id"],d["role"])
' "$registry_info") || return 2
  key="$root/vm/profiles/$name/control-key"
  known_hosts="$(dirname "$registry")/known_hosts"
  port="$(printf '%s' "$data" | "$VM_LIFECYCLE_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["runtime_identity"]["ssh_port"])')" || return 2
  [[ -f "$key" && ! -L "$key" && -f "$known_hosts" && ! -L "$known_hosts" ]] || {
    vm_lifecycle_error 'The selected profile SSH identity is missing; guest shutdown was not attempted.'; return 2;
  }
  # shellcheck disable=SC1091
  source "$(dirname "$VM_LIFECYCLE_SOURCE_FILE")/ssh.sh"
  vm_ssh_noninteractive "$key" "$known_hosts" "$port" 15 mester@127.0.0.1 'sudo systemctl poweroff'
}

vm_lifecycle_wait_qemu_stopped() {
  local root="$1" name="$2" data="$3" timeout="$4" registry_info registry run_id role state
  registry_info="$(vm_lifecycle_registry_for_profile "$root" "$name" "$data")" || return 2
  read -r registry run_id role < <("$VM_LIFECYCLE_PYTHON" -c '
import json,sys
d=json.loads(sys.argv[1]); print(d["registry"],d["run_id"],d["role"])
' "$registry_info") || return 2
  state="$("$VM_LIFECYCLE_PYTHON" "$VM_LIFECYCLE_PROCESS" wait-role \
    --registry "$registry" --run-id "$run_id" --role "$role" --timeout "$timeout" 2>/dev/null || true)"
  case "$state" in
    stopped) printf '%s\n' "$registry" "$run_id"; return 0 ;;
    running) return 124 ;;
    *) return 2 ;;
  esac
}

vm_lifecycle_stop_sidecars() {
  local registry="$1" run_id="$2" state
  state="$("$VM_LIFECYCLE_PYTHON" "$VM_LIFECYCLE_PROCESS" stop \
    --registry "$registry" --run-id "$run_id" --wait 5 2>/dev/null || true)"
  [[ "$state" == stopped ]] || return 2
}

vm_lifecycle_suspend_locked() {
  local root="$1" name="$2" recursive="${3:-0}" timeout="${4:-$VM_LIFECYCLE_DEFAULT_TIMEOUT}"
  local controller="${5:-}" data status identity_state shutdown_rc wait_rc registry run_id registry_run
  [[ "$recursive" == 0 || "$recursive" == 1 ]] || return 2
  [[ "$timeout" =~ ^[1-9][0-9]?$ ]] && (( timeout <= 60 )) || return 2
  data="$(vm_lifecycle_profile_read "$root" "$name")" || return 2
  status="$(vm_lifecycle_field "$data" status)" || return 2
  if [[ "$status" == suspended ]]; then
    identity_state="$(vm_lifecycle_identity_state "$data" "$root" "$name")" || return 2
    [[ "$identity_state" == stopped ]] || { vm_lifecycle_error 'Suspended profile still has a live or ambiguous QEMU identity.'; return 2; }
    printf 'Profile %s is already suspended; overlay and workspace were preserved.\n' "$name"
    return 0
  fi
  [[ "$status" == ready ]] || { vm_lifecycle_error "Profile '$name' is not in ready state."; return 2; }
  identity_state="$(vm_lifecycle_identity_state "$data" "$root" "$name")" || return 2
  if [[ "$identity_state" != running ]]; then
    vm_lifecycle_transition "$root" "$name" ready unknown '{}' >/dev/null || true
    vm_lifecycle_error 'QEMU identity is stopped or ambiguous; profile is now blocked as unknown.'
    return 2
  fi
  vm_lifecycle_children_guard "$root" "$recursive" "$controller" || return $?
  vm_lifecycle_transition "$root" "$name" ready suspending '{}' >/dev/null || return 2

  if vm_lifecycle_guest_shutdown "$root" "$name" "$data"; then
    shutdown_rc=0
  else
    shutdown_rc=$?
  fi
  if registry_run="$(vm_lifecycle_wait_qemu_stopped "$root" "$name" "$data" "$timeout")"; then
    registry="${registry_run%%$'\n'*}"
    run_id="${registry_run#*$'\n'}"
    if ! vm_lifecycle_stop_sidecars "$registry" "$run_id"; then
      vm_lifecycle_transition "$root" "$name" suspending unknown '{}' >/dev/null || true
      vm_lifecycle_error 'QEMU stopped but owned sidecars are ambiguous; profile state was preserved.'
      return 2
    fi
    vm_lifecycle_transition "$root" "$name" suspending suspended '{}' >/dev/null || return 2
    printf 'Profile %s is suspended; overlay, guest data, and workspace were preserved.\n' "$name"
    return 0
  else
    wait_rc=$?
  fi
  identity_state="$(vm_lifecycle_identity_state "$data" "$root" "$name")" || identity_state=unknown
  if [[ "$identity_state" == running ]]; then
    vm_lifecycle_transition "$root" "$name" suspending ready '{}' >/dev/null || true
  else
    vm_lifecycle_transition "$root" "$name" suspending unknown '{}' >/dev/null || true
  fi
  if (( wait_rc == 124 )); then
    vm_lifecycle_error 'Graceful guest shutdown timed out; QEMU was not force-killed and profile data was preserved.'
    return 124
  fi
  (( shutdown_rc == 0 )) || vm_lifecycle_error 'Guest shutdown was not confirmed; profile state was preserved.'
  return 1
}

vm_lifecycle_suspend() {
  local root="$1" name="$2" recursive="${3:-0}" timeout="${4:-$VM_LIFECYCLE_DEFAULT_TIMEOUT}"
  local controller="${5:-$root/vm/profiles/$name/runtime/lab-vm}"
  # shellcheck disable=SC2016 # This callback is intentionally evaluated by profile-run's locked Bash worker.
  "$VM_LIFECYCLE_PYTHON" "$VM_LIFECYCLE_PATHS" profile-run \
    --state-root "$root" --profile "$name" -- \
    bash -c 'set -euo pipefail; source "$1"; shift; vm_lifecycle_suspend_locked "$@"' \
    _ "$VM_LIFECYCLE_SOURCE_FILE" "$root" "$name" "$recursive" "$timeout" "$controller"
}

vm_lifecycle_status() {
  local root="$1" name="$2" data status identity_state
  data="$(vm_lifecycle_profile_read "$root" "$name")" || return 2
  status="$(vm_lifecycle_field "$data" status)" || return 2
  identity_state=stopped
  if [[ "$status" == ready || "$status" == suspending || "$status" == starting || "$status" == provisioning ]]; then
    identity_state="$(vm_lifecycle_identity_state "$data" "$root" "$name")" || identity_state=unknown
  fi
  printf '%s\n' "$data" | "$VM_LIFECYCLE_PYTHON" -c '
import json,sys
d=json.load(sys.stdin)
print(json.dumps({"name":d["name"],"status":d["status"],
 "runtime_state":sys.argv[1],"workspace":d["workspace"],
 "memory_gib":d["memory_gib"],"depth":d["depth"],
 "descendants_possible":d["descendants_possible"]},sort_keys=True))
' "$identity_state"
}

vm_lifecycle_list() {
  local root="$1" profiles
  profiles="$(vm_lifecycle_profile_list "$root")" || return 2
  printf '%s\n' "$profiles" | "$VM_LIFECYCLE_PYTHON" -c '
import json,sys
profiles=json.load(sys.stdin)
print(json.dumps([{"name":p["name"],"status":p["status"],
 "memory_gib":p["memory_gib"],"depth":p["depth"],
 "descendants_possible":p["descendants_possible"]} for p in profiles],sort_keys=True))
'
}

vm_lifecycle_destructive_guard() {
  local operation="$1" data="$2" auth_forward="$3" discard_recursive="$4"
  [[ "$auth_forward" == off ]] || {
    vm_lifecycle_error "Auth forwarding is enabled; $operation requires TASK-10 auth export. Resume the guest and export auth before retrying."
    return 2
  }
  [[ "$(vm_lifecycle_field "$data" descendants_possible)" == false || "$discard_recursive" == 1 ]] || {
    vm_lifecycle_error "Profile may contain descendants; $operation requires explicit --discard-recursive acknowledgement."
    return 2
  }
}

vm_lifecycle_require_confirmation() {
  local operation="$1" name="$2" effects="$3" yes="${4:-0}" answer
  printf 'This will %s for profile %s: %s\n' "$operation" "$name" "$effects" >&2
  (( yes == 1 )) && return 0
  [[ -t 0 ]] || { vm_lifecycle_error 'Destructive lifecycle action requires an interactive confirmation or explicit --yes.'; return 2; }
  read -r -p 'Proceed? [y/N] ' answer || answer=''
  [[ "$answer" == y || "$answer" == Y || "$answer" == yes || "$answer" == YES ]] || {
    vm_lifecycle_error 'Cancelled; profile, workspace, base image, and reports were preserved.'
    return 2
  }
}
