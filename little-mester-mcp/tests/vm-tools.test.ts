import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

const serverPath = path.resolve(__dirname, '../dist/mcp-server.js');

async function connectOrdinaryServer(env: Record<string, string>, cwd = path.resolve(__dirname, '../..')) {
    const client = new Client({ name: 'vm-boundary-test', version: '1.0.0' }, { capabilities: {} });
    const transport = new StdioClientTransport({
        command: process.execPath,
        args: [serverPath, '--vm-host', '--workspace=/etc', '--role=host-owner'],
        cwd,
        env: { ...process.env, ...env } as Record<string, string>,
        stderr: 'pipe',
    });
    await client.connect(transport);
    return { client, transport };
}

describe('host-only VM MCP boundary', () => {
    it('ordinary ticket stdio server does not register lifecycle tools under hostile env and args', async () => {
        const marker = path.join(os.tmpdir(), `vm-core-called-${process.pid}`);
        fs.rmSync(marker, { force: true });
        const { client, transport } = await connectOrdinaryServer({
            MESTER_VM_HOST_MODE: '1',
            MESTER_VM_INSTALL_ROOT: '/tmp/forged-install',
            MESTER_VM_CORE: `touch ${marker}`,
            MESTER_VM_WORKSPACE: '/etc',
            MESTER_VM_ROLE: 'host-owner',
        });

        try {
            const result = await client.listTools();
            const names = result.tools.map(tool => tool.name);
            expect(names).toEqual(expect.arrayContaining([
                'mester_tickets_create', 'mester_tickets_list', 'mester_tickets_get',
                'mester_tickets_update', 'mester_tickets_archive', 'mester_tickets_search',
                'mester_tickets_board', 'mester_tickets_validate',
                'mester_tickets_reconcile_054_fingerprint_case',
            ]));
            expect(names.some(name => name.startsWith('mester_vm_'))).toBe(false);

            let callRejected = false;
            try {
                const call = await client.callTool({ name: 'mester_vm_smoke', arguments: { workspace: '/etc' } });
                callRejected = call.isError === true;
            } catch {
                callRejected = true;
            }
            expect(callRejected).toBe(true);
            expect(fs.existsSync(marker)).toBe(false);
        } finally {
            await client.close();
            await transport.close();
            fs.rmSync(marker, { force: true });
        }
    });

    it('ordinary server remains ticket-only from a hostile workspace config directory', async () => {
        const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'vm-hostile-workspace-'));
        fs.writeFileSync(path.join(workspace, '.little-mester.conf'), [
            'MESTER_VM_HOST_MODE=1',
            'MESTER_VM_CORE=touch /tmp/vm-workspace-config-executed',
            'MESTER_VM_WORKSPACE=/etc',
            'MESTER_VM_ROLE=host-owner',
            '',
        ].join('\n'));
        fs.chmodSync(path.join(workspace, '.little-mester.conf'), 0o666);
        const marker = '/tmp/vm-workspace-config-executed';
        fs.rmSync(marker, { force: true });
        const { client, transport } = await connectOrdinaryServer({
            MESTER_VM_HOST_MODE: '1', MESTER_VM_CORE: `touch ${marker}`,
        }, workspace);

        try {
            const names = (await client.listTools()).tools.map(tool => tool.name);
            expect(names.some(name => name.startsWith('mester_vm_'))).toBe(false);
            const call = await client.callTool({ name: 'mester_vm_smoke', arguments: { workspace: '/etc' } });
            expect(call.isError).toBe(true);
            expect(fs.existsSync(marker)).toBe(false);
        } finally {
            await client.close();
            await transport.close();
            fs.rmSync(workspace, { recursive: true, force: true });
            fs.rmSync(marker, { force: true });
        }
    });

    it.todo('dedicated host entrypoint refuses without installed provenance before opening stdio');
});
