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

    create(params: { area?: string; type?: string; title: string; objective?: string }): Ticket;
    list(area?: string, status?: string): Ticket[];
    update(number: number, patch: Partial<Omit<Ticket, 'id' | 'file_path' | 'number'>>): Ticket | undefined;
    archive(number: number): Ticket | undefined;
    validate(): { synced: boolean; missing_files?: string[]; extra_files?: string[] };
}

function validateType(type: unknown): void {
    if (typeof type !== 'string' || !VALID_TYPES.has(type)) {
        throw new Error(`Invalid ticket type '${type}'. Must be one of: spike, feature, bug, improvement, doc, infra`);
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
}

function ticketFileName(number: number): string {
    return `ticket-${String(number).padStart(3, '0')}.json`;
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

    // Guard against missing/corrupt index → auto-reconstruct from disk
    if (!fs.existsSync(indexPath)) {
        writeFromScratch();
    } else {
        const sanity = readIndex(indexPath);
        if (!sanity || !sanity.schemaVersion) {
            writeFromScratch();
        }
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

        create(
            params: {
                area?: string;
                type?: string;
                title: string;
                objective?: string;
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
            }
        ): Ticket {
            const area       = params.area          || '';
            const typeRaw     = params.type;
            let   typeVal: Ticket['type'] = 'feature';

            if (typeRaw) {
                validateType(typeRaw);
                typeVal = typeRaw as Ticket['type'];
            }

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
                models_used: [],
            };

            persist(ticket);
            return ticket;
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

        update(number: number, patch: Partial<Omit<Ticket, 'id' | 'file_path' | 'number'>>): Ticket | undefined {
            const file = path.join(ticketDir, ticketFileName(number));
            if (!fs.existsSync(file)) return undefined;

            const raw      = fs.readFileSync(file, 'utf-8');
            const data:    Ticket              = JSON.parse(raw);
            validateData(data);

            if (patch.status && patch.status !== data.status) {
                const newStatus = patch.status as string;
                assertTransition(data.status, newStatus);
            }

            for (const key of Object.keys(patch)) {
                if ((patch as Record<string, unknown>)[key] != null &&
                    key !== 'id'          &&
                    key !== 'file_path'   &&
                    key !== 'number')
                {
                    (data as unknown as Record<string, unknown>)[key] = (patch as Record<string, unknown>)[key];
                }
            }

            data.updated_at = new Date().toISOString();
            persist(data);
            return data;
        },

        archive(number: number): Ticket | undefined {
            const file = path.join(ticketDir, ticketFileName(number));
            if (!fs.existsSync(file)) return undefined;

            const raw     = fs.readFileSync(file, 'utf-8');
            const data: Ticket = JSON.parse(raw);
            validateData(data);

            data.status      = 'archived';
            data.updated_at  = new Date().toISOString();
            persist(data);
            return data;
        },

        validate(): { synced: boolean; missing_files?: string[]; extra_files?: string[] } {
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

            return { synced: missing.length === 0 && extra.length === 0, missing_files: missing.length ? missing.map(n => path.join(ticketDir, ticketFileName(n))) : undefined, extra_files: extra.length ? extra : undefined };
        },
    };
}
