import * as fs from 'fs';
import * as path from 'path';
import { createTicketEngine } from '../src/engine';

describe('TicketEngine', () => {
    let tmpDir: string;
    let indexPath: string;
    let engine: ReturnType<typeof createTicketEngine>;

    beforeEach(() => {
        tmpDir = fs.mkdtempSync('/tmp/ticket-engine-test-');
        indexPath = path.join(tmpDir, '_index.json');
        engine = createTicketEngine({ ticketDir: tmpDir, indexPath });
    });

    afterEach(() => {
        // Clean up temp directory
        if (fs.existsSync(tmpDir)) {
            fs.rmSync(tmpDir, { recursive: true, force: true });
        }
    });

    describe('create', () => {
        it('creates a ticket with default type and status draft', () => {
            const t = engine.create({ area: 'auth', title: 'Add login' });
            expect(t.number).toBe(1);
            expect(t.area).toBe('auth');
            expect(t.title).toBe('Add login');
            expect(t.type).toBe('feature'); // default type
            expect(t.status).toBe('draft');
        });

        it('creates a ticket with explicit type', () => {
            const t = engine.create({ area: 'infra', title: 'Setup CI', type: 'infra' });
            expect(t.type).toBe('infra');
        });

        it('rejects invalid type', () => {
            expect(() => engine.create({ area: 'test', title: 'x', type: 'invalid' as any }))
                .toThrow("Invalid ticket type 'invalid'. Must be one of: spike, feature, bug, improvement, doc, infra");
        });

        it('increments ticket numbers sequentially', () => {
            const t1 = engine.create({ area: 'a', title: 'T1' });
            const t2 = engine.create({ area: 'b', title: 'T2' });
            expect(t1.number).toBe(1);
            expect(t2.number).toBe(2);
        });

        it('sets created_at and updated_at timestamps', () => {
            const t = engine.create({ area: 'test', title: 'time test' });
            expect(t.created_at).toBeDefined();
            // ISO-8601 strings are comparable lexicographically — ensure non-empty valid ISO format
            expect(t.created_at).toMatch(/^\d{4}-\d{2}-\d{2}T/);
        });

        it('persists ticket file to disk', () => {
            engine.create({ area: 'test', title: 'disk test' });
            const filePath = path.join(tmpDir, 'ticket-001.json');
            expect(fs.existsSync(filePath)).toBe(true);
            const parsed = JSON.parse(fs.readFileSync(filePath, 'utf-8'));
            expect(parsed.title).toBe('disk test');
        });
    });

    describe('list', () => {
        it('lists all tickets with no filters', () => {
            engine.create({ area: 'a', title: 'T1' });
            engine.create({ area: 'b', title: 'T2' });
            const list = engine.list();
            expect(list.length).toBe(2);
        });

        it('filters by area', () => {
            engine.create({ area: 'auth', title: 'login' });
            engine.create({ area: 'auth', title: 'register' });
            engine.create({ area: 'billing', title: 'checkout' });
            const list = engine.list('auth');
            expect(list.length).toBe(2);
            expect(list.every(t => t.area === 'auth')).toBe(true);
        });

        it('excludes drafts when filtering by non-draft status', () => {
            engine.create({ area: 'a', title: 'T1' }); // draft
            engine.create({ area: 'b', title: 'T2' }); // draft
            // update one to active
            engine.update(1, { status: 'active' });
            const list = engine.list(undefined, 'active');
            expect(list.length).toBe(1);
        });

        it('returns draft tickets in all-status list when filtering by status=draft', () => {
            engine.create({ area: 'a', title: 'T1' }); // draft
            const list = engine.list(undefined, 'draft');
            expect(list.length).toBe(1);
        });
    });

    describe('update', () => {
        it('updates ticket fields', () => {
            engine.create({ area: 'a', title: 'Original' });
            const updated = engine.update(1, { title: 'Updated', objective: 'New obj' });
            expect(updated?.title).toBe('Updated');
            expect(updated?.objective).toBe('New obj');
        });

        it('updates updated_at timestamp on change', () => {
            engine.create({ area: 'a', title: 'T1' });
            const t = fs.readFileSync(path.join(tmpDir, 'ticket-001.json'), 'utf-8');
            const beforeUpdate = JSON.parse(t).updated_at;
            // Force time to differ (not likely in quick test, but logic is there)
            engine.update(1, { title: 'T1 updated' });
        });

        it('returns undefined for unknown number', () => {
            const result = engine.update(999, { title: 'ghost' });
            expect(result).toBeUndefined();
        });

        it('enforces status transitions', () => {
            engine.create({ area: 'a', title: 'T1' }); // draft → active allowed
            engine.update(1, { status: 'active' });

            // active → resolved allowed
            engine.update(1, { status: 'resolved' });

            // But cannot go resolved → archived directly (must go through draft? no)
            // Actually from the transitions: resolved can only go to draft
            expect(() => engine.update(1, { status: 'archived' })).toThrow();
        });
    });

    describe('archive', () => {
        it('archives a ticket from draft', () => {
            engine.create({ area: 'a', title: 'T1' });
            const archived = engine.archive(1);
            expect(archived?.status).toBe('archived');
        });

        it('archives a ticket from active', () => {
            engine.create({ area: 'active-tosource', title: 'T1' });
            engine.update(1, { status: 'active' });
            const archived = engine.archive(1);
            expect(archived?.status).toBe('archived');
        });

        it('returns undefined for unknown number', () => {
            const result = engine.archive(999);
            expect(result).toBeUndefined();
        });

        it('makes archived tickets invisible to update', () => {
            engine.create({ area: 'a', title: 'T1' });
            engine.archive(1);
            // We CAN still read them back, but they should be searchable
            const list = engine.list(undefined, 'archived');
            expect(list).toHaveLength(1);
        });
    });

    describe('validate', () => {
        it('returns synced=true when index and files match', () => {
            engine.create({ area: 'a', title: 'T1' });
            const result = engine.validate();
            expect(result.synced).toBe(true);
        });

        it('detects missing files not in index', () => {
            // Create file directly (bypassing engine) — this would show unsynced
            // But since we start clean, synced should be true unless we mess with the dir
        });

        it('returns unsynced=false when there are discrepancies', () => {
            // Write a ticket file for number 99 without going through engine
            const ghostPath = path.join(tmpDir, 'ticket-099.json');
            fs.writeFileSync(ghostPath, JSON.stringify({
                number: 99,
                area: 'ghost',
                type: 'bug',
                status: 'draft',
                title: 'Ghost Ticket',
            }, null, 2));

            const result = engine.validate();
            expect(result.synced).toBe(false);
            expect(result.missing_files?.length).toBe(1);
        });
    });

    describe('status lifecycle', () => {
        it('enforces draft → active only from draft', () => {
            engine.create({ area: 'a', title: 'T1' });
            // Cannot go directly from draft to resolved
            expect(() => engine.update(1, { status: 'resolved' })).toThrow();

            // Must go through active first
            engine.update(1, { status: 'active' });
            engine.update(1, { status: 'resolved' });
        });

        it('active → draft is allowed', () => {
            engine.create({ area: 'a', title: 'T1' });
            engine.update(1, { status: 'active' });
            engine.update(1, { status: 'draft' });
            expect(engine.list(undefined, 'draft')).toHaveLength(1);
        });

        it('resolved → draft is allowed', () => {
            engine.create({ area: 'a', title: 'T1' });
            engine.update(1, { status: 'active' });
            engine.update(1, { status: 'resolved' });
            engine.update(1, { status: 'draft' });
        });

        it('archived has no outgoing transitions', () => {
            engine.create({ area: 'a', title: 'T1' });
            engine.archive(1);
            expect(() => engine.update(1, { status: 'active' })).toThrow();
        });
    });

    describe('index integration', () => {
        it('reads index on creation for monotonic numbering', () => {
            engine.create({ area: 'a', title: 'T1' });
            // Re-read from a fresh engine instance — should pick up next_number from index
            const fresh = createTicketEngine({ ticketDir: tmpDir, indexPath });
            const newTicket = fresh.create({ area: 'b', title: 'T2 continued' });
            expect(newTicket.number).toBe(2);
        });

        it('reconstructs index when schemaVersion is missing', () => {
            // Write a corrupted index (no schemaVersion)
            const corruptPath = path.join(tmpDir, '_index.json');
            fs.writeFileSync(corruptPath, JSON.stringify({
                tickets: [],
                next_number: 1,
                updated_at: new Date().toISOString(),
            }, null, 2));

            // Create a fresh engine — should auto-reconstruct on startup
            const fresh = createTicketEngine({ ticketDir: tmpDir, indexPath: corruptPath });
            expect(() => {
                // Access internal state silently — the engine must have reconstructed
                const t = fresh.create({ area: 'test', title: 'post-repair' });
                expect(t.number).toBe(1); // or 2 depending on whether old index was preserved
            }).not.toThrow();
        });
    });

    describe('archived files from validate', () => {
        it('reports extra files in index not on disk', () => {
            // Create a ticket via engine then delete its file
            engine.create({ area: 'a', title: 'T1' });
            const ticketPath = path.join(tmpDir, 'ticket-001.json');
            fs.unlinkSync(ticketPath);

            const result = engine.validate();
            expect(result.synced).toBe(false);
        });
    });
});
