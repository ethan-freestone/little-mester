#!/usr/bin/env node
/**
 * Little Mester Ticket Engine CLI
 * 
 * Entry point for `npm run dev` — stub for now.
 * Full CLI commands will be implemented per ticket-012/blueprint.md Phase 3.
 */

import { createTicketEngine } from './engine.js';
import * as path from 'path';

const TICKET_DIR = process.env.TICKET_DIR || './docs/tickets';

function resolveDir(raw: string): string {
    return path.resolve(raw);
}

console.log('Little Mester Ticket Engine CLI — stub');
console.log(`Ticket directory: ${resolveDir(TICKET_DIR)}`);
console.log('');
console.log('Available commands (not yet implemented):');
console.log('  create --area <area> --type <type> "Title" [--objective "..."]');
console.log('  list [--area <area>] [--status <status>]');
console.log('  update <number> [--status <status>] [--title <title>] [--objective <obj>]');
console.log('  archive <number>');
console.log('  validate');
console.log('');
console.log('For now, use the MCP server: npm run server');