#!/usr/bin/env node
/**
 * Little Mester Ticket Engine CLI
 *
 * Wires every command to src/engine.ts (createTicketEngine).
 *
 * Usage:
 *   ticket-engine create --area <area> --type <type> "Title" [--objective "..."]
 *   ticket-engine list [--area <area>] [--status <status>]
 *   ticket-engine update <number> [--status <status>] [--title <title>] [--objective <obj>] [--area <area>] [--type <type>] [--priority <priority>]
 *   ticket-engine archive <number>
 *   ticket-engine validate
 *
 * Options:
 *   --ticket-dir <dir>   Directory containing ticket JSON files (default: ./docs/tickets or TICKET_DIR env)
 *   -h, --help           Show this help
 */

import {
    createTicketEngine,
    type AcceptanceCriteriaInput,
    type AcceptanceCriterionInput,
    type Ticket,
    type TicketPatch,
} from './engine';
import * as path from 'path';

type CliArgs = {
    command?: string;
    ticketDir?: string;
    positionals: string[];
    help?: boolean;
    area?: string;
    type?: string;
    status?: string;
    title?: string;
    objective?: string;
    priority?: string;
    comment?: string;
    /** Repeatable --ac "<criterion>" (append on update). */
    ac: string[];
    /** Repeatable --ac-status <id>=<status>. */
    acStatus: string[];
    /** Repeatable --ac-evidence <id>=<evidence>. */
    acEvidence: string[];
    /** Repeatable test-ledger flags (append on update). */
    testNeeded: string[];
    testCreated: string[];
    testCase: string[];
    limit?: number;
    json?: boolean;
    baseline?: boolean;
};

const PRIORITIES = ['trivial', 'minor', 'major', 'critical'] as const;
type Priority = typeof PRIORITIES[number];

/** Validate a --priority value and return it with a real union type (no casts to never). */
function parsePriority(value?: string): Priority | undefined {
    if (value === undefined) return undefined;
    if (!(PRIORITIES as readonly string[]).includes(value)) {
        throw new Error(`Invalid priority '${value}'. Must be one of: ${PRIORITIES.join(', ')}`);
    }
    return value as Priority;
}

/** Parse `id=value` flags used by --ac-status / --ac-evidence. */
function parseKeyValue(value: string, flag: string): { key: string; value: string } {
    const eq = value.indexOf('=');
    if (eq <= 0) {
        throw new Error(`${flag} expects <criterion-id>=<value>, got '${value}'`);
    }
    return { key: value.slice(0, eq), value: value.slice(eq + 1) };
}

function testsPatch(args: CliArgs): { needed: string[]; created: string[]; cases: string[] } | undefined {
    if (args.testNeeded.length === 0 && args.testCreated.length === 0 && args.testCase.length === 0) {
        return undefined;
    }
    return {
        needed: args.testNeeded,
        created: args.testCreated,
        cases: args.testCase,
    };
}

function usage(): string {
    return `Little Mester Ticket Engine CLI

Usage:
  ticket-engine create --area <area> --type <type> --ac "<criterion>" "Title"
                       [--ac "<criterion>" ...] [--objective "..."] [--priority <priority>]
                       [--test-needed "..."] [--test-created "..."] [--test-case "..."] [--comment "..."]
  ticket-engine list [--area <area>] [--status <status>]
  ticket-engine update <number> [--status <status>] [--title <title>] [--objective <obj>]
                       [--area <area>] [--type <type>] [--priority <priority>]
                       [--ac "<criterion>"] [--ac-status <id>=<status>] [--ac-evidence <id>=<evidence>]
                       [--test-needed "..."] [--test-created "..."] [--test-case "..."] [--comment "..."]
  ticket-engine archive <number>
  ticket-engine audit [--limit N] [--json] [--baseline]
  ticket-engine validate

Options:
  --ticket-dir <dir>   Directory containing ticket JSON files (default: ./docs/tickets or TICKET_DIR env)
  --ac <text>          Acceptance criterion (repeatable; at least one required on create)
  --ac-status <id>=<s> Update one criterion's status (pending|in-progress|test-needed|test-created|passing|waived)
  --ac-evidence <id>=<text>  Set evidence for a criterion (required when status=passing)
  --test-needed <text> Add a test that still needs to be written (repeatable)
  --test-created <text> Add a test that exists (repeatable)
  --test-case <text>   Add a named test case/observed check (repeatable)
  --comment <text>     Add a comment to the ticket (create or update)
  --priority <p>       trivial | minor | major | critical
  --limit <n>          audit: show only the last N entries
  --json               audit: print the raw JSON array
  --baseline           audit: adopt un-audited ticket files as baseline entries
  -h, --help           Show this help
`;
}

function parseArgs(argv: string[]): CliArgs {
    const args: CliArgs = {
        positionals: [],
        ac: [],
        acStatus: [],
        acEvidence: [],
        testNeeded: [],
        testCreated: [],
        testCase: [],
    };
    let i = 0;
    while (i < argv.length) {
        const arg = argv[i];
        if (arg === '--ticket-dir' && i + 1 < argv.length) {
            args.ticketDir = argv[++i];
        } else if (arg === '-h' || arg === '--help') {
            args.help = true;
        } else if (arg.startsWith('--')) {
            const eq = arg.indexOf('=');
            const key = arg.slice(2, eq !== -1 ? eq : arg.length);
            const val = eq !== -1 ? arg.slice(eq + 1) : (i + 1 < argv.length && !argv[i + 1].startsWith('-') ? argv[++i] : true);
            if (key === 'area')        args.area = val as string;
            else if (key === 'type')   args.type = val as string;
            else if (key === 'status') args.status = val as string;
            else if (key === 'title')  args.title = val as string;
            else if (key === 'objective') args.objective = val as string;
            else if (key === 'priority') args.priority = val as string;
            else if (key === 'comment')  args.comment = val as string;
            else if (key === 'ac')       args.ac.push(String(val));
            else if (key === 'ac-status') args.acStatus.push(String(val));
            else if (key === 'ac-evidence') args.acEvidence.push(String(val));
            else if (key === 'test-needed') args.testNeeded.push(String(val));
            else if (key === 'test-created') args.testCreated.push(String(val));
            else if (key === 'test-case') args.testCase.push(String(val));
            else if (key === 'ticket-dir') args.ticketDir = val as string;
            else if (key === 'limit') args.limit = Number.parseInt(String(val), 10);
            else if (key === 'json') args.json = true;
            else if (key === 'baseline') args.baseline = true;
        } else {
            args.positionals.push(arg);
        }
        i++;
    }
    if (args.positionals.length > 0) {
        args.command = args.positionals[0];
    }
    return args;
}

function getTicketDir(args: CliArgs): string {
    const dir = (args.ticketDir as string | undefined) || process.env.TICKET_DIR || './docs/tickets';
    return path.resolve(dir);
}

function formatTicket(t: Ticket): string {
    const num = t.number ?? t.id ?? '?';
    const status = t.status ?? '?';
    const type = t.type ?? '?';
    const area = t.area ?? '(no area)';
    const title = t.title ?? '(no title)';
    return `  #${num} [${status.toUpperCase()}] ${type} — ${area} — ${title}`;
}

async function main() {
    const argv = process.argv.slice(2);
    const args = parseArgs(argv);

    if (args.help || !args.command) {
        console.log(usage());
        process.exit(args.help ? 0 : 1);
    }

    const ticketDir = getTicketDir(args);
    const indexPath = path.join(ticketDir, '_index.json');
    const engine = createTicketEngine({ ticketDir, indexPath });

    switch (args.command) {
        case 'create': {
            const title = args.positionals[1];
            if (!title) {
                console.error('Error: create requires a title argument');
                console.log(usage());
                process.exit(1);
            }
            if (args.ac.length === 0) {
                console.error('Error: create requires at least one --ac "<acceptance criterion>"');
                console.log(usage());
                process.exit(1);
            }
            try {
                const ticket = engine.create({
                    area: args.area,
                    type: args.type || undefined,
                    title: title,
                    objective: args.objective || undefined,
                    priority: parsePriority(args.priority),
                    acceptance_criteria: args.ac,
                    tests: testsPatch(args),
                    comments: args.comment ? [{
                        timestamp: new Date().toISOString(),
                        author: 'cli',
                        body: args.comment,
                    }] : undefined,
                });
                console.log(`✅ Created ticket #${ticket.number}`);
                console.log(formatTicket(ticket));
                if (ticket.objective) {
                    console.log(`   Objective: ${ticket.objective}`);
                }
                console.log(`   Acceptance criteria: ${ticket.acceptance_criteria?.length ?? 0}`);
                if (ticket.tests && (ticket.tests.needed.length || ticket.tests.created.length || ticket.tests.cases.length)) {
                    console.log(
                        `   Tests: ${ticket.tests.needed.length} needed, ` +
                        `${ticket.tests.created.length} created, ${ticket.tests.cases.length} cases`
                    );
                }
                if (args.comment) {
                    console.log(`   Comment: ${args.comment}`);
                }
            } catch (err: unknown) {
                const msg = err instanceof Error ? err.message : String(err);
                console.error(`❌ Failed to create ticket: ${msg}`);
                process.exit(1);
            }
            break;
        }

        case 'list': {
            const tickets = engine.list(args.area as string | undefined, args.status as string | undefined);
            if (tickets.length === 0) {
                console.log('No tickets found.');
            } else {
                console.log(`Found ${tickets.length} ticket(s):`);
                for (const t of tickets) {
                    console.log(formatTicket(t));
                }
            }
            break;
        }

        case 'update': {
            const number = parseInt(args.positionals[1] as string, 10);
            if (!number) {
                console.error('Error: update requires a ticket number');
                process.exit(1);
            }
            try {
                const existing = engine.list().find(t => t.number === number);
                const patch: TicketPatch = {};
                if (args.status)   patch.status = args.status as Ticket['status'];
                if (args.title)    patch.title = args.title;
                if (args.objective) patch.objective = args.objective;
                if (args.area)     patch.area = args.area;
                if (args.type)     patch.type = args.type as Ticket['type'];
                const priority = parsePriority(args.priority);
                if (priority)      patch.priority = priority;

                if (args.ac.length || args.acStatus.length || args.acEvidence.length) {
                    if (!existing) {
                        console.error(`❌ Ticket #${number} not found`);
                        process.exit(1);
                    }
                    const criteria: AcceptanceCriteriaInput = existing.acceptance_criteria
                        ? [...existing.acceptance_criteria]
                        : [];

                    if (args.ac.length) criteria.push(...args.ac);
                    for (const spec of args.acStatus) {
                        const { key, value } = parseKeyValue(spec, '--ac-status');
                        const idx = criteria.findIndex(c =>
                            typeof c === 'object' && c !== null && c.id === key
                        );
                        if (idx < 0) throw new Error(`Acceptance criterion '${key}' not found`);
                        criteria[idx] = { ...(criteria[idx] as AcceptanceCriterionInput), status: value };
                    }
                    for (const spec of args.acEvidence) {
                        const { key, value } = parseKeyValue(spec, '--ac-evidence');
                        const idx = criteria.findIndex(c =>
                            typeof c === 'object' && c !== null && c.id === key
                        );
                        if (idx < 0) throw new Error(`Acceptance criterion '${key}' not found`);
                        criteria[idx] = { ...(criteria[idx] as AcceptanceCriterionInput), evidence: value };
                    }
                    patch.acceptance_criteria = criteria;
                }

                const tests = testsPatch(args);
                if (tests) patch.tests = tests;

                if (args.comment) {
                    // Append a comment to existing comments array
                    patch.comments = [...(existing?.comments ?? []), {
                        timestamp: new Date().toISOString(),
                        author: 'cli',
                        body: args.comment,
                    }];
                }
                const ticket = engine.update(number, patch);
                if (!ticket) {
                    console.error(`❌ Ticket #${number} not found`);
                    process.exit(1);
                }
                console.log(`✏️ Ticket #${number} updated:`);
                console.log(formatTicket(ticket));
                if (ticket.acceptance_criteria?.length) {
                    const counts = ticket.acceptance_criteria.reduce<Record<string, number>>((acc, c) => {
                        acc[c.status] = (acc[c.status] ?? 0) + 1;
                        return acc;
                    }, {});
                    console.log(
                        `   Acceptance criteria: ${ticket.acceptance_criteria.length} (` +
                        Object.entries(counts).map(([status, count]) => `${status}=${count}`).join(', ') +
                        ')'
                    );
                }
                if (args.comment) {
                    console.log(`   Comment: ${args.comment}`);
                }
            } catch (err: unknown) {
                const msg = err instanceof Error ? err.message : String(err);
                console.error(`❌ Failed to update ticket: ${msg}`);
                process.exit(1);
            }
            break;
        }

        case 'archive': {
            const number = parseInt(args.positionals[1] as string, 10);
            if (!number) {
                console.error('Error: archive requires a ticket number');
                process.exit(1);
            }
            try {
                const ticket = engine.archive(number);
                if (!ticket) {
                    console.error(`❌ Ticket #${number} not found`);
                    process.exit(1);
                }
                console.log(`📦 Ticket #${number} archived:`);
                console.log(formatTicket(ticket));
            } catch (err: unknown) {
                const msg = err instanceof Error ? err.message : String(err);
                console.error(`❌ Failed to archive ticket: ${msg}`);
                process.exit(1);
            }
            break;
        }

        case 'audit': {
            if (args.limit !== undefined && (!Number.isInteger(args.limit) || args.limit <= 0)) {
                console.error('Error: --limit requires a positive integer');
                process.exit(1);
            }
            try {
                if (args.baseline) {
                    const added = engine.baseline();
                    if (added.length === 0) {
                        console.log('No un-audited ticket files found; baseline unchanged.');
                    } else {
                        console.log(`✅ Adopted ${added.length} ticket file(s) as baseline audit entries.`);
                        for (const entry of added) {
                            console.log(`   + #${entry.ticket_number} baseline`);
                        }
                    }
                }

                const entries = engine.getAuditLog();
                const shown = args.limit ? entries.slice(-args.limit) : entries;

                if (args.json) {
                    console.log(JSON.stringify(shown, null, 2));
                    break;
                }

                if (shown.length === 0) {
                    console.log('Audit log is empty.');
                    break;
                }

                const count = shown.length < entries.length
                    ? ` (last ${shown.length} of ${entries.length} entries)`
                    : ` (${entries.length} entries)`;
                console.log(`Audit log${count}:`);
                for (const entry of shown) {
                    console.log(
                        `  ${entry.timestamp}  ${entry.operation.padEnd(8)} ` +
                        `#${String(entry.ticket_number).padStart(3, '0')}  ${entry.hash.slice(0, 12)}…`
                    );
                }
            } catch (err: unknown) {
                const msg = err instanceof Error ? err.message : String(err);
                console.error(`❌ Failed to read audit log: ${msg}`);
                process.exit(1);
            }
            break;
        }

        case 'validate': {
            const result = engine.validate();
            const enforcement = result.enforcement;

            if (result.synced) {
                console.log('✅ Index is in sync with ticket files on disk.');
            } else {
                console.log('❌ Index is OUT OF SYNC with ticket files on disk.');
                if (result.missing_files?.length) {
                    console.log(`   Missing files: ${result.missing_files.join(', ')}`);
                }
                if (result.extra_files?.length) {
                    console.log(`   Extra index entries: ${result.extra_files.join(', ')}`);
                }
            }

            const lockIcon = enforcement.lock.state === 'stale' ? '❌'
                : enforcement.lock.state === 'held' ? '⏳' : '✅';
            console.log(`${lockIcon} Lock: ${enforcement.lock.message}`);

            const auditIcon = enforcement.audit.valid ? '✅' : '❌';
            console.log(`${auditIcon} Audit: ${enforcement.audit.messages.join(' ')}`);

            if (enforcement.has_orphans) {
                console.log(`❌ Orphans: ${enforcement.orphan_files.length} ticket file(s) on disk with no audit entry — human review required.`);
                for (const file of enforcement.orphan_files) {
                    console.log(`   ↳ ${path.basename(file)}`);
                }
            } else {
                console.log('✅ Orphans: none.');
            }

            const quality = result.quality;
            const errors = quality.issues.filter(issue => issue.severity === 'error');
            const warnings = quality.issues.filter(issue => issue.severity === 'warning');
            if (errors.length === 0) {
                console.log(`✅ Acceptance criteria: ${quality.tickets_checked} ticket(s) checked; all have criteria + test ledgers.`);
            } else {
                console.log(`❌ Acceptance criteria: ${errors.length} error(s) across ${quality.tickets_checked} ticket(s).`);
            }
            for (const issue of quality.issues) {
                const icon = issue.severity === 'error' ? '❌' : '⚠️';
                console.log(`   ${icon} #${String(issue.ticket_number).padStart(3, '0')} [${issue.field}] ${issue.message}`);
            }
            if (warnings.length > 0 && errors.length === 0) {
                console.log(`   ⚠️ ${warnings.length} warning(s) — drift is visible but not blocking.`);
            }

            if (!result.ok) process.exit(1);
            break;
        }

        default:
            console.error(`Error: unknown command '${args.command}'`);
            console.log(usage());
            process.exit(1);
    }
}

main().catch((err) => {
    console.error(`Fatal error: ${err}`);
    process.exit(1);
});
