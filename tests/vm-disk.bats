#!/usr/bin/env bats
# V-IMAGE: disposable fixtures only; mocks curl/qemu-img and never boots a VM.

setup() {
  ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)"
  SYSTEM_PYTHON="$(command -v python3)"
  export SYSTEM_PYTHON
  VM_TEST_ROOT="$(mktemp -d /tmp/little-mester-disk.XXXXXX)"
  chmod 700 "$VM_TEST_ROOT"
  export HOME="$VM_TEST_ROOT/home"
  mkdir -m 700 "$HOME" "$VM_TEST_ROOT/bin" "$VM_TEST_ROOT/workspace"
  export MESTER_STATE_DIR="$VM_TEST_ROOT/state"
  export VM_PATHS_HELPER="$ROOT/lib/vm/paths.py"
  export FAKE_SOURCE="$VM_TEST_ROOT/source.qcow2"
  export FAKE_CURL_LOG="$VM_TEST_ROOT/curl.log"
  export FAKE_QEMU_LOG="$VM_TEST_ROOT/qemu.log"
  export FAKE_BOOT_BAD=0 FAKE_SOURCE_FORMAT=qcow2
  unset VM_NESTED_LAUNCH FAKE_FAIL_DOWNLOAD FAKE_QEMU_FAIL FAKE_CURL_DELAY
  python3 - "$FAKE_SOURCE" <<'PY'
from pathlib import Path
import sys
Path(sys.argv[1]).write_bytes(b"FIXTURE QCOW2 IMAGE\n" * 32)
PY
  VM_IMAGE_SHA256="$(sha256sum "$FAKE_SOURCE" | awk '{print $1}')"
  export VM_IMAGE_SHA256
  export PATH="$VM_TEST_ROOT/bin:$PATH"

  cat > "$VM_TEST_ROOT/bin/curl" <<'CURL'
#!/usr/bin/env python3
import os, shutil, sys
args=sys.argv[1:]
with open(os.environ["FAKE_CURL_LOG"], "a", encoding="utf-8") as stream:
    stream.write(" ".join(args)+"\n")
if os.environ.get("FAKE_FAIL_DOWNLOAD") == "1":
    raise SystemExit(22)
if os.environ.get("FAKE_CURL_DELAY"):
    import time
    time.sleep(float(os.environ["FAKE_CURL_DELAY"]))
out=args[args.index("--output")+1]
shutil.copyfile(os.environ["FAKE_SOURCE"], out)
CURL

  cat > "$VM_TEST_ROOT/bin/qemu-img" <<'QEMU'
#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args=sys.argv[1:]
with open(os.environ["FAKE_QEMU_LOG"], "a", encoding="utf-8") as stream:
    stream.write(" ".join(args)+"\n")
if os.environ.get("FAKE_QEMU_FAIL") == (args[0] if args else ""):
    raise SystemExit(1)
MAGIC=b"LM-QCOW-FIXTURE-1\n"
def metadata(path):
    data=Path(path).read_bytes()
    if data.startswith(MAGIC):
        return json.loads(data[len(MAGIC):])
    return {"filename":str(Path(path).resolve()),"format":os.environ.get("FAKE_SOURCE_FORMAT","qcow2"),
            "virtual-size":1024*1024*1024,"backing-filename":None,"bootable":True}
def write_meta(path, item):
    Path(path).write_bytes(MAGIC+json.dumps(item,sort_keys=True).encode())
if args[0] == "info":
    chain="--backing-chain" in args
    path=args[-1]
    item=metadata(path)
    item["filename"]=str(Path(path).resolve())
    if os.environ.get("FAKE_SOURCE_FORMAT") != "qcow2" and not Path(path).read_bytes().startswith(MAGIC):
        item["format"]=os.environ["FAKE_SOURCE_FORMAT"]
    if chain:
        result=[item]
        backing=item.get("backing-filename")
        if backing:
            backing_item=metadata(backing)
            backing_item["filename"]=str(Path(backing).resolve())
            result.append(backing_item)
    else:
        result=item
    print(json.dumps(result))
elif args[0] == "check":
    raise SystemExit(0)
elif args[0] == "dd":
    values=dict(arg.split("=",1) for arg in args[1:] if "=" in arg)
    sector=bytearray(512)
    if os.environ.get("FAKE_BOOT_BAD") != "1": sector[510:512]=b"\x55\xaa"
    Path(values["of"]).write_bytes(sector)
elif args[0] == "convert":
    source,dest=args[-2:]
    item={"filename":str(Path(dest).resolve()),"format":"qcow2","virtual-size":1024*1024*1024,
          "backing-filename":None,"bootable":True}
    write_meta(dest,item)
elif args[0] == "resize":
    path,size=args[1:]
    item=metadata(path)
    item["virtual-size"]=int(size[:-1])*1024**3
    write_meta(path,item)
elif args[0] == "create":
    backing=args[args.index("-b")+1]
    dest=args[args.index("-b")+2]
    size=args[-1]
    item={"filename":str(Path(dest).resolve()),"format":"qcow2",
          "virtual-size":int(size[:-1])*1024**3,
          "backing-filename":str(Path(backing).resolve()),"bootable":False}
    write_meta(dest,item)
else:
    raise SystemExit(2)
QEMU
  chmod 700 "$VM_TEST_ROOT/bin/curl" "$VM_TEST_ROOT/bin/qemu-img"
  # shellcheck disable=SC1091 # Bats resolves the fixture checkout root dynamically.
  source "$ROOT/lib/vm/disk.sh"
}

teardown() {
  if [[ "$VM_TEST_ROOT" == /tmp/little-mester-disk.* && -O "$VM_TEST_ROOT" ]]; then
    rm -rf -- "$VM_TEST_ROOT"
  fi
}

create_profile() {
  local name="$1"
  python3 - "$MESTER_STATE_DIR" "$VM_TEST_ROOT/workspace" "$name" "$VM_IMAGE_SHA256" <<'PY' | \
    python3 "$VM_PATHS_HELPER" profile-write --state-root "$MESTER_STATE_DIR" --profile "$name" >/dev/null
import json,sys,uuid
root,workspace,name,digest=sys.argv[1:]
print(json.dumps({"schema_version":1,"profile_id":str(uuid.uuid4()),"name":name,
 "workspace":workspace,"image_sha256":digest,
 "image_path":root+"/vm/images/"+digest+"/base.qcow2","memory_gib":4,
 "depth":0,"max_depth":0,"descendants_possible":False,"opencode_version":None,
 "guest_payload_sha256":None,"runtime_payload_sha256":None,"status":"allocated"}))
PY
}

mock_statvfs() {
  cat > "$VM_TEST_ROOT/bin/python3" <<'PYMOCK'
#!/usr/bin/env bash
  if [[ "${1:-}" == - ]]; then
  code="$(cat)"
  if [[ "$code" == *statvfs* ]]; then printf '%s\n' '@STAT_FREE@'; exit 0; fi
  printf '%s' "$code" | exec "$SYSTEM_PYTHON" "$@"
fi
exec "$SYSTEM_PYTHON" "$@"
PYMOCK
  sed -i "s/@STAT_FREE@/$1/" "$VM_TEST_ROOT/bin/python3"
  chmod 700 "$VM_TEST_ROOT/bin/python3"
}

@test "verified pin installs immutable standalone bootable base atomically" {
  run vm_disk_create_base "$MESTER_STATE_DIR" "https://images.example.invalid/pinned.img" "$VM_IMAGE_SHA256" 20
  [ "$status" -eq 0 ]
  [ -f "$MESTER_STATE_DIR/vm/images/$VM_IMAGE_SHA256/base.qcow2" ]
  [ -f "$MESTER_STATE_DIR/vm/images/$VM_IMAGE_SHA256/manifest.json" ]
  [ "$(find "$MESTER_STATE_DIR/vm/images/$VM_IMAGE_SHA256" -mindepth 1 -maxdepth 1 -printf '%f\n' | LC_ALL=C sort)" = $'base.qcow2\nmanifest.json' ]
  python3 - "$MESTER_STATE_DIR/vm/images/$VM_IMAGE_SHA256" "$VM_IMAGE_SHA256" <<'PY'
import hashlib,json,sys
from pathlib import Path
cache=Path(sys.argv[1]); manifest=json.loads((cache/"manifest.json").read_text())
assert manifest["source_sha256"]==sys.argv[2]
assert manifest["artifact_sha256"]==hashlib.sha256((cache/"base.qcow2").read_bytes()).hexdigest()
assert manifest["virtual_size_bytes"]==20*1024**3
assert (cache.stat().st_mode&0o777)==0o700
assert (cache/"base.qcow2").stat().st_mode&0o777==0o600
PY
  [ "$(wc -l < "$FAKE_CURL_LOG")" -eq 1 ]
  run vm_disk_create_base "$MESTER_STATE_DIR" "https://images.example.invalid/pinned.img" "$VM_IMAGE_SHA256" 20
  [ "$status" -eq 0 ]
  [ "$(wc -l < "$FAKE_CURL_LOG")" -eq 1 ]
}

@test "nested image download is pinned to parent proxy without direct fallback" {
  export VM_NESTED_LAUNCH=1
  run vm_disk_create_base "$MESTER_STATE_DIR" "https://images.example.invalid/pinned.img" "$VM_IMAGE_SHA256" 20
  [ "$status" -eq 0 ]
  grep -F -- '--proxy http://10.0.2.100:3128' "$FAKE_CURL_LOG"
  grep -F -- '--noproxy ' "$FAKE_CURL_LOG"
}

@test "untrusted or credential-bearing URL pins reject before private state allocation" {
  run vm_disk_create_base "$MESTER_STATE_DIR" "http://images.example.invalid/pinned.img" "$VM_IMAGE_SHA256" 20
  [ "$status" -eq 2 ]
  run vm_disk_create_base "$MESTER_STATE_DIR" "https://images.example.invalid/pinned.img?token=SECRET_CANARY" "$VM_IMAGE_SHA256" 20
  [ "$status" -eq 2 ]
  [[ "$output" != *SECRET_CANARY* ]]
  [ ! -e "$MESTER_STATE_DIR" ]
  [ ! -e "$FAKE_CURL_LOG" ]
}

@test "bad checksum leaves no digest cache member or staging directory" {
  bad="$(printf '%064d' 0)"
  run vm_disk_create_base "$MESTER_STATE_DIR" "https://images.example.invalid/pinned.img" "$bad" 20
  [ "$status" -eq 1 ] || [ "$status" -eq 2 ]
  [ ! -e "$MESTER_STATE_DIR/vm/images/$bad" ]
  [ -z "$(find "$MESTER_STATE_DIR/vm/images" -maxdepth 1 -name '.staging.*' -print -quit)" ]
  [ ! -e "$FAKE_QEMU_LOG" ] # Checksum mismatch must happen before any qemu-img use.
}

@test "format boot-marker conversion and download failures leave cache absent" {
  for failure in format boot convert download; do
    case "$failure" in
      format) export FAKE_SOURCE_FORMAT=raw ;;
      boot) export FAKE_BOOT_BAD=1 ;;
      convert) export FAKE_QEMU_FAIL=convert ;;
      download) export FAKE_FAIL_DOWNLOAD=1 ;;
    esac
    run vm_disk_create_base "$MESTER_STATE_DIR" "https://images.example.invalid/$failure.img" "$VM_IMAGE_SHA256" 20
    [ "$status" -ne 0 ]
    [ ! -e "$MESTER_STATE_DIR/vm/images/$VM_IMAGE_SHA256" ]
    unset FAKE_SOURCE_FORMAT FAKE_BOOT_BAD FAKE_QEMU_FAIL FAKE_FAIL_DOWNLOAD
    export FAKE_BOOT_BAD=0 FAKE_SOURCE_FORMAT=qcow2
  done
  [ -z "$(find "$MESTER_STATE_DIR/vm/images" -maxdepth 1 -name '.staging.*' -print -quit)" ]
}

@test "corrupt existing cache blocks even with ambient yes and remains byte-identical" {
  cache="$MESTER_STATE_DIR/vm/images/$VM_IMAGE_SHA256"
  mkdir -m 700 "$MESTER_STATE_DIR"
  mkdir -m 700 "$MESTER_STATE_DIR/vm" "$MESTER_STATE_DIR/vm/images" "$cache"
  printf 'preserve corrupt cache\n' > "$cache/base.qcow2"
  chmod 600 "$cache/base.qcow2"
  before="$(sha256sum "$cache/base.qcow2" | awk '{print $1}')"
  # shellcheck disable=SC2016 # Fixed test worker sources the helper in a child shell.
  run env VM_YES=1 bash -c 'source "$1/lib/vm/disk.sh"; vm_disk_create_base "$2" "$3" "$4" 20' \
    _ "$ROOT" "$MESTER_STATE_DIR" "https://images.example.invalid/pinned.img" "$VM_IMAGE_SHA256"
  [ "$status" -ne 0 ]
  [ "$(sha256sum "$cache/base.qcow2" | awk '{print $1}')" = "$before" ]
  [ ! -e "$FAKE_CURL_LOG" ]
}

@test "tampered published artifact is preserved and never silently replaced" {
  run vm_disk_create_base "$MESTER_STATE_DIR" "https://images.example.invalid/pinned.img" "$VM_IMAGE_SHA256" 20
  [ "$status" -eq 0 ]
  artifact="$MESTER_STATE_DIR/vm/images/$VM_IMAGE_SHA256/base.qcow2"
  printf 'tamper\n' >> "$artifact"
  before="$(sha256sum "$artifact" | awk '{print $1}')"
  # shellcheck disable=SC2016 # Fixed test worker sources the helper in a child shell.
  run env VM_YES=1 bash -c 'source "$1/lib/vm/disk.sh"; vm_disk_create_base "$2" "$3" "$4" 20' \
    _ "$ROOT" "$MESTER_STATE_DIR" "https://images.example.invalid/pinned.img" "$VM_IMAGE_SHA256"
  [ "$status" -eq 2 ]
  [ "$(sha256sum "$artifact" | awk '{print $1}')" = "$before" ]
  [ "$(wc -l < "$FAKE_CURL_LOG")" -eq 1 ]
}

@test "unknown cache members block reuse without being deleted" {
  run vm_disk_create_base "$MESTER_STATE_DIR" "https://images.example.invalid/pinned.img" "$VM_IMAGE_SHA256" 20
  [ "$status" -eq 0 ]
  marker="$MESTER_STATE_DIR/vm/images/$VM_IMAGE_SHA256/unrecognized"
  printf 'preserve\n' > "$marker"
  chmod 600 "$marker"
  run vm_disk_create_base "$MESTER_STATE_DIR" "https://images.example.invalid/pinned.img" "$VM_IMAGE_SHA256" 20
  [ "$status" -eq 2 ]
  [ "$(cat "$marker")" = preserve ]
  [ "$(wc -l < "$FAKE_CURL_LOG")" -eq 1 ]
}

@test "parallel same-pin requests serialize to one installed cache artifact" {
  export FAKE_CURL_DELAY=0.2
  bash -c 'source "$1/lib/vm/disk.sh"; vm_disk_create_base "$2" "$3" "$4" 20' \
    _ "$ROOT" "$MESTER_STATE_DIR" "https://images.example.invalid/pinned.img" "$VM_IMAGE_SHA256" &
  first=$!
  bash -c 'source "$1/lib/vm/disk.sh"; vm_disk_create_base "$2" "$3" "$4" 20' \
    _ "$ROOT" "$MESTER_STATE_DIR" "https://images.example.invalid/pinned.img" "$VM_IMAGE_SHA256" &
  second=$!
  wait "$first"
  wait "$second"
  [ "$(wc -l < "$FAKE_CURL_LOG")" -eq 1 ]
  [ -f "$MESTER_STATE_DIR/vm/images/$VM_IMAGE_SHA256/manifest.json" ]
}

@test "insufficient free space refuses before creating staging or downloading" {
  mock_statvfs 0
  run vm_disk_create_base "$MESTER_STATE_DIR" "https://images.example.invalid/pinned.img" "$VM_IMAGE_SHA256" 20
  [ "$status" -ne 0 ]
  [ ! -e "$FAKE_CURL_LOG" ]
  [ -z "$(find "$MESTER_STATE_DIR/vm/images" -maxdepth 1 -name '.staging.*' -print -quit)" ]
  [ ! -e "$MESTER_STATE_DIR/vm/images/$VM_IMAGE_SHA256" ]
}

@test "insufficient profile disk space refuses before overlay staging" {
  run vm_disk_create_base "$MESTER_STATE_DIR" "https://images.example.invalid/pinned.img" "$VM_IMAGE_SHA256" 20
  [ "$status" -eq 0 ]
  create_profile alpha
  mock_statvfs 0
  run vm_disk_create_overlay "$MESTER_STATE_DIR" alpha 20
  [ "$status" -eq 2 ]
  [ ! -e "$MESTER_STATE_DIR/vm/profiles/alpha/disk.qcow2" ]
  [ -z "$(find "$MESTER_STATE_DIR/vm/profiles/alpha" -maxdepth 1 -name '.overlay.*' -print -quit)" ]
}

@test "two profile overlays keep exact immutable base backing and distinct writable files" {
  run vm_disk_create_base "$MESTER_STATE_DIR" "https://images.example.invalid/pinned.img" "$VM_IMAGE_SHA256" 20
  [ "$status" -eq 0 ]
  create_profile alpha
  create_profile beta
  run vm_disk_create_overlay "$MESTER_STATE_DIR" alpha 20
  [ "$status" -eq 0 ]
  run vm_disk_create_overlay "$MESTER_STATE_DIR" beta 20
  [ "$status" -eq 0 ]
  [ -f "$MESTER_STATE_DIR/vm/profiles/alpha/disk.qcow2" ]
  [ -f "$MESTER_STATE_DIR/vm/profiles/beta/disk.qcow2" ]
  [ "$(sha256sum "$MESTER_STATE_DIR/vm/images/$VM_IMAGE_SHA256/base.qcow2" | awk '{print $1}')" = \
    "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["artifact_sha256"])' "$MESTER_STATE_DIR/vm/images/$VM_IMAGE_SHA256/manifest.json")" ]
  [ "$(sha256sum "$MESTER_STATE_DIR/vm/profiles/alpha/disk.qcow2" | awk '{print $1}')" != \
    "$(sha256sum "$MESTER_STATE_DIR/vm/profiles/beta/disk.qcow2" | awk '{print $1}')" ]
  base_before="$(sha256sum "$MESTER_STATE_DIR/vm/images/$VM_IMAGE_SHA256/base.qcow2" | awk '{print $1}')"
  alpha_before="$(sha256sum "$MESTER_STATE_DIR/vm/profiles/alpha/disk.qcow2" | awk '{print $1}')"
  beta_before="$(sha256sum "$MESTER_STATE_DIR/vm/profiles/beta/disk.qcow2" | awk '{print $1}')"
  run vm_disk_create_overlay "$MESTER_STATE_DIR" alpha 20
  [ "$status" -eq 0 ]
  [ "$(sha256sum "$MESTER_STATE_DIR/vm/images/$VM_IMAGE_SHA256/base.qcow2" | awk '{print $1}')" = "$base_before" ]
  [ "$(sha256sum "$MESTER_STATE_DIR/vm/profiles/alpha/disk.qcow2" | awk '{print $1}')" = "$alpha_before" ]
  [ "$(sha256sum "$MESTER_STATE_DIR/vm/profiles/beta/disk.qcow2" | awk '{print $1}')" = "$beta_before" ]
}

@test "existing overlay with wrong backing refuses and is never replaced" {
  run vm_disk_create_base "$MESTER_STATE_DIR" "https://images.example.invalid/pinned.img" "$VM_IMAGE_SHA256" 20
  [ "$status" -eq 0 ]
  create_profile alpha
  run vm_disk_create_overlay "$MESTER_STATE_DIR" alpha 20
  [ "$status" -eq 0 ]
  overlay="$MESTER_STATE_DIR/vm/profiles/alpha/disk.qcow2"
  wrong_base="$VM_TEST_ROOT/wrong-base.qcow2"
  python3 - "$wrong_base" "$overlay" <<'PY'
import json,sys
from pathlib import Path
magic=b"LM-QCOW-FIXTURE-1\n"
wrong=Path(sys.argv[1]); overlay=Path(sys.argv[2])
wrong.write_bytes(magic+json.dumps({"filename":str(wrong.resolve()),"format":"qcow2",
 "virtual-size":20*1024**3,"backing-filename":None,"bootable":True}).encode())
overlay.write_bytes(magic+json.dumps({"filename":str(overlay.resolve()),"format":"qcow2",
 "virtual-size":20*1024**3,"backing-filename":str(wrong.resolve()),"bootable":False}).encode())
PY
  chmod 600 "$wrong_base" "$overlay"
  before="$(sha256sum "$overlay" | awk '{print $1}')"
  run vm_disk_create_overlay "$MESTER_STATE_DIR" alpha 20
  [ "$status" -ne 0 ]
  [ "$(sha256sum "$overlay" | awk '{print $1}')" = "$before" ]
}

@test "seed output is atomic private data and secret-like input is rejected" {
  run vm_disk_create_base "$MESTER_STATE_DIR" "https://images.example.invalid/pinned.img" "$VM_IMAGE_SHA256" 20
  [ "$status" -eq 0 ]
  create_profile alpha
  seed="$VM_TEST_ROOT/user-data.yaml"
  printf '#cloud-config\npackages:\n - qemu-guest-agent\n' > "$seed"
  run bash -c 'source "$1/lib/vm/disk.sh"; vm_disk_write_seed "$2" alpha < "$3"' \
    _ "$ROOT" "$MESTER_STATE_DIR" "$seed"
  [ "$status" -eq 0 ]
  [ "$(cat "$MESTER_STATE_DIR/vm/profiles/alpha/seed.user-data")" = "$(cat "$seed")" ]
  [ "$(stat -c %a "$MESTER_STATE_DIR/vm/profiles/alpha/seed.user-data")" = 600 ]
  printf '#cloud-config\napi_token: SECRET_CANARY\n' > "$seed"
  before="$(sha256sum "$MESTER_STATE_DIR/vm/profiles/alpha/seed.user-data" | awk '{print $1}')"
  run bash -c 'source "$1/lib/vm/disk.sh"; vm_disk_write_seed "$2" alpha < "$3"' \
    _ "$ROOT" "$MESTER_STATE_DIR" "$seed"
  [ "$status" -eq 2 ]
  [[ "$output" != *SECRET_CANARY* ]]
  [ "$(sha256sum "$MESTER_STATE_DIR/vm/profiles/alpha/seed.user-data" | awk '{print $1}')" = "$before" ]
}
