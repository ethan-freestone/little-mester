# Ticket 019 -- Tech debt: lab is too big and hard to maintain

**Status**: ACTIVE
**Priority**: HIGH
**Type**: improvement
**Area**: infra
**Labels**: `scope/infra`, `type/improvement`
**Date**: 2026-10-22

---

## Objective

Reduce `./lab` file size and complexity by extracting all remaining inline subcommand handlers into standalone scripts, enforcing a maximum of 50 lines per handler in the main file.

---

## Scope & Boundaries

### In scope
- Audit every subcommand in `./lab` and measure its line count
- Extract handlers exceeding 50 lines into `bench/` or `scripts/` subscripts
- Ensure `exec` delegation with `VERBOSE` forwarding for each extracted handler
- Update `./lab --help` output to reflect new routing

### Do not modify
- `compose.yaml` service definitions
- `machine.conf` or `.little-mester.conf`
- Agent container images or Dockerfiles
- Existing `bench/bench-native.sh`, `bench/tune.sh`, `bench/ai-analyse.sh`

### File
- `lab` (primary target for extraction)
- `bench/` (subscript destination for bench-adjacent commands)
- `scripts/` (subscript destination for general tools)
- `docs/tickets/ticket-019-lab-too-big.md` (this ticket)
- `DECISIONS.md` (document the pattern — already done as DECISION-012)

---

## Steps

### Step 1: Audit `./lab` — measure each handler

Open `./lab` and count lines for each subcommand case arm. Record the count. Target handlers over 50 lines.

Current known:
- `setup` (~20 lines) — small, keep inline
- `build` (~8 lines) — small, keep inline
- `pull` (~5 lines) — small, keep inline
- `stop` (~1 line) — trivial
- `bench` (~60 lines) — EXTRACT to `bench/bench-runner.sh`
- `restart-llama` (~45 lines) — borderline, could extract to `scripts/restart-llama.sh`
- `tune` — already extracted (DECISION-012)
- `ai-analyze` — already extracted (DECISION-012)
- `exec` (~4 lines) — small, keep inline
- `shell` (~4 lines) — small, keep inline
- `--help` / no-arg paths — help text, keep inline

### Step 2: Extract `bench` handler

Create `bench/bench-runner.sh` with:
- HTTP mode build + wait + run logic (current lines 304-346)
- Native mode build + run logic (current lines 304-324)
- Accept `--json`, `-n`, `-t`, `-ub`, `--moe`, `--ctx` flags
- Forward to `dc` via the compose project directory

Update `lab`:
```bash
bench)
  shift
  exec env VERBOSE="$VERBOSE" BENCH_JSON="$BENCH_JSON" BENCH_N="$BENCH_N" NATIVE_THREADS="$NATIVE_THREADS" NATIVE_UB="$NATIVE_UB" NATIVE_MOE="$NATIVE_MOE" NATIVE_CTX="$NATIVE_CTX" "$LAB_DIR/bench/bench-runner.sh" "$@"
  ;;
```

### Step 3: Extract `restart-llama` handler (if exceeding 50 lines after cleanup)

Create `scripts/restart-llama.sh` with:
- `--cache` flag handling (sed injection into compose.yaml)
- `dc restart llamacpp || dc up -d llamacpp`

### Step 4: Update help text

Update the `--help` and no-arg help paths in `lab` to reflect which commands now delegate.

### Step 5: Verify

Run every `./lab` subcommand path (without docker) to confirm the help text and routing are intact:
```bash
./lab --help
./lab bench --help
./lab restart-llama --help
./lab tune --help
./lab ai-analyze --help
./lab bench --native --help
```

---

## Verification

Verify the work by running:
```bash
# Line count check
wc -l lab          # Should be under 400 lines after extraction

# Help text sanity — each should return 0
./lab --help
./lab bench --help
./lab restart-llama --help
./lab tune --help
./lab ai-analyze --help

# Syntax check
sh -n lab
sh -n bench/bench-runner.sh
```

Expected: all checks pass, lab under 400 lines.

---

## Files

| File | Role |
|------|------|
| `lab` | Primary target — extract inline handlers |
| `bench/bench-runner.sh` | New — bench subcommand handler |
| `scripts/restart-llama.sh` | New — restart-llama subcommand handler (if extracted) |
| `docs/tickets/ticket-019-lab-too-big.md` | This ticket file |
| `DECISIONS.md` | DECISION-012 documents the delegation pattern |

---

## Dependencies

- **DECISION-012** — establishes the delegation pattern (ACCEPTED, IMPLEMENTED)
- **TICKET-016** — WORKSPACE env var fix in bench-native.sh (RESOLVED)
- **TICKET-017** — model cache pre-population (RESOLVED)
- **TICKET-018** — tune sweep execution (RESOLVED)

---

## Notes

- `bench` handler is the highest-priority extraction target at ~60 lines
- The `confirm()` function (lines 151-164) and `check_workspace()` (lines 59-97) are shared and stay in `lab`
- Help text duplication is acceptable — it's small and self-documenting
- Target: `./lab` under 400 lines, no single handler over 50 lines
