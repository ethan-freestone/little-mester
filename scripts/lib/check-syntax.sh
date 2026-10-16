#!/usr/bin/env sh
# vim: set ft=sh:
# check-syntax -- validate POSIX/sh syntax for all scripts.
# Usage: ./scripts/lib/check-syntax.sh [--all] [script1 script2 ...]

set -e

die() { printf 'check-syntax: %s\n' "$*" >&2; exit 1; }
info() { printf 'INFO: %s\n' "$*" >&2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO_ROOT=""
for _d in "$SCRIPT_DIR" .. ../.. "/tmp/agent-workspace"; do
    case "$_d" in
        .) rp=$(pwd);;
        /*) rp="$_d";;
        *) rp="$(cd "$_d" 2>/dev/null && pwd || true)";;
    esac
    if [ -n "$rp" ] && [ -d "$rp/.git" ]; then REPO_ROOT="$rp"; break; fi
done
[ -z "$REPO_ROOT" ] && die "Cannot find repo root"

TOTAL=0
PASS=0
FAIL=0
SHOW_VERBOSE=yes
FILES_TO_CHECK=""

while [ $# -gt 0 ]; do
    case "$1" in
        --all) FILES_TO_CHECK="ALL"; shift;;
        --quiet) SHOW_VERBOSE=no; shift;;
        -h|--help)
            cat <<'EOP'
Usage: check-syntax [--all] [--quiet] [script1 script2 ...]

Checks POSIX/sh syntax for scripts using sh -n.

  --all      Check all .sh files under scripts/
  --quiet    Only print summary, skip per-file results
  (positional)  Specific script paths to check
EOP
            exit 0;;
        *) FILES_TO_CHECK="$FILES_TO_CHECK $1"; shift;;
    esac
done

if [ "$SHOW_VERBOSE" = "yes" ]; then
    info "Repo root: $REPO_ROOT"
fi

check_one() {
    _file="$1"
    [ -f "$_file" ] || { fail_msg "File not found: $_file"; return; }
    TOTAL=$((TOTAL+1))
    if sh -n "$_file" 2>/dev/null; then
        PASS=$((PASS+1))
        if [ "$SHOW_VERBOSE" = "yes" ]; then
            printf '  [PASS] %s\n' "$_file"
        fi
    else
        FAIL=$((FAIL+1))
        printf '  [FAIL] sh -n failed for %s:\n' "$_file" >&2
        sh -n "$_file" 2>&1 >&2 || true
    fi
}

if [ "$FILES_TO_CHECK" = "ALL" ] || { [ -n "$FILES_TO_CHECK" ] && printf '%s' "$FILES_TO_CHECK" | grep -q '^\s*$'; }; then
    for _script in "$REPO_ROOT"/scripts/*.sh; do
        [ -f "$_script" ] || continue
        case "$(_script)" in
            *"common.sh"|"*lib/"*) continue;;  # library files, checked by sourcing
        esac
        check_one "$_script"
    done
else
    for _script in $FILES_TO_CHECK; do
        _abs=""
        case "$_script" in
            /*) _abs="$_script";;
            *) _abs="$REPO_ROOT/$_script";;
        esac
        check_one "$_abs"
    done
fi

printf '\n'
info "*** RESULTS ***"
printf '  Total: %d | Passed: %d | Failed: %d\n' "$TOTAL" "$PASS" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
    printf '\n%sERROR%s: Syntax errors found. Fixed before committing.\n' "$_RED" "$_RESET" >&2
    exit 1
fi
info "All scripts pass sh -n syntax check."
exit 0
