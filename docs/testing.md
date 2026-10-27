# Testing

> How to run, write, and keep tests fast in little-mester.

---

## Why performance matters

Tests that take minutes lose their purpose: developers skip them, CI becomes a bottleneck, and the feedback loop that catches regressions evaporates. A test suite should run in **seconds**, not minutes, so that:

1. **Developers run it locally before every commit** — if it takes 30s+, they'll skip it.
2. **CI gives fast feedback on PRs** — a 4-minute check discourages force-pushes and slows iteration.
3. **Parallel tests stay parallel** — a slow test serialises the whole suite when `maxWorkers` splits the load.

As a rule of thumb: the full suite should complete in under 30 seconds. If a test file is slow, profile it individually (`npx jest <file>`) and find the bottleneck before making any other change.

---

## Running tests

### Ticket engine (Jest)

```bash
cd little-mester-mcp
npm test          # builds + runs the full suite (143 tests)
npm run test:coverage   # builds + runs with coverage
```

### Bats (host-side)

```bash
bats tests/       # requires bats-core installed
```

---

## Speed-up techniques

### 1. Pre-build and run compiled JS — don't use `npx ts-node` per test

`npx ts-node` spawns a full Node process, loads ts-node, compiles TypeScript, and runs your code. Doing that **once per test** (56 CLI tests × one `ts-node` startup each = ~120 seconds) is the single biggest waste.

**Fix:** Build the project once, then run the compiled JS directly:

```ts
// ❌ Slow — 2+ seconds per test call
function runCli(args: string[]): string {
    return execSync(`npx ts-node "${CLI}" ${args.map(quote).join(' ')}`, { /* … */ }).stdout;
}

// ✅ Fast — <100ms per test call
const CLI = path.resolve(__dirname, '..', 'dist', 'cli.js');

function runCli(args: string[]): { stdout: string; stderr: string; exitCode: number } {
    const result = spawnSync(process.execPath, [CLI, ...args], {
        cwd: path.dirname(CLI),
        env: { ...process.env, NODE_NO_WARNINGS: '1' },
        encoding: 'utf-8',
    });
    return { stdout: result.stdout ?? '', stderr: result.stderr ?? '', exitCode: result.status ?? 1 };
}
```

**Key points:**
- Use `spawnSync` with an **argument array**, not `execSync` with a shell-joined string. Joining with spaces breaks multi-word arguments (`"Add login"` → `Add login` = two tokens).
- `spawnSync` never throws — the exit code is in `result.status`. No try/catch needed.
- Set `NODE_NO_WARNINGS: '1'` to suppress noisy TypeScript/Node warnings in test output.

### 2. Replace `spawnSync(process.execPath, ...)` with lightweight alternatives

Any test helper that needs to confirm a PID is dead should NOT spawn a full Node.js process. Node startup alone is ~200ms.

```ts
// ❌ Slow — spawns a full Node process every call
function deadPid(): number {
    const child = spawnSync(process.execPath, ['-e', 'process.exit(0)']);
    return child.pid ?? 999999;
}

// ✅ Fast — just return a known-high PID (the test only checks that the lock is stale)
function deadPid(): number {
    return 999999;
}
```

### 3. Use Jest parallel workers

Jest splits tests across workers by default, but setting `maxWorkers` explicitly ensures heavy test files are parallelised:

```js
// jest.config.js
module.exports = {
  preset: 'ts-jest',
  testEnvironment: 'node',
  roots: ['<rootDir>/tests'],
  testMatch: ['**/*.test.ts'],
  maxWorkers: '50%',
};
```

### 4. Remove `--detectOpenHandles` from CI

`--detectOpenHandles` forces Jest to walk open file descriptors, timers, and connections after every test. It adds significant overhead for negligible benefit in a test-only context.

```json
{
  "scripts": {
    "test": "npm run build && jest --verbose"
  }
}
```

### 5. Profile individual test files

When a suite is slow, isolate the culprit:

```bash
npx jest tests/engine.test.ts   # 55 tests
npx jest tests/cli.test.ts      # 56 tests
npx jest tests/mcp-server.test.ts # 32 tests
```

This tells you which file to optimise first. In our case, `cli.test.ts` was the bottleneck at 127s (fixed to ~9s).

### 6. Seed test data once per `beforeEach`

Avoid repeated setup in individual tests. Use `beforeEach` to create a fresh temp directory and seed a valid `_index.json` once, then let each test run against a clean state.

```ts
function createTempDir(): string {
    const dir = fs.mkdtempSync('/tmp/test-');
    fs.writeFileSync(
        path.join(dir, '_index.json'),
        JSON.stringify({ schemaVersion: 1, tickets: [], next_number: 1, updated_at: new Date().toISOString() }, null, 2)
    );
    return dir;
}
```

### 7. Keep test dependencies isolated

Each test file should manage its own temp directory lifecycle (`beforeEach`/`afterEach`). Never share state between tests — this enables parallel execution and makes failures reproducible.

---

## Testing conventions

### File naming

- Tests live alongside source: `src/engine.ts` → `tests/engine.test.ts`
- One test file per logical module

### Test structure

```ts
describe('ModuleName', () => {
    let tmpDir: string;
    let engine: TicketEngine;

    beforeEach(() => { /* fresh state */ });
    afterEach(() => { /* cleanup */ });

    describe('methodName', () => {
        it('handles the happy path', () => { /* … */ });
        it('rejects invalid input', () => { /* … */ });
    });
});
```

### Assertions

- Use `expect().toBe()` for exact matches, `expect().toContain()` for substring matches
- Always assert on **observable output** (stdout, file content, return values) — not internal state
- For CLI tests, assert on both exit code and output content

### Test coverage goals

- Engine logic: 100% — core business logic must be fully covered
- CLI: cover all commands, flags, and error paths
- MCP server: cover tool registration, input validation, and error handling

---

## CI checklist

Before merging a PR that touches tests:

- [ ] Full suite passes (`npm test`)
- [ ] Suite runs in under 30 seconds
- [ ] No skipped tests (`it.skip`, `describe.skip`) unless explicitly justified
- [ ] New test files are added to the test match pattern if needed
- [ ] Coverage report doesn't show new gaps in engine logic
