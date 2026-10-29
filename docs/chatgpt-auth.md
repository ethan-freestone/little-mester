# ChatGPT authentication runbook (container and VM)

> **Status (2026-10-29):** Manual legacy device-code login is verified in the current agent container. A temporary host-browser MCP bridge and one-command `./lab-vm auth` flow are implemented with local tests, but have not been live-verified on CachyOS; native browser acceptance remains open (#45). Safe same-port proxy recovery is locally tested but still needs the owner's VM host run (#41). VM/host credential escrow and export-before-delete are **not implemented**; the VM overlay remains the only persistence for guest-created auth. Do not destroy the current host-Docker session or VM state as part of this test.

Use this for operational ChatGPT login and credential-preservation steps when OpenCode runs without a browser. The general auth boundary is [decision C.9.13](DECISIONS.md); acceptance criteria, implementation chronology, and test results belong to [ticket #45](tickets/ticket-045.json). This uses a ChatGPT Plus or Pro subscription; it does not require an OpenAI API key or separate API billing.

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

## Native browser sign-in from the VM

The VM runs OpenCode directly as guest user `mester`, so its native `/connect`
dialog and callback listener are in the same guest/server namespace. For the
pinned OpenCode **2.2.0**, source inspection confirms OpenAI registers method ID
`chatgpt-browser` (shown as **Codex browser (legacy)**), listens on guest
`localhost:1455`, and falls back to `localhost:1457`. This is a browser OAuth
flow, not the separate legacy device-code flow. These are version-specific
details; re-check them when changing the pinned OpenCode version.

1. Run `./lab-vm auth` on the host. It starts the guest TUI, temporary host MCP
   browser bridge, callback forwards for ports 1455/1457, and a reverse SSH
   forward to the bridge. No second terminal or manual URL copy is required.
2. In the guest TUI, run `/connect`, choose **OpenAI**, then choose its browser
   OAuth method (not **Codex device code (legacy)**). When OpenCode requests a
   browser open, a temporary guest `xdg-open` shim calls the host MCP server's
   sole `open_auth_url` tool. The host server accepts only HTTPS URLs on the
   exact `auth.openai.com` or `chatgpt.com` origins and opens them with the host
   default browser. It has no arbitrary command/file/network tool and does not
   handle the OAuth callback.
3. Keep the TUI running through success or cancellation, then exit OpenCode.
   The command removes the guest token/helper and closes the SSH forwards and
   host MCP process. The host callback ports are loopback-only; callback traffic
   still goes directly to guest `localhost` over the `-L` SSH forwards.
4. Check the saved account in the guest with `opencode auth list`, then select
   an OpenAI model and make one ordinary completion request.

The host MCP listener binds only to `127.0.0.1:19533`; the guest reaches it only
through a reverse forward on guest loopback. A random, per-run bearer token is
sent to the guest over SSH stdin and held in a mode-`0600` temporary file. The
bridge and opener never log or retain authorization URLs, OAuth state, codes,
or tokens. The guest shim refuses all non-allowlisted URLs instead of falling
back to a general host browser launcher. If this OpenCode build does not use
`xdg-open` for the native auth action, the live acceptance run must stop and
record that behavior; do not paste or log the URL as a workaround.

`./lab-vm auth-forward` remains a manual callback-only fallback for diagnosing
the guest listener, but requires a separate terminal and manual URL handoff.

The callback URI uses `http://localhost:1455/auth/callback` (or port 1457 if
the guest's primary port is occupied). The host browser's localhost callback
reaches only the guest OpenCode callback listener through SSH; no LAN listener,
new QEMU host-forward, general host route, or credential copy is created. If a
host callback port is occupied, SSH fails closed with `ExitOnForwardFailure`;
stop the conflicting local listener before retrying. The guest still sends
OAuth token exchange/refresh and model traffic through the restricted egress
proxy. The workspace allowlist includes only the two OpenAI-specific hostnames
needed for those requests, `auth.openai.com` and `chatgpt.com`, alongside its
existing package domains. Because this is a workspace-level policy, the same
destinations are also allowed to host-Docker sessions using this workspace. A
running VM proxy keeps its startup policy snapshot; after changing the manifest,
run `./lab-vm egress-reload` to refresh the proxy on its existing loopback port
without restarting QEMU or changing the overlay. A proxy started by older code
may be replaced briefly to add safe reload support; active proxy connections can
drop during that one-time upgrade.

This wiring has **not yet passed live browser acceptance**. Ticket #45 still
requires an owner-controlled test of automatic host browser launch, success and
cancellation cleanup, occupied-port and egress-denied checks, `auth list`, and
an actual provider request. Never record the authorization URL, OAuth state,
code, or token in logs, tickets, or chat.

`chatgpt-headless` is a distinct device-code method. It does not exercise this
callback route and does not satisfy the browser-flow acceptance gate.

The methods previously observed in the current agent container (not a guarantee
for the pinned guest version) were:

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

Use `./lab-vm auth` for the normal single-command flow; it owns both the
temporary host-browser handoff and loopback callback forwards. If it fails,
record the OpenCode version and a redacted error, then stop rather than copying
the authorization URL or opening a LAN listener. `./lab-vm auth-forward` is a
callback-only diagnostic fallback for a separately controlled test; it does not
open the browser or satisfy the single-command browser acceptance gate.

### Verify export/import commands

```bash
opencode auth export --help
opencode auth import --help
```

OpenCode v2's `auth export` output includes secrets. Handle command output accordingly.
