# GitHub Actions — When & Why to Add CI

> **What goes in here:** Decision criteria for what runs in GitHub Actions, how to structure workflows, and when to add new CI jobs. Prevents CI from becoming a dumping ground.

---

## Decision criteria — should something run in CI?

Add a check to CI only when it meets **at least two** of these:

| Criterion | Why it matters |
|-----------|----------------|
| **Fast** (< 30s typical) | CI runs on every push + every PR. Slow jobs create a feedback loop that people work around (pushing without checking). |
| **Security-critical** | If this check fails silently, the repo ships a vulnerability or data leak. Examples: workspace path validation, egress gates. |
| **People skip it locally** | Static checks, linting, schema validation. Developers forget or disable them; CI catches them every time. |
| **Requires shared infrastructure** | Needs a Docker network, external service, or specific OS. Something that only CI can reliably provide. |
| **Prevents expensive rework** | Catches bugs early so the agent doesn't spend hours on a wrong approach. |

**Does NOT belong in CI:**
- Live/E2E tests that require a running TUI, GPU, or Docker-in-Docker
- Human verification (e.g., "does the logo look right?")
- Anything that takes > 2 minutes
- Redundant checks already enforced locally (e.g., `tsc` when the dev runs `npm test` which builds first)

---

## Workflow vs job — what goes where?

### One workflow per subsystem (not per check)

A **workflow file** is a coarse-grained unit — it represents a subsystem, service, or deployment.

**Good workflow files:**
- `ticket-engine.yml` → the ticket engine module
- `ci.yml` → repo-wide checks (when they span multiple subsystems)
- `deploy.yml` → deployment to a target environment

**Bad workflow files:**
- `bats.yml` → bats tests are part of repo hygiene, not a subsystem
- `shellcheck.yml` → a single static check doesn't need its own file
- `lint.yml`, `test.yml`, `build.yml` → splitting checks by type creates parallelism problems (can't report together, can't cancel together)

### One workflow file → multiple parallel jobs

Within a workflow file, use **jobs** to group checks that:
1. Run on the same base environment (e.g., same `working-directory` or same OS)
2. Are independent of each other (no shared state between them)

**Example — `ci.yml` with 3 parallel jobs:**

```
ci.yml
├── node      — build, check:package, jest (TypeScript/Node)
├── bats      — lab.bats, tui-jsx.bats, exit-confirm.bats (bash + python3)
└── ticket-store — validate + no-file-mutation (needs checked-out repo)
```

All three start simultaneously on `ubuntu-latest`. They report as a single PR check group.

---

## Naming conventions

### Workflow file names
| Pattern | Example | When to use |
|---------|---------|-------------|
| `<subsystem>.yml` | `ticket-engine.yml` | A single module/subsystem |
| `ci.yml` | `ci.yml` | Repo-wide checks that span subsystems |
| `deploy-<env>.yml` | `deploy-staging.yml` | Deployment to a specific target |

### Workflow `name:` (what appears in GitHub UI)
- Human-readable: `"little-mester checks"`, `"ticket-engine"` — not the file name
- Use sentence case, not kebab-case

### Job names
- Short, descriptive: `"Build & test (TypeScript)"`, `"Bats (lab, TUI JSX, exit-confirm)"`
- Include the subsystem in parentheses when a job name is ambiguous

---

## Timeout policy

| Job type | Default timeout |
|----------|----------------|
| Build + test (Node) | 10 min |
| Bats (shell tests) | 5 min |
| Ticket store / validation | 5 min |
| Deploy | 15 min |

Timeouts prevent a hung job from blocking all future CI runs (the concurrency group cancels in-progress runs, but a stuck job still holds the lock).

---

## Concurrency policy

Every workflow file gets:

```yaml
concurrency:
  group: <workflow-name>-${{ github.ref }}
  cancel-in-progress: true
```

This means:
- Pushes to the same branch cancel the previous in-progress run
- PR pushes don't cancel the PR run (different `github.ref`)
- Future aggregate reporting works because all jobs in one workflow share the same concurrency group

---

## When to add a new check — decision tree

```
Is it a security-critical check?
  YES → Add it. (workspace validation, egress gates, schema validation)
  NO  → Is it fast (< 30s)?
        YES → Does people skip it locally?
              YES → Add it. (static checks, linting)
              NO  → Is it redundant with an existing CI check?
                     YES → Don't add it.
                     NO  → Evaluate: does it prevent expensive rework?
                            YES → Add it as a parallel job in the appropriate workflow.
                            NO  → Leave it for local development.
        NO  → Don't add it. (too slow for CI feedback loop)
```

---

## Anti-patterns to avoid

1. **One workflow per test type** — creates parallelism problems (can't report together, can't cancel together)
2. **Sequential jobs that could be parallel** — if jobs don't share state, run them in parallel
3. **Hardcoded secrets in workflow files** — use GitHub secrets or environment variables
4. **No timeout** — every job needs a `timeout-minutes`
5. **Checking out the repo twice** — share the checkout step via `uses: actions/checkout@v4` in the workflow root (inherits to all jobs)
6. **Running slow tests on PR** — fast feedback on PR, comprehensive tests on push
