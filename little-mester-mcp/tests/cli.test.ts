/**
 * CLI Tests — `little-mester-mcp/src/cli.ts`
 *
 * Tests for the CLI entry point and argument parsing.
 * Uses child_process.execSync to run the CLI in a separate Node process.
 * Ticket dir is always a fresh temp directory.
 */

import { execSync, spawnSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

const CLI = path.resolve(__dirname, '..', 'src', 'cli.ts');

// ── helpers ──────────────────────────────────────────────────────

function createTempDir(): string {
    const dir = fs.mkdtempSync('/tmp/cli-test-');
    // Seed a valid _index.json so the engine can read it
    fs.writeFileSync(
        path.join(dir, '_index.json'),
        JSON.stringify({ schemaVersion: 1, tickets: [], next_number: 1, updated_at: new Date().toISOString() }, null, 2)
    );
    return dir;
}

function cleanup(dir: string): void {
    if (fs.existsSync(dir)) fs.rmSync(dir, { recursive: true, force: true });
}

/** A PID that is guaranteed dead (the child has already exited). */
function deadPid(): number {
    const child = spawnSync(process.execPath, ['-e', 'process.exit(0)']);
    return child.pid ?? 999999;
}

function runCli(args: string[], env: Record<string, string> = {}): { stdout: string; stderr: string; exitCode: number } {
    try {
        const stdout = execSync(`npx ts-node "${CLI}" ${args.map(a => a.includes(' ') ? `"${a}"` : a).join(' ')}`, {
            cwd: path.dirname(CLI),
            env: { ...process.env, ...env, NODE_NO_WARNINGS: '1' },
            encoding: 'utf-8',
        });
        return { stdout, stderr: '', exitCode: 0 };
    } catch (err: unknown) {
        const e = err as { stdout?: string; stderr?: string; status?: number };
        return {
            stdout: e.stdout ?? '',
            stderr: e.stderr ?? '',
            exitCode: e.status ?? 1,
        };
    }
}

// ── argument parsing ─────────────────────────────────────────────

describe('Argument parsing', () => {
    let tmpDir: string;

    beforeEach(() => { tmpDir = createTempDir(); });
    afterEach(() => { cleanup(tmpDir); });

    it('exits 1 with no arguments (prints usage)', () => {
        const result = runCli([], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(1);
        expect(result.stdout).toContain('Usage:');
        expect(result.stdout).toContain('ticket-engine create');
    });

    it('exits 0 with --help', () => {
        const result = runCli(['--help'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(0);
        expect(result.stdout).toContain('Usage:');
    });

    it('exits 0 with -h', () => {
        const result = runCli(['-h'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(0);
    });

    it('exits 1 with unknown command', () => {
        const result = runCli(['bogus'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(1);
        expect(result.stderr).toContain("unknown command 'bogus'");
    });
});

// ── create ───────────────────────────────────────────────────────

describe('create', () => {
    let tmpDir: string;

    beforeEach(() => { tmpDir = createTempDir(); });
    afterEach(() => { cleanup(tmpDir); });

    it('creates a ticket and prints summary', () => {
        const result = runCli(['create', '--area', 'auth', '--type', 'feature', 'Add login'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(0);
        expect(result.stdout).toContain('✅ Created ticket #1');
        expect(result.stdout).toContain('[DRAFT]');
        expect(result.stdout).toContain('feature');
        expect(result.stdout).toContain('Add login');
    });

    it('creates a ticket with --objective', () => {
        const result = runCli(['create', '--area', 'auth', 'Login flow', '--objective', 'Users can login'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(0);
        expect(result.stdout).toContain('Objective: Users can login');
    });

    it('rejects missing title', () => {
        const result = runCli(['create', '--area', 'auth'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(1);
        expect(result.stderr).toContain('requires a title argument');
    });

    it('rejects invalid type', () => {
        const result = runCli(['create', '--area', 'x', '--type', 'bogus', 'T'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(1);
        expect(result.stderr).toContain("Invalid ticket type 'bogus'");
    });

    it('accepts all valid types', () => {
        for (const type of ['spike', 'feature', 'bug', 'improvement', 'doc', 'infra']) {
            const result = runCli(['create', '--area', 'a', '--type', type, `T-${type}`], { TICKET_DIR: tmpDir });
            expect(result.exitCode).toBe(0);
            expect(result.stdout).toContain(type);
        }
    });

    it('defaults type to feature when not specified', () => {
        const result = runCli(['create', '--area', 'x', 'T'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(0);
        expect(result.stdout).toContain('feature');
    });

    it('creates a ticket with --priority', () => {
        const result = runCli(['create', '--area', 'auth', '--priority', 'critical', 'Auth breach fix'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(0);
        expect(result.stdout).toContain('Created ticket #1');
        const data = JSON.parse(fs.readFileSync(path.join(tmpDir, 'ticket-001.json'), 'utf-8'));
        expect(data.priority).toBe('critical');
    });

    it('rejects an invalid --priority', () => {
        const result = runCli(['create', '--area', 'auth', '--priority', 'urgent', 'Bad priority'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(1);
        expect(result.stderr).toContain("Invalid priority 'urgent'");
    });

    it('creates a ticket with --comment', () => {
        const result = runCli(['create', '--area', 'auth', '--comment', 'Blocker for sprint', 'Auth fix'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(0);
        expect(result.stdout).toContain('Comment: Blocker for sprint');
        const data = JSON.parse(fs.readFileSync(path.join(tmpDir, 'ticket-001.json'), 'utf-8'));
        expect(data.comments).toHaveLength(1);
        expect(data.comments[0].body).toBe('Blocker for sprint');
        expect(data.comments[0].author).toBe('cli');
    });

    it('persists ticket file to disk', () => {
        runCli(['create', '--area', 'infra', 'CI pipeline'], { TICKET_DIR: tmpDir });
        expect(fs.existsSync(path.join(tmpDir, 'ticket-001.json'))).toBe(true);
        const data = JSON.parse(fs.readFileSync(path.join(tmpDir, 'ticket-001.json'), 'utf-8'));
        expect(data.number).toBe(1);
        expect(data.title).toBe('CI pipeline');
    });

    it('increments numbers across invocations', () => {
        runCli(['create', '--area', 'a', 'T1'], { TICKET_DIR: tmpDir });
        const r2 = runCli(['create', '--area', 'a', 'T2'], { TICKET_DIR: tmpDir });
        expect(r2.stdout).toContain('✅ Created ticket #2');
    });

    it('uses --ticket-dir override', () => {
        const overrideDir = createTempDir();
        const result = runCli(['create', '--area', 'a', '--ticket-dir', overrideDir, 'T'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(0);
        expect(fs.existsSync(path.join(overrideDir, 'ticket-001.json'))).toBe(true);
        cleanup(overrideDir);
    });
});

// ── list ─────────────────────────────────────────────────────────

describe('list', () => {
    let tmpDir: string;

    beforeEach(() => { tmpDir = createTempDir(); });
    afterEach(() => { cleanup(tmpDir); });

    it('lists all tickets when none filtered', () => {
        runCli(['create', '--area', 'auth', 'Login'], { TICKET_DIR: tmpDir });
        runCli(['create', '--area', 'billing', 'Checkout'], { TICKET_DIR: tmpDir });
        const result = runCli(['list'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(0);
        expect(result.stdout).toContain('Found 2 ticket(s)');
    });

    it('filters by area', () => {
        runCli(['create', '--area', 'auth', 'Login'], { TICKET_DIR: tmpDir });
        runCli(['create', '--area', 'auth', 'Register'], { TICKET_DIR: tmpDir });
        runCli(['create', '--area', 'billing', 'Checkout'], { TICKET_DIR: tmpDir });
        const result = runCli(['list', '--area', 'auth'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(0);
        expect(result.stdout).toContain('Found 2 ticket(s)');
    });

    it('filters by status', () => {
        runCli(['create', '--area', 'a', 'T1'], { TICKET_DIR: tmpDir });
        runCli(['update', '1', '--status', 'active'], { TICKET_DIR: tmpDir });
        runCli(['create', '--area', 'a', 'T2'], { TICKET_DIR: tmpDir }); // still draft
        const result = runCli(['list', '--status', 'active'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(0);
        expect(result.stdout).toContain('Found 1 ticket(s)');
    });

    it('shows "No tickets found" when empty', () => {
        const result = runCli(['list'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(0);
        expect(result.stdout).toContain('No tickets found.');
    });
});

// ── update ───────────────────────────────────────────────────────

describe('update', () => {
    let tmpDir: string;

    beforeEach(() => { tmpDir = createTempDir(); });
    afterEach(() => { cleanup(tmpDir); });

    it('updates title', () => {
        runCli(['create', '--area', 'a', 'Old title'], { TICKET_DIR: tmpDir });
        const result = runCli(['update', '1', '--title', 'New title'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(0);
        expect(result.stdout).toContain('New title');
    });

    it('updates status', () => {
        runCli(['create', '--area', 'a', 'T'], { TICKET_DIR: tmpDir });
        const result = runCli(['update', '1', '--status', 'active'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(0);
        expect(result.stdout).toContain('[ACTIVE]');
    });

    it('updates priority', () => {
        runCli(['create', '--area', 'a', 'T'], { TICKET_DIR: tmpDir });
        const result = runCli(['update', '1', '--priority', 'major'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(0);
        const data = JSON.parse(fs.readFileSync(path.join(tmpDir, 'ticket-001.json'), 'utf-8'));
        expect(data.priority).toBe('major');
    });

    it('rejects an invalid --priority', () => {
        runCli(['create', '--area', 'a', 'T'], { TICKET_DIR: tmpDir });
        const result = runCli(['update', '1', '--priority', 'urgent'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(1);
        expect(result.stderr).toContain("Invalid priority 'urgent'");
    });

    it('appends comment via --comment', () => {
        runCli(['create', '--area', 'a', 'T'], { TICKET_DIR: tmpDir });
        const result = runCli(['update', '1', '--comment', 'Review needed'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(0);
        expect(result.stdout).toContain('Comment: Review needed');
        const data = JSON.parse(fs.readFileSync(path.join(tmpDir, 'ticket-001.json'), 'utf-8'));
        expect(data.comments).toHaveLength(1);
        expect(data.comments[0].body).toBe('Review needed');
    });

    it('updates multiple fields', () => {
        runCli(['create', '--area', 'a', 'T'], { TICKET_DIR: tmpDir });
        const result = runCli(['update', '1', '--status', 'active', '--priority', 'critical'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(0);
        expect(result.stdout).toContain('[ACTIVE]');
    });

    it('exits 1 for unknown ticket', () => {
        const result = runCli(['update', '999', '--title', 'X'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(1);
        expect(result.stderr).toContain('not found');
    });

    it('rejects invalid status transition', () => {
        runCli(['create', '--area', 'a', 'T'], { TICKET_DIR: tmpDir });
        const result = runCli(['update', '1', '--status', 'resolved'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(1);
        expect(result.stderr).toContain('Failed to update');
    });

    it('requires a ticket number', () => {
        const result = runCli(['update'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(1);
        expect(result.stderr).toContain('requires a ticket number');
    });
});

// ── archive ──────────────────────────────────────────────────────

describe('archive', () => {
    let tmpDir: string;

    beforeEach(() => { tmpDir = createTempDir(); });
    afterEach(() => { cleanup(tmpDir); });

    it('archives a ticket', () => {
        runCli(['create', '--area', 'a', 'T'], { TICKET_DIR: tmpDir });
        const result = runCli(['archive', '1'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(0);
        expect(result.stdout).toContain('📦 Ticket #1 archived');
        expect(result.stdout).toContain('[ARCHIVED]');
    });

    it('exits 1 for unknown ticket', () => {
        const result = runCli(['archive', '999'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(1);
        expect(result.stderr).toContain('not found');
    });

    it('requires a ticket number', () => {
        const result = runCli(['archive'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(1);
        expect(result.stderr).toContain('requires a ticket number');
    });
});

// ── audit ────────────────────────────────────────────────────────

describe('audit', () => {
    let tmpDir: string;

    beforeEach(() => { tmpDir = createTempDir(); });
    afterEach(() => { cleanup(tmpDir); });

    it('prints the audit trail', () => {
        runCli(['create', '--area', 'a', 'T'], { TICKET_DIR: tmpDir });
        runCli(['update', '1', '--priority', 'major'], { TICKET_DIR: tmpDir });

        const result = runCli(['audit'], { TICKET_DIR: tmpDir });

        expect(result.exitCode).toBe(0);
        expect(result.stdout).toContain('Audit log (2 entries)');
        expect(result.stdout).toContain('create');
        expect(result.stdout).toContain('update');
        expect(result.stdout).toContain('#001');
    });

    it('prints raw JSON with --json', () => {
        runCli(['create', '--area', 'a', 'T'], { TICKET_DIR: tmpDir });

        const result = runCli(['audit', '--json'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(0);

        const entries = JSON.parse(result.stdout);
        expect(entries).toHaveLength(1);
        expect(entries[0].operation).toBe('create');
        expect(entries[0].ticket_number).toBe(1);
        expect(entries[0].hash).toMatch(/^[0-9a-f]{64}$/);
    });

    it('limits output with --limit', () => {
        runCli(['create', '--area', 'a', 'T1'], { TICKET_DIR: tmpDir });
        runCli(['create', '--area', 'a', 'T2'], { TICKET_DIR: tmpDir });
        runCli(['update', '1', '--title', 'T1 updated'], { TICKET_DIR: tmpDir });

        const result = runCli(['audit', '--limit', '1'], { TICKET_DIR: tmpDir });

        expect(result.exitCode).toBe(0);
        expect(result.stdout).toContain('last 1 of 3 entries');
        expect(result.stdout).toContain('update');
    });

    it('rejects a non-positive --limit', () => {
        const result = runCli(['audit', '--limit', '0'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(1);
        expect(result.stderr).toContain('positive integer');
    });

    it('adopts pre-existing files with --baseline', () => {
        // Simulate a ticket that predates the audit log.
        fs.writeFileSync(path.join(tmpDir, 'ticket-001.json'), JSON.stringify({
            id: 1,
            file_path: 'ticket-001.json',
            number: 1,
            area: 'legacy',
            type: 'feature',
            status: 'draft',
            title: 'Legacy ticket',
        }, null, 2));
        // Remove the seeded index so the engine rebuilds it from disk.
        fs.unlinkSync(path.join(tmpDir, '_index.json'));

        const baseline = runCli(['audit', '--baseline'], { TICKET_DIR: tmpDir });
        expect(baseline.exitCode).toBe(0);
        expect(baseline.stdout).toContain('Adopted 1 ticket file(s) as baseline');

        const json = JSON.parse(runCli(['audit', '--json'], { TICKET_DIR: tmpDir }).stdout);
        expect(json).toHaveLength(1);
        expect(json[0].operation).toBe('baseline');
        expect(json[0].ticket_number).toBe(1);

        // Baseline clears the orphan, so validate is clean.
        const validate = runCli(['validate'], { TICKET_DIR: tmpDir });
        expect(validate.exitCode).toBe(0);
        expect(validate.stdout).toContain('✅ Orphans: none.');
    });

    it('exits 1 when the audit log is corrupt', () => {
        runCli(['create', '--area', 'a', 'T'], { TICKET_DIR: tmpDir });
        fs.writeFileSync(path.join(tmpDir, '_audit.json'), '{ not json');

        const result = runCli(['audit'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(1);
        expect(result.stderr).toContain('Failed to read audit log');
    });
});

// ── validate ─────────────────────────────────────────────────────

describe('validate', () => {
    let tmpDir: string;

    beforeEach(() => { tmpDir = createTempDir(); });
    afterEach(() => { cleanup(tmpDir); });

    it('returns synced when index and files match', () => {
        runCli(['create', '--area', 'a', 'T'], { TICKET_DIR: tmpDir });
        const result = runCli(['validate'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(0);
        expect(result.stdout).toContain('✅ Index is in sync');
        expect(result.stdout).toContain('✅ Audit:');
        expect(result.stdout).toContain('✅ Orphans: none.');
    });

    it('detects out-of-sync when extra file exists', () => {
        runCli(['create', '--area', 'a', 'T'], { TICKET_DIR: tmpDir });
        // Create a ghost file not in index
        fs.writeFileSync(path.join(tmpDir, 'ticket-099.json'), JSON.stringify({ number: 99, area: 'x', type: 'bug', status: 'draft', title: 'Ghost' }, null, 2));
        const result = runCli(['validate'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(1);
        expect(result.stdout).toContain('OUT OF SYNC');
    });

    it('reports missing files', () => {
        runCli(['create', '--area', 'a', 'T'], { TICKET_DIR: tmpDir });
        // Delete the file but leave index entry — engine reports this as "Extra index entries"
        // (index has entry with no corresponding file on disk)
        fs.unlinkSync(path.join(tmpDir, 'ticket-001.json'));
        const result = runCli(['validate'], { TICKET_DIR: tmpDir });
        expect(result.exitCode).toBe(1);
        expect(result.stdout).toContain('OUT OF SYNC');
        expect(result.stdout).toContain('Extra index entries');
    });

    it('exits 1 and flags orphans (never deletes them)', () => {
        runCli(['create', '--area', 'a', 'T1'], { TICKET_DIR: tmpDir });

        // A hand-written ticket that also updated the index: sync is fine, but
        // there is no audit entry, so it is an orphan.
        fs.writeFileSync(path.join(tmpDir, 'ticket-002.json'), JSON.stringify({
            id: 2,
            file_path: 'ticket-002.json',
            number: 2,
            area: 'handmade',
            type: 'bug',
            status: 'draft',
            title: 'Hand written',
        }, null, 2));
        const indexPath = path.join(tmpDir, '_index.json');
        const index = JSON.parse(fs.readFileSync(indexPath, 'utf-8'));
        index.tickets.push({ number: 2, file_path: 'ticket-002.json', status: 'draft', area: 'handmade' });
        fs.writeFileSync(indexPath, JSON.stringify(index, null, 2));

        const result = runCli(['validate'], { TICKET_DIR: tmpDir });

        expect(result.exitCode).toBe(1);
        expect(result.stdout).toContain('✅ Index is in sync');
        expect(result.stdout).toContain('Orphans: 1 ticket file(s)');
        // The file is still there: orphans are reported, not cleaned up.
        expect(fs.existsSync(path.join(tmpDir, 'ticket-002.json'))).toBe(true);
    });

    it('exits 1 on a stale lock from a dead process', () => {
        runCli(['create', '--area', 'a', 'T'], { TICKET_DIR: tmpDir });
        fs.writeFileSync(path.join(tmpDir, '.engine.lock'), JSON.stringify({
            pid: deadPid(),
            acquired_at: new Date().toISOString(),
        }, null, 2));

        const result = runCli(['validate'], { TICKET_DIR: tmpDir });

        expect(result.exitCode).toBe(1);
        expect(result.stdout).toContain('❌ Lock: Stale lock');
    });

    it('exits 1 when an audit entry was tampered with', () => {
        runCli(['create', '--area', 'a', 'T'], { TICKET_DIR: tmpDir });
        const auditPath = path.join(tmpDir, '_audit.json');
        const audit = JSON.parse(fs.readFileSync(auditPath, 'utf-8'));
        audit[0].ticket_number = 99;
        fs.writeFileSync(auditPath, JSON.stringify(audit, null, 2));

        const result = runCli(['validate'], { TICKET_DIR: tmpDir });

        expect(result.exitCode).toBe(1);
        expect(result.stdout).toContain('❌ Audit: Audit hash mismatch');
    });
});
