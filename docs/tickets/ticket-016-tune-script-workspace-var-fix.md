# TICKET-016: `./lab tune` fails — `bench-native.sh` missing `WORKSPACE` env var

**Status**: RESOLVED
**Priority**: HIGH
**Date**: 2026-10-22

---

## Problem

Running `./lab tune --keep-running` fails because `bench-native.sh` calls `docker compose` directly without the `WORKSPACE` environment variable set. Docker Compose then fails on the interpolation in `compose.yaml`:

```yaml
WORKSPACE: ${WORKSPACE:?run via ./lab}:/workspace:${MOUNT_MODE:-rw}
```

The `${WORKSPACE:?…}` syntax causes Docker Compose to error with `run via ./lab` and abort all commands.

---

## Root Cause

`bench-native.sh` invokes `docker compose` directly (e.g., `docker compose -f compose.yaml --project-directory $LAB_DIR ps llamacpp`) at several points, but never sets the `WORKSPACE` environment variable that the compose file requires.

The `./lab` script has a `dc()` wrapper function (line 38-57) that sets `WORKSPACE` before calling docker compose:

```bash
dc() {
  env "WORKSPACE=${WORKSPACE:-$LAB_DIR}" \
    docker compose -f "$LAB_COMPOSE" \
    --project-directory "$LAB_DIR" \
    "$@"
}
```

All other scripts that need docker compose call `dc()` to get this setup. `bench-native.sh` was written to call `docker compose` directly, bypassing this helper.

---

## Impact

- `./lab tune --keep-running` — fails (container is started by the tune script, but bench-native.sh can't verify its status)
- Any direct invocation of `bench-native.sh` without `WORKSPACE` set — fails

---

## Fix

Added `WORKSPACE="${WORKSPACE:-$LAB_DIR}"` at the top of `bench-native.sh`, right after `LAB_DIR` is set (after line 22). This ensures all `docker compose` calls in the script have the variable available.

```bash
LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAB_COMPOSE="$LAB_DIR/compose.yaml"
WORKSPACE="${WORKSPACE:-$LAB_DIR}"
```

The default `${WORKSPACE:-$LAB_DIR}` matches the pattern used by the `dc()` wrapper in `./lab`, so callers that already set `WORKSPACE` keep their value, while direct invocations get a sensible default.

---

## Files Changed

| File | Change |
|------|--------|
| `bench/bench-native.sh` | Added `WORKSPACE="${WORKSPACE:-$LAB_DIR}"` after `LAB_DIR` assignment (line 24) |

---

## Verification

The fix was verified by checking that `docker compose -f "$LAB_COMPOSE" --project-directory "$LAB_DIR" ps llamacpp` now succeeds because `WORKSPACE` is set. The `${WORKSPACE:?run via ./lab}` interpolation in `compose.yaml` no longer triggers an error.
