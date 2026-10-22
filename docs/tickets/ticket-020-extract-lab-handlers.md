# Ticket 020 -- Extract remaining lab handlers into subscripts

**Status**: ACTIVE
**Priority**: HIGH
**Type**: improvement
**Area**: infra
**Labels**: `scope/infra`, `type/improvement`
**Date**: 2026-10-22
**Parent**: TICKET-019 (lab too big — tech debt)

---

## Objective

Implement the delegation pattern from DECISION-012 for all remaining `./lab` subcommand handlers that exceed 50 lines, bringing the main `./lab` file under 400 lines.

---

## Scope & Boundaries

### In scope
- Extract `bench` handler into `bench/bench-runner.sh`
- Extract `restart-llama` handler into `scripts/restart-llama.sh`
- Update `./lab` to use `exec` delegation for both
- Forward `VERBOSE` and subcommand-specific env vars
- Update help text in `./lab`

### Do not modify
- `compose.yaml` service definitions or networks
- `machine.conf`, `.little-mester.conf`, `models.txt`
- Agent container images or Dockerfiles
- Existing scripts: `bench-native.sh`, `tune.sh`, `ai-analyse.sh`
- `dc()` wrapper function in `./lab`
- `confirm()`, `check_workspace()`, `check_egress_permission()` — these are shared helpers

### File
- `lab` — remove inline handlers
- `bench/bench-runner.sh` — new
- `scripts/restart-llama.sh` — new
- `docs/tickets/ticket-020-extract-lab-handlers.md` — this ticket

---

## Steps

### Step 1: Extract `bench` handler

Create `bench/bench-runner.sh` that contains:
- The `bench` subcommand's flag parsing (`--native`, `--json`, `-n`, `-t`, `-ub`, `--moe`, `--ctx`, `--help`)
- The `--help` help text
- HTTP mode logic: build bench image, wait for llamacpp health, run bench
- Native mode logic: build bench image with GPU profile, set env vars, run bench

Read `LAB_DIR`, `VERBOSE`, `NO_CACHE` from env (set by `./lab` delegation).
Read `BENCH_JSON`, `BENCH_N`, `NATIVE_THREADS`, `NATIVE_UB`, `NATIVE_MOE`, `NATIVE_CTX` from env.
Use `dc()` from the existing compose setup.

Update `./lab`:
```bash
bench)
  shift
  exec env VERBOSE="$VERBOSE" BENCH_JSON="$BENCH_JSON" BENCH_N="$BENCH_N" \
    NATIVE_THREADS="$NATIVE_THREADS" NATIVE_UB="$NATIVE_UB" \
    NATIVE_MOE="$NATIVE_MOE" NATIVE_CTX="$NATIVE_CTX" NO_CACHE="$NO_CACHE" \
    "$LAB_DIR/bench/bench-runner.sh" "$@"
  ;;
```

### Step 2: Extract `restart-llama` handler

Create `scripts/restart-llama.sh` that contains:
- The `restart-llama` flag parsing (`--cache`, `--help`)
- The `--help` help text
- The `--cache` logic: check for existing `--cache` in compose.yaml, sed-inject if absent
- `dc restart llamacpp || dc up -d llamacpp`

Update `./lab`:
```bash
restart-llama)
  shift
  exec env VERBOSE="$VERBOSE" LAB_DIR="$LAB_DIR" dc() { ... } "$LAB_DIR/scripts/restart-llama.sh" "$@"
  ;;
```

Wait — `restart-llama` uses `dc` which is defined inside `./lab`. The script can't call `dc()` directly. Options:
a) Have the script call `docker compose` directly with the right env (like `bench-native.sh`)
b) Have `./lab` export a `LAB_COMPOSE` path and the script calls docker compose with `--project-directory`
c) Source a helper from `./lab` (messy)

Go with option (b): `./lab` sets `LAB_COMPOSE` and the script calls docker compose directly:
```bash
restart-llama)
  shift
  exec env VERBOSE="$VERBOSE" LAB_COMPOSE="$LAB_DIR/compose.yaml" LAB_DIR="$LAB_DIR" \
    "$LAB_DIR/scripts/restart-llama.sh" "$@"
  ;;
```

The script then does:
```bash
dc() {
  env "WORKSPACE=${WORKSPACE:-$LAB_DIR}" \
    docker compose -f "$LAB_COMPOSE" --project-directory "$LAB_DIR" "$@"
}
```

### Step 3: Update `./lab` help text

Update the `--help` and no-arg help paths to show which commands now route to subscripts. Keep it brief.

### Step 4: Validate

```bash
sh -n lab
sh -n bench/bench-runner.sh
sh -n scripts/restart-llama.sh
./lab --help
./lab bench --help
./lab restart-llama --help
wc -l lab   # target: under 400
```

---

## Verification

Verify the work by running:
```bash
./scripts/validate-tickets --strict
sh -n lab bench/bench-runner.sh scripts/restart-llama.sh
wc -l lab   # should be under 400
```

Expected: all checks pass, lab under 400 lines, help text intact.

---

## Files

| File | Role |
|------|------|
| `lab` | Remove inline bench + restart-llama handlers |
| `bench/bench-runner.sh` | New — bench subcommand handler |
| `scripts/restart-llama.sh` | New — restart-llama subcommand handler |
| `docs/tickets/ticket-020-extract-lab-handlers.md` | This ticket file |

---

## Dependencies

- **TICKET-019** — tech debt audit and planning (lab too big, ACTIVE)
- **DECISION-012** — delegation pattern (ACCEPTED, IMPLEMENTED) — references `tune` and `ai-analyze` extraction
- **TICKET-016** — WORKSPACE env var fix (RESOLVED)
- **TICKET-017** — model cache pre-population (RESOLVED)
- **TICKET-018** — tune sweep execution (RESOLVED)

---

## Notes

- The `bench` handler is the higher-priority extraction (~60 lines)
- The `restart-llama` handler uses `dc()` which is defined in `lab` — the script must define its own `dc()` wrapper or call docker compose directly
- After both extractions, `lab` should be under 400 lines (from current ~505)
- Remaining inline handlers (`setup`, `build`, `pull`, `stop`, `exec`, `shell`, help text) are all under 50 lines and can stay
