import * as fs from 'fs';
import * as path from 'path';

const SCHEMA_VERSION = 1;
const INDEX_FILENAME = '_index.json';
const FILE_PATTERN = /^ticket-\d+\.json$/;

export interface IndexEntry {
    number: number;
    file_path: string;
    status: string;
    area: string;
}

export interface TicketIndex {
    schemaVersion: number;
    tickets: IndexEntry[];
    updated_at: string;
}

const DEFAULT_INDEX: TicketIndex = {
    schemaVersion: SCHEMA_VERSION,
    tickets: [],
    updated_at: new Date().toISOString(),
};

export function readIndex(indexPath: string): TicketIndex {
    if (!fs.existsSync(indexPath)) return DEFAULT_INDEX;
    const content = fs.readFileSync(indexPath, 'utf-8');
    const parsed = JSON.parse(content) as Partial<TicketIndex>;
    return {
        schemaVersion: parsed.schemaVersion || SCHEMA_VERSION,
        tickets: parsed.tickets || [],
        updated_at: parsed.updated_at || new Date().toISOString(),
    };
}

export function writeIndex(
    indexPath: string,
    index: TicketIndex,
): void {
    fs.mkdirSync(path.dirname(indexPath), { recursive: true });
    const serialized = JSON.stringify({
        ...index,
        updated_at: new Date().toISOString(),
    }, null, 2);
    fs.writeFileSync(indexPath, serialized, 'utf-8');
}

export function scanTicketDir(ticketDir: string): IndexEntry[] {
    if (!fs.existsSync(ticketDir)) return [];
    const files = fs.readdirSync(ticketDir).filter(f => FILE_PATTERN.test(f));
    return files.map(f => ({
        number: parseInt(f.match(/\d+/)?.[0] || '0', 10),
        file_path: f,
    }));
}

export function reconstructIndex(ticketDir: string): TicketIndex {
    const entries = scanTicketDir(ticketDir);
    const tickets = entries.map(e => {
        const filePath = path.join(ticketDir, e.file_path);
        let status = 'draft';
        let area = '';
        if (fs.existsSync(filePath)) {
            const raw = fs.readFileSync(filePath, 'utf-8');
            const ticket = JSON.parse(raw);
            status = ticket.status || 'draft';
            area = ticket.area || '';
        }
        return { ...e, status, area };
    });
    return { schemaVersion: SCHEMA_VERSION, tickets, updated_at: new Date().toISOString() };
}
