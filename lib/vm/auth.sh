#!/usr/bin/env bash
# Scoped provider auth transfer and private host escrow. Sourcing is inert.

VM_AUTH_SOURCE_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
VM_AUTH_ROOT="$(cd "$(dirname "$VM_AUTH_SOURCE_FILE")/../.." && pwd)"
VM_AUTH_PYTHON="${VM_AUTH_PYTHON:-python3}"
VM_AUTH_SSH_TIMEOUT="${VM_AUTH_SSH_TIMEOUT:-120}"
VM_AUTH_ACTIVE_PROVIDERS=()

vm_auth_error() {
  printf 'REFUSED: %s\n' "$*" >&2
  return 2
}

vm_auth_version_supported() {
  [[ "${1:-}" == 2.2.5 ]] || {
    vm_auth_error 'Auth and browser adapters require the reviewed OpenCode v2.2.5 compatibility entry.'
    return 2
  }
}

vm_auth_resolve_providers() {
  local selector="${1:-off}" version="${2:-}" item
  local -a requested=()
  [[ "$selector" == off ]] && return 0
  vm_auth_version_supported "$version" || return $?
  if [[ "$selector" == auto ]]; then
    printf '%s\n' openai anthropic kimi
    return 0
  fi
  IFS=, read -r -a requested <<<"$selector"
  ((${#requested[@]} > 0)) || { vm_auth_error 'Auth provider selector is empty.'; return 2; }
  local -A seen=()
  for item in "${requested[@]}"; do
    case "$item" in
      openai|anthropic|kimi) ;;
      *) vm_auth_error 'Auth provider selector is unsupported by the reviewed adapter table.'; return 2 ;;
    esac
    [[ -z "${seen[$item]+x}" ]] || { vm_auth_error 'Auth provider selector contains a duplicate.'; return 2; }
    seen[$item]=1
    printf '%s\n' "$item"
  done
}

vm_auth_validate_export() {
  local provider="${1:-}" source="${2:-}"
  [[ "$source" == /* && -f "$source" && ! -L "$source" ]] || {
    vm_auth_error 'Credential export is not a regular private file.'
    return 2
  }
  "$VM_AUTH_PYTHON" - "$provider" "$source" <<'PY'
import json
import os
import stat
import sys

def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result

def invalid_constant(_value):
    raise ValueError

provider, filename = sys.argv[1:]
try:
    info = os.stat(filename, follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > 1024 * 1024):
        raise ValueError
    with open(filename, "rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=invalid_constant)
    if provider == "openai":
        if not isinstance(value, list):
            raise ValueError
        for entry in value:
            if not isinstance(entry, dict) or not {
                "id", "integrationID", "label", "active", "value"
            } <= entry.keys():
                raise ValueError
            if (entry["integrationID"] != "openai" or not isinstance(entry["id"], str)
                    or not entry["id"] or not isinstance(entry["label"], str)
                    or type(entry["active"]) is not bool or not isinstance(entry["value"], dict)):
                raise ValueError
            credential = entry["value"]
            kind = credential.get("type")
            if kind == "oauth":
                if not all(isinstance(credential.get(key), str) and credential[key]
                           for key in ("methodID", "access", "refresh")):
                    raise ValueError
                if type(credential.get("expires")) is not int or credential["expires"] < 0:
                    raise ValueError
            elif kind == "key":
                if not isinstance(credential.get("key"), str) or not credential["key"]:
                    raise ValueError
            elif kind == "external":
                if not isinstance(credential.get("methodID"), str) or not credential["methodID"]:
                    raise ValueError
            else:
                raise ValueError
    elif provider in ("anthropic", "kimi"):
        expected = {"schema_version", "provider", "api_key"}
        if (not isinstance(value, dict) or set(value) != expected
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["provider"] != provider or not isinstance(value["api_key"], str)
                or not value["api_key"] or len(value["api_key"].encode("utf-8")) > 16384
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value["api_key"])):
            raise ValueError
    else:
        raise ValueError
except Exception:
    raise SystemExit(2)
PY
}

vm_auth_escrow_path() {
  local directory="${1:-}" provider="${2:-}" workspace="${3:-}" repository="${4:-}"
  "$VM_AUTH_PYTHON" - "$directory" "$provider" "$workspace" "$repository" <<'PY'
import os
import stat
import sys
from pathlib import Path

directory, provider, workspace, repository = sys.argv[1:]
try:
    if provider not in {"openai", "anthropic", "kimi"}:
        raise ValueError
    raw = Path(directory)
    if not raw.is_absolute() or "\n" in directory or "\r" in directory:
        raise ValueError
    info = raw.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError
    if stat.S_IMODE(info.st_mode) & 0o077 or raw.resolve(strict=True) != raw:
        raise ValueError
    target = raw / (provider + ".json")
    resolved = target.resolve(strict=False)
    for candidate in (workspace, repository):
        if not candidate:
            continue
        base = Path(candidate).resolve(strict=False)
        if resolved == base or base in resolved.parents:
            raise ValueError
    try:
        target_info = target.lstat()
    except FileNotFoundError:
        pass
    else:
        if (not stat.S_ISREG(target_info.st_mode) or stat.S_ISLNK(target_info.st_mode)
                or target_info.st_uid != os.getuid() or target_info.st_nlink != 1
                or stat.S_IMODE(target_info.st_mode) & 0o077):
            raise ValueError
    print(target)
except Exception:
    raise SystemExit(2)
PY
}

vm_auth_ensure_escrow_dir() {
  local directory="${1:-}" workspace="${2:-}" repository="${3:-}" parent
  [[ "$directory" == /* && "$directory" != *$'\n'* && "$directory" != *$'\r'* ]] || {
    vm_auth_error 'Credential escrow directory must be an absolute private path.'
    return 2
  }
  "$VM_AUTH_PYTHON" - "$directory" "$workspace" "$repository" <<'PY' || {
import sys
from pathlib import Path
try:
    directory = Path(sys.argv[1])
    if directory.resolve(strict=False) != directory:
        raise ValueError
    for candidate in sys.argv[2:]:
        if candidate:
            base = Path(candidate).resolve(strict=False)
            if directory == base or base in directory.parents:
                raise ValueError
except Exception:
    raise SystemExit(2)
PY
    vm_auth_error 'Credential escrow path overlaps the workspace or checkout; destructive action was refused.'
    return 2
  }
  if [[ ! -e "$directory" && ! -L "$directory" ]]; then
    parent="$(dirname -- "$directory")"
    [[ -d "$parent" && ! -L "$parent" ]] || {
      vm_auth_error 'Credential escrow parent is missing or unsafe; the guest was preserved.'
      return 2
    }
    "$VM_AUTH_PYTHON" - "$parent" "$workspace" "$repository" <<'PY' || {
import os
import stat
import sys
from pathlib import Path
try:
    path = Path(sys.argv[1])
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077 or path.resolve(strict=True) != path):
        raise ValueError
    for candidate in sys.argv[2:]:
        if candidate:
            base = Path(candidate).resolve(strict=False)
            resolved = path.resolve(strict=True)
            if resolved == base or base in resolved.parents:
                raise ValueError
except Exception:
    raise SystemExit(2)
PY
      vm_auth_error 'Credential escrow parent is not private; the guest was preserved.'
      return 2
    }
    (umask 077; mkdir -- "$directory") 2>/dev/null || {
      vm_auth_error 'Credential escrow directory could not be created safely.'
      return 2
    }
  fi
  [[ -d "$directory" && ! -L "$directory" ]] || {
    vm_auth_error 'Credential escrow directory is unsafe; the guest was preserved.'
    return 2
  }
  "$VM_AUTH_PYTHON" - "$directory" <<'PY'
import os
import stat
import sys
from pathlib import Path
try:
    path = Path(sys.argv[1])
    info = path.lstat()
    assert stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
    assert info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) & 0o077 == 0
    assert path.resolve(strict=True) == path
except Exception:
    raise SystemExit(2)
PY
}

vm_auth_atomic_export() {
  local source="${1:-}" target="${2:-}" provider="${3:-}"
  "$VM_AUTH_PYTHON" - "$source" "$target" "$provider" <<'PY'
import json
import os
import stat
import tempfile
from pathlib import Path
import sys

def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result

def invalid_constant(_value):
    raise ValueError

source, target, provider = sys.argv[1:]
temporary = None
try:
    source_path = Path(source)
    target_path = Path(target)
    source_info = source_path.lstat()
    parent_info = target_path.parent.lstat()
    if (not stat.S_ISREG(source_info.st_mode) or source_info.st_uid != os.getuid()
            or source_info.st_nlink != 1 or stat.S_IMODE(source_info.st_mode) & 0o077
            or source_info.st_size > 1024 * 1024):
        raise ValueError
    if (not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
            or parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) & 0o077
            or target_path.parent.resolve(strict=True) != target_path.parent):
        raise ValueError
    try:
        target_info = target_path.lstat()
    except FileNotFoundError:
        target_info = None
    if target_info and (not stat.S_ISREG(target_info.st_mode) or stat.S_ISLNK(target_info.st_mode)
            or target_info.st_uid != os.getuid() or target_info.st_nlink != 1
            or stat.S_IMODE(target_info.st_mode) & 0o077):
        raise ValueError
    raw = source_path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=invalid_constant)
    if provider == "openai":
        if not isinstance(value, list):
            raise ValueError
        for entry in value:
            if not isinstance(entry, dict) or not {"id", "integrationID", "label", "active", "value"} <= entry.keys():
                raise ValueError
            if (entry["integrationID"] != "openai" or not isinstance(entry["id"], str)
                    or not entry["id"] or not isinstance(entry["label"], str)
                    or type(entry["active"]) is not bool or not isinstance(entry["value"], dict)):
                raise ValueError
            credential = entry["value"]
            kind = credential.get("type")
            if kind == "oauth":
                if (not all(isinstance(credential.get(key), str) and credential[key]
                            for key in ("methodID", "access", "refresh"))
                        or type(credential.get("expires")) is not int or credential["expires"] < 0):
                    raise ValueError
            elif kind == "key":
                if not isinstance(credential.get("key"), str) or not credential["key"]:
                    raise ValueError
            elif kind == "external":
                if not isinstance(credential.get("methodID"), str) or not credential["methodID"]:
                    raise ValueError
            else:
                raise ValueError
    elif provider in ("anthropic", "kimi"):
        if (not isinstance(value, dict) or set(value) != {"schema_version", "provider", "api_key"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["provider"] != provider or not isinstance(value["api_key"], str)
                or not value["api_key"] or len(value["api_key"].encode("utf-8")) > 16384
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value["api_key"])):
            raise ValueError
    else:
        raise ValueError
    fd, temporary = tempfile.mkstemp(prefix=f".{provider}-auth.", dir=target_path.parent)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target_path)
    temporary = None
    directory_fd = os.open(target_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
except Exception:
    if temporary:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    raise SystemExit(2)
PY
}

vm_auth_export_stages_remove() {
  local path
  for path in "$@"; do
    [[ -n "$path" ]] && rm -f -- "$path"
  done
}

vm_auth_marker_path() {
  local root="${1:-}" name="${2:-}" provider="${3:-}" profile marker
  [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$ && "$provider" =~ ^(openai|anthropic|kimi)$ ]] || return 2
  profile="$root/vm/profiles/$name"
  [[ -d "$profile" && ! -L "$profile" ]] || return 2
  marker="$profile/auth-imported-$provider"
  if [[ -e "$marker" || -L "$marker" ]]; then
    "$VM_AUTH_PYTHON" - "$marker" "$provider" <<'PY' || return 2
import os
import stat
import sys
from pathlib import Path
try:
    path = Path(sys.argv[1])
    info = path.lstat()
    expected = f"v1:{sys.argv[2]}:2.2.5\n".encode()
    assert stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
    assert info.st_uid == os.getuid() and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600
    assert path.read_bytes() == expected
except Exception:
    raise SystemExit(2)
PY
    return $?
  fi
  return 1
}

vm_auth_write_marker() {
  local root="$1" name="$2" provider="$3" marker temporary profile
  profile="$root/vm/profiles/$name"
  marker="$profile/auth-imported-$provider"
  [[ -d "$profile" && ! -L "$profile" && ! -e "$marker" && ! -L "$marker" ]] || {
    vm_auth_error 'Auth import marker path is unsafe; guest credentials were preserved.'
    return 2
  }
  temporary="$(mktemp "$profile/.auth-marker.XXXXXX")" || return 2
  (umask 077; printf 'v1:%s:2.2.5\n' "$provider" >"$temporary") || {
    rm -f -- "$temporary"
    return 2
  }
  chmod 600 "$temporary" && ln -- "$temporary" "$marker" || {
    rm -f -- "$temporary"
    vm_auth_error 'Could not atomically record the successful auth import.'
    return 2
  }
  rm -f -- "$temporary"
}

vm_auth_guest_command() {
  local root="$1" name="$2" data="$3" remote_command="$4"
  local info registry run_id role port run_dir key known_hosts
  info="$(vm_lifecycle_registry_for_profile "$root" "$name" "$data")" || return 2
  read -r registry run_id role < <(printf '%s' "$info" | "$VM_AUTH_PYTHON" -c '
import json,sys
try:
 item=json.load(sys.stdin)
 print(item["registry"],item["run_id"],item["role"])
except Exception: raise SystemExit(2)
') || return 2
  port="$(printf '%s' "$data" | "$VM_AUTH_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["runtime_identity"]["ssh_port"])')" || return 2
  run_dir="$(dirname -- "$registry")"
  key="$root/vm/profiles/$name/control-key"
  known_hosts="$run_dir/known_hosts"
  if ! declare -F vm_ssh_noninteractive >/dev/null; then
    # shellcheck disable=SC1091 # Loaded only by an explicit auth operation.
    source "$VM_AUTH_ROOT/lib/vm/ssh.sh"
  fi
  vm_ssh_listener_verified "$registry" "$run_id" "$role" "$port" || {
    vm_auth_error 'Selected guest SSH listener is not verified; auth transfer was refused.'
    return 2
  }
  vm_ssh_noninteractive "$key" "$known_hosts" "$port" "$VM_AUTH_SSH_TIMEOUT" \
    mester@127.0.0.1 "$remote_command"
}

vm_auth_guest_export() {
  local root="$1" name="$2" data="$3" provider="$4" command code quoted
  case "$provider" in
    openai) command='opencode auth export openai' ;;
    anthropic|kimi)
      code='import json,os,stat,sys
from pathlib import Path
provider=sys.argv[1]
path=Path.home()/".config/opencode/little-mester-auth"/(provider+".json")
try:
 info=path.lstat()
 assert path.resolve(strict=True)==path
 assert stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
 assert info.st_uid==os.getuid() and info.st_nlink==1 and stat.S_IMODE(info.st_mode)==0o600
 assert info.st_size<=20000
 raw=path.read_bytes()
 data=json.loads(raw.decode("utf-8"))
 assert set(data)=={"schema_version","provider","api_key"}
 assert type(data["schema_version"]) is int and data["schema_version"]==1
 assert data["provider"]==provider and isinstance(data["api_key"],str) and data["api_key"]
 assert len(data["api_key"].encode())<=16384 and not any(ord(c)<32 or 127<=ord(c)<=159 for c in data["api_key"])
 sys.stdout.buffer.write(raw)
except Exception:
 raise SystemExit(2)'
      printf -v quoted '%q' "$code"
      printf -v command 'python3 -c %s %q' "$quoted" "$provider"
      ;;
    *) vm_auth_error 'Unsupported auth export adapter.'; return 2 ;;
  esac
  vm_auth_guest_command "$root" "$name" "$data" "$command"
}

vm_auth_key_install_command() {
  local provider="$1" code quoted
  case "$provider" in
    anthropic|kimi) ;;
    *) vm_auth_error 'Unsupported runtime key adapter.'; return 2 ;;
  esac
  code='import json,os,stat,sys,tempfile
from pathlib import Path
provider=sys.argv[1]
try:
 data=json.load(sys.stdin)
 assert set(data)=={"schema_version","provider","api_key"}
 assert type(data["schema_version"]) is int and data["schema_version"]==1
 assert data["provider"]==provider and isinstance(data["api_key"],str) and data["api_key"]
 assert len(data["api_key"].encode())<=16384 and not any(ord(c)<32 or 127<=ord(c)<=159 for c in data["api_key"])
 directory=Path.home()/".config/opencode/little-mester-auth"
 directory.mkdir(mode=0o700,parents=True,exist_ok=True)
 dinfo=directory.lstat()
 assert stat.S_ISDIR(dinfo.st_mode) and not stat.S_ISLNK(dinfo.st_mode) and dinfo.st_uid==os.getuid()
 assert directory.resolve(strict=True)==directory
 assert stat.S_IMODE(dinfo.st_mode)==0o700
 target=directory/(provider+".json")
 try:
  old=target.lstat()
  assert stat.S_ISREG(old.st_mode) and not stat.S_ISLNK(old.st_mode) and old.st_uid==os.getuid() and old.st_nlink==1 and stat.S_IMODE(old.st_mode)==0o600
 except FileNotFoundError: pass
 fd,temp=tempfile.mkstemp(prefix="."+provider+".",dir=directory)
 try:
  os.fchmod(fd,0o600)
  with os.fdopen(fd,"w",encoding="utf-8") as stream:
    json.dump(data,stream,separators=(",",":")); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
  os.replace(temp,target)
  dfd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
  try: os.fsync(dfd)
  finally: os.close(dfd)
 except Exception:
  try: os.unlink(temp)
  except OSError: pass
  raise
except Exception:
 raise SystemExit(2)'
  printf -v quoted '%q' "$code"
  printf 'python3 -c %s %q' "$quoted" "$provider"
}

vm_auth_import_if_needed() {
  local root="$1" name="$2" data="$3" selector="$4" version="$5"
  local escrow_dir="$6" workspace="$7" repository="$8" providers provider path marker command index
  local -a import_providers=() import_paths=()
  VM_AUTH_ACTIVE_PROVIDERS=()
  [[ "$selector" == off ]] && return 0
  providers="$(vm_auth_resolve_providers "$selector" "$version")" || return $?
  while IFS= read -r provider; do
    [[ -n "$provider" ]] || continue
    marker="$(vm_auth_marker_path "$root" "$name" "$provider")"
    case "$?" in
      0)
        VM_AUTH_ACTIVE_PROVIDERS+=("$provider")
        continue
        ;;
      1) ;;
      *) vm_auth_error 'Existing auth-import marker is unsafe; guest credentials were not overwritten.'; return 2 ;;
    esac
    if [[ ! -d "$escrow_dir" || -L "$escrow_dir" ]]; then
      [[ "$selector" == auto ]] && continue
      vm_auth_error "Selected $provider escrow is unavailable; no guest credential was changed."
      return 2
    fi
    path="$(vm_auth_escrow_path "$escrow_dir" "$provider" "$workspace" "$repository")" || {
      vm_auth_error 'Credential escrow path is unsafe; no guest credential was changed.'
      return 2
    }
    if [[ ! -f "$path" ]]; then
      [[ "$selector" == auto ]] && continue
      vm_auth_error "Selected $provider escrow file is missing; no guest credential was changed."
      return 2
    fi
    vm_auth_validate_export "$provider" "$path" || {
      vm_auth_error "Selected $provider escrow file is invalid; no guest credential was changed."
      return 2
    }
    import_providers+=("$provider")
    import_paths+=("$path")
  done <<<"$providers"
  for index in "${!import_providers[@]}"; do
    provider="${import_providers[$index]}"
    path="${import_paths[$index]}"
    vm_auth_validate_export "$provider" "$path" || {
      vm_auth_error "Selected $provider escrow file changed during validation; no further guest credential was changed."
      return 2
    }
    if [[ "$provider" == openai ]]; then
      command='opencode auth import'
    else
      command="$(vm_auth_key_install_command "$provider")" || return $?
    fi
    if ! vm_auth_guest_command "$root" "$name" "$data" "$command" \
      <"$path" >/dev/null 2>&1; then
      vm_auth_error "Selected $provider credential transfer failed; the guest state was preserved."
      return 1
    fi
    vm_auth_write_marker "$root" "$name" "$provider" || return $?
    VM_AUTH_ACTIVE_PROVIDERS+=("$provider")
  done
}

vm_auth_export_before_delete() {
  local root="$1" name="$2" data="$3" selector="$4" version="$5"
  local escrow_dir="$6" workspace="$7" repository="$8"
  local providers provider status identity_state path temporary marker marker_rc index
  local -a staged_paths=() staged_targets=() staged_providers=()
  [[ "$selector" == off ]] && return 0
  providers="$(vm_auth_resolve_providers "$selector" "$version")" || return $?
  status="$(printf '%s' "$data" | "$VM_AUTH_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["status"])')" || return 2
  [[ "$status" == ready ]] || {
    vm_auth_error 'Auth export requires the guest to be running; resume it and retry. The profile overlay was preserved.'
    return 2
  }
  declare -F vm_lifecycle_identity_state >/dev/null || {
    vm_auth_error 'Guest identity cannot be verified for auth export; the profile overlay was preserved.'
    return 2
  }
  identity_state="$(vm_lifecycle_identity_state "$data" "$root" "$name")" || identity_state=unknown
  [[ "$identity_state" == running ]] || {
    vm_auth_error 'Guest is not running as the verified profile; auth export was refused and the overlay was preserved.'
    return 2
  }
  vm_auth_ensure_escrow_dir "$escrow_dir" "$workspace" "$repository" || return $?
  while IFS= read -r provider; do
    [[ -n "$provider" ]] || continue
    path="$(vm_auth_escrow_path "$escrow_dir" "$provider" "$workspace" "$repository")" || {
      vm_auth_export_stages_remove "${staged_paths[@]}"
      vm_auth_error 'Credential escrow path is unsafe; destructive lifecycle action was refused.'
      return 2
    }
    if [[ "$selector" == auto && "$provider" != openai ]]; then
      marker="$(vm_auth_marker_path "$root" "$name" "$provider")"
      marker_rc=$?
      (( marker_rc <= 1 )) || {
        vm_auth_export_stages_remove "${staged_paths[@]}"
        vm_auth_error 'Existing auth-import marker is unsafe; destructive lifecycle action was refused.'
        return 2
      }
      (( marker_rc == 0 )) || [[ -e "$path" || -L "$path" ]] || continue
    fi
    temporary="$(mktemp "$escrow_dir/.${provider}-export.XXXXXX")" || {
      vm_auth_export_stages_remove "${staged_paths[@]}"
      vm_auth_error 'Private credential export staging could not be created; the guest was preserved.'
      return 1
    }
    staged_paths+=("$temporary")
    staged_targets+=("$path")
    staged_providers+=("$provider")
    chmod 600 "$temporary" || {
      vm_auth_export_stages_remove "${staged_paths[@]}"
      vm_auth_error 'Private credential export staging failed; the guest was preserved.'
      return 1
    }
    if ! vm_auth_guest_export "$root" "$name" "$data" "$provider" \
      >"$temporary" 2>/dev/null; then
      vm_auth_export_stages_remove "${staged_paths[@]}"
      vm_auth_error "Selected $provider export failed; destructive lifecycle action was refused and the guest was preserved."
      return 1
    fi
    if ! vm_auth_validate_export "$provider" "$temporary"; then
      vm_auth_export_stages_remove "${staged_paths[@]}"
      vm_auth_error "Selected $provider export was invalid; destructive lifecycle action was refused and the guest was preserved."
      return 1
    fi
  done <<<"$providers"
  for index in "${!staged_paths[@]}"; do
    if ! vm_auth_atomic_export "${staged_paths[$index]}" \
      "${staged_targets[$index]}" "${staged_providers[$index]}"; then
      vm_auth_export_stages_remove "${staged_paths[@]}"
      vm_auth_error 'Staged auth export could not be atomically committed; destructive action was refused and the guest was preserved.'
      return 1
    fi
  done
  vm_auth_export_stages_remove "${staged_paths[@]}"
  printf 'Selected auth credentials were saved to private escrow; credential contents were not logged.\n'
}
