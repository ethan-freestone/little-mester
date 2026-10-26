#!/usr/bin/env node
/**
 * Little Mester MCP Server
 *
 * Central Jira-like ticketing hub for both humans and agents.
 *
 * This is the FIRST MODULE of Little Mester MCP.
 * Package: @little-mester/ticket-engine-mcp (this directory: little-mester-mcp/)
 * Future modules (file search, notes, etc.) will live alongside in the same server process.
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

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { createTicketEngine, Ticket } from './engine.js';
import * as path from 'path';
import { z } from 'zod';

// ── Configuration ──────────────────────────────────────────────────

const TICKET_DIR = process.env.TICKET_DIR || './docs/tickets';

function resolveDir(raw: string): string {
    return path.resolve(raw);
}

function parseCliArg(name: string): string | undefined {
    const flag = `--${name}=`;
    for (const arg of process.argv) {
        if (arg.startsWith(flag)) return arg.slice(flag.length);
    }
    return undefined;
}

const CLI_TICKET_DIR = parseCliArg('ticket-dir');
const BASE_TICKET_DIR = resolveDir(CLI_TICKET_DIR || TICKET_DIR);

// Single server instance — the ticket engine is the first module
// Future modules will register their tools on this same server
const server = new McpServer({
    name: 'little-mester',
    version: '0.1.0',
});

// ── Helpers ────────────────────────────────────────────────────────

const acceptanceCriterionSchema = z.object({
    id: z.string().min(1).optional().describe('Stable criterion id (A1, A2, …). Auto-assigned when omitted.'),
    text: z.string().min(1).describe('What must be true for this criterion to be met'),
    status: z.enum(['pending', 'in-progress', 'test-needed', 'test-created', 'passing', 'waived'])
        .optional()
        .describe('Verification state. passing requires evidence; waived requires note.'),
    evidence: z.string().min(1).optional().describe('Test path/case, command + result, or observed artifact (required for passing)'),
    note: z.string().min(1).optional().describe('Why no test is needed/possible (required for waived)'),
});

const testsSchema = z.object({
    needed: z.array(z.string().min(1)).optional().describe('Tests that still need to be written'),
    created: z.array(z.string().min(1)).optional().describe('Test files/cases that exist'),
    cases: z.array(z.string().min(1)).optional().describe('Named cases covered, including manual observations'),
});

function formatAcceptanceCriteria(ticket: Ticket): string {
    const criteria = ticket.acceptance_criteria ?? [];
    if (criteria.length === 0) return '';
    const lines = [`\nAcceptance criteria (${criteria.length}):`];
    for (const ac of criteria) {
        lines.push(`  ${ac.id} [${ac.status}] ${ac.text}`);
    }
    return lines.join('\n');
}

function formatTests(ticket: Ticket): string {
    const tests = ticket.tests;
    if (!tests) return '';
    const counts = `needed ${tests.needed.length} | created ${tests.created.length} | cases ${tests.cases.length}`;
    return `\nTests: ${counts}`;
}

function formatTicket(ticket: Ticket): string {
    const num = ticket.number ?? ticket.id ?? '?';
    const status = ticket.status ?? '?';
    const type = ticket.type ?? '?';
    const area = ticket.area ?? '';
    const title = ticket.title ?? '(no title)';
    const created = ticket.created_at ? new Date(ticket.created_at).toLocaleDateString() : '—';
    const updated = ticket.updated_at ? new Date(ticket.updated_at).toLocaleDateString() : '—';

    let line = `**#${num}** — [${status.toUpperCase()}] ${type} — ${area || 'no area'}\n`;
    line += `${title}\n`;
    if (ticket.objective) line += `> ${ticket.objective}\n`;
    line += `Created: ${created}  |  Updated: ${updated}`;
    line += formatAcceptanceCriteria(ticket);
    line += formatTests(ticket);
    return line;
}

function ticketSummary(tickets: Ticket[]): string {
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
        inputSchema: z.object({
            area: z.string().optional().describe('Area or subsystem (e.g. "auth", "frontend", "infra")'),
            type: z.enum(['spike', 'feature', 'bug', 'improvement', 'doc', 'infra']).optional().describe('Ticket type'),
            title: z.string().min(1).describe('Short descriptive title for the ticket'),
            objective: z.string().optional().describe('One-sentence objective'),
            acceptance_criteria: z.array(z.union([z.string().min(1), acceptanceCriterionSchema]))
                .min(1)
                .describe('Required: at least one acceptance criterion. Strings get auto ids and pending status.'),
            tests: testsSchema.optional().describe('Test ledger: needed/created/cases. Arrays may be empty.'),
            ticket_dir: z.string().optional().describe('Optional override: directory containing ticket JSON files (absolute or relative path)'),
        }),
    },
    async ({ area, type, title, objective, acceptance_criteria, tests, ticket_dir }) => {
        const engine = createTicketEngine({
            ticketDir: resolveDir(ticket_dir || BASE_TICKET_DIR),
            indexPath: path.join(resolveDir(ticket_dir || BASE_TICKET_DIR), '_index.json'),
        });

        try {
            const ticket = engine.create({ area, type, title, objective, acceptance_criteria, tests });
            return {
                content: [{
                    type: 'text',
                    text: `✅ Created ticket #${ticket.number}:\n\n${formatTicket(ticket)}`,
                }],
                structuredContent: ticket as Record<string, unknown>,
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
 * List tickets with optional filters.
 */
server.registerTool(
    'mester_tickets_list',
    {
        title: 'List Tickets',
        description: 'List tickets, optionally filtered by area and/or status. Returns a summary and structured JSON of all matching tickets.',
        inputSchema: z.object({
            area: z.string().optional().describe('Filter by area (e.g. "auth")'),
            status: z.enum(['draft', 'active', 'resolved', 'archived']).optional().describe('Filter by status'),
            ticket_dir: z.string().optional().describe('Optional override: directory containing ticket JSON files'),
        }),
    },
    async ({ area, status, ticket_dir }) => {
        const engine = createTicketEngine({
            ticketDir: resolveDir(ticket_dir || BASE_TICKET_DIR),
            indexPath: path.join(resolveDir(ticket_dir || BASE_TICKET_DIR), '_index.json'),
        });

        try {
            const tickets = engine.list(area, status);
            const filter = [area ? `area=${area}` : null, status ? `status=${status}` : null].filter(Boolean).join(', ');
            const filterText = filter ? ` (filtered: ${filter})` : '';
            return {
                content: [{
                    type: 'text',
                    text: `📋 Found ${tickets.length} ticket(s)${filterText}:\n\n${ticketSummary(tickets)}`,
                }],
                structuredContent: { tickets: tickets as Record<string, unknown>[], count: tickets.length },
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
 * Get a single ticket by number.
 */
server.registerTool(
    'mester_tickets_get',
    {
        title: 'Get Ticket',
        description: 'Get a single ticket by its number. Returns full ticket details.',
        inputSchema: z.object({
            number: z.number().int().positive().describe('Ticket number to retrieve'),
            ticket_dir: z.string().optional().describe('Optional override: directory containing ticket JSON files'),
        }),
    },
    async ({ number, ticket_dir }) => {
        const engine = createTicketEngine({
            ticketDir: resolveDir(ticket_dir || BASE_TICKET_DIR),
            indexPath: path.join(resolveDir(ticket_dir || BASE_TICKET_DIR), '_index.json'),
        });

        try {
            const tickets = engine.list(undefined, undefined);
            const ticket = tickets.find((t: Ticket) => t.number === number);
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
                structuredContent: ticket as Record<string, unknown>,
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
        description: 'Update fields on an existing ticket. Only provided fields are changed; others remain as-is. Status transitions follow DECISIONS.md D.3. acceptance_criteria replaces the array; tests arrays are merged into the existing ledger.',
        inputSchema: z.object({
            number: z.number().int().positive().describe('Ticket number to update'),
            title: z.string().min(1).optional().describe('New title'),
            objective: z.string().optional().describe('New objective'),
            status: z.enum(['draft', 'active', 'resolved', 'archived']).optional().describe('New status'),
            area: z.string().optional().describe('New area'),
            type: z.enum(['spike', 'feature', 'bug', 'improvement', 'doc', 'infra']).optional().describe('New type'),
            priority: z.enum(['trivial', 'minor', 'major', 'critical']).optional().describe('New priority'),
            acceptance_criteria: z.array(z.union([z.string().min(1), acceptanceCriterionSchema]))
                .min(1)
                .optional()
                .describe('Replace the full acceptance-criteria array. Include existing criteria to keep them.'),
            tests: testsSchema.optional().describe('Test ledger entries to add; arrays are merged, not replaced.'),
            ticket_dir: z.string().optional().describe('Optional override: directory containing ticket JSON files'),
        }),
    },
    async ({ number, ticket_dir, ...patch }) => {
        const engine = createTicketEngine({
            ticketDir: resolveDir(ticket_dir || BASE_TICKET_DIR),
            indexPath: path.join(resolveDir(ticket_dir || BASE_TICKET_DIR), '_index.json'),
        });

        try {
            const ticket = engine.update(number, patch);
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
                structuredContent: ticket as Record<string, unknown>,
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
 * Archive a ticket (sets status to archived).
 */
server.registerTool(
    'mester_tickets_archive',
    {
        title: 'Archive Ticket',
        description: 'Archive a ticket by setting its status to ARCHIVED. Per DECISIONS.md D.3, archiving is allowed from ANY status.',
        inputSchema: z.object({
            number: z.number().int().positive().describe('Ticket number to archive'),
            ticket_dir: z.string().optional().describe('Optional override: directory containing ticket JSON files'),
        }),
    },
    async ({ number, ticket_dir }) => {
        const engine = createTicketEngine({
            ticketDir: resolveDir(ticket_dir || BASE_TICKET_DIR),
            indexPath: path.join(resolveDir(ticket_dir || BASE_TICKET_DIR), '_index.json'),
        });

        try {
            const ticket = engine.archive(number);
            if (!ticket) {
                return {
                    content: [{ type: 'text', text: `❌ Ticket #${number} not found.` }],
                    isError: true,
                };
            }
            return {
                content: [{
                    type: 'text',
                    text: `📦 Ticket #${number} archived:\n\n${formatTicket(ticket)}`,
                }],
                structuredContent: ticket as Record<string, unknown>,
            };
        } catch (err: unknown) {
            return {
                content: [{ type: 'text', text: `❌ Failed to archive ticket: ${err instanceof Error ? err.message : String(err)}` }],
                isError: true,
            };
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
        inputSchema: z.object({
            area: z.string().optional().describe('Filter by area'),
            type: z.enum(['spike', 'feature', 'bug', 'improvement', 'doc', 'infra']).optional().describe('Filter by type'),
            status: z.enum(['draft', 'active', 'resolved', 'archived']).optional().describe('Filter by status'),
            keyword: z.string().optional().describe('Free-text keyword to search in title, objective, and area (case-insensitive)'),
            limit: z.number().int().min(1).max(100).optional().describe('Maximum results to return'),
            ticket_dir: z.string().optional().describe('Optional override: directory containing ticket JSON files'),
        }),
    },
    async ({ area, type, status, keyword, limit, ticket_dir }) => {
        const engine = createTicketEngine({
            ticketDir: resolveDir(ticket_dir || BASE_TICKET_DIR),
            indexPath: path.join(resolveDir(ticket_dir || BASE_TICKET_DIR), '_index.json'),
        });

        try {
            let tickets = engine.list(area, status);

            if (type) {
                tickets = tickets.filter((t: Ticket) => t.type === type);
            }

            if (keyword) {
                const kw = keyword.toLowerCase();
                tickets = tickets.filter((t: Ticket) => {
                    const title = String(t.title ?? '').toLowerCase();
                    const objective = String(t.objective ?? '').toLowerCase();
                    const area = String(t.area ?? '').toLowerCase();
                    const criteria = (t.acceptance_criteria ?? []).map(ac => ac.text).join(' ').toLowerCase();
                    return title.includes(kw) || objective.includes(kw) || area.includes(kw) || criteria.includes(kw);
                });
            }

            // Sort by number
            tickets.sort((a: Ticket, b: Ticket) => (a.number as number) - (b.number as number));

            if (limit) {
                tickets = tickets.slice(0, limit);
            }

            const criteria = [area ? `area=${area}` : null, type ? `type=${type}` : null, status ? `status=${status}` : null, keyword ? `keyword="${keyword}"` : null].filter(Boolean).join(', ');
            const criteriaText = criteria ? ` (${criteria})` : '';
            return {
                content: [{
                    type: 'text',
                    text: `🔍 Found ${tickets.length} ticket(s)${criteriaText}:\n\n${ticketSummary(tickets)}`,
                }],
                structuredContent: { tickets: tickets as Record<string, unknown>[], count: tickets.length },
            };
        } catch (err: unknown) {
            return {
                content: [{ type: 'text', text: `❌ Failed to search tickets: ${err instanceof Error ? err.message : String(err)}` }],
                isError: true,
            };
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
        description: 'Display a Kanban-style board view of all tickets grouped by status (DRAFT, ACTIVE, RESOLVED, ARCHIVED). Useful for getting a quick overview of the current state. Sorted by priority (critical→major→minor→trivial) then updated_at descending per DECISIONS.md D.4/H.',
        inputSchema: z.object({
            ticket_dir: z.string().optional().describe('Optional override: directory containing ticket JSON files'),
        }),
    },
    async ({ ticket_dir }) => {
        const dir = resolveDir(ticket_dir || BASE_TICKET_DIR);
        const engine = createTicketEngine({ ticketDir: dir, indexPath: path.join(dir, '_index.json') });

        try {
            const allTickets = engine.list();

            const statuses: Array<'draft' | 'active' | 'resolved' | 'archived'> = ['draft', 'active', 'resolved', 'archived'];
            const board: Record<string, Ticket[]> = {};
            for (const s of statuses) board[s] = [];

            for (const ticket of allTickets) {
                const st = ticket.status as 'draft' | 'active' | 'resolved' | 'archived';
                if (board[st]) board[st].push(ticket);
            }

            // Sort each column by priority (critical → major → minor → trivial), then updated_at descending
            // Per DECISIONS D.4/H: priority is primary sort for board views
            const priorityOrder: Record<string, number> = { critical: 0, major: 1, minor: 2, trivial: 3 };
            for (const s of statuses) {
                board[s].sort((a, b) => {
                    const pa = priorityOrder[(a as Ticket).priority as string] ?? 4;
                    const pb = priorityOrder[(b as Ticket).priority as string] ?? 4;
                    if (pa !== pb) return pa - pb;
                    // Secondary sort: updated_at descending (most recent first)
                    const ua = new Date((a as Ticket).updated_at as string).getTime();
                    const ub = new Date((b as Ticket).updated_at as string).getTime();
                    return ub - ua;
                });
            }

            const lines: string[] = [];
            for (const s of statuses) {
                const tickets = board[s];
                lines.push(`## ${s.toUpperCase()} (${tickets.length})`);
                if (tickets.length === 0) {
                    lines.push('*(empty)*');
                } else {
                    lines.push(tickets.map(formatTicket).join('\n\n'));
                }
                lines.push('');
            }

            return {
                content: [{
                    type: 'text',
                    text: lines.join('\n'),
                }],
                structuredContent: board as Record<string, Record<string, unknown>[]>,
            };
        } catch (err: unknown) {
            return {
                content: [{ type: 'text', text: `❌ Failed to get board: ${err instanceof Error ? err.message : String(err)}` }],
                isError: true,
            };
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
        description: 'Validate the ticket store: index-file sync, audit/lock/orphan enforcement, and the acceptance-criteria/test-ledger policy. Returns isError when any check fails.',
        inputSchema: z.object({
            ticket_dir: z.string().optional().describe('Optional override: directory containing ticket JSON files'),
        }),
    },
    async ({ ticket_dir }) => {
        const dir = resolveDir(ticket_dir || BASE_TICKET_DIR);
        const engine = createTicketEngine({ ticketDir: dir, indexPath: path.join(dir, '_index.json') });

        try {
            const result = engine.validate();
            const lines: string[] = [];

            if (result.synced) {
                lines.push('✅ Index is in sync with ticket files on disk.');
            } else {
                lines.push('❌ Index is OUT OF SYNC with ticket files on disk.', '');
                if (result.missing_files?.length) {
                    lines.push(`Missing files (on disk but not in index): ${result.missing_files.join(', ')}`);
                }
                if (result.extra_files?.length) {
                    lines.push(`Extra index entries (in index but not on disk): ${result.extra_files.join(', ')}`);
                }
            }

            lines.push(`Lock: ${result.enforcement.lock.message}`);
            lines.push(`Audit: ${result.enforcement.audit.messages.join(' ')}`);
            if (result.enforcement.has_orphans) {
                lines.push(`Orphans: ${result.enforcement.orphan_files.length} ticket file(s) with no audit entry — human review required.`);
            } else {
                lines.push('Orphans: none.');
            }

            const quality = result.quality;
            const errors = quality.issues.filter(issue => issue.severity === 'error');
            const warnings = quality.issues.filter(issue => issue.severity === 'warning');
            lines.push(
                errors.length === 0
                    ? `Acceptance criteria: ${quality.tickets_checked} ticket(s) checked; all have criteria + test ledgers.`
                    : `Acceptance criteria: ${errors.length} error(s) across ${quality.tickets_checked} ticket(s).`
            );
            for (const issue of quality.issues) {
                lines.push(`  ${issue.severity === 'error' ? '❌' : '⚠️'} #${String(issue.ticket_number).padStart(3, '0')} [${issue.field}] ${issue.message}`);
            }
            if (warnings.length > 0 && errors.length === 0) {
                lines.push(`⚠️ ${warnings.length} warning(s) — drift is visible but not blocking.`);
            }

            return {
                content: [{ type: 'text', text: lines.join('\n') }],
                isError: !result.ok,
                structuredContent: {
                    ok: result.ok,
                    synced: result.synced,
                    missing: result.missing_files,
                    extra: result.extra_files,
                    enforcement: result.enforcement as unknown as Record<string, unknown>,
                    quality: result.quality as unknown as Record<string, unknown>,
                },
            };
        } catch (err: unknown) {
            return {
                content: [{ type: 'text', text: `❌ Failed to validate: ${err instanceof Error ? err.message : String(err)}` }],
                isError: true,
            };
        }
    }
);

// ── Main ───────────────────────────────────────────────────────────

async function main() {
    const transport = new StdioServerTransport();
    await server.connect(transport);
    console.error('Little Mester MCP Server running on stdio');
}

main().catch((err) => {
    console.error('Fatal error:', err);
    process.exit(1);
});