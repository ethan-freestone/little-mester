#!/bin/sh
# Bounded OpenCode CLI readiness check with diagnostics that never emit output.
set -u

opencode_path=/usr/local/bin/opencode
if [ ! -x "$opencode_path" ]; then
  printf '%s\n' 'KVM_M2_READINESS_FAIL check=opencode-executable path=/usr/local/bin/opencode'
  exit 44
fi

diagnostic_dir=$(mktemp -d /tmp/lm-opencode-check.XXXXXX) || {
  printf '%s\n' 'KVM_M2_READINESS_FAIL check=opencode-diagnostics-temp'
  exit 44
}
cleanup_diagnostics() {
  rm -f -- "$diagnostic_dir/ambient.out" "$diagnostic_dir/ambient.err" \
    "$diagnostic_dir/protected.out" "$diagnostic_dir/protected.err" \
    "$diagnostic_dir/dependencies"
  rmdir -- "$diagnostic_dir"
}
run_version() {
  output_file=$1
  error_file=$2
  shift 2
  (ulimit -f 64; timeout --signal=TERM --kill-after=1 3 "$@" "$opencode_path" --version \
    >"$output_file" 2>"$error_file")
}
version_is_pinned() {
  [ "$(cat "$1")" = 'opencode v2.2.5' ]
}
failure_class() {
  result=$1
  error_file=$2
  output_file=$3
  if [ "$result" -eq 0 ]; then
    if version_is_pinned "$output_file"; then printf version-pass; else printf version-mismatch; fi
    return
  fi
  case "$result" in
    124) printf timeout ;;
    137|143) printf killed-or-signalled ;;
    *)
      if grep -Eqi 'error while loading shared libraries|cannot open shared object' "$error_file"; then
        printf shared-library-failure
      elif grep -Eqi 'No such file or directory|bad interpreter' "$error_file"; then
        printf missing-interpreter-or-runtime
      elif grep -Eqi 'Exec format error|cannot execute binary file|invalid ELF' "$error_file"; then
        printf binary-format-or-architecture
      elif grep -Eqi 'Permission denied' "$error_file"; then
        printf permission-failure
      elif [ "$result" -eq 126 ]; then
        printf execution-refused
      elif [ "$result" -eq 127 ]; then
        printf command-or-interpreter-not-found
      else
        printf process-exit
      fi
      ;;
  esac
}

if run_version "$diagnostic_dir/ambient.out" "$diagnostic_dir/ambient.err"; then
  ambient_status=0
else
  ambient_status=$?
fi
if [ "$ambient_status" -eq 0 ] && version_is_pinned "$diagnostic_dir/ambient.out"; then
  cleanup_diagnostics
  exit 0
fi

# Match the environment used by the protected OpenCode runner. This retry
# distinguishes an SSH-shell environment mismatch from an installation defect.
if timeout --signal=TERM --kill-after=1 3 /usr/bin/python3 -I \
    /usr/local/libexec/lm-guest-user-dirs.py prepare-auth >/dev/null 2>&1; then
  :
else
  prepare_auth_status=$?
  printf 'KVM_M2_READINESS_FAIL check=opencode-prepare-auth exit=%s\n' "$prepare_auth_status"
  cleanup_diagnostics
  exit 44
fi
if run_version "$diagnostic_dir/protected.out" "$diagnostic_dir/protected.err" \
    /usr/bin/env -i HOME=/home/mester PATH=/usr/local/bin:/usr/bin:/bin \
    TERM=xterm-256color LANG=C.UTF-8 TMUX_TMPDIR=/home/mester/.local/share/opencode/tmux \
    XDG_DATA_HOME=/home/mester/.local/share XDG_CONFIG_HOME=/etc \
    OPENCODE_CONFIG=/etc/opencode/opencode.json OPENCODE_DISABLE_PROJECT_CONFIG=1 \
    HTTP_PROXY=http://10.0.2.100:3128 HTTPS_PROXY=http://10.0.2.100:3128 \
    http_proxy=http://10.0.2.100:3128 https_proxy=http://10.0.2.100:3128 \
    NO_PROXY=localhost,127.0.0.1,10.0.2.100 no_proxy=localhost,127.0.0.1,10.0.2.100 \
    OPENCODE_CLI_CONFIG_CONTENT='{"plugins":["/etc/opencode/plugins/little-mester-exit-confirm"],"keybinds":{"app.exit":"none","prompt.clear":"none"}}'; then
  protected_status=0
else
  protected_status=$?
fi
if [ "$protected_status" -eq 0 ] && version_is_pinned "$diagnostic_dir/protected.out"; then
  printf 'KVM_M2_READINESS_ENV_DIAG ambient_exit=%s ambient_class=%s protected_env=pass\n' \
    "$ambient_status" \
    "$(failure_class "$ambient_status" "$diagnostic_dir/ambient.err" "$diagnostic_dir/ambient.out")"
  cleanup_diagnostics
  exit 0
fi

if [ "$protected_status" -eq 0 ]; then
  failure_check=opencode-version
  protected_class=version-mismatch
else
  failure_check=opencode-version-command
  protected_class=$(failure_class "$protected_status" "$diagnostic_dir/protected.err" \
    "$diagnostic_dir/protected.out")
fi
ambient_class=$(failure_class "$ambient_status" "$diagnostic_dir/ambient.err" \
  "$diagnostic_dir/ambient.out")
target=$(readlink -f "$opencode_path" 2>/dev/null || printf unresolved)
target_safe=$(printf '%s' "$target" | tr -cd '[:alnum:]/_.+-' | cut -c1-160)
binary_format=unavailable
if command -v file >/dev/null 2>&1; then
  binary_format=$(file -Lb "$target" 2>/dev/null | tr -cd '[:alnum:] ./()+_:-' | cut -c1-100)
  [ -n "$binary_format" ] || binary_format=unavailable
fi
binary_stat=$(stat -Lc '%a:%u:%g:%s' "$opencode_path" 2>/dev/null || printf unavailable)
home_stat=$(stat -Lc '%a:%u:%g' /home/mester 2>/dev/null || printf unavailable)
home_value=$(printf '%s' "${HOME:-unset}" | tr -cd '[:alnum:]/_.+-' | cut -c1-120)
[ -n "$home_value" ] || home_value='unset'
package_version=$(/usr/bin/python3 -c \
  'import json,pathlib,sys; p=pathlib.Path(sys.argv[1]).resolve().parent.parent / "package.json"; print(json.loads(p.read_text()).get("version", "unknown"))' \
  "$opencode_path" 2>/dev/null | tr -cd '[:alnum:].+-' | cut -c1-40)
[ -n "$package_version" ] || package_version=unknown
runtime_dependencies=unavailable
if command -v ldd >/dev/null 2>&1 \
    && timeout --signal=TERM --kill-after=1 2 ldd "$target" >"$diagnostic_dir/dependencies" 2>&1; then
  if grep -q 'not found' "$diagnostic_dir/dependencies"; then
    runtime_dependencies=missing
  else
    runtime_dependencies=resolved
  fi
elif [ -s "$diagnostic_dir/dependencies" ] \
    && grep -qi 'not a dynamic executable' "$diagnostic_dir/dependencies"; then
  runtime_dependencies=static-or-nondynamic
elif command -v ldd >/dev/null 2>&1; then
  runtime_dependencies=check-failed
fi
printf 'KVM_M2_READINESS_FAIL check=%s expected=opencode-v2.2.5 ambient_exit=%s ambient_class=%s protected_exit=%s protected_class=%s uid=%s home=%s home_mode_uid_gid=%s arch=%s binary_target=%s binary_format=%s binary_mode_uid_gid_size=%s package_version=%s runtime_dependencies=%s\n' \
  "$failure_check" "$ambient_status" "$ambient_class" "$protected_status" "$protected_class" \
  "$(id -u)" "$home_value" "$home_stat" "$(uname -m 2>/dev/null || printf unknown)" "$target_safe" \
  "$binary_format" "$binary_stat" "$package_version" "$runtime_dependencies"
cleanup_diagnostics
exit 44
