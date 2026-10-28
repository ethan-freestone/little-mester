# ChatGPT Plus authentication in containers and disposable VMs

> **Status (2026-10-28):** Manual legacy device-code login is verified in the current agent container. Non-device-code browser sign-in is a **required acceptance gate** (#45 A12/A13), not yet verified across the VM/agent boundary. Automatic credential transfer/persistence is also **not implemented yet**. Export existing auth privately before recreating its container or deleting its VM.

Use this runbook when OpenCode runs without a browser, especially inside the agent container or the planned disposable VM. It uses a ChatGPT Plus or Pro subscription; it does not require an OpenAI API key or separate API billing.

## Currently verified login (legacy device code)

Run this where OpenCode is running:

```bash
opencode auth login openai --method chatgpt-headless
```

OpenCode prints a short-lived code and this address:

```text
https://auth.openai.com/codex/device
```

Open that address in any browser on the host, enter the code, sign in to the paid ChatGPT account, and approve access. The container polls OpenAI and receives the result directly, so no callback into the container is required.

Confirm the result:

```bash
opencode auth list
```

The OpenAI entry should report `stored`.

## Browser sign-in must also work (required, not yet implemented)

Do not make the VM depend on Codex device code remaining available. OpenCode V2
documents **Sign in with ChatGPT** and legacy Codex methods. Inspect the methods
offered by the installed version with an interactive `opencode auth login openai`
or `/connect`; use the supported non-device-code method when testing #45. Method
IDs and callback ports are not a stable harness contract.

The existing device-code proof below does **not** satisfy the browser gate.

The normal browser method starts a callback listener and supplies a redirect such as:

```text
http://127.0.0.1:<random-port>/auth/callback
```

When the browser is on the host but OpenCode is in a container, the browser's `127.0.0.1` is the host, not the container. If the selected method uses a callback, it cannot reach OpenCode unless that port is forwarded correctly. The listener belongs to the **server doing auth**, which may be a shared service, not the CLI process.

Required implementation: use supported token-sharing when offered, or create a
temporary loopback-only path **host browser → control SSH → VM → agent/server
listener**. SSH forwarding to guest localhost alone does not reach container
localhost. No general host networking, LAN port publishing, or relaxation of
the egress proxy's private-address rejection is acceptable. Cancel/finish must
remove temporary forwarding. Outbound provider traffic still obeys egress policy.

Ticket #45 owns the exact human procedure and results: browser success with
device-code unavailable, cancellation, occupied callback port, denied egress,
`auth list`, one provider request, container recreation and VM export/re-import.
Do not start an unattended OAuth flow or put auth URLs/state/codes in tickets.

`chatgpt-headless` uses a device-code flow instead. Both sides communicate with OpenAI, so it works across the container or VM boundary without a forwarded callback port.

The available methods observed in this environment were:

```text
chatgpt-token-sharing
chatgpt-browser
chatgpt-headless
key
```

`chatgpt-headless` remains a verified option while available, **not the only supported architecture**. The `key` method requires separately billed OpenAI API credentials and is not a replacement for a ChatGPT Plus subscription or the browser acceptance gate.

## What was verified

On 2026-10-27 we:

1. Ran `opencode auth login openai --method chatgpt-headless` inside the agent container.
2. Opened the displayed device URL in the host browser.
3. Entered the short-lived code and approved access with the ChatGPT account.
4. Confirmed `opencode auth list` displayed `OpenAI ... stored`.

Do not record an actual device code in documentation. Codes expire quickly and are only useful during the active login attempt.

## Where OpenCode stores the login

OpenCode v2 stores saved OAuth credentials in its SQLite database. Check the actual location rather than assuming it:

```bash
opencode debug paths db
```

In the current agent container it reported:

```text
/home/agent/.local/share/opencode/opencode.db
```

That location is currently under `/home/agent`, which `compose.yaml` mounts as tmpfs. The login therefore survives while that container state exists, but it does **not** survive recreation of the agent container. It also will not automatically appear in a new VM.

Do not solve this by copying `opencode.db`. It also contains other OpenCode state and is not OpenCode's supported credential-transfer interface. Use `opencode auth export` and `opencode auth import`.

## Preserve the current login outside the container

The export contains live access and refresh credentials. Run the following **on the Docker host**, replacing `<container>` with the current agent container name or ID. Shell redirection then creates the file on the host rather than inside the container:

```bash
umask 077
install -d -m 700 "$HOME/.local/share/little-mester/secrets"

docker exec <container> opencode auth export openai \
  > "$HOME/.local/share/little-mester/secrets/openai-auth.json"

chmod 600 "$HOME/.local/share/little-mester/secrets/openai-auth.json"
```

Verify only that the file exists and is non-empty; do not print it into logs or chat:

```bash
test -s "$HOME/.local/share/little-mester/secrets/openai-auth.json"
```

If OpenCode is not running in Docker, the equivalent local command is:

```bash
umask 077
opencode auth export openai > /secure/host/path/openai-auth.json
```

## Required VM bootstrap design

A disposable VM cannot be instantly connected without receiving persistent OAuth state. The intended lifecycle is:

```text
One-time login: supported ChatGPT browser or device flow -> host credential escrow
VM up:          host escrow -> control SSH/stdin -> opencode auth import
Agent restart:  guest OpenCode data directory persists outside agent tmpfs
VM down:        latest opencode auth export -> atomic host escrow update
Next VM up:     import the latest host escrow
```

### Import without another login

OpenCode can read an exported credential document from stdin:

```bash
opencode auth import < /secure/path/openai-auth.json
```

For VM automation, stream the host file through the existing control SSH connection rather than adding it to the workspace or cloud-init data. The exact SSH wrapper belongs in `lab-vm`; conceptually it is:

```bash
ssh <vm-control-options> mester@127.0.0.1 \
  'opencode auth import' \
  < "$HOME/.local/share/little-mester/secrets/openai-auth.json"
```

If OpenCode runs only in the agent container, the import must execute in that container or in its startup entrypoint—not merely under the VM's `mester` account.

### Persist state across agent-container recreation

The agent's whole home directory is currently tmpfs:

```yaml
tmpfs:
  - /home/agent
```

The VM implementation must mount a guest-side persistent directory over the narrower OpenCode data path, for example:

```yaml
volumes:
  - /home/mester/.local/share/opencode:/home/agent/.local/share/opencode
```

The guest directory must exist, be writable by the agent UID/GID, and remain private. This nested mount preserves refreshed credentials and OpenCode state across agent-container recreation while leaving the rest of `/home/agent` ephemeral.

### Preserve refreshed credentials across VM deletion

`lab-vm down` currently deletes the qcow2 overlay. Any guest-side OpenCode data therefore disappears at that point. OAuth refresh credentials may change while OpenCode is used, so repeatedly importing an old one-time export is not a durable design.

Before deleting the overlay, `lab-vm down` should:

1. Export the active OpenAI credential from the running OpenCode environment.
2. Transfer it over the control SSH channel to a temporary host file with mode `0600`.
3. Check that the export command succeeded and the file is non-empty.
4. Atomically replace the host escrow file.
5. Only then tear down the agent and delete the VM overlay.

An alternative is a small persistent state disk that `down` never deletes. Do not bake credentials into the shared base image.

## Security rules

The exported JSON grants access to the ChatGPT account. Treat it like a password or SSH private key.

- Never commit it to Git.
- Never save it under `/workspace` or any project being exposed through virtiofs.
- Never put it in `opencode.json`, `compose.yaml`, an image layer, the base VM image, or the cloud-init seed ISO.
- Never print it in CI output, debugging logs, issue reports, or chat.
- Keep the host directory mode `0700` and file mode `0600`.
- Transfer it through stdin or another non-logging channel.
- If it is exposed, run `opencode auth logout openai`, revoke the integration from the OpenAI account if available, and authenticate again.
- The VM and agent necessarily gain access to this credential while using OpenAI. This is an explicit expansion of the original local-model-only trust boundary.

The OpenAI provider also requires outbound network access. An isolated session cannot authenticate or call ChatGPT; the egress policy must permit the OpenAI endpoints used by OpenCode.

## Troubleshooting

### The code expired

Run the login command again and use the new code:

```bash
opencode auth login openai --method chatgpt-headless
```

### OpenCode asks for `--method`

Non-interactive terminals cannot show the method picker. Supply it explicitly:

```bash
opencode auth login openai --method chatgpt-headless
```

### Login worked but vanished later

Check both commands:

```bash
opencode auth list
opencode debug paths db
```

If the database is under `/home/agent`, the tmpfs was probably recreated. Import the host escrow, then implement or repair the guest-side data-directory mount.

### Browser waits on a localhost callback

The browser-based method may be correct. Identify the callback port and the
server namespace; both VM and agent-container hops must be reachable from host
loopback. Do not open a LAN listener or substitute device code and call the
browser test passing. If forwarding is not implemented, record #45 as blocked;
the legacy device flow can be a temporary workaround only while available.

### Verify export/import commands

```bash
opencode auth export --help
opencode auth import --help
```

OpenCode v2's `auth export` output includes secrets. Handle command output accordingly.
