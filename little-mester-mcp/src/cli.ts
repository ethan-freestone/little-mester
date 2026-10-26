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

import { createTicketEngine, type Ticket } from './engine';
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
};

function usage(): string {
    return `Little Mester Ticket Engine CLI

Usage:
  ticket-engine create --area <area> --type <type> "Title" [--objective "..."]
  ticket-engine list [--area <area>] [--status <status>]
  ticket-engine update <number> [--status <status>] [--title <title>] [--objective <obj>] [--area <area>] [--type <type>] [--priority <priority>]
  ticket-engine archive <number>
  ticket-engine validate

Options:
  --ticket-dir <dir>   Directory containing ticket JSON files (default: ./docs/tickets or TICKET_DIR env)
  -h, --help           Show this help
`;
}

function parseArgs(argv: string[]): CliArgs {
    const args: CliArgs = { positionals: [] };
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
            else if (key === 'ticket-dir') args.ticketDir = val as string;
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
            try {
                const ticket = engine.create({
                    area: args.area as string | undefined,
                    type: (args.type as string | undefined) ?? undefined,
                    title: title as string,
                    objective: args.objective as string | undefined,
                });
                console.log(`✅ Created ticket #${ticket.number}`);
                console.log(formatTicket(ticket));
                if (ticket.objective) {
                    console.log(`   Objective: ${ticket.objective}`);
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
            const patch: Record<string, unknown> = {};
            if (args.status)   patch.status = args.status;
            if (args.title)    patch.title = args.title;
            if (args.objective) patch.objective = args.objective;
            if (args.area)     patch.area = args.area;
            if (args.type)     patch.type = args.type;
            if (args.priority) patch.priority = args.priority;
            try {
                const ticket = engine.update(number, patch as never);
                if (!ticket) {
                    console.error(`❌ Ticket #${number} not found`);
                    process.exit(1);
                }
                console.log(`✏️ Ticket #${number} updated:`);
                console.log(formatTicket(ticket));
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

        case 'validate': {
            const result = engine.validate();
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
                process.exit(1);
            }
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
