#!/usr/bin/env bash
# Bounded host-only apt setup shared by the independent hosted KVM jobs.
set -Eeuo pipefail
umask 077

phase="${1:-}"
case "$phase" in
  update)
    timeout_seconds=180
    apt_command=(update)
    ;;
  install)
    timeout_seconds=480
    repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    mapfile -t vm_packages < <(/usr/bin/python3 -I "$repo_root/lib/vm/host-dependencies.py" ci --list-packages apt)
    ((${#vm_packages[@]} > 0)) || {
      printf 'HOST_APT_SETUP_FAIL phase=install exit_status=2 reason=empty_shared_dependency_manifest\n' >&2
      exit 2
    }
    apt_command=(install -y --no-install-recommends
      --no-upgrade --no-remove "${vm_packages[@]}")
    ;;
  *)
    printf 'HOST_APT_SETUP_FAIL phase=invalid exit_status=2\n' >&2
    exit 2
    ;;
esac

runner_temp="${RUNNER_TEMP:-}"
[[ "$runner_temp" == /* && -d "$runner_temp" ]] || {
  printf 'HOST_APT_SETUP_FAIL phase=%s exit_status=2 reason=invalid_RUNNER_TEMP\n' "$phase" >&2
  exit 2
}
log="$(mktemp "$runner_temp/little-mester-kvm-apt-$phase.XXXXXX")"
trap 'rm -f -- "$log"' EXIT

apt_options=(
  -o Acquire::Retries=1
  -o Acquire::http::Timeout=30
  -o Acquire::https::Timeout=30
  -o DPkg::Lock::Timeout=30
)
started=$SECONDS
printf 'HOST_APT_SETUP_START phase=%s timeout_seconds=%s retries=1 lock_wait_seconds=30\n' \
  "$phase" "$timeout_seconds"

set +e
# apt runs elevated, but the private log is intentionally created/written by the runner user.
# shellcheck disable=SC2024
sudo -n /usr/bin/timeout --signal=TERM --kill-after=10s "${timeout_seconds}s" \
  apt-get "${apt_options[@]}" "${apt_command[@]}" >"$log" 2>&1
exit_status=$?
set -e
duration_seconds=$((SECONDS - started))

if (( exit_status == 0 )); then
  printf 'HOST_APT_SETUP_PASS phase=%s duration_seconds=%s exit_status=0\n' \
    "$phase" "$duration_seconds"
  exit 0
fi

if grep -Eqi 'Could not get lock|Unable to acquire.*lock|lock-frontend' "$log"; then
  classification=package-manager-lock
elif (( exit_status == 124 || exit_status == 137 )); then
  classification=timeout
elif grep -Eqi 'Could not resolve|Temporary failure resolving|Failed to fetch|Connection timed out|Network is unreachable|Connection failed' "$log"; then
  classification=network-acquisition
else
  classification=apt-failure
fi

printf 'HOST_APT_SETUP_FAIL phase=%s duration_seconds=%s timeout_seconds=%s exit_status=%s classification=%s\n' \
  "$phase" "$duration_seconds" "$timeout_seconds" "$exit_status" "$classification" >&2
python3 - "$log" >&2 <<'PY'
import collections
import re
import sys

# Reserve one byte for the framing newline emitted after this process.
begin = b"HOST_APT_SETUP_DIAGNOSTICS_BEGIN\n"
end = b"\nHOST_APT_SETUP_DIAGNOSTICS_END\n"
limit = 8192 - len(begin) - len(end)
tail = collections.deque()
size = 0
url_credentials = re.compile(r"(https?://)[^/@\s]*@", re.IGNORECASE)
authorization = re.compile(r"(?i)(\bauthorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+(?:\s+[^\s,;]+)?")
secret_field = re.compile(
    r"(?i)(\b(?:password|passwd|token|secret|api[_-]?key)\b\s*[=:]\s*)[^\s,;]+"
)
bearer = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")

with open(sys.argv[1], "rb") as source:
    for raw_line in source:
        line = raw_line.decode("utf-8", "replace")
        line = url_credentials.sub(r"\1[REDACTED]@", line)
        line = authorization.sub(r"\1[REDACTED]", line)
        line = secret_field.sub(r"\1[REDACTED]", line)
        line = bearer.sub(r"\1[REDACTED]", line)
        safe = line.encode("ascii", "replace")
        safe = bytes(byte for byte in safe if byte in (9, 10, 13) or 32 <= byte <= 126)
        tail.append(safe)
        size += len(safe)
        while size > limit and tail:
            excess = size - limit
            first = tail[0]
            if len(first) <= excess:
                size -= len(tail.popleft())
            else:
                tail[0] = first[excess:]
                size -= excess
sys.stdout.buffer.write(begin + b"".join(tail) + end)
PY
exit "$exit_status"
