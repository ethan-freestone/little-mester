#!/usr/bin/env sh
# vim: set ft=sh:
# common.sh — shared logging, colors, die helpers, repo discovery, arg parsing.
# Compatible with both POSIX sh and fish (sourced). Do NOT execute directly.

# Prevent double-sourcing.
if [ -n "${_COMMON_LOADED:-}" ]; then
    return 0 2>/dev/null || true
fi
_COMMON_LOADED=1

# ---------- Colors (disabled on non-TTY or NO_COLOR) ----------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    _RED="$(printf '\033[0;31m')"
    _GREEN="$(printf '\033[0;32m')"
    _YELLOW="$(printf '\033[0;33m')"
    _BLUE="$(printf '\033[0;34m')"
    _CYAN="$(printf '\033[0;36m')"
    _RESET="$(printf '\033[0m')"
else
    _RED=""
    _GREEN=""
    _YELLOW=""
    _BLUE=""
    _CYAN=""
    _RESET=""
fi

# ---------- Die / warn / info helpers ----------
die() {
    msg="$*"
    [ -z "$msg" ] && msg="error occurred"
    printf '%sERROR%s: %s\n' "$_RED" "$_RESET" "$msg" >&2
    exit 1
}

warn() {
    printf '%sWARN%s: %s\n' "$_YELLOW" "$_RESET" "$*" >&2
}

info() {
    printf '%sINFO%s: %s\n' "$_BLUE" "$_RESET" "$*"
}

step() {
    printf '%s%s%s\n' "${_CYAN}" "  $*" "${_RESET}"
}

ok_msg() {
    printf '  %sOK%s: %s\n' "$_GREEN" "$_RESET" "$*"
}

fail_msg() {
    printf '  %sFAIL%s: %s\n' "$_RED" "$_RESET" "$*" >&2
}

# ---------- Repo discovery ----------
# Walk up from the caller's directory to find .git/. Saves REPO_ROOT, TICKETS_DIR.
discover_repo_root() {
    dir="${1:-}"
    if [ -z "$dir" ]; then
        # Try BASH_SOURCE first (works when sourced in bash), else fall back to $0 or PWD.
        case "${BASH_SOURCE:-}" in
            "")
                case "$(pwd)" in
                    /) break ;;  # root filesystem reached
                    *) dir="$(pwd)" ;;
                esac
                ;;
            _) dir="$PWD" ;;
            *) dir="$(cd "$(dirname "${BASH_SOURCE[${1:+$1}]:-$_SCRIPT_LINE}")" && pwd)" ;;
        esac
    fi

    _REPO_ROOT=""
    check="$dir"
    while [ -n "$check" ]; do
        if [ -d "$check/.git" ]; then
            _REPO_ROOT="$check"
            break
        fi
        parent="$(dirname "$check")"
        [ "$parent" = "$check" ] && break  # reached root filesystem
        check="$parent"
    done

    if [ -z "$_REPO_ROOT" ]; then
        die "Could not find repository root (.git not found). Run inside a git repo."
    fi

    REPO_ROOT="$_REPO_ROOT"
    TICKETS_DIR="${TICKETS_DIR:-$REPO_ROOT/docs/tickets}"
}

# ---------- Arg parsing helpers ----------
# Parses --flag val, -f val, and standalone --flag / -f (boolean).
# Results stored as:
#   PAIRED_ARGS = "key=val key=val"    (space-separated pairs)
#   FLAGS_ONLY  = "flag1 flag2 ..."     (boolean flags present)

parse_args() {
    # Reset state. If associative array exists from bash, delete it.
    if [ -n "${_PAIRED_ARGS+_}" ]; then
        eval unset PAIRED_ARGS 2>/dev/null || true
    fi
    PAIRED_ARGS=""
    FLAGS_ONLY=""

    while [ $# -gt 0 ]; do
        case "$1" in
            --*)
                key="${1#--}"
                if [ $# -ge 2 ] && [ "${2:-}" != --* ] && [ -n "${2:-}" ]; then
                    PAIRED_ARGS="$PAIRED_ARGS ${key}=$2 "
                    shift 2
                else
                    FLAGS_ONLY="$FLAGS_ONLY $key "
                    shift
                fi
                ;;
            -*)
                flag="${1#-}"
                if [ $# -ge 2 ] && [ "${2:-}" != --* ] && [ -n "${2:-}" ]; then
                    PAIRED_ARGS="$PAIRED_ARGS ${flag}=$2 "
                    shift 2
                else
                    FLAGS_ONLY="$FLAGS_ONLY $flag "
                    shift
                fi
                ;;
            *)
                shift
                ;;
        esac
    done
}

# get_arg key — prints value of last --key val pair. Empty string if absent.
get_arg() {
    _ga_key="$1"
    # Walk PAIRED_ARGS and find matching key (last wins, like getopt).
    for _pair in $PAIRED_ARGS; do
        case "$_pair" in
            "${_ga_key}="*) printf '%s\n' "${_pair#*=}" ;;
        esac
    done
}

# has_flag name — returns 0 if flag present, 1 otherwise.
has_flag() {
    _hf_key="$1"
    case "$FLAGS_ONLY" in
        *" $_hf_key "*) return 0 ;;
    esac
    return 1
}
