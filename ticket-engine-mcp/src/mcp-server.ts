#!/usr/bin/env node
/**
 * Little Mester MCP Server
 *
 * Central Jira-like ticketing hub for both humans and agents.
 *
 * Routing modes:
 *   same-repo     — tickets live in the project repo (default)
 *   any-directory — tickets live anywhere on disk (absolute path)
 *   separate-repo — tickets live in a completely isolated repo
 *
 * Configuration:
 *   - Env:          TICKET_DIR=/path/to/tickets
 *   - CLI arg:      --ticket-dir=/path/to/tickets
 *   - Per-tool:     optional ticket_dir parameter on every tool
 *
 * All tool responses include:
 *   - content:        Human-readable markdown summary
 *   - structuredContent: Machine-parseable JSON for agents
 */

import { McpServer } from '@modelcontextprotocol/sdk/dist/esm/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/dist/esm/server/stdio.js';
import { createTicketEngine } from './engine.js';
import * as path from 'path';

// ── Configuration ──────────────────────────────────────────────────

const TICKET_DIR = process.env.TICKET_DIR || './docs/tickets';

function resolveDir(raw: string): string {
    return path.resolve(raw);
}

// Parse CLI args (e.g. --ticket-dir=/foo)
function parseCliArg(name: string): string | undefined {
    const flag = `--${name}=`;
    for (const arg of process.argv) {
        if (arg.startsWith(flag)) return arg.slice(flag.length);
    }
    return undefined;
}

const CLI_TICKET_DIR = parseCliArg('ticket-dir');
const BASE_TICKET_DIR = resolveDir(CLI_TICKET_DIR || TICKET_DIR);

const server = new McpServer({
    name: 'little-mester',
    version: '0.1.0',
});

// ── Helpers ────────────────────────────────────────────────────────

function formatTicket(ticket: Record<string, unknown>): string {
    const t = ticket as Record<string, string | number>;
    const num = t.number ?? t.id ?? '?';
    const status = t.status ?? '?';
    const type = t.type ?? '?';
    const area = t.area ?? '';
    const title = t.title ?? '(no title)';
    const created = t.created_at ? new Date(t.created_at as string).toLocaleDateString() : '—';
    const updated = t.updated_at ? new Date(t.updated_at as string).toLocaleDateString() : '—';

    let line = `**#${num}** — [${status.toUpperCase()}] ${type} — ${area || 'no area'}\n`;
    line += `${title}\n`;
    if (t.objective) line += `> ${t.objective}\n`;
    line += `Created: ${created}  |  Updated: ${updated}`;
    return line;
}

function ticketSummary(tickets: Record<string, unknown>[]): string {
    if (!tickets.length) return 'No tickets found.';
    return tickets.map(formatTicket).join('\n\n');
}

// ── Tools ──────────────────────────────────────────────────────────

/**
 * Create a new ticket in draft status.
 */
server.registerTool(
    'mester_tickets_create',
    {
        title: 'Create Ticket',
        description: 'Create a new ticket in the Little Mester ticketing system. The ticket starts in DRAFT status. Returns full ticket details and structured JSON.',
        inputSchema: {
            area:       { type: 'string',  description: 'Area or subsystem (e.g. "auth", "frontend", "infra")' },
            type:       { type: 'string',  description: 'Ticket type: spike | feature | bug | improvement | doc | infra',  enum: ['spike', 'feature', 'bug', 'improvement', 'doc', 'infra'] },
            title:      { type: 'string',  description: 'Short descriptive title for the ticket',                        minLength: 1 },
            objective:  { type: 'string',  description: 'One-sentence objective or acceptance criteria' },
            ticket_dir: { type: 'string',  description: 'Optional override: directory containing ticket JSON files (absolute or relative path)' },
        },
        annotations: {
            titleForHuman: 'Create a new ticket',
        },
    },
    async ({ area, type, title, objective, ticket_dir }) => {
        const engine = createTicketEngine({
            ticketDir: resolveDir(ticket_dir || BASE_TICKET_DIR),
            indexPath: path.join(resolveDir(ticket_dir || BASE_TICKET_DIR), '_index.json'),
        });

        try {
            const ticket = engine.create({ area, type, title, objective });
            return {
                content: [{
                    type: 'text',
                    text: `✅ Ticket created:\n\n${formatTicket(ticket)}\n\n**Next step:** Update the ticket with details, move to ACTIVE when ready.`,
                }],
                structuredContent: ticket,
            };
        } catch (err: unknown) {
            return {
                content: [{ type: 'text', text: `❌ Failed to create ticket: ${err instanceof Error ? err.message : String(err)}` }],
                isError: true,
            };
        }
    }
);

/**
 * List tickets with optional area and status filtering.
 */
server.registerTool(
    'mester_tickets_list',
    {
        title: 'List Tickets',
        description: 'List tickets, optionally filtered by area and/or status. Returns a summary and structured JSON of all matching tickets.',
        inputSchema: {
            area:       { type: 'string',  description: 'Filter by area (e.g. "auth")' },
            status:     { type: 'string',  description: 'Filter by status: draft | active | resolved | archived',          enum: ['draft', 'active', 'resolved', 'archived'] },
            ticket_dir: { type: 'string',  description: 'Optional override: directory containing ticket JSON files' },
        },
        annotations: {
            titleForHuman: 'List tickets',
        },
    },
    async ({ area, status, ticket_dir }) => {
        const engine = createTicketEngine({
            ticketDir: resolveDir(ticket_dir || BASE_TICKET_DIR),
            indexPath: path.join(resolveDir(ticket_dir || BASE_TICKET_DIR), '_index.json'),
        });

        try {
            const tickets = engine.list(area, status);
            const filter = [area ? `area=${area}` : null, status ? `status=${status}` : null].filter(Boolean).join(', ') || 'none';
            return {
                content: [{
                    type: 'text',
                    text: `📋 Found **${tickets.length}** ticket(s)\n\n${ticketSummary(tickets)}`,
                }],
                structuredContent: {
                    count: tickets.length,
                    filter: JSON.parse(filter || '{}'),
                    tickets,
                },
            };
        } catch (err: unknown) {
            return {
                content: [{ type: 'text', text: `❌ Failed to list tickets: ${err instanceof Error ? err.message : String(err)}` }],
                isError: true,
            };
        }
    }
);

/**
 * Get a single ticket by its number.
 */
server.registerTool(
    'mester_tickets_get',
    {
        title: 'Get Ticket',
        description: 'Retrieve a single ticket by its number. Returns the full ticket data including all fields.',
        inputSchema: {
            number:     { type: 'number',  description: 'Ticket number (e.g. 15)',                           minimum: 1 },
            ticket_dir: { type: 'string',  description: 'Optional override: directory containing ticket JSON files' },
        },
        annotations: {
            titleForHuman: 'Get a specific ticket',
        },
    },
    async ({ number, ticket_dir }) => {
        const engine = createTicketEngine({
            ticketDir: resolveDir(ticket_dir || BASE_TICKET_DIR),
            indexPath: path.join(resolveDir(ticket_dir || BASE_TICKET_DIR), '_index.json'),
        });

        try {
            const tickets = engine.list(undefined, undefined);
            const ticket = tickets.find((t: Record<string, unknown>) => t.number === number);
            if (!ticket) {
                return {
                    content: [{ type: 'text', text: `❌ Ticket #${number} not found.` }],
                    isError: true,
                };
            }
            return {
                content: [{
                    type: 'text',
                    text: `📄 Ticket #${number}\n\n${formatTicket(ticket)}`,
                }],
                structuredContent: ticket,
            };
        } catch (err: unknown) {
            return {
                content: [{ type: 'text', text: `❌ Failed to get ticket: ${err instanceof Error ? err.message : String(err)}` }],
                isError: true,
            };
        }
    }
);

/**
 * Update an existing ticket.
 */
server.registerTool(
    'mester_tickets_update',
    {
        title: 'Update Ticket',
        description: 'Update an existing ticket. At least one field must be provided. Status transitions are enforced (draft→active, active→resolved/draft, resolved→draft). Returns the updated ticket.',
        inputSchema: {
            number:     { type: 'number',  description: 'Ticket number to update',                        minimum: 1 },
            area:       { type: 'string',  description: 'New area' },
            type:       { type: 'string',  description: 'New type: spike | feature | bug | improvement | doc | infra', enum: ['spike', 'feature', 'bug', 'improvement', 'doc', 'infra'] },
            status:     { type: 'string',  description: 'New status: draft | active | resolved | archived', enum: ['draft', 'active', 'resolved', 'archived'] },
            title:      { type: 'string',  description: 'New title' },
            objective:  { type: 'string',  description: 'New objective' },
            ticket_dir: { type: 'string',  description: 'Optional override: directory containing ticket JSON files' },
        },
        annotations: {
            titleForHuman: 'Update a ticket',
        },
    },
    async ({ number, ...patch }) => {
        const engine = createTicketEngine({
            ticketDir: resolveDir((patch as Record<string, unknown>).ticket_dir as string || BASE_TICKET_DIR),
            indexPath: path.join(resolveDir((patch as Record<string, unknown>).ticket_dir as string || BASE_TICKET_DIR), '_index.json'),
        });

        try {
            // Extract ticket_dir from patch before passing to update
            const { ticket_dir: _, ...updatePatch } = patch as Record<string, unknown>;
            const ticket = engine.update(number, updatePatch);
            if (!ticket) {
                return {
                    content: [{ type: 'text', text: `❌ Ticket #${number} not found.` }],
                    isError: true,
                };
            }
            return {
                content: [{
                    type: 'text',
                    text: `✏️ Ticket #${number} updated:\n\n${formatTicket(ticket)}`,
                }],
                structuredContent: ticket,
            };
        } catch (err: unknown) {
            return {
                content: [{ type: 'text', text: `❌ Failed to update ticket: ${err instanceof Error ? err.message : String(err)}` }],
                isError: true,
            };
        }
    }
);

/**
 * Archive a ticket.
 */
server.registerTool(
    'mester_tickets_archive',
    {
        title: 'Archive Ticket',
        description: 'Archive a ticket (moves it to archived status). Archived tickets are kept for history but excluded from active views.',
        inputSchema: {
            number:     { type: 'number',  description: 'Ticket number to archive',                   minimum: 1 },
            ticket_dir: { type: 'string',  description: 'Optional override: directory containing ticket JSON files' },
        },
        annotations: {
            titleForHuman: 'Archive a ticket',
        },
    },
    async ({ number, ticket_dir }) => {
        const dir = resolveDir(ticket_dir || BASE_TICKET_DIR);
        const engine = createTicketEngine({ ticketDir: dir, indexPath: path.join(dir, '_index.json') });

        try {
            const ticket = engine.archive(number);
            if (!ticket) {
                return { content: [{ type: 'text', text: `❌ Ticket #${number} not found.` }], isError: true };
            }
            return {
                content: [{ type: 'text', text: `📦 Ticket #${number} archived:\n\n${formatTicket(ticket)}` }],
                structuredContent: ticket,
            };
        } catch (err: unknown) {
            return { content: [{ type: 'text', text: `❌ Failed to archive ticket: ${err instanceof Error ? err.message : String(err)}` }], isError: true };
        }
    }
);

/**
 * Search tickets with multi-criteria filtering.
 */
server.registerTool(
    'mester_tickets_search',
    {
        title: 'Search Tickets',
        description: 'Search tickets with multiple criteria: area, type, status, and a keyword that searches the title, objective, and area fields. Returns matching tickets sorted by number.',
        inputSchema: {
            area:       { type: 'string',  description: 'Filter by area' },
            type:       { type: 'string',  description: 'Filter by type: spike | feature | bug | improvement | doc | infra', enum: ['spike', 'feature', 'bug', 'improvement', 'doc', 'infra'] },
            status:     { type: 'string',  description: 'Filter by status', enum: ['draft', 'active', 'resolved', 'archived'] },
            keyword:    { type: 'string',  description: 'Free-text keyword to search in title, objective, and area (case-insensitive)' },
            limit:      { type: 'number',  description: 'Maximum results to return', minimum: 1, maximum: 100 },
            ticket_dir: { type: 'string',  description: 'Optional override: directory containing ticket JSON files' },
        },
        annotations: {
            titleForHuman: 'Search tickets',
        },
    },
    async ({ area, type, status, keyword, limit, ticket_dir }) => {
        const engine = createTicketEngine({
            ticketDir: resolveDir(ticket_dir || BASE_TICKET_DIR),
            indexPath: path.join(resolveDir(ticket_dir || BASE_TICKET_DIR), '_index.json'),
        });

        try {
            let tickets = engine.list(area, status);

            if (keyword) {
                const kw = keyword.toLowerCase();
                tickets = tickets.filter((t: Record<string, unknown>) => {
                    const title = String(t.title ?? '').toLowerCase();
                    const objective = String(t.objective ?? '').toLowerCase();
                    const area = String(t.area ?? '').toLowerCase();
                    return title.includes(kw) || objective.includes(kw) || area.includes(kw);
                });
            }

            // Sort by number
            tickets.sort((a: Record<string, unknown>, b: Record<string, unknown>) => (a.number as number) - (b.number as number));

            // Apply limit
            if (limit && limit > 0) {
                tickets = tickets.slice(0, limit);
            }

            const searchInfo: Record<string, unknown> = {};
            if (area) searchInfo.area = area;
            if (type) searchInfo.type = type;
            if (status) searchInfo.status = status;
            if (keyword) searchInfo.keyword = keyword;

            return {
                content: [{
                    type: 'text',
                    text: `🔍 Found **${tickets.length}** result(s)${limit ? ` (of ${tickets.length} total)` : ''}\n\n${ticketSummary(tickets)}`,
                }],
                structuredContent: {
                    count: tickets.length,
                    search: searchInfo,
                    tickets,
                },
            };
        } catch (err: unknown) {
            return { content: [{ type: 'text', text: `❌ Search failed: ${err instanceof Error ? err.message : String(err)}` }], isError: true };
        }
    }
);

/**
 * Kanban board view — tickets grouped by status.
 */
server.registerTool(
    'mester_tickets_board',
    {
        title: 'Kanban Board',
        description: 'Display a Kanban-style board view of all tickets grouped by status (DRAFT, ACTIVE, RESOLVED, ARCHIVED). Useful for getting a quick overview of the current state.',
        inputSchema: {
            ticket_dir: { type: 'string',  description: 'Optional override: directory containing ticket JSON files' },
        },
        annotations: {
            titleForHuman: 'Kanban board',
        },
    },
    async ({ ticket_dir }) => {
        const dir = resolveDir(ticket_dir || BASE_TICKET_DIR);
        const engine = createTicketEngine({ ticketDir: dir, indexPath: path.join(dir, '_index.json') });

        try {
            const allTickets = engine.list();

            const statuses: Array<'draft' | 'active' | 'resolved' | 'archived'> = ['draft', 'active', 'resolved', 'archived'];
            const board: Record<string, Record<string, unknown>[]> = {};
            for (const s of statuses) board[s] = [];

            for (const ticket of allTickets) {
                const st = ticket.status as 'draft' | 'active' | 'resolved' | 'archived';
                if (board[st]) board[st].push(ticket);
            }

            // Sort each column by number
            for (const s of statuses) {
                board[s].sort((a, b) => (a.number as number) - (b.number as number));
            }

            const activeCount = board.active.length;
            const draftCount  = board.draft.length;
            const total       = allTickets.length;

            const columns = statuses
                .map(s => {
                    const count = board[s].length;
                    const icon = s === 'draft' ? '📝' : s === 'active' ? '🚀' : s === 'resolved' ? '✅' : '📦';
                    const items = board[s].map(t => `  • #${t.number} ${t.title || '(no title)'}`).join('\n');
                    return `**${icon} ${s.toUpperCase()} (${count})**\n${items || '  (empty)'}\n`;
                })
                .join('\n');

            return {
                content: [{
                    type: 'text',
                    text: `📊 **Kanban Board**\nTotal: ${total} | Active: ${activeCount} | Draft: ${draftCount}\n\n${columns}`,
                }],
                structuredContent: {
                    board,
                    summary: { total, active: activeCount, draft: draftCount },
                    columns: statuses.map(s => ({
                        status: s,
                        count: board[s].length,
                        tickets: board[s],
                    })),
                },
            };
        } catch (err: unknown) {
            return { content: [{ type: 'text', text: `❌ Board failed: ${err instanceof Error ? err.message : String(err)}` }], isError: true };
        }
    }
);

/**
 * Validate index-file consistency.
 */
server.registerTool(
    'mester_tickets_validate',
    {
        title: 'Validate',
        description: 'Validate the ticket index against the actual ticket files on disk. Reports sync status, missing files, and extra index entries.',
        inputSchema: {
            ticket_dir: { type: 'string',  description: 'Optional override: directory containing ticket JSON files' },
        },
        annotations: {
            titleForHuman: 'Validate tickets',
        },
    },
    async ({ ticket_dir }) => {
        const dir = resolveDir(ticket_dir || BASE_TICKET_DIR);
        const engine = createTicketEngine({ ticketDir: dir, indexPath: path.join(dir, '_index.json') });

        try {
            const result = engine.validate();
            if (result.synced) {
                return {
                    content: [{ type: 'text', text: '✅ Index is in sync with ticket files on disk.' }],
                    structuredContent: { synced: true },
                };
            }
            const lines = ['❌ Index is out of sync:'];
            if (result.missing_files?.length) {
                lines.push(`  Missing from index: ${result.missing_files.join(', ')}`);
            }
            if (result.extra_files?.length) {
                lines.push(`  Extra in index: ${result.extra_files.join(', ')}`);
            }
            return {
                content: [{ type: 'text', text: lines.join('\n') }],
                structuredContent: { synced: false, missing: result.missing_files, extra: result.extra_files },
                isError: true,
            };
        } catch (err: unknown) {
            return { content: [{ type: 'text', text: `❌ Validation failed: ${err instanceof Error ? err.message : String(err)}` }], isError: true };
        }
    }
);

// ── Start ──────────────────────────────────────────────────────────

async function main() {
    const transport = new StdioServerTransport();
    await server.connect(transport);
    // eslint-disable-next-line no-console
    console.error(`✅ Little Mester MCP server running. Ticket dir: ${BASE_TICKET_DIR}`);
}

main().catch((err) => {
    // eslint-disable-next-line no-console
    console.error('Failed to start MCP server:', err);
    process.exit(1);
});
