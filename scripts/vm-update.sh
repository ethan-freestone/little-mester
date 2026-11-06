#!/usr/bin/env bash
# Root-only guest updater. Scan records an exact, expiring plan; apply consumes
# only that plan and never rebuilds the VM or touches workspace/auth data.
set -euo pipefail
umask 077

VM_UPDATE_PYTHON="${VM_UPDATE_PYTHON:-python3}"
VM_UPDATE_ROOT="${VM_UPDATE_ROOT:-/}"
VM_UPDATE_TEST_MODE="${VM_UPDATE_TEST_MODE:-0}"
VM_UPDATE_PLAN_DIR="${VM_UPDATE_PLAN_DIR:-/run/lab-vm-update}"
VM_UPDATE_PROC_ROOT="${VM_UPDATE_PROC_ROOT:-/proc}"
VM_UPDATE_ROOT_PREFIX="${VM_UPDATE_ROOT%/}"
[[ -n "$VM_UPDATE_ROOT_PREFIX" ]] || VM_UPDATE_ROOT_PREFIX=/
VM_UPDATE_BOOTSTRAP="${VM_UPDATE_BOOTSTRAP:-$VM_UPDATE_ROOT_PREFIX/usr/local/sbin/lab-vm-guest-bootstrap}"
VM_UPDATE_TUI_CHECKER="${VM_UPDATE_TUI_CHECKER:-$VM_UPDATE_ROOT_PREFIX/usr/local/libexec/lab-vm-opencode-tui-check.py}"
VM_UPDATE_INSTALL_TOOLS="${VM_UPDATE_INSTALL_TOOLS:-$VM_UPDATE_ROOT_PREFIX/usr/local/sbin/lab-vm-install-tools}"
VM_UPDATE_APT_GET_BIN="${VM_UPDATE_APT_GET_BIN:-apt-get}"
VM_UPDATE_APT_BIN="${VM_UPDATE_APT_BIN:-apt}"
VM_UPDATE_NPM_BIN="${VM_UPDATE_NPM_BIN:-npm}"
VM_UPDATE_OPENCODE_BIN="${VM_UPDATE_OPENCODE_BIN:-opencode}"
VM_UPDATE_DOCKER_BIN="${VM_UPDATE_DOCKER_BIN:-docker}"
VM_UPDATE_APT_LOG_TAIL="${VM_UPDATE_APT_LOG_TAIL:-8}"

vm_update_error() { printf 'UPDATE BLOCKED: %s\n' "$*" >&2; }
vm_update_partial() { printf 'UPDATE PARTIAL: %s; guest state and credentials were preserved.\n' "$*" >&2; }
vm_update_rooted() {
  if [[ "$VM_UPDATE_ROOT" == / ]]; then printf '%s\n' "$1"; else printf '%s%s\n' "${VM_UPDATE_ROOT%/}" "$1"; fi
}

if [[ "$VM_UPDATE_TEST_MODE" == 1 ]]; then
  [[ "$VM_UPDATE_ROOT" == /* && "$VM_UPDATE_ROOT" != / && -d "$VM_UPDATE_ROOT" && ! -L "$VM_UPDATE_ROOT" ]] || {
    vm_update_error 'fixture mode requires a separate existing guest root.'; exit 2;
  }
else
  (( EUID == 0 )) || { vm_update_error 'guest updates must run as root.'; exit 2; }
  [[ "$VM_UPDATE_ROOT" == / ]] || { vm_update_error 'non-system guest roots are fixture-only.'; exit 2; }
fi
[[ "$VM_UPDATE_APT_LOG_TAIL" =~ ^[1-9][0-9]{0,2}$ ]] || { vm_update_error 'invalid bounded apt log length.'; exit 2; }
[[ -f "$VM_UPDATE_BOOTSTRAP" && ! -L "$VM_UPDATE_BOOTSTRAP" ]] || { vm_update_error 'guest bootstrap helpers are unavailable.'; exit 2; }

export VM_GUEST_ROOT="$VM_UPDATE_ROOT"
export VM_GUEST_TEST_MODE="$VM_UPDATE_TEST_MODE"
export VM_GUEST_APT_GET_BIN="$VM_UPDATE_APT_GET_BIN"
export VM_GUEST_NPM_BIN="$VM_UPDATE_NPM_BIN"
export VM_GUEST_OPENCODE_BIN="$VM_UPDATE_OPENCODE_BIN"
# shellcheck disable=SC1090 # Fixed, root-owned guest helper; fixture override is isolated.
source "$VM_UPDATE_BOOTSTRAP"

vm_update_json_field() {
  local json="$1" field="$2"
  printf '%s' "$json" | "$VM_UPDATE_PYTHON" -c '
import json,sys
try:
    value=json.load(sys.stdin)
    for part in sys.argv[1].split("."): value=value[part]
    if isinstance(value,bool): print("true" if value else "false")
    elif isinstance(value,(str,int)) or value is None: print("" if value is None else value)
    else: raise ValueError
except Exception: raise SystemExit(2)
' "$field"
}

vm_update_target_field() {
  local json="$1" target="$2" field="$3"
  printf '%s' "$json" | "$VM_UPDATE_PYTHON" -c '
import json,sys
try:
    data=json.load(sys.stdin)
    item=next(item for item in data["targets"] if item["name"]==sys.argv[1])
    value=item[sys.argv[2]]
    if isinstance(value,bool): print("true" if value else "false")
    elif isinstance(value,(str,int)) or value is None: print("" if value is None else value)
    else: raise ValueError
except Exception: raise SystemExit(2)
' "$target" "$field"
}

vm_update_load_context() {
  local marker owner mode links
  marker="$(vm_update_rooted /var/lib/lab-vm/guest-ready.json)"
  [[ -f "$marker" && ! -L "$marker" ]] || { vm_update_error 'ready marker is missing or unsafe.'; return 2; }
  read -r owner mode links < <(stat -c '%u %a %h' -- "$marker") || return 2
  [[ "$mode" == 644 && "$links" == 1 ]] || { vm_update_error 'ready marker permissions are invalid.'; return 2; }
  if [[ "$VM_UPDATE_TEST_MODE" != 1 ]]; then [[ "$owner" == 0 ]] || { vm_update_error 'ready marker is not root-owned.'; return 2; }; fi
  VM_UPDATE_MARKER_JSON="$(cat -- "$marker")" || return 2
  printf '%s' "$VM_UPDATE_MARKER_JSON" | "$VM_UPDATE_PYTHON" -c '
import json,re,sys
try:
    d=json.load(sys.stdin)
    assert d["status"] == "ready" and d["schema_version"] == 1
    assert d["workload_runtime"] in {"docker","none"}
    assert d["agent"] in {"opencode","aider"}
    assert re.fullmatch(r"[a-f0-9]{64}",d["payload_sha256"])
    assert re.fullmatch(r"2\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?",d["opencode_version"])
    selector=d.get("opencode_version_selector","latest")
    assert selector == "latest" or re.fullmatch(r"v?2\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?",selector)
    assert 0 <= d["depth"] <= d["max_depth"] <= 32
    assert d["model_provider"] in {"none","ollama","llamacpp","remote"}
except Exception: raise SystemExit(2)
' || { vm_update_error 'ready marker data is invalid.'; return 2; }

  VM_GUEST_RUNTIME="$(vm_update_json_field "$VM_UPDATE_MARKER_JSON" workload_runtime)" || return 2
  VM_GUEST_AGENT="$(vm_update_json_field "$VM_UPDATE_MARKER_JSON" agent)" || return 2
  VM_GUEST_DEPTH="$(vm_update_json_field "$VM_UPDATE_MARKER_JSON" depth)" || return 2
  VM_GUEST_MAX_DEPTH="$(vm_update_json_field "$VM_UPDATE_MARKER_JSON" max_depth)" || return 2
  VM_GUEST_VERSION_SELECTOR="$(vm_update_json_field "$VM_UPDATE_MARKER_JSON" opencode_version_selector)" || VM_GUEST_VERSION_SELECTOR=latest
  VM_GUEST_PAYLOAD_SHA256="$(vm_update_json_field "$VM_UPDATE_MARKER_JSON" payload_sha256)" || return 2
  VM_GUEST_MODEL_PROVIDER="$(vm_update_json_field "$VM_UPDATE_MARKER_JSON" model_provider)" || return 2
  VM_GUEST_MODEL_PORT="$(vm_update_json_field "$VM_UPDATE_MARKER_JSON" model_port)" || return 2
  VM_GUEST_MODEL_ID="$(vm_update_json_field "$VM_UPDATE_MARKER_JSON" model_id)" || return 2
  # shellcheck disable=SC2034 # Guest bootstrap helpers consume this dynamically.
  VM_GUEST_UID="${VM_UPDATE_UID:-$(stat -c '%u' -- "$(vm_update_rooted /workspace)")}" || return 2
  # shellcheck disable=SC2034 # Guest bootstrap helpers consume this dynamically.
  VM_GUEST_GID="${VM_UPDATE_GID:-$(stat -c '%g' -- "$(vm_update_rooted /workspace)")}" || return 2
  # shellcheck disable=SC2034 # vm_guest_build_private_payload reads the renderer.
  VM_GUEST_CONFIG_RENDERER=/usr/local/libexec/lab-vm-opencode-config.py
  VM_GUEST_APT_GET_BIN="$VM_UPDATE_APT_GET_BIN"
  VM_GUEST_NPM_BIN="$VM_UPDATE_NPM_BIN"
  VM_GUEST_OPENCODE_BIN="$VM_UPDATE_OPENCODE_BIN"
  vm_guest_set_proxy_environment
}

vm_update_plan_dir() { vm_update_rooted "$VM_UPDATE_PLAN_DIR"; }
vm_update_state_path() { vm_update_rooted /var/lib/lab-vm/update-state.json; }

vm_update_hash_source() {
  local helper workspace
  helper="$(vm_update_rooted /usr/local/libexec/lab-vm-guest-hash.sh)"
  workspace="$(vm_update_rooted /workspace)"
  [[ -f "$helper" && ! -L "$helper" && -d "$workspace" && ! -L "$workspace" ]] || {
    vm_update_error 'selected workspace or shared payload hash helper is unavailable.'; return 2;
  }
  bash -c 'source "$1"; shift; vm_guest_payload_hash "$@"' _ "$helper" \
    "$workspace" "$VM_GUEST_RUNTIME" "$VM_GUEST_AGENT" "$VM_GUEST_VERSION_SELECTOR" \
    "$VM_GUEST_DEPTH" "$VM_GUEST_MAX_DEPTH" "$VM_GUEST_MODEL_PROVIDER" \
    "$VM_GUEST_MODEL_PORT" "$VM_GUEST_MODEL_ID"
}

vm_update_state_hash() {
  local state marker_hash
  state="$(vm_update_state_path)"
  marker_hash="$VM_GUEST_PAYLOAD_SHA256"
  if [[ ! -e "$state" && ! -L "$state" ]]; then printf '%s\n' "$marker_hash"; return 0; fi
  [[ -f "$state" && ! -L "$state" ]] || { vm_update_error 'update state is unsafe.'; return 2; }
  "$VM_UPDATE_PYTHON" - "$state" "$VM_UPDATE_TEST_MODE" "$marker_hash" <<'PY'
import json,os,stat,sys
from pathlib import Path
p=Path(sys.argv[1]); test_mode=sys.argv[2]=="1"; fallback=sys.argv[3]
try:
    info=p.lstat()
    assert stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
    assert stat.S_IMODE(info.st_mode)==0o600 and info.st_nlink==1
    assert test_mode or info.st_uid==0
    data=json.loads(p.read_text(encoding="utf-8"))
    value=data.get("payload_sha256",fallback)
    assert isinstance(value,str) and len(value)==64 and all(c in "0123456789abcdef" for c in value)
except Exception: raise SystemExit(2)
print(value)
PY
}

vm_update_write_state() {
  local digest="$1" path
  path="$(vm_update_state_path)"
  VM_UPDATE_STATE_HASH="$digest" "$VM_UPDATE_PYTHON" - "$path" "$VM_UPDATE_TEST_MODE" <<'PY'
import json,os,stat,sys,tempfile
from pathlib import Path
path=Path(sys.argv[1]); test_mode=sys.argv[2]=="1"; digest=os.environ.get("VM_UPDATE_STATE_HASH","")
if len(digest)!=64 or any(c not in "0123456789abcdef" for c in digest): raise SystemExit(2)
path.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
if path.exists() or path.is_symlink():
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=0o600 or (not test_mode and info.st_uid!=0): raise SystemExit(2)
fd,tmp=tempfile.mkstemp(prefix=".update-state-",dir=path.parent)
try:
    os.fchmod(fd,0o600)
    with os.fdopen(fd,"w",encoding="utf-8") as stream:
        json.dump({"schema_version":1,"payload_sha256":digest},stream,sort_keys=True,separators=(",",":")); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
    os.replace(tmp,path); tmp=""
    directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(directory)
    finally: os.close(directory)
finally:
    if tmp and os.path.exists(tmp): os.unlink(tmp)
PY
}

vm_update_resolve_opencode_candidate() {
  local raw version
  raw="$("$VM_UPDATE_NPM_BIN" view @opencode/cli@latest version --json)" || return 1
  version="$(printf '%s' "$raw" | "$VM_UPDATE_PYTHON" -c '
import json,re,sys
try:
    value=json.load(sys.stdin)
    if isinstance(value,list) and len(value)==1: value=value[0]
    assert isinstance(value,str) and re.fullmatch(r"2\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?",value)
except Exception: raise SystemExit(2)
print(value)
')" || return 1
  printf '%s\n' "$version"
}

vm_update_current_opencode() {
  local expected reported normalized
  expected="$(vm_update_json_field "$VM_UPDATE_MARKER_JSON" opencode_version)" || return 2
  reported="$("$VM_UPDATE_OPENCODE_BIN" --version 2>/dev/null)" || return 1
  normalized="$(printf '%s' "$reported" | "$VM_UPDATE_PYTHON" -c '
import re,sys
m=re.search(r"(?<![0-9A-Za-z.])v?(2\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)(?![0-9A-Za-z.])",sys.stdin.read())
if not m: raise SystemExit(2)
print(m.group(1))
')" || return 1
  [[ "$normalized" == "$expected" ]] || { vm_update_error 'installed OpenCode version differs from the ready marker.'; return 2; }
  printf '%s\n' "$expected"
}

vm_update_tools_scan() {
  local tools_dir pip_bin package output current candidate
  local -a entries=()
  tools_dir="$(vm_update_rooted /opt/little-mester/python-tools)"
  pip_bin="$tools_dir/bin/pip"
  [[ -d "$tools_dir" && ! -L "$tools_dir" && -x "$pip_bin" && ! -L "$pip_bin" ]] || {
    vm_update_error 'isolated guest Python tools are unavailable.'; return 1;
  }
  local installed
  installed="$("$pip_bin" list --format=json)" || return 1
  for package in pip ruff uv aider-chat; do
current="$(printf '%s' "$installed" | "$VM_UPDATE_PYTHON" -c '
import json,re,sys
try:
 d=json.load(sys.stdin); key=sys.argv[1].lower().replace("_","-")
 x=[i["version"] for i in d if i["name"].lower().replace("_","-")==key]
 assert len(x)==1 and isinstance(x[0],str)
 assert re.fullmatch(r"[0-9][0-9A-Za-z]*(?:[.+_!-][0-9A-Za-z]+)*",x[0])
 print(x[0])
except Exception: raise SystemExit(2)
' "$package")" || return 1
    output="$("$pip_bin" index versions --proxy http://10.0.2.100:3128 "$package" 2>/dev/null)" || return 1
    candidate="$(printf '%s\n' "$output" | "$VM_UPDATE_PYTHON" -c '
import re,sys
text=sys.stdin.read()
match=re.search(r"^[^\n]*?\(([0-9][0-9A-Za-z]*(?:[.+_!-][0-9A-Za-z]+)*)\)",text,re.M)
if not match:
 for line in text.splitlines():
  if line.startswith("Available versions:"):
   item=line.partition(":")[2].strip().split(",",1)[0].strip()
   if re.fullmatch(r"[0-9][0-9A-Za-z]*(?:[.+_!-][0-9A-Za-z]+)*",item): match=[None,item]
if not match: raise SystemExit(2)
print(match[1])
')" || return 1
    entries+=("$package"$'\t'"$current"$'\t'"$candidate")
  done
  printf '%s\n' "${entries[@]}"
}

vm_update_refresh_apt_metadata() {
  local log
  log="$(mktemp "$(vm_update_plan_dir)/.apt-update.XXXXXX")" || return 1
  if vm_guest_apt_get update >"$log" 2>&1; then
    tail -n "$VM_UPDATE_APT_LOG_TAIL" "$log" >&2 || true
    rm -f -- "$log"
    return 0
  else
    local rc=$?
    tail -n "$VM_UPDATE_APT_LOG_TAIL" "$log" >&2 || true
    rm -f -- "$log"
    return "$rc"
  fi
}

vm_update_apt_packages_scan() {
  local output_file parsed
  output_file="$(mktemp "$(vm_update_plan_dir)/.apt-list.XXXXXX")" || return 1
  if ! "$VM_UPDATE_APT_BIN" list --upgradable >"$output_file" 2>&1; then
    tail -n "$VM_UPDATE_APT_LOG_TAIL" "$output_file" >&2 || true
    rm -f -- "$output_file"
    return 1
  fi
  parsed="$("$VM_UPDATE_PYTHON" - "$output_file" <<'PY'
import json,re,sys
rows=[]
for line in open(sys.argv[1],encoding="utf-8",errors="replace"):
    m=re.match(r"^([a-z0-9][a-z0-9+.-]*)/[^\s]+\s+([^\s]+)\s+[^\s]+\s+\[upgradable from: ([^]]+)\]",line.strip())
    if m:
        name,candidate,current=m.groups()
        if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*",name): raise SystemExit(2)
        if not re.fullmatch(r"[A-Za-z0-9.+:~_-]+",candidate) or not re.fullmatch(r"[A-Za-z0-9.+:~_-]+",current): raise SystemExit(2)
        rows.append({"name":name,"candidate":candidate,"current":current})
for row in sorted(rows,key=lambda x:x["name"]): print(json.dumps(row,separators=(",",":")))
PY
)" || { rm -f -- "$output_file"; return 1; }
  rm -f -- "$output_file"
  printf '%s\n' "$parsed"
}

vm_update_validate_targets() {
  local raw="$1" item
  local -A seen=()
  VM_UPDATE_TARGETS=()
  [[ -n "$raw" ]] || { vm_update_error 'scan requires at least one target.'; return 2; }
  IFS=, read -r -a requested <<<"$raw"
  for item in "${requested[@]}"; do
    case "$item" in opencode|config|tools|system|docker) ;; *) vm_update_error 'unknown update target.'; return 2 ;; esac
    [[ ! -v "seen[$item]" ]] || { vm_update_error 'duplicate update target.'; return 2; }
    seen[$item]=1
    VM_UPDATE_TARGETS+=("$item")
  done
}

vm_update_json_plan_write() {
  local plan_id="$1" data="$2" directory path
  directory="$(vm_update_plan_dir)"
  "$VM_UPDATE_PYTHON" - "$directory" "$VM_UPDATE_TEST_MODE" <<'PY'
import os,stat,sys
from pathlib import Path
path=Path(sys.argv[1]); test_mode=sys.argv[2]=="1"
path.mkdir(mode=0o700,parents=True,exist_ok=True)
info=path.lstat()
if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode): raise SystemExit(2)
if stat.S_IMODE(info.st_mode)!=0o700 or (not test_mode and info.st_uid!=0): raise SystemExit(2)
PY
  [[ ! -L "$directory" && -d "$directory" ]] || { vm_update_error 'update plan directory is unsafe.'; return 2; }
  path="$directory/$plan_id.json"
  PLAN_JSON="$data" "$VM_UPDATE_PYTHON" - "$path" "$VM_UPDATE_TEST_MODE" <<'PY'
import json,os,stat,sys,tempfile
from pathlib import Path
path=Path(sys.argv[1]); test_mode=sys.argv[2]=="1"
data=json.loads(os.environ["PLAN_JSON"])
if path.exists() or path.is_symlink(): raise SystemExit(2)
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
try:
    with os.fdopen(fd,"w",encoding="utf-8") as stream:
        json.dump(data,stream,sort_keys=True,separators=(",",":")); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1 or (not test_mode and info.st_uid!=0): raise SystemExit(2)
except Exception:
    try: path.unlink()
    except OSError: pass
    raise
PY
}

vm_update_scan() {
  local raw_targets="$1" auth_enabled="$2" target current candidate status state_hash source_hash
  local opencode_current='' opencode_candidate='' tools_json='[]' packages_json='[]' apt_output=''
  local candidate_hash='' current_hash=''
  local has_tools_update=0
  local -a target_json=() tool_json=() system_json=() docker_json=()
  vm_update_validate_targets "$raw_targets" || return $?
  [[ "$auth_enabled" == 0 || "$auth_enabled" == 1 ]] || { vm_update_error 'invalid auth state.'; return 2; }
  install -d -m 0700 "$(vm_update_plan_dir)"

  local needs_apt=0
  for target in "${VM_UPDATE_TARGETS[@]}"; do
    if [[ "$target" == system || ( "$target" == docker && "$VM_GUEST_RUNTIME" == docker ) ]]; then needs_apt=1; fi
  done
  if (( needs_apt )); then
    vm_update_refresh_apt_metadata || { vm_update_error 'guest package metadata refresh failed; no update plan was created.'; return 1; }
    apt_output="$(vm_update_apt_packages_scan)" || { vm_update_error 'guest package candidates could not be scanned; no update plan was created.'; return 1; }
    while IFS= read -r target; do
      [[ -n "$target" ]] || continue
      local name cur cand
      name="$(printf '%s' "$target" | "$VM_UPDATE_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["name"])')" || return 1
      cur="$(printf '%s' "$target" | "$VM_UPDATE_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["current"])')" || return 1
      cand="$(printf '%s' "$target" | "$VM_UPDATE_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["candidate"])')" || return 1
      if [[ " ${VM_UPDATE_TARGETS[*]} " == *' system '* ]] && [[ "$name" != docker-ce && "$name" != docker-ce-cli && "$name" != containerd.io && "$name" != docker-buildx-plugin && "$name" != docker-compose-plugin ]]; then
        system_json+=("$target")
      fi
      if [[ " ${VM_UPDATE_TARGETS[*]} " == *' docker '* && "$VM_GUEST_RUNTIME" == docker ]] &&
        [[ "$name" == docker-ce || "$name" == docker-ce-cli || "$name" == containerd.io || "$name" == docker-buildx-plugin || "$name" == docker-compose-plugin ]]; then
        docker_json+=("$target")
      fi
    done <<<"$apt_output"
  fi

  for target in "${VM_UPDATE_TARGETS[@]}"; do
    case "$target" in
      opencode)
        opencode_current="$(vm_update_current_opencode)" || { vm_update_error 'current OpenCode version could not be verified.'; return 1; }
        opencode_candidate="$(vm_update_resolve_opencode_candidate)" || { vm_update_error 'exact OpenCode candidate could not be resolved.'; return 1; }
        status=CURRENT; [[ "$opencode_current" == "$opencode_candidate" ]] || status=UPDATE
        if [[ "$auth_enabled" == 1 && "$status" == UPDATE && "$opencode_candidate" != 2.2.5 ]]; then status=BLOCKED; fi
        target_json+=("$(printf '{\"name\":\"opencode\",\"status\":\"%s\",\"current\":\"%s\",\"candidate\":\"%s\"}' "$status" "$opencode_current" "$opencode_candidate")")
        ;;
      config)
        state_hash="$(vm_update_state_hash)" || return 2
        source_hash="$(vm_update_hash_source)" || { vm_update_error 'selected guest payload could not be hashed.'; return 1; }
        candidate_hash="$source_hash"
        status=CURRENT; [[ "$state_hash" == "$source_hash" ]] || status=UPDATE
        target_json+=("$(printf '{\"name\":\"config\",\"status\":\"%s\",\"current_hash\":\"%s\",\"candidate_hash\":\"%s\"}' "$status" "$state_hash" "$source_hash")")
        ;;
      tools)
        local tools_output
        tools_output="$(vm_update_tools_scan)" || { vm_update_error 'Python tool versions could not be scanned.'; return 1; }
        tool_json=()
        while IFS=$'\t' read -r name cur cand; do
          [[ -n "$name" ]] || continue
          status=CURRENT; [[ "$cur" == "$cand" ]] || status=UPDATE
          [[ "$cur" == "$cand" ]] || has_tools_update=1
          tool_json+=("$(printf '{\"name\":\"%s\",\"current\":\"%s\",\"candidate\":\"%s\"}' "$name" "$cur" "$cand")")
        done <<<"$tools_output"
        tools_json="[$(IFS=,; printf '%s' "${tool_json[*]}")]"
        status=CURRENT; (( has_tools_update == 0 )) || status=UPDATE
        target_json+=("$(printf '{\"name\":\"tools\",\"status\":\"%s\",\"packages\":%s}' "$status" "$tools_json")")
        ;;
      system)
        packages_json="[$(IFS=,; printf '%s' "${system_json[*]}")]"
        status=CURRENT; [[ "$packages_json" == '[]' ]] || status=UPDATE
        target_json+=("$(printf '{\"name\":\"system\",\"status\":\"%s\",\"packages\":%s}' "$status" "$packages_json")")
        ;;
      docker)
        if [[ "$VM_GUEST_RUNTIME" == none ]]; then
          target_json+=( '{"name":"docker","status":"NOT_APPLICABLE","packages":[]}' )
        else
          packages_json="[$(IFS=,; printf '%s' "${docker_json[*]}")]"
          status=CURRENT; [[ "$packages_json" == '[]' ]] || status=UPDATE
          target_json+=("$(printf '{\"name\":\"docker\",\"status\":\"%s\",\"packages\":%s}' "$status" "$packages_json")")
        fi
        ;;
    esac
  done

  local plan_id plan targets_json targets_text
  plan_id="$("$VM_UPDATE_PYTHON" -c 'import secrets; print(secrets.token_hex(16))')" || return 1
  targets_json="[$(IFS=,; printf '%s' "${target_json[*]}")]"
  targets_text="$(IFS=,; printf '%s' "${VM_UPDATE_TARGETS[*]}")"
  [[ -n "$candidate_hash" ]] || candidate_hash="$VM_GUEST_PAYLOAD_SHA256"
  current_hash="$(vm_update_state_hash)" || return 2
  plan="$(VM_UPDATE_TARGETS_JSON="$targets_json" "$VM_UPDATE_PYTHON" - "$plan_id" "$targets_text" "$auth_enabled" "$candidate_hash" "$current_hash" <<'PY'
import json,sys,time
plan_id,targets,auth,candidate,current=sys.argv[1:]
print(json.dumps({"schema_version":1,"plan_id":plan_id,"created":int(time.time()),
 "targets_selected":targets.split(","),"auth_enabled":auth=="1",
 "candidate_payload_sha256":candidate,"current_payload_sha256":current,
 "targets":json.loads(__import__("os").environ["VM_UPDATE_TARGETS_JSON"])},sort_keys=True,separators=(",",":")))
PY
)" || return 1
  VM_UPDATE_TARGETS_JSON="$targets_json" vm_update_json_plan_write "$plan_id" "$plan" || { vm_update_error 'exact update plan could not be recorded safely.'; return 1; }
  printf '%s\n' "$plan"
}

vm_update_plan_load() {
  local plan_id="$1" path owner mode links
  [[ "$plan_id" =~ ^[a-f0-9]{32}$ ]] || { vm_update_error 'invalid update plan identifier.'; return 2; }
  path="$(vm_update_plan_dir)/$plan_id.json"
  "$VM_UPDATE_PYTHON" - "$(vm_update_plan_dir)" "$VM_UPDATE_TEST_MODE" <<'PY'
import os,stat,sys
from pathlib import Path
path=Path(sys.argv[1]); test_mode=sys.argv[2]=="1"
try:
    info=path.lstat()
    assert stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
    assert stat.S_IMODE(info.st_mode)==0o700 and (test_mode or info.st_uid==0)
except Exception: raise SystemExit(2)
PY
  [[ -f "$path" && ! -L "$path" ]] || { vm_update_error 'scanned update plan is missing.'; return 2; }
  read -r owner mode links < <(stat -c '%u %a %h' -- "$path") || return 2
  [[ "$mode" == 600 && "$links" == 1 ]] || { vm_update_error 'scanned update plan permissions are invalid.'; return 2; }
  if [[ "$VM_UPDATE_TEST_MODE" != 1 ]]; then [[ "$owner" == 0 ]] || { vm_update_error 'scanned update plan is not root-owned.'; return 2; }; fi
  VM_UPDATE_PLAN_PATH="$path"
  VM_UPDATE_PLAN_JSON="$(cat -- "$path")" || return 2
  "$VM_UPDATE_PYTHON" - "$path" "$plan_id" <<'PY'
import json,re,sys,time
from pathlib import Path
try:
    d=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")); assert d["schema_version"]==1 and d["plan_id"]==sys.argv[2]
    assert type(d["created"]) is int and 0 <= time.time()-d["created"] <= 3600
    selected=d["targets_selected"]
    records=d["targets"]
    allowed={"opencode","config","tools","system","docker"}
    assert isinstance(selected,list) and selected and len(selected)==len(set(selected))
    assert all(isinstance(item,str) and item in allowed for item in selected)
    assert type(d["auth_enabled"]) is bool
    assert isinstance(records,list) and [x["name"] for x in records]==selected
    assert all(x["status"] in {"CURRENT","UPDATE","BLOCKED","NOT_APPLICABLE"} for x in records)
    for item in records:
        if item["name"]=="opencode":
            assert re.fullmatch(r"2\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?",item["current"])
            assert re.fullmatch(r"2\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?",item["candidate"])
        elif item["name"]=="config":
            assert re.fullmatch(r"[a-f0-9]{64}",item["current_hash"])
            assert re.fullmatch(r"[a-f0-9]{64}",item["candidate_hash"])
        elif item["name"] in {"system","docker","tools"}:
            assert isinstance(item["packages"],list)
            names=set()
            for package in item["packages"]:
                assert package["name"] not in names
                names.add(package["name"])
                assert isinstance(package["name"],str)
                assert isinstance(package["current"],str) and isinstance(package["candidate"],str)
                if item["name"]=="tools":
                    assert package["name"] in {"pip","ruff","uv","aider-chat"}
                    assert re.fullmatch(r"[0-9][0-9A-Za-z]*(?:[.+_!-][0-9A-Za-z]+)*",package["current"])
                    assert re.fullmatch(r"[0-9][0-9A-Za-z]*(?:[.+_!-][0-9A-Za-z]+)*",package["candidate"])
                else:
                    assert re.fullmatch(r"[a-z0-9][a-z0-9+.-]*",package["name"])
                    assert re.fullmatch(r"[A-Za-z0-9.+:~_-]+",package["current"])
                    assert re.fullmatch(r"[A-Za-z0-9.+:~_-]+",package["candidate"])
    assert re.fullmatch(r"[a-f0-9]{64}",d["candidate_payload_sha256"])
    assert re.fullmatch(r"[a-f0-9]{64}",d["current_payload_sha256"])
except Exception: raise SystemExit(2)
PY
}

vm_update_plan_field() { vm_update_json_field "$VM_UPDATE_PLAN_JSON" "$1"; }

vm_update_guard() {
  [[ -f "$VM_UPDATE_TUI_CHECKER" && ! -L "$VM_UPDATE_TUI_CHECKER" ]] || { vm_update_error 'OpenCode TUI guard is unavailable.'; return 2; }
  if "$VM_UPDATE_PYTHON" "$VM_UPDATE_TUI_CHECKER" --proc-root "$VM_UPDATE_PROC_ROOT" >/dev/null; then
    :
  else
    local rc=$?
    vm_update_error 'active or ambiguous OpenCode terminal session blocks guest update.'
    return "$rc"
  fi

  local needs_workload_guard
  needs_workload_guard="$("$VM_UPDATE_PYTHON" - "$VM_UPDATE_PLAN_PATH" <<'PY'
import json,sys
from pathlib import Path
d=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print("1" if any(x["name"] in {"system","docker"} and x["status"]=="UPDATE" for x in d["targets"]) else "0")
PY
)" || return 2
  if [[ "$needs_workload_guard" == 1 && "$VM_GUEST_RUNTIME" == docker ]]; then
    local running
    running="$("$VM_UPDATE_DOCKER_BIN" ps --quiet)" || { vm_update_error 'guest Docker workload state is unavailable; update is blocked.'; return 2; }
    [[ ! "$running" =~ [^[:space:]] ]] || { vm_update_error 'active guest Docker workloads block OS/Docker update.'; return 2; }
  fi
}

vm_update_apply_apt_target() {
  local target="$1" package_json packages
  packages="$("$VM_UPDATE_PYTHON" - "$VM_UPDATE_PLAN_PATH" "$target" <<'PY'
import json,re,sys
from pathlib import Path
d=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")); target=sys.argv[2]
try: items=next(x["packages"] for x in d["targets"] if x["name"]==target)
except Exception: raise SystemExit(2)
for item in items:
 name,version=item["name"],item["candidate"]
 assert re.fullmatch(r"[a-z0-9][a-z0-9+.-]*",name)
 assert re.fullmatch(r"[A-Za-z0-9.+:~_-]+",version)
 print(name+"\t"+version)
PY
)" || return 2
  [[ -n "$packages" ]] || return 0
  local -a args=()
  while IFS=$'\t' read -r package_json current; do
    [[ -n "$package_json" ]] || continue
    args+=("$package_json=$current")
  done <<<"$packages"
  local log rc
  log="$(mktemp "$(vm_update_plan_dir)/.apt-apply.XXXXXX")" || return 1
  if vm_guest_apt_get install -y --no-install-recommends --only-upgrade "${args[@]}" >"$log" 2>&1; then
    tail -n "$VM_UPDATE_APT_LOG_TAIL" "$log" >&2 || true
    rm -f -- "$log"
    return 0
  else
    rc=$?
    tail -n "$VM_UPDATE_APT_LOG_TAIL" "$log" >&2 || true
    rm -f -- "$log"
    vm_update_partial "$target package application failed"
    return "$rc"
  fi
}

vm_update_apply_config() {
  local candidate
  candidate="$(vm_update_plan_field candidate_payload_sha256)" || return 2
  VM_GUEST_PAYLOAD_SHA256="$candidate"
  VM_GUEST_UPDATE_MODE=1
  export VM_GUEST_UPDATE_MODE VM_GUEST_PAYLOAD_SHA256
  if vm_guest_build_private_payload >/dev/null; then
    vm_update_write_state "$candidate" || { vm_update_partial 'config payload installed but update-state could not be committed'; return 1; }
    return 0
  fi
  vm_update_partial 'config payload build failed; previous runtime/config were retained where possible'
  return 1
}

vm_update_replace_marker_version() {
  local version marker
  version="$1"
  marker="$(vm_update_rooted /var/lib/lab-vm/guest-ready.json)"
  "$VM_UPDATE_PYTHON" - "$marker" "$version" "$VM_UPDATE_TEST_MODE" <<'PY'
import json,os,re,stat,sys,tempfile
from pathlib import Path
path=Path(sys.argv[1]); version=sys.argv[2]; test_mode=sys.argv[3]=="1"
if not re.fullmatch(r"2\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?",version): raise SystemExit(2)
info=path.lstat()
if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink!=1 or stat.S_IMODE(info.st_mode)!=0o644 or (not test_mode and info.st_uid!=0): raise SystemExit(2)
data=json.loads(path.read_text(encoding="utf-8")); assert data.get("status")=="ready"
data["opencode_version"]=version
fd,tmp=tempfile.mkstemp(prefix=".guest-ready-update-",dir=path.parent)
try:
    os.fchmod(fd,0o644)
    with os.fdopen(fd,"w",encoding="utf-8") as stream:
        json.dump(data,stream,sort_keys=True,separators=(",",":")); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
    os.replace(tmp,path); tmp=""
    directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(directory)
    finally: os.close(directory)
finally:
    if tmp and os.path.exists(tmp): os.unlink(tmp)
PY
}

vm_update_apply_opencode() {
  local current candidate manifest stage_dir staged_manifest
  current="$(vm_update_target_field "$VM_UPDATE_PLAN_JSON" opencode current)" || return 2
  candidate="$(vm_update_target_field "$VM_UPDATE_PLAN_JSON" opencode candidate)" || return 2
  [[ "$candidate" =~ ^2\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$ ]] || { vm_update_error 'scanned OpenCode candidate is invalid.'; return 2; }
  if [[ "$(vm_update_plan_field auth_enabled)" == true && "$candidate" != 2.2.5 ]]; then
    vm_update_error 'auth-enabled profiles require a reviewed OpenCode compatibility entry; package update was not attempted.'
    return 2
  fi
  [[ "$(vm_update_current_opencode)" == "$current" ]] || { vm_update_error 'OpenCode changed after scan; re-scan before applying.'; return 2; }
  manifest="$(vm_update_rooted /var/lib/lab-vm/install-manifest.json)"
  [[ -f "$manifest" && ! -L "$manifest" ]] || { vm_update_error 'exact install manifest is missing or unsafe.'; return 2; }
  stage_dir="$(dirname "$manifest")"
  staged_manifest="$stage_dir/.install-manifest-update-${VM_UPDATE_PLAN_ID}"
  [[ ! -e "$staged_manifest" && ! -L "$staged_manifest" ]] || { vm_update_error 'update manifest staging path already exists.'; return 2; }
  VM_GUEST_VERSION_SELECTOR="$candidate"
  VM_GUEST_PAYLOAD_SHA256="$(vm_update_plan_field candidate_payload_sha256)"
  if ! vm_guest_install_opencode "$staged_manifest" >&2; then
    rm -f -- "$staged_manifest"
    vm_update_partial 'OpenCode package update failed; original manifest and ready version remain recorded'
    return 1
  fi
  mv -f -- "$staged_manifest" "$manifest" || { vm_update_partial 'OpenCode installed but exact manifest replacement failed'; return 1; }
  if ! vm_update_replace_marker_version "$candidate"; then
    vm_update_partial 'OpenCode and install manifest changed but ready marker could not be updated'
    return 1
  fi
  VM_UPDATE_APPLIED_OPENCODE="$candidate"
}

vm_update_apply() {
  local plan_id="$1" changes target status state_hash source_hash
  VM_UPDATE_PLAN_ID="$plan_id"
  vm_update_plan_load "$plan_id" || return $?
  [[ "$(vm_update_plan_field auth_enabled)" == true || "$(vm_update_plan_field auth_enabled)" == false ]] || return 2
  local blocked
  blocked="$("$VM_UPDATE_PYTHON" - "$VM_UPDATE_PLAN_PATH" <<'PY'
import json,sys
from pathlib import Path
print("1" if any(x["status"]=="BLOCKED" for x in json.loads(Path(sys.argv[1]).read_text())["targets"]) else "0")
PY
)" || return 2
  [[ "$blocked" == 0 ]] || { vm_update_error 'the scanned plan contains an unsupported auth compatibility change.'; return 2; }
  changes="$("$VM_UPDATE_PYTHON" - "$VM_UPDATE_PLAN_PATH" <<'PY'
import json,sys
from pathlib import Path
print("1" if any(x["status"]=="UPDATE" for x in json.loads(Path(sys.argv[1]).read_text())["targets"]) else "0")
PY
)" || return 2
  if [[ "$changes" == 0 ]]; then
    rm -f -- "$VM_UPDATE_PLAN_PATH"
    printf '{"status":"CURRENT","opencode_version":"%s"}\n' "$(vm_update_json_field "$VM_UPDATE_MARKER_JSON" opencode_version)"
    return 0
  fi
  vm_update_guard || return $?

  # Disruptive host-guest package changes precede stateful app/config updates.
  for target in system docker; do
    status="$(vm_update_target_field "$VM_UPDATE_PLAN_JSON" "$target" status 2>/dev/null || true)"
    if [[ "$status" == UPDATE ]]; then vm_update_apply_apt_target "$target" || return $?; fi
  done
  status="$(vm_update_target_field "$VM_UPDATE_PLAN_JSON" config status 2>/dev/null || true)"
  if [[ "$status" == UPDATE ]]; then
    state_hash="$(vm_update_state_hash)" || return 2
    [[ "$state_hash" == "$(vm_update_plan_field current_payload_sha256)" ]] || { vm_update_error 'installed config hash changed after scan; re-scan before applying.'; return 2; }
    source_hash="$(vm_update_hash_source)" || return 1
    [[ "$source_hash" == "$(vm_update_plan_field candidate_payload_sha256)" ]] || { vm_update_error 'workspace payload changed after scan; re-scan before applying.'; return 2; }
    vm_update_apply_config || return $?
  fi
  status="$(vm_update_target_field "$VM_UPDATE_PLAN_JSON" tools status 2>/dev/null || true)"
  if [[ "$status" == UPDATE ]]; then
    [[ -f "$VM_UPDATE_INSTALL_TOOLS" && ! -L "$VM_UPDATE_INSTALL_TOOLS" ]] || { vm_update_error 'selected guest tools installer is unavailable.'; return 2; }
    if ! VM_PYTHON_TOOLS_DIR="$(vm_update_rooted /opt/little-mester/python-tools)" \
      LAB_VM_PROXY=http://10.0.2.100:3128 VM_PYTHON_TOOLS_PYTHON="$VM_UPDATE_PYTHON" \
      VM_PYTHON_TOOLS_TEST_MODE="$VM_UPDATE_TEST_MODE" \
      bash "$VM_UPDATE_INSTALL_TOOLS" "$VM_UPDATE_PLAN_PATH" >&2; then
      vm_update_partial 'selected Python tool installation failed'
      return 1
    fi
  fi
  status="$(vm_update_target_field "$VM_UPDATE_PLAN_JSON" opencode status 2>/dev/null || true)"
  if [[ "$status" == UPDATE ]]; then vm_update_apply_opencode || return $?; fi

  rm -f -- "$VM_UPDATE_PLAN_PATH"
  printf '{"status":"UPDATED","opencode_version":"%s"}\n' \
    "${VM_UPDATE_APPLIED_OPENCODE:-$(vm_update_json_field "$VM_UPDATE_MARKER_JSON" opencode_version)}"
}

vm_update_cancel() {
  local plan_id="$1"
  vm_update_plan_load "$plan_id" || return $?
  rm -f -- "$VM_UPDATE_PLAN_PATH"
  printf '{"status":"CANCELLED"}\n'
}

vm_update_main() {
  local action="${1:-}" targets='' auth_enabled=0 plan_id=''
  shift || true
  while (($#)); do
    case "$1" in
      --targets|--auth-enabled|--plan)
        (($# >= 2)) || { vm_update_error 'an option is missing its value.'; return 2; }
        case "$1" in --targets) targets="$2" ;; --auth-enabled) auth_enabled="$2" ;; --plan) plan_id="$2" ;; esac
        shift 2 ;;
      *) vm_update_error 'unknown updater option.'; return 2 ;;
    esac
  done
  vm_update_load_context || return $?
  case "$action" in
    scan) vm_update_scan "$targets" "$auth_enabled" ;;
    apply) vm_update_apply "$plan_id" ;;
    cancel) vm_update_cancel "$plan_id" ;;
    *) vm_update_error 'usage: vm-update.sh scan --targets LIST [--auth-enabled 0|1] | apply|cancel --plan TOKEN'; return 2 ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  vm_update_main "$@"
fi
