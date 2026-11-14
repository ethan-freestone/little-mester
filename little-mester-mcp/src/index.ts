import * as fs from 'fs';
import * as crypto from 'crypto';
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
    atomicWriteFile(indexPath, serialized, 0o666);
}

export function atomicWriteFile(filePath: string, contents: string, mode: number): void {
    const directory = path.dirname(filePath);
    let existingMode: number | undefined;
    try {
        const existing = fs.lstatSync(filePath);
        if (!existing.isFile() || existing.isSymbolicLink() || existing.nlink !== 1) {
            throw new Error(`Refusing unsafe replacement target: ${path.basename(filePath)}`);
        }
        existingMode = existing.mode & 0o777;
    } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
    }
    const temporary = path.join(directory,
        `.${path.basename(filePath)}.${process.pid}.${crypto.randomBytes(8).toString('hex')}.tmp`);
    const fd = fs.openSync(temporary, 'wx', mode);
    try {
        if (existingMode !== undefined) fs.fchmodSync(fd, existingMode);
        fs.writeFileSync(fd, contents, 'utf-8');
        fs.fsyncSync(fd);
    } catch (error) {
        fs.closeSync(fd);
        try { fs.unlinkSync(temporary); } catch { /* preserve the primary write failure */ }
        throw error;
    }
    fs.closeSync(fd);
    try {
        fs.renameSync(temporary, filePath);
    } catch (error) {
        try { fs.unlinkSync(temporary); } catch { /* preserve the primary rename failure */ }
        throw error;
    }
    const directoryFd = fs.openSync(directory, 'r');
    try { fs.fsyncSync(directoryFd); } finally { fs.closeSync(directoryFd); }
}

export function scanTicketDir(ticketDir: string): IndexEntry[] {
    if (!fs.existsSync(ticketDir)) return [];
    const files = fs.readdirSync(ticketDir).filter(f => FILE_PATTERN.test(f));
    return files.map(f => ({
        number: parseInt(f.match(/\d+/)?.[0] || '0', 10),
        file_path: f,
        status: 'draft' as string,
        area: '' as string,
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
