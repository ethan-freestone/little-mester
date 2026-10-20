#!/usr/bin/env node
/**
 * migrate-tickets.ts — Convert docs/tickets/ticket-NNN.md files to JSON,
 * validate against ticket-v1.json schema, and report results.
 */

import * as fs from 'fs';
import * as path from 'path';

// ── helpers ──────────────────────────────────────────────────────

function readJson<T>(fp: string): T {
    const raw = fs.readFileSync(fp, 'utf-8');
    return JSON.parse(raw) as T;
}

function writeJson(fp: string, obj: unknown, indent = 2): void {
    fs.writeFileSync(fp, JSON.stringify(obj, null, indent) + '\n', 'utf-8');
}

// ── schema loader ────────────────────────────────────────────────

const SCHEMA_DIR = path.resolve(__dirname, '..', 'docs');

interface SchemaField {
    type?: string | string[];
    enum?: unknown[];
    required?: boolean;
    minLength?: number;
    minimum?: number;
}

interface SchemaType {
    properties: Record<string, SchemaField>;
    required: string[];
    [key: string]: unknown;
}

const schema = readJson<Record<string, unknown>>(path.join(SCHEMA_DIR, 'ticket-v1.json')) as SchemaType;

// ── parse metadata from MD ticket ────────────────────────────────

function extractNumber(filename: string): number {
    // e.g. "ticket-007-ticket-migration-and-schema.md" → 7
    const m = filename.match(/ticket-(\d+)/);
    return m ? parseInt(m![1], 10) : 0;
}

function parseMD(fileContent: string, filename: string): Partial<TicketJson> {
    const lines = fileContent.split('\n');
    let result: Partial<TicketJson> = {
        area: '',
        type: 'feature',         // most early tickets have no explicit type
        status: 'draft',          // default for pre-migration tickets
        title: '',
        objective: '',
        number: extractNumber(filename),
    };

    // Extract title from first heading line "# Ticket NNN — ..."
    const titleLine = lines.find(l => /^#\s+Ticket\s+\d+/.test(l)) || lines[0];
    if (titleLine) {
        const m = titleLine.match(/^#\s+[Tt]icket\s+(\d+)\s*[—\-‑]\s*(.*)/);
        result.title = m ? m[2].trim() : titleLine.replace(/^#+\s*/, '').trim();
    }

    // Look for a metadata table early in the file: "field | type | ..."
    // Many tickets have a table like:
    // | Field     | Type   | ...
    // and rows with values we can pluck
    let inTable = false;
    let headerFields: string[] = [];
    const MAX_ROWS_TO_SCAN = 60;

    for (let i = 0; i < Math.min(MAX_ROWS_TO_SCAN, lines.length); i++) {
        const line = lines[i];

        // Table start
        if (/^\|.*\|/.test(line) && /^\|?\s*Field\s*\|/i.test(line)) {
            inTable = true;
            headerFields = line.split('|').map(h => h.trim().toLowerCase()).filter(Boolean);
            continue;
        }

        // Table end (empty separator row) — actually skip-separator rows are also |:---|
        // We'll track data rows until a blank or non-table line
        if (!/^(\|?[\s\-:|]+)?\||$/.test(line) || !line.includes('|')) {
            inTable = false;
            continue;
        }

        if (!inTable) continue;

        const cells = line.split('|').map(c => c.trim()).filter(Boolean);

        // Look for "area" row
        const areaIdx = headerFields.findIndex(h => h === 'area');
        if (areaIdx !== -1 && cells[areaIdx]) {
            result.area = cells[areaIdx];
        }

        // Look for "type" row
        const typeIdx = headerFields.findIndex(h => h === 'type');
        if (typeIdx !== -1 && cells[typeIdx]) {
            const val = cells[typeIdx].toLowerCase();
            const validTypes: string[] = ['spike', 'feature', 'bug', 'improvement', 'doc', 'infra'];
            // Map old "pause" type to something sensible
            if (val === 'pause' || val === 'paused') {
                result.type = 'doc';  // paused work is documented as a doc ticket
            } else if (validTypes.includes(val)) {
                result.type = val as TicketJson['type'];
            }
        }

        // Look for "status" row or text that says "status"
        const statusIdx = headerFields.findIndex(h => h === 'status');
        if (statusIdx !== -1 && cells[statusIdx]) {
            const s = cells[statusIdx].toLowerCase();
            if (['draft', 'active', 'resolved', 'archived'].includes(s)) {
                result.status = s as TicketJson['status'];
            }
        }

        // Look for objective / "goal" / "description" rows
        const objIdx = headerFields.findIndex(h => h === 'objective' || h === 'goal' || h === 'description');
        if (objIdx !== -1 && cells[objIdx]) {
            result.objective = cells[objIdx];
        }
    }

    // Backup: search for "area:" or "area  : pattern lines
    if (!result.area) {
        const areaMatch = fileContent.match(/[\s-]area\s*[:\-]\s*"([^"]+)"/i);
        if (areaMatch) result.area = areaMatch[1];
    }

    // Look for a date pattern near the top to use as created_at / updated_at
    const now = new Date().toISOString();
    result.created_at = now;
    result.updated_at = now;

    // Fallback: if no area was ever set, default to 'general' (retrospective migration)
    // These early tickets predate the area concept — users can update later.
    if (!result.area || !result.area.trim()) {
        result.area = 'general';
    }

    return result;
}

// ── lightweight schema validation ───────────────────────────────

const VALID_TYPES: ReadonlySet<string> = new Set(['spike', 'feature', 'bug', 'improvement', 'doc', 'infra']);
const VALID_STATUSES: ReadonlySet<string> = new Set(['draft', 'active', 'resolved', 'archived']);

interface ValidationResult {
    ok: boolean;
    errors: string[];
}

function validateAgainstSchema(ticket: TicketJson, schema: SchemaType): ValidationResult {
    const errors: string[] = [];

    for (const req of schema.required) {
        if ((ticket as unknown as Record<string, unknown>)[req] === undefined || (ticket as unknown as Record<string, unknown>)[req] === null) {
            errors.push(`missing required field '${req}'`);
        }
    }

    // area: string, minLength 1
    if (typeof ticket['area'] !== 'string' || !ticket['area']) {
        errors.push("'area' must be a non-empty string");
    }

    // type: enum
    if (!VALID_TYPES.has(ticket['type'])) {
        errors.push(`invalid type '${ticket['type']}' — expected one of spike|feature|bug|improvement|doc|infra`);
    }

    // status: enum
    if (!VALID_STATUSES.has(ticket['status'])) {
        errors.push(`invalid status '${ticket['status']}' — expected one of draft|active|resolved|archived`);
    }

    // number: integer >= 1
    if (typeof ticket['number'] !== 'number' || ticket['number'] < 1) {
        errors.push("'number' must be a positive integer");
    }

    // title: string, minLength 1
    if (typeof ticket['title'] !== 'string' || !ticket['title']) {
        errors.push("'title' must be a non-empty string");
    }

    return { ok: errors.length === 0, errors };
}

// ── main ───────────────────────────────────────────────────────

function run(inputDir: string, outputDir: string): void {
    // Create input dir if it doesn't exist (for the workspace root case)
    let files: string[] = [];
    if (fs.existsSync(inputDir)) {
        files = fs.readdirSync(inputDir).filter(f => /^ticket-\d+-.+\.md$/.test(f)).sort();
    }

    if (files.length === 0) {
        console.warn(`No ticket-*.md files found in ${inputDir}`);
        process.exit(1);
    }

    let passed = 0;
    let failed = 0;

    for (const file of files) {
        const filePath = path.join(inputDir, file);
        const number = extractNumber(file);
        // strip extension and number suffix: "ticket-007-ticket-migration-and-schema.md" → "ticket-007.json"
        const nameWithoutExt = file.replace(/\.(md|markdown)$/, '');
        const numMatch = nameWithoutExt.match(/^(ticket-\d+)-.*/);
        const baseName = numMatch ? numMatch[1] : nameWithoutExt;
        const outPath = path.join(outputDir, `${baseName}.json`);

        const content = fs.readFileSync(filePath, 'utf-8');
        let extracted = parseMD(content, file);

        // Build the JSON ticket object
        const ticket: TicketJson = {
            $schema: '',  // will be set below
            number:     extracted.number          || 0,
            area:       extracted.area           ?? '',
            type:       (extracted.type as TicketJson['type'])  || 'feature',
            status:     (extracted.status as TicketJson['status']) || 'draft',
            title:      extracted.title           ?? '',
            objective:  extracted.objective       ?? null,
            created_at: extracted.created_at      ?? new Date().toISOString(),
            updated_at: extracted.updated_at      ?? new Date().toISOString(),
        };

        // Validate
        const result = validateAgainstSchema(ticket as TicketJson, schema);
        if (!result.ok) {
            console.error(`  ✗ ${file} → ${outPath}`);
            for (const err of result.errors) console.error(`    - ${err}`);
            failed++;
            continue;
        }

        // Set the $schema reference
        ticket.$schema = 'https://raw.githubusercontent.com/little-mester/ticket-engine/v1.0/docs/SCHEMA.md';

        writeJson(outPath, ticket);
        console.log(`  ✓ ${file} → ${outPath} [area=${ticket.area || '(none)'}, type=${ticket.type}, status=${ticket.status}]`);
        passed++;
    }

    console.log(`\nMigration complete: ${passed}/${files.length} tickets migrated, ${failed} failed validation\n`);
    if (failed > 0) {
        process.exit(1);
    }
}

// ── types ───────────────────────────────────────────────────────

interface TicketJson {
    $schema: string;
    number: number;
    area: string;
    type: 'spike' | 'feature' | 'bug' | 'improvement' | 'doc' | 'infra';
    status: 'draft' | 'active' | 'resolved' | 'archived';
    title: string;
    objective: string | null;
    created_at: string;
    updated_at: string;
}

// ── CLI arg parsing (simple) ───────

const args = process.argv.slice(2);
let inputDir = path.resolve(process.cwd(), 'docs', 'tickets');
let outputDir = path.resolve(process.cwd(), 'docs', 'tickets');

for (let i = 0; i < args.length - 1; i++) {
    if (args[i] === '--input')   inputDir   = path.resolve(args[i + 1]);
    if (args[i] === '--output')  outputDir  = path.resolve(args[i + 1]);
}

console.log(`Migration: ${inputDir} → ${outputDir}`);
run(inputDir, outputDir);
