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
  export VM_LIFECYCLE_PYTHON="$(command -v python3)"
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
  "argv":["/usr/bin/qemu-system-x86_64","-name","daily","-drive",f"file={disk},format=qcow2,if=virtio"]}}
paths.profile_write(root,"daily",data)
disk.write_text("fixture overlay\n"); disk.chmod(0o600)
PY
  export PROFILE_JSON="$(python3 "$VM_LIFECYCLE_PATHS" profile-read --state-root "$STATE" --profile daily)"
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
