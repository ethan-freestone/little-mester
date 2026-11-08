#!/usr/bin/env bash
# Immutable checksum-keyed qcow2 cache, profile-owned overlays and atomic seed data.
# This file is inert when sourced; network/image work is an explicit function call.

VM_DISK_PIN_URL="https://cloud-images.ubuntu.com/releases/noble/release-20261017/ubuntu-24.04-server-cloudimg-amd64.img"
VM_DISK_PIN_SHA256="04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94"
VM_BASE_SIZE=20
VM_DISK_SOURCE_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
VM_DISK_PATHS_HELPER="${VM_PATHS_HELPER:-$(dirname "$VM_DISK_SOURCE_FILE")/paths.py}"

vm_disk_error() {
  printf 'REFUSED: %s\n' "$*" >&2
  return 2
}

vm_disk_profile_name_valid() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]
}

vm_disk_pin_valid() {
  local url="$1" digest="$2"
  [[ "$digest" =~ ^[a-f0-9]{64}$ ]] || return 1
  python3 - "$url" <<'PY'
import sys
from urllib.parse import urlsplit
try:
    value=urlsplit(sys.argv[1])
    ok=(value.scheme=="https" and bool(value.hostname) and value.username is None
        and value.password is None and value.port in (None,443) and not value.fragment and not value.query
        and not any(ord(c)<32 or ord(c)==127 for c in sys.argv[1]))
except (ValueError,UnicodeError):
    ok=False
raise SystemExit(0 if ok else 1)
PY
}

vm_disk_free_bytes() {
  python3 - "$1" <<'PY'
import os,sys
stat=os.statvfs(sys.argv[1])
print(stat.f_bavail*stat.f_frsize)
PY
}

vm_disk_qcow_info_valid() {
  local path="$1" require_boot="$2" expected_bytes="${3:-0}" info sector result=0
  [[ -f "$path" && ! -L "$path" ]] || return 1
  info="$(qemu-img info --output=json "$path" 2>/dev/null)" || return 1
  printf '%s' "$info" | python3 -c '
import json,sys
try:
 d=json.load(sys.stdin)
 assert isinstance(d,dict) and d.get("format")=="qcow2"
 assert not d.get("backing-filename") and not d.get("full-backing-filename")
 size=d.get("virtual-size")
 assert type(size) is int and size>0
 expected=int(sys.argv[1])
 assert not expected or size==expected
except Exception: raise SystemExit(1)
' "$expected_bytes" || return 1
  qemu-img check "$path" >/dev/null 2>&1 || return 1
  if [[ "$require_boot" == 1 ]]; then
    sector="$(mktemp /tmp/little-mester-boot.XXXXXX)" || return 1
    chmod 600 "$sector"
    if ! qemu-img dd -f qcow2 -O raw "if=$path" "of=$sector" bs=512 count=1 >/dev/null 2>&1; then
      rm -f -- "$sector"
      return 1
    fi
    python3 - "$sector" <<'PY' || result=1
import sys
data=open(sys.argv[1],"rb").read(512)
raise SystemExit(0 if len(data)==512 and data[510:512]==b"\x55\xaa" else 1)
PY
    rm -f -- "$sector"
    (( result == 0 )) || return 1
  fi
}

vm_disk_manifest_valid() {
  local cache="$1" url="$2" digest="$3" expected_bytes="$4" manifest artifact entries manifest_size
  [[ -d "$cache" && ! -L "$cache" && -f "$cache/manifest.json" && ! -L "$cache/manifest.json" ]] || return 1
  [[ -f "$cache/base.qcow2" && ! -L "$cache/base.qcow2" ]] || return 1
  manifest_size="$(stat -c '%s' -- "$cache/manifest.json" 2>/dev/null)" || return 1
  [[ "$manifest_size" =~ ^[0-9]+$ ]] && (( manifest_size <= 4096 )) || return 1
  entries="$(python3 - "$cache" <<'PY'
import os,sys
print("\n".join(sorted(os.listdir(sys.argv[1]))))
PY
)" || return 1
  [[ "$entries" == $'base.qcow2\nmanifest.json' ]] || return 1
  manifest="$(cat -- "$cache/manifest.json")" || return 1
  artifact="$(sha256sum -- "$cache/base.qcow2" | awk '{print $1}')" || return 1
  printf '%s' "$manifest" | python3 -c '
import json,re,sys
try:
 d=json.load(sys.stdin)
 assert set(d)=={"schema_version","source_url","source_sha256","artifact_sha256","virtual_size_bytes","format"}
 assert d["schema_version"]==1 and d["format"]=="qcow2"
 assert d["source_url"]==sys.argv[1] and d["source_sha256"]==sys.argv[2]
 assert d["artifact_sha256"]==sys.argv[3] and re.fullmatch(r"[a-f0-9]{64}",d["artifact_sha256"])
 assert type(d["virtual_size_bytes"]) is int and d["virtual_size_bytes"]==int(sys.argv[4])
except Exception: raise SystemExit(1)
' "$url" "$digest" "$artifact" "$expected_bytes" || return 1
  vm_disk_qcow_info_valid "$cache/base.qcow2" 1 "$expected_bytes"
}

vm_disk_create_base() {
  local root="${1:-${MESTER_STATE_DIR:-${HOME}/.local/share/little-mester}}"
  local url="${2:-$VM_DISK_PIN_URL}" digest="${3:-$VM_DISK_PIN_SHA256}" size="${4:-$VM_BASE_SIZE}"
  if [[ ! "$size" =~ ^[1-9][0-9]{0,2}$ ]] || (( 10#$size > 128 )); then
    vm_disk_error 'Base image size must be whole GiB from 1 to 128.'; return 2
  fi
  vm_disk_pin_valid "$url" "$digest" || { vm_disk_error 'Image pin must be an HTTPS URL and lowercase SHA-256.'; return 2; }
  [[ -f "$VM_DISK_PATHS_HELPER" ]] || { vm_disk_error 'Private state policy helper is unavailable.'; return 2; }
  local tool
  for tool in python3 qemu-img curl sha256sum awk stat mktemp; do
    command -v "$tool" >/dev/null 2>&1 || { vm_disk_error 'A required image preparation tool is unavailable; no state or download was started.'; return 2; }
  done
  # shellcheck disable=SC2016 # Fixed worker source runs in a separate Bash under the Python-held cache lock.
  python3 "$VM_DISK_PATHS_HELPER" image-run --state-root "$root" --sha256 "$digest" -- \
    bash -c 'set -euo pipefail; source "$1"; shift; vm_disk_create_base_locked "$@"' \
    _ "$VM_DISK_SOURCE_FILE" "$url" "$digest" "$size"
}

vm_disk_create_base_locked() {
  local url="$1" digest="$2" size="$3" images="$VM_IMAGE_IMAGES_DIR"
  local cache="$VM_IMAGE_CACHE_DIR" expected_bytes=$((10#$size * 1024 * 1024 * 1024))
  local free_bytes max_download stage download converted actual artifact
  local -a curl_args
  if [[ -e "$cache" || -L "$cache" ]]; then
    vm_disk_manifest_valid "$cache" "$url" "$digest" "$expected_bytes" || {
      vm_disk_error 'Existing image cache is invalid or belongs to another pin; it was preserved.'; return 2;
    }
    printf '%s\n' "$cache/base.qcow2"
    return 0
  fi
  free_bytes="$(vm_disk_free_bytes "$images")" || { vm_disk_error 'Could not check image-cache free space.'; return 2; }
  [[ "$free_bytes" =~ ^[0-9]+$ ]] || { vm_disk_error 'Could not check image-cache free space.'; return 2; }
  max_download=1073741824
  local minimum=2147483648
  (( free_bytes >= minimum && free_bytes >= max_download )) || {
    vm_disk_error 'Insufficient free space for safely staging the pinned image.'; return 2;
  }
  umask 077
  stage="$(mktemp -d "$images/.staging.$digest.XXXXXX")" || { vm_disk_error 'Could not create private image staging.'; return 1; }
  VM_DISK_STAGE_PATH="$stage"
  trap 'if [[ -n "${VM_DISK_STAGE_PATH:-}" && -d "$VM_DISK_STAGE_PATH" ]]; then rm -rf -- "$VM_DISK_STAGE_PATH"; fi' EXIT
  download="$stage/source.img"
  : > "$download"
  chmod 600 "$download"
  curl_args=(--silent --show-error --fail --location --retry 2 --max-filesize "$max_download"
             --proto '=https' --proto-redir '=https' --output "$download")
  if [[ "${VM_NESTED_LAUNCH:-0}" == 1 ]]; then
    curl_args+=(--proxy http://10.0.2.100:3128 --noproxy '')
  fi
  if ! curl "${curl_args[@]}" "$url" >/dev/null 2>"$stage/download.stderr"; then
    vm_disk_error 'Pinned image download failed; no direct-network fallback was attempted.'; return 1
  fi
  actual="$(sha256sum -- "$download" | awk '{print $1}')" || return 1
  [[ "$actual" == "$digest" ]] || { vm_disk_error 'Pinned image checksum mismatch; staged bytes were discarded.'; return 1; }
  local source_info
  source_info="$(qemu-img info --output=json "$download" 2>/dev/null)" || {
    vm_disk_error 'Downloaded image format could not be inspected.'; return 1;
  }
  printf '%s' "$source_info" | python3 -c '
import json,sys
try:
 d=json.load(sys.stdin)
 assert isinstance(d,dict) and d.get("format")=="qcow2"
 assert not d.get("backing-filename") and not d.get("full-backing-filename")
except Exception: raise SystemExit(1)
' || { vm_disk_error 'Pinned source is not a standalone qcow2 image.'; return 1; }
  converted="$stage/base.qcow2"
  if ! qemu-img convert -f qcow2 -O qcow2 "$download" "$converted" >/dev/null 2>"$stage/convert.stderr"; then
    vm_disk_error 'Pinned image conversion failed; staged bytes were discarded.'; return 1
  fi
  if ! qemu-img resize "$converted" "${size}G" >/dev/null 2>"$stage/resize.stderr"; then
    vm_disk_error 'Base image resize failed; staged bytes were discarded.'; return 1
  fi
  chmod 600 "$converted"
  vm_disk_qcow_info_valid "$converted" 1 "$expected_bytes" || {
    vm_disk_error 'Converted image is not a valid standalone bootable qcow2; staged bytes were discarded.'; return 1;
  }
  artifact="$(sha256sum -- "$converted" | awk '{print $1}')" || return 1
  python3 - "$stage/manifest.json" "$url" "$digest" "$artifact" "$expected_bytes" <<'PY'
import json,os,sys
path,url,source,artifact,size=sys.argv[1:]
data={"schema_version":1,"source_url":url,"source_sha256":source,
      "artifact_sha256":artifact,"virtual_size_bytes":int(size),"format":"qcow2"}
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
with os.fdopen(fd,"w",encoding="utf-8") as stream:
    json.dump(data,stream,sort_keys=True,separators=(",",":"))
    stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
PY
  rm -f -- "$download" "$stage/download.stderr" "$stage/convert.stderr" "$stage/resize.stderr"
  if ! python3 "$VM_DISK_PATHS_HELPER" image-publish --state-root "$VM_IMAGE_STATE_ROOT" \
    --sha256 "$digest" --staging "$stage" >/dev/null; then
    vm_disk_error 'Atomic image-cache publication failed; existing cache data was preserved.'; return 1
  fi
  stage=''
  VM_DISK_STAGE_PATH=''
  trap - EXIT
  printf '%s\n' "$cache/base.qcow2"
}

vm_disk_overlay_chain_valid() {
  local overlay="$1" base="$2" expected_bytes="$3" chain
  [[ -f "$overlay" && ! -L "$overlay" && -f "$base" && ! -L "$base" ]] || return 1
  qemu-img check "$overlay" >/dev/null 2>&1 || return 1
  chain="$(qemu-img info --backing-chain --output=json "$overlay" 2>/dev/null)" || return 1
  printf '%s' "$chain" | python3 -c '
import json,os,sys
try:
 chain=json.load(sys.stdin)
 assert isinstance(chain,list) and len(chain)==2
 overlay,base=chain
 expected=os.path.realpath(sys.argv[1])
 def backing(item): return item.get("full-backing-filename") or item.get("backing-filename")
 assert overlay.get("format")=="qcow2" and base.get("format")=="qcow2"
 assert os.path.realpath(backing(overlay))==expected
 assert os.path.realpath(base.get("filename"))==expected
 assert not backing(base)
 assert type(overlay.get("virtual-size")) is int and overlay["virtual-size"]==int(sys.argv[2])
 assert base.get("virtual-size")==overlay.get("virtual-size")
except Exception: raise SystemExit(1)
' "$base" "$expected_bytes"
}

vm_disk_create_overlay() {
  local root="$1" profile="$2" size="${3:-}"
  vm_disk_profile_name_valid "$profile" || { vm_disk_error 'Invalid profile name.'; return 2; }
  [[ -f "$VM_DISK_PATHS_HELPER" ]] || { vm_disk_error 'Private state policy helper is unavailable.'; return 2; }
  command -v qemu-img >/dev/null 2>&1 || { vm_disk_error 'qemu-img is required; profile state was not changed.'; return 2; }
  # shellcheck disable=SC2016 # Fixed worker source runs in a separate Bash under the Python-held profile lock.
  python3 "$VM_DISK_PATHS_HELPER" profile-run --state-root "$root" --profile "$profile" -- \
    bash -c 'set -euo pipefail; source "$1"; shift; vm_disk_create_overlay_locked "$@"' \
    _ "$VM_DISK_SOURCE_FILE" "$root" "$profile" "$size"
}

vm_disk_create_overlay_locked() {
  local root="$1" profile="$2" requested_size="$3" profile_path="$VM_PROFILE_PATH"
  local base="$VM_PROFILE_IMAGE_PATH" digest="$VM_PROFILE_IMAGE_SHA256" manifest cache expected_bytes size stage overlay free_bytes
  [[ "$digest" =~ ^[a-f0-9]{64}$ && "$base" == "$root/vm/images/$digest/base.qcow2" ]] || {
    vm_disk_error 'Profile image identity does not match the immutable cache layout.'; return 2;
  }
  cache="$(dirname "$base")"
  [[ -f "$cache/manifest.json" && ! -L "$cache/manifest.json" ]] || {
    vm_disk_error 'Profile base image cache manifest is missing.'; return 2;
  }
  local manifest_size
  manifest_size="$(stat -c '%s' -- "$cache/manifest.json" 2>/dev/null)" || return 2
  if [[ ! "$manifest_size" =~ ^[0-9]+$ ]] || (( manifest_size > 4096 )); then
    vm_disk_error 'Profile base cache metadata is invalid.'; return 2
  fi
  manifest="$(cat -- "$cache/manifest.json")" || return 2
  local source_url
  source_url="$(printf '%s' "$manifest" | python3 -c '
import json,sys
try:
 d=json.load(sys.stdin); print(d["source_url"])
except Exception: raise SystemExit(1)
' )" || { vm_disk_error 'Profile base cache metadata is invalid.'; return 2; }
  expected_bytes="$(printf '%s' "$manifest" | python3 -c '
import json,sys
try:
 d=json.load(sys.stdin); assert d.get("source_sha256")==sys.argv[1]
 assert d.get("format")=="qcow2" and type(d.get("virtual_size_bytes")) is int
 assert d["virtual_size_bytes"]%1024**3==0
 print(d["virtual_size_bytes"])
except Exception: raise SystemExit(1)
' "$digest")" || { vm_disk_error 'Profile base cache metadata is invalid.'; return 2; }
  vm_disk_manifest_valid "$cache" "$source_url" "$digest" "$expected_bytes" || {
    vm_disk_error 'Profile base cache failed immutable manifest validation.'; return 2;
  }
  size=$((expected_bytes / 1024 / 1024 / 1024))
  if [[ -n "$requested_size" ]]; then
    if [[ ! "$requested_size" =~ ^[1-9][0-9]{0,2}$ ]] || (( 10#$requested_size > 128 || 10#$requested_size != size )); then
      vm_disk_error 'Overlay capacity must match its immutable base image.'; return 2
    fi
  fi
  overlay="$profile_path/disk.qcow2"
  if [[ -e "$overlay" || -L "$overlay" ]]; then
    vm_disk_overlay_chain_valid "$overlay" "$base" "$expected_bytes" || {
      vm_disk_error 'Existing profile overlay has an unexpected backing chain; it was preserved.'; return 2;
    }
    printf '%s\n' "$overlay"
    return 0
  fi
  free_bytes="$(vm_disk_free_bytes "$profile_path")" || { vm_disk_error 'Could not check profile disk free space.'; return 2; }
  if [[ ! "$free_bytes" =~ ^[0-9]+$ ]] || (( free_bytes < 67108864 )); then
    vm_disk_error 'Insufficient free space for safely staging a profile overlay.'; return 2
  fi
  umask 077
  stage="$(mktemp -d "$profile_path/.overlay.XXXXXX")" || { vm_disk_error 'Could not create private overlay staging.'; return 1; }
  local stage_dir="$stage"
  VM_DISK_STAGE_PATH="$stage"
  trap 'if [[ -n "${VM_DISK_STAGE_PATH:-}" && -d "$VM_DISK_STAGE_PATH" ]]; then rm -rf -- "$VM_DISK_STAGE_PATH"; fi' EXIT
  if ! qemu-img create -f qcow2 -F qcow2 -b "$base" "$stage/disk.qcow2" "${size}G" >/dev/null 2>"$stage/create.stderr"; then
    vm_disk_error 'Overlay creation failed; profile and base cache were preserved.'; return 1
  fi
  chmod 600 "$stage/disk.qcow2"
  vm_disk_overlay_chain_valid "$stage/disk.qcow2" "$base" "$expected_bytes" || {
    vm_disk_error 'New overlay backing-chain validation failed.'; return 1;
  }
  rm -f -- "$stage/create.stderr"
  if ! python3 "$VM_DISK_PATHS_HELPER" overlay-publish --state-root "$root" --profile "$profile" \
    --staging "$stage/disk.qcow2" >/dev/null; then
    vm_disk_error 'Atomic overlay publication failed; existing profile data was preserved.'; return 1
  fi
  stage=''
  VM_DISK_STAGE_PATH=''
  trap - EXIT
  # Remove only the now-empty staging directory created by this operation.
  rmdir -- "$stage_dir"
  printf '%s\n' "$overlay"
}

vm_disk_write_seed() {
  local root="$1" profile="$2"
  vm_disk_profile_name_valid "$profile" || { vm_disk_error 'Invalid profile name.'; return 2; }
  python3 "$VM_DISK_PATHS_HELPER" seed-write --state-root "$root" --profile "$profile"
}
