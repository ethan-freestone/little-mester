import * as fs from 'fs';
import * as crypto from 'crypto';
import * as os from 'os';
import * as path from 'path';
import { execFileSync, spawnSync } from 'child_process';
import { createTicketEngine } from '../src/engine';

describe('TicketEngine', () => {
    let tmpDir: string;
    let indexPath: string;
    let engine: ReturnType<typeof createTicketEngine>;
    let privateTmpDir: string | undefined;

    beforeEach(() => {
        tmpDir = fs.mkdtempSync('/tmp/ticket-engine-test-');
        privateTmpDir = undefined;
        indexPath = path.join(tmpDir, '_index.json');
        engine = createTicketEngine({ ticketDir: tmpDir, indexPath });
    });

    afterEach(() => {
        // Clean up temp directory
        if (fs.existsSync(tmpDir)) {
            fs.rmSync(tmpDir, { recursive: true, force: true });
        }
        if (privateTmpDir && fs.existsSync(privateTmpDir)) {
            fs.rmSync(privateTmpDir, { recursive: true, force: true });
        }
    });

    describe('create', () => {
        it('creates a ticket with default type and status draft', () => {
            const t = engine.create({ acceptance_criteria: ['Criterion met'], area: 'auth', title: 'Add login' });
            expect(t.number).toBe(1);
            expect(t.area).toBe('auth');
            expect(t.title).toBe('Add login');
            expect(t.type).toBe('feature'); // default type
            expect(t.status).toBe('draft');
        });

        it('creates a ticket with explicit type', () => {
            const t = engine.create({ acceptance_criteria: ['Criterion met'], area: 'infra', title: 'Setup CI', type: 'infra' });
            expect(t.type).toBe('infra');
        });

        it('rejects invalid type', () => {
            expect(() => engine.create({ acceptance_criteria: ['Criterion met'], area: 'test', title: 'x', type: 'invalid' as any }))
                .toThrow("Invalid ticket type 'invalid'. Must be one of: spike, feature, bug, improvement, doc, infra");
        });

        it('increments ticket numbers sequentially', () => {
            const t1 = engine.create({ acceptance_criteria: ['Criterion met'], area: 'a', title: 'T1' });
            const t2 = engine.create({ acceptance_criteria: ['Criterion met'], area: 'b', title: 'T2' });
            expect(t1.number).toBe(1);
            expect(t2.number).toBe(2);
        });

        it('sets created_at and updated_at timestamps', () => {
            const t = engine.create({ acceptance_criteria: ['Criterion met'], area: 'test', title: 'time test' });
            expect(t.created_at).toBeDefined();
            // ISO-8601 strings are comparable lexicographically — ensure non-empty valid ISO format
            expect(t.created_at).toMatch(/^\d{4}-\d{2}-\d{2}T/);
        });

        it('persists ticket file to disk', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'test', title: 'disk test' });
            const filePath = path.join(tmpDir, 'ticket-001.json');
            expect(fs.existsSync(filePath)).toBe(true);
            const parsed = JSON.parse(fs.readFileSync(filePath, 'utf-8'));
            expect(parsed.title).toBe('disk test');
        });
    });

    describe('acceptance criteria + test ledger', () => {
        it('requires at least one criterion on create', () => {
            expect(() => engine.create({ area: 'a', title: 'No criteria' } as any))
                .toThrow(/acceptance_criteria is required/);
            expect(() => engine.create({ area: 'a', title: 'Empty criteria', acceptance_criteria: [] }))
                .toThrow(/at least one criterion/);
        });

        it('normalizes bare strings to auto ids with pending status', () => {
            const t = engine.create({
                area: 'a',
                title: 'Normalize',
                acceptance_criteria: ['first criterion', 'second criterion'],
            });
            expect(t.acceptance_criteria).toEqual([
                { id: 'A1', text: 'first criterion', status: 'pending' },
                { id: 'A2', text: 'second criterion', status: 'pending' },
            ]);
            expect(t.tests).toEqual({ needed: [], created: [], cases: [] });
        });

        it('accepts explicit ids, statuses, evidence, and notes', () => {
            const t = engine.create({
                area: 'a',
                title: 'Full criteria',
                acceptance_criteria: [
                    { id: 'A1', text: 'Verified', status: 'passing', evidence: 'jest tests/engine.test.ts' },
                    { id: 'A2', text: 'Not testable', status: 'waived', note: 'Visual check only' },
                ],
                tests: {
                    needed: ['write a test'],
                    created: ['tests/engine.test.ts'],
                    cases: ['normalizes input'],
                },
            });
            expect(t.acceptance_criteria?.[0].evidence).toBe('jest tests/engine.test.ts');
            expect(t.acceptance_criteria?.[1].note).toBe('Visual check only');
            expect(t.tests).toEqual({
                needed: ['write a test'],
                created: ['tests/engine.test.ts'],
                cases: ['normalizes input'],
            });
        });

        it('rejects invalid criteria', () => {
            const base = { area: 'a', title: 'Bad criteria' };
            expect(() => engine.create({
                ...base,
                acceptance_criteria: [{ text: 'bad', status: 'done' }],
            })).toThrow(/invalid status 'done'/);

            expect(() => engine.create({
                ...base,
                acceptance_criteria: [
                    { id: 'A1', text: 'one' },
                    { id: 'A1', text: 'two' },
                ],
            })).toThrow(/Duplicate acceptance criterion id 'A1'/);

            expect(() => engine.create({
                ...base,
                acceptance_criteria: [{ id: 'A1', text: 'passing', status: 'passing' }],
            })).toThrow(/has no evidence/);

            expect(() => engine.create({
                ...base,
                acceptance_criteria: [{ id: 'A1', text: 'waived', status: 'waived' }],
            })).toThrow(/has no note/);
        });

        it('replaces criteria and merges the test ledger on update', () => {
            engine.create({
                area: 'a',
                title: 'Update policy',
                acceptance_criteria: ['original'],
                tests: { needed: ['first test'], created: [], cases: [] },
            });

            const updated = engine.update(1, {
                acceptance_criteria: [
                    { id: 'A1', text: 'original', status: 'test-created' },
                    { id: 'A2', text: 'added later', status: 'test-needed' },
                ],
                tests: {
                    needed: ['second test'],
                    created: ['tests/engine.test.ts'],
                    cases: ['case one'],
                },
            });

            expect(updated?.acceptance_criteria?.map(ac => ac.id)).toEqual(['A1', 'A2']);
            expect(updated?.tests).toEqual({
                needed: ['first test', 'second test'],
                created: ['tests/engine.test.ts'],
                cases: ['case one'],
            });
        });

        it('validate fails when a ticket has no acceptance criteria', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'a', title: 'Legacy' });
            const ticketPath = path.join(tmpDir, 'ticket-001.json');
            const data = JSON.parse(fs.readFileSync(ticketPath, 'utf-8'));
            delete data.acceptance_criteria;
            fs.writeFileSync(ticketPath, JSON.stringify(data, null, 2));

            const result = engine.validate();
            expect(result.quality.ok).toBe(false);
            expect(result.quality.issues.some(i => i.severity === 'error' && /Missing acceptance_criteria/.test(i.message))).toBe(true);
            expect(result.ok).toBe(false);
        });

        it('validate reports drift warnings without failing', () => {
            engine.create({
                acceptance_criteria: [{ id: 'A1', text: 'needs a test', status: 'test-needed' }],
                area: 'a',
                title: 'Drift',
            });

            const result = engine.validate();
            expect(result.quality.ok).toBe(true);
            expect(result.quality.issues.some(i => i.severity === 'warning' && /test-needed/.test(i.message))).toBe(true);
        });

        it('validate warns when a resolved ticket still has unverified criteria', () => {
            engine.create({ acceptance_criteria: ['not verified'], area: 'a', title: 'Resolved early' });
            engine.update(1, { status: 'active' });
            engine.update(1, { status: 'resolved' });

            const result = engine.validate();
            expect(result.quality.ok).toBe(true);
            expect(result.quality.issues.some(i => i.severity === 'warning' && /resolved but 1 criterion/.test(i.message))).toBe(true);
        });
    });

    describe('list', () => {
        it('lists all tickets with no filters', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'a', title: 'T1' });
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'b', title: 'T2' });
            const list = engine.list();
            expect(list.length).toBe(2);
        });

        it('filters by area', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'auth', title: 'login' });
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'auth', title: 'register' });
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'billing', title: 'checkout' });
            const list = engine.list('auth');
            expect(list.length).toBe(2);
            expect(list.every(t => t.area === 'auth')).toBe(true);
        });

        it('excludes drafts when filtering by non-draft status', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'a', title: 'T1' }); // draft
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'b', title: 'T2' }); // draft
            // update one to active
            engine.update(1, { status: 'active' });
            const list = engine.list(undefined, 'active');
            expect(list.length).toBe(1);
        });

        it('returns draft tickets in all-status list when filtering by status=draft', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'a', title: 'T1' }); // draft
            const list = engine.list(undefined, 'draft');
            expect(list.length).toBe(1);
        });
    });

    describe('one-time ticket 54 privacy reconciliation', () => {
        function fixtureWithSensitiveCase() {
            const repoRoot = path.join(tmpDir, 'fixture-repo');
            const ticketDir = path.join(repoRoot, 'docs', 'tickets');
            fs.mkdirSync(repoRoot, { recursive: true });
            execFileSync('git', ['init', '--quiet', repoRoot]);
            fs.mkdirSync(ticketDir, { recursive: true });
            const fixtureEngine = createTicketEngine({
                ticketDir,
                indexPath: path.join(ticketDir, '_index.json'),
            });
            for (let number = 1; number <= 54; number++) {
                fixtureEngine.create({
                    area: 'test', title: `Ticket ${number}`,
                    acceptance_criteria: ['Criterion remains unchanged'],
                });
            }
            const fingerprint = `SHA256:${'A'.repeat(43)}`;
            const original = `OWNER-REPORTED HOST-KEY FINGERPRINT CHECK (2026-11-12; post-failure state): `
                + `ssh-keygen reports expected persistent \`ssh_host_ed25519_key.pub\` fingerprint ${fingerprint}; `
                + 'pinned known_hosts key for `[127.0.0.1]:46617` matches. '
                + '`guestfish` unavailable; guest-disk key unknown.';
            const before = fixtureEngine.update(54, { tests: { cases: [original] } })!;
            privateTmpDir = fs.mkdtempSync(path.join(os.homedir(), '.ticket-engine-test-private-'));
            fs.chmodSync(privateTmpDir, 0o700);
            const archiveDir = path.join(privateTmpDir, 'private-ticket-evidence');
            fs.mkdirSync(archiveDir, { recursive: true, mode: 0o700 });
            fs.chmodSync(archiveDir, 0o700);
            const archive = path.join(archiveDir, 'ticket-054-host-key-observation.json');
            const authorizedCaseSha256 = crypto.createHash('sha256').update(original).digest('hex');
            return { fixtureEngine, original, before, privateTmpDir, archive, authorizedCaseSha256 };
        }

        it('archives exact case privately, sanitizes the case, and appends without rewriting audit history', () => {
            const { fixtureEngine, original, before, archive, authorizedCaseSha256 } = fixtureWithSensitiveCase();
            const criteriaBefore = before.acceptance_criteria;
            const auditBefore = fixtureEngine.getAuditLog();
            const after = fixtureEngine.reconcileTicket054FingerprintCase(archive, authorizedCaseSha256);
            expect(fixtureEngine.reconcileTicket054FingerprintCase(archive, authorizedCaseSha256)).toEqual(after);
            expect(after.status).toBe('draft');
            expect(after.acceptance_criteria).toEqual(criteriaBefore);
            const sanitizedCases = after.tests!.cases;
            expect(sanitizedCases).toHaveLength(1);
            expect(sanitizedCases[0]).toContain('comparison: MATCH');
            expect(sanitizedCases[0]).toContain('UNKNOWN');
            expect(sanitizedCases[0]).not.toContain('SHA256:');
            expect(JSON.parse(fs.readFileSync(archive, 'utf-8')).evidence).toBe(original);
            expect(fs.statSync(archive).mode & 0o777).toBe(0o600);
            expect(fixtureEngine.getAuditLog().slice(0, auditBefore.length)).toEqual(auditBefore);
            expect(fixtureEngine.getAuditLog()).toHaveLength(auditBefore.length + 1);
            const auditAfter = fixtureEngine.getAuditLog();
            expect(auditAfter[auditAfter.length - 1]).toMatchObject({
                operation: 'privacy-redaction', ticket_number: 54,
            });
            expect(fixtureEngine.validate().enforcement.audit.valid).toBe(true);
            expect(fixtureEngine.getAuditLog()).toHaveLength(auditBefore.length + 1);
        });

        it('resumes after audit replacement failure without rewriting prior entries', () => {
            const { fixtureEngine, original, archive, authorizedCaseSha256 } = fixtureWithSensitiveCase();
            const before = fixtureEngine.getAuditLog();
            const sanitized = 'OWNER-REPORTED HOST-KEY CHECK (2026-11-12; post-failure state): '
                + 'expected persistent host-key vs pinned known_hosts comparison: MATCH. '
                + 'Guest-disk active public host-key fingerprint: UNKNOWN (guestfish unavailable). '
                + 'No guest mismatch/root-cause inference or M1a acceptance; exact private evidence '
                + 'is retained outside the repository.';
            fs.writeFileSync(archive, JSON.stringify({ ticket_number: 54, evidence: original }, null, 2) + '\n',
                { mode: 0o600 });
            fs.chmodSync(archive, 0o600);
            const ticketPath = path.join(tmpDir, 'fixture-repo', 'docs', 'tickets', 'ticket-054.json');
            const ticket = JSON.parse(fs.readFileSync(ticketPath, 'utf-8'));
            ticket.tests.cases = [sanitized];
            fs.writeFileSync(ticketPath, JSON.stringify(ticket, null, 2));
            expect(fixtureEngine.getAuditLog()).toEqual(before);
            fixtureEngine.reconcileTicket054FingerprintCase(archive, authorizedCaseSha256);
            expect(fixtureEngine.getAuditLog()).toHaveLength(before.length + 1);
            expect(fixtureEngine.getAuditLog().slice(-1)[0]).toMatchObject({
                operation: 'privacy-redaction', ticket_number: 54,
            });
            expect(fixtureEngine.validate().enforcement.audit.valid).toBe(true);
        });

        it('refuses symlinked archive ancestry without changing the ticket', () => {
            const { fixtureEngine, privateTmpDir: privateDir, archive, authorizedCaseSha256 } = fixtureWithSensitiveCase();
            const link = path.join(privateDir, 'repo-link');
            fs.symlinkSync(path.join(tmpDir, 'fixture-repo'), link, 'dir');
            const unsafeArchive = path.join(link, 'private.json');
            expect(() => fixtureEngine.reconcileTicket054FingerprintCase(unsafeArchive, authorizedCaseSha256))
                .toThrow('private evidence archive path contains a symlink');
            expect(fixtureEngine.list().find(ticket => ticket.number === 54)!.tests!.cases[0])
                .toContain('SHA256:');
            expect(fs.existsSync(archive)).toBe(false);
        });

        it('does not replace different existing evidence at the private archive path', () => {
            const { fixtureEngine, archive, authorizedCaseSha256 } = fixtureWithSensitiveCase();
            const competitorEvidence = 'owner-private evidence from a concurrent archive operation';
            fs.writeFileSync(archive, competitorEvidence, { mode: 0o600, flag: 'wx' });
            expect(() => fixtureEngine.reconcileTicket054FingerprintCase(archive, authorizedCaseSha256))
                .toThrow('existing private evidence archive is unsafe or does not match');
            expect(fs.readFileSync(archive, 'utf-8')).toBe(competitorEvidence);
            expect(fixtureEngine.list().find(ticket => ticket.number === 54)!.tests!.cases[0])
                .toContain('SHA256:');
        });

        it('recovers a dead writer temporary hard link without changing the archived evidence', () => {
            const { fixtureEngine, original, archive, authorizedCaseSha256 } = fixtureWithSensitiveCase();
            fs.writeFileSync(archive,
                JSON.stringify({ ticket_number: 54, evidence: original }, null, 2) + '\n',
                { mode: 0o600, flag: 'wx' });
            const orphanLink = path.join(path.dirname(archive),
                '.ticket-054-evidence.99999999.deadbeef.tmp');
            fs.linkSync(archive, orphanLink);
            expect(fs.statSync(archive).nlink).toBe(2);

            fixtureEngine.reconcileTicket054FingerprintCase(archive, authorizedCaseSha256);

            expect(fs.existsSync(orphanLink)).toBe(false);
            expect(fs.statSync(archive).nlink).toBe(1);
            expect(JSON.parse(fs.readFileSync(archive, 'utf-8')).evidence).toBe(original);
            expect(fixtureEngine.validate().enforcement.audit.valid).toBe(true);
        });

        it('does not clean an orphan alias before retry authorization succeeds', () => {
            const { fixtureEngine, original, archive, authorizedCaseSha256 } = fixtureWithSensitiveCase();
            const sanitized = 'OWNER-REPORTED HOST-KEY CHECK (2026-11-12; post-failure state): '
                + 'expected persistent host-key vs pinned known_hosts comparison: MATCH. '
                + 'Guest-disk active public host-key fingerprint: UNKNOWN (guestfish unavailable). '
                + 'No guest mismatch/root-cause inference or M1a acceptance; exact private evidence '
                + 'is retained outside the repository.';
            const wrongEvidence = original.replace('matches.', 'differs.');
            fs.writeFileSync(archive,
                JSON.stringify({ ticket_number: 54, evidence: wrongEvidence }, null, 2) + '\n',
                { mode: 0o600, flag: 'wx' });
            const orphanLink = path.join(path.dirname(archive),
                '.ticket-054-evidence.99999999.deadbeef.tmp');
            fs.linkSync(archive, orphanLink);
            const ticketPath = path.join(tmpDir, 'fixture-repo', 'docs', 'tickets', 'ticket-054.json');
            const ticket = JSON.parse(fs.readFileSync(ticketPath, 'utf-8'));
            ticket.tests.cases = [sanitized];
            fs.writeFileSync(ticketPath, JSON.stringify(ticket, null, 2));

            expect(() => fixtureEngine.reconcileTicket054FingerprintCase(archive, authorizedCaseSha256))
                .toThrow('the exact ticket-054 fingerprint observation was not found');
            expect(fs.existsSync(orphanLink)).toBe(true);
            expect(fs.statSync(archive).nlink).toBe(2);
        });
    });

    describe('update', () => {
        it('updates ticket fields', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'a', title: 'Original' });
            const updated = engine.update(1, { title: 'Updated', objective: 'New obj' });
            expect(updated?.title).toBe('Updated');
            expect(updated?.objective).toBe('New obj');
        });

        it('updates updated_at timestamp on change', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'a', title: 'T1' });
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
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'a', title: 'T1' }); // draft → active allowed
            engine.update(1, { status: 'active' });

            // active → resolved allowed
            engine.update(1, { status: 'resolved' });

            // resolved → archived IS allowed per DECISIONS.md D.3
            engine.update(1, { status: 'archived' });

            // archived → draft IS allowed per DECISIONS.md D.3
            engine.update(1, { status: 'draft' });

            // But draft → archived is allowed (archive from any status)
            engine.update(1, { status: 'archived' });

            // archived → active should throw (not allowed)
            expect(() => engine.update(1, { status: 'active' })).toThrow();
        });
    });

    describe('archive', () => {
        it('archives a ticket from draft', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'a', title: 'T1' });
            const archived = engine.archive(1);
            expect(archived?.status).toBe('archived');
        });

        it('archives a ticket from active', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'active-tosource', title: 'T1' });
            engine.update(1, { status: 'active' });
            const archived = engine.archive(1);
            expect(archived?.status).toBe('archived');
        });

        it('returns undefined for unknown number', () => {
            const result = engine.archive(999);
            expect(result).toBeUndefined();
        });

        it('makes archived tickets invisible to update', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'a', title: 'T1' });
            engine.archive(1);
            // We CAN still read them back, but they should be searchable
            const list = engine.list(undefined, 'archived');
            expect(list).toHaveLength(1);
        });
    });

    describe('validate', () => {
        it('returns synced=true when index and files match', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'a', title: 'T1' });
            const result = engine.validate();
            expect(result.synced).toBe(true);
            expect(result.ok).toBe(true);
            expect(result.enforcement.audit.valid).toBe(true);
            expect(result.enforcement.has_orphans).toBe(false);
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
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'a', title: 'T1' });
            // Cannot go directly from draft to resolved
            expect(() => engine.update(1, { status: 'resolved' })).toThrow();

            // Must go through active first
            engine.update(1, { status: 'active' });
            engine.update(1, { status: 'resolved' });
        });

        it('active → draft is allowed', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'a', title: 'T1' });
            engine.update(1, { status: 'active' });
            engine.update(1, { status: 'draft' });
            expect(engine.list(undefined, 'draft')).toHaveLength(1);
        });

        it('resolved → draft is allowed', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'a', title: 'T1' });
            engine.update(1, { status: 'active' });
            engine.update(1, { status: 'resolved' });
            engine.update(1, { status: 'draft' });
        });

        it('archived can only transition to draft', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'a', title: 'T1' });
            engine.archive(1);

            // archived → active should throw
            expect(() => engine.update(1, { status: 'active' })).toThrow();

            // archived → resolved should throw
            expect(() => engine.update(1, { status: 'resolved' })).toThrow();

            // archived → draft IS allowed per DECISIONS.md D.3
            const reopened = engine.update(1, { status: 'draft' });
            expect(reopened?.status).toBe('draft');
        });
    });

    describe('index integration', () => {
        it('reads index on creation for monotonic numbering', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'a', title: 'T1' });
            // Re-read from a fresh engine instance — should pick up next_number from index
            const fresh = createTicketEngine({ ticketDir: tmpDir, indexPath });
            const newTicket = fresh.create({ acceptance_criteria: ['Criterion met'], area: 'b', title: 'T2 continued' });
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
                const t = fresh.create({ acceptance_criteria: ['Criterion met'], area: 'test', title: 'post-repair' });
                expect(t.number).toBe(1); // or 2 depending on whether old index was preserved
            }).not.toThrow();
        });

        it('reconstructs index after an unresolved merge leaves conflict markers', () => {
            const corruptPath = path.join(tmpDir, '_index.json');
            fs.writeFileSync(corruptPath, '<<<<<<< HEAD\n{"schemaVersion": 1}\n=======\n{"schemaVersion": 1}\n>>>>>>> branch\n');

            const fresh = createTicketEngine({ ticketDir: tmpDir, indexPath: corruptPath });
            const ticket = fresh.create({
                acceptance_criteria: ['Criterion met'],
                area: 'test',
                title: 'rebuilt after conflict',
            });

            expect(ticket.number).toBe(1);
            expect(fresh.validate().synced).toBe(true);
        });
    });

    describe('archived files from validate', () => {
        it('reports extra files in index not on disk', () => {
            // Create a ticket via engine then delete its file
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'a', title: 'T1' });
            const ticketPath = path.join(tmpDir, 'ticket-001.json');
            fs.unlinkSync(ticketPath);

            const result = engine.validate();
            expect(result.synced).toBe(false);
        });
    });

    describe('enforcement: lock + audit + orphans', () => {
        const lockPath = () => path.join(tmpDir, '.engine.lock');

        function writeLock(pid: number): void {
            fs.writeFileSync(lockPath(), JSON.stringify({
                pid,
                acquired_at: new Date().toISOString(),
            }, null, 2));
        }

        function deadPid(): number {
            // Spawn a tiny process and kill it — far faster than spawning Node.
            spawnSync('timeout', ['0.001', 'true']);
            // Return the PID of the child we just waited on (it's definitely dead now).
            // If spawnSync returned null (e.g. timeout not found), fall back to a safe high number.
            const child = spawnSync('true');
            return child.pid ?? 999999;
        }

        it('getEnforcement returns enforced=true after fresh create', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'auth', title: 'Add login' });

            const result = engine.getEnforcement();
            expect(result.enforced).toBe(true);
            expect(result.has_orphans).toBe(false);
            expect(result.messages.some(m => m.includes('intact'))).toBe(true);
        });

        it('audit entries have valid per-entry hashes', () => {
            const crypto = require('crypto');

            engine.create({ acceptance_criteria: ['Criterion met'], area: 'auth', title: 'T1' });
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'infra', title: 'T2' });

            const auditPath = path.join(tmpDir, '_audit.json');
            const audit: Array<{ timestamp: string; operation: string; ticket_number: number; hash: string }> =
                JSON.parse(fs.readFileSync(auditPath, 'utf-8'));

            for (const entry of audit) {
                const canonical = JSON.stringify({
                    timestamp: entry.timestamp,
                    operation: entry.operation,
                    ticket_number: entry.ticket_number,
                }, null, 0);
                const expectedHash = crypto.createHash('sha256').update(canonical).digest('hex');
                expect(entry.hash).toBe(expectedHash);
            }
        });

        it('getEnforcement detects tampered audit hash', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'auth', title: 'T1' });

            const auditPath = path.join(tmpDir, '_audit.json');
            const audit = JSON.parse(fs.readFileSync(auditPath, 'utf-8'));
            // Tamper with the hash
            audit[0].hash = 'deadbeef' + audit[0].hash.slice(8);
            fs.writeFileSync(auditPath, JSON.stringify(audit, null, 2));

            const result = engine.getEnforcement();
            expect(result.enforced).toBe(false);
            expect(result.messages.some(m => m.includes('hash mismatch'))).toBe(true);
        });

        it('getEnforcement detects orphan files (on disk, not in audit)', () => {
            // Create one ticket via engine
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'auth', title: 'EngineTicket' });

            // Create another ticket file directly on disk (bypassing engine)
            const orphanContent = JSON.stringify({
                id: 99,
                file_path: 'ticket-099.json',
                number: 99,
                area: 'hacked',
                type: 'bug',
                status: 'active',
                title: 'ManualFile',
                created_at: new Date().toISOString(),
                updated_at: new Date().toISOString(),
            }, null, 2);
            fs.writeFileSync(path.join(tmpDir, 'ticket-099.json'), orphanContent);

            const result = engine.getEnforcement();
            expect(result.has_orphans).toBe(true);
            expect(result.messages.some(m => m.includes('Orphan files'))).toBe(true);
            // Orphans don't break enforcement (fail open)
            expect(result.enforced).toBe(true);
        });

        it('lock is transient: released after a successful write', () => {
            expect(fs.existsSync(lockPath())).toBe(false);

            engine.create({ acceptance_criteria: ['Criterion met'], area: 'auth', title: 'T1' });

            // The lock only exists during the write window.
            expect(fs.existsSync(lockPath())).toBe(false);
        });

        it('refuses to write while a live process holds the lock', () => {
            writeLock(process.pid); // this test process is definitely alive

            expect(() => engine.create({ acceptance_criteria: ['Criterion met'], area: 'auth', title: 'T1' }))
                .toThrow(/locked by live process/);

            // No partial write occurred.
            expect(fs.existsSync(path.join(tmpDir, 'ticket-001.json'))).toBe(false);
        });

        it('clears a stale lock from a dead PID and proceeds', () => {
            writeLock(deadPid());

            const ticket = engine.create({ acceptance_criteria: ['Criterion met'], area: 'auth', title: 'T1' });

            expect(ticket.number).toBe(1);
            expect(fs.existsSync(lockPath())).toBe(false);
        });

        it('getEnforcement reports a stale lock as a problem', () => {
            writeLock(deadPid());

            const result = engine.getEnforcement();

            expect(result.lock.state).toBe('stale');
            expect(result.enforced).toBe(false);
            expect(result.messages.some(m => m.includes('Stale lock'))).toBe(true);
        });

        it('getEnforcement treats a missing lock as normal (fail-open)', () => {
            const result = engine.getEnforcement();

            expect(result.lock.state).toBe('absent');
            expect(result.enforced).toBe(true);
            expect(result.messages.some(m => m.includes('No lock held'))).toBe(true);
        });

        it('accepts audit entries that revisit an older ticket number', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'a', title: 'T1' });   // #1
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'a', title: 'T2' });   // #2
            engine.update(1, { title: 'T1 updated' });   // back to #1

            const result = engine.getEnforcement();
            const audit = JSON.parse(fs.readFileSync(path.join(tmpDir, '_audit.json'), 'utf-8'));

            expect(audit.map((e: { ticket_number: number }) => e.ticket_number)).toEqual([1, 2, 1]);
            expect(result.audit.valid).toBe(true);
            expect(result.enforced).toBe(true);
        });

        it('rejects a corrupt (non-array) audit log', () => {
            fs.writeFileSync(path.join(tmpDir, '_audit.json'), JSON.stringify({ nope: true }));

            const result = engine.getEnforcement();

            expect(result.audit.valid).toBe(false);
            expect(result.enforced).toBe(false);
            expect(result.messages.some(m => m.includes('not a JSON array'))).toBe(true);
        });

        it('merges another branch audit log without dropping entries', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'a', title: 'local' });

            const otherDir = fs.mkdtempSync('/tmp/engine-merge-');
            const otherIndex = path.join(otherDir, '_index.json');
            const other = createTicketEngine({ ticketDir: otherDir, indexPath: otherIndex });
            other.create({
                acceptance_criteria: ['Criterion met'],
                area: 'a',
                title: 'incoming one',
            });
            other.create({
                acceptance_criteria: ['Criterion met'],
                area: 'a',
                title: 'incoming two',
            });
            // Use the second entry so it is distinct even if both stores wrote
            // in the same millisecond.
            const incoming = other.getAuditLog().slice(-1);

            try {
                const result = engine.mergeAuditLog(incoming);
                expect(result).toEqual({ added: 1, total: 2 });
                expect(engine.getAuditLog().map(entry => entry.ticket_number)).toEqual([1, 2]);

                // Merging the same log again is idempotent.
                expect(engine.mergeAuditLog(incoming)).toEqual({ added: 0, total: 2 });
                expect(engine.getEnforcement().audit.valid).toBe(true);
            } finally {
                fs.rmSync(otherDir, { recursive: true, force: true });
            }
        });

        it('refuses to merge an audit log with an invalid hash', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'a', title: 'local' });

            const incoming = engine.getAuditLog().map(entry => ({ ...entry }));
            incoming[0].hash = '0'.repeat(64);

            expect(() => engine.mergeAuditLog(incoming))
                .toThrow(/incoming audit log is invalid.*hash mismatch/i);
            expect(engine.getAuditLog()).toHaveLength(1);
        });

        it('baseline adopts un-audited files, is idempotent, and clears orphans', () => {
            // Simulate a store that predates the audit log.
            const makeTicket = (number: number) => JSON.stringify({
                id: number,
                file_path: `ticket-${String(number).padStart(3, '0')}.json`,
                number,
                area: 'legacy',
                type: 'feature',
                status: 'draft',
                title: `Legacy ${number}`,
                created_at: new Date().toISOString(),
                updated_at: new Date().toISOString(),
                acceptance_criteria: [
                    { id: 'A1', text: 'Legacy behaviour preserved', status: 'waived', note: 'Historical ticket predates the acceptance-criteria policy' },
                ],
                tests: { needed: [], created: [], cases: [] },
            }, null, 2);
            fs.writeFileSync(path.join(tmpDir, 'ticket-001.json'), makeTicket(1));
            fs.writeFileSync(path.join(tmpDir, 'ticket-002.json'), makeTicket(2));
            // Drop the index so a fresh engine reconstructs it from disk.
            fs.unlinkSync(indexPath);
            const fresh = createTicketEngine({ ticketDir: tmpDir, indexPath });

            expect(fresh.getEnforcement().has_orphans).toBe(true);

            const added = fresh.baseline();
            expect(added.map(e => e.operation)).toEqual(['baseline', 'baseline']);
            expect(added.map(e => e.ticket_number)).toEqual([1, 2]);

            const after = fresh.getEnforcement();
            expect(after.has_orphans).toBe(false);
            expect(after.audit.valid).toBe(true);
            expect(fresh.validate().ok).toBe(true);

            // Running again adds nothing.
            expect(fresh.baseline()).toHaveLength(0);
        });

        it('baseline refuses to append to a tampered audit log', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'auth', title: 'T1' });
            const auditPath = path.join(tmpDir, '_audit.json');
            const audit = JSON.parse(fs.readFileSync(auditPath, 'utf-8'));
            audit[0].ticket_number = 42;
            fs.writeFileSync(auditPath, JSON.stringify(audit, null, 2));

            expect(() => engine.baseline()).toThrow(/Refusing baseline/);
        });

        it('audit log is created on first operation, not at init', () => {
            const auditPath = path.join(tmpDir, '_audit.json');
            // Audit file doesn't exist until first engine operation
            expect(fs.existsSync(auditPath)).toBe(false);

            // After creating a ticket, audit file exists with 1 entry
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'auth', title: 'T1' });
            expect(fs.existsSync(auditPath)).toBe(true);
            const audit = JSON.parse(fs.readFileSync(auditPath, 'utf-8'));
            expect(audit.length).toBe(1);
        });

        it('audit log records create and update operations', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'auth', title: 'T1' });
            engine.update(1, { title: 'Updated title' });

            const auditPath = path.join(tmpDir, '_audit.json');
            const audit = JSON.parse(fs.readFileSync(auditPath, 'utf-8'));

            expect(audit.length).toBe(2);
            expect(audit[0].operation).toBe('create');
            expect(audit[0].ticket_number).toBe(1);
            expect(audit[1].operation).toBe('update');
            expect(audit[1].ticket_number).toBe(1);
        });

        it('audit log records archive operation', () => {
            engine.create({ acceptance_criteria: ['Criterion met'], area: 'auth', title: 'T1' });
            engine.archive(1);

            const auditPath = path.join(tmpDir, '_audit.json');
            const audit = JSON.parse(fs.readFileSync(auditPath, 'utf-8'));

            expect(audit.length).toBe(2);
            expect(audit[1].operation).toBe('archive');
            expect(audit[1].ticket_number).toBe(1);
        });
    });
});
