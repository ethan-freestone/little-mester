#!/usr/bin/env node
/**
 * Package smoke test — run after `npm run build`.
 *
 * Guards the things that break consumers even when the Jest suite is green:
 *   - package.json entries point at files that the build actually emits
 *   - the built CLI runs without ts-node
 *   - the published tarball contains dist/ and docs/, not tests/src/
 */
'use strict';

const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const pkg = require(path.join(root, 'package.json'));

function fail(message) {
    console.error(`❌ ${message}`);
    process.exit(1);
}

function expectFile(relative, label) {
    const absolute = path.join(root, relative);
    if (!fs.existsSync(absolute)) {
        fail(`${label} '${relative}' does not exist (run npm run build first)`);
    }
    return absolute;
}

for (const field of ['main', 'types']) {
    if (typeof pkg[field] !== 'string') fail(`package.json is missing "${field}"`);
    expectFile(pkg[field], `package.json ${field}`);
}

if (!pkg.bin || typeof pkg.bin['ticket-engine'] !== 'string') {
    fail('package.json is missing bin.ticket-engine');
}
const cliPath = expectFile(pkg.bin['ticket-engine'], 'bin.ticket-engine');
const cliSource = fs.readFileSync(cliPath, 'utf-8');
if (!cliSource.startsWith('#!/usr/bin/env node')) {
    fail(`'${pkg.bin['ticket-engine']}' does not start with a node shebang`);
}

const requiredExports = ['createTicketEngine'];
const engine = require(path.join(root, pkg.main));
for (const name of requiredExports) {
    if (typeof engine[name] !== 'function') {
        fail(`main entry '${pkg.main}' does not export ${name}()`);
    }
}

const help = spawnSync(process.execPath, [cliPath, '--help'], { encoding: 'utf-8' });
if (help.status !== 0 || !help.stdout.includes('Usage:')) {
    fail(`built CLI failed --help (exit ${help.status})\n${help.stderr}`);
}

const pack = spawnSync('npm', ['pack', '--dry-run', '--json'], {
    cwd: root,
    encoding: 'utf-8',
});
if (pack.status !== 0) {
    fail(`npm pack --dry-run failed\n${pack.stderr}`);
}

let packInfo;
try {
    packInfo = JSON.parse(pack.stdout);
} catch (err) {
    fail(`cannot parse npm pack output: ${err.message}`);
}
const files = new Set((packInfo[0]?.files ?? []).map(file => file.path));

for (const required of ['dist/engine.js', 'dist/cli.js', 'dist/mcp-server.js', 'docs/SCHEMA.md']) {
    if (!files.has(required)) fail(`published tarball is missing '${required}'`);
}
for (const forbidden of ['src/engine.ts', 'tests/engine.test.ts', 'package-lock.json']) {
    if (files.has(forbidden)) fail(`published tarball unexpectedly contains '${forbidden}'`);
}

console.log(`✅ package entry points exist (${pkg.main}, ${pkg.types}, ${pkg.bin['ticket-engine']})`);
console.log('✅ main entry exports createTicketEngine');
console.log('✅ built CLI runs --help');
console.log(`✅ tarball contains dist/ and docs/ (${files.size} files)`);
