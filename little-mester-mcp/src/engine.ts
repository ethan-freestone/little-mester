import * as fs from 'fs';
import * as path from 'path';
import { readIndex, writeIndex, reconstructIndex } from './index';

const VALID_TYPES = new Set(['spike', 'feature', 'bug', 'improvement', 'doc', 'infra']);
const STATUS_TRANSITIONS: Record<string, string[]> = {
    draft: ['active', 'archived'],
    active: ['resolved', 'draft'],
    resolved: ['draft', 'archived'],
    archived: ['draft'],
};

// ── Structural enforcement: lock + audit trail ───────────────────
//
// INTENT: Every create/update/archive MUST go through the engine.
//
// This is DETECTION, not prevention. A human or agent with file write
// access can bypass it. The goal is to make a bypass obvious during
// `validate` before index/audit drift spreads.
//
// How it works:
//   1. Lock file (.engine.lock) — exists ONLY while a write is running.
//      It records the owning PID. A live PID blocks other writers; a
//      dead PID means the previous writer crashed, so the lock is stale
//      and is cleared automatically. No lock present is the normal state.
//   2. Audit log (_audit.json) — append-only JSON array, one lean entry
//      per engine write. Each entry has a SHA-256 hash of its own
//      timestamp+operation+ticket_number. If those fields are edited by
//      hand, the hash no longer matches. We verify on every validate().
//   3. Orphan detection — ticket files on disk with no audit entry.
//      These are flagged for human review; never auto-deleted or
//      auto-adopted (fail-open). `audit --baseline` is the one explicit
//      adoption path.
//
// Why not a hash chain? A chain (each entry hashes the previous) is
// unnecessary overhead for a single-user local tool. One hash per entry
// is enough to detect manual edits. Lock + orphan detection + per-entry
// hashes together make engine bypasses visible.
//
// Why not signatures? We don't need to stop a motivated insider who
// already has file write access. We need accidental or convenient edits
// to be *discoverable*.
//
// INTENT (continued): Keep it lean. Each audit entry is 3 fields + 1 hash.
// No param snapshots, no merkle trees, no genesis blocks. The exact
// invariants are documented in docs/tickets/architecture/DECISIONS.md §I.

import * as crypto from 'crypto';

const LOCK_FILENAME = '.engine.lock';
const AUDIT_FILENAME = '_audit.json';

export type AuditOperation = 'create' | 'update' | 'archive' | 'baseline';

const AUDIT_OPERATIONS = new Set<AuditOperation>(['create', 'update', 'archive', 'baseline']);

/** A lean audit entry — one line per engine write, with per-entry hash. */
export interface AuditEntry {
    /** ISO-8601 timestamp */
    timestamp: string;
    /** Operation performed ('baseline' = one-time adoption of a pre-existing file) */
    operation: AuditOperation;
    /** Ticket number affected */
    ticket_number: number;
    /** SHA-256 of canonical JSON of timestamp+operation+ticket_number.
     *  Verifies the entry wasn't manually edited after creation. */
    hash: string;
}

export type LockState = 'absent' | 'held' | 'stale';

export interface LockStatus {
    state: LockState;
    pid?: number;
    acquired_at?: string;
    message: string;
}

export interface AuditStatus {
    valid: boolean;
    entries: number;
    messages: string[];
}

export interface EnforcementResult {
    /** true = audit hashes are valid and the lock is not stale.
     *  Orphans do NOT make this false: enforcement is fail-open. */
    enforced: boolean;
    /** true = ticket files on disk with no audit entry — flagged for human review */
    has_orphans: boolean;
    /** Absolute paths of orphan files */
    orphan_files: string[];
    lock: LockStatus;
    audit: AuditStatus;
    messages: string[];
}

export interface QualityIssue {
    ticket_number: number;
    field: 'acceptance_criteria' | 'tests';
    severity: 'error' | 'warning';
    message: string;
}

export interface QualityResult {
    /** true when there are no policy errors. Warnings do not fail the gate. */
    ok: boolean;
    tickets_checked: number;
    issues: QualityIssue[];
}

export interface ValidateResult {
    synced: boolean;
    missing_files?: string[];
    extra_files?: string[];
    enforcement: EnforcementResult;
    quality: QualityResult;
    /** true only when index is synced, enforcement is valid, no orphans exist,
     *  and every ticket passes the acceptance-criteria/test policy.
     *  The CLI exits 1 when this is false. */
    ok: boolean;
}

// ── Lock management ───────────────────────────────────────────────
// The lock is TRANSIENT: acquired immediately before a write, released
// in a finally block. If the process dies mid-write, the next writer
// sees a dead PID and clears the stale lock.

/** True if a process with this PID exists (EPERM also means it exists). */
function isPidAlive(pid: number): boolean {
    try {
        process.kill(pid, 0);
        return true;
    } catch (err) {
        return (err as NodeJS.ErrnoException).code === 'EPERM';
    }
}

function readLockFile(lockPath: string): { pid?: number; acquired_at?: string } | null {
    try {
        return JSON.parse(fs.readFileSync(lockPath, 'utf-8'));
    } catch {
        return null;
    }
}

function acquireLock(ticketDir: string): void {
    const lockPath = path.join(ticketDir, LOCK_FILENAME);
    const payload = JSON.stringify({
        pid: process.pid,
        acquired_at: new Date().toISOString(),
    }, null, 2);

    // Two attempts: 'wx' fails if another writer won the race. On EEXIST we
    // either refuse (live owner) or clear the stale/corrupt file and retry.
    for (let attempt = 0; attempt < 2; attempt++) {
        try {
            fs.writeFileSync(lockPath, payload, { encoding: 'utf-8', flag: 'wx' });
            return;
        } catch (err) {
            if ((err as NodeJS.ErrnoException).code !== 'EEXIST') throw err;

            const existing = readLockFile(lockPath);
            if (existing && typeof existing.pid === 'number' && isPidAlive(existing.pid)) {
                throw new Error(
                    `Ticket directory is locked by live process PID ${existing.pid}` +
                    (existing.acquired_at ? ` (acquired ${existing.acquired_at})` : '')
                );
            }

            // Dead PID or corrupt lock: clear it and retry once.
            try {
                fs.unlinkSync(lockPath);
            } catch (unlinkErr) {
                if ((unlinkErr as NodeJS.ErrnoException).code !== 'ENOENT') throw unlinkErr;
            }
        }
    }

    throw new Error('Could not acquire the ticket engine lock (unexpected contention)');
}

function releaseLock(ticketDir: string): void {
    const lockPath = path.join(ticketDir, LOCK_FILENAME);
    const existing = readLockFile(lockPath);
    // Never remove a lock owned by another live writer.
    if (!existing || existing.pid !== process.pid) return;
    try {
        fs.unlinkSync(lockPath);
    } catch (err) {
        if ((err as NodeJS.ErrnoException).code !== 'ENOENT') throw err;
    }
}

/** Run `fn` inside the write lock, always releasing it afterwards. */
function withLock<T>(ticketDir: string, fn: () => T): T {
    acquireLock(ticketDir);
    try {
        return fn();
    } finally {
        releaseLock(ticketDir);
    }
}

function inspectLock(ticketDir: string): LockStatus {
    const lockPath = path.join(ticketDir, LOCK_FILENAME);
    if (!fs.existsSync(lockPath)) {
        return {
            state: 'absent',
            message: 'No lock held (normal — the lock only exists during writes)',
        };
    }

    const data = readLockFile(lockPath);
    if (!data || typeof data.pid !== 'number') {
        return {
            state: 'stale',
            message: 'Stale/corrupt lock file (no valid PID) — safe to clear',
        };
    }

    if (isPidAlive(data.pid)) {
        return {
            state: 'held',
            pid: data.pid,
            acquired_at: data.acquired_at,
            message: `Write in progress by live PID ${data.pid}` +
                (data.acquired_at ? ` (since ${data.acquired_at})` : ''),
        };
    }

    return {
        state: 'stale',
        pid: data.pid,
        acquired_at: data.acquired_at,
        message: `Stale lock from dead PID ${data.pid}` +
            (data.acquired_at ? ` (acquired ${data.acquired_at})` : '') +
            ' — safe to clear',
    };
}

// ── Audit log management ──────────────────────────────────────────

/** Read the audit log. Missing file = empty log; corrupt file throws. */
function readAuditLog(ticketDir: string): AuditEntry[] {
    const auditPath = path.join(ticketDir, AUDIT_FILENAME);
    if (!fs.existsSync(auditPath)) return [];
    const parsed = JSON.parse(fs.readFileSync(auditPath, 'utf-8'));
    if (!Array.isArray(parsed)) {
        throw new Error('Audit log is not a JSON array');
    }
    return parsed as AuditEntry[];
}

/** Read the audit log, treating any problem as an empty log (for orphan detection). */
function tryReadAuditLog(ticketDir: string): AuditEntry[] {
    try {
        return readAuditLog(ticketDir);
    } catch {
        return [];
    }
}

function writeAuditLog(ticketDir: string, entries: AuditEntry[]): void {
    const auditPath = path.join(ticketDir, AUDIT_FILENAME);
    fs.writeFileSync(auditPath, JSON.stringify(entries, null, 2), 'utf-8');
}

/** Compute SHA-256 of the canonical JSON (no whitespace) of an entry's non-hash fields. */
function entryHash(entry: Omit<AuditEntry, 'hash'>): string {
    const { timestamp, operation, ticket_number } = entry;
    // Key order is part of the contract: timestamp, operation, ticket_number.
    return crypto.createHash('sha256')
        .update(JSON.stringify({ timestamp, operation, ticket_number }))
        .digest('hex');
}

function makeAuditEntry(operation: AuditOperation, ticketNumber: number, timestamp?: string): AuditEntry {
    const base: Omit<AuditEntry, 'hash'> = {
        timestamp: timestamp ?? new Date().toISOString(),
        operation,
        ticket_number: ticketNumber,
    };
    return { ...base, hash: entryHash(base) };
}

function appendAuditEntry(
    ticketDir: string,
    entry: { operation: Exclude<AuditOperation, 'baseline'>; ticket_number: number }
): void {
    const entries = readAuditLog(ticketDir);
    entries.push(makeAuditEntry(entry.operation, entry.ticket_number));
    writeAuditLog(ticketDir, entries);
}

/** Verify all audit entries have valid hashes and non-decreasing timestamps. */
function verifyAuditEntries(entries: AuditEntry[]): AuditStatus {
    if (entries.length === 0) {
        return { valid: true, entries: 0, messages: ['Audit log empty — no write operations recorded yet'] };
    }

    for (let i = 0; i < entries.length; i++) {
        const entry = entries[i];
        const where = `entry #${i + 1}`;

        if (!entry || typeof entry !== 'object') {
            return { valid: false, entries: entries.length, messages: [`Audit ${where} is not an object`] };
        }
        if (typeof entry.timestamp !== 'string' || Number.isNaN(Date.parse(entry.timestamp))) {
            return { valid: false, entries: entries.length, messages: [`Audit ${where} has an invalid timestamp`] };
        }
        if (!AUDIT_OPERATIONS.has(entry.operation)) {
            return { valid: false, entries: entries.length, messages: [`Audit ${where} has an invalid operation '${entry.operation}'`] };
        }
        if (!Number.isInteger(entry.ticket_number) || entry.ticket_number <= 0) {
            return { valid: false, entries: entries.length, messages: [`Audit ${where} has an invalid ticket number`] };
        }
        if (typeof entry.hash !== 'string' || !/^[0-9a-f]{64}$/.test(entry.hash)) {
            return { valid: false, entries: entries.length, messages: [`Audit ${where} has a malformed hash`] };
        }

        const expected = entryHash({
            timestamp: entry.timestamp,
            operation: entry.operation,
            ticket_number: entry.ticket_number,
        });
        if (entry.hash !== expected) {
            return {
                valid: false,
                entries: entries.length,
                messages: [`Audit hash mismatch at ${where} — entry was manually edited`],
            };
        }

        // Ticket numbers may go backwards (update an older ticket), so order
        // is checked by timestamp only.
        if (i > 0 && Date.parse(entry.timestamp) < Date.parse(entries[i - 1].timestamp)) {
            return { valid: false, entries: entries.length, messages: [`Audit ${where} is older than the previous entry`] };
        }
    }

    return { valid: true, entries: entries.length, messages: [`Audit log intact (${entries.length} entries)`] };
}

function verifyAuditLog(ticketDir: string): AuditStatus {
    let entries: AuditEntry[];
    try {
        entries = readAuditLog(ticketDir);
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return { valid: false, entries: 0, messages: [`Audit log unreadable/corrupt: ${msg}`] };
    }
    return verifyAuditEntries(entries);
}

// ── Public types ──────────────────────────────────────────────────

/**
 * Acceptance-criterion lifecycle. The status is the single place to track
 * whether a criterion is implemented and whether it has a test, because
 * "tests needed / not needed / created / passing" are all states of the
 * same acceptance criterion.
 *
 *   pending      — not started / nothing verified yet
 *   in-progress  — implementation underway
 *   test-needed  — implemented, verification still required
 *   test-created — a test exists, but has not been confirmed passing
 *   passing      — verified; `evidence` is required
 *   waived       — no test needed/possible; `note` is required
 */
export const ACCEPTANCE_CRITERIA_STATUSES = [
    'pending',
    'in-progress',
    'test-needed',
    'test-created',
    'passing',
    'waived',
] as const;

export type AcceptanceCriterionStatus = typeof ACCEPTANCE_CRITERIA_STATUSES[number];

const AC_STATUS_SET = new Set<string>(ACCEPTANCE_CRITERIA_STATUSES);
const AC_ID_PATTERN = /^[A-Za-z][A-Za-z0-9_-]{0,15}$/;

export interface AcceptanceCriterion {
    /** Stable short id (A1, A2, … or another identifier). Unique per ticket. */
    id: string;
    text: string;
    status: AcceptanceCriterionStatus;
    /** Required when status = 'passing'. Test path/case, command + result, or artifact. */
    evidence?: string;
    /** Required when status = 'waived'. Why no test is needed/possible. */
    note?: string;
}

/** Accepted input for create/update. Bare strings get an auto id and `pending`. */
export interface AcceptanceCriterionInput {
    id?: string;
    text: string;
    status?: AcceptanceCriterionStatus | string;
    evidence?: string;
    note?: string;
}

export type AcceptanceCriteriaInput = Array<string | AcceptanceCriterionInput>;

/**
 * The ticket-level test ledger. `needed` and `created` are the paper trail;
 * `cases` names the concrete cases (automated or observed manually).
 *
 * On update, the three arrays are merged with the existing values rather
 * than replaced, so a later edit cannot silently erase the trail.
 */
export interface TicketTests {
    needed: string[];
    created: string[];
    cases: string[];
}

export type TicketTestsInput = Partial<TicketTests> | string[] | null;

export interface CreateTicketParams {
    area?: string;
    type?: string;
    title: string;
    objective?: string;
    /** At least one is required on create. */
    acceptance_criteria: AcceptanceCriteriaInput;
    tests?: TicketTestsInput;
    // Model tracking fields
    suggested_model?: string;
    estimated_models?: { model: string; subtask_hint: string }[];
    estimated_model?: string;
    // Other extended fields
    estimate?: number;
    priority?: 'trivial' | 'minor' | 'major' | 'critical';
    labels?: string[];
    parent?: number;
    is_epic?: boolean;
    assigned_to?: string;
    spans?: { start: string; end: string };
    comments?: { timestamp: string; author: string; body: string }[];
}

export type TicketPatch =
    Partial<Omit<Ticket, 'id' | 'file_path' | 'number' | 'acceptance_criteria' | 'tests'>> & {
        /** Replaces the acceptance-criteria array. Use the CLI id flags to patch in place. */
        acceptance_criteria?: AcceptanceCriteriaInput;
        /** Arrays are merged with the existing ledger on update. */
        tests?: TicketTestsInput;
    };

export interface Ticket {
    id?: number;
    file_path: string;
    number: number;
    area: string;
    type: 'spike' | 'feature' | 'bug' | 'improvement' | 'doc' | 'infra';
    status: 'draft' | 'active' | 'resolved' | 'archived';
    title?: string;
    objective?: string;
    created_at?: string;
    updated_at?: string;
    priority?: 'trivial' | 'minor' | 'major' | 'critical';
    /** Every ticket must carry at least one criterion. */
    acceptance_criteria?: AcceptanceCriterion[];
    /** Every ticket must carry the test ledger (arrays may be empty). */
    tests?: TicketTests;
    // Model tracking fields (per DECISIONS.md E)
    suggested_model?: string;
    estimated_models?: { model: string; subtask_hint: string }[];
    models_used?: { model: string; thinking_time_seconds: number; idle_time_seconds: number; cost_estimate_usd: number; context_window_used_pct: number }[];
    // Other extended fields
    estimated_model?: string;
    estimate?: number;
    labels?: string[];
    links?: { target: number; type: string }[];
    subtickets?: { id: number; title: string; status: string }[];
    notes?: string;
    comments?: { timestamp: string; author: string; body: string }[];
    attachments?: string[];
    spans?: { start: string; end: string };
    assigned_to?: string;
    parent?: number;
    is_epic?: boolean;
    blocked_by?: number[];
    dependencies?: number[];
    [key: string]: unknown;
}

export interface TicketEngine {
    ticketDir: string;
    indexPath: string;

    create(params: CreateTicketParams): Ticket;
    list(area?: string, status?: string): Ticket[];
    update(number: number, patch: TicketPatch): Ticket | undefined;
    archive(number: number): Ticket | undefined;
    validate(): ValidateResult;
    getEnforcement(): EnforcementResult;
    /** All audit entries, oldest first. Throws if the log is corrupt. */
    getAuditLog(): AuditEntry[];
    /**
     * Merge another branch's audited entries into this log, preserving the
     * append-only history. Entries are de-duplicated and re-ordered by
     * timestamp, then the merged log is verified before it is written.
     */
    mergeAuditLog(entries: AuditEntry[]): { added: number; total: number };
    /** Adopt un-audited ticket files as `baseline` entries. Idempotent.
     *  Returns only the entries added by this call. Refuses if the audit
     *  log is already invalid. */
    baseline(): AuditEntry[];
}

function validateType(type: unknown): void {
    if (typeof type !== 'string' || !VALID_TYPES.has(type)) {
        throw new Error(`Invalid ticket type '${type}'. Must be one of: spike, feature, bug, improvement, doc, infra`);
    }
}

function normalizeStringList(value: unknown, field: string): string[] {
    if (value === undefined || value === null) return [];
    if (!Array.isArray(value)) {
        throw new Error(`${field} must be an array of strings`);
    }
    const seen = new Set<string>();
    const out: string[] = [];
    for (const item of value) {
        if (typeof item !== 'string') {
            throw new Error(`${field} must contain only strings`);
        }
        const trimmed = item.trim();
        if (!trimmed) {
            throw new Error(`${field} must not contain empty strings`);
        }
        if (!seen.has(trimmed)) {
            seen.add(trimmed);
            out.push(trimmed);
        }
    }
    return out;
}

function normalizeAcceptanceCriteria(input: unknown): AcceptanceCriterion[] {
    if (!Array.isArray(input)) {
        throw new Error('acceptance_criteria must be an array');
    }
    if (input.length === 0) {
        throw new Error('acceptance_criteria must contain at least one criterion');
    }

    const used = new Set<string>();
    let auto = 1;

    return input.map((raw, index) => {
        let text: unknown;
        let id: string | undefined;
        let status: unknown;
        let evidence: unknown;
        let note: unknown;

        if (typeof raw === 'string') {
            text = raw;
        } else if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
            const obj = raw as Record<string, unknown>;
            text = obj.text;
            id = typeof obj.id === 'string' ? obj.id.trim() : undefined;
            status = obj.status;
            evidence = obj.evidence;
            note = obj.note;
        } else {
            throw new Error(`acceptance_criteria[${index}] must be a string or an object`);
        }

        if (typeof text !== 'string' || !text.trim()) {
            throw new Error(`acceptance_criteria[${index}].text is required`);
        }

        if (id) {
            if (!AC_ID_PATTERN.test(id)) {
                throw new Error(`acceptance_criteria[${index}].id '${id}' is invalid (letters, digits, underscore, hyphen; must start with a letter)`);
            }
            if (used.has(id)) {
                throw new Error(`Duplicate acceptance criterion id '${id}'`);
            }
        } else {
            while (used.has(`A${auto}`)) auto++;
            id = `A${auto++}`;
        }
        used.add(id);

        const statusValue = status === undefined ? 'pending' : status;
        if (typeof statusValue !== 'string' || !AC_STATUS_SET.has(statusValue)) {
            throw new Error(
                `acceptance criterion '${id}' has invalid status '${String(statusValue)}'. ` +
                `Must be one of: ${ACCEPTANCE_CRITERIA_STATUSES.join(', ')}`
            );
        }

        const evidenceValue = typeof evidence === 'string' ? evidence.trim() : '';
        const noteValue = typeof note === 'string' ? note.trim() : '';

        if (statusValue === 'passing' && !evidenceValue) {
            throw new Error(`acceptance criterion '${id}' is passing but has no evidence (test path/case, command, or observed artifact)`);
        }
        if (statusValue === 'waived' && !noteValue) {
            throw new Error(`acceptance criterion '${id}' is waived but has no note explaining why no test is needed`);
        }

        const criterion: AcceptanceCriterion = {
            id,
            text: text.trim(),
            status: statusValue as AcceptanceCriterionStatus,
        };
        if (evidenceValue) criterion.evidence = evidenceValue;
        if (noteValue) criterion.note = noteValue;
        return criterion;
    });
}

function normalizeTests(input: unknown): TicketTests {
    if (input === undefined || input === null) {
        return { needed: [], created: [], cases: [] };
    }
    // A bare array is shorthand for `needed`.
    if (Array.isArray(input)) {
        return { needed: normalizeStringList(input, 'tests.needed'), created: [], cases: [] };
    }
    if (typeof input !== 'object') {
        throw new Error('tests must be an object with needed/created/cases arrays');
    }
    const obj = input as Record<string, unknown>;
    return {
        needed: normalizeStringList(obj.needed, 'tests.needed'),
        created: normalizeStringList(obj.created, 'tests.created'),
        cases: normalizeStringList(obj.cases, 'tests.cases'),
    };
}

/** Merge two test ledgers without dropping existing entries. */
function mergeTests(existing: TicketTests, incoming: TicketTests): TicketTests {
    return {
        needed: normalizeStringList([...existing.needed, ...incoming.needed], 'tests.needed'),
        created: normalizeStringList([...existing.created, ...incoming.created], 'tests.created'),
        cases: normalizeStringList([...existing.cases, ...incoming.cases], 'tests.cases'),
    };
}

function assertAcceptanceCriteriaShape(value: unknown): void {
    if (value === undefined) return;
    if (!Array.isArray(value)) {
        throw new TypeError('ticket.acceptance_criteria must be an array');
    }
    const ids = new Set<string>();
    value.forEach((raw, index) => {
        if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
            throw new TypeError(`ticket.acceptance_criteria[${index}] must be an object`);
        }
        const obj = raw as Record<string, unknown>;
        if (typeof obj.id !== 'string' || !AC_ID_PATTERN.test(obj.id)) {
            throw new TypeError(`ticket.acceptance_criteria[${index}].id is invalid`);
        }
        if (ids.has(obj.id)) {
            throw new TypeError(`ticket.acceptance_criteria has duplicate id '${obj.id}'`);
        }
        ids.add(obj.id);
        if (typeof obj.text !== 'string' || !obj.text.trim()) {
            throw new TypeError(`ticket.acceptance_criteria[${index}].text is required`);
        }
        if (typeof obj.status !== 'string' || !AC_STATUS_SET.has(obj.status)) {
            throw new TypeError(`ticket.acceptance_criteria[${index}].status is invalid`);
        }
    });
}

function assertTestsShape(value: unknown): void {
    if (value === undefined) return;
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
        throw new TypeError('ticket.tests must be an object');
    }
    const obj = value as Record<string, unknown>;
    for (const key of ['needed', 'created', 'cases'] as const) {
        if (obj[key] === undefined) continue;
        if (!Array.isArray(obj[key]) || (obj[key] as unknown[]).some(v => typeof v !== 'string')) {
            throw new TypeError(`ticket.tests.${key} must be an array of strings`);
        }
    }
}

function assertTransition(from: string, to: string): void {
    const targets = STATUS_TRANSITIONS[from];
    if (targets && !targets.includes(to)) {
        throw new Error(`Cannot transition '${from}' -> '${to}'`);
    }
    if (!targets) {
        throw new Error(`Status '${from}' is terminal — no outgoing transitions allowed`);
    }
}

function validateData(data: Record<string, unknown> | Ticket): void {
    if (typeof data.number !== 'number') throw new TypeError('ticket.number missing');
    if (typeof data.area !== 'string' || !data.area) throw new TypeError('ticket.area required');
    if (!VALID_TYPES.has(data.type as string)) throw new TypeError(`Invalid type '${data.type}'`);
    if (typeof data.status !== 'string') throw new TypeError('ticket.status missing');
    if (typeof data.title !== 'string' || !data.title) throw new TypeError('ticket.title required');
    assertAcceptanceCriteriaShape(data.acceptance_criteria);
    assertTestsShape(data.tests);
}

function ticketFileName(number: number): string {
    return `ticket-${String(number).padStart(3, '0')}.json`;
}

function detectOrphanFiles(ticketDir: string): string[] {
    const auditedNumbers = new Set(tryReadAuditLog(ticketDir).map(e => e.ticket_number));
    const files = fs.readdirSync(ticketDir).filter(f => /^ticket-\d+\.json$/.test(f));
    const orphanFiles: string[] = [];
    for (const file of files) {
        const num = parseInt(file.match(/\d+/)![0], 10);
        if (!auditedNumbers.has(num)) {
            orphanFiles.push(path.join(ticketDir, file));
        }
    }
    return orphanFiles;
}

/**
 * Check the acceptance-criteria + test-ledger policy for every ticket.
 *
 * Errors fail `validate` (missing fields, invalid status, `passing` with no
 * evidence, `waived` with no note). Warnings surface drift but do not block:
 * a test-needed criterion with an empty `needed` list, a created/passing
 * criterion with no test in `created`, or a resolved ticket with unverified
 * criteria.
 */
function checkQuality(ticketDir: string): QualityResult {
    const issues: QualityIssue[] = [];
    const files = fs.readdirSync(ticketDir)
        .filter(f => /^ticket-\d+\.json$/.test(f))
        .sort();

    for (const file of files) {
        const number = parseInt(file.match(/\d+/)![0], 10);
        let data: Ticket;
        try {
            data = JSON.parse(fs.readFileSync(path.join(ticketDir, file), 'utf-8')) as Ticket;
        } catch {
            issues.push({
                ticket_number: number,
                field: 'acceptance_criteria',
                severity: 'error',
                message: 'Ticket file is not valid JSON',
            });
            continue;
        }

        const criteria = data.acceptance_criteria;
        if (!Array.isArray(criteria) || criteria.length === 0) {
            issues.push({
                ticket_number: number,
                field: 'acceptance_criteria',
                severity: 'error',
                message: 'Missing acceptance_criteria (at least one criterion is required)',
            });
        } else {
            const ids = new Set<string>();
            criteria.forEach((raw, index) => {
                const label = raw && typeof raw === 'object' && typeof raw.id === 'string'
                    ? raw.id
                    : `#${index + 1}`;
                if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
                    issues.push({
                        ticket_number: number,
                        field: 'acceptance_criteria',
                        severity: 'error',
                        message: `Criterion ${label} is not an object`,
                    });
                    return;
                }
                const ac = raw as unknown as Record<string, unknown>;
                if (typeof ac.id !== 'string' || !AC_ID_PATTERN.test(ac.id)) {
                    issues.push({
                        ticket_number: number,
                        field: 'acceptance_criteria',
                        severity: 'error',
                        message: `Criterion ${label} has a missing or invalid id`,
                    });
                } else if (ids.has(ac.id)) {
                    issues.push({
                        ticket_number: number,
                        field: 'acceptance_criteria',
                        severity: 'error',
                        message: `Duplicate acceptance criterion id '${ac.id}'`,
                    });
                } else {
                    ids.add(ac.id);
                }
                if (typeof ac.text !== 'string' || !ac.text.trim()) {
                    issues.push({
                        ticket_number: number,
                        field: 'acceptance_criteria',
                        severity: 'error',
                        message: `Criterion ${label} has no text`,
                    });
                }
                if (typeof ac.status !== 'string' || !AC_STATUS_SET.has(ac.status)) {
                    issues.push({
                        ticket_number: number,
                        field: 'acceptance_criteria',
                        severity: 'error',
                        message: `Criterion ${label} has invalid status '${String(ac.status)}'`,
                    });
                }
                if (ac.status === 'passing' && (typeof ac.evidence !== 'string' || !ac.evidence.trim())) {
                    issues.push({
                        ticket_number: number,
                        field: 'acceptance_criteria',
                        severity: 'error',
                        message: `Criterion ${label} is passing but has no evidence`,
                    });
                }
                if (ac.status === 'waived' && (typeof ac.note !== 'string' || !ac.note.trim())) {
                    issues.push({
                        ticket_number: number,
                        field: 'acceptance_criteria',
                        severity: 'error',
                        message: `Criterion ${label} is waived but has no note`,
                    });
                }
            });
        }

        if (!data.tests || typeof data.tests !== 'object' || Array.isArray(data.tests)) {
            issues.push({
                ticket_number: number,
                field: 'tests',
                severity: 'error',
                message: 'Missing tests ledger (needed/created/cases arrays are required)',
            });
        } else {
            const tests = data.tests as unknown as Record<string, unknown>;
            for (const key of ['needed', 'created', 'cases'] as const) {
                if (!Array.isArray(tests[key]) || (tests[key] as unknown[]).some(v => typeof v !== 'string')) {
                    issues.push({
                        ticket_number: number,
                        field: 'tests',
                        severity: 'error',
                        message: `tests.${key} must be an array of strings`,
                    });
                }
            }

            if (Array.isArray(criteria)) {
                const needed = Array.isArray(tests.needed) ? tests.needed.length : 0;
                const created = Array.isArray(tests.created) ? tests.created.length : 0;
                for (const ac of criteria) {
                    if (!ac || typeof ac !== 'object') continue;
                    if (ac.status === 'test-needed' && needed === 0) {
                        issues.push({
                            ticket_number: number,
                            field: 'tests',
                            severity: 'warning',
                            message: `Criterion ${ac.id} is test-needed but tests.needed is empty`,
                        });
                    }
                    if ((ac.status === 'test-created' || ac.status === 'passing') && created === 0) {
                        issues.push({
                            ticket_number: number,
                            field: 'tests',
                            severity: 'warning',
                            message: `Criterion ${ac.id} is ${ac.status} but tests.created is empty`,
                        });
                    }
                }
            }
        }

        if (data.status === 'resolved' && Array.isArray(criteria)) {
            const unverified = criteria.filter(ac =>
                ac && typeof ac === 'object' && ac.status !== 'passing' && ac.status !== 'waived'
            );
            if (unverified.length > 0) {
                issues.push({
                    ticket_number: number,
                    field: 'acceptance_criteria',
                    severity: 'warning',
                    message: `Ticket is resolved but ${unverified.length} criterion/criteria are not passing or waived`,
                });
            }
        }
    }

    return {
        ok: !issues.some(issue => issue.severity === 'error'),
        tickets_checked: files.length,
        issues,
    };
}

// ────────────────────────────── engine ──────────────────────────────

export function createTicketEngine({
    ticketDir,
    indexPath,
}: {
    ticketDir: string;
    indexPath: string;
}): TicketEngine {
    fs.mkdirSync(ticketDir, { recursive: true });
    fs.mkdirSync(path.dirname(indexPath), { recursive: true });

    // Guard against missing/corrupt index → auto-reconstruct from disk.
    // Corruption includes JSON parse failures from an unresolved merge.
    function indexIsUsable(): boolean {
        try {
            const sanity = readIndex(indexPath);
            return Boolean(sanity && sanity.schemaVersion);
        } catch {
            return false;
        }
    }

    if (!fs.existsSync(indexPath) || !indexIsUsable()) {
        writeFromScratch();
    }

    function writeFromScratch(): void {
        const rebuilt = reconstructIndex(ticketDir);
        writeIndex(indexPath, { ...rebuilt });
    }

    function nextNumber(): number {
        const idx = readIndex(indexPath);
        return idx.tickets.length ? Math.max(...idx.tickets.map(e => e.number)) + 1 : 1;
    }

    function persist(ticket: Ticket): void {
        fs.writeFileSync(
            path.join(ticketDir, ticketFileName(ticket.number)),
            JSON.stringify({ ...ticket }, null, 2),
            'utf-8'
        );

        const idx = readIndex(indexPath);
        let found = false;
        for (let i = 0; i < idx.tickets.length; i++) {
            if (idx.tickets[i].number === ticket.number) {
                idx.tickets[i] = {
                    number: ticket.number,
                    file_path: ticketFileName(ticket.number),
                    status: ticket.status,
                    area: ticket.area,
                };
                found = true;
                break;
            }
        }
        if (!found) {
            idx.tickets.push({
                number: ticket.number,
                file_path: ticketFileName(ticket.number),
                status: ticket.status,
                area: ticket.area,
            });
        }
        writeIndex(indexPath, idx);
    }

    return {
        ticketDir,
        indexPath,

        create(params: CreateTicketParams): Ticket {
            const area       = params.area          || '';
            const typeRaw     = params.type;
            let   typeVal: Ticket['type'] = 'feature';

            if (typeRaw) {
                validateType(typeRaw);
                typeVal = typeRaw as Ticket['type'];
            }

            if (params.acceptance_criteria === undefined || params.acceptance_criteria === null) {
                throw new Error('acceptance_criteria is required: every ticket must have at least one criterion');
            }
            const acceptanceCriteria = normalizeAcceptanceCriteria(params.acceptance_criteria);
            const tests = normalizeTests(params.tests);

            return withLock(ticketDir, () => {
                const now  = new Date().toISOString();
                const num  = nextNumber();
                const file = path.join(ticketDir, ticketFileName(num));

                const ticket: Ticket = {
                    id: num,
                    file_path: file,
                    number:    num,
                    area,
                    type:     typeVal,
                    status:   'draft',
                    title:    params.title,
                    objective: params.objective || '',
                    created_at: now,
                    updated_at: now,
                    acceptance_criteria: acceptanceCriteria,
                    tests,
                    // Model tracking fields
                    suggested_model: params.suggested_model,
                    estimated_models: params.estimated_models,
                    estimated_model: params.estimated_model,
                    // Other extended fields
                    estimate: params.estimate,
                    priority: params.priority,
                    labels: params.labels,
                    parent: params.parent,
                    is_epic: params.is_epic,
                    assigned_to: params.assigned_to,
                    spans: params.spans,
                    comments: params.comments,
                    models_used: [],
                };

                // Enforcement: persist + audit inside one locked write window.
                persist(ticket);
                appendAuditEntry(ticketDir, {
                    operation: 'create',
                    ticket_number: num,
                });

                return ticket;
            });
        },

        list(area?: string, status?: string): Ticket[] {
            const idx = readIndex(indexPath);
            let entries = [...idx.tickets];

            if (area)   entries = entries.filter(e => e.area === area);
            if (status) {
                // When filtering by a specific non-draft status, only return matching files
                // When filtering for 'draft', include all drafts (may be missing from disk)
                if (status === 'draft') {
                    entries = entries.filter(e => e.status === 'draft');
                } else {
                    entries = entries.filter(e => e.status === status);
                }
            }

            return entries
                .filter(e => fs.existsSync(path.join(ticketDir, ticketFileName(e.number))))
                .map(e => {
                    const raw = fs.readFileSync(path.join(ticketDir, ticketFileName(e.number)), 'utf-8');
                    const data: Ticket = JSON.parse(raw);
                    validateData(data);
                    return data;
            });
        },

        update(number: number, patch: TicketPatch): Ticket | undefined {
            const file = path.join(ticketDir, ticketFileName(number));
            if (!fs.existsSync(file)) return undefined;

            return withLock(ticketDir, () => {
                const raw      = fs.readFileSync(file, 'utf-8');
                const data:    Ticket              = JSON.parse(raw);
                validateData(data);

                if (patch.status && patch.status !== data.status) {
                    const newStatus = patch.status as string;
                    assertTransition(data.status, newStatus);
                }

                // Normalise policy fields before assignment. ACs replace the
                // array; test ledger arrays merge so the trail is append-only.
                const normalized: Record<string, unknown> = { ...patch };
                if (patch.acceptance_criteria !== undefined) {
                    normalized.acceptance_criteria = normalizeAcceptanceCriteria(patch.acceptance_criteria);
                }
                if (patch.tests !== undefined) {
                    normalized.tests = mergeTests(normalizeTests(data.tests), normalizeTests(patch.tests));
                }

                for (const key of Object.keys(normalized)) {
                    if (normalized[key] != null &&
                        key !== 'id'          &&
                        key !== 'file_path'   &&
                        key !== 'number')
                    {
                        (data as unknown as Record<string, unknown>)[key] = normalized[key];
                    }
                }

                data.updated_at = new Date().toISOString();

                // Enforcement: persist + audit inside one locked write window.
                persist(data);
                appendAuditEntry(ticketDir, {
                    operation: 'update',
                    ticket_number: number,
                });

                return data;
            });
        },

        archive(number: number): Ticket | undefined {
            const file = path.join(ticketDir, ticketFileName(number));
            if (!fs.existsSync(file)) return undefined;

            return withLock(ticketDir, () => {
                const raw     = fs.readFileSync(file, 'utf-8');
                const data: Ticket = JSON.parse(raw);
                validateData(data);

                data.status      = 'archived';
                data.updated_at  = new Date().toISOString();

                // Enforcement: persist + audit inside one locked write window.
                persist(data);
                appendAuditEntry(ticketDir, {
                    operation: 'archive',
                    ticket_number: number,
                });

                return data;
            });
        },

        validate(): ValidateResult {
            const idx = readIndex(indexPath);
            const files = fs.readdirSync(ticketDir).filter(f => /^ticket-\d+\.json$/.test(f));

            const fileNums   = new Set(files.map(f => parseInt(f.match(/\d+/)![0], 10)));
            const indexedNums = idx.tickets.map(e => e.number);

            const missing: number[] = [];
            for (const n of fileNums) {
                if (!indexedNums.includes(n)) missing.push(n);
            }

            const extra: string[] = [];
            for (const entry of idx.tickets) {
                if (!fileNums.has(entry.number)) extra.push(entry.file_path);
            }

            const synced = missing.length === 0 && extra.length === 0;
            const enforcement = this.getEnforcement();
            const quality = checkQuality(ticketDir);

            return {
                synced,
                missing_files: missing.length ? missing.map(n => path.join(ticketDir, ticketFileName(n))) : undefined,
                extra_files: extra.length ? extra : undefined,
                enforcement,
                quality,
                // Policy errors fail validation just like index/audit drift.
                ok: synced && enforcement.enforced && !enforcement.has_orphans && quality.ok,
            };
        },

        getEnforcement(): EnforcementResult {
            const messages: string[] = [];

            // Check 1: Lock state. Missing is normal; stale (dead PID) is a problem.
            const lock = inspectLock(this.ticketDir);
            messages.push(`Lock: ${lock.message}`);

            // Check 2: Audit log hashes and shape. A non-decreasing ticket
            // number is NOT required (updates can target older tickets).
            const audit = verifyAuditLog(this.ticketDir);
            messages.push(...audit.messages);

            // Check 3: Orphan files (on disk but not in audit — flagged for
            // review, never auto-deleted).
            const orphanFiles = detectOrphanFiles(this.ticketDir);
            const hasOrphans = orphanFiles.length > 0;
            if (hasOrphans) {
                messages.push(`⚠️ Orphan files detected (${orphanFiles.length}): on disk but not in audit log — requires human review before any action`);
                for (const f of orphanFiles) {
                    messages.push(`   ↳ ${path.basename(f)}`);
                }
            }

            return {
                enforced: lock.state !== 'stale' && audit.valid,
                has_orphans: hasOrphans,
                orphan_files: orphanFiles,
                lock,
                audit,
                messages,
            };
        },

        getAuditLog(): AuditEntry[] {
            return readAuditLog(this.ticketDir);
        },

        mergeAuditLog(incoming: AuditEntry[]): { added: number; total: number } {
            if (!Array.isArray(incoming)) {
                throw new Error('incoming audit log must be a JSON array');
            }

            return withLock(this.ticketDir, () => {
                const current = readAuditLog(this.ticketDir);
                const currentStatus = verifyAuditEntries(current);
                if (!currentStatus.valid) {
                    throw new Error(`Refusing merge: local audit log is invalid (${currentStatus.messages.join('; ')})`);
                }

                const incomingStatus = verifyAuditEntries(incoming);
                if (!incomingStatus.valid) {
                    throw new Error(`Refusing merge: incoming audit log is invalid (${incomingStatus.messages.join('; ')})`);
                }

                const key = (entry: AuditEntry) =>
                    `${entry.timestamp}\u0000${entry.operation}\u0000${entry.ticket_number}\u0000${entry.hash}`;
                const seen = new Set(current.map(key));
                const merged = [...current];
                let added = 0;

                for (const entry of incoming) {
                    const entryKey = key(entry);
                    if (seen.has(entryKey)) continue;
                    seen.add(entryKey);
                    merged.push(entry);
                    added++;
                }

                // Stable sort: equal timestamps keep local entries before incoming ones.
                merged.sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp));

                const mergedStatus = verifyAuditEntries(merged);
                if (!mergedStatus.valid) {
                    throw new Error(`Refusing merge: merged audit log is invalid (${mergedStatus.messages.join('; ')})`);
                }

                if (added > 0) {
                    writeAuditLog(this.ticketDir, merged);
                }
                return { added, total: merged.length };
            });
        },

        baseline(): AuditEntry[] {
            return withLock(this.ticketDir, () => {
                // Never append to a log we cannot trust.
                const audit = verifyAuditLog(this.ticketDir);
                if (!audit.valid) {
                    throw new Error(`Refusing baseline: audit log is invalid (${audit.messages.join('; ')})`);
                }

                const entries = readAuditLog(this.ticketDir);
                const audited = new Set(entries.map(e => e.ticket_number));
                const files = fs.readdirSync(this.ticketDir)
                    .filter(f => /^ticket-\d+\.json$/.test(f))
                    .sort();

                const added: AuditEntry[] = [];
                const timestamp = new Date().toISOString();
                for (const file of files) {
                    const number = parseInt(file.match(/\d+/)![0], 10);
                    if (audited.has(number)) continue;
                    const entry = makeAuditEntry('baseline', number, timestamp);
                    entries.push(entry);
                    added.push(entry);
                }

                if (added.length > 0) {
                    writeAuditLog(this.ticketDir, entries);
                }
                return added;
            });
        },
    };
}
