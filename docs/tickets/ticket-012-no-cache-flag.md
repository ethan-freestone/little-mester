# TICKET-012: Add --no-cache flag to lab setup/build

**Status**: DONE
**Priority**: MEDIUM
**Completed**: 2026-10-21

## Changes

- Added `NO_CACHE` variable to lab script
- Added `--no-cache` flag parsing
- `./lab setup --no-cache` and `./lab build --no-cache` now pass `--no-cache` to `docker compose build`
- Updated help text
