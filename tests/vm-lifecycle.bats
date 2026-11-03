#!/usr/bin/env bats
# V-LIFE fixture tests: synthetic profile metadata and mocked process/SSH calls.

setup() {
  ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
  TEST_ROOT="$(mktemp -d /tmp/little-mester-lifecycle.XXXXXX)"
  chmod 700 "$TEST_ROOT"
  STATE="$TEST_ROOT/state"
  WORKSPACE="$TEST_ROOT/workspace"
  mkdir -m 700 "$STATE" "$WORKSPACE"
  export VM_LIFECYCLE_ROOT="$ROOT"
  export VM_LIFECYCLE_PATHS="$ROOT/lib/vm/paths.py"
  export VM_LIFECYCLE_PROCESS="$ROOT/lib/vm/process.py"
  export VM_LIFECYCLE_CHILDREN="$ROOT/scripts/vm-recursion-live-children.py"
VM_LIFECYCLE_PYTHON="$(command -v python3)"
export VM_LIFECYCLE_PYTHON
  export LIFE_LOG="$TEST_ROOT/calls.log"
  : > "$LIFE_LOG"
  source "$ROOT/lib/vm/lifecycle.sh"
  vm_lifecycle_identity_state() {
    if [[ "$LIFE_WAIT" == unknown && -s "$LIFE_LOG" ]] && grep -q '^wait:' "$LIFE_LOG"; then
      printf 'unknown\n'
    else
      printf '%s\n' "$LIFE_IDENTITY"
    fi
  }
  vm_lifecycle_children_guard() { printf 'children:%s\n' "$2" >> "$LIFE_LOG"; return "$LIFE_CHILD_RC"; }
  vm_lifecycle_guest_shutdown() { printf 'shutdown\n' >> "$LIFE_LOG"; return "$LIFE_SHUTDOWN_RC"; }
  vm_lifecycle_wait_qemu_stopped() {
    printf 'wait:%s\n' "$4" >> "$LIFE_LOG"
    case "$LIFE_WAIT" in
      stopped) printf '%s\n%s\n' "$STATE/vm/profiles/daily/runs/test/processes.json" test; return 0 ;;
      running) return 124 ;;
      *) return 2 ;;
    esac
  }
  vm_lifecycle_stop_sidecars() { printf 'sidecars\n' >> "$LIFE_LOG"; return "$LIFE_STOP_RC"; }

  python3 - "$ROOT/lib/vm/paths.py" "$STATE" "$WORKSPACE" <<'PY'
import importlib.util
import sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("fixture_paths",sys.argv[1])
paths=importlib.util.module_from_spec(spec); spec.loader.exec_module(paths)
root=Path(sys.argv[2]); workspace=Path(sys.argv[3]); image="a"*64
(root/"vm").mkdir(mode=0o700); (root/"vm/images").mkdir(mode=0o700)
image_dir=root/"vm/images"/image; image_dir.mkdir(mode=0o700)
(image_dir/"base.qcow2").write_text("fixture base\n"); (image_dir/"base.qcow2").chmod(0o600)
profile=root/"vm/profiles/daily"
disk=profile/"disk.qcow2"
data={
 "schema_version":1,"profile_id":"00000000-0000-4000-8000-000000000001",
 "name":"daily","workspace":str(workspace),"image_sha256":image,
 "image_path":str(image_dir/"base.qcow2"),"memory_gib":8,"depth":0,
 "max_depth":1,"descendants_possible":True,"opencode_version":"2.0.0",
 "guest_payload_sha256":"b"*64,"runtime_payload_sha256":"c"*64,"status":"ready",
 "runtime_identity":{
  "pid":4321,"start_ticks":12345,"executable":"/usr/bin/qemu-system-x86_64",
  "disk_path":str(disk),"socket_path":"/tmp/lifecycle-vfs.sock","ssh_port":40222,
  "argv":["/usr/bin/qemu-system-x86_64","-name","daily","-drive",f"file={disk},format=qcow2,if=virtio",
    "-chardev","socket,id=charfs,path=/tmp/lifecycle-vfs.sock",
    "-netdev","user,id=net0,hostfwd=tcp:127.0.0.1:40222-:22,restrict=on"]}}
paths.profile_write(root,"daily",data)
disk.write_text("fixture overlay\n"); disk.chmod(0o600)
PY
  PROFILE_JSON="$(python3 "$VM_LIFECYCLE_PATHS" profile-read --state-root "$STATE" --profile daily)"
  export PROFILE_JSON
  export LIFE_IDENTITY=running LIFE_CHILD_RC=0 LIFE_SHUTDOWN_RC=0 LIFE_WAIT=stopped LIFE_STOP_RC=0
}

teardown() {
  if [[ "$TEST_ROOT" == /tmp/little-mester-lifecycle.* && -O "$TEST_ROOT" ]]; then
    rm -rf -- "$TEST_ROOT"
  fi
}

profile_status() {
  python3 "$VM_LIFECYCLE_PATHS" profile-read --state-root "$STATE" --profile daily \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
}

@test "suspend uses child guard then graceful shutdown and preserves overlay" {
  run vm_lifecycle_suspend_locked "$STATE" daily 0 5 "$TEST_ROOT/frozen/lab-vm"
  [ "$status" -eq 0 ]
  [ "$(profile_status)" = suspended ]
  [ -f "$STATE/vm/profiles/daily/disk.qcow2" ]
  [ -d "$WORKSPACE" ]
  [ "$(cat "$LIFE_LOG")" = $'children:0\nshutdown\nwait:5\nsidecars' ]
}

@test "already suspended is idempotent and performs no process action" {
  python3 - "$VM_LIFECYCLE_PATHS" "$STATE" <<'PY'
import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("fixture_paths",sys.argv[1]); p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
p.profile_transition(Path(sys.argv[2]),"daily","ready","suspending",{})
p.profile_transition(Path(sys.argv[2]),"daily","suspending","suspended",{})
PY
  LIFE_IDENTITY=stopped
  run vm_lifecycle_suspend_locked "$STATE" daily 0 5 "$TEST_ROOT/frozen/lab-vm"
  [ "$status" -eq 0 ]
  [ ! -s "$LIFE_LOG" ]
}

@test "suspend refuses a missing frozen controller instead of sourcing checkout code" {
  run vm_lifecycle_suspend "$STATE" daily
  [ "$status" -eq 2 ]
  [[ "$output" == *"no complete frozen lifecycle runtime"* ]]
  [ "$(profile_status)" = ready ]
  [ ! -s "$LIFE_LOG" ]
}

@test "RAM defaults and identity-bound reservation are selected before allocation" {
  local budget="$TEST_ROOT/memory-budget"
  cat > "$budget" <<'BUDGET'
#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  defaults)
    case "$3" in 0) memory=8 ;; 1) memory=4 ;; 2) memory=2 ;; *) memory=1 ;; esac
    printf '{"depth":%s,"memory_gib":%s}\n' "$3" "$memory"
    ;;
  reserve)
    printf 'reserve:%s:%s\n' "$5" "$7" >> "$LIFE_LOG"
    [[ "${LIFE_BUDGET_FAIL:-0}" == 0 ]] || exit 2
    printf '{"profile":"%s","requested_mib":%s,"reserved_mib":%s}\n' "$5" "$7" "$7"
    ;;
  release)
    printf 'release:%s:%s\n' "$5" "$7" >> "$LIFE_LOG"
    printf '{"profile":"%s","released":true}\n' "$5"
    ;;
  *) exit 2 ;;
esac
BUDGET
  chmod 700 "$budget"
  VM_LIFECYCLE_MEMORY_BUDGET="$budget"

  run vm_lifecycle_memory_gib 0
  [ "$status" -eq 0 ] && [ "$output" = 8 ]
  run vm_lifecycle_memory_gib 3
  [ "$status" -eq 0 ] && [ "$output" = 1 ]
  run vm_lifecycle_memory_gib 2 12
  [ "$status" -eq 0 ] && [ "$output" = 12 ]
  run vm_lifecycle_memory_gib 2 129
  [ "$status" -eq 2 ]

  vm_lifecycle_memory_reserve "$STATE" candidate 2
  [ "$VM_LIFECYCLE_MEMORY_GIB" = 2 ]
  [ "$(cat "$LIFE_LOG")" = 'reserve:candidate:2048' ]
  local owner_pid="$VM_LIFECYCLE_MEMORY_OWNER_PID"
  vm_lifecycle_memory_release "$STATE" candidate >/dev/null
  [ "$(cat "$LIFE_LOG")" = $'reserve:candidate:2048'$'\nrelease:candidate:'"$owner_pid" ]
  [ -z "${VM_LIFECYCLE_MEMORY_GIB:-}" ]
  [ -z "${VM_LIFECYCLE_MEMORY_OWNER_PID:-}" ]

  export LIFE_BUDGET_FAIL=1
  if vm_lifecycle_memory_reserve "$STATE" blocked 3; then
    return 1
  else
    [ "$?" -eq 2 ]
  fi
  [ -z "${VM_LIFECYCLE_MEMORY_GIB:-}" ]
  [ ! -e "$STATE/vm/profiles/blocked/profile.json" ]
  unset LIFE_BUDGET_FAIL
}

@test "profile allocation validates and reserves RAM before its atomic create" {
  local budget="$TEST_ROOT/memory-budget" profile_json="$TEST_ROOT/allocated.json"
  cat > "$budget" <<'BUDGET'
#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  defaults)
    case "$3" in 0) memory=8 ;; 1) memory=4 ;; 2) memory=2 ;; *) memory=1 ;; esac
    printf '{"depth":%s,"memory_gib":%s}\n' "$3" "$memory"
    ;;
  reserve)
    printf 'reserve:%s:%s\n' "$5" "$7" >> "$LIFE_LOG"
    [[ "${LIFE_RACE_PROFILE:-}" != "$5" ]] || {
      mkdir -m 700 -p "$3/vm/profiles/$5"
      chmod 700 "$3/vm" "$3/vm/profiles" "$3/vm/profiles/$5"
      printf 'race\n' > "$3/vm/profiles/$5/unexpected"
      chmod 600 "$3/vm/profiles/$5/unexpected"
    }
    [[ "${LIFE_BUDGET_FAIL:-0}" == 0 ]] || exit 2
    printf '{"profile":"%s","requested_mib":%s,"reserved_mib":%s}\n' "$5" "$7" "$7"
    ;;
  release) printf 'release:%s:%s\n' "$5" "$7" >> "$LIFE_LOG" ;;
  *) exit 2 ;;
esac
BUDGET
  chmod 700 "$budget"
  VM_LIFECYCLE_MEMORY_BUDGET="$budget"
  python3 - "$VM_LIFECYCLE_PATHS" "$STATE" "$WORKSPACE" <<'PY' > "$profile_json"
import importlib.util,json,sys,uuid
from pathlib import Path
spec=importlib.util.spec_from_file_location("fixture_paths",sys.argv[1]); p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
data={"schema_version":1,"profile_id":str(uuid.uuid4()),"name":"candidate",
 "workspace":sys.argv[3],"image_sha256":None,"image_path":None,"memory_gib":8,
 "depth":2,"max_depth":2,"descendants_possible":False,"opencode_version":None,
 "guest_payload_sha256":None,"runtime_payload_sha256":None,"status":"allocated"}
json.dump(data,sys.stdout)
PY

  vm_lifecycle_profile_allocate "$STATE" candidate 2 '' < "$profile_json" > "$TEST_ROOT/created.json"
  [ "$VM_LIFECYCLE_MEMORY_GIB" = 2 ]
  [ "$(cat "$LIFE_LOG")" = 'reserve:candidate:2048' ]
  python3 - "$TEST_ROOT/created.json" "$STATE" <<'PY'
import json,os,sys
from pathlib import Path
data=json.loads(Path(sys.argv[1]).read_text())
saved=json.loads((Path(sys.argv[2])/"vm/profiles/candidate/profile.json").read_text())
assert data==saved and data["status"]=="allocated" and data["memory_gib"]==2, (data,saved)
PY

  local calls
  calls="$(cat "$LIFE_LOG")"
  if vm_lifecycle_profile_allocate "$STATE" candidate 2 '' < "$profile_json" >/dev/null; then
    return 1
  else
    [ "$?" -eq 2 ]
  fi
  [ -z "${VM_LIFECYCLE_MEMORY_GIB:-}" ]
  [ "$(cat "$LIFE_LOG")" = "$calls" ]

  sed 's/"depth":2/"depth":1/' "$profile_json" > "$TEST_ROOT/invalid.json"
  if vm_lifecycle_profile_allocate "$STATE" candidate 2 '' < "$TEST_ROOT/invalid.json" >/dev/null; then
    return 1
  else
    [ "$?" -eq 2 ]
  fi
  [ "$(cat "$LIFE_LOG")" = "$calls" ]

  export LIFE_BUDGET_FAIL=1
  sed 's/candidate/blocked/g' "$profile_json" > "$TEST_ROOT/blocked.json"
  if vm_lifecycle_profile_allocate "$STATE" blocked 2 '' < "$TEST_ROOT/blocked.json" >/dev/null; then
    return 1
  else
    [ "$?" -eq 2 ]
  fi
  [ ! -e "$STATE/vm/profiles/blocked/profile.json" ]
  [ "$(cat "$LIFE_LOG")" = $'reserve:candidate:2048\nreserve:blocked:2048' ]
  unset LIFE_BUDGET_FAIL

  export LIFE_RACE_PROFILE=raced
  sed 's/candidate/raced/g' "$profile_json" > "$TEST_ROOT/raced.json"
  if vm_lifecycle_profile_allocate "$STATE" raced 2 '' < "$TEST_ROOT/raced.json" >/dev/null; then
    return 1
  else
    [ "$?" -eq 2 ]
  fi
  local final_calls controller_pid="$BASHPID"
  final_calls="$(cat "$LIFE_LOG")"
  [ "$final_calls" = $'reserve:candidate:2048\nreserve:blocked:2048\nreserve:raced:2048\nrelease:raced:'"$controller_pid" ] || {
    printf 'Unexpected budget call order: %q\n' "$final_calls" >&3
    return 1
  }
}

@test "fresh up reserves before image work then freezes and starts one exact profile" {
  source "$ROOT/lib/vm/disk.sh"
  mkdir -m 700 "$TEST_ROOT/bin"
  cat > "$TEST_ROOT/bin/ssh-keygen" <<'KEYGEN'
#!/usr/bin/env bash
set -euo pipefail
while (($#)); do
  if [[ "$1" == -f ]]; then shift; target="$1"; break; fi
  shift
done
[[ -n "${target:-}" ]]
(umask 077; printf '%s\n' '-----BEGIN OPENSSH PRIVATE KEY-----' 'fixture-private-key' '-----END OPENSSH PRIVATE KEY-----' > "$target")
(umask 077; printf '%s\n' 'ssh-ed25519 AAAATEST profile-key' > "$target.pub")
KEYGEN
  cat > "$TEST_ROOT/bin/genisoimage" <<'ISO'
#!/usr/bin/env bash
set -euo pipefail
while (($#)); do
  if [[ "$1" == -output ]]; then shift; target="$1"; break; fi
  shift
done
python3 - "$target" <<'PY'
import sys
payload=bytearray(17*2048)
payload[16*2048]=1
payload[16*2048+1:16*2048+6]=b"CD001"
payload[16*2048+6]=1
open(sys.argv[1],"wb").write(payload)
PY
ISO
  chmod 700 "$TEST_ROOT/bin/ssh-keygen" "$TEST_ROOT/bin/genisoimage"
  export PATH="$TEST_ROOT/bin:$PATH"
  VM_CONFIG_JSON='{"agent":"opencode","model":{"port":null,"provider":"none"},"opencode_version":"2.0.0","workload_runtime":"none"}'
  vm_setup() { printf 'setup\n' >> "$LIFE_LOG"; }
  vm_lifecycle_memory_reserve() {
    printf 'reserve\n' >> "$LIFE_LOG"
    VM_LIFECYCLE_MEMORY_GIB=8
    VM_LIFECYCLE_MEMORY_OWNER_PID="$BASHPID"
  }
  vm_lifecycle_memory_release() { printf 'release:%s\n' "$3" >> "$LIFE_LOG"; }
  vm_guest_payload_hash() { printf '%s\n' 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'; }
  vm_cloud_init_user_data() { printf '#cloud-config\nssh_authorized_keys:\n - %s\n' "$4"; }
  vm_disk_create_base() {
    printf 'base\n' >> "$LIFE_LOG"
    local image="$1/vm/images/$VM_DISK_PIN_SHA256"
    mkdir -p "$image"
    chmod 700 "$1/vm" "$1/vm/images" "$image"
    printf 'fixture base\n' > "$image/base.qcow2"
    chmod 600 "$image/base.qcow2"
  }
  vm_disk_create_overlay() {
    printf 'overlay\n' >> "$LIFE_LOG"
    printf 'fixture overlay\n' > "$1/vm/profiles/$2/disk.qcow2"
    chmod 600 "$1/vm/profiles/$2/disk.qcow2"
  }
  vm_runner_start() {
    printf 'runner\n' >> "$LIFE_LOG"
    VM_RUNNER_PROCESS_REGISTRY="$1/runs/$2/processes.json"
    VM_RUNNER_QEMU_ROLE=qemu_1
    VM_RUNNER_SSH_PORT=40222
  }
  vm_lifecycle_capture_runtime_identity() {
    printf 'identity\n' >> "$LIFE_LOG"
    python3 - "$1" "$2" "$6" <<'PY'
import json,os,sys
root,name,port=sys.argv[1:]
profile=f"{root}/vm/profiles/{name}"
disk=f"{profile}/disk.qcow2"
socket="/tmp/lifecycle-fixture.sock"
argv=["/bin/bash","-enable-kvm","-m","8192","-drive",f"file={disk},format=qcow2,if=virtio",
      "-chardev",f"socket,id=charfs,path={socket}","-netdev",
      f"user,id=net0,hostfwd=tcp:127.0.0.1:{port}-:22,restrict=on"]
executable=os.path.realpath("/bin/bash")
argv[0]=executable
item={"pid":4321,"start_ticks":99,"executable":executable,"disk_path":disk,
      "socket_path":socket,"ssh_port":int(port),"argv":argv}
print(json.dumps(item,sort_keys=True,separators=(",",":")))
PY
  }
  vm_lifecycle_guest_readiness() {
    printf 'readiness\n' >> "$LIFE_LOG"
    printf '{"opencode_version":"2.0.0"}\n'
  }
  vm_lifecycle_run_frozen_worker() {
    local callback="$4"
    shift 4
    printf 'worker:%s\n' "$callback" >> "$LIFE_LOG"
    "$callback" "$@"
  }

  if vm_lifecycle_up "$STATE" fresh "$WORKSPACE" "$ROOT" 0 0 '' 0 0 0 60 30 \
    > "$TEST_ROOT/up.stdout" 2> "$TEST_ROOT/up.stderr"; then
    status=0
  else
    status=$?
  fi
  output="$(cat "$TEST_ROOT/up.stderr" "$TEST_ROOT/up.stdout")"
  if [ "$status" -ne 0 ]; then
    printf 'fresh up status=%s output=%s log=%s\n' "$status" "$output" "$(cat "$LIFE_LOG")" >&3
    cat "$TEST_ROOT/up.stderr" >&3
    [[ ! -e "$STATE/vm/profiles/fresh/profile.json" ]] || cat "$STATE/vm/profiles/fresh/profile.json" >&3
  fi
  [ "$status" -eq 0 ]
  [ "$(python3 "$VM_LIFECYCLE_PATHS" profile-read --state-root "$STATE" --profile fresh | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])')" = ready ]
  [ -f "$STATE/vm/profiles/fresh/disk.qcow2" ]
  [ -f "$STATE/vm/profiles/fresh/seed.iso" ]
  [ -f "$STATE/vm/profiles/fresh/control-key" ]
  [ "$(stat -c '%a' "$STATE/vm/profiles/fresh/control-key")" = 600 ]
  python3 - "$STATE/vm/profiles/fresh/seed.iso" <<'PY'
import sys
data=open(sys.argv[1],"rb").read()
assert data[16*2048+1:16*2048+6]==b"CD001"
PY
  [ "$(cat "$LIFE_LOG")" = $'setup\nreserve\nbase\nworker:vm_lifecycle_prepare_and_start_locked\noverlay\nrunner\nidentity\nreadiness\nrelease:'"$VM_LIFECYCLE_MEMORY_OWNER_PID" ]
}

@test "resize reserves exact target RAM before graceful child-safe shutdown and reuses overlay" {
  local profile="$STATE/vm/profiles/daily" before
  before="$(sha256sum "$profile/disk.qcow2" | awk '{print $1}')"
  vm_lifecycle_profile_runtime() { printf '%s\n' "$TEST_ROOT/frozen"; }
  vm_lifecycle_memory_reserve() {
    printf 'reserve:%s:%s:%s\n' "$4" "${5:-}" "$BASHPID" >> "$LIFE_LOG"
    VM_LIFECYCLE_MEMORY_GIB="$4"
    VM_LIFECYCLE_MEMORY_OWNER_PID="$BASHPID"
  }
  vm_lifecycle_memory_release() { printf 'release:%s\n' "$3" >> "$LIFE_LOG"; }
  vm_lifecycle_identity_state() {
    if [[ "$(profile_status)" == suspended ]]; then printf 'stopped\n'; else printf 'running\n'; fi
  }
  vm_lifecycle_start_profile_locked() {
    local data identity patch
    data="$(vm_lifecycle_profile_read "$1" "$2")"
    [ "$(vm_lifecycle_field "$data" memory_gib)" = 12 ]
    printf 'start:%s\n' "$(vm_lifecycle_field "$data" memory_gib)" >> "$LIFE_LOG"
    identity="$(printf '%s' "$PROFILE_JSON" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["runtime_identity"]))')"
    patch="$(printf '{"runtime_identity":%s}' "$identity")"
    vm_lifecycle_transition "$1" "$2" starting provisioning "$patch" >/dev/null
    vm_lifecycle_transition "$1" "$2" provisioning ready '{"opencode_version":"2.0.0"}' >/dev/null
    vm_lifecycle_memory_release "$1" "$2" "$4" >/dev/null
  }
  vm_lifecycle_run_frozen_worker() {
    local callback="$4"
    shift 4
    printf 'worker:%s\n' "$callback" >> "$LIFE_LOG"
    "$callback" "$@"
  }

  run vm_lifecycle_resize "$STATE" daily 12 0 5 none '' '' 30
  [ "$status" -eq 0 ]
  [ "$(profile_status)" = ready ]
  [ "$(python3 "$VM_LIFECYCLE_PATHS" profile-read --state-root "$STATE" --profile daily | python3 -c 'import json,sys;print(json.load(sys.stdin)["memory_gib"])')" = 12 ]
  [ "$(sha256sum "$profile/disk.qcow2" | awk '{print $1}')" = "$before" ]
  local resize_calls expected_calls
  resize_calls="$(cat "$LIFE_LOG")"
  expected_calls=$'reserve:12:daily:'
  [[ "$resize_calls" == "$expected_calls"* ]] || {
    printf 'Unexpected resize call order: %q\n' "$resize_calls" >&3
    return 1
  }
  local reserved_owner="${resize_calls%%$'\n'*}"
  reserved_owner="${reserved_owner##*:}"
  expected_calls=$'reserve:12:daily:'"$reserved_owner"$'\nworker:vm_lifecycle_resize_locked\nchildren:0\nshutdown\nwait:5\nsidecars\nstart:12\nrelease:'"$reserved_owner"
  [ "$resize_calls" = "$expected_calls" ] || {
    printf 'Unexpected resize call order: %q (expected %q)\n' "$resize_calls" "$expected_calls" >&3
    return 1
  }
}

@test "resize RAM preflight refusal leaves process profile and overlay untouched" {
  local profile="$STATE/vm/profiles/daily" before
  before="$(sha256sum "$profile/disk.qcow2" | awk '{print $1}')"
  vm_lifecycle_profile_runtime() { printf '%s\n' "$TEST_ROOT/frozen"; }
  vm_lifecycle_memory_reserve() {
    printf 'reserve:%s:%s\n' "$4" "${5:-}" >> "$LIFE_LOG"
    return 2
  }
  vm_lifecycle_run_frozen_worker() {
    printf 'unexpected-worker\n' >> "$LIFE_LOG"
    return 99
  }

  run vm_lifecycle_resize "$STATE" daily 12 0 5 none '' '' 30
  [ "$status" -eq 2 ]
  [ "$(profile_status)" = ready ]
  [ "$(sha256sum "$profile/disk.qcow2" | awk '{print $1}')" = "$before" ]
  [ "$(cat "$LIFE_LOG")" = 'reserve:12:daily' ]
}

@test "resize graceful-shutdown timeout restores the old ready profile and releases reservation" {
  local profile="$STATE/vm/profiles/daily" before calls reserved_owner
  before="$(sha256sum "$profile/disk.qcow2" | awk '{print $1}')"
  LIFE_WAIT=running
  vm_lifecycle_profile_runtime() { printf '%s\n' "$TEST_ROOT/frozen"; }
  vm_lifecycle_memory_reserve() {
    printf 'reserve:%s:%s:%s\n' "$4" "${5:-}" "$BASHPID" >> "$LIFE_LOG"
    VM_LIFECYCLE_MEMORY_OWNER_PID="$BASHPID"
  }
  vm_lifecycle_memory_release() { printf 'release:%s\n' "$3" >> "$LIFE_LOG"; }
  vm_lifecycle_run_frozen_worker() {
    local callback="$4"
    shift 4
    printf 'worker:%s\n' "$callback" >> "$LIFE_LOG"
    "$callback" "$@"
  }

  run vm_lifecycle_resize "$STATE" daily 12 0 5 none '' '' 30
  [ "$status" -eq 124 ]
  [ "$(profile_status)" = ready ]
  [ "$(sha256sum "$profile/disk.qcow2" | awk '{print $1}')" = "$before" ]
  [ "$(python3 "$VM_LIFECYCLE_PATHS" profile-read --state-root "$STATE" --profile daily | python3 -c 'import json,sys;print(json.load(sys.stdin)["memory_gib"])')" = 8 ]
  calls="$(cat "$LIFE_LOG")"
  reserved_owner="${calls%%$'\n'*}"
  reserved_owner="${reserved_owner##*:}"
  [ "$calls" = $'reserve:12:daily:'"$reserved_owner"$'\nworker:vm_lifecycle_resize_locked\nchildren:0\nshutdown\nwait:5\nrelease:'"$reserved_owner" ]
}

@test "delete authenticates policy and removes only the suspended exact profile after safe checks" {
  python3 - "$VM_LIFECYCLE_PATHS" "$STATE" <<'PY'
import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("fixture_paths",sys.argv[1]); p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
root=Path(sys.argv[2])
p.profile_transition(root,"daily","ready","suspending",{})
p.profile_transition(root,"daily","suspending","suspended",{})
PY
  VM_CONFIG_JSON='{"auth_forward":"off"}'
  LIFE_IDENTITY=stopped
  vm_lifecycle_profile_runtime() { printf 'runtime\n' >> "$LIFE_LOG"; printf '%s\n' "$TEST_ROOT/frozen"; }
  vm_lifecycle_runs_stopped() { printf 'runs-stopped\n' >> "$LIFE_LOG"; }
  vm_lifecycle_memory_recover() { printf 'reservation-recovery\n' >> "$LIFE_LOG"; }
  vm_lifecycle_run_frozen_worker() {
    local callback="$4"
    shift 4
    printf 'worker:%s\n' "$callback" >> "$LIFE_LOG"
    "$callback" "$@"
  }

  run vm_lifecycle_delete "$STATE" daily 1 1 0 5
  [ "$status" -eq 0 ]
  [ ! -e "$STATE/vm/profiles/daily/profile.json" ]
  [ -d "$WORKSPACE" ]
  [ -f "$STATE/vm/images/$(python3 -c 'import json,sys;print(json.loads(sys.argv[1])["image_sha256"])' "$PROFILE_JSON")/base.qcow2" ]
  [[ "$output" == *'archive run reports'* ]]
  [ "$(cat "$LIFE_LOG")" = $'runtime\nworker:vm_lifecycle_delete_locked\nruns-stopped\nreservation-recovery' ]
}

@test "auth-enabled deletion refuses before confirmation, worker or guest shutdown" {
  VM_CONFIG_JSON='{"auth_forward":"auto"}'
  run vm_lifecycle_delete "$STATE" daily 1 1 0 5
  [ "$status" -eq 2 ]
  [[ "$output" == *'requires TASK-10 auth export'* ]]
  [ "$(profile_status)" = ready ]
  [ ! -s "$LIFE_LOG" ]
}

@test "delete refuses ambiguous allocation recovery before changing profile status" {
  python3 - "$VM_LIFECYCLE_PATHS" "$STATE" <<'PY'
import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("fixture_paths",sys.argv[1]); p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
root=Path(sys.argv[2])
p.profile_transition(root,"daily","ready","suspending",{})
p.profile_transition(root,"daily","suspending","suspended",{})
PY
  VM_CONFIG_JSON='{"auth_forward":"off"}'
  LIFE_IDENTITY=stopped
  vm_lifecycle_profile_runtime() { printf '%s\n' "$TEST_ROOT/frozen"; }
  vm_lifecycle_runs_stopped() { printf 'runs-stopped\n' >> "$LIFE_LOG"; }
  vm_lifecycle_memory_recover() {
    printf 'reservation-ambiguous\n' >> "$LIFE_LOG"
    return 2
  }
  vm_lifecycle_run_frozen_worker() {
    local callback="$4"
    shift 4
    "$callback" "$@"
  }

  run vm_lifecycle_delete "$STATE" daily 1 1 0 5
  [ "$status" -eq 2 ]
  [ "$(profile_status)" = suspended ]
  [ -f "$STATE/vm/profiles/daily/disk.qcow2" ]
  [ "$(cat "$LIFE_LOG")" = $'runs-stopped\nreservation-ambiguous' ]
}

@test "rebuild never starts replacement after exact deletion fails" {
  vm_lifecycle_delete() {
    printf 'delete:%s:%s:%s:%s\n' "$2" "$3" "$4" "$5" >> "$LIFE_LOG"
    return 2
  }
  vm_lifecycle_up() {
    printf 'unexpected-up\n' >> "$LIFE_LOG"
    return 99
  }

  run vm_lifecycle_rebuild "$STATE" daily "$WORKSPACE" "$ROOT" 0 1 '' 0 0 1 1 0 5 30
  [ "$status" -eq 2 ]
  [ "$(cat "$LIFE_LOG")" = 'delete:daily:1:1:0' ]
}

@test "workspace seed refresh builds before metadata commit and preserves old seed on failure" {
  local profile="$STATE/vm/profiles/daily" workspace_b="$TEST_ROOT/workspace-b"
  mkdir -m 700 "$workspace_b"
  printf '%s\n' 'fixture private key' > "$profile/control-key"
  chmod 600 "$profile/control-key"
  printf '#cloud-config\nold: true\n' > "$profile/seed.user-data"
  chmod 600 "$profile/seed.user-data"
  python3 - "$profile/seed.iso" <<'PY'
import sys
payload=bytearray(17*2048)
payload[16*2048]=1
payload[16*2048+1:16*2048+6]=b"CD001"
payload[16*2048+6]=1
payload[-1]=1
open(sys.argv[1],"wb").write(payload)
PY
  chmod 600 "$profile/seed.iso"
  local overlay_before key_before seed_before
  overlay_before="$(sha256sum "$profile/disk.qcow2" | awk '{print $1}')"
  key_before="$(sha256sum "$profile/control-key" | awk '{print $1}')"
  seed_before="$(sha256sum "$profile/seed.iso" | awk '{print $1}')"
  python3 - "$VM_LIFECYCLE_PATHS" "$STATE" "$workspace_b" <<'PY'
import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("fixture_paths",sys.argv[1]); p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
root=Path(sys.argv[2])
p.profile_transition(root,"daily","ready","suspending",{})
p.profile_transition(root,"daily","suspending","suspended",{"runtime_identity":None})
PY
  ssh-keygen() { printf 'ssh-ed25519 AAAATEST fixture-key\n'; }
  vm_cloud_init_user_data() { printf '#cloud-config\npayload: %s\n' "${10}"; }
  genisoimage() {
    local target=''
    while (($#)); do
      if [[ "$1" == -output ]]; then shift; target="$1"; break; fi
      shift
    done
    python3 - "$target" "${ISO_MARKER:-2}" <<'PY'
import sys
payload=bytearray(17*2048)
payload[16*2048]=1
payload[16*2048+1:16*2048+6]=b"CD001"
payload[16*2048+6]=1
payload[-1]=int(sys.argv[2])
open(sys.argv[1],"wb").write(payload)
PY
  }

  genisoimage() { return 1; }
  if vm_lifecycle_switch_seed_locked "$STATE" daily "$workspace_b" 0 1 none opencode 2.0.0 \
    dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd "$(id -u)" "$(id -g)"; then
    return 1
  else
    [ "$?" -eq 1 ]
  fi
  [ "$(profile_status)" = suspended ]
  [ "$(python3 "$VM_LIFECYCLE_PATHS" profile-read --state-root "$STATE" --profile daily | python3 -c 'import json,sys;print(json.load(sys.stdin)["workspace"])')" = "$WORKSPACE" ]
  [ "$(cat "$profile/seed.user-data")" = $'#cloud-config\nold: true' ]
  [ "$(sha256sum "$profile/seed.iso" | awk '{print $1}')" = "$seed_before" ]
  [ -z "$(find "$profile" -maxdepth 1 -type d -name '.asset.*' -print -quit)" ]

  genisoimage() {
    local target=''
    while (($#)); do
      if [[ "$1" == -output ]]; then shift; target="$1"; break; fi
      shift
    done
    python3 - "$target" "${ISO_MARKER:-2}" <<'PY'
import sys
payload=bytearray(17*2048)
payload[16*2048]=1
payload[16*2048+1:16*2048+6]=b"CD001"
payload[16*2048+6]=1
payload[-1]=int(sys.argv[2])
open(sys.argv[1],"wb").write(payload)
PY
  }
  vm_lifecycle_switch_seed_locked "$STATE" daily "$workspace_b" 0 1 none opencode 2.0.0 \
    dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd "$(id -u)" "$(id -g)"
  [ "$(profile_status)" = starting ]
  python3 "$VM_LIFECYCLE_PATHS" profile-read --state-root "$STATE" --profile daily | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["workspace"]==sys.argv[1] and d["guest_payload_sha256"]=="d"*64' "$workspace_b"
  [ "$(cat "$profile/seed.user-data")" = $'#cloud-config\npayload: dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd' ]
  [ "$(sha256sum "$profile/disk.qcow2" | awk '{print $1}')" = "$overlay_before" ]
  [ "$(sha256sum "$profile/control-key" | awk '{print $1}')" = "$key_before" ]
  [ "$(sha256sum "$profile/seed.iso" | awk '{print $1}')" != "$seed_before" ]
  [ -z "$(find "$profile" -maxdepth 1 -type d -name '.asset.*' -print -quit)" ]
}

@test "status checks recorded identity for suspended and unknown profiles without mutation" {
  python3 - "$VM_LIFECYCLE_PATHS" "$STATE" <<'PY'
import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("fixture_paths",sys.argv[1]); p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
root=Path(sys.argv[2])
p.profile_transition(root,"daily","ready","suspending",{})
p.profile_transition(root,"daily","suspending","suspended",{})
PY
  LIFE_IDENTITY=running
  before="$(profile_status)"
  run vm_lifecycle_status "$STATE" daily
  [ "$status" -eq 0 ]
  printf '%s' "$output" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["status"]=="suspended" and d["runtime_state"]=="running"'
  [ "$(profile_status)" = "$before" ]

  python3 - "$VM_LIFECYCLE_PATHS" "$STATE" <<'PY'
import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("fixture_paths",sys.argv[1]); p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
p.profile_transition(Path(sys.argv[2]),"daily","suspended","unknown",{})
PY
  LIFE_IDENTITY=unknown
  run vm_lifecycle_status "$STATE" daily
  [ "$status" -eq 0 ]
  printf '%s' "$output" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["status"]=="unknown" and d["runtime_state"]=="unknown"'
  [ "$(profile_status)" = unknown ]
}

@test "status reports in-flight startup as unknown without changing profile metadata" {
  python3 - "$VM_LIFECYCLE_PATHS" "$STATE" <<'PY'
import importlib.util,sys,uuid
from pathlib import Path
spec=importlib.util.spec_from_file_location("fixture_paths",sys.argv[1]); p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
root=Path(sys.argv[2]); data=p.profile_read(root,"daily")
data={key:value for key,value in data.items() if key!="runtime_identity"}
data.update(name="pending",profile_id=str(uuid.uuid4()),status="allocated")
p.profile_create(root,"pending",data)
p.profile_transition(root,"pending","allocated","starting",{})
PY
  before="$(python3 "$VM_LIFECYCLE_PATHS" profile-read --state-root "$STATE" --profile pending)"
  run vm_lifecycle_status "$STATE" pending
  [ "$status" -eq 0 ]
  printf '%s' "$output" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["status"]=="starting" and d["runtime_state"]=="unknown"'
  [ "$(python3 "$VM_LIFECYCLE_PATHS" profile-read --state-root "$STATE" --profile pending)" = "$before" ]
}

@test "locked resume preserves exact overlay and marks failed readiness without losing identity evidence" {
  local profile="$STATE/vm/profiles/daily"
  printf 'fixture seed\n' > "$profile/seed.iso"
  printf 'fixture control key\n' > "$profile/control-key"
  chmod 600 "$profile/seed.iso" "$profile/control-key"
  python3 - "$VM_LIFECYCLE_PATHS" "$STATE" <<'PY'
import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("fixture_paths",sys.argv[1]); p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
root=Path(sys.argv[2])
p.profile_transition(root,"daily","ready","suspending",{})
p.profile_transition(root,"daily","suspending","suspended",{})
p.profile_transition(root,"daily","suspended","starting",{"runtime_identity":None})
PY
  vm_runner_start() {
    printf 'runner\n' >> "$LIFE_LOG"
    VM_RUNNER_PROFILE_DIR="$1"
    VM_RUNNER_RUN_ID="$2"
    VM_RUNNER_RUN_DIR="$1/runs/$2"
    VM_RUNNER_PROCESS_REGISTRY="$VM_RUNNER_RUN_DIR/processes.json"
    VM_RUNNER_QEMU_ROLE=qemu_1
    VM_RUNNER_SSH_PORT=40222
  }
  vm_lifecycle_capture_runtime_identity() {
    printf 'identity\n' >> "$LIFE_LOG"
    printf '%s' "$PROFILE_JSON" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["runtime_identity"]))'
  }
  vm_lifecycle_guest_readiness() {
    printf 'readiness\n' >> "$LIFE_LOG"
    [[ "${LIFE_READY_FAIL:-0}" == 0 ]] || return 124
    printf '{"opencode_version":"2.0.0"}\n'
  }
  vm_lifecycle_memory_release() { printf 'release:%s\n' "$3" >> "$LIFE_LOG"; }

  vm_lifecycle_start_profile_locked "$STATE" daily "$WORKSPACE" 1234 none '' '' 30
  [ "$(profile_status)" = ready ]
  [ "$(cat "$LIFE_LOG")" = $'runner\nidentity\nreadiness\nrelease:1234' ]
  [ -f "$profile/disk.qcow2" ]
  [ -d "$WORKSPACE" ]

  : > "$LIFE_LOG"
  python3 - "$VM_LIFECYCLE_PATHS" "$STATE" <<'PY'
import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("fixture_paths",sys.argv[1]); p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
root=Path(sys.argv[2])
p.profile_transition(root,"daily","ready","suspending",{})
p.profile_transition(root,"daily","suspending","suspended",{})
p.profile_transition(root,"daily","suspended","starting",{"runtime_identity":None})
PY
  export LIFE_READY_FAIL=1
  if vm_lifecycle_start_profile_locked "$STATE" daily "$WORKSPACE" 1234 none '' '' 30; then
    return 1
  else
    [ "$?" -eq 124 ]
  fi
  unset LIFE_READY_FAIL
  python3 - "$VM_LIFECYCLE_PATHS" "$STATE" <<'PY'
import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("fixture_paths",sys.argv[1]); p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p)
data=p.profile_read(Path(sys.argv[2]),"daily")
assert data["status"]=="failed" and "runtime_identity" not in data
PY
  [ -f "$profile/disk.qcow2" ]
  [ "$(cat "$LIFE_LOG")" = $'runner\nidentity\nreadiness\nshutdown\nwait:60\nsidecars\nrelease:1234' ]
}

@test "resume is warm-idempotent and A-to-B-to-B switches only once on the same overlay" {
  local workspace_b="$TEST_ROOT/workspace-b" profile="$STATE/vm/profiles/daily"
  mkdir -m 700 "$workspace_b"
  vm_lifecycle_identity_state() {
    if [[ "$(profile_status)" == suspended ]] && grep -q '^wait:' "$LIFE_LOG"; then
      printf 'stopped\n'
    else
      printf '%s\n' "$LIFE_IDENTITY"
    fi
  }
  vm_lifecycle_guest_readiness() {
    printf 'readiness\n' >> "$LIFE_LOG"
    printf '{"opencode_version":"2.0.0"}\n'
  }
  vm_guest_payload_hash() {
    printf 'payload:%s:%s:%s\n' "$1" "$2" "$4" >> "$LIFE_LOG"
    printf '%s\n' 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd'
  }
  vm_lifecycle_switch_seed_locked() {
    local patch
    patch="$(python3 - "$3" "$9" <<'PY'
import json,sys
print(json.dumps({"workspace":sys.argv[1],"guest_payload_sha256":sys.argv[2],
                  "runtime_identity":None},sort_keys=True,separators=(",",":")))
PY
    )"
    vm_lifecycle_transition "$1" "$2" suspended starting "$patch" >/dev/null
    printf 'seed:%s:%s\n' "$3" "$9" >> "$LIFE_LOG"
  }
  vm_lifecycle_wait_qemu_stopped() {
    printf 'wait:%s\n' "$4" >> "$LIFE_LOG"
    printf '%s\n%s\n' "$STATE/vm/profiles/daily/runs/test/processes.json" test
    return 0
  }
  vm_lifecycle_start_profile_locked() {
    local identity patch data
    data="$(vm_lifecycle_profile_read "$STATE" daily)"
    if [[ "$3" == "$workspace_b" ]]; then
      [ "$(vm_lifecycle_field "$data" guest_payload_sha256)" = 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd' ]
    fi
    printf 'start:%s\n' "$3" >> "$LIFE_LOG"
    identity="$(printf '%s' "$PROFILE_JSON" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["runtime_identity"]))')"
    patch="$(printf '{"runtime_identity":%s}' "$identity")"
    vm_lifecycle_transition "$STATE" daily starting provisioning "$patch" >/dev/null
    vm_lifecycle_transition "$STATE" daily provisioning ready '{"opencode_version":"2.0.0"}' >/dev/null
  }

  run vm_lifecycle_resume_locked "$STATE" daily "$WORKSPACE" "$ROOT" 0 0 0 5 '' none '' '' 30 none opencode 2.0.0
  [ "$status" -eq 0 ]
  [ "$(cat "$LIFE_LOG")" = readiness ]
  [ "$(profile_status)" = ready ]

  : > "$LIFE_LOG"
  run vm_lifecycle_resume_locked "$STATE" daily "$workspace_b" "$ROOT" 0 0 0 5 '' none '' '' 30 none opencode 2.0.0
  [ "$status" -eq 0 ]
  [ "$(profile_status)" = ready ]
  python3 - "$VM_LIFECYCLE_PATHS" "$STATE" "$workspace_b" <<'PY'
import json,subprocess,sys
from pathlib import Path
data=json.loads(subprocess.check_output([sys.executable,sys.argv[1],"profile-read","--state-root",sys.argv[2],"--profile","daily"]))
assert data["workspace"]==sys.argv[3] and data["status"]=="ready"
PY
  [ "$(cat "$LIFE_LOG")" = $'payload:'"$workspace_b"$':none:2.0.0\nchildren:0\nshutdown\nwait:5\nsidecars\nseed:'"$workspace_b"$':dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd\nstart:'"$workspace_b" ]
  [ -f "$profile/disk.qcow2" ]
  [ -d "$WORKSPACE" ]
  [ -d "$workspace_b" ]

  : > "$LIFE_LOG"
  run vm_lifecycle_resume_locked "$STATE" daily "$workspace_b" "$ROOT" 0 0 0 5 '' none '' '' 30 none opencode 2.0.0
  [ "$status" -eq 0 ]
  [ "$(cat "$LIFE_LOG")" = readiness ]
  [ -f "$profile/disk.qcow2" ]
}

@test "workspace seed prebuild failure preserves suspended metadata and never starts QEMU" {
  local workspace_b="$TEST_ROOT/workspace-b" profile="$STATE/vm/profiles/daily"
  mkdir -m 700 "$workspace_b"
  local overlay_before
  overlay_before="$(sha256sum "$profile/disk.qcow2" | awk '{print $1}')"
  vm_lifecycle_identity_state() {
    if [[ "$(profile_status)" == suspended ]] && grep -q '^wait:' "$LIFE_LOG"; then
      printf 'stopped\n'
    else
      printf '%s\n' "$LIFE_IDENTITY"
    fi
  }
  vm_lifecycle_wait_qemu_stopped() {
    printf 'wait:%s\n' "$4" >> "$LIFE_LOG"
    printf '%s\n%s\n' "$STATE/vm/profiles/daily/runs/test/processes.json" test
  }
  vm_guest_payload_hash() {
    printf 'payload:%s\n' "$1" >> "$LIFE_LOG"
    printf '%s\n' 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'
  }
  vm_lifecycle_switch_seed_locked() {
    printf 'seed-failed:%s\n' "$3" >> "$LIFE_LOG"
    return 1
  }
  vm_lifecycle_start_profile_locked() {
    printf 'unexpected-start\n' >> "$LIFE_LOG"
    return 99
  }

  run vm_lifecycle_resume_locked "$STATE" daily "$workspace_b" "$ROOT" 0 0 0 5 '' none '' '' 30 none opencode 2.0.0
  [ "$status" -eq 1 ]
  [ "$(profile_status)" = suspended ]
  python3 - "$VM_LIFECYCLE_PATHS" "$STATE" "$workspace_b" <<'PY'
import json,subprocess,sys
from pathlib import Path
data=json.loads(subprocess.check_output([sys.executable,sys.argv[1],"profile-read","--state-root",sys.argv[2],"--profile","daily"]))
assert data["workspace"]!=sys.argv[3], data
assert data["guest_payload_sha256"]=="b"*64, data
assert data["runtime_identity"]["pid"]==4321, data
PY
  [ "$(sha256sum "$profile/disk.qcow2" | awk '{print $1}')" = "$overlay_before" ]
  [[ "$(cat "$LIFE_LOG")" == *'seed-failed:'"$workspace_b" ]]
  [[ "$(cat "$LIFE_LOG")" != *unexpected-start* ]]
}

@test "ambiguous process identity becomes unknown without shutdown" {
  LIFE_IDENTITY=unknown
  run vm_lifecycle_suspend_locked "$STATE" daily 0 5 "$TEST_ROOT/frozen/lab-vm"
  [ "$status" -eq 2 ]
  [ "$(profile_status)" = unknown ]
  [ ! -s "$LIFE_LOG" ]
}

@test "nested child refusal leaves parent ready and running" {
  LIFE_CHILD_RC=2
  run vm_lifecycle_suspend_locked "$STATE" daily 1 5 "$TEST_ROOT/frozen/lab-vm"
  [ "$status" -eq 2 ]
  [ "$(profile_status)" = ready ]
  [ "$(cat "$LIFE_LOG")" = 'children:1' ]
}

@test "recursive child failure never sends parent shutdown" {
  LIFE_CHILD_RC=5
  run vm_lifecycle_suspend_locked "$STATE" daily 1 5 "$TEST_ROOT/frozen/lab-vm"
  [ "$status" -eq 5 ]
  [ "$(profile_status)" = ready ]
  [ "$(cat "$LIFE_LOG")" = 'children:1' ]
}

@test "shutdown timeout restores ready and does not force-kill QEMU" {
  LIFE_WAIT=running
  run vm_lifecycle_suspend_locked "$STATE" daily 0 1 "$TEST_ROOT/frozen/lab-vm"
  [ "$status" -eq 124 ]
  [ "$(profile_status)" = ready ]
  [ "$(cat "$LIFE_LOG")" = $'children:0\nshutdown\nwait:1' ]
  [ -f "$STATE/vm/profiles/daily/disk.qcow2" ]
}

@test "unverifiable process after shutdown becomes unknown" {
  LIFE_WAIT=unknown
  LIFE_IDENTITY=unknown
  run vm_lifecycle_suspend_locked "$STATE" daily 0 1 "$TEST_ROOT/frozen/lab-vm"
  [ "$status" -eq 2 ]
  [ "$(profile_status)" = unknown ]
  [ ! -s "$LIFE_LOG" ] || [[ "$(cat "$LIFE_LOG")" == $'children:0\nshutdown\nwait:1' ]]
}

@test "sidecar uncertainty leaves profile blocked and keeps overlay" {
  LIFE_STOP_RC=2
  run vm_lifecycle_suspend_locked "$STATE" daily 0 5 "$TEST_ROOT/frozen/lab-vm"
  [ "$status" -eq 2 ]
  [ "$(profile_status)" = unknown ]
  [ -f "$STATE/vm/profiles/daily/disk.qcow2" ]
}

@test "destructive guard requires separate descendant and auth acknowledgements" {
  run vm_lifecycle_destructive_guard down "$PROFILE_JSON" off 0
  [ "$status" -eq 2 ]
  run vm_lifecycle_destructive_guard down "$PROFILE_JSON" enabled 1
  [ "$status" -eq 2 ]
  run vm_lifecycle_destructive_guard down "$PROFILE_JSON" off 1
  [ "$status" -eq 0 ]
}

@test "yes confirmation never substitutes for descendant acknowledgement" {
  run vm_lifecycle_destructive_guard rebuild "$PROFILE_JSON" off 0
  [ "$status" -eq 2 ]
  run vm_lifecycle_require_confirmation down daily 'remove profile overlay and guest state' 1
  [ "$status" -eq 0 ]
}
