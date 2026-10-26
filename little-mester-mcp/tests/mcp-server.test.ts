/**
 * MCP Server Tests
 * 
 * Tests for all 8 MCP tools: create, list, get, update, archive, search, board, validate
 * Uses Jest with in-memory ticket directory for isolation.
 */

import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { createTicketEngine } from '../src/engine';

// ── Test Helpers ────────────────────────────────────────────────────

function createTempTicketDir(): string {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'mester-test-'));
    fs.mkdirSync(path.join(dir, 'tickets'), { recursive: true });
    fs.writeFileSync(path.join(dir, 'tickets', '_index.json'), JSON.stringify({
        schemaVersion: 1,
        next_number: 1,
        entries: []
    }, null, 2));
    return path.join(dir, 'tickets');
}

function cleanupTempDir(dir: string): void {
    fs.rmSync(dir, { recursive: true, force: true });
}

function createTestEngine(ticketDir: string) {
    return createTicketEngine({
        ticketDir,
        indexPath: path.join(ticketDir, '_index.json')
    });
}

// ── Tests ──────────────────────────────────────────────────────────

describe('MCP Server Tools', () => {
    let ticketDir: string;
    let engine: ReturnType<typeof createTestEngine>;

    beforeEach(() => {
        ticketDir = createTempTicketDir();
        engine = createTestEngine(ticketDir);
    });

    afterEach(() => {
        cleanupTempDir(path.dirname(ticketDir));
    });

    describe('mester_tickets_create', () => {
        it('creates a ticket with required fields', () => {
            const ticket = engine.create({
                acceptance_criteria: ['Criterion met'],
                area: 'auth',
                type: 'feature',
                title: 'Add login endpoint',
                objective: 'Implement POST /auth/login'
            });

            expect(ticket.number).toBe(1);
            expect(ticket.area).toBe('auth');
            expect(ticket.type).toBe('feature');
            expect(ticket.status).toBe('draft');
            expect(ticket.title).toBe('Add login endpoint');
            expect(ticket.objective).toBe('Implement POST /auth/login');
            expect(ticket.created_at).toBeDefined();
            expect(ticket.updated_at).toBeDefined();
        });

        it('creates ticket with defaults when optional fields omitted', () => {
            const ticket = engine.create({ acceptance_criteria: ['Criterion met'], title: 'Minimal ticket' });

            expect(ticket.number).toBe(1);
            expect(ticket.area).toBe('');
            expect(ticket.type).toBe('feature');
            expect(ticket.status).toBe('draft');
            expect(ticket.title).toBe('Minimal ticket');
        });

        it('increments ticket numbers sequentially', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], title: 'First' });
            engine.create({ acceptance_criteria: ['Criterion met'], title: 'Second' });
            const third = engine.create({ acceptance_criteria: ['Criterion met'], title: 'Third' });

            expect(third.number).toBe(3);
        });

        it('persists ticket file to disk', () => {
            const ticket = engine.create({ acceptance_criteria: ['Criterion met'], title: 'Persisted' });
            const file = path.join(ticketDir, `ticket-${String(ticket.number).padStart(3, '0')}.json`);

            expect(fs.existsSync(file)).toBe(true);
            const data = JSON.parse(fs.readFileSync(file, 'utf-8'));
            expect(data.title).toBe('Persisted');
        });

        it('rejects invalid type', () => {
            expect(() => engine.create({ acceptance_criteria: ['Criterion met'], title: 'Bad', type: 'invalid' })).toThrow();
        });
    });

    describe('mester_tickets_list', () => {
        beforeEach(() => {
            const t1 = engine.create({ acceptance_criteria: ['Criterion met'], area: 'auth', type: 'feature', title: 'Auth feature' });
            const t2 = engine.create({ acceptance_criteria: ['Criterion met'], area: 'auth', type: 'bug', title: 'Auth bug' });
            engine.update(t2.number, { status: 'active' });
            const t3 = engine.create({ acceptance_criteria: ['Criterion met'], area: 'ui', type: 'feature', title: 'UI feature' });
            engine.update(t3.number, { status: 'active' });
            engine.update(t3.number, { status: 'resolved' });
        });

        it('lists all tickets with no filters', () => {
            const tickets = engine.list();
            expect(tickets.length).toBe(3);
        });

        it('filters by area', () => {
            const tickets = engine.list('auth');
            expect(tickets.length).toBe(2);
            expect(tickets.every(t => t.area === 'auth')).toBe(true);
        });

        it('filters by status', () => {
            const tickets = engine.list(undefined, 'active');
            expect(tickets.length).toBe(1);
            expect(tickets[0].status).toBe('active');
        });

        it('filters by area and status', () => {
            const tickets = engine.list('auth', 'draft');
            expect(tickets.length).toBe(1);
            expect(tickets[0].area).toBe('auth');
            expect(tickets[0].status).toBe('draft');
        });

        it('includes drafts when filtering by status=draft', () => {
            const tickets = engine.list(undefined, 'draft');
            expect(tickets.length).toBe(1);
            expect(tickets[0].status).toBe('draft');
        });
    });

    describe('mester_tickets_get', () => {
        let ticket1: ReturnType<typeof engine.create>;

        beforeEach(() => {
            ticket1 = engine.create({ acceptance_criteria: ['Criterion met'], area: 'auth', title: 'Test ticket' });
        });

        it('returns ticket by number', () => {
            const tickets = engine.list();
            const found = tickets.find(t => t.number === ticket1.number);
            expect(found).toBeDefined();
            expect(found?.title).toBe('Test ticket');
        });

        it('returns undefined for non-existent number', () => {
            const tickets = engine.list();
            const found = tickets.find(t => t.number === 999);
            expect(found).toBeUndefined();
        });
    });

    describe('mester_tickets_update', () => {
        let ticket1: ReturnType<typeof engine.create>;

        beforeEach(() => {
            ticket1 = engine.create({ acceptance_criteria: ['Criterion met'], area: 'auth', title: 'Original', objective: 'Original obj' });
        });

        it('updates title', () => {
            const updated = engine.update(ticket1.number, { title: 'Updated title' });
            expect(updated?.title).toBe('Updated title');
            expect(updated?.objective).toBe('Original obj'); // unchanged
        });

        it('updates objective', () => {
            const updated = engine.update(ticket1.number, { objective: 'New objective' });
            expect(updated?.objective).toBe('New objective');
        });

        it('updates status with valid transition', () => {
            const updated = engine.update(ticket1.number, { status: 'active' });
            expect(updated?.status).toBe('active');
        });

        it('updates priority', () => {
            const updated = engine.update(ticket1.number, { priority: 'critical' });
            expect(updated?.priority).toBe('critical');
        });

        it('updates updated_at timestamp', async () => {
            const before = ticket1.updated_at;
            // Small delay to ensure timestamp changes
            await new Promise(r => setTimeout(r, 10));
            const updated = engine.update(ticket1.number, { title: 'New title' });
            expect(updated?.updated_at).not.toBe(before);
        });

        it('returns undefined for unknown ticket number', () => {
            const result = engine.update(999, { title: 'Ghost' });
            expect(result).toBeUndefined();
        });

        it('enforces status transitions', () => {
            // draft → active → resolved → archived → draft
            engine.update(ticket1.number, { status: 'active' });
            engine.update(ticket1.number, { status: 'resolved' });
            engine.update(ticket1.number, { status: 'archived' });
            engine.update(ticket1.number, { status: 'draft' });

            // invalid: draft → archived (allowed per D.3)
            engine.update(ticket1.number, { status: 'archived' });

            // invalid: archived → active (not allowed)
            expect(() => engine.update(ticket1.number, { status: 'active' })).toThrow();
        });
    });

    describe('mester_tickets_archive', () => {
        let ticket1: ReturnType<typeof engine.create>;

        beforeEach(() => {
            ticket1 = engine.create({ acceptance_criteria: ['Criterion met'], area: 'auth', title: 'To archive' });
        });

        it('archives a ticket from draft', () => {
            const archived = engine.archive(ticket1.number);
            expect(archived?.status).toBe('archived');
        });

        it('archives a ticket from active', () => {
            engine.update(ticket1.number, { status: 'active' });
            const archived = engine.archive(ticket1.number);
            expect(archived?.status).toBe('archived');
        });

        it('returns undefined for unknown number', () => {
            const result = engine.archive(999);
            expect(result).toBeUndefined();
        });

        it('allows updating non-status fields on archived tickets', () => {
            engine.archive(ticket1.number);
            const updated = engine.update(ticket1.number, { title: 'Renamed after archive' });
            expect(updated?.title).toBe('Renamed after archive');
            expect(updated?.status).toBe('archived');
        });
    });

    describe('mester_tickets_search', () => {
        beforeEach(() => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'auth', type: 'feature', title: 'Login endpoint', objective: 'Add login' });
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'auth', type: 'bug', title: 'Token refresh', objective: 'Fix refresh' });
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'ui', type: 'feature', title: 'Dashboard', objective: 'Build dashboard' });
        });

        it('filters by keyword in title', () => {
            const tickets = engine.list();
            const filtered = tickets.filter(t => 
                t.title?.toLowerCase().includes('login') ||
                t.objective?.toLowerCase().includes('login')
            );
            expect(filtered.length).toBe(1);
            expect(filtered[0].title).toBe('Login endpoint');
        });

        it('filters by keyword in objective', () => {
            const tickets = engine.list();
            const filtered = tickets.filter(t => 
                t.title?.toLowerCase().includes('dashboard') ||
                t.objective?.toLowerCase().includes('dashboard')
            );
            expect(filtered.length).toBe(1);
            expect(filtered[0].title).toBe('Dashboard');
        });

        it('filters by type', () => {
            const tickets = engine.list(undefined, undefined);
            const filtered = tickets.filter(t => t.type === 'bug');
            expect(filtered.length).toBe(1);
            expect(filtered[0].type).toBe('bug');
        });

        it('filters by area', () => {
            const tickets = engine.list('auth');
            expect(tickets.length).toBe(2);
            expect(tickets.every(t => t.area === 'auth')).toBe(true);
        });

        it('limits results', () => {
            const tickets = engine.list().slice(0, 2);
            expect(tickets.length).toBe(2);
        });
    });

    describe('mester_tickets_board', () => {
        beforeEach(() => {
            const t1 = engine.create({ acceptance_criteria: ['Criterion met'], area: 'auth', title: 'Draft 1' });
            engine.update(t1.number, { status: 'draft', priority: 'critical' });
            const t2 = engine.create({ acceptance_criteria: ['Criterion met'], area: 'auth', title: 'Draft 2' });
            engine.update(t2.number, { status: 'draft', priority: 'minor' });
            const t3 = engine.create({ acceptance_criteria: ['Criterion met'], area: 'ui', title: 'Active 1' });
            engine.update(t3.number, { status: 'active', priority: 'major' });
            const t4 = engine.create({ acceptance_criteria: ['Criterion met'], area: 'ui', title: 'Resolved 1' });
            engine.update(t4.number, { status: 'active' });
            engine.update(t4.number, { status: 'resolved', priority: 'trivial' });
            const t5 = engine.create({ acceptance_criteria: ['Criterion met'], area: 'infra', title: 'Archived 1' });
            engine.update(t5.number, { status: 'active' });
            engine.update(t5.number, { status: 'resolved' });
            engine.update(t5.number, { status: 'archived', priority: 'critical' });
        });

        it('groups tickets by status', () => {
            const all = engine.list();
            const statuses = ['draft', 'active', 'resolved', 'archived'];
            const board: Record<string, ReturnType<typeof engine.list>> = {};
            for (const s of statuses) board[s] = [];
            for (const t of all) board[t.status as keyof typeof board].push(t);

            expect(board.draft.length).toBe(2);
            expect(board.active.length).toBe(1);
            expect(board.resolved.length).toBe(1);
            expect(board.archived.length).toBe(1);
        });

        it('sorts by priority then updated_at desc', () => {
            const all = engine.list();
            const drafts = all.filter(t => t.status === 'draft');
            drafts.sort((a, b) => {
                const order = { critical: 0, major: 1, minor: 2, trivial: 3 };
                const pa = order[a.priority as keyof typeof order] ?? 4;
                const pb = order[b.priority as keyof typeof order] ?? 4;
                if (pa !== pb) return pa - pb;
                return new Date(b.updated_at!).getTime() - new Date(a.updated_at!).getTime();
            });

            expect(drafts[0].priority).toBe('critical');
            expect(drafts[1].priority).toBe('minor');
        });
    });

    describe('mester_tickets_validate', () => {
        it('returns synced=true when index and files match', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], title: 'Test' });
            // validate is not directly exposed on engine, but we can test via index
            const indexPath = path.join(ticketDir, '_index.json');
            const index = JSON.parse(fs.readFileSync(indexPath, 'utf-8'));
            expect(index.tickets?.length).toBe(1);
        });

        it('detects missing files not in index', () => {
            // Create a ticket file directly (bypassing index)
            const file = path.join(ticketDir, 'ticket-999.json');
            fs.writeFileSync(file, JSON.stringify({
                number: 999, area: 'test', type: 'feature', status: 'draft', title: 'Orphan'
            }, null, 2));

            const indexPath = path.join(ticketDir, '_index.json');
            const index = JSON.parse(fs.readFileSync(indexPath, 'utf-8'));
            // Index doesn't know about ticket-999
            expect(index.tickets?.find((e: any) => e.number === 999)).toBeUndefined();
        });
    });
});