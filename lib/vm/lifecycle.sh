#!/usr/bin/env bash
# Safe profile lifecycle orchestration. Sourcing this module is inert.

VM_LIFECYCLE_SOURCE_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
VM_LIFECYCLE_ROOT="$(cd "$(dirname "$VM_LIFECYCLE_SOURCE_FILE")/../.." && pwd)"
VM_LIFECYCLE_PYTHON="${VM_LIFECYCLE_PYTHON:-python3}"
VM_LIFECYCLE_PATHS="${VM_LIFECYCLE_PATHS:-$VM_LIFECYCLE_ROOT/lib/vm/paths.py}"
VM_LIFECYCLE_PROCESS="${VM_LIFECYCLE_PROCESS:-$VM_LIFECYCLE_ROOT/lib/vm/process.py}"
VM_LIFECYCLE_CHILDREN="${VM_LIFECYCLE_CHILDREN:-$VM_LIFECYCLE_ROOT/scripts/vm-recursion-live-children.py}"
VM_LIFECYCLE_MEMORY_BUDGET="${VM_LIFECYCLE_MEMORY_BUDGET:-$VM_LIFECYCLE_ROOT/scripts/vm_memory_budget.py}"
VM_LIFECYCLE_DEFAULT_TIMEOUT=60

# shellcheck source=lib/vm/auth.sh
# shellcheck disable=SC1091 # Frozen runtime path is derived from this module.
source "$VM_LIFECYCLE_ROOT/lib/vm/auth.sh"

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
  vm_lifecycle_workspace_resolve "$@" >/dev/null
}

vm_lifecycle_workspace_resolve() {
  local root="$1" workspace="$2" repository="$3" recurse="${4:-0}" unsafe="${5:-0}" result
  local -a args=(validate-workspace --state-root "$root" \
    --repository "$repository" --workspace "$workspace")
  (( recurse == 0 )) || args+=(--recurse)
  (( unsafe == 0 )) || args+=(--unsafe-recurse)
  result="$("$VM_LIFECYCLE_PYTHON" "$VM_LIFECYCLE_PATHS" "${args[@]}")" || return $?
  printf '%s' "$result" | "$VM_LIFECYCLE_PYTHON" -c '
import json,sys
try:
    value=json.load(sys.stdin)["workspace"]
    assert isinstance(value,str) and value.startswith("/")
    print(value)
except Exception:
    raise SystemExit(2)
'
}

vm_lifecycle_memory_gib() {
  local depth="$1" requested_gib="${2:-}" defaults memory
  if [[ ! "$depth" =~ ^(0|[1-9][0-9]?)$ ]] || (( 10#$depth > 32 )); then
    vm_lifecycle_error 'Recursion depth must be a whole number from 0 to 32.'
    return 2
  fi
  if [[ -n "$requested_gib" ]]; then
    if [[ ! "$requested_gib" =~ ^[1-9][0-9]{0,2}$ ]] || (( 10#$requested_gib > 128 )); then
      vm_lifecycle_error 'Requested VM memory must be whole GiB from 1 to 128.'
      return 2
    fi
    printf '%s\n' "$((10#$requested_gib))"
    return 0
  fi
  defaults="$(vm_lifecycle_memory_helper defaults --depth "$depth")" || return 2
  memory="$(printf '%s' "$defaults" | "$VM_LIFECYCLE_PYTHON" -c '
import json,sys
try:
    data=json.load(sys.stdin)
    assert type(data.get("depth")) is int and data["depth"] == int(sys.argv[1])
    value=data["memory_gib"]
    assert type(value) is int and 1 <= value <= 128
    print(value)
except Exception:
    raise SystemExit(2)
' "$depth")" || return 2
  printf '%s\n' "$memory"
}

vm_lifecycle_memory_helper() {
  if [[ "$VM_LIFECYCLE_MEMORY_BUDGET" == *.py ]]; then
    "$VM_LIFECYCLE_PYTHON" "$VM_LIFECYCLE_MEMORY_BUDGET" "$@"
  else
    "$VM_LIFECYCLE_MEMORY_BUDGET" "$@"
  fi
}

vm_lifecycle_memory_reserve() {
  local root="$1" name="$2" depth="$3" requested_gib="${4:-}" release_profile="${5:-}"
  local memory requested_mib
  unset VM_LIFECYCLE_MEMORY_GIB VM_LIFECYCLE_MEMORY_OWNER_PID
  memory="$(vm_lifecycle_memory_gib "$depth" "$requested_gib")" || return $?
  requested_mib=$((10#$memory * 1024))
  # Do not capture this command's stdout with $(...): reservation identity is
  # bound to this long-lived controller shell and must survive the call.
  local -a args=(reserve --state-root "$root" --profile "$name" --requested-mib "$requested_mib")
  [[ -z "$release_profile" ]] || args+=(--release-profile "$release_profile")
  vm_lifecycle_memory_helper "${args[@]}" >/dev/null || return $?
  # shellcheck disable=SC2034 # The lifecycle caller reads the chosen allocation in its current shell.
  VM_LIFECYCLE_MEMORY_GIB="$memory"
  # shellcheck disable=SC2034 # Passed to the locked lifecycle worker for exact-owner release.
  VM_LIFECYCLE_MEMORY_OWNER_PID="$BASHPID"
}

vm_lifecycle_memory_release() {
  # The PID is only a lookup hint: the helper recaptures full identity and compares
  # it with the private reservation before unlinking anything.
  local root="$1" name="$2" owner_pid="${3:-${VM_LIFECYCLE_MEMORY_OWNER_PID:-$BASHPID}}"
  if vm_lifecycle_memory_helper release --state-root "$root" --profile "$name" \
    --owner-pid "$owner_pid"; then
    unset VM_LIFECYCLE_MEMORY_GIB VM_LIFECYCLE_MEMORY_OWNER_PID
    return 0
  else
    return $?
  fi
}

vm_lifecycle_memory_check() {
  local root="$1" name="$2" requested_gib="$3" requested_mib
  if [[ ! "$requested_gib" =~ ^[1-9][0-9]{0,2}$ ]] || (( 10#$requested_gib > 128 )); then
    vm_lifecycle_error 'Requested VM memory must be whole GiB from 1 to 128.'
    return 2
  fi
  requested_mib=$((10#$requested_gib * 1024))
  vm_lifecycle_memory_helper check --state-root "$root" \
    --release-profile "$name" --requested-mib "$requested_mib"
}

vm_lifecycle_memory_recover() {
  local root="$1" name="$2"
  vm_lifecycle_memory_helper recover --state-root "$root" --profile "$name"
}

vm_lifecycle_profile_exists() {
  local root="$1" name="$2" profiles
  profiles="$(vm_lifecycle_profile_list "$root")" || return 2
  printf '%s' "$profiles" | "$VM_LIFECYCLE_PYTHON" -c '
import json,sys
try:
    profiles=json.load(sys.stdin)
    assert isinstance(profiles,list)
    print("yes" if any(item.get("name")==sys.argv[1] for item in profiles) else "no")
except Exception:
    raise SystemExit(2)
' "$name"
}

vm_lifecycle_profile_allocate() {
  # Call directly in the controller shell; its reservation must outlive this function.
  local root="$1" name="$2" depth="$3" requested_gib="${4:-}"
  local raw memory prepared profiles existing created create_rc
  unset VM_LIFECYCLE_MEMORY_GIB VM_LIFECYCLE_MEMORY_OWNER_PID
  raw="$(cat)" || return 2
  memory="$(vm_lifecycle_memory_gib "$depth" "$requested_gib")" || return $?
  prepared="$(printf '%s' "$raw" | "$VM_LIFECYCLE_PYTHON" -c '
import importlib.util,json,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("vm_allocate_paths",sys.argv[1])
if spec is None or spec.loader is None: raise SystemExit(2)
paths=importlib.util.module_from_spec(spec); spec.loader.exec_module(paths)
try:
    data=json.load(sys.stdin)
    assert data.get("name")==sys.argv[3] and data.get("status")=="allocated"
    assert type(data.get("depth")) is int and data["depth"]==int(sys.argv[4])
    data["memory_gib"]=int(sys.argv[5])
    paths.profile_validate(data,Path(sys.argv[2]),sys.argv[3])
    sys.stdout.buffer.write(paths.encoded(data))
except Exception:
    raise SystemExit(2)
' "$VM_LIFECYCLE_PATHS" "$root" "$name" "$depth" "$memory")" || {
    vm_lifecycle_error 'New profile metadata failed validation; no profile or RAM reservation was created.'
    return 2
  }
  profiles="$(vm_lifecycle_profile_list "$root")" || return 2
  existing="$(printf '%s' "$profiles" | "$VM_LIFECYCLE_PYTHON" -c '
import json,sys
try:
    data=json.load(sys.stdin)
    assert isinstance(data,list)
    print("yes" if any(item.get("name")==sys.argv[1] for item in data) else "no")
except Exception:
    raise SystemExit(2)
' "$name")" || return 2
  [[ "$existing" == no ]] || { vm_lifecycle_error "Profile '$name' already exists; its state was preserved."; return 2; }

  vm_lifecycle_memory_reserve "$root" "$name" "$depth" "$memory" || return $?
  if created="$(printf '%s' "$prepared" | "$VM_LIFECYCLE_PYTHON" "$VM_LIFECYCLE_PATHS" \
    profile-create --state-root "$root" --profile "$name")"; then
    printf '%s\n' "$created"
  else
    create_rc=$?
    if ! vm_lifecycle_memory_release "$root" "$name" >/dev/null; then
      vm_lifecycle_error 'Profile creation failed and the RAM reservation could not be released; allocation remains blocked.'
      return 2
    fi
    unset VM_LIFECYCLE_MEMORY_GIB VM_LIFECYCLE_MEMORY_OWNER_PID
    return "$create_rc"
  fi
}

vm_lifecycle_identity_state() {
  local data="$1" root="$2" name="$3"
  "$VM_LIFECYCLE_PYTHON" - "$VM_LIFECYCLE_PROCESS" "$data" "$root" "$name" \
    "${VM_LIFECYCLE_PROC_ROOT:-/proc}" <<'PY'
import importlib.util, json, os, shutil, sys
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
    argv0=expected["argv"][0]
    if os.path.isabs(argv0):
        actual_executable=Path(argv0).resolve(strict=True)
    else:
        resolved=shutil.which(argv0)
        if resolved is None: raise ValueError
        actual_executable=Path(resolved).resolve(strict=True)
    recorded_executable=Path(expected["executable"]).resolve(strict=True)
    drive=f"file={disk},format=qcow2,if=virtio"
    socket=f"socket,id=charfs,path={item['socket_path']}"
    hostfwd=f"hostfwd=tcp:127.0.0.1:{item['ssh_port']}-:22"
    if (executable not in {"qemu-system-x86_64","qemu-system-x86"}
            or actual_executable != recorded_executable
            or item.get("disk_path") != str(disk)
            or drive not in expected["argv"]
            or socket not in expected["argv"]
            or not any(hostfwd in arg for arg in expected["argv"])
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

vm_lifecycle_runner_registry_state() {
  local registry="$1" run_id="$2" result
  [[ -f "$registry" && ! -L "$registry" ]] || { printf 'absent\n'; return 0; }
  result="$("$VM_LIFECYCLE_PYTHON" "$VM_LIFECYCLE_PROCESS" status \
    --registry "$registry" --run-id "$run_id" 2>/dev/null)" || {
    printf 'unknown\n'
    return 0
  }
  printf '%s' "$result" | "$VM_LIFECYCLE_PYTHON" -c '
import json,sys
try:
    state=json.load(sys.stdin)["state"]
    assert state in {"running","stopped","unknown"}
    print(state)
except Exception:
    raise SystemExit(2)
' 2>/dev/null || printf 'unknown\n'
}

vm_lifecycle_capture_runtime_identity() {
  local root="$1" name="$2" registry="$3" run_id="$4" role="$5" ssh_port="$6"
  "$VM_LIFECYCLE_PYTHON" - "$VM_LIFECYCLE_PROCESS" "$root" "$name" \
    "$registry" "$run_id" "$role" "$ssh_port" "${VM_LIFECYCLE_PROC_ROOT:-/proc}" <<'PY'
import importlib.util,json,re,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("vm_start_process",sys.argv[1])
if spec is None or spec.loader is None: raise SystemExit(2)
process=importlib.util.module_from_spec(spec); spec.loader.exec_module(process)
try:
    root=Path(sys.argv[2]); name=sys.argv[3]; registry=Path(sys.argv[4])
    run_id=sys.argv[5]; role=sys.argv[6]; ssh_port=int(sys.argv[7]); proc_root=Path(sys.argv[8])
    profile=root/"vm/profiles"/name; disk=profile/"disk.qcow2"
    record=process.registry_read(registry,run_id)
    entry=record["processes"].get(role)
    assert role.startswith("qemu_") and entry and entry["identity"] and entry["exit_code"] is None
    identity=entry["identity"]
    assert process.identity_state(identity,proc_root=proc_root)=="running"
    argv=identity["argv"]
    assert Path(identity["executable"]).name=="qemu-system-x86_64"
    argv0=Path(argv[0])
    if not argv0.is_absolute():
        import shutil
        resolved=shutil.which(argv[0])
        assert resolved
        argv0=Path(resolved)
    assert argv0.resolve(strict=True)==Path(identity["executable"]).resolve(strict=True)
    expected_drive=f"file={disk},format=qcow2,if=virtio"
    drives=[argv[index+1] for index,item in enumerate(argv[:-1]) if item=="-drive"]
    chardevs=[item for item in argv if item.startswith("socket,id=charfs,path=")]
    netdevs=[argv[index+1] for index,item in enumerate(argv[:-1]) if item=="-netdev"]
    assert expected_drive in drives and len(chardevs)==1 and len(netdevs)==1
    socket=chardevs[0].partition("path=")[2]
    matches=re.findall(r"hostfwd=tcp:127\.0\.0\.1:(\d+)-:22",netdevs[0])
    assert len(matches)==1 and int(matches[0])==ssh_port
    assert "restrict=on" in netdevs[0]
    item={"pid":identity["pid"],"start_ticks":identity["start_ticks"],
          "executable":identity["executable"],"argv":argv,
          "disk_path":str(disk),"socket_path":socket,"ssh_port":ssh_port}
    print(json.dumps(item,sort_keys=True,separators=(",",":")))
except Exception:
    raise SystemExit(2)
PY
}

vm_lifecycle_guest_readiness() {
  local root="$1" name="$2" data="$3" timeout="$4"
  local info registry run_id role run_dir key port status_json marker_json state
  info="$(vm_lifecycle_registry_for_profile "$root" "$name" "$data")" || return 2
  read -r registry run_id role < <("$VM_LIFECYCLE_PYTHON" -c '
import json,sys
d=json.loads(sys.argv[1]); print(d["registry"],d["run_id"],d["role"])
' "$info") || return 2
  run_dir="$(dirname "$registry")"
  key="$root/vm/profiles/$name/control-key"
  port="$(printf '%s' "$data" | "$VM_LIFECYCLE_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["runtime_identity"]["ssh_port"])')" || return 2
  [[ "$timeout" =~ ^[1-9][0-9]{0,3}$ ]] && (( 10#$timeout <= 1800 )) || return 2
  [[ -f "$key" && ! -L "$key" && -f "$run_dir/known_hosts" && ! -L "$run_dir/known_hosts" ]] || return 2
  # shellcheck disable=SC1091 # The frozen runtime module is selected by its exact profile root.
  source "$VM_LIFECYCLE_ROOT/lib/vm/ssh.sh"
  # shellcheck disable=SC1091 # The frozen runtime module is selected by its exact profile root.
  source "$VM_LIFECYCLE_ROOT/lib/vm/guest.sh"
  local deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    if status_json="$(vm_ssh_noninteractive "$key" "$run_dir/known_hosts" "$port" \
      15 mester@127.0.0.1 'sudo cloud-init status --long --format=json' 2>/dev/null)" &&
      marker_json="$(vm_ssh_noninteractive "$key" "$run_dir/known_hosts" "$port" \
        15 mester@127.0.0.1 'sudo cat /var/lib/lab-vm/guest-ready.json' 2>/dev/null)" &&
      vm_guest_readiness_validate 0 "$status_json" "$marker_json" \
        "$(vm_lifecycle_field "$data" guest_payload_sha256)" >/dev/null 2>&1; then
      printf '%s\n' "$marker_json"
      return 0
    fi
    state="$(vm_lifecycle_identity_state "$data" "$root" "$name")" || state=unknown
    [[ "$state" == running ]] || {
      vm_lifecycle_error 'QEMU stopped or became ambiguous before guest readiness; profile evidence was preserved.'
      return 1
    }
    sleep 2
  done
  vm_lifecycle_error 'Guest did not reach bounded cloud-init, workspace and native-harness readiness.'
  return 124
}

vm_lifecycle_runner_start_for_depth() {
  local depth="$1" maximum="$2"
  shift 2
  if (( depth > 0 )); then
    # The QEMU/proxy being started belongs to the child depth, not the current
    # launcher process. This selects the immediate parent's proxy upstream.
    LAB_VM_RECURSION_DEPTH="$depth" LAB_VM_MAX_DEPTH="$maximum" vm_runner_start "$@"
  else
    vm_runner_start "$@"
  fi
}

vm_lifecycle_start_profile_locked() {
  local root="$1" name="$2" workspace="$3" owner_pid="${4:-}"
  local model_kind="${5:-none}" model_port="${6:-}" metrics_port="${7:-}"
  local readiness_timeout="${8:-1800}"
  local profile data status memory run_id runtime_identity patch runner_rc state marker version
  local profile_depth profile_max_depth
  profile="$root/vm/profiles/$name"
  data="$(vm_lifecycle_profile_read "$root" "$name")" || return 2
  status="$(vm_lifecycle_field "$data" status)" || return 2
  [[ "$status" == starting ]] || { vm_lifecycle_error 'Only an exact starting profile may enter QEMU startup.'; return 2; }
  [[ "$workspace" == "$(vm_lifecycle_field "$data" workspace)" ]] || {
    vm_lifecycle_error 'Selected workspace must be committed before startup under the profile lock.'
    return 2
  }
  [[ -f "$profile/seed.iso" && ! -L "$profile/seed.iso" &&
    -f "$profile/disk.qcow2" && ! -L "$profile/disk.qcow2" &&
    -f "$profile/control-key" && ! -L "$profile/control-key" ]] || {
    vm_lifecycle_transition "$root" "$name" starting failed '{}' >/dev/null || true
    [[ -z "$owner_pid" ]] || vm_lifecycle_memory_release "$root" "$name" "$owner_pid" >/dev/null || true
    vm_lifecycle_error 'Exact profile seed, overlay or control key is missing; profile data was preserved.'
    return 2
  }
  memory="$(vm_lifecycle_field "$data" memory_gib)" || return 2
  if ! declare -F vm_runner_start >/dev/null; then
    # shellcheck disable=SC1091 # A frozen lifecycle module loads its fixed sibling runner.
    source "$VM_LIFECYCLE_ROOT/lib/vm/runner.sh"
  fi
  if ! run_id="$("$VM_LIFECYCLE_PYTHON" -c 'import uuid; print(uuid.uuid4())')"; then
    vm_lifecycle_transition "$root" "$name" starting failed '{}' >/dev/null || true
    [[ -z "$owner_pid" ]] || vm_lifecycle_memory_release "$root" "$name" "$owner_pid" >/dev/null || true
    return 2
  fi
  profile_depth="$(vm_lifecycle_field "$data" depth)" || return 2
  profile_max_depth="$(vm_lifecycle_field "$data" max_depth)" || return 2
  [[ "$profile_depth" == 0 ]] || metrics_port=''
  if vm_lifecycle_runner_start_for_depth "$profile_depth" "$profile_max_depth" \
    "$profile" "$run_id" "$name" "$workspace" \
    "$profile/seed.iso" "$profile/disk.qcow2" "$memory" \
    "$model_kind" "$model_port" "$metrics_port"; then
    runner_rc=0
  else
    runner_rc=$?
  fi
  if (( runner_rc != 0 )); then
    state=absent
    if [[ -n "${VM_RUNNER_PROCESS_REGISTRY:-}" ]]; then
      state="$(vm_lifecycle_runner_registry_state "$VM_RUNNER_PROCESS_REGISTRY" "$run_id")" || state=unknown
    fi
    case "$state" in
      stopped|absent)
        vm_lifecycle_transition "$root" "$name" starting failed '{}' >/dev/null || true
        [[ -z "$owner_pid" ]] || vm_lifecycle_memory_release "$root" "$name" "$owner_pid" >/dev/null || true
        ;;
      *)
        vm_lifecycle_transition "$root" "$name" starting unknown '{}' >/dev/null || true
        vm_lifecycle_error 'Startup rollback could not prove every run-owned process stopped; RAM reservation and profile were preserved.'
        ;;
    esac
    return "$runner_rc"
  fi

  if ! runtime_identity="$(vm_lifecycle_capture_runtime_identity "$root" "$name" \
    "$VM_RUNNER_PROCESS_REGISTRY" "$run_id" "$VM_RUNNER_QEMU_ROLE" "$VM_RUNNER_SSH_PORT")"; then
    vm_lifecycle_transition "$root" "$name" starting unknown '{}' >/dev/null || true
    vm_lifecycle_error 'QEMU started but exact runtime identity could not be recorded; reservation was preserved.'
    return 2
  fi
  patch="$(printf '{"runtime_identity":%s}' "$runtime_identity")"
  if ! vm_lifecycle_transition "$root" "$name" starting provisioning "$patch" >/dev/null; then
    vm_lifecycle_transition "$root" "$name" starting unknown '{}' >/dev/null || true
    vm_lifecycle_error 'QEMU is running but profile identity could not be committed; reservation was preserved.'
    return 2
  fi
  data="$(vm_lifecycle_profile_read "$root" "$name")" || return 2

  if marker="$(vm_lifecycle_guest_readiness "$root" "$name" "$data" "$readiness_timeout")"; then
    version="$(printf '%s' "$marker" | "$VM_LIFECYCLE_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["opencode_version"])')" || version=''
  else
    runner_rc=$?
    vm_lifecycle_guest_shutdown "$root" "$name" "$data" >/dev/null 2>&1 || true
    if state="$(vm_lifecycle_wait_qemu_stopped "$root" "$name" "$data" "$VM_LIFECYCLE_DEFAULT_TIMEOUT")"; then
      local stopped_registry="${state%%$'\n'*}" stopped_run_id="${state#*$'\n'}"
      if vm_lifecycle_stop_sidecars "$stopped_registry" "$stopped_run_id" &&
        vm_lifecycle_transition "$root" "$name" provisioning failed \
          '{"runtime_identity":null}' >/dev/null; then
        [[ -z "$owner_pid" ]] || vm_lifecycle_memory_release "$root" "$name" "$owner_pid" >/dev/null || true
        return "$runner_rc"
      fi
    fi
    vm_lifecycle_transition "$root" "$name" provisioning unknown '{}' >/dev/null || true
    vm_lifecycle_error 'Guest readiness failed and graceful cleanup was not fully verified; profile and RAM reservation were preserved.'
    return "$runner_rc"
  fi
  [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$ ]] || {
    vm_lifecycle_transition "$root" "$name" provisioning unknown '{}' >/dev/null || true
    vm_lifecycle_error 'Guest readiness returned an invalid exact harness version; profile and reservation were preserved.'
    return 2
  }
  vm_lifecycle_transition "$root" "$name" provisioning ready \
    "$(printf '{"opencode_version":"%s"}' "$version")" >/dev/null || return 2
  if [[ -n "$owner_pid" ]] && ! vm_lifecycle_memory_release "$root" "$name" "$owner_pid" >/dev/null; then
    vm_lifecycle_error 'VM is ready but its exact RAM reservation could not be released; resource accounting remains conservatively blocked.'
    return 2
  fi
  printf 'Profile %s is ready at workspace %s.\n' "$name" "$workspace"
}

vm_lifecycle_prepare_profile_assets_locked() {
  local root="$1" name="$2" workspace="$3" depth="$4" max_depth="$5"
  local workload="$6" agent="$7" version="$8" expected_payload="$9"
  local uid="${10}" gid="${11}" model_provider="${12:-none}"
  local model_port="${13:-}" model_id="${14:-}"
  local data status profile actual_payload stage_dir private_key public_key user_data path
  profile="$root/vm/profiles/$name"
  data="$(vm_lifecycle_profile_read "$root" "$name")" || return 2
  status="$(vm_lifecycle_field "$data" status)" || return 2
  [[ "$status" == starting ]] || { vm_lifecycle_error 'Asset preparation requires the exact starting profile lock.'; return 2; }
  for path in "$profile/seed.user-data" "$profile/seed.iso" "$profile/control-key"; do
    [[ ! -e "$path" && ! -L "$path" ]] || {
      vm_lifecycle_error 'An exact profile startup asset already exists; it was preserved.'
      return 2
    }
  done
  if ! declare -F vm_disk_create_overlay >/dev/null; then
    # shellcheck disable=SC1091 # Initial startup uses only the frozen profile payload.
    source "$VM_LIFECYCLE_ROOT/lib/vm/disk.sh"
  fi
  if ! declare -F vm_cloud_init_user_data >/dev/null; then
    # shellcheck disable=SC1091 # Initial startup uses only the frozen profile payload.
    source "$VM_LIFECYCLE_ROOT/lib/vm/cloud-init.sh"
  fi
  if ! declare -F vm_guest_payload_hash >/dev/null; then
    # shellcheck disable=SC1091 # Initial startup uses only the frozen profile payload.
    source "$VM_LIFECYCLE_ROOT/lib/vm/guest.sh"
  fi
  actual_payload="$(vm_guest_payload_hash "$workspace" "$workload" "$agent" \
    "$version" "$depth" "$max_depth" "$model_provider" "$model_port" "$model_id")" || {
    vm_lifecycle_error 'Workspace guest payload could not be revalidated; no seed or key was written.'
    return 2
  }
  [[ "$actual_payload" == "$expected_payload" ]] || {
    vm_lifecycle_error 'Workspace guest payload changed after allocation; startup assets were not written.'
    return 2
  }
  vm_disk_create_overlay "$root" "$name" >/dev/null || return $?
  stage_dir="$(mktemp -d "$profile/.asset.XXXXXX")" || return 1
  chmod 700 "$stage_dir" || return 1
  private_key="$stage_dir/control-key"
  if ! ssh-keygen -q -t ed25519 -N '' -C "little-mester-$name" -f "$private_key"; then
    rm -f -- "$private_key" "$private_key.pub"
    rmdir -- "$stage_dir" 2>/dev/null || true
    vm_lifecycle_error 'Could not create the private profile SSH key; profile assets were preserved.'
    return 1
  fi
  chmod 600 "$private_key" "$private_key.pub" || return 1
  public_key="$(cat -- "$private_key.pub")" || return 1
  user_data="$(vm_cloud_init_user_data "$name" "$uid" "$gid" "$public_key" \
    "$depth" "$max_depth" "$workload" "$agent" "$version" "$expected_payload" \
    http://10.0.2.100:3128 "$model_provider" "$model_port" "$model_id")" || {
    rm -f -- "$private_key" "$private_key.pub"
    rmdir -- "$stage_dir" 2>/dev/null || true
    return 2
  }
  if ! printf '%s' "$user_data" | vm_disk_write_seed "$root" "$name" >/dev/null; then
    rm -f -- "$private_key" "$private_key.pub"
    rmdir -- "$stage_dir" 2>/dev/null || true
    return 2
  fi
  local profile_id iso_tool
  profile_id="$(vm_lifecycle_field "$data" profile_id)" || return 2
  printf 'instance-id: iid-%s\nlocal-hostname: %s\n' "$profile_id" "$name" \
    > "$stage_dir/meta-data"
  chmod 600 "$stage_dir/meta-data"
  install -m 600 "$profile/seed.user-data" "$stage_dir/user-data"
  iso_tool="$(command -v genisoimage || command -v mkisofs || true)"
  [[ -n "$iso_tool" ]] || {
    rm -f -- "$private_key" "$private_key.pub" "$stage_dir/meta-data" "$stage_dir/user-data"
    rmdir -- "$stage_dir" 2>/dev/null || true
    vm_lifecycle_error 'genisoimage or mkisofs is required to create the NoCloud seed; profile user-data was preserved.'
    return 2
  }
  if ! "$iso_tool" -quiet -output "$stage_dir/seed.iso" -volid cidata -joliet -rock \
    "$stage_dir/user-data" "$stage_dir/meta-data" >/dev/null 2>&1; then
    rm -f -- "$private_key" "$private_key.pub" "$stage_dir/meta-data" "$stage_dir/user-data" "$stage_dir/seed.iso"
    rmdir -- "$stage_dir" 2>/dev/null || true
    vm_lifecycle_error 'NoCloud seed ISO creation failed; existing profile data was preserved.'
    return 1
  fi
  chmod 600 "$stage_dir/seed.iso" || return 1
  "$VM_LIFECYCLE_PYTHON" "$VM_LIFECYCLE_PATHS" asset-publish --state-root "$root" --profile "$name" \
    --asset control-key --staging "$private_key" >/dev/null || return 2
  "$VM_LIFECYCLE_PYTHON" "$VM_LIFECYCLE_PATHS" asset-publish --state-root "$root" --profile "$name" \
    --asset seed.iso --staging "$stage_dir/seed.iso" >/dev/null || return 2
  rm -f -- "$private_key.pub" "$stage_dir/meta-data" "$stage_dir/user-data"
  rmdir -- "$stage_dir" || {
    vm_lifecycle_error 'Startup assets were published but private staging cleanup could not be verified.'
    return 2
  }
}

vm_lifecycle_switch_seed_locked() {
  local root="$1" name="$2" workspace="$3" depth="$4" max_depth="$5"
  local workload="$6" agent="$7" version="$8" payload="$9"
  local uid="${10}" gid="${11}"
  local model_provider="${12:-none}" model_port="${13:-}" model_id="${14:-}"
  local profile="$root/vm/profiles/$name" data status old_workspace key public_key user_data
  local instance_id stage_dir iso_tool
  data="$(vm_lifecycle_profile_read "$root" "$name")" || return 2
  status="$(vm_lifecycle_field "$data" status)" || return 2
  old_workspace="$(vm_lifecycle_field "$data" workspace)" || return 2
  [[ "$status" == suspended && "$workspace" != "$old_workspace" &&
    "$payload" =~ ^[a-f0-9]{64}$ ]] || {
    vm_lifecycle_error 'Workspace seed replacement requires a verified suspended profile and a new payload digest.'
    return 2
  }
  key="$profile/control-key"
  [[ -f "$key" && ! -L "$key" && "$(stat -c '%a' -- "$key")" == 600 &&
    -f "$profile/seed.iso" && ! -L "$profile/seed.iso" ]] || {
    vm_lifecycle_error 'The exact profile SSH key or prior NoCloud seed is missing; workspace was not booted.'
    return 2
  }
  if ! declare -F vm_cloud_init_user_data >/dev/null; then
    # shellcheck disable=SC1091 # Workspace changes use only the frozen profile guest payload.
    source "$VM_LIFECYCLE_ROOT/lib/vm/cloud-init.sh"
  fi
  public_key="$(ssh-keygen -y -P '' -f "$key")" || {
    vm_lifecycle_error 'The existing profile control key could not be verified; it was preserved.'
    return 2
  }
  user_data="$(vm_cloud_init_user_data "$name" "$uid" "$gid" "$public_key" \
    "$depth" "$max_depth" "$workload" "$agent" "$version" "$payload" \
    http://10.0.2.100:3128 "$model_provider" "$model_port" "$model_id")" || return 2
  instance_id="$("$VM_LIFECYCLE_PYTHON" -c 'import uuid; print(uuid.uuid4())')" || return 2
  stage_dir="$(mktemp -d "$profile/.asset.XXXXXX")" || return 1
  if ! chmod 700 "$stage_dir" ||
    ! printf 'instance-id: iid-%s\nlocal-hostname: %s\n' "$instance_id" "$name" > "$stage_dir/meta-data" ||
    ! printf '%s' "$user_data" > "$stage_dir/user-data" ||
    ! chmod 600 "$stage_dir/meta-data" "$stage_dir/user-data"; then
    rm -f -- "$stage_dir/meta-data" "$stage_dir/user-data"
    rmdir -- "$stage_dir" 2>/dev/null || true
    vm_lifecycle_error 'Private NoCloud staging could not be prepared; the prior seed was preserved.'
    return 1
  fi
  iso_tool="$(command -v genisoimage || command -v mkisofs || true)"
  [[ -n "$iso_tool" ]] || {
    rm -f -- "$stage_dir/meta-data" "$stage_dir/user-data"
    rmdir -- "$stage_dir" 2>/dev/null || true
    vm_lifecycle_error 'genisoimage or mkisofs is required for a workspace switch; prior seed was preserved.'
    return 2
  }
  if ! "$iso_tool" -quiet -output "$stage_dir/seed.iso" -volid cidata -joliet -rock \
    "$stage_dir/user-data" "$stage_dir/meta-data" >/dev/null 2>&1; then
    rm -f -- "$stage_dir/meta-data" "$stage_dir/user-data" "$stage_dir/seed.iso"
    rmdir -- "$stage_dir" 2>/dev/null || true
    vm_lifecycle_error 'NoCloud seed build failed; the prior ISO and overlay were preserved.'
    return 1
  fi
  if ! chmod 600 "$stage_dir/seed.iso"; then
    rm -f -- "$stage_dir/meta-data" "$stage_dir/user-data" "$stage_dir/seed.iso"
    rmdir -- "$stage_dir" 2>/dev/null || true
    vm_lifecycle_error 'NoCloud seed permissions could not be set; the prior seed was preserved.'
    return 1
  fi
  local patch
  patch="$("$VM_LIFECYCLE_PYTHON" - "$workspace" "$payload" <<'PY'
import json,sys
print(json.dumps({"workspace":sys.argv[1],"guest_payload_sha256":sys.argv[2],
                  "runtime_identity":None},sort_keys=True,separators=(",",":")))
PY
  )" || {
    rm -f -- "$stage_dir/meta-data" "$stage_dir/user-data" "$stage_dir/seed.iso"
    rmdir -- "$stage_dir" 2>/dev/null || true
    return 2
  }
  if ! vm_lifecycle_transition "$root" "$name" suspended starting "$patch" >/dev/null; then
    rm -f -- "$stage_dir/meta-data" "$stage_dir/user-data" "$stage_dir/seed.iso"
    rmdir -- "$stage_dir" 2>/dev/null || true
    vm_lifecycle_error 'Workspace switch state could not be committed; old workspace and seed were preserved.'
    return 2
  fi
  if ! "$VM_LIFECYCLE_PYTHON" "$VM_LIFECYCLE_PATHS" asset-replace \
    --state-root "$root" --profile "$name" --asset seed.iso \
    --staging "$stage_dir/seed.iso" --user-data-staging "$stage_dir/user-data" >/dev/null; then
    rm -f -- "$stage_dir/meta-data" "$stage_dir/user-data" "$stage_dir/seed.iso"
    rmdir -- "$stage_dir" 2>/dev/null || true
    vm_lifecycle_error 'NoCloud seed replacement failed; the overlay and prior boot ISO were preserved.'
    return 2
  fi
  rm -f -- "$stage_dir/meta-data" "$stage_dir/user-data"
  rmdir -- "$stage_dir" || {
    vm_lifecycle_error 'Workspace seed was replaced but private staging cleanup remains uncertain.'
    return 2
  }
}

vm_lifecycle_prepare_and_start_locked() {
  local root="$1" name="$2" workspace="$3" depth="$4" max_depth="$5"
  local workload="$6" agent="$7" version="$8" expected_payload="$9"
  local uid="${10}" gid="${11}" owner_pid="${12}"
  local model_kind="${13:-none}" model_port="${14:-}" metrics_port="${15:-}"
  local readiness_timeout="${16:-1800}" model_provider="${17:-none}" model_id="${18:-}" rc
  if vm_lifecycle_prepare_profile_assets_locked "$root" "$name" "$workspace" \
    "$depth" "$max_depth" "$workload" "$agent" "$version" "$expected_payload" \
    "$uid" "$gid" "$model_provider" "$model_port" "$model_id"; then
    :
  else
    rc=$?
    vm_lifecycle_transition "$root" "$name" starting failed '{}' >/dev/null || true
    [[ -z "$owner_pid" ]] || vm_lifecycle_memory_release "$root" "$name" "$owner_pid" >/dev/null || true
    return "$rc"
  fi
  vm_lifecycle_start_profile_locked "$root" "$name" "$workspace" "$owner_pid" \
    "$model_kind" "$model_port" "$metrics_port" "$readiness_timeout"
}

vm_lifecycle_install_runtime() {
  local root="$1" name="$2" source_root="$3"
  local -a files=(
    lab-vm lib/state.sh lib/vm/config.sh lib/vm/cloud-init.sh lib/vm/disk.sh
    lib/vm/recursion.sh
    lib/vm/auth.sh lib/vm/browser-bridge-server.py lib/vm/browser-open.py
    lib/vm/browser-open.sh lib/vm/guest-bootstrap.sh lib/vm/guest.sh
    lib/vm/lifecycle.sh lib/vm/paths.py lib/vm/process.py lib/vm/proxy.py
    lib/vm/runner.sh lib/vm/services.sh lib/vm/ssh.sh
    scripts/vm-opencode-config.py scripts/vm-python-tools.sh
    scripts/vm-recursion-live-children.py scripts/vm-recursion-workspace.py \
    scripts/vm-self-test.py scripts/vm_memory_budget.py
    lib/vm/metrics_service.py agent/metrics/gather.py
  )
  "$VM_LIFECYCLE_PYTHON" "$VM_LIFECYCLE_PATHS" runtime-install --state-root "$root" --profile "$name" \
    --source "$source_root" --files "${files[@]}"
}

vm_lifecycle_json_field() {
  local data="$1" expression="$2"
  printf '%s' "$data" | "$VM_LIFECYCLE_PYTHON" -c '
import json,sys
try:
    value=json.loads(sys.stdin.read())
    for key in sys.argv[1].split("."):
        value=value[key]
    if value is None: print("")
    elif isinstance(value,bool): print("true" if value else "false")
    elif isinstance(value,(str,int)): print(value)
    else: raise ValueError
except Exception:
    raise SystemExit(2)
' "$expression"
}

vm_lifecycle_metrics_select_port() {
  local root="$1" runtime="$2" config="$3" depth="$4"
  local enabled service collector port
  if [[ "$depth" != 0 ]]; then
    # No ancestor-host metrics are forwarded into nested child profiles.
    printf '\n'
    return 0
  fi
  enabled="$(vm_lifecycle_json_field "$config" metrics 2>/dev/null || true)"
  case "$enabled" in
    off) printf '\n'; return 0 ;;
    on)
      service="$runtime/lib/vm/metrics_service.py"
      collector="$runtime/agent/metrics/gather.py"
      if [[ -f "$service" && ! -L "$service" && -f "$collector" && ! -L "$collector" ]]; then
        port="$("$VM_LIFECYCLE_PYTHON" "$service" ensure --state-root "$root" \
          --port 19532 --collector "$collector" 2>/dev/null)" || port=''
      else
        port=''
      fi
      if [[ "$port" != 19532 ]]; then
        printf 'WARNING: host metrics service is unavailable or unverified; VM startup will continue without the metrics forward.\n' >&2
        printf '\n'
      else
        printf '%s\n' "$port"
      fi
      ;;
    *) printf '\n' ;;
  esac
}

vm_lifecycle_up() {
  local root="$1" name="$2" requested_workspace="$3" repository="$4"
  local depth="$5" max_depth="$6" requested_memory="$7" recurse="$8" unsafe="$9"
  local recursive="${10}" timeout="${11:-$VM_LIFECYCLE_DEFAULT_TIMEOUT}"
  local readiness_timeout="${12:-1800}" config workspace exists workload agent version provider model_port
  local model_kind=none metrics_port='' memory payload data runtime_info runtime_hash patch owner_pid rc model_id
  [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$ ]] || {
    vm_lifecycle_error 'New VM profile names must be valid cloud-init hostnames.'
    return 2
  }
  config="${VM_CONFIG_JSON:-}"
  [[ -n "$config" ]] || config='{}'
  workload="$(vm_lifecycle_json_field "$config" workload_runtime)" || return 2
  agent="$(vm_lifecycle_json_field "$config" agent)" || return 2
  version="$(vm_lifecycle_json_field "$config" opencode_version)" || return 2
  provider="$(vm_lifecycle_json_field "$config" model.provider)" || return 2
  model_port="$(vm_lifecycle_json_field "$config" model.port)" || return 2
  model_id="$(vm_lifecycle_json_field "$config" model.id 2>/dev/null || true)"
  case "$provider" in
    none|remote)
      [[ -z "$model_port" ]] || { vm_lifecycle_error 'A model port is not valid for this provider.'; return 2; }
      model_kind=none
      ;;
    ollama|llamacpp)
      if [[ ! "$model_port" =~ ^[1-9][0-9]{0,4}$ ]] || (( 10#$model_port > 65535 )); then
        vm_lifecycle_error 'A valid explicit loopback port is required for the selected local model.'
        return 2
      fi
      model_kind="$provider"
      ;;
    *) vm_lifecycle_error 'Unsupported model provider.'; return 2 ;;
  esac
  exists="$(vm_lifecycle_profile_exists "$root" "$name")" || return 2
  if [[ "$exists" == yes ]]; then
    vm_lifecycle_resume "$root" "$name" "$requested_workspace" "$repository" \
      "$recurse" "$unsafe" "$recursive" "$timeout" "$model_kind" \
      "$model_port" "$metrics_port" "$readiness_timeout" "$workload" "$agent" "$version" \
      "$provider" "$model_id"
    return $?
  fi
  workspace="$(vm_lifecycle_workspace_resolve "$root" "$requested_workspace" "$repository" \
    "$recurse" "$unsafe")" || {
    vm_lifecycle_error 'Selected workspace failed canonical host policy; no profile, image or process was created.'
    return 2
  }
  if declare -F vm_setup >/dev/null; then
    vm_setup || return $?
  fi
  case "$agent" in
    opencode|aider) ;;
    *) vm_lifecycle_error 'Unsupported native guest harness.'; return 2 ;;
  esac
  case "$workload" in docker|none) ;; *) vm_lifecycle_error 'Unsupported guest workload runtime.'; return 2 ;; esac
  if [[ ! "$depth" =~ ^(0|[1-9][0-9]?)$ ]] || (( 10#$depth > 32 )) ||
    [[ ! "$max_depth" =~ ^(0|[1-9][0-9]?)$ ]] || (( 10#$max_depth > 32 || 10#$depth > 10#$max_depth )); then
    vm_lifecycle_error 'VM recursion depth and maximum are invalid.'
    return 2
  fi
  memory="$(vm_lifecycle_memory_gib "$depth" "$requested_memory")" || return $?
  if ! declare -F vm_guest_payload_hash >/dev/null; then
    # shellcheck disable=SC1091 # Source is read only for the first-install payload digest.
    source "$VM_LIFECYCLE_ROOT/lib/vm/guest.sh"
  fi
  payload="$(vm_guest_payload_hash "$workspace" "$workload" "$agent" \
    "$version" "$depth" "$max_depth" "$provider" "$model_port" "$model_id")" || {
    vm_lifecycle_error 'Selected workspace does not contain a valid native guest payload.'
    return 2
  }
  data="$("$VM_LIFECYCLE_PYTHON" - "$root" "$name" "$workspace" \
    "$VM_DISK_PIN_SHA256" "$memory" "$depth" "$max_depth" "$payload" <<'PY'
import json,sys,uuid
root,name,workspace,image,memory,depth,maximum,payload=sys.argv[1:]
data={"schema_version":1,"profile_id":str(uuid.uuid4()),"name":name,
      "workspace":workspace,"image_sha256":image,
      "image_path":f"{root}/vm/images/{image}/base.qcow2",
      "memory_gib":int(memory),"depth":int(depth),"max_depth":int(maximum),
      "descendants_possible":int(depth)<int(maximum),"opencode_version":None,
      "guest_payload_sha256":payload,"runtime_payload_sha256":None,"status":"allocated"}
print(json.dumps(data,sort_keys=True,separators=(",",":")))
PY
  )" || return 2
  vm_lifecycle_profile_allocate "$root" "$name" "$depth" "$memory" <<<"$data" >/dev/null || return $?
  owner_pid="$VM_LIFECYCLE_MEMORY_OWNER_PID"
  if vm_disk_create_base "$root" >/dev/null; then
    :
  else
    rc=$?
    vm_lifecycle_transition "$root" "$name" allocated failed '{}' >/dev/null || true
    vm_lifecycle_memory_release "$root" "$name" "$owner_pid" >/dev/null || true
    return "$rc"
  fi
  if runtime_info="$(vm_lifecycle_install_runtime "$root" "$name" "$VM_LIFECYCLE_ROOT")"; then
    runtime_hash="$(printf '%s' "$runtime_info" | "$VM_LIFECYCLE_PYTHON" -c '
import json,sys
try:
    value=json.load(sys.stdin)["runtime_payload_sha256"]
    assert isinstance(value,str) and len(value)==64
    print(value)
except Exception: raise SystemExit(2)
')" || runtime_hash=''
  else
    rc=$?
    vm_lifecycle_transition "$root" "$name" allocated failed '{}' >/dev/null || true
    vm_lifecycle_memory_release "$root" "$name" "$owner_pid" >/dev/null || true
    return "$rc"
  fi
  [[ "$runtime_hash" =~ ^[a-f0-9]{64}$ ]] || {
    vm_lifecycle_transition "$root" "$name" allocated failed '{}' >/dev/null || true
    vm_lifecycle_memory_release "$root" "$name" "$owner_pid" >/dev/null || true
    vm_lifecycle_error 'Frozen profile runtime did not return a valid payload digest.'
    return 2
  }
  patch="$(printf '{"runtime_payload_sha256":"%s"}' "$runtime_hash")"
  if ! vm_lifecycle_transition "$root" "$name" allocated starting "$patch" >/dev/null; then
    vm_lifecycle_error 'Profile startup transition failed; allocated profile and reservation were preserved.'
    return 2
  fi
  local runtime
  runtime="$(vm_lifecycle_profile_runtime "$root" "$name")" || {
    vm_lifecycle_transition "$root" "$name" starting failed '{}' >/dev/null || true
    vm_lifecycle_memory_release "$root" "$name" "$owner_pid" >/dev/null || true
    return 2
  }
  metrics_port="$(vm_lifecycle_metrics_select_port "$root" "$runtime" "$config" "$depth" "$metrics_port")" || return 2
  local uid gid
  uid="$(id -u)" && gid="$(id -g)" || return 2
  if vm_lifecycle_run_frozen_worker "$root" "$name" "$runtime" \
    vm_lifecycle_prepare_and_start_locked "$root" "$name" "$workspace" "$depth" \
    "$max_depth" "$workload" "$agent" "$version" "$payload" "$uid" "$gid" \
    "$owner_pid" "$model_kind" "$model_port" "$metrics_port" "$readiness_timeout" \
    "$provider" "$model_id"; then
    return 0
  else
    rc=$?
  fi
  local after after_status
  after="$(vm_lifecycle_profile_read "$root" "$name" 2>/dev/null || true)"
  after_status="$(vm_lifecycle_field "$after" status 2>/dev/null || true)"
  case "$after_status" in
    ready|suspended|failed)
      vm_lifecycle_memory_release "$root" "$name" "$owner_pid" >/dev/null || true
      ;;
    *) ;;
  esac
  return "$rc"
}

vm_lifecycle_resume_locked() {
  local root="$1" name="$2" requested_workspace="${3:-}" repository="$4"
  local recurse="${5:-0}" unsafe="${6:-0}" recursive="${7:-0}"
  local timeout="${8:-$VM_LIFECYCLE_DEFAULT_TIMEOUT}" owner_pid="${9:-}"
  local model_kind="${10:-none}" model_port="${11:-}" metrics_port="${12:-}"
  local readiness_timeout="${13:-1800}" workload="${14:-docker}"
  local agent="${15:-opencode}" version="${16:-latest}"
  local model_provider="${17:-$model_kind}" model_id="${18:-}"
  local data status stored_workspace workspace identity_state marker
  local depth max_depth payload uid gid
  data="$(vm_lifecycle_profile_read "$root" "$name")" || return 2
  status="$(vm_lifecycle_field "$data" status)" || return 2
  stored_workspace="$(vm_lifecycle_field "$data" workspace)" || return 2
  workspace="${requested_workspace:-$stored_workspace}"
  if [[ -z "$requested_workspace" && ! -d "$stored_workspace" ]]; then
    vm_lifecycle_error 'Recorded workspace is missing; resume requires an explicit valid --workspace selection.'
    return 2
  fi
  workspace="$(vm_lifecycle_workspace_resolve "$root" "$workspace" "$repository" 1 0)" || return 2
  if [[ "$workspace" != "$stored_workspace" ]]; then
    workspace="$(vm_lifecycle_workspace_resolve "$root" "$workspace" "$repository" \
      "$recurse" "$unsafe")" || return 2
    if ! declare -F vm_guest_payload_hash >/dev/null; then
      # shellcheck disable=SC1091 # Switching hashes use the integrity-validated frozen guest payload.
      source "$VM_LIFECYCLE_ROOT/lib/vm/guest.sh"
    fi
    depth="$(vm_lifecycle_field "$data" depth)" || return 2
    max_depth="$(vm_lifecycle_field "$data" max_depth)" || return 2
    payload="$(vm_guest_payload_hash "$workspace" "$workload" "$agent" \
      "$version" "$depth" "$max_depth" "$model_provider" "$model_port" "$model_id")" || {
      vm_lifecycle_error 'Selected workspace does not contain a valid frozen guest payload; current VM state was preserved.'
      return 2
    }
  fi

  case "$status" in
    ready)
      identity_state="$(vm_lifecycle_identity_state "$data" "$root" "$name")" || identity_state=unknown
      if [[ "$identity_state" != running ]]; then
        vm_lifecycle_transition "$root" "$name" ready unknown '{}' >/dev/null || true
        vm_lifecycle_error 'Ready profile process identity is stopped or ambiguous; profile is blocked as unknown.'
        return 2
      fi
      if [[ "$workspace" == "$stored_workspace" ]]; then
        marker="$(vm_lifecycle_guest_readiness "$root" "$name" "$data" 15)" || {
          vm_lifecycle_error 'Warm readiness verification failed; no bootstrap or repair was attempted.'
          return 1
        }
        printf 'Profile %s is already ready at workspace %s; no bootstrap or restart was performed.\n' "$name" "$workspace"
        return 0
      fi
      vm_lifecycle_suspend_locked "$root" "$name" "$recursive" "$timeout" \
        "$root/vm/profiles/$name/runtime/lab-vm" || return $?
      data="$(vm_lifecycle_profile_read "$root" "$name")" || return 2
      status="$(vm_lifecycle_field "$data" status)" || return 2
      ;;
    suspended) ;;
    *)
      vm_lifecycle_error "Profile '$name' is in '$status'; resume is allowed only for ready or suspended profiles."
      return 2
      ;;
  esac

  [[ "$status" == suspended ]] || {
    vm_lifecycle_error 'Workspace switch did not finish in the exact suspended state; no restart was attempted.'
    return 2
  }
  identity_state="$(vm_lifecycle_identity_state "$data" "$root" "$name")" || identity_state=unknown
  if [[ "$identity_state" != stopped ]]; then
    vm_lifecycle_transition "$root" "$name" suspended unknown '{}' >/dev/null || true
    vm_lifecycle_error 'Suspended profile still has a live or ambiguous QEMU identity; resume is blocked.'
    return 2
  fi
  if [[ "$workspace" != "$stored_workspace" ]]; then
    uid="$(id -u)" && gid="$(id -g)" || return 2
    if vm_lifecycle_switch_seed_locked "$root" "$name" "$workspace" \
      "$depth" "$max_depth" "$workload" "$agent" "$version" "$payload" "$uid" "$gid" \
      "$model_provider" "$model_port" "$model_id"; then
      :
    else
      local seed_rc=$?
      local after_switch after_switch_status
      after_switch="$(vm_lifecycle_profile_read "$root" "$name" 2>/dev/null || true)"
      after_switch_status="$(vm_lifecycle_field "$after_switch" status 2>/dev/null || true)"
      if [[ "$after_switch_status" == starting ]] &&
        vm_lifecycle_transition "$root" "$name" starting failed '{}' >/dev/null; then
        [[ -z "$owner_pid" ]] || vm_lifecycle_memory_release "$root" "$name" "$owner_pid" >/dev/null || true
      fi
      return "$seed_rc"
    fi
  else
    vm_lifecycle_transition "$root" "$name" suspended starting '{"runtime_identity":null}' >/dev/null || return 2
  fi
  vm_lifecycle_start_profile_locked "$root" "$name" "$workspace" "$owner_pid" \
    "$model_kind" "$model_port" "$metrics_port" "$readiness_timeout"
}

vm_lifecycle_resize() {
  local root="$1" name="$2" requested_memory="$3" recursive="${4:-0}"
  local timeout="${5:-$VM_LIFECYCLE_DEFAULT_TIMEOUT}" owner_pid="${6:-}"
  local model_kind="${7:-none}" model_port="${8:-}" metrics_port="${9:-}"
  local readiness_timeout="${10:-1800}" data status identity_state current_memory
  local patch workspace runtime rc
  # shellcheck disable=SC2034 # The memory helper reads this dynamically scoped frozen-runtime path.
  local VM_LIFECYCLE_MEMORY_BUDGET
  if [[ ! "$requested_memory" =~ ^[1-9][0-9]{0,2}$ ]] || (( 10#$requested_memory > 128 )); then
    vm_lifecycle_error 'Requested VM memory must be whole GiB from 1 to 128.'
    return 2
  fi
  data="$(vm_lifecycle_profile_read "$root" "$name")" || return 2
  status="$(vm_lifecycle_field "$data" status)" || return 2
  current_memory="$(vm_lifecycle_field "$data" memory_gib)" || return 2
  workspace="$(vm_lifecycle_field "$data" workspace)" || return 2
  workspace="$(vm_lifecycle_workspace_resolve "$root" "$workspace" "$workspace" 1 1)" || {
    vm_lifecycle_error 'Recorded workspace is missing or no longer passes host path policy; resize did not shut down the guest.'
    return 2
  }
  case "$status" in
    ready)
      identity_state="$(vm_lifecycle_identity_state "$data" "$root" "$name")" || identity_state=unknown
      [[ "$identity_state" == running ]] || {
        vm_lifecycle_transition "$root" "$name" ready unknown '{}' >/dev/null || true
        vm_lifecycle_error 'Ready profile identity is stopped or ambiguous; resize is blocked.'
        return 2
      }
      ;;
    suspended)
      identity_state="$(vm_lifecycle_identity_state "$data" "$root" "$name")" || identity_state=unknown
      [[ "$identity_state" == stopped ]] || {
        vm_lifecycle_transition "$root" "$name" suspended unknown '{}' >/dev/null || true
        vm_lifecycle_error 'Suspended profile identity is live or ambiguous; resize is blocked.'
        return 2
      }
      ;;
    *)
      vm_lifecycle_error "Profile '$name' is in '$status'; resize is allowed only for ready or suspended profiles."
      return 2
      ;;
  esac
  if [[ "$requested_memory" == "$current_memory" ]]; then
    printf 'Profile %s already has %s GiB; no restart was performed.\n' "$name" "$current_memory"
    return 0
  fi
  runtime="$(vm_lifecycle_profile_runtime "$root" "$name")" || return 2
  VM_LIFECYCLE_MEMORY_BUDGET="$runtime/scripts/vm_memory_budget.py"
  local depth
  depth="$(vm_lifecycle_field "$data" depth)" || return 2
  metrics_port="$(vm_lifecycle_metrics_select_port "$root" "$runtime" \
    "${VM_CONFIG_JSON:-}" "$depth" "$metrics_port")" || return 2
  local release_profile=''
  [[ "$status" != ready ]] || release_profile="$name"
  vm_lifecycle_memory_reserve "$root" "$name" "$depth" "$requested_memory" "$release_profile" || return $?
  owner_pid="$VM_LIFECYCLE_MEMORY_OWNER_PID"
  if vm_lifecycle_run_frozen_worker "$root" "$name" "$runtime" vm_lifecycle_resize_locked \
    "$root" "$name" "$requested_memory" "$recursive" "$timeout" "$owner_pid" \
    "$model_kind" "$model_port" "$metrics_port" "$readiness_timeout"; then
    return 0
  else
    rc=$?
  fi
  local after after_status
  after="$(vm_lifecycle_profile_read "$root" "$name" 2>/dev/null || true)"
  after_status="$(vm_lifecycle_field "$after" status 2>/dev/null || true)"
  case "$after_status" in
    ready|suspended|failed)
      identity_state="$(vm_lifecycle_identity_state "$after" "$root" "$name" 2>/dev/null || true)"
      if [[ "$identity_state" == running || "$identity_state" == stopped ]]; then
        vm_lifecycle_memory_release "$root" "$name" "$owner_pid" >/dev/null || true
      fi
      ;;
    *) ;;
  esac
  return "$rc"
}

vm_lifecycle_resize_locked() {
  local root="$1" name="$2" requested_memory="$3" recursive="${4:-0}"
  local timeout="${5:-$VM_LIFECYCLE_DEFAULT_TIMEOUT}" owner_pid="${6:-}"
  local model_kind="${7:-none}" model_port="${8:-}" metrics_port="${9:-}"
  local readiness_timeout="${10:-1800}" data status identity_state workspace patch rc
  data="$(vm_lifecycle_profile_read "$root" "$name")" || return 2
  status="$(vm_lifecycle_field "$data" status)" || return 2
  workspace="$(vm_lifecycle_field "$data" workspace)" || return 2
  workspace="$(vm_lifecycle_workspace_resolve "$root" "$workspace" "$workspace" 1 1)" || {
    vm_lifecycle_error 'Recorded workspace failed locked host path validation; resize did not stop the guest.'
    return 2
  }
  if [[ "$status" == ready ]]; then
    vm_lifecycle_suspend_locked "$root" "$name" "$recursive" "$timeout" \
      "$root/vm/profiles/$name/runtime/lab-vm" || return $?
    data="$(vm_lifecycle_profile_read "$root" "$name")" || return 2
    status="$(vm_lifecycle_field "$data" status)" || return 2
  fi
  [[ "$status" == suspended ]] || {
    vm_lifecycle_error 'Resize did not reach a verified suspended state; no RAM setting was changed.'
    return 2
  }
  identity_state="$(vm_lifecycle_identity_state "$data" "$root" "$name")" || identity_state=unknown
  [[ "$identity_state" == stopped ]] || {
    vm_lifecycle_transition "$root" "$name" suspended unknown '{}' >/dev/null || true
    vm_lifecycle_error 'QEMU remained live or ambiguous after graceful shutdown; resize was not attempted.'
    return 2
  }
  patch="$(printf '%s' "$requested_memory" | "$VM_LIFECYCLE_PYTHON" -c '
import json,sys
try:
    value=int(sys.stdin.read())
    assert 1 <= value <= 128
    print(json.dumps({"memory_gib":value,"runtime_identity":None},sort_keys=True,separators=(",",":")))
except Exception: raise SystemExit(2)
')" || return 2
  vm_lifecycle_transition "$root" "$name" suspended starting "$patch" >/dev/null || return 2
  vm_lifecycle_start_profile_locked "$root" "$name" "$workspace" "$owner_pid" \
    "$model_kind" "$model_port" "$metrics_port" "$readiness_timeout"
}

vm_lifecycle_runs_stopped() {
  local root="$1" name="$2"
  "$VM_LIFECYCLE_PYTHON" - "$VM_LIFECYCLE_PATHS" "$VM_LIFECYCLE_PROCESS" \
    "$root" "$name" "${VM_LIFECYCLE_PROC_ROOT:-/proc}" <<'PY'
import importlib.util,sys
from pathlib import Path
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    if spec is None or spec.loader is None: raise RuntimeError
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module
try:
    paths=load("vm_delete_paths",sys.argv[1]); process=load("vm_delete_process",sys.argv[2])
    root=Path(sys.argv[3]); profile=paths.profile_path(root,sys.argv[4]); runs=profile/"runs"
    if not runs.exists() and not runs.is_symlink(): raise SystemExit(0)
    paths.private_entry(runs)
    if runs.is_symlink() or not runs.is_dir(): raise ValueError
    for run in sorted(runs.iterdir(),key=lambda p:p.name):
        paths.private_entry(run)
        if run.is_symlink() or not run.is_dir(): raise ValueError
        registry=run/"processes.json"
        paths.private_entry(registry)
        if process.registry_status(registry,run.name,proc_root=Path(sys.argv[5]))["state"]!="stopped":
            raise ValueError
except Exception:
    raise SystemExit(2)
PY
}

vm_lifecycle_delete_locked() {
  local root="$1" name="$2" data status identity_state runtime patch result
  data="$(vm_lifecycle_profile_read "$root" "$name")" || return 2
  status="$(vm_lifecycle_field "$data" status)" || return 2
  case "$status" in
    allocated|suspended|failed) ;;
    *) vm_lifecycle_error "Profile '$name' is in '$status'; exact deletion requires allocated, suspended or failed state."; return 2 ;;
  esac
  if printf '%s' "$data" | "$VM_LIFECYCLE_PYTHON" -c 'import json,sys; raise SystemExit(0 if "runtime_identity" in json.load(sys.stdin) else 1)'; then
    identity_state="$(vm_lifecycle_identity_state "$data" "$root" "$name")" || identity_state=unknown
    if [[ "$identity_state" != stopped ]]; then
      case "$status" in suspended|failed) vm_lifecycle_transition "$root" "$name" "$status" unknown '{}' >/dev/null || true ;; esac
      vm_lifecycle_error 'The exact profile QEMU identity is not proven stopped; profile and reports were preserved.'
      return 2
    fi
    patch='{"runtime_identity":null}'
  else
    patch='{}'
  fi
  vm_lifecycle_runs_stopped "$root" "$name" || {
    vm_lifecycle_error 'A recorded run process is running or ambiguous; exact profile deletion was not started.'
    return 2
  }
  vm_lifecycle_memory_recover "$root" "$name" >/dev/null || {
    vm_lifecycle_error 'An allocation reservation is active or ambiguous; exact profile deletion was blocked.'
    return 2
  }
  vm_lifecycle_transition "$root" "$name" "$status" deleting "$patch" >/dev/null || return 2
  result="$("$VM_LIFECYCLE_PYTHON" "$VM_LIFECYCLE_PATHS" profile-delete \
    --state-root "$root" --profile "$name")" || {
    vm_lifecycle_error 'Profile deletion did not pass exact filesystem validation; uncertain assets were preserved.'
    return 2
  }
  printf '%s\n' "$result"
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

vm_lifecycle_profile_runtime() {
  local root="$1" name="$2" runtime path
  runtime="$root/vm/profiles/$name/runtime"
  for path in "$runtime/lab-vm" "$runtime/lib/state.sh" "$runtime/lib/vm/auth.sh" \
    "$runtime/lib/vm/browser-bridge-server.py" "$runtime/lib/vm/browser-open.py" \
    "$runtime/lib/vm/browser-open.sh" "$runtime/lib/vm/lifecycle.sh" \
    "$runtime/lib/vm/paths.py" "$runtime/lib/vm/config.sh" "$runtime/lib/vm/disk.sh" \
    "$runtime/lib/vm/cloud-init.sh" "$runtime/lib/vm/guest.sh" \
    "$runtime/lib/vm/guest-bootstrap.sh" "$runtime/lib/vm/runner.sh" \
    "$runtime/lib/vm/ssh.sh" "$runtime/lib/vm/services.sh" \
    "$runtime/lib/vm/process.py" "$runtime/lib/vm/proxy.py" \
    "$runtime/scripts/vm-opencode-config.py" "$runtime/scripts/vm-python-tools.sh" \
    "$runtime/scripts/vm-recursion-live-children.py" "$runtime/scripts/vm-recursion-workspace.py" \
    "$runtime/scripts/vm-self-test.py" \
    "$runtime/scripts/vm_memory_budget.py"; do
    [[ -f "$path" && ! -L "$path" ]] || {
      vm_lifecycle_error 'The selected profile has no complete frozen lifecycle runtime; source-tree control was not executed.'
      return 2
    }
  done
  [[ -x "$runtime/lab-vm" ]] || { vm_lifecycle_error 'Frozen profile launcher is not executable.'; return 2; }
  "$VM_LIFECYCLE_PYTHON" "$VM_LIFECYCLE_PATHS" runtime-validate --state-root "$root" --profile "$name" >/dev/null || {
    vm_lifecycle_error 'The selected profile runtime failed integrity validation; source-tree control was not executed.'
    return 2
  }
  printf '%s\n' "$runtime"
}

vm_lifecycle_run_frozen_worker() {
  local root="$1" name="$2" runtime="$3" callback="$4"
  shift 4
  case "$callback" in
    vm_lifecycle_suspend_locked|vm_lifecycle_resume_locked|vm_lifecycle_resize_locked|vm_lifecycle_delete_locked|vm_lifecycle_prepare_and_start_locked) ;;
    *) vm_lifecycle_error 'Invalid frozen lifecycle callback.'; return 2 ;;
  esac
  # shellcheck disable=SC2016 # This callback is evaluated by the frozen locked profile worker.
  "$VM_LIFECYCLE_PYTHON" "$runtime/lib/vm/paths.py" profile-run --state-root "$root" --profile "$name" -- \
    bash -c 'set -euo pipefail; unset VM_LIFECYCLE_ROOT VM_LIFECYCLE_PYTHON VM_LIFECYCLE_PATHS VM_LIFECYCLE_PROCESS VM_LIFECYCLE_CHILDREN VM_LIFECYCLE_MEMORY_BUDGET VM_LIFECYCLE_DEFAULT_TIMEOUT VM_LIFECYCLE_SOURCE_FILE; source "$1"; shift; callback="$1"; shift; "$callback" "$@"' \
    _ "$runtime/lib/vm/lifecycle.sh" "$callback" "$@"
}

vm_lifecycle_suspend() {
  local root="$1" name="$2" recursive="${3:-0}" timeout="${4:-$VM_LIFECYCLE_DEFAULT_TIMEOUT}"
  local controller="${5:-$root/vm/profiles/$name/runtime/lab-vm}" runtime
  runtime="$(vm_lifecycle_profile_runtime "$root" "$name")" || return 2
  # The single-quoted body runs inside the validated frozen profile worker.
  # shellcheck disable=SC2016
  vm_lifecycle_run_frozen_worker "$root" "$name" "$runtime" vm_lifecycle_suspend_locked \
    "$root" "$name" "$recursive" "$timeout" "$controller"
}

vm_lifecycle_resume() {
  local root="$1" name="$2" requested_workspace="${3:-}" repository="$4"
  local recurse="${5:-0}" unsafe="${6:-0}" recursive="${7:-0}"
  local timeout="${8:-$VM_LIFECYCLE_DEFAULT_TIMEOUT}" model_kind="${9:-none}"
  local model_port="${10:-}" metrics_port="${11:-}" readiness_timeout="${12:-1800}"
  local workload="${13:-}" agent="${14:-}" version="${15:-}"
  local model_provider="${16:-$model_kind}" model_id="${17:-}"
  local data status stored_workspace workspace depth memory release_profile='' runtime owner_pid='' rc after after_status identity_state
  # shellcheck disable=SC2034 # The memory helper reads this dynamically scoped frozen-runtime path.
  local VM_LIFECYCLE_MEMORY_BUDGET
  if [[ -z "$workload" || -z "$agent" || -z "$version" ]]; then
    if [[ -n "${VM_CONFIG_JSON:-}" ]]; then
      workload="${workload:-$(vm_lifecycle_json_field "$VM_CONFIG_JSON" workload_runtime)}" || return 2
      agent="${agent:-$(vm_lifecycle_json_field "$VM_CONFIG_JSON" agent)}" || return 2
      version="${version:-$(vm_lifecycle_json_field "$VM_CONFIG_JSON" opencode_version)}" || return 2
    else
      workload="${workload:-docker}"
      agent="${agent:-opencode}"
      version="${version:-latest}"
    fi
  fi
  data="$(vm_lifecycle_profile_read "$root" "$name")" || return 2
  status="$(vm_lifecycle_field "$data" status)" || return 2
  stored_workspace="$(vm_lifecycle_field "$data" workspace)" || return 2
  workspace="${requested_workspace:-$stored_workspace}"
  if [[ -z "$requested_workspace" && ! -d "$stored_workspace" ]]; then
    vm_lifecycle_error 'Recorded workspace is missing; resume requires an explicit valid --workspace selection.'
    return 2
  fi
  workspace="$(vm_lifecycle_workspace_resolve "$root" "$workspace" "$repository" 1 0)" || return 2
  if [[ "$workspace" == "$stored_workspace" ]]; then
    :
  else
    workspace="$(vm_lifecycle_workspace_resolve "$root" "$workspace" "$repository" \
      "$recurse" "$unsafe")" || return 2
  fi
  case "$status" in
    ready)
      identity_state="$(vm_lifecycle_identity_state "$data" "$root" "$name")" || identity_state=unknown
      [[ "$identity_state" == running ]] || { vm_lifecycle_error 'Ready profile identity is not verifiably running.'; return 2; }
      [[ "$workspace" == "$stored_workspace" ]] || release_profile="$name"
      ;;
    suspended)
      identity_state="$(vm_lifecycle_identity_state "$data" "$root" "$name")" || identity_state=unknown
      [[ "$identity_state" == stopped ]] || { vm_lifecycle_error 'Suspended profile identity is not verifiably stopped.'; return 2; }
      ;;
    *) vm_lifecycle_error "Profile '$name' is not ready or suspended; no startup was attempted."; return 2 ;;
  esac
  runtime="$(vm_lifecycle_profile_runtime "$root" "$name")" || return 2
  depth="$(vm_lifecycle_field "$data" depth)" || return 2
  if [[ "$status" == suspended || -n "$release_profile" ]]; then
    metrics_port="$(vm_lifecycle_metrics_select_port "$root" "$runtime" \
      "${VM_CONFIG_JSON:-}" "$depth" "$metrics_port")" || return 2
  elif [[ "$depth" != 0 ]]; then
    metrics_port=''
  fi
  VM_LIFECYCLE_MEMORY_BUDGET="$runtime/scripts/vm_memory_budget.py"
  if [[ -n "$release_profile" || "$status" == suspended ]]; then
    memory="$(vm_lifecycle_field "$data" memory_gib)" || return 2
    vm_lifecycle_memory_reserve "$root" "$name" "$depth" "$memory" "$release_profile" || return $?
    owner_pid="$VM_LIFECYCLE_MEMORY_OWNER_PID"
  fi
  if vm_lifecycle_run_frozen_worker "$root" "$name" "$runtime" vm_lifecycle_resume_locked \
    "$workspace" "$repository" "$recurse" "$unsafe" "$recursive" "$timeout" \
    "$owner_pid" "$model_kind" "$model_port" "$metrics_port" "$readiness_timeout" \
    "$workload" "$agent" "$version" "$model_provider" "$model_id"; then
    return 0
  else
    rc=$?
  fi
  if [[ -n "$owner_pid" ]]; then
    after="$(vm_lifecycle_profile_read "$root" "$name" 2>/dev/null || true)"
    after_status="$(vm_lifecycle_field "$after" status 2>/dev/null || true)"
    case "$after_status" in
      ready|suspended|failed)
        identity_state="$(vm_lifecycle_identity_state "$after" "$root" "$name")" || identity_state=unknown
        if [[ "$identity_state" != unknown ]]; then
          vm_lifecycle_memory_release "$root" "$name" "$owner_pid" >/dev/null || true
        fi
        ;;
    esac
  fi
  return "$rc"
}

vm_lifecycle_auth_delete_guard() {
  local root="$1" name="$2" data="$3" phase="${4:-export}" config="${VM_CONFIG_JSON:-}"
  local fields auth_forward escrow_dir version workspace
  [[ -n "$config" ]] || {
    vm_lifecycle_error 'Deletion requires validated configuration so auth export policy can be checked.'
    return 2
  }
  fields="$(printf '%s' "$config" | "$VM_LIFECYCLE_PYTHON" -c '
import json,sys
try:
    config=json.load(sys.stdin)
    value=config["auth_forward"]
    selector=",".join(value) if isinstance(value,list) else value
    print("\t".join((selector,config.get("auth_escrow_dir", ""))))
except Exception:
    raise SystemExit(2)
')" || {
    vm_lifecycle_error 'Auth export policy is missing or invalid; destructive action was refused.'
    return 2
  }
  IFS=$'\t' read -r auth_forward escrow_dir <<<"$fields"
  [[ "$auth_forward" == off ]] && return 0
  [[ "$phase" == preflight || "$phase" == export ]] || return 2
  version="$(vm_lifecycle_field "$data" opencode_version)" || return 2
  workspace="$(vm_lifecycle_field "$data" workspace)" || return 2
  [[ -n "$escrow_dir" ]] || escrow_dir="$root/secrets"
  if [[ "$phase" == preflight ]]; then
    vm_auth_resolve_providers "$auth_forward" "$version" >/dev/null || {
      vm_lifecycle_error 'Auth forwarding is enabled; destructive action requires TASK-10 auth export, which is unavailable for this profile version.'
      return 2
    }
    [[ "$(vm_lifecycle_field "$data" status)" == ready ]] || {
      vm_lifecycle_error 'Auth export requires the guest to be running; resume it and retry. The profile overlay was preserved.'
      return 2
    }
    local identity_state
    identity_state="$(vm_lifecycle_identity_state "$data" "$root" "$name")" || identity_state=unknown
    [[ "$identity_state" == running ]] || {
      vm_lifecycle_error 'Guest is not running as the verified profile; auth export was refused and the overlay was preserved.'
      return 2
    }
    return 0
  fi
  vm_auth_export_before_delete "$root" "$name" "$data" "$auth_forward" \
    "$version" "$escrow_dir" "$workspace" "$VM_LIFECYCLE_ROOT"
}

vm_lifecycle_delete() {
  local root="$1" name="$2" discard_recursive="${3:-0}" yes="${4:-0}"
  local recursive="${5:-0}" timeout="${6:-$VM_LIFECYCLE_DEFAULT_TIMEOUT}"
  local data status identity_state runtime
  data="$(vm_lifecycle_profile_read "$root" "$name")" || return 2
  vm_lifecycle_destructive_guard down "$data" off "$discard_recursive" || return 2
  vm_lifecycle_auth_delete_guard "$root" "$name" "$data" preflight || return $?
  vm_lifecycle_require_confirmation down "$name" \
    'remove only this exact profile overlay, seed, key and frozen runtime; archive run reports; preserve workspace, shared base cache and other profiles' \
    "$yes" || return 2
  vm_lifecycle_auth_delete_guard "$root" "$name" "$data" export || return $?
  status="$(vm_lifecycle_field "$data" status)" || return 2
  case "$status" in
    ready)
      vm_lifecycle_suspend "$root" "$name" "$recursive" "$timeout" || return $?
      ;;
    suspended|allocated|failed) ;;
    *) vm_lifecycle_error "Profile '$name' is in '$status'; ambiguous or in-flight state blocks deletion."; return 2 ;;
  esac
  data="$(vm_lifecycle_profile_read "$root" "$name")" || return 2
  status="$(vm_lifecycle_field "$data" status)" || return 2
  [[ "$status" == suspended || "$status" == allocated || "$status" == failed ]] || return 2
  runtime="$(vm_lifecycle_profile_runtime "$root" "$name")" || return 2
  vm_lifecycle_run_frozen_worker "$root" "$name" "$runtime" vm_lifecycle_delete_locked \
    "$root" "$name"
}

vm_lifecycle_rebuild() {
  local root="$1" name="$2" workspace="$3" repository="$4"
  local depth="$5" max_depth="$6" requested_memory="$7" recurse="$8" unsafe="$9"
  local discard_recursive="${10}" yes="${11}" recursive="${12}"
  local timeout="${13:-$VM_LIFECYCLE_DEFAULT_TIMEOUT}" readiness_timeout="${14:-1800}"
  vm_lifecycle_delete "$root" "$name" "$discard_recursive" "$yes" "$recursive" "$timeout" || return $?
  vm_lifecycle_up "$root" "$name" "$workspace" "$repository" "$depth" "$max_depth" \
    "$requested_memory" "$recurse" "$unsafe" "$recursive" "$timeout" "$readiness_timeout"
}

vm_lifecycle_status() {
  local root="$1" name="$2" data status has_identity identity_state
  data="$(vm_lifecycle_profile_read "$root" "$name")" || return 2
  status="$(vm_lifecycle_field "$data" status)" || return 2
  has_identity="$(printf '%s' "$data" | "$VM_LIFECYCLE_PYTHON" -c '
import json,sys
try:
    value=json.load(sys.stdin).get("runtime_identity")
    print("yes" if isinstance(value,dict) else "no")
except Exception:
    raise SystemExit(2)
')" || return 2
  if [[ "$has_identity" == yes ]]; then
    identity_state="$(vm_lifecycle_identity_state "$data" "$root" "$name")" || identity_state=unknown
  elif [[ "$status" == ready || "$status" == suspending || "$status" == \
    starting || "$status" == provisioning || "$status" == unknown ]]; then
    identity_state=unknown
  else
    identity_state=stopped
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
